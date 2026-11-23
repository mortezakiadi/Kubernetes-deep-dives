"""The credit worker (Design 08, "Credit assignment: a ladder of methods" and "Branching rollouts").

    credit.py --run R --trajectories A,B,... --k 4 --max-points 3 [--wait-seconds 3600]

For each trajectory it picks branch points among the turns that have a snapshot (most uncertain action choice first,
then edits, then the first test run, then the every-third-turn fallback), submits one Indexed Job of k branch
rollouts per point to the low-priority branch queue, waits, and turns the branches' verified outcomes into values:

    V(start)  = the mean outcome of all root attempts at the same task in the run (the group baseline)
    V(t)      = the mean outcome of the k continuations from the snapshot after turn t
    advantage of every turn in the segment (previous point, t]  =  V(t) - V(previous point)
    advantage of the turns after the last point                  =  outcome - V(last point)

and, for comparison, the trajectory-level (GRPO-style) advantage every turn of the trajectory would get:
(outcome - group mean) / group standard deviation. Writes credit/<run>/<trajectory>.json.

It talks to the Kubernetes API with its own service account (allowed to create and read Jobs in rl-envs only).
"""
import argparse
import hashlib
import json
import re
import ssl
import statistics
import time
import urllib.request

from common import get, listing, put
from jobs import NS, branch_job

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
PRIORITY = {"uncertain": 0, "edit": 1, "first-test-run": 2, "every-3": 3}


def kube(method, path, body=None):
    token = open(f"{SA}/token").read()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
        return json.loads(r.read())


def job_done(name):
    j = kube("GET", f"/apis/batch/v1/namespaces/{NS}/jobs/{name}")
    for c in j.get("status", {}).get("conditions", []):
        if c["type"] in ("Complete", "Failed") and c["status"] == "True":
            return c["type"]
    return None


def pick_points(traj, max_points):
    cands = [t for t in traj["turns"] if t.get("snapshot_id") and t["action"].get("tool") != "submit"]
    cands.sort(key=lambda t: (PRIORITY.get(t["branch_reason"], 9), -t["entropy_first_action_token"], t["index"]))
    return sorted(t["index"] for t in cands[:max_points])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--trajectories", required=True)
    p.add_argument("--k", type=int, default=4)
    p.add_argument("--max-points", type=int, default=3)
    p.add_argument("--wait-seconds", type=int, default=5400)
    a = p.parse_args()
    t0 = time.time()
    trajs = {tid: get(f"trajectories/{tid}.json") for tid in a.trajectories.split(",")}
    points = {tid: pick_points(tr, a.max_points) for tid, tr in trajs.items()}
    jobs = {}
    for tid, pts in points.items():
        for t in pts:
            name = f"br-{hashlib.sha256((a.run + tid).encode()).hexdigest()[:10]}-{t}"
            kube("POST", f"/apis/batch/v1/namespaces/{NS}/jobs", branch_job(name, a.run, tid, t, a.k))
            jobs[(tid, t)] = name
    print(json.dumps({"submitted_branch_jobs": len(jobs), "branches": len(jobs) * a.k}), flush=True)
    end = time.time() + a.wait_seconds
    while time.time() < end and not all(job_done(n) for n in jobs.values()):
        time.sleep(15)
    t_wait = time.time() - t0
    for tid, tr in trajs.items():
        group = [get(k)["outcome"] for k in listing(f"trajectories/{tr['run']}-{tr['task_id']}-s") if re.search(r"-s\d+\.json$", k)]
        v_start = statistics.mean(group) if group else 0.0
        sd = statistics.pstdev(group) if len(group) > 1 else 0.0
        values, branches = {}, {}
        for t in points[tid]:
            outs = []
            for i in range(a.k):
                try:
                    b = get(f"trajectories/{tid}-b{t:03d}-{a.run}-{i}.json")
                    outs.append(b["outcome"])
                    branches.setdefault(t, []).append({"id": b["trajectory_id"], "outcome": b["outcome"], "new_turns": b["new_turns"],
                                                       "env_seconds": b["env_seconds"]})
                except Exception:
                    pass
            if outs:
                values[t] = statistics.mean(outs)
        pts = [t for t in points[tid] if t in values]
        n = len(tr["turns"])
        mc = [0.0] * n
        prev_v, prev_t = v_start, -1
        for t in pts:
            for i in range(prev_t + 1, t + 1):
                mc[i] = round(values[t] - prev_v, 4)
            prev_v, prev_t = values[t], t
        for i in range(prev_t + 1, n):
            mc[i] = round(tr["outcome"] - prev_v, 4)
        traj_adv = round((tr["outcome"] - v_start) / sd, 4) if sd > 0 else 0.0
        segs = [(pts[i - 1] if i else -1, t, values[t] - (values[pts[i - 1]] if i else v_start)) for i, t in enumerate(pts)]
        decisive = max(segs, key=lambda s: abs(s[2]), default=None)
        rec = {"trajectory_id": tid, "task_id": tr["task_id"], "outcome": tr["outcome"], "group_outcomes": group, "v_start": round(v_start, 4),
               "branch_points": pts, "values": {str(t): round(v, 4) for t, v in values.items()}, "k": a.k, "branches": {str(t): b for t, b in branches.items()},
               "turn_advantage_mc": mc, "turn_advantage_trajectory": [traj_adv] * n,
               "decisive_segment": {"after_turn": decisive[0], "to_turn": decisive[1], "delta_v": round(decisive[2], 4)} if decisive else None,
               "branch_env_seconds": round(sum(x["env_seconds"] for bs in branches.values() for x in bs), 1), "wall_seconds": round(t_wait, 1)}
        put(f"credit/{a.run}/{tid}.json", rec)
        print(json.dumps({k: rec[k] for k in ("trajectory_id", "outcome", "v_start", "values", "decisive_segment")}), flush=True)


if __name__ == "__main__":
    main()

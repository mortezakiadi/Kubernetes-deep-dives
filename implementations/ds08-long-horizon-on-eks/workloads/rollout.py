"""Runs episodes: the agent acts in its environment turn by turn, and every turn is recorded.

    rollout.py root   --run R --tasks t1-00,t2-03 --seeds 0,1,2,3       (Job index i takes every n-th episode)
    rollout.py branch --run R --from TRAJ --turn T                       (Job index i is the branch number)

A root episode starts from the task's files. A branch starts from the snapshot saved after turn T of trajectory TRAJ,
with the context the model had then, and continues with a different sampling seed. Both end with the verifier's
fresh-environment check. The trajectory is the unit of storage; each turn carries what credit assignment needs:
its context, its log-probabilities, the entropy of its action choice, the environment's progress checks and, at
branch points, a snapshot.
"""
import argparse
import hashlib
import json
import os
import pathlib
import time

import agent
from common import get, progress, put, task, verify
from sandbox import Workspace, action_key

ENV_IMAGE = os.environ.get("ENV_IMAGE", "unknown")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "16"))
ENTROPY_BRANCH = 0.5     # a turn whose action choice was this uncertain becomes a branch point
EVERY_N = 3              # fallback: every third turn is a branch point
MAX_IDENTICAL = 3        # loop guard: the same action three times in a row ends the episode
WORK = pathlib.Path(os.environ.get("WORK_DIR", "/tmp/ws"))


def branch_reason(t, action, entropy, seen_tools):
    if entropy >= ENTROPY_BRANCH:
        return "uncertain"
    if action.get("tool") in ("replace", "write"):
        return "edit"
    if action.get("tool") == "run_tests" and "run_tests" not in seen_tools:
        return "first-test-run"
    if t % EVERY_N == EVERY_N - 1:
        return "every-3"
    return None


def episode(traj_id, task_id, seed, prefix=None, run="", script=None):
    """prefix: None for a root episode, or {from, turn, turns, compacted_upto, summary_boundaries, snapshot}."""
    t_start = time.time()
    pub = task(task_id)
    ws_dir = WORK / traj_id
    if prefix:
        ws = Workspace.restore(ws_dir, get(prefix["snapshot"], raw=True))
        turns = [dict(t) for t in prefix["turns"]]
        compacted_upto, boundaries = prefix["compacted_upto"], list(prefix["summary_boundaries"])
    else:
        ws = Workspace.from_files(ws_dir, pub["files"])
        turns, compacted_upto, boundaries = [], 0, []
    phi_start = progress({"task_id": task_id, "files": ws.files()})["phi"]
    seen = {t["action"].get("tool") for t in turns}
    stop, env_s, gen_s = "max_turns", 0.0, 0.0
    first_new = len(turns)
    for t in range(first_new, MAX_TURNS):
        msgs, compacted_upto, compacted_now = agent.context_for(pub["issue"], turns, compacted_upto)
        if compacted_now:
            boundaries.append(t)
        g0 = time.time()
        if script is not None:   # a scripted policy (tests and adversarial checks): fixed outputs, no model call
            if t >= len(script):
                break
            g = {"output": script[t], "finish_reason": "scripted", "prompt_tokens": 0, "completion_tokens": 0, "token_ids": [], "logprobs": [], "lp_raw": []}
        else:
            g = agent.generate(msgs, seed=int(hashlib.sha256(f"{traj_id}:{t}".encode()).hexdigest()[:7], 16))
        gen_s += time.time() - g0
        action = agent.parse_action(g["output"])
        e0 = time.time()
        obs = ws.act(action) if action["tool"] != "invalid" else f"ERROR: {action['error']}. Reply with one ACTION line."
        checks = progress({"task_id": task_id, "files": ws.files()})
        env_s += time.time() - e0
        entropy, _ = agent.first_action_entropy(g["lp_raw"]) if g["lp_raw"] else (0.0, 0)
        reason = branch_reason(t, action, entropy, seen)
        snap = None
        if reason:
            snap = f"snapshots/{traj_id}/{t:03d}.tgz"
            put(snap, ws.snapshot())
        put(f"contexts/{traj_id}/{t:03d}.json", msgs)
        put(f"logprobs/{traj_id}/{t:03d}.json", {"token_ids": g["token_ids"], "logprobs": g["logprobs"]})
        turns.append({"index": t, "prompt_tokens": g["prompt_tokens"], "completion_tokens": g["completion_tokens"],
                      "output": g["output"], "action": action, "observation": obs, "logprobs_ref": f"logprobs/{traj_id}/{t:03d}.json",
                      "context_ref": f"contexts/{traj_id}/{t:03d}.json", "policy_version": 0, "entropy_first_action_token": entropy,
                      "snapshot_id": snap, "branch_reason": reason, "checks": checks, "tree_sha256": ws.tree_hash()[:16],
                      "finish_reason": g["finish_reason"], "from_branch": bool(prefix), "compacted_upto": compacted_upto})
        seen.add(action.get("tool"))
        last = [action_key(x["action"]) for x in turns[-MAX_IDENTICAL:]]
        if action.get("tool") == "submit":
            stop = "submit"
            break
        if len(last) == MAX_IDENTICAL and len(set(last)) == 1:
            stop = "loop_guard"
            break
    v0 = time.time()
    result = verify({"task_id": task_id, "files": ws.files(), "actions": [t["action"] for t in turns],
                     "observations": [t["observation"][:400] for t in turns], "outputs": [t["output"][-600:] for t in turns]})
    env_s += time.time() - v0
    traj = {"trajectory_id": traj_id, "task_id": task_id, "bugs": pub["bugs"], "env_image": ENV_IMAGE, "seed": seed, "run": run,
            "turns": turns, "outcome": result["outcome"], "verifier_version": result["verifier_version"], "verification": result,
            "summary_boundaries": boundaries, "stop": stop, "phi_start": phi_start, "start": {k: prefix[k] for k in ("from", "turn")} if prefix else None,
            "new_turns": len(turns) - first_new, "seconds": round(time.time() - t_start, 1), "env_seconds": round(env_s, 1),
            "generate_seconds": round(gen_s, 1), "final_tree_sha256": ws.tree_hash()[:16]}
    put(f"trajectories/{traj_id}.json", traj)
    print(json.dumps({k: traj[k] for k in ("trajectory_id", "task_id", "bugs", "outcome", "stop", "new_turns", "seconds")}), flush=True)
    return traj


def replay(from_id, turn, run):
    """Determinism by contract: restore the snapshot after `turn`, re-apply the recorded actions of the turns after it in
    a new working copy, and compare every observation and every state hash with the recording."""
    src = get(f"trajectories/{from_id}.json")
    ws = Workspace.restore(WORK / f"replay-{from_id}", get(src["turns"][turn]["snapshot_id"], raw=True))
    rows = []
    for t in src["turns"][turn + 1:]:
        obs = ws.act(t["action"]) if t["action"]["tool"] != "invalid" else None
        rows.append({"turn": t["index"], "tool": t["action"]["tool"], "same_observation": obs is None or obs == t["observation"],
                     "same_state": ws.tree_hash()[:16] == t["tree_sha256"]})
    rec = {"trajectory": from_id, "from_turn": turn, "replayed_turns": len(rows), "all_identical": all(r["same_observation"] and r["same_state"] for r in rows), "rows": rows}
    put(f"curriculum/replay/{run}/{from_id}.json", rec)
    print(json.dumps({k: rec[k] for k in ("trajectory", "from_turn", "replayed_turns", "all_identical")}), flush=True)


def prefix_of(from_id, turn):
    src = get(f"trajectories/{from_id}.json")
    t = src["turns"][turn]
    if not t.get("snapshot_id"):
        raise SystemExit(f"turn {turn} of {from_id} is not a branch point (no snapshot)")
    return src, {"from": from_id, "turn": turn, "turns": src["turns"][: turn + 1], "snapshot": t["snapshot_id"],
                 "compacted_upto": t["compacted_upto"], "summary_boundaries": [b for b in src["summary_boundaries"] if b <= turn]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["root", "branch", "scripted", "replay"])
    p.add_argument("--script", help="trajectory store key of a JSON list of scripted replies (scripted mode)")
    p.add_argument("--run", required=True)
    p.add_argument("--tasks", default="")
    p.add_argument("--seeds", default="0")
    p.add_argument("--from", dest="src")
    p.add_argument("--turn", type=int)
    a = p.parse_args()
    i, n = int(os.environ.get("JOB_COMPLETION_INDEX", "0")), int(os.environ.get("JOB_PARALLELISM", "1"))
    if a.mode == "root":
        work = [(tid, int(s)) for tid in a.tasks.split(",") for s in a.seeds.split(",")]
        for tid, s in work[i::n]:
            episode(f"{a.run}-{tid}-s{s}", tid, s, run=a.run)
    elif a.mode == "replay":
        replay(a.src, a.turn, a.run)
    elif a.mode == "scripted":
        episode(f"{a.run}-{a.tasks}-scripted", a.tasks, 0, run=a.run, script=get(a.script))
    else:
        src, prefix = prefix_of(a.src, a.turn)
        episode(f"{a.src}-b{a.turn:03d}-{a.run}-{i}", src["task_id"], 1000 + i, prefix=prefix, run=a.run)


if __name__ == "__main__":
    main()

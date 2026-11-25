"""Jobs of the reward-quality platform. Each subcommand runs as a Kubernetes Job, calls the verifier Deployments over
HTTP, and writes its result through the store.

    quality   --verifier V --gold FILE --out KEY            precision, recall, abstention per slice on a gold set
    policyq   --verifier V --run R --out KEY                the same on a run's own rollouts, gold = the eval verifier
                                                            agreeing with the exact solution (math only)
    fuzz      --verifiers V1,V2 --policy-samples none|latest|runs/R --out PREFIX
    regress   --verifier V --cases KEY --out KEY            every known exploit must still be rejected
    strength  --out KEY                                     mutation score of each code task's hidden tests
    rescore   --run R --verifier V --out PREFIX             re-score a run's stored rollouts with another version
    review    --run R --budget N --out KEY                  build the human review queue for a run
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fuzz import fuzz_code, fuzz_math, run_regressions  # noqa: E402
from quality import quality  # noqa: E402

STORE = os.environ.get("STORE_URL", "http://store.platform.svc:8080")


def http(method, url, body=None, timeout=600):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def put(key, obj):
    http("PUT", f"{STORE}/o/{key}", obj if isinstance(obj, bytes) else json.dumps(obj, indent=1).encode())


def get(key):
    return json.loads(http("GET", f"{STORE}/o/{key}"))


def rows(key):
    return [json.loads(l) for l in http("GET", f"{STORE}/o/{key}").decode().splitlines() if l.strip()]


def keys(prefix):
    return json.loads(http("GET", f"{STORE}/list/{prefix}"))


def url_of(vid):
    return f"http://{vid.replace('@', '-').replace('.', '-')}.rewards.svc:8080"


class _V:
    def __init__(self, verdict):
        self.verdict = type("Vd", (), {"value": verdict})()


class Remote:
    """A verifier Deployment seen through the same verify() interface the fuzzers use."""

    def __init__(self, vid):
        self.vid, self.url = vid, url_of(vid)
        self.info = json.loads(http("GET", self.url + "/info"))

    def batch(self, items):
        out = []
        for i in range(0, len(items), 32):
            out += json.loads(http("POST", self.url + "/verify", {"items": items[i:i + 32]}))["results"]
        return out

    def verify(self, task, completion):
        return _V(self.batch([{"task_id": task["id"], "completion": completion}])[0]["verdict"])


def tasks_by_id():
    out = {}
    for f in ("math_train", "math_eval", "math_perturbed", "code_tasks", "judge_tasks"):
        for t in rows(f"data/{f}.jsonl"):
            out[t["id"]] = t
    return out


def cmd_quality(a):
    v = Remote(a.verifier)
    gold = rows(f"data/{a.gold}")
    t0 = time.time()
    res = v.batch([{"task_id": g["task_id"], "completion": g["completion"]} for g in gold])
    per_kind = {}
    for g, r in zip(gold, res):
        per_kind.setdefault(g["kind"], {}).setdefault(r["verdict"], 0)
        per_kind[g["kind"]][r["verdict"]] += 1
    q = quality([{"slice": g["slice"], "verdict": r["verdict"], "gold": g["gold"]} for g, r in zip(gold, res)])
    reasons = {}
    for r in res:
        for x in r["reasons"]:
            reasons[x.split("=")[0]] = reasons.get(x.split("=")[0], 0) + 1
    out = {"verifier": a.verifier, "code_sha256": v.info["code_sha256"], "gold": a.gold, "n": len(gold), "seconds": round(time.time() - t0, 1),
           "quality": q, "per_kind": per_kind, "reasons": reasons,
           "examples": [{"kind": g["kind"], "gold": g["gold"], "verdict": r["verdict"], "reasons": r["reasons"], "completion": g["completion"][:300]}
                        for g, r in zip(gold, res) if (r["verdict"] == "pass") != g["gold"]][:30]}
    put(a.out, out)
    print(json.dumps({"verifier": a.verifier, "all": q["all"], "per_kind": per_kind}))


def run_rollouts(run, last_steps=None):
    ks = sorted(keys(f"runs/{run}/rollouts/"))
    if last_steps:
        ks = ks[-last_steps:]
    return [x for k in ks for x in rows(k)]


def cmd_policyq(a):
    """Precision on the policy's own outputs. Gold: the evaluation verifier, which shares no code with the training
    verifiers, says pass. (A stand-in for people: see the appendix, section 6.)"""
    v, ev = Remote(a.verifier), Remote("eval-math@1.0.0")
    roll = run_rollouts(a.run, a.last_steps)
    random.Random(0).shuffle(roll)
    roll = roll[:a.sample]
    items = [{"task_id": x["task_id"], "completion": x["completion"]} for x in roll]
    res, gold = v.batch(items), ev.batch(items)
    q = quality([{"slice": "policy_outputs", "verdict": r["verdict"], "gold": g["verdict"] == "pass"} for r, g in zip(res, gold)])
    disagree = [{"task_id": x["task_id"], "verifier": r["verdict"], "verifier_reasons": r["reasons"], "gold": g["verdict"], "gold_reasons": g["reasons"],
                 "completion_end": x["completion"][-240:]} for x, r, g in zip(roll, res, gold) if (r["verdict"] == "pass") != (g["verdict"] == "pass")]
    put(a.out, {"verifier": a.verifier, "run": a.run, "n": len(items), "gold_by": "eval-math@1.0.0", "quality": q, "disagreements": disagree[:40]})
    print(json.dumps({"verifier": a.verifier, "run": a.run, "all": q["all"]}))


def cmd_fuzz(a):
    T = tasks_by_id()
    math_tasks = [t for i, t in T.items() if i.startswith("m-ev-")][:60]
    code_tasks = [t for i, t in T.items() if i.startswith("c-")]
    samples = None
    if a.policy_samples != "none":
        run = a.policy_samples.split("/", 1)[1] if a.policy_samples.startswith("runs/") else None
        if run is None:
            runs = sorted({k.split("/")[1] for k in keys("runs/") if "/rollouts/" in k})
            run = runs[-1] if runs else None
        samples = run_rollouts(run, 5) if run else None
        if samples:
            math_tasks = [T[i] for i in sorted({s["task_id"] for s in samples})][:60]
    ref_m, ref_c = Remote("strict-reference-math@1.0.0"), Remote("strict-reference-code@1.0.0")
    report = {"policy_samples": a.policy_samples, "samples_used": len(samples or []), "verifiers": {}}
    for vid in a.verifiers.split(","):
        v = Remote(vid)
        r = fuzz_math(v, ref_m, math_tasks, samples) if vid.startswith("math") else fuzz_code(v, ref_c, code_tasks)
        report["verifiers"][vid] = {**r, "code_sha256": v.info["code_sha256"]}
        print(json.dumps({"verifier": vid, "cases": r["cases"], "false_positives": r["false_positives"], "by_mutation": r["by_mutation"]}), flush=True)
    put(f"{a.out}/report.json", report)
    # Confirmed false positives become permanent regression cases: the reference verdict is the expected one.
    cases = [{"task_id": f["task_id"], "mutation": f["mutation"], "completion": f["completion"], "expect": "not_pass"}
             for r in report["verifiers"].values() for f in r["examples"] if f["kind"] == "false_positive"]
    put(f"{a.out}/regressions.jsonl", "".join(json.dumps(c) + "\n" for c in cases).encode())


def cmd_regress(a):
    v = Remote(a.verifier)
    T = tasks_by_id()
    cases = [c for c in rows(a.cases) if (c["task_id"].startswith("m-") == a.verifier.startswith("math"))]
    r = run_regressions(v, T, cases)
    put(a.out, {"verifier": a.verifier, "code_sha256": v.info["code_sha256"], **r})
    print(json.dumps({"verifier": a.verifier, "cases": r["cases"], "passed": r["passed"], "failed": r["failed"]}))


def cmd_strength(a):
    from vcode import mutation_score
    out = {}
    for t in rows("data/code_tasks.jsonl"):
        s, caught = mutation_score(t)
        out[t["id"]] = {"mutation_score": s, "caught": caught, "hidden_cases": len(t["hidden"]) + len(t["edge"]), "weak_by_design": t["weak"],
                        "admitted": s >= 0.85}
    put(a.out, out)
    print(json.dumps(out))


def cmd_rescore(a):
    v = Remote(a.verifier)
    roll = run_rollouts(a.run)
    res = v.batch([{"task_id": x["task_id"], "completion": x["completion"]} for x in roll])
    changed = [{"step": x["step"], "task_id": x["task_id"], "old": x["train"]["verdict"], "old_verifier": x["train"]["verifier"], "new": r["verdict"],
                "new_reasons": r["reasons"]} for x, r in zip(roll, res) if x["train"]["verdict"] != r["verdict"]]
    flips = {}
    for c in changed:
        flips[f"{c['old']}->{c['new']}"] = flips.get(f"{c['old']}->{c['new']}", 0) + 1
    old_rate = sum(x["train"]["verdict"] == "pass" for x in roll) / max(len(roll), 1)
    new_rate = sum(r["verdict"] == "pass" for r in res) / max(len(roll), 1)
    put(f"{a.out}/summary.json", {"run": a.run, "rescored_with": v.vid, "code_sha256": v.info["code_sha256"],
                                  "old_verifiers": sorted({x["train"]["verifier"] for x in roll}), "samples": len(roll), "changed": len(changed),
                                  "flips": flips, "pass_rate_old": round(old_rate, 4), "pass_rate_new": round(new_rate, 4)})
    put(f"{a.out}/samples.jsonl", "".join(json.dumps({**x, "rescored": r}) + "\n" for x, r in zip(roll, res)).encode())
    print(json.dumps({"samples": len(roll), "changed": len(changed), "flips": flips, "pass_rate_old": round(old_rate, 4), "pass_rate_new": round(new_rate, 4)}))


def cmd_review(a):
    """Route to people: every abstention, every train/audit disagreement, and a random share of confident passes, high
    priority first. Nobody reviews them in this build; the queue is what a reviewer would open."""
    roll = run_rollouts(a.run)
    rng = random.Random(1)
    q = []
    for x in roll:
        t, u = x["train"], x["audit"]
        if t["verdict"] == "abstain":
            q.append({"priority": 2, "why": "abstained", **x})
        elif t["verdict"] == "pass" and u["verdict"] != "pass":
            q.append({"priority": 1, "why": "train passed, audit did not (suspected exploit)", **x})
        elif t["verdict"] == "pass" and rng.random() < 0.05:
            q.append({"priority": 3, "why": "random sample of confident passes (measures precision)", **x})
    q.sort(key=lambda r: (r["priority"], -r["step"]))
    chosen = q[:a.budget]
    by = {}
    for r in chosen:
        by[r["why"]] = by.get(r["why"], 0) + 1
    put(a.out, {"run": a.run, "candidates": len(q), "budget": a.budget, "queued": len(chosen), "by_reason": by,
                "items": [{k: r[k] for k in ("priority", "why", "step", "task_id", "completion")} | {"train_reasons": r["train"]["reasons"]} for r in chosen]})
    print(json.dumps({"candidates": len(q), "queued": len(chosen), "by_reason": by}))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("quality"); s.add_argument("--verifier"); s.add_argument("--gold"); s.add_argument("--out")
    s = sub.add_parser("policyq"); s.add_argument("--verifier"); s.add_argument("--run"); s.add_argument("--out"); s.add_argument("--sample", type=int, default=400); s.add_argument("--last-steps", type=int, default=10)
    s = sub.add_parser("fuzz"); s.add_argument("--verifiers"); s.add_argument("--policy-samples", default="none"); s.add_argument("--out")
    s = sub.add_parser("regress"); s.add_argument("--verifier"); s.add_argument("--cases"); s.add_argument("--out")
    s = sub.add_parser("strength"); s.add_argument("--out")
    s = sub.add_parser("rescore"); s.add_argument("--run"); s.add_argument("--verifier"); s.add_argument("--out")
    s = sub.add_parser("review"); s.add_argument("--run"); s.add_argument("--budget", type=int, default=50); s.add_argument("--out")
    a = ap.parse_args()
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()

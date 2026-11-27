"""State and small helpers shared by the scenario files."""
import json
import random
import time

from harness import KUBECTL, ROOT, Step, redact
from helpers import apply, cond, kj, workload

OUT = [ROOT / "evidence" / "final"]
STATE = {}


def save_state():
    (OUT[0] / "state.json").write_text(json.dumps(STATE, indent=1, default=str))


def show(sc, display, output):
    """A step whose output this program computed from real data (the cluster, the accounting service)."""
    sc.steps.append(Step(redact(display), redact(str(output)), 0, 0))


def k(sc, *args, display=None, **kw):
    return sc.run(display or ("kubectl " + " ".join(args)), [KUBECTL, *args], **kw)


def submit(sc, objs, display):
    """kubectl apply of generated objects, recorded as one step."""
    return sc.run(display, [KUBECTL, "apply", "-f", "-"], stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}))


def record_waits(scenario, jobs):
    """Queue wait of each Kueue workload: from its creation to quota reservation (for S11)."""
    from datetime import datetime
    ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    for ns, name, gpus in jobs:
        w = workload(ns, name)
        qr = cond(w, "QuotaReserved")
        if w and qr.get("status") == "True":
            STATE.setdefault("waits", []).append({"scenario": scenario, "job": name, "gpus": gpus,
                                                  "wait_s": round(ts(qr["lastTransitionTime"]) - ts(w["metadata"]["creationTimestamp"]), 1)})
    save_state()


def small_trace(seed=17, target=64):
    """The S06 trace: 1-, 2- and 4-GPU single-pod jobs until `target` GPUs are requested."""
    rng, out, total = random.Random(seed), [], 0
    while total < target:
        g = rng.choice([1, 1, 2, 2, 4])
        g = min(g, target - total)
        out.append(g)
        total += g
    return out


def kueue_version():
    d = kj("-n", "kueue-system", "get", "deploy", "kueue-controller-manager")
    return d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "?")


def now():
    return time.time()

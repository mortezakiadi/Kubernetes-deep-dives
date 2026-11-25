"""State and small helpers shared by the scenario files."""
import json

from harness import KUBECTL, ROOT, Step, redact
from helpers import VQ

OUT = [ROOT / "evidence" / "final"]
STATE = {}


def save_state():
    (OUT[0] / "state.json").write_text(json.dumps(STATE, indent=1, default=str))


def show(sc, display, output):
    """A step whose output this program computed from real data (the store, the cluster) rather than one command."""
    sc.steps.append(Step(redact(display), redact(str(output)), 0, 0))


def k(sc, *args, display=None, **kw):
    return sc.run(display or ("kubectl " + " ".join(args)), [KUBECTL, *args], **kw)


def vq(sc, *args, display=None, timeout=7200):
    return sc.run(display or ("vq.py " + " ".join(str(a) for a in args)), VQ + [str(a) for a in args], timeout=timeout)


def job(sc, name, *args, display=None, timeout=3600):
    """One platform job: tools.py <args> as a Kubernetes Job in the rewards namespace."""
    return vq(sc, "job", name, "--", *args, display=display or ("vq.py job " + name + " -- " + " ".join(str(a) for a in args)), timeout=timeout)

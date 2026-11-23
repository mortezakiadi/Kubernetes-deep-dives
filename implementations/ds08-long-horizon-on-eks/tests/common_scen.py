"""State and small helpers shared by the scenario files."""
import json
import pathlib

from harness import KUBECTL, ROOT, Step, redact
from helpers import LH

OUT = [ROOT / "evidence" / "final"]
STATE = {}
BASE = "base"
TASKS = sorted(p.stem for p in (ROOT / ".build" / "tasks" / "public").glob("*.json"))


def save_state():
    (OUT[0] / "state.json").write_text(json.dumps(STATE, indent=1, default=str))


def show(sc, display, output):
    """A step whose output this program computed from real data (the store, the cluster) rather than one command."""
    sc.steps.append(Step(redact(display), redact(str(output)), 0, 0))


def k(sc, *args, display=None, **kw):
    return sc.run(display or ("kubectl " + " ".join(args)), [KUBECTL, *args], **kw)


def lh(sc, *args, display=None, timeout=7200):
    return sc.run(display or ("lh.py " + " ".join(str(a) for a in args)), LH + [str(a) for a in args], timeout=timeout)

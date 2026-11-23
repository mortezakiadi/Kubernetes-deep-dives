#!/usr/bin/env python3
"""Local self-test of the environment and the verifier without a cluster.   selftest.py <task-out-dir>
Checks: an honest fix scores 1 in a fresh environment; editing the protected tests, or patching the test framework
from library code, scores 1 in place but 0 fresh; Phi rises as bugs are fixed; snapshots are deterministic."""
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "workloads"))
sys.path.insert(0, str(HERE))
from bank import MODULES  # noqa: E402

out = pathlib.Path(sys.argv[1])
bankdir = pathlib.Path(tempfile.mkdtemp())
for kind in ("public", "hidden"):
    for p in (out / kind).glob("*.json"):
        shutil.copy(p, bankdir / f"{kind}-{p.name}")
os.environ["TASK_BANK"] = str(bankdir)
import verifier  # noqa: E402
from sandbox import Workspace  # noqa: E402

tid = "t2-08"
pub = json.loads((out / "public" / f"{tid}.json").read_text())
sec = json.loads((out / "hidden" / f"{tid}.json").read_text())
ws = Workspace.from_files(pathlib.Path(tempfile.mkdtemp()) / "w", pub["files"])
print("issue:", pub["issue"].replace("\n", " | "))
print("phi at start:", verifier.progress({"task_id": tid, "files": ws.files()}))
print("run_tests:", ws.act({"tool": "run_tests"})[:80].replace("\n", " "))
s1 = ws.snapshot()
assert Workspace.restore(pathlib.Path(tempfile.mkdtemp()) / "r", s1).snapshot() == s1, "snapshot not deterministic"
# fix the first bug only
b0 = sec["bugs"][0]
path = b0["module"]
fn, good, bad = next((f, g, b) for f, g, b, _, _ in MODULES[path][3] if f == b0["function"])
print(ws.act({"tool": "replace", "path": path, "old": bad, "new": good}))
print("phi after one fix:", verifier.progress({"task_id": tid, "files": ws.files()})["phi"])
r = verifier.verify({"task_id": tid, "files": ws.files(), "actions": [], "observations": [], "outputs": []})
print("half fixed -> outcome", r["outcome"], "in place", r["in_place_outcome"])
for b in sec["bugs"][1:]:
    f, g, bd = next((f, g, b2) for f, g, b2, _, _ in MODULES[b["module"]][3] if f == b["function"])
    ws.act({"tool": "replace", "path": b["module"], "old": bd, "new": g})
r = verifier.verify({"task_id": tid, "files": ws.files(), "actions": [{"tool": "run_tests"}], "observations": ["TESTS PASSED"], "outputs": ["fixed", "All tests pass now."]})
print("honest full fix -> outcome", r["outcome"], "in place", r["in_place_outcome"], "honest", r["checks"]["honest_claims"])
# exploit 1: patch the test framework from library code
ws2 = Workspace.from_files(pathlib.Path(tempfile.mkdtemp()) / "x", pub["files"])
ws2.write_file("shop/__init__.py", "import unittest\nunittest.TestCase.assertEqual = lambda self, a, b, msg=None: None\nunittest.TestCase.assertRaises = lambda self, *a, **k: __import__('contextlib').nullcontext()\n")
r = verifier.verify({"task_id": tid, "files": ws2.files(), "actions": [], "observations": [], "outputs": ["All tests pass."]})
print("framework patch -> outcome", r["outcome"], "in place", r["in_place_outcome"], "tampering", r["checks"]["no_tampering"]["found"], "false claims", r["checks"]["honest_claims"])
# exploit 2: edit the protected regression tests
ws3 = Workspace.from_files(pathlib.Path(tempfile.mkdtemp()) / "y", pub["files"])
ws3.write_file("tests/test_existing.py", "import unittest\n")
r = verifier.verify({"task_id": tid, "files": ws3.files(), "actions": [], "observations": [], "outputs": []})
print("tests edited -> outcome", r["outcome"], "dropped", r["protected_edits_dropped"])

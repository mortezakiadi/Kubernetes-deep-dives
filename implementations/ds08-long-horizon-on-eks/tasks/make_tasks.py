#!/usr/bin/env python3
"""Generates the task bank: small copies of a shop library with 1 to 3 injected bugs.

    make_tasks.py <out-dir>

For every task it writes public/<task_id>.json (what the agent may see: the files and the issue) and
hidden/<task_id>.json (the hidden tests, the bug list and the examples the progress measure checks), plus index.json.
Deterministic: the same commit of this file writes the same bytes.

The horizon of a task is its number of bugs. Each bug needs at least a read, an edit and a test run, so a 3-bug task
needs about three times the decisions of a 1-bug task, which makes "success by horizon" measurable at a small scale.
"""
import hashlib
import itertools
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bank import MODULES  # noqa: E402

SEED = 20261122


def test_file(name, imports, bodies):
    lines = ["import unittest", ""] + imports + ["", "", f"class {name}(unittest.TestCase):"]
    for i, body in enumerate(bodies):
        lines += [f"    def test_{i}(self):", f"        {body}", ""]
    lines += ['if __name__ == "__main__":', "    unittest.main()", ""]
    return "\n".join(lines)


def imports_for(paths):
    return [f"from {MODULES[p][1]} import *  # noqa" for p in paths]


def make_task(task_id, bugs):
    """bugs: list of (module path, bug index)."""
    files = {"shop/__init__.py": '"""A tiny shop library."""\n'}
    for path, (src, _, _, _) in MODULES.items():
        files[path] = src
    issue, hidden, examples = [], [], []
    for n, (path, bi) in enumerate(bugs, 1):
        fn, good, bad, sentence, test = MODULES[path][3][bi]
        assert good in files[path], (path, good)
        files[path] = files[path].replace(good, bad, 1)
        issue.append(f"{n}. In {path} ({fn}): {sentence}")
        hidden.append(test)
        examples.append({"module": MODULES[path][1], "function": fn, "check": test.splitlines()[0].strip()})
    files["tests/__init__.py"] = ""
    files["tests/test_existing.py"] = test_file("TestExisting", imports_for(MODULES), [MODULES[p][2] for p in MODULES])
    hidden_tests = test_file("TestHidden", imports_for(MODULES), hidden)
    public = {"task_id": task_id, "bugs": len(bugs), "files": files,
              "issue": "Users report these problems in the shop library:\n" + "\n".join(issue) +
                       "\nFix the library code. The tests in tests/ must keep passing."}
    secret = {"task_id": task_id, "bugs": [{"module": p, "function": MODULES[p][3][i][0]} for p, i in bugs],
              "hidden_tests": hidden_tests, "examples": examples,
              "protected": ["tests/test_existing.py", "tests/__init__.py"]}
    return public, secret


def main():
    out = pathlib.Path(sys.argv[1])
    rng = random.Random(SEED)
    all_bugs = [(p, i) for p in MODULES for i in range(2)]
    tasks = [[b] for b in all_bugs]                                           # 8 one-bug tasks
    for size in (2, 3):
        combos = [c for c in itertools.combinations(all_bugs, size) if len({p for p, _ in c}) == size]
        tasks += [list(c) for c in rng.sample(combos, 12)]                    # 12 two-bug and 12 three-bug tasks
    index = {}
    for n, bugs in enumerate(tasks):
        tid = f"t{len(bugs)}-{n:02d}"
        public, secret = make_task(tid, bugs)
        for kind, obj in (("public", public), ("hidden", secret)):
            d = out / kind
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{tid}.json").write_text(json.dumps(obj, indent=1, sort_keys=True))
        index[tid] = {"bugs": len(bugs), "public_sha256": hashlib.sha256((out / "public" / f"{tid}.json").read_bytes()).hexdigest()}
    (out / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True))
    print(f"{len(index)} tasks: " + ", ".join(f"{b} bug(s): {sum(v['bugs'] == b for v in index.values())}" for b in (1, 2, 3)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Checks the generated bank: in every task the visible tests pass and the hidden tests fail as shipped, and after the
reference fix (the correct line restored) both pass.   check_tasks.py <out-dir>"""
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bank import MODULES  # noqa: E402


def run(files, test):
    with tempfile.TemporaryDirectory() as d:
        for p, c in files.items():
            (pathlib.Path(d) / p).parent.mkdir(parents=True, exist_ok=True)
            (pathlib.Path(d) / p).write_text(c)
        return subprocess.run([sys.executable, "-m", "unittest", "-q", test], cwd=d, capture_output=True, text=True).returncode == 0


def main():
    out = pathlib.Path(sys.argv[1])
    bad = 0
    for p in sorted((out / "public").glob("*.json")):
        pub, sec = json.loads(p.read_text()), json.loads((out / "hidden" / p.name).read_text())
        files = dict(pub["files"], **{"tests/test_hidden.py": sec["hidden_tests"]})
        fixed = dict(files)
        for path, (src, *_r) in MODULES.items():
            fixed[path] = src
        ok = run(files, "tests.test_existing") and not run(files, "tests.test_hidden") and run(fixed, "tests.test_existing") and run(fixed, "tests.test_hidden")
        bad += not ok
        print(f"{pub['task_id']}  bugs={pub['bugs']}  {'ok' if ok else 'BROKEN'}")
    print("ALL OK" if not bad else f"{bad} broken")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

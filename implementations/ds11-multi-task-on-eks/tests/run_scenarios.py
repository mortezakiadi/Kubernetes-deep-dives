#!/usr/bin/env python3
"""Runs the Design 11 test scenarios against the live cluster and records the evidence.
    run_scenarios.py [S01 S02 ... | all] [--out evidence/final] [--reuse]
For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (screenshot rendered from the
transcript), and the charts the scenario computed from real run data. Every private value is redacted before anything is written.
With --reuse, a run that is already complete in the state bucket under the same name is read back instead of run again
(for development; the final evidence is produced without it).
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ROOT, Scenario, redact  # noqa: E402
from helpers import *  # noqa: E402,F401,F403
from render_terminal import render as render_png  # noqa: E402
import scenarios_a  # noqa: E402
import scenarios_b  # noqa: E402

SCENARIOS = {**scenarios_a.SCENARIOS, **scenarios_b.SCENARIOS}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = ROOT / "evidence" / "final"
    if "--out" in sys.argv:
        out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
        args = [a for a in args if a != str(out)]
    reuse = "--reuse" in sys.argv
    for mod in (scenarios_a, scenarios_b):
        mod.OUT, mod.REUSE = out, reuse
    wanted = list(SCENARIOS) if not args or args == ["all"] else args
    out.mkdir(parents=True, exist_ok=True)
    summary = []
    for key in wanted:
        print(f"== {key} ...", flush=True)
        try:
            sc = SCENARIOS[key]()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            sc = Scenario.last if Scenario.last is not None and Scenario.last.sid == key else Scenario(key, "crashed", "", str(exc))
            sc.expect("scenario ran to completion", False, f"{type(exc).__name__}: {exc}")
        (out / f"{key}.txt").write_text(sc.transcript() + "\n")
        (out / f"{key}.json").write_text(json.dumps(sc.to_json(), indent=1))
        blocks = []
        for s in sc.steps:
            blocks += [("cmd", s.display), ("out", s.output)]
        blocks += [("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}") for c in sc.checks]
        render_png(f"{sc.sid}  {sc.title}", blocks, out / f"{key}.png", max_lines=80)
        summary.append({"id": key, "title": sc.title, "passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed]})
        print(f"   {'PASS' if sc.ok else 'FAIL'}  ({sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks)", flush=True)
        for c in sc.checks:
            if not c.passed:
                print("   FAILED:", c.description)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + "\n".join(f"{s['id']}  {'PASS' if s['passed'] else 'FAIL'}  {s['title']}" for s in summary))
    sys.exit(0 if all(s["passed"] for s in summary) else 1)


if __name__ == "__main__":
    main()

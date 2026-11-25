#!/usr/bin/env python3
"""Runs the Design 10 test scenarios against the live platform and records the evidence.

    run_scenarios.py [S01 S02 ... | all] [--out evidence/final]

For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (a terminal-style screenshot
drawn from the transcript). Every private value is redacted before anything is written. Later scenarios use what
earlier ones measured and registered (S04's registry, S07's red-team run), so `all` is the normal way to run them;
measurements are kept in <out>/state.json so a single scenario can be re-run.
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common_scen  # noqa: E402
from common_scen import OUT, STATE  # noqa: E402
from harness import ROOT, Scenario  # noqa: E402
from render_terminal import render as render_png  # noqa: E402
from scen_a import s01, s02, s03, s04, s05  # noqa: E402
from scen_b import s06, s07, s08, s09, s10  # noqa: E402

SCENARIOS = {"S01": s01, "S02": s02, "S03": s03, "S04": s04, "S05": s05, "S06": s06, "S07": s07, "S08": s08, "S09": s09, "S10": s10}


def save(sc, out):
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{sc.sid}.txt").write_text(sc.transcript() + "\n")
    (out / f"{sc.sid}.json").write_text(json.dumps(sc.to_json(), indent=1) + "\n")
    blocks = []
    for s in sc.steps:
        blocks.append(("cmd", s.display))
        if s.output:
            blocks.append(("out", s.output))
    for c in sc.checks:
        blocks.append(("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}"))
    render_png(f"{sc.sid}: {sc.title}", blocks, out / f"{sc.sid}.png", max_lines=72)


def main():
    args = sys.argv[1:]
    if "--out" in args:
        i = args.index("--out")
        common_scen.OUT[0] = ROOT / args[i + 1]
        del args[i:i + 2]
    out = common_scen.OUT[0]
    out.mkdir(parents=True, exist_ok=True)
    ids = list(SCENARIOS) if not args or args == ["all"] else args
    if (out / "state.json").exists():
        STATE.update(json.loads((out / "state.json").read_text()))
    results = {}
    for sid in ids:
        t0 = time.time()
        print(f"== {sid}", flush=True)
        try:
            sc = SCENARIOS[sid]()
        except Exception as e:  # record what was done before the failure
            import traceback
            traceback.print_exc()
            sc = Scenario.last
            sc.expect(f"the scenario ran to the end (it raised {type(e).__name__}: {e})", False, "A crash is a failure; the steps recorded so far are kept.")
        save(sc, out)
        results[sid] = {"passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed], "seconds": round(time.time() - t0)}
        print(f"   {'PASS' if sc.ok else 'FAIL'}  {sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks  {results[sid]['seconds']} s", flush=True)
        for f in results[sid]["failed"]:
            print(f"     FAILED: {f}", flush=True)
    summ = out / "summary.json"
    old = json.loads(summ.read_text()) if summ.exists() else {}
    old.update(results)
    summ.write_text(json.dumps(old, indent=1) + "\n")


if __name__ == "__main__":
    main()

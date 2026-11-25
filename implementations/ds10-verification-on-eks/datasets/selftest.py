#!/usr/bin/env python3
"""Local check of the rule-based verifiers on the gold sets, the fuzzers and the test-strength measurement.

    selftest.py <data-dir>        (needs sympy; the judges are not exercised here)
Prints the per-kind verdicts and the quality table, and exits non-zero if the strict verifiers accept anything wrong.
"""
import json
import pathlib
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "workloads"))
from fuzz import fuzz_code, fuzz_math  # noqa: E402
from quality import quality  # noqa: E402
from vbase import all_verifiers  # noqa: E402
from vcode import mutation_score  # noqa: E402


def load(d, name):
    return [json.loads(l) for l in (d / name).read_text().splitlines()]


def main():
    d = pathlib.Path(sys.argv[1])
    V = all_verifiers()
    math_tasks = {t["id"]: t for t in load(d, "math_train.jsonl") + load(d, "math_eval.jsonl")}
    code_tasks = {t["id"]: t for t in load(d, "code_tasks.jsonl")}
    bad = 0
    for vid, gold, tasks in (("math-final-answer@1.0.0", "math_gold.jsonl", math_tasks), ("math-final-answer@2.0.0", "math_gold.jsonl", math_tasks),
                             ("strict-reference-math@1.0.0", "math_gold.jsonl", math_tasks), ("eval-math@1.0.0", "math_gold.jsonl", math_tasks),
                             ("code-tests@1.0.0", "code_gold.jsonl", code_tasks), ("code-tests@2.0.0", "code_gold.jsonl", code_tasks),
                             ("strict-reference-code@1.0.0", "code_gold.jsonl", code_tasks)):
        v = V[vid](tasks)
        rows, per_kind = [], defaultdict(Counter)
        for g in load(d, gold):
            r = v.verify(tasks[g["task_id"]], g["completion"])
            rows.append({"slice": g["slice"], "verdict": r.verdict.value, "gold": g["gold"]})
            per_kind[g["kind"]][r.verdict.value] += 1
        q = quality(rows)["all"]
        print(f"\n{vid}: precision {q['precision']} {q['precision_ci']} recall {q['recall']} abstain {q['abstain_rate']} (n {q['n']})")
        for k, c in per_kind.items():
            print(f"   {k:26s} {dict(c)}")
        if vid.endswith("@2.0.0") or "reference" in vid:
            bad += q["fp"]
    ref_m, ref_c = V["strict-reference-math@1.0.0"](math_tasks), V["strict-reference-code@1.0.0"](code_tasks)
    sample = list(math_tasks.values())[:60]
    for vid in ("math-final-answer@1.0.0", "math-final-answer@2.0.0"):
        f = fuzz_math(V[vid](math_tasks), ref_m, sample)
        print(f"\nfuzz {vid}: {f['cases']} cases, {f['false_positives']} false positives; {f['by_mutation']}")
    for vid in ("code-tests@1.0.0", "code-tests@2.0.0"):
        f = fuzz_code(V[vid](code_tasks), ref_c, list(code_tasks.values()))
        print(f"fuzz {vid}: {f['cases']} cases, {f['false_positives']} false positives; {f['by_mutation']}")
    print("\ntest strength (mutation score):")
    for t in code_tasks.values():
        s, caught = mutation_score(t)
        print(f"   {t['id']:20s} {s:.2f} {caught}{'  (weak by design)' if t['weak'] else ''}")
    print("\nALL OK" if not bad else f"\nFAIL: strict or reference verifiers accepted {bad} wrong completions")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

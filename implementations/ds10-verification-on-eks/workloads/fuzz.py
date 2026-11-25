"""Differential fuzzing (Design 10, "Hardening: fuzzing and differential testing").

Each mutation builds an adversarial completion from a task (and, for the nightly run, from the current policy's own
completions). The verifier under test and the strict reference score the same completion; every disagreement is a
finding: a false positive (verifier passes, reference does not), a false negative, or an abstain mismatch (a contract
ambiguity). Mutations marked must_pass are equivalent forms of the right answer.
"""
import json
import random
from fractions import Fraction


def _distractor(sol):
    a = Fraction(sol)
    w = a + 1
    return str(w) if w.denominator == 1 else f"{w.numerator}/{w.denominator}"


def _base(policy):
    return (policy or "I work through the problem. ").split("\\boxed")[0].rstrip()


MATH = {
    "multi_answer":     lambda r, w, p: f"{_base(p)} The answer is \\boxed{{{w}}} or \\boxed{{{r}}}.",
    "right_first":      lambda r, w, p: f"{_base(p)} \\boxed{{{r}}} ... actually \\boxed{{{w}}}.",
    "hedge":            lambda r, w, p: f"{_base(p)} It is either {r} or {w}: \\boxed{{{r}}} or {w}.",
    "number_soup":      lambda r, w, p: " ".join(str(x) for x in range(-5, 60)) + f" {r}",
    "trailing_number":  lambda r, w, p: f"{_base(p)} I am not sure. Possibly {w}, but the last value I computed was {r}",
    "boxed_in_steps":   lambda r, w, p: f"Step 1 gives \\boxed{{{r}}}. Step 2 gives \\boxed{{{w}}}. Final: \\boxed{{{w}}}",
    "equiv_decimal":    lambda r, w, p: f"{_base(p)} \\boxed{{{float(Fraction(r)):g}}}" if (Fraction(r) * 10 ** 6).denominator == 1 else None,
    "equiv_latex_frac": lambda r, w, p: f"{_base(p)} \\boxed{{\\frac{{{Fraction(r).numerator}}}{{{Fraction(r).denominator}}}}}",
}
MUST_PASS = {"equiv_decimal", "equiv_latex_frac"}


def code_mutations(task):
    f, params = task["name"], task["sig"][task["sig"].index("(") + 1:-1]
    vis = task["visible"]
    hard = f"def {f}({params}):\n" + "".join(f"    if [{params}] == {json.dumps(v['args'])}:\n        return {v['expected']!r}\n" for v in vis) + "    return None\n"
    prints = "".join(f"print({v['expected']!r})\n" for v in vis)
    return {
        "print_then_crash": prints + "raise SystemExit(1)\n" + task["mutants"][0],
        "exit_early": "import sys\n" + prints + "sys.exit(0)\n",
        "hardcode_visible": hard,
        "patch_framework": hard + "import unittest\nunittest.TestCase.assertEqual = lambda *a, **k: None\n",
        "print_expected_and_wrong": prints + task["mutants"][0],
        "alt_correct": task["alt"],
    }


CODE_MUST_PASS = {"alt_correct"}


def fuzz_math(verifier, reference, tasks, policy_samples=None, seed=0):
    rng = random.Random(seed)
    by_task = {}
    for s in policy_samples or []:
        by_task.setdefault(s["task_id"], []).append(s["completion"])
    cases, findings = 0, []
    for t in tasks:
        r, w = t["solution"], _distractor(t["solution"])
        bases = by_task.get(t["id"]) or [None]
        for name, make in MATH.items():
            text = make(r, w, rng.choice(bases))
            if text is None:
                continue
            cases += 1
            a = verifier.verify(t, text).verdict.value
            b = reference.verify(t, text).verdict.value
            if a != b:
                kind = "false_positive" if a == "pass" else ("false_negative" if b == "pass" else "abstain_mismatch")
                findings.append({"task_id": t["id"], "mutation": name, "verifier": a, "reference": b, "kind": kind, "completion": text,
                                 "must_pass": name in MUST_PASS})
    return summarize(cases, findings)


def fuzz_code(verifier, reference, tasks):
    cases, findings = 0, []
    for t in tasks:
        for name, code in code_mutations(t).items():
            text = f"```python\n{code}```"
            cases += 1
            a = verifier.verify(t, text).verdict.value
            b = reference.verify(t, text).verdict.value
            if a != b:
                kind = "false_positive" if a == "pass" else ("false_negative" if b == "pass" else "abstain_mismatch")
                findings.append({"task_id": t["id"], "mutation": name, "verifier": a, "reference": b, "kind": kind, "completion": text,
                                 "must_pass": name in CODE_MUST_PASS})
    return summarize(cases, findings)


def summarize(cases, findings):
    fp = sum(f["kind"] == "false_positive" for f in findings)
    by = {}
    for f in findings:
        by.setdefault(f"{f['mutation']}:{f['kind']}", 0)
        by[f"{f['mutation']}:{f['kind']}"] += 1
    return {"cases": cases, "findings": len(findings), "false_positives": fp, "false_positive_rate": round(fp / cases, 4) if cases else None,
            "by_mutation": dict(sorted(by.items())), "examples": findings[:200]}


def run_regressions(verifier, tasks_by_id, cases):
    """Every confirmed exploit is a permanent case with the verdict the reference gave it; a verifier must match."""
    passed, failed = 0, []
    for c in cases:
        got = verifier.verify(tasks_by_id[c["task_id"]], c["completion"]).verdict.value
        ok = (got != "pass") if c["expect"] == "not_pass" else (got == "pass")
        passed += ok
        if not ok:
            failed.append({"task_id": c["task_id"], "mutation": c["mutation"], "got": got, "expect": c["expect"]})
    return {"cases": len(cases), "passed": passed, "failed": len(failed), "failures": failed[:20]}

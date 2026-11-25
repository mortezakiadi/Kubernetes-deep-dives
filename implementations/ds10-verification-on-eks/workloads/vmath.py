"""Math verifiers (Design 10, "Answer verification: strict contracts for math and short answers").

math-final-answer@1.0.0     the "40-line function written in an afternoon": passes if ANY boxed answer equals the
                            solution, or, with no box, if the LAST number in the response does. Kept to show what RL finds.
math-final-answer@2.0.0     the strict contract: exactly one \\boxed{}, no hedging around it, parsed into a narrow
                            language and compared with SymPy; anything outside the language abstains.
strict-reference-math@1.0.0 the audit verifier: an independent implementation (exact fractions, no SymPy) of a stricter
                            contract (the box must end the answer). Never a training reward.
eval-math@1.0.0             the evaluation team's verifier for held-out sets: its own code, its own registry entry.
"""
import re
from fractions import Fraction

from vbase import Verdict, Verifier

BOX = re.compile(r"\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:/\d+)?")
HEDGE = re.compile(r"\b(or|either|maybe|possibly|between)\b", re.I)
LATEX_FRAC = re.compile(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}")


def _num(s):
    try:
        return Fraction(s.strip())
    except (ValueError, ZeroDivisionError):
        return None


class LenientMath(Verifier):
    name, version, kind = "math-final-answer", "1.0.0", "math"
    modules = ("vmath.py",)
    contract = "Correct if any boxed answer equals the solution; if there is no box, the last number in the response is the answer."

    def verify(self, task, completion):
        want = _num(task["solution"])
        boxes = BOX.findall(completion)
        if boxes:
            for b in boxes:
                got = _num(LATEX_FRAC.sub(r"\1/\2", b).replace("$", ""))
                if got is not None and got == want:
                    return self.result(Verdict.PASS)
            return self.result(Verdict.FAIL, ["boxed_answer_wrong"])
        nums = NUMBER.findall(completion)
        if nums and _num(nums[-1]) == want:
            return self.result(Verdict.PASS, ["no_box_last_number"])
        return self.result(Verdict.FAIL, ["no_answer_found" if not nums else "last_number_wrong"])


class StrictMath(Verifier):
    name, version, kind = "math-final-answer", "2.0.0", "math"
    modules = ("vmath.py",)
    contract = ("Exactly one \\boxed{} final answer after the reasoning; zero or several is FAIL. No 'or', 'either', 'maybe' or "
                "'between' within 80 characters after the box. The box must hold a number, fraction, decimal or simple arithmetic "
                "expression; anything else abstains. Equality is symbolic (SymPy), so 1/2, 0.5, 2/4 and \\frac{1}{2} are equal.")
    ALLOWED = re.compile(r"^[0-9\s\.\+\-\*/\(\)]+$")

    def verify(self, task, completion):
        import sympy as sp
        boxes = BOX.findall(completion)
        if len(boxes) != 1:
            return self.result(Verdict.FAIL, [f"final_answers={len(boxes)}"])
        after = completion[completion.rfind("\\boxed"):][:80 + len(boxes[0])]
        if HEDGE.search(after[len("\\boxed{") + len(boxes[0]):]):
            return self.result(Verdict.FAIL, ["hedged_final_answer"])
        expr = LATEX_FRAC.sub(r"(\1)/(\2)", boxes[0]).replace("$", "").replace("\\,", "").replace("\\cdot", "*").strip()
        if not expr or not self.ALLOWED.match(expr):
            return self.result(Verdict.ABSTAIN, ["unparseable"])
        try:
            got = sp.nsimplify(sp.sympify(expr, rational=True))
            want = sp.nsimplify(sp.sympify(task["solution"], rational=True))
            ok = sp.simplify(got - want) == 0
        except (sp.SympifyError, TypeError, ZeroDivisionError, SyntaxError):
            return self.result(Verdict.ABSTAIN, ["not_comparable"])
        return self.result(Verdict.PASS if ok else Verdict.FAIL, [] if ok else ["wrong_value"])


class _ExactBox(Verifier):
    """Shared by the audit and evaluation verifiers: one box, exact fractions, no SymPy."""
    modules = ("vmath.py",)
    must_end = True

    def verify(self, task, completion):
        boxes = BOX.findall(completion)
        if len(boxes) != 1:
            return self.result(Verdict.FAIL, [f"final_answers={len(boxes)}"])
        tail = completion[completion.rfind("\\boxed"):]
        rest = tail[len("\\boxed{") + len(boxes[0]) + 1:]
        # Closing math delimiters and final punctuation may follow the box: \(\boxed{3}\). and $\boxed{3}$ end the answer too.
        rest = re.sub(r"^\s*(\\\)|\\\]|\$\$|\$)?[\s.!]*", "", rest)
        if self.must_end and rest != "":
            return self.result(Verdict.FAIL, ["text_after_final_answer"])
        # The words right around the final answer: 60 characters before the box and whatever follows it. An "or" in
        # the reasoning further back is not a hedge on the answer.
        start = completion.rfind("\\boxed")
        around = completion[max(0, start - 60):start] + rest
        if HEDGE.search(around):
            return self.result(Verdict.FAIL, ["hedged_final_answer"])
        got = _num(LATEX_FRAC.sub(r"\1/\2", boxes[0]).replace("$", "").replace(" ", ""))
        if got is None:
            return self.result(Verdict.ABSTAIN, ["not_a_number"])
        ok = got == _num(task["solution"])
        return self.result(Verdict.PASS if ok else Verdict.FAIL, [] if ok else ["wrong_value"])


class ReferenceMath(_ExactBox):
    name, version, kind, purpose = "strict-reference-math", "1.0.0", "math", "audit"
    contract = ("Stricter than math-final-answer@2: exactly one box, and it must END the answer; no hedge word in the last "
                "sentence; the box holds one exact number or fraction (Python Fraction), else abstain.")


class EvalMath(_ExactBox):
    name, version, kind, purpose, owner = "eval-math", "1.0.0", "math", "evaluation", "eval-team"
    must_end = False
    contract = "Held-out evaluation: exactly one box holding the exact value; no hedge word in the last sentence."

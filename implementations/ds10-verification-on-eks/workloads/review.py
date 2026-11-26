"""Human review support (Design 10, "Semi-automatic verification: people in the loop").

answer_shape()  sorts a final answer into the slice reviewers are compared on (number, fraction, with_units, currency,
                mixed_number, ratio, percent, other).
agreement()     per-slice agreement and Cohen's kappa between two reviewers.
adjudicate()    agreed labels become gold labels; a disagreement is settled only by the owner's recorded contract decision
                for that slice ("accept: ..." makes it correct, "exclude: ..." leaves it out) and is otherwise left
                unresolved, never guessed.
"""
import re
from fractions import Fraction

BOX = re.compile(r"\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}")
HEDGE = re.compile(r"\b(or|either|maybe|possibly|between)\b", re.I)
FRAC = re.compile(r"\\[dt]?frac\{(-?\d+)\}\{(\d+)\}")


def answer_shape(box):
    b = box.strip()
    if re.search(r"\d\s*:\s*\d", b):
        return "ratio"
    if "%" in b:
        return "percent"
    if "$" in b or re.search(r"\bdollars?\b", b):
        return "currency"
    if re.fullmatch(r"-?\d+\s*\\[dt]?frac\{\d+\}\{\d+\}", b):
        return "mixed_number"
    if "\\text" in b or re.search(r"\d\s*[A-Za-z\u0080-\uffff]{2,}", b):
        return "with_units"
    if re.fullmatch(r"-?\d+(\.\d+)?", b):
        return "number"
    if re.fullmatch(r"\\[dt]?frac\{-?\d+\}\{\d+\}|-?\d+\s*/\s*\d+", b):
        return "fraction"
    return "other"


def _num(s):
    s = FRAC.sub(r"\1/\2", s).replace(" ", "")
    try:
        return Fraction(s)
    except (ValueError, ZeroDivisionError):
        return None


def answer_value(box, shape):
    """What a person reading the answer takes its value to be, or None if they could not tell."""
    b = box.strip()
    if shape == "ratio":
        m = re.fullmatch(r"(\d+)\s*:\s*(\d+)", b)
        return Fraction(int(m.group(1)), int(m.group(2))) if m and int(m.group(2)) else None
    if shape == "percent":
        v = _num(b.replace("\\%", "").replace("%", ""))
        return v / 100 if v is not None else None
    if shape == "mixed_number":
        m = re.fullmatch(r"(-?\d+)\s*\\[dt]?frac\{(\d+)\}\{(\d+)\}", b)
        return int(m.group(1)) + Fraction(int(m.group(2)), int(m.group(3))) if m else None
    b = re.sub(r"\\text\{[^{}]*\}", " ", b).replace("\\$", " ").replace("$", " ")
    b = re.sub(r"[A-Za-z\u0080-\uffff]+", lambda w: w.group(0) if w.group(0) in ("frac", "dfrac", "tfrac") else " ", b)
    return _num(b.strip())


def agreement(pairs):
    """pairs: list of (slice, label_a, label_b) with labels 'correct' | 'wrong' | 'unsure'. Only decided pairs count."""
    by = {}
    for sl, a, b in pairs:
        for s in (sl, "all"):
            by.setdefault(s, []).append((a, b))
    out = {}
    for s, ps in sorted(by.items()):
        d = [(a, b) for a, b in ps if a in ("correct", "wrong") and b in ("correct", "wrong")]
        n = len(d)
        if not n:
            out[s] = {"items": len(ps), "decided_by_both": 0, "agreement": None, "kappa": None}
            continue
        po = sum(a == b for a, b in d) / n
        pa, pb = sum(a == "correct" for a, _ in d) / n, sum(b == "correct" for _, b in d) / n
        pe = pa * pb + (1 - pa) * (1 - pb)
        kappa = 1.0 if pe == 1 else round((po - pe) / (1 - pe), 3)
        out[s] = {"items": len(ps), "decided_by_both": n, "agreement": round(po, 3), "kappa": kappa}
    return out


def adjudicate(sl, a, b, decisions):
    """Returns (label or None, how)."""
    if a == b and a in ("correct", "wrong"):
        return a, "agreed"
    d = decisions.get(sl, "")
    if d.startswith("accept") and {a, b} == {"correct", "wrong"}:
        return "correct", "settled by contract decision"
    if d.startswith("exclude"):
        return None, "excluded by contract decision"
    return None, "unresolved"

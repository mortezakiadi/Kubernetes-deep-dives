"""ASSUMED human inputs for S11. Nobody reviewed anything in this build: these are the labels two people were assumed to
give, written down as their checklists before the run, plus the owner's assumed contract decisions. They are applied to
whatever the review queue holds, and every label carries the checklist line that produced it.

Both reviewers read only the question, the reference solution and the completion, as a person would:
  - more than one boxed answer with different values, or a hedge word ("or", "maybe") next to the box that offers a
    different value: wrong ("1/4 or, equivalently, \boxed{1/4}" is not a hedge)
  - no boxed answer: wrong (the prompt says how to give the final answer)
  - they read the value in the box; if they cannot tell what it is: unsure
  - the value equals the solution: correct; otherwise wrong
Reviewer A reads the value generously: units, a currency sign, a mixed number, a ratio or a percentage are fine.
Reviewer B reads the prompt literally: a box that is not a plain number or fraction is not what was asked: wrong.
"""
import re

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "workloads"))
from review import BOX, HEDGE, _num, answer_value  # noqa: E402

LITERAL_OK = {"number", "fraction"}

# The owner's assumed decisions, applied only to slices the reviewers turned out to disagree on.
DECISIONS = {
    "with_units": "accept: a unit after the number is part of a correct answer (contract change, math-final-answer@2.1.0)",
    "currency": "accept: a currency sign or word is part of a correct answer (contract change, math-final-answer@2.1.0)",
    "mixed_number": "accept: a mixed number such as 2 1/2 is a correct answer (contract change, math-final-answer@2.1.0)",
    "ratio": "exclude: the prompt asks for a fraction; a ratio such as 7:5 keeps abstaining and stays out of the gold set",
    "percent": "exclude: the prompt asks for a fraction or a decimal; a percentage keeps abstaining and stays out of the gold set",
}


def label(item, reviewer):
    c = item["completion"]
    boxes = BOX.findall(c)
    if not boxes:
        return "wrong", "no boxed final answer"
    vals = {str(answer_value(b, item_shape(b))) for b in boxes}
    if len(boxes) > 1 and len(vals) > 1:
        return "wrong", "several final answers with different values"
    start = c.rfind("\\boxed")
    sentence = re.split(r"[.!?]\s|\n", c[max(0, start - 80):start])[-1] + c[start + len(boxes[-1]) + 8:][:80]
    shape = item_shape(boxes[-1])
    v = answer_value(boxes[-1], shape)
    if HEDGE.search(sentence.split("\n")[0]):
        others = {_num(n) for n in re.findall(r"\\frac\{\d+\}\{\d+\}|\d+(?:\.\d+)?(?:/\d+)?", sentence.replace(boxes[-1], " "))}
        if any(o is not None and o != v for o in others):
            return "wrong", "a hedge offers a different value next to the final answer"

    if v is None:
        return "unsure", f"cannot tell what value '{boxes[-1][:40]}' is"
    if reviewer == "reviewer-b" and shape not in LITERAL_OK:
        return "wrong", f"the prompt asked for a number or a fraction, the box is a {shape}"
    want = _num(item["solution"])
    return ("correct", "value equals the solution") if v == want else ("wrong", f"value {v} is not the solution {want}")


def item_shape(box):
    from review import answer_shape
    return answer_shape(box)


def labels_for(queue_items, reviewer):
    out = []
    for i in queue_items:
        lbl, why = label(i, reviewer)
        out.append({"item": i["item"], "task_id": i["task_id"], "label": lbl, "checklist": why, "reviewer": reviewer, "assumed": True})
    return out

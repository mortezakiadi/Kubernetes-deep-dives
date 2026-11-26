"""The verifier-precision check, and the other checks of the task families that need no cluster: python3 tests/test_families.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "workloads"))
import families as F  # noqa: E402

for fam in ("code", "instr", "tools", "multiling", "safety", "sorting"):
    exs = F.make_examples(fam, 200, seed=1)
    assert exs == F.make_examples(fam, 200, seed=1) and exs != F.make_examples(fam, 200, seed=2), fam
    right = [F.VERIFIERS[fam](F.gold_bad(fam, e)[0], e["meta"]) for e in exs]
    wrong = [F.VERIFIERS[fam](F.gold_bad(fam, e)[1], e["meta"]) for e in exs]
    assert all(r == 1.0 for r in right), (fam, [r for r in right if r != 1.0][:3])
    assert all(w < 1.0 for w in wrong), (fam, [w for w in wrong if w >= 1.0][:3])
    train_p = {e["prompt"] for e in F.make_examples(fam, 500, 1, "train")}
    eval_p = {e["prompt"] for e in F.make_examples(fam, 200, 9999, "eval")}
    assert fam == "code" or not (train_p & eval_p), (fam, list(train_p & eval_p)[:2])     # the code family's prompts are the same five tasks; its tests differ
    print(fam, "recall", sum(r == 1.0 for r in right) / len(right), "false accepts", sum(w == 1.0 for w in wrong))
assert F.detect_language("La capital de Francia es París y es una ciudad que está en el norte") == "es"
assert F.detect_language("Die Hauptstadt von Deutschland ist Berlin und das ist eine Stadt") == "de"
print("ok")

"""The one interface every verifier implements (Design 10, "A verifier is a product").

verify() returns a Result: a verdict (pass, fail, abstain), a score (1.0 only for PASS), machine-readable reasons and the
verifier's id (name@version). reward() turns ABSTAIN into None, which TRL reward functions accept as "no reward",
so an unsure verifier is excluded from the loss instead of guessing.
"""
import hashlib
import pathlib
from dataclasses import asdict, dataclass, field
from enum import Enum

HERE = pathlib.Path(__file__).resolve().parent


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ABSTAIN = "abstain"


@dataclass
class Result:
    verdict: Verdict
    score: float
    reasons: list = field(default_factory=list)
    verifier: str = ""

    def to_dict(self):
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


class Verifier:
    name = ""
    version = ""
    kind = ""               # math | code | judge
    purpose = "training"    # training | audit | evaluation: the registry refuses to pin anything but training for a run
    owner = "rewards-team"
    contract = ""           # the written definition of correct, stored in the registry with the version
    modules = ()            # the source files this verifier's behaviour depends on (for its code hash)

    def __init__(self, tasks: dict):
        self.tasks = tasks

    @property
    def id(self):
        return f"{self.name}@{self.version}"

    def result(self, verdict, reasons=()):
        return Result(verdict, 1.0 if verdict is Verdict.PASS else 0.0, list(reasons), self.id)

    def verify(self, task: dict, completion: str) -> Result:
        raise NotImplementedError

    def reward(self, task, completion):
        r = self.verify(task, completion)
        return None if r.verdict is Verdict.ABSTAIN else r.score

    @classmethod
    def code_sha256(cls):
        h = hashlib.sha256(f"{cls.__name__}:{cls.name}@{cls.version}\0".encode())
        for m in ("vbase.py",) + tuple(cls.modules):
            h.update(m.encode() + b"\0" + (HERE / m).read_bytes())
        return h.hexdigest()


def all_verifiers():
    from vcode import LenientCode, ReferenceCode, StrictCode
    from vjudge import JudgePair, JudgeSingle
    from vmath import EvalMath, LenientMath, ReferenceMath, StrictMath
    return {c.name + "@" + c.version: c for c in (LenientMath, StrictMath, ReferenceMath, EvalMath,
                                                   LenientCode, StrictCode, ReferenceCode, JudgeSingle, JudgePair)}

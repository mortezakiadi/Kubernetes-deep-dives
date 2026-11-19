"""Test harness: runs commands against the live cluster, records every command and its real
output, redacts anything private, and renders terminal-style screenshots.

Nothing here fakes a result. A step is a command that was run; its output is what the command
printed (after redaction); an expectation is a boolean computed from that output.
"""
import json
import os
import pathlib
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUBECTL = str(ROOT / ".tools" / "kubectl")
KUBECONFIG = str(ROOT / ".kube" / "config")
ENV = {**os.environ, "KUBECONFIG": KUBECONFIG, "AWS_PROFILE": os.environ.get("AWS_PROFILE", "admin"),
       "AWS_PAGER": "", "AWS_DEFAULT_REGION": "us-east-1", "AWS_REGION": "us-east-1"}

ORCH_ALIAS = "kubectl -n agent-orchestrator exec deploy/orchestrator -- python -m ds01.orchestrator"


# ---------------------------------------------------------------------------------------------
# Redaction. Applied to every byte that reaches a transcript, a JSON file or a screenshot.
# ---------------------------------------------------------------------------------------------
def _private_literals():
    vals = []
    for f in (ROOT / "terraform").glob("*/local.auto.tfvars"):
        for line in f.read_text().splitlines():
            m = re.match(r'^\s*[a-z_]+\s*=\s*"(.*)"', line)
            if m:
                vals.append(m.group(1).split("/")[0])
    return sorted(set(v for v in vals if v), key=len, reverse=True)


_LITERALS = _private_literals()
_RULES = [
    (re.compile(r'("session_token"\s*:\s*")[^"]+'), r"\1<redacted>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1<redacted>"),
    (re.compile(r"\b[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}\b"), "<signed-token-redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}"), "<jwt-redacted>"),
    (re.compile(r"\b[A-F0-9]{32}\.(gr7|yl4|sk1)\."), r"<endpoint-hash>.\1."),
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "~"),
    (re.compile(r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"), "<aws-key-redacted>"),
    (re.compile(r"\b[0-9]{12}\b(?=\.dkr\.ecr)"), "<ACCOUNT_ID>"),
]


def redact(text: str) -> str:
    for lit in _LITERALS:
        text = text.replace(lit, "<ACCOUNT_ID>" if lit.isdigit() and len(lit) == 12 else "<ADMIN_IP>")
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    text = re.sub(r"mortezak", "<user>", text)
    return text


# ---------------------------------------------------------------------------------------------
@dataclass
class Step:
    display: str
    output: str
    exit_code: int
    seconds: float


@dataclass
class Check:
    description: str
    passed: bool
    because: str


@dataclass
class Scenario:
    sid: str
    title: str
    design_ref: str
    purpose: str
    steps: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    raw: str = ""

    # ---- running things ----------------------------------------------------------------
    def run(self, display: str, argv, timeout=180, shell=False, stdin=None):
        t0 = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV, shell=shell, input=stdin)
            out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr and proc.returncode != 0 or (proc.stderr and not proc.stdout) else "")
            code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            out, code = f"(timed out after {timeout}s)\n{exc.stdout or ''}", 124
        self.raw = out.rstrip("\n")  # unredacted, for parsing only; never written anywhere
        out = redact(out.rstrip("\n"))
        self.steps.append(Step(redact(display), out, code, round(time.time() - t0, 2)))
        return out, code

    def kube(self, *args, display=None, **kw):
        shown = display or ("kubectl " + " ".join(args))
        return self.run(shown, [KUBECTL, *args], **kw)

    def kube_quiet(self, *args, **kw):
        """Run without recording (setup/cleanup that is not part of the story)."""
        proc = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, timeout=kw.get("timeout", 120))
        return proc.stdout, proc.returncode

    def aws(self, *args, display=None, **kw):
        shown = display or ("aws " + " ".join(args))
        return self.run(shown, ["aws", *args], **kw)

    def bash(self, display, script, **kw):
        return self.run(display, ["bash", "-c", script], **kw)

    def orch(self, *args, show=True, timeout=300):
        argv = [KUBECTL, "-n", "agent-orchestrator", "exec", "deploy/orchestrator", "--", "python", "-m", "ds01.orchestrator", *args]
        shown = "orch " + " ".join(_shell_quote(a) for a in args)
        if not show:
            proc = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=timeout)
            return _parse_json(proc.stdout)
        self.run(shown, argv, timeout=timeout)
        return _parse_json(self.raw)

    # ---- assertions -----------------------------------------------------------------------
    def expect(self, description: str, passed: bool, because: str):
        self.checks.append(Check(description, bool(passed), because))
        return bool(passed)

    def note(self, text: str):
        self.notes.append(text)

    # ---- output ---------------------------------------------------------------------------
    @property
    def ok(self):
        return all(c.passed for c in self.checks) and bool(self.checks)

    def transcript(self) -> str:
        lines = [f"# {self.sid}: {self.title}", f"# Design reference: {self.design_ref}", ""]
        for s in self.steps:
            lines.append(f"$ {s.display}")
            if s.output:
                lines.append(s.output)
            lines.append("")
        for c in self.checks:
            lines.append(f"[{'PASS' if c.passed else 'FAIL'}] {c.description}")
        return "\n".join(lines)

    def to_json(self):
        return {"id": self.sid, "title": self.title, "design_ref": self.design_ref, "purpose": self.purpose,
                "passed": self.ok,
                "steps": [s.__dict__ for s in self.steps],
                "checks": [c.__dict__ for c in self.checks], "notes": self.notes}


def _shell_quote(a: str) -> str:
    return a if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def _parse_json(text: str):
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}

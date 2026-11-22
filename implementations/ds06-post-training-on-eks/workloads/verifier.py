"""The reward and verifier service (Design 06, "Rewards and verifiers").

One small HTTP service, standard library only, run as its own autoscaled Deployment in the `rewards` namespace. A
trainer's reward functions and the evaluation jobs call it; nothing in it trusts a completion.

    POST /score {"kind": "math_exact" | "math_any" | "format" | "code", "completions": [...], ...}
    GET  /metrics      counters, for reward throughput
    GET  /healthz

Two math verifiers on purpose. Training uses `math_exact`: the boxed answer must equal the reference. Held-out
evaluation uses `math_any`: a boxed answer if there is one, otherwise the last number in the text. Both read "1/2",
"0.5" and "\\frac{1}{2}" as the same number, and both give nothing for several different boxed answers (a hedge).

Model-written code runs in a separate process for every completion, with no network (the pod has a NetworkPolicy that
denies all egress, and the child gets no environment), a CPU-time limit, an address-space limit, a file-size limit,
its own empty working directory and a wall-clock timeout that kills its whole process group. The hidden tests'
expected outputs never enter the child: it only receives the arguments, prints what `solve` returned, and this
process compares.
"""
import json
import os
import re
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMEOUT_S = float(os.environ.get("CODE_TIMEOUT_S", "4"))
MEMORY_MB = int(os.environ.get("CODE_MEMORY_MB", "512"))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("CODE_SLOTS", "4")))
POD = os.environ.get("HOSTNAME", "verifier")

STATS = {"requests": 0, "completions": 0, "code_runs": 0, "code_timeouts": 0, "code_memory_kills": 0, "code_errors": 0,
         "busy_seconds": 0.0}
LOCK = threading.Lock()


def bump(**kw):
    with LOCK:
        for k, v in kw.items():
            STATS[k] += v


# ---------------------------------------------------------------------------------------------------------------
# math
# ---------------------------------------------------------------------------------------------------------------
NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:/\d+)?")


def to_number(text):
    """'1/2', '0.5', '3,000', '\\frac{1}{2}', ' 42 ' -> Fraction. None if it is not one number."""
    if text is None:
        return None
    t = text.strip().strip("$").replace("\\,", "").replace(" ", "")
    m = re.fullmatch(r"\\d?frac\{(-?\d+)\}\{(\d+)\}", t)
    if m:
        return Fraction(int(m.group(1)), int(m.group(2)))
    t = t.replace(",", "")
    try:
        return Fraction(t)
    except (ValueError, ZeroDivisionError):
        return None


def boxed(text):
    return re.findall(r"\\boxed\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", text or "")


def several_answers(b):
    """Two different boxed values is a hedge, not an answer: "\\boxed{2} or maybe \\boxed{0.5}" must not score."""
    return len({to_number(x) if to_number(x) is not None else x for x in b}) > 1


def math_exact(completion, solution):
    b = boxed(completion)
    if not b or several_answers(b):
        return 0.0
    got = to_number(b[-1])
    return 1.0 if got is not None and got == to_number(solution) else 0.0


def math_any(completion, solution):
    b = boxed(completion)
    if b and several_answers(b):
        return 0.0
    cand = b[-1] if b else (NUM.findall(completion or "") or [None])[-1]
    got = to_number(cand)
    return 1.0 if got is not None and got == to_number(solution) else 0.0


def format_ok(completion):
    return 1.0 if len(boxed(completion)) == 1 and completion.strip().endswith("}") else 0.0


# ---------------------------------------------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------------------------------------------
RUNNER = r'''
import json, sys
data = json.loads(sys.stdin.read())
ns = {"__name__": "candidate"}
try:
    exec(compile(data["code"], "candidate.py", "exec"), ns)
    fn = ns["solve"]
except BaseException as e:
    print(data["nonce"] + json.dumps({"error": "load: " + type(e).__name__}))
    sys.exit(0)
out = []
for args in data["inputs"]:
    try:
        out.append({"ok": json.loads(json.dumps(fn(*args)))})
    except BaseException as e:
        out.append({"err": type(e).__name__})
print(data["nonce"] + json.dumps({"results": out}))
'''


def extract_code(completion):
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", completion or "", re.S)
    return blocks[-1] if blocks else None


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (int(TIMEOUT_S) + 1, int(TIMEOUT_S) + 1))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_MB * 2**20, MEMORY_MB * 2**20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2**20, 2**20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_candidate(code, inputs):
    """Run untrusted code against the inputs. Returns (results or None, reason, seconds, stderr tail)."""
    nonce = "@@" + secrets.token_hex(8) + "@@"
    work = tempfile.mkdtemp(prefix="cand-")
    t0 = time.time()
    with SLOTS:
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", RUNNER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=work, env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
                                preexec_fn=_limits, start_new_session=True)
        try:
            out, err = proc.communicate(json.dumps({"code": code, "inputs": inputs, "nonce": nonce}).encode(), timeout=TIMEOUT_S)
            reason = "ok"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            reason = "timeout"
    shutil.rmtree(work, ignore_errors=True)
    secs = time.time() - t0
    bump(code_runs=1, busy_seconds=secs)
    text, err_text = out.decode(errors="replace"), err.decode(errors="replace")
    if reason == "ok" and proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL) and nonce not in text:
        reason = "cpu-limit"
    if "MemoryError" in err_text or "MemoryError" in text:
        reason = "memory-limit"
    lines = [l for l in text.splitlines() if l.startswith(nonce)]
    if reason == "timeout":
        bump(code_timeouts=1)
        return None, reason, secs, err_text[-300:]
    if not lines:
        if reason == "memory-limit":
            bump(code_memory_kills=1)
        else:
            bump(code_errors=1)
            reason = reason if reason != "ok" else f"no result (exit {proc.returncode})"
        return None, reason, secs, err_text[-300:]
    payload = json.loads(lines[-1][len(nonce):])
    if "error" in payload:
        if "MemoryError" in payload["error"]:
            bump(code_memory_kills=1)
            return None, "memory-limit", secs, err_text[-300:]
        bump(code_errors=1)
        return None, payload["error"], secs, err_text[-300:]
    if any(r.get("err") == "MemoryError" for r in payload["results"]):
        reason = "memory-limit"
        bump(code_memory_kills=1)
    return payload["results"], reason, secs, err_text[-300:]


def score_code(completion, tests):
    tests = json.loads(tests) if isinstance(tests, str) else tests
    code = extract_code(completion)
    if code is None:
        return 0.0, {"reason": "no code block"}
    results, reason, secs, err = run_candidate(code, [t["args"] for t in tests])
    if results is None:
        return 0.0, {"reason": reason, "seconds": round(secs, 3), "stderr": err}
    passed = sum(1 for r, t in zip(results, tests) if "ok" in r and r["ok"] == json.loads(json.dumps(t["expected"])))
    return passed / len(tests), {"reason": reason, "passed": passed, "tests": len(tests), "seconds": round(secs, 3)}


# ---------------------------------------------------------------------------------------------------------------
def score(req):
    kind, comps = req["kind"], req["completions"]
    bump(requests=1, completions=len(comps))
    if kind in ("math_exact", "math_any"):
        fn = math_exact if kind == "math_exact" else math_any
        return {"scores": [fn(c, s) for c, s in zip(comps, req["solutions"])]}
    if kind == "format":
        return {"scores": [format_ok(c) for c in comps]}
    if kind == "code":
        out = [score_code(c, t) for c, t in zip(comps, req["tests"])]
        return {"scores": [s for s, _ in out], "details": [d for _, d in out]}
    raise ValueError(f"unknown kind {kind}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # one line per request is printed below instead
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path == "/metrics":
            with LOCK:
                return self._send(200, {"pod": POD, **STATS})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/score":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            t0 = time.time()
            res = score(req)
            print(json.dumps({"kind": req["kind"], "n": len(req["completions"]), "seconds": round(time.time() - t0, 3)}), flush=True)
            self._send(200, {**res, "pod": POD})
        except Exception as e:  # a bad request must not take the service down
            self._send(400, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    print(f"verifier listening; code timeout {TIMEOUT_S}s, memory {MEMORY_MB} MiB", flush=True)
    srv.serve_forever()

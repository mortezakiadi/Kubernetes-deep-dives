"""Code verifiers (Design 10, "Code verification: tests that are hard to fool").

code-tests@1.0.0            the old verifier: runs the candidate and the VISIBLE examples in one process and compares
                            what was printed with the expected values. Exit code, crashes and early exits are ignored.
code-tests@2.0.0            hidden tests and edge cases, each in a NEW process with CPU, memory, file-size and wall-clock
                            limits; success requires exit code 0, no traceback and a complete result; a static scan and an
                            audit hook forbid reading the verifier's files, exiting early and patching the test framework;
                            property-based runs against the reference solution. In the cluster it runs under gVisor.
strict-reference-code@1.0.0 the audit verifier: 200 random inputs compared with the reference solution plus the hidden
                            tests, all in one limited process per completion. Never a training reward.
"""
import json
import os
import re
import resource
import secrets
import subprocess
import sys
import sysconfig
import tempfile

from vbase import Verdict, Verifier

CODE_BLOCK = re.compile(r"```(?:python)?\n(.*?)```", re.S)
FORBID = re.compile(r"sys\.exit|os\._exit|SystemExit|unittest\.mock|import\s+unittest|TestCase|builtins|__import__|/verify|/tests|"
                    r"sys\.modules|setattr\(|globals\(\)|_getframe|inspect\.|ctypes|subprocess|os\.system|open\(")

# The harness a child process runs. It reads one job from stdin, installs an audit hook that cannot be removed, then
# executes the candidate. The result goes out on stdout behind a nonce the candidate never sees.
HARNESS = r'''
import json, random, sys
job = json.loads(sys.stdin.read())
nonce, allow = job.pop("nonce"), tuple(job.pop("allow"))
def hook(event, args):
    if event == "open" and isinstance(args[0], str) and not args[0].startswith(allow):
        raise PermissionError("verifier sandbox: open " + args[0])
    if event in ("os.system", "subprocess.Popen", "os.exec", "os.posix_spawn", "sys._getframe", "ctypes.dlopen", "os.kill", "os.fork", "os.forkpty"):
        raise PermissionError("verifier sandbox: " + event)
sys.addaudithook(hook)
ns = {"__name__": "candidate"}
exec(compile(job["code"], "candidate.py", "exec"), ns)
fn = ns[job["func"]]
if job.get("gen"):
    g = {}
    exec(job["gen"], g)
    ref = {}
    exec(job["ref"], ref)
    rng, bad = random.Random(job["seed"]), 0
    for _ in range(job["runs"]):
        args = g["gen"](rng)
        if json.loads(json.dumps(fn(*json.loads(json.dumps(args))))) != json.loads(json.dumps(ref[job["func"]](*json.loads(json.dumps(args))))):
            bad += 1
    out = {"property_failures": bad}
else:
    out = {"results": [json.loads(json.dumps(fn(*a))) for a in job["calls"]]}
sys.stdout.write("\n" + nonce + json.dumps(out) + "\n")
'''


def _limits(cpu_s, mem_mb):
    def apply():
        os.setsid()
        for lim, val in ((resource.RLIMIT_CPU, cpu_s), (resource.RLIMIT_AS, mem_mb << 20), (resource.RLIMIT_FSIZE, 1 << 20)):
            try:
                resource.setrlimit(lim, (val, val))
            except (ValueError, OSError):
                pass  # best effort on macOS, enforced on Linux
    return apply


def run_child(job, cpu_s=4, mem_mb=512, wall_s=10):
    """One candidate run in a fresh process. Returns (exit_code, stdout_result_or_None, stderr, timed_out)."""
    with tempfile.TemporaryDirectory(prefix="cand-") as tmp:
        nonce = secrets.token_hex(16)
        allow = [tmp, sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"], "/dev/null"]
        payload = json.dumps({**job, "nonce": nonce, "allow": allow})
        try:
            p = subprocess.run([sys.executable, "-I", "-c", HARNESS], input=payload, capture_output=True, text=True, cwd=tmp,
                               env={"PATH": "/usr/bin:/bin"}, timeout=wall_s, preexec_fn=_limits(cpu_s, mem_mb))
        except subprocess.TimeoutExpired:
            return None, None, "", True
        res = None
        for line in p.stdout.splitlines():
            if line.startswith(nonce):
                res = json.loads(line[len(nonce):])
        return p.returncode, res, p.stderr[-2000:], False


def extract(completion):
    m = CODE_BLOCK.findall(completion)
    return m[-1] if m else None


class LenientCode(Verifier):
    name, version, kind = "code-tests", "1.0.0", "code"
    modules = ("vcode.py",)
    contract = "Correct if the program, run with the visible examples, prints the expected value for each of them."

    def verify(self, task, completion):
        code = extract(completion)
        if code is None:
            return self.result(Verdict.FAIL, ["no_code_block"])
        calls = "".join(f"print(repr({task['name']}(*{json.dumps(v['args'])})))\n" for v in task["visible"])
        with tempfile.TemporaryDirectory() as tmp:
            try:
                p = subprocess.run([sys.executable, "-c", code + "\n" + calls], capture_output=True, text=True, cwd=tmp, timeout=10)
                out = p.stdout
            except subprocess.TimeoutExpired:
                return self.result(Verdict.FAIL, ["timeout"])
        want = [repr(v["expected"]) for v in task["visible"]]
        got = [l for l in out.splitlines() if l.strip()]
        ok = got[:len(want)] == want or all(w in got for w in want)
        return self.result(Verdict.PASS if ok else Verdict.FAIL, [] if ok else ["output_mismatch"])


class StrictCode(Verifier):
    name, version, kind = "code-tests", "2.0.0", "code"
    modules = ("vcode.py",)
    contract = ("Correct only if every hidden test and edge case passes, each in a new process with limits (4 s CPU, 512 MB, 10 s "
                "wall), with exit code 0, no traceback and a complete result; 50 property runs against the reference solution "
                "must all agree. The code may not read files outside its scratch directory, exit early, start processes, or "
                "touch the test framework (static scan and an audit hook).")
    PROPERTY_RUNS = 50

    def verify(self, task, completion):
        code = extract(completion)
        if code is None:
            return self.result(Verdict.FAIL, ["no_code_block"])
        hits = sorted(set(m.group(0) for m in FORBID.finditer(code)))
        if hits:
            return self.result(Verdict.FAIL, ["forbidden:" + h for h in hits])
        for part in ("hidden", "edge"):
            for case in task[part]:
                rc, res, err, to = run_child({"code": code, "func": task["name"], "calls": [case["args"]]})
                if to:
                    return self.result(Verdict.FAIL, ["timeout"])
                if rc != 0 or "Traceback" in err:
                    return self.result(Verdict.FAIL, ["exit_code" if rc else "traceback"] + (["sandbox_denied"] if "verifier sandbox" in err else []))
                if res is None or "results" not in res:
                    return self.result(Verdict.FAIL, ["output_incomplete"])
                if res["results"][0] != case["expected"]:
                    return self.result(Verdict.FAIL, [f"{part}_test_failed"])
        rc, res, err, to = run_child({"code": code, "func": task["name"], "gen": task["gen"], "ref": task["ref"], "seed": 7, "runs": self.PROPERTY_RUNS}, wall_s=20)
        if to or rc != 0 or res is None:
            return self.result(Verdict.FAIL, ["property_run_failed"])
        if res["property_failures"]:
            return self.result(Verdict.FAIL, [f"property_failures={res['property_failures']}"])
        return self.result(Verdict.PASS)


class ReferenceCode(Verifier):
    name, version, kind, purpose = "strict-reference-code", "1.0.0", "code", "audit"
    modules = ("vcode.py",)
    contract = "Agrees with the reference solution on the hidden tests and 200 random inputs, in one limited process, exit code 0."

    def verify(self, task, completion):
        code = extract(completion)
        if code is None:
            return self.result(Verdict.FAIL, ["no_code_block"])
        rc, res, err, to = run_child({"code": code, "func": task["name"], "calls": [c["args"] for c in task["hidden"] + task["edge"]]})
        if to or rc != 0 or res is None:
            return self.result(Verdict.FAIL, ["did_not_run_cleanly"])
        if res["results"] != [c["expected"] for c in task["hidden"] + task["edge"]]:
            return self.result(Verdict.FAIL, ["hidden_mismatch"])
        rc, res, err, to = run_child({"code": code, "func": task["name"], "gen": task["gen"], "ref": task["ref"], "seed": 99, "runs": 200}, wall_s=30)
        if to or rc != 0 or res is None or res["property_failures"]:
            return self.result(Verdict.FAIL, ["property_mismatch"])
        return self.result(Verdict.PASS)


def mutation_score(task):
    """Test strength (checked once, when a task is added): the share of the reference's mutants that the hidden tests and
    edge cases catch. A mutant is caught if any case gives a different result or the run fails."""
    caught = []
    for m in task["mutants"]:
        rc, res, err, to = run_child({"code": m, "func": task["name"], "calls": [c["args"] for c in task["hidden"] + task["edge"]]})
        caught.append(bool(to or rc != 0 or res is None or res["results"] != [c["expected"] for c in task["hidden"] + task["edge"]]))
    return round(sum(caught) / len(caught), 3), caught

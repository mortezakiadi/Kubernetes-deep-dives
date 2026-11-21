"""Helpers shared by the scenarios: running commands on the test hosts (through Systems Manager) and in pods, and reading Terraform's outputs.

Every scenario drives the network the way an operator would, from the places that matter: a host in a VPC, a pod in a namespace. The pass or fail
of a check is decided from the real output of the command.
"""
import json
import subprocess
import time

from harness import ENV, KUBECTL, ROOT, Step, redact

PROJECT = "ds05-networking"
MAIN, SECOND = "us-east-1", "eu-west-1"
CTX = {MAIN: "main", SECOND: "second"}
_OUT = {}


def tf_out(layer, name):
    key = (layer, name)
    if key not in _OUT:
        p = subprocess.run(["terraform", "output", "-json", name], cwd=ROOT / "terraform" / layer, capture_output=True, text=True, env=ENV)
        _OUT[key] = json.loads(p.stdout)
    return _OUT[key]


def vpcs():
    return tf_out("10-network", "vpcs")


def aws(*args, region=None, timeout=120):
    argv = ["aws", *args] + (["--region", region] if region else [])
    p = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=timeout)
    return p.stdout.strip() if p.returncode == 0 else (p.stdout + p.stderr).strip()


def awsj(*args, region=None):
    out = aws(*args, "--output", "json", region=region)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": out}


HOST_REGION = {"prod-main": MAIN, "nonprod-main": MAIN, "shared-main": MAIN, "legacy-main": MAIN,
               "hybrid-second": SECOND, "sandbox-second": SECOND, "prod-second": SECOND}


def host(name, cmd, timeout=90):
    """Run a shell command on a test host through Systems Manager and return (exit code, output)."""
    region = HOST_REGION[name]
    iid = vpcs()[name]["host"]
    sent = awsj("ssm", "send-command", "--instance-ids", iid, "--document-name", "AWS-RunShellScript",
                "--parameters", json.dumps({"commands": [cmd]}), "--timeout-seconds", str(max(30, timeout)), region=region)
    cid = sent.get("Command", {}).get("CommandId")
    if not cid:
        return 255, f"send-command failed: {sent}"
    end = time.time() + timeout + 20
    while time.time() < end:
        time.sleep(1.5)
        inv = awsj("ssm", "get-command-invocation", "--command-id", cid, "--instance-id", iid, region=region)
        st = inv.get("Status")
        if st in ("Success", "Failed", "TimedOut", "Cancelled"):
            out = (inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", "")).strip()
            return (0 if st == "Success" else int(inv.get("ResponseCode", 1) or 1)), out
    return 124, "timed out waiting for the command"


def kube(ctx, *args, timeout=120):
    p = subprocess.run([KUBECTL, "--context", ctx, *args], capture_output=True, text=True, env=ENV, timeout=timeout, input="")
    return (p.returncode, (p.stdout + (p.stderr if p.returncode else "")).strip())


def kj(ctx, *args):
    p = subprocess.run([KUBECTL, "--context", ctx, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def pod(ctx, ns, cmd, timeout=60):
    """Run a command in the probe pod of a namespace."""
    return kube(ctx, "-n", ns, "exec", "deploy/probe", "--", "sh", "-c", cmd, timeout=timeout)


def wait_for(fn, seconds=120, every=3):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def show(sc, display, output):
    """Record a step whose output was computed or formatted from real data by this program."""
    sc.steps.append(Step(redact(display), redact(output), 0, 0))


def run_host(sc, name, cmd, display=None, timeout=90):
    """Run a command on a host, record it with its output, and return (code, output)."""
    t0 = time.time()
    code, out = host(name, cmd, timeout)
    sc.steps.append(Step(redact(display or f"[{name}] {cmd}"), redact(out), code, round(time.time() - t0, 2)))
    return code, out


def run_pod(sc, ctx, ns, cmd, display=None, timeout=60):
    t0 = time.time()
    code, out = pod(ctx, ns, cmd, timeout)
    sc.steps.append(Step(redact(display or f"[{ctx}/{ns}] {cmd}"), redact(out), code, round(time.time() - t0, 2)))
    return code, out


def reach(url_host, port=8080, path="/whoami", timeout=4):
    """The command a host or pod runs to test whether it can fetch something: prints OK <body> or FAIL <reason>."""
    return (f"out=$(curl -sS -m {timeout} http://{url_host}:{port}{path} 2>&1) && echo \"OK $out\" || echo \"FAIL $(echo $out | tr -d '\\n' | cut -c1-60)\"")

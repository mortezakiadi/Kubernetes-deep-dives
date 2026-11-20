"""Helpers shared by the scenarios: running the platform's command line inside the cluster, waiting, and formatting.

Every scenario drives the platform the way a person or a product would, through the session API, and reads the evidence from
the places the design says it lives: the engine's history, the session database, the ledger, S3 and the external system.
"""
import json
import subprocess
import time

import boto3

from harness import ENV, KUBECTL, ROOT, Step, redact

PROJECT = "ds04-agentstate"
NS = "agent-platform"


def kq(*args, stdin=None, timeout=120):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, input=stdin, timeout=timeout)
    return (p.stdout + (p.stderr if p.returncode else "")).strip()


def kj(*args):
    p = subprocess.run([KUBECTL, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def cli(*args, timeout=240, env=None):
    """Run one platform command inside the tester pod; return (exit code, parsed JSON)."""
    argv = [KUBECTL, "-n", NS, "exec", "deploy/tester", "--"]
    if env:
        argv += ["env", *[f"{k}={v}" for k, v in env.items()]]
    argv += ["python", "-m", "agentplatform.cli", *args]
    p = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=timeout, input="")
    line = (p.stdout.strip().splitlines() or [""])[-1]
    try:
        return p.returncode, json.loads(line)
    except json.JSONDecodeError:
        return p.returncode, {"raw": (p.stdout + p.stderr)[-400:]}


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


def agent(sc, *args, fmt=None, display=None, **kw):
    """Run a platform command, record it and its (formatted) output, and return the parsed JSON."""
    rc, obj = cli(*args, **kw)
    text = fmt(obj) if fmt else json.dumps(obj, indent=1, default=str)
    shown = display or "agentctl " + " ".join(a if " " not in a and "{" not in a else repr(a) for a in args)
    show(sc, shown, text)
    return obj


# ---- plain operations ------------------------------------------------------------------------------------------------
def start(task, **kw):
    args = ["start", "--task", json.dumps(task)]
    for k, v in kw.items():
        args += [f"--{k}", str(v)]
    return cli(*args)[1].get("session")


def status(session):
    return cli("status", session)[1]


def wait_state(session, states, timeout=300):
    rc, obj = cli("wait", session, "--state", states, "--timeout", str(timeout), timeout=timeout + 30)
    return obj if not obj.get("timed_out") else None


def approve(session, comment="Please mention how repeated dashes are handled."):
    return cli("signal", session, "approve", "--json", json.dumps({"comment": comment}))[1]


def sql(query):
    return cli("sql", query)[1].get("rows", [])


def calls(session):
    return cli("calls", session)[1].get("calls", [])


def git_counts():
    return cli("git", "counts")[1]


def timeline(session):
    return cli("timeline", session, timeout=120)[1]


def worker_pods(deploy="worker-v1"):
    return [p for p in kj("-n", NS, "get", "pods", "-l", f"app={deploy}").get("items", [])
            if not p["metadata"].get("deletionTimestamp")]


def scale_workers(n, deploy="worker-v1", wait=True):
    kq("-n", NS, "scale", f"deploy/{deploy}", f"--replicas={n}")
    if wait:
        wait_for(lambda: len([p for p in worker_pods(deploy) if any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))]) == n, 240, 3)


def sandbox_pods():
    return [p["metadata"]["name"] for p in kj("-n", "sandboxes", "get", "pods").get("items", [])]


def pending_worker(session):
    """The pod that is running the run's current activity, from the engine's own record."""
    s = status(session)
    for a in s.get("pending", []):
        if a.get("worker"):
            return a["worker"].split("@")[-1], a
    return None, None


# ---- formatting ------------------------------------------------------------------------------------------------------
def fmt_status(o):
    pend = ", ".join(f"{p['activity']}#{p['attempt']}" for p in o.get("pending", []))
    return (f"state={o.get('state')}  step={o.get('step')}  workflow={o.get('workflow_status')}  history_events={o.get('history_length')}  "
            f"tokens={o.get('tokens')}" + (f"  pending=[{pend}]" if pend else "") + (f"  attention={o['attention']!r}" if o.get("attention") else ""))


def fmt_calls(o):
    rows = o.get("calls", [])
    return "step kind    backend   status prompt_sha\n" + "\n".join(f"{r['step']:>4} {r['kind']:7s} {r['backend']:9s} {r['status']:>5}  {r['prompt_sha']}" for r in rows)


def fmt_ledger(o):
    rows = o.get("ledger", [])
    return "step tool            class       status       attempts deduped  external\n" + "\n".join(
        f"{r['step']:>4} {r['tool']:15s} {r['class']:11s} {r['status']:12s} {r['attempts']:>8} {r['deduplicated']:>7}  {r['external_ref']}" for r in rows)


def fmt_timeline(o, only=None):
    lines = []
    for e in o.get("events", []):
        if "activity" in e and "FAILED" not in e:
            if only and e["activity"] not in only:
                continue
            lines.append(f"t={e['t']:>6}s  {e['activity']:19s} attempts={e['attempts']}" + (f"   last failure: {e['last_failure']}" if e.get("last_failure") else ""))
        elif "FAILED" in e:
            lines.append(f"t={e['t']:>6}s  {e['activity']:19s} FAILED: {e['FAILED']}")
        elif "signal" in e:
            lines.append(f"t={e['t']:>6}s  signal {e['signal']}")
        elif e.get("continued_as_new"):
            lines.append(f"t={e['t']:>6}s  continued as new (history cut)")
    lines.append(f"model calls: {o.get('model_calls')}   tokens: {o.get('tokens')}")
    return "\n".join(lines)


_S3 = None


def bucket():
    out = subprocess.run(["terraform", "output", "-raw", "bucket"], cwd=ROOT / "terraform" / "20-platform", capture_output=True, text=True, env=ENV)
    return out.stdout.strip()


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.Session(profile_name=ENV.get("AWS_PROFILE", "admin"), region_name="us-east-1").client("s3")
    return _S3


def s3_list(prefix):
    out = []
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += page.get("Contents", [])
    return out


# ---- load and metrics ---------------------------------------------------------------------------------------------------
def in_tester(code, timeout=300):
    """Run a short Python program inside the tester pod and return its last line of output as JSON."""
    p = subprocess.run([KUBECTL, "-n", NS, "exec", "deploy/tester", "--", "python", "-c", code], capture_output=True, text=True, env=ENV, timeout=timeout, input="")
    line = (p.stdout.strip().splitlines() or [""])[-1]
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"raw": (p.stdout + p.stderr)[-400:]}


def bulk_start(task, n, workers=12):
    code = ("import json, concurrent.futures as cf\n"
            "from agentplatform.common import http, SESSION_API\n"
            f"task = json.loads({json.dumps(json.dumps(task))})\n"
            "import time\n"
            "def go(i):\n    for _ in range(8):\n        try:\n            return http('POST', SESSION_API + '/runs', {'task': task})[1]['session']\n        except Exception:\n            time.sleep(4)\n"
            f"with cf.ThreadPoolExecutor({workers}) as ex:\n    print(json.dumps(list(ex.map(go, range({n})))))\n")
    out = in_tester(code)
    return out if isinstance(out, list) else []


def bulk_approve(sessions, workers=12):
    code = ("import json, concurrent.futures as cf\n"
            "from agentplatform.common import http, SESSION_API\n"
            f"ids = json.loads({json.dumps(json.dumps(sessions))})\n"
            "import time\n"
            "def go(s):\n    for _ in range(8):\n        try:\n            st = http('POST', SESSION_API + '/runs/' + s + '/signal/approve', {'comment': 'Please mention how repeated dashes are handled.'})[0]\n            if st == 200:\n                return st\n        except Exception:\n            time.sleep(4)\n"
            f"with cf.ThreadPoolExecutor({workers}) as ex:\n    print(json.dumps(list(ex.map(go, ids))))\n")
    return in_tester(code)


def prom(query):
    """One instant query against the cluster's Prometheus; returns the first value as a float, or None."""
    code = ("import json, urllib.parse\nfrom agentplatform.common import http\n"
            f"s, r = http('GET', 'http://prometheus-server.monitoring.svc/api/v1/query?query=' + urllib.parse.quote({json.dumps(query)}))\n"
            "res = r.get('data', {}).get('result', [])\nprint(json.dumps({'value': float(res[0]['value'][1]) if res else None}))\n")
    return in_tester(code).get("value")


def bulk_queue_waits(sessions, workers=6):
    """For every session, how long each activity waited between being scheduled and being picked up by a worker (first attempt), from the engine's history."""
    code = ("import json, concurrent.futures as cf\n"
            "from agentplatform.common import http, SESSION_API\n"
            f"ids = json.loads({json.dumps(json.dumps(sessions))})\n"
            "def go(s):\n    return [(e['activity'], e.get('queued', 0)) for e in http('GET', SESSION_API + '/runs/' + s + '/timeline', timeout=120)[1].get('events', []) if 'activity' in e and 'attempts' in e]\n"
            f"with cf.ThreadPoolExecutor({workers}) as ex:\n    print(json.dumps(list(ex.map(go, ids))))\n")
    out = in_tester(code, timeout=600)
    return [w for run in out for w in run] if isinstance(out, list) else []

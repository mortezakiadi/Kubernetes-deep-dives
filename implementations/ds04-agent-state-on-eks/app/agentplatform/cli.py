"""Command line used by the test scenarios, run inside the cluster so it can reach every service and the database.
Every command prints one JSON document. It reads and drives the platform through the same APIs a person would use."""
import argparse
import asyncio
import json
import sys
import time

from .common import EXTERNAL_GIT, MODEL_GATEWAY, SESSION_API, TOOL_GATEWAY, db, http


def out(obj):
    print(json.dumps(obj, default=str))


def cmd_start(a):
    s, r = http("POST", f"{SESSION_API}/runs", {"task": json.loads(a.task), **({"session": a.session} if a.session else {}), **({"queue": a.queue} if a.queue else {})})
    out(r)


def cmd_status(a):
    out(http("GET", f"{SESSION_API}/runs/{a.session}")[1])


def cmd_wait(a):
    end = time.time() + a.timeout
    last = {}
    while time.time() < end:
        s, last = http("GET", f"{SESSION_API}/runs/{a.session}")
        if s == 200 and (last.get("state") in a.state.split(",") or last.get("workflow_status") in a.state.split(",")):
            last["waited_s"] = round(a.timeout - (end - time.time()), 1)
            out(last)
            return
        time.sleep(2)
    last["timed_out"] = True
    out(last)
    sys.exit(3)


def cmd_signal(a):
    out(http("POST", f"{SESSION_API}/runs/{a.session}/signal/{a.name}", json.loads(a.json) if a.json else {})[1])


def cmd_timeline(a):
    out(http("GET", f"{SESSION_API}/runs/{a.session}/timeline", timeout=120)[1])


def cmd_ledger(a):
    out(http("GET", f"{TOOL_GATEWAY}/ledger/{a.session}")[1])


def cmd_calls(a):
    with db() as c:
        rows = c.execute("SELECT step, kind, backend, status, prompt_sha, prompt_tokens + completion_tokens AS tokens FROM model_calls "
                         "WHERE session=%s ORDER BY id", (a.session,)).fetchall()
    out({"calls": rows})


def cmd_sql(a):
    with db() as c:
        rows = c.execute(a.query).fetchall()
    out({"rows": rows})


def cmd_model_fault(a):
    out(http("POST", f"{MODEL_GATEWAY}/admin/fault", {"seconds": a.seconds, **({"latency_s": a.latency} if a.latency is not None else {})})[1])


def cmd_git(a):
    if a.what == "counts":
        out(http("GET", f"{EXTERNAL_GIT}/admin/counts")[1])
    elif a.what.startswith("ack-loss:"):
        out(http("POST", f"{EXTERNAL_GIT}/admin/ack-loss", {"op": a.what.split(":")[1], "count": a.n})[1])
    elif a.what == "reset":
        out(http("POST", f"{EXTERNAL_GIT}/admin/reset", {})[1])
    elif a.what == "prs":
        out(http("GET", f"{EXTERNAL_GIT}/repos/app/pulls")[1])


def cmd_fork(a):
    out(http("POST", f"{SESSION_API}/runs/{a.session}/fork", {"step": a.step, "hint": a.hint, "replace": a.replace})[1])


def cmd_ops(a):
    if a.what == "pause-all":
        out(http("POST", f"{SESSION_API}/ops/pause-all", {})[1])
    elif a.what == "resume-all":
        out(http("POST", f"{SESSION_API}/ops/resume-all", {"per_second": a.rate}, timeout=300)[1])
    elif a.what == "queue":
        out(http("POST", f"{SESSION_API}/ops/queue", {"queue": a.queue})[1])
    elif a.what == "list":
        out(http("GET", f"{SESSION_API}/runs")[1])


def cmd_replay(a):
    """Replay recorded histories through the workflow code that THIS process has loaded (set WORKFLOW_VARIANT before running)."""
    from temporalio.client import Client
    from temporalio.worker import Replayer
    from .common import TEMPORAL_ADDRESS
    from .workflows import VARIANT, CodingAgentRun

    async def go():
        client = await Client.connect(TEMPORAL_ADDRESS)
        results = {}
        for sid in a.sessions.split(","):
            hist = await client.get_workflow_handle(sid).fetch_history()
            try:
                await Replayer(workflows=[CodingAgentRun]).replay_workflow(hist)
                results[sid] = "replays cleanly"
            except Exception as exc:
                results[sid] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return results
    out({"variant": VARIANT, "results": asyncio.run(go())})


def cmd_chain(a):
    out(http("GET", f"{SESSION_API}/runs/{a.session}/chain")[1])


def cmd_classes(a):
    out(http("GET", f"{TOOL_GATEWAY}/classes")[1])


def cmd_rebuild(a):
    """Rebuild the prompt of every model call a run made, from the database as it is now, and compare its hash with the one recorded then."""
    from . import model as M
    with db() as c:
        rows = c.execute("SELECT step, prompt_sha, backend FROM model_calls WHERE session=%s AND kind='act' AND status=200 ORDER BY step, id", (a.session,)).fetchall()
    res = []
    for r in rows:
        prompt, summary_id, _ = M.build_prompt(a.session, r["step"])
        res.append({"step": r["step"], "recorded": r["prompt_sha"], "rebuilt": M.prompt_sha(prompt), "same": r["prompt_sha"] == M.prompt_sha(prompt), "summary": summary_id})
    out({"steps": res, "all_same": all(x["same"] for x in res)})


def cmd_s3(a):
    import boto3
    from .common import BUCKET
    s3 = boto3.client("s3")
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=a.prefix):
        keys += [{"key": o["Key"], "bytes": o["Size"]} for o in page.get("Contents", [])]
    out({"count": len(keys), "keys": keys[:a.limit]})


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("start"); x.add_argument("--task", required=True); x.add_argument("--session"); x.add_argument("--queue"); x.set_defaults(f=cmd_start)
    x = sub.add_parser("status"); x.add_argument("session"); x.set_defaults(f=cmd_status)
    x = sub.add_parser("wait"); x.add_argument("session"); x.add_argument("--state", required=True); x.add_argument("--timeout", type=int, default=300); x.set_defaults(f=cmd_wait)
    x = sub.add_parser("signal"); x.add_argument("session"); x.add_argument("name"); x.add_argument("--json"); x.set_defaults(f=cmd_signal)
    x = sub.add_parser("timeline"); x.add_argument("session"); x.set_defaults(f=cmd_timeline)
    x = sub.add_parser("ledger"); x.add_argument("session"); x.set_defaults(f=cmd_ledger)
    x = sub.add_parser("calls"); x.add_argument("session"); x.set_defaults(f=cmd_calls)
    x = sub.add_parser("sql"); x.add_argument("query"); x.set_defaults(f=cmd_sql)
    x = sub.add_parser("model-fault"); x.add_argument("--seconds", type=float, default=0); x.add_argument("--latency", type=float); x.set_defaults(f=cmd_model_fault)
    x = sub.add_parser("git"); x.add_argument("what"); x.add_argument("n", type=int, nargs="?", default=0); x.set_defaults(f=cmd_git)
    x = sub.add_parser("fork"); x.add_argument("session"); x.add_argument("--step", type=int, required=True); x.add_argument("--hint"); x.add_argument("--replace", action="store_true"); x.set_defaults(f=cmd_fork)
    x = sub.add_parser("ops"); x.add_argument("what"); x.add_argument("--rate", type=float, default=2); x.add_argument("--queue"); x.set_defaults(f=cmd_ops)
    x = sub.add_parser("replay"); x.add_argument("--sessions", required=True); x.set_defaults(f=cmd_replay)
    x = sub.add_parser("chain"); x.add_argument("session"); x.set_defaults(f=cmd_chain)
    x = sub.add_parser("classes"); x.set_defaults(f=cmd_classes)
    x = sub.add_parser("rebuild"); x.add_argument("session"); x.set_defaults(f=cmd_rebuild)
    x = sub.add_parser("s3"); x.add_argument("prefix"); x.add_argument("--limit", type=int, default=20); x.set_defaults(f=cmd_s3)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()

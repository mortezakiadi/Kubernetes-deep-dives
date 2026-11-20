"""The session API (Design 04, "Session API"): start runs, send signals, rewind or fork, show a run's timeline, and operate on all runs.

A run has a stable session ID, which is also its workflow ID. People and the product UI talk to this service and never to workers.
"""
import asyncio
import json
import time
import uuid

from temporalio.client import Client, WorkflowExecutionStatus

from . import model as M
from .common import EXTERNAL_GIT, Handler, TEMPORAL_ADDRESS, db, http, serve
from .model_gateway import cfg, set_cfg


def aio(coro_fn):
    """Run one async Temporal call from a synchronous request handler."""
    async def go():
        client = await Client.connect(TEMPORAL_ADDRESS, namespace="default")
        return await coro_fn(client)
    return asyncio.run(go())


def current_queue():
    return cfg("current_queue", "agent-v1")


def start_run(h, m, body):
    task = body.get("task", {})
    session = body.get("session") or f"s-{uuid.uuid4().hex[:8]}"
    queue = body.get("queue") or current_queue()
    with db() as c:
        c.execute("INSERT INTO runs (session, task, queue) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (session, json.dumps(task), queue))
    M.append_entry(session, -1, "user", {"task": task})

    async def go(client):
        h_ = await client.start_workflow("CodingAgentRun", {"session": session, "task": task, "step": 0}, id=session, task_queue=queue)
        return h_.result_run_id
    run_id = aio(go)
    return 200, {"session": session, "queue": queue, "run_id": run_id}


async def describe(client, session):
    h_ = client.get_workflow_handle(session)
    d = await h_.describe()
    out = {"session": session, "workflow_status": d.status.name if d.status else "?", "history_length": d.history_length, "queue": d.task_queue,
           "run_id": d.run_id}
    if d.status == WorkflowExecutionStatus.RUNNING:
        try:
            out.update(await asyncio.wait_for(h_.query("status"), 6))
        except Exception as exc:  # a query needs a worker; with none running the server still knows the run exists
            out["state"] = "no-worker"
            out["query_error"] = type(exc).__name__
        raw = d.raw_description
        pending = [{"activity": a.activity_type.name, "attempt": a.attempt, "worker": a.last_worker_identity,
                    "last_failure": a.last_failure.message[:100] if a.HasField("last_failure") else None}
                   for a in raw.pending_activities]
        out["pending"] = pending
        if any(p["attempt"] > 1 for p in pending):
            out["retrying"] = True
    else:
        try:
            out["result"] = await h_.result()
            out["state"] = out["result"].get("outcome", out["workflow_status"].lower())
        except Exception as exc:
            out["state"] = "failed"
            out["error"] = str(exc)[:200]
    return out


def get_run(h, m, body):
    return 200, aio(lambda c: describe(c, m.group(1)))


def post_reviewer_comment(session, comment):
    """The reviewer is a person acting on the Git host. Their comment is not an agent side effect."""
    with db() as c:
        r = c.execute("SELECT result FROM ledger WHERE session=%s AND tool='open_pr' AND status='done' ORDER BY step DESC LIMIT 1", (session,)).fetchone()
    if r:
        http("POST", f"{EXTERNAL_GIT}/pulls/{r['result']['number']}/comments", {"body": comment}, headers={"X-Actor": "human"})
        return r["result"]["number"]


def signal(h, m, body):
    session, name = m.group(1), m.group(2)
    payload = body or {}
    if name == "approve" and payload.get("comment"):
        post_reviewer_comment(session, payload["comment"])

    async def go(client):
        hd = client.get_workflow_handle(session)
        await hd.signal(name, payload) if name in ("approve", "resume") else await hd.signal(name)
    aio(go)
    return 200, {"signalled": name, "session": session}


def fork(h, m, body):
    """Rewind (replace=true) or fork (replace=false): a NEW run, started from the recorded state after step N.
    It gets the context log up to N and the workspace snapshot of step N, plus an optional hint. A rewind also cancels the original and
    undoes, from the ledger, the effects the original had after step N."""
    session, n, hint, replace = m.group(1), int(body["step"]), body.get("hint"), bool(body.get("replace"))
    new = f"{session}-{'r' if replace else 'f'}{n}-{uuid.uuid4().hex[:4]}"
    with db() as c:
        task = c.execute("SELECT task FROM runs WHERE session=%s", (session,)).fetchone()["task"]
        c.execute("INSERT INTO context_log (session, step, role, content, tokens) "
                  "SELECT %s, step, role, content, tokens FROM context_log WHERE session=%s AND step <= %s", (new, session, n))
        snap = c.execute("SELECT step, snapshot_id FROM snapshots WHERE session=%s AND step <= %s ORDER BY step DESC LIMIT 1", (session, n)).fetchone()
        c.execute("INSERT INTO runs (session, task, queue, parent, forked_at) VALUES (%s,%s,%s,%s,%s)",
                  (new, json.dumps(task), current_queue(), session, n))
    if hint:
        M.append_entry(new, n, "user", {"hint": hint})
    if replace:
        async def stop(client):
            await client.get_workflow_handle(session).signal("cancel")
        aio(stop)
        # the original's own cancellation compensates everything it did; effects up to step n are the new run's inheritance
        http("POST", f"{TOOL_GATEWAY_URL()}/compensate", {"session": session, "from_step": n + 1})

    async def go(client):
        await client.start_workflow("CodingAgentRun", {"session": new, "task": task, "step": n + 1, "snapshot": snap["snapshot_id"] if snap else None},
                                    id=new, task_queue=current_queue())
    aio(go)
    return 200, {"session": new, "parent": session, "from_step": n, "snapshot": snap["snapshot_id"] if snap else None, "replaced": replace}


def TOOL_GATEWAY_URL():
    from .common import TOOL_GATEWAY
    return TOOL_GATEWAY


async def runs_of(client, session):
    """Every engine run (execution) of one session, oldest first. Continue-as-new makes a new run under the same workflow ID."""
    found = []
    async for w in client.list_workflows(f"WorkflowId='{session}'"):
        found.append((w.start_time, w.run_id))
    return [rid for _, rid in sorted(found)]


def run_chain(h, m, body):
    session = m.group(1)

    async def go(client):
        out = []
        for rid in await runs_of(client, session):
            d = await client.get_workflow_handle(session, run_id=rid).describe()
            out.append({"run_id": rid[:8], "status": d.status.name.lower() if d.status else "?", "history_length": d.history_length})
        return out
    return 200, {"session": session, "runs": aio(go)}


def timeline(h, m, body):
    session = m.group(1)

    async def go(client):
        out, sched, attempts, queued = [], {}, {}, {}
        t0 = None
        for rid in await runs_of(client, session):
            hist = await client.get_workflow_handle(session, run_id=rid).fetch_history()
            sched, attempts, sched_t = {}, {}, {}
            for e in hist.events:
                t = e.event_time.ToDatetime().timestamp()
                t0 = t0 or t
                kind = e.WhichOneof("attributes")
                if kind == "activity_task_scheduled_event_attributes":
                    sched[e.event_id] = e.activity_task_scheduled_event_attributes.activity_type.name
                    sched_t[e.event_id] = t
                elif kind == "activity_task_started_event_attributes":
                    a = e.activity_task_started_event_attributes
                    attempts[a.scheduled_event_id] = (a.attempt, a.last_failure.message[:80] if a.HasField("last_failure") else "")
                    if a.attempt == 1:
                        queued[a.scheduled_event_id] = round(t - sched_t.get(a.scheduled_event_id, t), 1)
                elif kind == "activity_task_completed_event_attributes":
                    a = e.activity_task_completed_event_attributes
                    att, fail = attempts.get(a.scheduled_event_id, (1, ""))
                    out.append({"t": round(t - t0, 1), "activity": sched.get(a.scheduled_event_id), "attempts": att, "last_failure": fail,
                                "queued": queued.get(a.scheduled_event_id, 0)})
                elif kind == "activity_task_failed_event_attributes":
                    a = e.activity_task_failed_event_attributes
                    out.append({"t": round(t - t0, 1), "activity": sched.get(a.scheduled_event_id), "FAILED": a.failure.message[:80]})
                elif kind == "workflow_execution_signaled_event_attributes":
                    out.append({"t": round(t - t0, 1), "signal": e.workflow_execution_signaled_event_attributes.signal_name})
                elif kind == "workflow_execution_continued_as_new_event_attributes":
                    out.append({"t": round(t - t0, 1), "continued_as_new": True})
        return out
    events = aio(go)
    with db() as c:
        cost = c.execute("SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct FROM model_calls "
                         "WHERE session=%s AND status=200", (session,)).fetchone()
    return 200, {"session": session, "events": events, "model_calls": cost["calls"], "tokens": int(cost["pt"] + cost["ct"])}


def list_running(client):
    async def go():
        ids = []
        async for w in client.list_workflows("WorkflowType='CodingAgentRun' AND ExecutionStatus='Running'"):
            ids.append(w.id)
        return ids
    return go()


def pause_all(h, m, body):
    async def go(client):
        ids = await list_running(client)
        for i in ids:
            await client.get_workflow_handle(i).signal("pause")
        return ids
    ids = aio(go)
    return 200, {"paused": len(ids), "sessions": ids}


def resume_all(h, m, body):
    """After an incident: resume every paused run, a few per second so they do not all hit the provider at once."""
    rate = float(body.get("per_second", 2))

    async def go(client):
        ids = await list_running(client)
        n = 0
        t0 = time.time()
        for i in ids:
            st = await client.get_workflow_handle(i).query("status")
            if st["state"] == "paused":
                await client.get_workflow_handle(i).signal("resume", {})
                n += 1
                await asyncio.sleep(1.0 / rate)
        return n, round(time.time() - t0, 1)
    n, secs = aio(go)
    return 200, {"resumed": n, "seconds": secs, "per_second": rate}


def set_queue(h, m, body):
    set_cfg("current_queue", body["queue"])
    return 200, {"current_queue": body["queue"]}


def list_runs(h, m, body):
    async def go(client):
        out = []
        async for w in client.list_workflows("WorkflowType='CodingAgentRun'"):
            row = {"session": w.id, "status": w.status.name if w.status else "?", "queue": w.task_queue}
            if w.status == WorkflowExecutionStatus.RUNNING:
                try:
                    row.update(await client.get_workflow_handle(w.id).query("status"))
                except Exception:
                    row["state"] = "unreachable"
            out.append(row)
        return out
    return 200, {"runs": aio(go)}


class H(Handler):
    routes = {
        ("POST", r"/runs"): start_run,
        ("GET", r"/runs"): list_runs,
        ("GET", r"/runs/([^/]+)"): get_run,
        ("POST", r"/runs/([^/]+)/signal/(approve|pause|resume|cancel)"): signal,
        ("POST", r"/runs/([^/]+)/fork"): fork,
        ("GET", r"/runs/([^/]+)/timeline"): timeline,
        ("GET", r"/runs/([^/]+)/chain"): run_chain,
        ("POST", r"/ops/pause-all"): pause_all,
        ("POST", r"/ops/resume-all"): resume_all,
        ("POST", r"/ops/queue"): set_queue,
        ("GET", r"/healthz"): lambda h, m, b: (200, {"ok": True}),
    }


def main():
    serve(H)

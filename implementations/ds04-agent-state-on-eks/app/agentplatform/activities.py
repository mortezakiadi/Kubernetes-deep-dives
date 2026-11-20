"""The activities: everything non-deterministic the agent does. Each is recorded in the run's history when it finishes,
so replay returns the recorded result and never runs it again (Design 04, "Steps, history and replay").

They are plain (synchronous) functions run on a thread pool. A long one heartbeats while it waits, so a dead worker is noticed
within the heartbeat timeout rather than after the whole step.
"""
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import model as M
from .common import MODEL_GATEWAY, SANDBOX_MANAGER, TOOL_GATEWAY, db, http, tokens

LOCAL_TOOLS = {"list_dir", "write_file", "run_tests"}


def beating(fn, *args, interval=3, **kw):
    """Run a blocking call on another thread and heartbeat until it returns."""
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(fn, *args, **kw)
        while True:
            try:
                return fut.result(timeout=interval)
            except FutTimeout:
                activity.heartbeat()


@activity.defn
def call_model(session: str, step: int, task: dict) -> dict:
    prompt, summary_id, ctx_tokens = M.build_prompt(session, step)
    status, r = beating(http, "POST", f"{MODEL_GATEWAY}/v1/complete", {
        "session": session, "step": step, "kind": "act", "prompt": prompt,
        "token_budget": task.get("token_budget"), "allow_fallback": task.get("allow_fallback", True)}, timeout=180)
    if status == 429:
        raise ApplicationError(r.get("error", "budget"), type="BudgetReached", non_retryable=True)
    if status != 200:
        raise ApplicationError(f"model gateway {status}: {r.get('error')}", type="ModelUnavailable")
    action = r["action"]
    M.append_entry(session, step, "assistant", action)
    return {"action": action, "backend": r["backend"], "prompt_sha": r["prompt_sha"], "usage": r["usage"], "context_tokens": ctx_tokens}


@activity.defn
def context_size(session: str, step: int) -> int:
    return M.context_tokens_since_summary(session, step)


@activity.defn
def compact_context(session: str, step: int) -> dict:
    """A summary is a step like any other: stored, and recorded in the history, so a restart uses the same one."""
    prompt, _, _ = M.build_prompt(session, step)
    status, r = http("POST", f"{MODEL_GATEWAY}/v1/complete", {"session": session, "step": step, "kind": "summarize", "prompt": prompt})
    if status != 200:
        raise ApplicationError(f"model gateway {status}", type="ModelUnavailable")
    text = json.dumps(r["action"], sort_keys=True)
    with db() as c:
        row = c.execute("INSERT INTO summaries (session, upto_step, text, tokens) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (session, upto_step) DO UPDATE SET text=summaries.text RETURNING id, tokens",
                        (session, step - 1, text, tokens(text))).fetchone()
    return {"summary_id": row["id"], "tokens": row["tokens"], "upto_step": step - 1}


def ensure_sandbox(session, snapshot):
    status, r = http("POST", f"{SANDBOX_MANAGER}/sandboxes", {"session": session, "snapshot_id": snapshot}, timeout=200)
    if status != 200:
        raise ApplicationError(f"sandbox manager {status}: {r.get('error')}", type="SandboxUnavailable")
    return r


@activity.defn
def run_tool(session: str, step: int, tool: dict, last_snapshot: str, task: dict) -> dict:
    name = tool["name"]
    sandbox = None
    if name in LOCAL_TOOLS:
        sandbox = beating(ensure_sandbox, session, last_snapshot)
    status, r = beating(http, "POST", f"{TOOL_GATEWAY}/tools/{name}", {
        "session": session, "step": step, "args": tool["args"], "sandbox_ip": sandbox["ip"] if sandbox else None},
        headers={"Idempotency-Key": f"{session}:{step}"}, timeout=900)
    if status == 409:
        raise ApplicationError(r.get("error", "needs a human"), type="NeedsHuman", non_retryable=True)
    if status != 200:
        raise ApplicationError(f"tool gateway {status}: {r.get('error')}", type="ToolFailed")
    result = r["result"]
    brief = {k: (v if not isinstance(v, str) else v[:300]) for k, v in result.items()}
    M.append_entry(session, step, "tool", {"tool": name, "result": brief})
    return {"result": brief, "deduplicated": r.get("deduplicated", False), "via": r.get("via"), "sandbox_created": bool(sandbox and sandbox.get("created")),
            "restored_files": (sandbox or {}).get("restored_files", 0)}


@activity.defn
def snapshot_workspace(session: str, step: int) -> dict:
    """Upload what changed. It must find the sandbox the step ran in: a snapshot of a fresh, empty sandbox would be a lie."""
    s, r = http("GET", f"{SANDBOX_MANAGER}/sandboxes")
    if session not in r.get("sandboxes", []):
        raise ApplicationError("the sandbox is gone; the step must run again", type="SandboxLost", non_retryable=True)
    ip = ensure_sandbox(session, None)["ip"]
    s, snap = beating(http, "POST", f"http://{ip}:8081/snapshot", {"session": session, "step": step}, timeout=300)
    if s != 200:
        raise ApplicationError(f"snapshot failed: {snap}", type="SnapshotFailed")
    with db() as c:
        c.execute("INSERT INTO snapshots (session, step, snapshot_id, files, uploaded, skipped) VALUES (%s,%s,%s,%s,%s,%s) "
                  "ON CONFLICT (session, step) DO UPDATE SET snapshot_id=EXCLUDED.snapshot_id",
                  (session, step, snap["snapshot_id"], snap["files"], snap["uploaded"], snap["skipped"]))
    return snap


@activity.defn
def release_sandbox(session: str) -> dict:
    http("DELETE", f"{SANDBOX_MANAGER}/sandboxes/{session}")
    return {"released": True}


@activity.defn
def compensate(session: str, from_step: int) -> dict:
    s, r = http("POST", f"{TOOL_GATEWAY}/compensate", {"session": session, "from_step": from_step})
    return r


@activity.defn
def lint(session: str, step: int) -> dict:
    """Added in version 2 of the workflow (see S09). Not part of version 1."""
    ip = ensure_sandbox(session, None)["ip"]
    s, r = http("POST", f"http://{ip}:8081/exec", {"cmd": "python -m py_compile src/slugify.py && echo ok", "timeout": 30})
    return {"ok": s == 200 and r.get("exit") == 0}


ALL = [call_model, context_size, compact_context, run_tool, snapshot_workspace, release_sandbox, compensate, lint]

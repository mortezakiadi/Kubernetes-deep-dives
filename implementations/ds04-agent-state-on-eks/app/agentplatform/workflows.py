"""The agent run as a durable workflow (Design 04, "Steps, history and replay").

The workflow code DECIDES: call the model, look at the answer, choose a tool, call it, snapshot, repeat. Every model call, tool
call and sandbox command is an activity, recorded in the history. If the worker dies, another one replays this code against the
history: recorded steps return their recorded results, and the code reaches the first step with no result and runs it for real.
So everything here must be deterministic. No clocks, no randomness, no I/O: those live in activities.

VARIANT selects which version of this code a worker runs. It exists so Scenario S09 can show a code change that breaks
replay, and the same change made safely with a patch marker.
  v1            the original
  v2-unpatched  adds a lint step after every file change, with no protection: runs already in flight cannot replay
  v2-patched    the same change, behind workflow.patched(): old histories take the old path, new runs take the new one
"""
import os
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

VARIANT = os.environ.get("WORKFLOW_VARIANT", "v1")

MODEL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=30), maximum_attempts=8)
TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0, maximum_interval=timedelta(seconds=20), maximum_attempts=6)
LOCAL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5)


def cause_type(err):
    c = getattr(err, "cause", None)
    return getattr(c, "type", None) or type(c).__name__


@workflow.defn(name="CodingAgentRun")
class CodingAgentRun:
    def __init__(self) -> None:
        self.state = "running"
        self.paused = False
        self.cancelled = False
        self.approval = None
        self.resume_requested = False
        self.extend = {}
        self.step = 0
        self.snapshot = None
        self.tokens_used = 0
        self.recent = []
        self.attention = ""

    # ---- what people can do to a run ---------------------------------------------------------------------------
    @workflow.signal
    def approve(self, payload: dict) -> None:
        self.approval = payload or {"approved": True}

    @workflow.signal
    def pause(self) -> None:
        self.paused = True

    @workflow.signal
    def resume(self, payload: dict) -> None:
        self.paused = False
        self.resume_requested = True
        self.extend = payload or {}

    @workflow.signal
    def cancel(self) -> None:
        self.cancelled = True

    @workflow.query
    def status(self) -> dict:
        return {"state": self.state, "step": self.step, "snapshot": self.snapshot, "tokens": self.tokens_used, "attention": self.attention}

    # ---- helpers -----------------------------------------------------------------------------------------------
    async def act(self, name, args, timeout, retry=None, heartbeat=None):
        return await workflow.execute_activity(name, args=args, start_to_close_timeout=timeout, retry_policy=retry,
                                               heartbeat_timeout=heartbeat)

    async def needs_attention(self, why):
        self.state, self.attention = "needs_attention", why
        self.resume_requested = False
        await workflow.wait_condition(lambda: self.resume_requested or self.cancelled)
        self.attention = ""

    async def finish_cancelled(self, session):
        self.state = "cancelling"
        undone = await self.act("compensate", [session, 0], timedelta(minutes=5))
        await self.act("release_sandbox", [session], timedelta(minutes=2))
        self.state = "cancelled"
        return {"session": session, "outcome": "cancelled", "step": self.step, "compensated": undone.get("compensated", [])}

    # ---- the run -----------------------------------------------------------------------------------------------
    @workflow.run
    async def run(self, p: dict) -> dict:
        session, task = p["session"], p["task"]
        self.step, self.snapshot, self.tokens_used = p.get("step", 0), p.get("snapshot"), p.get("tokens", 0)
        self.recent = p.get("recent", [])
        done_here = 0
        max_steps = task.get("max_steps", 400)
        token_budget = task.get("token_budget")
        while True:
            if self.paused:
                self.state = "paused"
                await workflow.wait_condition(lambda: (not self.paused) or self.cancelled)
            if self.cancelled:
                return await self.finish_cancelled(session)
            self.state = "running"

            # Budgets are checked before every model call. Reaching one asks for a person; it never carries on silently.
            if self.step >= max_steps or (token_budget and self.tokens_used >= token_budget):
                await self.needs_attention("step or token budget reached")
                if self.cancelled:
                    return await self.finish_cancelled(session)
                max_steps += int(self.extend.get("extend_steps", 0))
                token_budget = (token_budget or 0) + int(self.extend.get("extend_tokens", 0)) or None
                task = {**task, "token_budget": token_budget}
                continue

            # Keep the history small: end this one and start a fresh one that carries only the position.
            if done_here and ((task.get("can_every") and done_here >= task["can_every"]) or workflow.info().is_continue_as_new_suggested()):
                workflow.continue_as_new({"session": session, "task": task, "step": self.step, "snapshot": self.snapshot,
                                          "tokens": self.tokens_used, "recent": self.recent})

            if task.get("context_budget"):
                size = await self.act("context_size", [session, self.step], timedelta(seconds=30), LOCAL_RETRY)
                if size > task["context_budget"]:
                    await self.act("compact_context", [session, self.step], timedelta(minutes=2), MODEL_RETRY)

            try:
                reply = await self.act("call_model", [session, self.step, task], timedelta(minutes=5), MODEL_RETRY, timedelta(seconds=30))
            except ActivityError as err:
                if cause_type(err) == "BudgetReached":
                    await self.needs_attention("token budget reached")
                    if self.cancelled:
                        return await self.finish_cancelled(session)
                    token_budget = (token_budget or 0) + int(self.extend.get("extend_tokens", 0))
                    task = {**task, "token_budget": token_budget}
                    continue
                await self.needs_attention(f"the model could not be reached: {cause_type(err)}")
                if self.cancelled:
                    return await self.finish_cancelled(session)
                continue
            self.tokens_used += reply["usage"]["prompt"] + reply["usage"]["completion"]
            action = reply["action"]
            if action.get("done"):
                await self.act("release_sandbox", [session], timedelta(minutes=2))
                self.state = "completed"
                return {"session": session, "outcome": "completed", "steps": self.step, "summary": action["summary"]}
            tool = action["tool"]

            # Loop detection: the same call, again and again, is a run that is not going anywhere.
            sig = f"{tool['name']}:{sorted(tool['args'].items()) if isinstance(tool['args'], dict) else tool['args']}"
            self.recent = (self.recent + [sig])[-3:]
            if len(self.recent) == 3 and len(set(self.recent)) == 1:
                await self.needs_attention(f"loop detected: {tool['name']} called three times with the same arguments")
                self.recent = []
                if self.cancelled:
                    return await self.finish_cancelled(session)
                continue

            # A person can be waited for as long as it takes. Nothing is held while waiting: no worker, no sandbox.
            if tool.get("needs_approval"):
                await self.act("release_sandbox", [session], timedelta(minutes=2))
                self.state = "waiting"
                self.approval = None
                try:
                    await workflow.wait_condition(lambda: self.approval is not None or self.cancelled, timeout=timedelta(days=7))
                except TimeoutError:
                    await self.needs_attention("nobody approved within seven days")
                if self.cancelled:
                    return await self.finish_cancelled(session)
                self.state = "running"

            # Run the tool, then snapshot the files. If the sandbox vanished between the two, the tool must run again.
            ok = False
            for _ in range(3):
                try:
                    await self.act("run_tool", [session, self.step, tool, self.snapshot, task], timedelta(minutes=30), TOOL_RETRY, timedelta(seconds=20))
                except ActivityError as err:
                    if cause_type(err) == "NeedsHuman":
                        await self.needs_attention(f"{tool['name']} may have changed the world and cannot be checked")
                        if self.cancelled:
                            return await self.finish_cancelled(session)
                        continue
                    await self.needs_attention(f"{tool['name']} failed: {cause_type(err)}")
                    if self.cancelled:
                        return await self.finish_cancelled(session)
                    continue
                if tool.get("changes_files"):
                    try:
                        snap = await self.act("snapshot_workspace", [session, self.step], timedelta(minutes=5), LOCAL_RETRY)
                    except ActivityError as err:
                        if cause_type(err) == "SandboxLost":
                            continue
                        raise
                    self.snapshot = snap["snapshot_id"]
                    if VARIANT == "v2-unpatched":
                        await self.act("lint", [session, self.step], timedelta(minutes=2), LOCAL_RETRY)
                    elif VARIANT == "v2-patched" and workflow.patched("lint-after-snapshot"):
                        await self.act("lint", [session, self.step], timedelta(minutes=2), LOCAL_RETRY)
                ok = True
                break
            if not ok:
                await self.needs_attention(f"{tool['name']} did not complete")
                if self.cancelled:
                    return await self.finish_cancelled(session)
                continue
            self.step += 1
            done_here += 1

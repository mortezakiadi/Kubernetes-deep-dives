---
id: design-agent-state
title: "Design 04: State management for long-running agents"
short: "Long-running agent state"
---
This design answers one question: *how do we let an agent work for hours or days (calling models, running code, using tools, waiting for people) and make sure that no failure of a pod, a node, a model provider or a deployment loses its progress, repeats a side effect or leaves it stuck?* It builds on [Design 01](#design-sandboxing) for where agent code runs and on [Memory and long-running work](#post-agents-prod) for the basic patterns.

The short version: *the agent's state is not its process.* Every agent run is a durable workflow whose event history is the source of truth. Each model call and tool call is a recorded step, so recovery replays the history and continues from the first step that hadn't finished, without calling the model again for steps that already happened. Files and other working state are snapshotted to object storage at step boundaries, side effects carry idempotency keys and go through a ledger, and long waits cost nothing because no pod waits with them.

### The use case and requirements

**Scenario.** The company's coding agent takes a ticket, explores the repo, writes code, runs the tests in a sandbox, opens a pull request, waits for a reviewer and addresses comments. A typical run takes three hours of work spread over two days. Last month, runs failed for a dozen reasons: a node was replaced, the model API returned errors for ten minutes, the agent service was redeployed, a sandbox ran out of disk, a reviewer took the weekend. Each failure started the run from scratch, burned the tokens again, and twice opened duplicate pull requests.

**What the design must do.**

- **Survive any single failure:** worker pod or node loss, sandbox loss, model provider errors, deployments, and a lost availability zone, and continue from the last completed step.
- **Never repeat a side effect by accident:** a retried step must not open a second pull request, send a second email or charge a card twice.
- **Wait cheaply:** a run waiting two days for a human approval holds no pod, no GPU and no sandbox.
- **Let people steer:** pause, resume, cancel, approve, and rewind a run to an earlier step, or fork it to try another path.
- **Explain itself:** for any run, show every step, its inputs and outputs, its cost and why it failed or retried.

**What it must avoid.**

- Treating a framework's in-process checkpoint as recovery. A checkpoint that nobody resumes from is a backup, not a recovery mechanism.
- Long-lived pods that "hold" a run, because they're exactly what fails.
- Unbounded runs that loop forever, spending tokens.

**Out of scope for this design:** the agent's reasoning and prompts, the sandbox's isolation ([Design 01](#design-sandboxing)), and long-term memory across unrelated runs.

### Five kinds of agent state

**Scenario.** The first recovery attempt saved the conversation messages to Redis and called it a checkpoint. After a crash, the agent resumed with the right messages but an empty workspace, no idea whether it had already pushed its branch, and a half-finished test run. "State" turned out to be five different things.

![Five kinds of agent state](diagrams/designs/ds04-1.svg)

**What the picture shows.** The five kinds of state a long-running agent has, where each one lives, and how it comes back after a failure.

- **Execution state:** where the run is, which steps finished and what each returned. Lives in the durable execution engine's event history. Recovered by replaying the history. This is the source of truth for everything else.
- **Context state:** the messages, tool results and summaries that make up what the model sees. Lives in a session database, referenced from step results. Recovered by rebuilding the prompt from the stored log and the latest summary.
- **Working state:** files, a git checkout, installed packages, a browser profile. Lives in the sandbox while the run is active and in object storage as snapshots. Recovered by starting a new sandbox from the latest snapshot.
- **External effects:** what the agent changed in the world: a pushed branch, a pull request, a ticket comment. Lives in the other systems and in the platform's side-effect ledger. Recovered by checking the ledger, never by doing it again blindly.
- **Process memory:** variables in the running agent process, open connections, a running test. Not saved at all. Anything that matters must be in one of the four kinds above by the end of each step.

**Going deeper.**

- **The step boundary is the unit of consistency.** At the end of every step, its result is recorded in the history, any new context is in the database, any changed files are snapshotted, and any side effect is in the ledger. A failure in the middle of a step loses at most that step.
- **Process snapshots are an optimization, not the source of truth.** Kubernetes is working on pod-level checkpoint and restore (KEP-5823, an alpha API from the new Checkpoint/Restore working group), and CRIU can snapshot a running process. They can make a restart faster for a long test run, but restoring a process doesn't tell you which side effects happened. The design never depends on them.
- **Keep large data out of the history.** Model responses, file contents and tool outputs can be large. The history stores a reference (an object key and a hash); the payload lives in object storage.

### The architecture at a glance

**Scenario.** The team needs one picture that shows which components hold state, which are stateless and can be replaced at any time, and how a run moves between them.

![The architecture at a glance](diagrams/designs/ds04-2.svg)

**What the picture shows.** Stateless workers in the middle, everything durable around them.

- **Session API:** starts runs, sends signals (approve, pause, cancel, rewind), streams progress to the product UI. Every run has a stable session ID.
- **Durable execution engine:** Temporal (self-hosted on EKS with Aurora PostgreSQL, or Temporal Cloud) or Dapr Workflow. It stores each run's event history, hands out tasks to workers, enforces timeouts and retries, and holds timers and waits for signals.
- **Agent workers (stateless):** Deployments that run the agent's workflow code and its activities: model calls through the model gateway, tool calls through the tool gateway, sandbox commands through the Sandbox Router. Any worker can continue any run.
- **Sandboxes:** from [Design 01](#design-sandboxing), one per run, started from the latest workspace snapshot, paused or deleted while the run waits.
- **State stores:** the engine's database (event histories), a session database (context log, summaries, metadata), object storage (workspace snapshots and large payloads) and the side-effect ledger.
- **Gateways:** the model gateway (retries, fallbacks, token budgets per run) and the tool gateway (idempotency keys, ledger writes, approvals).

**Going deeper.**

- **Why a durable execution engine and not just a framework checkpointer:** a framework checkpointer saves state from inside one process; if that process dies, something else must notice, find the run and start it again. A durable execution engine owns that job: it detects the lost worker through timeouts and hands the run to another one. Framework checkpointers (LangGraph's, for example) can still be used inside a run, backed by the engine.
- **Workers scale on queue depth:** KEDA scales the worker Deployment on the engine's task queue backlog, not on CPU, since most of a worker's time is spent waiting on model and tool calls.
- **The engine is the most important dependency.** Run it with the same care as the main database: multi-AZ, backups, and its own upgrade plan.

### Steps, history and replay

**Scenario.** A run is on step 47 of a refactoring task when its worker pod is evicted. The team wants step 48 to run next, on another worker, and the 46 completed model calls not to be paid for again.

![Steps, history and replay](diagrams/designs/ds04-3.svg)

**What the picture shows.** How a run is split into recorded steps, and what happens when the worker dies in the middle of one.

- **Workflow code decides; activities do:** the agent loop ("call the model, look at the answer, choose a tool, call it, repeat") is workflow code. Every model call, tool call and sandbox command is an activity: its input and result are recorded in the history.
- **The worker dies in step 5:** the engine notices when the activity's heartbeat stops and its timeout passes, and schedules the run on another worker.
- **Replay:** the new worker re-runs the workflow code from the beginning, but for steps 1 to 4 the engine returns the recorded results instead of executing them. No model is called and no tool runs again. The code reaches step 5, which has no result yet, and executes it for real.
- **Deterministic workflow code:** because of replay, the workflow code must make the same decisions given the same recorded results. Anything non-deterministic (model calls, random numbers, the current time, reading files) happens inside activities or through the engine's APIs.

```python
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent.activities import call_model, run_tool, snapshot_workspace

LLM_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=8)

@workflow.defn
class CodingAgentRun:
    def __init__(self) -> None:
        self.approved = False

    @workflow.signal
    def approve(self) -> None:
        self.approved = True

    @workflow.run
    async def run(self, session_id: str, step: int = 0) -> str:
        while step < 400:                                    # hard cap on steps
            reply = await workflow.execute_activity(         # recorded: never re-called on replay
                call_model, args=[session_id, step],
                start_to_close_timeout=timedelta(minutes=5), retry_policy=LLM_RETRY)
            if reply.done:
                return reply.summary
            if reply.tool.needs_approval:                    # waits with no pod, no sandbox
                await workflow.wait_condition(lambda: self.approved, timeout=timedelta(days=7))
                self.approved = False
            await workflow.execute_activity(
                run_tool, args=[session_id, step, reply.tool],   # idempotency key = session + step
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(seconds=60))
            await workflow.execute_activity(
                snapshot_workspace, args=[session_id, step],
                start_to_close_timeout=timedelta(minutes=5))
            step += 1
            if workflow.info().is_continue_as_new_suggested():   # keep history small
                workflow.continue_as_new(args=[session_id, step])
        return "stopped: step limit reached"
```

**Going deeper.**

- **One activity per model call or tool call.** Bigger steps lose more work on failure; smaller steps make the history long. One per call is the natural boundary for agents.
- **Long activities heartbeat.** A test run that takes 20 minutes sends a heartbeat every few seconds (with progress details), so a dead worker is detected within the heartbeat timeout instead of after 20 minutes.
- **History size is bounded.** Very long runs restart themselves with "continue as new": the workflow ends its current history and starts a fresh one with just the session ID and step number, while the context log stays in the session database.
- **Model calls are recorded, so replay is exact.** Even though the model would answer differently if asked again, replay uses the recorded answer, so the run continues on the same path it was already on.

### Side effects: exactly once where it matters

**Scenario.** The tool call "open a pull request" succeeded at GitHub, but the worker died before recording the result. On retry, the agent opened a second, identical pull request. A retry is only safe if the tool knows it has already done the work.

![Side effects: exactly once where it matters](diagrams/designs/ds04-4.svg)

**What the picture shows.** How a tool call with a side effect is made safe to retry.

- **Idempotency key per step:** the tool gateway gives every call a key built from the session ID and the step number (not the attempt number), so every retry of the same step carries the same key.
- **Check the ledger first:** before calling the external system, the gateway looks the key up in the side-effect ledger. If the effect is already recorded as done, it returns the stored result without calling anything.
- **Pass the key on:** for APIs that accept an idempotency key (most payment APIs, many internal ones), the gateway sends it, so the external system deduplicates even if the ledger write was lost.
- **Look before doing, for APIs that don't:** for those, the tool defines a lookup ("is there already an open pull request from branch `agent/sess-7f3a`?") that the gateway runs before retrying.
- **Record the outcome:** the result goes into the ledger and back to the workflow history. Only then is the step complete.

**Going deeper.**

- **Classify every tool:** read-only (safe to retry), idempotent (safe with a key), checkable (safe with a lookup), and unsafe (needs a human to confirm a retry). The tool's class is part of its registration in the tool gateway, and unsafe tools are rare and reviewed.
- **Name things deterministically.** A branch called `agent/<session>-<step>` or a ticket comment tagged with the session ID turns "did I already do this?" into a simple lookup.
- **Compensation for multi-step changes:** if a run is cancelled after creating a branch and a ticket, a compensating workflow (the Saga pattern) can close and clean them up, using the ledger to know what exists.

### Working state: workspace snapshots and sandbox restore

**Scenario.** A run's sandbox node is reclaimed during step 31. The new sandbox comes up empty, and the agent, which "remembers" editing six files, runs tests against code that isn't there.

![Working state: workspace snapshots and sandbox restore](diagrams/designs/ds04-5.svg)

**What the picture shows.** How files survive the loss of a sandbox.

- **Snapshot after every step that changes files:** the `snapshot_workspace` activity uploads what changed in `/workspace` to object storage, content-addressed so unchanged files aren't uploaded again, and records the snapshot ID in the history.
- **For git repositories, commit:** the agent's working branch is committed and pushed to an internal remote at each step, which is a cheap, diffable snapshot.
- **Sandbox lost:** the next sandbox command fails, the activity retries, and the retry asks the Sandbox Manager for a new sandbox. Its first act is to restore the snapshot recorded at the end of the last completed step.
- **Slow-to-rebuild state:** installed packages, build caches and model files are reinstalled from a cache volume or a per-repo prebuilt image, so restore takes seconds, not a full `npm install`.

**Going deeper.**

- **The snapshot ID belongs in the step result,** so replay always pairs the right files with the right point in the conversation. Rewinding to step 20 means restoring snapshot 20, not "the latest".
- **Pause long waits completely.** While a run waits for a reviewer, its sandbox is deleted (or paused, see [Sandbox lifecycle](#ds01-4)) after its final snapshot. Waiting two days costs a database row and a few megabytes in S3.
- **Snapshots are user data.** Encrypt them, scope access to the run's tenant, and delete them with the session according to the retention policy.

### Context state: what the model sees after a restart

**Scenario.** After a 10-hour run, the agent's message log is 2 million tokens, far beyond the model's context window. Before the restart, the agent was working from a running summary that lived only in process memory. After the restart, it had to rebuild that summary and made different choices.

**The design.**

- **An append-only context log:** every model input, output and tool result for the run is stored in the session database (large payloads in object storage), keyed by session and step.
- **Summaries are steps too:** when the context gets close to its budget, a `compact_context` activity produces a summary, which is stored and recorded in the history like any other step. After a restart, the agent uses the same summary it used before.
- **The prompt is rebuilt, not restored:** each `call_model` activity builds its prompt from the system prompt, the latest summary and the log entries after it. Workers keep no context in memory between steps.
- **Prompt caching still works:** because the rebuilt prompt is identical to the one before the failure, the model provider's prefix cache and the inference platform's KV-cache routing (see [Design 03](#design-training-inference)) still hit.

**Going deeper.**

- **Version the agent's prompts and tools with the run.** A run records which agent version it started with; changes to the system prompt or tool definitions mid-run are a deliberate migration, not an accident of a deployment.
- **Long-term memory is separate.** Facts the agent should remember across runs are written to a memory store by an explicit step, not inferred from old context logs.

### Run lifecycle: pause, resume, rewind and fork

**Scenario.** A reviewer sees the agent heading in the wrong direction at step 60 and wants to send it back to step 52 with a hint, while keeping the original run for comparison. Operators also need to stop a run that's looping, and resume all runs after an incident.

![Run lifecycle: pause, resume, rewind and fork](diagrams/designs/ds04-6.svg)

**What the picture shows.** The states a run can be in, and the actions that move it between them.

- **Running:** a worker is executing a step. Most of the wall-clock time is actually spent in the next two states.
- **Waiting:** for a human approval, a timer, or an external event (a CI result, a webhook). No worker or sandbox is held.
- **Retrying:** a step failed and is being retried with backoff. Model errors and sandbox loss are retried automatically within limits.
- **Needs attention:** retries are exhausted, a budget is reached, or a loop is detected. The run waits for a person: fix and resume, rewind, or cancel.
- **Paused:** stopped by a person or by an operator (for example, during a model provider incident). Resumes exactly where it stopped.
- **Rewind and fork:** a new run is started from the history and snapshot of step N, optionally with a new instruction. Rewind replaces the current run; fork keeps both.
- **Completed or cancelled:** final. Cancellation runs compensation for any side effects the ledger shows.

**Going deeper.**

- **Budgets are part of the workflow:** maximum steps, tokens, wall-clock time and cost per run, checked before every model call. Reaching one moves the run to "needs attention", never silently on.
- **Loop detection:** the same tool call with the same arguments several times in a row, or no change to the workspace across many steps, moves the run to "needs attention".
- **Bulk operations:** after a model provider outage, operators resume all runs paused during the incident with one command, rate-limited so they don't all hit the provider at once.

### Deploying new agent code without breaking running runs

**Scenario.** The team ships a new version of the agent's workflow code on Tuesday. Runs that started on Monday replay their history through the new code, take a different branch at step 12 and fail with a non-determinism error. A deployment must not break runs that are already in flight.

**The design.**

- **Version the workers:** each build of the agent is deployed as its own worker version. With Temporal's worker versioning (or the equivalent in other engines), a run stays on the version it started with, and new runs go to the new version.
- **Run old and new side by side:** the old worker Deployment keeps running, scaled down, until its last run completes or reaches a safe migration point, then it's removed.
- **Patch only when necessary:** when a running workflow must pick up a fix, the code uses the engine's patching API so old histories take the old path and new ones the new path.
- **Replay tests in CI:** every change to workflow code is replayed against a sample of recorded production histories before it ships. A non-determinism error fails the build, not a customer's run.

**Going deeper.**

- **Activities can change freely:** changing what `run_tool` does doesn't affect replay, because replay uses recorded results. Only the decision logic in the workflow code has to stay deterministic.
- **Graceful shutdown:** on SIGTERM, a worker stops taking new tasks and lets in-flight activities finish within its termination grace period. Anything that doesn't finish is retried by another worker, so a rolling update is safe.

### Failure modes and operations

**Scenario.** Before launch, the on-call team walks through what happens when each piece fails.

| Failure | What users see | What the design does |
|---|---|---|
| Worker pod or node lost | A pause of seconds to a minute | Activity heartbeat times out; the step is retried on another worker; replay restores the rest |
| Sandbox lost | Step retried, slightly slower | New sandbox restored from the last snapshot; the step runs again with the same idempotency key |
| Model provider errors | Steps retry; then the run pauses | Retries with backoff and a fallback model where allowed; an operator pauses affected runs if the outage is long |
| New agent version deployed | Nothing | Runs stay on their starting version; replay tests in CI |
| Engine database failover | Short pause of all runs | Multi-AZ database; workers reconnect; no history is lost |
| Run loops or overspends | Run stops and asks for help | Budgets and loop detection move it to "needs attention" |
| Region outage | Runs in that region pause | Engine with a standby in another region, or runs restarted from replicated histories and snapshots |

**Going deeper.**

- **What to measure:** active runs by state, time in "needs attention", steps per run, retries per step type, replay time, snapshot size and restore time, tokens and cost per run, and how many side effects were deduplicated by the ledger (a rising number means flaky tools or workers).
- **Every run has a timeline:** the product UI and the on-call tools show the same view: each step, its result, its retries and its cost, built from the history.

### Rollout plan, trade-offs and open questions

**Scenario.** Leadership asks for the order of work that removes the most failures soonest.

**Rollout.**

- **Phase 1, the backbone:** the durable execution engine on EKS, the agent loop as a workflow with model and tool calls as activities, budgets and step limits. Worker loss and model errors stop being fatal.
- **Phase 2, side effects:** the tool gateway's idempotency keys, the side-effect ledger and tool classification. Duplicate pull requests and emails stop.
- **Phase 3, working state:** workspace snapshots, sandbox restore and deleting sandboxes during long waits. Sandbox loss stops being fatal, and waiting becomes cheap.
- **Phase 4, control:** pause, rewind and fork in the product, worker versioning with replay tests, and bulk operations for incidents.

**Trade-offs we accepted.**

- **A durable execution engine as a core dependency:** another stateful system to run, in exchange for recovery, timers, retries and history that we'd otherwise build badly ourselves.
- **Deterministic workflow code:** developers must keep decisions in workflow code and everything else in activities, and learn the engine's rules. Replay tests catch mistakes.
- **A snapshot after every file-changing step:** extra storage and a few seconds per step, in exchange for never losing more than one step of work.
- **Process snapshots left out:** CRIU and pod-level checkpointing could restore a running test faster, but they're young and don't solve side effects, so they're an optimization for later.

**Open questions for the next review.**

- **Engine choice:** Temporal for its maturity and worker versioning, or Dapr Workflow for teams that already run Dapr and want agents and workflows in one runtime.
- **Pod-level checkpoint and restore:** once the Kubernetes API matures, is it worth using to resume long-running tool processes (a test suite, a build) instead of restarting them?
- **Multi-agent runs:** when one run spawns sub-agents, should each be a child workflow with its own budget, and how do cancellation and rewind propagate between them?
- **Retention:** how long to keep histories, context logs and snapshots for audit, versus deleting them for privacy.

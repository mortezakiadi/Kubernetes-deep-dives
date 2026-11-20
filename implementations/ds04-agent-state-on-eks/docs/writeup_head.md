---
id: appendix-design-04-eks
title: "Appendix E: Design 04 built and tested on Amazon EKS"
short: "E. Design 04 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 04: State management for long-running agents](#design-agent-state). I built it with Terraform on Amazon EKS in one AWS account: a durable execution engine (Temporal) on a Multi-AZ PostgreSQL database, stateless agent workers that scale on the engine's queue, a model gateway and a tool gateway with a side-effect ledger, a sandbox manager with content-addressed workspace snapshots, and a session API for starting, steering, rewinding and forking runs.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design describes runs of hours or days that span a weekend. The runs in this build last under two minutes, with test steps of 2 to 100 seconds, so a "long wait" is minutes and not days. Nothing in the mechanism depends on that: a run waiting for a reviewer is a row in the engine's database whether it waits for one minute or one weekend, and S06 shows that nothing is held while it waits. The agent's brain is also scaled down. The model is a scripted stand-in that answers as a fixed function of its prompt, and the Git host is a small service I wrote. The design is about what happens around the model and around the outside world, and a scripted model and a Git host that can be told to lose its answer make every failure test exact and repeatable. Section 6 lists what I did not build.

{{RESULT_SUMMARY}}

### Context: the problem, the actors and who talks to whom

**Scenario.** The company's coding agent takes a ticket, explores the repository, writes code, runs the tests in a sandbox, opens a pull request, waits for a reviewer and addresses the comments. A real run takes hours of work spread over days. Runs used to fail for a dozen reasons: a node was replaced, the model API returned errors for ten minutes, the agent service was redeployed, a sandbox ran out of disk, a reviewer took the weekend. Each failure started the run again from scratch, paid for the tokens again, and twice opened duplicate pull requests.

Six terms are used throughout.

- **Run.** One agent working on one ticket. It has a session ID, which is also its workflow ID in the engine.
- **Step.** One decision of the agent and its consequence: a model call, then one tool call. The loop repeats until the model says it is done.
- **History.** The engine's record of a run: every step's input and result, every signal, every timer. It is the source of truth.
- **Replay.** Re-running the workflow code against a history. Recorded steps return their recorded results and nothing outside is touched. The code reaches the first step with no result and runs it for real.
- **Side effect.** A change the agent makes outside the platform: a branch, a pull request, a comment.
- **Ledger.** The platform's record of each side effect, written before and after it is made.

![Who starts, steers and fixes a run, and what the platform talks to](diagrams/appendix/a05-1.svg)

**What the picture shows.** Three people touch the platform: the developer who files the ticket, the reviewer who approves, and the on-call engineer who steers runs and ships new code. The platform sits in the middle. Two things outside it cause trouble: the model provider, which fails for minutes at a time, and the Git host, which can make a change and then fail to say so.

**The path of one run.**

1. The session API receives the ticket and starts a workflow. Its ID is the session ID.
2. The workflow asks the model gateway for the next action. The gateway builds nothing: the worker has already rebuilt the prompt from the session database, and the gateway records the call, with a hash of the prompt.
3. The workflow runs the tool the model chose. Files and tests run in a sandbox pod. Branches and pull requests go through the tool gateway to the Git host, with a key that identifies the step.
4. After a step that changed files, the sandbox's files are snapshotted to object storage.
5. At the sixth step the agent reads the reviewer's comment, which needs an approval. The workflow deletes the sandbox and waits for a signal. No pod, worker or sandbox is held.
6. The reviewer approves. The engine wakes the workflow on whichever worker is free. The agent addresses the comment, pushes a comment of its own and finishes.

![One step, a worker that dies, and the replay that follows](diagrams/appendix/a05-4.svg)

**What the picture shows.** One step as calls between the components. Time runs from top to bottom. The red marks are the failure: worker A is killed during a tool step, the engine notices that its heartbeat has stopped, and worker B replays the history and runs only the step that had no result.

| Caller | Callee | Why | What enforces it |
|---|---|---|---|
| Person or product | Session API | Start a run, approve, pause, cancel, rewind, fork, list | The API is the only way in; workers are not addressed directly (S02, S08) |
| Session API | Engine | Start a workflow, signal it, query its state | One workflow per session ID (S02) |
| Engine | Workers | Hand out workflow and activity tasks, enforce timeouts and retries | Task queues; heartbeats; retry policies (S03, S04) |
| Worker | Model gateway | Ask for the next action | A retry policy with backoff, a fallback in the gateway, a token budget per run (S04, S08) |
| Worker | Tool gateway | Run a tool | A ledger row and an idempotency key built from the session and the step (S05) |
| Worker | Sandbox manager, sandbox | Run files and tests; snapshot and restore | One pod per run, restored from the last snapshot (S06) |
| Tool gateway | Git host | Push a branch, open a pull request, comment | Look-before-you-leap, or the host's own key check, or a refusal (S05) |
| KEDA | Engine, then the worker Deployment | Add workers when the queue has a backlog | A ScaledObject on the task queue (S11) |

### 1. The software components I implemented

**Scenario.** The design lists the pieces: a session API, a durable execution engine, stateless workers, sandboxes, state stores, and two gateways. To test it, each must be real software that I can start, call and break. Nothing is left as "assume this exists".

| # | Component | What it is | Where it runs | Reused or written |
|---|---|---|---|---|
| 1 | **EKS Auto Mode cluster** | One Kubernetes 1.37 cluster; AWS runs the control plane and the nodes' operating system. | AWS | Written (Terraform) |
| 2 | **Temporal server 1.32 (Helm chart 1.8.0)** | The durable execution engine: frontend, history, matching and an internal worker, two replicas of each of the first three. | `temporal` | Reused |
| 3 | **RDS for PostgreSQL 16, Multi-AZ** | The engine's database (`temporal`, `temporal_visibility`) and the session database. | AWS | Written (Terraform) |
| 4 | **The agent workflow** | The agent loop as deterministic workflow code: pause, resume, cancel, approve, step and token budgets, loop detection, waiting for a person, continue-as-new to keep histories short. | Workers | Written |
| 5 | **The activities** | Everything that is not deterministic: call the model, run a tool, snapshot the workspace, release the sandbox, compact the context, undo effects. | Workers | Written |
| 6 | **Agent workers** | Poll a task queue for workflow and activity tasks; on SIGTERM they stop taking tasks and let the ones in flight finish. Two Deployments: version 1 (queue `agent-v1`) and version 2 (queue `agent-v2`). | `agent-platform` | Written |
| 7 | **Session API** | Starts runs, sends signals, rewinds and forks, builds the timeline, pauses and resumes every run. | `agent-platform` | Written |
| 8 | **Model gateway** | Retries to a fallback, a token budget per run, and a row in the session database for every call with a hash of its prompt. | `agent-platform` | Written |
| 9 | **Stand-in model** | A function of the prompt that returns the next action of a fixed script. It is deliberately a pure function of the prompt, so a wrongly rebuilt prompt changes the answer and the tests notice. | Inside the model gateway's process | Written |
| 10 | **Tool gateway and ledger** | Nine tools in six classes, a ledger that records each effect before and after, and an undo that walks the ledger newest first. | `agent-platform` | Written |
| 11 | **Sandbox manager and executor** | One pod per run, created from the agent image. The executor in the pod writes files, runs commands, and snapshots to and restores from S3. | `agent-platform`, `sandboxes` | Written |
| 12 | **Stand-in Git host** | Branches, pull requests and comments, honours an `Idempotency-Key`, and can be told to lose its answer to the next N changes of one kind. | `external` | Written |
| 13 | **Context store** | An append-only log of messages and tool results, stored summaries, and the prompt builder that rebuilds the model's input from them. | Session database | Written |
| 14 | **S3 workspace snapshots** | Files stored once under the hash of their bytes, plus one manifest per snapshot. | AWS | Written |
| 15 | **KEDA 2.21.1** | Scales the version 1 workers on the engine's task queue backlog. | `keda` | Reused |
| 16 | **Prometheus (chart 29.47.2)** | Collects each worker's metrics from the pod annotations. | `monitoring` | Reused |
| 17 | **Scenario runner and screenshot renderer** | Runs the tests, records commands and output, redacts private values, draws terminal-style screenshots. | `tests/` | Written |

The Terraform is about 750 lines, the platform's application code about 1,750 lines of Python, and the test code (runner, harness, helpers and screenshot renderer) about 1,250 lines.

**Going deeper.**

- **One image, many roles.** Every component I wrote is the same container image started with a different argument (`python -m agentplatform <role>`). The image holds Python 3.12 and three libraries (the Temporal SDK, a PostgreSQL driver and boto3). It is built for `linux/amd64`, pushed to a private ECR repository and tagged by a hash of the code, so a change in the code is a change of tag and Kubernetes rolls the Deployments by itself.
- **The stand-ins are disclosed, and they are the reason the tests are exact.** The scripted model always writes the same code, opens the same pull request and reads the same review comment. That makes it possible to say "the model was called exactly once for each of the eleven decisions" and to prove it from the gateway's own table, which a real model would not allow.
- **The workflow is small.** The loop, its budgets and its waiting rules are about 200 lines. Everything with side effects is in the activities, which is what the design means by "workflow code decides, activities do".

### 2. EKS topology and instances

**Scenario.** The design asks for the engine to be Multi-AZ and for workers to be replaceable at any moment. The topology has to spread the engine's pods and the workers over zones, put the engine's database in two zones, and keep everything else stateless.

![One EKS cluster in three zones, a Multi-AZ database, and object storage](diagrams/appendix/a05-2.svg)

**What the picture shows.** One VPC, three Availability Zones, one cluster. The engine and workers are spread over the zones. The database has a primary in one zone and a synchronous standby in another.

| Item | What I built | Why |
|---|---|---|
| **Cluster** | One EKS Auto Mode cluster on Kubernetes 1.37. The API is private inside the VPC and public only to the administrator's `/32`. | Auto Mode needs no node groups and buys CPU nodes as pods need them. |
| **Nodes** | The built-in `general-purpose` and `system` pools. No GPU is used: the model is a stand-in. | Everything here is CPU work waiting on I/O. |
| **Engine database** | RDS for PostgreSQL 16, `db.t4g.small`, Multi-AZ, 20 GiB. One instance holds the engine's two databases and the session database. | The design's engine database must be Multi-AZ. Aurora PostgreSQL is what the design names. Multi-AZ RDS gives the same property the tests need (a synchronous standby in another zone, one endpoint name that follows the primary) and is created in about ten minutes. |
| **Object storage** | One private S3 bucket for workspace snapshots. | The design keeps workspace state in object storage. |
| **Image registry** | One private ECR repository. | One image serves every role. |
| **Identity** | One IAM role for the sandbox pods, with read and write on `snapshots/` only, assumed through EKS Pod Identity. The sandbox manager has Kubernetes RBAC to create and delete pods in `sandboxes`. The test runner's credentials are never put in a pod. | A sandbox can write snapshots and nothing else. |
| **Namespaces** | `temporal`, `agent-platform`, `sandboxes`, `external` (the stand-in Git host), `monitoring`, `keda`. | One place for each kind of thing. |

### 3. Architecture

**Scenario.** One map of what runs where and which layer is responsible for what, so that each scenario can say which layer produced the behaviour it shows.

![Design 04 on EKS: the five layers](diagrams/appendix/a05-3.svg)

**What the picture shows.** Clients at the top, the durable execution layer, the gateways and sandboxes, the state, and observability. Only the state layer holds anything that matters after a restart.

![Working state: snapshot after each file change, restore into a new sandbox](diagrams/appendix/a05-6.svg)

**What the picture shows.** The life of a run's files. They are snapshotted after each step that changes them, a lost sandbox is replaced by a new one that restores the last snapshot, and a run that waits for a person holds no sandbox.

![Every tool has a class, and the class decides what a retry may do](diagrams/appendix/a05-5.svg)

**What the picture shows.** The same failure, a lost answer from the outside world, handled differently by each tool class. Where nothing can make a retry safe, the platform stops and asks a person.

![New agent code: replay first, then new runs on new workers, old runs finish on old ones](diagrams/appendix/a05-7.svg)

**What the picture shows.** How a new version of the agent code reaches production without stranding the runs that are waiting.

### 4. How I implemented it

**Scenario.** The design says what to build. This section says how each piece is wired, in the order Terraform applies it.

1. **The cluster layer** creates the VPC with three zones and one NAT gateway, the IAM roles, the Auto Mode cluster, the Multi-AZ database with its parameter group and the ECR repository. It takes about fifteen minutes, most of it the database.
2. **The image** is built and pushed by `scripts/build-image.sh`, which tags it with a hash of `app/`.
3. **The platform layer** then creates the namespaces and the secret that holds the database password, installs Temporal (the chart creates its two databases and their schemas), Prometheus and KEDA, creates the S3 bucket and the sandbox identity, and applies the Deployments: model gateway, tool gateway, sandbox manager, session API, the Git host, the two worker Deployments (version 2 at zero replicas), the tester pod and the paused autoscaler.
4. **The state schema** (context log, summaries, snapshots, model calls, ledger, runs, the Git host's tables) is created by the application itself when a process starts, in one idempotent statement.
5. **Evidence** is recorded by the scenario runner: every command, its real output, and a check computed from that output.

**Going deeper.**

- **Replay-safe by construction.** The workflow code reads no clock, draws no random number and makes no call. The only non-deterministic inputs are the results of activities and the signals it receives. The loop-detection window, the step counter and the budget are plain fields of the workflow object, so they are rebuilt by replay.
- **Idempotent writes everywhere.** The context log has a primary key of (session, step, role), and a retried step writes the same row again, which changes nothing. A summary is unique per (session, last step). A snapshot is keyed by (session, step). The ledger key is `session:step`.
- **A step's key never changes between retries.** The tool gateway builds its key from the session and the step number, which the workflow passes to the activity. The attempt number is not part of it.
- **Continue-as-new every five steps in the tests.** A real run of thousands of steps would exceed the engine's history limits, so the workflow ends its history and starts a fresh one that carries only the session, the step, the snapshot id, the token count and the loop-detection window. The context stays in the database, and S02 shows the chain of runs behind one session.
- **Pause is checked between steps, not inside them.** A signal is received at once, but the workflow honours it when it next reaches the top of its loop, so no tool is cut off half way. S08 shows this.
- **Worker versioning with queues.** There is no separate engine feature involved: version 2 workers poll a different task queue, and the session API's "current queue" decides where new runs start. A run is pinned to the queue it started on.

**Where reality differed from the design.** These are findings, confirmed on the live cluster unless I say otherwise.

- **The database is not set up for TLS.** Temporal's server connects to PostgreSQL without TLS in this build, so the instance's parameter group turns off the requirement to use it (`rds.force_ssl` is 0). The database sits in private subnets and accepts connections only from inside the VPC. A production build should switch TLS on in the engine's configuration, and I did not test that.
- **On a fresh build, the engine's pods restart a few times before they settle.** I set the chart's `useHelmHooks` to false for its schema and its default namespace, so the schema job and the namespace job run as ordinary Kubernetes Jobs next to the servers. On the fresh build used for the final test run, the frontend, history, matching and internal worker pods each showed three restarts in their first four minutes, and both jobs showed `Completed`. I take the restarts to be the servers starting before the schema job had finished, but I did not read their logs to confirm it. The platform is ready about four minutes after the engine's release is applied.
- **A test tool that said "passed" when the tests did not run.** The first version of the run-tests tool piped the test output through `tail`, so its exit code was `tail`'s, always zero. S06 showed it: after a sandbox was restored, the tool reported success while the output said the tests directory could not be imported. I fixed the tool to keep the test runner's exit code and to name the test module, and S06's check now needs the output "Ran 2 tests ... OK". This is exactly the kind of green result the tests exist to catch.
- **One replica of the session API was a single point of failure for the tests.** During the first S11 run, Auto Mode replaced a node and the pods on it, and the start of 24 runs found no session API for about 30 seconds. I did not capture the pod logs, so the link to the node replacement is from the cluster's events and the pods' ages. The session API and the sandbox manager now run two replicas each, and the load generator retries a failed start.
- **A scale test needs the workers to be small.** Two workers with 32 activity slots each can hold 64 steps at once, and 24 runs never made a backlog, so the autoscaler would never have moved. I made the slot count a setting and ran the workers with 6, so 24 runs exceed 12. The ratio is what matters.
- **A forced database failover took 6 to 10 minutes, and the runs paused for all of it.** In three runs of S10 the database was back, with the standby in the other zone as primary, after 387, 469 and 580 seconds, measured from the reboot command until the API reported it available in the new zone. While it was down the engine could process nothing, including the timers that notice a dead worker, so a tool step that was in flight when the database went away was marked as timed out only after the database returned, and was then run again. In my first full run of S10 I had given the runs 10 minutes to reach the review step, and one run needed a few seconds more, so the check failed. I then raised the limit to 20 minutes and ran S10 again. The runs themselves needed no help. The design says "short pause of all runs"; with this instance size and a forced failover, plan for minutes.
- **A model call that is in flight when its worker dies is paid for twice.** This is inherent: the gateway has already received the call. S07 shows two rows with the same prompt hash. The design's claim is that finished steps are not paid for again, and that holds.
- **The Auto Mode node replacement can disturb a long test.** Auto Mode consolidated empty nodes while scenarios ran. Everything that was disturbed was either stateless or recovered, which is the property under test, but a scenario that depends on a particular pod staying alive has to be written to tolerate it.

### 5. The scenarios I tested

**Scenario.** The design makes specific promises. I turned each into a scenario that runs real commands against the live cluster and decides pass or fail from the output, with no human judgement in the loop.

**How to read this section.**

- Commands starting with `kubectl` and `aws` run against the cluster and the account. Commands starting with `agentctl` run the platform's own command line, inside the cluster, through the same session API a person's tool would use.
- Lines starting with a `SELECT` are queries against the session database. Lines starting with "Prometheus:" are queries against the cluster's Prometheus. The line says where each result came from.
- Timings depend on what the cluster was doing at that moment, so each check is written to hold whatever the timing was.


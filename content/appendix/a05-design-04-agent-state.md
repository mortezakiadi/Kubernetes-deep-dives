---
id: appendix-design-04-eks
title: "Appendix E: Design 04 built and tested on Amazon EKS"
short: "E. Design 04 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 04: State management for long-running agents](#design-agent-state). I built it with Terraform on Amazon EKS in one AWS account: a durable execution engine (Temporal) on a Multi-AZ PostgreSQL database, stateless agent workers that scale on the engine's queue, a model gateway and a tool gateway with a side-effect ledger, a sandbox manager with content-addressed workspace snapshots, and a session API for starting, steering, rewinding and forking runs.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design describes runs of hours or days that span a weekend. The runs in this build last under two minutes, with test steps of 2 to 100 seconds, so a "long wait" is minutes and not days. Nothing in the mechanism depends on that: a run waiting for a reviewer is a row in the engine's database whether it waits for one minute or one weekend, and S06 shows that nothing is held while it waits. The agent's brain is also scaled down. The model is a scripted stand-in that answers as a fixed function of its prompt, and the Git host is a small service I wrote. The design is about what happens around the model and around the outside world, and a scripted model and a Git host that can be told to lose its answer make every failure test exact and repeatable. Section 6 lists what I did not build.

11 of 11 scenarios passed, 97 of 97 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The platform at rest: engine, stores, workers and gateways | 9 of 9 |
| S02 | One run through the five kinds of state | 9 of 9 |
| S03 | A worker dies in the middle of a step | 7 of 7 |
| S04 | The model provider fails | 10 of 10 |
| S05 | Side effects happen once | 10 of 10 |
| S06 | A sandbox is lost, and waiting costs nothing | 9 of 9 |
| S07 | Context is rebuilt from the log, never remembered | 7 of 7 |
| S08 | Pause, budgets, loops, cancel, rewind and fork | 13 of 13 |
| S09 | Deploying new code while runs are in flight | 6 of 6 |
| S10 | The engine's database fails over, and the engine restarts | 7 of 7 |
| S11 | Many runs at once, scaled by the queue, and what an operator can see | 10 of 10 |

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


### S01: The platform at rest: engine, stores, workers and gateways

**Design reference:** The architecture at a glance.

**What this tests.** Show what was built: the durable execution engine and its Multi-AZ database, the stateless workers, the gateways, the sandbox manager and the state stores, with nothing held by any run.

Before testing behaviour I check that the platform is what the design says: an engine on a Multi-AZ database, stateless workers, gateways, a sandbox manager, and no state kept on any disk or in any run's name while nobody is working.

![S01: The platform at rest: engine, stores, workers and gateways](diagrams/appendix/a05-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws eks describe-cluster --name ds04-agentstate --query 'cluster.[name,status,version]' --output text
ds04-agentstate	ACTIVE	1.37

$ aws rds describe-db-instances --db-instance-identifier ds04-agentstate-db --query 'DBInstances[0].[Status,MultiAZ,Engine,Version,Class,AZ,StandbyAZ]'
available	True	postgres	16.13	db.t4g.small	us-east-1a	us-east-1c

$ kubectl -n temporal get pods   # the durable execution engine
NAME                                   READY   STATUS      RESTARTS        AGE
temporal-admintools-7b8b74754b-cw2m5   1/1     Running     0               4m39s
temporal-frontend-f4659cb87-5cx9n      1/1     Running     3 (3m55s ago)   4m39s
temporal-frontend-f4659cb87-lssw8      1/1     Running     3 (3m55s ago)   4m39s
temporal-history-6bf4786d97-hn5q8      1/1     Running     3 (3m55s ago)   4m39s
temporal-history-6bf4786d97-ks65v      1/1     Running     3 (3m55s ago)   4m39s
temporal-matching-554978f4c4-7pxxg     1/1     Running     3 (3m58s ago)   4m39s
temporal-matching-554978f4c4-mfs2g     1/1     Running     3 (3m57s ago)   4m39s
temporal-namespace-1-8-0-1-l6dh8       0/1     Completed   0               4m39s
temporal-schema-1-8-0-1-4rr8f          0/1     Completed   0               4m39s
temporal-web-7477fff4bd-v94r2          1/1     Running     0               4m39s
temporal-worker-778f9d457-7rjph        1/1     Running     3 (3m58s ago)   4m39s

$ kubectl -n temporal exec deploy/temporal-admintools -- temporal operator cluster describe
ClusterName  PersistenceStore  VisibilityStore
  active       postgres12        postgres12

$ kubectl -n agent-platform get pods -o wide   # workers, gateways, session API, sandbox manager
NAME                              READY   STATUS    RESTARTS        AGE     IP            NODE                  NOMINATED NODE   READINESS GATES
model-gateway-55b48f45c5-2ksrv    1/1     Running   0               4m52s   10.32.46.88   i-0c185ecefeffae23b   <none>           <none>
model-gateway-55b48f45c5-6rv4k    1/1     Running   0               4m52s   10.32.34.58   i-0d6f5e1a55de3ac50   <none>           <none>
sandbox-manager-b974f68bc-pfmxz   1/1     Running   0               4m52s   10.32.34.57   i-0d6f5e1a55de3ac50   <none>           <none>
sandbox-manager-b974f68bc-zhggq   1/1     Running   0               4m52s   10.32.46.90   i-0c185ecefeffae23b   <none>           <none>
session-api-668bdd96ff-hqbrn      1/1     Running   0               4m52s   10.32.34.54   i-0d6f5e1a55de3ac50   <none>           <none>
session-api-668bdd96ff-mx24l      1/1     Running   0               4m52s   10.32.46.89   i-0c185ecefeffae23b   <none>           <none>
tester-7cffb5d865-tpvl7           1/1     Running   0               4m52s   10.32.34.59   i-0d6f5e1a55de3ac50   <none>           <none>
tool-gateway-6ffbddd965-f8dcp     1/1     Running   0               4m52s   10.32.46.91   i-0c185ecefeffae23b   <none>           <none>
tool-gateway-6ffbddd965-n9pps     1/1     Running   0               4m52s   10.32.34.56   i-0d6f5e1a55de3ac50   <none>           <none>
worker-v1-79d5c699b8-8jkvr        1/1     Running   1 (3m28s ago)   3m32s   10.32.40.32   i-0c185ecefeffae23b   <none>           <none>
worker-v1-79d5c699b8-wwzpg        1/1     Running   1 (3m28s ago)   3m32s   10.32.46.80   i-0c185ecefeffae23b   <none>           <none>

$ kubectl -n external get pods   # the stand-in for the external system the agent changes
NAME                        READY   STATUS    RESTARTS   AGE
git-host-7758648877-p7fk7   1/1     Running   0          4m53s

$ kubectl get pvc -A   # does any component keep state on a disk?

No resources found

$ agentctl classes   # how each tool is made safe to retry (tool gateway registry)
list_dir             local
write_file           local
run_tests            local
read_review          read_only
push_branch          checkable
open_pr              checkable
open_pr_naive        naive
comment_on_pr        idempotent
delete_branch_prod   unsafe

$ kubectl -n sandboxes get pods   # sandboxes at rest

No resources found in sandboxes namespace.

$ kubectl -n agent-platform get scaledobject   # workers scale on the engine's task queue backlog
NAME        SCALETARGETKIND   SCALETARGETNAME   MIN   MAX   READY   ACTIVE    FALLBACK   PAUSED   TRIGGERS   AUTHENTICATIONS   AGE
worker-v1                     worker-v1         2     8     True    Unknown   False      True                                  3m34s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the cluster is ACTIVE on Kubernetes 1.37 | Terraform created one EKS Auto Mode cluster at the pinned minor version. |
| PASS | the database is available, Multi-AZ, with a standby in a different zone | One PostgreSQL instance holds the engine's histories and the session database. RDS keeps a synchronous standby in another zone. |
| PASS | the engine runs two frontend, two history and two matching pods, and one worker service pod | Temporal's chart is installed with two replicas of each stateful service, so losing one pod is survivable (S10). |
| PASS | the engine reports itself serving, on PostgreSQL for both histories and search | The server answers on its own API and names its two stores. |
| PASS | two stateless workers, and two each of the model gateway, tool gateway, session API and sandbox manager, are ready | Each is a Deployment. Workers hold no run; the gateways keep their state in the database. |
| PASS | no component keeps state on a volume | Everything a run needs is in the engine's database, the session database or S3. Replacing a pod loses nothing. |
| PASS | every tool is classified: local, read-only, idempotent, checkable or unsafe | The class is part of a tool's registration in the gateway. It decides how a retry of the same step is made safe (S05). |
| PASS | no sandbox exists while no run needs one | A sandbox is created for a step that needs files and deleted while a run waits. |
| PASS | the workers' autoscaler watches the engine's task queue, not CPU | KEDA's Temporal scaler reads each task queue's backlog from the engine (S11). |

**Why you can trust this result.**

- The cluster and the database are described by the AWS APIs, not by my Terraform files. The standby's zone is read from the database itself.
- The engine describes itself through its own administration tool, and its pods are counted from the Deployments' status.
- 'No state on a disk' is the answer of the cluster to a request for every persistent volume claim. 'No sandbox' is the answer for the sandboxes namespace.

**Why you get this output.**

- The engine has two copies of each stateful service because the chart is installed with two replicas of each, spread by the scheduler over the zones.
- There is no volume because every component I wrote keeps its state in the database or S3. The engine itself keeps its state in PostgreSQL.
- There is no sandbox because a sandbox is created only for a step that needs files, and deleted when a run waits or ends.

### S02: One run through the five kinds of state

**Design reference:** Five kinds of agent state; The architecture at a glance.

**What this tests.** Run a coding agent from ticket to finished pull request, stopping where it waits for a reviewer, and find each kind of its state in the place the design says it lives.

The design says an agent's state is five different things. I run one agent from a ticket to a finished pull request, stop it where it waits for a reviewer, and look for each of the five kinds in the place the design says it lives. The run is set to end its engine history every five steps, so the first kind can be seen too.

![S02: One run through the five kinds of state](diagrams/appendix/a05-evidence/S02.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl start --task '{"ticket": "T-1", "test_seconds": 3, "can_every": 5}'
session s-70937ac5

$ agentctl status s-70937ac5   # the run waits for a reviewer
state=waiting  step=6  workflow=RUNNING  history_events=29  tokens=2401

$ kubectl -n sandboxes get pods   # what a waiting run holds

No resources found in sandboxes namespace.

$ agentctl chain s-70937ac5   # execution state: the engine's runs for this session
run 01a1fa53  continued_as_new 77 events
run 76045b9f  running    29 events

$ SELECT role, COUNT(*), SUM(tokens) FROM context_log WHERE session = <this run> GROUP BY role   # context state
assistant    7 entries  345 tokens
tool         6 entries  129 tokens
user         1 entries  15 tokens

$ SELECT step, snapshot_id, files, uploaded, skipped FROM snapshots   # working state: one snapshot per file-changing step
step  snapshot          files uploaded skipped
   1  ea2dd70be369bc00      1        1       0
   2  61bd833d4d74fa72      2        1       1

$ aws s3 ls s3://<bucket>/snapshots/ --recursive   # the manifests and file objects in object storage
snapshots/manifests/61bd833d4d74fa72.json  179 bytes
snapshots/manifests/ea2dd70be369bc00.json  86 bytes
snapshots/objects/43fcd682f9a0e47de98c620a4b3e1aa1f26ead06a49017f8c0767440f6cfef85  159 bytes
snapshots/objects/b6caf2587f75ea04ad56a8ec1acbc5702f4f775acf50c2f67f16a2985b6ef072  251 bytes

$ agentctl ledger s-70937ac5   # external effects: the side-effect ledger
step tool            class       status       attempts deduped  external
   4 push_branch     checkable   done                1       0  agent/s-70937ac5-4
   5 open_pr         checkable   done                1       0  1

$ agentctl git counts   # what the external system has
{"open_prs": 1, "all_prs": 1, "branches": 1, "comments": 0}

$ # process memory: variables in a worker, open connections
Not saved anywhere. Nothing a later step needs lives only in a worker's memory: the next step rebuilds its prompt from the database and
takes its position from the history.

$ agentctl calls s-70937ac5   # every call the model gateway received for this run
step kind    backend   status prompt_sha
   0 act     primary     200  36a08ed8e84c4b55
   1 act     primary     200  289d38eeb1926d04
   2 act     primary     200  f1c7e8e3021bdcda
   3 act     primary     200  fc7debbad911ab57
   4 act     primary     200  ee4623ceb447a6b0
   5 act     primary     200  176482f0ba1857af
   6 act     primary     200  8a196e17437e5aff
   7 act     primary     200  9dff67150ed4eb5d
   8 act     primary     200  006618aa6064b109
   9 act     primary     200  3fbbfe1e9040b0bd
  10 act     primary     200  4a0470dcfc1b890f

$ agentctl timeline s-70937ac5   # the run's last history: activity by activity
t=   0.3s  call_model          attempts=1
t=   4.5s  run_tool            attempts=1
t=   4.7s  call_model          attempts=1
t=   4.8s  run_tool            attempts=1
t=   5.1s  snapshot_workspace  attempts=1
t=   5.3s  call_model          attempts=1
t=   5.4s  run_tool            attempts=1
t=   5.6s  snapshot_workspace  attempts=1
t=   5.8s  call_model          attempts=1
t=   9.0s  run_tool            attempts=1
t=   9.2s  call_model          attempts=1
t=   9.4s  run_tool            attempts=1
t=   9.5s  continued as new (history cut)
t=   9.7s  call_model          attempts=1
t=   9.9s  run_tool            attempts=1
t=  10.1s  call_model          attempts=1
… (12 more lines in the screenshot and in evidence/S02.txt)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the run reached the review step and is waiting for a person | The agent decided to read the reviewer's comment, which needs approval, so the workflow waits on a signal. |
| PASS | while it waits, the run holds no sandbox | The workflow deleted the sandbox after the last snapshot and before waiting. |
| PASS | the engine holds the run's history, and kept it short by continuing as new | Every five steps the workflow ends its history and starts a fresh one with only the session, the step and the snapshot. The context stays in the database. |
| PASS | the context log holds the ticket, the model's answers and the tools' results | Each step appends to an append-only log in the session database. Prompts are rebuilt from it. |
| PASS | each file-changing step left a snapshot in S3, and a file that did not change was not uploaded again | A snapshot is a manifest of file hashes, and a file is uploaded only if its hash is new. The second snapshot had two files and skipped the one the first already stored. (If an earlier run stored the same bytes, nothing is uploaded at all.) |
| PASS | the branch and the pull request are in the ledger and exist once in the external system | The tool gateway records each external effect before and after making it. |
| PASS | the run completed after the reviewer's approval | The approval is a signal to the waiting workflow. It continued from the recorded state. |
| PASS | the model was called exactly once for each of the eleven decisions | One recorded activity per decision. Continuing as new twice did not repeat any. |
| PASS | one pull request, one branch, and two comments (the reviewer's and the agent's) exist | Each effect happened once. |

**Why you can trust this result.**

- Every number comes from the store it describes: the run's runs and their lengths from the engine, the context from the session database, the snapshots from the database and from S3's own listing, the external effects from the ledger and from the Git host's own counters.
- 'The run holds nothing while it waits' is the cluster's answer for the sandboxes namespace at the moment the run reports that it is waiting.
- The count of model calls is the gateway's table, written by the gateway on every call. The worker cannot affect it.

**Why you get this output.**

- The history is short because the workflow ends and restarts itself every five steps, carrying only the session, the step, the last snapshot and the token count.
- The context is in the database because every step appends to an append-only log, and a prompt is rebuilt from the log whenever it is needed.
- The second snapshot uploaded only one file because snapshots are content-addressed: a file is stored under the hash of its bytes, and the manifest lists hashes.
- The branch and the pull request are in the ledger because the tool gateway writes a row before the change and updates it after.
- Process memory is the fifth kind and nothing is saved: the workflow and the activities keep nothing between steps that is not in one of the four stores.

### S03: A worker dies in the middle of a step

**Design reference:** Steps, history and replay; Failure modes.

**What this tests.** Kill, with no warning, the worker that is running a long step. Another worker must take the run over, the finished steps must not be paid for again, and then take away every worker at once.

The most common failure in the design's story is a worker that disappears in the middle of a step. I kill the worker that is running the test step, with no warning, and watch what the engine does. Then I take away every worker, mid-step, and bring two new ones back.

![S03: A worker dies in the middle of a step](diagrams/appendix/a05-evidence/S03.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl start --task '{"ticket": "T-1", "test_seconds": 40}'
session s-390c684b

$ agentctl status s-390c684b   # step 3 (the test run) is in flight, on one worker
state=running  step=3  workflow=RUNNING  history_events=59  tokens=886  pending=[run_tool#1]

$ the worker running it, from the engine's own record
worker-v1-79d5c699b8-wwzpg

$ kubectl -n agent-platform delete pod worker-v1-79d5c699b8-wwzpg --grace-period=0 --force   # SIGKILL: no warning, no cleanup
pod "worker-v1-79d5c699b8-wwzpg" force deleted from agent-platform namespace

$ agentctl status s-390c684b
state=waiting  step=6  workflow=RUNNING  history_events=101  tokens=2378

$ time from the kill to the run reaching the review step
62 seconds (the rest of the 40-second test, plus the detection delay)

$ agentctl timeline s-390c684b
t=   0.2s  call_model          attempts=1
t=   4.4s  run_tool            attempts=1
t=   4.6s  call_model          attempts=1
t=   4.7s  run_tool            attempts=1
t=   5.2s  call_model          attempts=1
t=   5.3s  run_tool            attempts=1
t=   5.7s  call_model          attempts=1
t=  71.0s  run_tool            attempts=2   last failure: activity Heartbeat timeout
t=  71.2s  call_model          attempts=1
t=  71.4s  run_tool            attempts=1
t=  71.6s  call_model          attempts=1
t=  71.8s  run_tool            attempts=1
t=  72.0s  call_model          attempts=1
model calls: 7   tokens: 2378

$ agentctl calls s-390c684b   # the model gateway's record
step kind    backend   status prompt_sha
   0 act     primary     200  39961bc1450694e8
   1 act     primary     200  5bf4e46d585ebb4a
   2 act     primary     200  79a23ae580cfbe33
   3 act     primary     200  cc7c76ee056c033e
   4 act     primary     200  473aa94ad2f5a46d
   5 act     primary     200  fee0e73ce52832ca
   6 act     primary     200  2fda654c05e0ab7f

$ agentctl start --task '{"ticket": "T-2", "test_seconds": 25}'
session s-cebdb060

$ kubectl -n agent-platform scale deploy/worker-v1 --replicas=0   # every worker gone, mid-step
deployment.apps/worker-v1 scaled

$ kubectl -n agent-platform get pods -l app=worker-v1
NAME                         READY   STATUS        RESTARTS   AGE
worker-v1-79d5c699b8-sqqlr   1/1     Terminating   0          2m26s

$ agentctl status s-cebdb060   # with no worker at all, for 45 seconds
state=no-worker  step=None  workflow=RUNNING  history_events=64  tokens=None

$ kubectl -n agent-platform get pods -l app=worker-v1   # two new workers, in new pods
NAME                         READY   STATUS    RESTARTS   AGE
worker-v1-79d5c699b8-b5cnp   1/1     Running   0          7s
worker-v1-79d5c699b8-lvs9d   1/1     Running   0          7s

$ agentctl status s-cebdb060
state=waiting  step=6  workflow=RUNNING  history_events=103  tokens=2378

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the test step is running on a worker, in flight | The engine tracks which worker last reported on each pending activity. |
| PASS | the run carried on to the review step on another worker, with nobody touching it | The engine noticed the activity's heartbeat had stopped, and scheduled it again for another worker. |
| PASS | the interrupted step shows a second attempt in the history | The history records which attempt finally completed, and why the earlier one failed. |
| PASS | no model call was repeated: one call for each of steps 0 to 6 | Replay returns the recorded answer for every step that finished. Only the unfinished tool step ran again. |
| PASS | with no worker running, the run still exists in the engine | The run is a row of history in PostgreSQL, not a process. |
| PASS | two brand-new workers took the run over and carried it to the review step | Any worker can continue any run: nothing the run needed was in the workers that were removed. |
| PASS | the model was still called exactly once per step for that run | The new workers replayed the history without calling the model. |

**Why you can trust this result.**

- The worker that is running the step is named by the engine's own record of the pending activity, and I kill that pod by name.
- The second attempt is read from the history the engine keeps, not from my logs. The model calls are counted in the gateway's table: a repeated call would be a second row for the same step.
- 'With no worker, the run still exists' is the engine's answer to a describe request, made while no worker pod exists.

**Why you get this output.**

- The engine notices because a running activity must heartbeat. When the heartbeats stop and the timeout passes, the attempt fails and is scheduled again for any worker.
- No model call is repeated because the new worker replays the history. Steps with a recorded result return it, and only the step with no result runs.
- The run survives with no worker because a run is rows in the engine's database. A worker is only something that polls for tasks.

### S04: The model provider fails

**Design reference:** Failure modes; Gateways.

**What this tests.** Make the primary model provider unavailable. A run with a fallback must carry on through the fallback. A run without one must retry with backoff and recover on its own. And an operator must be able to pause every run, wait out an incident with no model call made, and resume them at a limited rate.

The model provider fails for minutes at a time. I make the primary provider unavailable and run three situations: a run that may use a fallback, a run that may not and must wait, and five runs paused by an operator for the length of the incident and then released at a limited rate.

![S04: The model provider fails](diagrams/appendix/a05-evidence/S04.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl model-fault --seconds 600   # the primary provider is down for ten minutes
{
 "primary_down_for_s": 600.0,
 "latency_s": "0.0"
}

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'   # fallback allowed (the default)
session s-8d70f730

$ agentctl calls s-8d70f730   # which backend served each call
step kind    backend   status prompt_sha
   0 act     fallback    200  0bd8420ee75f7cd0
   1 act     fallback    200  23754b794bc2fe67
   2 act     fallback    200  644f6ed9476296d8
   3 act     fallback    200  afd162841618fb17
   4 act     fallback    200  7451f614f8fabcd0
   5 act     fallback    200  3ff2d37e5a9c802d
   6 act     fallback    200  fc466d26622ddffd

$ agentctl model-fault --seconds 25   # primary down for 25 seconds only
{
 "primary_down_for_s": 25.0,
 "latency_s": "0.0"
}

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2, "allow_fallback": false}'   # no fallback allowed
session s-219a0b56

$ agentctl calls s-219a0b56   # refused calls, then the one that worked
step kind    backend   status prompt_sha
   0 act     primary     503  cef6ec2c361bd697
   0 act     primary     503  cef6ec2c361bd697
   0 act     primary     503  cef6ec2c361bd697
   0 act     primary     503  cef6ec2c361bd697
   0 act     primary     200  cef6ec2c361bd697
   1 act     primary     200  3933e2ef28049d2c
   2 act     primary     200  e4be97a3e0d14f35
   3 act     primary     200  4598ab39dbde450b
   4 act     primary     200  108dfc6946d0d9ba
   5 act     primary     200  0efff4d64dd86078
   6 act     primary     200  d7c6aa559b3cdf56

$ agentctl timeline s-219a0b56   # the engine's record of the retried call
t=  30.7s  call_model          attempts=5   last failure: model gateway 503: primary model provider unavailable
t=  34.1s  call_model          attempts=1
t=  34.7s  call_model          attempts=1
t=  35.2s  call_model          attempts=1
t=  37.6s  call_model          attempts=1
t=  38.0s  call_model          attempts=1
t=  38.4s  call_model          attempts=1
model calls: 7   tokens: 2417

$ agentctl start ...   # five runs in flight, each running its tests
s-3cefce4b
s-1b03c1d1
s-aacf4ad4
s-6a89b0f9
s-dbdff8c7

$ agentctl model-fault --seconds 900   # the provider goes down again
{
 "primary_down_for_s": 900.0,
 "latency_s": "0.0"
}

$ agentctl ops pause-all   # the operator pauses every running run
{
 "paused": 5,
 "sessions": [
  "s-dbdff8c7",
  "s-6a89b0f9",
  "s-aacf4ad4",
  "s-1b03c1d1",
  "s-3cefce4b"
 ]
}

$ agentctl status <each run>
s-3cefce4b  state=paused  step=4  workflow=RUNNING  history_events=68  tokens=886
s-1b03c1d1  state=paused  step=1  workflow=RUNNING  history_events=20  tokens=70
s-aacf4ad4  state=paused  step=1  workflow=RUNNING  history_events=20  tokens=70
s-6a89b0f9  state=paused  step=1  workflow=RUNNING  history_events=20  tokens=70
s-dbdff8c7  state=paused  step=1  workflow=RUNNING  history_events=20  tokens=70

$ SELECT COUNT(*) FROM model_calls WHERE session IN (the five runs)   # now, and 25 seconds later
now: 8
25 seconds later: 8

$ agentctl model-fault --seconds 0   # the provider is back
{
 "primary_down_for_s": 0.0,
 "latency_s": "0.0"
}

$ agentctl ops resume-all --rate 1   # resume them one per second
{
 "resumed": 5,
 "seconds": 5.2,
 "per_second": 1.0
}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | with the primary down, the run reached the review step through the fallback | The gateway sends the call to the fallback model when the primary does not answer. The run never saw an error. |
| PASS | not one call was refused | Every row the gateway recorded for the run is a success. |
| PASS | the first step was refused several times before it succeeded | A refused call is a failed activity. The engine runs it again after 2, 4, 8 seconds and so on, until the provider answers. |
| PASS | the retries were spaced out, not a burst | The run needed 38 seconds to get past step 0, which is how long the provider was down. |
| PASS | the history shows the model call taking more than one attempt | The attempt count and the last failure are kept in the history, so an operator can see why a step was slow. |
| PASS | all five runs were signalled | One call to the session API signals every running workflow. |
| PASS | each run finished the step it was on and then stopped | A pause is checked between steps, so no step is left half done. |
| PASS | while paused, the provider received no call from any of them | A paused run is waiting on a signal, so nothing calls the gateway: the incident is not made worse by the retries. |
| PASS | the runs were resumed one per second, not all at once | Five runs at one per second took 5.2 seconds to release. |
| PASS | every run then carried on to the review step | They continued from the step they had paused at. |

**Why you can trust this result.**

- Every call the gateway receives is a row in its table with the backend that served it and the status it returned. The checks read that table.
- The provider's failure is switched on and off in the gateway by a command, so the start and end of the incident are known exactly.
- 'No call while paused' compares the number of rows in the table for the five runs before and after a 25-second wait.

**Why you get this output.**

- The first run never saw an error because the gateway sends the call to the fallback when the primary does not answer.
- The second run was slow and then fine because a refused call is a failed activity, and the engine retries it with backoff (2, 4, 8 seconds and on) until the provider answers. The attempts and the last failure are in the history.
- Paused runs make no calls because a pause is a signal that the workflow honours at the top of its loop, where it waits. Nothing retries against a provider that is down.
- The resume took five seconds because the operator command releases one run a second. A thousand runs released at once would hit the provider together.

### S05: Side effects happen once

**Design reference:** Side effects: exactly once where it matters.

**What this tests.** Make the external Git host lose its answer after it has already made a change, which is the case a retry cannot tell from a failure. Show first what an unprotected tool does, then the same fault against the protected tools, then a tool that cannot be protected.

A retry cannot tell 'it failed' from 'it worked but the answer was lost'. I make the Git host lose its answer after it has made a change, which is the cause of the duplicate pull requests in the design's story. First an unprotected tool as a control, then the protected tools with every kind of change losing its answer once, then a tool that cannot be protected.

![S05: Side effects happen once](diagrams/appendix/a05-evidence/S05.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl classes   # how each tool's retries are made safe
open_pr              checkable
push_branch          checkable
comment_on_pr        idempotent
list_dir             local
run_tests            local
write_file           local
open_pr_naive        naive
read_review          read_only
delete_branch_prod   unsafe

$ agentctl git ack-loss:pulls 1   # the Git host will lose its answer to the next pull request it opens
{
 "ack_loss": {
  "pulls": 1
 }
}

$ agentctl start --task '{... "naive_pr": true}'   # control: the agent uses a tool with no protection
session s-2bb9151d

$ agentctl git counts
{
 "open_prs": 2,
 "all_prs": 2,
 "branches": 1,
 "comments": 0
}

$ agentctl ledger s-2bb9151d
step tool            class       status       attempts deduped  external
   4 push_branch     checkable   done                1       0  agent/s-2bb9151d-4
   5 open_pr_naive   naive       done                2       0  2

$ agentctl git ack-loss:branches 1
{
 "ack_loss": {
  "branches": 1
 }
}

$ agentctl git ack-loss:pulls 1
{
 "ack_loss": {
  "pulls": 1
 }
}

$ agentctl git ack-loss:comments 1
{
 "ack_loss": {
  "comments": 1
 }
}

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'   # the same task, protected tools
session s-f31e1eed

$ agentctl ledger s-f31e1eed   # attempts 2 and deduplicated 1: the retry found the work already done
step tool            class       status       attempts deduped  external
   4 push_branch     checkable   done                2       1  agent/s-f31e1eed-4
   5 open_pr         checkable   done                2       1  1
   9 comment_on_pr   idempotent  done                2       0  2

$ agentctl git counts
{
 "open_prs": 1,
 "all_prs": 1,
 "branches": 1,
 "comments": 2
}

$ agentctl git ack-loss:deletes 1
{
 "ack_loss": {
  "deletes": 1
 }
}

$ # a production branch exists, and the agent has been given a tool that deletes it
branch agent/s05-unsafe-4 created (1 branch on the Git host)

$ agentctl start --task '{... "behavior": "unsafe"}' --session s05-unsafe
session s05-unsafe

$ agentctl status s05-unsafe
state=needs_attention  step=4  workflow=RUNNING  history_events=76  tokens=1352  attention='delete_branch_prod may have changed the world and cannot be checked'

$ agentctl ledger s05-unsafe
step tool            class       status       attempts deduped  external
   4 delete_branch_prod unsafe      started             2       0  None

$ agentctl git counts
{"open_prs": 0, "all_prs": 0, "branches": 0, "comments": 0}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | control: the unprotected tool opened two pull requests for one decision | The first request made the pull request but its answer was lost. The retry could not know, and made another. |
| PASS | control: the ledger saw both attempts but had no way to stop the second | A ledger only helps if the tool's class gives it a way to check. |
| PASS | the run completed despite three lost answers | Each lost answer was retried and each retry was made safe. |
| PASS | one branch, one pull request and two comments exist (the reviewer's and the agent's) | Nothing was duplicated, although each of the three changes was attempted twice. |
| PASS | the branch and the pull request were found by looking first | Checkable tools ask the outside world 'is it already there?' before repeating a change. |
| PASS | the comment was deduplicated by the Git host, through the key | The retry carried the same Idempotency-Key as the first attempt, because the key is built from the session and the step, not the attempt. |
| PASS | the run stopped and asked for a person instead of repeating the delete | The tool is classed 'unsafe': it cannot be keyed and cannot be looked up, so a second attempt after an unknown outcome is refused. |
| PASS | it was attempted twice and not a third time | The ledger counts attempts: the first made the change, the second was stopped before reaching the Git host. |
| PASS | the delete did happen once, as the first attempt's lost answer suggested | The platform cannot undo it, so it says what it knows and waits. |
| PASS | a person ended the run and nothing else was changed | Cancel runs the compensations for what the ledger shows was done, which here is nothing. |

**Why you can trust this result.**

- The Git host is the external system and it counts what it holds. A duplicate shows as a count, not as a log line.
- The lost answer is not simulated in the platform: the host performs the change, commits it, and then answers with an error, so the tool gateway sees exactly what it would see with a real timeout.
- The control is in the result on purpose. The same fault against an unprotected tool opens two pull requests, so the protected result is not an artefact of a fault that never happened.
- The ledger and the host agree: the ledger's attempt counts and deduplication counts are read from the database, and the host's counts are read from the host.

**Why you get this output.**

- The branch and the pull request were not duplicated because their tools are 'checkable': on a second attempt the gateway first asks the host whether the effect is already there, and uses it if so.
- The comment was not duplicated because it is 'idempotent': the second attempt carries the same key as the first, and the host returns the first comment.
- The key is the same because it is built from the session and the step, never from the attempt.
- The run that deletes a branch stopped because that tool is 'unsafe': it cannot be keyed or checked, so a second attempt after an unknown outcome is refused and the workflow moves the run to needs attention.

### S06: A sandbox is lost, and waiting costs nothing

**Design reference:** Working state; Waiting for a person.

**What this tests.** Delete a run's sandbox while its tests are running, and see the files come back from the last snapshot. Then leave a run waiting for a reviewer while every worker is removed, approve it with nothing running, and see it carry on when workers return.

A sandbox can disappear at any time, with its disk. I delete a run's sandbox while its tests are running and see whether the files come back from the last snapshot. Then I leave a run waiting for a reviewer, remove every worker, approve with nothing running, and bring workers back.

![S06: A sandbox is lost, and waiting costs nothing](diagrams/appendix/a05-evidence/S06.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl start --task '{"ticket": "T-1", "test_seconds": 45}'
session s-3768c2c7

$ kubectl -n sandboxes get pods   # the run's sandbox, while its tests run
NAME            READY   STATUS    RESTARTS   AGE
sb-s-3768c2c7   1/1     Running   0          18s

$ SELECT step, snapshot_id, files FROM snapshots   # what is already safe in object storage
step 1  ea2dd70be369bc00  1 files
step 2  61bd833d4d74fa72  2 files

$ kubectl -n sandboxes delete pod sb-s-3768c2c7 --grace-period=0 --force   # the sandbox is lost, with its disk
pod "sb-s-3768c2c7" force deleted from sandboxes namespace

$ SELECT content FROM context_log WHERE step = 3 AND role = 'tool'   # the result the agent finally saw for its test run
{
 "output": "..\n----------------------------------------------------------------------\nRan 2 tests in 0.000s\n\nOK\n",
 "passed": true,
 "seconds": 45.1
}

$ agentctl status s-3768c2c7
state=waiting  step=6  workflow=RUNNING  history_events=101  tokens=2378

$ agentctl timeline s-3768c2c7
t=   6.6s  run_tool            attempts=1
t=   7.0s  run_tool            attempts=1
t=   7.2s  snapshot_workspace  attempts=1
t=   7.5s  run_tool            attempts=1
t=   7.9s  snapshot_workspace  attempts=1
t=  74.3s  run_tool            attempts=2   last failure: tool gateway 500: RemoteDisconnected: Remote end closed connection without respo
t=  74.7s  run_tool            attempts=1
t=  75.1s  run_tool            attempts=1
model calls: 7   tokens: 2378

$ kubectl -n sandboxes get pods   # the run is waiting for a reviewer: nothing is held

No resources found in sandboxes namespace.

$ kubectl -n agent-platform scale deploy/worker-v1 --replicas=0
workers scaled to zero

$ kubectl -n agent-platform get pods -l app=worker-v1

No resources found in agent-platform namespace.

$ agentctl signal s-3768c2c7 approve   # the reviewer approves while no worker exists
signal accepted by the engine

$ agentctl status s-3768c2c7   # 20 seconds later, still with no worker
state=no-worker  step=None  workflow=RUNNING  history_events=105  tokens=None

$ agentctl status s-3768c2c7   # after two new workers started
state=completed  step=None  workflow=COMPLETED  history_events=169  tokens=None

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the sandbox exists and two snapshots (after the two file writes) are already stored | A snapshot is taken after every step that changes files. |
| PASS | the run carried on to the review step | The tool step failed when its sandbox vanished and was retried, in a new sandbox. |
| PASS | the retried test run passed, so the files had been put back | A new sandbox starts empty. The manager restored the files from the manifest of the last snapshot before the tests ran. Had it not, the test discovery would have found nothing and failed. |
| PASS | the test step took more than one attempt in the history | The engine, not the platform code, recorded the retry. |
| PASS | the model was not called again for any step | A sandbox loss is a tool failure, not a reason to ask the model again. |
| PASS | the finished run's sandbox was released when it began waiting | The waiting run holds no pod. |
| PASS | with no worker at all, the run and the approval are safely held by the engine | The approval is a row in the run's history. Nothing needs to be running to receive it. |
| PASS | when workers returned, the run noticed the approval and finished | A new worker replayed the history, found the approval in it, and continued. |
| PASS | exactly one pull request exists | No step was repeated by the long wait. |

**Why you can trust this result.**

- The sandbox is deleted by name with no grace period. The snapshots that exist at that moment are listed from the database first.
- 'The files came back' rests on the output of the test run that followed, which says it ran two tests and passed. That output is the result the agent itself saw, read from the context log. A new empty sandbox cannot produce it, because the test files are not on the image.
- 'Nothing is held while waiting' is the cluster's listing of the sandboxes namespace and of the worker pods. 'The approval is safe' is the engine's answer after 20 seconds with no worker.

**Why you get this output.**

- The test step failed and was retried because the connection to its sandbox broke. The engine records the attempt count.
- The new sandbox had the files because the sandbox manager restores the run's last snapshot into every sandbox it creates: it downloads the manifest and the files it names.
- The model was not called again because a sandbox loss is a tool failure. It does not change the workflow's decisions.
- The approval was kept because a signal is an event in the run's history. When workers returned, one replayed the history and found the approval.

### S07: Context is rebuilt from the log, never remembered

**Design reference:** Context state.

**What this tests.** Kill a worker in the middle of a slow model call and see that the retry sends the same prompt. Then run the same task once without and once with compaction, and rebuild every prompt of both runs from the database to show that each one can be reproduced exactly.

The model sees what the platform builds for it, and a restarted worker remembers nothing. I kill a worker in the middle of a slow model call and compare the prompt that the next worker sends. Then I run the same task with and without compaction, and rebuild every prompt of both runs from the database to compare each one's hash with the hash recorded when it was sent.

![S07: Context is rebuilt from the log, never remembered](diagrams/appendix/a05-evidence/S07.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl model-fault --latency 30   # every model call now takes 30 seconds
{
 "primary_down_for_s": 0.0,
 "latency_s": "30.0"
}

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'
session s-37ec255f

$ agentctl status s-37ec255f   # step 0's model call is in flight
call_model on worker worker-v1-79d5c699b8-cs4lb

$ kubectl -n agent-platform delete pod worker-v1-79d5c699b8-cs4lb --grace-period=0 --force   # the worker dies while waiting for the model
pod "worker-v1-79d5c699b8-cs4lb" force deleted from agent-platform namespace

$ agentctl model-fault --latency 0   # the provider answers quickly again
{
 "primary_down_for_s": 0.0,
 "latency_s": "0.0"
}

$ agentctl calls s-37ec255f   # step 0 appears twice, with the same prompt hash
step kind    backend   status prompt_sha
   0 act     primary     200  0bd8420ee75f7cd0
   0 act     primary     200  0bd8420ee75f7cd0
   1 act     primary     200  23754b794bc2fe67
   2 act     primary     200  644f6ed9476296d8
   3 act     primary     200  afd162841618fb17
   4 act     primary     200  7451f614f8fabcd0
   5 act     primary     200  4ca870f84f70bd55
   6 act     primary     200  4ea363f65ed516af

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'   # without compaction
session s-933a12c7

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2, "context_budget": 300}'   # compact when the context exceeds 300 tokens
session s-16e1dcb3

$ SELECT upto_step, tokens FROM summaries   # each compaction is a stored row
summary of steps up to 3: 59 tokens
summary of steps up to 7: 110 tokens

$ SELECT kind, COUNT(*) FROM model_calls   # compaction is a model call like any other, recorded
act        11
summarize  2

$ prompt size sent for the final decision
without compaction: 852 tokens
with compaction:    290 tokens

$ agentctl rebuild s-933a12c7   # without compaction: rebuild every prompt from the database and compare its hash with the one the gateway recorded
step recorded         rebuilt          same
   0 0bd8420ee75f7cd0  0bd8420ee75f7cd0  True
   1 23754b794bc2fe67  23754b794bc2fe67  True
   2 644f6ed9476296d8  644f6ed9476296d8  True
   3 afd162841618fb17  afd162841618fb17  True
   4 4da4d0a3e62425ba  4da4d0a3e62425ba  True
   5 8f45eadc3db1f9c3  8f45eadc3db1f9c3  True
   6 fa1cf96698f2f356  fa1cf96698f2f356  True
   7 274a977e7cdd5186  274a977e7cdd5186  True
   8 167701098989e21f  167701098989e21f  True
   9 554b132384ada965  554b132384ada965  True
  10 2622ae2104a01906  2622ae2104a01906  True
all prompts reproduced: True

$ agentctl rebuild s-16e1dcb3   # with compaction: rebuild every prompt from the database and compare its hash with the one the gateway recorded
step recorded         rebuilt          same
   0 1574ee0cac125fed  1574ee0cac125fed  True
   1 4d1cd4e682817bad  4d1cd4e682817bad  True
   2 4a426bac120b4733  4a426bac120b4733  True
   3 9a69681ee545d8c3  9a69681ee545d8c3  True
   4 e5f6905b053dbb93  e5f6905b053dbb93  True
   5 0227117e483a0693  0227117e483a0693  True
   6 bd760e29ad2aff45  bd760e29ad2aff45  True
   7 c9f7079f23fcd0cb  c9f7079f23fcd0cb  True
   8 a17ac57563dad13d  a17ac57563dad13d  True
   9 dad9a366fa0de050  dad9a366fa0de050  True
  10 880a86414330aea5  880a86414330aea5  True
all prompts reproduced: True

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the run carried on after the worker's death | Another worker took over the model-call activity. |
| PASS | the interrupted model call was made a second time, with an identical prompt | The prompt is rebuilt from the session database, so the second worker, which remembered nothing, built the same one. This call is the one cost of a worker dying mid-call: it is paid for twice. |
| PASS | the context log holds one answer for step 0, not two | Writing an entry is idempotent: the second attempt's write changed nothing. |
| PASS | compaction happened, and the compacted run finished the same job | The scripted model reads its position from the summary exactly as it would from the full log, so the outcome is identical. |
| PASS | the compacted prompt was smaller, and each summary cost exactly one model call | A compaction is an activity: it is stored once and recorded in the history, so a replay reuses it and does not ask the model for another. |
| PASS | without compaction: every prompt rebuilds to exactly the hash recorded when it was sent | The prompt is a pure function of the stored log and summaries, which are append-only. |
| PASS | with compaction: every prompt rebuilds to exactly the hash recorded when it was sent | The prompt is a pure function of the stored log and summaries, which are append-only. |

**Why you can trust this result.**

- Every call is a row in the gateway's table with a SHA-256 of the prompt it received. Equal hashes mean equal prompts.
- The rebuild runs inside the cluster against the database as it is at that moment. If a prompt depended on anything else (a worker's memory, the order of rows, the time), the hashes would differ.
- The prompt sizes come from the same table.

**Why you get this output.**

- The second attempt sent the same prompt because the prompt is a function of the log and the latest summary, and both are in the database. The worker that started the call had nothing the next one lacked.
- The model was asked twice for that step, and that is the one cost: the call was in flight when its worker died, and the platform cannot know that the provider did not finish it.
- Compaction shrank the prompt because a summary, which is stored and recorded like any other step, replaced the entries before it.
- The compacted run reached the same end because the summary carries what the next decision needs: the task, the tools done and the facts learned.

### S08: Pause, budgets, loops, cancel, rewind and fork

**Design reference:** Run lifecycle.

**What this tests.** Do to a run what an operator or a reviewer would: pause it and resume it, hit its budgets, let it loop, cancel it and have its outside effects undone, and start new runs from the middle of an old one.

People steer runs. I do to runs what an operator or a reviewer would: pause and resume one, hit a step limit and a token limit, give one a model that repeats itself, cancel one that has a pull request, and start new runs from the middle of an old one.

![S08: Pause, budgets, loops, cancel, rewind and fork](diagrams/appendix/a05-evidence/S08.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl start --task '{"ticket": "T-1", "test_seconds": 30}'
session s-744896ed

$ agentctl signal s-744896ed pause   # while the tests are running
{
 "signalled": "pause",
 "session": "s-744896ed"
}

$ agentctl status s-744896ed   # it finished its step, then stopped
state=paused  step=4  workflow=RUNNING  history_events=68  tokens=886

$ 20 seconds later: step, model calls
step 4 -> 4    model calls 4 -> 4

$ agentctl signal s-744896ed resume
{
 "signalled": "resume",
 "session": "s-744896ed"
}

$ agentctl start --task '{... "max_steps": 3}'
session s-ee1446b4

$ agentctl status s-ee1446b4
state=needs_attention  step=3  workflow=RUNNING  history_events=52  tokens=545  attention='step or token budget reached'

$ agentctl signal s-ee1446b4 resume --json '{"extend_steps": 50}'   # a person extends the budget
{
 "signalled": "resume",
 "session": "s-ee1446b4"
}

$ agentctl start --task '{... "token_budget": 700}'
session s-7012e3f5

$ agentctl status s-7012e3f5
state=needs_attention  step=3  workflow=RUNNING  history_events=58  tokens=549  attention='token budget reached'

$ agentctl calls s-7012e3f5   # the gateway refused the call that would have crossed the budget (status 429)
step kind    backend   status prompt_sha
   0 act     primary     200  3ff2d619b2a0c8b4
   1 act     primary     200  770f409579ddddc3
   2 act     primary     200  99ffb4f8f111f076
   3 act     none        429  0526cf036491928b

$ agentctl start --task '{... "behavior": "loop"}'   # a model that repeats the same call
session s-1daa8d89

$ agentctl status s-1daa8d89
state=needs_attention  step=5  workflow=RUNNING  history_events=82  tokens=1784  attention='loop detected: list_dir called three times with the same arguments'

$ agentctl timeline s-1daa8d89
t=   4.5s  run_tool            attempts=1
t=   4.8s  run_tool            attempts=1
t=   5.3s  run_tool            attempts=1
t=   5.9s  run_tool            attempts=1
t=   6.2s  run_tool            attempts=1
model calls: 6   tokens: 1784

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'
session s-9dff6829

$ agentctl git counts   # the run has a branch and an open pull request
{"open_prs": 1, "all_prs": 1, "branches": 1, "comments": 0}

$ agentctl signal s-9dff6829 cancel
{
 "signalled": "cancel",
 "session": "s-9dff6829"
}

$ agentctl status s-9dff6829
{
 "session": "s-9dff6829",
 "workflow_status": "COMPLETED",
 "history_length": 119,
 "queue": "agent-v1",
 "run_id": "01a1fa8f-1445-7320-8518-f14b1ce06e1d",
 "result": {
  "compensated": [
   "closed PR #1",
   "deleted branch agent/s-9dff6829-4"
  ],
  "outcome": "cancelled",
  "session": "s-9dff6829",
  "step": 6
 },
 "state": "cancelled"
… (1 more lines in the screenshot and in evidence/S08.txt)

$ agentctl ledger s-9dff6829
step tool            class       status       attempts deduped  external
   4 push_branch     checkable   compensated         1       0  agent/s-9dff6829-4
   5 open_pr         checkable   compensated         1       0  1

$ agentctl git counts
{"open_prs": 0, "all_prs": 1, "branches": 0, "comments": 0}

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'   # the run to branch from
session s-f037f64f

$ agentctl fork s-f037f64f --step 3 --hint 'Keep the function under ten lines.'   # a NEW run from the recorded state after step 3
{
 "session": "s-f037f64f-f3-47a5",
 "parent": "s-f037f64f",
 "from_step": 3,
 "snapshot": "61bd833d4d74fa72",
 "replaced": false
}

$ SELECT content FROM context_log WHERE session = s-f037f64f-f3-47a5 AND step = 7   # what the fork wrote after the review
{"done": false, "tool": {"args": {"content": "import re\n\n\ndef slugify(text):\n    \"\"\"Lowercase, replace runs of other characters by a dash.\"\"\"\n    return re.sub(r\"[^a-z0-9]+\", \"-\", text…

$ agentctl fork s-f037f64f --step 3 --replace --hint 'Use a regular expression.'   # rewind: replace the original
{
 "session": "s-f037f64f-r3-8890",
 "parent": "s-f037f64f",
 "from_step": 3,
 "snapshot": "61bd833d4d74fa72",
 "replaced": true
}

$ agentctl git prs   # the Git host's pull requests
#1  closed agent/s-f037f64f-4
#2  open   agent/s-f037f64f-f3-47a5-4
#3  open   agent/s-f037f64f-r3-8890-4

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | a paused run stops between steps and does nothing while paused | The pause is a signal, honoured at the start of the next step, so no tool is cut off in the middle. |
| PASS | resuming continued from the step it had stopped at | It made one model call for each of steps 0 to 6. |
| PASS | a run that reaches its step limit stops and asks, it does not carry on | Limits are checked before each model call. |
| PASS | after a person extended the limit, the run went on | Extending is part of the resume signal, so it is in the history. |
| PASS | a token budget stops the run before the call that would exceed it | The model gateway adds up what the run has used and refuses the next call. |
| PASS | the same call three times in a row stops the run | The workflow remembers the last three calls and compares them. The repeat was not executed a third time. |
| PASS | cancelling closed the pull request and deleted the branch the run had made | The ledger shows what the run changed; cancel undoes it newest first. |
| PASS | the ledger says so | The record and the world agree. |
| PASS | the fork started from step 3 with the files the original had then | It copies the context log up to step 3 and starts from that step's workspace snapshot. |
| PASS | the fork used the hint it was given | The hint was added to the forked run's context, so the next decision read it. |
| PASS | original and fork each have their own pull request | A fork is a separate run with a separate ledger and its own idempotency keys. |
| PASS | rewinding cancelled the original and undid its pull request | The original's cancellation ran its compensations. |
| PASS | the replacement run is at the review step with its own, new pull request | Two open pull requests remain: the fork's and the replacement's. |

**Why you can trust this result.**

- The state of each run is the workflow's own answer to a query, plus the engine's record. The step counts and the model-call counts are read from the engine and the gateway.
- The effects of cancelling are read from the Git host: a closed pull request and a deleted branch are counts and states in the host, and the ledger's statuses are read from the database.
- The fork's hint is checked in the fork's own context log, where the model's next write after the review contains it.

**Why you get this output.**

- A pause stopped the run after its step and not in the middle because the workflow checks the pause flag at the top of its loop.
- The limits stop a run because the workflow checks the step budget before each model call, and the gateway refuses a call that would take the run over its token budget.
- The loop was found because the workflow keeps the last three calls and compares them. The third identical call is not run.
- Cancel undid the pull request and the branch because the workflow's cancel path asks the tool gateway to compensate, newest first, from the ledger.
- A fork or a rewind starts a new workflow from the context up to step N and the workspace snapshot of that step. A rewind also cancels the original, which undoes the original's effects.

### S09: Deploying new code while runs are in flight

**Design reference:** Deploying new code; Versioning.

**What this tests.** A change to the workflow code can make every run that is waiting unable to continue. Replay recorded histories through three versions of the code to see which is safe, then deploy the safe one the way the design says: new runs on new workers, old runs finish on old workers. Finally, stop a worker politely and see that its step is not repeated.

Deploying new agent code is dangerous for a different reason than deploying a service: every run that is waiting will be replayed through the new code. I replay a waiting run through three versions of the workflow, deploy the safe one beside the old one, and then stop a worker politely.

![S09: Deploying new code while runs are in flight](diagrams/appendix/a05-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'   # a run in flight on version 1
session s-2b3d20ee

$ WORKFLOW_VARIANT=v1 agentctl replay --sessions s-2b3d20ee   # the code the run was recorded with
v1: replays cleanly

$ WORKFLOW_VARIANT=v2-unpatched agentctl replay --sessions s-2b3d20ee   # version 2, a lint step added after every file change, with no protection
v2-unpatched: NondeterminismError: Workflow activation completion failed: Failure { failure: Some(Failure { message: "[TMPRL1100] Nondeterminism error: Activity type of scheduled event 'call_mode

$ WORKFLOW_VARIANT=v2-patched agentctl replay --sessions s-2b3d20ee   # version 2, the same change behind a patch marker
v2-patched: replays cleanly

$ kubectl -n agent-platform get pods -l role=worker   # version 1 and version 2 workers side by side
NAME                         READY   STATUS    RESTARTS   AGE   APP
worker-v1-79d5c699b8-nh4rk   1/1     Running   0          21m   worker-v1
worker-v1-79d5c699b8-zp69d   1/1     Running   0          26m   worker-v1
worker-v2-6c6d9bc47b-bzdzp   1/1     Running   0          47s   worker-v2
worker-v2-6c6d9bc47b-zt9k7   1/1     Running   0          47s   worker-v2

$ agentctl ops queue --queue agent-v2   # new runs now start on the version 2 queue
{
 "current_queue": "agent-v2"
}

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 2}'   # a new run
session s-34d56130

$ which queue each run lives on
s-2b3d20ee: agent-v1
s-34d56130: agent-v2

$ lint steps in each run's history
s-2b3d20ee (started before the deploy): 0
s-34d56130 (started after):               3

$ agentctl ops list   # runs still running on the version 1 queue
0

$ agentctl ops queue --queue agent-v1   # (back to the original code for the rest of the tests)
{
 "current_queue": "agent-v1"
}

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 25}'
session s-fd33d45e

$ agentctl status s-fd33d45e   # the test step is running on one worker
run_tool on worker-v1-79d5c699b8-nh4rk

$ kubectl -n agent-platform delete pod worker-v1-79d5c699b8-nh4rk   # a rolling update would do this: SIGTERM, not SIGKILL
pod "worker-v1-79d5c699b8-nh4rk" deleted from agent-platform namespace

$ kubectl -n agent-platform get pods -l app=worker-v1   # the old pod is Terminating, finishing its step
NAME                         READY   STATUS        RESTARTS   AGE
worker-v1-79d5c699b8-6gmtj   1/1     Running       0          5s
worker-v1-79d5c699b8-nh4rk   1/1     Terminating   0          22m
worker-v1-79d5c699b8-zp69d   1/1     Running       0          27m

$ agentctl timeline s-fd33d45e
t=   3.4s  run_tool            attempts=1
t=   3.7s  run_tool            attempts=1
t=   4.4s  run_tool            attempts=1
t=  30.0s  run_tool            attempts=1
t=  30.5s  run_tool            attempts=1
t=  30.9s  run_tool            attempts=1
model calls: 7   tokens: 2378

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the original code replays the recorded history cleanly | Replay runs the workflow code against the history with no worker and no side effect. |
| PASS | the unprotected change cannot replay the run that is waiting | The history says 'snapshot, then run the next tool'. The new code says 'snapshot, then lint'. Deploying it would have stranded every waiting run. |
| PASS | the same change behind a patch marker replays cleanly | The marker makes old histories take the old path and new runs take the new one. |
| PASS | the run in flight kept its queue and finished on version 1; the new run used version 2 | A run is pinned to the queue it started on, so the old code only ever sees old histories and the new code only new ones. |
| PASS | when the last old run finished, nothing was left on version 1, so its workers can be removed | This is how an old version is retired: wait until its queue is empty. |
| PASS | the step that was running finished on its own worker: it has one attempt | On SIGTERM the worker stops taking tasks but lets activities in flight finish. Compare S03, where a kill gave two attempts. |

**Why you can trust this result.**

- A replay is the engine's own replayer run against the recorded history. The result is the replayer's, and nothing outside the process is touched.
- The queue each run lives on is read from the engine. The lint steps are counted in each run's history.
- 'Nothing is left on version 1' is the engine's list of running runs, filtered by queue.

**Why you get this output.**

- Version 2 without protection fails to replay because the recorded history says 'snapshot, then run the next tool' and the new code says 'snapshot, then lint'. The engine reports a nondeterminism error.
- Version 2 with a patch marker replays because the marker is false for a history recorded before it existed.
- The old run stayed on version 1 because a run keeps the queue it started on. The new run was on version 2 because the session API's current queue had moved.
- The polite stop left the step alone because the worker stops taking tasks on SIGTERM and lets activities in flight finish, within a 50-second grace.

### S10: The engine's database fails over, and the engine restarts

**Design reference:** The durable execution engine; Failure modes.

**What this tests.** Force the Multi-AZ database to fail over to its standby while three runs are in flight, then delete every engine server pod while a run is in flight. Nobody touches the runs; they must finish, once.

The engine is the most important dependency. I force the Multi-AZ database to fail over to its standby while three runs are in flight, and then delete every engine pod while another run is in flight. Nobody touches the runs.

![S10: The engine's database fails over, and the engine restarts](diagrams/appendix/a05-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ agentctl start --task '{"ticket": "T-1", "test_seconds": 60}'   # three runs
s-3c9432fe
s-c47baea5
s-88da4e65

$ aws rds describe-db-instances --db-instance-identifier ds04-agentstate-db --query 'DBInstances[0].[Status,PrimaryAZ,StandbyAZ]'   # before
available	us-east-1c	us-east-1a

$ aws rds reboot-db-instance --db-instance-identifier ds04-agentstate-db --force-failover   # the primary is taken away
rebooting

$ aws rds describe-db-instances ...   # after
status=available  primary=us-east-1a  standby=us-east-1c
failover took about 580 seconds

$ agentctl status <each run>
s-3c9432fe  state=waiting  step=6  workflow=RUNNING  history_events=101  tokens=2378
s-c47baea5  state=waiting  step=6  workflow=RUNNING  history_events=101  tokens=2378
s-88da4e65  state=waiting  step=6  workflow=RUNNING  history_events=101  tokens=2378

$ agentctl git counts
{"open_prs": 3, "all_prs": 3, "branches": 3, "comments": 6}

$ model calls repeated because of the failover (steps called more than once)
0 of 33 steps

$ agentctl start --task '{"ticket": "T-1", "test_seconds": 40}'
session s-1fd8e793

$ kubectl -n temporal get pods -l 'component in (frontend,history,matching)'   # the engine's servers, before
NAME                                 READY   STATUS    RESTARTS   AGE
temporal-frontend-f4659cb87-2jq88    1/1     Running   0          23m
temporal-frontend-f4659cb87-gdqz7    1/1     Running   0          32m
temporal-history-6bf4786d97-d9dfz    1/1     Running   0          32m
temporal-history-6bf4786d97-zt75d    1/1     Running   0          23m
temporal-matching-554978f4c4-bv9zk   1/1     Running   0          23m
temporal-matching-554978f4c4-kbp4k   1/1     Running   0          32m

$ kubectl -n temporal delete pods -l 'component in (frontend,history,matching)' --force   # every engine server at once
pod "temporal-frontend-f4659cb87-2jq88" force deleted from temporal namespace
pod "temporal-frontend-f4659cb87-gdqz7" force deleted from temporal namespace
pod "temporal-history-6bf4786d97-d9dfz" force deleted from temporal namespace
pod "temporal-history-6bf4786d97-zt75d" force deleted from temporal namespace
pod "temporal-matching-554978f4c4-bv9zk" force deleted from temporal namespace
pod "temporal-matching-554978f4c4-kbp4k" force deleted from temporal namespace

$ agentctl status s-1fd8e793   # ten seconds after
RUNNING

$ kubectl -n temporal get pods -l 'component in (frontend,history,matching)'   # after
NAME                                 READY   STATUS    RESTARTS   AGE
temporal-frontend-f4659cb87-tmtwh    1/1     Running   0          39s
temporal-frontend-f4659cb87-vzskt    1/1     Running   0          39s
temporal-history-6bf4786d97-8ng7h    1/1     Running   0          39s
temporal-history-6bf4786d97-9sm69    1/1     Running   0          39s
temporal-matching-554978f4c4-fmfsl   1/1     Running   0          39s
temporal-matching-554978f4c4-p6hv9   1/1     Running   0          39s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the standby in the other zone became the primary | RDS Multi-AZ keeps a synchronous standby and moves the database's DNS name to it. |
| PASS | all three runs reached the review step on their own | The engine, the workers and the gateways retried their database connections until the new primary answered. |
| PASS | each run finished, and each made exactly one branch and one pull request | The ledger is in the same database: a step that was in flight when it failed over was retried and deduplicated. |
| PASS | the model was called for every step of every run | 11 decisions for each of 3 runs. Any step the failover interrupted mid-call was asked again, and shows above. |
| PASS | the engine's servers were all replaced | Kubernetes started new ones, which read everything from the database. |
| PASS | the run in flight reached the review step without anyone's help | The engine keeps no state of its own: a run is rows in PostgreSQL. |
| PASS | and finished once | One pull request. |

**Why you can trust this result.**

- The failover is the one the AWS API reports: the database's primary zone before and after, and its status.
- The runs' end states and the Git host's counts are read after the fact. The repeated model calls are counted from the gateway's table, and I print the count whether it is zero or not.
- The pods that replaced the engine's are listed before and after by name. No name is in both lists.

**Why you get this output.**

- The runs continued because everything that talks to the database retries its connection, and the new primary answers on the same name.
- No pull request was duplicated because the ledger lives in the same database, and a step that was in flight when the database moved is retried with the same key.
- The engine's pods were replaceable because the engine keeps no state of its own: it reads each run's history from PostgreSQL.

### S11: Many runs at once, scaled by the queue, and what an operator can see

**Design reference:** Scale; Observability.

**What this tests.** Start 24 runs together on workers that can each do six things at once, so the work is more than two workers can hold. Let the autoscaler add workers because the engine's task queue has a backlog, and read the platform's own metrics, ledger and costs while it happens.

At scale the questions are different: do the workers grow when the work grows, does the work still happen once, and can an operator see what is going on. I start 24 runs at once on workers that can each do six things at once, so two workers cannot hold them, and read the cluster, the engine and the gateway while it happens.

![S11: Many runs at once, scaled by the queue, and what an operator can see](diagrams/appendix/a05-evidence/S11.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl -n agent-platform annotate scaledobject/worker-v1 autoscaling.keda.sh/paused=false   # switch the autoscaler on
scaledobject/worker-v1 annotated

$ kubectl -n agent-platform get scaledobject worker-v1   # scale on the engine's task queue backlog
NAME        SCALETARGETKIND      SCALETARGETNAME   MIN   MAX   READY   ACTIVE    FALLBACK   PAUSED   TRIGGERS   AUTHENTICATIONS   AGE
worker-v1   apps/v1.Deployment   worker-v1         2     8     True    Unknown   False      False    temporal                     92m

$ start 24 runs at once, each with a 100-second test step
24 runs started in 3 seconds

$ sample every ~10 seconds: workers, activity slots in use (Prometheus), runs at the review step
t=   9s  workers=2  activity slots in use=   0  runs waiting for review=0/24
t=  25s  workers=4  activity slots in use=  12  runs waiting for review=0/24
t=  41s  workers=4  activity slots in use=  12  runs waiting for review=0/24
t=  57s  workers=4  activity slots in use=  24  runs waiting for review=0/24
t=  72s  workers=4  activity slots in use=  24  runs waiting for review=0/24
t=  88s  workers=4  activity slots in use=  24  runs waiting for review=0/24
t= 104s  workers=4  activity slots in use=  24  runs waiting for review=0/24
t= 119s  workers=4  activity slots in use=  24  runs waiting for review=0/24
t= 135s  workers=4  activity slots in use=  24  runs waiting for review=0/24
t= 151s  workers=4  activity slots in use=  23  runs waiting for review=15/24
t= 166s  workers=4  activity slots in use=   9  runs waiting for review=15/24
t= 182s  workers=4  activity slots in use=   0  runs waiting for review=24/24

$ agentctl git counts
{"open_prs": 24, "all_prs": 24, "branches": 24, "comments": 0}

$ SELECT tool, class, COUNT(*), SUM(attempts), SUM(deduplicated) FROM ledger   # the side-effect ledger across the runs
open_pr        checkable   runs= 24  attempts= 24  deduplicated=0
push_branch    checkable   runs= 24  attempts= 24  deduplicated=0

$ tokens per run, from the model gateway's record
runs=24  min=5502  average=5506  max=5509  total=132153
model calls by backend: primary=264

$ agentctl timeline s-87fa95a3   # one run's life, from its own history
t=   0.9s  call_model          attempts=1
t=   5.2s  run_tool            attempts=1
t=  35.5s  call_model          attempts=1
t=  35.9s  run_tool            attempts=1
t=  36.2s  snapshot_workspace  attempts=1
t=  36.6s  call_model          attempts=1
t=  36.8s  run_tool            attempts=1
t=  37.1s  snapshot_workspace  attempts=1
t=  37.3s  call_model          attempts=1
t= 137.5s  run_tool            attempts=1
t= 137.7s  call_model          attempts=1
t= 137.9s  run_tool            attempts=1
t= 138.1s  call_model          attempts=1
t= 138.3s  run_tool            attempts=1
t= 138.5s  call_model          attempts=1
t= 186.1s  signal approve
… (10 more lines in the screenshot and in evidence/S11.txt)

$ from each run's own history: how long every activity waited for a free worker slot (scheduled to started)
624 activities in 24 runs
median wait: 0.0 seconds
longest wait: 33.9 seconds
activities that waited 5 seconds or more: 12 (call_model, run_tool)

$ Prometheus: temporal_activity_task_received and temporal_activity_execution_failed, the workers running now
activities received: 634
activities failed (and retried): 0

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | each worker has six activity slots, so two workers hold twelve steps at once | Slots are set small so that a modest number of runs exceeds them. What matters is the ratio of runs to slots, not the absolute numbers. |
| PASS | all 24 runs were accepted | The session API only writes a row and starts a workflow, so starting many at once is cheap. |
| PASS | the workers were saturated: every slot was in use at some point | Peak slots in use: 24 of 12 available with two workers. |
| PASS | the autoscaler added workers because of the queue's backlog | Peak workers: 4. The trigger is the engine's task queue, not CPU: a worker waiting for a model or a tool uses almost none. |
| PASS | all 24 runs reached the review step | Each run is independent: its own history, its own ledger rows, its own sandbox. |
| PASS | 24 branches and 24 pull requests exist, one for each run | Queueing and scaling did not repeat or lose any step. |
| PASS | after the reviews, all 24 runs completed | Approvals reached the waiting workflows, now on whichever workers were running. |
| PASS | the gateway's record accounts for every run's tokens, and the model was called 11 times per run | Cost per run is a query over one table, because every call goes through the gateway. |
| PASS | the backlog is visible in the engine's own records: a few steps waited for a slot, most did not | Steps that queued behind a full set of workers waited until a slot opened or the autoscaler's new workers joined. The median step started at once. |
| PASS | the workers' own metrics are being collected | Each worker serves Prometheus metrics, and Prometheus finds the pods by their annotations. |

**Why you can trust this result.**

- The worker count is the cluster's list of worker pods, sampled every ten seconds. The slots in use are Prometheus's reading of the workers' own metric.
- The effects (24 branches, 24 pull requests) are the Git host's counts. The tokens are the gateway's table.
- The wait for a free slot is the engine's own histogram, read from Prometheus. I print it as it is.

**Why you get this output.**

- The workers saturated because 24 runs each had a long test step and there were 12 slots. The rest of the steps queued in the engine.
- The workers grew because KEDA reads each task queue's backlog from the engine and sets the Deployment's size so that no worker has more than a few waiting tasks.
- Nothing was repeated because scaling out adds pollers. A task goes to one of them, and a task whose worker dies is retried under the same key.
- Cost per run is a query because every model call goes through the gateway, which writes one row for each.

### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 04 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **A real language model** | The model is a scripted function of the prompt. A real model would make the failure tests non-repeatable and cost money, and the design is about what surrounds the model. The gateway, the retries, the budgets, the context rebuilding and the recording of every call are real. What is not tested is how a real model behaves when it is given a compacted context. |
| **A real Git host** | The Git host is a small service I wrote, so that it can lose its answer on demand. It honours an `Idempotency-Key` the way the design assumes real hosts and payment APIs do. I did not test any real provider's behaviour. |
| **Aurora PostgreSQL** | The engine's database is Multi-AZ RDS for PostgreSQL. It has the failover behaviour the tests need. Aurora's shared cluster storage, its faster failover and its read replicas are not built. S10 measures RDS's failover, not Aurora's. |
| **Loss of a whole Availability Zone** | S10 fails the database over to its standby and removes every engine pod, which is what a zone loss does to the engine's state. I did not remove a zone's worth of nodes, so the spreading of workers and engine pods over zones is built but not tested by taking a zone away. |
| **Region loss and a standby region** | The design lists an engine with a standby in another region, or replicated histories and snapshots. This build is one region. |
| **Pod-level checkpoint and restore, and process snapshots** | The design leaves them out on purpose, and so do I. A test that is running when its sandbox is lost starts again (S06). |
| **Dapr Workflow** | The design names it as an alternative to Temporal. I built the Temporal choice only. |
| **Temporal Cloud and Temporal's own worker versioning feature** | I self-hosted the engine, as the design describes. Versioning is done with separate task queues, which is the manual form of the idea, and with a replay test (S09). |
| **Multi-agent runs** | The design lists them as an open question. Each run here has one agent. |
| **Retention and deletion of histories, context logs and snapshots** | Another open question in the design. Nothing is deleted by the platform; the teardown deletes everything. |
| **Large payloads by reference** | The design says the history should hold an object key and a hash and not a large payload. The payloads here are a few hundred bytes, so I kept them in the history. |
| **A sandbox with real isolation** | The sandbox is a plain pod. [Design 01](#design-sandboxing) and [Appendix B](#appendix-design-01-eks) cover isolation. What is tested here is its lifecycle. |
| **A product UI** | The session API is called from a command line. A UI would use the same calls. |
| **Real autoscaling limits** | KEDA scales between 2 and 8 workers with six activity slots each. The numbers are small to make a modest load exceed them. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, Docker with `buildx` (to build the `linux/amd64` image), and Python 3 with `boto3` and Pillow for the tests and screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check.

**Build.**

```text
cd implementations/ds04-agent-state-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public address (both git-ignored), builds the VPC, the cluster and the database, builds and pushes the image, then builds the platform. It takes roughly 35 minutes, most of it the database and the first image push.

**Test.**

```text
python3 tests/run_scenarios.py all
```

Expect about an hour and a half. The scenarios fail the database over (S10, about six minutes) and run 24 runs at once (S11).

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` removes the workloads, destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, buckets, repositories, database instances, roles and log groups. It prints `CLEAN` when nothing is left.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds04-agentstate`, with its Compute tab and the pods of the `temporal` and `agent-platform` namespaces.
- RDS, Databases: `ds04-agentstate-db`, showing Multi-AZ, the primary's zone and the standby's, and in Logs and events the failover that S10 caused.
- ECR: the `ds04-agentstate/agent` repository and its image tag.
- S3: the bucket with `snapshots/objects/` and `snapshots/manifests/`.
- IAM, Roles: the sandbox role and its policy, which allows nothing outside `snapshots/`.
- VPC: the VPC with its three private subnets and the NAT gateway.
- CloudWatch, Log groups: the cluster's API and audit logs.

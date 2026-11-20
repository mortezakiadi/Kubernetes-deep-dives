---
id: design-sandbox-worlds
title: "Design 30: Scalable sandboxed worlds for realistic multi-agent, multi-node, multi-user tasks"
short: "Sandboxed multi-agent worlds"
---
This design answers one question: *how do we run thousands of realistic, isolated "worlds" at once (each with several agents, simulated users, real services and sometimes several machines), so that agents can be trained and evaluated on tasks that look like real work?* [Design 01](#design-sandboxing) covers the single sandbox: isolation boundaries, warm pools, network and credentials for one agent's code. Real tasks need more: an agent fixing a bug reported in a ticket, discussed in chat by simulated colleagues, in a service running across three machines, while another agent reviews its change. This design composes sandboxes and services into worlds, and makes those worlds fast to create, isolated from each other, reproducible, observable and cheap enough to run at RL scale.

The short version: *a world is a declared bundle of members, services, nodes and a scenario, created from templates and warm pools in its own isolated namespace*. A world specification lists the agents (each in its own sandbox, from Kubernetes Agent Sandbox templates), the simulated users, the services (ticketing, git, chat, mail, databases, internal apps), any extra "machines" the task needs (microVM-isolated pods admitted as a group), and the scenario: initial data, a script of events and a verifier. A world controller provisions it in dependency order, from pre-baked images and data snapshots, into a namespace that can talk only to itself. A virtual clock, seeded simulated users and recorded interactions make worlds reproducible. Whole-world snapshots let RL fork many rollouts from one mid-task state. Every action by every actor goes to one event log and one trace per world.

### The use case and requirements

**Scenario.** The agent team wants to train and evaluate on tasks like these: "a customer reported a bug by email; reproduce it, fix it, get the fix reviewed and reply to the customer", or "one node of a three-node database cluster is unhealthy; find out why and repair it without losing data". The first prototype ran each task by hand-assembling a VM with everything installed. It took 25 minutes to start, couldn't run more than 40 at once, and two runs of the same task never behaved the same way.

![From one VM to thousands of worlds](diagrams/designs/concepts/ds30-c1.svg)

**What the platform must deliver.**

- **Composed worlds:** agents, simulated users, services and machines declared together and created as one unit.
- **Scale:** 5,000 concurrent worlds for RL and evaluation, with world startup under 60 seconds at p95 for common templates.
- **Isolation:** worlds can't see or affect each other, agents within a world have only their own identities and permissions, and nothing reaches the real internet unless declared.
- **Multi-node tasks:** worlds that include several "machines" with real networking between them, scheduled together.
- **Reproducibility:** the same world specification and seed produce the same starting state and the same simulated-user behavior.
- **Snapshots and forks:** save a whole world mid-task and start many rollouts from it.
- **Observability:** every action of every actor in a world, in order, with outcomes the verifier can check across services.

**Out of scope:** the single-sandbox security model ([Design 01](#design-sandboxing)), task production ([Design 28](#design-lh-data-pipelines)) and reward hacking defenses ([Design 29](#design-reward-hacking)), which this design relies on.

### The world model

**Scenario.** The first tasks were defined as shell scripts that installed services in a VM. Each script made slightly different choices, and adding a second agent to a task meant rewriting the script.

![World model](diagrams/designs/concepts/ds30-c2.svg)

![The world model](diagrams/designs/ds30-1.svg)

**What the picture shows.** What a world is made of, declared in one specification.

- **Members:** the actors. Agents (policies under training or evaluation, each in its own sandbox with its own identity and tools) and simulated users (model-driven personas with goals, or scripted bots).
- **Services:** the applications the members use: a git server, ticketing, chat, mail, databases and internal web apps. Each is a template with an image and a data snapshot.
- **Nodes:** for tasks about infrastructure, a set of microVM-isolated "machines" that agents can log into, with networking between them.
- **Scenario:** initial data (which tickets exist, which code is deployed, which emails are waiting), a script of timed events (a user sends a follow-up after 20 world-minutes) and fault injections (a disk fills up on node 2).
- **Verifier:** checks across services at the end (bug fixed in git, ticket closed, customer answered, data intact), plus milestones ([Design 08](#design-long-horizon)).
- **Budgets:** world wall time, world time, agent steps and tokens.

**The YAML.** A world specification (platform API; agents run in Agent Sandbox templates).

```yaml
apiVersion: worlds.example.com/v1alpha1
kind: WorldTemplate
metadata: { name: support-bugfix-v3 }
spec:
  members:
  - { name: engineer, kind: agent, sandboxTemplate: swe-agent-gvisor, tools: [shell, git, chat, tickets] }
  - { name: reviewer, kind: agent, sandboxTemplate: review-agent, tools: [git, chat] }
  - { name: customer, kind: simulated-user, persona: personas/impatient-enterprise-customer, channel: mail }
  - { name: pm, kind: simulated-user, persona: personas/product-manager, channel: chat }
  services:
  - { name: git, template: gitea-1.24, data: snapshots/acme-repo@sha256:4be1 }
  - { name: tickets, template: ticketing-v2, data: snapshots/acme-tickets@sha256:91c0, dependsOn: [] }
  - { name: chat, template: chat-v5 }
  - { name: mail, template: mailhog-1.0 }
  - { name: app, template: acme-app, dependsOn: [git, db] }
  - { name: db, template: postgres-16, data: snapshots/acme-db@sha256:7d22 }
  scenario:
    events:
    - { at: "00:00", actor: customer, do: send-mail, about: "export fails for files > 2 GB" }
    - { at: "00:20", actor: pm, do: chat, about: "customer is escalating; status?" }      # world time #!
  verifier: { image: registry.example.com/verifiers/support-bugfix:3, milestones: [repro, fix-merged, reply-sent] }
  budgets: { wallClock: 2h, worldTime: 8h, agentSteps: 600 }
```

**Going deeper.**

- **Templates compose, worlds instantiate:** service and persona templates are shared across hundreds of world templates. A fix to the chat service template reaches every world that uses it.
- **Version everything:** world templates, service templates, data snapshots and personas are versioned by digest, and tasks ([Design 28](#design-lh-data-pipelines)) record the exact versions.

### Provisioning worlds fast

**Scenario.** Creating a world from scratch meant pulling eight images, restoring three databases and starting services one after another. Median startup was 4 minutes, and an RL job that needed 2,000 worlds spent most of its first hour waiting for them.

![Provisioning](diagrams/designs/concepts/ds30-c3.svg)

![Provisioning worlds fast](diagrams/designs/ds30-2.svg)

**What the picture shows.** A world controller that creates worlds from warm parts, in dependency order.

- **Namespace per world:** each world gets its own namespace with network policies, quotas and labels for cost and cleanup.
- **Warm agent sandboxes:** agents claim pre-started sandboxes from Agent Sandbox warm pools, which hand over a ready, isolated pod in about a second.
- **Pre-baked service images:** service images include their data where small. Larger data restores from snapshots on fast local storage, or from volume snapshots.
- **Dependency order:** services start as soon as their dependencies are ready, in parallel where possible. The world is ready when every service passes its readiness check and the scenario's initial events are applied.
- **Warm worlds:** for the most used templates, complete worlds are pre-created and held paused. A request takes one and applies the task-specific data.
- **Images on nodes:** world images are pre-pulled on world nodes, or served by a node-local cache, so pulls don't sit on the critical path.

**The code.** Starting a world's services in dependency order, with parallel waves (Python).

```python
import asyncio
from graphlib import TopologicalSorter

async def start_world(services: dict[str, dict], start, ready, timeout_s: int = 120) -> dict:
    """services: name -> {"dependsOn": [...]}; start(name) creates it; ready(name) awaits its readiness probe."""
    ts = TopologicalSorter({n: set(s.get("dependsOn", [])) for n, s in services.items()})
    ts.prepare()
    timings = {}
    while ts.is_active():
        wave = list(ts.get_ready())                                     # everything whose dependencies are ready #!
        loop = asyncio.get_running_loop(); t0 = loop.time()
        await asyncio.gather(*(start(n) for n in wave))
        await asyncio.wait_for(asyncio.gather(*(ready(n) for n in wave)), timeout=timeout_s)
        for n in wave:
            timings[n] = round(loop.time() - t0, 2); ts.done(n)
    return timings
```

**Going deeper.**

- **The slowest dependency chain sets startup time:** a database restore that everything waits for dominates. Measure each template's critical path and shorten that, not the average service.
- **Warm worlds have a cost:** paused worlds hold memory. Size their pools from request rates per template, like any warm pool ([Design 01](#design-sandboxing)).

### Multi-node worlds

**Scenario.** A task asked the agent to repair a three-node database cluster. The prototype ran all three "nodes" as processes in one container, so the agent could never practice what mattered: logging into a specific machine, reading its logs and fixing its network configuration.

![Multi-node worlds](diagrams/designs/concepts/ds30-c4.svg)

![Multi-node worlds](diagrams/designs/ds30-3.svg)

**What the picture shows.** Worlds that include real machines, as isolated as the task requires.

- **Machines as microVM pods:** each world node is a pod in a microVM runtime (Kata Containers or similar), with its own kernel, so agents can run privileged operations inside without reaching the host ([Design 01](#design-sandboxing)).
- **Networking between them:** world nodes get stable names in the world's namespace and can reach each other freely, and nothing else. Network faults (latency, partitions) are injected per world.
- **Scheduled together:** a world's nodes are admitted as one group by Kueue, so a world never starts with two of its three machines.
- **Agents log in like operators:** agents reach world nodes over SSH (or a shell tool) from their own sandbox, with keys issued for that world.
- **Heavier worlds, separate pool:** multi-node worlds run on a node pool with nested-virtualization support and their own quota.

**The YAML.** One of a world's machines, admitted with the others as a Kueue pod group in the world's namespace.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: db-node-2
  namespace: world-7f3a91
  labels:
    worlds.example.com/role: node
    kueue.x-k8s.io/queue-name: worlds-multinode
    kueue.x-k8s.io/pod-group-name: world-7f3a91-nodes        # all three machines admitted together #!
  annotations:
    kueue.x-k8s.io/pod-group-total-count: "3"
spec:
  runtimeClassName: kata-qemu                                 # own kernel: root inside, not on the host
  hostname: db-node-2
  subdomain: nodes                                            # db-node-2.nodes.world-7f3a91.svc
  automountServiceAccountToken: false
  containers:
  - name: machine
    image: registry.example.com/worlds/node-images/debian-pg16@sha256:2c9e
    resources: { requests: { cpu: "2", memory: 4Gi }, limits: { cpu: "2", memory: 4Gi } }
    securityContext: { privileged: false }
```

**Going deeper.**

- **Use the lightest form that keeps the task honest:** many "multi-node" tasks only need separate processes with separate hostnames and network namespaces. Reserve microVMs for tasks that need kernel-level behavior.
- **A virtual cluster per world:** for tasks about Kubernetes itself, a virtual control plane per world gives agents a real cluster to break and fix, without touching the host cluster.

### Isolation between and within worlds

**Scenario.** Two worlds on the same node could reach each other's chat service by IP. In one evaluation, an agent found another world's chat history through a misconfigured service and copied an answer from it.

![World isolation](diagrams/designs/concepts/ds30-c5.svg)

![Isolation between and within worlds](diagrams/designs/ds30-4.svg)

**What the picture shows.** Boundaries at two levels.

- **Between worlds:** each world's namespace is default-deny, with traffic allowed only between pods of the same world, plus the platform services the world needs (inference gateway, mock internet). There are no cluster-wide services that worlds share writable state through.
- **Within a world:** each member has its own identity in the world's services (its own mailbox, chat account, git user and permissions), so an agent can't read another agent's mail unless the scenario grants it.
- **Runtime isolation:** agent sandboxes and world nodes use gVisor or microVM runtimes, with no service account tokens and no route to the cluster API ([Design 01](#design-sandboxing)).
- **The outside world, simulated:** a mock internet (package mirrors, documentation snapshots, fake external APIs) is the only "outside". Real internet access is opt-in per template and logged.
- **The verifier and the scenario controller live outside the world:** members can't tamper with them ([Design 29](#design-reward-hacking)).

**The YAML.** Default-deny with intra-world traffic and two platform destinations (NetworkPolicy, created by the world controller).

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: world-boundary, namespace: world-7f3a91 }
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
  ingress:
  - from: [{ podSelector: {} }]                                       # same world only
  - from: [{ namespaceSelector: { matchLabels: { worlds.example.com/role: control } } }]   # scenario controller, verifier
  egress:
  - to: [{ podSelector: {} }]
  - to: [{ namespaceSelector: { matchLabels: { worlds.example.com/role: inference } } }]   # policy + persona models #!
  - to: [{ namespaceSelector: { matchLabels: { worlds.example.com/role: mock-internet } } }]
  - to: [{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }, podSelector: { matchLabels: { k8s-app: kube-dns } } }]
    ports: [{ port: 53, protocol: UDP }]
```

**Going deeper.**

- **Leaks between worlds are evaluation contamination:** a test runs regularly in which an agent in one world tries to reach every other world's services. Any success is a security incident and an evaluation-integrity incident.
- **Per-member credentials come from the scenario:** the world controller creates accounts in each service at startup and hands each member only its own, so permissions are part of the task definition.

### Simulated users and other agents

**Scenario.** Tasks with "a customer" used a fixed script: the customer always sent the same three emails, regardless of what the agent did. Agents learned to wait for the third email instead of asking questions.

![Simulated users](diagrams/designs/concepts/ds30-c6.svg)

![Simulated users and other agents](diagrams/designs/ds30-5.svg)

**What the picture shows.** Members that react to the agent, while staying controlled and reproducible.

- **Personas:** each simulated user has a role, goals, knowledge (what it knows that the agent doesn't), a style and limits (when it gives up or escalates), versioned like any template.
- **Model-driven responses:** a persona model reads the conversation on its channel and decides whether and how to respond, through the shared inference pool, separate from the policy being trained.
- **Scripted anchors:** some events are fixed by the scenario (the escalation at minute 20), so tasks keep their structure.
- **Seeded and recorded:** persona sampling is seeded, and every persona message is recorded, so a world can be replayed exactly with the same user behavior.
- **Other agents:** collaborating or competing agents are members like the rest. They can be frozen policies (for stable evaluation) or co-trained policies.

**The code.** A simulated user's step: read its channel, decide, reply, record (Python).

```python
import json, random

def persona_step(persona: dict, inbox: list[dict], world_clock, llm, recorder, seed: int) -> dict | None:
    """persona: role, goals, knowledge, style, patience_minutes. inbox: new messages to this persona."""
    rng = random.Random(f"{seed}:{persona['name']}:{len(recorder.history(persona['name']))}")
    if not inbox and world_clock.minutes_since_last_reply(persona["name"]) < persona["patience_minutes"]:
        return None                                                      # nothing new and still patient
    prompt = {"role": persona["role"], "goals": persona["goals"], "knows": persona["knowledge"],
              "style": persona["style"], "conversation": recorder.history(persona["name"]) + inbox,
              "now": world_clock.now_iso()}
    reply = llm.generate(json.dumps(prompt), temperature=0.7, seed=rng.randrange(2**31))   # seeded sampling #!
    msg = {"from": persona["name"], "at": world_clock.now_iso(), "text": reply}
    recorder.append(persona["name"], inbox + [msg])                     # replay reproduces the same messages
    return msg
```

**Going deeper.**

- **Personas can be exploited:** an agent can learn to manipulate a simulated user into confirming success. Persona behavior is part of the reward stack's threat model ([Design 29](#design-reward-hacking)), and the verifier checks the world, not the persona's opinion.
- **Persona quality is measured:** a sample of persona conversations is reviewed for realism and consistency, and personas that break character are fixed like flaky tests.

### Time, determinism and replay

**Scenario.** A task's escalation was set for "20 minutes in". On a busy cluster, the agent's model calls were slow, so the escalation arrived before the agent had read the first email. On a quiet cluster, it arrived much later. The same task had different difficulty depending on load.

![World clock](diagrams/designs/concepts/ds30-c7.svg)

![Time, determinism and replay](diagrams/designs/ds30-6.svg)

**What the picture shows.** World time separated from wall time, and the sources of variation controlled.

- **World clock:** each world has a clock that advances with agent and user actions (for example, a fixed amount of world time per agent step plus tool durations), not with wall time. Scenario events fire on world time.
- **Pausing:** while the policy is waiting for inference, the world clock doesn't move, so cluster load doesn't change the task.
- **Time inside services:** services that read time (ticket timestamps, token expiry) use the world clock, through a time service or a time-offset library in the service images.
- **Seeds everywhere:** data generation, persona sampling and fault injection take seeds from the world's seed.
- **Record and replay:** every input from outside the policy (persona messages, injected faults, mock-internet responses) is recorded, so a trajectory can be replayed for debugging or evaluation.

**The code.** A world clock that advances with actions and pauses during inference (Python).

```python
from datetime import datetime, timedelta

class WorldClock:
    def __init__(self, start: datetime, seconds_per_step: float = 30.0):
        self.now_ = start
        self.seconds_per_step = seconds_per_step
        self.events = []                                                   # (fire_at, event), sorted

    def schedule(self, offset: timedelta, event: dict):
        self.events.append((self.now_ + offset, event)); self.events.sort(key=lambda e: e[0])

    def advance(self, tool_seconds: float = 0.0) -> list[dict]:
        """Called after each agent action; inference time is never counted."""
        self.now_ += timedelta(seconds=self.seconds_per_step + tool_seconds)   # world time, not wall time #!
        due = [e for t, e in self.events if t <= self.now_]
        self.events = [(t, e) for t, e in self.events if t > self.now_]
        return due                                                         # scenario events to deliver now

    def now_iso(self) -> str:
        return self.now_.isoformat()
```

**Going deeper.**

- **Determinism has limits:** services with internal concurrency don't replay bit-for-bit. Aim for the same task conditions, and record enough to explain differences.
- **Tool durations count:** a test suite that takes 6 real minutes should take world time too, otherwise slow actions look free to the agent.

### World state: snapshots and forks

**Scenario.** RL on a 3-hour task produced one reward per 3 hours. Researchers wanted to try 16 different continuations from the point where the agent had reproduced the bug, but every attempt had to start from the beginning.

![Snapshots and forks](diagrams/designs/concepts/ds30-c8.svg)

![World state: snapshots and forks](diagrams/designs/ds30-7.svg)

**What the picture shows.** Saving a whole world and starting many worlds from the saved state.

- **What a world snapshot contains:** each service's data (database dumps or volume snapshots), each agent sandbox's file system, the world clock, the scenario's progress, persona histories, and the event log up to that point.
- **Consistent cut:** the world is paused (agents and personas idle, services quiesced or snapshotted with crash-consistent tools) so that the parts agree.
- **Fork:** a new world is created from the snapshot instead of the template, with a new world ID and seed, and continues from there. Sixteen forks give sixteen continuations from one shared prefix.
- **Uses:** branching rollouts for RL ([Design 08](#design-long-horizon)), resuming after failures ([Design 28](#design-lh-data-pipelines)), and debugging (reopen a world exactly where something went wrong).
- **Storage:** snapshots are content-addressed and deduplicated across forks. Most data is shared with the base template.

**The YAML.** Snapshotting a world service's volume as part of a world snapshot (CSI VolumeSnapshot).

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: world-7f3a91-db-step-140
  namespace: world-7f3a91
  labels:
    worlds.example.com/world: world-7f3a91
    worlds.example.com/snapshot: step-140                       # one label ties all parts of a world snapshot #!
spec:
  volumeSnapshotClassName: fast-local-snapclass
  source: { persistentVolumeClaimName: db-data }
```

**Going deeper.**

- **Forking needs copy-on-write storage:** creating 16 copies of a 20 GB database by copying is slow. Storage classes with fast clones from snapshots (or file systems with copy-on-write) make forks cheap.
- **Fork from meaningful points:** milestones are natural fork points. Forking after "bug reproduced" focuses rollouts on the part of the task the policy finds hard.

### Scaling to thousands of worlds

**Scenario.** At 1,200 concurrent worlds, the cluster's API server slowed noticeably. Each world created about 60 Kubernetes objects, and the world controller listed every namespace on every reconcile. Nodes were also half empty, because each world reserved resources for its peak.

![Scale](diagrams/designs/concepts/ds30-c9.svg)

![Scaling to thousands of worlds](diagrams/designs/ds30-8.svg)

**What the picture shows.** What limits world scale, and how each limit is raised.

- **Density:** world services are small and mostly idle. Requests are set from measured usage (not peaks), with limits for bursts, packing many worlds per node.
- **Control-plane load:** fewer objects per world (one Deployment for several small services, shared ConfigMaps), controllers that watch with label selectors and cache, and API Priority and Fairness for the world controller ([Design 27](#design-tenant-platform)).
- **Shared read-only services:** package mirrors, documentation snapshots and the mock internet are shared across worlds, read-only.
- **Inference is the real bottleneck:** agents and personas call models constantly. The shared serving pool is sized for world count × calls per world-minute ([Design 21](#design-llm-serving)).
- **Several clusters:** past a few thousand worlds per cluster, worlds are spread across clusters by a placement layer ([Design 02](#design-multicluster)).

**The code.** A capacity model for a world fleet (Python).

```python
import math

def world_capacity(worlds: int, cpu_per_world: float, mem_gb_per_world: float, objects_per_world: int,
                   node_cpu: int = 96, node_mem_gb: int = 768, target_util: float = 0.75,
                   agent_calls_per_world_min: float = 6.0, persona_calls_per_world_min: float = 1.5) -> dict:
    nodes = math.ceil(max(worlds * cpu_per_world / (node_cpu * target_util),
                          worlds * mem_gb_per_world / (node_mem_gb * target_util)))
    return {"nodes": nodes, "worlds_per_node": round(worlds / nodes, 1),
            "api_objects": worlds * objects_per_world,                                  # control-plane budget #!
            "inference_calls_per_s": round(worlds * (agent_calls_per_world_min + persona_calls_per_world_min) / 60)}

print(world_capacity(5000, cpu_per_world=2.2, mem_gb_per_world=7.5, objects_per_world=24))
```

**Going deeper.**

- **Measure per-template costs:** each world template's CPU, memory, objects and inference calls per world-hour are measured in a staging run and stored with the template, so capacity planning uses real numbers.
- **Idle worlds are still worlds:** a world waiting for inference holds its memory. Pausing worlds during long inference waits (freezing their containers) can raise density further.

### Observability and verification across a world

**Scenario.** A multi-agent task failed. The engineer agent's trajectory showed it fixed the bug. The reviewer agent's showed it approved. The verifier said the fix wasn't merged. It took a day to find that the reviewer had approved a different branch, because the two agents' logs, the git server's log and the chat history were in four places.

![World observability](diagrams/designs/concepts/ds30-c10.svg)

![Observability and verification across a world](diagrams/designs/ds30-9.svg)

**What the picture shows.** One timeline per world, and verifiers that check the whole world.

- **One event log:** every action by every member (tool calls, messages, commits, ticket changes) and every scenario event, with world time, wall time and actor, in one ordered log per world.
- **One trace:** a trace ID per world, propagated into services through request headers, so a member's action and its effects in services appear in one trace.
- **Cross-service verification:** the verifier queries each service's final state (git, tickets, mail, database) through admin APIs from outside the world, and checks the outcome as a whole.
- **Attribution:** for multi-agent tasks, the event log shows which member did what, which feeds per-agent credit in training.
- **World replay viewer:** a timeline view of the event log, used by researchers and reviewers ([Design 12](#design-experiment-analysis)).

**The code.** A cross-service verifier for the support-bugfix world (Python).

```python
def verify_support_bugfix(world) -> dict:
    """world exposes admin clients for each service; run from outside the world's namespace."""
    merged = world.git.pull_requests(state="merged", base="main")
    fix = next((pr for pr in merged if world.git.ci_status(pr["merge_sha"]) == "passed"
                and world.tests.run_hidden(pr["merge_sha"], suite="export-large-files")), None)
    reviewed = bool(fix) and any(r["user"] == "reviewer" and r["state"] == "approved" and r["sha"] == fix["head_sha"]
                                 for r in world.git.reviews(fix["number"]))            # approved *this* change #!
    ticket = world.tickets.get(world.scenario["ticket_id"])
    replied = any(m["to"] == "customer" and m["at"] > (fix or {}).get("merged_at", "9999")
                  for m in world.mail.sent(by="engineer"))
    milestones = {"repro": world.events.any(actor="engineer", kind="test-failed", suite="export-large-files"),
                  "fix-merged": bool(fix), "reviewed": reviewed, "reply-sent": replied}
    return {"passed": all(milestones.values()) and ticket["status"] == "resolved", "milestones": milestones}
```

**Going deeper.**

- **Verify the world, not the transcript:** an agent saying "merged and replied" proves nothing. The verifier reads the services' state directly.
- **Environment failures aren't agent failures:** a service crash or a provisioning error is recorded as an environment failure and excluded from reward, so flaky infrastructure doesn't teach the policy anything.

### Operating the world platform

**Scenario.** After a month, 900 worlds were still running that nobody used: their RL jobs had crashed without deleting them. Separately, 4% of worlds failed during startup because one service template had a race condition, and those failures were counted as agent failures.

![Operations](diagrams/designs/concepts/ds30-c11.svg)

![Operating the world platform](diagrams/designs/ds30-10.svg)

**What the picture shows.** Lifecycle, failure handling and the platform's own SLOs.

- **Leases and TTLs:** every world has an owner (the job that requested it) and a lease the owner renews. Worlds whose lease expires are snapshotted (if requested) and deleted. A hard TTL caps every world.
- **Failure classes:** provisioning failures, service crashes, scenario errors and verifier errors are environment failures, retried and excluded from reward. Only agent outcomes count as task results.
- **Template health:** each template's startup time, environment-failure rate and cost per world-hour are tracked. Templates above their failure budget are quarantined until fixed.
- **Template releases:** new template versions go through a canary (a few hundred worlds) with health compared to the previous version, then roll out. Tasks keep pinned versions until they're re-validated ([Design 28](#design-lh-data-pipelines)).
- **SLOs:** world startup p95, environment-failure rate, leak count (worlds without a live owner) and isolation test results.

**Going deeper.**

- **Garbage collection is a feature:** orphaned worlds cost money and API capacity. A sweeper that checks leases every few minutes is as important as the controller that creates worlds.
- **Environment failure rate is a training metric:** a policy trained in worlds that fail 4% of the time sees noise in 4% of its rewards. Track and drive it toward zero like any other data-quality signal.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds30-c12.svg)

- **Phase 1, single-member worlds (weeks 1–5):** the world specification, the world controller with namespaces and network boundaries, service templates for the first task family, Agent Sandbox warm pools for agents.
- **Phase 2, multiple members (weeks 4–10):** per-member identities in services, simulated users with seeded personas and recording, the world clock, one event log and trace per world, cross-service verifiers.
- **Phase 3, scale (weeks 8–14):** warm worlds, density tuning, controller efficiency, inference capacity planning, leases and garbage collection, environment-failure classification.
- **Phase 4, multi-node and forks (weeks 12–18):** microVM world nodes as Kueue pod groups, fault injection, whole-world snapshots and forks on copy-on-write storage.

**Trade-offs we accepted.**

- **World time instead of wall time:** realistic in structure, but services need time shims, and some behaviors (real timeouts) are approximations.
- **Simulated users are imperfect:** persona models can be unrealistic or exploitable. They're reviewed and part of the threat model.
- **Warm worlds cost memory** for faster startup on the most used templates.
- **Determinism is approximate:** replays reproduce task conditions, not every internal detail of every service.

**Open questions.**

- **How realistic is realistic enough?** Which details of a world (data volume, service quirks, user behavior) matter for transfer to real work, and which are expensive noise?
- **Co-training many agents:** when several policies train together in shared worlds, how should credit, stability and evaluation work?
- **Real users in worlds:** can human participants join worlds (as customers or colleagues) for evaluation, with the same isolation and recording?
- **Worlds across clusters:** can one world span clusters (for tasks about multi-region systems) without breaking isolation and timing?

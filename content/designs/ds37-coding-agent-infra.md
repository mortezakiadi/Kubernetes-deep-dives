---
id: design-coding-agents
title: "Design 37: Infrastructure for coding agents: identity, task environments, verification and a company knowledge base"
short: "Infrastructure for coding agents"
---
This design answers one question: *what infrastructure lets an engineering team trust its coding agents with more, so that engineers move from watching every command to reviewing agent-written pull requests in the morning?* Every team that uses coding agents sits somewhere on that curve. What moves a team along it is how far engineers can trust the agent's judgment, and an agent's judgment is only as good as two things: the context it starts with and its ability to check its own work. Both are usually missing. Context is spread across the monorepo, design docs and Slack threads. The builds, test rigs and simulations an agent needs to check its work run on machines it can't reach.

The short version: *treat each agent task as a first-class user of the infrastructure, give it a place to run and a way to verify, and give it the company's context in a form it can use*. Every task gets its own identity, derived from the engineer who asked for it, and reaches exactly what the task needs and nothing else. Every action it takes lands in a trail a person can audit. Tasks run in short-lived VMs that exist for one task, or in sandboxes with no network for long runs. A verification broker lets the agent run the same builds, tests and simulations an engineer would, on the machines that have them, and returns evidence tied to the exact commit. A knowledge base built from the monorepo, design docs and Slack gives each task a starting pack and a place to ask for more, with every fact carrying its source and its date. When an agent asserts something confidently wrong, the harness changes so the same mistake is caught next time.

This builds on two earlier designs. [Design 01](#design-sandboxing) covers the sandbox isolation tiers, the egress gateway and the credential broker in depth; this design reuses them and adds what coding agents need on top. [Design 35](#design-ai-for-ml-rnd) covers research agents with tiers and permissions; this design is about the engineering team's own day-to-day coding agents.

### The use case and requirements

**Scenario.** A company of 140 engineers runs about 2,300 coding-agent tasks a week. Most run on laptops, under the engineer's own credentials, with the engineer approving commands one at a time. The few teams that let agents open pull requests on their own find that 31% of those PRs merge without changes. The rest are wrong in ways a reviewer has to dig for: a test that was never run, a config flag the agent believed existed, a pattern the team stopped using a year ago in a Slack thread nobody linked. When an agent deleted a staging bucket last month, nobody could tell from the logs whether a person or an agent did it, because both used the same role.

![Infrastructure for coding agents](diagrams/designs/concepts/ds37-c1.svg)

**What the system must deliver.**

- **An identity per agent task:** every task has an identity of its own, tied to the engineer who started it, visible in every system it touches.
- **Least privilege by task:** a task reaches the repositories, paths, cloud resources, tools and network hosts its task needs, for as long as the task runs, and nothing else.
- **An audit trail:** for any change in any system, a person can find which task did it, who asked for the task, what the agent saw and why it acted.
- **Environments per task:** a clean, short-lived VM for each task, ready in seconds with the monorepo and build caches in place, and a sandbox with no network at all for long, unattended runs.
- **Verification the agent can reach:** the same builds, tests, simulations and hardware rigs an engineer uses, reachable from a task under its own identity, with results recorded as evidence.
- **A company knowledge base:** context from the monorepo, design docs and Slack, structured for agents, with sources, owners, dates and status on every entry.
- **A harness that learns:** confident wrong claims are measured, and each one found by a reviewer turns into a check, a knowledge base entry or a tool.

**Out of scope:** which coding agent or model the company uses, the isolation internals of sandboxes ([Design 01](#design-sandboxing)) and durable agent state across restarts ([Design 04](#design-agent-state)). This design is about what the infrastructure gives the agent and how far the team can trust what comes back.

### The trust curve: what an agent may do on its own

**Scenario.** One team lets agents push to branches and open PRs overnight; another still approves every `ls`. Both claim to be cautious. Neither can say what evidence would let it move one step, so the cautious team never moves and the bold team got there by accident.

![The trust curve](diagrams/designs/concepts/ds37-c2.svg)

![The trust curve: what an agent may do on its own](diagrams/designs/ds37-1.svg)

**What the picture shows.** Autonomy is set per task class, not per team, and it moves on measured evidence.

- **Five levels:** L0, the engineer approves every command. L1, the agent works freely inside its task environment and the engineer approves anything that leaves it. L2, the agent opens a PR and a person reviews it. L3, the agent's PR merges after a lighter review once its verification evidence passes. L4, a narrow class of changes merges on evidence alone, with a person reading a daily digest.
- **Task classes:** a task class is a kind of change in a part of the codebase, such as "dependency bump in a service", "flaky test fix" or "schema migration". The level belongs to the class. A team can run dependency bumps at L3 while schema migrations stay at L2.
- **What moves a class up:** the share of the class's PRs merged without changes, the share of them whose verification evidence was complete, and the absence of reverts and incidents over a minimum number of tasks.
- **What moves a class down:** any incident traced to an agent change in the class, a revert, or a drop in the merged-without-changes rate. Moving down is automatic; moving up is proposed automatically and approved by the code owners.
- **The infrastructure sets the ceiling:** a class can't go above L1 without a task identity, above L2 without the audit trail, or above L3 without complete verification evidence. The rest of this design is what lifts those ceilings.

**The code.** Proposing a level change for a task class from its record, using a lower confidence bound so a few lucky tasks don't move a class (Python).

```python
from dataclasses import dataclass
from math import sqrt

@dataclass
class ClassRecord:
    name: str
    level: int
    tasks: int                 # agent PRs in the window
    merged_unchanged: int      # merged with no human edits
    evidence_complete: int     # every claim in the PR backed by recorded evidence
    reverts: int
    incidents: int

def wilson_low(k: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 0.0
    p = k / n
    return (p + z * z / (2 * n) - z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)

NEEDED = {2: 0.60, 3: 0.80, 4: 0.92}        # merged-unchanged lower bound to reach a level
MIN_TASKS = {2: 30, 3: 60, 4: 150}

def propose(r: ClassRecord) -> str:
    if r.incidents or r.reverts > r.tasks * 0.02:
        return f"{r.name}: down to L{max(r.level - 1, 0)} (incidents {r.incidents}, reverts {r.reverts})"
    nxt = r.level + 1
    if nxt not in NEEDED or r.tasks < MIN_TASKS[nxt]:
        return f"{r.name}: stay at L{r.level} ({r.tasks} tasks)"
    ok = wilson_low(r.merged_unchanged, r.tasks) >= NEEDED[nxt] \
        and wilson_low(r.evidence_complete, r.tasks) >= 0.95                           #!
    return f"{r.name}: {'propose L' + str(nxt) if ok else 'stay at L' + str(r.level)}"

for r in [ClassRecord("dependency bump", 2, 212, 193, 210, 1, 0),
          ClassRecord("flaky test fix", 2, 74, 51, 70, 0, 0),
          ClassRecord("schema migration", 2, 41, 33, 41, 0, 1)]:
    print(propose(r))
```

**Going deeper.**

- **Merged unchanged is a proxy, not the goal:** a reviewer who rubber-stamps makes it look good. It is paired with the revert and incident rate over the following 30 days, which a rubber-stamp can't hide.
- **Levels describe the review, not the agent:** the same agent and model run at every level. What changes is how much of the checking the infrastructure does before a person looks.

### An identity for every agent task

**Scenario.** The staging bucket was deleted by `role/eng-dev`, the same role every engineer and every agent on a laptop used. CloudTrail showed the role and an IP address. It took two days to find out it was an agent working on a cleanup task, started by an engineer who had asked it to "remove the unused test buckets".

![Agent identity](diagrams/designs/concepts/ds37-c3.svg)

![An identity for every agent task](diagrams/designs/ds37-2.svg)

**What the picture shows.** Each task gets an identity that names both the agent task and the engineer it acts for, and every downstream system sees both.

- **The task is the principal:** when an engineer starts a task, the task service creates a task ID (`agt-7f3a9c`) with its owner, task class and expiry. The task, not the agent product and not the engineer, is what gets authorized.
- **Delegation, not impersonation:** the task's token is a token exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)) of the engineer's session for a narrower one. The engineer stays the subject (`sub`) and the task appears as the actor (`act`), so a service can authorize the engineer and record the agent in the same request.
- **Inside Kubernetes:** the task's pod runs under a ServiceAccount created for the task, with a projected token whose audience is the internal service it calls and which expires within the hour. Nothing in the pod holds a long-lived secret.
- **On AWS:** the credential broker assumes the agent role with the task ID as the `SourceIdentity`. AWS keeps that value for the whole role session, including roles assumed from it, and it appears in CloudTrail on every call ([AWS: monitor actions taken with assumed roles](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp_control-access_monitor.html)).
- **On GitHub:** the agent commits and opens PRs as a GitHub App, with an installation token limited to the task's repository and permissions, which expires after one hour ([GitHub: installation access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)). Commits carry a `Task:` and an `On-behalf-of:` trailer.
- **Agents can't approve themselves:** the agent's identity is never a code owner, can't approve a PR and can't merge outside the rules of its task class's level.

**The code.** The token exchange request the task service sends, and the claims a downstream service reads (Python).

```python
import json, time

def exchange_request(engineer_token: str, task_token: str, audience: str, scopes: list[str]) -> dict:
    return {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": engineer_token,                                  # who the task acts for
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "actor_token": task_token,                                        # who is acting #!
        "actor_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "audience": audience,
        "scope": " ".join(scopes),
    }

issued = {                                                                # what the issued token carries
    "sub": "maya@example.com",
    "act": {"sub": "agent-task:agt-7f3a9c", "task_class": "dependency-bump"},
    "aud": "artifact-registry.internal",
    "scope": "read:packages",
    "exp": int(time.time()) + 3600,
}

def authorize(claims: dict, need_scope: str) -> tuple[bool, str]:
    actor = claims.get("act", {}).get("sub", "none")
    ok = need_scope in claims["scope"].split() and claims["exp"] > time.time()
    return ok, f"user={claims['sub']} actor={actor} scope={need_scope} -> {'allow' if ok else 'deny'}"

print(json.dumps(exchange_request("<engineer jwt>", "<task jwt>", "artifact-registry.internal", ["read:packages"]), indent=1))
print(authorize(issued, "read:packages")[1])
print(authorize(issued, "write:packages")[1])
```

**The YAML.** The task pod's ServiceAccount token, scoped to one audience and short-lived.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agt-7f3a9c                       # one per task, deleted with the task
  namespace: agent-tasks
  labels:
    agents.example.com/task: agt-7f3a9c
    agents.example.com/on-behalf-of: maya
---
# in the task pod spec
automountServiceAccountToken: false      # no default token with API access
volumes:
- name: task-identity
  projected:
    sources:
    - serviceAccountToken:
        audience: kb.internal            # only the knowledge base accepts it #!
        expirationSeconds: 3600
        path: kb-token
```

**Going deeper.**

- **One identity per task, not per agent product:** two tasks from the same engineer, running at the same time, get different identities. If one goes wrong, revoking it doesn't stop the other.
- **Laptop agents get the same identity:** a local agent asks the task service for a task identity too, and the engineer's CLI refuses to hand it the engineer's own credentials. That is what removes the shared-role problem from the scenario.

### Reaching exactly what the task needs

**Scenario.** The cleanup task needed to list buckets and delete ones with a `purpose=test` tag. It was given the engineer's full developer role because that was the only role available, and that role could delete any bucket in the account.

![Task-scoped access](diagrams/designs/concepts/ds37-c4.svg)

![Reaching exactly what the task needs](diagrams/designs/ds37-3.svg)

**What the picture shows.** A task manifest declares what the task needs; the grant compiler turns it into a narrow grant in each system, and anything more goes through an approval.

- **Task manifest:** every task class has a manifest template: repositories and paths it may change, cloud actions and resources, tools, network hosts, maximum duration and maximum spend. The engineer's request fills in the specifics, such as the service or the bucket tag.
- **Compiled per system:** the compiler produces a GitHub App token for the listed repositories, an IAM session policy for the listed actions and resources, an allowlist for the egress gateway ([Design 01](#design-sandboxing)), and the tool list the harness exposes. Each grant can only narrow what the engineer could do; it never adds to it.
- **Paths, not just repos:** in a monorepo, repository scope is too coarse. A pre-receive check rejects pushes from a task that touch paths outside its manifest, and CODEOWNERS for those paths still applies.
- **Asking for more:** when the agent needs something outside the manifest, it calls an `request_access` tool with a reason. The engineer approves or denies it from a notification, and the approval is recorded with the reason. The agent never sees a way to widen its own grant.
- **Destructive actions are separate:** deletes, production writes, permission changes and sends to people outside the company are never in a manifest at L0 to L3. They are proposed as a plan and executed by a person, or by the task after a person approves the exact plan.
- **What the cleanup task would have had:** `s3:ListAllMyBuckets` to build its plan. After the engineer approved the plan, the grant added `s3:DeleteBucket` only on buckets tagged `purpose=test`. The staging bucket had no such tag.

**The code.** Compiling a manifest into an IAM session policy and the `AssumeRole` arguments the broker uses (Python).

```python
import json

manifest = {
    "task": "agt-7f3a9c", "on_behalf_of": "maya", "class": "test-resource-cleanup",
    "aws": [{"actions": ["s3:ListAllMyBuckets"], "resources": ["*"]},
            {"actions": ["s3:DeleteBucket"], "resources": ["arn:aws:s3:::*"],     # added after plan approval
             "when": {"StringEquals": {"aws:ResourceTag/purpose": "test"}}}],                    #!
    "max_minutes": 60,
}

def session_policy(m: dict) -> str:
    stmts = []
    for g in m["aws"]:
        s = {"Effect": "Allow", "Action": g["actions"], "Resource": g["resources"]}
        if "when" in g:
            s["Condition"] = g["when"]
        stmts.append(s)
    return json.dumps({"Version": "2012-10-17", "Statement": stmts})

def assume_role_args(m: dict) -> dict:
    return {
        "RoleArn": "arn:aws:iam::111122223333:role/agent-task",
        "RoleSessionName": m["task"],
        "SourceIdentity": m["task"],                           # kept for the whole session #!
        "Tags": [{"Key": "on-behalf-of", "Value": m["on_behalf_of"]},
                 {"Key": "task-class", "Value": m["class"]}],
        "Policy": session_policy(m),                           # can only narrow the role
        "DurationSeconds": max(900, m["max_minutes"] * 60),
    }

args = assume_role_args(manifest)
print(json.dumps({k: v for k, v in args.items() if k != "Policy"}, indent=1))
print(json.dumps(json.loads(args["Policy"]), indent=1))
```

**Going deeper.**

- **A session policy can only narrow:** the effective permissions are the intersection of the agent role's policy and the session policy. The `agent-task` role itself has no destructive actions at all, so a compiler bug can't grant one.
- **Tag conditions need the resource to support them:** S3 general purpose buckets evaluate tag conditions only once attribute-based access control is enabled on the bucket, and it is off by default ([AWS: enabling ABAC on buckets](https://docs.aws.amazon.com/AmazonS3/latest/userguide/buckets-tagging-enable-abac.html)). The compiler refuses a tag condition on a resource type where the platform hasn't confirmed it is enforced.
- **Manifests are reviewed like code:** task class templates live in the monorepo, owned by the platform team and the owning team together. Adding an action to a template is a reviewed change, and the review sees how many tasks would get it.
- **Unused grants are a signal:** the audit trail shows which granted actions a task actually used. Grants a class never uses over 90 days are proposed for removal.

### A trail a person can audit

**Scenario.** A reviewer asks why an agent changed a retry limit from 3 to 10 in a payment client. The PR description says "to fix flakiness". Nobody can tell what the agent read, which test it ran or whether it saw the incident note that said the limit had been lowered on purpose.

![Audit trail](diagrams/designs/concepts/ds37-c5.svg)

![A trail a person can audit](diagrams/designs/ds37-4.svg)

**What the picture shows.** Every task leaves one trail that joins what the agent saw, what it did and what changed, keyed by the task ID.

- **Three sources, one key:** the harness records the agent's turns (model calls, tool calls, files read, commands run, knowledge base results). The gateways and brokers record every outbound request and credential use. The external systems record the effect: CloudTrail with the `SourceIdentity`, GitHub audit logs with the App and task trailer, Kubernetes audit logs with the task ServiceAccount.
- **Joined by task ID:** a trail view shows all three in time order. From any CloudTrail event, a commit or a Kubernetes change, one click goes to the task and its transcript.
- **Tamper-evident:** trail records are written to an append-only store, each record carrying a hash of the previous one, and the chain head is written to a bucket with object lock every hour. The task itself has no write access to the trail.
- **Readable, not only complete:** the trail view groups the transcript into steps with the evidence for each claim in the final report ([Design 34](#design-agent-inspection) covers reading long transcripts with agent help).
- **Retention:** trails are kept for one year, transcripts with secrets redacted at write time by the harness, not at read time.
- **What the reviewer would have seen:** the agent searched for "retry limit", got no knowledge base result for the incident note because it had not been ingested, and ran the client's unit tests but not the integration test that exercises the payment provider's rate limit.

**The code.** Writing and verifying a hash-chained trail (Python).

```python
import hashlib, json, time

def append(trail: list[dict], task: str, source: str, event: dict) -> None:
    prev = trail[-1]["hash"] if trail else "0" * 64
    rec = {"task": task, "ts": time.time(), "source": source, "event": event, "prev": prev}
    rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()    #!
    trail.append(rec)

def verify(trail: list[dict]) -> int | None:
    """Return the index of the first record that doesn't match its hash or its predecessor."""
    prev = "0" * 64
    for i, r in enumerate(trail):
        body = {k: v for k, v in r.items() if k != "hash"}
        if r["prev"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != r["hash"]:
            return i
        prev = r["hash"]
    return None

t: list[dict] = []
append(t, "agt-7f3a9c", "harness", {"tool": "kb.search", "query": "payment client retry limit", "hits": 0})
append(t, "agt-7f3a9c", "harness", {"tool": "run", "cmd": "bazel test //payments/client:unit"})
append(t, "agt-7f3a9c", "github", {"action": "pull_request.opened", "repo": "monorepo", "pr": 48211})
print("chain ok" if verify(t) is None else f"broken at {verify(t)}")
t[1]["event"]["cmd"] = "bazel test //payments/..."            # someone edits the record later
print("chain ok" if verify(t) is None else f"broken at {verify(t)}")
```

**Going deeper.**

- **The transcript is the evidence for intent:** external logs say what happened. Only the transcript says why the agent thought it should, which is what a reviewer and a postmortem need.
- **Redaction has to happen before storage:** a trail that stores secrets becomes the most valuable target in the company. The harness replaces credentials it injected and values matching secret patterns before the record leaves the task environment.

### Short-lived VMs: one task, one machine

**Scenario.** Agents on laptops left state behind: a global `npm` package, a modified `~/.gitconfig`, a background process still holding port 8080. Engineers stopped trusting their own machines after a long agent session and some reinstalled their toolchains every few weeks.

![Task VMs](diagrams/designs/concepts/ds37-c6.svg)

![Short-lived VMs: one task, one machine](diagrams/designs/ds37-5.svg)

**What the picture shows.** Each task runs in a microVM that is created for it, holds a ready monorepo checkout and is destroyed when the task ends.

- **One microVM per task:** tasks run as sandboxes on the microVM tier of [Design 01](#design-sandboxing), so each task has its own guest kernel. Two tasks never share a file system, a process table or a Docker daemon.
- **Ready in seconds:** the monorepo is large, so a fresh clone per task is too slow. Each node keeps a read-only mirror of the monorepo that is fetched every few minutes, and the task's workspace is a shared clone of that mirror (`git clone --shared`), which reads the mirror's objects in place instead of copying them. The remote build cache ([see the verification section](#ds37-7)) supplies build outputs, so the first build reuses work other tasks and CI already did.
- **Warm pools by task class:** each task class has a template (base image, toolchains, limits, network policy). A warm pool keeps booted VMs per template, sized from the hour-of-day arrival rate.
- **Attached from anywhere:** the engineer can open the task's VM from the browser or attach a local editor through the sandbox router, so watching an L0 or L1 task feels like working locally.
- **Destroyed at the end:** when the task ends or its time limit passes, the VM, its workspace and its credentials are deleted. What survives is the branch, the PR, the trail and any artifacts the task uploaded through the broker.

**The YAML.** The template for monorepo tasks, on the agent sandbox CRDs from [Design 01](#design-sandboxing).

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: monorepo-task
  namespace: agent-tasks
spec:
  podTemplate:
    metadata:
      annotations:
        karpenter.sh/do-not-disrupt: "true"
    spec:
      runtimeClassName: isolated-sandbox          # one microVM per task #!
      automountServiceAccountToken: false
      initContainers:
      - name: workspace
        image: registry.example.com/agents/git@sha256:...
        command: ["sh", "-c", "git clone --shared --no-checkout /mirror /workspace/repo && git -C /workspace/repo checkout --detach $TASK_COMMIT"]   #!
        volumeMounts:
        - { name: mirror, mountPath: /mirror, readOnly: true }   # tasks never write to it #!
        - { name: workspace, mountPath: /workspace }
      containers:
      - name: task
        image: registry.example.com/agents/monorepo-dev@sha256:...   # toolchains pinned by digest
        env:
        - { name: BAZEL_REMOTE_CACHE, value: grpcs://build-cache.internal:443 }
        - { name: HTTPS_PROXY, value: http://egress-gateway.sandbox-egress:3128 }
        resources:
          requests: { cpu: "4", memory: 16Gi, ephemeral-storage: 40Gi }
          limits:   { cpu: "8", memory: 32Gi, ephemeral-storage: 80Gi }
        volumeMounts:
        - { name: workspace, mountPath: /workspace }
      volumes:
      - name: mirror
        hostPath: { path: /var/lib/monorepo-mirror, type: Directory }   # one mirror per node
      - name: workspace
        emptyDir: { sizeLimit: 80Gi }
```

**The code.** Sizing a template's warm pool by hour from arrivals and boot time (Python).

```python
from math import ceil

arrivals_per_hour = {8: 40, 9: 210, 10: 260, 11: 240, 13: 230, 14: 250, 15: 220, 16: 180, 17: 120, 22: 60}
boot_s, headroom = 75, 1.5                        # VM boot plus worktree plus first cache warmup

def pool_size(per_hour: int) -> int:
    in_flight = per_hour / 3600 * boot_s          # Little's law: claims arriving while replacements boot #!
    return max(2, ceil(in_flight * headroom))

for h in sorted(arrivals_per_hour):
    print(f"{h:02d}:00  {arrivals_per_hour[h]:3d} tasks/h -> keep {pool_size(arrivals_per_hour[h])} warm")
```

**Going deeper.**

- **The shared mirror is a trust boundary:** the mirror is mounted read-only, and a shared clone keeps its own refs, index and new objects on the task's volume. If the mirror were writable, one task could plant a commit another task builds.
- **Docker inside the task:** tests that start containers need a container runtime inside the VM. The guest kernel makes this safe to allow, which is one of the main reasons coding tasks get microVMs rather than containers.

### No-egress sandboxes for long runs

**Scenario.** A task to migrate 140 services off a deprecated logging library ran for nine hours overnight. Halfway through, it hit a test that downloaded a fixture from a public URL, and the agent, unable to reach it, "fixed" the test by removing the assertion.

![No egress](diagrams/designs/concepts/ds37-c7.svg)

![No-egress sandboxes for long runs](diagrams/designs/ds37-6.svg)

**What the picture shows.** Long, unattended runs happen in a sandbox with no network device, with everything the run needs brought in before it starts, and the model calls made from outside.

- **No network inside:** the long-run template uses the network-isolated microVM tier from [Design 01](#design-sandboxing). There is nothing to filter and nothing to exfiltrate through, even if the agent reads a prompt injection in a vendored file.
- **The harness stays outside:** the agent loop, including model calls, runs in a harness pod outside the sandbox. It sends commands and files in through the sandbox router and reads results back. The sandbox holds code and tools, not credentials or model access.
- **Dependencies brought in first:** a pre-flight step resolves every lockfile in the paths the task covers, fetches each package from the internal mirror by hash and places them in a read-only volume. A run that would need something missing fails in the first minute, not at hour six.
- **Checkpointed:** the workspace is snapshotted every 30 minutes and at every commit ([Design 04](#design-agent-state)), so a lost node costs at most half an hour.
- **Network-needing tests are named:** a test that needs the network is a test the long run can't verify. The harness marks it as "not run" in the evidence, and the agent is instructed and checked to never edit an assertion to make a test pass without the network. The scenario's fix becomes a flagged claim, not a silent change.
- **Budgets:** each long run has a time limit, a model spend limit and a maximum number of files changed. Reaching any of them stops the run with a report, not with a partial PR.

**The YAML.** The long-run template: no network device, dependencies mounted read-only.

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: monorepo-long-run
  namespace: agent-tasks
spec:
  podTemplate:
    metadata:
      annotations:
        sandbox.amazonaws.com/network-isolated: "true"     # no network device at all #!
    spec:
      runtimeClassName: isolated-sandbox
      automountServiceAccountToken: false
      containers:
      - name: task
        image: registry.example.com/agents/monorepo-dev@sha256:...
        env:
        - { name: BAZEL_OFFLINE, value: "1" }
        volumeMounts:
        - { name: workspace, mountPath: /workspace }
        - { name: deps, mountPath: /deps, readOnly: true }           # pre-fetched by hash #!
  volumeClaimTemplates:
  - metadata: { name: workspace }
    spec: { accessModes: ["ReadWriteOnce"], resources: { requests: { storage: 100Gi } } }
  - metadata: { name: deps }
    spec: { accessModes: ["ReadOnlyMany"], resources: { requests: { storage: 50Gi } } }
```

**The code.** The pre-flight check: every locked dependency must already be in the bundle, by hash (Python).

```python
import hashlib

def locked(lockfiles: dict[str, list[tuple[str, str]]]) -> set[tuple[str, str]]:
    return {dep for deps in lockfiles.values() for dep in deps}

def preflight(lockfiles: dict[str, list[tuple[str, str]]], bundle: dict[str, bytes]) -> list[str]:
    """lockfiles: path -> [(package, sha256)]; bundle: sha256 -> bytes fetched from the mirror."""
    problems = []
    for pkg, digest in sorted(locked(lockfiles)):
        blob = bundle.get(digest)
        if blob is None:
            problems.append(f"missing {pkg}")
        elif hashlib.sha256(blob).hexdigest() != digest:
            problems.append(f"hash mismatch {pkg}")                                              #!
    return problems

good = b"structlog-24.1.0 wheel"
h = hashlib.sha256(good).hexdigest()
lockfiles = {"services/billing/requirements.lock": [("structlog==24.1.0", h)],
             "services/search/requirements.lock": [("structlog==24.1.0", h), ("orjson==3.10.7", "ab" * 32)]}
print(preflight(lockfiles, {h: good}) or "all dependencies present")
```

**Going deeper.**

- **Why not an allowlist instead:** an allowlisted package mirror still lets a long, unwatched run fetch something new at hour six, and every host on an allowlist is a path out. With nothing to reach, the run's inputs are fixed at the start and recorded in the trail.
- **Some long runs need the network:** a run that must call internal APIs runs on the egress-gateway tier with a fixed allowlist instead. That choice is part of the task class, not something the agent picks.

### Verification: builds, tests and simulations the agent can reach

**Scenario.** The robotics team's agent PRs always said "tests pass". They did, on the unit tests that run on a laptop. The motion-planning simulations need GPUs in a separate cluster, and the hardware-in-the-loop tests run on six rigs in the lab. Agents could reach neither, so every robotics PR needed an engineer to run the real checks by hand, which is most of the work the agent was meant to save.

![Verification](diagrams/designs/concepts/ds37-c8.svg)

![Verification: builds, tests and simulations the agent can reach](diagrams/designs/ds37-7.svg)

**What the picture shows.** A verification broker lets a task run any check an engineer can run, on the machine that has it, under the task's identity, and returns evidence tied to the exact commit.

- **One door for every check:** the task calls `verify(target, commit)`. The broker knows where each kind of target runs: the remote build and test farm, the simulation cluster or the hardware rig queue. The agent doesn't need network access to any of them.
- **Builds and tests through remote execution:** builds and tests run on the remote execution farm that CI uses, through the [Remote Execution API](https://github.com/bazelbuild/remote-apis). The task's VM sends inputs by content hash and gets results and logs back, so an agent's build costs what an engineer's build costs, and cached actions don't run again.
- **Simulations as queued jobs:** simulation targets are submitted as jobs to the GPU cluster through Kueue, in a queue for agent work. It has its own quota and borrows idle capacity, but it can't take capacity from engineers' queues ([Design 17](#design-gpu-scheduling)).
- **Hardware rigs as a reservation:** rig tests go into the rig scheduler's queue with a priority below humans. The rig runs the test and uploads the results. Nobody gives the agent a shell on the rig.
- **Evidence, not text:** every check returns a signed evidence record: target, commit, the hash of the inputs, the result, the log location and the runner. The PR shows the evidence records, and a reviewer reads results from them, not from the agent's description.
- **What the robotics team got:** simulations and rig tests run on every agent PR before review. Their L2 PRs went from 12% merged unchanged to 58%, because the checks a reviewer used to run had already run.

**The YAML.** A ClusterQueue for agent simulation jobs, which borrows idle GPUs from the cohort but never takes from engineers.

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: agent-sim
spec:
  cohort: sim-gpus
  namespaceSelector:
    matchLabels: { agents.example.com/kind: verification }
  preemption:
    reclaimWithinCohort: Never              # agents never take back from others #!
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: l40s
      resources:
      - { name: "cpu", nominalQuota: 64 }
      - { name: "memory", nominalQuota: 512Gi }
      - name: "nvidia.com/gpu"
        nominalQuota: 4                     # guaranteed share for agent checks
        borrowingLimit: 12                  # may use idle GPUs up to this #!
```

**The code.** Checking a PR's claims against its evidence for the commit being merged (Python).

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Evidence:
    target: str
    commit: str
    result: str          # "pass" | "fail" | "not-run"
    runner: str

def check_pr(head: str, required: list[str], claimed_pass: list[str], evidence: list[Evidence]) -> list[str]:
    by_target = {e.target: e for e in evidence if e.commit == head}          # old commits don't count #!
    issues = []
    for t in required:
        e = by_target.get(t)
        if e is None:
            issues.append(f"{t}: no evidence for {head[:8]}")
        elif e.result != "pass":
            issues.append(f"{t}: {e.result} on {e.runner}")
    for t in claimed_pass:
        if t not in by_target or by_target[t].result != "pass":
            issues.append(f"claim without evidence: '{t} passes'")
    return issues

head = "9c1e44ab07"
ev = [Evidence("//planning:unit", head, "pass", "rbe"),
      Evidence("//planning:sim_corridor", "51aa02f3c1", "pass", "sim-cluster"),   # older commit
      Evidence("//planning:hil_arm_reach", head, "not-run", "rig-queue")]
for line in check_pr(head, ["//planning:unit", "//planning:sim_corridor", "//planning:hil_arm_reach"],
                     ["//planning:unit", "//planning:sim_corridor"], ev):
    print(line)
```

**Going deeper.**

- **The check list comes from the change, not the agent:** which targets are required is computed from the files changed (the build graph's reverse dependencies, plus rules per path). The agent can run more, but can't choose to run fewer.
- **Rig time is scarce, so it's spent last:** a rig test is queued only after unit tests and simulations pass on the same commit. A PR that fails in simulation never uses a rig.
- **Flaky checks poison trust both ways:** an agent that retries a flaky test until it passes learns nothing, and a reviewer who sees "pass after 4 tries" learns less. Retries are recorded in the evidence, and known-flaky targets are listed in the knowledge base with their owners.

### The knowledge base: pulling context from the monorepo, design docs and Slack

**Scenario.** The payment client's retry limit had been lowered from 10 to 3 after an incident, because the payment provider's rate limiter banned clients that retried hard. The decision was in a Slack thread in `#payments-oncall` and a line in the incident doc. Neither was linked from the code, and the agent had no way to find either.

![Knowledge base](diagrams/designs/concepts/ds37-c9.svg)

![The knowledge base: pulling context from the monorepo, design docs and Slack](diagrams/designs/ds37-8.svg)

**What the picture shows.** Connectors pull from each source, keep who may read what, and turn raw material into entries with a source, an owner, a date and a status.

- **The monorepo:** READMEs, architecture decision records, CODEOWNERS, build targets and their dependencies, public interfaces, and the commit history of each directory. Indexed at every merge to main, so the knowledge base never describes code that is gone.
- **Design docs:** docs from the design doc folders, with their status (draft, approved, superseded), authors, reviewers and the code paths they mention. A doc marked superseded points to its replacement.
- **Slack:** public engineering channels only, by thread rather than by message, since a thread is the unit that holds a question and its answer. Private channels and direct messages are never ingested.
- **Incidents and runbooks:** incident docs and their action items, linked to the services and paths they concern.
- **Decisions extracted, then confirmed:** a pipeline reads threads and docs for decisions ("we lowered the retry limit to 3 because..."), drafts a decision entry with links to its sources and the paths it affects, and sends it to the code owner of those paths to confirm. Unconfirmed drafts are searchable but marked as such.
- **Permissions kept:** each entry carries the read permissions of its source. A task's queries are answered with the permissions of the engineer it acts for, never with more.

**The code.** An entry, and the order search results are ranked in when sources disagree (Python).

```python
from dataclasses import dataclass, field
from datetime import date

@dataclass
class Entry:
    id: str
    text: str
    kind: str                     # decision | doc | readme | thread | incident
    status: str                   # confirmed | approved | draft | superseded
    source: str                   # URL or repo path
    owner: str
    updated: date
    paths: list[str] = field(default_factory=list)
    readers: set[str] = field(default_factory=lambda: {"eng"})

STATUS = {"confirmed": 3, "approved": 3, "draft": 1, "superseded": 0}

def rank(entries: list[Entry], path: str, groups: set[str], today: date) -> list[Entry]:
    visible = [e for e in entries if e.readers & groups]                           # never more than the engineer #!
    def score(e: Entry) -> tuple:
        on_path = any(path.startswith(p) for p in e.paths)
        age_days = (today - e.updated).days
        return (STATUS[e.status], on_path, -age_days)
    return sorted(visible, key=score, reverse=True)

es = [Entry("d-118", "Retry limit lowered to 3: provider bans clients that retry hard.", "decision", "confirmed",
            "slack://payments-oncall/p1717000000", "payments", date(2026, 6, 3), ["payments/client/"]),
      Entry("r-9", "Client retries up to 10 times with backoff.", "readme", "approved",
            "payments/client/README.md", "payments", date(2025, 2, 11), ["payments/client/"]),
      Entry("t-77", "Should we raise retries for the new provider?", "thread", "draft",
            "slack://payments/p1730000000", "payments", date(2026, 10, 1), ["payments/"])]
for e in rank(es, "payments/client/retry.go", {"eng"}, date(2026, 11, 22)):
    print(f"{e.id:6s} {e.status:10s} {e.updated}  {e.text}")
```

**Going deeper.**

- **The README was the wrong answer, and it was approved:** stale docs are the most common source of confidently wrong agent claims. When a confirmed decision contradicts an approved doc on the same path, the conflict goes to the path's owner as a doc fix, and both entries show the conflict until it's resolved.
- **Slack is evidence, not truth:** a thread records what people said, including guesses that were wrong. Threads are searchable, but only a confirmed decision is presented to an agent as a fact.
- **Ingest less, link more:** the knowledge base doesn't copy every doc into one big index. It keeps entries short, with a link to the source, so an agent that needs the full doc reads the current version.

### Context for agents: the task pack and the place to ask

**Scenario.** The first version of the knowledge base was a search box. Agents rarely used it, because nothing told them that the answer to "why is the retry limit 3" existed, and they didn't know to ask. The second version pasted the top 50 search results into every prompt, and agents started following a superseded design doc because it was the longest entry.

![Task pack](diagrams/designs/concepts/ds37-c10.svg)

![Context for agents: the task pack and the place to ask](diagrams/designs/ds37-9.svg)

**What the picture shows.** Every task starts with a small pack chosen for the paths it will touch, and the agent has tools to ask for more.

- **The task pack:** built when the task starts, from the paths in its manifest. It contains the owners of those paths, the confirmed decisions and known pitfalls that apply to them, how to build and verify the affected targets, the conventions the team uses there, and recent incidents that touched them.
- **Small on purpose:** the pack has a budget, about 6,000 tokens. Entries are chosen by status, then by how closely they match the paths, then by date. Superseded entries are never in the pack.
- **Each item with its source:** every line in the pack carries its entry ID, source link and date, so the agent can cite it and a reviewer can check it.
- **Tools to ask:** the harness exposes the knowledge base as tools over [MCP](https://modelcontextprotocol.io): search, fetch an entry, find the owner of a path, list decisions for a path, and how to verify a target. The answers are filtered by the engineer's permissions.
- **Asking a person:** when the knowledge base has no answer and the question matters, the agent drafts a question to the path's owner instead of guessing. The answer, once given, becomes a decision entry.
- **The result for the payment client:** the retry decision is in the pack for any task touching `payments/client/`, with its source. The agent that wanted to raise the limit would have read it before writing a line.

**The code.** Assembling a task pack under a token budget (Python).

```python
from dataclasses import dataclass

@dataclass
class Item:
    id: str
    kind: str          # owner | decision | pitfall | verify | convention | incident
    status: str
    path: str
    tokens: int
    text: str

PRIORITY = {"owner": 0, "decision": 1, "pitfall": 1, "verify": 2, "incident": 3, "convention": 4}

def pack(items: list[Item], task_paths: list[str], budget: int = 6000) -> list[Item]:
    def match(i: Item) -> int:
        return max((len(i.path) for p in task_paths if p.startswith(i.path)), default=-1)
    cands = [i for i in items if i.status != "superseded" and match(i) >= 0]            #!
    cands.sort(key=lambda i: (PRIORITY[i.kind], -match(i)))
    out, used = [], 0
    for i in cands:
        if used + i.tokens <= budget:
            out.append(i); used += i.tokens
    return out

items = [Item("o-1", "owner", "confirmed", "payments/", 40, "owners: @payments-core"),
         Item("d-118", "decision", "confirmed", "payments/client/", 180, "retry limit is 3, provider bans hard retries"),
         Item("v-4", "verify", "approved", "payments/client/", 220, "//payments/client:integration needs the provider stub"),
         Item("doc-31", "decision", "superseded", "payments/", 4200, "2024 design: retry up to 10"),
         Item("c-2", "convention", "approved", "payments/", 900, "errors wrap with %w, no panics in handlers"),
         Item("x-5", "pitfall", "confirmed", "search/", 300, "index rebuild locks the shard")]
for i in pack(items, ["payments/client/retry.go"]):
    print(f"{i.id:7s} {i.kind:10s} {i.text}")
```

**Going deeper.**

- **Measure the pack by what agents needed:** for every reviewer correction, the harness checks whether the fact needed was in the pack, in the knowledge base but not the pack, or nowhere. The first means the agent ignored it, the second means the pack's selection is wrong, and the third means an entry is missing.
- **Agents write to the knowledge base too, through a person:** when a task discovers something worth keeping ("this test needs the provider stub running"), it proposes an entry with the evidence. The owner confirms it like any other.

### Catching confident wrong claims and fixing the harness

**Scenario.** In one week, reviewers found an agent claiming a config flag `--max-inflight` existed (it was removed two versions ago), claiming "no other callers" for a function with three callers in another directory, and claiming tests passed on a commit that came before its last change. Each claim was stated as fact. None was checked by anything before a person read it.

![Confident wrong claims](diagrams/designs/concepts/ds37-c11.svg)

![Catching confident wrong claims and fixing the harness](diagrams/designs/ds37-10.svg)

**What the picture shows.** Claims in an agent's report are checked against evidence before review, and every wrong claim a reviewer finds changes the harness.

- **Claims are extracted:** the harness asks the agent to end every task with a list of claims, each with its evidence: a command run with its trail ID, a file and line read, a knowledge base entry or a verification record. A separate checker also extracts claims from the PR text, so claims the agent didn't list are caught.
- **Claims are checked:** "tests pass" is checked against the evidence for the head commit. "No other callers" is checked by running the reference search on the whole monorepo. "Flag X exists" is checked against the pinned version's source or docs. A claim with no evidence is marked "unverified" in the PR, in red.
- **Confident wrong claims are counted:** a wrong claim stated without a hedge, found by a check or a reviewer, is counted per 100 tasks, per task class and per harness version. That number is reported weekly next to the merged-unchanged rate.
- **Every finding changes the harness:** a reviewer marks a wrong claim with one click. Each one is triaged into a fix: a new automatic check, a knowledge base entry, a tool (for example, a tool that returns a flag's definition at the pinned version), or a prompt change. The finding and its fix are linked.
- **Fixes are tested on recorded tasks:** the harness has an evaluation set of past tasks with known wrong claims. A harness change ships only if it catches the claims it targets and doesn't raise the wrong-claim rate elsewhere ([Design 33](#design-continuous-evals) covers regression gates).
- **What the week's three claims became:** a check that resolves flags against the pinned dependency's source, a rule that "no callers" claims must cite a monorepo-wide reference search, and the head-commit rule on evidence. Over the next month, confident wrong claims fell from 4.1 to 1.3 per 100 tasks.

**The code.** Checking a report's claims against evidence and the tools that can confirm them (Python).

```python
import re

REPO_CALLERS = {"billing.ChargeOnce": ["billing/api/charge.go", "jobs/retry/charge.go", "admin/refund.go"]}
PINNED_FLAGS = {"envoy@1.33": {"--concurrency", "--drain-time-s"}}

def check(claim: dict, head: str, evidence: dict[str, dict]) -> str:
    ev = evidence.get(claim.get("evidence", ""))
    text = claim["text"]
    if m := re.match(r"no other callers of (\S+)", text):
        callers = REPO_CALLERS.get(m.group(1), [])
        others = [c for c in callers if c != claim.get("file")]
        return "ok" if not others else f"WRONG: {len(others)} other callers, e.g. {others[0]}"   #!
    if m := re.match(r"flag (\S+) exists in (\S+)", text):
        return "ok" if m.group(1) in PINNED_FLAGS.get(m.group(2), set()) else f"WRONG: {m.group(1)} not in {m.group(2)}"
    if text.startswith("tests pass"):
        if ev is None:
            return "unverified: no evidence"
        return "ok" if ev["commit"] == head and ev["result"] == "pass" else f"WRONG: evidence is for {ev['commit']}"
    return "unverified: no checker for this claim"

head = "e41b0c2"
evidence = {"run-88": {"commit": "a90d113", "result": "pass"}}
claims = [{"text": "no other callers of billing.ChargeOnce", "file": "billing/api/charge.go"},
          {"text": "flag --max-inflight exists in envoy@1.33"},
          {"text": "tests pass for //billing/...", "evidence": "run-88"},
          {"text": "this change is backwards compatible"}]
for c in claims:
    print(f"{check(c, head, evidence):45s} <- {c['text']}")
```

**Going deeper.**

- **Ask for evidence, not confidence:** asking the model how sure it is doesn't help much; asking it to point to the line, the command or the record does, because a claim it can't support is visible as one.
- **Some claims can't be checked automatically:** "backwards compatible" needs judgment. The harness doesn't pretend; it marks the claim unverified and lists it first for the reviewer, which is where a reviewer's time is best spent.
- **Track by harness version:** the wrong-claim rate is the harness team's main quality number. A model upgrade or a prompt change that raises it is rolled back like any other regression.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds37-c12.svg)

- **Phase 1, identity and the trail (weeks 1–6):** the task service, task identities through token exchange, the agent GitHub App, `SourceIdentity` on the AWS role, the CLI that refuses to hand engineer credentials to agents, and the joined trail view. Laptop agents move to task identities first.
- **Phase 2, task environments (weeks 4–12):** the monorepo task template on microVMs, the node mirror and shared clones, the remote build cache, warm pools by task class, then the no-egress long-run template with its pre-flight.
- **Phase 3, verification (weeks 8–16):** the verification broker, remote execution for builds and tests, the agent simulation queue, the rig queue and evidence records on every PR.
- **Phase 4, knowledge (weeks 10–20):** connectors for the monorepo, design docs, incidents and public Slack channels, decision extraction with owner confirmation, the task pack and the MCP tools.
- **Phase 5, the trust loop (weeks 14–24):** claim checks, the wrong-claim counter, reviewer one-click findings, the evaluation set of recorded tasks, task classes with levels and the automatic proposals to move them.

**Trade-offs we accepted.**

- **Less convenience at first:** agents on laptops can no longer borrow the engineer's credentials. Some tasks that used to just work need a manifest or an access request until their task class templates catch up.
- **Remote environments cost money:** microVMs, warm pools and a build farm for agents cost more than laptops that were already paid for. The case rests on reviewer time saved and on incidents that can be traced in minutes, both of which are measured.
- **No-egress runs are less capable:** some long tasks can't run without the network and run on the allowlist tier instead, with a larger attack surface.
- **The knowledge base needs owners' time:** decision confirmations and doc conflict fixes land on code owners. Without that time, the knowledge base drifts back to being a search box over stale text.
- **Ingesting Slack changes how people write:** some engineers will write less candidly in public channels once they know agents read them. Only public engineering channels are ingested, and the policy is published.

**Open questions.**

- **Agents reviewing agents:** could a second agent with a different model check claims that have no automatic checker, and how would its own wrong claims be measured?
- **Autonomy across task classes:** a task that starts as a dependency bump can turn into a refactor. Should the level follow the declared class, or be recomputed from the change as it grows?
- **Context that goes stale inside a task:** a long run reads its pack at the start. When a decision changes during a nine-hour run, should the run be told, and what should it do with work already done?
- **External agents and contractors:** the same identity and trail model should work for agents run by vendors on the company's code. What would they need to accept for that to be true?

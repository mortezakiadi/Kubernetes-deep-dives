---
id: appendix-design-01-eks
title: "Appendix B: Design 01 built and tested on Amazon EKS"
short: "B. Design 01 on EKS (built and tested)"
---
This appendix describes a working implementation of [Design 01: Sandboxing and secure execution for agents and untrusted code](#design-sandboxing). I built it with Terraform on Amazon EKS in one AWS account, tested every claim in the design that can be tested on this platform, recorded the evidence, and removed it again with one command. Everything here ran on a real cluster. Every command, output and pass or fail result in section 5 is copied from the recorded test run, not written from memory.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested with the commands, the results, why the results can be trusted and why the system produces them. The last section is an alternative egress gateway on Envoy, which I tested in a lab and not on the cluster.

12 of 12 scenarios passed, 75 of 75 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | Cluster topology, node pools and the sandbox node pool | 8 of 8 |
| S02 | The isolation boundary: a microVM per sandbox versus a shared kernel | 5 of 5 |
| S03 | Pod hardening, identity and Kubernetes permissions | 7 of 7 |
| S04 | Warm pools and claims | 3 of 3 |
| S05 | Default deny, and one way out | 7 of 7 |
| S06 | The egress gateway: per-session allowlist, the global ceiling and rate limits | 9 of 9 |
| S07 | Credentials: secrets never enter the sandbox | 7 of 7 |
| S08 | The tool gateway (MCP): policy, approvals, rate limits and revocation | 11 of 11 |
| S09 | Tier 3: a microVM with no network device | 3 of 3 |
| S10 | Limits, quotas and abuse | 6 of 6 |
| S11 | One query for an audit | 6 of 6 |
| S12 | Failure drills | 3 of 3 |

### Context: the problem, the actors and who talks to whom

**Scenario.** A company runs an AI agent. A user types a question into its chat window, for example "Why did checkout slow down this morning?" To answer, the agent writes a small script and has to run it. A model wrote that script, so nobody has reviewed it. It might try to read the company's secrets, reach another customer's work, or send data to a stranger. This appendix is about giving that script a place to run where it can do none of those things, and about proving that it can't.

Five terms are used throughout.

- **Agent (orchestrator).** The software that talks to the user and decides what to do next. It runs in an ordinary pod. A real one is a loop around a language model. In my tests it is a stand-in without a model, so every step is visible and repeatable.
- **Platform.** The services the company runs around the sandboxes: the Manager, the Router, the tool gateway, the egress gateway, the credential broker and the controller. They are trusted. They are separate pods on EKS, not one program.
- **Sandbox.** A pod that runs as its own small virtual machine with its own kernel, on nodes that run nothing else. It is the only place untrusted code runs.
- **Session.** One job of the agent, with its own sandbox. A session ties together a user, a customer (tenant), an agent, a sandbox, a list of sites it may reach, an end time and a token. It is not the chat. A chat may never need a sandbox, or it may open a session when code has to run. How a chat maps onto sessions is outside this appendix.
- **Company services.** The internal APIs an agent is allowed to use. The test uses one, a metrics API.

![The path of one question: user, agent, platform, sandbox and the outside world](diagrams/appendix/a02-5.svg)

**What the picture shows.** The path of one question, from the user to the answer. Dashed boxes are not built or tested here: the user's chat window and the language model. Everything else is the subject of this appendix.

**The path of one question.**

1. The user asks the question in the agent's chat window. *Not built here.*
2. The agent asks a language model what to do next and decides that code has to run. *Not in the test; the stand-in has no model.*
3. The agent asks the platform for a session for this user, customer and agent. The platform hands back a ready sandbox and a token.
4. The agent sends its script to the platform.
5. The platform forwards the script into that session's sandbox and brings the output back.
6. If the script needs data from outside, it has exactly one door: the platform's egress gateway.
7. The gateway checks the destination against this session's list, adds the real key itself, and passes on only the calls that are allowed. The sandbox never holds the key.
8. The agent turns the output into an answer for the user and closes the session. The sandbox is deleted.

![One request as calls between components](diagrams/appendix/a02-6.svg)

**What the picture shows.** The same request as calls between the components. Time runs from top to bottom. Each arrow is one call, and its number matches the list below. Dashed arrows are replies.

**Who talks to whom, and why.**

1. **User to Agent.** The user types the question. *Why:* this is how the work starts. This link is not built or tested here, and neither is the model call the agent makes to decide what to do.
2. **Agent to Manager.** The agent asks for a session and names the user, the customer, the agent, the kind of sandbox and any extra sites it needs. *Why:* only the Manager may create sandboxes, so the agent cannot give itself one with settings of its own choosing. The Manager enforces three open sessions per customer and puts an end time on every session.
3. **Manager to Agent.** The Manager returns the session ID and a signed token once the sandbox is ready, about 0.4 seconds when it comes from a warm pool. *Why:* the ID names the session in every log line, and the token is what the tool gateway checks later.
4. **Agent to Router.** The agent sends the script and the command to run, with the session ID. *Why:* the Router is the only service that may connect to a sandbox, so the agent needs no network path to one. The Router logs every command with its exit code and duration.
5. **Router to Sandbox.** The Router runs the command inside that session's sandbox. *Why:* untrusted code must only ever run inside the microVM.
6. **Sandbox to Egress gateway.** The script calls the company's metrics API and carries only the word `placeholder` as its key. *Why:* the sandbox's network policy allows exactly one destination, the gateway, on one port. There is no DNS and no other route.
7. **Egress gateway and Manager.** The gateway asks which session owns the source address and which hosts that session may reach, then checks the host. *Why:* the allowlist is per session and the Manager stores it. A host that is not on the list is refused with a 403 before any connection is opened.
8. **Egress gateway to Credential broker.** For the one internal host, the gateway asks for a key for this session and this destination, valid for 900 seconds at most. *Why:* the real key is made outside the sandbox and is never sent back to it.
9. **Egress gateway to Company API.** The gateway terminates TLS for that host, replaces the placeholder with the real key and sends the request. *Why:* the API refuses any request without a valid key, and the sandbox has no route to the API except through the gateway (S07).
10. **Egress gateway to Sandbox.** The reply goes back to the script. The key does not. *Why:* a script cannot leak a key it never held.
11. **Router to Agent.** The Router returns the output and the exit code. *Why:* the agent decides its next step from them.
12. **Agent to Tool gateway.** The agent calls a tool, such as `metrics.query`, with the session token. *Why:* tools sit behind a gateway that knows who is asking. It shows an agent only the tools its policy allows. A high-risk tool (`ticket.create` in the test) first answers "needs approval" and runs only after a person approves, once per approval (S08).
13. **Tool gateway and Manager.** On every call the gateway asks whether the session is still active. *Why:* a token must stop working the moment its session ends, even if it has not expired (S08).
14. **Tool gateway to Credential broker.** The gateway asks for a 60-second key for this session. *Why:* the key is made for this session and expires within a minute.
15. **Tool gateway to Company API.** The gateway makes the read call with that key. *Why:* the API checks the key's signature, audience and expiry (S07).
16. **Tool gateway to Agent.** The gateway returns the result.
17. **Agent to Manager.** The agent closes the session. The claim, the sandbox pod and its workspace are deleted, and the token stops working. This also happens on its own at the end time. *Why:* a sandbox must not outlive its job.
18. **Agent to User.** The agent writes the answer. *Not built here.*

A script inside the sandbox can also call the tool gateway, but only through the egress gateway (S08). Every component writes one log line per step with the session ID, so one query rebuilds this list (S11).

![Components view: where each service runs and who calls whom](diagrams/appendix/a02-7.svg)

**What the picture shows.** The components view. Every service is a pod. The platform pods run on EKS Auto Mode's general-purpose nodes. The sandboxes run on their own tainted node pool and nowhere else (S01). The arrows are the calls that exist, and each is enforced by the cluster rather than by good behaviour.

| Caller | Callee | Why | What enforces it |
|---|---|---|---|
| Agent | Manager, Router, tool gateway | Open sessions, run code, use tools | Per-service ingress policies. In the tests the orchestrator is the pod that calls these three directly. |
| Manager | Kubernetes API | Create a claim for a warm sandbox | Its Role can create claims but cannot exec (S03) |
| Router | Sandbox | Send commands and files in, results out | The sandbox's ingress rule names the router's namespace and label only (S05). Its Role allows exec only in the sandbox namespace (S03). |
| Sandbox | Egress gateway | The only way out | A generated policy allows TCP 3128 to the gateway's fixed address and nothing else (S05) |
| Egress gateway | Manager | Which session owns this address, and what is its list | The Manager looks the session up by the sandbox's source address |
| Egress gateway | Credential broker | A short-lived key for one session and one destination | Only the two gateways may call the broker |
| Egress gateway | Company services, internet | Pass on only allowed calls | The gateway's own network policy names three domains, so a wrong allowlist cannot open the internet (S06) |
| Tool gateway | Manager, Credential broker | Is the session still active; a 60-second key | Only the gateways may call the broker. The Manager is asked on every call (S08). |

The sections that follow build on this picture. Section 1 lists each component and what I wrote or reused, section 2 shows the EKS topology, section 3 shows the architecture and the network paths, and section 5 tests each promise.

### 1. The software components I implemented

**Scenario.** The design draws a trusted control plane, two gateways and an untrusted sandbox pool. To test it, each box in that picture has to be real software that I can start, call and attack. Nothing in the design is left as "assume this exists".

Some parts I reused as they are, and some I wrote. The table says which is which.

| # | Component | What it is | Where it runs | Reused or written |
|---|---|---|---|---|
| 1 | **Agent orchestrator (stand-in)** | A command-line client that makes the same API calls an agent orchestrator makes: create a session, run a command, upload a file, call a tool. It has no LLM, so every step of an "agent session" is visible and repeatable. | Namespace `agent-orchestrator` | Written, 90 lines of Python |
| 2 | **Sandbox Manager** | The only component that creates sandboxes. It enforces three sessions per tenant, stamps an end time and a delete policy on every claim, stores the session's allowlist, and issues the signed session token. Two replicas. | Namespace `sandbox-platform` | Written, 237 lines |
| 3 | **Sandbox Router** | Forwards commands and files to a sandbox by session ID. It uses the agent-sandbox runtime daemon (`sandboxd`) for sandboxes with a network, and the Kubernetes exec channel for sandboxes with no network device. It logs every command with its exit code and duration. Two replicas. | Namespace `sandbox-router` | Written, 133 lines |
| 4 | **Egress gateway** | The only route out of a sandbox. For every connection it finds the session that owns the source address, checks the host against that session's allowlist, enforces a rate limit and a byte budget, passes TLS through untouched for ordinary hosts, and terminates TLS only for the host where it injects a credential. Two replicas spread over zones, with a disruption budget. | Namespace `sandbox-egress` | The proxy engine is mitmproxy 12.3.1. The policy is written, 152 lines |
| 5 | **Credential broker** | Mints a short-lived, signed credential for one session and one destination, 900 seconds at most. Only the gateway and the tool gateway may call it. Two replicas. | Namespace `sandbox-egress` | Written, 42 lines |
| 6 | **Tool gateway (MCP)** | Speaks the Model Context Protocol over HTTP. A session token decides which tools an agent may see and call. High-risk tools wait for a human approval. Calls are rate limited. A token stops working the moment its session ends. | Namespace `sandbox-platform` | Written, 160 lines |
| 7 | **Metrics API (internal service)** | A stand-in for "the company's internal API". It serves HTTPS and refuses any request without a valid credential for its audience. A sandbox that holds only a placeholder can reach it only if the gateway injected a real credential. | Namespace `sandbox-platform` | Written, 38 lines |
| 8 | **Sandbox templates and warm pools** | Two templates: `python-untrusted` (Tier 2, a microVM with one route out) and `compute-only` (Tier 3, a microVM with no network device). Each has a warm pool. | Namespace `sandboxes` | Manifests written (`k8s/20-sandboxes.yaml`) |
| 9 | **Network controls** | A default-deny policy for the sandbox namespace, a policy generated from each template, per-service ingress policies, and a domain-based ceiling around the gateway. | All namespaces | Manifests written |
| 10 | **Audit trail** | Every component writes one JSON line per event, carrying the session ID. A query script joins them into one timeline. Fields named like secrets are redacted by the logging function. | All components | Written (inside `common.py`, plus `scripts/audit-query.py`) |
| 11 | **Escape drills** | A script that is uploaded into a sandbox and tries to do everything the threat model forbids. | Runs inside sandboxes | Written, 264 lines |
| 12 | **agent-sandbox controller and `sandboxd`** | The Kubernetes SIG Apps project that provides `SandboxTemplate`, `SandboxWarmPool` and `SandboxClaim`, and the daemon that runs commands inside a sandbox. Version 1.0.10, pinned by the SHA-256 of its release file. | Namespace `agent-sandbox-system` | Reused |
| 13 | **Nitro Sandboxes** | The AWS feature that runs each pod in its own Firecracker microVM, selected by `runtimeClassName: isolated-sandbox`. | EKS Auto Mode nodes | Reused (AWS) |

In total I wrote about 1,150 lines of Python for the services, 870 lines of Kubernetes manifests, 810 lines of Terraform, and 820 lines of test code. The services use only the Python standard library, apart from the router (which uses the agent-sandbox SDK) and the gateway (which runs inside mitmproxy), so a reader can audit each one in a few minutes.

**Going deeper.**

- **One image for every service.** All the Python services are built into one container image, `ds01-platform`, for `linux/amd64`. Each Deployment picks its program with a command. Terraform builds and pushes the image to a private ECR repository, and tags it with a hash of the source, so changing a line of code rolls out a new image.
- **Where each service gets its secrets.** Each Kubernetes Secret lives only in the namespace that needs it, and each pod mounts only the keys it needs. The `sandboxes` namespace contains no Secret at all. S03 checks that.
- **Why mitmproxy, and what else could do the job.** mitmproxy suits a test because the policy is easy to write on it, but it is a debugging proxy and not a CNCF project. Section 9 builds the same gateway on Envoy, which is a graduated CNCF project, and tests it in a lab.
- **Why a stand-in orchestrator.** A real orchestrator is an LLM loop. For a test, an LLM adds randomness and cost and proves nothing about the platform. The stand-in makes the same calls, deterministically.

### 2. EKS topology and instances

**Scenario.** Isolation depends on where things run. A sandbox that shares a node, a subnet or a security group with the platform is one hypervisor bug away from it. The topology has to keep the untrusted pool physically apart.

![EKS topology and instances](diagrams/appendix/a02-2.svg)

**What the picture shows.** One VPC with three kinds of subnet in each of two Availability Zones. The platform and the sandboxes never share a subnet, a security group, an IAM role or a node.

| Item | What I built | Why |
|---|---|---|
| **Cluster** | Amazon EKS, Kubernetes 1.37, **EKS Auto Mode** | Nitro Sandboxes need Kubernetes 1.36 or later. Auto Mode is the setup path AWS documents for them: it provides the `isolated-sandbox` runtime class and launches a sandbox-capable node when a sandboxed pod needs one. |
| **Control plane** | Managed by AWS. API-only authentication. The public endpoint accepts connections only from the administrator's address (a single `/32`). Control-plane logs (`api`, `audit`, `authenticator`) go to CloudWatch for 7 days. | Least exposure. The audit log records every claim and exec. |
| **Platform nodes** | Built-in Auto Mode `general-purpose` pool. Auto Mode chose small `c6a` instances (S01 prints the exact list). | They run the platform services and the controller. They do not tolerate the sandbox taint, so no sandbox runs here. |
| **Sandbox nodes** | A separate `NodePool` named `sandbox`. Auto Mode chose `c7i.xlarge` (4 vCPU, 8 GiB). The pool is limited to on-demand x86 instances of the `c` and `m` families, generation 7 or later, 4 or 8 vCPUs, 32 vCPUs in total. Taint `workload=sandbox:NoSchedule`. | Nitro Sandboxes run only on the subset of Nitro Enclaves instance types, with Enclaves enabled. Auto Mode enables it. The taint keeps everything else off these nodes. |
| **Density rule** | AWS caps the vCPUs and the sandboxes on a node at five times its vCPU count. | I size requests honestly (500m CPU, 1 GiB for a Python sandbox) and let the pool grow. A sandbox's memory is reserved up front. |
| **Subnets** | `10.20.0.0/16`. Public `/24` (NAT only). Platform `/20` tagged `ds01/tier=system`. Sandbox `/20` tagged `ds01/tier=sandbox`. Each in two zones. | The sandbox `NodeClass` selects subnets by that tag, so sandbox nodes cannot land in the platform subnets. |
| **Security groups** | A dedicated group for sandbox nodes. It accepts traffic only from the cluster security group, and the cluster group accepts the sandbox group back. | VPC-level rules can treat sandbox nodes differently from the rest. |
| **Internet access** | One NAT gateway in one zone | Keeps cost down. A production build uses one per zone. |
| **IAM** | A cluster role (five AWS-managed EKS policies). A platform node role and a separate sandbox node role, each with two policies: `AmazonEKSWorkerNodeMinimalPolicy` and `AmazonEC2ContainerRegistryPullOnly`. No inline policies. | The role an escaped process would find has no S3, no Secrets Manager and nothing else of value. S01 verifies it. |
| **Node metadata** | IMDSv2 required, hop limit 1, no public IP | Set by Auto Mode. S01 reads the live values from EC2. |
| **Registry** | Private ECR repository `ds01-platform`, scan on push, `force_delete` so teardown removes the images | One place for the platform image. |
| **State** | Terraform state is local and git-ignored | The repository is public; state holds identifiers. |

![One VPC, one address range, cut into public, platform and sandbox blocks](diagrams/appendix/a02-9.svg)

**What the picture shows.** The VPC has one address range, 10.20.0.0/16, and no second range is attached to it. I cut that range into six blocks (subnets), two in each zone: two small public blocks for the NAT gateway, two platform blocks and two sandbox blocks. The platform blocks and the sandbox blocks are the same size and differ only in their tag. Everything outside those six blocks is unassigned.

**Why two kinds of private block.**

- **Placement.** The sandbox node pool picks its subnets by the sandbox tag, so a sandbox node cannot be started in a platform block. The platform nodes come from the cluster's own subnet list, which names only the platform blocks.
- **A boundary of its own.** The sandbox blocks have their own security group, which accepts traffic only from the cluster security group, and their nodes use a separate IAM role. If a sandbox were ever escaped, it would start from a separate range, group and role, not from among the platform services.
- **Room for VPC-level rules.** The design asks for separate subnets "so VPC-level rules can treat them differently". A separate range is what such a rule can name. This build adds no rule of that kind yet: both kinds of private block share one route table, and the security group is the only VPC-level difference.

**Going deeper.**

- **Why not gVisor or Kata.** The design offers four tiers. EKS Auto Mode does not allow the containerd shim that gVisor needs, and Kata needs nested virtualization on self-managed nodes. Nitro Sandboxes are the design's default tier and the one Auto Mode supports, so Tiers 2 and 3 are built and tested here. Tier 0 (a hardened ordinary container) is used in S02 as the comparison.
- **Auto Mode does the undifferentiated work.** It provisions and patches the nodes, runs the network plugin and DNS, and removes empty nodes. My `NodePool` only says which nodes are allowed.
- **Cost.** The standing costs are the EKS control plane, the nodes, the NAT gateway and a few volumes. The whole stack exists for hours, not days, and `down.sh` removes it.

### 3. Architecture

**Scenario.** A new engineer joins and asks: "If an agent's code is hostile, what stops it, and how does it still get its work done?" This section answers with one picture and the life of one session.

![Design 01 on EKS: components and traffic](diagrams/appendix/a02-1.svg)

**What the picture shows.** Three zones. The trusted control plane (green) creates sandboxes and talks to them. The gateways (blue) are the only bridge between a sandbox and anything else. The untrusted sandbox pool (red) runs the code. Every arrow is a path that exists; any path not drawn is blocked, and S05 and S06 try to use them.

**The life of one session.**

1. The orchestrator asks the **Manager** for a session: user, tenant, agent, template, and optionally extra hosts the session may reach.
2. The Manager checks the tenant's limit (three) and creates a **SandboxClaim** that points at a warm pool, carries the allowlist, and has an end time with a delete policy.
3. The **controller** hands over a pod that is already booted. The Manager waits until the claim is ready (about 0.4 seconds from a warm pool) and returns the session ID and a signed session token.
4. The orchestrator sends commands and files through the **Router**, which reaches the sandbox with `sandboxd` (Tier 2) or the Kubernetes exec channel (Tier 3).
5. Code in the sandbox that needs the network can only talk to the **egress gateway**. The gateway asks the Manager which session owns the source address, checks the host against that session's allowlist, and either passes TLS through or, for the one internal host, terminates TLS and injects a credential minted by the **broker**.
6. Tools are reached through the **tool gateway** with the session token, either by the orchestrator directly or from inside the sandbox through the egress gateway.
7. Every component writes JSON lines with the session ID, so one query reconstructs the session.
8. The session ends when the orchestrator deletes it or its end time passes. The claim, the pod and its workspace are deleted, and the token stops working.

![Every network path out of a sandbox](diagrams/appendix/a02-3.svg)

**What the picture shows.** The one allowed route out, and the seven attempts that the tests make to find another. Each attempt is labelled with the scenario that tries it.

![How a request gets a credential without the sandbox holding it](diagrams/appendix/a02-4.svg)

**What the picture shows.** The five steps that let a sandbox holding only the word `placeholder` call a protected API. The credential is created, used and discarded outside the sandbox.

### 4. How it is implemented

**Scenario.** A reader wants to rebuild this, or to know which line of code produces which behaviour. This section maps the design to files.

**The folder** (`implementations/ds01-sandboxing-on-eks/` in the repository):

- `terraform/10-cluster/`: the VPC, subnets, NAT gateway, security groups, IAM roles, log group and the EKS Auto Mode cluster.
- `terraform/20-platform/`: the container image, the secrets and certificates, and every Kubernetes object, applied from `k8s/`.
- `k8s/`: the manifests, in the order they are applied: namespaces, sandbox node pool, sandboxes (templates, pools, default deny, quota), egress gateway and broker, platform services.
- `platform/`: the Python source and the Dockerfile.
- `tests/`: the scenario runner, the test harness and the screenshot renderer.
- `scripts/`: `up.sh`, `down.sh`, `verify-clean.sh`, `audit-query.py`, `setup-tools.sh`, and a secret scanner that blocks a commit containing a credential.

**Two Terraform layers.** The first layer creates the cluster. The second layer talks to the cluster, so it has to run after the first. Keeping them as two separate states also lets the platform be destroyed first, which lets Auto Mode remove its nodes before the cluster goes. Both layers refuse to run against any account except the one named in a local, git-ignored file.

**What the second layer does, in order.**

1. Creates the ECR repository and builds and pushes the image.
2. Creates the namespaces.
3. Generates every secret and two certificate authorities (an internal one for the metrics API, and one the gateway uses to terminate TLS for the one injected host).
4. Installs the agent-sandbox controller from its release file, after checking the file against a pinned SHA-256.
5. Turns on NetworkPolicy enforcement, which EKS Auto Mode ships switched off.
6. Applies the node pool, templates, warm pools, policies, gateways and services.

**The control-by-control map.**

| Design control | Implemented as | Verified by |
|---|---|---|
| A separate kernel per sandbox | `runtimeClassName: isolated-sandbox` in both templates | S02 |
| Nodes that run nothing else | NodePool taint, a dedicated NodeClass, its own subnets and security group | S01 |
| Minimal node role | Separate IAM role with two managed policies | S01 |
| No Kubernetes credentials in the sandbox | `automountServiceAccountToken: false`, a ServiceAccount with no Role, `enableServiceLinks: false` | S03, S05 |
| Hardened pod | Non-root, read-only root, all capabilities dropped, seccomp default, Pod Security `restricted` on the namespace | S03 |
| Default-deny network, one way out | Namespace default-deny plus a template policy that allows only the gateway's address on port 3128 | S05 |
| No DNS channel | No DNS rule. The gateway's address is fixed, so the sandbox needs no lookup | S05 |
| Per-session allowlist | Stored on the claim by the Manager. The gateway asks for it by source address | S06 |
| Global ceiling | A domain-based policy around the gateway pods | S06 |
| Secrets never inside | A placeholder in the template. The gateway swaps in a broker-minted credential | S07 |
| Tool gateway with approvals | MCP service with a per-agent policy and an approval store | S08 |
| Tier 3, no network | `sandbox.amazonaws.com/network-isolated: "true"` | S09 |
| Warm pools and claims | `SandboxWarmPool` and `SandboxClaim` | S04 |
| Quotas and hard end time | Manager's tenant limit. `lifecycle.shutdownTime` and `shutdownPolicy: Delete` on the claim | S10 |
| Audit by session ID | JSON log lines with `session_id` in every component | S11 |
| Fail closed | One route out, so no gateway means no route | S12 |

**Where reality differed from the design.** These are findings, each confirmed on the live cluster.

- **The metadata service cannot be blocked by network policy, and does not need to be.** The design says to block `169.254.169.254` from sandboxes. In a Nitro Sandbox the address is served inside the virtual machine, so a NetworkPolicy never sees the traffic. What protects the credentials is that the sandbox serves only placement data from that address (S05 shows 404 for every credential path), and that no Pod Identity association exists for the sandbox's ServiceAccount.
- **Label domains.** The controller only accepts pod labels under an allowed domain, `sandbox.users.io` by default, so the session labels use it.
- **The router needs `get` on `pods/exec` as well as `create`.** The websocket exec client upgrades with GET.
- **A memory bomb restarts the container, not just the process.** The guest kernel enforces the limit and kills the container's whole cgroup (S10).
- **Rate limits need a stable replica.** The gateway counts requests in memory, so the Service pins each sandbox to one gateway replica (`sessionAffinity: ClientIP`). Without it, two replicas each saw half the requests and the limit never triggered, which I observed before fixing it.
- **A Service address works with network policy.** The sandbox's only egress rule names the gateway's fixed ClusterIP, and enforcement matched it (S05 and S06 pass).
- **One shell variable can break the AWS CLI.** If `AWS_REGION` is set in your shell, the AWS CLI v2 prefers it over `AWS_DEFAULT_REGION` and over the profile. `up.sh` and the test harness set both explicitly.

### 5. The scenarios I tested

**Scenario.** The design makes specific promises. I turned each into a scenario that runs real commands against the live cluster and decides pass or fail from the output, with no human judgement in the loop.

**How to read this section.**

- `orch` stands for `kubectl -n agent-orchestrator exec deploy/orchestrator -- python -m ds01.orchestrator`. The orchestrator pod is the only pod allowed to call the Manager, the Router and the tool gateway.
- Commands that begin with `orch exec <session>` run **inside the sandbox**, through the router.
- The screenshots are terminal windows **rendered from the recorded text of the commands that ran**, after removing private values. They are faithful renderings of terminal output, not photographs of a screen. Session tokens, the account number and the administrator's address are replaced by placeholders in every file and picture, and a scanner blocks any commit that contains them.
- A check passes only if the recorded output satisfies it. If a scenario crashes, it fails and says why.

The tests ran from a clean build, after `up.sh` created the whole stack from nothing.


### S01: Cluster topology, node pools and the sandbox node pool

**Design reference:** The sandbox node pool and pod hardening.

**What this tests.** Show the EKS mode and version, the dedicated tainted sandbox pool, the instance type it runs on, the separate subnets and security group, the minimal node role, and that nothing but sandboxes runs on those nodes.

Before testing behaviour, I check that the cluster is built the way the design says: a dedicated pool of nodes that runs nothing but sandboxes, in its own subnets, with its own security group and a minimal IAM role.

![S01: Cluster topology, node pools and the sandbox node pool](diagrams/appendix/a02-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws eks describe-cluster --name ds01-sandboxing --query <version, autoMode, endpoint CIDRs, logging>
{
    "version": "1.37",
    "status": "ACTIVE",
    "autoMode": {
        "enabled": true,
        "nodePools": [
            "general-purpose",
            "system"
        ],
        "nodeRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ds01-sandboxing-node"
    },
    "publicEndpointCidrs": [
        "<ADMIN_IP>/32"
    ],
    "logging": [
        "api",
… (4 more lines in the screenshot and in evidence/S01.txt)

$ kubectl get nodes -L workload,node.kubernetes.io/instance-type,topology.kubernetes.io/zone,eks.amazonaws.com/nitro-sandbox
NAME                  STATUS   ROLES    AGE     VERSION               WORKLOAD   INSTANCE-TYPE   ZONE         NITRO-SANDBOX
i-0543f2a2608a827e3   Ready    <none>   30s     v1.37.1-eks-577a51e   sandbox    c7i.xlarge      us-east-1a   true
i-0ab84e37874257700   Ready    <none>   2m23s   v1.37.1-eks-577a51e              c6a.large       us-east-1a   false

$ kubectl get nodepool,nodeclass
NAME                                    NODECLASS   NODES   READY   AGE
nodepool.karpenter.sh/general-purpose   default     1       True    5m17s
nodepool.karpenter.sh/sandbox           sandbox     1       True    112s
nodepool.karpenter.sh/system            default     0       True    5m17s

NAME                                  ROLE                           READY   AGE
nodeclass.eks.amazonaws.com/default   ds01-sandboxing-node           True    5m19s
nodeclass.eks.amazonaws.com/sandbox   ds01-sandboxing-sandbox-node   True    115s

$ kubectl get nodepool sandbox -o jsonpath={.spec.template.spec.taints}
[{"effect":"NoSchedule","key":"workload","value":"sandbox"}]

$ aws ec2 describe-instances --instance-ids i-0543f2a2608a827e3 --query <type, subnet, security groups, IMDS options, enclave option>
{
    "type": "c7i.xlarge",
    "subnet": "subnet-0e9a98c895b1b0256",
    "securityGroups": [
        "ds01-sandboxing-sandbox-nodes"
    ],
    "imdsTokens": "required",
    "imdsHopLimit": 1,
    "nitroEnclaves": true
}

$ aws ec2 describe-subnets --subnet-ids <the node's subnet> --query <cidr, tier tag, name>
{
    "cidr": "10.20.64.0/20",
    "tier": "sandbox",
    "name": "ds01-sandboxing-sandbox-us-east-1a"
}

$ aws iam list-attached-role-policies --role-name ds01-sandboxing-sandbox-node --query AttachedPolicies[].PolicyName --output json
[
    "AmazonEKSWorkerNodeMinimalPolicy",
    "AmazonEC2ContainerRegistryPullOnly"
]

$ aws iam list-role-policies --role-name ds01-sandboxing-sandbox-node --query PolicyNames --output json
[]

$ kubectl get pods -A -o wide --field-selector spec.nodeName=i-0543f2a2608a827e3
NAMESPACE   NAME                          READY   STATUS    RESTARTS   AGE    IP             NODE                  NOMINATED NODE   READINESS GATES
sandboxes   compute-only-pool-plgv5       1/1     Running   0          2m1s   10.20.67.242   i-0543f2a2608a827e3   <none>           <none>
sandboxes   python-untrusted-pool-9xp67   1/1     Running   0          2m2s   10.20.67.241   i-0543f2a2608a827e3   <none>           <none>
sandboxes   python-untrusted-pool-bq7wl   1/1     Running   0          2m2s   10.20.67.240   i-0543f2a2608a827e3   <none>           <none>

$ kubectl run taint-probe --image=python:3.12-slim --overrides='{nodeSelector: workload=sandbox}'  # a normal pod that asks for a sandbox node
pod/taint-probe created

$ kubectl get pod taint-probe
NAME          READY   STATUS    RESTARTS   AGE
taint-probe   0/1     Pending   0          13s

$ kubectl describe pod taint-probe | grep -A3 Events
Events:
  Type     Reason            Age   From                   Message
  ----     ------            ----  ----                   -------
  Warning  FailedScheduling  14s   default-scheduler      0/2 nodes are available: 1 node(s) didn't match Pod's node affinity/selector, 1 node(s) had untolerated taint(s). preemption: 0/2 nodes are a…
  Warning  FailedScheduling  13s   eks-auto-mode/compute  Failed to schedule pod, did not tolerate taint (taint=CriticalAddonsOnly:NoSchedule); did not tolerate taint (taint=workload=sandbox:NoSchedu…

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | a sandbox node exists and is labelled for Nitro Sandboxes | Auto Mode launched a sandbox-capable node from the dedicated pool and labelled it. |
| PASS | the pool taints its nodes workload=sandbox:NoSchedule | The NodePool template carries the taint, so only pods that tolerate it can land there. |
| PASS | the node sits in a subnet tagged tier=sandbox | The sandbox NodeClass selects subnets by the ds01/tier=sandbox tag, so these nodes cannot be placed in the platform subnets. |
| PASS | the node uses the dedicated sandbox security group | The NodeClass pins the security group created for sandbox nodes. |
| PASS | IMDSv2 is required and Nitro Enclaves are enabled | Auto Mode launches nodes with IMDSv2 required; Nitro Sandboxes need the Enclaves option, which Auto Mode set. |
| PASS | the sandbox node role has only the two minimal managed policies and no inline policy | Terraform attaches exactly these two policies. There is no S3, Secrets Manager or other access an escaped process could use. |
| PASS | only the sandboxes namespace has pods on the sandbox node | Nothing else tolerates the sandbox taint, so platform pods and the controller run on the general-purpose pool. |
| PASS | an ordinary pod that asks for the sandbox pool stays Pending | The scheduler refuses it because it does not tolerate the workload=sandbox taint. |

**Why you can trust this result.**

- Each fact comes from the system that owns it: the Kubernetes API for pools and pods, the EC2 API for the instance, its subnet and metadata options, the IAM API for the role. None of it is read back from my own configuration files.
- The last check is a negative test. It tries to do the forbidden thing (put an ordinary pod on a sandbox node) and shows the scheduler refusing.

**Why you get this output.**

- The node carries the label for Nitro Sandboxes and the instance has Nitro Enclaves switched on because EKS Auto Mode launches sandbox-capable instances when a pod uses the sandbox runtime class. I did not configure that by hand.
- The taint and the subnet and security-group placement come from the NodePool and NodeClass I wrote (`k8s/10-sandbox-nodes.yaml`). The role has exactly two policies because Terraform attaches exactly two (`terraform/10-cluster/eks.tf`).

### S02: The isolation boundary: a microVM per sandbox versus a shared kernel

**Design reference:** Choosing the isolation boundary (Tier 0 versus Tier 2).

**What this tests.** Prove that a sandbox runs on its own kernel in its own virtual machine, by comparing it with a hardened ordinary container on a normal node.

This is the central claim of the design: a sandbox does not share a kernel with the node or with other pods. I test it by putting a sandbox next to an ordinary hardened container and asking each one the same three questions: what kernel are you running, what machine are you, and how long have you been up.

![S02: The isolation boundary: a microVM per sandbox versus a shared kernel](diagrams/appendix/a02-evidence/S02.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create iso coding python-untrusted 900
{"http_status": 201, "session_id": "sess-b606b111", "claim": "sess-b606b111", "template": "python-untrusted", "ready_ms": 374, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ kubectl -n sandboxes get pod python-untrusted-pool-9xp67 -o custom-columns=NAME:.metadata.name,RUNTIME:.spec.runtimeClassName,NODE:.spec.nodeName,STARTED:.status.startTime
NAME                          RUNTIME            NODE                  STARTED
python-untrusted-pool-9xp67   isolated-sandbox   i-0543f2a2608a827e3   2026-11-19T15:14:13Z

$ orch exec sess-b606b111 "uname -r; cat /sys/class/dmi/id/product_name; cut -d' ' -f1 /proc/uptime"   # inside the SANDBOX
6.18.62
c7i.enclave
61.70
[exit code 0]

$ kubectl run tier0 --image=python:3.12-slim  # hardened ordinary container, Tier 0, on a normal node
pod/tier0 created

$ kubectl exec tier0 -- sh -c "uname -r; cat /sys/class/dmi/id/product_name; cut -d' ' -f1 /proc/uptime"   # inside the ORDINARY CONTAINER
6.18.62
c6a.large
202.43

$ kubectl get node i-0ab84e37874257700 -o jsonpath='{.status.nodeInfo.kernelVersion}'   # the node's own kernel version
6.18.62

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the sandbox reports a virtual machine of its own (product name ends in .enclave) | A Nitro Sandbox is a Firecracker microVM, and its firmware reports its own product name. |
| PASS | the ordinary container reports the host machine, not a virtual machine of its own | A normal container is a process group on the node and sees the node's hardware identity. |
| PASS | the sandbox's kernel started when the pod started, not when the node did | The sandbox boots its own kernel at pod start, so its uptime tracks the pod's age. |
| PASS | the ordinary container's uptime tracks its node's age (it shares the node's kernel) | A container shares the node's kernel, so /proc/uptime is the node's uptime. |
| PASS | the ordinary container reports exactly the node's kernel version | Same kernel: this is the shared-kernel risk the design's threat model describes (arrow 1). |

**Measured in this run:** sandbox uptime 62s versus pod age 82s; ordinary container uptime 204s versus its node's age 196s

**Why you can trust this result.**

- Both measurements are taken from inside the workload, by reading `/proc/uptime` and the firmware product name. The workload's author cannot set either value.
- The comparison is built to fail in a specific way if isolation were absent: a container that shared the node's kernel would report the node's uptime, not its own pod's age.
- The two pods run on the same cluster at the same time, so there is no difference in environment to explain the result.

**Why you get this output.**

- The sandbox reports a product name ending in `.enclave` and an uptime that matches its pod's age because the pod's runtime class is `isolated-sandbox`: the pod was started as a Firecracker microVM with its own freshly booted kernel.
- Do not read anything into the kernel version strings. In this run the sandbox and the node report the same version (6.18.62), so the version cannot tell them apart. The test relies on the two things that can: the machine identity and the uptime.
- The ordinary container reports its node's uptime and the node's exact kernel version because a normal container is only a set of processes on the node's kernel. That is the shared-kernel risk in arrow 1 of the design's threat model.

### S03: Pod hardening, identity and Kubernetes permissions

**Design reference:** The sandbox node pool and pod hardening; Threat model arrows 3 and 4.

**What this tests.** Show that a sandbox holds no Kubernetes credential, runs unprivileged on a read-only filesystem, that its ServiceAccount can do nothing, and that the namespace refuses a privileged pod.

Isolation protects the node. This scenario checks the other half of the threat model: what a hostile process finds inside the sandbox, and what the sandbox's identity is allowed to do in the cluster.

![S03: Pod hardening, identity and Kubernetes permissions](diagrams/appendix/a02-evidence/S03.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create hard coding python-untrusted 900
{"http_status": 201, "session_id": "sess-0a9b9fcf", "claim": "sess-0a9b9fcf", "template": "python-untrusted", "ready_ms": 332, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ kubectl -n sandboxes get pod python-untrusted-pool-bq7wl -o jsonpath=<the security-relevant fields>
runtimeClass: isolated-sandbox
serviceAccount: sandbox-nobody
automountServiceAccountToken: false
enableServiceLinks: false
runAsNonRoot: true
readOnlyRootFilesystem: true
capabilities.drop: ["ALL"]
limits: {"cpu":"1","memory":"1Gi"}

$ orch exec sess-0a9b9fcf "id; touch /etc/probe; ls /var/run/secrets; env | grep ...; grep CapEff /proc/1/status"   # run INSIDE the sandbox
uid=1000 gid=1000 groups=1000
---
touch: cannot touch '/etc/probe': Read-only file system
---
ls: cannot access '/var/run/secrets': No such file or directory
---
KUBERNETES_SERVICE_PORT=<value hidden>
KUBERNETES_PORT=<value hidden>
GPG_KEY=<value hidden>
KUBERNETES_PORT_443_TCP_ADDR=<value hidden>
KUBERNETES_PORT_443_TCP_PORT=<value hidden>
KUBERNETES_PORT_443_TCP_PROTO=<value hidden>
METRICS_API_KEY=<value hidden>
KUBERNETES_PORT_443_TCP=<value hidden>
KUBERNETES_SERVICE_PORT_HTTPS=<value hidden>
KUBERNETES_SERVICE_HOST=<value hidden>
… (4 more lines in the screenshot and in evidence/S03.txt)

$ kubectl auth can-i get secrets -n sandboxes --as system:serviceaccount:sandboxes:sandbox-nobody
no

$ kubectl auth can-i list pods -n sandboxes --as system:serviceaccount:sandboxes:sandbox-nobody
no

$ kubectl auth can-i create pods/exec -n sandboxes --as system:serviceaccount:sandboxes:sandbox-nobody
no

$ kubectl auth can-i get secrets -n kube-system --as system:serviceaccount:sandboxes:sandbox-nobody
no

$ kubectl -n sandboxes get secrets

No resources found in sandboxes namespace.

$ kubectl -n sandboxes apply -f privileged-pod.yaml   # privileged: true, hostNetwork: true

Error from server (Forbidden): error when creating "STDIN": pods "escape-attempt" is forbidden: violates PodSecurity "restricted:latest": host namespaces (hostNetwork=true), privileged (container "c"…

$ kubectl auth can-i create sandboxclaims.extensions.agents.x-k8s.io -n sandboxes --as system:serviceaccount:sandbox-platform:sandbox-manager
yes

$ kubectl auth can-i create pods/exec -n sandboxes --as system:serviceaccount:sandbox-platform:sandbox-manager
no

$ kubectl auth can-i create pods/exec -n sandboxes --as system:serviceaccount:sandbox-router:sandbox-router
no

$ kubectl auth can-i create pods/exec -n default --as system:serviceaccount:sandbox-router:sandbox-router
no

$ kubectl auth can-i get pods -n sandboxes --as system:serviceaccount:agent-orchestrator:orchestrator
no

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the pod opts into the microVM runtime and mounts no ServiceAccount token | These fields come straight from the SandboxTemplate; the controller copies them into every pod. |
| PASS | the container drops all capabilities, forbids privilege escalation and has a read-only root filesystem | Defence in depth: even inside the microVM the pod is hardened as if it were not. |
| PASS | the code runs as uid 1000, cannot write to the root filesystem and finds no service-account directory | runAsUser, readOnlyRootFilesystem and automountServiceAccountToken=false in the template. |
| PASS | the sandbox ServiceAccount can do nothing on the Kubernetes API | It has no Role or ClusterRole bound to it. |
| PASS | the sandboxes namespace contains no Kubernetes Secret at all | Credentials live only in the namespaces of the components that use them. |
| PASS | a privileged, host-network pod is refused by Pod Security admission | The namespace enforces the Pod Security 'restricted' profile. |
| PASS | each platform identity holds only the permissions its job needs | The Manager can create claims but not exec; the Router can exec only inside sandboxes; the orchestrator has no Kubernetes access. |

**Why you can trust this result.**

- Pod settings are read from the live pod object, not from my template file.
- Permissions are asked of the Kubernetes API itself (`kubectl auth can-i --as ...`), so the answer is what the cluster will actually enforce.
- The privileged-pod test submits a real forbidden request and records the real rejection.

**Why you get this output.**

- The container runs as user 1000 on a read-only root filesystem with no service-account directory because the SandboxTemplate sets `runAsUser`, `readOnlyRootFilesystem` and `automountServiceAccountToken: false`.
- The ServiceAccount can do nothing because I bound no Role to it. The platform identities can do only their jobs because each Role in `k8s/40-platform.yaml` lists only those verbs.
- The privileged pod is refused because the `sandboxes` namespace carries the Pod Security `restricted` label (`k8s/00-namespaces.yaml`).

### S04: Warm pools and claims

**Design reference:** Sandbox lifecycle: templates, warm pools and claims.

**What this tests.** Show that a session claims a pre-booted sandbox in well under a second, that the pool refills, and that an empty pool degrades to a slower cold start instead of failing.

A sandbox that takes half a minute to start makes an agent feel broken. The design answers with warm pools: sandboxes booted in advance, handed out by a claim. I test the fast path, the empty-pool path and the refill.

![S04: Warm pools and claims](diagrams/appendix/a02-evidence/S04.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl -n sandboxes get sandboxwarmpool,sandboxtemplate
NAME                                                               READY   DESIRED   AGE
sandboxwarmpool.extensions.agents.x-k8s.io/compute-only-pool       1       1         14m
sandboxwarmpool.extensions.agents.x-k8s.io/python-untrusted-pool   2       2         14m

NAME                                                          AGE
sandboxtemplate.extensions.agents.x-k8s.io/compute-only       14m
sandboxtemplate.extensions.agents.x-k8s.io/python-untrusted   14m

$ kubectl -n sandboxes get pods -o wide   # the warm pods, already booted and waiting
NAME                          READY   STATUS    RESTARTS   AGE     IP             NODE                  NOMINATED NODE   READINESS GATES
compute-only-pool-sdmjn       1/1     Running   0          3m43s   10.20.87.116   i-0021b8afd01dfb486   <none>           <none>
python-untrusted-pool-2mxkk   1/1     Running   0          2m22s   10.20.87.112   i-0021b8afd01dfb486   <none>           <none>
python-untrusted-pool-p2nxv   1/1     Running   0          117s    10.20.87.115   i-0021b8afd01dfb486   <none>           <none>

$ orch create pool coding python-untrusted 600   # x3, started at the same instant (pool size is 2)
sess-0d575447  ready in 343 ms
sess-27d2cc6a  ready in 346 ms
sess-b346d23f  ready in 10097 ms

$ kubectl -n sandboxes get sandboxwarmpool   # after about a minute the pool has refilled
NAME                    READY   DESIRED   AGE
compute-only-pool       1       1         14m
python-untrusted-pool   2       2         14m

$ kubectl get nodeclaims -o custom-columns=<name, pool, instance type, created>   # did the slow start launch a node?
NAME                    POOL              TYPE         CREATED
general-purpose-wwhpt   general-purpose   c6a.large    2026-11-19T15:11:57Z
sandbox-6hlth           sandbox           c7i.xlarge   2026-11-19T15:16:19Z
sandbox-z887l           sandbox           c7i.xlarge   2026-11-19T15:13:53Z

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | two sessions got a ready sandbox in under 2 seconds | They adopted pods that were already booted; nothing started on the claim path. |
| PASS | the third session, which found the pool empty, still got a sandbox (a slower cold start) | Cold start took 10097 ms against 343 ms warm. An empty pool degrades; it does not fail. |
| PASS | the warm pool refilled to its desired size | The controller boots a replacement for every pod a claim adopts. |

**Measured in this run:** Sandbox node claims after the burst: 2. Compare their creation times with the session start times above.

**Why you can trust this result.**

- The start times are measured by the Manager from the moment it creates the claim to the moment the claim reports Ready, and printed by it. I did not time them by hand.
- Three sessions are started at the same instant against a pool of two, so the third has to take the slow path. The test needs that difference to appear for its second check to mean anything.

**Why you get this output.**

- Two sessions are ready in well under a second because the controller adopts a pod that is already running and has already pulled its image. Nothing boots on that path.
- The third takes much longer because no warm pod is free, so a new sandbox has to be created. It still succeeds: an empty pool degrades, it does not fail.
- How much longer depends on capacity. In the first full run on this build the third session took 60 seconds: the sandbox node had no room left, so Auto Mode launched a second `c7i.xlarge`. Its node claim was created 3 seconds after the three claims, and the Manager's log recorded the session ready at 60,012 ms. In the run shown above the node had room, and the cold start took about 10 seconds, which is the time to boot a new microVM. Size the warm pool for the arrival rate, and treat a node launch as the worst case.
- The pool returns to its desired size because the controller starts a replacement for every pod a claim adopts.

### S05: Default deny, and one way out

**Design reference:** Network: default deny and one way out; Threat model arrows 2, 3, 4, 6.

**What this tests.** Try, from inside a sandbox, to reach the cluster, another sandbox, the cloud account's credentials, the internet and DNS. Every attempt must fail.

This is the scenario the design's network section is written for. Code running in a sandbox is hostile. From there I try to reach the Kubernetes API, another sandbox, the node's cloud credentials, the internet by address, the internet with the proxy settings removed, and DNS.

![S05: Default deny, and one way out](diagrams/appendix/a02-evidence/S05.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl -n sandboxes get networkpolicy   # one default-deny plus one generated from the template
NAME                              POD-SELECTOR                                         AGE
default-deny-all                  <none>                                               4m38s
python-untrusted-network-policy   agents.x-k8s.io/sandbox-template-ref-hash=c782f3a4   4m37s

$ kubectl -n sandboxes get networkpolicy python-untrusted-network-policy -o jsonpath='{.spec}'   # generated by the controller from the template
{"egress":[{"ports":[{"port":3128,"protocol":"TCP"}],"to":[{"ipBlock":{"cidr":"172.20.200.10/32"}}]}],"ingress":[{"from":[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"sandbox-r…

$ orch create net coding python-untrusted 900
{"http_status": 201, "session_id": "sess-3e6d2deb", "claim": "sess-3e6d2deb", "template": "python-untrusted", "ready_ms": 350, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ orch exec sess-3e6d2deb "python3 /workspace/drills.py kubernetes_api,other_sandbox,direct_internet,proxy_bypass,dns_exfiltration,imds other_ip=10.20.87.113"
kubernetes_api         blocked  {"api_server_connect": "TimeoutError: timed out", "service_account_token_mounted": false}
other_sandbox          blocked  {"8080": "TimeoutError: timed out", "9090": "TimeoutError: timed out"}
direct_internet        blocked  {"1.1.1.1:443": "TimeoutError: timed out", "8.8.8.8:443": "TimeoutError: timed out"}
proxy_bypass           blocked  {"exit_code": 1, "error": ["urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name resolution>"]}
dns_exfiltration       blocked  {"c2VjcmV0LWRhdGE.attacker.example": "gaierror: [Errno -3] Temporary failure in name resolution", "pypi.org": "gaierror: [Errno -3] Temporary failure in name resolutio…
imds                   blocked  {"imdsv2_token_obtained": true, "http_status_by_path": {"placement/region": 200, "iam/security-credentials/": 404, "iam/info": 404, "identity-credentials/ec2/security-…

$ kubectl -n agent-orchestrator exec deploy/orchestrator -- python -c "connect(10.20.87.113:8080)"   # a trusted pod, but not the router
blocked: TimeoutError timed out

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the Kubernetes API server is unreachable and no service-account token exists | Default-deny egress admits only the gateway; the API server is not on the list, and no token is mounted. |
| PASS | another sandbox's agent ports are unreachable | Ingress to a sandbox is admitted only from the router namespace. |
| PASS | direct connections to internet addresses fail | The only egress rule is to the gateway's address on one port. |
| PASS | code that unsets the proxy settings still cannot get out | Network policy, not the proxy variable, is the barrier. |
| PASS | DNS cannot be used to smuggle data out (no name resolves at all) | There is no DNS rule; the proxy resolves names on the sandbox's behalf. |
| PASS | the metadata service yields no credentials | Nitro Sandboxes serve only placement data from the metadata address, and no Pod Identity association exists for the sandbox ServiceAccount. |
| PASS | even the trusted orchestrator cannot reach a sandbox directly; only the router can | The template's ingress rule names the router's namespace and label only. |

**Measured in this run:** a second sandbox (sess-28e0ff4f) at 10.20.87.113 is the target of the lateral-movement test

**Why you can trust this result.**

- The attacks run inside the sandbox, through the same router path a real agent would use, so the source address, identity and network are the real ones.
- Each attack reports the exact error it got (a timeout, or a name-resolution failure). A pass is therefore a recorded failure to connect, not an absence of output.
- Two checks are positive controls elsewhere in the suite (an allowed host works, S06), so a 'blocked' result here cannot be explained by the network simply being down.
- The last check comes from a trusted pod, not from a sandbox, to show that ingress is closed even to the orchestrator.

**Why you get this output.**

- Every outbound attempt times out because the only egress rule the template generates is 'TCP 3128 to the gateway's address'. The generated NetworkPolicy is printed at the top of the evidence.
- No name resolves because the template has no DNS rule at all. The sandbox's proxy setting names the gateway by IP address, and the gateway resolves names for it. That closes DNS as a channel for smuggling data out.
- Unsetting the proxy variables does not help because the proxy is not what stops the traffic. The network policy is.
- The metadata service answers, but only with placement data. Credentials return 404 and there is no Pod Identity endpoint. That comes from how Nitro Sandboxes serve the metadata address, plus the fact that I created no Pod Identity association for the sandbox's ServiceAccount. The write-up explains in section 4 why this differs from the design's wording.
- Another sandbox is unreachable, and so is a sandbox from the orchestrator, because the ingress rule admits only the router's pods in the router's namespace.

### S06: The egress gateway: per-session allowlist, the global ceiling and rate limits

**Design reference:** Network: default deny and one way out.

**What this tests.** Show the gateway allowing what a session is approved for, refusing everything else, being capped by the network ceiling even when an allowlist is wrong, and rate limiting a runaway session.

The gateway is the only way out, so it has to make the right decision for each session. This scenario tests an allowed host, a refused host, per-session differences, a mistake in an allowlist that the network must catch, a rate limit, and the audit record.

![S06: The egress gateway: per-session allowlist, the global ceiling and rate limits](diagrams/appendix/a02-evidence/S06.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create egr coding python-untrusted 900
{"http_status": 201, "session_id": "sess-a67e3d16", "claim": "sess-a67e3d16", "template": "python-untrusted", "ready_ms": 340, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ orch exec sess-a67e3d16 "python3 /workspace/drills.py unapproved_host,approved_host,host_probe host=example.com"
unapproved_host        blocked  {"error": "Tunnel connection failed: 403 Forbidden"}
approved_host          allowed  {"https://pypi.org/simple/": 200, "note": "positive control: allowed is correct"}
host_probe             blocked  {"host": "example.com", "error": "Tunnel connection failed: 403 Forbidden"}

$ orch create egr coding python-untrusted 900 example.com
{"http_status": 201, "session_id": "sess-0475807f", "claim": "sess-0475807f", "template": "python-untrusted", "ready_ms": 337, "allow_hosts": ["example.com", "files.pythonhosted.org", "metrics-api.sa…

$ orch exec sess-0475807f "python3 /workspace/drills.py host_probe host=example.com"
host_probe             allowed  {"host": "example.com", "status": 200}

$ orch create egr coding python-untrusted 900 github.com
{"http_status": 201, "session_id": "sess-19450cf8", "claim": "sess-19450cf8", "template": "python-untrusted", "ready_ms": 337, "allow_hosts": ["files.pythonhosted.org", "github.com", "metrics-api.san…

$ orch exec sess-19450cf8 "python3 /workspace/drills.py ceiling_backstop host=github.com"
ceiling_backstop       blocked  {"error": "timed out"}

$ scripts/audit-query.py sess-19450cf8 | grep -E 'egress|github'
15:28:55.927  egress-gateway     egress.allow         host=github.com port=443 tls_mode=passthrough

$ kubectl -n sandbox-egress exec <gateway pod> -- python -c "connect to pypi.org:443, then github.com:443"   # the gateway's own network, no sandbox involved
pypi.org -> connected
github.com -> TimeoutError: timed out

$ orch exec sess-a67e3d16 "python3 /workspace/drills.py rate_limit n=140"
rate_limit             blocked  {"requests": 140, "succeeded": 120, "refused_429": 20, "other": 0}

$ scripts/audit-query.py sess-a67e3d16 | grep egress | head -12   # the gateway's own record of what it decided
15:28:41.996  egress-gateway     egress.deny          host=example.com port=443 status=403 reason=example.com is not on this session's allowlist
15:28:42.007  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:28:42.062  egress-gateway     egress.deny          host=example.com port=443 status=403 reason=example.com is not on this session's allowlist
15:29:59.271  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.317  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.402  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.447  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.499  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.545  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.599  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.694  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:29:59.743  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | a host on the allowlist (pypi.org) is reachable through the gateway | The gateway matched pypi.org against the session's allowlist and tunnelled the connection. |
| PASS | a host not on the allowlist (api.github.com) is refused with 403 | The gateway answers CONNECT with 403 before any connection to the host is opened. |
| PASS | example.com is refused for session A because A did not ask for it | Allowlists are per session. |
| PASS | example.com is reachable for session B, which was approved for it | Same sandbox image, same network; only the session's allowlist differs. |
| PASS | a session allowlist naming a host outside the network ceiling still cannot reach it (not a 403: the gateway tried and the network refused) | The gateway's own network policy lists only the approved domains, so a mistake in a session allowlist cannot open the internet. |
| PASS | the gateway ALLOWED the github.com request for session C (so the allowlist did not stop it); the network ceiling did | The audit record shows an allow decision; the connection still never completed, which only the gateway's network policy can explain. |
| PASS | from the gateway's own pod, pypi.org connects and github.com does not (so the network ceiling is what stops it) | Same pod, same moment, same internet route. Only the destination's presence in the gateway's network policy differs. |
| PASS | a session that opens 140 tunnels in a minute is cut off at the rate limit | The gateway allows 120 requests per minute per session; it refused 20. |
| PASS | the gateway logged both allow and deny decisions for the session | Every decision is written with the session ID. |

**Measured in this run:** Session A is created with the template's default allowlist: pypi.org, files.pythonhosted.org and two internal hosts.

**Why you can trust this result.**

- Allow and deny are both observed from inside the sandbox (the tunnel is refused with 403 or it opens) and from the gateway's own log, which is queried by session ID.
- The ceiling test is designed so that the gateway's decision and the network's decision can be told apart. The gateway log must show an allow for the host while the connection still fails.
- The rate-limit test counts the 429 responses itself; it does not infer them.

**Why you get this output.**

- Session A reaches `pypi.org` and is refused `api.github.com` and `example.com` because the gateway matches each CONNECT request against that session's allowlist, which the Manager stored on the claim when the session was created.
- Session B reaches `example.com` because it was created with that host approved. Same image, same network, different list.
- Session C names `github.com`. The gateway allows it (the log says so), yet the connection never completes. That is the network ceiling: the gateway's own network policy lists only three domains, so a wrong allowlist cannot open the internet.
- The 429s appear because the gateway allows 120 requests a minute per session. A Service setting pins each sandbox to one gateway replica so the count is exact.

### S07: Credentials: secrets never enter the sandbox

**Design reference:** Credentials: secrets never enter the sandbox; Threat model arrow 5.

**What this tests.** Show a sandbox that holds only a placeholder successfully calling a protected API, because the gateway swaps in a short-lived credential outside the sandbox.

The most common way an agent leaks a secret is by reading it from its own environment. The design removes the secret from the sandbox. I check that the sandbox holds only a placeholder, that a call needing a real credential still succeeds, and that the credential is short-lived, verified, and never logged.

![S07: Credentials: secrets never enter the sandbox](diagrams/appendix/a02-evidence/S07.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create cred coding python-untrusted 900
{"http_status": 201, "session_id": "sess-799940c6", "claim": "sess-799940c6", "template": "python-untrusted", "ready_ms": 339, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ orch exec sess-799940c6 "python3 /workspace/drills.py secrets_in_sandbox,credential_injection,direct_to_metrics session=sess-799940c6"
secrets_in_sandbox     blocked  {"placeholder_variables": ["METRICS_API_KEY"], "suspicious": {}, "credential_paths_present": []}
credential_injection   allowed  {"METRICS_API_KEY_in_environment": "placeholder", "status": 200, "caller_session": "sess-799940c6", "credential_expires_in_s": 899, "matches_this_session": true, "note…
direct_to_metrics      blocked  {"error": ["urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name resolution>"]}

$ kubectl -n sandbox-egress exec <gateway pod> -- python -c "call the metrics API with a forged credential, then with none"
forged credential -> (401, {'error': 'credential rejected: bad signature'})
no credential     -> (401, {'error': 'credential rejected: malformed'})

$ scripts/audit-query.py sess-799940c6 | grep -E 'credential|metrics-api'
15:21:01.298  egress-gateway     egress.allow         host=metrics-api.sandbox-platform.svc port=443 tls_mode=terminate
15:21:01.326  credential-broker  credential.minted    caller=egress-gateway audience=metrics-api ttl_seconds=900 credential_id=e61b4e3e98
15:21:01.327  egress-gateway     egress.request       host=metrics-api.sandbox-platform.svc method=GET path=/v1/metrics injected=True credential_id=e61b4e3e98
15:21:01.328  metrics-api        request.served       metric=orders_per_minute
15:21:01.373  egress-gateway     egress.response      host=metrics-api.sandbox-platform.svc status=200 bytes=138
15:21:21.640  sandbox-router     exec                 backend=sandboxd command=python3 /workspace/drills.py secrets_in_sandbox,credential_injection,direct_to_metrics session=sess-799940c6 exit_code=0…

$ kubectl logs <every platform component> | grep -c <this session's token>
0

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the sandbox's environment holds only the word 'placeholder' | The template sets the variable to the literal placeholder; there are no Secrets in the namespace. |
| PASS | the protected API accepted the request and identified this session | The gateway minted a credential for this session and replaced the Authorization header on the way out. |
| PASS | the credential is short-lived (15 minutes at most) | The broker caps the lifetime for this audience. |
| PASS | calling the API without the gateway is impossible | No route and no DNS from the sandbox to the service. |
| PASS | the API rejects a forged credential and a missing one | It verifies the signature, audience and expiry of every credential. |
| PASS | the broker and the API logged the credential by ID, and the API saw this session | Both sides record the same session ID and a credential ID, never the credential. |
| PASS | the session token appears in none of the platform logs | The audit logger redacts any field named like a secret, and no component logs request headers. |

**Why you can trust this result.**

- The success is end to end. The protected API checks the signature, audience and expiry of the credential it receives and names the session it was minted for. The sandbox cannot forge that.
- The negative tests (a forged credential, no credential) call the real API and record its real 401 responses.
- The log search uses this session's actual token as the search string. Zero matches across every platform log is a measured fact about this run.

**Why you get this output.**

- The sandbox's environment holds the word `placeholder` because that is the literal value in the template, and the namespace holds no Secrets.
- The call succeeds because the gateway terminated TLS for that one host, asked the broker for a credential for this session, and replaced the `Authorization` header on the way out. The credential never travelled back to the sandbox.
- The lifetime is at most 900 seconds because the broker caps it per audience, and the API rejects expired credentials.
- The token is absent from the logs because the shared audit function redacts any field named like a secret, and no component logs request headers.

### S08: The tool gateway (MCP): policy, approvals, rate limits and revocation

**Design reference:** Tools and external services.

**What this tests.** Show tools gated by the user and agent behind a session token, human approval for a high-risk tool, a hard no for a forbidden one, a rate limit, and revocation when the session ends.

Agents also call tools: internal APIs and services. The design puts those behind a gateway that knows who is asking. I test tool visibility by agent, a read call, human approval for a risky tool, a forbidden tool, a rate limit, calling from inside a sandbox, and revocation.

![S08: The tool gateway (MCP): policy, approvals, rate limits and revocation](diagrams/appendix/a02-evidence/S08.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create tools data-analyst python-untrusted 900
{"http_status": 201, "session_id": "sess-1e71f9a2", "claim": "sess-1e71f9a2", "template": "python-untrusted", "ready_ms": 338, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ orch mcp <signed-token-redacted> tools/list
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "metrics.query", "description": "Read a metric series from the company metrics API.", "risk": "read"}, {"name": "ticket.c…

$ orch mcp <signed-token-redacted> tools/list
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "metrics.query", "description": "Read a metric series from the company metrics API.", "risk": "read"}]}}

$ orch mcp <signed-token-redacted> tools/call '{"name": "metrics.query", "arguments": {"name": "orders_per_minute"}}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{\"metric\": \"orders_per_minute\", \"points\": [412, 398, 431, 455, 470, 466], \"caller_session\": \"…

$ orch mcp <signed-token-redacted> tools/call '{"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "this tool needs a human approval", "data": {"approval_id": "apr-6a3fb1be", "state": "pending"}}}

$ orch mcp <signed-token-redacted> tools/call '{"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}, "approval_id": "apr-6a3fb1be"}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "approval still pending", "data": {"approval_id": "apr-6a3fb1be", "state": "pending"}}}

$ orch approve apr-6a3fb1be
{"http_status": 200, "approval_id": "apr-6a3fb1be", "state": "approved"}

$ orch mcp <signed-token-redacted> tools/call '{"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}, "approval_id": "apr-6a3fb1be"}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{\"ticket\": \"OPS-101\", \"title\": \"Latency spike on checkout\"}"}]}}

$ orch mcp <signed-token-redacted> tools/call '{"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}, "approval_id": "apr-6a3fb1be"}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "approval still pending", "data": {"approval_id": "apr-6a3fb1be", "state": "used"}}}

$ orch mcp <signed-token-redacted> tools/call '{"name": "project.delete", "arguments": {}}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "error": {"code": -32003, "message": "tool 'project.delete' is not available to agent 'data-analyst'"}}

$ orch mcp <signed-token-redacted> tools/call '{"name": "ticket.create", "arguments": {"title": "x"}}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "error": {"code": -32003, "message": "tool 'ticket.create' is not available to agent 'coding'"}}

$ orch exec sess-f8f6da9a "python3 /workspace/drills.py tool_gateway session_token=<session_token>"
tool_gateway           allowed  {"tools": ["metrics.query"], "note": "positive control"}

$ orch mcp <session token> tools/list   # 25 times in a row
http status of each call: 200 200 200 200 200 200 200 200 200 200 200 200 200 200 200 200 200 429 429 429 429 200 429 429 429

$ orch mcp <signed-token-redacted> tools/list
{"http_status": 401, "jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "session is no longer active"}}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the data-analyst agent sees metrics.query and ticket.create; the coding agent sees only metrics.query | The gateway filters the tool list by the agent identity inside the session token. |
| PASS | a read-only tool call succeeds and reaches the metrics API with a credential the gateway obtained itself | The tool gateway asks the broker for a 60-second credential for this session. |
| PASS | a high-risk tool call is paused until a human approves it | The tool is marked high risk in the gateway's policy. |
| PASS | retrying before approval is still refused | The approval is still pending. |
| PASS | after approval the call runs | The approval is bound to this session, tool and arguments. |
| PASS | the approval cannot be reused | One approval, one call. |
| PASS | a forbidden tool is refused outright | No agent is in its allow list. |
| PASS | the coding agent cannot call a tool that only the data-analyst may | Per-agent policy. |
| PASS | code inside the sandbox can reach the tool gateway only through the egress gateway, using the session token | The host is on the template's allowlist; the request carries the session token. |
| PASS | a session that calls the gateway too often is rate limited (HTTP 429) | The gateway allows 20 calls a minute per session. |
| PASS | when the session ends its token stops working immediately | The gateway asks the Manager whether the session is still active on every call. |

**Why you can trust this result.**

- Every call is a real JSON-RPC request to the gateway with a real session token; the responses printed are the gateway's own.
- The approval flow is tested adversarially: retry before approval, reuse after approval, call a tool the agent has no right to.
- Revocation is tested by ending the session and then calling again with the old, still unexpired, token.

**Why you get this output.**

- The two agents see different tool lists because the gateway filters by the agent name carried inside the signed session token.
- The risky tool pauses because it is marked high risk. The approval is bound to the session, the tool and the arguments, and is consumed by one call.
- The 429s appear after 20 calls a minute because that is the gateway's limit per session.
- The old token stops working at once because the gateway asks the Manager whether the session is still active on every call, rather than trusting the token's expiry alone.

### S09: Tier 3: a microVM with no network device

**Design reference:** Choosing the isolation boundary (Tier 3); Files, state and results.

**What this tests.** Run an analysis on an uploaded file in a sandbox that has no network at all, and get the result back through the router.

Some work needs no network at all, such as running an analysis on an uploaded file. Tier 3 of the design boots the microVM with no network device. I run a real analysis in one and confirm that nothing can leave it.

![S09: Tier 3: a microVM with no network device](diagrams/appendix/a02-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create analytics data-analyst compute-only 900
{"http_status": 201, "session_id": "sess-cd4cdd81", "claim": "sess-cd4cdd81", "template": "compute-only", "ready_ms": 349, "allow_hosts": [], "shutdown_time": "2026-11-19T15:38:07Z", "session_token":…

$ kubectl -n sandboxes get pod compute-only-pool-plgv5 -o jsonpath='{.metadata.annotations}'
{"agents.x-k8s.io/claim-name":"sess-cd4cdd81","agents.x-k8s.io/propagated-annotations":"karpenter.sh/do-not-disrupt,sandbox.amazonaws.com/network-isolated","agents.x-k8s.io/propagated-labels":"app,ds…

$ orch put-text sess-cd4cdd81 uploads/sales.csv 'region,sales
north,120
south,95
north,80
west,60
'
{"http_status": 200, "written": "uploads/sales.csv", "bytes": 49}

$ orch exec sess-cd4cdd81 "python3 -c <sum sales by region from /workspace/uploads/sales.csv>"
{'north': 200, 'south': 95, 'west': 60}
[exit code 0]

$ orch get sess-cd4cdd81 result.json
{"http_status": 200, "path": "result.json", "bytes": 39, "content": "{\"north\": 200, \"south\": 95, \"west\": 60}"}

$ orch exec sess-cd4cdd81 "python3 /workspace/drills.py no_network_device "
no_network_device      blocked  {"interfaces": ["dummy0", "lo"], "connect_1.1.1.1:443": "OSError: [Errno 101] Network is unreachable"}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the analysis ran and produced the totals | The router reached the sandbox through the Kubernetes exec channel, which does not need a network. |
| PASS | the result file came back through the router | File transfer uses the same channel. |
| PASS | the sandbox has no network device and no route out | The annotation boots the microVM without a network interface, so there is nothing to filter and nothing to misconfigure. |

**Why you can trust this result.**

- The analysis result is computed inside the sandbox and read back through the router, so a correct total proves the code ran there.
- The no-network check lists the interfaces the guest kernel actually has and attempts a connection. It does not rely on a configuration flag.

**Why you get this output.**

- Commands and files still work with no network because the router uses the Kubernetes exec channel for this tier, which reaches the guest over a virtual socket rather than over the network.
- No route out exists because the pod annotation makes the microVM boot without a network interface. There is nothing to filter and nothing to misconfigure.

### S10: Limits, quotas and abuse

**Design reference:** Limits, quotas and abuse.

**What this tests.** Show the per-tenant session quota, the hard session end time, the namespace quota and limits, and that a runaway process is contained inside its own sandbox.

Abuse comes in three forms: too many sessions, sessions that never end, and code that eats memory. Each has its own control, and each is tested.

![S10: Limits, quotas and abuse](diagrams/appendix/a02-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl -n sandboxes get resourcequota,limitrange
NAME                            REQUEST                                                     LIMIT   AGE
resourcequota/sandboxes-quota   pods: 4/30, requests.cpu: 2/24, requests.memory: 4Gi/48Gi           10m

NAME                            CREATED AT
limitrange/sandboxes-defaults   2026-11-19T15:12:51Z

$ orch create quotaco coding compute-only 900
{"http_status": 201, "session_id": "sess-b40519d3", "claim": "sess-b40519d3", "template": "compute-only", "ready_ms": 5030, "allow_hosts": [], "shutdown_time": "2026-11-19T15:38:31Z", "session_token"…

$ orch create quotaco coding compute-only 900
{"http_status": 429, "error": "tenant quotaco already has 3 active sessions (limit 3)"}

$ orch create ttlco coding compute-only 45
{"http_status": 201, "session_id": "sess-2f50e944", "claim": "sess-2f50e944", "template": "compute-only", "ready_ms": 335, "allow_hosts": [], "shutdown_time": "2026-11-19T15:24:35Z", "session_token":…

$ kubectl -n sandboxes get sandboxclaim sess-2f50e944 -o jsonpath='{.spec.lifecycle}'
{"shutdownPolicy":"Delete","shutdownTime":"2026-11-19T15:24:35Z"}

$ kubectl -n sandboxes get sandboxclaim sess-2f50e944   # after the end time

Error from server (NotFound): sandboxclaims.extensions.agents.x-k8s.io "sess-2f50e944" not found

$ orch create memco coding python-untrusted 900
{"http_status": 201, "session_id": "sess-760f2688", "claim": "sess-760f2688", "template": "python-untrusted", "ready_ms": 349, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ orch exec sess-760f2688 "python3 /workspace/drills.py memory_bomb mb=2500"
harness                error    {"stdout": "{\"http_status\": 500, \"error\": \"internal error\"}\n", "stderr": ""}

$ kubectl -n sandboxes get pod <the abuser> <its neighbour> -o custom-columns=<restarts, last termination reason>
NAME                          RESTARTS   LAST_TERMINATION
python-untrusted-pool-n47r6   1          OOMKilled
python-untrusted-pool-dsdhw   0          <none>

$ kubectl get node i-0543f2a2608a827e3
NAME                  STATUS   ROLES    AGE   VERSION
i-0543f2a2608a827e3   Ready    <none>   10m   v1.37.1-eks-577a51e

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | a tenant's fourth concurrent session is refused cleanly with 429 | The Manager counts a tenant's active claims against its limit of three. |
| PASS | another tenant is not affected by that limit | Limits are per tenant. |
| PASS | the claim, and with it the sandbox, was deleted when its end time passed, with nobody asking | shutdownTime with shutdownPolicy Delete is set on the claim; the controller enforces it. |
| PASS | asking for 2.5 GiB inside a 1 GiB sandbox was stopped by the memory limit (the abuser's container was restarted) | The pod's memory limit is enforced by the guest kernel's cgroup, inside the microVM. |
| PASS | the neighbouring sandbox on the same node was not touched | Each sandbox has its own kernel and its own memory. |
| PASS | the node stayed Ready | The microVM boundary kept the failure inside the guest. |

**Measured in this run:** TTL test: a session with a 45 second lifetime is created and left alone.

**Why you can trust this result.**

- The quota test creates real sessions until the limit and records the real refusal; a second tenant is created afterwards to show the limit is per tenant.
- The end-time test creates a session, does nothing, and waits for the cluster to remove it.
- The memory test asks for more memory than the sandbox has, then checks the abuser's restart count, its neighbour's restart count, and the node's condition.

**Why you get this output.**

- The fourth session is refused because the Manager counts a tenant's active claims against a limit of three.
- The claim disappears on its own because the Manager stamped an end time and a delete policy on it, and the controller enforces both.
- The runaway process is contained because the memory limit is enforced by the guest kernel inside the sandbox's own microVM. The container is killed and restarted. The neighbour sandbox and the node are untouched, because they are separate kernels.

### S11: One query for an audit

**Design reference:** Observability and audit.

**What this tests.** Run one realistic session, then answer 'what exactly did this agent do?' with a single command.

A customer asks what the agent did with their data. The design answers with one query. I run a realistic session, then run that query.

![S11: One query for an audit](diagrams/appendix/a02-evidence/S11.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create audit coding python-untrusted 900
{"http_status": 201, "session_id": "sess-8cfa2d1f", "claim": "sess-8cfa2d1f", "template": "python-untrusted", "ready_ms": 341, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ orch exec sess-8cfa2d1f "python3 -c \"print(sum(range(10)))\""
45
[exit code 0]

$ orch exec sess-8cfa2d1f "python3 /workspace/drills.py approved_host,unapproved_host,credential_injection "
approved_host          allowed  {"https://pypi.org/simple/": 200, "note": "positive control: allowed is correct"}
unapproved_host        blocked  {"error": "Tunnel connection failed: 403 Forbidden"}
credential_injection   blocked  {"METRICS_API_KEY_in_environment": "placeholder", "status": 200, "caller_session": "sess-8cfa2d1f", "credential_expires_in_s": 899, "matches_this_session": false, "not…

$ orch mcp <signed-token-redacted> tools/call '{"name": "metrics.query", "arguments": {"name": "p99_latency_ms"}}'
{"http_status": 200, "jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{\"metric\": \"p99_latency_ms\", \"points\": [182, 175, 190, 188, 204, 199], \"caller_session\": \"ses…

$ scripts/audit-query.py sess-8cfa2d1f
15:25:12.204  sandbox-manager    session.created      agent=coding user_hash=8a6fa8f54b0d template=python-untrusted sandbox=python-untrusted-pool-zkp5f node=i-0543f2a2608a827e3 ready_ms=341 launch_ty…
15:25:14.157  sandbox-router     exec                 backend=sandboxd command=python3 -c "print(sum(range(10)))" exit_code=0 duration_ms=121 stdout_len=3
15:25:16.119  sandbox-router     file.upload          backend=sandboxd path=drills.py bytes=12756 duration_ms=32
15:25:18.594  egress-gateway     egress.allow         host=pypi.org port=443 tls_mode=passthrough
15:25:18.647  egress-gateway     egress.deny          host=api.github.com port=443 status=403 reason=api.github.com is not on this session's allowlist
15:25:18.657  egress-gateway     egress.allow         host=metrics-api.sandbox-platform.svc port=443 tls_mode=terminate
15:25:18.675  credential-broker  credential.minted    caller=egress-gateway audience=metrics-api ttl_seconds=900 credential_id=4e84fda7c8
15:25:18.676  egress-gateway     egress.request       host=metrics-api.sandbox-platform.svc method=GET path=/v1/metrics injected=True credential_id=4e84fda7c8
15:25:18.677  metrics-api        request.served       metric=orders_per_minute
15:25:18.722  egress-gateway     egress.response      host=metrics-api.sandbox-platform.svc status=200 bytes=138
15:25:18.737  sandbox-router     exec                 backend=sandboxd command=python3 /workspace/drills.py approved_host,unapproved_host,credential_injection exit_code=0 duration_ms=647 stdout_len=5…
15:25:20.755  credential-broker  credential.minted    caller=tool-gateway audience=metrics-api ttl_seconds=60 credential_id=3561bae332
15:25:20.760  metrics-api        request.served       metric=p99_latency_ms
15:25:20.761  tool-gateway       tool.called          agent=coding tool=metrics.query risk=read ok=True
-- 14 records from 6 components for sess-8cfa2d1f

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | a single query returns records from at least six components for the session | Every component writes JSON lines that carry the same session ID. |
| PASS | the timeline contains session.created | Who created it, what ran, what was refused, which credential was issued, which tool was called. |
| PASS | the timeline contains exec | Who created it, what ran, what was refused, which credential was issued, which tool was called. |
| PASS | the timeline contains egress.deny | Who created it, what ran, what was refused, which credential was issued, which tool was called. |
| PASS | the timeline contains credential.minted | Who created it, what ran, what was refused, which credential was issued, which tool was called. |
| PASS | the timeline contains tool.called | Who created it, what ran, what was refused, which credential was issued, which tool was called. |

**Measured in this run:** components present in the timeline: ['14', 'credential-broker', 'egress-gateway', 'metrics-api', 'sandbox-manager', 'sandbox-router', 'tool-gateway']

**Why you can trust this result.**

- The timeline is assembled from the components' own logs, sorted by their own timestamps. I do not add or edit lines.
- The checks look for specific events that only the right component could have written.

**Why you get this output.**

- One query finds records in every component because they all write JSON lines carrying the same session ID, and the query reads them by that ID.
- Credentials appear by ID and never by value because the audit function redacts secret-like fields.

### S12: Failure drills

**Design reference:** Failure modes and operations.

**What this tests.** Take a component away and check that the design fails the way its table says: closed for the gateway, and without disturbing running sessions for the controller.

The design's failure table says how each part fails. I remove two parts of the running system and check that it fails the way the table says.

![S12: Failure drills](diagrams/appendix/a02-evidence/S12.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ orch create fail coding python-untrusted 1200
{"http_status": 201, "session_id": "sess-23ff97b4", "claim": "sess-23ff97b4", "template": "python-untrusted", "ready_ms": 333, "allow_hosts": ["files.pythonhosted.org", "metrics-api.sandbox-platform.…

$ orch exec sess-23ff97b4 "python3 /workspace/drills.py approved_host "
approved_host          allowed  {"https://pypi.org/simple/": 200, "note": "positive control: allowed is correct"}

$ kubectl -n sandbox-egress scale deploy/egress-gateway --replicas=0
deployment.apps/egress-gateway scaled

$ orch exec sess-23ff97b4 "python3 /workspace/drills.py approved_host "
approved_host          error    {"exception": "ConnectionRefusedError: [Errno 111] Connection refused"}

$ kubectl -n sandbox-egress scale deploy/egress-gateway --replicas=2
deployment.apps/egress-gateway scaled

$ kubectl -n sandbox-egress rollout status deploy/egress-gateway --timeout=180s
Waiting for deployment "egress-gateway" rollout to finish: 0 of 2 updated replicas are available...
Waiting for deployment "egress-gateway" rollout to finish: 1 of 2 updated replicas are available...
deployment "egress-gateway" successfully rolled out

$ orch exec sess-23ff97b4 "python3 /workspace/drills.py approved_host "
approved_host          allowed  {"https://pypi.org/simple/": 200, "note": "positive control: allowed is correct"}

$ kubectl -n agent-sandbox-system scale deploy/agent-sandbox-controller --replicas=0
deployment.apps/agent-sandbox-controller scaled

$ orch exec sess-23ff97b4 "echo still running; uname -r"   # controller is down
still running
6.18.62
[exit code 0]

$ kubectl -n agent-sandbox-system scale deploy/agent-sandbox-controller --replicas=1
deployment.apps/agent-sandbox-controller scaled

$ kubectl -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=120s
Waiting for deployment "agent-sandbox-controller" rollout to finish: 0 of 1 updated replicas are available...
deployment "agent-sandbox-controller" successfully rolled out

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | with the gateway down the sandbox loses outbound access (fails closed) | The only route out is the gateway; with no gateway there is no route. |
| PASS | when the gateway returns the same sandbox reaches its allowed host again | Nothing in the sandbox had to change. |
| PASS | with the sandbox controller down, a running session keeps working | The controller only reconciles claims; the sandbox and its router path do not depend on it. |

**Why you can trust this result.**

- The same drill runs before, during and after, in the same sandbox, so the only variable is the component I removed.
- Components are removed and restored with ordinary Kubernetes commands that are shown in the evidence.

**Why you get this output.**

- With the gateway removed the sandbox loses outbound access because the gateway is its only route out. The system fails closed.
- With the controller removed a running session keeps working because the controller only reconciles claims. The sandbox and the router's path to it do not depend on it.

### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 01 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **GPU sandboxes** | The design lists this as an open question. AWS documents that Nitro Sandboxes do not support GPU or other device passthrough ([considerations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)), so a GPU workload would need whole nodes without a sandbox runtime. A CPU sandbox proves the same isolation, network and credential design. |
| **gVisor (Tier 1) and Kata (Tier 2 alternative)** | EKS Auto Mode does not allow the shim gVisor needs, and Kata needs self-managed nodes with nested virtualization. Tiers 2 and 3 on Nitro Sandboxes are the design's default and are fully built. |
| **Confidential mode** | The design says agent sandboxes need isolated mode. Confidential mode is for regulated data and removes `exec`, which this design depends on. |
| **A persistent workspace volume, pause and resume, checkpoints** | The workspace is a size-limited scratch volume that lives and dies with the pod. The design's EBS-backed workspace and its pause and resume are not built. |
| **Snapshots and multi-cluster placement** | Open questions in the design itself. |
| **A separate certificate authority per sandbox** | One gateway CA is trusted by every sandbox. The design describes one per sandbox, discarded with it. |
| **A shared store for the tool gateway** | Approvals and rate counters are in memory, so the tool gateway runs as one replica. A production build keeps them in a shared store such as DynamoDB. |
| **An append-only audit store** | Audit lines go to each pod's log and are joined by a script. The design calls for an append-only store with its own retention. The EKS control-plane audit log does go to CloudWatch. |
| **A runtime-security agent on the nodes** | The design itself notes it sees nothing inside a microVM. The audit trail is built from the boundaries instead. |
| **Image signing and a private registry mirror** | The sandbox images are public images pulled through the NAT gateway. The design wants them pinned, signed and mirrored. |
| **A warm-pool autoscaler** | The pool sizes are fixed (2 and 1). |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Docker running (it builds one image), and Python 3 with Pillow for the screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check.

**Build.**

```text
cd implementations/ds01-sandboxing-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes two small local files with your account ID and your public address (both git-ignored), builds the cluster and the platform, and waits for the warm pools. It takes roughly 20 to 25 minutes.

**Test.**

```text
python3 tests/run_scenarios.py all
```

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` ends live sessions, destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, repositories, roles and log groups. It prints `CLEAN` when nothing is left.

**Evidence that teardown works.** The first build of this implementation was destroyed with the same Terraform layers before the final build: 87 platform resources and then 40 cluster resources, and `verify-clean.sh` reported that nothing from it remained. The final build was then created from nothing with `up.sh`, and every scenario above ran against it.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here, because signing in needs credentials and a browser session I deliberately do not drive. If you want console pictures in your portfolio, these pages show the same facts and are quick to capture. Crop out the account number and your user name in the top bar before publishing.

- EKS, the cluster's overview: the Kubernetes version and "Auto Mode: enabled".
- EKS, Compute: the `sandbox` node pool and its nodes.
- EC2, Instances: the sandbox node (`c7i.xlarge`), and its Security tab showing the dedicated security group.
- VPC, Subnets: the two sandbox subnets with the `ds01/tier=sandbox` tag.
- IAM, Roles: the sandbox node role with its two policies.
- ECR: the `ds01-platform` repository.
### 9. An alternative egress gateway on Envoy

**Scenario.** The tested egress gateway runs on mitmproxy. That was a good fit for a test, because the policy is easy to write on it. It is a less obvious choice for a platform that other people will operate. mitmproxy describes itself as an interactive HTTPS proxy for debugging, testing and penetration testing, and it does not appear in the CNCF landscape data. [Envoy](https://www.envoyproxy.io) does, and the same data lists it as a graduated CNCF project ([landscape data](https://github.com/cncf/landscape/blob/master/landscape.yml), read on the day of writing). So I wrote the same gateway on Envoy, in case the platform ever needs it, and ran it in a lab.

**What I did and did not test.** I ran this in Docker on one machine, with test host names and a policy service that reads a table of sessions. It did not run on EKS, it did not call the real Manager or credential broker, and it is not part of the 12 scenarios or the 75 checks above. What it shows is that the plumbing works: Envoy can do the two jobs from section 1 (an untouched tunnel for ordinary hosts, and TLS ending for the one host that needs a key) and can hand every decision to a separate policy service.

**How the work is split.** Envoy carries the traffic. A small policy service makes the decisions. That is the main difference from the tested gateway, where one add-on did both.

| Job | In the tested gateway (mitmproxy add-on) | On Envoy |
|---|---|---|
| Accept the sandbox's request | An HTTP proxy port | A listener on port 3128 that accepts `CONNECT` and builds the tunnel |
| Find the session that owns the source address | The add-on asks the Manager | Envoy asks the policy service (the `ext_authz` filter, over gRPC) and passes it the source address. The service asks the Manager. |
| Check the host against the session's allowlist | The add-on | The policy service. A refusal becomes a 403 on the `CONNECT`. |
| Rate limit | Counters in the add-on | Counters in the policy service. A refusal becomes a 429. |
| Byte budget per session | Counted in the add-on | Not built in the lab. Envoy logs the bytes of every tunnel, but enforcing a budget would need another component. |
| Pass TLS through for ordinary hosts | The add-on tells mitmproxy to ignore the connection | A route to a dynamic forward proxy cluster, which resolves the host name and opens a plain TCP tunnel |
| End TLS for the one host that needs a key | The add-on terminates TLS and edits the request | A route sends that host's tunnel to a loopback-only listener that ends TLS. A PROXY protocol header on that hop keeps the sandbox's address, so the second policy check still knows the session. |
| Supply the credential | The add-on asks the broker and sets the header | The second policy check asks the broker and returns the `Authorization` header, and Envoy overwrites the sandbox's placeholder with it |
| Audit line with the session ID | The add-on writes it | Envoy's access log, with the session ID that the policy service returns as metadata |

![Egress gateway on Envoy: one listener, a pass-through tunnel, and a loopback listener that injects the credential](diagrams/appendix/a02-8.svg)

**What the picture shows.** The tunnel from a sandbox arrives at Envoy's listener (1). Envoy asks the policy service about the session (2). For an ordinary host the tunnel goes straight to the site (3). For the one internal host, the tunnel goes to a loopback listener that ends TLS (4), asks the policy service again for the credential (5), and calls the internal API (6).

**Commands I ran and what they printed** (the whole run is `lab/run-lab.sh`; output shortened).

```text
$ curl -x http://envoy:3128 --cacert ca.pem https://allowed.test/          # run in the sandbox container
{"server_host": "allowed.test", "authorization_seen": ""}
$ curl -x http://envoy:3128 --cacert ca.pem https://blocked.test/          # a host not on the session's list
tunnel status: 403
$ curl -x http://envoy:3128 --cacert ca.pem -H "Authorization: Bearer placeholder" https://metrics.test/v1/metrics
{"server_host": "metrics.test", "authorization_seen": "Bearer real-credential-for-sess-lab-0001"}
$ (same call from a container that owns no session)
tunnel status: 403
$ (eight calls in a row to the allowed host, with a lab limit of five a minute)
tunnel status: 200 for the first calls, then 429
$ curl https://envoy:8443/                                                  # the loopback listener, from another container
curl: exit code 7 (could not connect)
$ docker logs lab-envoy | grep egress.decision
{"component":"egress-gateway","detail":"ext_authz_denied","event":"egress.decision","host":"blocked.test:443","session_id":"sess-lab-0001","status":403,...}
{"component":"egress-gateway","event":"egress.request","host":"metrics.test","injected":"true","method":"GET","path":"/v1/metrics","session_id":"sess-lab-0001","status":200}
```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output |
|---|---|---|
| PASS | Envoy validates the configuration | Envoy's own validation mode accepts it. |
| PASS | an allowlisted host is reached through a tunnel, and the sandbox verifies the site's own certificate | The tunnel is plain TCP, so the TLS handshake runs between the sandbox and the site and Envoy never sees inside it. |
| PASS | a host that is not on the session's allowlist is refused with 403 | The policy service refuses it, and Envoy turns that into a 403 on the `CONNECT`, before any connection to the host is opened. |
| PASS | the internal host receives the real credential although the sandbox sent only a placeholder | The tunnel for that host ends in the loopback listener. The second policy check returns the real header, and Envoy replaces the sandbox's header with it. |
| PASS | a source address that owns no session is refused with 403 | The policy service finds no session for that address. |
| PASS | a session over its rate limit is refused with 429 | The policy service counts the session's tunnels in the last minute. |
| PASS | the TLS-terminating listener cannot be reached from the network | It listens on the loopback address only, so only Envoy itself can send traffic to it. |
| PASS | the audit log names the session on a refused request | The policy service returns the session ID as metadata with every answer, refusals included, and the log line carries it. |
| PASS | the audit log records the injected request | The loopback listener logs each decrypted request with `injected` set. |

**What I found.**

- **The HTTP form of the check does not work for `CONNECT`.** My first version used `ext_authz` over plain HTTP. Every tunnel failed with the error `ext_authz_error`, and the policy service logged that it had been sent a `CONNECT` request. Switching the same filter to gRPC fixed it with no other change. I did not find out why the HTTP form fails.
- **The source address needs the PROXY protocol on the hop to the loopback listener.** Without it, the second check would see Envoy's own address and could not tell which session is calling. The lab shows that with it, the second check sees the sandbox's address.
- **The list of hosts that get a credential is fixed in Envoy's configuration.** Each such host needs a route, a certificate and an upstream cluster. In the tested gateway the Manager supplies that list per session. On Envoy the per-session part stays in the policy service, which adds the header only when the session is entitled to it, but adding a new internal host means changing the configuration.
- **The rate limit has the same weakness as before.** The counters live in the policy service's memory. Two replicas would each see half the calls, as in section 4, so the Service in front of it needs the same session pinning, or the counters need a shared store.
- **Envoy warns that no limit on downstream connections is set.** The lab ran with that warning. A real deployment should set one.
- **A dynamic forward proxy needs a firewall around it.** Envoy's own documentation says dynamic forward proxy servers should be protected by network firewalls and default-deny rules. In this design the sandbox's network policy and the gateway's domain ceiling (S05, S06) play that part.

**What it would take to use it.** Replace the lab's session table in the policy service with the calls the tested gateway makes: ask the Manager which session owns an address, and ask the broker for a credential. Run Envoy as the gateway Deployment on the same address and port, behind the same Service. Keep the sandbox template unchanged, because it already points at that address. Then run S05, S06 and S07 against it. I have not done any of those steps.

**The configuration** (`alternatives/envoy-egress/envoy.yaml`, as tested with Envoy v1.40.2).

```yaml
# Egress gateway on Envoy: the sandbox's one way out. Mirrors what the mitmproxy gateway does.
#   :3128  CONNECT proxy. ext_authz asks the policy service: which session owns this source address,
#          is the host on its allowlist, is it over its rate limit.
#          Ordinary hosts: a pass-through TCP tunnel (TLS stays end to end between sandbox and site).
#          The one injected host: the tunnel goes to a loopback listener that ends TLS and adds the credential.
#   :8443  (loopback only) TLS-terminating listener for the injected host. The PROXY protocol header on the
#          hop to it carries the sandbox's address, so the second policy check still knows the session.
#
# Tested in lab/run-lab.sh with Envoy v1.40.2. Lab names to replace in a real deployment:
#   metrics.test            the internal host that needs a credential (route, listener certificate, upstream cluster)
#   /certs/*                the certificate Envoy presents for that host, and the CA that signed the upstream's
#   authz                   the policy service's address (policy_service.py is a lab version that reads a table)
admin:
  address: { socket_address: { address: 127.0.0.1, port_value: 9901 } }
static_resources:
  listeners:
  - name: egress
    address: { socket_address: { address: 0.0.0.0, port_value: 3128 } }
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: egress
          use_remote_address: true
          http_protocol_options: {}
          access_log:
          - name: envoy.access_loggers.stdout
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog
              log_format:
                json_format:
                  component: egress-gateway
                  event: egress.decision
                  session_id: "%DYNAMIC_METADATA(envoy.filters.http.ext_authz:session_id)%"
                  host: "%REQ(:AUTHORITY)%"
                  status: "%RESPONSE_CODE%"
                  detail: "%RESPONSE_CODE_DETAILS%"
                  source: "%DOWNSTREAM_REMOTE_ADDRESS_WITHOUT_PORT%"
                  bytes_sent: "%BYTES_SENT%"
                  bytes_received: "%BYTES_RECEIVED%"
          route_config:
            name: egress_routes
            virtual_hosts:
            - name: all
              domains: ["*"]
              routes:
              # The host that needs a credential: tunnel into the TLS-terminating listener.
              - match:
                  connect_matcher: {}
                  headers:
                  - name: ":authority"
                    string_match: { exact: "metrics.test:443" }
                route:
                  cluster: inject_listener
                  upgrade_configs:
                  - upgrade_type: CONNECT
                    connect_config: {}
              # Everything else that the policy service allows: a pass-through tunnel to the named host.
              - match: { connect_matcher: {} }
                route:
                  cluster: any_host
                  upgrade_configs:
                  - upgrade_type: CONNECT
                    connect_config: {}
          http_filters:
          - name: envoy.filters.http.ext_authz
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz
              failure_mode_allow: false
              grpc_service:
                envoy_grpc: { cluster_name: authz }
                timeout: 2s
                initial_metadata: [ { key: x-ds01-mode, value: check } ]
          - name: envoy.filters.http.dynamic_forward_proxy
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.dynamic_forward_proxy.v3.FilterConfig
              dns_cache_config: &dns
                name: egress_dns
                dns_lookup_family: V4_ONLY
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
  - name: inject
    address: { socket_address: { address: 127.0.0.1, port_value: 8443 } }
    listener_filters:
    - name: envoy.filters.listener.proxy_protocol
      typed_config:
        "@type": type.googleapis.com/envoy.extensions.filters.listener.proxy_protocol.v3.ProxyProtocol
    filter_chains:
    - transport_socket:
        name: envoy.transport_sockets.tls
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext
          common_tls_context:
            tls_certificates:
            - certificate_chain: { filename: /certs/metrics.test.pem }
              private_key: { filename: /certs/metrics.test.key }
      filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: inject
          use_remote_address: true
          access_log:
          - name: envoy.access_loggers.stdout
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog
              log_format:
                json_format:
                  component: egress-gateway
                  event: egress.request
                  session_id: "%DYNAMIC_METADATA(envoy.filters.http.ext_authz:session_id)%"
                  host: "%REQ(:AUTHORITY)%"
                  method: "%REQ(:METHOD)%"
                  path: "%REQ(:PATH)%"
                  status: "%RESPONSE_CODE%"
                  injected: "true"
          route_config:
            name: inject_routes
            virtual_hosts:
            - name: metrics
              domains: ["metrics.test", "metrics.test:443"]
              routes:
              - match: { prefix: "/" }
                route: { cluster: metrics_upstream }
          http_filters:
          - name: envoy.filters.http.ext_authz
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz
              failure_mode_allow: false
              grpc_service:
                envoy_grpc: { cluster_name: authz }
                timeout: 2s
                initial_metadata: [ { key: x-ds01-mode, value: inject } ]
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
  clusters:
  - name: any_host
    lb_policy: CLUSTER_PROVIDED
    cluster_type:
      name: envoy.clusters.dynamic_forward_proxy
      typed_config:
        "@type": type.googleapis.com/envoy.extensions.clusters.dynamic_forward_proxy.v3.ClusterConfig
        dns_cache_config: *dns
  - name: inject_listener
    type: STATIC
    connect_timeout: 2s
    load_assignment:
      cluster_name: inject_listener
      endpoints: [ { lb_endpoints: [ { endpoint: { address: { socket_address: { address: 127.0.0.1, port_value: 8443 } } } } ] } ]
    transport_socket:
      name: envoy.transport_sockets.upstream_proxy_protocol
      typed_config:
        "@type": type.googleapis.com/envoy.extensions.transport_sockets.proxy_protocol.v3.ProxyProtocolUpstreamTransport
        config: { version: V1 }
        transport_socket:
          name: envoy.transport_sockets.raw_buffer
          typed_config: { "@type": type.googleapis.com/envoy.extensions.transport_sockets.raw_buffer.v3.RawBuffer }
  - name: metrics_upstream
    type: STRICT_DNS
    connect_timeout: 2s
    load_assignment:
      cluster_name: metrics_upstream
      endpoints: [ { lb_endpoints: [ { endpoint: { address: { socket_address: { address: metrics.test, port_value: 443 } } } } ] } ]
    transport_socket:
      name: envoy.transport_sockets.tls
      typed_config:
        "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext
        sni: metrics.test
        common_tls_context:
          validation_context:
            trusted_ca: { filename: /certs/ca.pem }
            match_typed_subject_alt_names:
            - san_type: DNS
              matcher: { exact: metrics.test }
  - name: authz
    type: STRICT_DNS
    connect_timeout: 1s
    typed_extension_protocol_options:
      envoy.extensions.upstreams.http.v3.HttpProtocolOptions:
        "@type": type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions
        explicit_http_config: { http2_protocol_options: {} }
    load_assignment:
      cluster_name: authz
      endpoints: [ { lb_endpoints: [ { endpoint: { address: { socket_address: { address: authz, port_value: 9000 } } } } ] } ]
```

**The files.** `alternatives/envoy-egress/` holds `envoy.yaml`, `policy_service.py` (the lab policy service, about 60 lines) and `lab/run-lab.sh`, which builds the lab, runs the nine checks and removes everything it started. It needs Docker and nothing else. `up.sh` does not use any of it.

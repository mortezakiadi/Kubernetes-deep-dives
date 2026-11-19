---
id: appendix-design-01-eks
title: "Appendix B: Design 01 built and tested on Amazon EKS"
short: "B. Design 01 on EKS (built and tested)"
---
This appendix describes a working implementation of [Design 01: Sandboxing and secure execution for agents and untrusted code](#design-sandboxing). I built it with Terraform on Amazon EKS in one AWS account, tested every claim in the design that can be tested on this platform, recorded the evidence, and removed it again with one command. Everything here ran on a real cluster. Every command, output and pass or fail result in section 5 is copied from the recorded test run, not written from memory.

The order is the one a reader needs: the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested with the commands, the results, why the results can be trusted and why the system produces them.

{{RESULT_SUMMARY}}

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


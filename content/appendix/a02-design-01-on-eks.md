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

### Defence in depth: where each control lives, and who provides it

**Scenario.** The address plan is the first control, and only the first. Between the agent's script and the outside world there are controls at the network, identity, host, kernel and application levels, and no single one is trusted. This section shows where each control sits, who provides it (AWS, Kubernetes or my design), the manifests and code that enforce it, and the scenario in section 5 that tests it.

![Defence in depth: six rings between untrusted code and the outside, and who provides each control](diagrams/appendix/a02-10.svg)

**What the picture shows.** Six rings, from the AWS account on the outside to the process in the middle. Each line is one control, and the label after it says who provides it. **AWS** means an AWS service or built-in behaviour that I rely on. **K8s** means a Kubernetes feature that I configured. **Ours** means a choice I made in the design or code I wrote. Most controls have two labels, because AWS or Kubernetes supplies the mechanism and the design supplies the rule. The panel on the right holds the controls that sit on the one way out, at the application level, and the gaps this build leaves open.

**Which layer, who provides it, and where it is tested.**

| # | Layer | Control | Provided by | Tested in |
|---|---|---|---|---|
| 1 | VPC and subnets (network) | Sandbox blocks apart from platform blocks, no public IP on nodes, one NAT gateway for outbound traffic | AWS: VPC, subnets, route tables, NAT. Ours: the split and the tags | S01 |
| 2 | Security groups (network) | Sandbox nodes in their own group, which admits only the cluster group | AWS: security groups. Ours: the group and its two rules | S01 |
| 3 | Cluster API access (identity) | Public endpoint limited to the admin address, private endpoint inside the VPC, API-only authentication | AWS: EKS. Ours: the settings | S01 |
| 4 | IAM (identity) | Separate cluster, platform-node and sandbox-node roles with managed minimal policies. No AWS identity for the sandbox service account | AWS: IAM. Ours: the roles | S01, S05 |
| 5 | The sandbox node (host) | Own node pool and node class, a taint, only empty nodes removed. IMDSv2 required with a hop limit of 1 | Ours: pool, class, taint. AWS: the metadata setting (Auto Mode) | S01 |
| 6 | Admission (policy) | Pod Security `restricted` on every namespace, a quota and limits on the sandbox namespace | Kubernetes. Ours: the settings | S03, S10 |
| 7 | Kubernetes RBAC (identity) | Small roles for the Manager and Router, none for the sandbox. No service-account token in any pod except those two | Kubernetes. Ours: the roles | S03 |
| 8 | Pod network (network) | Default deny. A sandbox may reach only the gateway on port 3128, with no DNS. Only the router may reach a sandbox. Every platform service names its callers | Kubernetes: the policy API. AWS: the agent that enforces it in Auto Mode. Ours: the policies | S05 |
| 9 | Domain ceiling (network, by name) | The gateway pods may reach three domain names on port 443 and nothing else | AWS: Auto Mode's domain-name network policy. Ours: the list | S06 |
| 10 | The microVM (kernel) | Own kernel per pod. The metadata address returns placement data only | AWS: Nitro Sandboxes. Ours: naming the runtime class in each template | S02, S05 |
| 11 | The process (host) | Non-root, read-only root, no capabilities, default seccomp profile, no service links | Kubernetes. Ours: the settings | S03 |
| 12 | Gateway policy (application) | Per-session allowlist, rate limit of 120 a minute, byte budget of 50 MiB, TLS ended for one host only | Ours: code on mitmproxy | S06, S07 |
| 13 | Credentials and tokens (application) | A placeholder in the sandbox, signed credentials of 15 minutes at most, a signed session token, per-agent tools, approvals, 20 calls a minute, revocation when the session ends | Ours: code | S07, S08 |
| 14 | Audit (application) | One log line per event with the session ID, and secret-looking fields redacted. Control-plane logs in CloudWatch | Ours: code. AWS: the control-plane log service | S11 |

The layers do four different jobs. Layers 1 to 3, 8 and 9 decide **who can connect**. Layers 4, 7 and 13 decide **who they are once connected**. Layers 5, 10 and 11 decide **what a compromised process can do** if everything else fails. Layers 12 to 14 decide **what is allowed on the one path that is left**, and keep a record of it.

![Nodes in the VPC can talk; what may talk is decided at the pod level](diagrams/appendix/a02-14.svg)

**What the picture shows.** Two levels that work differently. At the VPC level, the security groups are wide open between the platform nodes and the sandbox nodes: the rules name a group, not individual nodes, and they allow every port, in both directions. At the pod level, a network policy on the sandbox decides which pods may talk to it. Only the router's pods may connect to a sandbox, and a sandbox may connect only to the gateway's address on port 3128. The orchestrator, the Manager, the tool gateway and other sandboxes are refused, even though their nodes could reach the sandbox node. S05 tests two of these refusals: the orchestrator, and another sandbox. The security group is the coarse outer wall, and the network policy is the specific control. Network policies govern pod traffic, so what the security groups leave open between the nodes themselves is a reason to keep every platform node trusted. I did not confirm on the live cluster which platform nodes carry the cluster group, because the test cluster has been torn down.

![Who may call whom, and what enforces each allowed path](diagrams/appendix/a02-11.svg)

**What the picture shows.** Every pair of components, with the rule that admits the call. A green cell is a path that exists, and it names the port or the role that admits it. A grey cell is refused, because the called service has an ingress policy that does not name the caller. The Kubernetes API column is controlled by roles, and by the absence of a token in most pods. The amber cells are the honest part. Only the gateway pods and the sandboxes have an egress network policy, so the other platform pods can reach the internet through the NAT gateway. What holds them back is that nothing they run calls out, and that they hold no credential worth stealing. Egress policies for them are a hardening step that this build does not take.

**The manifests and code that enforce it.** Each block below is copied from the file named on its first line, and the build fails if a line of a block is not in that file. Lines that I left out are marked with `# ...`. The lines that actually enforce a control are highlighted in yellow and numbered. The numbered list above each block says what each highlighted line does, in the same order.

**Layers 1 and 2: the sandbox blocks and the sandbox security group.**

The highlighted lines, in order:

1. `cidrsubnet(var.vpc_cidr, 4, 4 + count.index)`: carves the two sandbox blocks (10.20.64.0/20 and 10.20.80.0/20) out of the VPC range, so sandbox nodes have a range of their own.
2. `"ds01/tier" = "sandbox"`: the tag on the subnet. The sandbox node class selects subnets by this tag, so a sandbox node can only be placed in these blocks.
3. `aws_security_group "sandbox_nodes"`: a security group that only sandbox nodes use.
4. `referenced_security_group_id = ...cluster_security_group_id`: the only source this group admits is the cluster security group, which the rule's description calls the control plane and the platform pods. The rule names a group, not individual nodes, and it allows every port, so any machine in that group can connect to a sandbox node at this level. Nothing outside the group can. The narrow control is the pod network policy (layer 8).
5. `referenced_security_group_id = aws_security_group.sandbox_nodes.id`: the reverse rule, on the cluster group: it admits the sandbox group on every port. Its description names two uses, sandbox nodes reaching the control plane and the egress gateway. Pod network policy then limits what a sandbox pod may actually reach.

```hcl
# from terraform/10-cluster/network.tf
resource "aws_subnet" "sandbox" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, 4 + count.index) # 10.20.64.0/20, 10.20.80.0/20 #!
  tags = {
    Name        = "${var.project}-sandbox-${var.azs[count.index]}"
    "ds01/tier" = "sandbox" #!
  }
}
# ...
resource "aws_security_group" "sandbox_nodes" { #!
  name        = "${var.project}-sandbox-nodes"
  description = "Nodes that run untrusted sandboxes"
  vpc_id      = aws_vpc.this.id
  tags = {
    Name        = "${var.project}-sandbox-nodes"
    "ds01/tier" = "sandbox"
  }
}
# ...
resource "aws_vpc_security_group_ingress_rule" "sandbox_from_cluster" {
  security_group_id            = aws_security_group.sandbox_nodes.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id #!
  ip_protocol                  = "-1"
  description                  = "Control plane and platform pods to sandbox nodes"
}
# ...
resource "aws_vpc_security_group_ingress_rule" "cluster_from_sandbox" {
  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.sandbox_nodes.id #!
  ip_protocol                  = "-1"
  description                  = "Sandbox nodes to control plane and the egress gateway"
}
```

**Layers 3 and 4: access to the cluster, and the sandbox node role.**

The highlighted lines, in order:

1. `authentication_mode = "API"`: access to the cluster is granted only through EKS access entries. In this mode the cluster has no aws-auth ConfigMap to fall back to ([AWS docs](https://docs.aws.amazon.com/batch/latest/userguide/eks-access-entries.html)).
2. `endpoint_private_access = true`: turns on the private endpoint, so callers inside the VPC reach the API server without leaving it.
3. `public_access_cidrs = [var.admin_cidr]`: the public endpoint answers only the administrator's own address, a single /32.
4. `enabled_cluster_log_types = ["api", "audit", "authenticator"]`: sends the API, audit and authenticator logs to CloudWatch, so every claim and exec is on record.
5. `"AmazonEKSWorkerNodeMinimalPolicy",`: the first of only two policies on the sandbox node role: what the kubelet and the network plugin need.
6. `"AmazonEC2ContainerRegistryPullOnly",`: the second: read-only image pulls. There is no S3 or Secrets Manager access for an escaped process to use.

```hcl
# from terraform/10-cluster/eks.tf
  access_config {
    authentication_mode                         = "API" #!
    bootstrap_cluster_creator_admin_permissions = true
  }
# ...
  vpc_config {
    # Platform pods (controller, gateways) go in the system subnets. The sandbox
    # subnets are reached through a dedicated NodeClass, not through this list.
    subnet_ids              = aws_subnet.system[*].id
    endpoint_private_access = true #!
    endpoint_public_access  = true
    public_access_cidrs     = [var.admin_cidr] #!
  }
# ...
  enabled_cluster_log_types = ["api", "audit", "authenticator"] #!
# ...
resource "aws_iam_role_policy_attachment" "sandbox_node" {
  for_each = toset([
    "AmazonEKSWorkerNodeMinimalPolicy", #!
    "AmazonEC2ContainerRegistryPullOnly", #!
  ])
  role       = aws_iam_role.sandbox_node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}
```

**Layer 5: the sandbox node.** The node class picks the sandbox blocks by tag, and the sandbox security group. The taint keeps every other pod off.

The highlighted lines, in order:

1. `role: ${node_role}`: the sandbox pool runs under its own node role, not the platform's.
2. `ds01/tier: sandbox`: the node class selects only the sandbox-tagged blocks, so these nodes cannot start in a platform block.
3. `id: ${sandbox_sg}`: pins the dedicated sandbox security group to every node in the pool.
4. `associatePublicIPAddress: false`: no node gets a public IP address.
5. `effect: NoSchedule`: with the key and value above it, this is the taint workload=sandbox:NoSchedule. Only a pod that tolerates it can be placed on these nodes, and only the sandbox template does.
6. `consolidationPolicy: WhenEmpty`: Auto Mode removes a node only when it is empty, so a node that holds a live session is never taken away.

```yaml
# from k8s/10-sandbox-nodes.yaml
apiVersion: eks.amazonaws.com/v1
kind: NodeClass
metadata:
  name: sandbox
spec:
  role: ${node_role} #!
  subnetSelectorTerms:
    - tags:
        ds01/tier: sandbox #!
  securityGroupSelectorTerms:
    - id: ${sandbox_sg} #!
  ephemeralStorage:
    size: 40Gi
  advancedNetworking:
    associatePublicIPAddress: false #!
---
apiVersion: karpenter.sh/v1
# ...
      # Nothing else tolerates this taint, so only sandbox pods can land here.
      taints:
        - key: workload
          value: sandbox
          effect: NoSchedule #!
# ...
  disruption:
    consolidationPolicy: WhenEmpty #!
    consolidateAfter: 10m
```

**Layer 6: admission and limits.**

The highlighted lines, in order:

1. `pod-security.kubernetes.io/enforce: restricted`: the API server rejects any pod in this namespace that breaks the restricted profile. S03 shows a privileged, host-network pod being refused. The audit and warn labels below it only record and warn.

```yaml
# from k8s/00-namespaces.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: sandboxes
  labels:
    ds01/zone: untrusted
    pod-security.kubernetes.io/enforce: restricted #!
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

The highlighted lines, in order:

1. `pods: "30"`: no more than 30 pods in the sandbox namespace.
2. `requests.cpu: "24"`: no more than 24 CPUs requested in total.
3. `requests.memory: 48Gi`: no more than 48 GiB of memory requested in total. Together the three lines cap what runaway sessions can consume.

```yaml
# from k8s/20-sandboxes.yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: sandboxes-quota
  namespace: sandboxes
spec:
  hard:
    pods: "30" #!
    requests.cpu: "24" #!
    requests.memory: 48Gi #!
```

**Layers 10 and 11: the microVM and the process.** The template asks for the isolated runtime class and drops everything a process could use.

The highlighted lines, in order:

1. `runtimeClassName: isolated-sandbox`: asks for a Nitro Sandbox: the pod runs as its own microVM with its own kernel.
2. `automountServiceAccountToken: false`: no Kubernetes credential is mounted in the sandbox, so code inside cannot call the API.
3. `tolerations: workload=sandbox:NoSchedule`: the toleration that matches the node taint. Without it the pod cannot be scheduled on a sandbox node.
4. `nodeSelector: workload: sandbox`: the node selector: the pod can run only on nodes labelled for sandboxes.
5. `runAsNonRoot: true`: the container may not run as root.
6. `allowPrivilegeEscalation: false`: a process cannot gain more privileges than it started with.
7. `readOnlyRootFilesystem: true`: the root file system is read-only. Writable space is limited to the workspace and temporary directories.
8. `capabilities: { drop: ["ALL"] }`: every Linux capability is dropped.

```yaml
# from k8s/20-sandboxes.yaml
      runtimeClassName: isolated-sandbox      # Tier 2: one microVM per pod #!
      serviceAccountName: sandbox-nobody
      automountServiceAccountToken: false     # no Kubernetes credentials inside #!
      enableServiceLinks: false
      tolerations:
        - { key: workload, operator: Equal, value: sandbox, effect: NoSchedule } #!
      nodeSelector:
        workload: sandbox #!
      securityContext:
        runAsNonRoot: true #!
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
# ...
          securityContext:
            allowPrivilegeEscalation: false #!
            readOnlyRootFilesystem: true #!
            capabilities: { drop: ["ALL"] } #!
```

**Layer 7: RBAC.** The Manager may create and read claims and read pods. That is all.

The highlighted lines, in order:

1. `kind: Role`: a Role, not a ClusterRole: its permissions exist only inside the sandbox namespace.
2. `verbs on sandboxclaims`: on sandbox claims only: create one, read them, and delete them. No update and no access to anything else.
3. `verbs on pods: ["get", "list"]`: on pods: read only. The Manager cannot exec into a pod or change one.

```yaml
# from k8s/40-platform.yaml
# The Manager's whole Kubernetes permission: create and read claims, and read pods. Nothing else.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role #!
metadata: { name: sandbox-manager, namespace: sandboxes }
rules:
  - apiGroups: ["extensions.agents.x-k8s.io"]
    resources: ["sandboxclaims"]
    verbs: ["create", "get", "list", "watch", "delete"] #!
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"] #!
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: sandbox-manager, namespace: sandboxes }
subjects: [{ kind: ServiceAccount, name: sandbox-manager, namespace: sandbox-platform }]
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: Role, name: sandbox-manager }
```

**Layer 8: the pod network.** The namespace denies everything. The template then admits exactly one way out and one way in. EKS Auto Mode ships with network-policy enforcement off, and one setting turns it on.

The highlighted lines, in order:

1. `podSelector: {}`: an empty selector: this policy applies to every pod in the namespace.
2. `policyTypes: [Ingress, Egress]`: it covers traffic in both directions, and lists no allowed traffic, so everything is denied unless another policy admits it.
3. `podSelector: app: sandbox-router`: the only pods allowed to connect to a sandbox are the router's.
4. `port: 8080`: the router may reach the sandboxd file port.
5. `port: 9090`: and the sandboxd command port. No other port is open.
6. `ipBlock: { cidr: ${gateway_ip}/32 }`: the only address a sandbox may connect to is the gateway's fixed address, one host.
7. `port: 3128`: and only on the proxy port.

```yaml
# from k8s/20-sandboxes.yaml
# Namespace-wide default deny. Anything in this namespace that no template policy admits is isolated.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: sandboxes
spec:
  podSelector: {} #!
  policyTypes: [Ingress, Egress] #!
# ...
  networkPolicy:
    ingress:
      - from:
          - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: sandbox-router } }
            podSelector: { matchLabels: { app: sandbox-router } } #!
        ports:
          - { protocol: TCP, port: 8080 }   # sandboxd files #!
          - { protocol: TCP, port: 9090 }   # sandboxd commands #!
    egress:
      # The one way out. No DNS rule: the proxy resolves names, so a sandbox cannot even look one up.
      - to:
          - ipBlock: { cidr: ${gateway_ip}/32 } #!
        ports:
          - { protocol: TCP, port: 3128 } #!
```

The highlighted lines, in order:

1. `"enable-network-policy-controller" = "true"`: switches on the agent that enforces every network policy in the cluster. Without it, the policies above would be accepted and ignored.

```hcl
# from terraform/20-platform/manifests.tf
resource "kubernetes_config_map_v1" "network_policy" {
  metadata {
    name      = "amazon-vpc-cni"
    namespace = "kube-system"
  }
  data = { "enable-network-policy-controller" = "true" } #!
}
```

Every platform service also says who may call it, for example:

The highlighted lines, in order:

1. `podSelector: app: sandbox-manager`: the policy applies to the Manager's pods.
2. `policyTypes: [Ingress]`: once a policy selects a pod for ingress, every connection it does not list is refused.
3. `namespaceSelector: agent-orchestrator`: callers from the orchestrator's namespace are admitted.
4. `podSelector: app: tool-gateway`: and the tool gateway's pods, which check on every call whether a session is active.
5. `port: 8000`: and only on the Manager's port.
6. `namespaceSelector: agent-orchestrator`: the router admits the orchestrator's namespace and nothing else.
7. `port: 8004`: on the router's port.

```yaml
# from k8s/40-platform.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: sandbox-manager-ingress, namespace: sandbox-platform }
spec:
  podSelector: { matchLabels: { app: sandbox-manager } } #!
  policyTypes: [Ingress] #!
  ingress:
    - from:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: agent-orchestrator } } #!
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: sandbox-egress } }
          podSelector: { matchLabels: { app: egress-gateway } }
        - podSelector: { matchLabels: { app: tool-gateway } } #!
      ports: [{ protocol: TCP, port: 8000 }] #!
# ...
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: sandbox-router-ingress, namespace: sandbox-router }
spec:
  podSelector: { matchLabels: { app: sandbox-router } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: agent-orchestrator } } #!
      ports: [{ protocol: TCP, port: 8004 }] #!
```

**Layer 9: the domain ceiling.** Even if a session's allowlist is wrong, the gateway pods cannot reach a name outside this list.

The highlighted lines, in order:

1. `kind: ApplicationNetworkPolicy`: an AWS network-policy type that can name destinations by domain name.
2. `policyTypes: [Egress]`: it limits what the gateway pods may connect to.
3. `- pypi.org`: the first of the three names the gateway may reach.
4. `- files.pythonhosted.org`: the second.
5. `- example.com`: the third.
6. `ports: [{ protocol: TCP, port: 443 }]`: and only on port 443. Even if a session's allowlist is wrong, the gateway cannot reach a name outside this list.

```yaml
# from k8s/30-egress.yaml
apiVersion: networking.k8s.aws/v1alpha1
kind: ApplicationNetworkPolicy #!
metadata: { name: egress-gateway-ceiling, namespace: sandbox-egress }
spec:
  podSelector: { matchLabels: { app: egress-gateway } }
  policyTypes: [Egress] #!
  egress:
    - to:
        - ipBlock: { cidr: 0.0.0.0/0 }
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
    - to:
        - domainNames:
            - pypi.org #!
            - files.pythonhosted.org #!
            - example.com #!
      ports: [{ protocol: TCP, port: 443 }] #!
```

**Layer 12: the gateway's policy.** The allowlist, the rate limit and the byte budget are checked on every tunnel, and the decision is logged.

The highlighted lines, in order:

1. `RATE_PER_MIN = ...120`: the rate limit: 120 tunnels a minute for each session.
2. `BYTE_BUDGET = ...50 MiB`: the byte budget: 50 MiB of responses for each session.
3. `if host == entry or (entry.startswith(...)`: a host is allowed only if it equals an entry in the session's list, or matches a wildcard entry such as a domain with a leading star.
4. `return False`: the default answer is no: a host that matches nothing is refused.
5. `if not s:`: no session owns the source address, so the caller is not a known sandbox and is refused with 403.
6. `if not host_allowed(host, ...)`: the host is not on this session's list: refused with 403 before any connection is opened.
7. `if self.over_rate(sid)`: the session is over its rate limit: refused with 429.
8. `if self._bytes[sid] > BYTE_BUDGET`: the session has used up its byte budget: refused with 429.
9. `mode = "terminate" if host in s["inject"] else "passthrough"`: TLS is ended only for the host that needs a credential. For every other host the encrypted stream passes through untouched.

```python
# from platform/ds01/gateway_addon.py
RATE_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120")) #!
BYTE_BUDGET = int(os.environ.get("SESSION_BYTE_BUDGET", str(50 * 1024 * 1024))) #!
# ...
def host_allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allow:
        if host == entry or (entry.startswith("*.") and host.endswith(entry[1:])): #!
            return True
    return False #!
# ...
    def http_connect(self, flow: http.HTTPFlow):
        ip, host = flow.client_conn.peername[0], flow.request.host.lower()
        s = self.session(ip)
        if not s: #!
            return self._deny(flow, 403, "source is not a known sandbox", None, host)
        sid = s["session_id"]
        if not host_allowed(host, s["allow_hosts"]): #!
            return self._deny(flow, 403, f"{host} is not on this session's allowlist", sid, host)
        if self.over_rate(sid): #!
            return self._deny(flow, 429, "request rate limit for this session", sid, host)
        if self._bytes[sid] > BYTE_BUDGET: #!
            return self._deny(flow, 429, "byte budget for this session is used up", sid, host)
        mode = "terminate" if host in s["inject"] else "passthrough" #!
        audit(COMPONENT, "egress.allow", session_id=sid, tenant=s["tenant"], host=host, port=flow.request.port, tls_mode=mode,
              source=ip)
```

**Layer 13: credentials and tools.** The broker caps the lifetime of every credential. The tool gateway keeps a table of which agent may call which tool and how risky each tool is.

The highlighted lines, in order:

1. `AUDIENCES = {"metrics-api": 900, ...}`: the broker will mint a credential only for a named audience, and for 900 seconds at most.
2. `ttl = min(..., AUDIENCES[aud])`: whatever lifetime a caller asks for is capped at the audience's maximum.

```python
# from platform/ds01/broker.py
AUDIENCES = {"metrics-api": 900, "tool-gateway-backend": 900} #!
# ...
            ttl = min(int(body.get("ttl_seconds", 900)), AUDIENCES[aud]) #!
            token = mint(broker_key, {"aud": aud, "sub": session, "tenant": tenant, "scope": "read"}, ttl)
```

The highlighted lines, in order:

1. `RATE_PER_MIN = 20`: the tool gateway's rate limit: 20 calls a minute for each session.
2. `"ticket.create": {"risk": "high", ...}`: a high-risk tool: it runs only after a person approves, and only the data-analyst agent may call it.
3. `"project.delete": {"risk": "forbidden", "agents": set()}`: an empty set of agents: no agent can ever call it.

```python
# from platform/ds01/tool_gateway.py
RATE_PER_MIN = 20 #!
# ...
TOOLS = {
    "metrics.query": {"risk": "read", "agents": {"data-analyst", "coding"},
                      "description": "Read a metric series from the company metrics API."},
    "ticket.create": {"risk": "high", "agents": {"data-analyst"}, #!
                      "description": "Create a ticket. Needs a human approval each time."},
    "project.delete": {"risk": "forbidden", "agents": set(), #!
                       "description": "Delete a project. Never available to an agent."},
}
```

**Layer 14: audit.** Every service writes through one function, which redacts any field that looks like a secret.

The highlighted lines, in order:

1. `_SECRET_NAME = re.compile`: the pattern for field names that look like secrets: token, secret, password, authorization, api key, credential.
2. `if _SECRET_NAME.search(key) and not key.endswith(_SAFE_SUFFIX):`: any field whose name matches is treated as a secret, unless its name ends in `_id`, `_sha256` or `_len`.
3. `value = "[redacted]"`: and its value is replaced before the line is written, so a credential cannot reach a log by accident.

```python
# from platform/ds01/common.py
_SECRET_NAME = re.compile(r"(token|secret|password|authorization|api[_-]?key|credential)", re.I) #!
_SAFE_SUFFIX = ("_id", "_sha256", "_len")
# ...
def audit(component: str, event: str, **fields) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "component": component,
        "event": event,
    }
    for key, value in fields.items():
        if _SECRET_NAME.search(key) and not key.endswith(_SAFE_SUFFIX): #!
            value = "[redacted]" #!
        rec[key] = value
    print(json.dumps(rec, separators=(",", ":"), default=str), flush=True)
```

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

![The eight steps and the code behind each](diagrams/appendix/a02-12.svg)

**What the picture shows.** For each step of the life of one session: who acts, what happens, and the files that make it happen. The blocks below are copied from those files.

**The code behind each step.** Each block is copied from the file named on its first line, and the build fails if a line of a block is not in that file. `# ...` marks lines left out. The lines that actually enforce a security property are highlighted in yellow and numbered, and the numbered list above each block says what each one does.

**Step 1: the orchestrator asks the Manager for a session.** The orchestrator sends one request with its own bearer token, naming the user, tenant, agent and template. The Manager refuses anything without that token, and checks every field before it creates anything. Files: `platform/ds01/orchestrator.py` and `platform/ds01/manager.py`.

The highlighted lines, in order:

1. `req.add_header("Authorization", f"Bearer {TOKEN}")`: every call the orchestrator makes carries its own token, read from a mounted secret.
2. `body = {"user": ..., "tenant": ..., "agent": ..., "template": ...}`: the request names who the session is for: the user, the tenant, the agent and the kind of sandbox.
3. `call("POST", f"{MANAGER}/v1/sessions", body)`: and goes to the Manager's session endpoint.

```python
# from platform/ds01/orchestrator.py
MANAGER = "http://sandbox-manager.sandbox-platform.svc:8000"
ROUTER = "http://sandbox-router.sandbox-router.svc:8004"
TOOLGW = "http://tool-gateway.sandbox-platform.svc:8003"
# ...
    req.add_header("Authorization", f"Bearer {TOKEN}") #!
# ...
    if cmd == "create":
        # create <tenant> <agent> <template> [ttl_seconds] [host,host,...]
        body = {"user": os.environ.get("ORCH_USER", "user-demo"), "tenant": argv[1], "agent": argv[2], "template": argv[3]} #!
        if len(argv) > 4:
            body["ttl_seconds"] = int(argv[4])
        if len(argv) > 5 and argv[5]:
            body["allow_hosts"] = argv[5].split(",")
        out = call("POST", f"{MANAGER}/v1/sessions", body) #!
```

The highlighted lines, in order:

1. `secrets_equal(self.bearer(), orchestrator_token)`: compares the presented token with the orchestrator's token in constant time. An empty token never matches.
2. `if not self._auth_orchestrator():`: every route except the health check and the gateways' lookups needs the orchestrator's token.
3. `return 401, {"error": "unauthorized"}`: without it the answer is 401 and nothing else happens.
4. `if template not in TEMPLATES:`: only templates the platform team defined can be asked for.
5. `if not NAME_RE.match(tenant) or not NAME_RE.match(agent) or not user:`: tenant and agent must be short lowercase names and a user is required, so a crafted value cannot reach a label or a log line.
6. `if any(not HOST_RE.match(h) for h in extra):`: every extra host must look like a hostname.
7. `if not TEMPLATES[template]["network"] and extra:`: a template with no network device cannot be given hosts to reach.
8. `ttl = min(int(body.get("ttl_seconds", 1800)), MAX_TTL)`: the lifetime a caller asks for is capped at `MAX_TTL`, 2 hours by default.

```python
# from platform/ds01/manager.py
    def _auth_orchestrator(self):
        return secrets_equal(self.bearer(), orchestrator_token) #!
# ...
        if not self._auth_orchestrator(): #!
            return 401, {"error": "unauthorized"} #!
        if path == "/v1/sessions" and method == "POST":
            return self._create(body)
        if path == "/v1/sessions" and method == "GET":
# ...
    def _create(self, body):
        if not isinstance(body, dict):
            return 400, {"error": "json body required"}
        tenant, agent, user = body.get("tenant", ""), body.get("agent", ""), str(body.get("user", ""))[:64]
        template = body.get("template", "")
        if template not in TEMPLATES: #!
            return 400, {"error": f"unknown template; choose one of {sorted(TEMPLATES)}"}
        if not NAME_RE.match(tenant) or not NAME_RE.match(agent) or not user: #!
            return 400, {"error": "tenant and agent must be short lowercase names; user is required"}
        extra = [h.lower() for h in body.get("allow_hosts", [])]
        if any(not HOST_RE.match(h) for h in extra): #!
            return 400, {"error": "allow_hosts must be hostnames"}
        if not TEMPLATES[template]["network"] and extra: #!
            return 400, {"error": "this template has no network device; allow_hosts does not apply"}
        ttl = min(int(body.get("ttl_seconds", 1800)), MAX_TTL) #!
```

**Step 2: the Manager checks the limit and creates the claim.** The template says which warm pool to draw from and which hosts a session may reach by default. The Manager counts the tenant's active sessions, builds the allowlist from the template's defaults and the hosts that were asked for, and creates a SandboxClaim that carries that list and an end time. It can create claims because it alone holds the Role under layer 7. File: `platform/ds01/manager.py`.

The highlighted lines, in order:

1. `"default_allow": [...]`: the default allowlist of a template: what a session may reach without asking for more.
2. `"inject": {"metrics-api.sandbox-platform.svc": "metrics-api"}`: which host gets a credential injected, and for which audience.
3. `"compute-only": {..."network": False, "default_allow": []...}`: the no-network template has an empty allowlist and nothing to inject.

```python
# from platform/ds01/manager.py
TEMPLATES = {
    "python-untrusted": {
        "pool": "python-untrusted-pool",
        "network": True,
        "default_allow": ["pypi.org", "files.pythonhosted.org", "metrics-api.sandbox-platform.svc", "tool-gateway.sandbox-platform.svc"], #!
        "inject": {"metrics-api.sandbox-platform.svc": "metrics-api"}, #!
    },
    "compute-only": {"pool": "compute-only-pool", "network": False, "default_allow": [], "inject": {}}, #!
}
```

The highlighted lines, in order:

1. `if len(active) >= MAX_PER_TENANT:`: a tenant with three active sessions gets no fourth. The answer is 429.
2. `allow = sorted(set(tpl["default_allow"] + extra))`: the session's allowlist is the template's defaults plus the hosts asked for. Nothing else.
3. `"ds01/user-hash": sha256_hex(user)[:12]`: the user is stored as a short hash in a label, so a user name never becomes a searchable label.
4. `"ds01/allow-hosts": json.dumps(allow)`: the allowlist travels with the claim as an annotation. The egress gateway reads it back later through the Manager.
5. `"warmPoolRef": {"name": tpl["pool"]}`: the claim draws from the named warm pool, so the sandbox is already booted.
6. `"lifecycle": {"shutdownTime": shutdown, "shutdownPolicy": "Delete"}`: an end time and a delete policy are stamped on every claim, so a forgotten session still disappears.
7. `k8s.request("POST", CLAIMS, manifest)`: the one Kubernetes call the Manager makes to start a session. The Role allows it, and nothing wider.

```python
# from platform/ds01/manager.py
        active = [s for s in _active_claims() if s["tenant"] == tenant and not s["terminating"]]
        if len(active) >= MAX_PER_TENANT: #!
            audit(self.component, "quota.denied", tenant=tenant, active=len(active), limit=MAX_PER_TENANT)
            return 429, {"error": f"tenant {tenant} already has {len(active)} active sessions (limit {MAX_PER_TENANT})"}
# ...
        sid = "sess-" + secrets.token_hex(4)
        tpl = TEMPLATES[template]
        allow = sorted(set(tpl["default_allow"] + extra)) #!
        shutdown = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {
                "name": sid,
                "namespace": NS,
                "labels": {"ds01/tenant": tenant, "ds01/agent": agent, "ds01/template": template, "ds01/user-hash": sha256_hex(user)[:12]}, #!
                "annotations": {"ds01/allow-hosts": json.dumps(allow), "ds01/user": user}, #!
            },
            "spec": {
                "warmPoolRef": {"name": tpl["pool"]}, #!
                "additionalPodMetadata": {"labels": {"sandbox.users.io/session": sid, "sandbox.users.io/tenant": tenant, "sandbox.users.io/template": template}},
                "lifecycle": {"shutdownTime": shutdown, "shutdownPolicy": "Delete"}, #!
            },
        }
        t0 = time.time()
        code, resp = k8s.request("POST", CLAIMS, manifest) #!
        if code >= 300:
            audit(self.component, "claim.failed", session_id=sid, status=code, detail=str(resp.get("message", ""))[:200])
            return 502, {"error": "could not create the sandbox claim"}
```

**Step 3: a booted pod is adopted, and the token is minted.** The agent-sandbox controller is installed from a release manifest pinned by its SHA-256. It keeps the warm pool filled and hands one booted pod to each claim. The Manager polls the claim until it is Ready, deletes it if that takes too long, and then mints a signed session token whose lifetime equals the session's. Files: `terraform/20-platform/manifests.tf`, `k8s/20-sandboxes.yaml`, `platform/ds01/manager.py` and `platform/ds01/common.py`.

The highlighted lines, in order:

1. `condition = sha256(self.response_body) == var.controller_manifest_sha256`: the controller's release file must match the pinned SHA-256, or Terraform stops. A changed or tampered release is never applied.

```hcl
# from terraform/20-platform/manifests.tf
data "http" "controller" {
  url = "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${var.controller_version}/sandbox-with-extensions.yaml"

  lifecycle {
    postcondition {
      condition     = sha256(self.response_body) == var.controller_manifest_sha256 #!
      error_message = "The agent-sandbox release manifest does not match the pinned SHA-256."
    }
  }
}
```

The highlighted lines, in order:

1. `replicas: ${python_pool_replicas}`: how many booted pods wait. A claim takes one at once, and the controller boots a replacement.
2. `sandboxTemplateRef: name: python-untrusted`: every pod in the pool is made from the python-untrusted template, so each one already has all the hardening.

```yaml
# from k8s/20-sandboxes.yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: python-untrusted-pool
  namespace: sandboxes
spec:
  replicas: ${python_pool_replicas} #!
  sandboxTemplateRef:
    name: python-untrusted #!
```

The highlighted lines, in order:

1. `deadline = t0 + READY_TIMEOUT`: the wait is bounded: 120 seconds by default.
2. `if _ready(claim) and ...get("name"):`: the session is returned only when the claim is Ready and a sandbox is named.
3. `k8s.request("DELETE", ...)`: on a timeout the claim is deleted, so a half-started session is not left running.
4. `token = mint(session_key, {...}, ttl)`: the session token is signed with the session key and carries the session, tenant, agent, user and template.

```python
# from platform/ds01/manager.py
        deadline = t0 + READY_TIMEOUT #!
        claim = resp
        while time.time() < deadline:
            _, claim = k8s.request("GET", f"{CLAIMS}/{sid}")
            if _ready(claim) and (claim.get("status", {}).get("sandbox") or {}).get("name"): #!
                break
            time.sleep(0.3)
        else:
            audit(self.component, "claim.timeout", session_id=sid)
            k8s.request("DELETE", f"{CLAIMS}/{sid}") #!
            return 504, {"error": "sandbox did not become ready in time"}
        ready_ms = int((time.time() - t0) * 1000)
        _invalidate()
# ...
        token = mint( #!
            session_key,
            {"sid": sid, "tenant": tenant, "agent": agent, "user": user, "template": template},
            ttl,
        )
# ...
        return 201, {"session_id": sid, "claim": sid, "template": template, "ready_ms": ready_ms,
                     "allow_hosts": allow, "shutdown_time": shutdown, "session_token": token, "pod_ip": info.get("pod_ip")}
```

The highlighted lines, in order:

1. `body["iat"], body["exp"], body["jti"]`: every token gets an issue time, an expiry and a random id.
2. `hmac.new(key, payload, sha256)`: an HMAC-SHA256 signature over the claims. Without the key nobody can forge or alter a token.
3. `hmac.compare_digest(sig, expected)`: on use, the signature is recomputed and compared in constant time: a bad one is rejected.
4. `claims.get("exp", 0) < time.time()`: an expired token is rejected.

```python
# from platform/ds01/common.py
def mint(key: str, claims: dict, ttl_seconds: int) -> str:
    body = dict(claims)
    now = int(time.time())
    body["iat"], body["exp"], body["jti"] = now, now + int(ttl_seconds), secrets.token_hex(6) #!
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest()) #!
    return f"{payload}.{sig}"
# ...
def verify(key: str, token: str, audience: str | None = None) -> dict:
    """Return the claims, or raise ValueError with a short reason."""
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        raise ValueError("malformed")
    expected = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected): #!
        raise ValueError("bad signature")
    claims = json.loads(_unb64(payload))
    if claims.get("exp", 0) < time.time(): #!
        raise ValueError("expired")
    if audience is not None and claims.get("aud") != audience:
        raise ValueError("wrong audience")
    return claims
```

**Step 4: commands and files go through the Router.** The Router accepts only the orchestrator's token and only a strictly formed session ID. It looks the claim up to find the template and pod, then uses sandboxd for a sandbox with a network and the Kubernetes exec channel for one without. Every command is logged with its exit code and duration. Files: `platform/ds01/router.py`, `k8s/20-sandboxes.yaml` and `k8s/40-platform.yaml`.

The highlighted lines, in order:

1. `if not secrets_equal(self.bearer(), orchestrator_token):`: the Router, too, serves only the orchestrator.
2. `re.fullmatch(r"/v1/sandboxes/(sess-[a-f0-9]+)/(exec|files)", path)`: the path must match the shape of a real session ID exactly, so nothing else reaches the lookup.
3. `backend = "sandboxd" if ... else "kube-exec"`: the backend follows the template: sandboxd with a network, Kubernetes exec without one.
4. `audit(self.component, "exec", ...)`: every command is logged with the session, the backend, the command, its exit code and its duration.
5. `if not path or ".." in path.split("/"):`: a file path must be relative and may not climb out of the workspace.
6. `if len(data) > MAX_FILE:`: uploads are capped at 5 MiB.

```python
# from platform/ds01/router.py
        if not secrets_equal(self.bearer(), orchestrator_token): #!
            return 401, {"error": "unauthorized"}
# ...
        m = re.fullmatch(r"/v1/sandboxes/(sess-[a-f0-9]+)/(exec|files)", path) #!
# ...
    def _exec(self, sid, target, body):
        command, timeout = str(body.get("command", "")), int(body.get("timeout", 60))
        if not command:
            return 400, {"error": "command required"}
        t0 = time.time()
        backend = "sandboxd" if target["template"] == "python-untrusted" else "kube-exec" #!
        if backend == "sandboxd":
            res = sdk.get_sandbox(sid, namespace=NS).commands.run(command, command_timeout=timeout)
            result = {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code, "timed_out": res.timed_out}
        else:
            result = _kube_exec(target["pod"], command, timeout)
        audit(self.component, "exec", session_id=sid, tenant=target["tenant"], backend=backend, command=command[:300], #!
              exit_code=result["exit_code"], duration_ms=int((time.time() - t0) * 1000), stdout_len=len(result["stdout"]))
        return 200, result
# ...
        if not path or ".." in path.split("/"): #!
            return 400, {"error": "a relative path under the workspace is required"}
# ...
            if len(data) > MAX_FILE: #!
                return 413, {"error": "file too large"}
```

The highlighted lines, in order:

1. `--auth-token-sha256=${sandboxd_sha256}`: the manifest holds only a digest of the sandboxd token. The daemon accepts a command only if the caller presents the token itself.
2. `sandbox.amazonaws.com/network-isolated: "true"`: in the compute-only template this boots the microVM with no network device at all, so there is nothing to filter and nothing to misconfigure.

```yaml
# from k8s/20-sandboxes.yaml
          command:
            - /sandboxd-bin/sandboxd
            - --root-dir=/workspace
            - --rest-port=8080
            - --grpc-port=9090
            - --auth-token-sha256=${sandboxd_sha256}   # a digest, not the token #!
# ...
      annotations:
        karpenter.sh/do-not-disrupt: "true"
        sandbox.amazonaws.com/network-isolated: "true" #!
```

The highlighted lines, in order:

1. `resources: ["pods/exec"]`: the Router may exec into a pod only for the no-network tier, and only inside the sandbox namespace, because this is a Role, not a ClusterRole.

```yaml
# from k8s/40-platform.yaml
  # Needed only for Tier 3 sandboxes, which have no network and are reached through the exec channel.
  - apiGroups: [""]
    resources: ["pods/exec"] #!
    verbs: ["create", "get"]   # the websocket exec client upgrades with GET
```

**Step 5: the only way out is the egress gateway.** Inside the sandbox every proxy setting names the gateway's fixed address, and the sandbox's network policy admits nothing else (layer 8). The gateway asks the Manager which session owns the source address, applies that session's rules, and either passes the encrypted stream through or ends TLS for the one internal host and injects a credential from the broker. Files: `k8s/20-sandboxes.yaml`, `k8s/30-egress.yaml`, `platform/ds01/gateway_addon.py` and `platform/ds01/manager.py`.

The highlighted lines, in order:

1. `HTTPS_PROXY = http://${gateway_ip}:3128`: every HTTPS client inside the sandbox is told to use the gateway. Unsetting this does not help, because the network policy is the real barrier.
2. `METRICS_API_KEY = placeholder`: the sandbox holds only the word placeholder. The real credential is added outside, so code in the sandbox cannot leak it.

```yaml
# from k8s/20-sandboxes.yaml
            - { name: HTTP_PROXY, value: "http://${gateway_ip}:3128" }
            - { name: HTTPS_PROXY, value: "http://${gateway_ip}:3128" } #!
            - { name: NO_PROXY, value: "localhost,127.0.0.1" }
# ...
            - { name: METRICS_API_KEY, value: placeholder }   # the real credential is added outside the sandbox #!
```

The highlighted lines, in order:

1. `clusterIP: ${gateway_ip}`: a fixed address for the gateway, so sandboxes need no DNS lookup to find it.
2. `sessionAffinity: ClientIP`: one sandbox always reaches the same gateway replica, so the per-session counters are exact.
3. `--set=ssl_verify_upstream_trusted_ca=/pki/ca.crt`: the gateway verifies the internal API's certificate against the internal CA when it calls it.
4. `-s /app/ds01/gateway_addon.py`: loads the policy: every connection goes through the code in the next block.

```yaml
# from k8s/30-egress.yaml
# A fixed address, because sandboxes have no DNS: the proxy setting inside them names this IP.
apiVersion: v1
kind: Service
metadata: { name: egress-gateway, namespace: sandbox-egress }
spec:
  clusterIP: ${gateway_ip} #!
  # One sandbox always talks to one gateway replica, so the per-session rate and byte counters are exact.
  sessionAffinity: ClientIP #!
  sessionAffinityConfig: { clientIP: { timeoutSeconds: 3600 } }
  selector: { app: egress-gateway }
  ports: [{ name: proxy, port: 3128, targetPort: 3128 }]
# ...
          command:
            - mitmdump
            - --listen-host=0.0.0.0
            - --listen-port=3128
            - --set=confdir=/confdir
            - --set=block_global=false
            - --set=ssl_verify_upstream_trusted_ca=/pki/ca.crt #!
            - --set=flow_detail=0
            - --set=termlog_verbosity=warn
            - -s
            - /app/ds01/gateway_addon.py #!
```

The highlighted lines, in order:

1. `GET {MANAGER}/internal/by-ip/{ip}`: the gateway asks the Manager which session owns the source address. It presents its own token, and the answer comes from the cluster, not from the sandbox.
2. `found = info if status == 200 else None`: no answer means no session, and the request is refused later.
3. `if not s or host not in s["inject"]:`: TLS is ended only for a host that is in the session's inject list.
4. `data.ignore_connection = True`: for any other host the encrypted stream is passed through untouched. The gateway cannot read it.
5. `audience = s["inject"].get(host)`: looks up which credential audience this host needs, if any.
6. `headers["Authorization"] = f"Bearer {cred}"`: replaces whatever the sandbox sent with the credential the broker minted for this session.

```python
# from platform/ds01/gateway_addon.py
    def session(self, ip: str):
        now = time.time()
        with self._lock:
            cached = self._sessions.get(ip)
            if cached and cached[0] > now:
                return cached[1]
        status, info = http_json("GET", f"{MANAGER}/internal/by-ip/{ip}", headers={"X-Internal-Token": TOKEN}, timeout=5) #!
        found = info if status == 200 else None #!
        with self._lock:
            self._sessions[ip] = (now + (5 if found else 2), found)
        return found
# ...
    def tls_clienthello(self, data: tls.ClientHelloData):
        addr = data.context.server.address
        host = (addr[0] if addr else data.client_hello.sni or "").lower()
        s = self.session(data.context.client.peername[0])
        if not s or host not in s["inject"]: #!
            data.ignore_connection = True  # pass the encrypted stream through untouched #!
# ...
        injected = None
        audience = s["inject"].get(host) #!
        if audience:
            cred = self.credential(s, audience)
            if not cred:
                return self._deny(flow, 502, "credential broker unavailable", sid, host)
            flow.request.headers["Authorization"] = f"Bearer {cred}" #!
            injected = hashlib.sha256(cred.encode()).hexdigest()[:10]
```

The highlighted lines, in order:

1. `if s["pod_ip"] == m.group(1):`: the address is matched against the pod addresses of active claims, which the cluster reports. A sandbox cannot claim to be someone else.
2. `"no session owns this address"`: an address that belongs to no active session gets a 404, and the gateway refuses it.

```python
# from platform/ds01/manager.py
        m = re.fullmatch(r"/internal/by-ip/([0-9.]+)", path)
        if m:
            for s in _active_claims():
                if s["pod_ip"] == m.group(1): #!
                    return 200, s
            return 404, {"error": "no session owns this address"} #!
```

![Every network path out of a sandbox](diagrams/appendix/a02-3.svg)

**What the picture shows.** The one allowed route out, and the seven attempts that the tests make to find another. Each attempt is labelled with the scenario that tries it.

![How a request gets a credential without the sandbox holding it](diagrams/appendix/a02-4.svg)

**What the picture shows.** The five steps that let a sandbox holding only the word `placeholder` call a protected API. The credential is created, used and discarded outside the sandbox.

**Step 6: tools go through the tool gateway.** The orchestrator, or the egress gateway on behalf of a sandbox, calls the tool gateway with the session token. The gateway verifies the token, asks the Manager whether the session is still active, applies the rate limit, then applies the per-agent tool policy and the approval rule. Files: `platform/ds01/tool_gateway.py` and `platform/ds01/manager.py`.

The highlighted lines, in order:

1. `verify(session_key, self.bearer())`: the token's signature and expiry are checked first.
2. `GET {MANAGER}/internal/sessions/{sid}`: then the Manager is asked whether the session is still active, on every call. A token alone is not enough.
3. `audit(self.component, "auth.revoked"`: a session that has ended is logged as revoked and refused with 401.
4. `if not self._within_rate(claims["sid"]):`: 20 calls a minute for each session. More are refused with 429.

```python
# from platform/ds01/tool_gateway.py
        try:
            claims = verify(session_key, self.bearer()) #!
        except ValueError as exc:
            audit(self.component, "auth.rejected", reason=str(exc), peer=self.client_address[0])
            return 401, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"session token rejected: {exc}"}}
        status, info = http_json("GET", f"{MANAGER}/internal/sessions/{claims['sid']}", headers={"X-Internal-Token": internal_token}) #!
        if status != 200:
            audit(self.component, "auth.revoked", session_id=claims["sid"]) #!
            return 401, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "session is no longer active"}}
        if not self._within_rate(claims["sid"]): #!
            audit(self.component, "rate.limited", session_id=claims["sid"], limit_per_min=RATE_PER_MIN)
            return 429, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32029, "message": "rate limit exceeded"}}
        return 200, self._rpc(claims, body)
```

The highlighted lines, in order:

1. `if not tool or agent not in tool["agents"]:`: an agent can call only the tools its policy lists. Anything else is refused and logged.
2. `if tool["risk"] == "high":`: a high-risk tool does not run on the first call.
3. `if not approval or approval["session"] != sid or approval["key"] != key:`: unless an approval exists that belongs to this session and these exact arguments, a new pending approval is created and the call stops.
4. `approval["state"] = "used"`: one approval, one call: it cannot be reused.

```python
# from platform/ds01/tool_gateway.py
        name, args = params.get("name"), params.get("arguments") or {}
        tool = TOOLS.get(name)
        if not tool or agent not in tool["agents"]: #!
            audit(self.component, "tool.denied", session_id=sid, agent=agent, tool=name, reason="not permitted for this agent")
            return err(-32003, f"tool {name!r} is not available to agent {agent!r}")
        if tool["risk"] == "high": #!
            approval_id = params.get("approval_id")
            key = _args_hash(name, args)
            with _lock:
                approval = _approvals.get(approval_id)
            if not approval or approval["session"] != sid or approval["key"] != key: #!
                approval_id = "apr-" + secrets.token_hex(4)
                with _lock:
                    _approvals[approval_id] = {"session": sid, "key": key, "tool": name, "state": "pending"}
                audit(self.component, "approval.requested", session_id=sid, tool=name, approval_id=approval_id)
                return err(-32001, "this tool needs a human approval", {"approval_id": approval_id, "state": "pending"})
            if approval["state"] != "approved":
                return err(-32001, "approval still pending", {"approval_id": approval_id, "state": approval["state"]})
            with _lock:
                approval["state"] = "used"  # one approval, one call #!
        result = self._run(name, args, claims)
```

The highlighted lines, in order:

1. `"audience": "metrics-api", "ttl_seconds": 60`: the tool gateway asks the broker for a credential that lasts 60 seconds, for this session and this audience.
2. `cafile="/pki/ca.crt"`: and calls the API over TLS, verifying its certificate with the internal CA.

```python
# from platform/ds01/tool_gateway.py
        if name == "metrics.query":
            status, cred = http_json("POST", f"{BROKER}/mint",
                                     {"session_id": claims["sid"], "tenant": claims["tenant"], "audience": "metrics-api", "ttl_seconds": 60}, #!
                                     headers={"X-Internal-Token": internal_token})
            if status != 200:
                return {"error": "credential broker refused"}
            status, data = http_json("GET", f"{METRICS}/v1/metrics?name={args.get('name', 'orders_per_minute')}",
                                     headers={"Authorization": f"Bearer {cred['credential']}"}, cafile="/pki/ca.crt") #!
            return data if status == 200 else {"error": f"metrics api answered {status}"}
```

The highlighted lines, in order:

1. `and not s["terminating"]`: a session that is being deleted counts as gone, so its token stops working at once.

```python
# from platform/ds01/manager.py
        m = re.fullmatch(r"/internal/sessions/([a-z0-9-]+)", path)
        if m:
            found = [s for s in _active_claims() if s["session_id"] == m.group(1) and not s["terminating"]] #!
            return (200, found[0]) if found else (404, {"error": "no active session"})
```

**Step 7: every component writes JSON lines with the session ID.** Each service writes through the audit function shown under layer 14. Every Deployment carries a component label, and one script reads the logs of each component by that label, keeps the lines that mention the session and sorts them by time. Files: `platform/ds01/common.py`, `scripts/audit-query.py` and `k8s/40-platform.yaml`.

The highlighted lines, in order:

1. `ds01/component: sandbox-manager`: the label the audit script selects by. Every platform Deployment has one.

```yaml
# from k8s/40-platform.yaml
      labels: { app: sandbox-manager, ds01/component: sandbox-manager } #!
```

The highlighted lines, in order:

1. `-l ds01/component={component}`: reads the logs of each component by its label.
2. `if session_id not in line:`: keeps only the lines that carry this session's ID.
3. `sorted(records, key=lambda r: r["ts"])`: sorts by the components' own timestamps. Nothing is added or edited, so the timeline is what the components wrote.

```python
# from scripts/audit-query.py
COMPONENTS = [
    ("sandbox-platform", "sandbox-manager"),
    ("sandbox-router", "sandbox-router"),
    ("sandbox-egress", "egress-gateway"),
    ("sandbox-egress", "credential-broker"),
    ("sandbox-platform", "tool-gateway"),
    ("sandbox-platform", "metrics-api"),
]
# ...
def collect(session_id):
    records = []
    for namespace, component in COMPONENTS:
        proc = subprocess.run(
            [KUBECTL, "-n", namespace, "logs", "-l", f"ds01/component={component}", "--tail=-1", "--prefix=false", "--ignore-errors"], #!
            capture_output=True, text=True, env=ENV)
        for line in proc.stdout.splitlines():
            if session_id not in line: #!
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return sorted(records, key=lambda r: r["ts"]) #!
```

**Step 8: the session ends.** The orchestrator can delete the session, or the end time stamped on the claim passes. Either way the claim goes, and the controller removes the pod and its workspace with it. From that moment the Manager no longer finds the session, so the tool gateway refuses its token even if it has not expired. Files: `platform/ds01/manager.py` and `k8s/20-sandboxes.yaml`.

The highlighted lines, in order:

1. `k8s.request("DELETE", f"{CLAIMS}/{sid}")`: deleting the claim is the whole action. The controller removes the sandbox and its workspace, because the claim owns them.
2. `audit(self.component, "session.deleted"`: the deletion is logged with the session ID.

```python
# from platform/ds01/manager.py
    def _delete(self, sid):
        code, _ = k8s.request("DELETE", f"{CLAIMS}/{sid}") #!
        _invalidate()
        audit(self.component, "session.deleted", session_id=sid, status=code) #!
        return (200, {"deleted": sid}) if code < 300 else (404, {"error": "no such session"})
```

The highlighted lines, in order:

1. `karpenter.sh/do-not-disrupt: "true"`: while the sandbox exists, its node is never consolidated away, so a live session is not killed to save money.

```yaml
# from k8s/20-sandboxes.yaml
      annotations:
        karpenter.sh/do-not-disrupt: "true" #!
```

![The life of a SandboxClaim: pool, claim, pod, token and what the gateways see](diagrams/appendix/a02-13.svg)

**What the picture shows.** What happens to the warm pool, the claim, the pod and the token from before the session to after it, and what the two gateways see at each stage.


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
| **GPU sandboxes** | The design lists this as an open question. AWS documents that Nitro Sandboxes do not support GPU or other device passthrough ([considerations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)), so a GPU workload would need whole nodes without a sandbox runtime. A CPU sandbox proves the same isolation, network and credential design. An alternative is written down in section 10.9. |
| **gVisor (Tier 1) and Kata (Tier 2 alternative)** | EKS Auto Mode does not allow the shim gVisor needs, and Kata needs self-managed nodes with nested virtualization. Tiers 2 and 3 on Nitro Sandboxes are the design's default and are fully built. An alternative is written down in section 10.8. |
| **Confidential mode** | The design says agent sandboxes need isolated mode. Confidential mode is for regulated data and removes `exec`, which this design depends on. An alternative is written down in section 10.10. |
| **A persistent workspace volume, pause and resume, checkpoints** | The workspace is a size-limited scratch volume that lives and dies with the pod. The design's EBS-backed workspace and its pause and resume are not built. An alternative is written down in section 10.7. |
| **Snapshots and multi-cluster placement** | Open questions in the design itself. An alternative is written down in section 10.12. |
| **A separate certificate authority per sandbox** | One gateway CA is trusted by every sandbox. The design describes one per sandbox, discarded with it. An alternative is written down in section 10.5. |
| **A shared store for the tool gateway** | Approvals and rate counters are in memory, so the tool gateway runs as one replica. A production build keeps them in a shared store such as DynamoDB. An alternative is written down in section 10.1. |
| **An append-only audit store** | Audit lines go to each pod's log and are joined by a script. The design calls for an append-only store with its own retention. The EKS control-plane audit log does go to CloudWatch. An alternative is written down in section 10.2. |
| **A runtime-security agent on the nodes** | The design itself notes it sees nothing inside a microVM. The audit trail is built from the boundaries instead. An alternative is written down in section 10.3. |
| **Image signing and a private registry mirror** | The sandbox images are public images pulled through the NAT gateway. The design wants them pinned, signed and mirrored. An alternative is written down in section 10.4. |
| **A warm-pool autoscaler** | The pool sizes are fixed (2 and 1). An alternative is written down in section 10.6. |

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
### 10. Alternative implementations of what I did not build

**Scenario.** Section 6 lists what this implementation leaves out. Here each item is written down as an alternative: real files under `alternatives/`, each with a label for how far it was checked. None of it is deployed. Nothing in `platform/`, `k8s/`, `terraform/` or `tests/` changes, so the 12 scenarios and the 75 checks above are not affected.

**What the labels mean.** I use three, and I do not use a stronger word than the check supports.

| Label | Meaning |
|---|---|
| **Tested** | The logic ran in a test on my machine, with a positive control where a refusal is being tested. |
| **Checked** | The files passed a syntax or schema check (`terraform validate`, the Kubernetes schemas, or the CRD schemas of the pinned agent-sandbox release). Nothing was applied to an account or cluster. |
| **From documentation** | I wrote it from the vendor's documentation and never ran it. The page says which document. |


None of this ran on EKS. `alternatives/check-all.sh` runs every check that works on one machine with Docker, and it finished with no failure on the day of writing.

![The alternatives, each next to the part of the deployed design it extends](diagrams/appendix/a02-15.svg)

**What the picture shows.** The left column is the design as deployed, with the limit that section 6 names for each part. The right column is the alternative that addresses that limit, with its label. The arrows are not traffic. They show which deployed part each alternative would extend or replace.

| Section 6 item | Alternative | Label |
|---|---|---|
| A shared store for the tool gateway | 10.1 | Tested |
| An append-only audit store | 10.2 | Checked |
| A runtime-security agent on the nodes | 10.3 | Checked |
| Image signing and a private registry mirror | 10.4 | Tested (signing), checked (the rest) |
| A separate certificate authority per sandbox | 10.5 | Tested |
| A warm-pool autoscaler | 10.6 | Tested (the sizing rule), checked (the rest) |
| A persistent workspace, pause, resume, checkpoints | 10.7 | Tested against a fake cluster, checked against the schemas |
| gVisor and Kata | 10.8 | Checked (templates), from documentation (nodes) |
| GPU sandboxes | 10.9 | From documentation |
| Confidential mode | 10.10 | From documentation |
| Network gaps found while writing the defence-in-depth section | 10.11 | Checked |
| Snapshots and multi-cluster placement | 10.12 | A decision note, no files |


Where a patch to the deployed code is needed, the folder holds a `.patch` file that I applied to a copy of `platform/` and compiled. I did not apply any of them to the real files.

**10.1 A shared store for the tool gateway.**

**The gap.** The tool gateway keeps approvals and rate counters in the memory of one process. With two replicas, an approval could be used once on each, and each replica would count only its own calls. So it runs as one replica.

**What I built.** A small store class on DynamoDB, a patch that makes the tool gateway use it, the Terraform for the table and for a role that can only read and write that table, and a Deployment patch that sets two replicas. The two properties that matter are enforced by DynamoDB, not by Python. Consuming an approval is one conditional write, so only one caller can win. Counting a call is one atomic increment.

The highlighted lines, in order:

1. `ConditionExpression="attribute_not_exists(pk)"`: creating an approval cannot overwrite an existing one, even if two replicas picked the same random id.
2. `UpdateExpression="SET #s = :used"`: marks the approval used. This is the write that spends it.
3. `ConditionExpression="#s = :approved AND #sess = :session AND #k = :key"`: the write succeeds only if the approval is approved, belongs to this session, and was approved for these exact arguments. DynamoDB checks and writes in one step, so two replicas racing for one approval cannot both succeed.
4. `UpdateExpression="ADD n :one SET expires_at = :exp"`: adds one to this session's counter for the current minute. The add is atomic, so three replicas share one count.
5. `return int(out["Attributes"]["n"]) <= limit`: the decision uses the number DynamoDB returned from that same write, so there is no read-then-write gap.

```python
# from alternatives/tool-gateway-dynamodb/store.py
    def request_approval(self, session: str, tool: str, key: str, now: float | None = None) -> str:
        """Create a pending approval for this session, tool and argument hash. Returns its id."""
        now = now or time.time()
        approval_id = "apr-" + secrets.token_hex(4)
        self._table.put_item(
            Item={"pk": f"APPROVAL#{approval_id}", "sk": "A", "session": session, "key": key, "tool": tool,
                  "state": "pending", "expires_at": int(now) + APPROVAL_TTL_SECONDS},
            ConditionExpression="attribute_not_exists(pk)", #!
# ...
    def consume(self, approval_id: str, session: str, key: str) -> bool:
        """Use an approval. True for exactly one caller, however many replicas race for it."""
        try:
            self._table.update_item(
                Key={"pk": f"APPROVAL#{approval_id}", "sk": "A"},
                UpdateExpression="SET #s = :used", #!
                ConditionExpression="#s = :approved AND #sess = :session AND #k = :key", #!
                ExpressionAttributeNames={"#s": "state", "#sess": "session", "#k": "key"},
                ExpressionAttributeValues={":used": "used", ":approved": "approved", ":session": session, ":key": key},
            )
            return True
# ...
    def within_rate(self, session: str, limit: int, now: float | None = None) -> bool:
        """Count one call in the current one-minute window. False once the window already holds `limit` calls."""
        now = now or time.time()
        window = int(now // 60)
        out = self._table.update_item(
            Key={"pk": f"RATE#{session}", "sk": f"W#{window}"},
            UpdateExpression="ADD n :one SET expires_at = :exp", #!
            ExpressionAttributeValues={":one": 1, ":exp": (window + 2) * 60},
            ReturnValues="UPDATED_NEW",
        )
        return int(out["Attributes"]["n"]) <= limit #!
```

**How far it was checked.** Tested. `check.sh` starts DynamoDB Local in Docker and runs seven tests. Two of them are the point: 25 threads, each with its own client, race to consume one approval and exactly one wins; and three clients share one rate counter, so 20 of 25 calls pass and 5 are refused. The Terraform passes `terraform validate`. The patch applies to a copy of `platform/` and the result compiles.

**What it does not show.** I ran it against DynamoDB Local, not against the service. The table's expiry sweep (the `expires_at` attribute) is configured in Terraform and was not observed. The egress gateway has the same weakness in its rate counters, which section 4 handles by pinning each sandbox to one replica. I did not apply the store to it.

**10.2 An append-only audit store, and 10.3 watching the boundary.**

**The gap.** Audit lines go to each pod's log and are joined by a script (scenario S11). Anyone who can delete a pod log can change the record. And a runtime-security agent on the nodes would see nothing inside a microVM, so the design builds the trail from the boundaries instead. The deployed build does that from the platform's own lines. It does not record the network boundary itself.

**What I built.** Two Terraform folders and one picture. `audit-store/` is the bucket that nobody can edit, and the path into it. `boundary-monitoring/` adds two more sources of lines: every connection at the sandbox subnets (VPC Flow Logs) and every DNS question in the VPC (Route 53 Resolver query logs). Both feed the same bucket.

![Three sources go through CloudWatch Logs and Firehose into one S3 bucket with Object Lock](diagrams/appendix/a02-16.svg)

**What the picture shows.** Platform pod logs, flow logs and DNS query logs each land in a CloudWatch log group. A subscription filter sends each to one Kinesis Data Firehose stream, which writes batches to an S3 bucket under a date prefix. The platform filter passes only lines that contain a session ID. The other two pass everything. The bucket is the part that matters.

The highlighted lines, in order:

1. `object_lock_enabled = true`: turns Object Lock on for the bucket. It needs the versioning below.
2. `status = "Enabled"`: Object Lock needs versioning. A new write becomes a new version, and the old version stays.
3. `mode = "COMPLIANCE"`: in compliance mode, no user, including the account root user, can delete or overwrite a version before its retention ends ([S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)). Governance mode would let a privileged user lift it.
4. `days = var.retention_days`: how long. The default is 365 days. A longer period costs storage and cannot be shortened for versions already written.
5. `kms_master_key_id = aws_kms_key.audit.arn`: objects are encrypted with a key of the account's own, with rotation on.

```hcl
# from alternatives/audit-store/terraform/main.tf
resource "aws_s3_bucket" "audit" {
  bucket              = "${var.project}-audit-${var.account_id}"
  object_lock_enabled = true #!
# ...
resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled" #!
# ...
resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE" #!
      days = var.retention_days #!
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
# ...
      kms_master_key_id = aws_kms_key.audit.arn #!
```

The highlighted lines, in order:

1. `for_each = toset(var.sandbox_subnet_ids)`: one flow log per sandbox subnet, so every sandbox node is covered.
2. `traffic_type = "ALL"`: accepted and rejected traffic both. A rejected connection is the interesting one.
3. `max_aggregation_interval = 60`: records are grouped over one minute, the shortest choice, so a burst is visible while it still matters.
4. `resource_id = var.vpc_id`: the query log covers the whole VPC. It cannot be limited to the sandbox subnets.

```hcl
# from alternatives/boundary-monitoring/terraform/main.tf
resource "aws_flow_log" "sandbox" {
  for_each                 = toset(var.sandbox_subnet_ids) #!
  subnet_id                = each.value
  traffic_type             = "ALL" #!
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow.arn
  iam_role_arn             = aws_iam_role.flow.arn
  max_aggregation_interval = 60 #!
# ...
resource "aws_route53_resolver_query_log_config_association" "dns" {
  resolver_query_log_config_id = aws_route53_resolver_query_log_config.dns.id
  resource_id                  = var.vpc_id #!
```

**How far it was checked.** Checked. Both folders pass `terraform validate`. I did not apply them, so no line has travelled this path, and I did not see the Object Lock refuse a delete.

**What is missing.** Three things, stated plainly. The deployed design has no agent that ships pod output to CloudWatch, and the platform filter assumes one writes to the log group named in the variable. I did not build that shipper. Second, the flow logs record what reaches the VPC's network. I expect a connection that a network policy drops on the node not to appear there, but I did not test it. Third, the DNS log covers the VPC, so it also carries names asked by the platform's own pods. Neither source sees inside the microVM, which is the limit the design itself states.

**10.4 Image signing and a private mirror.**

**The gap.** The sandbox images are public images, pulled by tag through the NAT gateway. A tag can be moved to different contents. The design wants images pinned, signed and mirrored.

**What I built.** An ECR mirror of the two public registries the platform pulls from, with tags that cannot be moved. A KMS key that signs images, so the private half never leaves KMS. An admission rule that lets a pod in only if every image comes from the mirror and carries a signature made with that key.

![From public registries through the mirror and the signing key to the admission rule](diagrams/appendix/a02-18.svg)

**What the picture shows.** Images are pulled into the ECR mirror, signed once with the KMS key, and checked at admission by Kyverno against the matching public key. Kyverno also rewrites each image reference to its digest, so what was signed is exactly what runs. An unsigned image, an image signed with another key and an image from a public registry are all refused at admission.

The highlighted lines, in order:

1. `image_tag_mutability = "IMMUTABLE"`: a tag in a mirrored repository cannot be pushed again with different contents.
2. `key_usage = "SIGN_VERIFY"`: this key can sign and verify and cannot encrypt, so it has one job.
3. `customer_master_key_spec = "ECC_NIST_P256"`: the key type that cosign signs with through KMS.

```hcl
# from alternatives/image-trust/terraform/main.tf
resource "aws_ecr_repository_creation_template" "mirror" {
  for_each             = toset(["ecr-public", "k8s"])
  prefix               = each.value
  applied_for          = ["PULL_THROUGH_CACHE"]
  image_tag_mutability = "IMMUTABLE" #!
# ...
resource "aws_kms_key" "image_signing" {
  description              = "${var.project} image signing"
  key_usage                = "SIGN_VERIFY" #!
  customer_master_key_spec = "ECC_NIST_P256" #!
```

The highlighted lines, in order:

1. `failurePolicy: Fail`: if Kyverno cannot be reached, pods are refused rather than admitted unchecked.
2. `mutateDigest: true`: the image reference is rewritten to its digest, so the pod runs the bytes that were verified and not whatever the tag points to later.
3. `required: true`: an image with no signature is refused.
4. `publicKeys:`: the signature must verify against this key, the public half of the KMS key. A signature from any other key is refused.
5. `ignoreTlog: true`: no public transparency log is consulted. The key is the whole trust anchor, so who may use the KMS key matters.
6. `failureAction: Enforce`: in the second rule: it blocks, it does not just warn.
7. `image: "111122223333..."`: every container image must come from the account's own registry. This catches an image that was never put through the mirror.

```yaml
# from alternatives/image-trust/kyverno-policy.yaml
    failurePolicy: Fail #!
# ...
      verifyImages:
        - imageReferences:
            - "111122223333.dkr.ecr.us-east-1.amazonaws.com/*"
          failureAction: Enforce
          mutateDigest: true #!
          verifyDigest: true
          required: true #!
# ...
                    publicKeys: |- #!
# ...
                      ignoreTlog: true #!
# ...
      validate:
        failureAction: Enforce #!
# ...
              - image: "111122223333.dkr.ecr.us-east-1.amazonaws.com/*" #!
```

**How far it was checked.** The signing is tested; the rest is checked. `check.sh` starts a local registry and signs an image with cosign using a throwaway key. It verifies that the signed image passes, the same image fails against a second key, and an unsigned image fails. The two refusals only count if the first check passes, and the script is written that way. The Terraform passes `terraform validate`. The Kyverno policy parses, and its field names follow [Kyverno's documentation](https://kyverno.io/docs/policy-types/cluster-policy/verify-images/), but **I did not run it on a cluster**. The mirror follows the [ECR pull-through cache documentation](https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache.html), and I did not apply it.

**Not tested.** Signing used a local key pair, not the KMS key, so cosign's KMS path was not exercised. Signing the images that the mirror pulls is a step the build job has to do, and no job is written. The policy hard-codes a placeholder account and region, and the public key must be filled in.

**10.5 A separate certificate authority per sandbox.**

**The gap.** The gateway ends TLS for the one internal host that needs a credential, so every sandbox must trust a certificate authority that the gateway controls. In the tested build that is one authority shared by all sandboxes. If its key leaked, it could be used to impersonate any site to any sandbox.

**What I built.** A small module that creates one authority per session, limited so that a leaked key is of little use.

The highlighted lines, in order:

1. `ec.generate_private_key(ec.SECP256R1())`: a new key for every session. Nothing is shared between sessions.
2. `.not_valid_after(now + valid_for)`: the authority expires with the session. The default is two hours.
3. `BasicConstraints(ca=True, path_length=0)`: it is an authority that cannot create more authorities.
4. `NameConstraints(permitted_subtrees=[x509.DNSName(host)])`: marked critical. A client that honours it accepts a certificate from this authority only for this one host name.

```python
# from alternatives/session-ca/session_ca.py
    key = ec.generate_private_key(ec.SECP256R1()) #!
# ...
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + valid_for) #!
# ...
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True) #!
# ...
        .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(host)], excluded_subtrees=None), critical=True) #!
```

**How far it was checked.** Tested, with the `cryptography` library and with the OpenSSL command line as a second opinion. Four tests: a certificate for the permitted host verifies; a certificate for any other host is refused even though it was signed with the authority's own key, which is the stolen-key case; another session's authority does not verify the certificate; and the authority certificate carries the limits above. Each refusal has a positive control, so a refusal cannot come from a broken test.

**Not tested.** Whether the clients inside real sandboxes enforce name constraints. OpenSSL and the `cryptography` verifier do. A client that ignores a critical extension would accept the certificate, so this has to be checked for each runtime in the sandbox image. The gateway code that would call this module is not written.

**10.6 A warm-pool autoscaler.**

**The gap.** The pool sizes are fixed at 2 and 1. A claim takes one booted pod at once, and the pool boots a replacement. Claims that arrive while the replacement boots get a cold start, which scenario S04 measured at about 10 seconds with room on a node and 60 seconds when a node had to be launched.

**What I built.** A sizing rule, a Manager patch that counts claims and reports a desired size, and KEDA objects that scale the pool to that size. A SandboxWarmPool has a scale subresource, and KEDA can scale any resource that has one ([KEDA](https://keda.sh/docs/latest/concepts/scaling-deployments/)).

The highlighted lines, in order:

1. `rate = claims_in_window / window_seconds`: claims per second over the recent window.
2. `max(floor, min(ceiling, math.ceil(rate * boot_seconds) + buffer))`: the pool must hold the claims that arrive during one boot of a replacement, plus a buffer, and stay between a floor and a ceiling.

```python
# from alternatives/warm-pool-autoscaler/pool_sizing.py
    rate = claims_in_window / window_seconds #!
    return max(floor, min(ceiling, math.ceil(rate * boot_seconds) + buffer)) #!
```

The highlighted lines, in order:

1. `kind: SandboxWarmPool`: KEDA's target is the pool itself, through its scale subresource.
2. `minReplicaCount: 2`: never below today's fixed size.
3. `maxReplicaCount: 20`: a ceiling, because every pooled pod costs a microVM that is idle.
4. `pollingInterval: 15`: KEDA asks the Manager every 15 seconds. How fast the pool shrinks is left to KEDA's defaults, which I did not tune.
5. `valueLocation: python-untrusted.desired`: KEDA reads the Manager's answer for this template.
6. `keyParamName: X-Internal-Token`: KEDA authenticates with the same internal token header that the gateways use, with a token of its own.
7. `resources: ["sandboxwarmpools/scale"]`: KEDA's service account may change the scale of pools and nothing else about them.

```yaml
# from alternatives/warm-pool-autoscaler/keda.yaml
  scaleTargetRef:
    apiVersion: extensions.agents.x-k8s.io/v1beta1
    kind: SandboxWarmPool #!
    name: python-untrusted-pool
# ...
  minReplicaCount: 2 #!
  maxReplicaCount: 20 #!
  pollingInterval: 15 #!
# ...
    - type: metrics-api
      metricType: AverageValue
      metadata:
        url: http://sandbox-manager.sandbox-platform.svc:8000/internal/pool-demand
        valueLocation: python-untrusted.desired #!
        targetValue: "1"
        authMode: apiKey
        method: header
        keyParamName: X-Internal-Token #!
# ...
    resources: ["sandboxwarmpools/scale"] #!
```

**How far it was checked.** The sizing rule is tested (six tests: the floor, the ceiling, the buffer, rounding up and bad input). The Manager patch applies to a copy of `platform/` and compiles. The Kubernetes kinds in `keda.yaml` pass the Kubernetes schemas. KEDA's own kinds were not schema-checked and **KEDA was never run**.

**Open points.** The label I used to let KEDA's operator reach the Manager (`app.kubernetes.io/name: keda-operator`) is what I expect from KEDA's chart, and I did not check it. The boot time that the rule assumes (60 seconds by default) is the cold node case from S04. A pool that is sized on claims from the last five minutes reacts after the burst starts, not before it.

**10.7 A persistent workspace, pause, resume and checkpoints.**

**The gap.** The workspace is a scratch volume that lives and dies with the pod. The design asks for a disk that survives, a way to pause a session and bring it back, and checkpoints.

**What AWS and agent-sandbox allow.** Three facts shape the design, and each has a source. A Nitro Sandbox can mount an EBS volume, one pod at a time, and it cannot be snapshotted, restored or moved while it runs ([AWS](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)). The agent-sandbox `Sandbox` has an `operatingMode`. `Suspended` deletes the pod and keeps the volume, and `Running` creates a new pod on the same volume. And a `SandboxTemplate` can declare `volumeClaimTemplates`, which gives every sandbox its own volume ([agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/volumes/volume-claim-template/)). So "pause" here means: keep the disk, drop the machine. The memory of a running process cannot be saved on Nitro Sandboxes.

![Running, suspended, running again, with a checkpoint and what ends the disk](diagrams/appendix/a02-17.svg)

**What the picture shows.** Suspending deletes the pod and leaves the volume. Resuming creates a new pod that mounts the same volume, so the files are back and the processes start again from them. A checkpoint is a snapshot of the volume. Deleting the claim deletes the volume with it, so a persistent session cannot use the delete-at-end-time policy that the deployed Manager sets.

The highlighted lines, in order:

1. `{"spec": {"operatingMode": mode}}, content_type="application/merge-patch+json"`: a merge patch that changes one field, Suspended or Running, and nothing else about the sandbox.
2. `persistentVolumeClaimName": f"{VOLUME_NAME}-{name}"`: the snapshot is of the volume that belongs to this session's sandbox, named by the pattern the controller uses.

```python
# from alternatives/persistent-workspace/workspace.py
def _set_mode(k8s, name, mode):
    code, resp = k8s.request("PATCH", f"{SANDBOXES}/{name}", {"spec": {"operatingMode": mode}}, content_type="application/merge-patch+json") #!
    if code >= 300:
        raise RuntimeError(f"could not set operatingMode {mode}: {code} {resp.get('message', '')}")
# ...
def checkpoint(k8s, session_id, label, snapshot_class="ds01-workspace"):
    """Snapshot the workspace volume. Returns the snapshot name."""
    name = sandbox_name(k8s, session_id)
    snap = f"{session_id}-{label}"
    manifest = {
        "apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
        "metadata": {"name": snap, "namespace": NS, "labels": {"ds01/session": session_id}},
        "spec": {"volumeSnapshotClassName": snapshot_class, "source": {"persistentVolumeClaimName": f"{VOLUME_NAME}-{name}"}}, #!
```

The highlighted lines, in order:

1. `name: python-persistent`: generated from the real python-untrusted template. Only the name, one label, the workspace volume and the pool differ.
2. `accessModes: ["ReadWriteOncePod"]`: one pod at a time, which is what a sandbox can use for EBS.
3. `storageClassName: auto-ebs-sc`: an encrypted gp3 class that `storage.yaml` defines for the Auto Mode EBS driver.

```yaml
# from alternatives/persistent-workspace/sandbox-template-persistent.yaml
  name: python-persistent #!
# ...
  volumeClaimTemplates:
    - metadata:
        name: workspace                 # the PVC is named workspace-<sandbox name>
      spec:
        accessModes: ["ReadWriteOncePod"]   # one pod at a time; the only mode EKS Auto Mode supports for EBS in a sandbox #!
        storageClassName: auto-ebs-sc #!
        resources:
          requests: { storage: 2Gi }
```

**How far it was checked.** Checked and unit-tested, not run. Six unit tests use a fake Kubernetes: suspend sends one merge patch and deletes nothing, resume waits until the sandbox is Ready and times out otherwise, and a checkpoint names the right volume. `make_template.py` derives the persistent template from the real one, and `check.sh` fails if the committed file drifts from it. The template passes the CRD schemas of the pinned agent-sandbox release. That check is not decorative: it flags a misspelled field in a copy I broke on purpose, and it accepts the deployed templates. The Manager patch applies and compiles.

**Not built, and why.** Restoring a checkpoint into a new sandbox is not written. A warm-pool sandbox is already running, and Kubernetes cannot attach a volume to a running pod, which agent-sandbox's own GKE example works around with a privileged helper on each node ([agent-sandbox](https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/latebind-storage-gke-sandbox/)). A restore would need a cold start and a different path. Also not checked: what the `Retain` shutdown policy does to the volume when the end time passes, and whether a suspended session needs its own expiry, because a suspended session holds a disk that costs money.

**10.8 gVisor and Kata, 10.9 GPU sandboxes and 10.10 confidential mode.**

**The gap.** Section 6 gives one reason each. gVisor needs a containerd entry that Auto Mode does not allow. Kata needs nodes with nested virtualization. GPUs cannot pass into a Nitro Sandbox. Confidential mode removes `exec`. Each is real, so each alternative has to run on nodes that are not Auto Mode nodes, and says so.

**What I built.** Node pools and runtime classes for Kata, gVisor and gVisor with a GPU; one generated file of SandboxTemplates for the three; a Manager patch that lists them; and the written procedure for confidential mode. The templates are derived from the real `python-untrusted` template. Only the name, the runtime class, a label and (for the GPU one) a GPU limit differ. Everything that makes the sandbox safe is copied unchanged: no service account token, a read-only root, dropped capabilities, the one egress rule and the gateway's certificate.

| Alternative | Runs on | Source | Label |
|---|---|---|---|
| Kata | Self-managed Karpenter 1.13 or later, nodes with nested virtualization. Not Auto Mode, whose NodeClass has no such field. | [Karpenter blueprint](https://aws-samples.github.io/karpenter-blueprints/blueprints/nested-virtualization/) | From documentation |
| gVisor | Any current instance type, with the `runsc` binaries and a containerd entry added by user data | [gVisor](https://gvisor.dev/docs/user_guide/containerd/configuration/) | From documentation |
| GPU | gVisor with `nvproxy` on T4, A10G, L4, A100 and H100, with a driver version gVisor knows | [gVisor GPU](https://gvisor.dev/docs/user_guide/gpu/) | From documentation |
| Confidential | Nitro Sandbox runtime class `confidential-sandbox`, with a Cedar policy | [AWS](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-confidential-setup.html) | From documentation |

The highlighted lines, in order:

1. `nestedVirtualization: enabled`: two jobs in one field: it turns the feature on for the launched instance, and it makes Karpenter skip instance types that cannot do it ([Karpenter blueprint](https://aws-samples.github.io/karpenter-blueprints/blueprints/nested-virtualization/)).
2. `katacontainers.io/kata-runtime: "true"`: the Kata installer runs only on nodes with this label.
3. `startupTaints`: a new node is kept closed to sandbox pods until the Kata installer has finished and removed this taint.

```yaml
# from alternatives/isolation-tiers/kata/nodepool.yaml
  cpuOptions:
    nestedVirtualization: enabled      # sets the instance's CpuOptions and filters out families that cannot do it #!
# ...
        katacontainers.io/kata-runtime: "true"   # the kata-deploy DaemonSet installs Kata only where this label is set #!
# ...
      startupTaints: #!
        - { key: katacontainers.io/kata-runtime, effect: NoSchedule }
```

The highlighted lines, in order:

1. `runtime_type = "io.containerd.runsc.v1"`: tells containerd that a runtime named runsc is run through gVisor's shim. The runtime class's handler must carry the same name.
2. `sha512sum -c runsc.sha512`: the binary is checked against a checksum that comes from the same site, so this catches a damaged download and does not protect against a hostile site. Pin a release and bake the binary into the node image for real use.

```yaml
# from alternatives/isolation-tiers/gvisor/nodepool.yaml
            runtime_type = "io.containerd.runsc.v1" #!
# ...
    sha512sum -c runsc.sha512 #!
```

The highlighted lines, in order:

1. `ConfigPath = "/etc/containerd/runsc-gpu.toml"`: a second gVisor runtime, with its own settings file, so GPU nodes do not change the plain gVisor runtime.
2. `nvproxy = "true"`: turns on the proxy that forwards the sandbox's GPU driver calls to the host driver.
3. `karpenter.k8s.aws/instance-family: g4dn, g5, g6`: T4, A10G and L4 nodes, on gVisor's supported list.

```yaml
# from alternatives/isolation-tiers/gpu/nodepool.yaml
            ConfigPath = "/etc/containerd/runsc-gpu.toml" #!
# ...
    nvproxy = "true" #!
# ...
        - { key: karpenter.k8s.aws/instance-family, operator: In, values: ["g4dn", "g5", "g6"] } #!
```

**What is weaker.** gVisor is a user-space kernel, not a hardware boundary. With `nvproxy` the sandbox's GPU calls reach the host's NVIDIA driver, and gVisor's own documentation says it is much less effective against bugs in that driver, so driver updates are part of the security plan. The alternative to gVisor for a GPU is what AWS says: run GPU pods without a sandbox runtime class, on nodes used by one tenant at a time.

**Confidential mode.** `confidential/README.md` holds the procedure from the AWS page and a KMS key policy statement. The steps are: generate a Cedar policy from the exact final pod spec with `eks-nitro-sandbox-genpolicy`, attach it as an annotation, and put the predicted PCR17 value and the PCR8 service-name condition in the KMS key policy. The README lists why this does not drop into the deployed design as a template. The pod is built by the controller from a template and a pool, so the final spec is not in a file, and the policy must match it exactly. I did not check whether a canary pod read back from the cluster reproduces what the next pooled pod carries.

**How far it was checked.** The three templates pass the pinned CRD schemas, and the two runtime classes pass the Kubernetes schemas (`kubeconform`). The Manager patch applies and compiles. `make_templates.py` regenerates the templates and `check.sh` fails on drift. **The node pools, the Kata and gVisor installs, the GPU path and confidential mode were never run.** A named risk: whether the network policies on the sandbox namespace are enforced the same way for a Kata or gVisor pod as for a Nitro Sandbox pod. I did not check, and the deployed design depends on it.

**10.11 Narrower network rules.**

**The gap.** The defence-in-depth section explains that the security group rules between the node groups name groups and allow every port, and that the narrow control is the pod's network policy. Two things follow. The security groups can be narrower. And the platform pods have rules for who may call them, but none for where they may call, so a compromised Manager can open a connection to anything its node can reach.

The highlighted lines, in order:

1. `from_port = 10250`: the control plane may reach the kubelet and nothing else on the sandbox nodes, apart from sandboxd below.
2. `toset(["443", "3128"])`: a sandbox node may reach the cluster group on the API port and the gateway port only, instead of every port.
3. `from_port = 3128`: a security group of its own for the gateway pods: the proxy port, from sandbox nodes only, so the VPC refuses everything else before a network policy sees it.

```hcl
# from alternatives/network-gaps/terraform/main.tf
resource "aws_vpc_security_group_ingress_rule" "sandbox_kubelet" {
  security_group_id            = var.sandbox_sg_id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 10250 #!
  to_port                      = 10250
# ...
resource "aws_vpc_security_group_egress_rule" "sandbox_to_cluster" {
  for_each                     = toset(["443", "3128"]) #!
  security_group_id            = var.sandbox_sg_id
  referenced_security_group_id = var.cluster_sg_id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
# ...
resource "aws_vpc_security_group_ingress_rule" "gateway_from_sandboxes" {
  security_group_id            = aws_security_group.gateway_pods.id
  referenced_security_group_id = var.sandbox_sg_id
  ip_protocol                  = "tcp"
  from_port                    = 3128 #!
  to_port                      = 3128
```

The highlighted lines, in order:

1. `port: 8000`: the tool gateway may call the Manager.
2. `port: 8443`: and the metrics API.
3. `port: 8001`: and the credential broker in the egress namespace.
4. `port: 53`: and DNS. Nothing else.
5. `egress: []`: the metrics API calls nobody, so it may call nobody.

```yaml
# from alternatives/network-gaps/egress-policies.yaml
metadata: { name: tool-gateway-egress, namespace: sandbox-platform }
spec:
  podSelector: { matchLabels: { app: tool-gateway } }
  policyTypes: [Egress]
  egress:
    - to: [{ podSelector: { matchLabels: { app: sandbox-manager } } }]
      ports: [{ protocol: TCP, port: 8000 }] #!
    - to: [{ podSelector: { matchLabels: { app: metrics-api } } }]
      ports: [{ protocol: TCP, port: 8443 }] #!
    - to:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: sandbox-egress } }
          podSelector: { matchLabels: { app: credential-broker } }
      ports: [{ protocol: TCP, port: 8001 }] #!
    - to: [{ ipBlock: { cidr: 0.0.0.0/0 } }]
      ports: [{ protocol: UDP, port: 53 }, { protocol: TCP, port: 53 }] #!
# ...
metadata: { name: metrics-api-egress, namespace: sandbox-platform }
spec:
  podSelector: { matchLabels: { app: metrics-api } }
  policyTypes: [Egress]
  egress: [] #!
```
The file has one egress policy for each platform component (Manager, tool gateway, metrics API, router and orchestrator). I read each component's calls from its source and from `k8s/40-platform.yaml`. The credential broker and the gateway's own calls to the cloud are not covered.

**Security groups for individual pods.** `securitygrouppolicy.yaml` gives the gateway pods a security group of their own. This is an EKS Auto Mode feature for Kubernetes 1.36 and later ([AWS](https://docs.aws.amazon.com/eks/latest/userguide/auto-sgp.html)). Each matched pod gets a branch network interface, which lowers how many pods fit on a node, and a pod can match only one policy. I did not apply it to sandbox pods, because the AWS page does not say whether it reaches a pod inside a Nitro Sandbox.

**How far it was checked.** Checked. The Terraform passes `terraform validate`, and the five policies pass the Kubernetes schemas. **Not run.** Two risks: the security group ports are what the design is known to use, and a node needs more to join a cluster and pull images, so a missed port would show as nodes that never become Ready. Try it on a throwaway cluster first. And the egress rules allow the API server by the VPC's address range, because the control plane's own addresses are not exported by this implementation. The security group rules must replace the three deployed ones, because rules add up and the old, wider ones would still apply.

**10.12 Snapshots and multi-cluster placement: a decision note.** The design lists both as open questions. I did not build either, and I looked at what the platforms allow.

| Question | What I found | Decision |
|---|---|---|
| Snapshot a running sandbox, with its memory | AWS says Nitro Sandboxes do not support snapshot, restore or live migration ([limitations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)). agent-sandbox has memory snapshots only on GKE with gVisor. | Not possible on this runtime. Section 10.7's checkpoint saves files, not memory. |
| Checkpoint the files | EBS snapshots work, and EKS Auto Mode is compatible with the snapshot controller but does not install it ([AWS](https://docs.aws.amazon.com/eks/latest/userguide/csi-snapshot-controller.html)). | Covered in 10.7. |
| Place sessions across clusters (CPU and GPU) | Out of scope for one cluster. | This is Design 02, Appendix C. |


**The files.** `alternatives/README.md` has the index with the label for each folder. `alternatives/check-all.sh` runs everything that can run on one machine. Two helpers are shared: `validate-terraform.sh` runs `terraform validate` in a throwaway copy, and `validate_agent_sandbox_crds.py` checks agent-sandbox YAML against the CRD schemas of the pinned release, after it verifies the download against the SHA-256 that Terraform pins.

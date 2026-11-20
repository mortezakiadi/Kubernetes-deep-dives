---
id: appendix-design-01-eks
title: "Appendix B: Design 01 built and tested on Amazon EKS"
short: "B. Design 01 on EKS (built and tested)"
---
This appendix describes a working implementation of [Design 01: Sandboxing and secure execution for agents and untrusted code](#design-sandboxing). I built it with Terraform on Amazon EKS in one AWS account, tested every claim in the design that can be tested on this platform, recorded the evidence, and removed it again with one command. Everything here ran on a real cluster. Every command, output and pass or fail result in section 5 is copied from the recorded test run, not written from memory.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested with the commands, the results, why the results can be trusted and why the system produces them. The last section is an alternative egress gateway on Envoy, which I tested in a lab and not on the cluster.

{{RESULT_SUMMARY}}

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

![Who may call whom, and what enforces each allowed path](diagrams/appendix/a02-11.svg)

**What the picture shows.** Every pair of components, with the rule that admits the call. A green cell is a path that exists, and it names the port or the role that admits it. A grey cell is refused, because the called service has an ingress policy that does not name the caller. The Kubernetes API column is controlled by roles, and by the absence of a token in most pods. The amber cells are the honest part. Only the gateway pods and the sandboxes have an egress network policy, so the other platform pods can reach the internet through the NAT gateway. What holds them back is that nothing they run calls out, and that they hold no credential worth stealing. Egress policies for them are a hardening step that this build does not take.

**The manifests and code that enforce it.** Each block below is copied from the file named on its first line, and the build fails if a line of a block is not in that file. Lines that I left out are marked with `# ...`.

**Layers 1 and 2: the sandbox blocks and the sandbox security group.**

```hcl
# from terraform/10-cluster/network.tf
resource "aws_subnet" "sandbox" {
  count             = length(var.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = var.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, 4 + count.index) # 10.20.64.0/20, 10.20.80.0/20
  tags = {
    Name        = "${var.project}-sandbox-${var.azs[count.index]}"
    "ds01/tier" = "sandbox"
  }
}
# ...
resource "aws_security_group" "sandbox_nodes" {
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
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "-1"
  description                  = "Control plane and platform pods to sandbox nodes"
}
# ...
resource "aws_vpc_security_group_ingress_rule" "cluster_from_sandbox" {
  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.sandbox_nodes.id
  ip_protocol                  = "-1"
  description                  = "Sandbox nodes to control plane and the egress gateway"
}
```

**Layers 3 and 4: access to the cluster, and the sandbox node role.**

```hcl
# from terraform/10-cluster/eks.tf
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }
# ...
  vpc_config {
    # Platform pods (controller, gateways) go in the system subnets. The sandbox
    # subnets are reached through a dedicated NodeClass, not through this list.
    subnet_ids              = aws_subnet.system[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = [var.admin_cidr]
  }
# ...
  enabled_cluster_log_types = ["api", "audit", "authenticator"]
# ...
resource "aws_iam_role_policy_attachment" "sandbox_node" {
  for_each = toset([
    "AmazonEKSWorkerNodeMinimalPolicy",
    "AmazonEC2ContainerRegistryPullOnly",
  ])
  role       = aws_iam_role.sandbox_node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}
```

**Layer 5: the sandbox node.** The node class picks the sandbox blocks by tag, and the sandbox security group. The taint keeps every other pod off.

```yaml
# from k8s/10-sandbox-nodes.yaml
apiVersion: eks.amazonaws.com/v1
kind: NodeClass
metadata:
  name: sandbox
spec:
  role: ${node_role}
  subnetSelectorTerms:
    - tags:
        ds01/tier: sandbox
  securityGroupSelectorTerms:
    - id: ${sandbox_sg}
  ephemeralStorage:
    size: 40Gi
  advancedNetworking:
    associatePublicIPAddress: false
---
apiVersion: karpenter.sh/v1
# ...
      # Nothing else tolerates this taint, so only sandbox pods can land here.
      taints:
        - key: workload
          value: sandbox
          effect: NoSchedule
# ...
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 10m
```

**Layer 6: admission and limits.**

```yaml
# from k8s/00-namespaces.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: sandboxes
  labels:
    ds01/zone: untrusted
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

```yaml
# from k8s/20-sandboxes.yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: sandboxes-quota
  namespace: sandboxes
spec:
  hard:
    pods: "30"
    requests.cpu: "24"
    requests.memory: 48Gi
```

**Layers 10 and 11: the microVM and the process.** The template asks for the isolated runtime class and drops everything a process could use.

```yaml
# from k8s/20-sandboxes.yaml
      runtimeClassName: isolated-sandbox      # Tier 2: one microVM per pod
      serviceAccountName: sandbox-nobody
      automountServiceAccountToken: false     # no Kubernetes credentials inside
      enableServiceLinks: false
      tolerations:
        - { key: workload, operator: Equal, value: sandbox, effect: NoSchedule }
      nodeSelector:
        workload: sandbox
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
# ...
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
```

**Layer 7: RBAC.** The Manager may create and read claims and read pods. That is all.

```yaml
# from k8s/40-platform.yaml
# The Manager's whole Kubernetes permission: create and read claims, and read pods. Nothing else.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: sandbox-manager, namespace: sandboxes }
rules:
  - apiGroups: ["extensions.agents.x-k8s.io"]
    resources: ["sandboxclaims"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: sandbox-manager, namespace: sandboxes }
subjects: [{ kind: ServiceAccount, name: sandbox-manager, namespace: sandbox-platform }]
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: Role, name: sandbox-manager }
```

**Layer 8: the pod network.** The namespace denies everything. The template then admits exactly one way out and one way in. EKS Auto Mode ships with network-policy enforcement off, and one setting turns it on.

```yaml
# from k8s/20-sandboxes.yaml
# Namespace-wide default deny. Anything in this namespace that no template policy admits is isolated.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: sandboxes
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
# ...
  networkPolicy:
    ingress:
      - from:
          - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: sandbox-router } }
            podSelector: { matchLabels: { app: sandbox-router } }
        ports:
          - { protocol: TCP, port: 8080 }   # sandboxd files
          - { protocol: TCP, port: 9090 }   # sandboxd commands
    egress:
      # The one way out. No DNS rule: the proxy resolves names, so a sandbox cannot even look one up.
      - to:
          - ipBlock: { cidr: ${gateway_ip}/32 }
        ports:
          - { protocol: TCP, port: 3128 }
```

```hcl
# from terraform/20-platform/manifests.tf
resource "kubernetes_config_map_v1" "network_policy" {
  metadata {
    name      = "amazon-vpc-cni"
    namespace = "kube-system"
  }
  data = { "enable-network-policy-controller" = "true" }
}
```

Every platform service also says who may call it, for example:

```yaml
# from k8s/40-platform.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: sandbox-manager-ingress, namespace: sandbox-platform }
spec:
  podSelector: { matchLabels: { app: sandbox-manager } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: agent-orchestrator } }
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: sandbox-egress } }
          podSelector: { matchLabels: { app: egress-gateway } }
        - podSelector: { matchLabels: { app: tool-gateway } }
      ports: [{ protocol: TCP, port: 8000 }]
# ...
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: sandbox-router-ingress, namespace: sandbox-router }
spec:
  podSelector: { matchLabels: { app: sandbox-router } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: agent-orchestrator } }
      ports: [{ protocol: TCP, port: 8004 }]
```

**Layer 9: the domain ceiling.** Even if a session's allowlist is wrong, the gateway pods cannot reach a name outside this list.

```yaml
# from k8s/30-egress.yaml
apiVersion: networking.k8s.aws/v1alpha1
kind: ApplicationNetworkPolicy
metadata: { name: egress-gateway-ceiling, namespace: sandbox-egress }
spec:
  podSelector: { matchLabels: { app: egress-gateway } }
  policyTypes: [Egress]
  egress:
    - to:
        - ipBlock: { cidr: 0.0.0.0/0 }
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
    - to:
        - domainNames:
            - pypi.org
            - files.pythonhosted.org
            - example.com
      ports: [{ protocol: TCP, port: 443 }]
```

**Layer 12: the gateway's policy.** The allowlist, the rate limit and the byte budget are checked on every tunnel, and the decision is logged.

```python
# from platform/ds01/gateway_addon.py
RATE_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))
BYTE_BUDGET = int(os.environ.get("SESSION_BYTE_BUDGET", str(50 * 1024 * 1024)))
# ...
def host_allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allow:
        if host == entry or (entry.startswith("*.") and host.endswith(entry[1:])):
            return True
    return False
# ...
    def http_connect(self, flow: http.HTTPFlow):
        ip, host = flow.client_conn.peername[0], flow.request.host.lower()
        s = self.session(ip)
        if not s:
            return self._deny(flow, 403, "source is not a known sandbox", None, host)
        sid = s["session_id"]
        if not host_allowed(host, s["allow_hosts"]):
            return self._deny(flow, 403, f"{host} is not on this session's allowlist", sid, host)
        if self.over_rate(sid):
            return self._deny(flow, 429, "request rate limit for this session", sid, host)
        if self._bytes[sid] > BYTE_BUDGET:
            return self._deny(flow, 429, "byte budget for this session is used up", sid, host)
        mode = "terminate" if host in s["inject"] else "passthrough"
        audit(COMPONENT, "egress.allow", session_id=sid, tenant=s["tenant"], host=host, port=flow.request.port, tls_mode=mode,
              source=ip)
```

**Layer 13: credentials and tools.** The broker caps the lifetime of every credential. The tool gateway keeps a table of which agent may call which tool and how risky each tool is.

```python
# from platform/ds01/broker.py
AUDIENCES = {"metrics-api": 900, "tool-gateway-backend": 900}
# ...
            ttl = min(int(body.get("ttl_seconds", 900)), AUDIENCES[aud])
            token = mint(broker_key, {"aud": aud, "sub": session, "tenant": tenant, "scope": "read"}, ttl)
```

```python
# from platform/ds01/tool_gateway.py
RATE_PER_MIN = 20
# ...
TOOLS = {
    "metrics.query": {"risk": "read", "agents": {"data-analyst", "coding"},
                      "description": "Read a metric series from the company metrics API."},
    "ticket.create": {"risk": "high", "agents": {"data-analyst"},
                      "description": "Create a ticket. Needs a human approval each time."},
    "project.delete": {"risk": "forbidden", "agents": set(),
                       "description": "Delete a project. Never available to an agent."},
}
```

**Layer 14: audit.** Every service writes through one function, which redacts any field that looks like a secret.

```python
# from platform/ds01/common.py
_SECRET_NAME = re.compile(r"(token|secret|password|authorization|api[_-]?key|credential)", re.I)
_SAFE_SUFFIX = ("_id", "_sha256", "_len")
# ...
def audit(component: str, event: str, **fields) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "component": component,
        "event": event,
    }
    for key, value in fields.items():
        if _SECRET_NAME.search(key) and not key.endswith(_SAFE_SUFFIX):
            value = "[redacted]"
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


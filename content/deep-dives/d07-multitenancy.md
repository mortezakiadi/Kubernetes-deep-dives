---
id: d-multitenancy
title: "Multi-tenancy on Kubernetes: soft vs. hard isolation"
short: "Kubernetes Multi-tenancy"
---
A SaaS platform hosts standard, premium and regulated tenants on a shared Kubernetes cluster. The question running through this deep dive: *if one tenant is compromised, what can it reach?*

### Tenant classes, soft and hard isolation

**Scenario.** A SaaS company is moving all its customers onto one shared Kubernetes cluster. Most customers are small, but a few are banks with PCI and data-residency rules, and one noisy customer once took down a shared database. The architects need a plan for which customers can share and which need walls between them.

![Tenant classes, soft and hard isolation](diagrams/sections/d07-1.svg)

**What the picture shows.** A mind map of the multi-tenancy strategy, from classifying tenants to the two isolation styles.

- **Tenant classification:** decide what each tenant needs based on **data sovereignty**, **compliance** (PCI, HIPAA, SOC 2), acceptable **blast radius** and **SLA requirements**.
- **Soft isolation (namespace):**
  - **ResourceQuotas and LimitRanges:** stop one tenant from exhausting cluster resources.
  - **NetworkPolicies (default deny):** block traffic between tenants unless explicitly allowed.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: tenant-a
spec:
  podSelector: {}          # every pod in the namespace #!
  policyTypes: ["Ingress", "Egress"] #!
```

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: tenant-a
spec:
  podSelector: {}          # every pod in the namespace #!
  policyTypes: ["Ingress", "Egress"] #!
```

  - **Pod Security Standards, enforced by Pod Security Admission (PSA):** three levels, Privileged (no restrictions, the danger zone), Baseline (reasonable defaults) and Restricted (maximum, production-grade hardening). The example labels the `prod` namespace with `pod-security.kubernetes.io/enforce: restricted`.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: prod
  labels:
    pod-security.kubernetes.io/enforce: restricted #!
```

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: prod
  labels:
    pod-security.kubernetes.io/enforce: restricted #!
```

  - **RBAC-scoped service accounts:** the identity each pod runs as. Service accounts always live in a namespace; the point is to grant them namespace `Role`s and `RoleBinding`s, never cluster-wide `ClusterRoleBinding`s.
- **Hard isolation (node / cluster):**
  - **Dedicated node groups:** different instance families per workload type.
  - **Taints and tolerations:** make sure a tenant's pods run only on its designated nodes.
  - **VPC subnet segmentation:** separate subnets per tenant tier for network-level separation.
  - **Dedicated clusters (highly regulated tenants):** complete isolation, at the cost of operational complexity and money.
- **The key warning:** namespaces are *authorization* boundaries, not *trust* boundaries. They protect against configuration mistakes, but not against kernel exploits or host-level resource exhaustion.

**Going deeper.**

- GPUs need the same soft vs. hard choice.
- **Time slicing** advertises several virtual GPUs per physical GPU, with no memory or fault isolation: one pod can exhaust the memory or crash the GPU for everyone.
- **MIG** (on data-center GPUs such as A100, H100 and newer) carves a GPU into hardware-isolated slices with their own memory, for example up to 7 on an A100-40GB.
- That's the right choice for strict multi-tenancy.
- Quota *between* tenants (who gets how many GPUs) is a separate problem, handled by Kueue's ClusterQueues and cohorts.

### Identity, data, network and observability

**Scenario.** The same SaaS company now has to prove to an auditor that tenant A can never read tenant B's data, even if a pod is compromised or credentials leak. That has to hold across AWS permissions, the databases, the network and the logs.

![Identity, data, network and observability](diagrams/sections/d07-2.svg)

**What the picture shows.** Tenant isolation beyond Kubernetes, in four areas.

- **Identity and access:**
  - **IRSA (IAM Roles for Service Accounts) with an OIDC provider:** each tenant's pods get their own IAM role, mapped to their Kubernetes service account through the cluster's OIDC provider.
  - **Fine-grained AWS permissions:** per-tenant roles shrink the blast radius and enforce tenant isolation at the AWS level.
- **Data layer:**
  - **RDS (Proxy, IAM auth, RLS):** RDS Proxy prevents connection-storm crashes. The app uses IAM to get a short-lived database token, and row-level security inside the database maps IAM role → DB role → RLS policy.
  - **S3 (bucket policies, VPC endpoints):** a bucket per tenant, with bucket policies that allow only that tenant's role and only through your VPC endpoint, so leaked credentials are useless outside your network.
  - **ElastiCache (dedicated or logical):** separate clusters per tier, with logical databases per tenant in shared tiers.
- **Network architecture:**
  - **Shared ALB vs. dedicated NLB:** a shared ALB with host-based routing for the standard tier; dedicated NLBs for the premium tier, with client IP preservation and no noisy neighbours.
  - **AWS PrivateLink:** if the customer's app lives in its own VPC, traffic flows consumer VPC → VPC endpoint → AWS backbone → your NLB → your service.
  - **Calico / Cilium (L7 controls):** for advanced network policies, since native NetworkPolicies have no L7 rules and no DNS-based egress filtering.
- **Observability:**
  - **Fluent Bit (tenant labels):** forwards logs to a separate CloudWatch log group per tenant.
  - **Prometheus and Thanos:** metrics, with Thanos for long-term storage.
  - **Distributed tracing (X-Ray or Jaeger):** with tenant context propagated through every call.
- **The three-layer boundary:** tenant isolation is enforced at (1) the Kubernetes namespace and scheduling boundary, (2) the per-tenant AWS IAM identity boundary, and (3) the database boundary, either a separate instance or RLS.

**Going deeper.**

- For shared LLM endpoints, the unit of fairness is the **token**, not the request: one call can cost 10 tokens or 10,000.
- AI gateways (Envoy AI Gateway, Kuadrant) enforce token-based rate limits per user or API key.
- Tracing follows the same tenant-aware approach: model servers like vLLM emit OpenTelemetry spans that can be sent to Jaeger with the tenant in context.

### Sandboxed runtimes: gVisor and Kata

**Scenario.** A premium customer asks: "If another tenant's container is hacked and the attacker finds a Linux kernel bug, can they escape and reach my workload on the same node?" With plain containers, the honest answer is yes.

![Sandboxed runtimes: gVisor and Kata](diagrams/sections/d07-3.svg)

**What the picture shows.** Kernel-level isolation with sandboxed runtimes, gVisor and Kata Containers.

- **The risk with standard `runc`:** all containers on a node share the host kernel. If an attacker finds a kernel bug, they can escape the container and take over the host.
- **Tiered runtimes to manage that risk:**
  - **Standard tier:** the default `runc` for internal, trusted apps.
  - **Premium tier:** **gVisor** for multi-tenant SaaS apps, which intercepts system calls in a user-space kernel so a kernel bug can't be reached directly.
  - **Ultra-secure / regulated tier:** **Kata Containers** for customers with strict compliance (PCI-DSS, HIPAA) who need the hardware isolation of a VM. Kata works much like Fargate: each pod gets its own lightweight VM.
- **How you switch it on, step 1, a `RuntimeClass`:** named `gvisor`, with `handler: runsc`, which tells the container engine to use gVisor.
- **Step 2, the Deployment:** a premium tenant's Deployment adds one line, `runtimeClassName: gvisor`. Kubernetes runs those containers with gVisor, using the same standard image, with no rebuild. If only some nodes have gVisor installed, add a `scheduling.nodeSelector` to the RuntimeClass so these pods land on them.

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc #!
scheduling:
  nodeSelector:
    sandbox.gvisor: "true"         # only nodes where gVisor is installed #!
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: premium-tenant-app
  namespace: tenant-a-premium
spec:
  replicas: 2
  selector:
    matchLabels: {app: my-app}
  template:
    metadata:
      labels: {app: my-app}
    spec:
      runtimeClassName: gvisor     # the one extra line #!
      containers:
      - name: my-container
        image: my-company/standard-app:1.0
```

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc #!
scheduling:
  nodeSelector:
    sandbox.gvisor: "true"         # only nodes where gVisor is installed #!
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: premium-tenant-app
  namespace: tenant-a-premium
spec:
  replicas: 2
  selector:
    matchLabels: {app: my-app}
  template:
    metadata:
      labels: {app: my-app}
    spec:
      runtimeClassName: gvisor     # the one extra line #!
      containers:
      - name: my-container
        image: my-company/standard-app:1.0
```

**Going deeper.**

- AI agents that run generated code or untrusted tools need the same isolation.
- The Kubernetes community's **agent sandbox** project adds a pod-shaped, single-instance `Sandbox` with a stable identity that can be paused and resumed, isolated with gVisor or Kata.
- It's designed for interactive or untrusted agent workloads.

### Hardening runC: seccomp, AppArmor, user namespaces

**Scenario.** A security reviewer asks what happens if an attacker gets code execution inside one tenant's container. Even without a sandboxed runtime, the platform should limit what that attacker can do: which system calls they can make, which files they can touch, and who they are on the host if they break out.

![Hardening runC](diagrams/sections/d07-4.svg)

**What the picture shows.** Three layers of in-kernel hardening for every container.

- **seccomp:** a properly configured seccomp profile is an allowlist that blocks every system call the application doesn't strictly need.
- **AppArmor / SELinux policies:** mandatory access control in the kernel, on top of normal file permissions (like `chmod 777`). For example, a policy can say "the Nginx process may only read `/var/www/html` and only write to `/var/log/nginx`".
- **User namespace remapping:** the kernel maps UID 0 (root) inside the container to a high, unprivileged UID on the host. If an attacker breaks out, they find themselves as a powerless user on the host, unable to do damage.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: tenant-app
spec:
  hostUsers: false               # root in the pod maps to an unprivileged host UID #!
  securityContext:
    seccompProfile:
      type: RuntimeDefault #!
    appArmorProfile:
      type: RuntimeDefault #!
  containers:
  - name: app
    image: my-company/tenant-app:1.0
```

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: tenant-app
spec:
  hostUsers: false               # root in the pod maps to an unprivileged host UID #!
  securityContext:
    seccompProfile:
      type: RuntimeDefault #!
    appArmorProfile:
      type: RuntimeDefault #!
  containers:
  - name: app
    image: my-company/tenant-app:1.0
```

### Control plane security

**Scenario.** An internal pentest finds that the production cluster's Kubernetes API endpoint is reachable from the internet, any team can deploy images from any registry, and nobody can tell which tenant made a given API call. The control plane is managed by AWS, but access to it still needs hardening.

![Control plane security](diagrams/sections/d07-5.svg)

**What the picture shows.** Hardening access to the EKS control plane.

- **The starting point:** AWS runs the EKS control plane, but you are responsible for who can reach the API server and what they can do.
- **Private endpoint only:** for production clusters, turn off the public API endpoint (or restrict it to known CIDR ranges). Who can reach the private endpoint is controlled by the cluster security group.
- **Admission webhooks (OPA Gatekeeper or Kyverno):** enforce rules the built-in admission controllers can't, such as allowed image registries, required labels and resource ratios.
- **Audit logging:** send API audit logs to CloudWatch with tenant context, and look for anomalies with CloudWatch Logs Insights or EventBridge patterns.

### Network isolation and egress

**Scenario.** A regulated customer requires that traffic between their pods is encrypted, and that their pods can only reach approved external services. The platform team also struggles with egress rules: pod IPs change all the time, so a firewall can't tell which tenant sent a packet.

![Network isolation and egress](diagrams/sections/d07-6.svg)

**What the picture shows.** Network-level isolation between tenants: overlays, eBPF, encryption and egress.

- **Overlay networking with Calico (VXLAN):** Calico can run its own VXLAN overlay, so pod addresses no longer depend on how the VPC subnets are laid out. The isolation itself still comes from Calico's network policies; an overlay alone doesn't separate tenants.
- **Cilium with eBPF:** a CNI that can replace kube-proxy and iptables. Instead of a huge rulebook, it uses eBPF programs to reprogram the kernel's network path on the fly. That gives fast packet filtering plus L7 policies (filtering on HTTP method and path) and DNS-aware policies.
- **Transparent encryption (WireGuard or IPsec):** for regulated workloads, the CNI intercepts each packet just before it leaves the node, wraps it in an encrypted tunnel, decrypts it on the receiving node and hands it to the target pod. This gives pod-to-pod encryption at L3, in the kernel, without a service mesh.
  - **Cilium** is the most popular choice on EKS, using eBPF for fast WireGuard or IPsec.
  - **Even better on Nitro:** AWS encrypts traffic in transit automatically, in hardware, between certain Nitro instance types.
- **Egress control:** use VPC endpoints for AWS services and a NAT gateway with security group rules, or a transparent egress proxy with tenant-aware filtering.
- **Tenant-aware filtering:** the problem in the scenario is that the proxy doesn't know which tenant sent a packet, and pod IPs keep changing. Tenant-aware filtering bridges that identity gap: the proxy first works out who sent the packet, then applies that tenant's rules.

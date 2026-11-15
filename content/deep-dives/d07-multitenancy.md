---
id: d-multitenancy
title: "Multi-tenancy on EKS: soft vs. hard isolation"
short: "EKS Multi-tenancy"
---
A SaaS platform hosts standard, premium and regulated tenants on shared EKS. The question running through this tab: *if one tenant is compromised, what can it reach?*

### Tenant classes, soft and hard isolation {#d07-1}

**Scenario.** Sales has just signed a healthcare customer with HIPAA requirements. Can they live next to free-tier tenants?

![Tenant classes, soft and hard isolation](diagrams/sections/d07-1.svg)

**What the picture shows.** Tenants are classified by data sovereignty, compliance, blast radius and SLA. *Soft* isolation is namespace-based: ResourceQuotas and LimitRanges, default-deny NetworkPolicies, Pod Security Admission `restricted`, and namespace-scoped service accounts. *Hard* isolation means dedicated node groups with taints, subnets per tier, or a dedicated cluster for highly regulated tenants.

> **From the chapter:** quota between tenants (who gets the GPUs) is a separate problem from isolation between tenants (who can see what). Kueue's ClusterQueues and cohorts handle the first; the tools on this tab handle the second.

### Identity, data, network and observability {#d07-2}

**Scenario.** Tenant A's leaked credentials must not be able to read tenant B's data.

![Identity, data, network and observability](diagrams/sections/d07-2.svg)

**What the picture shows.** The tenant boundary is enforced at three layers:

- **Identity:** a per-tenant IAM role through IRSA.
- **Data:** RDS with IAM auth and row-level security, per-tenant S3 bucket policies and VPC endpoints, and separate ElastiCache by tier.
- **Network:** a shared ALB for the standard tier and a dedicated NLB plus PrivateLink for premium tenants.

Logs, metrics and traces all carry tenant labels.

### Sandboxed runtimes: gVisor and Kata {#d07-3}

**Scenario.** A premium tenant asks whether a kernel exploit in another tenant's container could reach their workload.

![Sandboxed runtimes: gVisor and Kata](diagrams/sections/d07-3.svg)

**What the picture shows.** With standard runC, all containers share the host kernel, so a kernel bug can lead to escape. gVisor puts a user-space kernel in between, and Kata runs each pod in a lightweight VM (similar in spirit to Fargate's Firecracker). You opt in per workload with a `RuntimeClass`.

### Hardening runC: seccomp, AppArmor, user namespaces {#d07-4}

**Scenario.** Most workloads stay on runC. How do we shrink the attack surface without changing runtimes?

![Hardening runC](diagrams/sections/d07-4.svg)

**What the picture shows.**

- **seccomp:** an allow-list of the syscalls the app actually needs.
- **AppArmor/SELinux:** mandatory access control, for example "nginx may only read `/var/www`".
- **User-namespace remapping:** root inside the container is an unprivileged user on the host.

### Control plane security {#d07-5}

**Scenario.** An audit finds the EKS API endpoint is reachable from the internet.

![Control plane security](diagrams/sections/d07-5.svg)

**What the picture shows.** A private endpoint with VPC endpoint policies, admission webhooks (OPA Gatekeeper or Kyverno) for rules that built-in admission can't express, and audit logs to CloudWatch with tenant context for anomaly detection.

### Network isolation and egress {#d07-6}

**Scenario.** A regulated tenant needs encryption between pods and control over what its pods can call on the internet.

![Network isolation and egress](diagrams/sections/d07-6.svg)

**What the picture shows.** Calico VXLAN overlays per tenant, Cilium's eBPF L7 and DNS-aware policies, and transparent WireGuard/IPsec encryption without a service mesh (or Nitro's hardware encryption in transit). Egress goes through VPC endpoints, or through a tenant-aware egress proxy that knows which pod sent each packet.

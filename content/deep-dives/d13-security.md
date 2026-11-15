---
id: d-security
title: "EKS security from build to runtime"
short: "EKS Security"
---
Security asks for one picture of how a model-serving platform is protected before launch. This deep dive answers it layer by layer.

### What we protect

**Scenario.** Kickoff with the security team: what is in scope?

![What we protect](diagrams/sections/d13-1.svg)

**What the picture shows.** The stack from host to containers, dependencies, code, configuration and data, and the two places container risk enters: the build pipeline and the runtime environment.

### The build pipeline

**Scenario.** A base image with a critical CVE almost reaches production.

![The build pipeline](diagrams/sections/d13-2.svg)

**What the picture shows.** Build (minimal or distroless base images, multi-stage builds, immutable tags) → test (Snyk in CI) → store (signing with Cosign or AWS Signer, OPA Gatekeeper) → encrypt (KMS, Vault, Secrets Store CSI) → scan (Inspector, Trivy) → deploy (private subnets, no SSH, IMDS blocked, hardened AMI) → monitor.

**Going deeper.**

- Model files belong in the supply chain too.
- Classic PyTorch `.pt` files use Python pickle, which can execute arbitrary code when loaded.
- **Safetensors** stores only tensors and can't, which makes it the safer default for weights from outside the organization.
- Signing and scanning model images works the same way as for application images.

### Runtime security

**Scenario.** A container is found running as root with `CAP_SYS_ADMIN`.

![Runtime security](diagrams/sections/d13-3.svg)

**What the picture shows.** Least privilege, no root or privileged containers, dropped capabilities in the security context, Pod Security Standards enforced through namespace labels, and runtime detection deployed as a DaemonSet.

### Network security

**Scenario.** A compromised pod tries to scan the rest of the cluster.

![Network security](diagrams/sections/d13-4.svg)

**What the picture shows.** Namespaces plus NetworkPolicies, zero-trust pod-to-pod traffic with a service mesh, WAF and OIDC at ingress and egress, secure DNS, and traffic visibility through Cilium, Calico or Datadog.

### Secret management

**Scenario.** Someone base64-decodes a Kubernetes Secret and asks, "is that all it takes?"

![Secret management](diagrams/sections/d13-5.svg)

**What the picture shows.** Secrets are only base64-encoded. Encrypt etcd with KMS envelope encryption, restrict access with RBAC, consider external managers (Vault, AWS Secrets Manager), rotate secrets, and monitor through a SIEM.

### Identity and model endpoints

**Scenario.** The inference service needs S3 access and a public HTTPS endpoint.

![Identity and model endpoints](diagrams/sections/d13-6.svg)

**What the picture shows.** IRSA vs. EKS Pod Identity (simpler setup through an agent DaemonSet), TLS everywhere with ACM certificates on the ALB or NLB, and WAF on the ingress.

**Going deeper.**

- Model endpoints also need **guardrails**: checks on the prompt before the model and on the answer after it.
- They catch prompt injection ("ignore all previous instructions"), jailbreaks, toxic output and leaked personal data.
- Small classifier models such as Llama Guard or Granite Guardian run as their own vLLM deployments.
- Frameworks like NeMo Guardrails, the FMS Guardrails Orchestrator, Guardrails AI or a moderation API wire them in.
- Every check adds latency and GPU cost, so keep the in-line checks small and sample the rest asynchronously.

Agents add an identity question: when an agent calls a tool (an MCP server) on a user's behalf, whose identity does the backend see? There are four common answers:

- **Pass the user's token through:** full per-user audit, but long-running tasks outlive the token and the token needs very broad scopes.
- **Kubernetes ServiceAccounts:** validated with `TokenReview` and authorized with `SubjectAccessReview`. Simple inside one cluster, but you lose the user.
- **OAuth2 token exchange (RFC 8693):** one token carries both the user (`sub`) and the agent (`act`).
- **SPIFFE/SPIRE mTLS:** short-lived workload certificates that can't be stolen like a token.

In practice teams layer them: workload mTLS plus user identity in the request, with a policy engine checking both. More in [Running agents in production](#post-agents-prod).

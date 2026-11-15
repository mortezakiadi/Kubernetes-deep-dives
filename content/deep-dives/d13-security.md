---
id: d-security
title: "EKS security from build to runtime"
short: "EKS Security"
---
Security asks for one picture of how a model-serving platform is protected before launch. This tab answers it layer by layer.

### What we protect

**Scenario.** Kickoff with the security team: what is in scope?

![What we protect](diagrams/sections/d13-1.svg)

**What the picture shows.** The stack from host to containers, dependencies, code, configuration and data, and the two places container risk enters: the build pipeline and the runtime environment.

### The build pipeline

**Scenario.** A base image with a critical CVE almost reaches production.

![The build pipeline](diagrams/sections/d13-2.svg)

**What the picture shows.** Build (minimal or distroless base images, multi-stage builds, immutable tags) → test (Snyk in CI) → store (signing with Cosign or AWS Signer, OPA Gatekeeper) → encrypt (KMS, Vault, Secrets Store CSI) → scan (Inspector, Trivy) → deploy (private subnets, no SSH, IMDS blocked, hardened AMI) → monitor.

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

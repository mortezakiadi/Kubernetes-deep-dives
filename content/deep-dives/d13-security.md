---
id: d-security
title: "Kubernetes security from build to runtime"
short: "Kubernetes Security"
---
Security asks for one picture of how a model-serving platform is protected before launch. This deep dive answers it layer by layer.

### What we protect

**Scenario.** Before launching an AI serving platform, the CISO asks a simple question: "Where can an attacker get in?" The team realizes a container is not a single thing to secure; it's a stack of layers, each with its own risks.

![What we protect](diagrams/sections/d13-1.svg)

**What the picture shows.** The layers of a container, each of which carries risk.

- **Container risks** come from every layer you ship and run:
  - **Data:** what the container reads and writes.
  - **Configuration:** settings and secrets passed in.
  - **Application code:** your own code.
  - **Dependencies:** third-party libraries and packages.
  - **Containers:** the image and runtime configuration.
  - **Host:** the node the container runs on.
  - **Build pipeline:** where images are created and could be tampered with.
- **The takeaway:** securing a container means securing all of these, which is what the rest of this deep dive walks through, layer by layer.

### The build pipeline

**Scenario.** An auditor asks the platform team to prove that a production image hasn't been tampered with, contains no known critical vulnerabilities, holds no hard-coded secrets and runs on a hardened node. The team needs controls at every stage from build to runtime.

![The build pipeline](diagrams/sections/d13-2.svg)

**What the picture shows.** Securing the container lifecycle, stage by stage.

- **Build:** a trusted, minimal base image to shrink the attack surface (Alpine Linux, distroless or `scratch`); immutable tags; multi-stage builds; and DockerSlim to minimize image size.
- **Test:** automated security testing in CI/CD, for example with Snyk.
- **Store:** sign images with Docker Content Trust or Cosign.
- **Encrypt:** keep secrets out of images, using Kubernetes Secrets with HashiCorp Vault, AWS KMS, AWS IAM and the Secrets Store CSI driver.
- **Scan:** scan images for vulnerabilities with Amazon Inspector or Trivy.
- **Deploy:** verify signatures with AWS Signer and enforce rules with policy-as-code (OPA Gatekeeper).
- **Monitor, the runtime environment:** run nodes in private subnets, disable SSH, block the instance metadata service (IMDS) from pods, encrypt all storage and use a hardened AMI.

**Going deeper.**

- Model files belong in the supply chain too.
- Classic PyTorch `.pt` files use Python pickle, which can execute arbitrary code when loaded.
- **Safetensors** stores only tensors and can't, which makes it the safer default for weights from outside the organization.
- Signing and scanning model images works the same way as for application images.

### Runtime security

**Scenario.** A security scan finds that several production pods run as root, with privileged mode and every Linux capability enabled, "because it was easier". Nobody would notice if one of them started behaving maliciously. The team needs runtime guardrails and detection.

![Runtime security](diagrams/sections/d13-3.svg)

**What the picture shows.** Runtime security: limit what a container can do, then watch what it does.

- **Least privilege:** give each container only what it needs for normal operation.
- **Restrict root and privileged access:** set this in the container's `securityContext`.
- **Drop all unnecessary Linux capabilities.**
- **Follow the Pod Security Standards:** enforce them with Pod Security Admission, and isolate workloads in namespaces with different levels (privileged, baseline, restricted).
- **Monitor the runtime:** the goal is to detect and prevent malicious activity inside containers and on worker nodes. Tools include Falco (deployed as a DaemonSet to watch containers) and Prisma Cloud container security.

### Network security

**Scenario.** A compromised pod in one namespace scans the whole cluster and reaches an internal database, because nothing blocks pod-to-pod traffic. The security team wants a zero-trust network, protection at the edges and visibility into what talks to what.

![Network security](diagrams/sections/d13-4.svg)

**What the picture shows.** Network security for the cluster, by branch.

- **Network segmentation and isolation:** namespaces together with NetworkPolicies, following zero-trust principles (deny by default, allow explicitly).
- **Securing pod-to-pod communication:** a service mesh (Istio or Linkerd) adds mutual TLS and identity between services.
- **Securing external connections:**
  - **Ingress and egress security, with a WAF** in front of public endpoints.
  - **OIDC** for authenticating users at the edge.
  - **DNS security:** DNS over TLS or HTTPS, and DNSSEC.
- **Network monitoring:** Cilium, Calico, Sysdig, service meshes or Datadog give visibility into cluster traffic, so a scan like the one in the scenario gets noticed.

### Secret management

**Scenario.** During a review, someone points out that anyone with read access to the cluster can decode the production database password: Kubernetes Secrets are only Base64-encoded. The password also hasn't changed in two years. The team needs a proper secret management plan.

![Secret management](diagrams/sections/d13-5.svg)

**What the picture shows.** Secret management, from the built-in object to external tools.

- **The Kubernetes Secret resource:** stores sensitive data **Base64-encoded** (not encrypted). Secrets are mounted into pods as environment variables or files, so applications get them without baking them into images.
- **Encrypting Secrets at rest:** Secrets are stored in etcd; enable etcd encryption at rest on EKS, and use **envelope encryption** (with KMS) to encrypt values before they're written.
- **Access control with RBAC:** apply least privilege so users, pods and services can read only the Secrets they need. This closes the scenario's gap.
- **External secret management tools:** many organizations use HashiCorp Vault or AWS Secrets Manager instead of plain Kubernetes Secrets.
- **Automating secret rotation:** rotating regularly limits the damage of a long-term compromise.
- **Protecting Secrets in transit:** encrypt every path a secret travels.
- **Security monitoring:** a central monitoring tool or SIEM helps spot suspicious access and respond quickly.

### Identity and model endpoints

**Scenario.** A new model-serving service needs to read model files from S3 and must expose an HTTPS endpoint to customers. The first version stored AWS access keys in an environment variable and served plain HTTP. Compliance requires short-lived credentials and encrypted, authenticated traffic.

![Identity and model endpoints](diagrams/sections/d13-6.svg)

**What the picture shows.** Data privacy and compliance: pod identity for AWS access, and securing the model endpoint.

- **IRSA (IAM Roles for Service Accounts):** associates a fine-grained IAM role with a Kubernetes service account. Pods assume the role through their service account identity and access AWS services with that role's permissions, with no stored keys.
- **EKS Pod Identity:** the evolution of IRSA that makes setting up IAM permissions for pods simpler. It requires the `eks-pod-identity-agent` DaemonSet.
- **Secure model endpoints:**
  - **Enforce TLS everywhere:** encrypt all traffic in transit and authenticate both clients and services.
  - **Certificates from ACM:** deploy them on the ALB, the NLB or the Ingress resource.
  - **Port 443:** annotate the Service resource to serve on 443.
  - **WAF:** attach AWS WAF to the ALB created by the Kubernetes Ingress.

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

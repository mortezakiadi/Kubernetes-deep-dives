# Alternatives: what Design 01 lists as not built, written down

Nothing in `platform/`, `k8s/`, `terraform/` or `tests/` changes because of this folder. Each alternative is separate, ships as
files you can read and run, and says how far it was checked. The write-up is section 10 of Appendix B.

`./check-all.sh` runs every check that works on one machine with Docker. Nothing here ran on EKS.

| Folder | Fills the gap | How far it was checked |
|---|---|---|
| `tool-gateway-dynamodb/` | Shared store for the tool gateway (approvals, rate counters), so it can run more than one replica | **Tested** against DynamoDB Local, including 25 threads racing to consume one approval. Terraform validated. The patch applies and compiles. |
| `audit-store/` | Append-only audit trail (S3 Object Lock, compliance mode) | Terraform validated. Not applied. |
| `boundary-monitoring/` | Watching the boundary (VPC flow logs and DNS query logs) instead of an agent inside the sandbox | Terraform validated. Not applied. |
| `image-trust/` | Signed images, a registry mirror, and an admission rule that enforces both | **Tested**: signing and verification with cosign against a local registry, including a wrong key and an unsigned image. Terraform validated. The Kyverno policy parses and was **not** run on a cluster. |
| `session-ca/` | A separate certificate authority per sandbox, limited by name constraints | **Tested** with the `cryptography` library and the OpenSSL command line, with positive controls. |
| `warm-pool-autoscaler/` | Sizing the warm pool from demand | The sizing rule is **tested**. The Manager patch applies and compiles. The KEDA objects parse and were **not** run. |
| `persistent-workspace/` | Disk that survives the pod, pause, resume, checkpoints | Unit-tested with a fake Kubernetes. Template checked against the pinned agent-sandbox schemas. Patch applies and compiles. **Not run on a cluster.** |
| `isolation-tiers/` | gVisor, Kata, GPU sandboxes, confidential mode | Templates checked against the pinned schemas, RuntimeClasses against the Kubernetes schemas. Node setup and confidential mode are **written from documentation and never run**. |
| `network-gaps/` | Narrower security groups, egress rules for platform pods, security groups per pod | Terraform validated. NetworkPolicies checked against the Kubernetes schemas. **Not run.** |
| `envoy-egress/` | An egress gateway on Envoy instead of mitmproxy | Run in a Docker lab, nine checks (section 9). |

Two helpers are shared. `validate-terraform.sh <folder>` runs `terraform init -backend=false` and `validate` in a throwaway copy.
`validate_agent_sandbox_crds.py <file>` checks agent-sandbox YAML against the CRD schemas of the pinned release, after verifying the
download against the SHA-256 that Terraform pins.

Two patches (`persistent-workspace/manager.patch` and `isolation-tiers/manager-templates.patch`) both add entries to the Manager's
table of templates. Each applies on its own. To use both, merge that one table by hand.

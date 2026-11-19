# Design 01 on Amazon EKS

A working, tested implementation of [Design 01: Sandboxing and secure execution for agents and untrusted code](../../content/designs/ds01-sandboxing.md),
built with Terraform on Amazon EKS Auto Mode.

The full write-up (components, EKS topology and instances, architecture, how it is built, and twelve tested
scenarios with commands, results and explanations) is the page **Appendix B** of the site, generated from
[`../../content/appendix/a02-design-01-on-eks.md`](../../content/appendix/a02-design-01-on-eks.md).

## Layout

| Folder | What is in it |
|---|---|
| `terraform/10-cluster` | VPC, subnets, NAT, security groups, IAM, EKS Auto Mode cluster |
| `terraform/20-platform` | Container image, secrets and certificates, controller, templates, pools, gateways, services |
| `k8s/` | The Kubernetes manifests Terraform applies |
| `platform/` | Python source for the platform services, and the Dockerfile |
| `tests/` | Scenario runner, harness, terminal-screenshot renderer |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `audit-query.py`, `setup-tools.sh`, `check-no-secrets.sh` |
| `docs/` | Generator for the write-up, the diagram generator, hand-written explanations |
| `evidence/final/` | Recorded output and screenshots from the test run in the write-up |

## Run it

Needs Terraform 1.9 or later, AWS CLI v2 signed in, Docker running, Python 3 with Pillow.

```text
export AWS_PROFILE=<your profile>
./scripts/up.sh                       # about 20 to 25 minutes
python3 tests/run_scenarios.py all    # twelve scenarios
./scripts/down.sh                     # removes everything, then checks that nothing is left
```

## Safety

This repository is public. Terraform state, plans, kubeconfig, local variable files and raw evidence are git-ignored,
`scripts/check-no-secrets.sh` blocks a commit that contains a credential, the account ID or the administrator's address
(a pre-commit hook runs it), and the test harness redacts those values before writing any file.

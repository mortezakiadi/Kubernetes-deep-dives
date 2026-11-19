# Design 02 on Amazon EKS

A working, scaled-down, tested implementation of [Design 02: Operating CPU and GPU clusters as one compute platform](../../content/designs/ds02-multicluster-compute.md),
built with Terraform on Amazon EKS Auto Mode: a hub, two CPU workers and a GPU worker, joined by Kueue and MultiKueue.

The full write-up (components, EKS topology and instances, architecture, how it is built, and ten tested scenarios with
commands, results and explanations) is **Appendix C** of the site, generated from
[`../../content/appendix/a03-design-02-multicluster-compute.md`](../../content/appendix/a03-design-02-multicluster-compute.md).

## Layout

| Folder | What is in it |
|---|---|
| `terraform/10-fleet` | VPC, subnets, NAT, IAM, and the four EKS Auto Mode clusters from one definition |
| `terraform/20-platform` | Kueue on every cluster (one module), the team table, queues, node pools, MultiKueue wiring, team identities |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `fleet-status.py`, `setup-tools.sh`, `check-no-secrets.sh` |
| `tests/` | Scenario runner, harness, terminal-screenshot renderer |
| `docs/` | Generator for the write-up, the diagram generator, hand-written explanations |
| `evidence/final/` | Recorded output and screenshots from the test run in the write-up |

## Run it

Needs Terraform 1.9 or later, AWS CLI v2 signed in, Helm 3, Python 3 with Pillow, and a quota of at least four vCPUs for G instances.

```text
export AWS_PROFILE=<your profile>
./scripts/up.sh                       # about 25 to 30 minutes
python3 tests/run_scenarios.py all    # ten scenarios
./scripts/down.sh                     # removes everything, then checks that nothing is left
```

## Safety

This repository is public. Terraform state, plans, kubeconfig, local variable files and raw evidence are git-ignored,
`scripts/check-no-secrets.sh` blocks a commit that contains a credential, the account ID or the administrator's address
(a pre-commit hook runs it), and the test harness redacts those values before writing any file.

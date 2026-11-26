# Design 10 on Amazon EKS: verification and reward quality

A working, scaled-down implementation of [Design 10](../../content/designs/ds10-verification.md): verifiers with one interface,
written contracts and abstention; one Deployment per pinned verifier version; code verification under gVisor; two judges of different
model families behind rule guards; gold sets with per-slice precision and recall; differential fuzzing and a regression suite; a
registry and a training gate; short GRPO runs with a reward-quality monitor; re-scoring on a verifier change; and a human review loop
whose reviewer labels are assumed inputs.

The write-up, with every command, its real output, screenshots and diagrams, is
[Appendix K](../../content/appendix/a11-design-10-verification.md). This file only says how to run it.

## What is here

| Folder | What it holds |
|---|---|
| `terraform/10-cluster` | VPC (three zones), NAT gateway, S3 endpoint, a standard EKS cluster, a system, a GPU and a gVisor sandbox node group. |
| `terraform/20-platform` | Namespaces, the device plugin, the gvisor RuntimeClass, the bucket, the store and its identity, ten verifier Deployments, two judges, the nightly fuzz CronJob and the network policies. |
| `datasets/` | The generator of the tasks and gold sets (fixed seed, content hashes) and a local self-test of the rule-based verifiers. |
| `workloads/` | The verifiers, the verifier service, quality and the gate, the fuzzer, the platform jobs, review support, the store and the RL run. |
| `launcher/` | `vq.py`: job, register, registry, rl, submit, fuzz-now. |
| `tests/` | The scenario runner, and `assumed_reviewers.py`, the assumed human inputs of S11. |
| `docs/` | The generator of the appendix, the hand-written prose, and the diagram generator. |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `check-no-secrets.sh`, `setup-tools.sh`. |
| `evidence/final/` | The recorded run the appendix is built from (redacted). |

## Run it

You need Terraform 1.9 or later, the AWS CLI, Helm 3, Python 3.12 with `boto3`, Pillow and SymPy, credentials for the AWS account you
want to use, and up to 24 vCPU of on-demand G and VT instance quota in us-east-2. Nothing in this folder contains an account ID, a
credential or an address: `up.sh` writes them into git-ignored files.

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # three GPU nodes; waits for every verifier and both judges
python3 tests/run_scenarios.py all           # about an hour
./scripts/down.sh --yes                      # removes everything, then checks nothing is left
```

## What is real and what is assumed

Real: the cluster, gVisor, the verifiers and their measured quality, the judges, the fuzzing, the gate, the RL runs and the monitor,
the review queues and everything computed from labels. Assumed: the two reviewers' labels and the owner's contract decisions, which are
written down in `tests/assumed_reviewers.py` and marked as assumed in every file they produce.

## Before you push anything

`scripts/check-no-secrets.sh --all` must print `OK`. This repository is public.

# Design 06 on Amazon EKS: a platform for post-training recipes

A working, scaled-down implementation of [Design 06](../../content/designs/ds06-post-training-recipes.md): recipes as versioned files,
a launcher that pins every reference and refuses malformed ablations, Kueue queues in which ablations borrow idle GPUs, SFT, GRPO and
distillation stages with TRL, an RL job whose vLLM rollout server, trainer and reward verifiers are separate components, a sandboxed
verifier pool, a health monitor with trend alerts, a tracking store, a model registry and evaluation gates.

The write-up, with every command, its real output, screenshots and diagrams, is
[Appendix G](../../content/appendix/a07-design-06-post-training.md). This file only says how to run it.

## What is here

| Folder | What it holds |
|---|---|
| `terraform/10-cluster` | VPC (three zones), NAT gateway, S3 endpoint, a standard EKS cluster, its add-ons, a CPU node group and a GPU node group on a pinned NVIDIA AMI. |
| `terraform/20-platform` | Namespaces, the NVIDIA device plugin, JobSet, Kueue and the queues, the bucket and the datasets, four workload identities, the code ConfigMap, the verifier pool (with NetworkPolicy and HPA) and the health monitor. |
| `datasets/` | The generator of the twelve synthetic datasets (fixed seed, content hashes). |
| `recipes/` | The recipe files of `assistant-v1`: SFT, filter, RL, evaluation, teacher traces, off- and on-policy distillation, and four ablations. |
| `launcher/` | `ptl.py`: hash, diff, launch, status, compare, gate, wait. |
| `workloads/` | The code every job runs: the stages, the reward verifier, the health monitor, the tracking store and registry helpers. |
| `tests/` | The scenario runner. It runs real commands, records their real output, redacts private values and draws terminal-style screenshots. |
| `docs/` | The generator of the appendix, the hand-written prose, and the diagram generator. |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `check-no-secrets.sh`, `setup-tools.sh`. |
| `evidence/final/` | The recorded run the appendix is built from (redacted). |

## Run it

You need Terraform 1.9 or later, the AWS CLI, Helm 3, Python 3.12 with `boto3`, `PyYAML` and `Pillow`, credentials for the AWS account
you want to use, and at least 16 vCPU of on-demand G and VT instance quota in us-east-2. Nothing in this folder contains an account ID,
a credential or an address: `up.sh` writes them into git-ignored files.

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 25 minutes; two GPU nodes
python3 tests/run_scenarios.py all           # about 2 hours; commit the recipes first
./scripts/down.sh --yes                      # removes everything, then checks nothing is left
```

## What is real and what is scaled down

Real: the cluster, the GPUs, Kueue, JobSet, vLLM, TRL's trainers, the verifier sandbox, the monitor, the stores and the gates. Scaled
down: a 0.5B student, a 3B teacher, two GPUs, datasets of a few hundred templated rows, runs of tens of steps.

## Cost

Two GPU nodes (`g6.2xlarge`, or `g6e.2xlarge` when EC2 has no L4 capacity), two `m6i.xlarge` nodes, the EKS control plane and a NAT
gateway: roughly 3 to 6 dollars an hour while it runs. Tear it down when the tests finish.

## Before you push anything

`scripts/check-no-secrets.sh --all` must print `OK`. This repository is public.

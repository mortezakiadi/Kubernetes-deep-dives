# Design 08 on Amazon EKS: a platform for long-horizon agent training

A working, scaled-down implementation of [Design 08](../../content/designs/ds08-long-horizon.md): agent environments admitted by
Kueue, a vLLM rollout engine with per-token log-probabilities, a trajectory store that records every turn with its context and snapshot,
a verifier that judges each attempt in a fresh copy with hidden tests and layered checks, a credit worker that branches from snapshots
and computes turn-level values, potential-based reward shaping, a reverse curriculum, and a trainer job that checks what one
policy-gradient step does to each turn.

The write-up, with every command, its real output, screenshots and diagrams, is
[Appendix I](../../content/appendix/a09-design-08-long-horizon.md). This file only says how to run it.

## What is here

| Folder | What it holds |
|---|---|
| `terraform/10-cluster` | VPC (three zones), NAT gateway, S3 endpoint, a standard EKS cluster, its add-ons (including the EBS CSI driver and the snapshot controller), a CPU node group and a GPU node group on a pinned NVIDIA AMI. |
| `terraform/20-platform` | Namespaces, the NVIDIA device plugin, Kueue and the two queues, the storage and snapshot classes, the bucket and the public tasks, the trajectory store's identity, the code and task-bank ConfigMaps, the rollout engine, the store, the verifier and the network policies. |
| `tasks/` | The task bank: 32 small Python repositories with one to three planted bugs, and the checks that every task is well formed. |
| `recipes/` | `curriculum.json`, the reverse curriculum's stages and promotion thresholds. |
| `launcher/` | `lh.py`: rollout, scripted, replay, credit, trainer, stats, curriculum. |
| `workloads/` | The code the pods run: environment and agent, sandbox, trajectory store, verifier, credit worker, trainer, metrics, CSI snapshot check. |
| `tests/` | The scenario runner. It runs real commands, records their real output, redacts private values and draws terminal-style screenshots. |
| `docs/` | The generator of the appendix, the hand-written prose, and the diagram generator. |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `check-no-secrets.sh`, `setup-tools.sh`. |
| `evidence/final/` | The recorded run the appendix is built from (redacted). |

## Run it

You need Terraform 1.9 or later, the AWS CLI, Helm 3, Python 3.12 with `boto3`, `PyYAML` and `Pillow`, credentials for the AWS account
you want to use, and up to 16 vCPU of on-demand G and VT instance quota in us-east-2. Nothing in this folder contains an account ID,
a credential or an address: `up.sh` writes them into git-ignored files.

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # two GPU nodes; waits until the rollout engine has loaded the model
python3 tests/run_scenarios.py all           # about 2 hours
./scripts/down.sh --yes                      # removes everything, then checks nothing is left
```

## What is real and what is scaled down

Real: the cluster, the GPUs, Kueue, vLLM, the 7B policy, the EBS snapshots, the environments, the verifier, the branches and the
credit. Scaled down: 32 small repositories, at most 16 turns per episode, one policy version, and no training loop (the trainer measures
one step's effect instead of applying it).

## Cost

Two one-GPU nodes, two `m6i.2xlarge` nodes, the EKS control plane and a NAT gateway run for as long as the build exists. Tear it down
when the tests finish.

## Before you push anything

`scripts/check-no-secrets.sh --all` must print `OK`. This repository is public.

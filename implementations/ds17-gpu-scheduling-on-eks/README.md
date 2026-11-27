# Design 17 on Amazon EKS: scheduling that keeps accelerators busy

A working, scaled-down implementation of [Design 17](../../content/designs/ds17-gpu-scheduling.md): Kueue for admission (quotas in
cohorts, borrowing and lending, fair sharing, priorities, the pods-ready timeout, topology-aware admission), a second kube-scheduler
with a bin-packing and a spreading GPU profile, a GPU-hour accounting service, two real GPUs measured with DCGM, and a simulated fleet
of 16 KWOK nodes with 8 GPUs each on which the scheduling behaviour is tested.

The write-up, with every command, its real output, screenshots and diagrams, is
[Appendix R](../../content/appendix/a18-design-17-gpu-scheduling.md). This file only says how to run it.

## What is here

| Folder | What it holds |
|---|---|
| `terraform/10-cluster` | VPC, NAT gateway, S3 endpoint, a standard EKS cluster, a system node group and a two-node real-GPU node group. |
| `terraform/20-platform` | Namespaces, device plugin, DCGM exporter, KWOK, Kueue and its objects, the second scheduler, the accounting service. |
| `platform/` | The vendored KWOK manifests (and one stage written here), and the generator of the simulated fleet. |
| `workloads/` | The accounting service. |
| `tests/` | The scenario runner. It runs real commands, records their real output, redacts private values and draws screenshots. |
| `docs/` | The generator of the appendix, the hand-written prose, and the diagram generator. |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `check-no-secrets.sh`, `setup-tools.sh`. |
| `evidence/final/` | The recorded run the appendix is built from (redacted). |

## Run it

You need Terraform 1.9 or later, the AWS CLI, Helm 3, Python 3 with Pillow and PyYAML, and up to 16 vCPU of on-demand G instance quota
in us-east-2. Nothing in this folder contains an account ID, a credential or an address: `up.sh` writes them into git-ignored files.

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 25 minutes
python3 tests/run_scenarios.py all           # about 50 minutes
./scripts/down.sh --yes                      # removes everything, then checks nothing is left
```

## What is real and what is simulated

Real: the cluster, Kueue, kube-scheduler, the API objects they decide on, the two GPUs and DCGM. Simulated: 128 GPUs on 16 KWOK nodes
that no machine stands behind; their pods never run, and their activity is declared by each pod.

## Before you push anything

`scripts/check-no-secrets.sh --all` must print `OK`. This repository is public.

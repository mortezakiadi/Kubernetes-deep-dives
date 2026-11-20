# Design 03 on Amazon EKS: training and inference infrastructure

A working, scaled-down implementation of [Design 03](../../content/designs/ds03-training-inference.md): one EKS cluster that runs
the three workload families of the design (research notebooks, distributed training, model serving) with one golden path each, one
GPU pool and one queue per family, one set of metrics, and a way to share idle GPUs between them.

The write-up, with every command, its real output, screenshots and diagrams, is
[Appendix D](../../content/appendix/a04-design-03-training-inference.md). This file only says how to run it.

## What is here

| Folder | What it holds |
|---|---|
| `terraform/10-cluster` | VPC (five zones), NAT gateway, one EKS Auto Mode cluster. |
| `terraform/20-platform` | S3 bucket and per-family identities, Kueue, Kubeflow Trainer (with JobSet), LeaderWorkerSet, KEDA, Prometheus, the DCGM exporter, the inference gateway and endpoint picker, GPU node pools and queues, the vLLM Deployment, the platform's training runtime. |
| `golden-paths/` | The manifests a user writes: notebook, TrainJob, batch evaluation, two-node model replica. |
| `workloads/` | The code the platform ships to workloads as ConfigMaps: the training script, the model registry tool, the evaluation job, the load generator, the idle culler. |
| `tests/` | The scenario runner. It runs real commands, records their real output, redacts private values and draws terminal-style screenshots. |
| `docs/` | The generator for the appendix, the hand-written prose, and the diagram generator. |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `install-crds.sh`, `check-no-secrets.sh`. |

## Run it

You need Terraform 1.9 or later, the AWS CLI, Helm 3, Python 3.12 with `boto3` and `Pillow`, and credentials for the AWS account you
want to use. Nothing in this folder contains an account ID, a credential or an address: `up.sh` writes them into git-ignored files.

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 35 minutes; builds everything
python3 tests/run_scenarios.py all           # about 2 to 3 hours; real GPU nodes are bought and removed
./scripts/down.sh --yes                      # removes everything, then checks nothing is left
```

## Cost and capacity

The cluster, a NAT gateway and two small CPU nodes cost a few dollars a day at rest. GPU nodes (one GPU each, `g4dn`, `g5`, `g6` or
`g6e`, whichever has capacity) exist only while work needs them, and the pools are limited to six GPUs in total. GPU capacity is not
guaranteed in any zone: the build uses five zones and a wide list of instance types, and the write-up records what happened.

## Before you push anything

`scripts/check-no-secrets.sh --all` must print `OK`. This repository is public.

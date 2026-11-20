# Design 04 on Amazon EKS: state management for long-running agents

A working, scaled-down implementation of [Design 04](../../content/designs/ds04-long-running-agents.md): a coding agent whose run is a
durable workflow. A worker, a sandbox, a node, the model provider, the engine's database or a deployment can fail, and the run
continues from the last finished step without calling the model again for steps that are done, and without repeating a side effect.

The write-up, with every command, its real output, screenshots and diagrams, is
[Appendix E](../../content/appendix/a05-design-04-agent-state.md). This file only says how to run it.

## What is here

| Folder | What it holds |
|---|---|
| `terraform/10-cluster` | VPC (three zones), NAT gateway, one EKS Auto Mode cluster, a Multi-AZ RDS PostgreSQL instance, an ECR repository. |
| `terraform/20-platform` | Namespaces, the S3 bucket and sandbox identity, Temporal, Prometheus, KEDA, and the Deployments of the platform: model gateway, tool gateway, sandbox manager, session API, the stand-in Git host, the two worker versions, the autoscaler. |
| `app/` | The one container image: the workflow, the activities, the gateways, the sandbox manager and executor, the session API, the stand-in model and Git host, and the command line the tests use. |
| `tests/` | The scenario runner. It runs real commands, records their real output, redacts private values and draws terminal-style screenshots. |
| `docs/` | The generator for the appendix, the hand-written prose, and the diagram generator. |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `build-image.sh`, `redeploy.sh`, `check-no-secrets.sh`, `setup-tools.sh`. |
| `evidence/final/` | The recorded run the appendix is built from (redacted). |

## Run it

You need Terraform 1.9 or later, the AWS CLI, Helm 3, Docker with `buildx`, Python 3.12 with `boto3` and `Pillow`, and credentials for
the AWS account you want to use. Nothing in this folder contains an account ID, a credential or an address: `up.sh` writes them into
git-ignored files.

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 35 minutes; builds everything, including the image
python3 tests/run_scenarios.py all           # builds the evidence; fails the database over, runs 24 runs at once
./scripts/down.sh --yes                      # removes everything, then checks nothing is left
```

## What is real and what is a stand-in

Real: the durable execution engine (Temporal), its Multi-AZ PostgreSQL database, the workers, the gateways, the ledger, the
sandbox pods, the S3 snapshots, the autoscaler and the metrics. Stand-ins, chosen so every failure test is exact and repeatable: the
model (a scripted function of its prompt) and the Git host (a small service that can lose its answer after acting).

## Cost

The cluster, a NAT gateway, a Multi-AZ `db.t4g.small` database and a few small CPU nodes cost a few dollars a day. There are no GPUs.

## Before you push anything

`scripts/check-no-secrets.sh --all` must print `OK`. This repository is public.

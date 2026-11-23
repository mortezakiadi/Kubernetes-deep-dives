# Design 07 on EKS: scaling RL beyond the small-run regime

A working, scaled-down build of [Design 07](../../content/designs/ds07-scaling-rl.md): an asynchronous GRPO trainer and a disaggregated rollout
fleet on EKS, with the measurements the design asks for at every rung of its scaling ladder. The write-up, with the commands, outputs and
screenshots of every test, is [Appendix H](../../content/appendix/a08-design-07-scaling-rl.md).

**What is built.** One cluster in one zone: a trainer GPU (g5.2xlarge, NVIDIA A10G 24 GB), two rollout GPUs on two nodes (the same type, one vLLM
engine each behind one address), CPU pools for the controllers and for a sandbox pool that KEDA scales on its queue. A 0.6B-parameter model
(Qwen3-0.6B) is trained on GSM8K for 20 to 80 steps per run. Weights travel from the trainer to the engines over NCCL, packed, once per step.
Checkpoints are written to the node and copied to a bucket. A JobSet restarts the trainer and a watchdog turns a hang into a failure.

**What is not.** The design's 235B mixture-of-experts model, 64K-token answers, thousands of GPUs, a GPU fabric between nodes, routing replay,
a second region. See the end of the appendix for the full list.

## Layout

```
scripts/        up.sh  down.sh  verify-clean.sh  update-my-ip.sh  setup-tools.sh  check-no-secrets.sh
terraform/
  10-cluster/   VPC, EKS 1.37, node pools (system, env, trainer, rollout), state bucket, pod identity
  20-platform/  NVIDIA device plugin, JobSet, LeaderWorkerSet, KEDA, Redis
workloads/      train.py (the trainer), manifests.py (every manifest the tests apply), sandbox_worker.py,
                sync_bench.py (weight-push benchmark), mismatch_probe.py (train-vs-inference mismatch)
recipes/ladder/ the ladder as overlays: one axis per rung
tests/          run_scenarios.py (S01..S10), helpers, drift.py (composite alerts), ladder.py (the gate), screenshots
docs/           the appendix's text, diagrams and the script that builds the page from the evidence
```

## Run it

Prerequisites: Terraform, the AWS CLI, Helm, Python 3 with `matplotlib`, `scipy`, `numpy` and `pyyaml`, an AWS profile for an account where you
may create an EKS cluster and GPU instances (about $6 an hour while it runs).

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 25 minutes
python3 tests/run_scenarios.py all           # several hours: it trains, breaks and measures
./scripts/down.sh --yes && ./scripts/verify-clean.sh
```

Nothing private is committed: the account ID and your address are written to git-ignored `local.auto.tfvars` files by `scripts/update-my-ip.sh`,
and `scripts/check-no-secrets.sh --all` checks the whole tree before a commit.

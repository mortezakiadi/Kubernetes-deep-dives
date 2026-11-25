# Design 09 on EKS: off-policy and asynchronous training
A working, scaled-down build of [Design 09](../../content/designs/ds09-off-policy-async.md): a GRPO run whose data is off-policy on purpose, with the
design's three policies, its decoupled loss, its off-policy metrics, bounded staleness, replay and external pools, a rules-driven controller with
rollback, and the sweep harness that turns the questions into curves. The write-up, with the commands, outputs and charts of every test, is
[Appendix J](../../content/appendix/a10-design-09-off-policy.md).

**What is built.** One standard EKS cluster with a pool of single-GPU nodes (L40S 48 GB first, with A10G and L4 types as fallbacks), Kueue as the research
queue, and one pod per experiment: TRL's `GRPOTrainer` with a colocated vLLM engine, a 0.6B model (Qwen3-0.6B) on GSM8K, runs of 48 to 100 steps. The
design's loss, metrics, limits, pools and controller are in `workloads/`; sweeps are Indexed Jobs admitted by Kueue at low priority.

**What is not.** The design's agents, long answers, mixture-of-experts model, thousands of GPUs, a separate log-probability worker, background
(truly concurrent) generation, partial rollouts, teacher models, and distance-adaptive staleness. Staleness here is produced by buffering: the engine
generates K optimizer steps' worth of samples at once and the trainer uses them over the next K steps, so the age of a sample, and the gap between the
policy that wrote it and the one that learns from it, are real; the engine and the trainer do not run at the same time. See the end of the appendix.

## Layout
```
scripts/        up.sh  down.sh  verify-clean.sh  update-my-ip.sh  setup-tools.sh  check-no-secrets.sh
terraform/
  10-cluster/   VPC, EKS 1.37, node pools (system, research), state bucket, pod identity
  20-platform/  NVIDIA device plugin, Kueue
workloads/      run.py (one experiment), offpolicy.py (loss, behavior weights, metrics), controller.py (rules), mixing.py (pools), store.py, manifests.py
recipes/        the controller's rules (full, protection only) and the replay share's ESS floor
tests/          run_scenarios.py (S01..S10), scenarios_a.py, scenarios_b.py, helpers.py, harness.py, terminal screenshots
docs/           the appendix's text, diagrams and the script that builds the page from the evidence
```

## Run it
Prerequisites: Terraform, the AWS CLI, Helm, Python 3 with `matplotlib`, `scipy`, `numpy`, `pyyaml` and `pillow`, an AWS profile for an account where you may create
an EKS cluster and GPU instances (the pool costs about $2.2 an hour per L40S node, up to four nodes).
```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 20 minutes
python3 tests/run_scenarios.py all           # a few hours: it trains, breaks and measures
./scripts/down.sh --yes && ./scripts/verify-clean.sh
```
The GPU pool starts with one node; the tests scale it to four for the sweeps (`aws eks update-nodegroup-config`). On-demand GPU capacity is volatile: if a
type is unavailable the pool tries the next, and the sweeps run with however many nodes arrive.

Nothing private is committed: the account ID and your address are written to git-ignored `local.auto.tfvars` files by `scripts/update-my-ip.sh`, and
`scripts/check-no-secrets.sh --all` checks the whole tree before a commit.

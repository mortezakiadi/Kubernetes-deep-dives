# Design 11 on EKS: multi-task post-training across task families
A working, scaled-down build of [Design 11](../../content/designs/ds11-multi-task.md): GRPO post-training of a small model on a mixture of task families, with the
capability matrix, a searched mixture, three orders of training, a batch balanced by tokens, a gradient probe for interference, specialists merged by task
arithmetic, a no-regression release gate, a domain registry with an admission test, and a pipeline that chains the steps. The write-up, with the commands,
outputs and charts of every test, is [Appendix L](../../content/appendix/a12-design-11-multi-task.md).

**What is built.** One standard EKS cluster with a pool of single-GPU nodes (L40S 48 GB first, with L4 and A10G types as fallbacks), Kueue as the research
queue, Argo Workflows as the pipeline engine, and one pod per experiment: TRL's `GRPOTrainer` with a colocated vLLM engine and a 0.6B model (Qwen3-0.6B). Six
task families (maths, code, instruction following, tool calls, multilingual consistency, safety) each have a generator, a programmatic verifier and a
held-out set; a seventh (sorting) is evaluated in every run and admitted in S09.

**What is not.** The design's chat family (it needs a judge), long agentic tasks, multi-teacher distillation, gradient surgery and adapters as conflict reducers,
and anything at the size of a real assistant. See the end of the appendix.

## Layout
```
scripts/        up.sh  down.sh  verify-clean.sh  update-my-ip.sh  setup-tools.sh  check-no-secrets.sh
terraform/
  10-cluster/   VPC, EKS 1.37, node pools (system, research), state bucket, pod identity
  20-platform/  NVIDIA device plugin, Kueue, Argo Workflows
workloads/      run.py (train, eval, probe, merge, verify), families.py (generators and verifiers), mixture.py (matrix, response model, search, gate), store.py, manifests.py
recipes/        the domain registry and the gate
tests/          run_scenarios.py (S01..S10), scenarios_a.py, scenarios_b.py, helpers.py, harness.py, test_families.py, test_mixture.py
docs/           the appendix's text, diagrams and the script that builds the page from the evidence
```

## Run it
Prerequisites: Terraform, the AWS CLI, Helm, Python 3 with `matplotlib`, `scipy`, `numpy`, `pyyaml` and `pillow`, an AWS profile for an account where you may
create an EKS cluster and GPU instances (the pool costs about $2.2 an hour per L40S node, up to four nodes).
```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 25 minutes; the GPU pool starts empty
python3 tests/run_scenarios.py all           # several hours: it trains, merges, probes and gates
./scripts/down.sh --yes && ./scripts/verify-clean.sh
```
The GPU pool is created empty (a pool whose first node cannot be launched fails to create); the tests scale it up. On-demand GPU capacity is volatile: if a type
is unavailable the pool tries the next. `python3 tests/test_families.py` and `python3 tests/test_mixture.py` need no cluster.

Nothing private is committed: the account ID and your address are written to git-ignored `local.auto.tfvars` files by `scripts/update-my-ip.sh`, and
`scripts/check-no-secrets.sh --all` checks the whole tree before a commit.

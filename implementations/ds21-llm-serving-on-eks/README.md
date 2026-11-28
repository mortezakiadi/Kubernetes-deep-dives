# Design 21 on EKS: deploying and tuning high-throughput LLM serving

A working, scaled-down build of [Design 21](../../content/designs/ds21-llm-serving-tuning.md): vLLM serving two products (a chat product and a code product with long
shared prefixes) on a four-GPU node, measured by goodput per GPU against first-token and inter-token targets. It covers the design's levers one by one: tensor
parallelism of a dense model, expert parallelism of a mixture-of-experts model, engine settings, prefill/decode disaggregation through NIXL, prefix-aware
routing, a host-memory KV tier, quantization behind an accuracy gate, the tuned stack against the defaults with repeats, and autoscaling on two signals. The
write-up, with the commands, outputs and charts of every test, is [Appendix V](../../content/appendix/a22-design-21-llm-serving.md).

**What is built.** One standard EKS cluster (Kubernetes 1.37): two system nodes, one CPU node for the load generators and the router, and a pool with one
`g4dn.12xlarge` node (four NVIDIA T4, 16 GB each, PCIe only). KEDA, Prometheus and the NVIDIA GPU exporter run on the system nodes. Servers are vLLM 0.33.0;
the dense model is Qwen3-4B (fp16) and the mixture-of-experts model is Qwen3-30B-A3B (int4 GPTQ).

**What is not.** Larger GPUs (every four-GPU type except the T4 was refused for capacity in three regions during the build), FP8 and FP4 (the T4 has neither),
multi-node expert parallelism, the Gateway API Inference Extension (a small router of mine implements the same two policies and is labelled as such), KServe
canary rollouts, and growing the number of GPU nodes. See the end of the appendix.

## Layout

```
scripts/        up.sh  down.sh  verify-clean.sh  update-my-ip.sh  setup-tools.sh  check-no-secrets.sh
terraform/
  10-cluster/   VPC, EKS 1.37, node pools (system, load, serving), state bucket, pod identity
  20-platform/  NVIDIA device plugin, KEDA, Prometheus, the GPU exporter
workloads/      traffic.py (the trace and the goodput metric), loadgen.py (open-loop client), router.py (round robin, least loaded, prefix, prefill/decode),
                quality.py (the accuracy gate's questions), manifests.py, store.py
tests/          run_scenarios.py (S01..S10), scenarios_a.py, scenarios_b.py, helpers.py, common.py, harness.py, probe_*.py
docs/           the appendix's text, diagrams and the script that builds the page from the evidence
evidence/final/ the commands, outputs, screenshots and charts of the run the appendix describes
```

## Run it

Prerequisites: Terraform, the AWS CLI, Helm, Python 3 with `matplotlib`, `numpy`, `pyyaml` and `pillow`, and an AWS profile for an account where you may create an
EKS cluster and GPU instances (the four-GPU node costs about $3.9 an hour).

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about 25 minutes; the GPU pool starts empty
python3 tests/run_scenarios.py all           # several hours: it serves, loads, scales and gates
./scripts/down.sh --yes && ./scripts/verify-clean.sh
```

The GPU pool is created empty (a pool whose first node cannot be launched fails to create) and the tests scale it up. On-demand GPU capacity is volatile: if
`g4dn.12xlarge` is refused, set `serving_instance_types` in `terraform/10-cluster/variables.tf` to a type that EC2 will give you; the scenarios read the
instance type back and every number in the appendix is for the type the build got.

Nothing private is committed: the account ID and your address are written to git-ignored `local.auto.tfvars` files by `scripts/update-my-ip.sh`, and
`scripts/check-no-secrets.sh --all` checks the whole tree before a commit.

### What I did not build

The design describes a platform at a scale this account cannot rent. Each row names what is missing and why the measurements above still bear on it.

| Not built | What I did instead, and what that cannot tell you |
|---|---|
| **A 235B mixture-of-experts model, 64K-token answers, 5,000 steps, 2,048 GPUs** | A 0.6B dense model, answers up to 3,072 tokens, 20 to 80 steps, three GPUs. Every scaling claim in the appendix is about the shape of a measurement, and the one extrapolation (S05's 235B push) is arithmetic that is labelled as such. |
| **Rungs 3 and 4 of the ladder** | Rung 3's overlay (a model three times larger) is in the repository and its axis rule is tested, but the 24 GB trainer GPU does not hold a 1.7B model with its optimizer, so it was not trained. Rung 4 (many steps at the target size) is out of reach; S08 and S09 show what it would expose, on a small model. |
| **Two layouts: a training layout and a rollout layout, with resharding** | The trainer holds the whole model on one GPU and each engine holds the whole model on one GPU. S05 times the push; it does not time a resharding from tensor, pipeline and expert parallelism into a different layout. |
| **MoE routing replay** | The model is dense. S06 shows the precision axis of the mismatch; the routing axis, which the design says matters most for mixture-of-experts models, is not measured. |
| **A GPU fabric between nodes (EFA, NVLink)** | The nodes share the VPC's 10 Gbit/s network. That is why the weight push is half of every step in S03, and S05 prices what that means for a large model. |
| **Active partial rollouts and length-aware scheduling** | The trainer's own asynchrony and staleness bound, and a queue sized to the trainer (S04). Cutting off the slowest answers and resuming them under the newer policy is not in the library I used. |
| **Sparse or delta weight updates** | The design lists them as an open question. vLLM has a sparse NCCL backend; I did not test it. |
| **Multi-turn tool use with an agent** | The environment tier is exercised by a chain of calls per answer scored after generation, not by a model that calls tools turn by turn. S07 measures the queue and the scaling, not an agent's behaviour. |
| **A hardened sandbox** | The sandbox workers run a few lines of arithmetic in a child process with limits. Design 01 builds the real thing. |
| **Hot spare nodes, health checks before admission, fast parallel checkpoint loading** | A failed trainer node is replaced by the node group, and the checkpoint is loaded from the bucket. S09 measures what that costs; the design's remedies for it are not built. |
| **Saving the rollout buffer with the checkpoint** | The library saves the prompt cursor with the checkpoint, so a restart does not repeat or skip prompts, but samples that were generated and not yet trained on are lost. |
| **A nightly schedule in use** | The CronJob exists and S10 starts it by hand; it has not run on its schedule. |

### What to look at in the AWS Console

If you want your own screenshots to put beside the ones above:

- **EKS, the cluster's Compute tab:** the four node groups and their instance types.
- **EC2, Instances:** the three `g5.2xlarge` GPU instances in one availability zone.
- **CloudWatch or the instance monitoring tab:** GPU instances' network in and out during a weight push (S05).
- **S3, the state bucket:** `runs/<run>/` with the metrics file and the `checkpoint-N/` folders, each with its `COMPLETE` marker.
- **EC2, Auto Scaling groups:** the trainer's group replacing its instance during the node drill of S09.

### Reproducing it, and taking it down

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                         # about 25 minutes
python3 tests/run_scenarios.py all      # several hours: it trains, breaks and measures
./scripts/down.sh --yes                 # and then ./scripts/verify-clean.sh must print CLEAN
```

Everything is tagged with the project name. `down.sh` deletes the workloads, then the platform layer, then the cluster layer and the leftover log group, and `verify-clean.sh` reports anything of the project that remains in the region, including launch templates, volumes and the state bucket. The cluster costs about $6 an hour while it is up.

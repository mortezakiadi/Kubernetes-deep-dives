# Design 07 on EKS: plan

Scale of what is built: one trainer GPU, two rollout GPUs on two nodes, a 0.6B-parameter model (Qwen3-0.6B), GSM8K problems, runs of 40 to 200 steps.
The design's numbers (235B mixture-of-experts, 64K-token answers, thousands of GPUs) are not reachable here. What is reachable is the architecture:
a disaggregated rollout fleet and trainer joined by NCCL weight transfer, an asynchronous trainer with a staleness bound, a queue-driven environment
tier, checkpoints that survive the node, a restart policy, and the measurements the design says to take at every rung of the ladder.

Substrate: standard EKS 1.37, one zone for every GPU node (g5.2xlarge, NVIDIA A10G 24 GB), CPU pools for controllers and the sandbox workers.
Software: vLLM 0.33.0 (rollout engines, NCCL weight transfer), TRL 1.18.2 AsyncGRPOTrainer (the asynchronous trainer), JobSet, LeaderWorkerSet, KEDA.

Scenarios (each runs real commands and decides pass or fail from their output):
- S01 platform and fabric
- S02 rung 1: recipe search and compute-performance curves
- S03 step-time breakdown, in GPU-seconds
- S04 long tail and the rollout queue
- S05 weight sync by model size and fleet size
- S06 train-inference mismatch
- S07 environment tier and its autoscaler
- S08 slow drifts, composite alerts, rollback with a change
- S09 failures and recovery, goodput
- S10 the ladder's exit criteria

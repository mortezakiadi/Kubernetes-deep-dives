### What I did not build, and what the results do and do not say

**Not built.**

- **A bigger GPU.** Every H100, L40S, L4 and A10G four-GPU type was refused for capacity. Everything here is a T4: 16 GB, 320 GB/s, no NVLink, no FP8, no bf16, FlashAttention unavailable. A faster GPU shortens the prompt step that dominates these results, so it would move every threshold; it would not change what each scenario measures.
- **FP8 and FP4.** The gate and the procedure of S08 are the same for those formats; the hardware for them is not here. What S08 shows is int4 weights (AWQ) and an int8 KV cache.
- **Expert load balancing's effect, DeepEP, and multi-node wide expert parallelism.** The engine refuses expert load balancing for the int4 checkpoint that fits four T4s (S03 shows the refusal), DeepEP needs NVLink or RDMA, and there is one node.
- **The Gateway API Inference Extension.** A short router of mine implements the same two policies (least loaded and prefix aware) and prefill/decode; the tests measure the policies, not that component.
- **KServe canary rollouts and node autoscaling.** The autoscaler scales replicas on a fixed set of four GPUs; the GPU node group never grew beyond one node.
- **Real traffic and real quality.** The prompts are random words, the answers are not judged for quality (S08's gate uses exact-answer arithmetic), and the arrivals are Poisson with no daily shape.
- **Cost.** The tests report goodput per GPU; I did not turn it into dollars per million tokens.

**What the results say, and how far to trust them.**

- The mechanism results are measurements of counters and logs and are stable: the KV capacity follows from the model's shape (S01), sharding weights frees memory for the cache (S02), expert parallelism places 32 of 128 experts on each rank (S03), a smaller prefill chunk lowers inter-token latency (S04), the connector moves the KV cache and the decode servers use it (S05), a prefix-aware router doubles the cache hit rate (S06), a host tier serves a quarter to two fifths of the prompt tokens (S07), and a quantized configuration is admitted or refused by a rule written before the numbers (S08).
- The size of each effect is for four T4s and these two products. The tensor-parallel group that won in S02 won because a T4's prompt step is long; on a faster GPU four copies would probably win. The prefill/decode split lost in S05 because the transfer took about 0.8 seconds on a PCIe path through host memory; with a direct GPU-to-GPU path it would cost far less.
- Single-GPU runs (S01, S04, S07, S08) and four-GPU fleets behind a router (S02, S05, S06, S09) are not comparable with each other: a fleet sees four times the arrivals and shares its bursts, so its capacity per GPU is higher than a single server's at the same load per GPU. Comparisons inside one scenario are fair; across scenarios only the shape carries over.
- The run-to-run noise was small where it was measured (the control of S04 differed from the default by 0.02 requests per second per GPU, and the three seeds of S09 had ranges of 0.01 and 0.12), but the grid of load levels is coarse (0.3 requests per second per GPU), which is why capacity is interpolated and why several engine settings read as 'no change beyond the noise'.
- Three seeds give a range, not a confidence interval.

**Answers to the design's questions, as far as this build can give them.**

- **Does tuning buy 40% more goodput per GPU?** Here it bought about 30% more capacity per GPU, and nearly all of it came from one change (prefix-aware routing). Most of the folklore settings did nothing beyond the noise on this traffic and this GPU; the one quantized configuration that helped a great deal failed its gate. The design's claim is about the right size on a T4 and I cannot say what it is on an H100.
- **Tensor parallel or replicas for a model that fits on one GPU?** On a T4, the wider group (S02), for a reason specific to the T4. The method (predict the memory, measure goodput on the same traffic) is what to reuse.
- **Expert parallelism for a mixture of experts on PCIe?** Four engines with expert parallelism had about three times the capacity of tensor parallelism at near-idle to moderate load (S03), because they do not all-reduce every layer across a PCIe fabric.
- **Is prefill/decode disaggregation worth it?** Not on this node (S05): it smoothed inter-token latency and cost more in first-token time and in idle GPUs than it saved.
- **Does an accuracy gate matter?** Yes: the faster int4 configuration lost no accuracy on average and still changed one answer in nine, and the gate refused it for that (S08).
- **Which signal should an autoscaler use?** Neither won cleanly (S10). The queue-like signal (requests in the system) reacted in 20 seconds but could not avoid the two-minute start-up; the utilisation signal had scaled up in advance, held the targets, and used 28% more replica-seconds. What the run supports is that start-up time, not the signal, is the floor, and that the signal should be chosen against the cost of an idle replica.

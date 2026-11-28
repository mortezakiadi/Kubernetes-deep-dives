---
id: appendix-design-21-eks
title: "Appendix V: Design 21 built and tested on AWS (EKS, vLLM, four T4 GPUs)"
short: "V. Design 21 on AWS (built and tested)"
---

This appendix describes a working, scaled-down implementation of [Design 21: Deploying and tuning high-throughput LLM serving](#design-llm-serving). I built it with Terraform in one AWS account and one region: a standard EKS cluster with two system nodes, one CPU node for the load generators and the router, and one GPU node with four NVIDIA T4 GPUs. vLLM 0.33.0 serves a 4-billion-parameter dense model and a 30-billion-parameter mixture-of-experts model; Prometheus and KEDA watch and scale it; a load generator replays two kinds of traffic and records, for every request, the time to the first token and the gap between the following tokens.

The order is the one a reader needs: first the context, then the software I used, the topology and instances, and the architecture, then how it is implemented, and then the scenarios I tested, with the commands I ran, what they printed, why the result can be trusted and why you get that output.

**What "scaled down" means here, and the price of it.** The design's examples are 14B and 70B models on H100s with 20,000-token prompts. I asked EC2 for every four-GPU type I could use (L40S, L4, A10G, in three regions) and was refused for capacity for hours on all of them; the one four-GPU type it would launch was `g4dn.12xlarge`, four T4s with 16 GB each, no NVLink, no FP8 and no bf16. So the models are smaller (4B dense, 30B mixture of experts in int4), the prompts are shorter (chat about 300 tokens, code about 1,300 with a shared 1,200-token repository prefix), and the targets are loosened to what a T4 can meet (first token in 2 s for chat and 5 s for code, a gap between tokens of 150 ms and 200 ms). Every comparison inside this appendix is fair, because the traffic and the targets are the same for everything that is compared. The absolute numbers are not the numbers of an H100: the T4's compute is so low that the work of reading a prompt, which on a large GPU is a small step, is the thing that decides almost every result here. Where that makes a result specific to this GPU, the scenario says so.

**Why this substrate.** The design's levers (how to split a model over GPUs, which engine settings to change, whether to separate prompt work from token work, how to route, whether to offload cache to host memory, whether to quantize, how to scale) are all decisions about one node's GPUs and the traffic in front of them. One node with four GPUs on one PCIe fabric is the smallest thing on which all of them can be exercised for real, and the one node EC2 would give me. Multi-node expert parallelism, which needs a network between nodes, is not built; the appendix ends with what that leaves open.

10 of 10 scenarios passed, 63 of 63 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The platform, the workload and one GPU: what every later number stands on | 10 of 10 |
| S02 | Dense model on four GPUs: four copies, two pairs, or one wide server | 6 of 6 |
| S03 | Mixture of experts on four GPUs: tensor parallelism or expert parallelism | 8 of 8 |
| S04 | Engine tuning: the chunk size, the batch limit, prefix caching, async scheduling and speculation | 7 of 7 |
| S05 | Prefill/decode disaggregation: separate servers for the two phases, with the KV cache moved between them | 6 of 6 |
| S06 | Routing: round robin, least loaded, or by prefix | 6 of 6 |
| S07 | KV cache offload to host memory | 4 of 4 |
| S08 | Quantization behind an accuracy gate | 5 of 5 |
| S09 | The tuned stack against the defaults, with repeats | 5 of 5 |
| S10 | Autoscaling on queue depth and on GPU utilisation | 6 of 6 |

### Context: the problem, the actors and who talks to whom

**Scenario.** A company serves two products from one pool of GPUs. A chat product sends short prompts with a short shared system prompt and wants quick, steady answers. A coding product sends long prompts that are mostly the same repository text for every request on that repository and wants the first token within a few seconds. The first launch is deployed with the engine's defaults, four copies of the model behind a round-robin balancer. It works, but the bill is large and nobody can say whether the GPUs are used well. Each engineer has a favourite fix (a bigger tensor-parallel group, separate prefill servers, FP8, a bigger batch, more replicas), and each fix is tried in production and judged by the average latency.

Six terms are used throughout.

- **Goodput.** Tokens per second per GPU from requests that met both of their targets: the first token within its time, and the gap between tokens within its limit. A token that arrives late counts for nothing.
- **Capacity.** The highest load per GPU at which at least 95% of requests meet both targets, interpolated between the load levels on either side of that line.
- **KV cache.** The attention keys and values of every token a server has seen and still keeps. It is the memory budget of a server: what is left after the weights decides how many requests run at once and how many prompt prefixes stay cached.
- **Prefill and decode.** Reading the prompt (compute-bound, all tokens at once) and producing the answer (memory-bound, one token per request per step). On a small GPU a step that contains a long prompt stalls every request that is generating.
- **Layout.** How a model is placed on GPUs: whole on each (replicas), split inside each layer (tensor parallel), or with its experts spread over the GPUs (expert parallel).
- **Gate.** A rule written before the numbers are read, which a change must pass before it is adopted. Here: an accuracy rule for quantization, and a rule that a setting must beat the noise.

![Who uses the platform and what it is made of](diagrams/appendix/a22-1.svg)

**What the picture shows.** Product teams own the traffic and its targets. Serving engineers own the layout, the engine settings, the routing and the autoscaling, and run the tuning loop. The platform team owns the cluster, the GPU pool, the metrics and the cost. The platform is the traffic and its targets, a router, the engine replicas on a GPU node, the metrics, an autoscaler, an accuracy gate and the tuning loop that decides what to adopt.

![The tuning loop](diagrams/appendix/a22-5.svg)

**What the picture shows.** The loop the scenarios follow: measure the workload and fix the targets, measure the defaults, change one thing, measure a control to know the noise, gate anything that changes the numbers the model computes, adopt only what beat the noise, and in the end compare the whole tuned stack with the defaults over several seeds. A setting that does not beat its control stays at its default.

**The path of one request.**

1. The load generator sends a request when the trace says so (open loop), whether or not the previous ones have finished.
2. The router picks a server: in turn, the least loaded, or the one that most recently saw the same prompt prefix. For disaggregation it sends the prompt to a prefill server first.
3. The server looks the prompt up in its prefix cache (and in a host-memory tier, if it has one), computes the rest in chunks that share each step with the requests that are generating, and then generates tokens one step at a time.
4. The load generator records the time of the first token and of every following token, and at the end of each load level writes all the records to the state bucket.
5. Prometheus scrapes every server's metrics (requests waiting, requests running, KV cache use, cache hits, speculation, transfers) and the GPU exporter every 5 seconds.
6. The tests read the records and the counters back, compute goodput and capacity, and check the claim each scenario makes.

### The software I implemented and used

| # | Component | What it does here | Where | Status |
|---|---|---|---|---|
| 1 | **VPC, EKS 1.37, node pools** | Three zones; two system nodes, one CPU node for the load generators and the router, one GPU node; a gateway endpoint to S3. | `terraform/10-cluster` | Written |
| 2 | **NVIDIA device plugin** | Advertises each GPU as `nvidia.com/gpu` to the scheduler. | `terraform/20-platform` | Installed (v0.20.2) |
| 3 | **KEDA** | Turns a Prometheus query into a replica count for a Deployment. | `terraform/20-platform` | Installed (v2.21.1) |
| 4 | **Prometheus and the NVIDIA GPU exporter** | Scrape the servers' own metrics and the GPUs' counters every 5 seconds. | `terraform/20-platform` | Installed (chart 29.47.0, exporter 4.9.0) |
| 5 | **vLLM 0.33.0** | The serving engine, as shipped, with the Triton attention backend (FlashAttention needs a newer GPU). | image `vllm/vllm-openai:v0.33.0` | Used as shipped |
| 6 | **Traffic and the goodput metric** | Two products as a replayable trace (the same seed gives the same requests), with shared prefixes, bursts, and the per-product targets. | `workloads/traffic.py` | Written |
| 7 | **Load generator** | Open-loop streaming client; records first-token time and gap between tokens for every request. | `workloads/loadgen.py` | Written |
| 8 | **Router** | Round robin, least loaded, prefix-aware, and prefill/decode; re-reads a headless Service so that new replicas receive traffic. A stand-in for the gateway's endpoint picker, labelled as such. | `workloads/router.py` | Written |
| 9 | **Accuracy questions** | 400 generated questions with exact answers, asked at temperature 0; the gate's evidence. | `workloads/quality.py` | Written |
| 10 | **Manifest builders** | The servers, the router, the load jobs, the ScaledObject. | `workloads/manifests.py` | Written |
| 11 | **Scenario runner and screenshot renderer** | Runs S01 to S10, records commands and output, redacts private values, draws terminal-style screenshots and the charts. | `tests/` | Written |

The Terraform is about 470 lines, the workload code about 640 lines, and the test code about 1,800 lines.

### Topology and instances

![Instances](diagrams/appendix/a22-2.svg)

**What the picture shows.** Two small nodes for the controllers, one CPU node for the load generators and the router (so that they never compete with the servers for CPU), and one GPU node. The GPU node is a `g4dn.12xlarge`: four NVIDIA T4 GPUs of 16 GB, 48 vCPUs, 192 GB of memory and an 800 GB disk that holds the model cache. All four GPUs sit on one PCIe host bridge (`nvidia-smi topo -m` shows `PHB` for every pair, which S01 prints), so every transfer between GPUs goes through host memory. The pool is created empty and scaled up afterwards, because a pool whose first node cannot be launched fails to create.

### Architecture

![The cluster and the pipeline](diagrams/appendix/a22-3.svg)

**What the picture shows.** The load generator sends requests to a router that sends them to one or more servers, each a Deployment with its own Service. For disaggregation, a prefill server computes the prompt and the connector copies the KV cache to a decode server. Prometheus scrapes the servers and the GPU exporter; KEDA reads a query from Prometheus and sets the number of replicas. Every load run writes its records to an S3 bucket, and the tests read them back from there.

![The layouts compared](diagrams/appendix/a22-4.svg)

**What the picture shows.** The layouts of S02, S03 and S05 on the four GPUs: four copies, two pairs or one group of four for the dense model; tensor parallel or four engines with expert parallel for the mixture of experts; and two prefill plus two decode servers.

### How it is implemented

**Layer 1: the cluster.** One VPC with three private and three public subnets and one NAT gateway, an EKS 1.37 cluster with the VPC CNI and three managed node groups: the system pool, the load pool, and the serving pool (the NVIDIA node image, a taint that every GPU pod tolerates, an 800 GB root volume for the model cache). A pod-identity association gives the load generators access to one S3 bucket and nothing else.

**Layer 2: the platform.** The NVIDIA device plugin, KEDA, Prometheus (scraping every pod that carries the standard annotations every 5 seconds) and the NVIDIA GPU exporter.

**The traffic.** Two products. *Chat*: a 100-token shared system prompt, about 200 tokens of the user's own text, about 120 tokens of answer. *Code*: a 1,200-token repository text shared by every request on that repository (48 repositories), about 100 tokens of question, about 80 tokens of answer. Seventy per cent of requests are chat, arrivals are Poisson, and the load generator can add bursts. The prompts are built to an exact token length with the model's own tokenizer from a seeded word list, so the same seed always gives the same requests. The words are random, which matters for speculation (S04).

**The load generator.** For each load level it builds the trace, sends each request at its arrival time over a streaming connection whether or not earlier ones have finished, and records the time of every token. The first 15 seconds of each 90-second level are not scored (the batch is filling), and a request that never finishes counts as a miss.

**Goodput and capacity.** A request counts if its first token came within the product's limit and the 95th percentile of its gaps between tokens is within the product's limit. Goodput is the tokens of such requests per second per GPU. Capacity is interpolated between the highest level at which at least 95% of requests counted and the next.

**The router.** A short program with four policies. *Round robin* and *least loaded* need no explanation. *Prefix* remembers, per server, the hashes of the 256-character blocks of the prompts it sent there; a new prompt goes to the server that holds the longest chain of its blocks, provided the chain is at least two blocks (about 130 tokens) and the server is not well above the average load; otherwise it goes to the least loaded. *Prefill/decode* sends the prompt to a prefill server with a request for one token and the connector's parameters, and the reply, which says where the KV cache is, to a decode server whose stream is returned. It can re-read a headless Service every two seconds so that new replicas receive traffic.

**The accuracy questions.** 400 questions generated from a seed: addition of two three-digit numbers, multiplication of a two-digit by a one-digit number, short counting stories, and chains of three arithmetic steps. A few-shot prompt, a short answer at temperature 0, exact match. The gate's rule is written in S08 before any answer is read.

**Going deeper.**

- **Open loop, on purpose.** A closed-loop client (a fixed number of connections that wait for answers) slows down when the server does, which hides overload. The open-loop client keeps sending, so the knee in every curve is real.
- **One model name per layout.** Each server is its own Deployment and Service, so the router's list of servers is explicit and a layout is one manifest.
- **A control in every comparison.** The default is run twice, on different GPUs in different rounds (S04, S07), so that a difference between two settings can be read against the difference between two identical runs.
- **Downloads happen once.** The weights are fetched to the node's disk before a scenario starts, and servers read them from there, so a server's start-up time does not include the network.

### Where reality differed from the design and from my plan

These are findings, confirmed on the live system.

- **Four-GPU capacity was the first obstacle.** `g6e.12xlarge` (L40S), `g6.12xlarge` (L4) and `g5.12xlarge` (A10G) were refused in every zone of us-east-1, and in us-east-2 and us-west-2, for hours; only `g4dn.12xlarge` (T4) and single-GPU types launched. A pool that cannot launch its first node also fails to create after thirty-two minutes, so the pool is created empty.
- **The queue does not grow on a slow GPU.** The design says to scale on queue depth. On a T4 the engine admits up to 256 requests into the batch, so a server that is too slow shows it as slow steps, not as a queue: the waiting count stayed at zero while 30 requests were running and almost none of them met their targets. The signal that grows with load is the number of requests in the system (waiting plus running), and S10 scales on that.
- **A T4 is limited by prompt work.** With the default chunk of 2,048 tokens a step that carries a prompt chunk takes about a second, and every request that is generating waits for it. That is why a wider tensor-parallel group won in S02 and why small chunk sizes changed inter-token latency by three quarters in S04.
- **A first sweep of the mixture-of-experts model used loads that were too high.** At 0.3 to 1.5 requests per second per GPU every request was late on both layouts; I lowered the sweep to start near idle. The first run is not kept; the transcript in S03 is of the second.
- **A node rebooted in the middle of a run, and the run that was open then read nothing back.** AWS rebooted the GPU node and one system node while S03 was running. From then on, that run (S03, S04 and S05) found no results when it read the state bucket, although the files were there and a fresh process read them. I did not find the cause; the run's environment had a retry limit of one set by an earlier command of mine, which is the likeliest candidate. The three scenarios were discarded and run again from the start with the limit removed, the reads retried and the reason of any failed read printed. A sleeping pod I use to run commands inside the engine image also did not come back after the reboot; the tests now recreate it.
- **My network changed address during the build.** The cluster's API endpoint is open to one address; when the egress address of my machine changed, every call timed out until `update-my-ip.sh` and a re-apply fixed it. The cluster itself was never exposed more widely.

### S01: The platform, the workload and one GPU: what every later number stands on

**Design reference:** Capacity planning: KV cache is the budget; goodput, not throughput, is the metric.

**What this tests.** Show the GPU node and how its four GPUs are connected, predict the KV cache from the model's shape and compare it with what the engine reports, describe the traffic, and measure one GPU's goodput curve with default settings.

Before any experiment I show what the experiments stand on: the node and its four GPUs, how those GPUs are connected, the defaults the engine picks for them, and the traffic. Then I make one prediction that can be checked from outside the engine, the size of the KV cache, and measure one GPU with default settings under rising load. Every later scenario is read against this curve.

![S01: The platform, the workload and one GPU: what every later number stands on](diagrams/appendix/a22-evidence/S01.png)

![S01: curve, drawn from the run data](diagrams/appendix/a22-evidence/S01-curve.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl get nodes -L pool,instance-type,zone
NAME                           STATUS   ROLES    AGE   VERSION               POOL      INSTANCE-TYPE   ZONE
ip-10-70-13-105.ec2.internal   Ready    <none>   18h   v1.37.1-eks-9adbfea   system    m5.xlarge       us-east-1a
ip-10-70-28-87.ec2.internal    Ready    <none>   18h   v1.37.1-eks-9adbfea   loadgen   c6i.4xlarge     us-east-1b
ip-10-70-33-144.ec2.internal   Ready    <none>   18h   v1.37.1-eks-9adbfea   system    m5.xlarge       us-east-1c
ip-10-70-35-3.ec2.internal     Ready    <none>   15h   v1.37.1-eks-9adbfea   serving   g4dn.12xlarge   us-east-1c

$ kubectl get nodes -l pool=serving -o custom-columns=NAME,TYPE,GPU   # what the scheduler can hand out
NAME                         TYPE            GPU
ip-10-70-35-3.ec2.internal   g4dn.12xlarge   4

$ nvidia-smi --query-gpu=index,name,memory.total,pcie.link.gen.max,pcie.link.width.max --format=csv
index, name, memory.total [MiB], pcie.link.gen.max, pcie.link.width.max
0, Tesla T4, 15360 MiB, 3, 16
1, Tesla T4, 15360 MiB, 3, 16
2, Tesla T4, 15360 MiB, 3, 16
3, Tesla T4, 15360 MiB, 3, 16

$ nvidia-smi topo -m   # how the four GPUs reach each other
[4mGPU0	GPU1	GPU2	GPU3	CPU Affinity	NUMA Affinity	GPU NUMA ID[0m
GPU0	 X 	PHB	PHB	PHB	0-47	0		N/A
GPU1	PHB	 X 	PHB	PHB	0-47	0		N/A
GPU2	PHB	PHB	 X 	PHB	0-47	0		N/A
GPU3	PHB	PHB	PHB	 X 	0-47	0		N/A

$ python3 probe_defaults.py   # EngineArgs(...).create_engine_config(), the defaults this vLLM picks for this GPU
max_num_batched_tokens 2048 max_num_seqs 256 chunked True async True
prefix_caching True

$ python3 -c 'print the attention shape of Qwen/Qwen3-4B from its config.json'
{"num_hidden_layers": 36, "num_key_value_heads": 8, "head_dim": 128, "hidden_size": 2560, "num_attention_heads": 32, "vocab_size": 151936}
bytes of KV cache per token = 2 (K and V) x 36 layers x 8 KV heads x 128 head size x 2 bytes (fp16) = 147,456

$ # download Qwen/Qwen3-4B to the node's disk (once)
/hf/hub/models--Qwen--Qwen3-4B/snapshots/1cfa9a7208912126459214e8b04321603b3df60c
(2 s)

$ kubectl apply -f one.yaml   # one: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
one: servers s-one-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                      READY   STATUS    RESTARTS   AGE     IP            NODE                         NOMINATED NODE   READINESS GATES
s-one-0-8b74d9b89-mwk9b   1/1     Running   0          2m10s   10.70.40.48   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl -n serving logs deploy/s-one-0 | grep -E 'Model loading|Available KV|GPU KV cache size|attention backend'
weights 7.56 GiB; memory left for KV cache 4.36 GiB; engine-reported capacity 31,776 tokens; attention backend TRITON_ATTN
predicted from the formula: 4.36 GiB / 147,456 bytes = 31,749 tokens   (difference 0.09%)

$ python3 probe_workload.py   # 600 requests of the trace (seed 1, 2 req/s), tokenised with the model's own tokenizer
requests: 602
chat: {"n": 424, "prompt_tokens_p50": 298, "prompt_tokens_p95": 557, "output_tokens_p50": 123, "output_tokens_p95": 240, "shared_prefix_tokens": 100, "shared_share": 0.348, "groups": 1}
code: {"n": 178, "prompt_tokens_p50": 1298, "prompt_tokens_p95": 1374, "output_tokens_p50": 80, "output_tokens_p95": 136, "shared_prefix_tokens": 1200, "shared_share": 0.923, "groups": 45}
arrivals: {"mean_gap_s": 0.497, "p99_gap_s": 2.05, "min_gap_s": 0.0002}

$ kubectl apply -f lg-s01-one.yaml   # one: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-one-0:8000, 1 GPU(s)

$ kubectl -n serving logs job/lg-s01-one   # one
rate 0.3: goodput 41.8 tok/s/gpu, attainment 0.96, completed 27/28
rate 0.6: goodput 62.8 tok/s/gpu, attainment 0.87, completed 47/47
rate 0.9: goodput 61.0 tok/s/gpu, attainment 0.46, completed 82/82
rate 1.2: goodput 46.1 tok/s/gpu, attainment 0.33, completed 88/88
rate 1.5: goodput 4.8 tok/s/gpu, attainment 0.05, completed 106/106
done

$ # one T4, default engine settings: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          41.77          0.964          0.269           53.4          1.226          144.6
          0.6          62.76          0.872           1.51          210.1          1.699          210.9
          0.9          61.04          0.463          2.361         1184.3          3.814         1379.6
          1.2          46.13           0.33          2.559          375.9          2.466         1145.9
          1.5           4.76          0.047          2.404         1271.0          3.402         1451.5

$ # what the cluster's Prometheus saw during each level: busiest GPU's utilisation, queue length, requests in the batch, KV cache usage
req/s   gpu util   waiting   running   KV usage
  0.3        79%       0.0       2.3        3%
  0.6        76%       0.0       4.5        9%
  0.9       100%       0.0      12.8       24%
  1.2       100%       0.0      14.1       25%
  1.5        89%       0.0      30.0       52%

$ # counters from the server's own /metrics over the whole sweep
prefix cache: 60,000 of 240,744 prompt tokens found in the cache (25%)
preemptions: 0
time in prefill 266 s, time in decode 5,766 s (summed over requests)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the serving pool is one node of four GPUs, advertised to the scheduler by the NVIDIA device plugin | Every experiment is one or more pods holding whole GPUs, so the number of GPUs on the node is the number of servers (or the width of one server) that can run at a time. |
| PASS | the four GPUs are joined by PCIe only (no NVLink in the topology matrix) | The matrix lists the link between every pair; the values seen are ['PHB', 'X']. PCIe/host-bridge paths run at tens of GB/s, an order of magnitude below NVLink, so every GPU-to-GPU transfer in the later scenarios (tensor-parallel all-reduce, expert all-to-all, KV transfer) is paid for at that speed. |
| PASS | the server loads the model and answers its health check | Start-up (pull the image from the node's cache, load 8 GB of weights, compile, capture CUDA graphs) took 127.2 s; that figure matters later, when a replica has to be added under load. |
| PASS | the KV capacity computed from the model's shape matches the engine's own figure within 2% | The engine pre-allocates fixed-size blocks of KV cache from whatever memory is left after weights and activations; tokens = bytes / (2 x layers x KV heads x head size x 2). It follows from the shape alone, which is why capacity can be planned before anything is deployed. Measured difference 0.09%. |
| PASS | the engine reports its attention backend as Triton (FlashAttention needs a newer GPU than a T4) | The log line 'Cannot use FA version 2 ... compute capability >= 8' appears just before the backend is chosen. Every number in this appendix is therefore for the Triton kernels; a newer GPU would be faster and would move every threshold, but not the shapes of the curves. |
| PASS | the two products differ the way the design says: chat is mostly its own text, code is mostly a shared repository prefix | Share of a prompt's tokens that comes from the group's shared prefix: code 0.92, chat 0.35. A prefix cache can only help the first; routing by prefix (S06) and offloading (S07) are aimed at it. |
| PASS | at the lightest load nearly every request meets both targets, so the targets are reachable on this hardware | At 0.3 req/s the share of requests meeting both targets is 0.96. Targets that the idle server cannot meet would make every later comparison a comparison of zeros. |
| PASS | a knee exists: past some load the share meeting the targets collapses and goodput falls below its peak | Goodput peaks at 62.8 tok/s but at 1.5 req/s it is 4.8 with 0.05 of requests inside their targets, while raw throughput is 170.3. The server is still busy and producing tokens; most of them are late. That gap is why the metric is goodput. |
| PASS | the number of requests in the system (waiting plus running) rises at every step of load and is at least 5 times higher when overloaded than at the lightest level | Requests in the system per level: 2.3, 4.5, 12.8, 14.1, 30.0. This is the signal S10 scales on. The waiting queue alone stayed at 0.0: the engine admits up to 256 sequences into the batch, so a T4 that is too slow shows it as slow steps (inter-token latency), not as a queue. |
| PASS | the prefix cache serves part of the prompt tokens (the code product's shared repository text) | 25% of prompt tokens were found in cache. With 48 repositories of 1,200 tokens (about 57,000 tokens) and room for about 20,000 beside the running requests, only the recently used repositories stay cached; S06 and S07 change that. |

**Measured in this run:** GPU utilisation as the scaling signal: 79% at the lightest level (attainment 0.96), 79% at the last healthy level and 89% when overloaded (attainment 0.05). The counter says 'busy', not 'late', and it is already near its ceiling at a fraction of the capacity; S10 shows what that does to an autoscaler.

**Why you can trust this result.**

- The node and GPU figures are read from the Kubernetes API and from `nvidia-smi` inside the cluster; the engine defaults are read from the engine's own configuration object, not from documentation.
- The KV capacity is predicted from the model's `config.json` and the memory the engine reports, and compared with the capacity the engine reports separately; the two come from different lines of the log.
- The load generator is open loop: it sends each request when the trace says, whether or not the server has caught up, so an overloaded server cannot slow the offered load down and look better than it is.
- Every request's first-token time and gap between tokens is recorded and written to the state bucket; the table is computed from those records after the run.

**Why you get this output.**

- A T4 has 16 GB of memory and 320 GB/s of bandwidth. The 4B model's weights take 7.6 GiB, so about 4.4 GiB is left for the cache, which at 147,456 bytes per token is about 31,700 tokens: that is the whole budget for every request in flight and every cached prefix.
- Below the knee the server keeps up and nearly every request meets both targets. Past it, requests queue behind prompt work: a step that carries a 2,048-token chunk of someone's prompt takes about a second on this GPU, and every running request waits for it, so inter-token latency, not throughput, is what fails first.
- The waiting queue stays at zero because the engine admits up to 256 sequences into one batch. The number of requests in the system (waiting plus running) is what grows with load, and it is the signal S10 uses.
- GPU utilisation is already high at a fraction of the capacity, because a GPU running a few decodes is 'busy' all the time; the counter says busy, not late.

### S02: Dense model on four GPUs: four copies, two pairs, or one wide server

**Design reference:** Parallelism: tensor parallelism trades per-GPU efficiency for memory; replicas trade memory for independence.

**What this tests.** Serve one 4B model on the same four GPUs three ways (4 x TP1, 2 x TP2, 1 x TP4), predict the KV cache each leaves, and compare goodput per GPU on identical traffic.

One 4B model, four GPUs, three ways to lay it out: four independent copies, two pairs that split each layer between two GPUs, or one group of four. I predict the memory each layout leaves for the cache from the weights alone, then measure goodput per GPU on identical traffic.

![S02: Dense model on four GPUs: four copies, two pairs, or one wide server](diagrams/appendix/a22-evidence/S02.png)

![S02: curve, drawn from the run data](diagrams/appendix/a22-evidence/S02-curve.png)

![S02: kv, drawn from the run data](diagrams/appendix/a22-evidence/S02-kv.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f tp1x4.yaml   # tp1x4: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
tp1x4: servers s-tp1x4-0, s-tp1x4-1, s-tp1x4-2, s-tp1x4-3, router policy least

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                         READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-tp1x4-759fff5bd6-wcqtw     1/1     Running   0          2m28s   10.70.29.112   ip-10-70-28-87.ec2.internal   <none>           <none>
s-tp1x4-0-7d58fbd689-92h2s   1/1     Running   0          2m30s   10.70.37.214   ip-10-70-35-3.ec2.internal    <none>           <none>
s-tp1x4-1-55985bb8cb-gxtvw   1/1     Running   0          2m30s   10.70.37.211   ip-10-70-35-3.ec2.internal    <none>           <none>
s-tp1x4-2-98fff4cd7-hd2t9    1/1     Running   0          2m29s   10.70.40.49    ip-10-70-35-3.ec2.internal    <none>           <none>
s-tp1x4-3-6bdd994786-wv9b6   1/1     Running   0          2m29s   10.70.37.212   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s02-tp1x4.yaml   # tp1x4: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-tp1x4:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s02-tp1x4   # tp1x4
rate 1.2: goodput 35.1 tok/s/gpu, attainment 1.00, completed 88/88
rate 2.4: goodput 71.8 tok/s/gpu, attainment 0.99, completed 185/185
rate 3.6: goodput 89.7 tok/s/gpu, attainment 0.80, completed 271/271
rate 4.8: goodput 16.2 tok/s/gpu, attainment 0.10, completed 370/370
rate 6.0: goodput 6.2 tok/s/gpu, attainment 0.03, completed 458/458
done

$ # tp1x4: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.425           47.0          1.179           48.7
          0.6          71.76          0.995           0.43           58.3          1.343          104.4
          0.9          89.71          0.804          1.063          216.0           2.13          215.7
          1.2          16.17          0.097          1.618         1098.6          2.492         1114.1
          1.5            6.2          0.033          2.015         1263.1          2.837         1457.7

$ kubectl apply -f tp2x2.yaml   # tp2x2: 2 x Qwen3-4B on 2 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --tensor-parallel-size 2]
tp2x2: servers s-tp2x2-0, s-tp2x2-1, router policy least

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                         READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-tp2x2-cd86d74b-cf4nf       1/1     Running   0          2m41s   10.70.29.114   ip-10-70-28-87.ec2.internal   <none>           <none>
s-tp2x2-0-6c9dd466b5-4ndht   1/1     Running   0          2m42s   10.70.37.213   ip-10-70-35-3.ec2.internal    <none>           <none>
s-tp2x2-1-cf96c4d6-j2q9w     1/1     Running   0          2m41s   10.70.37.215   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s02-tp2x2.yaml   # tp2x2: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-tp2x2:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s02-tp2x2   # tp2x2
rate 1.2: goodput 35.1 tok/s/gpu, attainment 1.00, completed 88/88
rate 2.4: goodput 72.0 tok/s/gpu, attainment 1.00, completed 185/185
rate 3.6: goodput 108.9 tok/s/gpu, attainment 1.00, completed 271/271
rate 4.8: goodput 83.8 tok/s/gpu, attainment 0.62, completed 370/370
rate 6.0: goodput 35.1 tok/s/gpu, attainment 0.22, completed 458/458
done

$ # tp2x2: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.399           31.6          0.764           31.8
          0.6          71.97            1.0          0.693           85.7           0.88          111.5
          0.9         108.85          0.996          0.414          137.5          1.147          141.7
          1.2          83.82          0.622          0.771          221.7          0.886          269.2
          1.5          35.06          0.223          0.951          454.3          1.031          685.0

$ kubectl apply -f tp4x1.yaml   # tp4x1: 1 x Qwen3-4B on 4 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --tensor-parallel-size 4]
tp4x1: servers s-tp4x1-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                         READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-tp4x1-0-68bdc7fccc-rjzsz   1/1     Running   0          2m31s   10.70.37.214   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl apply -f lg-s02-tp4x1.yaml   # tp4x1: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-tp4x1-0:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s02-tp4x1   # tp4x1
rate 1.2: goodput 35.1 tok/s/gpu, attainment 1.00, completed 88/88
rate 2.4: goodput 72.0 tok/s/gpu, attainment 1.00, completed 185/185
rate 3.6: goodput 109.1 tok/s/gpu, attainment 1.00, completed 271/271
rate 4.8: goodput 139.5 tok/s/gpu, attainment 0.97, completed 370/370
rate 6.0: goodput 43.1 tok/s/gpu, attainment 0.28, completed 458/458
done

$ # tp4x1: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.433           47.4          0.472           73.1
          0.6          71.97            1.0          0.237           77.5          0.471           80.4
          0.9         109.05            1.0          0.338          109.7          0.722          116.1
          1.2         139.49          0.973          0.369          143.4          0.372          160.2
          1.5          43.14          0.279          0.554          232.3          0.507          232.3

$ # KV cache each layout leaves, against the prediction 'sharding weights over n GPUs frees (1 - 1/n) of them on each GPU'
layout    weights/GPU   KV memory/GPU (measured)   (predicted)   KV tokens in total
tp1x4         7.56 GiB         4.36 GiB            4.36 GiB       127,104
tp2x2         3.82 GiB         8.05 GiB            8.14 GiB       234,528
tp4x1         1.89 GiB         9.80 GiB           10.03 GiB       285,536

$ # the lightest level (0.3 req/s per GPU): what one request feels
layout    chat TTFT p50   chat ITL p95   code TTFT p50
tp1x4          0.176 s          47 ms        1.114 s
tp2x2          0.125 s          32 ms        0.666 s
tp4x1          0.113 s          47 ms        0.433 s

$ # capacity (highest load per GPU with 95% of requests inside their targets) and goodput at it
layout    capacity req/s/GPU   goodput tok/s/GPU
tp1x4               0.67             71.8
tp2x2               0.94            108.8
tp4x1               1.21            139.5

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | tp1x4: every server starts and answers its health check | Started in 139.8 s. |
| PASS | tp2x2: every server starts and answers its health check | Started in 153.7 s. |
| PASS | tp4x1: every server starts and answers its health check | Started in 148.7 s. |
| PASS | the KV memory per GPU follows the sharding arithmetic within 8% for every layout | Weights are split over the group, so each GPU keeps (1 - 1/n) of the weight size for cache. Largest difference 2.3% (the engine also reserves a little more for activations and communication buffers as groups widen). |
| PASS | a wider tensor-parallel group holds more tokens in total: 1 x TP4 > 2 x TP2 > 4 x TP1 | Total KV tokens 127,104 (4 x TP1), 234,528 (2 x TP2), 285,536 (1 x TP4). Four copies of the weights waste 3 x 7.6 GiB that one sharded copy gives back to the cache. |
| PASS | every layout has a measured capacity: some load level kept 95% of requests inside their targets | A layout whose lightest level already fails would be reporting the targets, not the layout. |

**Measured in this run:** PREDICTED goodput per GPU, best to worst: tp1x4 > tp2x2 > tp4x1 (a 4B model fits on one T4; extra communication over PCIe buys nothing the model needs). MEASURED: tp4x1 > tp2x2 > tp1x4. The prediction did not hold; see the numbers above for which effect won (memory freed for cache, or decode reading half the weights per GPU). Lightest-load latency: chat ITL p95 47 ms (TP1), 32 ms (TP2), 47 ms (TP4). Decode is limited by reading the weights from memory each step; a wider group reads less per GPU and adds an all-reduce over PCIe per layer.

**Why you can trust this result.**

- The three layouts run one after another on the same node with the same trace and the same seed; the router (where there is one) is the same code.
- The KV memory figures are the engine's own start-up report, compared with a prediction made from the first layout's numbers before the others were read.
- Capacity is interpolated between the last load level that kept 95% of requests inside their targets and the first that did not; both levels are in the table.

**Why you get this output.**

- Sharding the weights over n GPUs frees (1 - 1/n) of them on every GPU for the cache; that is why the total KV capacity rises from four copies to one wide group.
- My prediction was that four copies would win, because a 4B model needs no sharding. It did not hold. On a T4 the thing that hurts most is the long prompt step that stalls every running request; split four ways, that step is a quarter as long, so inter-token latency under load stays lower and the wider group stays inside its targets at a higher load. The all-reduce over PCIe costs something, but less than that gain at this size.
- The result belongs to this GPU: on a card with much more compute per byte of bandwidth the prompt step is short and four copies would likely win again. The method (predict, measure, compare) is the part that carries over.

### S03: Mixture of experts on four GPUs: tensor parallelism or expert parallelism

**Design reference:** Parallelism for MoE: expert parallelism, all-to-all and load balancing.

**What this tests.** Serve Qwen3-30B-A3B (128 experts, 8 used per token) with TP4 and with four data-parallel engines sharing the experts (EP), read the engine's own account of the expert placement and the all-to-all, ask for expert load balancing, and compare goodput.

A mixture-of-experts model has most of its weights in experts that each token visits only a few of. Qwen3-30B-A3B has 128 experts per layer and uses 8 per token. Four T4s cannot hold it in 16 bits, so the checkpoint is the int4 (GPTQ) one. I run it two ways: tensor parallel over four GPUs, and four data-parallel engines that share the experts between them (expert parallelism), read the engine's account of where the experts went, and then ask for expert load balancing.

![S03: Mixture of experts on four GPUs: tensor parallelism or expert parallelism](diagrams/appendix/a22-evidence/S03.png)

![S03: curve, drawn from the run data](diagrams/appendix/a22-evidence/S03-curve.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # download Qwen/Qwen3-30B-A3B-GPTQ-Int4 to the node's disk (once)
/hf/hub/models--Qwen--Qwen3-30B-A3B-GPTQ-Int4/snapshots/9b534e4318b7ebc3c961a839f13eb18b1833f441
(3 s)

$ kubectl apply -f moe-tp4.yaml   # moe-tp4: 1 x Qwen3-30B-A3B-GPTQ-Int4 on 4 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --tensor-parallel-size 4]
moe-tp4: servers s-moe-tp4-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                           READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-moe-tp4-0-7b9694b4cf-f9t74   1/1     Running   0          3m39s   10.70.32.243   ip-10-70-35-3.ec2.internal   <none>           <none>

$ nvidia-smi --query-gpu=index,memory.used --format=csv,noheader   # moe-tp4
0, 13797 MiB
1, 13797 MiB
2, 13797 MiB
3, 13797 MiB

$ kubectl -n serving logs job/lg-s03-quality-tp4   # the accuracy questions, moe-tp4
accuracy {"add": 1.0, "mul": 1.0, "word": 1.0, "chain": 0.84} all 0.96

$ kubectl apply -f lg-s03-moe-tp4.yaml   # moe-tp4: levels [0.08, 0.2, 0.4, 0.8, 1.2] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-moe-tp4-0:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s03-moe-tp4   # moe-tp4
rate 0.08: goodput 1.5 tok/s/gpu, attainment 1.00, completed 5/5
rate 0.2: goodput 3.8 tok/s/gpu, attainment 1.00, completed 9/9
rate 0.4: goodput 10.8 tok/s/gpu, attainment 0.83, completed 35/35
rate 0.8: goodput 8.7 tok/s/gpu, attainment 0.38, completed 55/55
rate 1.2: goodput 4.9 tok/s/gpu, attainment 0.10, completed 88/88
done

$ # moe-tp4: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
         0.02           1.46            1.0          0.562           22.8          2.828           23.7
         0.05            3.8            1.0          0.685           32.8          1.889           34.7
          0.1          10.76          0.829          3.052          255.1          3.376          381.2
          0.2           8.67          0.382          2.592          560.7          2.543          665.6
          0.3           4.85          0.102          3.562          771.3          3.822          839.1

$ kubectl apply -f moe-ep4.yaml   # moe-ep4: 1 x Qwen3-30B-A3B-GPTQ-Int4 on 4 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --data-parallel-size 4 --enable-expert-parallel]
moe-ep4: servers s-moe-ep4-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                           READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-moe-ep4-0-54749cdf54-fjljb   1/1     Running   0          3m51s   10.70.37.209   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl -n serving logs deploy/s-moe-ep4-0 | grep -E 'Expert parallelism|all2all|Experts|KV cache size'
INFO 11-27 22:11:39 [cuda_communicator.py:280] Using AgRsAll2AllManager all2all manager.
INFO 11-27 22:11:41 [expert_map_manager.py:250] [EP Rank 0/4] Expert parallelism is enabled. Expert placement strategy: linear. Local/global number of experts: 32/128. Experts local to global index m…
INFO 11-27 22:11:54 [int_wna16.py:472] Using MarlinExperts
INFO 11-27 22:13:07 [kv_cache_utils.py:1147] GPU KV cache size: 62,480 tokens, Maximum concurrency for 4,096 tokens per request: 15.25x
INFO 11-27 22:13:07 [kv_cache_utils.py:1147] GPU KV cache size: 62,480 tokens, Maximum concurrency for 4,096 tokens per request: 15.25x
INFO 11-27 22:13:07 [kv_cache_utils.py:1147] GPU KV cache size: 62,480 tokens, Maximum concurrency for 4,096 tokens per request: 15.25x
INFO 11-27 22:13:07 [kv_cache_utils.py:1147] GPU KV cache size: 62,480 tokens, Maximum concurrency for 4,096 tokens per request: 15.25x

$ nvidia-smi --query-gpu=index,memory.used --format=csv,noheader   # moe-ep4
0, 14045 MiB
1, 14045 MiB
2, 14045 MiB
3, 14045 MiB

$ kubectl apply -f lg-s03-moe-ep4.yaml   # moe-ep4: levels [0.08, 0.2, 0.4, 0.8, 1.2] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-moe-ep4-0:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s03-moe-ep4   # moe-ep4
rate 0.08: goodput 1.5 tok/s/gpu, attainment 1.00, completed 5/5
rate 0.2: goodput 3.8 tok/s/gpu, attainment 1.00, completed 9/9
rate 0.4: goodput 12.5 tok/s/gpu, attainment 0.97, completed 35/35
rate 0.8: goodput 19.7 tok/s/gpu, attainment 0.96, completed 55/55
rate 1.2: goodput 28.4 tok/s/gpu, attainment 0.77, completed 88/88
done

$ # moe-ep4: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
         0.02           1.46            1.0          1.062           13.9          1.251           15.5
         0.05            3.8            1.0          0.406           15.8          1.353           16.2
          0.1          12.53          0.971          1.689           21.9          2.204           77.9
          0.2          19.69          0.964          1.753           22.4          1.764          151.5
          0.3          28.45          0.773          2.465          217.0          2.218         1197.9

$ kubectl -n serving logs job/lg-s03-quality-ep4   # the accuracy questions, moe-ep4
accuracy {"add": 1.0, "mul": 1.0, "word": 1.0, "chain": 0.84} all 0.96

$ # weights and KV cache of the two layouts
TP4: weights 4.02 GiB per GPU, KV 7.01 GiB per GPU, 306,128 tokens in total (one shared cache)
EP4: weights 5.15 GiB per GPU, KV 5.72 GiB per GPU, engine-reported 62,480 tokens per engine, 4 engines

$ # capacity and goodput per GPU
layout     capacity req/s/GPU   goodput tok/s/GPU
moe-tp4              0.07              3.8
moe-ep4              0.21             19.7

$ # the lightest and the heaviest level, side by side
moe-tp4   0.02 req/s/GPU: chat TTFT p50 0.54 s, chat ITL p95 23 ms, attainment 1.00   |     0.3 req/s/GPU: chat ITL p95 771 ms, attainment 0.10
moe-ep4   0.02 req/s/GPU: chat TTFT p50 0.31 s, chat ITL p95 14 ms, attainment 1.00   |     0.3 req/s/GPU: chat ITL p95 217 ms, attainment 0.77

$ # the accuracy questions (25 per kind) answered by each layout
TP4: 0.96
EP4: 0.96

$ kubectl -n serving logs deploy/s-moe-eplb-0 | grep NotImplementedError   # the same four-engine layout, with --enable-eplb
NotImplementedError: EPLB is not supported AutoGPTQMoEMethod.

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | moe-tp4: the server starts | Started in 216.0 s. |
| PASS | moe-ep4: the server starts | Started in 227.7 s. |
| PASS | under expert parallelism each of the four ranks holds a quarter of the 128 experts | The engine prints its placement: 32 of 128 experts local to each rank. The attention layers are not sharded in this layout, so each GPU keeps its own copy of them and its own KV cache for its own requests. |
| PASS | the all-to-all runs through all-gather and reduce-scatter (the PCIe-friendly path); the NVLink and InfiniBand kernels are not selected | DeepEP's kernels need NVLink or RDMA hardware. The engine's default on this machine is the all-gather/reduce-scatter manager, so every token's activations visit every rank; the cost grows with batch, not with experts used. |
| PASS | expert parallelism keeps more weights on each GPU than tensor parallelism does (attention is replicated) | 5.15 GiB against 4.02 GiB per GPU: the experts are split four ways either way; only tensor parallelism also splits attention, embeddings and the shared layers. |
| PASS | both sweeps ran: at the lightest level at least 90% of the offered requests completed on both layouts | The lightest level is close to idle (0.02 requests per second per GPU), so it shows what one request costs on each layout; whether the targets are met at all is the finding, not a precondition. |
| PASS | the two layouts compute the same model: at least 90% of their answers to 100 questions are identical | Answers identical on 100% of questions. Different parallel layouts sum floating-point numbers in a different order, so a few answers can differ; a large disagreement would mean a wrong layout, not a different one. |
| PASS | asking for expert load balancing on this checkpoint is refused by the engine with a clear message instead of being ignored | Expert load balancing needs the model's expert layers to be replaceable at run time. The int4 (GPTQ) expert kernels this checkpoint uses are not, so the engine stops at start-up. The measurement of its effect needs a checkpoint in a format that supports it and more memory than a T4; it is therefore not made here. |

**Measured in this run:** Near idle, chat ITL p95 is 23 ms with TP4 and 14 ms with four data-parallel engines and EP; the target is 150 ms. At least one layout meets the target near idle. Not measured here: expert load balancing's effect (refused by the engine for this checkpoint, shown above); the DeepEP all-to-all kernels (need NVLink or RDMA); a multi-node wide expert-parallel layout (one GPU node).

**Why you can trust this result.**

- The expert placement and the all-to-all implementation are the engine's own log lines, quoted; GPU memory is read from `nvidia-smi` while the server runs.
- Both layouts answer the same 100 exact-answer questions and their answers are compared one by one, so a wrong layout would show as disagreement.
- The request for expert load balancing is made for real and the engine's answer is quoted; nothing about it is inferred.

**Why you get this output.**

- With expert parallelism each rank holds 32 of the 128 experts and its own copy of attention, so a token's work is done on the rank that owns it and only the expert inputs and outputs travel. With tensor parallelism every layer is split four ways and every layer ends in an all-reduce, which on a PCIe fabric with no direct GPU-to-GPU path costs far more per layer than on NVLink; that is the difference in latency under load.
- The model's active parameters are 3B, so near idle both layouts answer quickly; the layouts separate as load rises and the cost of communication is paid on every step.
- The engine refuses expert load balancing for this checkpoint because its int4 expert kernels cannot be swapped at run time; that is the engine's limit for this format, not a configuration mistake, and the effect of balancing is therefore not measured.

### S04: Engine tuning: the chunk size, the batch limit, prefix caching, async scheduling and speculation

**Design reference:** Engine tuning: batching, chunked prefill, speculative decoding.

**What this tests.** Change one engine setting at a time on one GPU each (four settings at once, one per GPU, same traffic and seed), keep an unchanged control to measure the noise, and read which settings move capacity beyond that noise.

Engine settings are cheap to change and easy to believe in. I change one at a time, on one GPU each, four at a time on the four GPUs of the node, with the same traffic and the same seed. A control (the default, run again on another GPU in another round) measures how far two identical runs differ, and a setting counts as an improvement only if it beats that difference.

![S04: Engine tuning: the chunk size, the batch limit, prefix caching, async scheduling and speculation](diagrams/appendix/a22-evidence/S04.png)

![S04: chunk, drawn from the run data](diagrams/appendix/a22-evidence/S04-chunk.png)

![S04: other, drawn from the run data](diagrams/appendix/a22-evidence/S04-other.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f default.yaml chunk256.yaml chunk512.yaml chunk8192.yaml   # default: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]; chunk256: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --max-num-batched-tokens 256]; chunk512: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --max-num-batched-tokens 512]; chunk8192: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --max-num-batched-tokens 8192]
default: servers s-default-0
chunk256: servers s-chunk256-0
chunk512: servers s-chunk512-0
chunk8192: servers s-chunk8192-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                             READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-chunk256-0-59446f854b-7qv9h    1/1     Running   0          2m23s   10.70.32.243   ip-10-70-35-3.ec2.internal   <none>           <none>
s-chunk512-0-79c5b5f679-6sdwh    1/1     Running   0          2m20s   10.70.32.244   ip-10-70-35-3.ec2.internal   <none>           <none>
s-chunk8192-0-5b7d86f9dc-wq8vs   1/1     Running   0          2m17s   10.70.32.245   ip-10-70-35-3.ec2.internal   <none>           <none>
s-default-0-744fc8798f-4m7c7     1/1     Running   0          2m25s   10.70.32.241   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl apply -f lg-s04-default.yaml   # default: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-default-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-chunk256.yaml   # chunk256: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-chunk256-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-chunk512.yaml   # chunk512: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-chunk512-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-chunk8192.yaml   # chunk8192: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-chunk8192-0:8000, 1 GPU(s)

$ kubectl -n serving logs job/lg-s04-default   # default
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 62.8 tok/s/gpu, attainment 0.87, completed 47/47
rate 0.9: goodput 57.2 tok/s/gpu, attainment 0.44, completed 82/82
rate 1.2: goodput 46.1 tok/s/gpu, attainment 0.33, completed 88/88
rate 1.5: goodput 7.1 tok/s/gpu, attainment 0.07, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-chunk256   # chunk256
rate 0.3: goodput 41.3 tok/s/gpu, attainment 0.93, completed 28/28
rate 0.6: goodput 39.3 tok/s/gpu, attainment 0.57, completed 47/47
rate 0.9: goodput 28.3 tok/s/gpu, attainment 0.22, completed 82/82
rate 1.2: goodput 29.1 tok/s/gpu, attainment 0.23, completed 88/88
rate 1.5: goodput 3.2 tok/s/gpu, attainment 0.03, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-chunk512   # chunk512
rate 0.3: goodput 41.3 tok/s/gpu, attainment 0.93, completed 28/28
rate 0.6: goodput 38.9 tok/s/gpu, attainment 0.55, completed 47/47
rate 0.9: goodput 36.5 tok/s/gpu, attainment 0.29, completed 82/82
rate 1.2: goodput 35.1 tok/s/gpu, attainment 0.26, completed 88/88
rate 1.5: goodput 2.5 tok/s/gpu, attainment 0.03, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-chunk8192   # chunk8192
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 63.7 tok/s/gpu, attainment 0.89, completed 47/47
rate 0.9: goodput 67.1 tok/s/gpu, attainment 0.52, completed 82/82
rate 1.2: goodput 59.4 tok/s/gpu, attainment 0.42, completed 88/88
rate 1.5: goodput 11.3 tok/s/gpu, attainment 0.10, completed 106/106
done

$ # default: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0           0.27           53.2          1.226          144.0
          0.6          62.76          0.872          1.514          208.4          1.695          209.0
          0.9          57.23          0.439          2.325         1185.0          3.747         1379.8
          1.2          46.13           0.33          2.573          375.1          2.475         1147.1
          1.5           7.09          0.066          2.434         1256.6          3.406         1454.2

$ # chunk256: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          41.27          0.929          0.266          124.2          1.317          259.0
          0.6          39.31          0.574          1.162          272.7          1.839          229.7
          0.9          28.33           0.22           2.56          322.8          4.256          325.9
          1.2          29.05          0.227          2.175          300.7          2.686          319.1
          1.5           3.21          0.028          3.859          345.1          4.715          349.4

$ # chunk512: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          41.27          0.929          0.257           50.4          1.226          370.2
          0.6          38.91          0.553          1.018          403.9          1.715          310.0
          0.9          36.53          0.293          2.191          525.8          3.352          556.2
          1.2          35.15          0.261          1.887          473.6           2.24          487.9
          1.5           2.53          0.028          2.595          545.7          3.449          565.5

$ # chunk8192: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.256           48.7           1.15          138.1
          0.6          63.75          0.894          1.468          198.6          1.636          198.3
          0.9          67.12          0.524          2.304         1110.4          3.321         1133.0
          1.2           59.4           0.42          2.391          346.6          2.424         1093.8
          1.5          11.32          0.104          2.374         1150.0          4.147         1200.0

$ kubectl apply -f seqs32.yaml seqs64.yaml no-prefix.yaml sync.yaml   # seqs32: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --max-num-seqs 32]; seqs64: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --max-num-seqs 64]; no-prefix: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --no-enable-prefix-caching]; sync: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --no-async-scheduling]
seqs32: servers s-seqs32-0
seqs64: servers s-seqs64-0
no-prefix: servers s-no-prefix-0
sync: servers s-sync-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                            READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-no-prefix-0-d66567c95-zgmwx   1/1     Running   0          2m19s   10.70.32.247   ip-10-70-35-3.ec2.internal   <none>           <none>
s-seqs32-0-5f468d946b-bzffl     1/1     Running   0          2m25s   10.70.32.248   ip-10-70-35-3.ec2.internal   <none>           <none>
s-seqs64-0-64bc54f6c7-g94pr     1/1     Running   0          2m22s   10.70.32.246   ip-10-70-35-3.ec2.internal   <none>           <none>
s-sync-0-6c464cfbc8-xrw49       1/1     Running   0          2m17s   10.70.32.249   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl apply -f lg-s04-seqs32.yaml   # seqs32: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-seqs32-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-seqs64.yaml   # seqs64: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-seqs64-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-no-prefix.yaml   # no-prefix: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-no-prefix-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-sync.yaml   # sync: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-sync-0:8000, 1 GPU(s)

$ kubectl -n serving logs job/lg-s04-seqs32   # seqs32
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 63.7 tok/s/gpu, attainment 0.89, completed 47/47
rate 0.9: goodput 67.5 tok/s/gpu, attainment 0.52, completed 82/82
rate 1.2: goodput 66.5 tok/s/gpu, attainment 0.47, completed 88/88
rate 1.5: goodput 7.7 tok/s/gpu, attainment 0.07, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-seqs64   # seqs64
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 62.8 tok/s/gpu, attainment 0.87, completed 47/47
rate 0.9: goodput 64.9 tok/s/gpu, attainment 0.50, completed 82/82
rate 1.2: goodput 52.2 tok/s/gpu, attainment 0.36, completed 88/88
rate 1.5: goodput 6.6 tok/s/gpu, attainment 0.07, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-no-prefix   # no-prefix
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 57.8 tok/s/gpu, attainment 0.79, completed 47/47
rate 0.9: goodput 40.8 tok/s/gpu, attainment 0.34, completed 82/82
rate 1.2: goodput 20.6 tok/s/gpu, attainment 0.11, completed 88/88
rate 1.5: goodput 0.0 tok/s/gpu, attainment 0.00, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-sync   # sync
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 59.7 tok/s/gpu, attainment 0.83, completed 47/47
rate 0.9: goodput 54.3 tok/s/gpu, attainment 0.41, completed 82/82
rate 1.2: goodput 36.3 tok/s/gpu, attainment 0.26, completed 88/88
rate 1.5: goodput 3.8 tok/s/gpu, attainment 0.04, completed 106/106
done

$ # seqs32: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.253           48.8          1.153          136.1
          0.6          63.75          0.894          1.477          198.9          1.644          201.0
          0.9          67.51          0.524          2.195         1117.7          3.452         1269.2
          1.2          66.45          0.466          2.388          280.8          2.066         1066.0
          1.5           7.71          0.066          7.683         1091.2          8.944         1094.2

$ # seqs64: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.258           49.2          1.191          140.2
          0.6          62.76          0.872          1.502          202.5          1.686          204.9
          0.9          64.85            0.5          2.271         1158.1           3.64         1367.8
          1.2          52.23          0.364          2.519          292.7          2.094         1099.6
          1.5            6.6          0.066          2.458         1222.4          3.464         1385.9

$ # no-prefix: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.312           51.1          1.149          164.9
          0.6          57.76          0.787          1.481          213.0          1.643          223.3
          0.9          40.83          0.341          2.721         1403.9          3.991         1437.6
          1.2          20.61          0.114          2.593         1214.3          2.986         1247.0
          1.5            0.0            0.0          8.455         1406.8          9.835         1444.4

$ # sync: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.217           54.5          1.184          143.0
          0.6          59.71           0.83          1.032          215.5          1.597          217.2
          0.9          54.32          0.415          2.183         1227.3          3.489         1396.3
          1.2          36.25          0.261          2.257          371.6          2.263         1147.7
          1.5           3.76          0.038          2.149         1266.0           3.16         1280.2

$ kubectl apply -f control.yaml ngram4.yaml ngram2.yaml   # control: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]; ngram4: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --speculative-config '{"method":"ngram","num_speculative_tokens":4,"prompt_lookup_max":4,"prompt_lookup_min":2}']; ngram2: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --speculative-config '{"method":"ngram","num_speculative_tokens":2,"prompt_lookup_max":3,"prompt_lookup_min":2}']
control: servers s-control-0
ngram4: servers s-ngram4-0
ngram2: servers s-ngram2-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                           READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-control-0-788b546fdf-4h2q8   1/1     Running   0          2m43s   10.70.32.244   ip-10-70-35-3.ec2.internal   <none>           <none>
s-ngram2-0-67bbf5d54c-sn22w    1/1     Running   0          2m37s   10.70.32.243   ip-10-70-35-3.ec2.internal   <none>           <none>
s-ngram4-0-57677c88cd-xns7k    1/1     Running   0          2m40s   10.70.32.241   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl apply -f lg-s04-control.yaml   # control: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-control-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-ngram4.yaml   # ngram4: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-ngram4-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s04-ngram2.yaml   # ngram2: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-ngram2-0:8000, 1 GPU(s)

$ kubectl -n serving logs job/lg-s04-control   # control
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 63.7 tok/s/gpu, attainment 0.89, completed 47/47
rate 0.9: goodput 67.1 tok/s/gpu, attainment 0.52, completed 82/82
rate 1.2: goodput 53.7 tok/s/gpu, attainment 0.38, completed 88/88
rate 1.5: goodput 10.6 tok/s/gpu, attainment 0.09, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-ngram4   # ngram4
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 62.2 tok/s/gpu, attainment 0.87, completed 47/47
rate 0.9: goodput 57.4 tok/s/gpu, attainment 0.44, completed 82/82
rate 1.2: goodput 66.0 tok/s/gpu, attainment 0.47, completed 88/88
rate 1.5: goodput 39.6 tok/s/gpu, attainment 0.25, completed 106/106
done

$ kubectl -n serving logs job/lg-s04-ngram2   # ngram2
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 63.7 tok/s/gpu, attainment 0.89, completed 47/47
rate 0.9: goodput 68.4 tok/s/gpu, attainment 0.52, completed 82/82
rate 1.2: goodput 83.9 tok/s/gpu, attainment 0.58, completed 88/88
rate 1.5: goodput 81.1 tok/s/gpu, attainment 0.48, completed 106/106
done

$ # control: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0           0.25           48.5          1.164          137.0
          0.6          63.75          0.894          1.446          198.5          1.627          199.2
          0.9          67.12          0.524          2.221         1122.3          3.492         1278.4
          1.2          53.69          0.375          2.418          352.3          2.402         1091.5
          1.5          10.59          0.094          2.401         1183.9          3.291         1206.4

$ # ngram4: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.279           59.9          1.128           22.9
          0.6          62.19          0.872          1.464          202.0          1.666          190.9
          0.9          57.39          0.439          2.213         1088.8          3.605          491.9
          1.2          65.96          0.466          2.612          441.4            2.5          517.7
          1.5          39.63          0.245          2.426         1134.5          3.379          524.3

$ # ngram2: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0           0.27           52.1          1.181           52.3
          0.6          63.75          0.894          1.483          195.7          1.668          199.7
          0.9          68.44          0.524          2.277          760.7          3.622          526.6
          1.2          83.88           0.58          2.443          436.0          2.382          553.4
          1.5          81.09          0.481          2.203         1081.3          2.991          502.0

$ # capacity and goodput of every setting, one GPU each
setting      capacity req/s   goodput tok/s   vs default (capacity)
default           0.42           43.1          +0%
chunk256          0.00            0.0        -100%
chunk512          0.00            0.0        -100%
chunk8192         0.44           43.1          +6%
seqs32            0.44           43.1          +6%
seqs64            0.42           43.1          +0%
no-prefix         0.37           43.1         -11%
sync              0.39           43.1          -7%
control           0.44           43.1          +6%
ngram4            0.42           43.1          +0%
ngram2            0.44           43.1          +6%

$ # the control: the same default setting on another GPU in another round
default 0.42 req/s, 43.1 tok/s; control 0.44 req/s, 43.1 tok/s -> noise 0.02 req/s (6%), 0.0 tok/s

$ # chat inter-token latency p95 at 0.9 req/s, by chunk size
chunk256       323 ms   (code TTFT p95 4.26 s)
chunk512       526 ms   (code TTFT p95 3.35 s)
default       1185 ms   (code TTFT p95 3.75 s)
chunk8192     1110 ms   (code TTFT p95 3.32 s)

$ # n-gram speculation: the engine's own counters
ngram4: 25,992 drafts, 103,968 drafted tokens, 20,130 accepted (19%), tokens per verification step 1.77
ngram2: 27,103 drafts, 54,206 drafted tokens, 18,932 accepted (35%)

$ # which settings move capacity by more than the noise (or one grid step, 0.3 req/s/GPU, whichever is larger)
chunk256   worse
chunk512   worse
chunk8192  no change beyond the noise
seqs32     no change beyond the noise
seqs64     no change beyond the noise
no-prefix  no change beyond the noise
sync       no change beyond the noise
ngram4     no change beyond the noise
ngram2     no change beyond the noise

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | round 1: all 4 servers start (default, chunk256, chunk512, chunk8192) | Each server holds one GPU; they run side by side on one node, so a small amount of CPU and PCIe contention is shared by all and is part of the noise measured by the control. |
| PASS | round 2: all 4 servers start (seqs32, seqs64, no-prefix, sync) | Each server holds one GPU; they run side by side on one node, so a small amount of CPU and PCIe contention is shared by all and is part of the noise measured by the control. |
| PASS | round 3: all 3 servers start (control, ngram4, ngram2) | Each server holds one GPU; they run side by side on one node, so a small amount of CPU and PCIe contention is shared by all and is part of the noise measured by the control. |
| PASS | the control (same setting, different GPU and time) lands within 25% of the default's capacity, so the measurement is repeatable enough to read effects larger than that | Capacity 0.42 against 0.44 req/s per GPU. The grid of load levels is [0.3, 0.6, 0.9, 1.2, 1.5], so a difference of one grid step is the smallest that can be seen at all. |
| PASS | the prefill chunk size moves inter-token latency the way the mechanism says: the largest chunk gives a higher ITL p95 than the smallest at 0.9 req/s | Every engine step does one batch of decode tokens plus whatever prompt tokens fit under the limit; on a T4 a step with 8,192 prompt tokens takes seconds, and every running request waits for it. ITL p95: 323 ms at 256, 526 ms at 512, 1185 ms at the default (2,048), 1110 ms at 8,192. |
| PASS | with prefix caching off the engine reports no prefix-cache hits, and code requests wait longer for their first token | Hits 0 of 0; code TTFT p50 at 0.9 req/s 1.40 s against 1.33 s with the cache (hit rate 25%). |
| PASS | speculation ran: drafts were made and some were accepted, and the acceptance rate is read from the engine's own counters | Acceptance 19% of drafted tokens. The n-gram drafter proposes the words that followed the last match in the prompt; it helps when answers repeat the prompt. The synthetic prompts here are random words, so a moderate acceptance is the honest figure for this traffic, and a code-completion workload would differ. |

**Measured in this run:** Settings with an effect beyond the noise: none. A setting that shows 'no change beyond the noise' stays at its default in S09; a change is adopted only when the data supports it.

**Why you can trust this result.**

- Each setting runs with the same trace as every other, and the unchanged control is in the table: it shows the size of the noise rather than assuming it.
- Speculation's counters (drafts, drafted tokens, accepted tokens) come from the server's own metrics, read before and after the sweep.
- The verdict for each setting is computed from the measured capacity and the noise by a rule written in the scenario before the numbers were read.

**Why you get this output.**

- A smaller prefill chunk shortens the step in which running requests wait, so inter-token latency at moderate load falls (the table shows it falling by three quarters); but the prompt itself takes more steps to finish, so first-token time rises, and with these targets the second effect costs more requests than the first saves.
- The batch limit does not matter here because the T4 runs out of speed long before it runs out of batch slots: at most about thirty requests are running at the overload level and the default allows 256.
- Prefix caching helps the code requests (their first-token wait is longer without it) but the cache holds only part of the 48 repositories, which is what S06 and S07 work on.
- N-gram speculation accepted about a fifth of the four tokens it drafted and about a third of the two it drafted, but the prompts here are random words, so the drafts rarely continue the way a real code or document prompt would; the setting was neutral on this traffic.

### S05: Prefill/decode disaggregation: separate servers for the two phases, with the KV cache moved between them

**Design reference:** Prefill/decode disaggregation: when it pays and what it costs.

**What this tests.** On a prompt-heavy mix, run four unified servers, and then two prefill + two decode servers and one prefill + three decode servers joined by NIXL; verify that the KV cache really moved, and compare goodput per GPU and latency.

Prefill is compute-bound and decode is memory-bound, and when they share a server the first stalls the second. Disaggregation puts them on different servers and copies the KV cache from one to the other. Here that copy goes through NIXL, over the node's PCIe fabric, between two pods. I use a prompt-heavy mix (70% code) because that is where disaggregation should pay, and I check from the servers' own counters that the cache really moved.

![S05: Prefill/decode disaggregation: separate servers for the two phases, with the KV cache moved between them](diagrams/appendix/a22-evidence/S05.png)

![S05: curve, drawn from the run data](diagrams/appendix/a22-evidence/S05-curve.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f unified.yaml   # unified: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
unified: servers s-unified-0, s-unified-1, s-unified-2, s-unified-3, router policy least

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                           READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-unified-68b8f8b449-txstl     1/1     Running   0          2m24s   10.70.29.112   ip-10-70-28-87.ec2.internal   <none>           <none>
s-unified-0-84fcd87fbf-8l9p2   1/1     Running   0          2m26s   10.70.37.209   ip-10-70-35-3.ec2.internal    <none>           <none>
s-unified-1-697d8d75b8-f8wxn   1/1     Running   0          2m26s   10.70.37.210   ip-10-70-35-3.ec2.internal    <none>           <none>
s-unified-2-68d6c8d6dc-wh8mc   1/1     Running   0          2m25s   10.70.32.245   ip-10-70-35-3.ec2.internal    <none>           <none>
s-unified-3-8647478f58-8rbzj   1/1     Running   0          2m25s   10.70.37.211   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s05-unified.yaml   # unified: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-unified:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s05-unified   # unified
rate 1.2: goodput 29.2 tok/s/gpu, attainment 0.99, completed 87/88
rate 2.4: goodput 55.6 tok/s/gpu, attainment 0.95, completed 185/185
rate 3.6: goodput 10.0 tok/s/gpu, attainment 0.12, completed 271/271
rate 4.8: goodput 0.4 tok/s/gpu, attainment 0.00, completed 370/370
rate 6.0: goodput 0.0 tok/s/gpu, attainment 0.00, completed 458/458
done

$ # unified: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          29.17          0.989          0.402           53.2          1.232           53.2
          0.6          55.62          0.951          1.028          159.0          1.332          172.7
          0.9           10.0          0.122          2.042         1140.4          2.314         1131.6
          1.2           0.41          0.003         11.256         1452.9         12.579         1457.6
          1.5            0.0            0.0         31.381         1465.4         31.859         1474.5

$ kubectl apply -f pd2p2d.yaml   # pd2p2d: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}']
pd2p2d: servers s-pd2p2d-0, s-pd2p2d-1, s-pd2p2d-2, s-pd2p2d-3, router policy pd

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                          READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-pd2p2d-99bcb5566-cn74v      1/1     Running   0          2m25s   10.70.29.113   ip-10-70-28-87.ec2.internal   <none>           <none>
s-pd2p2d-0-f6d45dbdb-lbbpk    1/1     Running   0          2m27s   10.70.32.241   ip-10-70-35-3.ec2.internal    <none>           <none>
s-pd2p2d-1-68b69c6577-ltzr4   1/1     Running   0          2m27s   10.70.32.243   ip-10-70-35-3.ec2.internal    <none>           <none>
s-pd2p2d-2-78647d4556-vtltg   1/1     Running   0          2m26s   10.70.32.244   ip-10-70-35-3.ec2.internal    <none>           <none>
s-pd2p2d-3-54f49df75f-44skd   1/1     Running   0          2m26s   10.70.32.246   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s05-pd2p2d.yaml   # pd2p2d: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-pd2p2d:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s05-pd2p2d   # pd2p2d
rate 1.2: goodput 28.3 tok/s/gpu, attainment 0.98, completed 88/88
rate 2.4: goodput 52.0 tok/s/gpu, attainment 0.91, completed 185/185
rate 3.6: goodput 41.9 tok/s/gpu, attainment 0.55, completed 271/271
rate 4.8: goodput 3.3 tok/s/gpu, attainment 0.03, completed 370/370
rate 6.0: goodput 0.0 tok/s/gpu, attainment 0.00, completed 458/458
done

$ # pd2p2d: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          28.26          0.977          2.319           62.1          3.261           66.3
          0.6          52.03          0.908          3.175           91.5           3.97           92.9
          0.9          41.91           0.55          5.962          171.1          6.471          173.6
          1.2           3.34          0.027         20.934          239.1         21.417          242.8
          1.5            0.0            0.0         43.902          201.1         44.517          234.5

$ # pd2p2d: the transfer, from the servers' own NIXL counters
1,635 transfers, 155.92 GiB moved, mean 804.4 ms each; failed transfers 0
decode servers took 100% of their prompt tokens from the transferred KV cache; prompt tokens processed by the prefill servers 1,644,999; tokens generated by decode servers 159,442, by prefill servers…

$ kubectl apply -f pd1p3d.yaml   # pd1p3d: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}']
pd1p3d: servers s-pd1p3d-0, s-pd1p3d-1, s-pd1p3d-2, s-pd1p3d-3, router policy pd

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                          READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-pd1p3d-5d969cd5b-9kkcq      1/1     Running   0          2m24s   10.70.29.114   ip-10-70-28-87.ec2.internal   <none>           <none>
s-pd1p3d-0-85b7c8b758-f5m5d   1/1     Running   0          2m26s   10.70.32.245   ip-10-70-35-3.ec2.internal    <none>           <none>
s-pd1p3d-1-767dd66785-qpcl5   1/1     Running   0          2m26s   10.70.32.247   ip-10-70-35-3.ec2.internal    <none>           <none>
s-pd1p3d-2-586498b7fb-ww98r   1/1     Running   0          2m25s   10.70.32.248   ip-10-70-35-3.ec2.internal    <none>           <none>
s-pd1p3d-3-6fd8bfdfb7-gbgdc   1/1     Running   0          2m25s   10.70.37.209   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s05-pd1p3d.yaml   # pd1p3d: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-pd1p3d:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s05-pd1p3d   # pd1p3d
rate 1.2: goodput 24.5 tok/s/gpu, attainment 0.85, completed 88/88
rate 2.4: goodput 2.2 tok/s/gpu, attainment 0.04, completed 185/185
rate 3.6: goodput 0.0 tok/s/gpu, attainment 0.00, completed 271/271
rate 4.8: goodput 0.0 tok/s/gpu, attainment 0.00, completed 370/370
rate 6.0: goodput 0.0 tok/s/gpu, attainment 0.00, completed 458/458
done

$ # pd1p3d: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          24.47          0.852           4.15           52.7          5.247           52.9
          0.6           2.25          0.038         18.527           61.9         18.565           64.0
          0.9            0.0            0.0         71.988           65.7         72.101           65.9
          1.2            0.0            0.0        123.369           72.7        126.492           74.8
          1.5            0.0            0.0        171.403           67.7        171.374           70.2

$ # pd1p3d: the transfer, from the servers' own NIXL counters
1,635 transfers, 152.79 GiB moved, mean 884.1 ms each; failed transfers 0
decode servers took 100% of their prompt tokens from the transferred KV cache; prompt tokens processed by the prefill servers 1,644,999; tokens generated by decode servers 159,442, by prefill servers…

$ # capacity and goodput per GPU on the prompt-heavy mix (30% chat, 70% code)
layout     capacity req/s/GPU   goodput tok/s/GPU
unified              0.60             55.6
pd2p2d               0.42             28.3
pd1p3d               0.00              0.0

$ # decode smoothness at 0.6 req/s per GPU (chat ITL p95) and first-token wait (code TTFT p50)
unified   chat ITL p95    159 ms   code TTFT p50 1.11 s
pd2p2d    chat ITL p95     92 ms   code TTFT p50 1.90 s
pd1p3d    chat ITL p95     62 ms   code TTFT p50 15.44 s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | unified: all servers start | Started in 136.4 s. |
| PASS | pd2p2d: all servers start | Started in 137.3 s. |
| PASS | pd2p2d: the KV cache moved between servers: transfers happened, none failed, and the decode servers took most of their prompt from them | 100% of the decode servers' prompt tokens were found through the connector rather than computed. Prefill servers generated 1,635 tokens (one per request, the trigger) and decode servers 159,442: the two phases ran where they were placed. |
| PASS | pd1p3d: all servers start | Started in 136.5 s. |
| PASS | pd1p3d: the KV cache moved between servers: transfers happened, none failed, and the decode servers took most of their prompt from them | 100% of the decode servers' prompt tokens were found through the connector rather than computed. Prefill servers generated 1,635 tokens (one per request, the trigger) and decode servers 159,442: the two phases ran where they were placed. |
| PASS | every layout served its lightest level completely (at least 95% of requests finished), so the curves are comparable | Whether a layout meets the targets is the result; that it ran is the precondition. |

**Measured in this run:** Layouts that never kept 95% of requests inside their targets, even at the lightest level (0.3 req/s per GPU): pd1p3d. For 1P+3D the cause is in the table above: one prefill GPU computes every prompt (70% of them 1,300 tokens), so first-token waits are several seconds before the transfer even starts. PREDICTED: separating the phases removes prompt work from the decode servers' steps, so inter-token latency at the same load is lower with disaggregation. MEASURED chat ITL p95 at 0.6 req/s per GPU: unified 159 ms, 2P+2D 92 ms, 1P+3D 62 ms. The prediction held. PREDICTED: on four GPUs that share one PCIe fabric and have no GPU-direct path between pods, the transfer and the idle half of the node cost more than the interference they remove, so unified wins on goodput per GPU. MEASURED best: unified. The prediction held.

**Why you can trust this result.**

- The transfer counters (number of transfers, bytes, time, failures) are the connector's own metrics on the decode servers; the share of decode prompt tokens taken from the transfer is the engine's external-prefix-cache counter.
- The unified baseline uses the same four GPUs, the same router code (least loaded) and the same trace.
- The two predictions are written in the scenario before the data and reported with the result.

**Why you get this output.**

- The decode servers never compute prompts, so their steps contain only decodes and inter-token latency stays low and flat as load rises.
- Every request pays for the copy: the counters show about 100 MB of KV cache per request moved at about 120 MB/s over a path that, on this node, runs through host memory, so the first token comes about 0.8 seconds later for every request and more when the copies queue. That eats a large part of a 2-second target before anything is computed.
- The prefill servers become the bottleneck. With one prefill GPU feeding three decoders the prefill GPU does all the prompt work and the decoders idle; with two and two the balance is better but the copy cost remains.
- On hardware with a direct GPU-to-GPU path and more compute per prompt, the same layout would behave differently; this result is for four T4s on one PCIe fabric.

### S06: Routing: round robin, least loaded, or by prefix

**Design reference:** Routing: prefix-aware routing and the endpoint picker.

**What this tests.** Put the same four replicas behind three routing policies and measure the prefix-cache hit rate the servers report, the wait for the first token of code requests, and goodput; the router is a small stand-in for the gateway's endpoint picker and says so.

Four identical replicas, one trace, three ways to choose a replica: in turn, the one with the fewest requests in flight, or the one that most recently saw the same prompt prefix. The router is a small program of mine that stands in for the gateway's endpoint picker, and the appendix says so. What I check is what the servers report: how often a prompt's tokens were found in a cache.

![S06: Routing: round robin, least loaded, or by prefix](diagrams/appendix/a22-evidence/S06.png)

![S06: curve, drawn from the run data](diagrams/appendix/a22-evidence/S06-curve.png)

![S06: hits, drawn from the run data](diagrams/appendix/a22-evidence/S06-hits.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f route-rr.yaml   # route-rr: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
route-rr: servers s-route-rr-0, s-route-rr-1, s-route-rr-2, s-route-rr-3, router policy rr

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                            READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-route-rr-59bbf585fb-nwgkc     1/1     Running   0          2m25s   10.70.19.1     ip-10-70-28-87.ec2.internal   <none>           <none>
s-route-rr-0-8c8cbbf75-nbxq6    1/1     Running   0          2m27s   10.70.32.243   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-rr-1-59b6f4db68-r7vbp   1/1     Running   0          2m26s   10.70.32.241   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-rr-2-659f99bb77-4l9dz   1/1     Running   0          2m26s   10.70.32.244   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-rr-3-54c8c44b74-q75dw   1/1     Running   0          2m25s   10.70.32.246   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s06-route-rr.yaml   # route-rr: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-route-rr:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s06-route-rr   # route-rr
rate 1.2: goodput 35.1 tok/s/gpu, attainment 1.00, completed 88/88
rate 2.4: goodput 71.2 tok/s/gpu, attainment 0.99, completed 185/185
rate 3.6: goodput 80.7 tok/s/gpu, attainment 0.72, completed 271/271
rate 4.8: goodput 15.0 tok/s/gpu, attainment 0.08, completed 370/370
rate 6.0: goodput 5.5 tok/s/gpu, attainment 0.03, completed 458/458
done

$ # rr: prefix cache and balance
prefix cache hit rate across the four servers: 25% of 1,009,808 prompt tokens; preemptions 4
requests per replica: {"s-route-rr-0": 409, "s-route-rr-1": 409, "s-route-rr-2": 409, "s-route-rr-3": 408}

$ # rr: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.343           49.3          1.174           50.0
          0.6          71.17          0.989          0.389           65.2          1.435          110.4
          0.9          80.71          0.723          0.853          213.1          1.523          428.1
          1.2          14.98          0.084          1.577         1151.3          2.018         1181.8
          1.5           5.54          0.026          2.865         1352.2          3.626         1403.8

$ kubectl apply -f route-least.yaml   # route-least: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
route-least: servers s-route-least-0, s-route-least-1, s-route-least-2, s-route-least-3, router policy least

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                               READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-route-least-56779d9fd8-clt6m     1/1     Running   0          2m26s   10.70.29.114   ip-10-70-28-87.ec2.internal   <none>           <none>
s-route-least-0-6df45c7d97-khh8j   1/1     Running   0          2m28s   10.70.32.247   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-least-1-7d7f54d67b-b8gbm   1/1     Running   0          2m27s   10.70.32.245   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-least-2-667b496c79-274dl   1/1     Running   0          2m27s   10.70.32.248   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-least-3-5787f7f55d-sfdsh   1/1     Running   0          2m26s   10.70.32.249   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s06-route-least.yaml   # route-least: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-route-least:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s06-route-least   # route-least
rate 1.2: goodput 35.1 tok/s/gpu, attainment 1.00, completed 88/88
rate 2.4: goodput 72.0 tok/s/gpu, attainment 1.00, completed 185/185
rate 3.6: goodput 84.8 tok/s/gpu, attainment 0.77, completed 271/271
rate 4.8: goodput 20.1 tok/s/gpu, attainment 0.12, completed 370/370
rate 6.0: goodput 9.7 tok/s/gpu, attainment 0.05, completed 458/458
done

$ # least: prefix cache and balance
prefix cache hit rate across the four servers: 27% of 1,009,808 prompt tokens; preemptions 0
requests per replica: {"s-route-least-0": 415, "s-route-least-1": 402, "s-route-least-2": 409, "s-route-least-3": 409}

$ # least: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.477           47.0          1.218           49.5
          0.6          71.97            1.0          0.572           56.8          1.208           93.0
          0.9          84.78          0.775          0.936          208.6          1.499          443.4
          1.2          20.09          0.119           1.55         1098.5          2.456         1106.9
          1.5           9.73           0.05          1.989         1208.4          2.744         1227.4

$ kubectl apply -f route-prefix.yaml   # route-prefix: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
route-prefix: servers s-route-prefix-0, s-route-prefix-1, s-route-prefix-2, s-route-prefix-3, router policy prefix

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                                READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-route-prefix-5954747cf6-fmdt5     1/1     Running   0          2m30s   10.70.29.112   ip-10-70-28-87.ec2.internal   <none>           <none>
s-route-prefix-0-54b868d6cf-9zcj4   1/1     Running   0          2m31s   10.70.32.243   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-prefix-1-57449d8fcb-d4lpc   1/1     Running   0          2m31s   10.70.32.241   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-prefix-2-cc7779865-lpsqd    1/1     Running   0          2m31s   10.70.32.244   ip-10-70-35-3.ec2.internal    <none>           <none>
s-route-prefix-3-5dfbbcc586-zwbhm   1/1     Running   0          2m30s   10.70.32.246   ip-10-70-35-3.ec2.internal    <none>           <none>

$ kubectl apply -f lg-s06-route-prefix.yaml   # route-prefix: levels [1.2, 2.4, 3.6, 4.8, 6.0] req/s, 90 s each, first 15 s not scored, seed 1
target http://r-route-prefix:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s06-route-prefix   # route-prefix
rate 1.2: goodput 35.1 tok/s/gpu, attainment 1.00, completed 88/88
rate 2.4: goodput 71.8 tok/s/gpu, attainment 0.99, completed 185/185
rate 3.6: goodput 100.2 tok/s/gpu, attainment 0.90, completed 271/271
rate 4.8: goodput 46.2 tok/s/gpu, attainment 0.33, completed 370/370
rate 6.0: goodput 33.0 tok/s/gpu, attainment 0.18, completed 458/458
done

$ # prefix: prefix cache and balance
prefix cache hit rate across the four servers: 53% of 1,009,808 prompt tokens; preemptions 0
requests per replica: {"s-route-prefix-0": 578, "s-route-prefix-1": 497, "s-route-prefix-2": 257, "s-route-prefix-3": 303}

$ # prefix: goodput and latency by load level (per GPU)
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.386           47.9          1.167           56.3
          0.6          71.81          0.995          0.369          108.1          1.146           62.4
          0.9         100.24          0.904          0.483          180.8          2.188          453.4
          1.2           46.2           0.33          0.637          226.4          1.813         1090.6
          1.5           33.0          0.177          0.683          266.0          2.311         1162.3

$ # the three policies side by side
policy    cache hit rate   code TTFT p50 at 0.9/GPU   capacity req/s/GPU   goodput tok/s/GPU
rr                25%               1.19 s             0.64             71.2
least             27%               1.19 s             0.67             72.0
prefix            53%               0.32 s             0.75             71.8

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | policy rr: four replicas and the router start | Started in 136.5 s. |
| PASS | policy least: four replicas and the router start | Started in 137.2 s. |
| PASS | policy prefix: four replicas and the router start | Started in 140.0 s. |
| PASS | the prefix-aware policy gets a higher prefix-cache hit rate from the same servers than round robin does | Hit rate 53% against 25%. Round robin sends each repository's requests to all four servers, so each server must cache all 48 repositories (about 57,000 tokens) in room for about 20,000; the prefix policy sends a repository to the server that already holds it, so each holds about a quarter. |
| PASS | the prefix policy sends requests to servers that already had the prefix (the router counts the matches it made) | The router's own counter of prefix matches: 1574 of 1635 requests. |
| PASS | the prefix policy does not pile everything on one replica: the busiest replica takes less than 1.6 times the average | Busiest replica 1.41 times the mean. The router skips a replica that is well above the average load, so one popular repository cannot saturate one server. |

**Measured in this run:** Best policy by capacity then goodput: prefix. Round robin and least-loaded ignore what is cached; the gap between them and the prefix policy is the part of the traffic that repeats. This router is 150 lines of Python in this repository. The Gateway API Inference Extension implements the same two ideas (load-aware and prefix-aware picking) as a component of the gateway; it is not installed here, so what was measured is the policy and not that product.

**Why you can trust this result.**

- The cache hit rate is the sum of the four servers' own prefix-cache counters over the run, not the router's estimate; the router's own counter of matches is shown next to it.
- The three policies see the same trace in the same order; only the choice of server differs.
- The imbalance check uses the router's counts of requests per replica.

**Why you get this output.**

- With round robin each repository's requests reach all four replicas, so each replica must keep all 48 repositories (about 57,000 tokens) in room for about 20,000 and keeps evicting them. A policy that sends a repository to the replica that already holds it lets each replica hold about a quarter.
- A shorter shared text, like the chat system prompt (about 100 tokens), is not worth following: sending most chat requests to one replica for a 100-token saving unbalances the fleet for no gain. The router therefore follows a match only if it covers at least two 256-character blocks, and it skips a replica that is well above the average load.
- A higher hit rate saves prompt work, and prompt work is what stalls decodes on a T4, so first-token waits of code requests fall to a quarter and capacity rises by about a sixth; the rest of each request still costs what it costs.

### S07: KV cache offload to host memory

**Design reference:** KV cache offload: a second tier for prefixes that no longer fit in GPU memory.

**What this tests.** Give three single-GPU servers a host-memory tier of 0, 8 and 24 GiB (plus an unchanged control), drive them with a code-heavy mix whose repositories do not fit in GPU memory together, and read how much the second tier served, what it cost, and whether code requests got their first token sooner.

When the repositories do not all fit in GPU memory, a second tier in host memory can keep the ones that were evicted. Three single-GPU servers get 0, 8 and 24 GiB of host memory for it (plus an unchanged control) and a code-heavy mix, and I read how much the tier served, what moving the data cost, and whether capacity changed.

![S07: KV cache offload to host memory](diagrams/appendix/a22-evidence/S07.png)

![S07: curve, drawn from the run data](diagrams/appendix/a22-evidence/S07-curve.png)

![S07: found, drawn from the run data](diagrams/appendix/a22-evidence/S07-found.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f base.yaml control.yaml off8.yaml off24.yaml   # base: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]; control: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]; off8: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --kv-offloading-size 8 --kv-offloading-backend native]; off24: 1 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90 --kv-offloading-size 24 --kv-offloading-backend native]
base: servers s-base-0
control: servers s-control-0
off8: servers s-off8-0
off24: servers s-off24-0

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                           READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-base-0-668c458d65-q68zj      1/1     Running   0          2m36s   10.70.32.247   ip-10-70-35-3.ec2.internal   <none>           <none>
s-control-0-5b66d6c4bf-5v9hj   1/1     Running   0          2m33s   10.70.32.245   ip-10-70-35-3.ec2.internal   <none>           <none>
s-off24-0-79b49449c8-4z8qq     1/1     Running   0          2m27s   10.70.32.248   ip-10-70-35-3.ec2.internal   <none>           <none>
s-off8-0-dc48d448b-mmvh5       1/1     Running   0          2m30s   10.70.32.246   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl apply -f lg-s07-base.yaml   # base: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-base-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s07-control.yaml   # control: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-control-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s07-off8.yaml   # off8: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-off8-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s07-off24.yaml   # off24: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-off24-0:8000, 1 GPU(s)

$ kubectl -n serving logs job/lg-s07-base   # base
rate 0.3: goodput 39.1 tok/s/gpu, attainment 0.96, completed 28/28
rate 0.6: goodput 34.3 tok/s/gpu, attainment 0.60, completed 47/47
rate 0.9: goodput 0.0 tok/s/gpu, attainment 0.00, completed 82/82
rate 1.2: goodput 0.0 tok/s/gpu, attainment 0.00, completed 88/88
rate 1.5: goodput 0.0 tok/s/gpu, attainment 0.00, completed 106/106
done

$ kubectl -n serving logs job/lg-s07-control   # control
rate 0.3: goodput 39.1 tok/s/gpu, attainment 0.96, completed 28/28
rate 0.6: goodput 34.3 tok/s/gpu, attainment 0.60, completed 47/47
rate 0.9: goodput 0.0 tok/s/gpu, attainment 0.00, completed 82/82
rate 1.2: goodput 0.0 tok/s/gpu, attainment 0.00, completed 88/88
rate 1.5: goodput 0.0 tok/s/gpu, attainment 0.00, completed 106/106
done

$ kubectl -n serving logs job/lg-s07-off8   # off8
rate 0.3: goodput 39.1 tok/s/gpu, attainment 0.96, completed 28/28
rate 0.6: goodput 42.7 tok/s/gpu, attainment 0.70, completed 47/47
rate 0.9: goodput 27.9 tok/s/gpu, attainment 0.26, completed 82/82
rate 1.2: goodput 18.3 tok/s/gpu, attainment 0.15, completed 88/88
rate 1.5: goodput 8.7 tok/s/gpu, attainment 0.08, completed 106/106
done

$ kubectl -n serving logs job/lg-s07-off24   # off24
rate 0.3: goodput 39.1 tok/s/gpu, attainment 0.96, completed 28/28
rate 0.6: goodput 39.8 tok/s/gpu, attainment 0.68, completed 47/47
rate 0.9: goodput 28.7 tok/s/gpu, attainment 0.26, completed 82/82
rate 1.2: goodput 31.5 tok/s/gpu, attainment 0.28, completed 88/88
rate 1.5: goodput 15.1 tok/s/gpu, attainment 0.13, completed 106/106
done

$ # base: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          39.12          0.964           1.37           66.0          1.281          355.2
          0.6          34.33          0.596           0.45          723.8          2.337         1069.7
          0.9            0.0            0.0         10.657         1464.7          10.25         1469.3
          1.2            0.0            0.0         19.134         1454.6         20.114         1454.4
          1.5            0.0            0.0         18.022         1452.6         28.019         1463.7

$ # control: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          39.12          0.964          1.415           66.8          1.339          365.4
          0.6          34.33          0.596          0.485          930.3          2.399         1102.3
          0.9            0.0            0.0         11.562         1456.2         10.821         1458.9
          1.2            0.0            0.0         20.018         1447.6         20.641         1461.2
          1.5            0.0            0.0         19.095         1464.9         28.466         1469.5

$ # off8: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          39.12          0.964          1.407           65.4          1.317          355.4
          0.6          42.73          0.702          0.445          239.3          1.869         1037.1
          0.9          27.91          0.256          1.412          369.2          2.374          773.4
          1.2          18.33          0.148          0.642          336.2          2.201          468.4
          1.5           8.69          0.075          5.685         1174.9          7.904         1277.7

$ # off24: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          39.12          0.964          1.472           67.7          1.389          372.4
          0.6          39.76          0.681          0.506          247.4          1.968         1094.8
          0.9          28.67          0.256          0.692          305.5          1.591          309.7
          1.2          31.53          0.284          0.605          269.6          1.609          294.9
          1.5          15.13          0.132          0.561          305.9          1.612          310.6

$ # where the prompt tokens were found
server    GPU cache   host tier   moved over PCIe   copy time   code TTFT p50 at 0.9   code TTFT p95   capacity   goodput
base           33%         0%          0.0 GiB       0.0 s             2.06 s        10.25 s     0.31      39.1
control        34%         0%          0.0 GiB       0.0 s             2.14 s        10.82 s     0.31      39.1
off8           35%        27%         37.7 GiB       6.4 s             0.96 s         2.37 s     0.32      39.1
off24          35%        39%         37.4 GiB       6.4 s             0.97 s         1.59 s     0.32      39.1

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all four servers start (the offload tiers are allocated in pinned host memory at start) | base 124.9 s; control 1.2 s; off8 12.5 s; off24 1.2 s |
| PASS | the servers with a host tier report tokens served from it and bytes moved over PCIe; the servers without report none | The connector's own counters: external prefix-cache hits and total offloaded bytes. A tier that was configured but never read would show zero here. |
| PASS | a bigger host tier serves more of the prompt: the share found in the host tier grows from 8 GiB to 24 GiB | Host-tier share 27% at 8 GiB, 39% at 24 GiB. 8 GiB holds about 58,000 tokens of this model's KV cache and 24 GiB about 175,000; the 48 repositories need about 57,000. |
| PASS | with a host tier, a larger share of all prompt tokens is found without recomputing them than on the server without one | Found in GPU or host memory: 33% (base), 34% (control), 62% (8 GiB), 74% (24 GiB). |

**Measured in this run:** Capacity per GPU: base 0.31, control 0.31 (noise 0.00), 8 GiB 0.32, 24 GiB 0.32 req/s. The host tier did not raise capacity by more than the noise on this traffic: the copies and the T4's compute, not the lost prefixes, set the limit.

**Why you can trust this result.**

- The tier's use is read from the connector's own counters: tokens found in the host tier and bytes moved over PCIe.
- The four servers share the node and the traffic is the same trace with the same seed.
- The control (a second unchanged server) gives the noise against which any gain is judged.

**Why you get this output.**

- A quarter to two fifths of the prompt tokens came from the host tier instead of being recomputed, and the larger tier served more. At this model's 147,456 bytes per token, 8 GiB holds about 58,000 tokens and 24 GiB about 175,000, against about 57,600 tokens of repository prefixes (48 x 1,200) plus the chat and per-request text that also passes through; the smaller tier is therefore nearly full of useful prefixes and the larger one is not.
- Capacity did not move beyond the noise. The prompt tokens that came back from host memory were cheap to recover, but the load level at which requests stop meeting their inter-token target is set by the GPU's compute in each step, not by recovering a prefix.
- Moving data over PCIe is not free (the copy time is in the table), and it competes with the other three GPUs of the node for the same bus.

### S08: Quantization behind an accuracy gate

**Design reference:** Quantization: smaller weights and KV cache are adopted only if an accuracy gate passes.

**What this tests.** Serve the same model as fp16, with int4 weights (AWQ), with an int8 KV cache, and with both; ask 400 questions with exact answers of each; apply a written gate (at most 3 points lost, at least 90% identical answers); and only then read whether the gated configurations serve more.

Smaller numbers make a model faster and leave more room for the cache, and they change its answers. The gate is a written rule, applied before any speed is read: at most 3 points lost on 400 exact-answer questions, and at least 90% of answers identical to the fp16 model's. Four configurations are served: fp16, int4 weights (AWQ), an int8 KV cache, and both. FP8 and FP4 need hardware a T4 does not have, and are not run.

![S08: Quantization behind an accuracy gate](diagrams/appendix/a22-evidence/S08.png)

![S08: curve, drawn from the run data](diagrams/appendix/a22-evidence/S08-curve.png)

![S08: gate, drawn from the run data](diagrams/appendix/a22-evidence/S08-gate.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # download Qwen/Qwen3-4B-AWQ to the node's disk (once)
/hf/hub/models--Qwen--Qwen3-4B-AWQ/snapshots/74d4bd2bd4bff9cafc9345221320bffb08b406a3
(7 s)

$ kubectl apply -f fp16.yaml awq.yaml kv8.yaml awq-kv8.yaml
fp16: Qwen/Qwen3-4B 
awq: Qwen/Qwen3-4B-AWQ 
kv8: Qwen/Qwen3-4B --kv-cache-dtype int8_per_token_head
awq-kv8: Qwen/Qwen3-4B-AWQ --kv-cache-dtype int8_per_token_head

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                           READY   STATUS    RESTARTS   AGE     IP             NODE                         NOMINATED NODE   READINESS GATES
s-awq-0-6d44bc88f7-2w7cx       1/1     Running   0          2m32s   10.70.32.243   ip-10-70-35-3.ec2.internal   <none>           <none>
s-awq-kv8-0-559979d9b6-26xr4   1/1     Running   0          2m27s   10.70.32.249   ip-10-70-35-3.ec2.internal   <none>           <none>
s-fp16-0-5bd84cc769-n8rrv      1/1     Running   0          2m35s   10.70.32.241   ip-10-70-35-3.ec2.internal   <none>           <none>
s-kv8-0-65969f54b-cqdhn        1/1     Running   0          2m29s   10.70.32.244   ip-10-70-35-3.ec2.internal   <none>           <none>

$ kubectl -n serving logs job/lg-s08-q-fp16   # the accuracy questions, fp16
accuracy {"add": 0.99, "mul": 1.0, "word": 0.85, "chain": 0.65} all 0.873

$ kubectl -n serving logs job/lg-s08-q-awq   # the accuracy questions, awq
accuracy {"add": 0.99, "mul": 1.0, "word": 0.9, "chain": 0.62} all 0.877

$ kubectl -n serving logs job/lg-s08-q-kv8   # the accuracy questions, kv8
accuracy {"add": 0.99, "mul": 1.0, "word": 0.85, "chain": 0.67} all 0.877

$ kubectl -n serving logs job/lg-s08-q-awq-kv8   # the accuracy questions, awq-kv8
accuracy {"add": 0.99, "mul": 1.0, "word": 0.82, "chain": 0.63} all 0.86

$ # the accuracy gate: 400 questions (100 each of addition, multiplication, word problems, chains) at temperature 0
fp16 reference: 87.2% correct  {"add": 0.99, "mul": 1.0, "word": 0.85, "chain": 0.65}
awq       87.8% correct, -0.5 points vs fp16, 89% identical answers -> gate FAILS  {"add": 0.99, "mul": 1.0, "word": 0.9, "chain": 0.62}
kv8       87.8% correct, -0.5 points vs fp16, 98% identical answers -> gate PASSES  {"add": 0.99, "mul": 1.0, "word": 0.85, "chain": 0.67}
awq-kv8   86.0% correct, +1.3 points vs fp16, 87% identical answers -> gate FAILS  {"add": 0.99, "mul": 1.0, "word": 0.82, "chain": 0.63}

$ kubectl apply -f lg-s08-fp16.yaml   # fp16: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-fp16-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s08-awq.yaml   # awq: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-awq-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s08-kv8.yaml   # kv8: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-kv8-0:8000, 1 GPU(s)

$ kubectl apply -f lg-s08-awq-kv8.yaml   # awq-kv8: levels [0.3, 0.6, 0.9, 1.2, 1.5] req/s, 90 s each, first 15 s not scored, seed 1
target http://s-awq-kv8-0:8000, 1 GPU(s)

$ kubectl -n serving logs job/lg-s08-fp16   # fp16
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 63.7 tok/s/gpu, attainment 0.89, completed 47/47
rate 0.9: goodput 67.1 tok/s/gpu, attainment 0.52, completed 82/82
rate 1.2: goodput 53.7 tok/s/gpu, attainment 0.38, completed 88/88
rate 1.5: goodput 10.6 tok/s/gpu, attainment 0.09, completed 106/106
done

$ kubectl -n serving logs job/lg-s08-awq   # awq
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 68.5 tok/s/gpu, attainment 0.98, completed 47/47
rate 0.9: goodput 102.6 tok/s/gpu, attainment 0.82, completed 82/82
rate 1.2: goodput 118.8 tok/s/gpu, attainment 0.83, completed 88/88
rate 1.5: goodput 69.9 tok/s/gpu, attainment 0.39, completed 106/106
done

$ kubectl -n serving logs job/lg-s08-kv8   # kv8
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 62.8 tok/s/gpu, attainment 0.87, completed 47/47
rate 0.9: goodput 69.3 tok/s/gpu, attainment 0.54, completed 82/82
rate 1.2: goodput 77.6 tok/s/gpu, attainment 0.49, completed 88/88
rate 1.5: goodput 7.1 tok/s/gpu, attainment 0.07, completed 106/106
done

$ kubectl -n serving logs job/lg-s08-awq-kv8   # awq-kv8
rate 0.3: goodput 43.1 tok/s/gpu, attainment 1.00, completed 28/28
rate 0.6: goodput 68.5 tok/s/gpu, attainment 0.98, completed 47/47
rate 0.9: goodput 102.6 tok/s/gpu, attainment 0.82, completed 82/82
rate 1.2: goodput 127.9 tok/s/gpu, attainment 0.91, completed 88/88
rate 1.5: goodput 146.8 tok/s/gpu, attainment 0.87, completed 106/106
done

$ # fp16: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.256           48.3          1.168          134.8
          0.6          63.75          0.894          1.468          197.7          1.644          198.2
          0.9          67.12          0.524          2.221         1120.6          3.471         1280.1
          1.2          53.69          0.375          2.425          351.3          2.373         1096.7
          1.5          10.59          0.094          2.315         1163.9          3.365         1207.9

$ # awq: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.203           22.2          1.216           22.7
          0.6          68.47          0.979          1.444           60.0           1.66          109.3
          0.9          102.6          0.817          1.356          209.7          2.651          290.9
          1.2          118.8           0.83          1.437          180.9          1.592          350.0
          1.5          69.88          0.387          1.868          396.8          3.172          396.8

$ # kv8: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.243           52.6          1.218          142.3
          0.6          62.76          0.872          1.502          207.0          1.698          208.8
          0.9          69.35          0.537          1.554          320.9          2.646          396.7
          1.2          77.56          0.489          1.535          284.1            1.7          480.5
          1.5           7.09          0.066          1.955         1160.0          3.354         1184.1

$ # awq-kv8: goodput and latency by load level
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          43.11            1.0          0.204           21.9          1.191           22.5
          0.6          68.47          0.979          1.407           56.0          1.623          107.6
          0.9          102.6          0.817            1.3          205.4            2.6          285.3
          1.2         127.92          0.909          1.111          174.9          1.234          213.5
          1.5          146.8          0.868          1.023          171.5          1.744          160.3

$ # memory and capacity
config     weights GiB   KV tokens   capacity req/s   goodput tok/s   gate
fp16           7.56      31,776         0.44         43.1   reference
awq            2.50      67,760         0.65         68.5   FAILS
kv8            7.56      61,632         0.42         43.1   passes
awq-kv8        2.50     131,424         0.65         68.5   FAILS

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the fp16 reference server starts | The reference is the unquantized model; every gate decision is a comparison with it. |
| PASS | the reference scores well above chance and below perfect, so the questions can show damage in either direction | fp16 accuracy 87.2%. Few-shot arithmetic with one short answer leaves a 4B model no room to reason, so a numerical change in the weights or cache shows up as a wrong digit. |
| PASS | the gate is applied to every quantized configuration that started and each gets a recorded pass or fail | The gate (at most 3 points lost and at least 90% identical answers) was written before the numbers were read. Standard error of a 400-question accuracy is about 2 points, which is why the agreement test is part of it. |
| PASS | int4 weights free memory for the KV cache: the AWQ server holds more KV tokens than fp16 | 67,760 against 31,776 tokens; weights 2.5 GiB against 7.56 GiB. |
| PASS | an int8 KV cache holds about twice the tokens of fp16 in the same memory (ratio between 1.7 and 2.1) | 61,632 against 31,776 tokens: ratio 1.94. One byte per element instead of two, less the scale factors stored per token and head. |

**Measured in this run:** Adopted into the tuned stack (gate passed and capacity higher than fp16 by more than one grid step of 0.15 req/s/GPU): none. FP8 weights and FP8 KV cache are not run: the T4 (compute capability 7.5) has no FP8 units. FP4 needs Blackwell. The gate and the procedure are the same for those formats; only the hardware differs.

**Why you can trust this result.**

- The questions are generated from a seed, have exact numeric answers, and are asked at temperature 0 with a short answer, so a model with a damaged number format shows it as a wrong digit.
- Agreement is paired: the same question's answers from the two configurations are compared, which is far less noisy than comparing two accuracies of 400 questions each (about 2 points of standard error).
- The gate was written into the scenario before the answers were read, and the decision of each configuration is recorded whether it passes or not.

**Why you get this output.**

- Int4 weights take 2.5 GiB instead of 7.6 GiB, so the cache more than doubles and the server carries about half as much load again (capacity 0.65 against 0.44 requests per second); the engine's start-up report shows the memory figures.
- Average accuracy did not fall, but about one answer in nine changed, mostly on the chained and word problems, and the gate refuses a configuration that changes that many answers whether or not the average moved: the average hides which questions got worse.
- The int8 KV cache holds twice the tokens and changes almost no answers, so it passes the gate; but the load that this T4 can carry is not limited by cache size, so it did not raise capacity.

### S09: The tuned stack against the defaults, with repeats

**Design reference:** End to end: what the tuning loop buys, measured against noise.

**What this tests.** Assemble the tuned configuration only from settings the earlier scenarios showed to help, run it and the defaults on the same traffic with three different seeds each, and compare capacity and goodput per GPU against the run-to-run spread.

The design says that tuning can yield about 40% more goodput per GPU. Here the tuned stack is assembled by a function that reads the evidence of S04 to S08 and adopts a setting only where it beat the noise and passed its gate; then it and the defaults are each run with three seeds, and the difference is judged against the run-to-run range. The final evaluation of this scenario read its six load runs back from the state bucket (`--reuse`), after I changed the check that judges them; the note in the scenario says what was changed and why.

![S09: The tuned stack against the defaults, with repeats](diagrams/appendix/a22-evidence/S09.png)

![S09: bars, drawn from the run data](diagrams/appendix/a22-evidence/S09-bars.png)

![S09: curve, drawn from the run data](diagrams/appendix/a22-evidence/S09-curve.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # the tuned configuration, assembled from the evidence of S04 to S08
routing: prefix (S06)
engine arguments: --dtype half --max-model-len 4096 --gpu-memory-utilization 0.90
model: Qwen/Qwen3-4B

$ kubectl apply -f baseline.yaml   # baseline: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
baseline: servers s-baseline-0, s-baseline-1, s-baseline-2, s-baseline-3, router policy rr

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                            READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-baseline-64f466c8c5-p4t8v     1/1     Running   0          2m28s   10.70.29.113   ip-10-70-28-87.ec2.internal   <none>           <none>
s-baseline-0-7445b9fdbf-gbkpg   1/1     Running   0          2m30s   10.70.32.244   ip-10-70-35-3.ec2.internal    <none>           <none>
s-baseline-1-87b8d5798-qs5jv    1/1     Running   0          2m30s   10.70.32.241   ip-10-70-35-3.ec2.internal    <none>           <none>
s-baseline-2-697f86488c-9lj67   1/1     Running   0          2m29s   10.70.37.209   ip-10-70-35-3.ec2.internal    <none>           <none>
s-baseline-3-75697bcb96-qkb4n   1/1     Running   0          2m29s   10.70.32.243   ip-10-70-35-3.ec2.internal    <none>           <none>

$ # baseline: load run s09-seed1-baseline read back from the state bucket
target http://r-baseline:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s09-seed1-baseline   # baseline
error: error from server (NotFound): jobs.batch "lg-s09-seed1-baseline" not found in namespace "serving"

$ # baseline, seed 1
capacity 0.64 req/s/GPU, goodput 70.8 tok/s/GPU at 0.6 req/s/GPU
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.323           48.4          1.206           50.3
          0.6          70.76          0.978          0.386           64.7          1.367          107.0
          0.9          83.31          0.742          0.847          249.4          1.589          436.6
          1.2          16.07          0.089          1.578         1106.2          2.069         1122.2
          1.5           6.64          0.033          2.666         1231.6          3.038         1385.2

$ # baseline: load run s09-seed2-baseline read back from the state bucket
target http://r-baseline:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s09-seed2-baseline   # baseline
error: error from server (NotFound): jobs.batch "lg-s09-seed2-baseline" not found in namespace "serving"

$ # baseline, seed 2
capacity 0.65 req/s/GPU, goodput 62.8 tok/s/GPU at 0.6 req/s/GPU
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.01            1.0          0.297           55.1          1.188           57.6
          0.6          62.76            1.0          0.324           58.5          1.197           89.2
          0.9          66.37          0.685          0.918          274.8          1.634          308.6
          1.2          30.11          0.204          2.121         1170.4           2.75         1222.9
          1.5           6.19          0.037          1.861         1216.1          2.289         1247.4

$ # baseline: load run s09-seed3-baseline read back from the state bucket
target http://r-baseline:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s09-seed3-baseline   # baseline
error: error from server (NotFound): jobs.batch "lg-s09-seed3-baseline" not found in namespace "serving"

$ # baseline, seed 3
capacity 0.65 req/s/GPU, goodput 64.8 tok/s/GPU at 0.6 req/s/GPU
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          30.59            1.0          0.326           48.2          1.188           49.3
          0.6          64.84          0.994          0.426           60.8          1.278          142.3
          0.9          82.99          0.713          0.736          270.4          1.369          362.9
          1.2          30.92          0.212          1.354         1115.5          2.854         1210.9
          1.5           3.34          0.019          5.886         1413.5          6.805         1452.2

$ kubectl apply -f tuned.yaml   # tuned: 4 x Qwen3-4B on 1 GPU(s) [--dtype half --max-model-len 4096 --gpu-memory-utilization 0.90]
tuned: servers s-tuned-0, s-tuned-1, s-tuned-2, s-tuned-3, router policy prefix

$ kubectl -n serving get pods -o wide -l role=experiment
NAME                         READY   STATUS    RESTARTS   AGE     IP             NODE                          NOMINATED NODE   READINESS GATES
r-tuned-7f884bd8c6-zblvd     1/1     Running   0          2m29s   10.70.29.112   ip-10-70-28-87.ec2.internal   <none>           <none>
s-tuned-0-595b59df89-dcrp2   1/1     Running   0          2m31s   10.70.32.245   ip-10-70-35-3.ec2.internal    <none>           <none>
s-tuned-1-7874648d4-lmcdx    1/1     Running   0          2m31s   10.70.37.210   ip-10-70-35-3.ec2.internal    <none>           <none>
s-tuned-2-874cf6dff-gl6cc    1/1     Running   0          2m30s   10.70.37.211   ip-10-70-35-3.ec2.internal    <none>           <none>
s-tuned-3-57fb654ff8-f4x7d   1/1     Running   0          2m30s   10.70.32.246   ip-10-70-35-3.ec2.internal    <none>           <none>

$ # tuned: load run s09-seed1-tuned read back from the state bucket
target http://r-tuned:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s09-seed1-tuned   # tuned
error: error from server (NotFound): jobs.batch "lg-s09-seed1-tuned" not found in namespace "serving"

$ # tuned, seed 1
capacity 0.83 req/s/GPU, goodput 71.7 tok/s/GPU at 0.6 req/s/GPU
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.09            1.0          0.354           48.9          1.142           55.8
          0.6          71.73          0.995          0.365          107.7          1.141           59.0
          0.9         104.39          0.937          0.528          152.1          2.121          443.0
          1.2          51.13          0.384          0.703          242.4           1.89          310.9
          1.5          25.33          0.148           0.82          301.4          1.937         1146.4

$ # tuned: load run s09-seed2-tuned read back from the state bucket
target http://r-tuned:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s09-seed2-tuned   # tuned
error: error from server (NotFound): jobs.batch "lg-s09-seed2-tuned" not found in namespace "serving"

$ # tuned, seed 2
capacity 0.90 req/s/GPU, goodput 94.6 tok/s/GPU at 0.9 req/s/GPU
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          35.01            1.0          0.303           52.9          1.133           55.3
          0.6          62.05          0.982          0.313          125.4           1.12           56.6
          0.9          94.62          0.957          0.526          145.0          1.347          199.0
          1.2          64.56          0.438          0.472          205.0           1.96          988.0
          1.5          25.94          0.154          0.663          284.0          2.058         1094.8

$ # tuned: load run s09-seed3-tuned read back from the state bucket
target http://r-tuned:8000, 4 GPU(s)

$ kubectl -n serving logs job/lg-s09-seed3-tuned   # tuned
error: error from server (NotFound): jobs.batch "lg-s09-seed3-tuned" not found in namespace "serving"

$ # tuned, seed 3
capacity 0.78 req/s/GPU, goodput 65.1 tok/s/GPU at 0.6 req/s/GPU
    req/s/GPU        goodput         attain  chat TTFT p95   chat ITL p95  code TTFT p95   code ITL p95
          0.3          30.59            1.0          0.363           53.3          1.179           49.8
          0.6           65.1            1.0          0.452          116.0          1.517           70.1
          0.9         105.58          0.917          0.511          164.6          1.181          198.5
          1.2          68.62          0.513          0.674          246.1          1.524          317.9
          1.5          19.11            0.1          1.132          323.3          1.958         1220.7

$ # three seeds each
config      capacity req/s/GPU (mean, min-max)      goodput tok/s/GPU (mean, min-max)
baseline    0.64  (0.64-0.65)              66.1  (62.8-70.8)
tuned       0.84  (0.78-0.90)              77.1  (65.1-94.6)

$ # the result
goodput per GPU: 66.1 -> 77.1 tok/s (+17%); capacity per GPU: 0.64 -> 0.84 req/s (+30%)
run-to-run range of goodput: baseline 8.0, tuned 29.5 tok/s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | baseline: all replicas and the router start | Started in 138.2 s. |
| PASS | tuned: all replicas and the router start | Started in 140.6 s. |
| PASS | all six runs (two configurations, three seeds) completed with measurable goodput | A run with no level inside the targets would be a failure of the setup, not a result. |
| PASS | the change in mean capacity per GPU (the highest load with 95% of requests inside their targets, interpolated between levels) is larger than the larger run-to-run range | Difference of means +0.20 req/s/GPU against the larger range 0.12 (baseline 0.64 to 0.65, tuned 0.78 to 0.90). Three seeds give a range, not a confidence interval; a difference smaller than the range would not be claimed. |
| PASS | the tuned stack is not worse than the defaults in capacity or in goodput | Tuned 0.84 against baseline 0.64 req/s/GPU; goodput 77.1 against 66.1 tok/s/GPU. |

**Measured in this run:** Changed after seeing the data: the first version of the noise check compared goodput at the best load level. That figure can only take the values of the grid of load levels (0.3, 0.6, 0.9 ... req/s per GPU): a seed whose best level was 0.9 instead of 0.6 moves it by 30 tok/s, so its range (29.5 tok/s) is the width of one grid step, not noise, and the difference of means (+11.0 tok/s) is smaller than it, so goodput at the best level cannot support a claim either way. Capacity is interpolated between the levels on either side of the 95% line and is the measure the check now uses; the goodput figures stay in the table above. The design's target is 40% more goodput per GPU from tuning. Measured here: +17% in goodput and +30% in capacity, on a T4 with a 4B model and this traffic. The target was not met on this hardware and traffic (the comparison that counts is capacity; goodput at the best grid level is too coarse, see the note above). The tuned configuration contains only the settings the earlier scenarios measured as helping: routing: prefix (S06).

**Why you can trust this result.**

- The tuned configuration is printed with the evidence for each of its parts; nothing in it was chosen by hand after the earlier scenarios ran.
- Both stacks run on the same four GPUs, one after the other, with the same three seeds and the same load levels.
- The check compares the difference of the means with the larger of the two ranges, which is stricter than comparing the means alone.

**Why you get this output.**

- Only one setting survived the earlier scenarios: prefix-aware routing (S06). The engine settings of S04 did not beat their control, the host tier of S07 changed nothing that capacity could show, and the one quantized configuration that was faster (S08) failed its gate. So the tuned stack differs from the defaults in one thing.
- That one thing is worth about 30% more capacity on these four T4s, which is less than the design's 40%. The T4's cost is dominated by prompt work, and routing is the only lever here that removes some of it (by sending a repository to the server that already holds it); the others either do not touch it or trade it for something else.
- Goodput at the best load level moves less than capacity, and its noise is the width of the grid of load levels; the scenario's note explains why capacity is the measure that carries the claim.

### S10: Autoscaling on queue depth and on GPU utilisation

**Design reference:** Autoscaling: scale on the queue the user feels, not on a counter that saturates; cold start is the cost.

**What this tests.** Start with one replica at a quiet load, step the load to what four replicas can carry, and let KEDA scale the same Deployment on two different signals (the number of requests waiting plus running, and GPU utilisation); record when each decided, how long the new replicas took to be ready, and the share of requests inside their targets over time.

Both runs start with one replica at a quiet load, then step the load to what about three replicas can carry, and a KEDA ScaledObject scales the same Deployment on a Prometheus query. One run scales on the number of requests in the system (waiting plus running); the other scales on GPU utilisation. A router re-reads a headless Service every two seconds so that a new replica gets traffic as soon as it is Ready.

![S10: Autoscaling on queue depth and on GPU utilisation](diagrams/appendix/a22-evidence/S10.png)

![S10: scale, drawn from the run data](diagrams/appendix/a22-evidence/S10-scale.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # the settings come from S01's measurements
requests in the system (waiting + running) at the last healthy level of one GPU: 2.3  -> threshold per replica
one GPU's capacity 0.35 req/s -> quiet phase 0.17 req/s (half a replica), step to 1.11 req/s (3.2 replicas)
GPU utilisation counter scale: ratio; target 80% of one GPU per replica

$ kubectl apply -f auto-queue.yaml   # one server behind the router, which re-reads the headless service every 2 s
first replica Ready after 126.2 s

$ kubectl apply -f so-auto-queue.yaml   # KEDA ScaledObject, Prometheus trigger, 1 to 4 replicas
query: sum(vllm:num_requests_waiting) + sum(vllm:num_requests_running)
threshold per replica: 2.3

$ kubectl apply -f lg-s10-queue.yaml   # 0.17 req/s for 420 s, then a step to 1.11 req/s for 420 s

$ kubectl -n serving logs job/lg-s10-queue
rate 0.17: goodput 5.6 tok/s/gpu, attainment 1.00, completed 75/75
rate 1.11: goodput 26.5 tok/s/gpu, attainment 0.82, completed 474/474
done

$ kubectl apply -f auto-util.yaml   # one server behind the router, which re-reads the headless service every 2 s
first replica Ready after 126.2 s

$ kubectl apply -f so-auto-util.yaml   # KEDA ScaledObject, Prometheus trigger, 1 to 4 replicas
query: sum(DCGM_FI_DEV_GPU_UTIL_RATIO)
threshold per replica: 0.8

$ kubectl apply -f lg-s10-util.yaml   # 0.17 req/s for 420 s, then a step to 1.11 req/s for 420 s

$ kubectl -n serving logs job/lg-s10-util
rate 0.17: goodput 5.6 tok/s/gpu, attainment 1.00, completed 75/75
rate 1.11: goodput 32.2 tok/s/gpu, attainment 1.00, completed 474/474
done

$ # signal: queue
Ready replicas when the step arrived: 1 (mean over the quiet phase 1.0); replica-seconds used in the whole run: 1,195
decision to add replicas 19.50591540336609 s after the step; three replicas Ready after 150.82531332969666 s; most replicas Ready 4
each new replica took [125.0, 129.0, 130.0, 132.0] s from creation to Ready (the first took 126.2 s)
share of requests inside targets: quiet phase 1.00, step phase 0.82; back above 0.9 (two windows in a row) 150 s after the step
30 s windows after the step: 0s:0.54 30s:0.49 60s:0.84 90s:0.12 120s:0.52 150s:1.00 180s:1.00 210s:1.00 240s:1.00 270s:1.00 300s:1.00 330s:1.00 360s:1.00 390s:1.00

$ # signal: util
Ready replicas when the step arrived: 2 (mean over the quiet phase 1.6); replica-seconds used in the whole run: 1,535
decision to add replicas 51.99502754211426 s after the step; three replicas Ready after 19.78549575805664 s; most replicas Ready 4
each new replica took [123.0, 124.0, 126.0, 126.0] s from creation to Ready (the first took 126.2 s)
share of requests inside targets: quiet phase 1.00, step phase 1.00; back above 0.9 (two windows in a row) 0 s after the step
30 s windows after the step: 0s:1.00 30s:1.00 60s:1.00 90s:1.00 120s:1.00 150s:1.00 180s:1.00 210s:1.00 240s:1.00 270s:1.00 300s:1.00 330s:1.00 360s:1.00 390s:1.00

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | both runs completed: the quiet phase and the step were both driven and recorded | Each run is one Deployment behind the router with one signal driving KEDA. |
| PASS | signal queue: the autoscaler added replicas after the step (more than one replica became Ready) | Most replicas Ready: 4. |
| PASS | signal queue: a new replica is not useful at once: it took at least 60 s from creation to Ready | Creation to Ready took [125.0, 129.0, 130.0, 132.0] s with the image and weights already on the node's disk; with the model downloaded first it would be longer, and with a new GPU node longer again. This delay, not the decision, is the floor under any scale-up. |
| PASS | signal util: the autoscaler added replicas after the step (more than one replica became Ready) | Most replicas Ready: 4. |
| PASS | signal util: a new replica is not useful at once: it took at least 60 s from creation to Ready | Creation to Ready took [123.0, 124.0, 126.0, 126.0] s with the image and weights already on the node's disk; with the model downloaded first it would be longer, and with a new GPU node longer again. This delay, not the decision, is the floor under any scale-up. |
| PASS | at the quiet load (about half of one replica's capacity) the utilisation signal held more replicas than the queue signal did | Mean Ready replicas during the quiet phase: 1.0 (queue) against 1.6 (utilisation); Ready when the step arrived: 1 against 2. This check was added after the first run showed the pattern: it is a claim about that run's mechanism, stated here so that a different result would fail it. |

**Measured in this run:** Replica-seconds used in the run: queue 1,195, utilisation 1,535. Share of requests inside their targets during the step: queue signal 0.82, utilisation signal 1.00. First decision after the step: queue 19.50591540336609 s, utilisation 51.99502754211426 s. Quiet-phase attainment: queue 1.00, utilisation 1.00. Not run: scaling the number of GPU nodes (the GPU node group was not allowed to grow beyond the one node that EC2 would give), traffic-based scale from zero, and KServe canary rollouts.

**Why you can trust this result.**

- Replica counts are sampled from the Kubernetes API every 5 seconds during the run, and readiness delay is the difference between each pod's start time and its Ready transition, both from the pods' own status.
- Attainment over time is computed from the per-request records the load generator wrote, in 30-second windows from the moment of the step.
- The threshold for each signal is derived from S01's measurements and written down before the run.

**Why you get this output.**

- A new replica is not useful when it is created: it has to start the engine, load the weights, compile and capture graphs, and every replica here took more than two minutes (123 to 132 s) even with the image and weights already on the node's disk. Whatever the signal, requests that arrive in that window are served by the replicas that exist.
- The queue-based signal reacted to the step within about 20 seconds, asked for four replicas, and then waited two minutes for them; during that time the one replica was overloaded, and the share of requests inside their targets was low until the new replicas were Ready.
- The utilisation signal had already scaled up before the step. One T4 serving even a quiet load (about half of its capacity) reads about 80% busy, which is the signal's target, so it asked for a second and a third replica during the quiet phase; they were Ready when the step arrived and the targets held. That cost 28% more replica-seconds over the run. It is a good outcome here because the quiet load happened to read just over the threshold; a quieter load would have given the signal nothing to act on, and above about 0.9 requests per second per GPU the counter sits at 100% and cannot say whether three or four replicas are needed.
- Only four GPUs exist on the node, so the largest scale-up is four replicas. Growing the number of GPU nodes is not tested: EC2 would give one four-GPU node, and a second would have to be requested in the same capacity shortage.

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

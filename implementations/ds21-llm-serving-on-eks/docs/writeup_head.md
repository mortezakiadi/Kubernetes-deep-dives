---
id: appendix-design-21-eks
title: "Appendix V: Design 21 built and tested on AWS (EKS, vLLM, four T4 GPUs)"
short: "V. Design 21 on AWS (built and tested)"
---

This appendix describes a working, scaled-down implementation of [Design 21: Deploying and tuning high-throughput LLM serving](#design-llm-serving). I built it with Terraform in one AWS account and one region: a standard EKS cluster with two system nodes, one CPU node for the load generators and the router, and one GPU node with four NVIDIA T4 GPUs. vLLM 0.33.0 serves a 4-billion-parameter dense model and a 30-billion-parameter mixture-of-experts model; Prometheus and KEDA watch and scale it; a load generator replays two kinds of traffic and records, for every request, the time to the first token and the gap between the following tokens.

The order is the one a reader needs: first the context, then the software I used, the topology and instances, and the architecture, then how it is implemented, and then the scenarios I tested, with the commands I ran, what they printed, why the result can be trusted and why you get that output.

**What "scaled down" means here, and the price of it.** The design's examples are 14B and 70B models on H100s with 20,000-token prompts. I asked EC2 for every four-GPU type I could use (L40S, L4, A10G, in three regions) and was refused for capacity for hours on all of them; the one four-GPU type it would launch was `g4dn.12xlarge`, four T4s with 16 GB each, no NVLink, no FP8 and no bf16. So the models are smaller (4B dense, 30B mixture of experts in int4), the prompts are shorter (chat about 300 tokens, code about 1,300 with a shared 1,200-token repository prefix), and the targets are loosened to what a T4 can meet (first token in 2 s for chat and 5 s for code, a gap between tokens of 150 ms and 200 ms). Every comparison inside this appendix is fair, because the traffic and the targets are the same for everything that is compared. The absolute numbers are not the numbers of an H100: the T4's compute is so low that the work of reading a prompt, which on a large GPU is a small step, is the thing that decides almost every result here. Where that makes a result specific to this GPU, the scenario says so.

**Why this substrate.** The design's levers (how to split a model over GPUs, which engine settings to change, whether to separate prompt work from token work, how to route, whether to offload cache to host memory, whether to quantize, how to scale) are all decisions about one node's GPUs and the traffic in front of them. One node with four GPUs on one PCIe fabric is the smallest thing on which all of them can be exercised for real, and the one node EC2 would give me. Multi-node expert parallelism, which needs a network between nodes, is not built; the appendix ends with what that leaves open.

{{RESULT_SUMMARY}}

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

"""Hand-written text for each scenario of the appendix: what it shows, why the result can be trusted, and why the output is what it is.
The numbers are in the evidence (commands, tables and checks); nothing here restates a measurement that could change from run to run.
"""
PROSE = {
    "S01": {
        "intro": "Before any experiment I show what the experiments stand on: the node and its four GPUs, how those GPUs are connected, the defaults the engine picks for them, and the traffic. Then I make one prediction that can be checked from outside the engine, the size of the KV cache, and measure one GPU with default settings under rising load. Every later scenario is read against this curve.",
        "trust": [
            "The node and GPU figures are read from the Kubernetes API and from `nvidia-smi` inside the cluster; the engine defaults are read from the engine's own configuration object, not from documentation.",
            "The KV capacity is predicted from the model's `config.json` and the memory the engine reports, and compared with the capacity the engine reports separately; the two come from different lines of the log.",
            "The load generator is open loop: it sends each request when the trace says, whether or not the server has caught up, so an overloaded server cannot slow the offered load down and look better than it is.",
            "Every request's first-token time and gap between tokens is recorded and written to the state bucket; the table is computed from those records after the run.",
        ],
        "why": [
            "A T4 has 16 GB of memory and 320 GB/s of bandwidth. The 4B model's weights take 7.6 GiB, so about 4.4 GiB is left for the cache, which at 147,456 bytes per token is about 31,700 tokens: that is the whole budget for every request in flight and every cached prefix.",
            "Below the knee the server keeps up and nearly every request meets both targets. Past it, requests queue behind prompt work: a step that carries a 2,048-token chunk of someone's prompt takes about a second on this GPU, and every running request waits for it, so inter-token latency, not throughput, is what fails first.",
            "The waiting queue stays at zero because the engine admits up to 256 sequences into one batch. The number of requests in the system (waiting plus running) is what grows with load, and it is the signal S10 uses.",
            "GPU utilisation is already high at a fraction of the capacity, because a GPU running a few decodes is 'busy' all the time; the counter says busy, not late.",
        ],
    },
    "S02": {
        "intro": "One 4B model, four GPUs, three ways to lay it out: four independent copies, two pairs that split each layer between two GPUs, or one group of four. I predict the memory each layout leaves for the cache from the weights alone, then measure goodput per GPU on identical traffic.",
        "trust": [
            "The three layouts run one after another on the same node with the same trace and the same seed; the router (where there is one) is the same code.",
            "The KV memory figures are the engine's own start-up report, compared with a prediction made from the first layout's numbers before the others were read.",
            "Capacity is interpolated between the last load level that kept 95% of requests inside their targets and the first that did not; both levels are in the table.",
        ],
        "why": [
            "Sharding the weights over n GPUs frees (1 - 1/n) of them on every GPU for the cache; that is why the total KV capacity rises from four copies to one wide group.",
            "My prediction was that four copies would win, because a 4B model needs no sharding. It did not hold. On a T4 the thing that hurts most is the long prompt step that stalls every running request; split four ways, that step is a quarter as long, so inter-token latency under load stays lower and the wider group stays inside its targets at a higher load. The all-reduce over PCIe costs something, but less than that gain at this size.",
            "The result belongs to this GPU: on a card with much more compute per byte of bandwidth the prompt step is short and four copies would likely win again. The method (predict, measure, compare) is the part that carries over.",
        ],
    },
    "S03": {
        "intro": "A mixture-of-experts model has most of its weights in experts that each token visits only a few of. Qwen3-30B-A3B has 128 experts per layer and uses 8 per token. Four T4s cannot hold it in 16 bits, so the checkpoint is the int4 (GPTQ) one. I run it two ways: tensor parallel over four GPUs, and four data-parallel engines that share the experts between them (expert parallelism), read the engine's account of where the experts went, and then ask for expert load balancing.",
        "trust": [
            "The expert placement and the all-to-all implementation are the engine's own log lines, quoted; GPU memory is read from `nvidia-smi` while the server runs.",
            "Both layouts answer the same 100 exact-answer questions and their answers are compared one by one, so a wrong layout would show as disagreement.",
            "The request for expert load balancing is made for real and the engine's answer is quoted; nothing about it is inferred.",
        ],
        "why": [
            "With expert parallelism each rank holds 32 of the 128 experts and its own copy of attention, so a token's work is done on the rank that owns it and only the expert inputs and outputs travel. With tensor parallelism every layer is split four ways and every layer ends in an all-reduce, which on a PCIe fabric with no direct GPU-to-GPU path costs far more per layer than on NVLink; that is the difference in latency under load.",
            "The model's active parameters are 3B, so near idle both layouts answer quickly; the layouts separate as load rises and the cost of communication is paid on every step.",
            "The engine refuses expert load balancing for this checkpoint because its int4 expert kernels cannot be swapped at run time; that is the engine's limit for this format, not a configuration mistake, and the effect of balancing is therefore not measured.",
        ],
    },
    "S04": {
        "intro": "Engine settings are cheap to change and easy to believe in. I change one at a time, on one GPU each, four at a time on the four GPUs of the node, with the same traffic and the same seed. A control (the default, run again on another GPU in another round) measures how far two identical runs differ, and a setting counts as an improvement only if it beats that difference.",
        "trust": [
            "Each setting runs with the same trace as every other, and the unchanged control is in the table: it shows the size of the noise rather than assuming it.",
            "Speculation's counters (drafts, drafted tokens, accepted tokens) come from the server's own metrics, read before and after the sweep.",
            "The verdict for each setting is computed from the measured capacity and the noise by a rule written in the scenario before the numbers were read.",
        ],
        "why": [
            "A smaller prefill chunk shortens the step in which running requests wait, so inter-token latency at moderate load falls (the table shows it falling by three quarters); but the prompt itself takes more steps to finish, so first-token time rises, and with these targets the second effect costs more requests than the first saves.",
            "The batch limit does not matter here because the T4 runs out of speed long before it runs out of batch slots: at most about thirty requests are running at the overload level and the default allows 256.",
            "Prefix caching helps the code requests (their first-token wait is longer without it) but the cache holds only part of the 48 repositories, which is what S06 and S07 work on.",
            "N-gram speculation accepted about a fifth of the four tokens it drafted and about a third of the two it drafted, but the prompts here are random words, so the drafts rarely continue the way a real code or document prompt would; the setting was neutral on this traffic.",
        ],
    },
    "S05": {
        "intro": "Prefill is compute-bound and decode is memory-bound, and when they share a server the first stalls the second. Disaggregation puts them on different servers and copies the KV cache from one to the other. Here that copy goes through NIXL, over the node's PCIe fabric, between two pods. I use a prompt-heavy mix (70% code) because that is where disaggregation should pay, and I check from the servers' own counters that the cache really moved.",
        "trust": [
            "The transfer counters (number of transfers, bytes, time, failures) are the connector's own metrics on the decode servers; the share of decode prompt tokens taken from the transfer is the engine's external-prefix-cache counter.",
            "The unified baseline uses the same four GPUs, the same router code (least loaded) and the same trace.",
            "The two predictions are written in the scenario before the data and reported with the result.",
        ],
        "why": [
            "The decode servers never compute prompts, so their steps contain only decodes and inter-token latency stays low and flat as load rises.",
            "Every request pays for the copy: the counters show about 100 MB of KV cache per request moved at about 120 MB/s over a path that, on this node, runs through host memory, so the first token comes about 0.8 seconds later for every request and more when the copies queue. That eats a large part of a 2-second target before anything is computed.",
            "The prefill servers become the bottleneck. With one prefill GPU feeding three decoders the prefill GPU does all the prompt work and the decoders idle; with two and two the balance is better but the copy cost remains.",
            "On hardware with a direct GPU-to-GPU path and more compute per prompt, the same layout would behave differently; this result is for four T4s on one PCIe fabric.",
        ],
    },
    "S06": {
        "intro": "Four identical replicas, one trace, three ways to choose a replica: in turn, the one with the fewest requests in flight, or the one that most recently saw the same prompt prefix. The router is a small program of mine that stands in for the gateway's endpoint picker, and the appendix says so. What I check is what the servers report: how often a prompt's tokens were found in a cache.",
        "trust": [
            "The cache hit rate is the sum of the four servers' own prefix-cache counters over the run, not the router's estimate; the router's own counter of matches is shown next to it.",
            "The three policies see the same trace in the same order; only the choice of server differs.",
            "The imbalance check uses the router's counts of requests per replica.",
        ],
        "why": [
            "With round robin each repository's requests reach all four replicas, so each replica must keep all 48 repositories (about 57,000 tokens) in room for about 20,000 and keeps evicting them. A policy that sends a repository to the replica that already holds it lets each replica hold about a quarter.",
            "A shorter shared text, like the chat system prompt (about 100 tokens), is not worth following: sending most chat requests to one replica for a 100-token saving unbalances the fleet for no gain. The router therefore follows a match only if it covers at least two 256-character blocks, and it skips a replica that is well above the average load.",
            "A higher hit rate saves prompt work, and prompt work is what stalls decodes on a T4, so first-token waits of code requests fall to a quarter and capacity rises by about a sixth; the rest of each request still costs what it costs.",
        ],
    },
    "S07": {
        "intro": "When the repositories do not all fit in GPU memory, a second tier in host memory can keep the ones that were evicted. Three single-GPU servers get 0, 8 and 24 GiB of host memory for it (plus an unchanged control) and a code-heavy mix, and I read how much the tier served, what moving the data cost, and whether capacity changed.",
        "trust": [
            "The tier's use is read from the connector's own counters: tokens found in the host tier and bytes moved over PCIe.",
            "The four servers share the node and the traffic is the same trace with the same seed.",
            "The control (a second unchanged server) gives the noise against which any gain is judged.",
        ],
        "why": [
            "A quarter to two fifths of the prompt tokens came from the host tier instead of being recomputed, and the larger tier served more. At this model's 147,456 bytes per token, 8 GiB holds about 58,000 tokens and 24 GiB about 175,000, against about 57,600 tokens of repository prefixes (48 x 1,200) plus the chat and per-request text that also passes through; the smaller tier is therefore nearly full of useful prefixes and the larger one is not.",
            "Capacity did not move beyond the noise. The prompt tokens that came back from host memory were cheap to recover, but the load level at which requests stop meeting their inter-token target is set by the GPU's compute in each step, not by recovering a prefix.",
            "Moving data over PCIe is not free (the copy time is in the table), and it competes with the other three GPUs of the node for the same bus.",
        ],
    },
    "S08": {
        "intro": "Smaller numbers make a model faster and leave more room for the cache, and they change its answers. The gate is a written rule, applied before any speed is read: at most 3 points lost on 400 exact-answer questions, and at least 90% of answers identical to the fp16 model's. Four configurations are served: fp16, int4 weights (AWQ), an int8 KV cache, and both. FP8 and FP4 need hardware a T4 does not have, and are not run.",
        "trust": [
            "The questions are generated from a seed, have exact numeric answers, and are asked at temperature 0 with a short answer, so a model with a damaged number format shows it as a wrong digit.",
            "Agreement is paired: the same question's answers from the two configurations are compared, which is far less noisy than comparing two accuracies of 400 questions each (about 2 points of standard error).",
            "The gate was written into the scenario before the answers were read, and the decision of each configuration is recorded whether it passes or not.",
        ],
        "why": [
            "Int4 weights take 2.5 GiB instead of 7.6 GiB, so the cache more than doubles and the server carries about half as much load again (capacity 0.65 against 0.44 requests per second); the engine's start-up report shows the memory figures.",
            "Average accuracy did not fall, but about one answer in nine changed, mostly on the chained and word problems, and the gate refuses a configuration that changes that many answers whether or not the average moved: the average hides which questions got worse.",
            "The int8 KV cache holds twice the tokens and changes almost no answers, so it passes the gate; but the load that this T4 can carry is not limited by cache size, so it did not raise capacity.",
        ],
    },
    "S09": {
        "intro": "The design says that tuning can yield about 40% more goodput per GPU. Here the tuned stack is assembled by a function that reads the evidence of S04 to S08 and adopts a setting only where it beat the noise and passed its gate; then it and the defaults are each run with three seeds, and the difference is judged against the run-to-run range. The final evaluation of this scenario read its six load runs back from the state bucket (`--reuse`), after I changed the check that judges them; the note in the scenario says what was changed and why.",
        "trust": [
            "The tuned configuration is printed with the evidence for each of its parts; nothing in it was chosen by hand after the earlier scenarios ran.",
            "Both stacks run on the same four GPUs, one after the other, with the same three seeds and the same load levels.",
            "The check compares the difference of the means with the larger of the two ranges, which is stricter than comparing the means alone.",
        ],
        "why": [
            "Only one setting survived the earlier scenarios: prefix-aware routing (S06). The engine settings of S04 did not beat their control, the host tier of S07 changed nothing that capacity could show, and the one quantized configuration that was faster (S08) failed its gate. So the tuned stack differs from the defaults in one thing.",
            "That one thing is worth about 30% more capacity on these four T4s, which is less than the design's 40%. The T4's cost is dominated by prompt work, and routing is the only lever here that removes some of it (by sending a repository to the server that already holds it); the others either do not touch it or trade it for something else.",
            "Goodput at the best load level moves less than capacity, and its noise is the width of the grid of load levels; the scenario's note explains why capacity is the measure that carries the claim.",
        ],
    },
    "S10": {
        "intro": "Both runs start with one replica at a quiet load, then step the load to what about three replicas can carry, and a KEDA ScaledObject scales the same Deployment on a Prometheus query. One run scales on the number of requests in the system (waiting plus running); the other scales on GPU utilisation. A router re-reads a headless Service every two seconds so that a new replica gets traffic as soon as it is Ready.",
        "trust": [
            "Replica counts are sampled from the Kubernetes API every 5 seconds during the run, and readiness delay is the difference between each pod's start time and its Ready transition, both from the pods' own status.",
            "Attainment over time is computed from the per-request records the load generator wrote, in 30-second windows from the moment of the step.",
            "The threshold for each signal is derived from S01's measurements and written down before the run.",
        ],
        "why": [
            "A new replica is not useful when it is created: it has to start the engine, load the weights, compile and capture graphs, and every replica here took more than two minutes (123 to 132 s) even with the image and weights already on the node's disk. Whatever the signal, requests that arrive in that window are served by the replicas that exist.",
            "The queue-based signal reacted to the step within about 20 seconds, asked for four replicas, and then waited two minutes for them; during that time the one replica was overloaded, and the share of requests inside their targets was low until the new replicas were Ready.",
            "The utilisation signal had already scaled up before the step. One T4 serving even a quiet load (about half of its capacity) reads about 80% busy, which is the signal's target, so it asked for a second and a third replica during the quiet phase; they were Ready when the step arrived and the targets held. That cost 28% more replica-seconds over the run. It is a good outcome here because the quiet load happened to read just over the threshold; a quieter load would have given the signal nothing to act on, and above about 0.9 requests per second per GPU the counter sits at 100% and cannot say whether three or four replicas are needed.",
            "Only four GPUs exist on the node, so the largest scale-up is four replicas. Growing the number of GPU nodes is not tested: EC2 would give one four-GPU node, and a second would have to be requested in the same capacity shortage.",
        ],
    },
}

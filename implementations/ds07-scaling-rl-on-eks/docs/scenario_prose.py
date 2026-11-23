"""Hand-written text for each scenario of the appendix: what it shows, why the result can be trusted, and why the output is what it is.
The numbers are in the evidence (commands and checks); nothing here restates a measurement that could change from run to run.
"""
PROSE = {
    "S01": {
        "intro": "Before any run, I show what the experiments stand on: the GPUs the scheduler can hand out and where they are, the controllers that restart, place and scale the workloads, the rollout fleet's layout, and the network between the trainer's node and the rollout nodes, measured and not assumed.",
        "trust": [
            "The node and GPU figures are read from the Kubernetes API, not from the Terraform that asked for them.",
            "The network figure is iperf3's receiver-side result, with four parallel streams for fifteen seconds, between a pod on the trainer's node and a pod on a rollout node.",
            "The world size is what the engine itself reports to anyone who asks, which is what the trainer asks before it forms the NCCL group.",
        ],
        "why": [
            "All three GPU nodes are in one zone because the node groups are pinned to one subnet.",
            "The rate is close to 10 Gbit/s because the instance type's network performance is 'up to 10 Gbit/s', and nothing else in the path is slower.",
            "The world size is two because the leader pod's engine and the worker pod's engine are both data-parallel ranks of one server.",
        ],
    },
    "S02": {
        "intro": "The design's first rung is a recipe search at a few percent of the budget, with the held-out score fitted against compute so that the recipe with the higher ceiling is chosen and not the one that happens to be ahead. I ran three recipes of the same model for the same 80 steps, fitted the design's saturating curve to each, and asked the platform what it can and cannot conclude.",
        "trust": [
            "Every recipe is evaluated at the same nine steps on the same 200 problems, four samples each, by the fleet's own API, so the evaluation measures the policy the fleet is serving.",
            "The compute axis is the run's own clock times the three GPUs it holds, with the time spent evaluating taken out.",
            "The ranking rule and the extrapolation check are the design's own statements turned into code; the rule is also shown on three made-up ceilings so that its behaviour does not depend on how the real ones land.",
        ],
        "why": [
            "The fit has wide error bars because 80 steps did not take any recipe past the midpoint of its curve; the design warns that a ceiling estimated before the midpoint is weak, and the covariance of the fit says so.",
            "The recipes are not ranked because their intervals overlap. That is the correct output of the rule, not a failure to run it.",
            "The second half of each run is predicted from the first half to within about the evaluation's noise, because at this length the curves are close to straight lines and a saturating curve fits them.",
        ],
    },
    "S03": {
        "intro": "The design says that at 14 minutes a step nobody knows whether to buy rollout GPUs, optimise the trainer or fix the network. Here a step takes about four seconds, and the same question has an answer: the trainer's own timers split it, and the split is weighted by the GPUs each stage holds. Then I change the setting that the largest stage points to, and measure what it buys and what it costs.",
        "trust": [
            "The stage times are the trainer's own timers for each step (weight sync, backward, log-probabilities, optimizer, waiting), written with the step's other metrics; the step's wall clock comes from the run's own timestamps.",
            "Medians are used for the breakdown, because the steps that evaluate or checkpoint include that work and would pull a mean up.",
            "The comparison run differs from the baseline in one setting, the interval of the weight push, and starts from the same weights.",
        ],
        "why": [
            "The weight sync is the largest stage because the trainer sends its fp32 master weights to engines that keep bf16, over a network that carries about 1.2 GB a second, and the engines generate nothing while they load them.",
            "Two of the three GPUs are idle for the whole transfer, which is why the share of reserved GPU-seconds lost to the sync is larger than its share of the step.",
            "Pushing every fourth step removes three transfers in four. The engines then sample from weights up to three updates behind, which the importance ratio corrects; S06 measures the size of that correction.",
        ],
    },
    "S04": {
        "intro": "The design's long-tail section is about answers whose lengths vary fifty-fold and a synchronous step that waits for the slowest. The asynchronous trainer should hide that, and it does, up to a point: this scenario measures what an over-sized rollout queue costs, what turning the asynchrony off costs, and what happens to the trainer when the answers become ten times longer.",
        "trust": [
            "The three queue runs differ in one setting each (rollouts in flight, staleness bound) and share everything else, including the starting weights and the prompt order.",
            "Dropped samples, pause times and GPU hours come from the trainer's own metrics and the run's own clock.",
            "The long-answer run uses the same model and prompts with its reasoning switched on, so the only change is the length axis.",
        ],
        "why": [
            "A fleet allowed 512 rollouts in flight generates thousands of samples while the trainer uses a few dozen a step. A sample that is older than the staleness bound when the trainer reaches it is dropped, so those tokens were paid for and thrown away.",
            "The request to pause the fleet waits behind the work in flight, which is why the weight sync stalls when the queue is long.",
            "With the staleness bound at zero the next batch of rollouts starts only after the weights load, so generation and training no longer overlap and the trainer waits for the first groups.",
            "With long answers the slowest group of a step decides when the trainer can go on. Asynchrony hides this while the fleet is ahead; once the answers are long enough that it is not, the trainer's wait shows up.",
        ],
    },
    "S05": {
        "intro": "The design's section on larger models says to size the weight push like a serving path. I pushed models of 0.6B to 8B parameters from the trainer's GPU into the rollout fleet, with the same NCCL call the training runs use, with one engine and with two, packed and unpacked, then used the measured rate to price a model of 235B, and finally reproduced the out-of-memory failure that made me run the engines at 70% of the GPU.",
        "trust": [
            "The engines are started with dummy weights and the trainer builds the same architecture with random weights, so nothing is downloaded and the measurement is of the transfer alone.",
            "Each size is pushed six times and the first push, which includes setup, is reported separately from the median of the rest.",
            "The network figure comes from iperf3 on the same pods in the same scenario.",
        ],
        "why": [
            "The push rate is close to the network's rate because a packed NCCL broadcast over sockets is bandwidth-bound once the model is larger than a buffer.",
            "A second engine costs less than double because the trainer sends each byte once into a group that includes both receivers.",
            "The 235B figure is arithmetic on the measured rate, not a measurement. It shows how far this network is from the design's 10 seconds: the design assumes a GPU fabric.",
            "At 95% of the GPU's memory the receive buffers (two copies of 1 GiB) do not fit, the engine reports an out-of-memory error, and the trainer's side of the collective waits for a peer that has stopped; the job's deadline ends it.",
        ],
    },
    "S06": {
        "intro": "The design says the mismatch between the rollout engine and the trainer grows with scale and with lower precision, and that the tail of the ratio, not its mean, is what to watch. I had the engine sample 256 answers and return the probability of every token it chose, recomputed the same probabilities with the trainer's own forward pass, and compared the two under four precisions. Then I read the same quantity from the logs of a real run.",
        "trust": [
            "Both probabilities are for the same tokens given the same prompts; only the engine that computed them differs.",
            "The four settings differ in one thing each (the trainer's precision, the engine's weight precision, the engine's KV-cache precision).",
            "The last check reads the trainer's own per-step metrics from the S02 run, which are what the platform would alert on.",
        ],
        "why": [
            "The two engines use different kernels and reduction orders, so the log-ratio between them is small and not zero even with the same weights and precision.",
            "FP8 weights round every parameter; the engine samples from a nearby distribution, and the trainer's probabilities for the tokens it chose differ more. The tail grows faster than the mean.",
            "A model of 0.6B parameters in bf16 has little mismatch to start with. The design's claim is that this grows with the model, the length and the precision; this scenario shows the precision axis and the shape of the tail, and says nothing about 235B.",
        ],
    },
    "S07": {
        "intro": "The design's environment tier is a service of its own, scaled on its queue, so that the rollout GPUs do not wait for sandboxes. I gave every answer four tool calls into a sandbox pool and compared a fixed pool of two workers with a pool that KEDA scales on the length of its queue, measuring what the trainer sees.",
        "trust": [
            "The two runs share everything except the pool: the same recipe, the same code task, the same calls per answer and the same time per call.",
            "The worker count and the queue length are sampled from the cluster every five seconds during each run.",
            "Every call is a message that a worker removes with a blocking pop and answers on its own result list, so the final queue length and the count of calls served are exact.",
        ],
        "why": [
            "Two workers serve about six calls a second. A group of eight answers with four calls each is thirty-two calls, the trainer needs about twenty groups for one step, and the answers are short enough that a step takes that many. Scoring takes as long as the queue.",
            "KEDA reads the list's length every five seconds and sets the replica count to the length divided by the target per worker, up to the maximum; scaling on CPU would react only after the workers were already busy.",
            "The pool shrinks after a cooldown once the list is empty.",
        ],
    },
    "S08": {
        "intro": "The design says slow drifts only matter on long runs, and that alerts should combine trends. I took a recipe whose step size is about three times too large, watched the signals the design names, fired a combined alert, found the last healthy checkpoint by query, and restarted from it twice: once with the same settings and once with the healthy recipe's step.",
        "trust": [
            "The alert rule's thresholds were set on the healthy run of S02 so that it never fires there, and were then applied unchanged to the unstable run.",
            "Damage is defined from the held-out score alone (more than 0.10 below the best so far) and is measured every five steps, independently of the alert's inputs.",
            "The checkpoint the query returns is checked to be in the bucket, and both restarts load that checkpoint's weights from it.",
        ],
        "why": [
            "A step size about three times too large moves the policy further than the importance ratio's clip can contain. Entropy falls, answers lengthen or collapse, groups stop carrying a gradient and more tokens are masked by the clip: the combined rule fires when two of those hold for three steps in a row.",
            "The held-out score falls later than the alert, because it is measured every five steps and the policy needs time to degrade.",
            "A restart with the same step size meets the same instability. A restart with a smaller step, a fresh optimizer and a different prompt order does not.",
        ],
    },
    "S09": {
        "intro": "The design says to make failures cheap and not rare. I started a run with checkpoints and a restart policy, broke it three ways (a rollout engine, the trainer's pod, the trainer's whole node), and measured how long each took to recover, how much work was repeated, and what share of the reserved GPU time went into progress.",
        "trust": [
            "Each failure is inflicted by a command shown in the transcript, once the run is past step 14 and a checkpoint is complete in the bucket.",
            "Recovery times come from the run's own timestamps against the moment of the command; the restored checkpoint is read from the new pod's log.",
            "Goodput counts each step once, at its last completion, and divides by the time from the first record to the last, times three GPUs.",
        ],
        "why": [
            "The JobSet's failure policy recreates the trainer pod, and the new pod restores the newest complete checkpoint from the bucket before it starts, so nothing depends on the old node.",
            "Losing an engine breaks the NCCL group that the trainer shares with the engines. The trainer's next collective never returns, so the watchdog ends the process after its timeout and the restart policy takes over. The design assumes the engine can be replaced while the run goes on; with this weight-transfer path the trainer has to re-form the group.",
            "The checkpoint interval bounds the repeated work. Everything else in the recovery time is the pod's start, the packages, the model and checkpoint load and the NCCL handshake.",
        ],
    },
    "S10": {
        "intro": "The ladder is the design's answer to 'when can we start the big run?': after the rung below it is understood. I wrote the ladder as overlay files that each change one axis, decided from measured evidence whether a rung may be climbed, accounted for what each rung spent, and kept a small reference run that repeats on a schedule.",
        "trust": [
            "The one-axis rule is applied to the overlay files as they are; an overlay that changes two axes is in the repository on purpose and is refused.",
            "The gate takes its three measured values from runs in this session: the ceiling's interval from the S02 fit, the unexplained waiting from the S03 breakdown, and the tested restart from S09.",
            "The reference run is started by the CronJob's own template, so the test exercises the object that would run at night.",
        ],
        "why": [
            "A rung that changes length and model size together cannot say which caused a regression, so the launcher refuses it before anything is scheduled.",
            "A rung is held when its criteria are not met by measurements, not by opinion. A short run at a new length has no points past the curve's midpoint, so the ceiling's interval is unknown and the gate says so.",
            "The reference run's tolerances are set so that the same recipe on the same code passes and a run whose reward or step time has moved does not; the comparison function is also applied to made-up perturbations of the baseline to show it fires.",
        ],
    },
}

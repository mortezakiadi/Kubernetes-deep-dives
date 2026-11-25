"""Hand-written text for each scenario of the appendix: what it shows, why the result can be trusted, and why the output is what it is.
The numbers are in the evidence (commands, tables and checks); nothing here restates a measurement that could change from run to run.
"""
PROSE = {
    "S01": {
        "intro": "Before any experiment, I show what the experiments stand on: the GPU nodes the scheduler can hand out, the controllers that admit and place the workloads, and the path one run takes through them. A three-step run goes through that path and writes the record every later scenario reads.",
        "trust": [
            "The node and GPU figures are read from the Kubernetes API, not from the Terraform that asked for them.",
            "The queue, its quota and its two priorities are read back from the cluster after the tests applied them.",
            "The software versions are written by the run itself, from the libraries it imported, not from the image tag I intended.",
        ],
        "why": [
            "The nodes are single-GPU instances of whichever type EC2 could supply: the pool lists several, and on-demand GPU capacity differs by type and by zone, so the type that comes up is not always the first on the list.",
            "The engine's memory is fixed at 8 GiB, whatever the card, so that every cell lays out its GPU the same way and a 24 GB card and a 48 GB card give comparable runs.",
            "Every step record carries the metrics and the live settings because the design's controller and the appendix's charts read nothing else.",
        ],
    },
    "S02": {
        "intro": "This is the design's central picture, measured. Every token has a behavior policy (the engine that sampled it), a proximal policy (the trainer's own weights at the sample's version, scoring it on arrival) and a target policy (the trainer's weights now). The run allows eight versions of staleness, so ages from 0 to 7 occur, and the gap between the policies is split into the part the engine causes and the part age causes.",
        "trust": [
            "The three log-probabilities per token come from the run's own loss computation. The token table is a dump of one row's values, not a reconstruction.",
            "The per-age figures are averages of what the run logged at each optimizer step, with the age of that step's samples.",
            "The age-0 identity (the proximal and the target policy agree to the last digit) is a check that could fail if the version bookkeeping were wrong; it is the first thing I looked at.",
        ],
        "why": [
            "At age 0 the trainer's weights have not moved since the sample was generated, so the target is the proximal policy. Whatever gap to the engine remains is the engine's own: kernels, rounding and fused operations that differ between the two forward passes.",
            "Each optimizer step moves the weights, so a sample that is older was written by weights further from the current ones. The gap grows with age. The engine's gap does not, because it compares two things that were both computed at the sample's own version.",
            "The extra forward pass costs a fraction of the training pass because it needs no gradient and no backward computation; the design accepts it in exchange for being able to tell the two gaps apart.",
        ],
    },
    "S03": {
        "intro": "The design's experiment harness, run once in full: staleness from 0 to 15 versions, naive against decoupled correction, two learning rates, two seeds, as one Indexed Job of 40 cells on the research queue at low priority. The output is a staleness curve for each setting and a reference table stored with the recipe.",
        "trust": [
            "Every number in the table is computed from the 40 metrics files the cells wrote to the bucket; none is typed in.",
            "Each cell is run with two seeds, and the table prints how far apart the two seeds of the same setting ended. A difference between settings smaller than that is not claimed.",
            "The predictions that were written down before the sweep are marked as such, and the one that turned out wrong is kept as a failed check next to what the sweep showed instead.",
        ],
        "why": [
            "At the smaller learning rate each step moves the weights little, so even a sample fifteen versions old is close to the current policy, and the staleness costs a few points at most. The larger learning rate is simply too large for this model: with no staleness at all the run collapses.",
            "A stale sample is more often outside the trust region than a fresh one, and a token outside it carries no gradient. A likely reason the cells with staleness collapse less often than the cell without it at the larger learning rate is that this clip brakes the step; I did not run the ablation that would isolate it, and I do not claim it generalises (S09 shows it does not carry over to the larger model).",
            "A larger K is cheaper per step because the engine generates once per K steps and the weights are pushed into it once.",
        ],
    },
    "S04": {
        "intro": "The design bounds staleness in two places: the buffer refuses a sample that has become too old, and the scheduler does not ask the engine for samples that would be refused. Eight versions of data are generated at once and the limit is set to three.",
        "trust": [
            "The age of each trained micro-batch is recorded as it is used, so 'no sample older than three' is a measurement of what the loss saw.",
            "The generated and wasted counts are the trainer's own counters of micro-batches produced and micro-batches thrown away.",
        ],
        "why": [
            "At the buffer, a batch of eight steps' worth is generated and only the first four steps' worth is allowed to be used. The rest is discarded when its turn comes, after the engine has already produced it.",
            "At the scheduler the batch is cut to four steps' worth before generation, so the engine never produces what would be refused: the same training data for half the generation.",
        ],
    },
    "S05": {
        "intro": "The engine and the trainer never agree exactly. Here the engine is made to disagree on purpose (noise on its weights, a stand-in for an engine that rounds differently, such as an fp8 build) with no staleness at all, so that the whole gap to the data is the engine's. The naive loss measures the trust region against the engine; the decoupled loss measures it against the trainer's own weights and treats the engine's gap as a bounded weight.",
        "trust": [
            "The noise is applied to the engine's copy of the weights after every sync and never to the trainer's, so the KL between them is exactly what the noise caused. It is labelled as injected wherever it appears: it is not a measurement of any real engine.",
            "Each setting has two seeds, and the table prints both final scores.",
            "The clipped fraction is the loss's own count of tokens whose update was cut by the trust region.",
        ],
        "why": [
            "The naive loss's ratio is the target over the engine, so when the engine is off the ratio is off by the engine's gap even before the first update, and tokens are cut for no reason connected to the update.",
            "The decoupled loss's ratio is the target over the trainer's score on arrival, which is 1 at age 0; the engine's gap multiplies the loss as a weight that is truncated at 3 and set to zero above 10.",
            "With the engine's noise at zero the naive loss still cuts a little more than the decoupled one (0.13% of tokens against none): the engine's own rounding is already a gap, a small one.",
        ],
    },
    "S06": {
        "intro": "Dynamic sampling keeps only the prompts whose answers disagree. That is a filter, and a filter is a policy: it changes what the model trains on, and its effect changes as the model learns. The run reports what the filter would remove, by task type, for every batch, and a second run applies it.",
        "trust": [
            "The task type is computed from the reference solution of each problem (the number of calculation steps), and the held-out set is the same for both runs and is not filtered.",
            "The dropped share is counted from the rewards the engine's answers actually received, per group of eight.",
        ],
        "why": [
            "A group whose eight answers all score the same has no advantage to learn from. Early on the long problems are mostly all-wrong; later the short ones are mostly all-right, so the filter's effective policy shifts from one type to another while the model improves.",
            "With the filter on, those rows are masked out of the loss, so fewer tokens carry gradient per micro-batch, and the answers they came from were generated for nothing.",
        ],
    },
    "S07": {
        "intro": "Own older data and human-written solutions are mixed into the batches at set shares, each pool with its own limits: replay by age and by distance from the current policy, external data through a small loss of its own. A last run lets an effective-sample-size floor lower the replay share automatically.",
        "trust": [
            "The share of each pool is counted from the source tag of every micro-batch of the run.",
            "The replay limits are checked on the metrics of the replayed micro-batches themselves: their age and their behavior KL when they were trained on.",
            "The external pool is human-written (the reference solutions of other training problems, rewritten into the answer format), not a model's output; the design's 'teacher' is not built.",
        ],
        "why": [
            "A replayed micro-batch was written by an older policy, so its importance weights are more uneven and its effective sample size is lower than that of fresh data. The age and distance limits retire the batches that are too far away.",
            "Human-written tokens are unlikely under the policy, so their weights would be extreme in the RL loss; they go through a plain likelihood loss at a small weight instead.",
            "The floor halves the replay share when the effective sample size stays under it for three steps and brings it back one notch at a time after thirty healthy steps.",
        ],
    },
    "S08": {
        "intro": "A learning rate ten times too high is pushed into a running job at step 30, a mistake an operator can make with one edit. The same fault is run with no controller, with the first two levels of the controller (skip a batch, cut the rate) and with all three (adding a rollback to the last healthy checkpoint). A smaller fault, a rate three times too high, shows what the controller does not see.",
        "trust": [
            "The fault is injected by the run itself and logged as an event, so every chart and table can mark the step.",
            "Every action of the controller is an event in the run's metrics file, with the metric values that triggered it; the table and the timeline are read from there.",
            "The rollback's target is a checkpoint that was tagged healthy when it was saved, using the thresholds of the rules file; the event says which checkpoint and which settings came back.",
        ],
        "why": [
            "A batch generated before the fault and trained on after it is stale under a step that is far too large, so its importance weights become very uneven and the effective sample size drops within a few steps.",
            "Skipping a batch and halving the rate for twenty steps cannot repair weights that have already moved the wrong way, and the rate returns to its faulty value afterwards. Only the rollback restores both the weights and the settings of a healthy state.",
            "A moderately high rate does not make the data look off-policy: the weights move steadily and the data stays close to them, so these metrics can stay quiet while the score falls. A rule on the score or the entropy would be needed for that, and none is in the design's list.",
        ],
    },
    "S09": {
        "intro": "The reference library is used the way the design intends: ask the table what a setting should do, run that setting with a seed the table has not seen, and compare. Then two cells of the grid are repeated on a model nearly three times larger.",
        "trust": [
            "The lookup reads the table written by S03 from the evidence of the same run; nothing is retyped.",
            "The new runs use a seed that no cell of the sweep used.",
            "The larger model is one seed per cell, and the text says so.",
        ],
        "why": [
            "At the stable learning rate the sweep's two seeds agree closely, so the table is a good predictor. At the large learning rate the cells collapse and the table says so, because a number without the collapse count would mislead.",
            "The size of the gap between policies at a given K depends on the learning rate and the step, not on the number of parameters, so it carries over to a larger model to within an order of magnitude.",
        ],
    },
    "S10": {
        "intro": "The research queue admits jobs by priority. The quota is squeezed to one GPU, four sweep jobs are submitted at low priority, and an interactive job is submitted while the first sweep job is running.",
        "trust": [
            "The admission order is read from Kueue's own Workload objects (the time each was admitted), not inferred from pod start times.",
            "The running job's restart count and completion are read from its pod and from its metrics file.",
        ],
        "why": [
            "Kueue admits the highest-priority waiting workload when quota frees up, and among equals the oldest. The interactive job therefore goes ahead of the three sweep jobs that arrived before it.",
            "The cluster queue's preemption policy is 'never', so priority decides who is admitted next, not who is evicted.",
        ],
    },
}

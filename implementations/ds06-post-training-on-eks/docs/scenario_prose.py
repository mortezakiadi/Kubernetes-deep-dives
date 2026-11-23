"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""
PROSE = {
    "S01": {
        "intro": "Before testing behaviour I check that the platform is what section 2 says: a standard EKS cluster with two managed node groups, "
                 "the GPU pool on a pinned accelerated AMI, the controllers, the two queues, the locked-down verifier namespace, four workload "
                 "identities, the versioned datasets, and no run yet.",
        "trust": [
            "Every fact comes from the system that owns it: the cluster and node groups from the EKS API, the instances from EC2, the queues from Kueue's own objects, the datasets from the bucket.",
            "The controllers' versions are read from the images of the running Deployments and DaemonSets, not from my Terraform files.",
            "'Nothing is running' is the API server's answer for Jobs, JobSets and Workloads in the run namespace.",
        ],
        "why": [
            "Auto Mode is off because the Terraform has no compute configuration; the node groups are what EKS describes because Terraform created them with those AMI types and release versions.",
            "Each GPU node advertises one GPU because the device plugin runs on the GPU pool and each instance type in the list has one GPU.",
            "The ablation queue owns nothing and may borrow two GPUs because that is its quota; production owns two and reclaims, which is what S06 relies on.",
        ],
    },
    "S02": {
        "intro": "The design's first requirement is that one versioned file fully describes a run. I hash the baseline SFT recipe, a reformatted copy "
                 "with the same content, and a copy with one value changed; resolve it without launching; then launch it and look at what was "
                 "recorded and what Kubernetes received.",
        "trust": [
            "The three hashes are printed by the launcher from three files on disk; the reformatted file is produced by a YAML round trip that drops comments and reverses the key order.",
            "The resolved description is read back from S3, not from the launcher's memory, and its write time is compared with the pod's creation time from the API server.",
            "The queue and priority Kueue applied are read from Kueue's Workload object, not from the labels I set.",
        ],
        "why": [
            "Formatting does not change the hash because the launcher hashes the canonical JSON of the parsed recipe.",
            "Every reference is pinned because the launcher replaces each reference with what it points to at that moment: a dataset's sha256, a model's commit, an image digest.",
            "The record exists before the pod because the launcher writes it first and creates the Job only if that write succeeded.",
        ],
    },
    "S03": {
        "intro": "SFT is where the design says behaviour changes most and is noticed least. I check the run's three ingredients on what the trainer "
                 "really did, then compare the base model and the SFT model on the same five suites and let the gate decide.",
        "trust": [
            "The mixture counts, the loss mask and the packing check are written by the training job from its own data and its own first batch, before the first optimizer step.",
            "The packing check has a built-in control: the same tokens treated as one sequence change the logits by tens, so a check that passed by accident would be visible.",
            "Both models are evaluated by the same job with the same datasets (by hash) and greedy decoding; the gate reads only that evaluation.",
        ],
        "why": [
            "Each source's share matches its weight because each of the 1,600 slots draws a source with the recipe's probability from a seeded generator.",
            "Only assistant tokens carry loss because TRL swaps in a training chat template that marks the assistant's turns, and labels everything else -100.",
            "The house style appears only after SFT because only the SFT data contains it; the system prompt does not describe it.",
        ],
    },
    "S04": {
        "intro": "The design's rule is one change per ablation. I try to launch an ablation that changes two keys, then a correct one that changes only "
                 "the reasoning traces' weight, and compare it with its parent. This is the design's own SFT scenario: more reasoning data, and the "
                 "model starts answering casual questions in numbered steps.",
        "trust": [
            "'Nothing was created' is counted from the API server and from the bucket's run prefixes, before and after the refused launch.",
            "The ablation's mixture is the one the training job recorded, not the one the recipe asked for.",
            "Parent and ablation differ in exactly one key (the launcher's diff is shown), were trained on the same data hashes, and were scored by the same suites.",
        ],
        "why": [
            "The refusal comes from the launcher's diff of the flattened recipe against its parent, before anything is written.",
            "The ablation runs at priority 100 on the ablation queue because the launcher puts every ablation there.",
            "Step leakage rises with the reasoning weight because conversion questions of families neither source covers are answered in the dominant style; the datasets were built so this effect exists and can be measured (section 4).",
            "The gate blocks the ablation even though its maths improved because every gate must pass: a capability gain does not buy a behaviour regression.",
        ],
    },
    "S05": {
        "intro": "Before RL the design estimates each prompt's pass rate with the starting model and keeps the prompts between 5% and 95%. I run that "
                 "filter with the promoted SFT model and look at what it removed and why.",
        "trust": [
            "Each pass rate is the share of 16 sampled answers that the verifier pool accepted; the counts are written by the job, per source.",
            "Kept, too easy and too hard add up to every prompt, which a program checks.",
            "The new datasets' row counts and hashes are read from their own metadata in S3 and match the job's report.",
        ],
        "why": [
            "Level 1 and 2 arithmetic are almost always right and level 4 often wrong, so the filter removes many easy prompts and some hard ones.",
            "Code tasks split by family: the families SFT showed are solved every time and are dropped as too easy, while some families it never showed are solved only now and then, so they are kept.",
            "The output is a new version because generated datasets are write-once; the RL recipe names that version, and the launcher records its hash.",
        ],
    },
    "S06": {
        "intro": "The design wants overnight ablations to borrow idle GPUs without ever slowing production. I start an RL ablation that borrows "
                 "both GPUs, then launch the production RL run.",
        "trust": [
            "Borrowing is read from the ablation ClusterQueue's own usage, which reports borrowed and total separately.",
            "The preemption is Kueue's condition on the ablation's Workload, with Kueue's reason and message.",
            "'Stopped as a whole' is the absence of any pod with the ablation's run label, polled from the API server.",
        ],
        "why": [
            "The ablation could start because production's quota was idle and the ablation queue may borrow up to two GPUs.",
            "Kueue preempted it because production's queue has reclaimWithinCohort: Any and the ablation's usage was borrowed.",
            "Both pods went together because Kueue suspends the JobSet, and JobSet removes every child Job.",
            "The ablation came back by itself because a preempted Workload returns to its queue; it restarted from step 0 (section 6).",
        ],
    },
    "S07": {
        "intro": "The design splits RL into rollout, reward and training so each scales separately. I look inside the production RL run from S06: "
                 "what Kueue admitted, where the parts ran, what the run reported while it ran, where its time went, and what it produced.",
        "trust": [
            "The admission is Kueue's: the Workload's pod sets and the resources assigned to each.",
            "The reward counts are the verifier pods' own counters, read inside each pod, so they do not depend on the trainer's logs.",
            "The mid-run reading was taken while the run's status said running; the timings are measured around TRL's own generate and weight-sync calls.",
        ],
        "why": [
            "One Workload asks for both GPUs because JobSet is a Kueue integration and each replicated job is a pod set of one Workload.",
            "Weight sync happens every step because in server mode TRL pushes every parameter to the vLLM server after each optimizer step, over NCCL between the two pods.",
            "The checkpoint list ends at two because the callback deletes all but the newest two after each upload.",
            "The gate's verdict follows from the evaluation alone; the transcript shows which checks passed and which failed.",
        ],
    },
    "S08": {
        "intro": "The design says groups whose answers all get the same reward cost full rollout compute and teach nothing. I compare the filtered "
                 "production run with its one-key ablation on unfiltered maths prompts.",
        "trust": [
            "The share of zero-spread groups is TRL's own per-step metric (frac_reward_zero_std), averaged over all 40 steps of each run.",
            "The two runs differ in one key, which the launcher checked when it accepted the ablation.",
        ],
        "why": [
            "Unfiltered prompts include many the model always solves; every answer in such a group scores 1, the group's advantage is zero, and the step's gradient gets nothing from it.",
            "The filtered run still has many zero-spread groups because the SFT model is very confident (its entropy is a few hundredths of a nat), so eight samples often agree; dynamic sampling, which refills the batch, is not built.",
        ],
    },
    "S09": {
        "intro": "The design says the signals that predict a collapse are there before the reward drops, if someone watches them. I run the RL "
                 "recipe with a higher learning rate as a one-key ablation and read what the health monitor, a separate Deployment, wrote.",
        "trust": [
            "The alerts are written by the monitor from the tracking store; neither the trainer nor the test computes them.",
            "Every rule compares a two-step window with the run's own first three steps and needs a minimum absolute change, so the healthy run is a real control and not a run that cannot trigger a rule.",
            "The rules' thresholds were set during development, after the first version raised an entropy alert on a healthy run whose entropy starts near zero (section 6).",
        ],
        "why": [
            "At 25 times the learning rate the policy breaks within a few steps: the gradient norm jumps on the step where it breaks, and the reward's two-step window crosses its threshold a step or so later.",
            "At 5 times the learning rate the failure is slower and noisier, and the order is not guaranteed: the measured steps of both rules are in the 'Measured in this run' line. In development the reward rule fired first at this rate. The design's claim that the leading signals come days earlier is about runs of thousands of steps; a 40-step run cannot show it, and this test does not claim it does.",
            "The healthy run raises nothing because its entropy stays within a few hundredths of a nat of its start and its gradient norm never reaches five times its median.",
        ],
    },
    "S10": {
        "intro": "The design's reward scenario is a model that learned to print the expected output. I send the verifier pool completions written "
                 "to cheat or to do harm, from the run namespace, as a trainer would; then check who can reach the pool and how its two maths "
                 "verifiers read answers.",
        "trust": [
            "The completions are sent from a pod in the run namespace, through the Service, exactly as the trainer's reward functions call it.",
            "The 'blocked' result from another namespace is a real connection attempt that failed, not a configuration read.",
            "The security settings are read from the running pod, not from the Deployment I wrote.",
        ],
        "why": [
            "Cheating cannot score because the candidate only ever sees the test arguments; the expected outputs stay in the verifier, which compares.",
            "The network calls fail because the namespace's NetworkPolicy drops all egress, including DNS; the call hangs and the time limit kills the child.",
            "Both verifiers agree on spellings of a number because both parse answers into exact fractions; they differ on unboxed answers on purpose.",
        ],
    },
    "S11": {
        "intro": "The design compares two ways to transfer a teacher's behaviour. I distil Qwen2.5-3B-Instruct into the promoted SFT model both "
                 "ways, on the same arithmetic prompts and a similar number of student updates, and evaluate the student, both results and the teacher.",
        "trust": [
            "Every model, the teacher included, is evaluated by the same suites, so 'the teacher is stronger' is measured here rather than assumed.",
            "The traces were kept only when the verifier pool accepted them, and the job reports how many it kept.",
            "The tokenizer check runs in the training job before training, on the two models it loads.",
        ],
        "why": [
            "Off-policy distillation is an SFT run whose only source is the teacher's verified traces; the student learns from states the teacher visited.",
            "On-policy distillation lowers its loss because that loss is the reverse KL between the student's and the teacher's next-token distributions on the student's own tokens, which training reduces.",
            "The teacher's habits move with its skill: the students' answers get longer, toward the teacher's length.",
            "A stronger teacher is not enough. The teacher multiplies three-digit numbers in one line; the SFT student had learned to write the multiplication out. A 0.5B student can copy the one-line form but not its accuracy, which is the design's warning that distillation can fail when teacher and student reason in different patterns. The comparison table and the sample answer show whether that happened in this run, and the gates decide.",
        ],
    },
}

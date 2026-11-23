"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""
PROSE = {
    "S01": {
        "intro": "Before testing behaviour I check that the platform is what section 2 says: a standard EKS cluster with two managed node groups, "
                 "the GPU pool on a pinned accelerated AMI, the CSI driver and snapshot controller, the two queues, the rollout engine serving the "
                 "pinned policy, the network policies, the published tasks without their hidden tests, and no run yet.",
        "trust": [
            "Every fact comes from the system that owns it: the cluster and node groups from the EKS API, the instances from EC2, the queues from Kueue's own objects, the model from the rollout engine's /v1/models, the tasks from the bucket.",
            "'No hidden test in the bucket' is a listing of the bucket's tasks/hidden/ prefix, not a statement about my upload code.",
            "'Nothing is running' is the API server's answer for Jobs in the environment namespace, and an empty trajectories/ prefix.",
        ],
        "why": [
            "Auto Mode is off because the Terraform has no compute configuration; the node groups are what EKS describes because Terraform created them with those AMI types and release versions.",
            "The GPU nodes are whichever listed type EC2 had capacity for, because the node group takes the first type in its list that it can launch.",
            "The branch queue owns nothing and may borrow everything because that is its quota; S11 depends on it.",
        ],
    },
    "S02": {
        "intro": "The design treats a trajectory as data: every turn, its context, its log-probabilities, its state and its branch points, replayable "
                 "from a snapshot. I run 128 root attempts (32 tasks, four seeds), open one trajectory turn by turn, and replay it from a snapshot.",
        "trust": [
            "The trajectory is read back from S3 through the store, after the episode's pod has exited; nothing is taken from the pod's memory.",
            "The per-token log-probabilities are the ones the rollout engine returned (with their token ids), not recomputed.",
            "The replay is a separate pod that has only the snapshot and the recorded actions; it compares every observation and every tree hash with the trajectory.",
        ],
        "why": [
            "Every turn has a context reference and log-probabilities because the environment writes both through the store before it starts the next turn.",
            "Only some turns are branch points because a snapshot is taken only for an uncertain choice, an edit, the first test run, or every third turn.",
            "The replay is identical because the tools are deterministic functions of the files, and the snapshot is a deterministic tar of exactly those files.",
        ],
    },
    "S03": {
        "intro": "The design's branching YAML restores k branches from a VolumeSnapshot. I seed an EBS volume with the state a trajectory recorded after "
                 "one of its turns, snapshot it with the CSI driver, restore four volumes from the snapshot, and check that each holds exactly that "
                 "state. I also time both snapshot paths.",
        "trust": [
            "The state is compared by tree hash: the seed pod's hash, the trajectory's recorded hash after that turn, and each restored volume's hash are computed independently.",
            "The snapshot's readiness and the timings come from the VolumeSnapshot objects and the pods' own clocks, not from assumptions about EBS.",
            "Each restored volume is a separate PersistentVolumeClaim with the snapshot as its dataSource, mounted by a separate pod.",
        ],
        "why": [
            "The restored volumes match because an EBS snapshot is a point-in-time copy of the blocks, and every restore is created from the same snapshot.",
            "A CSI snapshot takes tens of seconds to become ready and a restore needs a new volume, while a tar of a small working copy is a single S3 write. That is why the branch jobs here use the tar path, and why the snapshot cost decides how often one can branch.",
        ],
    },
    "S04": {
        "intro": "The design's first claim is that long tasks are hard in a specific way: success falls with the horizon, partial progress hides in "
                 "failures, and group-relative RL gets no signal from groups where every attempt agrees. I read those numbers off the 128 root attempts.",
        "trust": [
            "Every number is computed by metrics.py from the stored trajectories and their verified outcomes, in the same command shown.",
            "pass@1 and pass@4 come from the same four attempts per task, so the gap between them is the model's own variance, not a different run.",
            "The outcomes are the verifier's fresh-environment verdicts, which S09 shows cannot be gamed by the scripted tricks.",
        ],
        "why": [
            "Success falls with the number of bugs because each bug needs its own read, edit and test, and each turn is another chance to break something.",
            "Many groups have zero variance because the model either always or never solves easy and hard tasks; those groups give a group-relative advantage of zero to every turn.",
            "Failed attempts still show partial progress because Φ counts each issue example separately, so fixing one of three bugs is visible even when the outcome is 0.",
        ],
    },
    "S05": {
        "intro": "The design's answer to sparse credit is to branch from the middle of a trajectory and let verified continuations say what each "
                 "state was worth. The credit worker branches four times from up to three branch points of six trajectories and turns the results "
                 "into turn-level advantages.",
        "trust": [
            "Every value is a mean of verified outcomes of real branch episodes, each a separate pod admitted by the branch queue and judged in a fresh copy.",
            "The trajectory-level advantages are computed from the same group of root attempts, so the two methods differ only in how they assign credit.",
            "The prefix-cache numbers are the difference of vLLM's own counters read before and after the credit run.",
        ],
        "why": [
            "Turn-level advantages differ inside one trajectory because each segment gets the change in V between the branch points around it.",
            "A failed trajectory can have a state with value above zero because a branch from that state, with a different seed, found the fix the original attempt missed.",
            "Branches hit the prefix cache because every branch from a point sends the same messages up to that point; vLLM reuses those tokens' KV blocks.",
            "Branching costs several times the environment time of the trajectories it credits; that is the price the design names for this rung of the ladder.",
        ],
    },
    "S06": {
        "intro": "A credit method is only useful if its advantages are right. I estimate the same branch points again with twelve branches each as a "
                 "reference, and compare the four-branch advantages and the trajectory-level ones against it.",
        "trust": [
            "The reference uses fresh branches from the same snapshots; it shares no episode with the four-branch estimate.",
            "The correlation and the sign agreement are computed over every segment both runs have, not a chosen subset.",
        ],
        "why": [
            "The two Monte Carlo estimates agree because both are means of outcomes from the same states; more branches only reduce the noise.",
            "Trajectory-level credit cannot rank turns because it is one number per trajectory.",
        ],
    },
    "S07": {
        "intro": "Two training requirements of the design: train on the context the model actually saw, and let each turn's advantage move that turn. "
                 "The trainer recomputes log-probabilities on a GPU, then measures what one policy-gradient step would do to each turn.",
        "trust": [
            "The trainer loads the policy at the same commit with Transformers, a different engine from vLLM, so agreement is not one program checking itself.",
            "The full-history comparison uses the same turns and the same generated tokens; only the prompt changes.",
            "The per-turn change is computed from real gradients of the model with a LoRA adapter: the step's first-order effect on each turn's mean token log-probability.",
        ],
        "why": [
            "The stored context reproduces the rollout engine's log-probabilities because it is exactly the prompt the engine received, with the same chat template and token ids.",
            "The full history gives different numbers because compaction removed observations the model never read; training on them would be off-policy.",
            "Trajectory-level credit pushes every turn the same way because every turn shares one advantage; turn-level credit raises the turns that increased V and lowers the ones that decreased it.",
        ],
    },
    "S08": {
        "intro": "Denser rewards help long tasks, but a reward for activity can be farmed. I compare a fixed reward per test run with potential-based "
                 "shaping on the change in Φ, on a scripted looping agent, on a scripted agent that patches the test framework, and on all real attempts.",
        "trust": [
            "The scripted agents run through the same environment, the same /progress calls and the same verifier as the model.",
            "Φ in place is recorded next to Φ fresh on every turn, so the effect of measuring fresh is visible, not asserted.",
            "The correlations are over all 128 root attempts, computed by metrics.py from the stored Φ values and outcomes.",
        ],
        "why": [
            "Potential-based shaping pays the looping agent nothing because its state never changes, so Φ never rises; the fixed reward pays every repeat.",
            "The framework patch gets no progress because Φ is measured in a fresh copy of the task, and the tamper check rejects library code that names the test framework.",
            "Shaping on Φ tracks success because Φ rises exactly when an issue example starts giving the expected value.",
        ],
    },
    "S09": {
        "intro": "The design says the final check for a long task must run in a fresh environment with layered checks, because the agent controls its "
                 "own working copy. I send the verifier four scripted episodes built to look successful, then compare fresh and in-place verdicts on "
                 "every real attempt.",
        "trust": [
            "Each verdict lists every check with its result, and carries the verifier version.",
            "The in-place verdict is computed by the same verifier on the agent's own files, so the difference between the two is the effect of the fresh copy alone.",
            "The hidden tests are only in the verifier's ConfigMap (S01), so no episode could have read them.",
        ],
        "why": [
            "Editing the tests fails because the fresh copy drops the agent's edits to protected paths and the bug is still there.",
            "The framework patch passes in place and fails fresh because the tamper check rejects changed library code that names unittest or patches assertions.",
            "The huge rewrite fails because more than 60 changed lines is refused, whatever the tests say.",
            "The false claim is a success flagged as dishonest because honesty is recorded, not required: the bug was fixed.",
        ],
    },
    "S10": {
        "intro": "Exploration on long tasks needs successes to learn from. A reverse curriculum starts episodes close to the goal, from snapshots of "
                 "earlier successes, and moves the start back as success rates allow. I run its three stages on the three-bug tasks.",
        "trust": [
            "Every stage's episodes are real branch episodes from stored snapshots, verified in a fresh copy.",
            "The policy is the same in every stage, so the change in success rate comes from where the episode starts, not from training.",
        ],
        "why": [
            "Starting with two turns left succeeds more often because most of the work is already in the snapshot; only the last decisions remain.",
            "The scheduler promotes a stage when its success rate reaches the threshold in the spec, which is the design's rule for moving the start back.",
        ],
    },
    "S11": {
        "intro": "Branches are useful but lower priority than primary rollouts. I fill the environment capacity with branch rollouts, then submit primary "
                 "rollouts, and watch Kueue preempt the branches and re-admit them later.",
        "trust": [
            "Borrowing, eviction and re-admission are read from Kueue's ClusterQueue and Workload objects, not inferred from pod counts.",
            "The primary rollouts are a normal rollout job of the launcher, the same as in S02.",
        ],
        "why": [
            "The branches run on borrowed capacity because their queue has no quota of its own.",
            "They are preempted because the rollouts queue may reclaim within the cohort and the branches' usage was borrowed.",
            "They come back because a preempted Workload returns to its queue and is admitted again when capacity is free.",
        ],
    },
}

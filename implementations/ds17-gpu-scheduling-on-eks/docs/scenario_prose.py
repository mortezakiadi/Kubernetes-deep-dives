"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""
PROSE = {
    "S01": {
        "intro": "Before testing behaviour I check that the platform is what section 2 says: the cluster, the two node groups, the 16 simulated "
                 "nodes and their GPUs, Kueue and its objects, the second scheduler's two profiles, DCGM and the accounting service, and no workload.",
        "trust": [
            "Every fact comes from the system that owns it: the cluster from the EKS API, nodes and their allocatable GPUs from the API server, the profiles from the scheduler's own ConfigMap.",
            "The simulated nodes are listed next to the real ones by the same command; the difference is visible in their labels and instance types.",
        ],
        "why": [
            "The simulated nodes are Ready because KWOK renews their leases and status, as a kubelet would.",
            "The real GPU nodes advertise one GPU each because the device plugin runs on the GPU pool.",
        ],
    },
    "S02": {
        "intro": "The design's first warning is that allocation is not utilization. On the two real GPUs, one pod runs the CUDA n-body benchmark and "
                 "one 'notebook' holds a GPU doing nothing. Both GPUs are 100% allocated.",
        "trust": [
            "Utilization is DCGM's own measurement, read from each exporter pod and labelled with the pod that holds the GPU.",
            "The accounting service gets the same numbers independently and classifies them; its result is compared, not copied.",
        ],
        "why": [
            "The trainer's GPU is busy because the benchmark keeps its kernels running; the notebook's is at 0% because sleep never touches it.",
            "The accounting service splits the allocated GPU-minutes in half because one of two allocated GPUs is idle.",
        ],
    },
    "S03": {
        "intro": "Two jobs of 12 one-GPU pods compete for 16 free GPUs in one rack, first straight through the scheduler, then through a Kueue queue "
                 "with 16 GPUs of quota. Real job controllers create pods in growing batches; to make the race reproducible, the scenario raises "
                 "both jobs' parallelism in alternating steps of 2.",
        "trust": [
            "The pod counts are the API server's; the GPUs waiting for a gang are the accounting service's count of GPUs held by pods of an incomplete gang.",
            "The Kueue half uses the same jobs, nodes and free GPUs; only the queue label differs.",
        ],
        "why": [
            "Without admission each pod is placed on its own as soon as a GPU is free, so both jobs take part of the 16 and neither can start training.",
            "With Kueue the second job's whole request does not fit the remaining quota, so it stays suspended with no pods and holds nothing.",
        ],
    },
    "S04": {
        "intro": "Quota fit is not node fit. The queue has 16 GPUs of quota but the rack has only 8 free, because pods Kueue does not manage use the rest.",
        "trust": [
            "The eviction reason and requeue count are Kueue's own Workload status.",
            "The topology-aware queue's refusal is Kueue's own message, naming how many of the 12 pods fit in the rack.",
        ],
        "why": [
            "Kueue admits on quota, the scheduler can place only 8 pods, and waitForPodsReady evicts the workload after 60 s and requeues it with backoff.",
            "When the rack is free, the requeued workload is admitted again and every pod is placed.",
            "Topology-aware admission counts the rack's real free GPUs, including those used by unmanaged pods, so it never admits what cannot be placed.",
        ],
    },
    "S05": {
        "intro": "Four jobs through one topology-aware queue whose quota (256 GPUs) exceeds the fleet, so only the topology can hold a job back.",
        "trust": [
            "Each pod's node is read from the API server, and its block and rack from that node's labels.",
            "The waiting job's reason is Kueue's own message.",
        ],
        "why": [
            "A required block makes Kueue pick a block with enough free nodes and pin every pod there.",
            "Slices of 2 nodes that each require a rack keep pairs together inside the block.",
            "The second 8-node job waits because neither block has 8 free nodes; the job that only prefers a rack is admitted into a wider domain, and BestEffortFIFO lets it pass.",
        ],
    },
    "S06": {
        "intro": "The same seeded trace of 31 single-pod jobs (64 GPUs) runs twice through a queue without topology, once with each scheduler "
                 "profile, and then a gang of four 8-GPU pods is submitted.",
        "trust": [
            "Both runs use the same trace, the same nodes and the same scheduler binary; only schedulerName differs.",
            "Free GPUs per node are computed from the pods the API server lists on each node.",
        ],
        "why": [
            "MostAllocated scores the fullest node that fits highest, so small jobs fill nodes one after another and whole nodes stay free.",
            "LeastAllocated scores the emptiest node highest, so every node gets a few small jobs and none stays whole; the gang's 8-GPU pods fit nowhere, and the accounting service counts those free GPUs as fragmented.",
        ],
    },
    "S07": {
        "intro": "Packing only helps new pods. Starting from the spread layout with the gang waiting, the planner picks the nodes that are cheapest "
                 "to empty, they are drained, and the gang takes them.",
        "trust": [
            "The plan is computed from the pods on each node; the drain uses kubectl drain, which evicts through the Eviction API.",
            "Lost work is computed from each moved pod's start time and its 600 s checkpoint interval.",
        ],
        "why": [
            "Evicted pods get a DisruptionTarget condition, the Jobs' pod failure policy ignores it, and each Job recreates its pod on another node.",
            "The emptied nodes are whole again, so the gang's pods fit there.",
        ],
    },
    "S08": {
        "intro": "Two teams with 32 GPUs of nominal quota each in one cohort. Vision lends at most 24; LLM may borrow up to 32.",
        "trust": [
            "Usage, borrowing and preemption reasons are Kueue's own ClusterQueue and Workload status.",
            "The blocked-by-quota GPUs are the accounting service's count of free GPUs that fit a pending workload that is not admitted.",
        ],
        "why": [
            "LLM's eighth job waits because borrowing stops at vision's lending limit, although the fleet has free GPUs.",
            "When vision returns, its kept 8 GPUs start one job at once, and reclaimWithinCohort preempts two of LLM's borrowed jobs for the other two.",
        ],
    },
    "S09": {
        "intro": "Borrowed capacity is split by weight between two teams, and inside one team a project that has used less goes first.",
        "trust": [
            "Admitted GPUs per team and the preemption reasons are Kueue's status; the weighted shares are in the ClusterQueues' status.",
            "The admission order is read from the Workloads' conditions after the two freed slots are filled.",
        ],
        "why": [
            "A queue below its weighted share preempts a queue above it until the shares are as close to 2:1 as 8-GPU jobs allow.",
            "Usage-based admission fair sharing orders pending workloads by their LocalQueue's decayed recent usage, so the paper project's job passes sweeps queued before it.",
        ],
    },
    "S10": {
        "intro": "Urgent work gets capacity by preemption, the cost of preemption is measured, and a small job with a declared run-time limit fills a gap.",
        "trust": [
            "Priorities, preemption reasons and the run-time limit's effect are Kueue's Workload status.",
            "The lost work is the accounting service's record of the eviction.",
        ],
        "why": [
            "The release job preempts one best-effort job because withinClusterQueue is LowerPriority and release outranks best-effort.",
            "BestEffortFIFO admits the small job while the large one waits, and Kueue deactivates it when its admitted time passes the declared limit.",
        ],
    },
    "S11": {
        "intro": "The accounting service's breakdown for the whole run, and the queue wait of every Kueue workload by job size.",
        "trust": [
            "The total is checked against the number of GPUs times the sampled time, so no GPU-minute is missing or counted twice.",
            "The waits were recorded from Kueue's own timestamps before each scenario cleaned up.",
        ],
        "why": [
            "Each category is present because an earlier scenario created it on purpose: S02 idle GPUs, S03 a partial gang, S06 fragments, S08 quota policy, S10 preemption.",
        ],
    },
}

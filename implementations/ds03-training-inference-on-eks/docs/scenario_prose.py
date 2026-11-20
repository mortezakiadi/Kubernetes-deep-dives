"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""
PROSE = {
    "S01": {
        "intro": "Before testing behaviour I check that the platform is what the design says: one cluster, the workload APIs, the scheduler, the gateway and the metrics stack, one GPU pool and one queue per family, "
                 "and no GPU capacity running while nobody has asked for any.",
        "trust": [
            "Cluster state is read from the EKS API. The component versions are the image tags of the controllers' own Deployments, not the values in my configuration files.",
            "The 'no GPU at rest' check asks both Kubernetes (nodes that report a GPU) and EC2 (instances of a GPU type tagged to this cluster). A node hidden from one would still show in the other.",
            "The queue facts are read back from the live ClusterQueue objects, not from Terraform.",
        ],
        "why": [
            "Every component is running because Terraform installs each from a pinned chart and Helm waits for the release to be ready before Terraform continues.",
            "There is one pool per family because the node pools, the Kueue flavors and the queues are generated from one table in the platform layer, so a queue's flavor always names a real pool.",
            "No GPU is running because the GPU pools start empty and carry a limit. Auto Mode buys a node only for a pod that a queue has admitted.",
        ],
    },
    "S02": {
        "intro": "A researcher wants to try something on a GPU. The golden path is one manifest. I check that she gets the GPU through her own queue, that an idle notebook is stopped without anyone asking, "
                 "that the GPU goes back, and that her files do not.",
        "trust": [
            "The queue and priority come from the Workload object Kueue created, read back from the cluster.",
            "The GPU check runs `nvidia-smi` inside the notebook container, so it shows what the notebook itself can see.",
            "Culling is observed, not triggered: the test only waits. The culler's own log lines are shown, including the idle time it measured.",
            "The file check writes a file before the cull and reads it after the restart, in a new pod.",
        ],
        "why": [
            "The notebook goes through the research queue because the manifest carries the queue label and Kueue's StatefulSet integration turns it into a Workload.",
            "It is culled because the culler asks each Jupyter server for its kernels and terminals and scales the StatefulSet to zero when the last use is older than the limit. I first used the server's own activity time and found that the culler's polling kept resetting it, so the culler now ignores it.",
            "The GPU quota is released because deleting the pod ends the Workload's quota reservation.",
            "The files survive because the volume belongs to the StatefulSet's claim, which is not deleted when the pod is.",
        ],
    },
    "S03": {
        "intro": "A training job is one manifest. I check the properties the design asks for: the job's pods are admitted together or not at all, they land in one zone, the GPUs are tested before they train, "
                 "the loss goes down, and the checkpoints are complete.",
        "trust": [
            "The held-back job's message comes from the Workload's own condition. The pod count is read from the cluster.",
            "The pod creation times and the zones of the nodes come from the Kubernetes objects.",
            "The loss, step times and throughput are events the training script wrote to S3 as it ran. The Prometheus numbers are read through the API server's service proxy while the job runs.",
            "The checkpoint listing is a real S3 listing.",
        ],
        "why": [
            "The big job has no pods because Kueue keeps a TrainJob suspended until its whole request fits in the queue's quota. The training queue owns two GPUs and the job asked for three.",
            "Both pods appear together because Kueue unsuspends the JobSet once, and the JobSet creates all its pods.",
            "They share a zone because the platform runtime's pod template requires pod affinity on the zone label for pods of the same job. Karpenter honours it when it buys the second node.",
            "The burn-in runs because the training script multiplies large matrices on the GPU first and refuses to start if the result is not finite.",
        ],
    },
    "S04": {
        "intro": "A checkpoint is useful only if it is written often and does not stop training. I run the same job twice, writing each checkpoint inline once and in the background once, and measure what each costs.",
        "trust": [
            "The pauses are measured inside the training loop around the checkpoint call, and written to S3 as events.",
            "Both runs use the same code, model, data, GPUs and checkpoint steps. Only the mode flag differs.",
            "The marker times are the S3 objects' own modification times.",
        ],
        "why": [
            "Inline checkpointing stops training for the copy, the local write and the upload. Background checkpointing stops it only for the device-to-host copy; a thread does the rest.",
            "The completion markers are written last, by each rank, after its shard is uploaded. The loader accepts a step only when every rank's marker exists, so a crash during a checkpoint cannot produce a half checkpoint that looks complete.",
            "The shards are uneven because tensors are dealt out to ranks in turn, and the model's biggest tensors are not evenly spread.",
        ],
    },
    "S05": {
        "intro": "At scale something fails every few hours. I make two things fail on purpose in one running job: I terminate the EC2 instance of one rank, and later I freeze one rank's process. Nobody touches the job afterwards.",
        "trust": [
            "The node is terminated with the EC2 API, not by deleting a pod, so the failure is the real kind: the machine disappears.",
            "The freeze is a SIGSTOP sent to the training process. Kubernetes sees a healthy pod; only the training script's own watchdog can notice.",
            "Every attempt, resume step and watchdog event comes from the events the script wrote to S3. The restarts counter is read from the JobSet.",
            "Goodput is computed from timestamps in those events and the median step time the run itself reported.",
        ],
        "why": [
            "The job restarts because the platform runtime sets a JobSet failure policy of three restarts. When a pod disappears, the Job fails, and the JobSet recreates the whole group.",
            "It resumes because the script reads the S3 listing, picks the newest step whose markers are all present, loads every rank's shard and continues from the next step. The batches depend only on step and rank, so the resumed run sees the data it would have seen.",
            "The hang is caught because the surviving rank keeps a clock of the last completed step. A frozen peer leaves it waiting in a collective, so no step completes; after the timeout it exits and the failure policy takes over.",
            "Lost work is bounded by the checkpoint interval, because at most the steps since the last checkpoint are done twice.",
        ],
    },
    "S06": {
        "intro": "A model reaches production through a registry and gates, not through someone's notebook. I register a model, derive two compressed versions, evaluate all three on the same questions, "
                 "promote only what passes, put the winner in front of the serving stack, and roll it back.",
        "trust": [
            "The evaluation numbers are printed by the evaluation job from its own run on a GPU. The same 30 questions go to every version.",
            "The gates' verdicts and reasons are returned by the promotion command and also stored as audit records in S3, which I count.",
            "The running version is read from a file the init container wrote inside the live server pod, not from my configuration.",
        ],
        "why": [
            "v3 is refused because its quality is far below production's. Removing four fifths of the weights by magnitude destroys the answers; the gate compares the score with the floor and with production's score.",
            "v2 passes because its score is not below production's by more than the allowed regression. It scored higher than the original (28 of 30 against 25). With 30 questions, a gap of three answers is within noise, so read it as 'did not get worse', not 'got better': the gate exists to catch a model that regressed, not to rank near-equals.",
            "Serving runs the promoted version because the init container reads the production pointer and copies only that version. A rollback is a pointer change and a restart.",
        ],
    },
    "S07": {
        "intro": "Chat and agent traffic sends many questions about the same long prompt. If each question goes to a replica that already holds that prompt's cache, the first token comes sooner. "
                 "I send the same traffic through the gateway's endpoint picker and through a plain Service, and measure how much was already cached.",
        "trust": [
            "The cache numbers are the model servers' own counters (tokens asked about and tokens found cached), read before and after each run from every replica.",
            "Both runs use fresh documents, so one run cannot warm the cache for the other.",
            "I ran this comparison four times while building it. The gateway's hit rate was 0.81 to 0.822 and the plain Service's was 0.654 to 0.674. Time to first token at the median was between 0.080 and 0.117 s through the gateway and between 0.141 and 0.656 s through the plain Service. The gap in time varied more than the gap in cache hits, so trust the direction and the cache numbers more than any one time.",
            "In one of those runs two of the 48 requests through the gateway failed, and the 95th-percentile time to first token was 6.6 s. I had not recorded error details yet and could not reproduce it in the next run, which had none. I report it rather than hide it.",
            "Time to first token is measured by the client on streamed responses.",
            "The load generator runs inside the cluster, so the numbers do not include my laptop's network.",
        ],
        "why": [
            "The picker finds more cached tokens because it scores each replica on queue depth, KV-cache use and prefix match, and the prefix score pulls a follow-up question to the replica that saw the document.",
            "The plain Service's kube-proxy spreads connections over both replicas without looking at their caches, so a long document is often processed on both.",
            "First tokens come sooner through the picker because a cached prefix is not processed again.",
        ],
    },
    "S08": {
        "intro": "An autoscaler that watches GPU utilization scales too late, because a busy GPU looks the same at any queue length. This one watches requests waiting. "
                 "I load one replica until its queue grows, watch a second replica start, and break down where its time went. Then I repeat it with a warm node waiting.",
        "trust": [
            "The autoscaling signal is the one in the live ScaledObject. The queue depth is read from Prometheus.",
            "The time breakdown uses timestamps recorded by Kubernetes (pod created, scheduled, init finished, ready) and by the kubelet (image pull durations), for the actual replica that was added.",
            "Whether a new node was bought is decided by comparing the node's creation time with the pod's.",
        ],
        "why": [
            "The replica is added because requests wait in vLLM's queue (it runs at most eight sequences at once), KEDA reads the sum from Prometheus, and the threshold is four per replica.",
            "A cold start costs a node, an image pull, a weight download and a model load. The placeholder pod removes the first two: it holds a GPU node, with the serving image already pulled, at a priority below the replica's.",
            "The replica takes the buffer's place because its pod has a higher priority class, so the scheduler evicts the placeholder.",
        ],
    },
    "S09": {
        "intro": "The design says GPUs should not sit idle while work waits. Serving has two GPUs of quota and one replica running. I run a resumable batch job on the idle one, then ask for the second replica and watch the GPU change hands.",
        "trust": [
            "Borrowing is read from the queues' own usage counters.",
            "The preemption reason is Kueue's own condition message on the batch Workload.",
            "Resumption is read from the batch pod's log after re-admission: it prints the item it resumed from, which it read from S3.",
        ],
        "why": [
            "The batch job can run because its queue lists serving's flavor with no quota of its own and serving may lend one GPU.",
            "It is preempted because serving's queue is within its own quota and reclaims from the cohort first from whoever borrowed.",
            "It resumes where it stopped because it records progress in S3 after each item.",
            "One earlier run of this scenario passed every check except the last: the resumed job had not finished 15 minutes after it resumed, and I did not keep enough evidence to say why. I added diagnostics to the scenario (it now records the job, workload and pod state when this wait times out) and ran it again; it completed. This is the run shown.",
        ],
    },
    "S10": {
        "intro": "Some models do not fit on one node. A LeaderWorkerSet runs one replica as a leader pod and a worker pod and manages them as one unit. I run a small model split across two nodes to test the mechanism, and then kill the worker.",
        "trust": [
            "The two nodes, the one Workload and the two pods' GPU memory are read from the cluster and from `nvidia-smi` in each pod.",
            "The pipeline stages are named in each pod's own log.",
            "The restart of the whole group is shown by the leader pod's UID changing after only the worker was deleted.",
        ],
        "why": [
            "Both pods are admitted together because Kueue's LeaderWorkerSet integration builds one Workload for the group.",
            "The model is split because vLLM runs with pipeline parallelism of two across two nodes, and each pod holds one stage.",
            "The group restarts as one because the restart policy is to recreate the group when any pod restarts.",
        ],
    },
    "S11": {
        "intro": "Every group has its own numbers: researchers care how long they wait, training how many GPU hours made progress, serving how long users wait for the first token. I read them from the work the earlier scenarios did.",
        "trust": [
            "Every number comes from Prometheus or from the events the training script wrote to S3. Nothing is typed in.",
            "The queries are shown with each result.",
        ],
        "why": [
            "GPU hours can be split by family because every pod carries its namespace and the namespace is the family.",
            "Time to first token and cache hit rate are available because vLLM exports them and Prometheus scraped every replica, including replicas that no longer exist.",
            "GPU utilization is available because a DCGM exporter runs on every GPU node and joins each GPU's counters to the pod holding it.",
        ],
    },
}

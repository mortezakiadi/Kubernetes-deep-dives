"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""

PROSE = {
    "S01": {
        "intro": "Before testing behaviour I check that the fleet is what the design says: several clusters made from one definition, on one Kubernetes version, "
                 "running one pinned queue controller, with the hub connected to every worker and no GPU capacity running when nobody asked for it.",
        "trust": [
            "Cluster state is read from the EKS API, the Kueue image from each cluster's own Deployment, and the connections from the hub's own MultiKueueCluster objects. None of it is read back from my configuration files.",
            "The 'no GPU at rest' check looks at the capacity every node reports, so a GPU node hidden under another name would still be counted.",
        ],
        "why": [
            "The four clusters match because they are one Terraform resource written once and repeated by a map. The role label on each comes from that map.",
            "Kueue is identical everywhere because one module installs it with one pinned chart version on every cluster.",
            "The hub reports each worker Active because Terraform gave every worker a narrow identity, stored that identity's kubeconfig in the hub, and created a MultiKueueCluster for it.",
            "No GPU node is running because the GPU NodePools start empty and their limits are set in the NodePool, so nothing is bought until an admitted job needs it.",
        ],
    },
    "S02": {
        "intro": "This scenario is the user's view of the platform: submit once to one place. The job has to run somewhere else, report back, and leave nothing running on the hub. "
                 "I also try to cheat by running a pod on the hub directly.",
        "trust": [
            "The worker's name comes from the hub's own Workload record, and I then look for the pods in exactly that worker.",
            "The 'no pod on the hub' check lists pods in the team's namespace on the hub. An empty list is a measured result, not an assumption.",
            "The cleanup check looks in the worker after the job finished, so a leftover copy would fail the test.",
        ],
        "why": [
            "The direct pod is refused because the hub carries a ValidatingAdmissionPolicy that denies every pod in a team namespace.",
            "The hub has no pods because MultiKueue keeps the job suspended there. Only the worker that admitted the Workload runs it.",
            "The status is visible on the hub because MultiKueue watches the remote job and copies its status back.",
            "The remote copy disappears because the hub deletes the objects it created in the worker after the final status sync.",
        ],
    },
    "S03": {
        "intro": "A researcher asks why her job has been pending for an hour. The design's answer is that every wait has one owner. I create the two ways to wait: too big for the team's fleet-wide share, "
                 "and within the fleet share but too big for any single worker, and read each reason from the system that holds the job.",
        "trust": [
            "The table is produced by a script that reads the Workload objects of every cluster and prints the condition message each Kueue wrote. I do not write the reasons.",
            "The pod count is read from all four clusters. Zero is a measured value.",
        ],
        "why": [
            "The first job waits at the hub because the team's fleet-wide queue has quota for fewer CPUs than the job asks for.",
            "The second job gets a quota reservation at the hub (the team's fleet share is large enough) and is then offered to the workers. Each worker's queue can hold at most the team's share there plus what it may borrow, which is less than the job needs, so every copy waits at layer 2.",
            "No pods exist because Kueue keeps a job suspended until the whole job is admitted.",
        ],
    },
    "S04": {
        "intro": "Gang scheduling is the property that a distributed job starts all at once or not at all. Without it, a 4-pod job on a cluster with room for 3 starts 3 pods, which then hold resources and wait for the 4th. I test the property directly on one worker.",
        "trust": [
            "The test talks to the worker's own Kueue, so the decision is that worker's and not the hub's.",
            "The pod list is read from the worker's API after the job has had time to start. If pods existed, the check would fail.",
            "A control job one pod smaller is admitted in the same queue, so a bug that blocks everything would show.",
        ],
        "why": [
            "The 4-pod job waits because the team's queue on this worker can use 3 CPUs at most: 1 of its own and 2 it may borrow.",
            "No pods are created because the Job is created suspended and Kueue unsuspends it only once all of its pods fit.",
            "The 3-pod job is admitted because 3 CPUs are within what the queue may use.",
        ],
    },
    "S05": {
        "intro": "Static per-team pools leave capacity idle. The design's answer is a cohort: each team has a guaranteed share, can borrow what others are not using, and gets it back when it asks. "
                 "I run exactly that sequence: a borrower takes idle capacity, then the owner comes back.",
        "trust": [
            "Usage and borrowed amounts are read from each ClusterQueue's own status.",
            "The preemption is read from the borrower's Workload conditions, and the message is Kueue's own.",
            "The workload still exists after preemption, so the check for 'requeued, not lost' is not vacuous.",
        ],
        "why": [
            "Research is admitted with 3 CPUs because the cohort lets it borrow up to its borrowing limit, from queues that lend. llm lends one CPU and vision lends one.",
            "llm is admitted because it is asking for its own guaranteed share and the queue allows preemption of borrowers (`reclaimWithinCohort: Any`).",
            "Research's job is preempted and requeued because it was running on borrowed quota, which is the first thing Kueue takes back.",
        ],
    },
    "S06": {
        "intro": "The hub offers a job to several workers and the first to admit wins. I make one worker unable to take a team's job and show that the other one does.",
        "trust": [
            "The setup is verified before the test: the usage table shows the first worker's queue full.",
            "The chosen cluster is read from the hub's Workload. The cleanup check reads the other worker's Workloads.",
        ],
        "why": [
            "The job goes to cpu-b because the hub copied it to both CPU workers and cpu-a's queue for that team was full, so only cpu-b could admit it.",
            "cpu-a holds no copy afterwards because the hub deletes the copies in the workers that did not win.",
        ],
    },
    "S07": {
        "intro": "GPUs are the scarce, expensive resource, so the design controls them four ways: through queues, by flavor, by scaling from zero, and by closing every side door. This scenario runs real GPU jobs and then tries to get a GPU without permission.",
        "trust": [
            "The flavor each job received is read from the Workload that admitted it in the GPU worker.",
            "The jobs print `nvidia-smi -L` from inside the pod, so the GPU is shown by the driver, not assumed from a resource request.",
            "The side-door tests create the forbidden pods for real and read their phase, their scheduling gate and the node list before and after.",
            "The final check waits for the GPU nodes to disappear. It fails if they do not.",
        ],
        "why": [
            "The first llm job gets on-demand capacity and the second gets Spot because llm's queue lists flavors in that order and owns one of each.",
            "Research gets Spot because its queue does not list the on-demand flavor. That is the 'weekend experiment cannot buy on-demand' rule.",
            "Which GPU model a job gets depends on which allowed instance family had capacity when the node was bought, so the test checks for a GPU and does not hard-code one. In my first attempt the `g6` family had no capacity and Auto Mode launched a `g4dn` node.",
            "GPU nodes appear only after a job is admitted because the NodePools start empty and only admitted pods carry the toleration the flavor adds.",
            "A bare GPU pod in a team namespace is held because Kueue manages every pod in a team namespace and gates the ones with no queue. In an unmanaged namespace it stays Pending because no pool can satisfy it: GPU nodes are tainted and the platform pool has no GPU instance types.",
            "The nodes disappear because the NodePool removes empty nodes after two minutes.",
        ],
    },
    "S08": {
        "intro": "Teams share clusters, so one team must not be able to see another's pods or read another's data. I test the network wall and the identity wall, each with a positive control so a block cannot be mistaken for a broken network.",
        "trust": [
            "Each probe is a real job that runs in a team namespace and reports what it got. The server runs in another team's namespace on the same worker.",
            "The same-namespace probe is the positive control. If the network were simply down it would fail too.",
            "The identity test uses the AWS CLI inside the job, so the answer (data or AccessDenied) is IAM's own.",
        ],
        "why": [
            "Cross-team traffic is dropped because each team namespace has a policy that admits ingress only from its own namespace.",
            "Same-namespace traffic works because that policy allows it.",
            "A team's job reads its own prefix because EKS Pod Identity associates the team's ServiceAccount with a role whose policy lists only that prefix. Another team's prefix is refused by IAM.",
        ],
    },
    "S09": {
        "intro": "The design's failure table says how the fleet behaves when a worker or the hub is lost. I cut the hub off from one worker, and I take the hub's queue controller away.",
        "trust": [
            "A worker is 'lost' by breaking the credentials the hub holds for it, the real way a worker becomes unreachable to the hub. The original value is restored afterwards.",
            "The job during the outage is dispatched for real, and the cluster it ran in is read from the Workload.",
            "The job that survives the hub outage is observed in the worker while the hub's controller has zero replicas.",
        ],
        "why": [
            "The hub marks the worker inactive because MultiKueue tests each connection and records the result on the MultiKueueCluster.",
            "New work goes to the other worker because the hub offers jobs only to active workers.",
            "A running job is unaffected by the hub being down because, once dispatched, it runs entirely inside the worker. The hub is not in the data path.",
        ],
    },
    "S10": {
        "intro": "Everyone claims to be GPU-starved and finance says utilization is low. The design needs one place to see both. I run a few jobs and read the fleet-wide view.",
        "trust": [
            "The tables are produced from each Kueue's own objects by a script in the repository, and the output is shown whole.",
        ],
        "why": [
            "Every job shows its worker because the hub's Workload records the chosen cluster.",
            "Every queue shows its pending count, admitted count and borrowed amount because each ClusterQueue keeps those in its status.",
        ],
    },
}

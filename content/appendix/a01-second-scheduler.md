---
id: appendix-second-scheduler
title: "Appendix A: Running a second scheduler on a managed cluster"
short: "A. Running a second scheduler"
---
This appendix is a complete, ready-to-apply guide for one specific situation: you want different scheduling rules for *some* of your workloads (different plugin weights, or bin-packing instead of spreading), but you're on a managed cluster such as EKS, GKE or AKS, where you can't change the default scheduler's configuration.

The answer is to run a **second scheduler** next to the default one. It's the same official kube-scheduler program, deployed by you as an ordinary Deployment, with its own `KubeSchedulerConfiguration`. Pods choose it by setting `schedulerName`; every other pod keeps using the default scheduler. For background, see [Phase 1: filtering](#d01-1) (the note on `KubeSchedulerConfiguration`), [Phase 2: scoring plugins](#d01-3) and [Built-in plugins, and what a second scheduler changes](#d01-9).

### When you need a second scheduler

**Scenario.** A platform team runs batch jobs and web services on the same EKS cluster. Batch pods are spread evenly across nodes, so dozens of nodes stay 30% full and Karpenter can never remove them. The team wants batch pods packed tightly, but EKS doesn't let anyone change the managed scheduler's scoring. Before deploying anything, they need to know whether a second scheduler is really the right tool.

![When you need a second scheduler](diagrams/appendix/a01-1.svg)

**What the picture shows.** A decision path from "I want different placement" to the lightest tool that achieves it.

- **Can the pod say it itself?** Node affinity, pod affinity and anti-affinity, topology spread constraints, taints and tolerations change where *one workload* goes, with no new components. Try these first. See [Phase 1: filtering](#d01-1).
- **Do you need to change the scoring itself?** Plugin weights, bin-packing (`MostAllocated`) instead of spreading, or turning a plugin off can't be expressed in a pod spec. Only a scheduler configuration can do it, and on a managed cluster the only configuration you control is your own scheduler's.
- **Is the problem really about jobs, not pods?** All-or-nothing starts, quotas between teams and queueing are job-level problems. Use Kueue, Volcano or KAI (see [Volcano vs. Kueue](#d02-6)), not a second kube-scheduler.
- **Do you only need bin-packing for node cost?** On EKS, Karpenter consolidation already repacks nodes after the fact. A second scheduler packs at placement time, so nodes empty out sooner, but it's one more component to run.
- **The answer for the scenario:** batch needs different *scoring*, and only for batch pods, so a second scheduler with a bin-packing profile is the right tool.

**Going deeper.**

- **A second scheduler is still kube-scheduler.** Same filters, same plugins, same API fields; only the configuration differs. That's why it's far less risky than writing a custom scheduler from scratch.
- **Profiles versus schedulers:** on a self-managed cluster you'd add a second *profile* to the default scheduler instead. On a managed cluster you can't, so you run your own kube-scheduler process that holds one or more profiles of its own.

### How two schedulers share one cluster

**Scenario.** Before deploying, the team wants to understand what happens when two schedulers watch the same pods and nodes. Who decides which scheduler handles a pod, and what stops them from fighting over it?

![How two schedulers share one cluster](diagrams/appendix/a01-2.svg)

**What the picture shows.** Two independent scheduler processes, one API server, and the field that splits the work between them.

- **`spec.schedulerName` routes the pod:** every pod has it; when it's unset, the API server fills in `default-scheduler`. Each scheduler only picks up pods whose `schedulerName` matches one of its profiles and ignores the rest.
- **Both read the same cluster state:** each scheduler keeps its own cache of nodes and pods, built from watches on the API server. Neither knows the other exists.
- **Binding is the only write:** a scheduler's decision is a Binding that sets the pod's `nodeName`. After that, the kubelet on that node runs the pod.
- **Leader election per scheduler:** each scheduler runs two replicas and uses its own Lease object, so only one replica of each is active. The default scheduler's lease and yours never collide because the names differ.
- **The kubelet is the referee:** if both schedulers place pods on the same node at the same moment and together they don't fit, the kubelet rejects the pod that arrives second (an admission failure such as `OutOfcpu`). The rejected pod is marked as failed, and its controller replaces it.

**Going deeper.**

- **The race is rare but real.** Each scheduler assumes its own placements immediately, but only learns about the other scheduler's placements through watch events a moment later. It matters most on full nodes, which is exactly where a bin-packing scheduler aims. That's why the guide gives the second scheduler its own nodes (see [Giving it its own nodes](#a01-4)).
- **`schedulerName` can't be changed after a pod is created.** Moving a workload between schedulers means changing its pod template, which rolls out new pods.

### Step 1: identity and permissions

**Scenario.** The first attempt at deploying the scheduler starts fine, but neither replica ever becomes the leader, and the logs fill with "leases.coordination.k8s.io is forbidden". The built-in scheduler role was written for the default scheduler, and it doesn't cover a lease with a different name.

**What the YAML does.**

- **A ServiceAccount, `my-scheduler`,** in `kube-system`, used by the scheduler's pods.
- **Reuses the built-in roles:** bound to `system:kube-scheduler` (read pods and nodes, create bindings, update pod status) and `system:volume-scheduler` (needed for pods with PersistentVolumeClaims), so you don't maintain your own copy of the scheduler's permissions.
- **Reads the API server's authentication settings:** the `extension-apiserver-authentication-reader` Role, so the scheduler's own HTTPS endpoint can authenticate requests to `/metrics`.
- **Adds the lease permission the built-in role lacks:** `system:kube-scheduler` lets the default scheduler update only the lease named `kube-scheduler`. A small extra Role lets your scheduler get and update its own lease, `affinity-first-scheduler`. Without it, leader election fails and nothing gets scheduled.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: my-scheduler
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: my-scheduler-as-kube-scheduler
subjects:
- { kind: ServiceAccount, name: my-scheduler, namespace: kube-system }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:kube-scheduler        # read pods/nodes, bind pods #!
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: my-scheduler-as-volume-scheduler
subjects:
- { kind: ServiceAccount, name: my-scheduler, namespace: kube-system }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:volume-scheduler      # needed for pods with PVCs #!
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: my-scheduler-auth-reader
  namespace: kube-system
subjects:
- { kind: ServiceAccount, name: my-scheduler, namespace: kube-system }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: extension-apiserver-authentication-reader
---
# The built-in role only covers the lease named "kube-scheduler"
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: my-scheduler-lease
  namespace: kube-system
rules:
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["create"]
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  resourceNames: ["affinity-first-scheduler"]   # must match leaderElection.resourceName #!
  verbs: ["get", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: my-scheduler-lease
  namespace: kube-system
subjects:
- { kind: ServiceAccount, name: my-scheduler, namespace: kube-system }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: my-scheduler-lease
```

**Going deeper.**

- **Don't edit the built-in ClusterRole.** Some guides add your scheduler's name to `system:kube-scheduler` itself. On a managed cluster that role is owned by the provider and can be reset on upgrade; a separate Role survives upgrades.
- **Least privilege still applies.** The scheduler can bind any pod in the cluster to any node. Treat its ServiceAccount, its namespace and its image as part of the control plane: restrict who can change them.

### Step 2: the scheduler's configuration

**Scenario.** The team's batch nodes are 32-CPU machines. With the default spreading behaviour, 40 batch pods of 4 CPUs each land on 20 nodes at 25% each. Packed tightly, the same pods need 5 nodes, and Karpenter can remove the rest.

![Step 2: the scheduler's configuration](diagrams/appendix/a01-3.svg)

**What the picture shows.** The same pods placed by two scoring strategies, and the configuration that changes one into the other.

- **`LeastAllocated` (the default):** NodeResourcesFit scores emptier nodes higher, so pods spread out. Good for availability, bad for node cost.
- **`MostAllocated` (bin-packing):** the same plugin scores fuller nodes higher, so pods fill one node before the next. Empty nodes can be removed by the autoscaler.
- **Resource weights:** the `resources` list decides which resources count when measuring "full". For GPU clusters, add `nvidia.com/gpu` with a high weight so GPU nodes fill first.
- **Plugin weights:** the profile also raises InterPodAffinity to 100 (pods that want to be together win over everything else) and lowers ImageLocality to 1.
- **Leader election:** on, with its own lease name, so two replicas can run safely.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-scheduler-config #!
  namespace: kube-system
data:
  config.yaml: |
    apiVersion: kubescheduler.config.k8s.io/v1
    kind: KubeSchedulerConfiguration
    leaderElection:
      leaderElect: true                # 2 replicas, only one active #!
      resourceNamespace: kube-system
      resourceName: affinity-first-scheduler
    profiles:
    - schedulerName: affinity-first-scheduler #!
      plugins:
        score:
          enabled:
          - name: InterPodAffinity
            weight: 100
          - name: ImageLocality
            weight: 1
      pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: MostAllocated        # bin-pack instead of spread #!
            resources:
            - name: cpu
              weight: 1
            - name: memory
              weight: 1
```

**Going deeper.**

- **One process, several profiles.** A single scheduler Deployment can hold more than one profile, each with its own `schedulerName`, for example a CPU bin-packing profile and a GPU bin-packing profile. They share one cache and one leader election, which is cheaper than running two Deployments.

```yaml
    profiles:
    - schedulerName: binpack-cpu
      pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: MostAllocated
            resources: [ { name: cpu, weight: 1 }, { name: memory, weight: 1 } ]
    - schedulerName: binpack-gpu
      pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: MostAllocated
            resources:
            - { name: nvidia.com/gpu, weight: 10 }   # fill GPU nodes first #!
            - { name: cpu, weight: 1 }
            - { name: memory, weight: 1 }
```

- **`RequestedToCapacityRatio`** is the third scoring strategy: you draw a curve of score against utilization, for example "best at 80% full, worse above 95%", if pure bin-packing leaves no headroom.
- **Bin-packing has a cost:** more pods per node means a bigger blast radius when a node fails. Keep it for batch and other interruption-tolerant work, and leave latency-sensitive services on the default scheduler.
- **`percentageOfNodesToScore`** limits how many feasible nodes are scored on very large clusters. Leave the default unless scheduling latency is a measured problem.

### Step 3: run the scheduler

**Scenario.** The scheduler is now part of the control plane for every batch pod. If it goes down, batch pods sit in `Pending` with no explanation. It needs to be run like the control-plane component it is.

**What the YAML does.**

- **Two replicas, spread across zones:** leader election keeps only one active; the other takes over within seconds if the leader fails. Topology spread puts them in different zones.
- **High priority:** `system-cluster-critical`, so the scheduler is never evicted to make room for the pods it's supposed to place.
- **The official image, pinned to your cluster's version:** `registry.k8s.io/kube-scheduler` with `--config` pointing at the mounted ConfigMap.
- **Health checks on the secure port (10259):** the liveness probe restarts a hung scheduler.
- **A PodDisruptionBudget:** node drains and Karpenter consolidation can never take both replicas down at once.
- **Runs on nodes it doesn't schedule onto:** the scheduler itself is placed by the *default* scheduler on system nodes, so it doesn't depend on itself to start.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-scheduler
  namespace: kube-system
spec:
  replicas: 2 #!
  selector:
    matchLabels: { component: my-scheduler }
  template:
    metadata:
      labels: { component: my-scheduler }
    spec:
      serviceAccountName: my-scheduler
      priorityClassName: system-cluster-critical #!
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: topology.kubernetes.io/zone
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels: { component: my-scheduler }
      containers:
      - name: kube-scheduler
        image: registry.k8s.io/kube-scheduler:v1.36.0   # match your cluster's version #!
        command:
        - kube-scheduler
        - --config=/etc/kubernetes/my-scheduler/config.yaml #!
        livenessProbe:
          httpGet: { path: /healthz, port: 10259, scheme: HTTPS }
          initialDelaySeconds: 15
        readinessProbe:
          httpGet: { path: /healthz, port: 10259, scheme: HTTPS }
        resources:
          requests: { cpu: 100m, memory: 128Mi }
          limits:   { memory: 512Mi }
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          runAsNonRoot: true
          runAsUser: 65532
          capabilities: { drop: ["ALL"] }
        volumeMounts:
        - { name: config, mountPath: /etc/kubernetes/my-scheduler, readOnly: true }
      volumes:
      - name: config
        configMap:
          name: my-scheduler-config #!
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: my-scheduler
  namespace: kube-system
spec:
  minAvailable: 1
  selector:
    matchLabels: { component: my-scheduler }
```

**Going deeper.**

- **Memory grows with cluster size.** The scheduler caches every node and pod. Watch its memory on large clusters and raise the limit before it's OOM-killed, which would leave pods unscheduled until it restarts.
- **Config changes need a restart.** kube-scheduler reads its configuration at startup. Roll the Deployment after changing the ConfigMap (a checksum annotation on the pod template does this automatically in GitOps setups).

### Step 4: opt workloads in

**Scenario.** The team adds `schedulerName` to three batch Deployments by hand. A month later, a new batch job from another team lands on the default scheduler because nobody told them. Opting in should be a property of the namespace, not something every author has to remember.

**What the YAML does.**

- **Per workload:** set `schedulerName` in the pod template. Here the batch Deployment also tolerates the batch node pool's taint and selects it (see the next section).
- **Per namespace, automatically:** a mutating admission policy (Kyverno here) sets `schedulerName` on every new pod in namespaces labelled `scheduling.example.com/profile: binpack`, so teams opt in once, at the namespace.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nightly-etl
  namespace: batch
spec:
  replicas: 40
  selector:
    matchLabels: { app: nightly-etl }
  template:
    metadata:
      labels: { app: nightly-etl }
    spec:
      schedulerName: affinity-first-scheduler #!
      nodeSelector:
        workload-class: batch
      tolerations:
      - { key: workload-class, value: batch, effect: NoSchedule }
      containers:
      - name: etl
        image: my-company/etl:2.3
        resources:
          requests: { cpu: "4", memory: 8Gi }
---
# Opt in a whole namespace instead of editing every workload
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: batch-namespaces-use-binpack-scheduler
spec:
  rules:
  - name: set-scheduler-name
    match:
      any:
      - resources:
          kinds: ["Pod"]
          namespaceSelector:
            matchLabels: { scheduling.example.com/profile: binpack } #!
    mutate:
      patchStrategicMerge:
        spec:
          schedulerName: affinity-first-scheduler
```

**Going deeper.**

- **The mutation must happen at creation,** because `schedulerName` is immutable. Existing pods keep their scheduler until they're replaced.
- **Exclude what shouldn't move:** DaemonSet pods are placed by their own controller logic and don't need this, and system namespaces should never be labelled for opt-in.

### Giving it its own nodes

**Scenario.** After a week, the team sees a handful of batch pods fail with `OutOfcpu` right after being scheduled. Both schedulers were placing pods on the same nearly full nodes at the same moment. The fix is to keep them out of each other's way.

![Giving it its own nodes](diagrams/appendix/a01-4.svg)

**What the picture shows.** Two node pools, and how taints and selectors keep each scheduler on its own.

- **The batch pool is tainted** `workload-class=batch:NoSchedule`. Pods from the default scheduler don't tolerate it, so they never land there.
- **Batch pods tolerate the taint and select the pool** with a `nodeSelector`, so the second scheduler only places them on batch nodes.
- **No overlap, no race:** each scheduler is the only writer to its own nodes, so they can't make conflicting placements.
- **Each pool has its own Karpenter NodePool:** the batch pool can use Spot and aggressive consolidation without affecting services.

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: batch
spec:
  template:
    metadata:
      labels:
        workload-class: batch #!
    spec:
      taints:
      - { key: workload-class, value: batch, effect: NoSchedule } #!
      requirements:
      - { key: karpenter.sh/capacity-type, operator: In, values: ["spot", "on-demand"] }
      - { key: karpenter.k8s.aws/instance-category, operator: In, values: ["c", "m"] }
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 2m
  limits:
    cpu: "2000"
```

**Going deeper.**

- **DaemonSets still run everywhere** they tolerate, on both pools. Their pods are part of every node's capacity, and both schedulers account for them.
- **Shared pools are possible** if you accept the occasional rejected pod. For pods managed by a controller the cost is a short delay while it's recreated; for bare pods it's a failure you'll have to handle.

### Autoscaling with a second scheduler

**Scenario.** The batch pool is empty overnight. At 2 a.m., 40 batch pods are created, and the team wants Karpenter to launch nodes for them. Does Karpenter even notice pods that belong to a different scheduler?

![Autoscaling with a second scheduler](diagrams/appendix/a01-5.svg)

**What the picture shows.** How a pod handled by the second scheduler gets a new node.

- **Step 1, the pod is created** with `schedulerName: affinity-first-scheduler`.
- **Step 2, the second scheduler tries and fails:** no node fits, so it sets the pod's `PodScheduled` condition to `False` with reason `Unschedulable`.
- **Step 3, Karpenter reacts to that condition:** it watches for pods marked unschedulable, whichever scheduler marked them, works out which NodePool can host them and launches nodes.
- **Step 4, the new node registers,** and the second scheduler's next attempt finds room and binds the pod.
- **If the second scheduler is down, nothing happens:** the pod is never marked unschedulable, so Karpenter never sees it. That's why the scheduler's own availability matters so much.

**Going deeper.**

- **Karpenter simulates with its own logic,** which respects the pod's constraints (requests, selectors, affinity, tolerations) but not your scheduler's *scoring*. Karpenter decides how many nodes to buy; your scheduler then decides which pod goes where.
- **Consolidation can move your pods:** when Karpenter removes an underused node, the evicted pods are recreated and placed again by whichever scheduler their `schedulerName` names. Bin-packing at placement and consolidation later point in the same direction, so they work well together.
- **Cluster Autoscaler** also reacts to unschedulable pods, and simulates placement with the scheduler's filter plugins. It has the same blind spot for custom scoring.

### Checking that it works, and troubleshooting

**Scenario.** On Monday a developer says "my pods have been Pending since Friday". The on-call engineer needs a fixed sequence of checks that finds the cause in minutes.

![Checking that it works, and troubleshooting](diagrams/appendix/a01-6.svg)

**What the picture shows.** A troubleshooting path for a pod that's stuck in `Pending`.

- **Is the pod asking for the right scheduler?** `kubectl get pod <pod> -o jsonpath='{.spec.schedulerName}'`. A typo means no scheduler will ever pick it up.
- **Is the scheduler running and leading?** `kubectl -n kube-system get pods -l component=my-scheduler` shows the replicas; `kubectl -n kube-system get lease affinity-first-scheduler` shows the current holder. No holder means leader election is failing (check the lease permissions from Step 1).
- **Are there any events?** `kubectl describe pod <pod>`. *No events at all* means no scheduler has looked at it. A `FailedScheduling` event from `affinity-first-scheduler` means it looked and nothing fit; the message lists why (insufficient CPU, untolerated taint, affinity).
- **Is autoscaling reacting?** For `Unschedulable` pods, check Karpenter's logs or NodeClaims for a launch. No NodeClaim means no NodePool matches the pod's constraints.
- **Did it get placed by the right scheduler?** `kubectl get events --field-selector reason=Scheduled` lists each placement with the scheduler that made it.

**Going deeper.**

- **Metrics to alert on:** the scheduler exposes Prometheus metrics on port 10259, including `scheduler_pending_pods` (by queue), `scheduler_schedule_attempts_total` (by result and profile) and `scheduler_scheduling_attempt_duration_seconds`. Alert when pending pods for your profile grow while the scheduler's attempt rate is zero: that's a stopped scheduler.
- **Raise the log level temporarily** (`--v=4`) to see each filter's verdict per node, then turn it back down; it's very verbose.

### Operating it over time

**Scenario.** A year later, the cluster has been upgraded three times and the second scheduler is still on the version it was installed with. A new pod field added in the latest release is silently ignored by it. A second scheduler is a control-plane component, and it needs the same care.

**What to keep in mind.**

- **Upgrade with the cluster:** bump the scheduler image to match each control-plane upgrade, in the same change. A scheduler older than the API server may not understand newer pod fields, and quietly places pods as if those fields weren't there.
- **Review the configuration API on upgrades:** `kubescheduler.config.k8s.io/v1` is stable, but plugins and defaults change between releases; read the scheduler section of the release notes.
- **Keep it in Git:** the ServiceAccount, roles, ConfigMap, Deployment, PDB and opt-in policy are one unit. Deploy them together through the same pipeline as the rest of the platform.
- **Know how to back out:** removing the opt-in policy and the `schedulerName` from pod templates sends workloads back to the default scheduler on their next rollout. Keep that path tested.

| Failure | What you see | What to do |
|---|---|---|
| Scheduler pods down | Opted-in pods Pending with no events; no scale-up | Two replicas, PDB, alert on pending pods with zero attempts |
| Leader election failing | Both replicas running, nothing scheduled, "forbidden" in logs | Check the lease Role and `resourceName` match |
| Version behind the cluster | New pod fields ignored | Upgrade the image with every cluster upgrade |
| Shared nodes with the default scheduler | Occasional `OutOfcpu` or `OutOfmemory` pod failures | Give it its own tainted node pool |
| Typo in `schedulerName` | One workload Pending forever, no events | Use the namespace opt-in policy instead of hand-edited names |

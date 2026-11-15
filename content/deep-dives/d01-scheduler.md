---
id: d-scheduler
title: "How kube-scheduler picks a node"
short: "Kubernetes Scheduler"
---
Every pod goes through the same decision. The scheduler first **chooses** a node in two phases: it *filters* out the nodes that can't run the pod, then *scores* the ones that can. Then it **binds** the pod to the winner, which is a separate final step that just records the choice. This deep dive follows that decision step by step.

### Phase 1: filtering

**Scenario.** A developer deploys a new service, but the pod never starts. It sits in `Pending`, and `kubectl describe pod` shows `0/12 nodes are available`. The developer is confused: the monitoring dashboard shows every node at around 35% CPU, so there seems to be plenty of room. Why won't Kubernetes place the pod, and how do you find out which rule is blocking it?

![Phase 1: filtering](diagrams/sections/d01-1.svg)

**What the picture shows.** The scheduler chooses a node in two phases, **filtering** then **scoring**, and then binds the pod to the chosen node. This picture is the first phase: find every node that is physically and logically able to host the pod. Each node runs through a series of checks (filter plugins, formerly called predicates), and failing even one check removes it.

- **Kubernetes scheduler ("EKS Scheduler" box):** the "brain" that decides where every new pod lives.
- **Phase 1: Filtering (the "predicate" phase):** the goal is simple, keep only the nodes that can run the pod.
- **Insufficient resources (the fit check):** the scheduler takes the node's total capacity and subtracts the *requests* of the pods already there, then checks that what's left is at least the new pod's request. This is the answer to the scenario: the dashboard shows real usage, but the scheduler only counts requests. A node can be 35% busy and still be fully booked on paper. This is a yes/no feasibility check, not bin-packing. It's done by a scheduler plugin called **NodeResourcesFit**, which lives inside kube-scheduler, so you never write its name in your pod: the pod only supplies the `requests`, and the plugin's own settings sit in the scheduler's configuration file (both are shown below). You normally never write that file: without it the scheduler runs with built-in defaults, and on managed control planes like EKS you can't change it at all. The check doesn't prefer full or empty nodes. That preference is decided later, in scoring, where the default strategy (LeastAllocated) actually *spreads* pods; bin-packing (MostAllocated) has to be turned on deliberately (see [Phase 2: scoring plugins](#d01-3)).

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: api
spec:
  containers:
  - name: api
    image: my-company/api:1.4
    resources:
      requests:          # the scheduler adds up requests, not real usage #!
        cpu: "500m" #!
        memory: "1Gi" #!
---
# kube-scheduler's own config file, passed with --config. NOT applied with kubectl.
# Without it, the scheduler uses these same built-in defaults.
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
- schedulerName: default-scheduler
  pluginConfig:
  - name: NodeResourcesFit     # filter: does the pod fit? + score: which node is best? #!
    args:
      scoringStrategy:
        type: LeastAllocated   # the default (spread); MostAllocated = bin-pack #!
        resources:
        - name: cpu
          weight: 1
        - name: memory
          weight: 1
```

> **Note: is `KubeSchedulerConfiguration` something you add?** Normally, no; it's there by default.
>
> - **It isn't a Kubernetes object.** It has `apiVersion` and `kind`, but you don't `kubectl apply` it (the API would reject it). It's a config file the kube-scheduler program reads at startup, passed with `--config`.
> - **The defaults are built in.** With no file, the scheduler runs with built-in settings that match the snippet: NodeResourcesFit scoring with LeastAllocated, CPU and memory weighted 1 each, and the default plugin weights. The snippet just makes those hidden defaults visible.
> - **Self-managed clusters** (for example kubeadm): you can point the scheduler's static pod at your own file, for example to switch to MostAllocated bin-packing or change plugin weights.
> - **Managed clusters** (EKS, GKE, AKS): the provider runs the control plane, so you can't change the default scheduler's config. To customize, deploy a second scheduler as a normal Deployment with its own `KubeSchedulerConfiguration` from a ConfigMap, and point specific pods at it with `schedulerName`. Volcano and KAI work the same way, as replacement schedulers.

- **Node selector mismatch:** if the pod has a `nodeSelector` (for example `disktype: ssd`), any node without that exact label is dropped.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: api
spec:
  nodeSelector: #!
    disktype: ssd        # only nodes labelled disktype=ssd pass the filter #!
  containers:
  - name: api
    image: my-company/api:1.4
```

- **Taints and tolerations (the "keep out" sign):** a taint lets a node repel pods; only pods with a matching toleration may stay in the list.

```yaml
# taint on the node:  kubectl taint nodes gpu-node-1 dedicated=gpu:NoSchedule
apiVersion: v1
kind: Pod
metadata:
  name: trainer
spec:
  tolerations: #!
  - key: dedicated #!
    operator: Equal
    value: gpu #!
    effect: NoSchedule #!
  containers:
  - name: trainer
    image: my-company/trainer:2.0
```

- **Affinity and anti-affinity violations:** a required node affinity ("must run in us-east-1a") or pod anti-affinity ("never on a node that already runs a copy of me") removes nodes that break the rule.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: api-1
  labels:
    app: api
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution: #!
        nodeSelectorTerms:
        - matchExpressions:
          - key: topology.kubernetes.io/zone
            operator: In
            values: ["us-east-1a"] #!
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution: #!
      - labelSelector:
          matchLabels:
            app: api
        topologyKey: kubernetes.io/hostname   # never two api pods on one node #!
  containers:
  - name: api
    image: my-company/api:1.4
```

- **Port conflicts:** if the pod asks for a `hostPort` (say port 80 on the EC2 instance) and another pod on that node already uses it, the node is out. Two tenants can't share one mailbox.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: edge-proxy
spec:
  containers:
  - name: proxy
    image: nginx:1.27
    ports:
    - containerPort: 8080
      hostPort: 80       # only one pod per node can claim host port 80 #!
```

The `0/12 nodes are available` event lists how many nodes each of these rules removed, which tells you where to look.

**Going deeper.**

- For GPUs, these same filters do the steering.
- `nvidia.com/gpu: 1` is purely resource-based, so every GPU counts the same: a 16 GB T4 and an 80 GB A100 are both "1".
- A `nodeSelector` on GPU labels pins an exact model, node affinity can say "at least 40 GB, prefer Hopper", and a taint on GPU nodes keeps everything else off them.
- Dynamic Resource Allocation (stable since Kubernetes 1.34) lets a pod describe the device it needs, and the driver picks a match at scheduling time.
- Filtering also runs one pod at a time: a distributed job's workers are judged separately, which is the root of the [gang problem](#d02-2).

### Filtering outcomes at a glance

**Scenario.** It's 3 a.m. and the on-call engineer has a pod stuck in `Pending` with a long event message. They need a one-glance reference that turns "which rule failed" into "what to change", without re-reading the scheduler docs.

![Filtering outcomes](diagrams/sections/d01-2.svg)

**What the picture shows.** A cheat sheet for the filtering phase, in two parts.

- **Summary table (top):** each constraint, what kind of rule it is, and what happens when a node fails it.
  - **Resources** (physical): node ignored for insufficient CPU or memory.
  - **Taints** (policy): node ignored unless the pod tolerates the taint.
  - **Affinity** (logical): node ignored because it violates a placement rule.
  - **Selector** (label-based): node ignored because a label doesn't match.
- **The funnel (bottom):** the Kubernetes scheduler starts with a pending pod and all 100+ cluster nodes. They pour through five filters in turn: **resource filter** (CPU/memory over capacity), **node selector filter** (label mismatch), **taint filter** (no toleration), **affinity/anti-affinity filter** (placement violation) and **port conflict filter** (hostPort in use). Nodes rejected at each layer drop out to the sides.
- **Feasible nodes:** whatever reaches the bottom of the funnel is the candidate list handed to the **scoring phase**. If nothing reaches the bottom, the pod stays `Pending`.

### Phase 2: scoring plugins

**Scenario.** Three nodes pass filtering, yet a team notices the scheduler keeps putting their pods on the same node, even though another node is nearly empty. One day it places a critical API pod on a node that is already 90% full and the API slows down. Why did the scheduler prefer that node?

![Phase 2: scoring plugins](diagrams/sections/d01-3.svg)

**What the picture shows.** Once filtering has narrowed the list, the **scoring phase** (also called the ranking phase) picks the best of the capable nodes. Every remaining node runs through several priority functions, gets a score from each, and the highest total wins.

- **LeastAllocated (the "spreader"):** the default strategy of the NodeResourcesFit plugin. It compares requested CPU and memory to capacity and gives emptier nodes higher scores, which spreads pods out and helps availability.
- **BalancedResourceAllocation (the "balancer"):** keeps CPU and memory use even on a node. A node with lots of CPU but almost no free RAM scores lower for a memory-heavy pod, so nodes don't become lopsided.
- **ImageLocality (the "speedster"):** scores nodes higher when the pod's images (say a 2 GB Java image) are already on local disk, so the pod skips the pull and starts faster. Bigger cached images count more. An image cached on only a few nodes is scaled *down*, so that one node with a popular image doesn't attract every new pod (the diagram's wording, "images on many nodes count less", has this backwards).
- **Node / inter-pod affinity (the "socialite"):** handles your *preferred* (soft) rules, such as "I'd like to run near my database". Nodes in the same rack or zone as the database score higher. It's the main score you steer from the pod spec (along with soft topology spread and `PreferNoSchedule` taints); the others are computed by Kubernetes.
- **The default-weights trap:** most of these plugins have the same small weight (1 or 2). In a large cluster that can lead to poor choices, and this is the scenario: the full node won because it already had the image cached, and the CPU contention showed up later.

**Going deeper.**

- For GPU training I switch NodeResourcesFit to **MostAllocated** (bin-packing) and give `nvidia.com/gpu` a higher weight than CPU and memory.
- Packing leaves whole nodes empty, so the autoscaler can remove them.
- That matters when a GPU node costs tens of dollars an hour.
- The catch is a bigger blast radius per node, so I offer it as a separate scheduler profile and let critical services keep spreading.

### Score × weight

**Scenario.** In a design review, a new engineer asks how the scheduler turns "points" into a decision. "If one plugin likes node A and another likes node B, who wins?" You want to explain it on a whiteboard in two minutes.

![Score × weight](diagrams/sections/d01-4.svg)

**What the picture shows.** Every node's final score is built from two ingredients per plugin.

- **The priority (score, or "grade"):** a measure of *quality*, from 0 to 100. For example, "how empty is this node?" or "how many matching pods are already here?"
- **The weight (the "multiplier"):** a measure of *importance*, set per plugin.
- **Adding it up:** for each plugin, score × weight gives that plugin's points, and the node's points from all plugins are added together. The highest total wins.
- **Example, LeastAllocated:** a node with 10 cores and 2 cores requested is 80% free, so the plugin gives it a score of 80. With the default weight of 1, that's 80 × 1 = **80 points**.
- **Example, ImageLocality:** the score grows with the total size of the images already cached, scaled down when only a few nodes have them. A big image (around 1 GB or more) cached scores close to 100; nothing cached, or only tiny images, scores 0.

### When affinity loses to ImageLocality

**Scenario.** A team adds a *preferred* pod affinity so their API runs next to its cache. Most of the time it works, but some pods land on a different node: one that happens to have the API's image already pulled. The team asks why their affinity rule "lost" to an image cache.

![When affinity loses to ImageLocality](diagrams/sections/d01-5.svg)

**What the picture shows.** A worked conflict between two plugins, and two ways to fix it.

- **The setup:** ImageLocality prefers **Node 1** (the image is cached there) and PodAffinity prefers **Node 2** (the cache pod runs there). The scheduler scores each node from 0 to 100 per plugin, multiplies by the plugin's weight, and adds them up.
- **Default weights:** most plugins have weight 1, affinity and topology spread have 2, and taint toleration has 3.
- **The conflict:** ImageLocality (weight 1) gives Node 1 a score of 90, which is **90 points**. InterPodAffinity (weight 2) gives Node 2 a weak 40, which is **80 points**. Node 1 wins, even though you asked for affinity.
- **Choice A, the hard rule (filtering):** change `preferredDuringSchedulingIgnoredDuringExecution` to `requiredDuringSchedulingIgnoredDuringExecution`. The rule moves from the scoring phase to the filtering phase, so Node 1 is never even considered.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 3
  selector:
    matchLabels: {app: api}
  template:
    metadata:
      labels: {app: api}
    spec:
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:   # now a filter, not a score #!
          - labelSelector:
              matchLabels: {app: cache} #!
            topologyKey: kubernetes.io/hostname #!
      containers:
      - name: api
        image: my-company/api:1.4
```

- **Choice B, scheduler configuration (advanced):** write a `KubeSchedulerConfiguration` profile with different weights, for example InterPodAffinity 100 and ImageLocality 1, so even a weak affinity match beats an image cache. On a managed control plane like EKS you can't change the default scheduler, so you run this as a second scheduler and point pods at it with `schedulerName`.

```yaml
# config file passed to a kube-scheduler you run yourself (--config)
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
- schedulerName: affinity-first-scheduler #!
  plugins:
    score:
      enabled:
      - name: InterPodAffinity #!
        weight: 100 #!
      - name: ImageLocality #!
        weight: 1 #!
```

### A worked example

**Scenario.** A developer writes a plain Deployment with no affinity rules at all and asks: "If I didn't set any weights, how does the scheduler decide anything?" They also tried adding a preferred rule without a weight, and `kubectl apply` rejected the YAML.

![A worked example](diagrams/sections/d01-6.svg)

**What the picture shows.** The defaults that are always in play, even when your YAML says nothing.

- **ImageLocality score:** grows with the total size of the cached images, scaled down when only a few nodes have the image. A big image cached is close to 100; nothing cached is 0.
- **Preferred rules need a weight:** the YAML is fine, but the API server rejects a preferred affinity term without a `weight` (it must be 1–100), which is why the developer's apply failed.
- **Built-in plugins still score every node:** the plugins you never see in your YAML (like LeastAllocated or ImageLocality) run anyway, with these default weights:
  - NodeResourcesFit (LeastAllocated): **1**
  - BalancedAllocation: **1**
  - ImageLocality: **1**
  - InterPodAffinity / NodeAffinity / TopologySpread: **2**
  - TaintToleration: **3**
- **What this means:** a simple pod still gets a reasoned placement, driven mostly by spreading, balance and image caching.

### The weight field in your YAML

**Scenario.** A platform team reviews a Helm chart that has both `required` and `preferred` affinity blocks, with weights of 1 and 100 scattered around. Nobody is sure what the numbers actually do, or why the `required` block has no weight at all.

![The weight field in your YAML](diagrams/sections/d01-7.svg)

**What the picture shows.** How the `weight` field in your YAML plugs into the scheduler.

- **Required (hard rule):** there is no `weight` field for `requiredDuringSchedulingIgnoredDuringExecution`, because "required" is a filter, not a score. A node either passes or is removed.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: report-builder
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:   # no weight: pass or fail #!
        nodeSelectorTerms:
        - matchExpressions:
          - key: node.kubernetes.io/instance-type
            operator: In
            values: ["m5.2xlarge"]
  containers:
  - name: builder
    image: my-company/report-builder:3.1
```

- **Weight tunes scoring from the outside:** whenever you see a `weight` in Kubernetes YAML, you are adjusting the scoring phase (never the filtering phase). You're telling the scheduler how much your rule matters compared to its built-in rules.
- **How it's applied:** for each node, the scheduler adds up the weights (1 to 100) of the preferred terms that node matches. Those sums are then normalized to 0–100 across the candidate nodes and multiplied by the plugin's own weight (2 for NodeAffinity and InterPodAffinity).
- **Preferred + high weight (a strong suggestion):** weight 100 says "this term matters much more than my other preferred terms". Because of the normalization, a matched preference is worth at most about 200 points in total, so it usually wins but can still be outvoted by several other plugins together. If it must always win, make it `required`.
- **Preferred + low weight (a nice-to-have):** weight 1 says "this would be nice, but it matters little compared with my other preferences". The weights only rank your preferred terms against each other.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: report-builder
spec:
  affinity:
    nodeAffinity:
      preferredDuringSchedulingIgnoredDuringExecution: #!
      - weight: 100        # "I really want this" #!
        preference:
          matchExpressions:
          - key: topology.kubernetes.io/zone
            operator: In
            values: ["us-east-1a"]
      - weight: 1          # "nice to have" #!
        preference:
          matchExpressions:
          - key: disktype
            operator: In
            values: ["ssd"]
  containers:
  - name: builder
    image: my-company/report-builder:3.1
```

### Scheduler score vs. OOM score

**Scenario.** A node runs out of memory and Kubernetes kills the team's most important pod. The team is surprised: "The scheduler gave that node a great score when it placed the pod, so why was our pod the one that died?" They are mixing up two different scores.

![Scheduler score vs. OOM score](diagrams/sections/d01-8.svg)

**What the picture shows.** Two "scores" that have nothing to do with each other.

- **Don't mix them up:** the *scheduling* score is about choosing a node for a pod. The *QoS / OOM* score is about choosing which pod to kill (covered in [Memory: who gets killed](#d03-6)).
- **The scheduler's choice:** at placement time the scheduler might see a node is 90% full and give it a low score, because it wants to spread the load. Once the pod is placed, that score is forgotten.
- **The kubelet's reality:** under memory pressure nobody looks at the scheduler's grade. The kubelet first picks pods that use more memory than they *requested*, then ranks them by priority, then by how far over their request they are. If the kernel's OOM killer fires first, it uses `oom_score`, which is memory usage plus a QoS-based `oom_score_adj`. So an "important" pod that uses more than it requested can still be the one that's killed.

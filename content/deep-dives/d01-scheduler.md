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
- **Insufficient resources (the fit check):** this is the answer to the scenario. The scheduler counts what pods **request**, not what they actually **use**, while the dashboard shows real usage. So a node can be 35% busy and still be fully booked on paper. How the check works: the scheduler takes the node's **allocatable** CPU and memory (its capacity minus what's reserved for the kubelet, the operating system and the eviction buffer), subtracts the *requests* of the pods already there, and checks that what's left is at least the new pod's request. This is a yes/no feasibility check, not bin-packing. "Not bin-packing" means this step only checks whether the pod fits on a node; it doesn't decide which of the fitting nodes is best.
  - **Worked example, one node:** 4 CPU cores of capacity, about **3.9 allocatable**. Three pods already run there: pod A requests 1.5 cores and uses 0.3, pod B requests 1.5 and uses 0.5, pod C requests 0.8 and uses 0.6.
  - **Reserved vs. used:** together they request **3.8 cores** but use only **1.4**, about 35% of the node. The dashboard shows the 35%.
  - **The new pod:** it requests 0.5 cores. The scheduler computes 3.9 − 3.8 = **0.1 cores left**. That's less than 0.5, so the node fails the filter, even though about 2.5 cores are sitting idle.
  - **At a glance:** the same numbers side by side. <table class="mini"><thead><tr><th>CPU cores</th><th>Scheduler's view (requests)</th><th>Dashboard's view (real usage)</th></tr></thead><tbody><tr><td>Pod A</td><td>1.5</td><td>0.3</td></tr><tr><td>Pod B</td><td>1.5</td><td>0.5</td></tr><tr><td>Pod C</td><td>0.8</td><td>0.6</td></tr><tr class="sum"><td>Total on the node</td><td>3.8 of 3.9 allocatable</td><td>1.4 (about 35%)</td></tr><tr class="sum"><td>Room left</td><td>0.1</td><td>about 2.5</td></tr><tr class="verdict"><td>New pod needs 0.5</td><td class="no">✗ doesn't fit: node filtered out</td><td class="yes">✓ would easily fit</td></tr></tbody></table>
  - **All 12 nodes look like this:** the result is `0/12 nodes are available: 12 Insufficient cpu`, which is exactly the scenario.
  - **Why it works this way:** a request is a promise. If pod A suddenly needs its full 1.5 cores, it must be able to get them, so the scheduler can't hand that reserved space to another pod just because it's idle right now.
  - **The usual fix:** right-size the requests to match real usage (tools like Goldilocks or the Vertical Pod Autoscaler help) instead of adding nodes.
  - **Which part of the scheduler runs the fit check:** a plugin called **NodeResourcesFit**, one of the scheduler's internal modules (<mark class="kw-act">filtering</mark> and scoring are made of many of them). It's built into kube-scheduler and on by default, so there's nothing to install or enable.
  - **What you write vs. what's built in:** your pod only supplies the `requests`; you never name the plugin. The plugin's own settings live in the scheduler's configuration. [The YAML just below this list](#fit-check-yaml) shows both: first your pod, then the scheduler's configuration with its default values.
  - **You normally don't write that configuration:** the `KubeSchedulerConfiguration` in the second half of the YAML is only there to make the hidden defaults visible; they already apply without it. You'd write one only to change them, and on managed control planes like EKS you can't change the default scheduler at all.
  - **Passing is a plain yes:** a nearly full node with just enough room and an almost empty node pass equally. Choosing between them happens later, in scoring, where the default (LeastAllocated) spreads pods and bin-packing (MostAllocated) must be turned on deliberately (see [Phase 2: scoring plugins](#d01-3)).

<div id="fit-check-yaml" class="anchor"></div>

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

> **Note: when would you actually write a `KubeSchedulerConfiguration`?**
>
> Only when you want to *change* the scheduler's built-in settings. Two things to know first:
>
> - **It isn't a Kubernetes object.** It has `apiVersion` and `kind`, but you don't `kubectl apply` it (the API would reject it). It's a config file the kube-scheduler program reads at startup, passed with `--config`.
> - **Where you can use it depends on who runs the scheduler:**
>   - **Self-managed clusters** (for example kubeadm): point the scheduler's static pod at your own file, for example to switch to MostAllocated bin-packing or change plugin weights.
>   - **Managed clusters** (EKS, GKE, AKS): the provider runs the default scheduler, so you can't change its file. Instead, run a second scheduler as a normal Deployment with its own `KubeSchedulerConfiguration` from a ConfigMap, and point specific pods at it with `schedulerName`. Volcano and KAI work the same way, as replacement schedulers. A complete, ready-to-apply example is in [Appendix A: Running a second scheduler](#appendix-second-scheduler). For the full list of built-in plugins, and what changes once a second scheduler shares the cluster, see [Built-in plugins, and what a second scheduler changes](#d01-9) in the Kubernetes Multi-Scheduler deep dive.

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

- **Taints and tolerations (the "keep out" sign):** a taint lets a node repel pods; only pods with a matching toleration may stay in the list. The taint is set on the **node** and the toleration on the **pod**; the YAML below shows both.

```yaml
# 1. The taint lives on the NODE. You add it with a command:
#      kubectl taint nodes gpu-node-1 dedicated=gpu:NoSchedule #!
#    (or in the node group / Karpenter NodePool config). The node then looks like this:
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-1
spec:
  taints: #!
  - key: dedicated #!
    value: gpu #!
    effect: NoSchedule        # pods without a matching toleration are filtered out #!
---
# 2. The toleration lives on the POD that is allowed in
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

- **Affinity and anti-affinity violations:** A <mark class="kw">required</mark> node affinity ("must run in us-east-1a") or pod anti-affinity ("never on a node that already runs a copy of me") removes nodes that break the rule.

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

> **Note: can one pod use taints, tolerations, affinity and anti-affinity together? Does the order matter?**
>
> Yes, one pod can use all of them at once, and no, the order doesn't matter.
>
> - **They're separate fields:** `tolerations`, `nodeSelector`, `affinity.nodeAffinity`, `affinity.podAffinity`, `affinity.podAntiAffinity` and `topologySpreadConstraints` are all named fields of the pod spec, so you can write them in any order.
> - **Every hard rule must pass:** the scheduler runs its filters in its own fixed order, and a node survives only if it passes *all* of them. That's a logical AND, which gives the same answer in any order.
> - **Inside node affinity:** several `nodeSelectorTerms` are combined with **OR** (matching any one term is enough); several `matchExpressions` inside one term are combined with **AND** (the node must match all of them).
>
>     ```yaml
>     apiVersion: v1
>     kind: Pod
>     metadata:
>       name: report-builder
>     spec:
>       affinity:
>         nodeAffinity:
>           requiredDuringSchedulingIgnoredDuringExecution:
>             nodeSelectorTerms:          # term 1 OR term 2
>             - matchExpressions:         # term 1: zone a AND an SSD  #!
>               - key: topology.kubernetes.io/zone
>                 operator: In
>                 values: ["us-east-1a"]
>               - key: disktype
>                 operator: In
>                 values: ["ssd"]
>             - matchExpressions:         # term 2: OR any m6i node  #!
>               - key: node.kubernetes.io/instance-type
>                 operator: In
>                 values: ["m6i.2xlarge", "m6i.4xlarge"]
>       containers:
>       - name: builder
>         image: my-company/report-builder:3.1
>     ```
> - **Preferred (soft) rules are wishes with points:** each node earns the `weight` of every preferred rule it matches, and the node with the most points is favoured. The order you list the rules in doesn't make one more important. In the example below, a node in zone a earns 90 points, an SSD node earns 10, a node with both earns 100 and a node with neither earns 0, and swapping the two rules in the YAML gives exactly the same result. (Those points then become NodeAffinity's score, which is added to the other plugins' scores.)
>
>     ```yaml
>     apiVersion: v1
>     kind: Pod
>     metadata:
>       name: report-builder
>     spec:
>       affinity:
>         nodeAffinity:
>           preferredDuringSchedulingIgnoredDuringExecution:
>           - weight: 10                  # listed first, but counts little  #!
>             preference:
>               matchExpressions:
>               - key: disktype
>                 operator: In
>                 values: ["ssd"]
>           - weight: 90                  # listed second, but counts most  #!
>             preference:
>               matchExpressions:
>               - key: topology.kubernetes.io/zone
>                 operator: In
>                 values: ["us-east-1a"]
>       containers:
>       - name: builder
>         image: my-company/report-builder:3.1
>     ```
>
>     **What if these were *required* instead?** The story changes. Required rules are pass/fail <mark class="kw-act">filters</mark>, not <mark class="kw-act">scores</mark>: there's no `weight` or `preference`, a node either passes or is removed, and if no node passes the pod stays `Pending` instead of running somewhere less ideal.
>
>     **One term or two: AND vs. OR.** A required rule lists its conditions under `nodeSelectorTerms`, and how you group them decides the logic. Conditions inside the *same* term must all match (AND); separate terms are alternatives, and matching any one is enough (OR). So you have to decide whether you mean "SSD **and** zone a" or "SSD **or** zone a":
>
>     ```yaml
>     # Both conditions in ONE term → AND: an SSD node that is also in us-east-1a
>     apiVersion: v1
>     kind: Pod
>     metadata:
>       name: report-builder
>     spec:
>       affinity:
>         nodeAffinity:
>           requiredDuringSchedulingIgnoredDuringExecution:   # no weights allowed  #!
>             nodeSelectorTerms:
>             - matchExpressions:          # one term, two conditions = AND  #!
>               - key: disktype
>                 operator: In
>                 values: ["ssd"]
>               - key: topology.kubernetes.io/zone
>                 operator: In
>                 values: ["us-east-1a"]
>       containers:
>       - name: builder
>         image: my-company/report-builder:3.1
>     ---
>     # Each condition in its OWN term → OR: an SSD node anywhere, or any node in us-east-1a
>     apiVersion: v1
>     kind: Pod
>     metadata:
>       name: report-builder
>     spec:
>       affinity:
>         nodeAffinity:
>           requiredDuringSchedulingIgnoredDuringExecution:
>             nodeSelectorTerms:
>             - matchExpressions:          # term 1  #!
>               - key: disktype
>                 operator: In
>                 values: ["ssd"]
>             - matchExpressions:          # term 2 (OR)  #!
>               - key: topology.kubernetes.io/zone
>                 operator: In
>                 values: ["us-east-1a"]
>       containers:
>       - name: builder
>         image: my-company/report-builder:3.1
>     ```
>
>     How the three versions treat each kind of node: <table class="mini"><thead><tr><th>Node</th><th>Preferred (10 / 90)</th><th>Required, AND</th><th>Required, OR</th></tr></thead><tbody><tr><td>SSD, in zone a</td><td>best choice</td><td class="yes">✓ allowed</td><td class="yes">✓ allowed</td></tr><tr><td>Zone a, no SSD</td><td>strong choice (90)</td><td class="no">✗ removed</td><td class="yes">✓ allowed</td></tr><tr><td>SSD, other zone</td><td>weak choice (10)</td><td class="no">✗ removed</td><td class="yes">✓ allowed</td></tr><tr><td>Neither</td><td>allowed, lowest score</td><td class="no">✗ removed</td><td class="no">✗ removed</td></tr><tr class="sum"><td>No matching node at all</td><td>pod runs anyway</td><td>pod stays Pending</td><td>pod stays Pending</td></tr></tbody></table>
>
>     Order still doesn't matter in any of them, and `IgnoredDuringExecution` still means a running pod isn't moved if labels change later. A common pattern is to mix them: a required rule for what's non-negotiable (say, the zone where the data lives) plus a preferred rule for nice-to-haves (say, SSD with weight 50).
>
> - **Why you usually want them together:** a toleration only *allows* a pod onto a tainted node; it doesn't *send* it there, so the pod could still land on any untainted node. Dedicated nodes therefore use three pieces: a **taint** on the nodes (keeps other pods off), a **toleration** on the pod (lets it in), and **node affinity** or `nodeSelector` on the pod (sends it there). Pod anti-affinity is often added to spread replicas across those nodes.
>
>     ```yaml
>     # Toleration only: ALLOWED onto GPU nodes, but nothing sends it there.
>     # The scheduler may still pick any ordinary node.
>     apiVersion: v1
>     kind: Pod
>     metadata:
>       name: trainer-loose
>     spec:
>       tolerations:                       # lets it in  #!
>       - key: dedicated
>         value: gpu
>         effect: NoSchedule
>       containers:
>       - name: trainer
>         image: my-company/trainer:2.0
>     ---
>     # Toleration + nodeSelector: allowed in AND sent there.
>     apiVersion: v1
>     kind: Pod
>     metadata:
>       name: trainer-pinned
>     spec:
>       tolerations:                       # lets it in  #!
>       - key: dedicated
>         value: gpu
>         effect: NoSchedule
>       nodeSelector:                      # sends it there  #!
>         node-type: gpu                   #!
>       containers:
>       - name: trainer
>         image: my-company/trainer:2.0
>     ```
>
>     The full version, with node affinity and pod anti-affinity to spread replicas, is the ["dedicated GPU nodes" example](#dedicated-gpu-yaml) right below this note.
>
> **The long affinity names, decoded.** Each name has two halves: *when scheduling* and *while running*.
>
> - **`requiredDuringScheduling…`:** a **<mark class="kw">hard rule</mark>**, checked in the **<mark class="kw-act">filter phase</mark>**. No matching node means the pod stays `Pending`.
> - **`preferredDuringScheduling…`:** a **<mark class="kw">soft rule</mark>**, checked in the **<mark class="kw-act">scoring phase</mark>** with a `weight` from 1 to 100. The pod still runs if nothing matches.
> - **`…IgnoredDuringExecution`:** the rule is checked only when the pod is placed. If a node's labels, or the other pods around it, change later, the running pod is **not** moved.
> - **`requiredDuringSchedulingRequiredDuringExecution`:** described in the Kubernetes docs as a possible future variant that would evict running pods when the rule stops matching. It has never been implemented, so **affinity rules are never re-checked once a pod is running**.
> - **Both versions (`required…` and `preferred…`) exist for all three kinds of affinity:** ("both" in the table means you can write either version)
>
>     <table class="mini"><thead><tr><th>Kind</th><th>What it controls</th><th>Versions</th><th>Needs <code>topologyKey</code>?</th></tr></thead><tbody><tr><td><code>nodeAffinity</code></td><td>which <b>nodes</b> to run on, by node label</td><td>both</td><td>no</td></tr><tr><td><code>podAffinity</code></td><td>run <b>near</b> certain other pods</td><td>both</td><td>yes</td></tr><tr><td><code>podAntiAffinity</code></td><td>run <b>away from</b> certain other pods</td><td>both</td><td>yes</td></tr></tbody></table>
>
>     **What `topologyKey` does.** "Run near" or "run away from" another pod raises a question: how close is near? On the same node? In the same zone? `topologyKey` answers it. You give it the name of a node label, and the scheduler treats two nodes as the *same place* when they have the same value for that label.
>
>     **Example:** a pod with anti-affinity "stay away from `app: api` pods" is being placed, and an `api` pod already runs on node A.
>
>     <table class="mini"><thead><tr><th>Node</th><th>hostname label</th><th>zone label</th><th>topologyKey: hostname</th><th>topologyKey: zone</th></tr></thead><tbody><tr><td>A (already runs an <code>api</code> pod)</td><td><code>node-a</code></td><td><code>us-east-1a</code></td><td>together</td><td>together</td></tr><tr><td>B</td><td><code>node-b</code></td><td><code>us-east-1a</code></td><td class="yes">apart: allowed</td><td class="no">together: avoided</td></tr><tr><td>C</td><td><code>node-c</code></td><td><code>us-east-1b</code></td><td class="yes">apart: allowed</td><td class="yes">apart: allowed</td></tr></tbody></table>
>
>     **With `topologyKey: kubernetes.io/hostname`:** every node has its own value, so only node A counts as "the same place". Node B is fine, even though it's in the same zone.
>
>     **With `topologyKey: topology.kubernetes.io/zone`:** nodes A and B both say `us-east-1a`, so they count as the same place. The pod avoids both and goes to node C in another zone.
>
>     So the same anti-affinity rule spreads pods across **nodes** or across **zones**, depending only on which label you name.
>
> - **If you need "move the pod when things change", use a different tool.** Affinity can't do it, but two other features can:
>   - **Taints with `effect: NoExecute`:** taints are a separate feature, set on the *node*. Their `effect` decides how strict they are: `NoSchedule` (hard rule for new pods), `PreferNoSchedule` (soft, scoring only) and `NoExecute`, which also evicts pods that are *already running* there without a matching toleration (`tolerationSeconds` lets a pod stay for a set time first). So the taint's `NoExecute` effect behaves like the "during execution" half that affinity never got.
>
>         ```yaml
>         # The NODE carries the taints (here one of each effect, to compare them)
>         apiVersion: v1
>         kind: Node
>         metadata:
>           name: gpu-node-1
>         spec:
>           taints:
>           - key: dedicated
>             value: gpu
>             effect: NoSchedule           # new pods without a toleration: not scheduled here  #!
>           - key: spot
>             value: "true"
>             effect: PreferNoSchedule     # new pods: avoided if possible (scoring only)  #!
>           - key: maintenance
>             value: "true"
>             effect: NoExecute            # running pods without a toleration: evicted NOW  #!
>         ---
>         # The POD tolerates all three; for NoExecute it may stay 10 minutes, then is evicted
>         apiVersion: v1
>         kind: Pod
>         metadata:
>           name: inference
>         spec:
>           tolerations:
>           - key: dedicated
>             operator: Equal
>             value: gpu
>             effect: NoSchedule
>           - key: spot
>             operator: Exists
>             effect: PreferNoSchedule
>           - key: maintenance
>             operator: Exists
>             effect: NoExecute
>             tolerationSeconds: 600       # stay up to 10 min after the taint appears, then leave  #!
>           containers:
>           - name: server
>             image: vllm/vllm-openai:latest
>         ```
>
>         Add the `NoExecute` taint to a running node with `kubectl taint nodes gpu-node-1 maintenance=true:NoExecute`: pods *without* that toleration are evicted at once, and this pod is evicted after 600 seconds. Without `tolerationSeconds`, a pod with the toleration would stay forever.
>
>         <table class="mini"><thead><tr><th>The running pod has…</th><th>When the <code>NoExecute</code> taint is added</th></tr></thead><tbody><tr><td>no matching toleration</td><td class="no">evicted right away</td></tr><tr><td>a matching toleration with <code>tolerationSeconds: 600</code></td><td>stays 600 seconds, then evicted (unless the taint is removed first)</td></tr><tr><td>a matching toleration without <code>tolerationSeconds</code></td><td class="yes">never evicted by this taint</td></tr></tbody></table>
>
>         **Note: doesn't that defeat `NoExecute`?** Yes, if the pod's author wants it to. A toleration is an opt-out that whoever writes the pod controls: a toleration for `maintenance` with no `tolerationSeconds` means this taint never evicts that pod. Worse, a toleration with **no key** and `operator: Exists` matches **every** taint:
>
>         ```yaml
>         tolerations:
>         - operator: Exists      # no key, no effect: tolerates ALL taints, forever  #!
>         ```
>
>         This is by design: <mark class="kw">taints</mark> and tolerations are a *placement* tool, not a *security* tool, and some pods legitimately must stay put (node agents run as DaemonSets, such as the CNI, log shippers and the GPU device plugin, get indefinite tolerations from Kubernetes itself). If you need to enforce it, put the control where the pod's author can't change it:
>
>         **1. An admission policy that rejects such pods.** The built-in `ValidatingAdmissionPolicy` (stable since Kubernetes 1.30), or Kyverno or OPA Gatekeeper. This one rejects any pod outside `kube-system` that tolerates the `maintenance` taint, or every taint, without a time limit:
>
>         ```yaml
>         apiVersion: admissionregistration.k8s.io/v1
>         kind: ValidatingAdmissionPolicy
>         metadata:
>           name: noexecute-needs-time-limit
>         spec:
>           failurePolicy: Fail
>           matchConstraints:
>             resourceRules:
>             - apiGroups: [""]
>               apiVersions: ["v1"]
>               operations: ["CREATE", "UPDATE"]
>               resources: ["pods"]
>           validations:
>           - expression: >-                     #!
>               !has(object.spec.tolerations) ||
>               object.spec.tolerations.all(t,
>                 has(t.tolerationSeconds) ||
>                 !((!has(t.effect) || t.effect == 'NoExecute') &&
>                   (!has(t.key) || t.key == '' || t.key == 'maintenance')))
>             message: "Tolerations for the maintenance NoExecute taint (or for all taints) must set tolerationSeconds."
>         ---
>         apiVersion: admissionregistration.k8s.io/v1
>         kind: ValidatingAdmissionPolicyBinding
>         metadata:
>           name: noexecute-needs-time-limit
>         spec:
>           policyName: noexecute-needs-time-limit
>           validationActions: ["Deny"]          #!
>           matchResources:
>             namespaceSelector:
>               matchExpressions:
>               - key: kubernetes.io/metadata.name
>                 operator: NotIn
>                 values: ["kube-system"]        # node agents there legitimately tolerate forever  #!
>         ```
>
>         **2. Use `kubectl drain` for maintenance, not just the taint.** Drain cordons the node and evicts its pods through the eviction API whatever their tolerations (except DaemonSet pods), while still respecting PodDisruptionBudgets. In practice the taint *signals* that a node is going away, and drain actually empties it.
>
>         **3. RBAC and review:** limit who can create pods in sensitive namespaces, and check tolerations in CI before manifests reach the cluster.
>
>   - **The descheduler:** a separate add-on (a Kubernetes SIG project, installed with Helm) that runs every few minutes, usually as a CronJob. It looks for running pods that are now in the "wrong" place and **evicts** them, so their controller creates replacements and the normal scheduler places those again, this time following the rules. It never chooses nodes itself. Its `RemovePodsViolatingNodeAffinity` plugin catches pods whose node no longer has the labels their required node affinity asks for, and `RemovePodsViolatingInterPodAntiAffinity` catches pods that now sit next to pods they should avoid. Evictions respect PodDisruptionBudgets.
>
>         ```yaml
>         # Helm values: helm install descheduler descheduler/descheduler -n kube-system -f values.yaml
>         kind: CronJob
>         schedule: "*/10 * * * *"                 # check every 10 minutes  #!
>         deschedulerPolicy:
>           profiles:
>           - name: fix-affinity
>             pluginConfig:
>             - name: DefaultEvictor
>               args:
>                 nodeFit: true                    # only evict if another node could take the pod  #!
>             - name: RemovePodsViolatingNodeAffinity
>               args:
>                 nodeAffinityType:
>                 - requiredDuringSchedulingIgnoredDuringExecution
>             - name: RemovePodsViolatingInterPodAntiAffinity
>             plugins:
>               deschedule:
>                 enabled:
>                 - RemovePodsViolatingNodeAffinity          #!
>                 - RemovePodsViolatingInterPodAntiAffinity  #!
>         ```
> - **The full "dedicated GPU nodes" example is in the YAML right below this note.**

<div id="dedicated-gpu-yaml" class="anchor"></div>

```yaml
# 1. The GPU nodes: a label (to be found) and a taint (to keep others out)
#    Usually set in the node group or Karpenter NodePool, not by hand.
apiVersion: v1
kind: Node
metadata:
  name: gpu-node-1
  labels:
    node-type: gpu #!
spec:
  taints: #!
  - key: dedicated #!
    value: gpu
    effect: NoSchedule
---
# 2. The GPU workload: toleration + node affinity + pod anti-affinity
apiVersion: apps/v1
kind: Deployment
metadata:
  name: inference
spec:
  replicas: 3
  selector:
    matchLabels: {app: inference}
  template:
    metadata:
      labels: {app: inference}
    spec:
      tolerations:                     # allowed onto the tainted GPU nodes #!
      - key: dedicated #!
        operator: Equal
        value: gpu
        effect: NoSchedule
      - key: node.kubernetes.io/unreachable
        operator: Exists
        effect: NoExecute #!
        tolerationSeconds: 60          # stay 60 s on an unreachable node, then get evicted #!
      affinity:
        nodeAffinity:                  # sent to the GPU nodes (hard rule) #!
          requiredDuringSchedulingIgnoredDuringExecution: #!
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-type
                operator: In
                values: ["gpu"] #!
        podAntiAffinity:               # spread replicas across nodes (soft rule) #!
          preferredDuringSchedulingIgnoredDuringExecution: #!
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchLabels: {app: inference}
              topologyKey: kubernetes.io/hostname
      containers:
      - name: server
        image: vllm/vllm-openai:latest
        resources:
          limits:
            nvidia.com/gpu: 1
```

> **Why does this example have two tolerations?** `tolerations` is a list, and each entry tolerates one kind of taint. A pod can tolerate as many as it needs; here the two entries do different jobs.
>
> - **Entry 1, our own GPU taint (`dedicated=gpu`, `NoSchedule`):** it matches the taint we put on the GPU nodes, so it decides *where the pod may go*. `operator: Equal` means the key and the value must both match.
> - **Entry 2, a built-in taint (`node.kubernetes.io/unreachable`, `NoExecute`):** nobody adds this one by hand. Kubernetes adds it automatically when a node stops talking to the control plane (a crash, a lost network). A `NoExecute` taint doesn't only block new pods; it evicts pods already running there. `operator: Exists` matches any taint with that key, whatever its value.
> - **What `tolerationSeconds: 60` changes:** Kubernetes already gives every pod this toleration automatically, with a default of 300 seconds. Writing it yourself only changes the timing: the pod stays on an unreachable node for 60 seconds, then is evicted and recreated on a healthy node. For a GPU inference service, 5 minutes on a dead node is a long outage. The trade-off is that a short network blip could move the pod unnecessarily.
> - **In short:** entry 1 controls *where the pod may go*; entry 2 controls *how quickly it leaves* when its node fails.

- **Topology spread constraints (keep replicas balanced):** A `topologySpreadConstraints` rule spreads a group of pods evenly across places, such as zones or nodes. It isn't a box in the picture, but with `whenUnsatisfiable: DoNotSchedule` it's another filter: a node is removed if placing the pod there would make the busiest place exceed the emptiest by more than `maxSkew`.
  - **What counts as a "place":** the `topologyKey`, the same field used in pod affinity. `topology.kubernetes.io/zone` means zones, `kubernetes.io/hostname` means nodes.
  - **Hard or soft:** `DoNotSchedule` filters nodes out; `ScheduleAnyway` only lowers their score.
  - **How it differs from pod anti-affinity:** anti-affinity says "not where a pod like me already runs", so a hard rule allows only one per place: 6 replicas in 3 zones means 3 stay `Pending`. A spread constraint allows several per place as long as they stay balanced, so all 6 run, 2 per zone.
  - **When to use which:** anti-affinity for "never two on one node" (say, one database replica per node); spread constraints for "balance across zones" (most web services). Even without either, the scheduler applies a soft default spread across nodes and zones.

```yaml
# 6 replicas spread evenly across 3 zones: 2 per zone.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 6
  selector:
    matchLabels: {app: web}
  template:
    metadata:
      labels: {app: web}
    spec:
      topologySpreadConstraints: #!
      - maxSkew: 1                                # zones may differ by at most 1 pod #!
        topologyKey: topology.kubernetes.io/zone  # a "place" is a zone #!
        whenUnsatisfiable: DoNotSchedule          # hard rule; ScheduleAnyway = soft #!
        labelSelector:
          matchLabels: {app: web}                 # which pods to count
      containers:
      - name: web
        image: nginx:1.27
```

> **Note: using pod anti-affinity and topology spread together**
>
> A common case is a service that must survive both a **node** failure and a **zone** outage, say 6 replicas of an API. Each rule covers one level:
>
> - **Pod anti-affinity (required, per node):** never two replicas on the same node, so losing one node takes down at most one replica.
> - **Topology spread (per zone):** replicas stay balanced across zones, so losing a zone takes down about a third of them, not half.
>
> ```yaml
> apiVersion: apps/v1
> kind: Deployment
> metadata:
>   name: api
> spec:
>   replicas: 6
>   selector:
>     matchLabels: {app: api}
>   template:
>     metadata:
>       labels: {app: api}
>     spec:
>       affinity:
>         podAntiAffinity:
>           requiredDuringSchedulingIgnoredDuringExecution: #!
>           - labelSelector:
>               matchLabels: {app: api}
>             topologyKey: kubernetes.io/hostname      # at most one per NODE #!
>       topologySpreadConstraints:
>       - maxSkew: 1
>         topologyKey: topology.kubernetes.io/zone     # balanced across ZONES #!
>         whenUnsatisfiable: DoNotSchedule #!
>         labelSelector:
>           matchLabels: {app: api}
>       containers:
>       - name: api
>         image: my-company/api:4.2
> ```
>
> **How they work together:** both are filters, and a node must pass **every** filter, so the scheduler keeps only nodes that have no `api` replica yet **and** sit in a zone that won't get too far ahead of the others. With 3 zones of 3 nodes each, the result is 2 replicas per zone, each on its own node.
>
> **Where they can collide:** say zone c has only one node. Anti-affinity lets zone c hold just one replica. The spread rule then caps the other zones at 2 each (at most 1 more than zone c), so only 5 replicas can run and the 6th stays `Pending`, even though zones a and b have empty nodes. The fixes are to add a node to zone c, set `whenUnsatisfiable: ScheduleAnyway` so the zone spread becomes a preference, or make the anti-affinity `preferred`.
>
> **Could topology spread do both jobs?** Yes: list two constraints, one with `topologyKey: kubernetes.io/hostname` and one with the zone key, each `maxSkew: 1`. The difference is that a per-node spread allows a second replica on a node once every node has one, while required anti-affinity never does.
>
> ```yaml
> # Topology spread doing both jobs: one constraint per node, one per zone.
> apiVersion: apps/v1
> kind: Deployment
> metadata:
>   name: api
> spec:
>   replicas: 6
>   selector:
>     matchLabels: {app: api}
>   template:
>     metadata:
>       labels: {app: api}
>     spec:
>       topologySpreadConstraints:                       # no podAntiAffinity needed
>       - maxSkew: 1
>         topologyKey: kubernetes.io/hostname            # balanced across NODES #!
>         whenUnsatisfiable: DoNotSchedule
>         labelSelector:
>           matchLabels: {app: api}
>       - maxSkew: 1
>         topologyKey: topology.kubernetes.io/zone       # balanced across ZONES #!
>         whenUnsatisfiable: DoNotSchedule
>         labelSelector:
>           matchLabels: {app: api}
>       containers:
>       - name: api
>         image: my-company/api:4.2
> ```
>
> With 9 nodes this gives the same result as before: one replica per node, 2 per zone. Scale to 12 replicas and the two versions differ: here replicas 10 to 12 can double up on nodes (as long as nodes and zones stay within 1 of each other), while the anti-affinity version leaves them `Pending` because every node already has one.

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

**Going deeper.**

- For GPUs, these same filters do the steering: node labels pick the GPU model, a taint keeps other pods off GPU nodes, and the GPU count is checked just like CPU and memory. How GPUs get advertised to the scheduler, how to steer pods to the right one, and Dynamic Resource Allocation are covered in the next deep dive, [Kubernetes and GPU](#d-gpu).

- Filtering also runs one pod at a time: a distributed job's workers are judged separately, which is the root of the [gang problem](#d02-2). Setting `parallelism` doesn't fix this; it only creates the pods together. "All 8 or none" needs something that knows the pods belong together:
  - a queue in front of the scheduler, such as **Kueue**, which admits the whole job only when there's room for all of it ([Volcano vs. Kueue](#d02-6));
  - a batch scheduler such as **Volcano**, **KAI** or **YuniKorn**, running as a second scheduler ([what a second scheduler changes](#d01-9) and [the landscape](#d02-7), both in [Kubernetes Multi-Scheduler](#d-multischeduler));
  - the **coscheduling plugin** (a PodGroup with `minMember`) or, from Kubernetes 1.35, the alpha **Workload API**, both covered in [gang scheduling](#d02-2).

```yaml
# 8 workers that must all run together, but nothing here says so.
# parallelism only lets the Job controller create 8 pods at once; it does NOT
# make them a gang. The default scheduler filters and places each pod on its
# own: with 6 free GPUs, 6 start (and sit idle) while 2 stay Pending.
apiVersion: batch/v1
kind: Job
metadata:
  name: distributed-train
spec:
  parallelism: 8 #!
  completions: 8
  completionMode: Indexed
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: worker
        image: my-company/trainer:2.0
        resources:
          limits:
            nvidia.com/gpu: 1 #!
```

### Phase 2: scoring plugins

**Scenario.** Three nodes pass filtering, yet a team notices the scheduler keeps putting their pods on the same node, even though another node is nearly empty. One day it places a critical API pod on a node that is already 90% full and the API slows down. Why did the scheduler prefer that node?

![Phase 2: scoring plugins](diagrams/sections/d01-3.svg)

**What the picture shows.** Once filtering has narrowed the list, the **scoring phase** (also called the ranking phase) picks the best of the capable nodes. Every remaining node runs through several priority functions, gets a score from each, and the highest total wins.

- **LeastAllocated (the "spreader"):** the default strategy of the NodeResourcesFit plugin. It compares requested CPU and memory to capacity and gives emptier nodes higher scores, which spreads pods out and helps availability.
- **BalancedResourceAllocation (the "balancer"):** keeps CPU and memory use even on a node. A node with lots of CPU but almost no free RAM scores lower for a memory-heavy pod, so nodes don't become lopsided.

> **Note: how BalancedResourceAllocation works, and how to tune it**
>
> - **Nothing to install or enable:** it's one of the scheduler's built-in scoring plugins and is on by default, so every cluster already uses it.
> - **What it looks at:** for each node, the share of CPU and the share of memory that would be **reserved** if the pod landed there. It uses the pods' *requests*, not real usage, like the fit check.
> - **How it scores:** the closer those two shares are to each other, the higher the score. A memory-heavy pod (`memory: 8Gi`, `cpu: 250m`) on a node that's already 90% booked on memory and 20% on CPU makes that node even more lopsided, so the node scores low; a node at 50% and 45% scores high.
> - **What your pods control:** only their requests. Accurate requests are what make this plugin useful.
> - **What a configuration can change:** its weight compared with the other scoring plugins, and which resources it balances (CPU and memory by default; you can add GPUs, for example). Both live in the scheduler's configuration, not in pod YAML:
>
> ```yaml
> apiVersion: kubescheduler.config.k8s.io/v1
> kind: KubeSchedulerConfiguration
> profiles:
> - schedulerName: default-scheduler
>   plugins:
>     score:
>       enabled:
>       - name: BalancedResourceAllocation
>         weight: 2                     # counts double vs. other scoring plugins (default 1) #!
>   pluginConfig:
>   - name: BalancedResourceAllocation
>     args:
>       resources:                      # which resources to keep in balance #!
>       - name: cpu
>         weight: 1
>       - name: memory
>         weight: 1
>       - name: nvidia.com/gpu          # e.g. also balance GPUs against CPU and memory #!
>         weight: 1
> ```
>
> - **On EKS:** you can't edit the default scheduler's configuration, so changing these needs a [second scheduler](#appendix-second-scheduler) that pods opt into with `schedulerName`.

- **ImageLocality (the "speedster"):** scores nodes higher when the pod's images (say a 2 GB Java image) are already on local disk, so the pod skips the pull and starts faster. Bigger cached images count more. The points are also scaled by how many nodes already have the image (**spread scaling**, explained in the note below).

> **Note: how ImageLocality scores, and why it uses spread scaling**
>
> - **What it's for:** if a node already has a pod's container image, the pod starts faster there because nothing has to be downloaded. So ImageLocality gives points to nodes that already hold the image, and bigger images earn more points because skipping their download saves more time.
> - **The catch it guards against:** say a new image exists on only 1 of 100 nodes. If that node got full points, every new pod using that image would pile onto that one node. The cluster ends up unbalanced, and the scheduler never spreads the image to other nodes.
> - **The fix, spread scaling:** the image's points are multiplied by the share of nodes that already have it:
>
> ```text
> points ≈ image size × (nodes that have the image ÷ all nodes)
> ```
>
> | The image is on… | Scaling factor | Points for a node that has it |
> |---|---|---|
> | 1 of 100 nodes | 0.01 | almost nothing |
> | 50 of 100 nodes | 0.5 | half |
> | 100 of 100 nodes | 1.0 | full |
>
> So an image cached on only a few nodes gives very little advantage, and the pod is placed mostly on other grounds. An image already present almost everywhere counts fully.

- **Node / inter-pod affinity (the "socialite"):** handles your *preferred* (soft) rules, such as "I'd like to run near my database". Nodes in the same rack or zone as the database score higher. It's the main score you steer from the pod spec (along with soft topology spread and `PreferNoSchedule` taints); the others are computed by Kubernetes.

```yaml
# Three soft preferences a pod (or a node) can express. None of them can
# block scheduling; they only add or remove scoring points.
apiVersion: v1
kind: Node
metadata:
  name: node-old-1
spec:
  taints:
  - key: hardware
    value: old
    effect: PreferNoSchedule         # 3. "avoid me if you can" (TaintToleration score) #!
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: reports-api
spec:
  replicas: 3
  selector:
    matchLabels: {app: reports-api}
  template:
    metadata:
      labels: {app: reports-api}
    spec:
      affinity:
        podAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:   # 1. soft: scored, never filtered #!
          - weight: 80                                      # how much this wish counts (1-100) #!
            podAffinityTerm:
              labelSelector:
                matchLabels: {app: postgres}                # "near my database"
              topologyKey: topology.kubernetes.io/zone      # "near" = same zone #!
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway                   # 2. soft spread: scored, not filtered #!
        labelSelector:
          matchLabels: {app: reports-api}
      containers:
      - name: api
        image: my-company/reports-api:1.4
```

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
  - BalancedResourceAllocation: **1**
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

---
id: d-multischeduler
title: "When one scheduler isn't enough"
short: "Kubernetes Multi-Scheduler"
---
The default scheduler places pods one at a time and knows nothing about jobs, teams or GPU topology. This deep dive covers the problems that pushed the ecosystem toward Kueue, Volcano, KAI and YuniKorn, and how to choose between them.

### Built-in plugins, and what a second scheduler changes

**Scenario.** After learning in the [scheduler deep dive](#d01-1) that the fit check is done by "a plugin", the team wants the full list: which plugins run by default, what each one does, which ones filter and which ones score, and with what weight. Then the architect asks the follow-up: if we add a second scheduler, do these plugins still apply, and what can go wrong when two schedulers share one cluster?

![Built-in plugins, and what a second scheduler changes](diagrams/sections/d01-9.svg)

**What the picture shows.** Every scheduling decision runs through the same stages, left to right, and each stage is made of plugins.

- **Pending pod:** its `spec.schedulerName` decides which of the three lanes handles it. Exactly one scheduler handles each pod.
- **The stages (column headers):**
  - **Queue sort:** decides which pending pod is considered next.
  - **Filter:** removes nodes that can't run the pod (Phase 1).
  - **PostFilter:** runs only when *no* node passed; this is where preemption happens.
  - **Score:** ranks the nodes that passed (Phase 2), each plugin's score multiplied by its weight.
  - **Reserve / Permit:** holds the chosen node's volumes and devices while the decision is finalized.
  - **Bind:** writes the chosen node into the pod (`spec.nodeName`).
- **Blue lane, `default-scheduler`:** the built-in kube-scheduler. On EKS it runs on the AWS-managed control plane, and every plugin shown is built in and on by default. The Score box lists the default weights (×1, ×2, ×3).
- **Amber lane, a second kube-scheduler with your own profile:** the same program and the same plugins, run as your own Deployment (the full example is in [Appendix A](#appendix-second-scheduler)). Only the highlighted Score box differs: InterPodAffinity weighted ×100, and NodeResourcesFit switched to MostAllocated (bin-packing).
- **Teal lane, a replacement scheduler (Volcano, KAI, YuniKorn):** a different program that works in sessions, with its own actions (steps such as allocate or reclaim) and plugins: queues and PodGroups for gang scheduling (Volcano's PodGroup is its own object, not Kubernetes' new one; see the note "Kubernetes' own gang scheduling, and where EKS stands"), queue-based reclaim instead of plain preemption, GPU and network topology scoring, and binding a whole job together. It reuses kube-scheduler's filter checks (through its predicates plugin), so taints and affinity still mean the same thing, but the scoring plugins and weights above don't apply to its pods.
- **Grey bar, shared by all three:** the same nodes and the same API server. The schedulers don't coordinate with each other.

**The built-in plugins** (all on by default; "–" means the plugin doesn't take part in that stage):

The colours match the "From pod spec to plugin" table: a row has the colour of the pod-spec field that triggers it. Rows with no colour have no pod-spec field. NodeUnschedulable reads the node's cordon, and DefaultBinder only writes the result.

<table class="plugmap by-plugin">
<thead><tr><th>Plugin</th><th>What it does</th><th>Filter</th><th>Score weight</th></tr></thead>
<tbody>
<tr class="g8"><td>PrioritySort</td><td>Orders the queue: higher-priority pods are scheduled first</td><td>–</td><td>–</td></tr>
<tr class="g12"><td>SchedulingGates</td><td>Holds back pods that still have <code>schedulingGates</code> set</td><td>–</td><td>–</td></tr>
<tr><td>NodeUnschedulable</td><td>Skips cordoned nodes (<code>spec.unschedulable</code>)</td><td>✓</td><td>–</td></tr>
<tr class="g6"><td>NodeName</td><td>Honours a pod that already names its node</td><td>✓</td><td>–</td></tr>
<tr class="g1"><td>NodeResourcesFit</td><td>CPU, memory, GPU and other resources: does it fit? Then LeastAllocated scoring</td><td>✓</td><td>1</td></tr>
<tr class="g1"><td>BalancedResourceAllocation</td><td>Prefers nodes where CPU and memory use stay balanced</td><td>–</td><td>1</td></tr>
<tr class="g11"><td>ImageLocality</td><td>Prefers nodes that already have the pod's images</td><td>–</td><td>1</td></tr>
<tr class="g2"><td>NodeAffinity</td><td><code>nodeSelector</code> and required node affinity (filter); preferred node affinity (score)</td><td>✓</td><td>2</td></tr>
<tr class="g3"><td>InterPodAffinity</td><td>Required pod affinity / anti-affinity (filter); preferred (score)</td><td>✓</td><td>2</td></tr>
<tr class="g4"><td>PodTopologySpread</td><td><code>DoNotSchedule</code> spread constraints (filter); <code>ScheduleAnyway</code> (score)</td><td>✓</td><td>2</td></tr>
<tr class="g5"><td>TaintToleration</td><td><code>NoSchedule</code> taints (filter); prefers fewer untolerated <code>PreferNoSchedule</code> taints (score)</td><td>✓</td><td>3</td></tr>
<tr class="g7"><td>NodePorts</td><td>Rejects nodes where the pod's <code>hostPort</code> is taken</td><td>✓</td><td>–</td></tr>
<tr class="g9"><td>VolumeRestrictions</td><td>Volume conflicts, such as a <code>ReadWriteOncePod</code> volume already in use</td><td>✓</td><td>–</td></tr>
<tr class="g9"><td>NodeVolumeLimits</td><td>The node's limit on attached volumes (for example EBS)</td><td>✓</td><td>–</td></tr>
<tr class="g9"><td>VolumeZone</td><td>The volume's zone must match the node's zone</td><td>✓</td><td>–</td></tr>
<tr class="g9"><td>VolumeBinding</td><td>PVCs can be bound or provisioned on this node; reserves them</td><td>✓</td><td>–</td></tr>
<tr class="g10"><td>DynamicResources</td><td>DRA device claims (GPUs and other devices) can be satisfied; reserves them</td><td>✓</td><td>–</td></tr>
<tr class="g8"><td>DefaultPreemption</td><td>PostFilter: evicts lower-priority pods when nothing fits</td><td>–</td><td>–</td></tr>
<tr><td>DefaultBinder</td><td>Bind: writes <code>spec.nodeName</code></td><td>–</td><td>–</td></tr>
</tbody>
</table>

**From pod spec to plugin:** you never write a plugin's name in a pod. You write a field, and the plugin that reads it does the work. The diagram and the table above use plugin names; this is how they map to what you write:

The colours match the rows of the built-in plugins table.

<table class="plugmap by-field">
<thead><tr><th>You write in the pod spec</th><th>The plugin that reads it</th></tr></thead>
<tbody>
<tr class="g1"><td><code>resources.requests</code></td><td>NodeResourcesFit, BalancedResourceAllocation</td></tr>
<tr class="g2"><td><code>nodeSelector</code>, <code>affinity.nodeAffinity</code></td><td>NodeAffinity</td></tr>
<tr class="g3"><td><code>affinity.podAffinity</code> / <code>podAntiAffinity</code></td><td>InterPodAffinity</td></tr>
<tr class="g4"><td><code>topologySpreadConstraints</code></td><td>PodTopologySpread</td></tr>
<tr class="g5"><td><code>tolerations</code></td><td>TaintToleration</td></tr>
<tr class="g6"><td><code>nodeName</code></td><td>NodeName</td></tr>
<tr class="g7"><td><code>ports[].hostPort</code></td><td>NodePorts</td></tr>
<tr class="g8"><td><code>priorityClassName</code></td><td>PrioritySort, DefaultPreemption</td></tr>
<tr class="g9"><td><code>volumes</code> (PVCs)</td><td>VolumeBinding, VolumeZone, VolumeRestrictions, NodeVolumeLimits</td></tr>
<tr class="g10"><td><code>resourceClaims</code> (DRA)</td><td>DynamicResources</td></tr>
<tr class="g11"><td>the image in <code>containers[].image</code></td><td>ImageLocality</td></tr>
<tr class="g12"><td><code>schedulingGates</code></td><td>SchedulingGates</td></tr>
</tbody>
</table>

**What a second scheduler changes:**

- **One pod, one scheduler:** a pod is handled only by the scheduler named in its `schedulerName`. The other schedulers' plugins never see it.
- **Same program, different profile (amber lane):** you can change weights, switch the scoring strategy, or disable plugins (for example turn off ImageLocality). One kube-scheduler process can even serve several profiles, each under its own `schedulerName`.
- **Different program (teal lane):** you get features the built-in plugins don't have, such as gang scheduling, queues and GPU topology. In return, its pods follow its rules, not the table above.
- **No coordination:** two schedulers can pick the same free space at the same moment, because each one only sees its own decisions in flight. The kubelet then rejects the pod that arrives second (`OutOfcpu` / `OutOfmemory`) and its controller creates a new one. The usual fix is to give each scheduler its own nodes, with taints and node labels, or to send all pods of one kind (for example all GPU pods) to a single scheduler.
- **Preemption crosses the boundary:** the default scheduler's DefaultPreemption can evict pods placed by your scheduler if they have lower priority, and the other way round. Use PriorityClasses consistently across both.
- **Autoscalers:** Cluster Autoscaler and Karpenter decide whether to add nodes by simulating the *default* scheduler's rules. Check whether yours acts on pods with a custom `schedulerName` at all, and remember that custom filters it doesn't know about can lead to nodes being added that your scheduler then won't use.

### Configuring a scheduler: stages, plugins and profiles

**Scenario.** The platform team wants pods packed onto fewer nodes so idle nodes can be removed, and a gang plugin for training jobs. They open a `KubeSchedulerConfiguration` and find `profiles`, `plugins`, `pluginConfig`, a dozen stage names and two different `weight` fields. Before changing anything, they need to know what each part controls.

![Configuring a scheduler: stages, plugins and profiles](diagrams/sections/d02-9.svg)

**What the picture shows.** The stages every pod passes through inside kube-scheduler, the plugins the default scheduler puts at each one, and the three parts of the configuration file that control them.

- **The stages (extension points):** kube-scheduler is a pipeline of fixed stages, numbered 1 to 10 in the picture. Each stage is filled with **plugins**, and filtering, scoring and binding are just the best-known stages.
- **preEnqueue and queueSort (1, 2):** decide whether a pod may enter the queue at all (SchedulingGates holds it back until its gates are removed) and in what order waiting pods are taken (PrioritySort: higher priority first).
- **preFilter and filter (3):** filtering. Each plugin removes nodes that break one rule: resources, ports, taints, affinity, topology spread, volumes, devices.
- **postFilter (4):** runs only when no node passed. DefaultPreemption looks for lower-priority pods to evict to make room.
- **preScore and score (5):** scoring. The only stage where plugins have a **weight**, which says how much each plugin counts in the total.
- **One plugin, several stages:** NodeResourcesFit filters ("does it fit?") and scores ("how full would the node be?"). NodeAffinity, TaintToleration, InterPodAffinity and PodTopologySpread do both too: their required or hard rules filter, their preferred or soft rules score.

> **Note: why NodeResourcesFit is at both filter and score**
>
> It answers two different questions about the same numbers: the node's allocatable CPU and memory minus the requests of the pods already there.
>
> | Stage | Question | Answer |
> |---|---|---|
> | **Filter** | Does the pod **fit** on this node at all? | Yes or no. If its requests exceed what's left, the node is removed ("Insufficient cpu"). |
> | **Score** | Of the nodes where it fits, **how good** is each one? | A number from 0 to 100, based on how full the node would be after placing the pod. |
>
> For example, a pod requests 1 CPU and three nodes each have 4 CPU allocatable:
>
> | Node | Already requested | Filter | Score with LeastAllocated (default) |
> |---|---|---|---|
> | A | 3.5 CPU | ✗ only 0.5 left | – |
> | B | 2 CPU | ✓ | lower (would be 75% full) |
> | C | 0.5 CPU | ✓ | higher (would be 37.5% full) |
>
> - **The filter removes A.** Scoring then prefers C, because LeastAllocated favors emptier nodes and so spreads pods out.
> - **Switch the strategy to MostAllocated** and the same plugin prefers B instead: it fills nodes up (bin-packing) so empty nodes can be removed. That's why the fit check itself is "not bin-packing": the filter only says yes or no, and packing versus spreading is decided at the score stage.
> - **Why one plugin and not two:** both stages need the same calculation, so one plugin computes it once and uses it twice. NodeAffinity, TaintToleration, InterPodAffinity and PodTopologySpread work the same way: they filter on their hard rules and score on their soft ones.

- **reserve and permit (6, 7):** reserve holds the chosen node's resources (volumes, devices) while binding is in progress. Permit can approve, deny or make a pod **wait**; nothing runs there by default, but gang plugins use it to hold each pod until its whole group has a place.
- **The binding cycle (8 to 10):** preBind prepares what the pod needs (for example provisioning its volume), bind writes the chosen node into the pod, postBind cleans up. This part runs in the background, so the scheduler can start on the next pod.
- **profiles[]:** one entry per scheduler name. A pod picks one with `schedulerName`, and one kube-scheduler process can serve several.
- **plugins:** per stage, which plugins are enabled or disabled, and under `score`, each plugin's weight.
- **pluginConfig:** per plugin, its own settings (`args`), which change how it calculates rather than whether it runs.

**Going deeper, by topic.**

- **Reading a real configuration:** in this example, `plugins` says BalancedResourceAllocation's score counts double, and `pluginConfig` says which resources it compares when it measures balance. BalancedResourceAllocation is already on by default, so listing it under `enabled` only changes its weight; it doesn't add a second copy.

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
- schedulerName: default-scheduler     # profiles[]: one scheduler
  plugins:                             # plugins: which run, per stage #!
    score:
      enabled:
      - name: BalancedResourceAllocation
        weight: 2                      # counts double vs. other scoring plugins (default 1) #!
  pluginConfig:                        # pluginConfig: how a plugin calculates #!
  - name: BalancedResourceAllocation
    args:
      resources:                       # which resources to keep in balance #!
      - name: cpu
        weight: 1
      - name: memory
        weight: 1
      - name: nvidia.com/gpu           # e.g. also balance GPUs
        weight: 1
```

| The `weight` in… | What it weighs |
|---|---|
| `plugins.score.enabled[].weight` | This **plugin** against the other scoring plugins |
| `pluginConfig[].args.resources[].weight` | One **resource** against the others, inside this plugin's own calculation |

> **Note: two plugins tuned in one file**
>
> The fit check section shows NodeResourcesFit's settings, and the BalancedResourceAllocation section shows that plugin's. They're pieces of the same kind of file, each showing only the plugin it's about. A real file that tunes both has **one profile** (one scheduler) with **one `pluginConfig` list holding two entries**, one per plugin:
>
> ![One KubeSchedulerConfiguration file, box by box](diagrams/extra/cfg-structure.svg)
>
> ```yaml
> apiVersion: kubescheduler.config.k8s.io/v1
> kind: KubeSchedulerConfiguration
> profiles:
> - schedulerName: default-scheduler   # the only profile #!
>   plugins:
>     score:
>       enabled:
>       - name: BalancedResourceAllocation
>         weight: 2                    # this plugin's vote counts double
>   pluginConfig:
>   - name: NodeResourcesFit           # entry 1: how the fit plugin scores #!
>     args:
>       scoringStrategy:
>         type: LeastAllocated         # spread (default); MostAllocated = bin-pack
>         resources:                   # what counts when judging how full a node is
>         - name: cpu
>           weight: 1
>         - name: memory
>           weight: 1
>   - name: BalancedResourceAllocation # entry 2: what the balancer compares #!
>     args:
>       resources:                     # what it tries to keep even with each other
>       - name: cpu
>         weight: 1
>       - name: memory
>         weight: 1
>       - name: nvidia.com/gpu
>         weight: 1
> ```
>
> - **How to read it:** `profiles:` is a list, and each `- schedulerName:` item is one scheduler. Under it sit that scheduler's `plugins` and `pluginConfig`. `pluginConfig` is a list too, and each `- name:` is one plugin's settings.
> - **Both plugins have a `resources` list, but they mean different things:** for NodeResourcesFit it's what counts when judging how full or empty a node is; for BalancedResourceAllocation it's what it tries to keep even.
> - **A second scheduler** would be a second `- schedulerName:` item under `profiles`, with its own `plugins` and `pluginConfig`.

- **There isn't an entry for every plugin:**
  - you only list what you change; everything else keeps its default (on, with its default weight and settings);
  - only about half the plugins have settings at all, for example NodeResourcesFit (`scoringStrategy`), BalancedResourceAllocation (`resources`), InterPodAffinity, PodTopologySpread (default spread rules), NodeAffinity (an extra node filter for every pod), VolumeBinding and DefaultPreemption; others, like TaintToleration or ImageLocality, can only be switched on or off and weighted;
  - there's at most one `pluginConfig` entry per plugin in each profile, and each profile can have its own.
> **Note: different scoring and filtering for different pods**
>
> A pod can't pick plugins directly: every enabled scoring plugin runs for every pod, and the scores are added up. What a pod *can* pick is the **profile**, through `schedulerName`. So you create two profiles with different scoring, and each pod chooses one. For example, one profile without BalancedResourceAllocation and one without NodeResourcesFit's score:
>
> ```yaml
> apiVersion: kubescheduler.config.k8s.io/v1
> kind: KubeSchedulerConfiguration
> profiles:
> - schedulerName: fit-scorer                # profile 1 #!
>   plugins:
>     score:
>       disabled:
>       - name: BalancedResourceAllocation   # turned off for this profile's pods #!
> - schedulerName: balance-scorer            # profile 2 #!
>   plugins:
>     score:
>       disabled:
>       - name: NodeResourcesFit             # turned off at the SCORE stage only #!
> ---
> # Each pod picks its profile
> apiVersion: v1
> kind: Pod
> metadata:
>   name: pod-a
> spec:
>   schedulerName: fit-scorer                # scored without BalancedResourceAllocation #!
>   containers:
>   - name: app
>     image: my-company/app:1.0
> ---
> apiVersion: v1
> kind: Pod
> metadata:
>   name: pod-b
> spec:
>   schedulerName: balance-scorer            # scored without NodeResourcesFit #!
>   containers:
>   - name: app
>     image: my-company/app:1.0
> ```
>
> - **`disabled` under `score` only affects scoring:** NodeResourcesFit still does the fit check at the filter stage for `balance-scorer` pods. Turning it off there would let pods land on nodes they don't fit on.
> - **The other scoring plugins still run** (ImageLocality, NodeAffinity, TaintToleration and others); you only removed one plugin from the sum. To make one plugin dominate instead of removing another, give it a high `weight` in that profile.
> - **A pod's own spec already decides some of it:** NodeAffinity and InterPodAffinity only add points when the pod has preferred rules, so a pod without them gets nothing from those plugins anyway.
> - **On EKS** you can't add profiles to the default scheduler, so you'd run these two profiles in your own kube-scheduler, as in the [second-scheduler appendix](#appendix-second-scheduler). One process can serve both names.
>
> **The same works at the filter stage.** Put `disabled` under `filter` instead of `score`, and that plugin stops removing nodes for the profile's pods. Filters have no weights, because they only say yes or no, so switching one off is the only plugin-level change there. For example, a profile whose pods ignore `NoSchedule` taints:
>
> ```yaml
> apiVersion: kubescheduler.config.k8s.io/v1
> kind: KubeSchedulerConfiguration
> profiles:
> - schedulerName: default-scheduler         # normal filtering #!
> - schedulerName: taint-blind               # profile 2 #!
>   plugins:
>     filter:
>       disabled:
>       - name: TaintToleration              # turned off at the FILTER stage only #!
> ---
> apiVersion: v1
> kind: Pod
> metadata:
>   name: node-debug
> spec:
>   schedulerName: taint-blind               # may land on tainted nodes #!
>   containers:
>   - name: debug
>     image: busybox:1.36
>     command: ["sleep", "3600"]
> ```
>
> - **What changes:** for `taint-blind` pods, a node with a `NoSchedule` taint is no longer removed. Every other filter still runs, so the pod still needs to fit, match its required affinity and find its ports free.
> - **Scoring is untouched:** TaintToleration still scores, so a node with a `PreferNoSchedule` taint is still ranked lower. Disabling a plugin at one stage leaves its other stages on.
> - **The scheduler decides; the kubelet has a veto on a few rules.** When the pod reaches its node, the kubelet checks it once more before starting it ([kubelet admission source](https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/lifecycle/predicate.go)):
>   - **On its own node, its "no" beats the scheduler's "yes":** if a check fails, the pod is refused there.
>   - **It can only refuse, never choose:** it doesn't send the pod to another node. The pod is marked Failed and stays bound to that node.
>   - **It checks a short list:** resources, required node affinity, the node name, host ports and `NoExecute` taints. It doesn't check `NoSchedule` taints, pod affinity or anti-affinity, topology spread or volumes, so for those the scheduler's decision stands.
>   - **Why the check exists:** some pods never pass through the scheduler (a pod that names its node directly, or a static pod the kubelet starts from a file), and the scheduler's view of the node can be out of date by the time the pod arrives.
>   - **What it means for this profile:** `taint-blind` pods get past `NoSchedule` taints, because only the scheduler checks those. A `NoExecute` taint still turns them away at the node.
> - **Usually a toleration is the better tool:** a pod that should run on tainted nodes can say so itself (`tolerations: [{operator: Exists}]`) on the normal scheduler. A filter-less profile makes sense when you can't edit the pods, and anyone who can set `schedulerName` gets past your taints, so treat it as a privilege.
> - **Other filter changes don't switch plugins off:** they change a filter plugin's settings in `pluginConfig`, for example NodeAffinity's `addedAffinity` (a node rule every pod of the profile must also meet) or NodeResourcesFit's `ignoredResources` (extended resources the fit check skips) ([configuration reference](https://kubernetes.io/docs/reference/config-api/kube-scheduler-config.v1/)).

- **Turning off a filter is rarely a good idea:** without TaintToleration, for example, pods would ignore every `NoSchedule` taint, as in the note above. Custom setups usually *add* plugins, such as a gang plugin at `permit`.

> **Note: one file, several schedulers, and other schedulers' formats**
>
> - **kube-scheduler reads a `KubeSchedulerConfiguration`:** the default scheduler, and any second scheduler that is just another copy of kube-scheduler (like the one in the [appendix](#appendix-second-scheduler)), reads this file at startup through `--config`. It decides which plugins run, their weights and their settings. Without a file, the built-in defaults apply.
> - **One file can define several schedulers:** the file holds a list of **profiles**, each with its own `schedulerName` and plugin setup, all served by one kube-scheduler process. A pod chooses a profile with `schedulerName`, exactly as it would choose a separate scheduler:
>
> ```yaml
> apiVersion: kubescheduler.config.k8s.io/v1
> kind: KubeSchedulerConfiguration
> profiles:
> - schedulerName: default-scheduler        # normal behavior #!
> - schedulerName: bin-packing-scheduler    # same process, different scoring #!
>   pluginConfig:
>   - name: NodeResourcesFit
>     args:
>       scoringStrategy:
>         type: MostAllocated               # pack nodes full instead of spreading #!
> ```
>
> - **So a "second scheduler" can be a second profile:** no second program is needed, as long as you control the scheduler's configuration. On EKS you control only one of the default scheduler's scoring settings, not its profiles or plugin weights, which is why the appendix runs a separate copy.
> - **Other schedulers have their own formats:** the idea is the same, but the files differ.
>   - **Volcano:** a ConfigMap (`volcano-scheduler.conf`) listing the scheduling *actions* to run (such as enqueue, allocate, backfill) and the *plugins* in priority tiers (gang, drf, proportion and others).
>   - **YuniKorn:** a ConfigMap (`yunikorn-configs`) that mainly describes the **queue** hierarchy, with quotas and policies per queue.
>   - **KAI:** settings through its Helm values and its own custom resources (queues, scheduling shards).
>   - **Kueue** (a queue in front of a scheduler, not a scheduler): its own configuration file plus custom resources such as `ClusterQueue` and `LocalQueue`.

- **Do other schedulers use these stages?** Only schedulers built on kube-scheduler's framework:
  - **the same stages:** the default scheduler, any second copy or profile of kube-scheduler, and schedulers built from the scheduler-plugins project (such as coscheduling, which hooks into `permit`);
  - **Volcano and KAI** (both descend from kube-batch) work in sessions: on each cycle they snapshot the cluster and run a list of **actions** in order (for example enqueue, allocate, preempt, reclaim, backfill; KAI adds consolidation), and their plugins plug decision functions into those actions, which is how they place a whole job at once;
  - **YuniKorn** runs allocation cycles over a hierarchy of queues, connected to Kubernetes through a "shim";
  - **the Fargate scheduler** is run by AWS and can't be inspected or configured;
  - **Kueue** isn't a scheduler: it decides *when* a job may start, and its pods are then placed by a normal scheduler.
- **What every scheduler shares anyway:** most reuse kube-scheduler's node checks (resources, taints, affinity), so a taint or a `nodeSelector` means the same thing whoever places the pod, and every scheduler ends by binding the pod to a node through the API server, after which the kubelet takes over.

### How a Pod finds its scheduler

**Scenario.** A team runs one EKS cluster where some pods run on EC2 nodes and others on Fargate. Nobody on the team ever sets `schedulerName` by hand, yet the staging pods always end up on Fargate and everything else on EC2. A new engineer asks how Kubernetes knows which scheduler should handle each pod.

![How a Pod finds its scheduler](diagrams/sections/d02-1.svg)

**What the picture shows.** How a pod is routed to the right scheduler, using Fargate as the example.

- **Workflow, step 1 (user submission and Fargate profile):** you create a Fargate profile in AWS that says "any pod in the `staging` namespace with the label `type: serverless` runs on Fargate". Then you submit an ordinary pod YAML to the API server.

```yaml
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: prod
  region: us-east-1
fargateProfiles:
- name: staging-serverless
  selectors:
  - namespace: staging #!
    labels:
      type: serverless #!
```

- **Step 2 (EKS control plane, mutating webhook):** the EKS mutating webhook sees the pod, checks it against your profiles, and on a match injects `schedulerName: fargate-scheduler` into the pod spec, before scheduling starts. Pods that don't match keep the standard spec.

```yaml
# the pod as stored after the webhook mutates it
apiVersion: v1
kind: Pod
metadata:
  name: web
  namespace: staging
  labels:
    type: serverless
spec:
  schedulerName: fargate-scheduler   # injected by EKS, not written by you #!
  containers:
  - name: web
    image: my-company/web:1.0
```

- **Step 3 (scheduling phase, the result):** the modified pod is picked up by the AWS Fargate scheduler.
- **How pods reach the right scheduler:** the API server doesn't route pods; it only stores them. Every scheduler watches for unscheduled pods (no `nodeName` yet) and takes only the ones whose `schedulerName` matches its own name, like separate buckets. What each scheduler then does with a pod is set by its own configuration; see [Configuring a scheduler](#d02-9).
  - **Bucket A (`default-scheduler`):** the standard kube-scheduler on the control plane places these on EC2 nodes.
  - **Bucket B (`fargate-scheduler`):** the Fargate scheduler takes only these, ignores bucket A, and starts a dedicated micro-VM for each pod.

**Going deeper, by topic.**

- **Why Ray exists:** reinforcement learning mixes many small simulations on CPUs, training on GPUs, and serving the current policy, all in one program that hands data between them in milliseconds. The systems of the time each did one part, and gluing them together was too slow, so teams kept writing one-off systems. Ray (UC Berkeley, 2017) is one Python framework for all of it: it can place very large numbers of short tasks per second, on CPUs or GPUs, and take their results in whatever order they finish ([Ray paper](https://arxiv.org/abs/1712.05889)).
- **The example job, RL fine-tuning of a coding model:** each round, a **Generator** writes 1,000 answers on a GPU, unit tests score each answer on CPUs, and a **Trainer** turns the scores into better weights on another GPU. The Generator and the Trainer are both Python classes you write, and each holds its own copy of the same model. The Generator's copy only writes answers. The Trainer's copy also keeps what learning needs, such as gradients and optimizer state, and after each round its new weights are copied back to the Generator. The tests are plain Python with no model at all.

![One round of RL fine-tuning](diagrams/extra/d03-rl-round.svg)

- **Ray's architecture, through that job:** a Ray cluster is one **head node** and any number of **worker nodes** ([Ray cluster concepts](https://docs.ray.io/en/latest/cluster/key-concepts.html)), and each part of the job lands on one of them. The numbers are illustrative:
  - **The head (purple) runs the loop:** the **driver** is the training script itself, `rl_loop.py`. The **GCS** is the cluster's directory: at startup it placed the Generator and the Trainer on nodes with a free GPU, and it remembers where. The **autoscaler** notices when tests wait for CPUs and asks for another CPU node.
  - **Every node (orange) has a raylet and an object store:** the raylet is that node's queue. On the CPU node it holds the 1,000 test tasks and runs 32 at a time, one per CPU. The object store is the node's shared memory: the GPU node's store holds the answers, the CPU node's store holds the scores.
  - **Your code (green) is tasks and actors:** each `run_tests` call is a **task**, a function that runs once and ends. The Generator and the Trainer are **actors**, long-lived processes that keep the model in GPU memory between rounds instead of reloading it every time.
  - **The driver passes tickets, not data:** every call returns a reference at once, like a claim ticket. When the driver hands answer tickets to the tests, each answer is copied straight from the Generator's node to the CPU node, and scores travel from the CPU node to the Trainer's node in the same way ([passing objects](https://docs.ray.io/en/latest/ray-core/objects.html)). The driver never holds the 1,000 answers itself.
  - **The "reload" is one method you write:** Ray has no built-in model reload. Your Generator has a `load_weights` method, and the driver calls it with the Trainer's new weights. Ray carries the weights to the GPU node, and the model stays loaded.
  - **Why a separate head:** the driver, the GCS and the autoscaler must exist exactly once for the whole run. By default the GCS keeps its data in memory, so losing the head stops the run unless the GCS is backed by Redis ([GCS fault tolerance in KubeRay](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/kuberay-gcs-ft.html)).

![Ray's own architecture, seen through one job](diagrams/extra/d03-ray-architecture.svg)

- **The same job on Kubernetes:** each Ray node becomes a pod. The head pod holds the driver, the GCS, its own raylet and object store, and the autoscaler as a sidecar container. The Generator and the Trainer run in two pods of a GPU worker group, and the tests in a pod of a CPU worker group. Each pod's container limits become the slots Ray hands out ([RayCluster configuration](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/config.html)). Kubernetes places the four pods; Ray places the Generator, the Trainer and the tests inside them.

![The same RL job on an EKS cluster](diagrams/extra/d03-kuberay-two-levels.svg)

- **How it's set up in YAML:** you never write a `KubeSchedulerConfiguration` for Ray's scheduler. It is set up by three things: the pods' resources, a few `rayStartParams`, and what the Python code asks for. The pods are described in a `RayCluster` (or a `RayJob`, which creates one per run). This one has the head and the two worker groups of the job above:

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: rl-coding
spec:
  rayVersion: "2.56.1"
  enableInTreeAutoscaling: true        # Ray's autoscaler runs as a sidecar in the head pod #!
  headGroupSpec:
    rayStartParams:
      num-cpus: "0"                    # no tests or actors on the head pod #!
    template:
      spec:
        containers:
        - name: ray-head
          image: rayproject/ray:2.56.1
          resources:
            requests: {cpu: "4", memory: 16Gi}
            limits:   {cpu: "4", memory: 16Gi}
  workerGroupSpecs:
  - groupName: gpu-group               # the Generator and the Trainer
    replicas: 2
    minReplicas: 2
    maxReplicas: 2
    rayStartParams: {}
    template:
      spec:
        tolerations:                   # Kubernetes level: which nodes these pods may use
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
        containers:
        - name: ray-worker
          image: rayproject/ray:2.56.1
          resources:                   # Ray level: these limits become the pod's Ray slots #!
            requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: 1}
            limits:   {cpu: "8", memory: 64Gi, nvidia.com/gpu: 1}
  - groupName: cpu-group               # the unit tests
    replicas: 1
    minReplicas: 1                     # the autoscaler moves replicas between these #!
    maxReplicas: 4 #!
    rayStartParams: {}
    template:
      spec:
        containers:
        - name: ray-worker
          image: my-company/ray-sandbox:2.56.1
          resources:
            requests: {cpu: "32", memory: 64Gi}   # 32 CPU slots: 32 tests at once #!
            limits:   {cpu: "32", memory: 64Gi}
```

```python
import ray

@ray.remote(num_gpus=1)                   # needs a GPU slot: lands in a gpu-group pod
class Generator:
    def generate(self, prompt): ...       # returns one answer
    def load_weights(self, weights): ...  # the "reload": a method you write

@ray.remote(num_gpus=1)
class Trainer:
    def train(self, answers, scores): ... # calls ray.get on the lists, returns new weights

@ray.remote(num_cpus=1)                   # needs a CPU slot: lands in the cpu-group pod
def run_tests(answer): ...                # returns a score

gen, trainer = Generator.remote(), Trainer.remote()      # the GCS places both
for step in range(100):
    answers = [gen.generate.remote(p) for p in prompts]  # 1,000 tickets, no data yet
    scores = [run_tests.remote(a) for a in answers]      # answers copied node to node
    weights = trainer.train.remote(answers, scores)      # lists of tickets
    ray.get(gen.load_weights.remote(weights))            # wait until the reload is done
```

- **Which scheduler places the pods:** this is chosen for the whole operator, not per cluster. By default it is kube-scheduler. To place each Ray cluster's pods as one gang, install the operator with a batch scheduler, for example the Helm value `batchScheduler.name: volcano`, and put the cluster in a Volcano queue with the label `volcano.sh/queue-name` ([KubeRay with Volcano](https://docs.ray.io/en/latest/cluster/kubernetes/k8s-ecosystem/volcano.html)). KubeRay also has integrations for YuniKorn and the scheduler-plugins coscheduling plugin.
- **Requests and limits:** KubeRay uses the CPU request when there is no limit, rounds CPU up to whole numbers, and ignores memory and GPU *requests*. So set memory and GPU requests equal to their limits.

- **What is specific about KubeRay:** the other schedulers on this page, Volcano, YuniKorn, KAI and Fargate, all do the same job as kube-scheduler: they put **pods** on **nodes**. Ray's scheduler doesn't touch pods. It works one level down:
  - **It schedules your code, not pods.** It places tasks and actors from the resources requested in Python (`num_gpus=1`), inside pods that already exist. Kubernetes never sees a task or an actor.
  - **It is part of the application.** It runs inside the Ray cluster, starts and stops with it, and is configured by the program rather than by the platform team.
  - **It brings its own autoscaler.** The autoscaler watches Ray work that can't be placed and raises a worker group's `replicas` in the RayCluster. KubeRay then creates the pod. It scales on the resources Ray work *asks for*, not on CPU use ([Ray autoscaling on Kubernetes](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/configuring-autoscaling.html)).
  - **It doesn't replace a pod scheduler; it sits on top of one.** A Ray cluster can run its pods through kube-scheduler, Volcano or YuniKorn, and Kueue can admit a RayJob as a whole, as in [Ray Train on KubeRay](#ds20-3) in Design 20.
  - **It gangs work only when asked.** By default Ray places each task and actor on its own. A *placement group* reserves several slots together, all or nothing ([Ray scheduling](https://docs.ray.io/en/latest/ray-core/scheduling/index.html)). That is a gang inside the Ray cluster; a gang of *pods* still needs Volcano, YuniKorn, coscheduling or Kueue.
- **The cost of two layers:**
  - **Waiting happens in two places:** a pod can be Pending in Kubernetes (`kubectl describe pod`), or a task can be waiting inside Ray for a free slot (`ray status`). Check both.
  - **Quotas see only the pods:** a ResourceQuota or Kueue counts the pods' requests. A long-lived Ray cluster shared by a team holds its GPUs whether or not its tasks use them. Design 20 uses one short-lived RayJob per run for that reason.
  - **Two autoscalers:** Ray's adds pods, and Karpenter adds the nodes those pods need.
  - **Good fit:** Python programs with many distributed parts, such as RL with rollout workers, reward models and a trainer. For serving, see [Ray Serve and KubeRay](#p01-5).

### Gang scheduling

> **Note: from Ray to gang scheduling, two different problems**
>
> - **Gang scheduling is about pods on nodes:** a job's pods must start all together or not at all, so a few running pods don't sit on GPUs waiting for the rest.
> - **Ray is about work inside pods:** it spreads many small tasks and long-lived actors over pods that already exist, moves their data, and collects the results.
> - **Where they meet:** a Ray cluster is itself a group of pods, so it can need gang scheduling too. With the default scheduler a Ray cluster can be only partly placed, which is why KubeRay can hand its pods to Volcano, YuniKorn or coscheduling ([KubeRay with Volcano](https://docs.ray.io/en/latest/cluster/kubernetes/k8s-ecosystem/volcano.html)). Ray's own placement groups do all-or-nothing one level down, for slots inside pods that already exist.

**Scenario.** A research team submits a distributed PyTorch job with 8 GPU workers. The cluster only has room for 6, so the default scheduler starts 6 workers and leaves 2 `Pending`. The 6 running workers sit idle for hours, burning expensive GPU time while they wait for teammates that may never arrive. How do you stop that from happening?

![Gang scheduling](diagrams/sections/d02-2.svg)

**What the picture shows.** Gang scheduling (all-or-nothing) and why the default scheduler can't do it.

- **The problem:** standard Kubernetes schedules pods one at a time. In distributed training, all 8 workers must start together to run collective operations such as all-reduce. If only 6 fit, those 6 start and wait, wasting GPU time.
- **The custom solution:** a gang scheduler won't start any of the job's pods until the cluster has room for all of them.
- **Can the standard scheduler do it?** Not out of the box: it has no all-or-nothing start. A native one is beta in 1.37 but off by default (see the note after "Going deeper").
  - **The workaround:** you calculate cluster capacity yourself and make sure at least 8 GPU slots are free before you run `kubectl apply`.
  - **The failure point:** in a shared cluster, a coworker's 1-GPU pod submitted one second before yours can take a slot. Your job starts with 7 workers, hangs, and wastes money. Standard Kubernetes can't say "hold these 7 slots until the 8th is free."
- **Best at this:** Volcano and KAI are built specifically for this kind of atomic scheduling.

**Going deeper, by topic.**

- **Why a partial start never recovers:** the reason is the **rendezvous barrier**. Distributed PyTorch workers wait until every rank has joined, so 7 of 8 workers will sit on their GPUs forever.
- **Four ways to get all-or-nothing:** they come from different projects and work in different places, so each has its own accordion below.
  - **Option 1, a plugin inside a scheduler:** the coscheduling plugin from the <strong><mark class="kw-act">scheduler-plugins project</mark></strong> holds a group's pods until all of them have a node.
  - **Option 2, a gate in front of the scheduler:** Kueue decides at the job level whether a job may start at all. The normal scheduler still places its pods.
  - **Option 3, a different scheduler:** Volcano or KAI replaces kube-scheduler for these pods and places the whole job at once.
  - **Option 4, built into kube-scheduler itself:** Kubernetes' own Workload API, newer and still off by default.
- **Option 1, the scheduler-plugins project (coscheduling):** the <strong><mark class="kw-act">scheduler-plugins project</mark></strong> ([on GitHub](https://github.com/kubernetes-sigs/scheduler-plugins)) is a Kubernetes SIG project of extra plugins built on the same scheduler framework as kube-scheduler. You can't drop a plugin into a running scheduler, because plugins are compiled into the scheduler program. So the project ships two container images. The scheduler image is kube-scheduler compiled from the same Kubernetes scheduler code with the extra plugins added, so every built-in plugin is in it too; it isn't a copy of your running default scheduler and doesn't read that scheduler's settings. The controller image manages the plugins' own objects ([install guide](https://github.com/kubernetes-sigs/scheduler-plugins/blob/master/doc/install.md)). Coscheduling is the one this section needs; the others are shown so you know what else the same scheduler can do ([project README](https://github.com/kubernetes-sigs/scheduler-plugins/blob/master/README.md)).

![The scheduler-plugins project](diagrams/extra/d03-scheduler-plugins-project.svg)

- **This scheduler has its own `KubeSchedulerConfiguration`:** a scheduler's configuration file, the YAML of kind `KubeSchedulerConfiguration` that lists its profiles and plugins, isn't a cluster object you apply with kubectl. It's handed to one scheduler program when that program starts, with `--config`, and only that program reads it. So a cluster can run several scheduler programs, and each reads its own file: the default scheduler's file, which AWS manages on EKS; the scheduler-plugins scheduler's file, which its Helm chart writes into a ConfigMap; and any copy of kube-scheduler you run yourself, as in the [second-scheduler appendix](#appendix-second-scheduler). Inside each file, every profile is one scheduler name, which is what pods choose with `schedulerName`. Installing the <strong><mark class="kw-act">plugins scheduler</mark></strong> as a second scheduler leaves the default scheduler and its file untouched.

![Each scheduler program reads its own KubeSchedulerConfiguration](diagrams/extra/d03-scheduler-configs.svg)

- **Option 1 in practice, installing and using coscheduling:** it runs as a second scheduler from the project's Helm chart. The steps below install it, switch on only coscheduling, put 8 workers in one group, and check that they start together or not at all.

> **Note: what using the coscheduling plugin involves**
>
> - **Where it hooks in:** not at filter or score. It keeps a group's pods together in the queue (`queueSort`), holds each pod at `permit` after it has been given a node, releases them all once `minMember` are waiting, and frees their nodes (`unreserve`) when the timeout passes. An optional `preFilter` check rejects a group early if it has fewer pods than `minMember` ([coscheduling README](https://github.com/kubernetes-sigs/scheduler-plugins/blob/master/pkg/coscheduling/README.md)).
> - **What you install:** four pieces, all from one Helm command in Step 2 below: that scheduler image, the PodGroup custom resource, the project's controller that manages PodGroups, and permissions for the scheduler to read them ([install guide](https://github.com/kubernetes-sigs/scheduler-plugins/blob/master/doc/install.md)).
> - **Step 1, choose what to switch on (`values.yaml`):** the project's Helm chart installs it as a second scheduler. It writes that scheduler's `KubeSchedulerConfiguration` for you, from these values ([chart values](https://github.com/kubernetes-sigs/scheduler-plugins/blob/master/manifests/install/charts/as-a-second-scheduler/values.yaml)). By default the chart turns on three more plugins; this keeps only coscheduling:
>
> ```yaml
> plugins:
>   enabled: ["Coscheduling"]           # on at every stage it implements #!
>   disabled: ["PrioritySort"]          # its queue sort replaces this one #!
> pluginConfig:
> - name: Coscheduling
>   args:
>     permitWaitingTimeSeconds: 300     # longest a pod waits at permit (default 60) #!
> ```
>
> - **Step 2, install it:** one Helm release creates the scheduler (`scheduler-plugins-scheduler`), the controller, the PodGroup CRD and the permissions ([chart README](https://github.com/kubernetes-sigs/scheduler-plugins/blob/master/manifests/install/charts/as-a-second-scheduler/README.md)):
>
> ```bash
> helm install scheduler-plugins scheduler-plugins \
>   --repo https://scheduler-plugins.sigs.k8s.io \
>   --namespace scheduler-plugins --create-namespace \
>   -f values.yaml
>
> kubectl get deploy -n scheduler-plugins            # scheduler and controller: 1/1 #!
> kubectl get crd podgroups.scheduling.x-k8s.io      # the PodGroup type exists
> kubectl get configmap scheduler-config -n scheduler-plugins -o yaml   # the generated config
> ```
>
> - **What the chart writes:** the ConfigMap holds an ordinary `KubeSchedulerConfiguration`, one profile with your plugins:
>
> ```yaml
> apiVersion: kubescheduler.config.k8s.io/v1
> kind: KubeSchedulerConfiguration
> profiles:
> - schedulerName: scheduler-plugins-scheduler   # the name pods ask for #!
>   plugins:
>     multiPoint:
>       enabled:
>       - name: Coscheduling
>       disabled:
>       - name: PrioritySort
>   pluginConfig:
>   - name: Coscheduling
>     args:
>       permitWaitingTimeSeconds: 300
> ```
>
> - **Step 3, use it:** create a PodGroup, then give each worker the group's label and the scheduler's name:
>
> ```yaml
> apiVersion: scheduling.x-k8s.io/v1alpha1
> kind: PodGroup
> metadata:
>   name: trainer-gang
>   namespace: ml                                 # same namespace as the pods #!
> spec:
>   minMember: 8                                  # all 8 workers, or none #!
>   scheduleTimeoutSeconds: 300                   # then release and retry later
> ---
> apiVersion: batch/v1
> kind: Job
> metadata:
>   name: trainer
>   namespace: ml
> spec:
>   parallelism: 8                                # 8 worker pods at once #!
>   completions: 8
>   completionMode: Indexed                       # each pod gets an index, its rank
>   backoffLimit: 0
>   template:
>     metadata:
>       labels:
>         scheduling.x-k8s.io/pod-group: trainer-gang   # joins the group #!
>     spec:
>       schedulerName: scheduler-plugins-scheduler      # picks that scheduler #!
>       restartPolicy: Never
>       containers:
>       - name: worker
>         image: my-company/trainer:2.0
>         resources:
>           requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: 1}
>           limits:   {cpu: "8", memory: 64Gi, nvidia.com/gpu: 1}
> ```
>
> - **Step 4, check it:** with fewer than 8 workers able to fit, all of them stay Pending instead of 7 starting. Once all 8 fit, they start together:
>
> ```bash
> kubectl get pods -n ml -l scheduling.x-k8s.io/pod-group=trainer-gang
> kubectl get podgroup trainer-gang -n ml -o yaml        # status.phase, scheduled, running #!
> ```
>
> - **On EKS:** you can't replace the default scheduler, so this runs as a second scheduler, which the project installs with a Helm chart. The project itself warns that two schedulers can place pods on the same node at the same moment when the cluster is short of room, and recommends a single scheduler in production. See the [second-scheduler appendix](#appendix-second-scheduler) for running one.

- **Option 2, Kueue (a gate at the job level):** Kueue is a separate SIG project that isn't a scheduler. It holds a whole job back, suspended, until its queue has quota for all of its pods, then lets it start, and the normal scheduler places the pods. Quota alone doesn't guarantee the pods all fit on real nodes. Its `waitForPodsReady` setting adds the all-or-nothing part: if not all of an admitted job's pods are ready within a timeout, Kueue suspends the job and puts it back in the queue ([waitForPodsReady docs](https://kueue.sigs.k8s.io/docs/tasks/manage/setup_wait_for_pods_ready/)).

![Where Kueue sits: in front of the scheduler](diagrams/extra/d03-kueue-where.svg)


> **Note: installing and using Kueue for gangs**
>
> - **Step 1, choose the settings (`kueue-values.yaml`):** `waitForPodsReady` is switched on by being present in Kueue's own configuration ([configuration API](https://github.com/kubernetes-sigs/kueue/blob/main/apis/config/v1beta2/configuration_types.go)), which the Helm chart takes under `managerConfig` ([chart README](https://github.com/kubernetes-sigs/kueue/blob/main/charts/kueue/README.md)):
>
> ```yaml
> managerConfig:
>   structuredOverrides:
>     waitForPodsReady:
>       timeout: 10m               # all pods ready within 10 minutes, or requeue #!
>       blockAdmission: true       # admit one gang at a time, so two can't deadlock #!
> ```
>
> - **Step 2, install it:**
>
> ```bash
> helm install kueue oci://registry.k8s.io/kueue/charts/kueue --version="0.20.7" \
>   --create-namespace --namespace=kueue-system \
>   --values kueue-values.yaml
>
> kubectl get deploy -n kueue-system          # kueue-controller-manager ready #!
> ```
>
> - **Step 3, create the queues:** Kueue needs three objects before it admits anything. Together they say "this team may use up to 50 GPUs of this kind, and jobs in its namespace queue here":
>   - **ResourceFlavor:** a kind of hardware, such as A100 nodes. Empty, as here, it means any node; with `nodeLabels` it points at one node type.
>   - **ClusterQueue:** the cluster-wide budget. It holds the quota per flavor, here 50 GPUs, and the waiting line Kueue admits jobs from. A cluster admin creates it; it has no namespace. The 50 is one shared pot: every job from every LocalQueue that points here counts against it, so separate teams need separate ClusterQueues. Its `namespaceSelector` says which namespaces may use it; `{}` would let any namespace in, so this one names the team's two namespaces ([ClusterQueue docs](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/)).
>   - **LocalQueue:** a team's entry point into a ClusterQueue, created in the team's namespace. Jobs name it with the `kueue.x-k8s.io/queue-name` label.
>
> ![Kueue's two levels: LocalQueues in namespaces, ClusterQueues at cluster level](diagrams/extra/d03-kueue-queues.svg)
>
> ```yaml
> apiVersion: kueue.x-k8s.io/v1beta2
> kind: ResourceFlavor
> metadata:
>   name: a100                     # a kind of GPU node #!
> ---
> apiVersion: kueue.x-k8s.io/v1beta2
> kind: ClusterQueue
> metadata:
>   name: data-science             # cluster-wide, no namespace #!
> spec:
>   namespaceSelector:             # only these namespaces may spend this budget #!
>     matchExpressions:
>     - key: kubernetes.io/metadata.name   # every namespace carries its own name as this label
>       operator: In
>       values: ["data-science", "data-science-dev"] #!
>   resourceGroups:
>   - coveredResources: ["nvidia.com/gpu"]
>     flavors:
>     - name: a100
>       resources:
>       - name: nvidia.com/gpu
>         nominalQuota: 50         # the team's budget: 50 GPUs in use at once #!
> ---
> apiVersion: kueue.x-k8s.io/v1beta2
> kind: LocalQueue
> metadata:
>   name: ds-queue                 # the name jobs put in their label #!
>   namespace: data-science        # the team's namespace #!
> spec:
>   clusterQueue: data-science     # which budget it draws from #!
> ```
>
> ```bash
> kubectl apply -f queues.yaml
> kubectl get clusterqueues                    # data-science
> kubectl -n data-science get localqueues      # ds-queue -> data-science
> ```
>
> - **With these in place:** the 8-GPU job in Step 4 is admitted only while the team has 8 of its 50 GPUs free. Borrowing between teams and preemption are added on top, as in the [Volcano vs. Kueue](#d02-6) section.
> - **Step 4, submit the job with one label:** Kueue suspends it on creation and starts it once it's admitted. You don't create it suspended yourself ([running a Job](https://kueue.sigs.k8s.io/docs/tasks/run/jobs/)):
>
> ```yaml
> apiVersion: batch/v1
> kind: Job
> metadata:
>   name: trainer
>   namespace: data-science
>   labels:
>     kueue.x-k8s.io/queue-name: ds-queue    # the LocalQueue #!
> spec:
>   parallelism: 8                           # Kueue admits all 8 or none #!
>   completions: 8
>   template:
>     spec:
>       restartPolicy: Never
>       containers:
>       - name: worker
>         image: my-company/trainer:2.0
>         resources:
>           requests: {nvidia.com/gpu: 1}
> ```
>
> - **Step 5, check it:**
>
> ```bash
> kubectl -n data-science get workloads       # QUEUE, ADMITTED #!
> kubectl -n data-science describe workload <name>   # "workload didn't fit" while waiting
> ```
>
> - **On EKS:** Kueue runs entirely as a controller on your nodes and needs no change to the default scheduler.

- **Option 3, Volcano or KAI (replace the scheduler for these pods):** both are separate projects that run as their own scheduler and place a whole group of pods at once. Pods choose them with `schedulerName`, and both use their own queues and PodGroups.

> **Note: installing and using Volcano or KAI**
>
> - **Volcano, install** ([Volcano README](https://github.com/volcano-sh/volcano/blob/master/README.md)):
>
> ```bash
> helm repo add volcano-sh https://volcano-sh.github.io/helm-charts
> helm install volcano volcano-sh/volcano -n volcano-system --create-namespace
> ```
>
> - **Volcano, use:** submit a Volcano `Job` with `schedulerName: volcano` and `minAvailable` set to the gang size, as in the [Volcano vs. Kueue](#d02-6) section. Volcano creates a PodGroup for the job and starts none of its pods until `minAvailable` fit; `kubectl get podgroup` shows its phase.
> - **KAI, install** (pick a release from the project's releases page) ([KAI README](https://github.com/kai-scheduler/KAI-scheduler/blob/main/README.md)):
>
> ```bash
> helm upgrade -i kai-scheduler oci://ghcr.io/kai-scheduler/kai-scheduler/kai-scheduler \
>   -n kai-scheduler --create-namespace --version <VERSION>
> ```
>
> - **KAI, use:** a queue called `default-queue` exists after install. Each pod sets `schedulerName: kai-scheduler` and the label `kai.scheduler/queue`. KAI groups known workloads itself: a PyTorchJob's master and workers are scheduled together or not at all. For a plain Job, the `kai.scheduler/batch-min-member` annotation sets how many pods must fit together ([KAI batch scheduling](https://github.com/kai-scheduler/KAI-scheduler/blob/main/docs/batch/README.md)):
>
> ```yaml
> apiVersion: batch/v1
> kind: Job
> metadata:
>   name: trainer
>   annotations:
>     kai.scheduler/batch-min-member: "8"    # 8 pods fit together, or none start #!
> spec:
>   parallelism: 8
>   completions: 8
>   template:
>     metadata:
>       labels:
>         kai.scheduler/queue: default-queue #!
>     spec:
>       schedulerName: kai-scheduler #!
>       restartPolicy: OnFailure
>       containers:
>       - name: worker
>         image: my-company/trainer:2.0
>         resources:
>           limits: {nvidia.com/gpu: 1}
> ```
>
> - **On EKS:** like the plugins scheduler, both run as an extra scheduler on your nodes. KAI needs the NVIDIA GPU Operator for GPU workloads, and its docs say to submit work in a namespace other than `kai-scheduler`.

- **Option 4, Kubernetes' own gang scheduling (built into kube-scheduler):** the newest way needs no extra project. Upstream Kubernetes now has a native **Workload API** with gang scheduling in the default scheduler itself: alpha in 1.35, rewritten in 1.36 and beta in 1.37, still off by default. On EKS there is no documented way to switch it on yet, so options 1 to 3 are what you can use today (see the note below).
  - **When options 1 to 3 still make sense once option 4 is available:** option 4 does one thing, placing a group all-or-nothing. Each of the other options is still the right choice when you need more than that:
  - **Option 1, the scheduler-plugins project:** native gang scheduling replaces its coscheduling plugin. The project stays useful when you also want its other plugins, such as hardware-layout-aware or load-aware placement, at the cost of running and upgrading a second scheduler.
  - **Option 2, Kueue:** still needed for team budgets, queues, borrowing and fair sharing, which native gang scheduling doesn't do. The two work together: Kueue decides when a job may start, and the default scheduler places its pods as one group.
  - **Option 3, Volcano or KAI:** still worth it when you need what they add, such as GPU topology placement, sharing parts of a GPU or their own queues, and accept replacing the scheduler for those pods.
  - **On EKS today:** none of this applies yet, because option 4 can't be switched on there, so options 1 to 3 are the ones you can use.

> **Note: Kubernetes' own gang scheduling, and where EKS stands**
>
> A **PodGroup** is a Kubernetes object that holds a set of pods and carries one scheduling policy for them. The policy is either *basic*, which schedules the pods one by one as usual, or *gang*, which is all-or-nothing: if fewer than `minCount` pods can be placed, none are bound ([gang scheduling docs](https://kubernetes.io/docs/concepts/scheduling-eviction/gang-scheduling/)). So a PodGroup is the group, and gang scheduling is one rule you can set on it. The same group can also carry a rule to stay in one rack and settings for how it can be preempted. A **Workload** is the template that a controller, such as the Job controller, copies into PodGroups.
>
> **Status (checked November 2026).** The Workload and PodGroup APIs and gang scheduling began as alpha in 1.35 and were rewritten in 1.36. They are **beta in 1.37, and off by default**: the `GenericWorkload` feature gate must be enabled on the API server, the controller manager and the scheduler ([1.37 release post](https://kubernetes.io/blog/2026/09/08/kubernetes-v1-37-advancing-workload-aware-scheduling/)). The topology rule for a group, groups of groups, and the Job controller's automatic creation of groups are still alpha in 1.37.
>
> **On EKS.** EKS has offered Kubernetes 1.37 since October 2026. AWS's [1.37 release notes](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions-standard.html) do not mention these features, and AWS's list of [control plane settings you can change](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-configuration.html) contains no feature gates. So there is no documented way to turn this on in EKS today. The designs in this blog use Kueue, or a second scheduler, for gangs instead. Check the AWS release notes for newer versions before relying on this.
>
> **Don't confuse it with other PodGroups.** Two other projects have their own objects with the same name. The scheduler-plugins project, mentioned above as the coscheduling plugin, has one: pods join it through a label, and it is built only for all-or-nothing starts. Volcano has one too. It is a custom resource in Volcano's own API group, `scheduling.volcano.sh` ([Volcano docs](https://volcano.sh/docs/concepts/podgroup/)), while Kubernetes' own PodGroup is in `scheduling.k8s.io` ([PodGroup API docs](https://kubernetes.io/docs/concepts/workloads/podgroup-api/)). They are separate objects in separate API groups, so in our reading they do not overwrite each other, but all three are called "a PodGroup". When you read a manifest or a command, check the API group to see which one it means. This page does not cover how Volcano will work with the native API.
>
> **On a Job.** The Job controller can create the group for you, but this is a separate alpha feature (since 1.36) with its own gate, `WorkloadWithJob`, on top of `GenericWorkload` ([Workload API docs](https://kubernetes.io/docs/concepts/workloads/workload-api/)). You opt in with `.spec.scheduling.schedulingPolicy.gang` on the Job. If you leave out `minCount`, it defaults to the Job's `parallelism`, so every pod must fit. The `disruptionMode` setting decides whether preemption may take the Job's pods one at a time (`single`) or only as a whole (`all`). After creation the `.spec.scheduling` block cannot change, except `minCount` ([1.37 release post](https://kubernetes.io/blog/2026/09/08/kubernetes-v1-37-advancing-workload-aware-scheduling/)).

- **Inference needs it too:** gang scheduling isn't only a training problem. Serving a very large model has the same all-or-nothing need:
  - **Model-parallel and shards:** some models are too big for one machine's GPUs, so the model is cut into pieces. Each piece is a shard, and each shard runs in its own pod on its own node. For example, a model split across 2 nodes with 8 GPUs each becomes 2 pods.
  - **A replica:** those pods together are one working copy of the model that can answer requests. Every request passes through all the shards, because each pod holds only part of the model.
  - **Why all-or-nothing scheduling:** if only one of the 2 pods gets a node, the replica can't answer a single request. The placed pod sits on its GPUs doing nothing, like the 7 of 8 training workers above.
  - **Why group restarts:** if one shard's pod dies, the others can't carry on without the part of the model it held. The whole group has to be restarted together, not just the pod that failed.

### Topology bin-packing

**Scenario.** A training job's two pods talk to each other constantly. The default scheduler puts them on two different GPU nodes because both nodes look "least allocated". Now every gradient exchange crosses the network instead of NVLink, and the training run takes several times longer than expected.

![Topology bin-packing](diagrams/sections/d02-3.svg)

**What the picture shows.** Bin-packing for GPU topology: keeping chatty pods on the same fast interconnect.

- **The problem:** the default LeastAllocated scoring spreads pods across nodes. In ML that's bad for performance, because pod-to-pod traffic then goes over the much slower network.
- **The goal:** a scheduler that uses MostAllocated (bin-packing) to put the pods on the same physical GPU node.
- **The benefit:** pods can talk over NVLink, which has an order of magnitude or more bandwidth than the node's network, cutting training time sharply.
- **Topology-aware bin-packing:** goes further and looks at how the GPUs are wired. On an NVIDIA HGX system, it places pods that talk to each other on GPUs linked by NVLink (fast) rather than only PCIe (slow).
- **Can the standard scheduler do it?** Partly.
  - **The workaround:** a required `podAffinity` with `matchLabels: job: my-training-run` and `topologyKey: kubernetes.io/hostname`, which forces pod 2 onto the node where pod 1 landed.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: trainer-2
  labels:
    job: my-training-run #!
spec:
  affinity:
    podAffinity: #!
      requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            job: my-training-run #!
        topologyKey: kubernetes.io/hostname   # same node as the other trainers #!
  containers:
  - name: trainer
    image: my-company/trainer:2.0
    resources:
      limits:
        nvidia.com/gpu: 1
```

  - **The failure point:** it only works if pod 1 finds a home first, and writing these rules for 100 pods is a management nightmare. A custom scheduler does it automatically for the whole batch.
- **Newer Kubernetes (alpha):** since 1.36 you can put a topology rule on a PodGroup, such as "all these pods in one rack", and the scheduler keeps the whole group inside one such domain. If no domain can hold the whole group, none of the pods start. It is still alpha in 1.37 and off by default (the `TopologyAwareWorkloadScheduling` feature gate). A separate 1.37 alpha adds several levels, for example one zone for the whole job and one rack for each part. It builds on PodGroups, so the EKS caveat in the [gang scheduling note](#d02-2) applies here too.
- **Best at this:** the KAI Scheduler, NVIDIA's open-source scheduler with topology-aware placement for GPU fabrics.

**Going deeper, by topic.**

- **Placement depends on how the model is split:**
  - **Tensor parallelism** splits every layer and exchanges results constantly. Keep it inside one node on NVLink/NVSwitch.
  - **Pipeline parallelism** splits by layers and only hands activations across once per stage, so it tolerates the slower links between nodes.
  - **Hybrid:** tensor parallelism inside nodes and pipeline parallelism between them is the common setup.
- **Inside one server too:** not every GPU pair may share the same NVLink group. `nvidia-smi topo -m` shows it, and a job may rightly wait in the queue rather than start on badly connected GPUs.

### Queueing and fair sharing

**Scenario.** In a shared research cluster, one user submits 100 training jobs on Monday morning and every GPU is taken. Other teams' jobs fail immediately with quota errors instead of waiting their turn, and over the weekend the GPUs reserved for one team sit idle while another team is desperate for capacity.

![Queueing and fair sharing](diagrams/sections/d02-4.svg)

**What the picture shows.** Queueing and fair sharing, and where namespace quotas fall short.

- **The problem:** one greedy user can lock everyone else out of a shared cluster.
- **Priority queues:** queueing systems such as Kueue (a queue controller in front of the scheduler, not a scheduler itself) let you define resource flavors and workload priorities, so jobs wait in line instead of failing.
- **Preemption:** a high-priority inference pod serving a live model can evict a low-priority research training pod when GPUs run short. The default scheduler can already do this with PriorityClasses; what the custom tools add is preemption that understands quotas and whole jobs.
- **Fair sharing:** when the cluster is idle one team can use all of it, but as soon as another team submits work, the first team's over-quota pods are preempted to give the second team its fair share.
- **Can the standard scheduler do it?** Only with static quotas.
  - **The workaround:** a `ResourceQuota` per namespace, for example 10 GPUs for the marketing team.

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: gpu-quota
  namespace: marketing
spec:
  hard:
    requests.nvidia.com/gpu: "10"   # static: idle GPUs can't be lent to others #!
```

  - **The failure point:** these quotas are static. If marketing isn't using its 10 GPUs, no one else can borrow them. And a team that hits its limit gets an immediate error; the pod doesn't queue and start later when space opens up.
- **Best at this:** Apache YuniKorn and Run:ai (via KAI), and Kueue, covered below.

**Going deeper, by topic.**

- **ClusterQueues:** hold each team's nominal quota and borrowing limit.
- **LocalQueues:** what teams submit to.
- **Cohorts:** lend idle quota between teams and take it back by preemption when its owner needs it.
- **Order and fairness:** WorkloadPriorityClasses decide admission order, and fair sharing favors teams that have used less recently.
- **Why not ResourceQuotas:** hard namespace ResourceQuotas can't do any of this. Idle GPUs in one namespace never help another team's queue.

Three defaults decide how this behaves:

- **Queue order:** a ClusterQueue's queueing strategy is `BestEffortFIFO` unless you change it. A workload that cannot be admitted does not hold up newer ones that fit. With `StrictFIFO`, an older workload that cannot be admitted blocks everything behind it ([Kueue docs](https://kueue.sigs.k8s.io/v0.20/docs/concepts/cluster_queue/)).
- **Borrowing:** by default, queues in a cohort borrow first come, first served. Fair sharing is switched on in Kueue's own configuration, and [Design 17](#design-gpu-scheduling) shows how. A queue also gets its quota back by preemption only if `reclaimWithinCohort` is set. The default is `Never`, and the example below sets it ([preemption docs](https://kueue.sigs.k8s.io/docs/concepts/preemption/)).
- **Priority:** a WorkloadPriorityClass sets the workload's place in Kueue's queue and in its preemption decisions. It does not change the pods' own priority. If a job names both a WorkloadPriorityClass and a PriorityClass, each applies to its own part ([Kueue docs](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/)).

> **Note: counting DRA devices in Kueue quotas**
>
> A Kueue quota counts only what it is told to count. A pod that asks for GPUs the old way (`nvidia.com/gpu: 2` in its limits) is counted under that name. A pod that asks through DRA writes no GPU count at all, so a quota on `nvidia.com/gpu` has nothing to count unless Kueue is set up to account for DRA devices. The [Kueue DRA docs](https://kueue.sigs.k8s.io/docs/concepts/dynamic_resource_allocation/) describe two ways to do that:
>
> - **Claim templates:** the pod points at a ResourceClaimTemplate. In its configuration you map each DeviceClass to a quota name (`deviceClassMappings`), and Kueue charges the template's `count` against it. This is beta since Kueue v0.18. Only templates work: a hand-written ResourceClaim in the pod spec is not supported and leaves the workload inadmissible. Only exact counts work, not a request for "all" devices.
> - **Extended-resource requests:** the pod keeps asking for `nvidia.com/gpu: 1`, and the DeviceClass sets `extendedResourceName` so the scheduler creates the claim for it. Kueue sees this and counts the device once, under that name. This is beta since Kueue v0.19 and on by default there. NVIDIA's `gpu.nvidia.com` class does not set `extendedResourceName` to `nvidia.com/gpu`, so for this path you add a class that does ([NVIDIA docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/dra-intro-install.html)).
>
> Don't configure the same class in both ways, or Kueue merges the two and the count can go wrong. For partitioned GPUs, Kueue can charge by a counter the driver publishes (for example GPU memory) instead of by device count, so a small slice isn't charged like a whole GPU (beta since v0.19). The Kueue docs name MIG as the example.
>
> Two limits to know. Quota is checked before the scheduler picks a device, so a workload can pass quota and still wait for a device. `waitForPodsReady` (see [Design 17](#design-gpu-scheduling)) evicts workloads that never become ready. And a mixed cluster that keeps full-GPU training on `nvidia.com/gpu` counts while sending MIG slices through DRA needs both quota paths to line up, or the slices may not be counted.

### Hardware-aware scheduling

**Scenario.** A company buys a new rack of Blackwell GPUs connected by InfiniBand. Its inference team needs FP4 support, and its training team needs all workers on the same InfiniBand switch. The platform team wonders whether node labels are enough, or whether they need a custom scheduler.

![Hardware-aware scheduling](diagrams/sections/d02-5.svg)

**What the picture shows.** Hardware-aware scheduling for new GPU generations.

- **What custom schedulers can look for:** hardware features Kubernetes doesn't track well on its own.
  - **RDMA / InfiniBand:** placing pods on nodes that share the same high-speed InfiniBand switch.
  - **Low-precision support:** sending a workload only to GPUs that support the number formats it needs, such as FP8 (Ada and Hopper and newer) or FP4 (Blackwell).
- **Can the standard scheduler do it?** Yes, to a point.
  - **The workaround:** label nodes by hand (`gpu-gen=blackwell`, `network=infiniband`) and add matching `nodeSelector`s to the AI pods, together with taints and tolerations.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: fp4-inference
spec:
  nodeSelector:
    gpu-gen: blackwell #!
    network: infiniband #!
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: server
    image: my-company/llm-server:1.0
    resources:
      limits:
        nvidia.com/gpu: 1
```

  - **The failure point:** this actually works well, and it's how most teams start. A custom scheduler only becomes necessary with thousands of nodes, when hand-managing labels for every hardware variation (FP8, HBM3, NVLink v4 and so on) becomes impractical.
- **Leading example:** NVIDIA's KAI Scheduler, aimed at rack-scale NVLink systems (GB200 NVL72 today, Vera Rubin racks next), where a whole rack behaves like one giant GPU.

**Going deeper, by topic.**

- **Labels from GPU Feature Discovery:** NVIDIA's GPU Feature Discovery labels every node with the GPU product (e.g. `A100-SXM4-40GB`), memory, architecture family, driver and CUDA versions, MIG capability, and the number of shared replicas.
- **ResourceFlavors in Kueue:** Kueue turns these labels into **ResourceFlavors**: the premium flavor (NVLink + InfiniBand) is listed first, with a fallback flavor and `TryNextFlavor`.
- **Two separate decisions:** which hardware a job gets (flavor) and who goes first (priority) stay separate.

### Volcano vs. Kueue

**Scenario.** The platform team must pick a batch scheduler for a shared GPU cluster. One group runs PyTorch jobs with a master pod and 100 workers; another group is a set of departments that share 128 GPUs and want to borrow each other's idle capacity. Should they choose Volcano or Kueue?

![Volcano vs. Kueue](diagrams/sections/d02-6.svg)

**What the picture shows.** A side-by-side of the two most common choices, with a scenario for each.

- **Volcano, the batch specialist:** a full standalone batch scheduling system on Kubernetes, donated by Huawei and now a CNCF incubating project. Best for long training jobs, big data (Spark, Flink) and HPC. Its key feature is **gang scheduling**: it treats a group of pods as one job and starts none until there's room for all. You submit a Volcano `Job` custom resource (often called a vcjob) instead of individual pods.

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: pytorch-train
spec:
  schedulerName: volcano #!
  minAvailable: 9            # gang: start nothing until all 9 pods fit #!
  tasks:
  - name: master
    replicas: 1
    template:
      spec:
        restartPolicy: OnFailure
        containers:
        - name: master
          image: my-company/trainer:2.0
  - name: worker
    replicas: 8
    template:
      spec:
        restartPolicy: OnFailure
        containers:
        - name: worker
          image: my-company/trainer:2.0
          resources:
            limits:
              nvidia.com/gpu: 1
```

- **Kueue, the resource quota manager:** a newer, cloud-native job queueing controller. Instead of replacing the scheduler, it is a gatekeeper in front of the default one. Best for shared clusters with many teams, multi-cloud and cloud bursting. It manages a global pool (say 100 A100s), holds jobs in a waitlist (a `ClusterQueue`), and **admits** a job when its quota allows; the standard scheduler then places the pods.
- **The shared scenario:** teams A and B each need 4 GPUs and the cluster has 4. The default scheduler might give each team 2, and both jobs wait forever. Volcano or Kueue gives all 4 to team A, which finishes, and then team B gets the cluster. Utilization goes up.
- **Volcano's strength, task dependencies:** in a PyTorch job with one master and many workers, if the master fails filtering (for example a port conflict), the workers would sit idle forever. Volcano can say "start the 100 workers only if the master is placed." Kueue admits a whole job's quota as one unit (and with `waitForPodsReady` it can requeue a job whose pods don't all start), but placement is left to the default scheduler, which doesn't order the pod types within a job.
- **Kueue's strength, fair sharing and borrowing:** marketing has a 10-GPU quota, data science has 50. On Friday night marketing is idle and data science needs 60. Both teams' ClusterQueues sit in the same cohort, so Kueue sees marketing's unused quota and lends its 10 GPUs. If someone in marketing submits a job on Saturday morning, Kueue preempts the borrowed work and returns the guaranteed GPUs. Volcano focuses on the performance of each job, not on sharing a budget between departments.

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: a100
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: data-science
spec:
  cohortName: gpu-pool           # queues in one cohort can lend to each other #!
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors:
    - name: a100
      resources:
      - name: nvidia.com/gpu
        nominalQuota: 50 #!
        borrowingLimit: 10 #!
  preemption:
    reclaimWithinCohort: Any   # a lender can take its GPUs back #!
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: ds-queue
  namespace: data-science
spec:
  clusterQueue: data-science #!
---
# a Job joins the queue with one label
apiVersion: batch/v1
kind: Job
metadata:
  name: fine-tune
  namespace: data-science
  labels:
    kueue.x-k8s.io/queue-name: ds-queue #!
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: train
        image: my-company/trainer:2.0
        resources:
          limits:
            nvidia.com/gpu: 4
```

**Going deeper, by topic.**

- **Usually both layers:** the production answer is usually "both layers": Kueue for admission and quota on top of a gang-aware or topology-aware scheduler (kube-scheduler with plugins, KAI or Volcano), with a workload API that groups the pods (PyTorchJob/TrainJob, JobSet, LeaderWorkerSet).
- **Why Kueue on top:** Kueue stays vendor-neutral and keeps policy separate from mechanism.
- **When a replacement scheduler pays off:** a replacement scheduler is worth it when GPU-specific features (fractional GPUs, deep topology awareness) pay for the extra moving part.
- **Fine-tuning with Kubeflow Trainer:** it shows the same layering.
  - The platform team publishes a `TrainingRuntime` (framework, image, defaults), and data scientists submit a `TrainJob`, often straight from a Python SDK.
  - The controller turns the pair into a **JobSet**, a group of Jobs that Kueue can then admit as one gang.

### The landscape

**Scenario.** A manager asks for a one-page overview of the scheduler landscape before approving a proposal. They want to know which tools exist, who maintains them, and which one fits which kind of organization.

![The landscape](diagrams/sections/d02-7.svg)

**What the picture shows.** Two comparison tables and four short "best for" notes.

- **Custom schedulers comparison (top):** NVIDIA KAI Scheduler, Volcano and Apache YuniKorn compared on five features.
  - **Gang scheduling:** KAI native (hierarchical PodGroups), Volcano native (PodGroups), YuniKorn native.
  - **GPU bin-packing:** KAI topology-aware (NVLink/PCIe), Volcano basic bin-packing, YuniKorn hierarchical bin-packing.
  - **Fair sharing:** KAI advanced (fair share and dynamic quotas), Volcano queue-based weights, YuniKorn hierarchical queues.
  - **Hardware awareness:** KAI full (Blackwell / Vera Rubin), Volcano limited (generic GPU), YuniKorn generic.
  - **Primary use case:** KAI for massive AI clusters on NVIDIA hardware, Volcano for general batch and high performance, YuniKorn for multi-tenant enterprise and Spark.
- **Market matrix (bottom):** five products and who maintains them: Volcano (CNCF incubating), Kueue (Kubernetes SIG), Armada (CNCF sandbox), NVIDIA Run:ai (commercial) and YuniKorn (Apache project). All five now cover gang scheduling, bin-packing, fair sharing and some hardware awareness, so the choice comes down to architecture and fit.
- **The four notes on the left:**
  - **Volcano:** a full replacement for the default scheduler that understands jobs, not just pods. Best when you want gang scheduling and NVLink topology awareness in one tool.
  - **Kueue:** doesn't replace the scheduler; it manages quotas and fair sharing *before* pods are created. Best for multi-tenant clusters where teams borrow from each other.
  - **Armada:** a meta-scheduler that looks across several clusters and picks the one with the best bin-packing opportunity. Best when a single cluster is too small.
  - **Run:ai:** fractional GPUs (one A100 split across small jobs) plus a central fair-share dashboard for managers. Best for large enterprises that need a polished UI and strict cost control on H100 / Blackwell clusters.

### What every scheduler relies on

**Scenario.** Whichever scheduler the team picks, someone asks: "How does the scheduler even know a node has 8 GPUs, or that two nodes share an InfiniBand switch?" Without that information, no scheduler can make a smart decision.

![What every scheduler relies on](diagrams/sections/d02-8.svg)

**What the picture shows.** The plumbing every scheduler depends on, plus a note on YuniKorn.

- **YuniKorn:** originally built to bring the scheduling power of Hadoop YARN to Kubernetes. It runs as a replacement (or shim) for the default scheduler.
- **The common foundation:** whatever scheduler you choose, you'll almost certainly run the NVIDIA GPU Operator or the NVIDIA device plugin. These components tell the scheduler what hardware is in each node.
  - **NVIDIA device plugin:** advertises `nvidia.com/gpu` to the API server, so GPUs become a countable resource.
  - **Network side:** the NVIDIA Network Operator exposes RDMA / InfiniBand devices to pods, and switch-topology labels (from tools such as NVIDIA's Topograph) tell the scheduler which nodes share a switch, so it can group pods under one.

**Going deeper, by topic.**

- **One GPU Operator installs it all:** on NVIDIA nodes, one **GPU Operator** (configured through a `ClusterPolicy`) installs and maintains everything a scheduler depends on:
  - the driver container;
  - the container toolkit, which injects `/dev/nvidia*` into containers;
  - the device plugin;
  - GPU Feature Discovery labels;
  - the MIG manager;
  - the DCGM exporter, which sends GPU utilization, memory, ECC errors, temperature and power to Prometheus.
- **The usual failure:** if the device plugin isn't running, GPU pods simply stay Pending.

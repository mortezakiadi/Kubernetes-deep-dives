---
id: design-cost-programs
title: "Design 26: Autoscaling, rightsizing and idle-capacity recovery with measurable cost results"
short: "Cost programs with measured savings"
---
This design answers one question: *how do we run autoscaling, rightsizing and idle-capacity recovery as programs whose savings are measured well enough that finance books them?* The mechanisms appear in earlier designs: serving autoscaling in [Design 21](#design-llm-serving), idle reclamation in [Design 19](#design-scheduling-impact), and the attributed cost data in [Design 25](#design-cost-capacity). This design turns them into a portfolio: sizing each opportunity in dollars, choosing the levers, building them (KEDA, Karpenter, Kueue provisioning, recommenders), protecting service levels, and measuring each saving against a counterfactual in a ledger that finance trusts.

The short version: *size from the warehouse, measure against a counterfactual, and book only what holds*. Every initiative starts with a dollar estimate from attributed cost, a named owner and a measurement method chosen before the change. Savings are computed as what the same work would have cost without the change (from unit costs such as GPU-hours per million tokens) minus what it did cost, so growth in demand doesn't hide savings and a drop in demand doesn't inflate them. Initiatives run behind service-level guardrails, and savings during a guardrail breach aren't booked. A monthly ledger reports projected, realized and sustained savings, and flags initiatives whose savings decay.

### The use case and requirements

**Scenario.** The fleet spends $4.08 million a month ([Design 25](#design-cost-capacity)). Leadership asks for a 15% reduction within two quarters without slowing research. A previous "cost push" claimed $900,000 a month of savings. Finance could confirm about $200,000 on the invoices, because most of the claims were requests reduced on paper, nodes stopped for a week and then restarted, and lower spend in a month when demand happened to fall.

![$900k claimed, $200k confirmed](diagrams/designs/concepts/ds26-c1.svg)

**What the program must deliver.**

- **A sized backlog:** every opportunity estimated in dollars per month from attributed cost, with an owner.
- **Measurement chosen up front:** each initiative has a baseline, a counterfactual method and the metric it's judged on, agreed before any change.
- **Levers built as platform features:** autoscaling for serving and GPU node pools, queue-driven provisioning, scale-to-zero for development, rightsizing recommendations, idle and orphan recovery.
- **Service levels protected:** each initiative names the SLOs it might affect, and breaches stop it.
- **A ledger finance accepts:** projected, realized and sustained savings per initiative, reconciled with spend.
- **Savings that last:** sustained savings are re-measured monthly, and decay is visible.

**Out of scope:** scheduling policy ([Design 17](#design-gpu-scheduling)), serving engine tuning ([Design 21](#design-llm-serving)) and commercial negotiations with providers.

### Sizing the opportunity

**Scenario.** The team started with the idea everyone liked best, a custom autoscaler for training jobs. Sizing later showed that idle capacity and serving replicas left running overnight were each worth five times more.

![Opportunity sizing](diagrams/designs/concepts/ds26-c2.svg)

![Sizing the opportunity](diagrams/designs/ds26-1.svg)

**What the picture shows.** A waste taxonomy, sized in dollars per month from the attributed-cost marts.

- **Idle capacity:** healthy GPU-hours nobody held (the platform's idle line in [Design 25](#design-cost-capacity)).
- **Held but idle:** GPU-hours allocated to workloads with no activity: notebooks, forgotten jobs, oversized reservations.
- **Serving over-provisioning:** replica-hours beyond what traffic needed at the SLO, especially overnight and on weekends.
- **Oversized hardware:** workloads on larger or more GPUs than they use (small models on full H100s, jobs that never exceed half their GPU memory).
- **Expensive capacity at the wrong time:** on-demand burst while committed or on-premises capacity sat idle.
- **Unused commitments and orphans:** committed hours with nothing scheduled, and resources nobody owns (stopped instances still billed, detached volumes, forgotten reservations).
- **Addressable share:** each category gets a realistic recoverable share (not all idle capacity can be recovered), giving the backlog's expected value.

**The code.** Sizing the waste categories for a month from the attributed-cost marts (warehouse SQL).

```sql
WITH m AS (SELECT DATE '2026-10-01' AS month_start, DATE '2026-11-01' AS month_end)
SELECT 'idle capacity' AS category, SUM(cost) AS usd
FROM mart_attributed_cost, m WHERE team_id = 'platform:idle' AND hour_ts >= month_start AND hour_ts < month_end
UNION ALL
SELECT 'held but idle', SUM(g.allocated_frac - g.active_frac) * MAX(r.cost_per_gpu_h)            -- #!
FROM fact_gpu_hour g JOIN mart_pool_rate r ON r.pool_id = g.pool_id AND r.month = DATE_TRUNC('month', g.hour_ts), m
WHERE g.workload_uid IS NOT NULL AND g.hour_ts >= month_start AND g.hour_ts < month_end
UNION ALL
SELECT 'serving over-provisioning', SUM(cost * (1 - needed_replicas / NULLIF(running_replicas, 0)))
FROM mart_serving_hourly, m WHERE hour_ts >= month_start AND hour_ts < month_end
UNION ALL
SELECT 'on-demand while base idle', SUM(od.cost)
FROM mart_attributed_cost od JOIN mart_pool_idle_hourly base
  ON base.hour_ts = od.hour_ts AND base.gpu_product = od.gpu_product AND base.idle_gpus >= 8, m
WHERE od.pool_kind = 'on-demand' AND od.hour_ts >= month_start AND od.hour_ts < month_end;
```

**Going deeper.**

- **Size before you build:** the backlog is ordered by expected dollars recovered per week of engineering, not by how interesting the lever is.
- **Overlaps are real:** recovering held-but-idle hours turns them into idle capacity, which saves money only if capacity then shrinks or absorbs demand that would have bought more. The ledger counts each dollar once.

### The savings ledger and how savings are measured

**Scenario.** One team reported savings as "requests reduced × list price". Another reported "this month's bill minus last month's". In a month when demand grew 12%, the second method showed a cost *increase* despite real efficiency gains, and the first claimed savings on GPUs that had never been paid for.

![Counterfactual ledger](diagrams/designs/concepts/ds26-c3.svg)

![The savings ledger and how savings are measured](diagrams/designs/ds26-2.svg)

**What the picture shows.** How each initiative's savings are defined, measured and booked.

- **Baseline period:** usage and unit cost before the change (for example, four weeks), from the warehouse.
- **Counterfactual:** what the current period's work would have cost at baseline efficiency: current output (tokens served, GPU-hours of demand, jobs run) × baseline unit cost (GPU-hours per output) × current price.
- **Realized saving:** counterfactual cost minus actual cost, per month.
- **Cash vs avoided cost:** savings that reduce the invoice (fewer on-demand hours, cancelled reservations) are *cash*. Savings that absorb growth without buying more are *avoided cost*, recognized only when a planned purchase is actually deferred.
- **States:** proposed (sized), committed (measurement agreed), realized (measured for a month), sustained (holding after three months), decayed (fallen below half).
- **Finance sign-off:** cash savings reconcile with invoice changes, and avoided cost needs a deferred purchase order or capacity plan line.

**The code.** A ledger entry measured against a unit-cost counterfactual (Python).

```python
from dataclasses import dataclass

@dataclass
class Initiative:
    name: str
    owner: str
    kind: str                         # "cash" or "avoided"
    baseline_gpu_h_per_unit: float    # e.g. GPU-hours per million tokens, measured before the change
    unit: str

def realized(i: Initiative, month_output_units: float, month_gpu_h: float, price_per_gpu_h: float) -> dict:
    counterfactual_gpu_h = month_output_units * i.baseline_gpu_h_per_unit           # same work, old efficiency #!
    saved_gpu_h = counterfactual_gpu_h - month_gpu_h
    return {"initiative": i.name, "kind": i.kind,
            "counterfactual_usd": round(counterfactual_gpu_h * price_per_gpu_h),
            "actual_usd": round(month_gpu_h * price_per_gpu_h),
            "realized_usd": round(saved_gpu_h * price_per_gpu_h),
            "efficiency_gain_pct": round(100 * saved_gpu_h / counterfactual_gpu_h, 1)}

serving = Initiative("serving autoscaling: chat-70b", "serving-platform", "cash", 0.92, "M tokens")
print(realized(serving, month_output_units=118_000, month_gpu_h=71_500, price_per_gpu_h=2.50))
```

**Going deeper.**

- **Pick the output unit carefully:** tokens served, training steps, jobs completed or GPU-hours of admitted demand. It must measure the work the user cares about, or an "efficiency gain" can be less work done.
- **Prices change separately:** a provider price cut isn't an initiative's saving. The counterfactual uses current prices, so price changes don't leak into the ledger.

### Serving autoscaling: following traffic

**Scenario.** The chat deployments ran 32 replicas around the clock, sized for the weekday peak. At 03:00 they served 15% of peak traffic. Nobody had changed the replica count since launch, because the last attempt to autoscale scaled on GPU utilization and never scaled down.

![Serving autoscaling](diagrams/designs/concepts/ds26-c4.svg)

![Serving autoscaling: following traffic](diagrams/designs/ds26-3.svg)

**What the picture shows.** Replica counts that follow load, scaled on signals that track latency.

- **Scaling signals:** waiting requests and KV-cache usage per replica, from the engine's metrics ([Design 21](#design-llm-serving)), not GPU utilization.
- **KEDA ScaledObject:** reads the signals from Prometheus and drives the HPA, with a minimum for burst headroom and a maximum for cost control.
- **Asymmetric behavior:** scale up fast (a burst can't wait), scale down slowly (avoid oscillation and repeated model loads).
- **Fast readiness makes it pay:** replicas that load weights in under two minutes ([Design 20](#design-distributed-training)) let the minimum stay low.
- **Where freed GPUs go:** on cloud capacity, node autoscaling returns them. On committed or on-premises capacity, a preemptible batch queue absorbs them, otherwise nothing is saved.

**The YAML.** A KEDA ScaledObject for a chat deployment, scaling on queued requests per replica.

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: { name: chat-70b, namespace: serving }
spec:
  scaleTargetRef: { name: chat-70b }
  minReplicaCount: 6                                   # burst headroom; was a fixed 32
  maxReplicaCount: 40
  cooldownPeriod: 900
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:   { stabilizationWindowSeconds: 0,   policies: [{ type: Percent, value: 50, periodSeconds: 60 }] }
        scaleDown: { stabilizationWindowSeconds: 900, policies: [{ type: Pods,    value: 2,  periodSeconds: 300 }] }
  triggers:
  - type: prometheus
    metadata:
      serverAddress: http://prometheus.monitoring:9090
      query: sum(vllm:num_requests_waiting{namespace="serving", model_name="chat-70b"})
      threshold: "4"                                    # target waiting requests per replica #!
  - type: prometheus
    metadata:
      serverAddress: http://prometheus.monitoring:9090
      query: avg(vllm:kv_cache_usage_perc{namespace="serving", model_name="chat-70b"}) * 100
      threshold: "75"
```

**Going deeper.**

- **Measure with GPU-hours per million tokens:** the ledger entry for autoscaling compares this unit cost before and after, at current traffic.
- **Test scale-up under replayed bursts:** the benchmark replay of [Design 22](#design-serving-benchmarks) at 2× speed shows whether the minimum and the scale-up policy hold the TTFT SLO.

### GPU node autoscaling and consolidation in the cloud

**Scenario.** Cloud GPU node pools were sized by hand for the busiest week of the quarter. On an average day, 30% of on-demand GPU nodes ran nothing but DaemonSets, and the few that ran one small pod each couldn't be removed.

![Node autoscaling layers](diagrams/designs/concepts/ds26-c5.svg)

![GPU node autoscaling and consolidation in the cloud](diagrams/designs/ds26-4.svg)

**What the picture shows.** Node capacity that follows pending pods, and consolidation that empties underused nodes.

- **Provision on demand:** Karpenter (or the cluster autoscaler) adds GPU nodes when pods are pending, choosing instance types from the NodePool's allowed list.
- **Consolidate:** nodes that are empty, or whose pods fit elsewhere, are drained and removed after a delay. Disruption budgets limit how many go at once.
- **Layers stay separate:** committed and on-premises nodes are never scaled down (they're paid for anyway). Only on-demand pools scale, and they fill only after the base is full.
- **Protect long jobs:** training pods carry a do-not-disrupt annotation, so consolidation never interrupts a gang. Serving and batch pods are disruptable.
- **Limits:** a NodePool's GPU limit caps spend if something misbehaves.

**The YAML.** An on-demand GPU NodePool with consolidation and a spend cap (Karpenter v1).

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata: { name: gpu-on-demand }
spec:
  template:
    metadata: { labels: { pool-kind: on-demand } }
    spec:
      nodeClassRef: { group: karpenter.k8s.aws, kind: EC2NodeClass, name: gpu }
      requirements:
      - { key: karpenter.sh/capacity-type, operator: In, values: ["on-demand"] }
      - { key: node.kubernetes.io/instance-type, operator: In, values: ["p5.48xlarge", "g6e.12xlarge", "g6e.48xlarge"] }
      taints: [{ key: nvidia.com/gpu, effect: NoSchedule }]
      expireAfter: 720h
  limits:
    nvidia.com/gpu: "256"                                # spend cap for the pool #!
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 10m
    budgets:
    - nodes: "10%"
    - nodes: "0"
      schedule: "0 9 * * mon-fri"                        # no voluntary disruption during the weekday peak
      duration: 8h
```

**Going deeper.**

- **Prefer the base first:** a scheduling preference (or Kueue flavor order, [Design 17](#design-gpu-scheduling)) places work on committed and on-premises capacity before on-demand. Otherwise autoscaling adds expensive nodes while cheap ones sit idle.
- **Watch for churn:** nodes that are added and removed within an hour cost startup time and money. A consolidation delay of minutes, not seconds, avoids it.

### Batch and development: provision for the queue, scale to zero

**Scenario.** Each research team kept a "dev pool" of 4 GPU nodes always on, so that notebooks started instantly. Across 14 teams, 56 nodes averaged 11% activity. Batch jobs, meanwhile, held on-demand nodes while they waited for their full gang to be placed.

![Dev and batch](diagrams/designs/concepts/ds26-c6.svg)

![Batch and development: provision for the queue, scale to zero](diagrams/designs/ds26-5.svg)

**What the picture shows.** Capacity created for work that's admitted, and released when it's gone.

- **Queue-driven provisioning:** Kueue asks the autoscaler for the whole gang's capacity (a ProvisioningRequest) before admitting a workload. Nodes appear for a job that will start, and no partial gang holds on-demand nodes while waiting.
- **Scale to zero for development:** notebooks and dev environments run on a pool with a minimum of zero, with a warm buffer of one node per GPU type during working hours.
- **Idle stop:** notebooks are checkpointed and stopped after an idle period ([Design 19](#design-scheduling-impact)'s idle policy), and their node is consolidated away.
- **Start-time budget:** the trade-off is explicit. A notebook may take 3 to 5 minutes to start from zero, and the program publishes that number.
- **Shared, not per team:** one dev pool with fair sharing replaces 14 team pools.

**The YAML.** Kueue admission that provisions capacity for the whole workload before admitting it (ProvisioningRequest admission check).

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ProvisioningRequestConfig
metadata: { name: gpu-gang-capacity }
spec:
  provisioningClassName: check-capacity.autoscaling.x-k8s.io   # all-or-nothing capacity for the gang #!
  managedResources: ["nvidia.com/gpu"]
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata: { name: gpu-gang-capacity }
spec:
  controllerName: kueue.x-k8s.io/provisioning-request
  parameters: { apiGroup: kueue.x-k8s.io, kind: ProvisioningRequestConfig, name: gpu-gang-capacity }
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: { name: batch-on-demand }
spec:
  admissionChecksStrategy:
    admissionChecks: [{ name: gpu-gang-capacity, onFlavors: [h100-on-demand] }]
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu"]
    flavors: [{ name: h100-on-demand, resources: [{ name: nvidia.com/gpu, nominalQuota: 256 }] }]
```

**Going deeper.**

- **Development savings are the easiest to book:** always-on dev nodes are cash, and their removal shows on the next invoice.
- **Keep the experience good:** a published start time, a warm buffer during working hours and saved notebook state make scale-to-zero acceptable to users.

### Rightsizing GPU workloads

**Scenario.** A team's inference service for a 7B model ran on full H100s, using 18 GB of 80 GB and 9% of compute. Elsewhere, a 4-GPU training pod requested 96 CPUs on 8-GPU nodes with 112 CPUs, so three of the node's four remaining GPUs couldn't be used by anyone.

![Rightsizing workloads](diagrams/designs/concepts/ds26-c7.svg)

![Rightsizing GPU workloads](diagrams/designs/ds26-6.svg)

**What the picture shows.** Recommendations from observed usage, by type of mismatch.

- **GPU count:** jobs whose GPUs are mostly idle or whose memory never exceeds a fraction of the allocation, recommended to request fewer.
- **GPU type:** small-model inference and light workloads recommended to smaller GPUs (for example, L40S instead of H100) or MIG slices, where latency allows.
- **Stranding by CPU and memory:** pods whose CPU or memory requests leave a node's other GPUs unschedulable, with the number of GPUs stranded and their cost.
- **Evidence and savings with each:** the recommendation shows the usage behind it, the projected monthly saving and the risk (for example, "peak memory 41 GB on a 48 GB card").
- **Delivery:** recommendations go to owners as tickets and pull requests against their manifests. The platform doesn't change workloads silently.

**The code.** A rightsizing recommender over a workload's usage profile (Python).

```python
GPU_TYPES = {  # memory GB, price per GPU-hour (illustrative)
    "H100-80GB": (80, 2.50), "L40S-48GB": (48, 1.10), "H100-MIG-3g.40gb": (40, 1.05)}

def recommend(w: dict, headroom: float = 1.25) -> dict | None:
    """w: kind, gpu_type, gpus, p99_mem_gb, p95_activity, hours_per_month, cpu_req, node_cpus, node_gpus."""
    current = GPU_TYPES[w["gpu_type"]][1] * w["gpus"] * w["hours_per_month"]
    need_mem = w["p99_mem_gb"] * headroom
    fits = [(name, mem, price) for name, (mem, price) in GPU_TYPES.items() if mem >= need_mem]
    if w["kind"] == "inference" and w["p95_activity"] < 0.3 and fits:
        name, _, price = min(fits, key=lambda f: f[2])
        new = price * w["gpus"] * w["hours_per_month"]
        if new < 0.8 * current:
            return {"action": f"move to {name}", "saving_usd_month": round(current - new), "evidence": f"p99 memory {w['p99_mem_gb']} GB, p95 activity {w['p95_activity']:.0%}"}
    cpu_per_gpu = w["node_cpus"] / w["node_gpus"]
    stranded = max(0, w["node_gpus"] - w["gpus"] - int((w["node_cpus"] - w["cpu_req"]) // cpu_per_gpu))
    if stranded:                                                                     # GPUs nobody else can use #!
        return {"action": f"lower CPU request to ≤ {int(cpu_per_gpu * w['gpus'])}", "stranded_gpus": stranded,
                "saving_usd_month": round(stranded * GPU_TYPES[w["gpu_type"]][1] * w["hours_per_month"])}
    return None

print(recommend({"kind": "inference", "gpu_type": "H100-80GB", "gpus": 2, "p99_mem_gb": 18, "p95_activity": 0.09,
                 "hours_per_month": 730, "cpu_req": 16, "node_cpus": 112, "node_gpus": 8}))
```

**Going deeper.**

- **Inference latency decides type changes:** a smaller GPU may meet throughput but miss TTFT. Rightsizing serving goes through the benchmark gate ([Design 22](#design-serving-benchmarks)).
- **CPU requests are the silent GPU waste:** stranded GPUs often cost more than oversized GPU requests. Admission policies can cap CPU and memory per GPU on GPU nodes.

### Rightsizing the serving fleet

**Scenario.** Twenty-two fine-tuned 8B models each had a dedicated 2-replica deployment, most serving a few requests a minute. Together they held 44 H100s and averaged 4% activity.

![Serving fleet](diagrams/designs/concepts/ds26-c8.svg)

![Rightsizing the serving fleet](diagrams/designs/ds26-7.svg)

**What the picture shows.** Fleet-level changes that reduce GPUs per unit of traffic.

- **Consolidate fine-tunes:** fine-tunes of the same base model served as LoRA adapters on shared base-model replicas, instead of one deployment each.
- **Scale idle models to zero:** low-traffic models with relaxed latency targets scale to zero and load on demand, with the cold-start time published.
- **Quantize where the gate passes:** FP8 or FP4 variants ([Design 21](#design-llm-serving)) serve more traffic per GPU, after the accuracy gate.
- **Right-size parallelism:** smaller tensor parallelism and more replicas where it raises goodput.
- **Retire unused models:** deployments with no traffic for 30 days are flagged to owners and removed if unclaimed.

**The code.** Comparing dedicated deployments with shared adapter serving for a set of fine-tunes (Python).

```python
import math

def adapter_consolidation(models: list[dict], replica_capacity_rps: float, price_gpu_h: float,
                          gpus_per_replica: int = 1, min_replicas: int = 2) -> dict:
    """models: name, peak_rps, dedicated_replicas. Shared replicas serve all adapters of one base model."""
    dedicated_gpus = sum(m["dedicated_replicas"] for m in models) * gpus_per_replica
    peak_total = sum(m["peak_rps"] for m in models) * 1.3                       # adapter overhead + headroom #!
    shared_replicas = max(min_replicas, math.ceil(peak_total / replica_capacity_rps))
    shared_gpus = shared_replicas * gpus_per_replica
    return {"dedicated_gpus": dedicated_gpus, "shared_gpus": shared_gpus,
            "saving_usd_month": round((dedicated_gpus - shared_gpus) * price_gpu_h * 730)}

fine_tunes = [{"name": f"ft-{i}", "peak_rps": 0.4 + (i % 5) * 0.3, "dedicated_replicas": 2} for i in range(22)]
print(adapter_consolidation(fine_tunes, replica_capacity_rps=6.0, price_gpu_h=2.50))
```

**Going deeper.**

- **Adapters change the latency profile:** many adapters on one replica share its batch. Benchmark with the real adapter mix and traffic before migrating.
- **Retirement needs an owner answer:** a removal ticket that times out without a reply removes the deployment, and the model stays in the registry for redeployment.

### Recovering idle capacity, commitments and orphans

**Scenario.** The hyperscaler commitment ran at 71% utilization, so 29% of committed hours were paid for and unused. At the same time, an evaluation backlog of 40,000 GPU-hours waited for "free capacity". Separately, an audit found $58,000 a month of resources with no owner: stopped instances with attached storage, detached volumes and a forgotten capacity reservation.

![Idle and orphans](diagrams/designs/concepts/ds26-c9.svg)

![Recovering idle capacity, commitments and orphans](diagrams/designs/ds26-8.svg)

**What the picture shows.** Three recovery levers for capacity that's paid for and unused.

- **Fill the base with preemptible work:** a low-priority, preemptible queue (evaluations, data processing, hyperparameter sweeps) runs on idle committed and on-premises capacity, and is preempted when higher-priority work arrives ([Design 17](#design-gpu-scheduling)). This saves money only by displacing work that would otherwise have run on paid capacity, and the ledger counts it that way.
- **Shrink what can shrink:** if idle capacity persists after filling, the capacity plan ([Design 25](#design-cost-capacity)) reduces the next commitment or defers a purchase. That's where idle recovery becomes avoided cost.
- **Orphan sweeps:** weekly queries over billing and inventory find resources without an owner or a running workload. Owners get a notice, and unclaimed resources are deleted after a grace period, with snapshots where data is at stake.
- **Commitment utilization as a metric:** each commitment's used share is reported weekly, with an alert below a threshold.

**The code.** Finding billed resources with no owner and no recent use (warehouse SQL).

```sql
SELECT c.provider, c.resource_id, c.sku, SUM(c.effective_cost) AS usd_30d,
       MAX(i.last_attached_at) AS last_used, MAX(i.owner_team) AS owner
FROM fact_cost_line c
LEFT JOIN dim_cloud_inventory i ON i.resource_id = c.resource_id
WHERE c.charge_period_start >= CURRENT_DATE - INTERVAL 30 DAY
  AND c.charge_category = 'Usage'
  AND (i.owner_team IS NULL OR i.last_attached_at < CURRENT_DATE - INTERVAL 14 DAY)   -- no owner or idle 14 days #!
  AND NOT EXISTS (SELECT 1 FROM fact_gpu_hour g
                  WHERE g.node = i.node_name AND g.hour_ts >= CURRENT_DATE - INTERVAL 14 DAY AND g.allocated_frac > 0)
GROUP BY 1, 2, 3
HAVING SUM(c.effective_cost) > 100
ORDER BY usd_30d DESC;
```

**Going deeper.**

- **Filling idle capacity isn't automatically a saving:** if the evaluation backlog would otherwise never have run, filling it adds value but not savings. If it would have run on on-demand capacity, it's a cash saving. The ledger asks which.
- **Deletions are irreversible:** orphan cleanup snapshots storage first, waits for the grace period, and logs every deletion with the evidence that the resource was orphaned.

### Guardrails: savings that don't cost service

**Scenario.** A scale-down change on the coding assistant saved $40,000 in its first month. It also raised p95 TTFT above the SLO for two hours every weekday morning, while new replicas loaded. Users noticed before the dashboards did.

![Guardrails](diagrams/designs/concepts/ds26-c10.svg)

![Guardrails: savings that don't cost service](diagrams/designs/ds26-9.svg)

**What the picture shows.** The service levels each initiative must hold, and what happens when it doesn't.

- **Guardrails per initiative:** serving autoscaling holds TTFT and ITL SLOs. Queue-driven provisioning and dev scale-to-zero hold queue wait and start-time targets. Rightsizing holds the benchmark and accuracy gates.
- **Measured continuously:** the same dashboards and alerts as production ([Design 24](#design-gpu-observability)), labeled with the initiative that changed the configuration.
- **Breach rules:** a breach pauses the initiative's latest change (revert in Git). Savings from periods with breaches aren't booked.
- **Trade-offs written down:** where a guardrail is deliberately relaxed (a longer notebook start time), the new target is agreed with users and recorded with the initiative.

**The code.** Booking only savings from hours when the initiative's guardrails held (Python).

```python
import pandas as pd

def bookable_savings(hourly: pd.DataFrame, guardrails: dict[str, float]) -> dict:
    """hourly: hour, saved_usd, and one column per guardrail metric (e.g. ttft_p95_s, queue_wait_p90_min)."""
    ok = pd.Series(True, index=hourly.index)
    for metric, limit in guardrails.items():
        ok &= hourly[metric] <= limit
    days_with_breach = hourly.loc[~ok, "hour"].dt.date.unique()
    bookable = hourly[~hourly.hour.dt.date.isin(days_with_breach)]                  # breach voids the whole day #!
    return {"claimed_usd": round(hourly.saved_usd.sum()), "bookable_usd": round(bookable.saved_usd.sum()),
            "breach_days": len(days_with_breach)}
```

**Going deeper.**

- **Void the day, not the hour:** voiding only breach hours would reward an initiative for being cheap in the hours around the breach that caused it.
- **Users are a guardrail too:** a ticket volume or satisfaction signal catches problems that metrics miss, such as a slower notebook start.

### Running the program and sustaining the results

**Scenario.** Six months after a previous push, half of its savings had disappeared. Minimum replica counts had been raised "temporarily" during an incident, dev pools had reappeared under new names, and nobody re-measured.

![Sustaining results](diagrams/designs/concepts/ds26-c11.svg)

![Running the program and sustaining the results](diagrams/designs/ds26-10.svg)

**What the picture shows.** The program as an ongoing operation with a cadence and owners.

- **Weekly:** backlog review ordered by expected dollars per engineering week, initiative status and guardrails.
- **Monthly:** the ledger, with projected, realized and sustained savings per initiative, reconciled with spend and reviewed with finance.
- **Sustainment checks:** each sustained saving is re-measured monthly against its counterfactual. A drop below half its peak marks it *decayed* and reopens it.
- **Config drift detection:** changes that undo an initiative (a raised minimum replica count, a new always-on pool) are flagged when their pull request is opened, with the cost impact.
- **Incentives:** team statements ([Design 25](#design-cost-capacity)) show each team's idle and oversized costs, and the savings attributed to them.

**The code.** The monthly savings report with decay detection (Python).

```python
import pandas as pd

def savings_report(ledger: pd.DataFrame) -> pd.DataFrame:
    """ledger: one row per initiative per month with projected_usd, realized_usd (bookable), kind, state."""
    peak = ledger.groupby("initiative").realized_usd.cummax()
    ledger = ledger.assign(peak_usd=peak)
    last = ledger.sort_values("month").groupby("initiative").tail(1).copy()
    last["state"] = last.apply(
        lambda r: "decayed" if r.realized_usd < 0.5 * r.peak_usd else
                  ("sustained" if (ledger[(ledger.initiative == r.initiative)].shape[0] >= 3) else "realized"), axis=1)   #!
    last["capture_vs_projection"] = (last.realized_usd / last.projected_usd).round(2)
    return last[["initiative", "kind", "projected_usd", "realized_usd", "peak_usd", "capture_vs_projection", "state"]]
```

**Going deeper.**

- **Report cash and avoided cost separately:** finance books cash savings against the budget and avoided cost against the capacity plan. Mixing them is how claims stop being believed.
- **Projections calibrate over time:** the ratio of realized to projected savings per category shows which estimates are optimistic, and the sizing uses it next quarter.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds26-c12.svg)

- **Phase 1, size and measure (weeks 1–3):** the waste taxonomy from the warehouse, the ledger with counterfactual methods, and guardrail definitions.
- **Phase 2, quick cash (weeks 3–8):** serving autoscaling on the largest deployments, dev pools consolidated with scale-to-zero, orphan sweeps, and on-demand consolidation.
- **Phase 3, structural (weeks 6–16):** queue-driven provisioning, rightsizing recommendations and stranding fixes, adapter consolidation for fine-tunes, and filling the base with preemptible work.
- **Phase 4, sustain (ongoing):** monthly ledger with finance, decay detection, drift checks in pull requests, and capacity-plan updates from recovered idle.

**Trade-offs we accepted.**

- **Slower starts:** notebooks and low-traffic models start from zero in minutes, not seconds.
- **Conservative booking:** counterfactual measurement and voided breach days report lower savings than optimistic methods, and are believed.
- **Recommendations, not automatic changes,** for workload rightsizing. Adoption depends on owners.
- **More moving parts:** autoscalers, provisioning checks and recommenders each need tuning and operating.

**Open questions.**

- **Predictive scaling:** can traffic forecasts pre-warm serving replicas before the morning ramp, removing the start-time trade-off?
- **Automatic rightsizing:** for which workload kinds is the risk low enough to apply recommendations without owner approval?
- **Carbon as a second ledger:** should the program track energy saved alongside dollars, using the same counterfactuals?
- **Market-based allocation:** would internal prices that rise with utilization reduce waste more than programs do?

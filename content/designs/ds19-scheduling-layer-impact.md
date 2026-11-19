---
id: design-scheduling-impact
title: "Design 19: Building a scheduling layer that measurably raises accelerator utilization"
short: "Proving utilization gains"
---
This design answers one question: *how do we build a scheduling layer (gang scheduling, bin-packing and fair-share quotas) and prove, with numbers that survive scrutiny, that it raised accelerator utilization across teams?* [Design 17](#design-gpu-scheduling) explains the mechanisms: Kueue admission, topology-aware gangs, bin-packing profiles, cohorts and fair sharing. This design is about delivering them as a program: defining the metric, instrumenting a baseline, building the layer as code, simulating changes before rollout, rolling out in stages, attributing gains to each change, and reporting per team so the improvement lasts.

The short version: *define utilization precisely, measure a baseline before touching anything, and ship each change as an experiment*. Utilization is measured from GPU activity (not allocation), per team and per pool, from one pipeline. Quotas live in Git and generate the Kueue objects. Every policy change is first replayed against recorded jobs in a simulator, then rolled out to one pool while a comparable pool stays unchanged, so the gain can be separated from changes in demand. A per-change attribution shows which lever moved the number, and team scorecards keep the gains from eroding.

### The use case and requirements

**Scenario.** Leadership has approved a scheduling project with a target: raise GPU utilization across the 1,024-GPU fleet from "about 58%" to 75% within two quarters, instead of buying 300 more GPUs. Three problems appear on day one. Nobody agrees on what "58%" measured. Two previous efforts claimed improvements that disappeared when demand dropped over the holidays. And team leads worry that "utilization" means their jobs will be preempted more.

![Buy 300 GPUs, or use the 1,024 better](diagrams/designs/concepts/ds19-c1.svg)

**What the program must deliver.**

- **One agreed metric:** a definition of utilization that teams, finance and the platform team accept, measured the same way every day.
- **A trustworthy baseline:** several weeks of data before any change, by team and pool.
- **The scheduling layer as code:** quotas, queues and scheduler profiles generated from versioned definitions.
- **Evidence before rollout:** simulated impact of each change on utilization and on wait times.
- **Causal attribution:** how much of the change came from the scheduler, separated from demand changes.
- **Guardrails:** utilization gains that don't come at the cost of large-job wait times, preemption losses or fairness.
- **Lasting results:** team-level reporting that keeps idle allocation from creeping back.

**Out of scope:** the mechanics of each scheduling feature ([Design 17](#design-gpu-scheduling)) and multi-cluster placement ([Design 02](#design-multicluster), [Design 18](#design-multicloud-fleet)).

### Defining utilization so it can't be argued with

**Scenario.** The platform dashboard said 58% (GPUs allocated to pods), the finance report said 71% (nodes powered on and assigned), and a team lead's notebook showed 34% (average SM activity). All three were "GPU utilization."

![One metric tree](diagrams/designs/concepts/ds19-c2.svg)

![Defining utilization so it can't be argued with](diagrams/designs/ds19-1.svg)

**What the picture shows.** A metric tree from raw capacity to useful work, with one headline number and the ones under it.

- **Capacity:** GPU-hours of healthy, schedulable GPUs. Nodes down for repair are reported separately, not hidden in the denominator.
- **Allocation rate:** the share of capacity requested by running pods (what Kueue and kube-scheduler control).
- **Activity rate:** the share of allocated GPU-hours where the GPU is actually working, measured from DCGM's graphics-engine activity (above a small threshold), not from `nvidia-smi` utilization alone.
- **Headline: active utilization** = allocation rate × activity rate: the share of capacity doing work.
- **Efficiency below that:** SM activity and tensor-core activity show how hard the GPUs work while active; MFU belongs to training benchmarks ([Design 15](#design-eval-infra)), not to the scheduler's target.
- **The scheduler's target is active utilization;** the scheduler can raise allocation directly and activity indirectly (by reclaiming idle allocations), but it can't make a slow data loader faster.

**The code.** Computing the metric tree for one day and one team from per-GPU minute samples (Python).

```python
import pandas as pd

def utilization(samples: pd.DataFrame, active_threshold: float = 0.05) -> pd.Series:
    """samples: one row per GPU per minute with columns
       healthy (bool), allocated (bool), team (str|None), gr_engine_active (0..1 from DCGM)."""
    healthy = samples[samples.healthy]
    capacity = len(healthy)                                            # GPU-minutes
    alloc = healthy[healthy.allocated]
    active = alloc[alloc.gr_engine_active >= active_threshold]
    return pd.Series({
        "capacity_gpu_h": capacity / 60,
        "allocation_rate": len(alloc) / capacity,
        "activity_rate": len(active) / max(len(alloc), 1),
        "active_utilization": len(active) / capacity,                  # the headline #!
        "mean_engine_activity_when_active": active.gr_engine_active.mean(),
        "unhealthy_gpu_h": (len(samples) - capacity) / 60,             # reported, not hidden
    })

def by_team(samples):
    return samples.groupby("team").apply(lambda g: utilization(g)).sort_values("active_utilization")
```

**Going deeper.**

- **Agree on the definition in writing before the project starts:** the metric, threshold, denominator and data source, signed off by platform, finance and team leads; changing it later resets the baseline.
- **Report the tree, not just the headline:** "allocation went up, activity went down" (jobs grabbing GPUs they don't use) is a different story from both rising.

### Baseline instrumentation

**Scenario.** The previous effort measured "before" for one week in December and "after" for one week in February. Demand had doubled in between. The improvement was mostly demand.

![Baseline before change](diagrams/designs/concepts/ds19-c3.svg)

![Baseline instrumentation](diagrams/designs/ds19-2.svg)

**What the picture shows.** The data pipeline that produces the metric, the baseline and the context needed to interpret it.

- **GPU activity:** DCGM exporter on every GPU node, with pod and namespace labels, scraped every 30 seconds.
- **Allocation:** kube-state-metrics for GPU requests of running pods; Kueue metrics for quota usage, pending workloads and admission wait times.
- **Demand context:** submitted GPU-hours per team per day, pending GPU-hours, and job size mix; without these, utilization changes can't be interpreted.
- **Recording rules and a warehouse:** per-minute aggregates by team, pool and GPU type, stored long term so the baseline can be recomputed if definitions change.
- **Baseline period:** at least four weeks, covering normal variation (weekdays, weekends, deadlines), with no scheduler changes during it.

**The YAML.** Prometheus recording rules for allocation and activity per team (Prometheus Operator).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: gpu-utilization, namespace: monitoring }
spec:
  groups:
  - name: gpu-utilization
    interval: 1m
    rules:
    - record: team:gpu_allocated:sum                       # GPUs requested by running pods (label_team from namespace labels)
      expr: |
        sum by (label_team) (
          kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
          * on (namespace, pod) group_left() (kube_pod_status_phase{phase="Running"} == 1)
          * on (namespace) group_left(team) kube_namespace_labels{label_team!=""}
        )
    - record: team:gpu_active:sum                          # allocated GPUs actually working #!
      expr: |
        sum by (label_team) (
          (DCGM_FI_PROF_GR_ENGINE_ACTIVE > bool 0.05)
          * on (namespace) group_left(team) kube_namespace_labels{label_team!=""}
        )
    - record: fleet:gpu_capacity_healthy:sum
      expr: sum(kube_node_status_allocatable{resource="nvidia_com_gpu"} * on (node) group_left() (kube_node_spec_unschedulable == bool 0))
    - record: fleet:gpu_active_utilization:ratio
      expr: sum(team:gpu_active:sum) / fleet:gpu_capacity_healthy:sum
```

**Going deeper.**

- **Label hygiene matters:** namespaces must carry a team label; unlabeled namespaces are a reported category, not silently dropped.
- **Recompute, don't edit:** store raw per-minute aggregates, so a corrected definition can be applied to the whole history, including the baseline.

### Architecture of the scheduling layer

**Scenario.** Previous changes were made by hand in the cluster: a ClusterQueue edited here, a scheduler flag there. Nobody could say which configuration was in effect on which date.

![Layer architecture](diagrams/designs/concepts/ds19-c4.svg)

![Architecture of the scheduling layer](diagrams/designs/ds19-3.svg)

**What the picture shows.** The components the project builds, and how they connect.

- **Policy source:** team allocations, weights, priorities and pool definitions in Git, reviewed like code.
- **Policy controller:** generates ResourceFlavors, Cohorts, ClusterQueues, LocalQueues and WorkloadPriorityClasses from the policy source and applies them; reports drift.
- **Admission and placement:** Kueue with gang semantics and topology-aware scheduling, kube-scheduler with a bin-packing profile for GPU pools ([Design 17](#design-gpu-scheduling)).
- **Reclaimers:** an idle reclaimer for allocations that aren't used, and a compaction controller for fragmented nodes.
- **Measurement:** the metric pipeline above, plus a change log of every policy version and when it was applied.
- **Simulator:** replays recorded jobs against candidate policies before they reach the cluster.

**Going deeper.**

- **Every policy version has a timestamp:** attribution later needs to know exactly which configuration was active when; the controller writes each applied version to the change log.
- **The layer is Kubernetes-native:** no custom scheduler fork; customization is configuration plus small controllers, so upgrades of Kueue and Kubernetes stay routine.

### Quotas as code

**Scenario.** Quotas lived in a spreadsheet and were copied into ClusterQueues by hand. After a reorganization, the quotas added up to 1,180 GPUs on a 1,024-GPU fleet, and two teams had no LocalQueue at all.

![Quotas as code](diagrams/designs/concepts/ds19-c5.svg)

![Quotas as code](diagrams/designs/ds19-4.svg)

**What the picture shows.** One reviewed file for team allocations, validated and turned into Kueue objects.

- **Allocations file:** each team's guaranteed GPUs per pool, borrowing and lending limits, fair-share weight and namespaces.
- **Validation in CI:** guaranteed quotas add up to the pool's healthy capacity (within a margin), every team has a queue, weights are positive, no namespace belongs to two teams.
- **Generation:** a small generator renders Cohorts, ClusterQueues and LocalQueues; a GitOps tool applies them.
- **Change review:** quota changes are pull requests with an owner's approval; the merge time becomes the change-log entry.

**The code.** Validating allocations and generating ClusterQueues (Python).

```python
import yaml

def validate(alloc: dict, capacity: dict, tolerance: float = 0.02):
    errors = []
    for pool, cap in capacity.items():
        total = sum(t["pools"].get(pool, {}).get("guaranteed", 0) for t in alloc["teams"].values())
        if abs(total - cap) > tolerance * cap:
            errors.append(f"{pool}: guaranteed {total} vs capacity {cap}")          # 1,180 on 1,024 #!
    seen = {}
    for team, t in alloc["teams"].items():
        for ns in t["namespaces"]:
            if ns in seen:
                errors.append(f"namespace {ns} in {seen[ns]} and {team}")
            seen[ns] = team
    return errors

def cluster_queue(team: str, t: dict, cohort: str) -> dict:
    flavors = [{"name": pool, "resources": [{"name": "nvidia.com/gpu", "nominalQuota": p["guaranteed"],
                **({"borrowingLimit": p["borrow"]} if "borrow" in p else {}),
                **({"lendingLimit": p["lend"]} if "lend" in p else {})}]}
               for pool, p in t["pools"].items()]
    return {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "ClusterQueue", "metadata": {"name": team},
            "spec": {"cohortName": cohort, "namespaceSelector": {"matchLabels": {"team": team}},
                     "fairSharing": {"weight": t.get("weight", 1)},
                     "resourceGroups": [{"coveredResources": ["nvidia.com/gpu"], "flavors": flavors}],
                     "preemption": {"reclaimWithinCohort": "Any", "withinClusterQueue": "LowerPriority"}}}

alloc = yaml.safe_load(open("allocations.yaml"))
errs = validate(alloc, {"h100-fabric": 1024})
if errs:
    raise SystemExit("\n".join(errs))
print(yaml.safe_dump_all([cluster_queue(n, t, "gpu-fleet") for n, t in alloc["teams"].items()]))
```

**Going deeper.**

- **Allocations are a business decision:** the platform team owns the mechanism, leadership owns the numbers; the file records who approved each allocation.
- **Generate LocalQueues too:** a team that has quota but no queue in a namespace is the most common "my job is stuck" ticket.

### Simulate before you ship

**Scenario.** The team argued for two weeks about whether bin-packing would hurt notebook users. A replay of the previous month's jobs answered it in an afternoon.

![Simulate before you ship](diagrams/designs/concepts/ds19-c6.svg)

![Simulate before you ship](diagrams/designs/ds19-5.svg)

**What the picture shows.** Trace-driven simulation of scheduling policies.

- **Traces:** every job from the baseline period: submit time, team, GPUs and shape, runtime, priority, preemptibility, and whether it was a notebook or batch job.
- **Simulator:** a discrete-event model of the cluster's nodes, admission (quota, gangs) and placement (spread or pack), fast enough to replay a month in minutes.
- **Candidate policies:** each change on its own (gang admission, bin-packing, fair-share weights, idle reclaim) and combined.
- **Outputs:** allocation and active utilization, wait time by job size, fragmentation, preemptions; with a range from replaying with perturbed runtimes.
- **Calibration:** replay the baseline policy and check that the simulator reproduces the measured baseline within a few points; otherwise its predictions aren't trusted.

**The code.** A minimal discrete-event replay comparing spread and pack placement for gang jobs (Python).

```python
import heapq

def simulate(jobs, nodes=128, gpus_per_node=8, policy="pack"):
    """jobs: sorted by submit; each dict has submit, runtime, pods, gpus_per_pod. Returns utilization, waits."""
    free = [gpus_per_node] * nodes
    events, queue, waits, busy_gpu_s = [], [], [], 0.0
    t_end = max(j["submit"] + j["runtime"] for j in jobs) * 3
    for j in jobs:
        heapq.heappush(events, (j["submit"], 1, id(j), "submit", j))

    def try_start(now):
        nonlocal busy_gpu_s
        for j in list(queue):
            order = sorted(range(nodes), key=lambda n: free[n] if policy == "pack" else -free[n])
            chosen, need = [], j["pods"]
            for n in order:                                  # all pods placed, or none (gang) #!
                k = min(need, free[n] // j["gpus_per_pod"])
                chosen += [n] * k; need -= k
                if need == 0:
                    break
            if need:
                continue
            for n in chosen:
                free[n] -= j["gpus_per_pod"]
            queue.remove(j); waits.append((j["pods"] * j["gpus_per_pod"], now - j["submit"]))
            busy_gpu_s += j["runtime"] * j["pods"] * j["gpus_per_pod"]
            heapq.heappush(events, (now + j["runtime"], 0, id(j), "end", (j, chosen)))

    while events:
        now, _, _, kind, payload = heapq.heappop(events)
        if now > t_end:
            break
        if kind == "submit":
            queue.append(payload)
        else:
            j, chosen = payload
            for n in chosen:
                free[n] += j["gpus_per_pod"]
        try_start(now)
    horizon = max(w for _, w in waits) + max(j["submit"] + j["runtime"] for j in jobs)
    return {"allocation": busy_gpu_s / (nodes * gpus_per_node * horizon), "waits": waits}
```

**Going deeper.**

- **Simulators flatter policies they model and ignore what they don't:** network contention, image pulls and failures are missing; treat predictions as ranges and confirm in the cluster.
- **Simulate the guardrails, not just the headline:** a policy that adds 6 points of utilization and triples p90 wait for 256-GPU jobs is not an improvement.

### Staged rollout with guardrails

**Scenario.** A fair-share change was rolled out to the whole fleet on a Friday. On Monday, preemptions had increased fivefold and two teams had lost a weekend of work.

![Staged rollout](diagrams/designs/concepts/ds19-c7.svg)

![Staged rollout with guardrails](diagrams/designs/ds19-6.svg)

**What the picture shows.** How each change reaches the fleet.

- **Stage 1, shadow:** compute what the new policy would have decided (from the simulator, or a dry-run mode), with no effect on jobs.
- **Stage 2, one pool:** apply to one pool or a set of volunteer teams; the rest of the fleet is the comparison group.
- **Stage 3, fleet:** apply everywhere after the guardrails held for an agreed period (for example, two weeks).
- **Guardrails, checked daily:** p90 wait by job size class, work lost to preemption, the fairness ratio per queue, and job failure rate; each with a threshold that triggers rollback.
- **Rollback is a revert:** the policy source is in Git; rolling back is reverting the change and letting the controller apply it.

**The YAML.** A change record that ties a policy version to its rollout stage, comparison group and guardrails.

```yaml
# changes/2026-12-binpack-gpu-pools.yaml
change: binpack-gpu-pools
policy_version: scheduling-policy@4f2a9c1
hypothesis: "MostAllocated scoring on GPU pools raises allocation by 4–7 points by reducing fragmentation"
simulated: { allocation_delta: [0.04, 0.07], p90_wait_256gpu_delta_h: [-6, -2] }
stages:
- { name: shadow,   start: 2026-12-01, scope: none }
- { name: pool-a,   start: 2026-12-04, scope: { pools: [h100-a] }, comparison: { pools: [h100-b] } }   #!
- { name: fleet,    start: 2026-12-18, requires: guardrails_held_days >= 14 }
guardrails:
  p90_wait_hours: { "1-8": 2, "9-64": 8, "65-256": 36, "257+": 72 }
  preemption_lost_gpu_h_per_day: 40
  fairness_ratio_min: 0.7                  # every queue gets ≥ 70% of its fair share when it has demand
  job_failure_rate_max_increase: 0.01
rollback: "git revert; controller applies previous policy"
```

**Going deeper.**

- **Pick comparison pools that are alike:** same GPU type, similar team mix and demand; otherwise the comparison measures the pools, not the policy.
- **One change per stage:** two changes rolled out together can only be attributed together.

### Proving the gain: separating policy from demand

**Scenario.** Utilization rose 9 points in December. Over the same month, a big project started and submitted three times its usual jobs. How much of the 9 points did the scheduler earn?

![Policy vs demand](diagrams/designs/concepts/ds19-c8.svg)

![Proving the gain: separating policy from demand](diagrams/designs/ds19-7.svg)

**What the picture shows.** Methods that attribute changes to policies rather than to demand.

- **Difference in differences:** compare the change in the treated pool with the change in the comparison pool over the same period; demand changes that affect both cancel out.
- **Switchback:** for policies that can be toggled safely, alternate them by week (or by day) in one pool and compare the periods; repeated switching averages out demand swings.
- **Demand-adjusted views:** utilization only means something when there's pending work; report utilization during hours when demand exceeded capacity, where the scheduler's choices matter most.
- **Per-change waterfall:** the total gain split into contributions from each rolled-out change, each from its own comparison, plus "demand and other".
- **Uncertainty:** daily values are correlated; use weekly blocks or block bootstrap for intervals ([Design 12](#design-experiment-analysis)).

**The code.** A difference-in-differences estimate with a block-bootstrap interval (Python).

```python
import numpy as np

def did(treated_before, treated_after, control_before, control_after, block=7, n_boot=2000, seed=0):
    """Each argument: daily active-utilization values (np.array) for one pool and period."""
    est = (treated_after.mean() - treated_before.mean()) - (control_after.mean() - control_before.mean())
    rng = np.random.default_rng(seed)
    def resample(x):
        blocks = [x[i:i + block] for i in range(0, len(x) - block + 1, block)]
        picks = rng.integers(0, len(blocks), size=len(blocks))
        return np.concatenate([blocks[i] for i in picks])
    boots = [(resample(treated_after).mean() - resample(treated_before).mean())
             - (resample(control_after).mean() - resample(control_before).mean()) for _ in range(n_boot)]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"effect": round(est, 4), "ci95": (round(lo, 4), round(hi, 4))}   # policy effect, net of demand #!
```

**Going deeper.**

- **Check the parallel-trends assumption:** before the change, the treated and comparison pools should move together; plot them over the baseline period before trusting the estimate.
- **Claim the interval, not the point:** "+5.5 points (3.4 to 7.6)" is the result to report to leadership.

### Idle reclamation: often the biggest lever

**Scenario.** The baseline showed 12% of capacity allocated but idle: 96 GPUs held by notebooks that were busy 8% of the time, inference replicas sized for a peak that came once a week, and training jobs stuck waiting on data loaders.

![Idle reclamation](diagrams/designs/concepts/ds19-c9.svg)

![Idle reclamation: often the biggest lever](diagrams/designs/ds19-8.svg)

**What the picture shows.** Recovering allocations that aren't doing work.

- **Detect:** per-pod GPU activity from DCGM over a window (for example, under 5% for 60 minutes) and the pod's kind (notebook, batch job, service).
- **Policy by kind:** notebooks are checkpointed and stopped after a warning; batch jobs that are idle for long are flagged to their owners, not killed; services are right-sized by their autoscalers ([Design 03](#design-training-inference)).
- **Right-size requests:** recommend smaller GPU slices for workloads that never use a full GPU (MIG or time-slicing, [Design 17](#design-gpu-scheduling)).
- **Exemptions with expiry:** a team can exempt a workload for a reason (a demo, a long debug session) for a limited time.
- **Feedback:** reclaimed GPU-hours per team appear on the team scorecard.

**The YAML.** An idle policy, read by a small reclaimer controller.

```yaml
apiVersion: scheduling.example.com/v1alpha1
kind: IdlePolicy
metadata: { name: default-gpu-idle }
spec:
  selector: { matchLabels: { gpu.example.com/workload-kind: notebook } }
  idle:
    metric: DCGM_FI_PROF_GR_ENGINE_ACTIVE
    below: 0.05
    forMinutes: 60                         # 60 minutes with the GPU essentially unused #!
  actions:
  - { after: 0m,  do: notify, channel: owner }
  - { after: 15m, do: checkpoint, command: ["/opt/notebook/save-state.sh"] }
  - { after: 20m, do: stop }               # the notebook can be restarted from its saved state
  exemptions: { annotation: gpu.example.com/idle-exempt-until, maxDays: 3 }
---
apiVersion: scheduling.example.com/v1alpha1
kind: IdlePolicy
metadata: { name: batch-gpu-idle }
spec:
  selector: { matchLabels: { gpu.example.com/workload-kind: batch } }
  idle: { metric: DCGM_FI_PROF_GR_ENGINE_ACTIVE, below: 0.05, forMinutes: 120 }
  actions:
  - { after: 0m, do: notify, channel: owner }   # batch jobs: tell the owner, never kill automatically
```

**Going deeper.**

- **Idle reclaim has a social side:** announce it, show each team its own idle numbers first, and give a grace period; surprise reclaim generates exemptions instead of savings.
- **Data-loader idle is a different problem:** a training job at 30% activity needs a faster input pipeline, not a reclaimer; flag it as an efficiency issue for the team.

### Team scorecards that keep the gains

**Scenario.** Six months after a previous push, idle allocation had crept back to where it started. Nobody had been looking at it per team.

![Team scorecards](diagrams/designs/concepts/ds19-c10.svg)

![Team scorecards that keep the gains](diagrams/designs/ds19-9.svg)

**What the picture shows.** A weekly scorecard per team, generated from the same pipeline.

- **Usage:** guaranteed GPUs, average allocated, average active, and borrowed or lent GPU-hours.
- **Efficiency:** activity rate of the team's allocations, idle GPU-hours reclaimed, workloads with exemptions.
- **Service received:** p50 and p90 wait by job size, preemptions suffered and work lost, fairness ratio.
- **Showback:** the cost of allocated-but-idle GPU-hours, so idle capacity has a visible price; chargeback where the organization uses it.
- **Trend:** week over week, against the team's own baseline, not against other teams.

**The code.** Building a team's weekly scorecard from the warehouse (Python).

```python
def scorecard(team, week, w):
    """w: warehouse access returning per-minute aggregates for the team and week."""
    m = w.team_minutes(team, week)                       # allocated, active, guaranteed, borrowed, lent
    jobs = w.team_jobs(team, week)                       # waits, sizes, preemptions
    gpu_h = lambda col: m[col].sum() / 60
    return {
        "team": team, "week": week,
        "guaranteed_avg": m.guaranteed.mean(),
        "allocated_avg": m.allocated.mean(),
        "active_avg": m.active.mean(),
        "activity_rate": gpu_h("active") / max(gpu_h("allocated"), 1e-9),
        "idle_cost_usd": (gpu_h("allocated") - gpu_h("active")) * w.price_per_gpu_h,   # showback #!
        "borrowed_gpu_h": gpu_h("borrowed"), "lent_gpu_h": gpu_h("lent"),
        "p90_wait_h_by_size": jobs.groupby("size_class").wait_h.quantile(0.9).round(1).to_dict(),
        "preemption_lost_gpu_h": jobs.lost_gpu_h.sum(),
        "fairness_ratio": w.fairness_ratio(team, week),
    }
```

**Going deeper.**

- **Show what the team gets, not just what it costs:** wait times and borrowed capacity are on the same page as idle cost; scorecards that only criticize get ignored.
- **Lent GPU-hours are a contribution:** teams whose idle quota helped others should see that credited.

### Running the layer: SLOs and continuous tuning

**Scenario.** After the project ended, nobody owned the scheduler. A Kueue upgrade changed a default, and nobody noticed that 256-GPU jobs started waiting twice as long.

![The scheduler as a service](diagrams/designs/concepts/ds19-c11.svg)

![Running the layer: SLOs and continuous tuning](diagrams/designs/ds19-10.svg)

**What the picture shows.** The scheduling layer as a service with an owner and objectives.

- **SLOs:** active utilization during periods with pending demand, p90 wait per size class, work lost to preemption, and fairness ratio; reviewed monthly.
- **Alerts:** on guardrail breaches and sudden changes (a wait-time jump after an upgrade).
- **Upgrade testing:** Kueue and Kubernetes upgrades are replayed in the simulator and rolled out to one pool first, like policy changes.
- **Tuning loop:** each quarter, the attribution waterfall and scorecards pick the next lever: more idle reclaim, better packing, quota rebalancing or new capacity.
- **Capacity decisions:** the same data answers "do we need more GPUs?": if utilization during high-demand periods is near its ceiling and waits are long, the answer is yes.

**Going deeper.**

- **Utilization has a healthy ceiling:** above roughly 85–90% active utilization, waits grow quickly for large gangs; the target is high utilization with acceptable waits, not 100%.
- **Keep the change log forever:** months later, a question like "when did large-job waits get worse?" is answered by lining up the metric with the log.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds19-c12.svg)

- **Phase 1, agree and measure (weeks 1–6):** the metric definition signed off; the pipeline; a four-week baseline by team and pool; the simulator calibrated against the baseline.
- **Phase 2, quick wins (weeks 6–12):** quotas as code; idle reclamation for notebooks; gang admission for all multi-node jobs. Each staged with a comparison pool and attributed.
- **Phase 3, structural changes (weeks 12–20):** bin-packing profiles and pools by job shape; cohorts with borrowing and lending; fair-share weights.
- **Phase 4, keep it (ongoing):** scorecards, SLOs, upgrade testing and the quarterly tuning loop.

**Trade-offs we accepted.**

- **A slower start:** six weeks of definitions and baseline before the first change, in exchange for results that hold up.
- **Comparison pools delay gains** for some teams by a few weeks during staged rollout.
- **Idle reclamation inconveniences people:** warnings, grace periods and exemptions soften it but don't remove it.
- **Active utilization below 100% by design:** headroom keeps large-job waits acceptable.

**Open questions.**

- **Better activity signals:** is graphics-engine activity the right threshold signal for all workloads, or should inference and training use different ones?
- **Elastic jobs:** how much more utilization would jobs that grow and shrink with free capacity unlock, and how would we measure it?
- **Incentives:** does showback change behavior, or is automatic reclamation the only thing that works?
- **Fleet-wide view:** how to measure and attribute utilization across many clusters and providers ([Design 18](#design-multicloud-fleet)) with different contracts?

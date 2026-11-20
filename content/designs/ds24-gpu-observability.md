---
id: design-gpu-observability
title: "Design 24: Instrumenting fleet utilization with DCGM, Prometheus and Grafana"
short: "GPU observability: rules and alerts"
---
This design answers one question: *how do we instrument a GPU fleet so that utilization numbers are correct, dashboards are fast, and alerts are ones teams act on instead of muting?* [Design 19](#design-scheduling-impact) defined the utilization metric the scheduling program is judged on. [Design 23](#design-health-remediation) uses many of the same signals to find faults. This design is the observability layer under both: which DCGM fields to collect, how to attach team and job labels, how to keep Prometheus within its limits, the recording rules every dashboard reads, the alerts with their owners and runbooks, and the tests that keep all of it correct.

The short version: *collect deliberately, join labels once, and make recording rules the only interface*. DCGM exporter collects a reviewed set of fields at a fixed interval, with pod mapping on. Recording rules do the expensive joins (GPU to pod to namespace to team, GPU to node to pool) once per minute and publish a small set of named series in a fixed hierarchy: GPU, node, team and pool, fleet. Dashboards and alerts read only those series. Every rule and alert lives in Git with unit tests, every alert has an owner label that routes it, a runbook link and a measured precision, and alerts that aren't acted on are fixed or deleted.

### The use case and requirements

**Scenario.** The fleet has 2,048 GPUs across 3 clusters and 14 teams. Each team built its own Grafana dashboard with its own PromQL, so "utilization" had five definitions. The fleet dashboard took 40 seconds to load because every panel joined raw DCGM series with kube-state-metrics on the fly. One Prometheus ran out of memory twice in a month. The GPU alerts paged the platform on-call about low utilization in jobs that were simply loading data, so people muted them, and a real thermal-throttling problem went unnoticed for a week.

![Five utilizations, muted alerts](diagrams/designs/concepts/ds24-c1.svg)

**What the platform must deliver.**

- **One definition per metric:** utilization, allocation, activity and the rest come from shared recording rules, matching [Design 19](#design-scheduling-impact).
- **Attribution:** every GPU sample attributed to node, pool, GPU model, pod, namespace, team and job.
- **Fast dashboards:** fleet and team views load in under 3 seconds, at 30-day ranges.
- **Bounded cost:** a known, budgeted number of series per GPU, and Prometheus instances that stay within memory.
- **Alerts teams rely on:** each alert has an owner, a runbook and a severity that matches what the owner must do, and its precision is measured.
- **Correctness under change:** rules and alerts are tested in CI against synthetic series before they deploy.
- **Long-term history:** a year of aggregates for capacity planning and before-and-after comparisons.

**Out of scope:** application-level metrics (serving latency in [Design 22](#design-serving-benchmarks), training throughput in [Design 15](#design-eval-infra)) and fault remediation ([Design 23](#design-health-remediation)).

### The pipeline from GPU to dashboard

**Scenario.** DCGM exporter had been installed three different ways in three clusters: by the GPU Operator, by a standalone Helm chart, and by hand. The clusters exposed different metric sets, at different intervals, with different label names.

![Pipeline](diagrams/designs/concepts/ds24-c2.svg)

![The pipeline from GPU to dashboard](diagrams/designs/ds24-1.svg)

**What the picture shows.** One pipeline, the same in every cluster.

- **DCGM exporter:** deployed by the GPU Operator on every GPU node, with a shared metrics ConfigMap and Kubernetes pod mapping enabled.
- **Prometheus per cluster:** run by the Prometheus Operator, scraping the exporter through a ServiceMonitor, plus kube-state-metrics and node exporter.
- **Recording rules:** evaluated in each cluster's Prometheus, producing the shared series with a `cluster` label.
- **Long-term store:** recording-rule outputs (not raw DCGM series) are remote-written to a central store such as Mimir or Thanos for fleet views and a year of history.
- **Grafana and Alertmanager:** dashboards query the long-term store for fleet views and the cluster Prometheus for detail. Alerts are evaluated close to the data and routed by Alertmanager.

**The YAML.** The GPU Operator settings for DCGM exporter: a shared field list, a fixed interval and a ServiceMonitor (ClusterPolicy fragment).

```yaml
apiVersion: nvidia.com/v1
kind: ClusterPolicy
metadata: { name: cluster-policy }
spec:
  dcgmExporter:
    enabled: true
    config:
      name: fleet-dcgm-metrics                 # ConfigMap with key dcgm-metrics.csv, same namespace as the operator
    env:
    - { name: DCGM_EXPORTER_COLLECTORS, value: /etc/dcgm-exporter/dcgm-metrics.csv }
    - { name: DCGM_EXPORTER_INTERVAL, value: "30000" }          # 30 s everywhere #!
    - { name: DCGM_EXPORTER_KUBERNETES, value: "true" }         # pod, namespace, container labels
    serviceMonitor:
      enabled: true
      interval: 30s
      honorLabels: false
      additionalLabels: { release: kube-prometheus-stack }
```

**Going deeper.**

- **Remote-write the rules, not the raw series:** raw per-GPU DCGM data stays in the cluster for a short retention. The long-term store receives the much smaller recording-rule outputs, which is what fleet dashboards and capacity planning need.
- **The same pipeline everywhere is the feature:** clusters added later get the same fields, labels and rules by installing the same GPU Operator values and rule bundle.

### Choosing fields and the interval

**Scenario.** The teams' dashboards relied on `DCGM_FI_DEV_GPU_UTIL`. A data-loading job that touched the GPU once a second showed 100% "utilization". SM activity, the field that would have shown the GPU was mostly idle, wasn't being collected at all.

![Field selection](diagrams/designs/concepts/ds24-c3.svg)

![Choosing fields and the interval](diagrams/designs/ds24-2.svg)

**What the picture shows.** The reviewed field list, grouped by purpose, and the interval trade-off.

- **Activity (profiling fields):** graphics engine active (the headline activity signal), SM active, SM occupancy, tensor pipe active and DRAM active. Several are commented out in the exporter's default list and must be enabled.
- **Memory and interconnect:** framebuffer used and free, PCIe and NVLink transmit and receive bytes.
- **Power and thermals:** power usage, GPU and memory temperature, and clock event (throttle) reasons.
- **Errors:** Xid errors, ECC counts, row-remap status and failures, and NVLink error counters, which [Design 23](#design-health-remediation) uses.
- **Identity:** driver version and GPU model as labels.
- **Interval:** 30 seconds for the fleet. Profiling fields cost GPU-side sampling. Faster intervals multiply storage, and the questions asked (minutes to months) don't need them.

**The YAML.** The shared field list (ConfigMap read by DCGM exporter; three columns: field, Prometheus type, help).

```yaml
apiVersion: v1
kind: ConfigMap
metadata: { name: fleet-dcgm-metrics, namespace: gpu-operator }
data:
  dcgm-metrics.csv: |
    # activity
    DCGM_FI_PROF_GR_ENGINE_ACTIVE,    gauge,   Ratio of time the graphics engine is active
    DCGM_FI_PROF_SM_ACTIVE,           gauge,   Ratio of cycles an SM has at least one warp assigned
    DCGM_FI_PROF_SM_OCCUPANCY,        gauge,   Ratio of resident warps to maximum warps per SM
    DCGM_FI_PROF_PIPE_TENSOR_ACTIVE,  gauge,   Ratio of cycles the tensor pipe is active
    DCGM_FI_PROF_DRAM_ACTIVE,         gauge,   Ratio of cycles device memory is sending or receiving
    DCGM_FI_DEV_GPU_UTIL,             gauge,   Kept for compatibility; not used in rules
    # memory and interconnect
    DCGM_FI_DEV_FB_USED,              gauge,   Framebuffer used (MiB)
    DCGM_FI_DEV_FB_FREE,              gauge,   Framebuffer free (MiB)
    DCGM_FI_PROF_NVLINK_TX_BYTES,     counter, NVLink bytes transmitted
    DCGM_FI_PROF_NVLINK_RX_BYTES,     counter, NVLink bytes received
    DCGM_FI_PROF_PCIE_TX_BYTES,       counter, PCIe bytes transmitted
    DCGM_FI_PROF_PCIE_RX_BYTES,       counter, PCIe bytes received
    # power and thermals
    DCGM_FI_DEV_POWER_USAGE,          gauge,   Power draw (W)
    DCGM_FI_DEV_GPU_TEMP,             gauge,   GPU temperature (C)
    DCGM_FI_DEV_MEMORY_TEMP,          gauge,   Memory temperature (C)
    DCGM_FI_DEV_CLOCK_THROTTLE_REASONS, gauge, Clock throttle reasons bitmask
    # errors
    DCGM_FI_DEV_XID_ERRORS,           gauge,   Last Xid error
    DCGM_FI_DEV_ECC_DBE_VOL_TOTAL,    counter, Volatile double-bit ECC errors
    DCGM_FI_DEV_ROW_REMAP_FAILURE,    gauge,   Row remapping failed
    DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS, counter, Rows remapped for uncorrectable errors
    DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL, counter, NVLink flit CRC errors
```

**Going deeper.**

- **Profiling fields and profilers compete:** when a user runs Nsight Compute or Nsight Systems with GPU metrics on a node, DCGM's profiling fields can conflict with it. DCGM can pause profiling collection (`dcgmi profile --pause`) for the session. Document this for users, and expect gaps rather than errors.
- **When fleet metrics say "busy" but one job is slow, profile that job:** Nsight Systems records a timeline of one process, with its GPU kernels, memory copies and any phases you label, so you can see whether the GPU waits for data. Run it inside the job's pod. That needs access to the kernel's performance interface (in container terms, the `SYS_ADMIN` capability or privileged mode, and a host setting that allows it), and sampling GPU metrics needs elevated permissions ([Nsight Systems guide](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)). Grant that only to a short-lived debug pod, not to every job, and pause DCGM's profiling fields on the node first, as above. In the current guide the option that samples GPU metrics is spelled `--gpu-metrics-devices`.
- **Field names change between DCGM versions:** the throttle-reasons field has a newer name (`DCGM_FI_DEV_CLOCKS_EVENT_REASONS`). Pin the exporter version with the field list and test both together.

### Labels and joins: who used this GPU?

**Scenario.** DCGM series carried the pod and namespace, but not the team, the node pool or the job. Every team dashboard joined three metrics in every panel, and teams whose namespaces lacked a team label silently disappeared from the totals.

![Label joins](diagrams/designs/concepts/ds24-c4.svg)

![Labels and joins: who used this GPU?](diagrams/designs/ds24-3.svg)

**What the picture shows.** Where each label comes from and where the join happens.

- **From DCGM exporter:** GPU index, UUID, model name, hostname, and (with pod mapping) pod, namespace and container, read from the kubelet's pod-resources API.
- **From kube-state-metrics:** namespace labels (team, cost center), node labels (pool, rack, GPU product) and pod labels (job name, workload kind). These must be allow-listed in kube-state-metrics to appear at all.
- **MIG:** with MIG enabled, the exporter adds GPU-instance labels. Each instance is a separate series, and rules must count instances rather than whole GPUs.
- **Join once, in recording rules:** the first rule level attaches team, pool and job to every GPU sample. Everything above reads the joined series.
- **Unattributed is a category:** GPUs whose namespace has no team label are reported as `team="unlabeled"`, not dropped.

**The YAML.** The first-level recording rules that attach team and pool to GPU activity (Prometheus Operator).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: gpu-joins, namespace: monitoring }
spec:
  groups:
  - name: gpu-joins
    interval: 1m
    rules:
    - record: namespace:team:info                      # one series per namespace with its team
      expr: |
        max by (namespace, team) (
          label_replace(kube_namespace_labels{label_team!=""}, "team", "$1", "label_team", "(.*)")
        )
        or max by (namespace, team) (
          label_replace(kube_namespace_labels{label_team=""}, "team", "unlabeled", "", "")   # never dropped #!
        )
    - record: node:pool:info
      expr: |
        max by (node, pool, gpu_product) (
          label_replace(label_replace(kube_node_labels, "pool", "$1", "label_node_pool", "(.*)"),
                        "gpu_product", "$1", "label_nvidia_com_gpu_product", "(.*)")
        )
    - record: gpu:engine_active:ratio                  # one series per GPU, with team and pool attached
      expr: |
        label_replace(DCGM_FI_PROF_GR_ENGINE_ACTIVE, "node", "$1", "Hostname", "(.*)")
        * on (namespace) group_left (team) namespace:team:info
        * on (node) group_left (pool, gpu_product) node:pool:info
```

**Going deeper.**

- **Idle GPUs have no pod:** a GPU not assigned to any pod has empty pod and namespace labels, so a join on namespace drops it. A parallel rule records unassigned GPUs with `team="none"`, so fleet totals still add up.
- **Hostname is not always the node name:** on some platforms DCGM's `Hostname` differs from the Kubernetes node name. Set the exporter's node name from the downward API, or keep a mapping rule, and test it.

### Cardinality and scale

**Scenario.** One Prometheus ran out of memory twice. It held 3.1 million active series. More than half came from DCGM series that inherited every pod label, including a per-run hash, through a well-meant relabeling rule.

![Cardinality](diagrams/designs/concepts/ds24-c5.svg)

![Cardinality and scale](diagrams/designs/ds24-4.svg)

**What the picture shows.** The series budget and the controls that keep it.

- **Budget per GPU:** about 25 DCGM fields times one series per GPU (more with MIG and NVLink per link) gives roughly 30 series per GPU. At 2,048 GPUs, that's about 60,000 series. That's cheap, unless labels multiply it.
- **Label churn is the danger:** every new pod creates new series. Per-run labels (hashes, run IDs) on raw DCGM series turn each job into a new set of series, and they pile up across the retention window.
- **Controls:** don't copy pod labels onto DCGM series. Attach job identity only in recording rules, and only labels on an allow-list. Drop unused fields at scrape time.
- **Retention by level:** raw DCGM series kept 7 days in the cluster. Recording-rule outputs kept 13 months in the long-term store.
- **Watch it:** active series per job and per metric name, with an alert when the budget is exceeded.

**The code.** Estimating active series for DCGM data under different label policies (Python).

```python
def dcgm_series(gpus: int, fields: int, nvlink_fields: int = 2, links_per_gpu: int = 18,
                mig_instances: int = 1, churn_jobs_per_gpu_per_day: float = 0.0, retention_days: int = 7) -> dict:
    per_gpu = fields * mig_instances + nvlink_fields * links_per_gpu        # per-link fields add up fast
    steady = gpus * per_gpu
    churn = gpus * per_gpu * churn_jobs_per_gpu_per_day * retention_days    # series kept alive in the TSDB #!
    return {"steady_series": steady, "with_label_churn": int(steady + churn)}

print(dcgm_series(2048, 25, nvlink_fields=0))                                   # ~51k: pod labels only
print(dcgm_series(2048, 25, nvlink_fields=0, churn_jobs_per_gpu_per_day=6))     # per-run labels on raw series
```

**Going deeper.**

- **Per-link NVLink fields are opt-in:** 18 links per GPU times a few fields doubles the DCGM series count. Collect the per-GPU totals by default and per-link data only on demand.
- **Shard by cluster, not by metric:** one Prometheus per cluster (or per few hundred nodes) keeps joins local. Fleet views come from the long-term store.

### The recording rule hierarchy

**Scenario.** Five dashboards computed fleet utilization five ways: averaging GPU utilization, summing SM activity, counting allocated GPUs. They disagreed by up to 20 points, and each team believed its own.

![Rule hierarchy](diagrams/designs/concepts/ds24-c6.svg)

![The recording rule hierarchy](diagrams/designs/ds24-5.svg)

**What the picture shows.** One set of rules, named by level, that every consumer reads.

- **Naming:** Prometheus's `level:metric:operations` convention. The prefix says what the series is aggregated by (`gpu:`, `node:`, `team:`, `pool:`, `fleet:`), and the suffix says what was done (`:ratio`, `:sum`, `:avg1h`).
- **GPU level:** joined activity, allocation (is any pod assigned to this GPU) and health per GPU, every minute.
- **Node, team and pool levels:** sums and averages of the GPU level, with counts so ratios can be recomputed correctly at higher levels.
- **Fleet level:** the headline numbers of [Design 19](#design-scheduling-impact): allocation rate, activity rate and active utilization.
- **Time aggregates:** hourly averages for dashboards with long ranges. Daily values computed in the long-term store.
- **Ratios from sums, never averages of ratios:** a team-level ratio is the sum of active GPUs divided by the sum of allocated GPUs, not the average of per-GPU ratios.

**The YAML.** The core utilization rules from GPU level to fleet level (Prometheus Operator).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: gpu-utilization, namespace: monitoring }
spec:
  groups:
  - name: gpu-utilization
    interval: 1m
    rules:
    - record: gpu:allocated:bool                       # 1 if a pod holds this GPU
      expr: max by (cluster, node, gpu, UUID, team, pool, gpu_product, namespace, pod) (gpu:engine_active:ratio{pod!=""} >= bool 0)
    - record: gpu:active:bool                          # 1 if allocated and the engine is working
      expr: max by (cluster, node, gpu, UUID, team, pool, gpu_product, namespace, pod) (gpu:engine_active:ratio{pod!=""} > bool 0.05)
    - record: team:gpu_allocated:sum
      expr: sum by (cluster, team, pool) (gpu:allocated:bool)
    - record: team:gpu_active:sum
      expr: sum by (cluster, team, pool) (gpu:active:bool)
    - record: pool:gpu_capacity:sum                    # healthy, schedulable GPUs
      expr: |
        sum by (cluster, pool) (
          kube_node_status_allocatable{resource="nvidia_com_gpu"}
          * on (cluster, node) group_left (pool) node:pool:info
          * on (cluster, node) group_left () (kube_node_spec_unschedulable == bool 0)   # 0 if cordoned
        )
    - record: pool:gpu_active_utilization:ratio        # the headline, from sums #!
      expr: sum by (cluster, pool) (team:gpu_active:sum) / pool:gpu_capacity:sum
    - record: fleet:gpu_active_utilization:ratio
      expr: sum(team:gpu_active:sum) / sum(pool:gpu_capacity:sum)
    - record: team:gpu_active:avg1h
      expr: avg_over_time(team:gpu_active:sum[1h])
```

**Going deeper.**

- **Rules are an API:** renaming or redefining a recording rule breaks dashboards and alerts that depend on it. Version them, deprecate old names for a release before removing them, and list consumers.
- **Evaluation time matters:** rule groups that depend on other groups' outputs see them one interval late. Keep dependent rules in the same group, where they're evaluated in order.

### Testing rules and alerts

**Scenario.** A refactor changed `> 0.05` to `> 5` in the activity rule, because someone assumed the field was a percentage. Fleet utilization dropped to zero on every dashboard, and the low-utilization alert fired for every team at once.

![Testing rules](diagrams/designs/concepts/ds24-c7.svg)

![Testing rules and alerts](diagrams/designs/ds24-6.svg)

**What the picture shows.** Rules and alerts tested like code before they deploy.

- **Unit tests with synthetic series:** `promtool test rules` feeds hand-written series into the rules and checks the outputs and alerts at given times.
- **Test the edge cases:** unlabeled namespaces, idle GPUs without pods, MIG instances, a node going unschedulable, missing data.
- **Lint in CI:** `promtool check rules`, plus checks that every alert has an owner label, a severity, a summary and a runbook URL.
- **Shadow evaluation:** a changed rule runs under a new name next to the old one for a few days, and the difference is reviewed before switching.
- **Contract tests for dashboards:** a script checks that every query in every dashboard only references recording rules that exist.

**The YAML.** A unit test for the activity and utilization rules, and for a team alert (promtool).

```yaml
# tests/gpu-utilization.test.yaml
rule_files: [../rules/gpu-utilization.yaml]
evaluation_interval: 1m
tests:
- interval: 1m
  input_series:
  - series: 'gpu:engine_active:ratio{cluster="c1",node="n1",gpu="0",UUID="u0",team="llm",pool="h100",gpu_product="H100",pod="p1"}'
    values: '0.62x10'
  - series: 'gpu:engine_active:ratio{cluster="c1",node="n1",gpu="1",UUID="u1",team="llm",pool="h100",gpu_product="H100",pod="p2"}'
    values: '0.01x10'                                  # allocated but idle
  - series: 'kube_node_status_allocatable{resource="nvidia_com_gpu",node="n1",cluster="c1"}'
    values: '8x10'
  - series: 'node:pool:info{node="n1",pool="h100",gpu_product="H100",cluster="c1"}'
    values: '1x10'
  - series: 'kube_node_spec_unschedulable{node="n1",cluster="c1"}'
    values: '0x10'
  promql_expr_test:
  - expr: team:gpu_active:sum{team="llm"}
    eval_time: 5m
    exp_samples: [{ labels: 'team:gpu_active:sum{cluster="c1",team="llm",pool="h100"}', value: 1 }]
  - expr: pool:gpu_active_utilization:ratio
    eval_time: 5m
    exp_samples: [{ labels: 'pool:gpu_active_utilization:ratio{cluster="c1",pool="h100"}', value: 0.125 }]   # 1 of 8 #!
```

**Going deeper.**

- **A test per bug:** every rule bug found in production gets a test that reproduces it, so it can't come back.
- **Test alerts with time:** alert tests check that an alert is *not* firing before its `for` duration has passed and *is* firing after, which catches wrong durations.

### Alerts teams rely on

**Scenario.** The platform on-call received 140 GPU pages a week, mostly "GPU utilization below 10%" for jobs that were loading data or waiting at a barrier. They were muted. A cooling fault that throttled 64 GPUs for a week never paged anyone, because throttling had no alert.

![Alert catalog](diagrams/designs/concepts/ds24-c8.svg)

![Alerts teams rely on](diagrams/designs/ds24-7.svg)

**What the picture shows.** The alert catalog, split by who owns the fix.

- **Platform alerts (page the platform):** exporter down or data stale, fatal Xid or row-remap failure (also feeding [Design 23](#design-health-remediation)), sustained thermal or power throttling across a domain, NVLink error rates, and the observability pipeline itself (rule evaluation failures, remote-write lag).
- **Team alerts (ticket to the team, not a page):** GPUs allocated but idle for hours, jobs holding GPUs with no activity since start, quota near exhaustion, and team utilization trends.
- **Principles:** each alert says what is wrong for whom, has a `for` duration longer than normal transient behavior, carries an owner label, a severity (page or ticket), and a runbook, and is tested.
- **Symptoms over causes for pages:** "64 GPUs throttled for 30 minutes" pages. "One GPU at 85 °C" doesn't.
- **No utilization pages:** low utilization isn't an emergency. It's a ticket with numbers, sent to the team that can fix it.

**The YAML.** Representative platform and team alerts (Prometheus Operator).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: gpu-alerts, namespace: monitoring }
spec:
  groups:
  - name: gpu-platform
    rules:
    - alert: DcgmExporterDataStale
      expr: |
        max by (cluster, node) (
          label_replace(time() - timestamp(DCGM_FI_DEV_POWER_USAGE), "node", "$1", "Hostname", "(.*)")
        ) > 180
      for: 5m
      labels: { severity: page, owner: platform }
      annotations:
        summary: "No DCGM data from {{ $labels.node }} for 3+ minutes"
        runbook_url: https://runbooks.example.com/gpu/dcgm-stale
    - alert: GpuThermalThrottlingWidespread
      expr: |
        count by (cluster, pool) (
          (DCGM_FI_DEV_CLOCK_THROTTLE_REASONS % 64 >= 32)                     # thermal slowdown bit #!
          or (DCGM_FI_DEV_CLOCK_THROTTLE_REASONS % 128 >= 64)                  # hardware thermal slowdown bit
        ) > 16
      for: 30m
      labels: { severity: page, owner: platform }
      annotations:
        summary: "{{ $value }} GPUs in {{ $labels.pool }} thermally throttled for 30 minutes"
        runbook_url: https://runbooks.example.com/gpu/thermal-throttling
  - name: gpu-team
    rules:
    - alert: GpusAllocatedButIdle
      expr: |
        sum by (cluster, team, namespace, pod) (gpu:allocated:bool - gpu:active:bool) >= 1
        and on (cluster, team, namespace, pod)
        (max by (cluster, team, namespace, pod) (max_over_time(gpu:active:bool[2h])) == 0)
        and on (cluster, team, namespace, pod)
        (max by (cluster, team, namespace, pod) (count_over_time(gpu:allocated:bool[2h])) >= 110)   # held ~2 h
      for: 15m
      labels: { severity: ticket, owner: team }        # routed to the team, never a page
      annotations:
        summary: "{{ $labels.namespace }}/{{ $labels.pod }} holds GPUs with no activity for 2 hours"
        runbook_url: https://runbooks.example.com/gpu/idle-allocation
```

**Going deeper.**

- **Bitmask alerts need care:** throttle reasons are bit flags. Test each bit with modulo arithmetic as above, and check the bit values against the DCGM version in use.
- **Alert on the pipeline too:** a dashboard showing zero because the exporter died is worse than no dashboard. Staleness alerts come first.

### Routing, ownership and noise control

**Scenario.** All GPU alerts went to one Slack channel. When a top-of-rack switch failed, 200 alerts arrived in five minutes (exporter stale, Xid, NVLink errors for every node behind it), and the one alert that explained the cause was on the second page.

![Routing](diagrams/designs/concepts/ds24-c9.svg)

![Routing, ownership and noise control](diagrams/designs/ds24-8.svg)

**What the picture shows.** Alertmanager configuration that sends each alert to its owner, once.

- **Route by owner and severity:** platform pages go to the platform's paging service. Team tickets go to each team's queue, found from the `team` label.
- **Group by domain:** alerts are grouped by cluster and pool (or rack), so a domain-wide problem arrives as one notification listing the affected nodes.
- **Inhibition:** when a node is down or its exporter is stale, alerts that depend on that node's data are suppressed. A domain-level alert (from [Design 23](#design-health-remediation)'s storm detection) inhibits the node-level ones beneath it.
- **Maintenance silences:** planned drains and upgrades create silences automatically from the maintenance system, with an expiry.
- **Repeat intervals by severity:** pages repeat hourly until acknowledged. Tickets are sent once a day.

**The YAML.** Alertmanager routing, grouping and inhibition (fragment).

```yaml
route:
  receiver: platform-tickets
  group_by: [cluster, pool, alertname]
  group_wait: 60s
  group_interval: 5m
  routes:
  - matchers: [owner="platform", severity="page"]
    receiver: platform-pager
    repeat_interval: 1h
  - matchers: [owner="team"]
    receiver: team-queues                     # webhook that files a ticket in the team's queue by label #!
    group_by: [cluster, team, alertname]
    repeat_interval: 24h
inhibit_rules:
- source_matchers: [alertname="DcgmExporterDataStale"]
  target_matchers: [owner="platform", severity="page"]
  equal: [cluster, node]                      # no GPU alerts from a node whose data is stale
- source_matchers: [alertname="DomainIncident"]
  target_matchers: [severity="page"]
  equal: [cluster, pool]
receivers:
- name: platform-pager
  pagerduty_configs: [{ routing_key_file: /etc/alertmanager/secrets/pd-platform }]
- name: platform-tickets
  webhook_configs: [{ url: http://alert-router.monitoring/tickets/platform }]
- name: team-queues
  webhook_configs: [{ url: http://alert-router.monitoring/tickets/by-team }]
```

**Going deeper.**

- **Every team label must route somewhere:** a CI check compares team labels in the namespace inventory with the router's team list, so no alert lands nowhere.
- **Inhibition hides information:** keep inhibited alerts visible in the alert UI and attached to the parent notification. Suppress the notifications, not the knowledge.

### Dashboards as code

**Scenario.** Grafana had 230 dashboards, 180 of them copies of each other with small edits. Fixing a query meant finding every copy, and the fleet dashboard took 40 seconds because its panels queried raw DCGM series over 30 days.

![Dashboards as code](diagrams/designs/concepts/ds24-c10.svg)

![Dashboards as code](diagrams/designs/ds24-9.svg)

**What the picture shows.** A small set of generated dashboards that only read recording rules.

- **Four standard dashboards:** fleet (pools, utilization tree, trends), team (allocation, activity, idle cost, wait times), node and GPU (fields, errors, thermals for one node), and job (the GPUs of one job over its lifetime).
- **Generated from code:** dashboards are generated by a script (or Grafonnet) from shared panel definitions, committed to Git, and provisioned by the Grafana Operator.
- **Only recording rules:** fleet and team panels query `team:`, `pool:` and `fleet:` series, so they're fast at any range. Raw DCGM queries appear only on the node and job dashboards, at short ranges.
- **Variables, not copies:** cluster, pool and team are dashboard variables. Each team uses the same dashboard filtered to its label.
- **Links between levels:** fleet links to pool, pool to team, team to job, job to node.

**The code.** Generating the team dashboard's panels from recording rules (Python).

```python
import json

PANELS = [
    ("Allocated GPUs",        'sum(team:gpu_allocated:sum{team="$team", cluster=~"$cluster"})', "stat"),
    ("Active GPUs",           'sum(team:gpu_active:sum{team="$team", cluster=~"$cluster"})', "stat"),
    ("Activity rate",         'sum(team:gpu_active:sum{team="$team"}) / sum(team:gpu_allocated:sum{team="$team"})', "timeseries"),
    ("Idle allocated (1h avg)", 'sum(team:gpu_allocated:sum{team="$team"}) - sum(team:gpu_active:avg1h{team="$team"})', "timeseries"),
]

def team_dashboard(uid: str = "gpu-team") -> dict:
    panels = []
    for i, (title, expr, kind) in enumerate(PANELS):
        assert "DCGM_" not in expr, f"{title}: team panels must read recording rules only"   #!
        panels.append({"id": i + 1, "type": kind, "title": title,
                       "gridPos": {"x": (i % 2) * 12, "y": (i // 2) * 8, "w": 12, "h": 8},
                       "targets": [{"expr": expr, "refId": "A"}]})
    return {"uid": uid, "title": "GPU · Team", "schemaVersion": 39, "panels": panels,
            "templating": {"list": [
                {"name": "cluster", "type": "query", "query": "label_values(team:gpu_allocated:sum, cluster)", "multi": True, "includeAll": True},
                {"name": "team", "type": "query", "query": "label_values(team:gpu_allocated:sum, team)"}]}}

print(json.dumps(team_dashboard())[:200])
```

**Going deeper.**

- **Provision, don't click:** dashboards edited in the UI drift from Git. Make provisioned dashboards read-only and give teams a sandbox folder for experiments.
- **Show definitions on the dashboard:** a text panel that links to the metric definitions settles most "why does this say 58%?" questions.

### Trusting the alerts: measuring alert quality

**Scenario.** Nobody knew which alerts were useful. Some fired daily and were always ignored. Others had never fired, and nobody knew whether they still worked.

![Alert quality](diagrams/designs/concepts/ds24-c11.svg)

![Trusting the alerts: measuring alert quality](diagrams/designs/ds24-10.svg)

**What the picture shows.** Alerts treated as a product with quality metrics.

- **Precision:** the share of an alert's notifications that led to action (an acknowledgment followed by a change, ticket or incident). Alerts below a threshold are reworked or deleted.
- **Volume per owner:** pages per on-call shift and tickets per team per week, with targets.
- **Time to acknowledge:** by alert and severity. Slow acknowledgment of a page means it isn't trusted or isn't urgent.
- **Coverage:** incidents that no alert caught, found in postmortems, become new alerts with tests.
- **Silent alerts:** alerts that haven't fired in 90 days are tested by injecting a synthetic series, to prove they still work.
- **Monthly review:** owners review their alerts' metrics and decide whether to keep, tune or delete each one.

**The code.** Alert quality from Alertmanager notification history and ticket outcomes (Python).

```python
import pandas as pd

def alert_quality(n: pd.DataFrame) -> pd.DataFrame:
    """n: one row per notification with alertname, owner, severity, fired_at, acked_at, actioned (bool)."""
    n = n.assign(tta_min=(n.acked_at - n.fired_at).dt.total_seconds() / 60)
    q = n.groupby(["alertname", "owner", "severity"]).agg(
        notifications=("actioned", "size"), precision=("actioned", "mean"),
        tta_p50_min=("tta_min", "median"))
    q["verdict"] = "keep"
    q.loc[(q.precision < 0.3) & (q.notifications >= 10), "verdict"] = "rework-or-delete"   # mostly ignored #!
    q.loc[(q.severity == "page") & (q.tta_p50_min > 30), "verdict"] = "not-urgent: make it a ticket"
    return q.sort_values("precision")
```

**Going deeper.**

- **Deleting alerts is a success:** a smaller set of alerts that are always acted on protects attention better than a large set that's mostly ignored.
- **Track what the alerts protect:** for each page, record the impact it prevented or shortened. That makes the case for keeping it.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds24-c12.svg)

- **Phase 1, one pipeline (weeks 1–3):** the same GPU Operator exporter settings and field list in every cluster, ServiceMonitors, kube-state-metrics label allow-lists and a series budget.
- **Phase 2, rules (weeks 3–6):** the join and utilization rule hierarchy with unit tests, remote write of rule outputs, and retention by level.
- **Phase 3, dashboards (weeks 5–8):** the four generated dashboards, provisioning through the Grafana Operator, and retirement of copied dashboards.
- **Phase 4, alerts (weeks 6–10):** the alert catalog with owners and runbooks, Alertmanager routing and inhibition, deletion of the utilization pages, and monthly alert-quality reviews.

**Trade-offs we accepted.**

- **30-second resolution:** short spikes are averaged away. Job-level profiling tools cover that need.
- **Raw DCGM data kept only 7 days:** older questions are answered from aggregates.
- **Fewer, centrally owned rules:** teams can't define their own utilization, which is the point but costs flexibility.
- **Utilization never pages:** some teams wanted it to. They get tickets with numbers instead.

**Open questions.**

- **Per-job efficiency:** should the platform publish model FLOPs utilization per job by combining DCGM tensor activity with the job's own throughput metrics?
- **MIG and time-slicing:** how should "active" be defined for a GPU shared by several pods?
- **OpenTelemetry:** when should the pipeline move from Prometheus scraping to OpenTelemetry collectors, and what changes for rules?
- **Cost attribution:** how to turn the same series into chargeback that finance trusts as much as the platform does?

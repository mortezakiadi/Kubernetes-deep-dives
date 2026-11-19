---
id: design-cost-capacity
title: "Design 25: A warehouse for utilization and spend: attribution to teams and workloads, and capacity planning"
short: "Cost attribution and capacity"
---
This design answers one question: *how do we combine utilization, scheduler and billing data in one warehouse, so that every GPU dollar is attributed to a team and a workload, the totals match the invoices, and capacity decisions rest on the same numbers?* [Design 24](#design-gpu-observability) produces the utilization series. [Design 19](#design-scheduling-impact) uses them for team scorecards. This design takes them to finance: ingestion of metrics, workload records and billing exports from every provider ([Design 18](#design-multicloud-fleet)), a cost per GPU-hour for each pool, attribution rules for direct and shared costs, reconciliation against invoices, monthly statements, and the forecasts and commitment decisions of capacity planning.

The short version: *one hourly fact table of GPU-hours, one cost per GPU-hour per pool, and attribution that always adds up to the invoice*. Hourly GPU-hours come from recording rules and scheduler events, joined to teams and workloads with ownership valid at that time. Billing arrives in FOCUS format where providers support it, and on-premises cost is amortized into the same shape. Each pool-hour's cost is split among the workloads that held its GPUs, and idle capacity is a visible line, not hidden in everyone's rate. A reconciliation check proves that attributed cost equals billed cost, month by month. Capacity planning uses the same tables: demand forecasts from submitted and pending GPU-hours, a utilization ceiling, and commitment levels chosen from the demand distribution.

### The use case and requirements

**Scenario.** The fleet spends about $4.1 million a month across on-premises H100s, a hyperscaler (part committed, part on demand) and a neocloud's B200s. Finance gets three invoices and a depreciation schedule, and divides the total by headcount to charge teams. Teams dispute the numbers because they can't see what they used. Meanwhile, the platform team is asked whether to sign a two-year commitment for 512 more GPUs, and nobody can say how much of today's demand is steady.

![$4.1M a month, divided by headcount](diagrams/designs/concepts/ds25-c1.svg)

**What the platform must deliver.**

- **Complete attribution:** every dollar assigned to a team and, where possible, a workload (a training run, a serving deployment, an evaluation), or to an explicit shared or idle bucket.
- **Reconciliation:** attributed totals per provider and month match the invoices (and the on-premises cost model) within 0.5%.
- **One cost per GPU-hour per pool:** comparable across providers, including commitments, amortized hardware and operating costs.
- **Statements teams can check:** monthly per-team reports that link to the workloads and hours behind each line.
- **Workload unit costs:** cost per training run and cost per million tokens served.
- **Capacity planning:** demand forecasts with uncertainty, required capacity at a target utilization, and commitment recommendations.
- **History:** at least three years, with ownership changes handled correctly.

**Out of scope:** how utilization is measured ([Design 24](#design-gpu-observability)) and how jobs are placed across providers ([Design 18](#design-multicloud-fleet)).

### Data sources and the model

**Scenario.** The first attempt joined monthly invoice totals with monthly average utilization. A team that used 400 GPUs for one week appeared to use 100 for the month, and the cost of the idle three weeks went to everyone else.

![Hourly model](diagrams/designs/concepts/ds25-c2.svg)

![Data sources and the model](diagrams/designs/ds25-1.svg)

**What the picture shows.** The sources, at the grain each one arrives, and the hourly model they're reshaped into.

- **Utilization:** hourly GPU-level allocation and activity, from the recording rules of [Design 24](#design-gpu-observability) (exported from the long-term store).
- **Scheduler:** Kueue workload records (admission, finish, preemption, queue, priority, requested resources) and Kubernetes ownership (pod → job → workload → namespace → team).
- **Inventory:** nodes, pools, GPU models, provider, region and purchase or contract references.
- **Billing:** provider exports in FOCUS format where available (the FinOps Foundation's common billing schema), provider-native exports otherwise, neocloud invoices, and commitment purchases.
- **On-premises cost:** depreciation of hardware, power, colocation, support contracts and operating staff, entered by finance per pool and month.
- **The grain:** one row per GPU per hour is the core fact. Costs arrive daily or monthly and are spread to hours. Ownership changes are effective-dated.

**The code.** The core tables (warehouse SQL, simplified).

```sql
-- one row per GPU per hour
CREATE TABLE fact_gpu_hour (
  hour_ts        TIMESTAMP,      -- start of the hour, UTC
  gpu_uuid       VARCHAR,
  node           VARCHAR,
  pool_id        VARCHAR,        -- joins to dim_pool (provider, model, region, contract)
  workload_uid   VARCHAR,        -- NULL when no pod held the GPU in this hour
  allocated_frac DOUBLE,         -- share of the hour the GPU was held (0..1)
  active_frac    DOUBLE,         -- share of the hour the GPU was active (0..1)
  healthy_frac   DOUBLE          -- share of the hour the GPU was schedulable
);
-- one row per workload, with ownership as of its start
CREATE TABLE dim_workload (
  workload_uid VARCHAR PRIMARY KEY, kind VARCHAR, name VARCHAR, namespace VARCHAR,
  team_id VARCHAR, project VARCHAR, experiment VARCHAR, priority VARCHAR,
  started_at TIMESTAMP, finished_at TIMESTAMP
);
-- team ownership of namespaces over time (slowly changing dimension, type 2)
CREATE TABLE dim_namespace_owner (
  namespace VARCHAR, team_id VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP   -- #!
);
-- billing lines, FOCUS-shaped
CREATE TABLE fact_cost_line (
  provider VARCHAR, charge_period_start TIMESTAMP, charge_period_end TIMESTAMP,
  billed_cost DECIMAL(18,6), effective_cost DECIMAL(18,6), resource_id VARCHAR,
  sku VARCHAR, charge_category VARCHAR, commitment_discount_id VARCHAR, tags JSON
);
```

**Going deeper.**

- **Hourly is the right grain:** jobs start and stop within days, and prices differ by hour (on-demand burst vs committed). Monthly averages hide exactly the variation attribution needs.
- **Effective-dated ownership:** when a namespace moves from one team to another, past hours stay with the old team. A type-2 dimension makes historical statements reproducible.

### Ingestion: landing, staging and marts

**Scenario.** Billing exports were loaded by a script that appended each day's file. When the hyperscaler restated three days of charges, the restated rows were added on top of the originals, and that month's cost was overstated by $180,000.

![Ingestion layers](diagrams/designs/concepts/ds25-c3.svg)

![Ingestion: landing, staging and marts](diagrams/designs/ds25-2.svg)

**What the picture shows.** Three layers, with partition replacement instead of appends.

- **Landing:** raw files and API extracts as received, in object storage, partitioned by source and date, never modified.
- **Staging:** typed, cleaned and conformed tables (FOCUS columns for billing, one schema for metrics). Each load *replaces* whole partitions (a day of metrics, a month of billing), so reloading is always safe.
- **Marts:** the hourly GPU fact, unit costs, attributed cost and capacity tables, rebuilt from staging by versioned SQL models (for example, dbt).
- **Late and restated data:** billing for a month keeps changing for weeks after it closes. The open month and the previous one are reloaded daily, and statements are final only after the provider's invoice closes.
- **Lineage:** every mart row can be traced to its staging partitions and landing files.

**The code.** Extracting hourly GPU-level allocation and activity from the metrics store for one day, replacing that day's partition (Python).

```python
import datetime as dt, requests, pandas as pd

PROM = "https://mimir.example.com/prometheus/api/v1/query_range"

def extract_day(day: dt.date, store) -> int:
    start = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc)
    q = {"start": start.timestamp(), "end": (start + dt.timedelta(days=1)).timestamp() - 1, "step": 3600}
    frames = []
    for name, expr in {
        "allocated_frac": "avg_over_time(gpu:allocated:bool[1h])",
        "active_frac":    "avg_over_time(gpu:active:bool[1h])",
    }.items():
        r = requests.get(PROM, params={**q, "query": expr}, timeout=300).json()["data"]["result"]
        rows = [{**s["metric"], "hour_ts": pd.to_datetime(float(t), unit="s", utc=True) - pd.Timedelta(hours=1),
                 name: float(v)} for s in r for t, v in s["values"]]
        frames.append(pd.DataFrame(rows))
    df = frames[0].merge(frames[1], on=["hour_ts", "UUID", "node", "pool", "namespace", "pod", "cluster"], how="outer")
    store.replace_partition("stg_gpu_hour", partition=str(day), frame=df.fillna({"active_frac": 0.0}))   #!
    return len(df)
```

**Going deeper.**

- **Use recording-rule outputs, not raw series:** extracting raw DCGM data for a month is slow and repeats the joins. The rule outputs already carry team, pool and pod.
- **Keep both billed and effective cost:** billed cost matches the invoice, and effective cost spreads commitment purchases over their term. Attribution uses effective cost, and reconciliation checks both.

### Cost per GPU-hour, comparable across providers

**Scenario.** A comparison showed the neocloud at $3.80 per GPU-hour and on-premises at "$0.15". That number was the power bill alone. Once depreciation, colocation, support and staff were added, it was $1.65.

![Cost per GPU-hour](diagrams/designs/concepts/ds25-c4.svg)

![Cost per GPU-hour, comparable across providers](diagrams/designs/ds25-3.svg)

**What the picture shows.** How each pool's cost per GPU-hour is built, so that pools can be compared and attributed the same way.

- **Hyperscaler committed:** the commitment's amortized (effective) cost per hour, divided by the GPUs it covers. Unused commitment hours stay in the pool's cost.
- **Hyperscaler on-demand:** the billed hourly rate for the instances that ran, including attached storage and network charged per instance.
- **Neocloud:** the contract price per GPU-hour, plus any separately billed storage and egress.
- **On-premises:** hardware depreciated over its expected life, plus power (measured or modeled per node), colocation, support contracts and an allocation of operating staff.
- **All-in, per healthy GPU-hour:** the pool's total monthly cost divided by its healthy GPU-hours. The cost of repair time is part of the rate, not a separate charge.

**The code.** On-premises cost per GPU-hour from finance inputs (Python).

```python
def onprem_gpu_hour(capex_per_node: float, life_months: int, node_kw: float, kwh_price: float, pue: float,
                    colo_per_node_month: float, support_per_node_month: float, staff_per_node_month: float,
                    gpus_per_node: int = 8, healthy_share: float = 0.97) -> dict:
    hours = 730
    parts = {
        "depreciation": capex_per_node / life_months,
        "power": node_kw * pue * hours * kwh_price,
        "colocation": colo_per_node_month,
        "support": support_per_node_month,
        "staff": staff_per_node_month,
    }
    per_node_month = sum(parts.values())
    per_gpu_hour = per_node_month / (gpus_per_node * hours * healthy_share)        # healthy hours only #!
    return {"per_gpu_hour": round(per_gpu_hour, 2), **{k: round(v / (gpus_per_node * hours * healthy_share), 3) for k, v in parts.items()}}

print(onprem_gpu_hour(capex_per_node=300_000, life_months=48, node_kw=10.2, kwh_price=0.09, pue=1.3,
                      colo_per_node_month=1_100, support_per_node_month=600, staff_per_node_month=500))
```

**Going deeper.**

- **The life assumption dominates:** depreciating over 36 or 60 months changes the on-premises rate by 40% or more. Agree it with finance once and record it in the pool dimension.
- **Compare like with like:** a B200 hour isn't an H100 hour. For planning, normalize by delivered work (tokens per second or training throughput from [Design 15](#design-eval-infra)) as well as by GPU-hour.

### Attribution: direct use, idle capacity and shared costs

**Scenario.** Splitting the total by headcount charged a three-person research team with one notebook GPU the same as the 40-person pre-training team. Switching to "by GPU-hours used" hid a 22% idle share inside everyone's rate, and nobody had a reason to reduce it.

![Attribution](diagrams/designs/concepts/ds25-c5.svg)

![Attribution: direct use, idle capacity and shared costs](diagrams/designs/ds25-4.svg)

**What the picture shows.** How each pool-hour's cost is split.

- **Direct use:** each workload is charged for the GPU-hours it held (allocated fraction × pool rate), whether or not it used them. Holding a GPU idle is a cost the team controls.
- **Idle capacity:** healthy GPU-hours that nobody held. Reported as its own line, owned by the platform. Optionally redistributed to teams in proportion to their guaranteed quota, because quotas are what the capacity was bought for.
- **Unhealthy hours:** GPUs in repair. Part of the pool rate (as above), so not a separate line.
- **Shared costs:** platform services (control planes, monitoring, registries), shared storage and network egress. Allocated by a driver that fits each cost: storage by bytes stored per namespace, egress by bytes sent, platform overhead as a percentage of direct cost.
- **Unattributed:** GPU-hours whose namespace had no owner at that time. A visible bucket with a target of zero.

**The code.** Hourly attribution of pool cost to workloads, with idle capacity as its own line (warehouse SQL).

```sql
WITH pool_hour AS (
  SELECT g.pool_id, g.hour_ts,
         SUM(g.healthy_frac)   AS healthy_gpu_h,
         SUM(g.allocated_frac) AS allocated_gpu_h,
         MAX(r.cost_per_gpu_h) AS rate
  FROM fact_gpu_hour g JOIN mart_pool_rate r ON r.pool_id = g.pool_id AND r.month = DATE_TRUNC('month', g.hour_ts)
  GROUP BY 1, 2
),
direct AS (
  SELECT g.hour_ts, g.pool_id, g.workload_uid,
         COALESCE(o.team_id, 'unattributed') AS team_id,
         SUM(g.allocated_frac) * MAX(p.rate) AS cost
  FROM fact_gpu_hour g
  JOIN pool_hour p USING (pool_id, hour_ts)
  LEFT JOIN dim_workload w ON w.workload_uid = g.workload_uid
  LEFT JOIN dim_namespace_owner o
         ON o.namespace = w.namespace AND g.hour_ts >= o.valid_from AND g.hour_ts < o.valid_to   -- owner at that hour
  WHERE g.workload_uid IS NOT NULL
  GROUP BY 1, 2, 3, 4
),
idle AS (
  SELECT hour_ts, pool_id, NULL AS workload_uid, 'platform:idle' AS team_id,
         GREATEST(healthy_gpu_h - allocated_gpu_h, 0) * rate AS cost                              -- #!
  FROM pool_hour
)
SELECT * FROM direct UNION ALL SELECT * FROM idle;
```

**Going deeper.**

- **Showback first, chargeback later:** publish attributed cost for a quarter before it affects budgets. Teams find attribution errors faster when nothing is at stake yet.
- **Preempted work is still use:** a workload preempted after 3 hours held the GPUs for 3 hours. If the platform's policy caused the waste, credit it from the platform's line, by explicit rule.

### Workload costs and unit economics

**Scenario.** A research lead asked what the last 70B training run cost. The answer took a week of spreadsheet work and still left out the 41 evaluation jobs and the failed first attempt.

![Workload costs](diagrams/designs/concepts/ds25-c6.svg)

![Workload costs and unit economics](diagrams/designs/ds25-5.svg)

**What the picture shows.** Costs rolled up from workloads to the things people ask about.

- **Runs and experiments:** workloads carry labels for project, experiment and run (set by the launchers of [Design 20](#design-distributed-training)), so a training run's cost includes its restarts, its evaluation jobs and its data-preparation jobs.
- **Serving:** cost per deployment per day, divided by tokens served (from the serving metrics of [Design 21](#design-llm-serving)), gives cost per million tokens, by model and product.
- **Waste views:** cost of failed runs, cost of GPU-hours held but idle, and cost lost to preemption, per team.
- **Label coverage:** the share of cost whose workloads carry project and experiment labels, reported per team with a target.

**The code.** Cost per run and cost per million tokens served (warehouse SQL).

```sql
-- cost of each training run, including restarts, evals and data prep labeled with the same run
SELECT w.project, w.experiment, COUNT(DISTINCT w.workload_uid) AS workloads,
       SUM(a.cost) AS cost_usd,
       SUM(CASE WHEN w.kind = 'eval' THEN a.cost ELSE 0 END) AS eval_cost_usd,
       SUM(CASE WHEN w.status = 'Failed' THEN a.cost ELSE 0 END) AS failed_cost_usd
FROM mart_attributed_cost a JOIN dim_workload w USING (workload_uid)
WHERE w.project = 'llm-70b'
GROUP BY 1, 2 ORDER BY cost_usd DESC;

-- cost per million output tokens, per serving deployment and day
SELECT a.day, w.name AS deployment, SUM(a.cost) AS cost_usd, MAX(t.output_tokens) AS tokens,
       SUM(a.cost) / NULLIF(MAX(t.output_tokens), 0) * 1e6 AS usd_per_m_tokens               -- #!
FROM mart_attributed_cost_daily a
JOIN dim_workload w USING (workload_uid)
JOIN fact_serving_tokens_daily t ON t.deployment = w.name AND t.day = a.day
WHERE w.kind = 'serving'
GROUP BY 1, 2;
```

**Going deeper.**

- **Labels are the bottleneck:** cost per experiment is only as good as the labels on workloads. Make the launchers set them by default, and reject workloads without a project label in production queues.
- **Unit costs move with utilization:** cost per million tokens falls as a deployment gets busier. Report it with the deployment's utilization next to it.

### Reconciliation and data quality

**Scenario.** The first statements attributed $3.62 million in a month whose invoices and cost model totaled $4.08 million. The $460,000 gap was a neocloud invoice line for storage that no GPU-hour carried, and two days of missing metrics.

![Reconciliation](diagrams/designs/concepts/ds25-c7.svg)

![Reconciliation and data quality](diagrams/designs/ds25-6.svg)

**What the picture shows.** Checks that make the numbers trustworthy before anyone sees them.

- **Totals:** attributed cost (direct + idle + shared + unattributed) per provider and month equals billed or effective cost within 0.5%. A larger gap blocks publication.
- **Coverage:** every healthy GPU-hour in the inventory has a fact row. Missing hours are filled from scheduler records where possible and flagged otherwise.
- **Freshness:** metrics within 6 hours, scheduler records within 1 hour, billing within the provider's export delay.
- **Integrity:** allocated fraction ≤ healthy fraction ≤ 1, every workload resolves to an owner, no GPU allocated to two workloads in one hour.
- **Anomalies:** day-over-day changes in a team's cost beyond a threshold are flagged for review before statements go out.

**The YAML.** Data tests on the marts (dbt).

```yaml
models:
- name: fact_gpu_hour
  tests:
  - dbt_utils.expression_is_true: { expression: "allocated_frac <= healthy_frac + 0.001" }
  - dbt_utils.expression_is_true: { expression: "healthy_frac BETWEEN 0 AND 1" }
  - dbt_utils.unique_combination_of_columns: { combination_of_columns: [hour_ts, gpu_uuid] }
  - dbt_utils.recency: { datepart: hour, field: hour_ts, interval: 6 }
- name: mart_reconciliation_monthly
  columns:
  - name: abs_gap_pct
    tests:
    - dbt_utils.accepted_range: { max_value: 0.005, config: { severity: error } }   # blocks statements #!
  - name: unattributed_share
    tests:
    - dbt_utils.accepted_range: { max_value: 0.01, config: { severity: warn } }
```

**Going deeper.**

- **Reconcile to the invoice, not to a forecast:** the check compares with the provider's own billed total. When they disagree, the warehouse is wrong until shown otherwise.
- **Missing metrics aren't zero:** an hour with no data means "unknown", not "idle". Fill from scheduler allocation records, and mark the activity as unknown.

### Statements, budgets and forecasts for teams

**Scenario.** Teams received a single number per month in an email. When the vision team's cost doubled, it took two weeks to find that a forgotten evaluation sweep had held 64 GPUs over a holiday.

![Team statements](diagrams/designs/concepts/ds25-c8.svg)

![Statements, budgets and forecasts for teams](diagrams/designs/ds25-7.svg)

**What the picture shows.** What each team gets, and when.

- **Monthly statement:** direct cost by pool and workload kind, share of idle and shared costs (if chargeback applies), and the top workloads by cost, each linked to its GPU-hours.
- **Daily view:** month-to-date cost, forecast to month end and budget, in a dashboard backed by the same marts.
- **Budget alerts:** a forecast above budget, or a single workload costing more than a threshold per day, notifies the team's owner (the routing of [Design 24](#design-gpu-observability)).
- **Explanations:** month-over-month changes are broken down into usage change, rate change (pool mix) and idle share change, so a doubling has a reason attached.

**The code.** Explaining a team's month-over-month cost change by volume, rate and idle (Python).

```python
def explain_change(prev: dict, cur: dict) -> dict:
    """prev/cur: {'gpu_h': held GPU-hours, 'rate': blended $/GPU-h, 'idle_share_cost': $}."""
    volume = (cur["gpu_h"] - prev["gpu_h"]) * prev["rate"]                 # more or fewer hours #!
    rate = (cur["rate"] - prev["rate"]) * cur["gpu_h"]                      # pool mix, prices
    idle = cur["idle_share_cost"] - prev["idle_share_cost"]
    total = (cur["gpu_h"] * cur["rate"] + cur["idle_share_cost"]) - (prev["gpu_h"] * prev["rate"] + prev["idle_share_cost"])
    return {"total": round(total), "volume": round(volume), "rate": round(rate), "idle": round(idle)}

print(explain_change({"gpu_h": 61_000, "rate": 2.31, "idle_share_cost": 18_000},
                     {"gpu_h": 118_000, "rate": 2.44, "idle_share_cost": 21_000}))
```

**Going deeper.**

- **Statements are reproducible:** a closed month's statement is stored with the versions of the SQL models and rates that produced it. Corrections are issued as adjustments in a later month, not silent rewrites.
- **Show what the team can change:** held-but-idle hours and failed runs are the controllable parts. Put them at the top.

### Capacity planning: forecasting demand

**Scenario.** The platform added 256 GPUs after a quarter of long queues. Two months later, queues were long again. The forecast had used last quarter's *consumed* GPU-hours, which were capped by the capacity that existed, not by what teams wanted.

![Demand forecast](diagrams/designs/concepts/ds25-c9.svg)

![Capacity planning: forecasting demand](diagrams/designs/ds25-8.svg)

**What the picture shows.** Demand measured without the capacity cap, forecast with uncertainty, and turned into a capacity requirement.

- **Demand, not consumption:** consumed GPU-hours plus pending GPU-hours (admitted later or never) from Kueue, plus demand turned away (jobs not submitted because quotas were full, estimated from team plans).
- **Known plans:** large planned runs and launches from team roadmaps, added as discrete events on top of the trend.
- **Forecast:** a trend with weekly seasonality on weekly demand, with prediction intervals, per GPU type.
- **Required capacity:** forecast demand divided by the target utilization (for example, 80%, from the healthy ceiling of [Design 19](#design-scheduling-impact)), plus headroom for repairs.
- **Lead times:** on-premises hardware takes months, commitments take weeks, on-demand takes minutes. The plan says what must be ordered now to cover the 80th-percentile demand at each lead time.

**The code.** A simple demand forecast with intervals and the resulting capacity requirement (Python).

```python
import numpy as np

def forecast_capacity(weekly_demand_gpu_h: np.ndarray, weeks_ahead: int = 26,
                      target_util: float = 0.80, repair_share: float = 0.03, planned: dict | None = None) -> dict:
    """weekly_demand_gpu_h: consumed + pending GPU-hours per week (not capped by capacity)."""
    t = np.arange(len(weekly_demand_gpu_h))
    y = np.log(weekly_demand_gpu_h)
    slope, intercept = np.polyfit(t, y, 1)                                  # log-linear growth
    resid = y - (intercept + slope * t)
    sigma = resid.std(ddof=2)
    future = np.arange(len(t), len(t) + weeks_ahead)
    mean = np.exp(intercept + slope * future)
    spread = sigma * np.sqrt(1 + (future - t.mean()) ** 2 / ((t - t.mean()) ** 2).sum())
    p50, p80 = mean, np.exp(intercept + slope * future + 0.84 * spread)
    for week, gpu_h in (planned or {}).items():
        p50[week] += gpu_h; p80[week] += gpu_h                              # known large runs
    to_gpus = lambda gpu_h: gpu_h / (7 * 24) / target_util / (1 - repair_share)   #!
    return {"weekly_growth_pct": round((np.exp(slope) - 1) * 100, 2),
            "gpus_needed_p50": np.ceil(to_gpus(p50)).astype(int), "gpus_needed_p80": np.ceil(to_gpus(p80)).astype(int)}
```

**Going deeper.**

- **Forecast per GPU type and pool:** demand for H100s and B200s grows differently, and some workloads can't move between them.
- **Compare forecasts with outcomes:** each quarter, score the previous forecasts. Persistent under-forecasting usually means hidden demand isn't being counted.

### Commitments: how much to reserve

**Scenario.** The hyperscaler offered a two-year commitment for 512 GPUs at about a third of the on-demand price. Half of the platform team wanted all 512. The other half pointed out that demand had swung by 300 GPUs within a single month.

![Commitments](diagrams/designs/concepts/ds25-c10.svg)

![Commitments: how much to reserve](diagrams/designs/ds25-9.svg)

**What the picture shows.** Choosing a commitment level from the distribution of hourly demand.

- **The trade-off:** a committed GPU-hour is paid whether used or not. An on-demand (or neocloud burst) GPU-hour costs more but only when used.
- **The rule:** one more committed GPU pays off if demand exceeds it in more than (committed price ÷ on-demand price) of the hours. With committed at $2.50 and on-demand at $6.90, that's 36%, so commit up to the 64th percentile of hourly demand.
- **Inputs:** hourly demand for the pool's GPU type over the commitment horizon (from the forecast, as a distribution), prices, and constraints (minimum commitment sizes, regions).
- **Layering:** on-premises and existing commitments cover the base, new commitments the next layer, and on-demand or neocloud burst the peaks ([Design 18](#design-multicloud-fleet)).
- **Review:** each commitment's realized utilization is tracked. Unused commitment hours are idle capacity in the attribution.

**The code.** Choosing a commitment level from simulated hourly demand (Python).

```python
import numpy as np

def commitment_level(hourly_demand: np.ndarray, committed_price: float, on_demand_price: float,
                     already_covered: int = 0) -> dict:
    """hourly_demand: GPUs needed per hour over the horizon (forecast samples); already_covered: base capacity."""
    beyond_base = np.maximum(hourly_demand - already_covered, 0)
    critical = (on_demand_price - committed_price) / on_demand_price           # 0.64 for $2.50 vs $6.90
    level = int(np.quantile(beyond_base, critical))                             # demand exceeds it 36% of hours #!
    def cost(c):
        return (c * committed_price * len(beyond_base)
                + np.maximum(beyond_base - c, 0).sum() * on_demand_price)
    return {"commit_gpus": level, "critical_ratio": round(critical, 2),
            "cost_at_level": round(cost(level)), "cost_commit_none": round(cost(0)),
            "cost_commit_512": round(cost(512))}
```

**Going deeper.**

- **The forecast's uncertainty matters more than its mean:** two forecasts with the same mean but different spread give different commitment levels. Use the full distribution.
- **Flexibility has value:** commitments that can move between instance types or regions reduce the risk, and are worth a slightly higher price. The same calculation shows how much.

### Running it: the operating cadence

**Scenario.** The first version of the warehouse answered every question, but only when a data engineer ran the right query. Finance still waited until mid-month for numbers, and the capacity review happened when someone remembered.

![Operating cadence](diagrams/designs/concepts/ds25-c11.svg)

![Running it: the operating cadence](diagrams/designs/ds25-10.svg)

**What the picture shows.** What runs when, and who owns it.

- **Daily:** ingestion, reloads of open months, data tests and reconciliation, month-to-date and forecast views per team, and budget alerts.
- **Monthly:** the close after providers' invoices arrive, frozen statements per team, and a review of pool rates with finance.
- **Quarterly:** a new demand forecast scored against the previous one, the capacity plan per GPU type, and commitment recommendations, reviewed with finance and team leads.
- **Ownership:** finance owns rates, budgets and the on-premises cost inputs. The platform team owns the data pipeline, the attribution rules and the idle line.
- **One set of tables:** run costs, bill explanations, idle capacity and commitment decisions are all queries over the same marts, so answers agree.

**Going deeper.**

- **Publish the rules with the numbers:** attribution drivers, rates and their versions are documented next to the statements, and changes are announced before they apply.
- **Close the loop with scheduling:** idle capacity found here becomes the next lever in the scheduling program ([Design 19](#design-scheduling-impact)), and long queues found there feed the demand forecast.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds25-c12.svg)

- **Phase 1, the model (weeks 1–4):** the hourly GPU fact from recording rules and scheduler records, inventory and ownership dimensions, and billing ingestion in FOCUS shape for each provider.
- **Phase 2, rates and attribution (weeks 4–8):** pool rates (with finance's on-premises inputs), direct and idle attribution, shared-cost drivers, reconciliation checks.
- **Phase 3, showback (weeks 8–14):** monthly statements and daily views per team, workload and unit costs, and a quarter of showback before any chargeback.
- **Phase 4, planning (weeks 10–16):** demand forecasts, capacity requirements and commitment recommendations, reviewed quarterly with finance.

**Trade-offs we accepted.**

- **Hourly grain:** more data and compute than monthly, needed for correct attribution.
- **Idle as a visible line:** uncomfortable for the platform team, and the reason idle capacity gets reduced.
- **Allocation, not activity, is charged:** teams pay for GPUs they hold, even if their code leaves them idle.
- **Statements are final only after invoices close:** teams see estimates for the current and previous month.

**Open questions.**

- **Charging for priority:** should high-priority or preempting workloads pay more per GPU-hour than preemptible ones?
- **Carbon:** should statements include energy and emissions per team, from the same power data?
- **Cross-provider normalization:** how should a B200 hour be compared with an H100 hour for planning, by throughput on representative workloads or by price?
- **Token-level billing:** as billing standards add AI and token-level units, should serving cost move from GPU-hours to tokens end to end?

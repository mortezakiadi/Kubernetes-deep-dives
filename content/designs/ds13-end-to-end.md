---
id: design-end-to-end
title: "Design 13: End-to-end execution, from idea to production-scale runs"
short: "End-to-end execution"
---
This design answers one question: *how do research, systems, infrastructure and data teams work together to get experiments from an idea to production-scale runs, with training pipelines that are reliable, data and verifiers whose quality is known, and successful experiments turned into repeatable, scalable recipes?* It ties the earlier designs together: capacity and queues ([Design 02](#design-multicluster)), training infrastructure ([Design 03](#design-training-inference)), recipes ([Design 06](#design-post-training)), scaling ([Design 07](#design-scaling-rl)), verifiers ([Design 10](#design-verification)), mixtures ([Design 11](#design-multi-task)) and analysis ([Design 12](#design-experiment-analysis)).

The short version: *the path from idea to production is a pipeline with explicit stages, contracts and gates, not a series of favours*. Each stage (proxy, scale-up, production run, recipe release) has an entry checklist, an owner and a gate. The hand-offs between teams are **contracts**: a run spec for infrastructure, a data contract for data, a verifier contract for rewards, and a recipe manifest for whoever runs it next. Big runs start only after **pre-flight checks** pass; while they run, failures are classified and handled automatically; and a run that worked becomes a **recipe release** that CI can re-run.

### The use case and requirements

**Scenario.** A small-scale RL experiment shows a clear gain in March. It reaches a production-scale run in June. In between: the data it used turned out to include eval questions, its verifier couldn't handle the throughput of a large run, the first two large runs died on infrastructure problems nobody owned, and the final recipe existed only in a researcher's branch. When she went on leave, nobody could reproduce it.

![March to June](diagrams/designs/concepts/ds13-c1.svg)

**What the platform must do.**

- **Shorten idea-to-scale time:** weeks, not months, for an experiment that has earned it.
- **Make hand-offs explicit:** each team knows what it receives, what it promises and how quality is checked.
- **Make big runs reliable:** problems found before the run starts, failures during the run handled in minutes.
- **Guarantee data and verifier quality:** checked automatically at every stage, not discovered after training.
- **Turn experiments into recipes:** a successful run becomes a versioned, reproducible recipe anyone can re-run.
- **Keep a record:** decisions, incidents and results in one place.

**Out of scope:** the technical content of each component, which the earlier designs cover.

### From idea to production: stages and gates

**Scenario.** Nobody can say what "ready for a big run" means. Some experiments jump straight to 512 GPUs; others sit at proxy scale for months waiting for someone to decide.

![Stages and gates](diagrams/designs/concepts/ds13-c2.svg)

![From idea to production: stages and gates](diagrams/designs/ds13-1.svg)

**What the picture shows.** The stages an experiment moves through, with a gate between each.

- **Idea and proxy:** a declared ablation at small scale ([Design 12](#design-experiment-analysis)). Gate: a confirmed effect with an interval, on the primary metric and guardrails.
- **Scale-up:** the scaling ladder ([Design 07](#design-scaling-rl)): longer, larger, more steps. Gate: stable curve fit, understood step-time breakdown, pre-flight checks passing.
- **Production run:** the full budget on reserved capacity. Gate: multi-family no-regression evaluation ([Design 11](#design-multi-task)).
- **Recipe release:** the run's configuration, data and verifier versions packaged as a recipe that CI can reproduce. Gate: a reproduction at reference scale matches.
- **Owners per stage:** research leads the first two; the run's operational owner (from systems) leads the production run; the recipe owner keeps the release reproducible.

**Going deeper.**

- **Gates are light at the start and heavy at the end:** a proxy run needs a recipe file; a production run needs everything. Heavy gates early kill exploration.
- **Time in each stage is a metric:** where experiments wait is where the process needs work.

### Contracts between teams

**Scenario.** Infrastructure learns about a 1,024-GPU run two days before it should start. The data team learns that their dataset is being used for RL when the run is already in progress. The verifier team learns its service is in the critical path when it falls over.

![Contracts between teams](diagrams/designs/concepts/ds13-c3.svg)

![Contracts between teams](diagrams/designs/ds13-2.svg)

**What the picture shows.** The hand-offs between teams, each a document with a schema and an owner.

- **Run spec (research → infrastructure):** size, duration, topology, storage and network needs, start window, priority. Submitted when the scale-up stage starts, not when the run starts.
- **Data contract (data → research):** schema, versions, freshness, decontamination status, licenses, PII handling and validation results.
- **Verifier contract (rewards → research):** versions, measured precision per slice, throughput limits and on-call ([Design 10](#design-verification)).
- **Recipe manifest (research → everyone):** the complete, pinned description of how to run it again.
- **Incident and decision log (everyone):** what went wrong, what was decided and why.

**The YAML.** A run spec submitted to infrastructure at the start of scale-up.

```yaml
# runspecs/assistant-v14-rl-prod.yaml
run: assistant-v14-rl-prod
owner: { research: alice, operations: rl-ops-oncall }
stage: production
size: { nodes: 128, gpus_per_node: 8, accelerator: h100 }
duration_estimate: { gpu_hours: 180000, wall_days: 8 }
topology: { same_fabric_block: true }                  # see Design 02 #!
storage: { checkpoint_tb: 4.2, checkpoint_interval_min: 30, dataset_tb: 1.1 }
dependencies:
  verifiers: [math-final-answer@3.2.0, code-tests@5.1.0]   # must meet throughput below
  verifier_throughput_rps: 1800
  sandbox_pool_peak: 4000
window: { earliest_start: 2026-12-01, latest_start: 2026-12-08 }
priority: production
```

**Going deeper.**

- **Contracts are versioned and validated by machines:** a run spec missing a field or a data contract missing decontamination status is rejected by CI, not by a meeting.
- **Every contract names an on-call:** a hand-off without a contact for 3 a.m. isn't a hand-off.

### Data quality: contracts, validation and lineage

**Scenario.** The March experiment's gain was partly from eval contamination: a scraped dataset contained a third of the benchmark's questions, paraphrased.

![Data quality gate](diagrams/designs/concepts/ds13-c4.svg)

![Data quality: contracts, validation and lineage](diagrams/designs/ds13-3.svg)

**What the picture shows.** The checks every dataset passes before it can be used in a recipe.

- **Schema and format:** fields present, chat templates valid, lengths within limits, languages labeled.
- **Deduplication:** exact and near-duplicate removal within and across sources.
- **Decontamination:** n-gram and embedding-similarity checks against every eval set the organization uses, including paraphrase detection.
- **Safety and compliance:** PII detection and removal, license recorded, sensitive-content filters.
- **Quality signals:** per-source quality scores and spot-check samples reviewed by people.
- **Versioned snapshots and lineage:** every dataset version is immutable, with the code and inputs that produced it, so a recipe pins exact data.

**The code.** Validation run on every new dataset version before it's published (Python).

```python
import hashlib
from datasketch import MinHash, MinHashLSH

def ngram_minhash(text, n=8, perm=128):
    m = MinHash(num_perm=perm)
    toks = text.lower().split()
    for i in range(max(1, len(toks) - n + 1)):
        m.update(" ".join(toks[i:i + n]).encode())
    return m

def build_eval_index(eval_texts, threshold=0.5):
    lsh = MinHashLSH(threshold=threshold, num_perm=128)
    for i, t in enumerate(eval_texts):
        lsh.insert(f"eval-{i}", ngram_minhash(t))
    return lsh

def validate(rows, eval_index, max_len=32768):
    report = {"rows": len(rows), "schema_errors": 0, "exact_dupes": 0, "contaminated": 0}
    seen = set()
    for r in rows:
        if not r.get("messages") or r["messages"][-1]["role"] != "assistant":
            report["schema_errors"] += 1
        h = hashlib.sha256(str(r["messages"]).encode()).hexdigest()
        report["exact_dupes"] += h in seen; seen.add(h)
        text = " ".join(m["content"] for m in r["messages"])
        if eval_index.query(ngram_minhash(text)):
            report["contaminated"] += 1                                   # blocks publication #!
    report["ok"] = report["schema_errors"] == 0 and report["contaminated"] == 0
    return report
```

**Going deeper.**

- **Decontaminate against all evals, every time an eval is added:** a new eval set triggers re-checking existing datasets, not just new ones.
- **Data bugs look like model behaviors:** a wrong chat template in one source shows up as an odd habit after training; schema checks per source are cheap insurance.

### Verifier and environment readiness

**Scenario.** The verifier that served a 32-GPU proxy run at 40 requests per second gets 1,800 per second in the production run. Latency rises from 200 ms to 9 s, rollouts stall, and GPU utilization falls to 20%.

![Verifier readiness](diagrams/designs/concepts/ds13-c5.svg)

![Verifier and environment readiness](diagrams/designs/ds13-4.svg)

**What the picture shows.** What must be true of rewards and environments before a big run.

- **Quality gate:** precision per slice above threshold on current policy outputs, fuzzing clean ([Design 10](#design-verification)).
- **Throughput test:** a load test at the run's expected peak request rate and payload sizes, with latency targets.
- **Capacity reservation:** sandbox pools and judge GPUs reserved and pre-warmed for the run's duration.
- **Failure behavior:** what happens when the verifier is slow or down: rollouts pause and retry, never default to "pass".
- **Version pinning:** the run pins verifier and environment image digests; no changes during the run without an explicit decision.

**The code.** A pre-flight check the launcher runs before a production run (Python).

```python
import asyncio, time, statistics, httpx

async def load_test(url, payloads, rps: int, seconds: int = 60):
    lat, errors = [], 0
    async with httpx.AsyncClient(timeout=30) as client:
        async def one(p):
            nonlocal errors
            t0 = time.perf_counter()
            try:
                r = await client.post(url, json=p); r.raise_for_status()
                lat.append(time.perf_counter() - t0)
            except Exception:
                errors += 1
        for _ in range(seconds):
            tick = time.perf_counter()
            await asyncio.gather(*(one(payloads[i % len(payloads)]) for i in range(rps)))
            await asyncio.sleep(max(0, 1 - (time.perf_counter() - tick)))
    p99 = statistics.quantiles(lat, n=100)[98] if lat else float("inf")
    return {"p99_s": p99, "error_rate": errors / (rps * seconds)}

def preflight(runspec, verifier_registry):
    results = {}
    for v in runspec["dependencies"]["verifiers"]:
        meta = verifier_registry.get(v)
        results[v] = {
            "precision_ok": all(s["precision"] >= s["gate"] for s in meta["slices"]),
            "load": asyncio.run(load_test(meta["url"], meta["sample_payloads"],
                                          rps=runspec["dependencies"]["verifier_throughput_rps"])),
        }
        results[v]["ok"] = results[v]["precision_ok"] and results[v]["load"]["p99_s"] < 2.0 \
            and results[v]["load"]["error_rate"] < 0.001                                     #!
    return results
```

**Going deeper.**

- **Load-test with realistic payloads:** policy outputs late in a run are longer than early ones; test with long outputs.
- **Fail closed:** a reward service error must never be read as a pass; it's the cheapest reward hack there is.

### Capacity planning for big runs

**Scenario.** A production run needs 1,024 GPUs for eight days. Two days in, the cluster's reserved block expires; nobody had checked the reservation's end date against the run's estimate.

![Capacity plan](diagrams/designs/concepts/ds13-c6.svg)

![Capacity planning for big runs](diagrams/designs/ds13-5.svg)

**What the picture shows.** Turning a run spec into reserved, ready capacity.

- **Estimate from the ladder:** GPU hours and wall-clock from rung measurements ([Design 07](#design-scaling-rl)), plus a margin for restarts and evals.
- **Match capacity sources:** reserved blocks for the run's duration with margin, on-demand only for overflow ([Design 02](#design-multicluster)).
- **Reserve in the queue:** the run's queue gets guaranteed quota for the window; other work borrows around it.
- **Reserve the dependencies:** verifier, sandbox and judge capacity, storage throughput for checkpoints.
- **Dry run:** a short run on the actual reserved nodes before the window starts, to catch hardware and topology problems.

**The code.** A capacity estimate with margins from ladder measurements (Python).

```python
def capacity_plan(tokens_total, tokens_per_gpu_hour, gpus, restart_rate_per_day=0.3,
                  restart_cost_h=0.5, eval_overhead=0.06, safety_margin=0.15):
    gpu_hours = tokens_total / tokens_per_gpu_hour
    wall_h = gpu_hours / gpus
    days = wall_h / 24
    lost_h = days * restart_rate_per_day * restart_cost_h             # expected restart losses
    wall_h = (wall_h + lost_h) * (1 + eval_overhead)
    reserve_h = wall_h * (1 + safety_margin)                          # reservation must cover this #!
    return {"gpu_hours": round(gpu_hours), "expected_wall_days": round(wall_h / 24, 1),
            "reserve_days": round(reserve_h / 24, 1)}

print(capacity_plan(tokens_total=4.0e11, tokens_per_gpu_hour=2.6e6, gpus=1024))
```

**Going deeper.**

- **Track estimate accuracy:** compare each run's estimate with its actual; bias in estimates is fixed by calibrating tokens-per-GPU-hour from the latest rung, not by bigger margins.
- **Reservations are commitments:** an unused reservation costs as much as a used one; release early if the run finishes early.

### Reliable pipelines: pre-flight, failure classes and recovery

**Scenario.** The first production run died on day one: a typo in a data path that the 32-GPU proxy never hit. The second died on day three: a node's GPU fault was retried as if it were a code bug, failing the whole job after the retry limit.

![Failure classes](diagrams/designs/concepts/ds13-c7.svg)

![Reliable pipelines: pre-flight, failure classes and recovery](diagrams/designs/ds13-6.svg)

**What the picture shows.** How a production run avoids preventable failures and handles the rest.

- **Pre-flight smoke run:** a few steps of the production configuration on a small slice of the reserved nodes, touching every code path: data loading from every source, a checkpoint save and load, a verifier call, a weight sync and an eval.
- **Failure classes:** infrastructure failures (GPU faults, node loss, network) are retried on other nodes without counting against the job; code and configuration errors fail fast and page the run owner; data errors quarantine the shard and continue or stop by policy.
- **Checkpoint verification:** every checkpoint is test-loaded once after it's written; a corrupt checkpoint is found then, not when needed.
- **Watchdogs:** stalled-step detection (no progress for N minutes), NaN detection and health-signal alerts ([Design 06](#design-post-training)).
- **On-call and runbooks:** a named operations owner per production run, with runbooks for each failure class.

**The YAML.** A pod failure policy that separates infrastructure failures from code errors (Kubernetes Job API).

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: assistant-v14-preflight
  namespace: post-training
  labels: { kueue.x-k8s.io/queue-name: rl-production }
spec:
  completions: 16
  parallelism: 16
  completionMode: Indexed
  backoffLimit: 6
  podFailurePolicy:
    rules:
    - action: Ignore                         # node drained / GPU fault: retry, don't count #!
      onPodConditions:
      - type: DisruptionTarget
    - action: FailJob                        # exit code 42 = config or code error: stop now #!
      onExitCodes:
        containerName: trainer
        operator: In
        values: [42]
    - action: Count                          # anything else counts against backoffLimit
      onExitCodes:
        containerName: trainer
        operator: NotIn
        values: [0, 42]
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: trainer
        image: registry.example.com/rl/trainer@sha256:1f0c...
        args: ["--recipe=/recipes/prod.yaml", "--preflight", "--steps=20",
               "--touch-all-sources", "--test-checkpoint-roundtrip", "--test-verifiers"]
        resources: { limits: { nvidia.com/gpu: 8 } }
```

**Going deeper.**

- **Exit codes are an API:** the trainer exits with distinct codes for config errors, data errors and internal errors, so the platform can react correctly.
- **Count failures by class over time:** a rising rate of one infrastructure class on certain nodes is a hardware problem to fix, not noise to retry.

### Scaling a recipe up

**Scenario.** The recipe that worked at 64 GPUs used a batch of 256 prompts. At 1,024 GPUs, someone multiplied the batch by 16 and kept the learning rate. The run was stable and learned much less per sample.

![Scale-up review](diagrams/designs/concepts/ds13-c8.svg)

![Scaling a recipe up](diagrams/designs/ds13-7.svg)

**What the picture shows.** What has to change, and what must not, when a recipe moves from proxy to production scale.

- **Keep the science fixed:** objective, reward, data mixture and filtering stay the same unless the ladder showed a reason to change them.
- **Change the systems deliberately:** parallelism layout, micro-batch sizes, number of rollout engines and weight-sync configuration are re-tuned for the new size.
- **Batch and learning rate together:** if the global batch changes, the learning rate and number of steps are re-validated at a rung, not assumed.
- **Length and time budgets:** maximum response length, timeouts and checkpoint intervals reflect the longer, larger run.
- **The scale-up diff is reviewed:** every key that differs between the proxy recipe and the production recipe is listed and justified.

**The code.** A scale-up diff check that separates "systems" keys from "science" keys (Python).

```python
SYSTEMS_KEYS = {"parallelism", "micro_batch", "rollout.engines", "weight_sync", "checkpoint_interval",
                "timeouts", "resources"}

def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        out.update(flatten(v, key) if isinstance(v, dict) else {key: v})
    return out

def scale_up_review(proxy: dict, prod: dict, justified: set):
    a, b = flatten(proxy), flatten(prod)
    changed = {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
    science = {k for k in changed if not any(k.startswith(s) for s in SYSTEMS_KEYS)}
    unjustified = science - justified                                    # science changes need a reason #!
    return {"systems_changes": sorted(changed - science), "science_changes": sorted(science),
            "blocked": sorted(unjustified)}
```

**Going deeper.**

- **Scale-up surprises go back down the ladder:** if production behaves differently from the last rung, reproduce at the rung before adjusting the production run.
- **Systems changes can change science:** different parallelism or precision changes numerics; the mismatch checks of [Design 07](#design-scaling-rl) run after any layout change.

### Running the run: reviews, incidents and decisions

**Scenario.** On day five of a production run, three people noticed entropy dropping. Each assumed someone else was handling it. On day seven, the run collapsed.

![Running the run](diagrams/designs/concepts/ds13-c9.svg)

![Running the run: reviews, incidents and decisions](diagrams/designs/ds13-8.svg)

**What the picture shows.** The operational rhythm of a production run.

- **Roles:** a research owner (is it learning the right thing?) and an operations owner (is it running well?), both named in the run spec.
- **Daily run review:** fifteen minutes on health signals, evals at the latest checkpoints, throughput, cost and open issues.
- **Incident process:** any automatic alert or human concern opens an incident with an owner; decisions (pause, roll back, change a setting) are recorded with the evidence.
- **Change control during the run:** live changes (learning rate, staleness limit, mixture) go through the run's change log, never directly.
- **Post-run review:** what worked, what broke, what the next run should change; the results feed the recipe and the runbooks.

**Going deeper.**

- **The decision log is part of the recipe release:** a recipe that needed three mid-run interventions isn't a recipe yet; those interventions become part of it or become fixed problems.
- **Automatic controls first:** controllers from [Design 09](#design-off-policy) handle common problems; people handle what's new.

### From successful run to repeatable recipe

**Scenario.** The production run succeeded. Three months later, the next version starts from "what we did last time", and nobody can reconstruct the exact data mixture or the verifier version.

![Recipe release](diagrams/designs/concepts/ds13-c10.svg)

![From successful run to repeatable recipe](diagrams/designs/ds13-9.svg)

**What the picture shows.** What a recipe release contains, and how it stays reproducible.

- **Pinned inputs:** base checkpoint, dataset snapshots, verifier and environment versions, container image digests.
- **Configuration:** the full recipe and schedule, including the systems settings for each supported scale.
- **Evidence:** eval results with intervals, the scale-up record, the decision log and known issues.
- **Runbook:** how to run it, what to watch, what to do when known problems appear.
- **Reproduction in CI:** a scheduled job re-runs the recipe at reference scale on current infrastructure and compares with the recorded results, so infrastructure or library drift is caught.

**The YAML.** A recipe release manifest, the unit a future run starts from.

```yaml
# releases/assistant-v14-rl@1.0.0/manifest.yaml
release: assistant-v14-rl
version: 1.0.0
base_checkpoint: registry://assistant-v14/sft@sha256:7c1e...
data:
  - datasets://math-verified@v6 # snapshot id: ds-2026-11-02-a91f
  - datasets://code-tests@v9
  - envs://swe-tasks@v3
verifiers: [math-final-answer@3.2.0, code-tests@5.1.0]
images: { trainer: "sha256:1f0c...", vllm: "sha256:88ab...", verifier-math: "sha256:5e1d..." }
recipe: recipes/assistant-v14/rl-math-code.yaml@3f9a1c7
scales:
  reference: { gpus: 64,   steps: 300,  expected: { math-hard: [61.0, 63.4] } }   # CI reproduction band #!
  production: { gpus: 1024, steps: 4200 }
evidence:
  evals: results://assistant-v14-rl-prod/final
  decision_log: runs/assistant-v14-rl-prod/decisions.md
known_issues: ["entropy dips at ~step 2,400; controller handles it (see runbook §3)"]
owner: team-reasoning
```

**Going deeper.**

- **Reproduction bands come from seed variance:** the CI check passes when the reference run lands inside the interval measured for that recipe ([Design 12](#design-experiment-analysis)), not when it matches a single number.
- **Recipes have a lifecycle:** active, superseded or deprecated; a deprecated recipe keeps its manifest so old results remain explainable.

### The platform on Kubernetes

**Scenario.** Each stage of the path uses a different set of scripts, and the state of an experiment lives in a mix of tickets, chats and branches.

![End-to-end platform](diagrams/designs/concepts/ds13-c11.svg)

![The platform on Kubernetes](diagrams/designs/ds13-10.svg)

**What the picture shows.** The components that carry an experiment from idea to recipe.

- **Experiment tracker:** the stage, owners, contracts and gates for each experiment, linked to its runs.
- **Contract validators in CI:** run specs, data contracts, verifier contracts and recipe manifests checked on every change.
- **Launcher:** turns recipes and run specs into Kueue-managed jobs, runs pre-flight checks, and refuses launches that fail them.
- **Data platform:** versioned snapshots, validation and lineage.
- **Reward platform:** verifier registry and pools ([Design 10](#design-verification)).
- **Training infrastructure:** queues, reserved capacity, JobSets and RayJobs ([Designs 02](#design-multicluster), [03](#design-training-inference)).
- **Run operations:** dashboards, alerts, controllers, incident and decision logs.
- **Recipe registry and reproduction CI:** releases, manifests and scheduled reproduction runs.

**The YAML.** A scheduled reproduction of the recipe release at reference scale, triggered from CI (GitHub Actions).

```yaml
# .github/workflows/reproduce-recipes.yaml
name: reproduce-recipes
on:
  schedule: [ { cron: "23 3 * * 1" } ]          # weekly, Monday 03:23 UTC
  workflow_dispatch: {}
jobs:
  reproduce:
    runs-on: ubuntu-latest
    strategy:
      matrix: { release: [assistant-v14-rl@1.0.0, assistant-v14-sft@2.1.0] }
    steps:
    - uses: actions/checkout@v4
    - name: Submit reference run
      run: |
        python3 tools/launch.py --release "releases/${{ matrix.release }}/manifest.yaml" \
          --scale reference --queue research-small --wait
    - name: Compare with expected band
      run: |
        python3 tools/compare_band.py --release "releases/${{ matrix.release }}/manifest.yaml" \
          --run latest --fail-outside-band          # drift in infra or libraries fails the check #!
```

**Going deeper.**

- **One identifier everywhere:** the experiment ID appears in run specs, jobs, checkpoints, eval results and recipe releases, so any artifact leads back to the decision that created it.
- **Dashboards per stage:** how many experiments are in each stage, how long they wait, and why they fail gates.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds13-c12.svg)

- **Phase 1, visibility:** stages, owners and the experiment tracker; run specs submitted at scale-up; decision logs for production runs.
- **Phase 2, quality gates:** data validation and decontamination in CI; verifier readiness checks; launcher pre-flight.
- **Phase 3, reliability:** failure classes and pod failure policies, checkpoint verification, watchdogs, on-call and runbooks.
- **Phase 4, repeatability:** recipe releases, scale-up reviews and reproduction CI.

**Trade-offs we accepted.**

- **Gates add steps:** a few days of checks before a large run, in exchange for not losing weeks to preventable failures.
- **Reserved capacity costs money while idle,** in exchange for runs that start on time and finish.
- **Contracts formalize relationships:** some flexibility is lost, and much less is lost in miscommunication.
- **Reproduction CI uses compute weekly,** in exchange for catching drift before a new version depends on an old recipe.

**Open questions.**

- **How light can early gates be** while still catching the problems that are expensive later?
- **Automatic run reviews:** can the analysis platform ([Design 12](#design-experiment-analysis)) draft the daily run review?
- **Shared ownership at the boundary:** who owns a failure that is half data and half verifier, and how is that decided quickly?
- **Recipes across model generations:** how much of a recipe release carries over to the next base model, and how is that tested?

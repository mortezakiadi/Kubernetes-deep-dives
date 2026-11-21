---
id: design-lh-data-pipelines
title: "Design 28: Automated data collection pipelines for long-horizon RL tasks"
short: "Long-horizon RL data pipelines"
---
This design answers one question: *how do we automatically produce a steady supply of long-horizon RL tasks and trajectories (multi-hour coding, tool-use and computer-use tasks with hundreds of steps) that are verified, at the right difficulty, free of contamination, and traceable from source to training run?* [Design 08](#design-long-horizon) covers the training methods for long horizons. [Design 10](#design-verification) covers verifiers and reward quality, and [Design 16](#design-feedback-loop) covers human feedback. This design is the data factory that feeds them: sourcing tasks, building and validating their environments, running rollouts at scale, storing trajectories, calibrating difficulty, filtering for quality, involving experts where automation can't, and measuring yield and cost at every stage.

The short version: *a task is an environment plus a verifier that has been proven to work, and every stage is an idempotent, measured step in one pipeline*. Candidate tasks are mined from real repositories and tools, synthesized, or written by experts. Each is packaged as a container image, an initial state and a verifier. It's accepted only if the verifier passes the reference solution, fails the starting state, and does both reliably across repeated runs. Reference policies then attempt each task several times to estimate its difficulty, and only tasks in a learnable band are kept. Rollouts run in sandboxes with snapshots, so long trajectories can resume after failures. Trajectories are stored step by step in a versioned schema. A funnel dashboard shows the yield and cost of every stage.

### The use case and requirements

**Scenario.** The research team trains agents on software-engineering tasks (fix an issue in a real repository, with tests as the verifier), tool-use tasks and some computer-use tasks. Each task takes 50 to 400 agent steps and 20 minutes to several hours. The first dataset of 3,000 tasks was assembled by hand over two months. A quarter of its verifiers turned out to pass on the unchanged repository, 9% of tasks overlapped with an evaluation benchmark, and nobody could say which version of a task a given training run had used.

![A data factory for long tasks](diagrams/designs/concepts/ds28-c1.svg)

**What the pipeline must deliver.**

- **Volume:** about 12,000 new accepted tasks per quarter, and millions of trajectories per month for training and analysis.
- **Proven verifiers:** every accepted task's verifier passes a reference solution, fails the initial state, and gives the same answers across repeated runs.
- **Calibrated difficulty:** each task's success rate under reference policies is known, and the training mix draws from a target band.
- **Integrity:** no near-duplicates, no overlap with evaluation sets, licenses and sensitive data checked.
- **Resumable long rollouts:** a trajectory that runs for hours survives worker and node failures without starting over.
- **Lineage:** every task and trajectory traceable to its source, pipeline version and the runs that used it.
- **Measured yield and cost:** candidates in, accepted tasks out, and cost per accepted task at each stage.

**Out of scope:** the RL algorithms ([Design 08](#design-long-horizon), [Design 09](#design-off-policy)), training infrastructure ([Design 14](#design-reliable-rl-infra)) and sandbox internals ([Design 01](#design-sandboxing)).

### Anatomy of a long-horizon task

**Scenario.** Tasks were stored as a prompt and a URL to a repository. Six months later, half the URLs pointed at changed code, dependencies had moved, and the same "task" behaved differently in every run.

![Task anatomy](diagrams/designs/concepts/ds28-c2.svg)

![Anatomy of a long-horizon task](diagrams/designs/ds28-1.svg)

**What the picture shows.** Everything needed to reproduce a task exactly, in one versioned record.

- **Environment:** a container image digest with the repository, tools and dependencies installed, never a mutable tag or URL.
- **Initial state:** the starting point (commit, files, database contents, browser state), stored as a snapshot.
- **Instruction:** what the agent is asked to do, as the agent will see it.
- **Verifier:** a program that inspects the final state and returns a result, with optional milestone checks for partial progress on long tasks ([Design 08](#design-long-horizon)).
- **Budgets:** maximum steps, tokens, wall-clock time and tool calls.
- **Reference solution:** a known-good final state or patch, used to validate the verifier, never shown to the agent.
- **Metadata:** source, license, domain, difficulty estimates, pipeline version and content hash.

**The code.** The task record (Python).

```python
from dataclasses import dataclass, field
import hashlib, json

@dataclass(frozen=True)
class Task:
    task_id: str
    image: str                         # registry.example.com/tasks/env@sha256:...  (digest, never a tag) #!
    initial_state: str                 # s3://rl-tasks/snapshots/<sha256>.tar.zst
    instruction: str
    verifier: dict                     # {"cmd": [...], "timeout_s": 900, "milestones": [...]}
    reference_solution: str            # s3://rl-tasks/refs/<sha256>.patch  (never given to the agent)
    budgets: dict                      # {"max_steps": 400, "max_tokens": 2_000_000, "wall_s": 14_400}
    source: dict                       # {"kind": "mined", "repo": "...", "pr": 4812, "license": "Apache-2.0"}
    domain: str
    pipeline_version: str
    difficulty: dict = field(default_factory=dict)   # {"policy-v7": {"pass_at_1": 0.31, "n": 8}}

    def content_hash(self) -> str:
        core = {k: getattr(self, k) for k in ("image", "initial_state", "instruction", "verifier", "budgets")}
        return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]
```

**Going deeper.**

- **Content hashes are task versions:** a change to the image, state, instruction, verifier or budgets produces a new hash. Training runs record hashes, not names.
- **The reference solution is a secret:** it lives in a separate store with access limited to the validation stage, so it can't leak into agent prompts or training data.

### Sourcing candidate tasks

**Scenario.** All tasks came from two people writing them by hand. They produced about 40 a week, mostly in the two languages they knew best.

![Sources](diagrams/designs/concepts/ds28-c3.svg)

![Sourcing candidate tasks](diagrams/designs/ds28-2.svg)

**What the picture shows.** Four sources of candidates, each with its own strengths and failure modes.

- **Mined from real work:** merged pull requests in permissively licensed repositories that fix an issue and add or change tests. The tests that fail before and pass after become the verifier. Realistic, and the largest source.
- **Synthesized:** variants of validated tasks (different bugs injected into the same repository, the same feature asked of a different module), and tasks generated by models from specifications. Cheap and plentiful, but needs strict validation.
- **Expert-authored:** domain experts write tasks where mining is thin (long multi-system workflows, specialized domains), with reference solutions ([Design 16](#design-feedback-loop)).
- **Environment-native:** simulators and tool sandboxes that generate tasks with built-in verifiers (web apps with seeded databases, games, spreadsheets).
- **Source mix as a dial:** each source's share is set per quarter from yield, cost and what the training team needs.

**The code.** Turning a merged pull request into a candidate task: the tests that fail before and pass after (Python, simplified).

```python
def candidate_from_pr(repo: str, pr: dict, run_tests) -> dict | None:
    """pr: base_sha, merge_sha, changed_test_files, issue_text, license. run_tests(sha, files) -> {test: passed}."""
    if pr["license"] not in {"MIT", "Apache-2.0", "BSD-3-Clause"} or not pr["changed_test_files"]:
        return None
    before = run_tests(pr["base_sha"], pr["changed_test_files"], apply_tests_from=pr["merge_sha"])
    after = run_tests(pr["merge_sha"], pr["changed_test_files"])
    fail_to_pass = [t for t, ok in after.items() if ok and not before.get(t, False)]   # the verifier #!
    pass_to_pass = [t for t, ok in after.items() if ok and before.get(t, False)]       # must not break
    if not fail_to_pass:
        return None
    return {"repo": repo, "base_sha": pr["base_sha"], "instruction": pr["issue_text"],
            "verifier": {"fail_to_pass": fail_to_pass, "pass_to_pass": pass_to_pass},
            "reference": {"merge_sha": pr["merge_sha"]}, "license": pr["license"]}
```

**Going deeper.**

- **Issue text isn't always a good instruction:** many issues assume context or describe the fix. An instruction-rewriting step (model-assisted, then sampled for human review) makes them self-contained without leaking the solution.
- **Synthesized tasks drift toward easy:** generators produce what they can verify. Track difficulty by source and cap synthesized tasks in the easy band.

### Building and validating environments

**Scenario.** A quarter of the hand-assembled tasks had verifiers that passed on the unchanged repository. Agents were rewarded for doing nothing. Another 6% failed randomly because their tests depended on network access or timing.

![Validation](diagrams/designs/concepts/ds28-c4.svg)

![Building and validating environments](diagrams/designs/ds28-3.svg)

**What the picture shows.** The checks that turn a candidate into a validated task.

- **Build:** a container image for the task's environment, built reproducibly (pinned base image, locked dependencies, no network after build), pushed by digest.
- **Gold check:** the verifier must pass on the reference solution.
- **Null check:** the verifier must fail on the initial state, so doing nothing earns nothing.
- **Stability:** both checks are repeated (for example, 5 times) in fresh sandboxes. Any disagreement marks the task flaky, and it's rejected or quarantined for repair.
- **Isolation check:** the verifier runs with network disabled, so tasks don't depend on the outside world.
- **Cost recorded:** build and validation time per task, for the funnel.

**The code.** Validating a built task with gold, null and stability checks (Python).

```python
def validate(task, sandbox, repeats: int = 5) -> dict:
    """sandbox.run(image, state, apply=None, cmd, network=False) -> {"passed": bool, "seconds": float}"""
    gold, null = [], []
    for _ in range(repeats):
        gold.append(sandbox.run(task.image, task.initial_state, apply=task.reference_solution,
                                cmd=task.verifier["cmd"], network=False)["passed"])
        null.append(sandbox.run(task.image, task.initial_state, apply=None,
                                cmd=task.verifier["cmd"], network=False)["passed"])
    verdict = ("valid" if all(gold) and not any(null) else
               "flaky" if any(gold) and not all(gold) or any(null) and not all(null) else
               "verifier-passes-null" if all(null) else "gold-fails")                       #!
    return {"task_id": task.task_id, "verdict": verdict, "gold_passes": sum(gold), "null_passes": sum(null)}
```

**Going deeper.**

- **Check that the verifier can't be satisfied trivially:** beyond the null check, a few cheap adversarial attempts (deleting the failing tests, hard-coding expected outputs) should also fail. These come from the reward-hacking catalog of [Design 10](#design-verification).
- **Flaky tasks are repairable:** most flakiness comes from network, time or randomness in tests. A repair queue with common fixes (seeding, mocking time) recovers a share of them.

### Orchestrating the pipeline

**Scenario.** The pipeline was a set of scripts run by hand in sequence. When the build step crashed halfway through 20,000 candidates, the team reran everything, including 11,000 builds that had already succeeded.

![Pipeline DAG](diagrams/designs/concepts/ds28-c5.svg)

![Orchestrating the pipeline](diagrams/designs/ds28-4.svg)

**What the picture shows.** The pipeline as a DAG of idempotent, cached steps on Kubernetes.

- **Stages:** source → build → validate → calibrate → filter → accept → publish, each processing batches of tasks.
- **Idempotent by content hash:** each step's output is keyed by its input's hash and the step's version. Rerunning skips work already done.
- **Retries by error class:** infrastructure errors (node lost, registry timeout) retry automatically, task errors (build failed) are recorded as results, not retried.
- **Fan-out with limits:** thousands of builds and validations run in parallel, limited by Kueue quotas ([Design 17](#design-gpu-scheduling)) so the pipeline doesn't starve training.
- **Pipeline versions:** a change to any step's code bumps its version. Tasks record which versions produced them.

**The YAML.** The pipeline's build-and-validate stages as an Argo WorkflowTemplate, with memoization and retries.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: WorkflowTemplate
metadata: { name: rl-task-pipeline, namespace: rl-data }
spec:
  entrypoint: main
  arguments: { parameters: [{ name: batch }] }                   # s3 path listing candidate ids
  templates:
  - name: main
    dag:
      tasks:
      - { name: list, template: list-candidates }
      - name: build
        template: build-env
        depends: list
        arguments: { parameters: [{ name: candidate, value: "{{item}}" }] }
        withParam: "{{tasks.list.outputs.result}}"
      - name: validate
        template: validate-task
        depends: build
        arguments: { parameters: [{ name: candidate, value: "{{item}}" }] }
        withParam: "{{tasks.list.outputs.result}}"
  - name: build-env
    inputs: { parameters: [{ name: candidate }] }
    memoize:                                                       # skip candidates already built by this version #!
      key: "build-v14-{{inputs.parameters.candidate}}"
      maxAge: "2160h"
      cache: { configMap: { name: rl-build-cache } }
    retryStrategy: { limit: 3, retryPolicy: OnError, backoff: { duration: "30s", factor: 2 } }
    container:
      image: registry.example.com/rl-data/builder:14
      args: ["build", "--candidate", "{{inputs.parameters.candidate}}", "--push-by-digest"]
      resources: { requests: { cpu: "4", memory: 16Gi } }
  - name: validate-task
    inputs: { parameters: [{ name: candidate }] }
    memoize: { key: "validate-v9-{{inputs.parameters.candidate}}", maxAge: "2160h", cache: { configMap: { name: rl-validate-cache } } }
    retryStrategy: { limit: 2, retryPolicy: OnError }
    container:
      image: registry.example.com/rl-data/validator:9
      args: ["validate", "--candidate", "{{inputs.parameters.candidate}}", "--repeats", "5"]
  - name: list-candidates
    script:
      image: python:3.12-slim
      command: [python]
      source: |
        import json; print(json.dumps([f"cand-{i:06d}" for i in range(500)]))   # stand-in for reading the batch
```

**Going deeper.**

- **Batches of hundreds, not one workflow per task:** very large fan-outs strain the workflow controller. Batches of a few hundred candidates per workflow keep it responsive.
- **Results as data, not logs:** each step writes a structured result row (task, step, version, verdict, cost) to the warehouse, which feeds the funnel and makes reruns auditable.
- **Clean up intermediate artifacts:** set `artifactGC.strategy: OnWorkflowDeletion` (or `OnWorkflowCompletion`) on the workflow, and `Never` on the outputs you must keep. Put `{{workflow.uid}}` in the S3 keys, so two runs of the same template never delete each other's files. Deletion runs in its own pod, which needs a service account allowed to delete from the bucket ([Artifact garbage collection](https://argo-workflows.readthedocs.io/en/latest/walk-through/artifacts/)).

### Running long rollouts at scale

**Scenario.** Rollouts for 4-hour tasks ran on preemptible nodes. When a node was reclaimed at hour three, the rollout started over. On busy days, 30% of rollout compute was spent repeating work.

![Resumable rollouts](diagrams/designs/concepts/ds28-c6.svg)

![Running long rollouts at scale](diagrams/designs/ds28-5.svg)

**What the picture shows.** Rollout workers that survive interruptions and share a serving pool.

- **Sandboxes per rollout:** each attempt runs in its own sandbox ([Design 01](#design-sandboxing)) from the task's image and initial state, with network rules from the task.
- **Policy served separately:** agents call a shared inference pool ([Design 21](#design-llm-serving)), so rollout workers are cheap CPU pods and GPU capacity is shared.
- **Snapshots at checkpoints:** every N steps (or minutes), the sandbox's state and the conversation so far are snapshotted. An interrupted rollout resumes from the latest snapshot on another node.
- **Budgets enforced by the harness:** steps, tokens, wall time and tool calls. A rollout that exceeds them ends with a recorded reason.
- **Throughput control:** rollouts are queued in Kueue with quotas, so calibration, data collection and online RL share capacity predictably.

**The code.** A resumable rollout loop with periodic snapshots (Python).

```python
import time

def run_rollout(task, policy, sandbox_pool, store, snapshot_every: int = 20) -> dict:
    ckpt = store.latest_checkpoint(task.task_id, policy.version)               # resume if interrupted
    sb = sandbox_pool.start(task.image, ckpt["state"] if ckpt else task.initial_state)
    history, step = (ckpt["history"], ckpt["step"]) if ckpt else ([], 0)
    t0, tokens = time.time() - (ckpt["elapsed_s"] if ckpt else 0), ckpt["tokens"] if ckpt else 0
    while step < task.budgets["max_steps"]:
        action, used = policy.act(task.instruction, history)                     # call to the shared serving pool
        tokens += used
        obs = sb.execute(action)                                                 # tool call, shell command, click
        history.append({"step": step, "action": action, "obs": store.put_blob(obs), "t": time.time() - t0})
        step += 1
        if action.get("type") == "submit" or tokens > task.budgets["max_tokens"] or time.time() - t0 > task.budgets["wall_s"]:
            break
        if step % snapshot_every == 0:
            store.save_checkpoint(task.task_id, policy.version, {"state": sb.snapshot(), "history": history,
                                  "step": step, "elapsed_s": time.time() - t0, "tokens": tokens})   #!
    result = sb.verify(task.verifier)
    return {"task_id": task.task_id, "policy": policy.version, "steps": step, "tokens": tokens,
            "reward": result["passed"], "milestones": result.get("milestones"), "history": history}
```

**Going deeper.**

- **Snapshot cost vs lost work:** snapshots take seconds to minutes for large sandboxes. Choose the interval from the interruption rate, as with training checkpoints ([Design 20](#design-distributed-training)).
- **Deterministic resume isn't guaranteed:** a resumed rollout continues from the saved state but samples new actions. Record resumes in the trajectory so analyses can account for them.

### Trajectory format and storage

**Scenario.** Trajectories were saved as one JSON file per rollout, with full observations inline. A month of rollouts was 380 TB, most of it repeated file listings and screenshots, and loading one task's trajectories meant scanning everything.

![Trajectory storage](diagrams/designs/concepts/ds28-c7.svg)

![Trajectory format and storage](diagrams/designs/ds28-6.svg)

**What the picture shows.** A step-level, versioned format with large content stored once.

- **Step records:** one row per step with trajectory ID, step index, action (tool, arguments), observation reference, timing, token counts and any intermediate reward or milestone.
- **Content-addressed blobs:** observations (file contents, screenshots, logs) stored by hash in object storage. Repeated observations are stored once.
- **Trajectory summary:** one row per trajectory with task hash, policy version, outcome, total steps and tokens, cost, resume count and pipeline version.
- **Columnar files, partitioned:** Parquet partitioned by date and task domain, so queries read only what they need.
- **Schema versions:** the schema has a version, readers handle known versions, and changes are additive.

**The code.** The step and trajectory schemas (Python, PyArrow).

```python
import pyarrow as pa

STEP_SCHEMA = pa.schema([
    ("trajectory_id", pa.string()), ("step", pa.int32()),
    ("action_type", pa.string()), ("action", pa.string()),              # JSON: tool name + arguments
    ("obs_ref", pa.string()),                                             # sha256 of the observation blob #!
    ("obs_bytes", pa.int64()), ("t_s", pa.float32()),
    ("prompt_tokens", pa.int32()), ("completion_tokens", pa.int32()),
    ("milestone", pa.string()),                                           # nullable: milestone reached at this step
], metadata={"schema_version": "3"})

TRAJECTORY_SCHEMA = pa.schema([
    ("trajectory_id", pa.string()), ("task_hash", pa.string()), ("policy_version", pa.string()),
    ("purpose", pa.string()),                                             # calibration | training | eval | analysis
    ("reward", pa.float32()), ("steps", pa.int32()), ("tokens", pa.int64()),
    ("wall_s", pa.float32()), ("resumes", pa.int16()), ("end_reason", pa.string()),
    ("cost_usd", pa.float32()), ("pipeline_version", pa.string()), ("date", pa.date32()),
], metadata={"schema_version": "3"})
```

**Going deeper.**

- **Store what training needs, keep what analysis needs:** training reads actions, observation text and rewards. Analysis also needs timing and sandbox logs. Both live in the same format, in different columns and blobs.
- **Retention by purpose:** calibration rollouts can be summarized and their blobs expired after months. Trajectories used for training are kept with the run that used them.

### Difficulty calibration and the training band

**Scenario.** The first training run used every validated task. Forty percent were solved by the starting policy on every attempt, and 30% were never solved by any policy. Neither group gave a learning signal, and together they took 70% of rollout compute.

![Difficulty band](diagrams/designs/concepts/ds28-c8.svg)

![Difficulty calibration and the training band](diagrams/designs/ds28-7.svg)

**What the picture shows.** Each task's success rate under reference policies, and the band training draws from.

- **Calibration rollouts:** each validated task is attempted several times (for example, 8) by one or two reference policies (the current training policy and a stronger one).
- **Estimates with uncertainty:** pass rates with intervals. Eight attempts can't distinguish 10% from 20%, so tasks whose interval reaches well past a band edge are marked provisional and get more attempts.
- **The learnable band:** tasks the current policy solves sometimes but not always (for example, 5% to 80%). Those always solved or never solved are set aside.
- **Too hard for now:** tasks that no policy solves but a stronger policy or a human expert can, kept for later, when the policy improves.
- **Re-calibration:** as the policy improves, tasks move out of the band at the top. Periodic re-calibration on a sample refreshes the band.

**The code.** Assigning tasks to bands from calibration rollouts, with intervals (Python).

```python
from math import sqrt

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))

def band(k_train: int, n_train: int, k_strong: int, n_strong: int, lo: float = 0.05, hi: float = 0.80,
         min_n: int = 8) -> str:
    low, high = wilson(k_train, n_train)
    if (n_train >= min_n and k_train == 0) or high < lo:
        return "too-hard-solvable" if k_strong > 0 else "too-hard"      # keep the first kind for later
    if (n_train >= min_n and k_train == n_train) or low > hi:
        return "too-easy"
    if n_train >= min_n and lo <= k_train / n_train <= hi:
        return "train" if low >= lo / 2 and high <= min(1.0, hi + 0.15) else "train-provisional"
    return "needs-more-attempts"                                        # too few attempts to place it #!
```

**Going deeper.**

- **Calibration costs real compute:** 8 attempts at a 2-hour task is 16 sandbox-hours plus inference. Calibrate on a subset first, and extend to tasks whose sources have a good yield.
- **Long tasks need partial credit to learn from:** in the hard band, milestone rewards ([Design 08](#design-long-horizon)) can turn a never-solved task into one with a usable signal. Calibrate milestone rates too.

### Quality and integrity filters

**Scenario.** After a strong evaluation result, someone noticed that 9% of training tasks came from the same repositories and pull requests as the evaluation benchmark. The result had to be withdrawn.

![Integrity filters](diagrams/designs/concepts/ds28-c9.svg)

![Quality and integrity filters](diagrams/designs/ds28-8.svg)

**What the picture shows.** Filters every task passes before acceptance.

- **Evaluation contamination:** tasks are compared with every evaluation set by source (same repository and commit range, same pull request) and by content (instruction and patch similarity). Matches are excluded, and evaluation sources are blocked at mining time.
- **Near-duplicates:** tasks too similar to each other (the same fix in a fork, synthesized variants that differ trivially) are clustered, and one per cluster is kept.
- **Verifier audit:** a sample of tasks per source gets adversarial attempts and human review of the verifier ([Design 10](#design-verification)).
- **Licenses and sensitive data:** source licenses checked against an allow-list. Secrets, personal data and credentials in repositories detected and removed or the task rejected.
- **Instruction leakage:** instructions that reveal the solution (code from the fix in the issue text) are rewritten or rejected.

**The code.** Near-duplicate and contamination check with shingled similarity (Python).

```python
def shingles(text: str, k: int = 8) -> set[int]:
    toks = text.split()
    return {hash(" ".join(toks[i:i + k])) for i in range(max(len(toks) - k + 1, 1))}

def jaccard(a: set, b: set) -> float:
    return len(a & b) / max(len(a | b), 1)

def integrity(task: dict, eval_index: dict[str, set], accepted_index: dict[str, set],
              eval_sources: set[str], contam: float = 0.5, dup: float = 0.8) -> str:
    if task["source"].get("repo_pr") in eval_sources:
        return "reject:eval-source"                                     # same PR as an eval task #!
    sig = shingles(task["instruction"] + " " + task.get("reference_patch_text", ""))
    if any(jaccard(sig, s) >= contam for s in eval_index.values()):
        return "reject:eval-content"
    if any(jaccard(sig, s) >= dup for s in accepted_index.values()):
        return "reject:near-duplicate"
    return "pass"
```

**Going deeper.**

- **Use locality-sensitive hashing at scale:** pairwise comparison doesn't scale past tens of thousands of tasks. MinHash with LSH finds candidate pairs quickly, then the exact check confirms them.
- **Block contamination at the source:** keeping a list of evaluation repositories and commit ranges in the miner prevents most contamination before any compute is spent.

### Experts in the loop

**Scenario.** Mining produced plenty of single-repository bug fixes but almost no tasks spanning several systems, the kind the product team cared about most. Two contracted experts wrote 30 such tasks in a month, and 11 of them failed validation.

![Experts in the loop](diagrams/designs/concepts/ds28-c10.svg)

![Experts in the loop](diagrams/designs/ds28-9.svg)

**What the picture shows.** Where people add what automation can't, and how their work flows through the same pipeline.

- **Task authoring:** experts write tasks in an authoring environment that runs the same build and validation checks as the pipeline, so problems show up while they write.
- **Demonstrations:** for very hard tasks, an expert's own trajectory in the sandbox is recorded in the trajectory format, as a demonstration or a check that the task is solvable.
- **Review queues:** samples of mined and synthesized tasks, rewritten instructions and verifier audits go to reviewers, with clear rubrics and two reviewers on a subset to measure agreement ([Design 16](#design-feedback-loop)).
- **Paying for accepted tasks:** experts are paid for tasks that pass validation and calibration, with a fixed rate for review work, which aligns incentives with the pipeline's checks.
- **Feedback to authors:** each author sees validation results, difficulty and which tasks were used, which improves the next batch.

**The code.** Acceptance and agreement metrics for expert work (Python).

```python
import pandas as pd

def author_report(tasks: pd.DataFrame, reviews: pd.DataFrame) -> pd.DataFrame:
    """tasks: author, task_id, verdict (valid/flaky/...), band; reviews: task_id, reviewer, label (accept/reject)."""
    t = tasks.assign(valid=tasks.verdict.eq("valid"), in_band=tasks.band.eq("train"))
    per_author = t.groupby("author").agg(submitted=("task_id", "size"), valid_rate=("valid", "mean"),
                                         accepted=("in_band", "sum"))
    dbl = reviews.groupby("task_id").filter(lambda g: g.reviewer.nunique() >= 2)
    agree = dbl.groupby("task_id").label.agg(lambda s: s.nunique() == 1).mean()          # reviewer agreement #!
    per_author["review_agreement"] = round(float(agree), 3) if len(dbl) else None
    return per_author.sort_values("accepted", ascending=False)
```

**Going deeper.**

- **Experts are the scarcest input:** spend their time where automation fails (multi-system tasks, verifier audits, very hard demonstrations), not on reviewing tasks a check could reject.
- **Authoring tools are part of the pipeline:** an author who sees "your verifier passes on the initial state" in seconds fixes it immediately, instead of after a day in a queue.

### Measuring yield, cost and freshness

**Scenario.** The team knew the pipeline produced "about 4,000 tasks a month". It didn't know that the build stage lost 49% of candidates, mostly to one failing dependency mirror, or that a validated task cost $38 on average and an accepted one $120.

![Funnel and cost](diagrams/designs/concepts/ds28-c11.svg)

![Measuring yield, cost and freshness](diagrams/designs/ds28-10.svg)

**What the picture shows.** The funnel, its cost, and the alerts that keep it healthy.

- **Funnel by stage and source:** candidates → built → validated → calibrated → in band → passed integrity → accepted, each with counts and the reasons tasks were lost.
- **Cost per stage:** compute and human cost at each stage, and cost per accepted task by source.
- **Throughput and freshness:** tasks accepted per week, time from candidate to accepted, and the age of tasks in the training band.
- **Alerts:** a sudden drop in a stage's yield (a broken mirror, a changed verifier) or a rise in flakiness pages the pipeline owner.
- **Consumption:** which tasks training runs drew, how often, and with what results, which feeds re-calibration and source decisions.

**The code.** The funnel with loss reasons and cost per accepted task, by source (warehouse SQL).

```sql
SELECT source_kind,
       COUNT(*)                                                     AS candidates,
       SUM(CASE WHEN build_ok THEN 1 ELSE 0 END)                    AS built,
       SUM(CASE WHEN verdict = 'valid' THEN 1 ELSE 0 END)           AS validated,
       SUM(CASE WHEN band = 'train' THEN 1 ELSE 0 END)              AS in_band,
       SUM(CASE WHEN accepted THEN 1 ELSE 0 END)                    AS accepted,
       SUM(cost_usd) / NULLIF(SUM(CASE WHEN accepted THEN 1 ELSE 0 END), 0) AS usd_per_accepted,   -- #!
       MODE() WITHIN GROUP (ORDER BY loss_reason)                   AS top_loss_reason
FROM fact_task_pipeline
WHERE quarter = '2026-Q4'
GROUP BY source_kind
ORDER BY accepted DESC;
```

**Going deeper.**

- **Optimize the expensive end:** calibration rollouts cost far more per task than builds. Cheap early filters that reject tasks likely to fall out of band (by source and repository statistics) save the most money.
- **Track yield per repository:** a few repositories produce most valid tasks. Mining effort follows yield.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds28-c12.svg)

- **Phase 1, the task record (weeks 1–4):** the task schema with content hashes, the gold, null and stability checks, and re-validation of the existing 3,000 tasks.
- **Phase 2, the factory (weeks 3–10):** mining from pull requests, reproducible builds, the orchestrated DAG with memoization, and the funnel tables.
- **Phase 3, rollouts and calibration (weeks 8–14):** resumable rollout workers on the shared serving pool, the trajectory format, calibration and bands.
- **Phase 4, integrity and experts (weeks 10–16):** contamination and near-duplicate filters, verifier audits, the expert authoring environment and review queues.

**Trade-offs we accepted.**

- **Strict validation discards tasks:** many real tasks fail the null or stability checks. Fewer, trustworthy tasks beat more, noisy ones.
- **Calibration is expensive** but saves far more training compute than it costs.
- **Snapshots add storage and latency** to every long rollout, in exchange for not repeating hours of work.
- **Experts are paid per accepted task,** which some find stricter than hourly work.

**Open questions.**

- **Tasks that grow with the policy:** can the pipeline generate harder variants of solved tasks automatically, keeping the band full as the policy improves?
- **Verifiers for open-ended work:** for tasks without tests (design documents, research), how far can model-based verifiers go before reward hacking dominates ([Design 10](#design-verification))?
- **Environment diversity:** how many distinct repositories, tools and environments does a policy need to generalize, and how should the source mix be set from that?
- **Sharing with evaluation:** how should the evaluation team ([Design 15](#design-eval-infra)) draw held-out tasks from the same pipeline without contaminating training?

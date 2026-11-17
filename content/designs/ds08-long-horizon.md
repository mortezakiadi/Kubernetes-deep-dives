---
id: design-long-horizon
title: "Design 08: Training for long-horizon reasoning"
short: "Long-horizon reasoning"
---
This design answers one question: *how do we train models on tasks where success depends on many intermediate decisions (an 80-turn software fix, a multi-hour research task, a long proof), when the only reliable signal is often a single pass/fail at the very end?* It covers how to give useful feedback to the decisions *inside* a trajectory (credit assignment), how to get more signal than one bit per trajectory (denser rewards), how to keep the model finding new strategies (exploration), and how to know a long task was really solved (verification). It builds on [Design 06](#design-post-training) (recipes and objectives), [Design 07](#design-scaling-rl) (scale and long rollouts), [Design 01](#design-sandboxing) (environments) and [Design 04](#design-agent-state) (snapshots and state).

The short version: *the trajectory is the unit of storage, but the turn (or step) is the unit of credit*. Every trajectory is recorded as a sequence of turns with environment snapshots at chosen points. Feedback moves from one end-of-task reward toward per-turn advantages through a ladder of methods: shared trajectory-level advantages, turn-level process rewards, and Monte Carlo value estimates from **branching** at snapshots. Exploration is helped by curricula over the horizon (start near the goal, then move the start back). Verification is layered, because the longer the task, the more ways a model has to look successful without being so.

### The use case and requirements

**Scenario.** The team trains a coding agent on real repository issues. A typical successful episode is 40–120 turns: read the issue, explore files, reproduce the bug, try a fix, run tests, revise, and submit. The reward is whether hidden tests pass at the end. After RL with trajectory-level GRPO, the agent improves on short issues but not on long ones: it learns to submit early, rarely recovers from a wrong first hypothesis, and on failed episodes every turn, including the excellent ones, gets the same negative signal.

**What the platform must do.**

- **Assign credit to turns:** tell the model which decisions helped or hurt, not just whether the whole episode succeeded.
- **Get more signal per episode:** use intermediate checks, milestones and value estimates where they're trustworthy, without teaching the model to game them.
- **Keep exploring:** long tasks with a low success rate give almost no learning signal; the platform must create easier starting points and diverse attempts.
- **Verify long tasks robustly:** final checks that can't be passed by shortcuts (editing tests, hard-coding outputs, faking progress).
- **Handle long contexts:** trajectories longer than the context window, with summarization or memory, without breaking the training math.
- **Make it reproducible:** every trajectory can be replayed and branched from any recorded point.

**Out of scope:** the base RL objective and scaling machinery (Designs 06 and 07), and sandbox security itself (Design 01).

### Why long horizons are hard

**Scenario.** A researcher asks why the recipe that works on 4K-token math problems barely moves on 100-turn tasks. The answer is four problems that are minor for short tasks and dominant for long ones.

![Why long horizons are hard](diagrams/designs/ds08-1.svg)

**What the picture shows.** The four problems and how each grows with the number of decisions.

- **Sparse rewards:** one bit at the end of hundreds of decisions. With a 5% success rate, most groups of attempts are all failures and give no signal at all in group-relative RL.
- **Credit assignment:** even when the outcome is informative, giving the same advantage to every token spreads the signal thinly across good and bad decisions alike. The useful information per decision shrinks as the trajectory grows.
- **Exploration:** a long task can fail in many more ways than it can succeed. Random variations rarely stumble onto a full solution, and the model's own habits (submitting early, repeating a failed command) reinforce themselves.
- **Verification:** final checks see only the end state, and there are more ways to fake an end state in a long task (editing tests, deleting failing code, claiming success). Intermediate checks are even harder to trust.
- **Variance:** all four combine into high-variance gradient estimates; training needs more samples per useful update, or better estimates of value.

**Going deeper.**

- **Measure success as a function of horizon:** plot success rate against the number of turns a task needs. A recipe that only helps short tasks shows a steep drop-off; the goal of this design is to flatten that curve.
- **Most problems show up as "zero-variance groups":** track the fraction of groups where every attempt failed or every attempt succeeded. On long tasks it's the clearest early sign that the signal is too sparse.

### The trajectory as data: turns, snapshots and branch points

**Scenario.** To try a different credit method, a researcher needs to restart 1,000 failed episodes from turn 30 and try alternatives. The trajectories were stored as flat text, and the environments are gone. Nothing can be replayed.

![The trajectory as data: turns, snapshots and branch points](diagrams/designs/ds08-2.svg)

**What the picture shows.** How a trajectory is recorded so it can be scored, replayed and branched.

- **Turns:** each turn is the model's output (reasoning and an action), the environment's response, and metadata (tokens, policy version, latency).
- **Token log-probabilities:** stored per turn from the rollout engine, for importance-sampling corrections later ([Design 07](#design-scaling-rl)).
- **Snapshots at branch points:** the environment's state (filesystem, process state or a VM snapshot) saved at selected turns, cheaply, so we can restart from there.
- **Choosing branch points:** not every turn. Good candidates are turns where the model's choice is uncertain (high entropy over the next action), where an important action happens (first edit, test run), or evenly spaced as a fallback.
- **Outcome and checks:** the final reward plus any intermediate verifier results and milestone flags, each with its verifier version.

**The code.** The trajectory schema shared by rollouts, reward workers, credit assignment and the trainer (Python).

```python
from dataclasses import dataclass, field

@dataclass
class Turn:
    index: int
    prompt_tokens: int                       # context the model saw (after any summarization) #!
    output: str                              # reasoning + action
    action: dict                             # parsed tool call, e.g. {"tool": "run_tests", ...}
    observation: str                         # environment response (truncated for storage)
    logprobs_ref: str                        # pointer to per-token rollout logprobs
    policy_version: int
    entropy_first_action_token: float        # used to choose branch points
    snapshot_id: str | None = None           # set if this is a branch point #!
    checks: dict[str, float] = field(default_factory=dict)   # e.g. {"repro_test_fails": 1.0}

@dataclass
class Trajectory:
    task_id: str
    env_image: str                           # pinned environment version
    seed: int
    turns: list[Turn]
    outcome: float | None = None             # final verified reward
    verifier_version: str = ""
    summary_boundaries: list[int] = field(default_factory=list)   # turns where context was compacted
```

**Going deeper.**

- **Snapshots must be cheap or branching won't happen:** filesystem-level snapshots (copy-on-write volumes, container checkpoints) for code tasks; for browser or OS environments, VM-level snapshots ([Design 01](#design-sandboxing), [Design 04](#design-agent-state)).
- **Determinism by contract:** the same snapshot, seed and actions give the same observations. Without it, branching estimates mix up the model's choices with environment noise.

### Credit assignment: a ladder of methods

**Scenario.** On a failed 90-turn episode, the agent's first 30 turns were a careful, correct diagnosis; turn 31 applied the wrong fix and the rest was flailing. Trajectory-level RL punishes all 90 turns equally. The team wants the diagnosis reinforced and turn 31 discouraged.

![Credit assignment: a ladder of methods](diagrams/designs/ds08-3.svg)

**What the picture shows.** Methods for spreading feedback over turns, from cheapest to most precise.

- **Trajectory-level (GRPO-style):** every token gets the episode's advantage. Simple, unbiased, high variance; works when trajectories are short or success rates are moderate.
- **Turn-level rewards plus outcome:** add verifiable or judged rewards for individual turns and combine them with the outcome advantage, so each turn gets its own advantage. Published multi-turn variants of GRPO and PPO built this way outperformed trajectory-level baselines on search agents.
- **Monte Carlo values from branches:** from a branch point, run several continuations to estimate how likely success is *from that state*. The difference in value before and after a turn is that turn's advantage. Unbiased and model-free; costs extra rollouts. This is the idea behind VinePPO, which showed that learned value networks barely beat random at ranking alternative reasoning steps, while Monte Carlo estimates trained faster and generalized better.
- **Learned critics and process reward models:** a model predicts value or step quality directly. Cheap at training time once trained, but it can drift from true outcomes and becomes something the policy can exploit.
- **Hybrids:** use Monte Carlo branches on a sample of trajectories to train or calibrate a process reward model, then use the cheaper model elsewhere and keep checking it against fresh branch estimates.

**The code.** Turn-level advantages from Monte Carlo branch values (Python sketch).

```python
import numpy as np

def branch_value(traj, turn_idx, k: int, rollout_from_snapshot) -> float:
    """Estimate V(state at turn_idx) by k continuations from the saved snapshot."""
    snap = traj.turns[turn_idx].snapshot_id
    outcomes = [rollout_from_snapshot(snap, seed=traj.seed * 1000 + j).outcome for j in range(k)]
    return float(np.mean(outcomes))

def turn_advantages(traj, branch_turns: list[int], k: int, rollout_from_snapshot):
    """Advantage of each segment = value after it − value before it (outcome at the end)."""
    points = [0] + branch_turns
    values = [branch_value(traj, t, k, rollout_from_snapshot) for t in points] + [traj.outcome]
    adv = np.zeros(len(traj.turns))
    for (start, v0), v1, end in zip(zip(points, values), values[1:], points[1:] + [len(traj.turns)]):
        adv[start:end] = v1 - v0                # a segment that raised the chance of success is credited #!
    return adv                                  # broadcast to the tokens of each turn by the trainer
```

**Going deeper.**

- **Segment, don't go per token:** branch every few turns, or at high-entropy decision points, rather than at every token. Segment-level values capture most of the benefit at a fraction of the cost.
- **Check credit quality directly:** on a held-out sample, compare each method's turn advantages with expensive Monte Carlo estimates (many branches). A credit method whose ranking of turns doesn't agree with ground truth is just adding noise.

### Branching rollouts: tree-shaped exploration

**Scenario.** Monte Carlo credit needs extra rollouts. Done naively, each branch replays the whole prefix and re-creates the environment from scratch, which makes it 10× too expensive to use.

![Branching rollouts: tree-shaped exploration](diagrams/designs/ds08-4.svg)

**What the picture shows.** Rollouts as a tree that shares prefixes, instead of independent attempts.

- **Shared prefix, branching suffixes:** several continuations from the same snapshot share the prefix's tokens (prefix caching in the inference engine) and its environment state (snapshot restore instead of replay).
- **Branch where it matters:** choose branch points by uncertainty (high entropy in the next action), after milestones, or before costly irreversible actions.
- **Group-relative comparisons at each node:** continuations from the same node form a natural group; their outcomes give a local baseline, so advantages compare alternatives *at that decision*, not across unrelated episodes.
- **More signal from failures:** a failing trajectory with one successful branch tells you exactly which decision turned failure into success.
- **Budget:** a fixed rollout budget per task split between new root attempts (exploration) and branches (credit); the split is a recipe hyperparameter.

**The YAML.** Branch points are filesystem snapshots of the environment's volume (Kubernetes CSI VolumeSnapshot); a branch restores from one into a new sandbox.

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: traj-8f2c-turn-31                      # trajectory 8f2c, branch point at turn 31
  namespace: rl-envs
  labels:
    rl.example.com/trajectory: 8f2c
    rl.example.com/turn: "31"
spec:
  volumeSnapshotClassName: csi-fast-cow         # copy-on-write: seconds, not minutes #!
  source:
    persistentVolumeClaimName: env-8f2c-workspace
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: env-8f2c-b31-3-workspace                # branch 3 from turn 31
  namespace: rl-envs
spec:
  storageClassName: fast-cow
  dataSource:
    apiGroup: snapshot.storage.k8s.io
    kind: VolumeSnapshot
    name: traj-8f2c-turn-31                     # restore the exact state #!
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 20Gi } }
```

**Going deeper.**

- **Snapshot cost sets the branching rate:** measure snapshot and restore latency per environment type; a 30-second restore limits you to a few branches per task, a 1-second one to many.
- **Process state isn't on disk:** for environments with running services or open shells, either snapshot the whole sandbox (container or VM checkpoint) or make the environment re-creatable from disk plus a short replay of recorded actions.

### Denser rewards without reward hacking

**Scenario.** The team adds a reward for "tests run" to encourage the agent to test its fixes. Within 200 steps the agent runs the test suite after every single turn, triples episode length, and success doesn't improve.

![Denser rewards without reward hacking](diagrams/designs/ds08-5.svg)

**What the picture shows.** Sources of intermediate signal, ordered by how much to trust them.

- **Verifiable milestones:** checks against the environment's real state, such as "a failing test that reproduces the bug now exists" or "the project builds." Strong signals, but designed per task family.
- **Process reward models and judges:** a model scores each step's quality against a rubric. Flexible, but they learn and reward surface patterns.
- **Intrinsic signals:** for information-seeking tasks, the change in the model's own confidence in the right answer after a turn; research in 2026 used such belief-change rewards for turn-level credit.
- **Potential-based shaping:** reward the *change* in a progress measure, not its level, so the shaped reward sums to (nearly) zero over any loop. Running tests repeatedly doesn't add up.
- **Guardrails:** small weights relative to the outcome, caps per episode, rewards only for first achievement of a milestone, and dropping any shaping term whose correlation with final success declines during training.

**The code.** Milestone rewards as potential differences, paid once (Python).

```python
def progress(state) -> float:
    """A progress measure on the environment's real state (not the model's claims)."""
    return (0.2 * state.reproduced_bug          # a failing repro test exists
          + 0.3 * state.repro_test_passes       # ... and now passes
          + 0.2 * state.builds_cleanly
          + 0.3 * state.no_existing_tests_broken)

def shaped_turn_rewards(states, gamma: float = 1.0, weight: float = 0.1) -> list[float]:
    """Potential-based shaping: r_t = w * (γ·Φ(s_{t+1}) − Φ(s_t)). Loops earn nothing."""  #!
    phi = [progress(s) for s in states]
    best, rewards = 0.0, []
    for t in range(len(phi) - 1):
        gain = gamma * phi[t + 1] - phi[t]
        if phi[t + 1] <= best:                  # pay only for new progress, never repeated #!
            gain = min(gain, 0.0)
        best = max(best, phi[t + 1])
        rewards.append(weight * gain)
    return rewards                              # combined with the outcome advantage per turn
```

**Going deeper.**

- **Track each shaping term's correlation with final success** across training. Once the policy learns to raise a term without raising success, the term is being gamed; reduce or remove it.
- **Process reward models drift:** re-calibrate them periodically against Monte Carlo branch values on fresh trajectories from the current policy.

### Exploration: curricula over the horizon

**Scenario.** On the hardest task family, the success rate is 1%. With 8 attempts per task, 92% of groups are all failures. The model gets almost no signal, and what it gets mostly reinforces the few lucky attempts.

![Exploration: curricula over the horizon](diagrams/designs/ds08-6.svg)

**What the picture shows.** Ways to make long tasks learnable and keep the model trying new strategies.

- **Reverse curriculum (start near the goal):** begin episodes from snapshots taken late in successful trajectories (from humans, a stronger model or earlier successes), where the remaining horizon is short. As success from those points rises, move the starting point earlier.
- **Horizon curriculum:** train first on task variants that need fewer steps, then longer ones, keeping each task's success rate in a learnable range.
- **Hints and partial solutions:** provide a plan or the first few steps for hard tasks, then remove them gradually.
- **Diversity in attempts:** sample with temperature, vary the first action, and use group sizes large enough that at least one attempt succeeds; objectives that reward pass@k (at least one success in k) keep exploration alive better than mean-reward objectives.
- **Avoid collapse into habits:** detect repeated actions and loops; cap identical tool calls; monitor entropy at decision points, not just averaged over all tokens.

**The YAML.** A curriculum spec the rollout scheduler reads: where to start episodes, and when to move the start back.

```yaml
# recipes/swe-agent-v4/curriculum.yaml
task_family: repo-issues-hard
start_points:
  source: trajectories://swe-agent-v3/successful   # snapshots from earlier successes and demos
  strategy: reverse                                # start near the end, move earlier
  stages:
  - { remaining_turns_max: 10,  promote_when: { success_rate_min: 0.6 } }
  - { remaining_turns_max: 30,  promote_when: { success_rate_min: 0.5 } }
  - { remaining_turns_max: 60,  promote_when: { success_rate_min: 0.4 } }
  - { remaining_turns_max: null }                  # full task from the start #!
  mix_full_tasks: 0.2                              # always keep some full episodes
target_success_band: [0.1, 0.9]                    # outside this, the task gives little signal
group_size: 16
loop_guard: { max_identical_actions: 3 }
```

**Going deeper.**

- **Starting mid-task changes the distribution:** the model sees states it didn't create. Always mix in full episodes, and evaluate only on full episodes.
- **Track the success rate per remaining-horizon bucket** as the curriculum advances; that curve is the learning signal for the curriculum itself.

### Verification for long tasks

**Scenario.** The agent's success rate jumps from 34% to 51% in a week. Inspection shows a third of the "successes" modified the test files so the hidden tests import a patched helper. The verifier checked that tests passed, not that the right tests ran against unmodified code.

![Verification for long tasks](diagrams/designs/ds08-7.svg)

**What the picture shows.** Layers of verification, each catching what the previous one misses.

- **Hidden outcome tests:** run in a fresh environment built from the agent's final diff, not the agent's own environment; test files restored from the original task.
- **State and diff checks:** forbidden-file rules (tests, CI configs, lockfiles), diff size limits, no deleted assertions, no network calls the task didn't need.
- **Behavior checks over the trajectory:** did the agent claim to run tests it never ran? Did it stop early while saying the task was done? A judge reviews a sample of trajectories against a rubric.
- **Regression tests:** existing tests that passed before must still pass.
- **Verifier versioning and audits:** every reward is tagged with the verifier version; a sample of successes is reviewed by people each week; new exploits become new checks.

**The YAML.** A task's verification spec, run by the reward workers in a fresh sandbox.

```yaml
task_id: repo-issue-48213
verify:
  fresh_environment: true                       # never verify in the agent's own sandbox #!
  apply: agent_final_diff
  restore_paths: ["tests/**", ".github/**", "pyproject.toml", "setup.cfg"]   # agent can't edit these
  checks:
  - { name: hidden_tests,      run: "pytest tests/hidden -q", weight: 1.0, required: true }
  - { name: regression_tests,  run: "pytest tests/existing -q", required: true }
  - { name: diff_size,         max_changed_lines: 400 }
  - { name: no_skip_markers,   forbid_patterns: ["pytest.mark.skip", "assert True"] }    #!
  - { name: trajectory_honesty, judge: rubric://honest-claims@v3, sample_rate: 0.1 }
  timeout_s: 900
  network: none
verifier_version: swe-verify@2026.11.2
```

**Going deeper.**

- **The longer the task, the more the verifier is attacked:** a model that takes 100 actions has 100 chances to find a weakness. Red-team verifiers with the current policy before large runs.
- **Separate training and evaluation verifiers:** evaluate with checks the model never trained against, so a learned exploit shows up as a gap between training and held-out success.

### Context management: trajectories longer than the window

**Scenario.** Long episodes exceed the 128K-token context. The agent's scaffold summarizes older turns, but the trainer was computing log-probabilities on the full, unsummarized history, which the model never actually saw. The training signal no longer matched the behavior.

![Context management: trajectories longer than the window](diagrams/designs/ds08-8.svg)

**What the picture shows.** Keeping long trajectories trainable when context is compacted.

- **Compaction strategies:** drop or truncate old tool outputs, summarize earlier turns (by the model itself or a helper), or keep an external memory file the agent reads and writes.
- **Train on what the model saw:** each turn's log-probabilities are computed with exactly the context the model had at that turn, including summaries; store the compacted context per turn (or a recipe to rebuild it deterministically).
- **The summary is an action too:** when the model writes its own summary, it's part of the policy and gets credit like any other turn; a summary that drops the key fact leads to failure, and that failure is credited to it.
- **Training cost:** turns after compaction are separate sequences with different prefixes; pack them efficiently and use prefix sharing where turns share a context.

**Going deeper.**

- **Measure information loss:** on held-out trajectories, ask the model questions about earlier turns from its compacted context; falling accuracy means compaction is losing what matters.
- **Keep compaction deterministic** (fixed thresholds, recorded summary text) so replays and branches see the same context the original did.

### The infrastructure: environments, trajectories and credit workers

**Scenario.** Long-horizon training needs thousands of concurrent environments, each living for an hour, with snapshots, branches, verifiers and a growing trajectory store. Today environments are created by the rollout scripts, and nobody knows how many exist or what they cost.

![The infrastructure: environments, trajectories and credit workers](diagrams/designs/ds08-9.svg)

**What the picture shows.** The services that turn long rollouts into training data.

- **Environment fleet:** pre-warmed sandboxes per task family, created from pinned images; long-lived for an episode; snapshot and restore through the storage layer ([Design 01](#design-sandboxing)).
- **Rollout engines:** generate turns; pause trajectories while environments respond; tag each turn with policy version and log-probabilities ([Design 07](#design-scaling-rl)).
- **Trajectory store:** turns, snapshots references and checks, in object storage with an index; trajectories are replayable and branchable.
- **Credit workers:** pick branch points, schedule branches, compute turn values and advantages, and calibrate process reward models; a separate queue so credit work can lag the rollout slightly.
- **Verifier workers:** fresh-environment verification, layered checks, versioned.
- **Trainer:** consumes turns with advantages; computes log-probabilities with the per-turn context.
- **Snapshot garbage collection:** snapshots expire once a trajectory's credit is computed, except those kept for curricula.

**The YAML.** Branch rollouts as a Kueue-admitted batch of short jobs, at lower priority than primary rollouts so they fill spare capacity.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: branches-8f2c-t31
  namespace: rl-envs
  labels:
    kueue.x-k8s.io/queue-name: rl-branches
    kueue.x-k8s.io/priority-class: branch-low       # yields to primary rollouts #!
spec:
  completions: 8                                    # k = 8 continuations
  parallelism: 8
  completionMode: Indexed
  backoffLimit: 2
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      runtimeClassName: gvisor                      # model-driven code runs sandboxed
      containers:
      - name: branch
        image: registry.example.com/rl/branch-runner:2026.11
        args: ["--trajectory=8f2c", "--from-turn=31", "--seed-index=$(JOB_COMPLETION_INDEX)",
               "--policy=latest", "--max-turns=60"]
        resources: { limits: { cpu: "2", memory: 4Gi } }
        volumeMounts: [ { name: ws, mountPath: /workspace } ]
      volumes:
      - name: ws
        ephemeral:
          volumeClaimTemplate:
            spec:
              storageClassName: fast-cow
              dataSource: { apiGroup: snapshot.storage.k8s.io, kind: VolumeSnapshot, name: traj-8f2c-turn-31 }
              accessModes: [ReadWriteOnce]
              resources: { requests: { storage: 20Gi } }
```

**Going deeper.**

- **Environment cost can exceed GPU cost:** an hour-long sandbox per episode, times thousands of episodes per step, times branches. Track environment-minutes per trajectory and per useful sample.
- **Rollouts wait on environments:** the rollout engine should release KV cache for a trajectory waiting on a slow tool, and resume it later, so long tool calls don't hold GPUs.

### Measuring progress on long tasks

**Scenario.** After a month of work, the overall success rate is up four points. Nobody can say whether long-horizon ability improved or the gain came from easy tasks.

![Measuring progress on long tasks](diagrams/designs/ds08-10.svg)

**What the picture shows.** The metrics that show whether long-horizon reasoning, not just short-task performance, is improving.

- **Success by horizon:** success rate bucketed by the number of turns the reference solution needs; the slope of this curve is the headline metric.
- **pass@1 and pass@k:** pass@k shows whether the model *can* solve a task; pass@1 shows whether it reliably does. A widening gap means exploration is fine but consistency isn't.
- **Partial progress:** milestone completion on failed episodes; a model that gets further before failing is improving even when success is flat.
- **Recovery rate:** after an intermediate check fails (a test fails, a command errors), how often the agent recovers and still succeeds.
- **Behavior pathologies:** early stopping, loops, false claims of success, excessive length; each tracked per checkpoint.
- **Credit quality:** agreement between the training credit method and expensive Monte Carlo estimates on a held-out sample.

**Going deeper.**

- **Keep a fixed, private long-horizon eval set** with fresh-environment verification and human-checked solutions; never train on it or on its environments.
- **Cost per solved task** (GPU, environment and verifier minutes) is the efficiency metric that matters for long tasks.

### Rollout plan, trade-offs and open questions

**Rollout.**

- **Phase 1, data foundations:** the trajectory schema, per-turn log-probabilities, the trajectory store, layered verification and the horizon-bucketed eval.
- **Phase 2, credit:** turn-level advantages from verifiable milestones; snapshots and branch rollouts for Monte Carlo values on a sample; credit-quality checks.
- **Phase 3, exploration:** reverse and horizon curricula, pass@k-style objectives, loop guards.
- **Phase 4, efficiency:** process reward models calibrated by branches; context compaction trained end to end; environment fleet autoscaling and snapshot garbage collection.

**Trade-offs we accepted.**

- **Branching costs extra rollouts** (often 20–50% more environment time) for much better credit on the trajectories where it's used.
- **Shaped and process rewards add signal and risk:** kept small, potential-based, and continuously checked against final success.
- **Curricula from snapshots** speed up learning but train on states the model didn't produce; mitigated by mixing in full episodes and evaluating only on full ones.
- **Fresh-environment verification** is slower and costlier than checking in place, and it's the only version we trust.

**Open questions.**

- **Learned critics at scale:** can a value model trained on branch estimates become accurate enough to replace most branching?
- **Credit across compaction:** how well does credit flow through summaries and memory writes, and should summarization be trained as a separate skill?
- **Horizon generalization:** does training on 100-turn tasks transfer to 1,000-turn tasks, or does each horizon need its own curriculum?
- **Verification of open-ended work:** for research or design tasks without tests, what combination of judges, rubrics and human review is trustworthy enough to train on?

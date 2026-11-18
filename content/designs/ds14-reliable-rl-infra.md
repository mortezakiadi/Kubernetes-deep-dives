---
id: design-reliable-rl-infra
title: "Design 14: Reliable infrastructure for large-scale RL training"
short: "Reliable RL infrastructure"
---
This design answers one question: *how do we design and build infrastructure that keeps large-scale RL training running (thousands of GPUs, weeks of wall-clock time, many cooperating services) when hardware fails every few hours, services slow down, and some failures are silent?* [Design 07](#design-scaling-rl) covers what changes when RL scales and [Design 13](#design-end-to-end) covers how runs get from idea to production. This design is about the infrastructure underneath: failure domains, node health, fault tolerance for each RL component, whole-state checkpointing, stragglers and silent errors, and how reliability is measured and tested.

The short version: *assume something is always broken, and make each break cost seconds of one component instead of hours of the whole run*. The RL system is split into **failure domains** with different recovery strategies: the trainer is stateful and synchronous (fast checkpoint, fast restart, or per-step recovery across replica groups); rollout engines, verifiers and environments are stateless or cheaply restorable (retry, replace, route around). Nodes are continuously health-checked and drained before they break a job. All RL state (not only weights) is checkpointed asynchronously and verified. And reliability is a measured number, **goodput**, with an SLO, tested on purpose with fault injection.

### The use case and requirements

**Scenario.** The RL platform runs a 2,048-GPU training job with 512 more GPUs of rollout engines, a 6,000-pod sandbox pool and a judge fleet. In the first month: a GPU failure roughly every few hours, two silent NaN events from a bad GPU, a network flap that hung all NCCL collectives for 40 minutes, a verifier deploy that returned errors read as "fail" for an hour, and a checkpoint that couldn't be loaded when it was needed. Goodput was 61%.

![A month of failures](diagrams/designs/concepts/ds14-c1.svg)

**What the platform must do.**

- **Contain failures:** a broken GPU, engine, verifier or sandbox affects the smallest possible part of the system.
- **Recover in minutes:** automatic detection, replacement and resumption, without a person in the loop for common failures.
- **Lose little work:** checkpoints frequent and cheap enough that a restart loses minutes, not hours.
- **Catch silent failures:** slow nodes, numerical corruption and hung collectives are detected, not discovered from the loss curve.
- **Measure reliability:** goodput and recovery times as tracked numbers with targets.
- **Prove it:** fault injection in staging and during rehearsals, so recovery paths are known to work.

**Out of scope:** algorithmic stability (Designs 06–09) and the run's organizational process (Design 13).

### Anatomy of an RL system and its failure domains

**Scenario.** One sandbox node's disk filled up. Rollouts that used it hung, the trainer waited for a full batch, and the whole 2,048-GPU job sat idle for 25 minutes. A failure in the cheapest component stopped the most expensive one.

![Failure domains](diagrams/designs/concepts/ds14-c2.svg)

![Anatomy of an RL system and its failure domains](diagrams/designs/ds14-1.svg)

**What the picture shows.** The components of a large RL system, grouped by how they fail and recover.

- **Trainer (stateful, synchronous):** thousands of GPUs in collectives; one failed rank stops all of them. Recovery: restart from checkpoint, or per-step recovery across replica groups.
- **Rollout engines (stateless, independent):** each engine can fail alone; in-flight requests are retried elsewhere. Recovery: replace.
- **Reward and environment tier (stateless or snapshot-backed):** verifiers, judges and sandboxes. Recovery: retry, circuit-break, replace.
- **Coordination state:** the sample buffer, the controller, weight versions and the data cursor. Small but critical; must be durable and replicated.
- **Storage and network:** checkpoint storage, object storage, the GPU fabric and the pod network. Shared by everything; failures here are wide.
- **The rule:** failures flow toward the trainer as *delays*, never as *stops*. Every dependency of the trainer has a timeout, a fallback and a bound on how long the trainer will wait.

**Going deeper.**

- **Map the blast radius explicitly:** for each component, write down what stops when it fails; anything that can stop the trainer, apart from the trainer itself, is a design bug.
- **Decoupling buys reliability as well as throughput:** the asynchronous designs of [Design 07](#design-scaling-rl) and [Design 09](#design-off-policy) mean a slow rollout tier reduces fresh data instead of halting training.

### Reliability targets: failure rates, goodput and checkpoint intervals

**Scenario.** Someone proposes checkpointing every 4 hours "to save storage." Someone else proposes every 5 minutes "to be safe." Neither has done the arithmetic.

![Goodput arithmetic](diagrams/designs/concepts/ds14-c3.svg)

![Reliability targets: failure rates, goodput and checkpoint intervals](diagrams/designs/ds14-2.svg)

**What the picture shows.** How failure rates turn into lost time, and how to choose the checkpoint interval.

- **Failures scale with size:** a job's failure rate is roughly the per-node rate times the number of nodes. Meta's Llama 3 405B run on 16,384 H100 GPUs saw 419 unexpected interruptions in 54 days, about one every three hours, with GPUs or their memory responsible for about half.
- **Lost time per failure:** detection time + restart time + recomputation since the last checkpoint (on average half the interval).
- **Checkpoint cost:** the time training is paused to save; asynchronous checkpointing reduces it to seconds.
- **Optimal interval:** roughly the square root of (2 × checkpoint cost × mean time between failures), the Young–Daly approximation. Faster checkpoints allow shorter intervals.
- **Goodput:** the fraction of reserved GPU time that produced training progress. The target (for example, 90%+) is set per run and tracked daily.

**The code.** Expected goodput and the best checkpoint interval for a job (Python).

```python
import math

def plan(nodes, node_mtbf_h, ckpt_cost_s, restart_s, detect_s, step_overhead=0.0):
    job_mtbf_s = node_mtbf_h * 3600 / nodes                       # failures add up across nodes
    interval_s = math.sqrt(2 * ckpt_cost_s * job_mtbf_s)          # Young–Daly approximation #!
    ckpt_overhead = ckpt_cost_s / interval_s
    lost_per_failure = detect_s + restart_s + interval_s / 2
    failure_overhead = lost_per_failure / job_mtbf_s
    goodput = (1 - ckpt_overhead) * (1 - failure_overhead) * (1 - step_overhead)
    return {"job_mtbf_h": round(job_mtbf_s / 3600, 2), "ckpt_interval_min": round(interval_s / 60, 1),
            "goodput": round(goodput, 3)}

# 256 nodes, a node fails every ~60 days on average; sync vs async checkpoint, slow vs fast restart
print(plan(256, 60 * 24, ckpt_cost_s=300, restart_s=1800, detect_s=300))   # sync, slow restart
print(plan(256, 60 * 24, ckpt_cost_s=8,   restart_s=240,  detect_s=30))    # async, hot spares #!
```

**Going deeper.**

- **Restart time matters as much as checkpoint interval:** with failures every few hours, a 30-minute restart costs more than any checkpoint policy can recover.
- **Measure, don't assume, node MTBF:** use the fleet's own failure history per hardware generation; it changes with age and firmware.

### Node health: burn-in, continuous checks and automatic drain

**Scenario.** A node with a GPU throwing intermittent memory errors joins the training job. It doesn't fail outright; it fails the job twice a day for a week, each time looking like a new problem.

![Node health](diagrams/designs/concepts/ds14-c4.svg)

![Node health: burn-in, continuous checks and automatic drain](diagrams/designs/ds14-3.svg)

**What the picture shows.** The health lifecycle of a GPU node.

- **Burn-in before admission:** new or repaired nodes run GPU diagnostics, memory tests, NVLink and network bandwidth tests and a short multi-node collective test before they can join training pools.
- **Continuous passive checks:** GPU error counters (XID errors, ECC and row-remap events), thermals, PCIe and NIC errors, collected per node.
- **Periodic active checks:** short diagnostic runs during idle windows or between jobs.
- **Automatic drain:** a node that fails a check is tainted so no new pods land on it, its workloads are moved, and it goes to repair; a node that fails repeatedly is retired.
- **Feedback from jobs:** if one node appears in several job failures, it's drained even if its checks pass.

**The YAML.** A health-check DaemonSet that taints a node when its GPU checks fail (the check script uses NVIDIA DCGM; node tainting through the API).

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: gpu-health
  namespace: gpu-ops
spec:
  selector: { matchLabels: { app: gpu-health } }
  template:
    metadata: { labels: { app: gpu-health } }
    spec:
      serviceAccountName: gpu-health            # RBAC: patch nodes (taints) only
      nodeSelector: { nvidia.com/gpu.present: "true" }
      tolerations: [ { operator: Exists } ]     # keep running on tainted nodes
      priorityClassName: system-node-critical
      containers:
      - name: check
        image: registry.example.com/gpu-ops/health:2026.11
        args:
        - --interval=60s
        - --dcgm-level=1                        # quick passive checks; level 3 in idle windows
        - --xid-fatal=48,63,64,74,79,94,95      # treat these XIDs as fatal #!
        - --on-fail=taint:gpu-health=failed:NoSchedule
        - --report=http://fleet-health.gpu-ops:8080/events
        env:
        - name: NODE_NAME
          valueFrom: { fieldRef: { fieldPath: spec.nodeName } }
        securityContext: { privileged: true }   # DCGM needs device access
        resources: { limits: { cpu: "500m", memory: 512Mi } }
```

**Going deeper.**

- **Drain gently during a run:** a failing node in a running job is replaced at the next checkpoint or restart, not by killing the job immediately, unless the failure is already fatal.
- **Track node history:** every node's failures and repairs are recorded; repeat offenders are retired before they fail another run.

### Trainer fault tolerance: from fast restarts to per-step recovery

**Scenario.** Each trainer failure took the whole 256-node job down for 35 minutes: rescheduling pods, pulling images, re-forming NCCL groups and loading a 3 TB checkpoint. Four failures a day cost more than two hours of 2,048 GPUs.

![Trainer recovery levels](diagrams/designs/concepts/ds14-c5.svg)

![Trainer fault tolerance: from fast restarts to per-step recovery](diagrams/designs/ds14-4.svg)

**What the picture shows.** Three levels of trainer fault tolerance, each building on the previous.

- **Fast full restart:** hot spare nodes reserved in the queue, images pre-pulled, checkpoints loaded in parallel from local copies or peers; the whole job restarts in a few minutes.
- **In-place restart:** only the failed pod is replaced and the job restarts its processes without rescheduling the healthy pods, avoiding the pod-level work.
- **Per-step recovery across replicas:** with replicated data-parallel groups (DDP or HSDP), a failed replica group drops out, training continues with the rest, and the group rejoins by copying state from a healthy peer. Libraries like torchft implement this, allowing membership changes at the training step granularity instead of stopping the world.
- **Collective timeouts:** NCCL watchdog timeouts set so a hung collective fails in minutes, not hours; the failure is then handled like any other.

**The YAML.** A JobSet failure policy: infrastructure failures restart without using up the restart budget; configuration errors stop immediately.

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: rl-trainer-prod
  namespace: rl-scale
  labels: { kueue.x-k8s.io/queue-name: rl-production }
spec:
  failurePolicy:
    maxRestarts: 20
    restartStrategy: Recreate
    rules:
    - name: infra-disruption                       # node drained, GPU fault, preemption
      action: RestartJobSetAndIgnoreMaxRestarts     # doesn't consume the budget #!
      onJobFailureReasons: [PodFailurePolicy]
    - name: config-error
      action: FailJobSet                            # exit 42: don't retry a bad config #!
      onJobFailureReasons: [BackoffLimitExceeded]
      targetReplicatedJobs: [launcher]
  replicatedJobs:
  - name: trainer
    replicas: 1
    template:
      spec:
        parallelism: 256
        completions: 256
        backoffLimit: 0
        podFailurePolicy:
          rules:
          - action: FailJob
            onPodConditions: [ { type: DisruptionTarget } ]
        template:
          spec:
            restartPolicy: Never
            containers:
            - name: trainer
              image: registry.example.com/rl/trainer@sha256:1f0c...
              env:
              - { name: TORCH_NCCL_ASYNC_ERROR_HANDLING, value: "1" }
              - { name: TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC, value: "300" }   # hung collective → error #!
              args: ["--resume=latest", "--ckpt-load=peer-first"]
              resources: { limits: { nvidia.com/gpu: 8 } }
  - name: launcher
    replicas: 1
    template:
      spec:
        backoffLimit: 0
        template:
          spec:
            restartPolicy: Never
            containers:
            - name: validate
              image: registry.example.com/rl/trainer@sha256:1f0c...
              args: ["--validate-config-only"]      # exits 42 on config errors
```

**Going deeper.**

- **Per-step recovery has requirements:** replicated data-parallel groups, a coordination service, and enough memory headroom to run with one group missing; it pays off most at very large scale.
- **Measure each recovery phase:** detection, scheduling, image, process start, collective setup, checkpoint load. The slowest phase is the one to fix.

### Rollout fleet resilience

**Scenario.** One vLLM engine started returning garbled output after a driver error but kept passing its HTTP health check. For two hours, 1/64 of rollouts were nonsense with near-zero rewards.

![Healthy-looking but wrong](diagrams/designs/concepts/ds14-c6.svg)

![Rollout fleet resilience](diagrams/designs/ds14-5.svg)

**What the picture shows.** Keeping a large fleet of inference engines healthy and its failures invisible to the trainer.

- **Stateless engines:** any request can be retried on another engine; partial generations are discarded or resumed according to policy.
- **Timeouts and hedging:** each request has a deadline; requests in the slowest tail are re-issued on another engine and the first answer wins.
- **Deep health checks:** beyond "is it up," engines periodically answer a fixed canary prompt; output and log-probabilities are compared with expected values, catching silent corruption.
- **Quarantine:** an engine that fails a canary or produces abnormal statistics (rewards, lengths, log-probabilities far from its peers) is drained and replaced.
- **Rolling weight updates:** new weights are loaded engine by engine (or group by group), so the fleet never goes fully offline.

**The code.** A rollout client with deadlines, retries on another engine and hedged requests (Python, asyncio).

```python
import asyncio, random

async def generate(engines, request, deadline_s=600, hedge_after_s=120, max_attempts=3):
    """Send to one engine; if slow, hedge on a second; retry elsewhere on failure."""
    tried, attempt = set(), 0
    while attempt < max_attempts:
        attempt += 1
        primary = random.choice([e for e in engines.healthy() if e not in tried]); tried.add(primary)
        tasks = {asyncio.create_task(primary.generate(request))}
        try:
            done, _ = await asyncio.wait(tasks, timeout=hedge_after_s)
            if not done:                                            # slow: hedge on another engine #!
                backup = random.choice([e for e in engines.healthy() if e not in tried]); tried.add(backup)
                tasks.add(asyncio.create_task(backup.generate(request)))
                done, _ = await asyncio.wait(tasks, timeout=deadline_s - hedge_after_s,
                                             return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.exception():
                    for other in tasks - {t}: other.cancel()
                    return t.result()
        except Exception:
            pass
        for t in tasks: t.cancel()
        engines.report_failure(primary)                              # feeds quarantine decisions
    raise TimeoutError("rollout failed on all attempts")             # the buffer skips this prompt
```

**Going deeper.**

- **Compare engines with their peers:** per-engine reward, length and log-probability distributions should match the fleet; an outlier engine is suspect even when it responds.
- **Hedging costs capacity:** hedge only the slowest few percent, and count hedged duplicates in the throughput budget.

### Weight synchronization you can trust

**Scenario.** A weight push was interrupted halfway by a network flap. Half of one engine's layers were new and half were old. It served for an hour before anyone noticed.

![Atomic weight sync](diagrams/designs/concepts/ds14-c7.svg)

![Weight synchronization you can trust](diagrams/designs/ds14-6.svg)

**What the picture shows.** Making weight distribution atomic, verifiable and recoverable.

- **Stage, then swap:** new weights are written to a staging buffer on each engine; the engine switches versions only when the full set has arrived and verified.
- **Checksums per bucket:** each transferred bucket carries a checksum; a mismatch triggers a re-send of that bucket.
- **Version handshake:** after the swap, each engine reports its version and a fingerprint (for example, the log-probability of a canary prompt); the coordinator checks them.
- **Timeouts and fallback path:** if the fast path (NCCL over the GPU fabric) fails or times out, engines load the version from object storage instead.
- **Version-tagged tokens:** every generated token carries the version that produced it ([Design 09](#design-off-policy)), so a mistake is traceable and correctable.

**The code.** Staged, checksummed weight loading with an atomic swap on the engine side (Python sketch).

```python
import hashlib, torch

class WeightReceiver:
    def __init__(self, model):
        self.model, self.staging, self.expected = model, {}, {}

    def begin(self, version: int, manifest: dict):          # manifest: bucket_id -> (names, sha256)
        self.version, self.expected, self.staging = version, manifest, {}

    def receive(self, bucket_id: str, tensors: dict[str, torch.Tensor]) -> bool:
        h = hashlib.sha256()
        for name in sorted(tensors):
            h.update(tensors[name].contiguous().view(torch.uint8).cpu().numpy().tobytes())
        if h.hexdigest() != self.expected[bucket_id][1]:
            return False                                     # coordinator re-sends this bucket #!
        self.staging.update({n: t.to("cuda", non_blocking=True) for n, t in tensors.items()})
        return True

    def commit(self) -> int:
        if set(self.staging) != {n for names, _ in self.expected.values() for n in names}:
            raise RuntimeError("incomplete weight set; keeping current version")
        with torch.no_grad():
            for name, p in self.model.named_parameters():
                p.copy_(self.staging[name])                  # swap only when everything arrived #!
        self.staging.clear()
        return self.version
```

**Going deeper.**

- **Checksums on GPU:** hashing large tensors on CPU is slow; compute a cheap GPU-side checksum (for example, sums over strided slices) per bucket and keep the full hash for the object-storage path.
- **Staging needs memory:** a full staging copy doubles weight memory; with tight memory, stage per layer and swap layer by layer between requests.

### The reward and environment tier: fail closed, degrade gracefully

**Scenario.** A verifier deploy started returning HTTP 500 for 30% of requests. The reward client treated errors as score 0. For an hour, the policy was punished for correct answers that happened to hit the broken pods.

![Errors are not scores](diagrams/designs/concepts/ds14-c8.svg)

![The reward and environment tier: fail closed, degrade gracefully](diagrams/designs/ds14-7.svg)

**What the picture shows.** Patterns that keep reward failures from corrupting training or stalling it.

- **Errors are not scores:** a failed reward call is an error, retried or marked "no reward" (excluded from the loss), never a 0 or a 1 ([Design 10](#design-verification)).
- **Circuit breakers:** when a verifier's error rate crosses a threshold, calls stop for a cooling period; affected samples wait or are skipped.
- **Back-pressure:** when the reward queue is long, rollout engines slow down new trajectories instead of piling up work.
- **Bulkheads:** separate pools per verifier and per environment type, so one overloaded verifier doesn't starve the others.
- **Sandbox hygiene:** leaked or stuck sandboxes are reaped by age; pools are refilled before they run dry.
- **Pinned versions during a run:** verifier deploys during a run are explicit, canaried and reversible.

**The code.** A circuit breaker around reward calls that turns failures into "no reward", never into a score (Python).

```python
import time

class RewardBreaker:
    def __init__(self, call, max_error_rate=0.05, window=200, cooldown_s=60):
        self.call, self.max, self.window, self.cooldown = call, max_error_rate, window, cooldown_s
        self.results, self.open_until = [], 0.0

    def score(self, task, completion) -> float | None:
        if time.time() < self.open_until:
            return None                                   # breaker open: no reward, sample excluded #!
        try:
            r = self.call(task, completion)
            self._record(ok=True)
            return r
        except Exception:
            self._record(ok=False)
            return None                                   # an error is never a score #!

    def _record(self, ok: bool):
        self.results = (self.results + [ok])[-self.window:]
        errs = self.results.count(False) / len(self.results)
        if len(self.results) >= 50 and errs > self.max:
            self.open_until = time.time() + self.cooldown  # stop hammering a failing service
            self.results.clear()
```

**Going deeper.**

- **Track "no reward" rate per verifier** as a first-class metric; a rise means data is being lost even if training looks fine.
- **Graceful degradation is a policy:** decide in advance whether a run pauses, continues with fewer domains, or slows down when a verifier is unavailable.

### Checkpointing the whole RL state

**Scenario.** After a restart from checkpoint, the run repeated 40,000 prompts it had already trained on, the controller forgot that it had tightened the staleness limit, and the replay buffer was empty. The model was restored; the run wasn't.

![Whole-state checkpoint](diagrams/designs/concepts/ds14-c9.svg)

![Checkpointing the whole RL state](diagrams/designs/ds14-8.svg)

**What the picture shows.** Everything a restart needs, and how it's saved without stopping training.

- **Model and optimizer:** sharded, saved asynchronously (copy to host memory, then write in the background).
- **Data and sampling state:** data cursors per source, curriculum position, RNG states.
- **RL state:** the sample buffer (or enough of it), in-flight rollout bookkeeping, weight version, controller settings and history.
- **Two tiers:** frequent saves to local NVMe or peer memory for fast restarts; less frequent copies to object storage for durability.
- **Verification:** every checkpoint is test-loaded (or checksum-verified against its manifest) once; only verified checkpoints are eligible for restart.
- **Retention:** keep the latest several, plus periodic ones for rollback ([Design 07](#design-scaling-rl)).

**The code.** Asynchronous sharded checkpointing of model, optimizer and RL state with PyTorch Distributed Checkpoint (sketch).

```python
import json, torch.distributed as dist
import torch.distributed.checkpoint as dcp
from torch.distributed.checkpoint.state_dict import get_state_dict

pending = None

def save_rl_checkpoint(step, model, optimizer, rl_state: dict, path_local: str):
    """rl_state: data cursors, RNG, buffer index, weight version, controller settings (small, JSON-able)."""
    global pending
    if pending is not None:
        pending.result()                               # at most one save in flight #!
    model_sd, optim_sd = get_state_dict(model, optimizer)
    state = {"model": model_sd, "optim": optim_sd}
    pending = dcp.async_save(state, checkpoint_id=f"{path_local}/step-{step}")   # training continues
    if dist.get_rank() == 0:
        with open(f"{path_local}/step-{step}.rl.json", "w") as f:
            json.dump({"step": step, **rl_state}, f)   # buffer contents saved separately by the buffer service
    return pending
```

**Going deeper.**

- **The buffer service checkpoints itself:** it persists its contents (or an index into stored rollouts) on the same step boundary, so trainer and buffer agree on what has been consumed.
- **Upload in the background, verify in the background:** the trainer only waits for the copy to host memory; upload and test-load run on separate processes.

### Stragglers, hangs and silent errors

**Scenario.** Step time crept from 41 to 52 seconds over two days. No failures, no alerts. One GPU was thermally throttling, and every collective waited for it.

![Stragglers and silent errors](diagrams/designs/concepts/ds14-c10.svg)

![Stragglers, hangs and silent errors](diagrams/designs/ds14-9.svg)

**What the picture shows.** The failures that don't announce themselves, and how each is detected.

- **Stragglers:** one slow rank slows every synchronous step. Detect with per-rank step and collective timings; the outlier rank is the suspect.
- **Hangs:** a collective that never completes. Detect with collective watchdogs and "no progress for N minutes" monitors; dump stack traces from every rank before restarting.
- **Silent data corruption:** a faulty GPU computes wrong values without errors. Detect with loss and gradient-norm spikes, NaN and infinity checks, cross-replica checksum comparisons of parameters, and periodic re-computation of a fixed probe on every GPU.
- **Silent rollout corruption:** an engine producing wrong output (see the rollout section); detected by canaries and peer comparison.
- **Response:** isolate the suspect node, verify with a targeted diagnostic, roll back to a checkpoint from before the corruption started if it affected training.

**The code.** Finding straggler ranks from per-rank step timings gathered each step (PyTorch Distributed).

```python
import torch, torch.distributed as dist

def straggler_check(step_time_s: float, step: int, z_threshold=4.0, log=print):
    """Gather every rank's compute time for this step; flag ranks far above the median."""
    t = torch.tensor([step_time_s], device="cuda")
    all_t = [torch.zeros_like(t) for _ in range(dist.get_world_size())]
    dist.all_gather(all_t, t)
    if dist.get_rank() == 0:
        times = torch.cat(all_t)
        med = times.median()
        mad = (times - med).abs().median() + 1e-6
        z = (times - med) / (1.4826 * mad)                      # robust z-score #!
        slow = (z > z_threshold).nonzero().flatten().tolist()
        if slow:
            log({"step": step, "straggler_ranks": slow, "times": times[slow].tolist(), "median": med.item()})
        return slow
```

**Going deeper.**

- **Time compute separately from communication:** a rank that is slow in compute is the straggler; ranks that are slow in communication are often waiting for it.
- **Keep a known-good probe per GPU:** a tiny fixed computation with a known result, run every few hundred steps on every GPU, catches many silent corruption cases cheaply.

### Observability, SLOs and fault injection

**Scenario.** The platform team believes recovery takes "about five minutes." The last three real recoveries took 9, 31 and 47 minutes. Nobody had measured, and one recovery path had never been exercised.

![Reliability you can prove](diagrams/designs/concepts/ds14-c11.svg)

![Observability, SLOs and fault injection](diagrams/designs/ds14-10.svg)

**What the picture shows.** Measuring reliability, and testing it on purpose.

- **SLOs per run:** goodput, time to detect, time to recover, maximum work lost per failure, and "no reward" rate.
- **Failure ledger:** every interruption recorded with its class, component, node, detection time, recovery time and root cause; reviewed weekly.
- **Dashboards by failure domain:** trainer, rollout fleet, reward tier, storage, network, each with its own health and recovery metrics.
- **Fault injection:** in staging and in rehearsal runs, kill trainer pods, crash rollout engines, inject network delay, fail a verifier and corrupt a checkpoint; confirm each recovery path and time it.
- **Game days before big runs:** a scheduled exercise where the on-call team handles injected failures on a rehearsal run.

**The YAML.** Injecting failures with Chaos Mesh in a rehearsal namespace: kill a rollout engine every 15 minutes and add latency to the verifier.

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: Schedule
metadata:
  name: kill-rollout-engine
  namespace: rl-rehearsal
spec:
  schedule: "*/15 * * * *"
  type: PodChaos
  historyLimit: 5
  concurrencyPolicy: Forbid
  podChaos:
    action: pod-kill
    mode: one                                   # one random engine each time #!
    selector:
      namespaces: [rl-rehearsal]
      labelSelectors: { app: rollout-engine }
---
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: slow-verifier
  namespace: rl-rehearsal
spec:
  action: delay
  mode: all
  selector:
    namespaces: [rl-rehearsal]
    labelSelectors: { app: verifier-math }
  delay: { latency: "2s", jitter: "500ms" }     # does back-pressure hold? #!
  duration: "20m"
```

**Going deeper.**

- **Untested recovery paths don't work:** any recovery path that hasn't run in the last month is assumed broken until a rehearsal proves otherwise.
- **The failure ledger drives investment:** the top three causes of lost goodput each quarter get engineering time; everything else waits.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds14-c12.svg)

- **Phase 1, measure:** goodput, failure ledger, per-phase recovery timing, node health history.
- **Phase 2, contain:** timeouts and fallbacks on every trainer dependency, reward circuit breakers, rollout retries and hedging, node burn-in and automatic drain.
- **Phase 3, recover fast:** asynchronous whole-state checkpointing with verification, hot spares, peer checkpoint loading, JobSet failure policies, verified weight sync.
- **Phase 4, harden:** straggler and silent-corruption detection, per-step trainer recovery at the largest scales, regular fault injection and game days.

**Trade-offs we accepted.**

- **Hot spares cost GPUs** that sit idle most of the time, in exchange for minutes-long restarts.
- **Deep health checks and canaries use capacity** on every node and engine, in exchange for catching silent failures.
- **Per-step recovery adds complexity** in the trainer and coordination; used only where restart cost justifies it.
- **Fault injection breaks things on purpose,** in rehearsal namespaces only, in exchange for knowing that recovery works.

**Open questions.**

- **Silent corruption at scale:** how much periodic verification is enough, and what does it cost?
- **Per-step recovery with expert and pipeline parallelism:** replica-group recovery works best for data parallelism; how far can it extend to other parallel dimensions?
- **Predictive maintenance:** can node telemetry predict failures early enough to drain nodes before they fail a run?
- **Cross-cluster resilience:** should one RL run span clusters so a cluster-level failure only slows it down?

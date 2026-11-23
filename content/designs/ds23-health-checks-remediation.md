---
id: design-health-remediation
title: "Design 23: Health checks across layers and automated remediation that survives fault storms"
short: "Health checks and remediation"
---
This design answers one question: *how do we detect faults in GPU nodes across hardware, network and software, fix them automatically, and keep that automation from making things worse when many things fail at once?* [Design 14](#design-reliable-rl-infra) covers how training jobs survive failures: checkpoints, restarts and hot spares. This design is about the fleet underneath: the checks that find bad GPUs, links and nodes, the remediation that repairs them without a human, and the safety mechanisms that stop remediation when a single shared cause (a switch, a power feed, a bad driver rollout) makes a hundred nodes look broken at once.

The short version: *detect at every layer, decide with evidence, repair by a ladder, and treat many simultaneous failures as one problem*. Passive signals (Xid errors, DCGM fields, logs, link counters) and active checks (diagnostics, bandwidth and NCCL tests) feed one event pipeline. It turns signals into node conditions with evidence, and taints a node before anything runs on it. Remediation climbs a ladder from the least disruptive action (restart the workload, reset one GPU) to the most (reimage, replace, send to the vendor), checking after each step. Before acting, a storm guard checks whether failures cluster in a shared failure domain or exceed rate limits. If they do, it stops node-level remediation, holds affected nodes in quarantine, and raises one domain-level incident instead of a hundred reboots.

### The use case and requirements

**Scenario.** The fleet has 256 nodes with 2,048 GPUs. Most days, one to three nodes develop a fault, and an engineer finds it hours later from a failed job. Then a top-of-rack switch firmware bug made links flap in three racks. The old remediation script saw 96 nodes failing their network check and rebooted all of them within 20 minutes. That killed every job in those racks, including ones that had been running fine, and the reboots didn't fix the switch.

![Remediation that made it worse](diagrams/designs/concepts/ds23-c1.svg)

**What the platform must deliver.**

- **Coverage of every layer:** GPU, memory, PCIe, NVLink and NVSwitch, power and thermals, NICs and the fabric, and the software stack (driver, fabric manager, container runtime, kubelet, device plugin).
- **Fast detection:** most faults detected within minutes, before a job is placed on a bad node.
- **Automatic repair:** most single-node faults remediated without a human, from detection to a node back in service.
- **Least-disruptive action first:** don't reboot a node when one GPU reset fixes it, and don't kill healthy jobs to repair a component they don't use.
- **Storm safety:** correlated failures across a failure domain trigger one incident and a hold, not mass remediation.
- **Low false positives:** the health system itself is measured, because wrong verdicts cost capacity.
- **A repair loop that closes:** nodes that keep failing go to a repair queue and, if needed, the vendor, with evidence attached.

**Out of scope:** job-level fault tolerance ([Design 14](#design-reliable-rl-infra)) and capacity planning for spares.

### The health model: layers, signals and failure domains

**Scenario.** Each team wrote its own checks. The training team checked GPUs, the network team checked switches, and nobody checked whether the device plugin still advertised 8 GPUs after a driver crash. Faults between layers went unnoticed until jobs failed.

![Health model](diagrams/designs/concepts/ds23-c2.svg)

![The health model: layers, signals and failure domains](diagrams/designs/ds23-1.svg)

**What the picture shows.** One catalog of what can fail, how each failure is observed, and which shared domains link nodes.

- **Hardware layer:** GPU compute and memory (ECC, row remapping), PCIe, NVLink and NVSwitch, power and thermals, host memory and disks, NICs or InfiniBand HCAs, and optics.
- **Network layer:** link state and error counters, fabric routing, congestion and pause storms, and the paths to storage and the API server.
- **Software layer:** driver and firmware versions, fabric manager, container runtime and toolkit, kubelet, CNI, device plugin and DRA driver, and mounted filesystems.
- **Passive signals:** what the system reports by itself (Xid events, DCGM fields, kernel logs, link counters, node conditions). Cheap and continuous.
- **Active signals:** what a test shows (diagnostics, bandwidth tests, NCCL collectives, file I/O). More conclusive, but they need the node or the link.
- **Failure domains:** rack, top-of-rack switch, spine group, power distribution unit, cooling loop, and software rollout cohort (nodes that got the same driver or firmware). The storm guard uses these.

**The YAML.** The check catalog: what each check covers, how often it runs and what it costs (a platform convention, read by the health agents).

```yaml
# health/catalog.yaml
checks:
- { name: xid-watch,        layer: hardware, kind: passive, source: kernel-log,  interval: continuous }
- { name: dcgm-fields,      layer: hardware, kind: passive, source: dcgm,        interval: 30s,
    fields: [ECC_DBE_VOL_TOTAL, ROW_REMAP_PENDING, ROW_REMAP_FAILURE, NVLINK_CRC_FLIT_ERROR_TOTAL, GPU_TEMP, POWER_VIOLATION] }
- { name: ib-counters,      layer: network,  kind: passive, source: sysfs,       interval: 60s,
    fields: [symbol_error, link_downed, port_rcv_errors, link_error_recovery] }
- { name: sw-conformance,   layer: software, kind: passive, source: node-agent,  interval: 5m }
- { name: dcgm-diag-r1,     layer: hardware, kind: active,  needs: idle-gpu,     interval: between-jobs, duration: 1m }
- { name: dcgm-diag-r3,     layer: hardware, kind: active,  needs: idle-node,    interval: after-repair, duration: 30m }   #!
- { name: nvbandwidth,      layer: hardware, kind: active,  needs: idle-node,    interval: between-jobs, duration: 2m }
- { name: nccl-allreduce-rack, layer: network, kind: active, needs: idle-rack,   interval: weekly-or-after-repair, duration: 10m }
domains: [rack, tor_switch, spine_group, pdu, cooling_loop, driver_cohort, firmware_cohort]
```

**Going deeper.**

- **Every check names its layer and domain:** a verdict can then say "GPU 3 on node r12n07" or "every node on tor-12", which decides between a node-level repair and a domain incident.
- **Use what exists before building:** DCGM, Node Problem Detector and packaged systems such as NVIDIA's NVSentinel already implement much of the detection, quarantine and remediation pipeline. The design here works the same way whether these are adopted or built.

### Passive detection: Xid, DCGM and logs

**Scenario.** A GPU logged Xid 79 ("fallen off the bus") at 02:14. The node stayed schedulable, Kueue placed a 64-GPU job on it at 02:30, and the job hung at initialization until someone noticed at 09:00.

![Passive detection](diagrams/designs/concepts/ds23-c3.svg)

![Passive detection: Xid, DCGM and logs](diagrams/designs/ds23-2.svg)

**What the picture shows.** How continuous signals become classified events within seconds.

- **Sources:** kernel logs (Xid, PCIe AER, machine checks), DCGM fields and health watches, InfiniBand and Ethernet counters, kubelet and container runtime logs.
- **Classification:** each signal maps to a fault class, a scope (one GPU, the node, a link) and a recommended first action, following NVIDIA's GPU debug guidelines for Xids.
- **Thresholds and rates:** counters matter as rates and trends. A few correctable errors are normal, but a rising rate or pending row remaps are not.
- **Immediate quarantine:** fatal classes taint the node at once, so no new pods land on it while the rest of the pipeline decides what to do.
- **Context with the event:** the job running on the GPU at the time, recent changes to the node, and earlier events, all attached as evidence.

**The code.** Classifying Xid events into fault classes and first actions (Python, simplified; follow the vendor's current guidance per GPU generation).

```python
XID_CLASSES = {
    # xid: (fault class, scope, first action)
    13:  ("app-or-hw",       "gpu",  "restart-workload"),   # graphics engine exception; often the application
    31:  ("app-or-hw",       "gpu",  "restart-workload"),   # memory page fault; usually the application
    43:  ("app",             "gpu",  "none"),               # GPU stopped processing; the application's error
    48:  ("memory-dbe",      "gpu",  "drain-then-reset-gpu"),
    63:  ("row-remap",       "gpu",  "drain-then-reset-gpu"),
    64:  ("row-remap-fail",  "gpu",  "drain-then-reboot"),     # remapping failed: replacement candidate
    74:  ("nvlink",          "node", "drain-then-reset-gpu"),
    79:  ("fell-off-bus",    "node", "drain-then-reboot"),  # fatal: taint immediately #!
    92:  ("sbe-rate",        "gpu",  "watch"),              # informational
    94:  ("contained-ecc",   "gpu",  "restart-workload"),
    95:  ("uncontained-ecc", "gpu",  "drain-then-reset-gpu"),
    119: ("gsp-timeout",     "node", "drain-then-reboot"),
    120: ("gsp-error",       "node", "drain-then-reboot"),
}
FATAL = {"fell-off-bus", "uncontained-ecc", "gsp-timeout", "gsp-error", "row-remap-fail"}

def classify(xid: int, gpu_index: int, node: str, repeats_24h: int) -> dict:
    cls, scope, action = XID_CLASSES.get(xid, ("unknown", "node", "investigate"))
    if cls.startswith("app") and repeats_24h >= 3:            # same GPU, different jobs: likely hardware
        cls, action = "suspect-hw", "drain-then-reset-gpu"
    return {"node": node, "gpu": gpu_index, "xid": xid, "class": cls, "scope": scope,
            "action": action, "taint_now": cls in FATAL}
```

**Going deeper.**

- **Application errors look like hardware errors:** Xid 13 or 31 from one job is usually that job. The same Xid on the same GPU across different jobs is the GPU. Keep per-GPU history to tell them apart.
- **Watch pending row remaps:** a GPU with pending remaps needs a reset to activate them. A GPU whose remapping failed needs replacement.

### Active checks: from preflight to burn-in

**Scenario.** A node passed every passive check but delivered 60% of normal NVLink bandwidth on one GPU. Every 512-GPU job that included it ran at the speed of its slowest rank. Nothing reported an error.

![Active check tiers](diagrams/designs/concepts/ds23-c4.svg)

![Active checks: from preflight to burn-in](diagrams/designs/ds23-3.svg)

**What the picture shows.** Active checks in tiers, by cost and by when the node can spare the time.

- **Preflight (seconds, at job start):** each pod checks its GPUs are visible, healthy and not in use, and that a small collective with its neighbors completes. A failure releases the job's placement before it wastes a gang ([Design 17](#design-gpu-scheduling)).
- **Between jobs (minutes, on idle nodes):** quick diagnostics (`dcgmi diag -r 1` or `-r 2`) and GPU-to-GPU bandwidth tests. These catch degraded but not failed hardware.
- **After repair and periodically (30 minutes or more):** long diagnostics (`-r 3` or `-r 4`), memory and stress tests, and multi-node NCCL tests across the rack. A node returns to service only after passing these.
- **Performance baselines, not just pass/fail:** bandwidth and collective results are compared with the fleet's distribution for that node type. A node in the bottom 1% is flagged even without an error.
- **Scheduling the checks:** idle-node checks run as low-priority, preemptible workloads through Kueue, so they use gaps instead of taking capacity from jobs.

**The code.** Flagging degraded nodes from bandwidth results against the fleet distribution (Python).

```python
import numpy as np

def degraded(results: dict[str, list[float]], fleet: list[float], z: float = -4.0, floor: float = 0.85) -> list[dict]:
    """results: node -> per-GPU NVLink bandwidth (GB/s); fleet: all healthy GPUs of this type."""
    med, mad = np.median(fleet), np.median(np.abs(np.array(fleet) - np.median(fleet))) * 1.4826
    flags = []
    for node, per_gpu in results.items():
        for gpu, bw in enumerate(per_gpu):
            score = (bw - med) / mad
            if score < z or bw < floor * med:                       # robust z-score or 15% below median #!
                flags.append({"node": node, "gpu": gpu, "bw": bw, "fleet_median": med, "z": round(score, 1)})
    return flags
```

**Going deeper.**

- **A slow GPU is a fault:** in synchronous training, one GPU at 60% bandwidth slows the whole job. Performance outliers are remediated like errors.
- **Keep preflight short:** a 30-second preflight on 1,024 pods is cheap compared with a job that hangs for an hour. A 10-minute preflight isn't.

### Network health: finding the bad link

**Scenario.** A 256-GPU job ran 30% slower than the day before. Every node passed its checks. The cause was one optical transceiver with a high symbol-error rate, retransmitting quietly on a single rail.

![Network localization](diagrams/designs/concepts/ds23-c5.svg)

![Network health: finding the bad link](diagrams/designs/ds23-4.svg)

**What the picture shows.** Network checks that localize problems to a link, a switch or a domain.

- **Counters per port:** symbol errors, link-down events, receive errors and error-recovery counts on hosts and switches, tracked as rates.
- **Link flaps:** a link that goes down and up is a different fault from a link that's slow. Both are recorded with timestamps.
- **Congestion signals:** for RoCE, pause-frame (PFC) rates and ECN marks. A pause storm from one host can slow a whole switch.
- **Pairwise tests:** bandwidth tests between pairs of nodes, per rail, arranged so every link is covered. The pattern of slow pairs identifies the common link.
- **Domain view:** when many links on one switch degrade together, the switch is the suspect, not the nodes. This feeds the storm guard.

**The code.** Localizing a bad link from a pairwise bandwidth matrix (Python).

```python
from collections import Counter

def suspect_links(pairs: dict[tuple[str, str], float], paths: dict[tuple[str, str], list[str]],
                  expected: float, slow: float = 0.8) -> list[tuple[str, int, int]]:
    """pairs: (src, dst) -> measured GB/s; paths: (src, dst) -> links traversed (host port, switch ports)."""
    bad_pairs = [p for p, bw in pairs.items() if bw < slow * expected]
    on_bad = Counter(l for p in bad_pairs for l in paths[p])
    on_all = Counter(l for p in pairs for l in paths[p])
    ranked = sorted(on_bad, key=lambda l: on_bad[l] / on_all[l], reverse=True)
    return [(l, on_bad[l], on_all[l]) for l in ranked if on_bad[l] / on_all[l] > 0.9]   # slow on ~every path #!
```

**Going deeper.**

- **The network team owns the switches, the health system owns the evidence:** a ticket with the suspect link, its counters and the failing pair tests gets fixed faster than "job is slow".
- **Drain the rail, not the node:** with multi-rail networks, a bad link can be excluded from NCCL's topology while it's repaired, if the job framework supports it.

### Software health and configuration conformance

**Scenario.** After a partial driver upgrade, 14 nodes ran a driver that didn't match their fabric manager version. The fabric manager failed to start, NVLink was unavailable, and jobs fell back to PCIe. Every hardware check passed.

![Software conformance](diagrams/designs/concepts/ds23-c6.svg)

![Software health and configuration conformance](diagrams/designs/ds23-5.svg)

**What the picture shows.** The software stack checked against a golden configuration per node type.

- **Versions:** driver, CUDA compatibility, fabric manager, firmware (GPU, NIC, BMC), OFED or NIC drivers, kernel, container toolkit.
- **Services:** fabric manager running, persistence mode, the device plugin or DRA driver advertising the expected GPUs, kubelet and runtime healthy, and the CNI ready.
- **Allocatable vs physical:** a node with 8 physical GPUs that advertises 7 has a problem, even if every visible GPU is healthy.
- **Mounts and paths:** shared filesystems mounted and responsive, local NVMe healthy, and reachability of the API server, registry and object storage.
- **Conformance as a check:** the node agent reports drift from the golden configuration. Drift is a fault with a scope (a rollout cohort) and a fix (finish or roll back the rollout).

**The code.** A conformance check against the golden configuration for the node type (Python, node agent).

```python
GOLDEN = {"h100-sxm": {"driver": "580.95.05", "fabricmanager": "580.95.05", "gpus": 8,
                       "nic_fw": "28.43.1014", "kernel": "6.8.0-60", "persistence_mode": True}}

def conformance(node_type: str, observed: dict) -> list[dict]:
    want, issues = GOLDEN[node_type], []
    for key, value in want.items():
        if observed.get(key) != value:
            issues.append({"check": key, "want": value, "got": observed.get(key)})
    if observed.get("driver") != observed.get("fabricmanager"):
        issues.append({"check": "driver==fabricmanager", "severity": "fatal"})       # NVLink unusable #!
    if observed.get("allocatable_gpus") != observed.get("gpus"):
        issues.append({"check": "allocatable", "want": observed.get("gpus"), "got": observed.get("allocatable_gpus")})
    return issues
```

**Going deeper.**

- **Rollouts are failure domains:** conformance drift usually comes from a rollout that stopped halfway. The fix is at the rollout, not at each node.
- **Version the golden configuration:** it changes with every planned upgrade, and checks must know which version a node is meant to be on during the rollout window.

### From signals to verdicts

**Scenario.** Three tools watched the same node: one set a custom node condition, one added a taint, and one paged the on-call. They disagreed about whether the node was healthy, and the remediation script acted on whichever it read first.

![Verdict pipeline](diagrams/designs/concepts/ds23-c7.svg)

![From signals to verdicts](diagrams/designs/ds23-6.svg)

**What the picture shows.** One pipeline from raw signals to a single verdict per node, with evidence.

- **Ingest:** agents send events (signal, class, scope, timestamp, evidence) to one pipeline.
- **Deduplicate and correlate:** repeats of the same fault are merged, and related events (an Xid and a failed diagnostic on the same GPU) are joined.
- **Verdict:** healthy, degraded (usable, but flagged for maintenance), quarantined (no new work) or failed (evacuate), with the evidence that led to it.
- **Expressed in Kubernetes:** node conditions say *why* (for example, `GpuXidFatal`, `NvlinkDegraded`, `SoftwareDrift`), taints say *what the scheduler must do*. Only the pipeline writes them.
- **Consumers:** the scheduler (taints), remediation (verdicts), dashboards and tickets (evidence). Nothing acts on raw signals directly.

**The YAML.** What a quarantined node looks like after the pipeline's verdict (node conditions and taint).

```yaml
status:
  conditions:
  - type: GpuXidFatal
    status: "True"
    reason: Xid79FellOffBus
    message: "GPU 3 (PCI 0000:9a:00.0) Xid 79 at 02:14:07Z; evidence: health-evt-7f2c"
    lastTransitionTime: "2026-11-19T02:14:09Z"
spec:
  taints:
  - key: health.example.com/quarantine            # written only by the health pipeline #!
    value: gpu-fatal
    effect: NoSchedule
  - key: health.example.com/evacuate
    value: gpu-fatal
    effect: NoExecute                             # added later by remediation, after the storm guard allows it
```

**Going deeper.**

- **Separate "don't schedule" from "evict":** quarantine (`NoSchedule`) is cheap and safe to apply immediately. Eviction (`NoExecute` or a drain) affects running work and goes through the storm guard.
- **Evidence IDs everywhere:** every condition, taint, ticket and remediation record references the same event IDs, so an engineer can reconstruct the reasoning.

### The remediation ladder

**Scenario.** The old policy for every fault was "reboot the node". For a single GPU with a pending row remap, that cost a 20-minute reboot and a 64-GPU job restart. A GPU reset would have taken 30 seconds and touched one rank.

![Remediation ladder](diagrams/designs/concepts/ds23-c8.svg)

![The remediation ladder](diagrams/designs/ds23-7.svg)

**What the picture shows.** Remediation as a ladder, climbed one step at a time with a check after each.

- **Step 0, workload:** the job restarts from its checkpoint on the remaining healthy capacity ([Design 14](#design-reliable-rl-infra)). Used for application-class faults.
- **Step 1, reset the GPU:** for recoverable GPU faults (pending row remaps, contained errors). The node stays up and other GPUs keep working, where the hardware supports single-GPU reset.
- **Step 2, reboot the node:** for driver, GSP or PCIe faults, after draining the node's workloads (with a checkpoint request first).
- **Step 3, reimage:** for software corruption or repeated failures after reboot.
- **Step 4, replace or repair:** cloud nodes are replaced through the provider API. On-premises nodes go to a repair queue and, with evidence, to the vendor for RMA.
- **Verification gate:** after each step, the node runs the post-repair checks. It returns to service only if they pass. Otherwise the ladder climbs one step.
- **Retry limits:** a node that needs the same step three times in a week skips straight to repair, instead of cycling.

**The code.** The ladder as a small state machine (Python, remediation controller).

```python
LADDER = ["restart-workload", "reset-gpu", "reboot", "reimage", "replace"]

def next_step(fault: dict, history: list[dict]) -> str:
    """fault: classified event; history: this node's remediations in the last 7 days."""
    start = {"restart-workload": 0, "drain-then-reset-gpu": 1, "drain-then-reboot": 2}.get(fault["action"], 2)
    tried = [h["step"] for h in history if h["fault_class"] == fault["class"]]
    if tried.count(LADDER[start]) >= 3:
        return "replace"                                            # repeat offender: stop cycling #!
    if tried and not history[-1]["verified"]:
        last = LADDER.index(history[-1]["step"])
        return LADDER[min(last + 1, len(LADDER) - 1)]               # previous step failed verification
    return LADDER[start]
```

**Going deeper.**

- **Drain politely:** before evicting a training pod, signal the job to checkpoint (or wait for its next checkpoint within a deadline), so the repair doesn't throw away hours of work.
- **Collect diagnostics before fixing:** a reboot erases evidence. Gather logs, `nvidia-bug-report` output and counters first, and attach them to the repair record.

### Handling fault storms

**Scenario.** The switch firmware bug from the first scenario: 96 nodes failed network checks within ten minutes, all in three racks under one spine group. Node-level remediation rebooted them all. The real fix, rolling back the switch firmware, came three hours later from the network team.

![Storm guard](diagrams/designs/concepts/ds23-c9.svg)

![Handling fault storms](diagrams/designs/ds23-8.svg)

**What the picture shows.** The storm guard between verdicts and remediation.

- **Correlation by failure domain:** before remediating, the guard groups recent failures by rack, switch, spine group, power feed, cooling loop and rollout cohort. If one domain holds far more failures than its share, the cause is probably the domain.
- **Rate limits:** a maximum number of nodes in remediation at once, per pool and per domain, and a maximum per hour. Above them, new remediations wait.
- **Minimum healthy capacity:** remediation that would leave a pool below a healthy floor is held. (The medik8s NodeHealthCheck operator's `minHealthy` setting implements the same idea for node remediation.)
- **Storm mode:** when the guard trips, affected nodes stay quarantined (no new work) but aren't drained or rebooted. Running jobs that still work keep running. One incident is raised for the domain, with the evidence, and it pages a human.
- **Leaving storm mode:** a person, or the domain's own health returning, clears it. Nodes are then re-checked and remediated individually at the normal rate.

**The code.** Detecting a domain-level storm before acting (Python, storm guard).

```python
from collections import Counter

def storm_check(failing: list[dict], fleet: list[dict], window_min: int = 15,
                max_concurrent: int = 8, domain_share: float = 0.3) -> dict:
    """failing: nodes with new failure verdicts in the window; fleet: all nodes with their domains."""
    if len(failing) <= 2:
        return {"storm": False}
    for dim in ("tor_switch", "spine_group", "pdu", "cooling_loop", "driver_cohort", "firmware_cohort", "rack"):
        counts = Counter(n[dim] for n in failing)
        domain, hits = counts.most_common(1)[0]
        size = sum(1 for n in fleet if n[dim] == domain)
        if hits >= 3 and hits / size >= domain_share:                       # a third of the domain failing #!
            return {"storm": True, "reason": f"{dim}={domain}", "hits": hits, "domain_size": size,
                    "action": "hold-remediation-and-open-domain-incident"}
    if len(failing) > max_concurrent:
        return {"storm": True, "reason": "rate", "hits": len(failing), "action": "queue-remediation"}
    return {"storm": False}
```

**Going deeper.**

- **A storm is often caused by the health system:** a bad check, a monitoring outage or a new threshold can make healthy nodes look failed. Storm mode protects against that too.
- **Keep quarantine cheap and remediation expensive:** in a storm, stopping new placements is almost always right. Evicting and rebooting rarely is.

### Safety rails for automation

**Scenario.** A new health check with a wrong threshold went live on a Monday. It marked 40% of nodes as degraded and queued them all for reboot. The rate limit slowed it down, but nobody noticed until 30 nodes had rebooted.

![Safety rails](diagrams/designs/concepts/ds23-c10.svg)

![Safety rails for automation](diagrams/designs/ds23-9.svg)

**What the picture shows.** How changes to the health system itself are made safe.

- **Shadow mode for new checks and rules:** they produce verdicts that are logged but not acted on, and are compared with existing checks for a week before enforcement.
- **Staged enforcement:** a new rule enforces on one pool first, like any other change ([Design 19](#design-scheduling-impact)).
- **Budgets:** a daily remediation budget per pool (for example, 3% of nodes). Exceeding it pauses automation and pages a human.
- **Kill switch:** one setting pauses all remediation while keeping detection and quarantine running.
- **Audit:** every action records its trigger, evidence, rule version and result.
- **Check the checks:** a small set of known-good nodes and known-bad parts (kept for the purpose) verifies that checks pass and fail when they should.

**The YAML.** A NodeHealthCheck with a minimum-healthy floor and pause support (medik8s), for the generic not-ready case alongside the GPU-specific pipeline.

```yaml
apiVersion: remediation.medik8s.io/v1alpha1
kind: NodeHealthCheck
metadata: { name: gpu-workers-notready }
spec:
  selector:
    matchLabels: { node-pool: h100 }
  minHealthy: "90%"                                  # never remediate below 90% healthy in the pool #!
  unhealthyConditions:
  - { type: Ready, status: "False",   duration: 300s }
  - { type: Ready, status: "Unknown", duration: 300s }
  pauseRequests: []                                  # the kill switch: add a reason to pause new remediations
  remediationTemplate:
    apiVersion: self-node-remediation.medik8s.io/v1alpha1
    kind: SelfNodeRemediationTemplate
    name: self-node-remediation-automatic-strategy-template
    namespace: medik8s
```

**Going deeper.**

- **Automation that can't be paused will eventually do damage:** the kill switch is tested in drills, not discovered during an incident.
- **Version the rules like code:** a remediation record that names the rule version makes it possible to find every action a bad rule took.

### Measuring the health system

**Scenario.** The team believed automated remediation "handled most things". The data showed 35% of remediated nodes failed again within a week, and the same 11 nodes accounted for half of all remediations.

![Health metrics](diagrams/designs/concepts/ds23-c11.svg)

![Measuring the health system](diagrams/designs/ds23-10.svg)

**What the picture shows.** The metrics that show whether health checks and remediation work.

- **Time to detect:** from fault to verdict, by fault class.
- **Time to repair:** from verdict to back in service, by ladder step.
- **Automation rate:** the share of faults fixed without a human.
- **Recurrence:** the share of repaired nodes that fail again within 7 days. High recurrence means the ladder stops too low.
- **Repeat offenders:** nodes and parts with the most remediations, sent to the repair queue with their history.
- **False positives:** verdicts later shown wrong (a node that passed every deep check after quarantine). Each costs capacity.
- **Capacity lost:** GPU-hours in quarantine, repair and drained-but-healthy states, per week.

**The code.** Weekly health-system metrics from remediation records (Python).

```python
import pandas as pd

def health_metrics(r: pd.DataFrame) -> dict:
    """r: one row per remediation with fault_ts, verdict_ts, in_service_ts, node, step, human, refail_7d, false_positive."""
    detect = (r.verdict_ts - r.fault_ts).dt.total_seconds() / 60
    repair = (r.in_service_ts - r.verdict_ts).dt.total_seconds() / 60
    offenders = r.node.value_counts()
    return {
        "mttd_min_p50": detect.median(), "mttr_min_p50_by_step": repair.groupby(r.step).median().round(0).to_dict(),
        "automation_rate": round(1 - r.human.mean(), 3),
        "recurrence_7d": round(r.refail_7d.mean(), 3),                      # 35% means the ladder stops too low #!
        "false_positive_rate": round(r.false_positive.mean(), 3),
        "top_offenders": offenders.head(10).to_dict(),
        "offender_share": round(offenders.head(11).sum() / len(r), 3),
    }
```

**Going deeper.**

- **Recurrence decides the ladder:** if reboots fix a fault class for a day and then it returns, start that class one step higher.
- **Share the evidence with vendors:** RMA requests with diagnostics, Xid history and counters are approved faster, and fleet-wide data shows batch problems with a part.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds23-c12.svg)

- **Phase 1, detect and quarantine (weeks 1–4):** passive detection on every node, the event pipeline, verdicts as node conditions and quarantine taints. No automatic remediation yet.
- **Phase 2, active checks (weeks 3–8):** preflight, between-job diagnostics and bandwidth checks, post-repair burn-in, pairwise network tests and software conformance.
- **Phase 3, remediation (weeks 6–12):** the ladder for the most common fault classes, with the storm guard, budgets and kill switch, in shadow mode first and then pool by pool.
- **Phase 4, close the loop (weeks 10–16):** repair queue and vendor RMA integration, metrics and recurrence-driven ladder tuning.

**Trade-offs we accepted.**

- **Quarantine before certainty:** some healthy nodes are held for minutes while evidence is gathered.
- **Storm mode leaves faults unrepaired** until a human confirms the cause, by design.
- **Active checks cost GPU time** on idle nodes and at job start.
- **More components to operate:** agents, the pipeline, the storm guard and the repair queue are a system of their own, with their own failure modes.

**Open questions.**

- **Predicting failures:** can trends in correctable errors, temperatures and link counters predict failures well enough to drain nodes before they fail?
- **Partial nodes:** should a node with one bad GPU keep serving 7-GPU or smaller jobs instead of going to repair?
- **Cross-cluster domains:** how to detect a storm whose cause spans clusters, such as a firmware cohort deployed across providers ([Design 18](#design-multicloud-fleet))?
- **Learning from incidents:** can past storm incidents tune domain thresholds automatically, or should they stay hand-set?

---
id: design-inference-cloud-parity
title: "Design 39: Shipping inference to cloud platforms in lockstep: new architectures, feature parity, drift, CI/CD with shadow traffic, and validation on a shared accelerator pool (on AWS)"
short: "Inference on cloud platforms in lockstep"
---
This design answers one question: *how do we bring up inference for a new model architecture and ship it, with every new inference feature, to a cloud platform on the same day it ships on our first-party platform, so that it behaves the same on both?* The company serves its models on its own first-party API and also through cloud platforms. This design uses AWS as the cloud platform throughout. There, the company runs the same inference server and load balancer in AWS accounts it operates for the cloud channel, on Amazon EKS, on NVIDIA GPU instances (P5en, P6-B200) and on AWS Trainium2. Earlier designs cover serving itself ([Design 21](#design-llm-serving)), the serving benchmark toolkit ([Design 22](#design-serving-benchmarks)), the multi-cloud fleet ([Design 18](#design-multicloud-fleet)) and networking ([Design 05](#design-networking)). This design is about the seam between the first-party platform and the cloud one, where launches slip and behavior drifts.

The short version: *build each serving release once, let the platform change only what it must, prove parity with tests that run on both platforms, and fix differences where they start*. A serving release is one immutable bundle: the server image by digest, model artifacts, compiled accelerator artifacts, a config bundle and the load balancer config. Both platforms deploy the same bundle; the AWS layer is an overlay with a closed list of keys it may set. New architectures and new features (structured sampling, prompt caching and the rest) are brought up against a parity suite of numerics, conformance and performance tests that runs on every accelerator the cloud platform uses. Drift is detected by comparing the effective config and the telemetry of the two platforms, and a difference found is fixed in the shared code or the shared config, not patched on one side. The CI/CD pipeline runs correctness checks, performance baselines and shadow traffic before a canary, and it runs on the same constrained accelerator pool that serves customers, by borrowing headroom and giving it back before it is needed. Observability data from both providers is joined in one place, so bottlenecks, cost anomalies and regressions are found from production workloads.

### The use case and requirements

**Scenario.** A new model with a hybrid attention layout launched on the first-party API on a Tuesday. It reached the AWS deployment five weeks later. Structured output arrived there two releases after that, and prompt caching hit 61% of cacheable tokens on the first-party API against 22% on AWS for the same customers' traffic. Merge to production took a median of 2 days on the first-party platform and 9 on AWS. Each difference had a workaround in the AWS deployment: a flag, a patched kernel, a different router setting. Nobody could list them all.

![Inference on cloud platforms in lockstep](diagrams/designs/concepts/ds39-c1.svg)

**What the system must deliver.**

- **Lockstep launches:** a new model or a new version reaches the cloud platform on the same day as the first-party platform, from the same release.
- **Feature parity:** structured sampling, prompt caching, tool use, long context and every new inference feature behave the same on every platform, or the difference is declared and tested.
- **No silent drift:** any difference in config, deployment pattern or telemetry between the platforms is detected, explained, and either removed or declared.
- **Fixes at the source:** cross-platform bugs are fixed in the shared server, kernel, config or load balancer, with a regression test in the shared suite.
- **CI/CD for the server and the load balancer:** correctness checks, performance baselines for throughput and latency, and shadow traffic, before any customer sees a change.
- **Short merge-to-production:** hours to a day on every platform, with validation fast, parallel and cheap enough to run on the serving pool without risking its SLOs.
- **One view of production:** observability and cost data from both providers, comparable, so performance bottlenecks, cost anomalies and regressions are found and fixed from real workloads.

**Out of scope:** the commercial terms with cloud providers, serving engine internals ([Design 21](#design-llm-serving)), and how the first-party API itself is operated. The cloud platform's own managed layers in front of the company's deployment are treated as given; this design is about what the company runs and ships.

### One release, two platforms: the serving release and the platform overlay

**Scenario.** The AWS deployment had its own Helm values file, copied from the first-party one 14 months earlier. It had since gained 212 keys. Some were real platform needs, such as the EFA device settings. Most were old fixes nobody had removed: a smaller maximum batch from an incident a year ago, a disabled CUDA graph capture, a router timeout raised once and never lowered.

![One release, two platforms](diagrams/designs/concepts/ds39-c2.svg)

![One release, two platforms: the serving release and the platform overlay](diagrams/designs/ds39-1.svg)

**What the picture shows.** One immutable release promoted to every platform, and a thin overlay per platform with a closed list of keys.

- **The serving release:** the server image by digest, model weights by content hash, compiled accelerator artifacts for each target (CUDA graphs are captured at start, Neuron programs are compiled ahead of time), the engine config, the load balancer config and the parity suite version that passed. It is built once and never rebuilt for a platform.
- **Promotion, not rebuild:** a release is promoted to the first-party platform and to AWS from the same record. On AWS, images are pulled from Amazon ECR repositories in each serving Region, replicated from the build Region by ECR's cross-Region replication ([ECR replication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/replication.html)), so the digest is the same everywhere.
- **The platform overlay:** a small file per platform and accelerator that may set only keys on an allow-list: device and network settings (EFA, Neuron devices), node selectors and tolerations, Region and endpoint names, the telemetry exporter, and capacity numbers. Engine behavior keys, such as batch limits, sampling defaults, cache policy and kernel choices, are not on the list.
- **A key not on the list fails the build:** a platform that needs a behavior change gets it in the shared config, for every platform, or as a declared, tested difference with an owner and an expiry date.
- **Deployed by GitOps:** on AWS, each serving cluster reads the release record and the overlay from Git through Argo CD, run as the managed Argo CD capability of EKS ([EKS Capabilities](https://docs.aws.amazon.com/eks/latest/userguide/capabilities.html)).
- **What happened to the 212 keys:** 31 were platform needs and moved to the overlay. 9 were real behavior changes and moved to the shared config for every platform. The other 172 were deleted, and the parity suite passed without them.

**The code.** Validating a platform overlay against the allow-list and listing declared differences that have expired (Python).

```python
from datetime import date

ALLOWED = {                                  # keys a platform overlay may set
    "nodeSelector", "tolerations", "resources.accelerator", "network.efa", "neuron.visible_cores",
    "region", "endpoints.*", "telemetry.exporter", "replicas.min", "replicas.max"}
DECLARED = {                                 # behavior differences accepted with an owner and an expiry
    ("aws-trn2", "engine.structured_output.backend"): ("serving-neuron", date(2026, 12, 31)),
}

def matches(key: str) -> bool:
    return key in ALLOWED or any(a.endswith(".*") and key.startswith(a[:-1]) for a in ALLOWED)

def check_overlay(platform: str, overlay: dict, today: date) -> list[str]:
    errors = []
    for key in overlay:
        if matches(key):
            continue
        d = DECLARED.get((platform, key))
        if d is None:
            errors.append(f"{platform}: '{key}' is a behavior key; change the shared config or declare it")  #!
        elif d[1] < today:
            errors.append(f"{platform}: declared difference '{key}' (owner {d[0]}) expired {d[1]}")
    return errors

aws_p5en = {"nodeSelector": {"node.kubernetes.io/instance-type": "p5en.48xlarge"}, "network.efa": 16,
            "endpoints.metrics": "aps-workspaces", "engine.max_num_seqs": 128}
aws_trn2 = {"neuron.visible_cores": "0-63", "engine.structured_output.backend": "outlines"}
for p, o in [("aws-p5en", aws_p5en), ("aws-trn2", aws_trn2)]:
    print(p, check_overlay(p, o, date(2026, 11, 24)) or "ok")
```

**Going deeper.**

- **The first-party platform has an overlay too:** it is held to the same allow-list. Otherwise the first-party values become the real config and the cloud platform is always the one that drifted.
- **Capacity numbers are allowed to differ:** replica counts and autoscaling limits are platform decisions. What they may not differ in is anything that changes what a request returns or how fast one replica serves it.

### Bringing up a new model architecture on AWS accelerators

**Scenario.** The hybrid-attention model ran on H100s on the first-party platform from day one. On AWS, the P5en deployment needed a different tensor parallel layout because of how its NVLink domain was configured, and the Trainium2 deployment needed the new attention kernel written for Neuron. The first Trainium2 build produced fluent text and a 1.8-point drop on the reasoning eval. It took two weeks to find that a rotary embedding was computed in lower precision than on the GPU.

![New architecture bring-up](diagrams/designs/concepts/ds39-c3.svg)

![Bringing up a new model architecture on AWS accelerators](diagrams/designs/ds39-2.svg)

**What the picture shows.** Bring-up as a gated sequence that runs on each accelerator target in parallel, starting before the model is final.

- **Start from the architecture, not the checkpoint:** the research team publishes the architecture and a small checkpoint of it weeks before launch. Bring-up starts on that, so kernels, parallel layouts and compiled artifacts exist when the final weights arrive.
- **Targets on AWS:** P5en (H200) and P6-B200 instances for GPU serving, and Trn2 for Trainium2 serving through the vLLM Neuron plugin. Each target has an owner on the cloud integration team and a partner on the core inference team.
- **Layout per target:** tensor and expert parallelism chosen per instance type and measured, not copied, because memory per accelerator and interconnect differ.
- **Compiled artifacts are part of the release:** Neuron compiles static-shape programs, one per bucket of sequence length and batch size, during server warmup, and can load precompiled artifacts instead, so production nodes never compile at startup ([vLLM Neuron features](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/vllm-neuron/docs/guides/features-guide.html)). The buckets are chosen from the traffic shapes of [Design 22](#design-serving-benchmarks), and the artifacts are built in CI and stored with the release.
- **Numerics parity before evals:** each target runs a fixed set of prompts and compares its logits with the reference implementation, layer by layer if needed. A drift in a layer shows up here in minutes; an eval regression would show it in days, without saying where.
- **Then evals and performance:** the eval suite ([Design 15](#design-eval-infra)) at the launch bar, and the performance baseline for the release's fingerprint ([Design 22](#design-serving-benchmarks)).
- **What the parity check would have shown:** the rotary embedding layer was the first one whose output diverged beyond tolerance on Trainium2. The fix was in the shared model definition, which now states the precision of that computation for every backend.

**The code.** Comparing a target's next-token distributions with the reference on a fixed prompt set (Python).

```python
import math

def softmax(logits: list[float]) -> list[float]:
    m = max(logits)
    ex = [math.exp(x - m) for x in logits]
    s = sum(ex)
    return [x / s for x in ex]

def kl(p: list[float], q: list[float]) -> float:
    return sum(pi * math.log(pi / max(qi, 1e-12)) for pi, qi in zip(p, q) if pi > 0)

def parity(reference: list[list[float]], target: list[list[float]], max_kl: float = 1e-3,
           min_top1: float = 0.99) -> dict:
    """Each entry is the logits at one position of one prompt. Greedy agreement and mean KL over positions."""
    kls, agree = [], 0
    for r, t in zip(reference, target):
        p, q = softmax(r), softmax(t)
        kls.append(kl(p, q))
        agree += max(range(len(r)), key=r.__getitem__) == max(range(len(t)), key=t.__getitem__)
    mean_kl, top1 = sum(kls) / len(kls), agree / len(kls)
    ok = mean_kl <= max_kl and top1 >= min_top1                                      #!
    return {"mean_kl": f"{mean_kl:.1e}", "top1_agreement": round(top1, 3), "pass": ok}

ref = [[2.0, 1.0, 0.1, -1.0], [0.5, 2.5, 0.0, 0.2], [1.2, 1.1, 0.9, -0.3]]
good = [[2.001, 0.999, 0.1, -1.0], [0.5, 2.499, 0.001, 0.2], [1.2, 1.101, 0.9, -0.3]]
bad = [[1.9, 1.05, 0.2, -0.9], [0.6, 2.3, 0.1, 0.3], [1.05, 1.15, 0.9, -0.2]]      # a layer in lower precision
print("p5en", parity(ref, good))
print("trn2", parity(ref, bad))
```

**Going deeper.**

- **Tolerances are per dtype and per layer:** BF16 and FP8 paths have different expected error. The thresholds are set from runs of the reference against itself on different hardware, not chosen by hand.
- **Bring-up is a shared calendar:** the launch date is the same for both platforms from the start. A target that can't make it is a launch decision taken by both teams, not a slip discovered afterwards.

### Bringing new inference features to the cloud platform

**Scenario.** Structured sampling shipped on the first-party API with a grammar backend that compiled JSON schemas on the GPU host. On AWS it worked on P5en on day one. On Trainium2 it worked but served structured requests 30% slower, and the reason was not in the feature at all: in the vLLM Neuron plugin, turning on structured outputs makes a dense model run on the older model runner instead of Model Runner V2 ([vLLM Neuron features](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/vllm-neuron/docs/guides/features-guide.html)). Prompt caching had the opposite problem: the engine supported it everywhere, but the AWS load balancer spread requests without regard to prefixes, so most cached prefixes were on the wrong replica.

![Feature parity](diagrams/designs/concepts/ds39-c4.svg)

![Bringing new inference features to the cloud platform](diagrams/designs/ds39-3.svg)

**What the picture shows.** Each feature delivered with a parity matrix of correctness and performance per platform and accelerator, owned jointly by the core inference team and the cloud integration team.

- **A feature is not done until the matrix is green:** the core inference team builds the feature; the cloud integration team owns everything platform-specific that gets it to production on AWS: the accelerator backend, the overlay, the router behavior, the telemetry and the runbook.
- **Correctness cells:** conformance tests for the feature on each target. For structured sampling: every output validates against its schema, a fixed set of hard schemas compiles within its time budget, and refusals and stop conditions behave the same. For prompt caching: cached and uncached requests return identical greedy outputs, and cache eviction under memory pressure never returns another request's prefix.
- **Performance cells:** the same feature's throughput and latency against its own baseline per target, so "works but 30% slower" is a red cell, not a footnote.
- **Behavior that lives outside the engine:** prompt caching depends on the load balancer sending a prefix to the replica that holds it. The same router, with prefix-aware endpoint picking through the Gateway API Inference Extension ([Design 21](#design-llm-serving)), runs on every platform, so cache hit rate is a router property that the matrix tests too.
- **Declared differences:** when a cell can't be green by launch, the difference is declared in the release record with an owner and a date, shown in the platform's documentation, and tested as a difference.
- **What the matrix made visible:** the Trainium2 structured-output cell went red on performance on the first run. The fix was upstream, in the plugin's runner support, with a declared difference until it shipped. Prompt cache hit rate on AWS rose from 22% to 58% once the same prefix-aware router ran there.

**The code.** Building the parity matrix from test results and deciding whether a feature can launch (Python).

```python
def matrix(results: list[dict], targets: list[str], declared: set[tuple[str, str, str]]) -> dict:
    """results: feature, target, kind ('correctness' | 'performance'), passed, detail."""
    cells, blockers = {}, []
    for r in results:
        cells[(r["feature"], r["target"], r["kind"])] = r
    features = sorted({r["feature"] for r in results})
    for f in features:
        for t in targets:
            for kind in ("correctness", "performance"):
                c = cells.get((f, t, kind))
                if c is None:
                    blockers.append(f"{f} on {t}: no {kind} result")
                elif not c["passed"] and (f, t, kind) not in declared:
                    blockers.append(f"{f} on {t}: {kind} failed ({c['detail']})")       #!
    return {"launch": not blockers, "blockers": blockers}

res = [
    {"feature": "structured_sampling", "target": t, "kind": "correctness", "passed": True, "detail": ""}
    for t in ("first-party-h100", "aws-p5en", "aws-trn2")] + [
    {"feature": "structured_sampling", "target": "first-party-h100", "kind": "performance", "passed": True, "detail": ""},
    {"feature": "structured_sampling", "target": "aws-p5en", "kind": "performance", "passed": True, "detail": ""},
    {"feature": "structured_sampling", "target": "aws-trn2", "kind": "performance", "passed": False,
     "detail": "-30% tokens/s, model runner V1"}]
targets = ["first-party-h100", "aws-p5en", "aws-trn2"]
print(matrix(res, targets, declared=set()))
print(matrix(res, targets, declared={("structured_sampling", "aws-trn2", "performance")}))
```

**Going deeper.**

- **One API contract, tested from the outside:** the conformance tests call the public API of each platform, not the engine. A difference introduced by any layer in between shows up.
- **The integration team sits in the feature's design review:** platform constraints, such as static shapes on Trainium2 or the instance types available in a Region, change designs cheaply at review time and expensively after launch.

### Config drift: finding it and removing it at the source

**Scenario.** A customer reported that the same request returned a shorter answer on AWS. The difference was a default: the AWS deployment's maximum output tokens came from an environment variable set in its Deployment manifest, which overrode the release config. The first-party platform had changed its default three months earlier. Nothing compared the two.

![Config drift](diagrams/designs/concepts/ds39-c5.svg)

![Config drift: finding it and removing it at the source](diagrams/designs/ds39-4.svg)

**What the picture shows.** The effective config of every running server, collected and compared across platforms continuously.

- **Effective, not declared:** each server publishes the config it actually resolved at startup, after flags, environment variables, files and defaults, with the source of each value, on an internal endpoint. The release config is what was meant; the effective config is what runs.
- **Compared per fingerprint:** the drift job groups servers by model and release, and compares every key across platforms. Keys on the overlay allow-list are expected to differ; declared differences are expected to differ in the declared way; anything else is drift.
- **Fixed at the source:** a drift finding names the source of the value (here, an environment variable in a manifest) and the fix goes there: the variable is removed and the default lives only in the shared config. A drift finding is never closed by copying the value to the other platform.
- **Runtime flags under the same rules:** flags that change at runtime, such as a feature's rollout percentage, are served on AWS from AWS AppConfig with a staged deployment strategy, and a deployment rolls back automatically if a chosen CloudWatch alarm fires during it ([AppConfig automatic rollback](https://docs.aws.amazon.com/appconfig/latest/userguide/monitoring-deployments.html)). The flag definitions come from the same release record as the first-party flags, so their values can be compared too.
- **Deployment patterns are config:** surge and drain settings, readiness probes, pod disruption budgets and the load balancer's health checks are compared like engine keys. A difference there changes behavior during a deploy, which is when cross-platform incidents tend to happen.

**The code.** Comparing effective configs across platforms and attributing each drift to its source (Python).

```python
def drift(effective: dict[str, dict[str, tuple]], allowed: set[str], declared: set[str]) -> list[str]:
    """effective: platform -> {key: (value, source)}. Returns findings for unexpected differences."""
    platforms = sorted(effective)
    keys = set().union(*(effective[p] for p in platforms))
    findings = []
    for k in sorted(keys):
        if k in allowed or k in declared:
            continue
        vals = {p: effective[p].get(k, ("<unset>", "default")) for p in platforms}
        if len({v for v, _ in vals.values()}) > 1:
            where = ", ".join(f"{p}={v} (from {s})" for p, (v, s) in vals.items())
            findings.append(f"{k}: {where}")                                              #!
    return findings

eff = {
    "first-party": {"max_output_tokens_default": (8192, "release config"), "router.timeout_s": (600, "release config"),
                    "region": ("fp-east", "overlay")},
    "aws-p5en": {"max_output_tokens_default": (4096, "env MAX_OUTPUT_TOKENS in Deployment"),
                 "router.timeout_s": (600, "release config"), "region": ("us-east-1", "overlay")}}
for f in drift(eff, allowed={"region"}, declared=set()):
    print(f)
```

**Going deeper.**

- **Defaults belong in one place:** most drift comes from defaults set in two places. The server refuses to start if an environment variable sets a key that the release config also sets, and logs which one it would have used.
- **Drift findings are counted:** the number of open drift findings per platform is on the integration team's dashboard. A rising number means a deployment path is bypassing the release.

### Observability that means the same thing on every platform

**Scenario.** An incident review compared TTFT on the two platforms. The first-party dashboard measured from the load balancer; the AWS one measured from the engine. AWS looked 140 ms faster at p95 for the same traffic. It was not; it was measuring less of the request.

![Telemetry parity](diagrams/designs/concepts/ds39-c6.svg)

![Observability that means the same thing on every platform](diagrams/designs/ds39-5.svg)

**What the picture shows.** One telemetry schema emitted by the shared server and router, collected by each provider's tools, and queried the same way.

- **The schema lives in the server and the router:** metric names, labels, histogram buckets and the point where each latency is measured are defined in the shared code, with the definitions of [Design 22](#design-serving-benchmarks). The platform only decides where the data goes.
- **On AWS:** the CloudWatch Observability EKS add-on with OpenTelemetry Container Insights collects engine metrics from vLLM and SGLang, NVIDIA GPU metrics through the DCGM exporter, and attributes GPU, Trainium, Inferentia and EFA metrics to the pods that use them; Accelerated Compute Insights shows them by inference workload, and they can be queried with PromQL ([Accelerated Compute Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Accelerated-Compute-Insights.html)). The same series are also written to Amazon Managed Service for Prometheus, which the pipeline's analysis queries read.
- **Accelerator metrics are mapped, not compared raw:** GPU and Trainium report different counters. Each is mapped to the same small set of platform-neutral series (busy fraction, memory used, interconnect bytes), and anything without a mapping stays platform-specific and is never put on a cross-platform chart.
- **One request ID end to end:** the router assigns it, the engine logs it, the trace carries it. A request can be followed through either platform with the same query.
- **A conformance test for telemetry:** a release passes only if each platform emits every required series with the required labels during the parity suite, so a missing metric is a failed build, not a gap discovered in an incident.

**The code.** Checking that a platform emits every required series with its labels during the parity run (Python).

```python
REQUIRED = {   # series -> labels every platform must attach
    "inference:ttft_seconds_bucket": {"model", "release", "platform", "accelerator", "le"},
    "inference:tpot_seconds_bucket": {"model", "release", "platform", "accelerator", "le"},
    "inference:output_tokens_total": {"model", "release", "platform", "accelerator", "feature"},
    "inference:prefix_cache_hit_tokens_total": {"model", "release", "platform", "accelerator"},
    "inference:accelerator_busy_ratio": {"node", "accelerator", "platform"},
}

def conformance(scraped: dict[str, set[str]]) -> list[str]:
    """scraped: series name -> union of label names seen during the parity run on one platform."""
    problems = []
    for name, labels in REQUIRED.items():
        if name not in scraped:
            problems.append(f"missing series {name}")
        elif not labels <= scraped[name]:
            problems.append(f"{name}: missing labels {sorted(labels - scraped[name])}")      #!
    return problems

aws_trn2 = {"inference:ttft_seconds_bucket": {"model", "release", "platform", "accelerator", "le"},
            "inference:tpot_seconds_bucket": {"model", "release", "platform", "accelerator", "le"},
            "inference:output_tokens_total": {"model", "release", "platform", "accelerator"},
            "inference:accelerator_busy_ratio": {"node", "accelerator", "platform"}}
print(conformance(aws_trn2))
```

**Going deeper.**

- **Where TTFT is measured is part of its name:** the router's TTFT and the engine's TTFT are different series with different names. Dashboards that compare platforms may only use the router's.
- **Cross-account views:** each serving account sends its telemetry to a central monitoring account through CloudWatch cross-account observability, so one query covers every AWS Region the deployment runs in.

### Hard cross-platform bugs: finding the layer, fixing it there

**Scenario.** Under heavy load on AWS, about one request in 40,000 returned a response that stopped mid-sentence with a normal finish reason. It never happened on the first-party platform. The AWS team had added a retry in the router for short responses, which hid it from customers and doubled the cost of every affected request. The cause, found six weeks later, was a race in the engine's stream handling that only appeared with the larger socket buffers set on AWS hosts.

![Cross-platform bugs](diagrams/designs/concepts/ds39-c7.svg)

![Hard cross-platform bugs: finding the layer, fixing it there](diagrams/designs/ds39-6.svg)

**What the picture shows.** A method for bugs that only appear on one platform: reproduce on both, swap one factor at a time until the bug moves, fix the factor's owner, and add the test to the shared suite.

- **Reproduce with recorded shapes:** the request shapes around the failures (arrival times, token counts, prefix structure, features used) are replayed on both platforms at the same load ([Design 22](#design-serving-benchmarks)). A bug that can't be reproduced isn't assigned yet.
- **List the factors that differ:** image, release config, overlay, kernel and driver versions, instance type, host OS settings, network, router config, load. Most of them are already in the release record and the drift report.
- **Swap until it moves:** run the replay with the first-party value of one factor at a time on AWS, or the AWS value on first-party hardware, and halve the set when several factors are swapped at once. The factor whose swap makes the bug appear or disappear is the cause or points at it.
- **Fix where it starts:** the owner of that factor fixes it: the engine team for the race, the platform team if a host setting is wrong. A platform-specific workaround is allowed only as a declared, time-boxed mitigation with the fix scheduled, and it is removed when the fix ships.
- **Every fix adds a test:** the replay that reproduced the bug joins the shared suite and runs on every platform, so the bug can't come back on either.

**The code.** Finding the factor that makes a bug appear by swapping halves of the differing factors (Python).

```python
def find_factor(base: dict, other: dict, reproduces) -> list[str]:
    """base: the config where the bug doesn't happen; other: where it does.
    reproduces(config) runs the replay and returns True if the bug appears. Returns the culprit factors."""
    diff = [k for k in base if base[k] != other.get(k)]
    def run(keys):
        return reproduces({**base, **{k: other[k] for k in keys}})
    culprits, todo = [], [diff]
    while todo:
        keys = todo.pop()
        if not run(keys):
            continue
        if len(keys) == 1:
            culprits.append(keys[0]); continue
        half = len(keys) // 2
        a, b = keys[:half], keys[half:]
        if not run(a) and not run(b):                    # needs factors from both halves together #!
            culprits.extend(keys); continue
        todo += [a, b]
    return culprits

fp = {"image": "sha256:ab12", "kernel": "6.8", "socket_rmem_max": 4_194_304, "router": "v3.4",
      "instance": "h100x8", "nccl": "2.27", "load_rps": 900}
aws = {**fp, "kernel": "6.12", "socket_rmem_max": 33_554_432, "instance": "p5en.48xlarge"}
bug = lambda c: c["socket_rmem_max"] > 16_000_000 and c["load_rps"] > 500      # stands in for the replay
print(find_factor(fp, aws, bug))
```

**Going deeper.**

- **Workarounds are counted like drift:** each declared mitigation is listed with its age and cost. The router retry in the scenario cost more each month than the engineering time to find the race.
- **Make the platforms closer, not the bug rarer:** when the investigation shows the platforms differ in a way nobody chose, such as a host setting, the difference is removed even if it wasn't the cause.

### CI/CD for the inference server and the load balancer

**Scenario.** The AWS pipeline was a sequence of manual steps: build an AWS image, deploy to a staging cluster, run a smoke test, wait for someone with access to approve, deploy Region by Region. Performance was checked by watching dashboards after the deploy. Two of the last ten releases were rolled back on AWS for latency regressions that a benchmark would have shown in an hour.

![CI/CD pipeline](diagrams/designs/concepts/ds39-c8.svg)

![CI/CD for the inference server and the load balancer](diagrams/designs/ds39-7.svg)

**What the picture shows.** One pipeline for every platform, where each stage produces evidence attached to the release, and the stages before customers run in parallel per target.

- **Build once:** the release is built and signed once; the compiled artifacts for each accelerator target are built in parallel and attached to it.
- **Fast checks first:** unit tests and CPU-only tests of the router, the config and the API contract. They run in minutes and stop most bad changes before any accelerator is used.
- **Correctness on accelerators:** numerics parity, feature conformance, cache correctness and the telemetry conformance check, on each target in parallel.
- **Performance baselines:** throughput and latency for the release's fingerprint on each target, compared with the previous release's baseline within its noise band ([Design 22](#design-serving-benchmarks)). The router is benchmarked too: added latency at p99, and routing quality (prefix cache hit rate on replayed traffic).
- **Shadow traffic:** the candidate receives a mirrored share of production traffic, and its responses are compared and discarded (next section).
- **Canary and progressive rollout:** Argo Rollouts shifts traffic in steps per Region, and each step runs an analysis against Amazon Managed Service for Prometheus, signed with SigV4 ([Argo Rollouts: Prometheus](https://argo-rollouts.readthedocs.io/en/stable/analysis/prometheus/)). A failed analysis rolls back without a person.
- **The load balancer ships the same way:** router releases go through the same stages, with their own correctness checks (routing decisions on recorded request streams) and performance baselines, and are rolled out after the servers they route to.

**The YAML.** Mirroring 2% of a model's traffic to the candidate, and the canary analysis that reads TTFT from Amazon Managed Service for Prometheus.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: model-a, namespace: serving }
spec:
  parentRefs: [{ name: inference-gateway }]
  rules:
  - backendRefs:
    - { group: inference.networking.k8s.io, kind: InferencePool, name: model-a-stable }
    filters:
    - type: RequestMirror
      requestMirror:
        backendRef: { name: model-a-shadow, port: 8000 }       # responses from the mirror are discarded
        percent: 2                                               #!
---
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata: { name: ttft-p95, namespace: serving }
spec:
  args: [{ name: release }]
  metrics:
  - name: ttft-p95
    interval: 2m
    failureLimit: 1
    successCondition: result[0] <= 0.8                           # seconds, the model's SLO
    provider:
      prometheus:
        address: https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-EXAMPLE
        query: |
          histogram_quantile(0.95, sum by (le) (rate(
            inference:ttft_seconds_bucket{release="{{args.release}}", platform="aws"}[5m])))
        authentication:
          sigv4: { region: us-east-1 }
```

**Going deeper.**

- **Percentage mirroring is standard Gateway API:** the `percent` and `fraction` fields of the RequestMirror filter are in the Standard channel since v1.3 ([Gateway API v1.3](https://kubernetes.io/blog/2025/06/02/gateway-api-v1-3/)). Whether a given gateway implements them is checked in its conformance report before relying on it.
- **Approvals are evidence, not people:** a release moves to the next stage when its evidence passes. A person approves only declared differences and exceptions.

### Shadow traffic and correctness checks

**Scenario.** A release changed the default sampler's handling of a rare stop sequence. Every synthetic test passed. In shadow traffic, 0.3% of the candidate's responses for one large customer's tool-calling prompts ended one token earlier than the stable release's, which broke their JSON parsing. The release was stopped before the canary.

![Shadow comparison](diagrams/designs/concepts/ds39-c9.svg)

![Shadow traffic and correctness checks](diagrams/designs/ds39-8.svg)

**What the picture shows.** Mirrored production requests served by the candidate and compared with the stable release's responses on the checks that can be compared.

- **What is mirrored:** a small share of real traffic, sampled across features, customers' request shapes and context lengths. The mirror runs in the same account and Region as the stable release, and its outputs are kept only as long as the comparison needs.
- **Deterministic requests are compared exactly:** greedy requests should return the same tokens. The comparison reports the share of exact matches, and the position of the first difference where they differ.
- **Sampled requests are compared by properties:** length distribution, finish reasons, structured output validity, tool-call validity, refusal rate. Sampled text can't be compared token by token.
- **Latency is compared as distributions:** TTFT and TPOT of the candidate against the stable release on the same requests, with the noise of the mirror itself measured by mirroring stable to a second stable copy.
- **Thresholds per check, and a stop:** a check beyond its threshold stops the rollout and attaches the differing requests' shapes, and for the customer's own traffic, the tokens near the first difference, to the release record for the owning team.

**The code.** Comparing shadow responses with the stable release's (Python).

```python
def compare(pairs: list[dict]) -> dict:
    """pairs: stable and shadow responses for the same request: greedy, tokens, finish, valid_json (or None)."""
    greedy = [p for p in pairs if p["greedy"]]
    exact = sum(p["stable"]["tokens"] == p["shadow"]["tokens"] for p in greedy)
    finish_diff = sum(p["stable"]["finish"] != p["shadow"]["finish"] for p in pairs)
    json_pairs = [p for p in pairs if p["stable"]["valid_json"] is not None]
    json_broken = sum(p["stable"]["valid_json"] and not p["shadow"]["valid_json"] for p in json_pairs)
    out = {"greedy_exact": round(exact / max(len(greedy), 1), 4),
           "finish_reason_diff": round(finish_diff / len(pairs), 4),
           "json_newly_invalid": round(json_broken / max(len(json_pairs), 1), 4)}
    out["stop"] = out["greedy_exact"] < 0.999 or out["json_newly_invalid"] > 0.0005           #!
    return out

def resp(tokens, finish="stop", valid_json=None):
    return {"tokens": tokens, "finish": finish, "valid_json": valid_json}

pairs = [{"greedy": True, "stable": resp([1, 2, 3], valid_json=i < 300), "shadow": resp([1, 2, 3], valid_json=i < 300)}
         for i in range(997)]
pairs += [{"greedy": True, "stable": resp([5, 6, 7, 8], valid_json=True), "shadow": resp([5, 6, 7], valid_json=False)}
          for _ in range(3)]
print(compare(pairs))
```

**Going deeper.**

- **Exact match needs determinism on both sides:** batch composition can change floating-point results. Greedy shadow comparisons run with batch-invariant kernels where the engine has them, and otherwise use a tolerance calibrated from stable-to-stable mirroring.
- **Shadow capacity is real capacity:** a 2% mirror of a large model costs accelerators. It runs on borrowed headroom (next section) and is sized to reach a statistical answer, not to run forever.

### Fast, parallel, affordable validation on the serving pool

**Scenario.** Validation needed accelerators, and on AWS the accelerators were the serving pool, sized for peak with 20% headroom. The pipeline waited for a dedicated test cluster of eight P5en instances, so releases queued behind each other. A full validation took 14 hours, mostly waiting; the merge-to-production median was 9 days.

![Validation on the serving pool](diagrams/designs/concepts/ds39-c10.svg)

![Fast, parallel, affordable validation on the serving pool](diagrams/designs/ds39-9.svg)

**What the picture shows.** Validation that borrows the serving pool's headroom when traffic allows, runs in parallel, and gives capacity back before the SLO needs it.

- **Measure cycle time by stage:** merge to production is split into queue time and run time for every stage. Most of the 9 days was queueing for the test cluster and waiting for approvals; the tests themselves took a few hours.
- **Run only what a change can affect:** a router-only change doesn't run engine numerics; a Neuron kernel change doesn't run GPU performance. The change's files map to the stages and targets it can affect, and the full suite runs nightly and before each launch.
- **Small shapes first:** correctness checks run on the smallest deployment that exercises the code path (one instance, a small model of the same architecture), and only the final performance baseline needs the production shape.
- **Borrow the serving pool's headroom:** validation runs as low-priority workloads in a Kueue queue that borrows from the serving cohort and is preempted when serving needs capacity ([Design 17](#design-gpu-scheduling)). A planner admits validation only within the headroom that the traffic forecast says won't be needed for the next hour, keeping the SLO's headroom intact.
- **Start fast:** model weights are cached on node-local NVMe, and Neuron's precompiled artifacts are part of the release, so a validation replica is ready in minutes rather than recompiling.
- **Reserve for launches:** for a new architecture's launch week, the full matrix runs on capacity bought for it as EC2 Capacity Blocks for ML, which reserve GPU and Trainium instances for a fixed window starting on a future date ([EKS: accelerated compute](https://docs.aws.amazon.com/eks/latest/userguide/ml-compute-management.html)), so launch validation never competes with serving.
- **What the scenario's pipeline became:** validation in about 3 hours, run mostly off-peak on borrowed headroom, and a merge-to-production median of 1.5 days on AWS, the same as first-party.

**The code.** How many validation replicas can borrow the serving pool in the next hour without touching the SLO headroom (Python).

```python
import math

def validation_budget(pool_replicas: int, forecast_peak_rps: float, rps_per_replica: float,
                      headroom: float = 0.20, startup_min: float = 6, window_min: float = 60) -> dict:
    """Replicas the next hour's forecast peak needs, plus the SLO headroom, are reserved for serving.
    Validation may use the rest, minus what serving could need while a preempted replica restarts."""
    needed = math.ceil(forecast_peak_rps / rps_per_replica * (1 + headroom))
    spare = max(0, pool_replicas - needed)
    restart_margin = math.ceil(spare * startup_min / window_min)                 # serving can't wait for a cold start #!
    usable = max(0, spare - restart_margin)
    return {"serving_needs": needed, "spare": spare, "validation_replicas": usable}

for hour, rps in [("03:00", 1_150), ("11:00", 4_100), ("19:00", 3_300)]:
    print(hour, validation_budget(pool_replicas=64, forecast_peak_rps=rps, rps_per_replica=80))
```

**Going deeper.**

- **Preemption must be clean:** a validation job preempted mid-run restarts its stage, not the pipeline. Stages are short enough (under 30 minutes each) that losing one costs little.
- **The headroom is a promise to customers:** validation never borrows the SLO headroom, even when a launch is late. A late launch takes Capacity Blocks or waits; it doesn't take customers' margin.

### Finding bottlenecks, cost anomalies and regressions across providers

**Scenario.** The cost per million output tokens of one model was 34% higher on AWS than on the first-party platform for similar traffic. Some of it was price. Most of it, the data later showed, was a configuration of Trainium2 decode buckets that padded short requests to a much larger batch size, plus 11% of Trainium2 replicas that sat idle overnight in a Region whose traffic had moved.

![Cross-provider analysis](diagrams/designs/concepts/ds39-c11.svg)

![Finding bottlenecks, cost anomalies and regressions across providers](diagrams/designs/ds39-10.svg)

**What the picture shows.** Telemetry and cost from both providers joined per model, release, platform and accelerator, with detectors for the three kinds of problem.

- **Cost per unit of work on AWS:** split cost allocation data in CUR 2.0 attributes EC2 instance cost to EKS pods, including GPU, Trainium and Inferentia resources ([split cost allocation for ML on EKS](https://aws.amazon.com/blogs/aws-cloud-financial-management/improve-cost-visibility-of-machine-learning-workloads-on-amazon-eks-with-aws-split-cost-allocation-data/)), and imports Kubernetes labels as cost allocation tags once activated ([Kubernetes labels for cost allocation](https://docs.aws.amazon.com/cur/latest/userguide/split-cost-allocation-data-kubernetes-labels.html)). With the model, release and accelerator as pod labels, cost per million output tokens comes from one join with the token counters. The first-party platform's cost comes from [Design 25](#design-cost-capacity) in the same shape.
- **Bottlenecks:** per accelerator target, where the time goes: queueing, prefill, decode, padding (for static-shape targets), and router overhead, from the shared telemetry. A target whose padding share grows is a bucket problem; one whose queueing grows at moderate load is a scheduling or batch problem.
- **Cost anomalies:** an hourly detector on cost per million tokens per model and platform, against its own weekly pattern. AWS Cost Anomaly Detection runs as a backstop on the whole account; it reads Cost Explorer data, which can take up to 24 hours to arrive ([Cost Anomaly Detection](https://docs.aws.amazon.com/cost-management/latest/userguide/manage-ad.html)), so it can't be the first alarm.
- **Regressions in production:** each release's production efficiency and latency, per platform, compared with the previous release on the same traffic mix. A regression found in production also adds the traffic shape that revealed it to the CI replay set, which is how the pipeline's tests follow real workloads.
- **Remediation goes to the source:** findings are assigned to the owner of the cause, as in the bug method above. The bucket configuration in the scenario was fixed in the shared bucket selection, from traffic shapes, for every Trainium2 deployment.

**The code.** Cost per million output tokens by model, release and accelerator, from CUR 2.0 split line items joined with the token counters (Athena SQL).

```sql
-- cur: the CUR 2.0 export with split cost allocation data; the tag keys below are how the activated
-- Kubernetes labels appear in this export's resource_tags map (check your own export for the exact keys).
-- tokens_hourly: hourly output tokens per pod label set, exported from the telemetry store.
WITH pod_cost AS (
  SELECT date_trunc('hour', line_item_usage_start_date)            AS hour,
         resource_tags['user_model']                               AS model,
         resource_tags['user_release']                             AS release,
         resource_tags['user_accelerator']                         AS accelerator,
         SUM(split_line_item_split_cost + split_line_item_unused_cost) AS usd     -- idle share counted #!
  FROM cur
  WHERE line_item_usage_start_date >= date '2026-11-01'
    AND split_line_item_parent_resource_id IS NOT NULL
    AND resource_tags['aws_eks_namespace'] = 'serving'
  GROUP BY 1, 2, 3, 4)
SELECT c.model, c.release, c.accelerator,
       SUM(c.usd)                                       AS usd,
       SUM(t.output_tokens) / 1e6                       AS m_tokens,
       SUM(c.usd) / NULLIF(SUM(t.output_tokens) / 1e6, 0) AS usd_per_m_tokens
FROM pod_cost c
JOIN tokens_hourly t
  ON t.hour = c.hour AND t.model = c.model AND t.release = c.release AND t.accelerator = c.accelerator
GROUP BY 1, 2, 3
ORDER BY usd_per_m_tokens DESC;
```

**Going deeper.**

- **Counting unused cost on purpose:** a replica that holds an instance's accelerators and serves nothing still costs money. Adding the unused share to the serving cost makes idle replicas show up in cost per token, instead of vanishing into an unattributed line.
- **Compare platforms on the same traffic:** cost per token differs with the request mix. Cross-platform comparisons use replayed traffic of the same shape, or production windows where the mix is within a set distance, before anyone concludes that one platform is more expensive.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds39-c12.svg)

- **Phase 1, one release and the overlay (weeks 1–6):** the release record, ECR replication by digest, the overlay allow-list with the build check, managed Argo CD on the AWS serving clusters, and the effective-config endpoint with the first drift report.
- **Phase 2, the parity suite (weeks 4–12):** numerics parity per target, feature conformance for structured sampling and prompt caching, the telemetry schema and its conformance check, and the parity matrix in the release record.
- **Phase 3, the pipeline (weeks 8–16):** parallel stages per target, performance baselines for the server and the router, percentage mirroring with shadow comparison, and Argo Rollouts analysis against Amazon Managed Service for Prometheus.
- **Phase 4, validation on the serving pool (weeks 12–20):** change-based stage selection, the low-priority validation queue with the headroom planner, cached weights and precompiled Neuron artifacts, and Capacity Blocks for launch weeks.
- **Phase 5, cross-provider analysis (weeks 14–24):** split cost allocation with activated labels, cost per token per platform, the hourly anomaly detector, bottleneck breakdowns per target, and production regressions feeding the CI replay set. The next new architecture is brought up on both platforms on one calendar.

**Trade-offs we accepted.**

- **Less freedom for the platform team:** the AWS deployment can't change engine behavior on its own, even in an incident. Emergency changes go into the shared config, for every platform, and are reviewed afterwards.
- **Launches wait for the slowest target, or declare it:** lockstep means a launch either waits for every target or ships with a declared difference. Shipping first-party first is a decision recorded with its reason, not a default.
- **Validation on serving capacity adds risk:** borrowing the serving pool needs a correct forecast and clean preemption. The headroom planner is conservative, and launches use reserved capacity instead.
- **Shadow traffic costs accelerators and needs care with data:** mirrored requests are real customer data. They stay in the same account and Region, outputs are kept only for the comparison, and the mirror share is the smallest that gives an answer.
- **Fixing at the source is slower than a workaround the first time:** a time-boxed mitigation is allowed, but every one has an owner and an expiry. The rule pays back on the second and third platform.

**Open questions.**

- **More than one cloud platform:** each additional platform adds targets to every matrix. At what point should the suite run a sample of targets per change, and the full matrix only nightly?
- **Static-shape accelerators and new features:** features designed on GPUs keep arriving with dynamic shapes. Should the core inference team's design review require a static-shape plan for every feature, or should Trainium2 support follow by a release?
- **The cloud platform's own layers:** the provider's managed layers in front of the deployment can change behavior too. How much of their behavior should the parity suite test from the outside, and how are differences there owned?
- **Shared capacity across platforms:** if both platforms run on AWS capacity, should validation for one borrow the other's headroom, and who decides when they compete?

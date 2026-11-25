---
id: appendix-design-10-eks
title: "Appendix K: Design 10 built and tested on Amazon EKS"
short: "K. Design 10 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 10: Verification and reward quality](#design-verification). I built it with Terraform on a standard Amazon EKS cluster in one AWS account: verifiers with one interface, written contracts and abstention; one Deployment per pinned verifier version; code verification inside gVisor; two model-based judges of different families behind rule-based guards; gold sets, per-slice precision and recall with intervals; differential fuzzing against strict reference verifiers and a regression suite; a registry and a training gate; short GRPO runs with a reward-quality monitor that compares every step's reward with an independent audit verifier; re-scoring when a verifier changes; and a human review queue.

The order is the one a reader needs: first the context, then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them. Section 6 says plainly what I did not build.

**What "scaled down" means here.** The design is about verifiers that score millions of rollouts for large RL runs on math, code and open-ended tasks. This build uses 400 templated math word problems for training, 120 held-out ones and their 120 isomorphic rewrites, 8 small Python functions with hidden tests, and 10 open questions with reference answers. Its gold sets hold 1,015 math, 152 code and 100 judge completions whose correctness is known by construction. The RL runs train Qwen2.5-0.5B-Instruct for 80 GRPO steps on one GPU. None of the mechanisms depends on the size. A larger run pins a verifier through the same registry, reads its reward from the same Deployment, and is watched by the same monitor.

{{RESULT_SUMMARY}}

### Context: the problem, the actors and who talks to whom

**Scenario.** The design's opening case: an RL run reports large gains on training reward, and an external evaluation shows almost none. The math verifier accepted answers that listed several candidates; the code verifier counted a program as correct if its printed output matched even when it crashed; the benchmark shared a verifier with training. This build reproduces each of those verifiers as the old version 1.0.0, builds the strict version 2.0.0 next to it, and measures the difference, including what an RL run does to each.

Six terms are used throughout.

- **Verifier.** A versioned implementation of one contract, `name@version`, that returns a verdict (pass, fail or abstain), a score, machine-readable reasons, and its own id. Abstain means "no reward": the sample is left out of the loss.
- **Contract.** The written definition of correct for a verifier: answer format, accepted equivalences, what is forbidden, and when to abstain. It is served by the verifier itself and stored in its registry entry.
- **Gold set.** Completions whose correctness is known by construction, each tagged with a slice: canonical, adversarial, equivalent form, cosmetic variant, contract violation and others.
- **Audit verifier.** An independent implementation of a stricter contract (`strict-reference-math@1.0.0`, `strict-reference-code@1.0.0`), used for differential fuzzing and by the monitor, and never pinned as a training reward.
- **Registry entry.** A verifier's contract, code hash, purpose, owner, the gate's checks with their evidence, and a status: approved, rejected, audit only or evaluation only. A training run may pin only an approved training verifier whose running service has the approved code hash.
- **Hacking gap.** Per RL step, the mean training reward minus the mean audit reward on the same rollouts. The monitor alerts on how far it grows above its level over the first three steps.

![Who uses the platform, what it is made of, and what it talks to](diagrams/appendix/a11-1.svg)

**What the picture shows.** The rewards team measures, fuzzes and registers verifiers with the launcher `vq.py`. A training run pins a registered verifier and gets its reward from that verifier's Deployment, while the monitor scores the same rollouts with the audit verifier. The judge verifiers call two judge models. The evaluation team owns its own verifier. Everything is written through one store; only the operator writes the registry. Outside the platform there are only the models, the images and two Python wheels, each pinned.

### 1. The software components I implemented

**Scenario.** A reader needs to know which parts I wrote and which I reused, before reading any result.

| # | Component | What it does | Where it runs | Written or reused |
|---|---|---|---|---|
| 1 | **Standard EKS cluster** | Kubernetes 1.37 with three managed node groups (system, GPU, gVisor sandbox) and the EKS add-ons, the VPC CNI with network policy enforcement on. | AWS | Written (Terraform) |
| 2 | **gVisor 20261116.0** | A user-space kernel for the pods that run model-written programs: installed by the sandbox nodes' launch template, checked against a SHA-512 in Terraform, exposed as the `gvisor` RuntimeClass. | Sandbox node | Reused, configured |
| 3 | **NVIDIA device plugin 0.20.2** | Advertises `nvidia.com/gpu` on the GPU nodes. | `nvidia-device-plugin` | Reused |
| 4 | **Verifier interface (`vbase.py`)** | `Verdict`, `Result`, `verify()`, `reward()` that turns ABSTAIN into `None`, the code hash of each version. | Library | Written |
| 5 | **Math verifiers (`vmath.py`)** | `math-final-answer@1.0.0` (any matching box, or the last number), `@2.0.0` (exactly one box, no hedge, a narrow language, SymPy equality, abstain otherwise), the audit `strict-reference-math@1.0.0` (exact fractions, the box ends the answer) and the evaluation team's `eval-math@1.0.0`. | `rewards` | Written |
| 6 | **Code verifiers (`vcode.py`)** | `code-tests@1.0.0` (printed output for the visible examples), `@2.0.0` (hidden tests and edge cases, a new limited process per test, exit code, an audit hook, a static scan, property runs against the reference), the audit `strict-reference-code@1.0.0`, and mutation testing of each task's tests. | `rewards`, gVisor for 2.0.0 and the reference | Written |
| 7 | **Judge verifiers (`vjudge.py`)** | `judge-single@1.0.0` (one judge, a bare 0–10 score) and `judge-pair@2.0.0` (rule guards, two judges with the reference and a yes/no rubric, abstain on disagreement). | `rewards` | Written |
| 8 | **Judge models** | vLLM 0.33.0 serving Qwen2.5-7B-Instruct and Mistral-7B-Instruct-v0.3, each pinned to a commit, one GPU each. | `judges` | Reused, configured |
| 9 | **Verifier service (`vserve.py`)** | One HTTP server per Deployment: `/verify`, `/info` (contract, purpose, owner, code hash, the kernel it sees), `/stats`. A verifier exception becomes an abstention, never a reward. | `rewards` | Written |
| 10 | **Quality and the gate (`quality.py`)** | Precision, recall and abstention per slice with 95% Wilson intervals; the gate, which uses the lower bound of precision. | Jobs, launcher | Written |
| 11 | **Fuzzer (`fuzz.py`)** | Math and code mutations, differential testing against the reference, a regression suite from confirmed false positives. | Jobs | Written |
| 12 | **Platform jobs (`tools.py`)** | quality, policy-output quality, fuzz, regress, test strength, re-score, review queue; a nightly fuzz CronJob. | `rewards` | Written |
| 13 | **RL run with the monitor (`rl.py`)** | TRL 1.18.2 `GRPOTrainer` with colocated vLLM; the reward from the pinned verifier, the audit scores, the gap and its growth, reasons, shortcuts, the alert and the stop; evaluation before and after. | `training`, one GPU | Written on TRL 1.18.2 |
| 14 | **Store (`rstore.py`)** | The only bucket writer in the cluster; refuses writes to `registry/` and `data/`. | `platform` | Written |
| 15 | **Launcher (`vq.py`)** | Runs jobs, applies the gate and writes the registry, pins verifiers for a run (exit 3 when refused), starts the nightly fuzz on demand. | The operator's machine | Written |
| 16 | **Datasets (`make_data.py`)** | All tasks and gold sets from templates and a fixed seed, with content hashes; `selftest.py` checks the rule-based verifiers locally. | `datasets/` | Written |
| 17 | **Scenario runner and screenshot renderer** | Runs the scenarios, records commands and output, redacts private values, draws terminal-style screenshots. | `tests/` | Adapted from Appendix I |

{{CODE_SIZE}}

**Going deeper.**

- **The old verifiers are real code, not mock-ups.** Each 1.0.0 version is the design's "40-line function written in an afternoon" for its kind, deployed the same way as the strict one. Every measured difference between the two versions is a difference between two running services.
- **Images, unmodified.** Verifiers, the store and the jobs run the Python 3.12 slim image pinned by digest, with the code in a ConfigMap whose name carries the code's hash. The strict math verifier installs SymPy 1.14.0 and mpmath 1.3.0 from the store with `--require-hashes`, because the verifier pods have no internet. The judges and the RL run use the vLLM 0.33.0 image pinned by digest; the RL run adds TRL 1.18.2 with the image's own packages held fixed.
- **Code hashes identify versions.** A version's hash covers its class, its name@version and its source files, so the nine services report nine different hashes, and a registry entry can be checked against the running service.

### 2. EKS topology and instances

**Scenario.** The design needs CPU verifiers in quantity, code verification in a sandbox with no network, GPUs for the judges and for short red-team RL runs, and a registry the training side cannot write to.

![One VPC in us-east-2, three zones, a system, a GPU and a gVisor sandbox node group](diagrams/appendix/a11-2.svg)

**What the picture shows.** One VPC in us-east-2 with a public and a private subnet in each of three zones, one NAT gateway, an S3 gateway endpoint, one EKS cluster with three managed node groups spread over all three zones, the bucket, and the network policies.

**Why standard EKS with managed node groups, and not Auto Mode.** I chose the substrate for what this design needs.

- **gVisor needs a containerd entry.** The sandbox node group's launch template adds the `runsc` runtime to containerd and installs the gVisor release by checksum. EKS Auto Mode does not let you add a runtime shim (Appendix B records this), so code verification under gVisor needs nodes whose configuration you own.
- **A fixed GPU pool with a pinned driver.** The judges are part of a verifier's definition; their numerics depend on the GPU stack. A managed node group's AMI release is a Terraform variable (`1.37.1-20261119`), so it changes only when that line changes. Auto Mode replaces nodes after at most 21 days with the latest AMI ([Security considerations for EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/auto-security.html)).
- **Ordinary EC2 instances.** The nodes are listed by EC2, so cost and teardown can be checked from the account (S01, section 7).

| Item | What I built | Why |
|---|---|---|
| **Region** | us-east-2. One VPC of five allowed per region; the NAT gateway uses one of five Elastic IPs. | Design 09's build uses us-east-1 in the same account. |
| **Cluster** | EKS `ds10-verify`, Kubernetes 1.37, `bootstrap_self_managed_addons = false`, access entries. The public endpoint accepts only the administrator's addresses. | The network I built from sends each connection out through one of a small pool of addresses, so `up.sh` samples the pool and allows each address as a `/32`, and every `kubectl` call is retried when the endpoint cannot be reached. |
| **Add-ons** | VPC CNI with `enableNetworkPolicy`, kube-proxy, Pod Identity agent, CoreDNS, metrics-server. | The verifiers' "no internet" relies on NetworkPolicy. |
| **System node group** | 2 × `m6i.2xlarge` (8 vCPU, 32 GiB), on demand. | The store, the rule-based verifiers (two replicas each) and the jobs. |
| **GPU node group** | 3 nodes, one GPU each, on demand, 200 GB disk, AL2023 NVIDIA AMI `1.37.1-20261119`, GPU taint. Types in priority order: `g6e.xlarge`, `g6.2xlarge`, `g6e.2xlarge`, `g5.xlarge`, `g5.2xlarge`, `g6.xlarge`. | Two judges and one RL run. EC2 refused some of these types for capacity in earlier builds, so the list is long; S01 records what the final run got. |
| **Sandbox node group** | 1 × `m6i.xlarge`, tainted `ds10/sandbox=gvisor`, launch template with gVisor `20261116.0` and the `runsc` containerd runtime, instance metadata limited to one hop. | Pods that name the `gvisor` RuntimeClass land here and run under gVisor. |
| **Storage** | One private S3 bucket (`ds10-verify-data-<random>`). | Datasets, wheels, registry, runs, reports. |
| **Identity** | One Pod Identity role, for the store: read the bucket, write `runs/`, `fuzz/`, `reviews/`, `rescore/`, `evals/`. Verifier pods have no role and no service-account token. | Code a reward runs has nothing to misuse, and nothing in the cluster can write the registry. |
| **Namespaces** | `rewards` (restricted Pod Security: verifiers and jobs), `judges`, `training`, `platform`. | One place for each kind of thing. |

### 3. Architecture

**Scenario.** One map of what runs where and which layer is responsible for what, so that each scenario can say which layer produced the behaviour it shows.

![Design 10 on EKS: the five layers](diagrams/appendix/a11-3.svg)

**What the picture shows.** The operator's tools at the top, the verifier implementations, how they are served, the quality machinery, and the stores. A verifier is a library class, a served version, and a registry entry; the three are tied together by the code hash.

![A verifier's life: contract to re-score](diagrams/appendix/a11-4.svg)

**What the picture shows.** The design's lifecycle as this build implements it, with the scenario that exercises each step. A confirmed exploit becomes a regression case that every later version must reject.

![What a code reward is allowed to do: compute, and nothing else](diagrams/appendix/a11-5.svg)

**What the picture shows.** The layers around a candidate program: a restricted pod with no network and no identity, gVisor, a new limited process per test with an audit hook, and a result channel behind a nonce. The dashed box says what is weaker than the design's ideal.

![The reward-quality monitor inside one RL step](diagrams/appendix/a11-6.svg)

**What the picture shows.** Each step's 64 completions go to the pinned training verifier for the reward and to the audit verifier for the check. The monitor logs the gap, its growth over the first three steps, the false positives against the audit, reason rates, shortcut detectors and length, and pauses promotion or stops the run on the thresholds.

![Guarded judging: rules first, two judges, abstain on disagreement](diagrams/appendix/a11-7.svg)

**What the picture shows.** The guarded judge: rules decide what rules can decide, two judges of different families answer the same yes/no rubric with the reference answer, and a disagreement goes to review instead of becoming a reward.

### 4. How I implemented it

**Scenario.** Someone who wants to rebuild or extend this needs to know how the pieces fit, in the order they are built.

1. **Terraform, cluster layer (`terraform/10-cluster`).** The VPC, subnets, NAT gateway and S3 endpoint; the cluster and its roles; the add-ons nodes need before they become Ready; the system and GPU node groups; the sandbox node group with its launch template; then CoreDNS and metrics-server. The provider has `allowed_account_ids`; the account ID and the administrator's addresses live only in a git-ignored `local.auto.tfvars` that `up.sh` writes. State is local and git-ignored.
2. **Datasets and wheels.** `up.sh` runs `datasets/make_data.py`, which writes eight files and an index with each file's sha256, then `datasets/selftest.py`, which checks the rule-based verifiers on the gold sets locally and fails if a strict or reference verifier accepts anything wrong. It downloads the two pinned wheels.
3. **Terraform, platform layer (`terraform/20-platform`).** Namespaces with their Pod Security levels; the device plugin; the `gvisor` RuntimeClass; the bucket with the datasets and the wheels (each checked against its pinned sha256); the store and its identity; the code ConfigMap and the task ConfigMap; nine verifier Deployments and Services, two judge Deployments, the nightly fuzz CronJob; the network policies.
4. **Measuring a verifier.** `vq.py job q-<v> -- quality --verifier V --gold FILE` runs a Job in `rewards` that sends every gold completion to the verifier's Service and writes per-slice precision, recall and abstention with Wilson intervals, the verdicts per kind, the reasons, and the misjudged examples.
5. **Fuzzing and regressions.** The fuzz job builds mutations of each task's right and wrong answers (several answers, the right one first, hedges, number soup, a trailing number, boxes in the steps, and equivalent forms that must pass) and of each code task (print then crash, exit early, hard-code the visible examples, patch the test framework, print the expected values before a wrong program, and an alternative correct solution). It scores each with the verifier under test and the reference, and writes every disagreement. Confirmed false positives become `regressions.jsonl`; the regress job checks that a verifier rejects every one.
6. **The gate and the registry.** `vq.py register` reads the service's `/info` (inside its pod, because the rewards NetworkPolicy keeps the API server's proxy out), reads the stored evidence, checks it was measured on the same code hash, applies the gate (precision lower bound at least 0.95 on the gold set and, once measured, on policy outputs; abstention at most 0.10; fuzz false-positive rate at most 0.005; every regression case rejected) and writes `registry/<name>/<version>.json`.
7. **Pinning and the RL run.** `vq.py rl` refuses with exit 3 unless the training verifier is approved for training, the audit and evaluation verifiers are registered for their purposes, and each running service's code hash equals its registry entry; `--red-team` allows a rejected training verifier on purpose. It writes `runs/<run>/resolved.json` and creates a GPU Job. The job checks the pins again against `/info`, evaluates the base policy on the held-out set and the rewrites with the evaluation verifier, trains with GRPO (8 prompts × 8 samples per step, DAPO loss, no KL, 320 tokens, colocated vLLM), and at every step calls the training verifier for the reward and the audit verifier for the monitor, stores every rollout with both verdicts, and logs the monitor row. It evaluates again at the end.
8. **Re-scoring and review.** The rescore job sends every stored rollout of a run to another verifier version and counts the verdicts that changed. The review job builds a queue within a budget: train-pass/audit-fail first, then abstentions, then a random 5% of confident passes.
9. **Scenarios and evidence.** `tests/run_scenarios.py` runs every command it shows, records the real output, redacts the account ID, the administrator's addresses and the local user name, computes each check from the output, and draws the screenshot from the same text.

### 5. The scenarios I tested

**Scenario.** Each test below starts from a question the design asks and ends with evidence. Every command was run by the scenario runner against the live platform, in one run from a fresh build, in the order shown, and the output is what the command printed (shortened here; the screenshot and the evidence file have it in full). Each check is computed by the program from that output, not written by hand. The RL runs sample at temperature 1.0, so their exact numbers differ from run to run; the checks test the direction of each effect, not a value.

**How to read the screenshots.** They are drawn from the recorded text by the renderer in `tests/render_terminal.py`. Lines starting with `$` are the commands; lines starting with `[PASS]` or `[FAIL]` are the checks. Where a step reads the store rather than running one command, the step's title says which object it read.


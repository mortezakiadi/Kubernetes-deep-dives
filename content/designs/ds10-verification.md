---
id: design-verification
title: "Design 10: Verification and reward quality"
short: "Verification and reward quality"
---
This design answers one question: *how do we build verification pipelines that we can trust as RL rewards, for tasks where correctness can be checked automatically or semi-automatically, and how do we detect and reduce reward hacking, false positives, brittle verifiers and the other failure modes that make RL look better than it really is?* Rewards were introduced in [Design 06](#design-post-training), long-horizon verification in [Design 08](#design-long-horizon), and sandboxed execution in [Design 01](#design-sandboxing). This design treats the verifier itself as the product.

The short version: *a verifier is production software that an optimizer is actively attacking*. Every verifier has a written contract, a versioned implementation, a labeled test set, a fuzzing suite and measured precision and recall per task slice, and it may **abstain** when unsure. During training, a **reward-quality monitor** compares the training reward with independent checks (a stricter verifier, perturbed versions of the task, sampled human review) and treats any widening gap as reward hacking until proven otherwise. A verifier change re-scores the data it affected.

### The use case and requirements

**Scenario.** The team's math-and-code RL run reports +11 points on training reward and +9 on the internal benchmark. An external evaluation shows +2. Investigation finds three problems: the math verifier accepts answers that list several candidates, the code verifier counts a program as correct if its printed output matches even when it crashes afterwards, and the benchmark shares a verifier with training. RL learned all three.

![Reported gains vs real gains](diagrams/designs/concepts/ds10-c1.svg)

**What the platform must do.**

- **Make verifiers trustworthy:** precise contracts, measured error rates, and evidence before a verifier is used for training.
- **Prefer precision:** a verifier that sometimes rejects correct answers slows learning; one that accepts wrong answers teaches the wrong behavior.
- **Find weaknesses before the model does:** fuzzing, differential testing and adversarial cases on every verifier change.
- **Detect hacking during training:** independent signals that diverge from the training reward when the policy exploits the verifier.
- **Use people where they matter:** route uncertain and disputed cases to human review, within a budget.
- **Keep evaluation honest:** evaluation verifiers that are independent of training verifiers.

**Out of scope:** the RL objective (Design 06), sandbox isolation itself (Design 01).

### How RL looks better than it is

**Scenario.** Each failure was found by accident. The team wants a checklist of the ways a reward can lie, so each one has an owner and a test.

![Seven failure modes](diagrams/designs/concepts/ds10-c2.svg)

![How RL looks better than it is](diagrams/designs/ds10-1.svg)

**What the picture shows.** The failure modes, grouped by where they come from.

- **False positives:** the verifier accepts a wrong answer. The most dangerous kind, because RL actively seeks them out and reinforces them.
- **False negatives:** the verifier rejects a correct answer (a different but equivalent form, unexpected formatting). Slows learning and pushes the model toward the verifier's preferred format.
- **Brittle verifiers:** results change with harmless variations (whitespace, units, `0.5` vs `1/2`, key order in JSON). Both false positives and negatives hide here.
- **Reward shortcuts:** the model satisfies what the verifier checks without doing what the task intends. Recent work found RLVR-trained models abandoning rule induction because the verifier only checked whether the outputs were consistent with the examples.
- **Judge exploits:** model-based judges reward surface patterns; one study found that openers like "Let's solve this step by step," or even a lone colon, triggered false positive rewards from several LLM judges.
- **Environment exploits:** editing tests, reading answers from the environment, timing out the checker, or causing a crash that the harness misreads as success.
- **Measurement leaks:** evaluation sets contaminated by training data, or evaluated with the same verifier used for training, so exploits show up as gains.

**Going deeper.**

- **The optimizer finds what you didn't test:** a verifier that's 99% precise on natural answers can be far less precise on answers produced by a policy trained against it. Measure precision on the policy's own outputs, not just on a static set.
- **Hacking grows with optimization:** the 2026 study found hacking increased with task complexity and compute. A verifier that's good enough for a small run may not be good enough for a large one.

### A verifier is a product: contract, interface and lifecycle

**Scenario.** The math verifier is a 40-line function written in an afternoon. Three teams copied it with small changes. Nobody knows which copy scored which run.

![Anatomy of a verifier](diagrams/designs/concepts/ds10-c3.svg)

![A verifier is a product: contract, interface and lifecycle](diagrams/designs/ds10-2.svg)

**What the picture shows.** The lifecycle every verifier goes through before and after it's used for training.

- **Contract:** a written definition of what counts as correct, including the answer format, accepted equivalences, what's forbidden (multiple answers, hedging) and when to abstain.
- **Implementation:** a versioned package with one interface: verdict (pass, fail, abstain), score, reasons and the verifier version.
- **Test set:** labeled examples, including tricky cases and past exploits, run in CI on every change.
- **Fuzzing and differential tests:** adversarial completions generated automatically, compared against a stricter reference verifier.
- **Calibration:** precision and recall measured per task slice on gold labels and on the current policy's outputs.
- **Deployment and monitoring:** served with a version; every reward is tagged with that version; quality is monitored during training.
- **Change and re-score:** a fix creates a new version, and data scored by the old one is re-scored.

**The code.** The interface every verifier implements (Python).

```python
from dataclasses import dataclass, field
from enum import Enum

class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ABSTAIN = "abstain"          # unsure: no reward, routed to review #!

@dataclass
class Result:
    verdict: Verdict
    score: float                 # 0..1; 1.0 only for PASS
    reasons: list[str] = field(default_factory=list)   # machine-readable, e.g. "multiple_final_answers"
    verifier: str = ""           # e.g. "math-final-answer@3.2.0"

class Verifier:
    name: str
    version: str

    def verify(self, task: dict, completion: str) -> Result:
        raise NotImplementedError

    def reward(self, task: dict, completion: str) -> float | None:
        r = self.verify(task, completion)
        return None if r.verdict is Verdict.ABSTAIN else r.score   # None = excluded from the loss #!
```

**Going deeper.**

- **Abstention is a feature:** a verifier allowed to say "I don't know" can be strict without being wrong. Abstained samples are excluded from the loss (TRL reward functions, for example, accept `None`) and counted, so a rising abstention rate is visible.
- **Reasons are data:** aggregated reasons ("multiple_final_answers" rising from 0.1% to 6%) are often the first sign of a policy probing the verifier.

### Measuring verifier quality

**Scenario.** Asked "how good is our code verifier?", the team answers "it uses the official tests." That doesn't say how often it's wrong, or on what.

![Precision first](diagrams/designs/concepts/ds10-c4.svg)

![Measuring verifier quality](diagrams/designs/ds10-3.svg)

**What the picture shows.** How verifier quality is measured, and why precision gets the most attention.

- **Gold labels:** a sample of completions labeled by people or by a much stricter, slower reference check, refreshed regularly from the current policy's outputs.
- **Confusion matrix per slice:** precision (of what the verifier accepts, how much is correct) and recall (of what's correct, how much it accepts), by task type, difficulty and answer format.
- **Precision first:** a 2026 study of noisy rewards found that RL performance tracked the verifier's precision far more than its recall, and that moderate noise could even help exploration while false positives enabled hacking.
- **Two populations:** measure on a static gold set (regressions in the verifier) and on fresh policy outputs (exploits the policy has found). The second number is the one that drifts.
- **Abstention rate and coverage:** a verifier that abstains on 30% of a slice gives that slice little signal; track it per slice.

**The code.** Computing quality per slice from paired verifier and gold decisions (Python).

```python
from collections import defaultdict

def verifier_quality(rows):
    """rows: dicts with slice, verdict ('pass'|'fail'|'abstain'), gold (True/False)."""
    by = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "abstain": 0})
    for r in rows:
        c = by[r["slice"]]
        if r["verdict"] == "abstain":
            c["abstain"] += 1
        elif r["verdict"] == "pass":
            c["tp" if r["gold"] else "fp"] += 1
        else:
            c["fn" if r["gold"] else "tn"] += 1
    out = {}
    for s, c in by.items():
        decided = c["tp"] + c["fp"] + c["fn"] + c["tn"]
        out[s] = {
            "precision": c["tp"] / max(c["tp"] + c["fp"], 1),      # the number that matters most #!
            "recall": c["tp"] / max(c["tp"] + c["fn"], 1),
            "abstain_rate": c["abstain"] / max(decided + c["abstain"], 1),
            "n": decided + c["abstain"],
        }
    return out
```

**Going deeper.**

- **Gate training on precision:** a verifier may be used for training on a slice only above a precision threshold (for example, 98% on fresh policy outputs); below it, the slice abstains or goes to review.
- **Report intervals, not points:** with a few hundred labels per slice, a 99% precision estimate has a wide interval. Size the gold sample for the precision you need to prove.

### Hardening: fuzzing and differential testing

**Scenario.** After a hack is found, the fix takes a day. But the policy found the hack in a few hundred steps; the team wants to find such holes before training does.

![Differential fuzzing](diagrams/designs/concepts/ds10-c5.svg)

![Hardening: fuzzing and differential testing](diagrams/designs/ds10-4.svg)

**What the picture shows.** Generating adversarial completions and comparing verifiers to find false positives automatically.

- **Mutation-based fuzzing:** take known wrong and right answers and apply transformations: list several answers, put the right answer first and a wrong one last, hedge ("either 3 or 4"), add the expected output in a comment, wrap answers in odd formatting, use equivalent forms.
- **Model-based adversaries:** prompt a model to produce answers that would pass the verifier without solving the task; or run a short RL run against the verifier on purpose (a "red-team run") and inspect what gets rewarded.
- **Differential testing:** run the production verifier and a stricter reference on the same cases; every disagreement is either a false positive, a false negative or a contract ambiguity, and each is triaged.
- **Regression suite:** every confirmed exploit becomes a permanent test case.
- **Published evidence:** a 2026 verifier-fuzzing study showed that realistic bugs (accepting the first number in a response, ignoring duplicate JSON keys, treating printed text as program correctness) create high-reward false positives, and stricter variants removed the measured false positives on the same generated cases.

**The code.** A small fuzzing harness that applies mutations and reports disagreements with a strict reference (Python).

```python
import random

MUTATIONS = {
    "multi_answer":   lambda wrong, right: f"The answer is \\boxed{{{wrong}}} or \\boxed{{{right}}}",
    "right_first":    lambda wrong, right: f"\\boxed{{{right}}} ... actually \\boxed{{{wrong}}}",
    "hedge":          lambda wrong, right: f"It is either {right} or {wrong}.",
    "number_soup":    lambda wrong, right: " ".join(str(x) for x in range(-5, 50)),
    "equiv_form":     lambda wrong, right: f"\\boxed{{{to_equivalent_form(right)}}}",   # must PASS
}

def fuzz(verifier, reference, tasks, n_per_task=5, seed=0):
    rng, findings = random.Random(seed), []
    for task in tasks:
        right, wrong = task["solution"], task["distractor"]
        for name in rng.sample(list(MUTATIONS), k=min(n_per_task, len(MUTATIONS))):
            completion = MUTATIONS[name](wrong, right)
            a = verifier.verify(task, completion).verdict
            b = reference.verify(task, completion).verdict
            if a != b:
                findings.append({"task": task["id"], "mutation": name, "prod": a, "ref": b,
                                 "kind": "false_positive" if a == "pass" else "false_negative"})  #!
    return findings
```

**Going deeper.**

- **Fuzz on every verifier change in CI,** and nightly against the current policy's outputs, since new policy behavior opens new attack surfaces.
- **A red-team run is cheap insurance:** a few hundred RL steps on a small model against a new verifier often surfaces its worst holes before a large run does.

### Answer verification: strict contracts for math and short answers

**Scenario.** The math verifier extracts "the last number in the response." The policy learns to end every answer with a short list of plausible values.

![Strict answer contract](diagrams/designs/concepts/ds10-c6.svg)

![Answer verification: strict contracts for math and short answers](diagrams/designs/ds10-5.svg)

**What the picture shows.** A strict final-answer contract and the steps that enforce it.

- **One designated final answer:** exactly one marked final answer (for example, one `\boxed{}` after the reasoning). Zero or several means FAIL, not "take the last one."
- **Parse into a narrow language:** numbers, fractions, intervals, simple expressions; anything outside the accepted language abstains instead of guessing.
- **Symbolic equivalence:** compare with a computer algebra system (SymPy), not string matching, with explicit rules for units, rounding and ordering.
- **Hedging detection:** reject answers that say "or" around the final answer, or that give a range when a value was asked for.
- **Abstain on ambiguity:** when the reference answer itself has several valid forms that the parser can't settle, abstain and route to review.

**The code.** A strict final-answer verifier (Python, SymPy).

```python
import re
import sympy as sp
from sympy.parsing.latex import parse_latex

BOX = re.compile(r"\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}")

class StrictFinalAnswer(Verifier):
    name, version = "math-final-answer", "3.2.0"

    def verify(self, task, completion):
        boxes = BOX.findall(completion)
        if len(boxes) != 1:                                             # exactly one final answer #!
            return Result(Verdict.FAIL, 0.0, [f"final_answers={len(boxes)}"], self.id)
        if re.search(r"\b(or|either)\b", completion[completion.rfind("\\boxed"):][:80]):
            return Result(Verdict.FAIL, 0.0, ["hedged_final_answer"], self.id)
        try:
            got, want = parse_latex(boxes[0]), parse_latex(task["solution"])
        except Exception:
            return Result(Verdict.ABSTAIN, 0.0, ["unparseable"], self.id)   # don't guess #!
        try:
            ok = sp.simplify(got - want) == 0
        except Exception:
            return Result(Verdict.ABSTAIN, 0.0, ["not_comparable"], self.id)
        return Result(Verdict.PASS if ok else Verdict.FAIL, float(ok), [], self.id)

    @property
    def id(self):
        return f"{self.name}@{self.version}"
```

**Going deeper.**

- **The prompt must state the contract:** tell the model exactly how to give its final answer. A strict verifier with an unstated contract produces false negatives that look like capability problems.
- **Track the failure reasons per step:** a policy that starts hitting "final_answers=2" more often is probing the contract.

### Code verification: tests that are hard to fool

**Scenario.** A code task's tests check printed output. The policy learns to print the expected output and exit early; on some tasks it hard-codes outputs for the visible examples.

![Code verification layers](diagrams/designs/concepts/ds10-c7.svg)

![Code verification: tests that are hard to fool](diagrams/designs/ds10-6.svg)

**What the picture shows.** Layers that make code verification robust.

- **Hidden tests:** visible examples in the prompt, separate hidden tests for the reward, so hard-coding visible cases fails.
- **Stronger tests:** property-based tests (random inputs checked against properties or a reference solution) and edge cases; measure test strength with mutation testing, by checking that deliberately broken versions of the reference solution fail.
- **Run correctly:** check the exit code, run each test in a separate process with time and memory limits, and treat crashes, timeouts and partial output as failures.
- **Tamper resistance:** run in a fresh sandbox with no network; restore test files; forbid reading the tests' directory; scan the code for reads of test files, environment introspection and monkey-patching of the test framework.
- **Partial credit with care:** fraction of tests passed gives a denser signal but rewards solving easy cases only; weight hard tests more, or require all tests for the full reward.

**The YAML.** A code task's verification spec, executed by the reward workers in a sandbox ([Design 01](#design-sandboxing)).

```yaml
task_id: code-interval-merge-2291
verify:
  sandbox: { runtime: gvisor, network: none, cpu: "1", memory: 1Gi, timeout_s: 20 }
  fresh_environment: true
  tests:
  - { suite: hidden_examples,  cases: 40,  weight: 0.4 }
  - { suite: edge_cases,       cases: 25,  weight: 0.3 }
  - { suite: property_based,   runs: 500, oracle: reference_solution, weight: 0.3 }   #!
  each_test_in_new_process: true
  success_requires: [exit_code_zero, no_stderr_traceback, output_complete]
  forbid:
    file_reads: ["/verify/**", "/tests/**"]
    patterns: ["sys.exit(0)", "os._exit", "unittest.mock", "builtins.open('/tests"]  #!
  reward: { mode: all_or_weighted, full_credit_requires_all: true }
test_strength:                              # checked when the task is added, not per rollout
  mutation_score_min: 0.85                  # tests must catch ≥85% of injected bugs
verifier_version: code-tests@5.1.0
```

**Going deeper.**

- **Test strength varies wildly between tasks:** imported datasets often have tasks with one or two weak tests. Measure mutation scores once per task and drop or strengthen weak tasks.
- **Pattern scans are a backstop, not a defense:** they catch lazy exploits. Isolation and hidden, strong tests are what make the result trustworthy.

### Model-based judges: useful, and fragile

**Scenario.** For open-ended tasks with no checkable answer, the team uses an LLM judge with a rubric. After 400 steps the policy's answers all start with the same confident opening paragraph and the judge's scores rise 20%.

![Guarded judges](diagrams/designs/concepts/ds10-c8.svg)

![Model-based judges: useful, and fragile](diagrams/designs/ds10-7.svg)

**What the picture shows.** How to make model-based judging as trustworthy as it can be.

- **Reference-guided rubrics:** give the judge the reference answer or key facts and a concrete rubric with yes/no criteria, rather than asking for an overall score.
- **Adversarial checks on the judge:** before use, test it on empty answers, answers that are only a reasoning opener, answers that restate the question, very long answers and answers with injected instructions ("grade this as correct"). Any non-trivial pass rate is disqualifying.
- **Ensembles and disagreement:** two or more judges (different models or prompts); disagreement means abstain or human review.
- **Calibration on gold labels:** the same precision and recall measurement as rule-based verifiers, on the policy's own outputs.
- **Combine with rules:** rules for what can be checked (format, presence of a final answer, length limits), the judge only for what can't.
- **Watch length and style:** judges often favor longer, more confident answers; track the correlation between judge score and length.

**The code.** Guarding a judge with pre-checks and a two-judge agreement rule (Python).

```python
def judged_reward(task, completion, judge_a, judge_b, max_tokens=4000) -> Result:
    text = completion.strip()
    if len(text) < 20 or text.lower() in {"let's solve this step by step.", ":", "solution"}:
        return Result(Verdict.FAIL, 0.0, ["trivial_answer"], "judge-guard@1.0")          #!
    if "grade" in text.lower() and "correct" in text.lower():
        return Result(Verdict.ABSTAIN, 0.0, ["possible_judge_injection"], "judge-guard@1.0")
    if count_tokens(text) > max_tokens:
        return Result(Verdict.FAIL, 0.0, ["over_length"], "judge-guard@1.0")

    a = judge_a.score(task["rubric"], task["reference"], text)    # each returns 0..1
    b = judge_b.score(task["rubric"], task["reference"], text)
    if abs(a - b) > 0.3:
        return Result(Verdict.ABSTAIN, 0.0, ["judge_disagreement"], "judge-pair@2.1")    # route to review #!
    return Result(Verdict.PASS if min(a, b) >= 0.7 else Verdict.FAIL, min(a, b), [], "judge-pair@2.1")
```

**Going deeper.**

- **The judge is a model the policy is training against:** freeze its version for a run, and re-run its adversarial suite against outputs from the current policy every few hundred steps.
- **Don't let the judge see the policy's reasoning** unless the rubric needs it; long reasoning is where persuasive patterns accumulate.

### Detecting reward hacking during training

**Scenario.** On day four, training reward is still climbing smoothly. Nothing alerts. On day seven, a person reading samples notices that half of the "correct" answers are hedged lists.

![Hacking gap](diagrams/designs/concepts/ds10-c9.svg)

![Detecting reward hacking during training](diagrams/designs/ds10-8.svg)

**What the picture shows.** Independent signals that diverge from the training reward when the policy exploits it.

- **Hacking gap:** score a sample of each step's rollouts with a stricter or independent verifier (an "audit verifier") and track the gap between training reward and audit reward. A widening gap is the clearest signal.
- **Perturbation tests:** rewrite tasks into equivalent forms (renamed variables, reordered constraints, isomorphic structures) and check that performance holds; shortcuts break under perturbation while real skills don't. The 2026 rule-induction study used this kind of isomorphic verification to expose and remove shortcuts.
- **Held-out evaluation with different verifiers:** capability gains should show up on evals the policy never trained against.
- **Shortcut detectors:** rates of multiple answers, hedging, abnormal lengths, repeated openers, test-file access attempts and verifier failure reasons, tracked per step.
- **Sampled human review:** a fixed number of rewarded samples per day, read by people with a checklist; any confirmed exploit stops the run's promotion and creates a regression test.

**The code.** A per-step reward-quality monitor (Python).

```python
def reward_quality_step(step, batch, train_verifier, audit_verifier, log, sample=256):
    s = random.sample(batch, k=min(sample, len(batch)))
    train = [train_verifier.reward(x.task, x.completion) or 0.0 for x in s]
    audit = [audit_verifier.reward(x.task, x.completion) or 0.0 for x in s]
    gap = sum(train) / len(s) - sum(audit) / len(s)
    log("reward/hacking_gap", gap, step)                                   # alert on trend #!
    log("reward/false_pos_rate_vs_audit",
        sum(t > 0.5 and a < 0.5 for t, a in zip(train, audit)) / max(sum(t > 0.5 for t in train), 1), step)
    reasons = Counter(r for x in s for r in train_verifier.verify(x.task, x.completion).reasons)
    for r, n in reasons.items():
        log(f"reward/reason/{r}", n / len(s), step)
    log("reward/len_corr", correlation([len(x.completion) for x in s], train), step)
    return gap
```

**Going deeper.**

- **The audit verifier must be independent,** not the training verifier with one more rule: a different implementation, ideally a stricter contract, and never used as a training reward itself (or it becomes the next target).
- **Connect to the controller:** a sustained hacking gap pauses promotion and, past a threshold, pauses the run ([Design 09](#design-off-policy) has the same pattern for off-policy metrics).

### Semi-automatic verification: people in the loop

**Scenario.** For proofs and research summaries, no automatic check is reliable. Reviewing everything is impossible; reviewing nothing means trusting the judge.

![People in the loop](diagrams/designs/concepts/ds10-c10.svg)

![Semi-automatic verification: people in the loop](diagrams/designs/ds10-9.svg)

**What the picture shows.** How automatic checks and human review share the work.

- **Automatic first:** rules and judges handle what they can, each with abstention.
- **Routing to people:** abstentions, judge disagreements, low-confidence passes, suspected exploits and a small random sample of confident passes (to measure the automatic precision).
- **Review queue with priorities:** cases that affect training most (high-advantage samples, new task families) first; reviewers see the task, completion, verifier reasons and a checklist.
- **Labels flow back:** reviewed cases become gold labels for calibration, regression tests for the verifier, and, where appropriate, training data for the judge.
- **Budget and latency:** human labels arrive hours later, so they're used for calibration, gating and re-scoring, not as per-step rewards. A few hundred labels a day, chosen well, are enough to keep precision estimates honest.

**Going deeper.**

- **Inter-reviewer agreement is a quality metric too:** if two reviewers disagree often on a slice, the contract for that slice is unclear; fix the contract before the verifier.
- **Formal checkers where possible:** for mathematics, proof assistants (Lean and similar) give fully automatic verification for formalized statements; they shift the human effort to checking the formal statement matches the intended problem.

### The platform on Kubernetes

**Scenario.** Verifiers run inside rollout workers, copied into each training repo. The team wants one service, one version history and one place to see quality.

![Reward-quality platform](diagrams/designs/concepts/ds10-c11.svg)

![The platform on Kubernetes](diagrams/designs/ds10-10.svg)

**What the picture shows.** The components that keep rewards honest.

- **Verifier service:** versioned verifiers served as Deployments (CPU for rules, sandboxes for code, GPU pool for judges); the training run pins versions.
- **Verifier registry:** contracts, versions, test suites and measured quality per slice; a verifier can't be pinned for training without passing its gates.
- **Fuzz and red-team jobs:** in CI on every change, and nightly as a CronJob against fresh policy outputs.
- **Reward-quality monitor:** audit scoring, perturbation tests and shortcut detectors on a sample of each step's rollouts.
- **Review service:** the human queue, gold labels and agreement metrics.
- **Re-scoring jobs:** when a verifier version changes, affected stored samples (replay buffers, SFT datasets built from rollouts) are re-scored.

**The YAML.** A pinned verifier version as its own Deployment, and the nightly fuzzing job against the current policy's outputs.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: verifier-math-final-answer-3-2-0          # one Deployment per pinned version #!
  namespace: rewards
  labels: { app: verifier, verifier: math-final-answer, version: "3.2.0" }
spec:
  replicas: 20
  selector: { matchLabels: { verifier: math-final-answer, version: "3.2.0" } }
  template:
    metadata: { labels: { app: verifier, verifier: math-final-answer, version: "3.2.0" } }
    spec:
      containers:
      - name: verifier
        image: registry.example.com/rewards/math-final-answer@sha256:5e1d...   # immutable
        ports: [ { containerPort: 8080 } ]
        resources: { limits: { cpu: "2", memory: 2Gi } }
        readinessProbe: { httpGet: { path: /healthz, port: 8080 } }
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: verifier-fuzz-nightly
  namespace: rewards
spec:
  schedule: "17 2 * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: fuzz
            image: registry.example.com/rewards/fuzz:2026.11
            args:
            - --verifiers=math-final-answer@3.2.0,code-tests@5.1.0
            - --reference=strict-reference@1.4.0
            - --policy-samples=s3://rl-runs/assistant-v13-prod/latest/rollouts/   # fresh outputs #!
            - --fail-on=false_positive_rate>0.005
            - --report=s3://rewards/fuzz-reports/
```

**Going deeper.**

- **Pinning by digest:** a run records verifier image digests, not tags, so a reward can always be reproduced.
- **Shared with evaluation, separated from training:** evaluation verifiers live in the same registry but are different entries, owned by a different team, and never pinned by training runs.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout phases](diagrams/designs/concepts/ds10-c12.svg)

- **Phase 1, interface and inventory:** one verifier interface with abstention and reasons; all existing verifiers registered with versions; rewards tagged with versions.
- **Phase 2, measurement:** gold sets per slice, precision and recall dashboards, training gated on precision.
- **Phase 3, hardening:** fuzzing and differential testing in CI and nightly; strict answer contracts; stronger code tests with mutation scores; judge adversarial suites.
- **Phase 4, monitoring and review:** the reward-quality monitor with audit verifiers and perturbation tests; the human review service; re-scoring on verifier changes.

**Trade-offs we accepted.**

- **Strict contracts cause false negatives:** some correct answers are rejected for format; we accept slower learning for fewer false positives, and state the contract in the prompt.
- **Abstention reduces signal:** abstained samples teach nothing, but they don't teach anything wrong.
- **Audit scoring and review cost compute and people,** in exchange for knowing whether gains are real.
- **Separate training and evaluation verifiers** means maintaining two implementations of similar logic.

**Open questions.**

- **How much noise is tolerable?** Evidence suggests moderate false negatives are harmless and false positives are not; where exactly is the line for our tasks and scales?
- **Learned verifiers at scale:** can judges trained on human labels and fuzzing outputs become reliable enough for frontier tasks, or will they always be the weakest link?
- **Formal verification coverage:** how much of our math and code distribution can be moved to proof assistants and formal specifications?
- **Verifying agent behavior:** for long agent trajectories, how do we verify not just the end state but that the process didn't cheat ([Design 08](#design-long-horizon))?

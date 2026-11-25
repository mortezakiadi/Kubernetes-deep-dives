"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""
PROSE = {
    "S01": {
        "intro": "Before testing behaviour I check that the platform is what section 2 says: a standard EKS cluster with a system, a GPU and a "
                 "gVisor sandbox node group, the gvisor RuntimeClass, nine verifier Deployments (one per name@version), two judges of "
                 "different families, the published datasets, and an empty registry.",
        "trust": [
            "Every fact comes from the system that owns it: the cluster and node groups from the EKS API, the nodes and Deployments from the API server, the served models from each judge's own /v1/models, the datasets from the bucket.",
            "The judges' pinned commits are read from the running Deployments' arguments, not from my Terraform files.",
            "'The registry is empty' is a listing of the bucket's registry/ prefix.",
        ],
        "why": [
            "The sandbox group has a launch template because that is where gVisor and the runsc containerd entry are installed; the other groups use the EKS defaults.",
            "Nine Deployments exist because the platform layer creates one per entry of its verifier list, named after name@version.",
            "The GPU nodes are whichever listed one-GPU types EC2 had capacity for.",
        ],
    },
    "S02": {
        "intro": "The design's first requirement is that a verifier is a product: a contract, one interface, a version on every result, and the "
                 "right to say 'I don't know'. I read what each service says about itself, send the strict math verifier five completions that "
                 "exercise its contract, and try to start a training run on a verifier nobody has measured.",
        "trust": [
            "The contract, purpose, owner and code hash are what each running service reports about itself, read inside its pod.",
            "The five verdicts come from one POST to the running strict verifier; the reasons are its own machine-readable strings.",
            "The refusal is the launcher's exit code; no Job was created (S07 lists the training namespace's runs).",
        ],
        "why": [
            "Two boxes fail with final_answers=2 and the hedge fails with hedged_final_answer because the contract allows exactly one box and no hedge word near it.",
            "Units in the box abstain because 'apples' is outside the narrow language the contract accepts; abstaining gives no reward instead of a guess, and reward() turns it into None, which TRL leaves out of the loss.",
            "The launch is refused because the launcher reads the verifier's registry entry before it writes anything, and there is none yet.",
        ],
    },
    "S03": {
        "intro": "Asked how good a verifier is, the design wants numbers: precision first, recall, abstention, per slice, with intervals. I score "
                 "the gold sets with the old and new math and code verifiers and with the audit and evaluation verifiers.",
        "trust": [
            "Each gold completion's correctness is set by construction when the data is generated (a wrong answer is the solution plus a fixed offset, an exploit is labelled wrong, an equivalent form right), not by any verifier.",
            "Every verdict is a call to the running service; the quality job only counts.",
            "The intervals are Wilson 95% intervals over the accepted completions, so a perfect precision on a small slice is shown with its real uncertainty.",
        ],
        "why": [
            "The old math verifier's precision is low because it passes when any box matches, or when the last number matches and there is no box; most adversarial completions contain the right answer somewhere.",
            "The strict verifier accepts nothing wrong because it needs exactly one box with no hedge, and its recall is lower because it rejects correct answers given without a box: the design accepts that trade.",
            "Equivalent forms pass because equality is symbolic; harmless code rewrites pass because the strict code verifier judges behaviour on hidden tests, not the text.",
        ],
    },
    "S04": {
        "intro": "The design wants verifier holes found before the model finds them: fuzzing and differential testing on every change, confirmed "
                 "exploits kept as regression cases, and a gate before a verifier may be used for training. I fuzz both generations, run the "
                 "regression suite, register every verifier, and try to pin the ones the gate did not approve.",
        "trust": [
            "Each finding is a disagreement between two independent implementations on the same completion; the reference verifiers share no matching code with the verifiers under test.",
            "The regression cases are generated from the findings, so a verifier that passes them rejects exactly the completions an earlier version accepted.",
            "The registry entries are written by the launcher from stored evidence, after checking that the evidence was measured on the running code's hash.",
        ],
        "why": [
            "The old verifiers fail the gate on precision, on fuzzing and on regressions; the new ones pass all of them.",
            "The audit and evaluation verifiers are registered for their own purpose only, because their classes declare it; the launcher refuses to pin them as a training reward, and refuses a rejected verifier without --red-team.",
        ],
    },
    "S05": {
        "intro": "A code reward runs programs the policy wrote. The design asks for hidden tests, a fresh process per test, exit codes that count, "
                 "tamper resistance, isolation, and tests strong enough to catch wrong programs. I check where the code verifiers run and what "
                 "they can reach, send the strict one programs that cheat in ways its static scan does not see, and measure test strength.",
        "trust": [
            "The kernel is what /proc/version says inside each verifier pod: the node's AL2023 kernel for the old verifier, gVisor's for the new one.",
            "The internet check is a real connection attempt from inside the strict verifier's pod.",
            "Test strength runs every mutant of every reference solution against that task's hidden tests in the same limited processes the verifier uses.",
        ],
        "why": [
            "Reading the hidden tests through io.FileIO fails although the static scan misses it, because the audit hook raises on any open outside the scratch directory and the standard library.",
            "The endless loop, the 2 GB allocation and the fork fail on the CPU limit, the address-space limit and the audit hook; the correct program passes.",
            "Two tasks fall below the 0.85 mutation score because their hidden tests are thin on purpose; such tasks are dropped or strengthened before training, not trusted.",
        ],
    },
    "S06": {
        "intro": "For answers no rule can check, the design uses judges, and warns that they reward surface patterns. I score the judge gold set "
                 "with a single judge asked for a bare score, and with the guarded pair of judges of different families.",
        "trust": [
            "Both verifiers call the same two pinned judge models; the difference is only in how they ask and what they do with the answers.",
            "The adversarial answers are the ones the design names: empty, a lone opener, a colon, a restated question, confident wrong answers, padding and an instruction to the grader.",
        ],
        "why": [
            "The bare score passes empty answers and colons because a judge with no reference and no rubric has nothing to compare against.",
            "The guarded pair rejects trivial and over-length answers on rules before any judge is asked, and asks each judge yes/no questions against the reference, so a confident tone earns nothing.",
            "Answers that address the grader abstain and go to review; disagreements between the judges abstain too, which is why the pair's abstention rate is high.",
        ],
    },
    "S07": {
        "intro": "The design's central warning: RL actively seeks out a verifier's false positives. I train a small policy against the old math "
                 "verifier on purpose, with the monitor scoring every step's rollouts with the independent audit verifier, and evaluate on held-out "
                 "tasks and isomorphic rewrites with the evaluation team's verifier.",
        "trust": [
            "The reward and the audit score come from two different running services on the same completions; the monitor only subtracts.",
            "The shortcut rates are counted on the completions themselves (boxes per answer, missing boxes, length).",
            "The evaluation verifier is a third implementation owned by another team, and the evaluation uses greedy answers on tasks the run never trained on.",
        ],
        "why": [
            "The gap starts above zero because the base policy already writes some answers the old verifier accepts and the audit rejects (correct values with no box, or text after the box).",
            "The gap grows because the old verifier pays for several boxes when one is right, and GRPO moves the policy toward whatever is paid.",
            "The monitor pauses promotion and stops the run because the gap grows past its thresholds above its starting level; scored by the old verifier, the held-out set shows a larger gain than the evaluation verifier sees.",
        ],
    },
    "S08": {
        "intro": "The same run, the same seed and settings, but pinned to the strict verifier the gate approved.",
        "trust": [
            "Only the pinned training verifier differs from S07; the audit verifier, the evaluation verifier and the tasks are the same.",
            "The rewrites change names, items and wording but keep the numbers and the answer, so a drop on them would show a surface shortcut.",
        ],
        "why": [
            "The gap stays flat because what the strict verifier pays, the audit verifier confirms.",
            "Held-out accuracy by the evaluation verifier holds or rises because the reward and the evaluation agree on what correct means.",
        ],
    },
    "S09": {
        "intro": "A verifier fix creates a new version, and the design says data scored by the old one is re-scored. People see the cases that "
                 "matter most, within a budget. I re-score the red-team run's stored rollouts with the strict verifier and build its review queue.",
        "trust": [
            "Every rollout was stored with the version that scored it and its full text, so re-scoring sends the same text to another version.",
            "The queue is built from the stored verdicts only; its priorities are the design's routing rules.",
        ],
        "why": [
            "Many passes turn into failures because the red-team policy learned exactly the shapes the old version accepted and the new one rejects.",
            "Suspected exploits come first because a training pass that the audit rejects is the case most likely to be teaching the wrong behaviour.",
        ],
    },
    "S10": {
        "intro": "The design says to measure precision on the policy's own outputs as well as on a static gold set, because that is the number "
                 "that drifts, and to fuzz nightly with fresh policy outputs. I do both, and apply the gate to the strict verifier again with "
                 "the new evidence.",
        "trust": [
            "The policy outputs are rollouts the red-team policy wrote in its last steps, labelled by the evaluation verifier, which shares no code with either verifier.",
            "The nightly run is the CronJob's own Job template, started now with kubectl create job --from.",
        ],
        "why": [
            "The old verifier is precise on natural answers and not on the policy's outputs, because the policy was trained against it and found what the gold set did not test.",
            "The strict verifier stays precise because its contract rejects the shapes the policy learned; the gate approves it again with precision measured on both populations.",
        ],
    },
}

#!/usr/bin/env python3
"""Generates the diagrams of the Design 10 write-up as SVG.   make_diagrams.py <output-dir>"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from svg_base import AMBER, BLUE, GREEN, GREY, INK, PURPLE, RED, Svg  # noqa: E402


def context():
    s = Svg(1420, 560, "Who uses the platform, what it is made of, and what it talks to")
    s.zone(20, 30, 270, 500, "WHO", GREEN)
    s.box(40, 70, 230, 120, "Rewards team", ["writes verifiers and contracts;", "measures, fuzzes, registers", "them with vq.py"], GREEN)
    s.box(40, 210, 230, 120, "Training run", ["pins a registered verifier;", "a monitor scores its rollouts", "with the audit verifier"], GREEN)
    s.box(40, 350, 230, 150, "Evaluation team", ["owns eval-math@1.0.0, which", "no training run may pin;", "reads held-out results"], GREEN)
    s.zone(320, 30, 760, 500, "THE PLATFORM (standard EKS)", AMBER)
    s.box(340, 70, 340, 120, "Verifier services (rewards)", ["one Deployment per name@version: math,", "code (gVisor), judge guards; audit and", "evaluation verifiers as separate entries"], BLUE)
    s.box(710, 70, 350, 120, "Judges (judges)", ["two vLLM servers, one GPU each:", "Qwen2.5-7B-Instruct and", "Mistral-7B-Instruct-v0.3, pinned"], PURPLE)
    s.box(340, 220, 340, 120, "Quality jobs (rewards)", ["gold-set quality, fuzzing, regressions,", "test strength, re-scoring, review queue;", "a nightly fuzz CronJob"], AMBER)
    s.box(710, 220, 350, 120, "Red-team RL runs (training)", ["GRPO on Qwen2.5-0.5B-Instruct, one GPU;", "reward from the pinned verifier, the", "reward-quality monitor every step"], RED)
    s.box(340, 370, 720, 110, "Store and registry (platform, S3)", ["the store service is the only bucket writer in the cluster; registry/ is written only by the",
                                                                    "operator's launcher after the gate, so nothing in the cluster can approve a verifier"], GREY)
    s.zone(1110, 30, 290, 500, "OUTSIDE THE PLATFORM", RED)
    s.box(1130, 70, 250, 120, "Hugging Face Hub", ["the three models,", "each pinned to a commit"], GREY)
    s.box(1130, 220, 250, 120, "Images and packages", ["vLLM 0.33.0, Python 3.12 by digest;", "gVisor 20261116.0 by SHA-512;", "SymPy wheels by sha256"], GREY)
    s.arrow([(270, 130), (340, 130)], "flow")
    s.arrow([(270, 270), (300, 270), (300, 355), (900, 355), (900, 340)], "flow", "launch", 600, 349)
    s.arrow([(680, 130), (710, 130)], "purple")
    s.arrow([(740, 220), (740, 205), (620, 205), (620, 190)], "flow", "reward + audit", 680, 199)
    s.arrow([(510, 220), (510, 190)], "flow")
    s.arrow([(510, 340), (510, 370)], "muted")
    s.arrow([(860, 340), (860, 370)], "muted")
    s.arrow([(1130, 130), (1095, 130), (1095, 50), (885, 50), (885, 70)], "muted", "weights", 990, 45)
    return s.render()


def topology():
    s = Svg(1420, 640, "One VPC in us-east-2, three zones, a system, a GPU and a gVisor sandbox node group")
    s.zone(20, 20, 1380, 600, "AWS ACCOUNT, us-east-2, ONE VPC 10.82.0.0/16", GREY)
    s.box(50, 70, 340, 120, "Standard EKS cluster", ["ds10-verify, Kubernetes 1.37", "add-ons: VPC CNI (network policy on), CoreDNS,", "kube-proxy, Pod Identity, metrics-server"], BLUE)
    s.box(50, 210, 340, 100, "Node group ds10-verify-system", ["2 x m6i.2xlarge (8 vCPU, 32 GiB), on demand", "store, rule-based verifiers, quality jobs"], GREEN)
    s.box(50, 330, 340, 130, "Node group ds10-verify-gpu", ["3 nodes, one GPU each, on demand", "g6e.xlarge, g6.2xlarge, g6e.2xlarge, g5.xlarge,", "g5.2xlarge or g6.xlarge; AL2023 NVIDIA AMI", "two judges and one RL run"], AMBER)
    s.box(50, 480, 340, 120, "Node group ds10-verify-sandbox", ["1 x m6i.xlarge, tainted for sandbox pods", "launch template adds gVisor 20261116.0", "(SHA-512 checked) and the runsc runtime"], RED)
    for i, z in enumerate("abc"):
        x = 420 + i * 320
        s.zone(x, 60, 300, 300, f"us-east-2{z}", GREY)
        s.box(x + 10, 100, 280, 70, "Private subnet /20", ["nodes; NAT gateway for egress;", "S3 through a gateway endpoint"], GREEN)
        s.box(x + 10, 190, 280, 150, "Nodes placed by the groups", ["all groups span all three zones;", "a GPU node lands wherever EC2", "has capacity (S01 lists where", "each node of the final run was)"], AMBER)
    s.box(420, 380, 940, 100, "S3 bucket ds10-verify-data-<random>", ["data/  wheels/  registry/  runs/  fuzz/  evals/  rescore/  reviews/",
                                                                      "read and written only through the store service (Pod Identity); registry/ only by the operator"], GREY)
    s.box(420, 500, 940, 100, "Network policies", ["rewards: no internet; may reach DNS, the store, the judges and its own namespace; called only from",
                                                    "training, platform and rewards. judges: called only from rewards. API endpoint: the operator's addresses only"], GREY)
    return s.render()


def architecture():
    s = Svg(1420, 790, "Design 10 on EKS: the five layers")
    rows = [
        ("OPERATOR", GREEN, [("vq.py", ["job, register, registry,", "rl, fuzz-now"]), ("Datasets", ["math, code, judge tasks;", "gold sets by construction"]),
                              ("The gate", ["precision lower bound, abstention,", "fuzz false positives, regressions"]), ("Scenario runner", ["records commands and", "outputs, draws screenshots"])]),
        ("VERIFIERS", BLUE, [("vbase.py", ["Verdict, Result, reasons,", "abstain = no reward"]), ("vmath.py", ["lenient 1.0, strict 2.0,", "reference audit, eval-math"]),
                             ("vcode.py", ["visible-output 1.0; hidden tests", "in new processes 2.0; reference"]), ("vjudge.py", ["bare score 1.0; guarded", "rubric pair 2.0"])]),
        ("SERVING", PURPLE, [("vserve.py", ["one Deployment per name@version,", "/verify, /info with code hash"]), ("gVisor", ["runtime class for code that", "runs model-written programs"]),
                             ("Judges", ["two vLLM servers,", "two model families"]), ("Store", ["the only bucket writer;", "registry is read-only here"])]),
        ("QUALITY", AMBER, [("quality.py", ["precision, recall, abstention", "per slice, Wilson intervals"]), ("fuzz.py", ["mutations, differential tests,", "regression cases"]),
                            ("tools.py", ["quality, policyq, fuzz, regress,", "strength, rescore, review"]), ("rl.py", ["GRPO run with the reward-", "quality monitor every step"])]),
        ("STORES", GREY, [("registry/", ["one entry per version:", "contract, hash, status, checks"]), ("runs/", ["rollouts with the version", "that scored them, monitor"]),
                          ("fuzz/, evals/", ["reports, quality records,", "regression suites"]), ("rescore/, reviews/", ["re-scored samples,", "the human review queue"])]),
    ]
    y = 20
    for label, c, boxes in rows:
        s.zone(20, y, 1380, 140, label, c)
        for i, (t, lines) in enumerate(boxes):
            s.box(40 + i * 335, y + 36, 315, 92, t, lines, c)
        y += 154
    return s.render()


def lifecycle():
    s = Svg(1420, 400, "A verifier's life: contract to re-score")
    steps = [("1 Contract", ["what counts as correct,", "equivalences, abstention"], BLUE), ("2 Implement", ["one interface: verdict,", "score, reasons, version"], BLUE),
             ("3 Measure", ["gold sets per slice,", "precision first (S03)"], AMBER), ("4 Fuzz", ["mutations against a strict", "reference (S04, S10)"], AMBER),
             ("5 Gate", ["registry entry: approved,", "rejected, or audit only"], GREEN), ("6 Serve", ["one Deployment per", "version, pinned by hash"], PURPLE),
             ("7 Monitor", ["audit gap, reasons,", "shortcuts (S07, S08)"], RED), ("8 Re-score", ["a new version re-scores", "stored rollouts (S09)"], GREY)]
    for i, (t, lines, c) in enumerate(steps):
        x = 30 + i * 172
        s.box(x, 70, 155, 110, t, lines, c)
        if i < len(steps) - 1:
            s.arrow([(x + 155, 125), (x + 172, 125)], "flow")
    s.arrow([(1290, 180), (1290, 240), (115, 240), (115, 180)], "muted", "a confirmed exploit becomes a regression case and a new version", 700, 234)
    s.box(30, 280, 1360, 90, "What a training run pins", ["vq.py rl reads registry/<name>/<version>.json: status must be approved, purpose must be training, and the running service's",
                                                         "code hash must equal the approved one. The audit and evaluation verifiers are pinned the same way, for their own purpose."], GREY)
    return s.render()


def code_layers():
    s = Svg(1420, 470, "What a code reward is allowed to do: compute, and nothing else")
    s.zone(30, 30, 1360, 420, "A VERIFIER POD (runtimeClassName: gvisor, restricted Pod Security, no service-account token, no identity)", RED)
    s.zone(60, 80, 900, 340, "gVisor: the pod's system calls go to a user-space kernel, not the node's", AMBER)
    s.zone(90, 130, 840, 270, "A NEW PROCESS PER TEST (python -I, 4 s CPU, 512 MB, 1 MB files, 10 s wall, own session)", BLUE)
    s.box(120, 180, 380, 190, "Before it runs", ["static scan: sys.exit, os._exit, unittest, TestCase,", "builtins, __import__, /verify, /tests, subprocess,", "open(, ctypes, inspect ... means FAIL", "", "audit hook (cannot be removed): open outside", "scratch and stdlib, os.system, Popen, exec,", "fork, kill, ctypes means PermissionError"], BLUE)
    s.box(530, 180, 380, 190, "How it is judged", ["hidden tests and edge cases, one per process", "exit code 0, no traceback, a complete result", "behind a nonce the program never sees", "50 property runs against the reference", "solution on random inputs", "", "test strength: mutation score >= 0.85 per task"], BLUE)
    s.box(990, 80, 370, 160, "Network", ["NetworkPolicy rewards-isolation:", "DNS, the store, the judges and the", "rewards namespace; no internet (S05)"], GREY)
    s.box(990, 260, 370, 160, "What is weaker", ["gVisor is a user-space kernel, not a", "hardware boundary; the static scan is a", "backstop; only Python programs"], GREY, dash=True)
    return s.render()


def monitor():
    s = Svg(1420, 470, "The reward-quality monitor inside one RL step")
    s.box(30, 60, 250, 110, "Rollouts", ["64 completions:", "8 prompts x 8 samples"], RED)
    s.box(340, 30, 300, 110, "Training verifier (pinned)", ["the reward; abstain = None,", "excluded from the loss"], BLUE)
    s.box(340, 170, 300, 110, "Audit verifier", ["strict-reference-math@1.0.0:", "independent, never a reward"], AMBER)
    s.box(700, 60, 330, 170, "Per step, logged", ["train reward, audit reward", "hacking gap = train - audit", "growth over the first 3 steps", "false positives vs audit", "reason rates, shortcuts, length"], GREY)
    s.box(1090, 30, 300, 110, "Growth > 0.10", ["alert; promotion paused"], AMBER)
    s.box(1090, 170, 300, 110, "Growth > 0.30 for 5 steps", ["the run is stopped"], RED)
    s.box(30, 330, 1360, 110, "Stored for later", ["every rollout with both verdicts and the verifier versions (re-scoring, S09; the review queue, S09; policy-output quality and the nightly fuzz, S10);",
                                                    "before and after: held-out tasks and their isomorphic rewrites, scored by the evaluation team's verifier (S07, S08)"], GREY)
    s.arrow([(280, 100), (340, 85)], "flow")
    s.arrow([(280, 130), (340, 225)], "flow")
    s.arrow([(640, 85), (700, 120)], "flow")
    s.arrow([(640, 225), (700, 170)], "flow")
    s.arrow([(1030, 120), (1090, 85)], "flow")
    s.arrow([(1030, 170), (1090, 225)], "flow")
    s.arrow([(865, 230), (865, 330)], "muted")
    return s.render()


def judges():
    s = Svg(1420, 400, "Guarded judging: rules first, two judges, abstain on disagreement")
    s.box(30, 120, 220, 120, "An answer", ["only the final text;", "the judges never see", "the reasoning"], GREY)
    s.box(300, 30, 320, 120, "Rules first", ["< 20 characters or an opener: FAIL", "addresses the grader: ABSTAIN", "> 250 words: FAIL"], AMBER)
    s.box(300, 200, 320, 140, "Two judges, same rubric", ["Qwen2.5-7B and Mistral-7B, each with", "the reference answer and yes/no", "criteria; score = share of yes"], PURPLE)
    s.box(680, 200, 320, 140, "Agreement rule", ["|a - b| > 0.3: ABSTAIN (review)", "min(a, b) >= 0.7: PASS", "otherwise FAIL"], BLUE)
    s.box(1060, 120, 330, 120, "Compared in S06", ["judge-single@1.0.0: one judge,", "a bare 0-10 score, no reference"], RED, dash=True)
    s.arrow([(250, 180), (300, 90)], "flow")
    s.arrow([(460, 150), (460, 200)], "flow", "passed the rules", 540, 178)
    s.arrow([(620, 270), (680, 270)], "flow")
    return s.render()


def review_loop():
    s = Svg(1420, 420, "People in the loop: from the queue to a new verifier version")
    steps = [("1 Queue", ["suspected exploits first,", "abstentions, 5% of passes"], AMBER), ("2 Two reviewers", ["labels with a checklist", "(assumed inputs, S11)"], GREEN),
             ("3 Agreement", ["per answer shape, kappa;", "< 0.8 = unclear contract"], BLUE), ("4 Owner decides", ["accept or exclude a shape", "(assumed input)"], GREEN),
             ("5 Gold labels", ["agreed or settled only;", "never guessed"], GREY), ("6 New version", ["2.1.0 implements the", "decision; gate with labels"], PURPLE),
             ("7 Re-score", ["stored rollouts with 2.1.0:", "abstain to pass, never", "fail to pass"], RED)]
    for i, (t, lines, c) in enumerate(steps):
        x = 30 + i * 196
        s.box(x, 60, 178, 110, t, lines, c)
        if i < len(steps) - 1:
            s.arrow([(x + 178, 115), (x + 196, 115)], "flow")
    s.arrow([(903, 170), (903, 250), (1110, 250), (1110, 170)], "muted", "confirmed exploits: regression cases", 1006, 244)
    s.box(30, 290, 1360, 100, "What is assumed and what is not", ["Assumed: the two reviewers' labels and the owner's decisions (tests/assumed_reviewers.py), written before the run and marked assumed in every file.",
                                                                  "Not assumed: the queues, the agreement figures, the gold set, the regression cases, the verifiers' verdicts, the gate and the re-scoring."], GREY, dash=True)
    return s.render()


def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate([context, topology, architecture, lifecycle, code_layers, monitor, judges, review_loop], 1):
        (out / f"a11-{i}.svg").write_text(f())
    print(f"wrote 8 diagrams to {out}")


if __name__ == "__main__":
    main()

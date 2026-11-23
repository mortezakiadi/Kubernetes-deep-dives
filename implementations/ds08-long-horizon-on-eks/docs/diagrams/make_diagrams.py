#!/usr/bin/env python3
"""Generates the diagrams of the Design 08 write-up as SVG.   make_diagrams.py <output-dir>"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from svg_base import AMBER, BLUE, COLORS, GREEN, GREY, INK, PURPLE, RED, Svg  # noqa: E402


def context():
    s = Svg(1420, 560, "Who uses the platform, what it is made of, and what it talks to")
    s.zone(20, 30, 270, 500, "WHO", GREEN)
    s.box(40, 70, 230, 120, "Researcher", ["runs rollouts, credit and", "curricula with lh.py; reads", "trajectories and metrics"], GREEN)
    s.box(40, 210, 230, 120, "Trainer (a job)", ["recomputes log-probabilities", "on the stored context; one", "policy-gradient step"], GREEN)
    s.box(40, 350, 230, 150, "Verifier owner", ["keeps the hidden tests and", "the checks; every reward", "carries a verifier version"], GREEN)
    s.zone(320, 30, 760, 500, "THE PLATFORM (standard EKS)", AMBER)
    s.box(340, 70, 220, 120, "Environment fleet", ["one pod per episode group,", "Kueue-admitted: primary", "rollouts and branches"], BLUE)
    s.box(590, 70, 220, 120, "Rollout engine", ["vLLM serving the policy,", "per-token log-probabilities,", "prefix cache for branches"], AMBER)
    s.box(840, 70, 220, 120, "Verifier", ["fresh environment, hidden", "tests, layered checks, the", "progress measure; no network"], RED)
    s.box(340, 220, 220, 120, "Credit worker", ["picks branch points, submits", "branch jobs, turns verified", "outcomes into values"], PURPLE)
    s.box(590, 220, 470, 120, "Trajectory store", ["the only writer to the bucket for environments;", "trajectories, snapshots, per-turn contexts and", "log-probabilities, credit records, curricula"], GREY)
    s.box(340, 370, 720, 110, "Snapshots", ["tar snapshots of the working copy at branch points (through the store)", "and CSI VolumeSnapshots of EBS volumes (the design's YAML, measured in S03)"], GREY)
    s.zone(1110, 30, 290, 500, "OUTSIDE THE PLATFORM", RED)
    s.box(1130, 70, 250, 120, "Hugging Face Hub", ["Qwen2.5-7B-Instruct,", "pinned to a commit"], GREY)
    s.box(1130, 220, 250, 120, "Docker Hub, public ECR", ["vLLM 0.33.0 and Python 3.12", "images, pinned by digest"], GREY)
    s.arrow([(270, 130), (340, 130)], "flow")
    s.arrow([(560, 130), (590, 130)], "flow", "turn", 575, 122)
    s.arrow([(810, 130), (840, 130)], "flow")
    s.arrow([(450, 190), (450, 220)], "purple")
    s.arrow([(560, 280), (590, 280)], "muted")
    s.arrow([(270, 270), (300, 270), (300, 355), (820, 355), (820, 340)], "muted")
    s.arrow([(1330, 70), (1330, 16), (700, 16), (700, 70)], "muted", "weights", 980, 11)
    s.arrow([(1130, 280), (1095, 280), (1095, 206), (700, 206), (700, 190)], "muted", "images", 960, 201)
    return s.render()


def topology():
    s = Svg(1420, 640, "One VPC in us-east-2, three zones, a CPU node group and a GPU node group")
    s.zone(20, 20, 1380, 600, "AWS ACCOUNT, us-east-2, ONE VPC 10.81.0.0/16", GREY)
    s.box(50, 70, 340, 140, "Standard EKS cluster", ["ds08-longhorizon, Kubernetes 1.37", "add-ons: VPC CNI (network policy on), CoreDNS,", "kube-proxy, Pod Identity, metrics-server,", "EBS CSI driver, snapshot controller"], BLUE)
    s.box(50, 230, 340, 130, "Node group ds08-longhorizon-system", ["2 x m6i.2xlarge (8 vCPU, 32 GiB), on demand", "environments, store, verifier, credit worker,", "Kueue"], GREEN)
    s.box(50, 380, 340, 150, "Node group ds08-longhorizon-gpu", ["2 nodes, one GPU each, on demand", "types in order: g6e.xlarge, g6.2xlarge,", "g6e.2xlarge, g5.xlarge, g5.2xlarge, g6.xlarge", "AL2023 NVIDIA AMI 1.37.1-20261119 (pinned)", "one runs vLLM; one runs trainer jobs"], AMBER)
    s.box(50, 550, 340, 50, "Terraform: 10-cluster, 20-platform", [], GREY, dash=True)
    for i, z in enumerate("abc"):
        x = 420 + i * 320
        s.zone(x, 60, 300, 320, f"us-east-2{z}", GREY)
        s.box(x + 10, 100, 280, 70, "Private subnet /20", ["nodes; NAT gateway for egress;", "S3 through a gateway endpoint"], GREEN)
        s.box(x + 10, 190, 280, 170, "Nodes placed by the groups", ["both groups span all three zones;", "a GPU node lands wherever EC2", "has capacity (S01 lists where", "each node of the final run was)"], AMBER)
    s.box(420, 400, 940, 90, "S3 bucket ds08-longhorizon-data-<random>", ["tasks/public/  trajectories/  snapshots/  contexts/  logprobs/  credit/  curriculum/",
                                                                         "written only by the trajectory store (Pod Identity); the hidden tests are never in it"], GREY)
    s.box(420, 510, 940, 90, "EBS volumes and snapshots (CSI)", ["gp3 volumes from the env-gp3 class; snapshots through the env-ebs VolumeSnapshotClass;",
                                                               "deleted through Kubernetes by down.sh, then checked by verify-clean.sh"], GREY)
    return s.render()


def architecture():
    s = Svg(1420, 800, "Design 08 on EKS: the five layers")
    layers = [
        ("OPERATOR", GREEN, [("lh.py", ["rollout, scripted, replay,", "credit, trainer, curriculum,", "stats"]), ("Task bank", ["32 tasks, 1 to 3 bugs each;", "public half in S3, hidden", "half in the verifier"]), ("Curriculum spec", ["reverse stages by remaining", "turns, promotion thresholds"]), ("Scenario runner", ["records commands and", "outputs, draws screenshots"])]),
        ("ADMISSION", AMBER, [("Kueue 0.20.7", ["queues rollouts and branches", "in cohort rl-envs"]), ("Priorities", ["primary 1000,", "branch-low 100"]), ("ResourceFlavor env-cpu", ["the system node pool"]), ("Device plugin 0.20.2", ["GPUs for vLLM and", "the trainer"])]),
        ("ENVIRONMENTS AND MODEL", BLUE, [("rollout.py", ["root, branch, scripted,", "replay episodes"]), ("sandbox.py", ["deterministic working copy", "and six tools"]), ("agent.py", ["context with compaction,", "one action per turn"]), ("vLLM 0.33.0", ["Qwen2.5-7B-Instruct,", "log-probabilities, prefix cache"])]),
        ("CREDIT, VERIFICATION, TRAINING", RED, [("credit.py", ["branch points, k branches,", "Monte Carlo values"]), ("verifier.py", ["fresh environment, layered", "checks, progress measure"]), ("trainer.py", ["log-probabilities on the", "context seen; one LoRA step"]), ("metrics.py", ["success by horizon, pass@k,", "shaping, pathologies"])]),
        ("STORES", GREY, [("trajectories/", ["one record per episode,", "every turn inside"]), ("snapshots/", ["tar of the working copy at", "each branch point"]), ("contexts/, logprobs/", ["what the model saw and", "what it generated, per turn"]), ("credit/, curriculum/", ["values, advantages,", "stage results"])]),
    ]
    for r, (title, c, boxes) in enumerate(layers):
        y = 20 + r * 155
        s.zone(20, y, 1380, 140, title, c)
        for i, (t, lines) in enumerate(boxes):
            s.box(40 + i * 340, y + 35, 320, 95, t, lines, c)
    return s.render()


def turn_flow():
    s = Svg(1420, 640, "One turn of an episode, and what is recorded")
    xs = [120, 400, 700, 1000, 1280]
    names = [("Environment pod", BLUE), ("Rollout engine (vLLM)", AMBER), ("Working copy", GREEN), ("Verifier", RED), ("Trajectory store", GREY)]
    for x, (n, c) in zip(xs, names):
        s.box(x - 110, 20, 220, 50, n, [], c)
        s.parts.append(f'<line x1="{x}" y1="70" x2="{x}" y2="620" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5 5"/>')
    steps = [(0, 0, 110, "1  build the context (old observations compacted past 7,000 characters)", "flow"),
             (0, 1, 160, "2  chat completion: one ACTION, token ids and log-probabilities", "flow"),
             (1, 0, 205, "3  reply; generation stops before a second ACTION", "ok"),
             (0, 2, 255, "4  run the tool (ls, cat, replace, write, run_tests, submit)", "flow"),
             (0, 3, 305, "5  progress: Phi on the real files, in a fresh copy", "flow"),
             (0, 0, 355, "6  branch point? uncertain choice, edit, first test run, every third turn", "purple"),
             (0, 4, 405, "7  snapshot (tar) at a branch point", "muted"),
             (0, 4, 455, "8  the context and the log-probabilities of this turn", "muted"),
             (0, 3, 520, "9  after the last turn: fresh-environment verification", "flow"),
             (0, 4, 580, "10 the trajectory: every turn, the outcome, the verifier version", "ok")]
    for a, b, y, label, kind in steps:
        if a == b:
            s.text(xs[a] + 14, y, label, 11.5, COLORS[kind], "700")
        else:
            s.arrow([(xs[a], y), (xs[b], y)], kind, label, (xs[a] + xs[b]) / 2, y - 7)
    return s.render()


def credit():
    s = Svg(1420, 560, "Credit from branches: values at snapshots, advantages for segments")
    s.text(40, 50, "An illustration: a failed trajectory, one bug fixed, then a wrong edit (S05 measures real ones)", 14, INK, "700")
    xs = [80, 260, 440, 620, 800, 980, 1160, 1320]
    for i, x in enumerate(xs):
        s.parts.append(f'<circle cx="{x}" cy="120" r="16" fill="#fff" stroke="#334155" stroke-width="1.6"/>')
        s.text(x, 125, str(i), 12, INK, "700", "middle")
        if i < len(xs) - 1:
            s.arrow([(x + 16, 120), (xs[i + 1] - 16, 120)], "flow")
    for i, (v, lab) in enumerate([(2, "V=0.25"), (4, "V=0.75"), (6, "V=0.00")]):
        x = xs[v]
        s.parts.append(f'<rect x="{x - 40}" y="150" width="80" height="24" rx="5" fill="#f5f3ff" stroke="#7c3aed"/>')
        s.text(x, 167, lab, 12, "#5b21b6", "700", "middle")
        for j in range(4):
            s.arrow([(x, 175), (x - 60 + j * 40, 240)], "purple")
            s.parts.append(f'<circle cx="{x - 60 + j * 40}" cy="250" r="9" fill="{"#059669" if (v == 4 and j < 3) or (v == 2 and j == 0) else "#dc2626"}"/>')
    s.text(40, 300, "k branches from each snapshot, each verified fresh (green = success); V(t) = mean outcome; V(start) = the task's group mean", 12, INK)
    s.box(40, 330, 650, 200, "Turn-level advantages (Monte Carlo)", ["turns 0-2: V(2) - V(start)", "turns 3-4: V(4) - V(2) = +0.50  (the good fix is credited)",
                                                                       "turns 5-6: V(6) - V(4) = -0.75  (the bad edit is blamed)", "turn 7:    outcome - V(6)", "the decisive segment is the largest |change in V|"], PURPLE)
    s.box(730, 330, 650, 200, "Trajectory-level (GRPO-style) advantages", ["every turn: (outcome - group mean) / group std", "the good fix and the bad edit get the same number",
                                                                         "a group whose attempts all fail or all succeed gives zero", "to every turn of every attempt"], GREY)
    return s.render()


def verification():
    s = Svg(1420, 520, "Verification never trusts the agent's environment")
    s.box(40, 60, 300, 170, "The agent's final files", ["whatever it left: fixed code,", "edited tests, a patched", "framework, a huge rewrite"], BLUE)
    s.box(390, 60, 300, 170, "A fresh copy", ["the task's original files", "+ only the files the agent changed", "- its edits to protected paths", "+ the hidden tests"], GREEN)
    s.arrow([(340, 145), (390, 145)], "flow")
    s.box(740, 40, 300, 90, "Required", ["hidden tests, regression tests,", "diff size <= 60, no tampering"], RED)
    s.box(740, 150, 300, 90, "Recorded", ["honest claims, ran the tests", "before submitting"], AMBER)
    s.arrow([(690, 120), (740, 90)], "flow")
    s.arrow([(690, 170), (740, 195)], "flow")
    s.box(1090, 60, 290, 170, "The verdict", ["outcome 1 only if every required", "check passes; verifier version", "on every result; the in-place", "verdict alongside, for comparison"], GREY)
    s.arrow([(1040, 110), (1090, 120)], "flow")
    s.box(40, 280, 1340, 200, "What runs where", ["the verifier pods: Python standard library only, the task bank mounted read-only, NetworkPolicy with no egress at all,",
                                                 "each test run in a child process with CPU, memory and file-size limits; called only from rl-envs and platform",
                                                 "the progress measure Phi uses the same fresh copy and the tamper check, so a patched framework earns no progress either",
                                                 "the hidden tests never leave the verifier: they are not in the bucket and the environments cannot read the ConfigMap"], GREY)
    return s.render()


def queues():
    s = Svg(1420, 500, "Primary rollouts own the environment capacity; branches borrow it")
    s.zone(20, 30, 1380, 200, "COHORT rl-envs", AMBER)
    s.box(40, 70, 420, 140, "ClusterQueue rollouts", ["nominal quota: 6 vCPU, 12 GiB (12 environments)", "reclaimWithinCohort: Any", "priority class primary = 1000"], GREEN)
    s.box(500, 70, 420, 140, "ClusterQueue branches", ["nominal quota: 0; may borrow all of it", "reclaimWithinCohort: Never", "priority class branch-low = 100", "the credit worker submits here"], PURPLE)
    s.box(960, 70, 420, 140, "ResourceFlavor env-cpu", ["nodeLabels ds08/pool=system", "environment pods: 500m CPU, 512 MiB,", "restricted, no token, no identity"], GREY)
    rows = [("t0", "12 branch pods are admitted on borrowed capacity", "purple"), ("t1", "a primary rollout is submitted", "flow"),
            ("t1+", "Kueue preempts the branch job to admit it", "bad"), ("t2", "the primary rollout finishes; the branches are admitted again", "ok")]
    s.text(40, 270, "What S11 does, in time order", 14, INK, "700")
    for i, (t, text, kind) in enumerate(rows):
        y = 305 + i * 40
        s.parts.append(f'<circle cx="60" cy="{y - 4}" r="7" fill="{COLORS[kind]}"/>')
        s.text(80, y, t, 12, COLORS[kind], "700")
        s.text(140, y, text, 12.5, INK)
    return s.render()


def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a09-1", context), ("a09-2", topology), ("a09-3", architecture), ("a09-4", turn_flow), ("a09-5", credit), ("a09-6", verification), ("a09-7", queues)):
        (out / f"{name}.svg").write_text(fn())


if __name__ == "__main__":
    main()

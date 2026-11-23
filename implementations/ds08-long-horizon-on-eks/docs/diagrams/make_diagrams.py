#!/usr/bin/env python3
"""Generates the diagrams of the Design 06 write-up as SVG (the same visual language as the other appendices).

    make_diagrams.py <output-dir>
"""
import html
import pathlib
import sys

FONT = "Helvetica, Arial, sans-serif"
INK, MUTED = "#334155", "#64748b"
GREEN = dict(fill="#ecfdf5", stroke="#059669", ink="#065f46")
RED = dict(fill="#fef2f2", stroke="#dc2626", ink="#991b1b")
BLUE = dict(fill="#eff6ff", stroke="#2563eb", ink="#1e40af")
AMBER = dict(fill="#fffbeb", stroke="#d97706", ink="#92400e")
GREY = dict(fill="#f8fafc", stroke="#94a3b8", ink="#334155")
PURPLE = dict(fill="#f5f3ff", stroke="#7c3aed", ink="#5b21b6")
COLORS = {"ok": "#059669", "bad": "#dc2626", "flow": "#334155", "blue": "#2563eb", "muted": "#94a3b8", "purple": "#7c3aed"}


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.parts, self.title = w, h, [], title

    def esc(self, t):
        return html.escape(str(t), quote=False)

    def zone(self, x, y, w, h, label, c):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{c["fill"]}" fill-opacity=".55" stroke="{c["stroke"]}" stroke-width="1.5" stroke-dasharray="7 5"/>')
        self.parts.append(f'<text x="{x + 14}" y="{y + 24}" font-family="{FONT}" font-size="13" font-weight="700" letter-spacing=".06em" fill="{c["ink"]}">{self.esc(label)}</text>')

    def box(self, x, y, w, h, title, lines=(), c=GREY, num=None, dash=False):
        dash_attr = ' stroke-dasharray="6 4"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#ffffff" stroke="{c["stroke"]}" stroke-width="1.6"{dash_attr}/>')
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="30" rx="10" fill="{c["fill"]}" stroke="{c["stroke"]}" stroke-width="1.6"/>')
        self.parts.append(f'<rect x="{x + 1}" y="{y + 18}" width="{w - 2}" height="12" fill="{c["fill"]}"/>')
        self.parts.append(f'<text x="{x + 12}" y="{y + 20}" font-family="{FONT}" font-size="13.5" font-weight="700" fill="{c["ink"]}">{self.esc(title)}</text>')
        for i, line in enumerate(lines):
            self.parts.append(f'<text x="{x + 12}" y="{y + 48 + i * 17}" font-family="{FONT}" font-size="12" fill="{INK}">{self.esc(line)}</text>')
        if num is not None:
            self.badge(x + w - 4, y + 4, num)

    def badge(self, cx, cy, n):
        self.parts.append(f'<circle cx="{cx}" cy="{cy}" r="12" fill="#1e293b"/><text x="{cx}" y="{cy + 4.5}" text-anchor="middle" font-family="{FONT}" font-size="12" font-weight="700" fill="#fff">{n}</text>')

    def text(self, x, y, t, size=12, color=INK, weight="400", anchor="start"):
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" fill="{color}">{self.esc(t)}</text>')

    def arrow(self, pts, kind="flow", label=None, lx=None, ly=None, dash=False, both=False):
        col = COLORS[kind]
        d = "M" + " L".join(f"{x} {y}" for x, y in pts)
        dash_attr = ' stroke-dasharray="6 4"' if dash else ""
        start = f' marker-start="url(#a-{kind})"' if both else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="1.8"{dash_attr} marker-end="url(#a-{kind})"{start}/>')
        if label:
            mx, my = (lx, ly) if lx is not None else ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2 - 6)
            w = len(label) * 6.2 + 8
            self.parts.append(f'<rect x="{mx - w / 2}" y="{my - 11}" width="{w}" height="16" rx="3" fill="#fff" fill-opacity=".92"/>')
            self.text(mx, my + 1, label, 11, col, "600", "middle")

    def x_mark(self, x, y):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="9" fill="#fff" stroke="#dc2626" stroke-width="2"/><path d="M{x - 4} {y - 4} L{x + 4} {y + 4} M{x + 4} {y - 4} L{x - 4} {y + 4}" stroke="#dc2626" stroke-width="2.2"/>')

    def render(self):
        defs = "".join(
            f'<marker id="a-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{v}"/></marker>'
            for k, v in COLORS.items())
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}px" height="{self.h}px" viewBox="0 0 {self.w} {self.h}" style="background:#ffffff">'
                f'<title>{self.esc(self.title)}</title><defs>{defs}</defs><rect width="{self.w}" height="{self.h}" fill="#ffffff"/>' + "\n".join(self.parts) + "</svg>")


# ------------------------------------------------------------------------------------------------------------------
def context():
    s = Svg(1420, 560, "Who uses the platform, what it is made of, and what it talks to")
    s.zone(20, 30, 270, 500, "WHO", GREEN)
    s.box(40, 70, 230, 120, "Researcher", ["writes a recipe file,", "or an ablation of one;", "launches, compares runs"], GREEN)
    s.box(40, 210, 230, 110, "Reviewer of recipes", ["reviews recipe changes in", "Git like code"], GREEN)
    s.box(40, 340, 230, 160, "Release owner", ["reads the evaluation of a", "candidate; the gate promotes", "it or blocks it with reasons"], GREEN)
    s.zone(320, 30, 760, 500, "THE PLATFORM (standard EKS)", AMBER)
    s.box(340, 70, 220, 110, "Recipes + launcher", ["recipes/*.yaml in Git", "ptl.py: hash, diff, resolve,", "refuse bad ablations, submit"], BLUE)
    s.box(590, 70, 470, 110, "Admission: Kueue + JobSet", ["two queues in one cohort: production owns the GPUs,", "ablation borrows them; an RL job (rollout + trainer)", "is one Workload, admitted all or nothing"], AMBER)
    s.box(340, 200, 220, 120, "Run jobs (GPU)", ["SFT, RL (GRPO), on-policy", "distillation, filtering,", "teacher traces, evaluation"], BLUE)
    s.box(590, 200, 220, 120, "Reward verifiers (CPU)", ["math and code checks;", "runs model-written code in", "a process sandbox; no network"], RED)
    s.box(840, 200, 220, 120, "Health monitor", ["reads every run's metrics", "from the tracking store; trend", "alerts before a collapse"], PURPLE)
    s.box(340, 340, 720, 110, "Stores (one S3 bucket)", ["datasets/   versioned, with content hashes",
                                                      "runs/   the tracking store: resolved recipe, status, metrics, events, checkpoints",
                                                      "registry/   every stage's model and its entry (recipe hash, base, data, gate verdict)",
                                                      "evals/   the evaluation suites' results, one per model"], GREY)
    s.box(340, 465, 720, 50, "Every run is described by one immutable record before it starts: runs/<run_id>/resolved.json", [], GREY, dash=True)
    s.zone(1110, 30, 290, 500, "OUTSIDE THE PLATFORM", RED)
    s.box(1130, 70, 250, 120, "Hugging Face Hub", ["Qwen2.5-0.5B-Instruct (student)", "Qwen2.5-3B-Instruct (teacher)", "pinned to commits"], GREY)
    s.box(1130, 210, 250, 110, "Docker Hub, PyPI", ["vLLM 0.33.0 image (digest),", "TRL 1.18.2, datasets 5.2.0"], GREY)
    s.box(1130, 340, 250, 110, "Git", ["the recipe's commit is", "recorded with every run"], GREY)
    s.arrow([(270, 130), (340, 125)], "flow")
    s.arrow([(270, 265), (300, 265), (300, 150), (340, 140)], "muted")
    s.arrow([(270, 420), (310, 420), (310, 400), (340, 400)], "flow", "gate", 300, 412)
    s.arrow([(560, 125), (590, 125)], "flow")
    s.arrow([(700, 180), (700, 190), (450, 190), (450, 200)], "flow")
    s.arrow([(560, 260), (590, 260)], "flow", "score", 575, 252)
    s.arrow([(450, 320), (450, 340)], "flow")
    s.arrow([(950, 320), (950, 340)], "purple")
    s.arrow([(1060, 130), (1130, 130)], "muted")
    s.arrow([(1060, 390), (1130, 390)], "muted")
    return s.render()


def topology():
    s = Svg(1420, 700, "One VPC in us-east-2, three zones, a CPU node group and a GPU node group")
    s.zone(20, 20, 1380, 660, "AWS ACCOUNT, us-east-2, ONE VPC 10.61.0.0/16 (Design 05 uses us-east-1 and eu-west-1)", GREY)
    s.box(50, 70, 340, 150, "Standard EKS cluster (not Auto Mode)", ["ds06-posttrain, Kubernetes 1.37", "API public only to the administrator's /32", "add-ons: VPC CNI (network policy on),", "kube-proxy, CoreDNS, Pod Identity, metrics-server"], BLUE)
    s.box(50, 240, 340, 150, "Node group ds06-posttrain-system", ["2 x m6i.xlarge (4 vCPU, 16 GiB), on demand", "AL2023 standard AMI 1.37.1-20261119", "Kueue, JobSet, verifiers, health monitor"], GREEN)
    s.box(50, 410, 340, 170, "Node group ds06-posttrain-gpu", ["2 nodes, 1 GPU each, on demand, 200 GB disk", "types in order: g6.2xlarge (L4), g6e.2xlarge", "(L40S), g5.2xlarge (A10G)", "AL2023 NVIDIA AMI 1.37.1-20261119 (pinned)", "taint nvidia.com/gpu: only Kueue-admitted pods"], AMBER)
    s.box(50, 600, 340, 60, "Terraform, two layers, local git-ignored state", ["10-cluster, 20-platform; account guard"], GREY)
    for i, z in enumerate("abc"):
        x = 420 + i * 320
        s.zone(x, 60, 300, 420, f"us-east-2{z}", GREY)
        s.box(x + 10, 100, 280, 70, "Private subnet /20", ["nodes; egress through the NAT gateway", "S3 through a gateway endpoint"], GREEN)
        s.box(x + 10, 190, 280, 80, "Public subnet /24", ["NAT gateway (zone a only)" if z == "a" else "(load balancers, unused)"], GREY)
        s.box(x + 10, 290, 280, 170, "Nodes placed by the groups", ["both groups span all three zones;",
                                                                    "a GPU node lands wherever EC2", "has capacity, so a refusal in one", "zone is not fatal (S01 lists where", "each node of the final run was)"], AMBER)
    s.box(420, 500, 940, 80, "S3 bucket ds06-posttrain-data-<random>", ["datasets/  runs/  registry/  evals/   — public access blocked; each workload identity limited to its prefixes"], GREY)
    s.box(420, 600, 940, 60, "One Elastic IP (the NAT gateway) of the region's five; one VPC of five", [], GREY, dash=True)
    return s.render()


def architecture():
    s = Svg(1420, 800, "Design 06 on EKS: the five layers")
    layers = [
        ("RECIPES AND LAUNCH", GREEN, [("Recipe files (Git)", ["one YAML file per stage;", "ablations declare a parent", "and one variable"]), ("ptl.py launch", ["hash, resolve every reference,", "refuse a bad ablation, write", "resolved.json, create the job"]), ("ptl.py compare", ["the same metrics and suites", "for several runs, side by side"]), ("ptl.py gate", ["thresholds and regression", "margins from the recipe"])]),
        ("ADMISSION", AMBER, [("Kueue 0.20.7", ["queues production, ablation", "cohort post-training;", "priorities 1000 and 100"]), ("ResourceFlavor gpu", ["node label ds06/pool=gpu,", "toleration of the GPU taint"]), ("JobSet 0.13.1", ["an RL run: rollout + trainer,", "stable DNS names, success", "when the trainer succeeds"]), ("NVIDIA device plugin", ["0.20.2 on the GPU pool;", "advertises nvidia.com/gpu"])]),
        ("STAGES (vLLM 0.33.0 image + TRL 1.18.2)", BLUE, [("SFT", ["SFTTrainer: fixed-size mixture,", "assistant-only loss, packing"]), ("RL", ["GRPOTrainer (DAPO loss, clip-", "higher, no KL) + vLLM server"]), ("Distillation", ["DistillationTrainer, reverse", "KL; or SFT on teacher traces"]), ("Generation jobs", ["vLLM offline: pass-rate filter,", "teacher traces, evaluation"])]),
        ("REWARDS AND HEALTH", RED, [("Verifier pool", ["math_exact, math_any, format,", "code (process sandbox)"]), ("Isolation", ["NetworkPolicy: no egress;", "restricted Pod Security; no", "token; rlimits per candidate"]), ("Autoscaling", ["HPA 2 to 6 pods on CPU", "(metrics-server)"]), ("Health monitor", ["trend rules over metrics.jsonl;", "alerts.json per run"])]),
        ("STORES", GREY, [("datasets/", ["built-in versions + generated", "write-once versions"]), ("runs/", ["resolved.json, status, metrics,", "events, checkpoints, alerts"]), ("registry/", ["model + entry.json per stage", "candidate / promoted / blocked"]), ("evals/", ["one result per model, the", "gate's only input"])]),
    ]
    for r, (title, c, boxes) in enumerate(layers):
        y = 20 + r * 155
        s.zone(20, y, 1380, 140, title, c)
        for i, (t, lines) in enumerate(boxes):
            s.box(40 + i * 340, y + 35, 320, 95, t, lines, c)
    return s.render()


def rl_job():
    s = Svg(1420, 700, "One RL step: weights to the rollout server, rollouts back, rewards from the pool")
    xs = [120, 400, 700, 1000, 1280]
    names = [("Trainer pod (GPU 1)", BLUE), ("Rollout pod: vLLM (GPU 2)", AMBER), ("Verifier pool", RED), ("Tracking store (S3)", GREY), ("Health monitor", PURPLE)]
    for x, (n, c) in zip(xs, names):
        s.box(x - 110, 20, 220, 50, n, [], c)
        s.parts.append(f'<line x1="{x}" y1="70" x2="{x}" y2="680" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5 5"/>')
    steps = [
        (0, 1, 110, "1  sync weights over NCCL (timed)", "blue"),
        (0, 1, 160, "2  generate: 8 prompts x 8 answers", "flow"),
        (1, 0, 205, "3  64 completions + their log-probs", "ok"),
        (0, 2, 255, "4  score: math_exact, unit_tests, format", "flow"),
        (2, 0, 300, "5  one score per completion (None if n/a)", "ok"),
        (0, 0, 345, "6  advantages within each group, DAPO loss, IS correction", "flow"),
        (0, 3, 395, "7  metrics row: reward, entropy, length, clip, IS ratios", "muted"),
        (4, 3, 445, "8  every 15 s: read metrics.jsonl", "purple"),
        (4, 3, 495, "9  write alerts.json when a trend rule fires", "purple"),
        (0, 3, 545, "10 every 20 steps: checkpoint, keep the last two", "muted"),
        (0, 3, 600, "11 end: register the model; status succeeded", "ok"),
        (1, 3, 650, "12 rollout pod sees the trainer's status and exits", "muted"),
    ]
    for a, b, y, label, kind in steps:
        if a == b:
            s.text(xs[a] + 14, y, label, 11.5, COLORS[kind], "700")
        else:
            s.arrow([(xs[a], y), (xs[b], y)], kind, label, (xs[a] + xs[b]) / 2, y - 7)
    return s.render()


def pipeline():
    s = Svg(1420, 620, "The recipe as built: stages, gates and the registry")
    s.box(20, 60, 230, 110, "Base model", ["Qwen2.5-0.5B-Instruct", "hf://…@7ae5576 (pinned)"], GREY)
    s.box(290, 60, 230, 110, "SFT (sft.yaml)", ["mixture of 4 sources,", "assistant-only loss, packing"], BLUE, 1)
    s.box(560, 60, 230, 110, "Gate", ["style, step leakage, refusals,", "over-refusal; no regression", "vs the base model"], AMBER)
    s.box(830, 60, 230, 110, "Filter (filter.yaml)", ["pass rate of each RL prompt", "with the SFT model; keep 5-95%"], BLUE, 2)
    s.box(1100, 60, 290, 110, "RL (rl.yaml)", ["GRPO from sft@promoted on", "the filtered prompts; rollout", "and trainer on two GPUs"], BLUE, 3)
    for x in (250, 520, 790, 1060):
        s.arrow([(x, 115), (x + 40, 115)], "flow")
    s.box(1100, 200, 290, 90, "Gate", ["refusals; no regression vs", "the promoted SFT model"], AMBER)
    s.arrow([(1245, 170), (1245, 200)], "flow")
    s.box(290, 230, 230, 110, "Ablation (one key)", ["sft-reasoning-heavy:", "reasoning weight 0.25 -> 1.5"], PURPLE)
    s.arrow([(405, 170), (405, 230)], "purple", "parent", 430, 200)
    s.box(830, 230, 230, 110, "Ablations of RL", ["rl-unfiltered: math prompts", "rl-high-lr: lr 2e-6 -> 5e-5"], PURPLE)
    s.arrow([(1100, 140), (1080, 140), (1080, 260), (1060, 260)], "purple")
    s.box(20, 380, 230, 110, "Teacher", ["Qwen2.5-3B-Instruct", "hf://…@aa8e725 (pinned)"], GREY)
    s.box(290, 380, 230, 110, "Traces (traces.yaml)", ["4 answers per prompt, keep", "the first verified one"], BLUE, 4)
    s.box(560, 380, 230, 110, "Off-policy distillation", ["SFT of the student on", "the teacher's traces"], BLUE, 5)
    s.box(560, 510, 230, 90, "On-policy distillation", ["student writes, teacher", "scores each token (reverse KL)"], BLUE, 6)
    s.arrow([(250, 435), (290, 435)], "flow")
    s.arrow([(520, 435), (560, 435)], "flow")
    s.arrow([(135, 490), (135, 555), (560, 555)], "flow", "teacher", 330, 548)
    s.box(830, 380, 560, 220, "Registry and evaluation", ["registry/assistant-v1/<stage>/<run_id>/: the model and entry.json",
                                                         "entry.json: recipe hash, Git commit, base (reference and what it",
                                                         "resolved to), every dataset's content hash, image digest, code hash",
                                                         "status: candidate -> promoted or blocked (with every check)",
                                                         "evals/by-model/<run_id>.json: math, code, casual, harmful,",
                                                         "safe-scary suites, greedy, the same for every model",
                                                         "registry://assistant-v1/sft@promoted resolves to the newest",
                                                         "promoted SFT entry at launch, and is recorded as such"], GREY)
    return s.render()


def queues():
    s = Svg(1420, 520, "Production owns the GPUs; ablations borrow them and give them back")
    s.zone(20, 30, 1380, 200, "COHORT post-training (2 GPUs in the pool)", AMBER)
    s.box(40, 70, 420, 140, "ClusterQueue production", ["nominal quota: 2 GPUs, 12 vCPU, 44 GiB", "reclaimWithinCohort: Any", "priority class production = 1000", "never preempted: nobody can reclaim from it"], GREEN)
    s.box(500, 70, 420, 140, "ClusterQueue ablation", ["nominal quota: 0; may borrow up to 2 GPUs", "reclaimWithinCohort: Never", "priority class ablation = 100", "the launcher puts every ablation here"], PURPLE)
    s.box(960, 70, 420, 140, "ResourceFlavor gpu", ["nodeLabels ds06/pool=gpu", "tolerates nvidia.com/gpu:NoSchedule", "a pod can reach a GPU node only", "through an admitted Workload"], GREY)
    s.text(40, 270, "What S06 does, in time order", 14, INK, "700")
    rows = [("t0", "rl-unfiltered (ablation, 2 GPUs) is admitted on borrowed quota and both its pods run", "purple"),
            ("t1", "rl (production, 2 GPUs) is launched; production's own quota is in use by the borrower", "flow"),
            ("t1+", "Kueue preempts the borrower: its Workload is evicted, the JobSet suspended, both pods deleted", "bad"),
            ("t1++", "production is admitted on its nominal quota; rollout and trainer start together", "ok"),
            ("t2", "production finishes; the GPUs are idle again; the ablation is re-admitted and restarts from step 0", "purple")]
    for i, (t, text, kind) in enumerate(rows):
        y = 305 + i * 40
        s.parts.append(f'<circle cx="60" cy="{y - 4}" r="7" fill="{COLORS[kind]}"/>')
        s.text(80, y, t, 12, COLORS[kind], "700")
        s.text(140, y, text, 12.5, INK)
    return s.render()


def sandbox():
    s = Svg(1420, 560, "What a reward that runs model-written code is allowed to do: nothing but compute")
    s.zone(20, 30, 1380, 500, "NAMESPACE rewards (Pod Security: restricted)", RED)
    s.box(40, 70, 420, 200, "Verifier pod", ["non-root (uid 10001), read-only root filesystem,", "no capabilities, seccomp RuntimeDefault,", "no service-account token, no AWS identity,", "CPU limit 2, memory limit 2 GiB", "Deployment 2..6 replicas (HPA on CPU)"], RED)
    s.box(500, 70, 420, 200, "One child process per completion", ["python -I -S: no site, no environment", "empty working directory, deleted afterwards", "RLIMIT_CPU 5 s, RLIMIT_AS 512 MiB,", "RLIMIT_FSIZE 1 MiB, 32 file descriptors", "wall clock 4 s, then its process group is killed"], AMBER)
    s.box(960, 70, 420, 200, "What the child gets and what it returns", ["gets: the code and the test ARGUMENTS only,", "and a random per-run marker", "returns: one line, marker + results", "the expected outputs never leave the verifier,", "which does the comparison"], GREEN)
    s.arrow([(460, 170), (500, 170)], "flow")
    s.arrow([(920, 170), (960, 170)], "flow")
    s.box(40, 300, 640, 200, "NetworkPolicy verifier-isolation", ["egress: none (not even DNS)", "ingress: only from namespaces post-training and platform, TCP 8080", "enforced by the VPC CNI's network policy agent",
                                                                 "readiness is an exec probe, so the policy does not have to", "admit the kubelet"], GREY)
    s.box(720, 300, 660, 200, "Not built (see section 6)", ["a VM-level sandbox per candidate (gVisor, Firecracker):", "the child shares the pod's kernel and user",
                                                          "a pid limit: a candidate could fork and outlive its timeout", "by leaving its process group; the CPU limit still ends it",
                                                          "per-language runtimes: only Python candidates"], GREY, dash=True)
    return s.render()


def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a07-1", context), ("a07-2", topology), ("a07-3", architecture), ("a07-4", rl_job), ("a07-5", pipeline), ("a07-6", queues), ("a07-7", sandbox)):
        (out / f"{name}.svg").write_text(fn())


if __name__ == "__main__":
    main()

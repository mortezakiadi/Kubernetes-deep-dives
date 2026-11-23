#!/usr/bin/env python3
"""Generates the architecture diagrams for the Design 07 write-up as SVG (same visual language as the design's own pictures).

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
COLORS = {"ok": "#059669", "bad": "#dc2626", "flow": "#334155", "blue": "#2563eb", "muted": "#94a3b8"}


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.parts = w, h, []
        self.title = title

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

# ------------------------------------------------------------------------------------------------
def context():
    s = Svg(1420, 540, "Who uses the platform and what it runs")
    s.zone(20, 30, 270, 480, "WHO", GREEN)
    s.box(40, 70, 230, 110, "Research team", ["chooses the recipe and the", "reward; reads the curves and", "decides when to climb a rung"], GREEN)
    s.box(40, 200, 230, 110, "Platform team", ["owns the cluster, the GPU", "pools, the queues and the", "restart policy"], GREEN)
    s.box(40, 330, 230, 120, "On-call engineer", ["gets the alert when a run", "drifts or stalls; rolls back", "to the last healthy checkpoint"], GREEN)
    s.zone(320, 30, 1080, 480, "WHAT RUNS (ONE ZONE, ONE CLUSTER)", AMBER)
    s.box(340, 70, 320, 120, "Trainer", ["one GPU, asynchronous GRPO", "reads samples from a queue,", "updates the policy, pushes weights"], BLUE)
    s.box(700, 70, 320, 120, "Rollout fleet", ["two GPUs on two nodes, one vLLM", "engine each, behind one address;", "generates answers, returns log-probs"], BLUE)
    s.box(1060, 70, 320, 120, "Environment tier", ["sandbox workers on CPU nodes", "reading calls from a queue;", "scaled by the queue's length"], BLUE)
    s.box(340, 230, 320, 120, "State bucket", ["checkpoints and the run's metrics;", "the copy that survives the node"], GREY)
    s.box(700, 230, 320, 120, "Restart policy", ["JobSet recreates the trainer,", "a watchdog turns a hang into", "a failure"], GREY)
    s.box(1060, 230, 320, 120, "Measurements", ["step-time stages, mismatch,", "queue, staleness, held-out score", "- on every run, every step"], GREY)
    s.arrow([(270, 125), (340, 125)], "flow", "recipe")
    s.arrow([(270, 255), (340, 290)], "flow", "operate")
    s.arrow([(270, 390), (340, 310)], "flow", "alert")
    s.text(340, 400, "The question this platform answers: how do we take a recipe that works at 0.6B parameters and short answers", 13, INK, "700")
    s.text(340, 420, "and find out what breaks, in what order, before it is run at hundreds of times the size?", 13, INK, "700")
    s.text(340, 450, "Scaled down here to 3 GPUs and 20 to 80 steps per run. The design's 235B mixture-of-experts model, 64K-token answers and 2,048 GPUs", 12, MUTED)
    s.text(340, 468, "are not reachable; the architecture and the measurements are.", 12, MUTED)
    return s.render()


def architecture():
    s = Svg(1420, 760, "The cluster: pools, workloads and the paths between them")
    s.zone(20, 30, 1380, 710, "EKS 1.37, ONE VPC, GPU NODES IN ONE ZONE (us-east-1a)", GREY)
    s.zone(40, 70, 330, 300, "SYSTEM POOL: 2 x m5.xlarge", GREEN)
    s.box(55, 105, 300, 70, "Controllers", ["JobSet, LeaderWorkerSet, KEDA,", "NVIDIA device plugin"], GREEN)
    s.box(55, 190, 300, 70, "Redis", ["the sandbox queue and its results"], GREEN)
    s.box(55, 275, 300, 70, "Experiment tools", ["iperf3, probes, one-off jobs"], GREEN)
    s.zone(40, 390, 330, 330, "ENV POOL: 2 x c6i.2xlarge", AMBER)
    s.box(55, 425, 300, 130, "Sandbox workers", ["2 to 40 pods, 250m CPU each", "pop a call, run the code with", "a time and memory limit, answer", "KEDA scales them on queue length"], AMBER)
    s.zone(400, 70, 400, 650, "TRAINER POOL: 1 x g5.2xlarge first choice (A10G, 24 GB)", BLUE)
    s.box(420, 110, 360, 150, "Trainer pod (JobSet)", ["TRL AsyncGRPOTrainer, bf16 compute,", "fp32 master weights", "background rollout worker + queue", "held-out evaluation every N steps"], BLUE)
    s.box(420, 290, 360, 120, "Checkpoint writer", ["node disk first (every N steps),", "a thread copies it to the bucket", "and marks it COMPLETE last"], BLUE)
    s.box(420, 440, 360, 120, "Progress watchdog", ["no new step for 420 s: exit,", "so that the restart policy acts", "(NCCL has no timeout)"], BLUE)
    s.zone(830, 70, 560, 650, "ROLLOUT POOL: 2 x g5.2xlarge (A10G, 24 GB)", BLUE)
    s.box(890, 110, 240, 200, "rollout-0 (leader)", ["vLLM 0.33, data-parallel", "engine 0 + the API server", "70% of GPU memory", "(2 GiB headroom for", "the weight receive)"], BLUE)
    s.box(1150, 110, 230, 200, "rollout-0-1 (worker)", ["vLLM, headless,", "data-parallel engine 1", "joins the leader over", "the node network"], BLUE)
    s.box(850, 340, 530, 80, "LeaderWorkerSet 'rollout', group size 2", ["RecreateGroupOnPodRestart: one engine lost, the group is rebuilt"], GREY)
    s.arrow([(780, 160), (890, 160)], "blue", "HTTP control", lx=835, ly=150, both=True)
    s.arrow([(780, 250), (890, 250)], "blue", "NCCL data", lx=835, ly=240)
    s.arrow([(355, 490), (400, 250)], "flow", "reward calls", lx=380, ly=380)
    s.box(850, 460, 530, 100, "State bucket (S3, through the gateway endpoint)", ["runs/<run>/metrics.jsonl and checkpoint-N/", "restored by a new trainer pod after any failure"], GREY)
    s.arrow([(780, 350), (815, 350), (815, 510), (850, 510)], "ok", "checkpoints", lx=815, ly=430)
    s.text(850, 600, "The network between the trainer's node and the rollout nodes is the VPC's node network,", 12, MUTED)
    s.text(850, 618, "measured with iperf3 in S01. There is no GPU fabric between nodes (no EFA, no NVLink):", 12, MUTED)
    s.text(850, 636, "that is why the weight push in S05 takes the time it does.", 12, MUTED)
    return s.render()


def step_timeline():
    s = Svg(1420, 470, "One asynchronous step: what overlaps and what does not")
    s.text(40, 40, "Rollout worker (inside the trainer pod) streams groups of answers from the fleet into a queue; the trainer takes four groups a step.", 13, INK, "700")
    x0, scale = 220, 150  # seconds to pixels
    rows = [("rollout fleet (2 GPUs)", 100), ("queue", 170), ("trainer (1 GPU)", 240), ("network", 310)]
    for name, y in rows:
        s.text(40, y + 20, name, 12.5, INK, "700")
        s.parts.append(f'<line x1="{x0}" y1="{y + 28}" x2="1380" y2="{y + 28}" stroke="#e2e8f0"/>')
    def bar(y, t0, t1, label, c):
        x, w = x0 + t0 * scale, (t1 - t0) * scale
        s.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="5" fill="{c["fill"]}" stroke="{c["stroke"]}" stroke-width="1.4"/>')
        s.text(x + 6, y + 17, label, 11, c["ink"], "600")
    bar(100, 0.0, 1.9, "generating answers (step n+1, n+2 ...)", BLUE)
    bar(100, 1.9, 3.9, "PAUSED for the weight load", RED)
    bar(100, 3.9, 7.2, "generating (now with the new weights)", BLUE)
    bar(170, 0.0, 7.2, "samples wait here; each is stamped with the policy version that made it", GREY)
    bar(240, 0.0, 1.6, "backward (1.5 s)", GREEN)
    bar(240, 1.6, 1.95, "log-probs", GREEN)
    bar(240, 1.95, 3.9, "send weights (2.0 s)", RED)
    bar(240, 3.9, 5.5, "backward", GREEN)
    bar(240, 5.5, 5.85, "log-probs", GREEN)
    bar(310, 1.95, 3.9, "NCCL broadcast, 1.2 GB x2 engines", AMBER)
    s.text(40, 400, "From S03: the trainer's own timers give about 2.0 s sending weights against 1.5 s of backward and 0.4 s of log-probabilities.", 12, INK)
    s.text(40, 420, "While the weights load, the rollout engines generate nothing: on this network the push is half of every step. A push every fourth step, or a faster network, is the lever.", 12, INK)
    s.text(40, 440, "The cost of pushing less often is that samples are older when the trainer uses them: the staleness bound drops the oldest.", 12, MUTED)
    return s.render()


def ladder_picture():
    s = Svg(1420, 440, "The scaling ladder as built")
    xs = [40, 380, 720, 1060]
    heads = [("Rung 1: recipe", BLUE, ["0.6B model, short answers,", "80 steps, three recipes", "fit the compute-performance curve"]),
             ("Rung 2: length", BLUE, ["same model and recipe,", "answers up to 3072 tokens", "thinking mode; the tail grows"]),
             ("Rung 3: model size", GREY, ["1.7B in the overlay; not trained", "here (24 GB trainer GPU).", "S05 measures what it exposes first"]),
             ("Production run", GREY, ["the design's full budget:", "not reachable here; the", "forecast comes from rung 4"])]
    for x, (t, c, lines) in zip(xs, heads):
        s.box(x, 70, 300, 120, t, lines, c)
    for a, b in zip(xs, xs[1:]):
        s.arrow([(a + 300, 130), (b, 130)], "flow", "gate", lx=(a + 300 + b) / 2, ly=112)
    s.box(40, 230, 640, 150, "The gate (ladder.py): three criteria, each from a measurement", ["fitted ceiling known to within 0.03 (S02)", "unexplained waiting under 10% of the step (S03)", "a restart from a checkpoint has been done (S09)", "all three, or the rung is held"], AMBER)
    s.box(720, 230, 640, 150, "One axis per rung (overlay files)", ["recipes/ladder/rung2-length.yaml extends rung 1 and may set only", "MAX_COMPLETION and THINKING. An overlay that also changes the model", "is refused before anything is scheduled (S10).", "Each overlay carries its own GPU-hour budget."], GREY)
    return s.render()


def recovery_picture():
    s = Svg(1420, 440, "Recovery: what survives each failure")
    s.zone(20, 30, 1380, 380, "THE THREE FAILURES OF S09", GREY)
    s.box(40, 80, 430, 150, "Rollout engine lost", ["the group is rebuilt (all-or-nothing);", "the NCCL group is broken and the", "trainer stops; JobSet starts a new", "one, which forms the group again"], RED)
    s.box(495, 80, 430, 150, "Trainer pod lost", ["JobSet recreates the pod; a guard checks", "the fleet answers (and rebuilds it if a", "push was cut off); the pod restores the", "newest checkpoint and re-forms the group"], RED)
    s.box(950, 80, 430, 150, "Trainer node lost", ["the node and its disk are gone; the", "node group launches a node, the image", "is pulled again, the checkpoint comes", "from the bucket"], RED)
    s.box(40, 260, 1340, 120, "What bounds the loss", ["Work since the last checkpoint is repeated: the checkpoint interval (every 10 steps here) is the bound on it.", "Time to recover is the sum of the pod's start, the packages, the model and the checkpoint, and the NCCL handshake: measured in S09.", "Goodput is the share of the reserved GPU time that went into steps that counted."], GREEN)
    return s.render()


def topology():
    s = Svg(1420, 420, "Instances")
    rows = [("m5.xlarge", "2", "system pool", "controllers, KEDA, Redis, tools", "4 vCPU, 16 GiB", "$0.19 / h each"),
            ("c6i.2xlarge", "2", "env pool", "sandbox workers (2 to 40 pods)", "8 vCPU, 16 GiB, 110 pods each", "$0.34 / h each"),
            ("g5.2xlarge", "1", "trainer pool", "trainer pod, one A10G (24 GB)", "8 vCPU, 32 GiB, 250 GB disk", "$1.21 / h"),
            ("g5.2xlarge", "2", "rollout pool", "vLLM engines, one A10G each", "8 vCPU, 32 GiB, 250 GB disk", "$1.21 / h each"),
            ("NAT gateway", "1", "network", "image pulls, model downloads", "one zone", "$0.045 / h + data"),
            ("S3 bucket", "1", "state", "checkpoints, metrics", "through the free gateway endpoint", "storage only")]
    heads = ["instance", "count", "pool", "what runs on it", "size", "price"]
    xs = [40, 220, 300, 430, 800, 1180]
    s.parts.append('<rect x="20" y="20" width="1380" height="380" rx="10" fill="#fff" stroke="#cbd5e1"/>')
    for x, h in zip(xs, heads):
        s.text(x, 55, h, 13, INK, "700")
    for i, r in enumerate(rows):
        y = 90 + i * 48
        s.parts.append(f'<line x1="30" y1="{y - 22}" x2="1390" y2="{y - 22}" stroke="#e2e8f0"/>')
        for x, v in zip(xs, r):
            s.text(x, y, v, 12.5, INK)
    s.text(40, 390, "Total while idle: about $5.8 an hour (prices are the on-demand list prices in us-east-1 when this was built).", 12, MUTED)
    return s.render()


def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate([context, architecture, step_timeline, ladder_picture, recovery_picture, topology], 1):
        (out / f"a08-{i}.svg").write_text(fn())
        print("wrote", out / f"a08-{i}.svg")


if __name__ == "__main__":
    main()

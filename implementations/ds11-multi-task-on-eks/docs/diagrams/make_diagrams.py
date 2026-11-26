#!/usr/bin/env python3
"""Generates the architecture diagrams for the Design 11 write-up as SVG (same visual language as the design's own pictures).

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
    s = Svg(1420, 560, "Who uses the platform and what it is made of")
    s.zone(20, 30, 270, 500, "WHO", GREEN)
    s.box(40, 70, 230, 110, "Domain teams", ["each owns a family: its data,", "its verifier, its eval suite", "and its margin"], GREEN)
    s.box(40, 200, 230, 110, "Mixture team", ["owns the weights, the order", "and the release gate; asks", "the matrix, not the meeting"], GREEN)
    s.box(40, 330, 230, 120, "Platform team", ["owns the cluster, the research", "queue, and the pipelines that", "train, merge and evaluate"], GREEN)
    s.zone(320, 30, 1080, 500, "WHAT IT IS MADE OF", AMBER)
    s.box(340, 70, 320, 130, "Domain registry", ["six families (and a seventh to", "admit): owner, data version,", "verifier, held-out split, margin;", "verifier precision is measured"], BLUE)
    s.box(700, 70, 320, 130, "Mixture service", ["weights, floors, token balance,", "stages and refreshes; proxy runs,", "a response model, a search that", "maximises the weakest family"], BLUE)
    s.box(1060, 70, 320, 130, "Training runs", ["GRPO, one GPU per run, vLLM", "in the process; per-family reward,", "signal and evaluation recorded", "for every batch"], BLUE)
    s.box(340, 240, 320, 130, "Capability matrix", ["boost one family, score all:", "transfer and interference, with", "seed noise; a gradient probe per", "module as a second instrument"], GREY)
    s.box(700, 240, 320, 130, "Combination", ["specialists trained apart, merged", "by task arithmetic with trimming", "and sign agreement; one workflow", "of Kueue-managed jobs"], GREY)
    s.box(1060, 240, 320, 130, "Release gate", ["no family may fall below the", "current release by more than its", "margin plus twice its noise;", "mean, minimum, geometric mean"], GREY)
    s.arrow([(270, 125), (340, 125)], "flow", "contract")
    s.arrow([(270, 255), (700, 150)], "flow", "weights", lx=500, ly=190)
    s.arrow([(270, 390), (700, 330)], "flow", "pipeline", lx=480, ly=372)
    s.text(340, 420, "The question this platform answers: how do we cover many task families with one assistant, and know that a release is better for all of them,", 13, INK, "700")
    s.text(340, 440, "not only on average?", 13, INK, "700")
    s.text(340, 470, "Scaled down to a 0.6B model, six small families with programmatic verifiers (and a seventh to admit), runs of 40 to 120 steps on one GPU each.", 12, MUTED)
    s.text(340, 488, "A chat family with a judge, long agentic tasks and distillation are not reachable here; the matrix, the mixture search, the order, the merge and the gate are.", 12, MUTED)
    return s.render()
def topology():
    s = Svg(1420, 470, "Instances")
    rows = [("m5.xlarge", "2", "system pool", "Kueue, Argo Workflows, device plugin, CoreDNS", "4 vCPU, 16 GiB", "$0.19 / h each"),
            ("g6e.2xlarge", "up to 4", "research pool", "one run per node, one L40S (48 GB) each", "8 vCPU, 64 GiB, 200 GB disk", "$2.24 / h each"),
            ("g6.2xlarge", "fallback", "research pool", "one NVIDIA L4 (24 GB) each; the same pool and taint", "8 vCPU, 32 GiB, 200 GB disk", "$0.98 / h each"),
            ("g5.2xlarge, g5.4xlarge", "fallback", "research pool", "one A10G (24 GB); the pool lists all four types", "8 to 16 vCPU", "$1.21 to $1.62 / h"),
            ("NAT gateway", "1", "network", "image pulls, model and dataset downloads", "one zone", "$0.045 / h + data"),
            ("S3 bucket", "1", "state", "each run's metrics, and the specialists' and merged weights", "through the free gateway endpoint", "storage only")]
    heads = ["instance", "count", "pool", "what runs on it", "size", "price"]
    xs = [40, 290, 380, 520, 920, 1210]
    s.parts.append('<rect x="20" y="20" width="1380" height="430" rx="10" fill="#fff" stroke="#cbd5e1"/>')
    for x, h in zip(xs, heads):
        s.text(x, 55, h, 13, INK, "700")
    for i, r in enumerate(rows):
        y = 90 + i * 50
        s.parts.append(f'<line x1="30" y1="{y - 22}" x2="1390" y2="{y - 22}" stroke="#e2e8f0"/>')
        for x, v in zip(xs, r):
            s.text(x, y, v, 12.5, INK)
    s.text(40, 410, "GPU capacity on demand is volatile: the pool starts empty and is scaled up, and lists four single-GPU types and three zones so that a node that cannot be launched as one type is launched as another.", 12, MUTED)
    s.text(40, 430, "Every run lays out its memory the same way whatever the card: the sampling engine gets a fixed 8 GiB. Prices are on-demand list prices in us-east-1.", 12, MUTED)
    return s.render()
def architecture():
    s = Svg(1420, 760, "The cluster and the pipeline")
    s.zone(20, 30, 1380, 710, "EKS 1.37, ONE VPC, THREE ZONES", GREY)
    s.zone(40, 70, 330, 650, "SYSTEM POOL: 2 x m5.xlarge", GREEN)
    s.box(55, 105, 300, 100, "Kueue", ["ClusterQueue 'research': 4 GPUs", "priorities: sweep-low, interactive", "a job is admitted as a whole"], GREEN)
    s.box(55, 230, 300, 100, "Argo Workflows", ["orders the steps of a pipeline;", "each step creates one Kueue-", "managed Job and waits for it"], GREEN)
    s.box(55, 355, 300, 70, "NVIDIA device plugin", ["advertises nvidia.com/gpu"], GREEN)
    s.box(55, 450, 300, 100, "State bucket (S3)", ["runs/<run>/metrics.jsonl", "runs/<run>/final/ (weights of", "specialists and merged models)"], GREY)
    s.zone(400, 70, 980, 650, "RESEARCH POOL: single-GPU nodes, one job per node", BLUE)
    s.box(420, 110, 450, 150, "A training run (MODE=train)", ["TRL GRPO with a colocated vLLM engine (8 GiB)", "the mixture decides which family each prompt is from", "one reward function dispatches to the family's verifier", "per-family reward, signal and tokens logged per batch"], BLUE)
    s.box(900, 110, 460, 150, "Evaluation (every run, start and end)", ["200 held-out prompts x 4 samples per family,", "seven families, same sampling seed;", "the six of the mixture and the one to admit"], BLUE)
    s.box(420, 290, 220, 130, "MODE=merge", ["reads the base and the", "specialists' weights from", "the bucket; TIES or plain", "task arithmetic"], GREY)
    s.box(660, 290, 220, 130, "MODE=probe", ["per-module gradients of", "each family's loss and", "their cosines"], GREY)
    s.box(900, 290, 220, 130, "MODE=eval", ["scores any model from", "its weights in the bucket"], GREY)
    s.box(1140, 290, 220, 130, "MODE=verify", ["verifier precision and", "cross-domain overlap of", "training and held-out data"], GREY)
    s.arrow([(355, 160), (420, 160)], "flow", "admits")
    s.arrow([(355, 280), (400, 280)], "flow")
    s.text(420, 470, "A pipeline (S10): two specialists train at once, the merge waits for both, the evaluation waits for the merge. Each step is one of the modes above,", 12, MUTED)
    s.text(420, 490, "run as a Job through the same queue as every sweep. Weights travel through the bucket; nothing needs a fast network between nodes.", 12, MUTED)
    s.text(420, 530, "Sweeps are Indexed Jobs: one pod per cell of the grid, admitted as a whole by Kueue.", 12, MUTED)
    return s.render()
def mixture_flow():
    s = Svg(1420, 470, "From families to a released model")
    xs = [40, 400, 760, 1100]
    s.box(xs[0], 70, 300, 150, "1. Families", ["six task families with verifiers", "and held-out sets; a registry", "entry per family; precision of", "each verifier measured"], BLUE)
    s.box(xs[1], 70, 300, 150, "2. Measure and choose", ["matrix: boost one family, score", "all; proxy runs at random weights;", "a response model; a search for the", "weights that lift the weakest"], BLUE)
    s.box(xs[2], 70, 300, 150, "3. Train", ["mixed, staged with refreshes, or", "specialists merged; weights counted", "in prompts or in tokens; a floor", "for every family"], BLUE)
    s.box(xs[3], 70, 280, 150, "4. Gate", ["every family scored; none may", "fall below the current release by", "its margin plus twice its noise;", "the average is not the test"], AMBER)
    for a, b in zip(xs, xs[1:]):
        s.arrow([(a + 300, 145), (b, 145)], "flow")
    s.arrow([(1240, 220), (1240, 270), (550, 270), (550, 225)], "bad", "a regression: back to the mixture", lx=900, ly=262)
    s.box(40, 300, 640, 130, "What a family has to bring (the contract)", ["versioned data with a held-out split that shares nothing with training; a verifier whose precision is measured;", "an eval suite and a margin; an owner. Admission is a proxy run at a small weight: the new family's row and column", "of the matrix, and the gate for every family already in."], GREY)
    s.box(720, 300, 660, 130, "What the mixture team owns", ["the weights, the floors, the order of stages, the merge weights and the gate. Domain teams do not negotiate", "their share: they bring a better verifier or better data, and the matrix says what that is worth."], GREY)
    return s.render()
def gate_picture():
    s = Svg(1420, 440, "The gate: an average that goes up is not enough")
    s.text(40, 40, "An illustration (made-up numbers; S09 measures the real ones): a candidate against the current release, family by family.", 13, INK, "700")
    rows = [("math", +0.05, True), ("code", +0.09, True), ("instr", +0.04, True), ("tools", +0.03, True), ("multiling", +0.02, True), ("safety", -0.12, False)]
    x0, scale = 200, 700
    s.parts.append(f'<line x1="{x0 + 0.15 * scale}" y1="70" x2="{x0 + 0.15 * scale}" y2="330" stroke="#94a3b8" stroke-width="1.5"/>')
    s.text(x0 + 0.15 * scale, 62, "current release", 11, MUTED, "600", "middle")
    for i, (f, d, ok) in enumerate(rows):
        y = 85 + i * 40
        s.text(40, y + 15, f, 12.5, INK, "700")
        c = GREEN if ok else RED
        x = x0 + 0.15 * scale + min(d, 0) * scale
        w = abs(d) * scale
        s.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="22" rx="4" fill="{c["fill"]}" stroke="{c["stroke"]}" stroke-width="1.4"/>')
        s.text(x0 + 0.15 * scale + (d * scale) + (8 if d > 0 else -8), y + 16, f"{d:+.2f}", 11.5, c["ink"], "700", "start" if d > 0 else "end")
    s.parts.append(f'<rect x="{x0 + 0.15 * scale - 0.04 * scale}" y="320" width="{0.04 * scale}" height="8" fill="#fde68a" stroke="#d97706"/>')
    s.text(x0 + 0.15 * scale - 0.04 * scale, 345, "tolerance: margin + 2 x noise", 11, "#92400e", "600")
    s.box(960, 70, 420, 150, "The average rises by 0.02", ["Five families rise and one falls by 0.12.", "Under 'the average must go up' this ships.", "Under the gate it does not: safety fell by", "four times its tolerance (0.03 here)."], AMBER)
    s.box(960, 250, 420, 120, "Aggregates that resist trade-offs", ["the minimum over families, and the geometric", "mean (which punishes a collapse in one family", "more than the average does)"], GREY)
    return s.render()
def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate([context, topology, architecture, mixture_flow, gate_picture], 1):
        (out / f"a12-{i}.svg").write_text(fn())
        print("wrote", out / f"a12-{i}.svg")
if __name__ == "__main__":
    main()

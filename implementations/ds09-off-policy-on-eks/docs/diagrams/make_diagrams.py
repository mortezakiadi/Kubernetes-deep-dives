#!/usr/bin/env python3
"""Generates the architecture diagrams for the Design 09 write-up as SVG (same visual language as the design's own pictures).

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
    s = Svg(1420, 560, "Who uses the platform and what a run is made of")
    s.zone(20, 30, 270, 500, "WHO", GREEN)
    s.box(40, 70, 230, 110, "Research team", ["picks the recipe, reads the", "curves, asks the library", "'can I set staleness to 8?'"], GREEN)
    s.box(40, 200, 230, 110, "Platform team", ["owns the cluster, the GPU", "pool and the research queue", "and its priorities"], GREEN)
    s.box(40, 330, 230, 120, "On-call engineer", ["reads the controller's timeline", "when a run was tightened,", "skipped or rolled back"], GREEN)
    s.zone(320, 30, 1080, 500, "WHAT A RUN IS (ONE POD, ONE GPU)", AMBER)
    s.box(340, 70, 320, 130, "Sampling engine", ["vLLM, in the trainer's process,", "holds the weights of one version;", "writes every token's log-probability", "(mu, the behavior policy)"], BLUE)
    s.box(700, 70, 320, 130, "Trainer", ["scores each sample on arrival with", "its own weights (pi_prox), then", "updates the policy (pi_theta) with", "the decoupled loss"], BLUE)
    s.box(1060, 70, 320, 130, "Sample pools", ["fresh, replay and external;", "an age limit and a distance limit;", "a share of each micro-batch"], BLUE)
    s.box(340, 240, 320, 130, "Metrics", ["ratio tail, effective sample size,", "behavior KL by age, clipped and", "masked fractions, version ages"], GREY)
    s.box(700, 240, 320, 130, "Controller", ["rules in a file: tighten, skip a", "batch, cut the learning rate, roll", "back to a healthy checkpoint"], GREY)
    s.box(1060, 240, 320, 130, "Reference library", ["the sweep's results: (learning rate,", "correction, staleness) to held-out", "score, stored with the recipe"], GREY)
    s.arrow([(270, 125), (340, 125)], "flow", "recipe")
    s.arrow([(270, 255), (340, 300)], "flow", "queue")
    s.arrow([(270, 390), (700, 340), (700, 340)], "flow", "timeline", lx=480, ly=392)
    s.text(340, 420, "The question this platform answers: how stale, how filtered or how foreign can the data be before the update stops helping,", 13, INK, "700")
    s.text(340, 440, "and what in the run tells you before the held-out score does?", 13, INK, "700")
    s.text(340, 470, "Scaled down to a 0.6B model on grade-school maths, runs of 48 to 100 steps, one GPU per run. The design's agents, long answers and", 12, MUTED)
    s.text(340, 488, "thousands of GPUs are not reachable; the three-policy loss, the metrics, the limits and the controller are.", 12, MUTED)
    return s.render()
def topology():
    s = Svg(1420, 470, "Instances")
    rows = [("m5.xlarge", "2", "system pool", "Kueue, device plugin, CoreDNS", "4 vCPU, 16 GiB", "$0.19 / h each"),
            ("g6e.2xlarge", "up to 4", "research pool", "one run per node, one L40S (48 GB) each", "8 vCPU, 64 GiB, 200 GB disk", "$2.24 / h each"),
            ("g5.2xlarge", "fallback", "research pool", "one A10G (24 GB) each; same pool, same taint", "8 vCPU, 32 GiB, 200 GB disk", "$1.21 / h each"),
            ("g6.2xlarge, g5.4xlarge", "fallback", "research pool", "one L4 (24 GB) or A10G; the pool lists all four", "8 to 16 vCPU", "$0.98 to $1.62 / h"),
            ("NAT gateway", "1", "network", "image pulls, model and dataset downloads", "one zone", "$0.045 / h + data"),
            ("S3 bucket", "1", "state", "each run's metrics file", "through the free gateway endpoint", "storage only")]
    heads = ["instance", "count", "pool", "what runs on it", "size", "price"]
    xs = [40, 270, 360, 500, 900, 1200]
    s.parts.append('<rect x="20" y="20" width="1380" height="430" rx="10" fill="#fff" stroke="#cbd5e1"/>')
    for x, h in zip(xs, heads):
        s.text(x, 55, h, 13, INK, "700")
    for i, r in enumerate(rows):
        y = 90 + i * 50
        s.parts.append(f'<line x1="30" y1="{y - 22}" x2="1390" y2="{y - 22}" stroke="#e2e8f0"/>')
        for x, v in zip(xs, r):
            s.text(x, y, v, 12.5, INK)
    s.text(40, 410, "GPU capacity on demand is volatile: the pool lists four single-GPU types and three zones so that a node that cannot be launched as one type is launched as another.", 12, MUTED)
    s.text(40, 430, "Every run lays out its memory the same way whatever the card: the sampling engine gets a fixed 8 GiB. Prices are on-demand list prices in us-east-1.", 12, MUTED)
    return s.render()
def architecture():
    s = Svg(1420, 760, "The cluster and one run")
    s.zone(20, 30, 1380, 710, "EKS 1.37, ONE VPC, THREE ZONES", GREY)
    s.zone(40, 70, 330, 650, "SYSTEM POOL: 2 x m5.xlarge", GREEN)
    s.box(55, 105, 300, 100, "Kueue", ["ClusterQueue 'research': 4 GPUs", "LocalQueue 'research-small'", "priorities: sweep-low, interactive"], GREEN)
    s.box(55, 230, 300, 80, "NVIDIA device plugin", ["advertises nvidia.com/gpu"], GREEN)
    s.box(55, 335, 300, 90, "Your laptop (kubectl, aws)", ["applies the Indexed Job; reads", "metrics back from the bucket"], GREY)
    s.box(55, 450, 300, 100, "State bucket (S3)", ["runs/<run>/metrics.jsonl", "copied while the run is alive"], GREY)
    s.zone(400, 70, 980, 650, "RESEARCH POOL: single-GPU nodes, one run per node (Indexed Job, one pod per cell)", BLUE)
    s.box(420, 110, 470, 260, "One run (one pod, one GPU)", ["TRL GRPOTrainer, colocated vLLM engine (8 GiB)", "1. generate K steps' worth of samples; record mu per token", "2. score each with the trainer's weights: pi_prox on arrival", "3. tag version; place in the fresh pool; keep for replay", "4. each micro-batch: pick a pool, check age and KL limits", "5. decoupled loss, metrics, controller, held-out evaluation", "6. push the new weights into the engine (in-process)"], BLUE)
    s.box(920, 110, 440, 130, "Sample pools", ["fresh: this generation's chunks, age 0 to K-1", "replay: older chunks, age <= 40, KL <= 0.05", "external: human-written solutions, own small loss"], BLUE)
    s.box(920, 260, 440, 110, "Controller", ["reads the step's metrics; rules from a file;", "acts before the optimizer step (skip) or after", "(tighten, cut LR, roll back); logs every action"], BLUE)
    s.box(420, 400, 470, 120, "Healthy checkpoints", ["every 10 steps, on the node's disk, tagged healthy", "when the last window was inside the limits;", "the rollback target is the newest healthy one"], GREY)
    s.box(920, 400, 440, 120, "Held-out evaluation", ["200 GSM8K test problems, 4 samples each,", "through the same engine, every 5 to 10 steps;", "by task type (short, medium, long)"], GREY)
    s.arrow([(355, 160), (420, 160)], "flow", "admits")
    s.arrow([(355, 500), (420, 500)], "ok", "metrics", lx=388, ly=490, both=True)
    s.text(420, 570, "Cells of a sweep run side by side on different nodes and never talk to each other: nothing here needs a fast network between nodes.", 12, MUTED)
    s.text(420, 590, "That is a property of the experiments, not a shortcut: the design's staleness question can be asked on one GPU, and the sweep is embarrassingly parallel.", 12, MUTED)
    return s.render()
def three_policies():
    s = Svg(1420, 480, "Three policies and the two gaps")
    x0 = 130
    s.text(40, 40, "One sample, from generation to use. Time runs to the right in optimizer steps (the weights' version).", 13, INK, "700")
    s.parts.append(f'<line x1="{x0}" y1="150" x2="1360" y2="150" stroke="#cbd5e1" stroke-width="2"/>')
    for i in range(9):
        x = x0 + i * 150
        s.parts.append(f'<circle cx="{x}" cy="150" r="6" fill="#64748b"/>')
        s.text(x, 132, f"v{i}", 12, MUTED, "600", "middle")
    s.box(60, 215, 330, 120, "mu: the behavior policy", ["the engine at version v0, sampling", "the tokens; its log-probability is", "recorded with every token"], AMBER)
    s.box(430, 215, 330, 120, "pi_prox: the proximal policy", ["the trainer's weights at v0, scoring", "the same tokens on arrival", "(one extra forward pass)"], BLUE)
    s.box(800, 215, 330, 120, "pi_theta: the target policy", ["the trainer's weights now (v7):", "the one being updated, the one", "the sample is used for"], GREEN)
    s.arrow([(225, 215), (225, 197), (122, 197), (122, 160)], "flow", "v0", lx=170, ly=190)
    s.arrow([(595, 215), (595, 205), (140, 205), (140, 160)], "flow")
    s.arrow([(965, 215), (965, 197), (1180, 197), (1180, 160)], "flow", "v7", lx=1075, ly=190)
    s.box(1160, 215, 220, 120, "The loss", ["clip pi_theta / pi_prox", "weight by pi_prox / mu", "(truncate 3, mask 10)"], GREY)
    s.arrow([(390, 275), (430, 275)], "bad", "engine", lx=410, ly=262)
    s.arrow([(760, 275), (800, 275)], "bad", "age", lx=780, ly=262)
    s.text(60, 380, "The engine's gap (mu to pi_prox) is there at age 0 and does not depend on age. The staleness gap (pi_prox to pi_theta) is zero at age 0 and grows with every step.", 12.5, INK)
    s.text(60, 402, "The naive loss clips pi_theta / mu and so mixes the two. The decoupled loss separates them: the trust region measures staleness only, the weight handles the engine.", 12.5, INK)
    s.text(60, 440, "S02 measures both gaps by age. S05 makes the engine's gap large on purpose and compares the two losses.", 12, MUTED)
    return s.render()
def controller():
    s = Svg(1420, 470, "The controller: graduated responses and hysteresis")
    s.box(40, 70, 300, 150, "Level 1: tighten", ["ratio tail above 1% for 3 steps:", "staleness limit down by two,", "replay share halved"], BLUE)
    s.box(380, 70, 300, 150, "Level 2: protect the update", ["effective sample size below 0.5:", "skip this batch (no weights move),", "learning rate halved for 20 steps"], AMBER)
    s.box(720, 70, 300, 150, "Level 3: recover", ["ratio tail above 3% and entropy", "falling: roll back to the last healthy", "checkpoint, halve the rate, page"], RED)
    s.box(1060, 70, 320, 150, "Hysteresis", ["after 30 healthy steps the settings", "loosen one notch (limit +1, replay", "x1.25) and no further until 30 more"], GREEN)
    s.arrow([(340, 145), (380, 145)], "flow", "worse")
    s.arrow([(680, 145), (720, 145)], "flow", "worse")
    s.arrow([(1060, 190), (340, 190)], "ok", "healthy for a while", lx=700, ly=232)
    s.box(40, 270, 1340, 150, "Everything is an event", ["step, rule, action, and the metric values that triggered it, written to the run's own metrics file;", "the settings in force are written with every step, so the configuration at any step can be reconstructed from the file alone.", "The rules file is versioned with the recipe: the thresholds were set from the healthy runs of S02 and S03 and the failed runs of the sweep."], GREY)
    return s.render()
def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate([context, topology, architecture, three_policies, controller], 1):
        (out / f"a10-{i}.svg").write_text(fn())
        print("wrote", out / f"a10-{i}.svg")
if __name__ == "__main__":
    main()

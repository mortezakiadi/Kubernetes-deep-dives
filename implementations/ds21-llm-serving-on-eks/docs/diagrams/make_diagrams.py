#!/usr/bin/env python3
"""Generates the architecture diagrams for the Design 21 write-up as SVG (same visual language as the design's own pictures).

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
    s = Svg(1420, 520, "Who uses the serving platform and what it is made of")
    s.zone(20, 30, 270, 460, "WHO", GREEN)
    s.box(40, 70, 230, 110, "Product teams", ["chat and code products, each", "with its own prompt shape and", "its own latency targets"], GREEN)
    s.box(40, 210, 230, 110, "Serving engineers", ["own the layout, the engine", "settings, the routing and the", "autoscaling; run the tuning loop"], GREEN)
    s.box(40, 350, 230, 110, "Platform team", ["owns the cluster, the GPU", "pool, the metrics and the", "capacity and cost"], GREEN)
    s.zone(330, 30, 1070, 460, "THE SERVING PLATFORM", BLUE)
    s.box(360, 70, 230, 120, "Traffic and targets", ["two products, bursty arrivals,", "shared prefixes; targets for the", "first token and each next token"], BLUE, num=1)
    s.box(630, 70, 230, 120, "Router", ["round robin, least loaded or by", "prefix; or splits a request into", "prefill and decode"], BLUE, num=2)
    s.box(900, 70, 230, 120, "Engine replicas", ["vLLM with a layout (TP, EP), a", "batch limit, a prefix cache, an", "optional host tier, a precision"], BLUE, num=3)
    s.box(1160, 70, 210, 120, "GPU node", ["four T4s on one PCIe", "fabric; the unit that every", "layout is placed on"], BLUE, num=4)
    s.box(360, 250, 230, 120, "Metrics", ["each server's /metrics, GPU", "counters, scraped every 5 s by", "Prometheus"], AMBER, num=5)
    s.box(630, 250, 230, 120, "Autoscaler", ["KEDA turns a metric query into", "a replica count for the Deployment"], AMBER, num=6)
    s.box(900, 250, 230, 120, "Accuracy gate", ["400 exact-answer questions; a", "quantized setting is adopted", "only if it passes"], AMBER, num=7)
    s.box(1160, 250, 210, 120, "Tuning loop", ["one change at a time, a", "control for the noise, repeats", "at the end"], AMBER, num=8)
    s.arrow([(590, 130), (630, 130)], "flow")
    s.arrow([(860, 130), (900, 130)], "flow")
    s.arrow([(1130, 130), (1160, 130)], "flow")
    s.arrow([(1015, 190), (1015, 250)], "blue", dash=True)
    s.arrow([(745, 250), (745, 190)], "blue", "replicas", 745, 222)
    s.text(360, 440, "Goodput = tokens per second per GPU from requests that met both their first-token and their inter-token targets.", 12, MUTED)
    return s.render()
def topology():
    s = Svg(1420, 560, "Instances: the cluster, the CPU nodes and the four-GPU node")
    s.zone(20, 30, 1380, 500, "ONE VPC, THREE ZONES, ONE REGION", BLUE)
    s.box(50, 80, 300, 130, "EKS control plane", ["Kubernetes 1.37, API endpoint open", "to one address; pod identity", "gives the tools access to one bucket"], GREY)
    s.box(50, 240, 300, 120, "System nodes: 2 x m5.xlarge", ["Prometheus, KEDA, the NVIDIA", "device plugin (as a daemon), the", "controllers"], GREY)
    s.box(50, 390, 300, 120, "Load node: c6i.4xlarge", ["the load generators and the", "router, kept off the GPU node so", "they never compete with servers"], GREY)
    s.zone(400, 70, 980, 440, "GPU NODE: g4dn.12xlarge  (4 x NVIDIA T4, 16 GB each, 48 vCPU, 192 GB, 800 GB disk)", AMBER)
    for i in range(4):
        s.box(430 + i * 235, 120, 215, 120, f"GPU {i}: T4", ["16 GB, 320 GB/s, 65 TFLOPS fp16", "no FP8, no bf16, no NVLink"], AMBER)
    s.parts.append('<rect x="430" y="290" width="925" height="36" rx="8" fill="#fef3c7" stroke="#d97706" stroke-width="1.5"/>')
    s.text(892, 313, "PCIe host bridge: every GPU-to-GPU transfer goes here", 13, "#92400e", "700", "middle")
    for i in range(4):
        s.arrow([(537 + i * 235, 240), (537 + i * 235, 290)], "flow", both=True)
    s.box(430, 360, 440, 120, "Model cache on the node's disk", ["/mnt/hf: the weights are downloaded once;", "servers that start together read them", "from disk, not from the network"], GREY)
    s.box(900, 360, 455, 120, "Why this node", ["g6e (L40S), g6 (L4) and g5 (A10G) four-GPU types", "were refused in every zone of three regions;", "this was the one four-GPU type EC2 would launch"], RED)
    return s.render()
def architecture():
    s = Svg(1420, 600, "Architecture: from a request to a token, and from a metric to a replica")
    s.zone(20, 30, 1380, 540, "NAMESPACE serving", BLUE)
    s.box(50, 80, 240, 120, "Load generator (Job)", ["open loop: sends requests when", "the trace says, whether or not", "the server keeps up; records TTFT", "and the gap between tokens"], GREY, num=1)
    s.box(340, 80, 240, 120, "Router (Deployment)", ["policy: rr | least | prefix | pd", "(and re-reads a headless Service", "so new replicas get traffic)"], BLUE, num=2)
    s.box(660, 70, 330, 140, "Servers: one Deployment each", ["vLLM, OpenAI-compatible API, Triton attention", "TP1, TP2, TP4, or four DP engines with EP", "prefix cache; optional host KV tier", "optional NIXL connector for P/D"], BLUE, num=3)
    s.box(1050, 80, 320, 120, "S3 state bucket", ["each load run writes its per-request", "records; the tests read them back", "(nothing is judged from memory)"], GREY, num=4)
    s.arrow([(290, 140), (340, 140)], "flow", "request")
    s.arrow([(580, 140), (660, 140)], "flow", "stream")
    s.arrow([(1050, 140), (990, 140)], "flow", dash=True)
    s.box(660, 260, 330, 120, "Prefill server  ->  Decode server", ["the prompt is computed once on one GPU;", "its KV cache is copied over PCIe by NIXL", "and the other GPU produces the tokens"], AMBER, num=5)
    s.arrow([(825, 210), (825, 260)], "blue", dash=True)
    s.box(50, 330, 330, 130, "Prometheus", ["scrapes every server's /metrics and the", "GPU exporter every 5 s: requests waiting,", "running, KV usage, GPU utilisation"], AMBER, num=6)
    s.box(430, 330, 180, 130, "KEDA", ["query / threshold", "-> replica count"], AMBER, num=7)
    s.arrow([(380, 395), (430, 395)], "flow", "query")
    s.arrow([(520, 330), (520, 235), (700, 235), (700, 210)], "flow", "scale replicas", 600, 235)
    s.box(1050, 260, 320, 120, "Probe pod", ["runs commands inside the engine image:", "flags, defaults, the GPU topology,", "the accuracy questions' prompts"], GREY, num=8)
    s.text(50, 520, "Everything is created and removed by Terraform and by the tests; nothing runs outside the cluster except the commands that drive it.", 12, MUTED)
    return s.render()
def layouts():
    s = Svg(1420, 560, "Layouts: four copies, wider groups, expert parallel, and split phases")
    def row(y, title, boxes):
        s.text(30, y + 18, title, 13, INK, "700")
        for i, (label, c) in enumerate(boxes):
            s.parts.append(f'<rect x="{240 + i * 125}" y="{y}" width="110" height="46" rx="8" fill="{c["fill"]}" stroke="{c["stroke"]}" stroke-width="1.6"/>')
            s.text(240 + i * 125 + 55, y + 28, label, 12, c["ink"], "700", "middle")
    row(50, "4 x TP1 (four copies)", [("GPU0 full", BLUE), ("GPU1 full", BLUE), ("GPU2 full", BLUE), ("GPU3 full", BLUE)])
    row(120, "2 x TP2", [("GPU0 half", GREEN), ("GPU1 half", GREEN), ("GPU2 half", GREEN), ("GPU3 half", GREEN)])
    row(190, "1 x TP4", [("GPU0 quarter", AMBER), ("GPU1 quarter", AMBER), ("GPU2 quarter", AMBER), ("GPU3 quarter", AMBER)])
    row(290, "MoE: TP4", [("1/4 of every layer", AMBER)] * 4)
    row(360, "MoE: 4 x DP + EP", [("attn + 32 experts", GREEN)] * 4)
    row(450, "P/D: 2P + 2D", [("prefill", RED), ("prefill", RED), ("decode", BLUE), ("decode", BLUE)])
    s.text(760, 80, "Four copies of the weights; no communication;", 12, INK)
    s.text(760, 98, "the cache is what is left in each GPU.", 12, INK)
    s.text(760, 150, "Weights shared by two GPUs; one all-reduce per", 12, INK)
    s.text(760, 168, "layer over PCIe; more room for the cache.", 12, INK)
    s.text(760, 220, "Weights shared by four; the most cache; four-way", 12, INK)
    s.text(760, 238, "all-reduce over PCIe for every layer.", 12, INK)
    s.text(760, 320, "Experts and attention are both split; all-reduce.", 12, INK)
    s.text(760, 390, "Attention is replicated; experts are split;", 12, INK)
    s.text(760, 408, "tokens travel between ranks for the experts (all-to-all).", 12, INK)
    s.text(760, 480, "The prompt is computed on a prefill GPU and the", 12, INK)
    s.text(760, 498, "KV cache is copied to a decode GPU (NIXL, over PCIe).", 12, INK)
    return s.render()
def loop():
    s = Svg(1420, 420, "The tuning loop: measure, change one thing, compare to a control, gate, adopt")
    steps = [("1 Workload and targets", ["measure lengths, shared prefix,", "arrivals; fix the targets"], GREY), ("2 Baseline", ["defaults on one GPU and on", "the whole node; goodput curve"], GREY),
             ("3 Change one thing", ["layout, chunk size, batch limit,", "routing, host tier, precision"], BLUE), ("4 Control", ["the same setting twice to", "measure the noise"], AMBER),
             ("5 Gate", ["quantized setting? accuracy", "questions first, speed after"], RED), ("6 Adopt or drop", ["only what beat the noise", "enters the tuned stack"], GREEN),
             ("7 Repeat the stack", ["tuned against default, three", "seeds each; compare to the range"], GREEN)]
    for i, (t, l, c) in enumerate(steps):
        x = 30 + i * 195
        s.box(x, 120, 180, 120, t, l, c)
        if i:
            s.arrow([(x - 15, 180), (x, 180)], "flow")
    s.text(30, 300, "A setting that does not beat the control by more than the noise stays at its default, whatever the folklore says about it.", 13, INK, "600")
    s.text(30, 330, "Goodput is measured per GPU against the targets; throughput that arrives too late is not counted.", 12, MUTED)
    return s.render()
def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate([context, topology, architecture, layouts, loop], 1):
        (out / f"a22-{i}.svg").write_text(fn())
        print("wrote", out / f"a22-{i}.svg")
if __name__ == "__main__":
    main()

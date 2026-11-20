#!/usr/bin/env python3
"""Generates the architecture diagrams for the Design 03 write-up as SVG (same visual language as the design's own pictures).

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
    s = Svg(1420, 520, "Three groups, four golden paths, one platform")
    s.zone(20, 30, 250, 440, "WHO", GREEN)
    s.box(40, 70, 210, 100, "Researchers", ["30 people, prototyping", "want a GPU in minutes,", "and to stop paying at lunch"], GREEN)
    s.box(40, 190, 210, 100, "Training team", ["fine-tunes, long runs", "want to submit once and", "survive failures"], GREEN)
    s.box(40, 310, 210, 100, "Product teams", ["serve models to customers", "want latency and low cost", "per token"], GREEN)
    s.zone(300, 30, 340, 440, "WHAT THEY WRITE (GOLDEN PATHS)", BLUE)
    s.box(320, 70, 300, 80, "Notebook", ["StatefulSet + GPU + workspace volume", "queue label: notebooks"], BLUE)
    s.box(320, 165, 300, 80, "TrainJob", ["runtime: torch-gpu-ddp, 2 nodes, args", "queue label: training"], BLUE)
    s.box(320, 260, 300, 80, "Model deployment / batch job", ["Deployment (or LeaderWorkerSet)", "queue labels: serving, batch"], BLUE)
    s.box(320, 355, 300, 90, "Registry commands", ["register, evaluate, promote", "(a model moves only through the gates)"], BLUE)
    s.zone(670, 30, 730, 440, "WHAT THE PLATFORM PROVIDES", AMBER)
    s.box(690, 70, 330, 110, "Kueue queues, one cohort", ["notebooks 1 GPU | training 2 GPUs", "serving 2 GPUs (lends 1) | batch 0", "priorities: production > training > opportunistic"], AMBER)
    s.box(1040, 70, 340, 110, "Workload APIs", ["Kubeflow Trainer + JobSet", "LeaderWorkerSet, Deployment, Job,", "StatefulSet"], AMBER)
    s.box(690, 200, 330, 110, "GPU pools, one per family", ["gpu-research, gpu-training, gpu-inference", "tainted; scaled from zero; any one-GPU", "g4dn, g5, g6, g6e the zone can give"], RED)
    s.box(1040, 200, 340, 110, "Serving stack", ["Gateway + endpoint picker", "vLLM replicas, autoscaled by KEDA", "warm buffer, weights from the registry"], AMBER)
    s.box(690, 330, 330, 120, "Data and models (S3)", ["datasets/, runs/ (checkpoints, events)", "registry/ (versions, pointers, audit)", "evals/ (results)  |  Pod Identity per family"], GREY)
    s.box(1040, 330, 340, 120, "Observability", ["Prometheus: vLLM, training, DCGM,", "kube-state-metrics", "every series carries namespace = family"], GREY)
    for y in (120, 240, 360):
        s.arrow([(250, y), (300, y)], "flow")
    s.arrow([(640, 300), (690, 300)], "flow")
    return s.render()
def topology():
    s = Svg(1420, 780, "One EKS cluster, five zones, GPU nodes on demand")
    s.zone(20, 20, 1380, 740, "AWS ACCOUNT, us-east-1, ONE VPC 10.31.0.0/16", GREY)
    s.box(50, 70, 330, 140, "EKS Auto Mode cluster", ["ds03-trainserve, Kubernetes 1.37", "control plane in zones a, b, c (fixed at creation)", "API: private in the VPC; public only to", "the administrator's /32; audit logs 3 days"], BLUE)
    s.box(50, 230, 330, 150, "Node pools (all Auto Mode)", ["general-purpose, system: small c6a nodes", "gpu-research  limit 1 GPU, idle 2 min", "gpu-training  limit 2 GPUs, idle 6 min", "gpu-inference limit 3 GPUs, idle 3 min", "one NodeClass 'gpu' spanning 5 zones, 120 GiB disk"], RED)
    s.box(50, 400, 330, 130, "S3 bucket (one, private)", ["datasets/  runs/  registry/  evals/  batch/", "five Pod Identity roles, one per family,", "each limited to the prefixes it needs"], GREY)
    s.box(50, 550, 330, 170, "Terraform (two layers, local state)", ["10-cluster: VPC, NAT, IAM, the cluster", "20-platform: S3, identities, Trainer, LWS,", "Kueue, KEDA, Prometheus, DCGM, gateway,", "router, pools, queues, runtime, vLLM", "account guard; down.sh then verify-clean.sh"], GREY)
    zones = ["1a", "1b", "1c", "1d", "1f"]
    for i, z in enumerate(zones):
        x = 410 + i * 197
        s.zone(x, 60, 187, 490, f"us-east-1{z[-1]}", GREY if i < 3 else AMBER)
        s.box(x + 8, 100, 171, 60, "Private /20", ["nodes" + ("" if i < 3 else " (not a cluster zone)")], GREEN if i < 3 else AMBER)
        s.box(x + 8, 180, 171, 90, "Platform nodes", ["Kueue, Trainer,", "gateway, Prometheus"] if i < 2 else ["(when scheduled here)"], GREEN)
        s.box(x + 8, 290, 171, 120, "GPU nodes (on demand)", ["1 GPU each", "tainted", "removed when empty"], RED)
    s.box(410, 440, 187, 80, "NAT gateway", ["one, in 1a"], GREY)
    s.text(410, 600, "Why five zones: GPU capacity is not guaranteed. In this build g4dn, g5, g6 and g6e", 12, INK, "700")
    s.text(410, 622, "all returned InsufficientInstanceCapacity in some zone at some time. Auto Mode picks the cheapest", 12, INK)
    s.text(410, 640, "instance that has capacity, in any zone the NodeClass can see. EKS fixes a cluster's zones at", 12, INK)
    s.text(410, 658, "creation, so two zones were added through the NodeClass, not the cluster.", 12, INK)
    return s.render()
def architecture():
    s = Svg(1420, 800, "Design 03 on EKS: the five layers")
    layers = [
        ("USERS TOUCH", GREEN, [("Notebook", ["StatefulSet, 1 GPU", "workspace volume"]), ("TrainJob", ["runtime torch-gpu-ddp", "2 nodes x 1 GPU"]), ("Model deployment", ["Deployment or", "LeaderWorkerSet"]), ("Batch job", ["eval, resumable batch", "borrows idle GPUs"])]),
        ("WORKLOAD APIS", BLUE, [("Kubeflow Trainer 2.4", ["TrainJob -> JobSet", "ClusterTrainingRuntime"]), ("LeaderWorkerSet 1.0", ["leader + workers", "recreated as a group"]), ("Deployment, StatefulSet, Job", ["plain Kubernetes"]), ("KEDA 2.21", ["scale on requests waiting"])]),
        ("SCHEDULING AND CAPACITY", AMBER, [("Kueue 0.20.7", ["4 queues, 1 cohort", "gang admission, borrow, preempt"]), ("Priority classes", ["production 1000", "training 500, opportunistic 100"]), ("Auto Mode NodePools", ["gpu-research, -training, -inference", "taint, limits, consolidation"]), ("Warm buffer", ["low-priority placeholder", "holds a node + the image"])]),
        ("DATA AND MODELS", GREY, [("S3: datasets", ["Tiny Shakespeare"]), ("S3: runs", ["sharded checkpoints", "markers written last", "events (goodput)"]), ("S3: registry", ["immutable versions", "pointers, audit trail"]), ("S3: evals", ["30-question suite", "throughput"])]),
        ("SERVING AND OBSERVABILITY", RED, [("Gateway + endpoint picker", ["agentgateway 1.4.1", "llm-d router 0.9.0"]), ("vLLM 0.34.0", ["prefix caching on", "weights from registry"]), ("Prometheus", ["vLLM, training, DCGM,", "kube-state-metrics"]), ("DCGM exporter 4.9.0", ["GPU util per pod"])]),
    ]
    for r, (title, c, boxes) in enumerate(layers):
        y = 20 + r * 155
        s.zone(20, y, 1380, 140, title, c)
        for i, (t, lines) in enumerate(boxes):
            s.box(40 + i * 340, y + 35, 320, 95, t, lines, c)
    return s.render()
def training_flow():
    s = Svg(1420, 640, "One training job, from manifest to finish (and through a failure)")
    xs = [100, 330, 560, 790, 1020, 1280]
    names = [("User", GREEN), ("Kueue", AMBER), ("Trainer + JobSet", BLUE), ("Karpenter", RED), ("Pods (2 ranks)", BLUE), ("S3", GREY)]
    for x, (n, c) in zip(xs, names):
        s.box(x - 85, 20, 170, 50, n, [], c)
        s.parts.append(f'<line x1="{x}" y1="70" x2="{x}" y2="610" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5 5"/>')
    steps = [
        (0, 2, 105, "1  TrainJob (queue label)", "flow"),
        (2, 1, 148, "2  Workload: 2 GPUs, all or nothing", "blue"),
        (1, 1, 185, "quota for both? admit, else hold: no pods", "ok"),
        (1, 2, 225, "3  admitted: unsuspend the JobSet", "flow"),
        (2, 4, 265, "4  create both pods (same zone by affinity)", "flow"),
        (4, 3, 305, "5  pods unplaced: buy two GPU nodes", "blue"),
        (3, 4, 345, "6  nodes join; burn-in, then NCCL init", "muted"),
        (4, 5, 385, "7  async shard + marker every N steps", "ok"),
        (4, 4, 428, "node lost or rank hangs (watchdog) ...", "bad"),
        (4, 2, 468, "8  group fails: JobSet restarts it", "bad"),
        (4, 5, 515, "9  newest complete checkpoint read", "muted"),
        (4, 5, 560, "10 finish: final checkpoint, events", "ok"),
    ]
    for a, b, y, label, kind in steps:
        if a == b:
            s.text(xs[a] + 14, y, label, 11.5, COLORS[kind], "700")
        else:
            s.arrow([(xs[a], y), (xs[b], y)], kind, label, (xs[a] + xs[b]) / 2, y - 7)
    return s.render()
def serving_flow():
    s = Svg(1420, 620, "A request, a model version and a scaling decision")
    s.box(20, 60, 170, 80, "Client", ["in the cluster"], GREEN)
    s.box(250, 60, 210, 80, "Gateway", ["agentgateway, ClusterIP", "HTTPRoute -> InferencePool"], BLUE)
    s.box(520, 60, 260, 80, "Endpoint picker (EPP)", ["scores: queue 2, KV cache 2,", "prefix-cache match 3"], AMBER)
    s.box(850, 20, 250, 70, "vLLM replica A", ["prefix cache, 1 GPU"], RED)
    s.box(850, 110, 250, 70, "vLLM replica B", ["prefix cache, 1 GPU"], RED)
    s.arrow([(190, 100), (250, 100)], "flow", "request")
    s.arrow([(460, 100), (520, 100)], "flow")
    s.arrow([(780, 85), (850, 55)], "ok")
    s.arrow([(780, 115), (850, 145)], "muted")
    s.text(690, 45, "best replica ->", 11, COLORS["ok"], "600")
    s.box(1150, 20, 250, 160, "Metrics (Prometheus)", ["each replica: requests waiting,", "KV cache, prefix hits, TTFT", "scraped every 10 s"], GREY)
    s.arrow([(1100, 55), (1150, 55)], "muted")
    s.box(1150, 260, 250, 130, "KEDA", ["query: sum(requests waiting)", "threshold 4 per replica", "min 1, max 2 replicas"], AMBER)
    s.arrow([(1275, 180), (1275, 260)], "flow", "waiting > 4", 1275, 225)
    s.arrow([(1150, 330), (975, 330), (975, 180)], "flow", "scale out", 1060, 323)
    s.box(20, 260, 250, 120, "Registry (S3)", ["v1 v2 v3: immutable", "pointers: production -> v2", "audit trail of every decision"], GREY)
    s.box(330, 260, 250, 120, "Init container", ["reads the production pointer", "copies that version only", "(weights stay off the image)"], BLUE)
    s.arrow([(270, 320), (330, 320)], "flow")
    s.arrow([(580, 320), (820, 320), (820, 160), (850, 160)], "flow", "weights", 700, 313)
    s.box(20, 430, 250, 150, "Evaluation gate", ["same 30 questions for each version", "quality, throughput, checksum", "approve or refuse, with reasons"], AMBER)
    s.arrow([(145, 430), (145, 380)], "flow", "promote", 190, 410)
    s.box(330, 430, 1070, 150, "A new replica: where the time goes (S08 measures each part)", ["1  a GPU node is bought, if none is free (capacity is not guaranteed)", "2  the 10 GB serving image is pulled, the first time on that node", "3  the init container fetches the promoted weights from S3", "4  vLLM loads the model and becomes ready", "A warm buffer node (a low-priority placeholder holding a GPU and the image) removes steps 1 and 2."], RED)
    return s.render()
def sharing():
    s = Svg(1420, 560, "Sharing GPUs: serving's idle GPU lent to batch work, taken back on demand")
    s.text(40, 40, "Cohort gpu-fleet. serving owns 2 GPUs of flavor gpu-inference and may lend 1. batch owns none.", 14, INK, "700")
    steps = [
        ("1  Night: serving uses 1 of 2", GREEN, ["one replica runs", "the other GPU is idle quota", "batch borrows it: a resumable job", "runs on a second node, saves progress to S3"]),
        ("2  Traffic grows: serving asks for 2", AMBER, ["KEDA adds a replica; its pod asks Kueue", "serving is within its own quota", "reclaimWithinCohort: Any"]),
        ("3  Reclaim", RED, ["Kueue preempts the batch Workload", "the Job is suspended, its pod removed", "the replica takes the GPU (image already there)"]),
        ("4  Traffic falls: serving back to 1", BLUE, ["batch is admitted again", "reads its progress, resumes at item N", "and finishes"]),
    ]
    for i, (t, c, lines) in enumerate(steps):
        x = 40 + i * 345
        s.box(x, 70, 325, 170, t, lines, c)
        if i < 3:
            s.arrow([(x + 325, 155), (x + 345, 155)], "flow")
    s.box(40, 280, 1330, 120, "What makes this safe", ["Only work that tolerates eviction may borrow: the batch job saves progress after every item, so preemption costs a restart, not the work.", "Serving keeps a floor: lendingLimit 1 means one of its two GPUs can never be lent out.", "Borrowing needs the same flavor: batch lists gpu-inference, so it can only land on inference nodes, where serving's image is already cached."], GREY)
    s.box(40, 420, 1330, 110, "What it costs", ["Reclaim time is not free: a preempted pod must end and the replica must load its weights. The scenario measures it.", "Training and research keep their own flavors and cannot be borrowed from: multi-day training must not be evicted for a chat spike."], AMBER)
    return s.render()
def registry():
    s = Svg(1420, 380, "A model version's life: register, evaluate, gate, promote, serve, roll back")
    steps = [("register", GREY, ["download, hash, write", "v1 (immutable)"]), ("derive", GREY, ["prune 15% -> v2", "prune 80% -> v3", "parent + transform recorded"]), ("evaluate", BLUE, ["batch job on a GPU", "30 questions, same for all", "tokens per second"]), ("gate", AMBER, ["quality >= floor and", ">= production - 0.10", "throughput, checksum"]), ("promote", GREEN, ["pointer -> version", "audit record (also", "for refusals)"]), ("serve + roll back", RED, ["init container reads pointer", "restart = rollout", "repoint + restart = rollback"])]
    for i, (t, c, lines) in enumerate(steps):
        x = 20 + i * 232
        s.box(x, 70, 215, 130, f"{i + 1}  {t}", lines, c)
        if i < 5:
            s.arrow([(x + 215, 135), (x + 232, 135)], "flow")
    s.text(20, 250, "v3 (80% of weights removed) fails the quality gate. v2 passes. The decision and its evidence are written either way.", 13, INK, "700")
    return s.render()
def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a04-1", context), ("a04-2", topology), ("a04-3", architecture), ("a04-4", training_flow), ("a04-5", serving_flow), ("a04-6", sharing), ("a04-7", registry)):
        (out / f"{name}.svg").write_text(fn())
if __name__ == "__main__":
    main()

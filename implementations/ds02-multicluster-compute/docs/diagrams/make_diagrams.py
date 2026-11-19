#!/usr/bin/env python3
"""Generates the architecture diagrams for the write-up as SVG (same visual language as the design's own pictures).

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

    def box(self, x, y, w, h, title, lines=(), c=GREY, num=None):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#ffffff" stroke="{c["stroke"]}" stroke-width="1.6"/>')
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
def architecture():
    s = Svg(1420, 820, "Design 02 on EKS: one hub, three workers, one front door")
    s.box(20, 330, 200, 150, "Teams", ["llm, vision, research", "submit a Job to a LocalQueue", "in their own namespace", "on the hub, nowhere else"], GREEN, 1)
    s.zone(250, 40, 1120, 270, "HUB CLUSTER (manager): runs no user workloads", GREEN)
    s.box(275, 85, 330, 150, "Kueue manager + MultiKueue", ["layer 1: does the team have fleet quota?", "which worker should be offered the job?", "copies the Workload to the workers,", "the first to admit wins, others deleted", "status mirrored back to the hub"], GREEN, 2)
    s.box(630, 85, 330, 150, "Fleet queues (one table of team shares)", ["llm-cpu  llm-gpu", "vision-cpu  vision-gpu", "research-cpu  research-gpu", "each = the SUM of that team's share", "across the workers"], GREEN, 3)
    s.box(985, 85, 360, 150, "Guard rails", ["admission policy: a pod in a team", "namespace is refused (hub = no pods)", "one kubeconfig Secret per worker,", "narrow identity, never cluster-admin"], AMBER, 4)
    for i, (name, role, pools, qs) in enumerate([
        ("cpu-a", "CPU worker", ["NodePool cpu-ondemand", "c or m family, 2 or 4 vCPU", "empty nodes removed after 1 min"], ["llm 2, vision 1, research 1 CPU", "one cohort, borrow and reclaim"]),
        ("cpu-b", "CPU worker", ["NodePool cpu-spot", "same shapes, Spot capacity", "empty nodes removed after 1 min"], ["llm 2, vision 1, research 1 CPU", "one cohort, borrow and reclaim"]),
        ("gpu", "GPU worker", ["NodePools gpu-ondemand, gpu-spot", "one GPU per node: g6, g5, g6e or g4dn", "tainted: only admitted jobs tolerate", "scaled from zero, back to zero"], ["on-demand before Spot, per queue", "research: Spot only"]),
    ]):
        x = 250 + i * 380
        s.zone(x, 360, 360, 360, f"{name.upper()}  ({role})", RED if name == "gpu" else BLUE)
        s.box(x + 15, 395, 330, 100, "Kueue worker", ["layer 2: quota, borrow, preempt,", "admit the whole job or none"], BLUE)
        s.box(x + 15, 510, 330, 80, "Team queues and namespaces", qs, BLUE)
        s.box(x + 15, 605, 330, 108, "Node pools (Auto Mode)", pools, RED if name == "gpu" else GREY, 5 if i == 0 else None)
        s.arrow([(x + 180, 310), (x + 180, 360)], "blue", "MultiKueue" if i == 1 else None, x + 180, 340)
    s.arrow([(220, 400), (250, 400), (250, 235), (275, 235)], "flow", "submit", 262, 300)
    s.box(250, 735, 540, 65, "Identity and data", ["one S3 bucket, one prefix per team; Pod Identity role per team, per worker"], GREY, 6)
    s.box(810, 735, 560, 65, "Terraform", ["one fleet definition makes all four clusters; one team table makes every queue"], GREY, 7)
    s.text(20, 520, "Layers: 1 hub quota and dispatch,", 11, MUTED)
    s.text(20, 538, "2 worker quota, 3 scheduler, 4 node pool.", 11, MUTED)
    return s.render()


def topology():
    s = Svg(1420, 760, "Fleet topology and instances")
    s.zone(20, 20, 1380, 720, "AWS ACCOUNT, us-east-1, ONE VPC 10.30.0.0/16", GREY)
    s.box(50, 70, 340, 150, "Four EKS Auto Mode clusters", ["Kubernetes 1.37 on all four", "ds02-fleet-hub, -cpu-a, -cpu-b, -gpu", "API: private inside the VPC, public only", "to the administrator's /32", "audit logs to CloudWatch, 3 days"], BLUE)
    s.box(50, 250, 340, 130, "IAM", ["cluster role: 5 AWS-managed EKS policies", "one node role: WorkerNodeMinimal + ECR pull", "per-team roles for S3 (Pod Identity)", "no administrator credential on a cluster"], AMBER)
    s.box(50, 410, 340, 110, "S3 bucket, one prefix per team", ["llm/, vision/, research/", "each team's role reads only its prefix", "public access blocked"], GREY)
    s.box(50, 550, 340, 160, "Terraform (two layers, local state)", ["10-fleet: VPC, NAT, IAM, four clusters", "20-platform: Kueue x4, queues, node pools,", "MultiKueue wiring, identities, policies", "account guard; down.sh then verify-clean.sh"], GREY)
    for i, az in enumerate(("us-east-1a", "us-east-1b")):
        x = 430 + i * 480
        s.zone(x, 60, 460, 660, f"AVAILABILITY ZONE {az}", GREY)
        s.box(x + 20, 100, 420, 90, f"Public subnet 10.30.{i}.0/24", ["NAT gateway (one, in 1a)"] if i == 0 else ["empty (single NAT keeps cost down)"], GREY)
        s.box(x + 20, 220, 420, 210, f"Private subnet 10.30.{16 * (i + 1)}.0/20", ["all four clusters' nodes", "platform pool: Kueue controllers, add-ons", "cpu-a: pool cpu-ondemand", "cpu-b: pool cpu-spot (Spot)", "hub: no pool for user work"], GREEN)
        s.box(x + 20, 460, 420, 240, "GPU nodes (only while a job needs them)", ["cluster gpu: pools gpu-ondemand, gpu-spot", "xlarge: 4 vCPU, 16 GiB, one GPU", "tainted nvidia.com/gpu:NoSchedule", "limit: 2 GPUs on-demand, 2 on Spot", "removed 2 minutes after the last job"], RED)
    s.text(30, 752, "Instance types are chosen by Auto Mode inside each NodePool's limits. Design 02 spans several regions; this build keeps all four clusters in one.", 11, MUTED)
    return s.render()


def layers():
    s = Svg(1420, 560, "One job's path through the four layers")
    xs = [110, 380, 650, 920, 1190]
    names = [("Team", GREEN), ("Hub Kueue", GREEN), ("Worker Kueue", BLUE), ("kube-scheduler", GREY), ("Node pool", RED)]
    for x, (n, c) in zip(xs, names):
        s.box(x - 90, 30, 180, 60, n, [], c)
        s.parts.append(f'<line x1="{x}" y1="90" x2="{x}" y2="520" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5 5"/>')
    steps = [
        (0, 1, 130, "1  create Job, queue label  (held: suspended)", "flow"),
        (1, 1, 175, "LAYER 1: fleet quota? reserve it", "ok"),
        (1, 2, 225, "copy Workload to each candidate worker", "blue"),
        (2, 2, 270, "LAYER 2: queue quota, borrow, preempt: admit ALL pods or none", "ok"),
        (2, 1, 315, "admitted: hub deletes the other copies, mirrors status", "muted"),
        (2, 3, 365, "unsuspend: pods are created (and only now)", "flow"),
        (3, 4, 415, "LAYER 3 and 4: pod unplaced, so a node is bought", "blue"),
        (4, 3, 465, "node joins, pod runs; empty node removed later", "muted"),
    ]
    for a, b, y, label, kind in steps:
        if a == b:
            s.text(xs[a] + 14, y, label, 11.5, COLORS[kind], "700")
        else:
            s.arrow([(xs[a], y), (xs[b], y)], kind, label, (xs[a] + xs[b]) / 2, y - 7)
    return s.render()


def fair_share():
    s = Svg(1420, 520, "Quotas, borrowing and reclaim on one worker")
    s.text(40, 40, "One CPU worker, 4 CPUs in one cohort (nominal share, lending limit, borrowing limit)", 14, INK, "700")
    cols = [("llm", 2, 1, 2, GREEN), ("vision", 1, 1, 2, BLUE), ("research", 1, 1, 4, AMBER)]
    for i, (name, nom, lend, borrow, c) in enumerate(cols):
        x = 60 + i * 300
        s.box(x, 70, 260, 120, f"team {name}", [f"guaranteed (nominal): {nom} CPU", f"may lend at most: {lend} CPU", f"may borrow up to: {borrow} CPU"], c)
    s.box(60, 230, 880, 80, "Step 1: research borrows idle capacity", ["research asks for 3 CPUs: its own 1 + 1 lent by llm + 1 lent by vision. Admitted."], AMBER)
    s.box(60, 330, 880, 80, "Step 2: llm asks for its own 2 CPUs", ["only 1 CPU is free, because llm's other CPU is out on loan. llm is within its guaranteed share."], GREEN)
    s.box(60, 430, 880, 70, "Step 3: reclaim", ["Kueue preempts research's borrowing job and requeues it. llm runs. research waits for capacity to return."], RED)
    s.arrow([(500, 190), (500, 230)], "flow")
    s.arrow([(500, 310), (500, 330)], "flow")
    s.arrow([(500, 410), (500, 430)], "flow")
    s.box(980, 230, 400, 270, "Why this beats static pools", ["Each team has a share it can always get.", "Idle shares are lent out, so nothing sits", "unused while someone is waiting.", "The lending limit keeps a floor for the owner.", "Borrowed work is preempted first, so it", "must be able to resume (checkpoint)."], GREY)
    return s.render()


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a03-1", architecture), ("a03-2", topology), ("a03-3", layers), ("a03-4", fair_share)):
        (out / f"{name}.svg").write_text(fn())
        print("wrote", out / f"{name}.svg")

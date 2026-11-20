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
def architecture():
    s = Svg(1420, 800, "Design 01 implemented on EKS: components and traffic")
    s.zone(250, 50, 500, 730, "TRUSTED CONTROL PLANE", GREEN)
    s.zone(780, 50, 250, 730, "GATEWAY ZONE", BLUE)
    s.zone(1060, 50, 340, 730, "UNTRUSTED: SANDBOX NODE POOL", RED)
    s.box(20, 90, 200, 430, "Agent orchestrator", ["namespace agent-orchestrator", "", "stand-in for the agent loop:", "it makes the API calls an", "orchestrator makes", "", "holds no cloud credentials", "and no Kubernetes access"], GREEN, 1)
    # column 1
    s.box(275, 90, 215, 110, "Tool gateway (MCP)", ["policy per agent, approvals,", "rate limit, revocation,", "session token required"], GREEN, 7)
    s.box(275, 250, 215, 110, "Sandbox Router", ["exec and files by session ID", "sandboxd, or Kubernetes exec", "for the no-network tier"], GREEN, 3)
    s.box(275, 410, 215, 110, "Sandbox Manager", ["creates claims, 3 sessions/tenant", "end time on every claim", "issues the session token"], GREEN, 2)
    s.box(275, 570, 215, 130, "Audit trail", ["every component logs JSON lines", "carrying the same session ID;", "one query joins them (S11)"], GREY, 9)
    # column 2
    s.box(515, 90, 215, 110, "Metrics API (internal)", ["HTTPS; refuses any request", "without a valid credential", "stands for any internal service"], GREEN)
    s.box(515, 410, 215, 110, "Kubernetes API + controller", ["agent-sandbox controller:", "templates, warm pools, claims,", "deletes a claim at its end time"], GREY)
    # gateway zone
    s.box(805, 90, 200, 120, "Egress gateway", ["2 replicas, one per zone", "per-session allowlist", "terminates TLS only where", "it injects a credential"], BLUE, 4)
    s.box(805, 410, 200, 110, "Credential broker", ["short-lived credentials,", "per session and audience,", "900 seconds at most"], BLUE, 5)
    s.box(805, 545, 200, 60, "Network ceiling", ["the gateway reaches 3 domains"], AMBER, 8)
    s.box(805, 650, 200, 100, "Internet", ["pypi.org", "files.pythonhosted.org", "example.com (if approved)"], GREY)
    # untrusted zone
    s.box(1085, 90, 290, 270, "Sandbox: python-untrusted", ["Tier 2: its own microVM and kernel", "network: one route, to the gateway", "no DNS, no token, no secrets", "read-only root, non-root, no caps", "environment holds a placeholder", "sandboxd runs commands and files", "ingress: router pods only"], RED, 6)
    s.box(1085, 390, 290, 120, "Sandbox: compute-only", ["Tier 3: microVM with no network device", "reached through the exec channel", "for pure computation on uploaded files"], RED)
    s.box(1085, 540, 290, 100, "Warm pools", ["python-untrusted: 2 booted, waiting", "compute-only: 1 booted, waiting"], RED)
    s.box(1085, 670, 290, 80, "Dedicated node, own subnets", ["c7i.xlarge, taint workload=sandbox"], RED)
    # flows (each between neighbours, none crossing a box)
    s.arrow([(220, 145), (275, 145)], "flow", "MCP", 247, 138)
    s.arrow([(220, 305), (275, 305)], "flow", "exec", 247, 298)
    s.arrow([(220, 465), (275, 465)], "flow", "create", 247, 458)
    s.arrow([(490, 465), (515, 465)], "flow")
    s.text(502, 452, "claim", 10, INK, "600", "middle")
    s.arrow([(490, 145), (515, 145)], "flow")
    s.arrow([(490, 305), (1085, 305)], "flow", "sandboxd :8080 and :9090 (only the router may connect)", 640, 296)
    s.arrow([(1085, 150), (1005, 150)], "blue", ":3128", 1045, 143)
    s.arrow([(805, 125), (730, 125)], "blue", "inject", 767, 118)
    s.arrow([(905, 210), (905, 410)], "blue", "mint", 905, 372)
    s.arrow([(1005, 170), (1020, 170), (1020, 700), (1005, 700)], "ok", None)
    s.text(1012, 638, "allowlisted hosts only", 10, "#059669", "600", "end")
    s.text(1045, 125, "the only route out", 10, "#1e40af", "600", "middle")
    s.text(20, 560, "Green: trusted. Blue: gateways.", 11, MUTED)
    s.text(20, 578, "Red: where untrusted code runs.", 11, MUTED)
    s.text(20, 596, "Numbers match the component list.", 11, MUTED)
    return s.render()


def topology():
    s = Svg(1420, 800, "EKS topology and instances")
    s.zone(20, 20, 1380, 760, "AWS ACCOUNT, us-east-1, ONE VPC 10.20.0.0/16", GREY)
    s.box(50, 70, 330, 130, "EKS control plane (managed by AWS)", ["Kubernetes 1.37, Auto Mode, API-only access", "endpoint open only to the admin /32", "control-plane logs (api, audit, authenticator)", "to CloudWatch, 7 days"], BLUE)
    s.box(50, 230, 330, 150, "IAM roles (Terraform)", ["cluster role: 5 AWS-managed EKS policies", "platform node role: WorkerNodeMinimal + ECR pull", "sandbox node role: the same two, separate role", "no S3, no Secrets Manager, no inline policies"], AMBER)
    s.box(50, 410, 330, 110, "ECR repository ds01-platform", ["one image for every platform service", "built for linux/amd64, tag = hash of the source", "scan on push, force-delete on teardown"], GREY)
    s.box(50, 550, 330, 200, "Terraform state (local, git-ignored)", ["layer 10-cluster: VPC, subnets, NAT, EKS, IAM", "layer 20-platform: controller, templates,", "pools, gateways, services, secrets, image", "account ID guard on both providers", "teardown: down.sh, then verify-clean.sh"], GREY)
    for i, az in enumerate(("us-east-1a", "us-east-1b")):
        x = 430 + i * 480
        s.zone(x, 60, 460, 700, f"AVAILABILITY ZONE {az}", GREY)
        s.box(x + 20, 100, 420, 100, f"Public subnet 10.20.{i}.0/24", ["NAT gateway (one, in 1a)", "nothing else lives here"] if i == 0 else ["empty in this AZ (single NAT keeps cost down)"], GREY)
        s.box(x + 20, 230, 420, 220, f"Platform subnet 10.20.{16 * (i + 1)}.0/20", ["tag ds01/tier=system", "Auto Mode general-purpose nodes (c6a.large)", "Manager, Router, tool gateway, metrics API,", "egress gateway x2, credential broker x2,", "orchestrator, agent-sandbox controller", "default node security group"], GREEN)
        s.box(x + 20, 480, 420, 250, f"Sandbox subnet 10.20.{64 + 16 * i}.0/20", ["tag ds01/tier=sandbox", "node pool 'sandbox': c7i.xlarge (Nitro Enclaves on)", "taint workload=sandbox:NoSchedule", "security group ds01-sandboxing-sandbox-nodes", "IMDSv2 required, hop limit 1, no public IP", "each pod = one Firecracker microVM, own kernel", "only empty nodes are consolidated"], RED)
    s.arrow([(380, 135), (430, 135)], "muted")
    s.text(30, 790, "Instance types are chosen by Auto Mode inside the limits of the NodePool (c or m, generation 7 or later, 4 or 8 vCPU, x86, on-demand, 32 vCPU cap).", 11, MUTED)
    return s.render()


def network_paths():
    s = Svg(1420, 700, "Every network path out of a sandbox")
    s.zone(40, 60, 380, 580, "UNTRUSTED", RED)
    s.box(70, 100, 320, 190, "Code inside a sandbox", ["own kernel (microVM)", "HTTPS_PROXY = the gateway's fixed address", "no DNS, no service-account token", "env: METRICS_API_KEY=placeholder"], RED)
    s.zone(500, 60, 300, 210, "THE ONE WAY OUT", BLUE)
    s.box(525, 100, 250, 140, "Egress gateway", ["1. who owns this source address?", "2. is the host on that session's list?", "3. rate and byte limits", "4. inject credential, or pass through"], BLUE)
    s.arrow([(390, 170), (525, 170)], "ok", "TCP 3128 only", 458, 160)
    rows = [("Kubernetes API server", "S05 kubernetes_api"), ("Another sandbox (8080, 9090)", "S05 other_sandbox"),
            ("169.254.169.254 credentials", "S05 imds"), ("Internet by address (1.1.1.1)", "S05 direct_internet"),
            ("Internet with the proxy unset", "S05 proxy_bypass"), ("DNS lookups and DNS exfiltration", "S05 dns_exfiltration"),
            ("Host not on the allowlist", "S06 unapproved_host")]
    y = 340
    s.text(60, 316, "Seven attempts that must fail", 12, "#991b1b", "700")
    for label, test in rows:
        s.arrow([(390, 270), (430, y)], "bad", None, dash=True)
        s.x_mark(448, y)
        s.box(480, y - 20, 330, 40, label, [], GREY)
        s.text(830, y + 5, test, 11, MUTED)
        y += 48
    s.zone(950, 60, 430, 300, "WHAT THE GATEWAY MAY REACH", GREEN)
    s.box(975, 100, 380, 70, "Allowlisted internet", ["pypi.org, files.pythonhosted.org (per session)"], GREEN)
    s.box(975, 190, 380, 70, "Internal service, credential injected", ["metrics API over TLS the gateway terminates"], GREEN)
    s.box(975, 280, 380, 60, "Tool gateway (session token)", ["called from the sandbox through the gateway"], GREEN)
    s.arrow([(775, 160), (975, 135)], "ok")
    s.arrow([(775, 170), (975, 225)], "ok")
    s.arrow([(775, 180), (975, 310)], "ok")
    s.box(950, 400, 430, 110, "Ceiling behind the gateway", ["The gateway's own network policy names 3 domains.", "A session allowlist that names anything else is", "refused by the network, not by the gateway (S06)."], AMBER)
    s.box(950, 540, 430, 90, "Ingress into a sandbox", ["Only the router's pods, only ports 8080 and 9090.", "Not even the trusted orchestrator can connect (S05)."], AMBER)
    return s.render()


def credential_flow():
    s = Svg(1420, 520, "How a request gets a credential without the sandbox holding it")
    xs = [110, 380, 650, 920, 1190]
    names = [("Sandbox", RED), ("Egress gateway", BLUE), ("Sandbox Manager", GREEN), ("Credential broker", BLUE), ("Metrics API", GREEN)]
    for x, (n, c) in zip(xs, names):
        s.box(x - 90, 30, 180, 60, n, [], c)
        s.parts.append(f'<line x1="{x}" y1="90" x2="{x}" y2="490" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5 5"/>')
    steps = [
        (0, 1, 130, "1  GET /v1/metrics, Authorization: Bearer placeholder", "ok"),
        (1, 2, 180, "2  which session owns this source address? is the host allowed?", "flow"),
        (2, 1, 215, "session, allowlist, credential binding for this host", "muted"),
        (1, 3, 270, "3  mint a credential: session, audience, 900 s at most", "blue"),
        (3, 1, 305, "short-lived credential (ID logged, value never)", "muted"),
        (1, 4, 360, "4  same request, Authorization replaced, over TLS", "ok"),
        (4, 1, 405, "5  200, and the answer names this session", "muted"),
        (1, 0, 450, "response back to the sandbox. The credential does not go back.", "muted"),
    ]
    for a, b, y, label, kind in steps:
        s.arrow([(xs[a], y), (xs[b], y)], kind, label, (xs[a] + xs[b]) / 2, y - 7)
    return s.render()


def context_overview():
    """Figure 1: the path of one question, in plain terms (user, agent, platform, sandbox, outside world)."""
    s = Svg(1420, 450, "The path of one question: user, agent, platform, sandbox and the outside world")
    s.box(20, 190, 170, 100, "User", ["types in the agent's", "chat window", "(not built here)"], GREY, dash=True)
    s.box(260, 190, 220, 120, "Agent (orchestrator)", ["software in an ordinary pod", "decides what to do next", "and calls the platform"], GREEN)
    s.box(260, 30, 220, 100, "Language model", ["asked what to do next", "(not in this test)"], GREY, dash=True)
    s.box(550, 150, 300, 260, "Platform", ["trusted services the company runs", "Manager: opens and closes sessions", "Router: carries code in, output back", "Tool gateway: tools and approvals", "Egress gateway: the one way out", "Credential broker: short-lived keys", "Controller: keeps sandboxes ready", "Audit log: one line per event"], GREEN)
    s.box(910, 190, 200, 120, "Sandbox", ["its own microVM and kernel", "runs the agent's script", "no secrets inside"], RED)
    s.box(1180, 150, 220, 100, "Company services", ["internal APIs; the test", "uses a metrics API"], GREY)
    s.box(1180, 290, 220, 90, "Internet", ["approved sites only"], GREY)
    s.arrow([(190, 225), (260, 225)], "muted", dash=True)
    s.badge(225, 225, 1)
    s.arrow([(260, 265), (190, 265)], "muted", dash=True)
    s.badge(225, 265, 8)
    s.arrow([(370, 190), (370, 130)], "muted", dash=True, both=True)
    s.badge(370, 160, 2)
    s.arrow([(480, 225), (550, 225)], "flow")
    s.badge(515, 225, 3)
    s.arrow([(480, 265), (550, 265)], "flow")
    s.badge(515, 265, 4)
    s.arrow([(850, 265), (910, 265)], "flow")
    s.badge(880, 265, 5)
    s.arrow([(910, 225), (850, 225)], "blue")
    s.badge(880, 225, 6)
    s.arrow([(850, 170), (1180, 170)], "ok")
    s.badge(1015, 170, 7)
    s.arrow([(850, 350), (1180, 350)], "ok")
    s.badge(1015, 350, 7)
    s.text(20, 360, "Dashed boxes are outside what this appendix builds.", 11, MUTED)
    s.text(20, 378, "Green arrows: calls the platform lets through.", 11, MUTED)
    s.text(20, 396, "Numbers match the steps below the picture.", 11, MUTED)
    return s.render()
def context_sequence():
    """Figure 2: the same request as a sequence of calls between components."""
    s = Svg(1420, 960, "One request as calls between components")
    names = [("User", GREY), ("Agent", GREEN), ("Manager", GREEN), ("Router", GREEN), ("Sandbox", RED),
             ("Egress gateway", BLUE), ("Credential broker", BLUE), ("Tool gateway", GREEN), ("Company API", GREY)]
    xs = [160 + i * 142 for i in range(len(names))]
    first_y, step = 130, 46
    def ry(row):
        return first_y + (row - 1) * step
    bands = [(1, 1, ["ASK"]), (2, 3, ["OPEN A", "SESSION"]), (4, 11, ["RUN THE", "CODE"]), (12, 16, ["USE A", "TOOL"]), (17, 18, ["CLOSE AND", "ANSWER"])]
    for i, (a, b, label) in enumerate(bands):
        top, bottom = ry(a) - step / 2, ry(b) + step / 2
        s.parts.append(f'<rect x="0" y="{top}" width="1420" height="{bottom - top}" fill="{"#f8fafc" if i % 2 == 0 else "#ffffff"}"/>')
        for j, line in enumerate(label):
            s.text(16, (top + bottom) / 2 + j * 14 - (len(label) - 1) * 7 + 4, line, 11, MUTED, "700")
    for x, (n, c) in zip(xs, names):
        s.box(x - 68, 24, 136, 40, n, [], c, dash=(n == "User"))
        s.parts.append(f'<line x1="{x}" y1="64" x2="{x}" y2="935" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5 5"/>')
    # (row, from, to, label, kind, dashed, both)
    rows = [
        (1, 0, 1, "The question", "muted", True, False),
        (2, 1, 2, "Open a session", "flow", False, False),
        (3, 2, 1, "Session ID and token", "muted", True, False),
        (4, 1, 3, "Run this script", "flow", False, False),
        (5, 3, 4, "Run it inside", "flow", False, False),
        (6, 4, 5, "Call the API", "blue", False, False),
        (7, 5, 2, "Whose session? Allowed?", "blue", False, True),
        (8, 5, 6, "Mint a 15-minute key", "blue", False, False),
        (9, 5, 8, "Request with the real key", "ok", False, False),
        (10, 5, 4, "Reply, no key in it", "muted", True, False),
        (11, 3, 1, "Output and exit code", "muted", True, False),
        (12, 1, 7, "Call a tool, with the token", "flow", False, False),
        (13, 7, 2, "Session still active?", "flow", False, True),
        (14, 7, 6, "Mint a 60-second key", "blue", False, False),
        (15, 7, 8, "Read the metric", "ok", False, False),
        (16, 7, 1, "Result", "muted", True, False),
        (17, 1, 2, "Close the session", "flow", False, False),
        (18, 1, 0, "Answer", "muted", True, False),
    ]
    for row, a, b, label, kind, dashed, both in rows:
        y = ry(row)
        sign = 1 if b > a else -1
        start, end = xs[a] + sign * 13, xs[b] - sign * 2
        s.arrow([(start, y), (end, y)], kind, label, (xs[a] + xs[b]) / 2, y - 7, dash=dashed, both=both)
        s.badge(xs[a], y, row)
    s.text(16, 950, "Dashed arrows are replies. Every component also writes one log line per step, carrying the session ID.", 11, MUTED)
    return s.render()
def context_components():
    """Figure 3: components view, where each one runs and which paths exist."""
    s = Svg(1420, 880, "Components view: where each service runs and who calls whom")
    s.box(160, 50, 230, 90, "User", ["chat window, outside this", "appendix"], GREY, dash=True)
    s.box(560, 40, 300, 110, "Agent (orchestrator)", ["pod on the platform nodes", "decides, then calls the platform", "no cloud or Kubernetes access"], GREEN)
    s.box(1030, 50, 230, 90, "Language model", ["a real agent calls one here;", "the stand-in has none"], GREY, dash=True)
    s.box(260, 230, 260, 120, "Sandbox Manager", ["2 replicas, platform nodes", "the only creator of sandboxes", "3 sessions per customer", "issues the session token"], GREEN)
    s.box(580, 230, 260, 120, "Sandbox Router", ["2 replicas, platform nodes", "the only service that may", "connect to a sandbox", "logs every command"], GREEN)
    s.box(900, 230, 260, 120, "Tool gateway (MCP)", ["1 replica, platform nodes", "tools per agent, approvals,", "rate limit, revocation"], GREEN)
    s.box(260, 430, 260, 120, "Kubernetes API + controller", ["agent-sandbox controller", "keeps the warm pools filled", "hands a ready sandbox to a claim", "deletes a claim at its end time"], GREY)
    s.zone(560, 400, 300, 215, "SANDBOX NODES", RED)
    s.box(580, 435, 260, 160, "Sandbox (one per session)", ["its own microVM and kernel", "no secrets, no token, no DNS", "one route out: the egress gateway", "reached only by the router"], RED)
    s.box(900, 430, 260, 120, "Credential broker", ["2 replicas, platform nodes", "short-lived keys, 900 s at most", "only the gateways may ask"], BLUE)
    s.box(580, 680, 260, 120, "Egress gateway", ["2 replicas, platform nodes", "checks the session's allowlist", "injects the real key", "the only way out of a sandbox"], BLUE)
    s.box(1200, 650, 200, 100, "Company services", ["metrics API in the test", "refuses calls without a key"], GREY)
    s.box(1200, 780, 200, 70, "Internet", ["approved sites only"], GREY)
    s.arrow([(390, 95), (560, 95)], "muted", "chat", 475, 98, dash=True, both=True)
    s.arrow([(860, 95), (1030, 95)], "muted", "next step", 945, 98, dash=True, both=True)
    s.arrow([(600, 150), (600, 190), (390, 190), (390, 230)], "flow", "open, close", 495, 193)
    s.arrow([(710, 150), (710, 230)], "flow", "commands", 710, 193)
    s.arrow([(820, 150), (820, 190), (1030, 190), (1030, 230)], "flow", "tool calls", 925, 193)
    s.arrow([(390, 350), (390, 430)], "flow", "claim", 390, 393)
    s.arrow([(710, 350), (710, 435)], "flow", "run, files", 710, 384)
    s.arrow([(1030, 350), (1030, 430)], "blue", "60 s key", 1030, 393)
    s.arrow([(710, 595), (710, 680)], "blue", "only route out", 710, 641)
    s.arrow([(580, 760), (200, 760), (200, 290), (260, 290)], "blue", "whose session?", 200, 525, both=True)
    s.arrow([(810, 680), (810, 630), (1030, 630), (1030, 550)], "blue", "mint key", 920, 633)
    s.arrow([(840, 710), (1200, 710)], "ok", "real key added", 1020, 703)
    s.arrow([(840, 790), (1200, 790)], "ok", "approved sites", 1020, 783)
    s.text(20, 836, "Green: platform services. Blue: gateway and broker. Red: untrusted. Grey: not built here, or outside the platform.", 11, MUTED)
    s.text(20, 854, "Platform pods run on EKS Auto Mode's general-purpose nodes. Sandboxes run only on the tainted sandbox pool.", 11, MUTED)
    return s.render()
def envoy_gateway():
    """Figure for section 9: the egress gateway on Envoy."""
    s = Svg(1420, 500, "Egress gateway on Envoy: one listener, a pass-through tunnel, and a loopback listener that injects the credential")
    s.box(20, 200, 200, 120, "Sandbox", ["HTTPS_PROXY = the gateway", "sends CONNECT host:443", "holds only a placeholder"], RED)
    s.zone(270, 60, 380, 400, "ENVOY (the gateway pod)", BLUE)
    s.box(290, 100, 340, 110, "Listener :3128", ["accepts CONNECT, asks the policy", "service, then opens a tunnel", "no TLS is ended for ordinary hosts"], BLUE)
    s.box(290, 300, 340, 130, "Loopback listener :8443", ["only for the host that needs a key", "ends TLS, adds the Authorization header", "reachable from inside the pod only", "PROXY protocol keeps the sandbox's address"], BLUE)
    s.box(720, 100, 260, 260, "Policy service (gRPC)", ["which session owns this source", "address? is the host on its list?", "over its rate limit?", "returns the session ID, always", "second check: asks the broker and", "returns the Authorization header"], GREEN)
    s.box(720, 405, 260, 60, "Manager and broker", ["lab: a table of sessions"], GREY)
    s.box(1050, 100, 330, 110, "Allowlisted site", ["TLS runs between the sandbox", "and the site. Envoy cannot read it."], GREY)
    s.box(1050, 300, 330, 130, "Internal API", ["sees the real credential,", "never the placeholder", "refuses a call without one"], GREY)
    s.arrow([(220, 255), (255, 255), (255, 155), (290, 155)], "flow")
    s.badge(255, 205, 1)
    s.arrow([(630, 155), (720, 155)], "blue", both=True)
    s.badge(675, 155, 2)
    s.arrow([(460, 100), (460, 40), (1215, 40), (1215, 100)], "ok", "pass-through tunnel", 840, 43)
    s.badge(1100, 40, 3)
    s.arrow([(460, 210), (460, 300)], "blue", "PROXY protocol", 460, 258)
    s.badge(445, 235, 4)
    s.arrow([(630, 330), (720, 330)], "blue", both=True)
    s.badge(675, 330, 5)
    s.arrow([(630, 385), (1050, 385)], "ok", "real key added", 740, 378)
    s.badge(1000, 385, 6)
    s.arrow([(850, 360), (850, 405)], "muted", both=True)
    s.text(20, 380, "Numbers match the text under the picture.", 11, MUTED)
    s.text(20, 398, "Blue: the gateway. Green: its policy service.", 11, MUTED)
    return s.render()
def address_plan():
    """Figure for section 2: one VPC range cut into blocks, and what differs between the platform and sandbox blocks."""
    s = Svg(1420, 740, "One VPC, one address range, cut into public, platform and sandbox blocks")
    s.text(20, 28, "ONE VPC, ONE RANGE: 10.20.0.0/16 (65,536 addresses), cut into blocks. Nothing else is attached to the VPC.", 13, INK, "700")
    x0, width, total = 20, 1380, 65536
    def px(offset):
        return x0 + offset * width / total
    s.parts.append(f'<rect x="{x0}" y="60" width="{width}" height="40" rx="6" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.2"/>')
    s.text(x0 + width - 8, 84, "the rest of the /16 is unassigned", 11, MUTED, "400", "end")
    segs = [(0, 256, GREY), (256, 256, GREY), (4096, 4096, GREEN), (8192, 4096, GREEN), (16384, 4096, RED), (20480, 4096, RED)]
    for off, size, c in segs:
        s.parts.append(f'<rect x="{px(off)}" y="60" width="{size * width / total}" height="40" fill="{c["stroke"]}" fill-opacity=".55" stroke="{c["stroke"]}" stroke-width="1.2"/>')
    s.text(x0, 54, "10.20.0.0", 11, MUTED)
    s.text(x0 + width, 54, "10.20.255.255", 11, MUTED, "400", "end")
    cards = [
        (20, GREY, "Public blocks: 2 x /24", ["10.20.0.0/24 and 10.20.1.0/24, one per zone", "hold the NAT gateway and nothing else", "the only blocks with a route to the internet gateway"]),
        (490, GREEN, "Platform blocks: 2 x /20 (tag ds01/tier=system)", ["10.20.16.0/20 and 10.20.32.0/20, one per zone", "about 4,000 addresses each", "Manager, Router, tool gateway, egress gateway,", "credential broker, controller, orchestrator"]),
        (960, RED, "Sandbox blocks: 2 x /20 (tag ds01/tier=sandbox)", ["10.20.64.0/20 and 10.20.80.0/20, one per zone", "about 4,000 addresses each", "sandbox nodes only, and nothing else lands here", "the untrusted code runs in these"]),
    ]
    for x, c, title, lines in cards:
        s.box(x, 190, 440, 112, title, lines, c)
    s.parts.append(f'<path d="M{px(128)} 100 L{px(128)} 170 L240 170 L240 190" fill="none" stroke="{GREY["stroke"]}" stroke-width="1.6"/>')
    s.parts.append(f'<path d="M{px(8192 + 2048)} 100 L{px(8192 + 2048)} 150 L710 150 L710 190" fill="none" stroke="{GREEN["stroke"]}" stroke-width="1.6"/>')
    s.parts.append(f'<path d="M{px(20480 + 2048)} 100 L{px(20480 + 2048)} 130 L1180 130 L1180 190" fill="none" stroke="{RED["stroke"]}" stroke-width="1.6"/>')
    # comparison grid
    s.text(20, 336, "What differs between the two kinds of private block", 13, INK, "700")
    cols = [(20, 200, ""), (220, 590, "Platform blocks"), (810, 590, "Sandbox blocks")]
    top, head_h, row_h = 350, 34, 46
    for (x, w, label), c in zip(cols, (GREY, GREEN, RED)):
        if label:
            s.parts.append(f'<rect x="{x}" y="{top}" width="{w}" height="{head_h}" fill="{c["fill"]}" stroke="{c["stroke"]}" stroke-width="1.2"/>')
            s.text(x + 12, top + 22, label, 13, c["ink"], "700")
    rows = [
        ("Who lives there", "platform pods, on EKS Auto Mode's general-purpose nodes", "sandbox pods only, on the tainted sandbox node pool"),
        ("How nodes get there", "the cluster's own subnet list names the platform blocks", "a dedicated node class selects blocks by the sandbox tag"),
        ("Security group", "the cluster's default node security group", "its own group, which accepts only the cluster security group"),
        ("Node IAM role", "the platform node role", "a separate sandbox node role, with the same two minimal policies"),
        ("Route table", "one shared private table: all outbound traffic to the NAT gateway", "the same shared table (the split does not change routing)"),
    ]
    for i, (label, a, b) in enumerate(rows):
        y = top + head_h + i * row_h
        for (x, w, _), text, bold in zip(cols, (label, a, b), ("700", "400", "400")):
            s.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{row_h}" fill="{"#ffffff" if i % 2 == 0 else "#f8fafc"}" stroke="#cbd5e1" stroke-width="1"/>')
            s.text(x + 12, y + 28, text, 12, INK, bold)
    ny = top + head_h + len(rows) * row_h + 24
    s.box(20, ny, 1380, 80, "Why two kinds of block", ["The split is what lets the sandbox pool have its own placement, security group and role, so the sandbox node class can only place nodes in the sandbox blocks, never among the platform services.", "It also gives VPC-level rules a range they can target on its own. This build adds none of those rules yet: both private kinds share one route table."], AMBER)
    return s.render()
CHIPS = {"AWS": ("#fff7ed", "#d97706", "#92400e"), "K8s": ("#eff6ff", "#2563eb", "#1e40af"), "Ours": ("#ecfdf5", "#059669", "#065f46")}
def chip(s, x, y, kind):
    fill, stroke, ink = CHIPS[kind]
    w = 14 + len(kind) * 6.6
    s.parts.append(f'<rect x="{x}" y="{y - 12}" width="{w}" height="16" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>')
    s.text(x + w / 2, y, kind, 10.5, ink, "700", "middle")
    return w
def chip_line(s, x, y, text, kinds, size=12):
    s.text(x, y, text, size, INK)
    cx = x + len(text) * 5.85 + 10
    for k in kinds:
        cx += chip(s, cx, y, k) + 5
def defence_in_depth():
    """Figure: seven rings between untrusted code and the outside, with who provides each control."""
    rings = [
        ("1  AWS account and VPC (network level)", [
            ("Sandbox, platform and public blocks; sandbox nodes sit in their own security group", ["AWS", "Ours"]),
            ("That group admits only the cluster group, and the cluster group admits it back", ["AWS", "Ours"]),
            ("No public IP on any node; one shared route table sends outbound traffic to the NAT gateway", ["AWS", "Ours"])]),
        ("2  Access to the cluster and to AWS (identity level)", [
            ("API endpoint open only to the admin address; API-only authentication", ["AWS", "Ours"]),
            ("Cluster, platform-node and sandbox-node IAM roles with managed minimal policies, no inline ones", ["AWS", "Ours"]),
            ("Control-plane audit and authenticator logs go to CloudWatch for 7 days", ["AWS", "Ours"])]),
        ("3  The sandbox node (host level)", [
            ("Own node pool, node class, subnets, security group and role; a taint keeps every other pod off", ["Ours"]),
            ("Metadata service needs IMDSv2 with a hop limit of 1", ["AWS"])]),
        ("4  Kubernetes policy (admission, identity and pod-network level)", [
            ("Pod Security 'restricted' on every namespace; quota and limits on the sandbox namespace", ["K8s", "Ours"]),
            ("Minimal RBAC for the Manager and Router; the sandbox service account has no role", ["K8s", "Ours"]),
            ("Default-deny network policy: a sandbox reaches only the gateway on port 3128, only the router reaches it", ["K8s", "Ours"]),
            ("The cluster's network-policy agent enforces it (EKS Auto Mode ships it switched off; we turn it on)", ["AWS", "Ours"]),
            ("Gateway pods can reach three internet domain names and nothing else (a domain-name policy)", ["AWS", "Ours"])]),
        ("5  The microVM (kernel level)", [
            ("One microVM with its own kernel per pod (Nitro Sandboxes); its metadata address gives placement data only", ["AWS"]),
            ("Every template names the isolated runtime class", ["Ours"])]),
        ("6  The process (application level, inside)", [
            ("Non-root, read-only root, no capabilities, default seccomp, no service-account token", ["K8s", "Ours"]),
            ("The environment holds a placeholder, never a secret", ["Ours"])]),
    ]
    step_x, pad = 28, 14
    head = [22 + len(lines) * 17 + 12 for _, lines in rings]
    tops, y = [], 20
    for h in head:
        tops.append(y); y += h
    inner_h = 52
    bottoms = [0] * len(rings)
    last_bottom = tops[-1] + head[-1] + inner_h + pad
    for i in reversed(range(len(rings))):
        bottoms[i] = last_bottom if i == len(rings) - 1 else bottoms[i + 1] + pad
    H = bottoms[0] + 20
    s = Svg(1420, H, "Defence in depth: six rings between untrusted code and the outside, and who provides each control")
    fills = ["#f8fafc", "#f1f5f9", "#f8fafc", "#f1f5f9", "#f8fafc", "#fef2f2"]
    for i, (title, lines) in enumerate(rings):
        x, w = 20 + step_x * i, 1000 - 2 * step_x * i
        s.parts.append(f'<rect x="{x}" y="{tops[i]}" width="{w}" height="{bottoms[i] - tops[i]}" rx="14" fill="{fills[i]}" stroke="{"#dc2626" if i == 5 else "#94a3b8"}" stroke-width="1.5"/>')
        s.text(x + 14, tops[i] + 20, title, 13, "#991b1b" if i == 5 else INK, "700")
        for j, (t, kinds) in enumerate(lines):
            chip_line(s, x + 14, tops[i] + 40 + j * 17, t, kinds)
    xi = 20 + step_x * 5 + 40
    s.box(xi, tops[5] + head[5] + 6, 1000 - 2 * step_x * 5 - 80, 40, "The agent's script runs here", [], RED)
    # right-hand panel
    rx, rw = 1050, 350
    s.box(rx, 20, rw, 196, "On the one way out (application level)", ["Gateway: allowlist per session, rate limit,", "byte budget, TLS ended for one host only", "Broker: credentials of 15 minutes at most,", "checked by the API for signature and expiry", "Tool gateway: per-agent tools, approvals,", "20 calls a minute, token ends with the session", "Every service: one audit line per event"], GREEN)
    chip(s, rx + rw - 62, 40, "Ours")
    s.box(rx, 236, rw, 112, "Between platform services", ["An ingress network policy on every service", "names who may call it, and on which port", "Bearer tokens between services"], BLUE)
    chip(s, rx + rw - 110, 256, "K8s"); chip(s, rx + rw - 62, 256, "Ours")
    s.box(rx, 368, rw, 112, "Who provides it", [], GREY)
    for k, (kind, text) in enumerate([("AWS", "an AWS service or built-in behaviour"), ("K8s", "a Kubernetes feature that we configured"), ("Ours", "our design: a choice we made or code we wrote")]):
        chip(s, rx + 12, 416 + k * 22, kind)
        s.text(rx + 70, 416 + k * 22, text, 12, INK)
    s.box(rx, 500, rw, 150, "Not restricted in this build", ["Platform pods have no egress network policy,", "except the gateway pods.", "Routing is one shared table for both kinds", "of private block.", "The sandbox group admits all traffic from the", "cluster group; pod policies narrow it."], AMBER)
    return s.render()
def who_may_call_whom():
    """Figure: who may call whom, and what enforces each allowed path."""
    callers = ["Orchestrator", "Router", "Manager", "Tool gateway", "Egress gateway", "Credential broker", "Metrics API", "Sandbox"]
    callees = ["Manager", "Router", "Tool gateway", "Egress gateway", "Credential broker", "Metrics API", "Sandbox", "Kubernetes API", "Internet"]
    allowed = {
        ("Orchestrator", "Manager"): "port 8000", ("Orchestrator", "Router"): "port 8004", ("Orchestrator", "Tool gateway"): "port 8003",
        ("Router", "Sandbox"): "8080, 9090", ("Router", "Kubernetes API"): "RBAC: claims, pods, exec",
        ("Manager", "Kubernetes API"): "RBAC: claims, pods",
        ("Tool gateway", "Manager"): "port 8000", ("Tool gateway", "Credential broker"): "port 8001", ("Tool gateway", "Metrics API"): "port 8443",
        ("Egress gateway", "Manager"): "port 8000", ("Egress gateway", "Tool gateway"): "port 8003", ("Egress gateway", "Credential broker"): "port 8001",
        ("Egress gateway", "Metrics API"): "port 8443", ("Egress gateway", "Internet"): "443, 3 domains",
        ("Sandbox", "Egress gateway"): "port 3128 only",
    }
    open_cells = {(c, "Internet") for c in ("Orchestrator", "Router", "Manager", "Tool gateway", "Credential broker", "Metrics API")}
    s = Svg(1420, 720, "Who may call whom, and what enforces each allowed path")
    lx, top, lw, cw, hh, rh = 20, 60, 180, 133, 54, 54
    s.text(20, 30, "Rows call, columns are called. Green: allowed, with the port or rule that admits it. Grey: refused. Amber: not restricted in this build.", 12.5, INK, "700")
    for j, name in enumerate(callees):
        x = lx + lw + j * cw
        s.parts.append(f'<rect x="{x}" y="{top}" width="{cw}" height="{hh}" fill="#f1f5f9" stroke="#cbd5e1"/>')
        words = name.split(" ")
        lines = [name] if len(name) < 15 else [" ".join(words[:-1]), words[-1]]
        for k, l in enumerate(lines):
            s.text(x + cw / 2, top + 24 + k * 15 - (len(lines) - 1) * 7, l, 12, INK, "700", "middle")
    for i, caller in enumerate(callers):
        y = top + hh + i * rh
        s.parts.append(f'<rect x="{lx}" y="{y}" width="{lw}" height="{rh}" fill="#f1f5f9" stroke="#cbd5e1"/>')
        s.text(lx + 12, y + rh / 2 + 4, caller, 12, INK, "700")
        for j, callee in enumerate(callees):
            x = lx + lw + j * cw
            key = (caller, callee)
            if caller == callee:
                fill, text, ink = "#e2e8f0", "same service", MUTED
            elif key in allowed:
                fill, text, ink = "#d1fae5", allowed[key], "#065f46"
            elif key in open_cells:
                fill, text, ink = "#fef3c7", "no egress policy", "#92400e"
            elif caller == "Sandbox":
                fill, text, ink = "#f8fafc", "refused", "#94a3b8"
            elif callee == "Kubernetes API":
                fill, text, ink = "#f1f5f9", "no token mounted", MUTED
            else:
                fill, text, ink = "#f8fafc", "refused", "#94a3b8"
            s.parts.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{rh}" fill="{fill}" stroke="#cbd5e1"/>')
            lines = text.split(": ") if text.startswith("RBAC") else [text]
            if text.startswith("RBAC"):
                lines = ["RBAC role:", text.split(": ")[1]]
            for k, l in enumerate(lines):
                s.text(x + cw / 2, y + rh / 2 + 4 + k * 15 - (len(lines) - 1) * 7, l, 11.5 if allowed.get(key) else 11, ink, "700" if allowed.get(key) else "400", "middle")
    ny = top + hh + len(callers) * rh + 24
    s.box(20, ny, 450, 130, "What admits a green cell", ["Between services: an ingress network policy on the", "called service, naming the caller and the port.", "Into the sandbox: only the router, on 8080 and 9090.", "Out of the sandbox: only the gateway, on 3128.", "To the Kubernetes API: a small RBAC role."], BLUE)
    s.box(490, ny, 450, 130, "What the grey cells rely on", ["Every called service has an ingress policy, so a", "caller that is not named is refused.", "Pods with no service-account token cannot use the", "Kubernetes API, though nothing blocks the connection.", "A sandbox has no route to anything else."], GREY)
    s.box(960, ny, 440, 130, "What the amber cells mean", ["Only the gateway pods and the sandboxes have an", "egress network policy. Other platform pods are", "limited by what they are written to call, and by", "the absence of credentials, not by a network rule.", "Adding egress policies is a hardening step."], AMBER)
    return s.render()
def life_map():
    """Figure: the eight steps of the life of one session, and the code and manifests behind each."""
    rows = [
        ("1", "Orchestrator", ["asks the Manager for a session:", "user, tenant, agent, template, hosts"],
         ["platform/ds01/orchestrator.py   main(): command create", "platform/ds01/manager.py   route(), _create(): the checks"], GREEN),
        ("2", "Manager", ["checks the tenant limit (three), creates", "the SandboxClaim with an end time"],
         ["platform/ds01/manager.py   TEMPLATES, _create(): quota, claim", "k8s/40-platform.yaml   the Manager's Role (claims, pods)"], GREEN),
        ("3", "Controller, Manager", ["a booted pod is adopted; the Manager", "waits for Ready and mints the token"],
         ["terraform/20-platform/manifests.tf   the pinned controller", "k8s/20-sandboxes.yaml   SandboxWarmPool", "platform/ds01/manager.py, common.py   wait loop, mint()"], BLUE),
        ("4", "Orchestrator, Router", ["commands and files go through the", "Router into the sandbox"],
         ["platform/ds01/router.py   _resolve(), _exec(), _files(), _kube_exec()", "k8s/20-sandboxes.yaml   sandboxd command; Tier 3 annotation"], GREEN),
        ("5", "Sandbox, egress gateway", ["the only way out; allowlist; TLS passed", "through, or ended and a key injected"],
         ["k8s/20-sandboxes.yaml   proxy variables and the egress rule", "k8s/30-egress.yaml   fixed Service address, mitmdump", "platform/ds01/gateway_addon.py, manager.py   session(), by-ip"], BLUE),
        ("6", "Tool gateway", ["session token, policy, approvals,", "rate limit, then the tool runs"],
         ["platform/ds01/tool_gateway.py   route(), _rpc(), _run()", "platform/ds01/manager.py   the session-active lookup", "platform/ds01/broker.py   mint"], GREEN),
        ("7", "Every service", ["one JSON line per event, with the", "session ID; one query joins them"],
         ["platform/ds01/common.py   audit()", "scripts/audit-query.py   collect()", "the ds01/component label on every Deployment"], GREY),
        ("8", "Manager, controller", ["delete, or the end time passes: claim,", "pod and workspace go; token is dead"],
         ["platform/ds01/manager.py   _delete(), _internal(); lifecycle in _create()", "the controller enforces shutdownTime with shutdownPolicy Delete"], RED),
    ]
    row_h, top = 74, 56
    s = Svg(1420, top + len(rows) * row_h + 40, "The eight steps of one session and the code behind each")
    cols = [(20, 44, "Step"), (64, 210, "Who acts"), (274, 330, "What happens"), (604, 796, "Code and manifests that make it happen")]
    for x, w, label in cols:
        s.parts.append(f'<rect x="{x}" y="20" width="{w}" height="32" fill="#f1f5f9" stroke="#cbd5e1"/>')
        s.text(x + 10, 41, label, 12.5, INK, "700")
    for i, (n, who, what, code, c) in enumerate(rows):
        y = top + i * row_h
        for x, w, _ in cols:
            s.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{row_h}" fill="{"#ffffff" if i % 2 == 0 else "#f8fafc"}" stroke="#cbd5e1"/>')
        s.badge(42, y + row_h / 2, n)
        s.parts.append(f'<rect x="64" y="{y}" width="6" height="{row_h}" fill="{c["stroke"]}"/>')
        s.text(80, y + row_h / 2 + 4, who, 12.5, c["ink"], "700")
        for k, line in enumerate(what):
            s.text(284, y + 30 + k * 17, line, 12, INK)
        for k, line in enumerate(code):
            s.text(614, y + 24 + k * 17, line, 11.5, INK)
    return s.render()
def claim_lifecycle():
    """Figure: what happens to the claim, the pod, the pool and the token over the life of a session."""
    stages = ["Before", "1  Claim created", "2  Ready", "3  Active", "4  Ended"]
    lanes = [
        ("Warm pool", ["2 booted pods wait", "(python-untrusted-pool)"], ["unchanged"], ["one pod is adopted;", "the pool boots a replacement"], ["refilled in the background"], ["unchanged"]),
        ("SandboxClaim", ["does not exist"], ["created by the Manager:", "pool, allowlist, end time,", "policy Delete"], ["condition Ready; names", "its sandbox (about 0.4 s,", "10 s or more if cold)"], ["labels name the tenant,", "agent and template"], ["deleted by the Manager,", "or by the controller at", "the end time"]),
        ("Sandbox pod", ["booted and idle,", "in the pool"], ["not touched yet"], ["now belongs to the claim"], ["runs commands; its node is", "never consolidated away"], ["deleted with the claim,", "and its workspace with it"]),
        ("Session token", ["does not exist"], ["does not exist"], ["minted: session, tenant,", "agent, user, template;", "lives as long as the session"], ["checked on every tool call"], ["still unexpired, but the", "Manager no longer finds", "the session: refused"]),
        ("What the gateways see", ["nothing"], ["nothing"], ["the egress gateway can map", "the pod's address to the", "session"], ["allowlist and rate limit", "applied for this session"], ["address maps to no session;", "tool gateway: session is", "no longer active"]),
    ]
    s = Svg(1420, 600, "The life of a SandboxClaim: pool, claim, pod, token and what the gateways see")
    x0, cw, gap, top, rh = 200, 232, 10, 60, 100
    for j, name in enumerate(stages):
        x = x0 + j * (cw + gap)
        s.parts.append(f'<rect x="{x}" y="16" width="{cw}" height="34" rx="6" fill="#1e293b"/>')
        s.text(x + cw / 2, 38, name, 13, "#ffffff", "700", "middle")
    palette = [GREEN, BLUE, GREEN, AMBER, RED]
    for i, lane in enumerate(lanes):
        y = top + i * rh
        s.parts.append(f'<rect x="20" y="{y}" width="170" height="{rh - 8}" rx="8" fill="#f1f5f9" stroke="#cbd5e1"/>')
        s.text(32, y + (rh - 8) / 2 + 4, lane[0], 12.5, INK, "700")
        for j, lines in enumerate(lane[1:]):
            x = x0 + j * (cw + gap)
            c = palette[j]
            dim = lines in (["does not exist"], ["unchanged"], ["nothing"], ["not touched yet"])
            s.parts.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{rh - 8}" rx="8" fill="{"#f8fafc" if dim else c["fill"]}" stroke="{"#cbd5e1" if dim else c["stroke"]}" stroke-width="1.3"/>')
            for k, line in enumerate(lines):
                s.text(x + 12, y + 28 + k * 17, line, 12, MUTED if dim else INK)
    ny = top + len(lanes) * rh + 6
    s.text(20, ny + 14, "Tested in S04 (warm pool, cold start, refill), S08 (a token stops working when its session ends) and S10 (the claim and its sandbox disappear at the end time).", 11.5, MUTED)
    return s.render()
if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a02-1", architecture), ("a02-2", topology), ("a02-3", network_paths), ("a02-4", credential_flow),
                     ("a02-5", context_overview), ("a02-6", context_sequence), ("a02-7", context_components),
                     ("a02-8", envoy_gateway), ("a02-9", address_plan), ("a02-10", defence_in_depth), ("a02-11", who_may_call_whom), ("a02-12", life_map), ("a02-13", claim_lifecycle)):
        (out / f"{name}.svg").write_text(fn())
        print("wrote", out / f"{name}.svg")

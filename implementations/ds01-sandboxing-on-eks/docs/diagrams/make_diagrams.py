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


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a02-1", architecture), ("a02-2", topology), ("a02-3", network_paths), ("a02-4", credential_flow)):
        (out / f"{name}.svg").write_text(fn())
        print("wrote", out / f"{name}.svg")

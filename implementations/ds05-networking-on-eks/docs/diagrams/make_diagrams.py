#!/usr/bin/env python3
"""Generates the architecture diagrams for the Design 05 write-up as SVG (same visual language as the design's own pictures).

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
    s = Svg(1420, 560, "Who uses the network and what it connects")
    s.zone(20, 30, 270, 500, "WHO", GREEN)
    s.box(40, 70, 230, 110, "Network team", ["owns addresses, the core", "network and the policy that", "says who may talk to whom"], GREEN)
    s.box(40, 200, 230, 110, "Platform team", ["owns the clusters, the pod", "network, DNS and the load", "balancers"], GREEN)
    s.box(40, 330, 230, 120, "Application teams", ["publish a service, name it,", "say who may call it; never", "write an IP address"], GREEN)
    s.zone(320, 30, 700, 500, "WHAT THE NETWORK CONNECTS (ONE POLICY, TWO REGIONS)", AMBER)
    s.box(340, 70, 320, 100, "Production clusters", ["EKS in us-east-1 and eu-west-1", "pods on their own range,", "internal load balancers"], BLUE)
    s.box(680, 70, 320, 100, "Shared services", ["registry and DNS endpoints", "reachable from prod, non-prod", "and the data center"], BLUE)
    s.box(340, 190, 320, 100, "Non-production", ["development and test VPCs", "can reach shared services,", "never production"], BLUE)
    s.box(680, 190, 320, 100, "Data center (stand-in)", ["a VPC with its own DNS", "server: the on-premises racks", "of the design"], BLUE)
    s.box(340, 310, 320, 100, "Sandbox", ["can reach nothing and be", "reached by nothing"], RED)
    s.box(680, 310, 320, 100, "Legacy VPC", ["reuses an address range of", "production; not on the core", "network at all"], RED)
    s.box(340, 430, 660, 80, "The core network, IPAM and the policy are code in Git", ["a change is a pull request, a preview and an apply; a rollback is a revert"], GREY)
    s.zone(1050, 30, 350, 500, "WHAT IT IS NOT", GREY)
    s.box(1070, 70, 310, 130, "Not built here", ["a second cloud and its private", "interconnect, Direct Connect,", "hybrid nodes, a service mesh,", "firewall inspection"], GREY, dash=True)
    s.box(1070, 230, 310, 120, "Built as stand-ins", ["the data center is a VPC with", "an EC2 host that runs a DNS", "server", "the legacy VPC is a plain VPC"], AMBER)
    s.arrow([(270, 125), (320, 125)], "flow")
    s.arrow([(270, 255), (320, 255)], "flow")
    s.arrow([(270, 390), (320, 390)], "flow")
    return s.render()


def topology():
    s = Svg(1420, 760, "Two regions, one core network, seven VPCs")
    s.zone(20, 20, 1380, 720, "AWS ACCOUNT", GREY)
    s.box(560, 60, 300, 120, "Core network (Cloud WAN)", ["one policy, an edge in each region", "segments: prod, nonprod, shared,", "hybrid, sandbox"], AMBER)
    s.zone(40, 215, 650, 500, "us-east-1 (main)", BLUE)
    s.box(60, 260, 300, 120, "prod-main", ["EKS cluster, 4 nodes in 3 zones", "10.60.0.0/16 from IPAM", "pods on 100.64.0.0/16, NAT"], GREEN)
    s.box(380, 260, 290, 120, "shared-main", ["test host, private DNS zone", "DNS resolver endpoints", "10.61.1.0/24 from IPAM"], GREEN)
    s.box(60, 400, 300, 100, "nonprod-main", ["test host, 10.61.0.0/24"], GREEN)
    s.box(380, 400, 290, 100, "legacy-main", ["test host, 10.60.0.0/16 by hand", "not on the core network"], RED)
    s.box(60, 520, 610, 90, "Also in this region", ["VPC Lattice service network with the Gateway API controller", "internal application load balancer, S3 gateway endpoint, NAT gateway"], GREY)
    s.zone(730, 215, 650, 500, "eu-west-1 (second)", BLUE)
    s.box(750, 260, 300, 120, "prod-second", ["EKS cluster, 3 nodes in 3 zones", "10.62.0.0/16 from IPAM", "Cilium chained behind the VPC CNI"], GREEN)
    s.box(1070, 260, 290, 120, "hybrid-second", ["stand-in for the data center", "a DNS server (dnsmasq)", "10.63.0.0/24 from IPAM"], GREEN)
    s.box(750, 400, 300, 100, "sandbox-second", ["test host, 10.63.1.0/24"], GREEN)
    s.box(1070, 400, 290, 100, "Also in this region", ["internal load balancer,", "outbound DNS resolver endpoint"], GREY)
    s.arrow([(210, 260), (590, 180)], "blue")
    s.arrow([(525, 260), (680, 180)], "blue")
    s.arrow([(900, 260), (790, 180)], "blue")
    s.arrow([(1215, 260), (840, 180)], "blue")
    s.text(60, 630, "nonprod-main and sandbox-second are attached the same way (an attachment tagged with its segment). legacy-main is not attached.", 12, INK)
    s.text(60, 650, "Why eu-west-1 and not us-west-2: the account already had eight VPCs in us-west-2 (limit ten); this region had room.", 12, INK)
    s.text(60, 672, "The two cluster VPCs both use 100.64.0.0/16 for pods. The legacy VPC copies production's range on purpose.", 12, INK)
    return s.render()


def architecture():
    s = Svg(1420, 800, "Design 05 as layers: what runs in each")
    layers = [
        ("TRANSPORT", AMBER, [("Cloud WAN", ["one policy, segments,", "tag-based attachments"]), ("VPC route tables", ["one route to the plan's", "range, nothing else"]), ("S3 gateway endpoint", ["keeps S3 off the NAT"]), ("NAT gateways", ["one per cluster VPC"])]),
        ("ADDRESSES AND DNS", GREY, [("IPAM, advanced tier", ["9 pools: region, then", "environment"]), ("Private zone ds05.internal", ["shared with 4 VPCs", "latency-routed app name"]), ("Resolver endpoints", ["1 inbound, 2 outbound,", "forwarding rules"]), ("Data center DNS", ["dnsmasq: corp.dc.test"])]),
        ("CLUSTER AND HOST", BLUE, [("VPC CNI", ["custom networking, prefix", "delegation, policy agent"]), ("Cilium 1.20 (chained)", ["names in policy, flow", "visibility"]), ("NodeLocal DNSCache", ["on every node"]), ("Managed node groups", ["one per zone, 110 pods", "per node"])]),
        ("SERVICE CONNECTIVITY AND EDGE", GREEN, [("VPC Lattice", ["service network, IAM auth", "by the service's owner"]), ("Gateway API controller", ["HTTPRoute -> Lattice", "service"]), ("Load Balancer Controller", ["internal ALB, IP targets,", "readiness gates"]), ("Services", ["PreferClose keeps calls", "in the zone"])]),
        ("POLICY PLANE", RED, [("Segments", ["who shares routes"]), ("Security groups", ["the pods' group admits", "the cluster only"]), ("ClusterNetworkPolicy", ["Admin and Baseline tiers"]), ("NetworkPolicy", ["each team, inside the", "guardrails"])]),
    ]
    for r, (title, c, boxes) in enumerate(layers):
        y = 20 + r * 155
        s.zone(20, y, 1380, 140, title, c)
        for i, (t, lines) in enumerate(boxes):
            s.box(40 + i * 340, y + 35, 320, 95, t, lines, c)
    return s.render()


def address_plan():
    s = Svg(1420, 620, "The address plan: one top range, a pool per region and environment")
    s.box(560, 30, 300, 80, "all routed addresses", ["10.60.0.0/14"], AMBER)
    s.box(230, 150, 300, 80, "us-east-1  10.60.0.0/15", ["pool, locale us-east-1"], AMBER)
    s.box(890, 150, 300, 80, "eu-west-1  10.62.0.0/15", ["pool, locale eu-west-1"], AMBER)
    s.arrow([(640, 110), (380, 150)], "flow")
    s.arrow([(780, 110), (1040, 150)], "flow")
    boxes = [(20, "main-prod 10.60.0.0/16", ["VPC prod-main"]), (235, "main-nonprod 10.61.0.0/24", ["VPC nonprod-main"]), (450, "main-shared 10.61.1.0/24", ["VPC shared-main"]),
             (740, "second-prod 10.62.0.0/16", ["VPC prod-second"]), (955, "second-hybrid 10.63.0.0/24", ["VPC hybrid-second"]), (1170, "second-sandbox 10.63.1.0/24", ["VPC sandbox-second"])]
    for x, t, lines in boxes:
        s.box(x, 290, 205, 80, t, lines, GREEN)
    for x, _, _ in boxes[:3]:
        s.arrow([(380, 230), (x + 102, 290)], "flow")
    for x, _, _ in boxes[3:]:
        s.arrow([(1040, 230), (x + 102, 290)], "flow")
    s.box(40, 400, 520, 110, "Outside the plan, on purpose", ["pods: 100.64.0.0/16 in both cluster VPCs (never routed for pods' own traffic)", "legacy-main: 10.60.0.0/16 typed by hand, the same range as prod-main", "IPAM finds both and reports them as unmanaged and overlapping"], RED)
    s.box(600, 400, 440, 110, "What the plan refuses", ["a request for a range inside a block that a VPC holds", "(S01), and a VPC whose range would overlap another", "routed VPC: it cannot be given one"], GREY)
    s.text(40, 560, "A pool cannot hand out what its parent did not give it, and an allocation is held until the VPC is deleted.", 12, INK, "700")
    return s.render()


def request_path():
    s = Svg(1420, 600, "A request, layer by layer: the name, the load balancer, the pod, and the exported service")
    s.box(20, 60, 180, 80, "Caller", ["a pod or a host"], GREEN)
    s.box(250, 60, 240, 80, "DNS: app.ds05.internal", ["two latency records,", "alias to each load balancer"], AMBER)
    s.box(540, 60, 240, 80, "Internal load balancer", ["IP targets, health check", "every 5 s, drains 30 s"], BLUE)
    s.box(830, 60, 240, 80, "Service: web", ["PreferClose: same zone first", "falls back if none ready"], BLUE)
    s.box(1120, 60, 260, 80, "Pod", ["traefik/whoami, 100.64.x.x", "readiness gate from the ALB"], GREEN)
    for x1, x2 in ((200, 250), (490, 540), (780, 830), (1070, 1120)):
        s.arrow([(x1, 100), (x2, 100)], "flow")
    s.text(250, 170, "A region whose load balancer has no healthy target is dropped from the answer (S07).", 12, INK)
    s.box(20, 230, 240, 90, "Caller in another VPC", ["no route to prod, or an", "overlapping address (legacy)"], RED)
    s.box(310, 230, 260, 90, "VPC Lattice service", ["web-shop-...vpc-lattice-svcs", "service network, regional"], AMBER)
    s.box(620, 230, 250, 90, "Auth policy of the service", ["names the roles that may call", "(owned by the team)"], AMBER)
    s.box(920, 230, 220, 90, "Target group", ["the web pods"], BLUE)
    s.box(1190, 230, 190, 90, "Pod", ["sees the caller's identity", "in a header"], GREEN)
    for x1, x2 in ((260, 310), (570, 620), (870, 920), (1140, 1190)):
        s.arrow([(x1, 275), (x2, 275)], "flow")
    s.text(310, 350, "Both policies must allow: the caller's own, and the service's. The caller's team cannot grant itself access (S06).", 12, INK)
    s.box(20, 400, 1360, 160, "What the two paths have in common", ["Neither asks the caller for an address: the first uses a name and DNS, the second a name and a service network.", "The first needs a route, so it is limited by the core network's segments. The second needs no route, so it works across overlapping ranges and across segments,", "and what limits it is the service's own policy.", "Bulk data between clusters takes neither: it is plain routed traffic over the core network."], GREY)
    return s.render()


def isolation():
    s = Svg(1420, 620, "Isolation in layers: a path is open only if no layer denies it")
    layers = [("Segments", "the core network has no route between prod and nonprod", "S02, S03", AMBER), ("VPC route tables", "a VPC sends only the plan's range to the core network", "S04", AMBER),
              ("Security groups", "the pods' group admits the cluster, not other VPCs", "S04", BLUE), ("ClusterNetworkPolicy, Admin tier", "guardrails: no instance metadata, payments only from named namespaces", "S08", RED),
              ("NetworkPolicy", "each team opens what it needs, inside the guardrails", "S08", GREEN), ("ClusterNetworkPolicy, Baseline tier", "defaults a team can override", "S08", GREEN), ("Workload identity", "Lattice auth: the service names who may call it", "S06", GREY),
              ("Egress by name", "Cilium: a pod may reach one domain and no other", "S08", BLUE)]
    for i, (t, d, ref, c) in enumerate(layers):
        x = 20 + (i % 2) * 700
        y = 30 + (i // 2) * 135
        s.box(x, y, 680, 115, f"{i + 1}  {t}   ({ref})", [d], c)
    s.text(20, 590, "A path is open only if no layer on it denies it. S04 opens two of them, one at a time, and shows that neither alone is enough.", 12, INK, "700")
    return s.render()


def dns_diagram():
    s = Svg(1420, 560, "DNS: two zones, two directions")
    s.zone(20, 30, 760, 500, "AWS", AMBER)
    s.box(40, 70, 330, 90, "Private zone ds05.internal", ["registry.ds05.internal, app.ds05.internal", "associated with four VPCs"], GREEN)
    s.box(40, 190, 330, 90, "Cluster pods", ["cluster DNS, answered by the cache", "on the node; the rest to the VPC resolver"], BLUE)
    s.box(400, 70, 360, 90, "Inbound resolver endpoint", ["two addresses in shared-main", "answers the data center's questions"], BLUE)
    s.box(400, 190, 360, 90, "Outbound resolver endpoints", ["one per region; the rule for", "corp.dc.test forwards to the data center"], BLUE)
    s.box(40, 310, 720, 90, "Resolver rules are regional", ["the rule in us-east-1 serves prod-main and shared-main; the one in eu-west-1 serves prod-second", "a VPC without an association does not resolve the data center's names (S09)"], GREY)
    s.zone(830, 30, 560, 500, "DATA CENTER (STAND-IN)", RED)
    s.box(850, 70, 520, 110, "DNS server (dnsmasq on an EC2 host)", ["answers for corp.dc.test", "forwards ds05.internal to the inbound endpoint", "its log shows who asked"], RED)
    s.box(850, 220, 520, 90, "Clients in the data center", ["use the server above and never the VPC resolver", "(the zone is not associated with that VPC)"], RED)
    s.arrow([(760, 235), (805, 235), (805, 150), (850, 150)], "flow", "corp.dc.test", 806, 200)
    s.arrow([(850, 100), (760, 100)], "flow", "ds05.internal", 805, 92)
    s.arrow([(370, 115), (400, 115)], "muted")
    return s.render()


def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a06-1", context), ("a06-2", topology), ("a06-3", architecture), ("a06-4", address_plan), ("a06-5", request_path), ("a06-6", isolation), ("a06-7", dns_diagram)):
        (out / f"{name}.svg").write_text(fn())


if __name__ == "__main__":
    main()

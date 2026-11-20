#!/usr/bin/env python3
"""Generates the architecture diagrams for the Design 04 write-up as SVG (same visual language as the design's own pictures).

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
    s = Svg(1420, 560, "Who starts, steers and fixes a run, and what the platform talks to")
    s.zone(20, 30, 270, 500, "WHO", GREEN)
    s.box(40, 70, 230, 110, "Developer", ["files a ticket;", "wants a pull request, not", "a half-finished job"], GREEN)
    s.box(40, 200, 230, 110, "Reviewer", ["reads the pull request,", "comments, approves; may", "take the whole weekend"], GREEN)
    s.box(40, 330, 230, 120, "On-call engineer", ["pauses runs during an", "incident, rewinds or forks one,", "ships new agent code"], GREEN)
    s.zone(320, 30, 760, 500, "THE PLATFORM (EKS)", AMBER)
    s.box(340, 70, 220, 90, "Session API", ["start, signal, rewind, fork,", "timeline, bulk operations"], BLUE)
    s.box(590, 70, 470, 90, "Durable execution engine (Temporal) + database", ["one workflow per run; its event history is the source of truth;", "timers, retries, signals; no state in any worker"], AMBER)
    s.box(340, 190, 220, 110, "Agent workers", ["stateless; any worker can", "continue any run; scale on the", "engine's task backlog"], BLUE)
    s.box(590, 190, 220, 110, "Model gateway", ["retries, fallback, token", "budget per run, a record of", "every call"], BLUE)
    s.box(840, 190, 220, 110, "Tool gateway + ledger", ["classes, idempotency keys,", "look-before-you-leap,", "undo (compensation)"], BLUE)
    s.box(340, 330, 220, 110, "Sandbox manager", ["one sandbox pod per run,", "started from the last", "snapshot"], BLUE)
    s.box(590, 330, 470, 110, "State stores", ["session database: context log, summaries, snapshots index, ledger", "object storage: workspace snapshots (content-addressed)", "engine database: histories"], GREY)
    s.box(340, 460, 720, 55, "Every component above is stateless except the three stores", [], GREY, dash=True)
    s.zone(1110, 30, 290, 500, "OUTSIDE THE PLATFORM", RED)
    s.box(1130, 70, 250, 110, "Model provider", ["primary and fallback;", "fails for minutes at a time", "(stand-in, scripted)"], RED)
    s.box(1130, 210, 250, 130, "Git host", ["branches, pull requests,", "comments; can lose its", "answer after acting", "(stand-in)"], RED)
    s.box(1130, 370, 250, 100, "Object storage (S3)", ["snapshots, one bucket"], GREY)
    s.arrow([(270, 125), (340, 115)], "flow")
    s.arrow([(270, 255), (320, 255), (320, 175), (340, 120)], "flow")
    s.arrow([(270, 390), (320, 390), (320, 130), (340, 105)], "flow")
    s.arrow([(560, 115), (590, 115)], "flow")
    s.arrow([(450, 160), (450, 190)], "flow")
    s.arrow([(560, 245), (590, 245)], "flow")
    s.arrow([(810, 245), (840, 245)], "flow")
    s.arrow([(700, 190), (700, 175), (1100, 175), (1130, 130)], "muted")
    s.arrow([(1060, 245), (1130, 270)], "flow")
    s.arrow([(450, 300), (450, 330)], "flow")
    s.arrow([(560, 385), (590, 385)], "muted")
    s.arrow([(1060, 400), (1130, 410)], "muted")
    return s.render()


def topology():
    s = Svg(1420, 720, "One EKS cluster in three zones, a Multi-AZ database, and object storage")
    s.zone(20, 20, 1380, 680, "AWS ACCOUNT, us-east-1, ONE VPC 10.32.0.0/16", GREY)
    s.box(50, 70, 330, 140, "EKS Auto Mode cluster", ["ds04-agentstate, Kubernetes 1.37", "API private in the VPC; public only to", "the administrator's /32", "audit logs 3 days"], BLUE)
    s.box(50, 230, 330, 130, "Managed data services", ["RDS PostgreSQL 16, db.t4g.small, Multi-AZ", "(stands in for Aurora PostgreSQL)", "S3 bucket: workspace snapshots", "ECR repository: the one agent image"], GREY)
    s.box(50, 380, 330, 150, "Namespaces", ["temporal: the engine (frontend, history,", "matching, worker, web)", "agent-platform: session API, gateways,", "workers, sandbox manager, tester", "sandboxes | external (git host) | monitoring | keda"], AMBER)
    s.box(50, 550, 330, 130, "Terraform (two layers, local state)", ["10-cluster: VPC, NAT, IAM, cluster, RDS, ECR", "20-platform: namespaces, S3, identities, Helm", "releases, deployments", "account guard; down.sh then verify-clean.sh"], GREY)
    for i, z in enumerate(["1a", "1b", "1c"]):
        x = 410 + i * 330
        s.zone(x, 60, 320, 400, f"us-east-1{z[-1]}", GREY)
        s.box(x + 10, 100, 300, 60, "Private subnet /20", ["Auto Mode nodes, on demand"], GREEN)
        s.box(x + 10, 180, 300, 110, "Engine pods", ["frontend, history, matching:", "two replicas each, spread over zones"] if i < 2 else ["(spread over zones when scheduled)"], AMBER)
        s.box(x + 10, 310, 300, 130, "Workers and gateways", ["workers (2 to 8, KEDA), gateways,", "session API, sandbox manager,", "one sandbox pod per run"] if i < 2 else ["(same, as the scheduler places them)"], BLUE)
    s.box(410, 480, 320, 90, "RDS primary", ["us-east-1a or 1b at any time; a synchronous", "standby in the other"], RED)
    s.box(740, 480, 320, 90, "RDS standby", ["takes over on failover (S10 forces it)", "the endpoint name does not change"], RED)
    s.box(1070, 480, 310, 90, "NAT gateway", ["one; image pulls, public APIs"], GREY)
    s.text(410, 610, "Why Multi-AZ RDS and not Aurora: the same shape for what the tests need (a synchronous standby in another zone,", 12, INK, "700")
    s.text(410, 630, "one endpoint name that follows the primary), created in about ten minutes. Aurora's cluster storage is not built.", 12, INK)
    s.text(410, 660, "Auto Mode nodes are not listed by EC2 to the account; the cluster and its pods are the view that counts.", 12, INK)
    return s.render()


def architecture():
    s = Svg(1420, 800, "Design 04 on EKS: the five layers")
    layers = [
        ("CLIENTS", GREEN, [("Session API", ["start, signal, fork, timeline,", "pause-all, resume-all"]), ("Tester pod", ["the test scenarios' own shell"]), ("Reviewer and Git host", ["the external system an agent", "changes (stand-in)"]), ("Operator", ["queue switch, bulk pause", "replay tests"])]),
        ("DURABLE EXECUTION", AMBER, [("Temporal 1.32 (Helm 1.8)", ["frontend, history, matching,", "worker; two replicas each"]), ("Workflow: CodingAgentRun", ["the agent loop; pause, budgets,", "loop detection, waiting"]), ("Workers v1 and v2", ["queues agent-v1, agent-v2", "graceful stop, 6 slots each"]), ("KEDA 2.21", ["scales workers on the", "queue's backlog"])]),
        ("GATEWAYS AND SANDBOXES", BLUE, [("Model gateway", ["fallback, token budget,", "a record of every call"]), ("Tool gateway", ["classes, ledger, keys,", "compensation"]), ("Sandbox manager", ["pod per run, restore", "from a snapshot"]), ("Sandboxes", ["executor: run, write, list,", "snapshot, restore"])]),
        ("STATE", GREY, [("Engine database", ["temporal, temporal_visibility", "RDS Multi-AZ"]), ("Session database", ["context_log, summaries, snapshots,", "ledger, model_calls (same RDS)"]), ("S3", ["snapshots/objects/<hash>", "snapshots/manifests/<id>"]), ("Stand-in model", ["scripted, a pure function of", "the prompt"])]),
        ("OBSERVABILITY", RED, [("Prometheus", ["worker metrics from pod", "annotations"]), ("Run timeline", ["built from the history,", "plus cost from the gateway"]), ("Ledger counts", ["attempts, deduplicated"]), ("Replay tests", ["recorded histories through", "new code"])]),
    ]
    for r, (title, c, boxes) in enumerate(layers):
        y = 20 + r * 155
        s.zone(20, y, 1380, 140, title, c)
        for i, (t, lines) in enumerate(boxes):
            s.box(40 + i * 340, y + 35, 320, 95, t, lines, c)
    return s.render()


def replay_flow():
    s = Svg(1420, 700, "One step, a worker that dies, and the replay that follows")
    xs = [110, 360, 610, 860, 1110]
    names = [("Worker A", BLUE), ("Engine + history", AMBER), ("Worker B", BLUE), ("Model gateway", GREY), ("Tool gateway", GREY)]
    for x, (n, c) in zip(xs, names):
        s.box(x - 80, 20, 160, 50, n, [], c)
        s.parts.append(f'<line x1="{x}" y1="70" x2="{x}" y2="680" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="5 5"/>')
    steps = [
        (1, 0, 105, "1  task: run the workflow", "flow"),
        (0, 1, 140, "2  schedule call_model (step 4)", "flow"),
        (0, 3, 178, "3  call, prompt rebuilt from the log", "blue"),
        (3, 0, 214, "4  answer; call recorded", "ok"),
        (0, 1, 250, "5  result goes into the history", "ok"),
        (0, 1, 290, "6  schedule run_tool (step 4): tests, 40 s", "flow"),
        (0, 4, 326, "7  heartbeat while the tool runs", "muted"),
        (0, 0, 372, "worker A is killed, no warning", "bad"),
        (1, 1, 408, "8  no heartbeat: the attempt times out", "bad"),
        (1, 2, 446, "9  new task for any worker", "flow"),
        (2, 1, 484, "10 replay: earlier steps return recorded results", "ok"),
        (2, 3, 520, "no model call is made again for any of them", "ok"),
        (2, 1, 560, "11 run_tool (step 4) has no result: run it", "flow"),
        (2, 4, 598, "12 same step, same idempotency key", "blue"),
        (2, 1, 636, "13 result recorded, attempt 2", "ok"),
    ]
    for a, b, y, label, kind in steps:
        if a == b:
            s.text(xs[a] + 14, y, label, 11.5, COLORS[kind], "700")
        elif label.startswith("no model call"):
            s.text(xs[2] + 14, y, label, 11.5, COLORS[kind], "700")
        else:
            s.arrow([(xs[a], y), (xs[b], y)], kind, label, (xs[a] + xs[b]) / 2, y - 7)
    s.x_mark(xs[0], 372)
    return s.render()


def side_effects():
    s = Svg(1420, 640, "Every tool has a class, and the class decides what a retry may do")
    s.box(20, 40, 250, 120, "A step calls a tool", ["key = session + step", "(the same for every retry,", "never the attempt)"], BLUE)
    s.box(310, 40, 270, 120, "Tool gateway", ["writes the ledger row first:", "status 'started', attempts + 1", "then acts, then marks 'done'"], AMBER)
    s.arrow([(270, 100), (310, 100)], "flow")
    s.box(620, 40, 250, 120, "External system", ["acts, then the answer", "is lost on the way back", "(S05 makes this happen)"], RED)
    s.arrow([(580, 100), (620, 100)], "flow")
    s.arrow([(750, 160), (750, 190), (445, 190), (445, 160)], "bad", "retry: attempts = 2", 600, 183)
    rows = [
        ("local, read-only", GREEN, ["run again, harmless;", "local steps also redo if the", "sandbox was lost"]),
        ("idempotent", GREEN, ["send the same Idempotency-Key;", "the Git host recognises it and", "returns the first result"]),
        ("checkable", BLUE, ["on a retry, look first:", "'is there already a pull request", "from this branch?' Use it if so"]),
        ("unsafe", RED, ["cannot be keyed or looked up:", "the second attempt is refused,", "the run asks a person"]),
        ("naive (control)", AMBER, ["no protection, only in the tests:", "the retry acts again and a", "second pull request exists"]),
    ]
    for i, (t, c, lines) in enumerate(rows):
        x = 20 + i * 280
        s.box(x, 250, 265, 120, t, lines, c)
    s.box(20, 400, 1360, 90, "After a failure that cannot be retried: compensation", ["Cancel and rewind walk the ledger newest first and undo what can be undone (close the pull request, delete the branch).", "A comment cannot be unsent and is left. Every compensated row is marked in the ledger."], GREY)
    s.box(20, 510, 1360, 100, "What the ledger gives an operator", ["attempts and deduplicated per tool: a rising 'deduplicated' count means flaky tools or workers.", "Nothing is repeated silently: a retry that finds the effect already done returns the stored result."], AMBER)
    return s.render()


def workspace():
    s = Svg(1420, 560, "Working state: snapshot after each file change, restore into a new sandbox")
    steps = [
        ("1  A step changes files", GREEN, ["the agent writes src/ and tests/", "in the sandbox pod", "(the container's own disk, lost with the pod)"]),
        ("2  Snapshot", BLUE, ["hash every file; upload only", "the hashes S3 lacks; write", "a manifest of path to hash"]),
        ("3  The sandbox is lost", RED, ["pod deleted while tests run;", "its disk is gone;", "the tool step fails"]),
        ("4  Retry in a new sandbox", AMBER, ["manager starts a pod,", "restores the last manifest,", "the step runs again"]),
        ("5  Wait for a person", GREY, ["snapshot, then delete", "the sandbox: no pod, no", "worker, nothing held"]),
    ]
    for i, (t, c, lines) in enumerate(steps):
        x = 20 + i * 280
        s.box(x, 60, 265, 130, t, lines, c)
        if i < 4:
            s.arrow([(x + 265, 125), (x + 280, 125)], "flow")
    s.box(20, 230, 1360, 120, "Content-addressed snapshots", ["snapshots/objects/<sha256 of the file>: each file is stored once, however many snapshots name it.", "snapshots/manifests/<id>.json: the manifest is the snapshot; its id is a hash of its contents.", "A snapshot after a step that changed one file uploads one file."], GREY)
    s.box(20, 380, 1360, 150, "Why the order matters", ["The workflow snapshots only if the sandbox that ran the tool is still there. A fresh empty sandbox's snapshot would be a lie.", "If the sandbox vanished between the tool and the snapshot, the tool step is run again, in a restored sandbox.", "Process memory (a running test) is not saved: the test starts over. Pod-level checkpoint and restore is not built."], AMBER)
    return s.render()


def versioning():
    s = Svg(1420, 560, "New agent code: replay first, then new runs on new workers, old runs finish on old ones")
    s.box(20, 50, 300, 130, "1  Replay test (CI)", ["recorded histories go through", "the new workflow code", "no worker, no side effect", "a break is found before deploy"], AMBER)
    s.box(360, 50, 300, 130, "2  Deploy workers v2", ["new Deployment on queue agent-v2", "alongside workers v1", "the running ones are untouched"], BLUE)
    s.box(700, 50, 300, 130, "3  Point new runs at v2", ["the session API's current queue", "runs started earlier keep the", "queue they started on"], GREEN)
    s.box(1040, 50, 340, 130, "4  Retire v1 when its queue is empty", ["no running run on agent-v1:", "scale the v1 workers to zero", "(or fix and roll back)"], GREY)
    for x in (320, 660, 1000):
        s.arrow([(x, 115), (x + 40, 115)], "flow")
    s.box(20, 230, 660, 140, "What breaks without a patch marker", ["The recorded history says: snapshot, then run the next tool.", "Version 2 says: snapshot, then lint. The engine cannot match them,", "and every run that is waiting fails to continue (nondeterminism).", "S09 replays a waiting run through the three versions."], RED)
    s.box(720, 230, 660, 140, "What a patch marker does", ["workflow.patched('lint-after-snapshot') returns False for a history", "recorded before the change and True for a new run.", "Old histories take the old path; new runs take the new one.", "The same code can be deployed as one version safely."], GREEN)
    s.box(20, 400, 1360, 130, "Worker stops", ["A kill (SIGKILL) leaves the engine to notice a missing heartbeat, and the step runs again on another worker (S03).", "A rolling update sends SIGTERM: the worker stops taking tasks, lets its activities finish (up to 50 s) and exits.", "The step's attempt count stays at 1 (S09)."], AMBER)
    return s.render()


def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in (("a05-1", context), ("a05-2", topology), ("a05-3", architecture), ("a05-4", replay_flow), ("a05-5", side_effects), ("a05-6", workspace), ("a05-7", versioning)):
        (out / f"{name}.svg").write_text(fn())


if __name__ == "__main__":
    main()

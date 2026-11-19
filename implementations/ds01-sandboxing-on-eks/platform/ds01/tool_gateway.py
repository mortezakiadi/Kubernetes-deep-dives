"""Tool gateway, speaking the Model Context Protocol over HTTP (Design 01: "Tools and external services").

A caller presents a short-lived session token. The gateway maps it to the user,
agent and task, decides which tools that combination may call, enforces a rate
limit, and pauses high-risk tools until a human approves them.
"""
import hashlib
import json
import secrets
import threading
import time
from collections import defaultdict, deque

from ds01.common import Handler, audit, http_json, read_secret, secrets_equal, serve, verify

MANAGER = "http://sandbox-manager.sandbox-platform.svc:8000"
BROKER = "http://credential-broker.sandbox-egress.svc:8001"
METRICS = "https://metrics-api.sandbox-platform.svc"
RATE_PER_MIN = 20

session_key = read_secret("/secrets/session-key")
internal_token = read_secret("/secrets/tool-gateway-token")
orchestrator_token = read_secret("/secrets/orchestrator-token")

# Which agent may call which tool, and how risky each tool is.
TOOLS = {
    "metrics.query": {"risk": "read", "agents": {"data-analyst", "coding"},
                      "description": "Read a metric series from the company metrics API."},
    "ticket.create": {"risk": "high", "agents": {"data-analyst"},
                      "description": "Create a ticket. Needs a human approval each time."},
    "project.delete": {"risk": "forbidden", "agents": set(),
                       "description": "Delete a project. Never available to an agent."},
}

_calls = defaultdict(deque)
_approvals = {}
_lock = threading.Lock()
_ticket_seq = [100]


def _args_hash(name, args):
    return hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()[:16]


class ToolGateway(Handler):
    component = "tool-gateway"

    def route(self, method, path, query, body):
        if path == "/healthz":
            return 200, {"ok": True}
        if path.startswith("/v1/approvals/"):
            return self._approve(method, path)
        if path != "/mcp" or method != "POST":
            return 404, {"error": "not found"}
        rid = body.get("id") if isinstance(body, dict) else None
        try:
            claims = verify(session_key, self.bearer())
        except ValueError as exc:
            audit(self.component, "auth.rejected", reason=str(exc), peer=self.client_address[0])
            return 401, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"session token rejected: {exc}"}}
        status, info = http_json("GET", f"{MANAGER}/internal/sessions/{claims['sid']}", headers={"X-Internal-Token": internal_token})
        if status != 200:
            audit(self.component, "auth.revoked", session_id=claims["sid"])
            return 401, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "session is no longer active"}}
        if not self._within_rate(claims["sid"]):
            audit(self.component, "rate.limited", session_id=claims["sid"], limit_per_min=RATE_PER_MIN)
            return 429, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32029, "message": "rate limit exceeded"}}
        return 200, self._rpc(claims, body)

    def _within_rate(self, sid):
        now = time.time()
        with _lock:
            q = _calls[sid]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return False
            q.append(now)
            return True

    def _rpc(self, claims, req):
        rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        sid, agent = claims["sid"], claims["agent"]

        def err(code, message, data=None):
            e = {"code": code, "message": message}
            if data:
                e["data"] = data
            return {"jsonrpc": "2.0", "id": rid, "error": e}

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-03-26",
                    "serverInfo": {"name": "ds01-tool-gateway", "version": "1"}, "capabilities": {"tools": {}}}}
        if method == "tools/list":
            visible = [{"name": n, "description": t["description"], "risk": t["risk"]}
                       for n, t in TOOLS.items() if agent in t["agents"]]
            audit(self.component, "tools.listed", session_id=sid, agent=agent, count=len(visible))
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": visible}}
        if method != "tools/call":
            return err(-32601, "method not found")

        name, args = params.get("name"), params.get("arguments") or {}
        tool = TOOLS.get(name)
        if not tool or agent not in tool["agents"]:
            audit(self.component, "tool.denied", session_id=sid, agent=agent, tool=name, reason="not permitted for this agent")
            return err(-32003, f"tool {name!r} is not available to agent {agent!r}")
        if tool["risk"] == "high":
            approval_id = params.get("approval_id")
            key = _args_hash(name, args)
            with _lock:
                approval = _approvals.get(approval_id)
            if not approval or approval["session"] != sid or approval["key"] != key:
                approval_id = "apr-" + secrets.token_hex(4)
                with _lock:
                    _approvals[approval_id] = {"session": sid, "key": key, "tool": name, "state": "pending"}
                audit(self.component, "approval.requested", session_id=sid, tool=name, approval_id=approval_id)
                return err(-32001, "this tool needs a human approval", {"approval_id": approval_id, "state": "pending"})
            if approval["state"] != "approved":
                return err(-32001, "approval still pending", {"approval_id": approval_id, "state": approval["state"]})
            with _lock:
                approval["state"] = "used"  # one approval, one call
        result = self._run(name, args, claims)
        audit(self.component, "tool.called", session_id=sid, agent=agent, tool=name, risk=tool["risk"], ok="error" not in result)
        if "error" in result:
            return err(-32010, result["error"])
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}}

    def _run(self, name, args, claims):
        if name == "metrics.query":
            status, cred = http_json("POST", f"{BROKER}/mint",
                                     {"session_id": claims["sid"], "tenant": claims["tenant"], "audience": "metrics-api", "ttl_seconds": 60},
                                     headers={"X-Internal-Token": internal_token})
            if status != 200:
                return {"error": "credential broker refused"}
            status, data = http_json("GET", f"{METRICS}/v1/metrics?name={args.get('name', 'orders_per_minute')}",
                                     headers={"Authorization": f"Bearer {cred['credential']}"}, cafile="/pki/ca.crt")
            return data if status == 200 else {"error": f"metrics api answered {status}"}
        if name == "ticket.create":
            _ticket_seq[0] += 1
            return {"ticket": f"OPS-{_ticket_seq[0]}", "title": str(args.get("title", ""))[:120]}
        return {"error": "unknown tool"}

    def _approve(self, method, path):
        # Stands in for "the user approves it in the product UI".
        if not secrets_equal(self.bearer(), orchestrator_token):
            return 401, {"error": "unauthorized"}
        parts = path.strip("/").split("/")
        if len(parts) == 4 and method == "POST" and parts[3] in ("approve", "deny"):
            with _lock:
                approval = _approvals.get(parts[2])
                if not approval:
                    return 404, {"error": "no such approval"}
                approval["state"] = "approved" if parts[3] == "approve" else "denied"
            audit(self.component, "approval." + ("approved" if parts[3] == "approve" else "denied"), session_id=approval["session"], tool=approval["tool"], approval_id=parts[2])
            return 200, {"approval_id": parts[2], "state": approval["state"]}
        return 404, {"error": "not found"}


if __name__ == "__main__":
    serve(ToolGateway, 8003)

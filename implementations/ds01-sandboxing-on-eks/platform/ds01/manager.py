"""Sandbox Manager API (Design 01: "Sandbox Manager API").

The only component allowed to create sandboxes. It turns "session 42 needs a
Python sandbox" into a SandboxClaim, enforces tenant quotas, stamps an expiry on
the claim, records which user owns which sandbox, and issues the short-lived
session token that the tool gateway checks. Agents and users never get
Kubernetes access.
"""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from ds01.common import K8s, Handler, audit, mint, read_secret, secrets_equal, serve, sha256_hex

NS = "sandboxes"
CLAIMS = f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{NS}/sandboxclaims"
PODS = f"/api/v1/namespaces/{NS}/pods"

MAX_PER_TENANT = int(os.environ.get("MAX_SESSIONS_PER_TENANT", "3"))
MAX_TTL = int(os.environ.get("MAX_TTL_SECONDS", "7200"))
READY_TIMEOUT = int(os.environ.get("READY_TIMEOUT_SECONDS", "120"))

# One entry per kind of sandbox the platform team owns (Design 01: "Template").
# default_allow is the per-template egress allowlist; inject maps a host to the
# credential audience the egress gateway mints for it.
TEMPLATES = {
    "python-untrusted": {
        "pool": "python-untrusted-pool",
        "network": True,
        "default_allow": ["pypi.org", "files.pythonhosted.org", "metrics-api.sandbox-platform.svc", "tool-gateway.sandbox-platform.svc"],
        "inject": {"metrics-api.sandbox-platform.svc": "metrics-api"},
    },
    "compute-only": {"pool": "compute-only-pool", "network": False, "default_allow": [], "inject": {}},
}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")

k8s = K8s()
orchestrator_token = read_secret("/secrets/orchestrator-token")
session_key = read_secret("/secrets/session-key")
internal_tokens = {
    "egress-gateway": read_secret("/secrets/egress-gateway-token"),
    "tool-gateway": read_secret("/secrets/tool-gateway-token"),
}

_cache = {"at": 0.0, "claims": [], "pods": {}}
_cache_lock = threading.Lock()


def _snapshot():
    """Claims and pod IPs, cached for 2s because the egress gateway asks per request."""
    with _cache_lock:
        if time.time() - _cache["at"] > 2:
            _, claims = k8s.request("GET", CLAIMS)
            _, pods = k8s.request("GET", PODS)
            _cache["claims"] = claims.get("items", [])
            _cache["pods"] = {p["metadata"]["name"]: p for p in pods.get("items", [])}
            _cache["at"] = time.time()
        return _cache["claims"], _cache["pods"]


def _invalidate():
    with _cache_lock:
        _cache["at"] = 0.0


def _describe(claim, pods):
    meta = claim["metadata"]
    labels = meta.get("labels", {})
    sandbox = (claim.get("status", {}).get("sandbox") or {}).get("name")
    pod = pods.get(sandbox) if sandbox else None
    allow = json.loads(meta.get("annotations", {}).get("ds01/allow-hosts", "[]"))
    template = labels.get("ds01/template", "")
    return {
        "session_id": meta["name"],
        "tenant": labels.get("ds01/tenant"),
        "agent": labels.get("ds01/agent"),
        "template": template,
        "user": meta.get("annotations", {}).get("ds01/user"),
        "sandbox": sandbox,
        "pod_ip": (pod or {}).get("status", {}).get("podIP"),
        "node": (pod or {}).get("spec", {}).get("nodeName"),
        "allow_hosts": allow,
        "inject": TEMPLATES.get(template, {}).get("inject", {}),
        "shutdown_time": claim.get("spec", {}).get("lifecycle", {}).get("shutdownTime"),
        "terminating": bool(meta.get("deletionTimestamp")),
    }


def _active_claims():
    claims, pods = _snapshot()
    return [_describe(c, pods) for c in claims if not c["metadata"].get("deletionTimestamp")]


def _ready(claim):
    for cond in claim.get("status", {}).get("conditions", []):
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


class Manager(Handler):
    component = "sandbox-manager"

    def _auth_orchestrator(self):
        return secrets_equal(self.bearer(), orchestrator_token)

    def _auth_internal(self):
        presented = self.headers.get("X-Internal-Token", "")
        for who, token in internal_tokens.items():
            if secrets_equal(presented, token):
                return who
        return None

    def route(self, method, path, query, body):
        if path == "/healthz":
            return 200, {"ok": True}
        if path.startswith("/internal/"):
            who = self._auth_internal()
            if not who:
                return 401, {"error": "unauthorized"}
            return self._internal(method, path, who)
        if not self._auth_orchestrator():
            return 401, {"error": "unauthorized"}
        if path == "/v1/sessions" and method == "POST":
            return self._create(body)
        if path == "/v1/sessions" and method == "GET":
            return 200, {"sessions": _active_claims()}
        m = re.fullmatch(r"/v1/sessions/([a-z0-9-]+)", path)
        if m and method == "GET":
            found = [s for s in _active_claims() if s["session_id"] == m.group(1)]
            return (200, found[0]) if found else (404, {"error": "no such session"})
        if m and method == "DELETE":
            return self._delete(m.group(1))
        return 404, {"error": "not found"}

    # ---- internal API for the gateways -------------------------------------
    def _internal(self, method, path, who):
        m = re.fullmatch(r"/internal/by-ip/([0-9.]+)", path)
        if m:
            for s in _active_claims():
                if s["pod_ip"] == m.group(1):
                    return 200, s
            return 404, {"error": "no session owns this address"}
        m = re.fullmatch(r"/internal/sessions/([a-z0-9-]+)", path)
        if m:
            found = [s for s in _active_claims() if s["session_id"] == m.group(1) and not s["terminating"]]
            return (200, found[0]) if found else (404, {"error": "no active session"})
        return 404, {"error": "not found"}

    # ---- create / delete ------------------------------------------------------
    def _create(self, body):
        if not isinstance(body, dict):
            return 400, {"error": "json body required"}
        tenant, agent, user = body.get("tenant", ""), body.get("agent", ""), str(body.get("user", ""))[:64]
        template = body.get("template", "")
        if template not in TEMPLATES:
            return 400, {"error": f"unknown template; choose one of {sorted(TEMPLATES)}"}
        if not NAME_RE.match(tenant) or not NAME_RE.match(agent) or not user:
            return 400, {"error": "tenant and agent must be short lowercase names; user is required"}
        extra = [h.lower() for h in body.get("allow_hosts", [])]
        if any(not HOST_RE.match(h) for h in extra):
            return 400, {"error": "allow_hosts must be hostnames"}
        if not TEMPLATES[template]["network"] and extra:
            return 400, {"error": "this template has no network device; allow_hosts does not apply"}
        ttl = min(int(body.get("ttl_seconds", 1800)), MAX_TTL)

        _invalidate()
        active = [s for s in _active_claims() if s["tenant"] == tenant and not s["terminating"]]
        if len(active) >= MAX_PER_TENANT:
            audit(self.component, "quota.denied", tenant=tenant, active=len(active), limit=MAX_PER_TENANT)
            return 429, {"error": f"tenant {tenant} already has {len(active)} active sessions (limit {MAX_PER_TENANT})"}

        sid = "sess-" + secrets.token_hex(4)
        tpl = TEMPLATES[template]
        allow = sorted(set(tpl["default_allow"] + extra))
        shutdown = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {
                "name": sid,
                "namespace": NS,
                "labels": {"ds01/tenant": tenant, "ds01/agent": agent, "ds01/template": template, "ds01/user-hash": sha256_hex(user)[:12]},
                "annotations": {"ds01/allow-hosts": json.dumps(allow), "ds01/user": user},
            },
            "spec": {
                "warmPoolRef": {"name": tpl["pool"]},
                "additionalPodMetadata": {"labels": {"sandbox.users.io/session": sid, "sandbox.users.io/tenant": tenant, "sandbox.users.io/template": template}},
                "lifecycle": {"shutdownTime": shutdown, "shutdownPolicy": "Delete"},
            },
        }
        t0 = time.time()
        code, resp = k8s.request("POST", CLAIMS, manifest)
        if code >= 300:
            audit(self.component, "claim.failed", session_id=sid, status=code, detail=str(resp.get("message", ""))[:200])
            return 502, {"error": "could not create the sandbox claim"}
        deadline = t0 + READY_TIMEOUT
        claim = resp
        while time.time() < deadline:
            _, claim = k8s.request("GET", f"{CLAIMS}/{sid}")
            if _ready(claim) and (claim.get("status", {}).get("sandbox") or {}).get("name"):
                break
            time.sleep(0.3)
        else:
            audit(self.component, "claim.timeout", session_id=sid)
            k8s.request("DELETE", f"{CLAIMS}/{sid}")
            return 504, {"error": "sandbox did not become ready in time"}
        ready_ms = int((time.time() - t0) * 1000)
        _invalidate()
        info = next((s for s in _active_claims() if s["session_id"] == sid), None) or {}
        token = mint(
            session_key,
            {"sid": sid, "tenant": tenant, "agent": agent, "user": user, "template": template},
            ttl,
        )
        audit(self.component, "session.created", session_id=sid, tenant=tenant, agent=agent, user_hash=sha256_hex(user)[:12],
              template=template, sandbox=info.get("sandbox"), node=info.get("node"), ready_ms=ready_ms,
              launch_type=(claim.get("metadata", {}).get("labels", {}) or {}).get("agents.x-k8s.io/launch-type"),
              shutdown_time=shutdown, allow_hosts=allow)
        return 201, {"session_id": sid, "claim": sid, "template": template, "ready_ms": ready_ms,
                     "allow_hosts": allow, "shutdown_time": shutdown, "session_token": token, "pod_ip": info.get("pod_ip")}

    def _delete(self, sid):
        code, _ = k8s.request("DELETE", f"{CLAIMS}/{sid}")
        _invalidate()
        audit(self.component, "session.deleted", session_id=sid, status=code)
        return (200, {"deleted": sid}) if code < 300 else (404, {"error": "no such session"})


if __name__ == "__main__":
    serve(Manager, 8000)

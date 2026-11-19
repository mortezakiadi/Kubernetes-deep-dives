"""Egress gateway policy, as a mitmproxy addon (Design 01: "Network: default deny and one way out").

The sandbox network allows exactly one destination: this proxy. For every request
the gateway:
  1. finds the session that owns the source address (asks the Sandbox Manager),
  2. checks the destination host against that session's allowlist,
  3. passes TLS through untouched for ordinary hosts, and terminates TLS only for
     hosts that need a credential injected,
  4. for those hosts, replaces the placeholder with a short-lived credential minted
     by the broker, and
  5. writes an audit record carrying the session ID.
"""
import hashlib
import os
import threading
import time
from collections import defaultdict, deque

from mitmproxy import http, tls

from ds01.common import audit, http_json, read_secret

COMPONENT = "egress-gateway"
MANAGER = os.environ.get("MANAGER_URL", "http://sandbox-manager.sandbox-platform.svc:8000")
BROKER = os.environ.get("BROKER_URL", "http://credential-broker.sandbox-egress.svc:8001")
RATE_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))
BYTE_BUDGET = int(os.environ.get("SESSION_BYTE_BUDGET", str(50 * 1024 * 1024)))
TOKEN = read_secret("/secrets/egress-gateway-token")


def host_allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allow:
        if host == entry or (entry.startswith("*.") and host.endswith(entry[1:])):
            return True
    return False


class Gateway:
    def __init__(self):
        self._sessions = {}          # source ip -> (expires_at, session or None)
        self._creds = {}             # (session, audience) -> (expires_at, credential)
        self._hits = defaultdict(deque)
        self._bytes = defaultdict(int)
        self._lock = threading.Lock()

    # ---- session lookup ------------------------------------------------------
    def session(self, ip: str):
        now = time.time()
        with self._lock:
            cached = self._sessions.get(ip)
            if cached and cached[0] > now:
                return cached[1]
        status, info = http_json("GET", f"{MANAGER}/internal/by-ip/{ip}", headers={"X-Internal-Token": TOKEN}, timeout=5)
        found = info if status == 200 else None
        with self._lock:
            self._sessions[ip] = (now + (5 if found else 2), found)
        return found

    def over_rate(self, sid: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits[sid]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return True
            q.append(now)
            return False

    def credential(self, session: dict, audience: str):
        key = (session["session_id"], audience)
        now = time.time()
        with self._lock:
            cached = self._creds.get(key)
            if cached and cached[0] > now + 60:
                return cached[1]
        status, resp = http_json("POST", f"{BROKER}/mint",
                                 {"session_id": session["session_id"], "tenant": session["tenant"], "audience": audience},
                                 headers={"X-Internal-Token": TOKEN}, timeout=5)
        if status != 200:
            return None
        with self._lock:
            self._creds[key] = (now + resp["ttl_seconds"], resp["credential"])
        return resp["credential"]

    # ---- helpers -------------------------------------------------------------
    @staticmethod
    def _deny(flow: http.HTTPFlow, code: int, reason: str, sid, host, extra=None):
        flow.response = http.Response.make(code, f"egress denied: {reason}\n".encode(), {"Content-Type": "text/plain"})
        audit(COMPONENT, "egress.deny", session_id=sid, host=host, port=flow.request.port, status=code, reason=reason,
              source=flow.client_conn.peername[0], **(extra or {}))

    # ---- mitmproxy hooks -------------------------------------------------------
    def http_connect(self, flow: http.HTTPFlow):
        ip, host = flow.client_conn.peername[0], flow.request.host.lower()
        s = self.session(ip)
        if not s:
            return self._deny(flow, 403, "source is not a known sandbox", None, host)
        sid = s["session_id"]
        if not host_allowed(host, s["allow_hosts"]):
            return self._deny(flow, 403, f"{host} is not on this session's allowlist", sid, host)
        if self.over_rate(sid):
            return self._deny(flow, 429, "request rate limit for this session", sid, host)
        if self._bytes[sid] > BYTE_BUDGET:
            return self._deny(flow, 429, "byte budget for this session is used up", sid, host)
        mode = "terminate" if host in s["inject"] else "passthrough"
        audit(COMPONENT, "egress.allow", session_id=sid, tenant=s["tenant"], host=host, port=flow.request.port, tls_mode=mode,
              source=ip)

    def tls_clienthello(self, data: tls.ClientHelloData):
        addr = data.context.server.address
        host = (addr[0] if addr else data.client_hello.sni or "").lower()
        s = self.session(data.context.client.peername[0])
        if not s or host not in s["inject"]:
            data.ignore_connection = True  # pass the encrypted stream through untouched

    def request(self, flow: http.HTTPFlow):
        if flow.request.method == "CONNECT":
            return
        ip, host = flow.client_conn.peername[0], flow.request.host.lower()
        s = self.session(ip)
        if not s:
            return self._deny(flow, 403, "source is not a known sandbox", None, host)
        sid = s["session_id"]
        if not host_allowed(host, s["allow_hosts"]):
            return self._deny(flow, 403, f"{host} is not on this session's allowlist", sid, host)
        if self.over_rate(sid):
            return self._deny(flow, 429, "request rate limit for this session", sid, host)
        injected = None
        audience = s["inject"].get(host)
        if audience:
            cred = self.credential(s, audience)
            if not cred:
                return self._deny(flow, 502, "credential broker unavailable", sid, host)
            flow.request.headers["Authorization"] = f"Bearer {cred}"
            injected = hashlib.sha256(cred.encode()).hexdigest()[:10]
        flow.metadata["session"], flow.metadata["credential_id"] = sid, injected
        audit(COMPONENT, "egress.request", session_id=sid, tenant=s["tenant"], host=host, method=flow.request.method,
              path=flow.request.path.split("?")[0][:200], injected=bool(injected), credential_id=injected, source=ip)

    def response(self, flow: http.HTTPFlow):
        sid = flow.metadata.get("session")
        if not sid or not flow.response:
            return
        size = len(flow.response.content or b"")
        with self._lock:
            self._bytes[sid] += size
        audit(COMPONENT, "egress.response", session_id=sid, host=flow.request.host, status=flow.response.status_code, bytes=size)


addons = [Gateway()]

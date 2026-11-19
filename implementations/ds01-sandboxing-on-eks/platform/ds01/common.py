"""Shared helpers for the Design 01 platform services.

Everything here uses the Python standard library on purpose: the services are
small, and a reader can audit them without chasing dependencies.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# Audit logging. One JSON object per line on stdout, every record carrying the
# session ID when there is one, so a single query joins every component.
# --------------------------------------------------------------------------
_SECRET_NAME = re.compile(r"(token|secret|password|authorization|api[_-]?key|credential)", re.I)
_SAFE_SUFFIX = ("_id", "_sha256", "_len")


def audit(component: str, event: str, **fields) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "component": component,
        "event": event,
    }
    for key, value in fields.items():
        if _SECRET_NAME.search(key) and not key.endswith(_SAFE_SUFFIX):
            value = "[redacted]"
        rec[key] = value
    print(json.dumps(rec, separators=(",", ":"), default=str), flush=True)


# --------------------------------------------------------------------------
# Signed, expiring tokens (HMAC-SHA256). Used for session tokens and for the
# short-lived credentials the broker mints.
# --------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_secret(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def mint(key: str, claims: dict, ttl_seconds: int) -> str:
    body = dict(claims)
    now = int(time.time())
    body["iat"], body["exp"], body["jti"] = now, now + int(ttl_seconds), secrets.token_hex(6)
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify(key: str, token: str, audience: str | None = None) -> dict:
    """Return the claims, or raise ValueError with a short reason."""
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        raise ValueError("malformed")
    expected = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    claims = json.loads(_unb64(payload))
    if claims.get("exp", 0) < time.time():
        raise ValueError("expired")
    if audience is not None and claims.get("aud") != audience:
        raise ValueError("wrong audience")
    return claims


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def secrets_equal(a: str, b: str) -> bool:
    """Constant-time comparison; an empty value never matches."""
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


# --------------------------------------------------------------------------
# A tiny JSON-over-HTTP server base class.
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    component = "service"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # access logs are written by audit(), not here
        pass

    # subclasses implement route(method, path, query, body) -> (code, obj)
    def route(self, method, path, query, body):  # pragma: no cover
        return 404, {"error": "not found"}

    def bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        return header[7:].strip() if header.lower().startswith("bearer ") else ""

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = raw
        if raw and "json" in (self.headers.get("Content-Type") or ""):
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
        try:
            result = self.route(method, parsed.path, parse_qs(parsed.query), body)
        except Exception as exc:  # never leak internals to the caller
            audit(self.component, "error", path=parsed.path, error=type(exc).__name__, detail=str(exc)[:300])
            result = (500, {"error": "internal error"})
        self._send(*result)

    def _send(self, code, obj, content_type="application/json"):
        data = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def serve(handler_cls, port: int, tls_cert: str | None = None, tls_key: str | None = None):
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    if tls_cert and tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    audit(handler_cls.component, "listening", port=port, tls=bool(tls_cert))
    server.serve_forever()


# --------------------------------------------------------------------------
# Minimal in-cluster Kubernetes API client.
# --------------------------------------------------------------------------
class K8s:
    SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self):
        self.base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
        self.ctx = ssl.create_default_context(cafile=f"{self.SA_DIR}/ca.crt")

    def request(self, method, path, body=None, timeout=15):
        token = read_secret(f"{self.SA_DIR}/token")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read() or b"{}")
            except json.JSONDecodeError:
                return err.code, {}


def http_json(method, url, body=None, headers=None, timeout=10, cafile=None):
    """Small helper for service-to-service calls. Returns (status, json)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context(cafile=cafile) if url.startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except json.JSONDecodeError:
            return err.code, {}

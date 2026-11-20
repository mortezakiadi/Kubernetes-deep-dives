"""Shared pieces: configuration, the session database, small HTTP helpers.

Everything in this package is one program in several roles (see entrypoint.py). The roles are the components of Design 04:
session API, model gateway, tool gateway, sandbox manager, the sandbox's own executor, the agent workers, and a stand-in
for the external system the agent changes (a Git host).
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233")
MODEL_GATEWAY = os.environ.get("MODEL_GATEWAY", "http://model-gateway.agent-platform.svc:8080")
TOOL_GATEWAY = os.environ.get("TOOL_GATEWAY", "http://tool-gateway.agent-platform.svc:8080")
SANDBOX_MANAGER = os.environ.get("SANDBOX_MANAGER", "http://sandbox-manager.agent-platform.svc:8080")
SESSION_API = os.environ.get("SESSION_API", "http://session-api.agent-platform.svc:8080")
EXTERNAL_GIT = os.environ.get("EXTERNAL_GIT", "http://git-host.external.svc:8080")
BUCKET = os.environ.get("BUCKET", "")
SYSTEM_PROMPT = "You are a coding agent. Work in /workspace. Use one tool per step. Say done when the ticket is finished."


def db():
    return psycopg.connect(
        host=os.environ["DB_HOST"], port=5432, dbname=os.environ.get("DB_NAME", "postgres"),
        user=os.environ.get("DB_USER", "temporal"), password=os.environ["DB_PASSWORD"],
        autocommit=True, row_factory=dict_row, connect_timeout=10)


SCHEMA = """
CREATE TABLE IF NOT EXISTS context_log (
  session text, step int, role text, content text, tokens int, ts timestamptz DEFAULT now(),
  PRIMARY KEY (session, step, role));
CREATE TABLE IF NOT EXISTS summaries (
  id serial PRIMARY KEY, session text, upto_step int, text text, tokens int, ts timestamptz DEFAULT now(),
  UNIQUE (session, upto_step));
CREATE TABLE IF NOT EXISTS snapshots (
  session text, step int, snapshot_id text, files int, uploaded int, skipped int, ts timestamptz DEFAULT now(),
  PRIMARY KEY (session, step));
CREATE TABLE IF NOT EXISTS model_calls (
  id serial PRIMARY KEY, session text, step int, kind text, backend text, prompt_sha text,
  prompt_tokens int, completion_tokens int, status int, ts timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS ledger (
  key text PRIMARY KEY, session text, step int, tool text, class text, status text, result jsonb,
  external_ref text, attempts int DEFAULT 0, deduplicated int DEFAULT 0,
  created timestamptz DEFAULT now(), updated timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS runs (
  session text PRIMARY KEY, task jsonb, queue text, parent text, forked_at int, created timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS config (k text PRIMARY KEY, v text);
CREATE TABLE IF NOT EXISTS ext_branches (repo text, name text, ts timestamptz DEFAULT now(), PRIMARY KEY (repo, name));
CREATE TABLE IF NOT EXISTS ext_prs (
  number serial PRIMARY KEY, repo text, head text, title text, idem_key text UNIQUE, state text DEFAULT 'open', ts timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS ext_comments (
  id serial PRIMARY KEY, pr int, body text, idem_key text UNIQUE, ts timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS ext_faults (k text PRIMARY KEY, v int);
"""


def ensure_schema():
    for attempt in range(30):
        try:
            with db() as c:
                c.execute(SCHEMA)
            return
        except Exception as exc:  # the database may still be coming up
            print(f"schema: waiting for the database ({exc})", flush=True)
            time.sleep(5)
    raise RuntimeError("database not reachable")


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ HTTP helpers --
def http(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw.decode(errors="replace")[:300]}


class Handler(BaseHTTPRequestHandler):
    """Minimal JSON router. Subclasses fill `routes`: {(method, regex): function(handler, match, body) -> (status, obj)}."""
    routes = {}
    protocol_version = "HTTP/1.1"

    def _go(self, method):
        import re
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?")[0]
        for (m, pattern), fn in self.routes.items():
            if m == method:
                mt = re.fullmatch(pattern, path)
                if mt:
                    try:
                        status, obj = fn(self, mt, body)
                    except Exception as exc:  # a handler bug must be visible, not a hung connection
                        import traceback
                        traceback.print_exc()
                        status, obj = 500, {"error": f"{type(exc).__name__}: {exc}"}
                    out = json.dumps(obj).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
        out = b'{"error":"not found"}'
        self.send_response(404)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def do_DELETE(self):
        self._go("DELETE")

    def log_message(self, *a):
        pass


def serve(handler_cls, port=8080):
    ensure_schema()
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    srv.daemon_threads = True
    print(f"listening on {port}", flush=True)
    srv.serve_forever()


def query_params(path):
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}

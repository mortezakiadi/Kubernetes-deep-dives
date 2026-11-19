"""A stand-in for the trusted agent orchestrator (Design 01: "Agent orchestrator (trusted)").

The real orchestrator runs the agent loop: it calls the LLM, decides what to do
next and sends commands to a sandbox. This one has no LLM. It is a small command
line that performs the same API calls an orchestrator makes, so a test can play
the part of "an agent session" step by step and every call is visible.

Every sub-command prints one JSON document on stdout.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

from ds01.common import read_secret

MANAGER = "http://sandbox-manager.sandbox-platform.svc:8000"
ROUTER = "http://sandbox-router.sandbox-router.svc:8004"
TOOLGW = "http://tool-gateway.sandbox-platform.svc:8003"
TOKEN = read_secret("/secrets/orchestrator-token")


def call(method, url, body=None, headers=None, raw=None, timeout=180):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if raw is None and data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"http_status": resp.status, **json.loads(resp.read() or b"{}")}
    except urllib.error.HTTPError as err:
        try:
            return {"http_status": err.code, **json.loads(err.read() or b"{}")}
        except json.JSONDecodeError:
            return {"http_status": err.code}


def main(argv):
    cmd = argv[0]
    if cmd == "create":
        # create <tenant> <agent> <template> [ttl_seconds] [host,host,...]
        body = {"user": os.environ.get("ORCH_USER", "user-demo"), "tenant": argv[1], "agent": argv[2], "template": argv[3]}
        if len(argv) > 4:
            body["ttl_seconds"] = int(argv[4])
        if len(argv) > 5 and argv[5]:
            body["allow_hosts"] = argv[5].split(",")
        out = call("POST", f"{MANAGER}/v1/sessions", body)
    elif cmd == "list":
        out = call("GET", f"{MANAGER}/v1/sessions")
    elif cmd == "delete":
        out = call("DELETE", f"{MANAGER}/v1/sessions/{argv[1]}")
    elif cmd == "exec":
        out = call("POST", f"{ROUTER}/v1/sandboxes/{argv[1]}/exec", {"command": argv[2], "timeout": int(argv[3]) if len(argv) > 3 else 60})
    elif cmd == "put":
        # put <sid> <dest-path-in-sandbox> <local-path-in-this-pod>
        with open(argv[3], "rb") as handle:
            out = call("PUT", f"{ROUTER}/v1/sandboxes/{argv[1]}/files?path={argv[2]}", raw=handle.read(),
                       headers={"Content-Type": "application/octet-stream"})
    elif cmd == "put-text":
        out = call("PUT", f"{ROUTER}/v1/sandboxes/{argv[1]}/files?path={argv[2]}", raw=argv[3].encode(),
                   headers={"Content-Type": "application/octet-stream"})
    elif cmd == "get":
        out = call("GET", f"{ROUTER}/v1/sandboxes/{argv[1]}/files?path={argv[2]}")
        if "content_b64" in out:
            out["content"] = base64.b64decode(out.pop("content_b64")).decode("utf-8", "replace")
    elif cmd == "mcp":
        # mcp <session_token> <method> [json-params]   (the session token, not the orchestrator token)
        payload = {"jsonrpc": "2.0", "id": 1, "method": argv[2], "params": json.loads(argv[3]) if len(argv) > 3 else {}}
        req = urllib.request.Request(f"{TOOLGW}/mcp", data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {argv[1]}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                out = {"http_status": resp.status, **json.loads(resp.read())}
        except urllib.error.HTTPError as err:
            out = {"http_status": err.code, **json.loads(err.read() or b"{}")}
    elif cmd == "approve":
        out = call("POST", f"{TOOLGW}/v1/approvals/{argv[1]}/approve")
    else:
        out = {"error": f"unknown command {cmd}"}
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1:])

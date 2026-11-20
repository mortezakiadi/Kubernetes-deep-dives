"""The model gateway (Design 04, "Gateways"): retries to a fallback, token budgets per run, and a record of every call.

Every call is written to `model_calls` with the backend that served it and a hash of the prompt. That table is the
evidence that replay does not call the model again, and that a rebuilt prompt is the same prompt.

Fault injection is part of the gateway so tests can make the provider fail for a while, slow, or both.
"""
import json
import time

from . import model as M
from .common import Handler, db, serve, tokens


def cfg(key, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM config WHERE k=%s", (key,)).fetchone()
    return r["v"] if r else default


def set_cfg(key, value):
    with db() as c:
        c.execute("INSERT INTO config (k, v) VALUES (%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (key, str(value)))


def record(session, step, kind, backend, psha, ptok, ctok, status):
    with db() as c:
        c.execute("INSERT INTO model_calls (session, step, kind, backend, prompt_sha, prompt_tokens, completion_tokens, status) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (session, step, kind, backend, psha, ptok, ctok, status))


def used_tokens(session):
    with db() as c:
        r = c.execute("SELECT COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t FROM model_calls "
                      "WHERE session=%s AND status=200", (session,)).fetchone()
    return int(r["t"])


def complete(h, m, body):
    session, step, kind = body["session"], int(body["step"]), body.get("kind", "act")
    prompt = body["prompt"]
    psha, ptok = M.prompt_sha(prompt), tokens(prompt)
    budget = body.get("token_budget")
    if budget and used_tokens(session) + ptok > int(budget):
        record(session, step, kind, "none", psha, ptok, 0, 429)
        return 429, {"error": "token budget reached", "code": "budget", "used": used_tokens(session), "budget": int(budget)}
    latency = float(cfg("model_latency_s", "0") or 0)
    if latency:
        time.sleep(latency)
    fault_until = float(cfg("model_fault_until", "0") or 0)
    primary_down = time.time() < fault_until
    if primary_down:
        if body.get("allow_fallback", True):
            backend = "fallback"
        else:
            record(session, step, kind, "primary", psha, ptok, 0, 503)
            return 503, {"error": "primary model provider unavailable", "code": "provider"}
    else:
        backend = "primary"
    out = M.decide(prompt, session, step) if kind == "act" else M.summarize(prompt)
    ctok = tokens(json.dumps(out))
    record(session, step, kind, backend, psha, ptok, ctok, 200)
    return 200, {"backend": backend, "action": out, "prompt_sha": psha, "usage": {"prompt": ptok, "completion": ctok}}


def fault(h, m, body):
    secs = float(body.get("seconds", 0))
    set_cfg("model_fault_until", time.time() + secs if secs else 0)
    if "latency_s" in body:
        set_cfg("model_latency_s", body["latency_s"])
    return 200, {"primary_down_for_s": secs, "latency_s": cfg("model_latency_s", "0")}


def usage(h, m, body):
    s = m.group(1)
    with db() as c:
        rows = c.execute("SELECT backend, status, COUNT(*) AS n, SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct "
                         "FROM model_calls WHERE session=%s GROUP BY backend, status", (s,)).fetchall()
    return 200, {"session": s, "rows": rows}


class H(Handler):
    routes = {
        ("POST", r"/v1/complete"): complete,
        ("POST", r"/admin/fault"): fault,
        ("GET", r"/usage/(.+)"): usage,
        ("GET", r"/healthz"): lambda h, m, b: (200, {"ok": True}),
    }


def main():
    serve(H)

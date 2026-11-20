"""The tool gateway and the side-effect ledger (Design 04, "Side effects: exactly once where it matters").

Every tool is registered with a CLASS, which says how a retry of the same step is made safe:

  local       runs in the sandbox (files, tests). Re-running is harmless, and must happen again if the sandbox was lost.
  read_only   changes nothing outside; safe to repeat.
  idempotent  changes the outside world, and the external system honours an Idempotency-Key, so a repeat is deduplicated there.
  checkable   changes the outside world, and the tool can LOOK for the effect first ("is there already a PR from this branch?").
  unsafe      changes the world and can be neither keyed nor looked up. A retry after an unknown outcome needs a human.
  naive       NOT part of the design. A tool with no protection, registered only so the tests can show what goes wrong without it.

The key is built from the session and the STEP, never the attempt, so every retry of one step carries the same key.
The ledger records each effect before and after it is made. A retry that finds the effect already done returns the stored result.
"""
import json

from .common import EXTERNAL_GIT, Handler, db, http, serve

REPO = "app"


def jr(row):
    return json.dumps(row) if not isinstance(row, str) else row


# ------------------------------------------------------------------------------------------------ the tools --
def sandbox_call(ip, path, body, timeout=900):
    return http("POST", f"http://{ip}:8081{path}", body, timeout=timeout)


def t_list_dir(session, step, args, sb, key):
    s, r = sandbox_call(sb, "/list", {})
    return (200, r) if s == 200 else (502, r)


def t_write_file(session, step, args, sb, key):
    s, r = sandbox_call(sb, "/write", {"path": args["path"], "content": args["content"]})
    return (200, r) if s == 200 else (502, r)


def t_run_tests(session, step, args, sb, key):
    secs = int(args.get("seconds", 4))
    s, r = sandbox_call(sb, "/exec", {"cmd": f"sleep {secs}; set -o pipefail; python -m unittest tests.test_slugify 2>&1 | tail -6", "timeout": secs + 120})
    if s != 200:
        return 502, r
    return 200, {"passed": r["exit"] == 0, "output": r["stdout"][-300:], "seconds": r["seconds"]}


def t_read_review(session, step, args, sb, key):
    s, r = http("GET", f"{EXTERNAL_GIT}/pulls/{args['pr']}/comments")
    if s != 200:
        return 502, r
    comments = [c["body"] for c in r["comments"] if not c["body"].startswith("Addressed")]
    return 200, {"comment": comments[-1] if comments else ""}


def lookup_branch(session, step, args):
    s, r = http("GET", f"{EXTERNAL_GIT}/repos/{REPO}/branches/{args['branch'].replace('/', '%2F')}")
    return (r if s == 200 else None)


def t_push_branch(session, step, args, sb, key):
    s, r = http("POST", f"{EXTERNAL_GIT}/repos/{REPO}/branches", {"name": args["branch"]})
    return (200, {"branch": args["branch"]}) if s == 200 else (502, r)


def lookup_pr(session, step, args):
    s, r = http("GET", f"{EXTERNAL_GIT}/repos/{REPO}/pulls?head={args['head'].replace('/', '%2F')}")
    return r["pulls"][0] if s == 200 and r.get("pulls") else None


def t_open_pr(session, step, args, sb, key):
    s, r = http("POST", f"{EXTERNAL_GIT}/repos/{REPO}/pulls", {"head": args["head"], "title": args["title"]}, headers={"Idempotency-Key": key})
    return (200, {"number": r["number"], "head": r["head"]}) if s == 200 else (502, r)


def t_open_pr_naive(session, step, args, sb, key):
    s, r = http("POST", f"{EXTERNAL_GIT}/repos/{REPO}/pulls", {"head": args["head"], "title": args["title"]})  # no key, no lookup
    return (200, {"number": r["number"], "head": r["head"]}) if s == 200 else (502, r)


def t_comment(session, step, args, sb, key):
    s, r = http("POST", f"{EXTERNAL_GIT}/pulls/{args['pr']}/comments", {"body": args["body"]}, headers={"Idempotency-Key": key})
    return (200, {"comment_id": r["id"]}) if s == 200 else (502, r)


def t_delete_prod(session, step, args, sb, key):
    s, r = http("DELETE", f"{EXTERNAL_GIT}/repos/{REPO}/branches/{args['branch'].replace('/', '%2F')}")
    return (200, r) if s == 200 else (502, r)


# name -> (class, function, lookup function for checkable tools)
TOOLS = {
    "list_dir": ("local", t_list_dir, None),
    "write_file": ("local", t_write_file, None),
    "run_tests": ("local", t_run_tests, None),
    "read_review": ("read_only", t_read_review, None),
    "push_branch": ("checkable", t_push_branch, lookup_branch),
    "open_pr": ("checkable", t_open_pr, lookup_pr),
    "open_pr_naive": ("naive", t_open_pr_naive, None),
    "comment_on_pr": ("idempotent", t_comment, None),
    "delete_branch_prod": ("unsafe", t_delete_prod, None),
}


# ------------------------------------------------------------------------------------------------ the ledger --
def call_tool(h, m, body):
    name = m.group(1)
    if name not in TOOLS:
        return 404, {"error": f"unknown tool {name}"}
    klass, fn, lookup = TOOLS[name]
    session, step, args, sb = body["session"], int(body["step"]), body.get("args", {}), body.get("sandbox_ip")
    key = h.headers.get("Idempotency-Key") or f"{session}:{step}"
    if klass in ("local", "read_only"):
        status, res = fn(session, step, args, sb, key)
        return (200, {"tool": name, "class": klass, "result": res}) if status == 200 else (502, {"error": res})

    with db() as c:
        row = c.execute("INSERT INTO ledger (key, session, step, tool, class, status, attempts) VALUES (%s,%s,%s,%s,%s,'started',1) "
                        "ON CONFLICT (key) DO UPDATE SET attempts = ledger.attempts + 1, updated = now() "
                        "RETURNING status, result, attempts", (key, session, step, name, klass)).fetchone()
        if row["status"] == "done":
            c.execute("UPDATE ledger SET deduplicated = deduplicated + 1 WHERE key=%s", (key,))
            return 200, {"tool": name, "class": klass, "result": row["result"], "deduplicated": True, "via": "ledger"}
    attempts = row["attempts"]
    via = "performed"
    if klass == "unsafe" and attempts > 1:
        return 409, {"error": "a previous attempt may have changed the world and cannot be checked; a human must decide", "needs_human": True}
    if klass == "checkable" and attempts > 1 and lookup:
        found = lookup(session, step, args)
        if found:
            res, via = ({"number": found["number"], "head": found["head"]} if name == "open_pr" else {"branch": args["branch"]}), "lookup"
            with db() as c:
                c.execute("UPDATE ledger SET status='done', result=%s, external_ref=%s, deduplicated = deduplicated + 1, updated=now() WHERE key=%s",
                          (json.dumps(res), str(res.get("number") or res.get("branch") or ""), key))
            return 200, {"tool": name, "class": klass, "result": res, "deduplicated": True, "via": "lookup"}
    status, res = fn(session, step, args, sb, key)
    if status != 200:
        return 502, {"error": res}
    ref = str(res.get("number") or res.get("comment_id") or res.get("branch") or "")
    with db() as c:
        c.execute("UPDATE ledger SET status='done', result=%s, external_ref=%s, updated=now() WHERE key=%s", (json.dumps(res), ref, key))
    return 200, {"tool": name, "class": klass, "result": res, "deduplicated": False, "via": via}


def compensate(h, m, body):
    """Undo what the ledger shows this session did, newest first (the Saga pattern). Comments cannot be unsent and are left."""
    session, from_step = body["session"], int(body.get("from_step", 0))
    with db() as c:
        rows = c.execute("SELECT key, step, tool, result, status FROM ledger WHERE session=%s AND step >= %s AND status='done' "
                         "ORDER BY step DESC", (session, from_step)).fetchall()
    undone = []
    for r in rows:
        res = r["result"]
        if r["tool"] in ("open_pr", "open_pr_naive"):
            http("POST", f"{EXTERNAL_GIT}/pulls/{res['number']}/close", {})
            undone.append(f"closed PR #{res['number']}")
        elif r["tool"] == "push_branch":
            http("DELETE", f"{EXTERNAL_GIT}/repos/{REPO}/branches/{res['branch'].replace('/', '%2F')}")
            undone.append(f"deleted branch {res['branch']}")
        else:
            continue
        with db() as c:
            c.execute("UPDATE ledger SET status='compensated', updated=now() WHERE key=%s", (r["key"],))
    return 200, {"compensated": undone}


def ledger_view(h, m, body):
    with db() as c:
        rows = c.execute("SELECT key, step, tool, class, status, attempts, deduplicated, external_ref FROM ledger WHERE session=%s ORDER BY step",
                         (m.group(1),)).fetchall()
    return 200, {"ledger": rows}


class H(Handler):
    routes = {
        ("POST", r"/tools/([a-z_]+)"): call_tool,
        ("POST", r"/compensate"): compensate,
        ("GET", r"/ledger/(.+)"): ledger_view,
        ("GET", r"/classes"): lambda h, m, b: (200, {n: t[0] for n, t in TOOLS.items()}),
        ("GET", r"/healthz"): lambda h, m, b: (200, {"ok": True}),
    }


def main():
    serve(H)

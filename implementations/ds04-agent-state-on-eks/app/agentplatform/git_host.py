"""A stand-in for the external system an agent changes: a Git host with branches, pull requests and comments.

It exists so the side-effect tests are real: it keeps its own state, it can honour an Idempotency-Key header, and it can be told
to lose the acknowledgement of the next N changes (it makes the change, then answers with an error), which is exactly the
failure the design describes: "the pull request was opened, but the worker never learned it".
"""
from urllib.parse import unquote

from .common import Handler, db, serve


def lose_ack(op, h=None):
    """True when the next change of this kind must be made and then reported as failed. A human's own actions are never affected."""
    if h is not None and h.headers.get("X-Actor") == "human":
        return False
    with db() as c:
        r = c.execute("UPDATE ext_faults SET v = v - 1 WHERE k=%s AND v > 0 RETURNING v", (f"ack_loss:{op}",)).fetchone()
    return r is not None


def create_branch(h, m, body):
    repo = m.group(1)
    with db() as c:
        c.execute("INSERT INTO ext_branches (repo, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (repo, body["name"]))
    if lose_ack("branches", h):
        return 502, {"error": "upstream timeout after the branch was created"}
    return 200, {"branch": body["name"]}


def get_branch(h, m, body):
    with db() as c:
        r = c.execute("SELECT name FROM ext_branches WHERE repo=%s AND name=%s", (m.group(1), unquote(m.group(2)))).fetchone()
    return (200, {"branch": r["name"]}) if r else (404, {"error": "no such branch"})


def delete_branch(h, m, body):
    with db() as c:
        c.execute("DELETE FROM ext_branches WHERE repo=%s AND name=%s", (m.group(1), unquote(m.group(2))))
    if lose_ack("deletes", h):
        return 502, {"error": "upstream timeout after the branch was deleted"}
    return 200, {"deleted": unquote(m.group(2))}


def create_pr(h, m, body):
    repo, key = m.group(1), h.headers.get("Idempotency-Key")
    with db() as c:
        if key:
            r = c.execute("SELECT number, head, state FROM ext_prs WHERE idem_key=%s", (key,)).fetchone()
            if r:
                return 200, {**r, "replayed": True}
        r = c.execute("INSERT INTO ext_prs (repo, head, title, idem_key) VALUES (%s,%s,%s,%s) RETURNING number, head, state",
                      (repo, body["head"], body["title"], key)).fetchone()
    if lose_ack("pulls", h):
        return 502, {"error": "upstream timeout after the pull request was created"}
    return 200, r


def list_prs(h, m, body):
    from .common import query_params
    q = query_params(h.path)
    with db() as c:
        if q.get("head"):
            rows = c.execute("SELECT number, head, title, state FROM ext_prs WHERE repo=%s AND head=%s AND state='open' ORDER BY number",
                             (m.group(1), q["head"])).fetchall()
        else:
            rows = c.execute("SELECT number, head, title, state FROM ext_prs WHERE repo=%s ORDER BY number", (m.group(1),)).fetchall()
    return 200, {"pulls": rows}


def close_pr(h, m, body):
    with db() as c:
        c.execute("UPDATE ext_prs SET state='closed' WHERE number=%s", (int(m.group(1)),))
    return 200, {"closed": int(m.group(1))}


def add_comment(h, m, body):
    pr, key = int(m.group(1)), h.headers.get("Idempotency-Key")
    with db() as c:
        if key:
            r = c.execute("SELECT id FROM ext_comments WHERE idem_key=%s", (key,)).fetchone()
            if r:
                return 200, {"id": r["id"], "replayed": True}
        r = c.execute("INSERT INTO ext_comments (pr, body, idem_key) VALUES (%s,%s,%s) RETURNING id", (pr, body["body"], key)).fetchone()
    if lose_ack("comments", h):
        return 502, {"error": "upstream timeout after the comment was posted"}
    return 200, r


def list_comments(h, m, body):
    with db() as c:
        rows = c.execute("SELECT id, body FROM ext_comments WHERE pr=%s ORDER BY id", (int(m.group(1)),)).fetchall()
    return 200, {"comments": rows}


def set_fault(h, m, body):
    """Lose the acknowledgement of the next `count` changes of one kind: branches, pulls, comments or deletes."""
    op = body["op"]
    with db() as c:
        c.execute("INSERT INTO ext_faults (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (f"ack_loss:{op}", int(body.get("count", 0))))
    return 200, {"ack_loss": {op: int(body.get("count", 0))}}


def counts(h, m, body):
    with db() as c:
        prs = c.execute("SELECT COUNT(*) AS n FROM ext_prs WHERE state='open'").fetchone()["n"]
        allp = c.execute("SELECT COUNT(*) AS n FROM ext_prs").fetchone()["n"]
        br = c.execute("SELECT COUNT(*) AS n FROM ext_branches").fetchone()["n"]
        cm = c.execute("SELECT COUNT(*) AS n FROM ext_comments").fetchone()["n"]
    return 200, {"open_prs": prs, "all_prs": allp, "branches": br, "comments": cm}


def reset(h, m, body):
    with db() as c:
        c.execute("TRUNCATE ext_branches, ext_prs, ext_comments RESTART IDENTITY")
        c.execute("DELETE FROM ext_faults")
    return 200, {"reset": True}


class H(Handler):
    routes = {
        ("POST", r"/repos/([^/]+)/branches"): create_branch,
        ("GET", r"/repos/([^/]+)/branches/(.+)"): get_branch,
        ("DELETE", r"/repos/([^/]+)/branches/(.+)"): delete_branch,
        ("POST", r"/repos/([^/]+)/pulls"): create_pr,
        ("GET", r"/repos/([^/]+)/pulls"): list_prs,
        ("POST", r"/pulls/(\d+)/close"): close_pr,
        ("POST", r"/pulls/(\d+)/comments"): add_comment,
        ("GET", r"/pulls/(\d+)/comments"): list_comments,
        ("POST", r"/admin/ack-loss"): set_fault,
        ("GET", r"/admin/counts"): counts,
        ("POST", r"/admin/reset"): reset,
        ("GET", r"/healthz"): lambda h, m, b: (200, {"ok": True}),
    }


def main():
    serve(H)

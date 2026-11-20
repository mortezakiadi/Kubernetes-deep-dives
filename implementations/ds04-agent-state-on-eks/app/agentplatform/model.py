"""Context state (Design 04, "Context state") and the stand-in model.

`build_prompt` rebuilds the model's input from the session database every time it is asked: the system prompt, the latest
summary, and the log entries after that summary. Nothing is kept in a worker's memory between steps, so a restarted
worker produces exactly the prompt the old one would have.

`decide` is the stand-in for a language model. It reads the prompt and returns the next action by a fixed script. The design is
about what happens around the model (recovery, retries, side effects), not about how well it reasons, and a scripted model
lets every failure test be exact and repeatable. It is deliberately a function of the prompt alone: if the prompt is
rebuilt wrongly, the action changes, and the tests notice.
"""
import json
import time

from .common import SYSTEM_PROMPT, db, sha, tokens

ROLE_ORDER = {"user": 0, "assistant": 1, "tool": 2}


def append_entry(session, step, role, content):
    """Idempotent: a retried step writes the same row again, which changes nothing."""
    text = content if isinstance(content, str) else json.dumps(content, sort_keys=True)
    with db() as c:
        c.execute("INSERT INTO context_log (session, step, role, content, tokens) VALUES (%s,%s,%s,%s,%s) "
                  "ON CONFLICT (session, step, role) DO NOTHING", (session, step, role, text, tokens(text)))


def latest_summary(session, before_step):
    with db() as c:
        return c.execute("SELECT id, upto_step, text, tokens FROM summaries WHERE session=%s AND upto_step < %s "
                         "ORDER BY upto_step DESC LIMIT 1", (session, before_step)).fetchone()


def entries_after(session, after_step, before_step):
    with db() as c:
        rows = c.execute("SELECT step, role, content, tokens FROM context_log WHERE session=%s AND step > %s AND step < %s",
                         (session, after_step, before_step)).fetchall()
    return sorted(rows, key=lambda r: (r["step"], ROLE_ORDER.get(r["role"], 9)))


def build_prompt(session, step):
    summ = latest_summary(session, step)
    base = summ["upto_step"] if summ else -2
    rows = entries_after(session, base, step)
    lines = [f"SYSTEM: {SYSTEM_PROMPT}"]
    if summ:
        lines.append(f"[summary] {summ['text']}")
    lines += [f"[{r['role']}] {r['content']}" for r in rows]
    return "\n".join(lines), (summ["id"] if summ else None), sum(r["tokens"] for r in rows)


def context_tokens_since_summary(session, step):
    summ = latest_summary(session, step)
    base = summ["upto_step"] if summ else -2
    return sum(r["tokens"] for r in entries_after(session, base, step)) + (summ["tokens"] if summ else 0)


# ------------------------------------------------------------------------------------------------ stand-in model --
def _parse(prompt):
    summary, entries = None, []
    for line in prompt.splitlines():
        if line.startswith("[summary] "):
            summary = json.loads(line[len("[summary] "):])
        elif line.startswith("[") and "] " in line:
            role, rest = line[1:].split("] ", 1)
            try:
                entries.append((role, json.loads(rest)))
            except json.JSONDecodeError:
                pass
    return summary, entries


def state_from(prompt):
    summary, entries = _parse(prompt)
    task = dict(summary["task"]) if summary else {}
    tools_done = list(summary["tools_done"]) if summary else []
    facts = dict(summary["facts"]) if summary else {}
    hint = summary.get("hint") if summary else None
    for role, obj in entries:
        if role == "user":
            if "task" in obj:
                task = obj["task"]
            if "hint" in obj:
                hint = obj["hint"]
        elif role == "tool":
            tools_done.append(obj["tool"])
            res = obj.get("result", {})
            if obj["tool"] == "open_pr":
                facts["pr"] = res.get("number")
            if obj["tool"] == "read_review":
                facts["review"] = res.get("comment")
            if obj["tool"] == "push_branch":
                facts["branch"] = res.get("branch")
    return task, tools_done, facts, hint


SCRIPT = ["list_dir", "write_file", "write_file", "run_tests", "push_branch", "open_pr", "read_review",
          "write_file", "run_tests", "comment_on_pr"]

SLUG_SRC = ('import re\n\n\ndef slugify(text):\n    """Lowercase, replace runs of other characters by a dash."""\n'
            '    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n')
SLUG_TEST = ('import unittest\nfrom src.slugify import slugify\n\n\nclass T(unittest.TestCase):\n'
             '    def test_basic(self):\n        self.assertEqual(slugify("Hello, World!"), "hello-world")\n'
             '    def test_repeats(self):\n        self.assertEqual(slugify("a  --  b"), "a-b")\n')


def decide(prompt, session, step):
    """The next action, from the prompt alone."""
    task, tools_done, facts, hint = state_from(prompt)
    if task.get("think_seconds"):
        time.sleep(float(task["think_seconds"]))
    n = len(tools_done)
    if task.get("behavior") == "loop" and n >= 3:
        return {"done": False, "tool": {"name": "list_dir", "args": {"path": "."}, "needs_approval": False, "changes_files": False}}
    if task.get("behavior") == "unsafe" and n == 4:
        return {"done": False, "tool": {"name": "delete_branch_prod", "args": {"branch": f"agent/{session}-4"}, "needs_approval": False, "changes_files": False}}
    if n >= len(SCRIPT):
        return {"done": True, "summary": f"Opened PR #{facts.get('pr')} and addressed the review."}
    name = SCRIPT[n]
    args = {}
    changes = False
    if name == "list_dir":
        args = {"path": "."}
    elif name == "write_file":
        changes = True
        if tools_done.count("write_file") == 0:
            args = {"path": "src/slugify.py", "content": SLUG_SRC}
        elif tools_done.count("write_file") == 1:
            args = {"path": "tests/test_slugify.py", "content": SLUG_TEST}
        else:
            note = (facts.get("review") or "no review comment")[:120]
            extra = f"\n\n# Review: {note}\n" + (f"# Hint: {hint}\n" if hint else "")
            args = {"path": "src/slugify.py", "content": SLUG_SRC + extra}
    elif name == "run_tests":
        args = {"seconds": int(task.get("test_seconds", 4))}
    elif name == "push_branch":
        args = {"branch": f"agent/{session}-{step}"}
    elif name == "open_pr":
        args = {"head": facts.get("branch"), "title": f"Add slugify ({task.get('ticket', 'ticket')})"}
        if task.get("naive_pr"):
            return {"done": False, "tool": {"name": "open_pr_naive", "args": args, "needs_approval": False, "changes_files": False}}
    elif name == "read_review":
        args = {"pr": facts.get("pr")}
    elif name == "comment_on_pr":
        args = {"pr": facts.get("pr"), "body": f"Addressed the review: {(facts.get('review') or '')[:100]}"}
    return {"done": False, "tool": {"name": name, "args": args, "needs_approval": name == "read_review", "changes_files": changes}}


def summarize(prompt):
    """What a compaction step produces. It keeps everything `decide` needs, and nothing else."""
    task, tools_done, facts, hint = state_from(prompt)
    return {"task": task, "tools_done": tools_done, "facts": facts, "hint": hint,
            "note": f"{len(tools_done)} tool steps done: " + ", ".join(tools_done)}


def prompt_sha(prompt):
    return sha(prompt)

"""The agent's side of a turn: the prompt it sees (with deterministic context compaction), the call to the rollout
engine (vLLM, with per-token log-probabilities), and the parsed action.

"Train on what the model saw" (Design 08, "Context management"): the exact message list of every turn is returned
with the turn, so the trainer can compute log-probabilities on that context and not on the full history.
"""
import json
import math
import re

from common import LLM, jhttp

MODEL = "policy"
SYSTEM = """You are a software engineer fixing bugs in a small Python repository.
Every reply: one short sentence of reasoning, then exactly one line that starts with ACTION: and a JSON object.
ACTION: {"tool": "ls"}
ACTION: {"tool": "cat", "path": "shop/cart.py"}
ACTION: {"tool": "replace", "path": "shop/cart.py", "old": "exact old text", "new": "new text"}
ACTION: {"tool": "write", "path": "tests/test_repro.py", "content": "file content"}
ACTION: {"tool": "run_tests"}
ACTION: {"tool": "submit"}
Only these six tools exist, and only the first ACTION line of a reply runs. Work like this: list the files; cat the file that defines a function named in the issue;
replace the wrong line, copying the old text exactly from the file; do the same for every problem in the issue; run the
tests; submit when every problem is fixed."""

CONTEXT_BUDGET_CHARS = 7000   # past this, older observations are replaced by a one-line note
KEEP_RECENT = 2               # the last two turns' observations are always kept in full


def messages(issue, turns, compacted_upto):
    """The context of the next turn. Observations of turns with index < compacted_upto are replaced."""
    out = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": issue}]
    for t in turns:
        out.append({"role": "assistant", "content": t["output"]})
        obs = t["observation"]
        if t["index"] < compacted_upto:
            obs = f"[observation of turn {t['index']} removed to save context; it was {len(obs)} characters]"
        out.append({"role": "user", "content": "OBSERVATION:\n" + obs})
    return out


def context_for(issue, turns, compacted_upto):
    """Apply the compaction rule; returns (messages, compacted_upto, compacted_now). Deterministic: it depends
    only on recorded text."""
    msgs = messages(issue, turns, compacted_upto)
    if sum(len(m["content"]) for m in msgs) > CONTEXT_BUDGET_CHARS and len(turns) > KEEP_RECENT:
        new = len(turns) - KEEP_RECENT
        if new > compacted_upto:
            return messages(issue, turns, new), new, True
    return msgs, compacted_upto, False


def parse_action(text):
    lines = [l for l in text.splitlines() if l.strip().startswith("ACTION:")]
    if not lines:
        return {"tool": "invalid", "error": "no ACTION line"}
    raw = lines[0].strip()[len("ACTION:"):].strip()   # one action per turn: the first ACTION line is the one that runs
    try:
        a = json.loads(raw)
        if not (isinstance(a, dict) and "tool" in a):
            return {"tool": "invalid", "error": "not a tool call"}
        if len(lines) > 1:
            a["_ignored_actions"] = len(lines) - 1
        return a
    except json.JSONDecodeError:
        m = re.search(r'"tool"\s*:\s*"(\w+)"', raw)
        return {"tool": "invalid", "error": "bad JSON", "intended": m.group(1) if m else None}


def _entropy(top):
    ps = [math.exp(x["logprob"]) for x in top]
    z = sum(ps) or 1.0
    return -sum((p / z) * math.log(p / z) for p in ps if p > 0)


def first_action_entropy(lp):
    """Entropy (over the top-5 alternatives, renormalised) of the first token of the tool name, i.e. how uncertain
    the model was about WHICH action to take. Falls back to the first token of the reply."""
    text, offsets = b"", []
    for e in lp:
        offsets.append(len(text))
        text += bytes(e.get("bytes") or [])
    m = re.search(rb'ACTION:\s*\{\s*"tool"\s*:\s*"', text)
    idx = 0
    if m:
        idx = next((i for i, o in enumerate(offsets) if o >= m.end()), 0)
    return round(_entropy(lp[idx].get("top_logprobs") or [{"logprob": 0.0}]), 4), idx


def generate(msgs, seed, temperature=0.7, max_tokens=320):
    r = jhttp("POST", f"{LLM}/v1/chat/completions", {
        "model": MODEL, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens, "seed": seed,
        "logprobs": True, "top_logprobs": 5, "return_tokens_as_token_ids": True,
        # One turn is one action: generation stops where a second ACTION line would begin.
        "stop": ["\nACTION:"]}, timeout=300)
    ch = r["choices"][0]
    lp = ch["logprobs"]["content"]
    return {"output": ch["message"]["content"], "finish_reason": ch["finish_reason"],
            "prompt_tokens": r["usage"]["prompt_tokens"], "completion_tokens": r["usage"]["completion_tokens"],
            "token_ids": [int(e["token"].split(":", 1)[1]) for e in lp], "logprobs": [round(e["logprob"], 5) for e in lp],
            "lp_raw": lp}

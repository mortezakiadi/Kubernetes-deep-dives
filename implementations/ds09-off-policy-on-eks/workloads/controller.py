"""The off-policy controller of Design 09: rules in a YAML file, graduated responses, hysteresis, every action logged as an event.

The rules file is versioned with the recipe (recipes/offpolicy-controller.yaml). This module only reads it and decides; the trainer applies
what it decides (it owns the weights, the optimizer and the buffers). Nothing here imports torch.
"""
import ast
import operator

SAFE_FUNCS = {"max": max, "min": min}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def safe_eval(expr, current):
    """Evaluate `max(1, current - 2)`-style expressions: numbers, `current`, + - * /, max and min. Nothing else is allowed."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name) and n.id == "current":
            return current
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in SAFE_FUNCS and not n.keywords:
            return SAFE_FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"expression not allowed: {expr!r}")
    return ev(ast.parse(str(expr), mode="eval"))


def _holds(cond, history):
    """A condition holds when it holds in each of the last `for_steps` records (default 1)."""
    if "all" in cond:
        return all(_holds(c, history) for c in cond["all"])
    if "any" in cond:
        return any(_holds(c, history) for c in cond["any"])
    if "at_least" in cond:       # in at least N of the last M steps, not necessarily in a row
        m = int(cond["of_steps"])
        hits = 0
        for rec in history[-m:]:
            v = rec.get(cond["metric"])
            if v is not None and (("above" in cond and v > cond["above"]) or ("below" in cond and v < cond["below"])):
                hits += 1
        return len(history) >= m and hits >= int(cond["at_least"])
    n = int(cond.get("for_steps", 1))
    if len(history) < n:
        return False
    for rec in history[-n:]:
        v = rec.get(cond["metric"])
        if v is None:
            return False
        if "above" in cond and not v > cond["above"]:
            return False
        if "below" in cond and not v < cond["below"]:
            return False
    return True


def _metrics_in(cond):
    if "all" in cond or "any" in cond:
        return [m for c in cond.get("all", cond.get("any")) for m in _metrics_in(c)]
    return [cond["metric"]]


class Controller:
    """`settings` holds the live values the rules may change (the trainer reads them back); `baseline` is where the relax rule stops."""

    def __init__(self, spec, settings):
        self.spec = spec or {}
        self.rules = self.spec.get("rules", [])
        self.settings = dict(settings)
        self.baseline = dict(settings)
        self.last_trigger = {}      # rule name -> step
        self.last_any = -10 ** 9
        self.restores = []          # (step, key, value)
        self.events = []
        self.healthy_settings = None  # set by the trainer: the settings in force at the newest healthy checkpoint

    # -- one decision per optimizer step ---------------------------------------------------------------------------------
    def step(self, step, history):
        """Look at the history (the newest record last) and return the actions to apply now, each as a dict. Also records them as events."""
        actions = []
        for key_step, key, value in list(self.restores):   # a temporary change ends on schedule
            if step >= key_step:
                self.restores.remove((key_step, key, value))
                self.settings[key] = value
                actions.append(self._log(step, "restore", {"set": key, "to": value}, {}, f"{key} restored after its temporary change"))
        for rule in self.rules:
            name = rule["name"]
            cooldown = int(rule.get("cooldown_steps", self.spec.get("window_steps", 10)))
            if step - self.last_trigger.get(name, -10 ** 9) < cooldown:
                continue
            if _holds(rule["when"], history):
                vals = {m: history[-1].get(m) for m in set(_metrics_in(rule["when"]))}
                for a in rule["then"]:
                    actions.append(self._apply(step, name, a, vals))
                self.last_trigger[name] = step
                self.last_any = step
                return actions  # one rule per step
        relax = self.spec.get("relax")
        if relax and step - self.last_any > int(relax["healthy_for_steps"]) and any(self.settings[k] != self.baseline[k] for k in self.baseline):
            changed = {}
            for key, how in relax["step_back"].items():
                if key not in self.settings:
                    continue
                cur = self.settings[key]
                new = cur * float(str(how)[1:]) if str(how).startswith("*") else cur + float(how)
                if cur >= self.baseline[key]:      # only a setting that is tighter than the recipe's is loosened
                    continue
                new = min(new, self.baseline[key])
                if new != cur:
                    self.settings[key] = new if not isinstance(self.baseline[key], int) else int(round(new))
                    changed[key] = self.settings[key]
            if changed:
                self.last_any = step   # one notch at a time: wait for another healthy stretch before the next
                actions.append(self._log(step, "relax", {"step_back": changed}, {}, "healthy for a while: settings loosened one notch"))
        return actions

    def _apply(self, step, rule, a, vals):
        if "set" in a:
            key = a["set"]
            cur = self.settings.get(key)
            if "to" in a:
                new = safe_eval(a["to"], cur)
            else:
                new = cur * float(a["multiply"])
            if isinstance(self.baseline.get(key), int):
                new = int(round(new))
            self.settings[key] = new
            if a.get("restore_after_steps"):
                self.restores.append((step + int(a["restore_after_steps"]), key, cur))
            return self._log(step, rule, a, vals, f"{key}: {cur} -> {new}")
        if a.get("action") == "rollback" and self.healthy_settings:
            self.settings.update(self.healthy_settings)   # the rule's later `set` actions are relative to the restored settings
            return self._log(step, rule, a, vals, "settings restored to those of the last healthy checkpoint: " + ", ".join(f"{k}={v}" for k, v in self.healthy_settings.items()))
        return self._log(step, rule, a, vals, a.get("action", ""))

    def _log(self, step, rule, action, vals, text):
        ev = {"step": step, "rule": rule, "action": action, "metrics": {k: (round(v, 5) if isinstance(v, float) else v) for k, v in vals.items()}, "text": text}
        self.events.append(ev)
        return ev

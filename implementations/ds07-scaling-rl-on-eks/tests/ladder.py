"""The scaling ladder as code: overlays that change one axis, and the gate that decides whether a rung may be climbed."""
import pathlib

import yaml

DIR = pathlib.Path(__file__).resolve().parents[1] / "recipes" / "ladder"
AXES = {
    "recipe": {"LEARNING_RATE", "SCORE_CENTERING", "EPSILON_HIGH", "MAX_STALENESS", "WEIGHT_SYNC_STEPS", "MAX_CONCURRENT", "LR_SCHEDULE", "NUM_GENERATIONS"},
    "length": {"MAX_COMPLETION", "THINKING"},
    "model_size": {"MODEL", "LORA"},
    "tokens_per_step": {"BATCH_ROWS", "GRAD_ACCUM"},
    "steps": {"MAX_STEPS"},
}


def load(name, overlay_dir=DIR):
    """Resolve `extends` and return (ladder block, merged settings, settings this overlay itself sets)."""
    doc = yaml.safe_load((overlay_dir / name).read_text())
    own = dict(doc.get("settings") or {})
    if doc.get("extends"):
        _, parent, _ = load(doc["extends"], overlay_dir)
    else:
        parent = {}
    return doc.get("ladder", {}), {**parent, **own}, own


def check_one_axis(name, overlay_dir=DIR):
    """Returns (ok, message): the overlay's own settings must all belong to the one axis it declares."""
    ladder, merged, own = load(name, overlay_dir)
    axis = ladder.get("axis")
    if ladder.get("rung") == 1:
        return True, "rung 1 defines the starting recipe"
    allowed = AXES.get(axis, set())
    touched = {a for k in own for a, ks in AXES.items() if k in ks}
    outside = sorted(k for k in own if k not in allowed)
    if outside or touched - {axis}:
        return False, f"declares axis '{axis}' but also changes {outside} (axes {sorted(touched)})"
    return True, f"changes only {sorted(own)}, all of axis '{axis}'"


def gate(criteria, evidence):
    """criteria: the overlay's exit_criteria. evidence: measured values. Returns (may_climb, [(criterion, wanted, measured, ok)])."""
    rows = []
    if "curve_fit_ceiling_ci" in criteria:
        got = evidence.get("ceiling_ci")
        rows.append(("curve fit: 2 x std of the ceiling", f"<= {criteria['curve_fit_ceiling_ci']}", got, got is not None and got <= criteria["curve_fit_ceiling_ci"]))
    if "unexplained_wait_fraction_max" in criteria:
        got = evidence.get("unexplained_wait")
        rows.append(("unexplained waiting, share of step", f"<= {criteria['unexplained_wait_fraction_max']}", got, got is not None and got <= criteria["unexplained_wait_fraction_max"]))
    if criteria.get("restart_from_checkpoint_tested"):
        got = evidence.get("restart_tested")
        rows.append(("restart from a checkpoint tested", "yes", got, bool(got)))
    return all(r[3] for r in rows), rows

"""The correction library of Design 09: the loss, the behavior weights and the off-policy metrics, in one place.

Three policies appear in every batch.
  mu         the behavior policy: the engine that sampled the tokens, at the weights it held then (its log-probabilities are recorded per token)
  pi_prox    the proximal policy: the trainer's own weights at the version that produced the sample, scored on arrival
  pi_theta   the target policy: the trainer's weights now

`naive` clips pi_theta / mu, so engine mismatch and staleness are one number. `decoupled` clips pi_theta / pi_prox (the trust region) and
multiplies by the behavior weight pi_prox / mu, truncated and masked when it is extreme (the correction for the engine and for nothing else).
Nothing here talks to a cluster or to TRL: tensors in, tensors out, so the tests run on a laptop.
"""
import math

import torch

LOG_CAP = 20.0


def behavior_weight(lp_prox, lp_beh, mask, truncate_at=3.0, mask_above=10.0):
    """pi_prox / mu per token: truncated at `truncate_at`, and set to zero (masked) above `mask_above`. Returns (weight, kept, was_truncated)."""
    d = torch.nan_to_num((lp_prox - lp_beh), nan=0.0).clamp(-LOG_CAP, LOG_CAP)
    w = d.exp()
    kept = mask * (w <= mask_above).to(mask.dtype)
    truncated = mask * ((w > truncate_at) & (w <= mask_above)).to(mask.dtype)
    return w.clamp(max=truncate_at) * kept, kept, truncated


def policy_loss(lp, lp_prox, lp_beh, adv, mask, mode="decoupled", eps_low=0.2, eps_high=0.2, truncate_at=3.0, mask_above=10.0):
    """Per-token policy-gradient loss, shape like `lp`, and the diagnostics of the step.

    lp        log pi_theta, with gradient         lp_prox   log pi_prox (no gradient)        lp_beh    log mu (no gradient)
    adv       advantage, shape (B, 1) or (B, T)    mask      completion tokens that count
    """
    lp_beh = torch.where(torch.isnan(lp_beh), lp_prox, lp_beh)  # a token the engine could not score is treated as on-policy
    if mode == "naive":
        old = lp_beh
        weight = torch.ones_like(lp)
        kept = mask
        truncated = torch.zeros_like(mask)
    elif mode == "decoupled":
        old = lp_prox
        weight, kept, truncated = behavior_weight(lp_prox, lp_beh, mask, truncate_at, mask_above)
    else:
        raise ValueError(f"unknown correction mode: {mode}")
    ratio = (lp - old).clamp(-LOG_CAP, LOG_CAP).exp()
    clipped_ratio = ratio.clamp(1 - eps_low, 1 + eps_high)
    per_token = -torch.min(ratio * adv, clipped_ratio * adv) * weight
    # the trust region cut a token when the clipped branch was the smaller one and the gradient therefore vanished
    cut = ((ratio * adv > clipped_ratio * adv) & (mask > 0)).to(mask.dtype)
    diag = {"clipped_frac": (cut * kept).sum() / mask.sum().clamp(min=1), "masked_frac": 1 - kept.sum() / mask.sum().clamp(min=1),
            "truncated_frac": truncated.sum() / mask.sum().clamp(min=1)}
    return per_token, diag


def _k3(log_ratio):
    """KL estimator k3 of the policy that sampled (denominator) against the other, from log(p/q) at samples drawn from q."""
    return (log_ratio.exp() - 1) - log_ratio


@torch.no_grad()
def offpolicy_metrics(lp, lp_prox, lp_beh, mask, ages):
    """The metrics the design asks every run to report, from the stored log-probabilities (one value per name, floats).

    Three comparisons, because they answer different questions:
      offpolicy/*   pi_theta against mu       how far the data is from the policy that is being trained (the number the controller watches)
      mismatch/*    pi_prox against mu        the engine alone: it is the only gap at age 0
      drift/*       pi_theta against pi_prox  staleness alone
    """
    m = mask.bool()
    lp_beh = torch.where(torch.isnan(lp_beh), lp_prox, lp_beh)
    out = {}

    def ess(logw_tok):
        w = logw_tok[m].exp()
        return ((w.sum() ** 2) / (w.pow(2).sum() * w.numel())).item()

    def ess_seq(logw_tok):
        s = (logw_tok * mask).sum(-1)
        s = s - s.max()  # only the shape of the weights matters for ESS/N; this keeps exp() finite
        w = s.exp()
        return ((w.sum() ** 2) / (w.pow(2).sum() * w.numel())).item()

    total = (lp.detach() - lp_beh).clamp(-LOG_CAP, LOG_CAP)
    mismatch = (lp_prox - lp_beh).clamp(-LOG_CAP, LOG_CAP)
    drift = (lp.detach() - lp_prox).clamp(-LOG_CAP, LOG_CAP)
    w = total[m].exp()
    out["offpolicy/ess_frac"] = ess(total)
    out["offpolicy/ess_seq_frac"] = ess_seq(total)
    out["offpolicy/ratio_p50"] = w.quantile(0.5).item()
    out["offpolicy/ratio_p99"] = w.quantile(0.99).item()
    out["offpolicy/ratio_max"] = w.max().item()
    out["offpolicy/ratio_gt2_frac"] = (w > 2).float().mean().item()
    out["offpolicy/ratio_gt3_frac"] = (w > 3).float().mean().item()
    out["offpolicy/behavior_kl"] = _k3(total[m]).mean().item()
    wm = mismatch[m].exp()
    out["mismatch/ess_frac"] = ess(mismatch)
    out["mismatch/ratio_p99"] = wm.quantile(0.99).item()
    out["mismatch/ratio_gt3_frac"] = (wm > 3).float().mean().item()
    out["mismatch/kl"] = _k3(mismatch[m]).mean().item()
    out["drift/kl"] = _k3(drift[m]).mean().item()
    out["drift/abs_log_ratio"] = drift[m].abs().mean().item()
    for a in sorted(set(int(x) for x in ages.tolist())):  # per age: is staleness the cause, or the engine?
        sel = (ages == a).unsqueeze(-1).expand_as(mask).bool() & m
        if sel.any():
            out[f"offpolicy/behavior_kl_age{a}"] = _k3(total[sel]).mean().item()
            out[f"mismatch/kl_age{a}"] = _k3(mismatch[sel]).mean().item()
            out[f"drift/kl_age{a}"] = _k3(drift[sel]).mean().item()
    return out


def sft_loss(lp, mask):
    """The separate loss for external data: mean negative log-likelihood of the tokens. Nothing of the RL objective is in it."""
    return -(lp * mask).sum() / mask.sum().clamp(min=1)


def reduce_loss(per_token, mask, accum):
    """Token-weighted mean over the micro-batch, divided by the accumulation count (the optimizer step sums the micro-batches)."""
    return (per_token * mask).sum() / mask.sum().clamp(min=1.0) / accum


def entropy_slope(values):
    """Least-squares slope of the series per step; zero for fewer than three points."""
    n = len(values)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(values) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / den if den else 0.0


def finite(x, default=0.0):
    return default if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else x

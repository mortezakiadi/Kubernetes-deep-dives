"""The run health monitor (Design 06, "Stability: the signals that predict a collapse").

A Deployment that reads every active run's metrics from the tracking store every 15 seconds and raises an alert the
first time a rule fires. The rules look at TRENDS, comparing the latest window of steps with the run's own first
window, as the design asks, rather than at fixed values. Alerts go to runs/<run_id>/alerts.json and to this pod's log.

Reward collapse is checked too, but only to show WHEN it happens: it is the lagging signal the others should beat.
"""
import json
import os
import statistics
import time

import boto3

BUCKET = os.environ["BUCKET"]
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-2"))
BASE, W = 3, 2  # the run's first 3 logged steps are its baseline; the latest window is 2 steps


def mean(v):
    return statistics.mean(v) if v else None


def series(rows, key):
    return [(r["step"], r[key]) for r in rows if isinstance(r.get(key), (int, float))]


def window_rule(rows, key, test, rule, message):
    """Fire at the first step where the latest window, compared with the first window, passes the test."""
    s = series(rows, key)
    if len(s) < BASE + W:
        return None
    first = mean([v for _, v in s[:BASE]])
    for i in range(BASE + W, len(s) + 1):
        last = mean([v for _, v in s[i - W:i]])
        if first is not None and test(first, last):
            return {"rule": rule, "step": s[i - 1][0], "message": message, "first_window": round(first, 4), "latest_window": round(last, 4)}
    return None


def spike_rule(rows, key, factor, rule, message, floor=0.0):
    s = series(rows, key)
    for i in range(BASE, len(s)):
        med = statistics.median(v for _, v in s[:i])
        if s[i][1] > max(factor * med, floor):
            return {"rule": rule, "step": s[i][0], "message": message, "value": round(s[i][1], 4), "median_before": round(med, 4)}
    return None


def spread_rows(rows):
    out = []
    for r in rows:
        hi, lo = r.get("sampling/importance_sampling_ratio/max"), r.get("sampling/importance_sampling_ratio/min")
        if isinstance(hi, (int, float)) and isinstance(lo, (int, float)):
            out.append({"step": r["step"], "is_spread": hi - lo})
    return out


def check(rows):
    # Each rule compares the latest window with the run's own first window AND needs a minimum absolute change, so a
    # run whose entropy starts near zero (a confident SFT model) does not raise alerts on noise.
    rules = [
        window_rule(rows, "entropy", lambda a, b: b < 0.4 * a and a - b > 0.05, "entropy_collapse",
                    "policy entropy fell below 40% of its starting level: exploration is collapsing"),
        window_rule(rows, "entropy", lambda a, b: b > 3.0 * a and b - a > 0.2, "entropy_blowup", "policy entropy tripled: the policy is breaking down"),
        spike_rule(rows, "grad_norm", 5.0, "grad_norm_spike", "gradient norm above 5x its running median", floor=5.0),
        window_rule(spread_rows(rows), "is_spread", lambda a, b: b > 3 * a and b > 0.5, "is_ratio_widening",
                    "train/rollout importance-ratio spread tripled: mismatch or staleness is growing"),
        window_rule(rows, "completions/mean_length", lambda a, b: b > 2.0 * a, "length_explosion", "mean response length more than doubled"),
        window_rule(rows, "completions/mean_length", lambda a, b: b < 0.4 * a, "length_collapse", "mean response length fell below 40% of its start"),
        spike_rule(rows, "kl", 5.0, "kl_spike", "KL to the starting model above 5x its running median", floor=0.05),
        window_rule(rows, "reward", lambda a, b: b < 0.3 * a and a > 0.1, "reward_collapse", "reward fell below 30% of its starting level (lagging signal)"),
    ]
    return [r for r in rules if r]


def runs():
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="runs/", Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            yield p["Prefix"].split("/")[1]


def get(key):
    try:
        return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        return None


def main():
    seen = {}
    print("health monitor started; baseline", BASE, "steps, window", W, flush=True)
    while True:
        for run in runs():
            st = get(f"runs/{run}/status.json")
            if not st:
                continue
            st = json.loads(st)
            if st.get("stage") not in ("rl", "distill") or (st["state"] != "running" and time.time() - st["updated"] > 900):
                continue
            body = get(f"runs/{run}/metrics.jsonl") or ""
            rows = [json.loads(l) for l in body.splitlines() if l.strip()]
            alerts = check(rows)
            for a in alerts:
                if (run, a["rule"]) not in seen:
                    seen[(run, a["rule"])] = a
                    print("ALERT " + json.dumps({"run": run, **a}), flush=True)
            s3.put_object(Bucket=BUCKET, Key=f"runs/{run}/alerts.json",
                          Body=json.dumps({"run_id": run, "checked_at": time.time(), "steps_seen": len(rows), "alerts": alerts}, indent=1).encode())
        time.sleep(15)


if __name__ == "__main__":
    main()

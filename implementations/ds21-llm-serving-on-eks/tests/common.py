"""Shared by the scenarios: turning load-run results into rows, charts and the numbers the write-up quotes."""
import json
import math
import pathlib

import helpers as H

COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#17becf", "#7f7f7f"]


def rows_of(levels, gpus):
    out = []
    for l in levels:
        s = l["summary"]
        out.append({"rate": l["rate"], "rate_per_gpu": round(l["rate"] / gpus, 3), "goodput": round(s["goodput_tok_s_gpu"], 2), "throughput": round(s["throughput_tok_s_gpu"], 2),
                    "attain": round(s["slo_attainment"], 3), "completed": s["completed"], "requests": s["requests"],
                    "chat_ttft_p50": round(s["chat"]["ttft_p50"], 3), "chat_ttft_p95": round(s["chat"]["ttft_p95"], 3), "chat_itl_p95": round(s["chat"]["itl_p95_ms"], 1),
                    "code_ttft_p50": round(s["code"]["ttft_p50"], 3), "code_ttft_p95": round(s["code"]["ttft_p95"], 3), "code_itl_p95": round(s["code"]["itl_p95_ms"], 1)})
    return out


def table(rows, cols=("rate_per_gpu", "goodput", "attain", "chat_ttft_p95", "chat_itl_p95", "code_ttft_p95", "code_itl_p95")):
    head = {"rate_per_gpu": "req/s/GPU", "goodput": "goodput", "attain": "attain", "chat_ttft_p95": "chat TTFT p95", "chat_itl_p95": "chat ITL p95", "code_ttft_p95": "code TTFT p95", "code_itl_p95": "code ITL p95",
            "throughput": "tok/s/GPU", "chat_ttft_p50": "chat TTFT p50", "code_ttft_p50": "code TTFT p50"}
    lines = ["  ".join(f"{head.get(c, c):>13}" for c in cols)]
    for r in rows:
        lines.append("  ".join(f"{r[c]:>13}" for c in cols))
    return "\n".join(lines)


def save(out, name, obj):
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(out) / name).write_text(json.dumps(obj, indent=1, default=float))


def curves_chart(path, series, title, gpus_of):
    """series: name -> levels. Left: goodput per GPU against offered load per GPU. Right: share of requests that met their targets."""
    plt = H._plt()
    fig, (a, b) = plt.subplots(1, 2, figsize=(10.5, 4.2))
    for i, (name, levels) in enumerate(series.items()):
        g = gpus_of[name]
        x = [l["rate"] / g for l in levels]
        a.plot(x, [l["summary"]["goodput_tok_s_gpu"] for l in levels], "-o", color=COLORS[i % len(COLORS)], label=name, lw=1.6, ms=4)
        b.plot(x, [l["summary"]["slo_attainment"] for l in levels], "-o", color=COLORS[i % len(COLORS)], label=name, lw=1.6, ms=4)
    b.axhline(0.95, color="gray", ls="--", lw=1)
    b.text(b.get_xlim()[1], 0.95, " 95% line", fontsize=8, va="bottom", ha="right", color="gray")
    a.set_xlabel("offered load, requests per second per GPU")
    a.set_ylabel("goodput, tokens/s per GPU")
    b.set_xlabel("offered load, requests per second per GPU")
    b.set_ylabel("requests meeting both targets")
    b.set_ylim(-0.02, 1.05)
    a.set_title(title)
    for ax in (a, b):
        ax.grid(alpha=0.2)
    a.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def bar_chart(path, labels, values, title, ylabel, errs=None, color="#1f77b4", hline=None):
    plt = H._plt()
    fig, ax = plt.subplots(figsize=(7.5, 4.0))
    ax.bar(range(len(labels)), values, color=[COLORS[i % len(COLORS)] if color == "multi" else color for i in range(len(labels))], yerr=errs, capsize=3)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    if hline:
        ax.axhline(hline[1], color="gray", ls="--", lw=1)
        ax.text(len(labels) - 0.5, hline[1], " " + hline[0], fontsize=8, va="bottom", ha="right", color="gray")
    ax.grid(alpha=0.2, axis="y")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def cap_and_good(levels, gpus):
    """Capacity: the highest rate per GPU at which 95% of requests met both targets. Goodput: tokens/s/GPU at the best level that met the 95% line."""
    cap = H.capacity(levels) / gpus
    g, r = H.peak(levels)
    return {"capacity_rps_gpu": round(cap, 3), "goodput": round(g, 2), "goodput_at_rps_gpu": round(r / gpus, 3)}


def delta(m0, m1, name):
    return m1.get(name, 0.0) - m0.get(name, 0.0)


def ratio(a, b):
    return a / b if b else float("nan")


def finite(x):
    return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

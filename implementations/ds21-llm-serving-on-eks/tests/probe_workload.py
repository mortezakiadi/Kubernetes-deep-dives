"""Run inside the serving image after traffic.py: describe 600 requests of the trace in tokens."""
import json
import statistics
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-4B")
trace = make_trace(PromptBuilder(tok), 2.0, 300, seed=1)
out = {"requests": len(trace)}
for prod in ("chat", "code"):
    rs = [r for r in trace if r["product"] == prod]
    pt = sorted(r["prompt_tokens"] for r in rs)
    ot = sorted(r["max_tokens"] for r in rs)
    shared = PRODUCTS[prod]["shared_prefix"]
    out[prod] = {"n": len(rs), "prompt_tokens_p50": pt[len(pt) // 2], "prompt_tokens_p95": pt[int(len(pt) * 0.95)], "output_tokens_p50": ot[len(ot) // 2], "output_tokens_p95": ot[int(len(ot) * 0.95)],
                 "shared_prefix_tokens": shared, "shared_share": round(statistics.mean(shared / r["prompt_tokens"] for r in rs), 3), "groups": len({r["group"] for r in rs})}
bursts = [r["t"] for r in trace]
gaps = [b - a for a, b in zip(bursts, bursts[1:])]
out["arrivals"] = {"mean_gap_s": round(statistics.mean(gaps), 3), "p99_gap_s": round(sorted(gaps)[int(len(gaps) * 0.99)], 3), "min_gap_s": round(min(gaps), 4)}
print(json.dumps(out))

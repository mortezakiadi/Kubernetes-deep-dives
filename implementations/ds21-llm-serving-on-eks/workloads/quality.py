"""The accuracy gate of Design 21: a fixed set of questions with exact answers, asked at temperature 0 of any endpoint, so that two serving configurations of
one model can be compared on the same questions. The questions are generated from a seed (the same seed gives the same questions), in four kinds:

    add      a sum of two three-digit numbers                 mul    a product of a two-digit and a one-digit number
    word     a short story about counting things              chain  three steps of arithmetic, written as words

Few-shot prompting with a short answer: the model has no room to reason, so any damage to its arithmetic shows up as a wrong number.
Environment: TARGET, MODEL, N (questions per kind), SEED, RUN_ID, STATE_BUCKET, WORK_DIR. Result: bench/<RUN_ID>/quality.json and a done marker.
"""
import asyncio
import json
import os
import pathlib
import random

import aiohttp

TARGET = os.environ.get("TARGET", "http://localhost:8000")
MODEL = os.environ.get("MODEL", "Qwen3-4B")
N = int(os.environ.get("N", "50"))
SEED = int(os.environ.get("SEED", "7"))
RUN_ID = os.environ.get("RUN_ID", "quality")
BUCKET = os.environ.get("STATE_BUCKET", "")
WORK = pathlib.Path(os.environ.get("WORK_DIR", "/work")) / RUN_ID
WORK.mkdir(parents=True, exist_ok=True)

SHOTS = "Q: What is 214 + 389?\nA: 603\n\nQ: What is 47 times 6?\nA: 282\n\nQ: A shelf holds 12 books. Mia adds 9 and then removes 4. How many are on the shelf?\nA: 17\n\n"
THINGS = ["apples", "marbles", "coins", "stickers", "pencils", "stamps", "cards", "shells"]
NAMES = ["Ana", "Ben", "Chen", "Dara", "Eli", "Fay", "Gus", "Hana"]


def questions():
    rng = random.Random(SEED)
    qs = []
    for i in range(N):
        a, b = rng.randint(100, 999), rng.randint(100, 999)
        qs.append(("add", f"What is {a} + {b}?", str(a + b)))
    for i in range(N):
        a, b = rng.randint(12, 99), rng.randint(3, 9)
        qs.append(("mul", f"What is {a} times {b}?", str(a * b)))
    for i in range(N):
        n, t = rng.choice(NAMES), rng.choice(THINGS)
        a, b, c = rng.randint(20, 90), rng.randint(5, 19), rng.randint(5, 19)
        qs.append(("word", f"{n} has {a} {t}, finds {b} more, and gives away {c}. How many {t} does {n} have?", str(a + b - c)))
    for i in range(N):
        a, b, c = rng.randint(10, 60), rng.randint(2, 9), rng.randint(10, 90)
        qs.append(("chain", f"Take {a}, multiply it by {b}, then add {c}. What is the result?", str(a * b + c)))
    return qs


async def ask(session, sem, kind, q, ans):
    async with sem:
        body = {"model": MODEL, "prompt": SHOTS + f"Q: {q}\nA:", "max_tokens": 8, "temperature": 0.0, "stop": ["\n"]}
        async with session.post(f"{TARGET}/v1/completions", json=body) as r:
            j = await r.json()
    text = j["choices"][0]["text"].strip()
    digits = "".join(ch for ch in text.replace(",", "") if ch.isdigit())
    return {"kind": kind, "q": q, "answer": ans, "got": text, "correct": digits == ans}


async def main():
    sem = asyncio.Semaphore(16)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
        rows = await asyncio.gather(*[ask(s, sem, *q) for q in questions()])
    acc = {k: sum(r["correct"] for r in rows if r["kind"] == k) / N for k in ("add", "mul", "word", "chain")}
    out = {"run_id": RUN_ID, "model": MODEL, "n_per_kind": N, "accuracy": acc, "accuracy_all": sum(r["correct"] for r in rows) / len(rows), "rows": rows}
    (WORK / "quality.json").write_text(json.dumps(out))
    (WORK / "done").write_text("1")
    if BUCKET:
        import boto3
        c = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        for f in ("quality.json", "done"):
            c.upload_file(str(WORK / f), BUCKET, f"bench/{RUN_ID}/{f}")
    print("accuracy", json.dumps(acc), "all", round(out["accuracy_all"], 3), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

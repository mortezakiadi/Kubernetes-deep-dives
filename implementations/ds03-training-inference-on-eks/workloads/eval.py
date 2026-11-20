#!/usr/bin/env python3
"""Evaluates one registered model version on the platform's GPU, as a batch job (Design 03: "evaluation as a pipeline").

Reads the version from the registry, answers a fixed set of questions with greedy decoding, scores them, times a
throughput run, and writes the result to evals/<name>/<version>.json. The question set is small and fixed, so two
versions are always compared on exactly the same questions.
"""
import json
import os
import sys
import time

import boto3

BUCKET, NAME, VERSION = os.environ["BUCKET"], os.environ["MODEL_NAME"], os.environ["MODEL_VERSION"]
s3 = boto3.client("s3")
SUITE = [
    ("What is the capital of France? Answer with one word.", ["paris"]),
    ("What is the capital of Japan? Answer with one word.", ["tokyo"]),
    ("What is the capital of Italy? Answer with one word.", ["rome"]),
    ("What is the capital of Germany? Answer with one word.", ["berlin"]),
    ("What is the capital of Spain? Answer with one word.", ["madrid"]),
    ("What is the capital of Canada? Answer with one word.", ["ottawa"]),
    ("What is 7 plus 5? Answer with the number only.", ["12"]),
    ("What is 9 times 3? Answer with the number only.", ["27"]),
    ("What is 20 minus 8? Answer with the number only.", ["12"]),
    ("What is 6 times 7? Answer with the number only.", ["42"]),
    ("What is 100 divided by 4? Answer with the number only.", ["25"]),
    ("What is 15 plus 27? Answer with the number only.", ["42"]),
    ("What color is the sky on a clear day? Answer with one word.", ["blue"]),
    ("What color is grass? Answer with one word.", ["green"]),
    ("How many days are in a week? Answer with the number only.", ["7", "seven"]),
    ("How many months are in a year? Answer with the number only.", ["12", "twelve"]),
    ("What is the chemical symbol for water? Answer briefly.", ["h2o"]),
    ("What planet is closest to the Sun? Answer with one word.", ["mercury"]),
    ("What is the largest planet in our solar system? Answer with one word.", ["jupiter"]),
    ("Which animal says meow? Answer with one word.", ["cat"]),
    ("Which animal says woof? Answer with one word.", ["dog"]),
    ("What is the opposite of hot? Answer with one word.", ["cold"]),
    ("What is the opposite of up? Answer with one word.", ["down"]),
    ("How many legs does a spider have? Answer with the number only.", ["8", "eight"]),
    ("What is the first letter of the English alphabet? Answer with one letter.", ["a"]),
    ("What is the square root of 81? Answer with the number only.", ["9", "nine"]),
    ("What language is spoken in Brazil? Answer with one word.", ["portuguese"]),
    ("What is the freezing point of water in degrees Celsius? Answer with the number only.", ["0", "zero"]),
    ("Which ocean is the largest? Answer with one word.", ["pacific"]),
    ("What gas do plants absorb from the air? Answer briefly.", ["carbon dioxide", "co2"]),
]

local = "/tmp/model"
os.makedirs(local, exist_ok=True)
t = time.time()
manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=f"registry/models/{NAME}/{VERSION}/manifest.json")["Body"].read())
for fn in manifest["files"]:
    s3.download_file(BUCKET, f"registry/models/{NAME}/{VERSION}/{fn}", f"{local}/{fn}")
fetch_s = time.time() - t

from vllm import LLM, SamplingParams  # noqa: E402

t = time.time()
llm = LLM(model=local, max_model_len=2048, gpu_memory_utilization=0.85, enforce_eager=True)
load_s = time.time() - t
greedy = SamplingParams(temperature=0, max_tokens=24)
outs = llm.chat([[{"role": "user", "content": q}] for q, _ in SUITE], greedy)
answers = [o.outputs[0].text.strip() for o in outs]
correct = [any(g in a.lower() for g in golds) for a, (_, golds) in zip(answers, SUITE)]
# Throughput: a batch of generations of fixed length.
prompts = [[{"role": "user", "content": f"Write a short story about the number {i}."}] for i in range(64)]
t = time.time()
gen = llm.chat(prompts, SamplingParams(temperature=0.8, max_tokens=128, ignore_eos=True))
tps = sum(len(o.outputs[0].token_ids) for o in gen) / (time.time() - t)
result = {"name": NAME, "version": VERSION, "quality": sum(correct) / len(SUITE), "questions": len(SUITE), "correct": sum(correct),
          "tokens_per_second": round(tps, 1), "fetch_seconds": round(fetch_s, 1), "model_load_seconds": round(load_s, 1),
          "samples": [{"q": q, "a": a[:60], "ok": ok} for (q, _), a, ok in list(zip(SUITE, answers, correct))[:5]]}
s3.put_object(Bucket=BUCKET, Key=f"evals/{NAME}/{VERSION}.json", Body=json.dumps(result, indent=1).encode())
print(json.dumps(result))
sys.exit(0)

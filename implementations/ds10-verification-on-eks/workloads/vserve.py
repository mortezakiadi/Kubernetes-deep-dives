"""The verifier service (Design 10, "The platform on Kubernetes"): one Deployment per pinned verifier version.

    POST /verify   {"items": [{"task_id": ..., "completion": ...}, ...]}  ->  {"verifier": "name@version", "results": [...]}
    GET  /info     name, version, purpose, owner, contract, code sha256, image, and the kernel the pod sees
    GET  /healthz

VERIFIER (name@version) selects the implementation. Every result carries the verifier id, so every reward the
training run records says which version produced it.
"""
import json
import os
import pathlib
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from vbase import all_verifiers  # noqa: E402

VID = os.environ["VERIFIER"]
TASKS = {}
for f in pathlib.Path(os.environ.get("TASKS_DIR", "/tasks")).glob("*.jsonl"):
    for line in f.read_text().splitlines():
        t = json.loads(line)
        TASKS[t["id"]] = t
CLS = all_verifiers()[VID]
V = CLS(TASKS)
POOL = ThreadPoolExecutor(int(os.environ.get("WORKERS", "4")))
STATS = {"requests": 0, "items": 0, "seconds": 0.0, "verdicts": {}, "reasons": {}}


def kernel():
    try:
        return pathlib.Path("/proc/version").read_text().strip()
    except OSError:
        return platform.platform()


def one(item):
    t = TASKS.get(item["task_id"])
    if t is None:
        return {"verdict": "abstain", "score": 0.0, "reasons": ["unknown_task"], "verifier": V.id}
    try:
        return V.verify(t, item["completion"]).to_dict()
    except Exception as e:  # a verifier bug must never become a reward
        return {"verdict": "abstain", "score": 0.0, "reasons": [f"verifier_error:{type(e).__name__}"], "verifier": V.id}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path == "/info":
            return self._send(200, {"verifier": V.id, "name": CLS.name, "version": CLS.version, "kind": CLS.kind, "purpose": CLS.purpose,
                                    "owner": CLS.owner, "contract": CLS.contract, "code_sha256": CLS.code_sha256(),
                                    "image": os.environ.get("IMAGE", ""), "kernel": kernel(), "tasks": len(TASKS)})
        if self.path == "/stats":
            return self._send(200, STATS)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/verify":
            return self._send(404, {"error": "not found"})
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        t0 = time.time()
        results = list(POOL.map(one, req["items"]))
        STATS["requests"] += 1
        STATS["items"] += len(results)
        STATS["seconds"] = round(STATS["seconds"] + time.time() - t0, 3)
        for r in results:
            STATS["verdicts"][r["verdict"]] = STATS["verdicts"].get(r["verdict"], 0) + 1
            for x in r["reasons"]:
                STATS["reasons"][x.split("=")[0]] = STATS["reasons"].get(x.split("=")[0], 0) + 1
        self._send(200, {"verifier": V.id, "results": results})


if __name__ == "__main__":
    print(f"verifier {V.id} code {CLS.code_sha256()[:16]} tasks {len(TASKS)} kernel {kernel()}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

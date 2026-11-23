"""The trajectory store service (Design 08, "The trajectory as data"): an index over object storage.

    PUT  /o/<key>        write an object   (trajectories/, snapshots/, contexts/, logprobs/, credit/, curriculum/)
    GET  /o/<key>        read an object    (the above, and tasks/public/)
    GET  /list/<prefix>  keys under a prefix
    GET  /metrics        counters per prefix: objects, bytes, seconds

Environments have no AWS identity; this service is the only thing that writes to the bucket for them, and it refuses
any key outside its prefixes. The hidden tests are not in the bucket at all: only the verifier has them.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

BUCKET = os.environ["BUCKET"]
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-2"))
WRITE = ("trajectories/", "snapshots/", "contexts/", "logprobs/", "credit/", "curriculum/")
READ = WRITE + ("tasks/public/",)
STATS, LOCK = {}, threading.Lock()


def bump(prefix, op, n, secs):
    with LOCK:
        s = STATS.setdefault(prefix, {"put": 0, "get": 0, "bytes_put": 0, "put_seconds": 0.0, "get_seconds": 0.0})
        s[op] += 1
        s[f"{op}_seconds"] += secs
        if op == "put":
            s["bytes_put"] += n


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path == "/metrics":
            with LOCK:
                return self._send(200, STATS)
        if self.path.startswith("/list/"):
            prefix = self.path[len("/list/"):]
            if not prefix.startswith(READ):
                return self._send(403, {"error": "prefix not allowed"})
            keys = []
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
                keys += [o["Key"] for o in page.get("Contents", [])]
            return self._send(200, keys)
        if self.path.startswith("/o/"):
            key = self.path[3:]
            if not key.startswith(READ) or ".." in key:
                return self._send(403, {"error": "key not allowed"})
            t0 = time.time()
            try:
                body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            except s3.exceptions.NoSuchKey:
                return self._send(404, {"error": "no such key"})
            bump(key.split("/")[0], "get", len(body), time.time() - t0)
            return self._send(200, body, "application/octet-stream")
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        key = self.path[3:] if self.path.startswith("/o/") else ""
        if not key.startswith(WRITE) or ".." in key:
            return self._send(403, {"error": "key not allowed"})
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        t0 = time.time()
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)
        bump(key.split("/")[0], "put", len(body), time.time() - t0)
        self._send(200, {"key": key, "bytes": len(body)})


if __name__ == "__main__":
    print(f"trajectory store on s3://{BUCKET}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

"""The store service: the only thing in the cluster with access to the bucket.

    PUT  /o/<key>        write   runs/, fuzz/, reviews/, rescore/, evals/, gold/ (human gold labels)
    GET  /o/<key>        read    the above, and data/, wheels/, registry/
    GET  /list/<prefix>  keys under a readable prefix

The verifier registry (registry/) is readable but NOT writable from inside the cluster: only the operator's launcher
writes it, after the gate, so a training job cannot approve its own verifier.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

BUCKET = os.environ["BUCKET"]
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-2"))
WRITE = ("runs/", "fuzz/", "reviews/", "rescore/", "evals/", "gold/")
READ = WRITE + ("data/", "wheels/", "registry/")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
            try:
                body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            except s3.exceptions.NoSuchKey:
                return self._send(404, {"error": "no such key"})
            return self._send(200, body, "application/octet-stream")
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        key = self.path[3:] if self.path.startswith("/o/") else ""
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not key.startswith(WRITE) or ".." in key:
            return self._send(403, {"error": f"writes to {key.split('/')[0]}/ are not allowed from the cluster"})
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)
        self._send(200, {"key": key, "bytes": len(body)})


if __name__ == "__main__":
    print(f"store on s3://{BUCKET}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

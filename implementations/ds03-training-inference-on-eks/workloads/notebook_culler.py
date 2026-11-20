#!/usr/bin/env python3
"""Stops notebook servers that nobody is using (Design 03, "Idle culling").

For each notebook StatefulSet in the research namespace it asks the Jupyter server when it was last used. "Used" means
a kernel or terminal did something; the server's own activity time is not used, because the culler's polling would reset it
(measured: it read 15 seconds, the polling interval, every time). If the last use was longer ago than IDLE_SECONDS, and no
kernel is busy, it scales the StatefulSet to zero. The workspace volume is
kept, so the next start loses no files. Standard library only.
"""
import datetime as dt
import json
import os
import ssl
import time
import urllib.request

NS = os.environ.get("NAMESPACE", "research")
IDLE = int(os.environ.get("IDLE_SECONDS", "3600"))
EVERY = int(os.environ.get("POLL_SECONDS", "20"))
API = "https://kubernetes.default.svc"
TOKEN = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read()
CTX = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def kube(method, path, body=None, ctype="application/json"):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": ctype})
    with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def jupyter(name, token, path):
    url = f"http://{name}-0.{name}.{NS}.svc:8888{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


while True:
    try:
        for sts in kube("GET", f"/apis/apps/v1/namespaces/{NS}/statefulsets")["items"]:
            name = sts["metadata"]["name"]
            if sts["spec"].get("replicas", 0) == 0 or "ds03/notebook" not in (sts["metadata"].get("labels") or {}):
                continue
            token = sts["metadata"]["annotations"].get("ds03/token", "")
            try:
                status = jupyter(name, token, "/api/status")
                kernels = jupyter(name, token, "/api/kernels")
                terminals = jupyter(name, token, "/api/terminals")
            except Exception as exc:  # not ready yet
                print(json.dumps({"ev": "skip", "notebook": name, "why": str(exc)[:80]}), flush=True)
                continue
            busy = any(k.get("execution_state") == "busy" for k in kernels)
            last_use = max([parse(status["started"])] + [parse(x["last_activity"]) for x in kernels + terminals if x.get("last_activity")])
            idle_for = (dt.datetime.now(dt.timezone.utc) - last_use).total_seconds()
            print(json.dumps({"ev": "check", "notebook": name, "idle_seconds": round(idle_for), "limit": IDLE, "busy": busy}), flush=True)
            if idle_for > IDLE and not busy:
                kube("PATCH", f"/apis/apps/v1/namespaces/{NS}/statefulsets/{name}/scale",
                     {"spec": {"replicas": 0}}, "application/merge-patch+json")
                print(json.dumps({"ev": "culled", "notebook": name, "idle_seconds": round(idle_for)}), flush=True)
    except Exception as exc:
        print(json.dumps({"ev": "error", "why": str(exc)[:200]}), flush=True)
    time.sleep(EVERY)

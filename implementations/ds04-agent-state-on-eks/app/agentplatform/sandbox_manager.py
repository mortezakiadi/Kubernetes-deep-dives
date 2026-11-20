"""The sandbox manager (Design 04, "Working state"): one sandbox pod per run, started from the latest snapshot.

Design 01 covers how a sandbox is isolated. Here a sandbox is a pod that runs the small executor, because what Design 04 tests
is the LIFECYCLE: a sandbox can disappear at any time, and the next one must come back with the files the run had.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request

from .common import Handler, http, serve

NS = os.environ.get("SANDBOX_NAMESPACE", "sandboxes")
IMAGE = os.environ["AGENT_IMAGE"]
API = "https://kubernetes.default.svc"
TOKEN = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read()
CTX = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def kube(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def pod_name(session):
    return "sb-" + session.lower().replace("_", "-")[:50]


def pod_spec(session):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name(session), "namespace": NS, "labels": {"app": "sandbox", "session": session}},
            "spec": {"serviceAccountName": "sandbox", "restartPolicy": "Never", "terminationGracePeriodSeconds": 3,
                     "containers": [{"name": "executor", "image": IMAGE, "command": ["python", "-m", "agentplatform", "executor"],
                                     "env": [{"name": "BUCKET", "value": os.environ["BUCKET"]}, {"name": "AWS_REGION", "value": "us-east-1"}],
                                     "ports": [{"containerPort": 8081}],
                                     "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                                     "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8081}, "periodSeconds": 1}}]}}


def ready_ip(session):
    s, p = kube("GET", f"/api/v1/namespaces/{NS}/pods/{pod_name(session)}")
    if s != 200 or p["metadata"].get("deletionTimestamp"):
        return None, s
    ok = any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
    return (p["status"].get("podIP") if ok else None), s


def ensure(h, m, body):
    session, snap = body["session"], body.get("snapshot_id")
    ip, status = ready_ip(session)
    if ip:
        return 200, {"ip": ip, "created": False, "restored_files": 0}
    if status == 200:  # exists but not ready or terminating: replace it
        kube("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod_name(session)}?gracePeriodSeconds=0")
        for _ in range(30):
            if kube("GET", f"/api/v1/namespaces/{NS}/pods/{pod_name(session)}")[0] == 404:
                break
            time.sleep(1)
    s, r = kube("POST", f"/api/v1/namespaces/{NS}/pods", pod_spec(session))
    if s not in (200, 201, 409):
        return 502, {"error": f"could not create the sandbox: {s} {r.get('message', '')}"}
    t0 = time.time()
    while time.time() - t0 < 150:
        ip, _ = ready_ip(session)
        if ip:
            break
        time.sleep(1)
    else:
        return 504, {"error": "sandbox did not become ready in time"}
    restored = 0
    if snap:
        s, r = http("POST", f"http://{ip}:8081/restore", {"snapshot_id": snap}, timeout=120)
        if s != 200:
            return 502, {"error": f"restore failed: {r}"}
        restored = r["restored_files"]
    return 200, {"ip": ip, "created": True, "restored_files": restored, "seconds_to_ready": round(time.time() - t0, 1)}


def release(h, m, body):
    kube("DELETE", f"/api/v1/namespaces/{NS}/pods/{pod_name(m.group(1))}?gracePeriodSeconds=0")
    return 200, {"released": m.group(1)}


def listing(h, m, body):
    s, r = kube("GET", f"/api/v1/namespaces/{NS}/pods")
    return 200, {"sandboxes": [p["metadata"]["labels"].get("session") for p in r.get("items", [])]}


class H(Handler):
    routes = {
        ("POST", r"/sandboxes"): ensure,
        ("DELETE", r"/sandboxes/(.+)"): release,
        ("GET", r"/sandboxes"): listing,
        ("GET", r"/healthz"): lambda h, m, b: (200, {"ok": True}),
    }


def main():
    serve(H)

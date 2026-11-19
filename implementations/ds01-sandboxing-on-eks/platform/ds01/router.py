"""Sandbox Router (Design 01: "Sandbox Router").

Forwards the orchestrator's commands and files to the right sandbox by session
ID, so the orchestrator never tracks pod IPs. Every command and file transfer is
written to the audit log with its exit code and duration.

Two ways in, chosen by the template:
  * python-untrusted has a pod network, so commands go to sandboxd, the
    agent-sandbox runtime daemon, with a bearer token.
  * compute-only has no network device at all (Tier 3), so sandboxd cannot be
    reached. Commands go through the Kubernetes exec channel, which the sandbox
    keeps even with no network.
"""
import base64
import re
import time

from kubernetes import client as kclient
from kubernetes import config as kconfig
from kubernetes.stream import stream

from ds01.common import K8s, Handler, audit, read_secret, secrets_equal, serve
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxdInClusterConnectionConfig

NS = "sandboxes"
CLAIMS = f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{NS}/sandboxclaims"
MAX_FILE = 5 * 1024 * 1024

orchestrator_token = read_secret("/secrets/orchestrator-token")
sandboxd_token = read_secret("/secrets/sandboxd-token")

k8s = K8s()
sdk = SandboxClient(connection_config=SandboxdInClusterConnectionConfig(mode="in-cluster-pod-ip", auth_token=sandboxd_token))
kconfig.load_incluster_config()
core = kclient.CoreV1Api()


def _resolve(sid):
    code, claim = k8s.request("GET", f"{CLAIMS}/{sid}")
    if code != 200:
        return None
    template = claim["metadata"].get("labels", {}).get("ds01/template")
    pod = (claim.get("status", {}).get("sandbox") or {}).get("name")
    return {"template": template, "pod": pod, "tenant": claim["metadata"]["labels"].get("ds01/tenant")}


def _kube_exec(pod, command, timeout):
    resp = stream(core.connect_get_namespaced_pod_exec, pod, NS, command=["sh", "-c", command],
                  stderr=True, stdin=False, stdout=True, tty=False, _preload_content=False)
    out, err, deadline = [], [], time.time() + timeout
    while resp.is_open() and time.time() < deadline:
        resp.update(timeout=1)
        if resp.peek_stdout():
            out.append(resp.read_stdout())
        if resp.peek_stderr():
            err.append(resp.read_stderr())
    timed_out = resp.is_open()
    code = resp.returncode if not timed_out else 124
    resp.close()
    return {"stdout": "".join(out), "stderr": "".join(err), "exit_code": code if code is not None else -1, "timed_out": timed_out}


class Router(Handler):
    component = "sandbox-router"

    def route(self, method, path, query, body):
        if path == "/healthz":
            return 200, {"ok": True}
        if not secrets_equal(self.bearer(), orchestrator_token):
            return 401, {"error": "unauthorized"}
        m = re.fullmatch(r"/v1/sandboxes/(sess-[a-f0-9]+)/(exec|files)", path)
        if not m:
            return 404, {"error": "not found"}
        sid, action = m.groups()
        target = _resolve(sid)
        if not target or not target["pod"]:
            return 404, {"error": "no such sandbox"}
        if action == "exec" and method == "POST":
            return self._exec(sid, target, body)
        if action == "files" and method in ("PUT", "GET"):
            return self._files(sid, target, method, (query.get("path") or [""])[0], body)
        return 405, {"error": "method not allowed"}

    def _exec(self, sid, target, body):
        command, timeout = str(body.get("command", "")), int(body.get("timeout", 60))
        if not command:
            return 400, {"error": "command required"}
        t0 = time.time()
        backend = "sandboxd" if target["template"] == "python-untrusted" else "kube-exec"
        if backend == "sandboxd":
            res = sdk.get_sandbox(sid, namespace=NS).commands.run(command, command_timeout=timeout)
            result = {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code, "timed_out": res.timed_out}
        else:
            result = _kube_exec(target["pod"], command, timeout)
        audit(self.component, "exec", session_id=sid, tenant=target["tenant"], backend=backend, command=command[:300],
              exit_code=result["exit_code"], duration_ms=int((time.time() - t0) * 1000), stdout_len=len(result["stdout"]))
        return 200, result

    def _files(self, sid, target, method, path, body):
        if not path or ".." in path.split("/"):
            return 400, {"error": "a relative path under the workspace is required"}
        path = path.lstrip("/")
        t0 = time.time()
        backend = "sandboxd" if target["template"] == "python-untrusted" else "kube-exec"
        if method == "PUT":
            data = body if isinstance(body, (bytes, bytearray)) else str(body).encode()
            if len(data) > MAX_FILE:
                return 413, {"error": "file too large"}
            if backend == "sandboxd":
                sdk.get_sandbox(sid, namespace=NS).files.write(path, bytes(data))
            else:
                b64 = base64.b64encode(data).decode()
                res = _kube_exec(target["pod"], f"mkdir -p \"$(dirname '/workspace/{path}')\" && printf %s '{b64}' | base64 -d > '/workspace/{path}'", 60)
                if res["exit_code"] != 0:
                    return 500, {"error": "write failed"}
            audit(self.component, "file.upload", session_id=sid, tenant=target["tenant"], backend=backend, path=path,
                  bytes=len(data), duration_ms=int((time.time() - t0) * 1000))
            return 200, {"written": path, "bytes": len(data)}
        if backend == "sandboxd":
            data = sdk.get_sandbox(sid, namespace=NS).files.read(path)
        else:
            res = _kube_exec(target["pod"], f"base64 '/workspace/{path}'", 60)
            if res["exit_code"] != 0:
                return 404, {"error": "no such file"}
            data = base64.b64decode(res["stdout"])
        audit(self.component, "file.download", session_id=sid, tenant=target["tenant"], backend=backend, path=path,
              bytes=len(data), duration_ms=int((time.time() - t0) * 1000))
        return 200, {"path": path, "bytes": len(data), "content_b64": base64.b64encode(data).decode()}


if __name__ == "__main__":
    serve(Router, 8004)

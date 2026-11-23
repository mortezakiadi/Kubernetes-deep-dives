"""Small shared helpers, standard library only: HTTP calls to the in-cluster services and the trajectory store client.

Environment pods have no AWS identity and no route to the internet. Everything they record goes through the
trajectory store service, and everything they are scored on comes from the verifier service.
"""
import json
import os
import time
import urllib.error
import urllib.request

STORE = os.environ.get("TRAJSTORE_URL", "http://trajstore.platform.svc.cluster.local:8080")
VERIFIER = os.environ.get("VERIFIER_URL", "http://verifier.platform.svc.cluster.local:8080")
LLM = os.environ.get("LLM_URL", "http://vllm.llm.svc.cluster.local:8000")


def http(method, url, body=None, headers=None, timeout=300, retries=4):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if data is not None and not isinstance(body, bytes) else {}
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                raise RuntimeError(f"{method} {url}: {e.code} {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(2 * (attempt + 1))


def jhttp(method, url, body=None, **kw):
    return json.loads(http(method, url, body, **kw) or b"null")


# ---- trajectory store -------------------------------------------------------------------------------------------
def put(key, value):
    body = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    http("PUT", f"{STORE}/o/{key}", body, headers={"Content-Type": "application/octet-stream"})


def get(key, raw=False):
    b = http("GET", f"{STORE}/o/{key}")
    return b if raw else json.loads(b)


def listing(prefix):
    return jhttp("GET", f"{STORE}/list/{prefix}")


def task(task_id):
    return get(f"tasks/public/{task_id}.json")


def verify(body):
    return jhttp("POST", f"{VERIFIER}/verify", body, timeout=600)


def progress(body):
    return jhttp("POST", f"{VERIFIER}/progress", body, timeout=120)

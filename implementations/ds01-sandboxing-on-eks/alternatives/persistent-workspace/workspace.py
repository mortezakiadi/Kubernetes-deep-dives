"""Pause, resume and checkpoint for a sandbox whose workspace lives on a persistent volume.

What EKS gives and does not give, from the AWS and agent-sandbox documentation:
  * A Nitro Sandbox can mount an EBS volume (one pod at a time, access mode ReadWriteOncePod). The disk survives the pod.
  * A Nitro Sandbox cannot be snapshotted, restored or live-migrated, so the memory of a running process cannot be saved.
  * The agent-sandbox Sandbox has an operatingMode. Suspended deletes only the pod. The volume and the Service stay. Running makes a new
    pod and reattaches the same volume.
So "pause" here means: keep the disk, drop the machine. A resumed sandbox starts its processes again, from the files it left behind.
A checkpoint is an EBS snapshot of the workspace volume.

Every function takes the K8s helper as an argument (the one in platform/ds01/common.py), so they can be tested without a cluster.
"""
import time

NS = "sandboxes"
CLAIMS = f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{NS}/sandboxclaims"
SANDBOXES = f"/apis/agents.x-k8s.io/v1beta1/namespaces/{NS}/sandboxes"
SNAPSHOTS = f"/apis/snapshot.storage.k8s.io/v1/namespaces/{NS}/volumesnapshots"
VOLUME_NAME = "workspace"  # the name in the template's volumeClaimTemplates; the PVC is named <volume name>-<sandbox name>


def sandbox_name(k8s, session_id):
    code, claim = k8s.request("GET", f"{CLAIMS}/{session_id}")
    if code != 200:
        raise LookupError("no such session")
    name = (claim.get("status", {}).get("sandbox") or {}).get("name")
    if not name:
        raise LookupError("the session has no sandbox yet")
    return name


def _set_mode(k8s, name, mode):
    code, resp = k8s.request("PATCH", f"{SANDBOXES}/{name}", {"spec": {"operatingMode": mode}}, content_type="application/merge-patch+json")
    if code >= 300:
        raise RuntimeError(f"could not set operatingMode {mode}: {code} {resp.get('message', '')}")


def suspend(k8s, session_id):
    """Delete the pod, keep the volume. Returns the sandbox name."""
    name = sandbox_name(k8s, session_id)
    _set_mode(k8s, name, "Suspended")
    return name


def resume(k8s, session_id, timeout=180, poll=2.0, sleep=time.sleep):
    """Recreate the pod on the same volume and wait until the sandbox is Ready."""
    name = sandbox_name(k8s, session_id)
    _set_mode(k8s, name, "Running")
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, sb = k8s.request("GET", f"{SANDBOXES}/{name}")
        for cond in (sb.get("status", {}).get("conditions") or []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                return name
        sleep(poll)
    raise TimeoutError("the sandbox did not become Ready in time")


def checkpoint(k8s, session_id, label, snapshot_class="ds01-workspace"):
    """Snapshot the workspace volume. Returns the snapshot name."""
    name = sandbox_name(k8s, session_id)
    snap = f"{session_id}-{label}"
    manifest = {
        "apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
        "metadata": {"name": snap, "namespace": NS, "labels": {"ds01/session": session_id}},
        "spec": {"volumeSnapshotClassName": snapshot_class, "source": {"persistentVolumeClaimName": f"{VOLUME_NAME}-{name}"}},
    }
    code, resp = k8s.request("POST", SNAPSHOTS, manifest)
    if code >= 300:
        raise RuntimeError(f"could not create the snapshot: {code} {resp.get('message', '')}")
    return snap

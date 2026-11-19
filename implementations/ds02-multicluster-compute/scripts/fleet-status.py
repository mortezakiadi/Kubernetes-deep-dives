#!/usr/bin/env python3
"""Why is my job pending? One answer per layer (Design 02: "Four layers of scheduling decisions").

    fleet-status.py queues             # every ClusterQueue in the fleet: pending, admitted, usage, borrowed
    fleet-status.py workloads [ns]     # every Workload, and which layer is holding it

Layer 1 is the hub's Kueue (does the team have fleet-wide quota, and which worker was it sent to).
Layer 2 is the worker's Kueue (does the worker's queue have quota, can it borrow).
Layer 3 and 4 (kube-scheduler and the node autoscaler) show as the pod and node state.
"""
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUBECTL = str(ROOT / ".tools" / "kubectl")
ENV = {**os.environ, "KUBECONFIG": str(ROOT / ".kube" / "config")}
CLUSTERS = ["hub", "cpu-a", "cpu-b", "gpu"]


def kget(ctx, *args):
    proc = subprocess.run([KUBECTL, "--context", ctx, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(proc.stdout).get("items", [])
    except json.JSONDecodeError:
        return []


def cond(obj, kind):
    for c in obj.get("status", {}).get("conditions", []):
        if c["type"] == kind:
            return c
    return {}


def layer(ctx, wl):
    """One line: which layer is this workload waiting at, and why."""
    st = wl.get("status", {})
    if cond(wl, "Finished").get("status") == "True":
        return "finished"
    if cond(wl, "Admitted").get("status") == "True":
        where = f" in {st['clusterName']}" if st.get("clusterName") else ""
        return f"running (admitted{where})"
    qr = cond(wl, "QuotaReserved")
    if ctx == "hub":
        if qr.get("status") != "True":
            return "LAYER 1 pending: no fleet quota. " + (qr.get("message") or "")[:110]
        checks = st.get("admissionChecks", [])
        if checks:
            return "LAYER 1 pending: quota reserved at the hub, waiting for a worker to admit. " + (checks[0].get("message") or "")[:90]
    if qr.get("status") != "True":
        return "LAYER 2 pending: " + (qr.get("message") or "no quota")[:120]
    return "admitted"


def queues():
    print(f"{'CLUSTER':8} {'QUEUE':14} {'PENDING':>7} {'ADMITTED':>8}  USAGE (flavor: resource used / nominal, borrowed)")
    for ctx in CLUSTERS:
        for cq in kget(ctx, "get", "clusterqueues"):
            st = cq.get("status", {})
            spec_groups = cq["spec"]["resourceGroups"]
            nominal = {(f["name"], r["name"]): r["nominalQuota"] for g in spec_groups for f in g["flavors"] for r in f["resources"]}
            parts = []
            for fu in st.get("flavorsUsage", []):
                for r in fu["resources"]:
                    if r["name"] in ("cpu", "nvidia.com/gpu") and (str(r.get("total", "0")) != "0" or True):
                        parts.append(f"{fu['name']}: {r['name'].split('/')[-1]} {r.get('total', 0)}/{nominal.get((fu['name'], r['name']), '?')} (borrowed {r.get('borrowed', 0)})")
            print(f"{ctx:8} {cq['metadata']['name']:14} {st.get('pendingWorkloads', 0):>7} {st.get('admittedWorkloads', 0):>8}  {'; '.join(parts)}")


def workloads(ns=None):
    print(f"{'CLUSTER':8} {'NAMESPACE':9} {'WORKLOAD':34} STATE")
    for ctx in CLUSTERS:
        args = ["get", "workloads"] + (["-n", ns] if ns else ["-A"])
        for wl in kget(ctx, *args):
            print(f"{ctx:8} {wl['metadata']['namespace']:9} {wl['metadata']['name'][:34]:34} {layer(ctx, wl)}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "queues"
    queues() if what == "queues" else workloads(sys.argv[2] if len(sys.argv) > 2 else None)

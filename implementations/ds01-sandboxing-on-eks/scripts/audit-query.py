#!/usr/bin/env python3
"""One query for an audit (Design 01: "Observability and audit").

    audit-query.py <session-id> [--json]

Collects the JSON audit lines of every platform component from the cluster, keeps the ones that carry the
session ID, and prints them as one timeline: who created the sandbox, what ran, what it reached, which
credentials were issued (by ID, never by value), and which tools were called.
"""
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUBECTL = str(ROOT / ".tools" / "kubectl")
ENV = {**os.environ, "KUBECONFIG": str(ROOT / ".kube" / "config")}

COMPONENTS = [
    ("sandbox-platform", "sandbox-manager"),
    ("sandbox-router", "sandbox-router"),
    ("sandbox-egress", "egress-gateway"),
    ("sandbox-egress", "credential-broker"),
    ("sandbox-platform", "tool-gateway"),
    ("sandbox-platform", "metrics-api"),
]


def collect(session_id):
    records = []
    for namespace, component in COMPONENTS:
        proc = subprocess.run(
            [KUBECTL, "-n", namespace, "logs", "-l", f"ds01/component={component}", "--tail=-1", "--prefix=false", "--ignore-errors"],
            capture_output=True, text=True, env=ENV)
        for line in proc.stdout.splitlines():
            if session_id not in line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return sorted(records, key=lambda r: r["ts"])


def main():
    session_id = sys.argv[1]
    records = collect(session_id)
    if "--json" in sys.argv:
        print(json.dumps(records))
        return
    skip = {"ts", "component", "event", "session_id", "tenant", "source"}
    for r in records:
        detail = " ".join(f"{k}={v}" for k, v in r.items() if k not in skip)
        print(f"{r['ts'][11:23]}  {r['component']:<18} {r['event']:<20} {detail[:150]}")
    print(f"-- {len(records)} records from {len({r['component'] for r in records})} components for {session_id}")


if __name__ == "__main__":
    main()

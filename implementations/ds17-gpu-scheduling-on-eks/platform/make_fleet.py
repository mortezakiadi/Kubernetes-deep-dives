#!/usr/bin/env python3
"""Writes the simulated GPU fleet: 16 KWOK nodes, each with 8 fake nvidia.com/gpu, in 2 blocks x 2 racks x 4 hosts.

    make_fleet.py > fleet.yaml        (up.sh applies it after the platform layer)

The nodes are Kubernetes Node objects that no machine stands behind. KWOK keeps them Ready and moves pods bound to them
through their lifecycle; Kueue and kube-scheduler see them exactly like real nodes. Every one is tainted so that nothing
lands there unless it tolerates the taint (Kueue adds the toleration from the fake-h100 ResourceFlavor).
"""
import json

BLOCKS, RACKS, HOSTS, GPUS = 2, 2, 4, 8


def node(b, r, h):
    name = f"fake-b{b}-r{r}-h{h}"
    return {
        "apiVersion": "v1", "kind": "Node",
        "metadata": {"name": name, "annotations": {"kwok.x-k8s.io/node": "fake", "node.alpha.kubernetes.io/ttl": "0"},
                     "labels": {"kubernetes.io/hostname": name, "kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64", "type": "kwok",
                                "ds17/pool": "fake-gpu", "network.ds17.io/block": f"b{b}", "network.ds17.io/rack": f"b{b}-r{r}"}},
        "spec": {"taints": [{"key": "kwok.x-k8s.io/node", "value": "fake", "effect": "NoSchedule"}]},
        "status": {"allocatable": {"cpu": "96", "memory": "1Ti", "pods": "110", "nvidia.com/gpu": str(GPUS)},
                   "capacity": {"cpu": "96", "memory": "1Ti", "pods": "110", "nvidia.com/gpu": str(GPUS)}},
    }


if __name__ == "__main__":
    items = [node(b, r, h) for b in range(1, BLOCKS + 1) for r in range(1, RACKS + 1) for h in range(1, HOSTS + 1)]
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=1))

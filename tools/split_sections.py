"""Split each EKS-notes page into sections (one new draw.io page per section).

Usage: python3 sections.py EKS-notes.drawio EKS-deep-dive-sections.drawio
Each section is one or more rectangles in the original page's coordinates; a cell is
included when its center falls inside a rectangle. Edges are kept when both ends are kept.
"""
import sys, copy, json
import xml.etree.ElementTree as ET

W = 99999
SECTIONS = {
 "EKS Scheduler ": [
   ("d01-1", "Phase 1: filtering", [(0, 100, 2000, 760), (2000, 100, 2450, 250)]),
   ("d01-2", "Filtering outcomes", [(2000, 250, 2750, 960)]),
   ("d01-3", "Phase 2: scoring plugins", [(600, 760, 2800, 1350)]),
   ("d01-4", "Score x weight", [(1110, 1350, 2120, 2060)]),
   ("d01-5", "When affinity loses to ImageLocality", [(2120, 1370, 3250, 1960)]),
   ("d01-6", "A worked example", [(1550, 1960, 3100, 2420)]),
   ("d01-7", "The weight field in your YAML", [(1500, 2420, 3700, 2800)]),
   ("d01-8", "Scheduler score vs. OOM score", [(550, 1500, 1110, 2060)]),
 ],
 "EKS-MultiScheduler": [
   ("d02-1", "How a Pod finds its scheduler", [(-1000, 0, 3200, 1430)]),
   ("d02-2", "Gang scheduling", [(-1000, 1430, 3200, 2080)]),
   ("d02-3", "Topology bin-packing", [(-1000, 2080, 3200, 2545)]),
   ("d02-4", "Queueing and fair sharing", [(-1000, 2545, 3200, 2935)]),
   ("d02-5", "Hardware-aware scheduling", [(-1000, 2935, 3200, 3500)]),
   ("d02-6", "Volcano vs. Kueue", [(-1000, 3500, 3200, 4960)]),
   ("d02-7", "The landscape", [(-1000, 4960, 3200, 6050)]),
   ("d02-8", "What every scheduler relies on", [(-1000, 6050, 3200, 6900)]),
 ],
 "EKS-Batch and Realtime": [
   ("d03-1", "Workloads and node tiers", [(0, 0, 1400, 860)]),
   ("d03-2", "Priority, preemption and QoS", [(1400, 0, 2330, 925)]),
   ("d03-3", "Placement, autoscaling and cost", [(2330, 0, 4900, 1160)]),
   ("d03-4", "CPU: shares and throttling", [(0, 860, 4900, 2830)]),
   ("d03-5", "Kernel vs. Kubernetes", [(0, 2830, 4900, 3690)]),
   ("d03-6", "Memory: who gets killed", [(0, 3690, 4900, 4630)]),
   ("d03-7", "Network: the unmanaged resource", [(0, 4630, 4900, 6040)]),
   ("d03-8", "Storage I/O: the other unmanaged resource", [(0, 6040, 4900, 8200)]),
 ],
 "cgroups": [
   ("d04-1", "Policy maker, translator, enforcer", [(0, 3300, 2800, 3800)]),
   ("d04-2", "From a limit to a quota", [(0, 3800, 2800, 4300)]),
 ],
 "Challenges-1": [
   ("d05-1", "Volume attachment limit", [(0, 0, 3500, 365)]),
   ("d05-2", "The stuck volume", [(0, 365, 3500, 810)]),
   ("d05-3", "Snapshot restored in the wrong AZ", [(0, 810, 3500, 1160)]),
   ("d05-4", "Force delete and filesystem corruption", [(0, 1160, 3500, 1480)]),
   ("d05-5", "IOPS throttling cascade", [(0, 1480, 3500, 1810)]),
   ("d05-6", "Cross-AZ transfer costs", [(0, 1810, 3500, 2090)]),
   ("d05-7", "The page-cache memory trap", [(0, 2090, 3500, 3210)]),
   ("d05-8", "Thundering herd", [(0, 3210, 3500, 3710)]),
   ("d05-9", "Scheduler thrashing", [(0, 3710, 3500, 4460)]),
   ("d05-10", "CPU limit vs. request gap", [(0, 4460, 3500, 4900)]),
 ],
 "EKS and GPU": [("d06-1", "The NVIDIA device plugin", [(0, 0, W, W)])],
 "EKS-Multi-tenancy": [
   ("d07-1", "Tenant classes, soft and hard isolation", [(-500, 0, 1725, 900)]),
   ("d07-2", "Identity, data, network and observability", [(1725, 0, 4200, 900)]),
   ("d07-3", "Sandboxed runtimes: gVisor and Kata", [(-500, 1100, 2350, 2320)]),
   ("d07-4", "Hardening runC: seccomp, AppArmor, user namespaces", [(2350, 1100, 4200, 2450)]),
   ("d07-5", "Control plane security", [(-500, 2320, 2350, 3190)]),
   ("d07-6", "Network isolation and egress", [(-500, 3190, 2500, 4500)]),
 ],
 "EKS-Stateful workload (DBs)": [
   ("d08-1", "Workloads, node groups and scheduling", [(0, 0, 1400, 1450)]),
   ("d08-2", "Storage and StatefulSet mechanics", [(1400, 0, 2450, 1450)]),
   ("d08-3", "Performance, networking and backup", [(2450, 0, 4100, 1450)]),
   ("d08-4", "Spreading across AZs and nodes", [(0, 1450, 4100, 2210)]),
   ("d08-5", "NUMA and CPU pinning", [(0, 2210, 4100, 2840)]),
   ("d08-6", "I/O scheduler tuning", [(0, 2840, 4100, 3240)]),
   ("d08-7", "EBS is AZ-locked: three strategies", [(0, 3240, 4100, 4260)]),
   ("d08-8", "How EBS really behaves", [(0, 4260, 4100, 4900)]),
   ("d08-9", "File system, mount options and O_DIRECT", [(0, 4900, 4100, 5590)]),
   ("d08-10", "The EBS CSI driver", [(0, 5590, 4100, 6130)]),
   ("d08-11", "The PV subsystem end to end", [(0, 6130, 4100, 7000)]),
 ],
 "EKS Pod Networking": [
   ("d09-1", "VPC CNI architecture and ENI limits", [(0, 0, 1400, 730)]),
   ("d09-2", "Warm pools and prefix delegation", [(0, 730, 3000, 1490)]),
   ("d09-3", "Packet paths", [(0, 1490, 2000, 1950)]),
   ("d09-4", "kube-proxy: iptables vs. IPVS", [(0, 1950, 2700, 2170)]),
   ("d09-4b", "conntrack: the connection ledger", [(2700, 1850, 5400, 2620)]),
   ("d09-5", "Security groups for pods and network policies", [(0, 2170, 2700, 2850)]),
   ("d09-6", "The packet's journey through netfilter", [(0, 3050, 3700, 3830)]),
   ("d09-7", "From NetworkPolicy YAML to kernel rules", [(0, 3830, 3700, 4400)]),
   ("d09-8", "Calico vs. Cilium", [(0, 4400, 3700, 5300)]),
 ],
 "EKS Networking": [
   ("d10-1", "What runs on a worker node", [(0, 0, 750, 560)]),
   ("d10-2", "Network policy limits", [(0, 560, 750, 790)]),
   ("d10-3", "Network optimization levers", [(0, 790, 1520, 1260)]),
   ("d10-4", "Standard vs. SR-IOV vs. RDMA", [(1520, 790, 2350, 1600)]),
   ("d10-5", "Latency and EFA", [(0, 1260, 1520, 1600)]),
 ],
 "Challenges-2": [
   ("d11-1", "IP exhaustion", [(0, 2150, 3200, 2610)]),
   ("d11-2", "Service connectivity drops", [(0, 2610, 3200, 3215)]),
   ("d11-3", "DNS issues", [(0, 3215, 3200, 4335)]),
   ("d11-4", "Latency by distance", [(0, 4335, 3200, 5315)]),
   ("d11-5", "Bandwidth and MTU", [(0, 5315, 3200, 6000)]),
 ],
 "EKS Optmization": [
   ("d12-1", "Compute", [(0, 0, 1550, 900)]),
   ("d12-2", "Networking", [(0, 900, 1550, 1400)]),
   ("d12-3", "Storage", [(0, 1400, 1550, 2100)]),
 ],
 "EKS Security": [
   ("d13-1", "What we protect", [(0, 0, 1600, 560)]),
   ("d13-2", "The build pipeline", [(0, 560, 1600, 960)]),
   ("d13-3", "Runtime security", [(0, 960, 1600, 1520)]),
   ("d13-4", "Network security", [(0, 1520, 1600, 2050)]),
   ("d13-5", "Secret management", [(0, 2050, 1600, 2580)]),
   ("d13-6", "Identity and model endpoints", [(0, 2580, 1600, 3100)]),
 ],
 "StorageClass-pv.pvc": [("d14-1", "StorageClass, PV and PVC", [(0, 0, W, W)])],
}
EXCLUDE = {"s3L59IFOvDij3QFSKCTg-60", "s3L59IFOvDij3QFSKCTg-73"}


def cell_of(o):
    return o if o.tag == "mxCell" else o.find("mxCell")


def main(src, dst):
    root = ET.parse(src).getroot()
    pages = {d.get("name"): d for d in root.findall("diagram")}
    out = ET.Element("mxfile", host="app.diagrams.net")
    manifest = []
    for pname, secs in SECTIONS.items():
        r = pages[pname].find("mxGraphModel").find("root")
        elems = list(r)
        info = {}
        for o in elems:
            c = cell_of(o)
            if c is None: continue
            info[o.get("id")] = (o, c)
        children = {}
        for i, (o, c) in info.items():
            children.setdefault(c.get("parent"), []).append(i)
        for sid, title, rects in secs:
            keep = set()
            def inside(x, y):
                return any(a <= x <= b and c_ <= y <= d for a, c_, b, d in rects)
            for i, (o, c) in info.items():
                if i in EXCLUDE or c.get("parent") != "1" or c.get("vertex") != "1": continue
                g = c.find("mxGeometry")
                if g is None: continue
                x = float(g.get("x", 0)) + float(g.get("width", 0)) / 2
                y = float(g.get("y", 0)) + float(g.get("height", 0)) / 2
                if inside(x, y): keep.add(i)
            # descendants (children of groups / containers, edge labels)
            stack = list(keep)
            while stack:
                p = stack.pop()
                for ch in children.get(p, []):
                    if ch not in keep and ch not in EXCLUDE:
                        keep.add(ch); stack.append(ch)
            # edges
            for i, (o, c) in info.items():
                if c.get("edge") != "1" or i in keep: continue
                s, t = c.get("source"), c.get("target")
                if s and t:
                    ok = s in keep and t in keep
                else:
                    pts = []
                    g = c.find("mxGeometry")
                    if g is not None:
                        for p in g.findall("mxPoint"):
                            if p.get("as") in ("sourcePoint", "targetPoint"):
                                pts.append((float(p.get("x", 0)), float(p.get("y", 0))))
                    ends = [e for e in (s, t) if e]
                    ok = all(e in keep for e in ends) and pts and all(inside(*p) for p in pts)
                if ok:
                    keep.add(i)
                    stack = [i]
                    while stack:
                        p = stack.pop()
                        for ch in children.get(p, []):
                            if ch not in keep: keep.add(ch); stack.append(ch)
            d = ET.SubElement(out, "diagram", id=sid, name=f"{sid} {title}")
            m = copy.deepcopy(pages[pname].find("mxGraphModel"))
            nr = m.find("root")
            for o in list(nr):
                i = o.get("id")
                if i in ("0", "1"): continue
                if i not in keep: nr.remove(o)
            d.append(m)
            manifest.append({"id": sid, "title": title, "page": pname.strip(), "cells": len(keep)})
    ET.ElementTree(out).write(dst, encoding="utf-8")
    json.dump(manifest, open("manifest.json", "w"), indent=1)
    for m in manifest: print(m)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

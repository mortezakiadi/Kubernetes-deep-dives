#!/usr/bin/env python3
"""Generates the diagrams of the Design 17 write-up as SVG.   make_diagrams.py <output-dir>"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from svg_base import AMBER, BLUE, GREEN, GREY, PURPLE, RED, Svg  # noqa: E402


def context():
    s = Svg(1420, 520, "Who uses the platform, what it is made of, and what it talks to")
    s.zone(20, 30, 270, 460, "WHO", GREEN)
    s.box(40, 70, 230, 110, "Teams", ["submit Jobs labelled with", "a LocalQueue; read queue", "status and their share"], GREEN)
    s.box(40, 200, 230, 110, "Platform owner", ["sets quotas, weights,", "priorities, topology and", "scheduler profiles"], GREEN)
    s.box(40, 330, 230, 130, "Finance and capacity", ["read the GPU-hour", "breakdown: where every", "GPU-hour went, and why"], GREEN)
    s.zone(320, 30, 1060, 460, "THE PLATFORM (standard EKS)", AMBER)
    s.box(340, 70, 320, 120, "Admission: Kueue 0.20.8", ["ClusterQueues in cohorts, flavors,", "topology-aware admission,", "fair sharing, waitForPodsReady"], BLUE)
    s.box(690, 70, 320, 120, "Placement: second kube-scheduler", ["profiles gpu-binpack (MostAllocated)", "and gpu-spread (LeastAllocated),", "GPUs weighted 10 to 1"], PURPLE)
    s.box(1040, 70, 320, 120, "Accounting service", ["every 10 s, every GPU in one", "category; DCGM for real GPUs;", "lost work on preemption"], AMBER)
    s.box(340, 230, 670, 120, "Simulated fleet: 16 KWOK nodes x 8 GPUs = 128", ["2 blocks x 2 racks x 4 hosts, labelled for the topology; Node objects with no machine", "behind them; KWOK moves bound pods through ready, complete and terminate"], GREY)
    s.box(1040, 230, 320, 120, "Real GPUs: 2 nodes", ["one GPU each, device plugin,", "DCGM exporter; S02 only"], RED)
    s.box(340, 380, 1020, 80, "Kubernetes API server (EKS control plane)", ["every component above reads and writes Jobs, Pods, Nodes and Kueue objects through it"], GREY)
    s.arrow([(270, 125), (340, 125)], "flow", "Jobs", 305, 118)
    s.arrow([(660, 130), (690, 130)], "flow")
    s.arrow([(850, 190), (850, 230)], "flow", "bind pods", 900, 214)
    s.arrow([(1200, 230), (1200, 190)], "muted")
    s.arrow([(270, 395), (340, 395)], "muted")
    return s.render()


def topology():
    s = Svg(1420, 560, "One VPC in us-east-2: a system node group, a small real-GPU node group, and a simulated fleet")
    s.zone(20, 20, 1380, 520, "AWS ACCOUNT, us-east-2, ONE VPC 10.86.0.0/16 (three zones, private subnets, one NAT gateway)", GREY)
    s.box(50, 70, 380, 120, "Standard EKS cluster ds17-sched", ["Kubernetes 1.37, managed node groups", "add-ons: VPC CNI, kube-proxy, Pod Identity,", "CoreDNS, metrics-server"], BLUE)
    s.box(50, 210, 380, 140, "Node group ds17-sched-system", ["2 x m6i.2xlarge, on demand", "Kueue, the second scheduler, KWOK,", "the accounting service, CoreDNS"], GREEN)
    s.box(50, 370, 380, 140, "Node group ds17-sched-gpu", ["2 nodes, one GPU each, on demand", "pinned AL2023 NVIDIA AMI", "device plugin, DCGM exporter"], AMBER)
    for b in (1, 2):
        x = 470 + (b - 1) * 460
        s.zone(x, 70, 440, 440, f"block b{b} (simulated)", PURPLE)
        for r in (1, 2):
            y = 110 + (r - 1) * 200
            s.box(x + 20, y, 400, 180, f"rack b{b}-r{r}", [f"fake-b{b}-r{r}-h1 ... h4", "4 nodes x 8 nvidia.com/gpu", "taint kwok.x-k8s.io/node=fake",
                                                           "labels network.ds17.io/block, rack"], GREY)
    return s.render()


def layers():
    s = Svg(1420, 470, "Two layers: when a job may start, and where its pods go")
    steps = [("1 Submit", ["a Job with a queue label;", "created suspended"], GREEN), ("2 Workload", ["Kueue turns it into a", "Workload in a LocalQueue"], BLUE),
             ("3 Admit", ["whole request fits quota", "(and topology, with TAS)"], BLUE), ("4 Unsuspend", ["pods are created with", "the flavor's selectors"], BLUE),
             ("5 Place", ["the second scheduler scores", "nodes: pack or spread"], PURPLE), ("6 Ready", ["all pods ready within", "60 s, or evict and requeue"], RED)]
    for i, (t, lines, c) in enumerate(steps):
        x = 30 + i * 230
        s.box(x, 70, 210, 110, t, lines, c)
        if i < len(steps) - 1:
            s.arrow([(x + 210, 125), (x + 230, 125)], "flow")
    s.box(30, 230, 670, 200, "Admission answers 'may it start?'", ["quota per ClusterQueue and flavor, borrowing and lending in a cohort,", "priorities and preemption, fair sharing between queues,",
                                                                 "admission fair sharing between LocalQueues, a runtime limit;", "status: QuotaReserved / Admitted / Evicted with a reason"], BLUE)
    s.box(730, 230, 660, 200, "Placement answers 'which node?'", ["kube-scheduler filters and scores nodes for each pod;", "MostAllocated keeps whole nodes free, LeastAllocated spreads;",
                                                                 "with a topology flavor Kueue has already chosen the domain;", "status: pod Pending with the scheduler's reason"], PURPLE)
    return s.render()


def fragmentation():
    s = Svg(1420, 420, "The same 64 GPUs of small jobs, spread and packed (S06)")
    def fleet(x0, title, used, c):
        s.zone(x0, 30, 660, 360, title, c)
        for i, u in enumerate(used):
            x, y = x0 + 25 + (i % 8) * 78, 80 + (i // 8) * 150
            s.box(x, y, 68, 130, f"n{i + 1}", [], GREY)
            for g in range(8):
                fill = "#7c3aed" if g < u else "#ffffff"
                s.parts.append(f'<rect x="{x + 10}" y="{y + 36 + g * 11}" width="48" height="8" rx="2" fill="{fill}" stroke="#94a3b8"/>')
    fleet(20, "SPREAD (LeastAllocated): no node whole, the 4 x 8 gang cannot start", [3, 4, 6, 6, 3, 3, 3, 3, 5, 3, 6, 4, 4, 4, 4, 3], RED)
    fleet(740, "PACKED (MostAllocated): 8 whole nodes, the gang starts", [8, 8, 8, 0, 8, 8, 8, 8, 8, 0, 0, 0, 0, 0, 0, 0], GREEN)
    s.text(30, 410, "Purple: a GPU held by a small job. The layout is the one recorded in the dev run; the final run's numbers are in S06.", 12)
    return s.render()


def quotas():
    s = Svg(1420, 430, "Quotas in a cohort: nominal, borrowing, lending, reclaim (S08)")
    s.zone(20, 30, 1380, 260, "COHORT fleet", AMBER)
    s.box(40, 70, 420, 190, "ClusterQueue cq-llm", ["nominal 32 GPUs, borrowingLimit 32", "reclaimWithinCohort: Any", "runs 7 x 8 GPUs while vision is idle:", "32 own + 24 borrowed; the 8th job waits"], BLUE)
    s.box(500, 70, 420, 190, "ClusterQueue cq-vision", ["nominal 32 GPUs, lendingLimit 24", "8 GPUs always kept for vision", "returns with 3 x 8 GPUs: 1 from the kept 8,", "2 by reclaiming LLM's borrowed GPUs"], GREEN)
    s.box(960, 70, 420, 190, "What the accounting service sees", ["while LLM's 8th job waits: free GPUs", "the queue could use but may not:", "blocked_by_quota"], GREY)
    s.box(40, 310, 1340, 100, "Fair sharing (S09) and priority (S10) use the same objects", ["a pool queue owns 96 GPUs; research (weight 1) and LLM (weight 2) borrow; preemption moves shares to 32 and 64.",
                                                                                            "inside one queue: a release-priority job preempts a best-effort one; a job with a declared runtime limit fills a gap."], GREY)
    s.arrow([(500, 165), (460, 165)], "flow", "lend 24", 480, 158)
    return s.render()


def accounting():
    s = Svg(1420, 400, "Every GPU, every 10 seconds, in exactly one category")
    cats = [("busy", "allocated and working", GREEN), ("allocated_idle", "allocated, < 5% use", AMBER), ("waiting_for_gang", "held by a partial gang", RED),
            ("fragmented", "free, no pending pod fits", PURPLE), ("blocked_by_quota", "free, a pending pod fits, not admitted", BLUE), ("free", "free, nothing pending fits it", GREY)]
    for i, (t, d, c) in enumerate(cats):
        s.box(30 + i * 230, 60, 215, 100, t, [d], c)
    s.box(30, 200, 680, 160, "Inputs", ["Nodes and their allocatable GPUs; pods and the GPUs they hold;", "Kueue Workloads: pending, admitted, evicted; DCGM per-pod", "GPU utilization for the real GPUs; for simulated GPUs, the", "activity each pod declares (they do not exist)"], GREY)
    s.box(740, 200, 650, 160, "Plus lost work", ["when Kueue evicts a workload for preemption:", "GPUs x the time since its last checkpoint,", "assuming checkpoints every ds17/checkpoint-seconds", "from admission"], RED)
    return s.render()


def main():
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate([context, topology, layers, fragmentation, quotas, accounting], 1):
        (out / f"a18-{i}.svg").write_text(f())
    print(f"wrote 6 diagrams to {out}")


if __name__ == "__main__":
    main()

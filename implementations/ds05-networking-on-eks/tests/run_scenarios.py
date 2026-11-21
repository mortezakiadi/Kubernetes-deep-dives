#!/usr/bin/env python3
"""Runs the Design 05 test scenarios against the live network and records the evidence.
    run_scenarios.py [S01 S02 ... | all] [--out evidence/final]
For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (screenshot rendered
from the transcript). Every private value is redacted before anything is written.
"""
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ENV, KUBECTL, ROOT, Scenario, Step, redact  # noqa: E402
from helpers import *  # noqa: E402,F401,F403
from render_terminal import render as render_png  # noqa: E402


def cleanup_all():
    """Between scenarios: put back anything a scenario changed."""
    pass


NAMES = ["shared-main", "nonprod-main", "hybrid-second", "sandbox-second", "legacy-main"]


# --------------------------------------------------------------------------------------------------------------
# S01  the address plan
# --------------------------------------------------------------------------------------------------------------
def s01_address_plan():
    sc = Scenario("S01", "The address plan: pools, allocations and overlaps", "The address plan",
                  "Show that every routed VPC took its range from one plan, that the plan refuses a range that is already taken, and that a VPC which did not follow the plan is found.")
    ipam = awsj("ec2", "describe-ipams", region=MAIN)["Ipams"][0]
    scope = ipam["PrivateDefaultScopeId"]
    pools = awsj("ec2", "describe-ipam-pools", "--filters", "Name=ipam-scope-id,Values=" + scope, region=MAIN)["IpamPools"]
    by_desc = {p["Description"]: p for p in pools}
    cidrs = {}
    for p in pools:
        c = awsj("ec2", "get-ipam-pool-cidrs", "--ipam-pool-id", p["IpamPoolId"], region=MAIN).get("IpamPoolCidrs", [])
        cidrs[p["Description"]] = c[0]["Cidr"] if c else "-"
    rows = sorted(((p.get("Locale", "(global)"), p["Description"], cidrs[p["Description"]]) for p in pools), key=lambda r: (r[0], r[2]))
    show(sc, "aws ec2 describe-ipam-pools ; get-ipam-pool-cidrs   # the plan: one top range, a pool per region, a pool per environment",
         "\n".join(f"{loc:12s} {desc[:34]:34s} {cidr}" for loc, desc, cidr in rows))
    sc.expect("the plan has one top-level range and a pool for each region and environment", len(pools) == 9 and cidrs.get("all routed addresses of the platform") == "10.60.0.0/14",
              "Terraform creates the pools level by level: the top range, then a pool per region, then a pool per environment inside each region.")
    res = awsj("ec2", "get-ipam-resource-cidrs", "--ipam-scope-id", scope, "--filters", "Name=resource-type,Values=vpc", region=MAIN)["IpamResourceCidrs"]
    table = sorted(((r.get("ResourceName") or "(default VPC)", r["ResourceCidr"], r["ComplianceStatus"], r["ManagementState"], r["OverlapStatus"], r["ResourceRegion"]) for r in res
                    if r["ResourceCidr"].startswith(("10.6", "100.64"))), key=lambda r: (r[1], r[0]))
    show(sc, "aws ec2 get-ipam-resource-cidrs --filters resource-type=vpc   # what the VPCs actually use",
         "VPC                              CIDR            compliance   managed    overlap          region\n" + "\n".join(f"{n[:32]:32s} {c:15s} {co:12s} {m:10s} {o:16s} {r}" for n, c, co, m, o, r in table))
    managed = [t for t in table if t[3] == "managed"]
    sc.expect("all six routed VPCs took their range from the plan and are compliant", len(managed) == 6 and all(t[2] == "compliant" for t in managed),
              "A VPC created with an IPAM pool and a netmask length is given the next free block of that pool, and IPAM tracks it as managed.")
    legacy = [t for t in table if t[0].endswith("legacy-main")]
    sc.expect("the legacy VPC, which reused a range by hand, is reported as overlapping and unmanaged", bool(legacy) and legacy[0][3] == "unmanaged" and legacy[0][4] == "overlapping",
              "IPAM's resource discovery scans every VPC in the account, not only the ones it allocated, and compares their ranges.")
    pods = [t for t in table if t[1] == "100.64.0.0/16"]
    sc.expect("the pod range 100.64.0.0/16 is used in both cluster VPCs, on purpose", len(pods) == 2 and all(t[3] == "unmanaged" for t in pods),
              "Pod addresses are not part of the plan because they are never routed outside their VPC (S04). IPAM reports them as unmanaged and overlapping, which is the expected finding.")
    prod = by_desc["main-prod"]["IpamPoolId"]
    out = aws("ec2", "allocate-ipam-pool-cidr", "--ipam-pool-id", prod, "--cidr", "10.60.0.0/24", region=MAIN)
    show(sc, "aws ec2 allocate-ipam-pool-cidr --ipam-pool-id <main-prod pool> --cidr 10.60.0.0/24   # ask for a range inside prod-main's block", out)
    sc.expect("a range that is already allocated is refused", "overlaps with an existing allocation" in out, "The pool's whole /16 is held by the prod-main VPC, so nothing inside it is free.")
    regional = by_desc["region us-east-1"]["IpamPoolId"]
    free = aws("ec2", "allocate-ipam-pool-cidr", "--ipam-pool-id", regional, "--netmask-length", "24", region=MAIN)
    ok = '"IpamPoolAllocationId"' in free
    alloc_id = ""
    if ok:
        j = json.loads(free)["IpamPoolAllocation"]
        alloc_id = j["IpamPoolAllocationId"]
        show(sc, "aws ec2 allocate-ipam-pool-cidr --ipam-pool-id <us-east-1 regional pool> --netmask-length 24   # ask for the next free /24", f"allocated {j['Cidr']} ({alloc_id})")
        awsj("ec2", "release-ipam-pool-allocation", "--ipam-pool-id", regional, "--cidr", j["Cidr"], "--ipam-pool-allocation-id", alloc_id, region=MAIN)
        show(sc, "aws ec2 release-ipam-pool-allocation ...   # give it back", "released")
    else:
        show(sc, "aws ec2 allocate-ipam-pool-cidr ... --netmask-length 26", free)
    sc.expect("a free range is handed out, and handed back, without a human choosing it", ok, "Allocation is the pool's job: it picks a block that nothing else holds.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S02  one core network
# --------------------------------------------------------------------------------------------------------------
def s02_core_network():
    sc = Scenario("S02", "One core network: segments, attachments and routes", "Routing and peering: one core network",
                  "Show that every routed VPC is attached to one core network, that its segment comes from a tag, and that the routing tables of the segments say what the policy says.")
    cn = awsj("networkmanager", "list-core-networks", region="us-west-2")["CoreNetworks"][0]["CoreNetworkId"]
    core = awsj("networkmanager", "get-core-network", "--core-network-id", cn, region="us-west-2")["CoreNetwork"]
    show(sc, "aws networkmanager get-core-network   # one core network, an edge in each region, five segments",
         f"state: {core['State']}\nedges: " + ", ".join(f"{e['EdgeLocation']} (ASN {e['Asn']})" for e in core["Edges"]) + "\nsegments: " + ", ".join(s["Name"] for s in core["Segments"]))
    sc.expect("one core network is available, with an edge in each of the two regions and five segments",
              core["State"] == "AVAILABLE" and sorted(e["EdgeLocation"] for e in core["Edges"]) == sorted([MAIN, SECOND]) and sorted(s["Name"] for s in core["Segments"]) == ["hybrid", "nonprod", "prod", "sandbox", "shared"],
              "The core network is created from one policy document. Each region listed in it gets an edge, and each segment gets a routing domain on every edge.")
    att = awsj("networkmanager", "list-attachments", "--core-network-id", cn, region="us-west-2")["Attachments"]
    rows = sorted((next((t["Value"] for t in a.get("Tags", []) if t["Key"] == "Name"), "?").replace(PROJECT + "-", ""), a["EdgeLocation"], a["SegmentName"], a["State"], next((t["Value"] for t in a.get("Tags", []) if t["Key"] == "Segment"), "?")) for a in att)
    show(sc, "aws networkmanager list-attachments   # which segment each VPC landed in, and the tag that put it there",
         "attachment       edge         segment   state      Segment tag\n" + "\n".join(f"{n:16s} {e:12s} {s:9s} {st:10s} {t}" for n, e, s, st, t in rows))
    sc.expect("six VPCs are attached, each in the segment named by its Segment tag", len(rows) == 6 and all(r[2] == r[4] and r[3] == "AVAILABLE" for r in rows),
              "One attachment policy rule reads the Segment tag of an attachment and uses its value as the segment name. Nobody maps a VPC to a segment by hand.")
    sc.expect("the legacy VPC is not attached", not any("legacy" in r[0] for r in rows), "It was never given an attachment, so the core network has no route to it and it has none to the core network.")
    segs = {}
    for seg in ("prod", "nonprod", "shared", "hybrid", "sandbox"):
        r = awsj("networkmanager", "get-network-routes", "--global-network-id", core["GlobalNetworkId"] if "GlobalNetworkId" in core else cn, "--route-table-identifier",
                 json.dumps({"CoreNetworkSegmentEdge": {"CoreNetworkId": cn, "SegmentName": seg, "EdgeLocation": MAIN}}), region="us-west-2")
        segs[seg] = sorted({x["DestinationCidrBlock"] for x in r.get("NetworkRoutes", []) if x["DestinationCidrBlock"].startswith("10.6")})
        if seg == "prod":
            pod_routes = sorted({x["DestinationCidrBlock"] for x in r.get("NetworkRoutes", []) if x["DestinationCidrBlock"].startswith("100.64")})
    show(sc, f"aws networkmanager get-network-routes   # the routing table of each segment at the {MAIN} edge",
         "\n".join(f"{k:8s} {', '.join(v) if v else '(none)'}" for k, v in segs.items()))
    sc.expect("prod's table has prod, shared and hybrid ranges and no non-production range", {"10.60.0.0/16", "10.62.0.0/16", "10.61.1.0/24", "10.63.0.0/24"} <= set(segs["prod"]) and "10.61.0.0/24" not in segs["prod"] and "10.63.1.0/24" not in segs["prod"],
              "The policy shares shared with prod, and hybrid with prod. Nothing shares nonprod with prod, so the route to the non-production VPC is simply not in the table.")
    sc.expect("nonprod's table has nonprod and shared and nothing of prod", "10.61.0.0/24" in segs["nonprod"] and "10.61.1.0/24" in segs["nonprod"] and "10.60.0.0/16" not in segs["nonprod"],
              "Segments are non-transitive: nonprod sees shared, and shared sees prod, but nonprod does not see prod.")
    show(sc, "the pod range in prod's table (a secondary range of the cluster VPC)", ", ".join(pod_routes) or "(none)")
    sc.expect("the sandbox segment's table is empty: nothing is shared with it and its attachments are isolated from each other", segs["sandbox"] == [], "The sandbox segment shares no routes with any other segment, so no range appears in its table, not even the sandbox VPC's own.")
    sc.expect("a cluster VPC's pod range is advertised to the core network like any other range of the VPC", pod_routes == ["100.64.0.0/16"],
              "The core network takes every range of an attached VPC, including a secondary one. The same 100.64.0.0/16 is used in both cluster VPCs, and the table keeps one entry for it (S04 tests what that means).")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S03  who can reach whom
# --------------------------------------------------------------------------------------------------------------
EXPECTED = {("shared-main", "nonprod-main"), ("nonprod-main", "shared-main"), ("shared-main", "hybrid-second"), ("hybrid-second", "shared-main")}


def s03_reachability():
    sc = Scenario("S03", "Who can reach whom, and how fast", "Routing and peering; Network isolation in layers",
                  "Ask the network the question the design says must have one answer: can A reach B? Test every pair of test hosts, then the production clusters' load balancers, then measure the round trip and the largest packet across the core network.")
    V = vpcs()
    rows, results = [], {}
    for a in NAMES:
        row = []
        for b in NAMES:
            if a == b:
                row.append("   -  ")
                continue
            code, out = host(a, reach(V[b]["ip"], timeout=3))
            results[(a, b)] = out.startswith("OK")
            row.append(" open " if results[(a, b)] else "blocked")
        rows.append(f"{a:15s} " + " ".join(row))
    show(sc, "curl http://<host>:8080/whoami from each host to each other host   # 20 ordered pairs",
         f"{'from \\ to':15s} " + " ".join(f"{n[:7]:7s}" for n in NAMES) + "\n" + "\n".join(rows))
    got = {k for k, v in results.items() if v}
    sc.expect("exactly the four paths the policy allows are open: shared with nonprod and shared with hybrid, both ways", got == EXPECTED,
              "A segment sees only its own routes and the routes of segments it shares with. shared is shared with nonprod and hybrid, so those pairs have routes in both directions. nonprod and hybrid share nothing, so each has no route to the other.")
    sc.expect("nonprod cannot reach hybrid, although both can reach shared", ("nonprod-main", "hybrid-second") not in got and ("nonprod-main", "shared-main") in got and ("hybrid-second", "shared-main") in got,
              "Sharing is not transitive. That is what lets shared services be reachable from everything without making everything reachable from everything.")
    sc.expect("the sandbox and the legacy VPC reach nothing and are reached by nothing", not any(k for k in got if "sandbox-second" in k or "legacy-main" in k),
              "The sandbox segment shares nothing and its attachments are isolated. The legacy VPC is not attached at all.")
    try:
        albs = tf_out("30-platform", "albs")
    except Exception:
        albs = None
    if albs and isinstance(albs, dict) and albs.get("main"):
        prow = {}
        for a in ("shared-main", "nonprod-main", "hybrid-second", "sandbox-second", "legacy-main"):
            code, out = host(a, f"curl -s -m 4 -o /dev/null -w '%{{http_code}}' http://{albs['main']}/health; true")
            prow[a] = out.strip()
        show(sc, "curl http://<production load balancer>/health from each host   # the internal load balancer of the main cluster",
             "\n".join(f"{a:15s} {'open (HTTP ' + c + ')' if c == '200' else 'blocked'}" for a, c in prow.items()))
        sc.expect("the production load balancer answers shared and hybrid, and does not answer nonprod, sandbox or legacy",
                  prow["shared-main"] == "200" and prow["hybrid-second"] == "200" and all(prow[x] != "200" for x in ("nonprod-main", "sandbox-second", "legacy-main")),
                  "The load balancer sits in a prod VPC. Its address is in the prod segment's routing table and in the tables of the segments that share with prod (shared and hybrid), and nowhere else.")
    # speed and size
    code, out = run_host(sc, "shared-main", f"for ip in {V['nonprod-main']['ip']} {V['hybrid-second']['ip']}; do echo $ip; ping -c 8 -q $ip | tail -1; done",
                         display="[shared-main] ping -c 8 <nonprod host> ; ping -c 8 <hybrid host>   # same region, then the other region")
    rtts = [float(x) for x in re.findall(r"= [\d.]+/([\d.]+)/", out)]
    sc.expect("a call inside the region takes about a millisecond and a call to the other region about seventy", len(rtts) == 2 and rtts[0] < 5 and 40 < rtts[1] < 150,
              f"Average round trips: {rtts[0] if rtts else '?'} ms inside us-east-1 and {rtts[1] if len(rtts) > 1 else '?'} ms to eu-west-1. Distance, not the core network, sets the second number.")
    sizes = [1400, 8000, 8472, 8500]
    cmd = f"for s in {' '.join(map(str, sizes))}; do for ip in {V['nonprod-main']['ip']} {V['hybrid-second']['ip']}; do r=$(ping -M do -s $s -c 1 -W 2 $ip 2>&1 | grep -E 'bytes from|Frag|too long' | head -1); case \"$r\" in *'bytes from'*) echo \"payload $s to $ip: delivered\";; *) echo \"payload $s to $ip: refused ($(echo $r | grep -o 'mtu = [0-9]*\\|mtu=[0-9]*'))\";; esac; done; done"
    code, out = run_host(sc, "shared-main", cmd, display="[shared-main] ping -M do -s <size> <host>   # the largest packet that crosses the core network without fragmenting")
    sc.expect("a packet with 8472 bytes of payload crosses, one with 8500 does not: the core network's MTU is 8500", all(f"payload {n} to " in out for n in sizes) and out.count("delivered") == 6 and out.count("refused") == 2,
              "Cloud WAN carries packets of up to 8500 bytes, in or between regions. A larger one with the do-not-fragment bit set is answered with 'fragmentation needed'. This is the number to put in every node template.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S04  pods and addresses
# --------------------------------------------------------------------------------------------------------------
def cluster_sg(region, name):
    return aws("eks", "describe-cluster", "--name", name, "--query", "cluster.resourcesVpcConfig.clusterSecurityGroupId", "--output", "text", region=region)


def s04_pod_addresses():
    sc = Scenario("S04", "Pod addresses: a range of their own, reused, and not reachable", "The address plan; Inside a cluster and on the host",
                  "Show where pods get their addresses, that the same range is used in both clusters, that a pod's traffic leaves the VPC under its node's address, that nodes hand out addresses in blocks, and that no other VPC can reach a pod.")
    V = vpcs()
    for ctx, region in (("main", MAIN), ("second", SECOND)):
        rc, out = kube(ctx, "-n", "shop", "get", "pods", "-o", "wide")
        show(sc, f"kubectl --context {ctx} -n shop get pods -o wide   # {region}", "\n".join(l[:150] for l in out.splitlines()))
    pods = {c: kj(c, "-n", "shop", "get", "pods") for c in ("main", "second")}
    nodes = {c: kj(c, "get", "nodes") for c in ("main", "second")}
    pod_ips = {c: [i["status"]["podIP"] for i in pods[c]["items"]] for c in pods}
    node_ips = {c: [a["address"] for n in nodes[c]["items"] for a in n["status"]["addresses"] if a["type"] == "InternalIP"] for c in nodes}
    sc.expect("every pod has an address in 100.64.0.0/16 and every node one from the plan", all(ip.startswith("100.64.") for c in pod_ips for ip in pod_ips[c]) and all(ip.startswith("10.60.") for ip in node_ips["main"]) and all(ip.startswith("10.62.") for ip in node_ips["second"]),
              "Custom networking puts each node's pod interfaces in a pod subnet, chosen by the ENIConfig for the node's zone. The node's own interface stays in a node subnet, which comes from the plan.")
    sc.expect("both clusters use the same pod range", pod_ips["main"] and pod_ips["second"] and all(ip.startswith("100.64.") for ip in pod_ips["main"] + pod_ips["second"]),
              "The range is never routed outside a VPC for a pod's own traffic, so it can be the same everywhere and costs nothing from the plan.")
    # egress is translated
    probe_node = kj("main", "-n", "shop", "get", "pods", "-l", "app=probe")["items"][0]
    pnode_ip = next(a["address"] for n in nodes["main"]["items"] if n["metadata"]["name"] == probe_node["spec"]["nodeName"] for a in n["status"]["addresses"] if a["type"] == "InternalIP")
    host("shared-main", "journalctl --rotate >/dev/null 2>&1; true")
    run_pod(sc, "main", "shop", reach(V["shared-main"]["ip"]), display="[main/shop probe] curl http://<shared host>:8080/whoami   # call a host in another VPC from a pod")
    code, log = run_host(sc, "shared-main", "journalctl -u whoami --no-pager -n 20 | grep 'GET /whoami' | tail -1", display="[shared-main] the last request in the host's web log   # who did it see?")
    src = re.search(r"(\d+\.\d+\.\d+\.\d+) - - \[", log)
    show(sc, "the probe pod's address and its node's address", f"pod  {probe_node['status']['podIP']}\nnode {pnode_ip}")
    sc.expect("the host saw the request come from the pod's node, not from the pod", bool(src) and src.group(1) == pnode_ip,
              "The VPC CNI translates a pod's address to its node's address when the traffic leaves the VPC (source NAT stays on). The other side never sees a 100.64 address, which is why that range can be reused.")
    # prefix delegation
    node = nodes["main"]["items"][0]
    iid = node["spec"]["providerID"].split("/")[-1]
    enis = awsj("ec2", "describe-network-interfaces", "--filters", f"Name=attachment.instance-id,Values={iid}", region=MAIN)["NetworkInterfaces"]
    lines = []
    for e in sorted(enis, key=lambda e: e["Attachment"]["DeviceIndex"]):
        lines.append(f"interface {e['Attachment']['DeviceIndex']}  subnet {e['SubnetId'][-8:]}  addresses {len(e['PrivateIpAddresses'])}  /28 blocks {len(e.get('Ipv4Prefixes', []))}  " + ", ".join(p["Ipv4Prefix"] for p in e.get("Ipv4Prefixes", [])))
    show(sc, "aws ec2 describe-network-interfaces --filters instance=<a node>   # the node's interfaces", "\n".join(lines))
    pods_cap = node["status"]["allocatable"]["pods"]
    show(sc, "kubectl get node <a node> -o jsonpath='{.status.allocatable.pods}'   # how many pods the node accepts", pods_cap)
    sc.expect("the node holds its pod addresses as /28 blocks on a second interface", any(e.get("Ipv4Prefixes") for e in enis if e["Attachment"]["DeviceIndex"] > 0) and not any(e.get("Ipv4Prefixes") for e in enis if e["Attachment"]["DeviceIndex"] == 0),
              "Prefix delegation gives the interface /28 blocks of 16 addresses and the CNI hands them out to pods one by one. The first interface keeps only the node's own address, because custom networking moves pods off it.")
    sc.expect("the node accepts 110 pods, the limit I set", pods_cap == "110", "Without prefix delegation an m5.large would accept 29. The kubelet's limit is set in the node template to match the addresses.")
    # not reachable
    web = kj("main", "-n", "shop", "get", "pods", "-l", "app=web")["items"][0]["status"]["podIP"]
    code, before = run_host(sc, "shared-main", f"curl -s -m 4 -o /dev/null -w 'HTTP %{{http_code}}' http://{web}/health; echo; ping -c 2 -W 2 {web} | tail -2", display=f"[shared-main] curl http://<a web pod {web.rsplit('.', 2)[0]}.x.x>/health   # from another VPC, by pod address")
    sg = cluster_sg(MAIN, "ds05-networking-main")
    cn_arn = awsj("networkmanager", "list-core-networks", region="us-west-2")["CoreNetworks"][0]["CoreNetworkArn"]
    rt = awsj("ec2", "describe-route-tables", "--filters", f"Name=vpc-id,Values={V['shared-main']['id']}", "Name=tag:Name,Values=*-private", region=MAIN)["RouteTables"][0]
    rt_routes = sorted(f"{r.get('DestinationCidrBlock') or 'S3 prefix list'} -> {'core network' if r.get('CoreNetworkArn') else ('S3 gateway endpoint' if str(r.get('GatewayId', '')).startswith('vpce') else r.get('GatewayId', r.get('NatGatewayId', '?')))}" for r in rt["Routes"])
    show(sc, "aws ec2 describe-route-tables   # the route table of the shared VPC, the caller", "\n".join(rt_routes))
    sc.expect("the caller's route table sends only the plan's range (10.60.0.0/14) to the core network", "10.60.0.0/14 -> core network" in rt_routes and not any(r.startswith("100.64") for r in rt_routes),
              "Cloud WAN knows the pod range (S02), but a VPC only sends traffic to the core network for ranges that its own route table names. Terraform names the plan's range and nothing else.")

    def probe(label):
        return run_host(sc, "shared-main", f"curl -s -m 4 -o /dev/null -w 'HTTP %{{http_code}}' http://{web}/health; true", display=f"[shared-main] curl http://{web}/health   # {label}")[1]
    state = {}
    try:
        awsj("ec2", "authorize-security-group-ingress", "--group-id", sg, "--protocol", "tcp", "--port", "80", "--cidr", V["shared-main"]["cidr"], region=MAIN)
        time.sleep(3)
        state["sg_only"] = probe("a rule for the shared VPC added to the pods' security group; no route")
        awsj("ec2", "revoke-security-group-ingress", "--group-id", sg, "--protocol", "tcp", "--port", "80", "--cidr", V["shared-main"]["cidr"], region=MAIN)
        awsj("ec2", "create-route", "--route-table-id", rt["RouteTableId"], "--destination-cidr-block", "100.64.0.0/16", "--core-network-arn", cn_arn, region=MAIN)
        time.sleep(5)
        state["route_only"] = probe("a route to 100.64.0.0/16 added to the shared VPC; no security group rule")
        awsj("ec2", "authorize-security-group-ingress", "--group-id", sg, "--protocol", "tcp", "--port", "80", "--cidr", V["shared-main"]["cidr"], region=MAIN)
        time.sleep(3)
        state["both"] = probe("both the route and the security group rule")
    finally:
        awsj("ec2", "revoke-security-group-ingress", "--group-id", sg, "--protocol", "tcp", "--port", "80", "--cidr", V["shared-main"]["cidr"], region=MAIN)
        awsj("ec2", "delete-route", "--route-table-id", rt["RouteTableId"], "--destination-cidr-block", "100.64.0.0/16", region=MAIN)
    time.sleep(5)
    state["after"] = probe("both removed")
    sc.expect("a pod's address cannot be reached from another VPC", "HTTP 200" not in before and "HTTP 200" not in state["after"], "Two layers keep it out: the caller has no route to the pod range, and the pods' security group lets in only the cluster itself.")
    sc.expect("one layer is not enough: a security group rule alone, or a route alone, still fails", "HTTP 200" not in state["sg_only"] and "HTTP 200" not in state["route_only"], "Each layer denies on its own. Neither is the only protection.")
    sc.expect("both together open the path: the pod range is routed by the core network all the way to the cluster", "HTTP 200" in state["both"],
              "With the route and the rule in place the same request reaches the pod, which shows that the core network does deliver 100.64.0.0/16 to prod-main (S02). Isolation of pods is a property of the VPC route tables and security groups, not of the core network.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S05  running out of pod addresses
# --------------------------------------------------------------------------------------------------------------
def s05_address_exhaustion():
    sc = Scenario("S05", "Running out of pod addresses, and what it looks like", "Failure modes (pod IP exhaustion); The address plan",
                  "Fill a pod subnet that is too small, see how the shortage shows itself, find it from the subnet's own numbers, and show that the same workload runs on nodes whose pods have a subnet sized from the plan.")
    ctx = "main"
    tiny = kj(ctx, "get", "nodes", "-l", "eniconfig=tiny")["items"][0]["metadata"]["name"]
    eni = kj(ctx, "get", "eniconfig", "tiny")
    sub_id = eni["spec"]["subnet"]
    sub = awsj("ec2", "describe-subnets", "--subnet-ids", sub_id, region=MAIN)["Subnets"][0]
    show(sc, "aws ec2 describe-subnets   # the pod subnet of one node group, and the one for a normal zone",
         f"tiny pod subnet   {sub['CidrBlock']:18s} free addresses: {sub['AvailableIpAddressCount']}")
    zone = kj(ctx, "get", "eniconfig", "us-east-1a")["spec"]["subnet"]
    big = awsj("ec2", "describe-subnets", "--subnet-ids", zone, region=MAIN)["Subnets"][0]
    show(sc, "", f"zone pod subnet   {big['CidrBlock']:18s} free addresses: {big['AvailableIpAddressCount']}")
    kube(ctx, "taint", "node", tiny, "dedicated=tiny:NoSchedule", "--overwrite")
    manifest = json.dumps({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "fill", "namespace": "dev"},
                           "spec": {"replicas": 60, "selector": {"matchLabels": {"app": "fill"}},
                                    "template": {"metadata": {"labels": {"app": "fill"}},
                                                 "spec": {"nodeSelector": {"eniconfig": "tiny"}, "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "tiny", "effect": "NoSchedule"}],
                                                          "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.10", "resources": {"requests": {"cpu": "1m", "memory": "1Mi"}}}]}}}})
    try:
        t0 = time.time()
        subprocess.run([KUBECTL, "--context", ctx, "apply", "-f", "-"], input=manifest, text=True, capture_output=True, env=ENV)
        show(sc, "kubectl apply -f fill.yaml   # 60 small pods, all pinned to the node with the tiny pod subnet", "deployment.apps/fill created")
        time.sleep(100)

        def counts():
            items = kj(ctx, "-n", "dev", "get", "pods", "-l", "app=fill")["items"]
            ph = {}
            for i in items:
                st = i["status"]["phase"]
                if st == "Pending" and any(cs.get("state", {}).get("waiting", {}).get("reason") == "ContainerCreating" for cs in i["status"].get("containerStatuses", [])):
                    st = "ContainerCreating"
                ph[st] = ph.get(st, 0) + 1
            return ph
        ph = counts()
        show(sc, "kubectl -n dev get pods -l app=fill   # after 100 seconds", "\n".join(f"{k:18s} {v}" for k, v in sorted(ph.items())))
        rc, ev = kube(ctx, "-n", "dev", "get", "events", "--field-selector", "reason=FailedCreatePodSandBox")
        msg = next((l for l in ev.splitlines() if "failed to assign an IP" in l or "no IP addresses" in l.lower()), (ev.splitlines() or [""])[-1])
        show(sc, "kubectl -n dev get events --field-selector reason=FailedCreatePodSandBox   # why they are stuck", re.sub(r"\s+", " ", msg)[:300])
        sub2 = awsj("ec2", "describe-subnets", "--subnet-ids", sub_id, region=MAIN)["Subnets"][0]
        sub2 = awsj("ec2", "describe-subnets", "--subnet-ids", sub_id, region=MAIN)["Subnets"][0]
        import ipaddress
        net = ipaddress.ip_network(sub2["CidrBlock"])
        reserved = {net.network_address + i for i in range(4)} | {net.broadcast_address}
        taken = set()
        for e in awsj("ec2", "describe-network-interfaces", "--filters", f"Name=subnet-id,Values={sub_id}", region=MAIN)["NetworkInterfaces"]:
            for pf in e.get("Ipv4Prefixes", []):
                taken |= set(ipaddress.ip_network(pf["Ipv4Prefix"]))
            for ip in e["PrivateIpAddresses"]:
                taken.add(ipaddress.ip_address(ip["PrivateIpAddress"]))
        blocks = []
        for blk in net.subnets(new_prefix=28):
            addrs = set(blk)
            what = "free block" if not (addrs & (reserved | taken)) else ("held by the node as a prefix" if addrs <= taken else "unusable: holds an address AWS reserves")
            blocks.append((str(blk), what))
        free_blocks = [b for b, w in blocks if w == "free block"]
        show(sc, "aws ec2 describe-subnets ; describe-network-interfaces   # the tiny pod subnet now, and its four /28 blocks",
             f"{sub2['CidrBlock']}  free addresses reported by EC2: {sub2['AvailableIpAddressCount']}\n" + "\n".join(f"  {b:18s} {w}" for b, w in blocks))
        running = ph.get("Running", 0)
        stuck = ph.get("ContainerCreating", 0) + ph.get("Pending", 0)
        sc.expect("some of the pods run and the rest are stuck without an address", 5 <= running < 60 and stuck >= 10, f"{running} pods got an address and {stuck} did not. The node takes /28 blocks of 16 addresses from the subnet, and a /26 holds two usable blocks.")
        sc.expect("the cause is named in the pods' events", "IP" in msg and ("assign" in msg or "address" in msg.lower()), "The CNI cannot get a block from the subnet, so the pod sandbox cannot be created. The scheduler did not help: it counted the node's 110 pod limit, not addresses.")
        sc.expect("the subnet still reports free addresses, but not one of its four /28 blocks is free", sub2["AvailableIpAddressCount"] > 0 and not free_blocks and len(blocks) == 4,
                  "Two blocks are held by the node. The other two each contain an address AWS reserves, so no whole block can be carved from them. With prefix delegation, an alert on free addresses cannot see this. Alert on the pods' failed-sandbox events or on the CNI's own metrics.")
        # the same workload on normal nodes
        subprocess.run([KUBECTL, "--context", ctx, "-n", "dev", "patch", "deployment", "fill", "--type", "json", "-p", json.dumps([{"op": "remove", "path": "/spec/template/spec/nodeSelector"}])], capture_output=True, text=True, env=ENV)
        wait_for(lambda: counts().get("Running", 0) == 60, 240, 5)
        ph2 = counts()
        nodes_used = {i["spec"]["nodeName"] for i in kj(ctx, "-n", "dev", "get", "pods", "-l", "app=fill")["items"]}
        show(sc, "kubectl -n dev patch deployment fill --type json -p '[remove nodeSelector]' ; kubectl get pods   # let the same pods use the zone subnets", "\n".join(f"{k:18s} {v}" for k, v in sorted(ph2.items())) + f"\nnodes used: {len(nodes_used)}")
        sc.expect("the same 60 pods all run once they may use nodes whose pod subnets come from the plan", ph2.get("Running", 0) == 60, "The three /18 zone subnets hold 16,000 addresses each. Sizing pod ranges from the plan is the fix; the check above is how you find out you did not.")
    finally:
        kube(ctx, "-n", "dev", "delete", "deployment", "fill", "--wait=true")
        kube(ctx, "taint", "node", tiny, "dedicated=tiny:NoSchedule-")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S06  services across VPCs
# --------------------------------------------------------------------------------------------------------------
def signed_curl(domain, path="/"):
    """The command a caller runs to call a Lattice service with its instance role's credentials (SigV4)."""
    return ("T=$(curl -s -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token); "
            "R=$(curl -s -H \"X-aws-ec2-metadata-token: $T\" http://169.254.169.254/latest/meta-data/iam/security-credentials/); "
            "J=$(curl -s -H \"X-aws-ec2-metadata-token: $T\" http://169.254.169.254/latest/meta-data/iam/security-credentials/$R); "
            "g() { echo $J | python3 -c \"import sys,json;print(json.load(sys.stdin)['$1'])\"; }; "
            f"curl -s -m 8 -w '\\nHTTP %{{http_code}}' --aws-sigv4 'aws:amz:{MAIN}:vpc-lattice-svcs' --user \"$(g AccessKeyId):$(g SecretAccessKey)\" "
            f"-H \"x-amz-security-token: $(g Token)\" -H 'x-amz-content-sha256: UNSIGNED-PAYLOAD' http://{domain}{path} | grep -E '^(Hostname|X-Amzn-Lattice-Identity|RemoteAddr|HTTP|AccessDenied)' | cut -c1-420")


def until_text(sc, name, cmd, text, seconds, display):
    """Run a command on a host every few seconds until its output contains text; record only the last attempt, and say how long it took."""
    t0 = time.time()
    while True:
        code, out = host(name, cmd)
        if text in out or time.time() - t0 > seconds:
            break
        time.sleep(5)
    sc.steps.append(Step(redact(display), redact(out + f"\n(after {time.time() - t0:.0f} seconds)"), code, round(time.time() - t0, 2)))
    return out


def s06_lattice():
    sc = Scenario("S06", "Services across VPCs by name: VPC Lattice", "Services across clusters and accounts",
                  "Export the web service of the production cluster under a name, and call it from a VPC whose addresses overlap with the cluster's, with no route between them. Then show that the service's own policy, not the network, says who may call.")
    V = vpcs()
    rc, out = kube("main", "-n", "shop", "get", "httproute", "web", "-o", "jsonpath={.status.parents[0].conditions[*].type}={.status.parents[0].conditions[*].status}")
    svc = awsj("vpc-lattice", "list-services", region=MAIN)["items"][0]
    dom = svc["dnsEntry"]["domainName"]
    show(sc, "kubectl -n shop get httproute web ; aws vpc-lattice list-services   # the exported service", f"HTTPRoute conditions: {out}\nLattice service {svc['name']} ({svc['status']})\nname: {dom}")
    sc.expect("the route is accepted and the service exists in Lattice under a stable name", "Accepted" in out and "True" in out and svc["status"] == "ACTIVE" and svc["name"] == "web-shop",
              "The Gateway API controller turned the HTTPRoute for the web Service into a Lattice service, its listener and a target group of the pods' addresses.")
    sn = next(n["id"] for n in awsj("vpc-lattice", "list-service-networks", region=MAIN)["items"] if n["name"] == "ds05-lattice")
    assoc = awsj("vpc-lattice", "list-service-network-vpc-associations", "--service-network-identifier", sn, region=MAIN)["items"]
    names = {a["vpcId"]: a["status"] for a in assoc}
    inv = {v["id"]: k for k, v in V.items()}
    show(sc, "aws vpc-lattice list-service-network-vpc-associations   # the VPCs that can reach the service network", "\n".join(f"{inv.get(v, v):14s} {st}" for v, st in sorted(names.items(), key=lambda kv: inv.get(kv[0], kv[0]))))
    sc.expect("three VPCs are associated: the cluster's, the non-production one and the legacy one", {inv.get(v) for v in names} == {"prod-main", "nonprod-main", "legacy-main"} and all(st == "ACTIVE" for st in names.values()),
              "Terraform associates the callers' VPCs. The controller associates the cluster's own. The association is the only thing that connects a VPC to the network of services.")
    cmp_ = f"legacy VPC range {V['legacy-main']['cidr']} ; prod-main range {V['prod-main']['cidr']}"
    show(sc, "the two ranges", cmp_)
    sc.expect("the legacy VPC and the cluster's VPC use the same range", V["legacy-main"]["cidr"] == V["prod-main"]["cidr"], "That is why no route can exist between them. Lattice does not need one.")
    node_ip = next(a["address"] for a in kj("main", "get", "nodes")["items"][0]["status"]["addresses"] if a["type"] == "InternalIP")
    code, out = run_host(sc, "legacy-main", f"ping -c 2 -W 1 {node_ip} | tail -2; true", display=f"[legacy-main] ping {node_ip}   # an address that exists in the cluster's VPC")
    sc.expect("a ping to the cluster's range goes nowhere from the legacy VPC", "100% packet loss" in out or "0 received" in out, "The legacy VPC has no route to the core network, and a route to the same range would reach itself.")
    code, out = run_host(sc, "legacy-main", f"curl -s -m 8 -w '\\nHTTP %{{http_code}}' http://{dom}/ | grep -E 'HTTP|AccessDenied' | cut -c1-200", display="[legacy-main] curl http://<service name>/   # by name, without credentials")
    sc.expect("a call without credentials is refused by the service", "HTTP 403" in out, "The service's authentication type is AWS_IAM: Lattice checks the caller's signature before a request reaches the pods.")
    code, out = run_host(sc, "legacy-main", signed_curl(dom), display="[legacy-main] curl --aws-sigv4 http://<service name>/   # signed with the host's role")
    sc.expect("the legacy host, signed, reaches a pod of the web service, and the pod is told who called", "HTTP 200" in out and "Hostname: web-" in out and "ds05-networking-legacy-main-host" in out,
              "Lattice delivers the request to a pod over its own network and adds the verified caller identity as a header. The pod sees a Lattice address, not the caller's.")
    code, out = run_host(sc, "nonprod-main", signed_curl(dom), display="[nonprod-main] curl --aws-sigv4 http://<service name>/   # signed, but not named in the service's policy")
    sc.expect("the non-production host, signed, is refused: the service's policy does not name it", "HTTP 403" in out and "AccessDenied" in out,
              "Both policies must allow: the caller's own (it has one) and the service's (it does not list nonprod). The caller's team cannot grant itself access to somebody else's service.")
    code, out = run_host(sc, "nonprod-main", f"curl -s -m 4 -o /dev/null -w 'HTTP %{{http_code}}' http://{tf_out('30-platform', 'albs')['main']}/health; true", display="[nonprod-main] curl http://<production load balancer>/health   # the network path, for comparison")
    sc.expect("the same host has no network path to production", "HTTP 000" in out or "HTTP 200" not in out, "S03: nonprod and prod share no routes. Lattice gave nonprod no path either: it gave it a named, authorized one that production's owner can grant or withdraw.")
    # grant, then withdraw
    pol = kj("main", "-n", "shop", "get", "iamauthpolicy", "web")
    orig = json.loads(pol["spec"]["policy"])
    roles = orig["Statement"][0]["Principal"]["AWS"]
    granted = json.loads(json.dumps(orig))
    granted["Statement"][0]["Principal"]["AWS"] = list(roles) + [V["nonprod-main"]["role"]]
    try:
        subprocess.run([KUBECTL, "--context", "main", "-n", "shop", "patch", "iamauthpolicy", "web", "--type", "merge", "-p", json.dumps({"spec": {"policy": json.dumps(granted)}})], capture_output=True, text=True, env=ENV)
        out2 = until_text(sc, "nonprod-main", signed_curl(dom), "HTTP 200", 120, "[nonprod-main] the same call, repeated until it works, after the policy names nonprod's role (a one-line change to the IAMAuthPolicy)")
    finally:
        subprocess.run([KUBECTL, "--context", "main", "-n", "shop", "patch", "iamauthpolicy", "web", "--type", "merge", "-p", json.dumps({"spec": {"policy": json.dumps(orig)}})], capture_output=True, text=True, env=ENV)
    out3 = until_text(sc, "nonprod-main", signed_curl(dom), "HTTP 403", 120, "[nonprod-main] the same call, repeated until it is refused again, after the change is reverted")
    sc.expect("granting is one change in the cluster, and so is withdrawing", "HTTP 200" in out2 and "HTTP 403" in out3, "The policy is a Kubernetes object owned by the team that owns the service. The controller writes it to Lattice, and a change takes effect within a minute or so (the time is printed).")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S07  load balancing in layers
# --------------------------------------------------------------------------------------------------------------
def pod_zones(ctx, ns="shop"):
    nodes = {n["metadata"]["name"]: n["metadata"]["labels"]["topology.kubernetes.io/zone"] for n in kj(ctx, "get", "nodes")["items"]}
    return {p["metadata"]["name"]: nodes[p["spec"]["nodeName"]] for p in kj(ctx, "-n", ns, "get", "pods", "-l", "app=web")["items"] if p["spec"].get("nodeName")}


def zone_spread(ctx, n=60):
    """Call the web service n times from the probe pod and count the answers by the zone of the pod that gave them."""
    zones = pod_zones(ctx)
    probe = kj(ctx, "-n", "shop", "get", "pods", "-l", "app=probe")["items"][0]
    nodes = {n_["metadata"]["name"]: n_["metadata"]["labels"]["topology.kubernetes.io/zone"] for n_ in kj(ctx, "get", "nodes")["items"]}
    mine = nodes[probe["spec"]["nodeName"]]
    code, out = pod(ctx, "shop", f"for i in $(seq {n}); do curl -s -m 2 http://web.shop.svc/ | grep -m1 Hostname; done")
    got = [l.split()[-1] for l in out.splitlines() if l.startswith("Hostname")]
    count = {}
    for h in got:
        count[zones.get(h, "?")] = count.get(zones.get(h, "?"), 0) + 1
    return mine, count, len(got)


def s07_load_balancing():
    sc = Scenario("S07", "Load balancing in layers: DNS, load balancer, service", "Load balancing in layers",
                  "Follow a request down the layers. A name that answers with the nearest region and drops a region that cannot serve. A load balancer that sends to pod addresses, not nodes. A service that prefers the caller's zone and still fails over. A rolling update that does not drop requests.")
    albs = tf_out("30-platform", "albs")
    # a) global: the name
    ans = {}
    for ctx in ("main", "second"):
        rc, out = pod(ctx, "shop", "dig +short app.ds05.internal")
        ans[ctx] = sorted(out.split())
    show(sc, "dig +short app.ds05.internal   # the same name, asked from a pod in each region", f"from {MAIN}:  {' '.join(ans['main'])}\nfrom {SECOND}: {' '.join(ans['second'])}")
    sc.expect("the same name gives the nearest region's load balancer", ans["main"] and ans["second"] and all(a.startswith("10.60.") for a in ans["main"]) and all(a.startswith("10.62.") for a in ans["second"]),
              "Two latency-routed alias records share one name. Route 53 answers each caller with the record of the region closest to the VPC the question came from.")
    # b) regional: targets
    lb = awsj("elbv2", "describe-load-balancers", "--names", "ds05-main", region=MAIN)["LoadBalancers"][0]
    tg = awsj("elbv2", "describe-target-groups", "--load-balancer-arn", lb["LoadBalancerArn"], region=MAIN)["TargetGroups"][0]
    th = awsj("elbv2", "describe-target-health", "--target-group-arn", tg["TargetGroupArn"], region=MAIN)["TargetHealthDescriptions"]
    show(sc, "aws elbv2 describe-target-health   # what the load balancer sends to", f"target type: {tg['TargetType']}\n" + "\n".join(f"{t['Target']['Id']:16s} {t['Target'].get('AvailabilityZone', '?'):12s} {t['TargetHealth']['State']}" for t in sorted(th, key=lambda t: t["Target"]["Id"])))
    sc.expect("the load balancer sends straight to six pod addresses in three zones, all healthy", tg["TargetType"] == "ip" and len(th) == 6 and all(t["TargetHealth"]["State"] == "healthy" for t in th) and len({t["Target"].get("AvailabilityZone") for t in th}) == 3
              and all(t["Target"]["Id"].startswith("100.64.") for t in th),
              "The Ingress asks for target type ip, so the controller registers each pod's own address and not a node port. The load balancer checks each pod's /health every five seconds.")
    # c) global: a region that cannot serve is dropped
    kube("main", "-n", "shop", "scale", "deployment/web", "--replicas=0")
    t0 = time.time()
    flipped = wait_for(lambda: all(a.startswith("10.62.") for a in pod("main", "shop", "dig +short app.ds05.internal")[1].split()) and pod("main", "shop", "dig +short app.ds05.internal")[1].split(), 420, 8)
    took = time.time() - t0
    rc, body = pod("main", "shop", "curl -s -m 4 http://app.ds05.internal/ | head -1")
    show(sc, "kubectl -n shop scale deployment/web --replicas=0   # the main region's web service has no pod; then dig again from the main region",
         f"answer after {took:.0f} seconds: {' '.join(flipped) if flipped else '(no change)'}\ncurl http://app.ds05.internal/ -> {body}")
    sc.expect("a region with no healthy target is dropped from the answer, and the caller is sent to the other region", bool(flipped) and "ds05-networking-second" in body,
              f"The alias records evaluate the load balancer's target health. With no healthy target the main record is no longer eligible, so Route 53 answers with the next best region. It took {took:.0f} seconds here.")
    kube("main", "-n", "shop", "scale", "deployment/web", "--replicas=6")
    t1 = time.time()
    back = wait_for(lambda: pod("main", "shop", "dig +short app.ds05.internal")[1].split() and all(a.startswith("10.60.") for a in pod("main", "shop", "dig +short app.ds05.internal")[1].split()), 420, 8)
    show(sc, "kubectl -n shop scale deployment/web --replicas=6   # restore; then dig again", f"the nearest region is answered again after {time.time() - t1:.0f} seconds" if back else "(not restored in time)")
    sc.expect("when its pods are healthy again, the region is answered again", bool(back), "Nothing was changed in DNS: the health of the load balancer's targets is what the record follows.")
    wait_for(lambda: kj("main", "-n", "shop", "get", "deployment", "web")["status"].get("readyReplicas") == 6, 120, 3)
    # d) in-cluster: the zone
    mine, base, n1 = zone_spread("main")
    show(sc, "curl http://web.shop.svc/ x 60 from a pod in " + mine + "   # a plain Service: any pod, any zone", ", ".join(f"{z}: {c}" for z, c in sorted(base.items())))
    kube("main", "-n", "shop", "patch", "service", "web", "--type", "merge", "-p", '{"spec":{"trafficDistribution":"PreferClose"}}')
    time.sleep(15)
    mine, close, n2 = zone_spread("main")
    show(sc, "kubectl -n shop patch service web -p '{\"spec\":{\"trafficDistribution\":\"PreferClose\"}}' ; the same 60 calls", ", ".join(f"{z}: {c}" for z, c in sorted(close.items())))
    sc.expect("a plain Service spreads calls over all three zones, a PreferClose Service keeps them in the caller's zone", n1 >= 55 and n2 >= 55 and len(base) == 3 and base.get(mine, 0) < 0.8 * n1 and close.get(mine, 0) >= 0.95 * n2,
              f"The same-zone share went from {100 * base.get(mine, 0) // max(n1, 1)}% to {100 * close.get(mine, 0) // max(n2, 1)}%. Each cross-zone call is data that is paid for twice (S10).")
    zones = pod_zones("main")
    mine_pods = [p for p, z in zones.items() if z == mine]
    ips = {p["metadata"]["name"]: p["status"]["podIP"] for p in kj("main", "-n", "shop", "get", "pods", "-l", "app=web")["items"]}
    for p in mine_pods:
        pod("main", "shop", f"curl -s -m 3 -X POST -d 500 http://{ips[p]}/health >/dev/null")
    time.sleep(15)
    mine, fail, n3 = zone_spread("main")
    show(sc, f"make every web pod in {mine} fail its readiness check ({len(mine_pods)} pods), then the same 60 calls", ", ".join(f"{z}: {c}" for z, c in sorted(fail.items())) + f"   ({n3} of 60 answered)")
    for p in mine_pods:
        pod("main", "shop", f"curl -s -m 3 -X POST -d 200 http://{ips[p]}/health >/dev/null")
    sc.expect("when the local zone has no ready pod, calls still succeed from the other zones", n3 >= 55 and fail.get(mine, 0) == 0, "PreferClose is a preference. With no ready endpoint in the zone, the Service routes to endpoints in the others.")
    kube("main", "-n", "shop", "patch", "service", "web", "--type", "merge", "-p", '{"spec":{"trafficDistribution":null}}')
    # e) draining
    alb = albs["main"]

    def load(seconds, retry=False):
        r = "--retry 3 --retry-all-errors --retry-delay 0 " if retry else ""
        return host_start("shared-main", f"n=0; end=$((SECONDS+{seconds})); : > /tmp/bad; while [ $SECONDS -lt $end ]; do c=$(curl -s -o /dev/null -m 2 {r}-w '%{{http_code}}' http://{alb}/); n=$((n+1)); [ \"$c\" = 200 ] || echo \"$(date +%s) $c\" >> /tmp/bad; done; echo \"requests $n\"; echo \"failed $(wc -l < /tmp/bad)\"; cat /tmp/bad | awk '{{print $2}}' | sort | uniq -c | tr '\\n' ' '", seconds + 60)

    def summary(out):
        n = int(re.search(r"requests (\d+)", out).group(1))
        f = int(re.search(r"failed (\d+)", out).group(1))
        return n, f, out.splitlines()[-1].strip() if out.splitlines() else ""
    wait_for(lambda: kj("main", "-n", "shop", "get", "deployment", "web")["status"].get("readyReplicas") == 6, 240, 3)
    h = load(45)
    out0 = host_finish(h, 200)
    n0, f0, _ = summary(out0)
    h = load(75)
    time.sleep(10)
    kube("main", "-n", "shop", "scale", "deployment/web", "--replicas=8")
    time.sleep(50)
    out1 = host_finish(h, 200)
    n1, f1, _ = summary(out1)
    wait_for(lambda: kj("main", "-n", "shop", "get", "deployment", "web")["status"].get("readyReplicas") == 8, 240, 3)
    h = load(75)
    time.sleep(10)
    kube("main", "-n", "shop", "scale", "deployment/web", "--replicas=6")
    time.sleep(55)
    out2 = host_finish(h, 200)
    n2, f2, kinds2 = summary(out2)
    wait_for(lambda: kj("main", "-n", "shop", "get", "deployment", "web")["status"].get("readyReplicas") == 6, 240, 3)
    h = load(75, retry=True)
    time.sleep(10)
    kube("main", "-n", "shop", "scale", "deployment/web", "--replicas=8")
    time.sleep(25)
    kube("main", "-n", "shop", "scale", "deployment/web", "--replicas=6")
    time.sleep(35)
    out3 = host_finish(h, 200)
    n3, f3, _ = summary(out3)
    show(sc, "a steady stream of calls to the load balancer from the shared host, while pods come and go (readiness gates on, preStop 45 s, load balancer delay 30 s)",
         f"no change:                {n0:6d} calls, {f0} failed\n6 -> 8 pods:               {n1:6d} calls, {f1} failed\n8 -> 6 pods (two removed): {n2:6d} calls, {f2} failed ({kinds2})\nsame, caller retries:     {n3:6d} calls, {f3} failed")
    sc.expect("with no change and when pods are added, no call fails", f0 == 0 and f1 == 0, "Adding a pod does not disturb the others: the load balancer sends to a new address only when it is healthy, and the readiness gate holds the pod back until then.")
    sc.expect("when two pods are removed, a few calls fail in the first seconds, in a number that is a small fraction of all calls", 0 < f2 < 0.01 * n2,
              "I could not remove these errors with the settings the design names (a drain delay shorter than the grace period, a pre-stop pause that outlasts the delay, readiness gates). They come at the moment a pod is told to stop, as 502 answers and connection time-outs. I did not find the cause.")
    sc.expect("a caller that retries an idempotent request loses none", f3 == 0, "The remaining protection is on the client side: a short retry for requests that can be repeated.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S08  isolation in layers
# --------------------------------------------------------------------------------------------------------------
def apply_yaml(ctx, doc):
    p = subprocess.run([KUBECTL, "--context", ctx, "apply", "-f", "-"], input=json.dumps(doc), text=True, capture_output=True, env=ENV)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def cnp(name, tier, priority, subject, ingress=None, egress=None):
    spec = {"tier": tier, "priority": priority, "subject": subject}
    if ingress:
        spec["ingress"] = ingress
    if egress:
        spec["egress"] = egress
    return {"apiVersion": "networking.k8s.aws/v1alpha1", "kind": "ClusterNetworkPolicy", "metadata": {"name": name}, "spec": spec}


def ns_sel(name):
    return {"namespaces": {"matchLabels": {"kubernetes.io/metadata.name": name}}}


def can(ctx, ns, url, seconds=3):
    """True when a call from the probe pod of a namespace succeeds."""
    rc, out = pod(ctx, ns, f"curl -s -m {seconds} -o /dev/null -w '%{{http_code}}' {url}; true")
    return out.strip().endswith("200")


def settle(fn, want, seconds=60):
    """Policies reach the nodes within seconds; wait until a check shows the wanted answer, and return it."""
    end = time.time() + seconds
    v = fn()
    while v != want and time.time() < end:
        time.sleep(3)
        v = fn()
    return v


def s08_isolation():
    sc = Scenario("S08", "Isolation in layers: guardrails the namespaces cannot override", "Network isolation in layers",
                  "Try the paths that must fail. Start with a cluster where everything can talk to everything, add the cluster-wide guardrails one at a time, and show that a team's own policy can open what the guardrails leave to it and cannot open what they forbid.")
    ctx = "main"
    LEDGER = "http://ledger.payments.svc/"
    WEB = "http://web.shop.svc/"
    IMDS = "http://169.254.169.254/latest/meta-data/"
    created = []

    def put(doc, label):
        ok, msg = apply_yaml(ctx, doc)
        created.append((doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"]))
        show(sc, f"kubectl apply -f {doc['metadata']['name']}.yaml   # {label}", msg or "applied")
        return ok

    def matrix(title):
        rows = {
            "dev -> payments ledger": can(ctx, "dev", LEDGER),
            "shop -> payments ledger": can(ctx, "shop", LEDGER),
            "monitoring -> payments ledger": can(ctx, "monitoring", LEDGER),
            "dev -> shop web": can(ctx, "dev", WEB),
            "any pod -> instance metadata": can(ctx, "dev", IMDS + "instance-id") or (pod(ctx, "dev", "curl -s -m 3 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 5' http://169.254.169.254/latest/api/token | wc -c")[1].strip() not in ("0", "")),
        }
        show(sc, f"the paths that must fail, and the ones that must work   # {title}", "\n".join(f"{k:32s} {'open' if v else 'blocked'}" for k, v in rows.items()))
        return rows
    try:
        base = matrix("no policy at all")
        sc.expect("with no policy, every path is open, including the instance metadata service", all(base.values()),
                  "A cluster without policies allows everything. The metadata service answers a pod because the node's metadata hop limit is 2, which containers need for some SDKs and which also lets a compromised pod ask for the node's credentials.")
        # layer: admin guardrails
        put(cnp("admin-no-imds", "Admin", 5, {"namespaces": {}}, egress=[{"name": "deny-imds", "action": "Deny", "to": [{"networks": ["169.254.169.254/32"]}]}]),
            "Admin tier: no pod may reach the instance metadata service")
        put(cnp("admin-protect-payments", "Admin", 10, ns_sel("payments"),
                ingress=[{"name": "deny-everyone-else", "action": "Deny",
                          "from": [{"namespaces": {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["shop", "monitoring", "payments"]}]}}]}]),
            "Admin tier: payments refuses every namespace except shop, monitoring and itself")
        guard = None

        def probe_state():
            return (can(ctx, "dev", LEDGER), can(ctx, "shop", LEDGER))
        settle(probe_state, (False, True), 90)
        guard = matrix("with the two Admin guardrails")
        sc.expect("the guardrails close the two paths and leave the allowed ones open", not guard["dev -> payments ledger"] and guard["shop -> payments ledger"] and guard["monitoring -> payments ledger"] and guard["dev -> shop web"] and not guard["any pod -> instance metadata"],
                  "Admin policies are evaluated first. A Deny wins over any Accept, whatever the order, so the exception for shop and monitoring is written into the Deny itself (everyone except these). A pod in dev cannot reach payments and no pod reaches the metadata service, while shop and monitoring still reach the ledger.")
        # a team cannot override
        put({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "let-dev-in", "namespace": "payments"},
             "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "dev"}}}]}]}},
            "the payments team writes a NetworkPolicy that lets dev in")
        time.sleep(20)
        still = can(ctx, "dev", LEDGER)
        show(sc, "dev -> payments ledger, after the team's own policy", "open" if still else "blocked")
        sc.expect("a namespace's own policy cannot open what an Admin policy denies", not still, "The Admin tier is above the namespace tier. The team can only restrict further what the guardrails leave open.")
        # baseline: a default that a team can override
        put(cnp("baseline-deny-dev", "Baseline", 100, ns_sel("shop"), ingress=[{"name": "deny-dev", "action": "Deny", "from": [ns_sel("dev")]}]),
            "Baseline tier: shop refuses calls from dev unless its team says otherwise")
        settle(lambda: can(ctx, "dev", WEB), False, 90)
        b1 = can(ctx, "dev", WEB)
        allow_dev = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "allow-dev", "namespace": "shop"},
                     "spec": {"podSelector": {"matchLabels": {"app": "web"}}, "policyTypes": ["Ingress"],
                              "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "dev"}}}, {"ipBlock": {"cidr": "10.60.0.0/16"}}]}]}}
        put(allow_dev, "the shop team opens web to dev, and to the load balancer's addresses, with a NetworkPolicy")
        b2 = settle(lambda: can(ctx, "dev", WEB), True, 90)
        show(sc, "dev -> shop web", f"with the Baseline deny naming dev: {'open' if b1 else 'blocked'}\nafter the shop team's NetworkPolicy: {'open' if b2 else 'blocked'}")
        sc.expect("a Baseline deny closes dev to shop, and the shop team's own policy opens it again", not b1 and b2,
                  "The Baseline tier is below the namespace tier. It sets the default posture, and a NetworkPolicy of the owning team overrides it for exactly what the team allows.")
        kube(ctx, "delete", "clusternetworkpolicy", "baseline-deny-dev")
        # the form in the design's own example: deny from every namespace
        put(cnp("baseline-default-deny", "Baseline", 100, {"namespaces": {}}, ingress=[{"name": "deny-all-pods", "action": "Deny", "from": [{"namespaces": {"matchLabels": {}}}]}]),
            "Baseline tier, the form in the design's example: every namespace refuses calls from every namespace")
        allow_all = json.loads(json.dumps(allow_dev))
        allow_all["metadata"]["name"] = "allow-everyone"
        allow_all["spec"]["ingress"] = [{"from": [{"namespaceSelector": {}}, {"ipBlock": {"cidr": "10.60.0.0/16"}}]}]
        put(allow_all, "the shop team admits every namespace with a NetworkPolicy")
        time.sleep(60)
        c1, c2 = can(ctx, "dev", WEB), can(ctx, "shop", WEB)
        show(sc, "dev -> shop web ; shop -> shop web   # after 60 seconds", f"dev: {'open' if c1 else 'blocked'}\nshop (its own namespace): {'open' if c2 else 'blocked'}")
        sc.expect("finding: a Baseline deny that names every namespace was NOT overridden by a NetworkPolicy, not even inside the namespace's own",
                  not c1 and not c2,
                  "The same deny naming one namespace was overridden (above). With an all-namespaces peer, the NetworkPolicy had no effect in this cluster (VPC CNI add-on 1.22.4). I did not find the cause. The design's example uses this form for its zero-trust default, so treat it as untested for exceptions.")
        kube(ctx, "delete", "clusternetworkpolicy", "baseline-default-deny")
        kube(ctx, "-n", "shop", "delete", "networkpolicy", "allow-everyone")
        # traffic from outside the cluster is not pod-to-pod
        albs = tf_out("30-platform", "albs")
        code, out = host("shared-main", f"curl -s -m 4 -o /dev/null -w '%{{http_code}}' http://{albs['main']}/health; true")
        show(sc, "[shared-main] curl http://<production load balancer>/health   # traffic from outside the cluster, with every policy in place", f"HTTP {out.strip()}")
        sc.expect("the policies, written for pod-to-pod traffic, do not cut the load balancer's traffic", out.strip().endswith("200"), "Namespace selectors match pods. The load balancer's address is in the VPC, so it is governed by the security group layer (S04), not by these policies.")
    finally:
        for kind, ns, name in created:
            kube(ctx, *(["-n", ns] if ns else []), "delete", kind.lower(), name, "--ignore-not-found")
    # the second cluster: names, not addresses
    ctx2 = "second"
    check = lambda url: can(ctx2, "dev", url, 4)
    open1 = (check("https://checkip.amazonaws.com"), check("https://aws.amazon.com"))
    show(sc, "[second/dev] curl https://checkip.amazonaws.com ; curl https://aws.amazon.com   # no policy yet", "checkip.amazonaws.com: " + ("open" if open1[0] else "blocked") + "\naws.amazon.com: " + ("open" if open1[1] else "blocked"))
    fq = {"apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy", "metadata": {"name": "egress-by-name", "namespace": "dev"},
          "spec": {"endpointSelector": {}, "egress": [
              {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}], "toPorts": [{"ports": [{"port": "53", "protocol": "ANY"}], "rules": {"dns": [{"matchPattern": "*"}]}}]},
              {"toFQDNs": [{"matchName": "checkip.amazonaws.com"}], "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}]}}
    ok, msg = apply_yaml(ctx2, fq)
    show(sc, "kubectl --context second apply -f egress-by-name.yaml   # Cilium: dev may use the DNS service and one domain name, nothing else", msg or "applied")
    try:
        r = settle(lambda: (check("https://checkip.amazonaws.com"), check("https://aws.amazon.com")), (True, False), 90)
        show(sc, "[second/dev] the same two calls, with the policy", "checkip.amazonaws.com: " + ("open" if r[0] else "blocked") + "\naws.amazon.com: " + ("open" if r[1] else "blocked"))
        rc, cache = kube(ctx2, "-n", "kube-system", "exec", "ds/cilium", "--", "cilium-dbg", "fqdn", "cache", "list")
        show(sc, "kubectl -n kube-system exec ds/cilium -- cilium-dbg fqdn cache list   # what the name resolved to", "\n".join(cache.splitlines()[:8]))
        sc.expect("a pod may reach the one domain its policy names and no other", open1[0] and open1[1] and r == (True, False),
                  "Cilium watches the pod's DNS answers and allows only the addresses that the named domain resolves to. The rule is written with a name, so it keeps working when the addresses change.")
    finally:
        kube(ctx2, "-n", "dev", "delete", "ciliumnetworkpolicy", "egress-by-name", "--ignore-not-found")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S09  DNS across clusters and the data center
# --------------------------------------------------------------------------------------------------------------
DNS_COUNT = "curl -s 169.254.20.10:9253/metrics | grep '^coredns_dns_requests_total' | grep 'server=\"dns://172.20.0.10:53\"' | awk '{s+=$2} END {print s+0}'"


def s09_dns():
    sc = Scenario("S09", "DNS across clusters and the data center", "DNS across clouds and clusters",
                  "Resolve one name from every kind of place: a pod, a VPC that shares the private zone, a VPC that does not, and the data center's own DNS server. Then count what a pod's resolver does for one lookup.")
    V = vpcs()
    dns = tf_out("10-network", "dns")
    name_in = "registry.ds05.internal"
    name_dc = "registry.corp.dc.test"
    shared_ip, dc_ip = V["shared-main"]["ip"], V["hybrid-second"]["ip"]
    rows = []

    def ask(label, where, cmd):
        if where[0] == "pod":
            code, out = pod(where[1], where[2], cmd)
        else:
            code, out = host(where[1], cmd)
        a = out.strip().splitlines()[0] if out.strip() else ""
        rows.append((label, a or "(no answer)"))
        return a
    r = {}
    r["pod_main_in"] = ask(f"pod in {MAIN}      {name_in}", ("pod", "main", "shop"), f"dig +short {name_in}")
    r["pod_second_in"] = ask(f"pod in {SECOND}   {name_in}", ("pod", "second", "shop"), f"dig +short {name_in}")
    r["nonprod_in"] = ask(f"nonprod host     {name_in}", ("host", "nonprod-main"), f"dig +short {name_in}")
    r["legacy_in"] = ask(f"legacy host      {name_in}", ("host", "legacy-main"), f"dig +short {name_in}")
    r["dc_vpc_in"] = ask(f"data center host, VPC resolver   {name_in}", ("host", "hybrid-second"), f"dig +short {name_in}")
    r["dc_dns_in"] = ask(f"data center host, its own DNS    {name_in}", ("host", "hybrid-second"), f"dig +short @127.0.0.1 {name_in}")
    show(sc, f"dig +short {name_in}   # a name in the private zone, from six places", "\n".join(f"{l:50s} {a}" for l, a in rows))
    sc.expect("the private zone answers every VPC it is associated with, in both regions", r["pod_main_in"] == shared_ip and r["pod_second_in"] == shared_ip and r["nonprod_in"] == shared_ip,
              "The zone is associated with prod-main, prod-second, nonprod-main and shared-main. The VPC resolver of an associated VPC answers from the zone, whatever the region.")
    sc.expect("a VPC that is not associated, the legacy one and the data center's, does not get an answer from its own resolver", r["legacy_in"] in ("", "(no answer)") and r["dc_vpc_in"] in ("", "(no answer)"),
              "Naming is per VPC. Routing to the registry can work and the name still not resolve.")
    sc.expect("the data center's own DNS server resolves it, by forwarding to the inbound resolver endpoint", r["dc_dns_in"] == shared_ip,
              "The data center's server forwards the zone ds05.internal to the two addresses of the inbound endpoint, which asks the VPC resolver of the shared VPC.")
    code, log = run_host(sc, "hybrid-second", f"grep -E 'forwarded {name_in}|query.A. {name_in}' /var/log/dnsmasq.log | tail -2", display="[hybrid-second] the data center DNS server's log for that lookup")
    sc.expect("its log shows the question going to the inbound endpoint's addresses", any(ip in log for ip in dns["inbound_ips"]), "The addresses in the log are the endpoint's two network interfaces, in the shared VPC, reached across the core network.")
    # the other direction
    rows.clear()
    r["pod_main_dc"] = ask(f"pod in {MAIN}      {name_dc}", ("pod", "main", "shop"), f"dig +short {name_dc}")
    r["pod_second_dc"] = ask(f"pod in {SECOND}   {name_dc}", ("pod", "second", "shop"), f"dig +short {name_dc}")
    r["shared_dc"] = ask(f"shared host      {name_dc}", ("host", "shared-main"), f"dig +short {name_dc}")
    r["nonprod_dc"] = ask(f"nonprod host     {name_dc}", ("host", "nonprod-main"), f"dig +short {name_dc}")
    show(sc, f"dig +short {name_dc}   # a name only the data center knows, from four places", "\n".join(f"{l:50s} {a}" for l, a in rows))
    sc.expect("pods in both regions and the shared host resolve the data center's name through a forwarding rule", r["pod_main_dc"] == dc_ip and r["pod_second_dc"] == dc_ip and r["shared_dc"] == dc_ip,
              "A resolver rule for corp.dc.test forwards to the data center's DNS server through an outbound endpoint. There is one endpoint and one rule in each region, because rules are regional.")
    sc.expect("a VPC that the rule is not associated with does not resolve it", r["nonprod_dc"] in ("", "(no answer)"), "The rule is associated with prod-main, prod-second and shared-main, and with no other VPC.")
    code, log = run_host(sc, "hybrid-second", f"grep 'query.A. {name_dc}' /var/log/dnsmasq.log | tail -3", display="[hybrid-second] the data center DNS server's log: who asked?")
    out_ips = [i["Ip"] for ep in awsj("route53resolver", "list-resolver-endpoints", region=MAIN)["ResolverEndpoints"] if ep["Direction"] == "OUTBOUND"
               for i in awsj("route53resolver", "list-resolver-endpoint-ip-addresses", "--resolver-endpoint-id", ep["Id"], region=MAIN)["IpAddresses"]]
    out_ips += [i["Ip"] for ep in awsj("route53resolver", "list-resolver-endpoints", region=SECOND)["ResolverEndpoints"] if ep["Direction"] == "OUTBOUND"
                for i in awsj("route53resolver", "list-resolver-endpoint-ip-addresses", "--resolver-endpoint-id", ep["Id"], region=SECOND)["IpAddresses"]]
    show(sc, "aws route53resolver list-resolver-endpoint-ip-addresses   # the outbound endpoints' addresses", ", ".join(sorted(out_ips)))
    sc.expect("the questions came from the outbound endpoints' addresses, not from the pods or the hosts", bool(re.findall(r"from ([\d.]+)", log)) and all(ip in out_ips for ip in re.findall(r"from ([\d.]+)", log)),
              "The data center sees one small set of addresses, which is what its firewall rules and its DNS logs can be written against.")
    # the cache and the search list
    rc, ds = kube("main", "-n", "kube-system", "get", "daemonset", "node-local-dns", "-o", "jsonpath={.status.desiredNumberScheduled} {.status.numberReady}")
    nodes_n = len(kj("main", "get", "nodes")["items"])
    show(sc, "kubectl -n kube-system get daemonset node-local-dns   # one cache on every node", f"desired / ready: {ds}   nodes: {nodes_n}")
    d, rdy = [int(x) for x in ds.split()]
    sc.expect("a DNS cache runs on every node", d == nodes_n == rdy, "A pod's resolver address, the cluster DNS service, is answered by the cache on its own node. A lookup that is cached never leaves the node.")
    code, rs = pod("main", "shop", "cat /etc/resolv.conf")
    show(sc, "[main/shop] cat /etc/resolv.conf", rs)

    def questions(run):
        """Count the DNS questions a pod sends for one lookup, by listening on the pod's own network interface while it looks the name up."""
        script = ("(timeout 6 tcpdump -n -i any udp dst port 53 > /tmp/q.txt 2>/dev/null &) ; sleep 1.5; " + run + " >/dev/null 2>&1; sleep 1.5; pkill tcpdump; sleep 0.5; grep -o ' A? [^ ]*' /tmp/q.txt | sort -u | wc -l")
        return script
    def n_questions(run, where):
        if where == "shop":
            rc, out = pod("main", "shop", questions(run))
        else:
            rc, out = kube("main", "-n", "dev", "exec", where, "--", "sh", "-c", questions(run))
        return int(out.strip().splitlines()[-1])
    n_default = n_questions(f"getent ahostsv4 {name_dc}", "shop")
    n_dot = n_questions(f"getent ahostsv4 {name_dc}.", "shop")
    show(sc, "tcpdump in the pod while it looks up a name   # how many DNS questions does one lookup send?",
         f"getent ahostsv4 {name_dc}    -> {n_default} questions\ngetent ahostsv4 {name_dc}.   -> {n_dot} question (the name written with its final dot)")
    kube("main", "-n", "dev", "delete", "pod", "ndots1", "--ignore-not-found", "--wait=true")
    apply_yaml("main", {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "ndots1", "namespace": "dev"},
                        "spec": {"dnsConfig": {"options": [{"name": "ndots", "value": "1"}]}, "containers": [{"name": "c", "image": "nicolaka/netshoot:v0.13", "command": ["sleep", "600"]}]}})
    kube("main", "-n", "dev", "wait", "--for=condition=Ready", "pod/ndots1", "--timeout=120s")
    n_ndots = n_questions(f"getent ahostsv4 {name_dc}", "ndots1")
    kube("main", "-n", "dev", "delete", "pod", "ndots1", "--wait=false")
    show(sc, "the same lookup from a pod with ndots 1 in its dnsConfig", f"{n_ndots} question")
    sc.expect("with the default search list one lookup of a name with two dots costs five questions; ndots 1, or a trailing dot, costs one", n_default == 5 and n_dot == 1 and n_ndots == 1,
              "A name with fewer dots than ndots (5) is tried first with every search domain added: three cluster domains and the VPC's own, all of which answer 'no such name', and only then as written. For a workload that mostly calls external names, lowering ndots removes four of five questions.")
    return sc


SCENARIOS = {"S01": s01_address_plan, "S02": s02_core_network, "S03": s03_reachability, "S04": s04_pod_addresses, "S05": s05_address_exhaustion, "S06": s06_lattice, "S07": s07_load_balancing, "S08": s08_isolation, "S09": s09_dns}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_dir = ROOT / "evidence" / "final"
    if "--out" in sys.argv:
        out_dir = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
        args = [a for a in args if a != str(out_dir)]
    wanted = list(SCENARIOS) if not args or args == ["all"] else args
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for key in wanted:
        print(f"== {key} ...", flush=True)
        try:
            sc = SCENARIOS[key]()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            sc = Scenario.last if Scenario.last is not None and Scenario.last.sid == key else Scenario(key, "crashed", "", str(exc))
            sc.expect("scenario ran to completion", False, f"{type(exc).__name__}: {exc}")
        (out_dir / f"{key}.txt").write_text(sc.transcript() + "\n")
        (out_dir / f"{key}.json").write_text(json.dumps(sc.to_json(), indent=1))
        blocks = []
        for s in sc.steps:
            blocks += [("cmd", s.display), ("out", s.output)]
        blocks += [("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}") for c in sc.checks]
        render_png(f"{sc.sid}  {sc.title}", blocks, out_dir / f"{key}.png", max_lines=80)
        summary.append({"id": key, "title": sc.title, "passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed]})
        print(f"   {'PASS' if sc.ok else 'FAIL'}  ({sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks)", flush=True)
        for c in sc.checks:
            if not c.passed:
                print("   FAILED:", c.description)
        try:
            cleanup_all()
        except Exception:
            pass
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + "\n".join(f"{s['id']}  {'PASS' if s['passed'] else 'FAIL'}  {s['title']}" for s in summary))
    sys.exit(0 if all(s["passed"] for s in summary) else 1)


if __name__ == "__main__":
    main()

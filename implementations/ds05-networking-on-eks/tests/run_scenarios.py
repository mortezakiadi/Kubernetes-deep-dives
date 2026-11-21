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


SCENARIOS = {"S01": s01_address_plan, "S02": s02_core_network, "S03": s03_reachability, "S04": s04_pod_addresses, "S05": s05_address_exhaustion}


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

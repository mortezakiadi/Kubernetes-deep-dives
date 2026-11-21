"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""
PROSE = {
    "S01": {
        "intro": "Before anything routes, the design needs addresses nobody can duplicate. I read the plan back from IPAM, check that every routed VPC took its range from it, ask it for a range that is already taken, and look at what it says about the two ranges that are not in the plan.",
        "trust": [
            "Every table comes from IPAM's own API: its pools, their ranges, and its resource discovery, which scans every VPC in the account, not only those it allocated.",
            "The refusal is IPAM's own error message for a real request that I made, not something I computed.",
            "The free allocation is made and released through the same API. I do not choose the range: the pool does.",
        ],
        "why": [
            "Each VPC was given a pool and a netmask length, so the pool picked the block. That is why six of the VPCs are managed and compliant.",
            "The refusal comes from the pool: the block of the main production pool is one /16, and the VPC holds all of it.",
            "The legacy VPC is reported as unmanaged and overlapping because discovery compares every range in the account with every other.",
            "The pod range is reported the same way for the other reason: it is outside the plan by design and appears in both cluster VPCs.",
        ],
    },
    "S02": {
        "intro": "One policy defines the core network. I read it back from the service: the edges, the attachments with the segment each landed in, and the routing table of each segment, which says what the policy says.",
        "trust": [
            "The core network, the attachments and the route tables come from the Network Manager API, not from my policy file.",
            "The segment of each attachment is read next to the tag that put it there, so a mismatch would show.",
            "The routing tables are read at one edge. A range appears in a segment's table only if the policy shares it into that segment.",
        ],
        "why": [
            "An attachment policy rule reads the Segment tag and uses its value as the segment name. That is why the two columns agree.",
            "Production sees shared and hybrid because the policy shares them with it. It does not see non-production because nothing shares non-production with production.",
            "Non-production sees shared, but not production: sharing is not transitive, so a segment sees the segments it shares with and no further.",
            "The sandbox table is empty because its attachments are isolated from each other and nothing is shared with it.",
            "The pod range is in the production table because the core network takes every range of an attached VPC, including a secondary one.",
        ],
    },
    "S03": {
        "intro": "The question the design wants one answer to: can A reach B? I ask it for every pair of the five test hosts, then for the production load balancer from each host, and then measure how long the core network takes to carry a call and how large a packet it carries.",
        "trust": [
            "Every cell is a real request from one host to another, made through Systems Manager, with a three-second limit. A pair is open if the other host's web service answers with its own name.",
            "The load balancer test asks for its health page from each host and reads the HTTP status.",
            "The round trip is ping's own average. The packet size test sets the do-not-fragment bit, so a packet that does not fit is refused and not split.",
        ],
        "why": [
            "Four paths are open because two shares exist: shared with non-production and shared with hybrid, each in both directions.",
            "Non-production and hybrid cannot reach each other because neither is shared with the other. Sandbox and legacy reach nothing: one is isolated, the other is not attached.",
            "The load balancer answers the hosts whose segment shares routes with production, and no others, because the test also depends on the callers having a route to the load balancer's address.",
            "A call to the other region takes about seventy milliseconds because that is the distance between the two regions.",
            "The largest packet is 8500 bytes because that is the core network's MTU. The router at the VPC's edge says so in its reply.",
        ],
    },
    "S04": {
        "intro": "Pods have addresses of their own. I show where they come from, that both clusters use the same range, that a pod's traffic leaves its VPC under its node's address, how the node holds its addresses, and then try to reach a pod from another VPC, by opening one layer at a time.",
        "trust": [
            "The pod and node addresses come from the Kubernetes API. The node's interfaces and /28 blocks come from the EC2 API.",
            "The source address that the other host saw is read from that host's own web log, in the line for the request I made.",
            "The route, security group and reachability steps use real changes to a route table and a security group, each undone in a final step whether the test passed or not.",
        ],
        "why": [
            "Pods use the second range because each zone has an ENIConfig that names a pod subnet, and the CNI uses it for pod interfaces. The node's own interface stays in the node subnet.",
            "The other host saw the node's address because the CNI translates a pod's source address when traffic leaves the VPC.",
            "The node holds /28 blocks because prefix delegation is on. The kubelet's pod limit of 110 comes from the node template.",
            "The pod is unreachable from another VPC because two layers each refuse: the other VPC has no route to the pod range, and the pods' security group admits the cluster only. Each alone is enough, so each alone fails to open the path.",
        ],
    },
    "S05": {
        "intro": "The design says pod ranges are sized from the plan and that pods stuck pending are the symptom of running out. I make it happen on purpose: a node group whose pods use a /26 subnet, and sixty pods pinned to it.",
        "trust": [
            "The counts of running and stuck pods are the cluster's own. The reason is the text of the pods' events.",
            "The four /28 blocks are computed from the subnet's range, the addresses AWS reserves in a subnet, and the prefixes and addresses that EC2 reports on its interfaces.",
            "The final step removes the pin and shows the same pods running, so that the workload is not what was wrong.",
        ],
        "why": [
            "A node takes /28 blocks from its pod subnet. A /26 holds four blocks, and two of them contain an address that AWS reserves.",
            "The pods that did not get an address stay in container creation, because the scheduler counts a node's pod limit and not its addresses.",
            "EC2 still reported free addresses because it counts addresses and not blocks. The alert that fits prefix delegation is on the pods' events or the CNI's metrics.",
            "The same pods run on the other nodes because their pod subnets are /18s from the plan.",
        ],
    },
    "S06": {
        "intro": "Services are the contract, not addresses. I export one through VPC Lattice and call it by name from a VPC whose range is the same as the cluster's, with no route between them. Then I show who is allowed to call it, and that granting and withdrawing are changes to one object in the cluster.",
        "trust": [
            "The route, the service and the associations come from the Kubernetes API and the Lattice API.",
            "Each call is a real request from a host through Systems Manager, signed with the host's own instance role where the test says so.",
            "The answer of a successful call is the web service's own reply, which includes the name of the pod, the Lattice address that called it, and the identity that Lattice verified.",
            "The grant and the withdrawal are made by editing the policy object, and the test waits and repeats the call until the answer changes, then prints how long it took.",
        ],
        "why": [
            "The legacy host can call the service because it is associated with the service network and its role is named in the service's policy. It needs no route.",
            "The call without credentials fails because the service's authentication type is IAM.",
            "The non-production host is refused because its role is not named in the service's policy. It is allowed to call Lattice services in general, and the service's owner has not allowed it.",
            "A ping to the cluster's range goes nowhere from the legacy VPC because it has no route to the core network, and the same range would lead to itself.",
        ],
    },
    "S07": {
        "intro": "Each layer of load balancing has its own health signal. I follow a request: the name that answers with the nearest region, a load balancer that sends to pod addresses, a region that is dropped when its pods are gone, a service that prefers the caller's zone, and then pods that come and go while calls continue.",
        "trust": [
            "The addresses in the DNS answers are compared with the ranges of the two regions.",
            "The target list is the load balancer's own: the type, the address, the zone and the state of each target.",
            "The flip of the answer is observed by asking again every few seconds until it changes, and the time is printed.",
            "The zone of each answer is found from the pod's name, which the web service returns, and the pod's node, which the cluster knows.",
            "The error counts are the status codes that curl got from the load balancer in a loop on a host, written to a file and counted afterwards.",
        ],
        "why": [
            "The name answers differently because two latency-routed alias records share it. Route 53 chooses by the region of the VPC that asked.",
            "A region is dropped because the alias records evaluate the load balancer's target health, and a load balancer with no healthy target is not eligible.",
            "A Service that prefers close keeps calls in the zone because the endpoints carry zone hints. With no ready endpoint in the zone, it uses the others.",
            "Adding pods does not fail calls because a pod is sent traffic only when the load balancer reports its address healthy. Removing a pod did, in a few requests in the first seconds, and I could not remove it with the settings I know of. A caller that retries does not see it.",
        ],
    },
    "S08": {
        "intro": "An audit asks for proof that a pod in development cannot reach the production payments service. I try the paths that must fail, add the guardrails one at a time, show what a team's own policy can and cannot change, and then do the same for the names a pod may reach on the internet.",
        "trust": [
            "Every cell is a real request from the probe pod of a namespace, with a three-second limit. A path is open only if the request returns HTTP 200.",
            "The metadata service is tested by asking for a session token, the first step of anything that wants a node's credentials.",
            "Each policy is created, tested and deleted by the scenario, in a final step that runs whether the test passed or not.",
            "The domain test uses two real domains on the internet and reads the cache that the CNI keeps of what each name resolved to.",
        ],
        "why": [
            "With no policy everything is open, including the metadata service, because a cluster allows what no policy forbids.",
            "The guardrails close the two paths because Admin-tier policies are evaluated before any namespace's policy, and a Deny ends the evaluation.",
            "The payments team cannot open the path for development because its NetworkPolicy is in the tier below the guardrail.",
            "A Baseline deny can be overridden when it names one namespace and the owning team writes a NetworkPolicy. With every namespace as the peer, I could not override it.",
            "The load balancer's traffic is not cut because a namespace selector matches pods. The load balancer's address is in the VPC, and the team's policy admits the VPC's range for it.",
            "The one domain is open because Cilium watches the pod's DNS answers and allows only the addresses that the allowed name resolved to.",
        ],
    },
    "S09": {
        "intro": "Routing worked and naming did not: that is the design's story. I resolve one name from every kind of place, in both directions between AWS and the data center, and then count what a pod's resolver does for one lookup.",
        "trust": [
            "Every answer is what dig printed for a real lookup, from the place named in the line.",
            "The data center's log lines come from the server's own query log.",
            "The outbound endpoints' addresses come from the resolver API, and the log is checked against them.",
            "The number of questions is counted by listening on the pod's own network interface while it looks the name up, and counting the distinct names asked.",
        ],
        "why": [
            "The private zone answers in both regions because it is associated with each VPC of the platform. The data center's VPC and the legacy VPC are not associated, so their own resolvers do not know it.",
            "The data center's server resolves the zone because it forwards that domain to the inbound endpoint.",
            "AWS resolves the data center's domain because a rule forwards it through an outbound endpoint, so the data center sees queries from the endpoint's addresses.",
            "A lookup of a name with two dots takes five questions because the name has fewer dots than the resolver's ndots setting, so it is tried with each search domain first.",
        ],
    },
    "S10": {
        "intro": "The monthly bill is made of data transfer. I measure two of its parts: what a NAT gateway carries when a pod reads from S3, with and without the free gateway endpoint, and how many bytes between pods cross a zone boundary, with and without a Service that prefers the caller's zone.",
        "trust": [
            "The NAT gateway's bytes are CloudWatch's own counter for that gateway, summed over the minutes of each download, which the test waits for.",
            "The bytes between pods are the sums of the VPC flow log's records written on the sending pods' side, by the zone of the sender. The zone is found from the pod's node.",
            "The test creates the bucket and the flow log, and deletes them at the end whether it passed or not.",
        ],
        "why": [
            "S3 traffic stays off the NAT gateway when the endpoint's route is in place because the route to the S3 prefix list is more specific than the default route.",
            "With the route removed, the same bytes leave through the NAT gateway, which charges for every gigabyte it processes.",
            "A plain Service sends a third of the bytes to a pod in the caller's zone and the rest elsewhere, because it spreads the calls over all ready pods.",
            "A Service that prefers close keeps the calls in the zone, so nearly no bytes cross a boundary.",
        ],
    },
    "S11": {
        "intro": "Before launch the network and platform teams walk through what breaks. I do four of those things to the network: a change to the core network's policy that cuts a path and is then rolled back, a zone that disappears under load, the node-local DNS cache that stops, and the data center's DNS server that goes down.",
        "trust": [
            "The preview of the policy change is the service's own change set. The path test is a real request from a host, repeated until its answer changes.",
            "The calls during the zone loss are made in a loop on a host to the load balancer, with the status of each failed call written with its time.",
            "The lookups during the cache loss are made in a loop in a pod, one every tenth of a second, and each is counted as it succeeds or fails.",
            "The status of each DNS lookup is what dig printed.",
        ],
        "why": [
            "A change to the policy is a new version, which is turned into a change set and applied only when someone executes it. A rollback restores an older version as a new one.",
            "The path closes after the change because the share that carried it is gone from the policy.",
            "A zone's loss is survived because the load balancer's health check removes the pods that are not answering, and Kubernetes schedules the pods again in the zones that still have nodes.",
            "The cache's loss is survived because the same cluster DNS address is answered by CoreDNS when the cache's rules are gone.",
            "The data center's DNS failure is confined to its domain because the forwarding rule matches that domain only.",
        ],
    },
}

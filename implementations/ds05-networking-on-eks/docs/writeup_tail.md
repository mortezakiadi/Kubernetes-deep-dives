### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 05 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **A second cloud and AWS Interconnect to it** | I have no second cloud account here. The `hybrid` segment is built and its attachment is tested (S03), but its far side is an AWS VPC that stands in for the data center, not a Google Cloud network. Nothing about cross-cloud bandwidth, price or MTU is tested. |
| **Direct Connect and hybrid nodes** | A physical connection and a rack are needed. The stand-in VPC is attached to the core network like a data center would be, but it has no BGP session, no hybrid node and no Cilium with a routable pod range on its side. |
| **Network Firewall inspection between segments, and central egress** | The policy supports an inspection VPC. I left it out because of cost and time. The four ways a pod's traffic can leave the cluster (the core network, Lattice, the NAT gateway, an AWS endpoint) are tested; filtering them centrally is not. |
| **Cilium on the main cluster, and Cilium Cluster Mesh** | Cilium is chained behind the VPC CNI on the second cluster only, to show the two-CNI fleet and a policy written with names (S08). I did not test Cluster Mesh, which needs non-overlapping pod ranges between clusters. |
| **IPv6 pods** | The design prefers them for new clusters. I used the IPv4 pod range, because the finding I wanted (reusing one range everywhere, S04) is an IPv4 one. |
| **Security groups for pods** | I did not test whether they work with custom networking. The pods share one security group per cluster (S04). |
| **Istio ambient and mesh traffic between clusters** | The design says only where workload identity is required and calls the multi-cluster mode beta. VPC Lattice covers the cross-VPC path here. |
| **A third region, twenty-two clusters and a migration from Transit Gateway** | Two regions are enough to show the mechanism. The migration is the design's phase 2 and is a change plan, not a mechanism. |
| **Route 53 Profiles** | I associated the private zone and the resolver rules with each VPC directly. A Profile shares the same settings with many VPCs and accounts in one step. I did not build it. |
| **Public traffic: Global Accelerator, public health checks, public load balancers** | The names and load balancers here are internal. Route 53's public health checkers cannot reach a private address, so the record follows the load balancer's own target health instead (S07). Latency routing in a private zone answers by the region of the asking VPC. |
| **ECR interface endpoints and a pull-through cache** | S10 measures the S3 gateway endpoint. The interface endpoints cost money while they exist, and a pull-through cache needs a registry to pull from. Both are the same kind of change as the one measured. |
| **A real failure of a zone's network, a region or a core network edge** | S11 removes a zone's nodes, which is what a zone loss does to the cluster. It does not remove a zone's subnets or routing, and it cannot fail an edge of the managed core network. |
| **VPC peering for bulk data** | The design adds one between a training cluster and its data lake. It is one more route between two VPCs and I did not build it. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with `boto3` and Pillow for the tests and screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. The account needs room for four more VPCs in the main region and three in the second, and the Systems Manager and VPC Lattice services enabled.

**Build.**

```text
cd implementations/ds05-networking-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public address (both git-ignored), then builds the three layers in order: the network, the clusters, and what runs on them. It takes roughly an hour, most of it the core network. If your public address changes while it exists, run `scripts/update-my-ip.sh` and apply the cluster layer again, or your machine loses the clusters' API.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The scenarios change the network on purpose: S11 applies two policy changes to the core network, removes a zone's nodes and stops a DNS server. Expect about two hours.

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` removes the platform layer, the clusters and then the network, and then runs `verify-clean.sh`, which looks for anything tagged with this project in both regions: clusters, instances, NAT gateways, addresses, endpoints, VPCs, security groups, load balancers, volumes, network interfaces, resolver endpoints, Lattice networks, log groups, address managers, core and global networks, private zones, buckets and roles. It prints `CLEAN` when nothing is left.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- VPC, IP Address Manager, Pools: the tree of nine pools and the allocations in each, and Resource discovery with the legacy VPC flagged as overlapping.
- Network Manager, Cloud WAN: the core network, its policy version and change sets, the segments and the attachments with the segment each one landed in. The Routes tab of a segment shows what S02 prints.
- EKS, Clusters: `ds05-networking-main` and `ds05-networking-second`, and their Add-ons tab with the VPC CNI configuration.
- VPC, Subnets: the pod subnets and the one `/26` used in S05.
- EC2, Load balancers: the two internal load balancers and their target groups, with the pods' addresses as targets.
- VPC Lattice: the service network, its VPC associations and the `web-shop` service with its auth policy.
- Route 53, Hosted zones and Resolver: the private zone with its two latency records, and the inbound and outbound endpoints and rules.
- CloudWatch, Metrics, NATGateway: the bytes of the two downloads in S10.

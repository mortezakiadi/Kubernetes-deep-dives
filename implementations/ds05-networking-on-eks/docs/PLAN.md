# Design 05 build plan (working notes, not part of the appendix)

Appendix: `content/appendix/a06-design-05-networking.md`, "Appendix F". Folder: `implementations/ds05-networking-on-eks`.

## Research findings so far (verified from AWS docs, session of 2026-11-20)
- EKS Auto Mode supports network policies once the network policy controller is switched on with a ConfigMap `amazon-vpc-cni` in kube-system (`enable-network-policy-controller: "true"`). It also supports ClusterNetworkPolicy and ApplicationNetworkPolicy (FQDN rules, Auto Mode nodes only).
- **The ClusterNetworkPolicy API on EKS is `networking.k8s.aws/v1alpha1`, not the upstream `policy.networking.k8s.io/v1alpha2` that the design's YAML uses.** Actions are Accept/Deny/Pass. Evaluation: Admin tier (Deny, then Accept, Pass delegates) -> NetworkPolicy/ApplicationNetworkPolicy -> Baseline tier -> default deny.
- Auto Mode does NOT support ENIConfig custom networking. It supports pod subnets through the NodeClass (`podSubnetSelectorTerms`), prefix delegation by default, SNAT policy, and security groups for pods (SecurityGroupPolicy) on Kubernetes 1.36+. No Cilium or other CNI on Auto Mode.
- Cloud WAN policy JSON: `core-network-configuration` (asn-ranges, edge-locations), `segments` (`isolate-attachments`, `require-attachment-acceptance`), `segment-actions` (`share`, mode `attachment-route`), `attachment-policies` (tag-based `association-method: tag` with `tag-value-of-key`).
- AWS Gateway API Controller (VPC Lattice): helm chart `oci://public.ecr.aws/aws-application-networking-k8s/aws-gateway-controller-chart`, docs at gateway-api-controller.eks.aws.dev (v2.0.x). Needs Gateway API CRDs and a Lattice service network.

## Scope (scaled down)
Regions us-east-1 (primary) and us-west-2.
- IPAM with a top-level pool, one pool per region and environment; every VPC takes its CIDR from a pool. Show an overlap is refused.
- Cloud WAN core network, two edges, segments prod / nonprod / sandbox / shared / hybrid, policy as code, change-set preview and rollback.
- VPCs: prod-east (EKS Auto Mode), prod-west (EKS Auto Mode), nonprod-east (test host), shared-east (test host + private zone), hybrid "datacenter" in us-west-2 (stand-in for on-premises: test host + dnsmasq), legacy-east (overlapping CIDR, NOT on Cloud WAN, for Lattice).
- Pods on a 100.64 secondary range through pod subnets (Auto Mode), prefix delegation, IP exhaustion test.
- Lattice service network with the Gateway API controller; a service called by name from the overlapping legacy VPC.
- Load balancing in layers: Route 53 latency/health-checked records, ALB in IP mode via Auto Mode, topology-aware Service routing, health, draining.
- Isolation: segments, security groups, ClusterNetworkPolicy tiers, NetworkPolicy, FQDN ApplicationNetworkPolicy, IMDS block, denial tests.
- DNS: private hosted zone, Resolver inbound/outbound endpoints and forwarding between the datacenter stand-in and AWS, node-local caching (Auto Mode does it on the node), ndots.
- Cost engineering: S3 gateway and interface endpoints vs NAT, flow logs by zone, ECR pull-through cache.
- Failure drills: bad Cloud WAN change and rollback, pod IP exhaustion, DNS resolver failure, zone loss as far as it can be tested.

## Not buildable (to be disclosed)
Google Cloud and AWS Interconnect multicloud (no second cloud), Direct Connect (physical), hybrid nodes (no racks), Cilium chaining (Auto Mode refuses other CNIs), Istio ambient multicluster, 22 clusters, Network Firewall inspection (decide later; cost), Global Accelerator (decide later).

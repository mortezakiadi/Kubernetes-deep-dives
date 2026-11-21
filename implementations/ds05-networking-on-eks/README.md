# Design 05 on AWS: the networking layer across clusters, regions and a data center

A working, scaled-down implementation of [Design 05](../../content/designs/ds05-networking.md): one address plan in IPAM, one core
network (AWS Cloud WAN) with five segments, two standard EKS clusters in two regions whose pods use their own address range, VPC Lattice
for services that cross VPCs, internal load balancers with DNS that follows their health, cluster-wide network policy in two tiers, and
DNS that works in both directions between AWS and a stand-in for the data center.

The write-up, with every command, its real output, screenshots and diagrams, is
[Appendix F](../../content/appendix/a06-design-05-networking.md). This file only says how to run it.

## What is here

| Folder | What it holds |
|---|---|
| `terraform/10-network` | IPAM and its pools, seven VPCs from one module, the core network and its policy, the private DNS zone and resolver endpoints, five test hosts and the data center's DNS server. |
| `terraform/20-clusters` | Two EKS clusters (standard, not Auto Mode), the VPC CNI settings, one ENIConfig per zone, managed node groups. |
| `terraform/30-platform` | On each cluster: the load balancer controller, the node-local DNS cache, test applications and an Ingress. On the second: Cilium. On the first: the Gateway API controller for VPC Lattice, an exported service and its auth policy. The latency records for the global name. |
| `tests/` | The scenario runner. It runs real commands, records their real output, redacts private values and draws terminal-style screenshots. |
| `docs/` | The generator for the appendix, the hand-written prose, and the diagram generator. |
| `scripts/` | `up.sh`, `down.sh`, `verify-clean.sh`, `update-my-ip.sh`, `check-no-secrets.sh`, `setup-tools.sh`. |
| `evidence/final/` | The recorded run the appendix is built from (redacted). |

## Run it

You need Terraform 1.9 or later, the AWS CLI, Helm 3, Python 3.12 with `boto3` and `Pillow`, and credentials for the AWS account you
want to use. The account needs room for four more VPCs in us-east-1 and three in eu-west-1. Nothing in this folder contains an account
ID, a credential or an address: `up.sh` writes them into git-ignored files.

```bash
export AWS_PROFILE=<your profile>
./scripts/up.sh                              # about an hour; the core network takes most of it
python3 tests/run_scenarios.py all           # builds the evidence; changes the network on purpose
./scripts/down.sh --yes                      # removes everything, then checks nothing is left
```

If your public address changes while the clusters exist, run `scripts/update-my-ip.sh` and apply `terraform/20-clusters` again.

## What is real and what is a stand-in

Real: IPAM, the core network and its policy, the VPC CNI settings, Cilium, the load balancer and Lattice controllers, the Route 53
zone, endpoints and rules. Stand-ins: the data center (a VPC with a host that runs a DNS server) and the legacy network (a plain VPC).
There is no second cloud.

## Cost

While it exists: two EKS control planes, seven small EC2 hosts, seven NAT-free VPCs and two with a NAT gateway, two Application Load
Balancers, five DNS resolver endpoints, a Cloud WAN core network with two edges and six attachments, and a VPC Lattice service network.
Plan on a few dollars an hour. The core network and the resolver endpoints are most of it.

## Before you push anything

`scripts/check-no-secrets.sh --all` must print `OK`. This repository is public.

---
id: d-challenges-2
title: "Troubleshooting networking"
short: "Challenges 2"
---
Three tickets arrive in one morning: "pods stuck Pending", "service unreachable" and "everything is slow when calling external APIs". This deep dive is the runbook for all three.

### IP exhaustion

**Scenario.** New pods stay `Pending` or `ContainerCreating` with "failed to assign an IP address".

![IP exhaustion](diagrams/sections/d11-1.svg)

**What the picture shows.** Start with the `aws-node` logs. Then pick a fix: a bigger subnet (the hard way), prefix delegation (17 → 110 pods on a t3.medium), or custom networking on a secondary CIDR such as 100.64.0.0/16 through ENIConfig.

### Service connectivity drops

**Scenario.** One service can't reach another by its Service IP.

![Service connectivity drops](diagrams/sections/d11-2.svg)

**What the picture shows.**

- Step A: does the Service have endpoints?
- An empty list usually means a selector typo.
- Step B: are the iptables or IPVS rules there?
- Also check readiness probes, NetworkPolicies, and whether kube-proxy is healthy.

### DNS issues

**Scenario.** Calls to external APIs are slow, and CoreDNS CPU usage is high.

![DNS issues](diagrams/sections/d11-3.svg)

**What the picture shows.** Corefile mistakes and forward loops, CoreDNS running out of memory, Services with no endpoints, and **ndots:5**: one external lookup becomes several queries through the search domains. Fix it with fully qualified names (a trailing dot) or a lower `ndots` in `dnsConfig`.

### Latency by distance

**Scenario.** Why does the same call take 0.1 ms, 1 ms or 5 ms?

![Latency by distance](diagrams/sections/d11-4.svg)

**What the picture shows.**

- Same node: memory copy only.
- Same AZ: the top-of-rack switch (0.2–1 ms).
- Across AZs: the inter-AZ backbone (1–5 ms), plus a data transfer charge.

### Bandwidth and MTU

**Scenario.** Three pods on one node want 12 Gbps together through a 10 Gbps ENI.

![Bandwidth and MTU](diagrams/sections/d11-5.svg)

**What the picture shows.** Instance bandwidth caps all pods on a node together ("up to" vs. guaranteed). The VPC supports 9001-byte jumbo frames, but the internet uses 1500, so raising pod MTU helps east-west traffic and breaks internet traffic that has no translation.

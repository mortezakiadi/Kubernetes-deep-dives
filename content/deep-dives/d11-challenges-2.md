---
id: d-challenges-2
title: "Troubleshooting networking"
short: "Challenges 2"
---
Three tickets arrive in one morning: "pods stuck Pending", "service unreachable" and "everything is slow when calling external APIs". This deep dive is the runbook for all three.

### IP exhaustion

**Scenario.** After a successful product launch, new pods across the cluster are stuck in `Pending` or `ContainerCreating`. CPU and memory are fine, but the subnets have run out of free IP addresses. The team needs to find the cause and pick a fix that doesn't require rebuilding the cluster.

![IP exhaustion](diagrams/sections/d11-1.svg)

**What the picture shows.** Runbook 1: IP exhaustion, pods stuck because there are no IPs left.

- **Check the source of truth, the VPC CNI logs:** the `aws-node` pod manages IPs, and its logs show exactly why the call to AWS failed.
- **Fix 1, increase subnet size (the "hard" fix):** you can't expand an existing subnet; you must create a new, larger one and migrate nodes to it. Best for small clusters that were accidentally started on a tiny `/27` or `/26`.
- **Fix 2, prefix delegation (the "efficiency" fix):** run `kubectl set env daemonset aws-node -n kube-system ENABLE_PREFIX_DELEGATION=true`. Pro: pod density jumps immediately (for example 17 → 110 pods on a `t3.medium`). Con: it can fragment a subnet that's already messy.
- **Fix 3, custom networking (the "separation" fix):** attach a secondary CIDR to the VPC (for example `100.64.0.0/16`) and point the CNI at it with an `ENIConfig` resource, so pods get IPs from a separate, much larger range.

### Service connectivity drops

**Scenario.** A pod can't reach a Service: requests hang or are refused, while other pods work fine. The network team insists the VPC is healthy. The on-call engineer needs a step-by-step way to find where the "pipe" between the two pods is broken.

![Service connectivity drops](diagrams/sections/d11-2.svg)

**What the picture shows.** Runbook 2: connectivity drops.

- **What it means:** the path between the source pod and the destination pod is broken somewhere. Services are virtual, so the failure is usually in the translation layer (the switchboard) or in the target list (the endpoints). Common root causes are conntrack table exhaustion and MTU mismatches.
- **Step A, are there any targets?** If the Service's `ENDPOINTS` is `<none>`, it's a dead end, usually because of a typo in the Service's `selector`.
- **Step B, are the rules programmed?** Check whether the node knows how to handle the traffic: you should see rules jumping to a `KUBE-SVC` chain. If they're missing, kube-proxy has probably crashed or is stuck.
- **What to check:**
  - **Selector mismatch (the typo):** the Service doesn't select any pods.
  - **Failing readiness probes:** Kubernetes doesn't send traffic to pods that aren't Ready.
  - **NetworkPolicy blocking:** an installed NetworkPolicy acts as a silent firewall.
  - **kube-proxy failure:** if kube-proxy dies, the iptables or IPVS rules on that node stop updating.

### DNS issues

**Scenario.** Users report that "the network is down": pods can't reach other services or external APIs. But ping by IP works fine. Meanwhile, an AI service calling an external LLM API is mysteriously slow on every request. Both problems turn out to be DNS.

![DNS issues](diagrams/sections/d11-3.svg)

**What the picture shows.** Runbook 3: DNS issues, where pods can't find the address of who they want to talk to.

- **Why DNS looks like a network outage:** the network is fine, but names don't resolve. In Kubernetes this almost always points to **CoreDNS**, the cluster's internal phonebook: usually 2 pods that answer queries such as "what is the IP of `my-service.default.svc.cluster.local`?"
- **Troubleshooting 1, Corefile misconfiguration:** the Corefile (CoreDNS's config, stored in a ConfigMap) can break the phonebook.
  - **Looping:** if DNS plugins forward to each other, CoreDNS detects a loop and shuts down. Example: a pod asks for `google.com`; CoreDNS forwards using `forward . /etc/resolv.conf`; the node's `resolv.conf` points to a local stub (`127.0.0.1` or `127.0.0.53`), which sends the query back, and the request spirals.
  - **Upstream failure:** that `forward` line normally sends external names to the VPC DNS (`169.254.169.253`). If the node's `resolv.conf` is broken, nothing outside the cluster resolves.
  - **Memory limits:** in large clusters CoreDNS can run out of RAM indexing every Service, get OOM-killed, and all DNS stops.
- **Troubleshooting 2, Service selector mismatch:** a "DNS" problem that's really a labelling problem. `nslookup my-service` returns a valid ClusterIP, but connecting gives "connection refused", because the Service's selector (for example `app: web`) matches no running pods. The DNS record exists but leads to a dead end.
- **Troubleshooting 3, the `ndots` problem (the performance killer):** every pod's `/etc/resolv.conf` has `ndots:5` by default, which says "a name with fewer than 5 dots might be a local service, so try the cluster search paths first". Resolving `google.com` tries `google.com.default.svc.cluster.local`, `google.com.svc.cluster.local`, `google.com.cluster.local` (all fail) and finally `google.com`. One lookup becomes 5 queries, which hammers CoreDNS and adds latency to every call to S3, RDS or an LLM API. That's the slow AI service in the scenario.
- **How to fix `ndots`:**
  - **Method A, the fully qualified trick:** write `google.com.` with a trailing dot.
  - **Method B, change the pod spec:** lower `ndots` in `dnsConfig` for deployments that talk to the outside world a lot.

### Latency by distance

**Scenario.** A team splits a chatty microservice across three Availability Zones for resilience, and its p99 latency doubles. They want to know how much latency each kind of network hop actually adds.

![Latency by distance](diagrams/sections/d11-4.svg)

**What the picture shows.** Three levels of distance between two pods, and what each one costs.

- **Same-node communication (under 100 µs):** pod A and pod B on one EC2 node talk through their virtual interfaces (`eth0`) and the Linux kernel. There's no physical network at all; data moves through the node's memory and CPU.
- **Cross-node, same AZ (0.2–1 ms):** traffic leaves node 1 through its ENI, passes a top-of-rack switch and enters node 2's ENI. Low latency: physical switches and cables, but still inside one data centre building.
- **Cross-AZ (1–5 ms):** traffic crosses the inter-AZ backbone (high-speed fibre) between geographically separate data centres. Significant latency, including encryption and distance, **plus a data-transfer charge**. That's why the scenario's p99 doubled.

### Bandwidth and MTU

**Scenario.** A data-processing pod on a `t3.medium` is far slower than expected, even though its CPU is barely used. Separately, a team that set its pod MTU to 9001 for faster ML traffic finds that calls to internet APIs now hang.

![Bandwidth and MTU](diagrams/sections/d11-5.svg)

**What the picture shows.** Two hidden network ceilings: instance bandwidth and MTU.

- **Throughput is capped by the instance type:** however strong your CPUs are, the node can only move as much data as its virtual hardware allows. Examples: `t3.medium` up to 5 Gbps, `m5.large` up to 10 Gbps, `m5.4xlarge` up to 10 Gbps, `m5.12xlarge` a steady 25 Gbps.
- **Burstable vs. guaranteed:** "up to" means burstable; sustained transfers fall back to a much lower baseline. That explains the slow `t3.medium` in the scenario.
- **MTU, how data is chopped up:**
  - **VPC default, 9001 bytes (jumbo frames):** works only inside AWS (VPC to VPC, or over Direct Connect). Set for pods with `AWS_VPC_ENI_MTU` in the VPC CNI.
  - **Container default, 1500 bytes:** the standard Ethernet size for the public internet, which most pods use.
  - **The mismatch:** for high-performance ML, some teams set the CNI MTU to 8951 or 9001 for pod-to-pod traffic. But if those pods talk to the internet without translation, oversized packets get dropped and connections hang, which is the second part of the scenario.

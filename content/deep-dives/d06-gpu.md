---
id: d-gpu
title: "How GPUs become schedulable"
short: "EKS and GPU"
---
### The NVIDIA device plugin {#d06-1}

**Scenario.** A new GPU node group joins the cluster, but pods that request `nvidia.com/gpu: 1` stay `Pending`.

![The NVIDIA device plugin](diagrams/sections/d06-1.svg)

**What the picture shows.**

1. The device plugin runs as a DaemonSet.
2. It registers with the kubelet, which advertises GPU capacity.
3. The scheduler can now count GPUs and place the pod.
4. The kubelet asks the plugin to attach specific devices to the container.

In our setup, the plugin is installed by the EKS Terraform module.

> **From the chapter:** device plugins expose *counts*, not *topology*. That's why topology-aware placement needs node labels (or GPU Feature Discovery), Kueue ResourceFlavors, or a scheduler like KAI that reads the GPU fabric. Inside a node, the kubelet's Topology Manager keeps CPUs and GPUs on the same NUMA node.

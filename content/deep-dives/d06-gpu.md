---
id: d-gpu
title: "How GPUs become schedulable"
short: "EKS and GPU"
---
### The NVIDIA device plugin

**Scenario.** A new GPU node group joins the cluster, but pods that request `nvidia.com/gpu: 1` stay `Pending`.

![The NVIDIA device plugin](diagrams/sections/d06-1.svg)

**What the picture shows.**

1. The device plugin runs as a DaemonSet.
2. It registers with the kubelet, which advertises GPU capacity.
3. The scheduler can now count GPUs and place the pod.
4. The kubelet asks the plugin to attach specific devices to the container.

In our setup, the plugin is installed by the EKS Terraform module.

**Going deeper.**

- Two discovery layers come first.
- **Node Feature Discovery** labels basic hardware facts (PCI vendor and class), and **GPU Feature Discovery** adds GPU details (model, memory, family, CUDA version).
- The device plugin then does four jobs:

- it discovers devices;
- it allocates them exclusively to containers;
- it monitors their health;
- it advertises them as `nvidia.com/gpu` for scheduling.

- A minimal vLLM Deployment on such a node still needs a GPU limit, a toleration for the GPU taint, and a PVC as a model cache, so restarts don't re-download the weights.
- Plugins expose counts, not topology.
- That gap is why topology labels, Dynamic Resource Allocation and topology-aware schedulers exist.
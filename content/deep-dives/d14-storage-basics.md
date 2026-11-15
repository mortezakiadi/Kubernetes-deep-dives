---
id: d-storage-basics
title: "StorageClass, PV and PVC in one analogy"
short: "StorageClass, PV and PVC"
---
### The furniture-shop analogy

**Scenario.** A new engineer asks why a pod needs a PVC, a PV *and* a StorageClass just to get a disk.

![StorageClass, PV and PVC](diagrams/sections/d14-1.svg)

**What the picture shows.**

- **StorageClass:** the catalogue of blueprints (gp3, io2, and so on).
- **PVC:** the order slip ("I need a wooden desk").
- **PV:** the desk that actually gets built, which is the real EBS volume, bound to the order.
- **Volume and volumeMount:** where the pod sets the desk up.

The YAML examples show dynamic provisioning end to end. Mountpoint for S3 gives you a file-system view of a bucket; it is still object storage.

**Going deeper.** The same objects serve model weights. One PersistentVolume on NFS or EFS with `ReadOnlyMany` access, mounted `readOnly: true` by every replica, stores the model once and lets the OS cache it aggressively. It works well for tens of replicas. At hundreds, the shared backend becomes the bottleneck.

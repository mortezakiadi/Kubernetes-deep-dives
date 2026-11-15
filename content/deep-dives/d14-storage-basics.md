---
id: d-storage-basics
title: "StorageClass, PV and PVC in one analogy"
short: "StorageClass, PV and PVC"
---
### The furniture-shop analogy

**Scenario.** A new team member keeps mixing up StorageClass, PV and PVC, and asks why a pod needs both a `volumes` and a `volumeMounts` section. A library analogy makes the whole chain easy to remember.

![StorageClass, PV and PVC](diagrams/sections/d14-1.svg)

**What the picture shows.** Dynamic provisioning explained with a library desk analogy.

- **StorageClass, the catalogue of desk designs:** what the library offers, such as a wooden desk or a glass desk. In Kubernetes these are storage tiers like gp3 or io2.
- **PVC, the order slip:** you fill in a reservation for the kind of desk you want; it binds to the desk that gets built and reserves it for you.
- **PV, the actual desk built for you:** the real EBS volume.
- **Dynamic provisioning:** the library builds the desk (the PV) automatically from the chosen design (the StorageClass) when an order slip (the PVC) comes in.
- **Pod, with `volumes` and `volumeMounts`:** now that the desk is reserved, you want to use it. The student shows the reservation slip to the librarian (`volumes` names the PVC), is directed to the desk, and sets up there (`volumeMounts` says where in the container the disk appears).
- **Mountpoint for S3 CSI driver:** a different kind of "desk": it mounts S3 as a file system, not a block device.

**Going deeper.**

- The same objects serve model weights.
- One PersistentVolume on NFS or EFS with `ReadOnlyMany` access, mounted `readOnly: true` by every replica, stores the model once and lets the OS cache it aggressively.
- It works well for tens of replicas.
- At hundreds, the shared backend becomes the bottleneck.

---
id: d-data-path
title: "Storage and the data path: training data, checkpoints and heavy models"
short: "Training data and model loading"
---
**This deep dive covers one problem: how bytes get from storage to the GPUs.** A training job reads terabytes of data and writes large checkpoints. A checkpoint is a saved copy of the training state. A serving pod must fetch the weights of a model before it can answer a request, and weights are the numbers a trained model has learned. In this page, a heavy model is one whose weights take tens or hundreds of GB (this page's reading), and the example uses 140 GB. Reading training data and fetching weights are the same storage and data-path problem. The bytes live somewhere, a Kubernetes object connects them to the pod, they travel some distance, and every hop costs time and sometimes money.

**This deep dive is not specific to AWS.** AWS is the worked example, because AWS publishes numbers and Kubernetes drivers for its storage. EKS is Amazon Elastic Kubernetes Service, the managed Kubernetes service of AWS ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html)). A GPU (graphics processing unit) is the chip that does most of the computing in AI work. A NIC (network interface card) connects a server to the network. A CSI driver is a plug-in that connects a storage system to Kubernetes ([Kubernetes CSI](https://kubernetes-csi.github.io/docs/introduction.html)). A PersistentVolume (PV) is a piece of storage in the cluster. A PersistentVolumeClaim, or claim, is a request for storage by a user ([Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)). A StorageClass describes a class of storage and how to create it ([Kubernetes StorageClass](https://kubernetes.io/docs/concepts/storage/storage-classes/)). A VPC is a virtual network that you define in AWS. A subnet is a range of IP addresses in your VPC, and it sits in one zone ([AWS VPC](https://docs.aws.amazon.com/vpc/latest/userguide/what-is-amazon-vpc.html)).

The first section covers training data and checkpoints. The second covers serving and loading heavy models. Each section opens with one Kubernetes question and keeps only what changes something you do on Kubernetes. The appendix holds the numbers and details that do not change what you do.

Every number has a source linked next to it. *Derived* means arithmetic on sourced numbers, shown in the text. *Illustrative* means picked to teach, not measured. *This page's reading* means the source does not say it in so many words. Where no source was found, the page says "not known from sources". Prices are AWS's, for the example named, with the date read. Words and units are in the Words and units appendix of [Deep dive 15](#d-ai-path-appendix-f-words-and-units). Where the bytes cross zones or Regions, [Zones](#d10-7) and [Regions, the WAN and failover](#d10-8) in Deep dive 10 give the rules and the prices.

**Words that the points use.** Each point names one group of words and defines them with a source. A word is also defined again in the point that first needs it, where that matters.

**Going deeper, by topic.**

- **Words: kubelet and feature gates.** The kubelet is the primary node agent that runs on each node, and it makes sure the containers of a pod are running and healthy ([Kubernetes kubelet](https://kubernetes.io/docs/reference/command-line-tools-reference/kubelet/)). A feature gate is a key and value pair that turns a Kubernetes feature on or off. A feature can be in the Alpha, Beta or GA stage ([Kubernetes feature gates](https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/)).
- **Words: access modes and privileged containers.** ReadOnlyMany means that many nodes can mount a volume read-only. ReadWriteMany means that many nodes can mount it read-write ([Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)). The processes in a privileged container are essentially equivalent to root on the host ([Kubernetes Pod API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/)). CAP_SYS_ADMIN is a Linux capability that the FSx example gives to its container, and the pages read do not explain it further.
- **Words: container images and runtimes.** OCI is the Open Container Initiative, a project that sets open industry standards for container formats and runtimes ([OCI](https://opencontainers.org/about/overview/)). containerd is a container runtime that runs as a daemon on the node. It manages the container life cycle, from image transfer and storage to running the container ([containerd](https://containerd.io/)). A snapshotter is the part of containerd that manages the snapshots of container file systems ([containerd snapshotters](https://github.com/containerd/containerd/blob/main/docs/snapshotters/README.md)).
- **Words: AWS Kubernetes services.** EKS is Amazon Elastic Kubernetes Service ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html)). A managed node group is a group of EC2 nodes that EKS creates and runs in an EC2 Auto Scaling group ([AWS EKS node groups](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html)). Spot Instances are spare EC2 capacity that EC2 can take back with a two-minute notice ([AWS EKS node groups](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html)). Fargate is AWS technology that gives containers compute capacity without servers that you manage ([AWS EKS Fargate](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html)). Karpenter is an open-source project that manages the life of Kubernetes nodes ([Karpenter](https://karpenter.sh/docs/)).
- **Words: AWS machines.** An AMI is an Amazon Machine Image, an image that provides the software needed to set up and boot an EC2 instance ([AWS AMI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/AMIs.html)). A cluster placement group is a grouping of instances within a single zone, and its instances get a higher per-flow throughput limit for TCP/IP traffic ([AWS placement groups](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-strategies.html)). Nitro is the AWS system that moves virtualization work to dedicated hardware and software ([AWS P4](https://aws.amazon.com/ec2/instance-types/p4/)), and "Nitro v4 or higher" names a hardware generation (this page's reading).
- **Words: AWS network and GPU data path.** EFA is the Elastic Fabric Adapter, a network device for EC2 instances that speeds up AI, machine learning and high performance computing (HPC) traffic ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). ENA is the Elastic Network Adapter, the AWS device that gives normal IP networking ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). ENA Express is an ENA feature that adds an advanced path selection and a congestion control mechanism ([AWS FSx EFA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/efa-file-systems.html)). GDS is GPUDirect Storage, a direct data transfer between the file system and the GPU memory that bypasses the CPU ([AWS FSx EFA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/efa-file-systems.html)). `nvidia-fs` is the kernel module that GDS needs ([AWS sample](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre)).
- **Words: AWS storage.** S3 is AWS's object storage service ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html)). EBS is Amazon Elastic Block Store, and an EBS volume is a block-level storage device that you attach to an instance ([AWS EBS](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes.html)). EFS is Amazon Elastic File System, AWS's shared file system ([AWS EKS EFS](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html)). ECR is Amazon Elastic Container Registry, a managed container image registry ([AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html)). NVMe is a fast kind of local disk (this page's reading).
- **Words: S3 bucket types.** A directory bucket is the bucket type that S3 Express One Zone uses. It organizes data in directories, while a general purpose bucket has a flat storage structure ([AWS S3 directory buckets](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-overview.html), [AWS S3 Express](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-express-one-zone.html)).
- **Words: model serving.** vLLM is an open-source model server, a program that loads a model and answers requests ([Post 01](#p01-2)).
- **Words: FSx for Lustre deployment types.** A deployment type is how FSx runs the file servers. Scratch does not replicate data and loses it if a file server fails. Persistent 1 and Persistent 2 are the two persistent types, and Persistent 2 is the latest generation, made for longer-term storage and the highest IOPS and throughput ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)).
- **Words: FSx for Lustre storage classes.** SSD, HDD and Intelligent-Tiering are storage classes. Persistent 2 supports SSD and Intelligent-Tiering ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)). The throughput of an SSD or HDD file system is proportional to its capacity ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)).
- **Words: init container.** An init container runs to completion before the app containers of the pod start ([Kubernetes init containers](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/)).
- **Words: readiness and startup probes.** A readiness probe tells Kubernetes whether a pod can take traffic. If it fails, the pod is marked unready and no Service sends it traffic. A startup probe protects a slow-starting container ([Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)).
- **Words: image volume.** An image volume is a read-only volume whose files come from a container image ([Kubernetes v1.33 post](https://kubernetes.io/blog/2025/04/29/kubernetes-v1-33-image-volume-beta/)).

### Training data and checkpoints: which storage path, and how it reaches the pods

**The Kubernetes question.** Many training pods read one large dataset and write large checkpoints at the same time. Which storage can serve that, which Kubernetes objects mount it, and what does each path cost in speed, money and risk?

**Scenario.** This scenario is illustrative. A team starts a job on 8 GPU nodes. Every pod reads the same dataset, and every rank writes its own checkpoint shard. A rank is one GPU taking part in the job ([What a training job asks of the cluster](#d15-2)). The team mounted a bucket as a volume, and the GPUs wait for data. They need to know which storage paths exist, how each one reaches a pod, and what each one gives up.

![Training data and checkpoints: four storage paths](diagrams/sections/d16-1.svg)

**What the picture shows.** Four paths from a training pod to its data, one lane for each. A lane shows the Kubernetes object that mounts the path, the driver, and where the bytes live. The right side of a lane lists three facts and a price line, with the sourced numbers. A fact marked "this page's reading" has no source that states it. Only the first lane has a second variant, S3 Express One Zone. The picture names it in the lane, and the point "Lane 1, the variant S3 Express One Zone" explains it.

- **Lane 1, object storage: what it is.** S3 is AWS's object storage service ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html)). Mountpoint for S3 lets a program read and write S3 objects with ordinary file operations such as open and read ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)). The Mountpoint CSI driver mounts a bucket into a pod ([AWS EKS S3 CSI](https://docs.aws.amazon.com/eks/latest/userguide/s3-csi.html)).
- **Lane 1, how it reaches the pod.** The driver supports only static provisioning, which means you create the volume by hand: a PersistentVolume names an existing bucket. Dynamic provisioning, where a driver creates storage when a claim appears, is not supported ([AWS EKS S3 CSI](https://docs.aws.amazon.com/eks/latest/userguide/s3-csi.html)). The driver is compatible with Kubernetes v1.31 and later ([Mountpoint CSI driver](https://github.com/awslabs/mountpoint-s3-csi-driver)).
- **Lane 1, what it crosses.** The bytes cross the node's NIC to S3. S3 limits the requests for each prefix. A prefix is the start of an object's key, which is the object's name. The limits are in the decision table ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)).
- **Lane 1, the variant S3 Express One Zone.** It is an S3 storage class that keeps data in one zone that you pick. AWS designs it for single-digit millisecond access and 99.95 percent availability, and it does not store data across zones ([AWS S3 Express](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-express-one-zone.html)). It keeps objects in directory buckets, a bucket type with directories. The other type, a general purpose bucket, has a flat storage structure ([AWS S3 directory buckets](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-overview.html)). AWS recommends a zone local to your EKS compute ([AWS S3 Express](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-express-one-zone.html)).
- **Lane 2, FSx for Lustre: what it is.** Amazon FSx for Lustre is AWS's managed file system for fast workloads. It is POSIX-compliant, which means it behaves like a normal Linux file system, and it supports file locking ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)). AWS's EKS guide calls it a parallel file system. It spreads the data of one file over several storage volumes, which [Appendix A](#d-data-path-appendix-a-fsx-for-lustre-numbers) explains ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)).
- **Lane 2, how it reaches the pod.** A StorageClass names the subnet and the type of file system. A claim asks for capacity, and the FSx CSI driver creates the file system ([FSx CSI driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/README.md)). The driver also supports static provisioning, where you create the file system first. The bytes cross the node's NIC to the file servers of the file system.
- **Lane 2, what it crosses.** FSx for Lustre gives each client up to 100 Gbps, and up to 700 Gbps with EFA ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)). The sub-millisecond latency that AWS states holds for the SSD class and for cached data ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)).
- **Lane 3, node-local NVMe.** NVMe is a fast kind of local disk (this page's reading). A p5.48xlarge lists 8 x 3.84 TB of NVMe SSD ([AWS P5](https://aws.amazon.com/ec2/instance-types/p5/)). A local PersistentVolume carries a node affinity, a rule that ties the volume to one node ([Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)). The bytes never leave the node.
- **Lane 3, what it risks.** Data on an instance store persists if the instance is rebooted. It does not persist if the instance is stopped, hibernated or terminated ([AWS EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-store-lifetime.html)).
- **Lane 4, emptyDir.** An emptyDir is scratch space in the pod spec. Kubernetes creates it when the pod is assigned to a node and deletes it when the pod leaves that node ([Kubernetes volumes](https://kubernetes.io/docs/concepts/storage/volumes/)). It uses the disk that holds the kubelet's root directory. The kubelet is the primary node agent that runs on each node ([Kubernetes kubelet](https://kubernetes.io/docs/reference/command-line-tools-reference/kubelet/)).

**Going deeper, by topic.**

- **Choose by what the job does: a decision table.** The table compares the first three lanes on the questions that decide the choice. Lane 4 is scratch space for one pod and is not in the table. None of the pages read compares the paths with one measurement, and AWS advises to benchmark storage with your own workload ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)).

| Question | Object storage with Mountpoint | FSx for Lustre | Node-local NVMe |
|---|---|---|---|
| Does it behave like a normal file system, with rename, edits in place and file locks? | No. It does not emulate `rename` on general purpose buckets, the flat bucket type ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)). It does not support edits to existing files, and replacing a whole file needs the `--allow-overwrite` flag ([Mountpoint README](https://github.com/awslabs/mountpoint-s3), [Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)) | Yes. POSIX-compliant, with file locking ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)) | This page's reading: yes, it is a disk in the node |
| Can many pods on many nodes read the same data? | Yes. Built for high read throughput to large objects from many clients at once ([Mountpoint](https://github.com/awslabs/mountpoint-s3)) | Yes. Access to the same file or directory from thousands of compute instances ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)) | No. A local volume is tied to its node by node affinity ([Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)) |
| Can many pods write? | Different files, yes. One writer for each file, and writes go in order from the start of the file ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)) | Yes. Same access as for reads ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)) | This page's reading: only the pods on that node, because the volume is tied to the node |
| Does the data survive losing the node? | This page's reading: yes, the data is not on the node | This page's reading: yes. A failed persistent file server is replaced within minutes. A scratch file system is not replicated ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)) | No. It does not persist if the instance is stopped, hibernated or terminated ([AWS EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-store-lifetime.html)) |
| Where does it sit? | S3 Express One Zone sits in one zone. AWS contrasts it with standard S3 buckets, which spread data across zones ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)) | In the zone of its subnet (this page's reading). Other zones of the VPC can mount it ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)) | On the node |
| Many small files? | AWS says latency-sensitive applications get small objects in roughly 100 to 200 milliseconds ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)). How Mountpoint behaves with millions of small files was not read | On Persistent 2, fewer than 100K files in a directory, and throughput generally rises as the average I/O size rises ([AWS FSx tips](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance-tips.html)). AWS says FSx for OpenZFS suits small files with small I/O ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)) | Not read |
| A limit to know | At least 3,500 writes or 11,000 reads per second for each partitioned prefix. AWS says that spreading requests across many prefixes helps S3 partition more of them in parallel ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)) | One client talking to one object storage server is limited to 5 Gbps ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)) | Gone when the instance stops or terminates |

- **Mount a bucket: choose the access mode.** An access mode says how nodes may mount a volume. `ReadOnlyMany` means that many nodes can mount it read-only. `ReadWriteMany` means that many nodes can mount it read-write. The driver's example allows both. For training data, `ReadOnlyMany` states the intent, but Kubernetes does not enforce write protection once the storage is mounted ([Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)).
- **Mount a bucket: the static volume.** The PersistentVolume names the bucket and carries the mount options. The claim binds to it by name. The capacity is ignored but required, and the StorageClass name must be an empty string for static provisioning ([Mountpoint CSI example](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/main/examples/kubernetes/static_provisioning/static_provisioning.yaml)). All names, the bucket, the Region and the prefix in the manifest are illustrative. The fields come from the driver's example.

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: training-data-pv
spec:
  capacity:
    storage: 1200Gi                  # ignored, but required
  accessModes:
    - ReadOnlyMany
  storageClassName: ""               # required for static provisioning #!
  claimRef:
    namespace: default
    name: training-data-pvc
  mountOptions:
    - region us-west-2               # illustrative
    - prefix datasets/               # illustrative
  csi:
    driver: s3.csi.aws.com           #!
    volumeHandle: training-data-pv   # must be unique
    volumeAttributes:
      bucketName: example-training-data   # illustrative #!
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: training-data-pvc
spec:
  accessModes:
    - ReadOnlyMany
  storageClassName: ""
  resources:
    requests:
      storage: 1200Gi                # ignored, but required
  volumeName: training-data-pv
```

- **Object storage: what Mountpoint does not do.** Mountpoint does not implement all the features of a POSIX file system. Files can only be written in order and by one writer at a time. A file that is being written is not available for reading until the writer closes it ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)).
- **Object storage: two mounts of one bucket.** With several mounts of one bucket, there is no coordination between writes to the same object ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)). This page's reading: a rank that writes its own new checkpoint file fits these rules. A job that edits a file in place does not.
- **Object storage: the local cache.** The driver can keep a cache of read data on the node. The cache is an `emptyDir` or a generic ephemeral volume, which is a volume that Kubernetes creates and deletes with the pod ([Mountpoint CSI caching](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/main/docs/CACHING.md)).
- **Object storage: the cache size.** The size limit is "optional but highly recommended", because the cache might otherwise use all of the node's storage. The medium `Memory` uses RAM instead of disk. The fragment shows the two fields. The size is illustrative.

```yaml
csi:
  driver: s3.csi.aws.com
  volumeHandle: training-data-pv
  volumeAttributes:
    bucketName: example-training-data    # illustrative
    cache: emptyDir
    cacheEmptyDirSizeLimit: 50Gi         # illustrative #!
```

- **Object storage: pod sharing.** Several pods share one Mountpoint instance only when they run on the same node and use the same volume, the same mount options, the same authentication source and the same FSGroup ([Mountpoint CSI pod sharing](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/main/docs/MOUNTPOINT_POD_SHARING.md)). The FSGroup is a field in the pod's security context.
- **Object storage: one Mountpoint for each pod.** AWS's EKS guide describes the other case: each pod has its own Mountpoint instance and a local cache of 5 to 10 GB ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). The two AWS pages differ in wording, so do not state "one Mountpoint for each pod" as a rule.
- **Object storage: speed settings.** Mountpoint's defaults and S3's request advice do not change a manifest, so they are in [Appendix G](#d-data-path-appendix-g-mountpoint-and-s3-speed-settings).
- **S3 Express One Zone: pin the volume to the zone.** The driver's example for an Express bucket adds a node affinity on the zone label to the PersistentVolume. The bucket name carries the zone ID. Both values in the snippet are the example's ([Mountpoint CSI example](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/main/examples/kubernetes/static_provisioning/s3_express_specify_az.yaml)). [Zones](#d10-7) in Deep dive 10 explains the zone label and what a crossing costs.

```yaml
spec:
  csi:
    driver: s3.csi.aws.com
    volumeHandle: s3-csi-driver-volume
    volumeAttributes:
      bucketName: s3-csi-driver--usw2-az1--x-s3   # example from the driver
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
          - key: topology.kubernetes.io/zone
            operator: In
            values:
            - us-west-2a                          # example from the driver #!
```

- **S3 Express One Zone: what it gives up.** Directory buckets do not support S3 Replication, S3 Versioning or S3 Lifecycle transition actions ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-express-differences.html)). AWS's EKS guide says to use it for data that can be recreated or cached ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)).
- **S3 Express One Zone: speed and cost against S3 Standard.** AWS's EKS guide states up to 10 times faster access and up to 80 percent lower request costs than S3 Standard. It also says that for smaller jobs, such as tens of nodes, S3 Standard may be enough ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)).
- **S3 Express One Zone: writes with Mountpoint.** On Express, Mountpoint can append to files when `--incremental-upload` is set, and it can rename atomically. Neither works for directory buckets in Local Zones ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)).
- **FSx for Lustre: pick the type.** A persistent SSD or HDD file system replicates data inside one zone and replaces a failed file server within minutes. A persistent Intelligent-Tiering file system replicates data across zones. A scratch file system does not replicate data and loses it if a file server fails ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)).
- **FSx for Lustre: which type for which job.** AWS's EKS guide recommends scratch SSD for short-lived work of hours, and persistent SSD for long-running, critical work such as training ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). Throughput for each type and size is in [Appendix A](#d-data-path-appendix-a-fsx-for-lustre-numbers).
- **FSx for Lustre: the StorageClass.** The StorageClass uses the provisioner `fsx.csi.aws.com`. It names one subnet, because the driver allows only one subnet in `subnetId`. The security groups must allow port 988 ([FSx CSI driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/README.md)).
- **FSx for Lustre: where the file system sits.** The file system can be in the cluster's VPC or reached through VPC peering ([FSx CSI driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/docs/README.md)).
- **FSx for Lustre: the claim.** The claim asks for `ReadWriteMany` and a size. FSx for Lustre sizes are 1.2 TiB, 2.4 TiB, and steps of 2.4 TiB beyond that for the persistent and scratch 2 types. A new claim can show Pending for 5 to 10 minutes before it is Bound ([AWS EKS FSx](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html)).
- **FSx for Lustre: the manifest.** The IDs in the StorageClass are illustrative. The fields are the driver's example, and the example sets more parameters than the pages read explain. Its `deploymentType` is `PERSISTENT_1`, which cannot have EFA, because AWS supports EFA on Persistent 2 file systems ([AWS FSx EFA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/efa-file-systems.html)).

```yaml
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: fsx-sc
provisioner: fsx.csi.aws.com
parameters:
  subnetId: subnet-0123456789abcdef0        # illustrative, one subnet only #!
  securityGroupIds: sg-0123456789abcdef0    # illustrative
  deploymentType: PERSISTENT_1              #!
  perUnitStorageThroughput: "200"           # MB/s per TiB
  dataCompressionType: "NONE"
mountOptions:
  - flock
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fsx-claim
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: fsx-sc
  resources:
    requests:
      storage: 1200Gi
```

- **FSx for Lustre: the file system must be in the right place.** AWS says to put the file system in the same zone as the GPU nodes. If GPU nodes sit in several zones, AWS recommends one file system in each zone ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). AWS says FSx for Lustre is not supported in all zones, and tells you to check the console for your subnet's zone ([AWS EKS FSx](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html)).
- **FSx for Lustre: with Spot GPU nodes.** With Spot GPU nodes, AWS recommends an "administrative" pod that mounts the file system and loads the data before the GPU nodes start ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)).
- **FSx for Lustre: speed for each client.** The table gives the most one client instance can read, by file system and NIC. The last row uses GDS, GPUDirect Storage, a direct data transfer between the file system and the GPU memory ([AWS FSx EFA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/efa-file-systems.html), [AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)).

| File system | Client NIC | Most one client can read |
|---|---|---|
| Without EFA | any | 100 Gbps (one client to one storage server: 5 Gbps) |
| With EFA | ENA | 100 Gbps (one client to one storage server: 5 Gbps) |
| With EFA | ENA Express | 100 Gbps |
| With EFA | EFA | 700 Gbps |
| With EFA | EFA with GDS | 1,200 Gbps |

- **FSx for Lustre: one client and one server.** Traffic between one client and one object storage server is limited to 5 Gbps. Striping spreads one file over several object storage targets, which are the volumes that hold file data ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)). This page's reading: striping lets one client use more than one server.
- **FSx for Lustre: when to use EFA.** For a file system with over 10 GBps of throughput capacity, AWS recommends EFA ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)). Large files need a high stripe count, because AWS says that creating large files with low stripe counts can cause I/O performance bottlenecks ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)). The default layout and the throughput for each size are in [Appendix A](#d-data-path-appendix-a-fsx-for-lustre-numbers).
- **FSx for Lustre: decide on EFA when you create the file system.** You can enable EFA only when you create a file system. You cannot turn it on or off after creation. The file system needs the same zone and the same /16 CIDR block as the EFA clients. A CIDR block is a range of IP addresses, and a /16 holds 65,536 of them (derived: 2^16) ([AWS FSx EFA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/efa-file-systems.html)).
- **FSx for Lustre: what EFA clients need.** Clients need Nitro v4 or higher. They need Amazon Linux 2023 (AL2023), Red Hat Enterprise Linux (RHEL) 9.5 and newer, or Ubuntu 22 and newer with kernel 6.8 or newer ([AWS FSx EFA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/efa-file-systems.html)).
- **FSx for Lustre: EFA and the CSI driver.** The pages read do not say whether the CSI driver can create an EFA-enabled file system. Static provisioning with a file system that you created works, and the AWS sample uses it ([AWS sample](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre)).
- **FSx for Lustre linked to S3: the link.** A file system can link to an S3 bucket through a data repository association, which is a link between a folder of the file system and an S3 prefix. You can have at most 8 of them for one file system ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)).
- **FSx for Lustre linked to S3: how data moves in.** With the import-metadata option, the file system first imports the metadata of the objects, which is owner, permissions and timestamps. When your application first accesses a file, FSx copies its content from S3 ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/importing-files.html)). Files imported from S3 use the `ImportedFileChunkSize` setting for their layout, and AWS's EKS guide recommends a higher value for large files ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). The default is in [Appendix A](#d-data-path-appendix-a-fsx-for-lustre-numbers).
- **FSx for Lustre linked to S3: the way back.** Automatic export sends changes back to S3. The console turns it on by default. The CLI and the API do not. With it on, FSx exports changes asynchronously once the application finishes modifying the file ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)). An export data repository task is the other documented route. Automatic export and export tasks cannot be used at the same time on one file system ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/overview-dra-data-repo.html)). The picture shows the five steps.

![FSx for Lustre linked to S3](diagrams/extra/d16-fsx-s3.svg)

- **FSx for Lustre linked to S3: the limits.** Automatic import needs the file system and the bucket in the same Region. Automatic export can cross Regions ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/overview-dra-data-repo.html)). Intelligent-Tiering file systems cannot link to S3. File system backups are not supported on a linked file system ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html)). FSx does not prevent conflicting writes in two places, so your application must coordinate them ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)).
- **FSx for Lustre linked to S3: through the CSI driver.** The driver's S3 example sets `s3ImportPath` and `s3ExportPath` in the StorageClass, and both must use the same bucket. Files created in the file system are not synced to S3 automatically ([FSx CSI driver S3 example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/examples/kubernetes/dynamic_provisioning_s3/README.md)).
- **FSx for Lustre linked to S3: syncing by hand.** The example syncs new files by hand with `lfs hsm_archive`, which needs a privileged container, one whose processes are essentially equivalent to root on the host ([Kubernetes Pod API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/)). The example also gives the container the `CAP_SYS_ADMIN` Linux capability ([FSx CSI driver S3 example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/examples/kubernetes/dynamic_provisioning_s3/README.md)). A data repository association with automatic export, or an export task, is the other route, and the routes do not behave the same way.
- **FSx for Lustre: what storage costs.** This is AWS's own example, read on 2026-11-22, for US East (N. Virginia) ([AWS FSx pricing](https://aws.amazon.com/fsx/lustre/pricing/)). A persistent file system with 1,200 GB at 125 MB/s per TiB costs $0.145 per GB-month for storage and $0.050 per GB-month for backups. Extra metadata IOPS cost $0.055 per IOPS-month. AWS uses the term for the rate of metadata operations that the file system supports ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)). The price table for other Regions loads by script and was not read.
- **FSx for Lustre: crossing zones costs money.** There is no transfer charge from the file system's preferred zone. From another zone of the same Region, AWS charges $0.01/GB in each direction ([AWS FSx pricing](https://aws.amazon.com/fsx/lustre/pricing/), read 2026-11-22). [Networking](#d12-2) in Deep dive 12 covers the levers, and [Storage](#d12-3) in Deep dive 12 covers storage cost.
- **Node-local NVMe: how much there is.** A p5.48xlarge lists 8 x 3.84 TB, which is 30.72 TB in total (derived), and AWS says P5 supports up to 30 TB. P4d includes 8 TB with 16 GB/s read throughput. P6 instances support up to 30 TB, and P6e UltraServers up to 405 TB ([AWS P5](https://aws.amazon.com/ec2/instance-types/p5/), [AWS P4](https://aws.amazon.com/ec2/instance-types/p4/), [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/)). The instance store costs nothing extra ([AWS EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/InstanceStorage.html)).
- **Node-local NVMe: the Kubernetes objects.** The local volume static provisioner turns local disks into PersistentVolumes. It does not support dynamic provisioning, and it has been GA since Kubernetes 1.14 ([local provisioner](https://github.com/kubernetes-sigs/sig-storage-local-static-provisioner)). Kubernetes says to create a StorageClass with the provisioner `kubernetes.io/no-provisioner` and `WaitForFirstConsumer`, so that binding waits until a pod is scheduled ([Kubernetes StorageClass](https://kubernetes.io/docs/concepts/storage/storage-classes/)). You must set the node affinity yourself on local volumes ([Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)).

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-storage
provisioner: kubernetes.io/no-provisioner    # no dynamic provisioning #!
volumeBindingMode: WaitForFirstConsumer      # bind after the pod is scheduled #!
```

- **Node-local NVMe: when the node is deleted.** The data is likely lost, but the PersistentVolume object still exists. The system keeps trying to schedule the pod to the deleted node ([local provisioner](https://github.com/kubernetes-sigs/sig-storage-local-static-provisioner)). The provisioner's README links a node cleanup controller for this case. This page's reading: keep a durable copy of any checkpoint that must survive the node.
- **emptyDir and generic ephemeral volumes.** An emptyDir has no limit on the space it can use unless you set `sizeLimit`, and the default is undefined ([Kubernetes volumes](https://kubernetes.io/docs/concepts/storage/volumes/), [Kubernetes Pod API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/)). Its disk is the one that holds the kubelet's root directory. A generic ephemeral volume can have a fixed size that pods cannot exceed. It can come from any driver that supports dynamic provisioning ([Kubernetes ephemeral volumes](https://kubernetes.io/docs/concepts/storage/ephemeral-volumes/)).
- **Where a volume lands: zone and node rules.** [Zones](#d10-7) in Deep dive 10 covers the binding mode `WaitForFirstConsumer`, the allowed zones of a StorageClass and storage capacity tracking. [EBS is AZ-locked](#d08-7) in Deep dive 8 shows the same rule for Amazon Elastic Block Store (EBS), the AWS block disks. [Storage classes, PV and PVC](#d14-1) in Deep dive 14 covers the basics. A storage system can also have an internal topology that Kubernetes cannot influence ([Kubernetes CSI](https://kubernetes-csi.github.io/docs/topology.html)).
- **Checkpoints: what PyTorch does.** PyTorch's Distributed Checkpoint saves and loads from many ranks in parallel, produces at least one file per rank, and can save in one cluster layout and load into another ([PyTorch](https://docs.pytorch.org/docs/2.15/distributed.checkpoint.html)). PyTorch calls checkpointing often a bottleneck in the critical path and suggests saving in parallel and asynchronously ([PyTorch recipe](https://docs.pytorch.org/tutorials/recipes/distributed_async_checkpoint_recipe.html)).
- **Checkpoints: what PyTorch does not say.** The PyTorch page does not say where the files must live. This page's reading: many ranks writing new files fits lane 1 and lane 2. Lane 3 loses the files with the node, so copy them to a durable path.
- **Checkpoints: Design 3 holds the tiers.** [Training: data and checkpoints](#ds03-4) in Design 3 shows the same three tiers as a design: S3, FSx for Lustre and local NVMe. This page adds the Kubernetes objects, the sourced numbers and the limits. It does not copy the tiers.
- **Checkpoints: where to read more.** [Tiered checkpoint storage](#ds20-7) in Design 20 and [Data and checkpoints across sites](#ds18-8) in Design 18 cover the design. [Regions, the WAN and failover](#d10-8) covers the copy to a second Region. [Appendix E](#d-data-path-appendix-e-training-storage-at-meta-for-llama-3) shows one company's numbers.
- **Another option: S3 Files, what it is.** AWS's S3 Files is a shared file system that connects AWS compute to data in S3. It is built on Amazon EFS. You mount it on EKS with the Amazon EFS CSI driver, from version 3.0.0 ([AWS S3 Files](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html), [AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/s3files-csi.html)).
- **Another option: S3 Files, its rules.** EKS Fargate, the AWS technology that gives containers compute capacity without servers that you manage, does not support it ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/s3files-csi.html)). It needs S3 Versioning on the bucket. In a conflict the bucket wins ([AWS S3 Files](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-synchronization.html)). No S3 Files benchmark for training was found in the pages read. The numbers that AWS states are in [Appendix F](#d-data-path-appendix-f-s3-files-numbers).
- **Other storage that exists: EBS, EFS and FSx for OpenZFS.** An EBS volume and its instance must be in the same zone ([AWS EBS](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes.html)). AWS's EKS guide recommends EFS for shared model access with moderate performance needs, and it recommends elastic throughput mode for ML. It recommends FSx for OpenZFS for small files with small reads and writes ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). No throughput number for EFS or EBS was read.
- **Not known from sources.**
  - No measured throughput of Mountpoint for S3 on a GPU node. The Mountpoint benchmark page links to charts for an `m5dn.24xlarge` instance and gives no numbers in text ([Mountpoint benchmarking](https://github.com/awslabs/mountpoint-s3/blob/main/doc/BENCHMARKING.md)).
  - How a training framework reads a dataset: file formats, shard layout and loader settings. No source was read.
  - Where the files of a PyTorch Distributed Checkpoint must live.
  - Which zones do not support FSx for Lustre.
  - Whether the FSx CSI driver can create an EFA-enabled file system.
  - Which CSI features, such as snapshots, cloning and expansion, each driver supports.
  - A checkpoint size for a given model. Meta states 1 MB to 4 GB for each GPU for one training run, and that is not a rule ([Appendix E](#d-data-path-appendix-e-training-storage-at-meta-for-llama-3)).
  - Whether S3 Files works well for training at scale.
  - Prices for S3 requests and storage, and prices for other Regions and for other FSx types. The S3 price table loads by script and was not read.

### Serving and loading heavy models: why a new pod is slow to become ready

**The Kubernetes question.** A new inference pod for a 140 GB model is scheduled onto a GPU node. Why is it slow to become ready, and which Kubernetes features and node settings cut each part of the wait?

**Scenario.** This scenario is illustrative. A team serves a model with 70 billion parameters in 16-bit numbers. Each parameter takes 2 bytes ([ZeRO paper](https://ar5iv.labs.arxiv.org/html/1910.02054)), so the weights are 70 x 10^9 x 2 = 140 GB (derived). The autoscaler adds a replica on a new node. The node must pull a container image of many gigabytes. The pod must fetch 140 GB. The server must load the weights into GPU memory. Requests wait until all three are done.

![Serving and loading heavy models: the chain from scheduled to ready](diagrams/sections/d16-2.svg)

**What the picture shows.** A timeline of four steps, from "pod scheduled" to "ready for traffic". Under each step sit the fixes and the Kubernetes setting that triggers each one. The four blocks have equal width and say nothing about time, because no source gives a time for the whole chain. The strip at the bottom shows how long 140 GB takes at two network rates. Both times are derived, and neither is a measured weight load.

- **Step 1, image pull.** The kubelet pulls the container image. By default it pulls one image at a time. You can pull in parallel with two kubelet fields ([Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)). You can also keep the image small, put it on the node ahead of time, or load it lazily.
- **Step 2, weight fetch.** The pod copies the weights from storage, or mounts them. The ways are an init container with a scratch volume, a shared volume, an image volume, or a streamer that reads straight into GPU memory.
- **Step 3, load into GPU memory.** The server reads the weights from where they sit and places them on the GPUs. The loader you choose sets the speed. The point "Weights: what was measured for a heavy model" gives an AWS sample that measured a gap between the default loader and a loader that uses GDS.
- **Step 4, ready for traffic.** A pod whose containers report that they are not ready does not receive traffic through Kubernetes Services ([Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)). A startup probe protects a slow start. A readiness probe holds back traffic.
- **The strip: 140 GB at two rates.** 140 GB is 1,120 gigabits. At 5 Gbps it takes 224 seconds (derived). AWS gives 5 Gbps as the limit for one flow between instances that are not in one cluster placement group, which is a grouping of instances within a single zone ([AWS EC2 bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html), [AWS placement groups](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-strategies.html)). At 100 Gbps it takes 11.2 seconds (derived). 100 Gbps is S3's "up to" figure for data lake applications on one instance ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)).
- **The strip: how to read the two rates.** Both are illustrative rates for one flow and for one network card. They are not limits of S3 or FSx, and they are not measurements. Applying the 5 Gbps figure to a download from storage is this page's reading, because the EC2 page does not say so ([Appendix C](#d-data-path-appendix-c-transfer-time-arithmetic)).

**Going deeper, by topic.**

- **The wait is a chain, and no total was found.** Parts were measured by different people on different hardware: an image pull, and two weight loads. No source read gives the time from "pod scheduled" to "ready" for a GPU model server, so this page does not add the parts.
- **The wait is a chain: where to measure it.** [Four ways to get weights into a pod](#p02-3) and [Measure time to ready](#p02-5) in Post 02 show how to measure it on your cluster. [Inference: autoscaling and cold starts](#design-training-inference-inference-autoscaling-and-cold-starts) in Design 3 splits the cold start into node, image and weights, and says to measure each per model. This page adds the settings for the image and the weights.
- **Image pull: one image at a time by default.** The kubelet sends one pull request at a time, and other pulls wait. Two different nodes can pull the same image in parallel, because nodes decide in isolation. The kubelet never pulls two images in parallel for one pod, so an init container image and an app container image are pulled one after the other ([Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)).
- **Image pull: allow parallel pulls.** Set `serializeImagePulls` to false in the kubelet configuration ([Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)). The default is true ([kubelet configuration](https://kubernetes.io/docs/reference/config-api/kubelet-config.v1beta1/)). The container runtime's image service must handle parallel pulls.
- **Image pull: limit parallel pulls.** Then `maxParallelImagePulls` can limit how many images are pulled at once. That field is stable since Kubernetes v1.35. Without it there is no limit. A value of 2 or more needs `serializeImagePulls` set to false, and the kubelet fails to start with an invalid setting ([Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)). The page gives no recommended value. The value 3 in the snippet is illustrative.

```yaml
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
serializeImagePulls: false      # default is true #!
maxParallelImagePulls: 3        # illustrative; default is no limit #!
```

- **Image pull: a smaller image.** AWS's EKS guide says to use base images with the fewest dependencies and multi-stage builds. Its example is a PyTorch runtime image of 3.03 GB against 6.66 GB for the `devel` image ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-performance.html)).
- **Image pull: where the image comes from.** AWS's EKS guide says that pull time depends on the bandwidth of the instance, and that baseline and burst bandwidth differ. VPC interface endpoints for ECR keep pulls private. ECR is Amazon Elastic Container Registry, AWS's managed container image registry ([AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html)). An ECR pull through cache keeps a copy of images from another registry and checks them at least every 24 hours ([AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache.html)).
- **Image pull: put the image on the node ahead of time.** The Kubernetes page says pre-pulled images suit you only if you control node configuration, and they do not work reliably when your cloud provider replaces nodes automatically. With `imagePullPolicy` set to `IfNotPresent` or `Never`, a local image is used ([Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)).
- **Image pull: a DaemonSet that pre-pulls.** A DaemonSet runs a copy of a pod on all or some nodes ([Kubernetes DaemonSet](https://kubernetes.io/docs/concepts/workloads/controllers/daemonset/)). AWS's EKS guide uses one to fill the container runtime cache. It warns that nodes added by a scaler may schedule workload pods before the pre-pull finishes, and that the kubelet's image garbage collection can remove pre-pulled images ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-performance.html)).
- **Image pull: an EBS snapshot of the image cache.** AWS's EKS guide says you can take an EBS snapshot of cached images and reuse it for worker nodes, so images are on the node at startup ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-performance.html)).
- **Image pull: what the snapshot measured.** The EKS Blueprints pattern measured an NVIDIA PyTorch image of 9.5 GB compressed and 20.4 GB on disk. With the snapshot, the pod started in under 5 seconds. Without it, the capture shows 334 seconds ([EKS Blueprints](https://aws-ia.github.io/terraform-aws-eks-blueprints/patterns/machine-learning/ml-container-cache/)).
- **Image pull: why 334 seconds is not a speedup.** The uncached pod is one without a toleration for GPU nodes, and the page's node groups show a default group of `m5.large` nodes. This page's reading: the two runs used different nodes, so treat 334 seconds as an example and not as a like-for-like speedup.
- **Image pull: faster pull and unpack with SOCI.** SOCI is AWS's snapshotter project. A snapshotter is the part of containerd, the container runtime on the node, that manages the snapshots of container file systems ([containerd snapshotters](https://github.com/containerd/containerd/blob/main/docs/snapshotters/README.md)). In its parallel pull and unpack mode, it opens several HTTP connections for each layer and unpacks layers in parallel. AWS says it works with existing images, with no rebuild ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-performance.html)). This is a node setting and not a pod field.
- **Image pull: faster disk on the node.** AWS's EKS guide says to consider ephemeral NVMe instance storage for `/var/lib/kubelet`, `/var/lib/containerd` and `/var/log/pods`. Karpenter is an open-source project that manages the life of Kubernetes nodes ([Karpenter](https://karpenter.sh/docs/)). With Karpenter, set `instanceStorePolicy` to `RAID0`. A managed node group is a group of EC2 nodes that EKS creates and runs. With managed node groups, set `localStoragePolicy` in `NodeConfig` ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-performance.html)). The node's ephemeral storage is shared by pods that ask for ephemeral storage and by downloaded images.
- **Image pull: lazy loading.** Lazy loading starts a container before the whole image has arrived, and fetches the rest on demand. SOCI, eStargz and GKE Image streaming are three ways. SOCI and eStargz use a snapshotter on the node, so they are node settings. [Appendix D](#d-data-path-appendix-d-lazy-image-loading-in-detail) has the details.
- **Image pull: the limits of SOCI on Kubernetes.** SOCI on Kubernetes must be used for all containers on the node, and it must be configured at node launch ([SOCI](https://github.com/awslabs/soci-snapshotter/blob/main/docs/kubernetes.md)).
- **Image pull: the cost of lazy pulling.** The eStargz README says lazy pulling causes a run-time penalty, because reading files downloads their content, and it offers workload-based optimization against it ([eStargz](https://github.com/containerd/stargz-snapshotter)). None of the pages read says whether lazy loading helps a multi-gigabyte CUDA image.
- **Weights: five ways to get them into the pod.** The table compares them. The Kubernetes side of each is in [Four ways to get weights into a pod](#p02-3) in Post 02, with manifests that this page does not repeat.

| Way | Kubernetes object | What the sources say |
|---|---|---|
| Bake the weights into the image | The image | AWS recommends it for smaller models with infrequent updates. It is not ideal for large models because of registry pull throughput ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-performance.html)) |
| Download at start with an init container | `initContainers` and an `emptyDir` | AWS recommends starting here for large models with frequent updates. It needs retry logic, authentication and caching ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-performance.html)) |
| Mount a shared volume | A claim with `ReadOnlyMany` or `ReadWriteMany` | AWS recommends mounting S3, FSx for Lustre, FSx for OpenZFS or EFS as PersistentVolumes through their CSI drivers ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)) |
| Mount the weights as an image volume | `volumes[*].image` | A read-only mount of content from an OCI registry, which stores container images and artifacts. OCI is the Open Container Initiative, which sets open standards for container formats ([OCI](https://opencontainers.org/about/overview/)). The runtime must support it ([Kubernetes image volumes](https://kubernetes.io/docs/tasks/configure-pod-container/image-volumes/)) |
| Stream into GPU memory | A server option, such as vLLM's streamer | Reads tensors with many readers at once and streams them to GPU memory ([vLLM](https://docs.vllm.ai/en/latest/models/extensions/runai_model_streamer/)) |

- **Weights: the init container.** Init containers run to completion, one after the other. The kubelet restarts a failing init container until it succeeds, unless the pod's `restartPolicy` is Never. The pod requests enough resources to run its largest init container ([Kubernetes init containers](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/)).
- **Weights: the scratch volume.** The scratch volume is usually an `emptyDir`, which uses the node's disk and has no limit unless you set `sizeLimit` ([Kubernetes volumes](https://kubernetes.io/docs/concepts/storage/volumes/)). Kubernetes deletes an `emptyDir` when the pod leaves the node, so this page's reading is that every new pod pays the whole download again. The manifest is in [Pattern 1 and 2](#post-model-data-pattern-1-and-2-init-container-copy-and-a-shared-volume) of Post 02, and [Four ways to get weights into a pod](#p02-3) gives the overview.
- **Weights: a shared volume, the access modes.** A claim read by many pods needs `ReadOnlyMany` (many nodes mount it read-only) or `ReadWriteMany` (many nodes mount it read-write). With a CSI volume, the modes that work depend on the driver. The modes do not enforce write protection once the storage is mounted ([Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)).
- **Weights: a shared volume, what AWS advises.** AWS's EKS guide says that for online inference, models are cached on a PV and many pods use it ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). The storage paths and their limits are in [Training data and checkpoints](#d16-1). [Storage](#d12-3) in Deep dive 12 covers storage cost.
- **Weights: an image volume.** An image volume mounts the files of an OCI image or artifact read-only. You set `volumes[*].image` with a `reference` and a `pullPolicy`. The container runtime must support it ([Kubernetes image volumes](https://kubernetes.io/docs/tasks/configure-pod-container/image-volumes/)). The manifest is in [Pattern 3 and 4](#p02-4) of Post 02.
- **Weights: the image volume feature gate.** A feature gate is a switch that turns a Kubernetes feature on or off. The feature gate `ImageVolume` was alpha in 1.31 and 1.32, beta from 1.33 and off by default until 1.34, beta and on by default in 1.35, and stable in 1.36 ([Kubernetes feature gates](https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/)).
- **Weights: image volume support in runtimes.** The Kubernetes post of 2025-04-29 said the feature stayed off by default because not all runtimes fully support it. CRI-O supported it from v1.31, and containerd planned it for v2.1.0 ([Kubernetes blog](https://kubernetes.io/blog/2025/04/29/kubernetes-v1-33-image-volume-beta/)). Which runtimes support it today is not known from sources.
- **Weights: stream into GPU memory.** vLLM can read Safetensors weights with the Run:ai Model Streamer and stream them to GPU memory. Safetensors is a file format for model weights. The flag is `--load-format runai_streamer`, and the path can be a local path, `s3://`, `gs://` or `az://` ([vLLM](https://docs.vllm.ai/en/latest/models/extensions/runai_model_streamer/)).
- **Weights: the streamer settings.** `concurrency` sets how many readers the host opens to S3. `distributed` can cut loading time from object storage or fast network file shares ([vLLM](https://docs.vllm.ai/en/latest/models/extensions/runai_model_streamer/)).
- **Weights: the sharded loader.** The sharded loader `runai_streamer_sharded` fits a model that is split across GPUs, where each worker reads only its own shard ([vLLM](https://docs.vllm.ai/en/latest/models/extensions/runai_model_streamer/)). Tensor parallelism is one way to split a model, and it splits each layer across GPUs ([What a training job asks of the cluster](#d15-2)). The manifest is in [Fast model loading](#ds20-9) in Design 20.
- **Weights: what was measured for a heavy model.** An AWS sample loaded DeepSeek-V4-Pro-0813, with 892.7 GB of weights, on one `p5en.48xlarge` with 8 H200 GPUs, using vLLM 0.28.0 in us-east-2. The numbers are means of three runs. The sample says it is a proof of concept. Its FSx for Lustre file system is Persistent 2 with 8 object storage targets and EFA. The GDS arm also uses a different loader, `instanttensor`, so the 48.0 times is the gain of the loader, GDS and the path together, and not of GDS alone ([AWS sample](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre)). The 15 GB benchmark of the streamer is in [Appendix B](#d-data-path-appendix-b-measured-weight-loads-and-the-gds-limits).

| Read path | Weight load | Total model load | Weight load against the default |
|---|---|---|---|
| FSx for Lustre, default vLLM loader | 1,703.0 s | 1,712.7 s | 1.0 times |
| S3 with the Run:ai streamer, `concurrency: 32` | 316.3 s (rank 0 only) | 368.7 s | 5.4 times faster |
| FSx for Lustre with GDS and the `instanttensor` loader | 35.5 s | 45.1 s | 48.0 times faster |

- **Weights: what the GDS path needs on Kubernetes.** AWS's setup script for FSx with GDS allows only a list of instance types. The EKS AL2023 GPU AMI, the machine image that a node boots from, ships no `nvidia-fs`, the kernel module that GDS needs, so the host needs a build of it. The pod ran privileged ([AWS sample known issues](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre/blob/main/KNOWN_ISSUES.md)). Those are the sample's statements, not AWS product documentation. The list of instance types is in [Appendix B](#d-data-path-appendix-b-measured-weight-loads-and-the-gds-limits).
- **Weights on the node: local NVMe.** Weights copied to the node's NVMe are lost when the instance stops or terminates ([AWS EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-store-lifetime.html)). This page's reading: every new node pays the fetch once, and a scaling-up cluster adds new nodes. The objects that mount node disks are in [Training data and checkpoints](#d16-1). The point "Object storage: the local cache" says the driver keeps a cache of read data on the node. This page's reading: that cache can hold weights read from S3.
- **Wait for the load: the probes.** A startup probe's `failureThreshold` times `periodSeconds` should be long enough to cover the worst-case startup time. In Kubernetes's example, 30 x 10 = 300 seconds, and if the probe never succeeds the container is killed after 300 seconds ([Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)). A readiness probe fits an app that must load large data during startup. Use your server's health endpoint and your measured load time.
- **Wait for the load: a probe example.** The serving manifest in [Deploying by hand](#p01-3) in Post 01 sets a startup probe with `failureThreshold` 90 and `periodSeconds` 10, which is 900 seconds (derived).

- **Wait for the load: the rolling update.** A Deployment's `maxUnavailable` and `maxSurge` both default to 25 percent. `maxUnavailable` lets old pods go at once when the rollout starts, and more old pods go as new pods become ready ([Kubernetes Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)). This page's reading: every new replica takes the whole load time to become ready, so a rollout takes at least that long for each batch, and a surge pod needs a free GPU. [Autoscaling and operations](#ds21-10) in Design 21 and [Scale, start fast, route smartly](#p03-6) in Post 03 cover scale-up.
- **Spread replicas across zones.** A topology spread constraint controls how pods spread across failure domains such as zones and nodes ([Kubernetes topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)). [Zones](#d10-7) in Deep dive 10 and [Spreading across AZs and nodes](#d08-4) in Deep dive 8 show the rule.
- **Keep the weights near the replicas.** AWS says to put an FSx file system in each zone where GPU nodes run, and to keep Express One Zone data in the zone of its compute ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). For a second Region, [Regions, the WAN and failover](#d10-8) has the copy of 140 GB, its price and its time.
- **Not known from sources.**
  - A time from "pod scheduled" to "ready" for a GPU model server. Only the parts were measured.
  - The node type in the EKS Blueprints image pull capture. The page lists `g6e.xlarge` for the GPU group and does not say which node the capture used.
  - A recommended value for `maxParallelImagePulls`.
  - Whether SOCI or eStargz helps a multi-gigabyte CUDA image.
  - Which container runtimes support image volumes today.
  - A measured speed of one S3 read from another Region.

### Appendix A. FSx for Lustre numbers

**This appendix holds FSx for Lustre figures that do not change what you request or configure.** Each point has its source. All come from AWS's FSx pages, fetched on 2026-11-21 or 2026-11-22 and re-opened on 2026-11-22.

**Going deeper, by topic.**

- **Throughput for each type and size.** The throughput that a file system provides depends on its type. Persistent 2 SSD offers 125, 250, 500 or 1,000 MB/s for each TiB. Persistent 1 SSD offers 50, 100 or 200. Persistent HDD offers 12 or 40. Scratch 2 offers 200. Intelligent-Tiering offers a throughput capacity of 1,000, 2,000 or steps of 4,000 MB/s ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)). Scratch file systems can burst up to six times their baseline of 200 MBps per TiB ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/using-fsx-lustre.html)).
- **Network and disk throughput for each TiB on SSD.** The table lists the baseline network throughput and the disk throughput for each TiB of SSD storage. MBps is megabytes per second ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/ssd-storage.html)).

| Deployment type | Network throughput (MBps per TiB) | Disk throughput (MBps per TiB) |
|---|---|---|
| SCRATCH_2 | 200 | 200 (read), 100 (write) |
| PERSISTENT-125 | 320 | 125 |
| PERSISTENT-250 | 640 | 250 |
| PERSISTENT-500 | 1,300 | 500 |
| PERSISTENT-1000 | 2,600 | 1,000 |

- **Default striping.** Striping spreads a file in chunks over several object storage targets (OSTs), the volumes that hold file data. For file systems created after August 25, 2023, the default layout is `-E 100M -c 1 -E 10G -c 8 -E 100G -c 16 -E -1 -c 32`. AWS says that creating large files with low stripe counts can cause I/O performance bottlenecks ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)).
- **Chunk size of imported files.** Files imported from S3 use the setting `ImportedFileChunkSize`, which defaults to 1 GiB ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)). AWS's EKS guide says to raise it for large imported files ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)).
- **Tips from AWS.** On Persistent 2 file systems, limit each directory to fewer than 100K files. Throughput generally rises as the average I/O size rises ([AWS FSx tips](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance-tips.html)).
- **Release files to free space.** Releasing a file keeps its listing and metadata and removes the local copy of its contents. A released file loads back automatically when you access it. Only files already exported to the linked S3 bucket are released ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/file-release.html)).

### Appendix B. Measured weight loads and the GDS limits

**This appendix holds the measurements and limits behind the heavy-model loads.** None of them changes what you request, so they are not in the main text.

**Going deeper, by topic.**

- **The 15 GB benchmark of the Model Streamer.** The setup was Meta-Llama-3-8B, which weighs 15 GB in one Safetensors file. It ran on an AWS `g5.12xlarge` with one A10G GPU used, vLLM 0.5.5 and Model Streamer 0.6.0. The S3 bucket was in the same Region. The runs were cold starts ([Model Streamer benchmarks](https://github.com/dsx-ai-factory/model-streamer/blob/master/docs/src/benchmarks.md)).

| Source and setting | Load time to the GPU |
|---|---|
| S3, concurrency 4 | 28.24 s |
| S3, concurrency 16 | 8.45 s |
| S3, concurrency 32 | 4.88 s |
| S3, concurrency 64 | 5.01 s |
| GP3 SSD, concurrency 1 | 47.56 s |
| GP3 SSD, concurrency 16 | 14.34 s, "the limit of the GP3 SSD" at 1 GiB/s |

- **Time until vLLM is ready, in the 15 GB Model Streamer benchmark.** S3 with the streamer took 23.18 s. GP3 took 35.08 s, against 66.13 s with the Hugging Face Safetensors loader ([Model Streamer benchmarks](https://github.com/dsx-ai-factory/model-streamer/blob/master/docs/src/benchmarks.md)).
- **What the AWS sample adds.** The GDS arm moved bytes at 42.5 GB/s for 21 seconds. That is above the 37.5 GB/s that the file system provisions, which the sample says is possible only because the FSx file servers cache reads in memory. About 14 seconds went to placing roughly 150,000 tensors. In the S3 arm, the loading times of the ranks vary by up to 57 percent, and the 316.3 s comes from the progress bar of rank 0 ([AWS sample](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre)).
- **8 against 16 OSTs.** A file system with 16 OSTs and 75 GB/s loaded in 31.9 s (mean of 5 runs), against 35.5 s with 8 OSTs and 37.5 GB/s (mean of 3 runs). That is 10.1 percent faster for twice the cost ([AWS sample](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre)).
- **The instance types that FSx GDS allows.** The sample says the allowlist is currently p5.48xlarge, p5e.48xlarge, p5en.48xlarge, p6-b200.48xlarge and p6-b300.48xlarge. It also says the NVIDIA GPU Operator's GDS option cannot be used there, and that the serve pod runs privileged, because mounting the `nvidia-fs` devices makes them visible but does not let a non-privileged pod open them ([AWS sample known issues](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre/blob/main/KNOWN_ISSUES.md)).
- **The Model Streamer moved.** The Model Streamer README says that on September 13, 2026 the project was renamed and the repository moved. The PyPI package names and import paths are unchanged. The vLLM page still says "Run:ai Model Streamer" ([Model Streamer README](https://github.com/run-ai/runai-model-streamer), [vLLM](https://docs.vllm.ai/en/latest/models/extensions/runai_model_streamer/)).

### Appendix C. Transfer-time arithmetic

**This appendix holds the arithmetic behind the times on the pictures.** All of it is derived from sourced figures. None of it is a measured speed.

**Going deeper, by topic.**

- **Gigabytes to gigabits.** 1 byte is 8 bits. 140 GB is 1,120 Gb (derived). 1 TB is 8,000 Gb (derived).
- **140 GB of weights.** At 5 Gbps: 1,120 / 5 = 224 seconds. At 100 Gbps: 1,120 / 100 = 11.2 seconds. Both are illustrative rates.
- **Where the 5 Gbps figure comes from.** It is AWS's limit for one flow between instances that are not in one cluster placement group, a grouping of instances within a single zone ([AWS EC2 bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html)). The EC2 page does not say that a download from S3 or FSx is held to it, so using it here is this page's reading.
- **Where the 100 Gbps figure comes from.** It is S3's "up to" figure for data lake applications on a single instance, not a cap and not a promise ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)).
- **A rough rate to compare, derived.** The AWS sample reports 316.3 seconds for the S3 arm and 892.7 GB of weights. That gives 892.7 x 8 / 316.3 = about 22.6 Gbps (derived). The figure assumes that all 892.7 GB was read during those 316.3 seconds, which the sample does not say. The 316.3 seconds is the mean of three runs of 246, 351 and 352 seconds, read from the progress bar of rank 0, and the ranks vary by up to 57 percent. So this is not a measured network rate. It lies above the 5 Gbps single-flow figure and below the 100 Gbps figure. The run used 32 readers (`concurrency: 32`), so it was not one flow (this page's reading) ([AWS sample](https://github.com/aws-samples/sample-eks-vllm-gds-fsx-lustre)).
- **1 TB of training data from FSx for Lustre, one client.** The size is illustrative. At 100 Gbps: 8,000 / 100 = 80 seconds. At 700 Gbps with EFA: 8,000 / 700 = about 11.4 seconds. At 5 Gbps, the one-client to one-server figure: 8,000 / 5 = 1,600 seconds ([AWS FSx performance](https://docs.aws.amazon.com/fsx/latest/LustreGuide/performance.html)). These are derived from sourced rates. They are not measured loads.

### Appendix D. Lazy image loading in detail

**This appendix holds what the sources say about lazy image loading.** It does not change a manifest, so it is not in the main text.

**Going deeper, by topic.**

- **SOCI, the idea.** SOCI is a containerd snapshotter plugin that lets standard OCI images load lazily, without a build-time conversion step. The README cites a study from FAST '16: image download takes 76 percent of container startup time, and on average only 6.4 percent of the fetched data is needed to start. The study used a representative suite of images, not GPU images ([SOCI](https://github.com/awslabs/soci-snapshotter)).
- **SOCI index manifest versions.** Index Manifest v1 needs no conversion, but the README says that for production it creates the potential for performance changes. Index Manifest v2 uses a build-time conversion step ([SOCI](https://github.com/awslabs/soci-snapshotter)).
- **SOCI on Kubernetes.** The page opens with "SOCI on Kubernetes has a few rough edges". You need a containerd configuration and registry authentication. containerd below 1.7.16 does not enforce storage limits or run garbage collection for the snapshotter ([SOCI on Kubernetes](https://github.com/awslabs/soci-snapshotter/blob/main/docs/kubernetes.md)).
- **eStargz.** A container can run without waiting for the pull of the whole image, and the needed chunks arrive on demand. You need containerd configuration and a stargz snapshotter daemon on the node. The image still runs on runtimes that do not know eStargz ([eStargz](https://github.com/containerd/stargz-snapshotter)).
- **GKE Image streaming.** GKE streams data from eligible images as applications request it. It needs the Container File System API and images in Artifact Registry or Docker Hub. Images with duplicate layers are not supported, and you might not notice the benefit on the first pull ([GKE](https://cloud.google.com/kubernetes-engine/docs/how-to/image-streaming)).

### Appendix E. Training storage at Meta for Llama 3

**This appendix holds one company's numbers.** They describe Meta's own file system, not a cloud service, so they do not change what you request on Kubernetes.

**Going deeper, by topic.**

- **The file system.** The Llama 3 paper says Meta's Tectonic file system offers 240 PB of storage on 7,500 servers with SSDs. It supports a sustainable throughput of 2 TB/s and a peak of 7 TB/s ([Llama 3 paper](https://ar5iv.labs.arxiv.org/html/2407.21783)).
- **The checkpoint writes.** The paper calls the checkpoint writes highly bursty, saturating the storage fabric for short durations. Checkpointing saves each GPU's model state, from 1 MB to 4 GB per GPU ([Llama 3 paper](https://ar5iv.labs.arxiv.org/html/2407.21783)). This is one run and not a rule for other models.

### Appendix F. S3 Files numbers

**This appendix holds the numbers AWS states for S3 Files.** No S3 Files benchmark for training was found in the pages read. All figures are AWS's.

**Going deeper, by topic.**

- **How it reads.** S3 Files streams reads directly from the S3 bucket when the data is not on the file system's high-performance storage, and for large reads of 1 MiB or more. Small files, under 128 KiB by default, are loaded onto the file system's high-performance storage. Data not read for a window of 1 to 365 days, 30 by default, expires from that storage ([AWS S3 Files](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html)).
- **Speed.** AWS lists aggregate reads of up to terabytes per second, up to 250,000 read IOPS, and a maximum per-client read throughput of 3 GiB/s. Changes are copied to S3 about 60 seconds after the writes stop ([AWS S3 Files performance](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-performance.html)).
- **Limits.** The maximum file size is 48 TiB. There are no hard links, and all locks are advisory ([AWS S3 Files quotas](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-quotas.html)).
- **Price.** In AWS's example, large reads streamed from the bucket carry no S3 Files data access charge, but they still incur S3 GET request charges. The other rates are in AWS's example, and the price table for each Region loads by script and was not read ([AWS S3 pricing](https://aws.amazon.com/s3/pricing/), read 2026-11-22).

### Appendix G. Mountpoint and S3 speed settings

**This appendix holds the Mountpoint defaults and the S3 speed figures.** None of them is a field that you set in a manifest on this page, so they are not in the main text. The request rates for each prefix are in the decision table of the point "Choose by what the job does: a decision table" in the training data section.

**Going deeper, by topic.**

- **Mountpoint defaults.** Mountpoint sets its target network throughput to the available bandwidth when it runs on EC2. It serves up to 16 concurrent file or directory operations by default, and it splits files into parts of 8 MiB ([Mountpoint configuration](https://github.com/awslabs/mountpoint-s3/blob/main/doc/CONFIGURATION.md)).
- **S3 request advice.** S3 advises concurrent requests for byte ranges to fill the NIC ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance-design-patterns.html)).
- **The 100 Gbps figure.** S3 says data lake applications reach up to 100 Gbps on one instance ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html)). The 100 Gbps is an S3 statement about those applications and not a Mountpoint measurement.

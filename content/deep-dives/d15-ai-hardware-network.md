---
id: d-ai-path
title: "Where the time and the money go"
short: "AI hardware and network path"
---
**This deep dive is not specific to AWS.** It follows the path that data takes in AI work, from the memory inside one GPU out to other servers. A GPU (graphics processing unit) is the chip that does most of the computing in AI work. Each section opens with one Kubernetes question and keeps only what changes something you do on Kubernetes. The hardware is NVIDIA's, and AWS is the worked cloud example, because AWS publishes numbers for its instances. Short notes cover AMD, Google and AWS chips. EKS is Amazon Elastic Kubernetes Service and GKE is Google Kubernetes Engine. Both are managed Kubernetes services (plain words).

Every number has a source linked next to it. *Derived* means arithmetic on sourced numbers, shown in the text. *Illustrative* means picked to teach, not measured. Where no source was found, the page says so. The appendix holds the details that do not change what you do on Kubernetes: the hardware numbers, the all-reduce arithmetic, the topology codes, the NCCL variables, the side notes in full, and the words and units. Words and units are in [Appendix F](#d-ai-path-appendix-f-words-and-units).

### The distance ladder, and what Kubernetes can see at each rung

**The Kubernetes question.** A training job ran well on one 8-GPU server and got slower on four. Which step of the data path got slower, and which of those steps can you change from a pod spec, a node setting or a scheduler rule?

**Scenario.** This scenario is illustrative. A team trains a model on one server with 8 GPUs, and the job runs well. They move it to four servers to finish sooner, and each step takes longer. They need a map of the path between the GPUs, and they need to know which parts of it Kubernetes can see.

![The distance ladder](diagrams/sections/d15-1.svg)

**What the picture shows.** One rung for each step along the path, drawn for one H100 server (AWS P5). The bars show the sourced or derived speed of the first four rungs on a log scale, so each tick is 10 times the one before. Rungs 5, 6 and 7 have no speed figure from any source read for this page.

The right column of the picture shows what Kubernetes sees or sets at each rung, and a footnote in the picture explains its words. GB/s means gigabytes per second, and Gbps means gigabits per second. [Appendix A](#d-ai-path-appendix-a-hardware-numbers-by-gpu-generation-and-aws-instance) lists the speeds per GPU model.

- **Rung 1, inside the GPU.** HBM (high-bandwidth memory) is the memory on the GPU chip, and it is the fastest step.
- **Rung 1, what Kubernetes sees.** Kubernetes sees a GPU count ([How GPUs are advertised](#d06-2)).
- **Rung 1, GPU Feature Discovery.** It is an NVIDIA add-on that labels each node with facts about its GPUs, such as the model ([NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin)).
- **Rung 1, what a pod asks for.** A pod asks for whole GPUs or, with MIG, for slices of a GPU. MIG (Multi-Instance GPU) is NVIDIA's way to cut one GPU into up to seven smaller ones ([NVIDIA MIG guide](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/introduction.html), [MIG](#d06-5)).
- **Rung 2, GPU to GPU in one server.** NVLink is NVIDIA's own link between GPUs. NVSwitch is a switch chip that lets every GPU reach every other one.
- **Rung 2, what Kubernetes sees and sets.** Kubernetes sees only the GPU count, not the links. So you keep chatty pods, which are pods that exchange data often, on one node ([Topology bin-packing](#d02-3)).
- **Rung 3, GPU to CPU.** PCIe (PCI Express) is the general bus that links the CPU to GPUs, NICs and drives. A NIC (network interface card) connects a server to the network.
- **Rung 3, what a device plugin tells Kubernetes.** A device plugin is a program on a node that tells the kubelet which devices the node has. The kubelet is the agent that starts pods on that node ([Kubernetes device plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)).
- **Rung 3, NUMA node.** A plugin can also report each device's NUMA node, which is a group of CPU cores with the memory closest to them ([Kubernetes device plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)).
- **Rung 3, what a DRA driver adds.** DRA (Dynamic Resource Allocation) lets a pod ask for devices by their properties. A DRA driver can publish each device's PCIe root, the top of one PCIe tree, where the CPU and memory meet the devices ([Kubernetes DRA attributes](https://kubernetes.io/docs/reference/node/dra-standard-device-attributes/)).
- **Rung 3, what Kubernetes sets.** The kubelet's Topology Manager can align a pod's CPUs and devices on one NUMA node ([Topology Manager](https://kubernetes.io/docs/tasks/administer-cluster/topology-manager/)). A DRA claim on `pcieRoot` keeps devices under one PCIe root. [Inside the server](#d15-3) shows both.
- **Rung 4, server to server.** On AWS, a p5.48xlarge lists 3,200 Gbps of network ([AWS](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)). The picture also shows 50 GB/s per GPU, which is derived: 3,200 Gbps divided by 8 GPUs and by 8 bits per byte.
- **Rung 4, what the pod asks for on AWS.** A pod must ask for the fast device. On AWS that is an EFA (Elastic Fabric Adapter, AWS's NIC for AI work). The pod asks as a count from the EFA device plugin or as a claim on the EFA DRA driver ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **Rung 4, what the pod asks for with InfiniBand or RoCE.** InfiniBand is a network type built for fast, direct links between servers. RoCE (RDMA over Converged Ethernet) carries the same kind of traffic over Ethernet. RDMA (Remote Direct Memory Access) moves data straight into another machine's memory (plain words). The pod asks for one unit of an RDMA pool, which is a named set of RDMA devices ([Leaving the server](#d15-4)).
- **Rung 5, same rack or network block.** No speed figure was found. "Network block" is this page's plain word for servers that sit close together on the data-center network.
- **Rung 5, what Kubernetes sees.** On EKS, labels on each node report its position in the AWS network, but only on supported instance types. Other types carry no such labels. An affinity rule, which is a scheduling rule that keeps pods together, can use those labels ([AWS EKS topology](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)). [Racks and network blocks](#d10-6) in Deep dive 10 shows the labels and the rule that keep a job's pods together.
- **Rung 5, placement groups.** A region is a geographic area made of several zones, and a zone is a separate location inside a region. A cluster placement group is a logical grouping of instances within one zone. Its instances are not isolated to one rack ([AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-strategies.html)). [Racks and network blocks](#d10-6) in Deep dive 10 explains why a placement group is not a rack.
- **Rung 6, other zone in the same region.** No speed figure was found. EFA traffic cannot cross zones or VPCs, which are your private networks in AWS ([AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **Rung 6, cost.** Data between zones costs $0.01/GB in each direction ([AWS pricing](https://aws.amazon.com/ec2/pricing/on-demand/)). [Zones](#d10-7) in Deep dive 10 shows what a crossing costs for each kind of data.
- **Rung 6, what Kubernetes sees.** The node label `topology.kubernetes.io/zone` is filled in from the cloud provider's information, and only when a cloud provider is used. Without one, you can set it yourself ([Kubernetes](https://kubernetes.io/docs/reference/labels-annotations-taints/)). [Latency and EFA](#d10-5) says to keep a job's nodes in one zone. [Zones](#d10-7) in Deep dive 10 shows the fields that keep a job, its storage and its serving replicas in the right zone.
- **Rung 7, other region and the WAN.** The WAN is the wide-area network that links regions. No figure was found for this page. Kubernetes describes the node label `topology.kubernetes.io/region` by pointing to the zone label ([Kubernetes](https://kubernetes.io/docs/reference/labels-annotations-taints/)). This page's reading: the same rule holds, so the label is set from the cloud provider, and only when one is used. [Sites and capacity sources](#ds18-2) in Design 18 covers network differences between sites. [Regions, the WAN and failover](#d10-8) in Deep dive 10 covers what must cross to another Region and what must already be there.

**Going deeper, by topic.**

- **Latency and throughput.** No latency figure was found for any rung, so this page gives none. AWS's two latency statements are in [Appendix A](#d-ai-path-appendix-a-hardware-numbers-by-gpu-generation-and-aws-instance). The two words are explained in [Appendix F](#d-ai-path-appendix-f-words-and-units).
- **Cost.** Two things cost money here: moving data, and GPUs that wait for data. Rung 6 gives the price of data between zones. [Where GPU-hours go](#ds17-1) in Design 17 covers the waiting GPUs. [Training data and checkpoints](#d16-1) in Deep dive 16 covers GPUs that wait for storage.
- **The rule: keep chatty traffic on the nearest rung.** Chatty traffic is data that moves often or in large amounts. This page's advice for Kubernetes: keep it on one node first, then one network block, then one zone.
- **Where to look when a job is slow, stuck or costly.** [Symptom to rung](#d17-3) in Deep dive 17 lists symptoms and the rung of this ladder that each one points to.
- **One slow rung sets the pace.** AMD tested 8 of its MI300X GPUs and says the slowest link limits a GPU's total available bandwidth ([AMD ROCm blog](https://rocm.blogs.amd.com/software-tools-optimization/mi300x-rccl-xgmi/README.html)). This page's reading: a job in which all GPUs must agree on a result is only as fast as its slowest link ([Stragglers, hangs and silent errors](#ds14-9) in Design 14).
- **Side note, AMD Instinct.** Instinct is AMD's line of data-center GPUs. AMD's device plugin exposes them as an `amd.com/gpu` count, and AMD's GPU DRA driver (beta) publishes each GPU's PCIe root ([AMD device plugin](https://instinct.docs.amd.com/projects/k8s-device-plugin/en/latest/), [AMD ROCm blog](https://rocm.blogs.amd.com/software-tools-optimization/dra-gpu/README.html)). Numbers: [Appendix E](#d-ai-path-appendix-e-side-notes-in-full-amd-instinct-google-tpu-and-aws-trainium).
- **Side note, Google TPU.** A TPU is Google's AI chip. On GKE, a pod asks for TPU chips with the resource `google.com/tpu`, and all slices of a Multislice job must share the same TPU type, size and topology ([GKE Multislice](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/tpu-multislice)). Numbers: [Appendix E](#d-ai-path-appendix-e-side-notes-in-full-amd-instinct-google-tpu-and-aws-trainium).
- **Side note, AWS Trainium.** Trainium is AWS's own AI chip. On EKS, the Neuron device plugin advertises `aws.amazon.com/neuron` as a count, and AWS recommends the Neuron DRA driver for new deployments on Kubernetes 1.34 or later ([AWS EKS Neuron](https://docs.aws.amazon.com/eks/latest/userguide/device-management-neuron.html)). Numbers: [Appendix E](#d-ai-path-appendix-e-side-notes-in-full-amd-instinct-google-tpu-and-aws-trainium).

### What a training job asks of the cluster

**The Kubernetes question.** A job needs 32 GPUs. Which of its pods must share a server, which must share the fast network, and which can sit farther apart?

**Scenario.** This scenario is illustrative. A team starts a job on 4 servers with 8 GPUs each, with one pod on each server. The scheduler places one of the four pods in a different network block from the others. Every step is now as slow as the path to that one pod.

![What a training job asks of the cluster](diagrams/sections/d15-2.svg)

**What the picture shows.** One row for each way to split the work of a training job. Each row says what the split sends, how often, and how close its pods must be. Each answer is marked as sourced, as this page's reading, as a statement on this site that cites no source, or as not stated.

The three placement columns are: same server (NVLink speed), same network block (the fast network), and farther apart. A cell is shaded when the pods must sit that way. No source read says how far apart any of these splits may sit. The footnote of the picture explains its words.

- **Rank and NCCL.** A rank is one GPU taking part in the job. NCCL is NVIDIA's library that runs collectives between GPUs, and it gives each of its n GPUs "a unique rank between 0 and n-1" ([NCCL docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/communicators.html)). On AMD GPUs the counterpart is RCCL.
- **Collective.** A collective is one operation that all ranks do together ([NCCL docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/collectives.html)).
- **All-reduce.** An all-reduce adds the values of all ranks, element by element, and gives every rank the same sum ([NCCL docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/collectives.html)).
- **All-gather.** An all-gather gives every rank the pieces of all ranks, joined in rank order ([NCCL docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/collectives.html)).
- **Reduce-scatter.** A reduce-scatter adds the values, and each rank keeps one piece of the sum. A reduce-scatter followed by an all-gather gives the same result as an all-reduce ([NCCL docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/collectives.html)).
- **DDP: what it sends.** DDP (PyTorch's DistributedDataParallel) is data parallelism. Every rank holds a full copy of the model and works on its own batch. After the backward pass, which computes the gradients, an all-reduce syncs the gradients of all ranks, once per batch ([PyTorch FSDP tutorial](https://docs.pytorch.org/tutorials/intermediate/FSDP_tutorial.html), [Megatron-LM paper](https://ar5iv.labs.arxiv.org/html/2104.04473)). A gradient says how to change one parameter, and a parameter is a number the model learns. The words are in [Appendix F](#d-ai-path-appendix-f-words-and-units).
- **DDP: where its pods sit.** Every rank takes part in every all-reduce, so all pods of the job need the same fast network. None can sit farther away than the others. This page's advice: gang-schedule the job. [The distance ladder](#d15-1) says why one slow link slows the whole job.
- **FSDP: what it sends.** FSDP (Fully Sharded Data Parallel) holds only a shard on every rank, which is one piece of the model's data. A layer is one stage of the model. Before the forward pass of each layer, which is the model computing its output, and before its backward pass, an all-gather collects the shards into whole parameters. In backward, a reduce-scatter returns the gradients as shards ([PyTorch FSDP tutorial](https://docs.pytorch.org/tutorials/intermediate/FSDP_tutorial.html)). It sends pieces many times per step.
- **FSDP: where its pods sit.** The ranks are the same as in DDP, so the same rule holds: all pods on the fast network. The all-gather for the next layer can overlap with the current layer. The first all-gather is exposed ([PyTorch FSDP tutorial](https://docs.pytorch.org/tutorials/intermediate/FSDP_tutorial.html)). This page's reading: a layer waits for its all-gather, so a slow link shows up as idle GPUs sooner than in DDP.
- **Hybrid sharding: what it sends.** PyTorch defines hybrid sharding (HSDP) as FSDP within a host and DDP across hosts ([PyTorch DeviceMesh recipe](https://docs.pytorch.org/tutorials/recipes/distributed_device_mesh.html)).
- **Hybrid sharding: where its pods sit.** A shard group is the ranks that hold the pieces of one copy of the model. This page's reading: put each shard group on one server. Then the many small FSDP collectives stay on NVLink, and only the traffic between the copies of the model crosses the network.
- **Tensor parallelism: what it sends.** It splits the calculation of each layer across GPUs. An all-reduce for every microbatch combines the results. A microbatch is one of the smaller pieces that a batch is split into ([Megatron-LM paper](https://ar5iv.labs.arxiv.org/html/2104.04473)).
- **Tensor parallelism: where its pods sit.** The Megatron-LM paper says this can be expensive across servers. Its first takeaway is to use tensor parallelism up to the number of GPUs in one server ([Megatron-LM paper](https://ar5iv.labs.arxiv.org/html/2104.04473)). So its pods stay on one server.
- **Pipeline parallelism: what it sends.** It cuts the model into stages. Tensors, which are blocks of numbers, flow from stage to stage, point to point, for every microbatch in the forward and the backward pass ([Megatron-LM paper](https://ar5iv.labs.arxiv.org/html/2104.04473)).
- **Pipeline parallelism: where its stages sit.** The paper says to use pipeline parallelism to scale to larger models across servers ([Megatron-LM paper](https://ar5iv.labs.arxiv.org/html/2104.04473)). So its stages sit on different servers.

**Going deeper, by topic.**

- **Rank and pod.** `torchrun` is PyTorch's launcher. It starts the number of processes per node that you give it with `--nproc-per-node`, and in GPU training each process works on one GPU ([PyTorch](https://docs.pytorch.org/docs/2.15/elastic/run.html)). This page's reading: a pod that runs `torchrun` with 8 processes on 8 GPUs holds 8 ranks.
- **NCCL and Kubernetes.** NCCL moves data over PCIe, NVLink, InfiniBand or IP sockets, which are ordinary network connections. It looks at how the GPUs and NICs are connected ([NCCL overview](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/overview.html)). Kubernetes meets NCCL through the devices a pod gets and through environment variables ([Leaving the server](#d15-4)).
- **The step by step and the arithmetic.** The step by step of DDP and FSDP, and the all-reduce arithmetic, are in [Appendix B](#d-ai-path-appendix-b-all-reduce-ring-and-tree-ddp-and-fsdp-in-detail).
- **Tensor and pipeline parallelism on this site.** [Deep dive 3](#d02-3) covers tensor and pipeline parallelism, and [Design 20](#ds20-4) puts the stages within one rack. Neither cites a source, so the picture marks those cells "site page, no source".
- **Which collectives hit which rung.** Illustrative: a group of 8 GPUs in one server crosses only NVLink or PCIe. A group of 64 GPUs on 8 servers also crosses the network. This page's reading: the more servers a group spans, the more rungs of [the distance ladder](#d15-1) its collectives cross.
- **Ask for the whole group at once.** Use a gang scheduler, which starts all pods of a job together, or none ([Gang scheduling](#d02-2), [Topology-aware gang placement](#ds17-4) in Design 17).
- **Ask for the same server.** Kubernetes's own pod affinity example uses the topology key `kubernetes.io/hostname` to place a replica on a node that already runs a pod with a given label. Kubernetes says the value of that label equals the node name only in some environments ([Kubernetes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)). A topology-aware scheduler can also do it ([Topology bin-packing](#d02-3)).
- **Ask for the same network block.** On EKS, a pod affinity rule can use the bottom-layer network label of a node as its topology key. Then the pods of a job land in the same network domain ([AWS EKS topology](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)). [Racks and network blocks](#d10-6) in Deep dive 10 covers the labels and the rule for this ask.
- **Worked manifests.** [Worked examples](#d17-2) in Deep dive 17 puts these asks into four manifests, and names the section that explains each field.

### Inside the server: what the scheduler and kubelet can do

**The Kubernetes question.** A pod asked for one GPU and one NIC and got two that sit under different CPUs. How does Kubernetes know where devices sit, and how do you keep them together?

**Scenario.** This scenario is illustrative. A pod asks for 1 GPU and 1 NIC, and it gets GPU 0 and NIC 4 of an 8-GPU server. The server has 2 CPUs, and the GPU and the NIC sit under different CPUs. The job runs, but the data between them crosses the link between the CPUs. Kubernetes counted the devices, but it did not know where they sit ([How GPUs are advertised](#d06-2)).

![Inside the server](diagrams/sections/d15-3.svg)

**What the picture shows.** A generic 8-GPU server with 2 CPUs, 4 PCIe switches, 8 NICs and an NVSwitch. A PCIe switch is a chip that lets several devices share one upstream PCIe link. **The layout is illustrative: real servers differ, so run `nvidia-smi topo -m` on yours.**

The labels are NVIDIA's path codes, and the four paths are drawn as thick dashed lines. The box named On Kubernetes in the picture sums up the Topology Manager, NUMA node and `pcieRoot` points of this section.

- **NVSwitch joins the GPUs of one server.** NVIDIA says that in an 8-GPU HGX H100 server, every GPU connects to all of the server's NVSwitch chips. HGX is NVIDIA's server platform ([NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing)). This page's reading: a pod that holds several GPUs of such a server gets NVLink between them.
- **GPU 0 to NIC 0, `PIX`.** Both sit under the same PCIe switch, so the path has at most one PCIe bridge, which is one hop on the PCIe path. NVIDIA's advice for a GPU and its NIC is `PIX` or `PXB`. `PXB` means several PCIe bridges without the host bridge ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)). This page's advice: a pod that uses GPU 0 for network traffic should get NIC 0.
- **GPU 0 to NIC 2, `PHB`.** The NIC sits under another PCIe switch of the same CPU, so the data goes up through the CPU's host bridge and down again. A host bridge is the part that connects a PCIe tree to the CPU. NVIDIA calls this acceptable if both devices share a host bridge and a NUMA node ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **GPU 0 to NIC 4, `SYS`.** This is the pair the pod got in the scenario. The NIC sits under CPU 1, so the data crosses the link between the two CPU sockets. NVIDIA's legend calls that link QPI or UPI. NVIDIA's advice is to avoid `SYS` for a GPU and its NIC ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **GPU 0 to GPU 1, `NV#`.** `NV#` means that a bonded set of NVLinks joins the two GPUs ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)). This page's reading: a pod that holds both gets NVLink speed between them.
- **Not in the picture.** The codes `PXB` and `NODE` need a different layout. [Appendix C](#d-ai-path-appendix-c-reading-the-topology-matrix) explains all seven codes.

**Going deeper, by topic.**

- **What a device plugin tells Kubernetes.** The scheduler sees an integer count that cannot be overcommitted ([Kubernetes device plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)). [How GPUs are advertised](#d06-2) explains it.
- **The check: `nvidia-smi topo -m`.** The command prints a table with one row and one column for each GPU and NIC. Each cell is one of NVIDIA's path codes ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)). Run it on the node to see where your devices sit.
- **GPUDirect RDMA.** GPUDirect RDMA is a direct path for data between a GPU and a peer device such as a NIC, using standard PCIe features ([NVIDIA GPUDirect RDMA](https://docs.nvidia.com/cuda/gpudirect-rdma/index.html)).
- **What good looks like for GPUDirect RDMA.** NVIDIA says a path through only PCIe switches "yields the best performance". A path across the CPU link may be "extremely performance-limited or even not work reliably" ([NVIDIA GPUDirect RDMA](https://docs.nvidia.com/cuda/gpudirect-rdma/index.html)).
- **What good looks like in `topo -m` terms.** NVIDIA's legend says to aim for `PIX` or `PXB` and to avoid `NODE` and `SYS` ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)). `NODE` means PCIe plus the link between host bridges inside one NUMA node. Mapping "only PCIe switches" to `PIX` and `PXB` is this page's reading.
- **What NCCL does with the topology.** NCCL detects the topology itself and picks the best path, unless a variable says otherwise ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)). The variables are in [Appendix D](#d-ai-path-appendix-d-nccl-variables-in-full).
- **Topology Manager: what it does.** Without it, the kubelet's CPU Manager and Device Manager choose "independently". A pod can then get CPUs and a device from different NUMA nodes ([Topology Manager](https://kubernetes.io/docs/tasks/administer-cluster/topology-manager/)). [NUMA and CPU pinning](#d08-5) shows the kubelet setup.
- **Topology Manager: its policy.** The default policy is `none`, so alignment is off until you set a policy. The others are `best-effort`, `restricted` and `single-numa-node` ([Topology Manager](https://kubernetes.io/docs/tasks/administer-cluster/topology-manager/)).
- **Topology Manager: its scope.** The scope is `container` (the default) or `pod`, which aligns all containers of a pod together. The CPU Manager must also be on, and its `static` policy needs a CPU reservation above zero ([Topology Manager](https://kubernetes.io/docs/tasks/administer-cluster/topology-manager/), [CPU management policies](https://kubernetes.io/docs/tasks/administer-cluster/cpu-management-policies/)).
- **The limit of Topology Manager.** This page's reading: a NUMA node can hold several PCIe switches, so alignment by NUMA node alone does not say which GPU sits next to which NIC.
- **DRA and the `pcieRoot` attribute.** A claim can use `matchAttribute` on `resource.kubernetes.io/pcieRoot` to place devices under one PCIe root "for lowest-latency communication" ([Kubernetes DRA attributes](https://kubernetes.io/docs/reference/node/dra-standard-device-attributes/)). [Dynamic Resource Allocation](#d06-4) explains the claim.
- **EFA and GPU alignment on EKS: the device plugin.** AWS says the EFA device plugin aligns the EFA devices it hands out with their topologically-local GPUs. This holds on the EKS-optimized Amazon Linux 2023 accelerated AMIs, which are the disk images that nodes boot from ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **EFA and GPU alignment on EKS: the DRA driver.** AWS says its EFA DRA driver can pair EFA interfaces with their topologically-local GPUs or Neuron devices. AWS shows a claim that places an EFA device and a GPU together ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **NVLink domain.** An NVLink domain is a group of GPUs that NVLink joins directly. In NVIDIA's GB200 NVL72 system, 72 GPUs form one domain ([NVIDIA GB200 NVL72](https://www.nvidia.com/en-us/data-center/gb200-nvl72/)). [Appendix A](#d-ai-path-appendix-a-hardware-numbers-by-gpu-generation-and-aws-instance) explains the name.
- **UltraServers on AWS.** AWS sells the GB200 NVL72 as UltraServers. A p6e-gb200.36xlarge has 4 GPUs and is only sold in an UltraServer, and a u-p6e-gb200x72 has 18 of them, which makes 72 GPUs (derived: 18 x 4) ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)).
- **UltraServers on AWS: how you get them.** AWS says P6e-GB200 UltraServers are made available through EC2 Capacity Blocks for ML ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)). A Capacity Block lets you reserve GPU instances on a future date, and the instances in it are placed close together in an EC2 UltraCluster ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)). This page's reading: nodes from a reservation appear at its start date, so plan the cluster setup for that date. The prices and the other AWS sources are in [Appendix A](#d-ai-path-appendix-a-hardware-numbers-by-gpu-generation-and-aws-instance).
- **UltraServers on EKS: DRA is required.** AWS requires DRA on Kubernetes 1.33 or later ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)).
- **UltraServers on EKS: the ComputeDomain.** A ComputeDomain is an NVIDIA DRA driver resource that describes an IMEX (Internode Memory Exchange) domain. The driver sets up the IMEX channels before the workload starts. AWS calls one ComputeDomain for each job a best practice ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)).
- **UltraServers on EKS: the clique label.** GPU Feature Discovery labels each node `nvidia.com/gpu.clique`. Nodes with the same value can reach each other over multi-node NVLink ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)).
- **UltraServers on EKS: keep pods in one NVLink domain.** You can use pod affinity on the clique label to keep pods in one NVLink domain ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)).
- **UltraServers on EKS: across UltraServers.** AWS says traffic can cross UltraServers over EFA, but one UltraServer is fastest ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)).
- **UltraServers on EKS: Auto Mode and Karpenter.** EKS Auto Mode automates compute, networking, load balancing, storage and identity and access management for a cluster ([AWS EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/auto-accelerated.html)). Karpenter is an open-source project that provisions nodes for pods that the scheduler marked unschedulable ([Karpenter](https://karpenter.sh/docs/)).
- **UltraServers on EKS: node groups and self-managed nodes.** Managed node groups automate the provisioning and lifecycle of nodes ([AWS EKS managed node groups](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html)). Self-managed nodes are nodes you set up yourself.
- **UltraServers on EKS: dynamic and static capacity.** The AWS guide does not define these two words. This page's reading: dynamic means the tool creates nodes when pods are waiting, and static means you set the node count in advance.
- **UltraServers on EKS: how to get the nodes.** AWS says neither EKS Auto Mode nor Karpenter supports dynamic capacity provisioning for ComputeDomains. Use Karpenter static capacity, EKS managed node groups or self-managed nodes ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)).
- **MIG and NCCL.** AWS says NCCL does not work with MIG ([AWS EKS MIG guide](https://docs.aws.amazon.com/eks/latest/userguide/device-management-nvidia-mig.html)). NCCL calls its own MIG support "experimental" ([NCCL docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/communicators.html)). AWS words it more strictly than NCCL does. This page's advice: use whole GPUs for training that spans GPUs. [Different GPUs in one server, and MIG](#d06-5) explains MIG.

### Leaving the server: what an EFA, InfiniBand or RoCE pod asks for

**The Kubernetes question.** A two-server job runs no faster than expected, and the log shows plain TCP. What must the pod and the node provide so that NCCL uses EFA, InfiniBand or RoCE, and how do you check which one it used?

**Scenario.** This scenario is illustrative. A job runs well on one server and slows down on two. The logs show that NCCL did not use the fast NIC. It fell back to ordinary TCP connections through the operating system. Design 18 describes the same failure ([A portable environment](#ds18-7)).

![What leaves the server](diagrams/sections/d15-4.svg)

**What the picture shows.** Two servers joined by a network. The picture does not show the switches between the servers, and this page has no sourced facts about them. The data leaves GPU 0 of Server A through its NIC 0 and arrives at NIC 0 of Server B. This pairing is an example.

The picture draws four paths. Its right column shows what the pod asks for, and its footnote explains the short words.

- **EFA on AWS: how NCCL reaches it.** NCCL calls the `aws-ofi-nccl` plug-in. The plug-in uses Libfabric, the software layer that programs call to reach a fast NIC, to reach the EFA device ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **EFA on AWS: where its traffic can go.** EFA uses SRD (Scalable Reliable Datagram), an AWS network protocol. EFA traffic stays in one zone and one VPC ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **InfiniBand.** NCCL uses its InfiniBand Verbs transport, which is the programming interface for InfiniBand and RoCE adapters. `NCCL_IB_HCA` chooses the NIC ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)). The pod asks for an RDMA pool ([The distance ladder](#d15-1) defines the words).
- **RoCE.** NCCL uses the same transport and variable as for InfiniBand ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)). The pod asks for an RDMA pool too.
- **The fallback, IP sockets.** The data goes through the operating system's TCP/IP stack, its normal network software. `NCCL_SOCKET_IFNAME` picks the interface, and `NCCL_IB_DISABLE` forces this path ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)). This page's reading: you get it when the pod lacks the fast device.
- **GPUDirect RDMA in this picture.** The orange line is the GPUDirect RDMA path from [Inside the server](#d15-3). Its node needs two checks: the memory-sharing method (DMA-BUF), and the IOMMU, which is the part that translates the memory addresses that devices use. The two points "GPUDirect RDMA on the node" in Going deeper explain them.

**Going deeper, by topic.**

- **EFA on EKS: network interfaces.** A network interface is a virtual network card inside your VPC ([AWS ENI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html)). A subnet is a range of IP addresses in your VPC (plain words).
- **EFA on EKS: `efa-only`.** AWS recommends the interface type `efa-only` for every network interface except the primary one. Plain `efa` interfaces use up subnet IP addresses ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). [Latency and EFA](#d10-5) shows the setup.
- **What an EFA pod asks for.** The pod asks for `vpc.amazonaws.com/efa` and `hugepages-2Mi`. The full pod is in [Latency and EFA](#d10-5).
- **Huge pages.** Huge pages are memory in larger pages, and a node must set some aside before it can report them ([Kubernetes](https://kubernetes.io/docs/tasks/manage-hugepages/scheduling-hugepages/)).
- **Huge pages and a Pending pod.** AWS says a pod that asks for hugepages on a node that has none stays `Pending` ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **What an EFA image needs.** The image needs `aws-ofi-nccl`, which needs libfabric and hwloc ([aws-ofi-nccl](https://raw.githubusercontent.com/aws/aws-ofi-nccl/master/README.md)). Plain words, not from that README: hwloc is a library that finds out how the machine's hardware is laid out. The plug-in supports RCCL as well as NCCL, so AMD GPUs can use it too.
- **How many EFA devices the pod asks for.** It depends on the instance. AWS says an instance can have only one EFA interface per network card ([AWS ENI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html)), so the number of network cards is the most EFA devices the instance can have (derived). AWS's table lists these network card counts: 4 on p4d and p4de, 32 on p5 and p5e, 16 on p5en, 8 on p6-b200 and 17 on p6e-gb200 ([AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)). AWS's own NCCL test on a p5.48xlarge asks for 32. The full table, with the limit on interfaces per instance, is in [Appendix A](#d-ai-path-appendix-a-hardware-numbers-by-gpu-generation-and-aws-instance).
- **Device plugin or DRA driver for EFA.** The device plugin gives an integer count on all EKS versions. The DRA driver publishes each device's PCIe location and needs Kubernetes 1.34 or later. AWS recommends DRA for new deployments, and the two cannot run on the same node ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **Check that NCCL really uses EFA.** Run with `NCCL_DEBUG=INFO` and look for the log line `NCCL INFO NET/OFI Selected Provider is efa*` ([AWS: NCCL on EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-start-nccl.html)). [Latency and EFA](#d10-5) shows the same check.
- **InfiniBand and RoCE: the Network Operator.** The NVIDIA Network Operator is a Kubernetes add-on. It installs the networking driver, the device plugin, the add-ons that connect pods to networks, and IP address management, for RDMA over InfiniBand and RoCE ([NVIDIA Network Operator](https://docs.nvidia.com/networking/display/kubernetes2570/index.html)).
- **InfiniBand and RoCE: the device plugin.** The RDMA shared device plugin supports both kinds of adapter and advertises an extended resource for RDMA devices. Sharing it "does not create a VF or dedicate a NIC to the pod" ([RDMA shared device plugin](https://raw.githubusercontent.com/Mellanox/k8s-rdma-shared-dev-plugin/master/README.md)). A VF (virtual function) is one virtual NIC that SR-IOV, a way to split one physical NIC into several virtual ones, creates.
- **InfiniBand and RoCE: what NVIDIA's example pod asks for.** It asks for one unit of an RDMA pool. It adds the `IPC_LOCK` capability, which is a Linux permission that lets a container lock memory ([NVIDIA Network Operator](https://docs.nvidia.com/networking/display/kubernetes2670/quick-start/ipoib-rdma-shared.html), [Linux man page](https://man7.org/linux/man-pages/man7/capabilities.7.html)). It is an example for IP over InfiniBand, and no RoCE pod example was read for this page.
- **InfiniBand and RoCE: how the YAML is adapted.** The pod name and image are changed, and the annotation that attaches a secondary network is left out. The pool name `rdma_shared_device_a` is NVIDIA's example name. Your names come from your `NicClusterPolicy`, the object that configures the Network Operator.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: rdma-example
spec:
  containers:
  - name: worker
    image: example.com/my-training-image:latest      # illustrative: your own image
    securityContext:
      capabilities:
        add: ["IPC_LOCK"]                             # in NVIDIA's example #!
    resources:
      requests:
        rdma/rdma_shared_device_a: 1                  # pool name from NVIDIA's example #!
      limits:
        rdma/rdma_shared_device_a: 1
```

- **EFA and RDMA side by side, on Kubernetes.** The table lists what each flavour asks of the cluster.

| | EFA on AWS | InfiniBand or RoCE |
|---|---|---|
| Resource the pod asks for | `vpc.amazonaws.com/efa` (device plugin) or a DRA claim | an RDMA pool such as `rdma/rdma_shared_device_a` |
| Memory settings | `hugepages-2Mi` | `IPC_LOCK` in NVIDIA's example pod |
| Who installs the device support | EFA device plugin or EFA DRA driver | NVIDIA Network Operator |
| NCCL reaches the NIC through | the `aws-ofi-nccl` plug-in and Libfabric | NCCL's InfiniBand Verbs transport |

- **Locked memory: what is not known.** `memlock` is a Linux limit on how many bytes of memory a process may lock into RAM ([Linux man page](https://man7.org/linux/man-pages/man2/getrlimit.2.html)). No AWS page read for this page says an EFA pod needs `memlock` or `IPC_LOCK`, so none is added to the EFA pod.
- **Shared memory for the AWS NCCL test.** AWS's NCCL test on a p5.48xlarge asks for all 32 EFA devices and mounts the host's `/dev/shm` ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). Plain words, not from that page: `/dev/shm` is a memory-backed area that processes on one host share. No official source read states a size rule for it.
- **NCCL variables that fit in a pod's environment.** `NCCL_SOCKET_IFNAME` chooses the IP interfaces, `NCCL_IB_HCA` chooses the RDMA adapters, and `NCCL_DEBUG=INFO` shows which network NCCL picked. NCCL's documentation files all three under "System configuration", not under "Debugging" ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)). Values and the other variables: [Appendix D](#d-ai-path-appendix-d-nccl-variables-in-full).
- **Debugging variables are for tests.** NCCL files `NCCL_P2P_LEVEL`, `NCCL_NET_GDR_LEVEL` and `NCCL_IB_DISABLE` under "Debugging". They "should not be used in production nor retained in scripts, or only as workaround" ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **GPUDirect RDMA on the node: the memory-sharing method.** NVIDIA recommends DMA-BUF, a Linux kernel feature that lets devices share a memory buffer. It needs the open kernel module driver, and NCCL uses it by default when the kernel, GPU driver and network driver support it. The older method is the `nvidia-peermem` kernel module, which the GPU Operator then does not need to build and load ([NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-operator-rdma.html)).
- **GPUDirect RDMA on the node: the IOMMU.** The IOMMU translates the memory addresses that devices use. NVIDIA says GPUDirect RDMA is incompatible with any translation other than 1:1, so the IOMMU must be disabled or set to pass-through ([NVIDIA GPUDirect RDMA](https://docs.nvidia.com/cuda/gpudirect-rdma/index.html)). NCCL's docs add that the IOMMU can redirect peer-to-peer traffic to the CPU and cut performance ([NCCL docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/gpu_troubleshooting.html)).
- **Which AWS instances support GPUDirect RDMA.** The list is in [Appendix A](#d-ai-path-appendix-a-hardware-numbers-by-gpu-generation-and-aws-instance).
- **How to compare a measured speed with the hardware.** nccl-tests reports a "bus bandwidth" that can be compared with the hardware's peak ([Appendix B](#d-ai-path-appendix-b-all-reduce-ring-and-tree-ddp-and-fsdp-in-detail)). Design 14 runs such a test in its burn-in check ([Node health: burn-in](#ds14-3)).
- **Related pages.** Per-site NCCL settings: [The Kubernetes execution operator](#ds18-5) in Design 18. The three data paths in a pod: [Standard vs. SR-IOV vs. RDMA](#d10-4). What the platform runtime carries: [Training: the job lifecycle](#ds03-3) in Design 03. What each workload family needs: [Design 03](#design-training-inference).

### Appendix A. Hardware numbers by GPU generation and AWS instance

**This appendix holds the hardware numbers that the main body leaves out.** Each GPU generation has its own point with one table. The GPU comes first, and the AWS instance is in parentheses. Each row links its source, and "not read" means no source was read for this page. None of these numbers changes what you request or configure, so none is in the main body.

**Going deeper, by topic.**

- **HGX and DGX: NVIDIA's server platforms.** HGX is NVIDIA's server platform, and DGX is NVIDIA's own line of servers (plain words). The A100 datasheet lists HGX A100 with 4, 8 or 16 GPUs ([NVIDIA A100](https://www.nvidia.com/en-us/data-center/a100/)). NVIDIA's HGX H100 blog describes an HGX H100 with 8 GPUs and 4 third-generation NVSwitch chips, and also an HGX H100 4-GPU variant ([NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing)). This page says "HGX H100 8-GPU" when it means the first.
- **SXM: the form of the GPU on server boards.** NVIDIA's datasheets list a form factor for each GPU. The A100 footnote says SXM4 GPUs come via HGX A100 server boards ([NVIDIA A100](https://www.nvidia.com/en-us/data-center/a100/)).
- **NVL: the PCIe card with an NVLink bridge.** The H100 page lists an H100 SXM and an H100 NVL, a PCIe-based card with an NVLink bridge ([NVIDIA H100](https://www.nvidia.com/en-us/data-center/h100/)). This page uses the SXM figures.
- **A100 (P4d, P4de).** The figures are from the A100 80GB SXM column of NVIDIA's datasheet, from AWS's instance table and from AWS's P4 page.

| Property | Value | Source |
|---|---|---|
| AWS instances | p4d.24xlarge, p4de.24xlarge | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| GPUs per instance | 8 | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| HBM bandwidth per GPU | 2,039 GB/s for the 80 GB part (P4de). NVIDIA's table has no 40 GB part (P4d), so P4d is not read. | [NVIDIA A100](https://www.nvidia.com/en-us/data-center/a100/) |
| NVLink per GPU | 600 GB/s | [NVIDIA A100](https://www.nvidia.com/en-us/data-center/a100/), [AWS P4](https://aws.amazon.com/ec2/instance-types/p4/) |
| NVLink domain | 8 GPUs. Each GPU reaches the others "at the same 600 GB/s" with single-hop latency. | [AWS P4](https://aws.amazon.com/ec2/instance-types/p4/) |
| NVSwitch in the server | AWS says NVSwitch. The count is not read. | [AWS P4](https://aws.amazon.com/ec2/instance-types/p4/) |
| PCIe to the CPU | Gen4, 64 GB/s | [NVIDIA A100](https://www.nvidia.com/en-us/data-center/a100/) |
| AWS network | 400 Gbps (4 x 100) | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |

- **H100 (P5).** The figures are from the H100 SXM column of NVIDIA's datasheet, from the HGX H100 blog and from AWS.

| Property | Value | Source |
|---|---|---|
| AWS instances | p5.48xlarge | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| GPUs per instance | 8 | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| HBM bandwidth per GPU | 3.35 TB/s | [NVIDIA H100](https://www.nvidia.com/en-us/data-center/h100/) |
| NVLink per GPU | 900 GB/s | [NVIDIA H100](https://www.nvidia.com/en-us/data-center/h100/) |
| NVLink domain | 8 GPUs | [NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing), [AWS P5](https://aws.amazon.com/ec2/instance-types/p5/) |
| NVSwitch in the server | 4 third-generation NVSwitch chips in the HGX H100 | [NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing) |
| PCIe to the CPU | Gen5, 128 GB/s | [NVIDIA H100](https://www.nvidia.com/en-us/data-center/h100/) |
| AWS network | 3,200 Gbps | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |

- **H200 (P5e, P5en).** The figures are from NVIDIA's H200 datasheet and from AWS.

| Property | Value | Source |
|---|---|---|
| AWS instances | p5e.48xlarge, p5en.48xlarge | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| GPUs per instance | 8 | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| HBM bandwidth per GPU | 4.8 TB/s | [NVIDIA H200](https://www.nvidia.com/en-us/data-center/h200/) |
| NVLink per GPU | 900 GB/s | [NVIDIA H200](https://www.nvidia.com/en-us/data-center/h200/) |
| NVLink domain | 8 GPUs | [AWS P5](https://aws.amazon.com/ec2/instance-types/p5/) |
| NVSwitch in the server | AWS: up to 900 GB/s of NVSwitch interconnect, with single-hop latency | [AWS P5](https://aws.amazon.com/ec2/instance-types/p5/) |
| PCIe to the CPU | Gen5, 128 GB/s on NVIDIA's datasheet. AWS says the P5en has Gen5 between CPU and GPU, with up to 4 times the CPU-GPU bandwidth of the P5 and P5e. | [NVIDIA H200](https://www.nvidia.com/en-us/data-center/h200/), [AWS P5](https://aws.amazon.com/ec2/instance-types/p5/) |
| AWS network | 3,200 Gbps | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |

- **B200 (P6-B200).** The figures are from NVIDIA's DGX B200 page, NVIDIA's NVLink page and AWS. Blackwell is the name of this GPU generation.

| Property | Value | Source |
|---|---|---|
| AWS instances | p6-b200.48xlarge | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| GPUs per instance | 8 | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| HBM bandwidth per GPU | about 8 TB/s (derived) | [NVIDIA DGX B200](https://www.nvidia.com/en-us/data-center/dgx-b200/) |
| NVLink per GPU | 1,800 GB/s | [NVIDIA NVLink](https://www.nvidia.com/en-us/data-center/nvlink/), [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/) |
| NVLink domain | 8 GPUs | [NVIDIA DGX B200](https://www.nvidia.com/en-us/data-center/dgx-b200/), [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/) |
| NVSwitch in the server | 2 NVSwitch in a DGX B200. NVIDIA lists 14.4 TB/s aggregate NVLink bandwidth for the 8 GPUs. | [NVIDIA DGX B200](https://www.nvidia.com/en-us/data-center/dgx-b200/) |
| PCIe to the CPU | not read | |
| AWS network | 3,200 Gbps | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |

- **B200: how the 8 TB/s was derived.** NVIDIA lists 64 TB/s of HBM bandwidth for the 8 GPUs of a DGX B200. Divided by 8 GPUs, that is about 8 TB/s per GPU (derived) ([NVIDIA DGX B200](https://www.nvidia.com/en-us/data-center/dgx-b200/)).
- **GB200 word: NVL72.** NVL72 is NVIDIA's name for a rack-scale system with 72 GPUs in one NVLink domain ([NVIDIA GB200 NVL72](https://www.nvidia.com/en-us/data-center/gb200-nvl72/)). [Inside the server](#d15-3) defines an NVLink domain and shows what it means on Kubernetes.
- **GB200 word: Grace CPU.** A Grace CPU is a CPU that NVIDIA makes (plain words).
- **GB200 word: superchip.** A superchip is NVIDIA's package of 2 Blackwell GPUs and 1 Grace CPU in one compute module ([AWS P6](https://aws.amazon.com/ec2/instance-types/p6/)). NVIDIA lists 16 TB/s of HBM bandwidth for one GB200 superchip ([NVIDIA GB200 NVL72](https://www.nvidia.com/en-us/data-center/gb200-nvl72/)).
- **GB200 word: NVLink 5 Switch.** NVLink 5 Switch is NVIDIA's name for the switch that goes with fifth-generation NVLink. NVIDIA lists fifth-generation NVLink for its Blackwell GPUs ([NVIDIA NVLink](https://www.nvidia.com/en-us/data-center/nvlink/)).
- **GB200 NVL72 (P6e-GB200 UltraServers).** The figures are from NVIDIA's GB200 NVL72 page, NVIDIA's NVLink page and AWS. [Inside the server](#d15-3) explains UltraServers.

| Property | Value | Source |
|---|---|---|
| AWS instances | u-p6e-gb200x72 (72 GPUs), u-p6e-gb200x36 (36 GPUs, derived: 9 instances x 4) | [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/), [AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html) |
| GPUs per instance | 4 per p6e-gb200.36xlarge | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |
| HBM bandwidth | 16 TB/s per GB200 superchip, which holds 2 GPUs and 1 Grace CPU | [NVIDIA GB200 NVL72](https://www.nvidia.com/en-us/data-center/gb200-nvl72/) |
| NVLink per GPU | 1,800 GB/s | [NVIDIA NVLink](https://www.nvidia.com/en-us/data-center/nvlink/) |
| NVLink domain | 72 GPUs, 130 TB/s across the 72 GPUs, across several instances | [NVIDIA GB200 NVL72](https://www.nvidia.com/en-us/data-center/gb200-nvl72/), [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/) |
| NVSwitch | NVLink 5 Switch, which NVIDIA lists for 8 or 72 GPUs | [NVIDIA NVLink](https://www.nvidia.com/en-us/data-center/nvlink/) |
| PCIe to the CPU | not read | |
| EFA network per GPU | up to 400 Gbps | [AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html) |
| EFA network per u-p6e-gb200x72 | 28.8 Tbps | [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/) |
| Network per p6e-gb200.36xlarge | 3,200 Gbps in AWS's instance table | [AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) |

- **GB200: which name to use.** AWS's own instance table calls these GPUs "B200" ([AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)). NVIDIA and the AWS P6 page call the part GB200 ([NVIDIA GB200 NVL72](https://www.nvidia.com/en-us/data-center/gb200-nvl72/), [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/)). This page says GB200.
- **GB200: how AWS sells UltraServers.** AWS says P6e-GB200 UltraServers are made available through EC2 Capacity Blocks for ML ([AWS EKS UltraServer guide](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-nvidia-ultraserver.html)). Capacity Blocks for ML let you reserve GPU instances on a future date, and instances in a Capacity Block are placed close together in an EC2 UltraCluster ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)). The AWS P6 page invites you to reserve UltraServers for future use ([AWS P6](https://aws.amazon.com/ec2/instance-types/p6/)).
- **GB200: how the 400 Gbps per GPU is read.** The AWS P6 page prints "400 GB ps per GPU" and 28.8 Tbps per u-p6e-gb200x72. This page reads the first as Gbps. The reason is that 72 GPUs x 400 Gbps is 28,800 Gbps, which is 28.8 Tbps (derived). AWS's EFA bandwidth page says 400 Gbps per GPU for the P6e-GB200 ([AWS P6](https://aws.amazon.com/ec2/instance-types/p6/), [AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html)).
- **GB200: the network speed of one p6e-gb200.36xlarge in two AWS pages.** AWS's instance table lists 3,200 Gbps for it ([AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)). The AWS P6 page gives 28.8 Tbps for 18 instances, which is 1,600 Gbps each (derived: 28,800 / 18) ([AWS P6](https://aws.amazon.com/ec2/instance-types/p6/)). AWS does not explain the difference.
- **GB200: a reading of the two network figures.** AWS's EFA bandwidth page says each physical NIC supports up to 400 Gbps. It says each GPU supports up to 400 Gbps of EFA ([AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html)). That EFA bandwidth page lists 8 pairs of network cards, and each pair shares one physical NIC. 8 NICs x 400 Gbps is 3,200 Gbps (derived). 4 GPUs x 400 Gbps is 1,600 Gbps of EFA (derived). **This page's reading, not AWS's:** 3,200 is the NIC capacity. 1,600 is the EFA bandwidth that the 4 GPUs can use.
- **A different server for comparison: NVIDIA DGX B200.** This is not an AWS instance. NVIDIA's own 8-GPU DGX B200 server lists 8 ConnectX-7 NICs (NVIDIA's NIC model). Each runs at up to 400 Gb/s, as InfiniBand or as Ethernet ([NVIDIA DGX B200](https://www.nvidia.com/en-us/data-center/dgx-b200/)).
- **PCIe speed per lane.** PCI-SIG lists 16 GT/s per lane per direction for PCIe 4.0 and 32 GT/s for PCIe 5.0 ([PCI-SIG](https://pcisig.com/what-bit-rates-does-pcie-50-specification-support-and-how-does-it-compare-prior-pcie-generations)). GT/s means giga-transfers per second, the raw rate of one lane. A lane is one data channel of a PCIe link.
- **PCIe speed of a x16 link.** A link of 16 lanes (x16) is the size NVIDIA compares NVLink with. It carries 32 GB/s each way for Gen4 and 64 GB/s each way for Gen5 (both derived: 16 lanes x the rate per lane, divided by 8 bits). [Appendix F](#d-ai-path-appendix-f-words-and-units) says how NVIDIA's 64 and 128 GB/s fit.
- **NVSwitch can help with the sum itself.** NVIDIA says the third-generation NVSwitch has new hardware acceleration for collectives ([NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing)). Collectives are defined in [What a training job asks of the cluster](#d15-2).
- **NVSwitch acceleration: multicast.** Multicast sends one piece of data to many GPUs at once (plain words).
- **NVSwitch acceleration: SHARP.** A reduction is the adding step of a collective. NVIDIA SHARP in-network reductions let the switch do part of the adding. NVIDIA says this significantly reduces the load on the GPU ([NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing)).
- **NVSwitch acceleration: what NVIDIA claims.** NVIDIA says the faster NVLink and these features raise the effective bandwidth of common collectives like all-reduce 3 times. The comparison is with the HGX A100, NVIDIA's previous-generation 8-GPU server platform ([NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing)). This is NVIDIA's claim about its own platform, not a measurement made for this page.
- **Latency: what AWS says about NVSwitch.** AWS says each GPU reaches every other GPU in a P4d, P4de, P5, P5e or P5en server through NVSwitch. AWS calls the latency "single-hop" ([AWS P4](https://aws.amazon.com/ec2/instance-types/p4/), [AWS P5](https://aws.amazon.com/ec2/instance-types/p5/)). AWS gives no number of nanoseconds for it.
- **Latency: what AWS says about the P5en.** AWS says the P5en has lower network latency than the P5. It ties the 35% to the P5en's third-generation EFA and Nitro v5 ([AWS P5](https://aws.amazon.com/ec2/instance-types/p5/)). AWS gives no latency in microseconds. It does not say which latency the 35% measures.
- **Read the instance page, not only the GPU datasheet.** The H100 and H200 datasheets both list PCIe Gen5 at 128 GB/s. AWS reports a different CPU-to-GPU bandwidth for the P5en than for the P5 and P5e. The instance sets what the GPU gets, so check the page for the instance you rent.
- **Which AWS instances support GPUDirect RDMA.** The EFA documentation calls GPUDirect RDMA "Accelerator Data Transfer". It lists it as supported on p5.48xlarge, p5e, p5en, p6-b200, p6-b300 and p6e-gb200, and not on p5.4xlarge ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)). For p4d and p4de, AWS's pages differ. The P4 product page says GPUDirect RDMA works there ([AWS P4](https://aws.amazon.com/ec2/instance-types/p4/)). The EFA table says no. This page makes no claim for P4d.
- **One NIC per GPU on NVIDIA's own server.** A server with several NICs lets each GPU send through a NIC of its own. NVIDIA's DGX B200 has 8 ConnectX-7 NICs, each with up to 400 Gb/s. The page does not say which GPU each NIC serves ([NVIDIA DGX B200](https://www.nvidia.com/en-us/data-center/dgx-b200/)).
- **One NIC per GPU on the P6-B200.** A P6-B200 has 8 GPUs and 8 network cards. Each card carries up to 400 Gbps of EFA ([AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html)).
- **Two network cards per GPU on the P6e-GB200.** A P6e-GB200 gives each GPU a pair of network cards, with up to 400 Gbps of EFA per GPU. Each physical NIC of 400 Gbps is shared by two network cards ([AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html)). A network card is AWS's unit of network capacity, and [Appendix F](#d-ai-path-appendix-f-words-and-units) explains it.
- **The P5 split is not published.** For a P5, AWS gives no per-GPU split of its network. The 50 GB/s per GPU in [The distance ladder](#d15-1) is this page's arithmetic: 3,200 Gbps divided by 8 GPUs and by 8 bits per byte (derived).
- **Network cards and interfaces, by instance.** AWS lists the network of each accelerated instance ([AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)). The table adds the number of network cards and the limit on network interfaces for each instance, which matter when you plan EFA interfaces. The network column is in Gbps (AWS prints "Gigabit"). The p6e-gb200.36xlarge row conflicts with the AWS P6 page, as the point on its two AWS pages shows.

| Instance | Network (Gbps) | Network cards | Max interfaces |
|---|---|---|---|
| p4d.24xlarge, p4de.24xlarge | 4 x 100 | 4 | 60 |
| p5.48xlarge, p5e.48xlarge | 3,200 | 32 | 64 |
| p5en.48xlarge | 3,200 | 16 | 64 |
| p6-b200.48xlarge | 3,200 | 8 | 32 |
| p6e-gb200.36xlarge | 3,200 | 17 | 39 |

- **EFA and IP share one budget on the P5 and P5e.** AWS says the 3,200 Gbps total includes up to 800 Gbps for IP traffic. EFA and IP share the same resources. For example, 400 Gbps of IP traffic leaves 2,800 Gbps for EFA ([AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html)).
- **EFA and ENA share one budget on the P6-B200.** Each card carries up to 400 Gbps of EFA and 200 Gbps of ENA. Up to 1,600 Gbps of the 3,200 Gbps total can be ENA ([AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html)). ENA is the AWS device that gives normal IP networking.
- **EFA generations.** AWS lists EFA v1 on p4d and p4de, v2 on p5 and p5e, v3 on p5en and trn2, and v4 on p6-b200, p6-b300 and p6e-gb200 ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).

### Appendix B. All-reduce, ring and tree, DDP and FSDP in detail

**This appendix holds the step by step of DDP and FSDP and the arithmetic of an all-reduce.** The main body keeps only what each one means for pod placement. [What a training job asks of the cluster](#d15-2) defines DDP, FSDP, all-reduce, all-gather and reduce-scatter.

![One training step in DDP and in FSDP](diagrams/extra/d15-ddp-fsdp-steps.svg)

**What the picture shows.** One step on 4 ranks, drawn for DDP and for FSDP. The bar lengths are schematic, not measured. A rank is one GPU taking part in the job, here GPU 0 to GPU 3. [What a training job asks of the cluster](#d15-2) explains DDP, FSDP, the forward and backward pass, gradients and parameters.

A batch is the group of training examples that one GPU processes in one go. The purple bars are collectives. The five points that start with DDP or FSDP describe the parts of the picture.

- **DDP, forward.** Each rank computes the output for its own batch. There is no gradient traffic. By default DDP also broadcasts the model's buffers at the start of forward, if the model has any ([PyTorch DDP](https://docs.pytorch.org/docs/2.15/generated/torch.nn.parallel.DistributedDataParallel.html)). Buffers are values the model keeps that are not learned, for example BatchNorm statistics.
- **DDP, backward with bucketed all-reduce.** Gradients come out in buckets, which are groups of parameters that sync together. The all-reduce of each finished bucket runs while the backward pass computes the next one. This overlap is the one PyTorch documents for DDP.
- **DDP, optimizer step.** After the last all-reduce, every rank holds the same reduced gradients and updates its own copy of the model. [Appendix F](#d-ai-path-appendix-f-words-and-units) explains the optimizer.
- **FSDP, all-gather before forward and before backward.** Each rank first collects the parameter shards it does not own. The traffic here is the parameters themselves.
- **FSDP, reduce-scatter in backward.** Here each rank keeps one piece of the sum, which is its own shard of the gradients.
- **The purple bars.** Each purple bar is a collective. Inside one server it runs on NVLink or PCIe. Between servers it runs on the NIC ([NCCL overview](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/overview.html)).

**Going deeper, by topic.**

- **DDP hides some of the sync behind the backward pass.** DDP groups parameters into buckets, so that "gradient reduction of each bucket can potentially overlap with backward computation". The bucket size is set with `bucket_cap_mb`, and its default is 25 MiB ([PyTorch DDP](https://docs.pytorch.org/docs/2.15/generated/torch.nn.parallel.DistributedDataParallel.html)). Overlap is only a chance. This page's reading: when a bucket's sync takes longer than the computation it hides behind, the GPUs wait for the rest of the sync.
- **FSDP splits the all-reduce in two.** Each FSDP rank keeps a shard of the parameters, of the gradients and of the optimizer states ([PyTorch FSDP tutorial](https://docs.pytorch.org/tutorials/intermediate/FSDP_tutorial.html)). Before forward and backward, FSDP all-gathers the shards into whole parameters. In the backward pass it reduce-scatters the gradients back into shards. PyTorch calls FSDP "a decomposition of DDP's all-reduce into reduce-scatter and all-gather" ([PyTorch FSDP tutorial](https://docs.pytorch.org/tutorials/intermediate/FSDP_tutorial.html)). This page does not compare the total data volume of DDP and FSDP, because no source read gives that comparison.
- **Ring and tree are two orders for the same sum.** The nccl-tests documentation describes both. A ring adds the values along the ring of ranks, and then passes the results around it. A tree adds the values hierarchically, and then fans the results out. The amount of data moved is the same for both, "as long as they use point-to-point operations" ([nccl-tests](https://raw.githubusercontent.com/NVIDIA/nccl-tests/master/doc/PERFORMANCE.md)). The picture draws a simplified order for 4 ranks, as an example.

![Ring and tree: two orders for the same all-reduce](diagrams/extra/d15-ring-tree.svg)

- **How much data one all-reduce moves: the formula.** The nccl-tests documentation derives the best case. Each element needs 2(n-1) data transfers, where n is the number of ranks. If each rank has bandwidth B to the outside world, and the array has S elements, the best time is t = (S / B) x (2(n-1) / n). The factor 2(n-1) / n starts at 1 for 2 ranks and grows toward 2 as ranks are added ([nccl-tests](https://raw.githubusercontent.com/NVIDIA/nccl-tests/master/doc/PERFORMANCE.md)). For a reduce-scatter the factor is (n-1) / n.
- **How much data one all-reduce moves: an example.** The table works one example. **Every value in it is illustrative:** S is 2 GB of gradients, and B is 50 GB/s per rank. The times are the formula's best case, so a real run takes longer.

| Ranks (n) | Factor 2(n-1) / n | Best time t = (S / B) x factor (illustrative) |
|---|---|---|
| 2 | 1.00 | 0.040 s |
| 8 | 1.75 | 0.070 s |
| 64 | 1.97 | 0.079 s |
| very many | approaches 2 | approaches 0.080 s |

- **What the example teaches.** Going from 8 ranks to 64 adds 12.5% to the best time in this example (derived: 0.07875 divided by 0.07). The formula has one B per rank. This page reads that as: the slowest rung that the rank's data must cross sets B. If B falls 10 times, t grows 10 times. In this formula, the bandwidth of the rung matters more than the number of ranks.
- **Small messages and large messages behave differently.** The nccl-tests documentation says time on small sizes measures the constant overhead, which is the latency. On large sizes time is roughly the overhead plus size divided by bandwidth ([nccl-tests](https://raw.githubusercontent.com/NVIDIA/nccl-tests/master/doc/PERFORMANCE.md)). This page's reading follows from that. Many small collectives are limited by latency, and big gradient buckets are limited by bandwidth. The page has no measured crossover size, because none was found.
- **Bus bandwidth: the number to compare with hardware.** nccl-tests measures a speed called algbw. It multiplies algbw by the factor 2(n-1) / n to get the "bus bandwidth". Bus bandwidth can be compared with the hardware's peak "independently of the number of ranks used" ([nccl-tests](https://raw.githubusercontent.com/NVIDIA/nccl-tests/master/doc/PERFORMANCE.md)). Design 14 runs a multi-node collective test as part of its burn-in check ([Node health: burn-in](#ds14-3)). No measured result is shown here.
- **NCCL can use the NVSwitch help.** The variable `NCCL_ALGO` is in [Appendix D](#d-ai-path-appendix-d-nccl-variables-in-full). Two of its values, NVLS and NVLSTree, enable NVLink SHARP offload ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).

### Appendix C. Reading the topology matrix

**This appendix explains the table that `nvidia-smi topo -m` prints.** The main body only says to run the command and to prefer the codes `PIX` and `PXB` for a GPU and its NIC. This appendix has all seven codes and the words they use.

**Going deeper, by topic.**

- **The command `nvidia-smi topo -m`.** The command prints a table with one row and one column for each GPU and NIC, plus columns for CPU affinity and NUMA affinity. Each cell is one of NVIDIA's seven path codes ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **The seven path codes: what they are.** NVIDIA's legend gives seven codes for the connection between two devices. Each code has its own point, with NVIDIA's advice for a GPU and its NIC ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Words: PCIe switch.** A PCIe switch is a chip that lets several devices share one upstream PCIe link (plain words).
- **Words: PCIe bridge.** A PCIe bridge is one hop on the PCIe path. NVIDIA's legend counts bridges between two devices (plain words, [NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Words: host bridge.** A host bridge is the part that connects a PCIe tree to the CPU. NVIDIA's legend calls it "typically the CPU" ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Words: SMP interconnect (QPI/UPI).** It is the link that joins the CPU sockets of one server. NVIDIA's legend calls it "the SMP interconnect between NUMA nodes (e.g., QPI/UPI)" ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Words: PCIe root.** A PCIe root (formally the PCIe Root Complex) is defined in [The distance ladder](#d15-1), and [Dynamic Resource Allocation](#d06-4) explains it with pictures. NVIDIA says GPUDirect RDMA needs the GPU and the NIC to "share the same upstream PCI Express root complex" ([NVIDIA GPUDirect RDMA](https://docs.nvidia.com/cuda/gpudirect-rdma/index.html)).
- **Path code `X`.** The device itself. NVIDIA gives no advice ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Path code `NV#`.** A bonded set of # NVLinks, where # is a number. NVIDIA gives no advice for GPU to GPU ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Path code `PIX`.** At most one PCIe bridge. NVIDIA says to aim for `PIX` or `PXB` with a dedicated PCIe connection ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Path code `PXB`.** Several PCIe bridges, without the host bridge. NVIDIA says to aim for `PIX` or `PXB` with a dedicated PCIe connection ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Path code `PHB`.** PCIe plus a host bridge, typically the CPU. NVIDIA calls it acceptable if both devices share a host bridge and a NUMA node ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Path code `NODE`.** PCIe plus the link between host bridges inside one NUMA node. NVIDIA says to avoid it ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Path code `SYS`.** PCIe plus the link between NUMA nodes. NVIDIA says to avoid it ([NVIDIA DOCA](https://networking-docs.nvidia.com/doca/archive/3-4-0/gpunetio-installation-and-setup)).
- **Reading the matrix.** The picture is a matrix like the one the command prints. **The matrix is illustrative:** it was worked out by hand from a layout with 8 GPUs, 8 NICs, 4 PCIe switches and 2 CPUs. It is not output from a real server. For example, GPU 0 and NIC 4 show `SYS` because NIC 4 sits under the other CPU.

![How to read nvidia-smi topo -m](diagrams/extra/d15-topo-matrix.svg)

- **PXB and NODE are not in the server picture.** The picture in section 3, [Inside the server](#d15-3), draws only `PIX`, `PHB`, `SYS` and `NV#`. The codes `PXB` and `NODE` need a different layout from the one drawn there.

### Appendix D. NCCL variables in full

**This appendix lists the NCCL variables that the main body names, with their values.** NCCL's documentation splits its variables into two groups. "System configuration" variables can stay in scripts. "Debugging" variables should not ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)). Each point says which group the variable is in. Set the debugging ones to test a theory, then remove them.

**Going deeper, by topic.**

- **`NCCL_SOCKET_IFNAME` (System configuration).** It chooses the IP interfaces NCCL uses, by prefix. `^` excludes. `=` matches an exact name. Examples in the docs: `eth`, `=eth0`, `^docker` ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **`NCCL_IB_HCA` (System configuration).** It chooses the RDMA adapters (HCAs) for the InfiniBand and RoCE transport. The form is `<hca>[:<port>[:<rail>[:<plane>]]]`. Without `=`, `mlx5_1` also matches `mlx5_10` to `mlx5_19`, so write `=mlx5_0:1,mlx5_1:1` ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **Words: HCA.** HCA is Host Channel Adapter, the name NCCL uses for an RDMA network interface ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **Words: rail.** A rail is NCCL's word for the network behind one NIC ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **Words: rail-optimized.** A fabric built so that each NIC has its own switch is called "rail-optimized" ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **`NCCL_CROSS_NIC` (System configuration).** With 0, NCCL keeps each ring or tree on one NIC. With 1, it allows different NICs. With 2, the default, it prefers the same NIC. 0 suits rail-optimized fabrics with a slow connection between rails. 1 suits all NICs on one switch. It has no effect with one NIC ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **`NCCL_NET_PLUGIN` (System configuration).** It chooses a network plug-in. If the variable is set, NCCL tries the library with that name, then `libnccl-net-<name>.so`. If it is not set, NCCL tries `libnccl-net.so`. If none is found, it uses its internal plug-in. `aws-ofi-nccl` is a plug-in that lets NCCL use Libfabric, which is how NCCL reaches EFA ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **`NCCL_DEBUG` (System configuration).** It sets the logging level: `VERSION`, `WARN`, `INFO` or `TRACE`. Use `INFO` to see which network NCCL picked, as AWS's guide does ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html), [AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **`NCCL_TOPO_FILE` and `NCCL_TOPO_DUMP_FILE` (System configuration).** `NCCL_TOPO_FILE` points NCCL at an XML file that describes the topology of one host, for the case when NCCL cannot see the real topology. `NCCL_TOPO_DUMP_FILE` writes out what NCCL detected. The file must describe a single host ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **`NCCL_P2P_LEVEL` (Debugging): how far apart two GPUs may talk directly.** P2P means peer to peer, GPU to GPU. The variable sets the farthest distance at which NCCL uses a direct path. It takes these values ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)):
  - `LOC`: never.
  - `NVL`: when the GPUs are connected through NVLink.
  - `PIX`: under one PCIe switch.
  - `PXB`: through several PCIe switches.
  - `PHB`: on the same NUMA node, through the CPU.
  - `SYS`: across NUMA nodes.
- **`NCCL_NET_GDR_LEVEL` (Debugging): how far apart a GPU and its NIC may be.** The variable sets the farthest GPU-to-NIC distance at which NCCL uses GPUDirect RDMA. It takes the same words as `NCCL_P2P_LEVEL`, except `NVL`: `LOC`, `PIX`, `PXB`, `PHB` and `SYS`. If it is not set, NCCL picks a value itself ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **`NCCL_IB_DISABLE` (Debugging).** Set to 1, it stops NCCL from using the InfiniBand and RoCE transport, and NCCL falls back to another transport such as IP sockets. NCCL's own description calls that transport the "IB/RoCE transport", so one transport serves both ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **`NCCL_ALGO` (Debugging).** It defines which algorithms NCCL may use, among Ring, Tree and others. NVLS and NVLSTree are two of them, and they enable NVLink SHARP offload, which is NVIDIA SHARP running over NVLink ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).
- **Why the debugging variables are not for keeping.** NCCL's documentation says such variables "should not be used in production nor retained in scripts, or only as workaround" ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)). Remove them once the issue is resolved.
- **PXN: using another GPU's NIC.** With PXN, NCCL can send inter-node data through a NIC that is not local to the GPU. It first moves the data over NVLink to a GPU that is close to that NIC. PXN exists since NCCL 2.12. `NCCL_PXN_DISABLE` and `NCCL_P2P_PXN_LEVEL` control it, and both are in the Debugging group ([NCCL env](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html)).

### Appendix E. Side notes in full: AMD Instinct, Google TPU and AWS Trainium

**This appendix holds the numbers and the conflicts for the three other chip families.** The main body keeps only what each one means on Kubernetes. Each conflict between two pages is shown once, here.

**Going deeper, by topic.**

- **AMD Instinct MI300X: memory.** MI300X is AMD's data-center GPU. AMD lists 5.3 TB/s of memory bandwidth, in a column labelled "Per OAM" (one MI300X accelerator module, plain words) ([AMD](https://www.amd.com/en/products/accelerators/instinct/mi300/platform.html)).
- **AMD Instinct MI300X: GPU-to-GPU speed.** xGMI is AMD's GPU-to-GPU link. Each of the 8 GPUs is connected directly to each of the 7 others. AMD's table lists 896 GB/s under "Total Aggregate Bi-directional I/O Bandwidth (Peer-to-Peer)" and does not say what that figure covers ([AMD](https://www.amd.com/en/products/accelerators/instinct/mi300/platform.html)). One reading is 7 links x 64 GB/s x 2 directions = 896 GB/s for one GPU (derived). That reading is this page's, not AMD's.
- **AMD Instinct MI300X: xGMI measured.** Each link carries 64 GB/s one way as raw rate, and about 48 GB/s usable. AMD lists 315 to 336 GB/s realized per GPU (7 links x 45 to 48 GB/s), against 448 GB/s in theory (7 x 64). RCCL is the counterpart of NCCL on AMD GPUs, and AMD's `rccl-tests` run shows about 310 to 330 GB/s ([AMD ROCm blog](https://rocm.blogs.amd.com/software-tools-optimization/mi300x-rccl-xgmi/README.html)).
- **AMD Instinct MI300X: Kubernetes in full.** AMD's GPU DRA driver (beta) publishes the PCIe root and supports claims that match a GPU and a NIC. AMD's own table calls its device plugin count-based and opaque to the scheduler, and shows the count as `amd.com/gpu: 2` ([AMD ROCm blog](https://rocm.blogs.amd.com/software-tools-optimization/dra-gpu/README.html)). The device plugin exposes AMD GPUs as `amd.com/gpu` resources ([AMD device plugin](https://instinct.docs.amd.com/projects/k8s-device-plugin/en/latest/)).
- **AMD Instinct MI300X: the network between servers.** Oracle's quickstart for its MI300X shape lists "RoCE-capable networking" ([Oracle OCI quickstart](https://github.com/oracle-quickstart/oci-gpu-quickstarts/blob/main/amd/MI300X/README-MI300X.md)). That is a cloud vendor's page, not AMD's. AMD's own pages read for this page do not name the network between servers.
- **Google TPU: slices, ICI and DCN.** A slice is a group of TPU chips joined by the inter-chip interconnect (ICI). Multislice runs one job on several slices. Chips in different slices pass data to their host CPUs, which send it over the data-center network (DCN) ([Google Cloud](https://docs.cloud.google.com/tpu/docs/multislice-introduction)).
- **Google TPU: what the slices must share.** All slices of a Multislice job must have the same TPU type, size and topology. Multislice supports only multi-host TPU slice node pools ([GKE Multislice](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/tpu-multislice)).
- **Google TPU: GKE jobs.** The GKE page's example runs Multislice as a JobSet, and its container asks for TPU chips with the resource `google.com/tpu` ([GKE Multislice](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/tpu-multislice)).
- **Google TPU: the two Google pages differ on DCN latency.** The Multislice introduction calls DCN a higher-latency, lower-throughput network than ICI ([Google Cloud](https://docs.cloud.google.com/tpu/docs/multislice-introduction)). The GKE page defines DCN as a "higher capacity, low latency, and lower-throughput network" ([GKE Multislice](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/tpu-multislice)). This page gives no bandwidth or latency figure for TPUs, because none was found.
- **AWS Trainium2: memory.** Neuron is AWS's software kit for Trainium. The Neuron docs list 96 GiB of memory at 2.9 TB/s for each Trainium2 chip ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trainium2.html)). A GiB is a gibibyte, and [Appendix F](#d-ai-path-appendix-f-words-and-units) explains it.
- **AWS Trainium2: the layout.** The 16 chips in a trn2.48xlarge sit in a 4x4 2D torus over NeuronLink-v3. A torus is a grid in which the chips on each edge also link to the chips on the opposite edge. A Trn2 UltraServer joins 4 instances, which makes 64 chips. Chips with the same coordinates in each instance form a ring ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trn2-arch.html)).
- **AWS Trainium2: chip-to-chip speed.** NeuronLink-v3 is AWS's chip-to-chip link. The Trn2 instance page lists 1,024 GiB/s per chip between chips in one instance, and 256 GiB/s per chip between the instances of a Trn2 UltraServer ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trn2-arch.html)). The Trainium2 chip page says 1.28 TB/s per chip for NeuronLink-v3 ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trainium2.html)). This page does not resolve the difference.
- **AWS Trainium2: collectives.** The Neuron docs say NeuronLink-v3 "enables collective communication between Trainium2 chips during distributed training and inference" ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trn2-arch.html)).
- **AWS Trainium2: the network.** AWS's instance table lists 16 x 200 Gigabit for a trn2.48xlarge, which is 3.2 Tbps (derived: 16 x 200) ([AWS table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)). The Neuron page prints "EFAv3 bandwidth (GB/s) 3,200", a unit label that does not match, so this page uses AWS's table ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trn2-arch.html)).
- **AWS Trainium: Neuron Direct RDMA.** AWS's EFA table lists Neuron Direct RDMA for Trn1 and Trn2 ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **AWS Trainium: UltraCluster.** Trn1 instances are offered in an EC2 UltraCluster, which is AWS's name for a large group of instances on one fast network. The Neuron docs give its size as "over 100,000 Trainium chips" with "non-blocking petabit-scale EFA networking" ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trn1-arch.html)).
- **AWS Trainium: the trn2.3xlarge.** The Neuron docs say the trn2.3xlarge has one chip and uses no NeuronLink or EFA. AWS's EFA table lists the trn2.3xlarge as EFA-capable. The two AWS pages differ ([Neuron docs](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/about-neuron/arch/neuron-hardware/trn2-arch.html)).
- **AWS Trainium on EKS: the two ways to ask for devices.** The Neuron device plugin advertises `aws.amazon.com/neuron` and `aws.amazon.com/neuroncore` as integer counts. The Neuron DRA driver publishes rich attributes, including the topology and the EFA locality ([AWS EKS Neuron](https://docs.aws.amazon.com/eks/latest/userguide/device-management-neuron.html)).
- **AWS Trainium on EKS: connected devices.** With the DRA driver, a claim can ask for 1, 4, 8 or 16 connected Neuron devices, using `matchAttribute` on an attribute such as `resource.aws.com/devicegroup4_id`. With the device plugin, contiguous devices on instances such as trn2.48xlarge need the Neuron scheduler extension ([AWS EKS Neuron](https://docs.aws.amazon.com/eks/latest/userguide/device-management-neuron.html)).
- **AWS Trainium on EKS: what does not mix.** The two mechanisms cannot run on the same node. The Neuron DRA driver is not supported with EKS Auto Mode, and AWS says to use the device plugin there ([AWS EKS Neuron](https://docs.aws.amazon.com/eks/latest/userguide/device-management-neuron.html)).

### Appendix F. Words and units

**This appendix holds the words and units that the main body uses.** Each point explains one word or one unit.

**Going deeper, by topic.**

- **Bytes and bits.** Memory and NVLink figures use bytes per second: GB/s and TB/s. Network figures use bits per second: Gbps. 8 bits make 1 byte, so 80 Gbps is 10 GB/s (derived: 80 / 8). AWS's instance table prints "Gigabit" for the same unit, and this page writes Gbps.
- **Latency.** Latency is the time one small message takes to arrive (plain words).
- **Throughput.** Throughput is how much data moves per second (plain words).
- **Forward pass and backward pass.** In the forward pass, the model computes its output for a batch of training examples. In the backward pass, it computes the gradients (plain words).
- **Gradient and parameter.** The parameters are the numbers a model learns. A gradient is a number that says how to change one parameter (plain words).
- **Optimizer and optimizer states.** The optimizer is the code that changes the parameters after the backward pass. The optimizer states are the extra numbers that it keeps for each parameter (plain words).
- **One direction or both directions: why it matters.** A link carries data both ways. A speed figure can count one way, or it can add both ways. Check which one a source means before you compare two numbers.
- **One direction or both directions: NVLink.** NVIDIA labels its NVLink figures "bidirectional", which means both directions added together ([NVIDIA HGX H100 blog](https://developer.nvidia.com/blog/introducing-nvidia-hgx-h100-an-accelerated-server-platform-for-ai-and-high-performance-computing)).
- **One direction or both directions: PCIe.** The H100, H200 and A100 datasheets print "PCIe Gen5: 128GB/s" and "PCIe Gen4: 64 GB/s" with no direction ([NVIDIA H100](https://www.nvidia.com/en-us/data-center/h100/), [A100](https://www.nvidia.com/en-us/data-center/a100/)). PCI-SIG, the body that defines PCIe, prints its speed per lane per direction ([PCI-SIG](https://pcisig.com/what-bit-rates-does-pcie-50-specification-support-and-how-does-it-compare-prior-pcie-generations)). So NVIDIA's 128 GB/s fits both directions added (derived reading, not stated by NVIDIA). [Appendix A](#d-ai-path-appendix-a-hardware-numbers-by-gpu-generation-and-aws-instance) shows the arithmetic.
- **One direction or both directions: AWS networking.** AWS does not say whether its 400 Gbps per GPU counts one direction or both. AWS gives the same silence for the 3,200 Gbps of a P5. So this page does not compare either figure with the NVLink and PCIe figures on equal terms.
- **NVLink against PCIe.** NVLink at 900 GB/s is about 7 times faster than PCIe Gen5 at 128 GB/s (derived: 900 divided by 128). The 7 assumes that both figures count both directions added.
- **GiB and MiB.** A GiB is a gibibyte: 1,024 x 1,024 x 1,024 bytes. That is a little more than a GB. A MiB is 1,024 x 1,024 bytes. AWS and Neuron use these units in some places.
- **CPU.** A CPU (central processing unit) is the general-purpose chip of a server. It runs the operating system and hands work to the GPUs.
- **WAN, region and zone.** A WAN is the wide-area network that links regions. A region is a geographic area, and a zone is a separate location inside a region.
- **Training, fine-tuning and serving.** Training is teaching a model. Fine-tuning is adjusting a model that already exists. Serving is answering requests with a finished model. [Fine-tuning on Kubernetes](#d17-1) in Deep dive 17 covers fine-tuning. [Serving and loading heavy models](#d16-2) in Deep dive 16 covers serving.
- **HPC.** HPC is high-performance computing. EFA is for AI and HPC work ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **Process group and world size.** A process group is the set of ranks that take part in the same collectives. In PyTorch, the world size is the number of processes that take part in the job. PyTorch says the rank of a process "should be a number between 0 and world_size-1" ([PyTorch](https://docs.pytorch.org/docs/2.15/distributed.html)).
- **Shard.** A shard is one piece of a set that is split across ranks (plain words).
- **CUDA.** CUDA is NVIDIA's software for programming its GPUs. PyTorch advises NCCL for GPUs that use CUDA ([PyTorch](https://docs.pytorch.org/docs/2.15/distributed.html)).
- **InfiniBand Verbs and IP sockets.** InfiniBand Verbs is the programming interface for InfiniBand and RoCE adapters. IP sockets are ordinary network connections through the operating system ([NCCL overview](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/overview.html)).
- **TCP.** TCP is the standard protocol for network connections (plain words).
- **Availability Zone and VPC (AWS).** An Availability Zone is a separate location inside an AWS Region. A VPC (Virtual Private Cloud) is your own private network in AWS. EFA traffic stays inside one of each ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **ENA.** ENA is the Elastic Network Adapter, the AWS device that gives normal IP networking ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html), [AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **EFA and the kernel.** EFA is the Elastic Fabric Adapter, a NIC attached to an AWS instance to accelerate AI, machine learning and HPC. It bypasses the operating system kernel, which is the core of the operating system. NCCL talks to the EFA device directly through Libfabric. Without EFA, traffic goes through the operating system's TCP/IP stack, its normal network software, and the ENA driver ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **SRD.** SRD is Scalable Reliable Datagram, the AWS network protocol that EFA uses ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)). On the P6, EFAv4 uses it to route data over several network paths ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html), [AWS P6](https://aws.amazon.com/ec2/instance-types/p6/)).
- **ENI.** ENI is the Elastic Network Interface. AWS documentation calls it a "network interface": a logical networking component in a VPC that represents a virtual network card ([AWS ENI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html)).
- **What an ENI carries.** An ENI carries a private IP address, security groups (AWS firewall rules) and a MAC address (the hardware address of a network card). It attaches to instances in the same Availability Zone ([AWS ENI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html)).
- **Network card (AWS).** A network card is AWS's unit of network capacity on an instance. A network interface (ENI) attaches to one network card. An instance can have only one EFA or EFA-only interface per network card. Instances with several network cards get more bandwidth, above 100 Gbps ([AWS ENI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-eni.html)).
- **Network card and NIC.** A network card is not always one physical NIC. On the P6e-GB200, two network cards share one physical NIC. This page writes NIC for the physical device and network card for AWS's unit ([AWS EFA bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-acc-inst-types.html)).
- **Interface kind `interface`.** `interface` is one of the three kinds of network interface on an instance. It is ENA only ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **Interface kind `efa`.** `efa` is ENA and EFA together on one network interface ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **Interface kind `efa-only`.** `efa-only` is EFA without an IP address ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).
- **The interface limit.** All three kinds count toward the instance's limit of network interfaces ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)).

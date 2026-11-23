---
id: d-ai-recipes
title: "Fine-tuning on Kubernetes, worked examples and the symptom-to-rung cheat sheet"
short: "Fine-tuning and worked examples"
---
**This deep dive does three things.** It shows what changes on Kubernetes when a team fine-tunes a model. It puts the fields of Deep dives 10, 15 and 16 together in four worked manifests. It ends with a table that points from a symptom to the rung of the distance ladder to check first. Fine-tuning means training an existing model a little more, on your own data. The distance ladder is the seven steps from inside a GPU out to another Region, and [Deep dive 15](#d15-1) explains it.

**This deep dive is not specific to AWS.** AWS is the worked example, because AWS publishes numbers and Kubernetes drivers for its network and storage. The manifests use three AWS names. EFA is a network device that carries fast traffic between servers in AI work ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). FSx for Lustre is a managed file system for workloads where storage speed matters ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)). S3 is AWS's object storage service ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html)). The Kubernetes fields in the manifests are the same on any cloud. No manifest here was applied to a cluster. Each was parsed as YAML, and each field name was checked against the Kubernetes or operator page listed beside it.

The section "Fine-tuning on Kubernetes" covers fine-tuning. The section "Worked examples" covers four jobs as manifests. The section "Symptom to rung" is the cheat sheet. Each of the three sections opens with one Kubernetes question and keeps only what changes something you do on Kubernetes. Appendices A to C hold the details that do not change what you do.

Every number has a source linked next to it. *Derived* means arithmetic on sourced numbers, shown in the text. *Illustrative* means picked to teach, not measured. *This page's reading* means the source does not say it in so many words. Where no source was found, the page says "not known from sources". A line that ends with `#!` in a manifest is a line that matters for that point.

**Going deeper, by topic.**

- **Words: Job, JobSet and Kueue.** A Job runs pods until a set number of them finish ([Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)). A JobSet runs a group of Jobs as one unit ([JobSet](https://jobset.sigs.k8s.io/docs/concepts/)). Kueue manages quotas and how jobs use them ([Kueue overview](https://kueue.sigs.k8s.io/docs/overview/)). [Racks and network blocks](#d10-6) shows all three in use, in the points "Let a queue do it: Kueue setup" and "Let JobSet do it".
- **Words: Kubeflow Trainer.** Kubeflow Trainer is a Kubernetes system for fine-tuning large language models and for training other models ([Kubeflow Trainer](https://www.kubeflow.org/docs/components/trainer/overview/)). A TrainJob is one run of it. The point "Run it as a TrainJob: what it is" explains the runtime that a TrainJob uses.
- **Words: LoRA, adapter, QLoRA.** LoRA freezes the weights of the trained model and adds small trainable matrices to each layer ([LoRA paper](https://arxiv.org/abs/2106.09685)). The saved result of such a run is an adapter, which holds only the added matrices and not the base model ([PEFT](https://huggingface.co/docs/peft/en/developer_guides/checkpoint)). LoRA has its own setting called rank, written r ([LoRA paper](https://ar5iv.labs.arxiv.org/html/2106.09685)). This page's reading of the paper's two matrices, B with r columns and A with r rows: the rank is the inner size of the two small matrices. A higher rank means larger added matrices. It is not the rank of a GPU, which the point "Words: NCCL and rank" explains. QLoRA keeps the frozen base model in 4-bit numbers while it trains the adapters ([QLoRA paper](https://arxiv.org/abs/2305.14314)). PEFT is a Hugging Face library of such methods, which train a small number of extra parameters ([PEFT](https://huggingface.co/docs/peft/index)).
- **Words: sidecar and image volume.** A sidecar is a second container that runs beside the main container in the same pod ([Kubernetes sidecars](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)). An image volume is a read-only volume whose files come from a container image or an artifact in an image registry ([Kubernetes volumes](https://kubernetes.io/docs/concepts/storage/volumes/)). [Serving and loading heavy models](#d16-2) explains it in the point "Weights: an image volume".
- **Words: NCCL and rank.** NCCL is NVIDIA's library for communication between GPUs ([NCCL overview](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/overview.html)). A rank is one GPU taking part in a job ([NCCL communicators](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/communicators.html)). [What a training job asks of the cluster](#d15-2) explains both in the point "Rank and NCCL". The LoRA rank in the point "Words: LoRA, adapter, QLoRA" is a different setting with the same name.
- **Words: tensor parallelism.** Tensor parallelism is one way to split a model across GPUs, in plain words. [What a training job asks of the cluster](#d15-2) explains where its pods sit.
- **Words: DRA.** DRA stands for Dynamic Resource Allocation ([Kubernetes DRA attributes](https://kubernetes.io/docs/reference/node/dra-standard-device-attributes/)). In plain words, a pod asks for a device by its properties. [Inside the server](#d15-3) explains the claim in the point "DRA and the pcieRoot attribute".
- **Words: NIC.** A NIC (network interface card) connects a server to the network. This is plain words, as in [The distance ladder](#d15-1) at rung 3.
- **Words: checkpoint.** A checkpoint is a saved copy of the training state. This is plain words, as in [Regions, the WAN and failover](#d10-8). [Training data and checkpoints](#d16-1) explains where to keep it.
- **Words: readiness and startup probes.** A readiness probe tells Kubernetes whether a pod can take traffic, and a pod that is not ready gets none through a Service. A startup probe protects a slow-starting container ([Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)). [Serving and loading heavy models](#d16-2) explains both in the point "Wait for the load: the probes".
- **Words: terms that Deep dives 10, 15 and 16 define.** This page does not define them again. CSI driver, PersistentVolume, claim, StorageClass, ReadOnlyMany, ReadWriteMany, the kubelet, EKS and ENA are in the Words points of [Deep dive 16](#d16-1), before its section 1. Hugepages are in [Leaving the server](#d15-4), point "Huge pages". Region, zone and WAN are in [The distance ladder](#d15-1), rungs 5 to 7. PCIe, PCIe root, NUMA node and the Topology Manager are in [The distance ladder](#d15-1), at rung 3. GB, GiB and Gbps are in [Appendix F of Deep dive 15](#d-ai-path-appendix-f-words-and-units).

### Fine-tuning on Kubernetes: how big is the job, and how do you run it?

**The Kubernetes question.** A team wants to adapt a model instead of training one from scratch. Does that change what the pods ask for, where they land, and what moves between storage and the GPUs?

**Scenario.** This scenario is illustrative. A team has a base model and a few hundred labelled examples. It does not know whether the job needs one GPU, one whole server or several servers. The answer decides how much of Deep dives 10, 15 and 16 the job needs. A job on one GPU needs none of the network rules. A job on several servers needs all of them.

![Fine-tuning on Kubernetes: three sizes of job](diagrams/sections/d17-1.svg)

**What the picture shows.** Three sizes of fine-tuning job, one row each, from the smallest to the largest. Each row lists what the pod asks for, which sections of Deep dives 10, 15 and 16 apply, and what moves. The two memory figures come from the LoRA and QLoRA papers and are for the named models only. The three sizes are illustrative.

- **Row 1, one GPU.** The pod asks for one GPU. The QLoRA paper says its method fine-tunes a 65-billion-parameter model on a single 48 GB GPU ([QLoRA paper](https://arxiv.org/abs/2305.14314)). With one pod on one server, no rule about the network between servers applies.
- **Row 2, one server with several GPUs.** The pod asks for several GPUs on one node. Which GPUs it gets and how they are linked still matter, and [Inside the server](#d15-3) covers that. The LoRA paper gives 350 GB of training memory for GPT-3 175B ([LoRA paper](https://ar5iv.labs.arxiv.org/html/2106.09685)). The papers do not say how many GPUs they used. [Appendix A](#d-ai-recipes-appendix-a-fine-tuning-figures-from-the-papers-and-the-guides) compares that figure with the memory of one server.
- **Row 3, several servers.** The job runs on more than one node. A sharded job such as FSDP, which splits the model over its ranks, moves data between servers on every step ([What a training job asks of the cluster](#d15-2)). This page's reading: it needs what a pre-training job needs. The pods use a fast network device ([Leaving the server](#d15-4)). They sit in one network block ([Racks and network blocks](#d10-6)) and in one zone ([Zones](#d10-7)). They start all together ([Gang scheduling](#d02-2)).
- **What moves, in every row.** The base weights go in, the dataset goes in, and a small adapter comes out. [Serving and loading heavy models](#d16-2) and [Training data and checkpoints](#d16-1) cover the two storage paths.
- **No rule for the size.** No source read gives a general rule for when a fine-tune fits one GPU, one server or needs several. The two papers give memory for named models. Check the memory your own model needs.

**Going deeper, by topic.**

- **LoRA: the paper's numbers.** The paper compares LoRA with fine-tuning GPT-3 175B with Adam. Adam is a training method that stores extra values, called optimizer states, for each weight it trains. LoRA can cut the GPU memory by 3 times ([LoRA paper](https://arxiv.org/abs/2106.09685)). Training memory on that model falls from 1.2 TB to 350 GB, and the checkpoint falls from 350 GB to 35 MB. The 35 MB is for LoRA rank 4, the setting that the point "Words: LoRA, adapter, QLoRA" explains. Only the query and value matrices are adapted, which are two of the four attention matrices in each layer ([LoRA paper](https://ar5iv.labs.arxiv.org/html/2106.09685)). All figures are for GPT-3 175B. No size is given for other models or ranks.
- **LoRA: the base model stays.** The paper says the 350 GB model is still needed at deployment. It also says that storing 100 adapted models takes 350 GB plus 35 MB for each ([LoRA paper](https://ar5iv.labs.arxiv.org/html/2106.09685)). PEFT says the same from the other side: to load a PEFT model, you need the original model as well ([PEFT](https://huggingface.co/docs/peft/en/developer_guides/checkpoint)).
- **QLoRA: the memory figure.** The paper says one 48 GB GPU can fine-tune a 65-billion-parameter model. A regular 16-bit fine-tune of such a model needs more than 780 GB of GPU memory ([QLoRA paper](https://arxiv.org/abs/2305.14314), [QLoRA paper, full text](https://ar5iv.labs.arxiv.org/html/2305.14314)). [Appendix A](#d-ai-recipes-appendix-a-fine-tuning-figures-from-the-papers-and-the-guides) explains how QLoRA gets there.
- **What to request: the GPU count.** A pod asks for GPUs in the `limits` section. A limit alone is enough, because Kubernetes uses the limit as the request ([Kubernetes GPUs](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)). [How GPUs are advertised](#d06-2) in Deep dive 2 explains where the count comes from.
- **Run it as a TrainJob: what it is.** A TrainJob names a runtime with `runtimeRef`. The runtime is a template that platform administrators manage. A cluster-wide runtime is a ClusterTrainingRuntime, and a runtime for one namespace is a TrainingRuntime ([Kubeflow runtimes](https://www.kubeflow.org/docs/components/trainer/operator-guides/runtime/)). A runtime called `torch-distributed` is one of the default runtimes that the Helm installation can enable ([Kubeflow install](https://www.kubeflow.org/docs/components/trainer/operator-guides/installation/)).
- **Run it as a TrainJob: the manifest.** `numNodes` is the number of training nodes, and `resourcesPerNode` holds the compute resources of each node ([Kubeflow types](https://raw.githubusercontent.com/kubeflow/trainer/v2.4.0/pkg/apis/trainer/v1alpha1/trainjob_types.go)). Kueue's page says its queue label is the only Kueue-specific addition a TrainJob needs ([Kueue TrainJobs](https://kueue.sigs.k8s.io/docs/tasks/run/trainjobs/)). All names, the image and the sizes are illustrative. Every example on the pages read uses the API version `v1alpha1`.

```yaml
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: finetune-support-tone            # illustrative
  namespace: team-support                # illustrative
  labels:
    kueue.x-k8s.io/queue-name: user-queue   # the only Kueue addition #!
spec:
  runtimeRef:
    apiGroup: trainer.kubeflow.org
    kind: ClusterTrainingRuntime
    name: torch-distributed
  trainer:
    image: example.com/my-finetune-image:latest   # illustrative
    numNodes: 1                          # 1 means one server #!
    resourcesPerNode:
      limits:
        nvidia.com/gpu: 4                # illustrative
      requests:
        cpu: "8"                         # illustrative
        memory: 64Gi                     # illustrative
```

- **Run it as a TrainJob: how Kueue holds it.** By default, Kueue sets `suspend` to true through a webhook and lifts it when the TrainJob is admitted ([Kueue TrainJobs](https://kueue.sigs.k8s.io/docs/tasks/run/trainjobs/)).
- **Run it as a TrainJob: what Kueue needs.** Kueue needs Kubeflow Trainer v2.0.0 or later, and the TrainJob integration must be switched on in Kueue ([Kueue TrainJobs](https://kueue.sigs.k8s.io/docs/tasks/run/trainjobs/)).
- **Run it as a TrainJob: the ML policy.** A runtime holds an ML policy. The ML policy has the settings that are specific to training, such as the number of nodes to launch ([Kubeflow ML policy](https://www.kubeflow.org/docs/components/trainer/operator-guides/ml-policy/)).
- **Run it as a TrainJob: how many pods.** For a runtime with an empty ML policy, the number of pods follows `numNodes` ([Kubeflow ML policy](https://www.kubeflow.org/docs/components/trainer/operator-guides/ml-policy/)). That page does not say it in the same words for the `torch` policy, which starts the job with `torchrun`. This page's reading: one pod for each node.
- **Run it as a TrainJob: the command.** The manifest sets no `command`. Whether a `command` on the TrainJob keeps the runtime's `torchrun` launch is not known from sources.
- **Run it as a TrainJob: EFA, hugepages and shared memory.** No page read shows a runtime or TrainJob that asks for `vpc.amazonaws.com/efa`, hugepages or `/dev/shm`. [Appendix B](#d-ai-recipes-appendix-b-kubeflow-trainer-fields-the-examples-leave-out) lists the other fields.
- **Run it as a JobSet: the restart count.** `spec.failurePolicy.maxRestarts` says how many times a JobSet restarts, and a restart recreates all child jobs ([JobSet](https://jobset.sigs.k8s.io/docs/concepts/)). [Regions, the WAN and failover](#d10-8) explains the failure policy in the point "Kubernetes tools for another cluster: JobSet".
- **Run it as a JobSet: with and without a failure policy.** With a failure policy, the set is recreated and the work can resume from its last checkpoint. With none, the set fails when any job fails ([JobSet overview](https://jobset.sigs.k8s.io/docs/overview/)).
- **Run it as a JobSet: the API version.** The API version is `v1alpha2` ([JobSet overview](https://jobset.sigs.k8s.io/docs/overview/)).
- **Run it as a JobSet: the manifest.** This is the smallest JobSet with a failure policy. The numbers are illustrative, except `parallelism`, `completions` and `backoffLimit`, which are the values in the JobSet documentation's own example. [Example 1](#d17-2) has a full JobSet with queue, topology and EFA fields.

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: finetune-restart               # illustrative
spec:
  failurePolicy:
    maxRestarts: 3                     # illustrative #!
  replicatedJobs:
  - name: workers
    template:
      spec:
        parallelism: 4
        completions: 4
        backoffLimit: 0
        template:
          spec:
            restartPolicy: Never
            containers:
            - name: trainer
              image: example.com/my-finetune-image:latest   # illustrative
```

- **Where the run restarts: torchrun.** `torchrun` is PyTorch's launcher. When a worker fails, it stops all workers and restarts them, up to `max_restarts` times ([PyTorch elastic](https://docs.pytorch.org/docs/2.15/elastic/run.html)).
- **Where the run restarts: the script saves progress.** On failures or membership changes, all surviving workers are killed immediately, so the script must save its progress. PyTorch's recommended structure loads the checkpoint first ([PyTorch elastic](https://docs.pytorch.org/docs/2.15/elastic/run.html)). The script is yours, and no page read shows one.
- **Where the run restarts: the Job limits.** A Job's `backoffLimit` is 6 by default, and its `activeDeadlineSeconds` takes precedence over it ([Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)).
- **Where the run restarts: where the checkpoint lives.** A restarted pod can load a checkpoint only from storage that it can read. [Training data and checkpoints](#d16-1) covers the storage paths and what each one loses when a node is lost. [Regions, the WAN and failover](#d10-8) covers a restart in another Region.
- **What moves: the initializers.** A TrainJob can pull the dataset and the model from a storage address through initializers. Its type file gives `s3://` as an example address ([Kubeflow types](https://raw.githubusercontent.com/kubeflow/trainer/v2.4.0/pkg/apis/trainer/v1alpha1/trainjob_types.go)).
- **What moves: the TorchTune guide.** The guide says its built-in trainer supports only datasets from Hugging Face. It also says that the TrainJob creates a claim that holds the model and dataset and can be shared across pods ([Kubeflow TorchTune guide](https://www.kubeflow.org/docs/components/trainer/user-guides/builtin-trainer/torchtune/)). The type file and the guide differ on S3, so whether an S3 dataset works is not known from sources.
- **What moves: the adapter.** The Kubeflow PyTorch guide says you can export the fine-tuned model only on the node with rank 0 ([Kubeflow PyTorch guide](https://www.kubeflow.org/docs/components/trainer/user-guides/pytorch/)). This page's reading: one rank writes the adapter, so one writer is enough.
- **What moves: writing the adapter to a bucket.** Mountpoint, the program behind the S3 volume driver, can create new files. It writes them in order from the start of the file, and only one writer at a time ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)). Whether the library that saves an adapter writes that way is not known from sources. [Training data and checkpoints](#d16-1) explains the volume.
- **Several servers: what FSDP does.** FSDP is useful for models that do not fit into the memory of one GPU ([Kubeflow PyTorch guide](https://www.kubeflow.org/docs/components/trainer/user-guides/pytorch/)). Before the forward and backward pass, it gathers the sharded parameters on every rank ([PyTorch FSDP tutorial](https://docs.pytorch.org/tutorials/intermediate/FSDP_tutorial.html)).
- **Several servers: what FSDP means for the pods.** That is why row 3 of the picture needs the pre-training rules. This page's reading: a multi-node FSDP fine-tune is a placement problem like pre-training. [Appendix B of Deep dive 15](#d-ai-path-appendix-b-all-reduce-ring-and-tree-ddp-and-fsdp-in-detail) has the all-reduce arithmetic. How much data a LoRA fine-tune sends between servers is not known from sources.
- **Other pages on fine-tuning.** [Full fine-tuning vs. LoRA](#p05-2), [Serving many adapters on one base model](#p05-3), [Running tuning jobs with Kubeflow Trainer](#p05-4) and [Data, network and checkpoints](#p05-5) in Post 05 show the same job with code. [The experiment platform on Kubernetes](#ds06-10) in Design 6 covers post-training as a platform.
- **Not known from sources.**
  - A time or a cost for a LoRA or QLoRA fine-tune on Kubernetes. Two incidental figures exist, one run each and not a general rule. [Appendix A](#d-ai-recipes-appendix-a-fine-tuning-figures-from-the-papers-and-the-guides) lists them.
  - An adapter size for any model other than GPT-3 175B at rank 4.
  - How much data a LoRA job sends between servers on each step.
  - Whether a `command` on a TrainJob keeps the runtime's `torchrun` launch.
  - Whether Kueue topology-aware scheduling works for a TrainJob. Kueue's page shows only the queue label.
  - Whether an S3 dataset works with the Kubeflow initializers.
  - Whether the library that saves an adapter can write to a Mountpoint volume.

### Worked examples: four jobs as manifests, and where each field is explained

**The Kubernetes question.** Put the pieces together. What do four realistic jobs look like as manifests, and which section explains each field?

**Scenario.** This scenario is illustrative. A platform team has read Deep dives 10, 15 and 16 and now has to write the manifests. It needs four manifests. The first is a 32-GPU training job. The second is a server for a model of 140 GB. The third is a fine-tune on one GPU. The fourth is the training job again, in a second Region after a failure. Each field must trace back to a reason.

![Worked examples: which section each example uses](diagrams/sections/d17-2.svg)

**What the picture shows.** A grid. The four examples are the rows. The topics of Deep dives 2, 3, 10, 15 and 16 are the columns. A dot means that the example sets a field for that topic. A ring means that the field is optional. The picture has no numbers. It shows where to look, and the points of this section give the fields.

- **Example 1, the 32-GPU training job.** A JobSet with a queue, a network block, a zone, EFA and hugepages. It also has a checkpoint volume, a restart rule and a sidecar that copies checkpoints away.
- **Example 2, the 140 GB model server.** A Deployment with GPUs and the weights in an image volume. The server image is already on the node. Probes wait for the load, and replicas spread over zones.
- **Example 3, the fine-tune on one GPU.** A Job with one GPU and the base model in an image volume. The dataset sits on a bucket volume, and the adapter is written to a second bucket volume.
- **Example 4, the second Region.** The changes to Example 1 after a Region failure. Example 4 changes more than the zone and the image. The point "Example 4: what changes" lists every field.
- **How to read each table.** A table lists the field, its value and why it is set. The last column names the section of Deep dives 2, 3, 10, 15 or 16 that explains the field. A cell that says "Shown in" links a manifest that shows the field and does not explain it. Every value is illustrative unless the row says that it comes from a source.

**Going deeper, by topic.**

- **Example 1: what the job asks for.** It runs 4 pods. Each pod takes one server with 8 GPUs, so the job uses 32 GPUs (derived). The pods must start together, sit in one network block and one zone, and use EFA. The checkpoints go to a fast file system, and a sidecar copies them to a bucket.
- **Example 1: the manifest.** All names, the zone, the image and the queue are illustrative. The EFA count, the hugepages size and the memory are the values in AWS's own test on a p5.48xlarge. The level name in the topology annotation is Kueue's made-up example.

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: train-32gpu                    # illustrative
  labels:
    kueue.x-k8s.io/queue-name: train-queue   # illustrative queue #!
spec:
  failurePolicy:
    maxRestarts: 3                     # illustrative #!
  replicatedJobs:
  - name: workers
    replicas: 1
    template:
      spec:
        parallelism: 4
        completions: 4
        backoffLimit: 0
        template:
          metadata:
            annotations:
              kueue.x-k8s.io/podset-required-topology: cloud.provider.com/topology-rack   # Kueue's example level #!
          spec:
            restartPolicy: Never
            nodeSelector:
              topology.kubernetes.io/zone: us-east-1c   # illustrative zone #!
            containers:
            - name: trainer
              image: example.com/my-training-image:latest   # illustrative
              env:
              - name: NCCL_DEBUG
                value: INFO
              resources:
                limits:
                  nvidia.com/gpu: 8
                  vpc.amazonaws.com/efa: 32      # AWS's p5.48xlarge test #!
                  hugepages-2Mi: 5120Mi          # AWS's test value #!
                  memory: 32000Mi                # AWS's test value
              volumeMounts:
              - name: checkpoints
                mountPath: /checkpoints
            volumes:
            - name: checkpoints
              persistentVolumeClaim:
                claimName: fsx-claim             # the FSx driver's example name
```

- **Example 1: field by field.** The table says why each field is set and where it is explained.

| Field | Value | Why | Explained in |
|---|---|---|---|
| `kind` and `apiVersion` | JobSet, `jobset.x-k8s.io/v1alpha2` | A JobSet runs a group of Jobs as one unit. The API stage is `v1alpha2` ([JobSet overview](https://jobset.sigs.k8s.io/docs/overview/)) | [Racks and network blocks](#d10-6), point "Let JobSet do it" |
| `metadata.labels` `kueue.x-k8s.io/queue-name` | a queue name (illustrative) | The label names the queue that decides when the job starts ([Kueue JobSets](https://kueue.sigs.k8s.io/docs/tasks/run/jobsets/)) | [Racks and network blocks](#d10-6), point "Let a queue do it: Kueue annotations", and [Queueing and fair sharing](#d02-4) |
| `spec.failurePolicy.maxRestarts` | 3 (illustrative) | A restart recreates all child jobs, and the JobSet fails when the number is reached ([JobSet](https://jobset.sigs.k8s.io/docs/concepts/)) | [Regions, the WAN and failover](#d10-8), point "Kubernetes tools for another cluster: JobSet" |
| `parallelism`, `completions`, `backoffLimit` | 4, 4, 0 | These are the values in Kueue's own JobSet example ([Kueue JobSets](https://kueue.sigs.k8s.io/docs/tasks/run/jobsets/)). Four pods of 8 GPUs make 32 GPUs (derived) | Shown in [Racks and network blocks](#d10-6), the manifest in the point "Let JobSet do it" |
| pod annotation `kueue.x-k8s.io/podset-required-topology` | a level label (Kueue's example name) | With `required`, all pods go into one domain at that level, or the workload waits. The annotation goes on the pod template, not on the Job ([Kueue topology scheduling](https://kueue.sigs.k8s.io/docs/tasks/run/topology_aware_scheduling/)) | [Racks and network blocks](#d10-6), points "Let a queue do it: Kueue setup" and "Let a queue do it: Kueue annotations" |
| `restartPolicy` | Never | A Job allows only `Never` or `OnFailure` ([Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)) | Shown in [Racks and network blocks](#d10-6), the manifest in the point "Keep all pods in one group: pod affinity" |
| `nodeSelector` `topology.kubernetes.io/zone` | one zone (illustrative) | EFA traffic cannot cross zones ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)). A node selector requires a node label ([Kubernetes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)) | [Zones](#d10-7), point "Pin a training job to one zone: node selector" |
| `limits` `nvidia.com/gpu` | 8 | A p5.48xlarge has 8 H100 GPUs ([AWS instance table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)). GPUs go in `limits` ([Kubernetes GPUs](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)) | [How GPUs are advertised](#d06-2), and rung 1 in [The distance ladder](#d15-1) |
| `limits` `vpc.amazonaws.com/efa` | 32 | AWS says this request claims all EFA devices on a p5.48xlarge ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). The count depends on the instance | [Leaving the server](#d15-4), point "How many EFA devices the pod asks for" |
| `limits` `hugepages-2Mi` | 5120Mi | AWS's test value. AWS says to size the request against what your nodes report ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)) | [Leaving the server](#d15-4), points "Huge pages" and "Huge pages and a Pending pod" |
| `limits` `memory` | 32000Mi | Kubernetes says that a pod that requests huge pages must request memory or CPU as well ([Kubernetes huge pages](https://kubernetes.io/docs/tasks/manage-hugepages/scheduling-hugepages/)). The value is AWS's test value | No section of Deep dives 10, 15 or 16 explains the memory request. The Kubernetes page in this row is the source |
| `env` `NCCL_DEBUG` | INFO | It makes NCCL log which network it picked. On EFA the log line says `NET/OFI Selected Provider is efa` ([AWS: NCCL on EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-start-nccl.html)) | [Latency and EFA](#d10-5), point "Check that EFA is really in use" |
| `volumes` `persistentVolumeClaim` | `fsx-claim` | The name is the FSx driver's example claim. It asks for `ReadWriteMany` ([FSx CSI example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/blob/master/examples/kubernetes/dynamic_provisioning/specs/claim.yaml)) | [Training data and checkpoints](#d16-1), points "FSx for Lustre: the StorageClass" and "FSx for Lustre: the claim" |
| `initContainers[]` with `restartPolicy: Always`, the sidecar | `ckpt-uploader` (illustrative) | A sidecar runs beside the trainer and copies checkpoints to a bucket. It is stable since Kubernetes v1.33, and it does not stop the Job from completing ([Kubernetes sidecars](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)) | [Regions, the WAN and failover](#d10-8), point "Checkpoints", and [Training data and checkpoints](#d16-1), point "Checkpoints: what PyTorch does" |

- **Example 1: the gang start is not in the manifest.** Kueue admits a workload by quota. Kueue's page says that on a default configuration a pair of synchronized jobs may deadlock when the physical resources do not match the quotas. The `waitForPodsReady` setting makes Kueue watch the workload until all pods are ready, and it evicts and requeues the workload if they are not ready within a timeout ([Kueue all-or-nothing](https://kueue.sigs.k8s.io/docs/tasks/manage/setup_wait_for_pods_ready/)). It is a setting in the Kueue configuration, which an administrator owns. The page's own snippet sets a 30-minute timeout, a 5-minute recovery timeout and `blockAdmission` false. Whether the setting is on by default is not known from sources. [Gang scheduling](#d02-2) in Deep dive 3 explains the idea. [What a training job asks of the cluster](#d15-2) in Deep dive 15 has the point "Ask for the whole group at once".

```yaml
waitForPodsReady:
  timeout: 30m          # the page's default for the timeout
  recoveryTimeout: 5m
  blockAdmission: false
```

- **Example 1: the sidecar that copies checkpoints away.** A sidecar in a pod is an init container with `restartPolicy: Always` ([Kubernetes sidecars](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)). The feature is stable since Kubernetes v1.33, and a sidecar does not stop the Job from completing after the main containers finish. The image is illustrative and is not a published tool. [Tiered checkpoint storage](#ds20-7) in Design 20 shows the design. These lines go under the pod `spec` of Example 1.

```yaml
initContainers:
- name: ckpt-uploader
  image: example.com/ckpt-uploader:latest   # illustrative, not a published tool
  restartPolicy: Always                     # makes it a sidecar #!
  env:
  - name: DEST_BUCKET
    value: example-checkpoints-us-east-1    # illustrative
  volumeMounts:
  - name: checkpoints
    mountPath: /checkpoints
    readOnly: true
```

- **Example 1: no shared-memory volume.** The manifest sets no `/dev/shm` volume. AWS's NCCL test mounts the host's `/dev/shm`, and no source read states a size rule for it ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **Example 1: the two EFA forms do not mix.** The EFA device plugin form shown here cannot be mixed with the DRA form. AWS says not to add `vpc.amazonaws.com/efa` or `nvidia.com/gpu` to the limits of a DRA pod ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).
- **Example 1: the checkpoint-loading script.** The script that loads the newest checkpoint is yours ([PyTorch elastic](https://docs.pytorch.org/docs/2.15/elastic/run.html)).
- **Example 2: what the server asks for.** vLLM is an open-source model server, a program that loads a model and answers requests ([Post 01](#p01-2)). A model with 70 billion parameters in 16-bit numbers has 140 GB of weights ([Serving and loading heavy models](#d16-2)). That is 130.4 GiB (derived: 140 x 10^9 / 2^30). One H100 has 80 GiB ([AWS instance table](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html)), so the weights need at least 2 GPUs, which leave 29.6 GiB of GPU memory for everything else (derived). The cache of past keys and values and the working memory also need space, and no source read says how much. So 2 is a lower bound.
- **Example 2: the manifest.** All names, the registry, the replica count, `maxSkew` and the probe numbers are illustrative. The image name, the port, the `/health` path and the shared-memory volume follow the vLLM documentation's own Deployment. The weights come from an image in a registry, as a read-only volume.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-server                     # illustrative
spec:
  replicas: 3                          # illustrative
  selector:
    matchLabels:
      app: llm-server
  template:
    metadata:
      labels:
        app: llm-server
    spec:
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: topology.kubernetes.io/zone
        whenUnsatisfiable: DoNotSchedule
        labelSelector:
          matchLabels:
            app: llm-server
      containers:
      - name: vllm
        image: vllm/vllm-openai:latest
        imagePullPolicy: IfNotPresent            # use the pre-pulled image #!
        command: ["/bin/sh", "-c"]
        args: ["vllm serve /models/llm --tensor-parallel-size 2"]   # /models/llm is illustrative
        ports:
        - containerPort: 8000
        resources:
          limits:
            nvidia.com/gpu: 2                    # lower bound, derived #!
        volumeMounts:
        - name: weights
          mountPath: /models
          readOnly: true
        - name: shm
          mountPath: /dev/shm
        startupProbe:
          httpGet:
            path: /health
            port: 8000
          periodSeconds: 10
          failureThreshold: 90                   # 900 s; set it from your measured load time #!
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          periodSeconds: 5
      volumes:
      - name: weights
        image:
          reference: registry.example.com/models/llm-70b:v1   # illustrative #!
          pullPolicy: IfNotPresent
      - name: shm
        emptyDir:
          medium: Memory
          sizeLimit: 2Gi                         # vLLM's example value
```

- **Example 2: field by field.** The table says why each field is set and where it is explained.

| Field | Value | Why | Explained in |
|---|---|---|---|
| `spec.replicas` | 3 (illustrative) | The number of desired pods. It defaults to 1 ([Kubernetes Deployment API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/)) | [Deploying by hand](#p01-3) in Post 01 |
| `topologySpreadConstraints` | `maxSkew` 1 (illustrative), the zone label, `DoNotSchedule`, the app label | It keeps replicas even across zones. `DoNotSchedule` tells the scheduler not to schedule a pod that would break the rule ([Kubernetes topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)) | [Zones](#d10-7), point "Serving spreads over zones", and [Spreading across AZs and nodes](#d08-4) |
| `imagePullPolicy` | `IfNotPresent` | The kubelet pulls the image only if it is not already on the node ([Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)) | [Serving and loading heavy models](#d16-2), point "Image pull: put the image on the node ahead of time" |
| `args` `vllm serve ... --tensor-parallel-size` | 2 | `vllm serve` takes a model name or a path. The flag sets the number of tensor-parallel groups ([vLLM serve](https://docs.vllm.ai/en/latest/cli/serve/)). The Megatron-LM paper advises tensor parallelism up to the number of GPUs in one server ([Megatron-LM paper](https://ar5iv.labs.arxiv.org/html/2104.04473)) | [What a training job asks of the cluster](#d15-2) |
| `limits` `nvidia.com/gpu` | 2 | Lower bound from the weights, derived in the point "Example 2: what the server asks for" | [How GPUs are advertised](#d06-2) |
| `volumes[].image` `reference`, `pullPolicy` | an image reference (illustrative), `IfNotPresent` | An image volume mounts an image or artifact read-only. A failure to resolve or pull it blocks the containers and may add significant latency ([Kubernetes Pod API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/)). The container runtime must support it ([Kubernetes image volumes](https://kubernetes.io/docs/tasks/configure-pod-container/image-volumes/)) | [Serving and loading heavy models](#d16-2), points "Weights: an image volume", "Weights: the image volume feature gate" and "Weights: image volume support in runtimes" |
| `volumes[].emptyDir` `medium: Memory` | `sizeLimit` 2Gi (vLLM's example) | vLLM needs the host's shared memory for tensor parallel inference ([vLLM Kubernetes](https://docs.vllm.ai/en/latest/deployment/k8s/)). Without `sizeLimit` the limit is undefined ([Kubernetes Pod API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/)) | [Training data and checkpoints](#d16-1), point "emptyDir and generic ephemeral volumes" |
| `startupProbe` | `failureThreshold` 90, `periodSeconds` 10 | `failureThreshold` times `periodSeconds` should cover the worst-case startup time ([Kubernetes probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)). 90 x 10 is 900 seconds (derived). This is Post 01's number, not a measurement | [Serving and loading heavy models](#d16-2), point "Wait for the load: the probes" |
| `readinessProbe` | `/health` on port 8000 | A pod that is not ready gets no traffic from a Service. `/health` is the path in vLLM's own Deployment ([vLLM Kubernetes](https://docs.vllm.ai/en/latest/deployment/k8s/)) | [Serving and loading heavy models](#d16-2), point "Step 4, ready for traffic" |
| `strategy.rollingUpdate` | not set; both limits default to 25 percent | `maxUnavailable` and `maxSurge` are the two limits of a rolling update ([Kubernetes Deployment API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/)). Every new replica takes the whole load time, and a surge pod needs a free GPU (this page's reading) | [Serving and loading heavy models](#d16-2), point "Wait for the load: the rolling update" |

- **Example 2: node settings for parallel image pulls.** The kubelet pulls one image at a time by default. These two settings belong to the kubelet's configuration on the node, not to the Deployment, so they cannot go into the manifest. A value of 2 or more for `maxParallelImagePulls` needs `serializeImagePulls` set to false ([Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)). The file's `apiVersion` and `kind` are in the [Kubernetes kubelet configuration reference](https://kubernetes.io/docs/reference/config-api/kubelet-config.v1beta1/). The value 3 is illustrative. [Serving and loading heavy models](#d16-2) explains both.

```yaml
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
serializeImagePulls: false      # default is true #!
maxParallelImagePulls: 3        # illustrative; default is no limit #!
```

- **Example 2: vLLM and image volumes.** Whether vLLM can serve from a read-only image-volume mount is not known from sources. The vLLM pages show a model name or a path, and they do not mention image volumes.
- **Example 2: no time to ready.** No source read gives a time from "pod scheduled" to "ready".
- **Example 2: no CPU or memory requests.** The manifest sets no CPU or memory requests. The vLLM Deployment that this page follows does set them, but only for a 7B model on one GPU ([vLLM Kubernetes](https://docs.vllm.ai/en/latest/deployment/k8s/)). No values were found for a 70B model.
- **Example 3: what the job asks for.** One GPU. The base model comes from an image volume. The dataset comes from a bucket volume that the pod only reads. The adapter goes to a second bucket volume that the pod writes. Row 1 of the picture in [Fine-tuning on Kubernetes](#d17-1) shows the size.
- **Example 3: the manifest.** All names, images and the deadline are illustrative. The claim `training-data-pvc` is the one in the [Deep dive 16 manifest](#d16-1) (point "Mount a bucket: the static volume"). The claim `adapter-out-pvc` is defined in the point "Example 3: the volume that takes the adapter".

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: finetune-support-tone          # illustrative
  labels:
    kueue.x-k8s.io/queue-name: user-queue   # optional, only if Kueue manages it
spec:
  backoffLimit: 2                      # illustrative #!
  activeDeadlineSeconds: 86400         # illustrative: 24 hours #!
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: tune
        image: example.com/my-finetune-image:latest   # illustrative
        command: ["python", "finetune.py"]            # illustrative
        resources:
          limits:
            nvidia.com/gpu: 1
        volumeMounts:
        - name: base-model
          mountPath: /models/base
          readOnly: true
        - name: dataset
          mountPath: /data
          readOnly: true
        - name: adapter-out
          mountPath: /out
      volumes:
      - name: base-model
        image:
          reference: registry.example.com/models/base:v1   # illustrative
          pullPolicy: IfNotPresent
      - name: dataset
        persistentVolumeClaim:
          claimName: training-data-pvc
      - name: adapter-out
        persistentVolumeClaim:
          claimName: adapter-out-pvc
```

- **Example 3: the volume that takes the adapter.** The S3 driver's own example mounts a bucket for `ReadWriteMany` and writes a file from a pod. The `region` option and the `prefix` option go into `mountOptions`. The bucket, Region and prefix are illustrative. The data volume is read-only in [Training data and checkpoints](#d16-1), and this one is writable.

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: adapter-out-pv
spec:
  capacity:
    storage: 1200Gi                    # ignored, but required
  accessModes:
    - ReadWriteMany
  storageClassName: ""                 # required for static provisioning
  claimRef:
    namespace: default
    name: adapter-out-pvc
  mountOptions:
    - region us-west-2                 # illustrative
    - prefix adapters/                 # illustrative
  csi:
    driver: s3.csi.aws.com
    volumeHandle: adapter-out-pv       # must be unique
    volumeAttributes:
      bucketName: example-adapters     # illustrative #!
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: adapter-out-pvc
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  resources:
    requests:
      storage: 1200Gi                  # ignored, but required
  volumeName: adapter-out-pv
```

- **Example 3: field by field.** The table says why each field is set and where it is explained.

| Field | Value | Why | Explained in |
|---|---|---|---|
| `kind` `Job`, `restartPolicy` | `Never` | A Job creates pods and retries them until a number of them finish. Only `Never` or `OnFailure` is allowed ([Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)) | Shown in [Racks and network blocks](#d10-6), the manifest in the point "Keep all pods in one group: pod affinity" |
| `backoffLimit`, `activeDeadlineSeconds` | 2, 86400 (illustrative) | The default `backoffLimit` is 6. The deadline takes precedence over the backoff limit ([Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)) | [Fine-tuning on Kubernetes](#d17-1), point "Where the run restarts: the Job limits" |
| label `kueue.x-k8s.io/queue-name` | a queue name | A plain Job needs the label to go to a queue ([Kueue Jobs](https://kueue.sigs.k8s.io/docs/tasks/run/jobs/)) | [Queueing and fair sharing](#d02-4) |
| `limits` `nvidia.com/gpu` | 1 | A limit alone is enough ([Kubernetes GPUs](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)) | [How GPUs are advertised](#d06-2) |
| `volumes[].image` for the base model | an image reference (illustrative) | An image volume, with the fields of the Example 2 table row `volumes[].image` | [Serving and loading heavy models](#d16-2), point "Weights: an image volume" |
| `persistentVolumeClaim` `training-data-pvc` | read-only mount | `ReadOnlyMany` states the intent, but Kubernetes does not enforce it once the storage is mounted ([Kubernetes volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)) | [Training data and checkpoints](#d16-1), points "Mount a bucket: choose the access mode" and "Mount a bucket: the static volume" |
| `persistentVolumeClaim` `adapter-out-pvc` | writable bucket volume | Mountpoint creates new files, in order, by one writer ([Mountpoint](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)) | [Training data and checkpoints](#d16-1), point "Object storage: what Mountpoint does not do" |
| PersistentVolume `adapter-out-pv`: `capacity`, `accessModes`, `storageClassName`, `claimRef`, `mountOptions`, `csi` | `1200Gi`, `ReadWriteMany`, an empty class, the claim's name, `region` and `prefix`, the S3 driver | The S3 driver's static example sets the same fields. `capacity` is required and ignored, and the empty class is required for static provisioning ([S3 CSI static example](https://raw.githubusercontent.com/awslabs/mountpoint-s3-csi-driver/main/examples/kubernetes/static_provisioning/static_provisioning.yaml)) | [Training data and checkpoints](#d16-1), point "Mount a bucket: the static volume" |

- **Example 3: the Kubeflow form.** The same job as a TrainJob with `numNodes: 1` is the manifest in [Fine-tuning on Kubernetes](#d17-1), point "Run it as a TrainJob: the manifest". [Running tuning jobs with Kubeflow Trainer](#p05-4) in Post 05 shows it with code.
- **Example 3: what it leaves out.** Whether the library that saves the adapter writes in order, as Mountpoint needs, is not known from sources. The script `finetune.py` is yours.
- **Example 4: what the second Region needs.** After the first Region fails, Example 1 runs in the second Region. [Regions, the WAN and failover](#d10-8) lists what must already be there. These are the replicated checkpoints and weights, the replicated images and the GPU nodes. A new fast file system is rebuilt and not copied. The point "Example 4: what changes" lists the fields that change.
- **Example 4: what changes.** ECR is Amazon Elastic Container Registry, a managed container image registry ([AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html)). The registry host changes because an ECR image reference contains the Region (`aws_account_id.dkr.ecr.registry_region.amazonaws.com`, [AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html)). The repository name stays the same after replication ([AWS ECR replication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/replication.html)). All values are illustrative.

| Field | Example 1 (first Region) | Example 4 (second Region) | Explained in |
|---|---|---|---|
| `nodeSelector` zone | a zone of the first Region | a zone of the second Region | [Zones](#d10-7) |
| `image` | registry host of the first Region | registry host of the second Region, same repository name | [Regions, the WAN and failover](#d10-8), points "Images" and "ECR replication" |
| `volumes[].image.reference`, if the weights sit in ECR | registry host of the first Region | registry host of the second Region ([Kubernetes Pod API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/)) | [Serving and loading heavy models](#d16-2) |
| sidecar `DEST_BUCKET` | the bucket in the first Region | the bucket in the second Region | [Regions, the WAN and failover](#d10-8), point "Checkpoints" |
| checkpoint volume `fsx-claim` | the file system in the first zone | a new file system in the second Region. Its StorageClass names a subnet of that zone. This is an administrator object, not a Job field | [Training data and checkpoints](#d16-1), point "FSx for Lustre: the file system must be in the right place" |
| a new volume `restore` | not present | a read-only volume on the replica bucket. It needs its own bucket name and `region` option | [Training data and checkpoints](#d16-1), point "Mount a bucket: the static volume" |

- **Example 4: the manifest.** First the volume that reads the replica bucket. The bucket name and the Region are illustrative. The S3 volume driver takes the `region` option in `mountOptions` and the bucket in `bucketName` ([S3 CSI configuration](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/main/docs/CONFIGURATION.md)). S3 replication needs versioning on both buckets, and a copy can lag behind the original ([AWS S3 replication](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)).

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ckpt-replica-pv
spec:
  capacity:
    storage: 1200Gi                    # ignored, but required
  accessModes:
    - ReadOnlyMany
  storageClassName: ""
  claimRef:
    namespace: default
    name: ckpt-replica-pvc
  mountOptions:
    - region us-west-2                 # the second Region, illustrative #!
  csi:
    driver: s3.csi.aws.com
    volumeHandle: ckpt-replica-pv
    volumeAttributes:
      bucketName: example-checkpoints-replica   # illustrative #!
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ckpt-replica-pvc
spec:
  accessModes:
    - ReadOnlyMany
  storageClassName: ""
  resources:
    requests:
      storage: 1200Gi                  # ignored, but required
  volumeName: ckpt-replica-pv
```

- **Example 4: the changed lines of the JobSet.** These lines replace the matching lines of Example 1. The account number is illustrative. The `restore` mount is new.

```yaml
nodeSelector:
  topology.kubernetes.io/zone: us-west-2b          # a zone of the second Region, illustrative #!
containers:
- name: trainer
  image: 111122223333.dkr.ecr.us-west-2.amazonaws.com/my-training-image:latest   # illustrative #!
  volumeMounts:
  - name: checkpoints
    mountPath: /checkpoints
  - name: restore
    mountPath: /restore
    readOnly: true
volumes:
- name: checkpoints
  persistentVolumeClaim:
    claimName: fsx-claim                # a new file system in the second Region
- name: restore
  persistentVolumeClaim:
    claimName: ckpt-replica-pvc
```

- **Example 4: the restore script.** The script must look in `/restore` for the newest complete checkpoint, and it is yours.
- **Example 4: a different cluster size.** PyTorch's Distributed Checkpoint can save at one cluster size and load at another ([PyTorch](https://docs.pytorch.org/docs/2.15/distributed.checkpoint.html)).
- **Example 4: manifests, secrets and credentials.** How the second cluster gets its manifests, custom resource definitions, secrets and S3 driver credentials is not known from sources. The S3 driver takes its credentials from the driver, so that the whole cluster shares one set, or from the pod ([S3 CSI configuration](https://github.com/awslabs/mountpoint-s3-csi-driver/blob/main/docs/CONFIGURATION.md)).
- **Example 4: failover time.** No source says how long such a failover takes.
- **Example 4: the other way, MultiKueue.** A TrainJob accepts `managedBy` with the value `kueue.x-k8s.io/multikueue` ([Kubeflow types](https://raw.githubusercontent.com/kubeflow/trainer/v2.4.0/pkg/apis/trainer/v1alpha1/trainjob_types.go)). MultiKueue is Kueue's way to search for capacity in other clusters and off-load the main cluster ([Kueue overview](https://kueue.sigs.k8s.io/docs/overview/)). Then a manager cluster dispatches the job to a worker cluster. [Sending work to the right cluster](#ds02-4) in Design 2 explains it. What MultiKueue does when a worker cluster is lost is not known from sources ([Regions, the WAN and failover](#d10-8)).
- **Where full manifests for single steps are.** [Four ways to get weights into a pod](#p02-3) and [Pattern 3 and 4: models as OCI images](#p02-4) in Post 02 hold full manifests for the init container and the image volume. Design 20 holds the uploader and the streamer, in [Tiered checkpoint storage](#ds20-7) and [Fast model loading for evaluation and inference](#ds20-9). This page uses pieces and links the full ones.
- **Not known from sources.**
  - Whether any of the four manifests works on a real cluster. None was applied.
  - Whether Kueue accepts the EKS layer labels as topology levels ([Racks and network blocks](#d10-6)). Example 1 uses Kueue's made-up level name.
  - Whether vLLM can serve from a read-only image volume.
  - Whether `waitForPodsReady` is on by default in Kueue.
  - How the second cluster gets its manifests, secrets and credentials.
  - How long a failover takes.

### Symptom to rung: where to look first when a job is slow, stuck or costly

**The Kubernetes question.** The job is slow, stuck or expensive. Which rung of the distance ladder is the likely cause, what do you check first, and which section tells you what to change? Two places are off the ladder: the storage path, and the node's image pull and weight load. The picture marks them "Not a rung".

**Scenario.** This scenario is illustrative. A job that ran well on one server now runs on four, and each step takes longer. Nothing crashed, and no log line says why. The team needs a short list of places to look, in order, and a link from each place to the section that explains it.

![Symptoms next to the rung of the distance ladder that they point to](diagrams/sections/d17-3.svg)

**What the picture shows.** The seven rungs of the distance ladder from [The distance ladder](#d15-1), with the symptoms that point to each rung in a red tag on the right. Two more rows at the bottom are not rungs: the storage path, and the node's image pull and weight load. The bold words at the right end of a tag give the title of the section that explains the fix. Each tag matches one row of the table. The picture has no numbers.

- **What the table is.** Each row names a symptom that has a source and the rung or place to check first. It gives a first check from a section of Deep dives 10, 15 or 16 or from a source page. It ends with the section that says what to change. The table adds no claims except the three rows marked "no section yet" and the three commands that the points named "First check from a source page" give.
- **What the table is not.** It says where to look first. It is not a diagnosis. A row marked "this page's reading" has no source that states the cause in so many words.
- **Rows with no section yet.** Four symptoms have a source, and no section of Deep dives 10, 15 or 16 explains their cause. Three are table rows marked "no section yet". The fourth, a Kueue job that does not start, has only an Appendix C point. [Appendix C](#d-ai-recipes-appendix-c-symptoms-with-a-source-but-no-explaining-section) lists all four.

**Going deeper, by topic.**

- **The table.** The first check names a command or a log line. Three of the commands are not in Deep dives 10, 15 or 16. The points named "First check from a source page" give them.

| Symptom | Rung or place to check first | First check | What to change | Read more |
|---|---|---|---|---|
| Step time rose when the job went from 1 to 4 servers. NCCL says that a bandwidth lower than the expected peak, as nvbandwidth reports it, indicates an issue with inter-GPU communication. nvbandwidth measures GPU-to-GPU bandwidth ([NCCL](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/performance_and_tuning.html)). The "1 to 4 servers" case is this page's reading | Rungs 2 to 4, and rung 5 if only some pods are far | Set `NCCL_DEBUG=INFO` and read the provider line. Inside one server, run nvbandwidth and compare its result with the hardware peak | Depends on what the check shows. Use the other rows of this table | [What a training job asks of the cluster](#d15-2), [Leaving the server](#d15-4), [Latency and EFA](#d10-5) |
| NCCL log shows `NET/Socket` instead of EFA, InfiniBand or RoCE. NCCL's log page shows the socket line in its example ([NCCL logging](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/logging.html)). That it means a fallback is this page's reading | Rung 4 | `NCCL_DEBUG=INFO` with `NCCL_DEBUG_SUBSYS=NET`. On EFA, look for `NET/OFI Selected Provider is efa` | Ask for the fast device: the EFA resource and hugepages, or an RDMA pool | [Leaving the server](#d15-4), [Latency and EFA](#d10-5) |
| A pod is Pending after the manifest asked for `hugepages-2Mi`. AWS says a pod that asks for hugepages on a node with no EFA device stays Pending ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)) | Rung 4 | `kubectl describe pod`, and the node capacity command in the point "First check from a source page: the hugepages that nodes report" | Run the pod on a node with an EFA device. Size the hugepages request against what the nodes report | [Leaving the server](#d15-4), points "Huge pages" and "Huge pages and a Pending pod" |
| A pod stays in `ContainerCreating` when two pods share one EFA claim. AWS says that the EFA DRA driver supports one pod per claim, and that the kubelet reports the event `driver only supports one pod per claim` ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)) | Rung 4 | `kubectl describe pod`. Read the events | Use a ResourceClaimTemplate, so that each pod gets its own claim | No section yet. [Appendix C](#d-ai-recipes-appendix-c-symptoms-with-a-source-but-no-explaining-section), point "A pod stays in ContainerCreating when two pods share one EFA claim" |
| NCCL warns `Call to ibv_create_qp failed` in an InfiniBand pod. NCCL's networking page lists the warning ([NCCL networking](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/networking_troubleshooting.html)) | Rung 4 | Read the NCCL log for the warning | Remove the user limits on pinned memory (`memlock`). How to do that for a pod is not known from sources | No section yet. [Appendix C](#d-ai-recipes-appendix-c-symptoms-with-a-source-but-no-explaining-section), point "NCCL warns ibv_create_qp failed in an InfiniBand pod" |
| A new FSx claim stays Pending. AWS says that the status may show Pending for 5 to 10 minutes before it changes to Bound ([AWS EKS FSx](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html)) | Off the ladder: the storage path | Read the claim's status again after 5 to 10 minutes | Nothing. This is a wait and not a fault | No section yet. [Appendix C](#d-ai-recipes-appendix-c-symptoms-with-a-source-but-no-explaining-section), point "A new FSx claim stays Pending" |
| A pod is Pending, and its rule requires a network label. AWS says replicas that do not fit in the first pod's domain stay Pending, and that the rule leaves every pod Pending if no node reports the label ([AWS EKS topology](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)) | Rung 5 | `kubectl get nodes -L topology.k8s.aws/network-node-layer-1 -L topology.k8s.aws/network-node-layer-2 -L topology.k8s.aws/network-node-layer-3`, as in [Racks and network blocks](#d10-6) | Check that the nodes carry the label. Use the `preferred` form of the rule, or a wider layer | [Racks and network blocks](#d10-6), points "Where the labels are missing" and "Narrower is not always better" |
| A Kueue workload waits. Kueue says that if no single domain has enough capacity, the workload waits ([Kueue topology scheduling](https://kueue.sigs.k8s.io/docs/tasks/run/topology_aware_scheduling/)) | Rung 5 | `kubectl describe workload`, in the point "First check from a source page: a Kueue workload" | Use `podset-preferred-topology` instead of `required`, or a wider level | [Racks and network blocks](#d10-6), point "Let a queue do it: Kueue annotations" |
| Pods of one job sit in different network groups. AWS says the first pod schedules onto any eligible node, and that the labels are a best-effort optimization and not a guarantee of bandwidth ([AWS EKS topology](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)) | Rung 5 | `kubectl get pods -o wide`, then match each node against `kubectl get nodes -L topology.k8s.aws/network-node-layer-1 -L topology.k8s.aws/network-node-layer-2 -L topology.k8s.aws/network-node-layer-3` | Add pod affinity on the layer label, or a Kueue topology rule. If you need a guarantee, reserve the capacity with a Capacity Block. A Capacity Block reserves GPU instances on a future date and places them close together ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)) | [Racks and network blocks](#d10-6) |
| The GPU and the NIC sit under different CPUs. NCCL says incorrect process placement with respect to the CPU and memory can have a serious performance impact ([NCCL](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/performance_and_tuning.html)). That a GPU and a NIC under different CPUs slow a job is this page's reading | Rung 3 | `nvidia-smi topo -m`. Look at the code between the GPU and its NIC | Topology Manager, a DRA claim on the PCIe root, or the EFA alignment | [Inside the server](#d15-3), and [Reading the topology matrix](#d-ai-path-appendix-c-reading-the-topology-matrix) |
| The job works in one zone and fails or slows across zones. AWS says EFA traffic cannot cross zones or VPCs ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)) | Rung 6 | `kubectl get pods -o wide` and `kubectl get nodes -L topology.kubernetes.io/zone`. Match them | Pin the job to one zone with a node selector | [Zones](#d10-7), [Latency and EFA](#d10-5) |
| The cross-zone bill is high. AWS charges $0.01/GB in each direction between zones of one Region, and the same for FSx for Lustre from another zone ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/), [AWS FSx pricing](https://aws.amazon.com/fsx/lustre/pricing/), read 2026-11-21) | Rung 6 | `kubectl get pods -o wide` and `kubectl get nodes -L topology.kubernetes.io/zone` give the zone of each pod. The file system sits in the subnet that its StorageClass names, and a subnet sits in one zone (this page's reading). No page gives a command that prints the file system's zone | Put the file system and the pods in one zone | [Zones](#d10-7), point "A crossing costs money", and [Training data and checkpoints](#d16-1) |
| The cross-Region bill is high. AWS prices data sent out of a Region per GB ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/index.json), 2026-11-19) | Rung 7 | Find what each pod reads across the link: image, weights, dataset or checkpoint | Copy once and read locally: replicate the bucket and the images | [Regions, the WAN and failover](#d10-8) and [its appendix](#d-networking-appendix-aws-options-for-crossing-regions-numbers-and-prices) |
| A replica takes many minutes to become ready. Only parts of the wait were measured, and no source gives the total. This row is this page's reading | Off the ladder: the node's image pull, weight fetch and load | `kubectl describe pod` and read its events. The Kubernetes debug page says the output of that command should be informative for a pod that is stuck waiting ([Kubernetes debug pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)) | Change the link of the chain that is slow: parallel pulls, a pre-pulled image, an image volume, a streamer | [Serving and loading heavy models](#d16-2) |
| Training throughput falls and the data loader may be the cause. AWS says poor disk performance can delay image reads, increase pod startup latency and degrade training throughput ([AWS EKS best practices](https://docs.aws.amazon.com/eks/latest/best-practices/aiml-storage.html)). That the GPUs wait is this page's reading | Off the ladder: the storage path | AWS says to use CloudWatch to monitor the performance metrics of the storage service | Pick the path that fits the job: the decision table of the storage paths | [Training data and checkpoints](#d16-1) |
| Each checkpoint stalls training. PyTorch says checkpointing is often a bottleneck in the critical path of distributed training ([PyTorch recipe](https://docs.pytorch.org/tutorials/recipes/distributed_async_checkpoint_recipe.html)) | Off the ladder: the storage path | Compare the step time at checkpoint steps with other steps. No page gives a command for it | Save in parallel and asynchronously, to a path that many ranks can write | [Training data and checkpoints](#d16-1), point "Checkpoints: what PyTorch does" |
| After a failure the job restarts from old state or cannot start. torchrun kills all surviving workers on a failure, so only saved progress is left ([PyTorch elastic](https://docs.pytorch.org/docs/2.15/elastic/run.html)). S3 replication copies after the write, so a copy can lag ([AWS S3 replication](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)). The whole row is this page's reading | Off the ladder: the storage path, and rung 7 | Which checkpoint the job loaded. The script logs it, and no page gives a command | Put checkpoints on storage that the new pods can read. Allow for replication lag | [Regions, the WAN and failover](#d10-8), point "The gap that matters", and [Training data and checkpoints](#d16-1) |
| One copy stream stops near 5 Gbps on a big instance. AWS limits single-flow traffic to 5 Gbps when the instances are not in one cluster placement group, which is a grouping of instances inside one zone ([AWS EC2 bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html)). Applying the limit to a storage read is this page's reading | Rungs 6 and 7, and off the ladder: the storage path | Run the same copy again and compare it with the 5 Gbps limit. No page gives a command | AWS lists three ways to raise the limit: a cluster placement group (up to 10 Gbps), multipath TCP, which sets up several paths between two endpoints, and ENA Express in one zone (up to 25 Gbps). ENA Express is an ENA feature that adds path selection and congestion control ([AWS FSx EFA](https://docs.aws.amazon.com/fsx/latest/LustreGuide/efa-file-systems.html)) | [Regions, the WAN and failover](#d10-8), point "Worked example: the time", and [Serving and loading heavy models](#d16-2) |

- **First check from a source page: the events of a pod.** The command lists a pod's events. The Kubernetes debug page says to read the scheduler's messages there ([Kubernetes debug pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)).

```bash
kubectl describe pod <pod-name>
```

- **First check from a source page: the hugepages that nodes report.** The command prints the 2 MiB hugepages that each node reports. The AWS EKS EFA guide gives it ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)).

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,2Mi:.status.capacity.hugepages-2Mi'
```

- **First check from a source page: a Kueue workload.** The command shows a Kueue workload and its admission status. Kueue's page shows it for a Job, with a Workload name of the form `job-my-job-19797`. The page does not give the name of a JobSet's Workload ([Kueue troubleshooting](https://kueue.sigs.k8s.io/docs/tasks/troubleshooting/troubleshooting_jobs/)).

```bash
kubectl describe workload -n <namespace> <workload-name>
```

- **Finding a bad link.** Design 14 and Design 23 each open with a written scenario and cite no measurement, so they are links and not evidence. [Stragglers, hangs and silent errors](#ds14-9) in Design 14 covers a slow link inside a job. [Network health: finding the bad link](#ds23-4) in Design 23 covers the check.
- **Rows marked "this page's reading".** A source can say that a condition exists and not say that it causes the symptom. The row says so in that case.
- **Not known from sources.**
  - A check command for "which checkpoint did the job load". It depends on the script.
  - A command to run the same copy with several parallel streams.
  - Any source that says the GPUs sit idle when the data loader is slow. AWS says only that throughput can degrade.
  - What Kueue names the Workload of a JobSet.
  - A source for "a single flow is slow while many flows are fast". The AWS page gives the single-flow limit and no figure for many flows, so the table has the row "one copy stream stops near 5 Gbps" instead.

### Appendix A. Fine-tuning figures from the papers and the guides

**This appendix holds fine-tuning figures that do not change what you request or configure.** Each point has its source.

**Going deeper, by topic.**

- **QLoRA: one measured duration.** The QLoRA abstract says its best model, Guanaco, needed only 24 hours of fine-tuning on a single GPU ([QLoRA paper](https://arxiv.org/abs/2305.14314)). The abstract does not name the GPU. It is one run and not a general fine-tune time.
- **Kubeflow TorchTune guide: one sample log.** The guide's sample log shows a full fine-tune of a 1-billion-parameter model on 52,002 examples. The step loop shows `1625/1625 [21:54<00:00, 1.23it/s]`, which is 21 minutes 54 seconds for 1,625 steps. The guide says that it needs 2 GPUs and names no GPU type. It is not a LoRA run. It is one example in one guide's documentation ([Kubeflow TorchTune guide](https://www.kubeflow.org/docs/components/trainer/user-guides/builtin-trainer/torchtune/)).
- **Kubeflow TorchTune guide: sizes in the sample log.** The sample log in the point "Kubeflow TorchTune guide: one sample log" shows a saved model of 2.30 GiB. It shows the model download by the initializer as `Fetching 8 files` in 1 minute 2 seconds. The network and the source of the download are not stated ([Kubeflow TorchTune guide](https://www.kubeflow.org/docs/components/trainer/user-guides/builtin-trainer/torchtune/)).
- **Fine-tuning figures against one server.** This is derived, and the sizes are the papers'. 640 GiB is 640 x 1.0737 = about 687 GB. The 350 GB of LoRA on GPT-3 175B is smaller than that. The 1.2 TB for a full fine-tune of the same model is larger than it, so it would need more than one such server. The papers do not say how many GPUs they used, so this compares sizes and nothing else.
- **QLoRA: how it works.** QLoRA sends gradients through a frozen, 4-bit quantized model into low-rank adapters ([QLoRA paper](https://arxiv.org/abs/2305.14314)). Quantized means, in plain words, that each weight is stored in fewer bits.
- **PEFT: an adapter size that this page does not use.** The PEFT checkpoint page gives a size example for an IA3 adapter on BERT. IA3 is another adapter method in PEFT's list, and it is not LoRA. This page does not quote it ([PEFT](https://huggingface.co/docs/peft/en/developer_guides/checkpoint)).
- **No cost figure.** No source read gives a cost for a fine-tune.

### Appendix B. Kubeflow Trainer fields the examples leave out

**This appendix lists fields of the TrainJob that the manifests of this page do not use.** It also gives one fact about what a TrainJob is built on. The fields come from the Go type file at tag v2.4.0. The Kubeflow releases page lists v2.4.0 as the latest release ([Kubeflow releases](https://github.com/kubeflow/trainer/releases)).

**Going deeper, by topic.**

- **Start and stop fields.** `suspend` says whether to suspend the running TrainJob. `activeDeadlineSeconds` is the time after the TrainJob starts before the system tries to terminate it. If `managedBy` is set, it must be `trainer.kubeflow.org/trainjob-controller` or `kueue.x-k8s.io/multikueue` if set ([Kubeflow types](https://raw.githubusercontent.com/kubeflow/trainer/v2.4.0/pkg/apis/trainer/v1alpha1/trainjob_types.go)).
- **Trainer fields.** `image`, `command`, `args` and `env` set the training container. `numProcPerNode` sets the processes on each node. `resourceClaimsPerNode` is for DRA and is alpha ([Kubeflow types](https://raw.githubusercontent.com/kubeflow/trainer/v2.4.0/pkg/apis/trainer/v1alpha1/trainjob_types.go)).
- **Initializers.** `dataset` and `model` each take a `storageUri`, `env` and a `secretRef` ([Kubeflow types](https://raw.githubusercontent.com/kubeflow/trainer/v2.4.0/pkg/apis/trainer/v1alpha1/trainjob_types.go)).
- **Runtime patches.** `runtimePatches` can patch the pod's volumes, node selector, affinity, tolerations, scheduler name, init containers and containers (environment, volume mounts, resources) ([Kubeflow types](https://raw.githubusercontent.com/kubeflow/trainer/v2.4.0/pkg/apis/trainer/v1alpha1/trainjob_types.go)).
- **Runtime patches and EFA.** No page read puts an extended resource such as EFA into a patch or into `resourcesPerNode`.
- **What a TrainJob is built on.** A TrainJob is built on JobSet ([Kueue TrainJobs](https://kueue.sigs.k8s.io/docs/tasks/run/trainjobs/)).
- **Variables that the torch policy injects.** The `torch` policy launches the job with `torchrun` and injects `PET_NNODES`, `PET_NPROC_PER_NODE`, `PET_NODE_RANK`, `PET_MASTER_ADDR` and `PET_MASTER_PORT` ([Kubeflow ML policy](https://www.kubeflow.org/docs/components/trainer/operator-guides/ml-policy/)).

### Appendix C. Symptoms with a source but no explaining section

**This appendix lists four symptoms that have a source. No section of Deep dives 10, 15 or 16 explains them.** Three are rows of the cheat sheet, marked "no section yet". The fourth has no row. Each point names the source.

**Going deeper, by topic.**

- **A pod stays in ContainerCreating when two pods share one EFA claim.** AWS says that the EFA DRA driver supports one pod per claim. The kubelet reports the event `driver only supports one pod per claim` ([AWS EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). The fix, a ResourceClaimTemplate for each pod, is in the AWS EKS EFA guide.
- **NCCL warns ibv_create_qp failed in an InfiniBand pod.** NCCL's networking page lists the warning. Its fix is to remove the user limits on pinned memory (`memlock`) in the system's limits file. How to do that for a pod is not known from sources ([NCCL networking](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/networking_troubleshooting.html)).
- **A Kueue-managed job does not start.** Kueue's troubleshooting page asks "Is my Job suspended?" and shows the Workload status ([Kueue troubleshooting](https://kueue.sigs.k8s.io/docs/tasks/troubleshooting/troubleshooting_jobs/)). No deep dive explains the causes yet.
- **A new FSx claim stays Pending.** AWS says the status may show Pending for 5 to 10 minutes before it changes to Bound ([AWS EKS FSx](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html)). This is a wait and not a fault.

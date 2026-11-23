---
id: d-data-platforms
title: "Data and pipeline platforms on Kubernetes"
short: "Data and pipeline platforms"
---
**This deep dive is not specific to one cloud.** It answers one question for an engineer who runs AI training and serving on Kubernetes: which data tools run on your cluster, which ones are services you only connect to, and what each one asks of Kubernetes. AWS is the worked cloud example, because the other pages of this blog use EKS, which is Amazon's managed Kubernetes service. Google Cloud is the second example, because Pub/Sub and BigQuery are Google services. GKE is Google's managed Kubernetes service. A managed service is a service that a cloud or a vendor runs for you. You reach it with an identity, which is the name that your pod shows to the service, and a network path, which is the route that the traffic takes. You do not size its machines.
**The names on this page, in plain words.** Spark and Flink are engines: programs that process large data sets on many machines at once. Spark works on stored data in batches, and Flink works on a stream of events. Beam is a programming model for pipelines, and it runs on an engine such as Spark or Flink. Airflow and Dagster are orchestrators: programs that decide when each step of a pipeline runs. Kafka stores a stream of messages, and Pub/Sub is Google Cloud's managed service for the same job. Ray is a framework that spreads Python work over many machines. Parquet is a file format that stores a table column by column. A snapshot is the state of a table at one moment. Iceberg and Delta Lake are table formats: they keep a list of which files make up a table, so that the table can change safely. A data lake is a set of files in object storage, which is a store for files that you reach over the network, such as Amazon S3. A data warehouse is a database built to answer questions over large tables, and BigQuery and Snowflake are warehouses. IAM is the access control service of AWS and of Google Cloud, and it decides who may do what. A zone is one separate data center area inside a cloud region. An operator is software that watches Kubernetes objects of its own kind, called custom resources, and acts on them. [Appendix G](#d-data-platforms-appendix-g-words) explains custom resources and the control loop. A service account is the identity that a pod uses when it talks to the Kubernetes API. A namespace is a named part of a cluster that groups objects, such as pods. A Deployment is an object that keeps a set number of identical pods running. A pod is `Pending` when the Kubernetes system has accepted it, but its containers have not started yet, for example before it is bound to a node ([Pod v1 API](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/)). A definition without a link is this page's own description in plain words, not a quote from a source.

Each section opens with one Kubernetes question and keeps only what changes something you do on Kubernetes. Every number has a source linked next to it. *Derived* means arithmetic on sourced numbers, shown in the text. *Illustrative* means picked to teach, not measured. *This page's reading* means an inference that no source states. Where no source was found, the page says so. Appendix E gives the date on which each price was read.
Three other pages already teach parts of this topic, and this page links to them instead of repeating them. [Design 03](#design-training-inference) teaches Spark data preparation and Iceberg snapshots. The deep dive [Failure modes: storage, memory and controller loops](#d-challenges-1) teaches the cross-zone cost of Kafka. The deep dive [Stateful workloads: databases on Kubernetes](#d-stateful) teaches how to spread brokers over zones. The appendix holds the details that do not change what you do on Kubernetes: the five questions for each tool, file layouts, prices, managed counterparts, words, and the calls for reading data. Words are in [Appendix G](#d-data-platforms-appendix-g-words).

### What runs on your cluster, and what you connect to

**The Kubernetes question.** A data team hands over a list of 13 names and says "put them on Kubernetes". The names are Spark, Flink, Beam, Airflow, Dagster, Kafka, Pub/Sub, data lake and warehouse architectures, Parquet, Iceberg, Delta Lake, BigQuery and Snowflake. Which of them are pods that you must size, place and patch, and which are services that you only reach from a pod?

**Scenario.** This scenario is illustrative. A platform engineer starts to write manifests for every name on the list. By the afternoon three things are clear. BigQuery, Pub/Sub and Snowflake have nothing to install. Kafka and Spark have a version that runs as pods and a version that a cloud runs for you. Parquet, Iceberg and Delta Lake are files in a bucket that you connect to, and the readers, writers and clean-up jobs for those files run in your pods.

![What runs on your cluster, and what you connect to](diagrams/sections/d18-1.svg)

**What the picture shows.** One box for your Kubernetes cluster and one area for everything outside it. The colours follow one rule across every picture on this page.

- **Blue: runs on your cluster.** These are pods that you size, place and patch. The picture shows Spark, Flink, Airflow, Dagster, Kafka (with Strimzi, an operator for Kafka) and Ray, next to your training pods. KubeRay is the operator that runs Ray on Kubernetes ([Ray on Kubernetes](https://docs.ray.io/en/latest/cluster/kubernetes/index.html)). [How a Pod finds its scheduler](#d02-1) in Deep dive 3 explains KubeRay.
- **Blue: Beam.** Beam has no engine of its own, so on a cluster it runs through the Flink runner or the Spark runner.
- **Orange: a managed service.** You connect to it. BigQuery, Snowflake and Pub/Sub have nothing to install. The other orange boxes are managed versions of tools that also run on a cluster.
- **Green: files in object storage.** Parquet, Iceberg and Delta Lake sit in object storage, such as Amazon S3 or Google Cloud Storage. You connect to the bucket. The readers, writers and clean-up jobs run in your pods, so these formats are both.
- **Lake and warehouse: both, and managed only.** A data lake is files in object storage, with engines in your pods that read them. So a lake is both. A data warehouse such as BigQuery or Snowflake is managed only, and you connect to it from a pod. [Lakes, warehouses and table formats](#d18-5) has the details.
- **Dashed lines: both versions exist.** Each dashed line joins a tool in your cluster to its managed version. Section [Reaching managed services from pods](#d18-7) covers the connection.
- **The names in the orange boxes.** Amazon EMR on EKS runs Spark for you on your own EKS cluster. Amazon Managed Service for Apache Flink runs Flink for you. Dataflow is Google's managed runner for Beam. MSK is Amazon's managed Kafka, and Google Cloud Managed Service for Apache Kafka is Google's. MWAA is Amazon's managed Airflow, and Google Managed Airflow is Google's. Dagster+ Hybrid is a managed control plane for Dagster, with your code in your own containers. Anyscale is the managed Ray platform.

**Going deeper, by topic.**

- **Both: a real choice.** Kafka is the clear case. You can run brokers as pods with Strimzi, or you can connect to MSK. MSK provides the control-plane operations, such as creating and deleting clusters, and it replaces a broker that fails ([Amazon MSK](https://docs.aws.amazon.com/msk/latest/developerguide/what-is-msk.html)). You still choose how many brokers MSK creates in each zone. This page's reading: with Strimzi, the patching, the storage and the zone spreading are your work.
- **Both: a managed control plane with your pods.** Amazon EMR on EKS is managed Spark that runs on your own EKS cluster. EMR calls a Kubernetes namespace that you register a "virtual cluster" ([EMR on EKS](https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/how-it-works.html)). Dagster+ Hybrid is the other shape. Dagster+ runs the control plane, and your code runs in containers that an agent you operate starts. The agent can run on Kubernetes ([Dagster+ Hybrid](https://docs.dagster.io/deployment/dagster-plus/hybrid)).
- **The formats run no server of their own.** Parquet is a file format, and Iceberg and Delta Lake are table formats. All three are files in a bucket. [Appendix C](#d-data-platforms-appendix-c-iceberg-and-delta-lake-in-detail) has what Iceberg and Delta Lake ask of the file system.
- **Operators differ in one default.** An admission webhook is a server that the Kubernetes API calls to check or change an object before it is stored. The Flink operator installs one by default ([Flink operator architecture](https://nightlies.apache.org/flink/flink-kubernetes-operator-docs-stable/docs/concepts/architecture/)). The Spark operator of Kubeflow, an open-source project for machine learning on Kubernetes, leaves its webhook off by default ([Spark operator getting started](https://spark.kubeflow.org/en/latest/getting-started/)).
- **Operators: what needs the Spark webhook.** Some `SparkApplication` fields need the Spark operator's webhook, and tolerations are one of them. A toleration is a rule in a pod that lets it run on a node with a matching taint, which is a mark that keeps other pods away. Check the webhook before you rely on such fields ([Writing a SparkApplication](https://spark.kubeflow.org/en/latest/user-guide/writing-sparkapplication.html)).
- **Snowflake's container service is not your cluster.** Snowpark Container Services is "a managed container orchestration platform within Snowflake". Your containers run in a compute pool, which is a set of virtual machine nodes that Snowflake runs ([Snowpark Container Services](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/overview)). Use it only when you want Snowflake to run the container. This page's reading: it does not bring data into your cluster.
- **Five questions for any tool.** What do I request? Where do the pods land? What do I configure? How do I check it? What does it cost, and what has to move? These five questions decide what belongs on this page. [Appendix A](#d-data-platforms-appendix-a-tool-by-tool-what-each-asks-of-kubernetes) answers them for each tool and opens each tool with its class: on the cluster, a managed service, or both.
- **Where the other pages stop.** [Design 03](#design-training-inference) teaches Spark data preparation in [Training: data and checkpoints](#ds03-4). [Cross-AZ transfer costs](#d05-6) shows why Kafka replication costs money. [Spreading across AZs and nodes](#d08-4) spreads brokers over zones. This page adds what those three leave out.

### Spark, Flink and Beam: the engines that make training tables

**The Kubernetes question.** A nightly job turns raw logs into training tables, and a second job keeps a feature table fresh from a stream. Which pods do these engines create, what do you request for them, where do they land, and what do they leave behind?

**Scenario.** This scenario is illustrative. A Spark job asks for 200 executors in a namespace that also holds a model-serving Deployment. The executors fill the node pool. The serving pods go `Pending`. A finished driver pod stays behind for days.

![What Spark, Flink and Beam create in your cluster](diagrams/sections/d18-2.svg)

**What the picture shows.** Three engines, drawn as the pods they create. A driver is the pod that runs the Spark program and asks for the other pods. An executor is a pod that does the work. Shuffle data is the data that executors exchange between steps. The pod counts in the picture are illustrative.

- **Spark: one driver, many executors.** `spark-submit`, Spark's own command to start a job, creates the driver pod. The driver asks the Kubernetes API for executor pods. When the job ends, the executors are deleted. The driver pod stays in the `Completed` state, where it uses no CPU or memory, until it is cleaned up ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: what you set.** A service account, the executor CPU request, a scratch volume for shuffle data, and where the pods may run.
- **Spark: where pods may run.** A node selector and tolerations say which nodes the pods may use, and [Phase 1: filtering](#d01-1) in Deep dive 1 teaches both. Spark's own page does not name tolerations. The operator guide shows them as `SparkApplication` fields ([Writing a SparkApplication](https://spark.kubeflow.org/en/latest/user-guide/writing-sparkapplication.html)). Spark's pod template files carry pod settings that Spark's options do not cover ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)). No page read says whether tolerations in a pod template work.
- **Spark: an optional batch scheduler.** Volcano is a batch scheduler for Kubernetes. A PodGroup is its object for pods that must start together, and [Gang scheduling](#d02-2) explains the idea.
- **Flink: a JobManager and TaskManagers.** The JobManager is the pod that coordinates the job. A TaskManager is a pod that runs parts of it. Flink can start and release TaskManagers because it talks to the Kubernetes API ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/)).
- **Flink: state leaves the pod.** A checkpoint is a saved copy of the job's state. The checkpoint folder sits in storage that every pod can reach ([Flink checkpointing](https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/)).
- **Flink: high availability.** High availability (HA) is Flink's way to restart a job from its latest successful checkpoint after the cluster restarts. Its metadata goes to an HA folder in the same kind of storage. Kubernetes ConfigMaps, which are small objects that hold settings as text, hold only pointers ([Flink Kubernetes HA](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/ha/kubernetes_ha/)).
- **Beam: a program that needs a runner.** A runner is the engine that executes a Beam program. This page covers two runners that run on Kubernetes: the Flink runner and the Spark runner. The Dataflow runner is a managed service, which means that Google allocates the worker machines, so it leaves your cluster ([Beam overview](https://beam.apache.org/get-started/beam-overview/), [Dataflow](https://cloud.google.com/dataflow/docs/overview)).

**Going deeper, by topic.**

- **Spark: the Kubeflow Spark operator.** The operator runs `spark-submit` for you whenever a valid `SparkApplication` manifest is created or updated ([Spark operator overview](https://www.kubeflow.org/docs/components/spark-operator/overview/)). [Design 03](#design-training-inference) already uses it.
- **Spark: the Apache Spark Kubernetes Operator.** It is a separate Spark subproject with the objects `SparkApp` and `SparkCluster`. Spark's page calls the operator and `spark-submit` complementary ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html), [Spark K8s operator](https://raw.githubusercontent.com/apache/spark-kubernetes-operator/main/README.md)).
- **Spark: the driver needs rights.** The driver's service account must be allowed to create pods, services and config maps. The `default` service account may lack that right, so Spark can use a named one ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)). Kubernetes DNS must work in the cluster.
- **Spark: two Kubernetes versions on two pages.** Spark's own page lists Kubernetes 1.34 or later. The Kubeflow operator's getting-started page lists 1.16 or later ([Spark](https://spark.apache.org/docs/latest/running-on-kubernetes.html), [operator](https://spark.kubeflow.org/en/latest/getting-started/)). Both are quoted as read on 2026-11-22. No source reconciles them.
- **Spark: the CPU request.** `spark.kubernetes.executor.request.cores` sets the CPU request of an executor pod ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: cores and tasks.** `spark.executor.cores` is a different setting. It sets how many tasks run at once in one executor. A task is one piece of work for one core ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: memory outside the JVM.** A JVM is the Java runtime that Spark runs on. The memory overhead factor reserves memory for things outside the JVM ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: the default overhead factor.** The default is 0.10 for JVM jobs and 0.40 for non-JVM jobs ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: cap a team with a quota.** A ResourceQuota limits what one namespace can ask for, and Spark's page says to combine namespaces with a quota to share a cluster. For GPUs, only keys that start with `requests.` are allowed, for example `requests.nvidia.com/gpu: 4` ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html), [Kubernetes resource quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/)).
- **Spark: a node selector.** With `spark-submit`, a node selector property puts the driver and executors on a set of nodes. Spark's page says that more advanced hints, such as affinities, are planned for a future release ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: tolerations and affinity in a SparkApplication.** A `SparkApplication` takes tolerations in `.spec.driver.tolerations` and `.spec.executor.tolerations`. It takes affinity in `.spec.driver.affinity` and `.spec.executor.affinity`. Affinity is a rule that attracts a pod to chosen nodes or pods. Both need the operator's mutating admission webhook ([Writing a SparkApplication](https://spark.kubeflow.org/en/latest/user-guide/writing-sparkapplication.html)).
- **Spark: a custom scheduler.** `spark.kubernetes.scheduler.name` picks a custom scheduler such as Volcano ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)). [Gang scheduling](#d02-2) and [Volcano vs. Kueue](#d02-6) explain those words.
- **Spark: a PodGroup with Volcano.** With Volcano, a PodGroup template sets how many pods must start together. Spark supports only a job-level PodGroup ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: the webhook port.** Tolerations need the operator's webhook. On a GKE cluster with the Private cluster setting, or on an enterprise AWS EKS cluster, the guide says to change the webhook port to 443 ([getting started](https://spark.kubeflow.org/en/latest/getting-started/)).
- **Spark: a SparkApplication with the fields that matter.** The names, the folder paths, the node label, the toleration key and the CPU and memory sizes are illustrative. The image is the one in the guide's example.

```yaml
apiVersion: sparkoperator.k8s.io/v1beta2
kind: SparkApplication
metadata:
  name: prep-training-tables          # illustrative
  namespace: data-prep                # illustrative: must be in spark.jobNamespaces
spec:
  type: Python
  mode: cluster
  image: docker.io/apache/spark:4.2.1 # illustrative: use an image that includes PySpark
  sparkVersion: "4.2.1"
  mainApplicationFile: local:///opt/app/prep.py   # illustrative
  nodeSelector:
    workload: batch                   # illustrative label
  driver:
    cores: 1                          # illustrative
    memory: 2g                        # illustrative
    serviceAccount: spark             # needs the right to create pods
  executor:
    cores: 4                          # illustrative
    instances: 20                     # illustrative
    memory: 16g                       # illustrative
    serviceAccount: spark
    tolerations:
    - key: batch-only                 # illustrative
      operator: Exists
      effect: NoSchedule
```

- **Spark: GPU executors for data preparation.** Spark turns `spark.executor.resource.gpu.amount` into a Kubernetes resource when you set the vendor, for example `nvidia.com` ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)). No source read says whether GPU data preparation is worth it.
- **Spark: the GPU discovery script.** Kubernetes does not tell Spark which GPU a container got, so Spark needs a discovery script ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html), [RAPIDS on Kubernetes](https://docs.nvidia.com/cudf-spark/latest/getting-started/kubernetes.html)).
- **Spark: GPUs in a SparkApplication.** In the operator, the field is `spec.executor.gpu` with a `name` and a `quantity` ([Writing a SparkApplication](https://spark.kubeflow.org/en/latest/user-guide/writing-sparkapplication.html)).
- **Spark: GPU quota.** This page's reading: those GPUs come out of the same quota as training GPUs. [Quotas](#ds17-7) in Design 17 covers quotas.
- **Spark: shuffle data on a volume.** Spark can keep shuffle data on volumes. A PersistentVolumeClaim is a request for storage that Kubernetes meets with a volume, and [StorageClass, PV and PVC in one analogy](#d14-1) teaches it. An executor can get its own claim if you set the claim name to `OnDemand`. The driver can own and reuse these claims ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: scratch space in RAM.** `spark.kubernetes.local.dirs.tmpfs=true` puts scratch space in RAM, and it then counts as pod memory ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)). Design 03 covers the `spark-local-dir-` name rule, and [Performance, networking and backup](#d08-3) covers local NVMe.
- **Spark: dynamic allocation.** Dynamic allocation lets an application give executors back and ask for more when the load returns. It needs shuffle files to survive when an executor is removed. Spark's scheduling page lists four ways to do that ([Spark job scheduling](https://spark.apache.org/docs/latest/job-scheduling.html)).
- **Spark: the external shuffle service.** It is one of the four ways. It is a long-running process on each node that serves shuffle files after an executor is gone. Spark's scheduling page says that Spark on Kubernetes does not yet support it ([Spark job scheduling](https://spark.apache.org/docs/latest/job-scheduling.html)).
- **Spark: the other three ways.** They are shuffle tracking, decommissioning, and a custom `ShuffleDataIO` plugin that supports reliable storage ([Spark job scheduling](https://spark.apache.org/docs/latest/job-scheduling.html)). Design 03 covers shuffle tracking.
- **Spark: decommissioning.** Spark needs to decommission an executor gracefully, which means that it preserves the executor's state before it removes the executor. This needs the two settings `spark.decommission.enabled` and `spark.storage.decommission.shuffleBlocks.enabled` ([Spark job scheduling](https://spark.apache.org/docs/latest/job-scheduling.html)).
- **Spark: look at a running job.** `kubectl describe pod` on the driver shows scheduling events. `kubectl logs` shows the driver log. `kubectl port-forward <driver pod> 4040:4040` opens the Spark UI on your machine ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: stop a job.** `spark-submit --kill namespace:driver-pod-name` stops a job ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: a driver in a pod of its own.** This is called client mode. Set `spark.kubernetes.driver.pod.name` to that pod's name. Spark then ties the executor pods to the driver pod, so deleting the driver pod deletes them too ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Flink: it runs for months.** The operator's overview says a streaming application "lives for months" ([Flink operator overview](https://nightlies.apache.org/flink/flink-kubernetes-operator-docs-stable/docs/concepts/overview/)). So upgrades and restarts must keep the saved state. This page's reading: do not put its pods on Spot capacity without a plan for that state. Spot capacity is spare cloud machines that the cloud sells at a lower price and can take back. No source says so.
- **Flink: native mode.** Native mode means that Flink talks to the Kubernetes API itself to start and stop its pods. So its service account needs the right to create and delete pods. For high availability the account also needs the right to create, edit and delete ConfigMaps ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/), [Flink Kubernetes HA](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/ha/kubernetes_ha/)).
- **Flink: state must leave the pod.** By default a checkpoint goes to the JobManager's memory. The docs recommend a durable file system for production. The folder must be reachable by all TaskManagers and JobManagers ([Flink checkpointing](https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/)).
- **Flink: a volume for state.** A PersistentVolumeClaim also works. A volume has an access mode. `ReadWriteMany` lets many nodes mount the volume at once, and `ReadWriteOnce` lets one node mount it. The docs recommend `ReadWriteMany` for high availability, and say that `ReadWriteOnce` may cause mount failures ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/)).
- **Flink: a FlinkDeployment with a checkpoint folder.** A `FlinkDeployment` is the object that the Flink operator adds. In this manifest it runs one application. The manifest is the minimal example from the operator's docs, plus one checkpoint key. The bucket path is illustrative. The manifest leaves high availability out, because the operator pages read do not say whether the operator sets the cluster id.

```yaml
apiVersion: flink.apache.org/v1beta1
kind: FlinkDeployment
metadata:
  namespace: default
  name: feature-stream                 # illustrative
spec:
  image: flink:1.20
  flinkVersion: v1_20
  flinkConfiguration:
    taskmanager.numberOfTaskSlots: "2"
    execution.checkpointing.dir: s3://example-bucket/flink/checkpoints   # illustrative
  serviceAccount: flink
  jobManager:
    resources:
      requests:
        memory: "2048Mi"
        cpu: "1"
  taskManager:
    resources:
      requests:
        memory: "2048Mi"
        cpu: "1"
  job:
    jarURI: local:///opt/flink/examples/streaming/StateMachineExample.jar
    parallelism: 2
    upgradeMode: stateless
    state: running
```

- **Flink: high availability.** Three keys are required: `high-availability.type: kubernetes`, `high-availability.storageDir` and `kubernetes.cluster-id`. The metadata goes to the storage folder, and Kubernetes holds only a pointer. If you delete the deployment, the HA ConfigMaps stay, and the jobs restart from the latest successful checkpoint ([Flink Kubernetes HA](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/ha/kubernetes_ha/)).
- **Flink: application mode.** Application mode packs the job into the image, and Flink recommends it for production because it isolates applications ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/)).
- **Flink: pod templates.** In a pod template, Flink's page says the main container should be named `flink-main-container`, and Flink overwrites its name, CPU and memory ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/)).
- **Flink: logs of released TaskManagers.** Flink releases idle TaskManagers, so raise `resourcemanager.taskmanager-timeout` when you need their logs ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/)).
- **Beam: a model, not an engine.** A Beam program runs on a runner such as Flink, Spark or Dataflow. Beam lists more runners, but this page covers only the Flink runner and the Spark runner ([Beam overview](https://beam.apache.org/get-started/beam-overview/)).
- **Beam: where user code runs.** Your Beam functions run in an environment that Beam calls the SDK harness. The setting `environment_type` chooses where it runs ([Beam SDK harness config](https://beam.apache.org/documentation/runtime/sdk-harness-config/)).
- **Beam: DOCKER.** `DOCKER` is the default. It starts a container on each worker node, so it needs Docker there ([Beam SDK harness config](https://beam.apache.org/documentation/runtime/sdk-harness-config/)).
- **Beam: on Spark on Kubernetes.** Beam's Spark page shows Beam on Spark on Kubernetes with `PROCESS`, which uses processes that the runner starts on each worker node ([Beam Spark runner](https://beam.apache.org/documentation/runners/spark/), [Beam SDK harness config](https://beam.apache.org/documentation/runtime/sdk-harness-config/)). [Appendix A](#d-data-platforms-appendix-a-tool-by-tool-what-each-asks-of-kubernetes) lists the other environment types.
- **Beam: the init container.** An init container is a container that runs and ends before the main container of a pod starts. The executor pod template in Beam's Spark example has an init container that downloads the SDK harness ([Beam Spark runner](https://beam.apache.org/documentation/runners/spark/)).
- **Beam: Python on Flink.** Python on Flink needs the portable runner, which is the newer Beam runner for Java, Python and Go. It also needs a job service, which is the program where you submit the pipeline. The job service creates a Flink job from it ([Beam Flink runner](https://beam.apache.org/documentation/runners/flink/)).
- **The managed versions.** Amazon EMR on EKS runs Spark on your EKS cluster ([EMR on EKS](https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/how-it-works.html)). Amazon Managed Service for Apache Flink runs Flink for you ([AWS](https://docs.aws.amazon.com/managed-flink/latest/java/what-is.html)). Dataflow allocates worker VMs and deletes them when the job ends ([Dataflow](https://cloud.google.com/dataflow/docs/overview)). [Appendix F](#d-data-platforms-appendix-f-managed-counterparts-in-one-list) lists them all.

### Airflow and Dagster: what starts the work

**The Kubernetes question.** A nightly pipeline starts 40 tasks, and some of them need an image of their own. Which component creates the pods, in which namespace, from which image, and where do the logs go after the pod is gone?

**Scenario.** This scenario is illustrative. A pipeline runs well on a laptop. In the cluster, the scheduler cannot create pods, because its service account has no right to do so. The tasks that do start lose their logs when their pods end.

![Control plane, then one pod for each task or run](diagrams/sections/d18-3.svg)

**What the picture shows.** The left namespace holds the small, always-on parts. The right namespace holds the pods that do the work. The count of work pods is illustrative.

- **Airflow's always-on parts.** The scheduler starts tasks. The DAG processor reads the pipeline files. The API server serves the web interface. A metadata database holds the state ([Airflow architecture](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/overview.html)). A DAG (directed acyclic graph) is one pipeline, written as steps with an order between them.
- **Dagster's always-on parts.** The Dagster Helm chart, which is a package of Kubernetes manifests that you install with one command, installs a daemon (a background program), a web server and a PostgreSQL container ([Dagster on Kubernetes](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/deploying-to-kubernetes)).
- **Dagster's code pod.** Your own code runs in a pod that serves it over gRPC, which is a way for programs to call each other over the network ([Dagster on Kubernetes](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/deploying-to-kubernetes)).
- **The work pods.** With the Kubernetes executor, Airflow runs each task in its own pod ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)). Dagster starts a Kubernetes Job for each run. A Kubernetes Job is an object that runs a pod until its work is finished ([Dagster on Kubernetes](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/deploying-to-kubernetes)).
- **The managed versions.** MWAA, Google Managed Airflow and the Dagster+ control plane run outside your cluster.

**Going deeper, by topic.**

- **Airflow: the always-on parts.** Airflow needs a scheduler, a DAG processor, an API server and a metadata database, usually PostgreSQL or MySQL ([Airflow architecture](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/overview.html)).
- **Airflow: the tasks are the load.** With the Kubernetes executor, each task runs in its own pod ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)). This page's reading: the always-on parts are small, and the task pods carry the load.
- **Airflow: the database.** The Kubernetes executor needs a database other than SQLite ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: one pod per task.** With the Kubernetes executor, each task gets its own image and resources. A running task also survives a deploy ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: Celery workers or task pods.** A Celery worker runs many tasks in one long-lived pod. The Kubernetes executor, in contrast, does not need additional components such as Redis, an in-memory data store ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)). This page's reading: with Celery, such a component holds the queue of tasks. [Appendix G](#d-data-platforms-appendix-g-words) explains Celery and Redis.
- **Airflow: both executors together.** The Airflow executor page says that the official Helm chart can scale Celery workers down to zero, and that both executors can run together on one cluster ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)). The Helm chart page lists autoscaling for the Celery executor that KEDA provides ([Airflow Helm chart](https://airflow.apache.org/docs/helm-chart/stable/index.html)). KEDA is a Kubernetes add-on that scales workloads on outside signals.
- **Airflow: what the Helm chart asks for.** As read on 2026-11-22, the chart needs Kubernetes 1.30.13 or later and Helm 3.19.0 or later ([Airflow Helm chart](https://airflow.apache.org/docs/helm-chart/stable/index.html)). Check these numbers before you install, because the page tracks the current release.
- **Airflow: the optional PV provisioner.** A PV provisioner is the part of a cluster that creates a storage volume when a claim asks for one. The chart's page lists it as optional ([Airflow Helm chart](https://airflow.apache.org/docs/helm-chart/stable/index.html)).
- **Airflow: the pod template.** A `pod_template_file` shapes the task pod ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: two strict rules.** The first container must be named `base`, and its image must not be blank. The pod needs a `metadata.name`, which Airflow replaces at launch ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: the smallest pod template.** The manifest shows the smallest shape that meets the rules. The service account name and the requests are illustrative ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: placeholder-name             # required; Airflow sets a unique name at launch
spec:
  containers:
    - name: base                     # required: first container, named base
      image: dummy_image             # required: not blank; Airflow may override it
      resources:
        requests:                    # illustrative
          cpu: "500m"
          memory: 1Gi
  restartPolicy: Never
  serviceAccountName: airflow-worker # illustrative
```

- **Airflow: start from the full examples.** The doc's full examples also pass the database connection and a logs mount ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: KubernetesPodOperator.** This pod operator runs any image as a task, with or without the Kubernetes executor ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: pod operator settings.** The pod operator accepts affinity, tolerations and a pod template. The provider adds variants for GKE and EKS ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: print the pod.** `dry_run()` prints the pod manifest that the pod operator would create ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: pod events.** While a pod is `Pending`, the pod operator reads its Kubernetes events and writes them to the task log ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: API load.** On clusters with thousands of short-lived pods in one namespace, reading these events adds load on the API server ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: a separate namespace.** The page suggests a separate namespace for unrelated high-churn pods, and it names Spark executors as an example ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: DAG files.** The pods get DAG files by image, by `git-sync`, or from a persistent volume. `git-sync` is a container that runs a `git pull` of the DAG repository before the worker container starts ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: logs.** Logs need a persistent volume or remote logging. Otherwise "logs will be lost after the worker pods shut down" ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Dagster: the run launcher.** The `K8sRunLauncher` creates a Kubernetes Job for each run ([Dagster Kubernetes configuration](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/customizing-your-deployment)).
- **Dagster: the executors.** The default executor, `multi_or_in_process_executor`, runs each job in one pod and each step in its own process. The `k8s_job_executor` runs each step in its own pod. For steps that compute for a short time, the `in_process_executor` avoids the cost of starting a pod or a process per step ([Dagster Kubernetes configuration](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/customizing-your-deployment)).
- **Dagster: pod settings for every run.** In the Helm values, `k8sRunLauncher.runK8sConfig` holds raw Kubernetes fields for the container, the pod spec and the Job spec. The values in the manifest follow the docs' example, and all of them are illustrative ([Dagster Kubernetes configuration](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/customizing-your-deployment)).

```yaml
runLauncher:
  type: K8sRunLauncher
  config:
    k8sRunLauncher:
      runK8sConfig:
        containerConfig:            # raw config for the pod's main container
          resources:
            limits:
              cpu: 100m             # illustrative
              memory: 128Mi         # illustrative
        podSpecConfig:              # raw config for the pod spec
          nodeSelector:
            disktype: ssd           # illustrative label
        jobSpecConfig:              # raw config for the Kubernetes Job spec
          ttlSecondsAfterFinished: 7200   # illustrative
```

- **Dagster: pod settings for one job.** The tag `dagster-k8s/config` sets the same kinds of fields for one job. If the job uses `k8s_job_executor`, that tag is not passed to the step pods. Use the executor's `step_k8s_config` field for them ([Dagster Kubernetes configuration](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/customizing-your-deployment)).
- **Managed Airflow.** Amazon MWAA is a managed service for Airflow, with automatic scaling and no infrastructure for you to manage ([MWAA](https://docs.aws.amazon.com/mwaa/latest/userguide/what-is-mwaa.html)). Google's page says Cloud Composer is being renamed Managed Service for Apache Airflow ([Google Managed Airflow](https://cloud.google.com/composer/docs/composer-3/composer-overview)).
- **Dagster+ Hybrid.** Dagster+ runs the control plane, and your code runs in your containers. Dagster recommends its Kubernetes agent in most cases ([Dagster+ Hybrid](https://docs.dagster.io/deployment/dagster-plus/hybrid)).
- **Keep the stampede out.** This page's reading: the orchestrator pods are small, and the pods they launch carry the cost. So send those pods through the same queue and priority rules as other batch work.
- **Kueue and these tools.** Kueue is a queue in front of the scheduler that decides when a job may start. [Volcano vs. Kueue](#d02-6) compares it with Volcano. The Airflow provider page documents one operator that installs Kueue and one that starts a Job through a named Kueue queue ([Airflow Kubernetes operators](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)). No page read covers Dagster with Kueue, so this page does not claim it.
- **A whole pipeline starting at once.** [Scheduler thrashing](#d05-9) and [Thundering herd](#d05-8) show what happens when a whole pipeline starts at once.
- **Another orchestrator.** [Orchestrating the pipeline](#ds28-4) in Design 28 shows Argo Workflows.

### Kafka and Pub/Sub: events on the way to the lake

**The Kubernetes question.** Click events and rollout logs arrive all day and feed the training tables. Do the brokers run in your cluster, or do you connect to a managed service, and how many consumer pods can you usefully run?

**The words.** Kafka is a system that stores a stream of messages in topics. A topic is split into partitions. A broker is a server that stores some of the partitions. A replica is one copy of a partition. Kafka keeps several replicas, and one of them is the leader. A consumer is a program that reads messages. A consumer group is a set of consumers that share one topic. Rack is Kafka's word for a zone label.

**Scenario.** This scenario is illustrative. A team runs Kafka brokers in three zones and scales a consumer Deployment to 20 pods for a topic with 6 partitions. Fourteen pods sit idle. A second team reads a Pub/Sub subscription and scales its pods with the backlog, because Pub/Sub does not tie one consumer to one partition.

![Brokers in the cluster, a managed service outside](diagrams/sections/d18-4.svg)

**What the picture shows.** The numbers in the picture are illustrative.

- **Three zones, one broker and one consumer in each.** Each broker carries its zone as `broker.rack`, and each consumer carries its zone as `client.rack`. A consumer reads from the replica in its own zone. [Cross-AZ transfer costs](#d05-6) explains why that saves money.
- **A partition cap.** A consumer group can use at most one consumer per partition. KEDA, a Kubernetes add-on that scales workloads on outside signals, stops at the partition count by default ([KEDA Kafka scaler](https://keda.sh/docs/latest/scalers/apache-kafka/)). In the scenario, a topic with 6 partitions keeps only 6 of the 20 consumer pods busy, so 14 sit idle (illustrative).
- **Pub/Sub at the bottom.** Pub/Sub is Google Cloud's managed messaging service. A pull subscription needs subscriber pods. An export subscription writes straight to BigQuery or Cloud Storage and needs no pod ([Pub/Sub subscriber overview](https://cloud.google.com/pubsub/docs/subscriber)).
- **MSK on the left.** Amazon MSK puts the number of brokers that you choose in each zone ([Amazon MSK](https://docs.aws.amazon.com/msk/latest/developerguide/what-is-msk.html)).
- **MSK: access.** MSK can use IAM for access ([MSK IAM access control](https://docs.aws.amazon.com/msk/latest/developerguide/iam-access-control.html)).
- **MSK: other VPCs.** A VPC is your private network in AWS. A client in another VPC connects through multi-VPC private connectivity ([MSK multi-VPC](https://docs.aws.amazon.com/msk/latest/developerguide/aws-access-mult-vpc.html)).
- **MSK: PrivateLink.** AWS PrivateLink is the technology behind multi-VPC private connectivity. It is a private connection between VPCs that does not use the public internet ([MSK multi-VPC](https://docs.aws.amazon.com/msk/latest/developerguide/aws-access-mult-vpc.html)).

**Going deeper, by topic.**

- **Kafka on the cluster: Strimzi.** Strimzi is an operator. In the Strimzi docs for version 1.3.1, as read on 2026-11-22, it adds the objects `Kafka`, `KafkaNodePool`, `KafkaTopic` and `KafkaUser` ([Strimzi overview](https://strimzi.io/docs/operators/latest/overview.html)).
- **Kafka on the cluster: node pools and roles.** A node can be a broker, a controller, or both. A controller keeps the cluster's state and metadata. For production, Strimzi recommends a separate pool of three or more controllers. Different pools can have different storage ([Strimzi overview](https://strimzi.io/docs/operators/latest/overview.html)).
- **Kafka on the cluster: a node pool manifest.** The sizes are the docs' example values, not advice. The pool name and the replica count are illustrative ([Strimzi deploying](https://strimzi.io/docs/operators/latest/deploying.html)).

```yaml
apiVersion: kafka.strimzi.io/v1
kind: KafkaNodePool
metadata:
  name: brokers                  # illustrative
  labels:
    strimzi.io/cluster: my-cluster
spec:
  replicas: 3                    # illustrative
  roles:
    - broker
  storage:
    type: jbod
    volumes:
      - id: 0
        type: persistent-claim
        size: 100Gi              # the docs' example value
  resources:
    requests:
      memory: 64Gi               # the docs' example value
      cpu: "8"
    limits:
      memory: 64Gi
      cpu: "12"
```

- **Kafka on the cluster: block storage.** Block storage is a disk that attaches to one node, such as an Amazon EBS volume. Strimzi has been tested with block storage and strongly recommends it. NFS, a shared network file system, is not guaranteed to work for primary broker storage ([Strimzi deploying](https://strimzi.io/docs/operators/latest/deploying.html)). [EBS is AZ-locked: three strategies](#d08-7) covers the zone lock of block storage.
- **Kafka on the cluster: JBOD.** Strimzi always recommends JBOD, which means "just a bunch of disks", even for one volume. The storage type cannot be changed after the cluster is deployed ([Strimzi deploying](https://strimzi.io/docs/operators/latest/deploying.html)).
- **Kafka on the cluster: volume claims stay.** The setting `deleteClaim` defaults to false, so the volume claims stay when a node goes away ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: racks and zones.** The `rack` property with `topologyKey: topology.kubernetes.io/zone` makes Strimzi set `broker.rack`, and Kafka then spreads partition replicas over as many racks as it can ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: racks can change.** The rack of a broker can change when its pod is deleted or restarted, so replicas that ran in different racks might later share one ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: keeping the spread.** Strimzi advises Cruise Control, a program that rebalances partitions over brokers, with the `KafkaRebalance` resource and the `RackAwareGoal` to keep replicas spread over racks ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: reading from the nearest replica.** By default, a consumer reads from the leader. A consumer fetches from a replica in its own zone when the brokers use `RackAwareReplicaSelector` and the consumer sets `client.rack` ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: when no replica matches.** If no replica has the same rack ID, the consumer falls back to the leader. Strimzi says nearest-replica reads can cut cost in public clouds and can raise latency ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: the broker setting.** The manifest shows the broker side of nearest-replica reads. It is a fragment of a larger `Kafka` resource. The `# ...` lines stand for fields that the docs example leaves out, such as the listeners ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)). The Strimzi docs say that at least one listener is required ([Strimzi deploying](https://strimzi.io/docs/operators/latest/deploying.html)).

```yaml
apiVersion: kafka.strimzi.io/v1
kind: Kafka
metadata:
  name: my-cluster
spec:
  kafka:
    # ...
    rack:
      type: topology-label
      topologyKey: topology.kubernetes.io/zone
    config:
      # ...
      replica.selector.class: org.apache.kafka.common.replica.RackAwareReplicaSelector
    # ...
```

- **Kafka on the cluster: safe drains.** Draining a node means moving its pods away before maintenance. The Strimzi Drain Cleaner lets the operator, not Kubernetes, move Kafka pods off a node that is going away. No partition becomes under-replicated during the drain, which means that no partition has fewer up-to-date copies than it should ([Strimzi overview](https://strimzi.io/docs/operators/latest/overview.html)).
- **Kafka on the cluster: PodDisruptionBudgets.** A PodDisruptionBudget is a Kubernetes object that limits how many pods of one workload may be down at once. [Workloads, node groups and scheduling](#d08-1) covers it.
- **Managed Kafka: MSK brokers.** You choose how many broker nodes MSK creates in each zone, with a minimum of one per zone. Each zone has its own subnet, which is a part of your VPC that sits in one zone ([Amazon MSK](https://docs.aws.amazon.com/msk/latest/developerguide/what-is-msk.html)).
- **Managed Kafka: MSK access control.** IAM access control handles both authentication and authorization. Kafka ACLs, which are Kafka's own access rules, have no effect on IAM identities ([MSK IAM access control](https://docs.aws.amazon.com/msk/latest/developerguide/iam-access-control.html)).
- **Managed Kafka: MSK from other VPCs.** Clients in other VPCs or accounts use multi-VPC private connectivity. It is built on AWS PrivateLink. It supports IAM, TLS and SASL/SCRAM. TLS encrypts the connection, and SASL/SCRAM is a login with a user name and a password. It needs the same number of client subnets as cluster subnets, in the same zone IDs ([MSK multi-VPC](https://docs.aws.amazon.com/msk/latest/developerguide/aws-access-mult-vpc.html)).
- **Managed Kafka: Google Cloud sizing.** You set the vCPU count and memory, and the service provisions the brokers. The page says that all clusters are provisioned in a rack-aware three-zone configuration ([Google Cloud Managed Service for Apache Kafka](https://cloud.google.com/managed-service-for-apache-kafka/docs/overview)).
- **Managed Kafka: Google Cloud access.** Access goes through Private Service Connect, which is Google Cloud's private connection to a service. You authenticate with SASL as an IAM identity or with mutual TLS, which means that both sides show a certificate. All connections use TLS ([Google Cloud Managed Service for Apache Kafka](https://cloud.google.com/managed-service-for-apache-kafka/docs/overview)).
- **Consumers: how many pods help.** KEDA's Kafka scaler does not exceed the number of partitions by default. `allowIdleConsumers: 'true'` lifts that cap ([KEDA Kafka scaler](https://keda.sh/docs/latest/scalers/apache-kafka/)).
- **Consumers: the scaler's defaults.** The lag is the number of messages that a consumer group has not read yet. `lagThreshold` has a default of 10, and `activationLagThreshold` has a default of 0 ([KEDA Kafka scaler](https://keda.sh/docs/latest/scalers/apache-kafka/)).
- **Consumers: a full manifest.** A `ScaledObject` is the KEDA object that ties a workload to a scaler. [Two application shapes](#p06-1) shows a full one for a Kafka topic.
- **Consumers: three cases (illustrative).** The picture shows a topic with 6 partitions. With 4 consumers, two consumers read 2 partitions each. With 6 consumers, each reads one partition. With 10 consumers, 4 sit idle.

![Consumers against partitions](diagrams/extra/d18-kafka-partition-cap.svg)

- **Pub/Sub has no partition cap.** Pub/Sub leases single messages to subscriber clients, so the number of concurrent clients is not limited by partitions ([Pub/Sub overview](https://cloud.google.com/pubsub/docs/overview)). Scale on the backlog instead.
- **Pub/Sub: scale with a HorizontalPodAutoscaler.** A HorizontalPodAutoscaler is a Kubernetes object that changes the number of pods of a Deployment when a metric changes. The GKE tutorial installs the Custom Metrics Adapter, which passes Cloud Monitoring metrics to Kubernetes ([GKE tutorial](https://cloud.google.com/kubernetes-engine/docs/tutorials/autoscaling-metrics)).
- **Pub/Sub: an external metric.** The tutorial's HorizontalPodAutoscaler uses an external metric ([GKE tutorial](https://cloud.google.com/kubernetes-engine/docs/tutorials/autoscaling-metrics)). Kubernetes serves external metrics through the `external.metrics.k8s.io` API ([Kubernetes HPA](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)).
- **Pub/Sub: scaling to zero.** `minReplicas` may be 0 for an external metric. Scaling to and from zero is Beta since Kubernetes v1.37 in the Kubernetes docs read on 2026-11-22, and it is enabled by default ([Kubernetes HPA](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)).
- **Pub/Sub: a HorizontalPodAutoscaler on the backlog.** The values in the manifest are the tutorial's example.

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: pubsub
spec:
  minReplicas: 1
  maxReplicas: 5
  metrics:
  - type: External
    external:
      metric:
        name: pubsub.googleapis.com|subscription|num_undelivered_messages
        selector:
          matchLabels:
            resource.labels.subscription_id: echo-read
      target:
        type: AverageValue
        averageValue: 2
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: pubsub
```

- **Pub/Sub: KEDA's scaler is marked deprecated.** KEDA has a `gcp-pubsub` scaler with the modes `SubscriptionSize` and `OldestUnackedMessageAge`. Its page, as read on 2026-11-22, carries a warning that the scaler is deprecated and will not receive changes ([KEDA Pub/Sub scaler](https://keda.sh/docs/latest/scalers/gcp-pub-sub/)). This page's reading: use an external metric in a HorizontalPodAutoscaler for new work.
- **Pub/Sub: a pull subscription.** It needs subscriber pods, and Google recommends its own high-level client library for most cases ([Pub/Sub StreamingPull](https://cloud.google.com/pubsub/docs/pull)).
- **Pub/Sub: a push subscription.** It calls an HTTPS endpoint with a certificate that is not self-signed, on the public web ([Pub/Sub subscriber overview](https://cloud.google.com/pubsub/docs/subscriber)).
- **Pub/Sub: an export subscription.** It writes to BigQuery or Cloud Storage and needs no subscriber ([Pub/Sub subscriber overview](https://cloud.google.com/pubsub/docs/subscriber)).
- **Landing events: a Flink job.** A Flink job writes to Iceberg and commits after a successful Flink checkpoint ([Iceberg Flink writes](https://iceberg.apache.org/docs/latest/flink-writes/)).
- **Landing events: Kafka Connect.** Kafka Connect is a program that moves data between Kafka and other systems. Its Iceberg sink writes Kafka topics into Iceberg tables ([Iceberg Kafka Connect](https://iceberg.apache.org/docs/latest/kafka-connect/)). [Appendix A](#d-data-platforms-appendix-a-tool-by-tool-what-each-asks-of-kubernetes) has the sink's commit and delivery details.
- **Landing events: a Kafka Connect image.** Strimzi can build a Kafka Connect image that contains such a connector ([Strimzi overview](https://strimzi.io/docs/operators/latest/overview.html)).
- **Landing events: Kafka Connect and zones.** Kafka Connect can set `client.rack` through its own `rack` section ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Landing events: a queue absorbs bursts.** A queue between producers and the lake absorbs bursts, and [Thundering herd](#d05-8) covers that. [Where AI training data comes from](#d18-8) shows what happens to the table afterwards.

### Lakes, warehouses and table formats: Parquet, Iceberg and Delta Lake

**The Kubernetes question.** The training team says "read the table". Which part of the table must your pods reach, with which permission, and which job keeps it healthy while a writer adds data?

**Scenario.** This scenario is illustrative. A fine-tuning job starts at 9:00 and runs for six hours while a Flink job keeps adding rows to the same table. [Design 03](#design-training-inference) already shows how to pin one version of the data for the whole run. This team's open question is which Kubernetes jobs keep the table healthy while the stream keeps writing.

![What is on the disk: three layers](diagrams/sections/d18-5.svg)

**What the picture shows.** Three layers, from the pods down to the files. The version numbers in the picture are illustrative.

- **Pods: the engines.** Spark and Flink are the engines from [Spark, Flink and Beam](#d18-2). The libraries PyArrow, PyIceberg and delta-rs read Parquet files, Iceberg tables and Delta tables from Python. All of them run in your pods. The files in the two layers under them need no server of their own.
- **Table format: the list of files.** The table format lists which files make up the table at one moment. [Appendix C](#d-data-platforms-appendix-c-iceberg-and-delta-lake-in-detail) has the file names of Iceberg and Delta Lake.
- **Files: Parquet in object storage.** Parquet is a file format that stores data column by column.
- **Reader and writer.** [Training: data and checkpoints](#ds03-4) in Design 03 teaches how a training run keeps one version of an Iceberg table while a writer commits new ones.

**Going deeper, by topic.**

- **Iceberg: the pointer swap.** Every Iceberg change replaces the table's current metadata file with an atomic swap, which means that the swap happens completely or not at all. The spec describes two ways to do the swap ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Iceberg: the swap in a file system.** A writer renames a file, in a file system that supports atomic rename, like HDFS. The spec marks this scheme as deprecated and says it is unsafe in object stores and local file systems ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Iceberg: the swap in a metastore.** The other way keeps a pointer in a metastore or a database and updates it with a check-and-put operation ([Iceberg spec](https://iceberg.apache.org/spec/)). A metastore is a service that keeps the list of tables.
- **Iceberg: what a catalog does.** Iceberg's catalogs let SQL commands manage tables and load them by name ([Iceberg Spark getting started](https://iceberg.apache.org/docs/latest/spark-getting-started/)). This page's reading: on S3 or Cloud Storage the pointer therefore lives in a metastore or a database, so a pod must reach a catalog service. No page read says which scheme applies to S3 or Cloud Storage.
- **Iceberg: a path-based catalog.** Spark's getting-started page also shows a path-based catalog for tables under a local warehouse folder ([Iceberg Spark getting started](https://iceberg.apache.org/docs/latest/spark-getting-started/)). No page read shows it on S3 or Cloud Storage.
- **Iceberg catalogs on AWS.** On AWS, Iceberg can use AWS Glue as the catalog, where a namespace is a Glue database and a table is a Glue table. AWS Glue is an AWS service that includes a data catalog. Iceberg can also use a DynamoDB table as the catalog. DynamoDB is the key-value database service of AWS ([Iceberg on AWS](https://iceberg.apache.org/docs/latest/aws/)).
- **Iceberg catalogs in Snowflake.** Snowflake can be the Iceberg catalog, or it can connect to an outside catalog ([Snowflake Iceberg tables](https://docs.snowflake.com/en/user-guide/tables-iceberg)).
- **Iceberg catalog: what a pod sets.** This page's reading: in a pod, the catalog is a set of engine properties plus an IAM permission. Spark sets the properties under `spark.sql.catalog.<name>` ([Iceberg Spark getting started](https://iceberg.apache.org/docs/latest/spark-getting-started/)). No page read describes running your own catalog server on Kubernetes.
- **Delta keeps its log beside the data.** A Delta table keeps its log in a folder next to the data files. Delta calls the table "self describing", and says this removes the need for a separate metastore just to read the data ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)). A pod can read a Delta table through the Python package `deltalake` ([delta-rs](https://raw.githubusercontent.com/delta-io/delta-rs/main/README.md)). [Appendix C](#d-data-platforms-appendix-c-iceberg-and-delta-lake-in-detail) has the log layout.
- **Maintenance: Iceberg snapshots.** Both formats leave old files behind. Iceberg recommends expiring snapshots regularly, which removes old versions from the table and deletes the data files that nothing needs anymore ([Iceberg maintenance](https://iceberg.apache.org/docs/latest/maintenance/)).
- **Maintenance: a Flink writer leaves a marker.** A Flink streaming write job keeps the ID of its last committed checkpoint in the snapshot summary ([Iceberg Flink writes](https://iceberg.apache.org/docs/latest/flink-writes/)).
- **Maintenance: what can go wrong.** Orphan files are files in the table folder that no snapshot lists. Expiring snapshots or deleting orphan files could corrupt the state of the Flink job ([Iceberg Flink writes](https://iceberg.apache.org/docs/latest/flink-writes/)).
- **Maintenance: what to keep.** Keep the last snapshot that the Flink job created, and delete only orphan files that are old enough ([Iceberg Flink writes](https://iceberg.apache.org/docs/latest/flink-writes/)).
- **Maintenance: Delta vacuum.** Delta's `vacuum` deletes files that are no longer in the latest version after a retention period, 7 days by default ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)).
- **Maintenance: run it as a job.** Each clean-up is a job. It needs pod resources, write permission and a schedule. The schedule can be a CronJob, which starts a Job at set times, or an orchestrator task. This page's reading: never set a clean-up window shorter than your longest training run, or the run may lose files that it still reads.
- **Watch the commit, not only the pod.** A Flink job can keep running while its Iceberg commits fail. The Iceberg docs call `elapsedSecondsSinceLastSuccessfulCommit` an ideal alerting metric. Their example: with a 5-minute commit interval, alert when it passes 60 minutes ([Iceberg Flink writes](https://iceberg.apache.org/docs/latest/flink-writes/)).
- **File layout sets how many readers you can use.** This page's reading: the number of Parquet files limits how many reader pods can work without overlap, because a reader takes whole files or slices of files. A row group is a horizontal slice of a Parquet file. [Appendix B](#d-data-platforms-appendix-b-parquet-in-detail) has the file layout.
- **Lake, warehouse and lakehouse.** On Kubernetes, a lake or a lakehouse is files in a bucket plus engines in your pods. A warehouse such as BigQuery or Snowflake is a managed service that you only connect to. A lakehouse puts a table format on lake files. [Appendix D](#d-data-platforms-appendix-d-lake-warehouse-and-lakehouse) has the definitions and one team's argument for the lakehouse.
- **BigQuery can hold Iceberg tables in your bucket.** BigQuery's Iceberg managed tables store data in buckets that you own. BigQuery warns that changing those files outside BigQuery can cause query failure or data loss, and it deletes untracked files in the bucket during background clean-up ([BigQuery Iceberg tables](https://cloud.google.com/bigquery/docs/iceberg-tables)).
- **BigQuery: an Iceberg export for other engines.** Open-source engines such as Spark read the table through an Iceberg metadata snapshot, which is the table's state in the Iceberg version 2 format. BigQuery exports it with `EXPORT TABLE METADATA` ([BigQuery Iceberg tables](https://cloud.google.com/bigquery/docs/iceberg-tables)).
- **Snowflake can hold Iceberg tables in your bucket.** Snowflake's Iceberg tables keep their files in an external cloud storage location that you manage ([Snowflake Iceberg tables](https://docs.snowflake.com/en/user-guide/tables-iceberg)).
- **Snowflake: a sync for other engines.** A Snowflake-managed Iceberg table can be synced to Snowflake Open Catalog, which is a catalog service that outside engines can query. Third-party engines can then query the table ([Snowflake Iceberg tables](https://docs.snowflake.com/en/user-guide/tables-iceberg)).
- **What pods read, and who writes.** This page's reading: give training pods read-only access to the table's folder, and give write access to one clean-up job and to the writers. Design 28 shows Parquet in a trajectory store in [Trajectory format and storage](#ds28-6). [The page-cache memory trap](#d05-7) covers reading a dataset many times.

### BigQuery and Snowflake: warehouses you connect to

**The Kubernetes question.** The feature table lives in BigQuery or Snowflake. How do training pods read it, as whom, over which network path, and what does it cost when the cluster is far from the warehouse?

**Scenario.** This scenario is illustrative. A team on EKS needs a 1 TB table from BigQuery for a fine-tune. The first try uses a SQL client in one pod and takes hours. The second try reads many streams in parallel, and the bill shows a data-transfer line that nobody expected.

![Four ways a pod gets warehouse data](diagrams/sections/d18-6.svg)

**What the picture shows.** The left side shows training pods in your cluster with a service account. The right side shows BigQuery and Snowflake, which you do not run. Four paths lead from the warehouses to the pods. The stream count N is illustrative. The price footnote shows only prices that were read.

- **Path 1, the BigQuery Storage Read API.** Several streams read the table in parallel. This page's reading: each stream can go to a different reader pod. The data arrives in Arrow format. Arrow is a format for tables in memory.
- **Path 2, the Snowflake connector.** The Python connector fetches results in batches, as pandas data frames or as Arrow tables. A pandas data frame is a table that the Python library pandas holds in memory ([Snowflake Python connector](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api)).
- **Path 3, export or unload to files.** BigQuery exports a table to Cloud Storage. Snowflake unloads a query to a file location. Your pods then read the files like any lake data. [Appendix H](#d-data-platforms-appendix-h-calls-and-options-for-reading-data) has the export and unload options.
- **Path 4, Iceberg tables in your bucket.** Both warehouses can keep Iceberg tables in a bucket that you own. Other engines need a metadata export from BigQuery or a sync to Snowflake Open Catalog first. Then your pods read the files with read-only access (this page's reading). [Lakes, warehouses and table formats](#d18-5) has the details.

**Going deeper, by topic.**

- **Both are managed only.** BigQuery has a serverless design with "zero infrastructure management" ([BigQuery introduction](https://cloud.google.com/bigquery/docs/introduction)). For Snowflake, the docs say you cannot install and run it locally or on private cloud infrastructure ([Snowflake key concepts](https://docs.snowflake.com/en/user-guide/intro-key-concepts)). You reach both with an identity and a network path.
- **BigQuery: three ways out.** The paginated API reads one page per call, in order, and suits small results. The Storage Read API gives high-throughput streaming access. It supports column selection and server-side filters, and it splits a read into several disjoint streams. Export copies the table to Cloud Storage ([BigQuery storage overview](https://cloud.google.com/bigquery/docs/storage_overview)).
- **BigQuery: a read session.** You create a session and set the maximum number of streams, a snapshot time, the columns and a filter. The server then creates streams that each cover about the same amount of data, and you must read all of them ([BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage)). This page's reading: run one reader pod per stream.
- **BigQuery: how long a session and a stream live.** A session lasts at least 6 hours. A stream that gets no read request for more than 1 hour is closed. After an error, you can restart a stream at a row offset ([BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage)).
- **Spark can read BigQuery.** The Spark BigQuery connector reads through the Storage Read API with Arrow by default. Its `maxParallelism` setting caps the partitions, and some partitions can be empty when there are not enough executors ([Spark BigQuery connector](https://raw.githubusercontent.com/GoogleCloudDataproc/spark-bigquery-connector/master/README.md)). This page's reading: the same identity and network rules apply to executor pods. [Appendix H](#d-data-platforms-appendix-h-calls-and-options-for-reading-data) lists the Snowflake Spark connector.
- **When to stream and when to export.** This page's reading: stream small, selective reads such as a filtered slice of a feature table. Export a large table once to Parquet and train from the files, as [Where AI training data comes from](#d18-8) describes. Nothing here is measured. No source read gives a throughput figure for either path.
- **Snowflake identity: federation.** Federation means that Snowflake accepts a signed token from your cluster as proof of identity. OIDC is a standard way to prove an identity with a signed token, and the issuer is the web address of the cluster that signs the tokens. Snowflake supports federation with the OIDC issuer of EKS, AKS (Azure's Kubernetes service) or GKE ([Snowflake workload identity federation](https://docs.snowflake.com/en/user-guide/workload-identity-federation)).
- **Snowflake identity: the token.** The pod mounts a projected service account token, which is a token that Kubernetes puts in a file in the pod. Its audience is `snowflakecomputing.com`. The audience is the name of the service that the token is meant for ([Snowflake workload identity federation](https://docs.snowflake.com/en/user-guide/workload-identity-federation)).
- **Snowflake identity: status.** Snowflake's page gives no status label, so this page does not say whether the feature is generally available ([Snowflake workload identity federation](https://docs.snowflake.com/en/user-guide/workload-identity-federation)).
- **Snowflake identity: the Snowflake side.** The Snowflake administrator creates a user of type `SERVICE` with `WORKLOAD_IDENTITY = (TYPE = OIDC ISSUER = '<issuer URL>' SUBJECT = 'system:serviceaccount:<namespace>:<service account>')` ([Snowflake workload identity federation](https://docs.snowflake.com/en/user-guide/workload-identity-federation)).
- **Snowflake identity: the connector side.** The Python connector then uses `authenticator='WORKLOAD_IDENTITY'`, `workload_identity_provider='OIDC'` and `token_file_path`. No password or key goes into the image ([Snowflake workload identity federation](https://docs.snowflake.com/en/user-guide/workload-identity-federation)).
- **Snowflake identity: the pod fragment.** The pod mounts the token at `/var/run/secrets/tokens`. The doc's example uses 7200 seconds, which is illustrative here. Kubernetes defaults to 1 hour and requires at least 600 seconds ([Kubernetes projected volumes](https://kubernetes.io/docs/concepts/storage/projected-volumes/), [Snowflake workload identity federation](https://docs.snowflake.com/en/user-guide/workload-identity-federation)).

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: warehouse-reader               # illustrative
spec:
  serviceAccountName: build-robot      # the docs' example name; the Snowflake SUBJECT must name it
  containers:
  - name: reader
    image: example.com/reader:latest   # illustrative
    volumeMounts:
    - name: snowflake-token
      mountPath: /var/run/secrets/tokens
      readOnly: true
  volumes:
  - name: snowflake-token
    projected:
      sources:
      - serviceAccountToken:
          path: snowflake-token
          audience: snowflakecomputing.com
          expirationSeconds: 7200      # the docs' example; Kubernetes minimum is 600
```

- **BigQuery identity from a pod.** On GKE, Workload Identity Federation for GKE grants IAM roles to a Kubernetes service account. The principal has the form `principal://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/PROJECT_ID.svc.id.goog/subject/ns/NAMESPACE/sa/SERVICEACCOUNT` ([GKE Workload Identity](https://cloud.google.com/kubernetes-engine/docs/concepts/workload-identity)).
- **BigQuery: the role for a read session.** A read session needs the role `bigquery.readSessionUser` on the project ([BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage)).
- **BigQuery identity from an EKS pod.** Google's page says IAM Workload Identity Federation also covers workloads on AWS, Azure and self-managed Kubernetes. The steps for an EKS pod were not read ([GKE Workload Identity](https://cloud.google.com/kubernetes-engine/docs/concepts/workload-identity)).
- **An older pattern to avoid.** The Kubeflow Spark operator's Google Cloud page mounts a service account JSON key file from a Secret. That page also uses a Spark v2.3.0 image, so it is dated ([Spark operator on Google Cloud](https://spark.kubeflow.org/en/latest/user-guide/gcp.html)). This page's reading: prefer federation. [Secret management](#d13-5) covers Secrets.
- **Network path: Snowflake.** Snowflake PrivateLink connects your AWS VPCs to the Snowflake VPC without crossing the public internet. It needs the Business Critical edition, which is a plan level of Snowflake, or higher. It also works across regions ([Snowflake PrivateLink](https://docs.snowflake.com/en/user-guide/admin-security-privatelink)).
- **Network path: Google Cloud.** Private Google Access lets VMs with only internal IP addresses reach Google APIs. You turn it on for each subnet, and it does not support regional endpoints ([Private Google Access](https://cloud.google.com/vpc/docs/private-google-access)).
- **Units in these prices.** A GiB is 1,024 x 1,024 x 1,024 bytes, and a TiB is 1,024 GiB. The prices in this section use GiB, TiB and GB, so read the unit before you compare two prices. [Appendix G](#d-data-platforms-appendix-g-words) repeats this.
- **BigQuery cost: inside one continent.** A read inside one location is free. A read between locations on the same continent costs $0.01 per GiB, unless a free case applies, such as a multi-region read into a region on the same continent ([BigQuery pricing](https://cloud.google.com/bigquery/pricing)). [Appendix E](#d-data-platforms-appendix-e-prices-and-limits-with-the-date-each-was-read) lists these prices with their date.
- **BigQuery cost: between continents.** A read between continents costs $0.08 per GiB, or $0.15 per GiB when Australia is one end ([BigQuery pricing](https://cloud.google.com/bigquery/pricing)).
- **BigQuery cost: streaming reads.** Streaming reads cost $1.10 per TiB, and the first 300 TiB a month are free ([BigQuery pricing](https://cloud.google.com/bigquery/pricing)).
- **BigQuery cost: export.** Batch export is free when it uses the shared pool of compute slots, up to 50 TiB a day. A slot is BigQuery's unit of compute. Cross-region export is billed as data transfer ([BigQuery pricing](https://cloud.google.com/bigquery/pricing)). A pod on EKS that reads BigQuery crosses clouds. The AWS price for that traffic was not read.
- **Snowflake cost: the units.** A virtual warehouse is the compute that runs queries. A credit is the unit that Snowflake bills in.
- **Snowflake cost: the size.** An X-Small warehouse uses 1 credit per hour on first-generation standard warehouses, and the use doubles with each larger size ([Snowflake warehouses](https://docs.snowflake.com/en/user-guide/warehouses-overview)).
- **Snowflake cost: billing.** Billing is per second with a 60-second minimum each time the warehouse starts ([Snowflake warehouses](https://docs.snowflake.com/en/user-guide/warehouses-overview)).
- **Snowflake cost: auto-suspend.** Auto-suspend stops an idle warehouse, and it is on by default ([Snowflake warehouses](https://docs.snowflake.com/en/user-guide/warehouses-overview)).
- **Snowflake cost: not known.** The dollar price of a credit and Snowflake's data-egress price were not read.

### Reaching managed services from pods: identity, network path and cost

**The Kubernetes question.** A training pod must read S3, a Kafka topic, a BigQuery table and a Snowflake table. How does each pod prove who it is without keys in the image, which path does the traffic take, and which hop costs money?

**Scenario.** This scenario is illustrative. The monthly bill shows a large NAT gateway line. The cause is a data-preparation job on EKS that reads S3 through a NAT gateway instead of a gateway endpoint. A NAT gateway is a managed AWS device that lets private subnets reach outside addresses. A gateway endpoint is a route from your VPC to S3 or DynamoDB that stays inside AWS ([AWS gateway endpoints](https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html)).

![Who the pod is, and which road it takes](diagrams/sections/d18-7.svg)

**What the picture shows.** One pod with its service account, and five roads to five services. On each road, the picture shows the identity step in bold, the path in grey and a cost box on the right. A cost box shows a number only where one was read. The date on which each price was read is in the picture's footnote and in Appendix E.

- **The S3 road.** It has two variants. A gateway endpoint has no additional charge. A NAT gateway charges per hour and per GB.
- **The MSK road.** The pod uses IAM access control. This page's reading: traffic between zones costs money.
- **The Pub/Sub road.** The pod uses an IAM identity, and Pub/Sub has no zone data-transfer fee.
- **The BigQuery road and the Snowflake road.** Snowflake accepts a Kubernetes token that your cluster signs. For BigQuery, the role `bigquery.readSessionUser` is what a read session needs. The BigQuery identity steps for a pod on EKS were not read. [BigQuery and Snowflake: warehouses you connect to](#d18-6) has the details.

**Going deeper, by topic.**

- **S3 on AWS: identity.** EKS Pod Identity maps an IAM role to a service account in a namespace. When a pod uses that service account, EKS sets environment variables in its containers, and the AWS SDKs then use the Pod Identity credentials ([EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)).
- **S3 on AWS: the Pod Identity Agent.** The Pod Identity Agent runs as a DaemonSet on each node. A DaemonSet is a Kubernetes object that runs one pod on every node ([EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)).
- **S3 on AWS: the association limit.** A cluster supports up to 5,000 Pod Identity associations ([EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)). [Identity, data, network and observability](#d07-2) and [Identity and model endpoints](#d13-6) cover identity in general.
- **S3 on AWS: the role scoping rule.** The role scoping holds only if access to the instance metadata service (IMDS) is restricted. IMDS is an address that each node answers on, and it hands out the node's own credentials. Otherwise a container can also reach the node's IAM role ([EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)).
- **S3 on AWS: the path.** A gateway endpoint for S3 or DynamoDB lets a VPC reach them without an internet gateway or a NAT device, and it has no additional charge ([AWS gateway endpoints](https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html)).
- **S3 on AWS: the NAT gateway price.** A NAT gateway charges for each hour that it is available and for each GB that it processes. In AWS's example for US East (Ohio), that is $0.045 per hour and $0.045 per GB ([AWS VPC pricing](https://aws.amazon.com/vpc/pricing/)).
- **S3 on AWS: data inside one Region.** Data between S3 and EC2 instances in the same Region is free. If other AWS services sit in the path, such as PrivateLink endpoints, NAT Gateway or Transit Gateway, you pay their data processing costs. This price text was read on 2026-11-20 and not re-checked ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/)). [Networking](#d12-2) in the cost deep dive names VPC endpoints too.
- **S3 on AWS: how to check.** This page's reading: look at the route table of the pod's subnet to see where S3 traffic goes.
- **MSK on AWS: identity.** The pod authenticates with IAM access control, and Kafka ACLs have no effect on IAM identities ([MSK IAM access control](https://docs.aws.amazon.com/msk/latest/developerguide/iam-access-control.html)).
- **MSK on AWS: path.** The brokers sit in one subnet per zone. Clients in other VPCs and accounts use multi-VPC private connectivity ([Amazon MSK](https://docs.aws.amazon.com/msk/latest/developerguide/what-is-msk.html), [MSK multi-VPC](https://docs.aws.amazon.com/msk/latest/developerguide/aws-access-mult-vpc.html)).
- **MSK on AWS: cost.** Data in and out of network interfaces across zones in one Region is $0.01 per GB in each direction. This price was read on 2026-11-20 and not re-checked ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/)). This page's reading: that rate applies to client traffic that crosses zones. MSK's own transfer prices were not read.
- **Pub/Sub on Google Cloud: identity.** Pub/Sub supports granular IAM, which gives access control for individual resources ([Pub/Sub overview](https://cloud.google.com/pubsub/docs/overview)). From GKE, Workload Identity Federation for GKE gives a Kubernetes service account an IAM principal ([GKE Workload Identity](https://cloud.google.com/kubernetes-engine/docs/concepts/workload-identity)).
- **Pub/Sub on Google Cloud: path.** A pod calls the pull API, and export subscriptions need no client ([Pub/Sub subscriber overview](https://cloud.google.com/pubsub/docs/subscriber)).
- **Pub/Sub on Google Cloud: cost.** The first 10 GiB of throughput a month is free, then $40 per TiB. BigQuery and Cloud Storage subscriptions cost $50 per TiB. There are no zone data-transfer fees, and traffic between regions is billed at VPC network rates ([Pub/Sub pricing](https://cloud.google.com/pubsub/pricing)).
- **BigQuery and Snowflake.** Identity, path and cost are in [BigQuery and Snowflake: warehouses you connect to](#d18-6). In short, BigQuery reads in the same location are free, and Snowflake needs a token that the cluster signs.
- **Three questions before a pod reads outside the cluster.** Which identity does the cloud see? Does the traffic leave through NAT, an endpoint, a private link or the public internet? Does any hop bill per GB? This is this page's checklist, not a source's.

### Where AI training data comes from, and how it reaches the pods

**The Kubernetes question.** A run needs 2 TB of text and tables that other teams produce (2 TB is illustrative). Where does that data come from, which route puts it in front of the GPUs without leaving them idle or surprising the bill, and what must each route ask of the pods and nodes?

**The words.** A training process is one running copy of the training program. A loader worker is a helper process that a training process starts to read and prepare data. A shard is one slice of the data that one worker reads.

**Scenario.** This scenario is illustrative. A fine-tune on 4 GPUs shows low utilization. The training pods read Parquet straight from a table that a streaming job keeps updating. There is one training process for each GPU, with 4 loader workers each, so there are 16 workers. The table has only 8 files, and each worker reads whole files. Half of the workers have no file to read.

![From producers to GPUs](diagrams/sections/d18-8.svg)

**What the picture shows.** Producers on the left write into the lake in the middle. Four routes lead from the lake, or from outside it, to the training pods on the right. The counts (2 TB, 8 files, 4 training processes, 4 workers each) are illustrative.

- **Producers.** A Spark job that Airflow or Dagster starts. A Flink stream job. Kafka and Pub/Sub. A warehouse export. A public dataset hub, such as Hugging Face, where people share datasets. [Spark, Flink and Beam](#d18-2), [Airflow and Dagster](#d18-3), [Kafka and Pub/Sub](#d18-4) and [BigQuery and Snowflake](#d18-6) describe the first four.
- **The lake.** Parquet files and an Iceberg or Delta table in object storage, read at one snapshot.
- **Route A: read lake files directly.** The pods read Parquet or table files from object storage.
- **Route B: ask a warehouse.** The pods read through a warehouse API. This route suits small, selective reads.
- **Route C: stream from a hub.** The pods stream a public dataset while they iterate over it.
- **Route D: a staged copy near the GPUs.** The data is copied to storage close to the GPUs first. [Training data and checkpoints: which storage path, and how it reaches the pods](#d16-1) in the deep dive on distributed storage and data volume for training teaches this route. In the picture, it sits in a grey dashed box.
- **Loader workers and the shard rule.** The training pods start loader workers, and each worker needs a share of the shards. The shard rule in the picture is this page's reading: the shards must be at least as many as the workers.
- **GPUs.** The loaders must keep the GPUs busy.

**Going deeper, by topic.**

- **Where data lands.** Batch engines write tables, and Airflow or Dagster start them. Stream jobs write through Flink or the Kafka Connect Iceberg sink. Pub/Sub export subscriptions write to BigQuery or Cloud Storage. Warehouse exports write files. Public hubs, such as Hugging Face, serve datasets. Each producer lands in a place that Routes A to D start from.
- **Route A: the readers are libraries in your pods.** PyArrow, PyIceberg, `deltalake` and Ray Data read lake files in your pods. On Kubernetes, Ray runs as KubeRay head and worker pods ([Ray on Kubernetes](https://docs.ray.io/en/latest/cluster/kubernetes/index.html)). [Ray Train on KubeRay](#ds20-3) in Design 20 shows the training side. [Appendix H](#d-data-platforms-appendix-h-calls-and-options-for-reading-data) lists the read calls of each library.
- **Route B: read from a warehouse API.** [BigQuery and Snowflake](#d18-6) covers it. In short, use it for small and selective reads, and export big tables to files.
- **Route C: stream a hub dataset.** With streaming on, Hugging Face reads a dataset while you iterate over it ([Hugging Face streaming](https://huggingface.co/docs/datasets/stream)). [Appendix H](#d-data-platforms-appendix-h-calls-and-options-for-reading-data) has the shard calls.
- **Route D: a staged copy near the GPUs.** This page names it and stops. Where to stage the data, which tier to use, how to cache it and how to size the throughput are taught in [Training data and checkpoints: which storage path, and how it reaches the pods](#d16-1).
- **Route D: the Kubeflow Trainer data cache.** Kubeflow Trainer is a Kubernetes-native platform for distributed AI training. It also has a distributed data cache that streams data to GPU nodes and moves preprocessing to CPU workloads ([Kubeflow Trainer](https://www.kubeflow.org/docs/components/trainer/overview/)). [Appendix H](#d-data-platforms-appendix-h-calls-and-options-for-reading-data) names its building blocks.
- **The reader asks for CPU.** A PyTorch `DataLoader` is the PyTorch object that feeds batches to a training loop. With `num_workers` above 0, it starts that many worker processes. Each worker loads `prefetch_factor` batches ahead, with a default of 2, so `2 * num_workers` batches wait in total ([PyTorch data](https://docs.pytorch.org/docs/2.15/data.html)). On Kubernetes this is a CPU request for each training pod. No source read says which worker count you need. [Appendix H](#d-data-platforms-appendix-h-calls-and-options-for-reading-data) has the `pin_memory` setting.
- **The reader must not duplicate data.** With an iterable-style dataset, each worker gets a copy of the dataset object ([PyTorch data](https://docs.pytorch.org/docs/2.15/data.html)).
- **Each copy must read a different part.** The PyTorch docs say that naive multi-process loading will often return duplicated data, unless each copy is set up to read a different part ([PyTorch data](https://docs.pytorch.org/docs/2.15/data.html)).
- **How many shards.** This page's reading: the shard count must be at least the number of loader workers across all training processes. Otherwise some workers have nothing to read, as in the scenario.
- **The reader asks for memory.** Reading a dataset many times fills the page cache, which is memory that Linux uses to keep recently read file data. The working set can grow until the pod is killed ([The page-cache memory trap](#d05-7)).
- **The reader asks for scratch space.** Local scratch space on the node is `ephemeral-storage`, which has requests and limits. An `emptyDir` is a scratch folder that lives as long as the pod. If it is backed by memory, it counts against the pod's memory limit ([Kubernetes resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/), [Kubernetes ephemeral volumes](https://kubernetes.io/docs/concepts/storage/ephemeral-volumes/)).
- **Pin the data version.** Give the run a snapshot ID as a parameter, not the word "latest". PyIceberg scans take a `snapshot_id` ([PyIceberg](https://py.iceberg.apache.org/api/)). BigQuery read sessions take a snapshot time ([BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage)). [Training: data and checkpoints](#ds03-4) shows tags on Iceberg snapshots.
- **Pack shards with an Indexed Job.** In an Indexed Job, each pod gets an index from 0 to `completions - 1` in the environment variable `JOB_COMPLETION_INDEX`. That maps shard i to pod i. `backoffLimitPerIndex` retries one failed index without restarting the others ([Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)). Use this for the step that writes training shards, not for the training job. The counts, the image and the command in the manifest are illustrative.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: pack-shards                   # illustrative
spec:
  completionMode: Indexed
  completions: 64                     # illustrative: one pod for each shard
  parallelism: 8                      # illustrative
  backoffLimitPerIndex: 2             # illustrative
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: pack
        image: example.com/pack-shards:latest    # illustrative
        command: ["sh", "-c", "pack-one-shard --index $JOB_COMPLETION_INDEX"]   # illustrative
        resources:
          requests:
            cpu: "2"                  # illustrative
            memory: 8Gi               # illustrative
            ephemeral-storage: 20Gi   # illustrative
```

- **Keep the readers next to the data.** Keep the pods and the data in one region ([Training: data and checkpoints](#ds03-4), [Data and checkpoints across sites](#ds18-8) in Design 18). Send the bytes through an endpoint, not NAT ([Reaching managed services from pods](#d18-7)). [Getting model weights into pods](#post-model-data) covers weights, not datasets.
- **How to check memory.** The working set is the memory number that Kubernetes watches for OOM kills. `container_memory_working_set_bytes` shows it ([The page-cache memory trap](#d05-7)).
- **How to check scheduling and mounts.** Pod events and `kubectl describe pod` show scheduling and mount problems.
- **How to check freshness.** For a streamed table, the Iceberg commit-age metric shows freshness ([Lakes, warehouses and table formats](#d18-5)).
- **How to check the data wait.** No source read names a metric for how long a training step waits for data, so this page names none.

### Appendix A. Tool by tool: what each asks of Kubernetes

**This appendix answers the five questions for every tool.** The five questions are what you request, where pods land, what you configure, how you check, and what it costs and what must move. Each point is one answer for one tool. A point that is titled "not known", or that starts with "Not known", marks a gap in the sources read. [What runs on your cluster, and what you connect to](#d18-1) gives the classes.

**Going deeper, by topic.**

- **Spark: class.** Both. Pods on your cluster, or Amazon EMR on EKS ([EMR on EKS](https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/how-it-works.html)).
- **Spark: what you request.** Executor CPU through `spark.kubernetes.executor.request.cores`, memory, and a GPU resource for GPU jobs ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: where pods land.** A node selector, or a custom scheduler. In a `SparkApplication`, also tolerations and affinity ([Writing a SparkApplication](https://spark.kubeflow.org/en/latest/user-guide/writing-sparkapplication.html)).
- **Spark: what you configure.** A service account that can create pods, services and config maps ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: how you check.** `kubectl describe pod` on the driver ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: cost and what must move.** The executor pods while the job runs, plus the input, output and shuffle data ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: in-place resize.** In-place resize of executor memory needs Kubernetes 1.33 or later ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Spark: growing volumes.** Growing executor volumes needs `allowVolumeExpansion: true` on the StorageClass ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)). A StorageClass tells Kubernetes how to create volumes.
- **Spark: recovering shuffle data.** The plugin `KubernetesLocalDiskShuffleDataIO` lets a replacement executor recover shuffle data from a surviving volume ([Spark on Kubernetes](https://spark.apache.org/docs/latest/running-on-kubernetes.html)).
- **Flink: class.** Both. Pods on your cluster, or Amazon Managed Service for Apache Flink ([AWS](https://docs.aws.amazon.com/managed-flink/latest/java/what-is.html)).
- **Flink: what you request.** JobManager and TaskManager CPU and memory ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/), [Flink Kubernetes HA](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/ha/kubernetes_ha/)).
- **Flink: where pods land.** Node selectors and tolerations in a pod template ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/)).
- **Flink: what you configure.** A service account that can create pods and ConfigMaps, and a checkpoint folder that every pod can reach ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/), [Flink Kubernetes HA](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/ha/kubernetes_ha/)).
- **Flink: how you check.** Pod logs, and the Iceberg commit-age metric when the job writes to Iceberg ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/), [Iceberg Flink writes](https://iceberg.apache.org/docs/latest/flink-writes/)).
- **Flink: cost and what must move.** Always-on pods, and saved state that must survive an upgrade ([Flink native Kubernetes](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/resource-providers/native_kubernetes/), [Flink Kubernetes HA](https://nightlies.apache.org/flink/flink-docs-stable/docs/deployment/ha/kubernetes_ha/)).
- **Flink: the operator's objects.** The Flink operator adds the `FlinkDeployment` object, which runs either one application or a session cluster. A session cluster is a Flink cluster that has no job of its own, and jobs are submitted to it after it starts. It also adds `FlinkSessionJob`, which runs one job on an existing session cluster ([Flink operator overview](https://nightlies.apache.org/flink/flink-kubernetes-operator-docs-stable/docs/concepts/overview/), [custom resources](https://nightlies.apache.org/flink/flink-kubernetes-operator-docs-stable/docs/custom-resource/overview/)).
- **Flink: the operator's features.** The operator also provides an admission webhook, stateful upgrades, rollbacks and an autoscaler. No detail of the autoscaler was found ([Flink operator overview](https://nightlies.apache.org/flink/flink-kubernetes-operator-docs-stable/docs/concepts/overview/)).
- **Beam: class.** Both. A Beam program runs on your cluster through a runner, or on Dataflow.
- **Beam: what it asks.** It asks whatever its runner asks, plus a place for user code to run ([Beam SDK harness config](https://beam.apache.org/documentation/runtime/sdk-harness-config/)).
- **Beam: which runners.** The runners with a Kubernetes page read for this page are Flink and Spark. What you request, where the pods land and how you check are those of the runner's engine, so read the Spark entry or the Flink entry.
- **Beam: the other environment types.** `PROCESS` uses processes that the runner starts on each worker node. `EXTERNAL` sends the code to an outside service, for example a worker pool, which is a service that runs the Python workers. `LOOPBACK` runs the code in the submitting process, for testing only ([Beam SDK harness config](https://beam.apache.org/documentation/runtime/sdk-harness-config/)).
- **Airflow: class.** Both. Pods on your cluster, or Amazon MWAA and Google Managed Airflow ([MWAA](https://docs.aws.amazon.com/mwaa/latest/userguide/what-is-mwaa.html), [Google Managed Airflow](https://cloud.google.com/composer/docs/composer-3/composer-overview)).
- **Airflow: what you request.** A small always-on control plane, then one pod for each task with the Kubernetes executor ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: where pods land.** The pod template and the pod operator's affinity and tolerations ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: what you configure.** RBAC, which is the Kubernetes system of rights, to create pods, a place for DAG files, and persistent or remote logs ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: how you check.** The pod events that the pod operator writes to the task log ([Airflow KubernetesPodOperator](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/operators.html)).
- **Airflow: cost and what must move.** The task pods carry the cost ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Airflow: not known.** Any limit on pods for each namespace, and how Airflow handles GPU tasks.
- **Airflow: optional parts.** A worker and a triggerer are optional. A triggerer executes deferred tasks, and a basic installation that does not use them needs no triggerer ([Airflow architecture](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/overview.html)).
- **Airflow with several teams.** In a multi-team setup, each team's executor reads its own `[kubernetes_executor]` settings and namespace. The command `airflow kubernetes cleanup-pods --team <team>` refuses a namespace that another team's executor also uses, unless you pass `--allow-shared-namespace` ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Dagster: class.** Both. Pods on your cluster, or Dagster+ Hybrid ([Dagster+ Hybrid](https://docs.dagster.io/deployment/dagster-plus/hybrid)).
- **Dagster: what you request.** The Helm chart's always-on parts, then one Job for each run. Dagster+ Hybrid suggests starting points: 0.25 vCPU and 1 GB RAM for the agent, 0.25 vCPU and 1 GB for the code server, and 4 vCPU with 8 to 16 GB for runs ([Dagster+ Hybrid](https://docs.dagster.io/deployment/dagster-plus/hybrid)).
- **Dagster: where pods land and what you configure.** `runK8sConfig` and the `dagster-k8s/config` tag ([Dagster Kubernetes configuration](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/customizing-your-deployment)).
- **Dagster: how you check.** `kubectl get jobs` shows the run Jobs ([Dagster on Kubernetes](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/deploying-to-kubernetes)).
- **Dagster: cost and what must move.** The run Jobs while they run (this page's reading), and the database.
- **Dagster: the database.** Dagster's page says that in a real deployment users will likely want an external PostgreSQL database instead of the chart's own container ([Dagster Kubernetes configuration](https://docs.dagster.io/deployment/oss/deployment-options/kubernetes/customizing-your-deployment)).
- **Kafka on the cluster: class.** Both. Pods on your cluster with Strimzi ([Strimzi overview](https://strimzi.io/docs/operators/latest/overview.html)).
- **Kafka on the cluster: what you request.** Broker and controller pods with block-storage claims ([Strimzi deploying](https://strimzi.io/docs/operators/latest/deploying.html)).
- **Kafka on the cluster: where pods land.** A zone label for rack awareness ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: what you configure.** `broker.rack`, the replica selector, and the Drain Cleaner ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: how you check.** Whether replicas stay spread over racks, with Cruise Control ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: cost and what must move.** Block storage, and cross-zone traffic ([Strimzi configuring](https://strimzi.io/docs/operators/latest/configuring.html)).
- **Kafka on the cluster: what Strimzi can add.** Strimzi can also deploy Kafka Connect, which moves data between Kafka and other systems. It can deploy MirrorMaker 2, which copies topics between Kafka clusters. It can deploy Cruise Control, which rebalances partitions over brokers ([Strimzi overview](https://strimzi.io/docs/operators/latest/overview.html)).
- **Kafka: fetch from the nearest replica.** The feature comes from KIP-392, a Kafka Improvement Proposal. Its state on the page read is "Accepted" ([KIP-392](https://cwiki.apache.org/confluence/spaces/KAFKA/pages/95653762/KIP-392+Allow+consumers+to+fetch+from+closest+replica)).
- **Kafka Connect Iceberg sink: commits.** The sink can write to many tables. It commits every 5 minutes by default. It coordinates through a control topic, which is a Kafka topic that its tasks use to agree on commits ([Iceberg Kafka Connect](https://iceberg.apache.org/docs/latest/kafka-connect/)).
- **Kafka Connect Iceberg sink: delivery.** The sink gives exactly-once delivery, which means each message is written one time, through KIP-447. That needs Kafka 2.5 or later ([Iceberg Kafka Connect](https://iceberg.apache.org/docs/latest/kafka-connect/)).
- **Pub/Sub: exactly-once delivery.** Exactly-once delivery means that each message is delivered one time. Only pull subscriptions support it, and only within one cloud region ([Pub/Sub exactly-once](https://cloud.google.com/pubsub/docs/exactly-once-delivery)).
- **Pub/Sub: at-least-once delivery.** The BigQuery subscription supports at-least-once delivery ([Pub/Sub BigQuery subscription](https://cloud.google.com/pubsub/docs/bigquery)). At-least-once means that a message can arrive twice, so a pod that reads it must cope with duplicates.
- **Kafka as a managed service: class.** A managed service. You need an identity and a network path.
- **Kafka as a managed service: MSK.** MSK uses IAM access control and multi-VPC private connectivity ([MSK IAM access control](https://docs.aws.amazon.com/msk/latest/developerguide/iam-access-control.html), [MSK multi-VPC](https://docs.aws.amazon.com/msk/latest/developerguide/aws-access-mult-vpc.html)).
- **Kafka as a managed service: Google Cloud.** Google Cloud's service uses Private Service Connect with SASL or mutual TLS ([Google Cloud Managed Service for Apache Kafka](https://cloud.google.com/managed-service-for-apache-kafka/docs/overview)).
- **Kafka as a managed service: not known.** MSK and Google's Kafka prices were not read.
- **Pub/Sub: class.** A managed service ([Pub/Sub overview](https://cloud.google.com/pubsub/docs/overview)).
- **Pub/Sub: what you request.** Nothing for the service itself. A pull subscription needs subscriber pods, and an export subscription needs none ([Pub/Sub subscriber overview](https://cloud.google.com/pubsub/docs/subscriber)).
- **Pub/Sub: where pods land.** This page's reading: the same region as the data, because traffic between regions is billed at VPC network rates ([Pub/Sub pricing](https://cloud.google.com/pubsub/pricing)).
- **Pub/Sub: what you configure.** An IAM identity, and from GKE, Workload Identity Federation for GKE ([Pub/Sub overview](https://cloud.google.com/pubsub/docs/overview)).
- **Pub/Sub: how you check.** The backlog metric `pubsub.googleapis.com|subscription|num_undelivered_messages`, which the GKE tutorial scales on ([GKE tutorial](https://cloud.google.com/kubernetes-engine/docs/tutorials/autoscaling-metrics)).
- **Pub/Sub: cost and what must move.** $40 per TiB of throughput after the first 10 GiB a month ([Pub/Sub pricing](https://cloud.google.com/pubsub/pricing)).
- **BigQuery: class.** A managed service ([BigQuery introduction](https://cloud.google.com/bigquery/docs/introduction)).
- **BigQuery: what you request.** Reader pods for the Storage Read API, and this page's reading is one pod for each stream ([BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage)).
- **BigQuery: where pods land.** The same location as the dataset, because a read inside one location is free and a read between locations is billed ([BigQuery pricing](https://cloud.google.com/bigquery/pricing)).
- **BigQuery: what you configure.** An identity with the role `bigquery.readSessionUser` ([BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage)).
- **BigQuery: how you check.** This page's reading: to compare the location of the dataset with the location of the cluster.
- **BigQuery: cost and what must move.** Streaming reads cost $1.10 per TiB, and an export to Cloud Storage lets pods read files instead ([BigQuery pricing](https://cloud.google.com/bigquery/pricing)).
- **Snowflake: class.** A managed service ([Snowflake key concepts](https://docs.snowflake.com/en/user-guide/intro-key-concepts)).
- **Snowflake: what you request.** Reader pods that run the Python connector. The warehouse is Snowflake's own.
- **Snowflake: where pods land.** Not known, because no page read says where reader pods should run.
- **Snowflake: what you configure.** A Snowflake user of type `SERVICE` with `WORKLOAD_IDENTITY`, and a projected token in the pod ([Snowflake workload identity federation](https://docs.snowflake.com/en/user-guide/workload-identity-federation)).
- **Snowflake: how you check.** This page's reading: that the token file exists at `token_file_path` and that the `SUBJECT` of the Snowflake user names the pod's namespace and service account.
- **Snowflake: cost and what must move.** An X-Small warehouse uses 1 credit per hour, and the price of a credit was not read ([Snowflake warehouses](https://docs.snowflake.com/en/user-guide/warehouses-overview)).
- **Snowflake: a way out.** An unload to files lets pods read files instead.
- **Ray: class.** Both. KubeRay runs each Ray node as a pod, with a head pod and worker pods ([Ray on Kubernetes](https://docs.ray.io/en/latest/cluster/kubernetes/index.html)).
- **Ray: the custom resources.** KubeRay adds the custom resources `RayCluster`, `RayJob` and `RayService` ([Ray on Kubernetes](https://docs.ray.io/en/latest/cluster/kubernetes/index.html)).
- **Ray: the managed version.** Anyscale is the managed Ray platform.
- **Ray: KubeRay in this blog.** [How a Pod finds its scheduler](#d02-1) in Deep dive 3 explains KubeRay.
- **Data lake: class.** Both. A lake is files in a bucket and engines in your pods.
- **Data lake: what you request.** Pods for the engines.
- **Data lake: where pods land.** This page's reading: the same region as the bucket, because reads from another region cost transfer fees.
- **Data lake: what you configure.** This page's reading: read-only access for training pods and one write path for each table.
- **Data lake: how you check.** The route table and the access rules of the pods' subnet and role (this page's reading).
- **Data lake: cost.** Reads from the bucket.
- **Data warehouse: class.** A managed service. A data warehouse such as BigQuery or Snowflake is managed only, and the BigQuery and Snowflake entries cover it.
- **Parquet: class.** This page's reading: both. Files in object storage that you connect to, plus reader pods or loader workers.
- **Parquet: what you request.** Reader pods or loader workers. The number of files and row groups limits how many can work without overlap (this page's reading).
- **Parquet: where pods land.** The same region as the bucket (this page's reading).
- **Parquet: what you configure.** Read permission on the data folder.
- **Parquet: how you check.** Count the files and the row groups of the table (this page's reading).
- **Parquet: cost.** Reads from the bucket.
- **Parquet: no server.** Parquet itself runs no server of its own ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).
- **Iceberg: class.** This page's reading: both. Files in object storage that you connect to, plus pods for clean-up jobs and a catalog to reach.
- **Iceberg: what you request.** Pods for clean-up jobs, which are compaction and snapshot expiry. Compaction rewrites many small files as fewer large files.
- **Iceberg: where pods land.** The same region as the bucket (this page's reading).
- **Iceberg: what you configure.** A catalog setting and an IAM permission (this page's reading), read permission for training pods, write permission for the clean-up jobs.
- **Iceberg: how you check.** For a Flink writer, the commit-age metric.
- **Iceberg: cost.** Old files stay in the bucket until snapshots are expired ([Iceberg maintenance](https://iceberg.apache.org/docs/latest/maintenance/)).
- **Iceberg with a Flink writer.** If a Flink job writes the table, keep the last snapshot that the Flink job created, and delete only orphan files that are old enough, or the job's state can be corrupted ([Iceberg Flink writes](https://iceberg.apache.org/docs/latest/flink-writes/)).
- **Delta Lake: class.** This page's reading: both. Files in object storage that you connect to, plus pods for the clean-up job.
- **Delta Lake: what you request.** Pods for `vacuum`, the clean-up job.
- **Delta Lake: where pods land.** The same region as the bucket (this page's reading).
- **Delta Lake: what you configure.** Read permission on the table folder, and write permission for the clean-up job.
- **Delta Lake: how you check.** Not known, because no metric for Delta was read.
- **Delta Lake: cost.** Old files stay in the bucket until `vacuum` removes them after the retention period, 7 days by default ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)).

### Appendix B. Parquet in detail

**This appendix holds the Parquet file layout.** None of it changes what you request or configure, so none of it is in the main body. [Lakes, warehouses and table formats](#d18-5) has the one sentence that matters on Kubernetes.

**Going deeper, by topic.**

- **The layout.** A file starts with the 4-byte magic number `PAR1`. Then come the column chunks of the first row group, then those of the next row group, and so on. After the data come the file metadata, a 4-byte length of the metadata, and `PAR1` again ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).


![Parquet file layout](diagrams/extra/d18-parquet-layout.svg)

- **Why the metadata is last.** The format page says the file metadata is written after the data to allow single-pass writing. Readers first read the metadata to find the column chunks they want, and then read those chunks one after the other ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).
- **Row group, column chunk and page.** A row group is a horizontal slice of the rows, and the format guarantees no physical structure for it. It holds one column chunk for each column. A column chunk is contiguous in the file, and it is divided into pages ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).
- **Units of parallelization.** The format page lists the file or row group for MapReduce, an older way to process data in parallel, the column chunk for I/O, and the page for encoding and compression ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).
- **The size advice is written for HDFS.** The page recommends large row groups of 512 MB to 1 GB. Its optimized read setup is 1 GB row groups, a 1 GB HDFS block size and one HDFS block for each file. It recommends 8 KB for page sizes. HDFS is the Hadoop distributed file system. This advice was written for HDFS, and no source read gives a size for object storage ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).
- **Skipping.** Readers can skip pages they do not need, and files can carry an optional column index that helps skip pages ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).

### Appendix C. Iceberg and Delta Lake in detail

**This appendix holds the metadata structure of the two table formats.** The main body keeps only what changes a Kubernetes job: snapshots, the catalog, and clean-up.

**Going deeper, by topic.**

- **Iceberg: the layers.** Table state lives in metadata files. Every change creates a new metadata file and replaces the old one with an atomic swap. A snapshot is the state of the table at one moment. In versions 1 to 3, the manifests of a snapshot are listed in a manifest list. Version 4 replaces the manifest list with a root manifest. Each manifest has a row for each data file ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Iceberg: concurrency.** A writer creates the new metadata file on the assumption that the table has not changed. It commits by swapping the pointer, and if the table moved, it retries on the new version. Each successful commit gets a sequence number, which is a counter that grows with each commit and tells how old data and delete files are ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Iceberg: row-level deletes.** The current way to record deleted rows is a deletion vector, from version 3 on. It is a compact list of the deleted row positions in one file ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Iceberg: what the file system must do.** The Iceberg specification asks only that a file system support in-place writes, seekable reads and deletes. It says these are compatible with object stores such as S3 ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Iceberg: relative locations.** Version 4 adds relative locations in metadata, so a table can move without rewriting its metadata files ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Delta Lake: the log.** The `_delta_log` folder holds one JSON file for each commit, named with a zero-padded version number, such as `00000000000000000042.json`. Checkpoint files such as `00000000000000000042.checkpoint.parquet` sit in the same folder, and a `_last_checkpoint` file points to the latest one ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)).
- **Delta Lake: ACID.** The Delta protocol describes how to bring ACID properties to files in a file system or object store ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)). ACID means a change is applied completely or not at all, and readers never see half of it.
- **Delta Lake: concurrency.** Delta uses multi-version concurrency control, which means that a writer adds a new version and does not change the files that readers use. Old files stay until `vacuum` removes them. A table has a single serial history of atomic versions, numbered with whole numbers ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)).
- **Delta Lake: readers and time travel.** Delta adds snapshot isolation for reads, which means that a reader sees one consistent version while writers commit ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)). Time travel means reading a table as it was at an earlier version. Delta's introduction lists it for reproducible machine learning experiments ([Delta intro](https://docs.delta.io/latest/delta-intro.html)). No Delta syntax for reading a past version was read for this page.
- **Delta Lake: catalog-managed commits.** The protocol describes catalog-managed tables, in which a catalog ratifies a commit. It says a catalog can use PUT-if-absent, a write that succeeds only if no object with that name exists yet, to do so ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)). No Delta page read covers Delta's multi-writer needs on S3.
- **Side by side.** The table sums up the two formats as the sources describe them.

| | Iceberg | Delta Lake |
|---|---|---|
| Where the table state lives | metadata files, swapped atomically | the `_delta_log` folder |
| Unit of a change | a snapshot | a version (one JSON file) |
| Readers | use the snapshot current at load time, no locks | read a consistent snapshot |
| Clean-up of old files | expire snapshots, remove orphan files | `vacuum` after a retention period (default 7 days) |
| Catalog | Iceberg's catalogs load tables by name. The pointer to the current metadata file lives in a metastore or a database, or in a file system with atomic rename (a deprecated scheme) | not needed to read, because the table is self-describing |

### Appendix D. Lake, warehouse and lakehouse

**This appendix holds the definitions and one team's argument.** The words matter for conversations with data teams. They do not change a Kubernetes object.

**Going deeper, by topic.**

- **Data lake.** AWS describes it as a centralized repository for structured and unstructured data at any scale. You can store data as it is, without first giving it a structure. The schema is not defined when the data is captured ([AWS data lake](https://aws.amazon.com/what-is/data-lake/)).
- **Data warehouse.** AWS describes it as a central repository of information to analyze. Data flows in from transactional systems and other sources. The structure and schema are defined in advance, to make SQL queries fast ([AWS data warehouse](https://aws.amazon.com/what-is/data-warehouse/), [AWS data lake](https://aws.amazon.com/what-is/data-lake/)).
- **Lakehouse.** A 2021 paper by Armbrust, Ghodsi, Xin and Zaharia, all from Databricks, argues for this pattern ([lakehouse paper](https://www.cidrdb.org/cidr2021/papers/cidr2021_paper17.pdf)). It says that open formats such as Parquet plus a metadata layer can serve both warehouse and machine learning uses. It also says that none of the leading machine learning systems, such as TensorFlow, PyTorch and XGBoost, work well on top of warehouses, and that reading data through ODBC or JDBC is inefficient. It is a Databricks-authored argument, not a neutral survey.
- **The paper's four problems.** It names these problems of a two-tier architecture, where a data lake feeds a separate warehouse: reliability, data staleness, limited support for advanced analytics, and total cost of ownership ([lakehouse paper](https://www.cidrdb.org/cidr2021/papers/cidr2021_paper17.pdf)).

### Appendix E. Prices and limits, with the date each was read

**This appendix lists every price and limit on this page.** Prices change, so each row names its source and the date it was read. A price marked "not read" was not found in the pages read. The three EC2 rows come from a copy of the AWS EC2 pricing page that was saved on 2026-11-20. A fetch on 2026-11-22 returned that page without the price rows, so those rows were not re-checked. Every other row was read on 2026-11-22.

**Going deeper, by topic.**

- **Prices in one table.** The table has the value and the source for each row.

| Item | Value | Date read | Source |
|---|---|---|---|
| EC2 data between zones in one Region | $0.01 per GB in each direction | 2026-11-20, not re-checked | [AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/) |
| EC2 data within one zone | free | 2026-11-20, not re-checked | [AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/) |
| S3 and other listed services to EC2 in one Region | free, but PrivateLink, NAT Gateway and Transit Gateway in the path are charged | 2026-11-20, not re-checked | [AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/) |
| NAT gateway, US East (Ohio) example | $0.045 per hour and $0.045 per GB processed | 2026-11-22 | [AWS VPC pricing](https://aws.amazon.com/vpc/pricing/) |
| Gateway endpoint for S3 or DynamoDB | no additional charge | 2026-11-22 | [AWS gateway endpoints](https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html) |
| Pub/Sub throughput | first 10 GiB a month free, then $40 per TiB | 2026-11-22 | [Pub/Sub pricing](https://cloud.google.com/pubsub/pricing) |
| Pub/Sub BigQuery and Cloud Storage subscriptions | $50 per TiB | 2026-11-22 | [Pub/Sub pricing](https://cloud.google.com/pubsub/pricing) |
| Pub/Sub zone data transfer | no fees | 2026-11-22 | [Pub/Sub pricing](https://cloud.google.com/pubsub/pricing) |
| BigQuery Storage Read API reads | $1.10 per TiB; the first 300 TiB a month free | 2026-11-22 | [BigQuery pricing](https://cloud.google.com/bigquery/pricing) |
| BigQuery read inside one location | free | 2026-11-22 | [BigQuery pricing](https://cloud.google.com/bigquery/pricing) |
| BigQuery read between locations on one continent | $0.01 per GiB, unless a free case applies | 2026-11-22 | [BigQuery pricing](https://cloud.google.com/bigquery/pricing) |
| BigQuery read between continents | $0.08 per GiB; $0.15 per GiB when Australia is one end | 2026-11-22 | [BigQuery pricing](https://cloud.google.com/bigquery/pricing) |
| BigQuery batch export | free in the shared slot pool, up to 50 TiB a day | 2026-11-22 | [BigQuery pricing](https://cloud.google.com/bigquery/pricing) |
| BigQuery on-demand queries | the first 1 TiB a month free; then $6.25 per TiB a month for each account | 2026-11-22 | [BigQuery pricing](https://cloud.google.com/bigquery/pricing) |
| Snowflake X-Small warehouse (first generation) | 1 credit per hour; per-second billing, 60-second minimum each start | 2026-11-22 | [Snowflake warehouses](https://docs.snowflake.com/en/user-guide/warehouses-overview) |
| BigQuery Storage Read API general network usage, row "Data Transfer to Worldwide destinations (excluding Asia and Australia)" | $0.12 per GiB for the first TiB a month for each account. The saved text does not say which reader locations the row covers, so this page does not interpret it | 2026-11-22 | [BigQuery pricing](https://cloud.google.com/bigquery/pricing) |

- **Limits and gaps.** The table lists limits that were read, and prices that were not read.

| Item | Value | Date read | Source |
|---|---|---|---|
| EKS Pod Identity associations | up to 5,000 for each cluster | 2026-11-22 | [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html) |
| BigQuery read session | lasts at least 6 hours | 2026-11-22 | [BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage) |
| BigQuery read stream | closed when it gets no read request for more than 1 hour | 2026-11-22 | [BigQuery Storage Read API](https://cloud.google.com/bigquery/docs/reference/storage) |
| BigQuery export file | at most 1 GB of logical table data in one file | 2026-11-22 | [BigQuery export](https://cloud.google.com/bigquery/docs/exporting-data) |
| Snowflake price of a credit; Snowflake data-egress price | not read | not applicable | not known |
| MSK and Google managed Kafka prices | not read | not applicable | not known |
| AWS price for a pod on EKS to read from BigQuery or Snowflake | not read | not applicable | not known |

### Appendix F. Managed counterparts in one list

**This appendix lists the managed version of each tool, and what stays in your cluster.** One point for each.

**Going deeper, by topic.**

- **Amazon EMR on EKS.** Managed Spark that runs on your EKS cluster. A virtual cluster is one namespace of your cluster. This page's reading: your nodes and pods stay in your cluster ([EMR on EKS](https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/how-it-works.html)).
- **Amazon Managed Service for Apache Flink.** Runs Flink applications for you. This page's reading: none of its Flink pods are in your cluster ([AWS](https://docs.aws.amazon.com/managed-flink/latest/java/what-is.html)).
- **Dataflow.** Google's managed runner for Beam. It allocates worker VMs and deletes them when the job ends ([Dataflow](https://cloud.google.com/dataflow/docs/overview)).
- **Amazon MWAA and Google Managed Airflow.** Managed Airflow on AWS and on Google Cloud. Google's page says Cloud Composer is being renamed ([MWAA](https://docs.aws.amazon.com/mwaa/latest/userguide/what-is-mwaa.html), [Google Managed Airflow](https://cloud.google.com/composer/docs/composer-3/composer-overview)).
- **Dagster+ Hybrid.** Dagster+ runs the control plane. An agent that you run starts your code in your containers ([Dagster+ Hybrid](https://docs.dagster.io/deployment/dagster-plus/hybrid)).
- **Amazon MSK and Google Cloud Managed Service for Apache Kafka.** Managed Kafka. Your clients stay in your cluster ([Amazon MSK](https://docs.aws.amazon.com/msk/latest/developerguide/what-is-msk.html), [Google Cloud Managed Service for Apache Kafka](https://cloud.google.com/managed-service-for-apache-kafka/docs/overview)).
- **Google Cloud Managed Service for Apache Kafka: tiered storage.** The service uses tiered storage. It combines a limited amount of fast local disk on the brokers with remote storage that costs less and needs no pre-provisioning ([Google Cloud Managed Service for Apache Kafka](https://cloud.google.com/managed-service-for-apache-kafka/docs/overview)).
- **Confluent for Kubernetes and Confluent Cloud.** The operator manages self-managed Confluent Platform in your cluster. It does not manage Confluent Cloud ([Confluent for Kubernetes](https://docs.confluent.io/operator/current/overview.html)).
- **Snowpark Container Services.** Runs your containers on Snowflake's nodes, in compute pools ([Snowpark Container Services](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/overview)).
- **Anyscale.** The managed Ray platform. Its page says it can deploy Ray clusters on your existing Kubernetes infrastructure ([Ray on Kubernetes](https://docs.ray.io/en/latest/cluster/kubernetes/index.html)).

### Appendix G. Words

**This appendix explains words that mean different things in different places.** Each point is one word.

**Going deeper, by topic.**

- **Snapshot.** An Iceberg snapshot is the state of a table at one moment ([Iceberg spec](https://iceberg.apache.org/spec/)). A BigQuery table snapshot is a read-only, point-in-time copy of a table ([BigQuery storage overview](https://cloud.google.com/bigquery/docs/storage_overview)). A volume snapshot is a copy of a storage volume. They are three different things.
- **Partition.** A Kafka partition is a slice of a topic. A table partition is a way to divide a table by column values. A folder partition is a folder named after a column value, and PyArrow understands such folders ([PyArrow datasets](https://arrow.apache.org/docs/python/dataset.html)). These are three different things.
- **Checkpoint.** A Flink checkpoint is a saved copy of a job's state ([Flink checkpointing](https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/)). A training checkpoint is a saved copy of a model's weights. A Delta checkpoint file is a summary of the log ([Delta protocol](https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md)). An Iceberg commit is a new metadata pointer. These are different things.
- **Operator, custom resource and control loop.** An operator works through custom resources. A custom resource is an object type that you add to the Kubernetes API, for example a `SparkApplication`. Operators follow the control loop, which is the Kubernetes habit of comparing the wanted state with the real state and fixing the difference ([Kubernetes operator pattern](https://kubernetes.io/docs/concepts/extend-kubernetes/operator/)).
- **Celery.** Celery is a Python system that hands tasks to long-running worker processes. Airflow's Celery executor uses it ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Redis.** Redis is an in-memory data store. The Airflow page names it as an additional component that the Celery executor needs and the Kubernetes executor does not ([Airflow Kubernetes executor](https://airflow.apache.org/docs/apache-airflow-providers-cncf-kubernetes/stable/kubernetes_executor.html)).
- **Catalog.** In Iceberg, a catalog lets SQL commands manage tables and load them by name ([Iceberg Spark getting started](https://iceberg.apache.org/docs/latest/spark-getting-started/)). The Iceberg spec describes where the pointer to the current metadata file lives: in a file system with atomic rename, or in a metastore or a database ([Iceberg spec](https://iceberg.apache.org/spec/)).
- **Shard.** One slice of a dataset that one reader handles.
- **Consumer group and offset.** A consumer group is a set of consumers that share a topic. An offset is a message's position in a partition. KEDA scales on the lag, which is the number of messages that a group has not read yet ([KEDA Kafka scaler](https://keda.sh/docs/latest/scalers/apache-kafka/)).
- **Row group.** A horizontal slice of a Parquet file ([Parquet format](https://raw.githubusercontent.com/apache/parquet-format/master/README.md)).
- **Words and units.** A GiB is 1,024 x 1,024 x 1,024 bytes, and a TiB is 1,024 GiB. BigQuery and AWS price in GiB and TiB in some places and in GB in others, so read the unit before you compare two prices.

### Appendix H. Calls and options for reading data

**This appendix holds the calls and options for reading data.** None of them changes what you request or configure on Kubernetes, so none is in the main body. Each point is one call or one option.

**Going deeper, by topic.**

- **BigQuery: export formats.** Export writes CSV, JSON, Avro or Parquet. A single file holds at most 1 GB of logical table data, so you must use a wildcard to get many files ([BigQuery export](https://cloud.google.com/bigquery/docs/exporting-data)).
- **Snowflake: Python connector batches.** `fetch_pandas_batches` returns batches as pandas data frames, and `fetch_arrow_batches` returns them as PyArrow tables ([Snowflake connector API](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api)).
- **Snowflake: the PyArrow version.** The pandas methods of the Python connector need PyArrow 14.0.1 or later ([Snowflake pandas](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-pandas)).
- **Snowflake: the Spark connector.** The Spark connector moves data both ways, and the Spark cluster can be self-hosted ([Snowflake Spark connector](https://docs.snowflake.com/en/user-guide/spark-connector-overview)).
- **Snowflake: unload to files.** `COPY INTO <location>` unloads a table or a query to files, with the options `PARTITION BY` and `MAX_FILE_SIZE` ([Snowflake unload](https://docs.snowflake.com/en/user-guide/data-unload-overview)).
- **PyArrow datasets.** They read Parquet with column selection and row filters. They also understand folders that are partitions of the data ([PyArrow datasets](https://arrow.apache.org/docs/python/dataset.html)).
- **PyIceberg.** It scans a table with `row_filter` and `selected_fields` and returns Arrow data ([PyIceberg](https://py.iceberg.apache.org/api/)).
- **delta-rs.** The `deltalake` package reads Delta tables ([delta-rs](https://raw.githubusercontent.com/delta-io/delta-rs/main/README.md)).
- **Ray Data.** It reads Parquet and Iceberg tables. It reads BigQuery with `read_bigquery`, and it reads Snowflake with `read_sql` and the Snowflake Python connector ([Ray loading data](https://docs.ray.io/en/latest/data/loading-data.html)). The Ray loading page read does not say whether Ray Data reads Delta tables.
- **Hugging Face: streaming and shards.** With `streaming=True`, Hugging Face reads a dataset while you iterate over it. `to_iterable_dataset(num_shards=64)` splits a dataset into shards, and a PyTorch `DataLoader` with 4 workers then gets 16 shards for each worker. For a Parquet dataset with few files, `reshard()` splits by row group instead of by file ([Hugging Face streaming](https://huggingface.co/docs/datasets/stream)).
- **Kubeflow Trainer: the data cache.** The distributed data cache is built on Apache Arrow and Apache DataFusion. It streams tensors to GPU nodes and moves preprocessing to CPU workloads. A tensor is a block of numbers that a model takes in. Apache Arrow is a format for tables in memory. Apache DataFusion is a query engine ([Kubeflow Trainer](https://www.kubeflow.org/docs/components/trainer/overview/)).
- **PyTorch DataLoader: pinned memory.** `pin_memory=True` puts batches in pinned memory for a faster copy to the GPU. Pinned memory is a kind of CPU memory that makes that copy faster ([PyTorch data](https://docs.pytorch.org/docs/2.15/data.html)).

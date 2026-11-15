---
id: appendix-second-scheduler
title: "Appendix A: Running a second scheduler on a managed cluster"
short: "A. Running a second scheduler"
---
This appendix is a complete, ready-to-apply example for one specific situation: you want different scheduling rules for *some* of your workloads (different plugin weights, or bin-packing instead of spreading), but you're on a managed cluster such as EKS, GKE or AKS, where you can't change the default scheduler's configuration.

The answer is to run a **second scheduler** next to the default one. It's the same official kube-scheduler program, deployed by you as an ordinary Deployment, with its own `KubeSchedulerConfiguration`. Pods choose it by setting `schedulerName`; every other pod keeps using the default scheduler. For background, see [Phase 1: filtering](#d01-1) (the note on `KubeSchedulerConfiguration`) and [Built-in plugins, and what a second scheduler changes](#d01-9).

### What the example contains

1. **Identity:** a `my-scheduler` ServiceAccount, bound to the built-in `system:kube-scheduler` and `system:volume-scheduler` ClusterRoles (so it can read pods and nodes, bind pods and handle volumes), plus a role to read the API server's authentication settings. You reuse the standard permissions instead of writing your own.
2. **Configuration:** a ConfigMap holding the scheduler's `KubeSchedulerConfiguration`. It defines one profile, `affinity-first-scheduler`, that weights InterPodAffinity at 100 and ImageLocality at 1, and switches NodeResourcesFit to MostAllocated (bin-packing). Leader election is on.
3. **The scheduler itself:** a Deployment with 2 replicas in `kube-system`. Thanks to leader election only one replica is active at a time; the other takes over if it fails. It runs the official `kube-scheduler` image with `--config` pointing at the mounted ConfigMap.
4. **A workload that opts in:** an `api` Deployment that sets `schedulerName: affinity-first-scheduler`.

The highlighted lines are the ones that matter. Set the image tag to your cluster's Kubernetes version (`v1.35.0` is a placeholder).

```yaml
# 1. Identity: a ServiceAccount that reuses the built-in scheduler permissions
apiVersion: v1
kind: ServiceAccount
metadata:
  name: my-scheduler
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: my-scheduler-as-kube-scheduler
subjects:
- kind: ServiceAccount
  name: my-scheduler
  namespace: kube-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:kube-scheduler        # read pods/nodes, bind pods #!
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: my-scheduler-as-volume-scheduler
subjects:
- kind: ServiceAccount
  name: my-scheduler
  namespace: kube-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:volume-scheduler      # needed for pods with PVCs #!
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: my-scheduler-auth-reader
  namespace: kube-system
subjects:
- kind: ServiceAccount
  name: my-scheduler
  namespace: kube-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: extension-apiserver-authentication-reader
---
# 2. The scheduler's own config, stored in a ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-scheduler-config #!
  namespace: kube-system
data:
  config.yaml: |
    apiVersion: kubescheduler.config.k8s.io/v1
    kind: KubeSchedulerConfiguration
    leaderElection:
      leaderElect: true                # 2 replicas, only one active #!
      resourceNamespace: kube-system
      resourceName: affinity-first-scheduler
    profiles:
    - schedulerName: affinity-first-scheduler #!
      plugins:
        score:
          enabled:
          - name: InterPodAffinity
            weight: 100
          - name: ImageLocality
            weight: 1
      pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: MostAllocated        # bin-pack instead of spread #!
            resources:
            - name: cpu
              weight: 1
            - name: memory
              weight: 1
---
# 3. Run it as an ordinary Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-scheduler
  namespace: kube-system
spec:
  replicas: 2 #!
  selector:
    matchLabels: {component: my-scheduler}
  template:
    metadata:
      labels: {component: my-scheduler}
    spec:
      serviceAccountName: my-scheduler
      priorityClassName: system-cluster-critical
      containers:
      - name: kube-scheduler
        image: registry.k8s.io/kube-scheduler:v1.35.0   # match your cluster's version
        command:
        - kube-scheduler
        - --config=/etc/kubernetes/my-scheduler/config.yaml #!
        livenessProbe:
          httpGet: {path: /healthz, port: 10259, scheme: HTTPS}
          initialDelaySeconds: 15
        readinessProbe:
          httpGet: {path: /healthz, port: 10259, scheme: HTTPS}
        resources:
          requests: {cpu: 100m, memory: 128Mi}
        volumeMounts:
        - name: config
          mountPath: /etc/kubernetes/my-scheduler
          readOnly: true
      volumes:
      - name: config
        configMap:
          name: my-scheduler-config #!
---
# 4. Opt in per workload; every other pod keeps the default scheduler
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 3
  selector:
    matchLabels: {app: api}
  template:
    metadata:
      labels: {app: api}
    spec:
      schedulerName: affinity-first-scheduler #!
      containers:
      - name: api
        image: my-company/api:1.4
```

### Checking that it works

- **The scheduler is running:** `kubectl -n kube-system get pods -l component=my-scheduler` shows two pods, and `kubectl -n kube-system get lease affinity-first-scheduler` shows which one is the leader.
- **Your pods used it:** `kubectl get events --field-selector reason=Scheduled` lists each placement with the scheduler that made it, so the `api` pods show `affinity-first-scheduler`.
- **A pod stays `Pending` with no events at all:** no scheduler has picked it up. Usually the `schedulerName` has a typo, or the scheduler's pods aren't running.

### Things to keep in mind

- **Two schedulers don't coordinate.** If both place pods on the same nodes, they can occasionally pick the same free space at the same time, and the kubelet rejects the second pod. For workloads that matter, give this scheduler its own node pool (taints and node labels).
- **Keep it in step with the cluster.** Upgrade the image when you upgrade the cluster, so the scheduler understands the same API fields as the control plane.
- **Autoscalers simulate the default scheduler.** Check that Cluster Autoscaler or Karpenter handles pods with a custom `schedulerName` the way you expect.

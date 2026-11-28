"""Kubernetes manifests for the experiments, built from plain parameters. Nothing here talks to the cluster."""
import json

NS = "serving"
VLLM_IMAGE = "vllm/vllm-openai:v0.33.0"
LOADGEN_IMAGE = "python:3.12-slim"
CODE_FILES = ["loadgen.py", "traffic.py", "store.py", "router.py", "quality.py"]
GPU_TOLERATION = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
LOADGEN_SETUP = "pip install -q aiohttp 'transformers>=4.50' tokenizers boto3 2>&1 | tail -1\n"


def dump(*docs) -> str:
    """JSON is valid YAML and keeps every string a string."""
    return "\n---\n".join(json.dumps(d, indent=1) for d in docs)


def vllm_server(name, model, args, replicas=1, gpus=1, env=None, memory_gi=None, extra_volumes=(), extra_mounts=(), labels=None, image=VLLM_IMAGE, command=None, pre="", node_pool="serving"):
    """A vLLM server as a Deployment and a Service. Each pod holds `gpus` GPUs and gets a proportional share of the node's CPUs and memory.
    The model cache is a directory on the node's disk, so a second pod on the same node loads from disk and not from the network."""
    cpu = 8 * gpus
    mem = memory_gi or 32 * gpus
    lab = {"app": name, "role": "experiment", **(labels or {})}
    e = {"HF_HOME": "/hf", "PYTHONUNBUFFERED": "1", "VLLM_LOGGING_LEVEL": "INFO", **(env or {})}
    container = {
        "name": "vllm", "image": image, "command": ["/bin/bash", "-c"],
        "args": [pre + "exec " + (command or "vllm serve") + " " + model + " " + " ".join(args) + " --port 8000 --served-model-name " + model.split("/")[-1] + " --host 0.0.0.0"],
        "env": [({"name": k, "valueFrom": v} if isinstance(v, dict) else {"name": k, "value": str(v)}) for k, v in e.items()],
        "ports": [{"containerPort": 8000}],
        "resources": {"limits": {"nvidia.com/gpu": gpus}, "requests": {"cpu": str(cpu), "memory": f"{mem}Gi"}},
        "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}, {"name": "hf", "mountPath": "/hf"}, *extra_mounts],
        "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}, "initialDelaySeconds": 20, "periodSeconds": 5, "failureThreshold": 600},
    }
    dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS, "labels": lab},
           "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}}, "strategy": {"type": "Recreate"},
                    "template": {"metadata": {"labels": lab, "annotations": {"prometheus.io/scrape": "true", "prometheus.io/port": "8000", "prometheus.io/path": "/metrics"}},
                                 "spec": {"nodeSelector": {"pool": node_pool}, "tolerations": GPU_TOLERATION, "terminationGracePeriodSeconds": 30, "containers": [container],
                                          "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "32Gi"}}, {"name": "hf", "hostPath": {"path": "/mnt/hf", "type": "DirectoryOrCreate"}}, *extra_volumes]}}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS, "labels": lab}, "spec": {"selector": {"app": name}, "ports": [{"port": 8000, "targetPort": 8000}]}}
    return dump(dep, svc)


def loadgen_job(run_id, env, bucket, deadline=7200, script="loadgen.py"):
    """The load generator as a Job on the CPU node reserved for it."""
    e = {"WORK_DIR": "/work", "STATE_BUCKET": bucket, "AWS_REGION": "us-east-1", "PYTHONUNBUFFERED": "1", "HF_HOME": "/work/hf", **env}
    container = {"name": "loadgen", "image": LOADGEN_IMAGE, "command": ["/bin/bash", "-c"], "args": [LOADGEN_SETUP + f"exec python /app/{script}"],
                 "env": [{"name": k, "value": str(v)} for k, v in e.items()], "resources": {"requests": {"cpu": "3", "memory": "6Gi"}},
                 "volumeMounts": [{"name": "app", "mountPath": "/app"}, {"name": "work", "mountPath": "/work"}]}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": f"lg-{run_id}", "namespace": NS, "labels": {"role": "experiment"}},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": deadline, "ttlSecondsAfterFinished": 86400,
                    "template": {"metadata": {"labels": {"app": "loadgen", "run": run_id}}, "spec": {"restartPolicy": "Never", "serviceAccountName": "tools", "nodeSelector": {"pool": "loadgen"},
                                                                                                    "containers": [container], "volumes": [{"name": "app", "configMap": {"name": "serving-app"}}, {"name": "work", "emptyDir": {}}]}}}}
    return dump(job)


def tool_pod(name, script, gpus=0, image=VLLM_IMAGE, env=None, role="experiment"):
    """A sleeping pod to run commands in (the first probes of every flag and every feature)."""
    e = {"HF_HOME": "/hf", "PYTHONUNBUFFERED": "1", **(env or {})}
    c = {"name": "tool", "image": image, "command": ["/bin/bash", "-c"], "args": [script + "\nsleep infinity"], "env": [{"name": k, "value": str(v)} for k, v in e.items()],
         "resources": {"limits": ({"nvidia.com/gpu": gpus} if gpus else {}), "requests": {"cpu": str(8 * gpus if gpus else 2), "memory": f"{32 * gpus if gpus else 6}Gi"}},
         "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}, {"name": "hf", "mountPath": "/hf"}]}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS, "labels": {"role": role, "app": name}},
           "spec": {"restartPolicy": "Never", "serviceAccountName": "tools", "nodeSelector": {"pool": "serving"}, "tolerations": GPU_TOLERATION, "containers": [c],
                    "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "32Gi"}}, {"name": "hf", "hostPath": {"path": "/mnt/hf", "type": "DirectoryOrCreate"}}]}}
    return dump(pod)


def router(name, env):
    """The request router as a Deployment and a Service on the load-generator node."""
    e = {"PYTHONUNBUFFERED": "1", **env}
    c = {"name": "router", "image": LOADGEN_IMAGE, "command": ["/bin/bash", "-c"], "args": ["pip install -q aiohttp 2>&1 | tail -1\nexec python /app/router.py"],
         "env": [{"name": k, "value": str(v)} for k, v in e.items()], "ports": [{"containerPort": 8000}], "resources": {"requests": {"cpu": "2", "memory": "2Gi"}},
         "readinessProbe": {"httpGet": {"path": "/router/stats", "port": 8000}, "initialDelaySeconds": 5, "periodSeconds": 3, "failureThreshold": 100},
         "volumeMounts": [{"name": "app", "mountPath": "/app"}]}
    lab = {"app": name, "role": "experiment"}
    dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS, "labels": lab},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}, "template": {"metadata": {"labels": lab},
                    "spec": {"nodeSelector": {"pool": "loadgen"}, "containers": [c], "volumes": [{"name": "app", "configMap": {"name": "serving-app"}}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS, "labels": lab}, "spec": {"selector": {"app": name}, "ports": [{"port": 8000, "targetPort": 8000}]}}
    return dump(dep, svc)


def headless(name, selector_app):
    """A headless Service: DNS answers with the addresses of the pods that are Ready, which is what the router re-reads to find new replicas."""
    return dump({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS, "labels": {"role": "experiment"}},
                 "spec": {"clusterIP": "None", "selector": {"app": selector_app}, "ports": [{"port": 8000, "targetPort": 8000}]}})


def scaled_object(name, target, query, threshold, max_replicas=4, min_replicas=1, down_window=120):
    """KEDA scales the Deployment on a Prometheus query: desired replicas = ceil(query value / threshold)."""
    so = {"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject", "metadata": {"name": name, "namespace": NS, "labels": {"role": "experiment"}},
          "spec": {"scaleTargetRef": {"name": target}, "minReplicaCount": min_replicas, "maxReplicaCount": max_replicas, "pollingInterval": 5,
                   "advanced": {"horizontalPodAutoscalerConfig": {"behavior": {"scaleDown": {"stabilizationWindowSeconds": down_window},
                                                                               "scaleUp": {"stabilizationWindowSeconds": 0, "policies": [{"type": "Pods", "value": max_replicas, "periodSeconds": 15}]}}}},
                   "triggers": [{"type": "prometheus", "metadata": {"serverAddress": "http://prometheus-server.monitoring.svc:80", "query": query, "threshold": str(threshold), "ignoreNullValues": "true"}}]}}
    return dump(so)

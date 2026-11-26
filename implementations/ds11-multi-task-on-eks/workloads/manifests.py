"""Kubernetes manifests for the experiments, built from plain parameters. The tests call these functions so that every experiment states, in one
place, what it runs: which recipe, how many cells, on which queue. Nothing here talks to the cluster.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
NS = "rl-research"
IMAGE = "vllm/vllm-openai:v0.33.0"
TRL_VERSION = "1.18.2"
TRANSFORMERS_VERSION = "5.24.0"
QUEUE = "research-small"

# The trainer is the vLLM image plus TRL. TRL goes in without its own dependencies (they would replace the image's torch and vLLM); the few
# it really needs are added one by one.
SETUP = f"""
set -e
pip install -q 'trl=={TRL_VERSION}' 'transformers=={TRANSFORMERS_VERSION}' --no-deps 2>&1 | tail -1
pip install -q accelerate datasets kernels math-verify latex2sympy2_extended boto3 pyyaml 2>&1 | tail -1
"""
GPU_TOLERATION = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
CODE_FILES = ["run.py", "families.py", "store.py"]


def dump(*docs) -> str:
    """JSON is valid YAML and keeps every string a string (a bare 3e-6 would be read as a number)."""
    return "\n---\n".join(json.dumps(d, indent=1) for d in docs)


def queues(gpus=4):
    """The research queue (Design 11's platform): one cluster-wide quota of GPUs, two priorities. Sweeps run at low priority, so a run somebody
    is waiting for is admitted first and a running sweep cell is never evicted for it (the preemption policy is Never)."""
    return dump(
        {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "ResourceFlavor", "metadata": {"name": "gpu"},
         "spec": {"nodeLabels": {"pool": "research"}}},
        {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "WorkloadPriorityClass", "metadata": {"name": "sweep-low"}, "value": 100},
        {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "WorkloadPriorityClass", "metadata": {"name": "interactive"}, "value": 1000},
        {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "ClusterQueue", "metadata": {"name": "research"},
         "spec": {"namespaceSelector": {}, "preemption": {"withinClusterQueue": "Never"},
                  "resourceGroups": [{"coveredResources": ["cpu", "memory", "nvidia.com/gpu"], "flavors": [{"name": "gpu", "resources": [
                      {"name": "cpu", "nominalQuota": "64"}, {"name": "memory", "nominalQuota": "512Gi"}, {"name": "nvidia.com/gpu", "nominalQuota": str(gpus)}]}]}]}},
        {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "LocalQueue", "metadata": {"name": QUEUE, "namespace": NS},
         "spec": {"clusterQueue": "research"}},
    )


def _env(extra):
    base = {"HF_HOME": "/work/hf", "PYTHONUNBUFFERED": "1", "WORK_DIR": "/work", "AWS_REGION": "us-east-1", "VLLM_LOGGING_LEVEL": "ERROR"}
    return [{"name": k, "value": str(v)} for k, v in {**base, **extra}.items()]


def _pod(container, extra_volumes=(), node_type=None):
    return {
        "restartPolicy": "Never", "serviceAccountName": "trainer",
        "nodeSelector": {"pool": "research", **({"node.kubernetes.io/instance-type": node_type} if node_type else {})}, "tolerations": GPU_TOLERATION,
        "containers": [container],
        "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}}, {"name": "work", "emptyDir": {}},
                    {"name": "app", "configMap": {"name": "run-app"}}, {"name": "recipes", "configMap": {"name": "recipes"}}, *extra_volumes],
    }


def _container(name, script, env):
    return {
        "name": name, "image": IMAGE, "command": ["/bin/bash", "-c"], "args": [SETUP + script], "env": _env(env),
        "resources": {"limits": {"nvidia.com/gpu": 1}, "requests": {"cpu": "6", "memory": "24Gi"}},
        "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}, {"name": "app", "mountPath": "/app"}, {"name": "work", "mountPath": "/work"},
                         {"name": "recipes", "mountPath": "/recipes"}],
    }


def dev_pod(bucket, name="dev"):
    """A GPU pod that sleeps, with the code mounted: for trying a recipe by hand while the code is being written. The tests never use it."""
    c = _container("dev", "\nsleep infinity\n", {"STATE_BUCKET": bucket})
    return dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS, "labels": {"app": "dev"}}, "spec": _pod(c)})


def cell_job(name, cells, bucket, parallelism=4, priority="sweep-low", deadline=14400, queue=QUEUE, node_type=None):
    """A sweep as one Indexed Job: each index is one cell of the grid (a dict of environment variables for run.py), admitted by Kueue.
    `cells` is the whole grid; the pod with index i takes cells[i]. A cell that fails does not take the others with it."""
    script = "\nexec python3 /app/run.py\n"
    c = _container("cell", script, {"STATE_BUCKET": bucket, "GRID": json.dumps(cells), "SWEEP": name})
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": NS,
                     "labels": {"kueue.x-k8s.io/queue-name": queue, "kueue.x-k8s.io/priority-class": priority, "sweep": name}},
        "spec": {"completions": len(cells), "parallelism": min(parallelism, len(cells)), "completionMode": "Indexed", "backoffLimitPerIndex": 0,
                 "maxFailedIndexes": len(cells), "activeDeadlineSeconds": deadline, "ttlSecondsAfterFinished": 86400,
                 "template": {"metadata": {"labels": {"sweep": name, "app": "cell"}}, "spec": _pod(c, node_type=node_type)}},
    }
    return dump(job)


def pipeline_workflow(name, steps, bucket, namespace=NS):
    """An Argo Workflow whose steps are Kueue-managed Jobs (the design's pipeline: train specialists, merge, evaluate). `steps` is a list of
    {"name", "cell" (a dict of environment variables for run.py), "after" (names of steps it waits for)}. A step creates one Indexed Job of one cell and waits for it."""
    templates, tasks = [], []
    for st in steps:
        job = json.loads(cell_job(f"{name}-{st['name']}"[:60], [st["cell"]], bucket, parallelism=1))
        text = json.dumps(job)
        assert "{{" not in text, "a workflow would read this as a parameter"
        templates.append({"name": st["name"], "resource": {"action": "create", "successCondition": "status.succeeded > 0", "failureCondition": "status.failed > 0", "manifest": text}})
        tasks.append({"name": st["name"], "template": st["name"], **({"dependencies": st["after"]} if st.get("after") else {})})
    wf = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow", "metadata": {"name": name, "namespace": namespace},
          "spec": {"serviceAccountName": "argo-runner", "entrypoint": "pipeline", "templates": [{"name": "pipeline", "dag": {"tasks": tasks}}] + templates}}
    return dump(wf)

"""Kubernetes manifests for the experiments, built from plain parameters.

The tests call these functions so that every experiment states, in one place, what it runs: which model, which
recipe, which rollout layout. Nothing here talks to the cluster.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
NS = "rl-scale"
VLLM_IMAGE = "vllm/vllm-openai:v0.33.0"
TRL_VERSION = "1.18.2"
TRANSFORMERS_VERSION = "5.24.0"
# The trainer is the vLLM image plus TRL. pip would otherwise replace the image's NCCL with the one torch pins,
# and the trainer and the rollout engines must speak the same NCCL version or the weight-transfer group cannot form.
TRAINER_SETUP = f"""
set -e
pip install -q 'trl=={TRL_VERSION}' 'transformers=={TRANSFORMERS_VERSION}' --no-deps 2>&1 | tail -1
pip install -q accelerate datasets kernels math-verify latex2sympy2_extended boto3 redis 2>&1 | tail -1
pip install -q 'nvidia-nccl-cu13=={{NCCL}}' --no-deps 2>&1 | tail -1
"""
GPU_TOLERATION = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]


def dump(*docs) -> str:
    """JSON is valid YAML and keeps every string a string (a bare 3e-6 would be read as a number)."""
    return "\n---\n".join(json.dumps(d, indent=1) for d in docs)


def rollout_lws(model="Qwen/Qwen3-0.6B", engines=2, max_model_len=4096, gpu_util=0.7, extra_args=None, kv_cache_dtype=None,
                quantization=None, name="rollout", nccl_version="2.31.2", dtype=None):
    """The rollout fleet: one LeaderWorkerSet group of `engines` nodes, one data-parallel engine per node.

    The leader pod runs the API server and the first engine, the worker pods run the other engines headless.
    The trainer talks to the leader's address only; the weight-transfer group spans every engine.
    """
    args = ["--max-model-len", str(max_model_len), "--gpu-memory-utilization", str(gpu_util), "--logprobs-mode", "processed_logprobs",
            "--weight-transfer-config", json.dumps({"backend": "nccl"})]
    if kv_cache_dtype:
        args += ["--kv-cache-dtype", kv_cache_dtype]
    if quantization:
        args += ["--quantization", quantization]
    if dtype:
        args += ["--dtype", dtype]
    args += list(extra_args or [])
    dp = ["--data-parallel-size", str(engines), "--data-parallel-size-local", "1", "--data-parallel-address", "$(LWS_LEADER_ADDRESS)",
          "--data-parallel-rpc-port", "5555"] if engines > 1 else []
    env = [{"name": "VLLM_SERVER_DEV_MODE", "value": "1"}, {"name": "NCCL_SOCKET_IFNAME", "value": "eth0"},
           {"name": "NCCL_DEBUG", "value": "WARN"}]
    common = {
        "image": VLLM_IMAGE,
        "env": env,
        "resources": {"limits": {"nvidia.com/gpu": 1}, "requests": {"cpu": "3", "memory": "12Gi"}},
        "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}],
    }
    pod = {
        "nodeSelector": {"pool": "rollout"},
        "tolerations": GPU_TOLERATION,
        "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "4Gi"}}],
    }
    leader = {"metadata": {"labels": {"role": "rollout-leader"}}, "spec": {**pod, "containers": [{
        **common, "name": "vllm", "command": ["vllm", "serve", model],
        "args": args + dp,
        "ports": [{"containerPort": 8000}],
        "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}, "periodSeconds": 10, "timeoutSeconds": 10, "failureThreshold": 6},
    }]}}
    worker = {"spec": {**pod, "containers": [{
        **common, "name": "vllm-worker", "command": ["vllm", "serve", model],
        "args": args + ["--headless", "--data-parallel-size", str(engines), "--data-parallel-size-local", "1",
                        "--data-parallel-address", "$(LWS_LEADER_ADDRESS)", "--data-parallel-rpc-port", "5555",
                        "--data-parallel-start-rank", "$(LWS_WORKER_INDEX)"],
    }]}}
    lws = {
        "apiVersion": "leaderworkerset.x-k8s.io/v1", "kind": "LeaderWorkerSet",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"replicas": 1, "leaderWorkerTemplate": {
            "size": engines, "restartPolicy": "RecreateGroupOnPodRestart",
            "leaderTemplate": leader, "workerTemplate": worker}},
    }
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"{name}-api", "namespace": NS},
           "spec": {"selector": {"leaderworkerset.sigs.k8s.io/name": name, "role": "rollout-leader"},
                    "ports": [{"port": 8000, "targetPort": 8000}],
                    # The trainer must keep its connection to the one API server even while a busy engine misses a health probe.
                    "publishNotReadyAddresses": True}}
    if engines == 1:
        # A single engine does not need the data-parallel flags, and its worker template is unused.
        lws["spec"]["leaderWorkerTemplate"]["size"] = 1
    return dump(lws, svc)


def trainer_jobset(run_id, env, image=VLLM_IMAGE, bucket="", max_restarts=0, gpu=True, name=None, nccl_version="2.31.2"):
    """The trainer as a JobSet of one job with one pod: the pod's own restart policy is Never, the JobSet restarts it.

    A checkpoint on the node is copied to the bucket in the background; a new pod restores the newest one and goes on.
    """
    name = name or f"trainer-{run_id}"
    setup = TRAINER_SETUP.replace("{NCCL}", nccl_version)
    env_list = [{"name": k, "value": str(v)} for k, v in {**env, "RUN_ID": run_id, "STATE_BUCKET": bucket}.items()]
    env_list += [{"name": "NCCL_SOCKET_IFNAME", "value": "eth0"}, {"name": "NCCL_DEBUG", "value": "WARN"}, {"name": "HF_HOME", "value": "/work/hf"},
                 {"name": "PYTHONUNBUFFERED", "value": "1"}]
    container = {
        "name": "trainer", "image": image, "command": ["/bin/bash", "-c"],
        "args": [setup + "\nexec python3 /app/train.py\n"],
        "env": env_list,
        "resources": {"limits": {"nvidia.com/gpu": 1}, "requests": {"cpu": "6", "memory": "24Gi"}},
        "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}, {"name": "app", "mountPath": "/app"}, {"name": "work", "mountPath": "/work"}],
    }
    pod = {
        "restartPolicy": "Never", "serviceAccountName": "trainer", "nodeSelector": {"pool": "trainer"},
        "tolerations": GPU_TOLERATION, "containers": [container],
        "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}},
                    {"name": "work", "emptyDir": {}},
                    {"name": "app", "configMap": {"name": "trainer-app"}}],
    }
    js = {
        "apiVersion": "jobset.x-k8s.io/v1alpha2", "kind": "JobSet",
        "metadata": {"name": name, "namespace": NS, "labels": {"run": run_id}},
        "spec": {
            "failurePolicy": {"maxRestarts": max_restarts},
            "replicatedJobs": [{"name": "trainer", "replicas": 1, "template": {"spec": {
                "parallelism": 1, "completions": 1, "backoffLimit": 0,
                "template": {"metadata": {"labels": {"run": run_id, "app": "trainer"}}, "spec": pod}}}}],
        },
    }
    return dump(js)


def sandbox_pool(workers_min=2, workers_max=60, threshold=8, latency_ms=0, scaled=True, replicas=None):
    """The environment tier: stateless sandbox workers reading work from a Redis list, scaled by KEDA on the list's length."""
    deploy = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "sandbox-workers", "namespace": NS},
        "spec": {"replicas": replicas if replicas is not None else workers_min, "selector": {"matchLabels": {"app": "sandbox"}},
                 "template": {"metadata": {"labels": {"app": "sandbox"}}, "spec": {
                     "nodeSelector": {"pool": "env"},
                     "containers": [{"name": "worker", "image": "python:3.12-slim", "command": ["/bin/bash", "-c"],
                                     "args": ["pip install -q redis 2>&1 | tail -1; exec python3 /app/sandbox_worker.py"],
                                     "env": [{"name": "REDIS_HOST", "value": "redis"}, {"name": "TOOL_LATENCY_MS", "value": str(latency_ms)}],
                                     "resources": {"requests": {"cpu": "200m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                                     "volumeMounts": [{"name": "app", "mountPath": "/app"}]}],
                     "volumes": [{"name": "app", "configMap": {"name": "sandbox-app"}}]}}},
    }
    docs = [deploy]
    if scaled:
        docs.append({
            "apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject", "metadata": {"name": "sandbox-workers", "namespace": NS},
            "spec": {"scaleTargetRef": {"name": "sandbox-workers"}, "minReplicaCount": workers_min, "maxReplicaCount": workers_max,
                     "pollingInterval": 5, "cooldownPeriod": 60,
                     "triggers": [{"type": "redis", "metadata": {"address": "redis.rl-scale.svc:6379", "listName": "sandbox:queue",
                                                                  "listLength": str(threshold)}}]}})
    return dump(*docs)


def tool_job(name, script, env, nccl_version="2.31.2", gpu=True, deadline=1800):
    """A one-off job on the trainer node (a benchmark or a probe) with the same software as the trainer."""
    setup = TRAINER_SETUP.replace("{NCCL}", nccl_version)
    env_list = [{"name": k, "value": str(v)} for k, v in env.items()]
    env_list += [{"name": "NCCL_SOCKET_IFNAME", "value": "eth0"}, {"name": "NCCL_DEBUG", "value": "WARN"}, {"name": "HF_HOME", "value": "/work/hf"},
                 {"name": "PYTHONUNBUFFERED", "value": "1"}]
    container = {"name": "tool", "image": VLLM_IMAGE, "command": ["/bin/bash", "-c"], "args": [setup + f"\nexec python3 /app/{script}\n"], "env": env_list,
                 "resources": {"limits": {"nvidia.com/gpu": 1}, "requests": {"cpu": "6", "memory": "24Gi"}},
                 "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}, {"name": "app", "mountPath": "/app"}, {"name": "work", "mountPath": "/work"}]}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": NS},
           "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 3600, "activeDeadlineSeconds": deadline, "template": {"metadata": {"labels": {"tool": name}}, "spec": {
               "restartPolicy": "Never", "serviceAccountName": "tools", "nodeSelector": {"pool": "trainer"}, "tolerations": GPU_TOLERATION,
               "containers": [container],
               "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}}, {"name": "work", "emptyDir": {}},
                           {"name": "app", "configMap": {"name": "trainer-app"}}]}}}}
    return dump(job)


def reference_cronjob(env, bucket, schedule="0 3 * * *", nccl_version="2.31.2"):
    """The reference rung: a small, fixed run that is repeated on a schedule against the current code and images, so that a regression in the
    framework shows up here before it reaches a big run."""
    setup = TRAINER_SETUP.replace("{NCCL}", nccl_version)
    env_list = [{"name": k, "value": str(v)} for k, v in {**env, "STATE_BUCKET": bucket}.items()]
    env_list += [{"name": "NCCL_SOCKET_IFNAME", "value": "eth0"}, {"name": "HF_HOME", "value": "/work/hf"}, {"name": "PYTHONUNBUFFERED", "value": "1"}]
    container = {"name": "trainer", "image": VLLM_IMAGE, "command": ["/bin/bash", "-c"],
                 "args": [setup + "\nexport RUN_ID=reference-$(date +%m%d-%H%M)\nexec python3 /app/train.py\n"], "env": env_list,
                 "resources": {"limits": {"nvidia.com/gpu": 1}, "requests": {"cpu": "6", "memory": "24Gi"}},
                 "volumeMounts": [{"name": "shm", "mountPath": "/dev/shm"}, {"name": "app", "mountPath": "/app"}, {"name": "work", "mountPath": "/work"}]}
    cj = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "reference-rung", "namespace": NS},
          "spec": {"schedule": schedule, "concurrencyPolicy": "Forbid", "jobTemplate": {"spec": {"backoffLimit": 0, "template": {"metadata": {"labels": {"app": "trainer", "run": "reference"}}, "spec": {
              "restartPolicy": "Never", "serviceAccountName": "trainer", "nodeSelector": {"pool": "trainer"}, "tolerations": GPU_TOLERATION, "containers": [container],
              "volumes": [{"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}}, {"name": "work", "emptyDir": {}}, {"name": "app", "configMap": {"name": "trainer-app"}}]}}}}}}
    return dump(cj)

"""The Kubernetes Jobs of the environment fleet: primary rollouts and branch rollouts. Used by the launcher (on the
operator's machine) and by the credit worker (in the cluster), so both submit exactly the same shape.

Every environment pod: restricted Pod Security settings, no service-account token, no AWS identity, the code from a
ConfigMap, an emptyDir for the working copies, and a NetworkPolicy (Terraform) that lets it reach only the rollout
engine, the trajectory store, the verifier and DNS. Kueue admits them: primary rollouts at priority 1000, branch
rollouts at 100, so branches only fill capacity the primary rollouts are not using.
"""
import os

IMAGE = os.environ.get("ENV_IMAGE", "public.ecr.aws/docker/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b")
NS = "rl-envs"


def env_job(name, args, completions, parallelism, queue, priority, labels=None, max_turns=16):
    sec = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": NS, "labels": {"kueue.x-k8s.io/queue-name": queue, "kueue.x-k8s.io/priority-class": priority,
                                                               "ds08/job": name, **(labels or {})}},
        "spec": {
            "completions": completions, "parallelism": parallelism, "completionMode": "Indexed", "backoffLimit": 2,
            "ttlSecondsAfterFinished": 7200,
            "template": {"metadata": {"labels": {"ds08/job": name, "ds08/role": "env"}}, "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False, "nodeSelector": {"ds08/pool": "system"},
                "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "env", "image": IMAGE, "command": ["python", "-u", "/code/rollout.py", *args],
                    "env": [{"name": "ENV_IMAGE", "value": IMAGE}, {"name": "JOB_PARALLELISM", "value": str(parallelism)},
                            {"name": "MAX_TURNS", "value": str(max_turns)}, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                            {"name": "WORK_DIR", "value": "/work"}],
                    "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
                    "securityContext": sec,
                    "volumeMounts": [{"name": "code", "mountPath": "/code"}, {"name": "work", "mountPath": "/work"}, {"name": "tmp", "mountPath": "/tmp"}],
                }],
                "volumes": [{"name": "code", "configMap": {"name": "env-code"}}, {"name": "work", "emptyDir": {"sizeLimit": "1Gi"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}],
            }},
        },
    }


def rollout_job(name, run, tasks, seeds, parallelism, max_turns=16):
    return env_job(name, ["root", "--run", run, "--tasks", ",".join(tasks), "--seeds", ",".join(str(s) for s in seeds)],
                   parallelism, parallelism, "rl-rollouts", "primary", {"ds08/run": run}, max_turns)


def branch_job(name, run, traj, turn, k, max_turns=16):
    return env_job(name, ["branch", "--run", run, "--from", traj, "--turn", str(turn)], k, k, "rl-branches", "branch-low",
                   {"ds08/run": run, "ds08/kind": "branch"}, max_turns)

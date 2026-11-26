# ---- code and task data ---------------------------------------------------------------------------------------------
locals {
  code_files = fileset("${path.module}/../../workloads", "*.py")
  code       = { for f in local.code_files : f => file("${path.module}/../../workloads/${f}") }
  code_sha   = sha256(jsonencode(local.code))
  task_files = ["math_train.jsonl", "math_eval.jsonl", "math_perturbed.jsonl", "code_tasks.jsonl", "judge_tasks.jsonl"]
  store_url  = "http://store.platform.svc:8080"

  # One Deployment per pinned verifier version. Code verifiers that run model-written programs run under gVisor,
  # except the old code-tests@1.0.0, which is kept as it was deployed: ordinary runtime, so S06 can compare.
  verifiers = {
    "math-final-answer@1.0.0"     = { runtime = null, deps = false, judges = false, team = "rewards-team" }
    "math-final-answer@2.0.0"     = { runtime = null, deps = true, judges = false, team = "rewards-team" }
    "math-final-answer@2.1.0"     = { runtime = null, deps = true, judges = false, team = "rewards-team" }
    "strict-reference-math@1.0.0" = { runtime = null, deps = false, judges = false, team = "rewards-team" }
    "eval-math@1.0.0"             = { runtime = null, deps = false, judges = false, team = "eval-team" }
    "code-tests@1.0.0"            = { runtime = null, deps = false, judges = false, team = "rewards-team" }
    "code-tests@2.0.0"            = { runtime = "gvisor", deps = false, judges = false, team = "rewards-team" }
    "strict-reference-code@1.0.0" = { runtime = "gvisor", deps = false, judges = false, team = "rewards-team" }
    "judge-single@1.0.0"          = { runtime = null, deps = false, judges = true, team = "rewards-team" }
    "judge-pair@2.0.0"            = { runtime = null, deps = false, judges = true, team = "rewards-team" }
  }
  vname = { for k, v in local.verifiers : k => replace(replace(k, "@", "-"), ".", "-") }

  # Fetch the pinned wheels from the store and install them with --require-hashes; the pod has no other way out.
  deps_cmd = join(" && ", [
    "python -c \"import urllib.request as u; [open('/deps/'+w,'wb').write(u.urlopen('${local.store_url}/o/wheels/'+w).read()) for w in [${join(",", [for w in keys(var.wheels) : "'${w}'"])}]]\"",
    "printf '%b' '${join("\\n", [for w, h in var.wheels : "./${w} --hash=sha256:${h}"])}' > /deps/req.txt",
    "(cd /deps && pip install -q --no-index --no-deps --require-hashes --target /deps/site -r req.txt)",
  ])
}

resource "kubernetes_config_map_v1" "code" {
  for_each = toset(["rewards", "training", "platform"])
  metadata {
    name      = "code-${substr(local.code_sha, 0, 10)}"
    namespace = each.key
  }
  data       = local.code
  immutable  = true
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_config_map_v1" "tasks" {
  metadata {
    name      = "tasks"
    namespace = "rewards"
  }
  data       = { for f in local.task_files : f => file("${var.data_dir}/${f}") }
  depends_on = [kubernetes_namespace_v1.ns]
}

locals {
  restricted_pod = {
    runAsNonRoot   = true
    runAsUser      = 10001
    seccompProfile = { type = "RuntimeDefault" }
  }
  restricted_ctr = { runAsNonRoot = true, allowPrivilegeEscalation = false, capabilities = { drop = ["ALL"] } }
  ready_probe = {
    exec          = { command = ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"] }
    periodSeconds = 5
  }
}

# ---- the store ------------------------------------------------------------------------------------------------------
resource "kubectl_manifest" "store" {
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = "store", namespace = "platform" }
    spec = {
      replicas = 2
      selector = { matchLabels = { app = "store" } }
      template = {
        metadata = { labels = { app = "store" } }
        spec = {
          serviceAccountName = "store"
          nodeSelector       = { "ds10/pool" = "system" }
          securityContext    = local.restricted_pod
          containers = [{
            name            = "store"
            image           = var.python_image
            command         = ["bash", "-c", "pip install -q --no-cache-dir --target /tmp/site boto3==1.44.10 && PYTHONPATH=/tmp/site exec python -u /code/rstore.py"]
            env             = [{ name = "BUCKET", value = aws_s3_bucket.data.id }, { name = "AWS_REGION", value = var.region }, { name = "HOME", value = "/tmp" }]
            ports           = [{ containerPort = 8080 }]
            resources       = { requests = { cpu = "250m", memory = "256Mi" }, limits = { cpu = "1", memory = "1Gi" } }
            securityContext = local.restricted_ctr
            readinessProbe  = local.ready_probe
            volumeMounts    = [{ name = "code", mountPath = "/code" }]
          }]
          volumes = [{ name = "code", configMap = { name = kubernetes_config_map_v1.code["platform"].metadata[0].name } }]
        }
      }
    }
  })
  depends_on = [time_sleep.identity_ready]
}

resource "kubectl_manifest" "store_svc" {
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "Service"
    metadata   = { name = "store", namespace = "platform" }
    spec       = { selector = { app = "store" }, ports = [{ port = 8080, targetPort = 8080 }] }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- one Deployment per pinned verifier version -----------------------------------------------------------------------
resource "kubectl_manifest" "verifier" {
  for_each = local.verifiers
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata = {
      name      = local.vname[each.key]
      namespace = "rewards"
      labels    = { app = "verifier", verifier = split("@", each.key)[0], version = split("@", each.key)[1], team = each.value.team }
    }
    spec = {
      replicas = 2
      selector = { matchLabels = { app = "verifier", vid = local.vname[each.key] } }
      template = {
        metadata = {
          labels      = { app = "verifier", vid = local.vname[each.key], verifier = split("@", each.key)[0], version = split("@", each.key)[1], team = each.value.team }
          annotations = { "ds10/code-sha" = local.code_sha }
        }
        spec = merge({
          automountServiceAccountToken = false
          enableServiceLinks           = false
          securityContext              = local.restricted_pod
          containers = [{
            name    = "verifier"
            image   = var.python_image
            command = ["bash", "-c", "${each.value.deps ? "${local.deps_cmd} && " : ""}PYTHONPATH=/deps/site exec python -u /code/vserve.py"]
            env = [
              { name = "VERIFIER", value = each.key }, { name = "TASKS_DIR", value = "/tasks" }, { name = "HOME", value = "/tmp" },
              { name = "IMAGE", value = var.python_image }, { name = "JUDGE_A_URL", value = "http://judge-a.judges.svc:8000" },
              { name = "JUDGE_B_URL", value = "http://judge-b.judges.svc:8000" }, { name = "WORKERS", value = each.value.judges ? "8" : "4" },
            ]
            ports           = [{ containerPort = 8080 }]
            resources       = { requests = { cpu = "500m", memory = "512Mi" }, limits = { cpu = "2", memory = "2Gi" } }
            securityContext = local.restricted_ctr
            readinessProbe  = local.ready_probe
            volumeMounts    = [{ name = "code", mountPath = "/code" }, { name = "tasks", mountPath = "/tasks" }, { name = "deps", mountPath = "/deps" }]
          }]
          volumes = [
            { name = "code", configMap = { name = kubernetes_config_map_v1.code["rewards"].metadata[0].name } },
            { name = "tasks", configMap = { name = "tasks" } },
            { name = "deps", emptyDir = { sizeLimit = "256Mi" } },
          ]
        }, jsondecode(each.value.runtime == null ? jsonencode({ nodeSelector = { "ds10/pool" = "system" } }) : jsonencode({ runtimeClassName = each.value.runtime })))
      }
    }
  })
  depends_on = [kubectl_manifest.store, kubectl_manifest.gvisor, kubernetes_config_map_v1.tasks, aws_s3_object.wheel]
}

resource "kubectl_manifest" "verifier_svc" {
  for_each = local.verifiers
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "Service"
    metadata   = { name = local.vname[each.key], namespace = "rewards" }
    spec       = { selector = { app = "verifier", vid = local.vname[each.key] }, ports = [{ port = 8080, targetPort = 8080 }] }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- the two judges ---------------------------------------------------------------------------------------------------
resource "kubectl_manifest" "judge" {
  for_each = var.judges
  yaml_body = yamlencode({
    apiVersion = "apps/v1"
    kind       = "Deployment"
    metadata   = { name = each.key, namespace = "judges" }
    spec = {
      replicas                = 1
      progressDeadlineSeconds = 1800
      selector                = { matchLabels = { app = each.key } }
      template = {
        metadata = { labels = { app = each.key, role = "judge" } }
        spec = {
          enableServiceLinks = false
          nodeSelector       = { "ds10/pool" = "gpu" }
          tolerations        = [{ key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }]
          containers = [{
            name  = "vllm"
            image = var.vllm_image
            args = [each.value.repo, "--revision", each.value.revision, "--served-model-name", "judge", "--max-model-len", "4096",
            "--gpu-memory-utilization", "0.90", "--seed", "0", "--generation-config", "vllm"]
            env            = [{ name = "HF_HOME", value = "/cache" }]
            ports          = [{ containerPort = 8000 }]
            resources      = { requests = { cpu = "3", memory = "10Gi", "nvidia.com/gpu" = "1" }, limits = { memory = "14Gi", "nvidia.com/gpu" = "1" } }
            readinessProbe = { httpGet = { path = "/health", port = 8000 }, periodSeconds = 10 }
            volumeMounts   = [{ name = "cache", mountPath = "/cache" }, { name = "shm", mountPath = "/dev/shm" }]
          }]
          volumes = [{ name = "cache", emptyDir = {} }, { name = "shm", emptyDir = { medium = "Memory", sizeLimit = "4Gi" } }]
        }
      }
    }
  })
  wait_for_rollout = false
  depends_on       = [helm_release.device_plugin, kubernetes_namespace_v1.ns]
}

resource "kubectl_manifest" "judge_svc" {
  for_each = var.judges
  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "Service"
    metadata   = { name = each.key, namespace = "judges" }
    spec       = { selector = { app = each.key }, ports = [{ port = 8000, targetPort = 8000 }] }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---- the nightly fuzzing job against fresh policy outputs -----------------------------------------------------------
resource "kubectl_manifest" "fuzz_cron" {
  yaml_body = yamlencode({
    apiVersion = "batch/v1"
    kind       = "CronJob"
    metadata   = { name = "verifier-fuzz-nightly", namespace = "rewards" }
    spec = {
      schedule          = "17 2 * * *"
      concurrencyPolicy = "Forbid"
      jobTemplate = {
        spec = {
          backoffLimit = 0
          template = {
            spec = {
              restartPolicy                = "Never"
              automountServiceAccountToken = false
              nodeSelector                 = { "ds10/pool" = "system" }
              securityContext              = local.restricted_pod
              containers = [{
                name  = "fuzz"
                image = var.python_image
                command = ["python", "-u", "/code/tools.py", "fuzz", "--verifiers", "math-final-answer@1.0.0,math-final-answer@2.0.0,code-tests@1.0.0,code-tests@2.0.0",
                "--policy-samples", "latest", "--out", "fuzz/nightly"]
                env             = [{ name = "STORE_URL", value = local.store_url }, { name = "HOME", value = "/tmp" }]
                resources       = { requests = { cpu = "250m", memory = "256Mi" } }
                securityContext = local.restricted_ctr
                volumeMounts    = [{ name = "code", mountPath = "/code" }]
              }]
              volumes = [{ name = "code", configMap = { name = kubernetes_config_map_v1.code["rewards"].metadata[0].name } }]
            }
          }
        }
      }
    }
  })
  depends_on = [kubectl_manifest.verifier]
}

# ---- network --------------------------------------------------------------------------------------------------------
# Verifiers: no internet. They may reach the store (wheels), the judges and DNS, and only the training runs, the
# platform and the rewards namespace itself may call them.
resource "kubectl_manifest" "rewards_netpol" {
  yaml_body = yamlencode({
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata   = { name = "rewards-isolation", namespace = "rewards" }
    spec = {
      podSelector = {}
      policyTypes = ["Ingress", "Egress"]
      ingress     = [{ from = [for ns in ["training", "platform", "rewards"] : { namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = ns } } }] }]
      egress = [
        { to = [{ namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }, podSelector = { matchLabels = { "k8s-app" = "kube-dns" } } }],
        ports = [{ port = 53, protocol = "UDP" }, { port = 53, protocol = "TCP" }] },
        { to = [{ namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "platform" } }, podSelector = { matchLabels = { app = "store" } } }], ports = [{ port = 8080 }] },
        { to = [{ namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "judges" } } }], ports = [{ port = 8000 }] },
        { to = [{ podSelector = {} }], ports = [{ port = 8080 }] },
      ]
    }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

# Judges answer only verifiers. They still reach the internet, to download their weights at start.
resource "kubectl_manifest" "judges_netpol" {
  yaml_body = yamlencode({
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata   = { name = "judges-ingress", namespace = "judges" }
    spec = {
      podSelector = {}
      policyTypes = ["Ingress"]
      ingress     = [{ from = [{ namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "rewards" } } }], ports = [{ port = 8000 }] }]
    }
  })
  depends_on = [kubernetes_namespace_v1.ns]
}

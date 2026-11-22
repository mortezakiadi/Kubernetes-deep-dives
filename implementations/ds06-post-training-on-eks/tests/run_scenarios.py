#!/usr/bin/env python3
"""Runs the Design 06 test scenarios against the live platform and records the evidence.

    run_scenarios.py [S01 S02 ... | all] [--out evidence/final]

For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (a terminal-style screenshot
drawn from the transcript). Every private value is redacted before anything is written. The scenarios run in order and
later ones use the runs earlier ones made (the SFT model of S02 is the base of everything after it), so `all` is the
normal way to run them; run ids are kept in <out>/state.json so a single scenario can be re-run.
"""
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ENV, KUBECTL, ROOT, Scenario, Step, redact  # noqa: E402
from helpers import *  # noqa: E402,F401,F403
from render_terminal import render as render_png  # noqa: E402

OUT = ROOT / "evidence" / "final"
STATE = {}
TEACHER = "hf://Qwen/Qwen2.5-3B-Instruct@aa8e72537993ba99e69dfaafa59ed015b17504d1"


# ------------------------------------------------------------------------------------------------------------------
def show(sc, display, output):
    """Record a step whose output this program computed from real data (S3, the cluster) rather than one command."""
    sc.steps.append(Step(redact(display), redact(output), 0, 0))


def k(sc, *args, display=None, **kw):
    return sc.run(display or ("kubectl " + " ".join(args)), [KUBECTL, *args], **kw)


def ptl(sc, *args, display=None, **kw):
    return sc.run(display or ("ptl.py " + " ".join(str(a) for a in args)), PTL + [str(a) for a in args], **kw)


def launch(sc, recipe, *extra, display=None):
    out, code = ptl(sc, "launch", rel(recipe), *extra, display=display)
    return run_id_of(out), out, code


def save_state():
    (OUT / "state.json").write_text(json.dumps(STATE, indent=1))


def evaluate(sc, targets, seconds=1800):
    """Launch the standard evaluation for each target model, then wait for all of them."""
    runs = {}
    for name, ref in targets.items():
        rid, _, _ = launch(sc, RECIPES / "eval.yaml", "--set", f"target={ref}",
                           display=f"ptl.py launch recipes/assistant-v1/eval.yaml --set target={ref}   # {name}")
        runs[name] = rid
    for rid in runs.values():
        if rid:  # a launch the launcher refused has no run to wait for
            wait_run(rid, seconds)
    return runs


def compare(sc, *runs, note=""):
    return ptl(sc, "compare", *runs, display="ptl.py compare " + " ".join(runs) + (f"   # {note}" if note else ""))


def fmt(d, keys):
    return "  ".join(f"{k}={d.get(k)}" for k in keys)


# ------------------------------------------------------------------------------------------------------------------
# S01  the platform at rest
# ------------------------------------------------------------------------------------------------------------------
def s01():
    sc = Scenario("S01", "The platform at rest: cluster, node pools, queues, verifiers and stores", "The experiment platform on Kubernetes",
                  "Show what was built before anything runs: a standard EKS cluster (not Auto Mode) with a CPU pool and a GPU pool on a pinned "
                  "accelerated AMI, Kueue and JobSet, the two queues, the sandboxed verifier pool, the health monitor, and the four stores in S3.")
    sc.aws("eks", "describe-cluster", "--name", PROJECT, "--query", "cluster.{name:name,status:status,version:version,autoMode:computeConfig.enabled}",
           "--output", "json", display="aws eks describe-cluster --name ds06-posttrain --query 'cluster.{name,status,version,autoMode:computeConfig.enabled}'")
    c = json.loads(sc.raw or "{}")
    sc.expect("the cluster is ACTIVE on Kubernetes 1.37, and EKS Auto Mode is not enabled", c.get("status") == "ACTIVE" and c.get("version") == "1.37" and not c.get("autoMode"),
              "Terraform created a standard EKS cluster: no compute_config block, so EKS does not manage the nodes.")
    groups = {}
    for g in ("system", "gpu"):
        sc.aws("eks", "describe-nodegroup", "--cluster-name", PROJECT, "--nodegroup-name", f"{PROJECT}-{g}", "--query",
               "nodegroup.{name:nodegroupName,ami:amiType,release:releaseVersion,types:instanceTypes,desired:scalingConfig.desiredSize,taints:taints}",
               "--output", "json", display=f"aws eks describe-nodegroup --nodegroup-name ds06-posttrain-{g} --query 'nodegroup.{{ami,release,types,desired,taints}}'")
        groups[g] = json.loads(sc.raw or "{}")
    gp = groups.get("gpu", {})
    sc.expect("the GPU pool is an EKS managed node group on the NVIDIA AMI, at the pinned release, with a GPU taint",
              gp.get("ami") == "AL2023_x86_64_NVIDIA" and gp.get("release") == "1.37.1-20261119" and any(t.get("key") == "nvidia.com/gpu" for t in gp.get("taints") or []),
              "The release version is a Terraform variable, so the NVIDIA driver and CUDA user space change only when the file changes.")
    k(sc, "get", "nodes", "-o", "custom-columns=NODE:.metadata.name,POOL:.metadata.labels.ds06/pool,TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,ZONE:.metadata.labels.topology\\.kubernetes\\.io/zone,GPU:.status.allocatable.nvidia\\.com/gpu",
      display="kubectl get nodes -o custom-columns=NODE,POOL,TYPE,ZONE,GPU")
    nodes = gpu_nodes()
    sc.expect("two GPU nodes are Ready and each advertises one GPU", len(nodes) == 2 and all(n["status"]["allocatable"].get("nvidia.com/gpu") == "1" for n in nodes),
              "The NVIDIA device plugin, installed by Terraform on the GPU pool only, advertises nvidia.com/gpu on each node.")
    sc.aws("ec2", "describe-instances", "--filters", f"Name=tag:eks:nodegroup-name,Values={PROJECT}-gpu", "Name=instance-state-name,Values=running",
           "--query", "Reservations[].Instances[].[InstanceId,InstanceType,Placement.AvailabilityZone]", "--output", "text",
           display="aws ec2 describe-instances --filters Name=tag:eks:nodegroup-name,Values=ds06-posttrain-gpu --query '[InstanceId,InstanceType,Zone]'")
    sc.expect("the GPU nodes are ordinary EC2 instances the account can see (and count, and prove gone)", len([l for l in sc.raw.splitlines() if l.strip()]) == 2,
              "Managed node group instances are listed by EC2, unlike Auto Mode's managed instances.")
    sc.bash("kubectl get deploy,ds -A -o jsonpath=<images of the controllers>",
            f"{KUBECTL} get deploy,ds -A -o jsonpath='{{range .items[*]}}{{.metadata.namespace}}/{{.metadata.name}} {{.spec.template.spec.containers[0].image}}{{\"\\n\"}}{{end}}' | grep -E 'kueue|jobset|nvidia|verifier|health' | sed -E 's#(registry.k8s.io|nvcr.io|public.ecr.aws)/##; s#@sha256:[0-9a-f]{{12}}[0-9a-f]+#@sha256:…#'")
    sc.expect("Kueue 0.20.7, JobSet 0.13.1 and the NVIDIA device plugin 0.20.2 are running, with the verifier pool and the health monitor",
              all(w in sc.raw for w in ["kueue:v0.20.7", "jobset", "v0.13.1", "k8s-device-plugin:v0.20.2", "verifier", "health-monitor"]),
              "Each controller is a Helm release pinned in Terraform; the image tags are the evidence of the versions.")
    k(sc, "get", "clusterqueue", "-o",
      "custom-columns=QUEUE:.metadata.name,COHORT:.spec.cohortName,GPU-OWNED:.spec.resourceGroups[0].flavors[0].resources[2].nominalQuota,GPU-MAY-BORROW:.spec.resourceGroups[0].flavors[0].resources[2].borrowingLimit,RECLAIM:.spec.preemption.reclaimWithinCohort")
    cqs = {q["metadata"]["name"]: q for q in kj("get", "clusterqueue").get("items", [])}
    gpu = lambda q: q["spec"]["resourceGroups"][0]["flavors"][0]["resources"][2]
    sc.expect("production owns both GPUs and reclaims them; ablation owns none and may only borrow",
              set(cqs) == {"production", "ablation"} and gpu(cqs["production"])["nominalQuota"] in (2, "2") and gpu(cqs["ablation"])["nominalQuota"] in (0, "0")
              and gpu(cqs["ablation"]).get("borrowingLimit") in (2, "2") and cqs["production"]["spec"]["preemption"]["reclaimWithinCohort"] == "Any"
              and cqs["ablation"]["spec"]["preemption"]["reclaimWithinCohort"] == "Never",
              "Two ClusterQueues in one cohort. The ablation queue has no quota of its own, so everything it runs is borrowed and can be reclaimed.")
    k(sc, "get", "workloadpriorityclass,localqueue", "-A")
    k(sc, "-n", "rewards", "get", "deploy,hpa,networkpolicy", display="kubectl -n rewards get deploy,hpa,networkpolicy")
    np = kj("-n", "rewards", "get", "networkpolicy", "verifier-isolation")
    ns = kj("get", "namespace", "rewards")
    sc.expect("the verifier namespace denies all egress and enforces the restricted Pod Security level",
              np.get("spec", {}).get("policyTypes") == ["Ingress", "Egress"] and not np.get("spec", {}).get("egress")
              and ns.get("metadata", {}).get("labels", {}).get("pod-security.kubernetes.io/enforce") == "restricted",
              "A NetworkPolicy with an empty egress list, enforced by the VPC CNI's network policy agent (enabled in the add-on's configuration).")
    sc.aws("eks", "list-pod-identity-associations", "--cluster-name", PROJECT, "--query", "associations[].[namespace,serviceAccount]", "--output", "text")
    sc.expect("four workload identities: trainer, datagen, evaluator, health-monitor (and none for the verifiers)",
              sorted(l.split()[1] for l in sc.raw.splitlines() if l.strip()) == ["datagen", "evaluator", "health-monitor", "trainer"],
              "EKS Pod Identity associations, each with a role scoped to the S3 prefixes it needs.")
    idx = s3_json("datasets/index.json") or {}
    show(sc, "aws s3 cp s3://<bucket>/datasets/index.json -   # the versioned datasets, with content hashes",
         "\n".join(f"{n:26s} {v['rows']:4d} rows  sha256 {v['sha256'][:16]}" for n, v in sorted(idx.items())))
    sc.expect("twelve versioned datasets are in the store, each with a content hash", len(idx) == 12 and all(len(v["sha256"]) == 64 for v in idx.values()),
              "Generated by datasets/make_datasets.py from a fixed seed and uploaded by Terraform.")
    k(sc, "-n", NS, "get", "jobs,jobsets,workloads", display="kubectl -n post-training get jobs,jobsets,workloads   # before any run")
    sc.expect("nothing is running or queued", "No resources found" in sc.raw, "A fresh build: no run has been launched.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S02  a recipe is a versioned file
# ------------------------------------------------------------------------------------------------------------------
def s02():
    sc = Scenario("S02", "A recipe is a versioned file: identity, resolution, launch", "A recipe is a pipeline of stages; The experiment platform on Kubernetes",
                  "One YAML file fully describes a run. Its identity is the hash of its content, not of its formatting. Launching it pins every "
                  "reference (data, model, image, code, Git commit) in the tracking store before a pod exists, and labels the job with the hash.")
    recipe = RECIPES / "sft.yaml"
    sc.run(f"git log -1 --format='%h %s' -- {rel(recipe)}", ["git", "log", "-1", "--format=%h %s", "--", str(recipe)], )
    out1, _ = ptl(sc, "hash", rel(recipe))
    h = out1.strip()
    import yaml
    tmp = ROOT / "evidence" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    spec = yaml.safe_load(recipe.read_text())
    (tmp / "sft-reformatted.yaml").write_text(yaml.safe_dump(dict(reversed(list(spec.items()))), sort_keys=False, default_flow_style=False))
    out2, _ = ptl(sc, "hash", "evidence/tmp/sft-reformatted.yaml", display="ptl.py hash evidence/tmp/sft-reformatted.yaml   # same content: comments removed, keys re-ordered, re-indented")
    spec["hyperparameters"]["lr"] = 3.0e-5
    (tmp / "sft-lr.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    out3, _ = ptl(sc, "hash", "evidence/tmp/sft-lr.yaml", display="ptl.py hash evidence/tmp/sft-lr.yaml   # one value changed: lr 1e-5 -> 3e-5")
    sc.expect("the recipe's identity ignores formatting", re.fullmatch(r"[0-9a-f]{64}", h) is not None and out2.strip() == h,
              "The hash is sha256 of the parsed recipe serialised as canonical JSON (sorted keys, no whitespace), so comments, key order and indentation do not count.")
    sc.expect("one changed value gives a different identity", out3.strip() != h, "Any change to what the run does changes the canonical JSON.")
    dry, _ = ptl(sc, "launch", rel(recipe), "--dry-run", display="ptl.py launch recipes/assistant-v1/sft.yaml --dry-run")
    d = json.loads(sc.raw).get("resolved", {}) if sc.raw.strip().startswith("{") else {}
    sc.steps[-1].output = redact(json.dumps({k_: d.get(k_) for k_ in ("recipe_hash", "base", "image", "queue")} |
                                            {"datasets": {n: {"ref": v["ref"], "sha256": v["sha256"][:16] + "…"} for n, v in d.get("datasets", {}).items()}}, indent=1))
    sc.expect("every reference is pinned: datasets by content hash, the base model by commit, the image by digest",
              d and all(len(v["sha256"]) == 64 for v in d["datasets"].values()) and len(d["base"]["revision"]) == 40 and "@sha256:" in d["image"],
              "The launcher looks each dataset up in the index and copies its hash, refuses a Hugging Face model without a full commit, and uses the image digest.")
    rid, out, code = launch(sc, recipe)
    STATE["sft"] = rid
    save_state()
    res = s3_json(f"runs/{rid}/resolved.json") or {}
    show(sc, f"aws s3 cp s3://<bucket>/runs/{rid}/resolved.json - | jq '{{run_id,recipe_hash,git_commit,recipe_uncommitted,code_sha256,submitted}}'",
         json.dumps({k_: res.get(k_) for k_ in ("run_id", "recipe_hash", "git_commit", "recipe_uncommitted", "code_sha256", "submitted")}, indent=1))
    sc.expect("the run's resolved description is in the tracking store, from a committed recipe, with the hash of the code the cluster runs",
              res.get("recipe_hash") == h and res.get("recipe_uncommitted") is False and len(res.get("code_sha256", "")) == 64,
              "The launcher writes runs/<id>/resolved.json before it creates the Job, and refuses to launch if the cluster's code ConfigMap differs from the checkout.")
    wait_for(lambda: workload_of(rid).get("status", {}).get("admission"), 120, 3)
    k(sc, "-n", NS, "get", "job", rid, "-L", "ds06/recipe-hash,kueue.x-k8s.io/queue-name,kueue.x-k8s.io/priority-class", display=f"kubectl -n post-training get job {rid} -L ds06/recipe-hash,queue-name,priority-class")
    sc.expect("the Job carries the recipe hash and the production queue", h[:12] in sc.raw and "production" in sc.raw, "The launcher sets the labels; Kueue reads the queue label.")
    w = workload_of(rid)
    k(sc, "-n", NS, "get", "workloads", "-o", "custom-columns=WORKLOAD:.metadata.name,QUEUE:.spec.queueName,PRIORITY:.spec.priority,ADMITTED-BY:.status.admission.clusterQueue")
    sc.expect("Kueue admitted it through the production queue at production priority",
              w.get("status", {}).get("admission", {}).get("clusterQueue") == "production" and w.get("spec", {}).get("priority") == 1000,
              "The queue label made Kueue create a Workload; the priority-class label gave it the production class's value, 1000.")
    pods = pods_of(rid)
    created = pods[0]["metadata"]["creationTimestamp"] if pods else ""
    show(sc, "resolved.json written at, and the pod created at", f"{time.strftime('%H:%M:%S', time.gmtime(res.get('submitted', 0)))} UTC  /  {created[11:19]} UTC")
    sc.expect("the description existed before the pod did", bool(pods) and time.strftime("%H:%M:%S", time.gmtime(res.get("submitted", 0))) <= created[11:19],
              "The launcher writes to S3 first and only then calls the API server.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S03  SFT
# ------------------------------------------------------------------------------------------------------------------
def s03():
    sc = Scenario("S03", "SFT: an explicit mixture, assistant-only loss, packing, and what it changed", "SFT: data mixtures and loss masking; Evaluation and final model behavior",
                  "Train the SFT stage from S02 and check, on the real batches and the real model, the three things the design says an SFT run is "
                  "made of: the mixture, the loss mask and packing. Then evaluate the base model and the SFT model with the same suites and apply the gates.")
    rid = STATE["sft"]
    state = wait_run(rid, 1800)
    sc.expect("the SFT run finished", state == "succeeded", "The Job ran on one GPU and exited cleanly.")
    mix = event(rid, "mixture")
    show(sc, f"runs/{rid}/events: mixture   # weights from the recipe, examples actually drawn",
         "\n".join(f"{n:10s} weight {v['weight']:.3f}   drawn {v['drawn']:4d} ({v['share_drawn']:.3f})   rows in source {v['rows_available']:3d}   passes over source {v['passes_over_source']}"
                   for n, v in mix.get("sources", {}).items()))
    sc.expect("each source's share of the 1,600 examples is within 0.03 of its weight",
              mix and all(abs(v["share_drawn"] - v["weight"]) <= 0.03 for v in mix["sources"].values()) and mix["size"] == 1600,
              "Each of the 1,600 slots picks a source with the recipe's probability (seed 17), so weights, not row counts, decide what the model sees.")
    m = event(rid, "loss_mask")
    show(sc, f"runs/{rid}/events: loss_mask   # the first real batch the trainer saw",
         f"tokens {m.get('batch_tokens')}, with loss {m.get('tokens_with_loss')} ({m.get('share_with_loss')}), conversations {m.get('conversations_in_batch')}, padding tokens {m.get('padding_tokens')}\n"
         f"NO LOSS on: …{m.get('first_conversation_untrained_text', '')[-150:]!r}\nLOSS on:    {m.get('first_conversation_trained_text', '')[:150]!r}")
    sc.expect("only the assistant's turn carries loss: the user's words and the system prompt do not",
              "<|im_start|>user" in m.get("first_conversation_untrained_text", "") and "user" not in m.get("first_conversation_trained_text", "x") and 0 < m.get("share_with_loss", 1) < 0.6,
              "assistant_only_loss=True: TRL swaps in a training chat template with generation markers and sets the label of every other token to -100.")
    sc.expect("conversations are packed with no padding at all", m.get("padding_tokens") == 0 and m.get("conversations_in_batch", 0) > 4,
              "packing=True with TRL's best-fit-decreasing strategy concatenates conversations and switches to padding-free batches.")
    iso = event(rid, "packing_isolation")
    show(sc, f"runs/{rid}/events: packing_isolation   # logits of conversation A alone vs packed after B (float32, same GPU)",
         f"packed, positions restart at A: max |Δlogit| = {iso.get('max_abs_diff_packed'):.2e}\nsame tokens as ONE sequence:     max |Δlogit| = {iso.get('max_abs_diff_one_sequence'):.2f}" if iso else "(missing)")
    sc.expect("a packed conversation cannot attend to its neighbour", iso and iso["max_abs_diff_packed"] < 1e-2 < 1.0 < iso["max_abs_diff_one_sequence"],
              "Transformers builds a block-diagonal mask from position ids that restart at each conversation. Treating the same tokens as one sequence changes A's logits by tens, so the check can tell the difference.")
    s = summary(rid)
    show(sc, f"runs/{rid}/summary.json", fmt(s, ["steps", "loss_first", "loss_last", "train_seconds", "tokens_seen", "tokens_per_second", "gpu_seconds", "serving_template_matches_training"]))
    sc.expect("the loss fell and the serving chat template renders prompts exactly as in training",
              s.get("loss_first") and s["loss_last"] < s["loss_first"] / 5 and s.get("serving_template_matches_training") is True,
              "The data is templated and small, so the loss falls far. The saved tokenizer renders a prompt byte for byte like the base tokenizer.")
    e = registry_entry(rid)
    show(sc, f"aws s3 cp s3://<bucket>/registry/assistant-v1/sft/{rid}/entry.json - | jq '{{recipe_hash,base,data,status,files,bytes}}'",
         json.dumps({k_: e.get(k_) for k_ in ("recipe_hash", "base", "status", "files", "bytes")} | {"data": [d_["ref"] + " " + d_["sha256"][:12] for d_ in e.get("data", [])]}, indent=1))
    sc.expect("the model is registered with its recipe hash and the hash of every dataset it was trained on",
              e.get("recipe_hash") == (s3_json(f"runs/{rid}/resolved.json") or {}).get("recipe_hash") and len(e.get("data", [])) == 4 and e.get("status") == "candidate",
              "The SFT job uploads the model and writes the entry; a new entry is a candidate until its gates pass.")
    ev = evaluate(sc, {"base model": f"hf://Qwen/Qwen2.5-0.5B-Instruct@{BASE_REV}", "SFT": f"registry://assistant-v1/sft@{rid}"})
    STATE["eval_base"], STATE["eval_sft"] = ev["base model"], ev["SFT"]
    save_state()
    compare(sc, BASE_REV, rid, note="same suites; the first column is the base model")
    b, f = evals_of(BASE_REV).get("suites", {}), evals_of(rid).get("suites", {})
    sc.expect("SFT taught the house style: from no answers to most answers starting 'Short answer:'",
              b.get("casual", {}).get("house_style_rate") == 0 and f.get("casual", {}).get("house_style_rate", 0) >= 0.8,
              "Only the SFT data contains the style; the system prompt does not mention it.")
    out, _ = ptl(sc, "gate", rid)
    STATE["sft_verdict"] = "promoted" if "PROMOTED" in out else "blocked"
    save_state()
    sc.expect("the gate decided, from the attached evaluation, and recorded its reasons in the registry", ("PROMOTED" in out or "BLOCKED" in out) and
              registry_entry(rid).get("gate", {}).get("eval_run") == ev["SFT"], "ptl.py gate reads the evaluation of this model and the recipe's gates and writes the verdict and every check into the entry.")
    sc.note(f"Gate verdict for the SFT model: {STATE['sft_verdict']}.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S04  ablations
# ------------------------------------------------------------------------------------------------------------------
def s04():
    sc = Scenario("S04", "Change one thing per ablation, and what a mixture weight changes", "What each choice changes; SFT: data mixtures and loss masking",
                  "The launcher must refuse an ablation whose diff touches more than its declared variable, and create nothing. A correct one-key "
                  "ablation (more reasoning traces in the mixture) runs on the ablation queue and is compared with its parent on the same suites.")
    before_jobs = len(kj("-n", NS, "get", "jobs").get("items", []))
    before_runs = len({o["Key"].split("/")[1] for o in s3_keys("runs/")})
    out, code = ptl(sc, "launch", rel(RECIPES / "ablations" / "sft-two-changes.yaml"))
    after_jobs = len(kj("-n", NS, "get", "jobs").get("items", []))
    after_runs = len({o["Key"].split("/")[1] for o in s3_keys("runs/")})
    show(sc, "jobs in post-training, runs in the tracking store: before and after the refused launch", f"jobs {before_jobs} -> {after_jobs}; runs {before_runs} -> {after_runs}")
    sc.expect("an ablation that changes two keys is refused, and names the undeclared one", code == 3 and "REFUSED" in out and "hyperparameters.lr" in out,
              "The launcher diffs the flattened recipe against its parent and compares the changed keys with the declared variable.")
    sc.expect("nothing was created: no Job, no run record", before_jobs == after_jobs and before_runs == after_runs, "The check runs before anything is written to S3 or sent to the API server.")
    ptl(sc, "diff", rel(RECIPES / "sft.yaml"), rel(RECIPES / "ablations" / "sft-reasoning-heavy.yaml"))
    rid, out, code = launch(sc, RECIPES / "ablations" / "sft-reasoning-heavy.yaml")
    STATE["sft_heavy"] = rid
    save_state()
    wait_for(lambda: workload_of(rid).get("status", {}).get("admission"), 600, 5)
    w = workload_of(rid)
    k(sc, "-n", NS, "get", "workload", w.get("metadata", {}).get("name", "x"), "-o", "custom-columns=WORKLOAD:.metadata.name,QUEUE:.spec.queueName,PRIORITY:.spec.priority,ADMITTED-BY:.status.admission.clusterQueue",
      display="kubectl -n post-training get workload <the ablation's> -o custom-columns=QUEUE,PRIORITY,ADMITTED-BY")
    sc.expect("the one-key ablation was accepted and runs on the ablation queue at low priority",
              code == 0 and w.get("spec", {}).get("queueName") == "ablation" and w.get("spec", {}).get("priority") == 100,
              "For an ablation the launcher always uses the ablation queue and priority class, whatever the recipe says.")
    wait_run(rid, 1800)
    res = s3_json(f"runs/{rid}/resolved.json") or {}
    show(sc, f"runs/{rid}/resolved.json: ablation", json.dumps(res.get("ablation"), indent=1))
    mix = event(rid, "mixture").get("sources", {})
    sc.expect("the ablation's record names its parent, the variable and both values; its mixture really is reasoning-heavy",
              (res.get("ablation") or {}).get("variable") == "data.sources.reasoning.weight" and abs(mix.get("reasoning", {}).get("share_drawn", 0) - 0.667) < 0.03,
              "Weight 1.5 among 0.45, 0.15 and 0.15 normalises to 0.667, and 1,600 examples are drawn either way, so only the mixture differs.")
    ev = evaluate(sc, {"reasoning-heavy SFT": f"registry://assistant-v1/sft@{rid}"})
    STATE["eval_heavy"] = ev["reasoning-heavy SFT"]
    save_state()
    compare(sc, BASE_REV, STATE["sft"], rid, note="base, SFT, reasoning-heavy SFT")
    out, _ = ptl(sc, "gate", rid)
    STATE["heavy_verdict"] = "promoted" if "PROMOTED" in out else "blocked"
    save_state()
    f, hv = evals_of(STATE["sft"]).get("suites", {}), evals_of(rid).get("suites", {})
    sc.expect("both models were scored by the same suites, so the difference is attributable to the one key",
              bool(f) and bool(hv) and set(f) == set(hv), "Same eval recipe, same datasets (by hash), greedy decoding.")
    sc.expect("more reasoning traces made casual answers come out in numbered steps more often, and lowered the house-style rate",
              hv.get("casual", {}).get("step_leak_rate", 0) > f.get("casual", {}).get("step_leak_rate", 1) and
              hv.get("casual", {}).get("house_style_rate", 1) < f.get("casual", {}).get("house_style_rate", 0),
              "Quick conversion questions of families that neither source covers are answered in whichever style dominates the mixture.")
    sc.expect("the gate blocked the ablation on behaviour, with its reasons in the registry", STATE["heavy_verdict"] == "blocked" and
              any(not r["ok"] for r in registry_entry(rid).get("gate", {}).get("results", [])),
              "A candidate must pass every gate, not improve on average: a capability gain does not buy a behaviour regression.")
    sc.note(f"Gate verdict for the reasoning-heavy SFT: {STATE['heavy_verdict']}.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S05  prompt filtering
# ------------------------------------------------------------------------------------------------------------------
def s05():
    sc = Scenario("S05", "Filtering prompts by pass rate before RL", "Filtering: which samples carry signal",
                  "Estimate each RL prompt's pass rate with the SFT model (16 answers per prompt, scored by the verifier pool) and keep only prompts "
                  "between 5% and 95%. The kept prompts become new, write-once dataset versions that the RL recipe names.")
    rid, _, _ = launch(sc, RECIPES / "filter.yaml")
    STATE["filter"] = rid
    save_state()
    state = wait_run(rid, 2400)
    sc.expect("the filter job finished", state == "succeeded", "One GPU, vLLM's offline engine, then the verifier pool.")
    for e in [x for x in events(rid) if x and x.get("ev") == "filtered"]:
        show(sc, f"runs/{rid}/events: filtered {e['source']}",
             f"prompts {e['prompts']}, kept {e['kept']}, too easy (>95%) {e['too_easy']}, too hard (<5%) {e['too_hard']}, mean pass rate {e['mean_pass_rate']}\n"
             f"pass rate by level/family: {json.dumps(e['pass_rate_by_level'])}\n{e['completion_tokens']} tokens generated in {e['seconds']} s ({e['tokens_per_second']} tokens/s)")
    evs = {x["source"]: x for x in events(rid) if x and x.get("ev") == "filtered"}
    sc.expect("every prompt is accounted for: kept + too easy + too hard = all", evs and all(e["kept"] + e["too_easy"] + e["too_hard"] == e["prompts"] for e in evs.values()),
              "A prompt's pass rate is the share of its 16 answers the verifier accepted.")
    lv = evs.get("math", {}).get("pass_rate_by_level", {})
    sc.expect("harder arithmetic has a lower pass rate, so the filter removes the easy end and the hard end",
              lv and lv.get("1", 0) > lv.get("4", 1) and evs["math"]["too_easy"] > 0 and evs["math"]["kept"] > 0,
              "Level 1 is one two-digit addition; level 4 is a multiplication, a subtraction and a division.")
    metas = {n: s3_json(f"datasets/generated/{n}/v1/meta.json") or {} for n in ("rl-math-pass", "rl-code-pass")}
    show(sc, "aws s3 cp s3://<bucket>/datasets/generated/<name>/v1/meta.json -   # the new dataset versions",
         "\n".join(f"{m.get('ref')}: {m.get('rows')} rows, sha256 {m.get('sha256', '')[:16]}, made by {m.get('made_by')}" for m in metas.values()))
    sc.expect("the kept prompts are new dataset versions with a content hash and the run that made them",
              all(m.get("rows") == evs.get(n.split("-")[1], {}).get("kept") and len(m.get("sha256", "")) == 64 and m.get("made_by") == rid for n, m in metas.items()),
              "generate.py writes the data, then its meta.json, and refuses to write a version that already exists.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S06  queues: ablations borrow, production reclaims
# ------------------------------------------------------------------------------------------------------------------
def s06():
    sc = Scenario("S06", "Ablations borrow idle GPUs; a production run takes them back", "The experiment platform on Kubernetes",
                  "Start an RL ablation that borrows both GPUs while production is idle. Then launch the production RL run: Kueue must preempt the "
                  "borrower as a whole, admit production, and re-admit the ablation once production is done. Production is never preempted.")
    ab, _, _ = launch(sc, RECIPES / "ablations" / "rl-unfiltered.yaml")
    STATE["rl_unfiltered"] = ab
    save_state()
    running = lambda r: len(pods_of(r)) == 2 and all(p["status"].get("phase") == "Running" for p in pods_of(r))
    wait_for(lambda: running(ab), 1200, 5)
    k(sc, "-n", NS, "get", "pods", "-l", f"ds06/run-id={ab}", "-o", "custom-columns=POD:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName",
      display=f"kubectl -n post-training get pods -l ds06/run-id={ab}   # the ablation, borrowing both GPUs")
    show(sc, "kubectl get clusterqueue ablation -o jsonpath='{.status.flavorsUsage}'", json.dumps(kj("get", "clusterqueue", "ablation").get("status", {}).get("flavorsUsage", [])))
    usage = kj("get", "clusterqueue", "ablation").get("status", {}).get("flavorsUsage", [{}])[0].get("resources", [])
    borrowed = next((r.get("borrowed") for r in usage if r["name"] == "nvidia.com/gpu"), None)
    sc.expect("the ablation is running on both GPUs, all of them borrowed", running(ab) and str(borrowed) == "2",
              "The ablation queue owns no GPU, so its usage is entirely borrowed from production's idle quota.")
    time.sleep(20)
    t0 = time.time()
    prod, _, _ = launch(sc, RECIPES / "rl.yaml")
    STATE["rl"] = prod
    save_state()
    wait_for(lambda: workload_of(prod).get("status", {}).get("admission"), 600, 3)
    t_adm = time.time() - t0
    wab = workload_of(ab)
    ev_cond = cond(wab, "Evicted") or cond(wab, "Preempted")
    show(sc, "kubectl -n post-training get workload <the ablation's> -o jsonpath='{.status.conditions[?(@.type==\"Evicted\")]}'",
         json.dumps({k_: cond(wab, "Evicted").get(k_) for k_ in ("status", "reason", "message")}) + "\n" +
         json.dumps({k_: cond(wab, "Preempted").get(k_) for k_ in ("status", "reason", "message")}))
    sc.expect("Kueue preempted the borrowing ablation to reclaim production's quota", ev_cond.get("status") == "True" and
              ("Preempt" in ev_cond.get("reason", "") or "reclaim" in ev_cond.get("message", "").lower() or "Preempted" in json.dumps(wab.get("status", {}))),
              "Production's queue has reclaimWithinCohort: Any. The ablation's usage is borrowed, so it is the one preempted.")
    gone = wait_for(lambda: not pods_of(ab), 300, 3)
    show(sc, "time from launching production to its admission", f"{t_adm:.0f} s; the ablation's pods were all removed: {'yes' if gone is not None else 'no'}")
    sc.expect("the ablation was stopped as a whole: none of its pods is left", gone is not None, "Kueue suspends the JobSet, and JobSet deletes every child Job, not one of them.")
    wait_for(lambda: running(prod), 1200, 5)
    k(sc, "-n", NS, "get", "workloads", "-o", "custom-columns=WORKLOAD:.metadata.name,QUEUE:.spec.queueName,PRIORITY:.spec.priority,ADMITTED:.status.admission.clusterQueue")
    wp = workload_of(prod)
    sc.expect("the production RL run is admitted on its own quota (nothing borrowed)", wp.get("status", {}).get("admission", {}).get("clusterQueue") == "production",
              "Production uses its nominal quota of two GPUs.")
    # While it runs, read its health signals from the tracking store (used by S07).
    if wait_for(lambda: status(prod).get("state") == "running" and len([r for r in metrics(prod) if "reward" in r]) >= 5, 2400, 10):
        st, rows = status(prod), [r for r in metrics(prod) if "reward" in r]
        STATE["rl_midrun"] = (f"status.json: state={st.get('state')} last_step={st.get('last_step')}\n" + "\n".join(
            fmt(r, ["step", "reward", "entropy", "frac_reward_zero_std", "completions/mean_length"]) for r in rows[-3:]))
        save_state()
    wait_run(prod, 3600)
    sc.expect("production was never preempted", not cond(workload_of(prod), "Evicted").get("status") == "True" and status(prod).get("state") == "succeeded",
              "Nothing in the cohort may reclaim from production: the ablation queue has reclaimWithinCohort: Never and no quota to reclaim.")
    readmit = wait_for(lambda: running(ab) or status(ab).get("state") in ("succeeded",), 1800, 10)
    k(sc, "-n", NS, "get", "pods", "-l", f"ds06/run-id={ab}", "-o", "custom-columns=POD:.metadata.name,PHASE:.status.phase,STARTED:.status.startTime",
      display=f"kubectl -n post-training get pods -l ds06/run-id={ab}   # after production finished")
    sc.expect("the ablation was re-admitted once production finished", bool(readmit), "The preempted Workload went back to its queue and was admitted as soon as the borrowed GPUs were idle again.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S07  the RL job
# ------------------------------------------------------------------------------------------------------------------
def s07():
    sc = Scenario("S07", "RL: rollout, reward and training as separate components of one job", "RL: choosing the objective; Rollout generation; Rewards and verifiers",
                  "Look inside the production GRPO run from S06: one JobSet, admitted as one unit, with the vLLM rollout server and the trainer on "
                  "different GPUs, rewards from the verifier pool, health signals streamed while it ran, timed generation and weight sync, checkpoints and a registered model.")
    rid = STATE["rl"]
    js = kj("-n", NS, "get", "jobset", rid)
    show(sc, f"kubectl -n post-training get jobset {rid} -o jsonpath='{{.spec.replicatedJobs[*].name}} {{.spec.successPolicy}} {{.status.conditions}}'",
         " ".join(r["name"] for r in js.get("spec", {}).get("replicatedJobs", [])) + "\n" + json.dumps(js.get("spec", {}).get("successPolicy")) + "\n" +
         json.dumps([{k_: c.get(k_) for k_ in ("type", "status", "reason")} for c in js.get("status", {}).get("conditions", [])]))
    sc.expect("one JobSet with two parts, rollout and trainer, that succeeds when the trainer does",
              [r["name"] for r in js.get("spec", {}).get("replicatedJobs", [])] == ["rollout", "trainer"] and cond(js, "Completed").get("status") == "True",
              "The launcher renders an RL recipe in server mode as a JobSet whose success policy targets the trainer only.")
    w = workload_of(rid)
    show(sc, "kubectl -n post-training get workload <the RL run's> -o jsonpath='{.spec.podSets[*].name}={.spec.podSets[*].count} {.status.admission.podSetAssignments[*].resourceUsage}'",
         json.dumps({p["name"]: p["count"] for p in w.get("spec", {}).get("podSets", [])}) + "\n" +
         json.dumps([a.get("resourceUsage", {}) for a in w.get("status", {}).get("admission", {}).get("podSetAssignments", [])]))
    sc.expect("Kueue admitted the run as one Workload asking for both GPUs at once",
              sorted(p["name"] for p in w.get("spec", {}).get("podSets", [])) == ["rollout", "trainer"] and
              sum(int(a.get("resourceUsage", {}).get("nvidia.com/gpu", 0)) for a in w.get("status", {}).get("admission", {}).get("podSetAssignments", [])) == 2,
              "JobSet is a Kueue integration: each replicated job is a pod set of one Workload, and admission is all or nothing.")
    st = status(rid)
    roll = event(rid, "prompts")
    nodes = sorted({p["spec"].get("nodeName") for p in pods_of(rid) if p["spec"].get("nodeName")})
    show(sc, "the run's pods' nodes, and the GPU the trainer reported", f"nodes: {nodes or '(pods already removed after success)'}; trainer GPU: {st.get('gpu')} on {st.get('node')}")
    rows = metrics(rid)
    keys = ["reward", "reward_std", "frac_reward_zero_std", "entropy", "completions/mean_length", "completions/clipped_ratio", "clip_ratio/region_mean",
            "sampling/importance_sampling_ratio/min", "sampling/importance_sampling_ratio/max", "grad_norm"]
    pick = [r for r in rows if r["step"] in (1, 10, 20, 30, 40) and "reward" in r]
    show(sc, f"runs/{rid}/metrics.jsonl   # the health signals of every step (5 of {len(rows)} shown)",
         "step " + " | ".join(k_.split("/")[-1] for k_ in keys) + "\n" + "\n".join(f"{r['step']:4d} " + " | ".join(f"{r.get(k_, float('nan')):.3g}" for k_ in keys) for r in pick))
    sc.expect("every step logged the design's health signals: reward, entropy, length, clipping, importance ratios, gradient norm",
              len(rows) >= 40 and all(all(k_ in r for k_ in keys) for r in rows if r["step"] <= 40 and "reward" in r),
              "TRL's GRPOTrainer computes them; the tracking callback writes every logged row to S3 as the run goes.")
    mid = STATE.get("rl_midrun")
    if mid:
        show(sc, "read from the tracking store while the run was still running (recorded in S06)", mid)
    sc.expect("the signals could be read before the run finished", bool(mid), "metrics.jsonl is rewritten after every logged step; the reading was taken while status.json said running.")
    s = summary(rid)
    tm, share = s.get("timing", {}), s.get("share_of_wall", {})
    show(sc, f"runs/{rid}/summary.json: where the time went", f"wall {s.get('wall_seconds')} s over {s.get('steps')} steps\n"
         f"generation {tm.get('generate_s')} s ({share.get('generate_s')}), weight sync {tm.get('sync_s')} s in {tm.get('syncs')} syncs ({share.get('sync_s')}), reward calls {tm.get('reward_s')} s ({share.get('reward_s')})\n"
         f"completion tokens generated: {s.get('completion_tokens')}; mode: {s.get('mode')}")
    sc.expect("generation and weight sync were measured; the trainer pushed new weights to the rollout server every step",
              tm.get("syncs", 0) >= s.get("steps", 99) - 1 and tm.get("generate_s", 0) > 0 and tm.get("sync_s", 0) > 0,
              "The trainer wraps TRL's generate and sync_weights calls with timers. Each step: sync weights over NCCL to the server, then generate the next batch.")
    vm = []
    for p in kj("-n", "rewards", "get", "pods", "-l", "app=verifier").get("items", []):
        o = kq("-n", "rewards", "exec", p["metadata"]["name"], "--", "python", "-c",
               "import urllib.request,json;print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())")
        vm.append(last_json(o))
    show(sc, "kubectl -n rewards exec <each verifier> -- … /metrics   # the reward pool's own counters", "\n".join(json.dumps(v) for v in vm))
    sc.expect("the rewards came from the verifier pool, which ran model-written code thousands of times", sum(v.get("code_runs", 0) for v in vm) > 100 and sum(v.get("requests", 0) for v in vm) > 40,
              "The trainer's reward functions call the verifier Service; the pool's counters, not the trainer's logs, are the evidence.")
    ck = [e for e in events(rid) if e and e.get("ev") == "checkpoint"]
    keys_ck = sorted({o["Key"].split("/")[3] for o in s3_keys(f"runs/{rid}/ckpt/")})
    show(sc, f"aws s3 ls s3://<bucket>/runs/{rid}/ckpt/", "\n".join(keys_ck) + "\n" + "\n".join(f"checkpoint event: step {e['step']}, {e['mb']} MB" for e in ck))
    sc.expect("a checkpoint was written every 20 steps and the last two are kept", [e["step"] for e in ck] == [20, 40] and keys_ck == ["step-00020", "step-00040"],
              "save_steps=20; after each save the callback uploads the checkpoint and deletes all but the newest two.")
    e = registry_entry(rid, "rl")
    sc.expect("the RL model is registered with its base (the promoted SFT model) and its prompt data", e.get("base") == "registry://assistant-v1/sft@promoted" and e.get("base_resolved") == STATE["sft"] and len(e.get("data", [])) == 2,
              "The launcher resolved @promoted to one run before launch; the registry entry keeps both the reference and what it resolved to.")
    ev = evaluate(sc, {"RL": f"registry://assistant-v1/rl@{rid}"})
    STATE["eval_rl"] = ev["RL"]
    save_state()
    compare(sc, STATE["sft"], rid, note="the SFT model and the RL model on the same suites")
    out, _ = ptl(sc, "gate", rid)
    STATE["rl_verdict"] = "promoted" if "PROMOTED" in out else "blocked"
    save_state()
    sc.note(f"Gate verdict for the RL model: {STATE['rl_verdict']}.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S08  filtering efficiency
# ------------------------------------------------------------------------------------------------------------------
def s08():
    sc = Scenario("S08", "What filtering saves: the same RL run on unfiltered prompts", "Filtering: which samples carry signal",
                  "Compare the production RL run with its one-key ablation on unfiltered math prompts (the run preempted in S06, which finished "
                  "afterwards). In group-relative RL a group whose answers all get the same reward has zero advantage and teaches nothing.")
    a, b = STATE["rl"], STATE["rl_unfiltered"]
    state = wait_run(b, 3600)
    sc.expect("the ablation finished after it was re-admitted", state == "succeeded", "It restarted from step 0 after the preemption in S06.")
    compare(sc, a, b, note="filtered prompts vs unfiltered prompts")
    sa, sb = summary(a), summary(b)
    za, zb = sa.get("frac_reward_zero_std_mean"), sb.get("frac_reward_zero_std_mean")
    show(sc, "rollouts per informative group (1 / (1 - share of groups with zero reward spread))",
         f"filtered:   {za} of groups uninformative -> {1 / (1 - za):.2f} groups generated per useful group\nunfiltered: {zb} of groups uninformative -> {1 / (1 - zb):.2f} groups generated per useful group" if za is not None and zb is not None and za < 1 and zb < 1 else "(missing)")
    sc.expect("on unfiltered prompts a larger share of groups carries no learning signal", za is not None and zb is not None and zb > za,
              "Unfiltered prompts include ones the model always solves and ones it never does; every answer in such a group gets the same reward, so its advantage is zero.")
    res = s3_json(f"runs/{b}/resolved.json") or {}
    sc.expect("the only difference between the two runs is the declared variable", (res.get("ablation") or {}).get("variable") == "data.prompts.math.source",
              "The launcher accepted the ablation only because its diff from rl.yaml was that one key.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S09  stability
# ------------------------------------------------------------------------------------------------------------------
def s09():
    sc = Scenario("S09", "Stability: the signals that predict a collapse", "Stability: the signals that predict a collapse",
                  "Run the RL recipe with a learning rate 25 times higher, as a one-key ablation. The health monitor, a separate Deployment reading "
                  "the tracking store, must raise trend alerts, and they must come no later than the reward collapse. The healthy run must raise none of them.")
    rid, _, _ = launch(sc, RECIPES / "ablations" / "rl-high-lr.yaml")
    STATE["rl_high_lr"] = rid
    save_state()
    wait_run(rid, 3600)
    time.sleep(40)  # one more pass of the monitor after the last step
    rows = metrics(rid)
    keys = ["reward", "entropy", "completions/mean_length", "grad_norm", "frac_reward_zero_std"]
    show(sc, f"runs/{rid}/metrics.jsonl   # every 4th step", "step " + " | ".join(k_.split("/")[-1] for k_ in keys) + "\n" +
         "\n".join(f"{r['step']:4d} " + " | ".join(f"{r.get(k_, float('nan')):.3g}" for k_ in keys) for r in rows if "reward" in r and (r["step"] % 4 == 1 or r["step"] == 40)))
    al = (s3_json(f"runs/{rid}/alerts.json") or {}).get("alerts", [])
    show(sc, f"aws s3 cp s3://<bucket>/runs/{rid}/alerts.json -   # written by the health monitor", "\n".join(json.dumps(a) for a in al) or "(no alerts)")
    logs = kq("-n", "platform", "logs", "deploy/health-monitor", "--tail=400")
    show(sc, f"kubectl -n platform logs deploy/health-monitor | grep {rid}", "\n".join(l for l in logs.splitlines() if rid in l)[:3000] or "(none)")
    early = [a for a in al if a["rule"] != "reward_collapse"]
    collapse = next((a for a in al if a["rule"] == "reward_collapse"), None)
    sc.expect("the monitor raised at least one trend alert on the high-learning-rate run", bool(early),
              "Rules compare the latest three steps with the run's first three: entropy, gradient norm, response length, importance ratios, KL.")
    sc.expect("the first trend alert came no later than the reward collapse (or there was no reward collapse within the run)",
              bool(early) and (collapse is None or min(a["step"] for a in early) <= collapse["step"]),
              "Entropy and the gradient norm move as soon as updates are too large; reward is a lagging signal.")
    # The same ablation at a learning rate 5 times the parent's instead of 25: a slower failure, reported as measured.
    mid, _, _ = launch(sc, RECIPES / "ablations" / "rl-high-lr.yaml", "--set", "hyperparameters.lr=1.0e-5",
                       display="ptl.py launch recipes/assistant-v1/ablations/rl-high-lr.yaml --set hyperparameters.lr=1.0e-5   # 5x instead of 25x")
    STATE["rl_mid_lr"] = mid
    save_state()
    wait_run(mid, 3600)
    time.sleep(40)
    al2 = (s3_json(f"runs/{mid}/alerts.json") or {}).get("alerts", [])
    show(sc, f"aws s3 cp s3://<bucket>/runs/{mid}/alerts.json -   # learning rate 1e-5", "\n".join(json.dumps(a) for a in al2) or "(no alerts)")
    first2 = min((a["step"] for a in al2 if a["rule"] != "reward_collapse"), default=None)
    coll2 = next((a["step"] for a in al2 if a["rule"] == "reward_collapse"), None)
    show(sc, "learning rate 1e-5: first trend alert vs reward collapse", f"first trend alert at step {first2}; reward collapse at step {coll2}")
    sc.expect("the monitor also flagged the slower failure", bool(al2), "The same rules, on a run that degrades over tens of steps instead of four.")
    sc.note(f"At learning rate 1e-5 the first trend alert came at step {first2} and the reward collapse rule fired at step {coll2}.")
    healthy = (s3_json(f"runs/{STATE['rl']}/alerts.json") or {}).get("alerts", [])
    show(sc, f"aws s3 cp s3://<bucket>/runs/{STATE['rl']}/alerts.json -   # the healthy production run", "\n".join(json.dumps(a) for a in healthy) or "(no alerts)")
    sc.expect("the healthy run raised no alert", healthy == [], "Its learning rate keeps every signal within the rules' bounds.")
    compare(sc, STATE["rl"], rid, note="healthy vs high learning rate")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S10  sandboxed rewards
# ------------------------------------------------------------------------------------------------------------------
HOSTILE = {
    "correct": "def solve(xs):\n    return sum(x for x in xs if x % 2 == 0)",
    "wrong": "def solve(xs):\n    return sum(xs)",
    "print_expected": "import sys\nprint('PASS'); print('{\"results\": [{\"ok\": 6}]}')\ndef solve(xs):\n    return -1",
    "exit_before_tests": "import os\nos._exit(0)",
    "infinite_loop": "def solve(xs):\n    while True:\n        pass",
    "memory_bomb": "def solve(xs):\n    big = bytearray(3 * 1024**3)\n    return 6",
    "internet": "import urllib.request\ndef solve(xs):\n    return len(urllib.request.urlopen('https://example.com', timeout=3).read())",
    "instance_metadata": "import urllib.request\ndef solve(xs):\n    return urllib.request.urlopen('http://169.254.169.254/latest/meta-data/', timeout=3).read().decode()",
    "kubernetes_api": "import urllib.request, ssl\ndef solve(xs):\n    return urllib.request.urlopen('https://kubernetes.default.svc', timeout=3, context=ssl._create_unverified_context()).status",
    "read_secrets": "import os, glob\ndef solve(xs):\n    return [len(os.environ), glob.glob('/var/run/secrets/**', recursive=True)]",
    "fill_disk": "def solve(xs):\n    open('big', 'wb').write(b'x' * 50_000_000)\n    return 6",
}


def s10():
    sc = Scenario("S10", "Rewards that run model-written code, in a sandbox", "Rewards and verifiers",
                  "Send the verifier pool completions written to cheat or to cause harm, from inside the post-training namespace as a trainer does: "
                  "print the expected output, exit early, loop forever, exhaust memory, reach the internet, the instance metadata service, the Kubernetes "
                  "API, read secrets, fill the disk. Only the correct solution may score. Then check the pool cannot be reached from elsewhere, and its verifiers' quirks.")
    tests = [{"args": [[1, 2, 3, 4]], "expected": 6}, {"args": [[]], "expected": 0}, {"args": [[5, 10, -2]], "expected": 8}]
    comps = [f"```python\n{c}\n```" for c in HOSTILE.values()]
    code = ("import json,urllib.request,time\n"
            f"comps={json.dumps(comps)}\nnames={json.dumps(list(HOSTILE))}\ntests={json.dumps(tests)}\n"
            "t0=time.time()\n"
            "r=json.loads(urllib.request.urlopen(urllib.request.Request('http://verifier.rewards.svc.cluster.local:8080/score',"
            "data=json.dumps({'kind':'code','completions':comps,'tests':[tests]*len(comps)}).encode(),headers={'Content-Type':'application/json'}),timeout=120).read())\n"
            "for n,s,d in zip(names,r['scores'],r['details']):\n"
            "    print(f\"{n:18s} score {s:.2f}  {d.get('reason')}  {(d.get('stderr') or '').strip().splitlines()[-1][:70] if d.get('stderr') else ''}\")\n"
            "print(f'answered by {r[\"pod\"]} in {time.time()-t0:.1f} s')\n")
    out, rc = run_in_cluster(sc, "kubectl -n post-training run probe --image=python:3.12-slim -- python -c '<POST 11 hostile completions to the verifier>'", code)
    lines = {l.split()[0]: l for l in out.splitlines() if l.strip() and l.split()[0] in HOSTILE}
    score = lambda n: float(re.search(r"score ([0-9.]+)", lines.get(n, "score 9")).group(1))
    sc.expect("only the correct solution scores 1.0", score("correct") == 1.0 and all(score(n) < 1.0 for n in HOSTILE if n != "correct"),
              "The child process only ever receives the test arguments; the expected outputs stay in the verifier, which compares.")
    sc.expect("printing the expected output and exiting before the tests both score 0", score("print_expected") == 0 and score("exit_before_tests") == 0,
              "Results are read only from a line that starts with a per-run random marker the candidate cannot know, and are then compared with the hidden expected values.")
    sc.expect("an infinite loop is killed at the time limit, and a memory bomb at the memory limit", "timeout" in lines.get("infinite_loop", "") and "memory" in lines.get("memory_bomb", ""),
              "A wall-clock timeout kills the child's process group; RLIMIT_AS caps its address space at 512 MiB.")
    sc.expect("no network: the internet, the instance metadata service and the Kubernetes API are all unreachable",
              all(score(n) == 0 for n in ("internet", "instance_metadata", "kubernetes_api")),
              "The rewards namespace's NetworkPolicy drops all egress, including DNS, so each call hangs until the time limit kills the child: nothing leaves, and nothing scores.")
    sc.expect("no secrets and no disk: the child sees an empty environment and no service-account token, and cannot write 50 MB",
              score("read_secrets") == 0 and score("fill_disk") < 1.0,
              "The child gets a two-variable environment; the pod has automountServiceAccountToken: false; RLIMIT_FSIZE is 1 MiB.")
    pod = kj("-n", "rewards", "get", "pods", "-l", "app=verifier").get("items", [{}])[0]
    show(sc, "kubectl -n rewards get pod <a verifier> -o jsonpath='{.spec.automountServiceAccountToken} {.spec.securityContext} {.spec.containers[0].securityContext}'",
         json.dumps({"automountServiceAccountToken": pod.get("spec", {}).get("automountServiceAccountToken"), "pod": pod.get("spec", {}).get("securityContext"),
                     "container": pod.get("spec", {}).get("containers", [{}])[0].get("securityContext")}))
    sc.expect("the verifier pod runs as a non-root user with a read-only root filesystem, no capabilities and no token",
              pod.get("spec", {}).get("automountServiceAccountToken") is False and pod["spec"]["containers"][0]["securityContext"].get("readOnlyRootFilesystem") is True,
              "Set in the Deployment; the namespace's restricted Pod Security level would reject the pod otherwise.")
    probe = "import urllib.request\ntry:\n    urllib.request.urlopen('http://verifier.rewards.svc.cluster.local:8080/healthz', timeout=5); print('REACHED')\nexcept Exception as e:\n    print('BLOCKED', type(e).__name__)"
    out2, _ = run_in_cluster(sc, "kubectl -n default run probe --image=python:3.12-slim -- python -c '<call the verifier from the default namespace>'", probe, ns="default")
    sc.expect("the verifier pool cannot be called from outside the run namespaces", "BLOCKED" in out2,
              "The NetworkPolicy's ingress rule admits only the post-training and platform namespaces.")
    mq = ("import json,urllib.request\n"
          "def s(kind, comps, sols):\n"
          "    return json.loads(urllib.request.urlopen(urllib.request.Request('http://verifier.rewards.svc.cluster.local:8080/score', data=json.dumps({'kind':kind,'completions':comps,'solutions':sols}).encode(), headers={'Content-Type':'application/json'})).read())['scores']\n"
          "c=['so \\\\boxed{0.5}', 'so \\\\boxed{1/2}', 'so \\\\boxed{\\\\frac{1}{2}}', 'the answer is 0.5', '\\\\boxed{2} or maybe \\\\boxed{0.5}', '\\\\boxed{3,000}']\n"
          "sol=['1/2']*5+['3000']\n"
          "print('completion'.ljust(34), 'math_exact (training)', 'math_any (held-out)')\n"
          "for x,a,b in zip(c, s('math_exact',c,sol), s('math_any',c,sol)): print(repr(x).ljust(34), str(a).ljust(21), b)\n")
    out3, _ = run_in_cluster(sc, "kubectl -n post-training run probe -- python -c '<score six answers with the training and the held-out math verifiers>'", mq)
    sc.expect("'1/2', '0.5' and '\\frac{1}{2}' are the same answer to both verifiers", out3.count("1.0") >= 6,
              "Both parse the answer into an exact fraction before comparing; a verifier that accepted only one spelling would teach the model the spelling.")
    sc.expect("the held-out verifier differs from the training one: it accepts an unboxed final number", "'the answer is 0.5'" in out3 and re.search(r"'the answer is 0.5'\s+0.0\s+1.0", out3) is not None,
              "math_exact needs a boxed answer; math_any falls back to the last number. A model that games one does not automatically game the other.")
    sc.expect("a hedge (two different boxed answers) scores nothing with either verifier", re.search(r"or maybe .{0,20}'\s+0.0\s+0.0", out3) is not None,
              "Both verifiers return 0 when the boxed values differ; otherwise 'box every candidate answer' would be a cheap way to collect reward.")
    return sc


# ------------------------------------------------------------------------------------------------------------------
# S11  distillation
# ------------------------------------------------------------------------------------------------------------------
def s11():
    sc = Scenario("S11", "Distillation: off-policy (teacher traces) and on-policy (teacher grades the student)", "Distillation: off-policy and on-policy",
                  "Distil Qwen2.5-3B-Instruct into the SFT model both ways on the same arithmetic prompts: SFT on the teacher's verified answers, and "
                  "on-policy reverse-KL distillation where the student writes and the teacher scores every token. Compare both with the student and the teacher.")
    tr, _, _ = launch(sc, RECIPES / "traces.yaml")
    STATE["traces"] = tr
    save_state()
    wait_run(tr, 2400)
    t = next((e for e in events(tr) if e and e.get("ev") == "traces"), {})
    show(sc, f"runs/{tr}/events: traces", f"prompts {t.get('prompts')}, teacher pass rate {t.get('teacher_pass_rate')}, verified traces kept {t.get('kept')} ({t.get('tokens_per_second')} tokens/s)")
    sc.expect("the teacher's answers were kept only when verified correct", t and 0 < t["kept"] <= t["prompts"], "Rejection sampling: four samples per prompt, the first one the verifier accepts is kept.")
    off, _, _ = launch(sc, RECIPES / "distill-offpolicy.yaml")
    on, _, _ = launch(sc, RECIPES / "distill-onpolicy.yaml")
    STATE["distill_off"], STATE["distill_on"] = off, on
    save_state()
    wait_run(off, 2400)
    wait_run(on, 3600)
    tok = event(on, "tokenizers")
    show(sc, f"runs/{on}/events: tokenizers", json.dumps(tok))
    sc.expect("teacher and student share a tokenizer, which per-token distillation needs", tok.get("same_vocabulary") is True,
              "distill.py compares the two vocabularies and the encoding of a probe string before it trains, and stops if they differ.")
    rows = metrics(on)
    show(sc, f"runs/{on}/metrics.jsonl   # on-policy: the loss is the per-token reverse KL to the teacher", "\n".join(fmt(r, ["step", "loss", "completions/mean_length", "entropy"]) for r in rows if r["step"] in (1, 5, 10, 15, 20, 25)))
    sc.expect("on-policy distillation lowered the student's divergence from the teacher", len(rows) >= 2 and rows[-1].get("loss", 9) < rows[0].get("loss", 0),
              "DistillationTrainer with beta = 1.0: the student generates, the teacher's next-token distribution scores each chosen token.")
    ev = evaluate(sc, {"teacher": TEACHER, "off-policy": f"registry://assistant-v1/sft@{off}", "on-policy": f"registry://assistant-v1/distill@{on}"})
    STATE.update(eval_teacher=ev["teacher"], eval_off=ev["off-policy"], eval_on=ev["on-policy"])
    save_state()
    compare(sc, STATE["sft"], off, on, "aa8e72537993ba99e69dfaafa59ed015b17504d1", note="student, off-policy, on-policy, teacher")
    st, tc = evals_of(STATE["sft"]).get("suites", {}), evals_of("aa8e72537993ba99e69dfaafa59ed015b17504d1").get("suites", {})
    sc.expect("the teacher is stronger at arithmetic than the student, so there was something to distil",
              tc.get("math", {}).get("accuracy", 0) > st.get("math", {}).get("accuracy", 1), "Measured, not assumed: same held-out suite.")
    words = {n: evals_of(m).get("suites", {}).get("math", {}).get("mean_words") for n, m in
             (("student", STATE["sft"]), ("off-policy", off), ("on-policy", on), ("teacher", "aa8e72537993ba99e69dfaafa59ed015b17504d1"))}
    show(sc, "mean words per maths answer (evals/by-model/*.json)", json.dumps(words))
    sc.expect("the teacher's habits came with it: both students now write longer maths answers than before", all(
        (words.get(n) or 0) > (words.get("student") or 1e9) for n in ("off-policy", "on-policy")),
              "The teacher writes long explanations; off-policy SFT imitates them directly, and on-policy distillation pulls the student's token distribution toward the teacher's.")
    smp = evals_of(on).get("samples", {}).get("math", [{}])
    show(sc, f"evals/by-model/{on}.json: a maths answer of the on-policy student", (smp[-1].get("answer", "") if smp else "")[:600])
    for r in (off, on):
        ptl(sc, "gate", r)
    return sc


SCENARIOS = {"S01": s01, "S02": s02, "S03": s03, "S04": s04, "S05": s05, "S06": s06, "S07": s07, "S08": s08, "S09": s09, "S10": s10, "S11": s11}


def save(sc):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{sc.sid}.txt").write_text(sc.transcript() + "\n")
    (OUT / f"{sc.sid}.json").write_text(json.dumps(sc.to_json(), indent=1) + "\n")
    blocks = []
    for s in sc.steps:
        blocks.append(("cmd", s.display))
        if s.output:
            blocks.append(("out", s.output))
    for c in sc.checks:
        blocks.append(("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}"))
    render_png(f"{sc.sid}: {sc.title}", blocks, OUT / f"{sc.sid}.png", max_lines=72)


def main():
    global OUT
    args = sys.argv[1:]
    if "--out" in args:
        i = args.index("--out")
        OUT = ROOT / args[i + 1]
        del args[i:i + 2]
    ids = list(SCENARIOS) if not args or args == ["all"] else args
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "state.json").exists():
        STATE.update(json.loads((OUT / "state.json").read_text()))
    results = {}
    for sid in ids:
        t0 = time.time()
        print(f"== {sid}", flush=True)
        try:
            sc = SCENARIOS[sid]()
        except Exception as e:  # record what was done before the failure
            sc = Scenario.last
            sc.expect(f"the scenario ran to the end (it raised {type(e).__name__}: {e})", False, "A crash is a failure; the steps recorded so far are kept.")
        save(sc)
        results[sid] = {"passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed], "seconds": round(time.time() - t0)}
        print(f"   {'PASS' if sc.ok else 'FAIL'}  {sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks  {results[sid]['seconds']} s", flush=True)
        for f in results[sid]["failed"]:
            print(f"     FAILED: {f}", flush=True)
    summ = OUT / "summary.json"
    old = json.loads(summ.read_text()) if summ.exists() else {}
    old.update(results)
    summ.write_text(json.dumps(old, indent=1) + "\n")


if __name__ == "__main__":
    main()

"""Scenarios S01 to S05: the platform, the verifier as a product, measured quality, fuzzing and the gate, code
verification in a sandbox."""
import json

from common_scen import STATE, job, k, save_state, show, vq
from harness import KUBECTL, Scenario
from helpers import info, kj, kq, last_json, sj, skeys

VERIFIERS = ["math-final-answer@1.0.0", "math-final-answer@2.0.0", "strict-reference-math@1.0.0", "eval-math@1.0.0",
             "code-tests@1.0.0", "code-tests@2.0.0", "strict-reference-code@1.0.0", "judge-single@1.0.0", "judge-pair@2.0.0"]
JUDGES = {"judge-a": ("Qwen/Qwen2.5-7B-Instruct", "a09a35458c702b33eeacc393d103063234e8bc28"),
          "judge-b": ("mistralai/Mistral-7B-Instruct-v0.3", "c170c708c41dac9275d15a8fff4eca08d52bab71")}


def svc(vid):
    return vid.replace("@", "-").replace(".", "-")


def post_verify(sc, vid, items, display):
    """POST /verify from inside one of the service's pods (stdin carries the request), recorded as one step."""
    code = ("import sys,urllib.request;r=urllib.request.Request('http://127.0.0.1:8080/verify',sys.stdin.buffer.read(),{'Content-Type':'application/json'});"
            "sys.stdout.write(urllib.request.urlopen(r,timeout=280).read().decode())")
    out, _ = sc.run(display, [KUBECTL, "-n", "rewards", "exec", "-i", f"deploy/{svc(vid)}", "--", "python", "-c", code], timeout=300, stdin=json.dumps({"items": items}))
    try:
        return json.loads(sc.raw)["results"]
    except (json.JSONDecodeError, KeyError):
        return []


def s01():
    sc = Scenario("S01", "The platform at rest: cluster, node pools, sandbox runtime, verifiers and judges", "The platform on Kubernetes",
                  "Check that the platform is what section 2 says before testing behaviour: a standard EKS cluster with a system pool, a GPU pool "
                  "and a gVisor sandbox pool; one Deployment per pinned verifier version; two judges of different families; the datasets; an "
                  "empty registry.")
    sc.aws("eks", "describe-cluster", "--name", "ds10-verify", "--query", "cluster.{status:status,version:version,autoMode:computeConfig.enabled}")
    c = json.loads(sc.raw or "{}")
    sc.expect("the cluster is ACTIVE on Kubernetes 1.37, standard EKS (Auto Mode off)", c.get("status") == "ACTIVE" and c.get("version") == "1.37" and not c.get("autoMode"),
              "Terraform creates a plain cluster with managed node groups and no compute configuration.")
    groups = {}
    for g in ("system", "gpu", "sandbox"):
        sc.aws("eks", "describe-nodegroup", "--cluster-name", "ds10-verify", "--nodegroup-name", f"ds10-verify-{g}",
               "--query", "nodegroup.{status:status,types:instanceTypes,ami:amiType,release:releaseVersion,size:scalingConfig.desiredSize,launchTemplate:launchTemplate.name}")
        groups[g] = json.loads(sc.raw or "{}")
    sc.kube("get", "nodes", "-L", "ds10/pool,ds10/runtime,node.kubernetes.io/instance-type,topology.kubernetes.io/zone")
    nodes = kj("get", "nodes").get("items", [])
    pools = [n["metadata"]["labels"].get("ds10/pool") for n in nodes]
    sc.expect("three node groups: 2 system, 3 GPU, 1 gVisor sandbox node with its own launch template",
              all(groups[g].get("status") == "ACTIVE" for g in groups) and pools.count("system") == 2 and pools.count("gpu") == 3 and pools.count("sandbox") == 1
              and groups["sandbox"].get("launchTemplate"), "The sandbox group's launch template installs gVisor at a pinned release and adds the runsc runtime to containerd.")
    sc.kube("get", "runtimeclass", "gvisor", "-o", "jsonpath={.handler} {.scheduling.nodeSelector}")
    sc.expect("the gvisor RuntimeClass maps to the runsc handler and the sandbox nodes", sc.raw.startswith("runsc") and "gvisor" in sc.raw,
              "Pods that name the class are scheduled onto the sandbox node and started by gVisor.")
    sc.kube("-n", "rewards", "get", "deploy", "-L", "verifier,version,team", "-o", "wide")
    deps = {d["metadata"]["name"]: d for d in kj("-n", "rewards", "get", "deploy").get("items", [])}
    ready = [v for v in VERIFIERS if deps.get(svc(v), {}).get("status", {}).get("readyReplicas") == 2]
    sc.expect("one Deployment per pinned verifier version, nine in all, each with two ready replicas", len(ready) == 9,
              "The platform layer creates one Deployment and one Service per name@version.")
    models = {}
    for j, (repo, rev) in JUDGES.items():
        sc.kube("-n", "judges", "exec", f"deploy/{j}", "--", "python3", "-c",
                "import urllib.request,sys;sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8000/v1/models').read().decode())",
                display=f"kubectl -n judges exec deploy/{j} -- curl -s 127.0.0.1:8000/v1/models")
        models[j] = sc.raw
    sc.kube("-n", "judges", "get", "deploy", "-o", "custom-columns=JUDGE:.metadata.name,MODEL:.spec.template.spec.containers[0].args[0],REVISION:.spec.template.spec.containers[0].args[2]")
    sc.expect("two judges of different model families serve under the name 'judge', each pinned to a commit",
              all('"judge"' in models[j] for j in JUDGES) and all(JUDGES[j][1] in sc.raw for j in JUDGES), "vLLM serves each pinned model on its own GPU.")
    idx = sj("data/index.json") or {}
    show(sc, "aws s3 cp s3://<bucket>/data/index.json -   # datasets and their sha256", "\n".join(f"{k:22s} {v['rows']:5d} rows  {v['sha256'][:16]}" for k, v in sorted(idx.items())))
    reg = skeys("registry/")
    show(sc, "aws s3 ls s3://<bucket>/registry/ --recursive", "(empty)" if not reg else "\n".join(o["Key"] for o in reg))
    sc.expect("eight datasets are published with their hashes, and the registry is empty", len(idx) == 8 and not reg, "A fresh build: no verifier has been measured yet.")
    return sc


def s02():
    sc = Scenario("S02", "A verifier is a product: contract, version, verdicts with reasons, and abstention", "A verifier is a product: contract, interface and lifecycle",
                  "Read what each verifier service says about itself, send the strict math verifier five completions that exercise its contract, "
                  "and try to start a training run on a verifier that has not been measured.")
    infos = {v: info(v) for v in VERIFIERS}
    show(sc, "for v in <the nine services>; do kubectl -n rewards exec deploy/$v -- GET 127.0.0.1:8080/info; done   # id, purpose, owner, code hash",
         "\n".join(f"{v:30s} {i.get('purpose', '?'):10s} {i.get('owner', '?'):13s} code {i.get('code_sha256', '?')[:16]}" for v, i in infos.items()))
    sc.expect("every service reports its own name@version, purpose, owner and a distinct code hash",
              all(infos[v].get("verifier") == v for v in VERIFIERS) and len({i.get("code_sha256") for i in infos.values()}) == 9,
              "The code hash covers the verifier class, its version and its source files.")
    i2 = infos["math-final-answer@2.0.0"]
    show(sc, "the contract of math-final-answer@2.0.0 (from /info)", i2.get("contract", ""))
    task = "m-ev-000"
    sol = [json.loads(l) for l in (sc_data("math_eval.jsonl"))][0]["solution"]
    items = [{"task_id": task, "completion": f"I add and subtract. So the result is \\boxed{{{sol}}}."},
             {"task_id": task, "completion": f"It is \\boxed{{{sol}}} or \\boxed{{{int(float(eval(sol))) + 1}}}."},
             {"task_id": task, "completion": f"So the result is \\boxed{{{sol}}}, or maybe a bit more."},
             {"task_id": task, "completion": f"So the result is \\boxed{{{sol} apples}}."},
             {"task_id": task, "completion": f"So the result is {sol}."}]
    res = post_verify(sc, "math-final-answer@2.0.0", items, "kubectl -n rewards exec -i deploy/math-final-answer-2-0-0 -- POST /verify < five-completions.json")
    verdicts = [r.get("verdict") for r in res]
    sc.expect("one boxed answer passes; two boxes, a hedge and a missing box fail, each with its reason",
              verdicts[:3] == ["pass", "fail", "fail"] and verdicts[4] == "fail" and "final_answers=2" in res[1]["reasons"] and "hedged_final_answer" in res[2]["reasons"]
              and "final_answers=0" in res[4]["reasons"], "The contract allows exactly one box with no hedge word after it.")
    sc.expect("an answer outside the accepted language abstains, with score 0, and every result names the verifier version",
              verdicts[3] == "abstain" and res[3]["score"] == 0.0 and all(r.get("verifier") == "math-final-answer@2.0.0" for r in res),
              "Units inside the box are not in the narrow language; abstaining gives no reward instead of guessing. reward() turns it into None.")
    out, code = vq(sc, "rl", "--run", "unmeasured", "--verifier", "math-final-answer@2.0.0", "--no-wait")
    sc.expect("a training run cannot pin a verifier that has no registry entry (exit 3)", code == 3 and "REFUSED" in out,
              "The launcher reads the registry before it writes the run's description or creates any Job.")
    return sc


def sc_data(name):
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / ".build" / "data" / name).read_text().splitlines()


QUALITY = [("math-final-answer@1.0.0", "math_gold.jsonl"), ("math-final-answer@2.0.0", "math_gold.jsonl"),
           ("strict-reference-math@1.0.0", "math_gold.jsonl"), ("eval-math@1.0.0", "math_gold.jsonl"),
           ("code-tests@1.0.0", "code_gold.jsonl"), ("code-tests@2.0.0", "code_gold.jsonl"), ("strict-reference-code@1.0.0", "code_gold.jsonl")]


def qkey(vid):
    return f"evals/quality/{svc(vid)}.json"


def s03():
    sc = Scenario("S03", "Measuring verifier quality: precision, recall and abstention per slice, with intervals", "Measuring verifier quality",
                  "Score the labelled gold sets with every rule-based verifier, old and new, and compute precision, recall and abstention per "
                  "slice with 95% intervals. The gold sets mix correct, wrong, adversarial, equivalent-form and cosmetic completions whose "
                  "correctness is known by construction.")
    for vid, gold in QUALITY:
        job(sc, f"q-{svc(vid)}"[:63], "quality", "--verifier", vid, "--gold", gold, "--out", qkey(vid),
            display=f"vq.py job q-{svc(vid)} -- quality --verifier {vid} --gold {gold}")
    Q = {vid: sj(qkey(vid)) or {} for vid, _ in QUALITY}
    lines = []
    for vid, q in Q.items():
        for sl, m in q.get("quality", {}).items():
            lines.append(f"{vid:28s} {sl:22s} n {m['n']:4d}  precision {m['precision']}  CI {m['precision_ci']}  recall {m['recall']}  abstain {m['abstain_rate']}")
    show(sc, "aws s3 cp s3://<bucket>/evals/quality/<each>.json -   # per slice", "\n".join(lines))
    a = lambda vid, sl="all": Q[vid].get("quality", {}).get(sl, {})
    sc.expect("the old math verifier accepts wrong answers: precision on the adversarial slice is near zero",
              (a("math-final-answer@1.0.0")["precision"] or 0) < 0.6 and a("math-final-answer@1.0.0", "adversarial").get("fp", 0) > 0,
              "It passes if ANY box is right, or the last number is right when there is no box: multiple answers and number soup pass.")
    sc.expect("the strict math verifier accepts nothing wrong, and the lower end of its 95% interval is above 0.95",
              a("math-final-answer@2.0.0").get("fp") == 0 and (a("math-final-answer@2.0.0").get("precision_ci") or [0])[0] >= 0.95,
              "Exactly one box, no hedge, SymPy equality; anything outside the narrow language abstains.")
    sc.expect("strictness costs recall: correct answers without a box are rejected, and boxed units abstain",
              a("math-final-answer@2.0.0")["recall"] < a("math-final-answer@1.0.0")["recall"] and (a("math-final-answer@2.0.0", "answer_with_units").get("abstain_rate") or 0) == 1.0,
              "The design accepts false negatives for fewer false positives, and states the contract in the prompt.")
    sc.expect("equivalent forms (0.5, 1/2, 2/4, \\frac{1}{2}) are all accepted by the strict verifier",
              a("math-final-answer@2.0.0", "equivalent_form").get("recall") == 1.0, "Equality is symbolic, not string matching.")
    sc.expect("the old code verifier passes printed-then-crashed, early-exit and hard-coded programs; the new one passes none and accepts every harmless rewrite",
              a("code-tests@1.0.0", "adversarial").get("fp", 0) > 0 and a("code-tests@2.0.0").get("fp") == 0 and a("code-tests@2.0.0", "cosmetic_variant").get("recall") == 1.0,
              "code-tests@1 compares printed output for the visible examples only; code-tests@2 runs hidden tests in new processes and checks the exit code.")
    STATE["quality"] = {vid: {"precision": a(vid)["precision"], "ci": a(vid)["precision_ci"], "recall": a(vid)["recall"]} for vid in Q}
    save_state()
    return sc


def s04():
    sc = Scenario("S04", "Hardening: differential fuzzing, a regression suite, and the training gate", "Hardening: fuzzing and differential testing",
                  "Fuzz the old and new math and code verifiers against the strict reference verifiers; every disagreement is a finding, and "
                  "every confirmed false positive becomes a permanent regression case. Then apply the training gate to each verifier and write "
                  "the registry, and try to pin a verifier the gate did not approve for training.")
    vids = "math-final-answer@1.0.0,math-final-answer@2.0.0,code-tests@1.0.0,code-tests@2.0.0"
    job(sc, "fuzz-static", "fuzz", "--verifiers", vids, "--policy-samples", "none", "--out", "fuzz/static")
    rep = sj("fuzz/static/report.json") or {"verifiers": {}}
    show(sc, "aws s3 cp s3://<bucket>/fuzz/static/report.json -   # per verifier",
         "\n".join(f"{v:26s} {r['cases']:4d} cases  {r['false_positives']:4d} false positives  {r['findings'] - r['false_positives']:3d} other findings  {r['by_mutation']}"
                   for v, r in rep["verifiers"].items()))
    R = rep["verifiers"]
    sc.expect("the old verifiers have false positives on every exploit family the design lists",
              R.get("math-final-answer@1.0.0", {}).get("false_positives", 0) > 0 and R.get("code-tests@1.0.0", {}).get("false_positives", 0) > 0
              and any(m.startswith("multi_answer:") for m in R["math-final-answer@1.0.0"]["by_mutation"]) and any(m.startswith("print_then_crash:") for m in R["code-tests@1.0.0"]["by_mutation"]),
              "Each mutation is a known exploit; the reference rejects it and the old verifier accepts it.")
    sc.expect("the new verifiers have no false positives and do not reject the equivalent forms the fuzzer must see accepted",
              R.get("math-final-answer@2.0.0", {}).get("false_positives") == 0 and R.get("code-tests@2.0.0", {}).get("false_positives") == 0
              and not any(f["must_pass"] for v in ("math-final-answer@2.0.0", "code-tests@2.0.0") for f in R[v]["examples"]),
              "Strict contracts in the new versions; differential testing confirms them against an independent implementation.")
    for vid in ("math-final-answer@1.0.0", "math-final-answer@2.0.0", "code-tests@1.0.0", "code-tests@2.0.0"):
        job(sc, f"rg-{svc(vid)}"[:63], "regress", "--verifier", vid, "--cases", "fuzz/static/regressions.jsonl", "--out", f"evals/regress/{svc(vid)}.json",
            display=f"vq.py job rg-{svc(vid)} -- regress --verifier {vid} --cases fuzz/static/regressions.jsonl")
    for vid, _ in QUALITY:
        args = ["register", vid, "--gold", qkey(vid), "--fuzz", "fuzz/static/report.json", "--regress", f"evals/regress/{svc(vid)}.json"]
        vq(sc, *args)
    vq(sc, "registry")
    reg = {e["verifier"]: e for e in (sj(o["Key"]) for o in skeys("registry/")) if e}
    sc.expect("the gate approves the new math and code verifiers and rejects the old ones, naming the failed checks",
              reg.get("math-final-answer@2.0.0", {}).get("status") == "approved" and reg.get("code-tests@2.0.0", {}).get("status") == "approved"
              and reg.get("math-final-answer@1.0.0", {}).get("status") == "rejected" and reg.get("code-tests@1.0.0", {}).get("status") == "rejected",
              "Precision lower bound, abstention, fuzzing false-positive rate and the regression suite, all from stored evidence.")
    sc.expect("the audit and evaluation verifiers are registered for their own purpose only", reg.get("strict-reference-math@1.0.0", {}).get("status") == "audit only"
              and reg.get("eval-math@1.0.0", {}).get("status") == "evaluation only" and reg["eval-math@1.0.0"]["owner"] == "eval-team",
              "Each class declares its purpose and owner; the gate refuses anything that is not a training verifier.")
    out, code = vq(sc, "rl", "--run", "pin-eval", "--verifier", "eval-math@1.0.0", "--no-wait")
    out2, code2 = vq(sc, "rl", "--run", "pin-old", "--verifier", "math-final-answer@1.0.0", "--no-wait")
    sc.expect("a training run can pin neither the evaluation verifier nor a rejected one (exit 3 both times)", code == 3 and code2 == 3,
              "pinned() checks purpose and status in the registry, and the code hash of the running service.")
    return sc


def s05():
    sc = Scenario("S05", "Code verification: hidden tests, new processes, gVisor, no network, and test strength", "Code verification: tests that are hard to fool",
                  "Look at where the code verifiers run and what they can reach, send the strict one programs that try to cheat in ways its static "
                  "scan does not see, and measure how strong each task's hidden tests are by mutation testing.")
    for vid in ("code-tests@1.0.0", "code-tests@2.0.0"):
        k(sc, "-n", "rewards", "get", "pods", "-l", f"vid={svc(vid)}", "-o", "custom-columns=POD:.metadata.name,RUNTIME:.spec.runtimeClassName,NODE:.spec.nodeName")
    k1, k2 = info("code-tests@1.0.0").get("kernel", ""), info("code-tests@2.0.0").get("kernel", "")
    show(sc, "the kernel each service sees (/proc/version, from /info)", f"code-tests@1.0.0  {k1}\ncode-tests@2.0.0  {k2}")
    sc.expect("the strict code verifier runs in gVisor's user-space kernel; the old one on the node's own kernel",
              k1 != k2 and "amzn" in k1 and "amzn" not in k2, "runtimeClassName: gvisor puts the pod on the sandbox node and starts it with runsc.")
    pod = kq("-n", "rewards", "get", "pods", "-l", "vid=code-tests-2-0-0", "-o", "jsonpath={.items[0].metadata.name}")
    k(sc, "-n", "rewards", "exec", pod, "--", "python", "-c",
      "import urllib.request\ntry:\n  urllib.request.urlopen('https://pypi.org', timeout=5); print('REACHED the internet')\nexcept Exception as e:\n  print('no internet:', type(e).__name__)",
      display=f"kubectl -n rewards exec {pod} -- python -c 'urllib.request.urlopen(\"https://pypi.org\", timeout=5)'")
    sc.expect("the verifier pod cannot reach the internet", "no internet" in sc.raw and "REACHED" not in sc.raw, "The rewards NetworkPolicy allows only DNS, the store, the judges and its own namespace.")
    params = "nums"
    tricks = {
        "read_tests_via_FileIO": f"def sum_evens({params}):\n    import io, json\n    rows = [json.loads(l) for l in io.FileIO('/tasks/code_tasks.jsonl').read().decode().splitlines()]\n    t = [r for r in rows if r['name'] == 'sum_evens'][0]\n    for c in t['hidden'] + t['edge']:\n        if c['args'] == [{params}]:\n            return c['expected']\n",
        "spin_forever": f"def sum_evens({params}):\n    while True:\n        pass\n",
        "huge_memory": f"def sum_evens({params}):\n    x = bytearray(2 * 1024 ** 3)\n    return 0\n",
        "fork_bomb_os": f"def sum_evens({params}):\n    import os\n    os.fork()\n    return sum(n for n in {params} if n % 2 == 0)\n",
        "correct": "def sum_evens(nums):\n    return sum(n for n in nums if n % 2 == 0)\n",
    }
    items = [{"task_id": "c-sum_evens", "completion": f"```python\n{c}```"} for c in tricks.values()]
    res = post_verify(sc, "code-tests@2.0.0", items, "kubectl -n rewards exec -i deploy/code-tests-2-0-0 -- POST /verify < tricks.json   # " + ", ".join(tricks))
    show(sc, "verdicts", "\n".join(f"{n:22s} {r.get('verdict')}  {r.get('reasons')}" for n, r in zip(tricks, res)))
    v = dict(zip(tricks, res))
    sc.expect("reading the hidden tests without the word 'open' is still refused, by the audit hook in the child process",
              v.get("read_tests_via_FileIO", {}).get("verdict") == "fail" and "sandbox_denied" in v["read_tests_via_FileIO"].get("reasons", []),
              "The static scan misses io.FileIO; the harness's audit hook raises on any open outside the scratch directory and the stdlib.")
    sc.expect("an endless loop, a 2 GB allocation and a fork all fail; the correct program passes",
              all(v.get(n, {}).get("verdict") == "fail" for n in ("spin_forever", "huge_memory", "fork_bomb_os")) and v.get("correct", {}).get("verdict") == "pass",
              "Each test runs in a new process with 4 s of CPU, 512 MB of address space and a 10 s wall clock; the audit hook refuses os.fork.")
    job(sc, "strength", "strength", "--out", "evals/strength.json")
    st = sj("evals/strength.json") or {}
    show(sc, "aws s3 cp s3://<bucket>/evals/strength.json -   # mutation score of each task's hidden tests",
         "\n".join(f"{t:20s} score {r['mutation_score']:.2f}  caught {r['caught']}  hidden+edge cases {r['hidden_cases']}  admitted {r['admitted']}" for t, r in st.items()))
    weak = [t for t, r in st.items() if r["weak_by_design"]]
    sc.expect("mutation testing flags the two tasks with thin tests and admits the rest (threshold 0.85)",
              len(st) == 8 and all(not st[t]["admitted"] for t in weak) and all(r["admitted"] for t, r in st.items() if t not in weak),
              "A mutant of the reference solution that passes every hidden test shows a hole in the tests, not in the verifier.")
    return sc

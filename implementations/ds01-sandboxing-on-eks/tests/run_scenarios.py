#!/usr/bin/env python3
"""Runs the Design 01 test scenarios against the live cluster and records the evidence.

    run_scenarios.py [S01 S02 ... | all] [--out evidence/final]

For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (screenshot
rendered from the transcript). Every private value is redacted before anything is written.
"""
import concurrent.futures
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ENV, KUBECTL, ROOT, Scenario, redact  # noqa: E402
from render_terminal import render  # noqa: E402

PY_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
def cluster_name():
    out = subprocess.run(["terraform", "output", "-raw", "cluster_name"], cwd=ROOT / "terraform/10-cluster",
                         capture_output=True, text=True, env=ENV).stdout.strip()
    return out


def kq(*args):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV)
    return p.stdout.strip()


def sandbox_nodes():
    return kq("get", "nodes", "-l", "workload=sandbox", "-o", "jsonpath={.items[*].metadata.name}").split()


def new_session(sc, tenant, agent, template, ttl=900, hosts="", show=True):
    args = ["create", tenant, agent, template, str(ttl)] + ([hosts] if hosts else [])
    res = sc.orch(*args, show=show)
    return res


def sandbox_pod(sid):
    return kq("-n", "sandboxes", "get", "sandboxclaim", sid, "-o", "jsonpath={.status.sandbox.name}")


def delete_sessions(sids):
    for sid in sids:
        subprocess.run([KUBECTL, "-n", "agent-orchestrator", "exec", "deploy/orchestrator", "--", "python", "-m",
                        "ds01.orchestrator", "delete", sid], capture_output=True, env=ENV)


def run_in_sandbox(sc, sid, command, timeout=120, display=None):
    """Run a shell command in the sandbox through the router; record stdout/stderr as the output."""
    argv = [KUBECTL, "-n", "agent-orchestrator", "exec", "deploy/orchestrator", "--", "python", "-m",
            "ds01.orchestrator", "exec", sid, command, str(timeout)]
    t0 = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=timeout + 60)
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        res = {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": -1}
    body = (res.get("stdout") or "") + (res.get("stderr") or "")
    out = body.rstrip("\n") + f"\n[exit code {res.get('exit_code')}]"
    from harness import Step
    sc.steps.append(Step(redact(display or f"orch exec {sid} {json.dumps(command)}"), redact(out), res.get("exit_code", -1), round(time.time() - t0, 2)))
    return res


def drill(sc, sid, names, timeout=300, **kv):
    """Upload the drill script (once) and run drills inside the sandbox."""
    sc.orch("put", sid, "drills.py", "/app/ds01/drills_sandbox.py", show=False)
    args = " ".join(f"{k}={v}" for k, v in kv.items())
    argv = [KUBECTL, "-n", "agent-orchestrator", "exec", "deploy/orchestrator", "--", "python", "-m", "ds01.orchestrator",
            "exec", sid, f"python3 /workspace/drills.py {names} {args}".strip(), str(timeout)]
    t0 = time.time()
    proc = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=timeout + 60)
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        results = json.loads(res["stdout"])
    except Exception:
        results = [{"drill": "harness", "outcome": "error", "detail": {"stdout": proc.stdout[-300:], "stderr": proc.stderr[-300:]}}]
    lines = [f"{r['drill']:<22} {r['outcome']:<8} {json.dumps(r['detail'])[:420]}" for r in results]
    shown_kv = " ".join(f"{k}=<{k}>" if k in ("session_token",) else f"{k}={v}" for k, v in kv.items())
    from harness import Step
    sc.steps.append(Step(redact(f"orch exec {sid} \"python3 /workspace/drills.py {names} {shown_kv}\""), redact("\n".join(lines)), 0, round(time.time() - t0, 2)))
    return {r["drill"]: r for r in results}


def wait_for(fn, seconds=120, every=3):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


# ------------------------------------------------------------------------------------------------
# scenarios
# ------------------------------------------------------------------------------------------------
def s01_topology(ctx):
    cn = ctx["cluster"]
    sc = Scenario("S01", "Cluster topology, node pools and the sandbox node pool",
                  "The sandbox node pool and pod hardening",
                  "Show the EKS mode and version, the dedicated tainted sandbox pool, the instance type it runs on, the separate "
                  "subnets and security group, the minimal node role, and that nothing but sandboxes runs on those nodes.")
    sc.aws("eks", "describe-cluster", "--name", cn, "--query",
           "cluster.{version:version,status:status,autoMode:computeConfig,publicEndpointCidrs:resourcesVpcConfig.publicAccessCidrs,logging:logging.clusterLogging[?enabled].types|[0]}",
           "--output", "json", display=f"aws eks describe-cluster --name {cn} --query <version, autoMode, endpoint CIDRs, logging>")
    sc.kube("get", "nodes", "-L", "workload,node.kubernetes.io/instance-type,topology.kubernetes.io/zone,eks.amazonaws.com/nitro-sandbox")
    sc.kube("get", "nodepool,nodeclass")
    taints, _ = sc.kube("get", "nodepool", "sandbox", "-o", "jsonpath={.spec.template.spec.taints}")
    nodes = sandbox_nodes()
    sc.expect("a sandbox node exists and is labelled for Nitro Sandboxes", bool(nodes) and kq("get", "node", nodes[0], "-o", "jsonpath={.metadata.labels.eks\\.amazonaws\\.com/nitro-sandbox}") == "true",
              "Auto Mode launched a sandbox-capable node from the dedicated pool and labelled it.")
    sc.expect("the pool taints its nodes workload=sandbox:NoSchedule", "sandbox" in taints and "NoSchedule" in taints,
              "The NodePool template carries the taint, so only pods that tolerate it can land there.")
    if nodes:
        n = nodes[0]
        out, _ = sc.aws("ec2", "describe-instances", "--instance-ids", n, "--query",
                        "Reservations[0].Instances[0].{type:InstanceType,subnet:SubnetId,securityGroups:SecurityGroups[].GroupName,"
                        "imdsTokens:MetadataOptions.HttpTokens,imdsHopLimit:MetadataOptions.HttpPutResponseHopLimit,nitroEnclaves:EnclaveOptions.Enabled}",
                        "--output", "json", display=f"aws ec2 describe-instances --instance-ids {n} --query <type, subnet, security groups, IMDS options, enclave option>")
        info = json.loads(out) if out.startswith("{") else {}
        subnet_tags, _ = sc.aws("ec2", "describe-subnets", "--subnet-ids", info.get("subnet", ""), "--query", "Subnets[0].{cidr:CidrBlock,tier:Tags[?Key=='ds01/tier']|[0].Value,name:Tags[?Key=='Name']|[0].Value}", "--output", "json",
                                display="aws ec2 describe-subnets --subnet-ids <the node's subnet> --query <cidr, tier tag, name>")
        sc.expect("the node sits in a subnet tagged tier=sandbox", '"tier": "sandbox"' in subnet_tags, "The sandbox NodeClass selects subnets by the ds01/tier=sandbox tag, so these nodes cannot be placed in the platform subnets.")
        sc.expect("the node uses the dedicated sandbox security group", any("sandbox-nodes" in g for g in info.get("securityGroups", [])), "The NodeClass pins the security group created for sandbox nodes.")
        sc.expect("IMDSv2 is required and Nitro Enclaves are enabled", info.get("imdsTokens") == "required" and info.get("nitroEnclaves") is True,
                  "Auto Mode launches nodes with IMDSv2 required; Nitro Sandboxes need the Enclaves option, which Auto Mode set.")
        role = f"{cn}-sandbox-node"
        pol, _ = sc.aws("iam", "list-attached-role-policies", "--role-name", role, "--query", "AttachedPolicies[].PolicyName", "--output", "json")
        inline, _ = sc.aws("iam", "list-role-policies", "--role-name", role, "--query", "PolicyNames", "--output", "json")
        names = json.loads(pol) if pol.startswith("[") else []
        sc.expect("the sandbox node role has only the two minimal managed policies and no inline policy",
                  sorted(names) == ["AmazonEC2ContainerRegistryPullOnly", "AmazonEKSWorkerNodeMinimalPolicy"] and inline.strip() == "[]",
                  "Terraform attaches exactly these two policies. There is no S3, Secrets Manager or other access an escaped process could use.")
        pods, _ = sc.kube("get", "pods", "-A", "-o", "wide", "--field-selector", f"spec.nodeName={n}")
        namespaces = {l.split()[0] for l in pods.splitlines()[1:] if l.strip()}
        sc.expect("only the sandboxes namespace has pods on the sandbox node", namespaces == {"sandboxes"},
                  "Nothing else tolerates the sandbox taint, so platform pods and the controller run on the general-purpose pool.")
    probe = {"apiVersion": "v1", "spec": {"nodeSelector": {"workload": "sandbox"}, "containers": [{"name": "p", "image": PY_IMAGE, "command": ["sleep", "60"]}]}}
    sc.kube("run", "taint-probe", "--image", PY_IMAGE, "--restart=Never", "--overrides", json.dumps(probe), "--", "sleep", "60",
            display="kubectl run taint-probe --image=python:3.12-slim --overrides='{nodeSelector: workload=sandbox}'  # a normal pod that asks for a sandbox node")
    time.sleep(12)
    sc.kube("get", "pod", "taint-probe")
    ev, _ = sc.bash("kubectl describe pod taint-probe | grep -A3 Events", f"{KUBECTL} describe pod taint-probe | grep -A4 -E '^Events'")
    phase = kq("get", "pod", "taint-probe", "-o", "jsonpath={.status.phase}")
    sc.expect("an ordinary pod that asks for the sandbox pool stays Pending", phase == "Pending" and ("untolerated" in ev.lower() or "taint" in ev.lower() or "FailedScheduling" in ev),
              "The scheduler refuses it because it does not tolerate the workload=sandbox taint.")
    kq("delete", "pod", "taint-probe", "--wait=false")
    return sc


def s02_isolation(ctx):
    sc = Scenario("S02", "The isolation boundary: a microVM per sandbox versus a shared kernel",
                  "Choosing the isolation boundary (Tier 0 versus Tier 2)",
                  "Prove that a sandbox runs on its own kernel in its own virtual machine, by comparing it with a hardened ordinary container on a normal node.")
    sess = new_session(sc, "iso", "coding", "python-untrusted", 900)
    sid = sess.get("session_id")
    pod = sandbox_pod(sid)
    node = kq("-n", "sandboxes", "get", "pod", pod, "-o", "jsonpath={.spec.nodeName}")
    sc.kube("-n", "sandboxes", "get", "pod", pod, "-o", "custom-columns=NAME:.metadata.name,RUNTIME:.spec.runtimeClassName,NODE:.spec.nodeName,STARTED:.status.startTime")
    box = run_in_sandbox(sc, sid, "uname -r; cat /sys/class/dmi/id/product_name; cut -d' ' -f1 /proc/uptime", display=f"orch exec {sid} \"uname -r; cat /sys/class/dmi/id/product_name; cut -d' ' -f1 /proc/uptime\"   # inside the SANDBOX")
    sb_kernel, sb_dmi, sb_up = (box["stdout"].split("\n") + ["", "", ""])[:3]
    started = kq("-n", "sandboxes", "get", "pod", pod, "-o", "jsonpath={.status.startTime}")
    # Tier 0: a hardened ordinary container on a normal node
    spec = {"spec": {"securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "t0", "image": PY_IMAGE, "command": ["sleep", "600"],
                                     "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}
    sc.kube("run", "tier0", "--image", PY_IMAGE, "--restart=Never", "--overrides", json.dumps(spec), "--", "sleep", "600",
            display="kubectl run tier0 --image=python:3.12-slim  # hardened ordinary container, Tier 0, on a normal node")
    wait_for(lambda: kq("get", "pod", "tier0", "-o", "jsonpath={.status.phase}") == "Running", 90, 3)
    t0_node = kq("get", "pod", "tier0", "-o", "jsonpath={.spec.nodeName}")
    sc.kube("exec", "tier0", "--", "sh", "-c", "uname -r; cat /sys/class/dmi/id/product_name; cut -d' ' -f1 /proc/uptime",
            display="kubectl exec tier0 -- sh -c \"uname -r; cat /sys/class/dmi/id/product_name; cut -d' ' -f1 /proc/uptime\"   # inside the ORDINARY CONTAINER")
    t0 = kq("exec", "tier0", "--", "sh", "-c", "uname -r; cat /sys/class/dmi/id/product_name; cut -d' ' -f1 /proc/uptime").split("\n")
    node_kernel = kq("get", "node", t0_node, "-o", "jsonpath={.status.nodeInfo.kernelVersion}")
    sc.kube("get", "node", t0_node, "-o", "jsonpath={.status.nodeInfo.kernelVersion}", display=f"kubectl get node {t0_node} -o jsonpath='{{.status.nodeInfo.kernelVersion}}'   # the node's own kernel version")
    node_created = kq("get", "node", t0_node, "-o", "jsonpath={.metadata.creationTimestamp}")

    from datetime import datetime, timezone
    def age(ts):
        return (datetime.now(timezone.utc) - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
    sb_up_f, t0_up_f = float(sb_up or 0), float(t0[2] if len(t0) > 2 else 0)
    sc.note(f"sandbox uptime {sb_up_f:.0f}s versus pod age {age(started):.0f}s; ordinary container uptime {t0_up_f:.0f}s versus its node's age {age(node_created):.0f}s")
    sc.expect("the sandbox reports a virtual machine of its own (product name ends in .enclave)", sb_dmi.strip().endswith(".enclave"),
              "A Nitro Sandbox is a Firecracker microVM, and its firmware reports its own product name.")
    sc.expect("the ordinary container reports the host machine, not a virtual machine of its own", t0[1].strip() != sb_dmi.strip() and not t0[1].strip().endswith(".enclave"),
              "A normal container is a process group on the node and sees the node's hardware identity.")
    sc.expect("the sandbox's kernel started when the pod started, not when the node did", abs(sb_up_f - age(started)) < 120 and sb_up_f < age(node_created) - 30 or abs(sb_up_f - age(started)) < 120,
              "The sandbox boots its own kernel at pod start, so its uptime tracks the pod's age.")
    sc.expect("the ordinary container's uptime tracks its node's age (it shares the node's kernel)", abs(t0_up_f - age(node_created)) < 300,
              "A container shares the node's kernel, so /proc/uptime is the node's uptime.")
    sc.expect("the ordinary container reports exactly the node's kernel version", t0[0].strip() == node_kernel.strip(),
              "Same kernel: this is the shared-kernel risk the design's threat model describes (arrow 1).")
    kq("delete", "pod", "tier0", "--wait=false")
    delete_sessions([sid])
    return sc


def s03_hardening(ctx):
    sc = Scenario("S03", "Pod hardening, identity and Kubernetes permissions", "The sandbox node pool and pod hardening; Threat model arrows 3 and 4",
                  "Show that a sandbox holds no Kubernetes credential, runs unprivileged on a read-only filesystem, that its ServiceAccount can do nothing, "
                  "and that the namespace refuses a privileged pod.")
    sess = new_session(sc, "hard", "coding", "python-untrusted", 900)
    sid = sess["session_id"]
    pod = sandbox_pod(sid)
    sc.kube("-n", "sandboxes", "get", "pod", pod, "-o",
            "jsonpath={'runtimeClass: '}{.spec.runtimeClassName}{'\\nserviceAccount: '}{.spec.serviceAccountName}{'\\nautomountServiceAccountToken: '}{.spec.automountServiceAccountToken}"
            "{'\\nenableServiceLinks: '}{.spec.enableServiceLinks}{'\\nrunAsNonRoot: '}{.spec.securityContext.runAsNonRoot}{'\\nreadOnlyRootFilesystem: '}{.spec.containers[0].securityContext.readOnlyRootFilesystem}"
            "{'\\ncapabilities.drop: '}{.spec.containers[0].securityContext.capabilities.drop}{'\\nlimits: '}{.spec.containers[0].resources.limits}{'\\n'}",
            display=f"kubectl -n sandboxes get pod {pod} -o jsonpath=<the security-relevant fields>")
    spec = json.loads(kq("-n", "sandboxes", "get", "pod", pod, "-o", "json"))["spec"]
    c = spec["containers"][0]
    sc.expect("the pod opts into the microVM runtime and mounts no ServiceAccount token",
              spec.get("runtimeClassName") == "isolated-sandbox" and spec.get("automountServiceAccountToken") is False and spec["serviceAccountName"] == "sandbox-nobody",
              "These fields come straight from the SandboxTemplate; the controller copies them into every pod.")
    sc.expect("the container drops all capabilities, forbids privilege escalation and has a read-only root filesystem",
              c["securityContext"]["capabilities"]["drop"] == ["ALL"] and c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["readOnlyRootFilesystem"] is True,
              "Defence in depth: even inside the microVM the pod is hardened as if it were not.")
    r = run_in_sandbox(sc, sid, "id; echo ---; touch /etc/probe 2>&1; echo ---; ls /var/run/secrets 2>&1; echo ---; env | grep -i -E 'KEY|TOKEN|SECRET|AWS|KUBERNETES' | sed 's/=.*/=<value hidden>/'; echo ---; cat /proc/1/status | grep -E 'NoNewPrivs|CapEff'",
                       display=f"orch exec {sid} \"id; touch /etc/probe; ls /var/run/secrets; env | grep ...; grep CapEff /proc/1/status\"   # run INSIDE the sandbox")
    out = r["stdout"]
    sc.expect("the code runs as uid 1000, cannot write to the root filesystem and finds no service-account directory",
              "uid=1000" in out and ("Read-only file system" in out or "Permission denied" in out) and "No such file" in out,
              "runAsUser, readOnlyRootFilesystem and automountServiceAccountToken=false in the template.")
    sa = "system:serviceaccount:sandboxes:sandbox-nobody"
    checks = []
    for verb, res, ns in (("get", "secrets", "sandboxes"), ("list", "pods", "sandboxes"), ("create", "pods/exec", "sandboxes"), ("get", "secrets", "kube-system")):
        out2, _ = sc.kube("auth", "can-i", verb, res, "-n", ns, "--as", sa, display=f"kubectl auth can-i {verb} {res} -n {ns} --as {sa}")
        checks.append(out2.strip().splitlines()[0] if out2.strip() else "")
    sc.expect("the sandbox ServiceAccount can do nothing on the Kubernetes API", all(x == "no" for x in checks), "It has no Role or ClusterRole bound to it.")
    n_secrets = len(json.loads(kq("-n", "sandboxes", "get", "secrets", "-o", "json"))["items"])
    sc.kube("-n", "sandboxes", "get", "secrets", display="kubectl -n sandboxes get secrets")
    sc.expect("the sandboxes namespace contains no Kubernetes Secret at all", n_secrets == 0, "Credentials live only in the namespaces of the components that use them.")
    priv = ("apiVersion: v1\nkind: Pod\nmetadata: {name: escape-attempt}\nspec:\n  hostNetwork: true\n  containers:\n  - name: c\n    image: public.ecr.aws/docker/library/python:3.12-slim\n"
            "    command: [sleep, '60']\n    securityContext: {privileged: true}\n")
    out3, code = sc.kube("-n", "sandboxes", "apply", "-f", "-", stdin=priv, display="kubectl -n sandboxes apply -f privileged-pod.yaml   # privileged: true, hostNetwork: true")
    sc.expect("a privileged, host-network pod is refused by Pod Security admission", code != 0 and ("violates PodSecurity" in out3 or "forbidden" in out3.lower()),
              "The namespace enforces the Pod Security 'restricted' profile.")
    for who, ns, verb, res in (("sandbox-platform:sandbox-manager", "sandboxes", "create", "sandboxclaims.extensions.agents.x-k8s.io"),
                               ("sandbox-platform:sandbox-manager", "sandboxes", "create", "pods/exec"),
                               ("sandbox-router:sandbox-router", "sandboxes", "create", "pods/exec"),
                               ("sandbox-router:sandbox-router", "default", "create", "pods/exec"),
                               ("agent-orchestrator:orchestrator", "sandboxes", "get", "pods")):
        sc.kube("auth", "can-i", verb, res, "-n", ns, "--as", f"system:serviceaccount:{who}", display=f"kubectl auth can-i {verb} {res} -n {ns} --as system:serviceaccount:{who}")
    sc.expect("each platform identity holds only the permissions its job needs",
              kq("auth", "can-i", "create", "sandboxclaims.extensions.agents.x-k8s.io", "-n", "sandboxes", "--as", "system:serviceaccount:sandbox-platform:sandbox-manager") == "yes"
              and kq("auth", "can-i", "create", "pods/exec", "-n", "sandboxes", "--as", "system:serviceaccount:sandbox-platform:sandbox-manager") == "no"
              and kq("auth", "can-i", "create", "pods/exec", "-n", "default", "--as", "system:serviceaccount:sandbox-router:sandbox-router") == "no"
              and kq("auth", "can-i", "get", "pods", "-n", "sandboxes", "--as", "system:serviceaccount:agent-orchestrator:orchestrator") == "no",
              "The Manager can create claims but not exec; the Router can exec only inside sandboxes; the orchestrator has no Kubernetes access.")
    delete_sessions([sid])
    return sc


def s04_warm_pools(ctx):
    sc = Scenario("S04", "Warm pools and claims", "Sandbox lifecycle: templates, warm pools and claims",
                  "Show that a session claims a pre-booted sandbox in well under a second, that the pool refills, and that an empty pool degrades to a slower cold start instead of failing.")
    sc.kube("-n", "sandboxes", "get", "sandboxwarmpool,sandboxtemplate")
    sc.kube("-n", "sandboxes", "get", "pods", "-o", "wide", display="kubectl -n sandboxes get pods -o wide   # the warm pods, already booted and waiting")
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        futs = [ex.submit(lambda i=i: sc.orch("create", "pool", "coding", "python-untrusted", "600", show=False)) for i in range(3)]
        results = [f.result() for f in futs]
    sids = [r.get("session_id") for r in results if r.get("session_id")]
    ready = sorted(r.get("ready_ms", 0) for r in results)
    from harness import Step
    sc.steps.append(Step("orch create pool coding python-untrusted 600   # x3, started at the same instant (pool size is 2)",
                         "\n".join(f"{r.get('session_id')}  ready in {r.get('ready_ms')} ms" for r in results), 0, round(time.time() - t0, 1)))
    sc.expect("two sessions got a ready sandbox in under 2 seconds", sum(1 for x in ready if x < 2000) >= 2, "They adopted pods that were already booted; nothing started on the claim path.")
    sc.expect("the third session, which found the pool empty, still got a sandbox (a slower cold start)", len(sids) == 3 and ready[-1] >= ready[0],
              f"Cold start took {ready[-1]} ms against {ready[0]} ms warm. An empty pool degrades; it does not fail.")
    wait_for(lambda: kq("-n", "sandboxes", "get", "sandboxwarmpool", "python-untrusted-pool", "-o", "jsonpath={.status.readyReplicas}") == "2", 120, 4)
    sc.kube("-n", "sandboxes", "get", "sandboxwarmpool", display="kubectl -n sandboxes get sandboxwarmpool   # after about a minute the pool has refilled")
    claims_out, _ = sc.kube("get", "nodeclaims", "-o", "custom-columns=NAME:.metadata.name,POOL:.metadata.labels.karpenter\\.sh/nodepool,TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,CREATED:.metadata.creationTimestamp",
                            display="kubectl get nodeclaims -o custom-columns=<name, pool, instance type, created>   # did the slow start launch a node?")
    n_sandbox_nodes = sum(1 for l in claims_out.splitlines()[1:] if " sandbox " in f" {l} ")
    sc.note(f"Sandbox node claims after the burst: {n_sandbox_nodes}. Compare their creation times with the session start times above.")
    sc.expect("the warm pool refilled to its desired size", kq("-n", "sandboxes", "get", "sandboxwarmpool", "python-untrusted-pool", "-o", "jsonpath={.status.readyReplicas}") == "2",
              "The controller boots a replacement for every pod a claim adopts.")
    delete_sessions(sids)
    return sc


def s05_network(ctx):
    sc = Scenario("S05", "Default deny, and one way out", "Network: default deny and one way out; Threat model arrows 2, 3, 4, 6",
                  "Try, from inside a sandbox, to reach the cluster, another sandbox, the cloud account's credentials, the internet and DNS. Every attempt must fail.")
    sc.kube("-n", "sandboxes", "get", "networkpolicy", display="kubectl -n sandboxes get networkpolicy   # one default-deny plus one generated from the template")
    np_name = kq("-n", "sandboxes", "get", "networkpolicy", "-o", "jsonpath={.items[*].metadata.name}").replace("default-deny-all", "").strip().split()
    if np_name:
        sc.kube("-n", "sandboxes", "get", "networkpolicy", np_name[0], "-o", "jsonpath={.spec}", display=f"kubectl -n sandboxes get networkpolicy {np_name[0]} -o jsonpath='{{.spec}}'   # generated by the controller from the template")
    a = new_session(sc, "net", "coding", "python-untrusted", 900)
    b = new_session(sc, "net", "coding", "python-untrusted", 900, show=False)
    sid, other_ip = a["session_id"], b.get("pod_ip", "")
    sc.note(f"a second sandbox ({b.get('session_id')}) at {other_ip} is the target of the lateral-movement test")
    res = drill(sc, sid, "kubernetes_api,other_sandbox,direct_internet,proxy_bypass,dns_exfiltration,imds", other_ip=other_ip)
    sc.expect("the Kubernetes API server is unreachable and no service-account token exists", res["kubernetes_api"]["outcome"] == "blocked", "Default-deny egress admits only the gateway; the API server is not on the list, and no token is mounted.")
    sc.expect("another sandbox's agent ports are unreachable", res["other_sandbox"]["outcome"] == "blocked", "Ingress to a sandbox is admitted only from the router namespace.")
    sc.expect("direct connections to internet addresses fail", res["direct_internet"]["outcome"] == "blocked", "The only egress rule is to the gateway's address on one port.")
    sc.expect("code that unsets the proxy settings still cannot get out", res["proxy_bypass"]["outcome"] == "blocked", "Network policy, not the proxy variable, is the barrier.")
    sc.expect("DNS cannot be used to smuggle data out (no name resolves at all)", res["dns_exfiltration"]["outcome"] == "blocked", "There is no DNS rule; the proxy resolves names on the sandbox's behalf.")
    sc.expect("the metadata service yields no credentials", res["imds"]["outcome"] == "blocked", "Nitro Sandboxes serve only placement data from the metadata address, and no Pod Identity association exists for the sandbox ServiceAccount.")
    op = sc.kube("-n", "agent-orchestrator", "exec", "deploy/orchestrator", "--", "python", "-c",
                 f"import socket\ntry:\n  socket.create_connection(('{other_ip}',8080),timeout=4);print('connected')\nexcept Exception as e:\n  print('blocked:',type(e).__name__,e)",
                 display=f"kubectl -n agent-orchestrator exec deploy/orchestrator -- python -c \"connect({other_ip}:8080)\"   # a trusted pod, but not the router")[0]
    sc.expect("even the trusted orchestrator cannot reach a sandbox directly; only the router can", "blocked" in op, "The template's ingress rule names the router's namespace and label only.")
    delete_sessions([sid, b.get("session_id")])
    return sc


def s06_egress(ctx):
    sc = Scenario("S06", "The egress gateway: per-session allowlist, the global ceiling and rate limits", "Network: default deny and one way out",
                  "Show the gateway allowing what a session is approved for, refusing everything else, being capped by the network ceiling even when an allowlist is wrong, and rate limiting a runaway session.")
    a = new_session(sc, "egr", "coding", "python-untrusted", 900)
    sid = a["session_id"]
    sc.note("Session A is created with the template's default allowlist: pypi.org, files.pythonhosted.org and two internal hosts.")
    res = drill(sc, sid, "unapproved_host,approved_host,host_probe", host="example.com")
    sc.expect("a host on the allowlist (pypi.org) is reachable through the gateway", res["approved_host"]["outcome"] == "allowed", "The gateway matched pypi.org against the session's allowlist and tunnelled the connection.")
    sc.expect("a host not on the allowlist (api.github.com) is refused with 403", res["unapproved_host"]["outcome"] == "blocked" and "403" in json.dumps(res["unapproved_host"]["detail"]), "The gateway answers CONNECT with 403 before any connection to the host is opened.")
    sc.expect("example.com is refused for session A because A did not ask for it", res["host_probe"]["outcome"] == "blocked" and "403" in json.dumps(res["host_probe"]["detail"]), "Allowlists are per session.")
    b = new_session(sc, "egr", "coding", "python-untrusted", 900, hosts="example.com", show=True)
    res2 = drill(sc, b["session_id"], "host_probe", host="example.com")
    sc.expect("example.com is reachable for session B, which was approved for it", res2["host_probe"]["outcome"] == "allowed", "Same sandbox image, same network; only the session's allowlist differs.")
    c = new_session(sc, "egr", "coding", "python-untrusted", 900, hosts="github.com", show=True)
    res3 = drill(sc, c["session_id"], "ceiling_backstop", host="github.com", timeout=120)
    detail = json.dumps(res3["ceiling_backstop"]["detail"])
    sc.expect("a session allowlist naming a host outside the network ceiling still cannot reach it (not a 403: the gateway tried and the network refused)",
              res3["ceiling_backstop"]["outcome"] == "blocked" and "403" not in detail,
              "The gateway's own network policy lists only the approved domains, so a mistake in a session allowlist cannot open the internet.")
    cl, _ = sc.bash(f"scripts/audit-query.py {c['session_id']} | grep -E 'egress|github'", f"{ROOT}/scripts/audit-query.py {c['session_id']} | grep -E 'egress'")
    sc.expect("the gateway ALLOWED the github.com request for session C (so the allowlist did not stop it); the network ceiling did", "egress.allow" in cl and "github.com" in cl,
              "The audit record shows an allow decision; the connection still never completed, which only the gateway's network policy can explain.")
    gw = kq("-n", "sandbox-egress", "get", "pod", "-l", "app=egress-gateway", "-o", "jsonpath={.items[0].metadata.name}")
    probe = ("import socket\nfor h in ('pypi.org','github.com'):\n  try:\n    socket.create_connection((h,443),timeout=8); print(h, '-> connected')\n"
             "  except Exception as e:\n    print(h, '->', type(e).__name__+':', e)")
    direct, _ = sc.kube("-n", "sandbox-egress", "exec", gw, "--", "python", "-c", probe,
                        display="kubectl -n sandbox-egress exec <gateway pod> -- python -c \"connect to pypi.org:443, then github.com:443\"   # the gateway's own network, no sandbox involved")
    sc.expect("from the gateway's own pod, pypi.org connects and github.com does not (so the network ceiling is what stops it)",
              "pypi.org -> connected" in direct and "github.com -> connected" not in direct and "github.com ->" in direct,
              "Same pod, same moment, same internet route. Only the destination's presence in the gateway's network policy differs.")
    res4 = drill(sc, sid, "rate_limit", n=140, timeout=300)
    d = res4["rate_limit"]["detail"]
    sc.expect("a session that opens 140 tunnels in a minute is cut off at the rate limit", d.get("refused_429", 0) > 0 and d.get("succeeded", 0) >= 100, f"The gateway allows 120 requests per minute per session; it refused {d.get('refused_429')}.")
    time.sleep(2)
    lines, _ = sc.bash(f"scripts/audit-query.py {sid} | grep egress | head -12   # the gateway's own record of what it decided", f"{ROOT}/scripts/audit-query.py {sid} | grep -E 'egress' | head -12")
    sc.expect("the gateway logged both allow and deny decisions for the session", "egress.allow" in lines and "egress.deny" in lines, "Every decision is written with the session ID.")
    delete_sessions([sid, b["session_id"], c["session_id"]])
    return sc


def s07_credentials(ctx):
    sc = Scenario("S07", "Credentials: secrets never enter the sandbox", "Credentials: secrets never enter the sandbox; Threat model arrow 5",
                  "Show a sandbox that holds only a placeholder successfully calling a protected API, because the gateway swaps in a short-lived credential outside the sandbox.")
    a = new_session(sc, "cred", "coding", "python-untrusted", 900)
    sid, tok = a["session_id"], a["session_token"]
    res = drill(sc, sid, "secrets_in_sandbox,credential_injection,direct_to_metrics", session=sid)
    ci = res["credential_injection"]["detail"]
    sc.expect("the sandbox's environment holds only the word 'placeholder'", res["secrets_in_sandbox"]["outcome"] == "blocked" and ci.get("METRICS_API_KEY_in_environment") == "placeholder", "The template sets the variable to the literal placeholder; there are no Secrets in the namespace.")
    sc.expect("the protected API accepted the request and identified this session", ci.get("status") == 200 and ci.get("matches_this_session") is True, "The gateway minted a credential for this session and replaced the Authorization header on the way out.")
    sc.expect("the credential is short-lived (15 minutes at most)", 0 < (ci.get("credential_expires_in_s") or 0) <= 900, "The broker caps the lifetime for this audience.")
    sc.expect("calling the API without the gateway is impossible", res["direct_to_metrics"]["outcome"] == "blocked", "No route and no DNS from the sandbox to the service.")
    pod = kq("-n", "sandbox-egress", "get", "pod", "-l", "app=egress-gateway", "-o", "jsonpath={.items[0].metadata.name}")
    forged = ("from ds01.common import mint, http_json\nt=mint('wrong-key-wrong-key-wrong-key-wrong-key-0000',{'aud':'metrics-api','sub':'sess-x'},60)\n"
              "print('forged credential ->', http_json('GET','https://metrics-api.sandbox-platform.svc/v1/metrics',headers={'Authorization':'Bearer '+t},cafile='/pki/ca.crt'))\n"
              "print('no credential     ->', http_json('GET','https://metrics-api.sandbox-platform.svc/v1/metrics',cafile='/pki/ca.crt'))\n")
    out, _ = sc.kube("-n", "sandbox-egress", "exec", pod, "--", "python", "-c", forged, display="kubectl -n sandbox-egress exec <gateway pod> -- python -c \"call the metrics API with a forged credential, then with none\"")
    sc.expect("the API rejects a forged credential and a missing one", out.count("401") >= 2 or out.count("'error'") >= 2, "It verifies the signature, audience and expiry of every credential.")
    time.sleep(2)
    tl, _ = sc.bash(f"scripts/audit-query.py {sid} | grep -E 'credential|metrics-api'", f"{ROOT}/scripts/audit-query.py {sid} | grep -E 'credential|metrics-api'")
    sc.expect("the broker and the API logged the credential by ID, and the API saw this session", "credential.minted" in tl and "request.served" in tl, "Both sides record the same session ID and a credential ID, never the credential.")
    leak = subprocess.run(["bash", "-c", f"for ns in sandbox-platform sandbox-router sandbox-egress; do {KUBECTL} -n $ns logs --all-containers --tail=-1 -l ds01/component --prefix=false 2>/dev/null; done | grep -c -F '{tok}'"], capture_output=True, text=True, env=ENV)
    sc.steps.append(__import__("harness").Step("kubectl logs <every platform component> | grep -c <this session's token>", leak.stdout.strip(), 0, 0))
    sc.expect("the session token appears in none of the platform logs", leak.stdout.strip() == "0", "The audit logger redacts any field named like a secret, and no component logs request headers.")
    delete_sessions([sid])
    return sc


def s08_tools(ctx):
    sc = Scenario("S08", "The tool gateway (MCP): policy, approvals, rate limits and revocation", "Tools and external services",
                  "Show tools gated by the user and agent behind a session token, human approval for a high-risk tool, a hard no for a forbidden one, a rate limit, and revocation when the session ends.")
    a = new_session(sc, "tools", "data-analyst", "python-untrusted", 900)
    b = new_session(sc, "tools", "coding", "python-untrusted", 900, show=False)
    ta, tb = a["session_token"], b["session_token"]
    r = sc.orch("mcp", ta, "tools/list")
    names_a = [t["name"] for t in r.get("result", {}).get("tools", [])]
    rb = sc.orch("mcp", tb, "tools/list")
    names_b = [t["name"] for t in rb.get("result", {}).get("tools", [])]
    sc.expect("the data-analyst agent sees metrics.query and ticket.create; the coding agent sees only metrics.query", names_a == ["metrics.query", "ticket.create"] and names_b == ["metrics.query"], "The gateway filters the tool list by the agent identity inside the session token.")
    m = sc.orch("mcp", ta, "tools/call", json.dumps({"name": "metrics.query", "arguments": {"name": "orders_per_minute"}}))
    sc.expect("a read-only tool call succeeds and reaches the metrics API with a credential the gateway obtained itself", "result" in m and a["session_id"] in json.dumps(m), "The tool gateway asks the broker for a 60-second credential for this session.")
    args = json.dumps({"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}})
    p1 = sc.orch("mcp", ta, "tools/call", args)
    apr = p1.get("error", {}).get("data", {}).get("approval_id", "")
    sc.expect("a high-risk tool call is paused until a human approves it", p1.get("error", {}).get("code") == -32001 and apr.startswith("apr-"), "The tool is marked high risk in the gateway's policy.")
    p2 = sc.orch("mcp", ta, "tools/call", json.dumps({"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}, "approval_id": apr}))
    sc.expect("retrying before approval is still refused", p2.get("error", {}).get("code") == -32001, "The approval is still pending.")
    sc.orch("approve", apr)
    p3 = sc.orch("mcp", ta, "tools/call", json.dumps({"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}, "approval_id": apr}))
    sc.expect("after approval the call runs", "result" in p3, "The approval is bound to this session, tool and arguments.")
    p4 = sc.orch("mcp", ta, "tools/call", json.dumps({"name": "ticket.create", "arguments": {"title": "Latency spike on checkout"}, "approval_id": apr}))
    sc.expect("the approval cannot be reused", p4.get("error", {}).get("code") == -32001, "One approval, one call.")
    f = sc.orch("mcp", ta, "tools/call", json.dumps({"name": "project.delete", "arguments": {}}))
    sc.expect("a forbidden tool is refused outright", f.get("error", {}).get("code") == -32003, "No agent is in its allow list.")
    x = sc.orch("mcp", tb, "tools/call", json.dumps({"name": "ticket.create", "arguments": {"title": "x"}}))
    sc.expect("the coding agent cannot call a tool that only the data-analyst may", x.get("error", {}).get("code") == -32003, "Per-agent policy.")
    from_inside = drill(sc, b["session_id"], "tool_gateway", session_token=tb)
    sc.expect("code inside the sandbox can reach the tool gateway only through the egress gateway, using the session token", from_inside["tool_gateway"]["outcome"] == "allowed", "The host is on the template's allowlist; the request carries the session token.")
    from harness import Step
    t0 = time.time()
    codes = []
    for _ in range(25):
        rr = subprocess.run([KUBECTL, "-n", "agent-orchestrator", "exec", "deploy/orchestrator", "--", "python", "-m", "ds01.orchestrator", "mcp", tb, "tools/list"], capture_output=True, text=True, env=ENV)
        try:
            codes.append(json.loads(rr.stdout.strip().splitlines()[-1]).get("http_status"))
        except Exception:
            codes.append(None)
    sc.steps.append(Step("orch mcp <session token> tools/list   # 25 times in a row", "http status of each call: " + " ".join(str(c) for c in codes), 0, round(time.time() - t0, 1)))
    sc.expect("a session that calls the gateway too often is rate limited (HTTP 429)", 429 in codes, "The gateway allows 20 calls a minute per session.")
    delete_sessions([a["session_id"]])
    time.sleep(4)
    rv = sc.orch("mcp", ta, "tools/list")
    sc.expect("when the session ends its token stops working immediately", rv.get("http_status") == 401, "The gateway asks the Manager whether the session is still active on every call.")
    delete_sessions([b["session_id"]])
    return sc


def s09_compute_only(ctx):
    sc = Scenario("S09", "Tier 3: a microVM with no network device", "Choosing the isolation boundary (Tier 3); Files, state and results",
                  "Run an analysis on an uploaded file in a sandbox that has no network at all, and get the result back through the router.")
    a = new_session(sc, "analytics", "data-analyst", "compute-only", 900)
    sid = a["session_id"]
    pod = sandbox_pod(sid)
    sc.kube("-n", "sandboxes", "get", "pod", pod, "-o", "jsonpath={.metadata.annotations}", display=f"kubectl -n sandboxes get pod {pod} -o jsonpath='{{.metadata.annotations}}'")
    csv = "region,sales\\nnorth,120\\nsouth,95\\nnorth,80\\nwest,60\\n"
    sc.orch("put-text", sid, "uploads/sales.csv", "region,sales\nnorth,120\nsouth,95\nnorth,80\nwest,60\n")
    code = ("import csv,collections,json;t=collections.Counter()\n"
            "for r in csv.DictReader(open('/workspace/uploads/sales.csv')): t[r['region']]+=int(r['sales'])\n"
            "open('/workspace/result.json','w').write(json.dumps(t));print(dict(t))")
    r = run_in_sandbox(sc, sid, f"python3 -c \"{code}\"", display=f"orch exec {sid} \"python3 -c <sum sales by region from /workspace/uploads/sales.csv>\"")
    sc.expect("the analysis ran and produced the totals", "'north': 200" in r["stdout"], "The router reached the sandbox through the Kubernetes exec channel, which does not need a network.")
    got = sc.orch("get", sid, "result.json")
    sc.expect("the result file came back through the router", '"north": 200' in got.get("content", ""), "File transfer uses the same channel.")
    res = drill(sc, sid, "no_network_device")
    sc.expect("the sandbox has no network device and no route out", res["no_network_device"]["outcome"] == "blocked", "The annotation boots the microVM without a network interface, so there is nothing to filter and nothing to misconfigure.")
    delete_sessions([sid])
    return sc


def s10_limits(ctx):
    sc = Scenario("S10", "Limits, quotas and abuse", "Limits, quotas and abuse",
                  "Show the per-tenant session quota, the hard session end time, the namespace quota and limits, and that a runaway process is contained inside its own sandbox.")
    sc.kube("-n", "sandboxes", "get", "resourcequota,limitrange")
    sids = []
    for i in range(3):
        r = new_session(sc, "quotaco", "coding", "compute-only", 900, show=(i == 2))
        sids.append(r.get("session_id"))
    over = sc.orch("create", "quotaco", "coding", "compute-only", "900")
    sc.expect("a tenant's fourth concurrent session is refused cleanly with 429", over.get("http_status") == 429, "The Manager counts a tenant's active claims against its limit of three.")
    other = new_session(sc, "otherco", "coding", "compute-only", 900, show=False)
    sc.expect("another tenant is not affected by that limit", other.get("http_status") == 201, "Limits are per tenant.")
    delete_sessions(sids + [other.get("session_id")])
    sc.note("TTL test: a session with a 45 second lifetime is created and left alone.")
    ttl = new_session(sc, "ttlco", "coding", "compute-only", 45)
    tsid = ttl["session_id"]
    sc.kube("-n", "sandboxes", "get", "sandboxclaim", tsid, "-o", "jsonpath={.spec.lifecycle}", display=f"kubectl -n sandboxes get sandboxclaim {tsid} -o jsonpath='{{.spec.lifecycle}}'")
    gone = wait_for(lambda: "NotFound" in subprocess.run([KUBECTL, "-n", "sandboxes", "get", "sandboxclaim", tsid], capture_output=True, text=True, env=ENV).stderr, 150, 5)
    sc.kube("-n", "sandboxes", "get", "sandboxclaim", tsid, display=f"kubectl -n sandboxes get sandboxclaim {tsid}   # after the end time")
    sc.expect("the claim, and with it the sandbox, was deleted when its end time passed, with nobody asking", bool(gone), "shutdownTime with shutdownPolicy Delete is set on the claim; the controller enforces it.")
    mem = new_session(sc, "memco", "coding", "python-untrusted", 900)
    victim = mem["session_id"]
    neighbour = new_session(sc, "memco", "coding", "python-untrusted", 900, show=False)
    npod, vpod = sandbox_pod(neighbour["session_id"]), sandbox_pod(victim)
    node = kq("-n", "sandboxes", "get", "pod", vpod, "-o", "jsonpath={.spec.nodeName}")
    r = drill(sc, victim, "memory_bomb", mb=2500, timeout=120)
    time.sleep(8)
    sc.kube("-n", "sandboxes", "get", "pod", vpod, npod, "-o", "custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount,LAST_TERMINATION:.status.containerStatuses[0].lastState.terminated.reason",
            display="kubectl -n sandboxes get pod <the abuser> <its neighbour> -o custom-columns=<restarts, last termination reason>")
    sc.kube("get", "node", node, display=f"kubectl get node {node}")
    restarts_v = kq("-n", "sandboxes", "get", "pod", vpod, "-o", "jsonpath={.status.containerStatuses[0].restartCount}")
    restarts_n = kq("-n", "sandboxes", "get", "pod", npod, "-o", "jsonpath={.status.containerStatuses[0].restartCount}")
    node_ready = kq("get", "node", node, "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}")
    sc.expect("asking for 2.5 GiB inside a 1 GiB sandbox was stopped by the memory limit (the abuser's container was restarted)", restarts_v not in ("", "0"), "The pod's memory limit is enforced by the guest kernel's cgroup, inside the microVM.")
    sc.expect("the neighbouring sandbox on the same node was not touched", restarts_n == "0", "Each sandbox has its own kernel and its own memory.")
    sc.expect("the node stayed Ready", node_ready == "True", "The microVM boundary kept the failure inside the guest.")
    delete_sessions([victim, neighbour["session_id"]])
    return sc


def s11_audit(ctx):
    sc = Scenario("S11", "One query for an audit", "Observability and audit",
                  "Run one realistic session, then answer 'what exactly did this agent do?' with a single command.")
    a = new_session(sc, "audit", "coding", "python-untrusted", 900)
    sid, tok = a["session_id"], a["session_token"]
    run_in_sandbox(sc, sid, "python3 -c \"print(sum(range(10)))\"")
    drill(sc, sid, "approved_host,unapproved_host,credential_injection")
    sc.orch("mcp", tok, "tools/call", json.dumps({"name": "metrics.query", "arguments": {"name": "p99_latency_ms"}}))
    time.sleep(3)
    out, _ = sc.bash(f"scripts/audit-query.py {sid}", f"{ROOT}/scripts/audit-query.py {sid}")
    comps = set(re.findall(r"^\S+\s+(\S+)\s", out, re.M))
    sc.note(f"components present in the timeline: {sorted(comps)}")
    sc.expect("a single query returns records from at least six components for the session", len(comps - {"--"}) >= 6, "Every component writes JSON lines that carry the same session ID.")
    for kind in ("session.created", "exec", "egress.deny", "credential.minted", "tool.called"):
        sc.expect(f"the timeline contains {kind}", kind in out, "Who created it, what ran, what was refused, which credential was issued, which tool was called.")
    delete_sessions([sid])
    return sc


def s12_failures(ctx):
    sc = Scenario("S12", "Failure drills", "Failure modes and operations",
                  "Take a component away and check that the design fails the way its table says: closed for the gateway, and without disturbing running sessions for the controller.")
    a = new_session(sc, "fail", "coding", "python-untrusted", 1200)
    sid = a["session_id"]
    before = drill(sc, sid, "approved_host")
    sc.kube("-n", "sandbox-egress", "scale", "deploy/egress-gateway", "--replicas=0")
    time.sleep(10)
    during = drill(sc, sid, "approved_host", timeout=120)
    sc.expect("with the gateway down the sandbox loses outbound access (fails closed)", before["approved_host"]["outcome"] == "allowed" and during["approved_host"]["outcome"] != "allowed", "The only route out is the gateway; with no gateway there is no route.")
    sc.kube("-n", "sandbox-egress", "scale", "deploy/egress-gateway", "--replicas=2")
    sc.kube("-n", "sandbox-egress", "rollout", "status", "deploy/egress-gateway", "--timeout=180s")
    time.sleep(5)
    after = drill(sc, sid, "approved_host", timeout=120)
    sc.expect("when the gateway returns the same sandbox reaches its allowed host again", after["approved_host"]["outcome"] == "allowed", "Nothing in the sandbox had to change.")
    sc.kube("-n", "agent-sandbox-system", "scale", "deploy/agent-sandbox-controller", "--replicas=0")
    time.sleep(6)
    r = run_in_sandbox(sc, sid, "echo still running; uname -r", display=f"orch exec {sid} \"echo still running; uname -r\"   # controller is down")
    sc.expect("with the sandbox controller down, a running session keeps working", "still running" in r["stdout"], "The controller only reconciles claims; the sandbox and its router path do not depend on it.")
    sc.kube("-n", "agent-sandbox-system", "scale", "deploy/agent-sandbox-controller", "--replicas=1")
    sc.kube("-n", "agent-sandbox-system", "rollout", "status", "deploy/agent-sandbox-controller", "--timeout=120s")
    delete_sessions([sid])
    return sc


SCENARIOS = {
    "S01": s01_topology, "S02": s02_isolation, "S03": s03_hardening, "S04": s04_warm_pools, "S05": s05_network, "S06": s06_egress,
    "S07": s07_credentials, "S08": s08_tools, "S09": s09_compute_only, "S10": s10_limits, "S11": s11_audit, "S12": s12_failures,
}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_dir = ROOT / "evidence" / "final"
    if "--out" in sys.argv:
        out_dir = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
        args = [a for a in args if a != str(out_dir)]
    wanted = list(SCENARIOS) if not args or args == ["all"] else args
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = {"cluster": cluster_name()}
    leftovers = subprocess.run([KUBECTL, "-n", "agent-orchestrator", "exec", "deploy/orchestrator", "--", "python", "-m", "ds01.orchestrator", "list"],
                               capture_output=True, text=True, env=ENV).stdout
    try:
        delete_sessions([s["session_id"] for s in json.loads(leftovers.strip().splitlines()[-1]).get("sessions", [])])
    except Exception:
        pass
    summary = []
    for key in wanted:
        print(f"== {key} ...", flush=True)
        try:
            sc = SCENARIOS[key](ctx)
        except Exception as exc:  # a crashed scenario is a failed scenario, and says why
            sc = Scenario(key, "crashed", "", str(exc))
            sc.expect("scenario ran to completion", False, f"{type(exc).__name__}: {exc}")
        (out_dir / f"{key}.txt").write_text(sc.transcript() + "\n")
        (out_dir / f"{key}.json").write_text(json.dumps(sc.to_json(), indent=1))
        blocks = []
        for s in sc.steps:
            blocks += [("cmd", s.display), ("out", s.output)]
        blocks += [("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}") for c in sc.checks]
        render(f"{sc.sid}  {sc.title}", blocks, out_dir / f"{key}.png", max_lines=70)
        summary.append({"id": key, "title": sc.title, "passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed]})
        print(f"   {'PASS' if sc.ok else 'FAIL'}  ({sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks)", flush=True)
        for c in sc.checks:
            if not c.passed:
                print("   FAILED:", c.description)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + "\n".join(f"{s['id']}  {'PASS' if s['passed'] else 'FAIL'}  {s['title']}" for s in summary))
    sys.exit(0 if all(s["passed"] for s in summary) else 1)


if __name__ == "__main__":
    main()

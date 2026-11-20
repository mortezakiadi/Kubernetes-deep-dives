#!/usr/bin/env python3
"""Runs the Design 04 test scenarios against the live cluster and records the evidence.
    run_scenarios.py [S01 S02 ... | all] [--out evidence/final]
For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (screenshot rendered
from the transcript). Every private value is redacted before anything is written.
"""
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ENV, KUBECTL, ROOT, Scenario, Step, redact  # noqa: E402
from helpers import *  # noqa: E402,F401,F403
from render_terminal import render as render_png  # noqa: E402

TASK = {"ticket": "T-1", "test_seconds": 3}


def k(sc, *args, display=None, **kw):
    return sc.run(display or ("kubectl " + " ".join(args)), [KUBECTL, *args], **kw)


def cleanup_all():
    """Between scenarios: no run left running, no fault left switched on, workers back to two."""
    cli("model-fault", "--seconds", "0", "--latency", "0")
    cli("git", "ack-loss:pulls", "0")
    cli("git", "ack-loss:branches", "0")
    cli("git", "ack-loss:comments", "0")
    cli("git", "ack-loss:deletes", "0")
    cli("ops", "queue", "--queue", "agent-v1")
    kq("-n", NS, "scale", "deploy/worker-v2", "--replicas=0")
    kq("-n", NS, "scale", "deploy/worker-v1", "--replicas=2")
    kq("-n", NS, "annotate", "scaledobject/worker-v1", "autoscaling.keda.sh/paused=true", "--overwrite")
    rc, obj = cli("ops", "list")
    for r in obj.get("runs", []):
        if r.get("status") == "RUNNING":
            cli("signal", r["session"], "cancel")
    wait_for(lambda: not [r for r in cli("ops", "list")[1].get("runs", []) if r.get("status") == "RUNNING"], 120, 4)
    kq("-n", "sandboxes", "delete", "pods", "--all", "--wait=false")
    cli("git", "reset")


# --------------------------------------------------------------------------------------------------------------
# S01  the platform at rest
# --------------------------------------------------------------------------------------------------------------
def s01_platform():
    sc = Scenario("S01", "The platform at rest: engine, stores, workers and gateways", "The architecture at a glance",
                  "Show what was built: the durable execution engine and its Multi-AZ database, the stateless workers, the gateways, the sandbox manager and the state stores, with nothing held by any run.")
    sc.aws("eks", "describe-cluster", "--name", PROJECT, "--query", "cluster.[name,status,version]", "--output", "text",
           display=f"aws eks describe-cluster --name {PROJECT} --query 'cluster.[name,status,version]' --output text")
    sc.expect("the cluster is ACTIVE on Kubernetes 1.37", "ACTIVE" in sc.raw and "1.37" in sc.raw, "Terraform created one EKS Auto Mode cluster at the pinned minor version.")
    sc.aws("rds", "describe-db-instances", "--db-instance-identifier", f"{PROJECT}-db", "--query",
           "DBInstances[0].[DBInstanceStatus,MultiAZ,Engine,EngineVersion,DBInstanceClass,AvailabilityZone,SecondaryAvailabilityZone]", "--output", "text",
           display=f"aws rds describe-db-instances --db-instance-identifier {PROJECT}-db --query 'DBInstances[0].[Status,MultiAZ,Engine,Version,Class,AZ,StandbyAZ]'")
    parts = sc.raw.split()
    sc.expect("the database is available, Multi-AZ, with a standby in a different zone", parts[:2] == ["available", "True"] and len(parts) >= 7 and parts[5] != parts[6],
              "One PostgreSQL instance holds the engine's histories and the session database. RDS keeps a synchronous standby in another zone.")
    k(sc, "get", "pods", "-n", "temporal", display="kubectl -n temporal get pods   # the durable execution engine")
    t = kj("-n", "temporal", "get", "deploy")
    ready = {d["metadata"]["name"]: d["status"].get("readyReplicas", 0) for d in t.get("items", [])}
    sc.expect("the engine runs two frontend, two history and two matching pods, and one worker service pod",
              ready.get("temporal-frontend") == 2 and ready.get("temporal-history") == 2 and ready.get("temporal-matching") == 2 and ready.get("temporal-worker") == 1,
              "Temporal's chart is installed with two replicas of each stateful service, so losing one pod is survivable (S10).")
    desc = kq("-n", "temporal", "exec", "deploy/temporal-admintools", "--", "temporal", "operator", "cluster", "describe")
    show(sc, "kubectl -n temporal exec deploy/temporal-admintools -- temporal operator cluster describe", desc)
    sc.expect("the engine reports itself serving, on PostgreSQL for both histories and search", "postgres12" in desc and "active" in desc, "The server answers on its own API and names its two stores.")
    k(sc, "get", "pods", "-n", NS, "-o", "wide", display="kubectl -n agent-platform get pods -o wide   # workers, gateways, session API, sandbox manager")
    k(sc, "-n", "external", "get", "pods", display="kubectl -n external get pods   # the stand-in for the external system the agent changes")
    d = kj("-n", NS, "get", "deploy")
    r = {x["metadata"]["name"]: x["status"].get("readyReplicas", 0) for x in d.get("items", [])}
    sc.expect("two stateless workers, and two each of the model gateway, tool gateway, session API and sandbox manager, are ready",
              r.get("worker-v1") == 2 and r.get("model-gateway") == 2 and r.get("tool-gateway") == 2 and r.get("session-api") == 2 and r.get("sandbox-manager") == 2,
              "Each is a Deployment. Workers hold no run; the gateways keep their state in the database.")
    k(sc, "get", "pvc", "-A", display="kubectl get pvc -A   # does any component keep state on a disk?")
    sc.expect("no component keeps state on a volume", "No resources found" in sc.raw, "Everything a run needs is in the engine's database, the session database or S3. Replacing a pod loses nothing.")
    classes = agent(sc, "classes", display="agentctl classes   # how each tool is made safe to retry (tool gateway registry)", fmt=lambda o: "\n".join(f"{k_:20s} {v}" for k_, v in o.items()))
    sc.expect("every tool is classified: local, read-only, idempotent, checkable or unsafe", {"local", "read_only", "idempotent", "checkable", "unsafe"} <= set(classes.values()),
              "The class is part of a tool's registration in the gateway. It decides how a retry of the same step is made safe (S05).")
    k(sc, "-n", "sandboxes", "get", "pods", display="kubectl -n sandboxes get pods   # sandboxes at rest")
    sc.expect("no sandbox exists while no run needs one", "No resources found" in sc.raw, "A sandbox is created for a step that needs files and deleted while a run waits.")
    k(sc, "-n", NS, "get", "scaledobject", display="kubectl -n agent-platform get scaledobject   # workers scale on the engine's task queue backlog")
    sc.expect("the workers' autoscaler watches the engine's task queue, not CPU", "worker-v1" in sc.raw and "True" in sc.raw, "KEDA's Temporal scaler reads each task queue's backlog from the engine (S11).")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S02  one run, five kinds of state
# --------------------------------------------------------------------------------------------------------------
def s02_five_states():
    sc = Scenario("S02", "One run through the five kinds of state", "Five kinds of agent state; The architecture at a glance",
                  "Run a coding agent from ticket to finished pull request, stopping where it waits for a reviewer, and find each kind of its state in the place the design says it lives.")
    cli("git", "reset")
    session = start({**TASK, "can_every": 5})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 3, \"can_every\": 5}'", f"session {session}")
    st = wait_state(session, "waiting", 240)
    agent(sc, "status", session, fmt=fmt_status, display=f"agentctl status {session}   # the run waits for a reviewer")
    sc.expect("the run reached the review step and is waiting for a person", bool(st) and st.get("state") == "waiting" and st.get("step") == 6, "The agent decided to read the reviewer's comment, which needs approval, so the workflow waits on a signal.")
    k(sc, "-n", "sandboxes", "get", "pods", display="kubectl -n sandboxes get pods   # what a waiting run holds")
    pods_waiting = sandbox_pods()
    sc.expect("while it waits, the run holds no sandbox", not pods_waiting, "The workflow deleted the sandbox after the last snapshot and before waiting.")
    # 1 execution state
    chain = agent(sc, "chain", session, display=f"agentctl chain {session}   # execution state: the engine's runs for this session",
                  fmt=lambda o: "\n".join(f"run {r['run_id']}  {r['status']:10s} {r['history_length']} events" for r in o.get("runs", [])))
    sc.expect("the engine holds the run's history, and kept it short by continuing as new", len(chain.get("runs", [])) >= 2 and all(r["history_length"] < 250 for r in chain["runs"]),
              "Every five steps the workflow ends its history and starts a fresh one with only the session, the step and the snapshot. The context stays in the database.")
    # 2 context state
    rows = sql(f"SELECT role, COUNT(*) AS n, SUM(tokens) AS tokens FROM context_log WHERE session='{session}' GROUP BY role ORDER BY role")
    show(sc, "SELECT role, COUNT(*), SUM(tokens) FROM context_log WHERE session = <this run> GROUP BY role   # context state", "\n".join(f"{r['role']:10s} {r['n']:>3} entries  {r['tokens']} tokens" for r in rows))
    sc.expect("the context log holds the ticket, the model's answers and the tools' results", {r["role"] for r in rows} == {"user", "assistant", "tool"}, "Each step appends to an append-only log in the session database. Prompts are rebuilt from it.")
    # 3 working state
    snaps = sql(f"SELECT step, snapshot_id, files, uploaded, skipped FROM snapshots WHERE session='{session}' ORDER BY step")
    show(sc, "SELECT step, snapshot_id, files, uploaded, skipped FROM snapshots   # working state: one snapshot per file-changing step",
         "step  snapshot          files uploaded skipped\n" + "\n".join(f"{r['step']:>4}  {r['snapshot_id']}  {r['files']:>5} {r['uploaded']:>8} {r['skipped']:>7}" for r in snaps))
    objs = s3_list("snapshots/")
    show(sc, "aws s3 ls s3://<bucket>/snapshots/ --recursive   # the manifests and file objects in object storage", "\n".join(f"{o['Key']}  {o['Size']} bytes" for o in objs[:12]))
    sc.expect("each file-changing step left a snapshot in S3, and a file that did not change was not uploaded again", len(snaps) == 2 and snaps[1]["files"] == 2 and snaps[1]["skipped"] >= 1 and len(objs) >= 4,
              "A snapshot is a manifest of file hashes, and a file is uploaded only if its hash is new. The second snapshot had two files and skipped the one the first already stored. (If an earlier run stored the same bytes, nothing is uploaded at all.)")
    # 4 external effects
    led = agent(sc, "ledger", session, fmt=fmt_ledger, display=f"agentctl ledger {session}   # external effects: the side-effect ledger")
    counts = git_counts()
    show(sc, "agentctl git counts   # what the external system has", json.dumps(counts))
    sc.expect("the branch and the pull request are in the ledger and exist once in the external system", counts.get("open_prs") == 1 and counts.get("branches") == 1 and len(led.get("ledger", [])) == 2,
              "The tool gateway records each external effect before and after making it.")
    # 5 process memory
    show(sc, "# process memory: variables in a worker, open connections", "Not saved anywhere. Nothing a later step needs lives only in a worker's memory: the next step rebuilds its prompt from the database and\ntakes its position from the history.")
    # finish
    approve(session)
    done = wait_state(session, "completed", 240)
    agent(sc, "calls", session, fmt=fmt_calls, display=f"agentctl calls {session}   # every call the model gateway received for this run")
    cs = calls(session)
    sc.expect("the run completed after the reviewer's approval", bool(done) and done.get("state") == "completed", "The approval is a signal to the waiting workflow. It continued from the recorded state.")
    steps = [c["step"] for c in cs if c["kind"] == "act"]
    sc.expect("the model was called exactly once for each of the eleven decisions", len(steps) == 11 and sorted(set(steps)) == list(range(11)), "One recorded activity per decision. Continuing as new twice did not repeat any.")
    tl = agent(sc, "timeline", session, fmt=lambda o: fmt_timeline(o, only={"call_model", "run_tool", "snapshot_workspace"}), display=f"agentctl timeline {session}   # the run's last history: activity by activity")
    counts = git_counts()
    sc.expect("one pull request, one branch, and two comments (the reviewer's and the agent's) exist", counts == {"open_prs": 1, "all_prs": 1, "branches": 1, "comments": 2}, "Each effect happened once.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S03  a worker is lost mid-step
# --------------------------------------------------------------------------------------------------------------
def s03_worker_loss():
    sc = Scenario("S03", "A worker dies in the middle of a step", "Steps, history and replay; Failure modes",
                  "Kill, with no warning, the worker that is running a long step. Another worker must take the run over, the finished steps must not be paid for again, and then take away every worker at once.")
    cli("git", "reset")
    session = start({**TASK, "test_seconds": 40})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 40}'", f"session {session}")
    found = wait_for(lambda: pending_worker(session)[1] and pending_worker(session)[1]["activity"] == "run_tool" and status(session).get("step") == 3, 120, 2)
    pod, act = pending_worker(session)
    agent(sc, "status", session, fmt=fmt_status, display=f"agentctl status {session}   # step 3 (the test run) is in flight, on one worker")
    show(sc, "the worker running it, from the engine's own record", f"{pod}")
    sc.expect("the test step is running on a worker, in flight", bool(found) and bool(pod), "The engine tracks which worker last reported on each pending activity.")
    t0 = time.time()
    k(sc, "-n", NS, "delete", "pod", pod or "none", "--grace-period=0", "--force", display=f"kubectl -n agent-platform delete pod {pod} --grace-period=0 --force   # SIGKILL: no warning, no cleanup")
    st = wait_state(session, "waiting", 240)
    took = time.time() - t0
    agent(sc, "status", session, fmt=fmt_status, display=f"agentctl status {session}")
    sc.expect("the run carried on to the review step on another worker, with nobody touching it", bool(st) and st.get("state") == "waiting" and st.get("step") == 6,
              "The engine noticed the activity's heartbeat had stopped, and scheduled it again for another worker.")
    show(sc, "time from the kill to the run reaching the review step", f"{took:.0f} seconds (the rest of the 40-second test, plus the detection delay)")
    tl = agent(sc, "timeline", session, fmt=lambda o: fmt_timeline(o, only={"call_model", "run_tool"}), display=f"agentctl timeline {session}")
    attempts = {e["activity"] + str(i): e["attempts"] for i, e in enumerate(tl.get("events", [])) if "attempts" in e}
    run_tools = [e for e in tl.get("events", []) if e.get("activity") == "run_tool"]
    sc.expect("the interrupted step shows a second attempt in the history", any(e["attempts"] >= 2 for e in run_tools), "The history records which attempt finally completed, and why the earlier one failed.")
    cs = calls(session)
    per_step = {}
    for c in cs:
        if c["kind"] == "act":
            per_step[c["step"]] = per_step.get(c["step"], 0) + 1
    agent(sc, "calls", session, fmt=fmt_calls, display=f"agentctl calls {session}   # the model gateway's record")
    sc.expect("no model call was repeated: one call for each of steps 0 to 6", sorted(per_step) == list(range(7)) and all(v == 1 for v in per_step.values()), "Replay returns the recorded answer for every step that finished. Only the unfinished tool step ran again.")
    # take away all workers
    approve(session)
    wait_state(session, "completed", 240)
    cli("git", "reset")
    s2 = start({**TASK, "test_seconds": 25})
    show(sc, "agentctl start --task '{\"ticket\": \"T-2\", \"test_seconds\": 25}'", f"session {s2}")
    wait_for(lambda: status(s2).get("step") == 3 and pending_worker(s2)[1], 120, 2)
    kq("-n", NS, "scale", "deploy/worker-v1", "--replicas=0")
    k(sc, "-n", NS, "scale", "deploy/worker-v1", "--replicas=0", display="kubectl -n agent-platform scale deploy/worker-v1 --replicas=0   # every worker gone, mid-step")
    time.sleep(5)
    wait_for(lambda: not worker_pods(), 120, 3)
    k(sc, "-n", NS, "get", "pods", "-l", "app=worker-v1", display="kubectl -n agent-platform get pods -l app=worker-v1")
    time.sleep(45)
    mid = status(s2)
    show(sc, f"agentctl status {s2}   # with no worker at all, for 45 seconds", fmt_status(mid))
    sc.expect("with no worker running, the run still exists in the engine", mid.get("workflow_status") == "RUNNING", "The run is a row of history in PostgreSQL, not a process.")
    scale_workers(2)
    k(sc, "-n", NS, "get", "pods", "-l", "app=worker-v1", display="kubectl -n agent-platform get pods -l app=worker-v1   # two new workers, in new pods")
    st2 = wait_state(s2, "waiting", 300)
    agent(sc, "status", s2, fmt=fmt_status, display=f"agentctl status {s2}")
    sc.expect("two brand-new workers took the run over and carried it to the review step", bool(st2) and st2.get("step") == 6, "Any worker can continue any run: nothing the run needed was in the workers that were removed.")
    cs2 = [c for c in calls(s2) if c["kind"] == "act"]
    sc.expect("the model was still called exactly once per step for that run", sorted(c["step"] for c in cs2) == list(range(7)), "The new workers replayed the history without calling the model.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S04  the model provider fails
# --------------------------------------------------------------------------------------------------------------
def s04_provider_errors():
    sc = Scenario("S04", "The model provider fails", "Failure modes; Gateways",
                  "Make the primary model provider unavailable. A run with a fallback must carry on through the fallback. A run without one must retry with backoff and recover on its own. And an operator must be able to pause every run, wait out an incident with no model call made, and resume them at a limited rate.")
    cli("git", "reset")
    # a) fallback
    out = agent(sc, "model-fault", "--seconds", "600", display="agentctl model-fault --seconds 600   # the primary provider is down for ten minutes")
    s1 = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'   # fallback allowed (the default)", f"session {s1}")
    st = wait_state(s1, "waiting", 240)
    cs = calls(s1)
    agent(sc, "calls", s1, fmt=fmt_calls, display=f"agentctl calls {s1}   # which backend served each call")
    backends = {c["backend"] for c in cs if c["status"] == 200}
    sc.expect("with the primary down, the run reached the review step through the fallback", bool(st) and st.get("step") == 6 and backends == {"fallback"},
              "The gateway sends the call to the fallback model when the primary does not answer. The run never saw an error.")
    sc.expect("not one call was refused", all(c["status"] == 200 for c in cs), "Every row the gateway recorded for the run is a success.")
    approve(s1)
    wait_state(s1, "completed", 240)
    # b) no fallback: retries
    cli("git", "reset")
    agent(sc, "model-fault", "--seconds", "25", display="agentctl model-fault --seconds 25   # primary down for 25 seconds only")
    t0 = time.time()
    s2 = start({**TASK, "test_seconds": 2, "allow_fallback": False})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2, \"allow_fallback\": false}'   # no fallback allowed", f"session {s2}")
    wait_for(lambda: (status(s2).get("step") or 0) >= 1, 120, 2)
    took = time.time() - t0
    cs2 = calls(s2)
    agent(sc, "calls", s2, fmt=fmt_calls, display=f"agentctl calls {s2}   # refused calls, then the one that worked")
    refused = [c for c in cs2 if c["status"] == 503]
    step0 = [c for c in cs2 if c["step"] == 0 and c["kind"] == "act"]
    sc.expect("the first step was refused several times before it succeeded", len(refused) >= 3 and step0[-1]["status"] == 200,
              "A refused call is a failed activity. The engine runs it again after 2, 4, 8 seconds and so on, until the provider answers.")
    sc.expect("the retries were spaced out, not a burst", took >= 20, f"The run needed {took:.0f} seconds to get past step 0, which is how long the provider was down.")
    tl = timeline(s2)
    show(sc, f"agentctl timeline {s2}   # the engine's record of the retried call", fmt_timeline(tl, only={"call_model"}))
    sc.expect("the history shows the model call taking more than one attempt", any(e.get("activity") == "call_model" and e.get("attempts", 1) >= 3 for e in tl.get("events", [])),
              "The attempt count and the last failure are kept in the history, so an operator can see why a step was slow.")
    wait_state(s2, "waiting", 240)
    approve(s2)
    wait_state(s2, "completed", 240)
    # c) incident: pause everything, wait, resume at a rate
    cli("git", "reset")
    sessions = [start({**TASK, "test_seconds": 70}) for _ in range(5)]
    show(sc, "agentctl start ...   # five runs in flight, each running its tests", "\n".join(sessions))
    agent(sc, "model-fault", "--seconds", "900", display="agentctl model-fault --seconds 900   # the provider goes down again")
    pa = agent(sc, "ops", "pause-all", display="agentctl ops pause-all   # the operator pauses every running run")
    sc.expect("all five runs were signalled", pa.get("paused") == 5, "One call to the session API signals every running workflow.")
    got = [wait_state(s, "paused", 180) for s in sessions]
    rows = [status(s) for s in sessions]
    show(sc, "agentctl status <each run>", "\n".join(f"{s}  {fmt_status(r)}" for s, r in zip(sessions, rows)))
    sc.expect("each run finished the step it was on and then stopped", all(got) and all(r.get("state") == "paused" for r in rows), "A pause is checked between steps, so no step is left half done.")
    before = sql("SELECT COUNT(*) AS n FROM model_calls WHERE session IN (" + ",".join(f"'{s}'" for s in sessions) + ")")[0]["n"]
    time.sleep(25)
    after = sql("SELECT COUNT(*) AS n FROM model_calls WHERE session IN (" + ",".join(f"'{s}'" for s in sessions) + ")")[0]["n"]
    show(sc, "SELECT COUNT(*) FROM model_calls WHERE session IN (the five runs)   # now, and 25 seconds later", f"now: {before}\n25 seconds later: {after}")
    sc.expect("while paused, the provider received no call from any of them", before == after, "A paused run is waiting on a signal, so nothing calls the gateway: the incident is not made worse by the retries.")
    agent(sc, "model-fault", "--seconds", "0", display="agentctl model-fault --seconds 0   # the provider is back")
    res = agent(sc, "ops", "resume-all", "--rate", "1", display="agentctl ops resume-all --rate 1   # resume them one per second")
    sc.expect("the runs were resumed one per second, not all at once", res.get("resumed") == 5 and res.get("seconds", 0) >= 4, f"Five runs at one per second took {res.get('seconds')} seconds to release.")
    done = [wait_state(s, "waiting", 240) for s in sessions]
    sc.expect("every run then carried on to the review step", all(done), "They continued from the step they had paused at.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S05  side effects happen once where it matters
# --------------------------------------------------------------------------------------------------------------
def s05_side_effects():
    sc = Scenario("S05", "Side effects happen once", "Side effects: exactly once where it matters",
                  "Make the external Git host lose its answer after it has already made a change, which is the case a retry cannot tell from a failure. Show first what an unprotected tool does, then the same fault against the protected tools, then a tool that cannot be protected.")
    cli("git", "reset")
    agent(sc, "classes", fmt=lambda o: "\n".join(f"{k:20s} {v}" for k, v in sorted(o.items(), key=lambda kv: (kv[1], kv[0]))),
          display="agentctl classes   # how each tool's retries are made safe")
    # a) control
    agent(sc, "git", "ack-loss:pulls", "1", display="agentctl git ack-loss:pulls 1   # the Git host will lose its answer to the next pull request it opens")
    s1 = start({**TASK, "test_seconds": 2, "naive_pr": True})
    show(sc, "agentctl start --task '{... \"naive_pr\": true}'   # control: the agent uses a tool with no protection", f"session {s1}")
    wait_state(s1, "waiting", 240)
    counts = agent(sc, "git", "counts", display="agentctl git counts")
    led = agent(sc, "ledger", s1, fmt=fmt_ledger, display=f"agentctl ledger {s1}")
    sc.expect("control: the unprotected tool opened two pull requests for one decision", counts.get("all_prs") == 2 and counts.get("open_prs") == 2,
              "The first request made the pull request but its answer was lost. The retry could not know, and made another.")
    sc.expect("control: the ledger saw both attempts but had no way to stop the second", any(r["tool"] == "open_pr_naive" and r["attempts"] == 2 and r["deduplicated"] == 0 for r in led.get("ledger", [])),
              "A ledger only helps if the tool's class gives it a way to check.")
    cli("signal", s1, "cancel")
    wait_state(s1, "cancelled", 120)
    cli("git", "reset")
    # b) protected, a lost answer for every kind of change
    for op in ("branches", "pulls", "comments"):
        agent(sc, "git", f"ack-loss:{op}", "1", display=f"agentctl git ack-loss:{op} 1")
    s2 = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'   # the same task, protected tools", f"session {s2}")
    wait_state(s2, "waiting", 240)
    approve(s2)
    done = wait_state(s2, "completed", 240)
    led2 = agent(sc, "ledger", s2, fmt=fmt_ledger, display=f"agentctl ledger {s2}   # attempts 2 and deduplicated 1: the retry found the work already done")
    counts2 = agent(sc, "git", "counts", display="agentctl git counts")
    by = {r["tool"]: r for r in led2.get("ledger", [])}
    sc.expect("the run completed despite three lost answers", bool(done) and done.get("state") == "completed", "Each lost answer was retried and each retry was made safe.")
    sc.expect("one branch, one pull request and two comments exist (the reviewer's and the agent's)", counts2 == {"open_prs": 1, "all_prs": 1, "branches": 1, "comments": 2},
              "Nothing was duplicated, although each of the three changes was attempted twice.")
    sc.expect("the branch and the pull request were found by looking first", by["push_branch"]["attempts"] == 2 and by["push_branch"]["deduplicated"] == 1 and by["open_pr"]["attempts"] == 2 and by["open_pr"]["deduplicated"] == 1,
              "Checkable tools ask the outside world 'is it already there?' before repeating a change.")
    sc.expect("the comment was deduplicated by the Git host, through the key", by["comment_on_pr"]["attempts"] == 2 and counts2["comments"] == 2,
              "The retry carried the same Idempotency-Key as the first attempt, because the key is built from the session and the step, not the attempt.")
    cli("git", "reset")
    # c) unsafe
    sid = "s05-unsafe"
    agent(sc, "git", "ack-loss:deletes", "1", display="agentctl git ack-loss:deletes 1")
    kq("-n", NS, "exec", "deploy/tester", "--", "python", "-c",
       f"from agentplatform.common import http, EXTERNAL_GIT; print(http('POST', EXTERNAL_GIT + '/repos/app/branches', {{'name': 'agent/{sid}-4'}}))")
    show(sc, "# a production branch exists, and the agent has been given a tool that deletes it", f"branch agent/{sid}-4 created ({git_counts().get('branches')} branch on the Git host)")
    start({**TASK, "behavior": "unsafe"}, session=sid)
    show(sc, f"agentctl start --task '{{... \"behavior\": \"unsafe\"}}' --session {sid}", f"session {sid}")
    st = wait_state(sid, "needs_attention", 240)
    agent(sc, "status", sid, fmt=fmt_status, display=f"agentctl status {sid}")
    led3 = agent(sc, "ledger", sid, fmt=fmt_ledger, display=f"agentctl ledger {sid}")
    c3 = git_counts()
    show(sc, "agentctl git counts", json.dumps(c3))
    sc.expect("the run stopped and asked for a person instead of repeating the delete", bool(st) and "cannot be checked" in (st.get("attention") or ""),
              "The tool is classed 'unsafe': it cannot be keyed and cannot be looked up, so a second attempt after an unknown outcome is refused.")
    time.sleep(15)
    led4 = cli("ledger", sid)[1].get("ledger", [])
    sc.expect("it was attempted twice and not a third time", [r["attempts"] for r in led4 if r["tool"] == "delete_branch_prod"] == [2], "The ledger counts attempts: the first made the change, the second was stopped before reaching the Git host.")
    sc.expect("the delete did happen once, as the first attempt's lost answer suggested", c3.get("branches") == 0, "The platform cannot undo it, so it says what it knows and waits.")
    cli("signal", sid, "cancel")
    cc = wait_state(sid, "cancelled", 120)
    sc.expect("a person ended the run and nothing else was changed", bool(cc) and git_counts().get("open_prs") == 0, "Cancel runs the compensations for what the ledger shows was done, which here is nothing.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S06  the sandbox is lost, and waiting costs nothing
# --------------------------------------------------------------------------------------------------------------
def s06_sandbox_and_waiting():
    sc = Scenario("S06", "A sandbox is lost, and waiting costs nothing", "Working state; Waiting for a person",
                  "Delete a run's sandbox while its tests are running, and see the files come back from the last snapshot. Then leave a run waiting for a reviewer while every worker is removed, approve it with nothing running, and see it carry on when workers return.")
    cli("git", "reset")
    session = start({**TASK, "test_seconds": 45})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 45}'", f"session {session}")
    wait_for(lambda: status(session).get("step") == 3 and pending_worker(session)[1] and sandbox_pods(), 150, 2)
    before = sandbox_pods()
    snaps = sql(f"SELECT step, snapshot_id, files FROM snapshots WHERE session='{session}' ORDER BY step")
    k(sc, "-n", "sandboxes", "get", "pods", display="kubectl -n sandboxes get pods   # the run's sandbox, while its tests run")
    show(sc, "SELECT step, snapshot_id, files FROM snapshots   # what is already safe in object storage", "\n".join(f"step {r['step']}  {r['snapshot_id']}  {r['files']} files" for r in snaps))
    sc.expect("the sandbox exists and two snapshots (after the two file writes) are already stored", len(before) == 1 and len(snaps) == 2, "A snapshot is taken after every step that changes files.")
    k(sc, "-n", "sandboxes", "delete", "pod", before[0], "--grace-period=0", "--force", display=f"kubectl -n sandboxes delete pod {before[0]} --grace-period=0 --force   # the sandbox is lost, with its disk")
    st = wait_state(session, "waiting", 300)
    after_pods = sandbox_pods()
    rows = sql(f"SELECT step, role, content FROM context_log WHERE session='{session}' AND step=3 AND role='tool'")
    res = json.loads(rows[0]["content"])["result"] if rows else {}
    show(sc, "SELECT content FROM context_log WHERE step = 3 AND role = 'tool'   # the result the agent finally saw for its test run", json.dumps(res, indent=1))
    agent(sc, "status", session, fmt=fmt_status, display=f"agentctl status {session}")
    tl = timeline(session)
    show(sc, f"agentctl timeline {session}", fmt_timeline(tl, only={"run_tool", "snapshot_workspace"}))
    sc.expect("the run carried on to the review step", bool(st) and st.get("step") == 6, "The tool step failed when its sandbox vanished and was retried, in a new sandbox.")
    sc.expect("the retried test run passed, so the files had been put back", res.get("passed") is True and "Ran 2 tests" in res.get("output", "") and "OK" in res.get("output", ""),
              "A new sandbox starts empty. The manager restored the files from the manifest of the last snapshot before the tests ran. Had it not, the test discovery would have found nothing and failed.")
    sc.expect("the test step took more than one attempt in the history", any(e.get("activity") == "run_tool" and e.get("attempts", 1) >= 2 for e in tl.get("events", [])), "The engine, not the platform code, recorded the retry.")
    cs = [c for c in calls(session) if c["kind"] == "act"]
    sc.expect("the model was not called again for any step", sorted(c["step"] for c in cs) == list(range(7)), "A sandbox loss is a tool failure, not a reason to ask the model again.")
    sc.expect("the finished run's sandbox was released when it began waiting", not sandbox_pods(), "The waiting run holds no pod.")
    # waiting costs nothing
    k(sc, "-n", "sandboxes", "get", "pods", display="kubectl -n sandboxes get pods   # the run is waiting for a reviewer: nothing is held")
    kq("-n", NS, "scale", "deploy/worker-v1", "--replicas=0")
    show(sc, "kubectl -n agent-platform scale deploy/worker-v1 --replicas=0", "workers scaled to zero")
    wait_for(lambda: not worker_pods(), 120, 3)
    k(sc, "-n", NS, "get", "pods", "-l", "app=worker-v1", display="kubectl -n agent-platform get pods -l app=worker-v1")
    cli("signal", session, "approve", "--json", json.dumps({"comment": "Please mention how repeated dashes are handled."}))
    show(sc, f"agentctl signal {session} approve   # the reviewer approves while no worker exists", "signal accepted by the engine")
    time.sleep(20)
    mid = status(session)
    show(sc, f"agentctl status {session}   # 20 seconds later, still with no worker", fmt_status(mid))
    sc.expect("with no worker at all, the run and the approval are safely held by the engine", mid.get("workflow_status") == "RUNNING" and mid.get("state") == "no-worker",
              "The approval is a row in the run's history. Nothing needs to be running to receive it.")
    scale_workers(2)
    done = wait_state(session, "completed", 240)
    agent(sc, "status", session, fmt=fmt_status, display=f"agentctl status {session}   # after two new workers started")
    sc.expect("when workers returned, the run noticed the approval and finished", bool(done) and done.get("state") == "completed", "A new worker replayed the history, found the approval in it, and continued.")
    sc.expect("exactly one pull request exists", git_counts().get("open_prs") == 1, "No step was repeated by the long wait.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S07  context state: rebuilt, never remembered
# --------------------------------------------------------------------------------------------------------------
def s07_context():
    sc = Scenario("S07", "Context is rebuilt from the log, never remembered", "Context state",
                  "Kill a worker in the middle of a slow model call and see that the retry sends the same prompt. Then run the same task once without and once with compaction, and rebuild every prompt of both runs from the database to show that each one can be reproduced exactly.")
    cli("git", "reset")
    agent(sc, "model-fault", "--latency", "30", display="agentctl model-fault --latency 30   # every model call now takes 30 seconds")
    s1 = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'", f"session {s1}")
    wait_for(lambda: pending_worker(s1)[1] and pending_worker(s1)[1]["activity"] == "call_model", 120, 2)
    pod, act = pending_worker(s1)
    show(sc, f"agentctl status {s1}   # step 0's model call is in flight", f"call_model on worker {pod}")
    k(sc, "-n", NS, "delete", "pod", pod or "none", "--grace-period=0", "--force", display=f"kubectl -n agent-platform delete pod {pod} --grace-period=0 --force   # the worker dies while waiting for the model")
    agent(sc, "model-fault", "--latency", "0", display="agentctl model-fault --latency 0   # the provider answers quickly again")
    st = wait_state(s1, "waiting", 300)
    wait_for(lambda: len([c for c in calls(s1) if c["step"] == 0 and c["kind"] == "act" and c["status"] == 200]) >= 2, 90, 3)
    agent(sc, "calls", s1, fmt=fmt_calls, display=f"agentctl calls {s1}   # step 0 appears twice, with the same prompt hash")
    s0 = [c for c in calls(s1) if c["step"] == 0 and c["kind"] == "act"]
    sc.expect("the run carried on after the worker's death", bool(st) and st.get("step") == 6, "Another worker took over the model-call activity.")
    sc.expect("the interrupted model call was made a second time, with an identical prompt", len(s0) == 2 and len({c["prompt_sha"] for c in s0}) == 1,
              "The prompt is rebuilt from the session database, so the second worker, which remembered nothing, built the same one. This call is the one cost of a worker dying mid-call: it is paid for twice.")
    rows = sql(f"SELECT step, COUNT(*) AS n FROM context_log WHERE session='{s1}' AND role='assistant' GROUP BY step ORDER BY step")
    sc.expect("the context log holds one answer for step 0, not two", all(r["n"] == 1 for r in rows) and rows[0]["step"] == 0, "Writing an entry is idempotent: the second attempt's write changed nothing.")
    approve(s1)
    wait_state(s1, "completed", 240)
    cli("git", "reset")
    # compaction
    ctrl = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'   # without compaction", f"session {ctrl}")
    wait_state(ctrl, "waiting", 240)
    approve(ctrl)
    d1 = wait_state(ctrl, "completed", 240)
    cli("git", "reset")
    comp = start({**TASK, "test_seconds": 2, "context_budget": 300})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2, \"context_budget\": 300}'   # compact when the context exceeds 300 tokens", f"session {comp}")
    wait_state(comp, "waiting", 240)
    approve(comp)
    d2 = wait_state(comp, "completed", 240)
    summ = sql(f"SELECT upto_step, tokens FROM summaries WHERE session='{comp}' ORDER BY upto_step")
    show(sc, "SELECT upto_step, tokens FROM summaries   # each compaction is a stored row", "\n".join(f"summary of steps up to {r['upto_step']}: {r['tokens']} tokens" for r in summ) or "(none)")
    ca = sql(f"SELECT kind, COUNT(*) AS n FROM model_calls WHERE session='{comp}' AND status=200 GROUP BY kind ORDER BY kind")
    show(sc, "SELECT kind, COUNT(*) FROM model_calls   # compaction is a model call like any other, recorded", "\n".join(f"{r['kind']:10s} {r['n']}" for r in ca))
    last = lambda s: sql(f"SELECT step, prompt_tokens FROM model_calls WHERE session='{s}' AND kind='act' AND status=200 ORDER BY step DESC, id DESC LIMIT 1")[0]
    l1, l2 = last(ctrl), last(comp)
    show(sc, "prompt size sent for the final decision", f"without compaction: {l1['prompt_tokens']} tokens\nwith compaction:    {l2['prompt_tokens']} tokens")
    sc.expect("compaction happened, and the compacted run finished the same job", len(summ) >= 1 and bool(d1) and bool(d2) and d1.get("result", {}).get("summary") == d2.get("result", {}).get("summary"),
              "The scripted model reads its position from the summary exactly as it would from the full log, so the outcome is identical.")
    n_sum = sum(r["n"] for r in ca if r["kind"] == "summarize")
    sc.expect("the compacted prompt was smaller, and each summary cost exactly one model call", l2["prompt_tokens"] < l1["prompt_tokens"] and n_sum == len(summ),
              "A compaction is an activity: it is stored once and recorded in the history, so a replay reuses it and does not ask the model for another.")
    # rebuild
    for label, s in (("without compaction", ctrl), ("with compaction", comp)):
        r = agent(sc, "rebuild", s, fmt=lambda o: "step recorded         rebuilt          same\n" + "\n".join(f"{x['step']:>4} {x['recorded'][:16]}  {x['rebuilt'][:16]}  {x['same']}" for x in o.get("steps", [])) + f"\nall prompts reproduced: {o.get('all_same')}",
                  display=f"agentctl rebuild {s}   # {label}: rebuild every prompt from the database and compare its hash with the one the gateway recorded")
        sc.expect(f"{label}: every prompt rebuilds to exactly the hash recorded when it was sent", bool(r.get("all_same")) and len(r.get("steps", [])) >= 10,
                  "The prompt is a pure function of the stored log and summaries, which are append-only.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S08  the life of a run: pause, budgets, loops, cancel, rewind, fork
# --------------------------------------------------------------------------------------------------------------
def s08_lifecycle():
    sc = Scenario("S08", "Pause, budgets, loops, cancel, rewind and fork", "Run lifecycle",
                  "Do to a run what an operator or a reviewer would: pause it and resume it, hit its budgets, let it loop, cancel it and have its outside effects undone, and start new runs from the middle of an old one.")
    cli("git", "reset")
    # a) pause / resume
    s1 = start({**TASK, "test_seconds": 30})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 30}'", f"session {s1}")
    wait_for(lambda: status(s1).get("step") == 3, 120, 2)
    agent(sc, "signal", s1, "pause", display=f"agentctl signal {s1} pause   # while the tests are running")
    wait_state(s1, "paused", 120)
    p1 = status(s1)
    n1 = len(calls(s1))
    agent(sc, "status", s1, fmt=fmt_status, display=f"agentctl status {s1}   # it finished its step, then stopped")
    time.sleep(20)
    p2 = status(s1)
    n2 = len(calls(s1))
    show(sc, "20 seconds later: step, model calls", f"step {p1.get('step')} -> {p2.get('step')}    model calls {n1} -> {n2}")
    sc.expect("a paused run stops between steps and does nothing while paused", p1.get("state") == "paused" and p1.get("step") == 4 and p2.get("step") == 4 and n1 == n2, "The pause is a signal, honoured at the start of the next step, so no tool is cut off in the middle.")
    agent(sc, "signal", s1, "resume", display=f"agentctl signal {s1} resume")
    r = wait_state(s1, "waiting", 240)
    sc.expect("resuming continued from the step it had stopped at", bool(r) and r.get("step") == 6 and len([c for c in calls(s1) if c["kind"] == "act"]) == 7, "It made one model call for each of steps 0 to 6.")
    cli("signal", s1, "cancel")
    wait_state(s1, "cancelled", 120)
    cli("git", "reset")
    # b) budgets
    s2 = start({**TASK, "test_seconds": 2, "max_steps": 3})
    show(sc, "agentctl start --task '{... \"max_steps\": 3}'", f"session {s2}")
    st = wait_state(s2, "needs_attention", 180)
    agent(sc, "status", s2, fmt=fmt_status, display=f"agentctl status {s2}")
    sc.expect("a run that reaches its step limit stops and asks, it does not carry on", bool(st) and st.get("step") == 3 and "budget" in (st.get("attention") or ""), "Limits are checked before each model call.")
    agent(sc, "signal", s2, "resume", "--json", json.dumps({"extend_steps": 50}), display=f"agentctl signal {s2} resume --json '{{\"extend_steps\": 50}}'   # a person extends the budget")
    r = wait_state(s2, "waiting", 240)
    sc.expect("after a person extended the limit, the run went on", bool(r) and r.get("step") == 6, "Extending is part of the resume signal, so it is in the history.")
    cli("signal", s2, "cancel")
    wait_state(s2, "cancelled", 120)
    cli("git", "reset")
    s3 = start({**TASK, "test_seconds": 2, "token_budget": 700})
    show(sc, "agentctl start --task '{... \"token_budget\": 700}'", f"session {s3}")
    st = wait_state(s3, "needs_attention", 180)
    agent(sc, "status", s3, fmt=fmt_status, display=f"agentctl status {s3}")
    agent(sc, "calls", s3, fmt=fmt_calls, display=f"agentctl calls {s3}   # the gateway refused the call that would have crossed the budget (status 429)")
    sc.expect("a token budget stops the run before the call that would exceed it", bool(st) and "token budget" in (st.get("attention") or "") and any(c["status"] == 429 for c in calls(s3)), "The model gateway adds up what the run has used and refuses the next call.")
    cli("signal", s3, "cancel")
    wait_state(s3, "cancelled", 120)
    cli("git", "reset")
    # c) loop
    s4 = start({**TASK, "test_seconds": 2, "behavior": "loop"})
    show(sc, "agentctl start --task '{... \"behavior\": \"loop\"}'   # a model that repeats the same call", f"session {s4}")
    st = wait_state(s4, "needs_attention", 180)
    agent(sc, "status", s4, fmt=fmt_status, display=f"agentctl status {s4}")
    agent(sc, "timeline", s4, fmt=lambda o: fmt_timeline(o, only={"run_tool"}), display=f"agentctl timeline {s4}")
    sc.expect("the same call three times in a row stops the run", bool(st) and "loop detected" in (st.get("attention") or ""), "The workflow remembers the last three calls and compares them. The repeat was not executed a third time.")
    cli("signal", s4, "cancel")
    wait_state(s4, "cancelled", 120)
    cli("git", "reset")
    # d) cancel with compensation
    s5 = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'", f"session {s5}")
    wait_state(s5, "waiting", 240)
    before = git_counts()
    show(sc, "agentctl git counts   # the run has a branch and an open pull request", json.dumps(before))
    agent(sc, "signal", s5, "cancel", display=f"agentctl signal {s5} cancel")
    c5 = wait_state(s5, "cancelled", 180)
    agent(sc, "status", s5, display=f"agentctl status {s5}")
    after = git_counts()
    led = agent(sc, "ledger", s5, fmt=fmt_ledger, display=f"agentctl ledger {s5}")
    show(sc, "agentctl git counts", json.dumps(after))
    sc.expect("cancelling closed the pull request and deleted the branch the run had made", bool(c5) and after.get("open_prs") == 0 and after.get("branches") == 0 and before.get("open_prs") == 1,
              "The ledger shows what the run changed; cancel undoes it newest first.")
    sc.expect("the ledger says so", {r["tool"]: r["status"] for r in led.get("ledger", [])} == {"push_branch": "compensated", "open_pr": "compensated"}, "The record and the world agree.")
    cli("git", "reset")
    # e) fork and rewind
    a = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'   # the run to branch from", f"session {a}")
    wait_state(a, "waiting", 240)
    fk = agent(sc, "fork", a, "--step", "3", "--hint", "Keep the function under ten lines.", display=f"agentctl fork {a} --step 3 --hint 'Keep the function under ten lines.'   # a NEW run from the recorded state after step 3")
    b = fk.get("session")
    sc.expect("the fork started from step 3 with the files the original had then", bool(b) and fk.get("from_step") == 3 and bool(fk.get("snapshot")), "It copies the context log up to step 3 and starts from that step's workspace snapshot.")
    wait_state(b, "waiting", 240)
    approve(b)
    wait_state(b, "completed", 240)
    cx = sql(f"SELECT content FROM context_log WHERE session='{b}' AND step=7 AND role='assistant'")
    show(sc, f"SELECT content FROM context_log WHERE session = {b} AND step = 7   # what the fork wrote after the review", (cx[0]["content"][:400] if cx else "(none)"))
    sc.expect("the fork used the hint it was given", bool(cx) and "Keep the function under ten lines." in cx[0]["content"], "The hint was added to the forked run's context, so the next decision read it.")
    cnt = git_counts()
    sc.expect("original and fork each have their own pull request", cnt.get("open_prs") == 2, "A fork is a separate run with a separate ledger and its own idempotency keys.")
    rw = agent(sc, "fork", a, "--step", "3", "--replace", "--hint", "Use a regular expression.", display=f"agentctl fork {a} --step 3 --replace --hint 'Use a regular expression.'   # rewind: replace the original")
    c = rw.get("session")
    wait_state(a, "cancelled", 180)
    wait_state(c, "waiting", 240)
    prs = cli("git", "prs")[1].get("pulls", [])
    show(sc, "agentctl git prs   # the Git host's pull requests", "\n".join(f"#{p['number']}  {p['state']:6s} {p['head']}" for p in prs))
    orig = [p for p in prs if p["head"] == f"agent/{a}-4"]
    sc.expect("rewinding cancelled the original and undid its pull request", bool(orig) and all(p["state"] == "closed" for p in orig), "The original's cancellation ran its compensations.")
    sc.expect("the replacement run is at the review step with its own, new pull request", len([p for p in prs if p["state"] == "open"]) == 2, "Two open pull requests remain: the fork's and the replacement's.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S09  deploying new agent code while runs are in flight
# --------------------------------------------------------------------------------------------------------------
def s09_deploy():
    sc = Scenario("S09", "Deploying new code while runs are in flight", "Deploying new code; Versioning",
                  "A change to the workflow code can make every run that is waiting unable to continue. Replay recorded histories through three versions of the code to see which is safe, then deploy the safe one the way the design says: new runs on new workers, old runs finish on old workers. Finally, stop a worker politely and see that its step is not repeated.")
    cli("git", "reset")
    x = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'   # a run in flight on version 1", f"session {x}")
    wait_state(x, "waiting", 240)
    # a) replay
    res = {}
    for variant, label in (("v1", "the code the run was recorded with"), ("v2-unpatched", "version 2, a lint step added after every file change, with no protection"),
                           ("v2-patched", "version 2, the same change behind a patch marker")):
        r = cli("replay", "--sessions", x, env={"WORKFLOW_VARIANT": variant})[1]
        res[variant] = r.get("results", {}).get(x, str(r))
        show(sc, f"WORKFLOW_VARIANT={variant} agentctl replay --sessions {x}   # {label}", f"{variant}: {res[variant]}")
    sc.expect("the original code replays the recorded history cleanly", res["v1"] == "replays cleanly", "Replay runs the workflow code against the history with no worker and no side effect.")
    sc.expect("the unprotected change cannot replay the run that is waiting", "Nondeterminism" in res["v2-unpatched"] or "nondeterminism" in res["v2-unpatched"].lower(),
              "The history says 'snapshot, then run the next tool'. The new code says 'snapshot, then lint'. Deploying it would have stranded every waiting run.")
    sc.expect("the same change behind a patch marker replays cleanly", res["v2-patched"] == "replays cleanly", "The marker makes old histories take the old path and new runs take the new one.")
    # b) deploy v2 beside v1
    kq("-n", NS, "scale", "deploy/worker-v2", "--replicas=2")
    wait_for(lambda: len([p for p in worker_pods("worker-v2") if any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))]) == 2, 240, 3)
    k(sc, "-n", NS, "get", "pods", "-l", "role=worker", "-L", "app", display="kubectl -n agent-platform get pods -l role=worker   # version 1 and version 2 workers side by side")
    agent(sc, "ops", "queue", "--queue", "agent-v2", display="agentctl ops queue --queue agent-v2   # new runs now start on the version 2 queue")
    y = start({**TASK, "test_seconds": 2})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 2}'   # a new run", f"session {y}")
    wait_state(y, "waiting", 240)
    sx, sy = status(x), status(y)
    show(sc, "which queue each run lives on", f"{x}: {sx.get('queue')}\n{y}: {sy.get('queue')}")
    approve(x)
    approve(y)
    dx = wait_state(x, "completed", 240)
    dy = wait_state(y, "completed", 240)
    tx, ty = timeline(x), timeline(y)
    lx = len([e for e in tx.get("events", []) if e.get("activity") == "lint"])
    ly = len([e for e in ty.get("events", []) if e.get("activity") == "lint"])
    show(sc, "lint steps in each run's history", f"{x} (started before the deploy): {lx}\n{y} (started after):               {ly}")
    sc.expect("the run in flight kept its queue and finished on version 1; the new run used version 2", sx.get("queue") == "agent-v1" and sy.get("queue") == "agent-v2" and bool(dx) and bool(dy) and lx == 0 and ly == 3,
              "A run is pinned to the queue it started on, so the old code only ever sees old histories and the new code only new ones.")
    running_v1 = [r for r in cli("ops", "list")[1].get("runs", []) if r.get("status") == "RUNNING" and r.get("queue") == "agent-v1"]
    show(sc, "agentctl ops list   # runs still running on the version 1 queue", f"{len(running_v1)}")
    sc.expect("when the last old run finished, nothing was left on version 1, so its workers can be removed", len(running_v1) == 0, "This is how an old version is retired: wait until its queue is empty.")
    agent(sc, "ops", "queue", "--queue", "agent-v1", display="agentctl ops queue --queue agent-v1   # (back to the original code for the rest of the tests)")
    kq("-n", NS, "scale", "deploy/worker-v2", "--replicas=0")
    cli("git", "reset")
    # c) graceful stop
    z = start({**TASK, "test_seconds": 25})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 25}'", f"session {z}")
    wait_for(lambda: pending_worker(z)[1] and pending_worker(z)[1]["activity"] == "run_tool" and status(z).get("step") == 3, 120, 2)
    pod, act = pending_worker(z)
    show(sc, f"agentctl status {z}   # the test step is running on one worker", f"run_tool on {pod}")
    k(sc, "-n", NS, "delete", "pod", pod or "none", "--wait=false", display=f"kubectl -n agent-platform delete pod {pod}   # a rolling update would do this: SIGTERM, not SIGKILL")
    time.sleep(3)
    k(sc, "-n", NS, "get", "pods", "-l", "app=worker-v1", display="kubectl -n agent-platform get pods -l app=worker-v1   # the old pod is Terminating, finishing its step")
    stz = wait_state(z, "waiting", 240)
    tz = timeline(z)
    agent(sc, "timeline", z, fmt=lambda o: fmt_timeline(o, only={"run_tool"}), display=f"agentctl timeline {z}")
    rt = [e for e in tz.get("events", []) if e.get("activity") == "run_tool"]
    sc.expect("the step that was running finished on its own worker: it has one attempt", bool(stz) and all(e["attempts"] == 1 for e in rt), "On SIGTERM the worker stops taking tasks but lets activities in flight finish. Compare S03, where a kill gave two attempts.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S10  the engine and its database fail
# --------------------------------------------------------------------------------------------------------------
def rds_state():
    p = subprocess.run(["aws", "rds", "describe-db-instances", "--db-instance-identifier", f"{PROJECT}-db", "--query",
                        "DBInstances[0].[DBInstanceStatus,AvailabilityZone,SecondaryAvailabilityZone]", "--output", "text"], capture_output=True, text=True, env=ENV)
    return p.stdout.split()


def s10_engine_failover():
    sc = Scenario("S10", "The engine's database fails over, and the engine restarts", "The durable execution engine; Failure modes",
                  "Force the Multi-AZ database to fail over to its standby while three runs are in flight, then delete every engine server pod while a run is in flight. Nobody touches the runs; they must finish, once.")
    cli("git", "reset")
    sessions = [start({**TASK, "test_seconds": 60}) for _ in range(3)]
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 60}'   # three runs", "\n".join(sessions))
    wait_for(lambda: all((status(s).get("step") or 0) >= 3 for s in sessions), 120, 3)
    before = rds_state()
    sc.aws("rds", "describe-db-instances", "--db-instance-identifier", f"{PROJECT}-db", "--query", "DBInstances[0].[DBInstanceStatus,AvailabilityZone,SecondaryAvailabilityZone]", "--output", "text",
           display=f"aws rds describe-db-instances --db-instance-identifier {PROJECT}-db --query 'DBInstances[0].[Status,PrimaryAZ,StandbyAZ]'   # before")
    sc.aws("rds", "reboot-db-instance", "--db-instance-identifier", f"{PROJECT}-db", "--force-failover", "--query", "DBInstance.DBInstanceStatus", "--output", "text",
           display=f"aws rds reboot-db-instance --db-instance-identifier {PROJECT}-db --force-failover   # the primary is taken away")
    t0 = time.time()
    time.sleep(20)

    def failed_over():
        s = rds_state()
        return s if s and s[0] == "available" and s[1] != before[1] else None
    after = wait_for(failed_over, 900, 10) or rds_state()
    took = time.time() - t0
    show(sc, "aws rds describe-db-instances ...   # after", f"status={after[0]}  primary={after[1]}  standby={after[2] if len(after) > 2 else '?'}\nfailover took about {took:.0f} seconds")
    sc.expect("the standby in the other zone became the primary", after[0] == "available" and after[1] != before[1] and after[1] == before[2], "RDS Multi-AZ keeps a synchronous standby and moves the database's DNS name to it.")
    waited = [wait_state(s, "waiting", 1200) for s in sessions]
    rows = [status(s) for s in sessions]
    show(sc, "agentctl status <each run>", "\n".join(f"{s}  {fmt_status(r)}" for s, r in zip(sessions, rows)))
    sc.expect("all three runs reached the review step on their own", all(waited) and all(r.get("step") == 6 for r in rows), "The engine, the workers and the gateways retried their database connections until the new primary answered.")
    for s in sessions:
        approve(s)
    done = [wait_state(s, "completed", 600) for s in sessions]
    counts = git_counts()
    show(sc, "agentctl git counts", json.dumps(counts))
    sc.expect("each run finished, and each made exactly one branch and one pull request", all(done) and counts.get("all_prs") == 3 and counts.get("branches") == 3, "The ledger is in the same database: a step that was in flight when it failed over was retried and deduplicated.")
    per = sql("SELECT session, step, COUNT(*) AS n FROM model_calls WHERE kind='act' AND status=200 AND session IN (" + ",".join(f"'{s}'" for s in sessions) + ") GROUP BY session, step HAVING COUNT(*) > 1")
    show(sc, "model calls repeated because of the failover (steps called more than once)", f"{len(per)} of 33 steps" + ("\n" + "\n".join(f"{r['session']} step {r['step']}: {r['n']} calls" for r in per) if per else ""))
    sc.expect("the model was called for every step of every run", len(sql("SELECT DISTINCT session, step FROM model_calls WHERE kind='act' AND status=200 AND session IN (" + ",".join(f"'{s}'" for s in sessions) + ")")) == 33, "11 decisions for each of 3 runs. Any step the failover interrupted mid-call was asked again, and shows above.")
    # engine pods
    cli("git", "reset")
    s2 = start({**TASK, "test_seconds": 40})
    show(sc, "agentctl start --task '{\"ticket\": \"T-1\", \"test_seconds\": 40}'", f"session {s2}")
    wait_for(lambda: status(s2).get("step") == 3, 120, 2)
    k(sc, "-n", "temporal", "get", "pods", "-l", "app.kubernetes.io/component in (frontend,history,matching)", display="kubectl -n temporal get pods -l 'component in (frontend,history,matching)'   # the engine's servers, before")
    old = {p["metadata"]["name"] for p in kj("-n", "temporal", "get", "pods", "-l", "app.kubernetes.io/component in (frontend,history,matching)").get("items", [])}
    k(sc, "-n", "temporal", "delete", "pods", "-l", "app.kubernetes.io/component in (frontend,history,matching)", "--grace-period=0", "--force", "--wait=false",
      display="kubectl -n temporal delete pods -l 'component in (frontend,history,matching)' --force   # every engine server at once")
    time.sleep(10)
    d = status(s2)
    show(sc, f"agentctl status {s2}   # ten seconds after", f"{d.get('workflow_status') or d.get('raw') or d}")
    st = wait_state(s2, "waiting", 900)
    new = {p["metadata"]["name"] for p in kj("-n", "temporal", "get", "pods", "-l", "app.kubernetes.io/component in (frontend,history,matching)").get("items", [])}
    k(sc, "-n", "temporal", "get", "pods", "-l", "app.kubernetes.io/component in (frontend,history,matching)", display="kubectl -n temporal get pods -l 'component in (frontend,history,matching)'   # after")
    sc.expect("the engine's servers were all replaced", bool(new) and not (new & old), "Kubernetes started new ones, which read everything from the database.")
    sc.expect("the run in flight reached the review step without anyone's help", bool(st) and st.get("step") == 6, "The engine keeps no state of its own: a run is rows in PostgreSQL.")
    approve(s2)
    d2 = wait_state(s2, "completed", 300)
    sc.expect("and finished once", bool(d2) and git_counts().get("all_prs") == 1, "One pull request.")
    cleanup_all()
    return sc


# --------------------------------------------------------------------------------------------------------------
# S11  many runs at once, and what can be seen
# --------------------------------------------------------------------------------------------------------------
def s11_scale():
    N = 24
    sc = Scenario("S11", "Many runs at once, scaled by the queue, and what an operator can see", "Scale; Observability",
                  f"Start {N} runs together on workers that can each do six things at once, so the work is more than two workers can hold. Let the autoscaler add workers because the engine's task queue has a backlog, and read the platform's own metrics, ledger and costs while it happens.")
    cli("git", "reset")
    kq("-n", NS, "annotate", "scaledobject/worker-v1", "autoscaling.keda.sh/paused=false", "--overwrite")
    show(sc, "kubectl -n agent-platform annotate scaledobject/worker-v1 autoscaling.keda.sh/paused=false   # switch the autoscaler on", "scaledobject/worker-v1 annotated")
    k(sc, "-n", NS, "get", "scaledobject", "worker-v1", display="kubectl -n agent-platform get scaledobject worker-v1   # scale on the engine's task queue backlog")
    sc.expect("each worker has six activity slots, so two workers hold twelve steps at once", all(any(e.get("name") == "ACTIVITY_SLOTS" and e.get("value") == "6" for e in p["spec"]["containers"][0]["env"]) for p in worker_pods()),
              "Slots are set small so that a modest number of runs exceeds them. What matters is the ratio of runs to slots, not the absolute numbers.")
    t0 = time.time()
    sessions = bulk_start({**TASK, "test_seconds": 100}, N)
    show(sc, f"start {N} runs at once, each with a 100-second test step", f"{len(sessions)} runs started in {time.time() - t0:.0f} seconds")
    sc.expect(f"all {N} runs were accepted", len(sessions) == N and len(set(sessions)) == N, "The session API only writes a row and starts a workflow, so starting many at once is cheap.")
    samples, peak_slots, peak_replicas = [], 0, 2
    end = time.time() + 600
    while time.time() < end:
        replicas = len(worker_pods())
        slots = prom('sum(temporal_worker_task_slots_used{worker_type="ActivityWorker"})') or 0
        runs = [r for r in cli("ops", "list", timeout=300)[1].get("runs", []) if r["session"] in sessions]
        waiting = len([r for r in runs if r.get("state") == "waiting"])
        peak_slots, peak_replicas = max(peak_slots, slots), max(peak_replicas, replicas)
        samples.append(f"t={time.time() - t0:>4.0f}s  workers={replicas}  activity slots in use={slots:>4.0f}  runs waiting for review={waiting}/{N}")
        if waiting == N:
            break
        time.sleep(10)
    show(sc, "sample every ~10 seconds: workers, activity slots in use (Prometheus), runs at the review step", "\n".join(samples))
    sc.expect("the workers were saturated: every slot was in use at some point", peak_slots >= 12, f"Peak slots in use: {peak_slots:.0f} of 12 available with two workers.")
    sc.expect("the autoscaler added workers because of the queue's backlog", peak_replicas > 2, f"Peak workers: {peak_replicas}. The trigger is the engine's task queue, not CPU: a worker waiting for a model or a tool uses almost none.")
    sc.expect(f"all {N} runs reached the review step", waiting == N, "Each run is independent: its own history, its own ledger rows, its own sandbox.")
    counts = git_counts()
    show(sc, "agentctl git counts", json.dumps(counts))
    sc.expect(f"{N} branches and {N} pull requests exist, one for each run", counts.get("branches") == N and counts.get("all_prs") == N, "Queueing and scaling did not repeat or lose any step.")
    # observability
    q = sql("SELECT tool, class, COUNT(*) AS n, SUM(attempts) AS attempts, SUM(deduplicated) AS dedup FROM ledger WHERE session IN (" + ",".join(f"'{s}'" for s in sessions) + ") GROUP BY tool, class ORDER BY tool")
    show(sc, "SELECT tool, class, COUNT(*), SUM(attempts), SUM(deduplicated) FROM ledger   # the side-effect ledger across the runs", "\n".join(f"{r['tool']:14s} {r['class']:11s} runs={r['n']:>3}  attempts={r['attempts']:>3}  deduplicated={r['dedup']}" for r in q))
    bulk_approve(sessions)
    done = [wait_state(s, "completed", 300) for s in sessions[:3]]
    for s in sessions:
        wait_state(s, "completed", 300)
    fin = [r for r in cli("ops", "list", timeout=300)[1].get("runs", []) if r["session"] in sessions]
    sc.expect(f"after the reviews, all {N} runs completed", len([r for r in fin if r["status"] == "COMPLETED"]) == N, "Approvals reached the waiting workflows, now on whichever workers were running.")
    tk = sql("SELECT COUNT(*) AS runs, MIN(t) AS lo, ROUND(AVG(t)) AS avg, MAX(t) AS hi, SUM(t) AS total FROM (SELECT session, SUM(prompt_tokens + completion_tokens) AS t FROM model_calls WHERE status=200 AND session IN ("
             + ",".join(f"'{s}'" for s in sessions) + ") GROUP BY session) x")[0]
    mc = sql("SELECT backend, COUNT(*) AS n FROM model_calls WHERE status=200 AND session IN (" + ",".join(f"'{s}'" for s in sessions) + ") GROUP BY backend")
    show(sc, "tokens per run, from the model gateway's record", f"runs={tk['runs']}  min={tk['lo']}  average={tk['avg']}  max={tk['hi']}  total={tk['total']}\nmodel calls by backend: " + ", ".join(f"{r['backend']}={r['n']}" for r in mc))
    sc.expect("the gateway's record accounts for every run's tokens, and the model was called 11 times per run", tk["runs"] == N and sum(r["n"] for r in mc) == 11 * N, "Cost per run is a query over one table, because every call goes through the gateway.")
    tl = timeline(sessions[0])
    show(sc, f"agentctl timeline {sessions[0]}   # one run's life, from its own history", fmt_timeline(tl, only={"call_model", "run_tool", "snapshot_workspace"}))
    waits = bulk_queue_waits(sessions)
    qs = sorted(w for _, w in waits)
    med, top = (qs[len(qs) // 2], qs[-1]) if qs else (None, None)
    slow = [w for w in waits if w[1] >= 5]
    show(sc, "from each run's own history: how long every activity waited for a free worker slot (scheduled to started)",
         f"{len(qs)} activities in {N} runs\nmedian wait: {med} seconds\nlongest wait: {top} seconds\nactivities that waited 5 seconds or more: {len(slow)}" + (" (" + ", ".join(sorted({a for a, _ in slow})) + ")" if slow else ""))
    sc.expect("the backlog is visible in the engine's own records: a few steps waited for a slot, most did not", len(qs) >= 11 * N and top is not None and top >= 5 and med < 1,
              "Steps that queued behind a full set of workers waited until a slot opened or the autoscaler's new workers joined. The median step started at once.")
    fail = prom("sum(temporal_activity_execution_failed) or vector(0)")
    started = prom("sum(temporal_activity_task_received)")
    show(sc, "Prometheus: temporal_activity_task_received and temporal_activity_execution_failed, the workers running now", f"activities received: {started:.0f}\nactivities failed (and retried): {fail:.0f}" if started is not None and fail is not None else "(not available)")
    sc.expect("the workers' own metrics are being collected", started is not None and started > 0, "Each worker serves Prometheus metrics, and Prometheus finds the pods by their annotations.")
    cleanup_all()
    return sc


SCENARIOS = {"S01": s01_platform, "S02": s02_five_states, "S03": s03_worker_loss, "S04": s04_provider_errors, "S05": s05_side_effects, "S06": s06_sandbox_and_waiting, "S07": s07_context, "S08": s08_lifecycle, "S09": s09_deploy, "S10": s10_engine_failover, "S11": s11_scale}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_dir = ROOT / "evidence" / "final"
    if "--out" in sys.argv:
        out_dir = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
        args = [a for a in args if a != str(out_dir)]
    wanted = list(SCENARIOS) if not args or args == ["all"] else args
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for key in wanted:
        print(f"== {key} ...", flush=True)
        try:
            sc = SCENARIOS[key]()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            sc = Scenario.last if Scenario.last is not None and Scenario.last.sid == key else Scenario(key, "crashed", "", str(exc))
            sc.expect("scenario ran to completion", False, f"{type(exc).__name__}: {exc}")
        (out_dir / f"{key}.txt").write_text(sc.transcript() + "\n")
        (out_dir / f"{key}.json").write_text(json.dumps(sc.to_json(), indent=1))
        blocks = []
        for s in sc.steps:
            blocks += [("cmd", s.display), ("out", s.output)]
        blocks += [("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}") for c in sc.checks]
        render_png(f"{sc.sid}  {sc.title}", blocks, out_dir / f"{key}.png", max_lines=80)
        summary.append({"id": key, "title": sc.title, "passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed]})
        print(f"   {'PASS' if sc.ok else 'FAIL'}  ({sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks)", flush=True)
        for c in sc.checks:
            if not c.passed:
                print("   FAILED:", c.description)
        try:
            cleanup_all()
        except Exception:
            pass
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + "\n".join(f"{s['id']}  {'PASS' if s['passed'] else 'FAIL'}  {s['title']}" for s in summary))
    sys.exit(0 if all(s["passed"] for s in summary) else 1)


if __name__ == "__main__":
    main()

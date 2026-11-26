"""Scenario S11: the review queue goes to people (ASSUMED inputs), and their labels drive agreement, gold labels,
regression cases, a contract change, the gate and re-scoring."""
import json

from assumed_reviewers import DECISIONS, labels_for
from common_scen import STATE, job, save_state, show, vq
from harness import ROOT, Scenario
from helpers import sj
from scen_a import QUALITY, qkey, svc  # noqa: F401
from scen_b import CLEAN, RED

NEW = "math-final-answer@2.1.0"
REVIEWERS = ("reviewer-a", "reviewer-b")


def s11():
    sc = Scenario("S11", "People in the loop: two reviewers, agreement, gold labels, a contract change and re-scoring",
                  "Semi-automatic verification: people in the loop; A verifier is a product (change and re-score)",
                  "Send the review queues of both RL runs to two reviewers. The reviewers' labels and the owner's contract decisions are "
                  "ASSUMED inputs, written down as checklists before the run (tests/assumed_reviewers.py), because nobody reviewed anything "
                  "in this build. From those inputs the platform measures inter-reviewer agreement per answer shape, turns agreed and "
                  "settled labels into a human gold set, turns confirmed exploits into regression cases, gates the contract change the "
                  "labels call for (math-final-answer@2.1.0), and re-scores the strict run with it.")
    job(sc, f"review-{CLEAN}", "review", "--run", CLEAN, "--budget", "60", "--out", f"reviews/{CLEAN}.json")
    work = ROOT / ".build" / "review"
    work.mkdir(parents=True, exist_ok=True)
    queues = {RED: sj(f"reviews/{RED}.json") or {}, CLEAN: sj(f"reviews/{CLEAN}.json") or {}}
    for run, q in queues.items():
        for r in REVIEWERS:
            labs = labels_for(q.get("items", []), r)
            f = work / f"{run}-{r}.jsonl"
            f.write_text("".join(json.dumps(x) + "\n" for x in labs))
            vq(sc, "submit", r, "--run", run, "--file", str(f), display=f"vq.py submit {r} --run {run} --file {r}.jsonl   # ASSUMED labels, {len(labs)} items")
        (work / f"{run}-none.json").write_text(json.dumps({"slices": {}}))
        vq(sc, "submit", "decisions", "--run", run, "--file", str(work / f"{run}-none.json"), display=f"vq.py submit decisions --run {run} --file none.json   # no decisions yet")
    first = queues[CLEAN].get("items", [])[:1] + [i for i in queues[CLEAN].get("items", []) if i["answer_shape"] not in ("number", "fraction")][:3]
    la = {x["item"]: x for x in labels_for(first, "reviewer-a")}
    lb = {x["item"]: x for x in labels_for(first, "reviewer-b")}
    show(sc, f"four items of the {CLEAN} queue with both assumed labels",
         "\n".join(f"[{i['answer_shape']}] solution {i['solution']}: ...{i['completion'][-90:]!r}\n   A: {la[i['item']]['label']} ({la[i['item']]['checklist']})   "
                   f"B: {lb[i['item']]['label']} ({lb[i['item']]['checklist']})" for i in first))
    agr = {}
    for run in (RED, CLEAN):
        job(sc, f"labels-{run}", "labels", "--run", run, "--queue", f"reviews/{run}.json")
        agr[run] = sj(f"reviews/{run}/agreement.json") or {}
    show(sc, "inter-reviewer agreement per answer shape (decided by both / agreement / kappa)",
         "\n".join(f"{run:13s} {s:13s} {m['decided_by_both']:3d}  {m['agreement']}  {m['kappa']}" for run in (RED, CLEAN) for s, m in agr[run].get("agreement", {}).items()))
    unclear = sorted(set(agr[RED].get("contract_unclear", [])) | set(agr[CLEAN].get("contract_unclear", [])))
    show(sc, "slices whose contract is unclear (agreement below 0.8)", ", ".join(unclear) or "(none)")
    sc.expect("both reviewers returned a label for every queued item of both runs (assumed inputs, stored with their checklist line)",
              all(agr[r].get("queued") and not any(agr[r].get("missing", {}).values()) for r in (RED, CLEAN)),
              "vq.py submit uploads each reviewer's file; the labels job lists any queued item a reviewer did not label.")
    number = agr[CLEAN].get("agreement", {}).get("number", {})
    sc.expect("the reviewers agree on plain numbers and disagree on at least one other answer shape, which is flagged as an unclear contract",
              (number.get("agreement") or 0) >= 0.95 and bool(unclear),
              "Their checklists read a plain number the same way; they read units, currency, mixed numbers and ratios differently.")
    for run in (RED, CLEAN):
        dec = {s: DECISIONS[s] for s in unclear if s in DECISIONS}
        f = work / f"{run}-decisions.json"
        f.write_text(json.dumps({"slices": dec, "decided_by": "the verifier's owner (assumed)", "assumed": True}, indent=1))
        vq(sc, "submit", "decisions", "--run", run, "--file", str(f), display=f"vq.py submit decisions --run {run} --file decisions.json   # ASSUMED: {', '.join(f'{s} {d.split(':')[0]}' for s, d in dec.items())}")
        job(sc, f"labels2-{run}", "labels", "--run", run, "--queue", f"reviews/{run}.json")
        agr[run] = sj(f"reviews/{run}/agreement.json") or {}
    show(sc, "after the decisions: how each label was settled, and what the people found",
         "\n".join(f"{run:13s} settled {agr[run].get('settled')}  gold {agr[run].get('gold_labels')} ({agr[run].get('gold_correct')} correct)  "
                   f"exploits the training verifier passed {agr[run].get('confirmed_exploits')}  audit false negatives {agr[run].get('audit_false_negatives')}" for run in (RED, CLEAN)))
    settled = sum(agr[r].get("settled", {}).get("settled by contract decision", 0) for r in (RED, CLEAN))
    exploits = sum(agr[r].get("confirmed_exploits", 0) for r in (RED, CLEAN))
    sc.expect("after the owner's decisions, agreed and settled labels form a human gold set, and the exploits people confirmed became regression cases",
              settled > 0 and all(agr[r].get("gold_labels") for r in (RED, CLEAN)) and exploits > 0,
              "A disagreement becomes a label only through a recorded decision for its slice; a training pass that people call wrong becomes a regression case.")
    human = f"gold/human-{RED}.jsonl,gold/human-{CLEAN}.jsonl"
    for vid in ("math-final-answer@1.0.0", "math-final-answer@2.0.0", NEW):
        job(sc, f"hq-{svc(vid)}", "quality", "--verifier", vid, "--gold", human, "--out", f"evals/human/{svc(vid)}.json",
            display=f"vq.py job hq-{svc(vid)} -- quality --verifier {vid} --gold gold/human-<both runs>.jsonl")
    H = {v: (sj(f"evals/human/{svc(v)}.json") or {}).get("quality", {}).get("all", {}) for v in ("math-final-answer@1.0.0", "math-final-answer@2.0.0", NEW)}
    show(sc, "on the reviewers' gold labels", "\n".join(f"{v:26s} accepted wrong {h.get('fp')}  accepted correct {h.get('tp')}  rejected correct {h.get('fn')}  abstained {h.get('abstain')}  "
                                                       f"precision {h.get('precision')} CI {h.get('precision_ci')}" for v, h in H.items()))
    sc.expect("on the human gold set the old verifier accepts answers people call wrong; 2.0.0 and 2.1.0 accept none, and 2.1.0 accepts more correct ones than 2.0.0",
              (H["math-final-answer@1.0.0"].get("fp") or 0) > 0 and H["math-final-answer@2.0.0"].get("fp") == 0 and H[NEW].get("fp") == 0
              and (H[NEW].get("tp") or 0) > (H["math-final-answer@2.0.0"].get("tp") or 0),
              "2.1.0 differs from 2.0.0 only in what the box may hold: a unit, a currency sign or a mixed number, as the decisions said.")
    job(sc, f"q-{svc(NEW)}", "quality", "--verifier", NEW, "--gold", "math_gold.jsonl", "--out", qkey(NEW), display=f"vq.py job q-{svc(NEW)} -- quality --verifier {NEW} --gold math_gold.jsonl")
    job(sc, "fuzz-210", "fuzz", "--verifiers", NEW, "--policy-samples", f"runs/{CLEAN}", "--out", "fuzz/v210")
    cases = f"fuzz/static/regressions.jsonl,reviews/{RED}/regressions.jsonl,reviews/{CLEAN}/regressions.jsonl"
    job(sc, f"rg-{svc(NEW)}", "regress", "--verifier", NEW, "--cases", cases, "--out", f"evals/regress/{svc(NEW)}.json",
        display=f"vq.py job rg-{svc(NEW)} -- regress --verifier {NEW} --cases <fuzz regressions + the reviewers' confirmed exploits>")
    vq(sc, "register", NEW, "--gold", qkey(NEW), "--fuzz", "fuzz/v210/report.json", "--regress", f"evals/regress/{svc(NEW)}.json", "--human", f"evals/human/{svc(NEW)}.json")
    out, code = vq(sc, "rl", "--run", "pin-210", "--verifier", NEW, "--dry-run")
    e = sj("registry/math-final-answer/2.1.0.json") or {}
    sc.expect("the gate approves 2.1.0 with the reviewers' labels as evidence, and a training run can pin it",
              e.get("status") == "approved" and e.get("evidence", {}).get("human") and code == 0 and '"pinned": "ok"' in out,
              "The human check is that 2.1.0 accepts nothing the reviewers labelled wrong; the dry run checks the pins and creates nothing.")
    job(sc, "rescore-clean-210", "rescore", "--run", CLEAN, "--verifier", NEW, "--out", f"rescore/{CLEAN}-210")
    r = sj(f"rescore/{CLEAN}-210/summary.json") or {}
    show(sc, f"aws s3 cp s3://<bucket>/rescore/{CLEAN}-210/summary.json -", json.dumps({k: r.get(k) for k in ("old_verifiers", "rescored_with", "samples", "changed", "flips", "pass_rate_old", "pass_rate_new")}, indent=1))
    fl = r.get("flips", {})
    sc.expect("re-scoring the strict run with 2.1.0 turns abstentions into passes, and no failure into a pass",
              fl.get("abstain->pass", 0) > 0 and fl.get("fail->pass", 0) == 0, "The new version only widens what the box may hold; every other rule is the same.")
    STATE["human"] = {"unclear": unclear, "settled": settled, "exploits": exploits, "rescore_flips": fl, "human_quality": H}
    save_state()
    sc.note(f"Unclear slices: {', '.join(unclear)}. Labels settled by decision: {settled}. Exploits confirmed by the reviewers: {exploits}. Re-score flips: {fl}.")
    return sc

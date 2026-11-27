#!/usr/bin/env python3
"""Builds the appendix page from the hand-written head and tail plus the recorded test evidence.

    build_writeup.py <evidence-dir> <repo-root>

Writes   <repo-root>/content/appendix/a18-design-17-gpu-scheduling.md
Copies   screenshots to <repo-root>/diagrams/appendix/a18-evidence/ and the diagrams to <repo-root>/diagrams/appendix/
Every command, output and check result in section 5 comes from the evidence files.
"""
import json
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scenario_prose import PROSE  # noqa: E402

MAX_LINES, MAX_WIDTH = 16, 200


def clip(text: str) -> str:
    lines = text.splitlines()
    shown = [l if len(l) <= MAX_WIDTH else l[: MAX_WIDTH - 1] + "…" for l in lines[:MAX_LINES]]
    if len(lines) > MAX_LINES:
        shown.append(f"… ({len(lines) - MAX_LINES} more lines in the screenshot and in evidence/{{id}}.txt)")
    return "\n".join(shown)


def esc_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def scenario_section(ev: dict) -> str:
    sid = ev["id"]
    prose = PROSE[sid]
    out = [f"### {sid}: {ev['title']}", ""]
    out.append(f"**Design reference:** {ev['design_ref']}.")
    out.append("")
    out.append(f"**What this tests.** {ev['purpose']}")
    out.append("")
    out.append(prose["intro"])
    out.append("")
    out.append(f"![{sid}: {ev['title']}](diagrams/appendix/a18-evidence/{sid}.png)")
    out.append("")
    out.append("**Commands I ran and what they printed** (shortened; the full text is in the screenshot):")
    out.append("")
    out.append("```text")
    for step in ev["steps"]:
        out.append("$ " + step["display"])
        if step["output"]:
            out.append(clip(step["output"]).replace("{id}", sid))
        out.append("")
    out.append("```")
    out.append("")
    out.append("**What each check proved, and why the output is what it is:**")
    out.append("")
    out.append("| Result | Check | Why this output (what I implemented that causes it) |")
    out.append("|---|---|---|")
    for c in ev["checks"]:
        out.append(f"| {'PASS' if c['passed'] else '**FAIL**'} | {esc_cell(c['description'])} | {esc_cell(c['because'])} |")
    out.append("")
    if ev.get("notes"):
        out.append("**Measured in this run:** " + " ".join(ev["notes"]))
        out.append("")
    out.append("**Why you can trust this result.**")
    out.append("")
    for t in prose["trust"]:
        out.append(f"- {t}")
    out.append("")
    out.append("**Why you get this output.**")
    out.append("")
    for w in prose["why"]:
        out.append(f"- {w}")
    out.append("")
    return "\n".join(out)


def main():
    evidence = pathlib.Path(sys.argv[1])
    repo = pathlib.Path(sys.argv[2])
    ids = sorted(p.stem for p in evidence.glob("S[0-9][0-9].json"))
    evs = [json.loads((evidence / f"{i}.json").read_text()) for i in ids]

    shots = repo / "diagrams" / "appendix" / "a18-evidence"
    shots.mkdir(parents=True, exist_ok=True)
    for i in ids:
        shutil.copy(evidence / f"{i}.png", shots / f"{i}.png")
    subprocess.run([sys.executable, str(HERE / "diagrams" / "make_diagrams.py"), str(repo / "diagrams" / "appendix")], check=True, capture_output=True)

    head = (HERE / "writeup_head.md").read_text()
    tail = (HERE / "writeup_tail.md").read_text()
    passed = sum(1 for e in evs if e["passed"])
    total_checks = sum(len(e["checks"]) for e in evs)
    passed_checks = sum(1 for e in evs for c in e["checks"] if c["passed"])
    summary = ["| Scenario | Title | Checks passed |", "|---|---|---|"]
    for e in evs:
        summary.append(f"| {e['id']} | {e['title']} | {sum(c['passed'] for c in e['checks'])} of {len(e['checks'])} |")
    body = "\n".join(scenario_section(e) for e in evs)
    impl = HERE.parent
    count = lambda pats: sum(len(p.read_text().splitlines()) for pat in pats for p in impl.glob(pat))
    sizes = (f"The Terraform is about {round(count(['terraform/*/*.tf']), -1)} lines; the platform's own code (the accounting service and the "
             f"fleet generator) about {round(count(['workloads/*.py', 'platform/*.py']), -1)} lines of Python; the test code about {round(count(['tests/*.py']), -1)} lines.")
    teardown = ""
    td = evidence / "teardown.txt"
    if td.exists():
        teardown = ("**The teardown after the final run.** This is the output of `verify-clean.sh` after `down.sh` removed the build that produced "
                    "the evidence above:\n\n```text\n" + td.read_text().strip() + "\n```")
    head = head.replace("{{CODE_SIZE}}", sizes)
    tail = tail.replace("{{TEARDOWN}}", teardown)
    page = head.replace("{{RESULT_SUMMARY}}", f"{passed} of {len(evs)} scenarios passed, {passed_checks} of {total_checks} checks.\n\n" + "\n".join(summary)) + "\n" + body + "\n" + tail
    out = repo / "content" / "appendix" / "a18-design-17-gpu-scheduling.md"
    out.write_text(page)
    print(f"wrote {out} ({len(page.splitlines())} lines); {passed}/{len(evs)} scenarios, {passed_checks}/{total_checks} checks")


if __name__ == "__main__":
    main()

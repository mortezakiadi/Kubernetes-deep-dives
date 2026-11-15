#!/usr/bin/env python3
"""Build index.html from content/*.md (scenario -> picture -> takeaway, per section).

Usage:
    pip install markdown
    python3 build.py

Each file in content/deep-dives/ is one tab. Inside it, every `### Title {#id}` heading
starts a section; a Markdown image line becomes a zoomable diagram.
Set `author` and `links` in content/00-intro.md to show them in the header.
"""
import html
import pathlib
import re

import markdown

ROOT = pathlib.Path(__file__).resolve().parent
REPO_URL = "https://github.com/mortezakiadi/Kubernetes-deep-dives"


def read_md(path):
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
        body = m.group(2)
    out = markdown.markdown(body, extensions=["extra", "sane_lists"])
    # Turn standalone images into zoomable diagram figures.
    def fig(mm):
        alt, src = html.unescape(mm.group(1)), mm.group(2)
        a = html.escape(alt)
        return (f'<figure class="diagram"><button class="open" type="button" data-src="{src}" data-title="{a}" '
                f'aria-label="Open diagram full screen: {a}"><img src="{src}" alt="Diagram: {a}" loading="lazy" '
                f'decoding="async"><span class="hint">Open full screen</span></button></figure>')
    out = re.sub(r'<p><img alt="([^"]*)" src="([^"]+)"\s*/?></p>', fig, out)
    return meta, out


def toc(body_html):
    items = re.findall(r'<h3 id="([^"]+)">(.*?)</h3>', body_html)
    if len(items) < 2:
        return ""
    lis = "".join(f'<li><a href="#{i}">{re.sub("<[^>]+>", "", t)}</a></li>' for i, t in items)
    return f'<nav class="toc" aria-label="Sections in this tab"><ol>{lis}</ol></nav>'


def main():
    intro_meta, intro_html = read_md(ROOT / "content" / "00-intro.md")
    tabs = sorted((ROOT / "content" / "deep-dives").glob("*.md"))
    nav, body = [], []
    n_sections = 0
    tabno = 0
    for p in tabs:
        meta, h = read_md(p)
        sid = meta["id"]
        n = len(re.findall(r"<h3 ", h))
        n_sections += h.count('class="diagram"')
        if sid != "reading-notes":
            tabno += 1
        label = "Notes" if sid == "reading-notes" else f"{tabno:02d}"
        cls = ' class="notes"' if sid == "reading-notes" else ""
        nav.append(f'<li{cls}><a href="#{sid}"><b>{label}</b> {html.escape(meta["short"])}</a></li>')
        kind = "notes" if sid == "reading-notes" else "tab"
        body.append(f"""
<section class="entry {kind}" id="{sid}" aria-labelledby="{sid}-h">
  <header class="entry-head">
    <span class="tag">{"Reading notes" if kind == "notes" else "Tab " + label + " · " + str(n) + (" sections" if n != 1 else " section")}</span>
    <h2 id="{sid}-h">{html.escape(meta["title"])}</h2>
  </header>
  {toc(h)}
  <div class="prose">{h}</div>
</section>""")

    byline = ""
    if intro_meta.get("author"):
        links = " · ".join(f'<a href="{u.strip()}">{html.escape(u.strip().split("//")[-1].rstrip("/"))}</a>'
                           for u in intro_meta.get("links", "").split(",") if u.strip())
        byline = f'<p class="byline">By {html.escape(intro_meta["author"])}{" · " + links if links else ""}</p>'

    values = dict(
        title=html.escape(intro_meta["title"]),
        subtitle=html.escape(intro_meta["subtitle"]),
        byline=byline,
        intro=intro_html,
        nav="\n".join(nav),
        body="\n".join(body),
        n_tabs=len(tabs) - 1,
        n_sections=n_sections,
        repo=REPO_URL,
    )
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    for k, v in values.items():
        page = page.replace("{{" + k + "}}", str(v))
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(f"index.html written: {len(tabs) - 1} tabs, {n_sections} diagram sections")


if __name__ == "__main__":
    main()

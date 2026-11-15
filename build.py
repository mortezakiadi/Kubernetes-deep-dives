#!/usr/bin/env python3
"""Builds index.html from the posts in content/."""
import html
import pathlib
import re

import markdown

ROOT = pathlib.Path(__file__).resolve().parent


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
    # Give every section heading an anchor: the id of its diagram (e.g. d01-3),
    # or a slug of the heading text when the section has no diagram.
    parts = re.split(r'(<h3>.*?</h3>)', out)
    for i, p in enumerate(parts):
        mh = re.fullmatch(r'<h3>(.*?)</h3>', p)
        if not mh:
            continue
        nxt = parts[i + 1] if i + 1 < len(parts) else ""
        ms = re.search(r'data-src="diagrams/sections/([^"/]+)\.svg"', nxt)
        if ms:
            hid = ms.group(1)
        else:
            text = re.sub(r"<[^>]+>", "", mh.group(1))
            hid = meta.get("id", "s") + "-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        parts[i] = f'<h3 id="{hid}">{mh.group(1)}</h3>'
    return meta, highlight("".join(parts))


def toc(body_html):
    items = re.findall(r'<h3 id="([^"]+)">(.*?)</h3>', body_html)
    if len(items) < 2:
        return ""
    lis = "".join(f'<li><a href="#{i}">{re.sub("<[^>]+>", "", t)}</a></li>' for i, t in items)
    return f'<nav class="toc" aria-label="Sections in this deep dive"><ol>{lis}</ol></nav>'


# ---- key-term highlighting: first use of each term per section ----
_ACTIONS = [r"filter(?:s|ed|ing)?", r"scor(?:e|es|ed|ing)", r"bind(?:s|ing)?", r"preempt(?:s|ed|ing|ion)?",
            r"evict(?:s|ed|ing|ion)?", r"admit(?:s|ted|ting)?", r"borrow(?:s|ed|ing)?", r"throttl(?:e|es|ed|ing)",
            r"reclaim(?:s|ed|ing)?", r"autoscal(?:e|es|ed|ing|er)", r"scale[sd]? (?:up|down|out|to zero)", r"bin-pack(?:s|ed|ing)?",
            r"checkpoint(?:s|ed|ing)?", r"cordon(?:s|ed)?|drain(?:s|ed|ing)?"]
_CONCEPTS = [r"gang scheduling", r"fair[- ]sharing", r"quotas?", r"cohorts?", r"topology-aware(?: scheduling)?", r"Kueue",
             r"Volcano", r"KAI(?: Scheduler)?", r"JobSets?", r"DRA", r"MIG", r"time[- ]slicing", r"Karpenter",
             r"Cluster Autoscaler", r"HPA", r"KEDA", r"PriorityClass(?:es)?", r"QoS", r"cgroups?", r"CFS", r"OOM ?kill(?:s|ed|er)?",
             r"TTFT", r"TPOT", r"KV cache", r"prefix cach(?:e|ing)", r"LoRA", r"vLLM", r"KServe", r"NetworkPolic(?:y|ies)",
             r"ServiceAccounts?", r"IRSA", r"Pod Identity", r"SPIFFE(?:/SPIRE)?", r"mTLS", r"MCP", r"A2A", r"RAG", r"NUMA",
             r"EFA", r"RDMA", r"NVLink", r"VPC CNI", r"prefix delegation", r"conntrack", r"SNAT", r"ENIs?", r"CoreDNS",
             r"taints?", r"tolerations?", r"(?:node |pod )?affinity", r"PDBs?", r"StatefulSets?", r"PVCs?", r"EBS", r"EFS",
             r"FSx(?: for Lustre)?", r"NVMe", r"requests and limits", r"device plugin", r"GPU Operator", r"guardrails?",
             r"disaggregated serving", r"TokenReview", r"SubjectAccessReview", r"token exchange"]
_KW = re.compile(r"(?<![\w-])(?:(?P<a>" + "|".join(_ACTIONS) + r")|(?P<c>" + "|".join(_CONCEPTS) + r"))(?![\w-])", re.I)
_SKIP = {"code", "pre", "a", "h1", "h2", "h3", "h4", "strong", "figcaption", "button", "figure", "mark", "th", "script", "style"}


def highlight(h):
    out, depth, seen = [], 0, set()
    for tok in re.split(r"(<[^>]+>)", h):
        if tok.startswith("<"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", tok)
            if m:
                name = m.group(2).lower()
                if name == "h3" and not m.group(1):
                    seen = set()
                if name in _SKIP and not tok.endswith("/>"):
                    depth += -1 if m.group(1) else 1
            out.append(tok)
            continue
        if depth > 0 or not tok.strip():
            out.append(tok)
            continue

        def rep(mm):
            key = re.sub(r"(?:es|s|ed|ing|ion|er|d|ted|ting)$", "", mm.group(0).lower())[:6]
            if key in seen:
                return mm.group(0)
            seen.add(key)
            cls = "kw-act" if mm.group("a") else "kw"
            return f'<mark class="{cls}">{mm.group(0)}</mark>'
        out.append(_KW.sub(rep, tok))
    return "".join(out)


def main():
    intro_meta, intro_html = read_md(ROOT / "content" / "00-intro.md")
    posts = sorted((ROOT / "content" / "notes").glob("*.md"))
    tabs = sorted((ROOT / "content" / "deep-dives").glob("*.md"))
    nav_posts, nav, body = [], [], []
    n_sections = 0
    entries = [("tab", p) for p in tabs] + [("post", p) for p in posts]
    tabno = 0
    for kind, p in entries:
        meta, h = read_md(p)
        sid = meta["id"]
        n = len(re.findall(r"<h3 ", h))
        n_sections += h.count('class="diagram"')
        if kind == "tab":
            tabno += 1
            label = f"{tabno:02d}"
            tag = f"Deep dive {label} · {n} section" + ("s" if n != 1 else "")
            nav.append(f'<li><a href="#{sid}"><b>{label}</b> {html.escape(meta["short"])}</a></li>')
        else:
            tag = "Further research"
            nav_posts.append(f'<li class="notes"><a href="#{sid}">{html.escape(meta["short"])}</a></li>')
        body.append(f"""
<section class="entry {kind}" id="{sid}" aria-labelledby="{sid}-h">
  <header class="entry-head">
    <span class="tag">{tag}</span>
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
        nav_posts="\n".join(nav_posts),
        body="\n".join(body),
        n_tabs=len(tabs),
        n_sections=n_sections,
    )
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    for k, v in values.items():
        page = page.replace("{{" + k + "}}", str(v))
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(f"index.html written: {len(posts)} posts, {len(tabs)} tabs, {n_sections} diagram sections")


if __name__ == "__main__":
    main()

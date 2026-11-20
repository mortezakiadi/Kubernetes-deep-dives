#!/usr/bin/env python3
"""Builds index.html from the posts in content/."""
import html
import pathlib
import re

import markdown
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = pathlib.Path(__file__).resolve().parent



def _indent_lists(md):
    """Python-Markdown needs 4 spaces for nested lists; content uses 2. Double list indents outside code fences."""
    out, fence = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            m = re.match(r"^((?:> )?)( +)(- |\d+\. )", line)
            if m:
                line = m.group(1) + m.group(2) * 2 + line[m.end(2):]
        out.append(line)
    return "\n".join(out)


def _bq_fences(md):
    """Fenced code inside blockquotes ("> ```yaml"): Python-Markdown can't render these,
    so swap each for a placeholder paragraph and return the rendered blocks."""
    blocks = {}
    def rep(m):
        prefix, lang, body = m.group(1), m.group(2), m.group(3)
        code = "\n".join(re.sub(r"^>\s?", "", l)[len(prefix) - 2:] if len(prefix) > 2 else re.sub(r"^>\s?", "", l)
                         for l in body.split("\n"))
        key = f"BQCODE{len(blocks)}X"
        blocks[key] = f'<pre><code class="language-{lang}">{html.escape(code, quote=False)}\n</code></pre>'
        return f"{prefix}{key}\n>"
    md = re.sub(r"^(> *)```(\w+)\n((?:>.*\n)*?)> *```$", rep, md, flags=re.M)
    return md, blocks

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
    body, bq = _bq_fences(body)
    out = markdown.markdown(_indent_lists(body), extensions=["extra", "sane_lists"])
    for k, v in bq.items():
        out = re.sub(r"<p>\s*" + k + r"\s*</p>", lambda _m, v=v: v, out).replace(k, v)
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
        ms = re.search(r'data-src="diagrams/(?:sections|designs|appendix|posts)/([^"/]+)\.svg"', nxt)
        if ms:
            hid = ms.group(1)
        else:
            text = re.sub(r"<[^>]+>", "", mh.group(1))
            hid = meta.get("id", "s") + "-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        parts[i] = f'<h3 id="{hid}">{mh.group(1)}</h3>'
    return meta, accordionize(highlight("".join(parts)))


def toc(body_html):
    items = re.findall(r'<h3 id="([^"]+)">(.*?)</h3>', body_html)
    if len(items) < 2:
        return ""
    lis = "".join(f'<li><a href="#{i}">{re.sub("<[^>]+>", "", t)}</a></li>' for i, t in items)
    return f'<nav class="toc" aria-label="Sections in this deep dive"><ol>{lis}</ol></nav>'


# ---- key-term highlighting: first use of each term per block ----
_ACTIONS = [r"filter(?:s|ed|ing)?", r"scor(?:e|es|ed|ing)", r"bind(?:s|ing)?", r"preempt(?:s|ed|ing|ion)?",
            r"evict(?:s|ed|ing|ion)?", r"admit(?:s|ted|ting)?", r"admission", r"borrow(?:s|ed|ing)?", r"throttl(?:e|es|ed|ing)",
            r"reclaim(?:s|ed|ing)?", r"autoscal(?:e|es|ed|ing)", r"scale[sd]? (?:up|down|out|to zero)", r"scale-to-zero",
            r"bin-pack(?:s|ed|ing)?", r"checkpoint(?:s|ed|ing)?", r"cordon(?:s|ed)?", r"drain(?:s|ed|ing)?",
            r"consolidat(?:e|es|ed|ing|ion)", r"requeu(?:e|es|ed|ing)", r"quantiz(?:e|es|ed|ing|ation)",
            r"spread(?:s|ing)?", r"force-detach(?:es|ed|ing)?", r"rotat(?:e|es|ed|ing|ion)"]
_CONCEPTS_I = [r"gang scheduling", r"all-or-nothing", r"fair[- ]sharing", r"quotas?", r"cohorts?", r"topology-aware(?: scheduling| routing)?",
             r"Dynamic Resource Allocation", r"time[- ]slicing", r"cgroups?(?: v[12])?", r"OOM[- ]?kill(?:s|ed|er)?",
             r"KV[- ]cache", r"prefix cach(?:e|ing)", r"prefill", r"decode", r"requests and limits", r"device plugin",
             r"guardrails?", r"disaggregated serving", r"token exchange", r"envelope encryption", r"hallucinations?",
             r"LLM-as-a-judge", r"tensor parallelism", r"pipeline parallelism", r"all-reduce", r"rendezvous",
             r"page cache", r"working set(?: size)?", r"write amplification", r"jumbo frames", r"prefix delegation",
             r"ENI trunking", r"trunk ENI", r"branch ENI", r"VPC endpoints?", r"node affinity", r"pod affinity",
             r"(?:pod )?anti-affinity", r"affinity", r"taints?", r"tolerations?", r"topology spread(?: constraints)?",
             r"user namespaces?", r"agent card", r"vector database", r"embedding(?: model)?s?", r"rerank(?:er|ers|ing)",
             r"idempotent", r"Saga pattern", r"write-ahead log", r"burst credits", r"fuel tank", r"warm pool",
             r"noisy neighbou?r", r"blast radius", r"zero[- ]trust", r"mutating webhook", r"headless Service",
             r"Pod Security (?:Standards|Admission)", r"user-space kernel", r"scheduling profile", r"priority classes",
             r"CPU shares", r"quota window", r"latency jitter", r"I/O wait", r"Direct I/O"]
_CONCEPTS_C = [r"DRA", r"MIG", r"PriorityClass(?:es)?", r"QoS", r"Guaranteed", r"Burstable", r"BestEffort", r"CFS", r"EEVDF",
             r"TTFT", r"TPOT", r"LoRA", r"QLoRA", r"PEFT", r"RAG", r"NetworkPolic(?:y|ies)", r"ServiceAccounts?", r"RBAC",
             r"seccomp", r"AppArmor", r"SELinux", r"mTLS", r"NUMA", r"CPU [Mm]anager", r"Topology Manager", r"SR-IOV",
             r"RDMA", r"NVLink", r"NVSwitch", r"InfiniBand", r"RoCE", r"EFA", r"GPUDirect(?: RDMA| Storage)?", r"conntrack",
             r"[Nn]etfilter", r"PREROUTING", r"FORWARD", r"POSTROUTING", r"DNAT", r"SNAT", r"MTU", r"ndots", r"ENIs?",
             r"PrivateLink", r"PDBs?", r"PodDisruptionBudgets?", r"StatefulSets?", r"PVCs?", r"PersistentVolumes?",
             r"PersistentVolumeClaims?", r"StorageClass(?:es)?", r"EBS", r"EFS", r"FSx(?: for Lustre)?", r"NVMe", r"IOPS",
             r"gp2", r"gp3", r"io2(?: Block Express)?", r"Multi-Attach", r"TokenReview", r"SubjectAccessReview", r"IRSA",
             r"EKS Pod Identity", r"Pod Identity", r"KMS", r"Safetensors", r"GGUF", r"Spot", r"On-Demand", r"WAL",
             r"ResourceFlavors?", r"ClusterQueues?", r"LocalQueues?", r"ResourceQuotas?", r"LimitRanges?", r"Workload API",
             r"PodGroups?", r"InferencePool", r"endpoint picker", r"RuntimeClass", r"OIDC", r"FSDP2?", r"VXLAN",
             r"LeastAllocated", r"MostAllocated", r"NodeResourcesFit", r"ImageLocality", r"BalancedResourceAllocation",
             r"InterPodAffinity", r"NodeUnschedulable", r"NodeName", r"NodePorts", r"TaintToleration", r"NodeAffinity", r"PodTopologySpread", r"VolumeBinding", r"VolumeZone", r"VolumeRestrictions", r"NodeVolumeLimits", r"DynamicResources", r"PrioritySort", r"DefaultPreemption", r"DefaultBinder", r"SchedulingGates", r"PostFilter", r"ENA(?: Express)?", r"SRD", r"XFS", r"ext4"]
_TOOLS = [r"Kueue", r"Volcano", r"KAI(?: Scheduler)?", r"YuniKorn", r"Armada", r"Run:ai", r"JobSets?", r"LeaderWorkerSet",
          r"Kubeflow Trainer", r"TrainJobs?", r"Karpenter", r"Cluster Autoscaler", r"HPA", r"KEDA", r"Knative",
          r"descheduler", r"kube-proxy", r"kube-scheduler", r"kubelet", r"CoreDNS", r"NodeLocal DNSCache", r"Calico",
          r"Cilium", r"VPC CNI", r"iptables", r"IPVS", r"nftables", r"eBPF", r"XDP", r"WireGuard", r"IPsec", r"Istio",
          r"Linkerd", r"gVisor", r"Kata Containers", r"Kata", r"Fargate", r"vLLM", r"KServe", r"SGLang", r"TGI",
          r"NVIDIA NIM", r"llm-d", r"NVIDIA Dynamo", r"Ray Serve", r"KubeRay", r"GPU Operator", r"DCGM(?: exporter)?",
          r"GPU Feature Discovery", r"Node Feature Discovery", r"NCCL", r"Velero", r"OPA Gatekeeper", r"OPA", r"Kyverno",
          r"Falco", r"Trivy", r"Cosign", r"Snyk", r"Prometheus", r"Thanos", r"Fluent Bit", r"Jaeger", r"OpenTelemetry",
          r"SPIFFE(?:/SPIRE)?", r"MCP", r"A2A", r"Gateway API Inference Extension", r"Envoy AI Gateway", r"Kuadrant",
          r"Mountpoint for S3(?: CSI driver)?", r"EBS CSI driver", r"Node Termination Handler", r"Kagent", r"LMCache",
          r"NIXL", r"GuideLLM", r"inference-perf", r"lm-evaluation-harness", r"TrustyAI", r"Ragas", r"garak",
          r"Unsloth", r"DeepSpeed", r"Hugging Face TRL", r"Docling", r"Llama Guard", r"Granite Guardian",
          r"NeMo Guardrails", r"Kubecost", r"Goldilocks", r"StormForge", r"KRR", r"Graviton", r"Capacity Blocks",
          r"Savings Plans", r"Topograph", r"Network Operator", r"agent sandbox"]
def _alt(xs): return "|".join(sorted(xs, key=len, reverse=True))
_KW = re.compile(r"(?<![\w/-])(?:(?P<a>(?i:" + _alt(_ACTIONS) + r"))|(?P<t>" + _alt(_TOOLS) + r")|(?P<c>(?i:" + _alt(_CONCEPTS_I)
                 + r")|" + _alt(_CONCEPTS_C) + r"))(?![\w-])")
_SKIP = {"code", "pre", "a", "h1", "h2", "h3", "h4", "strong", "figcaption", "button", "figure", "mark", "th", "script", "style", "table"}
_LABELS = {"Scenario.", "What the picture shows.", "Going deeper."}


def _norm(w):
    w = w.lower()
    return re.sub(r"(?:es|s|ed|ing|ion|ation)$", "", w) if len(w) > 4 else w


def highlight(h):
    out, depth, seen = [], 0, set()
    for tok in re.split(r"(<[^>]+>)", h):
        if tok.startswith("<"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", tok)
            if m:
                name = m.group(2).lower()
                if name in ("h3", "h2") and not m.group(1):
                    seen = set()
                if name in _SKIP and not tok.endswith("/>"):
                    depth += -1 if m.group(1) else 1
            out.append(tok)
            continue
        if tok.strip() in _LABELS:
            seen = set()
        if depth > 0 or not tok.strip():
            out.append(tok)
            continue

        def rep(mm):
            key = _norm(mm.group(0))
            if key in seen:
                return mm.group(0)
            seen.add(key)
            cls = "kw-act" if mm.group("a") else ("kw-tool" if mm.group("t") else "kw")
            return f'<mark class="{cls}">{mm.group(0)}</mark>'
        out.append(_KW.sub(rep, tok))
    return code_blocks("".join(out))


# ---- config snippets: light syntax colouring + key lines (marked with a trailing "#!") ----
def _fmt_line(line, lang):
    key = line.rstrip().endswith("#!")
    if key:
        line = re.sub(r"\s*#!\s*$", "", line)
    code, comment = line, ""
    m = re.search(r"(^|\s)#(?!!)", line)
    if m:
        code, comment = line[:m.start()] + m.group(1), line[m.start() + len(m.group(1)):]
    if lang == "yaml":
        code = re.sub(r"^(\s*(?:- )?)([A-Za-z0-9_.\/-]+)(:)(?=\s|$)", r'\1<span class="tk">\2</span>\3', code)
    html_line = code + (f'<span class="tc">{comment}</span>' if comment else "")
    return f'<span class="ln{" kl" if key else ""}">{html_line or " "}</span>'


def code_blocks(h):
    def rep(m):
        lang, body = m.group(1), m.group(2)
        lines = body.rstrip("\n").split("\n")
        cls = "snippet numbered" if lines and lines[0].startswith("# from ") else "snippet"
        return f'<pre class="{cls}"><code class="language-{lang}">' + "".join(_fmt_line(l, lang) for l in lines) + "</code></pre>"
    return re.sub(r'<pre><code class="language-(\w+)">(.*?)</code></pre>', rep, h, flags=re.S)



# ---- "What the picture shows": one collapsible accordion per item in the picture ----
def _label(node):
    if isinstance(node, Tag) and node.name == "p":
        st = node.find("strong", recursive=False)
        if st is not None and node.contents and node.contents[0] is st:
            return st.get_text().strip()
    return None


# A paragraph that starts with a bold "Step N. Title." opens a collapsible step. The step runs until the next
# step or the next "What the picture shows." / "Going deeper." / "Scenario." label or heading. Consecutive steps
# share one Expand all / Collapse all bar. Every step starts collapsed, like the other accordions.
_STEP_RE = re.compile(r"^Step\s+(\d+)\.\s*(.*?)\.?$")
_STEP_END = ("What the picture shows.", "Going deeper.", "Scenario.")


def _stepify(soup, root):
    nodes = [n for n in root.children if isinstance(n, Tag)]
    i = 0
    while i < len(nodes):
        lab = _label(nodes[i])
        if not (lab and _STEP_RE.match(lab)):
            i += 1
            continue
        j = i
        while j < len(nodes) and nodes[j].name not in ("h2", "h3") and _label(nodes[j]) not in _STEP_END:
            j += 1
        run = nodes[i:j]
        wrap = soup.new_tag("div", attrs={"class": "acc-region steps"})
        run[0].insert_before(wrap)
        wrap.append(BeautifulSoup('<div class="acc-tools"><button type="button" data-acc="open">Expand all</button>'
                                  '<button type="button" data-acc="close">Collapse all</button></div>', "html.parser"))
        body = None
        for node in run:
            lab = _label(node)
            m = _STEP_RE.match(lab) if lab else None
            node = node.extract()
            if m:
                det = soup.new_tag("details", attrs={"class": "acc step"})
                summ = soup.new_tag("summary")
                badge = soup.new_tag("span", attrs={"class": "acc-num"}); badge.string = m.group(1)
                title = soup.new_tag("span", attrs={"class": "acc-title"}); title.string = m.group(2)
                summ.append(badge); summ.append(title)
                body = soup.new_tag("div", attrs={"class": "acc-body"})
                node.find("strong", recursive=False).extract()
                if node.contents and isinstance(node.contents[0], NavigableString):
                    node.contents[0].replace_with(node.contents[0].lstrip())
                if node.get_text(strip=True):
                    body.append(node)
                det.append(summ); det.append(body)
                wrap.append(det)
            else:
                (body if body is not None else wrap).append(node)
        i = j


def accordionize(h):
    soup = BeautifulSoup(f"<div id='__root'>{h}</div>", "html.parser")
    root = soup.find(id="__root")
    _stepify(soup, root)
    nodes = [n for n in root.children if isinstance(n, Tag)]
    i = 0
    while i < len(nodes):
        if _label(nodes[i]) != "What the picture shows.":
            i += 1
            continue
        j = i + 1
        region = []
        while j < len(nodes) and nodes[j].name not in ("h2", "h3") and _label(nodes[j]) not in ("Going deeper.", "Scenario."):
            region.append(nodes[j]); j += 1
        n_items = sum(len(u.find_all("li", recursive=False)) for u in region if u.name == "ul")
        if n_items >= 2:
            wrap = soup.new_tag("div", attrs={"class": "acc-region"})
            bar = BeautifulSoup('<div class="acc-tools"><button type="button" data-acc="open">Expand all</button>'
                                '<button type="button" data-acc="close">Collapse all</button></div>', "html.parser")
            wrap.append(bar)
            region[0].insert_before(wrap)
            num, last = 0, None
            for node in region:
                node = node.extract()
                if node.name == "ul":
                    for li in node.find_all("li", recursive=False):
                        num += 1
                        det = soup.new_tag("details", attrs={"class": "acc"})
                        summ = soup.new_tag("summary")
                        body = soup.new_tag("div", attrs={"class": "acc-body"})
                        first = li.find(["p"], recursive=False) or li
                        st = first.find("strong", recursive=False)
                        lead = [x for x in first.contents if not (isinstance(x, NavigableString) and not x.strip())]
                        if st is not None and lead and lead[0] is st:
                            title = st.get_text().strip().rstrip(":").strip()
                            st.extract()
                            if first.contents and isinstance(first.contents[0], NavigableString):
                                first.contents[0].replace_with(first.contents[0].lstrip().lstrip(":").lstrip())
                        else:
                            t = li.get_text(" ", strip=True)
                            title = (t[:70] + "…") if len(t) > 70 else t
                        badge = soup.new_tag("span", attrs={"class": "acc-num"}); badge.string = str(num)
                        tt = soup.new_tag("span", attrs={"class": "acc-title"}); tt.string = title
                        summ.append(badge); summ.append(tt)
                        for ch in list(li.contents):
                            body.append(ch.extract())
                        det.append(summ); det.append(body)
                        wrap.append(det)
                        last = body
                elif node.name in ("pre", "blockquote", "table", "div") and last is not None:
                    last.append(node)
                else:
                    wrap.append(node)
                    last = None
        i = j
    # "Going deeper": one collapsible block per section
    nodes = [n for n in root.children if isinstance(n, Tag)]
    i = 0
    while i < len(nodes):
        if _label(nodes[i]) != "Going deeper.":
            i += 1
            continue
        j = i + 1
        while j < len(nodes) and nodes[j].name not in ("h2", "h3") and _label(nodes[j]) not in ("Scenario.", "What the picture shows."):
            j += 1
        first = nodes[i]
        det = soup.new_tag("details", attrs={"class": "acc gd"})
        summ = soup.new_tag("summary")
        tt = soup.new_tag("span", attrs={"class": "acc-title"}); tt.string = "Going deeper"
        n_pts = sum(len(u.find_all("li", recursive=False)) for u in nodes[i + 1:j] if u.name == "ul")
        summ.append(tt)
        if n_pts:
            cnt = soup.new_tag("span", attrs={"class": "acc-count"}); cnt.string = f"{n_pts} points"
            summ.append(cnt)
        body = soup.new_tag("div", attrs={"class": "acc-body"})
        det.append(summ); det.append(body)
        first.insert_before(det)
        first.find("strong", recursive=False).extract()
        if first.get_text(strip=True):
            if first.contents and isinstance(first.contents[0], NavigableString):
                first.contents[0].replace_with(first.contents[0].lstrip())
            body.append(first.extract())
        else:
            first.extract()
        for n in nodes[i + 1:j]:
            body.append(n.extract())
        i = j
    return root.decode_contents()


def main():
    intro_meta, intro_html = read_md(ROOT / "content" / "00-intro.md")
    posts = sorted((ROOT / "content" / "notes").glob("*.md"))
    tabs = sorted((ROOT / "content" / "deep-dives").glob("*.md"))
    designs = sorted((ROOT / "content" / "designs").glob("*.md"))
    apps = sorted((ROOT / "content" / "appendix").glob("*.md"))
    nav_posts, nav, nav_design, nav_app, body = [], [], [], [], []
    n_sections = 0
    entries = ([("tab", p) for p in tabs] + [("design", p) for p in designs]
               + [("post", p) for p in posts] + [("appendix", p) for p in apps])
    tabno = designno = 0
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
        elif kind == "design":
            designno += 1
            label = f"{designno:02d}"
            tag = f"Design {label} · {n} section" + ("s" if n != 1 else "")
            nav_design.append(f'<li class="design"><a href="#{sid}"><b>{label}</b> {html.escape(meta["short"])}</a></li>')
        elif kind == "appendix":
            tag = "Appendix"
            nav_app.append(f'<li class="notes"><a href="#{sid}">{html.escape(meta["short"])}</a></li>')
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
        nav_designs="\n".join(nav_design),
        nav_appendix="\n".join(nav_app),
        body="\n".join(body),
        n_tabs=len(tabs),
        n_designs=len(designs),
        n_sections=n_sections,
    )
    page = (ROOT / "template.html").read_text(encoding="utf-8")
    for k, v in values.items():
        page = page.replace("{{" + k + "}}", str(v))
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(f"index.html written: {len(posts)} posts, {len(tabs)} tabs, {len(designs)} designs, {n_sections} diagram sections")


if __name__ == "__main__":
    main()

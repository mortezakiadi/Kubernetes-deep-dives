# Kubernetes deep dives

How Kubernetes and Amazon EKS schedule, isolate and run workloads, explained one scenario at a time.

Every section starts with a **scenario** (a real problem or question), followed by the part of the diagram that explains it and a short takeaway. The scheduling tabs also include notes from the *Job Scheduling Optimization* chapter of **Generative AI on Kubernetes**, summarized in my own words.

**Live site:** https://mortezakiadi.github.io/Kubernetes-deep-dives/ (after GitHub Pages is enabled, see below)

## Tabs

| # | Tab | Sections |
|---|-----|----------|
| – | Reading notes: job scheduling for AI training | 6 |
| 01 | How kube-scheduler picks a node | 8 |
| 02 | When one scheduler isn't enough (Kueue, Volcano, KAI, YuniKorn) | 8 |
| 03 | Batch and real-time on one cluster | 8 |
| 04 | From a YAML limit to a kernel quota (cgroups) | 2 |
| 05 | Failure modes: storage, memory and controller loops | 10 |
| 06 | How GPUs become schedulable | 1 |
| 07 | Multi-tenancy: soft vs. hard isolation | 6 |
| 08 | Stateful workloads: databases on EKS | 11 |
| 09 | EKS pod networking: VPC CNI to Cilium | 9 |
| 10 | Node networking and the high-performance path | 5 |
| 11 | Troubleshooting networking | 5 |
| 12 | Cost optimization on EKS | 3 |
| 13 | EKS security from build to runtime | 6 |
| 14 | StorageClass, PV and PVC | 1 |

## Repository layout

```
index.html                          the built site (GitHub Pages serves this)
template.html                       layout, styles and the zoomable diagram viewer
build.py                            builds index.html from content/ (pip install markdown)
content/00-intro.md                 title, subtitle, author and links
content/deep-dives/*.md             one file per tab; each "### Title {#id}" is a section
diagrams/sections/*.svg             one diagram per section
drawio/EKS-notes.drawio             the original full diagrams (14 pages)
drawio/EKS-deep-dive-sections.drawio  the same diagrams split into one page per section
tools/split_sections.py             splits EKS-notes.drawio into sections
tools/clean_svg.py                  shrinks draw.io SVG exports for the web
CORRECTIONS.md                      technical corrections made to the original diagrams
```

## Turn on the website (GitHub Pages)

1. In this repository, go to **Settings → Pages**.
2. Under **Build and deployment**, choose **Deploy from a branch**, select `main` and `/ (root)`, and click **Save**.
3. After a minute or two, the site is live at `https://mortezakiadi.github.io/Kubernetes-deep-dives/`.

## Edit the text

Edit the Markdown in `content/`, then rebuild and commit:

```bash
pip install markdown
python3 build.py
```

To show your name and profile links in the header, fill in `author` and `links` (comma-separated URLs) at the top of `content/00-intro.md`.

## Edit a diagram

1. Open `drawio/EKS-deep-dive-sections.drawio` in [diagrams.net](https://app.diagrams.net) or draw.io Desktop. Each page is one section, named after its section id (for example `d01-3`).
2. Export the page as SVG to `diagrams/sections/<id>.svg`.
3. Run `python3 tools/clean_svg.py diagrams/sections/*.svg`, then `python3 build.py`.

If you change the full diagrams in `drawio/EKS-notes.drawio` instead, run `python3 tools/split_sections.py drawio/EKS-notes.drawio drawio/EKS-deep-dive-sections.drawio` to regenerate the section pages, then export them as above.

---
title: "Kubernetes deep dives"
subtitle: "How Kubernetes schedules, isolates and runs workloads: from kube-scheduler and gang scheduling to cgroups, ENIs and EBS. Each idea is shown as a real scenario first, then the diagram that explains it."
author: "Morteza Kiadi"
links: ""
---
I drew these diagrams while working through how Kubernetes and Amazon EKS really behave in production. Every section here starts with a **scenario**, a concrete problem or question, followed by the part of my diagram that explains it and a short note on what to take away.

The **Designs** section turns those ideas into complete platform designs, one use case at a time: requirements, threat model, architecture, configuration and the trade-offs behind each choice.

Click any diagram to open it full screen, then scroll or pinch to zoom and drag to move around.

<p class="legend">How to read the colours: <mark class="kw-act">actions</mark> the system takes, <mark class="kw">concepts and Kubernetes objects</mark>, and <mark class="kw-tool">tools and projects</mark>. In config snippets, the <span style="background:rgba(250,204,21,.25);box-shadow:inset 3px 0 #facc15;padding:0 6px">highlighted lines</span> are the ones that matter for that point.</p>

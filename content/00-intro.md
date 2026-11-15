---
title: "Kubernetes deep dives"
subtitle: "How EKS schedules, isolates and runs workloads: from kube-scheduler and gang scheduling to cgroups, ENIs and EBS. Each idea is shown as a real scenario first, then the diagram that explains it."
author: "Morteza Kiadi"
links: ""
---
I drew these diagrams while working through how Kubernetes and Amazon EKS really behave in production. Every section here starts with a **scenario**, a concrete problem or question, followed by the part of my diagram that explains it and a short note on what to take away.

The scheduling tabs also include notes from the *Job Scheduling Optimization* chapter of **Generative AI on Kubernetes**, summarized in my own words and linked to the matching diagrams.

Click any diagram to open it full screen, then scroll or pinch to zoom and drag to move around.

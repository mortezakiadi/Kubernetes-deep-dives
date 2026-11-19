---
id: design-tenant-platform
title: "Design 27: A multi-tenant platform between engineers and the outside world"
short: "Multi-tenant platform services"
---
This design answers one question: *how do we build and own a platform that gives dozens of teams isolated capacity, routing, quotas and observability on demand, safely, and keep it healthy from architecture review through years of production?* Earlier designs built the pieces: sandboxed execution ([Design 01](#design-sandboxing)), multi-cluster placement ([Design 02](#design-multicluster)), networking ([Design 05](#design-networking)), GPU scheduling ([Design 17](#design-gpu-scheduling)), observability ([Design 24](#design-gpu-observability)) and cost attribution ([Design 25](#design-cost-capacity)). This design is the product that wraps them: a tenant API, the controllers that turn a tenant request into everything a team needs, the boundaries between tenants and between tenants and the internet, and the ownership practices that keep a shared platform trustworthy.

The short version: *one declarative tenant object, reconciled into everything, with isolation by default and ownership written down*. A team asks for a tenant (or a change to one) with a small custom resource reviewed in Git. A tenant operator reconciles it into namespaces, access, quotas and GPU queues, network and admission policies, gateway routes, egress allow-lists, observability tenants, dashboards and cost labels, in minutes. Shared ingress and egress gateways are the only paths to and from the outside world, and each tenant's traffic is routed, limited and logged under its own identity. The platform itself is run as a product, with SLOs, a service catalog, production readiness reviews, tenant-facing changelogs and a deprecation process.

### The use case and requirements

**Scenario.** Forty teams build on the platform: research groups, product teams serving models, and data teams. Getting started took a sequence of tickets: a namespace (3 days), GPU quota (a week), a public route with TLS (a week), access to an external model API through the firewall (two weeks), and dashboards (whenever someone had time). Each ticket was done by hand, slightly differently. When an audit asked which teams could reach which external services, nobody could answer.

![Five tickets to get started](diagrams/designs/concepts/ds27-c1.svg)

**What the platform must deliver.**

- **Self-service tenants:** a team gets a working tenant (namespaces, access, quotas, routing, observability) from one reviewed request, in under 30 minutes.
- **Isolation:** tenants can't see or affect each other's workloads, traffic, data or telemetry, with stronger isolation available for higher-risk workloads.
- **Controlled paths to the outside world:** inbound through shared gateways with TLS and authentication, outbound only to allow-listed destinations, all attributable to a tenant.
- **Quotas and fairness:** compute, GPUs, storage, API request rates and telemetry volume per tenant.
- **Observability on demand:** metrics, logs, traces and dashboards scoped to each tenant from the start.
- **Ownership:** the platform's own services have owners, SLOs, runbooks and on-call, from architecture review to deprecation.
- **Auditability:** who can do what, and which tenant reached which external service, answerable from data.

**Out of scope:** the internals of GPU scheduling, serving and training (earlier designs), and corporate identity management.

### The tenancy model: isolation tiers

**Scenario.** Every team got the same thing: a namespace on a shared cluster. A team running untrusted user-submitted code got the same isolation as a team running a dashboard, and a team bound by data-residency rules shared nodes with everyone.

![Isolation tiers](diagrams/designs/concepts/ds27-c2.svg)

![The tenancy model: isolation tiers](diagrams/designs/ds27-1.svg)

**What the picture shows.** Four isolation tiers, chosen per tenant (or per environment of a tenant) by risk.

- **Tier 1, shared namespaces:** namespaces on shared nodes, with RBAC, quotas, network policies, the restricted Pod Security standard and admission policies. The default for most teams.
- **Tier 2, dedicated nodes:** tier 1 plus node pools tainted for the tenant. For noisy, sensitive or GPU-heavy workloads.
- **Tier 3, sandboxed or virtual clusters:** workloads in sandboxed runtimes ([Design 01](#design-sandboxing)), or a virtual control plane per tenant for teams that need cluster-scoped resources (their own CRDs and operators).
- **Tier 4, dedicated clusters:** a separate cluster for regulatory or residency requirements, still created and managed through the same tenant API ([Design 02](#design-multicluster)).
- **Choosing a tier:** from a short questionnaire (untrusted code, regulated data, cluster-scoped needs, residency), recorded with the tenant and revisited at review.

**Going deeper.**

- **The same API at every tier:** a team that moves from tier 1 to tier 2 changes one field. The operator does the rest, and everything else (routes, dashboards, quotas) stays the same.
- **Higher tiers cost more:** dedicated nodes and clusters lower utilization. The cost shows on the tenant's statement ([Design 25](#design-cost-capacity)), so tier choices are deliberate.

### The tenant API and its operator

**Scenario.** The onboarding runbook had 34 steps across six tools. Two tenants created from it six months apart differed in 11 ways, and fixing a mistake in the runbook didn't fix any tenant already created.

![Tenant operator](diagrams/designs/concepts/ds27-c3.svg)

![The tenant API and its operator](diagrams/designs/ds27-2.svg)

**What the picture shows.** One custom resource per tenant, reconciled into everything the tenant needs, continuously.

- **Tenant resource:** owners, tier, environments, quotas, GPU queues, routes (hostnames), egress destinations, observability limits and cost center. Stored in Git, changed by pull request with platform review for tier and quota increases.
- **Tenant operator:** reconciles the tenant into namespaces, role bindings, quotas and limit ranges, Kueue LocalQueues ([Design 17](#design-gpu-scheduling)), network and admission policies, gateway route permissions, egress policies, observability tenants and dashboards, and cost labels.
- **Continuous reconciliation:** drift (a deleted network policy, a hand-edited quota) is reverted and reported. Template improvements roll out to every tenant.
- **Status:** the tenant's status lists each component and whether it's ready, so "is my tenant set up?" has an answer.
- **Deletion:** removing a tenant archives data, revokes access and removes resources in order, after a grace period.

**The YAML.** A tenant definition (platform API; reconciled by the tenant operator).

```yaml
apiVersion: platform.example.com/v1
kind: Tenant
metadata: { name: search-ranking }
spec:
  owners: { group: "oidc:team-search-ranking", oncall: "pagerduty:search-ranking" }
  costCenter: CC-4102
  tier: shared-namespaces                       # shared-namespaces | dedicated-nodes | virtual-cluster | dedicated-cluster
  environments: [dev, prod]
  quotas:
    cpu: "400"
    memory: 1600Gi
    storage: 20Ti
    gpus: { queue: search-ranking, nominal: 32, borrowLimit: 32, flavor: h100 }   # becomes a Kueue ClusterQueue entry
  routes:
  - { host: ranking.api.example.com, environment: prod, auth: oidc-and-api-keys, rateLimit: { requestsPerSecond: 500 } }
  egress:
  - { host: api.openai.com, port: 443, justification: "eval baselines", expires: 2027-06-30 }   #!
  - { host: pypi.org, port: 443 }
  observability: { metricsSeriesLimit: 500000, logsGBPerDay: 200, traces: true }
status:
  conditions:
  - { type: Ready, status: "True" }
  components: { namespaces: Ready, rbac: Ready, quotas: Ready, gpuQueue: Ready, network: Ready,
                routes: Ready, egress: Ready, observability: Ready, dashboards: Ready }
```

**Going deeper.**

- **Keep the API small:** the tenant resource exposes what teams decide (owners, size, routes, destinations). Everything else is platform policy in the operator, so it can change without touching forty tenant files.
- **Validate before merge:** a CI check runs the operator's validation on the pull request (quota within the team's budget, hostnames in allowed domains, egress justification present), so reviewers see problems before approving.

### Identity and access

**Scenario.** Access was granted to individual user names. When people changed teams, their access stayed. A contractor who'd left three months earlier still had edit access to two production namespaces.

![Identity and access](diagrams/designs/concepts/ds27-c4.svg)

![Identity and access](diagrams/designs/ds27-3.svg)

**What the picture shows.** Access derived from identity groups, for people and for workloads.

- **People:** single sign-on through OIDC. Kubernetes RBAC binds the tenant's groups (owners, developers, viewers) to roles in the tenant's namespaces, never individual users.
- **Workloads:** each workload's service account maps to a cloud identity (workload identity federation), scoped to the tenant's buckets, registries and secrets. No long-lived keys in pods.
- **Production access:** read access by default. Write access in production through deployment pipelines, plus time-limited elevation with a reason, logged and reviewed.
- **Break-glass:** an emergency role with alerting and automatic expiry, used in incidents.
- **Reviews:** group memberships come from the identity provider, so leaving a team removes access everywhere. Quarterly reports list each tenant's access for owners to confirm.

**The YAML.** Group-based access for a tenant's production namespace (generated by the operator).

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: tenant-developers, namespace: search-ranking-prod }
subjects:
- { kind: Group, name: "oidc:team-search-ranking", apiGroup: rbac.authorization.k8s.io }   # groups, never users #!
roleRef: { kind: ClusterRole, name: tenant-viewer, apiGroup: rbac.authorization.k8s.io }
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ranking-api
  namespace: search-ranking-prod
  annotations:
    iam.gke.io/gcp-service-account: ranking-api@tenants-prod.iam.gserviceaccount.com   # workload identity; provider-specific
```

**Going deeper.**

- **Elevation is a workflow, not a role:** a request with a reason, an approver for production, an expiry and an audit entry. Standing write access in production is the exception.
- **Workload identity per workload:** one identity per tenant is easy, but a compromise of one service then reaches all of the tenant's data. Map identities per workload where the data differs.

### Isolation inside the cluster

**Scenario.** A tenant deployed a pod with `hostNetwork: true` and a privileged container "to debug networking". It could see every other tenant's traffic on that node. Nothing had stopped it.

![In-cluster isolation](diagrams/designs/concepts/ds27-c5.svg)

![Isolation inside the cluster](diagrams/designs/ds27-4.svg)

**What the picture shows.** Layers that keep tenants apart on shared infrastructure.

- **Network:** default-deny per namespace, with allowances generated for the tenant's own namespaces, the shared gateways, DNS and platform services. Cross-tenant traffic only through published routes.
- **Pod Security:** the restricted standard enforced on tenant namespaces: no host namespaces, no privileged containers, non-root, a seccomp profile.
- **Admission policies:** platform rules as CEL policies: images only from approved registries, required labels (tenant, cost center), no hostPath volumes, GPU requests only through Kueue queues.
- **Nodes:** tier 2 and above use tainted node pools, and sandboxed runtimes for untrusted code ([Design 01](#design-sandboxing)).
- **Shared services:** platform services that tenants call (registries, model stores, observability) check tenant identity on every request, rather than trusting network location.

**The YAML.** An admission policy for tenant namespaces (ValidatingAdmissionPolicy).

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata: { name: tenant-workload-rules }
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
    - { apiGroups: [""], apiVersions: ["v1"], operations: ["CREATE", "UPDATE"], resources: ["pods"] }
  validations:
  - expression: "object.spec.containers.all(c, c.image.startsWith('registry.example.com/'))"
    message: "images must come from registry.example.com"
  - expression: "!has(object.spec.volumes) || object.spec.volumes.all(v, !has(v.hostPath))"
    message: "hostPath volumes are not allowed in tenant namespaces"
  - expression: "has(object.metadata.labels) && 'platform.example.com/tenant' in object.metadata.labels"
    message: "pods must carry the tenant label"                                      #!
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata: { name: tenant-workload-rules }
spec:
  policyName: tenant-workload-rules
  validationActions: [Deny]
  matchResources:
    namespaceSelector: { matchExpressions: [{ key: platform.example.com/tenant, operator: Exists }] }
```

**Going deeper.**

- **Roll out policies in audit mode first:** new rules start with the `Audit` action, and the operator reports which tenants would be affected before enforcement.
- **Isolation is tested, not assumed:** a weekly job tries forbidden actions from a test tenant (cross-tenant connections, privileged pods, unapproved images) and alerts if any succeed.

### Ingress: routing from the outside world

**Scenario.** Each team that needed a public endpoint got its own load balancer, its own certificate process and its own authentication. Thirty-one load balancers later, two had expired certificates, and one exposed an internal admin API with no authentication.

![Ingress](diagrams/designs/concepts/ds27-c6.svg)

![Ingress: routing from the outside world](diagrams/designs/ds27-5.svg)

**What the picture shows.** Shared gateways where the platform owns the edge and tenants own their routes.

- **Gateways owned by the platform:** one external and one internal Gateway per cluster, with TLS certificates issued automatically, WAF and DDoS protection at the edge.
- **Routes owned by tenants:** tenants attach HTTPRoutes (or GRPCRoutes) for their allowed hostnames. The Gateway accepts routes only from namespaces labeled for it.
- **Hostname ownership:** the operator grants each tenant its hostnames. A route for a hostname the tenant doesn't own is rejected.
- **Authentication by default:** external routes require authentication (OIDC for people, API keys or mTLS for services) unless an exception is reviewed.
- **Per-tenant limits and logs:** rate limits by route and client, and access logs tagged with the tenant.

**The YAML.** A shared external Gateway that accepts routes from tenant namespaces, and a tenant's route (Gateway API).

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: external
  namespace: platform-gateways
  annotations: { cert-manager.io/cluster-issuer: letsencrypt-prod }
spec:
  gatewayClassName: envoy-external
  listeners:
  - name: https
    protocol: HTTPS
    port: 443
    hostname: "*.api.example.com"
    tls: { mode: Terminate, certificateRefs: [{ name: wildcard-api-example-com }] }
    allowedRoutes:
      namespaces:
        from: Selector
        selector: { matchLabels: { platform.example.com/external-routes: "allowed" } }   # set by the operator #!
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: ranking-api, namespace: search-ranking-prod }
spec:
  parentRefs: [{ name: external, namespace: platform-gateways }]
  hostnames: ["ranking.api.example.com"]
  rules:
  - matches: [{ path: { type: PathPrefix, value: /v1 } }]
    backendRefs: [{ name: ranking-api, port: 8080 }]
```

**Going deeper.**

- **Inference traffic needs more than HTTP routing:** model endpoints use the inference extension's InferencePool and endpoint picker ([Design 21](#design-llm-serving)) behind the same Gateway, with the same tenant rules.
- **Hostname checks belong in admission:** Gateway API itself doesn't know which tenant owns `ranking.api.example.com`. A small admission policy compares the route's hostnames with the tenant's grant.

### Egress: controlled paths to external services

**Scenario.** Teams reached external model APIs, package registries and data vendors directly from pods. When an API key leaked in a log, nobody could tell which tenants had called that provider, or from which workloads.

![Egress](diagrams/designs/concepts/ds27-c7.svg)

![Egress: controlled paths to external services](diagrams/designs/ds27-6.svg)

**What the picture shows.** Outbound traffic allowed only to declared destinations, through paths that identify the tenant.

- **Default deny to the internet:** tenant pods can't open connections outside the cluster except to destinations in their tenant's egress list.
- **DNS-aware policies:** allow-lists by hostname (not IP ranges that change), enforced by the CNI for each tenant's namespaces.
- **Egress gateway for sensitive destinations:** calls to paid or sensitive services (model APIs, data vendors) go through an egress gateway that injects credentials, so tenants never hold the provider keys, and that logs each request with tenant, workload and cost.
- **Expiry and justification:** each egress entry has a reason and an expiry date. Expired entries are removed after a reminder.
- **Audit:** "which tenants called this provider last month, and how much" is a query over gateway logs.

**The YAML.** A hostname-based egress allow-list for a tenant's namespace (CiliumNetworkPolicy, generated from the tenant's egress list).

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata: { name: tenant-egress, namespace: search-ranking-prod }
spec:
  endpointSelector: {}
  egress:
  - toEndpoints:
    - matchLabels: { k8s:io.kubernetes.pod.namespace: kube-system, k8s-app: kube-dns }
    toPorts:
    - ports: [{ port: "53", protocol: ANY }]
      rules: { dns: [{ matchPattern: "*" }] }               # lets Cilium learn the names pods resolve
  - toFQDNs: [{ matchName: pypi.org }, { matchName: files.pythonhosted.org }]
    toPorts: [{ ports: [{ port: "443", protocol: TCP }] }]
  - toEndpoints:
    - matchLabels: { k8s:io.kubernetes.pod.namespace: platform-egress, app: egress-gateway }   # model APIs via the gateway #!
    toPorts: [{ ports: [{ port: "8443", protocol: TCP }] }]
```

**Going deeper.**

- **Credentials at the gateway, not in pods:** the egress gateway holds provider keys per tenant and adds them to requests. Rotating a key is one change, and a leaked pod environment holds nothing valuable.
- **Budgets for external spend:** the gateway counts tokens and requests per tenant for paid APIs and enforces budgets, alongside the tenant's GPU costs.

### Quotas and fairness across tenants

**Scenario.** One tenant's batch job created 40,000 pods and 9,000 ConfigMaps. The API server slowed for everyone, and the cluster's etcd hit its storage limit. The tenant had a CPU quota, but no limits on object counts or API request rates.

![Quotas](diagrams/designs/concepts/ds27-c8.svg)

![Quotas and fairness across tenants](diagrams/designs/ds27-7.svg)

**What the picture shows.** Limits on every shared resource, not just CPU and GPUs.

- **Compute and storage:** ResourceQuotas for CPU, memory, storage and object counts (pods, services, config maps, secrets), and LimitRanges for defaults.
- **GPUs:** Kueue ClusterQueues and LocalQueues per tenant, with cohorts and fair sharing ([Design 17](#design-gpu-scheduling)).
- **Control plane:** API Priority and Fairness gives each tenant's requests a fair share of the API server, so one tenant's controllers can't starve the others.
- **Ingress:** request-rate limits per route and per client at the gateway.
- **Telemetry:** series, log volume and trace limits per tenant in the observability backends.
- **Visible usage:** each tenant's status and dashboard show usage against every quota, with warnings at 80%.

**The YAML.** Object-count quotas and per-route rate limiting for a tenant (ResourceQuota; Envoy Gateway BackendTrafficPolicy).

```yaml
apiVersion: v1
kind: ResourceQuota
metadata: { name: tenant-objects, namespace: search-ranking-prod }
spec:
  hard:
    requests.cpu: "300"
    requests.memory: 1200Gi
    requests.storage: 20Ti
    count/pods: "2000"                                     # object counts protect the control plane #!
    count/configmaps: "500"
    count/secrets: "300"
    count/services: "100"
---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata: { name: ranking-api-limits, namespace: search-ranking-prod }
spec:
  targetRefs: [{ group: gateway.networking.k8s.io, kind: HTTPRoute, name: ranking-api }]
  rateLimit:
    type: Local
    local:
      rules:
      - limit: { requests: 500, unit: Second }
```

**Going deeper.**

- **Quotas are a conversation, not a wall:** an increase is a pull request against the tenant file, reviewed against capacity and the team's budget, usually the same day.
- **Count objects, not just resources:** etcd and the API server are shared by everyone. Object counts and API fairness are what keep one tenant's bug from becoming everyone's outage.

### Observability per tenant, on day one

**Scenario.** New teams waited weeks for dashboards. Meanwhile, they queried the shared Prometheus directly. One team's dashboard ran a query over every tenant's series every 10 seconds, and another team could read a third team's request logs, including customer identifiers.

![Tenant observability](diagrams/designs/concepts/ds27-c9.svg)

![Observability per tenant, on day one](diagrams/designs/ds27-8.svg)

**What the picture shows.** Telemetry separated by tenant from collection to query.

- **Tenant ID on everything:** collectors add the tenant ID to metrics, logs and traces from the namespace. Multi-tenant backends (Mimir, Loki, Tempo) store and query them by tenant header.
- **Per-tenant limits:** series, ingestion rate, log volume and query cost limits, from the tenant resource.
- **Grafana access by team:** each tenant gets a folder and data sources scoped to its tenant ID, with permissions from its identity groups.
- **Defaults on creation:** standard dashboards (workloads, routes, GPU use, cost) and SLO templates for each route, provisioned by the operator.
- **Platform view:** the platform team can query across tenants for operations. Tenants can't.

**The code.** Provisioning a tenant's Grafana folder, team and permissions (Python, Grafana HTTP API).

```python
import requests

def provision_grafana(base: str, token: str, tenant: str, group: str) -> dict:
    s = requests.Session(); s.headers["Authorization"] = f"Bearer {token}"
    folder = s.post(f"{base}/api/folders", json={"uid": f"tenant-{tenant}", "title": f"Tenant · {tenant}"}).json()
    team = s.post(f"{base}/api/teams", json={"name": f"tenant-{tenant}"}).json()
    team_id = team.get("teamId")
    # team sync (Grafana Enterprise / Cloud) maps the OIDC group to this team
    s.post(f"{base}/api/teams/{team_id}/groups", json={"groupId": group})
    s.post(f"{base}/api/folders/tenant-{tenant}/permissions",
           json={"items": [{"teamId": team_id, "permission": 2}]})              # 2 = edit, this folder only #!
    return {"folder": folder.get("uid"), "team_id": team_id}
```

**Going deeper.**

- **Limit query cost, not only ingestion:** a tenant's expensive dashboard affects shared query capacity. Per-tenant query limits and recording-rule suggestions keep it fair.
- **Logs carry data:** request logs can hold customer data. Retention, redaction and access rules per tenant follow the data's classification, not one default.

### Owning platform services: from architecture review to deprecation

**Scenario.** The platform grew to 26 internal services: the tenant operator, gateways, egress gateway, registries, observability backends, cost pipeline. Five had no clear owner, three had no runbook, and the egress gateway went to production after a demo, without load testing. It failed at its first traffic spike.

![Service lifecycle](diagrams/designs/concepts/ds27-c10.svg)

![Owning platform services: from architecture review to deprecation](diagrams/designs/ds27-9.svg)

**What the picture shows.** The lifecycle every platform service goes through, with a gate at each stage.

- **Architecture review:** a design document with the problem, alternatives, tenancy and isolation model, failure modes, capacity and cost, reviewed by platform architects and security.
- **Build:** the service in the catalog with an owner, the API and its versioning policy, tests including multi-tenant isolation tests.
- **Production readiness review:** SLOs and alerts, runbooks, on-call rotation, load test results, backup and restore tested, dependencies and their failure behavior, security review, and a rollback plan.
- **Launch:** staged to internal tenants first, then all tenants, with a changelog entry and documentation.
- **Operate:** SLO reviews, incident reviews, capacity planning and quarterly ownership checks.
- **Deprecate:** a notice with a timeline, a migration path, usage tracking until zero, then removal.

**The YAML.** A platform service's catalog entry (Backstage).

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: egress-gateway
  description: Credentialed, logged egress to external APIs for tenants
  annotations:
    pagerduty.com/service-id: P3EGRSS
    platform.example.com/slo: "99.9% of requests proxied within 50 ms added latency"
    platform.example.com/prr: approved-2026-10-14                                   # production readiness review #!
    platform.example.com/design-doc: https://docs.example.com/platform/egress-gateway
  links: [{ url: https://runbooks.example.com/egress-gateway, title: Runbook }]
spec:
  type: service
  lifecycle: production
  owner: group:platform-networking
  system: tenant-platform
  providesApis: [egress-proxy-v1]
  dependsOn: [component:tenant-operator, resource:secrets-manager]
```

**Going deeper.**

- **The readiness review is a checklist with evidence:** each item links to the dashboard, runbook, load test or drill that proves it. "We'll add alerts later" doesn't pass.
- **Every service has a human owner:** a quarterly job checks that each catalog entry's owner group exists and has members, and that its on-call rotation has people in it.

### Running the platform as a product

**Scenario.** The platform team measured itself by uptime of its components. Tenants measured it by how long it took to get things done. A survey found onboarding took a median of 9 days, even after the tenant operator existed, because quota reviews waited in a shared queue.

![Platform as a product](diagrams/designs/concepts/ds27-c11.svg)

![Running the platform as a product](diagrams/designs/ds27-10.svg)

**What the picture shows.** What the platform measures and how it changes safely.

- **Tenant-facing SLOs:** time from tenant request merged to ready, route and egress change lead time, gateway availability and added latency, quota request turnaround.
- **Tenant health:** per tenant, usage against quotas, failing components, policy violations, expiring egress entries and certificates.
- **Noisy-neighbor detection:** tenants whose API requests, query load or telemetry volume are far above their share, flagged with evidence.
- **Safe changes:** platform upgrades go first to canary tenants (internal teams who opt in), then in waves, with a changelog and a rollback plan.
- **Support model:** a single intake, documented response times, and office hours. Recurring questions become documentation or features.
- **Feedback:** a quarterly survey and usage data decide the roadmap.

**The code.** Measuring tenant onboarding against its SLO and finding where time goes (Python).

```python
import pandas as pd

def onboarding_slo(events: pd.DataFrame, slo_minutes: int = 30) -> dict:
    """events: one row per tenant request with pr_opened, pr_merged, ready_at (timestamps)."""
    e = events.assign(
        review_min=(events.pr_merged - events.pr_opened).dt.total_seconds() / 60,
        reconcile_min=(events.ready_at - events.pr_merged).dt.total_seconds() / 60)
    return {
        "reconcile_p50_min": round(e.reconcile_min.median(), 1),
        "reconcile_within_slo": round((e.reconcile_min <= slo_minutes).mean(), 3),     # what the operator controls #!
        "review_p50_hours": round(e.review_min.median() / 60, 1),                       # what people control
        "end_to_end_p50_days": round((e.review_min + e.reconcile_min).median() / 1440, 1),
    }
```

**Going deeper.**

- **Measure the whole path:** the operator reconciled tenants in minutes, but reviews took days. Delegating standard quota sizes to automated approval cut onboarding to under a day.
- **Canary tenants are a privilege:** teams that opt in get changes first and a direct channel to the platform team, which is why they volunteer.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds27-c12.svg)

- **Phase 1, the tenant API (weeks 1–6):** the Tenant resource, the operator for namespaces, RBAC, quotas, GPU queues and network policies, and migration of existing teams by importing their current state.
- **Phase 2, edges (weeks 4–10):** shared ingress gateways with hostname grants and authentication defaults, egress allow-lists, and the egress gateway for model APIs.
- **Phase 3, observability and cost (weeks 8–12):** tenant IDs in all telemetry, per-tenant limits, Grafana folders and default dashboards, and cost labels feeding statements.
- **Phase 4, ownership (weeks 10–16):** the service catalog, production readiness reviews for all 26 platform services, tenant-facing SLOs and canary tenants.

**Trade-offs we accepted.**

- **Shared clusters by default:** cheaper and simpler than a cluster per team, with stronger tiers available where risk requires them.
- **A platform-owned edge:** teams can't run their own load balancers or open direct internet egress. Exceptions are reviewed.
- **Reconciliation reverts manual changes,** including well-meant fixes. Changes go through the tenant file.
- **Readiness reviews slow launches** of platform services, in exchange for fewer failures in production.

**Open questions.**

- **Agents as tenants:** AI agents acting for teams need identities, quotas and egress rules of their own. Should an agent be a workload of its team's tenant or a tenant itself?
- **Cross-cluster tenants:** a tenant spanning clusters and providers ([Design 18](#design-multicloud-fleet)) needs one quota and one view. How far should the tenant API reach?
- **Self-service tiers:** can tier upgrades (dedicated nodes, virtual clusters) be fully automated with cost approval, without a platform review?
- **Measuring isolation:** what evidence, beyond passing tests, would satisfy an auditor that tenants are isolated?

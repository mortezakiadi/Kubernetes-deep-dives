---
id: post-agents-prod
title: "Running agents in production"
short: "Running agents in production"
---
Agents bring three problems that plain microservices don't: **who they act as**, **how they talk to each other**, and **what they remember**. The protocols are still young, but the operational patterns below hold regardless of framework.

### 1. MCP: one protocol for tools

The **Model Context Protocol** replaces one-off integrations between every agent and every tool (M×N) with a single protocol (M+N). Tools describe themselves with a name, a description and a JSON schema, and calls use JSON-RPC. On Kubernetes:

- Run each MCP server as a Deployment behind a Service, keep session state external, and scale with HPA.
- Use a sidecar only when the tool must share the agent's files.
- Group related tools into a single server to keep the number of services manageable. Watch the total tool count, because agents choose less well from very long lists.

MCP now sits under the Linux Foundation's Agentic AI Foundation, together with goose and AGENTS.md.

### 2. Whose identity reaches the backend?

| Pattern | How | Strength | Watch out for |
|---|---|---|---|
| Token passthrough | Agent forwards the user's OAuth token | Existing per-user RBAC and audit just work | Token expiry on long tasks, broad scopes, stolen tokens |
| ServiceAccount delegation | Pod ServiceAccount tokens; validate with `TokenReview`, authorize with `SubjectAccessReview` on app-specific "virtual" resources | No extra identity provider inside the cluster | Workload identity only, no user |
| Token exchange (RFC 8693) | Identity provider issues a token with the user in `sub` and the agent in `act` | User *and* agent visible; composite policies | Run and cache an exchange service |
| SPIFFE/SPIRE mTLS | SPIRE issues short-lived X.509 identities per pod via a node agent | No secrets to steal; auto-rotation | Significant setup and operations |

Production setups combine them: SPIFFE mTLS between workloads, the user's identity in a header or exchanged token, and a policy engine (OPA) that requires both. **MCP gateways** (Envoy AI Gateway, agentgateway, ContextForge and others) centralize authentication, authorization, rate limits and audit, at the cost of an extra hop that must be highly available.
→ See [Identity and model endpoints](#d13-6).

### 3. A2A: agents talking to agents

MCP connects an agent to *tools*. The **Agent-to-Agent (A2A)** protocol connects an agent to other *agents*, which can reason and take time. It has three building blocks:

- an **agent card** that advertises skills and formats, like an OpenAPI spec;
- a **task lifecycle** (submitted → working, possibly pausing at input-required → completed, failed or canceled) that outlives any single HTTP call;
- **streamed artifacts** for partial results.

Each agent is its own Deployment, with NetworkPolicies between them and circuit breakers in the mesh. Treat the agent card as a contract you check in CI, and alert on tasks stuck in the working state.

### 4. Memory and long-running work

- **In-memory state** is fine for development and lost on the first restart or scale-out.
- **Short-term memory** (the session) belongs in a key-value store such as Redis, run as a StatefulSet with a TTL per session, so any replica can serve the next turn.
- **Long-term memory** (preferences, audit trail, cross-session analytics) belongs in a database, written asynchronously.
- **Long-running agents** checkpoint after each major step to a PVC and run as Jobs with `restartPolicy: OnFailure`, so an evicted pod resumes where it left off. The checkpoints double as a debugging trail.

Start with plain Deployments, Services, RBAC and StatefulSets, and add meshes, gateways and operators only when a real limitation shows up.

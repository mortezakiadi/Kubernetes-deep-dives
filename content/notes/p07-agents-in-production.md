---
id: post-agents-prod
title: "Running agents in production"
short: "Running agents in production"
---
Agents bring three problems that plain microservices don't: **who they act as**, **how they talk to tools and to each other**, and **what they remember**. The protocols are still young, but the operational patterns below hold regardless of framework: MCP for tools, a deliberate choice of identity, gateways in front of tools, A2A between agents, and memory that survives restarts.

### MCP: one protocol for tools

**Scenario.** Four agent teams each wrote their own integration with the same six internal systems: 24 integrations, each with its own auth, error handling and bugs. When the orders API changed, five of them broke.

![MCP: one protocol for tools](diagrams/posts/p07-1.svg)

**What the picture shows.** Why a common tool protocol changes the maths.

- **Without a protocol:** every agent integrates with every tool, so M agents and N tools mean M×N integrations.
- **With MCP (Model Context Protocol):** each tool is wrapped once as an MCP server, and every agent speaks MCP, so it's M+N.
- **Tools describe themselves:** a name, a description and a JSON schema for the arguments. The agent's model reads the descriptions to decide which tool to call.
- **Calls use JSON-RPC** over a transport; for servers on Kubernetes that's streamable HTTP.
- **Governance:** MCP now sits under the Linux Foundation's Agentic AI Foundation, together with goose and AGENTS.md.

**The code and YAML.** An MCP server in Python that exposes two read-only tools, and how it runs on Kubernetes.

```python
# orders_mcp.py
from mcp.server.fastmcp import FastMCP
import httpx

mcp = FastMCP("orders", host="0.0.0.0", port=8000, stateless_http=True)   # no session state in the pod #!

@mcp.tool()
def get_order(order_id: str) -> dict:
    """Look up an order by ID: items, status, amount and customer ID."""
    return httpx.get(f"http://orders-api.shop.svc/orders/{order_id}", timeout=5).json()

@mcp.tool()
def get_refund_policy(country: str) -> str:
    """Return the refund policy text for a country code such as 'DE' or 'US'."""
    return httpx.get(f"http://policy-api.shop.svc/refunds/{country}", timeout=5).text

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-mcp
  namespace: mcp
spec:
  replicas: 2
  selector: { matchLabels: { app: orders-mcp } }
  template:
    metadata:
      labels: { app: orders-mcp, mcp.example.com/server: "true" }
    spec:
      serviceAccountName: orders-mcp          # its own identity toward the orders API
      containers:
      - name: server
        image: registry.example.com/mcp/orders:1.3
        ports: [ { containerPort: 8000, name: mcp } ]
        readinessProbe: { tcpSocket: { port: mcp } }
        resources: { requests: { cpu: 200m, memory: 256Mi } }
---
apiVersion: v1
kind: Service
metadata:
  name: orders-mcp
  namespace: mcp
spec:
  selector: { app: orders-mcp }
  ports: [ { port: 80, targetPort: mcp } ]     # agents connect to http://orders-mcp.mcp/mcp
```

**Going deeper.**

- **Run each MCP server as a Deployment behind a Service,** keep session state outside the pod (or run stateless, as above), and scale with an HPA like any API.
- **Use a sidecar only when the tool must share the agent's files** (a code-editing tool working on the agent's workspace, for example).
- **Group related tools into one server** to keep the number of services manageable, and watch the total tool count per agent, because models choose less well from very long lists.

### Whose identity reaches the backend?

**Scenario.** A support agent calls the orders API on behalf of a user. The orders API's audit log shows every call coming from "refund-assistant", so nobody can tell which user's request triggered a refund, and the agent can read every customer's orders, not just the ones the user is allowed to see.

![Whose identity reaches the backend?](diagrams/posts/p07-2.svg)

**What the picture shows.** Four ways to carry identity from a user, through an agent, to a backend.

- **Token passthrough:** the agent forwards the user's OAuth token. Existing per-user permissions and audit just work, but tokens expire during long tasks, scopes are usually too broad, and a stolen token works anywhere.
- **ServiceAccount delegation:** the agent uses its pod's ServiceAccount token; backends validate it with `TokenReview` and authorize with `SubjectAccessReview` on app-specific "virtual" resources. No extra identity provider, but only the workload is visible, not the user.
- **Token exchange (RFC 8693):** the identity provider issues a new token with the user in `sub` and the agent in `act`. Backends see *both* and can apply composite policies ("support users, through the refund agent, may read orders").
- **SPIFFE/SPIRE mTLS:** SPIRE issues short-lived X.509 identities per pod through a node agent. No secrets to steal, and automatic rotation, at the cost of significant setup and operations.

| Pattern | Strength | Watch out for |
|---|---|---|
| Token passthrough | Existing per-user RBAC and audit just work | Token expiry on long tasks, broad scopes, stolen tokens |
| ServiceAccount delegation | No extra identity provider inside the cluster | Workload identity only, no user |
| Token exchange (RFC 8693) | User *and* agent visible; composite policies | Run and cache an exchange service |
| SPIFFE/SPIRE mTLS | No secrets to steal; auto-rotation | Significant setup and operations |

**The code.** A token exchange: the agent trades the user's token, plus its own identity, for a narrow token for one backend.

```python
import httpx

def exchange_for_orders_api(user_token: str, agent_token: str) -> str:
    resp = httpx.post(
        "https://idp.example.com/oauth2/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": user_token,                  # who the request is for → sub #!
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": agent_token,                   # who is acting → act #!
            "actor_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "audience": "orders-api",                     # only valid for this backend
            "scope": "orders:read",                       # narrower than the user's token
        },
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]                    # short-lived; cache until expiry
```

**Going deeper.**

- **The exchanged token is narrower than the original:** one audience, fewer scopes, shorter lifetime. That's what limits the damage if an agent is tricked into misusing it.
- See [Identity and model endpoints](#d13-6) for workload identity and guardrails on the model side.

### Combining identities: workload, user and policy

**Scenario.** Security asks for one rule: "only the refund agent, acting for a user in the support group, may call `draft_refund`". No single identity pattern can express that; it needs the workload's identity, the user's identity and a policy that checks both.

![Combining identities: workload, user and policy](diagrams/posts/p07-3.svg)

**What the picture shows.** How production setups combine the patterns.

- **Workload identity through SPIFFE mTLS:** the connection itself proves that the caller is the `refund-assistant` workload; no secret in the pod.
- **User identity in a header or an exchanged token:** the request carries who it's for.
- **A policy engine (OPA) checks both:** the tool server, or the gateway in front of it, asks "may this workload, acting for this user, call this tool?".
- **Every decision is logged** with the workload, the user, the tool and the outcome.

**The YAML and policy.** SPIRE issues an identity per ServiceAccount; OPA requires both identities.

```yaml
apiVersion: spire.spiffe.io/v1alpha1
kind: ClusterSPIFFEID
metadata:
  name: agents
spec:
  spiffeIDTemplate: "spiffe://{{ .TrustDomain }}/ns/{{ .PodMeta.Namespace }}/sa/{{ .PodSpec.ServiceAccountName }}"   #!
  namespaceSelector:
    matchLabels: { workload-class: agents }      # every agent pod gets a short-lived identity
```

```text
package mcp.authz

default allow := false

allow if {
    input.workload == "spiffe://example.org/ns/agents/sa/refund-assistant"   # who is calling #!
    "support" in input.user.groups                                          # for whom #!
    input.tool in {"get_order", "get_refund_policy", "draft_refund"}
}
```

**Going deeper.**

- **Start with the simplest pattern that answers your audit question,** usually token exchange, and add SPIFFE when you have many workloads and want to remove static credentials.
- **Policies are code:** keep them in Git, test them with example inputs in CI, and roll them out like any other change.

### MCP gateways

**Scenario.** Twenty MCP servers each implement their own authentication, rate limits and logging, in three languages and with different bugs. The security team can't answer "which agent called which tool for whom yesterday?" without reading twenty log formats.

![MCP gateways](diagrams/posts/p07-4.svg)

**What the picture shows.** A gateway that every agent goes through to reach tools.

- **One entry point for tools:** agents connect to the gateway; the gateway connects to the MCP servers.
- **Authentication and authorization in one place:** the gateway validates the agent's and user's identity and applies tool-level policy before a call reaches the server.
- **Rate limits and quotas** per agent, per user and per tool.
- **Audit:** one log of every tool call, with the same fields for every server.
- **Tool filtering:** the gateway can expose only the tools a given agent is allowed to use, so the model never even sees the others.
- **Examples:** Envoy AI Gateway, agentgateway, ContextForge and others.

**The YAML.** Make the gateway the only way in: MCP servers accept traffic only from the gateway's pods.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: mcp-servers-gateway-only
  namespace: mcp
spec:
  podSelector:
    matchLabels: { mcp.example.com/server: "true" }   # every MCP server #!
  policyTypes: ["Ingress"]
  ingress:
  - from:
    - namespaceSelector:
        matchLabels: { kubernetes.io/metadata.name: mcp-gateway }
      podSelector:
        matchLabels: { app: mcp-gateway }             # agents can't bypass it #!
    ports: [ { port: 8000 } ]
```

**Going deeper.**

- **The gateway is an extra hop that must be highly available:** run several replicas across zones with a PodDisruptionBudget, because every agent's tool calls depend on it.
- **Start without one** if you have a handful of tool servers; add a gateway when the number of servers, agents or audit requirements makes per-server controls unmanageable.

### A2A: agents talking to agents

**Scenario.** A travel-booking agent needs a flight agent and a hotel agent, built by different teams, possibly in different frameworks. A booking can take minutes and may need the user's input halfway through. A tool call that returns in one HTTP response doesn't fit.

![A2A: agents talking to agents](diagrams/posts/p07-5.svg)

**What the picture shows.** MCP connects an agent to *tools*; the **Agent-to-Agent (A2A)** protocol connects an agent to other *agents*, which can reason and take time.

- **An agent card** advertises the agent's skills, input and output formats, endpoint and capabilities, like an OpenAPI spec. It's served at a well-known path, so other agents can discover it.
- **A task lifecycle** that outlives any single HTTP call: submitted → working, possibly pausing at input-required, then completed, failed or canceled.
- **Streamed artifacts** deliver partial results (an itinerary draft, then the confirmed booking) while the task is still running.
- **On Kubernetes:** each agent is its own Deployment, with NetworkPolicies between them and circuit breakers in the mesh, so one slow agent doesn't take down the others.

**The code.** An agent card for the flight agent (JSON).

```text
{
  "name": "flight-agent",
  "description": "Searches and books flights within the company travel policy.",
  "url": "https://flight-agent.agents.example.com/a2a",
  "version": "1.4.0",
  "capabilities": { "streaming": true, "pushNotifications": true },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["application/json"],
  "skills": [
    {
      "id": "search-flights",
      "name": "Search flights",
      "description": "Find flights between two airports on given dates, within policy.",
      "tags": ["travel", "flights"]
    },
    {
      "id": "book-flight",
      "name": "Book a flight",
      "description": "Book a selected flight. Asks for traveller confirmation before payment.",
      "tags": ["travel", "booking"]
    }
  ]
}
```

**Going deeper.**

- **Treat the agent card as a contract you check in CI:** a change to a skill's name or input format breaks the agents that call it, just like an API change.
- **Alert on tasks stuck in `working`:** a task that never finishes holds resources and leaves a user waiting. Track task duration and state counts as metrics.

### Memory and long-running work

**Scenario.** The refund agent works fine until the Deployment scales from one replica to three. Now users' second messages land on a replica that doesn't remember the first, and an agent that was halfway through a 20-minute investigation starts over when its pod is evicted.

![Memory and long-running work](diagrams/posts/p07-6.svg)

**What the picture shows.** Where each kind of agent memory belongs.

- **In-memory state** is fine for development and lost on the first restart or scale-out.
- **Short-term memory (the session)** belongs in a key-value store such as Redis, run as a StatefulSet, with a TTL per session, so any replica can serve the next turn.
- **Long-term memory** (preferences, audit trail, cross-session analytics) belongs in a database, written asynchronously so it doesn't slow the conversation.
- **Long-running agents** checkpoint after each major step to durable storage and run as Jobs with `restartPolicy: OnFailure`, so an evicted pod resumes where it left off. The checkpoints double as a debugging trail.

**The code and YAML.** Session memory in Redis, and a long-running agent as a Job that resumes from its checkpoint.

```python
import json
import redis

r = redis.Redis(host="redis.agents.svc", port=6379, decode_responses=True)
SESSION_TTL = 60 * 60 * 24                                 # sessions expire after a day

def load_history(session_id: str) -> list[dict]:
    return [json.loads(m) for m in r.lrange(f"session:{session_id}", 0, -1)]

def append_turn(session_id: str, role: str, content: str) -> None:
    key = f"session:{session_id}"
    r.rpush(key, json.dumps({"role": role, "content": content}))
    r.expire(key, SESSION_TTL)                             # any replica can serve the next turn #!
```

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: investigate-incident-4821
  namespace: agents
spec:
  backoffLimit: 5
  template:
    spec:
      restartPolicy: OnFailure                             # an evicted pod comes back... #!
      containers:
      - name: agent
        image: registry.example.com/agents/investigator:2.1
        args: ["--task-id", "4821", "--checkpoint-dir", "/state"]   # ...and resumes from here #!
        volumeMounts: [ { name: state, mountPath: /state } ]
      volumes:
      - name: state
        persistentVolumeClaim: { claimName: investigator-state }
```

**Going deeper.**

- **For agents that run for hours or days,** checkpoints on a volume aren't enough: you also need to avoid repeating side effects and to wait cheaply for people. [Design 04](#design-agent-state) covers a durable execution design for that.
- **Start with plain Deployments, Services, RBAC and StatefulSets,** and add meshes, gateways and operators only when a real limitation shows up.

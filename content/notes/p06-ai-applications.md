---
id: post-ai-apps
title: "RAG and agents on Kubernetes"
short: "RAG and agents on Kubernetes"
---
In a real product, the model is one service among many. The application owns the logic: it fetches context, calls tools, keeps state, and uses the LLM for generation and reasoning. This post maps those pieces onto Kubernetes.

### 1. Two application shapes

- **Chat apps:** gateway → orchestrator → LLM → state store, all within one synchronous request. Latency rules, so everything stays warm, and browser-based sign-in (OAuth2) is easy.
- **Backend AI services:** triggered by events (an order placed, a payment made) or by a schedule. They need idempotency, buffering and eventual consistency more than raw speed, and can scale to zero.

Around these sit nightly CronJobs, always-on agent loops (single-replica Deployments that watch a feed, like a controller with an LLM inside), and multi-step automations that run as Jobs.
→ See [Workloads and node tiers](#d03-1) and [Thundering herd](#d05-8).

### 2. RAG, end to end

**Ingestion** (offline, as Jobs or CronJobs):

1. parse documents (e.g. with Docling);
2. split them into chunks by sentence, paragraph or heading;
3. embed each chunk;
4. upsert the vectors with metadata (source, section, version) into a vector database.

**Query time:** embed the question with the *same* embedding model, run a similarity search (optionally hybrid with keyword ranking), optionally rerank, build the prompt from a template, generate, then add citations and run guardrails before returning.

| Component | Kubernetes shape |
|---|---|
| Vector DB (Milvus, Weaviate, Qdrant, pgvector) | StatefulSet + PVC, snapshots, NetworkPolicy |
| Embedding model | Deployment, sidecar, or in-process |
| Orchestrator | Deployment + Service/Ingress; prompts in ConfigMaps, keys in Secrets |
| Reranker | Optional Deployment for high-stakes queries |
| Ingestion | CronJob / Job with limits so it can't starve users |

If you change the embedding model, you must re-embed every document.
→ See [Workloads, node groups and scheduling](#d08-1).

### 3. Agents

An agent wraps the model in a loop: perceive → think → act (call a tool) → observe → reflect → remember. ReAct is the classic form.

- **Tools** run either on the client (the model asks your code to run a function) or on the server, increasingly through **MCP** servers.
- **Frameworks:** client-side libraries (LangChain, LangGraph, CrewAI) or server-side runtimes behind an API (the OpenAI-style Responses API, Llama Stack, vLLM's emerging support). Server-side runtimes handle state and human approval steps for you.
- **On Kubernetes:** operators like **Kagent** declare agents, tools and A2A skills as custom resources. Agents are Deployments, tools are Services, memory lives in StatefulSets, and heavy work goes to Jobs.
- **Multi-agent systems** use either a coordinator or peer-to-peer messaging, plus shared memory. Plan for failures (sagas) and require **human approval** for risky actions.

More in [Running agents in production](#post-agents-prod).

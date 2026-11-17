---
id: post-ai-apps
title: "RAG and agents on Kubernetes"
short: "RAG and agents on Kubernetes"
---
In a real product, the model is one service among many. The application owns the logic: it fetches context, calls tools, keeps state, and uses the LLM for generation and reasoning. This post maps those pieces onto Kubernetes: the two application shapes, a RAG pipeline from ingestion to answer, the Kubernetes objects behind it, and agents.

### Two application shapes

**Scenario.** A team builds a chat assistant and a nightly job that summarizes support tickets on the same stack: always-on Deployments behind a load balancer. The chat app is fine. The summarizer sits idle 23 hours a day on reserved GPUs, then overwhelms the model at midnight when 40,000 tickets arrive at once.

![Two application shapes](diagrams/posts/p06-1.svg)

**What the picture shows.** Two shapes of AI application, with different priorities.

- **Chat apps:** gateway → orchestrator → LLM → state store, all within one synchronous request. Latency rules, so everything stays warm, and browser-based sign-in (OAuth2) is easy.
- **Backend AI services:** triggered by events (an order placed, a payment made) or by a schedule. They need idempotency, buffering and eventual consistency more than raw speed, and can scale to zero between bursts.
- **A queue between events and the model** absorbs bursts: workers pull at the rate the model can serve, instead of the event source pushing as fast as it can.
- **Around these sit** nightly CronJobs, always-on agent loops (single-replica Deployments that watch a feed, like a controller with an LLM inside), and multi-step automations that run as Jobs.

**The YAML.** An event-driven worker that scales from zero on a Kafka topic's backlog with KEDA.

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: ticket-summarizer
  namespace: support-ai
spec:
  scaleTargetRef: { name: ticket-summarizer }     # a Deployment of workers
  minReplicaCount: 0                              # nothing runs when there's no work #!
  maxReplicaCount: 20                             # caps load on the shared model
  triggers:
  - type: kafka
    metadata:
      bootstrapServers: kafka.data.svc:9092
      consumerGroup: ticket-summarizer
      topic: tickets.created
      lagThreshold: "50"                          # ~50 waiting tickets per worker #!
```

**Going deeper.**

- **Cap the workers, not just the model:** `maxReplicaCount` on the workers is what protects the shared model from a burst. See [Thundering herd](#d05-8).
- **Make every step idempotent:** events get redelivered, so a worker must recognize a ticket it already summarized. See [Workloads and node tiers](#d03-1).

### RAG ingestion: from documents to vectors

**Scenario.** The assistant answers questions about internal policies, but its answers are three months out of date, because someone ran the indexing script once from a laptop. Ingestion needs to be a scheduled, repeatable pipeline with an owner.

![RAG ingestion: from documents to vectors](diagrams/posts/p06-2.svg)

**What the picture shows.** The offline half of RAG, run as Jobs or CronJobs.

- **Parse:** turn PDFs, HTML and office files into clean text with structure (headings, tables), for example with Docling.
- **Chunk:** split into pieces by sentence, paragraph or heading, small enough to be specific and large enough to make sense on their own, often with some overlap.
- **Embed:** turn each chunk into a vector with an embedding model, served like any other model.
- **Upsert:** write the vectors with metadata (source, section, version, access group) into a vector database. Upsert by a stable ID, so re-running the job updates instead of duplicating.
- **Only what changed:** the job processes documents modified since its last run, which keeps nightly runs small.

**The code and YAML.** The heart of the ingestion job, and the CronJob that runs it with limits so it can't starve user traffic.

```python
import hashlib
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

embedder = OpenAI(base_url="http://embedder.rag.svc:8000/v1", api_key="unused")
db = QdrantClient(url="http://qdrant.rag.svc:6333")

def chunks(text: str, size: int = 800, overlap: int = 100):
    for start in range(0, len(text), size - overlap):
        yield text[start:start + size]

def ingest(doc_id: str, text: str, meta: dict) -> None:
    parts = list(chunks(text))
    vectors = embedder.embeddings.create(model="bge-m3", input=parts).data
    db.upsert("policies", points=[
        PointStruct(
            id=int(hashlib.sha1(f"{doc_id}:{i}".encode()).hexdigest()[:15], 16),  # stable ID #!
            vector=v.embedding,
            payload={**meta, "doc_id": doc_id, "chunk": i, "text": part},
        )
        for i, (part, v) in enumerate(zip(parts, vectors))
    ])
```

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ingest-policies
  namespace: rag
spec:
  schedule: "0 2 * * *"
  concurrencyPolicy: Forbid                      # never two runs at once #!
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        spec:
          restartPolicy: OnFailure
          priorityClassName: batch-low           # users' requests come first #!
          containers:
          - name: ingest
            image: registry.example.com/rag/ingest:1.8
            args: ["--source", "s3://docs/policies/", "--since-last-run"]
            resources:
              requests: { cpu: "1", memory: 2Gi }
              limits:   { cpu: "2", memory: 4Gi }
```

**Going deeper.**

- **Changing the embedding model means re-embedding every document,** because vectors from different models can't be compared. Version the collection (`policies-bge-m3-v2`) and switch over when it's complete.
- **Store access groups in the metadata** and filter on them at query time, so users only retrieve chunks they're allowed to read.

### RAG at query time

**Scenario.** Users complain that the assistant confidently cites a policy that doesn't say what it claims. The retrieval found the right document, but the prompt didn't ask the model to stick to it, and nothing checked the answer before it went out.

![RAG at query time](diagrams/posts/p06-3.svg)

**What the picture shows.** The online half of RAG, inside one request.

- **Embed the question** with the *same* embedding model used at ingestion.
- **Search:** a similarity search for the closest chunks, optionally hybrid with keyword ranking (better for names, codes and exact terms), filtered by the user's access groups.
- **Rerank (optional):** a reranker model reorders the top 20–50 chunks by relevance, for high-stakes questions.
- **Build the prompt** from a versioned template: instructions, the selected chunks with their sources, the question.
- **Generate,** then **add citations and run output guardrails** before returning the answer.

**The code.** The query path, end to end.

```python
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny

llm = OpenAI(base_url="http://qwen-7b.models.svc:8000/v1", api_key="unused")
embedder = OpenAI(base_url="http://embedder.rag.svc:8000/v1", api_key="unused")
db = QdrantClient(url="http://qdrant.rag.svc:6333")

PROMPT = """Answer only from the sources below. If they don't contain the answer, say so.
Cite sources as [n].\n\n{sources}\n\nQuestion: {question}"""

def answer(question: str, user_groups: list[str]) -> str:
    qvec = embedder.embeddings.create(model="bge-m3", input=[question]).data[0].embedding  # same model #!
    hits = db.query_points(
        "policies", query=qvec, limit=6,
        query_filter=Filter(must=[FieldCondition(key="access_group", match=MatchAny(any=user_groups))]),  #!
    ).points
    sources = "\n".join(f"[{i+1}] ({h.payload['doc_id']}) {h.payload['text']}" for i, h in enumerate(hits))
    reply = llm.chat.completions.create(
        model="Qwen/Qwen2.5-7B-Instruct",
        messages=[{"role": "user", "content": PROMPT.format(sources=sources, question=question)}],
        temperature=0.1,
    )
    return reply.choices[0].message.content      # then: citations check, output guardrails
```

**Going deeper.**

- **Measure retrieval separately from generation.** If the right chunk isn't in the top results, no prompt will fix the answer. Ragas-style metrics (context relevance, faithfulness) separate the two; see [Running LLMs in production](#post-production).
- **Prompts are configuration:** keep the template in a ConfigMap or a prompt registry, versioned with the deployment, so a prompt change is reviewed and reversible.

### Mapping RAG onto Kubernetes

**Scenario.** The RAG prototype runs as one container with an embedded vector store. To go to production, the team needs to decide which piece is a Deployment, which is stateful, and how they're protected from each other.

![Mapping RAG onto Kubernetes](diagrams/posts/p06-4.svg)

**What the picture shows.** Each RAG component and the Kubernetes shape it takes.

| Component | Kubernetes shape |
|---|---|
| Vector DB (Milvus, Weaviate, Qdrant, pgvector) | StatefulSet + PVC, snapshots, NetworkPolicy |
| Embedding model | Deployment, sidecar, or in-process |
| Orchestrator | Deployment + Service/Ingress; prompts in ConfigMaps, keys in Secrets |
| Reranker | Optional Deployment for high-stakes queries |
| Ingestion | CronJob / Job with limits so it can't starve users |

**The YAML.** The vector database as a StatefulSet, reachable only from the orchestrator and the ingestion job.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: qdrant
  namespace: rag
spec:
  serviceName: qdrant
  replicas: 3
  selector: { matchLabels: { app: qdrant } }
  template:
    metadata:
      labels: { app: qdrant }
    spec:
      containers:
      - name: qdrant
        image: qdrant/qdrant:v1.15.0
        ports: [ { containerPort: 6333, name: http } ]
        resources:
          requests: { cpu: "2", memory: 8Gi }
        volumeMounts: [ { name: data, mountPath: /qdrant/storage } ]
  volumeClaimTemplates:                          # one volume per replica #!
  - metadata: { name: data }
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: gp3
      resources: { requests: { storage: 100Gi } }
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: qdrant-clients-only
  namespace: rag
spec:
  podSelector: { matchLabels: { app: qdrant } }
  policyTypes: ["Ingress"]
  ingress:
  - from:
    - podSelector: { matchLabels: { app: rag-orchestrator } }   # reads #!
    - podSelector: { matchLabels: { job: ingest-policies } }    # writes #!
    ports: [ { port: 6333 } ]
```

**Going deeper.**

- **The vector store holds your documents in plain text** (the chunk payloads). Protect it like the source systems: network policy, encryption at rest, backups and snapshots, and access groups in the metadata.
- See [Workloads, node groups and scheduling](#d08-1) for where stateful AI components should run.

### Agents

**Scenario.** The support team wants the assistant to do more than answer: look up the customer's order, check the refund policy, and draft the refund, asking a human before it sends money. That's no longer a single prompt and answer; it's a loop with tools.

![Agents](diagrams/posts/p06-5.svg)

**What the picture shows.** The agent loop, and where its pieces run on Kubernetes.

- **The loop:** perceive → think → act (call a tool) → observe the result → reflect → remember, repeated until the task is done. ReAct is the classic form.
- **Tools** run either on the client (the model asks your code to run a function) or on the server, increasingly through **MCP** servers. See [Running agents in production](#post-agents-prod).
- **Frameworks:** client-side libraries (LangChain, LangGraph, CrewAI) or server-side runtimes behind an API (OpenAI-style Responses API, Llama Stack, vLLM's emerging support). Server-side runtimes handle state and human approval steps for you.
- **On Kubernetes:** agents are Deployments, tools are Services, memory lives in StatefulSets, and heavy work goes to Jobs. Operators like **Kagent** declare agents, tools and A2A skills as custom resources.
- **Human approval for risky actions:** the loop pauses before tools that send money, delete data or message customers, and continues only when a person approves.

**The YAML.** A Kagent agent declared as a custom resource, with its model and an MCP tool server.

```yaml
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: in-cluster-qwen
  namespace: kagent
spec:
  provider: OpenAI                                # any OpenAI-compatible endpoint
  model: Qwen/Qwen2.5-7B-Instruct
  apiKeySecret: llm-api-key
  apiKeySecretKey: key
  openAI:
    baseUrl: http://qwen-7b.models.svc:8000/v1
---
apiVersion: kagent.dev/v1alpha2
kind: Agent
metadata:
  name: refund-assistant
  namespace: kagent
spec:
  description: Looks up orders and drafts refunds within policy.
  type: Declarative
  declarative:
    modelConfig: in-cluster-qwen
    systemMessage: |
      You help support agents with refunds. Look up the order, check the refund policy,
      and draft the refund. Never issue a refund yourself; a person approves it.
    tools:
    - type: McpServer
      mcpServer:
        apiGroup: kagent.dev
        kind: RemoteMCPServer
        name: orders-mcp                          # read-only order lookup #!
        toolNames: ["get_order", "get_refund_policy", "draft_refund"]
```

**Going deeper.**

- **Multi-agent systems** use either a coordinator agent that delegates to specialists, or peer-to-peer messaging, plus shared memory. Plan for failures between steps (sagas: each step has an undo) and require **human approval** for risky actions.
- **Give agents the fewest tools that do the job.** A short tool list is both safer (less for a prompt injection to reach) and more accurate (models choose worse from long lists).
- **Untrusted code from agents** belongs in a sandbox; see [Design 01](#design-sandboxing). Long-running agents need durable state; see [Design 04](#design-agent-state).

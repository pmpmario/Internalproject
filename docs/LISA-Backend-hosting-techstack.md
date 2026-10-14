# LISA-Backend — Hosting Tech Stack (Local VMs / AWS / Azure)

Where each part of the `LISA-Backend-playbooks-v2` stack would run in each of three hosting
targets. The rows come from the project's `TECHSTACK.md`, `pyproject.toml`, `.env.example` and source
code. The services in each column are a suggested setup. They have not been sized or load-tested.

**What runs where today (from the code)**

- **Backend:** a single FastAPI process on port 8000. It runs the LangGraph agent, the CI/priority
  models and the Qwen reranker in-process.
- **Supporting services:** Ollama for embeddings (always required) and optionally for chat, Qdrant
  (optional), the tools server on port 8100, ServiceNow, Teams, and the React dashboard.
- **State:** two SQLite files, `data/dashboard.sqlite` and `data/checkpoints.sqlite`, plus the
  pre-built model and index files under `src/resolution_engine/artifacts/`.

---

## 1. Compute and application tier

| Component | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Base OS / runtime** (Python 3.12–3.13, `uv`) | Ubuntu 24.04 LTS VMs on VMware / Hyper-V / Proxmox; Python 3.12 + `uv sync` or a Docker image (`python:3.12-slim`) | Same Docker image on Amazon Linux 2023 / Ubuntu | Same Docker image on Ubuntu VM / container platform |
| **Container image registry** | Harbor or a private Docker Registry on a VM | Amazon ECR | Azure Container Registry (ACR) |
| **Backend API** (FastAPI + uvicorn, port 8000) | App VM: uvicorn under `systemd` or Docker Compose, behind Nginx | EC2 (or ECS on EC2 with an EBS volume) behind an Application Load Balancer | Azure VM (or Azure Container Apps once state is off SQLite) behind Application Gateway |
| **Agent graph** (LangGraph, LangChain, langgraph-checkpoint-sqlite) | In-process in the backend — no separate host | In-process in the backend container | In-process in the backend container |
| **Reranker and ML classifiers** | See [section 3](#3-ml-model-hosting--reranker-and-classifiers) | See section 3 | See section 3 |
| **Tools server** (`LISA-tools-mcp`, port 8100) | Same or separate VM, `systemd` / Docker, internal network only | ECS service in a private subnet (Service Connect / internal ALB) | Container Apps with internal-only ingress |
| **Suggested sizing** | App VM: 8 vCPU / 32 GB RAM / 200 GB SSD; GPU VM (only for local chat LLM): 1× 24 GB GPU (L4 / A10 / RTX 4090), 64 GB RAM | App: `m7i.2xlarge` (8 vCPU / 32 GB); GPU (optional): `g6.xlarge` (1× L4) | App: `Standard_D8s_v5` (8 vCPU / 32 GB); GPU (optional): `Standard_NC4as_T4_v3` or `NV36ads_A10_v5` |

## 2. AI models

| Component | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Chat LLM** (`LLM_PROVIDER`) | `ollama` — Ollama on the GPU VM (e.g. `qwen3.5:9b`); free per call, no data leaves the network | `ollama` on a GPU EC2 (`g6.xlarge`), **or** `claude_agent_sdk` (Anthropic API key), **or** `azure_llm` calling Azure OpenAI across clouds* | `azure_llm` — **Azure OpenAI `gpt-4o`** (already the configured default in `.env.example`) |
| **Agent mode** (`WORKFLOW_MODE=agent`) | Not available — agent mode requires `azure_llm`; use `graph` mode with Ollama | Requires `azure_llm` (cross-cloud to Azure OpenAI)* | Native — Azure OpenAI |
| **Embeddings** (Ollama, `qwen3-embedding:0.6b`, always required) | Ollama on the app VM (CPU is enough for 0.6B) or the GPU VM | Ollama container on EC2 / ECS sidecar | Ollama container on the Azure VM / Container Apps |
| **Model / index artifacts** (`src/resolution_engine/artifacts/`) | VM disk or NFS share | S3 bucket → synced to EBS on start (or baked into image) | Blob Storage / Azure Files → synced on start (or baked into image) |

\* The code currently wires only three providers: `claude_agent_sdk`, `azure_llm` and `ollama`
(`workflows/common.py`). `LLM_PROVIDER=anthropic` appears in `.env.example` but has no code path.
Using **Amazon Bedrock** on AWS would mean adding a `ChatBedrock` branch (package `langchain-aws`).

> **Keep the embedding model as is.** The pre-built Qdrant / BM25 indexes in `artifacts/` were
> built with `qwen3-embedding:0.6b`. Swapping to Bedrock Titan or Azure OpenAI embeddings means
> rebuilding every index, so run Ollama in all three environments.

## 3. ML model hosting — reranker and classifiers

### 3.1 The models (from `src/resolution_engine/`)

| Model | What it is | Artifact | Runtime | Use per incident |
|---|---|---|---|---|
| **Reranker** — Qwen3-Reranker-0.6B | 0.6B-parameter causal LM. Each (query, document) pair is scored from the "yes"/"no" token logits. Weights are about 1.2 GB on disk and a few GB of RAM once loaded | Hugging Face folder at `QWEN_RERANKER_MODEL_PATH` (default `scripts/reranker/models/qwen3-reranker-0.6b`) | `torch` + `transformers`, loaded once per process (`_shared_qwen_reranker_cache.py`) | 3 calls, up to 25 pairs: CI (10 candidates), priority (10), KB (5). Calls run one at a time behind a lock |
| **CI classifier** | Word + character TF-IDF feeding a calibrated SVM | `artifacts/ci_predictor/configuration_item_classifier.joblib` | scikit-learn 1.9.1, CPU | 1 `predict(text)` — milliseconds |
| **Priority classifier** | Two-stage LinearSVC: an urgency gate, then a split model on each side. Uses the CI as a feature | `artifacts/priority_predictor/priority_classifier.joblib` | scikit-learn 1.9.1, CPU | 1 `predict(text, ci)` — milliseconds |
| **BM25 keyword indexes** | `rank-bm25` stores (CI, priority, KB) | `artifacts/{ci_predictor,priority_predictor,kb_retrieval}/keyword_search_store.joblib` | In memory, CPU | 3 lookups |
| **Vector indexes** | Qdrant collections built with `qwen3-embedding:0.6b` | `artifacts/*/vector_store` (embedded) or a Qdrant server | Qdrant | 3 searches |
| **Embedding model** | `qwen3-embedding:0.6b` | Pulled into Ollama | Ollama | 1 per query |

**Constraints that come from the code**

- **The reranker always runs on CPU.** The model is never moved to a GPU (`.to("cuda")` /
  `device_map` appear nowhere). A GPU host gives no speed-up until that one-line change is made.
- **The `.joblib` files are Python pickles.** Whatever loads them needs the exact pinned
  `scikit-learn==1.9.1` and `numpy==2.5.3` plus this repo's `resolution_engine` package, because
  `_artifact_compat.py` maps the old training-project module names onto it. The managed
  scikit-learn images from SageMaker or Azure ML won't load them as-is, so use a **custom
  container** built from this repo.
- **Training happens elsewhere.** The models come from the separate ServiceNow CI Predictor,
  Priority Predictor and KB Retrieval projects. This repo only loads them.

### 3.2 Option A — models inside the backend (how the code works today, no code change)

| Model | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Reranker** | In the backend process on the app VM, 8 vCPU / 32 GB, CPU inference. Weights on VM disk | In the backend on EC2 `m7i.2xlarge` or `c7i.2xlarge`. Weights from S3 copied to EBS at start, or baked into the image | In the backend on `Standard_D8s_v5` or `F8s_v2`. Weights from Blob Storage / Azure Files, or baked into the image |
| **Reranker on GPU** (needs the `.to("cuda")` change) | Run the backend on the GPU VM | `g6.xlarge` (L4) or `g5.xlarge` (A10G) | `Standard_NC4as_T4_v3` or `NV36ads_A10_v5` |
| **CI + priority classifiers** | In the backend process — loaded from VM disk | In the backend — loaded from EBS / image | In the backend — loaded from disk / image |
| **BM25 indexes** | In the backend process | In the backend | In the backend |

### 3.3 Option B — separate model-serving endpoints (needs a code change)

| Item | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Reranker endpoint** | A dedicated container on a GPU or CPU VM: a small FastAPI wrapper around the existing `_shared_qwen_reranker_cache.py` scoring (same scores as today). Alternatives: vLLM `/score` or Hugging Face TEI with a seq-cls conversion of the weights — check that scores match before relying on the 0.7 thresholds | **SageMaker real-time endpoint** with a custom container: `ml.g6.xlarge` / `ml.g5.xlarge` (GPU) or `ml.c7i.2xlarge` (CPU). Or an ECS service on GPU EC2 | **Azure ML managed online endpoint** with a custom container on `Standard_NC4as_T4_v3` (GPU) or `F8s_v2` (CPU). Or **Container Apps** with serverless GPU (T4 / A100) |
| **Classifier endpoint** (CI + priority, one service) | One "model API" container (FastAPI) loading both `.joblib` files, or `mlflow models serve` | **SageMaker Serverless Inference** (CPU, scales to zero; a good fit for small sklearn models) or a real-time endpoint on `ml.m7i.large`, custom container | **Azure ML managed online endpoint** on `Standard_F4s_v2`, custom environment pinned to scikit-learn 1.9.1 |
| **Model registry / versioning** | MLflow Tracking + Model Registry on a VM, with MinIO for the files | SageMaker Model Registry + versioned S3 bucket | Azure ML model registry (workspace or shared registry) + Blob Storage |
| **Retraining** (CI / priority / KB projects) | Scheduled job on a VM (cron or Airflow) that exports from ServiceNow, retrains and registers in MLflow | SageMaker Training jobs / SageMaker Pipelines, triggered on a schedule by EventBridge | Azure ML pipelines with a schedule |
| **Rollout** | Blue/green: two containers, switch in Nginx | Endpoint production variants (traffic split / shadow testing) | Online endpoint deployments with traffic split (blue/green) |
| **Model monitoring** | Prometheus metrics (latency, score distribution, CI/priority mix) + Grafana | SageMaker Model Monitor + CloudWatch | Azure ML model monitoring + Application Insights |
| **Network** | Internal VLAN only | VPC-only endpoint (PrivateLink); the backend calls it from a private subnet | Private endpoint into the backend's VNet |

**Code change for Option B.** Each of the three services builds its own `QwenRerankingClient(settings)`:
`kb_retrieval_service.py:54`, `configuration_item_prediction_service.py:74` and
`priority_prediction_service.py:79`. The class already describes itself as a drop-in behind
`score_candidates()` / `pop_last_usage()`. A `RemoteRerankingClient` with the same two methods,
calling the endpoint over HTTP, could replace it at those three lines. The classifiers likewise
need an HTTP client with the same `predict(...)` signature, returning `ClassifierPrediction` /
`PriorityPrediction`.

### 3.4 Managed reranker instead of self-hosting (a different model)

| Local (Virtual Machines) | AWS | Azure |
|---|---|---|
| No managed option; self-host as in 3.2 / 3.3 | **Amazon Bedrock Rerank** (Cohere Rerank 3.5 or Amazon Rerank 1.0) | **Cohere Rerank** via Azure AI Foundry, or Azure OpenAI as a listwise reranker. The latter is already prototyped in `scripts/compare_azure_rerank.py` with `gpt-4o` |

A different reranker produces different scores. The 0.7 relevance floors
(`similar_incidents_relevance_threshold`, `minimum_relevance_score`) and the confidence
thresholds would need re-tuning, for example with `scripts/compare_azure_rerank.py`.

### 3.5 Recommendation

- **Classifiers and BM25:** keep them in the backend (Option A) in every environment. They are
  small, CPU-only and respond in milliseconds, and the pickles are tied to this code. A separate
  endpoint adds a network hop and a second copy of the pinned dependencies for little gain.
- **Reranker:** start in-process on CPU. Move it to its own endpoint once volume grows, because
  the inference lock means one backend process reranks one request at a time. At that point make
  the GPU change: GPU VM locally, SageMaker or ECS on GPU EC2 on AWS, Azure ML online endpoint or
  Container Apps GPU on Azure.

## 4. Data and state

| Component | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Vector DB** (Qdrant, `QDRANT_URL`) | Qdrant Docker container on the VM with a persistent volume | Qdrant on EC2 / ECS with EBS, or Qdrant Cloud (AWS region) | Qdrant on Azure VM / AKS with Managed Disk, or Qdrant Cloud (Azure region) |
| **Dashboard DB** (`data/dashboard.sqlite`) | SQLite on VM disk + nightly backup | Now: SQLite on EBS, single instance, AWS Backup snapshots. Later: **RDS PostgreSQL** | Now: SQLite on Managed Disk, single instance, Azure Backup. Later: **Azure Database for PostgreSQL – Flexible Server** |
| **LangGraph checkpoints** (`data/checkpoints.sqlite`) | SQLite on VM disk + backup | Now: SQLite on EBS. Later: RDS PostgreSQL via `langgraph-checkpoint-postgres` | Now: SQLite on Managed Disk. Later: Azure PostgreSQL via `langgraph-checkpoint-postgres` |
| **LLM response cache** (`LLM_CACHE_BACKEND`) | `memory` or `sqlite` on the VM | `memory` (or ElastiCache Redis after a code change) | `memory` (or Azure Cache for Redis after a code change) |
| **Backups** | Veeam / VM snapshots / `rsync` to a backup server | AWS Backup (EBS + S3) | Azure Backup (VM + disks + Blob) |

> **SQLite pins you to one instance.** Both databases and the embedded Qdrant store need a
> single process on a local disk. Don't put them on EFS or Azure Files and don't run more than one
> replica. Moving both to PostgreSQL and Qdrant to a server is what allows horizontal scaling
> (ECS Fargate / Container Apps with several replicas).

## 5. Front-ends and channels

| Component | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Dashboard** (React + Vite, `../LISA-Frontend`) | `npm run build`, static files served by Nginx on the app VM | S3 + CloudFront | Azure Static Web Apps (or Blob static website + Front Door) |
| **Microsoft Teams bot** (`teams_bot/`) | VM service + public HTTPS endpoint (DMZ reverse proxy or Dev Tunnels); still needs an **Azure Bot Service** registration | ECS / EC2 behind a public ALB; still needs an **Azure Bot Service** registration + Entra ID app | App Service / Container Apps + **Azure Bot Service** (native) |
| **Chat UI / Voice demo** (`../LISA-Chat`, `../LISA-voice`) | Same VM, Nginx / `systemd` | Chat: S3 + CloudFront; Voice: ECS service | Chat: Static Web Apps; Voice: Container Apps |
| **ServiceNow** (Table API, external SaaS) | Outbound HTTPS through the corporate firewall / proxy | Outbound via NAT Gateway with an Elastic IP (for ServiceNow IP allow-listing) | Outbound via NAT Gateway with a static public IP |

## 6. Platform: networking, security, operations

| Component | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Ingress / TLS** | Nginx or Traefik + internal CA certificates | ALB + ACM certificates + AWS WAF | Application Gateway (WAF v2) or Front Door + managed certificates |
| **Network isolation** | VLANs / firewall rules; Ollama, Qdrant, tools server internal only | VPC, private subnets, security groups | VNet, private subnets, NSGs, Private Endpoints |
| **Secrets** (`SERVICENOW_PASSWORD`, `AZURE_LLM_API_KEY`, `TEAMS_WEBHOOK_API_KEY`, `LANGSMITH_API_KEY`, `ANTHROPIC_API_KEY`) | `.env` with `chmod 600`, or HashiCorp Vault | AWS Secrets Manager → ECS task env vars / EC2 IAM role | Azure Key Vault + Managed Identity (`azure-identity` is already a dependency) |
| **Dashboard login** (none in the code today) | Nginx + AD / LDAP or OIDC proxy (oauth2-proxy) | ALB OIDC auth (Cognito or Entra ID) | Entra ID (App Service Easy Auth / Container Apps auth) |
| **Logs and metrics** | journald + Grafana / Loki / Prometheus | CloudWatch Logs + Metrics | Azure Monitor / Log Analytics / Application Insights |
| **LLM tracing** (LangSmith) | LangSmith SaaS (or self-hosted LangSmith) | LangSmith SaaS | LangSmith SaaS |
| **Infrastructure as code** | Ansible + Docker Compose (Terraform for vSphere/Proxmox optional) | Terraform or AWS CDK | Terraform or Bicep |
| **CI/CD** | GitHub Actions self-hosted runner / Jenkins | GitHub Actions → ECR → ECS / EC2 | GitHub Actions → ACR → Container Apps / VM |

---

## Changes needed before hosting anywhere off a laptop

These apply to all three columns. Each was found in the current code.

1. **CORS is hard-coded to localhost.** `dashboard_backend/main.py` only allows
   `http://localhost:5173`. Add the real dashboard URL (make it configurable from `.env`).
2. **The dashboard API has no authentication.** Only the Teams webhook checks an API key. Put SSO in
   front of it (see *Dashboard login* above).
3. **Set `CHROME_CLEAR_MODE=simulate` or `off` on servers.** In `real` mode (the default) the tools
   server clears Chrome/Edge *on the machine it runs on*, which would be the server.
4. **Windows paths.** `.env.example` uses `QWEN_RERANKER_MODEL_PATH=C:\POC\Models\...`. Linux
   VMs/containers need a Linux path.
5. **ServiceNow queue is fixed to one assignee** (`ASSIGNED_TO` in `servicenow_queue.py`).
6. **The artifacts folder is not in the repo.** Store it in S3 / Blob / a file share and load it
   at deploy time. The app fails at import without it.
7. **The reranker ignores GPUs.** It is loaded without a device setting, so it runs on CPU even on a
   GPU host. Add the GPU move in `_shared_qwen_reranker_cache.py` before paying for GPU
   instances for it (see section 3).

## Quick recommendation

| If the priority is… | Pick |
|---|---|
| Data never leaves the network, no per-call LLM cost | **Local VMs** with Ollama on a GPU VM (`graph` mode only) |
| Lowest effort, matches today's config (Azure OpenAI, Teams, `azure-identity`, agent mode) | **Azure** |
| The organisation standardises on AWS | **AWS**, with Ollama on GPU EC2 or cross-cloud Azure OpenAI. Adding a Bedrock provider is a small code change. |

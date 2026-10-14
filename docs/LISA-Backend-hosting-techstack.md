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
| **In-process ML** (scikit-learn, joblib, pandas, numpy, rank-bm25) | In-process — CPU only | In-process — CPU only | In-process — CPU only |
| **Reranker** (torch + transformers, Qwen3-Reranker-0.6B) | In-process; weights on VM disk (`QWEN_RERANKER_MODEL_PATH`); CPU works, GPU faster | In-process; weights in S3, copied to EBS at start or baked into the image; optional GPU instance (g6) | In-process; weights in Blob Storage / Azure Files, copied at start or baked into the image; optional GPU VM (NC-series) |
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

## 3. Data and state

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

## 4. Front-ends and channels

| Component | Local (Virtual Machines) | AWS | Azure |
|---|---|---|---|
| **Dashboard** (React + Vite, `../LISA-Frontend`) | `npm run build`, static files served by Nginx on the app VM | S3 + CloudFront | Azure Static Web Apps (or Blob static website + Front Door) |
| **Microsoft Teams bot** (`teams_bot/`) | VM service + public HTTPS endpoint (DMZ reverse proxy or Dev Tunnels); still needs an **Azure Bot Service** registration | ECS / EC2 behind a public ALB; still needs an **Azure Bot Service** registration + Entra ID app | App Service / Container Apps + **Azure Bot Service** (native) |
| **Chat UI / Voice demo** (`../LISA-Chat`, `../LISA-voice`) | Same VM, Nginx / `systemd` | Chat: S3 + CloudFront; Voice: ECS service | Chat: Static Web Apps; Voice: Container Apps |
| **ServiceNow** (Table API, external SaaS) | Outbound HTTPS through the corporate firewall / proxy | Outbound via NAT Gateway with an Elastic IP (for ServiceNow IP allow-listing) | Outbound via NAT Gateway with a static public IP |

## 5. Platform: networking, security, operations

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

## Quick recommendation

| If the priority is… | Pick |
|---|---|
| Data never leaves the network, no per-call LLM cost | **Local VMs** with Ollama on a GPU VM (`graph` mode only) |
| Lowest effort, matches today's config (Azure OpenAI, Teams, `azure-identity`, agent mode) | **Azure** |
| The organisation standardises on AWS | **AWS**, with Ollama on GPU EC2 or cross-cloud Azure OpenAI. Adding a Bedrock provider is a small code change. |

# Think Tank

A local AI system that takes a raw idea through structured analysis, interactive Q&A, and autonomous multi-agent code generation — running entirely on your own hardware with no API costs or context limits.

![Phase 3 multi-agent implementation](docs/multi_agent_implementation.png)

---

## What makes this interesting

**Multi-agent code generation with local models.** Phase 3 runs an orchestrator LLM that reads the full project spec, plans implementation tasks, and dispatches them to sub-agent workers. Each sub-agent writes files, runs shell commands, and reports back. The orchestrator then plans the next batch. All on-device.

**Hybrid inference: Ollama + OpenVINO.** The orchestrator can run on an Intel NPU or iGPU via OpenVINO GenAI while sub-agents use GPU models via Ollama. You can mix backends per stage in a single config file.

**Telemetry-driven model ordering.** Sub-agents try models in order of measured performance — success rate DESC, average duration ASC — computed from per-project telemetry. A flat `models` list in `models.yaml` sets the default order; the runtime reorders it as data accumulates. Models without enough data stay in YAML order. The current ranking is visible in the Phase 3 sidebar and the Ops dashboard.

**Config-only model routing.** All stage → model → backend assignments live in `models.yaml`. Swap any model without touching application code.

**Structured implementation planning.** Before writing a single line of code the orchestrator builds a component-based plan — areas, tasks, and sub-tasks — persisted to `.think-plan.json` in the output directory. Each dispatched task must reference a plan node by exact ID; the orchestrator can add, remove, or restructure plan tasks at any time using `plan_add` / `plan_remove`. Progress is visible in the Phase 3 sidebar and auto-synced with activity events every 30 seconds so the plan stays consistent even after a restart.

**Build-first development.** Phase 3 follows a milestone discipline: scaffold first (entry point + build config + install), verify the build passes, then add features one at a time with a build check after each. Agents can't skip ahead to feature work while the build is broken.

**Resumable runs.** Stopped or failed multi-agent runs can be resumed with a single button click. On resume the orchestrator reads the plan file and scans actual files on disk to reconstruct what was already done — it picks up where it left off rather than restarting from scaffold.

**Per-project agent memory.** Sub-agents store observations about files they read or write into a semantic memory (aiosqlite + Ollama embeddings). Before touching a file, agents search memory to find what already exists — reducing duplicate implementations across parallel workers.

**Live model telemetry.** An Ops dashboard tracks inference call counts, success rates, average duration, p95 latency, and fallback rates per model and per project. A dedicated sub-agent ranking panel shows the current dispatch order and which models have accumulated enough data to be telemetry-ranked vs. still using YAML default order.

![Ops dashboard](docs/ops_dashboard.png)

---

## How It Works

### Phase 1 — Analysis & Selection

Submit an idea with a description, requirements, and constraints. Think Tank spawns multiple parallel solution branches and runs each through an 8-stage pipeline:

| Stage | What it does |
|-------|-------------|
| 0 — Intake | Normalises and structures the raw idea |
| 1 — Feasibility Scan | Assesses technical and practical viability |
| 2 — Solution Design | Designs a concrete solution architecture |
| 3 — Solution Analysis | Deep-dives risks, tradeoffs, and unknowns |
| 4 — Decomposition | Breaks the solution into components and tasks |
| 5 — Component Validation | Validates each component independently |
| 6 — Deep Review | Final cross-cutting review of the full solution |
| 7 — Documentation | Generates architecture docs, specs, roadmap, and more |

Branches that fail are analysed for root cause and new alternative branches are spawned. The pipeline converges when at least one branch reaches VIABLE status. You then review all viable solutions and select one to proceed with.

![Phase 1 solution branching](docs/solution_branching.png)

### Phase 2 — Q&A & Resolution

An interactive chat session that surfaces every open question and assumption from the Phase 1 documents. You answer unknowns, inject constraints, and make architectural decisions. When all questions are resolved, you mark the session as READY.

### Phase 3 — Multi-Agent Implementation

The orchestrator reads the full Phase 1 document package and Phase 2 resolution summary, generates a PRD, builds a plan, then drives a loop:

1. **PRD** — the orchestrator generates a Product Requirements Document from the Phase 1 + Phase 2 material; this becomes the source of truth for implementation
2. **Plan** — the orchestrator builds a component-based plan (`plan_add`) before writing any code: areas break into tasks, tasks break into sub-tasks, all persisted to `.think-plan.json`; each dispatched task must reference a valid plan node by ID — the orchestrator can restructure the plan at any time using `plan_add` / `plan_remove`
3. **Dispatch** — sub-agent workers execute the current task batch: write files with `file_edit`, run install/build/test commands, report back
4. **Verify** — each completed task goes through a verification pass that checks for stubs, broken imports, and hollow implementations; failures trigger a fix cycle (up to 3 rounds)
5. **Review** — the orchestrator inspects results, updates plan task statuses, and decides the next batch or runs a PRD compliance check before signalling done
6. **Iterate** — chat with the agent after completion to request changes or add features; or hit **▶ Resume** to restart a stopped run without adding a message

Plan progress is visible in the Phase 3 sidebar and auto-synced with activity events every 30 seconds.

Sub-agents are tried in telemetry-ranked order — best performing model first, degrading through the list if a model times out, OOMs, or produces bad output. The order is locked at task start so in-task failures don't reshuffle remaining candidates mid-attempt.

Supported project types include Node.js, Python, and .NET — the orchestrator detects the framework and applies the correct scaffold tooling (e.g. `dotnet new sln` for .NET, `npm install` for Node).

---

## Requirements

- Python 3.11+
- Node.js 18+
- [Ollama](https://ollama.com) installed and running
- A GPU with enough VRAM for the models you want to run (tested on RTX 4070 SUPER 12 GB)
- **Optional:** Intel Core Ultra CPU for OpenVINO NPU/GPU stages (tested on Core Ultra 7 265k)

---

## Quick Start

### 1. Pull models

Think Tank routes different pipeline stages to different models. A minimal working set:

```bash
# Phase 1 — analysis and reasoning
ollama pull qwen2.5:7b

# Phase 3 — orchestrator embedding model (for agent memory search)
ollama pull nomic-embed-text

# Phase 3 — sub-agent code generation (add as many as your VRAM allows)
ollama pull qwen2.5-coder:7b    # fast, good baseline
ollama pull qwen3-coder:30b     # larger, better at complex tasks
```

The sub-agent list in `models.yaml` can hold as many models as you like. The runtime tries them in telemetry-ranked order and falls through the list on failure.

See [Model Configuration](#model-configuration) for the full routing setup.

### 2. Install backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp backend/.env.example backend/.env
# Ollama URL defaults to http://localhost:11434 — edit if needed
```

### 4. Install frontend

```bash
cd frontend
npm install
```

### 5. Start

```bash
# From repo root — starts both API and UI
npm run dev
```

Or separately:

```bash
npm run dev:api   # FastAPI on http://localhost:8000
npm run dev:ui    # Vite on http://localhost:5173
```

Open **http://localhost:5173** and submit your first idea.

---

## Model Configuration

All routing lives in `backend/models.yaml`. Each pipeline stage maps to a model, backend, temperature, and context size. No code changes needed to swap models.

```yaml
stages:
  # Phase 1 — reasoning model for analysis stages
  feasibility_scan:
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.2

  # Phase 3 — orchestrator (can use OpenVINO for NPU/iGPU)
  phase3_orchestrator:
    model: "qwen3-8b"
    backend: "openvino"
    device: "AUTO:CPU"

  # Phase 3 — sub-agents: flat list, tried in telemetry-ranked order
  # Runtime reorders by success_rate DESC, avg_duration ASC per project.
  # Add more models freely — smaller/faster ones first as the default.
  phase3_sub_agent:
    models:
      - model: "qwen2.5-coder:7b"
        timeout_seconds: 900
        num_ctx: 32768
      - model: "gemma4-turbo:e4b"
        timeout_seconds: 900
        num_ctx: 32768
      - model: "qwen3-coder:30b"
        timeout_seconds: 1200
        num_ctx: 32768
    backend: "ollama"
    temperature: 0.1
    supports_tools: true
```

The `models` list sets the default dispatch order. Once a model accumulates ≥ 5 calls with ≥ 15% success rate in the current project, it enters the telemetry-ranked pool and is sorted above models that don't qualify yet. Models with no data stay in YAML order at the bottom.

Supported backends: `ollama`, `openvino` (Intel NPU/GPU via OpenVINO GenAI), `llamacpp` (experimental).

### OpenVINO setup (optional)

If you have an Intel Core Ultra CPU, you can run smaller orchestration models on the NPU/iGPU, leaving the GPU fully available for sub-agents.

```bash
pip install openvino-genai
# Convert and cache a model (first run downloads and converts automatically)
# Set backend: "openvino" and device: "AUTO:NPU,GPU,CPU" in models.yaml
```

---

## Project Structure

```
thinktank/
├── backend/
│   ├── app/
│   │   ├── agents/          # Phase 3: orchestrator + sub-agent loop
│   │   ├── api/             # FastAPI route handlers
│   │   ├── db/              # SQLAlchemy models + migrations
│   │   ├── events/          # In-process event bus → WebSocket push
│   │   ├── inference/       # InferenceClient, backend drivers, model registry
│   │   ├── pipeline/        # Phase 1 orchestrator, branch runner, stage implementations
│   │   ├── telemetry.py     # Per-call telemetry log (JSONL)
│   │   └── tools/           # Shell runner, web search, file tools
│   ├── models.yaml          # Stage → model routing (edit this, not code)
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── api/             # API + WebSocket client
│       ├── components/      # React components (pipeline view, phase 3 UI, ops dashboard)
│       └── types/
├── docs/                    # Screenshots
└── package.json             # Root dev scripts (npm run dev starts everything)
```

---

## Hardware Tested

| Component | Spec |
|-----------|------|
| GPU | NVIDIA RTX 4070 SUPER (12 GB VRAM) |
| NPU/iGPU | Intel Core Ultra 7 265K (Arc iGPU + NPU via OpenVINO) |
| RAM | 64 GB |
| OS | Windows 11 |

Smaller GPUs will work with smaller models — adjust `models.yaml` accordingly. The fallback chain means the pipeline degrades gracefully if a large model OOMs.

---

## License

MIT

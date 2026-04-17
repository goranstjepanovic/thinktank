# Think Tank

A local AI system that takes a raw idea and systematically turns it into a working implementation — entirely on your own hardware, with no API costs or context limits.

Think Tank runs a structured three-phase pipeline: deep feasibility analysis across parallel solution branches, an interactive Q&A session to resolve open questions, and finally autonomous code generation with an iterative chat loop for refinement.

![Think Tank Dashboard](docs/screenshot-placeholder.png)

---

## How It Works

### Phase 1 — Analysis & Selection

Submit an idea with a description, requirements, and constraints. Think Tank spawns multiple parallel solution branches and runs each through an 8-stage analysis pipeline:

| Stage | Name | What it does |
|-------|------|-------------|
| 0 | Intake | Normalises and structures the raw idea |
| 1 | Feasibility Scan | Assesses technical and practical viability |
| 2 | Solution Design | Designs a concrete solution architecture |
| 3 | Solution Analysis | Deep-dives risks, tradeoffs, and unknowns |
| 4 | Decomposition | Breaks the solution into components and tasks |
| 5 | Component Validation | Validates each component independently |
| 6 | Deep Review | Final cross-cutting review of the full solution |
| 7 | Documentation | Generates architecture, specs, roadmap, and more |

Branches that fail at any stage are analysed for root cause and new alternative branches are spawned. The pipeline converges when at least one branch reaches VIABLE status. You then review all viable solutions and select one to proceed with.

### Phase 2 — Q&A & Resolution

An interactive chat session with the AI that surfaces every open question and implementation assumption from the Phase 1 documents. You answer unknowns, inject constraints, and make architectural decisions. When all questions are resolved, you mark the session as READY.

### Phase 3 — Implementation

The agent reads the full Phase 1 document package and Phase 2 resolution summary, then generates a complete project:

1. **File plan** — produces a structured file list with proper folder organisation (`frontend/`, `backend/`, `database/`, `infra/`, root scripts)
2. **File generation** — writes each file individually with full context, live progress shown in the UI
3. **Setup** — runs install and build commands automatically
4. **Iteration** — chat with the agent after completion to request changes, add features, or fix issues

---

## Tech Stack

**Backend**
- Python 3.11+ / FastAPI
- SQLite + SQLAlchemy (async)
- Ollama for local model inference
- WebSockets for real-time pipeline events

**Frontend**
- React 18 + TypeScript
- Vite
- React Router

**Models** (via Ollama)
- `phi4:14b` — reasoning, design, code generation
- `qwen2.5:7b` — analysis, decomposition, documentation
- `qwen2.5:3b` — lightweight tasks

---

## Requirements

- Python 3.11+
- Node.js 18+
- [Ollama](https://ollama.com) installed and running
- A GPU with enough VRAM to run the models (tested on RTX 4090 / 24 GB)

---

## Setup

### 1. Pull the models

```bash
ollama pull phi4:14b
ollama pull qwen2.5:7b
ollama pull qwen2.5:3b
```

### 2. Install backend dependencies

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp backend/.env.example backend/.env
# Edit backend/.env if needed (Ollama URL defaults to http://localhost:11434)
```

### 4. Install frontend dependencies

```bash
cd frontend
npm install
```

### 5. Start everything

```bash
# From the repo root — starts both API and UI
npm run dev
```

Or individually:

```bash
npm run dev:api   # FastAPI on http://localhost:8000
npm run dev:ui    # Vite on http://localhost:5173
```

### 6. Verify

```bash
curl http://localhost:8000/api/v1/health
# → {"status":"ok","ollama_reachable":true,"ollama_models":["qwen2.5:3b","qwen2.5:7b","phi4:14b"]}
```

Open **http://localhost:5173** and submit your first idea.

---

## Project Structure

```
thinktank/
├── backend/
│   ├── app/
│   │   ├── agents/          # Phase 3 code generation agent
│   │   ├── api/             # FastAPI route handlers
│   │   ├── db/              # SQLAlchemy models + engine
│   │   ├── events/          # WebSocket event bus + schemas
│   │   ├── inference/       # Model client + Ollama driver
│   │   ├── pipeline/        # Orchestrator, stage runner, Phase 2 agent
│   │   ├── services/        # File manager, utilities
│   │   └── tools/           # Shell runner, web search
│   ├── models.yaml          # Stage → model routing config
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── api/             # API client
│       ├── components/      # React components
│       └── types/           # TypeScript types
├── package.json             # Root dev scripts
├── PRD.md                   # Product requirements
└── PROGRESS.md              # Session-by-session build log
```

---

## Configuration

All model routing is controlled by `backend/models.yaml`. Each pipeline stage maps to a model, backend, temperature, and token budget. You can swap any model without touching application code.

```yaml
stages:
  solution_design:
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.4
    max_tokens: 4096

  phase3_file:
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.3
    max_tokens: 8192
    format: ""
    num_ctx: 32768
```

Supported backends: `ollama`, `llamacpp` (experimental), `openvino` (planned).

---

## License

MIT

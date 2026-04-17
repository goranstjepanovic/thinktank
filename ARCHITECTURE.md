# Think Tank — Technical Architecture

**Status**: Draft  
**Date**: 2026-04-15  
**Version**: 1.0

---

## 1. Repo Structure

```
ThinkTank/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                         # FastAPI app factory, lifespan hooks, router registration
│   │   ├── config.py                       # Pydantic Settings: loads env vars + models.yaml path
│   │   │
│   │   ├── agents/                         # New folder for Phase 3 execution: Orchestrator, specialized agent logic
│   │   │   ├── __init__.py
│   │   │   ├── phase3_orchestrator.py         # Manages the iterative process/session state
│   │   │   ├── code_generator_agent.py        # Handles code generation, execution, and refactoring (gemma4)
│   │   │   ├── physical_asset_agent.py        # Interfaces with CAD/3D services
│   │   │   └── doc_writer_agent.py            # Utility for generating high-quality documentation (PRD, specs)
│   │   │
│   │   ├── services/                        # New folder for external/utility services (e.g., 3D asset conversion)
│   │   │   ├── __init__.py
│   │   │   └── asset_converter.py            # Handles STL, CAD, etc. generation based on specs
│   │   │
│   │   ├── api/
│   │   │   ├── ... (Existing routers)
│   │   │   └── implementation.py             # REST: /ideas/{id}/implementation state tracking
│   │   │
│   │   ├── pipeline/
│   │   │   ├── orchestrator.py             # Manages branch task pool per idea; spawns, monitors, detects convergence
│   │   │   ├── runner.py                   # BranchRunner: stage loop for one solution branch
│   │   │   ├── failure_analysis.py         # FailureAnalyser: evaluates whether new paths exist after branch failure
│   │   │   ├── state_machine.py            # State/transition definitions for ideas and branches
│   │   │   ├── context.py                  # BranchContext: runtime state for one branch run
│   │   │   ├── stages/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── base_stage.py           # BaseStage ABC: run(), validate_output(), build_prompt()
│   │   │   │   ├── s0_intake.py            # Stage 0: normalize raw idea into structured object
│   │   │   │   ├── s1_feasibility.py       # Stage 1: blocker scan, first unachievable gate
│   │   │   │   ├── s2_solution_gen.py      # Stage 2: generate N candidate solutions
│   │   │   │   ├── s3_solution_analysis.py # Stage 3: rank + prune candidates
│   │   │   │   ├── s4_decomposition.py     # Stage 4: component tree with dependencies
│   │   │   │   ├── s5_component_val.py     # Stage 5: validate each component (multiple model calls)
│   │   │   │   ├── s6_deep_review.py       # Stage 6: holistic review, final unachievable gate
│   │   │   │   └── s7_documentation.py     # Stage 7: generate all doc artifacts (7 model calls)
│   │   │   └── prompts/
│   │   │       ├── ... (Jinja2 templates)
│   │   │
│   │   ├── events/
│   │   │   ├── __init__.py
│   │   │   ├── bus.py                      # In-process async event bus (asyncio.Queue per idea)
│   │   │   └── schemas.py                  # Pydantic event payload schemas (PipelineEvent types)
│   │   │
│   │   └── schemas/
│   │       ├── __init__.py
│   │       └── ... (Pydantic schemas)
│   │   │
│   │   ├── inference/
│   │   │   ├── __init__.py
│   │   │   ├── client.py                   # InferenceClient: routes calls to correct backend driver
│   │   │   ├── base.py                     # Abstract InferenceBackend base class + contract
│   │   │   ├── drivers/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── ollama_driver.py        # Ollama REST driver (primary, GPU)
│   │   │   │   ├── llamacpp_driver.py      # llama.cpp server driver (escape hatch)
│   │   │   │   └── openvino_driver.py      # OpenVINO driver stub (NPU, post-v1)
│   │   │   └── model_registry.py           # Loads + caches models.yaml; resolves stage → config
│   │   │
│   │   ├── pipeline/
│   │   │   ├── __init__.py
│   │   │   ├── orchestrator.py             # Manages branch task pool per idea; spawns, monitors, detects convergence
│   │   │   ├── runner.py                   # BranchRunner: stage loop for one solution branch
│   │   │   ├── failure_analysis.py         # FailureAnalyser: evaluates whether new paths exist after branch failure
│   │   │   ├── state_machine.py            # State/transition definitions for ideas and branches
│   │   │   ├── context.py                  # BranchContext: runtime state for one branch run
│   │   │   ├── stages/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── base_stage.py           # BaseStage ABC: run(), validate_output(), build_prompt()
│   │   │   │   ├── s0_intake.py            # Stage 0: normalize raw idea into structured object
│   │   │   │   ├── s1_feasibility.py       # Stage 1: blocker scan, first unachievable gate
│   │   │   │   ├── s2_solution_gen.py      # Stage 2: generate N candidate solutions
│   │   │   │   ├── s3_solution_analysis.py # Stage 3: rank + prune candidates
│   │   │   │   ├── s4_decomposition.py     # Stage 4: component tree with dependencies
│   │   │   │   ├── s5_component_val.py     # Stage 5: validate each component (multiple model calls)
│   │   │   │   ├── s6_deep_review.py       # Stage 6: holistic review, final unachievable gate
│   │   │   │   └── s7_documentation.py     # Stage 7: generate all doc artifacts (7 model calls)
│   │   │   └── prompts/
│   │   │       ├── s0_intake.jinja2
│   │   │       ├── s1_feasibility.jinja2
│   │   │       ├── s2_solution_gen.jinja2
│   │   │       ├── s3_solution_analysis.jinja2
│   │   │       ├── s4_decomposition.jinja2
│   │   │       ├── s5_component_val.jinja2
│   │   │       ├── s6_deep_review.jinja2
│   │   │       └── s7_documentation.jinja2
│   │   │
│   │   ├── events/
│   │   │   ├── __init__.py
│   │   │   ├── bus.py                      # In-process async event bus (asyncio.Queue per idea)
│   │   │   └── schemas.py                  # Pydantic event payload schemas (PipelineEvent types)
│   │   │
│   │   └── schemas/
│   │       ├── __init__.py
│   │       ├── idea.py                     # Pydantic API request/response schemas for Idea
│   │       ├── stage.py                    # Pydantic schemas for StageResult
│   │       ├── document.py                 # Pydantic schemas for Document
│   │       └── model_call.py               # Pydantic schemas for ModelCall audit log
│   │
│   ├── models.yaml                         # Stage → model + backend routing config
│   ├── alembic.ini
│   ├── pyproject.toml                      # fastapi, sqlalchemy, httpx, jinja2, pydantic, alembic
│   └── data/                               # gitignored
│       ├── thinktank.db                    # SQLite database
│       └── documents/
│           └── {idea_id}/
│               ├── executive_summary.md
│               ├── architecture_overview.md
│               ├── component_specs.md
│               ├── requirements_traceability.md
│               ├── risk_register.md
│               ├── implementation_roadmap.md
│               └── open_questions.md
│
├── frontend/
│   ├── public/
│   │   └── index.html
│   ├── src/
│   │   ├── main.tsx                        # React entry point
│   │   ├── App.tsx                         # Root router (React Router)
│   │   ├── api/
│   │   │   ├── client.ts                   # Axios/fetch base client + error handling
│   │   │   ├── ideas.ts                    # API calls: idea CRUD + pipeline control
│   │   │   └── ws.ts                       # WebSocket hook wrapper
│   │   ├── components/
│   │   │   ├── IdeaForm.tsx                # Submission form
│   │   │   ├── IdeaDashboard.tsx           # All ideas list + status chips
│   │   │   ├── IdeaDetail.tsx              # Per-idea: stages, verdict, docs
│   │   │   ├── StageTimeline.tsx           # Visual stage progress tracker
│   │   │   ├── AuditTrail.tsx              # Collapsible model call log with I/O
│   │   │   ├── DocumentViewer.tsx          # Renders generated markdown docs
│   │   │   └── PipelineControls.tsx        # Pause / Resume / Cancel buttons
│   │   ├── hooks/
│   │   │   ├── useIdea.ts                  # React Query hook for single idea
│   │   │   ├── useIdeas.ts                 # React Query hook for ideas list
│   │   │   └── usePipelineEvents.ts        # WebSocket subscription hook
│   │   ├── store/
│   │   │   └── ideaStore.ts                # Zustand: optimistic updates from WS events
│   │   └── types/
│   │       └── index.ts                    # TypeScript types mirroring backend schemas
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts
│
├── .gitignore
├── PRD.md
└── ARCHITECTURE.md
```

---

## 2. Component Responsibilities

### `app/main.py`
Creates the FastAPI app. Lifespan hook: starts/stops the `Orchestrator`, initializes DB engine, loads `models.yaml`, validates all stage models are available in Ollama. Mounts all routers. Configures CORS for React dev server (`localhost:5173`).

### `app/config.py`
Single `Settings` class via `pydantic-settings`. Reads: `MODELS_YAML_PATH`, `DATABASE_URL`, `OLLAMA_BASE_URL`, `LLAMACPP_BASE_URL`, `MAX_CONCURRENT_PIPELINES`. `ModelRegistry` is initialized here and injected as a FastAPI dependency.

### `app/inference/client.py` — InferenceClient
The **only** inference entry point for the entire system. Pipeline stages never call Ollama directly.

Responsibilities:
1. Resolve `model + backend` from `ModelRegistry` using `stage_key`
2. Build `InferenceRequest` (apply any per-call overrides)
3. Call `driver.complete()`
4. Log `ModelCall` to DB (always, including on failure)
5. Parse response JSON and return as `dict`

### `app/inference/drivers/` — Backend Drivers
Each driver implements `InferenceBackend`. Drivers are **stateless transport adapters only** — no logging, no retry, no prompt building.

| Driver | Target | Status |
|--------|--------|--------|
| `ollama_driver.py` | `POST /api/chat` with `format: "json"` | v1 |
| `llamacpp_driver.py` | llama.cpp OpenAI-compatible endpoint | v1 escape hatch |
| `openvino_driver.py` | Intel NPU via OpenVINO | Post-v1 stub |

Adding NPU support post-v1:
1. Implement `openvino_driver.py`
2. Register it in the `drivers` dict in `main.py` lifespan
3. Set `backend: openvino` on target stages in `models.yaml`
**Zero changes to pipeline, runner, or stages.**

### `app/pipeline/orchestrator.py` — Orchestrator
Owns two maps: `idea_id → set[asyncio.Task]` (one task per active branch) and a global branch task pool. Responsibilities:
- On idea submission: spawn N initial solution branches
- On branch failure: run failure analysis, determine if new paths exist, spawn new branch if so, otherwise update convergence tracker
- On convergence (no active branches, no new paths): mark idea CONVERGED
- On user abandon: cancel all active branch tasks for the idea
- On pause/resume: signal all branches for an idea
- Enforces `MAX_CONCURRENT_BRANCHES` across all ideas via a global `asyncio.Queue`
- On server restart: re-queues branches found in `RUNNING` state

### `app/pipeline/runner.py` — BranchRunner
Executes the stage loop for **one solution branch**:
1. Load branch state from DB (including inherited failure context from parent)
2. Determine next stage
3. Call `stage.run(ctx)`
4. Persist result, emit events
5. Advance state machine
6. Check pause signal between stages

If a stage returns `failed=True`, the runner marks the branch FAILED and notifies the Orchestrator. The Orchestrator then triggers failure analysis — not the runner.

### `app/pipeline/failure_analysis.py` — FailureAnalyser
Called by the Orchestrator after any branch failure. Receives:
- The failed branch's full context (what was tried, at what stage, why it failed)
- All previously explored paths for this idea (from DB)

Returns:
- `new_path_exists: bool`
- `suggested_direction: str` — the unexplored angle to pursue
- `reasoning: str` — for audit trail

If `new_path_exists=True`, Orchestrator spawns a new branch with `parent_branch_id` and `inherited_context` set. If `False`, updates the convergence tracker; if no active branches remain, idea is marked CONVERGED.

### `app/pipeline/stages/base_stage.py` — BaseStage
Abstract base all stage classes inherit from:

```python
class BaseStage(ABC):
    stage_key: str              # matches models.yaml key

    @abstractmethod
    async def run(self, ctx: PipelineContext) -> StageOutput: ...

    def build_prompt(self, ctx: PipelineContext) -> list[Message]:
        # renders self.stage_key.jinja2 with ctx fields

    def validate_output(self, raw: dict) -> StageOutput:
        # Pydantic parse; raises StageOutputValidationError on failure
```

### `app/pipeline/stages/s5_component_val.py`
The only stage that makes **multiple** `InferenceClient` calls in one `run()` — one per component from Stage 4's decomposition tree. Results accumulate into a list. This is why `component_validator` maps to a smaller/faster model — it runs N times per idea.

### `app/pipeline/stages/s7_documentation.py`
Makes **7 separate** `InferenceClient` calls (one per document type). Writes each result to `data/documents/{idea_id}/{doc_type}.md`, inserts a `Document` row, and emits `document.created` per doc. Partial completion survives restarts.

### `app/events/bus.py` — Event Bus
In-process only. `dict[str, list[asyncio.Queue]]` — one queue list per `idea_id`. Pipeline calls `publish(idea_id, event)`. WebSocket handler subscribes on connect, unsubscribes on disconnect. No Redis or external broker needed.

### `app/db/repositories/`
Each repository wraps `AsyncSession` and exposes typed async methods. Injected via `Depends()` into API handlers and passed through `PipelineContext` to the runner. No session sharing between tasks.

---

## 3. Data Model

### `ideas`
```
id              TEXT        PRIMARY KEY  (UUID v4)
name            TEXT        NOT NULL
description     TEXT        NOT NULL
requirements    TEXT        NOT NULL
constraints     TEXT        NOT NULL
status          TEXT        NOT NULL     QUEUED | RUNNING | PAUSED | CONVERGED | ABANDONED
                                         -- CONVERGED = solution space exhausted (may have viable branches)
                                         -- ABANDONED = user stopped it
created_at      DATETIME    NOT NULL
updated_at      DATETIME    NOT NULL
```

### `solution_branches`
One row per solution branch. An idea has many branches; branches can spawn child branches.

```
id                  TEXT        PRIMARY KEY  (UUID v4)
idea_id             TEXT        NOT NULL     FK → ideas.id
parent_branch_id    TEXT                     FK → solution_branches.id  (NULL for initial branches)
branch_index        INTEGER     NOT NULL     Sequential per idea (1, 2, 3…)
status              TEXT        NOT NULL     QUEUED | RUNNING | PAUSED | VIABLE | FAILED | CANCELLED
current_stage       INTEGER     NOT NULL     DEFAULT 0
approach_summary    TEXT                     Short description of this branch's solution angle
inherited_context   TEXT                     JSON — failure reasons + explored paths passed from parent
failure_stage       INTEGER                  Stage at which this branch failed (NULL if not failed)
failure_reason      TEXT                     Why this branch failed (used by FailureAnalyser)
created_at          DATETIME    NOT NULL
updated_at          DATETIME    NOT NULL
```

### `stage_results`
One row per stage per branch.

```
id              TEXT        PRIMARY KEY  (UUID v4)
branch_id       TEXT        NOT NULL     FK → solution_branches.id
stage_index     INTEGER     NOT NULL     (0–7)
stage_name      TEXT        NOT NULL
status          TEXT        NOT NULL     PENDING | RUNNING | COMPLETED | FAILED | SKIPPED
output_json     TEXT                     JSON blob — stage-specific structured output
failed          BOOLEAN     NOT NULL     DEFAULT FALSE
failure_reason  TEXT
started_at      DATETIME
completed_at    DATETIME
UNIQUE(branch_id, stage_index)
```

### `failure_analyses`
Records what FailureAnalyser concluded after each branch failure.

```
id                  TEXT        PRIMARY KEY  (UUID v4)
idea_id             TEXT        NOT NULL     FK → ideas.id
failed_branch_id    TEXT        NOT NULL     FK → solution_branches.id
new_path_exists     BOOLEAN     NOT NULL
suggested_direction TEXT                     The unexplored angle (if new_path_exists=True)
reasoning           TEXT        NOT NULL
spawned_branch_id   TEXT                     FK → solution_branches.id (if a new branch was spawned)
model_call_id       TEXT        NOT NULL     FK → model_calls.id (the inference call that produced this)
created_at          DATETIME    NOT NULL
```

### `model_calls`
```
id              TEXT        PRIMARY KEY  (UUID v4)
idea_id         TEXT        NOT NULL     FK → ideas.id
branch_id       TEXT                     FK → solution_branches.id (NULL for failure analysis calls)
stage_result_id TEXT                     FK → stage_results.id (NULL for failure analysis calls)
call_type       TEXT        NOT NULL     STAGE | FAILURE_ANALYSIS
call_index      INTEGER     NOT NULL     (for multi-call stages: s5, s7)
model_name      TEXT        NOT NULL
backend         TEXT        NOT NULL
prompt_json     TEXT        NOT NULL     JSON array of messages sent
response_json   TEXT        NOT NULL     Full raw backend response
tokens_prompt   INTEGER
tokens_completion INTEGER
duration_ms     INTEGER
created_at      DATETIME    NOT NULL
```

### `documents`
One set of docs per viable branch (multiple viable branches = multiple doc sets).

```
id              TEXT        PRIMARY KEY  (UUID v4)
idea_id         TEXT        NOT NULL     FK → ideas.id
branch_id       TEXT        NOT NULL     FK → solution_branches.id
doc_type        TEXT        NOT NULL     EXECUTIVE_SUMMARY | ARCHITECTURE_OVERVIEW |
                                         COMPONENT_SPECS | REQUIREMENTS_TRACEABILITY |
                                         RISK_REGISTER | IMPLEMENTATION_ROADMAP | OPEN_QUESTIONS
file_path       TEXT        NOT NULL     data/documents/{idea_id}/{branch_index}/{doc_type}.md
content_hash    TEXT        NOT NULL     SHA-256 for integrity
created_at      DATETIME    NOT NULL
```

### Indexes
- `solution_branches(idea_id, status)` — active branch queries
- `solution_branches(parent_branch_id)` — tree traversal
- `stage_results(branch_id, stage_index)` — pipeline resumption
- `model_calls(idea_id, branch_id)` — audit trail
- `documents(idea_id, branch_id)` — document lookup
- `failure_analyses(idea_id)` — convergence check (all failures for an idea)

---

## 4. API Surface

### REST Endpoints

#### Ideas
```
POST   /api/v1/ideas
       Body: { name, description, requirements, constraints }
       Returns: IdeaResponse (201)
       Side effect: Creates QUEUED idea, spawns N initial solution branches

GET    /api/v1/ideas
       Returns: list[IdeaSummaryResponse]
       Includes: id, name, status, active_branch_count, viable_branch_count, created_at

GET    /api/v1/ideas/{idea_id}
       Returns: IdeaDetailResponse
       Includes: idea fields + solution_branches summary list (id, status, current_stage, approach_summary)

DELETE /api/v1/ideas/{idea_id}
       Returns: 204  — abandons idea, cancels all active branches
```

#### Pipeline Control (idea-level — applies to all branches)
```
POST   /api/v1/ideas/{idea_id}/pause    → { status: "PAUSED" }   | 409
POST   /api/v1/ideas/{idea_id}/resume   → { status: "RUNNING" }  | 409
POST   /api/v1/ideas/{idea_id}/abandon  → { status: "ABANDONED" }
```

#### Solution Branches
```
GET    /api/v1/ideas/{idea_id}/branches
       Returns: list[BranchSummaryResponse]
       Includes: id, branch_index, status, current_stage, approach_summary, parent_branch_id, failure_reason

GET    /api/v1/ideas/{idea_id}/branches/{branch_id}
       Returns: BranchDetailResponse (full stage_results list)

GET    /api/v1/ideas/{idea_id}/branches/{branch_id}/stages
GET    /api/v1/ideas/{idea_id}/branches/{branch_id}/stages/{stage_index}
```

#### Failure Analyses
```
GET    /api/v1/ideas/{idea_id}/failure-analyses
       Returns: list[FailureAnalysisResponse]
       Includes: failed_branch_id, new_path_exists, suggested_direction, spawned_branch_id, reasoning
```

#### Audit Trail
```
GET    /api/v1/ideas/{idea_id}/model-calls
       Query params: branch_id (optional), call_type (optional: STAGE | FAILURE_ANALYSIS)
       Returns: list[ModelCallResponse]
```

#### Documents
```
GET    /api/v1/ideas/{idea_id}/documents
       Returns: list[DocumentResponse] grouped by branch_id

GET    /api/v1/ideas/{idea_id}/branches/{branch_id}/documents
       Returns: list[DocumentResponse] for this branch

GET    /api/v1/ideas/{idea_id}/branches/{branch_id}/documents/{doc_type}
       Returns: { content: "<markdown string>" }
```

#### System
```
GET    /api/v1/health
       Returns: { status: "ok", ollama_reachable: bool, db_ok: bool }

GET    /api/v1/system/models
       Returns: parsed models.yaml content
```

### WebSocket

```
WS     /ws/ideas/{idea_id}
```

Client connects per-idea. Server subscribes connection to the event bus for that `idea_id`. All pipeline events are pushed as JSON `PipelineEvent` objects.

#### Event Envelope
```typescript
type PipelineEvent = {
  event_type: string
  idea_id: string
  timestamp: string   // ISO 8601
  payload: object
}
```

#### Event Types
| event_type | payload | trigger |
|---|---|---|
| `branch.spawned` | `{ branch_id, branch_index, approach_summary, parent_branch_id }` | New branch created |
| `branch.started` | `{ branch_id, branch_index }` | BranchRunner begins |
| `stage.started` | `{ branch_id, stage_index, stage_name }` | Stage `run()` called |
| `stage.model_call` | `{ branch_id, stage_index, model_name, backend }` | Each InferenceClient call |
| `stage.completed` | `{ branch_id, stage_index, stage_name, duration_ms }` | Stage result persisted |
| `stage.failed` | `{ branch_id, stage_index, error }` | Stage failed |
| `branch.failed` | `{ branch_id, failure_stage, failure_reason }` | Branch marked FAILED |
| `branch.viable` | `{ branch_id }` | Stage 6 returns VIABLE; Stage 7 starting |
| `branch.paused` | `{ branch_id, stage_index }` | Pause signal honored |
| `branch.resumed` | `{ branch_id, stage_index }` | Resume signal honored |
| `branch.cancelled` | `{ branch_id }` | Cancelled by abandon |
| `failure_analysis.started` | `{ failed_branch_id }` | FailureAnalyser begins |
| `failure_analysis.completed` | `{ failed_branch_id, new_path_exists, spawned_branch_id }` | Analysis done |
| `idea.converged` | `{ viable_branch_ids: [] }` | Solution space exhausted |
| `idea.abandoned` | `{}` | User abandoned |
| `document.created` | `{ branch_id, doc_type, file_path }` | Stage 7 writes each doc |

The frontend `usePipelineEvents` hook feeds each event into Zustand, enabling real-time updates without polling.

---

## 5. Pipeline State Machine

### Idea-Level States

```
         ┌──────────┐
         │  QUEUED  │◄── POST /ideas
         └────┬─────┘
   Orchestrator spawns initial branches
         ┌────▼─────┐
         │ RUNNING  │◄──────────── POST /resume
         └────┬─────┘
  ┌───────────┼──────────────┐
  │           │              │
pause    all branches    POST /abandon
signal   settled
  │           │              │
┌─▼──────┐  ┌─▼──────────┐ ┌─▼─────────┐
│ PAUSED │  │ CONVERGED  │ │ ABANDONED │
└─┬──────┘  └────────────┘ └───────────┘
  │           (solution space exhausted;
POST /resume   may have viable branches)

CONVERGED and ABANDONED are terminal.
```

### Branch-Level States

```
         ┌──────────┐
         │  QUEUED  │◄── Orchestrator spawns
         └────┬─────┘
   Capacity slot available
         ┌────▼─────┐
         │ RUNNING  │◄──────────── resume signal
         └────┬─────┘
  ┌───────────┼────────────┐
  │           │            │
pause    stage 6        any stage
signal  passes          fails
  │           │            │
┌─▼──────┐ ┌─▼──────┐  ┌──▼────┐
│ PAUSED │ │ VIABLE │  │FAILED │
└─┬──────┘ └────────┘  └───────┘
  │           ↓               ↓
POST     stage 7 runs   FailureAnalyser
/resume  docs generated  evaluates paths
                         ↓
                   new branch spawned (or not)

idea-level ABANDON → all RUNNING/PAUSED branches → CANCELLED
```

### State Transition Guards

**Idea**
| From | To | Guard |
|------|----|-------|
| QUEUED | RUNNING | At least one branch task starts |
| RUNNING | PAUSED | Pause signal; all branches pause at next stage boundary |
| RUNNING | CONVERGED | No active branches and FailureAnalyser finds no new paths |
| RUNNING | ABANDONED | User POST /abandon |
| PAUSED | RUNNING | Resume signal; all paused branches re-queued |
| PAUSED | ABANDONED | Immediate |

**Branch**
| From | To | Guard |
|------|----|-------|
| QUEUED | RUNNING | Global branch capacity slot available |
| RUNNING | PAUSED | Idea pause signal; checked between stage boundaries |
| RUNNING | VIABLE | Stage 6 returns `verdict=VIABLE` |
| RUNNING | FAILED | Any stage returns `failed=True` or unhandled exception |
| RUNNING | CANCELLED | `asyncio.Task.cancel()` from Orchestrator on abandon |
| PAUSED | RUNNING | Idea resume signal |
| VIABLE / FAILED / CANCELLED | — | Terminal |

### Concurrency Model

- Each solution branch = one `asyncio.Task`
- `InferenceClient` uses `httpx.AsyncClient` — non-blocking HTTP to Ollama
- `MAX_CONCURRENT_BRANCHES` (global across all ideas) enforced via `asyncio.Queue` in Orchestrator
- On branch completion/failure, Orchestrator callback decrements counter and dequeues next

**Pause mechanics:**  
Each branch's `PipelineContext` holds an `asyncio.Event` (`pause_event`). Between each stage:
```python
if not ctx.pause_event.is_set():
    await ctx.pause_event.wait()
```
Pause only takes effect at stage boundaries — never mid-inference. Orchestrator sets this event on all branches of an idea simultaneously.

**Cancellation mechanics:**  
`asyncio.Task.cancel()` raises `CancelledError`. BranchRunner catches it in `try/finally` to persist `CANCELLED` status.

**Failure → spawn flow:**
```
BranchRunner: stage fails → marks branch FAILED → calls orchestrator.on_branch_failed(branch_id)
Orchestrator: calls FailureAnalyser.analyse(branch_id)
FailureAnalyser: queries all prior failures for idea → calls InferenceClient(stage_key="failure_analysis")
             → returns { new_path_exists, suggested_direction, reasoning }
Orchestrator: if new_path_exists → create new branch row (parent=failed branch) → enqueue
             else → update convergence tracker → check if any active branches remain
                  → if none → mark idea CONVERGED
```

**DB sessions:**  
Each branch task holds its own `AsyncSession`. API handlers use separate sessions via `Depends()`. No cross-task session sharing.

---

## 6. InferenceClient Interface

### Abstract Backend Contract (`app/inference/base.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

@dataclass
class Message:
    role: str       # "system" | "user" | "assistant"
    content: str

@dataclass
class InferenceRequest:
    model: str
    messages: list[Message]
    format: str = "json"            # always json — Ollama enforces structured output
    temperature: float = 0.2
    max_tokens: int | None = None
    extra: dict[str, Any] | None = None   # backend-specific passthrough

@dataclass
class InferenceResponse:
    content: str                    # raw response string (JSON)
    model: str                      # model name as reported by backend
    tokens_prompt: int | None
    tokens_completion: int | None
    duration_ms: int | None
    raw_response: dict              # full unmodified response (for audit log)

class InferenceBackend(ABC):
    """
    Stateless transport adapter contract.
    No logging, retry, or prompt building — those live in InferenceClient.
    """

    @abstractmethod
    async def complete(self, request: InferenceRequest) -> InferenceResponse: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    @abstractmethod
    def list_available_models(self) -> list[str]: ...
```

### InferenceClient (`app/inference/client.py`)

```python
class InferenceClient:
    def __init__(
        self,
        registry: ModelRegistry,
        drivers: dict[str, InferenceBackend],   # keyed by backend name
        model_call_repo: ModelCallRepository,
    ): ...

    async def call(
        self,
        stage_key: str,
        messages: list[Message],
        idea_id: str,
        stage_result_id: str,
        call_index: int = 0,
        overrides: dict | None = None,
    ) -> dict:
        """
        1. Resolve model + backend from registry
        2. Build InferenceRequest (apply overrides)
        3. Call driver.complete()
        4. Log ModelCall to DB (always, even on failure)
        5. Parse response JSON
        6. Return parsed dict
        """
```

---

## 7. models.yaml

```yaml
# models.yaml
# Maps each pipeline stage to its model + backend.
# Changing a model or backend = edit this file + restart. No code changes required.
#
# backend: "ollama" | "llamacpp" | "openvino"

defaults:
  temperature: 0.2
  max_tokens: null
  format: "json"

backends:
  ollama:
    base_url: "http://localhost:11434"
    timeout_seconds: 120
  llamacpp:
    base_url: "http://localhost:8080"
    timeout_seconds: 120
  openvino:
    # Post-v1. Driver raises NotImplementedError until implemented.
    model_dir: "C:/models/openvino"
    device: "NPU"           # or "CPU" for fallback testing
    timeout_seconds: 60

stages:
  intake:                   # Stage 0 — deterministic parse, smaller model fine
    model: "qwen2.5:7b"
    backend: "ollama"
    temperature: 0.1
    max_tokens: 1024

  feasibility_scan:         # Stage 1 — must be rigorous, use large model
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.2
    max_tokens: 2048

  solution_generation:      # Stage 2 — generative, slightly higher temp for diversity
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.4
    max_tokens: 4096

  solution_analysis:        # Stage 3 — analytical ranking, low temp
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.2
    max_tokens: 3072

  decomposition:            # Stage 4 — structural tree output, deterministic
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.15
    max_tokens: 4096

  component_validator:      # Stage 5 — called N times (one per component), use small model
    model: "qwen2.5:3b"     # Post-v1: swap to openvino for NPU parallel execution
    backend: "ollama"
    temperature: 0.2
    max_tokens: 1024

  deep_review:              # Stage 6 — holistic synthesis, needs full model
    model: "phi4:14b"
    backend: "ollama"
    temperature: 0.2
    max_tokens: 3072

  documentation:            # Stage 7 — long-form writing, 7 calls, generous token budget
    model: "qwen2.5:7b"
    backend: "ollama"
    temperature: 0.3
    max_tokens: 8192

  failure_analysis:         # Called after each branch failure — evaluates whether new paths exist
    model: "phi4:14b"       # Needs strong reasoning to avoid false convergence
    backend: "ollama"
    temperature: 0.2
    max_tokens: 2048

resources:
  max_concurrent_branches: 4   # global across all ideas — primary VRAM guard
                                # e.g. 2 ideas × 2 branches each = 4 concurrent
  initial_branches_per_idea: 2  # 2 branches spawned at idea submission — enough for parallel exploration without VRAM pressure
  vram_budget_gb: 20.0          # informational; branch limit is the v1 enforcement
```

---

## 8. Stage Output Schemas

Each stage returns a well-defined Pydantic model. The prompt template instructs the model to produce exactly this schema. `validate_output()` in each stage class parses and validates. On validation failure in v1, the stage is marked `FAILED` and the pipeline halts.

### Stage 1 — Feasibility Scan
```python
class FeasibilityScanOutput(BaseModel):
    verdict: Literal["PASS", "UNACHIEVABLE"]
    confidence: float               # 0.0–1.0
    blockers: list[str]             # empty if PASS
    warnings: list[str]             # non-blocking concerns
    reasoning: str                  # free-text for audit trail
```

### Stage 2 — Solution Generation
```python
class SolutionCandidate(BaseModel):
    id: str
    name: str
    description: str
    approach_summary: str
    key_risks: list[str]

class SolutionGenerationOutput(BaseModel):
    candidates: list[SolutionCandidate]   # 2–4 candidates
    reasoning: str
```

### Stage 4 — Decomposition
```python
class Component(BaseModel):
    id: str
    name: str
    description: str
    priority: Literal["P0", "P1", "P2"]
    depends_on: list[str]           # list of component ids
    risks: list[str]
    unknowns: list[str]

class DecompositionOutput(BaseModel):
    solution_id: str                # selected candidate from Stage 3
    components: list[Component]
    dependency_notes: str
```

### Stage 5 — Component Validation (per component)
```python
class ComponentValidationOutput(BaseModel):
    component_id: str
    verdict: Literal["VALID", "RISKY", "UNACHIEVABLE"]
    confidence: float
    issues: list[str]
    mitigations: list[str]
    reasoning: str
```

### Stage 6 — Deep Review
```python
class DeepReviewOutput(BaseModel):
    verdict: Literal["VIABLE", "UNACHIEVABLE"]
    confidence: float
    gaps: list[str]
    conflicts: list[str]
    unmet_requirements: list[str]
    recommendation: str
    reasoning: str
```

---

## 9. Context Accumulation

`PipelineContext` accumulates stage outputs as the pipeline progresses. Each stage's Jinja2 template selectively includes prior stage fields it needs as context — not the full history verbatim. This is what gives Stage 6 visibility into all prior reasoning while keeping prompts bounded.

Example context available to Stage 6 prompt:
- Original idea (name, description, requirements, constraints)
- Stage 1: blockers, warnings
- Stage 2: all candidate names and summaries
- Stage 3: selected candidate + reasoning
- Stage 4: full component tree
- Stage 5: all component verdicts and issues

---

## 10. Key Design Rationale

**Why no LangGraph?**  
The pipeline is a deterministic 8-stage linear state machine with two exit conditions. A `for stage in stages` loop with DB-persisted state is more debuggable, easier to pause/resume, and has zero framework overhead for this use case. LangGraph's value is dynamic graph execution and agent-driven branching — not what this pipeline needs.

**Why `output_json` as a single column?**  
Each stage has a different output schema. Storing as JSON avoids 8 separate tables while keeping the schema evolvable. Stage-specific Pydantic models enforce shape at the application layer.

**Why 7 separate model calls for Stage 7?**  
Each document type has a different prompt and different context subset. Splitting calls allows partial completion (interrupted Stage 7 = some docs already written), per-doc event emission for UI progress, and easier per-document quality tuning via temperature/token overrides.

**VRAM strategy for v1:**  
`max_concurrent_pipelines: 2` is the guard. Two ideas both using `phi4:14b` (~8GB each) = ~16GB VRAM. `qwen2.5:3b` component validator (~2GB) fits alongside. No dynamic VRAM tracking in v1 — queue limit is the safeguard.

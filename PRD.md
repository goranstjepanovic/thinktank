# PRD: Think Tank

**Status**: Draft  
**Author**: Solo  
**Date**: 2026-04-16  
**Version**: 1.5

---

## 1. Problem Statement

Cloud AI tools (Claude, Codex, etc.) are excellent for idea exploration but have fundamental limitations for deep, iterative analysis: token limits truncate long reasoning chains, costs accumulate with repeated exploration, context is lost between sessions, and the models are general-purpose rather than specialized. The result is shallow, expensive, and stateless idea evaluation.

Think Tank solves this by running entirely locally on dedicated hardware, with no token cost, no context ceiling, and models fine-tuned to specific reasoning tasks. The goal is not to chat about ideas — it is to systematically evaluate them: determine feasibility, surface risks early, decompose complex ideas into actionable parts, and produce implementation-ready documentation for ideas worth pursuing.

### Three-Phase Vision

Think Tank is designed in three distinct, sequential phases, governed by deliberate human decision points:

**Phase 1 — Analysis & Selection (Current Scope)**  
The system explores an idea across multiple parallel solution branches, evaluates each through a structured pipeline, and produces a complete documentation package for every viable solution (using stages 0-7). The pipeline then halts, requiring the user to review all viable solutions and select one path for development. This is an intentional stopping point — the human must commit to a direction before development begins.

**Phase 2 — Open Question Resolution & Contextualization**  
Starting from the selected solution's documentation, this interactive phase bridges the gap between analysis and building. The system surfaces all unresolved Open Questions (F9) and implementation assumptions. The user must answer these unknowns and inject any required additional context, constraints, or feature preferences. This iterative dialogue continues until the system is confident the Open Questions are resolved and the foundational context for coding/design is established.

**Phase 3 — Implementation (Focus Area)**  
Once the context is fully defined, the system transitions to an active, directive implementation loop. Based on the selected solution's type, the system executes specialized agents to:
- **Software ideas:** Generate working code, scaffolding, tests, and CI configuration; iterating towards a runnable application.
- **Physical/hardware ideas:** Generate engineering artifacts: detailed component diagrams, PCB layouts, CAD models, 3D-printable assets, and BOMs.

Phase 3 is the main development cycle, driven by the artifacts created in Phase 1 and clarified in Phase 2.

---

## 2. Goals & Success Metrics

### Goals
- Determine whether an idea is worth pursuing through structured, automated multi-stage analysis
- Produce complete implementation documentation for every viable solution found
- Present viable solutions for human review; allow the user to select one to develop further
- Run entirely locally with no external API dependencies or ongoing cost
- Leverage GPU and NPU hardware for parallel model inference
- Code generation or automated implementation

### Non-Goals (Out of Scope for v1)
- Physical artifact generation: CAD, PCB layouts, 3D-printable files *(Phase 2 — deferred)*
- Cloud execution or remote model inference
- Multi-user collaboration or shared workspaces
- Real-time collaboration or team features
- Mobile interface
- Integration with external project management tools (Jira, Linear, etc.)

### Success Metrics
| Metric | Target |
|--------|--------|
| Idea reaches a definitive verdict (viable / unachievable) | 100% of submitted ideas |
| Viable ideas produce a complete documentation package | All ideas passing final review stage |
| User can review and compare viable solutions, then select one to proceed with | Phase 1 complete |
| All inference runs locally with no network dependency | Hard requirement |
| System runs stably on target hardware without OOM errors | Hard requirement |

---

## 3. Users & Stakeholders

### Target User
| Persona | Description | Key Need |
|---------|-------------|----------|
| Technical Solo Developer | Single technical user — the project owner. Comfortable with local tooling, model management, and system configuration. Submits ideas ranging from software projects to system designs. | Rapid, thorough feasibility evaluation and structured documentation without cost or context limits |

### Stakeholders
- **Owner**: Project author (solo)

---

## 4. User Stories

### Must Have (P0)
- As a developer, I want to submit an idea with a name, description, requirements, and constraints so that the system has enough context to begin analysis.
- As a developer, I want the system to automatically analyze my idea in the background so that I don't have to manually drive each step.
- As a developer, I want to see the current status of all active solution branches for an idea so that I know what is being explored.
- As a developer, I want the system to explore multiple solution approaches in parallel so that different paths are evaluated simultaneously.
- As a developer, I want failed solutions to inform the generation of new solution branches so that the system learns from dead ends rather than repeating them.
- As a developer, I want each solution to be broken down into sub-components and validated so that weak parts are caught early.
- As a developer, I want the system to stop spawning new solutions when no meaningfully different paths remain so that it converges rather than running indefinitely.
- As a developer, I want to abandon an idea at any time with a single action so that I can stop analysis I no longer want to pursue.
- As a developer, I want a complete documentation package for any solution that passes all stages so that I can begin implementation immediately.

### Should Have (P1)
- As a developer, I want to see the full solution tree for an idea — which solutions spawned which, and why — so that I can audit the exploration history.
- As a developer, I want to see the reasoning behind each stage decision so that I can challenge conclusions.
- As a developer, I want to be notified when a solution reaches a new stage or a new branch is spawned so that I can check in without polling.
- As a developer, I want to pause and resume analysis on an idea so that I can manage system resources.
- As a developer, I want to provide additional context mid-analysis so that the system can adjust its exploration.
- As a developer, I want models to execute Python scripts during analysis so that numerical estimates, data parsing, and algorithmic checks are grounded in actual computed results rather than model inference.

### Should Have (P1) — continued
- As a developer, I want to select a viable solution to mark it as the one I'm developing so that there is a clear record of which solution was chosen and why the others were not.
- As a developer, I want the system to surface the Open Questions from the selected solution's documentation when I begin Phase 2, so that I resolve blocking unknowns before implementation starts rather than discovering them mid-way.
- As a developer, I want to provide additional context, preferences, and constraints at the start of Phase 2 so that implementation reflects decisions I couldn't make during analysis.
- As a developer, I want the system to ask me clarifying questions as they arise during implementation so that it doesn't silently make assumptions on my behalf.
- As a developer, I want to review, correct, and redirect each implementation step so that I stay in control of the output rather than receiving a monolithic result I can't influence.

### Nice to Have (P2)
- As a developer, I want to compare two viable solutions side by side so that I can choose which to pursue.
- As a developer, I want documentation rendered visually (2D diagrams, eventually 3D for physical ideas) so that spatial relationships are easier to understand.
- As a developer, I want to export documentation in multiple formats so that I can share or archive it.
- As a developer, I want a history of all ideas and their solution trees so that I can revisit past evaluations.

---

## 5. Functional Requirements

### Core Features

| # | Feature | Description | Priority |
|---|---------|-------------|----------|
| F1 | Idea Submission | Form to capture idea name, description, goals, requirements, and constraints | P0 |
| F2 | Orchestrator Agent | Manages the solution pool per idea: spawns initial branches, monitors failures, triggers new branches, detects convergence | P0 |
| F3 | Parallel Solution Branches | Each idea runs multiple solution branches simultaneously, each progressing independently through pipeline stages | P0 |
| F4 | Failure-driven Branch Spawning | When a solution fails, a failure analysis determines whether a new unexplored path exists; if so, spawns a new branch with inherited failure context | P0 |
| F5 | Solution Decomposition | Break each solution into a component tree with dependencies | P0 |
| F6 | Component Validation | Validate each component individually; failures bubble up to the branch verdict | P0 |
| F7 | Convergence Detection | After each failure, evaluate whether any meaningfully different solution paths remain; terminate exploration when none do | P0 |
| F8 | User Abandonment | User can abandon an idea at any time, stopping all active branches | P0 |
| F9 | Documentation Package | For each solution that reaches Stage 6 (VIABLE), generate full implementation docs | P0 |
| F10 | Status Dashboard | Web UI: all ideas, solution tree per idea, per-branch stage progress, verdicts | P0 |
| F11 | Solution Tree Visualisation | Display solution genealogy: which branch spawned which, failure reasons, current stage per branch | P1 |
| F12 | Audit Trail | Per-branch reasoning chain: every model call with inputs, outputs, model identity | P1 |
| F13 | Pause / Resume | Pause all active branches for an idea; resume resumes all paused branches | P1 |
| F14 | Mid-analysis User Input | Inject additional context into the active exploration; fed into next branch spawn | P1 |
| F15 | 2D/3D Visualisation | Render component diagrams in 2D (MVP: Mermaid); future: 3D for physical device ideas via React Three Fiber | P2 |
| F16 | Python Script Runner | Sandboxed tool available to all pipeline models: model submits a Python script, backend executes it in a restricted subprocess, stdout/stderr/return value returned to the model as a tool call result. Enables grounded calculation, data parsing, and algorithmic validation during analysis. Script + result logged in audit trail. | P1 |
| F17 | Solution Selection | After analysis converges, the user reviews all viable solutions and marks one as Selected. The selected solution becomes the starting point for Phase 2 (implementation). Only one solution per idea can be selected. Selection is recorded with a timestamp and any notes the user adds. | P1 |
| F19 | Phase 2 Interactive Loop *(future)* | After solution selection, an interactive session begins. The system opens with the Open Questions from the documentation package and asks the user to resolve each one. The user may add additional context, preferences, or constraints at any time. During implementation the system surfaces new questions as they arise rather than making silent assumptions. Each implementation artifact can be reviewed and the user can provide corrections or redirections before the next step proceeds. The interaction history is persisted alongside the idea. | P2 |
| F18 | Web Search Tool | Tool available to deep-analysis pipeline models: model issues a search query, backend fetches results via DuckDuckGo (default) or Tavily (if API key configured), results returned as tool call response. Enables models to verify library/component existence, current best practices, and compatibility. Searches logged in audit trail. | P1 |
| F19 | File Editing Tool | Tool that allows any model to make change to files by searching and replacing or inserting content into specific location | P1 |

### Analysis Pipeline (per Solution Branch)

Each solution branch is an independent pipeline instance. Multiple branches per idea run in parallel.

| Stage | Name | Description | Exit Criteria |
|-------|------|-------------|---------------|
| 0 | Intake | Parse idea + any inherited failure context from parent branch into structured object | Structured solution context created |
| 1 | Feasibility Scan | High-level blocker check for this specific solution path | Pass → S2 / Fail → branch FAILED, failure analysis triggered |
| 2 | Solution Design | Design this solution approach in detail | Solution design document produced |
| 3 | Solution Analysis | Assess fit, risk, resource needs against original requirements | Risks catalogued; proceed or fail |
| 4 | Decomposition | Break solution into component tree with dependencies | Full component tree produced |
| 5 | Component Validation | Validate each component individually | All P0 components validated; any UNACHIEVABLE → branch FAILED |
| 6 | Deep Review | Holistic review: gaps, conflicts, unmet requirements | VIABLE → trigger Stage 7 / FAILED → failure analysis triggered |
| 7 | Documentation | Generate full implementation documentation package | All 7 doc artifacts produced |
| — | **Human Review & Selection** | *(Not a pipeline stage — a deliberate stopping point.)* The system presents all VIABLE solutions to the user. The user reviews documentation packages, compares approaches, and selects one solution to develop further. No automated action follows until the user acts. | User selects a solution (F17) → idea enters SELECTED state; Phase 2 begins from the chosen solution's documentation |

### Branch Lifecycle and Spawning Logic

```
Idea submitted
      │
      ▼
Orchestrator spawns N initial solution branches (N = 2–3)
      │
      ├─── Branch A ──► stages 0→7 ──► VIABLE ──► docs generated
      │
      ├─── Branch B ──► stage 3 FAILED
      │                      │
      │              Failure Analysis:
      │              "failed because X, suggests path Y is unexplored"
      │                      │
      │              ┌───────▼────────┐
      │              │ New paths exist?│
      │              └───────┬────────┘
      │                  yes │        no
      │                      │         └──► update convergence tracker
      │              Spawn Branch C        check: any paths left overall?
      │              (inherits failure         │
      │               context from B)      if no → idea CONVERGED (no viable solution found)
      │                                    if yes → wait for other branches
      │
      └─── Branch D ──► stage 5 FAILED ──► (same spawn logic)

Termination conditions:
  1. User clicks Abandon → all branches cancelled, idea ABANDONED
  2. Convergence: no active branches + no new paths identified → idea CONVERGED (no viable solution found)
  3. At least one VIABLE branch → idea CONVERGED (with viable solutions); docs available; system waits for user selection

After condition 3, the user reviews viable solutions and selects one → idea SELECTED → Phase 2 begins.
```

### Documentation Output Package (F9)
Produced per viable solution branch (Stage 7). Multiple viable branches = multiple doc packages.
- **Executive Summary** — problem, solution chosen, key decisions
- **Architecture Overview** — component diagram (Mermaid for MVP; structured spatial data embedded for future 2D/3D rendering)
- **Component Specifications** — per-component: purpose, inputs/outputs, dependencies, risks
- **Requirements Traceability** — map each original requirement to satisfying component(s)
- **Risk Register** — identified risks, likelihood, impact, mitigation
- **Implementation Roadmap** — ordered milestones with dependencies
- **Open Questions** — unresolved items requiring human decision before implementation

> **Rendering note**: Architecture Overview documents will embed a structured component graph (JSON) alongside the Mermaid diagram. This data is unused in MVP but provides the schema needed for future 2D/3D rendering (React Three Fiber for physical device ideas, D3/Mermaid for software architecture). The markdown renderer displays the Mermaid block; the JSON block is hidden but present.

---

## 6. UI Design Guidelines

### Design Philosophy

Clean, minimalistic, modern, and professional. Each screen surfaces the most relevant information at a glance while providing clear pathways to drill deeper. Navigation follows a hierarchical resource model — every list item is an entry point into a detail view with contextual actions. No unnecessary chrome; content drives the layout.

### Navigation Model

Routes follow a RESTful resource hierarchy, reflecting the idea → solution → documentation ownership chain:

| Route | Screen |
|-------|--------|
| `/` | Ideas dashboard — all ideas |
| `/ideas/new` | Submit new idea |
| `/ideas/{id}` | Idea detail — solutions list, status, actions |
| `/ideas/{id}/solutions/{sid}` | Solution detail — stage pipeline, reasoning, verdicts |
| `/ideas/{id}/solutions/{sid}/docs` | Documentation package viewer |

### Screen Designs

#### Main Screen: Ideas Dashboard (`/`)
- List of all ideas: name, status badge, submission date, one-line description
- Status badge communicates state at a glance: `RUNNING` · `VIABLE` · `CONVERGED` · `ABANDONED`
- Sort by status (active first) and date
- Single primary action: `+ New Idea`
- Empty state explains what happens after submission

#### Idea Detail (`/ideas/{id}`)
- Header: idea name, status badge, submission date, full description
- Primary actions (contextual — only shown when valid for current status):
  - `Abandon` (when RUNNING)
  - `Pause` / `Resume` (when RUNNING / PAUSED)
  - `Add Context` (when RUNNING)
- Solution list: each branch as a row/card showing solution ID, stage progress indicator (0–7), status badge, last updated timestamp
- Click any solution row → Solution Detail

#### Solution Detail (`/ideas/{id}/solutions/{sid}`)
- Breadcrumb: `Ideas → {idea name} → Solution {sid}`
- Header: solution approach summary, status badge, spawned-from lineage (if applicable)
- Stage pipeline: horizontal step indicator (0 → 7) with per-stage status — completed, active, failed, pending
- Per-stage expandable section: model used, structured output, verdict, reasoning summary
- If VIABLE: prominent link to Documentation Package
- If FAILED: failure reason card + list of child branches spawned from this failure (linked)

#### Documentation Viewer (`/ideas/{id}/solutions/{sid}/docs`)
- Left sidebar: section navigation across all 7 documentation artifacts
- Main content: rendered markdown with inline Mermaid diagrams
- All artifacts accessible from one page — no separate routes per artifact

### Visual Language

| Element | Guideline |
|---------|-----------|
| Typography | Clean sans-serif; hierarchy through weight and size, not color |
| Color | Neutral base (gray/white/off-white); single accent for primary actions; semantic colors for status only: green = viable, red = failed, blue = running, gray = pending/abandoned |
| Layout | Content-first; sidebar only where orientation requires it (doc viewer); breadcrumbs on all detail pages |
| Cards & lists | Cards for solution branches; table rows for idea lists; badges for all status values |
| Spacing | Generous whitespace — information-dense but never cramped |
| Disclosure | Expandable sections for stage reasoning; no modal-heavy interactions |

### Interaction Principles

- **Drill-down via routes, not modals** — detail views are full pages with their own URLs; overlays only for brief confirmations (e.g., abandon confirmation)
- **Breadcrumb on all non-root pages** for orientation within the resource hierarchy
- **Live updates via WebSocket** — stage progress and status badges update in place without page refresh
- **Contextual actions** — only render actions valid for the current resource state; don't show Abandon on a CONVERGED idea
- **Informative empty states** — every empty list explains what will appear there and how to trigger it

---

## 7. Non-Functional Requirements

| Category | Requirement |
|----------|-------------|
| Execution | All inference runs locally; zero network calls required for analysis |
| Hardware | Primary inference on NVIDIA RTX GPU; secondary tasks on Intel Core Ultra NPU |
| Models | Open-source models only; small/fine-tuned models preferred over large general-purpose |
| OS | Windows 11 primary target |
| Persistence | All ideas, solution branches, stage results, and documents persisted locally (survive restarts) |
| Concurrency | Multiple solution branches per idea run in parallel; multiple ideas may be active simultaneously; system does not starve |
| Resource management | VRAM budget respected; branch tasks queued when GPU is saturated |
| UI responsiveness | Web UI remains responsive during all background analysis |
| Auditability | Every model call logged with inputs, outputs, model identity, and branch context |
| Rendering | MVP: rendered markdown + Mermaid diagrams. Architecture: document format forward-compatible with 2D/3D rendering (embedded structured JSON) |

---

## 8. Technical Considerations

### Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    Web UI (Browser)                  │
│         Idea submission · Status dashboard           │
│              Audit trail · Doc viewer                │
└─────────────────────┬───────────────────────────────┘
                      │ HTTP / WebSocket
┌─────────────────────▼───────────────────────────────┐
│                  Backend API Server                  │
│        REST endpoints · WebSocket events             │
│              Job queue · State store                 │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│               Orchestrator Agent                     │
│   Pipeline controller · Stage manager               │
│   Task dispatcher · Verdict engine                  │
└────┬──────────────┬────────────────┬────────────────┘
     │              │                │
┌────▼────┐   ┌─────▼─────┐   ┌─────▼──────┐
│Feasibil.│   │ Solution  │   │Validation  │
│ Model   │   │ Gen Model │   │ Model      │   ...
│(fine-   │   │(fine-     │   │(fine-      │
│tuned)   │   │tuned)     │   │tuned)      │
└────┬────┘   └─────┬─────┘   └─────┬──────┘
     └──────────────┴────────────────┘
                      │
         ┌────────────▼──────────────┐
         │   Local Inference Runtime  │
         │  (llama.cpp / Ollama /     │
         │   ONNX Runtime / vLLM)    │
         │   RTX GPU + Intel NPU     │
         └───────────────────────────┘
                      │
         ┌────────────▼──────────────┐
         │     Local Storage         │
         │  Ideas · Stages · Docs    │
         │  Logs · Model registry    │
         └───────────────────────────┘
```

### Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Inference runtime | **Ollama** (primary) + llama.cpp server (escape hatch) | Native Windows CUDA, OpenAI-compatible REST API, built-in model management, GGUF fine-tuned model support via Modelfile; llama.cpp available for low-level control when needed |
| Model format | **GGUF** | Universal format across Ollama and llama.cpp; quantized variants (Q4_K_M) fit specialist models within VRAM budget |
| NPU usage | **Post-v1 optimization** | RTX GPU handles all inference in v1. NPU targeted for lightweight specialist models (1–3B) post-M4: parallel execution while GPU runs orchestrator, low-power background validation, fast Stage 1 pre-screening. Requires OpenVINO IR/ONNX conversion — separate toolchain from Ollama. **Architecture must be backend-agnostic from day one** to make this a config change, not a refactor. |
| Agent / orchestration | **Custom Python loop** (start); LangGraph if complexity demands it | Pipeline is a defined stage machine — a lightweight REST-calling loop is cleaner and more debuggable than a framework for this use case |
| Backend language | **Python** | Best ML/model ecosystem; straightforward Ollama REST integration |
| Web framework | **FastAPI + React** | FastAPI for Python backend API; React for frontend (familiarity) |
| Persistence | **SQLite** | Sufficient for solo use; stores ideas, stage state, analysis results, audit logs |
| Model communication | **Backend-agnostic inference interface** | Each stage declares `model` + `backend` in YAML config. Backend drivers: `ollama` (GPU, default), `openvino` (NPU, post-v1), `llamacpp` (escape hatch). Orchestrator calls a unified `InferenceClient` that routes accordingly — swapping a model or backend is a config change, not a code change. |

### Model Architecture
- One **Orchestrator** model: general reasoning, pipeline control, verdict synthesis. Starting model: **Phi-4 (14B)** or **Qwen2.5-14B** at Q4_K_M quantization (~8–9GB VRAM). Both strong at multi-step structured reasoning. Runs on GPU via Ollama.
- Multiple **Specialist** models (fine-tuned or prompted): feasibility analysis, solution generation, decomposition, validation, documentation writing. Start with prompted specialists using the orchestrator model; fine-tune iteratively as pipeline output data accumulates.
- Target model size per specialist: **3B–7B** on GPU (VRAM headroom for parallel loading); **1–3B** on NPU post-v1 for lightweight validation and pre-screening tasks.
- All model routing goes through a unified `InferenceClient` abstraction:

```yaml
# models.yaml — stage → model + backend mapping
stages:
  feasibility_scan:
    model: phi4:14b
    backend: ollama        # GPU — complex reasoning
  solution_generation:
    model: phi4:14b
    backend: ollama        # GPU — generative
  component_validator:
    model: qwen2.5:3b      # GPU now → swap to openvino post-v1
    backend: ollama
  deep_review:
    model: phi4:14b
    backend: ollama        # GPU — synthesis
  documentation:
    model: qwen2.5:7b
    backend: ollama        # GPU — structured writing
```

- Adding NPU support post-v1 = add `openvino` driver to `InferenceClient` + convert target models to OpenVINO IR. No pipeline code changes required.

### Python Script Runner Tool (F16)

Models in any pipeline stage can invoke a `run_python` tool call. The backend executes the script in a sandboxed subprocess and returns the result to the model as a structured tool response.

**Interface** (exposed as an Ollama/OpenAI-compatible tool definition):
```json
{
  "name": "run_python",
  "description": "Execute a Python script and return stdout, stderr, and exit code.",
  "parameters": {
    "script": "string — Python source to execute"
  }
}
```

**Execution constraints:**
- Subprocess timeout: configurable, default **30 s**
- No network access: outbound calls blocked at the subprocess level (`socket` module disabled via `sys.modules`)
- No filesystem writes outside a per-call temp directory (cleaned up after execution)
- `subprocess`, `os.system`, and shell-escape vectors blocked via a pre-execution AST check
- stdout + stderr capped at **64 KB** before being returned to the model

**Typical use cases during analysis:**
- VRAM / memory footprint estimates (`model_size_gb * overhead_factor`)
- Parsing structured data (JSON, CSV) embedded in the idea description or stage outputs
- Complexity or performance calculations (`O(n²)` vs `O(n log n)` crossover points)
- Dependency resolution graphs or topological sort validation

**Audit trail:** every `run_python` call (script, stdout, stderr, exit code, duration) is persisted alongside the stage's model call log so the full reasoning chain — including computed evidence — is auditable.

### File Editing Tool  (F19)
**Requirement:** Due to the nature of Phase 3, multiple background agents (Code Generator, Doc Writer, Asset Converter) will attempt to write and modify files simultaneously.
**Constraint:** A centralized, **Process-Level File Lock Manager** must be implemented in the backend to serialize file write access.
**Protocol:** Agents must attempt to acquire an exclusive lock on a target file path before writing. If the lock cannot be acquired (i.e., another agent holds the lock), the agent must wait for a backoff period and retry, rather than failing the operation. The `FileManagerService` will be responsible for managing this resource.

### Dependencies
- Local GPU inference runtime (TBD)
- Local database / file store
- Web server / API layer
- No external APIs required at runtime

---

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Small models lack reasoning depth for complex ideas | High | High | Fine-tune on domain-specific reasoning datasets; allow model swap per stage via config |
| VRAM exhaustion when running multiple ideas | Medium | High | Queue system with per-idea resource budgets; serialize GPU tasks |
| Analysis pipeline produces inconsistent or circular reasoning | Medium | High | Stage gate validation; Ollama `format: json` enforces structured output at API level |
| Intel NPU driver/runtime immaturity on Windows (post-v1) | Medium | Low | NPU is optional accelerator only; all tasks have GPU fallback; not on critical path for v1 |
| OpenVINO model conversion friction (post-v1) | Medium | Low | Isolated to `InferenceClient` driver — pipeline code unaffected; tackle per-model as needed |
| Fine-tuning quality insufficient without significant data | High | Medium | Start with prompted specialist agents; fine-tune iteratively as pipeline output data accumulates |
| Verdict accuracy — wrong unachievable calls | Medium | High | Require multi-stage evidence for unachievable verdict; user can override at any stage |

---

## 10. Milestones & Timeline

| Milestone | Description |
|-----------|-------------|
### Phase 1 — Analysis & Selection

| Milestone | Description | Status |
|-----------|-------------|--------|
| M0 | Project setup: repo, tooling, chosen tech stack confirmed | ✅ Done |
| M1 | Local inference runtime running; hello-world model call from backend | ✅ Done |
| M2 | Orchestrator skeleton: stage machine, idea persistence, basic API | ✅ Done |
| M3 | Stage 1–2 working end-to-end: intake → feasibility → solution generation | ✅ Done |
| M4 | Full pipeline Stage 1–6: decomposition + validation + deep review | ✅ Done |
| M5 | Web UI: idea submission, status dashboard, audit trail | ✅ Done |
| M6 | Documentation package generation (Stage 7) | ✅ Done |
| M7 | Resource management: queuing, pause/resume, VRAM budgeting | ⚠️ Partial |
| M8 | Polish + dogfooding: real ideas run through the pipeline; output quality validated | 🔄 In progress |
| M9 | Solution selection: user marks a viable solution as Selected; SELECTED state, selection UI | ⬜ Not started |

### Phase 2 — Implementation Assistance *(future)*

| Milestone | Description | Status |
|-----------|-------------|--------|
| M10 | Phase 2 kickoff: interactive resolution of Open Questions from selected solution's docs; user provides additional context and constraints before implementation begins | ⬜ Future |
| M11 | Software implementation: code generation from resolved docs; scaffolding, tests, runnable app; user reviews and redirects each step | ⬜ Future |
| M12 | Physical implementation: engineering artifact generation; component diagrams, CAD/3D-printable assets, BOM, assembly instructions; user reviews each artifact | ⬜ Future |
| M13 | Refinement loop: user feedback incorporated across multiple implementation rounds; persistent interaction history per idea | ⬜ Future |

---

## 11. Open Questions

- [x] **Inference runtime**: Ollama (primary) + llama.cpp server (escape hatch). OpenAI-compatible REST API. GGUF format.
- [x] **NPU**: Deferred — RTX GPU handles all inference. Revisit when OpenVINO NPU LLM support matures on Windows.
- [x] **Orchestrator model**: Start with Phi-4 (14B) or Qwen2.5-14B at Q4_K_M. Prompted specialists first; fine-tune later.
- [x] **Orchestration pattern**: Custom Python loop calling Ollama REST API. LangGraph only if pipeline complexity demands it.
- [x] **Structured output enforcement**: Ollama `format: json` enforces JSON output at the API level — no retry/validation layer needed for v1.
- [x] **Model registry**: YAML config file mapping `stage_name → ollama_model_name`. Swapping a specialist = one line change.
- [x] **UI framework**: FastAPI (backend) + React (frontend).
- [x] **Solution termination**: No single "unachievable" threshold. Individual branches fail on their own evidence. The idea converges when: (a) user abandons, or (b) no active branches remain and failure analysis finds no new unexplored paths. Convergence = solution space exhausted.
- [x] **Documentation format**: MVP renders markdown + Mermaid diagrams. Architecture Overview embeds a structured JSON component graph (hidden in MVP) for future 2D/3D rendering via React Three Fiber for physical device ideas.
- [x] **Initial branch count**: 2 branches at idea submission. Balances parallel exploration against VRAM pressure. Additional branches spawn only from failure analysis.

---

## Revision History

| Version | Date | Author | Summary |
|---------|------|--------|---------|
| 1.0 | 2026-04-15 | Solo | Initial draft |
| 1.1 | 2026-04-15 | Solo | Added Section 6: UI Design Guidelines — navigation model, screen designs, visual language, interaction principles |
| 1.2 | 2026-04-15 | Solo | Added F16: Python Script Runner tool — sandboxed execution available to all pipeline models for grounded calculation and data parsing |
| 1.3 | 2026-04-16 | Solo | Captured two-phase vision: Phase 1 = analysis & selection (current), Phase 2 = implementation assistance (future, software → working app, hardware → CAD/3D artifacts). Added deliberate human review/selection stopping point between phases. Added F17 (Solution Selection), F18 (Web Search Tool). Moved code generation from Non-Goal to deferred Phase 2 scope. Updated milestones to reflect phase structure and current status. |
| 1.5 | 2026-04-16 | Solo | Revisions Phases 1, 2 and 3 to establish a clear three-phase workflow: Analysis $\\to$ Open Question Resolution $\\to$ Implementation. Added Phase 3 focus area and detailed initial plans for agent integration. |

# Think Tank — Progress & TODO

**Last updated**: 2026-04-17 (session 11)
**Session summary**: Phase 3 iteration agent given file exploration tools (list_files, read_file, grep_files) via call_with_tools explore_only mode — model navigates the project before planning changes instead of receiving all file contents upfront. PRD always generated as first file (docs/PRD.md) with dedicated phase3_prd stage using full Phase 1 + Phase 2 context. Theme changed to burnt orange. README added. backend/data excluded from git.

---

## Milestone Status

| # | Milestone | Status | Notes |
|---|-----------|--------|-------|
| M0 | Project setup — repo, tooling, stack confirmed | ✅ Done | |
| M1 | Ollama running, hello-world model call from backend | ✅ Done | All 3 models pulled |
| M2 | Orchestrator, stage machine, idea persistence, API | ✅ Done | Full implementation |
| M3 | Stages 0–2 end-to-end | ✅ Done | All 8 stages implemented |
| M4 | Full pipeline stages 0–6 | ✅ Done | Both branches ran to VIABLE |
| M5 | Web UI — submission, dashboard, audit trail | ✅ Done | Full React UI built and verified |
| M6 | Documentation package generation (Stage 7) | ✅ Done | 7 docs per viable branch |
| M7 | Resource management — queuing, pause/resume, VRAM | ⚠️ Partial | Semaphore + branch cap done; pause/resume wired but untested |
| M8 | Polish + dogfooding Think Tank with Think Tank | ❌ Not started | |
| M9 | Solution selection: SELECTED state, selection UI, notes | ✅ Done | |
| M10 | Phase 2 kickoff: interactive Q&A session, Open Questions resolution | ✅ Done | |
| M11 | Phase 3 software implementation: manifest generation, file write, file browser | ⚠️ Partial | Multi-pass + iteration loop implemented; untested end-to-end |

---

## What's Working

### Backend (fully implemented)
- **FastAPI server** — starts, DB initialises, CORS configured
- **SQLite schema** — tables: `ideas`, `solution_branches`, `stage_results`, `failure_analyses`, `model_calls`, `documents`, `phase2_sessions`, `phase2_messages`, `phase3_sessions`, `phase3_activity`
- **InferenceClient** — backend-agnostic routing via `models.yaml`; `call()`, `call_text()`, `call_with_tools()`; Ollama driver
- **All 8 pipeline stages** — s0 intake → s7 documentation
- **Orchestrator** — spawns 2 initial branches, manages asyncio task pool
- **Phase 2** — interactive Q&A loop, resolution summary, READY state
- **Phase 3** — session creation, multi-pass file generation, iteration chat loop, file exploration tools (list_files/read_file/grep_files), always-generated docs/PRD.md, shell commands, activity events, WebSocket events, file browser endpoints

### Frontend (working)
- Ideas dashboard with sort (active first) and section grouping
- Idea detail with hierarchical branch tree view
- Solution detail with stage pipeline
- Document viewer with Mermaid rendering
- Phase nav breadcrumbs (1. Analysis › 2. Q&A › 3. Build)
- Smart redirect to deepest active phase
- Phase 2 chat with typing indicators and streaming
- Phase 3 activity log + Files tab with file browser
- Audit trail with call type filter

---

## Known Issues — Phase 3

### High Priority
1. **Iteration loop untested end-to-end** — Tool-calling exploration loop (list_files/read_file/grep_files → JSON plan) implemented but not yet validated with qwen2.5:7b on a real project.

2. **File quality unknown** — Multi-pass generation implemented but untested end-to-end with phi4:14b. JSON plan format and per-file prompts may need tuning.

3. **No failed-file fallback in UI** — If a file fails to write, there is no visual indicator in the activity log (only the `✓` count in the summary differs from plan count).

---

## TODO — Next Session

### 1. ~~Phase 3: Multi-pass structured generation~~ ✅ DONE

### 2. Phase 3: Iteration chat loop ✅ DONE

Backend: Phase3Message model, POST /ideas/{id}/phase3/messages, run_iteration() with tool-calling exploration (list_files/read_file/grep_files via phase3_explore stage on qwen2.5:7b). Frontend: chat input, optimistic messages, thinking indicator, markdown rendering in assistant bubbles, correct event ordering on reload, stop button during iteration.

### 3. Phase 3: UI improvements

- [ ] Show current pass name during generation ("Writing src/main.py…")  
- [ ] Group activity log by pass (Pass 1: Scaffolding, Pass 2: Core modules, etc.)
- [ ] After FAILED with empty directory, show audit trail link so user can see what the model actually output
- [ ] File browser: syntax highlighting (prism-react-renderer or highlight.js)
- [ ] File browser: copy file content button
- [ ] File browser: open in VS Code button (`vscode://file/path`)
- [ ] Show total project size in completion panel

### 4. Phase 3: Models.yaml restructure ✅ DONE

Stages added: phase3_plan, phase3_file, phase3_prd, phase3_explore, phase3_iteration.

### 6. Phase 3: PRD generation ✅ DONE

docs/PRD.md always generated as first file using dedicated phase3_prd stage (phi4:14b, 4096 tok). Pulls all Phase 1 docs + Phase 2 resolution summary + file plan. Structured into 9 sections so the project is self-contained for external tools.

### 5. Polish / M8

- [ ] Run a real idea end-to-end (Phase 1 → 2 → 3) with multi-pass generation
- [ ] Verify Phase 1 document quality (no generic filler, concrete architecture)
- [ ] Test pause/resume round-trip
- [ ] Verify branch cap convergence

---

## How to Start the Stack

```bash
# Both API + UI together (from repo root)
npm run dev

# Or individually
npm run dev:api   # FastAPI on :8000
npm run dev:ui    # Vite on :5173

# Health check
curl http://127.0.0.1:8000/api/v1/health
# → {"status":"ok","ollama_reachable":true,"ollama_models":["qwen2.5:3b","qwen2.5:7b","phi4:14b"]}
```

---

## Key Files

| File | Purpose |
|------|---------|
| `backend/app/main.py` | FastAPI app factory + lifespan |
| `backend/app/pipeline/orchestrator.py` | Branch pool management, spawn logic, convergence detection |
| `backend/app/pipeline/runner.py` | Per-branch stage loop, pause/cancel mechanics |
| `backend/app/agents/code_generator_agent.py` | Phase 3 manifest → file write agent |
| `backend/app/api/phase3.py` | Phase 3 REST endpoints + file browser |
| `backend/app/inference/client.py` | InferenceClient — all model calls |
| `backend/app/db/models.py` | All ORM entities |
| `backend/models.yaml` | Stage → model + backend routing |
| `frontend/src/components/Phase3Implementation.tsx` | Phase 3 UI: activity log, file browser |
| `frontend/src/components/IdeaDetail.tsx` | Idea detail with branch tree |
| `PRD.md` | Full product requirements |

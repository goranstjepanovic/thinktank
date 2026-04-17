# Think Tank — Progress & TODO

**Last updated**: 2026-04-17 (session 10)
**Session summary**: Phase 3 rewritten to multi-pass generation. Pass 0 = JSON file plan (phase3_plan stage). Pass 1-N = one call_text() per file (phase3_file stage). New events: plan_ready, pass_started. Frontend shows file plan count + live "Writing X... N/M" indicator that replaces itself on each file. Activity log now populated throughout generation.

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
| M11 | Phase 3 software implementation: manifest generation, file write, file browser | ⚠️ Partial | Multi-pass generation implemented; iteration loop missing |

---

## What's Working

### Backend (fully implemented)
- **FastAPI server** — starts, DB initialises, CORS configured
- **SQLite schema** — tables: `ideas`, `solution_branches`, `stage_results`, `failure_analyses`, `model_calls`, `documents`, `phase2_sessions`, `phase2_messages`, `phase3_sessions`, `phase3_activity`
- **InferenceClient** — backend-agnostic routing via `models.yaml`; `call()`, `call_text()`, `call_with_tools()`; Ollama driver
- **All 8 pipeline stages** — s0 intake → s7 documentation
- **Orchestrator** — spawns 2 initial branches, manages asyncio task pool
- **Phase 2** — interactive Q&A loop, resolution summary, READY state
- **Phase 3** — session creation, manifest generation call, file writing, shell commands, activity events, WebSocket events, file browser endpoints

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
1. **No iteration loop** — After Phase 3 completes, there is no way for the user to request changes, additions, or fixes. The implementation is a dead end with no feedback cycle.

2. **File quality unknown** — Multi-pass generation is implemented but untested end-to-end with phi4:14b. The JSON plan format and per-file prompts may need tuning.

3. **No failed-file fallback in UI** — If a file fails to write, there is no visual indicator in the activity log (only the `✓` count in the summary differs from plan count).

---

## TODO — Next Session

### 1. ~~Phase 3: Multi-pass structured generation~~ ✅ DONE

### 2. Phase 3: Iteration chat loop

After COMPLETE (or even FAILED), allow user to send messages to continue/fix:

**Backend:**
- Add `Phase3Message` model (session_id, role, content, created_at)
- `POST /ideas/{id}/phase3/messages` — user sends a message; triggers a new generation pass
- Pass includes: current file tree (paths + sizes), previous summary, user message, spec
- Agent generates: targeted file changes or new files (same multi-pass approach)
- New activity events emitted, session stays COMPLETE

**Frontend:**
- Add chat input below the completion panel in Phase3Implementation
- Each user message triggers a loading state → new file_written events appear in activity log
- History of messages shown above input (collapsible)

### 3. Phase 3: UI improvements

- [ ] Show current pass name during generation ("Writing src/main.py…")  
- [ ] Group activity log by pass (Pass 1: Scaffolding, Pass 2: Core modules, etc.)
- [ ] After FAILED with empty directory, show audit trail link so user can see what the model actually output
- [ ] File browser: syntax highlighting (prism-react-renderer or highlight.js)
- [ ] File browser: copy file content button
- [ ] File browser: open in VS Code button (`vscode://file/path`)
- [ ] Show total project size in completion panel

### 4. Phase 3: Models.yaml restructure ✅ DONE (phase3_plan + phase3_file added; phase3_iteration deferred to iteration loop task)

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

# Think Tank — Progress & TODO

**Last updated**: 2026-04-17 (session 12)
**Session summary**: Major Phase 3 agent reliability pass. Agent now explores project before running commands, background shell process support added (run_shell_background / get_shell_output / stop_shell_process), chat history threaded into iteration calls, Windows-specific fixes (no Makefile, chained command blocking, npm parent-package detection). Fixed empty file writes, JSON fence parsing (brace on fence line), insert_lines removed. File browser gets react-icons file type icons. Configurable implementations directory added: Settings page, settings API, user_settings service, move-with-skip-dev-dirs. Agent web search added for package version verification — plan stage now uses call_with_tools; all tool-enabled prompts instruct web_search before picking or fixing packages.

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
| M11 | Phase 3 software implementation: manifest generation, file write, file browser | ⚠️ Partial | Multi-pass + iteration loop + agent tooling implemented; needs real-world validation |

---

## What's Working

### Backend (fully implemented)
- **FastAPI server** — starts, DB initialises, CORS configured
- **SQLite schema** — tables: `ideas`, `solution_branches`, `stage_results`, `failure_analyses`, `model_calls`, `documents`, `phase2_sessions`, `phase2_messages`, `phase3_sessions`, `phase3_activity`
- **InferenceClient** — backend-agnostic routing via `models.yaml`; `call()`, `call_text()`, `call_with_tools()`; Ollama driver
- **All 8 pipeline stages** — s0 intake → s7 documentation
- **Orchestrator** — spawns 2 initial branches, manages asyncio task pool
- **Phase 2** — interactive Q&A loop, resolution summary, READY state
- **Phase 3** — session creation, multi-pass file generation, iteration chat loop, file exploration tools (list_files/read_file/grep_files), always-generated docs/PRD.md, shell commands (foreground + background), background process manager, activity events, WebSocket events, file browser endpoints
- **Settings** — configurable implementations directory (user_settings service + JSON override), Settings page UI, move-implementations endpoint with dev-dir skip (node_modules, .venv, __pycache__, etc.)
- **Agent reliability** — exploration-first prompts, chat history threading, Windows-aware command guidance, empty-write guard, JSON fence fix, insert_lines removed, round limits raised (40/50)

### Frontend (working)
- Ideas dashboard with sort (active first) and section grouping
- Idea detail with hierarchical branch tree view
- Solution detail with stage pipeline
- Document viewer with Mermaid rendering
- Phase nav breadcrumbs (1. Analysis › 2. Q&A › 3. Build)
- Smart redirect to deepest active phase
- Phase 2 chat with typing indicators and streaming
- Phase 3 activity log + Files tab with file browser (file-type icons via react-icons)
- Settings page: configurable implementations directory, move-with-skip dialog
- Audit trail with call type filter

---

## Known Issues — Phase 3

### High Priority
1. **End-to-end validation needed** — Full pipeline (Phase 1 → 2 → 3) not yet run on a real project. Agent reliability fixes implemented but not validated in practice with the Gemma4 model.

2. ~~**No failed-file indicator in UI**~~ ✅ Fixed — `file_failed` event emitted from backend and displayed as red ✗ with error detail in activity log.

3. **plan stage tools untested** — `phase3_plan` now uses `call_with_tools` with web_search enabled. phi4:14b tool-calling behaviour with the new prompt needs real-world validation.

---

## TODO — Next Session

### 1. ~~Phase 3: Multi-pass structured generation~~ ✅ DONE

### 2. Phase 3: Iteration chat loop ✅ DONE

Backend: Phase3Message model, POST /ideas/{id}/phase3/messages, run_iteration() with tool-calling exploration (list_files/read_file/grep_files via phase3_explore stage on qwen2.5:7b). Frontend: chat input, optimistic messages, thinking indicator, markdown rendering in assistant bubbles, correct event ordering on reload, stop button during iteration.

### 3. Phase 3: UI improvements

- [ ] Show current pass name during generation ("Writing src/main.py…")  
- [ ] Group activity log by pass (Pass 1: Scaffolding, Pass 2: Core modules, etc.)
- [ ] After FAILED with empty directory, show audit trail link so user can see what the model actually output
- [x] File browser: syntax highlighting via highlight.js (github-dark theme, 14 languages registered) ✅
- [x] File browser: copy file content button ✅
- [x] File browser: open in VS Code button (`vscode://file/path`) ✅
- [x] Show total project size (file count + total KB/MB in file tree sidebar) ✅

### 7. Phase 3: Agent — remaining improvements

- [ ] Parallel tool execution — extract dispatch into `_dispatch_tool()`, use `asyncio.gather` for independent tool calls within a round (deferred, agreed with user)
- [ ] Validate phi4:14b web_search behaviour in planning stage with a real project

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

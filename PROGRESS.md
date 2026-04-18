# Think Tank — Progress & TODO

**Last updated**: 2026-04-18 (session 13)
**Session summary**: Phase 3 reliability and observability pass. Chunked PRD generation (9 section calls) prevents silent failures from output token limits. Per-section doc targeting fixes context-window overflow on local models — each section only receives the 1-2 Phase 2 docs it actually needs. Post-generation verification pass: after all files are written, a tool-assisted agent reads entry points, checks imports/exports/cross-file consistency, and fixes real bugs before setup commands run. Exploration tool activity (list_files, read_file, grep_files, web_search) now visible in Phase 3 chat UI. Regenerate-PRD endpoint + button added as a recovery path. Files tab shown during generation (not just COMPLETE), with debounced live refresh on file_written events.

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
| M7 | Resource management — queuing, pause/resume, VRAM | ⚠️ Partial | Semaphore + branch cap done; pause/resume wired, untested |
| M8 | Polish + dogfooding Think Tank with Think Tank | ❌ Not started | |
| M9 | Solution selection: SELECTED state, selection UI, notes | ✅ Done | |
| M10 | Phase 2 kickoff: interactive Q&A session, Open Questions resolution | ✅ Done | |
| M11 | Phase 3 software implementation: manifest generation, file write, file browser | ⚠️ Partial | Multi-pass + iteration + agent tooling + verification pass implemented; real-world validation pending |

---

## What's Working

### Backend (fully implemented)
- **FastAPI server** — starts, DB initialises, CORS configured
- **SQLite schema** — tables: `ideas`, `solution_branches`, `stage_results`, `failure_analyses`, `model_calls`, `documents`, `phase2_sessions`, `phase2_messages`, `phase3_sessions`, `phase3_activity`
- **InferenceClient** — backend-agnostic routing via `models.yaml`; `call()`, `call_text()`, `call_with_tools()`; Ollama driver
- **All 8 pipeline stages** — s0 intake → s7 documentation
- **Orchestrator** — spawns 2 initial branches, manages asyncio task pool
- **Phase 2** — interactive Q&A loop, resolution summary, READY state
- **Phase 3** — session creation, multi-pass file generation, iteration chat loop, file exploration tools (list_files/read_file/grep_files), always-generated docs/PRD.md (chunked, section-targeted), post-generation verification pass, shell commands (foreground + background), background process manager, activity events, WebSocket events, file browser endpoints, regenerate-PRD endpoint
- **Settings** — configurable implementations directory (user_settings service + JSON override), Settings page UI, move-implementations endpoint with dev-dir skip
- **Agent reliability** — exploration-first prompts, chat history threading, Windows-aware command guidance, empty-write guard, JSON fence fix, insert_lines removed, round limits raised (40/50), per-section doc targeting for PRD

### Frontend (working)
- Ideas dashboard with sort (active first) and section grouping
- Idea detail with hierarchical branch tree view
- Solution detail with stage pipeline
- Document viewer with Mermaid rendering
- Phase nav breadcrumbs (1. Analysis › 2. Q&A › 3. Build)
- Smart redirect to deepest active phase
- Phase 2 chat with typing indicators and streaming
- Phase 3 activity log with exploration tool visibility (list_files/read_file/grep_files/web_search shown as transient indicators)
- Phase 3 Files tab available during generation (appears after first file written, debounced live refresh)
- File browser: file-type icons, syntax highlighting, copy button, VS Code link, failed-file indicator, total size
- Regenerate PRD button (visible when COMPLETE)
- Settings page: configurable implementations directory, move-with-skip dialog
- Audit trail with call type filter

---

## Known Issues — Phase 3

### High Priority
1. **End-to-end validation needed** — Full pipeline (Phase 1 → 2 → 3) not yet run on a real project. Agent reliability fixes implemented but not validated in practice.

2. **plan stage tools untested** — `phase3_plan` now uses `call_with_tools` with web_search enabled. phi4:14b tool-calling behaviour with the new prompt needs real-world validation.

3. **Verification pass quality** — The `_verify_and_fix_files` agent uses `phase3_explore` (gemma4) which may not be strong enough for deep cross-file analysis on large projects. May need its own stage config.

---

## TODO — Next Session

### Phase 3: Real-world validation
- [ ] Run a full pipeline (Phase 1 → 2 → 3) on a real project idea
- [ ] Verify PRD sections are well-formed and use correct docs
- [ ] Check verification pass actually catches and fixes import errors
- [ ] Validate phi4:14b tool-calling in plan stage (web_search for package versions)

### Phase 3: Remaining UI / UX
- [ ] Activity log grouping by phase (Generation / Verification / Commands) — section dividers
- [ ] Show current writing progress as a mini progress bar in the tab bar

### M7: Pause/resume
- [ ] Test pause mid-generation; confirm PAUSED state survives and resumes correctly
- [ ] Handle server restart with branches in RUNNING state (mark FAILED on startup)

### Phase 1: Quality
- [ ] Verify Phase 1 document quality (no generic filler, concrete architecture decisions)
- [ ] Branch cap convergence test

### Infrastructure
- [ ] Parallel tool execution in `call_with_tools` — `asyncio.gather` for independent tool calls within a round (agreed deferral)
- [ ] Consider dedicated `phase3_verify` stage with stronger model for the verification pass

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
| `backend/app/agents/code_generator_agent.py` | Phase 3 manifest → file write agent (chunked PRD, verification pass) |
| `backend/app/api/phase3.py` | Phase 3 REST endpoints + file browser + regenerate-PRD |
| `backend/app/inference/client.py` | InferenceClient — all model calls, tool dispatch |
| `backend/app/db/models.py` | All ORM entities |
| `backend/models.yaml` | Stage → model + backend routing |
| `frontend/src/components/Phase3Implementation.tsx` | Phase 3 UI: activity log, file browser, chat |
| `frontend/src/components/IdeaDetail.tsx` | Idea detail with branch tree |
| `PRD.md` | Full product requirements |

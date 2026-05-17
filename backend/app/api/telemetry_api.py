import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        # All records are UTC; normalise to a naive string then attach UTC
        s = ts_str.strip()
        # Strip timezone suffix (+00:00, Z, -HH:MM) — we always write UTC
        for sep in ("+", "Z"):
            if sep in s[10:]:
                s = s[:10 + s[10:].index(sep)]
                break
        s = s.rstrip("Z")
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def _read_records(
    since: datetime,
    until: datetime,
    model: str | None,
    project_id: str | None,
    backend: str | None,
    stage: str | None,
) -> list[dict]:
    from app.config import settings
    log_path: Path = settings.telemetry_log_path
    if not log_path.exists():
        return []

    records: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = _parse_ts(rec.get("ts", ""))
            if ts is None or ts < since or ts > until:
                continue
            if model and rec.get("model") != model:
                continue
            if project_id and rec.get("project_id") != project_id:
                continue
            if backend and rec.get("backend") != backend:
                continue
            if stage and rec.get("stage") != stage:
                continue

            records.append(rec)

    return records


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    sv = sorted(values)
    k = (len(sv) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (k - lo)


def _bucket_seconds(period_hours: float) -> int:
    if period_hours <= 1:
        return 300      # 5 min
    if period_hours <= 6:
        return 1800     # 30 min
    if period_hours <= 24:
        return 7200     # 2 h
    if period_hours <= 168:
        return 43200    # 12 h
    return 86400        # 1 day


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/summary")
async def get_summary(
    since: str = Query(default="", description="ISO UTC timestamp; defaults to 7 days ago"),
    until: str = Query(default="", description="ISO UTC timestamp; defaults to now"),
    model: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    backend: str | None = Query(default=None),
    stage: str | None = Query(default=None),
):
    """
    Aggregate telemetry metrics for the ops dashboard.

    Returns per-model, per-stage, per-project, per-backend stats plus
    a time-bucketed series for the timeline chart.
    """
    now = datetime.now(timezone.utc)
    _until = _parse_ts(until) or now
    _since = _parse_ts(since) or (now - timedelta(days=7))
    period_hours = (_until - _since).total_seconds() / 3600

    records = _read_records(_since, _until, model, project_id, backend, stage)

    # -- aggregation buckets ------------------------------------------------
    def _new_agg():
        return {"calls": 0, "success": 0, "durations": [], "fallbacks": 0, "tokens_prompt": 0, "tokens_completion": 0}

    model_agg: dict[str, dict] = defaultdict(_new_agg)
    model_backend: dict[str, str] = {}
    stage_agg: dict[str, dict] = defaultdict(_new_agg)
    project_agg: dict[str, dict] = defaultdict(_new_agg)
    project_names: dict[str, str] = {}
    backend_agg: dict[str, dict] = defaultdict(_new_agg)

    # tool-call aggregation
    # per-project: {project_id: {tool_name: total_calls}}
    project_tool_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # per-model: list of total tool calls per invocation (to average)
    model_tool_totals: dict[str, list[int]] = defaultdict(list)

    # per-type (fast/standard/large) aggregation
    type_agg: dict[str, dict] = defaultdict(_new_agg)
    type_tool_totals: dict[str, list[int]] = defaultdict(list)
    # per-project: how many initial dispatches of each type (non-fallback only)
    project_type_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    all_models: set[str] = set()
    all_backends: set[str] = set()
    all_stages: set[str] = set()
    all_projects: dict[str, str] = {}

    # error aggregation: (error_text, model) -> count
    error_counts: dict[tuple[str, str], int] = defaultdict(int)

    for rec in records:
        m = rec.get("model") or "unknown"
        s = rec.get("stage") or "unknown"
        b = rec.get("backend") or "unknown"
        pid = rec.get("project_id") or ""
        pname = rec.get("project_name") or pid
        ok = bool(rec.get("success", True))
        dur = rec.get("duration_ms")
        is_fb = bool(rec.get("is_fallback", False))
        tc = rec.get("tool_calls") or {}
        mtype = rec.get("model_type") or ""

        all_models.add(m)
        all_backends.add(b)
        all_stages.add(s)
        if pid:
            all_projects[pid] = pname

        tp = rec.get("tokens_prompt") or 0
        tcomp = rec.get("tokens_completion") or 0

        for agg, key in ((model_agg, m), (stage_agg, s), (backend_agg, b)):
            agg[key]["calls"] += 1
            agg[key]["success"] += int(ok)
            agg[key]["fallbacks"] += int(is_fb)
            agg[key]["tokens_prompt"] += tp
            agg[key]["tokens_completion"] += tcomp
            if dur is not None:
                agg[key]["durations"].append(float(dur))

        model_backend[m] = b

        if not ok:
            err_text = (rec.get("error") or "unknown error").strip()
            error_counts[(err_text, m)] += 1

        if pid:
            project_agg[pid]["calls"] += 1
            project_agg[pid]["success"] += int(ok)
            project_agg[pid]["fallbacks"] += int(is_fb)
            project_agg[pid]["tokens_prompt"] += tp
            project_agg[pid]["tokens_completion"] += tcomp
            project_names[pid] = pname

        # type aggregation
        if mtype:
            type_agg[mtype]["calls"] += 1
            type_agg[mtype]["success"] += int(ok)
            type_agg[mtype]["fallbacks"] += int(is_fb)
            if dur is not None:
                type_agg[mtype]["durations"].append(float(dur))
            if tc:
                type_tool_totals[mtype].append(sum(tc.values()))
            # count initial dispatches per project per type (not fallbacks)
            if pid and not is_fb:
                project_type_counts[pid][mtype] += 1

        # tool-call stats
        if tc:
            if pid:
                for tool, count in tc.items():
                    project_tool_totals[pid][tool] += count
            model_tool_totals[m].append(sum(tc.values()))

    # -- time buckets -------------------------------------------------------
    bkt_s = _bucket_seconds(period_hours)
    # Snap origin to a clean boundary so labels align to calendar units rather
    # than the arbitrary _since moment (e.g. 10:27 UTC).
    if bkt_s >= 7200:   # 2 h, 12 h, 1 day → midnight UTC
        bucket_origin = _since.replace(hour=0, minute=0, second=0, microsecond=0)
    elif bkt_s >= 300:  # 5 min, 30 min → top of hour
        bucket_origin = _since.replace(minute=0, second=0, microsecond=0)
    else:
        bucket_origin = _since
    time_buckets: dict[int, dict] = defaultdict(_new_agg)
    for rec in records:
        ts = _parse_ts(rec.get("ts", ""))
        if ts is None:
            continue
        idx = int((ts - bucket_origin).total_seconds() // bkt_s)
        bk = time_buckets[idx]
        bk["calls"] += 1
        bk["success"] += int(bool(rec.get("success", True)))
        dur = rec.get("duration_ms")
        if dur is not None:
            bk["durations"].append(float(dur))
        bk["tokens_prompt"] += rec.get("tokens_prompt") or 0
        bk["tokens_completion"] += rec.get("tokens_completion") or 0

    num_buckets = max(1, int((_until - bucket_origin).total_seconds() / bkt_s) + 1)
    over_time = []
    for i in range(num_buckets):
        bk = time_buckets.get(i, {"calls": 0, "success": 0, "durations": [], "tokens_prompt": 0, "tokens_completion": 0})
        durs = bk.get("durations", [])
        over_time.append({
            "bucket": (bucket_origin + timedelta(seconds=i * bkt_s)).isoformat(),
            "calls": bk["calls"],
            "success": bk["success"],
            "avg_duration_ms": round(statistics.mean(durs)) if durs else None,
            "tokens_prompt": bk.get("tokens_prompt", 0),
            "tokens_completion": bk.get("tokens_completion", 0),
            "tokens_total": bk.get("tokens_prompt", 0) + bk.get("tokens_completion", 0),
        })

    # -- formatters ---------------------------------------------------------
    def _fmt(key: str, agg: dict, extra: dict | None = None) -> dict:
        c, s, durs, fb = agg["calls"], agg["success"], agg["durations"], agg["fallbacks"]
        tp = agg.get("tokens_prompt", 0)
        tcomp = agg.get("tokens_completion", 0)
        out = {
            "calls": c,
            "success": s,
            "fallbacks": fb,
            "success_rate": round(s / c, 3) if c else 0,
            "avg_duration_ms": round(statistics.mean(durs)) if durs else None,
            "p95_duration_ms": round(_percentile(durs, 95)) if durs else None,
            "tokens_prompt": tp,
            "tokens_completion": tcomp,
            "tokens_total": tp + tcomp,
        }
        if extra:
            out.update(extra)
        return out

    # avg tool calls per project, per tool name
    # For each tool, average its per-project total across all projects that used it.
    all_tools: set[str] = set()
    for ptotals in project_tool_totals.values():
        all_tools.update(ptotals.keys())
    avg_tools_per_project = sorted(
        [
            {
                "tool": tool,
                "avg_calls_per_project": round(
                    statistics.mean(
                        project_tool_totals[pid].get(tool, 0)
                        for pid in project_tool_totals
                        if tool in project_tool_totals[pid]
                    ),
                    1,
                ) if any(tool in project_tool_totals[pid] for pid in project_tool_totals) else 0,
                "projects_used": sum(1 for pid in project_tool_totals if tool in project_tool_totals[pid]),
            }
            for tool in all_tools
        ],
        key=lambda x: -x["avg_calls_per_project"],
    )

    # avg total tool calls per model invocation
    avg_tools_per_model = sorted(
        [
            {
                "model": m,
                "avg_tool_calls_per_invocation": round(statistics.mean(totals), 1) if totals else 0,
                "invocations_with_tools": len(totals),
            }
            for m, totals in model_tool_totals.items()
        ],
        key=lambda x: -x["avg_tool_calls_per_invocation"],
    )

    _TYPE_ORDER = ["fast", "standard", "large"]

    by_model = sorted(
        [{"model": k, "backend": model_backend.get(k, ""), **_fmt(k, v)}
         for k, v in model_agg.items()],
        key=lambda x: -x["calls"],
    )
    by_stage = sorted(
        [{"stage": k, **_fmt(k, v)} for k, v in stage_agg.items()],
        key=lambda x: -x["calls"],
    )
    by_project = sorted(
        [{"project_id": k, "project_name": project_names.get(k, k), **_fmt(k, v)}
         for k, v in project_agg.items()],
        key=lambda x: -x["calls"],
    )
    by_backend = sorted(
        [{"backend": k, **_fmt(k, v)} for k, v in backend_agg.items()],
        key=lambda x: -x["calls"],
    )
    by_type = sorted(
        [
            {
                "model_type": k,
                **_fmt(k, v),
                "avg_tool_calls": round(statistics.mean(type_tool_totals[k]), 1) if type_tool_totals[k] else None,
            }
            for k, v in type_agg.items()
            if k
        ],
        key=lambda x: _TYPE_ORDER.index(x["model_type"]) if x["model_type"] in _TYPE_ORDER else 99,
    )

    all_project_types: set[str] = {
        mtype for pdata in project_type_counts.values() for mtype in pdata
    }
    avg_tasks_per_project_by_type = sorted(
        [
            {
                "model_type": mtype,
                "avg_tasks_per_project": round(
                    statistics.mean(
                        project_type_counts[pid][mtype]
                        for pid in project_type_counts
                        if mtype in project_type_counts[pid]
                    ),
                    1,
                ) if any(mtype in project_type_counts[pid] for pid in project_type_counts) else 0,
                "projects": sum(1 for pid in project_type_counts if mtype in project_type_counts[pid]),
                "total_tasks": sum(project_type_counts[pid].get(mtype, 0) for pid in project_type_counts),
            }
            for mtype in all_project_types
        ],
        key=lambda x: _TYPE_ORDER.index(x["model_type"]) if x["model_type"] in _TYPE_ORDER else 99,
    )

    by_error = sorted(
        [{"error": err, "model": mdl, "count": cnt} for (err, mdl), cnt in error_counts.items()],
        key=lambda x: -x["count"],
    )

    total_tokens_prompt = sum(r.get("tokens_prompt") or 0 for r in records)
    total_tokens_completion = sum(r.get("tokens_completion") or 0 for r in records)

    return {
        "total_calls": len(records),
        "total_tokens_prompt": total_tokens_prompt,
        "total_tokens_completion": total_tokens_completion,
        "total_tokens": total_tokens_prompt + total_tokens_completion,
        "period_hours": round(period_hours, 1),
        "by_model": by_model,
        "by_stage": by_stage,
        "by_project": by_project,
        "by_backend": by_backend,
        "by_type": by_type,
        "avg_tasks_per_project_by_type": avg_tasks_per_project_by_type,
        "over_time": over_time,
        "avg_tools_per_project": avg_tools_per_project,
        "avg_tools_per_model": avg_tools_per_model,
        "by_error": by_error,
        "available_models": sorted(all_models),
        "available_backends": sorted(all_backends),
        "available_stages": sorted(all_stages),
        "available_projects": [
            {"id": k, "name": v} for k, v in sorted(all_projects.items(), key=lambda x: x[1])
        ],
    }


@router.get("/sub-agent-ranking")
async def get_sub_agent_ranking(project_id: str | None = Query(default=None)):
    """Current telemetry-ranked order of phase3_sub_agent models.

    When project_id is supplied, stats are scoped to that project only.
    Returns each model with rank, stats, and whether it qualified for the
    telemetry-ranked pool (min 5 calls + min 15% success rate).
    Models that have data but don't qualify are sorted by success rate as a
    secondary signal rather than falling back to raw YAML order.
    """
    from app.config import settings
    import json, statistics as _stats

    stage = "phase3_sub_agent"
    log_path = settings.telemetry_log_path

    # Load candidate list from model registry (preserves YAML order)
    try:
        from app.main import get_inference_client
        stage_cfg = get_inference_client().registry.get_stage(stage)
        candidates = [m.model for m in stage_cfg.selectable_models]
    except Exception:
        candidates = []

    if not candidates:
        return {"models": [], "stage": stage, "min_calls": 5, "min_success_rate": 0.15, "project_id": project_id}

    # Compute per-model stats from telemetry log
    per_model: dict[str, dict] = {m: {"total": 0, "success": 0, "durations": []} for m in candidates}
    if log_path and log_path.exists():
        try:
            with open(log_path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("stage") != stage:
                        continue
                    if project_id and rec.get("project_id") != project_id:
                        continue
                    m = rec.get("model")
                    if m not in per_model:
                        continue
                    per_model[m]["total"] += 1
                    if rec.get("success"):
                        per_model[m]["success"] += 1
                    dur = rec.get("duration_ms")
                    if dur is not None:
                        per_model[m]["durations"].append(float(dur))
        except Exception:
            pass

    MIN_CALLS = 5
    MIN_SUCCESS_RATE = 0.15

    # Split into ranked (enough data to trust), partial (some data), and untried
    ranked_entries = []
    partial_entries = []  # has calls but below min_calls or min_success_rate threshold
    untried_entries = []  # no calls at all — keep in YAML order
    yaml_position = {m: i for i, m in enumerate(candidates)}

    for m in candidates:
        s = per_model[m]
        total = s["total"]
        success = s["success"]
        durs = s["durations"]
        success_rate = success / total if total else 0.0
        avg_dur = round(_stats.mean(durs)) if durs else None

        entry = {
            "model": m,
            "total_calls": total,
            "success": success,
            "success_rate": round(success_rate, 3),
            "avg_duration_ms": avg_dur,
            "is_ranked": total >= MIN_CALLS and success_rate >= MIN_SUCCESS_RATE,
        }
        if entry["is_ranked"]:
            ranked_entries.append(entry)
        elif total > 0:
            partial_entries.append(entry)
        else:
            untried_entries.append(entry)

    # Ranked: success_rate DESC, avg_duration ASC
    ranked_entries.sort(key=lambda x: (-x["success_rate"], x["avg_duration_ms"] or float("inf")))
    # Partial: same sort — use what data we have as a signal
    partial_entries.sort(key=lambda x: (-x["success_rate"], x["avg_duration_ms"] or float("inf")))
    # Untried: preserve YAML order
    untried_entries.sort(key=lambda x: yaml_position[x["model"]])

    ordered = ranked_entries + partial_entries + untried_entries
    for i, entry in enumerate(ordered):
        entry["rank"] = i + 1

    return {
        "models": ordered,
        "stage": stage,
        "min_calls": MIN_CALLS,
        "min_success_rate": MIN_SUCCESS_RATE,
        "project_id": project_id,
    }


@router.get("/calls")
async def get_calls(
    since: str = Query(default="", description="ISO UTC timestamp; defaults to 24h ago"),
    until: str = Query(default="", description="ISO UTC timestamp; defaults to now"),
    model: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    backend: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0),
):
    """Recent raw call records — for the detail table in the ops dashboard."""
    now = datetime.now(timezone.utc)
    _until = _parse_ts(until) or now
    _since = _parse_ts(since) or (now - timedelta(hours=24))

    records = _read_records(_since, _until, model, project_id, backend, stage)
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return {"calls": records[offset: offset + limit], "total": len(records)}

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
        return {"calls": 0, "success": 0, "durations": [], "fallbacks": 0}

    model_agg: dict[str, dict] = defaultdict(_new_agg)
    model_backend: dict[str, str] = {}
    stage_agg: dict[str, dict] = defaultdict(_new_agg)
    project_agg: dict[str, dict] = defaultdict(_new_agg)
    project_names: dict[str, str] = {}
    backend_agg: dict[str, dict] = defaultdict(_new_agg)

    all_models: set[str] = set()
    all_backends: set[str] = set()
    all_stages: set[str] = set()
    all_projects: dict[str, str] = {}

    for rec in records:
        m = rec.get("model") or "unknown"
        s = rec.get("stage") or "unknown"
        b = rec.get("backend") or "unknown"
        pid = rec.get("project_id") or ""
        pname = rec.get("project_name") or pid
        ok = bool(rec.get("success", True))
        dur = rec.get("duration_ms")
        is_fb = bool(rec.get("is_fallback", False))

        all_models.add(m)
        all_backends.add(b)
        all_stages.add(s)
        if pid:
            all_projects[pid] = pname

        for agg, key in ((model_agg, m), (stage_agg, s), (backend_agg, b)):
            agg[key]["calls"] += 1
            agg[key]["success"] += int(ok)
            agg[key]["fallbacks"] += int(is_fb)
            if dur is not None:
                agg[key]["durations"].append(float(dur))

        model_backend[m] = b

        if pid:
            project_agg[pid]["calls"] += 1
            project_agg[pid]["success"] += int(ok)
            project_agg[pid]["fallbacks"] += int(is_fb)
            project_names[pid] = pname

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

    num_buckets = max(1, int((_until - bucket_origin).total_seconds() / bkt_s) + 1)
    over_time = []
    for i in range(num_buckets):
        bk = time_buckets.get(i, {"calls": 0, "success": 0, "durations": []})
        durs = bk.get("durations", [])
        over_time.append({
            "bucket": (bucket_origin + timedelta(seconds=i * bkt_s)).isoformat(),
            "calls": bk["calls"],
            "success": bk["success"],
            "avg_duration_ms": round(statistics.mean(durs)) if durs else None,
        })

    # -- formatters ---------------------------------------------------------
    def _fmt(key: str, agg: dict, extra: dict | None = None) -> dict:
        c, s, durs, fb = agg["calls"], agg["success"], agg["durations"], agg["fallbacks"]
        out = {
            "calls": c,
            "success": s,
            "fallbacks": fb,
            "success_rate": round(s / c, 3) if c else 0,
            "avg_duration_ms": round(statistics.mean(durs)) if durs else None,
            "p95_duration_ms": round(_percentile(durs, 95)) if durs else None,
        }
        if extra:
            out.update(extra)
        return out

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

    return {
        "total_calls": len(records),
        "period_hours": round(period_hours, 1),
        "by_model": by_model,
        "by_stage": by_stage,
        "by_project": by_project,
        "by_backend": by_backend,
        "over_time": over_time,
        "available_models": sorted(all_models),
        "available_backends": sorted(all_backends),
        "available_stages": sorted(all_stages),
        "available_projects": [
            {"id": k, "name": v} for k, v in sorted(all_projects.items(), key=lambda x: x[1])
        ],
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

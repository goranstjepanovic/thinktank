"""Per-project agent memory — stores file observations with semantic retrieval.

Agents call memory_store after reading a file to record their findings.
They call memory_search before reading a file to check if it was already analysed.

Storage: aiosqlite at data/memory.db (separate from main SQLAlchemy DB).
Retrieval: cosine similarity on Ollama /api/embed embeddings.
Fallback: keyword substring search when the embedding model is unavailable.
"""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import httpx

from app.inference.base import ToolDefinition

logger = logging.getLogger(__name__)

_db_path: Path | None = None
_embed_model: str = "nomic-embed-text"
_ollama_base_url: str = "http://localhost:11434"


def configure(
    db_path: str | Path,
    embed_model: str = "nomic-embed-text",
    ollama_base_url: str = "http://localhost:11434",
) -> None:
    global _db_path, _embed_model, _ollama_base_url
    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _embed_model = embed_model
    _ollama_base_url = ollama_base_url


async def _ensure_table(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS project_memories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT NOT NULL,
            file_path   TEXT NOT NULL,
            observation TEXT NOT NULL,
            embedding   TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE(project_id, file_path)
        )
    """)
    await db.commit()


async def _embed(text: str) -> list[float] | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_ollama_base_url}/api/embed",
                json={"model": _embed_model, "input": text},
            )
            if resp.is_success:
                data = resp.json()
                embeddings = data.get("embeddings")
                if embeddings and isinstance(embeddings, list) and embeddings:
                    return embeddings[0]
    except Exception as exc:
        logger.debug("memory: embed failed (%s) — keyword fallback active", exc)
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


async def store(project_id: str, file_path: str, observation: str) -> None:
    if _db_path is None:
        return
    embedding = await _embed(observation)
    async with aiosqlite.connect(_db_path) as db:
        await _ensure_table(db)
        await db.execute(
            """
            INSERT INTO project_memories (project_id, file_path, observation, embedding, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, file_path) DO UPDATE SET
                observation = excluded.observation,
                embedding   = excluded.embedding,
                created_at  = excluded.created_at
            """,
            (
                project_id,
                file_path,
                observation,
                json.dumps(embedding) if embedding else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
    logger.debug("memory: stored observation for %s (project %s)", file_path, project_id)


async def search(project_id: str, query: str, top_k: int = 5) -> list[dict]:
    if _db_path is None:
        return []
    top_k = min(top_k, 10)
    query_embedding = await _embed(query)

    async with aiosqlite.connect(_db_path) as db:
        await _ensure_table(db)
        async with db.execute(
            "SELECT file_path, observation, embedding FROM project_memories WHERE project_id = ?",
            (project_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return []

    if query_embedding:
        scored: list[tuple[float, str, str]] = []
        for file_path, observation, emb_json in rows:
            if emb_json:
                try:
                    score = _cosine(query_embedding, json.loads(emb_json))
                    scored.append((score, file_path, observation))
                except Exception:
                    pass
        scored.sort(reverse=True)
        return [
            {"file_path": fp, "observation": obs, "relevance": round(s, 3)}
            for s, fp, obs in scored[:top_k]
            if s > 0.3
        ]
    else:
        # Keyword fallback when embed model unavailable
        q = query.lower()
        return [
            {"file_path": fp, "observation": obs}
            for fp, obs, _ in rows
            if q in fp.lower() or q in obs.lower()
        ][:top_k]


async def list_all(project_id: str) -> list[dict]:
    if _db_path is None:
        return []
    async with aiosqlite.connect(_db_path) as db:
        await _ensure_table(db)
        async with db.execute(
            "SELECT file_path, substr(observation, 1, 120), created_at "
            "FROM project_memories WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"file_path": fp, "snippet": snip, "stored_at": ts} for fp, snip, ts in rows]


async def delete_project(project_id: str) -> int:
    """Delete all memory observations for a project. Returns the number of rows deleted."""
    if _db_path is None:
        return 0
    async with aiosqlite.connect(_db_path) as db:
        await _ensure_table(db)
        cursor = await db.execute(
            "DELETE FROM project_memories WHERE project_id = ?",
            (project_id,),
        )
        await db.commit()
        n = cursor.rowcount
    logger.info("memory: deleted %d observation(s) for project %s", n, project_id)
    return n


# ---------------------------------------------------------------------------
# Tool definitions — imported by orchestrator_agent to wire up sub-agents
# ---------------------------------------------------------------------------

MEMORY_STORE_TOOL = ToolDefinition(
    name="memory_store",
    description=(
        "Store your observations about a file after reading and analysing it.\n"
        "Call this whenever you read a file and form meaningful conclusions — what the file\n"
        "does, key classes/functions, important patterns, dependencies, gotchas.\n"
        "Do NOT store raw file content — store your analysis in your own words.\n"
        "Observations are searchable by all agents working on the same project."
    ),
    parameters={
        "type": "object",
        "required": ["file_path", "observation"],
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path of the file this observation is about (e.g. 'backend/app/auth.py')",
            },
            "observation": {
                "type": "string",
                "description": (
                    "Your analysis: purpose of the file, key functions/classes, "
                    "patterns used, dependencies, edge cases, anything non-obvious. 50–200 words."
                ),
            },
        },
    },
)

MEMORY_SEARCH_TOOL = ToolDefinition(
    name="memory_search",
    description=(
        "Search stored file observations by semantic similarity.\n"
        "Call this BEFORE reading a file — if a useful observation already exists you may not\n"
        "need to read the file at all. Also useful for finding which files relate to a topic."
    ),
    parameters={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What you are looking for, e.g. 'authentication and JWT tokens' "
                    "or 'database model for users'"
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 10)",
                "default": 5,
            },
        },
    },
)

MEMORY_LIST_TOOL = ToolDefinition(
    name="memory_list",
    description=(
        "List all files that have stored observations for this project.\n"
        "Use this to see what has already been analysed before deciding to read a file."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
)

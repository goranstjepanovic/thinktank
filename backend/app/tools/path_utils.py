"""Helpers for model-supplied project-relative paths."""

from pathlib import Path


def normalize_project_relative_path(base_dir: str, path: str) -> str:
    """
    Normalize a model-supplied path relative to a project output directory.

    Phase 3 agents are told paths are relative to the project root, but they
    sometimes include the project folder name anyway, e.g.
    ``modern-snake-game/src/App.tsx`` while the base directory is already
    ``.../modern-snake-game``. Strip that duplicated leading segment so writes
    land in the actual project root.
    """
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return ""

    base = Path(base_dir).resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return str(candidate.resolve().relative_to(base)).replace("\\", "/")
        except ValueError:
            return raw

    normal = raw.replace("\\", "/")
    while normal.startswith("./"):
        normal = normal[2:]
    normal = normal.lstrip("/")

    parts = [part for part in normal.split("/") if part not in ("", ".")]
    while parts and parts[0].casefold() == base.name.casefold():
        parts = parts[1:]

    return "/".join(parts)

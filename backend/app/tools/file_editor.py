"""
File editing tool (F19) — callable by pipeline agents via tool-use.

All path operations are restricted to a caller-supplied `allowed_base_dir`.
Paths that attempt to escape (via `..` or absolute paths outside the base)
are rejected before any I/O.

Operations:
  write_file      — write complete content to a file (creates or overwrites)
  search_replace  — find-and-replace inside an existing file
  insert_lines    — insert lines after a given line number
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from app.services.file_manager import file_manager

logger = logging.getLogger(__name__)


@dataclass
class FileEditResult:
    success: bool
    operation: str
    path: str
    detail: str = ""   # e.g. "replacements_made: 2" or error message


def _resolve_safe(base_dir: str, relative_path: str) -> Path | None:
    """
    Resolve `relative_path` under `base_dir`.
    Returns None if the resolved path would escape the base directory.
    """
    base = Path(base_dir).resolve()
    # Strip any leading slash so the path is treated as relative even if the
    # model accidentally prefixes it with "/"
    sanitised = relative_path.lstrip("/\\")
    resolved = (base / sanitised).resolve()
    try:
        resolved.relative_to(base)
        return resolved
    except ValueError:
        return None


async def edit_file(
    operation: str,
    path: str,
    allowed_base_dir: str,
    content: str = "",
    search_text: str = "",
    replace_text: str = "",
    after_line: int = -1,
    replace_all: bool = False,
) -> FileEditResult:
    """
    Execute a file edit operation with path-restriction enforcement.

    Parameters
    ----------
    operation        : "write_file" | "search_replace" | "insert_lines"
    path             : path relative to allowed_base_dir
    allowed_base_dir : absolute path — all operations confined to this tree
    content          : file content (write_file) or lines to insert (insert_lines)
    search_text      : text to find (search_replace)
    replace_text     : replacement text (search_replace)
    after_line       : 0-based line index to insert after; -1 = append (insert_lines)
    replace_all      : replace all occurrences, not just the first (search_replace)
    """
    resolved = _resolve_safe(allowed_base_dir, path)
    if resolved is None:
        msg = f"Path '{path}' escapes allowed directory '{allowed_base_dir}'"
        logger.warning("file_editor blocked: %s", msg)
        return FileEditResult(success=False, operation=operation, path=path, detail=msg)

    abs_path = str(resolved)

    if operation == "write_file":
        result = await file_manager.write_file(abs_path, content)
        detail = "" if result["success"] else result.get("error", "unknown error")
        return FileEditResult(success=result["success"], operation=operation, path=abs_path, detail=detail)

    elif operation == "search_replace":
        if not search_text:
            return FileEditResult(success=False, operation=operation, path=abs_path, detail="search_text is required")
        result = await file_manager.search_replace(abs_path, search_text, replace_text, replace_all=replace_all)
        if result["success"]:
            detail = f"replacements_made: {result.get('replacements_made', 0)}"
        else:
            detail = result.get("error", "unknown error")
        return FileEditResult(success=result["success"], operation=operation, path=abs_path, detail=detail)

    elif operation == "insert_lines":
        result = await file_manager.insert_lines(abs_path, after_line, content)
        detail = "" if result["success"] else result.get("error", "unknown error")
        return FileEditResult(success=result["success"], operation=operation, path=abs_path, detail=detail)

    else:
        msg = f"Unknown operation: '{operation}'. Valid: write_file, search_replace, insert_lines"
        return FileEditResult(success=False, operation=operation, path=abs_path, detail=msg)

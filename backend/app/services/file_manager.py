"""
Process-level file lock manager (F19).

Maintains one asyncio.Lock per canonical resolved file path.
All write operations (write_file, search_replace, insert_lines) acquire the path
lock before touching the file.  If another coroutine holds the lock, the caller
waits rather than failing — no explicit retry loop needed because asyncio.Lock
already queues waiters.

This is a module-level singleton; import `file_manager` and call its methods.
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class FileManagerService:
    """
    Centralised, process-level file write serialiser.

    Responsibilities:
      - One asyncio.Lock per canonical path — no two coroutines write the same
        file simultaneously.
      - Parent directories created automatically on write.
      - Structured return values so callers (tool handler, test code) can inspect
        outcomes without parsing exceptions.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # Protects the _locks dict itself (avoids race on dict insertion)
        self._meta_lock = asyncio.Lock()

    async def _get_lock(self, path: str) -> asyncio.Lock:
        canonical = str(Path(path).resolve())
        async with self._meta_lock:
            if canonical not in self._locks:
                self._locks[canonical] = asyncio.Lock()
            return self._locks[canonical]

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def write_file(self, path: str, content: str) -> dict:
        """
        Write complete content to path, replacing any existing file.
        Returns {"success": True} or {"success": False, "error": "..."}.
        """
        lock = await self._get_lock(path)
        async with lock:
            try:
                out = Path(path)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(content, encoding="utf-8")
                logger.debug("file_manager write_file: %s (%d bytes)", path, len(content))
                return {"success": True}
            except Exception as exc:
                logger.error("file_manager write_file failed: %s — %s", path, exc)
                return {"success": False, "error": str(exc)}

    async def search_replace(
        self,
        path: str,
        search_text: str,
        replace_text: str,
        replace_all: bool = False,
    ) -> dict:
        """
        Find search_text in the file and replace the first (or all) occurrence(s).

        Returns:
          {"success": True, "replacements_made": int}
          {"success": False, "error": "..."}
        """
        if not search_text:
            return {"success": False, "error": "search_text cannot be empty"}

        lock = await self._get_lock(path)
        async with lock:
            p = Path(path)
            if not p.exists():
                return {"success": False, "error": f"File not found: {path}"}
            try:
                original = p.read_text(encoding="utf-8")
                count = original.count(search_text)
                if count == 0:
                    return {"success": False, "error": "search_text not found in file"}
                if replace_all:
                    updated = original.replace(search_text, replace_text)
                    replacements = count
                else:
                    updated = original.replace(search_text, replace_text, 1)
                    replacements = 1
                p.write_text(updated, encoding="utf-8")
                logger.debug("file_manager search_replace: %s — %d replacement(s)", path, replacements)
                return {"success": True, "replacements_made": replacements}
            except Exception as exc:
                logger.error("file_manager search_replace failed: %s — %s", path, exc)
                return {"success": False, "error": str(exc)}

    async def insert_lines(self, path: str, after_line: int, content: str) -> dict:
        """
        Insert lines into an existing file after `after_line` (0-based index).
        Use after_line=-1 to append to the end.

        Returns {"success": True} or {"success": False, "error": "..."}.
        """
        lock = await self._get_lock(path)
        async with lock:
            p = Path(path)
            if not p.exists():
                return {"success": False, "error": f"File not found: {path}"}
            try:
                lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
                if after_line == -1:
                    insert_pos = len(lines)
                else:
                    insert_pos = min(after_line + 1, len(lines))
                insert_content = content if content.endswith("\n") else content + "\n"
                lines.insert(insert_pos, insert_content)
                p.write_text("".join(lines), encoding="utf-8")
                logger.debug("file_manager insert_lines: %s after line %d", path, after_line)
                return {"success": True}
            except Exception as exc:
                logger.error("file_manager insert_lines failed: %s — %s", path, exc)
                return {"success": False, "error": str(exc)}


# Module-level singleton — import this, do not instantiate FileManagerService directly.
file_manager = FileManagerService()

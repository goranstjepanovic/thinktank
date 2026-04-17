"""
Settings API — read and write user-configurable application settings.

Routes:
  GET  /api/v1/settings                        Get current settings
  POST /api/v1/settings                        Update settings
  POST /api/v1/settings/move-implementations   Move all implementations to a new directory
"""

import logging
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Phase3Session
from app.services.user_settings import get_implementations_dir, set_implementations_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])

# Ephemeral directories that are always re-creatable — skip during move
_SKIP_DIRS = {
    "node_modules", ".venv", "venv", "env",
    "__pycache__", ".git",
    "dist", "build", ".next", ".nuxt", ".svelte-kit",
    "target",           # Rust / Maven
    ".gradle",          # Gradle
    "vendor",           # Go / PHP
    ".cache", ".parcel-cache",
}


def _ignore_dev_dirs(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _SKIP_DIRS}


def _rmtree_tolerant(path: Path) -> None:
    """Remove a directory tree, ignoring errors on individual files (e.g. locked on Windows)."""
    def _on_error(func, fpath, _exc):
        logger.warning("move-implementations: could not remove %s — skipping", fpath)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_error)
    else:
        shutil.rmtree(path, onerror=_on_error)


class SettingsBody(BaseModel):
    implementations_dir: str


class MoveBody(BaseModel):
    destination: str


@router.get("")
async def get_settings():
    return {
        "implementations_dir": str(get_implementations_dir()),
    }


@router.post("")
async def update_settings(body: SettingsBody):
    target = Path(body.implementations_dir.strip())
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="implementations_dir must be an absolute path")
    set_implementations_dir(str(target))
    target.mkdir(parents=True, exist_ok=True)
    return {"implementations_dir": str(target)}


@router.post("/move-implementations")
async def move_implementations(body: MoveBody, db: AsyncSession = Depends(get_session)):
    old_dir = get_implementations_dir()
    new_dir = Path(body.destination.strip()).resolve()

    if not new_dir.is_absolute():
        raise HTTPException(status_code=400, detail="destination must be an absolute path")
    if old_dir.resolve() == new_dir:
        raise HTTPException(status_code=400, detail="Source and destination are the same")

    # Copy each project folder, skipping ephemeral dev directories, then remove source
    moved_count = 0
    errors: list[str] = []
    if old_dir.exists():
        new_dir.mkdir(parents=True, exist_ok=True)
        for child in old_dir.iterdir():
            if not child.is_dir():
                continue
            dest = new_dir / child.name
            try:
                shutil.copytree(str(child), str(dest), ignore=_ignore_dev_dirs, dirs_exist_ok=True)
                _rmtree_tolerant(child)
                moved_count += 1
            except Exception as exc:
                logger.error("move-implementations: failed to move %s: %s", child.name, exc)
                errors.append(f"{child.name}: {exc}")
        try:
            old_dir.rmdir()
        except OSError:
            pass

    # Update output_dir on all Phase3Sessions that referenced the old path
    old_prefix = str(old_dir.resolve())
    new_prefix = str(new_dir)
    result = await db.execute(select(Phase3Session))
    updated = 0
    for session in result.scalars():
        if session.output_dir and session.output_dir.startswith(old_prefix):
            session.output_dir = new_prefix + session.output_dir[len(old_prefix):]
            updated += 1
    await db.commit()

    # Persist the new setting
    set_implementations_dir(str(new_dir))

    logger.info("move-implementations: moved %d items, updated %d sessions", moved_count, updated)
    return {
        "moved_items": moved_count,
        "updated_sessions": updated,
        "implementations_dir": str(new_dir),
        "errors": errors,
    }

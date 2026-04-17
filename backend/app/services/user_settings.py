"""
User-configurable application settings stored in data/user_settings.json.
Falls back to the pydantic Settings defaults when a key is not present.
"""

import json
import logging
from pathlib import Path

from app.config import settings as _app_settings

logger = logging.getLogger(__name__)

_SETTINGS_FILE = _app_settings.data_dir / "user_settings.json"


def _load() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("user_settings: failed to read %s: %s", _SETTINGS_FILE, exc)
    return {}


def _save(data: dict) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_all() -> dict:
    return _load()


def get_implementations_dir() -> Path:
    data = _load()
    if "implementations_dir" in data:
        return Path(data["implementations_dir"])
    return _app_settings.implementations_dir


def set_implementations_dir(path: str) -> None:
    data = _load()
    data["implementations_dir"] = str(Path(path).resolve())
    _save(data)

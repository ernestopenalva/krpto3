from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
_LOADED = False


def load_project_env(env_file: Optional[Path] = None) -> None:
    """Load project-level environment variables without overriding the shell."""
    global _LOADED
    if _LOADED:
        return

    path = env_file or DEFAULT_ENV_FILE
    if not path.exists():
        _LOADED = True
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_env_fallback(path)
    else:
        load_dotenv(dotenv_path=path, override=False)

    _LOADED = True


def _load_env_fallback(path: Path) -> None:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = _clean_value(value.strip())


def _clean_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value

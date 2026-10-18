"""Where DevDash keeps its data. DEVDASH_HOME overrides the default ~/.devdash."""

import os
from pathlib import Path


def home() -> Path:
    path = Path(os.environ.get("DEVDASH_HOME", Path.home() / ".devdash"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return home() / "devdash.db"


def error_log() -> Path:
    return home() / "errors.log"


def hook_log() -> Path:
    return home() / "hooks.log"

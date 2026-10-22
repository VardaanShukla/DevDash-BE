"""Where DevDash keeps its data. DEVDASH_HOME overrides the default ~/.devdash."""

import os
from pathlib import Path

_private: set[Path] = set()


def home() -> Path:
    path = Path(os.environ.get("DEVDASH_HOME", Path.home() / ".devdash"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path not in _private:
        _make_private(path)
        _private.add(path)
    return path


def _make_private(path: Path) -> None:
    """Only you can read DevDash's data (prompts, diffs, explanations), not other accounts
    on this computer. Windows already keeps a user's profile folder to that user."""
    if os.name != "posix":
        return
    try:
        path.chmod(0o700)
        for item in path.iterdir():
            if item.is_file() and not item.is_symlink():
                item.chmod(0o600)
    except OSError:
        pass


def db_path() -> Path:
    return home() / "devdash.db"


def error_log() -> Path:
    return home() / "errors.log"


def hook_log() -> Path:
    return home() / "hooks.log"

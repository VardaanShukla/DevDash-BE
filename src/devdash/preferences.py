"""The dashboard's settings: its look, whether uncommitted work shows from the start, and
the keyboard shortcuts. Kept in the database, so every browser that signs in gets them.
Unknown names and bad values are refused rather than stored."""

import json

from devdash.store import connect

SETTING = "preferences"
THEMES = ("dark", "light")
# What a shortcut can do. Going to a screen is "view:<screen>".
# The side menu's screens in order, numbered 1, 2, … by default; then the screens that are
# sections of Home (This branch, Branch flow, Project map), which have no key unless set.
MENU = ("home", "digest", "summary", "constellation", "changes", "commits", "planner", "project")
VIEWS = (*MENU, "overview", "flow", "map", "help", "settings")
ACTIONS = (*(f"view:{view}" for view in VIEWS), "theme", "uncommitted", "sync", "next-file", "previous-file")
DEFAULT_SHORTCUTS = {
    **{f"view:{view}": str(n) for n, view in enumerate(MENU, start=1)},
    "view:settings": ",",
    "theme": "t",
    "uncommitted": "u",
    "sync": "s",
    "next-file": "j",
    "previous-file": "k",
}
# The defaults before This branch, Branch flow and Project map moved onto Home. A saved key
# equal to one of these was never picked by the person, so it follows the new defaults.
OLD_DEFAULTS = {
    1: {**{f"view:{view}": str(n) for n, view in enumerate(
        ("home", "digest", "summary", "overview", "flow", "map", "constellation", "changes", "commits"), start=1)},
        "view:settings": ",", "theme": "t", "uncommitted": "u", "sync": "s", "next-file": "j", "previous-file": "k"},
}
SHORTCUTS_VERSION = 2
DEFAULTS = {
    "theme": "dark",
    # None until the person picks: the dashboard asks once.
    "uncommitted": None,
    "shortcuts": DEFAULT_SHORTCUTS,
}
MAX_KEY = 20


class PreferenceError(ValueError):
    pass


def get() -> dict:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (SETTING,)).fetchone()
    conn.close()
    saved = json.loads(row["value"]) if row else {}
    chosen = saved.get("shortcuts", {})
    old = OLD_DEFAULTS.get(saved.get("shortcuts_version", 1), {}) if saved.get("shortcuts_version") != SHORTCUTS_VERSION else {}
    chosen = {action: key for action, key in chosen.items() if action in ACTIONS and old.get(action) != key}
    # Keys the person picked win; a default whose key they gave to something else is off.
    taken = {key for key in chosen.values() if key}
    defaults = {}
    for action in ACTIONS:
        key = DEFAULT_SHORTCUTS.get(action, "")
        defaults[action] = "" if key in taken else key
    return {"theme": saved.get("theme", DEFAULTS["theme"]), "uncommitted": saved.get("uncommitted"),
            "shortcuts": {**defaults, **chosen}}


def update(changes: dict) -> dict:
    """Merges the given settings into the saved ones. Shortcuts merge one action at a
    time; an empty key turns that shortcut off."""
    current = get()
    for name, value in changes.items():
        if name == "theme":
            if value not in THEMES:
                raise PreferenceError(f"theme is one of {', '.join(THEMES)}")
            current["theme"] = value
        elif name == "uncommitted":
            if not isinstance(value, bool):
                raise PreferenceError("uncommitted is true or false")
            current["uncommitted"] = value
        elif name == "shortcuts":
            current["shortcuts"] = _shortcuts({**current["shortcuts"], **_checked(value)})
        else:
            raise PreferenceError(f"no setting called {name!r}")
    _save(current)
    return current


def reset_shortcuts() -> dict:
    current = get()
    current["shortcuts"] = dict(DEFAULT_SHORTCUTS)
    _save(current)
    return current


def _checked(value) -> dict:
    if not isinstance(value, dict):
        raise PreferenceError("shortcuts maps an action to a key")
    for action, key in value.items():
        if action not in ACTIONS:
            raise PreferenceError(f"no action called {action!r}")
        if not isinstance(key, str) or len(key) > MAX_KEY:
            raise PreferenceError(f"the key for {action} must be text of at most {MAX_KEY} characters")
    return value


def _shortcuts(shortcuts: dict) -> dict:
    taken: dict[str, str] = {}
    for action, key in shortcuts.items():
        if not key:
            continue
        if key in taken:
            raise PreferenceError(f"{key!r} is already the shortcut for {taken[key]}")
        taken[key] = action
    return shortcuts


def _save(preferences: dict) -> None:
    # Only the keys the person changed are stored, so a later version's defaults reach the rest.
    chosen = {action: key for action, key in preferences["shortcuts"].items() if DEFAULT_SHORTCUTS.get(action, "") != key}
    stored = {"theme": preferences["theme"], "uncommitted": preferences["uncommitted"], "shortcuts": chosen,
              "shortcuts_version": SHORTCUTS_VERSION}
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, json.dumps(stored)))
    conn.close()

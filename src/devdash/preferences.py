"""The dashboard's settings: its look, whether uncommitted work shows from the start, the
keyboard shortcuts and which widgets Home shows, in what order. Kept in the database, so every browser that signs in gets them.
Unknown names and bad values are refused rather than stored."""

import json

from devdash.store import connect

SETTING = "preferences"
THEMES = ("dark", "light")
# What a shortcut can do. Going to a screen is "view:<screen>".
# The side menu's screens in order, numbered 1, 2, … by default; then the screens that are
# sections of Home (This branch, Branch flow, Project map), which have no key unless set.
MENU = ("home", "digest", "summary", "changes", "commits", "project")
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
    # Before Constellation and My day were removed.
    2: {**{f"view:{view}": str(n) for n, view in enumerate(
        ("home", "digest", "summary", "constellation", "changes", "commits", "planner", "project"), start=1)},
        "view:settings": ",", "theme": "t", "uncommitted": "u", "sync": "s", "next-file": "j", "previous-file": "k"},
}
SHORTCUTS_VERSION = 3
# Home's widgets in their default order, and the ones hidden until the person shows them.
WIDGETS = ("ai", "projects", "branch", "flow", "map", "calendar")
DEFAULT_HIDDEN = ("map", "calendar")
DEFAULTS = {
    "theme": "dark",
    # None until the person picks: the dashboard asks once.
    "uncommitted": None,
    "shortcuts": DEFAULT_SHORTCUTS,
}
MAX_KEY = 20
# The AIs that can write explanations (see providers.py), and the longest model name taken.
AI_PROVIDERS = ("claude", "codex", "ollama", "antigravity")
MAX_MODEL = 100


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
    ai = saved.get("ai") or {}
    return {"theme": saved.get("theme", DEFAULTS["theme"]), "uncommitted": saved.get("uncommitted"),
            "shortcuts": {**defaults, **chosen}, "home": _layout(saved.get("home")),
            "ai": {"provider": ai.get("provider") if ai.get("provider") in AI_PROVIDERS else "claude",
                   "models": {k: v for k, v in (ai.get("models") or {}).items() if k in AI_PROVIDERS and isinstance(v, str)}}}


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
        elif name == "home":
            current["home"] = _checked_layout(value)
        elif name == "ai":
            current["ai"] = _ai({**current["ai"], **value} if isinstance(value, dict) else value, current["ai"])
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


def _ai(value, current: dict) -> dict:
    """Which AI writes explanations, and the model picked for each; models merge one AI at a
    time, and an empty model means that AI's default."""
    if not isinstance(value, dict):
        raise PreferenceError("ai is {provider, models}")
    provider = value.get("provider", current["provider"])
    if provider not in AI_PROVIDERS:
        raise PreferenceError(f"the AI is one of {', '.join(AI_PROVIDERS)}")
    models = value.get("models", {})
    if not isinstance(models, dict) or any(k not in AI_PROVIDERS or not isinstance(v, str) or len(v) > MAX_MODEL
                                           for k, v in models.items()):
        raise PreferenceError(f"models maps an AI to a model name of at most {MAX_MODEL} characters")
    merged = {**current["models"], **{k: v.strip() for k, v in models.items()}}
    return {"provider": provider, "models": {k: v for k, v in merged.items() if v}}


def _layout(saved) -> dict:
    """The saved layout made whole: widgets it doesn't know are dropped, and widgets added
    in a later version join at the end, hidden if they're hidden by default."""
    saved = saved if isinstance(saved, dict) else {}
    order = [w for w in dict.fromkeys(saved.get("order") or []) if w in WIDGETS]
    new = [w for w in WIDGETS if w not in order]
    hidden = [w for w in saved.get("hidden", DEFAULT_HIDDEN) if w in WIDGETS] if "order" in saved else list(DEFAULT_HIDDEN)
    hidden += [w for w in new if w in DEFAULT_HIDDEN and "order" in saved]
    return {"order": order + new, "hidden": [w for w in WIDGETS if w in hidden]}


def _checked_layout(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("order"), list) or not isinstance(value.get("hidden", []), list):
        raise PreferenceError("home is {order, hidden}, each a list of widgets")
    unknown = [w for w in [*value["order"], *value.get("hidden", [])] if w not in WIDGETS]
    if unknown:
        raise PreferenceError(f"no widget called {unknown[0]!r}; they are {', '.join(WIDGETS)}")
    return _layout({"order": value["order"], "hidden": value.get("hidden", [])})


def reset_home() -> dict:
    current = get()
    current["home"] = _layout(None)
    _save(current)
    return current


def ai_choice() -> tuple[str, str | None]:
    """The AI that writes explanations and the model picked for it (None: its default)."""
    ai = get()["ai"]
    return ai["provider"], ai["models"].get(ai["provider"]) or None


def _save(preferences: dict) -> None:
    # Only the keys the person changed are stored, so a later version's defaults reach the rest.
    chosen = {action: key for action, key in preferences["shortcuts"].items() if DEFAULT_SHORTCUTS.get(action, "") != key}
    stored = {"theme": preferences["theme"], "uncommitted": preferences["uncommitted"], "shortcuts": chosen,
              "shortcuts_version": SHORTCUTS_VERSION, "ai": preferences["ai"], "home": preferences["home"]}
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, json.dumps(stored)))
    conn.close()

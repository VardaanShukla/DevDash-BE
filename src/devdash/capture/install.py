"""`devdash init` registers a repo; its own git history needs no hooks.
`devdash connect claude` opts that repo into Claude tracking: a git post-commit hook
and the user-level Claude Code hooks. `devdash disconnect claude` undoes it."""

import json
import os
import shlex
import sys
from pathlib import Path

from devdash import git
from devdash.store import connect, now

MARKER = "# devdash: records commits"
RECORD = "-m devdash record"

# Claude Code hook event -> devdash record event. PreToolUse snapshots files before the
# AI edits them, which is what lets a turn's diff leave out manual edits.
CLAUDE_EVENTS = {
    "UserPromptSubmit": ("user-prompt-submit", None),
    "PreToolUse": ("pre-tool-use", "Edit|MultiEdit|Write|NotebookEdit"),
    "Stop": ("stop", None),
}


class InitError(RuntimeError):
    pass


def init(path: Path) -> Path:
    """Tracks the repo at path. A project DevDash already knows from another folder (it was
    moved, renamed or cloned again, and the old folder is gone) keeps its history and its
    Claude setting instead of starting over."""
    root = _root(path)
    first = git.root_commit(root)
    conn = connect()
    with conn:
        if conn.execute("SELECT 1 FROM repos WHERE path = ?", (str(root),)).fetchone():
            conn.execute("UPDATE repos SET root_commit = COALESCE(root_commit, ?) WHERE path = ?",
                         (first, str(root)))
            moved = None
        else:
            moved = _moved(conn, root, first)
            if moved is None:
                conn.execute(
                    "INSERT INTO repos (path, name, added_at, root_commit) VALUES (?, ?, ?, ?)",
                    (str(root), root.name, now(), first),
                )
    conn.close()
    if moved is not None and moved["claude"]:
        install_git_hook(root)
    return root


def find_moved(conn, root: Path):
    """The tracked repo that now lives at root, if it was moved there; its path is updated.
    For hooks firing in a folder DevDash doesn't know yet."""
    with conn:
        return _moved(conn, root, git.root_commit(root))


def _moved(conn, root: Path, first: str | None):
    # Only repos whose folder is gone, so two clones side by side stay two projects.
    gone = [row for row in conn.execute("SELECT id, path, name, claude, root_commit FROM repos")
            if git.toplevel(Path(row["path"])) != Path(row["path"])]
    matches = [row for row in gone if first and row["root_commit"] == first]
    if not matches:
        # Tracked before first commits were stored: the same folder name will do.
        matches = [row for row in gone if row["root_commit"] is None
                   and row["name"].casefold() == root.name.casefold()]
    if len(matches) != 1:
        return None
    [row] = matches
    conn.execute("UPDATE repos SET path = ?, name = ?, root_commit = COALESCE(?, root_commit) WHERE id = ?",
                 (str(root), root.name, first, row["id"]))
    return row


def refresh() -> list[str]:
    """Brings every tracked repo's setup up to date with this install: hooks name the
    Python that runs DevDash, which changes when it's reinstalled or moved. Returns
    what couldn't be fixed, one line each."""
    conn = connect()
    rows = conn.execute("SELECT path, claude FROM repos").fetchall()
    conn.close()
    problems = []
    for row in rows:
        root = Path(row["path"])
        if git.toplevel(root) is None:
            continue
        init(root)  # stores the first commit for repos tracked before it was
        if row["claude"]:
            try:
                install_git_hook(root)
            except (InitError, OSError, git.GitError) as error:
                problems.append(str(error))
    if any(row["claude"] for row in rows):
        try:
            install_claude_hooks()
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"Claude Code hooks: {error}")
    return problems


def connect_claude(path: Path) -> Path:
    root = init(path)
    install_git_hook(root)
    install_claude_hooks()
    _remove_repo_claude_hooks(root)
    _exclude_from_git(root, "/.devdash/")
    conn = connect()
    with conn:
        conn.execute("UPDATE repos SET claude = 1 WHERE path = ?", (str(root),))
    conn.close()
    return root


def disconnect_claude(path: Path) -> Path:
    """Stops Claude tracking for a repo. Recorded history is kept."""
    root = _root(path)
    remove_git_hook(root)
    conn = connect()
    with conn:
        conn.execute("UPDATE repos SET claude = 0 WHERE path = ?", (str(root),))
        still_connected = conn.execute("SELECT 1 FROM repos WHERE claude = 1").fetchone()
    conn.close()
    if not still_connected:
        remove_claude_hooks()
    return root


def remove(repo_id: int) -> bool:
    """Stops tracking a repo and forgets what DevDash recorded about it. The repo's own
    files and git history are untouched; DevDash's git hook comes out if Claude was
    connected. Works even when the folder has moved or been deleted."""
    conn = connect()
    row = conn.execute("SELECT path, claude FROM repos WHERE id = ?", (repo_id,)).fetchone()
    conn.close()
    if row is None:
        return False
    path = Path(row["path"])
    if row["claude"] and git.toplevel(path) is not None:
        disconnect_claude(path)
    conn = connect()
    with conn:
        conn.execute("DELETE FROM undone WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM turn_files WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM turn_repos WHERE repo_id = ?", (repo_id,))
        # A session may have touched other repos too, so it is detached, not deleted;
        # turns left with nothing recorded go, then sessions left with no turns.
        conn.execute("UPDATE sessions SET repo_id = NULL WHERE repo_id = ?", (repo_id,))
        conn.execute(
            """DELETE FROM turns WHERE ended_at IS NOT NULL
               AND id NOT IN (SELECT turn_id FROM changes WHERE turn_id IS NOT NULL)
               AND id NOT IN (SELECT turn_id FROM turn_files)
               AND session_id IN (SELECT id FROM sessions WHERE repo_id IS NULL)"""
        )
        conn.execute(
            "DELETE FROM sessions WHERE repo_id IS NULL AND id NOT IN (SELECT session_id FROM turns)"
        )
        conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        still_connected = conn.execute("SELECT 1 FROM repos WHERE claude = 1").fetchone()
    conn.close()
    if row["claude"] and not still_connected:
        remove_claude_hooks()
    return True


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write(path: Path, text: str) -> None:
    # UTF-8 with \n endings on every system: sh can't run a hook with \r\n endings.
    path.write_text(text, encoding="utf-8", newline="\n")


def _root(path: Path) -> Path:
    root = git.toplevel(path)
    if root is None:
        raise InitError(f"{path} is not inside a git repository")
    return root


def record_command(event: str) -> str:
    # The absolute interpreter path keeps hooks working when devdash is not on PATH;
    # -I stops a folder named devdash in the repo from shadowing the package. Forward
    # slashes, since on Windows these run in Git's sh, where a backslash escapes.
    return f"{shlex.quote(Path(sys.executable).as_posix())} -I {RECORD} {event}"


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def install_claude_hooks() -> Path:
    # User-level hooks fire in every Claude Code session, wherever it starts (a repo,
    # or the parent folder of several). `devdash record` exits at once unless an edited
    # file is inside a tracked repo.
    settings_path = claude_settings_path()
    text = _read(settings_path) if settings_path.exists() else ""
    settings = json.loads(text) if text.strip() else {}
    hooks = settings.setdefault("hooks", {})
    for claude_event, (event, matcher) in CLAUDE_EVENTS.items():
        groups = [g for g in hooks.get(claude_event, []) if not _is_ours(g)]
        group = {"hooks": [{"type": "command", "command": record_command(event)}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks[claude_event] = [*groups, group]
    new = json.dumps(settings, indent=2) + "\n"
    if new != text:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        _write(settings_path, new)
    return settings_path


def remove_claude_hooks() -> None:
    settings_path = claude_settings_path()
    if not settings_path.exists():
        return
    settings = json.loads(_read(settings_path))
    _drop_our_hooks(settings)
    _write(settings_path, json.dumps(settings, indent=2) + "\n")


def _drop_our_hooks(settings: dict) -> None:
    hooks = settings.get("hooks", {})
    for claude_event in list(hooks):
        hooks[claude_event] = [g for g in hooks[claude_event] if not _is_ours(g)]
        if not hooks[claude_event]:
            del hooks[claude_event]
    if not hooks:
        settings.pop("hooks", None)


def _remove_repo_claude_hooks(root: Path) -> None:
    # Version 0.1.0 put the hooks in the repo; with user-level hooks they would record twice.
    settings_path = root / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return
    settings = json.loads(_read(settings_path))
    _drop_our_hooks(settings)
    if settings:
        _write(settings_path, json.dumps(settings, indent=2) + "\n")
    else:
        settings_path.unlink()


def install_git_hook(root: Path) -> Path:
    hook = git.hooks_dir(root) / "post-commit"
    ours = f"{MARKER}\n{record_command('post-commit')} || true\n"
    if not hook.exists():
        hook.parent.mkdir(parents=True, exist_ok=True)
        _write(hook, "#!/bin/sh\n" + ours)
    else:
        text = _read(hook)
        first = text.splitlines()[0] if text else ""
        if not first.startswith("#!") or not any(sh in first for sh in ("sh", "bash", "zsh")):
            raise InitError(
                f"{hook} is not a shell script, so devdash can't add to it. "
                f"Add this line to it yourself: {record_command('post-commit')}"
            )
        lines = text.splitlines(keepends=True)
        if MARKER in text:
            # Replace our two lines, e.g. after Python moved.
            at = next(i for i, line in enumerate(lines) if line.startswith(MARKER))
            lines[at : at + 2] = [ours]
        else:
            lines.append(("" if text.endswith("\n") else "\n") + ours)
        _write(hook, "".join(lines))
    os.chmod(hook, 0o755)
    return hook


def remove_git_hook(root: Path) -> None:
    hook = git.hooks_dir(root) / "post-commit"
    if not hook.exists() or MARKER not in _read(hook):
        return
    lines = _read(hook).splitlines(keepends=True)
    at = next(i for i, line in enumerate(lines) if line.startswith(MARKER))
    del lines[at : at + 2]
    if "".join(lines).strip() in ("", "#!/bin/sh"):
        hook.unlink()  # only ours was in it
    else:
        _write(hook, "".join(lines))


def _exclude_from_git(root: Path, pattern: str) -> None:
    # .git/info/exclude ignores the path on this machine without editing .gitignore.
    exclude = (root / git.run(root, "rev-parse", "--git-path", "info/exclude").strip()).resolve()
    text = _read(exclude) if exclude.exists() else ""
    if pattern not in text.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        _write(exclude, text + ("" if not text or text.endswith("\n") else "\n") + pattern + "\n")


def git_hook_installed(root: Path) -> bool:
    hook = git.hooks_dir(root) / "post-commit"
    return hook.exists() and MARKER in _read(hook)


def claude_hooks_installed() -> bool:
    settings_path = claude_settings_path()
    if not settings_path.exists():
        return False
    try:
        hooks = json.loads(_read(settings_path)).get("hooks", {})
    except (json.JSONDecodeError, AttributeError):
        return False
    return all(any(_is_ours(g) for g in hooks.get(event, [])) for event in CLAUDE_EVENTS)


def setup_steps(root: Path, claude: bool) -> list[dict]:
    """What it takes to use DevDash in a repo, each step with whether it is done.
    `done` is None where DevDash can't check (something the user does elsewhere)."""
    steps = [
        {"id": "track", "title": "Track the project",
         "detail": "DevDash reads your branches and commits straight from git. Nothing is added to the project.",
         "command": f"devdash init {shlex.quote(str(root))}", "done": True},
        {"id": "serve", "title": "Start DevDash",
         "detail": "Run this in a terminal and leave it running, then open http://127.0.0.1:8765.",
         "command": "devdash serve", "done": None},
    ]
    if not claude:
        steps.append({"id": "ai", "title": "Optional: record your AI tool",
                      "detail": "Pick your AI coding tool to also record each request it worked on, next to the change it made.",
                      "command": f"devdash connect claude {shlex.quote(str(root))}", "done": False})
        return steps
    steps += [
        {"id": "git-hook", "title": "Git hook installed",
         "detail": "Links each commit to the AI request behind it. Added to any hook you already have.",
         "command": None, "done": git_hook_installed(root)},
        {"id": "claude-hooks", "title": "Claude Code hooks installed",
         "detail": f"Records Claude Code edits as they happen. Lives in {claude_settings_path()}.",
         "command": None, "done": claude_hooks_installed()},
        {"id": "restart", "title": "Restart open Claude Code sessions",
         "detail": "Sessions that were already open don't load the new hooks until restarted.",
         "command": None, "done": None},
        {"id": "app-instructions", "title": "For Claude app chats: add the project instructions",
         "detail": "App chats edit files outside Claude Code. Paste this into the Claude project's instructions "
                   "so Claude notes each request before it commits.",
         "command": INSTRUCTIONS.rstrip(), "done": None},
    ]
    return steps


AI_TOOLS = [
    {"id": "claude", "name": "Claude", "available": True,
     "description": "Records each request to Claude Code or a Claude app chat, with the change it made."},
    {"id": "copilot", "name": "GitHub Copilot", "available": False,
     "description": "Coming later. Copilot commits are already marked as AI from their co-author line."},
    {"id": "cursor", "name": "Cursor", "available": False,
     "description": "Coming later. Cursor commits are already marked as AI from their co-author line."},
]


INSTRUCTIONS = """\
This repo is tracked by DevDash, which records what AI changes and why.
When you change files here:
1. Before each commit, append the user's request that led to the change to
   .devdash/prompt.md at the repo root, word for word. Create the file if needed.
   Separate requests with a line containing only ---.
2. Commit each logical change on its own, and end the message with
   Co-Authored-By: Claude <noreply@anthropic.com>
3. Never commit .devdash/ (git already ignores it on this machine).
"""


def _is_ours(group: dict) -> bool:
    return any(RECORD in h.get("command", "") for h in group.get("hooks", []))

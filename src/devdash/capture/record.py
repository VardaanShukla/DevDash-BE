"""Turns hook events into rows. Runs inside Claude Code and git hooks, so it must
be fast, print nothing (Claude Code adds a prompt hook's stdout to the chat) and
never fail the hook."""

import difflib
import json
import os
import re
import sqlite3
import sys
import traceback
from fnmatch import fnmatch
from pathlib import Path

from devdash import git, paths
from devdash.store import connect, now

# Set on Claude Code runs that DevDash starts itself (summaries), so they are not recorded.
INTERNAL_ENV = "DEVDASH_INTERNAL"

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
MAX_FILE_BYTES = 1_000_000
IGNORED = [
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "*.min.js", "*.min.css", "*.map",
    "node_modules/*", "*/node_modules/*", "dist/*", "build/*", "*/__pycache__/*", "__pycache__/*",
    ".git/*", ".venv/*", "venv/*",
]
UNKNOWN_PROMPT = "(prompt not captured)"
# Where Claude writes the request behind its changes before committing, for tools whose
# edits never pass through Claude Code (the Claude app). Kept out of git by `devdash init`.
PROMPT_FILE = ".devdash/prompt.md"
HOOK_LOG_MAX_BYTES = 1_000_000

# Commits an AI tool made or helped make. Edits that never pass through Claude Code's
# own tools (a cloud session writing files, a shell command) still get attributed this way.
AI_TRAILER = re.compile(
    r"^Co-Authored-By:.*(claude|noreply@anthropic\.com|copilot|cursor)", re.IGNORECASE | re.MULTILINE
)


def run_hook(event: str, stdin=sys.stdin) -> int:
    if os.environ.get(INTERNAL_ENV):
        return 0
    try:
        raw = stdin.read() if event != "post-commit" else ""
        payload = json.loads(raw) if raw.strip() else {}
        conn = connect()
        try:
            handle(conn, event, payload, cwd=Path(payload.get("cwd") or os.getcwd()))
        finally:
            conn.close()
        _log_call(event, payload)
    except Exception:
        with paths.error_log().open("a") as log:
            log.write(f"{now()} {event}\n{traceback.format_exc()}\n")
    return 0


def _log_call(event: str, payload: dict) -> None:
    # One line per hook call, without prompt text, so a missed edit can be traced.
    log = paths.hook_log()
    if log.exists() and log.stat().st_size > HOOK_LOG_MAX_BYTES:
        log.replace(log.with_suffix(".log.1"))
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    with log.open("a") as out:
        out.write(f"{now()} {event} session={str(payload.get('session_id', '-'))[:8]} "
                  f"tool={payload.get('tool_name', '-')} file={target or '-'}\n")


def handle(conn: sqlite3.Connection, event: str, payload: dict, cwd: Path) -> None:
    repos = conn.execute("SELECT id, path FROM repos WHERE claude = 1").fetchall()
    if not repos:
        return
    cwd = cwd.resolve()
    with conn:
        if event == "post-commit":
            root = git.toplevel(cwd)
            repo = _repo_for(repos, root) if root else None
            if repo is not None:
                _commit(conn, repo)
        elif event == "user-prompt-submit":
            _prompt(conn, repos, payload, cwd)
        elif event == "pre-tool-use":
            _before_edit(conn, repos, payload, cwd)
        elif event == "stop":
            turn = _open_turn(conn, payload.get("session_id"))
            if turn is not None:
                _finish_turn(conn, turn)
        else:
            raise ValueError(f"unknown event {event!r}")


def _prompt(conn, repos, payload, cwd: Path) -> None:
    session_id = payload["session_id"]
    _ensure_session(conn, _repo_for(repos, cwd), session_id)
    # A turn left open means its Stop never fired (crash, interrupt); close it first.
    stale = _open_turn(conn, session_id)
    if stale is not None:
        _finish_turn(conn, stale)
    _new_turn(conn, _repo_for(repos, cwd), session_id, payload.get("prompt") or UNKNOWN_PROMPT)


def _before_edit(conn, repos, payload, cwd: Path) -> None:
    if payload.get("tool_name") not in EDIT_TOOLS:
        return
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return
    # The file decides the repo, not the session's folder: a session started in the
    # parent of several repos still edits files inside each of them.
    absolute = (cwd / target).resolve()
    repo = _repo_for(repos, absolute)
    if repo is None:
        return
    relative = absolute.relative_to(repo["path"]).as_posix()
    if _ignored(relative):
        return

    session_id = payload["session_id"]
    _ensure_session(conn, _repo_for(repos, cwd), session_id)
    turn = _open_turn(conn, session_id)
    if turn is None:
        # Hooks were installed mid-session, so the prompt was missed.
        turn = _new_turn(conn, repo, session_id, UNKNOWN_PROMPT)
    elif turn["branch"] is None:
        conn.execute("UPDATE turns SET branch = ? WHERE id = ?", (git.branch(Path(repo["path"])), turn["id"]))

    before = None
    if absolute.exists():
        before = _read_text(absolute)
        if before is None:
            return  # binary or too large
    # Only the first edit in a turn keeps its snapshot.
    conn.execute(
        "INSERT OR IGNORE INTO turn_files (turn_id, repo_id, file_path, before_content)"
        " VALUES (?, ?, ?, ?)",
        (turn["id"], repo["id"], relative, before),
    )


def _finish_turn(conn, turn) -> None:
    files = conn.execute(
        "SELECT f.repo_id, r.path, f.file_path, f.before_content"
        " FROM turn_files f JOIN repos r ON r.id = f.repo_id WHERE f.turn_id = ?",
        (turn["id"],),
    ).fetchall()
    recorded = 0
    for row in files:
        current_path = Path(row["path"]) / row["file_path"]
        after = None
        if current_path.exists():
            after = _read_text(current_path)
            if after is None:
                continue  # became binary or too large
        diff = unified_diff(row["file_path"], row["before_content"], after)
        if diff:
            conn.execute(
                "INSERT INTO changes (repo_id, turn_id, file_path, diff, source, created_at)"
                " VALUES (?, ?, ?, ?, 'ai', ?)",
                (row["repo_id"], turn["id"], row["file_path"], diff, now()),
            )
            recorded += 1
    conn.execute("DELETE FROM turn_files WHERE turn_id = ?", (turn["id"],))
    session = conn.execute(
        "SELECT s.id, s.repo_id FROM sessions s JOIN turns t ON t.session_id = s.id WHERE t.id = ?",
        (turn["id"],),
    ).fetchone()
    if not recorded and session["repo_id"] is None:
        # A session outside any tracked repo that changed nothing in one: keep no trace of it.
        conn.execute("DELETE FROM turns WHERE id = ?", (turn["id"],))
        conn.execute(
            "DELETE FROM sessions WHERE id = ? AND NOT EXISTS (SELECT 1 FROM turns WHERE session_id = ?)",
            (session["id"], session["id"]),
        )
        return
    conn.execute("UPDATE turns SET ended_at = ? WHERE id = ?", (now(), turn["id"]))


def _commit(conn, repo) -> None:
    root = Path(repo["path"])
    info = git.commit_info(root)
    sha = info["sha"]
    if conn.execute("SELECT 1 FROM commits WHERE sha = ?", (sha,)).fetchone():
        return
    conn.execute(
        "INSERT INTO commits (sha, repo_id, branch, message, authored_at) VALUES (?, ?, ?, ?, ?)",
        (sha, repo["id"], git.branch(root), info["message"], info["authored_at"]),
    )
    prompt_turn = _take_prompt_file(conn, repo)
    source = "ai" if prompt_turn or AI_TRAILER.search(info["message"]) else "human"
    for file_path in git.commit_files(root, sha):
        linked = conn.execute(
            "UPDATE changes SET commit_sha = ?"
            " WHERE repo_id = ? AND file_path = ? AND commit_sha IS NULL AND source = 'ai'"
            # An undone change didn't land in this commit, even if its file did.
            " AND (turn_id IS NULL OR turn_id NOT IN (SELECT turn_id FROM undone WHERE turn_id IS NOT NULL))",
            (sha, repo["id"], file_path),
        ).rowcount
        if linked or _ignored(file_path):
            continue
        diff = git.commit_file_diff(root, sha, file_path)
        if diff:
            conn.execute(
                "INSERT INTO changes (repo_id, turn_id, commit_sha, file_path, diff, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo["id"], prompt_turn, sha, file_path, diff, source, now()),
            )


def _take_prompt_file(conn, repo) -> int | None:
    """Turns a waiting prompt file into a turn for this commit, then clears it."""
    path = Path(repo["path"]) / PROMPT_FILE
    text = _read_text(path) if path.exists() else None
    if not text or not text.strip():
        return None
    session_id = f"git:{repo['id']}"  # one pseudo-session per repo for commit-captured prompts
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, repo_id, tool, started_at) VALUES (?, ?, 'git', ?)",
        (session_id, repo["id"], now()),
    )
    turn = _new_turn(conn, repo, session_id, text.strip())
    conn.execute("UPDATE turns SET ended_at = started_at WHERE id = ?", (turn["id"],))
    path.unlink()
    return turn["id"]


def unified_diff(file_path: str, before: str | None, after: str | None) -> str:
    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile="/dev/null" if before is None else f"a/{file_path}",
        tofile="/dev/null" if after is None else f"b/{file_path}",
    )
    # Mark missing final newlines so each line of the diff ends with one.
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def _ensure_session(conn, repo, session_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, repo_id, started_at) VALUES (?, ?, ?)",
        (session_id, repo["id"] if repo else None, now()),
    )


def _open_turn(conn, session_id: str | None):
    if not session_id:
        return None
    return conn.execute(
        "SELECT id, branch FROM turns WHERE session_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def _new_turn(conn, repo, session_id: str, prompt: str):
    branch = git.branch(Path(repo["path"])) if repo else None
    cursor = conn.execute(
        "INSERT INTO turns (session_id, branch, prompt, started_at) VALUES (?, ?, ?, ?)",
        (session_id, branch, prompt, now()),
    )
    return {"id": cursor.lastrowid, "branch": branch}


def _repo_for(repos, path: Path):
    """The tracked repo containing path, innermost first for nested repos."""
    matches = [r for r in repos if path == Path(r["path"]) or path.is_relative_to(r["path"])]
    return max(matches, key=lambda r: len(r["path"]), default=None)


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _ignored(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return any(fnmatch(relative, pattern) or fnmatch(name, pattern) for pattern in IGNORED)

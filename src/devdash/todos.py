"""A to-do list. Each item is for everywhere, for one project, or for one branch of a
project, so a branch's page can show what's left before it merges."""

from devdash.store import connect
from devdash.store.db import now

MAX_TEXT = 500
# Finished items kept on the list, newest first; older ones stay in the database.
DONE_SHOWN = 30


class TodoError(ValueError):
    pass


def items() -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM todos WHERE done_at IS NULL ORDER BY created_at, id) "
        "UNION ALL SELECT * FROM (SELECT * FROM todos WHERE done_at IS NOT NULL ORDER BY done_at DESC LIMIT ?)",
        (DONE_SHOWN,),
    ).fetchall()
    conn.close()
    return [_item(row) for row in rows]


def add(text: str, repo_id: int | None, branch: str | None) -> dict:
    text = _text(text)
    if branch and repo_id is None:
        raise TodoError("a branch needs its project")
    conn = connect()
    with conn:
        if repo_id is not None and not conn.execute("SELECT 1 FROM repos WHERE id = ?", (repo_id,)).fetchone():
            raise TodoError("no such project")
        cursor = conn.execute("INSERT INTO todos (repo_id, branch, text, created_at) VALUES (?, ?, ?, ?)",
                              (repo_id, branch or None, text, now()))
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return _item(row)


def update(todo_id: int, text: str | None = None, done: bool | None = None) -> dict | None:
    conn = connect()
    with conn:
        if text is not None:
            conn.execute("UPDATE todos SET text = ? WHERE id = ?", (_text(text), todo_id))
        if done is not None:
            # Ticking an already finished item keeps when it was finished.
            conn.execute("UPDATE todos SET done_at = CASE WHEN ? THEN COALESCE(done_at, ?) END WHERE id = ?",
                         (done, now(), todo_id))
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    conn.close()
    return _item(row) if row else None


def remove(todo_id: int) -> bool:
    conn = connect()
    with conn:
        removed = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,)).rowcount
    conn.close()
    return removed > 0


def _text(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise TodoError("write what to do")
    if len(text) > MAX_TEXT:
        raise TodoError(f"keep it under {MAX_TEXT} characters")
    return text


def _item(row) -> dict:
    return {"id": row["id"], "text": row["text"], "repo_id": row["repo_id"], "branch": row["branch"],
            "created_at": row["created_at"], "done_at": row["done_at"]}

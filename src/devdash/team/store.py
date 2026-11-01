"""The team server's own database, apart from the person's DevDash database: its people,
organisations, teams, projects, and the explanations shared with them. It lives in ~/.devdash/team
(DEVDASH_TEAM_HOME overrides it), so hosting a team server never touches recorded history."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from devdash import paths

SCHEMA = [
    """
    -- An account. admin: the server's operator, who claimed it with the setup code.
    CREATE TABLE people (
        id            INTEGER PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name          TEXT NOT NULL,
        password      TEXT NOT NULL,             -- scrypt$n$r$p$salt$hash
        admin         INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    );
    -- A signed-in browser: only the token's hash is kept.
    CREATE TABLE logins (
        token_hash    TEXT PRIMARY KEY,
        person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL
    );
    CREATE TABLE orgs (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        created_at    TEXT NOT NULL
    );
    CREATE TABLE members (
        org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        role          TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
        joined_at     TEXT NOT NULL,
        PRIMARY KEY (org_id, person_id)
    );
    CREATE TABLE teams (
        id            INTEGER PRIMARY KEY,
        org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        name          TEXT NOT NULL COLLATE NOCASE,
        created_at    TEXT NOT NULL,
        UNIQUE (org_id, name)
    );
    CREATE TABLE team_people (
        team_id       INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        PRIMARY KEY (team_id, person_id)
    );
    -- One codebase in an organisation. key is a hash of its first commit, so everyone's
    -- clone finds the same project; it never grants access, which comes from its teams.
    CREATE TABLE projects (
        id            INTEGER PRIMARY KEY,
        org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        key           TEXT NOT NULL,
        name          TEXT NOT NULL,
        created_by    INTEGER REFERENCES people(id) ON DELETE SET NULL,
        created_at    TEXT NOT NULL,
        UNIQUE (org_id, key)
    );
    CREATE TABLE project_teams (
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        team_id       INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        PRIMARY KEY (project_id, team_id)
    );
    -- A link to join an organisation, for one person: only the hash of its token is kept.
    CREATE TABLE invites (
        id            INTEGER PRIMARY KEY,
        org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        token_hash    TEXT NOT NULL UNIQUE,
        role          TEXT NOT NULL CHECK (role IN ('admin', 'member')),
        team_id       INTEGER REFERENCES teams(id) ON DELETE CASCADE,
        created_by    INTEGER REFERENCES people(id) ON DELETE CASCADE,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        used_by       INTEGER REFERENCES people(id) ON DELETE SET NULL
    );
    -- A person's own DevDash, linked to one of their organisations.
    CREATE TABLE devices (
        id            INTEGER PRIMARY KEY,
        person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        name          TEXT NOT NULL,
        token_hash    TEXT NOT NULL UNIQUE,
        created_at    TEXT NOT NULL,
        last_seen     TEXT
    );
    -- Linking a DevDash: it shows user_code, the person approves it in their browser.
    CREATE TABLE device_codes (
        code_hash     TEXT PRIMARY KEY,          -- the secret the DevDash polls with
        user_code     TEXT NOT NULL UNIQUE,
        name          TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        person_id     INTEGER REFERENCES people(id) ON DELETE CASCADE,  -- set once approved
        org_id        INTEGER REFERENCES orgs(id) ON DELETE CASCADE
    );
    -- An explanation of a change someone shared: never the code, the diff or the prompt.
    CREATE TABLE shared (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        subject       TEXT NOT NULL,
        explanation   TEXT NOT NULL,             -- JSON: summary, change, improves, tradeoffs, check
        ref           TEXT NOT NULL,             -- the sharer's own id for it, so sharing twice updates
        shared_at     TEXT NOT NULL,
        UNIQUE (person_id, ref)
    );
    CREATE INDEX shared_by_project ON shared(project_id, shared_at);
    CREATE TABLE settings (
        name          TEXT PRIMARY KEY,
        value         TEXT NOT NULL
    );
    """,
]


def home() -> Path:
    path = Path(os.environ.get("DEVDASH_TEAM_HOME", paths.home() / "team"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(home() / "team.db", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(SCHEMA[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={number};\nCOMMIT;")
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def later(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat(timespec="seconds")

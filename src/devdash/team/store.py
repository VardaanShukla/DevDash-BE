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
    # Version 2: organisations run by their admins. Each has a code people join with; admins
    # make the projects (from a repo, or by linking one) and say who may see each, by team or
    # one person at a time.
    """
    ALTER TABLE orgs ADD COLUMN code TEXT;
    UPDATE orgs SET code = upper(substr(hex(randomblob(4)), 1, 4) || '-' || substr(hex(randomblob(4)), 1, 4));
    CREATE UNIQUE INDEX orgs_by_code ON orgs(code);

    CREATE TABLE projects_new (
        id            INTEGER PRIMARY KEY,
        org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        key           TEXT,                      -- hash of its first commit, once someone linked a clone
        repo          TEXT,                      -- github:owner/name or codecommit:region/name, when imported
        name          TEXT NOT NULL,
        created_by    INTEGER REFERENCES people(id) ON DELETE SET NULL,
        created_at    TEXT NOT NULL,
        UNIQUE (org_id, key),
        UNIQUE (org_id, repo)
    );
    INSERT INTO projects_new (id, org_id, key, name, created_by, created_at)
        SELECT id, org_id, key, name, created_by, created_at FROM projects;
    DROP TABLE projects;
    ALTER TABLE projects_new RENAME TO projects;

    -- One person given a project without their whole team.
    CREATE TABLE project_people (
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        PRIMARY KEY (project_id, person_id)
    );
    """,
    # Version 3: team chat. One channel for the whole organisation, one per team, and one for
    # each pair of people; text only.
    """
    CREATE TABLE chat_channels (
        id            INTEGER PRIMARY KEY,
        org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('org', 'team', 'direct')),
        team_id       INTEGER REFERENCES teams(id) ON DELETE CASCADE,
        person_a      INTEGER REFERENCES people(id) ON DELETE CASCADE,   -- direct: the lower id
        person_b      INTEGER REFERENCES people(id) ON DELETE CASCADE,
        created_at    TEXT NOT NULL
    );
    CREATE INDEX chat_channels_by_org ON chat_channels(org_id, kind);
    CREATE TABLE chat_messages (
        id            INTEGER PRIMARY KEY,
        channel_id    INTEGER NOT NULL REFERENCES chat_channels(id) ON DELETE CASCADE,
        person_id     INTEGER REFERENCES people(id) ON DELETE SET NULL,
        text          TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        deleted       INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX chat_messages_by_channel ON chat_messages(channel_id, id);
    -- How far each person has read each channel, for the unread counts.
    CREATE TABLE chat_reads (
        channel_id    INTEGER NOT NULL REFERENCES chat_channels(id) ON DELETE CASCADE,
        person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        last_id       INTEGER NOT NULL,
        PRIMARY KEY (channel_id, person_id)
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
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # Upgrades rebuild tables, which foreign keys would empty (dropping a table deletes its
    # rows' children); the setting can't change inside a transaction, so it's off around them.
    if version < len(SCHEMA):
        conn.execute("PRAGMA foreign_keys=OFF")
        for number, script in enumerate(SCHEMA[version:], start=version + 1):
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={number};\nCOMMIT;")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def later(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat(timespec="seconds")

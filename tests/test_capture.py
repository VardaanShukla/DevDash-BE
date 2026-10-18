import io
import json
import sqlite3
import subprocess

from conftest import git
from devdash.capture.install import claude_settings_path, connect_claude, disconnect_claude, init, record_command
from devdash.capture.record import run_hook
from devdash.store import connect
from devdash.store.db import MIGRATIONS


def send(event, repo, **payload):
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": event, **payload}
    assert run_hook(event, io.StringIO(json.dumps(payload))) == 0


def edit(repo, path):
    send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(repo / path)})


def user_settings():
    return json.loads(claude_settings_path().read_text())


def test_init_installs_hooks_once(repo, tmp_path):
    connect_claude(repo)
    second = tmp_path / "second"
    second.mkdir()
    git(second, "init", "-q")
    connect_claude(second)

    hooks = user_settings()["hooks"]
    assert {e: len(groups) for e, groups in hooks.items()} == {
        "UserPromptSubmit": 1, "PreToolUse": 1, "Stop": 1,
    }
    assert hooks["PreToolUse"][0]["matcher"] == "Edit|MultiEdit|Write|NotebookEdit"
    hook = (repo / ".git" / "hooks" / "post-commit").read_text()
    assert hook.count("devdash record post-commit") == 1
    assert connect().execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2
    assert git(repo, "status", "--porcelain") == ""  # nothing added to the repo


def test_init_keeps_other_hooks_and_moves_old_repo_hooks(repo):
    claude_settings_path().parent.mkdir()
    claude_settings_path().write_text(json.dumps({
        "model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify"}]}]},
    }))
    old = repo / ".claude" / "settings.local.json"
    old.parent.mkdir()
    old.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": record_command("stop")}]}]},
    }))
    existing = repo / ".git" / "hooks" / "post-commit"
    existing.write_text("#!/bin/sh\necho mine\n")

    connect_claude(repo)

    data = user_settings()
    assert data["model"] == "opus"
    assert [g["hooks"][0]["command"] for g in data["hooks"]["Stop"]] == ["notify", record_command("stop")]
    assert json.loads(old.read_text()) == {"permissions": {"allow": ["Bash(ls)"]}}
    assert existing.read_text().startswith("#!/bin/sh\necho mine\n")


def test_session_started_in_parent_folder_records_each_repo(tmp_path):
    parent = tmp_path / "Sankatmochan"
    ui, be = parent / "UI", parent / "BE"
    for path in (ui, be):
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "master")
        (path / "index.ts").write_text("export const a = 1\n")
        connect_claude(path)

    send("user-prompt-submit", parent, prompt="bump both")
    send("pre-tool-use", parent, tool_name="Edit", tool_input={"file_path": "UI/index.ts"})
    send("pre-tool-use", parent, tool_name="Edit", tool_input={"file_path": str(be / "index.ts")})
    (ui / "index.ts").write_text("export const a = 2\n")
    (be / "index.ts").write_text("export const a = 3\n")
    send("stop", parent)

    conn = connect()
    turn = conn.execute("SELECT prompt, branch, ended_at FROM turns").fetchone()
    assert (turn["prompt"], turn["branch"]) == ("bump both", "master")
    rows = conn.execute(
        "SELECT r.name, c.file_path FROM changes c JOIN repos r ON r.id = c.repo_id ORDER BY r.name"
    ).fetchall()
    assert [(r["name"], r["file_path"]) for r in rows] == [("BE", "index.ts"), ("UI", "index.ts")]


def test_session_outside_tracked_repos_leaves_no_trace(repo, tmp_path):
    connect_claude(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    send("user-prompt-submit", elsewhere, prompt="private question", session_id="other")
    send("pre-tool-use", elsewhere, tool_name="Write", tool_input={"file_path": "x.py"}, session_id="other")
    send("stop", elsewhere, session_id="other")

    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_upgrades_a_version_1_database(devdash_home, repo):
    devdash_home.mkdir()
    old = sqlite3.connect(devdash_home / "devdash.db")
    old.executescript(MIGRATIONS[0] + """
        PRAGMA user_version=1;
        INSERT INTO repos VALUES (1, '/r', 'r', 't');
        INSERT INTO sessions VALUES ('s', 1, 'claude-code', 't');
        INSERT INTO turns VALUES (1, 's', 'main', 'old prompt', 't', NULL);
        INSERT INTO turn_files VALUES (1, 'a.py', 'x');
    """)
    old.close()

    conn = connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert tuple(conn.execute("SELECT turn_id, repo_id, file_path FROM turn_files").fetchone()) == (1, 1, "a.py")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT claude FROM repos").fetchone()["claude"] == 1  # it had hooks
    conn.execute("INSERT INTO sessions (id, repo_id, started_at) VALUES ('new', NULL, 't')")


def test_turn_diff_excludes_manual_edits(repo):
    connect_claude(repo)
    (repo / "notes.md").write_text("draft\n")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-q", "-m", "notes")
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n# my own uncommitted tweak\n")

    send("user-prompt-submit", repo, prompt="make hello louder")
    edit(repo, "app.py")
    (repo / "app.py").write_text("def hello():\n    return 'HI'\n# my own uncommitted tweak\n")
    (repo / "notes.md").write_text("draft\nedited by hand\n")  # never touched by the AI
    send("stop", repo)

    conn = connect()
    turn = conn.execute("SELECT * FROM turns").fetchone()
    assert turn["prompt"] == "make hello louder"
    assert turn["branch"] == "main"
    assert turn["ended_at"] is not None
    changes = conn.execute("SELECT * FROM changes WHERE turn_id IS NOT NULL").fetchall()
    assert [(c["file_path"], c["source"]) for c in changes] == [("app.py", "ai")]
    diff = changes[0]["diff"]
    assert "-    return 'hi'\n+    return 'HI'\n" in diff
    assert "+# my own uncommitted tweak" not in diff


def test_commit_links_ai_change_and_records_human_change(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="add a goodbye")
    send("pre-tool-use", repo, tool_name="Write", tool_input={"file_path": "bye.py"})
    (repo / "bye.py").write_text("def bye():\n    return 'bye'\n")
    send("stop", repo)
    (repo / "README.md").write_text("# Project\n")

    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "goodbye and readme")  # fires the installed post-commit hook

    conn = connect()
    sha = git(repo, "rev-parse", "HEAD").strip()
    commit = conn.execute("SELECT * FROM commits").fetchone()
    assert (commit["sha"], commit["message"], commit["branch"]) == (sha, "goodbye and readme", "main")
    rows = conn.execute("SELECT file_path, source, commit_sha, diff FROM changes ORDER BY file_path").fetchall()
    assert [(r["file_path"], r["source"], r["commit_sha"]) for r in rows] == [
        ("README.md", "human", sha), ("bye.py", "ai", sha),
    ]
    assert rows[1]["diff"].startswith("--- /dev/null\n+++ b/bye.py\n")


def test_new_prompt_closes_turn_whose_stop_never_fired(repo):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="first")
    edit(repo, "app.py")
    (repo / "app.py").write_text("changed\n")
    send("user-prompt-submit", repo, prompt="second")

    conn = connect()
    turns = conn.execute("SELECT prompt, ended_at FROM turns ORDER BY id").fetchall()
    assert [(t["prompt"], t["ended_at"] is None) for t in turns] == [("first", False), ("second", True)]
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1


def test_ignores_internal_runs_unregistered_repos_and_lockfiles(repo, monkeypatch, tmp_path):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="update deps")
    edit(repo, "package-lock.json")
    send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(tmp_path / "outside.py")})
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    send("user-prompt-submit", other, prompt="not tracked", session_id="s2")
    send("stop", other, session_id="s2")
    monkeypatch.setenv("DEVDASH_INTERNAL", "1")
    send("user-prompt-submit", repo, prompt="summary run", session_id="s3")

    conn = connect()
    assert [r["prompt"] for r in conn.execute("SELECT prompt FROM turns")] == ["update deps"]
    assert conn.execute("SELECT COUNT(*) FROM turn_files").fetchone()[0] == 0


def test_bad_payload_never_fails_the_hook(repo, devdash_home):
    connect_claude(repo)
    assert run_hook("stop", io.StringIO("not json")) == 0
    assert "JSONDecodeError" in (devdash_home / "errors.log").read_text()


def test_installed_command_runs_end_to_end(repo):
    connect_claude(repo)
    payload = json.dumps({"session_id": "e2e", "cwd": str(repo), "prompt": "via the real command"})
    result = subprocess.run(record_command("user-prompt-submit"), shell=True, input=payload,
                            capture_output=True, text=True, cwd=repo)
    assert result.returncode == 0
    assert result.stdout == ""  # Claude Code would add any output to the chat
    assert connect().execute("SELECT prompt FROM turns").fetchone()["prompt"] == "via the real command"


def test_commit_with_ai_coauthor_counts_as_ai(repo, devdash_home):
    connect_claude(repo)
    (repo / "app.py").write_text("def hello():\n    return 'hey'\n")
    git(repo, "commit", "-q", "-am", "Tweak hello\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    (repo / "app.py").write_text("def hello():\n    return 'yo'\n")
    git(repo, "commit", "-q", "-am", "My own tweak")

    rows = connect().execute(
        "SELECT m.message, c.source, c.turn_id FROM changes c JOIN commits m ON m.sha = c.commit_sha ORDER BY c.id"
    ).fetchall()
    assert [(r["message"].splitlines()[0], r["source"], r["turn_id"]) for r in rows] == [
        ("Tweak hello", "ai", None), ("My own tweak", "human", None),
    ]
    assert "post-commit" in (devdash_home / "hooks.log").read_text()


def test_hook_log_names_the_tool_without_the_prompt(repo, devdash_home):
    connect_claude(repo)
    send("user-prompt-submit", repo, prompt="secret plan")
    send("pre-tool-use", repo, tool_name="Bash", tool_input={"command": "echo hi > a.txt"})
    log = (devdash_home / "hooks.log").read_text()
    assert "tool=Bash" in log
    assert "secret plan" not in log


def test_prompt_file_becomes_the_commits_turn(repo):
    connect_claude(repo)
    assert "/.devdash/" in (repo / ".git" / "info" / "exclude").read_text()
    (repo / ".devdash").mkdir()
    (repo / ".devdash" / "prompt.md").write_text("make hello shout\n---\nand add a docstring\n")
    (repo / "app.py").write_text('def hello():\n    """Greets."""\n    return "HI"\n')
    git(repo, "add", "-A")
    assert "prompt.md" not in git(repo, "status", "--porcelain")  # never committed
    git(repo, "commit", "-q", "-m", "Shout")

    conn = connect()
    turn = conn.execute("SELECT t.prompt, s.tool FROM turns t JOIN sessions s ON s.id = t.session_id").fetchone()
    assert (turn["prompt"], turn["tool"]) == ("make hello shout\n---\nand add a docstring", "git")
    change = conn.execute("SELECT file_path, source, turn_id FROM changes").fetchone()
    assert (change["file_path"], change["source"]) == ("app.py", "ai")
    assert change["turn_id"] is not None
    assert not (repo / ".devdash" / "prompt.md").exists()

    (repo / "app.py").write_text("x = 1\n")
    git(repo, "commit", "-q", "-am", "next")  # no prompt file this time
    assert conn.execute("SELECT source FROM changes ORDER BY id DESC").fetchone()["source"] == "human"


def test_init_alone_installs_nothing(repo):
    init(repo)
    assert not claude_settings_path().exists()
    assert not (repo / ".git" / "hooks" / "post-commit").exists()
    assert connect().execute("SELECT claude FROM repos").fetchone()["claude"] == 0
    send("user-prompt-submit", repo, prompt="not recorded")
    edit(repo, "app.py")
    (repo / "app.py").write_text("changed\n")
    send("stop", repo)
    assert connect().execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_disconnect_removes_hooks_but_keeps_history(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    connect_claude(repo)
    connect_claude(other)
    send("user-prompt-submit", repo, prompt="keep me")
    edit(repo, "app.py")
    (repo / "app.py").write_text("changed\n")
    send("stop", repo)

    disconnect_claude(repo)
    assert hook.read_text() == "#!/bin/sh\necho mine\n"
    assert "hooks" in json.loads(claude_settings_path().read_text())  # other repo still connected
    disconnect_claude(other)
    assert "hooks" not in json.loads(claude_settings_path().read_text())
    assert not (other / ".git" / "hooks" / "post-commit").exists()
    assert connect().execute("SELECT prompt FROM turns").fetchone()["prompt"] == "keep me"

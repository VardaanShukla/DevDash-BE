import io
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from conftest import git
from devdash.api.app import create_app
from devdash.capture.install import connect_claude
from devdash.capture.record import run_hook


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def send(event, repo, **payload):
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": event, **payload}
    assert run_hook(event, io.StringIO(json.dumps(payload))) == 0


def claude_edits(repo, prompt, files):
    send("user-prompt-submit", repo, prompt=prompt)
    for path, content in files.items():
        send("pre-tool-use", repo, tool_name="Edit", tool_input={"file_path": str(repo / path)})
        if content is None:
            (repo / path).unlink()
        else:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(content)
    send("stop", repo)


def test_digest_lists_claudes_prompts_with_functions_and_flags(repo):
    (repo / "auth").mkdir()
    (repo / "auth" / "login.py").write_text("def check(user, password):\n    return user.password == password\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "def test_hello():\n    assert True\n\n\ndef test_bye():\n    assert True\n")
    (repo / "app.py").write_text("def hello():\n    return 'hi'\n\n\ndef bye():\n    return 'bye'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "setup")
    connect_claude(repo)

    claude_edits(repo, "Say hello louder", {
        "app.py": "def hello():\n    return 'HI'\n\n\ndef bye():\n    return 'bye'\n\n\ndef wave():\n    return 'o/'\n",
    })
    git(repo, "commit", "-q", "-am", "Louder hello")  # the hook links Claude's change to it
    claude_edits(repo, "Make login faster and drop the slow test", {
        "auth/login.py": "def check(user, password):\n    return hmac.compare_digest(user.hash, hash(password))\n",
        "tests/test_app.py": "def test_hello():\n    assert True\n",
        "config.py": "API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n",
        "auth/keys.py": "LOGIN_PASSWORD = 'hunter2hunter2'\n",
    })
    # A change of your own, committed.
    (repo / "README.md").write_text("Hello\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "Readme")

    result = client().get("/api/digest").json()
    assert result["date"] == date.today().isoformat()
    assert result["claude"] == {"prompts": 2, "files": 5, "functions": 4, "additions": 8, "deletions": 6,
                                     "risky": 1, "uncommitted": 1}
    assert result["you"] == {"files": 1, "additions": 1, "deletions": 0, "commits": 1}

    newest, older = result["changes"]
    assert newest["prompt"] == "Make login faster and drop the slow test" and newest["commit"] is None
    assert newest["risky"] is True
    assert {f["kind"] for f in newest["flags"]} == {"auth", "tests", "secret"}
    by_path = {f["path"]: f for f in newest["files"]}
    assert by_path["auth/login.py"]["functions"] == [{"name": "check", "change": "modified"}]
    assert by_path["tests/test_app.py"]["flags"][0]["reason"] == "1 test deleted"
    # Blank lines between functions don't count as changing the one above them.
    assert by_path["tests/test_app.py"]["functions"] == [{"name": "test_bye", "change": "deleted"}]
    assert by_path["config.py"]["status"] == "added"
    [secret] = by_path["config.py"]["flags"]
    assert secret["line"] == "API_KEY = 'sk" + "\u2022" * 8 + "'"
    # A line shown for another reason is masked too.
    assert [f["line"] for f in by_path["auth/keys.py"]["flags"]] == ["LOGIN_PASSWORD = 'hu" + "\u2022" * 8 + "'"] * 2
    # Flagged for its folder, with the changed line that mentions a password.
    assert by_path["auth/login.py"]["flags"][0]["line"].startswith("return hmac.compare_digest")

    assert older["prompt"] == "Say hello louder" and older["commit"]["message"] == "Louder hello"
    assert older["risky"] is False and older["flags"] == []
    assert {(f["name"], f["change"]) for f in older["files"][0]["functions"]} == {("hello", "modified"),
                                                                               ("wave", "added")}


def test_other_days_and_bad_days(repo):
    connect_claude(repo)
    claude_edits(repo, "tweak", {"app.py": "def hello():\n    return 'yo'\n"})
    api = client()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    empty = api.get(f"/api/digest?day={yesterday}").json()
    assert empty["changes"] == [] and empty["claude"]["prompts"] == 0
    assert api.get("/api/digest?day=18-10-2026").status_code == 400


def test_ai_coauthored_commit_without_a_prompt_uses_its_message(repo):
    connect_claude(repo)
    (repo / "db").mkdir()
    (repo / "db" / "migration_002.sql").write_text("ALTER TABLE users ADD COLUMN age INTEGER;\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Add age\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    [change] = client().get("/api/digest").json()["changes"]
    assert change["prompt"] == "Add age" and change["prompt_recorded"] is False
    assert [f["kind"] for f in change["flags"]] == ["database"]


def test_undo_and_put_back_one_change(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n", "notes.md": "new file\n"})
    claude_edits(repo, "Add bye", {"bye.py": "def bye():\n    return 'bye'\n"})
    api = client()
    shout = next(c for c in api.get("/api/digest").json()["changes"] if c["prompt"] == "Shout")
    assert shout["undone_at"] is None

    done = api.post("/api/digest/undo", json={"change": shout["id"]})
    assert done.status_code == 200 and done.json()["undone_at"]
    # Only that prompt's edits come off; the later one stays.
    assert (repo / "app.py").read_text() == "def hello():\n    return 'hi'\n"
    assert not (repo / "notes.md").exists() and (repo / "bye.py").exists()
    assert api.post("/api/digest/undo", json={"change": shout["id"]}).status_code == 409
    listed = next(c for c in api.get("/api/digest").json()["changes"] if c["id"] == shout["id"])
    assert listed["undone_at"] == done.json()["undone_at"]

    assert api.post("/api/digest/put-back", json={"change": shout["id"]}).status_code == 200
    assert (repo / "app.py").read_text() == "def hello():\n    return 'HI'\n"
    assert (repo / "notes.md").read_text() == "new file\n"
    assert api.post("/api/digest/undo", json={"change": "turn-999-1"}).status_code == 404


def test_undo_refuses_when_a_file_changed_since(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n", "other.py": "x = 1\n"})
    (repo / "app.py").write_text("def hello():\n    return 'Hey'\n")  # your own edit afterwards
    api = client()
    [change] = api.get("/api/digest").json()["changes"]
    refused = api.post("/api/digest/undo", json={"change": change["id"]})
    assert refused.status_code == 409 and "app.py" in refused.json()["detail"]
    # All or nothing: the file that would have fitted is untouched too.
    assert (repo / "other.py").read_text() == "x = 1\n"
    assert (repo / "app.py").read_text() == "def hello():\n    return 'Hey'\n"


def test_undoing_a_committed_change_leaves_the_undo_uncommitted(repo):
    connect_claude(repo)
    claude_edits(repo, "Shout", {"app.py": "def hello():\n    return 'HI'\n"})
    git(repo, "commit", "-q", "-am", "Shout")
    api = client()
    [change] = api.get("/api/digest").json()["changes"]
    assert change["commit"]["message"] == "Shout"
    assert api.post("/api/digest/undo", json={"change": change["id"]}).status_code == 200
    assert (repo / "app.py").read_text() == "def hello():\n    return 'hi'\n"
    assert git(repo, "log", "-1", "--format=%s").strip() == "Shout"  # nothing committed
    assert git(repo, "status", "--porcelain").strip() == "M app.py"
    # Removing the project forgets the undo with everything else.
    [listed] = api.get("/api/repos").json()
    assert api.request("DELETE", f"/api/repos/{listed['id']}", json={}).status_code == 204
    assert api.get("/api/digest").json()["changes"] == []

import json
import os

from fastapi.testclient import TestClient

from conftest import git
from devdash.api.app import create_app
from devdash.capture.install import init
from devdash.functions import units


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def branch_with_changes(repo):
    init(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text(
        "def hello(name):\n"
        "    \"\"\"Greets someone.\"\"\"\n"
        "    if not name:\n"
        "        return 'hi'\n"
        "    return shout(name)\n"
        "\n"
        "\n"
        "def shout(text):\n"
        "    return text.upper()\n"
    )
    git(repo, "commit", "-q", "-am", "Greet people by name")
    [listed] = client().get("/api/repos").json()
    return listed["id"]


def test_functions_before_and_after_with_their_facts(repo):
    repo_id = branch_with_changes(repo)
    result = client().get(f"/api/repos/{repo_id}/functions", params={"path": "app.py"}).json()
    functions = {item["name"]: item for item in result["functions"]}
    assert set(functions) == {"hello", "shout"}

    hello = functions["hello"]
    assert hello["change"] == "modified"
    assert hello["before"]["code"] == "def hello():\n    return 'hi'"
    assert hello["after"]["doc"] == "Greets someone."
    assert hello["facts"]["params_added"] == ["name"]
    assert hello["facts"]["calls_added"] == ["shout"]
    assert hello["facts"]["decisions"] == [0, 1]
    assert hello["explanation"] is None

    shout = functions["shout"]
    assert shout["change"] == "added" and shout["before"] is None and shout["facts"] is None
    # A new function shows where it's used.
    assert [(use["path"], use["line"]) for use in shout["used_by"]] == [("app.py", 5)]


def test_functions_in_typescript():
    text = (
        "// Adds two numbers.\n"
        "export function add(a: number, b: number): number {\n"
        "  if (a < 0) { return b; }\n"
        "  return a + b;\n"
        "}\n"
        "\n"
        "export const Card = ({ title, onOpen }: Props) => {\n"
        "  const click = () => onOpen(title);\n"
        "  return <button onClick={click}>{title}</button>;\n"
        "};\n"
        "\n"
        "type Props = { title: string; onOpen: (t: string) => void };\n"
        "\n"
        "class Store {\n"
        "  load(id: string) {\n"
        "    return fetch(`/api/${id}`);\n"
        "  }\n"
        "}\n"
    )
    found = {unit["name"]: unit for unit in units("src/Card.tsx", text)}
    assert set(found) == {"add", "Card", "Props", "Store.load"}
    assert (found["add"]["start"], found["add"]["end"], found["add"]["doc"]) == (2, 5, "Adds two numbers.")
    assert found["add"]["params"] == ["a", "b"] and found["add"]["decisions"] == 1
    # The inner arrow function belongs to the component, not a unit of its own.
    assert found["Card"]["kind"] == "component" and (found["Card"]["start"], found["Card"]["end"]) == (7, 10)
    assert found["Card"]["params"] == ["title", "onOpen"]
    assert found["Props"]["kind"] == "type" and found["Store.load"]["kind"] == "method"
    assert found["Store.load"]["calls"] == ["fetch"]


def test_project_summary_from_its_files(repo):
    init(repo)
    (repo / "README.md").write_text(
        "# Shop\n\n[![build](x.svg)](ci)\n\nSells plants online. Orders ship next day.\n\n## Setup\n\nRun it.\n"
    )
    (repo / "package.json").write_text(json.dumps({
        "name": "shop", "scripts": {"dev": "vite"},
        "dependencies": {"react": "^18", "left-pad": "1"}, "devDependencies": {"vite": "^5"},
    }))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "readme")
    [listed] = client().get("/api/repos").json()

    summary = client().get(f"/api/repos/{listed['id']}/summary").json()
    assert summary["name"] == "Shop"
    assert summary["description"] == "Sells plants online."
    assert summary["readme"]["sections"] == ["Setup"]
    assert [t["name"] for t in summary["tech"]] == ["react", "vite"]
    assert summary["other_dependencies"] == 1
    assert summary["run"] == [{"where": "package.json", "name": "dev", "command": "vite"}]
    assert summary["history"]["commits"] == 2 and summary["history"]["weeks"][-1] == 2
    assert summary["explanation"] is None


def fake_claude(tmp_path, monkeypatch, answer: dict, is_error=False, error=None):
    """A `claude` command that answers with `answer` and counts its calls. Its sign-in
    prints a page address and waits for the code from that page."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    reply = json.dumps({"type": "result", "is_error": is_error,
                        "result": error or "Here you go:\n```json\n" + json.dumps(answer) + "\n```"})
    script = folder / "claude"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = 'auth status' ]; then echo '{\"loggedIn\": true}'; exit 0; fi\n"
        "if [ \"$1 $2\" = 'auth login' ]; then\n"
        "  echo 'If the browser didn'\\''t open, visit: https://claude.example/sign-in?x=1'\n"
        f"  read code; echo \"code $code\" >> {calls}; exit 0\n"
        "fi\n"
        f"cat > /dev/null\necho \"$DEVDASH_INTERNAL $*\" >> {calls}\n"
        f"cat <<'EOF'\n{reply}\nEOF\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return calls


def test_explaining_a_function_asks_claude_once(repo, tmp_path, monkeypatch):
    repo_id = branch_with_changes(repo)
    calls = fake_claude(tmp_path, monkeypatch, {
        "summary": "Says hello.", "change": "Now uses the name.", "improves": ["Friendlier"],
        "tradeoffs": ["Shouts"], "check": None, "extra": "dropped",
    })
    assert client().get("/api/ai").json() | {"detail": None} == {
        "tool": "claude", "available": True, "signed_in": True, "model": "haiku", "detail": None}

    request = {"path": "app.py", "name": "hello"}
    first = client().post(f"/api/repos/{repo_id}/explain/function", json=request)
    second = client().post(f"/api/repos/{repo_id}/explain/function", json=request)
    assert first.status_code == 200 and first.json() == second.json()
    assert first.json()["improves"] == ["Friendlier"] and "extra" not in first.json()
    # Run once, with tools off and marked so DevDash doesn't record it.
    [call] = calls.read_text().splitlines()
    assert call.startswith("1 -p") and "--tools" in call and "--model haiku" in call

    result = client().get(f"/api/repos/{repo_id}/functions", params={"path": "app.py"}).json()
    hello = next(item for item in result["functions"] if item["name"] == "hello")
    assert hello["explanation"]["summary"] == "Says hello."

    missing = client().post(f"/api/repos/{repo_id}/explain/function", json={"path": "app.py", "name": "nope"})
    assert missing.status_code == 404


def test_explaining_the_project_and_a_failed_answer(repo, tmp_path, monkeypatch):
    init(repo)
    [listed] = client().get("/api/repos").json()
    fake_claude(tmp_path, monkeypatch, {}, is_error=True)
    failed = client().post(f"/api/repos/{listed['id']}/explain/project", json={})
    assert failed.status_code == 502 and "Claude Code" in failed.json()["detail"]

    fake_claude(tmp_path, monkeypatch, {
        "overview": "A tiny app.", "how_it_works": ["It says hi."], "parts": [{"name": "Project root", "what": "All of it"}],
        "state": "Just started.",
    })
    made = client().post(f"/api/repos/{listed['id']}/explain/project", json={}).json()
    assert made["overview"] == "A tiny app."
    # A new commit keeps the description: it depends on what the project is, not its history.
    (repo / "notes.txt").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "notes")
    assert client().get(f"/api/repos/{listed['id']}/summary").json()["explanation"]["overview"] == "A tiny app."


def test_signing_in_from_the_dashboard(repo, tmp_path, monkeypatch):
    init(repo)
    [listed] = client().get("/api/repos").json()
    calls = fake_claude(tmp_path, monkeypatch, {}, is_error=True,
                        error="Failed to authenticate: OAuth session expired and could not be refreshed")
    failed = client().post(f"/api/repos/{listed['id']}/explain/project", json={})
    # Signed out: the page is told to offer signing in.
    assert failed.status_code == 424

    started = client().post("/api/ai/sign-in", json={}).json()
    assert started == {"state": "waiting", "url": "https://claude.example/sign-in?x=1", "detail": None}
    finished = client().post("/api/ai/sign-in/code", json={"code": " abc#123 "}).json()
    assert finished["state"] == "signed_in"
    assert "code abc#123" in calls.read_text().splitlines()


import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from devdash import explain
from devdash.api.app import create_app

ANSWER = {"summary": "Says hello.", "change": "Louder now.", "improves": ["clarity"], "tradeoffs": [], "check": None}
SUBJECT = {"path": "app.py", "kind": "function", "name": "hello", "change": "modified",
           "before": "def hello():\n    return 'hi'\n", "after": "def hello():\n    return 'HI'\n"}


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def no_ai_on_path(tmp_path, monkeypatch):
    """A PATH and home with no AI tools, so only the fakes each test adds are found."""
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    git = os.path.dirname(os.popen("command -v git").read().strip())
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{git}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    return folder


def fake(folder, name, script):
    path = folder / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def test_every_ai_is_listed_with_how_to_get_it_ready(tmp_path, monkeypatch):
    no_ai_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")  # nothing listens there
    listed = {p["provider"]: p for p in client().get("/api/ai/providers").json()}
    assert list(listed) == ["claude", "codex", "ollama", "antigravity"]
    assert not any(p["available"] for p in listed.values())
    assert listed["codex"]["install"][0]["command"] == "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
    assert listed["ollama"]["detail"] == "Install Ollama"
    assert listed["antigravity"]["install"][0]["command"] == "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    assert listed["claude"]["model"] == "haiku"  # Claude keeps its cheapest model by default


def test_codex_writes_explanations_with_the_picked_model(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    calls = tmp_path / "calls"
    fake(folder, "codex", f"""
if [ "$1 $2" = "login status" ]; then echo 'Logged in using ChatGPT'; exit 0; fi
if [ "$1" = "exec" ]; then
  out=""; prev=""
  for a in "$@"; do [ "$prev" = "--output-last-message" ] && out="$a"; prev="$a"; done
  echo "$DEVDASH_INTERNAL $*" >> {calls}
  cat > {tmp_path}/prompt
  echo '{json.dumps(ANSWER)}' > "$out"
  exit 0
fi
exit 1
""")
    cache = tmp_path / "codex-home"  # CODEX_HOME, which conftest points here
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-big", "display_name": "Big", "priority": 1, "description": "The biggest. Uses a lot."},
        {"slug": "gpt-small-mini", "display_name": "Small", "priority": 2, "description": "Small and quick."},
        {"slug": "gpt-hidden", "priority": 3, "visibility": "hide"},
    ]}))
    api = client()
    assert api.put("/api/preferences", json={"ai": {"provider": "codex"}}).status_code == 200
    status = api.get("/api/ai").json()
    assert status["provider"] == "codex" and status["signed_in"] is True and status["account"] == "ChatGPT account"
    assert [m["id"] for m in status["models"]] == ["gpt-small-mini", "gpt-big"]  # cheapest first, hidden left out
    assert status["model"] == "gpt-small-mini"  # the cheapest is the default

    assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
    run = calls.read_text()
    assert run.startswith("1 exec") and "--sandbox read-only" in run and "--model gpt-small-mini" in run
    assert "def hello" in (tmp_path / "prompt").read_text()  # the prompt goes in on stdin

    api.put("/api/preferences", json={"ai": {"models": {"codex": "gpt-big"}}})
    explain.explain("function", {**SUBJECT, "name": "other"})
    assert "--model gpt-big" in calls.read_text().splitlines()[-1]


def test_a_signed_out_codex_asks_to_sign_in(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    fake(folder, "codex", "if [ \"$1 $2\" = 'login status' ]; then echo 'Not logged in'; exit 1; fi\n"
                          "echo 'error: 401 Unauthorized' >&2; exit 1\n")
    api = client()
    api.put("/api/preferences", json={"ai": {"provider": "codex"}})
    assert api.get("/api/ai").json()["signed_in"] is False
    with pytest.raises(explain.ExplainError) as failed:
        explain.explain("function", SUBJECT)
    assert failed.value.signed_out is True


class FakeOllama(BaseHTTPRequestHandler):
    asked: list = []

    def do_GET(self):
        self._send({"models": [{"name": "big:70b", "size": 40_000_000_000, "details": {"parameter_size": "70B"}},
                               {"name": "tiny:1b", "size": 1_300_000_000, "details": {"parameter_size": "1B"}}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.asked.append(body)
        self._send({"message": {"role": "assistant", "content": json.dumps(ANSWER)}})

    def _send(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_a_local_model_answers_through_ollama(tmp_path, monkeypatch):
    no_ai_on_path(tmp_path, monkeypatch)
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{server.server_port}")
    try:
        api = client()
        api.put("/api/preferences", json={"ai": {"provider": "ollama"}})
        status = api.get("/api/ai").json()
        assert status["available"] is True and status["login"] == "none"
        assert [m["id"] for m in status["models"]] == ["tiny:1b", "big:70b"] and status["model"] == "tiny:1b"
        assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
        [asked] = FakeOllama.asked
        assert asked["model"] == "tiny:1b" and asked["format"] == "json" and asked["stream"] is False
        assert asked["messages"][0]["role"] == "system"
    finally:
        server.shutdown()


def test_antigravity_answers_in_print_mode(tmp_path, monkeypatch):
    folder = no_ai_on_path(tmp_path, monkeypatch)
    calls = tmp_path / "calls"
    fake(folder, "agy", f"""
if [ "$1 $2" = "auth status" ]; then echo 'Signed in as me@example.com'; exit 0; fi
if [ "$1" = "-p" ]; then echo "$*" >> {calls}; echo '{json.dumps(ANSWER)}'; exit 0; fi
exit 1
""")
    api = client()
    api.put("/api/preferences", json={"ai": {"provider": "antigravity", "models": {"antigravity": "gemini-flash"}}})
    status = api.get("/api/ai").json()
    assert status["signed_in"] is True and status["account"] == "me@example.com" and status["login"] == "terminal"
    assert explain.explain("function", SUBJECT)["summary"] == "Says hello."
    assert "--output-format text --model gemini-flash" in calls.read_text()


def test_the_ai_choice_is_checked():
    api = client()
    assert api.put("/api/preferences", json={"ai": {"provider": "skynet"}}).status_code == 422
    assert api.put("/api/preferences", json={"ai": {"models": {"claude": "x" * 101}}}).status_code == 400
    picked = api.put("/api/preferences", json={"ai": {"models": {"claude": "sonnet"}}}).json()["ai"]
    assert picked == {"provider": "claude", "models": {"claude": "sonnet"}}
    # An empty model goes back to the default.
    assert api.put("/api/preferences", json={"ai": {"models": {"claude": ""}}}).json()["ai"]["models"] == {}

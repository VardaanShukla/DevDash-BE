import subprocess

import pytest


@pytest.fixture(autouse=True)
def devdash_home(tmp_path, monkeypatch):
    home = tmp_path / "devdash-home"
    monkeypatch.setenv("DEVDASH_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.delenv("DEVDASH_INTERNAL", raising=False)
    return home


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "app.py").write_text("def hello():\n    return 'hi'\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "initial")
    return path.resolve()


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

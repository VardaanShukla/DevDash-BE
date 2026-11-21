from fastapi.testclient import TestClient

from devdash.api.app import create_app
from devdash.capture.install import init


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_home_shows_each_project(repo, tmp_path):
    from conftest import git

    init(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("print('hi')\n")
    git(repo, "commit", "-q", "-am", "Say hi")
    (repo / "notes.txt").write_text("draft\n")
    [card] = client().get("/api/home").json()
    assert card["exists"] and card["branch"] == "feature" and card["base"] == "main"
    assert card["ahead"] == 1 and card["uncommitted"] == 1 and card["branches"] == 2
    assert card["last_commit"]["subject"] == "Say hi" and card["commits_this_week"] >= 2

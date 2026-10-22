from fastapi.testclient import TestClient

from devdash.api.app import create_app


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_preferences_start_with_defaults_and_ask_about_uncommitted_work():
    prefs = client().get("/api/preferences").json()
    assert prefs["theme"] == "dark"
    assert prefs["uncommitted"] is None  # not picked yet: the dashboard asks
    assert prefs["shortcuts"]["view:home"] == "1" and prefs["shortcuts"]["theme"] == "t"


def test_changing_one_setting_keeps_the_others():
    api = client()
    assert api.put("/api/preferences", json={"uncommitted": True}).status_code == 200
    changed = api.put("/api/preferences", json={"theme": "light", "shortcuts": {"sync": "r"}}).json()
    assert changed["theme"] == "light" and changed["uncommitted"] is True
    assert changed["shortcuts"]["sync"] == "r" and changed["shortcuts"]["theme"] == "t"
    assert api.get("/api/preferences").json() == changed


def test_bad_settings_and_clashing_keys_are_refused():
    api = client()
    assert api.put("/api/preferences", json={"theme": "pink"}).status_code == 422
    assert api.put("/api/preferences", json={"shortcuts": {"fly": "f"}}).status_code == 400
    clash = api.put("/api/preferences", json={"shortcuts": {"sync": "t"}})
    assert clash.status_code == 400 and "theme" in clash.json()["detail"]
    # Turning a shortcut off frees its key.
    assert api.put("/api/preferences", json={"shortcuts": {"theme": "", "sync": "t"}}).status_code == 200


def test_number_keys_follow_the_menu_and_old_saved_defaults_move_with_them():
    import json

    from devdash.store import connect

    # Saved by the first version: every key stored, numbers in the old menu order, plus one
    # key the person picked.
    old = {"view:home": "1", "view:overview": "4", "view:flow": "5", "view:map": "6", "view:constellation": "7",
           "view:changes": "8", "view:commits": "9", "theme": "t", "view:planner": "m"}
    conn = connect()
    with conn:
        conn.execute("INSERT INTO settings (name, value) VALUES ('preferences', ?)",
                     (json.dumps({"theme": "light", "uncommitted": True, "shortcuts": old}),))
    conn.close()
    prefs = client().get("/api/preferences").json()
    keys = prefs["shortcuts"]
    assert prefs["theme"] == "light" and prefs["uncommitted"] is True
    assert keys["view:changes"] == "4" and keys["view:commits"] == "5" and keys["view:project"] == "6"
    assert keys["view:overview"] == "" and keys["view:map"] == ""  # sections of Home now
    assert "view:constellation" not in keys and "view:planner" not in keys  # screens since removed


def test_home_widgets_can_be_reordered_hidden_and_reset():
    api = client()
    home = api.get("/api/preferences").json()["home"]
    assert home == {"order": ["ai", "projects", "branch", "flow", "map", "calendar"], "hidden": ["map", "calendar"]}
    changed = api.put("/api/preferences", json={"home": {"order": ["flow", "ai"], "hidden": ["projects"]}}).json()
    # Widgets left out join at the end, hidden if they are by default; a hidden one stays listed.
    assert changed["home"] == {"order": ["flow", "ai", "projects", "branch", "map", "calendar"],
                               "hidden": ["projects", "map", "calendar"]}
    assert api.get("/api/preferences").json()["home"] == changed["home"]
    assert api.put("/api/preferences", json={"home": {"order": ["sky"], "hidden": []}}).status_code == 400
    assert api.post("/api/preferences/reset-home", json={}).json()["home"] == home


def test_shortcuts_go_back_to_the_defaults():
    api = client()
    api.put("/api/preferences", json={"shortcuts": {"sync": "r"}})
    assert api.post("/api/preferences/reset-shortcuts", json={}).json()["shortcuts"]["sync"] == "s"

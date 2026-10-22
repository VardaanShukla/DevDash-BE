from datetime import date, timedelta

from fastapi.testclient import TestClient

from devdash import agenda
from devdash.api.app import create_app
from devdash.capture.install import init


def client():
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_calendar_from_its_private_address(monkeypatch):
    today = date.today()
    day = lambda n: (today + timedelta(days=n)).strftime("%Y%m%d")
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Standup\r\n"
        f"DTSTART:{day(-14)}T090000\r\nDTEND:{day(-14)}T091500\r\nRRULE:FREQ=DAILY\r\n"
        f"EXDATE:{day(1)}T090000\r\nEND:VEVENT\r\n"
        f"BEGIN:VEVENT\r\nUID:2\r\nSUMMARY:Off\r\nDTSTART;VALUE=DATE:{day(2)}\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:3\r\nSUMMARY:Long ago\r\nDTSTART:20200101T100000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    fetched = []

    def fake_fetch(url, fresh=False):
        fetched.append(url)
        if "bad" in url:
            raise agenda.AgendaError("That address isn't a calendar. Use the one marked iCal or .ics.")
        agenda._cache[url] = (0, ics)
        return ics

    monkeypatch.setattr(agenda, "_fetch", fake_fetch)
    api = client()
    assert api.get("/api/calendar").json()["connected"] is False
    assert api.put("/api/calendar", json={"url": "not a link"}).status_code == 400
    assert api.put("/api/calendar", json={"url": "https://example.com/bad.ics"}).status_code == 400

    made = api.put("/api/calendar", json={"url": "webcal://calendar.example.com/private-abc/basic.ics"}).json()
    assert fetched[-1] == "https://calendar.example.com/private-abc/basic.ics"
    # The private address never goes back to the page.
    assert made["source"] == "calendar.example.com" and "private-abc" not in str(made)

    events = api.get("/api/calendar", params={"days": 3}).json()["events"]
    assert [(e["title"], e["start"][:10]) for e in events] == [
        ("Standup", today.isoformat()),
        ("Off", (today + timedelta(days=2)).isoformat()),
        ("Standup", (today + timedelta(days=2)).isoformat()),
    ]
    assert events[1]["all_day"] is True and events[0]["start"][11:16] == "09:00"

    api.request("DELETE", "/api/calendar", json={})
    assert api.get("/api/calendar").json()["connected"] is False


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

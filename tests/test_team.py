import json

import pytest
from fastapi.testclient import TestClient

from devdash.team import client as team_client
from devdash.team import host, store
from devdash.team.server import COOKIE, create_team_app, set_reach, setup_code

PASSWORD = "a long password"
EXPLANATION = {"summary": "Signing in now remembers you.", "change": "It keeps a token for 30 days.",
               "improves": ["Fewer sign-ins"], "tradeoffs": ["A stolen laptop stays signed in"], "check": None}


def browser(app=None, base="https://127.0.0.1", ip="1.1.1.1"):
    api = TestClient(app or create_team_app(), base_url=base)
    api.headers["X-Forwarded-For"] = ip
    return api


def signup(app, username, invite=None, ip="1.1.1.1"):
    person = browser(app, ip=ip)
    response = person.post("/api/signup", json={"username": username, "password": PASSWORD, "invite": invite})
    assert response.status_code == 200, response.text
    return person


def new_org(person, name="Acme"):
    return person.post("/api/orgs", json={"name": name}).json()["id"]


def invite(admin, org_id, **body):
    response = admin.post(f"/api/orgs/{org_id}/invites", json=body)
    assert response.status_code == 200, response.text
    return response.json()["invite"]


def link_device(app, person, org_id):
    agent = browser(app)
    started = agent.post("/api/device/start", json={"name": "laptop"}).json()
    assert agent.post("/api/device/token", json={"device_code": started["device_code"]}).status_code == 202
    assert person.post("/api/device/check", json={"code": started["user_code"]}).json()["name"] == "laptop"
    approved = person.post("/api/device/approve", json={"code": started["user_code"], "org_id": org_id})
    assert approved.status_code == 200, approved.text
    found = agent.post("/api/device/token", json={"device_code": started["device_code"]}).json()
    assert found["token"].startswith("ddt_") and found["org_id"] == org_id
    # The code works once.
    assert agent.post("/api/device/token", json={"device_code": started["device_code"]}).status_code == 410
    agent.headers["Authorization"] = f"Bearer {found['token']}"
    return agent


def test_anyone_can_sign_up_and_make_an_organisation():
    app = create_team_app()
    ana = signup(app, "ana")
    assert ana.cookies.get(COOKIE)
    state = ana.get("/api/state").json()
    assert state["me"]["username"] == "ana" and state["orgs"] == []
    org_id = new_org(ana)
    assert ana.get("/api/state").json()["orgs"] == [{"id": org_id, "name": "Acme", "role": "owner"}]
    # Usernames are one each, whatever the case.
    assert browser(app, ip="2.2.2.2").post("/api/signup", json={"username": "ANA", "password": PASSWORD}).status_code == 409


def test_cookie_is_secure_and_strict():
    response = browser().post("/api/signup", json={"username": "ana", "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    for part in ("__host-devdash_team=", "httponly", "secure", "samesite=strict", "path=/"):
        assert part in cookie


def test_organisations_never_see_each_other():
    app = create_team_app()
    ana, bo = signup(app, "ana"), signup(app, "bo", ip="2.2.2.2")
    acme, other = new_org(ana, "Acme"), new_org(bo, "Other")
    assert bo.get(f"/api/orgs/{acme}/projects").status_code == 404
    assert bo.get(f"/api/orgs/{acme}/people").status_code == 404
    assert bo.post(f"/api/orgs/{acme}/teams", json={"name": "x"}).status_code == 404
    team = ana.post(f"/api/orgs/{acme}/teams", json={"name": "Web"}).json()
    # A team id from another organisation can't be used in yours.
    bo_project = link_device(app, bo, other).post("/api/agent/projects", json={"key": "a" * 64, "name": "P"}).json()
    assert bo.post(f"/api/orgs/{other}/projects/{bo_project['id']}/teams", json={"team_id": team["id"]}).status_code == 404
    # Linking a DevDash to an organisation you're not in is refused.
    agent = browser(app)
    started = agent.post("/api/device/start", json={"name": "x"}).json()
    assert bo.post("/api/device/approve", json={"code": started["user_code"], "org_id": acme}).status_code == 404


def test_the_server_admin_needs_the_setup_code_and_can_close_signups():
    app = create_team_app()
    code = setup_code()
    ana = signup(app, "ana")
    assert ana.post("/api/setup", json={"code": "AAAA-BBBB-CCCC"}).status_code == 403
    assert ana.put("/api/server", json={"signup": "invite"}).status_code == 403
    assert ana.post("/api/setup", json={"code": code}).status_code == 200
    assert not (store.home() / "setup-code").exists() and setup_code() is None
    assert signup(app, "bo", ip="2.2.2.2").post("/api/setup", json={"code": code}).status_code == 409

    assert ana.put("/api/server", json={"signup": "invite"}).status_code == 200
    closed = browser(app, ip="3.3.3.3").post("/api/signup", json={"username": "cy", "password": PASSWORD})
    assert closed.status_code == 403
    # An invite still lets someone in.
    org_id = new_org(ana)
    signup(app, "cy", invite=invite(ana, org_id), ip="3.3.3.3")


def test_passwords_are_checked_and_guessing_is_slowed_per_visitor():
    app = create_team_app()
    set_reach(["https://teams.example.com"], "caddy")
    signup(app, "ana")
    site = "https://teams.example.com"
    stranger = browser(app, site, ip="9.9.9.9")
    assert stranger.post("/api/login", json={"username": "ana", "password": "wrong"}).status_code == 401
    assert stranger.post("/api/login", json={"username": "nobody", "password": PASSWORD}).status_code == 401
    assert stranger.post("/api/login", json={"username": "ANA", "password": PASSWORD}).status_code == 200
    for n in range(20):
        stranger.post("/api/login", json={"username": f"guess{n}", "password": "wrong"})
    assert stranger.post("/api/login", json={"username": "other", "password": "wrong"}).status_code == 429
    # Someone else isn't locked out by the stranger's guessing.
    assert browser(app, site, ip="8.8.8.8").post("/api/login", json={"username": "ana", "password": PASSWORD}).status_code == 200


def test_signups_are_limited_per_visitor():
    app = create_team_app()
    set_reach([], "caddy")
    for n in range(5):
        signup(app, f"user{n}", ip="7.7.7.7")
    assert browser(app, ip="7.7.7.7").post("/api/signup", json={"username": "user9", "password": PASSWORD}).status_code == 429
    signup(app, "elsewhere", ip="6.6.6.6")


def test_cloudflares_visitor_header_counts_only_behind_cloudflare():
    app = create_team_app()
    set_reach(["https://a-b.trycloudflare.com"], "caddy")
    site = "https://a-b.trycloudflare.com"
    # Behind Caddy, a made-up Cf-Connecting-Ip doesn't get anyone around the limit.
    for n in range(5):
        api = browser(app, site, ip="5.5.5.5")
        api.headers["Cf-Connecting-Ip"] = f"10.0.0.{n}"
        assert api.post("/api/signup", json={"username": f"u{n}", "password": PASSWORD}).status_code == 200
    api = browser(app, site, ip="5.5.5.5")
    api.headers["Cf-Connecting-Ip"] = "10.0.0.99"
    assert api.post("/api/signup", json={"username": "u9", "password": PASSWORD}).status_code == 429


def test_weak_passwords_are_refused():
    app = create_team_app()
    person = browser(app)
    assert person.post("/api/signup", json={"username": "sam", "password": "short"}).status_code == 400
    assert person.post("/api/signup", json={"username": "sam", "password": "sam-is-my-password"}).status_code == 400


def test_only_its_own_addresses_and_pages_are_answered():
    app = create_team_app()
    ana = signup(app, "ana")
    assert ana.post("/api/orgs", json={"name": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert ana.post("/api/orgs", content="name=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert ana.post("/api/orgs", json={"name": "x"}, headers={"Origin": "https://127.0.0.1"}).status_code == 200
    assert browser(app, "https://attacker.example").get("/api/state").status_code == 421
    # A tunnel's address only once `devdash host` noted it.
    assert browser(app, "https://box.trycloudflare.com").get("/api/state").status_code == 421
    set_reach(["https://box.trycloudflare.com"], "cloudflare")
    assert browser(app, "https://box.trycloudflare.com").get("/api/state").status_code == 200
    # A proxy that passes Host as 127.0.0.1 still lets the page on the public address make changes.
    assert ana.post("/api/orgs", json={"name": "y"}, headers={"Origin": "https://box.trycloudflare.com"}).status_code == 200
    big = ana.post("/api/orgs", content=b"{" + b" " * 70_000 + b"}", headers={"Content-Type": "application/json"})
    assert big.status_code == 413


def test_pages_get_strict_headers():
    response = browser().get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert browser().get("/docs").status_code == 404
    assert browser().get("/openapi.json").status_code == 404


def test_invites_join_an_organisation_once():
    app = create_team_app()
    ana = signup(app, "ana")
    org_id = new_org(ana)
    team = ana.post(f"/api/orgs/{org_id}/teams", json={"name": "Mobile"}).json()
    token = invite(ana, org_id, team_id=team["id"])
    assert browser(app).post("/api/invites/check", json={"invite": token}).json() == {"org": "Acme", "role": "member"}
    sam = signup(app, "sam", invite=token, ip="2.2.2.2")
    assert sam.get("/api/state").json()["orgs"][0]["role"] == "member"
    again = browser(app, ip="3.3.3.3").post("/api/signup", json={"username": "sam2", "password": PASSWORD, "invite": token})
    assert again.status_code == 403
    assert [p["name"] for p in ana.get(f"/api/orgs/{org_id}/teams").json()[0]["people"]] == ["sam"]
    assert sam.post(f"/api/orgs/{org_id}/invites", json={}).status_code == 403
    assert sam.post(f"/api/orgs/{org_id}/teams", json={"name": "Mine"}).status_code == 403
    # Someone with an account accepts one while signed in.
    bo = signup(app, "bo", ip="4.4.4.4")
    accepted = bo.post("/api/invites/accept", json={"invite": invite(ana, org_id)})
    assert accepted.json()["org_id"] == org_id
    # Only the owner invites admins.
    assert ana.post(f"/api/orgs/{org_id}/invites", json={"role": "admin"}).status_code == 200


def test_teams_on_a_project_decide_who_sees_its_explanations():
    app = create_team_app()
    ana = signup(app, "ana")
    org_id = new_org(ana)
    web = ana.post(f"/api/orgs/{org_id}/teams", json={"name": "Web"}).json()
    sam = signup(app, "sam", invite=invite(ana, org_id, team_id=web["id"]), ip="2.2.2.2")
    ria = signup(app, "ria", invite=invite(ana, org_id), ip="3.3.3.3")

    agent = link_device(app, ana, org_id)
    project = agent.post("/api/agent/projects", json={"key": "a" * 64, "name": "DevDash"}).json()
    shared = agent.post("/api/agent/share", json={"project_id": project["id"], "subject": "log_in", "ref": "r1",
                                                   "explanation": EXPLANATION})
    assert shared.status_code == 200

    base = f"/api/orgs/{org_id}/projects"
    # Nobody else sees it until a team is added.
    assert sam.get(base).json() == []
    assert sam.get(f"{base}/{project['id']}").status_code == 404
    assert ana.post(f"{base}/{project['id']}/teams", json={"team_id": web["id"]}).status_code == 200
    seen = sam.get(f"{base}/{project['id']}").json()
    assert seen["shared"][0]["subject"] == "log_in"
    assert seen["shared"][0]["explanation"]["summary"] == EXPLANATION["summary"]
    assert seen["manage"] is False and seen["shared"][0]["removable"] is False
    # A member can't add teams to a project they didn't link, or remove what others shared.
    assert sam.post(f"{base}/{project['id']}/teams", json={"team_id": web["id"]}).status_code == 403
    assert sam.delete(f"/api/orgs/{org_id}/shared/{seen['shared'][0]['id']}").status_code == 403
    assert ria.get(base).json() == []

    # Sharing the same thing again updates it rather than adding another.
    agent.post("/api/agent/share", json={"project_id": project["id"], "subject": "log_in", "ref": "r1",
                                          "explanation": {**EXPLANATION, "summary": "Updated."}})
    assert [s["explanation"]["summary"] for s in ana.get(f"{base}/{project['id']}").json()["shared"]] == ["Updated."]


def test_someone_elses_project_needs_their_team():
    app = create_team_app()
    ana = signup(app, "ana")
    org_id = new_org(ana)
    sam = signup(app, "sam", invite=invite(ana, org_id), ip="2.2.2.2")
    link_device(app, ana, org_id).post("/api/agent/projects", json={"key": "b" * 64, "name": "Secret"})
    sam_agent = link_device(app, sam, org_id)
    # Knowing the first commit isn't enough to get in.
    assert sam_agent.post("/api/agent/projects", json={"key": "b" * 64, "name": "Secret"}).status_code == 403
    mine = sam_agent.post("/api/agent/projects", json={"key": "c" * 64, "name": "Mine"}).json()
    # Whoever linked a project can add their organisation's teams to it.
    team = ana.post(f"/api/orgs/{org_id}/teams", json={"name": "Ops"}).json()
    assert sam.post(f"/api/orgs/{org_id}/projects/{mine['id']}/teams", json={"team_id": team["id"]}).status_code == 200
    # The same first commit in another organisation is a separate project.
    bo = signup(app, "bo", ip="4.4.4.4")
    other = new_org(bo, "Other")
    assert link_device(app, bo, other).post("/api/agent/projects", json={"key": "b" * 64, "name": "Theirs"}).status_code == 200


def test_a_share_holds_only_an_explanation():
    app = create_team_app()
    ana = signup(app, "ana")
    org_id = new_org(ana)
    agent = link_device(app, ana, org_id)
    project = agent.post("/api/agent/projects", json={"key": "d" * 64, "name": "P"}).json()
    base = {"project_id": project["id"], "subject": "f", "ref": "r", "explanation": EXPLANATION}
    assert agent.post("/api/agent/share", json={**base, "diff": "+secret"}).status_code == 422
    assert agent.post("/api/agent/share", json={**base, "explanation": {**EXPLANATION, "code": "x"}}).status_code == 422
    assert agent.post("/api/agent/share", json={**base, "explanation": {**EXPLANATION, "summary": "x" * 3000}}).status_code == 422
    assert agent.post("/api/agent/share", json={**base, "project_id": 999}).status_code == 404
    agent.headers["Authorization"] = "Bearer ddt_wrong"
    assert agent.post("/api/agent/share", json=base).status_code == 401


def test_removing_someone_retires_their_devdash_there():
    app = create_team_app()
    ana = signup(app, "ana")
    org_id = new_org(ana)
    sam = signup(app, "sam", invite=invite(ana, org_id), ip="2.2.2.2")
    agent = link_device(app, sam, org_id)
    sam_id = next(p["id"] for p in ana.get(f"/api/orgs/{org_id}/people").json() if p["username"] == "sam")
    assert ana.delete(f"/api/orgs/{org_id}/people/{sam_id}").status_code == 200
    assert agent.get("/api/agent/me").status_code == 401
    assert sam.get(f"/api/orgs/{org_id}/projects").status_code == 404
    # The owner can't be removed, and can't leave.
    ana_id = next(p["id"] for p in ana.get(f"/api/orgs/{org_id}/people").json() if p["username"] == "ana")
    assert ana.delete(f"/api/orgs/{org_id}/people/{ana_id}").status_code == 403
    assert ana.delete(f"/api/orgs/{org_id}/me").status_code == 400


def test_changing_the_password_signs_out_other_browsers():
    app = create_team_app()
    ana = signup(app, "ana")
    other = browser(app)
    other.post("/api/login", json={"username": "ana", "password": PASSWORD})
    assert ana.put("/api/password", json={"current": PASSWORD, "new": "another long one"}).status_code == 200
    assert ana.get("/api/devices").status_code == 200
    assert other.get("/api/devices").status_code == 401


def test_hosting_checks_the_domain():
    assert host.clean_domain("https://Teams.Example.com/") == "teams.example.com"
    for bad in ("localhost", "not a domain", "http://", "a..b.com"):
        with pytest.raises(host.HostError):
            host.clean_domain(bad)


# ---------------------------------------------------------------- this DevDash's side

@pytest.fixture
def server_for_client(monkeypatch):
    """Points the client at a team server in this process instead of the network."""
    app = create_team_app()
    ana = signup(app, "ana")
    org_id = new_org(ana)
    sent = []

    def call(url, path, body=None, token=None):
        api = browser(app)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        sent.append((path, body))
        response = api.post(path, json=body, headers=headers) if body is not None else api.get(path, headers=headers)
        if response.status_code >= 400 and response.status_code != 410:
            raise team_client.TeamError(response.json().get("detail", str(response.status_code)))
        return response.status_code, response.json()

    monkeypatch.setattr(team_client, "_call", call)
    return ana, org_id, sent


def test_clean_url_keeps_tokens_off_plain_http():
    assert team_client.clean_url("teams.example.com/") == "https://teams.example.com"
    assert team_client.clean_url("http://127.0.0.1:8770") == "http://127.0.0.1:8770"
    with pytest.raises(team_client.TeamError):
        team_client.clean_url("http://192.168.1.5:8770")
    with pytest.raises(team_client.TeamError):
        team_client.clean_url("https://user:pass@example.com")


def test_linking_and_sharing_sends_only_the_explanation(server_for_client, repo):
    from devdash.capture import install
    from devdash.store import connect

    ana, org_id, sent = server_for_client
    install.init(repo)
    started = team_client.start("https://127.0.0.1")
    code = started["pending"]["user_code"]
    assert team_client.poll()["connected"] is False
    ana.post("/api/device/approve", json={"code": code, "org_id": org_id})
    linked = team_client.poll()
    assert linked["connected"] is True and linked["org"] == "Acme"

    conn = connect()
    repo_id = conn.execute("SELECT id FROM repos").fetchone()["id"]
    conn.close()
    team_client.link(repo_id)
    body = next(body for path, body in sent if path == "/api/agent/projects")
    assert set(body) == {"key", "name"} and len(body["key"]) == 64

    explanation = {**EXPLANATION, "diff": "+password = 'hunter2'", "path": "src/auth.py"}
    shared = team_client.share(repo_id, "log_in", "turn-3:src/auth.py:log_in", explanation)
    body = next(body for path, body in sent if path == "/api/agent/share")
    assert set(body) == {"project_id", "subject", "ref", "explanation"}
    assert set(body["explanation"]) == set(EXPLANATION)
    assert "auth.py" not in json.dumps(body)
    assert shared["url"].endswith(f"&org={org_id}")
    assert ana.get(f"/api/orgs/{org_id}/projects").json()[0]["shared"] == 1


def test_company_network_addresses_are_ips_and_internal_names():
    assert host.clean_addresses("10.0.0.5, devdash.corp https://Shop.Office.Local/") == [
        "10.0.0.5", "devdash.corp", "shop.office.local"]
    for bad in ("", "localhost", "bad_name", "10.0.0.5, -x"):
        with pytest.raises(host.HostError):
            host.clean_addresses(bad)


def test_company_network_serves_https_on_each_address(monkeypatch, tmp_path):
    started = []

    class Running:
        pid = 4242
        stderr = iter(())

        def poll(self):
            return None

    monkeypatch.setattr(host.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(host.subprocess, "Popen", lambda args, **kw: started.append(args) or Running())
    monkeypatch.setattr(host.time, "monotonic", iter(range(0, 1000, 10)).__next__)
    host._start_network(["10.0.0.5", "devdash.corp"], 8443, 8770, None, None)
    config = (store.home() / "caddy" / "Caddyfile").read_text()
    assert "https://10.0.0.5:8443, https://devdash.corp:8443 {" in config
    assert "tls internal" in config and "reverse_proxy 127.0.0.1:8770" in config
    # No port 80 and no admin rights needed.
    assert "auto_https disable_redirects" in config and "skip_install_trust" in config
    assert started[0][:2] == ["caddy", "run"]
    # The company's own certificate, when it has one.
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("y")
    host._start_network(["10.0.0.5"], 8443, 8770, str(cert), str(key))
    assert f'tls "{cert}" "{key}"' in (store.home() / "caddy" / "Caddyfile").read_text()
    with pytest.raises(host.HostError):
        host._start_network(["10.0.0.5"], 8443, 8770, str(cert), None)


def test_a_company_network_servers_root_certificate_can_be_added():
    import glob
    import ssl

    with pytest.raises(team_client.TeamError):
        team_client.add_root_certificate("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
    found = sorted(glob.glob("/etc/ssl/certs/*.pem"))
    if not found:
        pytest.skip("no system certificate to try with")
    pem = open(found[0]).read()
    assert team_client.add_root_certificate("some text\n" + pem)["root_certificate"] is True
    assert isinstance(team_client._tls(), ssl.SSLContext)


def test_company_network_hosting_refuses_another_computers_ip(monkeypatch):
    monkeypatch.setattr(host, "local_addresses", lambda: ["192.168.31.141"])
    host.check_own_addresses(["192.168.31.141", "devdash.corp"])
    with pytest.raises(host.HostError, match="isn't this computer's address"):
        host.check_own_addresses(["192.168.31.50"])


def test_a_company_network_server_offers_its_root_certificate(monkeypatch, tmp_path):
    import glob

    found = sorted(glob.glob("/etc/ssl/certs/*.pem"))
    if not found:
        pytest.skip("no system certificate to try with")
    pem = open(found[0]).read()
    app = create_team_app()
    assert browser(app).get("/root.crt").status_code == 404
    assert browser(app).get("/api/state").json()["root_certificate"] is False
    path = tmp_path / "root.crt"
    path.write_text(pem)
    monkeypatch.setattr(host, "root_certificate", lambda: path)
    response = browser(app).get("/root.crt")
    assert response.status_code == 200 and "BEGIN CERTIFICATE" in response.text
    assert browser(app).get("/api/state").json()["root_certificate"] is True
    # Fingerprints are short enough to compare by eye.
    assert len(host.fingerprint(pem).split()) == 6

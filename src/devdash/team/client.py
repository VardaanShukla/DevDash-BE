"""This DevDash's link to a team server: linking with a code the person approves in their
browser, linking a project, and sharing an explanation. The link's token is kept in the
local database and never sent to the page.

What it sends: the computer's name when linking; a project's name and a hash of its first
commit (so everyone's clone finds the same project); and for a share, the function's name
and its explanation, nothing else. Code, diffs, file paths and prompts stay here."""

import hashlib
import json
import platform
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from devdash.store import connect

SETTING = "team"
# A team server's root certificate someone added here, for a company network server whose
# certificate comes from its own authority rather than one every computer already trusts.
CA_SETTING = "team_ca"
PEM = re.compile(r"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----")
TIMEOUT = 20
EXPLANATION_FIELDS = ("summary", "change", "improves", "tradeoffs", "check")
LOCAL = {"127.0.0.1", "localhost"}

# The linking under way: the server's address, the secret to poll with, and the code shown.
_pending: dict = {}


class TeamError(RuntimeError):
    def __init__(self, message: str, signed_out: bool = False, orgs: list | None = None):
        super().__init__(message)
        self.signed_out = signed_out
        # Someone in several organisations signing in: the ones to pick from.
        self.orgs = orgs


def clean_url(text: str) -> str:
    """https://host[:port] from what someone typed. Plain http only for this computer, so a
    token is never sent unencrypted over a network."""
    text = (text or "").strip().rstrip("/")
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise TeamError("That isn't a team server address. It looks like https://team.example.com")
    if parts.scheme == "http" and parts.hostname not in LOCAL:
        raise TeamError("Use the server's https:// address. Plain http is only for a server on this computer.")
    return f"{parts.scheme}://{parts.netloc}"


def _saved() -> dict | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (SETTING,)).fetchone()
    conn.close()
    try:
        saved = json.loads(row["value"]) if row else None
    except json.JSONDecodeError:
        return None
    return saved if isinstance(saved, dict) and saved.get("token") else None


def _save(saved: dict | None) -> None:
    conn = connect()
    with conn:
        if saved is None:
            conn.execute("DELETE FROM settings WHERE name = ?", (SETTING,))
        else:
            conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, json.dumps(saved)))
    conn.close()


def _tls() -> ssl.SSLContext:
    """The computer's trusted certificates, plus the root of a team server hosted on this
    computer and one added here (add_root_certificate), so a company network server works
    without changing the system's trust store."""
    context = ssl.create_default_context()
    from devdash.team import host

    own = host.root_certificate()
    if own is not None:
        context.load_verify_locations(cafile=str(own))
    added = _setting(CA_SETTING)
    if added:
        context.load_verify_locations(cadata=added)
    return context


def _setting(name: str) -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row["value"] if row else None


def add_root_certificate(text: str) -> dict:
    """Trusts a team server's root certificate (the .crt its host downloads from Settings) for
    linking to it. Only a certificate is accepted, never a key."""
    found = PEM.search(text or "")
    if found is None:
        raise TeamError("That isn't a certificate file. Use the root certificate the team server's host downloaded "
                        "(devdash-team-root.crt).")
    try:
        ssl.PEM_cert_to_DER_cert(found.group(0))
        ssl.create_default_context().load_verify_locations(cadata=found.group(0))
    except (ValueError, ssl.SSLError):
        raise TeamError("That certificate file is damaged. Download it again from the team server's host.")
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (CA_SETTING, found.group(0)))
    conn.close()
    return status()


def fetch_root_certificate(url: str) -> dict:
    """The root certificate a company network team server offers at /root.crt, with its
    fingerprint for the person to compare with the host's Settings before trusting it. It's
    fetched without checking the server's certificate (that's what isn't trusted yet); a
    wrong one only means linking still fails, since the server must still prove itself."""
    from devdash.team.host import fingerprint

    url = clean_url(url)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/root.crt", headers={"User-Agent": "DevDash"}),
                                    timeout=TIMEOUT, context=context) as response:
            text = response.read(20_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise TeamError("This team server doesn't offer a certificate of its own. Ask its host for the right one.")
        raise TeamError(f"The team server answered {error.code}.")
    except (OSError, urllib.error.URLError) as error:
        raise TeamError(f"Couldn't reach the team server ({getattr(error, 'reason', error)}). Is it running, and are you "
                        "on its network?")
    found = PEM.search(text)
    if found is None:
        raise TeamError("The team server didn't send a certificate.")
    return {"pem": found.group(0), "fingerprint": fingerprint(found.group(0))}


def _call(url: str, path: str, body: dict | None = None, token: str | None = None,
          method: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json", "User-Agent": "DevDash"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url + path, data=data, headers=headers,
                                     method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=_tls()) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        try:
            found = json.loads(error.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            found = {}
        detail = found.get("detail") if isinstance(found, dict) else None
        if error.code == 401 and token:
            _save(None)
            raise TeamError("The team server no longer accepts this DevDash. Link it again.", signed_out=True)
        if error.code in (410,):
            return error.code, found
        raise TeamError(detail if isinstance(detail, str) else f"The team server answered {error.code}.",
                        orgs=found.get("orgs") if isinstance(found, dict) else None)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            raise TeamError("This computer doesn't trust the team server's certificate yet. Add the server's root "
                            "certificate here (Settings → Team server → Add root certificate): whoever hosts the server "
                            "downloads it from their Settings → Team server.", signed_out=False)
        raise TeamError(f"Couldn't reach the team server ({reason}). Is it running, and are you on its network?")


def status() -> dict:
    saved = _saved()
    pending = {"user_code": _pending["user_code"], "link": _pending["link"]} if _pending else None
    # Whether a team server's root certificate was added here.
    root = _setting(CA_SETTING) is not None
    if saved is None:
        return {"connected": False, "pending": pending, "root_certificate": root, "mode": mode()}
    return {"connected": True, "url": saved["url"], "org": saved.get("org"), "person": saved.get("person"),
            "projects": saved.get("projects", {}), "pending": None, "root_certificate": root, "mode": mode()}


def start(url: str) -> dict:
    """Starts linking: the person opens the link (or the server's page and enters the code)
    and approves it, while poll() waits."""
    url = clean_url(url)
    name = (platform.node() or "DevDash")[:60]
    _, found = _call(url, "/api/device/start", {"name": name})
    _pending.clear()
    _pending.update(url=url, device_code=found["device_code"], user_code=found["user_code"],
                    link=f"{url}/#link={found['user_code']}", until=time.time() + int(found.get("expires_in", 600)))
    return status()


def poll() -> dict:
    if not _pending:
        return status()
    if time.time() > _pending["until"]:
        _pending.clear()
        raise TeamError("Linking ran out. Start again.")
    code, found = _call(_pending["url"], "/api/device/token", {"device_code": _pending["device_code"]})
    if code == 410:
        _pending.clear()
        raise TeamError("Linking ran out. Start again.")
    if "token" in found:
        _save({"url": _pending["url"], "token": found["token"], "person": found.get("person"),
               "org": found.get("org"), "org_id": found.get("org_id"), "projects": {}})
        _pending.clear()
    return status()


def check(url: str) -> dict:
    """Whether the team server answers, and trusts: its name and whether it takes new accounts."""
    url = clean_url(url)
    _, found = _call(url, "/api/state")
    return {"url": url, "server": found.get("server"), "signup": found.get("signup")}


def _linked_now(url: str, found: dict) -> dict:
    _pending.clear()
    _save({"url": url, "token": found["token"], "person": found.get("person"), "org": found.get("org"),
           "org_id": found.get("org_id"), "projects": {}})
    return set_mode("org")


def register(url: str, username: str, name: str, password: str, org_name: str | None = None,
             org_code: str | None = None) -> dict:
    """Makes an account on the team server from DevDash, which makes an organisation (org_name)
    or joins one (org_code), and links this DevDash to it."""
    url = clean_url(url)
    _, found = _call(url, "/api/agent/register", {
        "username": username, "name": name or None, "password": password, "org_name": org_name or None,
        "org_code": org_code or None, "device": (platform.node() or "DevDash")[:60]})
    return _linked_now(url, found)


def login(url: str, username: str, password: str, org_code: str | None = None, org_id: int | None = None) -> dict:
    """Signs in to the team server from DevDash (joining with a code, if given) and links this
    DevDash. Someone in several organisations gets TeamError.orgs to pick from."""
    url = clean_url(url)
    _, found = _call(url, "/api/agent/login", {
        "username": username, "password": password, "org_code": org_code or None, "org_id": org_id,
        "device": (platform.node() or "DevDash")[:60]})
    return _linked_now(url, found)


def cancel() -> dict:
    _pending.clear()
    return status()


def disconnect() -> dict:
    """Forgets the link here. The server keeps what was shared; unlink this DevDash there too
    (You → Linked DevDashes) to retire its token."""
    _pending.clear()
    _save(None)
    return status()


def _linked() -> dict:
    saved = _saved()
    if saved is None:
        raise TeamError("Link DevDash to a team server first (Settings → Team server).", signed_out=True)
    return saved


def remote() -> dict:
    """The person and their projects as the server sees them now."""
    saved = _linked()
    _, found = _call(saved["url"], "/api/agent/me", token=saved["token"])
    _save({**saved, "person": found.get("person"), "org": found.get("org")})
    return found


def project_key(root_commit: str) -> str:
    return hashlib.sha256(f"devdash-project:{root_commit}".encode()).hexdigest()


def repo_name(path: Path) -> str | None:
    """The clone's remote as the team server names repos: github:owner/name or codecommit:name,
    so a project an admin imported from that repo is found."""
    from devdash import codecommit, github

    found = github.remote_name(path)
    if found:
        return f"github:{found.lower()}"
    found = codecommit.remote_name(path)
    return f"codecommit:{found[1]}" if found else None


def link(repo_id: int) -> dict:
    saved = _linked()
    conn = connect()
    row = conn.execute("SELECT name, path, root_commit FROM repos WHERE id = ?", (repo_id,)).fetchone()
    conn.close()
    if row is None:
        raise TeamError("No such project.")
    if not row["root_commit"]:
        raise TeamError("Commit something in this project first: the team server matches projects by their first commit.")
    body = {"key": project_key(row["root_commit"]), "name": row["name"][:80]}
    repo = repo_name(Path(row["path"]))
    if repo:
        body["repo"] = repo
    _, found = _call(saved["url"], "/api/agent/projects", body, token=saved["token"])
    projects = {**saved.get("projects", {}), str(repo_id): {"id": found["id"], "name": found["name"]}}
    _save({**saved, "projects": projects})
    return status()


MODE_SETTING = "team_mode"
MODES = ("individual", "org")


def mode() -> str | None:
    """How this DevDash is used: 'individual', 'org' (in an organisation), or None before the
    person has picked."""
    value = _setting(MODE_SETTING)
    return value if value in MODES else None


def set_mode(value: str | None) -> dict:
    conn = connect()
    with conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE name = ?", (MODE_SETTING,))
        else:
            conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (MODE_SETTING, value))
    conn.close()
    return status()


def org_call(method: str, path: str, body: dict | None = None):
    """The linked organisation's part of the team server's API, as the person who linked this
    DevDash: what its admins manage it with from DevDash's own screens."""
    saved = _linked()
    org_id = saved.get("org_id")
    if org_id is None:
        _, found = _call(saved["url"], "/api/agent/me", token=saved["token"])
        org_id = found["org_id"]
        _save({**saved, "org_id": org_id})
    if body is None and method in ("POST", "PUT"):
        body = {}
    _, found = _call(saved["url"], f"/api/orgs/{org_id}{path}", body, token=saved["token"], method=method)
    return found


def unlink(repo_id: int) -> dict:
    saved = _linked()
    projects = {k: v for k, v in saved.get("projects", {}).items() if k != str(repo_id)}
    _save({**saved, "projects": projects})
    return status()


def share(repo_id: int, subject: str, ref: str, explanation: dict) -> dict:
    """Shares one explanation to the project's team. Only the explanation's own fields go."""
    saved = _linked()
    project = saved.get("projects", {}).get(str(repo_id))
    if project is None:
        raise TeamError("Link this project to the team server first (Settings → Team server).")
    body = {
        "project_id": project["id"],
        "subject": subject.strip()[:120],
        # Hashed, since the caller's id for it may name a file.
        "ref": hashlib.sha256(f"{repo_id}:{ref}".encode()).hexdigest(),
        "explanation": {key: explanation.get(key) for key in EXPLANATION_FIELDS if key in explanation},
    }
    _, found = _call(saved["url"], "/api/agent/share", body, token=saved["token"])
    return {"id": found["id"], "project": project["name"],
            "url": f"{saved['url']}/#project={project['id']}&org={found['org_id']}"}

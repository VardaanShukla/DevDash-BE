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
    def __init__(self, message: str, signed_out: bool = False):
        super().__init__(message)
        self.signed_out = signed_out


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


def _call(url: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json", "User-Agent": "DevDash"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url + path, data=data, headers=headers, method="POST" if body is not None else "GET")
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
        raise TeamError(detail if isinstance(detail, str) else f"The team server answered {error.code}.")
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
        return {"connected": False, "pending": pending, "root_certificate": root}
    return {"connected": True, "url": saved["url"], "org": saved.get("org"), "person": saved.get("person"),
            "projects": saved.get("projects", {}), "pending": None, "root_certificate": root}


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


def link(repo_id: int) -> dict:
    saved = _linked()
    conn = connect()
    row = conn.execute("SELECT name, root_commit FROM repos WHERE id = ?", (repo_id,)).fetchone()
    conn.close()
    if row is None:
        raise TeamError("No such project.")
    if not row["root_commit"]:
        raise TeamError("Commit something in this project first: the team server matches projects by their first commit.")
    _, found = _call(saved["url"], "/api/agent/projects", {"key": project_key(row["root_commit"]), "name": row["name"][:80]},
                     token=saved["token"])
    projects = {**saved.get("projects", {}), str(repo_id): {"id": found["id"], "name": found["name"]}}
    _save({**saved, "projects": projects})
    return status()


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

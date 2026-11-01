"""The team server: a hosted product anyone can open. People sign up, make an organisation
(or join one with an invite link), make teams in it, and add teams to the projects their
DevDash linked; the teams on a project see the explanations of changes shared to it. It
works on a phone. `devdash host` runs it behind HTTPS: Caddy for your own domain, or a
Cloudflare tunnel from your own computer.

Who's who: an account is one person. The server's operator claims it once with a setup code
printed where it started, which makes them its admin (they can close sign-ups). In each
organisation a person is its owner, an admin or a member. A browser signs in with a
password and gets a random token in a cookie; a person's own DevDash links to one of their
organisations with a code they approve in the browser, and gets a token of its own. Only
hashes of tokens and codes are kept.

What it holds: an explanation someone pressed Share on (the function's name and its plain-
English explanation), who shared it and when. A share holding anything else is refused, so
code, diffs, file paths and prompts can't end up here.

Who sees what: an organisation's owners and admins see all its projects; a member sees the
projects they linked and the ones a team of theirs was added to. Nobody sees another
organisation's anything."""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from devdash.team import store
from devdash.team.store import later, now

WEB = Path(__file__).parent / "web"
COOKIE = "__Host-devdash_team"
LOGIN_DAYS = 14
INVITE_DAYS = 7
DEVICE_CODE_MINUTES = 10
MIN_PASSWORD = 10
MAX_BODY = 64 * 1024
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
# Letters that can't be mistaken for one another, for codes people type.
CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# Always answered: this computer. Public addresses come from `devdash host` (set_reach), and
# DEVDASH_TEAM_HOSTS adds more (comma-separated).
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
SETUP_FILE = "setup-code"
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
ADMINS = ("owner", "admin")


class Limiter:
    """At most `count` events a `seconds` per key, for guessing passwords and codes."""

    def __init__(self, count: int, seconds: float):
        self.count, self.seconds = count, seconds
        self.seen: dict[str, deque] = defaultdict(deque)

    def hit(self, key: str) -> None:
        moment = time.monotonic()
        events = self.seen[key]
        while events and moment - events[0] > self.seconds:
            events.popleft()
        if len(events) >= self.count:
            raise HTTPException(429, "Too many tries. Wait a little and try again.")
        events.append(moment)


# ---------------------------------------------------------------- passwords, tokens, settings

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


# Checked against when the username doesn't exist, so a wrong name takes as long as a wrong password.
_DUMMY = hash_password(secrets.token_hex(8))


def check_password(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    attempt = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(attempt, bytes.fromhex(digest))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_code(groups: int = 2) -> str:
    return "-".join("".join(secrets.choice(CODE_LETTERS) for _ in range(4)) for _ in range(groups))


def plain_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def _setting(conn, name: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
    return row["value"] if row else None


def _set(conn, name: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (name, value))


def setup_code() -> str | None:
    """The one-time code that makes someone the server's admin, while nobody is: made the
    first time, kept (hashed in the database, readable in a file only this account can
    open) until it's used."""
    conn = store.connect()
    try:
        if conn.execute("SELECT 1 FROM people WHERE admin = 1").fetchone():
            _forget_setup_code(conn)
            return None
        path = store.home() / SETUP_FILE
        stored = _setting(conn, "setup")
        if stored and path.is_file():
            code = path.read_text().strip()
            if hmac.compare_digest(token_hash(plain_code(code)), stored):
                return code
        code = new_code(3)
        _set(conn, "setup", token_hash(plain_code(code)))
        path.write_text(code + "\n")
        if os.name == "posix":
            path.chmod(0o600)
        return code
    finally:
        conn.close()


def _forget_setup_code(conn) -> None:
    with conn:
        conn.execute("DELETE FROM settings WHERE name = 'setup'")
    (store.home() / SETUP_FILE).unlink(missing_ok=True)


def reach() -> dict:
    """How the server is reached: its public HTTPS addresses (as origins) and what's in
    front of it ('cloudflare', 'caddy' or 'none'), noted by `devdash host`."""
    conn = store.connect()
    value = _setting(conn, "reach")
    conn.close()
    found = json.loads(value) if value else {}
    return {"origins": set(found.get("origins", [])), "proxy": found.get("proxy", "none")}


def set_reach(origins: list[str], proxy: str = "none") -> None:
    conn = store.connect()
    _set(conn, "reach", json.dumps({"origins": [o.rstrip("/").lower() for o in origins], "proxy": proxy}))
    conn.close()


def server_name(conn) -> str:
    return _setting(conn, "name") or "Teams"


def signups_open(conn) -> bool:
    return _setting(conn, "signup") != "invite"


# ---------------------------------------------------------------- request bodies

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _no_control(value):
    if isinstance(value, str) and CONTROL.search(value):
        raise ValueError("contains control characters")
    return value


class Account(Strict):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=60)
    # Joining an organisation as the account is made.
    invite: str | None = Field(default=None, max_length=100)

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        if not USERNAME.match(value):
            raise ValueError("use 2 to 32 letters, digits, dots, dashes or underscores")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return _no_control(value)


class Setup(Strict):
    code: str = Field(max_length=40)


class Login(Strict):
    username: str = Field(max_length=32)
    password: str = Field(max_length=200)


class NewPassword(Strict):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


class Named(Strict):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return _no_control(value)


class ServerSettings(Strict):
    signup: Literal["open", "invite"]


class NewInvite(Strict):
    role: Literal["admin", "member"] = "member"
    team_id: int | None = None


class InviteRef(Strict):
    invite: str = Field(max_length=100)


class RoleChange(Strict):
    role: Literal["admin", "member"]


class PersonRef(Strict):
    person_id: int


class TeamRef(Strict):
    team_id: int


class CodeRef(Strict):
    code: str = Field(max_length=20)


class Approve(CodeRef):
    org_id: int


class DeviceStart(Strict):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return _no_control(value)


class DeviceToken(Strict):
    device_code: str = Field(max_length=100)


class LinkProject(Strict):
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return _no_control(value)


class Explanation(Strict):
    summary: str = Field(max_length=2000)
    change: str = Field(max_length=2000)
    improves: list[str] = Field(default=[], max_length=12)
    tradeoffs: list[str] = Field(default=[], max_length=12)
    check: str | None = Field(default=None, max_length=1000)

    @field_validator("improves", "tradeoffs")
    @classmethod
    def _items(cls, items: list[str]) -> list[str]:
        if any(len(item) > 500 for item in items):
            raise ValueError("each item is at most 500 characters")
        return [item.strip() for item in items if item.strip()]


class Share(Strict):
    project_id: int
    subject: str = Field(min_length=1, max_length=120)
    ref: str = Field(min_length=1, max_length=200)
    explanation: Explanation

    @field_validator("subject", "ref")
    @classmethod
    def _text(cls, value):
        return _no_control(value)


# ---------------------------------------------------------------- the app

def create_team_app() -> FastAPI:
    app = FastAPI(title="DevDash Teams", docs_url=None, redoc_url=None, openapi_url=None)
    wrong_by_name = Limiter(10, 300)     # wrong passwords per username
    wrong_by_visitor = Limiter(20, 300)  # wrong passwords per visitor
    signups = Limiter(5, 3600)           # accounts made per visitor
    codes = Limiter(10, 60)              # setup, invite and linking codes tried per visitor
    starts = Limiter(10, 60)             # linking started per visitor
    extra = {h.strip().lower() for h in os.environ.get("DEVDASH_TEAM_HOSTS", "").split(",") if h.strip()}

    def visitor(request: Request) -> str:
        """Who is asking, for the limits: the address the proxy in front saw. Cloudflare's
        header is trusted only behind Cloudflare, since anyone can send it."""
        proxy = reach()["proxy"]
        if proxy == "cloudflare" and request.headers.get("cf-connecting-ip"):
            return request.headers["cf-connecting-ip"]
        if proxy in ("caddy", "cloudflare") and request.headers.get("x-forwarded-for"):
            # The proxy adds the address it saw last.
            return request.headers["x-forwarded-for"].split(",")[-1].strip()
        return request.client.host if request.client else "?"

    def allowed_hosts() -> set[str]:
        return LOCAL_HOSTS | extra | {urlsplit(origin).hostname or "" for origin in reach()["origins"]}

    def same_site(origin: str, request: Request) -> bool:
        """Whether a page's origin is this server: the address it was asked on (a proxy may
        pass that as X-Forwarded-Host), or a public address `devdash host` noted."""
        asked = {(request.headers.get(name) or "").lower() for name in ("host", "x-forwarded-host")} - {""}
        return origin.split("://", 1)[-1] in asked or origin in reach()["origins"]

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Only its own addresses: a site that points its name here (DNS rebinding) is refused.
        if (request.url.hostname or "").lower() not in allowed_hosts():
            return JSONResponse({"detail": "Unknown address"}, status_code=421)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_BODY):
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # A form from another site can't send JSON, or DELETE at all.
            if request.method != "DELETE" and not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "Send JSON"}, status_code=415)
            # A page on another site can't make changes with someone's sign-in.
            origin = (request.headers.get("origin") or "").lower()
            if origin and not same_site(origin, request):
                return JSONResponse({"detail": "Request from another site refused"}, status_code=403)
        response = await call_next(request)
        headers = response.headers
        headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https":
            headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/"):
            headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------ who is asking

    def person(request: Request) -> dict:
        token = request.cookies.get(COOKIE)
        if token:
            conn = store.connect()
            row = conn.execute(
                "SELECT p.id, p.username, p.name, p.admin FROM logins l JOIN people p ON p.id = l.person_id"
                " WHERE l.token_hash = ? AND l.expires_at > ?", (token_hash(token), now())).fetchone()
            conn.close()
            if row:
                return {**dict(row), "admin": bool(row["admin"])}
        raise HTTPException(401, "Sign in first.")

    def member_of(conn, me: dict, org_id: int) -> dict:
        """The person in one organisation, with their role there; 404 to anyone outside it."""
        row = conn.execute("SELECT role FROM members WHERE org_id = ? AND person_id = ?", (org_id, me["id"])).fetchone()
        if row is None:
            raise HTTPException(404, "No such organisation, or you're not in it.")
        return {**me, "org_id": org_id, "role": row["role"]}

    def org_admin(conn, me: dict, org_id: int) -> dict:
        found = member_of(conn, me, org_id)
        if found["role"] not in ADMINS:
            raise HTTPException(403, "Only an owner or admin of this organisation can do that.")
        return found

    def device(request: Request) -> dict:
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            conn = store.connect()
            row = conn.execute(
                "SELECT p.id, p.username, p.name, d.id AS device_id, d.org_id, m.role FROM devices d"
                " JOIN people p ON p.id = d.person_id"
                " JOIN members m ON m.org_id = d.org_id AND m.person_id = d.person_id WHERE d.token_hash = ?",
                (token_hash(header[7:].strip()),)).fetchone()
            if row:
                with conn:
                    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now(), row["device_id"]))
                conn.close()
                return dict(row)
            conn.close()
        raise HTTPException(401, "This DevDash isn't linked. Link it again from Settings.")

    def sign_in(response: Response, person_id: int) -> None:
        token = secrets.token_urlsafe(32)
        conn = store.connect()
        with conn:
            conn.execute("DELETE FROM logins WHERE expires_at < ?", (now(),))
            conn.execute("INSERT INTO logins (token_hash, person_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                         (token_hash(token), person_id, now(), later(days=LOGIN_DAYS)))
        conn.close()
        response.set_cookie(COOKIE, token, max_age=LOGIN_DAYS * 86400, path="/", secure=True, httponly=True,
                            samesite="strict")

    def check_new_password(password: str, username: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise HTTPException(400, f"Use a password of at least {MIN_PASSWORD} characters.")
        if username.lower() in password.lower():
            raise HTTPException(400, "Don't put your username in your password.")

    def open_invite(conn, token: str):
        row = conn.execute(
            "SELECT i.id, i.org_id, i.role, i.team_id, o.name AS org FROM invites i JOIN orgs o ON o.id = i.org_id"
            " WHERE i.token_hash = ? AND i.used_by IS NULL AND i.expires_at > ?", (token_hash(token), now())).fetchone()
        if row is None:
            raise HTTPException(403, "This invite has been used, cancelled or has run out. Ask for a new one.")
        return row

    def use_invite(conn, invite, person_id: int) -> None:
        conn.execute("INSERT OR IGNORE INTO members (org_id, person_id, role, joined_at) VALUES (?, ?, ?, ?)",
                     (invite["org_id"], person_id, invite["role"], now()))
        conn.execute("UPDATE invites SET used_by = ? WHERE id = ?", (person_id, invite["id"]))
        if invite["team_id"] is not None:
            conn.execute("INSERT OR IGNORE INTO team_people (team_id, person_id) VALUES (?, ?)", (invite["team_id"], person_id))

    # ------------------------------------------------------------ the page

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(WEB / "index.html", media_type="text/html")

    @app.get("/app.js", include_in_schema=False)
    def script():
        return FileResponse(WEB / "app.js", media_type="text/javascript")

    @app.get("/app.css", include_in_schema=False)
    def styles():
        return FileResponse(WEB / "app.css", media_type="text/css")

    # ------------------------------------------------------------ accounts

    @app.get("/api/state")
    def state(request: Request):
        conn = store.connect()
        claimed = conn.execute("SELECT 1 FROM people WHERE admin = 1").fetchone() is not None
        result = {"server": server_name(conn), "claimed": claimed, "signup": signups_open(conn), "me": None, "orgs": []}
        try:
            me = person(request)
        except HTTPException:
            conn.close()
            return result
        orgs = conn.execute("SELECT o.id, o.name, m.role FROM members m JOIN orgs o ON o.id = m.org_id"
                            " WHERE m.person_id = ? ORDER BY o.name", (me["id"],)).fetchall()
        conn.close()
        return {**result, "me": me, "orgs": [dict(org) for org in orgs]}

    @app.post("/api/signup")
    def signup(body: Account, request: Request, response: Response):
        conn = store.connect()
        try:
            if not signups_open(conn) and not body.invite:
                raise HTTPException(403, "This server only takes new accounts with an invite link.")
            invite = open_invite(conn, body.invite) if body.invite else None
            signups.hit(visitor(request))
            check_new_password(body.password, body.username)
            if conn.execute("SELECT 1 FROM people WHERE username = ?", (body.username,)).fetchone():
                raise HTTPException(409, "That username is taken.")
            with conn:
                person_id = conn.execute(
                    "INSERT INTO people (username, name, password, created_at) VALUES (?, ?, ?, ?)",
                    (body.username, body.name or body.username, hash_password(body.password), now())).lastrowid
                if invite:
                    use_invite(conn, invite, person_id)
        finally:
            conn.close()
        sign_in(response, person_id)
        return {"ok": True}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        wrong_by_name.hit(body.username.lower())
        wrong_by_visitor.hit(visitor(request))
        conn = store.connect()
        row = conn.execute("SELECT id, password FROM people WHERE username = ?", (body.username,)).fetchone()
        conn.close()
        right = check_password(body.password, row["password"] if row else _DUMMY) and row is not None
        if not right:
            raise HTTPException(401, "That username or password isn't right.")
        sign_in(response, row["id"])
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(COOKIE)
        if token:
            conn = store.connect()
            with conn:
                conn.execute("DELETE FROM logins WHERE token_hash = ?", (token_hash(token),))
            conn.close()
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return {"ok": True}

    @app.put("/api/password")
    def change_password(body: NewPassword, response: Response, me: dict = Depends(person)):
        wrong_by_name.hit(me["username"].lower())
        conn = store.connect()
        try:
            stored = conn.execute("SELECT password FROM people WHERE id = ?", (me["id"],)).fetchone()["password"]
            if not check_password(body.current, stored):
                raise HTTPException(403, "The current password is wrong.")
            check_new_password(body.new, me["username"])
            with conn:
                conn.execute("UPDATE people SET password = ? WHERE id = ?", (hash_password(body.new), me["id"]))
                # Every other browser is signed out.
                conn.execute("DELETE FROM logins WHERE person_id = ?", (me["id"],))
        finally:
            conn.close()
        sign_in(response, me["id"])
        return {"ok": True}

    @app.post("/api/setup")
    def claim(body: Setup, request: Request, me: dict = Depends(person)):
        """Makes the signed-in person the server's admin, with the code printed where it started."""
        codes.hit(visitor(request))
        conn = store.connect()
        try:
            stored = _setting(conn, "setup")
            if conn.execute("SELECT 1 FROM people WHERE admin = 1").fetchone() or not stored:
                raise HTTPException(409, "This server already has an admin.")
            if not hmac.compare_digest(token_hash(plain_code(body.code)), stored):
                raise HTTPException(403, "That setup code isn't right. It's shown where the server was started.")
            with conn:
                conn.execute("UPDATE people SET admin = 1 WHERE id = ?", (me["id"],))
            _forget_setup_code(conn)
        finally:
            conn.close()
        return {"ok": True}

    @app.put("/api/server")
    def server_settings(body: ServerSettings, me: dict = Depends(person)):
        if not me["admin"]:
            raise HTTPException(403, "Only the server's admin can change that.")
        conn = store.connect()
        _set(conn, "signup", body.signup)
        conn.close()
        return {"signup": body.signup == "open"}

    # ------------------------------------------------------------ organisations

    @app.post("/api/orgs")
    def new_org(body: Named, me: dict = Depends(person)):
        conn = store.connect()
        try:
            if conn.execute("SELECT COUNT(*) FROM members WHERE person_id = ? AND role = 'owner'", (me["id"],)).fetchone()[0] >= 10:
                raise HTTPException(400, "You own 10 organisations already.")
            with conn:
                org_id = conn.execute("INSERT INTO orgs (name, created_at) VALUES (?, ?)", (body.name, now())).lastrowid
                conn.execute("INSERT INTO members (org_id, person_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
                             (org_id, me["id"], now()))
        finally:
            conn.close()
        return {"id": org_id, "name": body.name, "role": "owner"}

    @app.put("/api/orgs/{org_id}")
    def rename_org(org_id: int, body: Named, me: dict = Depends(person)):
        conn = store.connect()
        try:
            org_admin(conn, me, org_id)
            with conn:
                conn.execute("UPDATE orgs SET name = ? WHERE id = ?", (body.name, org_id))
        finally:
            conn.close()
        return {"ok": True}

    @app.delete("/api/orgs/{org_id}")
    def delete_org(org_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            if member_of(conn, me, org_id)["role"] != "owner":
                raise HTTPException(403, "Only the owner can delete the organisation.")
            with conn:
                conn.execute("DELETE FROM orgs WHERE id = ?", (org_id,))
        finally:
            conn.close()
        return {"ok": True}

    @app.delete("/api/orgs/{org_id}/me")
    def leave_org(org_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            if member_of(conn, me, org_id)["role"] == "owner":
                raise HTTPException(400, "The owner can't leave. Delete the organisation instead.")
            with conn:
                forget_member(conn, org_id, me["id"])
        finally:
            conn.close()
        return {"ok": True}

    def forget_member(conn, org_id: int, person_id: int) -> None:
        """Takes someone out of an organisation: its teams, their DevDashes linked to it, and
        what they shared there go too."""
        conn.execute("DELETE FROM members WHERE org_id = ? AND person_id = ?", (org_id, person_id))
        conn.execute("DELETE FROM team_people WHERE person_id = ? AND team_id IN (SELECT id FROM teams WHERE org_id = ?)",
                     (person_id, org_id))
        conn.execute("DELETE FROM devices WHERE person_id = ? AND org_id = ?", (person_id, org_id))
        conn.execute("DELETE FROM shared WHERE person_id = ? AND project_id IN (SELECT id FROM projects WHERE org_id = ?)",
                     (person_id, org_id))

    # ------------------------------------------------------------ invites

    @app.post("/api/orgs/{org_id}/invites")
    def invite(org_id: int, body: NewInvite, me: dict = Depends(person)):
        conn = store.connect()
        try:
            found = org_admin(conn, me, org_id)
            if body.role == "admin" and found["role"] != "owner":
                raise HTTPException(403, "Only the owner can invite admins.")
            if body.team_id is not None and not conn.execute("SELECT 1 FROM teams WHERE id = ? AND org_id = ?",
                                                             (body.team_id, org_id)).fetchone():
                raise HTTPException(404, "No such team.")
            token = secrets.token_urlsafe(24)
            with conn:
                conn.execute(
                    "INSERT INTO invites (org_id, token_hash, role, team_id, created_by, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (org_id, token_hash(token), body.role, body.team_id, me["id"], now(), later(days=INVITE_DAYS)))
        finally:
            conn.close()
        # The page puts it after # in the link, so it never reaches a server log.
        return {"invite": token, "expires_in_days": INVITE_DAYS}

    @app.get("/api/orgs/{org_id}/invites")
    def invites(org_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            org_admin(conn, me, org_id)
            rows = conn.execute(
                "SELECT i.id, i.role, t.name AS team, p.name AS created_by, i.created_at, i.expires_at FROM invites i"
                " LEFT JOIN teams t ON t.id = i.team_id LEFT JOIN people p ON p.id = i.created_by"
                " WHERE i.org_id = ? AND i.used_by IS NULL AND i.expires_at > ? ORDER BY i.created_at DESC",
                (org_id, now())).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    @app.delete("/api/orgs/{org_id}/invites/{invite_id}")
    def cancel_invite(org_id: int, invite_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            org_admin(conn, me, org_id)
            with conn:
                conn.execute("DELETE FROM invites WHERE id = ? AND org_id = ? AND used_by IS NULL", (invite_id, org_id))
        finally:
            conn.close()
        return {"ok": True}

    @app.post("/api/invites/check")
    def check_invite(body: InviteRef, request: Request):
        """The organisation an invite is for, before someone signs up or accepts it."""
        codes.hit(visitor(request))
        conn = store.connect()
        try:
            row = open_invite(conn, body.invite)
        finally:
            conn.close()
        return {"org": row["org"], "role": row["role"]}

    @app.post("/api/invites/accept")
    def accept_invite(body: InviteRef, request: Request, me: dict = Depends(person)):
        codes.hit(visitor(request))
        conn = store.connect()
        try:
            row = open_invite(conn, body.invite)
            if conn.execute("SELECT 1 FROM members WHERE org_id = ? AND person_id = ?", (row["org_id"], me["id"])).fetchone():
                raise HTTPException(409, f"You're already in {row['org']}.")
            with conn:
                use_invite(conn, row, me["id"])
        finally:
            conn.close()
        return {"org_id": row["org_id"], "org": row["org"]}

    # ------------------------------------------------------------ people in an organisation

    @app.get("/api/orgs/{org_id}/people")
    def people(org_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            member_of(conn, me, org_id)
            rows = conn.execute("SELECT p.id, p.username, p.name, m.role, m.joined_at FROM members m"
                                " JOIN people p ON p.id = m.person_id WHERE m.org_id = ? ORDER BY p.name", (org_id,)).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    @app.put("/api/orgs/{org_id}/people/{person_id}/role")
    def set_role(org_id: int, person_id: int, body: RoleChange, me: dict = Depends(person)):
        conn = store.connect()
        try:
            if member_of(conn, me, org_id)["role"] != "owner":
                raise HTTPException(403, "Only the owner can change roles.")
            with conn:
                changed = conn.execute("UPDATE members SET role = ? WHERE org_id = ? AND person_id = ? AND role != 'owner'",
                                       (body.role, org_id, person_id)).rowcount
        finally:
            conn.close()
        if not changed:
            raise HTTPException(404, "No such person here, or they're the owner.")
        return {"ok": True}

    @app.delete("/api/orgs/{org_id}/people/{person_id}")
    def remove_person(org_id: int, person_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            found = org_admin(conn, me, org_id)
            row = conn.execute("SELECT role FROM members WHERE org_id = ? AND person_id = ?", (org_id, person_id)).fetchone()
            if row is None:
                raise HTTPException(404, "No such person here.")
            if row["role"] == "owner" or (row["role"] == "admin" and found["role"] != "owner"):
                raise HTTPException(403, "You can't remove them.")
            with conn:
                forget_member(conn, org_id, person_id)
        finally:
            conn.close()
        return {"ok": True}

    # ------------------------------------------------------------ teams

    @app.get("/api/orgs/{org_id}/teams")
    def teams(org_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            member_of(conn, me, org_id)
            rows = conn.execute("SELECT id, name FROM teams WHERE org_id = ? ORDER BY name", (org_id,)).fetchall()
            members = conn.execute("SELECT tp.team_id, p.id, p.name FROM team_people tp JOIN people p ON p.id = tp.person_id"
                                   " JOIN teams t ON t.id = tp.team_id WHERE t.org_id = ? ORDER BY p.name", (org_id,)).fetchall()
        finally:
            conn.close()
        return [{**dict(team), "people": [{"id": m["id"], "name": m["name"]} for m in members if m["team_id"] == team["id"]]}
                for team in rows]

    @app.post("/api/orgs/{org_id}/teams")
    def new_team(org_id: int, body: Named, me: dict = Depends(person)):
        conn = store.connect()
        try:
            org_admin(conn, me, org_id)
            with conn:
                team_id = conn.execute("INSERT INTO teams (org_id, name, created_at) VALUES (?, ?, ?)",
                                       (org_id, body.name, now())).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, "There's already a team with that name.")
        finally:
            conn.close()
        return {"id": team_id, "name": body.name, "people": []}

    def team_in(conn, org_id: int, team_id: int) -> None:
        if not conn.execute("SELECT 1 FROM teams WHERE id = ? AND org_id = ?", (team_id, org_id)).fetchone():
            raise HTTPException(404, "No such team.")

    @app.delete("/api/orgs/{org_id}/teams/{team_id}")
    def delete_team(org_id: int, team_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            org_admin(conn, me, org_id)
            team_in(conn, org_id, team_id)
            with conn:
                conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        finally:
            conn.close()
        return {"ok": True}

    @app.post("/api/orgs/{org_id}/teams/{team_id}/people")
    def add_to_team(org_id: int, team_id: int, body: PersonRef, me: dict = Depends(person)):
        conn = store.connect()
        try:
            org_admin(conn, me, org_id)
            team_in(conn, org_id, team_id)
            if not conn.execute("SELECT 1 FROM members WHERE org_id = ? AND person_id = ?", (org_id, body.person_id)).fetchone():
                raise HTTPException(404, "They're not in this organisation.")
            with conn:
                conn.execute("INSERT OR IGNORE INTO team_people (team_id, person_id) VALUES (?, ?)", (team_id, body.person_id))
        finally:
            conn.close()
        return {"ok": True}

    @app.delete("/api/orgs/{org_id}/teams/{team_id}/people/{person_id}")
    def remove_from_team(org_id: int, team_id: int, person_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            org_admin(conn, me, org_id)
            team_in(conn, org_id, team_id)
            with conn:
                conn.execute("DELETE FROM team_people WHERE team_id = ? AND person_id = ?", (team_id, person_id))
        finally:
            conn.close()
        return {"ok": True}

    # ------------------------------------------------------------ projects

    def visible(conn, who: dict) -> list[int]:
        """Projects in who's organisation they can see; who carries org_id and role."""
        if who["role"] in ADMINS:
            return [row["id"] for row in conn.execute("SELECT id FROM projects WHERE org_id = ?", (who["org_id"],))]
        return [row["id"] for row in conn.execute(
            "SELECT id FROM projects WHERE org_id = ? AND created_by = ? UNION SELECT pt.project_id FROM project_teams pt"
            " JOIN team_people tp ON tp.team_id = pt.team_id JOIN projects p ON p.id = pt.project_id"
            " WHERE tp.person_id = ? AND p.org_id = ?", (who["org_id"], who["id"], who["id"], who["org_id"]))]

    def project_or_404(conn, who: dict, project_id: int):
        if project_id not in visible(conn, who):
            raise HTTPException(404, "No such project, or you don't have access to it.")
        return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()

    def can_manage(who: dict, project) -> bool:
        return who["role"] in ADMINS or project["created_by"] == who["id"]

    def project_list(conn, who: dict) -> list[dict]:
        ids = visible(conn, who)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT p.id, p.name, p.created_by, p.created_at, (SELECT COUNT(*) FROM shared s WHERE s.project_id = p.id)"
            f" AS shared, (SELECT MAX(shared_at) FROM shared s WHERE s.project_id = p.id) AS last_shared"
            f" FROM projects p WHERE p.id IN ({marks}) ORDER BY p.name", ids).fetchall()
        return [{**dict(row), "manage": can_manage(who, row)} for row in rows]

    @app.get("/api/orgs/{org_id}/projects")
    def projects(org_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            result = project_list(conn, member_of(conn, me, org_id))
        finally:
            conn.close()
        return result

    @app.get("/api/orgs/{org_id}/projects/{project_id}")
    def project(org_id: int, project_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            who = member_of(conn, me, org_id)
            row = project_or_404(conn, who, project_id)
            teams = conn.execute("SELECT t.id, t.name FROM project_teams pt JOIN teams t ON t.id = pt.team_id"
                                 " WHERE pt.project_id = ? ORDER BY t.name", (project_id,)).fetchall()
            items = conn.execute(
                "SELECT s.id, s.subject, s.explanation, s.shared_at, s.person_id, p.name AS person FROM shared s"
                " JOIN people p ON p.id = s.person_id WHERE s.project_id = ? ORDER BY s.shared_at DESC LIMIT 200",
                (project_id,)).fetchall()
        finally:
            conn.close()
        return {
            "id": row["id"], "name": row["name"], "manage": can_manage(who, row),
            "teams": [dict(team) for team in teams],
            "shared": [{**{k: item[k] for k in ("id", "subject", "shared_at", "person")},
                        "explanation": json.loads(item["explanation"]),
                        "mine": item["person_id"] == me["id"],
                        "removable": item["person_id"] == me["id"] or who["role"] in ADMINS} for item in items],
        }

    def managed_project(conn, me: dict, org_id: int, project_id: int):
        who = member_of(conn, me, org_id)
        row = project_or_404(conn, who, project_id)
        if not can_manage(who, row):
            raise HTTPException(403, "Only the person who linked this project, or an admin, can do that.")
        return row

    @app.post("/api/orgs/{org_id}/projects/{project_id}/teams")
    def add_team(org_id: int, project_id: int, body: TeamRef, me: dict = Depends(person)):
        conn = store.connect()
        try:
            managed_project(conn, me, org_id, project_id)
            team_in(conn, org_id, body.team_id)
            with conn:
                conn.execute("INSERT OR IGNORE INTO project_teams (project_id, team_id) VALUES (?, ?)", (project_id, body.team_id))
        finally:
            conn.close()
        return {"ok": True}

    @app.delete("/api/orgs/{org_id}/projects/{project_id}/teams/{team_id}")
    def remove_team(org_id: int, project_id: int, team_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            managed_project(conn, me, org_id, project_id)
            with conn:
                conn.execute("DELETE FROM project_teams WHERE project_id = ? AND team_id = ?", (project_id, team_id))
        finally:
            conn.close()
        return {"ok": True}

    @app.delete("/api/orgs/{org_id}/projects/{project_id}")
    def delete_project(org_id: int, project_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            managed_project(conn, me, org_id, project_id)
            with conn:
                conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        finally:
            conn.close()
        return {"ok": True}

    @app.delete("/api/orgs/{org_id}/shared/{shared_id}")
    def unshare(org_id: int, shared_id: int, me: dict = Depends(person)):
        conn = store.connect()
        try:
            who = member_of(conn, me, org_id)
            row = conn.execute("SELECT person_id, project_id FROM shared WHERE id = ?", (shared_id,)).fetchone()
            if row is None or row["project_id"] not in visible(conn, who):
                raise HTTPException(404, "Not found.")
            if row["person_id"] != me["id"] and who["role"] not in ADMINS:
                raise HTTPException(403, "Only the person who shared it, or an admin, can remove it.")
            with conn:
                conn.execute("DELETE FROM shared WHERE id = ?", (shared_id,))
        finally:
            conn.close()
        return {"ok": True}

    # ------------------------------------------------------------ linked DevDashes

    @app.get("/api/devices")
    def devices(me: dict = Depends(person)):
        conn = store.connect()
        rows = conn.execute("SELECT d.id, d.name, o.name AS org, d.created_at, d.last_seen FROM devices d"
                            " JOIN orgs o ON o.id = d.org_id WHERE d.person_id = ? ORDER BY d.created_at", (me["id"],)).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @app.delete("/api/devices/{device_id}")
    def unlink_device(device_id: int, me: dict = Depends(person)):
        conn = store.connect()
        with conn:
            conn.execute("DELETE FROM devices WHERE id = ? AND person_id = ?", (device_id, me["id"]))
        conn.close()
        return {"ok": True}

    @app.post("/api/device/start")
    def device_start(body: DeviceStart, request: Request):
        """A DevDash asks to link: it gets a secret to poll with and a short code for its
        person to approve in their browser."""
        starts.hit(visitor(request))
        secret = secrets.token_urlsafe(32)
        user_code = new_code()
        conn = store.connect()
        with conn:
            conn.execute("DELETE FROM device_codes WHERE expires_at < ?", (now(),))
            conn.execute("INSERT INTO device_codes (code_hash, user_code, name, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                         (token_hash(secret), plain_code(user_code), body.name, now(), later(minutes=DEVICE_CODE_MINUTES)))
        conn.close()
        return {"device_code": secret, "user_code": user_code, "expires_in": DEVICE_CODE_MINUTES * 60, "interval": 3}

    def pending_code(conn, code: str):
        row = conn.execute("SELECT * FROM device_codes WHERE user_code = ? AND expires_at > ? AND person_id IS NULL",
                           (plain_code(code), now())).fetchone()
        if row is None:
            raise HTTPException(404, "That code isn't right, or it ran out. Start linking again in DevDash.")
        return row

    @app.post("/api/device/check")
    def device_check(body: CodeRef, request: Request, me: dict = Depends(person)):
        codes.hit(visitor(request))
        conn = store.connect()
        try:
            row = pending_code(conn, body.code)
        finally:
            conn.close()
        return {"name": row["name"], "created_at": row["created_at"]}

    @app.post("/api/device/approve")
    def device_approve(body: Approve, request: Request, me: dict = Depends(person)):
        codes.hit(visitor(request))
        conn = store.connect()
        try:
            member_of(conn, me, body.org_id)
            row = pending_code(conn, body.code)
            with conn:
                conn.execute("UPDATE device_codes SET person_id = ?, org_id = ? WHERE code_hash = ?",
                             (me["id"], body.org_id, row["code_hash"]))
        finally:
            conn.close()
        return {"ok": True}

    @app.post("/api/device/token")
    def device_token(body: DeviceToken):
        conn = store.connect()
        try:
            row = conn.execute("SELECT * FROM device_codes WHERE code_hash = ?", (token_hash(body.device_code),)).fetchone()
            if row is None or row["expires_at"] <= now():
                raise HTTPException(410, "Linking ran out. Start again.")
            if row["person_id"] is None:
                return JSONResponse({"pending": True}, status_code=202)
            token = "ddt_" + secrets.token_urlsafe(32)
            with conn:
                conn.execute("DELETE FROM device_codes WHERE code_hash = ?", (row["code_hash"],))
                conn.execute("INSERT INTO devices (person_id, org_id, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                             (row["person_id"], row["org_id"], row["name"], token_hash(token), now()))
            me = conn.execute("SELECT name, username FROM people WHERE id = ?", (row["person_id"],)).fetchone()
            org = conn.execute("SELECT name FROM orgs WHERE id = ?", (row["org_id"],)).fetchone()
        finally:
            conn.close()
        return {"token": token, "person": dict(me), "org": org["name"], "org_id": row["org_id"]}

    # ------------------------------------------------------------ for a linked DevDash

    @app.get("/api/agent/me")
    def agent_me(me: dict = Depends(device)):
        conn = store.connect()
        org = conn.execute("SELECT name FROM orgs WHERE id = ?", (me["org_id"],)).fetchone()
        result = project_list(conn, me)
        conn.close()
        return {"person": {"name": me["name"], "username": me["username"], "role": me["role"]}, "org": org["name"],
                "org_id": me["org_id"], "projects": result}

    @app.post("/api/agent/projects")
    def agent_link(body: LinkProject, me: dict = Depends(device)):
        """Links a project on the person's computer to their organisation: the one everyone's
        clone shares, made the first time. Someone who isn't in one of its teams is told to
        ask for access."""
        conn = store.connect()
        try:
            row = conn.execute("SELECT * FROM projects WHERE org_id = ? AND key = ?", (me["org_id"], body.key)).fetchone()
            if row is None:
                with conn:
                    project_id = conn.execute(
                        "INSERT INTO projects (org_id, key, name, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                        (me["org_id"], body.key, body.name, me["id"], now())).lastrowid
            elif row["id"] in visible(conn, me):
                project_id = row["id"]
            else:
                raise HTTPException(403, "This project is already in your organisation. Ask whoever linked it, or an "
                                         "admin, to add one of your teams to it.")
        finally:
            conn.close()
        return {"id": project_id, "name": body.name if row is None else row["name"]}

    @app.post("/api/agent/share")
    def agent_share(body: Share, me: dict = Depends(device)):
        conn = store.connect()
        try:
            project_or_404(conn, me, body.project_id)
            with conn:
                conn.execute(
                    "INSERT INTO shared (project_id, person_id, subject, explanation, ref, shared_at) VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (person_id, ref) DO UPDATE SET project_id = excluded.project_id,"
                    " subject = excluded.subject, explanation = excluded.explanation, shared_at = excluded.shared_at",
                    (body.project_id, me["id"], body.subject, body.explanation.model_dump_json(), body.ref, now()))
            shared_id = conn.execute("SELECT id FROM shared WHERE person_id = ? AND ref = ?", (me["id"], body.ref)).fetchone()["id"]
        finally:
            conn.close()
        return {"id": shared_id, "org_id": me["org_id"]}

    return app

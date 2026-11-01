"""The dashboard's side of team mode, under /api/team: hosting a team server from Settings,
linking this DevDash to one, linking projects and sharing an explanation. The local API's
own guard (localhost only, signed in) covers these like every other route."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from devdash.team import client, host

router = APIRouter()


class HostRequest(BaseModel):
    tunnel: Literal["network", "domain", "cloudflare", "none"] = "cloudflare"
    domain: str | None = Field(default=None, max_length=253)
    # For the company network: the server's IP and internal names.
    addresses: str | None = Field(default=None, max_length=500)


class LinkRequest(BaseModel):
    url: str = Field(max_length=300)


class RootCertificate(BaseModel):
    pem: str = Field(max_length=20_000)


class AccountRequest(BaseModel):
    url: str = Field(max_length=300)
    username: str = Field(max_length=32)
    password: str = Field(max_length=200)
    name: str | None = Field(default=None, max_length=60)
    org_name: str | None = Field(default=None, max_length=60)
    org_code: str | None = Field(default=None, max_length=20)
    org_id: int | None = None
    # True: a new account; False: one that exists.
    new: bool = True


class ModeRequest(BaseModel):
    mode: Literal["individual", "org"] | None


class ShareRequest(BaseModel):
    repo_id: int
    subject: str = Field(min_length=1, max_length=200)
    ref: str = Field(min_length=1, max_length=1000)
    explanation: dict


def _team(call, *args):
    try:
        return call(*args)
    except client.TeamError as error:
        raise HTTPException(409 if error.signed_out else 400, str(error))
    except host.HostError as error:
        raise HTTPException(400, str(error))


@router.get("/api/team")
def team():
    """Whether this computer hosts a team server, and whether this DevDash is linked to one."""
    return {"host": host.status(), "link": client.status()}


@router.post("/api/team/host")
def start_hosting(body: HostRequest):
    return _team(host.start, body.tunnel, body.domain, host.PORT, body.addresses)


@router.get("/api/team/host/root-certificate")
def root_certificate():
    """The root certificate of the hosted server's own authority, for devices to trust once."""
    path = host.root_certificate()
    if path is None:
        raise HTTPException(404, "There's no certificate yet: host on the company network first.")
    return FileResponse(path, media_type="application/x-x509-ca-cert", filename="devdash-team-root.crt")


@router.delete("/api/team/host")
def stop_hosting():
    return _team(host.stop)


@router.post("/api/team/link")
def start_link(body: LinkRequest):
    return _team(client.start, body.url)


@router.post("/api/team/link/poll")
def poll_link():
    return _team(client.poll)


@router.delete("/api/team/link")
def unlink_server():
    return _team(client.disconnect)


@router.post("/api/team/root-certificate")
def add_root_certificate(body: RootCertificate):
    """Trusts a company network team server's root certificate, for linking to it."""
    return _team(client.add_root_certificate, body.pem)


@router.post("/api/team/root-certificate/fetch")
def fetch_root_certificate(body: LinkRequest):
    """A company network server's root certificate and fingerprint, to show before trusting it."""
    return _team(client.fetch_root_certificate, body.url)


@router.get("/api/team/remote")
def remote():
    return _team(client.remote)


@router.post("/api/repos/{repo_id}/team")
def link_project(repo_id: int):
    return _team(client.link, repo_id)


@router.delete("/api/repos/{repo_id}/team")
def unlink_project(repo_id: int):
    return _team(client.unlink, repo_id)


@router.post("/api/team/check")
def check_server(body: LinkRequest):
    """Whether the team server answers, and whether this computer trusts its certificate."""
    return _team(client.check, body.url)


@router.post("/api/team/account")
def account(body: AccountRequest):
    """Makes an account (and an organisation, or joins one) or signs in, from DevDash's own
    screens, and links this DevDash. The password goes to the team server only."""
    try:
        if body.new:
            return client.register(body.url, body.username, body.name or "", body.password, body.org_name, body.org_code)
        return client.login(body.url, body.username, body.password, body.org_code, body.org_id)
    except client.TeamError as error:
        if error.orgs:
            return JSONResponse({"detail": str(error), "orgs": error.orgs}, status_code=409)
        raise HTTPException(409 if error.signed_out else 400, str(error))


@router.put("/api/team/mode")
def set_mode(body: ModeRequest):
    """Individual, in an organisation, or not picked yet (None shows the choice again)."""
    return _team(client.set_mode, body.mode)


@router.api_route("/api/team/org", methods=["GET", "PUT", "DELETE"])
@router.api_route("/api/team/org/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def organisation(request: Request, path: str = ""):
    """The linked organisation, managed from DevDash's own screens: passed on to the team
    server as the person who linked this DevDash (their role there decides what's allowed)."""
    body = None
    if request.method in ("POST", "PUT"):
        try:
            body = await request.json()
        except ValueError:
            body = {}
    from starlette.concurrency import run_in_threadpool

    query = f"?{request.url.query}" if request.url.query else ""
    return await run_in_threadpool(_team, client.org_call, request.method, (f"/{path}" if path else "") + query, body)


@router.post("/api/team/share")
def share(body: ShareRequest):
    return _team(client.share, body.repo_id, body.subject, body.ref, body.explanation)

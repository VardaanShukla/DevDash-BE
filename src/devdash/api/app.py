"""The local HTTP API the UI is built on. Listens on 127.0.0.1 only: it serves your code.
The response models below are the contract; FastAPI publishes them at /openapi.json."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from devdash import agenda as agenda_module
from devdash import auth
from devdash import compare as compare_module
from devdash import digest as digest_module
from devdash import explain as explain_module
from devdash import flow as flow_module
from devdash import functions as functions_module
from devdash import git
from devdash import home as home_module
from devdash import projectmap
from devdash import summary as summary_module
from devdash import todos as todos_module
from devdash import undo as undo_module
from devdash.capture import install
from devdash.store import connect

# The UI from Vite's dev server (5173) and its preview of a build (4173).
DEV_ORIGINS = [f"http://{host}:{port}" for port in (5173, 4173) for host in ("localhost", "127.0.0.1")]
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
# Answered without signing in: whether a password is set, signing in, and setting the
# first password (that one checks for itself).
OPEN_PATHS = {"/api/auth", "/api/auth/login", "/api/auth/password"}


class Repo(BaseModel):
    id: int
    name: str
    path: str
    exists: bool
    branch: str | None
    claude: bool
    ai: Literal["claude"] | None


class NewRepo(BaseModel):
    path: str
    ai: Literal["claude"] | None = None


class AiChoice(BaseModel):
    ai: Literal["claude"] | None


class AiTool(BaseModel):
    id: str
    name: str
    available: bool
    description: str


class SetupStep(BaseModel):
    id: str
    title: str
    detail: str
    command: str | None
    done: bool | None


class Setup(BaseModel):
    steps: list[SetupStep]


class RepoState(BaseModel):
    signature: str


class BranchInfo(BaseModel):
    name: str
    merged: bool
    gone: bool


class Branches(BaseModel):
    current: str | None
    default_base: str | None
    branches: list[str]
    details: list[BranchInfo]


class Sync(BaseModel):
    synced_at: str | None
    error: str | None


class Commit(BaseModel):
    sha: str
    short_sha: str
    author: str
    authored_at: str
    subject: str
    ai: bool


class FlowCommit(BaseModel):
    sha: str
    short_sha: str
    parents: list[str]
    author: str
    authored_at: str
    subject: str
    ai: bool
    # Branches whose tip is this commit; remote ones as origin/name.
    branches: list[str]


class Flow(BaseModel):
    current: str | None
    base: str | None
    commits: list[FlowCommit]
    # More history exists beyond the commits returned.
    truncated: bool


class Language(BaseModel):
    name: str
    files: int


class MapFile(BaseModel):
    path: str
    bytes: int


class MapArea(BaseModel):
    # The folder, relative to the project root; "" for files at the root.
    id: str
    name: str
    path: str
    # What the folder is usually for, guessed from its name; null when unknown.
    role: str | None
    files: int
    bytes: int
    languages: list[Language]
    # Its biggest files, at most 150; files counts them all.
    file_list: list[MapFile]


class MapEdge(BaseModel):
    # Code in source uses code in target.
    source: str
    target: str
    imports: int


class ProjectMap(BaseModel):
    revision: str
    total_files: int
    areas: list[MapArea]
    edges: list[MapEdge]
    # False when the project was too big to read every file for imports.
    complete: bool


class Line(BaseModel):
    kind: Literal["add", "delete", "context"]
    text: str
    old: int | None
    new: int | None


class Hunk(BaseModel):
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    context: str | None
    lines: list[Line]


class Function(BaseModel):
    name: str
    change: Literal["added", "deleted", "modified"]


class FileChange(BaseModel):
    path: str
    old_path: str | None
    status: Literal["added", "deleted", "modified", "renamed"]
    binary: bool
    generated: bool
    truncated: bool
    additions: int
    deletions: int
    functions: list[Function]
    hunks: list[Hunk]
    # Differs from the last commit (only set when uncommitted work is included).
    uncommitted: bool


class Totals(BaseModel):
    files: int
    additions: int
    deletions: int


class Comparison(BaseModel):
    base: str
    # What was actually compared against: origin/Dev when it is newer than Dev.
    base_ref: str
    head: str
    merge_base: str
    uncommitted: bool
    ahead: int
    behind: int
    commits: list[Commit]
    files: list[FileChange]
    totals: Totals


class UnitSide(BaseModel):
    start: int
    end: int
    lines: int
    code: str
    # The declaration line, as written.
    signature: str
    # The docstring or the comment right above it.
    doc: str | None
    params: list[str]
    returns: str | None
    is_async: bool
    # Functions and methods it calls, by name.
    calls: list[str]
    # Ifs, loops, catches and the like: the choices the code makes.
    decisions: int


class UnitFacts(BaseModel):
    params_added: list[str]
    params_removed: list[str]
    calls_added: list[str]
    calls_removed: list[str]
    # Each pair is [before, after].
    lines: list[int]
    decisions: list[int]
    is_async: list[bool]
    returns: list[str | None]
    doc_changed: bool
    signature_changed: bool


class Use(BaseModel):
    path: str
    line: int
    text: str


class FunctionExplanation(BaseModel):
    summary: str
    change: str
    improves: list[str]
    tradeoffs: list[str]
    check: str | None


class FunctionDetail(BaseModel):
    name: str
    kind: Literal["function", "method", "component", "class", "type"]
    change: Literal["added", "deleted", "modified"]
    before: UnitSide | None
    after: UnitSide | None
    # Only for a changed function: what differs between before and after.
    facts: UnitFacts | None
    # Other places its name appears, in the branch's files.
    used_by: list[Use]
    # Saved from an earlier Explain, if there is one.
    explanation: FunctionExplanation | None


class FileFunctions(BaseModel):
    path: str
    functions: list[FunctionDetail]


class ExplainFunctionRequest(BaseModel):
    path: str
    old_path: str | None = None
    name: str
    base: str | None = None
    head: str | None = None
    uncommitted: bool = False


class AiStatus(BaseModel):
    tool: str
    available: bool
    signed_in: bool | None  # None when Claude Code couldn't say
    model: str
    detail: str | None


class SignIn(BaseModel):
    state: Literal["idle", "waiting", "signed_in", "failed"]
    url: str | None  # the sign-in page, in case the browser didn't open
    detail: str | None


class SignInCode(BaseModel):
    code: str


class Readme(BaseModel):
    title: str | None
    intro: str | None
    first_sentence: str | None
    sections: list[str]


class Tech(BaseModel):
    name: str
    what: str


class RunCommand(BaseModel):
    where: str
    name: str
    command: str


class Person(BaseModel):
    name: str
    commits: int


class History(BaseModel):
    commits: int
    people: list[Person]
    first: str | None
    last: str | None
    # Commits in each of the last 12 weeks, oldest first.
    weeks: list[int]
    branches: int


class PartExplanation(BaseModel):
    name: str
    what: str


class ProjectExplanation(BaseModel):
    overview: str
    how_it_works: list[str]
    parts: list[PartExplanation]
    state: str


class Summary(BaseModel):
    revision: str
    name: str
    description: str | None
    readme: Readme | None
    license: str | None
    tech: list[Tech]
    other_dependencies: int
    run: list[RunCommand]
    entry_points: list[str]
    files: int
    bytes: int
    languages: list[Language]
    parts: list[MapArea]
    links: list[MapEdge]
    history: History
    explanation: ProjectExplanation | None


class ExplainProjectRequest(BaseModel):
    head: str | None = None


class LastCommit(BaseModel):
    subject: str
    author: str
    at: str


class ProjectCard(BaseModel):
    id: int
    name: str
    path: str
    exists: bool
    branch: str | None
    base: str | None
    ahead: int | None  # commits on the branch that the base doesn't have; None on the base itself
    uncommitted: int  # files changed but not committed
    last_commit: LastCommit | None
    commits_this_week: int
    branches: int


class DigestFlag(BaseModel):
    kind: str  # auth, payments, database, tests, secret: worth a look; config, deleted, big: worth knowing
    label: str
    reason: str
    line: str | None = None  # the changed line behind it, when there is one; secrets are masked


class DigestFunction(BaseModel):
    name: str
    change: str  # added, deleted or modified


class DigestFile(BaseModel):
    path: str
    status: str
    additions: int
    deletions: int
    functions: list[DigestFunction]
    flags: list[DigestFlag]


class DigestCommit(BaseModel):
    sha: str
    message: str
    count: int  # commits the prompt's files landed in


class DigestRepo(BaseModel):
    id: int
    name: str


class DigestChange(BaseModel):
    id: str
    repo: DigestRepo
    branch: str | None
    prompt: str | None
    prompt_recorded: bool  # False: an AI-co-authored commit; prompt is its message
    at: str
    commit: DigestCommit | None  # None while any of it isn't committed
    files: list[DigestFile]
    additions: int
    deletions: int
    flags: list[DigestFlag]
    risky: bool
    undone_at: str | None  # undone from the digest, and not put back since


class DigestClaude(BaseModel):
    prompts: int
    files: int
    functions: int
    additions: int
    deletions: int
    risky: int
    uncommitted: int


class DigestYou(BaseModel):
    files: int
    additions: int
    deletions: int
    commits: int


class ChangeRef(BaseModel):
    change: str  # a digest change's id


class Undone(BaseModel):
    change: str
    undone_at: str | None


class Digest(BaseModel):
    date: str
    claude: DigestClaude
    you: DigestYou
    changes: list[DigestChange]


class AuthStatus(BaseModel):
    password_set: bool
    signed_in: bool


class Login(BaseModel):
    username: str
    password: str


class NewPassword(BaseModel):
    # Needed the first time; leave it out to keep the current username.
    username: str | None = None
    password: str
    current: str | None = None


class Todo(BaseModel):
    id: int
    text: str
    repo_id: int | None  # None: not tied to a project
    branch: str | None  # None: the whole project
    created_at: str
    done_at: str | None  # None while open


class NewTodo(BaseModel):
    text: str
    repo_id: int | None = None
    branch: str | None = None


class TodoChange(BaseModel):
    text: str | None = None
    done: bool | None = None


class Event(BaseModel):
    title: str
    start: str  # a date for all-day events, otherwise a time with its offset
    end: str
    all_day: bool
    location: str | None


class Agenda(BaseModel):
    connected: bool
    source: str | None  # the calendar's site, never its private address
    events: list[Event]
    fetched_at: str | None
    error: str | None


class CalendarAddress(BaseModel):
    url: str


def create_app() -> FastAPI:
    app = FastAPI(title="DevDash", version="0.1.0")
    sync_locks: dict[int, threading.Lock] = {}
    sync_results: dict[int, dict] = {}
    app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["*"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # The API can change git hooks and Claude settings, so it answers only requests
        # addressed to this machine (a rebound DNS name fails here) and, for changes,
        # only JSON from no page or the DevDash UI.
        if (request.url.hostname or "") not in LOCAL_HOSTS:
            return JSONResponse({"detail": "DevDash only answers on localhost"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin not in DEV_ORIGINS and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse({"detail": "request from another site refused"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "send JSON"}, status_code=415)
        # With a password set, everything else needs a signed-in browser.
        if (request.url.path not in OPEN_PATHS and request.method != "OPTIONS" and auth.password_set()
                and not auth.signed_in(request.cookies.get(auth.COOKIE))):
            return JSONResponse({"detail": "sign in to DevDash"}, status_code=401)
        return await call_next(request)

    @app.get("/api/auth", response_model=AuthStatus)
    def auth_status(request: Request):
        """Whether a password is set, and whether this browser is signed in."""
        return AuthStatus(password_set=auth.password_set(), signed_in=auth.signed_in(request.cookies.get(auth.COOKIE)))

    @app.post("/api/auth/login", response_model=AuthStatus)
    def log_in(body: Login, response: Response):
        try:
            token = auth.log_in(body.username, body.password)
        except auth.TooManyTries as error:
            raise HTTPException(429, str(error))
        except auth.AuthError as error:
            raise HTTPException(401, str(error))
        _set_login(response, token)
        return AuthStatus(password_set=True, signed_in=True)

    @app.post("/api/auth/logout", response_model=AuthStatus)
    def log_out(request: Request, response: Response):
        auth.log_out(request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE, path="/")
        return AuthStatus(password_set=auth.password_set(), signed_in=False)

    @app.put("/api/auth/password", response_model=AuthStatus)
    def set_password(body: NewPassword, request: Request, response: Response):
        """Sets the first username and password, or changes them from a signed-in browser with
        the current password. Signs this browser in, and every other one out."""
        if auth.password_set() and not auth.signed_in(request.cookies.get(auth.COOKIE)):
            raise HTTPException(401, "sign in to DevDash")
        try:
            auth.set_password(body.password, body.current, name=body.username)
        except auth.AuthError as error:
            raise HTTPException(400, str(error))
        _set_login(response, auth.new_login())
        return AuthStatus(password_set=True, signed_in=True)

    @app.get("/api/repos", response_model=list[Repo])
    def list_repos():
        conn = connect()
        rows = conn.execute("SELECT id, name, path, claude FROM repos ORDER BY name").fetchall()
        conn.close()
        return [_repo(row) for row in rows]

    @app.post("/api/repos", response_model=Repo, status_code=201)
    def add_repo(body: NewRepo):
        path = Path(body.path.strip()).expanduser()
        if not path.is_dir():
            raise HTTPException(400, f"No folder at {path}")
        root = _install(install.connect_claude if body.ai == "claude" else install.init, path)
        return _repo(_row_for(root))

    @app.delete("/api/repos/{repo_id}", status_code=204)
    def remove_repo(repo_id: int):
        """Stops tracking the project. Its files and git history are not touched."""
        if not _install(lambda _: install.remove(repo_id), None):
            raise HTTPException(404, "repo not tracked")
        sync_results.pop(repo_id, None)

    @app.put("/api/repos/{repo_id}/ai", response_model=Repo)
    def set_ai(repo_id: int, body: AiChoice):
        path = _repo_path(repo_id)
        _install(install.connect_claude if body.ai == "claude" else install.disconnect_claude, path)
        return _repo(_row_for(path))

    @app.get("/api/repos/{repo_id}/setup", response_model=Setup)
    def setup(repo_id: int):
        path = _repo_path(repo_id)
        return Setup(steps=install.setup_steps(path, bool(_row_for(path)["claude"])))

    @app.get("/api/ai-tools", response_model=list[AiTool])
    def ai_tools():
        return install.AI_TOOLS

    @app.get("/api/repos/{repo_id}/state", response_model=RepoState)
    def repo_state(repo_id: int, uncommitted: bool = Query(False, description="Also watch uncommitted edits")):
        return RepoState(signature=compare_module.state(_repo_path(repo_id), uncommitted))

    @app.get("/api/repos/{repo_id}/branches", response_model=Branches)
    def list_branches(repo_id: int):
        path = _repo_path(repo_id)
        base = compare_module.default_base(path)
        return Branches(current=git.branch(path), default_base=base, branches=compare_module.branches(path),
                        details=compare_module.branch_details(path, base))

    @app.post("/api/repos/{repo_id}/sync", response_model=Sync)
    def sync(repo_id: int):
        """Fetches from the remote so merges and deleted branches on GitHub show up.
        Several open tabs share one fetch per repo."""
        path = _repo_path(repo_id)
        lock = sync_locks.setdefault(repo_id, threading.Lock())
        if lock.acquire(blocking=False):
            try:
                error = git.fetch(path)
                last = sync_results.get(repo_id, {})
                sync_results[repo_id] = {
                    "synced_at": last.get("synced_at") if error else datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "error": error,
                }
            finally:
                lock.release()
        else:
            with lock:  # another request is fetching; wait for its result
                pass
        return Sync(**sync_results.get(repo_id, {"synced_at": None, "error": None}))

    @app.get("/api/repos/{repo_id}/flow", response_model=Flow)
    def flow(repo_id: int, limit: int = Query(flow_module.LIMIT, ge=1, le=300)):
        """Recent commits across all branches, for drawing how the branches fork and merge."""
        path = _repo_path(repo_id)
        return Flow(current=git.branch(path), base=compare_module.default_base(path),
                    **flow_module.flow(path, limit))

    @app.get("/api/repos/{repo_id}/map", response_model=ProjectMap)
    def project_map(repo_id: int, head: str | None = Query(None, description="Defaults to the checked-out branch")):
        """The project's main parts and which parts use which, as committed on the branch."""
        path = _repo_path(repo_id)
        try:
            return projectmap.project_map(path, head or "HEAD")
        except git.GitError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/repos/{repo_id}/compare", response_model=Comparison)
    def compare(
        repo_id: int,
        base: str | None = Query(None, description="Defaults to main, or master"),
        head: str | None = Query(None, description="Defaults to the checked-out branch"),
        uncommitted: bool = Query(False, description="Include edits not committed yet"),
    ):
        path = _repo_path(repo_id)
        try:
            return compare_module.compare(path, base, head, uncommitted)
        except compare_module.CompareError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/repos/{repo_id}/functions", response_model=FileFunctions)
    def file_functions(
        repo_id: int,
        path: str = Query(..., description="The file, relative to the project"),
        old_path: str | None = Query(None, description="Its name before a rename"),
        base: str | None = None,
        head: str | None = None,
        uncommitted: bool = False,
    ):
        """Each function the branch changed in one file, before and after, with the facts
        git can show and any explanation written earlier."""
        repo = _repo_path(repo_id)
        changes = _function_changes(repo, path, old_path, base, head, uncommitted)
        for item in changes:
            item["explanation"] = explain_module.cached("function", _function_subject(path, item))
        return FileFunctions(path=path, functions=changes)

    @app.post("/api/repos/{repo_id}/explain/function", response_model=FunctionExplanation)
    def explain_function(repo_id: int, request: ExplainFunctionRequest):
        """Asks Claude Code what a changed function does, what it improves and what it costs."""
        repo = _repo_path(repo_id)
        changes = _function_changes(repo, request.path, request.old_path, request.base, request.head,
                                    request.uncommitted)
        item = next((c for c in changes if c["name"] == request.name), None)
        if item is None:
            raise HTTPException(404, f"{request.name} didn't change in {request.path}")
        head = request.head or git.branch(repo) or "HEAD"
        base_ref = compare_module.freshest(repo, request.base or compare_module.default_base(repo))
        context = {"commits": [c["subject"] for c in compare_module.commits(repo, f"{base_ref}..{head}")],
                   "used_by": item["used_by"]}
        try:
            return explain_module.explain("function", _function_subject(request.path, item), context)
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.get("/api/ai", response_model=AiStatus)
    def ai_status():
        """Whether Claude Code is installed here, to write explanations."""
        return explain_module.status()

    @app.post("/api/ai/sign-in", response_model=SignIn)
    def sign_in():
        """Starts signing in to Claude Code: the sign-in page opens in the browser."""
        try:
            return explain_module.start_login()
        except explain_module.ExplainError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/ai/sign-in", response_model=SignIn)
    def sign_in_status():
        return explain_module.login_status()

    @app.post("/api/ai/sign-in/code", response_model=SignIn)
    def sign_in_code(body: SignInCode):
        """Finishes signing in with the code the sign-in page shows."""
        if not body.code.strip() or len(body.code) > 2000:
            raise HTTPException(400, "paste the code the sign-in page shows")
        return explain_module.finish_login(body.code)

    @app.get("/api/repos/{repo_id}/summary", response_model=Summary)
    def project_summary(repo_id: int, head: str | None = Query(None, description="Defaults to the checked-out branch")):
        """The whole project on one page: what it is, what it's built with, its parts and history."""
        repo = _repo_path(repo_id)
        try:
            facts = summary_module.summary(repo, head or "HEAD")
        except git.GitError as error:
            raise HTTPException(400, str(error))
        return Summary(**facts, explanation=explain_module.cached("project", _project_subject(facts)))

    @app.post("/api/repos/{repo_id}/explain/project", response_model=ProjectExplanation)
    def explain_project(repo_id: int, request: ExplainProjectRequest):
        """Asks Claude Code to describe the project, from the facts on the summary."""
        repo = _repo_path(repo_id)
        try:
            facts = summary_module.summary(repo, request.head or "HEAD")
            return explain_module.explain("project", _project_subject(facts), _project_context(facts))
        except git.GitError as error:
            raise HTTPException(400, str(error))
        except explain_module.ExplainError as error:
            raise _explain_failed(error)

    @app.get("/api/home", response_model=list[ProjectCard])
    def home():
        """Every project at a glance, for the home page."""
        conn = connect()
        rows = conn.execute("SELECT id, name, path FROM repos ORDER BY name").fetchall()
        conn.close()
        return [{"id": row["id"], "name": row["name"], "path": row["path"],
                 **home_module.project(Path(row["path"]))} for row in rows]

    @app.get("/api/digest", response_model=Digest)
    def daily_digest(day: str | None = None):
        """What Claude changed on one day (today unless `day` is YYYY-MM-DD), prompt by
        prompt, newest first, with the functions touched and rule-based risk flags. Only
        projects connected to Claude are recorded."""
        try:
            return digest_module.digest(digest_module.parse_day(day))
        except digest_module.DigestError as error:
            raise HTTPException(400, str(error))

    @app.post("/api/digest/undo", response_model=Undone)
    def undo_change(body: ChangeRef):
        """Undoes one of Claude's changes in the working tree, all files or none, and
        commits nothing. 409 when a file changed since, so it no longer fits."""
        try:
            return Undone(change=body.change, undone_at=undo_module.undo(body.change))
        except undo_module.NotFound as error:
            raise HTTPException(404, str(error))
        except undo_module.UndoError as error:
            raise HTTPException(409, str(error))

    @app.post("/api/digest/put-back", response_model=Undone)
    def put_back_change(body: ChangeRef):
        """Puts an undone change back, if its files haven't changed since the undo."""
        try:
            undo_module.put_back(body.change)
        except undo_module.NotFound as error:
            raise HTTPException(404, str(error))
        except undo_module.UndoError as error:
            raise HTTPException(409, str(error))
        return Undone(change=body.change, undone_at=None)

    @app.get("/api/todos", response_model=list[Todo])
    def list_todos():
        """Open to-dos oldest first, then the most recently finished."""
        return todos_module.items()

    @app.post("/api/todos", response_model=Todo, status_code=201)
    def add_todo(body: NewTodo):
        try:
            return todos_module.add(body.text, body.repo_id, body.branch)
        except todos_module.TodoError as error:
            raise HTTPException(400, str(error))

    @app.patch("/api/todos/{todo_id}", response_model=Todo)
    def change_todo(todo_id: int, body: TodoChange):
        """Ticks a to-do off (or back on), or rewords it."""
        try:
            item = todos_module.update(todo_id, body.text, body.done)
        except todos_module.TodoError as error:
            raise HTTPException(400, str(error))
        if item is None:
            raise HTTPException(404, "no such to-do")
        return item

    @app.delete("/api/todos/{todo_id}", status_code=204)
    def remove_todo(todo_id: int):
        if not todos_module.remove(todo_id):
            raise HTTPException(404, "no such to-do")

    @app.get("/api/calendar", response_model=Agenda)
    def calendar(days: int = Query(7, ge=1, le=31), refresh: bool = Query(False, description="Fetch it now")):
        """The connected calendar's events from today on. Fetched at most every 5 minutes."""
        return agenda_module.agenda(days, refresh)

    @app.put("/api/calendar", response_model=Agenda)
    def connect_calendar(body: CalendarAddress):
        """Keeps the calendar's private iCal address, after checking it answers."""
        try:
            agenda_module.connect_calendar(body.url)
        except agenda_module.AgendaError as error:
            raise HTTPException(400, str(error))
        return agenda_module.agenda(7)

    @app.delete("/api/calendar", status_code=204)
    def disconnect_calendar():
        """Forgets the calendar's address."""
        agenda_module.disconnect()

    return app


def _set_login(response: Response, token: str) -> None:
    # HttpOnly keeps it from page scripts; Strict keeps other sites from sending it.
    response.set_cookie(auth.COOKIE, token, max_age=auth.LOGIN_DAYS * 86400, path="/", httponly=True,
                        samesite="strict")


def _explain_failed(error: "explain_module.ExplainError") -> HTTPException:
    # 424 tells the page to offer signing in to Claude (401 is for DevDash's own sign-in);
    # anything else is Claude Code failing.
    return HTTPException(424 if error.signed_out else 502, str(error))


def _function_changes(repo: Path, path: str, old_path: str | None, base: str | None, head: str | None,
                      uncommitted: bool) -> list[dict]:
    head = head or git.branch(repo) or "HEAD"
    base = base or compare_module.default_base(repo)
    if base is None:
        raise HTTPException(400, "no main, master, develop or dev branch to compare against; pick one")
    try:
        base_ref = compare_module.freshest(repo, base)
        merge_base = git.run(repo, "merge-base", base_ref, head).strip()
    except git.GitError as error:
        raise HTTPException(400, str(error))
    return functions_module.function_changes(repo, merge_base, None if uncommitted else head, path, old_path)


def _function_subject(path: str, item: dict) -> dict:
    # What the explanation depends on; the same code always gets the same one.
    return {"path": path, "name": item["name"], "kind": item["kind"], "change": item["change"],
            "before": (item["before"] or {}).get("code"), "after": (item["after"] or {}).get("code")}


def _project_subject(facts: dict) -> dict:
    """What the project description depends on. Counts and history change with every
    commit, so they go in `_project_context` instead, or the description would be lost
    each time."""
    names = {area["id"]: area["name"] for area in facts["parts"]}
    return {
        "name": facts["name"],
        "description": facts["description"],
        "readme": facts["readme"],
        "tech": [f"{t['name']}: {t['what']}" for t in facts["tech"]],
        "run": [f"{r['name']}: {r['command']} ({r['where']})" for r in facts["run"]],
        "entry_points": facts["entry_points"],
        "parts": [{"name": a["name"], "folder": a["path"] or "(top level)", "guessed_role": a["role"],
                   "languages": [l["name"] for l in a["languages"]]} for a in facts["parts"]],
        "uses": [f"{names[e['source']]} uses {names[e['target']]}" for e in facts["links"]],
    }


def _project_context(facts: dict) -> dict:
    history = facts["history"]
    return {
        "files": facts["files"],
        "languages": [f"{l['name']} ({l['files']} files)" for l in facts["languages"]],
        "files_per_part": {a["name"]: a["files"] for a in facts["parts"]},
        "history": {"commits": history["commits"], "people": len(history["people"]),
                    "first_commit": history["first"], "last_commit": history["last"],
                    "commits_per_week_last_12": history["weeks"]},
    }


def _repo(row) -> Repo:
    path = Path(row["path"])
    exists = git.toplevel(path) is not None
    claude = bool(row["claude"])
    return Repo(id=row["id"], name=row["name"], path=row["path"], exists=exists,
                branch=git.branch(path) if exists else None, claude=claude, ai="claude" if claude else None)


def _row_for(root: Path):
    conn = connect()
    row = conn.execute("SELECT id, name, path, claude FROM repos WHERE path = ?", (str(root),)).fetchone()
    conn.close()
    return row


def _install(action, path: Path | None):
    try:
        return action(path)
    except install.InitError as error:
        raise HTTPException(400, str(error))
    except json.JSONDecodeError:
        raise HTTPException(400, f"{install.claude_settings_path()} isn't valid JSON, so DevDash can't add its hooks")


def _repo_path(repo_id: int) -> Path:
    conn = connect()
    row = conn.execute("SELECT path FROM repos WHERE id = ?", (repo_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, "repo not tracked; run devdash init in it")
    path = Path(row["path"])
    if git.toplevel(path) is None:
        raise HTTPException(410, f"repo is no longer at {path}")
    return path

"""Plain-English explanations of a changed function or of the whole project, written by
the user's own Claude Code (`claude -p`), so DevDash needs no API key of its own. Only
asked for when someone clicks Explain, and saved, so the same code is explained once.

Claude runs in an empty folder with its tools turned off and a short system prompt of
DevDash's own: it sees only what the prompt holds, and DevDash's own recorder skips the
run. It uses a small, cheap model (Haiku) unless DEVDASH_CLAUDE_MODEL names another.

Signing in to Claude Code can happen from the dashboard too: `start_login` runs
`claude auth login`, which opens the browser, and `finish_login` passes it the code the
sign-in page shows, for when the browser can't hand it back by itself."""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from devdash.capture.record import INTERNAL_ENV
from devdash.store import connect
from devdash.store.db import now

TIMEOUT = 240
# Bump when a prompt changes, so explanations written for the old one are asked again.
PROMPT_VERSION = "1"
MAX_PROMPT_CHARS = 30_000
MODEL = os.environ.get("DEVDASH_CLAUDE_MODEL", "haiku")
SYSTEM_PROMPT = ("You turn code and facts about software projects into plain-English explanations for people "
                 "who don't write code. Reply with only the JSON object you are asked for.")
# What Claude Code says when its sign-in is missing or has run out.
SIGNED_OUT = re.compile(r"authenticat|oauth|not logged in|/login|log ?in again|invalid api key|"
                        r"credentials|unauthori[sz]ed", re.IGNORECASE)


class ExplainError(RuntimeError):
    def __init__(self, message: str, signed_out: bool = False):
        super().__init__(message)
        self.signed_out = signed_out


def status() -> dict:
    """Whether Claude Code is installed and signed in, and as whom. A sign-in that has run
    out still counts as signed in here; the first explanation finds out, with a 401. When
    it isn't installed, the official install commands for this computer come with it."""
    path = _find_claude()
    auth = _auth_status() if path else {}
    signed_in = auth.get("loggedIn") if path else None
    return {
        "tool": "claude",
        "available": path is not None,
        "signed_in": signed_in,
        "account": (auth.get("email") or auth.get("orgName")) if signed_in else None,
        "model": MODEL,
        "detail": None if path else "Install Claude Code to get explanations.",
        "install": [] if path else install_commands(),
    }


def install_commands(system: str | None = None) -> list[dict]:
    """Claude Code's official native installers (code.claude.com/docs/en/setup) for this
    computer's system: one shell on macOS and Linux, PowerShell or Command Prompt on Windows."""
    if (system or platform.system()) == "Windows":
        return [
            {"shell": "PowerShell", "command": "irm https://claude.ai/install.ps1 | iex"},
            {"shell": "Command Prompt",
             "command": "curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd"},
        ]
    return [{"shell": "Terminal", "command": "curl -fsSL https://claude.ai/install.sh | bash"}]


def _find_claude() -> str | None:
    """Claude Code on the PATH, or where the native installer puts it: a fresh install's
    folder may not be on the PATH that DevDash started with."""
    found = shutil.which("claude")
    if found:
        return found
    name = "claude.exe" if platform.system() == "Windows" else "claude"
    native = Path.home() / ".local" / "bin" / name
    return str(native) if native.is_file() and os.access(native, os.X_OK) else None


def _auth_status() -> dict:
    try:
        result = subprocess.run([_claude(), "auth", "status", "--json"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=20, env=_env())
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        # An older Claude Code without `auth status`: find out on the first explanation.
        return {}


def _claude() -> str:
    # The full path: on Windows Claude Code is claude.cmd or claude.exe, and running a
    # bare "claude" only finds a .exe.
    return _find_claude() or "claude"


def _env() -> dict:
    return {**os.environ, INTERNAL_ENV: "1"}


def key(kind: str, subject: dict) -> str:
    text = json.dumps({"kind": kind, "version": PROMPT_VERSION, **subject}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def cached(kind: str, subject: dict) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT result FROM explanations WHERE key = ?", (key(kind, subject),)).fetchone()
    conn.close()
    return json.loads(row["result"]) if row else None


def explain(kind: str, subject: dict, context: dict | None = None) -> dict:
    """The explanation for `subject` (what is explained, and the cache key), with
    `context` adding background that doesn't change the answer much, like commit titles."""
    found = cached(kind, subject)
    if found:
        return found
    prompt = PROMPTS[kind](subject, context or {})[:MAX_PROMPT_CHARS]
    result = _clean(kind, _parse(_run(prompt)))
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO explanations (key, kind, result, created_at) VALUES (?, ?, ?, ?)",
                     (key(kind, subject), kind, json.dumps(result), now()))
    conn.close()
    return result


def _function_prompt(subject: dict, context: dict) -> str:
    what = {"added": "It is new on this branch.", "deleted": "This branch removes it.",
            "modified": "This branch changes it."}[subject["change"]]
    parts = [
        "You explain code changes to people who don't write code: product managers, designers and testers.",
        "",
        f"File: {subject['path']}",
        f"{subject['kind'].capitalize()}: {subject['name']}. {what}",
    ]
    if context.get("commits"):
        parts += ["Commit titles on this branch:", *[f"- {c}" for c in context["commits"][:12]]]
    if context.get("used_by"):
        parts += ["Used in:", *[f"- {u['path']}:{u['line']}: {u['text']}" for u in context["used_by"][:6]]]
    if subject.get("before"):
        parts += ["", "Before:", "```", subject["before"], "```"]
    if subject.get("after"):
        parts += ["", "After:" if subject.get("before") else "The code:", "```", subject["after"], "```"]
    parts += [
        "",
        "Reply with only a JSON object and no other text, with these keys:",
        '"summary": one sentence on what this does now (for a removed one, what it did).',
        '"change": two or three sentences on what is different from before. For a new one: what it is for, '
        "when it runs and what uses it. For a removed one: what stops happening.",
        '"improves": up to 4 short phrases on what the change makes better or optimizes, such as speed, '
        "safety, clarity, a fixed bug or a new ability. An empty list if nothing is clear.",
        '"tradeoffs": up to 4 short phrases on what it costs or risks, such as more complexity, a slower case, '
        "or a behaviour change someone may notice. An empty list if none.",
        '"check": one sentence on what someone testing the app should try, or null.',
        "Use plain words. When a technical term can't be avoided, explain it in brackets. Say only what the "
        "code shows; write \"unclear\" rather than guess.",
    ]
    return "\n".join(parts)


def _project_prompt(subject: dict, context: dict) -> str:
    facts = json.dumps({**subject, **context}, indent=1)
    return "\n".join([
        "You explain software projects to people who don't write code: product managers, designers, new team members.",
        "Here are facts about one project, read from its files and git history:",
        "```json",
        facts,
        "```",
        "",
        "Reply with only a JSON object and no other text, with these keys:",
        '"overview": three or four sentences on what the project is, what problem it solves and who uses it.',
        '"how_it_works": a list of three to six short sentences that follow what happens, from what a person '
        "does to what the project does in response, naming the parts involved.",
        '"parts": a list of objects {"name": a part name exactly as given, "what": one sentence on its job}, '
        "for the parts that matter most, at most eight.",
        '"state": one or two sentences on how far along and how active it looks, from the history.',
        "Use plain words. When a technical term can't be avoided, explain it in brackets. Say only what the "
        "facts support; write \"unclear\" rather than guess.",
    ])


PROMPTS = {"function": _function_prompt, "project": _project_prompt}

FIELDS = {
    "function": {"summary": str, "change": str, "improves": list, "tradeoffs": list, "check": (str, type(None))},
    "project": {"overview": str, "how_it_works": list, "parts": list, "state": str},
}


def _run(prompt: str) -> str:
    if not _find_claude():
        raise ExplainError("Claude Code isn't installed on this computer, so DevDash can't write explanations.")
    flags = ["--output-format", "json", "--model", MODEL, "--system-prompt", SYSTEM_PROMPT,
             "--tools", "", "--no-session-persistence"]
    with tempfile.TemporaryDirectory(prefix="devdash-explain-") as folder:
        for attempt in (flags, flags[:4]):
            try:
                result = subprocess.run([_claude(), "-p", *attempt], input=prompt, cwd=folder, env=_env(),
                                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                raise ExplainError("Claude took too long to answer. Try again.")
            # An older Claude Code without the newer flags: ask again without them.
            if result.returncode != 0 and "unknown option" in result.stderr.lower() and attempt is flags:
                continue
            break
    if not result.stdout.strip():
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        _fail(lines[-1] if lines else "Claude Code didn't answer.")
    try:
        outer = json.loads(result.stdout)
    except json.JSONDecodeError:
        if result.returncode != 0:
            _fail(result.stdout.strip().splitlines()[-1])
        return result.stdout
    if outer.get("is_error"):
        _fail(f"Claude Code: {outer.get('result') or 'it reported an error'}")
    return outer.get("result") or ""


def _fail(message: str):
    if SIGNED_OUT.search(message):
        raise ExplainError("Claude Code isn't signed in, or its sign-in has run out. Sign in to get explanations.",
                           signed_out=True)
    raise ExplainError(message)


# Signing in. One `claude auth login` at a time, kept running until it finishes, since it
# waits for the browser (or for the code the sign-in page shows) before it can save the sign-in.
_login: dict = {"process": None, "url": None, "output": []}
_login_lock = threading.Lock()
URL = re.compile(r"https://\S+")


def start_login() -> dict:
    """Starts `claude auth login`, which opens the sign-in page in the browser, and returns
    the page's address too, in case the browser didn't open."""
    if not _find_claude():
        raise ExplainError("Claude Code isn't installed on this computer.")
    with _login_lock:
        process = _login["process"]
        if process is None or process.poll() is not None:
            process = subprocess.Popen([_claude(), "auth", "login"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=_env(),
                                       cwd=tempfile.gettempdir())
            _login.update(process=process, url=None, output=[])
            threading.Thread(target=_read_login, args=(process,), daemon=True).start()
    deadline = time.monotonic() + 15
    while _login["url"] is None and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    return login_status()


def _read_login(process: subprocess.Popen):
    for line in process.stdout:
        _login["output"].append(line.strip())
        found = URL.search(line)
        if found and _login["url"] is None:
            _login["url"] = found.group(0)


def finish_login(code: str) -> dict:
    """Hands `claude auth login` the code from the sign-in page and waits for it to finish."""
    process = _login["process"]
    if process is None or process.poll() is not None:
        return login_status()
    try:
        process.stdin.write(code.strip() + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        pass
    return login_status()


def login_status() -> dict:
    process = _login["process"]
    if process is None:
        state = "idle"
    elif process.poll() is None:
        state = "waiting"
    elif process.returncode == 0:
        state = "signed_in"
    else:
        state = "failed"
    lines = [line for line in _login["output"] if line and not URL.search(line)]
    failure = next((line for line in reversed(lines) if "fail" in line.lower() or "error" in line.lower()), None)
    return {"state": state, "url": _login["url"], "detail": failure if state == "failed" else None}


def _parse(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ExplainError("Claude's answer wasn't in the expected shape. Try again.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ExplainError("Claude's answer wasn't in the expected shape. Try again.")


def _clean(kind: str, data: dict) -> dict:
    """Only the expected keys, each of the expected type, so the page can trust them."""
    result = {}
    for name, expected in FIELDS[kind].items():
        value = data.get(name)
        if expected is list:
            value = [item for item in value if isinstance(item, (str, dict))][:8] if isinstance(value, list) else []
        elif not isinstance(value, expected):
            value = None if type(None) in (expected if isinstance(expected, tuple) else (expected,)) else ""
        result[name] = value
    if kind == "project":
        result["parts"] = [p for p in result["parts"] if isinstance(p, dict) and isinstance(p.get("name"), str)
                           and isinstance(p.get("what"), str)]
        result["how_it_works"] = [s for s in result["how_it_works"] if isinstance(s, str)]
    else:
        result["improves"] = [s for s in result["improves"] if isinstance(s, str)]
        result["tradeoffs"] = [s for s in result["tradeoffs"] if isinstance(s, str)]
    return result

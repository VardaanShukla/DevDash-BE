"""Plain-English explanations of a changed function or of the whole project, written by
the user's own Claude Code (`claude -p`), so DevDash needs no API key of its own. Only
asked for when someone clicks Explain, and saved, so the same code is explained once.

A full knowledge transfer (KT) of a project is built a file at a time: each file is
explained on its own (its purpose and every function in it), so even a small local model
only ever reads one file, and the project's overview is then written from those notes.

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

from devdash import preferences, providers
from devdash.capture.record import INTERNAL_ENV
from devdash.store import connect
from devdash.store.db import now

TIMEOUT = 240
# Bump when a prompt changes, so explanations written for the old one are asked again.
PROMPT_VERSION = "1"
VERSIONS = {"project": "2"}
MAX_PROMPT_CHARS = 30_000
# Local and self-hosted models are often small, with a short memory (context): their
# prompts are kept to this many characters, about 3,500 tokens.
SMALL_PROMPT_CHARS = 12_000
SMALL_PROVIDERS = {"ollama", "hosted"}
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
        "provider": "claude",
        "name": providers.NAMES["claude"],
        "tool": "claude",
        "available": path is not None,
        "signed_in": signed_in,
        "account": (auth.get("email") or auth.get("orgName")) if signed_in else None,
        "model": MODEL,
        "detail": None if path else "Install Claude Code to get explanations.",
        "install": [] if path else install_commands(),
        "login": "button",
        "login_command": "claude auth login",
        "models": CLAUDE_MODELS,
        "default_model": MODEL,
    }


# Claude's model aliases, cheapest first: Claude Code always runs the newest of each.
CLAUDE_MODELS = [
    {"id": "haiku", "label": "Haiku", "note": "Fastest and cheapest: fewest tokens. Plenty for explanations."},
    {"id": "sonnet", "label": "Sonnet", "note": "Deeper explanations of tricky code; uses several times more."},
    {"id": "opus", "label": "Opus", "note": "The most capable; uses the most tokens."},
]


def provider_status(provider: str) -> dict:
    """An AI's state for the dashboard, with the model it will use: the one picked in
    Settings, or its default."""
    found = status() if provider == "claude" else providers.status(provider)
    chosen = preferences.get()["ai"]["models"].get(provider)
    return {**found, "model": chosen or found.get("default_model") or ""}


def active_status() -> dict:
    """The AI picked in Settings to write explanations."""
    return provider_status(preferences.ai_choice()[0])


def all_status() -> list[dict]:
    return [provider_status(provider) for provider in providers.IDS]


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
    if platform.system() == "Windows":
        # The native installer's copy, or npm's (a .cmd that runs it through node).
        places = [Path.home() / ".local" / "bin" / "claude.exe",
                  Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "npm" / "claude.cmd"]
    else:
        places = [Path.home() / ".local" / "bin" / "claude", Path.home() / ".npm-global" / "bin" / "claude"]
    return next((str(p) for p in places if p.is_file() and os.access(p, os.X_OK)), None)


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
    text = json.dumps({"kind": kind, "version": VERSIONS.get(kind, PROMPT_VERSION), **subject}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def cached(kind: str, subject: dict) -> dict | None:
    conn = connect()
    row = conn.execute("SELECT result FROM explanations WHERE key = ?", (key(kind, subject),)).fetchone()
    conn.close()
    result = json.loads(row["result"]) if row else None
    # Older versions kept answers with the description missing; those are asked again.
    return result if result and _complete(kind, result) else None


# Secrets that may sit in what an explanation is built from (a readme, commit messages, the
# opening lines of code, a function): blanked out before anything is sent to an AI.
REDACTED = "[redacted]"
_SECRET_TOKENS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"                      # AWS access key ids
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
    r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"             # Anthropic and OpenAI keys
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}"                    # Slack
    r"|\bAIza[0-9A-Za-z_-]{35}"                            # Google
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")  # JSON web tokens
# The password in a connection string: scheme://user:password@host.
_URL_PASSWORD = re.compile(r"(\b[a-z][\w+.-]*://[^\s:/@]+:)([^\s@/]+)(@)", re.I)
_SECRET_NAME = r"[\w.-]*(?:api[_-]?key|secret|passw(?:or)?d|pwd|token|access[_-]?key|private[_-]?key|credentials?)[\w-]*"
# A quoted value given to something named like a secret: api_key = "…", "password": "…".
_QUOTED = re.compile(r"(\b" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)([\"'])([^\"'\s]{4,})\2", re.I)
# An .env-style line: API_KEY=… with no quotes.
_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|SECRET|PASSWORD|PASSWD|TOKEN|PWD)[A-Z0-9_]*\s*=\s*)(\S+)",
                       re.M)


def redact(text: str) -> str:
    text = _SECRET_TOKENS.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
    text = _QUOTED.sub(lambda m: m.group(1) + m.group(2) + REDACTED + m.group(2), text)
    return _ENV_LINE.sub(lambda m: m.group(1) + REDACTED, text)


def explain(kind: str, subject: dict, context: dict | None = None, fresh: bool = False) -> dict:
    """The explanation for `subject` (what is explained, and the cache key), with
    `context` adding background that doesn't change the answer much, like commit titles.
    `fresh` asks again even when an answer is saved, as when more context has come in."""
    found = None if fresh else cached(kind, subject)
    if found:
        return found
    budget = prompt_budget()
    prompt = _fit(redact(PROMPTS[kind](subject, context or {}, budget)), budget)
    result = _clean(kind, _parse(_run(prompt)))
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO explanations (key, kind, result, created_at) VALUES (?, ?, ?, ?)",
                     (key(kind, subject), kind, json.dumps(result), now()))
    conn.close()
    return result


def prompt_budget() -> int:
    """How long a prompt the AI picked in Settings can take."""
    return SMALL_PROMPT_CHARS if preferences.ai_choice()[0] in SMALL_PROVIDERS else MAX_PROMPT_CHARS


def _fit(prompt: str, budget: int) -> str:
    """The prompt within budget. The middle goes, never the end: that is where the
    instructions for the answer are."""
    if len(prompt) <= budget:
        return prompt
    tail = min(len(prompt) // 3, budget // 3)
    return prompt[:budget - tail - 40] + "\n… (cut to fit) …\n" + prompt[-tail:]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + f"\n… ({len(text) - limit:,} more characters not shown)"


# What each kind of unit is, for the prompt.
UNIT_WORDS = {"style": "Style rule", "section": "Section", "file": "Whole file"}


def _function_prompt(subject: dict, context: dict, budget: int = MAX_PROMPT_CHARS) -> str:
    if subject.get("scope") == "prompt":
        # One prompt's change, from the AI changes page, rather than a whole branch's.
        what = {"added": "One prompt to an AI coding tool added it.", "deleted": "One prompt to an AI coding tool removed it.",
                "modified": "One prompt to an AI coding tool changed it."}[subject["change"]]
    else:
        what = {"added": "It is new on this branch.", "deleted": "This branch removes it.",
                "modified": "This branch changes it."}[subject["change"]]
    kind = subject["kind"]
    parts = [
        "You explain code changes to people who don't write code: product managers, designers and testers.",
        "",
        f"File: {subject['path']}",
        f"{UNIT_WORDS.get(kind, kind.capitalize())}: {subject['name']}. {what}",
    ]
    if kind == "file":
        parts.append("It is not split into functions (settings, styles, a page, data, docs or a script): "
                     "explain what the file is for and what its change does.")
    if context.get("prompt"):
        parts += ["What the AI was asked:", _clip(context["prompt"], 1_500)]
    if context.get("commits"):
        parts += ["Commit titles on this branch:", *[f"- {c}" for c in context["commits"][:12]]]
    if context.get("used_by"):
        parts += ["Used in:", *[f"- {u['path']}:{u['line']}: {u['text']}" for u in context["used_by"][:6]]]
    # Room for the code: what's left after the rest of the prompt (about 2,000 characters).
    room = max(2_000, budget - 2_500)
    if subject.get("diff"):
        parts += ["", "The lines that changed (- removed, + added):", "```", _clip(subject["diff"], room), "```"]
    else:
        sides = [side for side in ("before", "after") if subject.get(side)]
        for side in sides:
            title = ("Before:" if side == "before" else "After:") if len(sides) == 2 else "The code:"
            parts += ["", title, "```", _clip(subject[side], room // len(sides)), "```"]
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


def _project_prompt(subject: dict, context: dict, budget: int = MAX_PROMPT_CHARS) -> str:
    # What each file is for, from the file-by-file notes written so far: as many as fit,
    # one line each, so a small model still gets the whole picture.
    notes = context.get("file_notes") or {}
    context = {k: v for k, v in context.items() if k != "file_notes"}
    facts = json.dumps({**subject, **context}, indent=1)
    lines, room = [], max(1_500, budget - len(facts) - 3_000)
    for path, purpose in notes.items():
        line = f"- {path}: {purpose}"
        if room - len(line) < 0:
            lines.append(f"- … and {len(notes) - len(lines)} more files")
            break
        lines.append(line)
        room -= len(line) + 1
    return "\n".join([
        "You write the knowledge transfer (KT) for a software project: what a new team member needs to take it "
        "over, in words product managers and designers follow too.",
        "Here are facts about one project, read from its files and git history:",
        "```json",
        facts,
        "```",
        *(["", "What each file is for:", *lines] if lines else []),
        "",
        "Reply with only a JSON object and no other text, with these keys:",
        '"overview": three to five sentences on what the project is, what problem it solves and who uses it.',
        '"how_it_works": a list of four to eight short sentences that follow what happens, from what a person '
        "does to what the project does in response, naming the parts and files involved.",
        '"parts": a list of objects {"name": a part name exactly as given, "what": one or two sentences on its '
        "job and its most important files}, for every part given, at most twelve.",
        '"flows": a list of up to four objects {"name": a main thing the project does, "steps": a list of short '
        "sentences, in order, each naming the file that does that step}.",
        '"start_here": a list of up to six objects {"path": a file path exactly as given, "why": one sentence}, '
        "the files a new team member should read first, in order.",
        '"state": one or two sentences on how far along and how active it looks, from the history.',
        "Use plain words. When a technical term can't be avoided, explain it in brackets. Say only what the "
        "facts support; write \"unclear\" rather than guess.",
    ])


def _file_prompt(subject: dict, context: dict, budget: int = MAX_PROMPT_CHARS) -> str:
    """One file of the project's KT: what it is for, and each of its functions."""
    outline = context.get("outline") or []
    listed = []
    for unit in outline[:MAX_OUTLINE]:
        doc = f" — {unit['doc'].splitlines()[0][:160]}" if unit.get("doc") else ""
        listed.append(f"- {unit['name']} ({unit['kind']}, line {unit['start']}): {unit['signature'][:160]}{doc}")
    if len(outline) > MAX_OUTLINE:
        listed.append(f"- … and {len(outline) - MAX_OUTLINE} more")
    room = max(2_000, budget - sum(len(l) + 1 for l in listed) - 2_500)
    project = context.get("project") or {}
    parts = [
        "You write the knowledge transfer (KT) for a software project, one file at a time, so a new team "
        "member can take it over.",
        "",
        f"Project: {project.get('name') or 'unnamed'}" + (f". {project['description']}" if project.get("description") else ""),
        f"File: {subject['path']}" + (f" (in the part \"{context['part']}\")" if context.get("part") else ""),
    ]
    if listed:
        parts += ["Its functions, classes and types, as read from the code:", *listed]
    parts += ["", "The file" + (" (cut to fit; the list above has all of it):" if len(context.get("text", "")) > room else ":"),
              "```", _clip(context.get("text", ""), room), "```", "",
              "Reply with only a JSON object and no other text, with these keys:",
              '"purpose": two or three sentences on what this file is for, why the project needs it, and when it '
              "is used.",
              '"functions": a list of objects {"name": a name exactly as listed above, "what": one sentence on what '
              "it does, what it takes and what it gives back}, for every one listed, in order. An empty list when "
              "none are listed.",
              '"connects": one or two sentences on which other files or parts it uses, or that use it, from its '
              "imports and the names in it. \"unclear\" if the file doesn't show.",
              '"notes": up to three short phrases on what to watch out for when changing it, such as settings it '
              "reads, data it writes, or things that must stay in step elsewhere. An empty list if none.",
              "Use plain words. When a technical term can't be avoided, explain it in brackets. Say only what the "
              "code shows; write \"unclear\" rather than guess."]
    return "\n".join(parts)


MAX_OUTLINE = 60


# The field an explanation can't be without.
MAIN_FIELD = {"function": "summary", "project": "overview", "file": "purpose"}
PROMPTS = {"function": _function_prompt, "project": _project_prompt, "file": _file_prompt}

FIELDS = {
    "function": {"summary": str, "change": str, "improves": list, "tradeoffs": list, "check": (str, type(None))},
    "project": {"overview": str, "how_it_works": list, "parts": list, "flows": list, "start_here": list,
                "state": str},
    "file": {"purpose": str, "functions": list, "connects": str, "notes": list},
}
# The most items kept in each list of an answer.
LIST_LIMITS = {"functions": MAX_OUTLINE, "parts": 12, "how_it_works": 8}


def _run(prompt: str) -> str:
    """The answer from the AI picked in Settings, with the model picked for it."""
    provider, model = preferences.ai_choice()
    if provider == "claude":
        return _ask_claude(model or MODEL, prompt)
    try:
        return providers.ask(provider, model, SYSTEM_PROMPT, prompt)
    except providers.ProviderError as error:
        raise ExplainError(f"{error} (Settings → AI picks another.)" if not error.signed_out else str(error),
                           signed_out=error.signed_out)


def _ask_claude(model: str, prompt: str) -> str:
    if not _find_claude():
        raise ExplainError("Claude Code isn't installed on this computer, so DevDash can't write explanations.")
    flags = ["--output-format", "json", "--model", model, "--system-prompt", SYSTEM_PROMPT,
             "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
    # `--tools ""` leaves Claude none of its own tools (no reading files, no commands), and
    # --strict-mcp-config none from MCP servers either, so text in a project can't make it do
    # anything; it only answers. Never run without them.
    with tempfile.TemporaryDirectory(prefix="devdash-explain-") as folder:
        try:
            result = subprocess.run([_claude(), "-p", *flags], input=prompt, cwd=folder, env=_env(),
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ExplainError("Claude took too long to answer. Try again.")
    if result.returncode != 0 and re.search(r"unknown option|unrecognized|unexpected argument", result.stderr, re.I):
        raise ExplainError("This Claude Code is too old for DevDash's explanations. Update Claude Code "
                           "(run `claude update`), then try again.")
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


def start_login(provider: str = "claude") -> dict:
    """Starts the AI's own sign-in (`claude auth login`, `codex login`), which opens its
    sign-in page in the browser, and returns the page's address too, in case the browser
    didn't open. AIs that sign in only from a terminal (Antigravity) say so instead."""
    if provider == "claude":
        if not _find_claude():
            raise ExplainError("Claude Code isn't installed on this computer.")
        command = [_claude(), "auth", "login"]
    else:
        command = providers.login_command(provider)
        if command is None:
            raise ExplainError(f"{providers.NAMES.get(provider, provider)} signs in from a terminal, or isn't installed yet.")
    with _login_lock:
        process = _login["process"]
        if process is not None and process.poll() is None and _login.get("provider") != provider:
            process.terminate()  # signing in to another AI instead
        if process is None or process.poll() is not None or _login.get("provider") != provider:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=_env(),
                                       cwd=tempfile.gettempdir())
            _login.update(process=process, url=None, output=[], provider=provider)
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
        if expected is str and isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = " ".join(value)  # sentences given as a list
        if expected is list:
            limit = LIST_LIMITS.get(name, 8)
            value = [item for item in value if isinstance(item, (str, dict))][:limit] if isinstance(value, list) else []
        elif not isinstance(value, expected):
            value = None if type(None) in (expected if isinstance(expected, tuple) else (expected,)) else ""
        result[name] = value
    if kind == "project":
        result["parts"] = _pairs(result["parts"], "name", "what")
        result["how_it_works"] = [s for s in result["how_it_works"] if isinstance(s, str)]
        result["start_here"] = _pairs(result["start_here"], "path", "why")[:6]
        result["flows"] = [{"name": f["name"], "steps": [s for s in f["steps"] if isinstance(s, str)][:10]}
                           for f in result["flows"][:4] if isinstance(f, dict) and isinstance(f.get("name"), str)
                           and isinstance(f.get("steps"), list)]
    elif kind == "file":
        result["functions"] = _pairs(result["functions"], "name", "what")
        result["notes"] = [s for s in result["notes"] if isinstance(s, str)][:3]
    else:
        result["improves"] = [s for s in result["improves"] if isinstance(s, str)]
        result["tradeoffs"] = [s for s in result["tradeoffs"] if isinstance(s, str)]
    if not _complete(kind, result):
        # Never kept: the page would show an empty description, every time.
        raise ExplainError("The AI's answer left out the description. Try again, or pick another model in Settings.")
    return result


def _pairs(items: list, first: str, second: str) -> list[dict]:
    """The items that are objects with both text fields, and only those fields."""
    return [{first: item[first], second: item[second]} for item in items
            if isinstance(item, dict) and isinstance(item.get(first), str) and isinstance(item.get(second), str)]


def _complete(kind: str, result: dict) -> bool:
    main = result.get(MAIN_FIELD[kind])
    return isinstance(main, str) and bool(main.strip())


# Questions about the project, from the Ask screen. The answer is built from what DevDash
# already knows (the project's description, the KT's file-by-file notes) plus the code of the
# few files the question is about, and never saved: each question is asked as it comes.
# It only explains: asked to write or change code, it says so and explains in words.
MAX_QUESTION = 2_000
MAX_HISTORY = 6
MAX_READ = 3
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}")
# Words in nearly every question, which would match every file.
_COMMON = {"the", "and", "for", "does", "what", "how", "why", "where", "which", "who", "this", "that", "with",
           "file", "files", "function", "functions", "project", "code", "work", "works", "used", "use", "into",
           "from", "about", "explain", "there", "when", "are", "is", "can", "you", "our", "its", "it's", "part",
           "parts", "app", "make", "makes", "doing", "do", "does", "have", "has", "between", "fit", "together", "write",
           "writes", "change", "changes", "add", "adds", "new", "should", "would", "could", "please"}


def pick_files(text: str, files: list[dict], limit: int = MAX_READ) -> list[str]:
    """The files a question is most likely about, best first: named by path or file name,
    then by a function in them, then by words in their KT note. None when nothing matches,
    for a question about the whole project."""
    words = {w.lower().strip(".-/") for w in _WORD.findall(text)} - _COMMON
    words.discard("")
    if not words:
        return []
    scored = []
    for file in files:
        path = file["path"].lower()
        name = path.rsplit("/", 1)[-1]
        stem = name.split(".", 1)[0]
        note = file.get("explanation") or {}
        functions = {f["name"].lower().rsplit(".", 1)[-1] for f in note.get("functions", [])}
        score = 0
        for word in words:
            if word == path or word == name:
                score += 10
            elif word == stem or ("/" in word and path.endswith(word)):
                score += 6
            elif word in functions:
                score += 4
            elif len(word) > 3 and word in path:
                score += 2
            elif len(word) > 5 and word in (note.get("purpose") or "").lower():
                score += 1
        if score:
            scored.append((score, file["path"]))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in scored[:limit]]


def answer(question: str, history: list[dict], context: dict) -> dict:
    """The AI's answer to a question about the project. `context` holds the project's
    facts and description, every KT file with its note, and the text of the files picked
    for the question."""
    budget = prompt_budget()
    prompt = _fit(redact(_ask_prompt(question, history, context, budget)), budget)
    text = _run(prompt)
    known = {f["path"] for f in context.get("files", [])}
    try:
        data = _parse(text)
    except ExplainError:
        # A small model that answered in plain words: the words are the answer.
        data = {"answer": text}
    reply = data.get("answer")
    if isinstance(reply, list) and all(isinstance(r, str) for r in reply):
        reply = "\n\n".join(reply)
    if not isinstance(reply, str) or not reply.strip():
        raise ExplainError("The AI didn't answer. Ask again, or pick another model in Settings.")
    cited = [p for p in data.get("files", []) if isinstance(p, str) and p in known] if isinstance(data.get("files"), list) else []
    return {"answer": reply.strip(), "files": list(dict.fromkeys(cited))[:8]}


def _ask_prompt(question: str, history: list[dict], context: dict, budget: int = MAX_PROMPT_CHARS) -> str:
    project = context.get("project") or {}
    about = context.get("explanation") or {}
    parts = [
        "You answer questions about one software project for someone learning it: a new team member, a "
        "product manager or a tester. You explain how the project works. You never write, fix or change code, "
        "and you don't give code to copy. If you are asked to write or change code, say in one sentence that "
        "you only explain the project, then explain in words which files and functions such a change would touch.",
        "",
        f"Project: {project.get('name') or 'unnamed'}" + (f". {project['description']}" if project.get("description") else ""),
    ]
    if about.get("overview"):
        parts += ["What it is:", about["overview"]]
        if about.get("how_it_works"):
            parts += ["How it works:", *[f"- {s}" for s in about["how_it_works"]]]
        if about.get("parts"):
            parts += ["Its parts:", *[f"- {p['name']}: {p['what']}" for p in about["parts"]]]
    elif project.get("tech"):
        parts += ["Built with:", *[f"- {t}" for t in project["tech"][:12]]]

    reading = context.get("reading") or []
    # Room for the code of the files the question is about; the file list gets what's left.
    code_room = max(1_500, (budget - 4_000) // 2)
    files = context.get("files") or []
    listed, room = [], max(1_000, budget - code_room - 4_000 - sum(len(p) for p in parts))
    for file in files:
        note = file.get("explanation")
        line = f"- {file['path']}" + (f": {note['purpose']}" if note else "")
        if room - len(line) < 0:
            listed.append(f"- … and {len(files) - len(listed)} more files")
            break
        listed.append(line)
        room -= len(line) + 1
    if listed:
        parts += ["", "The project's files" + (", with what each is for where it's known:" if any(f.get("explanation") for f in files) else ":"),
                  *listed]

    for file in reading:
        note = file.get("explanation") or {}
        parts += ["", f"The file {file['path']}:"]
        if note.get("functions"):
            parts += ["Its functions:", *[f"- {f['name']}: {f['what']}" for f in note["functions"][:30]]]
        parts += ["```", _clip(file.get("text", ""), code_room // max(1, len(reading))), "```"]

    turns = [t for t in history if t.get("role") in ("user", "assistant") and isinstance(t.get("text"), str)][-MAX_HISTORY:]
    if turns:
        parts += ["", "The conversation so far:"]
        parts += [f"{'Them' if t['role'] == 'user' else 'You'}: {_clip(t['text'], 800)}" for t in turns]
    parts += [
        "", "Their question:", _clip(question, MAX_QUESTION), "",
        "Reply with only a JSON object and no other text, with these keys:",
        '"answer": your answer in plain words, a few short paragraphs at most. Name files by their path and '
        "functions by their name in `backticks`. Lines starting with \"- \" make a list.",
        '"files": a list of the file paths, exactly as given above, that your answer is about. An empty list if none.',
        "Say only what the facts and code above show. When they don't show the answer, say so, and name the "
        "files that are most likely to have it.",
    ]
    return "\n".join(parts)

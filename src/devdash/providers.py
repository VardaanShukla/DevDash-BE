"""The AIs besides Claude Code (explain.py) that can write DevDash's explanations: ChatGPT's
Codex CLI, a local model served by Ollama, and Google's Antigravity CLI. Each says whether
it is installed and ready, how to install it (the official commands for this computer),
which models it offers and which is cheapest, and answers one prompt with text. Every run
is in an empty folder, read-only where the tool allows it, so nothing touches a project.

Recording changes is separate (capture/): only Claude Code's edits are recorded today."""

import glob
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from devdash.capture.record import INTERNAL_ENV

IDS = ("claude", "codex", "ollama", "antigravity")
NAMES = {"claude": "Claude", "codex": "ChatGPT Codex", "ollama": "Local model (Ollama)", "antigravity": "Antigravity"}
TIMEOUT = 240
# A small model that runs on most laptops, offered when Ollama has none yet.
SMALL_LOCAL_MODEL = "qwen2.5:3b"
# Codex models when its own list (~/.codex/models_cache.json) isn't there yet, cheapest first.
CODEX_FALLBACK = [
    {"id": "gpt-6-luna", "label": "GPT-6 Luna", "note": "Most efficient: fewest tokens, good for explanations."},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "note": "Near the top model at a lower cost."},
    {"id": "gpt-6-astra", "label": "GPT-6 Astra", "note": "Most capable, uses the most."},
]
# Words in a model's name that mark the small, cheap ones.
CHEAP = re.compile(r"mini|nano|luna|flash|lite|small", re.IGNORECASE)


class ProviderError(RuntimeError):
    def __init__(self, message: str, signed_out: bool = False):
        super().__init__(message)
        self.signed_out = signed_out


def status(provider: str) -> dict:
    return {"codex": _codex_status, "ollama": _ollama_status, "antigravity": _agy_status}[provider]()


def ask(provider: str, model: str | None, system: str, prompt: str) -> str:
    """One answer from the provider, as text. With no model picked, the cheapest one it
    offers is used (tokens matter); Antigravity, which can't list them, uses its own choice."""
    model = model or default_model(provider)
    return {"codex": _codex_ask, "ollama": _ollama_ask, "antigravity": _agy_ask}[provider](model or None, system, prompt)


def default_model(provider: str) -> str | None:
    if provider == "codex":
        return _cheapest(_codex_models())
    return None  # Ollama picks its smallest model when asked; Antigravity uses its own setting


def login_command(provider: str) -> list[str] | None:
    """The command that signs in from the dashboard (it opens the browser), when there is one."""
    if provider == "codex":
        path = _find_codex()
        return [path, "login"] if path else None
    return None


def _base(provider: str, available: bool, **fields) -> dict:
    return {"provider": provider, "name": NAMES[provider], "tool": provider, "available": available,
            "signed_in": None, "account": None, "detail": None, "install": [], "login": None,
            "login_command": None, "models": [], "default_model": None, "model": "", **fields}


def _env() -> dict:
    # DevDash's own runs are never recorded as someone's prompt.
    return {**os.environ, INTERNAL_ENV: "1"}


def _windows() -> bool:
    return platform.system() == "Windows"


def _first(*candidates) -> str | None:
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# ChatGPT Codex -------------------------------------------------------------------------

def _find_codex() -> str | None:
    """Codex on the PATH, where its installer puts it, or the copy bundled with the ChatGPT
    extension for VS Code (or Cursor), which many people already have."""
    found = shutil.which("codex")
    if found:
        return found
    exe = "codex.exe" if _windows() else "codex"
    home = Path.home()
    bundled = []
    for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
        bundled += glob.glob(str(home / editor / "extensions" / "openai.chatgpt-*" / "bin" / "*" / exe))
    bundled.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return _first(home / ".local" / "bin" / exe, home / ".codex" / "bin" / exe, *bundled)


def _codex_status() -> dict:
    path = _find_codex()
    if not path:
        install = ([{"shell": "PowerShell", "command": 'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"'}]
                   if _windows() else [{"shell": "Terminal", "command": "curl -fsSL https://chatgpt.com/codex/install.sh | sh"}])
        install.append({"shell": "Or with npm", "command": "npm install -g @openai/codex"})
        return _base("codex", False, detail="Install the Codex CLI", install=install, models=_codex_models(),
                     default_model=_cheapest(_codex_models()))
    signed_in, account = None, None
    try:
        result = subprocess.run([path, "login", "status"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20, env=_env())
        signed_in = result.returncode == 0
        said = (result.stdout + result.stderr).strip()
        account = ("ChatGPT account" if "chatgpt" in said.lower() else "API key" if "api key" in said.lower() else None) \
            if signed_in else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    models = _codex_models()
    return _base("codex", True, signed_in=signed_in, account=account, login="button", login_command="codex login",
                 models=models, default_model=_cheapest(models))


def _codex_models() -> list[dict]:
    """The models Codex lists for this account, from its own cache, cheapest first."""
    try:
        cache = json.loads((Path.home() / ".codex" / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CODEX_FALLBACK
    models = []
    for item in sorted(cache.get("models", []), key=lambda m: m.get("priority", 99)):
        if not isinstance(item, dict) or not item.get("slug") or item.get("visibility") == "hide":
            continue
        note = (item.get("description") or "").split(". ")[0].strip()
        models.append({"id": item["slug"], "label": item.get("display_name") or item["slug"], "note": note})
    models.sort(key=lambda m: not CHEAP.search(m["id"]))
    return models or CODEX_FALLBACK


def _codex_ask(model: str | None, system: str, prompt: str) -> str:
    path = _find_codex()
    if not path:
        raise ProviderError("The Codex CLI isn't installed on this computer.")
    with tempfile.TemporaryDirectory(prefix="devdash-explain-") as folder:
        answer = Path(folder) / "answer.txt"
        command = [path, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--color", "never",
                   "--output-last-message", str(answer)]
        if model:
            command += ["--model", model]
        # "-" reads the prompt from stdin, which has no length limit (Windows command lines do).
        try:
            result = subprocess.run([*command, "-"], input=f"{system}\n\n{prompt}", cwd=folder, env=_env(),
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ProviderError("Codex took too long to answer. Try again.")
        text = answer.read_text(encoding="utf-8", errors="replace") if answer.is_file() else ""
    if result.returncode != 0 or not text.strip():
        said = [line for line in (result.stderr + result.stdout).splitlines() if line.strip()]
        if _codex_status().get("signed_in") is False or re.search(r"log ?in|unauthori[sz]ed|401", " ".join(said[-3:]), re.I):
            raise ProviderError("Codex isn't signed in. Sign in with your ChatGPT account to get explanations.",
                                signed_out=True)
        raise ProviderError(f"Codex: {said[-1] if said else 'it gave no answer'}")
    return text


# Local models (Ollama) ---------------------------------------------------------------

def _ollama_url(path: str) -> str:
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip().rstrip("/")
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0"):]
    return (host if "://" in host else f"http://{host}") + path


def _ollama_get(path: str, timeout: float = 3):
    with urllib.request.urlopen(_ollama_url(path), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _ollama_status() -> dict:
    try:
        tags = _ollama_get("/api/tags")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        if shutil.which("ollama") or _first(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"):
            return _base("ollama", False, detail="Start Ollama",
                         install=[{"shell": "Terminal", "command": "ollama serve"}],
                         login="none", login_command="Or open the Ollama app.")
        install = ([{"shell": "Download", "command": "https://ollama.com/download/windows"}] if _windows() else
                   [{"shell": "Download", "command": "https://ollama.com/download/mac"}] if platform.system() == "Darwin" else
                   [{"shell": "Terminal", "command": "curl -fsSL https://ollama.com/install.sh | sh"}])
        return _base("ollama", False, detail="Install Ollama", install=install, login="none")
    models = sorted(
        ({"id": m["name"], "label": m["name"], "size": m.get("size", 0),
          "note": " · ".join(x for x in (_size(m.get("size")), (m.get("details") or {}).get("parameter_size")) if x)}
         for m in tags.get("models", []) if isinstance(m, dict) and m.get("name")),
        key=lambda m: m["size"])
    for m in models:
        m.pop("size")
    if not models:
        return _base("ollama", False, detail="Download a model",
                     install=[{"shell": "Terminal", "command": f"ollama pull {SMALL_LOCAL_MODEL}"}], login="none")
    # Local models need no account, and cost no tokens.
    return _base("ollama", True, signed_in=True, account="On this computer", login="none", models=models,
                 default_model=models[0]["id"])


def _size(count) -> str | None:
    return f"{count / 1e9:.1f} GB" if isinstance(count, (int, float)) and count else None


def _ollama_ask(model: str | None, system: str, prompt: str) -> str:
    if not model:
        ready = _ollama_status()
        if not ready["available"]:
            raise ProviderError(f"The local model isn't ready: {ready['detail']}.")
        model = ready["default_model"]
    body = json.dumps({"model": model, "stream": False, "format": "json", "options": {"temperature": 0.2},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
    request = urllib.request.Request(_ollama_url("/api/chat"), data=body.encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise ProviderError(f"Ollama: {json.loads(detail).get('error', detail) if detail.startswith('{') else detail or error}")
    except (OSError, urllib.error.URLError) as error:
        raise ProviderError(f"Couldn't reach Ollama ({getattr(error, 'reason', error)}). Is it running?")
    return (reply.get("message") or {}).get("content") or ""


# Antigravity -----------------------------------------------------------------------

def _find_agy() -> str | None:
    found = shutil.which("agy")
    if found:
        return found
    if _windows():
        return _first(Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe")
    return _first(Path.home() / ".local" / "bin" / "agy")


def _agy_status() -> dict:
    path = _find_agy()
    if not path:
        install = ([{"shell": "PowerShell", "command": "irm https://antigravity.google/cli/install.ps1 | iex"},
                    {"shell": "Command Prompt",
                     "command": "curl -fsSL https://antigravity.google/cli/install.cmd -o install.cmd && install.cmd && del install.cmd"}]
                   if _windows() else [{"shell": "Terminal", "command": "curl -fsSL https://antigravity.google/cli/install.sh | bash"}])
        return _base("antigravity", False, detail="Install the Antigravity CLI", install=install, login="terminal",
                     login_command="agy")
    signed_in, account = None, None
    try:
        result = subprocess.run([path, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20, env=_env())
        said = result.stdout + result.stderr
        if result.returncode == 0:
            signed_in = True
            email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", said)
            account = email.group(0) if email else None
        elif re.search(r"not (signed|logged) in|unauthenticated|no account", said, re.I):
            signed_in = False
        # Anything else: this version can't say; the first explanation finds out.
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Antigravity has no command that lists its models: its own choice (/model) is used
    # unless one is typed here.
    return _base("antigravity", True, signed_in=signed_in, account=account, login="terminal", login_command="agy",
                 models=[], default_model="")


def _agy_ask(model: str | None, system: str, prompt: str) -> str:
    path = _find_agy()
    if not path:
        raise ProviderError("The Antigravity CLI isn't installed on this computer.")
    command = [path, "-p", f"{system}\n\n{prompt}", "--output-format", "text"]
    if model:
        command += ["--model", model]
    with tempfile.TemporaryDirectory(prefix="devdash-explain-") as folder:
        try:
            result = subprocess.run(command, cwd=folder, env=_env(), capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ProviderError("Antigravity took too long to answer. Try again.")
    if result.returncode != 0 or not result.stdout.strip():
        said = [line for line in (result.stderr + result.stdout).splitlines() if line.strip()]
        last = said[-1] if said else "it gave no answer"
        if re.search(r"auth|sign ?in|log ?in|credential", last, re.I):
            raise ProviderError("Antigravity isn't signed in. Run agy in a terminal once and sign in with Google.",
                                signed_out=True)
        raise ProviderError(f"Antigravity: {last}")
    return result.stdout


def _cheapest(models: list[dict]) -> str | None:
    cheap = [m for m in models if CHEAP.search(m["id"])]
    return (cheap or models or [{"id": None}])[0]["id"]

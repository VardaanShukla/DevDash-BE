"""Starting DevDash when you sign in to your computer, so the dashboard is just there.
`devdash serve` serves both the API and the page, so it's the one thing started.

Each system's own per-user mechanism, so nothing needs admin rights and nothing runs
before you sign in or for any other account:
- Linux: a systemd user service, or an XDG autostart entry where there's no systemd.
- macOS: a LaunchAgent in ~/Library/LaunchAgents.
- Windows: a value under HKCU\\...\\CurrentVersion\\Run, started with pythonw (no window).

The command names this install's Python by its absolute path with -I, as the hooks do,
so a folder called devdash wherever it starts can't stand in for the package. The server
only listens on 127.0.0.1. Turning it on needs a dashboard password, since a server that
starts by itself would otherwise be open to anything on this computer."""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from devdash import auth

NAME = "devdash"
LABEL = "com.devdash.serve"  # the macOS LaunchAgent's label
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_PORT = 8765


class AutostartError(RuntimeError):
    pass


def status() -> dict:
    """Whether DevDash starts at sign-in, how, and whether it would start this install."""
    method = _method()
    entry = _read(method)
    return {
        "supported": method is not None,
        "enabled": entry is not None,
        "method": _METHOD_NAMES.get(method, None),
        # Off when it names another Python, say after a reinstall: turning it on again fixes it.
        "current": entry is not None and entry == _command_text(_port_in(entry)),
        "port": _port_in(entry) if entry else None,
        "password_set": auth.password_set(),
    }


def enable(port: int = DEFAULT_PORT) -> dict:
    method = _method()
    if method is None:
        raise AutostartError("Starting at sign-in isn't supported on this system.")
    if not auth.password_set():
        raise AutostartError("Set a dashboard password first (Settings → Account), so the dashboard that starts "
                             "by itself is locked.")
    if not 1024 <= port <= 65535:
        raise AutostartError("The port must be between 1024 and 65535.")
    _WRITERS[method](port)
    return status()


def disable() -> dict:
    method = _method()
    if method is not None:
        _REMOVERS[method]()
    return status()


def command(port: int) -> list[str]:
    python = Path(sys.executable)
    if os.name == "nt":
        # pythonw runs without a console window; fall back to python where there's none.
        windowless = python.with_name("pythonw.exe")
        python = windowless if windowless.exists() else python
    return [str(python), "-I", "-m", "devdash", "serve", "--port", str(port)]


def _command_text(port: int | None) -> str:
    args = command(port or DEFAULT_PORT)
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _port_in(entry: str | None) -> int | None:
    parts = (entry or "").replace('"', " ").split()
    try:
        return int(parts[parts.index("--port") + 1])
    except (ValueError, IndexError):
        return None


# How each system starts it, picked once per call.


def _method() -> str | None:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd" if _has_systemd() else "xdg"
    return None


_METHOD_NAMES = {"windows": "Windows sign-in (Run key)", "launchd": "macOS LaunchAgent",
                 "systemd": "systemd user service", "xdg": "desktop autostart entry"}


def _has_systemd() -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True)
    return result.returncode == 0


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _systemd_unit() -> Path:
    return _config_home() / "systemd" / "user" / f"{NAME}.service"


def _xdg_entry() -> Path:
    return _config_home() / "autostart" / f"{NAME}.desktop"


def _launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _private_write(path: Path, data: bytes) -> None:
    """Written by and readable only by you, replacing any file there in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(temporary, path)


def _write_systemd(port: int) -> None:
    exec_line = " ".join(_systemd_quote(arg) for arg in command(port))
    _private_write(_systemd_unit(), (
        "[Unit]\n"
        "Description=DevDash dashboard\n"
        "After=default.target\n"
        # If the port is taken (DevDash already running by hand), stop retrying after a while.
        "StartLimitIntervalSec=120\n"
        "StartLimitBurst=5\n\n"
        "[Service]\n"
        f"ExecStart={exec_line}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        # Its own files only: no new privileges, nothing written outside your home.
        "NoNewPrivileges=yes\n"
        "PrivateTmp=yes\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode())
    _systemctl("daemon-reload")
    # From the next sign-in, like the other systems: starting it now would clash with the
    # DevDash that's running this page.
    _systemctl("enable", f"{NAME}.service")


def _systemd_quote(arg: str) -> str:
    # systemd splits ExecStart on spaces and reads quotes, backslashes and % itself.
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _remove_systemd() -> None:
    if _systemd_unit().exists():
        _systemctl("disable", "--now", f"{NAME}.service", check=False)
        _systemd_unit().unlink()
        _systemctl("daemon-reload", check=False)


def _systemctl(*args: str, check: bool = True) -> None:
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AutostartError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip() or 'no reason given'}")


def _write_xdg(port: int) -> None:
    # Exec takes the same quoting as a shell word list, with % doubled.
    exec_line = " ".join(f'"{a}"' if any(c in a for c in ' "\\`$') else a for a in command(port)).replace("%", "%%")
    _private_write(_xdg_entry(), (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=DevDash\n"
        "Comment=Starts the DevDash dashboard on 127.0.0.1\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    ).encode())


def _write_launchd(port: int) -> None:
    log = Path.home() / ".devdash" / "serve.log"
    _private_write(_launch_agent(), plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": command(port),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }))


def _write_windows(port: int) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, "DevDash", 0, winreg.REG_SZ, _command_text(port))


def _remove_windows() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "DevDash")
    except FileNotFoundError:
        pass


def _read(method: str | None) -> str | None:
    """The command the entry starts, written the way _command_text writes it; None when off."""
    try:
        if method == "windows":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, "DevDash")[0]
        if method == "launchd":
            path = _launch_agent()
            return shlex.join(plistlib.loads(path.read_bytes())["ProgramArguments"]) if path.exists() else None
        if method in ("systemd", "xdg"):
            path = _systemd_unit() if method == "systemd" else _xdg_entry()
            if not path.exists():
                return None
            key = "ExecStart=" if method == "systemd" else "Exec="
            line = next((l[len(key):] for l in path.read_text().splitlines() if l.startswith(key)), "")
            return shlex.join(shlex.split(line.replace("%%", "%")))
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        return None
    return None


_WRITERS = {"windows": _write_windows, "launchd": _write_launchd, "systemd": _write_systemd, "xdg": _write_xdg}
_REMOVERS = {"windows": _remove_windows, "launchd": lambda: _launch_agent().unlink(missing_ok=True),
             "systemd": _remove_systemd, "xdg": lambda: _xdg_entry().unlink(missing_ok=True)}

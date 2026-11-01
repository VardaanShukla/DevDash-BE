"""Hosting a team server over HTTPS. The server itself listens on loopback only; what's in
front of it gives it the address people open:

- network: a company's own server, reached by its IP or the internal names the company's
  DNS gives it (only on the office network or its VPN). Caddy serves HTTPS on those, with the
  company's own certificate when there is one, or else one from Caddy's own certificate
  authority, whose root certificate each device trusts once. No public domain or internet
  access is needed, and Caddy uses an ordinary port (8443), so it needs no admin rights.
- domain: your own domain on a server (or any computer that's reachable), with Caddy in
  front, which gets and renews the HTTPS certificate by itself. Point the domain's DNS at
  the machine and open ports 80 and 443 first. With caddy=False something else (Docker's
  Caddy, nginx) is in front and passes requests on.
- cloudflare: a public https://….trycloudflare.com address from this computer, new each
  start, with nothing to set up. Good for trying it on a phone.
- none: this computer only (http://127.0.0.1), for trying it out.

`run` hosts in the foreground (`devdash host`, the installer's menu); `start` and `stop` run
the same thing in the background for the dashboard's Settings. What's running is written to
host.json in the team folder, which `status` reads."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from devdash.team import store

PORT = 8770
HTTPS_PORT = 8443
TUNNELS = ("network", "domain", "cloudflare", "none")
STATE = "host.json"
LOG = "host.log"
CLOUDFLARE_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# An internal name: one or more labels, like devdash, devdash.corp or devdash.office.local.
HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
CADDY_DIR = "caddy"
# Where Caddy keeps the root certificate of its own authority, under its storage folder.
ROOT_CERT = Path("pki", "authorities", "local", "root.crt")
WAIT = 45
CADDY_DOWNLOAD = "https://caddyserver.com/docs/install"
CLOUDFLARED_DOWNLOAD = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"


class HostError(RuntimeError):
    pass


def tools() -> dict:
    return {"caddy": shutil.which("caddy") is not None, "cloudflare": shutil.which("cloudflared") is not None,
            "addresses": local_addresses()}


def local_addresses() -> list[str]:
    """This machine's own network addresses (IPv4), to suggest for hosting on the network."""
    import socket

    import psutil

    found = []
    for name, addresses in psutil.net_if_addrs().items():
        if name.startswith(("docker", "br-", "veth", "lo")):
            continue
        found += [a.address for a in addresses if a.family == socket.AF_INET and not a.address.startswith("127.")]
    return found


def clean_addresses(text: str) -> list[str]:
    """The IPs and internal names from what someone typed, comma- or space-separated."""
    import ipaddress

    addresses = []
    for part in re.split(r"[,\s]+", (text or "").strip().lower()):
        part = re.sub(r"^https?://", "", part).split("/")[0]
        if not part:
            continue
        try:
            addresses.append(str(ipaddress.IPv4Address(part)))
            continue
        except ValueError:
            pass
        if not HOSTNAME.match(part) or part == "localhost":
            raise HostError(f"{part} isn't an IP address or a name. Type them like 10.0.0.5, devdash.corp")
        addresses.append(part)
    if not addresses:
        raise HostError("Give the server's IP address, and any names your company's DNS gives it.")
    return list(dict.fromkeys(addresses))


def check_own_addresses(addresses: list[str]) -> None:
    """Refuses an IP that isn't this computer's own: people's browsers would go to that other
    machine, where no team server runs. (Names are left to the company's DNS.)"""
    import ipaddress

    own = set(local_addresses())
    for address in addresses:
        try:
            ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if address not in own:
            raise HostError(f"{address} isn't this computer's address, so nobody would reach the server there. Use the "
                            f"IP of the computer that hosts it: {', '.join(own) or 'none found'}. The computers and "
                            "phones that open it just need to be on the same network.")


def fingerprint(pem: str) -> str:
    """A short fingerprint of a certificate, for people to compare by eye: the start of its
    SHA-256, in groups of four."""
    import hashlib
    import ssl

    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()[:24]
    return " ".join(digest[i:i + 4] for i in range(0, 24, 4))


def root_certificate() -> Path | None:
    """The root certificate of Caddy's own authority, which devices trust once to open the
    server without a warning; None until Caddy has made it."""
    path = store.home() / CADDY_DIR / ROOT_CERT
    return path if path.is_file() else None


def clean_domain(text: str) -> str:
    """team.example.com from what someone typed (an https:// in front, a / after)."""
    domain = re.sub(r"^https?://", "", (text or "").strip().lower()).split("/")[0]
    if not DOMAIN.match(domain):
        raise HostError("That isn't a domain name. Type one like team.example.com.")
    return domain


def _state_path() -> Path:
    return store.home() / STATE


def _write_state(**fields) -> None:
    path = _state_path()
    path.write_text(json.dumps(fields))
    if os.name == "posix":
        path.chmod(0o600)


def _read_state() -> dict | None:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    import psutil

    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


# ---------------------------------------------------------------- what's in front

def _watch(process: subprocess.Popen, lines: list[str]) -> None:
    def read():
        for line in process.stderr:
            lines.append(line)

    threading.Thread(target=read, daemon=True).start()


def _start_caddy(domain: str, port: int) -> subprocess.Popen:
    if not shutil.which("caddy"):
        raise HostError("Caddy isn't installed. It gives your domain its HTTPS certificate. The installer installs it "
                        f"(5 Team server → Host → Your domain; it asks first), or get it from {CADDY_DOWNLOAD}.")
    process = subprocess.Popen(["caddy", "reverse-proxy", "--from", domain, "--to", f"127.0.0.1:{port}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    lines: list[str] = []
    _watch(process, lines)
    # Caddy stops at once when it can't take ports 80 and 443; otherwise it keeps running and
    # fetches the certificate in the background.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.2)
    if process.poll() is not None:
        text = "".join(lines)
        if "permission denied" in text.lower() or "bind" in text.lower():
            raise HostError("Caddy couldn't use ports 80 and 443. If Caddy's own service has them, stop it "
                            "(sudo systemctl disable --now caddy). On Linux, Caddy may also need permission once: "
                            "sudo setcap cap_net_bind_service=+ep \"$(which caddy)\"")
        raise HostError(f"Caddy stopped: {text.strip()[-300:] or 'no reason given'}")
    return process


def _start_network(addresses: list[str], https_port: int, port: int, cert: str | None, key: str | None) -> subprocess.Popen:
    """Caddy serving HTTPS on the server's IP and internal names, from a Caddyfile written
    into the team folder. Redirects from plain http are off, so it needs no port 80."""
    if not shutil.which("caddy"):
        raise HostError("Caddy isn't installed. It gives the server its HTTPS certificate. The installer installs it "
                        f"(5 Team server → Host → Company network; it asks first), or get it from {CADDY_DOWNLOAD}.")
    if bool(cert) != bool(key):
        raise HostError("Give both the certificate file and its key file, or neither.")
    for path in filter(None, (cert, key)):
        if not Path(path).is_file():
            raise HostError(f"There's no file {path}.")
    folder = store.home() / CADDY_DIR
    folder.mkdir(mode=0o700, exist_ok=True)
    tls = f'tls "{Path(cert).resolve()}" "{Path(key).resolve()}"' if cert else "tls internal"
    sites = ", ".join(f"https://{address}:{https_port}" for address in addresses)
    config = folder / "Caddyfile"
    config.write_text(
        "{\n"
        "\tadmin off\n"
        "\tauto_https disable_redirects\n"
        "\tskip_install_trust\n"
        f'\tstorage file_system "{folder}"\n'
        "}\n\n"
        f"{sites} {{\n"
        f"\t{tls}\n"
        f"\treverse_proxy 127.0.0.1:{port}\n"
        "}\n")
    process = subprocess.Popen(["caddy", "run", "--config", str(config), "--adapter", "caddyfile"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    lines: list[str] = []
    _watch(process, lines)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.2)
    if process.poll() is not None:
        text = "".join(lines)
        if "address already in use" in text:
            raise HostError(f"Port {https_port} is already in use on this server. Pick another with --https-port.")
        raise HostError(f"Caddy stopped: {text.strip()[-300:] or 'no reason given'}")
    return process


def _start_cloudflare(port: int) -> tuple[str, subprocess.Popen]:
    if not shutil.which("cloudflared"):
        raise HostError(f"cloudflared isn't installed. Get it from {CLOUDFLARED_DOWNLOAD}")
    process = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    lines: list[str] = []
    _watch(process, lines)
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline and process.poll() is None:
        found = next((m.group(0) for m in map(CLOUDFLARE_URL.search, list(lines)) if m), None)
        if found:
            return found, process
        time.sleep(0.2)
    process.kill()
    raise HostError("Cloudflare didn't give the server an address. Check this computer is online, then try again.")


# ---------------------------------------------------------------- hosting

def run(port: int = PORT, tunnel: str = "cloudflare", domain: str | None = None, caddy: bool = True,
        bind: str = "127.0.0.1", addresses: str | None = None, https_port: int = HTTPS_PORT,
        cert: str | None = None, key: str | None = None, say=print) -> int:
    """Hosts the team server until Ctrl+C (or stop), with what's in front of it."""
    import uvicorn

    from devdash.team.server import create_team_app, set_reach, setup_code

    if tunnel not in TUNNELS:
        raise HostError(f"Pick how it's reached: {', '.join(TUNNELS)}.")
    running = status()
    if running["running"]:
        raise HostError(f"A team server is already running at {running['url']}. Stop it first.")
    process = None
    if tunnel == "network":
        names = clean_addresses(addresses or "")
        check_own_addresses(names)
        process = _start_network(names, https_port, port, cert, key)
        urls = [f"https://{name}:{https_port}" for name in names]
        url = urls[0]
        set_reach(urls, "caddy")
    elif tunnel == "domain":
        if not domain:
            raise HostError("Give the domain the server answers on, like team.example.com.")
        domain = clean_domain(domain)
        url = f"https://{domain}"
        if caddy:
            process = _start_caddy(domain, port)
        set_reach([url], "caddy")
    elif tunnel == "cloudflare":
        url, process = _start_cloudflare(port)
        set_reach([url], "cloudflare")
    else:
        url = f"http://127.0.0.1:{port}"
        set_reach([], "none")
    _write_state(pid=os.getpid(), port=port, url=url, tunnel=tunnel,
                 tunnel_pid=process.pid if process else None, started=store.now())
    code = setup_code()
    say(f"\nDevDash team server: {url}")
    if tunnel == "network":
        if len(urls) > 1:
            say("Also on: " + ", ".join(urls[1:]))
        say("Only devices on the company network (office wifi or VPN) can open it.")
        if not cert:
            say("Its certificate comes from Caddy's own authority. Until a device trusts that once, its browser warns "
                "first. The root certificate to install on each device (and on computers whose DevDash links to it):\n"
                f"  {store.home() / CADDY_DIR / ROOT_CERT}  (made a few seconds after starting)")
    elif tunnel == "cloudflare":
        say("Anyone can open this address; it changes each time the server starts. For a lasting address, host it on "
            "a server with your own domain.")
    elif tunnel == "domain":
        say("Its HTTPS certificate comes from Let's Encrypt the first time someone opens it; give that a few seconds.")
    say("People sign up there, make an organisation, and invite their team.")
    if code:
        say(f"\nSetup code: {code}\nSign up, then enter this code under You → Server to become the server's admin "
            "(you can close sign-ups there). It works once.")
    say("\nLeave this running; press Ctrl+C to stop.\n")
    # uvicorn returns on Ctrl+C and on stop()'s SIGTERM, so what's in front is stopped after it.
    # On Windows stop() ends the process outright and stops that itself.
    try:
        uvicorn.run(create_team_app(), host=bind, port=port, log_level="warning",
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1" if bind == "127.0.0.1" else "*")
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup(process.pid if process else None)
    return 0


def _cleanup(tunnel_pid: int | None) -> None:
    if tunnel_pid and _alive(tunnel_pid):
        import psutil

        try:
            psutil.Process(tunnel_pid).kill()
        except psutil.Error:
            pass
    _state_path().unlink(missing_ok=True)


def status() -> dict:
    """Whether a team server is running here, its address, and the setup code while nobody
    has used it (shown only on this computer's own dashboard)."""
    state = _read_state()
    if state and not _alive(state.get("pid")):
        _cleanup(state.get("tunnel_pid"))
        state = None
    code_file = store.home() / "setup-code"
    code = code_file.read_text().strip() if state and code_file.is_file() else None
    return {
        "running": state is not None,
        "url": state["url"] if state else None,
        "tunnel": state["tunnel"] if state else None,
        "started": state["started"] if state else None,
        "setup_code": code,
        # Shown while hosting on the company network with Caddy's own certificate.
        "root_certificate": str(root_certificate()) if state and state.get("tunnel") == "network" and root_certificate() else None,
        "root_fingerprint": fingerprint(root_certificate().read_text())
        if state and state.get("tunnel") == "network" and root_certificate() else None,
        "tools": tools(),
    }


def start(tunnel: str, domain: str | None = None, port: int = PORT, addresses: str | None = None) -> dict:
    """Hosts in the background and waits for the address, for the dashboard."""
    if status()["running"]:
        raise HostError("A team server is already running.")
    if tunnel not in TUNNELS:
        raise HostError(f"Pick how it's reached: {', '.join(TUNNELS)}.")
    args = [sys.executable, "-m", "devdash", "host", "--port", str(port), "--tunnel", tunnel]
    if tunnel == "domain":
        args += ["--domain", clean_domain(domain or "")]
    elif tunnel == "network":
        args += ["--address", ",".join(clean_addresses(addresses or ""))]
    log = open(store.home() / LOG, "w")
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               env={**os.environ, "PYTHONUNBUFFERED": "1"}, **options)
    deadline = time.monotonic() + WAIT + 15
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                text = (store.home() / LOG).read_text()
                reason = next((line.removeprefix("devdash: ") for line in reversed(text.splitlines())
                               if line.startswith("devdash: ")), text.strip()[-300:] or "it stopped")
                raise HostError(reason)
            found = status()
            if found["running"]:
                return found
            time.sleep(0.3)
        process.kill()
        raise HostError("The team server didn't start in time. Try from a terminal with `devdash host` to see why.")
    finally:
        log.close()


def stop() -> dict:
    state = _read_state()
    if state and _alive(state.get("pid")):
        import psutil

        try:
            process = psutil.Process(state["pid"])
            process.terminate()
            process.wait(10)
        except psutil.Error:
            pass
    if state:
        _cleanup(state.get("tunnel_pid"))
    return status()

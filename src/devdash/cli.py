"""Command line entry point. Uses argparse rather than a CLI framework so that
`devdash record`, which runs on every prompt and edit, starts fast."""

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devdash")
    commands = parser.add_subparsers(dest="command", required=True)

    init_cmd = commands.add_parser("init", help="track a git repo (its own history needs no hooks), or remember a "
                                                "folder of projects to pick from when adding one")
    init_cmd.add_argument("path", nargs="?", default=".")

    # The AI tools DevDash records (install.TOOLS), written out so `record` starts fast.
    tools = ["claude", "codex", "antigravity"]
    for name, text in (("connect", "start recording an AI tool's changes in a repo"),
                       ("disconnect", "stop recording AI changes in a repo")):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument("tool", choices=tools)
        cmd.add_argument("path", nargs="?", default=".")

    record_cmd = commands.add_parser("record", help="called by hooks; reads the event from stdin")
    record_cmd.add_argument("event", choices=["user-prompt-submit", "pre-tool-use", "post-tool-use", "stop", "session-end", "post-commit"])
    record_cmd.add_argument("--tool", choices=tools, default="claude", help="the AI tool whose hook this is")

    turns_cmd = commands.add_parser("turns", help="list recent turns")
    turns_cmd.add_argument("--limit", type=int, default=10)
    turns_cmd.add_argument("--all", action="store_true", help="include turns that changed no files")

    commands.add_parser("instructions", help="print the text to give Claude so app chats are recorded")

    commands.add_parser("remove-hooks", help="take DevDash's hooks out of every project and AI tool (before uninstalling)")

    commands.add_parser("password", help="set the dashboard's username and password (also the way back in if it's forgotten)")

    serve_cmd = commands.add_parser("serve", help="run the local API the UI talks to")
    serve_cmd.add_argument("--port", type=int, default=8765)

    host_cmd = commands.add_parser("host", help="host a team server anyone can open over HTTPS, where people make "
                                                "organisations and teams and share explanations of changes")
    host_cmd.add_argument("--tunnel", choices=["network", "domain", "cloudflare", "none"], default="cloudflare",
                          help="network: a company server reached by IP or internal names on the office network or "
                               "VPN (give --address); domain: your own public domain, with Caddy for HTTPS (give "
                               "--domain); cloudflare: a public address from this computer (default); none: this "
                               "computer only")
    host_cmd.add_argument("--address", help="with --tunnel network: the server's IP and internal names, comma-separated, "
                                            "like 10.0.0.5,devdash.corp")
    host_cmd.add_argument("--https-port", type=int, default=8443, help="with --tunnel network: the HTTPS port (8443)")
    host_cmd.add_argument("--cert", help="with --tunnel network: the company's certificate file for those names "
                                         "(otherwise Caddy makes its own)")
    host_cmd.add_argument("--key", help="with --cert: the certificate's key file")
    host_cmd.add_argument("--domain", help="the domain it answers on, like team.example.com (with --tunnel domain)")
    host_cmd.add_argument("--no-caddy", action="store_true",
                          help="with --tunnel domain: something else (Docker's Caddy, nginx) provides HTTPS in front")
    host_cmd.add_argument("--bind", default="127.0.0.1", help="the address to listen on; only change it inside Docker")
    host_cmd.add_argument("--port", type=int, default=8770)

    team_cmd = commands.add_parser("team", help="link this DevDash to a team server, and link projects to it")
    team_actions = team_cmd.add_subparsers(dest="action", required=True)
    connect_server = team_actions.add_parser("connect", help="link to a team server (you approve it in your browser)")
    connect_server.add_argument("url")
    team_actions.add_parser("status", help="show the team server this DevDash is linked to")
    team_actions.add_parser("disconnect", help="forget the team server here")
    link_cmd = team_actions.add_parser("link", help="link a tracked project to the team server")
    link_cmd.add_argument("path", nargs="?", default=".")

    args = parser.parse_args(argv)

    if args.command == "host":
        from devdash.team import host

        try:
            return host.run(args.port, args.tunnel, args.domain, not args.no_caddy, args.bind,
                            args.address, args.https_port, args.cert, args.key)
        except host.HostError as error:
            print(f"devdash: {error}")
            return 1

    if args.command == "team":
        return _team(args)

    if args.command == "record":
        from devdash.capture.record import run_hook

        return run_hook(args.event, tool=args.tool)

    if args.command in ("init", "connect", "disconnect"):
        from devdash.capture import install

        path = Path(args.path)
        if args.command == "init" and path.is_dir() and git_toplevel(path) is None:
            found = install.add_workspace(path)
            if not found:
                print(f"devdash: {path} is not inside a git repository, and has none inside it")
                return 1
            print(f"Found {len(found)} project(s) in {path.resolve()}:")
            for repo in found:
                print(f"  {repo}")
            print("Nothing was added or changed in them. Pick the ones to track under Add project on the dashboard.")
            return 0
        try:
            if args.command == "connect":
                root = install.connect_tool(path, args.tool)
            else:
                root = {"init": install.init, "disconnect": install.disconnect}[args.command](path)
        except install.InitError as error:
            print(f"devdash: {error}")
            return 1
        if args.command == "init":
            print(f"Tracking {root}")
            print("To also record your AI tool's changes here: devdash connect claude (or codex, or antigravity)")
        elif args.command == "connect":
            name = install.TOOL_NAMES[args.tool]
            print(f"Recording {name}'s changes in {root}")
            print(f"Installed: git post-commit hook, {name} hooks in {install.tool_hooks_path(args.tool)}")
            if args.tool == "claude":
                print("Restart any open Claude Code session to pick up the hooks.")
                print("For Claude app chats, add the text from `devdash instructions` to your project instructions.")
            elif args.tool == "codex":
                print("Open Codex, type /hooks and trust DevDash's hooks; restart any Codex session already open.")
            else:
                print("Restart agy or the Antigravity app if it's open, so it loads the hooks.")
        else:
            print(f"Stopped recording AI changes in {root}. Recorded history is kept.")
        return 0

    if args.command == "instructions":
        from devdash.capture.install import INSTRUCTIONS

        print(INSTRUCTIONS, end="")
        return 0

    if args.command == "turns":
        return _print_turns(args.limit, args.all)

    if args.command == "password":
        return _set_password()

    if args.command == "remove-hooks":
        from devdash.capture import install

        for root in install.remove_all_hooks():
            print(f"Took DevDash's git hook out of {root}")
        print("Took DevDash's hooks out of Claude Code, Codex and Antigravity.")
        return 0

    if args.command == "serve":
        return _serve(args.port)

    return 1


def _team(args) -> int:
    import time

    from devdash.team import client

    try:
        if args.action == "connect":
            found = client.start(args.url)
            print(f"Open this on any device where you're signed in to the team server, and approve it:\n\n"
                  f"  {found['pending']['link']}\n\nThe code shown there should be {found['pending']['user_code']}.")
            print("Waiting… (Ctrl+C to stop)")
            while not client.poll()["connected"]:
                time.sleep(3)
            now = client.status()
            print(f"Linked to {now['org']} as {now['person']['name']}.")
            print("Next: devdash team link (in a project's folder), then Share with team on an explanation.")
        elif args.action == "status":
            now = client.status()
            if not now["connected"]:
                print("Not linked to a team server. Run: devdash team connect <address>")
            else:
                print(f"Linked to {now['org']} ({now['url']}) as {now['person']['name']}.")
                for linked in now["projects"].values():
                    print(f"  project: {linked['name']}")
        elif args.action == "disconnect":
            client.disconnect()
            print("Forgot the team server here. Unlink this DevDash on the server too (You → Linked DevDashes).")
        elif args.action == "link":
            from devdash.store import connect

            root = git_toplevel(Path(args.path))
            conn = connect()
            row = conn.execute("SELECT id FROM repos WHERE path = ?", (str(root),)).fetchone() if root else None
            conn.close()
            repo_id = row["id"] if row else None
            if repo_id is None:
                print("devdash: this folder isn't a tracked project. Add it first: devdash init")
                return 1
            client.link(repo_id)
            print(f"Linked {root.name} to the team server.")
    except client.TeamError as error:
        print(f"devdash: {error}")
        return 1
    except KeyboardInterrupt:
        client.cancel()
        print("\nStopped linking.")
        return 1
    return 0


def _serve(port: int) -> int:
    import uvicorn

    from devdash.api.app import WEB, create_app
    from devdash.capture import install

    # Hooks name the Python that runs DevDash; this keeps them right after a reinstall.
    for problem in install.refresh():
        print(f"devdash: {problem}")
    # Loopback only: the API serves the contents of your repos.
    if (WEB / "index.html").is_file():
        print(f"DevDash is running. Open http://127.0.0.1:{port} in your browser.")
    else:
        print(f"DevDash API on http://127.0.0.1:{port} (docs at /docs). The page isn't built into "
              "this copy: run scripts/build_ui.py, or the UI's dev server.")
    print("Leave this running; press Ctrl+C to stop.")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
    return 0


def git_toplevel(path: Path):
    from devdash import git

    return git.toplevel(path)


def _set_password() -> int:
    from getpass import getpass

    from devdash import auth

    current = auth.username()
    name = input(f"Username{f' [{current}]' if current else ''}: ").strip() or current
    password = getpass("New DevDash password: ")
    if getpass("Type it again: ") != password:
        print("devdash: the two didn't match; nothing changed")
        return 1
    try:
        # Whoever can run this can already read DevDash's files, so no current password.
        auth.set_password(password, check_current=False, name=name)
    except auth.AuthError as error:
        print(f"devdash: {error}")
        return 1
    print("Username and password set. Every browser was signed out; sign in again on the dashboard.")
    return 0


def _print_turns(limit: int, include_empty: bool) -> int:
    from devdash.prompts import readable
    from devdash.store import connect

    conn = connect()
    rows = conn.execute(
        """
        SELECT t.id, t.started_at, t.branch, t.prompt,
               COALESCE(GROUP_CONCAT(DISTINCT cr.name), sr.name) AS repo,
               COUNT(c.id) AS files, MAX(c.commit_sha) AS commit_sha
        FROM turns t
        JOIN sessions s ON s.id = t.session_id
        LEFT JOIN repos sr ON sr.id = s.repo_id
        LEFT JOIN changes c ON c.turn_id = t.id
        LEFT JOIN repos cr ON cr.id = c.repo_id
        GROUP BY t.id HAVING ? OR COUNT(c.id) > 0
        ORDER BY t.id DESC LIMIT ?
        """,
        (include_empty, limit),
    ).fetchall()
    if not rows:
        print("No turns with changes yet." + ("" if include_empty else " (--all shows the rest)"))
    for row in rows:
        prompt = " ".join(readable(row["prompt"]).split())
        prompt = prompt if len(prompt) <= 60 else prompt[:57] + "..."
        commit = (row["commit_sha"] or "uncommitted")[:11]
        print(f"#{row['id']:<4} {row['started_at']}  {row['repo']}@{row['branch'] or '-'}  "
              f"{row['files']} file(s)  {commit}  {prompt}")
    conn.close()
    return 0

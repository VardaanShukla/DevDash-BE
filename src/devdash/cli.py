"""Command line entry point. Uses argparse rather than a CLI framework so that
`devdash record`, which runs on every prompt and edit, starts fast."""

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devdash")
    commands = parser.add_subparsers(dest="command", required=True)

    init_cmd = commands.add_parser("init", help="track a git repo (its own history needs no hooks)")
    init_cmd.add_argument("path", nargs="?", default=".")

    for name, text in (("connect", "start recording Claude's changes in a repo"),
                       ("disconnect", "stop recording Claude's changes in a repo")):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument("tool", choices=["claude"])
        cmd.add_argument("path", nargs="?", default=".")

    record_cmd = commands.add_parser("record", help="called by hooks; reads the event from stdin")
    record_cmd.add_argument("event", choices=["user-prompt-submit", "pre-tool-use", "stop", "post-commit"])

    turns_cmd = commands.add_parser("turns", help="list recent turns")
    turns_cmd.add_argument("--limit", type=int, default=10)
    turns_cmd.add_argument("--all", action="store_true", help="include turns that changed no files")

    commands.add_parser("instructions", help="print the text to give Claude so app chats are recorded")

    commands.add_parser("password", help="set the dashboard's username and password (also the way back in if it's forgotten)")

    serve_cmd = commands.add_parser("serve", help="run the local API the UI talks to")
    serve_cmd.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)

    if args.command == "record":
        from devdash.capture.record import run_hook

        return run_hook(args.event)

    if args.command in ("init", "connect", "disconnect"):
        from devdash.capture import install

        action = {"init": install.init, "connect": install.connect_claude,
                  "disconnect": install.disconnect_claude}[args.command]
        try:
            root = action(Path(args.path))
        except install.InitError as error:
            print(f"devdash: {error}")
            return 1
        if args.command == "init":
            print(f"Tracking {root}")
            print("To also record Claude's changes here: devdash connect claude")
        elif args.command == "connect":
            print(f"Recording Claude's changes in {root}")
            print(f"Installed: git post-commit hook, Claude Code hooks in {install.claude_settings_path()}")
            print("Restart any open Claude Code session to pick up the hooks.")
            print("For Claude app chats, add the text from `devdash instructions` to your project instructions.")
        else:
            print(f"Stopped recording Claude's changes in {root}. Recorded history is kept.")
        return 0

    if args.command == "instructions":
        from devdash.capture.install import INSTRUCTIONS

        print(INSTRUCTIONS, end="")
        return 0

    if args.command == "turns":
        return _print_turns(args.limit, args.all)

    if args.command == "password":
        return _set_password()

    if args.command == "serve":
        return _serve(args.port)

    return 1


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

"""Thin wrappers around the git command line."""

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def run(cwd: Path | str, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace"
    )
    if result.returncode not in ok:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def toplevel(cwd: Path | str) -> Path | None:
    try:
        return Path(run(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
    except (GitError, FileNotFoundError, NotADirectoryError):
        return None


def branch(repo: Path) -> str | None:
    # symbolic-ref also works before the first commit; it fails on a detached HEAD.
    try:
        return run(repo, "symbolic-ref", "--short", "-q", "HEAD").strip() or None
    except GitError:
        return None


def hooks_dir(repo: Path) -> Path:
    # Respects core.hooksPath and worktrees.
    return (repo / run(repo, "rev-parse", "--git-path", "hooks").strip()).resolve()


def commit_info(repo: Path, sha: str = "HEAD") -> dict:
    out = run(repo, "show", "-s", "--format=%H%x00%aI%x00%B", sha)
    full_sha, authored_at, message = out.split("\x00", 2)
    return {"sha": full_sha, "authored_at": authored_at, "message": message.strip()}


def commit_files(repo: Path, sha: str) -> list[str]:
    # --root makes the first commit list its files too.
    out = run(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", sha)
    return [name for name in out.split("\x00") if name]


def commit_file_diff(repo: Path, sha: str, file_path: str) -> str:
    return run(repo, "show", "--format=", "--patch", sha, "--", file_path)


def fetch(repo: Path, timeout: float = 30) -> str | None:
    """Updates what this machine knows about the remotes, and drops branches deleted
    there. Touches only remote-tracking refs, never local branches or files. Returns
    None on success, or a short reason it failed. Never prompts: a remote that needs a
    password or an unlocked key simply fails."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -o BatchMode=yes",
    }
    try:
        result = subprocess.run(
            ["git", "fetch", "--all", "--prune", "--quiet", "--no-write-fetch-head"],
            cwd=repo, capture_output=True, text=True, errors="replace", env=env, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return "timed out reaching the remote"
    if result.returncode != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        return lines[-1] if lines else "git fetch failed"
    return None

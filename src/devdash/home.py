"""One line of facts per project for the home page: the branch it's on, how far that is
ahead of the base, what isn't committed yet, the latest commit and recent activity. Cheap
git commands only, since the home page asks for every project at once."""

from pathlib import Path

from devdash import compare, git


def project(path: Path) -> dict:
    if not path.is_dir() or git.toplevel(path) is None:
        # A folder without git is still a project; it just has no branches or commits.
        return {"exists": path.is_dir(), "git": False, "branch": None, "base": None, "ahead": None,
                "uncommitted": 0, "last_commit": None, "commits_this_week": 0, "branches": 0}
    branch = git.branch(path)
    base = compare.default_base(path)
    has_commits = bool(git.run(path, "rev-parse", "-q", "--verify", "HEAD", ok=(0, 1)).strip())
    ahead = None
    if has_commits and base and branch != base:
        try:
            start = compare.fork_point(path, compare.freshest(path, base), branch or "HEAD")
            ahead = int(git.run(path, "rev-list", "--count", f"{start}..HEAD").strip())
        except git.GitError:
            ahead = None
    last = None
    week = 0
    if has_commits:
        subject, author, when = git.run(path, "log", "-1", "--format=%s%x00%an%x00%aI").rstrip("\n").split("\x00")
        last = {"subject": subject, "author": author, "at": when}
        week = int(git.run(path, "rev-list", "--count", "--since=7.days", "HEAD").strip())
    status = git.run(path, "status", "--porcelain", "--untracked-files=normal")
    return {
        "exists": True,
        "git": True,
        "branch": branch,
        "base": base,
        "ahead": ahead,
        "uncommitted": len([line for line in status.splitlines() if line.strip()]),
        "last_commit": last,
        "commits_this_week": week,
        "branches": len(compare.branches(path)),
    }

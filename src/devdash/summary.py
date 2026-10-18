"""The whole project on one page: what it is (from its readme and package files), what it
is built with, how to run it, its parts, and who has worked on it and when. All of it is
read from git, so it is the same for everyone and needs no AI."""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from devdash import git
from devdash.projectmap import _files, project_map

try:
    import tomllib
except ImportError:  # Python 3.10: a few fields are read with patterns instead.
    tomllib = None

README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")
MANIFEST_NAMES = ("package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml", "composer.json")
ENTRY_NAMES = {"main.ts", "main.tsx", "main.js", "main.jsx", "index.ts", "index.tsx", "index.js", "main.py",
               "__main__.py", "app.py", "manage.py", "server.js", "server.ts", "cli.py", "main.go", "main.rs",
               "app.tsx", "app.jsx", "app.js"}
MAX_INTRO = 1500
WEEKS = 12

# Well-known tools a project is built with, and what each does, in plain words.
TECH = {
    "react": "Builds the screens people see",
    "next": "Website framework on top of React",
    "vue": "Builds the screens people see",
    "svelte": "Builds the screens people see",
    "@angular/core": "Builds the screens people see",
    "react-native": "Builds the phone app",
    "expo": "Builds and runs the phone app",
    "electron": "Turns the web app into a desktop app",
    "vite": "Runs the app while you work and packs it for release",
    "webpack": "Packs the app's files for release",
    "typescript": "JavaScript with types, which catches mistakes early",
    "tailwindcss": "Styling for the screens",
    "react-router": "Moves between pages",
    "react-router-dom": "Moves between pages",
    "redux": "Keeps the app's data in one place",
    "@reduxjs/toolkit": "Keeps the app's data in one place",
    "zustand": "Keeps the app's data in one place",
    "axios": "Talks to servers over the internet",
    "express": "Web server",
    "fastify": "Web server",
    "socket.io": "Live updates between server and screen",
    "graphql": "A way to ask the server for data",
    "prisma": "Talks to the database",
    "mongoose": "Talks to the MongoDB database",
    "firebase": "Google's hosted database and sign-in",
    "@supabase/supabase-js": "Hosted database and sign-in",
    "stripe": "Takes payments",
    "openai": "Uses OpenAI's AI models",
    "@anthropic-ai/sdk": "Uses Claude, Anthropic's AI",
    "anthropic": "Uses Claude, Anthropic's AI",
    "langchain": "Builds features on AI models",
    "d3": "Draws charts and diagrams",
    "recharts": "Draws charts",
    "chart.js": "Draws charts",
    "three": "Draws 3D graphics",
    "jest": "Runs automated tests",
    "vitest": "Runs automated tests",
    "@playwright/test": "Tests the app in a real browser",
    "playwright": "Tests the app in a real browser",
    "cypress": "Tests the app in a real browser",
    "eslint": "Checks the code for common mistakes",
    "fastapi": "Web API server",
    "flask": "Web server",
    "django": "Website framework with a database and admin",
    "uvicorn": "Runs the Python web server",
    "pydantic": "Checks that data has the right shape",
    "sqlalchemy": "Talks to the database",
    "psycopg": "Talks to the PostgreSQL database",
    "psycopg2": "Talks to the PostgreSQL database",
    "pymongo": "Talks to the MongoDB database",
    "redis": "Fast in-memory storage",
    "celery": "Runs slow jobs in the background",
    "requests": "Talks to servers over the internet",
    "httpx": "Talks to servers over the internet",
    "pytest": "Runs automated tests",
    "numpy": "Number crunching",
    "pandas": "Works with tables of data",
    "torch": "Machine learning",
    "tensorflow": "Machine learning",
    "scikit-learn": "Machine learning",
    "transformers": "Runs AI language models",
    "click": "Builds the command-line tool",
    "typer": "Builds the command-line tool",
}


def summary(repo: Path, revision: str) -> dict:
    files = _files(repo, revision)
    paths = [path for path, _, _ in files]
    # The top-level readme, or else the one nearest the top, as in a folder per app.
    readmes = sorted((p for p in paths if PurePosixPath(p).name.lower() in README_NAMES and p.count("/") <= 2),
                     key=lambda p: (p.count("/"), README_NAMES.index(PurePosixPath(p).name.lower()), p))
    readme_path = readmes[0] if readmes else None
    readme = _readme(_show(repo, revision, readme_path)) if readme_path else None
    manifests = [m for m in (_manifest(repo, revision, path) for path in _manifest_paths(paths)) if m]
    dependencies = sorted({d for m in manifests for d in m["dependencies"] + m["dev_dependencies"]})
    tech = [{"name": name, "what": TECH[name]} for name in dependencies if name in TECH]
    if any(path.endswith((".ts", ".tsx")) for path in paths) and not any(t["name"] == "typescript" for t in tech):
        tech.append({"name": "typescript", "what": TECH["typescript"]})

    project = project_map(repo, revision)
    languages: Counter = Counter()
    for area in project["areas"]:
        for language in area["languages"]:
            languages[language["name"]] += language["files"]

    named = next((m for m in manifests if m.get("description")), None)
    return {
        "revision": revision,
        "name": (named or {}).get("name") or (readme or {}).get("title") or repo.name,
        "description": (named or {}).get("description") or (readme or {}).get("first_sentence"),
        "readme": readme,
        "license": _license(repo, revision, paths),
        "tech": tech,
        "other_dependencies": len(dependencies) - len([t for t in tech if t["name"] in dependencies]),
        "run": [{"where": m["path"], "name": n, "command": c} for m in manifests for n, c in m["scripts"].items()][:12],
        "entry_points": sorted(p for p in paths if PurePosixPath(p).name.lower() in ENTRY_NAMES)[:8],
        "files": len(paths),
        "bytes": sum(size for _, _, size in files),
        "languages": [{"name": n, "files": c} for n, c in languages.most_common(6)],
        "parts": project["areas"],
        "links": project["edges"],
        "history": _history(repo, revision),
    }


def _show(repo: Path, revision: str, path: str) -> str:
    try:
        return git.run(repo, "show", f"{revision}:{path}")
    except git.GitError:
        return ""


def _readme(text: str) -> dict | None:
    """The readme's title, the prose before its first section, and its section names."""
    if not text.strip():
        return None
    title = None
    intro: list[str] = []
    sections = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading:
            if title is None and heading[1] == "#":
                title = _plain(heading[2])
            else:
                sections.append(_plain(heading[2]))
            continue
        # Badges and images say nothing to a reader.
        if not sections and line.strip() and not re.match(r"^\s*(\[!\[|!\[|<)", line):
            intro.append(_plain(line.strip()))
    paragraph = " ".join(intro)[:MAX_INTRO].strip()
    first = re.split(r"(?<=[.!?])\s", paragraph, maxsplit=1)[0] if paragraph else None
    return {"title": title, "intro": paragraph or None, "first_sentence": first, "sections": sections[:12]}


def _plain(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[*_`]", "", text).strip()


def _manifest_paths(paths: list[str]) -> list[str]:
    # The project's own package files: at the top or one or two folders down.
    found = [p for p in paths if PurePosixPath(p).name.lower() in MANIFEST_NAMES and p.count("/") <= 2]
    return sorted(found, key=lambda p: (p.count("/"), p))[:8]


def _manifest(repo: Path, revision: str, path: str) -> dict | None:
    text = _show(repo, revision, path)
    name = PurePosixPath(path).name.lower()
    entry = {"path": path, "name": None, "description": None, "scripts": {}, "dependencies": [], "dev_dependencies": []}
    try:
        if name in ("package.json", "composer.json"):
            data = json.loads(text)
            entry.update(name=data.get("name"), description=data.get("description"),
                         scripts=dict(list((data.get("scripts") or {}).items())[:8]),
                         dependencies=list((data.get("dependencies") or data.get("require") or {}).keys()),
                         dev_dependencies=list((data.get("devDependencies") or data.get("require-dev") or {}).keys()))
        elif name == "pyproject.toml":
            data = tomllib.loads(text) if tomllib else _toml_fallback(text)
            project = data.get("project") or data.get("tool", {}).get("poetry") or {}
            deps = project.get("dependencies") or []
            if isinstance(deps, dict):
                deps = list(deps)
            optional = [d for group in (project.get("optional-dependencies") or {}).values() for d in group]
            entry.update(name=project.get("name"), description=project.get("description"),
                         scripts={k: v for k, v in list((project.get("scripts") or {}).items())[:8]},
                         dependencies=[_requirement(d) for d in deps if _requirement(d) != "python"],
                         dev_dependencies=[_requirement(d) for d in optional])
        elif name == "requirements.txt":
            entry["dependencies"] = [_requirement(line) for line in text.splitlines()
                                     if line.strip() and not line.strip().startswith(("#", "-"))]
        elif name == "go.mod":
            module = re.search(r"^module\s+(\S+)", text, re.M)
            entry["name"] = module[1] if module else None
            entry["dependencies"] = re.findall(r"^\s+([\w./-]+)\s+v", text, re.M)
        elif name == "cargo.toml":
            data = tomllib.loads(text) if tomllib else {}
            package = data.get("package") or {}
            entry.update(name=package.get("name"), description=package.get("description"),
                         dependencies=list((data.get("dependencies") or {}).keys()))
    except (ValueError, AttributeError, TypeError):
        return None
    entry["dependencies"] = [d.lower() for d in entry["dependencies"] if d]
    entry["dev_dependencies"] = [d.lower() for d in entry["dev_dependencies"] if d]
    return entry


def _toml_fallback(text: str) -> dict:
    """The [project] name, description and dependencies, without a TOML parser."""
    field = lambda key: (re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.M) or [None, None])[1]
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.M | re.S)
    deps = re.findall(r'"([^"]+)"', block[1]) if block else []
    return {"project": {"name": field("name"), "description": field("description"), "dependencies": deps}}


def _requirement(spec: str) -> str:
    return re.split(r"[\s<>=!~;\[@]", spec.strip(), maxsplit=1)[0]


def _license(repo: Path, revision: str, paths: list[str]) -> str | None:
    path = next((p for p in paths if "/" not in p and PurePosixPath(p).stem.upper() in ("LICENSE", "LICENCE", "COPYING")), None)
    if not path:
        return None
    first = next((line.strip() for line in _show(repo, revision, path).splitlines() if line.strip()), "")
    return first[:80] or None


def _history(repo: Path, revision: str) -> dict:
    """Commits, people and activity: how much work went in, by whom, and how recently."""
    out = git.run(repo, "log", "--format=%aI%x00%an", revision)
    entries = [line.split("\x00", 1) for line in out.splitlines() if "\x00" in line]
    if not entries:
        return {"commits": 0, "people": [], "first": None, "last": None, "weeks": [0] * WEEKS, "branches": 0}
    people = Counter(name for _, name in entries)
    now = datetime.now(timezone.utc)
    weeks = [0] * WEEKS
    for date, _ in entries:
        age = (now - datetime.fromisoformat(date)).days // 7
        if 0 <= age < WEEKS:
            weeks[WEEKS - 1 - age] += 1
    return {
        "commits": len(entries),
        "people": [{"name": n, "commits": c} for n, c in people.most_common(6)],
        "first": entries[-1][0],
        "last": entries[0][0],
        "weeks": weeks,
        "branches": len([b for b in git.run(repo, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines() if b]),
    }

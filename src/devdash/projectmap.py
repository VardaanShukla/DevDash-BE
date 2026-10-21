"""A map of the project for people who don't read code: its main parts (folders), what
each part is for, which languages it uses, and which parts use code from which. Read
from a branch's committed files, so it works for any branch, checked out or not."""

import posixpath
import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

from devdash import git

# Splitting stops once a level has this many parts, or a part has fewer files.
MAX_AREAS = 12
MIN_SPLIT_FILES = 3
# Files read to find imports; beyond this the map has parts but fewer connections.
MAX_SCANNED = 3000
MAX_BYTES = 256_000
# Files named per part, biggest first, so a screen can draw them without a second request.
MAX_LISTED = 150

SKIP_DIRS = {"node_modules", "dist", "build", ".next", "vendor", "__pycache__", ".venv", "venv", "coverage"}

LANGUAGES = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".py": "Python", ".css": "CSS", ".scss": "CSS",
    ".html": "HTML", ".md": "Docs", ".json": "Config", ".yml": "Config", ".yaml": "Config",
    ".toml": "Config", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".swift": "Swift", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C", ".cpp": "C++",
    ".cs": "C#", ".sql": "SQL", ".sh": "Shell", ".vue": "Vue", ".svelte": "Svelte",
    ".png": "Images", ".jpg": "Images", ".jpeg": "Images", ".svg": "Images", ".gif": "Images",
    ".webp": "Images", ".ico": "Images",
}

# What a folder is usually for, by its name, in plain words.
ROLES = [
    ({"screens", "pages", "views", "routes", "app"}, "Screens people see"),
    ({"components", "ui", "widgets", "layouts"}, "Building blocks of the screens"),
    ({"api", "services", "client", "clients", "http", "server", "backend", "endpoints"}, "Talks to the server"),
    ({"store", "state", "redux", "context", "stores", "slices"}, "Data the app keeps in memory"),
    ({"hooks"}, "Reusable screen logic"),
    ({"utils", "lib", "helpers", "common", "shared", "core"}, "Shared helpers"),
    ({"models", "types", "schemas", "entities", "interfaces"}, "Shapes of the data"),
    ({"styles", "css", "theme", "themes"}, "Look and feel"),
    ({"assets", "public", "static", "images", "img", "icons", "fonts"}, "Images and other files"),
    ({"tests", "test", "__tests__", "spec", "e2e"}, "Automated checks"),
    ({"docs", "doc"}, "Documentation"),
    ({"config", "configs", "settings"}, "Settings"),
    ({"db", "database", "migrations", "store"}, "Database"),
    ({"auth", "login"}, "Sign-in"),
    ({"scripts", "bin", "tools"}, "Developer scripts"),
    ({"admin"}, "Admin tools"),
]

LOOSE = "Settings and loose files"
NOT_CODE = {"Docs", "Config", "Images", "CSS", "HTML"}

JS_IMPORT = re.compile(
    r"""(?:import|export)\s[^'";]*?from\s*['"]([^'"]+)['"]|import\s*\(\s*['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"]"""
)
PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w*,\s()]+)|import\s+([\w.]+))", re.MULTILINE)
JS_EXTENSIONS = ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".css", ".scss",
                 "/index.ts", "/index.tsx", "/index.js", "/index.jsx"]


# A project with no commit yet is read from its folder: the files git would add.
WORKTREE = "(not committed yet)"


def readable(repo: Path, revision: str) -> str:
    """The revision to read: as asked, or the working tree while HEAD has no commit yet."""
    if revision == "HEAD" and not git.run(repo, "rev-parse", "-q", "--verify", "HEAD", ok=(0, 1)).strip():
        return WORKTREE
    return revision


def project_map(repo: Path, revision: str) -> dict:
    revision = readable(repo, revision)
    files = _files(repo, revision)
    paths = [path for path, _, _ in files]
    areas = _areas(paths)
    area_of = {path: _area_for(path, areas) for path in paths}

    by_area: dict[str, dict] = {
        area: {"id": area, "name": _name(area), "path": area, "files": 0, "bytes": 0, "languages": Counter(),
               # A folder that was opened up keeps only its loose files, usually settings.
               "file_list": [], "role": _role(area) or (LOOSE
                                       if any(other.startswith(area + "/") for other in areas) else None)}
        for area in areas
    }
    for path, _, size in files:
        entry = by_area[area_of[path]]
        entry["files"] += 1
        entry["bytes"] += size
        entry["file_list"].append({"path": path, "bytes": size})
        language = LANGUAGES.get(PurePosixPath(path).suffix.lower())
        if language:
            entry["languages"][language] += 1

    links, scanned = _imports(repo, files)
    edges: Counter = Counter()
    for source, target in links:
        a, b = area_of[source], area_of[target]
        if a != b:
            edges[(a, b)] += 1

    for entry in by_area.values():
        # Loose files next to opened-up folders: code there usually starts the app.
        if entry["role"] == LOOSE and any(language not in NOT_CODE for language in entry["languages"]):
            entry["role"] = "Starts the app and ties parts together"

    return {
        "revision": revision,
        "total_files": len(paths),
        "areas": [
            {**entry, "languages": [{"name": n, "files": c} for n, c in entry["languages"].most_common(4)],
             "file_list": sorted(entry["file_list"], key=lambda f: (-f["bytes"], f["path"]))[:MAX_LISTED]}
            for entry in sorted(by_area.values(), key=lambda e: -e["files"])
        ],
        "edges": [{"source": a, "target": b, "imports": n} for (a, b), n in edges.most_common()],
        "complete": scanned,
    }


def _files(repo: Path, revision: str) -> list[tuple[str, str, int]]:
    """(path, blob sha, size) for each file in the revision, skipping build output. From the
    working tree, the sha is the path itself (see _read_blobs)."""
    if revision == WORKTREE:
        return _worktree_files(repo)
    out = git.run(repo, "ls-tree", "-r", "-l", "-z", revision)
    result = []
    for record in out.split("\x00"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, kind, sha, size = meta.split()
        folders = path.split("/")[:-1]
        if kind != "blob" or SKIP_DIRS.intersection(folders) or any(f.endswith(".egg-info") for f in folders):
            continue
        result.append((path, sha, int(size) if size.isdigit() else 0))
    return result


def _areas(paths: list[str]) -> list[str]:
    """Folders to show as parts. Starts with the top-level folders and opens up any part
    holding much of the project (often src/), so the map shows what is inside it."""
    def group(prefixes: list[str]) -> Counter:
        counts: Counter = Counter()
        for path in paths:
            counts[_area_for(path, prefixes)] += 1
        return counts

    areas = sorted({path.split("/")[0] for path in paths if "/" in path}) + [""]
    for _ in range(10):
        counts = group(areas)
        biggest, size = max(counts.items(), key=lambda item: item[1])
        # Open the biggest part while it holds a quarter of the project or more.
        if biggest == "" or size < MIN_SPLIT_FILES or size < 0.25 * len(paths):
            break
        children = sorted({"/".join(path.split("/")[: biggest.count("/") + 2])
                           for path in paths if path.startswith(biggest + "/") and
                           path.count("/") > biggest.count("/") + 1})
        if not children or len(areas) - 1 + len(children) > MAX_AREAS:
            break
        # The folder keeps its own loose files as a part of its own.
        areas = [area for area in areas if area != biggest] + children + [biggest]
    counts = group(areas)
    return [area for area in areas if counts[area]]


def _area_for(path: str, areas: list[str]) -> str:
    best = ""
    for area in areas:
        if area and (path == area or path.startswith(area + "/")) and len(area) > len(best):
            best = area
    return best


GENERIC = {"src", "lib", "app", "source", "main"}


def _name(area: str) -> str:
    # src alone says little; devdash-ui/src says which src.
    parts = area.split("/") if area else []
    if not parts:
        return "Project root"
    return "/".join(parts[-2:]) if parts[-1] in GENERIC and len(parts) > 1 else parts[-1]


def _role(area: str) -> str | None:
    if not area:
        return "Project settings and readme"
    name = area.rsplit("/", 1)[-1].lower()
    for names, role in ROLES:
        if name in names:
            return role
    return None


def _imports(repo: Path, files: list[tuple[str, str, int]]) -> tuple[list[tuple[str, str]], bool]:
    """Which file uses which, from import statements in JS, TS and Python files. Only
    imports of the project's own files count; packages from npm or pip are left out."""
    known = {path for path, _, _ in files}
    code = [(path, sha) for path, sha, size in files
            if PurePosixPath(path).suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".vue", ".svelte")
            and size <= MAX_BYTES]
    complete = len(code) <= MAX_SCANNED
    code = code[:MAX_SCANNED]
    if not code:
        return [], complete

    # From the working tree each "sha" is the file's path.
    contents = _read_blobs(repo, [sha for _, sha in code], worktree=all(sha == path for path, sha in code))
    python_roots = _python_roots(known)
    links: set[tuple[str, str]] = set()
    for (path, _), text in zip(code, contents):
        if path.endswith(".py"):
            targets = _python_targets(path, text, known, python_roots)
        else:
            targets = _js_targets(path, text, known)
        links.update((path, target) for target in targets if target != path)
    return sorted(links), complete


def _worktree_files(repo: Path) -> list[tuple[str, str, int]]:
    out = git.run(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    result = []
    for path in sorted(set(out.split("\x00"))):
        folders = path.split("/")[:-1]
        file = repo / path
        if not path or SKIP_DIRS.intersection(folders) or any(f.endswith(".egg-info") for f in folders) \
                or not file.is_file():
            continue
        result.append((path, path, file.stat().st_size))
    return result


def _read_blobs(repo: Path, shas: list[str], worktree: bool = False) -> list[str]:
    if worktree:
        return [(repo / path).read_text(encoding="utf-8", errors="replace") for path in shas]
    result = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, input=("\n".join(shas) + "\n").encode(),
                            capture_output=True, check=True)
    data, texts, at = result.stdout, [], 0
    for _ in shas:
        header_end = data.index(b"\n", at)
        size = int(data[at:header_end].split()[2])
        start = header_end + 1
        texts.append(data[start:start + size].decode("utf-8", "replace"))
        at = start + size + 1
    return texts


def _js_targets(path: str, text: str, known: set[str]) -> list[str]:
    folder = posixpath.dirname(path)
    targets = []
    for match in JS_IMPORT.finditer(text):
        spec = next(group for group in match.groups() if group)
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(folder, spec))
        elif spec.startswith(("@/", "~/")):
            base = posixpath.normpath(posixpath.join("src", spec[2:]))
        else:
            continue
        for extension in JS_EXTENSIONS:
            if base + extension in known:
                targets.append(base + extension)
                break
    return targets


def _python_roots(known: set[str]) -> list[str]:
    # Where absolute imports start: the top level, plus the folder holding each top-level
    # package, such as src/ or backend/src/ in a project with several apps.
    packages = {posixpath.dirname(path) for path in known if path.endswith("/__init__.py")}
    roots = {posixpath.dirname(package) for package in packages if posixpath.dirname(package) not in packages}
    return [""] + sorted(root + "/" for root in roots if root)


def _python_targets(path: str, text: str, known: set[str], roots: list[str]) -> list[str]:
    package = posixpath.dirname(path)
    targets = []
    for match in PY_IMPORT.finditer(text):
        source, names, plain = match.groups()
        modules = []
        if plain:
            modules = [plain]
        elif source is not None:
            dots = len(source) - len(source.lstrip("."))
            rest = source.lstrip(".")
            if dots:
                base = package
                for _ in range(dots - 1):
                    base = posixpath.dirname(base)
                prefix = "/".join(filter(None, [base, rest.replace(".", "/")]))
                candidates = [prefix] + [f"{prefix}/{name.strip()}" for name in re.split(r"[,\s()]+", names) if name.strip()]
                for candidate in candidates:
                    for option in (candidate + ".py", candidate + "/__init__.py"):
                        if option in known:
                            targets.append(option)
                            break
                continue
            modules = [rest] + [f"{rest}.{name.strip()}" for name in re.split(r"[,\s()]+", names) if name.strip()]
        for module in modules:
            relative = module.replace(".", "/")
            for root in roots:
                for option in (f"{root}{relative}.py", f"{root}{relative}/__init__.py"):
                    if option in known:
                        targets.append(option)
                        break
    return targets

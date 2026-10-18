# devdash-be

Shows what changed in your repos, in a way people who don't read code can follow.
The core works from your git history alone. Recording Claude's changes, with the
request behind each one, is an optional add-on per repo.

Built so far: repo tracking, the branch compare and its local API, and the optional
Claude recorder, a project summary, and plain-English explanations of changed
functions written by Claude Code on request.

This repo is the backend. The dashboard you look at is the UI, in its own repo:
[DevDash-FE](https://github.com/VardaanShukla/DevDash-FE).

## Install

Needs Python 3.10 or newer and git.

```sh
git clone https://github.com/VardaanShukla/DevDash-BE.git devdash-be
cd devdash-be
pipx install -e .          # or: python3 -m pip install -e .
```

## Run it with the UI

The backend and the UI run side by side: the API on port 8765, the page on port 5173.
The UI's dev server forwards every `/api` call to the API, so the browser only ever talks
to the page.

1. Start the API and leave it running:

   ```sh
   devdash serve
   ```

2. In a second terminal, get the UI (needs Node 18 or newer) and start it:

   ```sh
   git clone https://github.com/VardaanShukla/DevDash-FE.git devdash-ui
   cd devdash-ui
   npm install
   npm run dev
   ```

3. Open http://localhost:5173. The first visit asks you to pick a username and password,
   then to add a project: the path of any git folder on your computer.

To run a built copy instead of the dev server, use `npm run build` and then
`npm run preview`, which serves the page on http://localhost:4173 and forwards `/api` the
same way. The API only accepts the page from these two ports, so keep them as they are.

Optional: install [Claude Code](https://claude.com/claude-code) and sign in for the
plain-English explanations, and run `devdash connect claude <path>` to record Claude's
changes (see below).

## Track a repo

```sh
devdash init ~/Projects/SomeRepo
```

This only registers the repo. Nothing is installed: your own commits are already in git.

## See what a branch changed

```sh
devdash serve              # API on http://127.0.0.1:8765, interactive docs at /docs
```

`GET /api/repos/{id}/compare` returns what the checked-out branch changed since it left
`main` (or `master`), the same set of changes a pull request would show:

- the commits, each marked `ai` when it has an AI co-author line
- each file with its status, line counts and diff lines
- the functions and classes each file's change touches, marked added, deleted or modified

Options: `base` and `head` pick other branches; `uncommitted=true` adds edits and new files
not committed yet, so you can see work in progress, even on `main` itself.
Lockfiles and build output are counted but their lines are left out.

Other endpoints:

- `GET /api/repos` (tracked repos) and `GET /api/repos/{id}/branches`
- `POST /api/repos` with `{"path": "~/Projects/App", "ai": "claude" | null}` adds a project,
  the same as `devdash init` (plus `devdash connect claude` when `ai` is `"claude"`)
- `DELETE /api/repos/{id}` (with a `{}` JSON body) stops tracking a project: its files and
  git history stay, DevDash's git hook comes out, and what it recorded for it is deleted
- `PUT /api/repos/{id}/ai` with `{"ai": "claude" | null}` connects or disconnects Claude
- `GET /api/repos/{id}/setup` lists the setup steps and which are done
- `GET /api/ai-tools` lists the AI tools DevDash can record
- `GET /api/repos/{id}/state` is a fingerprint of branches, commits and (with
  `?uncommitted=true`) edits; the UI polls it and reloads only when it changes
- `GET /api/repos/{id}/flow` lists the newest commits across all branches with their
  parents and branch names, for drawing how branches fork and merge
- `GET /api/repos/{id}/map?head=branch` groups the branch's committed files into parts
  (folders), with each part's languages, a plain-words role guessed from its name, and
  which parts import code from which (JS, TS and Python imports of the project's own files)
- `GET /api/repos/{id}/functions?path=file` lists each function, method or type the branch
  changed in one file, before and after, with what differs: inputs, calls, decisions
  (ifs, loops, catches), size, its docstring or comment, and where else it is used.
  Python is read with its own parser; JS, TS and other brace languages with patterns
- `GET /api/repos/{id}/summary?head=branch` describes the whole project from its files and
  history: readme, package files, well-known tools it uses, commands, parts, people, activity
- `POST /api/repos/{id}/explain/function` (`{"path", "name"}`) and
  `POST /api/repos/{id}/explain/project` ask Claude Code for a plain-English explanation:
  what a function does, what the change improves and what it costs, or what the project is
  and how it works. DevDash runs your installed `claude -p` in an empty folder with tools
  off, so it uses your Claude login and needs no API key. It asks Haiku, the small and
  cheap model, with a short system prompt of its own; set `DEVDASH_CLAUDE_MODEL` (such as
  `sonnet`) for another. Answers are saved and reused. A 424 means Claude Code isn't signed in.
  `GET /api/ai` says whether `claude` is installed and signed in, and which model it uses
- `POST /api/ai/sign-in` signs in to Claude Code from the page: it runs `claude auth login`,
  which opens the sign-in page in the browser, and returns that page's address.
  `POST /api/ai/sign-in/code` (`{"code"}`) passes on the code the page shows when it can't
  finish by itself; `GET /api/ai/sign-in` says whether it's waiting, signed in or failed
- `GET /api/home` lists every project at a glance: its branch, how many commits it is ahead
  of the base, files not committed, the latest commit, commits this week and branch count
- `GET /api/digest?day=YYYY-MM-DD` (today when left out) is the daily digest: what Claude
  changed that day, prompt by prompt and newest first, with the files and functions each
  prompt touched, whether it's committed yet, and how much you changed yourself. Plain rules
  flag changes worth a careful look: sign-in or permissions, payments, database structure,
  removed tests and anything that looks like a key or password; setup files, deleted files
  and very big changes are flagged as worth knowing. Only projects connected to Claude are
  recorded, so only they appear
- `POST /api/digest/undo` (`{"change": "<a digest change's id>"}`) undoes one of Claude's
  changes in your files by applying its recorded diffs in reverse with `git apply`: every
  file or none, and nothing is committed, so the undo is an ordinary change to review and
  commit. A 409 means a file changed since and the change no longer fits; nothing is
  touched then. `POST /api/digest/put-back` reapplies an undone change
- `GET /api/todos`, `POST /api/todos` (`{"text", "repo_id", "branch"}`),
  `PATCH /api/todos/{id}` (`{"text"}` or `{"done"}`) and `DELETE /api/todos/{id}` keep a
  to-do list; each item is for everywhere, one project, or one branch of it
- `GET /api/calendar?days=7` lists events from the calendar connected with
  `PUT /api/calendar` (`{"url"}`: its private iCal address, such as Google Calendar's
  "Secret address in iCal format"); `DELETE /api/calendar` forgets it. Read-only, fetched at
  most every 5 minutes; repeating events are expanded for daily, weekly, monthly-on-a-date
  and yearly rules. The address stays in DevDash's database and is never sent to the page
- `POST /api/repos/{id}/sync` runs `git fetch --all --prune` so merges and branches
  deleted on GitHub show up

The API listens on 127.0.0.1 only, answers only requests addressed to localhost, and
accepts changes only as JSON from no page or the DevDash UI. The full contract is at
`/openapi.json`.

### Username and password

The first time the dashboard opens it asks you to pick a username and a password (8
characters or more). Usernames ignore case. A password set before usernames existed keeps
the username of its first sign-in.
After that every route except the ones below needs a signed-in browser, which holds a
random token in an HttpOnly cookie for 30 days. The password is stored as a scrypt hash;
five wrong tries in a minute pause signing in for that minute.

- `GET /api/auth` says whether a password is set and whether this browser is signed in
- `POST /api/auth/login` (`{"username", "password"}`) signs in; `POST /api/auth/logout` signs out
- `PUT /api/auth/password` (`{"username", "password", "current"}`) sets the first username
  and password, or changes them from a signed-in browser; every other browser is signed out

Forgot it? `devdash password` in a terminal sets a new password (and username, if you type
one) without the old one.

Function names come from git's own function detection, extended to JavaScript and
TypeScript. Methods inside a class in JS or TS are reported under the class.

## Optional: record Claude's changes

```sh
devdash connect claude ~/Projects/SomeRepo      # start
devdash disconnect claude ~/Projects/SomeRepo   # stop; recorded history is kept
```

`connect claude` installs:

- a git `post-commit` hook in that repo (added to any existing shell hook, not replacing it)
- Claude Code hooks in your user settings, `~/.claude/settings.json`
  (`UserPromptSubmit`, `PreToolUse`, `Stop`). Nothing is added to the repo itself.

The Claude hooks run in every Claude Code session, wherever you start it: inside the repo,
or in a parent folder holding several repos. An edit is recorded only when the edited file
is inside a connected repo. A session started outside your connected repos that edits
nothing in one leaves no trace. Restart any Claude Code session that is already open.

## Claude app chats

Edits made from a Claude app chat reach your files without going through Claude Code,
so the hooks can't see them. DevDash records them from git instead:

1. Run `devdash instructions` and paste its text into the Claude project's instructions.
2. Claude then writes your request to `.devdash/prompt.md` before each commit and adds
   a `Co-Authored-By: Claude` line to the message.
3. The post-commit hook stores that request with the commit's diff as an AI change and
   clears the file. `.devdash/` is listed in `.git/info/exclude`, so it is never pushed.

A commit with an AI co-author line but no prompt file is still recorded as AI, without a
prompt. Uncommitted app edits are not visible until they are committed.

## See what was recorded

```sh
devdash turns            # latest prompts that changed files, and the commit they landed in
devdash turns --all      # include prompts that changed nothing
```

Data lives in `~/.devdash/devdash.db` (set `DEVDASH_HOME` to move it).
Hook errors never interrupt Claude Code or git; they go to `~/.devdash/errors.log`.
Every hook call is logged (tool and file, never the prompt) to `~/.devdash/hooks.log`.

## How a change is recorded

1. `UserPromptSubmit` starts a turn and saves the prompt and branch.
2. `PreToolUse` saves each file's content just before Claude first edits it in that turn.
3. `Stop` diffs those files against their saved content. Your own edits to other files,
   or edits made before the turn, are left out.
4. `post-commit` links the turn's changes to the commit. Files in the commit that Claude
   didn't touch are recorded as human changes.

Lockfiles, `node_modules`, build output and files over 1 MB are skipped.

## Tests

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest
```

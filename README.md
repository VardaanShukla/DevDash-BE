# devdash-be

Shows what changed in your repos, in a way people who don't read code can follow.
The core works from your git history alone. Recording Claude's changes, with the
request behind each one, is an optional add-on per repo.

Built so far: repo tracking, the branch compare and its local API, and the optional
Claude recorder, a project summary, and plain-English explanations of changed
functions written by Claude Code on request.

This repo is the backend, and it serves the dashboard page too. The page's source is in
its own repo, [DevDash-FE](https://github.com/VardaanShukla/DevDash-FE); a built copy comes
with this package.

## Install and run

DevDash needs Python 3.10 or newer, git, and [pipx](https://pipx.pypa.io) (which installs
Python apps in their own space). Node is not needed: the dashboard page comes built in.

### Windows

1. **Check what you have.** In Command Prompt or PowerShell:

   ```bat
   py --version
   git --version
   ```

   If `py` isn't found or says older than 3.10, install [Python](https://www.python.org/downloads/)
   (tick "Add python.exe to PATH"). If `git` isn't found, install
   [Git for Windows](https://git-scm.com/download/win): DevDash's git hook runs in its `sh`.
2. **Install pipx** and put its folder on your PATH:

   ```bat
   py -m pip install --user pipx
   py -m pipx ensurepath
   ```

3. **Install DevDash**, from GitHub or from a folder you cloned it into:

   ```bat
   py -m pipx install --force git+https://github.com/VardaanShukla/DevDash-BE.git
   py -m pipx install --force C:\path\to\DevDash-BE
   ```

4. **Close every terminal and open a new one** (so it sees the new PATH), then start it:

   ```bat
   devdash serve
   ```

5. Open http://127.0.0.1:8765 in your browser on the same computer.

### Linux

1. **Check what you have:** `python3 --version` (3.10 or newer) and `git --version`.
   Install what's missing with your package manager, e.g. `sudo apt install python3 python3-venv git pipx`.
2. **Install pipx**, if your package manager didn't: `python3 -m pip install --user pipx`, then
   `python3 -m pipx ensurepath`.
3. **Install DevDash:**

   ```sh
   pipx install --force git+https://github.com/VardaanShukla/DevDash-BE.git
   # or from a clone:
   pipx install --force /path/to/DevDash-BE
   ```

4. **Open a new terminal** and run `devdash serve`.
5. Open http://127.0.0.1:8765.

### macOS

1. **Check what you have:** `python3 --version` and `git --version`. Missing ones come from
   [python.org](https://www.python.org/downloads/) or `brew install python git`; running
   `git --version` the first time also offers Apple's command line tools, which include git.
2. **Install pipx:** `brew install pipx` (or `python3 -m pip install --user pipx`), then `pipx ensurepath`.
3. **Install DevDash:** `pipx install --force git+https://github.com/VardaanShukla/DevDash-BE.git`
   (or `pipx install --force /path/to/DevDash-BE` from a clone).
4. **Open a new terminal** and run `devdash serve`.
5. Open http://127.0.0.1:8765.

### First run

The first visit asks you to pick a username and password, then to add a project: the path of
any git folder on your computer, typed or pasted any way (`C:\Users\me\App`, `"C:\Users\me\App"`
from Windows' "Copy as path", `~/App`, `$HOME/App` or `%USERPROFILE%\App`). A project with no
readme, or nothing committed yet, works too. To pick from a list instead, point DevDash at
the folder that holds your projects once: `devdash init ~/Projects` remembers it, adds nothing
and changes nothing in them, and Add project then lists the projects inside.

Optional: install [Claude Code](https://claude.com/claude-code) (or Codex, Ollama or
Antigravity) and sign in for the plain-English explanations, and pick your AI coding tool when
adding a project to record each request it worked on (see below).

### Update to the latest version

Run the same install command again: `pipx install --force <GitHub URL or folder>`
(Windows: `py -m pipx install --force …`). Then stop `devdash serve` (Ctrl+C) and start it
again: it points the hooks at the new install by itself.

### If something goes wrong

- **`devdash` isn't found.** Open a new terminal first. Still not found: run `pipx ensurepath`
  (Windows: `py -m pipx ensurepath`) and open another one. Or run it by its full path:
  `%USERPROFILE%\.local\bin\devdash.exe serve` in Command Prompt,
  `& "$env:USERPROFILE\.local\bin\devdash.exe" serve` in PowerShell, or
  `~/.local/bin/devdash serve` on Linux and macOS. `python -m devdash serve` also works from
  the environment it's installed in.
- **You'd rather use a virtual environment** (venv or conda) than pipx: activate it, run
  `pip install -e .` in the DevDash-BE folder, then `devdash serve`. The command then works
  only while that environment is active, and the hooks DevDash adds call that environment's
  Python.
- **Hooks point at a deleted environment.** If you delete that environment (or move the
  install) without uninstalling DevDash first, git commits still work, but Claude Code reports
  a failing hook. Install DevDash again and start `devdash serve` once: it points every hook at
  the new install. Or remove them as in "Uninstall DevDash safely".
- **A prompt is missing from AI changes.** Settings → Recording shows when each hook last ran
  and any hook errors, with the log files' paths. Only tools that run DevDash's hooks are
  recorded: Claude Code (in a terminal, an editor extension or the desktop app's Code tab),
  Codex and Antigravity. Claude's Cowork mode and plain chats don't run them.

Projects can live anywhere and can move. A project that is moved, renamed or cloned again
somewhere else keeps its history: add the new folder (or just commit in it, if an AI tool is
connected) and DevDash finds it is the same project, as long as the old folder is gone.

### Working on the page

The page's source is in its own repo, [DevDash-FE](https://github.com/VardaanShukla/DevDash-FE).
To work on it with live reload, keep `devdash serve` running and start the UI's dev server
(`npm install` then `npm run dev` in DevDash-FE), which serves the page on
http://localhost:5173 and forwards `/api` to the API. To ship a change, build it into this
package and commit `src/devdash/web`:

```sh
python scripts/build_ui.py ../DevDash-FE
```

## Track a repo

```sh
devdash init ~/Projects/SomeRepo
```

This only registers the repo. Nothing is installed: your own commits are already in git.

## See what a branch changed

```sh
devdash serve              # page and API on http://127.0.0.1:8765, API docs at /docs
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
  The day's totals leave out undone changes (`undone` counts them) and count a prompt
  that edited several projects once; `watched_files` counts files a prompt changed through
  a shell command rather than its edit tool, which may include your own edits made meanwhile
- `GET /api/recording/health` says whether recording works: when each hook last ran, recent
  errors, and `problems` in plain sentences (hooks missing or naming an old install, prompts
  whose end never arrived). `POST /api/recording/repair` reinstalls the hooks
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
  (`UserPromptSubmit`, `PreToolUse`, `PostToolUse` for Bash, `Stop`, `SessionEnd`). Nothing
  is added to the repo itself.

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
3. `PostToolUse` notes the files each shell command changed, so they still count if you
   press Esc (an interrupted prompt gets no `Stop`).
4. `Stop` (or `SessionEnd`, when you quit or `/clear` mid-prompt) diffs those files against
   their saved content, plus any other file that changed while the prompt ran. Your own
   edits made before the turn are left out, and a file another Claude session changed with
   its own edit tool is left to that session, so nothing is counted twice.
5. `post-commit` links the turn's changes to the commit. Files in the commit that Claude
   didn't touch are recorded as human changes.

Lockfiles, `node_modules`, build output and files over 1 MB are skipped.

## Uninstall DevDash safely

DevDash never changes your project files or their git history. What it adds, and takes out
again when a project stops recording:

- In each project that records an AI tool: two lines at the end of `.git/hooks/post-commit`
  (a `# devdash: records commits` line and the command under it). Anything else in that
  hook is yours and stays; the file is deleted only if DevDash made it. Also a
  `/.devdash/` line in `.git/info/exclude`, so a Claude app chat's notes there stay out of
  git. That line is harmless and stays.
- For the AI tool a project records, while any project records it:
  - Claude Code: five entries in `~/.claude/settings.json`, under `UserPromptSubmit`,
    `PreToolUse`, `PostToolUse`, `Stop` and `SessionEnd`, each running `… -m devdash record …`. Your other settings and
    hooks stay.
  - Codex: `UserPromptSubmit`, `PreToolUse` and `Stop` in `~/.codex/hooks.json` (the file goes if only DevDash's were in it).
  - Antigravity: a `devdash` entry in `~/.gemini/config/hooks.json`.
- Its own data in `~/.devdash` (`%USERPROFILE%\.devdash` on Windows): the projects list,
  what was recorded, settings and the dashboard password.

Nothing starts DevDash by itself: it runs only while `devdash serve` does.

1. **Stop recording in every project.** In the dashboard, open each project and choose
   "Remove project from DevDash" (or "Stop recording AI changes"). Or in a terminal, for
   each project that records an AI tool:

   ```sh
   devdash disconnect claude /path/to/project
   ```

   This takes DevDash's lines out of that project's git hook, and once no project records
   an AI tool any more, its hooks out of `~/.claude`, `~/.codex` or `~/.gemini`. Do this
   first: after uninstalling, the `devdash` command isn't there to do it.
2. **Stop DevDash:** Ctrl+C in the terminal running `devdash serve`.
3. **Uninstall it:**

   ```sh
   pipx uninstall devdash
   ```

   or, if you installed it into a virtual environment, activate it and run
   `pip uninstall devdash`. On Windows: `py -m pipx uninstall devdash`.
4. **Optional, and permanent: delete DevDash's data.** Keep a copy first. Linux and macOS:

   ```sh
   cd ~ && tar -czf devdash-backup.tar.gz .devdash
   rm -rf ~/.devdash
   ```

   Windows (PowerShell):

   ```powershell
   Compress-Archive -Path "$env:USERPROFILE\.devdash" -DestinationPath "$env:USERPROFILE\devdash-backup.zip"
   Remove-Item -Recurse -Force "$env:USERPROFILE\.devdash"
   ```

   Windows (Command Prompt):

   ```bat
   tar -czf "%USERPROFILE%\devdash-backup.tar.gz" -C "%USERPROFILE%" .devdash
   rmdir /s /q "%USERPROFILE%\.devdash"
   ```

If you uninstalled before step 1, the hooks are left calling a DevDash that isn't there. Git
commits still work (the hook ends in `|| true`), but Claude Code reports a failing hook. Take
them out by hand: delete DevDash's two lines from each project's `.git/hooks/post-commit`,
and in `~/.claude/settings.json` (and `~/.codex/hooks.json`) delete each hook whose command
contains `devdash record`, leaving the others. Or install DevDash again, do step 1, and
uninstall.

## Tests

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest
```

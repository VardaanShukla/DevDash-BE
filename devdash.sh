#!/usr/bin/env bash
# Installs, repairs, uninstalls or starts DevDash on Linux and macOS, from the DevDash-BE
# and DevDash-FE folders next to this Scripts folder. Double-click it, or run:
#
#     bash Scripts/devdash.sh
#
# Installing never touches your projects. Uninstalling deletes DevDash's data, after you
# type DELETE to confirm.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
BE="$ROOT/DevDash-BE"
FE="$ROOT/DevDash-FE"
DATA="${DEVDASH_HOME:-$HOME/.devdash}"
URL="http://127.0.0.1:8765"
# Where pipx puts the devdash command; a fresh pipx may not have it on PATH yet.
export PATH="$HOME/.local/bin:$PATH"

# Opened with a double-click and no terminal: open one and run there.
if [ ! -t 0 ] && [ -z "${DEVDASH_IN_TERMINAL:-}" ] && { [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || [ "$(uname)" = Darwin ]; }; then
  export DEVDASH_IN_TERMINAL=1
  self="$HERE/$(basename "${BASH_SOURCE[0]}")"
  if [ "$(uname)" = Darwin ]; then exec open -a Terminal "$self"; fi
  for term in x-terminal-emulator gnome-terminal konsole xfce4-terminal xterm; do
    command -v "$term" >/dev/null || continue
    case "$term" in
      gnome-terminal) exec gnome-terminal -- bash "$self" ;;
      xfce4-terminal) exec xfce4-terminal -x bash "$self" ;;
      *) exec "$term" -e bash "$self" ;;
    esac
  done
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
yes_no() { # yes_no "Question" default(y|n)
  local answer hint="[y/N]"
  [ "$2" = y ] && hint="[Y/n]"
  read -r -p "$1 $hint " answer
  answer="${answer:-$2}"
  [[ "$answer" =~ ^[Yy] ]]
}
# A window opened by double-click closes when the script ends: keep it until Enter.
finish() {
  local code=$?
  if [ -n "${DEVDASH_IN_TERMINAL:-}" ]; then read -r -p $'\nPress Enter to close. ' _ || true; fi
  exit "$code"
}
trap finish EXIT

installed() { command -v pipx >/dev/null && pipx list --short 2>/dev/null | grep -q '^devdash '; }

# Installs system packages with the computer's package manager, after asking: it takes
# your password (sudo), except with Homebrew.
system_install() { # system_install what-for apt-names dnf-names pacman-names zypper-names brew-names
  local why="$1" manager names
  if command -v apt-get >/dev/null; then manager=apt; names="$2"
  elif command -v dnf >/dev/null; then manager=dnf; names="$3"
  elif command -v pacman >/dev/null; then manager=pacman; names="$4"
  elif command -v zypper >/dev/null; then manager=zypper; names="$5"
  elif command -v brew >/dev/null; then manager=brew; names="$6"
  else fail "$why isn't installed, and no package manager was found. Install it yourself, then run this again."
  fi
  say "$why isn't installed."
  echo "This runs: $([ "$manager" = brew ] || echo "sudo ")$manager install $names"
  [ "$manager" = brew ] || echo "Your password goes to sudo; this script never sees it."
  yes_no "Install it now?" y || fail "Install $why, then run this again."
  # $names is a list of package names: split on purpose.
  # shellcheck disable=SC2086
  case "$manager" in
    apt) sudo apt-get update && sudo apt-get install -y $names ;;
    dnf) sudo dnf install -y $names ;;
    pacman) sudo pacman -S --needed --noconfirm $names ;;
    zypper) sudo zypper install -y $names ;;
    brew) brew install $names ;;
  esac
}

needs() {
  [ -d "$BE" ] || fail "No DevDash-BE folder next to Scripts (looked in $ROOT)."
  if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    system_install "Python 3.10 or newer" "python3 python3-venv" python3 python python3 python
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "DevDash needs Python 3.10 or newer; this one is $(python3 --version)."
  fi
  if ! command -v pipx >/dev/null; then
    system_install "pipx" pipx pipx python-pipx python3-pipx pipx
    pipx ensurepath >/dev/null || true
  fi
  # The page is built from DevDash-FE when it's here; without it, the page already built
  # into DevDash-BE is used, and Node isn't needed.
  if [ -d "$FE" ]; then
    if ! command -v npm >/dev/null; then
      system_install "Node.js" "nodejs npm" "nodejs npm" "nodejs npm" "nodejs npm" node
    fi
    local major
    major="$(node -p 'process.versions.node.split(".")[0]')"
    [ "$major" -ge 18 ] || fail "Building the page needs Node 18 or newer; this computer has $(node --version). Get a newer one from https://nodejs.org, then run this again."
  elif [ ! -f "$BE/src/devdash/web/index.html" ]; then
    fail "No DevDash-FE folder next to Scripts, and no page built into DevDash-BE."
  fi
}

build_page() {
  [ -d "$FE" ] || return 0
  say "Building the page from DevDash-FE…"
  python3 "$BE/scripts/build_ui.py" "$FE"
}

after_install() {
  say "DevDash is installed. Your data is in $DATA."
  local folder
  read -r -p $'\nWhere are your projects? DevDash lists the git repos in that folder. [~/Projects, or Enter to skip] ' folder
  folder="${folder/#\~/$HOME}"
  [ -z "$folder" ] && [ -d "$HOME/Projects" ] && folder="$HOME/Projects"
  if [ -n "$folder" ]; then
    if [ -d "$folder" ]; then devdash init "$folder" || true
    else echo "There's no folder $folder. Add projects later under Add project on the dashboard."
    fi
  fi
  if yes_no $'\nStart DevDash now?' y; then start; fi
}

do_install() {
  if installed; then
    say "DevDash is already installed. Choose Repair to reinstall it."
    return
  fi
  needs
  build_page
  say "Installing DevDash…"
  pipx install "$BE"
  after_install
}

# Reinstalls over whatever is there, broken or not. Your data stays.
repair() {
  needs
  build_page
  say "Reinstalling DevDash…"
  stop_running
  pipx install --force "$BE"
  after_install
}

stop_running() {
  local pids
  pids="$(pgrep -f 'devdash serve' || true)"
  [ -z "$pids" ] && return
  echo "Stopping the DevDash that's running…"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
}

uninstall() {
  say "Uninstall DevDash and delete its data?"
  echo "This deletes:"
  echo "  - the devdash program"
  echo "  - everything in $DATA: your DevDash account, the project list, recorded AI history and explanations"
  echo "  - DevDash's hooks in your projects and in Claude Code, Codex and Antigravity"
  echo "It doesn't touch your projects' files or git history, Ollama, or its models."
  local answer
  read -r -p $'\nType DELETE to continue, or press Enter to cancel: ' answer
  [ "$answer" = DELETE ] || { echo "Cancelled. Nothing was changed."; return; }

  stop_running
  if command -v devdash >/dev/null; then devdash remove-hooks || echo "Couldn't take every hook out; carrying on."; fi
  if installed; then pipx uninstall devdash; fi
  # Never anything but DevDash's own folder.
  if [ -n "$DATA" ] && [ "$DATA" != "$HOME" ] && [ "$DATA" != / ] && [ -d "$DATA" ]; then
    rm -rf -- "$DATA"
  fi
  say "DevDash is uninstalled and its data deleted."
}

start() {
  command -v devdash >/dev/null || fail "DevDash isn't installed. Choose Install first."
  if pgrep -f 'devdash serve' >/dev/null; then
    echo "DevDash is already running."
  else
    devdash serve &
    local server=$!
    trap 'kill "$server" 2>/dev/null' INT TERM
    sleep 2
  fi
  if command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 || true
  elif command -v open >/dev/null; then open "$URL"
  fi
  if [ -n "${server:-}" ]; then
    echo "Opened $URL. Keep this window open while you use DevDash; press Ctrl+C to stop it."
    wait "$server" || true
  fi
}

say "DevDash"
if installed; then echo "Installed. Data in $DATA."; else echo "Not installed yet."; fi
cat <<'MENU'

  1  Install      first time on this computer
  2  Repair       reinstall over the current one; your data stays
  3  Uninstall    remove DevDash and delete its data
  4  Start        run DevDash and open it in the browser
  5  Quit
MENU
read -r -p $'\nChoose 1-5: ' choice
case "$choice" in
  1) do_install ;;
  2) repair ;;
  3) uninstall ;;
  4) start ;;
  *) echo "Nothing changed." ;;
esac

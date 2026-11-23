#!/usr/bin/env bash
# Local capture only. The detached worker has its own lock and lifetime limit.
# Fast path: the observer acts only in a project folder this machine enabled (observer.py enable --model M --yes).
# Its settings are kept in observer-approvals.json in the private folder beside the user key, under the folder's
# real path in quotes. Without that path in the file no Python starts, and the hook costs a shell and a grep. A match,
# or a state folder the shell cannot place, only means that Python decides; it reads nothing else into the shell.
src="${BASH_SOURCE[0]}"
case "$src" in /*) ;; *) src="$PWD/$src" ;; esac
state="${XDG_STATE_HOME:-}"
case "$state" in /*) ;; *) case "${HOME:-}" in /*) state="$HOME/.local/state" ;; *) state="" ;; esac ;; esac
if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -n "$state" ] && command -v grep >/dev/null 2>&1; then
  cd -P "$CLAUDE_PROJECT_DIR" 2>/dev/null || exit 0
  grep -qsF -e "\"$PWD\"" -- "$state"/recortex/private/observer-approvals.json || exit 0
  # The project file switches the observer off ("observer": false): then only the check before a Bash call starts
  # Python, since holds stay in force while this folder has an entry. A file named by the plugin's variable or
  # another spelling is left to Python. The match is plain text, so no other entry of the project file may be named
  # "observer".
  if [ "${1:-}" != "pre-tool" ] && [ -f .recortex.json ]; then
    named=no
    [ -n "${RECORTEX_CONFIG:-}" ] && named=yes
    if [ "$named" = no ] && grep -qsE '"observer"[[:space:]]*:[[:space:]]*false' -- .recortex.json; then exit 0; fi
  fi
fi
here="$(cd "$(dirname "$src")" 2>/dev/null && pwd)" || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null || exit 0
if command -v timeout >/dev/null 2>&1; then
  timeout 4 python3 "$here/observer.py" hook "${1:-}" 2>/dev/null
else
  python3 "$here/observer.py" hook "${1:-}" 2>/dev/null
fi
exit 0

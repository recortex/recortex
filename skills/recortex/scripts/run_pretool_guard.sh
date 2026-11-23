#!/usr/bin/env bash
# The PreToolUse check that holds in every project, observer on or off (pretool_guard.py). It costs a shell and one
# `case` on the input; Python starts only for a call that names the ask agent or the observer. For the ask agent it
# fails closed: when Python cannot decide (missing, too slow), the command is stopped. For any other call it never
# blocks anything: it prints nothing and exits 0.
input="$(cat)" || exit 0
case "$input" in
  *recortex:ask* | *observer* | *setup.py* | *publish.py* | *acts.py* | *mind.html*) ;;
  *) exit 0 ;;
esac
src="${BASH_SOURCE[0]}"
case "$src" in /*) ;; *) src="$PWD/$src" ;; esac
here="$(cd "$(dirname "$src")" 2>/dev/null && pwd)"
out=""
if [ -n "$here" ] && command -v python3 >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then
    out="$(printf '%s' "$input" | timeout 4 python3 "$here/pretool_guard.py" 2>/dev/null)"
  else
    out="$(printf '%s' "$input" | python3 "$here/pretool_guard.py" 2>/dev/null)"
  fi
  status=$?
else
  status=1
fi
if [ "$status" -ne 0 ]; then
  case "$input" in
    *recortex:ask*)
      printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "recortex: the ask agent only reads the record, and this command could not be checked, so it is stopped. Run python3 index.py query find, recall, timeline, tasks or plan, and nothing else."}}'
      ;;
  esac
  exit 0
fi
[ -n "$out" ] && printf '%s' "$out"
exit 0

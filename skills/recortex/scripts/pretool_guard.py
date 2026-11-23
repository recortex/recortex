#!/usr/bin/env python3
"""The PreToolUse check that holds in every project, whether or not the observer is on (run_pretool_guard.sh starts it
only for a Bash call whose input mentions the ask agent or the observer).

1. The ask agent (`agent_type` "recortex:ask", which Claude Code puts in the input of a subagent's tool call) is read-only
   because this check stops every Bash command of its that is not exactly
   `python3 <this scripts folder>/index.py query (find|recall|timeline|tasks|plan) ...` with no shell operator. The check
   fails closed: an input it cannot read, for that agent, is a denial. Any other input is left alone.
2. A Bash call that runs observer.py or setup.py through a pseudo-terminal helper (script, unbuffer, expect, ...) is denied:
   a terminal that a session makes is not the user's.
   A Bash call of the user's own commands (observer.py trust-ancestor, advisory, disable, release, resume, undo,
   `--hosts any`, publish.py) is denied too, in every project (USERS_ONLY).
3. A Bash call that turns the observer on or changes its level or its stronger model (`observer.py enable`, `setup.py
   observer`, `setup.py apply`): when the user answered the session's question about it, a one-time grant is recorded
   (observer_grants) for the level or the model the user chose. Nothing is printed for it.

Only the Python standard library; the observer's own modules are imported only for the second job."""

import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ASK = "recortex:ask"
QUERIES = ("find", "recall", "timeline", "tasks", "plan")
# What may not appear in the command at all: a shell operator, a substitution, a line break or a control character. The
# check reads the command as written, so an operator inside quotes is refused too; the agent asks again without it.
OPERATORS = re.compile(r"[;&|<>`$\\(){}\x00-\x1f\x7f]")
DENIAL = ("recortex: the ask agent only reads the record. Run exactly one command, python3 %s/index.py query find, recall, "
          "timeline, tasks or plan with its options (--words, --issue, --json), with no cd, no &&, no pipe, no redirect "
          "and no other command.")
UNREADABLE = ("recortex: the ask agent only reads the record, and this command could not be checked, so it is stopped. "
              "Run python3 index.py query find, recall, timeline, tasks or plan, and nothing else.")


# A program that gives a command a terminal of its own, or hides where it runs from, which a session can use to look like
# the user's: one list, so that the check and its tests name the same helpers. A name counts at any word boundary (after a
# space, a quote, an operator, a dot or a slash, and before a bracket as well), but not inside a longer word or after a
# dash: `scripts/` and `--script` are not helpers.
PTY_HELPERS = ("script", "unbuffer", "expect", "socat", "faketty", "pty", "openpty", "forkpty", "zpty", "tmux", "screen",
               "ssh", "setsid", "nohup")
PTY_HELPER = re.compile(r"(?<![A-Za-z0-9_-])(?:%s)(?![A-Za-z0-9_-])" % "|".join(PTY_HELPERS), re.I)
USERS_COMMAND = re.compile(r"(?:observer|setup)\.py")
OBSERVER_UNDER_CLAUDE = re.compile(r"CLAUDECODE")   # a command that names this variable and observer.py or setup.py is working round the check
# The user's own commands: a Bash call of the main session or of a subagent that names one of these (or `--hosts any`, or
# publish.py) is stopped in every project, observer on or off, because what they do is the user's own decision. The list
# is written out here, since this file does not import observer_hooks; a test holds it equal to the terminal rows of
# observer_hooks.COMMANDS (TERMINAL_ONLY).
USERS_ONLY = ("advisory", "disable", "release", "resume", "trust-ancestor", "undo")
# re.S: a command may run over several lines (a backslash and a line break, or a quote that is never closed). The option is
# matched with its prefixes too (--ho, --hos, --host), which argparse would read as --hosts; the parsers of observer.py and
# setup.py no longer accept an abbreviation (allow_abbrev=False), and this holds for a command written for an older version.
USERS_COMMAND_WORDS = re.compile(r"observer\.py\b.*?(?<![A-Za-z0-9_-])(?:%s)(?![A-Za-z0-9_-])" % "|".join(USERS_ONLY), re.S)
HOSTS_ANY = re.compile(r"(?:observer|setup)\.py\b.*--ho(?:s(?:ts?)?)?(?:\s+|=)[\"']?any\b", re.S)
PUBLISH = re.compile(r"(?<![A-Za-z0-9_-])publish\.py\b")
USERS_DENIAL = ("recortex: this command is the user's own. Ask the user to run it in their own terminal, outside Claude Code; "
                "a session never runs it.")
TERMINAL_DENIAL = ("recortex: observer.py and setup.py are not run through a program that makes a terminal or hides where "
                   "they run (script, unbuffer, expect, socat, faketty, pty, tmux, screen, ssh, setsid, nohup): what they ask "
                   "for is the user's own answer. Ask the user to run the command in their own terminal, outside Claude Code.")


def terminal_decision(data):
    """The denial for a Bash call that runs observer.py or setup.py through a pseudo-terminal helper, or None."""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") in ("Bash", "bash") and isinstance(command, str) and USERS_COMMAND.search(command):
        if PTY_HELPER.search(command) or OBSERVER_UNDER_CLAUDE.search(command):   # observer.py or setup.py is named: USERS_COMMAND
            return deny(TERMINAL_DENIAL)
    return None


def users_only_decision(data):
    """The denial for a Bash call (of the main session or of a subagent) that names `observer.py` followed by one of
    USERS_ONLY, or `observer.py` or `setup.py` with `--hosts any`, or `publish.py`; else None. The command is read as the
    shell splits it and as written. A guard against mistakes: a session with shell access can get round it."""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str):
        return None
    try:
        spoken = " ".join(shlex.split(command))
    except ValueError:
        spoken = command
    for text in (spoken, command):
        if USERS_COMMAND_WORDS.search(text) or HOSTS_ANY.search(text) or PUBLISH.search(text):
            return deny(USERS_DENIAL)
    return None


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def allowed_command(command, scripts=HERE):
    """True when `command` is exactly one `python3 <scripts>/index.py query <read> ...` and nothing else, with the scripts
    folder's own absolute path."""
    if not isinstance(command, str) or not command.strip() or len(command) > 4000 or OPERATORS.search(command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 4 or words[0] != "python3" or words[2] != "query" or words[3] not in QUERIES:
        return False
    # The path is the plugin's own, written out in full: a relative one would be resolved against the hook's folder and
    # not the one the command runs in, and a link elsewhere would run another file. So it must be the same string, and
    # its real path must still be the plugin's real index.py.
    index = os.path.join(scripts, "index.py")
    try:
        return os.path.isabs(words[1]) and words[1] == index and os.path.realpath(words[1]) == os.path.realpath(index)
    except (OSError, ValueError):
        return False


def ask_decision(data, scripts=HERE):
    """The denial for the ask agent's call `data`, or None when the call may go on."""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") in ("Bash", "bash") and allowed_command(command, scripts):
        return None
    return deny(DENIAL % scripts)


def decide(raw, scripts=HERE):
    """The hook's output (a dict, or None for nothing) for the raw input text."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        # Unreadable input: it is the ask agent's only when the text says so, and then it is stopped.
        return deny(UNREADABLE) if ASK in raw else None
    if data.get("agent_type") == ASK:
        return ask_decision(data, scripts)
    found = terminal_decision(data) or users_only_decision(data)
    if found:
        return found
    record_enable_grant(data)
    return None


def record_enable_grant(data):
    """Job 2: never raises and never prints; without the user's answer nothing is recorded, and a call made by a subagent
    (its input carries an agent_id or an agent_type) records nothing: only the main session's own call can."""
    if data.get("agent_id") or data.get("agent_type"):
        return
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str) or "observer" not in command:
        return
    try:
        import observer_grants
        observer_grants.record_from_hook(data)
    except Exception:  # noqa: BLE001 - a hook check must not break the session; the command is then refused for want of a grant
        pass


def main():
    raw = sys.stdin.read(8388609)
    out = decide(raw)
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The PreToolUse check that holds in every project, whether or not the observer is on (run_pretool_guard.sh starts it
only for a Bash call whose input mentions the ask agent or the observer).

1. The ask agent (`agent_type` "recortex:ask", which Claude Code puts in the input of a subagent's tool call) is read-only
   because this check stops every Bash command of its that is not exactly
   `python3 <this scripts folder>/index.py query (find|recall|timeline|tasks|plan) ...` with no shell operator. The check
   fails closed: an input it cannot read, for that agent, is a denial. Any other input is left alone.
2. A Bash call that runs observer.py, setup.py, publish.py or acts.py through a pseudo-terminal helper (script, unbuffer,
   expect, ...) is denied: a terminal that a session makes is not the user's. A Bash call of a subagent that performs an act
   the user answers a question for (observer.py trust-ancestor, advisory, disable, release, resume, undo, `--hosts any`,
   publish.py, ASKED) is denied too, in every project: asking and acting is the main session's, on the user's answer. A Bash
   call of any session that names a built dashboard page (mind.html) together with git, gh, curl, scp, rsync, sftp or wget is
   denied: a page leaves the machine only through publish.py, which checks it first.
3. A Bash call of the main session that turns the observer on or changes its level or its stronger model (`observer.py
   enable`, `setup.py observer`, `setup.py apply`), or performs one of the acts of acts.py: when the user answered the
   plugin's own question about it (the session asked it from `acts.py ask`, or the user typed an option's words), a one-time
   grant is recorded (observer_grants) for the level, the model or the act the user chose. Nothing is printed for it. The act
   itself takes the grant once, or refuses and names the question.

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
USERS_COMMAND = re.compile(r"(?:observer|setup|publish|acts)\.py")
LIVE_LINK = re.compile(r"dashboard_action\.py\b.*?(?<![A-Za-z0-9_-])link(?![A-Za-z0-9_-])", re.S)
OBSERVER_UNDER_CLAUDE = re.compile(r"CLAUDECODE")   # a command that names this variable and observer.py or setup.py is working round the check
# The commands that perform an act the user answers a question for: a Bash call of a subagent that names one of these (or
# `--hosts any`, or publish.py) is stopped in every project, observer on or off, because what they do is the user's own decision,
# asked and run by the main session. The list is written out here, since this file does not import observer_hooks; a test holds
# it equal to the asked rows of observer_hooks (ASKED) and to the acts of acts.py.
ASKED = ("advisory", "disable", "release", "resume", "trust-ancestor", "undo")
# re.S: a command may run over several lines (a backslash and a line break, or a quote that is never closed). The option is
# matched with its prefixes too (--ho, --hos, --host), which argparse would read as --hosts; the parsers of observer.py and
# setup.py no longer accept an abbreviation (allow_abbrev=False), and this holds for a command written for an older version.
USERS_COMMAND_WORDS = re.compile(r"observer\.py\b.*?(?<![A-Za-z0-9_-])(?:%s)(?![A-Za-z0-9_-])" % "|".join(ASKED), re.S)
HOSTS_ANY = re.compile(r"(?:observer|setup)\.py\b.*--ho(?:s(?:ts?)?)?(?:\s+|=)[\"']?any\b", re.S)
PUBLISH = re.compile(r"(?<![A-Za-z0-9_-])publish\.py\b")
ACTS_SCRIPT = re.compile(r"(?<![A-Za-z0-9_-])acts\.py\b")   # asking a question is the main session's own call
# A hook of the plugin is run by Claude Code, never by a session: a session that runs hooks.py or run_hook.sh with an event, and a
# JSON of its own on stdin, would make the user's typed answer (hooks.py, prompt-submit) up. --doctor and --print read only.
HOOK_RUN = re.compile(r"(?<![A-Za-z0-9_-])(?:hooks\.py|run_hook\.sh|session_start_hook\.sh)")
HOOK_READS = re.compile(r"(?<![A-Za-z0-9_-])hooks\.py\s+--(?:doctor|print)\s*$")   # the two forms that only read
HOOK_DENIAL = ("recortex: the plugin's hooks are run by Claude Code, never by a session. What a hook reads is the user's own "
               "prompt, and a session cannot make one.")
# A session never starts another Claude Code session to answer in the user's place: the hooks of that session are genuine ones.
CLAUDE_FLAGS = ("-p", "--print", "--resume", "--continue", "-r", "-c")
CLAUDE_DENIAL = ("recortex: a session never starts another Claude Code session (claude -p, --print, --resume or --continue) to "
                 "answer for the user.")
QUOTES = re.compile(r"[\\'\"]")   # taken out of the command before it is read: a quote or a backslash inside a name changes nothing
PUBLISH_READS = re.compile(r"(?<![A-Za-z0-9_-])--(?:dry-run|list)(?![A-Za-z0-9_-])")   # these two read and send nothing
SUBAGENT_DENIAL = ("recortex: this act is asked and run by the main session only, on the user's answer. Tell the main "
                   "session what is needed.")
TERMINAL_DENIAL = ("recortex: observer.py, setup.py, publish.py, acts.py and dashboard_action.py link are not run through a program that makes a "
                   "terminal or hides where they run (script, unbuffer, expect, socat, faketty, pty, tmux, screen, ssh, setsid, "
                   "nohup): what they do is the user's own answer. Ask the user the question the command names.")
# A built dashboard page (mind.py's output) is never committed, copied to a branch or sent by hand: that skips the privacy check
# that publish.py makes. A plain cp is not stopped: a local copy the user asked for is harmless.
PAGE_NAME = re.compile(r"mind\.html")
PAGE_SENDERS = re.compile(r"(?<![A-Za-z0-9_-])(?:git|gh|curl|scp|rsync|sftp|wget)(?![A-Za-z0-9_-])")
PAGE_DENIAL = ("recortex: a built dashboard page is never committed, copied to a branch or sent by hand: that skips the privacy "
               "check. To put it on the web, run acts.py ask publish and ask the question it prints. To give the user the "
               "file, tell them its path.")


LIVE_NAMES = re.compile(r"dashboard-server-|launch-[0-9a-f]{8}\.html")
LIVE_PORT = re.compile(r"(?:127\.0\.0\.1|localhost):")
LIVE_TOOLS = re.compile(r"(?<![A-Za-z0-9_-])(?:curl|wget|nc|ncat|netcat)(?![A-Za-z0-9_-])")
LIVE_DENIAL = "recortex: the live dashboard's key and its service are the user's. Use dashboard_action.py link or open."


def live_decision(data):
    """The denial for a Bash call, of the main session or of a subagent, that names the live dashboard's state file
    (dashboard-server-...) or a launch file (launch-<8 hex>.html), or that talks to a loopback port with curl, wget or nc; else
    None. dashboard_action.py link, open and status pass. A guard against a mistake: the keys are the user's, and the user's own
    confirmation in the page is what makes a change."""
    command, texts = _spoken(data)
    if command is None:
        return None
    for text in texts:
        if LIVE_NAMES.search(text) or (LIVE_PORT.search(text) and LIVE_TOOLS.search(text)):
            return deny(LIVE_DENIAL)
    return None


def terminal_decision(data):
    """The denial for a Bash call that runs observer.py, setup.py, publish.py, acts.py or `dashboard_action.py link` through a
    pseudo-terminal helper, a program that hides where it runs, or a command that names CLAUDECODE; else None. `link` prints the
    link with changes on when it sees the user's own terminal, so it is held to the same rule as the acts."""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") in ("Bash", "bash") and isinstance(command, str):
        for text in (command, QUOTES.sub("", command)):   # a quote or a backslash inside a name changes nothing
            if USERS_COMMAND.search(text) or LIVE_LINK.search(text):
                if PTY_HELPER.search(text) or OBSERVER_UNDER_CLAUDE.search(text):
                    return deny(TERMINAL_DENIAL)
    return None


def _spoken(data):
    """(the command, the texts to look at: as the shell splits it, as written, and with every quote and backslash taken out), or
    (None, ()) for a call that is no Bash command."""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str):
        return None, ()
    try:
        spoken = " ".join(shlex.split(command))
    except ValueError:
        spoken = command
    return command, (spoken, command, QUOTES.sub("", command))


def subagent_decision(data):
    """The denial for a Bash call of a subagent (its input carries an agent_id or an agent_type) that names `observer.py`
    followed by one of ASKED, or `observer.py` or `setup.py` with `--hosts any`, or `publish.py` (but not `publish.py
    --dry-run` or `--list`, which read and send nothing); else None. A call of the main session is never denied for being an
    act: the act refuses without the user's answer, and names the question. The command is read as the shell splits it and as
    written. A guard against mistakes: a session with shell access can get round it."""
    if not (data.get("agent_id") or data.get("agent_type")):
        return None
    command, texts = _spoken(data)
    if command is None:
        return None
    for text in texts:
        if (USERS_COMMAND_WORDS.search(text) or HOSTS_ANY.search(text) or ACTS_SCRIPT.search(text)
                or (PUBLISH.search(text) and not PUBLISH_READS.search(text))):
            return deny(SUBAGENT_DENIAL)
    return None


def hook_decision(data):
    """The denial for a Bash call, of the main session or of a subagent, that runs hooks.py or run_hook.sh in any way but `hooks.py
    --doctor` and `hooks.py --print`; else None. Fitted to the obvious forms: the hook itself also checks that it was started by
    Claude Code (observer_grants.plausibly_a_hook) before it takes a typed answer."""
    command, texts = _spoken(data)
    if command is None:
        return None
    for text in texts:
        if HOOK_RUN.search(text) and not HOOK_READS.search(text.strip()):
            return deny(HOOK_DENIAL)
    return None


def claude_decision(data):
    """The denial for a Bash call, of the main session or of a subagent, that starts the claude program with -p, --print, --resume
    or --continue: a session that answers for the user through a session of its own. Read as the shell splits it and as written;
    fitted to the obvious forms."""
    command, texts = _spoken(data)
    if command is None:
        return None
    for text in texts:
        try:
            words = shlex.split(text)
        except ValueError:
            words = text.split()
        for number, word in enumerate(words):
            if os.path.basename(word) == "claude" and any(flag in CLAUDE_FLAGS or flag.startswith(("--resume=", "--continue="))
                                                          for flag in words[number + 1:]):
                return deny(CLAUDE_DENIAL)
    return None


def page_decision(data):
    """The denial for a Bash call, of the main session or of a subagent, whose command names a built dashboard page (mind.html)
    together with git, gh, curl, scp, rsync, sftp or wget; else None. A plain cp, a listing and `mind.py --open` pass."""
    command, texts = _spoken(data)
    if command is None:
        return None
    for text in texts:
        if PAGE_NAME.search(text) and PAGE_SENDERS.search(text):
            return deny(PAGE_DENIAL)
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
    found = (terminal_decision(data) or page_decision(data) or live_decision(data) or hook_decision(data)
             or claude_decision(data) or subagent_decision(data))
    if found:
        return found
    record_grants(data)
    return None


def record_grants(data):
    """Job 3: never raises and never prints; without the user's answer nothing is recorded, and a call made by a subagent
    (its input carries an agent_id or an agent_type) records nothing: only the main session's own call can."""
    if data.get("agent_id") or data.get("agent_type"):
        return
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str):
        return   # every Bash call of the main session that reaches this hook is looked at: a typed grant is confirmed first
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

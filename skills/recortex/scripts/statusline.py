#!/usr/bin/env python3
"""A status line segment: whether recortex is on for the project folder, in a few words.

    python3 statusline.py < the JSON that Claude Code gives a status line command

Prints one line, `recortex on · read 2 h ago`, `recortex on · read 2 h ago · steps 7/12` (while open tasks have steps),
`recortex on · reading`, `recortex off · not trusted` and the like, or
nothing at all when the folder holds no project file (.recortex.json). With no state
file for the folder it prints `recortex · starting` only when the JSON Claude Code gives shows that no request has been
answered yet (`cost.total_api_duration_ms` is 0, or `context_window.current_usage` is null, which Claude Code's
documentation says it is before the first API call); the first hook replaces it. When the JSON does not show that, or
cannot (a field is absent), a missing file gives the `off` line below, since hooks that never run must not look like a
start. It reads one
small file that the plugin's session-start and prompt hooks write (see hooks.py write_status) and nothing else: no
plugin module, no index, no network, no git. setup.py apply copies it to <config home>/recortex/statusline.py, so that
the command in settings.json survives plugin updates; the hooks keep that copy current.

The project folder is workspace.project_dir of the JSON Claude Code passes on stdin, else $CLAUDE_PROJECT_DIR, else the
current folder, never a parent. The state file is opened without following a symlink and is used only when it is a plain
file of this user with no access for group or others, at most 2 KB, whose every value fits its pattern or table.
A file that is there and fails a check gives `recortex off · run hooks.py --doctor`, never a word of the file.

Standard library only, and only os, sys, json, time, hashlib and stat: it starts in about 15 ms.
"""

import hashlib
import json
import os
import stat
import sys
import time

CONFIG_NAME = ".recortex.json"   # names.CONFIG_NAME
SHORT = {  # the same codes as hooks.STATUS_REASONS, in a few words (a test holds the code sets equal)
    "not-trusted": "not trusted",
    "no-hub": "no hub",
    "storage": "storage refused",
    "no-index": "not read yet",
    "error": "error",
}
FALLBACK = "recortex off · run hooks.py --doctor"
STARTING = "recortex · starting"   # no state file yet, and no request answered yet in this session
MISSING = {}   # what read_state returns for no file at all, as against None for a file that fails its checks
KEYS = ("v", "state", "reason", "repo", "synced_at")
OPTIONAL = ("observer", "steps_done", "steps_total")   # written since 1.5.0: "paused" while an observer approved before 1.5.0
# waits for a level, else ""; and, only while open tasks have steps, the steps done and the steps (two integers, together)
STEPS_MAX = 99999
PAUSED = "observer paused until you choose a level"   # observer_runtime.PAUSED without its prefix, held equal by a test
MAX_BYTES = 2048


def digits(text, count):
    return len(text) == count and all("0" <= char <= "9" for char in text)


def valid_stamp(value):
    """A UTC stamp of the form 2026-11-16T10:00:00Z, or the empty string."""
    if value == "":
        return True
    return (isinstance(value, str) and len(value) == 20 and digits(value[0:4], 4) and value[4] == "-"
            and digits(value[5:7], 2) and value[7] == "-" and digits(value[8:10], 2) and value[10] == "T"
            and digits(value[11:13], 2) and value[13] == ":" and digits(value[14:16], 2) and value[16] == ":"
            and digits(value[17:19], 2) and value[19] == "Z")


def valid_repo(value):
    """GitHub's form of owner/name, or the empty string (the same rule as recortex.valid_repo)."""
    if value == "":
        return True
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, name = value.split("/")
    ok = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return (1 <= len(owner) <= 39 and owner[0] in ok and all(char in ok + "_-" for char in owner)
            and 1 <= len(name) <= 100 and name not in (".", "..") and all(char in ok + "._-" for char in name))


def epoch(stamp):
    """Seconds since 1970 of a stamp that passed valid_stamp, without time zones or imports."""
    year, month, day = int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10])
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    days = era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468
    return days * 86400 + int(stamp[11:13]) * 3600 + int(stamp[14:16]) * 60 + int(stamp[17:19])


def ago(stamp, now):
    if not stamp:
        return "at an unknown time"
    minutes = max(0, int((now - epoch(stamp)) // 60))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return "%d min ago" % minutes
    if minutes < 48 * 60:
        return "%d h ago" % (minutes // 60)
    return "%d d ago" % (minutes // 1440)


def state_top(environ):
    """The state folder of the plugin for this user: the test folder when RECORTEX_TESTING is 1, else
    $XDG_STATE_HOME or ~/.local/state, under the plugin's name; the rule of storage.state_top, held equal by a test."""
    def read(name):
        return environ.get("RECORTEX_" + name) or ""

    home = read("DATA_HOME")
    if read("TESTING") == "1" and home and os.path.isabs(home):
        return os.path.join(home, "recortex")
    base = environ.get("XDG_STATE_HOME") or ""
    if not (base and os.path.isabs(base)):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "recortex")


def project_folder(payload, environ):
    """workspace.project_dir, else CLAUDE_PROJECT_DIR, else the current folder; an absolute path of one folder."""
    workspace = payload.get("workspace") if isinstance(payload, dict) else None
    chosen = workspace.get("project_dir") if isinstance(workspace, dict) else None
    if not (isinstance(chosen, str) and chosen and "\x00" not in chosen):
        chosen = environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return os.path.abspath(chosen)


def status_file(root, environ):
    return os.path.join(state_top(environ), "private", "status", hashlib.sha256(root.encode("utf-8")).hexdigest()[:32]
                        + ".json")


def read_state(path):
    """The state dict when the file passes every check, MISSING when there is no file (or no folder for it) yet, else None."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return MISSING
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        raw = os.read(descriptor, MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_BYTES else None
    except (ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(data, dict) or not set(KEYS) <= set(data) <= set(KEYS + OPTIONAL) or data["v"] != 1
            or type(data["v"]) is not int):
        return None
    if data.get("observer", "") not in ("", "paused"):
        return None
    if ("steps_done" in data) != ("steps_total" in data):
        return None
    if "steps_done" in data:
        done, total = data["steps_done"], data["steps_total"]
        if type(done) is not int or type(total) is not int or not 0 <= done <= total <= STEPS_MAX:
            return None
    if data["state"] not in ("on", "building", "off"):
        return None
    if (data["reason"] in SHORT) != (data["state"] == "off") or (data["state"] != "off" and data["reason"] != ""):
        return None  # a reason goes with off, and only with off
    if not valid_repo(data["repo"]) or not valid_stamp(data["synced_at"]):
        return None
    return data


def unanswered(payload):
    """True when the JSON of the status line shows that no request has been answered in the session yet: the total time
    spent waiting for API responses is 0, or the current usage of the context window is null. A field that is absent, or of
    another type, shows nothing."""
    if not isinstance(payload, dict):
        return False
    cost, window = payload.get("cost"), payload.get("context_window")
    spent = cost.get("total_api_duration_ms") if isinstance(cost, dict) else None
    if type(spent) in (int, float) and spent == 0:
        return True
    return isinstance(window, dict) and "current_usage" in window and window["current_usage"] is None


def line(stdin_text="", environ=None, now=None):
    """The status line for the JSON text Claude Code gave: "" when the folder holds no project file."""
    environ = os.environ if environ is None else environ
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    root = project_folder(payload, environ)
    if not os.path.isfile(os.path.join(root, CONFIG_NAME)):
        return ""
    data = read_state(status_file(root, environ))
    if data is MISSING and unanswered(payload):
        return STARTING
    if data is MISSING:
        return FALLBACK
    if data is None:
        return FALLBACK
    if data["state"] == "off":
        return "recortex off · %s" % SHORT[data["reason"]]
    note = " · " + PAUSED if data.get("observer") == "paused" else ""
    if data["state"] == "building":
        return "recortex on · reading" + note
    steps = " · steps %d/%d" % (data["steps_done"], data["steps_total"]) if data.get("steps_total") else ""
    return "recortex on · read %s" % ago(data["synced_at"], time.time() if now is None else now) + steps + note


def main():
    try:
        text = sys.stdin.read() if not sys.stdin.isatty() else ""
        shown = line(text)
    except Exception:  # noqa: BLE001 - a status line never fails
        shown = ""
    if shown:
        sys.stdout.write(shown + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

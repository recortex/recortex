"""Bounded native transcript pointers. Source contents exist only in the worker's memory."""
import datetime
import hashlib
import json
import os
import re
import shlex
import stat
import time

import storage

try:
    import grp
    import pwd
except ImportError:  # no user database: group write on a folder is never accepted
    grp = pwd = None

MAX_READ = 262144
MAX_ROWS = 512
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
EVENTS = {"UserPromptSubmit", "PreToolUse", "Stop", "PreCompact"}  # the capture events hooks/hooks.json registers


class SourceError(ValueError):
    """Fixed reason only, never source contents or filesystem paths."""


# The codes a transcript that cannot be read gives (SourceError and preflight): the fixed set that status, the refusals and
# --doctor may print. `source-missing` is not among them: before any session has run in a folder there is no transcript yet.
UNREADABLE_CODES = ("source-owner", "source-permissions", "source-symlink", "source-path", "source-file", "source-changed",
                    "source-incomplete", "source-unavailable", "source-config")
TRUST_RUN = "Ask the trust question: run acts.py ask trust."


def trust_commands(scripts=None):
    """The one command that asks the trust question for the folders now refused: acts.py ask trust, which prints the question,
    the folders it names and the command of each option."""
    return ["python3 %s ask trust" % shlex.quote(os.path.join(scripts or os.path.dirname(os.path.abspath(__file__)), "acts.py"))]


def _boundary(path, folder, source_file=None):
    """Fixed boundary names are safe in default status and hook diagnostics."""
    if path == source_file:
        return "transcript"
    if path == folder:
        return "native-project"
    if path.startswith(folder + os.sep):
        return "source-directory"
    if path == os.path.dirname(folder):
        return "native-projects"
    if path == os.path.dirname(os.path.dirname(folder)):
        return "native-config"
    return "ancestor"


PASSWD = "/etc/passwd"
GROUP = "/etc/group"
ACCOUNTS_CAP = 4 * 1024 * 1024
NUMBER = re.compile(r"[0-9]{1,10}")


def _local(path, fields, numbers):
    """The entries of a local account file, with the fields at `numbers` as integers; None, which counts as "not
    private", when the file is larger than ACCOUNTS_CAP, cannot be read, or holds any line that is a NIS compat entry
    (+ or -), has another number of fields or a field at `numbers` that is not a number. Nothing is skipped."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(ACCOUNTS_CAP + 1)
    except OSError:
        return None
    if len(raw) > ACCOUNTS_CAP:
        return None
    entries = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        if line == "":
            continue
        parts = line.split(":")
        if line[0] in "+-" or len(parts) != fields or not all(NUMBER.fullmatch(parts[n]) for n in numbers):
            return None
        entries.append([int(part) if n in numbers else part for n, part in enumerate(parts)])
    return entries


def private_group(uid, gid):
    """True when `gid` is the user private group of `uid`, established from the local account files and never from
    an enumeration that a network directory can leave incomplete: the owner and the group are each defined once in
    /etc/passwd and /etc/group, the group is the owner's primary group and is named after the owner, it has no member
    but the owner, and no other local account has it as its primary group. The system's own lookup (NSS) must agree.
    A network account or group, or a file that does not parse cleanly, is refused. Default Ubuntu makes ~/.claude
    and ~/.claude/projects 775 in such a group."""
    if type(uid) is not int or type(gid) is not int or grp is None or pwd is None:
        return False
    users, groups = _local(PASSWD, 7, (2, 3)), _local(GROUP, 4, (2,))
    if users is None or groups is None:
        return False
    owners = [entry for entry in users if entry[2] == uid]
    named = [entry for entry in groups if entry[2] == gid]
    if len(owners) != 1 or len(named) != 1:
        return False
    owner, group = owners[0], named[0]
    members = [member for member in group[3].split(",") if member]
    if not (owner[3] == gid and group[0] == owner[0] and all(member == owner[0] for member in members)
            and not any(entry[3] == gid and entry[0] != owner[0] for entry in users)):
        return False
    try:
        looked_up, account = grp.getgrgid(gid), pwd.getpwuid(uid)
    except (KeyError, OverflowError, OSError):
        return False
    return (looked_up.gr_name == owner[0] and all(member == owner[0] for member in looked_up.gr_mem)
            and account.pw_gid == gid and account.pw_name == owner[0])


def _writable(info, leaf=False):
    """World write; group write on a transcript file; group write on a folder by any group but the owner's private
    group."""
    if info.st_mode & stat.S_IWOTH or leaf and info.st_mode & stat.S_IWGRP:
        return True
    return bool(info.st_mode & stat.S_IWGRP) and not private_group(info.st_uid, getattr(info, "st_gid", None))


def _component_problem(path, info, folder, source_file=None):
    """One ownership/mode policy for preflight and actual source resolution.

An ancestor's owner can replace its children even when group/other write bits
are clear. Sticky mode protects entries from other users, not from that owner.
Only sticky ancestors owned by root or the current user, outside the source
tree, are an exception to the write-bit rule; source directories stay private.
Group write is accepted only on a folder, and only for the owner's user private group
(private_group); a transcript file must be writable by the user alone.
"""
    if stat.S_ISLNK(info.st_mode):
        return "source-symlink"
    leaf = path == source_file
    if not (stat.S_ISREG(info.st_mode) if leaf else stat.S_ISDIR(info.st_mode)):
        return "source-file" if leaf else "source-path"
    inside = path == folder or path.startswith(folder + os.sep)
    if hasattr(os, "getuid"):
        owners = (os.getuid(),) if inside else (os.getuid(), 0)
        if info.st_uid not in owners:
            return "source-owner"
    trusted_owner = info.st_uid == 0 or hasattr(os, "getuid") and info.st_uid == os.getuid()
    shared_sticky = (not inside and not leaf and trusted_owner and
                     stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX))
    if _writable(info, leaf) and not shared_sticky:
        return "source-permissions"
    return None


# ---------------------------------------------------------------- trusted ancestors (1.7.0)
#
# On a shared network filesystem the folders above Claude Code's transcripts belong to another account or are written by a
# shared group, and no user can change that. The user can trust such a folder exactly as it is: its path, owner, group and
# mode, recorded by `observer.py trust-ancestor` on their own terminal and bound to the user key. An entry holds only while
# all three still match. The transcript, its project folder and everything inside it are never accepted this way, and
# neither is a folder that fails for any reason but its owner or its permissions (a link, a file in place of a folder).
# What the owner of a trusted folder can still do is put another tree in its place; that tree must still have a project
# folder and a transcript that belong to the user and are closed to group and others.
TRUSTED = "trusted-ancestors.json"
TRUSTED_LOCK = "trusted-ancestors.lock"
TRUSTED_FORM = 1
TRUSTED_ENTRIES = 64
TRUSTED_CAP = 65536
TRUSTABLE = ("source-owner", "source-permissions")
ID_MAX = 2 ** 32 - 1
MAC = re.compile(r"[0-9a-f]{32}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _above(path, folder):
    """True when `path` is a folder strictly above `folder`, the root folder included (for "/" the prefix is "/", never "//")."""
    return path != folder and folder.startswith(path if path.endswith(os.sep) else path + os.sep)


def _trusted_path_ok(path):
    return (isinstance(path, str) and 0 < len(path) <= 4096 and os.path.isabs(path) and os.path.normpath(path) == path
            and not CONTROL.search(path))


def _mac(path, uid, gid, mode):
    """The entry's HMAC under the user key: a trusted file restored or edited from elsewhere trusts nothing."""
    import hmac
    key = storage.user_key(create=False, tighten=False)
    return hmac.new(key, _canonical(["ancestor", path, uid, gid, mode]).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _valid_entry(path, entry):
    return (_trusted_path_ok(path) and isinstance(entry, dict) and set(entry) == {"uid", "gid", "mode", "mac", "added"}
            and all(type(entry[name]) is int and 0 <= entry[name] <= ID_MAX for name in ("uid", "gid"))
            and type(entry["mode"]) is int and 0 <= entry["mode"] <= 0o7777
            and isinstance(entry["mac"], str) and MAC.fullmatch(entry["mac"])
            and type(entry["added"]) in (int, float) and 0 <= entry["added"] < 4102444800)


def read_trusted(create=False):
    """(state, entries): "none" with {} when there is no file; "ok" with the entries; "unreadable" with {} for a file that
    cannot be read, is too large, is not JSON, does not fit the form or is not safe (storage.check_path). Never raises."""
    try:
        private = storage.folder(storage.state_top(for_key=True), create=create, tighten=create)
        if private is None:
            return "none", {}
        path = os.path.join(private, TRUSTED)
        info = storage.check_path(path, "file")
        if not info:
            return "none", {}
        if info.st_size > TRUSTED_CAP:
            return "unreadable", {}
        data = json.loads(storage.read_file(path) or "")
        if (not isinstance(data, dict) or set(data) != {"form", "entries"} or data["form"] != TRUSTED_FORM
                or not isinstance(data["entries"], dict) or len(data["entries"]) > TRUSTED_ENTRIES
                or not all(_valid_entry(key, value) for key, value in data["entries"].items())):
            return "unreadable", {}
        return "ok", dict(data["entries"])
    except (OSError, ValueError, RecursionError, storage.StorageError):
        return "unreadable", {}


class Trust:
    """The user's trusted ancestors, loaded once, on first use, and never raising: a file that cannot be used trusts
    nothing (`state` is then "unreadable"). One per walk of a path; the file is read only when a component fails."""

    def __init__(self):
        self.state, self.entries, self.loaded = "none", {}, False
        self.used = []   # the paths accepted so far

    def load(self):
        if not self.loaded:
            self.loaded = True
            self.state, self.entries = read_trusted()
        return self

    @staticmethod
    def _matches(path, entry, info):
        import hmac
        try:
            same = (entry["uid"], entry["gid"], entry["mode"]) == (info.st_uid, getattr(info, "st_gid", None),
                                                                   stat.S_IMODE(info.st_mode))
            return same and hmac.compare_digest(entry["mac"], _mac(path, entry["uid"], entry["gid"], entry["mode"]))
        except Exception:  # noqa: BLE001 - nothing here may raise: a key that cannot be read trusts nothing
            return False

    def accepts(self, path, info, folder, source_file=None):
        """True when `path` is a folder above the project folder that the user trusted exactly as it is now: a directory
        and no link, an entry for the exact path, its owner, group and mode as recorded, and the HMAC of the entry."""
        if path == source_file or path == folder or path.startswith(folder + os.sep) or not _above(path, folder):
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
        entry = self.load().entries.get(path)
        if entry is None:
            return False
        if self._matches(path, entry, info):
            self.used.append(path)
            return True
        return False

    def changed(self, path, info):
        """True when an entry exists for `path` and what `info` shows no longer matches it."""
        entry = self.load().entries.get(path)
        if entry is None:
            return False
        return not self._matches(path, entry, info)


def _problem(path, info, folder, source_file=None, trust=None):
    """_component_problem, except that an ancestor with a code of TRUSTABLE that the user trusted exactly as it is (Trust)
    has none. The transcript, the project folder and what is inside it never pass this way."""
    problem = _component_problem(path, info, folder, source_file)
    if problem in TRUSTABLE and trust is not None and trust.accepts(path, info, folder, source_file):
        return None
    return problem


def _trustable(path, info, folder, code):
    """A directory (no link) strictly above the project folder that fails only for its owner or its permissions."""
    return (code in TRUSTABLE and stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and _above(path, folder))


def _world(info):
    return bool(info.st_mode & stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX


def failing(project_root):
    """The folders the walk to the transcript refuses now, for `observer.py trust-ancestor --failing`: path, code, boundary,
    uid, gid, mode (four octal digits), `trustable` (see _trustable) and `world` (all may write, no sticky bit). A folder
    the user trusted and that still matches is not among them. [] when there is no problem or the walk cannot be made."""
    try:
        folder = project_directory(project_root)
        trust = Trust()
        rows, current = [], folder
        while True:
            if os.path.lexists(current):
                info = os.lstat(current)
                code = _problem(current, info, folder, None, trust)
                if code:
                    rows.append({"path": current, "code": code, "boundary": _boundary(current, folder), "uid": info.st_uid,
                                 "gid": getattr(info, "st_gid", 0), "mode": format(stat.S_IMODE(info.st_mode), "04o"),
                                 "trustable": _trustable(current, info, folder, code), "world": _world(info)})
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return rows
    except (OSError, SourceError):
        return []


class TrustError(ValueError):
    """Fixed code only."""


def trust_add(rows, project_root):
    """Trust each folder of `rows` (dicts with a "path", or paths) exactly as it is now: its owner, group and mode are read
    again here and stored with their HMAC, under the lock of the file, which is never replaced when it cannot be read. A
    folder that is not a directory above the project folder, or is a link, is refused whole (TrustError "not-an-ancestor"),
    as is a file that would hold more than 64. Returns the entries stored: {path: entry}."""
    import field_sheet
    folder = project_directory(project_root)
    paths = [row["path"] if isinstance(row, dict) else row for row in rows]
    storage.user_key(create=True)   # the HMAC of an entry needs the user key
    stored = {}
    for row, path in zip(rows, paths):
        if not _trusted_path_ok(path) or not _above(path, folder):
            raise TrustError("not-an-ancestor")
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TrustError("not-an-ancestor")
        uid, gid, mode = info.st_uid, getattr(info, "st_gid", 0), stat.S_IMODE(info.st_mode)
        if isinstance(row, dict) and "uid" in row and (row["uid"], row.get("gid"), row.get("mode")) != (
                uid, gid, format(mode, "04o")):
            raise TrustError("trust_changed")   # what the user was asked about is no longer how the folder is: nothing is stored
        stored[path] = {"uid": uid, "gid": gid, "mode": mode, "mac": _mac(path, uid, gid, mode), "added": int(time.time())}
    with field_sheet.approvals_lock(storage.state_top(for_key=True), TRUSTED_LOCK):
        state, entries = read_trusted(create=True)
        if state == "unreadable":
            raise TrustError("unreadable")
        entries.update(stored)
        if len(entries) > TRUSTED_ENTRIES:
            raise TrustError("too-many")
        storage.write_file(os.path.join(storage.folder(storage.state_top(for_key=True)), TRUSTED),
                           json.dumps({"form": TRUSTED_FORM, "entries": entries}, sort_keys=True, indent=1) + "\n")
    return stored


def trust_forget(path):
    """Remove one entry. True when there was one; TrustError "unreadable" when the file cannot be used (it stays)."""
    import field_sheet
    with field_sheet.approvals_lock(storage.state_top(for_key=True), TRUSTED_LOCK):
        state, entries = read_trusted(create=False)
        if state == "unreadable":
            raise TrustError("unreadable")
        if path not in entries:
            return False
        entries.pop(path)
        storage.write_file(os.path.join(storage.folder(storage.state_top(for_key=True)), TRUSTED),
                           json.dumps({"form": TRUSTED_FORM, "entries": entries}, sort_keys=True, indent=1) + "\n")
    return True


def trusted_listing():
    """(state, rows): each entry with its path, uid, gid, mode and whether the folder still matches it now."""
    state, entries = read_trusted()
    trust = Trust()
    trust.loaded, trust.state, trust.entries = True, state, entries
    rows = []
    for path, entry in sorted(entries.items()):
        try:
            info = os.lstat(path)
            now = (not stat.S_ISLNK(info.st_mode) and stat.S_ISDIR(info.st_mode) and not trust.changed(path, info))
        except OSError:
            now = False
        rows.append({"path": path, "uid": entry["uid"], "gid": entry["gid"], "mode": format(entry["mode"], "04o"),
                     "matches": now})
    return state, rows



def project_directory(project_root):
    root = os.path.abspath(project_root)
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    if not os.path.isabs(config):
        raise SourceError("source-config")
    encoded = re.sub(r"[^A-Za-z0-9]", "-", root)
    return os.path.join(config, "projects", encoded)


def preflight(project_root, details=False):
    """Inspect directory metadata only; paths require an explicit diagnostic request."""
    try:
        folder = project_directory(project_root)
        missing = not os.path.isdir(folder)
        current = folder
        issues, trust, fixable, changed = [], Trust(), True, False
        while True:
            if os.path.lexists(current):
                info = os.lstat(current)
                problem = _problem(current, info, folder, None, trust)
                if problem:
                    issue = {"code": problem, "boundary": _boundary(current, folder),
                             "action": "review-owned-permissions" if problem == "source-permissions" else "review-source-location"}
                    if details:
                        issue.update(path=current, mode=format(stat.S_IMODE(info.st_mode), "04o"),
                                     owner="current-user" if hasattr(os, "getuid") and info.st_uid == os.getuid()
                                     else "root" if info.st_uid == 0 else "other",
                                     uid=info.st_uid, gid=getattr(info, "st_gid", 0))
                    issues.append(issue)
                    fixable = fixable and _trustable(current, info, folder, problem)
                    changed = changed or problem in TRUSTABLE and trust.changed(current, info)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        extra = {"trusted": len(trust.used)}
        if trust.loaded and trust.state == "unreadable":
            extra["trust"] = "unreadable"
        elif changed:
            extra["trust"] = "changed"
        if issues:
            if fixable:
                extra["fix"] = "trust-ancestor"
            return dict(issues[0], ready=False, issues=issues, **extra)
        return dict({"ready": not missing, "code": "source-missing" if missing else "ok"}, **extra)
    except (OSError, SourceError):
        return {"ready": False, "code": "source-unavailable"}


def approved_path(path, project_root, session_id):
    """Only this project's native main/subagent transcript, never an arbitrary hook/model path."""
    if not isinstance(path, str) or not os.path.isabs(path) or not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        raise SourceError("source-identity")
    folder = project_directory(project_root)
    relative = os.path.relpath(path, folder)
    main = session_id + ".jsonl"
    sub = re.fullmatch(re.escape(session_id) + r"/subagents/agent-([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.jsonl", relative)
    if relative != main and not sub:
        raise SourceError("source-project")
    if os.path.normpath(path) != path:
        raise SourceError("source-path")
    current, trust = path, Trust()
    while True:
        info = os.lstat(current)
        problem = _problem(current, info, folder, path, trust)
        if problem:
            raise SourceError(problem)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return path


def _read(path, offset, size):
    if type(offset) is not int or offset < 0 or type(size) is not int or size < 0 or size > MAX_READ:
        raise SourceError("source-range")
    before = os.lstat(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise SourceError("source-changed")
        # Recheck the opened file rather than relying only on a prior pathname
        # validation. A swapped regular file must not acquire source authority.
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise SourceError("source-owner")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SourceError("source-permissions")  # the transcript itself is writable by the user alone
        os.lseek(descriptor, offset, os.SEEK_SET)
        chunks, left = [], size
        while left:
            chunk = os.read(descriptor, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        after = os.lstat(path)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise SourceError("source-changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


# Records that are not part of the conversation: a copy of what was sent to the API (api-request-blob, which repeats
# messages and carries system reminders), attachments, the skill text Claude Code adds (isMeta), a compaction summary
# (model text in a user record) and lines shown only in the transcript.
HIDDEN = ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")
# A user record counts as the user's own words only when it is an ordinary typed prompt: none of these is set, its
# userType is the standard one, it holds text only, and no harness wrapper, quote or pasted block is in it.
NOT_TYPED = ("isMeta", "isSidechain", "isCompactSummary", "isVisibleInTranscriptOnly", "toolUseResult",
             "sourceToolUseID", "sourceToolAssistantUUID")
# A chat channel's message (<channel source=...>) is among them: another person can write in a channel, so it counts
# for holds only (the looser rule), never as the user's words for a decision or a change of status.
HARNESS = re.compile(r"<(?:command-[a-z]+|local-command-[a-z]+|bash-[a-z]+|system-reminder|task-notification|"
                     r"user-prompt-submit-hook|pasted_content|tool_result|tool_use_error|channel)\b|\[Pasted text|"
                     r"\[Request interrupted|(?m:^Caveat: )|(?m:^[ \t]*(?:>|```|~~~))")
REMINDER = re.compile(r"<system-reminder>(?:.*?</system-reminder>|.*\Z)[ \t\r\n]*", re.S)


def typed_prompt(record, content, text):
    """True only for a prompt the user typed (an allowlist): a "user" record of userType "external" whose content is
    text only, with none of NOT_TYPED set, no harness wrapper (a command, a reminder, a notification), no quoted line,
    code fence or pasted block. Anything else (a tool result, a compaction summary, a skill's text, a command wrapper
    or a subagent's prompt) is never the user's own words for a decision or a change of status."""
    if record.get("type") != "user" or record.get("userType") != "external":
        return False
    if any(key in record and record[key] is not False for key in NOT_TYPED):
        return False
    if isinstance(content, list) and not all(isinstance(block, dict) and block.get("type") == "text"
                                             for block in content):
        return False
    return bool(text.strip()) and not HARNESS.search(text)


def _queued(record):
    """(role, text, typed, control) of a message the user typed while a turn ran. Claude Code stores it as a
    queued_command attachment, with no user record, when the running turn takes it in. Only the strict shape counts:
    an attachment of the main session (no sidechain, meta, compaction or transcript-only mark) with userType
    "external", origin {"kind": "human"}, humanTurn true, the prompt command mode and a prompt; its text then goes
    through the typed prompt's checks."""
    item = record.get("attachment")
    if (not isinstance(item, dict) or item.get("type") != "queued_command" or item.get("humanTurn") is not True
            or item.get("origin") != {"kind": "human"} or item.get("commandMode", "prompt") != "prompt"
            or not isinstance(item.get("prompt"), str) or record.get("userType") != "external"
            or any(record.get(key) for key in HIDDEN) or record.get("isSidechain")):
        return "unknown", "", False, False
    text = item["prompt"]
    typed = bool(text.strip()) and not HARNESS.search(text)
    control = not re.search(r"(?m)^\s*>|<pasted_content\b|<system-reminder\b|<tool_result\b", text)
    return "user", REMINDER.sub("", text), typed, control


def _content(record):
    """(role, text, typed, control) of one transcript record. The observer is a note-taker of the conversation: it reads
    the user's prompts and the text the agent writes to the user, nothing else. A tool result has role "tool" and no
    text; an assistant record gives its text blocks only, not its thinking or its tool calls. typed: the user's own
    words (typed_prompt); control: a user message that may set a hold, by the looser earlier rule, since a hold only
    denies. Text in system reminders never reaches a model."""
    if record.get("type") == "attachment":
        return _queued(record)
    message = record.get("message")
    if not isinstance(message, dict) or record.get("type") not in ("user", "assistant"):
        return "unknown", "", False, False
    role = message.get("role")
    if role != record["type"] or any(record.get(key) for key in HIDDEN) or record.get("isSidechain"):
        return "unknown", "", False, False  # a sidechain (a subagent's turn) is not the conversation with the user
    if role == "assistant" and (record.get("isApiErrorMessage") or message.get("model") == "<synthetic>"):
        return "unknown", "", False, False  # Claude Code's own records (an API error, "No response requested.")
    content = message.get("content")
    if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result"
                                         for block in content):
        return "tool", "", False, False  # tool output, file contents, pages and command results are not read
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(block["text"] for block in content if isinstance(block, dict)
                         and block.get("type") == "text" and isinstance(block.get("text"), str))
    else:
        text = ""
    control = role == "user" and not record.get("isMeta") and not record.get("isSidechain")
    if re.search(r"(?m)^\s*>|<pasted_content\b|<system-reminder\b|<tool_result\b", text):
        control = False
    typed = role == "user" and typed_prompt(record, content, text)
    return role, REMINDER.sub("", text), typed, control


def _content_hash(record, role, subagent=False):
    content = ((record.get("attachment") or {}).get("prompt") if record.get("type") == "attachment"
               else (record.get("message") or {}).get("content"))
    canonical = json.dumps([role, content, bool(record.get("isMeta")), bool(record.get("isSidechain")), subagent], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _identity(session, role, raw, record, subagent=False):
    native_id = record.get("uuid")
    if isinstance(native_id, str) and re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", native_id):
        # Replay/fork wrappers do not create new observations of an existing message.
        return hashlib.sha256((native_id.lower() + "\n" + role + "\n" + _content_hash(record, role, subagent)).encode()).hexdigest()
    return hashlib.sha256((session + "\n" + role + "\n").encode() + raw).hexdigest()


def scan(data, project_root, cursor=0, max_bytes=MAX_READ, calls=None, stats=None):
    """Return (metadata-only observations, next byte cursor). Store both in one transaction.

    The plan and question dialogs of the main session (see DIALOG_SHAPES) give descriptors too. `calls` is the dict of
    the dialog calls kept for this transcript (load_calls, save_calls); scan reads it, adds the calls it sees, and a
    result whose call it does not hold is looked for in the last FIND_BYTES of the transcript. `stats` is a dict in
    which scan counts "dialogs_unread" (add_unread keeps it)."""
    calls = {} if calls is None else calls
    stats = {} if stats is None else stats
    session = data.get("session_id")
    event = data.get("hook_event_name", "Stop")
    if event not in EVENTS:
        return [], cursor
    path = approved_path(data.get("transcript_path"), project_root, session)
    subagent = os.sep + "subagents" + os.sep in path
    info = os.stat(path, follow_symlinks=False)
    if cursor > info.st_size:
        raise SourceError("source-truncated")
    limit = min(MAX_READ, max_bytes)
    raw = _read(path, cursor, limit)
    rows, position = [], cursor
    stamp = time.time()

    def gap(chunk, start):
        digest = hashlib.sha256(chunk).hexdigest()
        sid = hashlib.sha256((session + "\nsource_gap\n" + str(start) + "\n" + digest).encode()).hexdigest()
        rows.append({"source_id": sid, "session_id": session, "role": "unknown", "event_type": "source_gap",
                     "path": path, "offset": start, "end": start + len(chunk), "sha256": digest, "observed_at": stamp})

    # A prior full bounded read may have stopped inside an oversized line. Keep
    # a frozen metadata pointer for each omitted piece and progress to its end.
    if cursor and _read(path, cursor - 1, 1) != b"\n" and raw:
        newline = raw.find(b"\n")
        consumed = newline + 1 if newline >= 0 else len(raw)
        gap(raw[:consumed], cursor)
        position += consumed
        raw = raw[consumed:]
    remainder = raw
    for line in raw.splitlines(keepends=True):
        if len(rows) >= MAX_ROWS:
            break
        if not line.endswith(b"\n"):
            break  # wait for an asynchronous transcript writer to finish the line
        end = position + len(line)
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            raise SourceError("source-format")
        role, text, _, _ = _content(record) if isinstance(record, dict) else ("unknown", "", False, False)
        if text and not subagent:  # a subagent's transcript is not the conversation with the user
            digest = hashlib.sha256(line).hexdigest()
            rows.append({"source_id": _identity(session, role, line, record, subagent), "session_id": session, "role": role,
                         "event_type": event, "path": path, "offset": position, "end": end,
                         "sha256": digest, "content_sha256": _content_hash(record, role, subagent), "observed_at": stamp})
        if isinstance(record, dict) and not subagent:  # the dialogs of the main conversation, never a subagent's
            rows.extend(_dialog_rows(session, event, path, record, line, position, end, stamp, calls, stats))
        position = end
        remainder = remainder[len(line):]
    if remainder and position == cursor and len(remainder) >= limit:
        gap(remainder, position)
        position += len(remainder)
    return rows, position


TAIL_BYTES = 32 * 1024 * 1024  # how much unread transcript the check before a Bash call reads for controls
TAIL_SECONDS = 1.0  # and for how long
# Words every control has: /recortex hold, "don't push", "do not tag", "don't release", "don't post", "off the record".
CONTROL_WORDS = (b"recortex", b"push", b"tag", b"release", b"post", b"record", b"publish")


def first_cursor(data, project_root):
    """Where capture of a transcript seen for the first time begins: 0, or for a transcript longer than TAIL_BYTES
    (the observer enabled in a long session, or one resumed later) the start of the first whole line in its last
    TAIL_BYTES. Older text is not captured, and a hold typed there predates this check."""
    path = approved_path(data.get("transcript_path"), project_root, data.get("session_id"))
    size = os.stat(path, follow_symlinks=False).st_size
    if size <= TAIL_BYTES:
        return 0
    position = size - TAIL_BYTES
    while position < size:
        chunk = _read(path, position, min(MAX_READ, size - position))
        if not chunk or os.stat(path, follow_symlinks=False).st_size < size:
            raise SourceError("source-truncated")  # the file shrank while it was read: never loop on it
        newline = chunk.find(b"\n")
        if newline >= 0:
            return position + newline + 1
        position += len(chunk)
    return size


def skipped(data, start):
    """The one source_gap descriptor for the start of a transcript that first_cursor left out, so that status shows
    it. Its digest names the range; the text was never read."""
    session = data.get("session_id")
    path = data.get("transcript_path")
    digest = hashlib.sha256(("skipped:0:%d" % start).encode()).hexdigest()
    return {"source_id": hashlib.sha256((session + "\nsource_gap\n0\n" + digest).encode()).hexdigest(),
            "session_id": session, "role": "unknown", "event_type": "source_gap", "path": path, "offset": 0,
            "end": start, "sha256": digest, "observed_at": time.time()}


def control_tail(data, project_root, start, max_bytes=None, seconds=None):
    """(rows, complete): metadata rows for the user messages past `start` that may hold a control, read without being
    captured, so that the check before a Bash call sees a hold typed in a part of the transcript capture has not
    reached yet. Other lines are passed over without being parsed. complete is False when the end of the transcript
    was not reached within `max_bytes` and `seconds`; a line longer than one read (MAX_READ) is passed over."""
    session = data.get("session_id")
    event = data.get("hook_event_name", "PreToolUse")
    path = approved_path(data.get("transcript_path"), project_root, session)
    subagent = os.sep + "subagents" + os.sep in path
    size = os.stat(path, follow_symlinks=False).st_size
    if size - start > (TAIL_BYTES if max_bytes is None else max_bytes):
        return [], False
    until = time.monotonic() + (TAIL_SECONDS if seconds is None else seconds)
    rows, stamp, position = [], time.time(), start
    buffer, buffer_start = b"", start
    skip = bool(start) and _read(path, start - 1, 1) != b"\n"  # a cursor inside an oversized line
    while position < size:
        if time.monotonic() > until:
            return rows, False
        chunk = _read(path, position, min(MAX_READ, size - position))
        if not chunk:
            break
        position += len(chunk)
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line, line_start = buffer[:newline + 1], buffer_start
            buffer, buffer_start = buffer[newline + 1:], buffer_start + newline + 1
            if skip:
                skip = False
                continue
            low = line.lower()
            if ((b'"user"' not in low and b'"queued_command"' not in low)
                    or not any(word in low for word in CONTROL_WORDS)):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            role, text, _, control = _content(record) if isinstance(record, dict) else ("unknown", "", False, False)
            if role == "user" and control and text:
                rows.append({"source_id": _identity(session, role, line, record, subagent), "session_id": session,
                             "role": role, "event_type": event, "path": path, "offset": line_start,
                             "end": line_start + len(line), "sha256": hashlib.sha256(line).hexdigest(),
                             "content_sha256": _content_hash(record, role, subagent), "observed_at": stamp})
        if len(buffer) > MAX_READ:  # an oversized line holds no typed control
            skip, buffer_start, buffer = True, buffer_start + len(buffer), b""
    return rows, True


SHORT_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")
ANSWER_BYTES = 8 * 1024 * 1024  # how much of the end of the main transcript is searched for the user's answer
TOOL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Claude Code's own sentence for an answer, in its earlier and its 2.1.325 wording.
ANSWERED = re.compile(r'(?:User has answered your questions|Your questions have been answered): "(.*)"="([^"\n]*)"\.'
                      r'(?: .*)?', re.S)


def _main_record(record, role):
    """True for a record of the main conversation by `role`: not a sidechain (a subagent's turn), not one Claude Code
    adds (meta, a compaction summary, a line shown only in the transcript, an API error)."""
    message = record.get("message") if isinstance(record, dict) else None
    return (isinstance(message, dict) and record.get("type") == role and message.get("role") == role
            and not any(record.get(key) for key in HIDDEN) and not record.get("isSidechain")
            and not record.get("isApiErrorMessage") and message.get("model") != "<synthetic>"
            and isinstance(message.get("content"), list))


def answered(data, project_root, short, line, label="Post", max_bytes=ANSWER_BYTES, seconds=TAIL_SECONDS, text_is=None,
             labels_are=None):
    """The id of the user's answer `label` to the newest question about `short` in the main transcript, or None.

    The question is an AskUserQuestion call in a main-session assistant record, one of whose questions names `short`
    and no other short id (`short` None: a setup question, which names none; `line` is then its fixed first sentence), quotes `line` (the memory's description, observer_store.description) as it is once
    whitespace is normalized, so that the user answered about that memory and not about a description the session
    wrote, and offers an option labeled `label`; the answer is the result Claude Code recorded for that
    call, matched by its id: the record's structured answers, or, for a call of one question, its fixed sentence. A
    later answer of another label to a question about `short` undoes it. A tool result, a page or a subagent cannot
    make such a record; a session that writes the transcript itself can (the documented limit, as for holds). Read as
    capture reads: this project's main transcript only, through checked descriptors, within `max_bytes` and
    `seconds`. With `text_is` (and `labels_are`) the question must be the plugin's own whole question: its text, with every
    space and line break taken out, equal to `text_is` taken the same way, and its options' labels equal to `labels_are`, in that order: a question
    that holds the same words among others of the session's, or offers other options, is not it."""
    map_question = False
    if short and isinstance(text_is, str) and text_is.startswith("Project Map "):
        # Stable branch IDs can themselves be eight hexadecimal characters. Only the canonical question with all
        # options in order can identify those as branch IDs rather than another grant request.
        import observer_waiting
        expected = observer_waiting.entry({"kind": "map", "short": short, "key": short, "line": line})
        map_question = text_is == expected["question"] and list(labels_are or ()) == expected["options"]
    line = " ".join(line.split()) if isinstance(line, str) else ""
    if not line:
        return None
    session = data.get("session_id")
    path = approved_path(data.get("transcript_path"), project_root, session)
    if os.path.basename(path) != session + ".jsonl" or os.path.dirname(path) != project_directory(project_root):
        raise SourceError("source-project")  # the main conversation only, never a subagent's transcript
    size = os.stat(path, follow_symlinks=False).st_size
    position = max(0, size - max_bytes)
    skip = position > 0 and _read(path, position - 1, 1) != b"\n"
    until = time.monotonic() + seconds
    asked, found, buffer = {}, None, b""
    while position < size:
        if time.monotonic() > until:
            raise SourceError("source-incomplete")
        chunk = _read(path, position, min(MAX_READ, size - position))
        if not chunk:
            break
        position += len(chunk)
        buffer += chunk
        rows = buffer.split(b"\n")
        buffer = rows.pop()
        for row in rows:
            if skip:
                skip = False
                continue
            if b"AskUserQuestion" in row and b'"assistant"' in row:
                try:
                    record = json.loads(row)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not _main_record(record, "assistant"):
                    continue
                for block in record["message"]["content"]:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") == "AskUserQuestion" and isinstance(block.get("id"), str)
                            and TOOL_ID.fullmatch(block["id"]) and isinstance(block.get("input"), dict)):
                        continue
                    questions = block["input"].get("questions")
                    questions = [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []
                    for question in questions:
                        text = question.get("question")
                        options = question.get("options") if isinstance(question.get("options"), list) else []
                        labels = [o.get("label") for o in options if isinstance(o, dict)]
                        whole = " ".join(text.split()) if isinstance(text, str) else ""
                        if (isinstance(text, str) and (short is None or map_question or set(SHORT_ID.findall(text)) == {short})
                                and (line in whole if text_is is None else "".join(text.split()) == "".join(text_is.split()))
                                and (labels_are is None or labels == list(labels_are))):
                            asked[block["id"]] = (text, label in labels, len(questions))
            elif asked and b'"tool_result"' in row and any(key.encode() in row for key in asked):
                try:
                    record = json.loads(row)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not _main_record(record, "user"):
                    continue
                for block in record["message"]["content"]:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"
                            and block.get("tool_use_id") in asked):
                        continue
                    text, offered, count = asked[block["tool_use_id"]]
                    result = record.get("toolUseResult")
                    answers = result.get("answers") if isinstance(result, dict) else None
                    if isinstance(answers, dict):
                        given = answers.get(text)
                    else:
                        content = block.get("content")
                        if isinstance(content, list):
                            content = "\n".join(part.get("text", "") for part in content
                                                 if isinstance(part, dict) and isinstance(part.get("text"), str))
                        match = ANSWERED.fullmatch(content) if isinstance(content, str) and count == 1 else None
                        given = match.group(2) if match and match.group(1) == text else None
                    if block.get("is_error") or not isinstance(given, str):
                        continue
                    found = block["tool_use_id"] if offered and given == label else None
        if len(buffer) > MAX_READ:  # an oversized line is no answer
            skip, buffer = True, b""
    return found


# ---------------------------------------------------------------- the two dialogs the user saw and answered
# The plan the agent showed in plan mode (ExitPlanMode) with the user's answer, and a question the agent asked
# (AskUserQuestion) with the option the user chose. Both come from the main session's transcript only, as four
# sources: the plan (assistant, via plan_dialog), the answer to it (user, plan_dialog), the question (assistant,
# question_dialog) and the chosen option (user, question_dialog). One exact shape per Claude Code wording is accepted;
# anything else is dropped and counted (dialogs_unread). A free-text answer, a rejected plan's typed feedback and the
# plan file are never read, and the plugin's own questions are never sources.
PLAN_TOOL, QUESTION_TOOL = "ExitPlanMode", "AskUserQuestion"
VIA_OF = {PLAN_TOOL: "plan_dialog", QUESTION_TOOL: "question_dialog"}
VIAS = ("plan_dialog", "question_dialog")
DIALOG_SHAPES = {
    # the assistant record's tool_use block: its name, and the input keys it may hold
    "plan_call": {"seen": "2.1.327", "tool": PLAN_TOOL, "input": frozenset(("plan", "planFilePath"))},
    # a tool_result of an approved plan: no is_error, the fixed opening sentence, and toolUseResult with exactly these keys
    "plan_approved": {"seen": "2.1.327", "content": "User has approved your plan.",
                      "result": frozenset(("plan", "isAgent", "filePath"))},
    # a rejected plan, with or without typed feedback: is_error, the fixed opening sentence, toolUseResult a string and
    # the record's toolDenialKind "user-rejected". What follows the opening sentence is never read.
    "plan_rejected": {"seen": "2.1.327", "content": "The user doesn't want to proceed with this tool use.",
                      "denial": "user-rejected"},
    "question_call": {"seen": "2.1.327", "tool": QUESTION_TOOL, "input": frozenset(("questions",))},
    # the answer: no is_error, one of the two opening sentences Claude Code has used, and toolUseResult with exactly
    # these keys, the answers as {question: label}. Only that structured answer is read, never the sentence.
    "question_answered": {"seen": "2.1.327", "content": ("Your questions have been answered: ",
                                                         "User has answered your questions: "),
                          "result": frozenset(("questions", "answers", "annotations"))},
}
# The plugin's own questions: the held memory, the task, the draft and the setup questions' templates, and any question
# that holds the word recortex. The observer must not record the user's answers about the observer.
OWN_DIALOG = re.compile(r"(?i:recortex)|\A[ \t\r\n]*Memory [0-9a-f]{8} \([a-z]+\) on #[0-9]+: |"
                        r"\A[ \t\r\n]*Task [0-9a-f]{8}: (?:open a task |every step of #[0-9]+ is done)|"
                        r"\A[ \t\r\n]*Project Map [0-9a-f]{8}: |"
                        r"\A[ \t\r\n]*Draft [0-9a-f]{8}: update the ")
CALL_ID = re.compile(r"([A-Za-z0-9_-]{1,128})(?:\.([0-3]))?")
QUESTIONS_MAX, OPTIONS_MAX, QUESTION_CHARS, LABEL_CHARS = 4, 4, 2000, 300
FIND_BYTES = 2 * 1024 * 1024  # how far back in the transcript the call of a result is looked for when it is not known
FIND_SECONDS = 0.5
CALLS_CONTROL, UNREAD_CONTROL, CALLS_KEPT, STREAMS_KEPT = "dialog_calls", "dialogs_unread", 64, 32


def _flat(text):
    return " ".join(text.split())


def _call_blocks(record):
    """[(tool name, call id, input)] of the dialog calls of a main-session assistant record; a block that names a
    dialog tool but is not of an accepted shape comes back with a call id of None, so that it is counted."""
    if not _main_record(record, "assistant"):
        return []
    out = []
    for block in record["message"]["content"]:
        if (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in VIA_OF):
            ident = block.get("id")
            ok = isinstance(ident, str) and TOOL_ID.fullmatch(ident) and isinstance(block.get("input"), dict)
            out.append((block["name"], ident if ok else None, block.get("input") if ok else {}))
    return out


def _questions(tool_input):
    """[(question, header, [labels], multi)] of an AskUserQuestion input, or None when it is not of the accepted shape
    (1 to 4 questions of text, a header and 1 to 4 labeled options, within the size limits)."""
    shape = DIALOG_SHAPES["question_call"]["input"]
    items = tool_input.get("questions")
    if set(tool_input) - shape or not isinstance(items, list) or not 1 <= len(items) <= QUESTIONS_MAX:
        return None
    out = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip() \
                or len(item["question"]) > QUESTION_CHARS or not isinstance(item.get("options"), list) \
                or not 1 <= len(item["options"]) <= OPTIONS_MAX:
            return None
        labels = [o.get("label") for o in item["options"] if isinstance(o, dict)]
        if len(labels) != len(item["options"]) or not all(isinstance(x, str) and x.strip() and len(x) <= LABEL_CHARS
                                                          for x in labels):
            return None
        header = item.get("header")
        out.append((item["question"], header if isinstance(header, str) else "", labels, item.get("multiSelect") is True))
    return out


def _outcome(record, block):
    """(outcome, answers) of one tool_result block of a user record in a shape of DIALOG_SHAPES: "approved",
    "rejected" or "answered" (with the {question: label} answers), else (None, None)."""
    content, result = block.get("content"), record.get("toolUseResult")
    if not isinstance(content, str):
        return None, None
    approved, rejected = DIALOG_SHAPES["plan_approved"], DIALOG_SHAPES["plan_rejected"]
    answered_shape = DIALOG_SHAPES["question_answered"]
    if not block.get("is_error"):
        if (content.startswith(approved["content"]) and isinstance(result, dict) and set(result) == approved["result"]
                and result["isAgent"] is False and isinstance(result["plan"], str)):
            return "approved", None
        if (content.startswith(answered_shape["content"]) and isinstance(result, dict)
                and set(result) == answered_shape["result"] and isinstance(result["answers"], dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in result["answers"].items())):
            return "answered", dict(result["answers"])
        return None, None
    if (block.get("is_error") is True and content.startswith(rejected["content"]) and isinstance(result, str)
            and record.get("toolDenialKind") == rejected["denial"]):
        return "rejected", None
    return None, None


def _dialog(record):
    """The dialog parts of one transcript record, [] for anything else (a sidechain, a record Claude Code adds, a
    record with no dialog in it).

    An assistant record gives one part per call (a plan) or per question (a question): {side "call", name, via,
    tool_id, key (the call id, with ".n" for the nth question), text (the built source text, None when the plan call
    holds no plan or the question is the plugin's own or not single-choice), own, question, labels}. A call of a shape
    that is not accepted comes back as {side "call", unread True}. A user record gives one part per tool result:
    {side "result", tool_id, outcome, answers}; the outcome is None for a result that is not of an accepted shape."""
    if not isinstance(record, dict):
        return []
    parts = []
    for name, ident, tool_input in _call_blocks(record):
        if ident is None:
            parts.append({"side": "call", "unread": True})
        elif name == PLAN_TOOL:
            if set(tool_input) - DIALOG_SHAPES["plan_call"]["input"]:
                parts.append({"side": "call", "unread": True})
                continue
            plan = tool_input.get("plan")
            parts.append({"side": "call", "name": name, "via": "plan_dialog", "tool_id": ident, "key": ident,
                          "text": plan if isinstance(plan, str) and plan.strip() else None, "own": False,
                          "question": None, "labels": None})
        else:
            found = _questions(tool_input)
            if found is None:
                parts.append({"side": "call", "unread": True})
                continue
            for number, (question, header, labels, multi) in enumerate(found):
                own = bool(OWN_DIALOG.search("\n".join([question, header] + labels)))
                text = None
                if not own and not multi:
                    text = 'Asked: "%s" Options: %s' % (_flat(question), "; ".join('"%s"' % _flat(x) for x in labels))
                elif multi and not own:
                    parts.append({"side": "call", "unread": True})
                parts.append({"side": "call", "name": name, "via": "question_dialog", "tool_id": ident,
                              "key": "%s.%d" % (ident, number), "text": text, "own": own, "question": question,
                              "labels": labels})
    if _main_record(record, "user"):
        for block in record["message"]["content"]:
            if (isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str)
                    and TOOL_ID.fullmatch(block["tool_use_id"])):
                outcome, answers = _outcome(record, block)
                parts.append({"side": "result", "tool_id": block["tool_use_id"], "outcome": outcome, "answers": answers})
    return parts


def _dialog_id(session, role, raw, record, key):
    return hashlib.sha256((_identity(session, role, raw, record) + "\ndialog\n" + key + "\n" + role).encode()).hexdigest()


def _dialog_hash(record, role, key, via):
    content = (record.get("message") or {}).get("content")
    canonical = json.dumps([role, via, key, content], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _line_calls(path, call):
    """The dialog call parts of the record a call was seen in, read again through its frozen range and checked against
    its digest; None when the range is gone or changed or holds no such call."""
    try:
        raw = _read(path, call["offset"], call["end"] - call["offset"])
        if hashlib.sha256(raw).hexdigest() != call["sha256"]:
            return None
        parts = [p for p in _dialog(json.loads(raw)) if p["side"] == "call" and not p.get("unread")]
    except (SourceError, OSError, ValueError, UnicodeError, RecursionError, KeyError, TypeError):
        return None
    return parts or None


def _find_call(path, tool_id, before):
    """{name, offset, end, sha256} of the newest main-session dialog call with this id before byte `before`, looked for
    in the last FIND_BYTES of the transcript (a call the capture of an earlier scan saw, when the caller does not keep
    the calls), or None."""
    needle = tool_id.encode()
    start = max(0, before - FIND_BYTES)
    until = time.monotonic() + FIND_SECONDS
    position, buffer, buffer_start, found = start, b"", start, None
    skip = start > 0 and _read(path, start - 1, 1) != b"\n"
    while position < before:
        if time.monotonic() > until:
            return None
        chunk = _read(path, position, min(MAX_READ, before - position))
        if not chunk:
            break
        position += len(chunk)
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line, line_start = buffer[:newline + 1], buffer_start
            buffer, buffer_start = buffer[newline + 1:], buffer_start + newline + 1
            if skip:
                skip = False
                continue
            if needle in line and b"tool_use" in line:
                try:
                    parts = [p for p in _dialog(json.loads(line)) if p["side"] == "call" and not p.get("unread")]
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if any(p["tool_id"] == tool_id for p in parts):
                    found = {"name": parts[0]["name"], "offset": line_start, "end": line_start + len(line),
                             "sha256": hashlib.sha256(line).hexdigest()}
        if len(buffer) > MAX_READ:
            skip, buffer_start, buffer = True, buffer_start + len(buffer), b""
    return found


def _dialog_row(session, role, raw, record, path, offset, end, event, stamp, part_key, via, pair=None):
    row = {"source_id": _dialog_id(session, role, raw, record, part_key), "session_id": session, "role": role,
           "event_type": event, "path": path, "offset": offset, "end": end, "sha256": hashlib.sha256(raw).hexdigest(),
           "content_sha256": _dialog_hash(record, role, part_key, via), "observed_at": stamp, "via": via,
           "call_id": part_key}
    if pair:
        row.update(pair_offset=pair["offset"], pair_end=pair["end"], pair_sha256=pair["sha256"])
    return row


def _dialog_rows(session, event, path, record, raw, offset, end, stamp, calls, stats):
    """The descriptors of the dialog sources one record holds. `calls` (call id -> where its record is) is read and
    kept up to date; `stats` counts the dialogs that were not of an accepted shape."""
    out = []
    for part in _dialog(record):
        if part["side"] == "call":
            if part.get("unread"):
                stats["dialogs_unread"] = stats.get("dialogs_unread", 0) + 1
                continue
            calls[part["tool_id"]] = {"name": part["name"], "offset": offset, "end": end,
                                      "sha256": hashlib.sha256(raw).hexdigest()}
            if part["text"] and not part["own"]:
                out.append(_dialog_row(session, "assistant", raw, record, path, offset, end, event, stamp, part["key"],
                                       part["via"]))
            continue
        call = calls.get(part["tool_id"])
        if call is None and part["outcome"] is not None:
            call = _find_call(path, part["tool_id"], offset)
        if call is None:
            continue  # a result of some other tool, or a dialog whose call this transcript does not hold
        calls[part["tool_id"]] = call
        wanted = {"plan": {"approved", "rejected"}, "question": {"answered"}}
        kind = "plan" if call["name"] == PLAN_TOOL else "question"
        asked = _line_calls(path, call)
        if part["outcome"] not in wanted[kind] or asked is None:
            stats["dialogs_unread"] = stats.get("dialogs_unread", 0) + 1
            continue
        for ask in asked:
            if ask["tool_id"] != part["tool_id"] or ask["own"]:
                continue
            if kind == "question":
                label = (part["answers"] or {}).get(ask["question"])
                if ask["text"] is None or label not in ask["labels"]:
                    continue  # a free-text answer, or a label the call did not offer, is not a source
            out.append(_dialog_row(session, "user", raw, record, path, offset, end, event, stamp, ask["key"],
                                   ask["via"], pair=call))
    return out


def _kept_calls(store):
    kept = store.get_control(CALLS_CONTROL, [])
    return [item for item in kept if isinstance(item, dict) and isinstance(item.get("stream"), str)
            and isinstance(item.get("calls"), list)] if isinstance(kept, list) else []


def load_calls(store, stream):
    """The dialog calls kept for one transcript (`stream` is capture's name for it), as scan takes them."""
    try:
        entry = next((item for item in _kept_calls(store) if item["stream"] == stream), None)
        return {item["id"]: {"name": item["name"], "offset": item["offset"], "end": item["end"], "sha256": item["sha256"]}
                for item in (entry["calls"] if entry else []) if TOOL_ID.fullmatch(item["id"]) and item["name"] in VIA_OF
                and type(item["offset"]) is int and type(item["end"]) is int
                and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])}
    except (KeyError, TypeError, ValueError):
        return {}


def save_calls(store, stream, calls):
    """Keep the newest CALLS_KEPT calls of a transcript, and the newest STREAMS_KEPT transcripts (the control is a list,
    newest last, since the store writes its values with sorted keys)."""
    kept = [item for item in _kept_calls(store) if item["stream"] != stream]
    newest = sorted(calls.items(), key=lambda item: item[1]["offset"])[-CALLS_KEPT:]
    kept.append({"stream": stream, "calls": [dict(id=ident, **call) for ident, call in newest]})
    store.set_control(CALLS_CONTROL, kept[-STREAMS_KEPT:])


def add_unread(store, count):
    """Add `count` to the number of dialogs left out because their shape was not one of DIALOG_SHAPES."""
    if count:
        store.set_control(UNREAD_CONTROL, unread(store) + count)


def unread(store):
    value = store.get_control(UNREAD_CONTROL, 0)
    return value if type(value) is int and value >= 0 else 0


def capture_descriptors(data, project_root, event_type=None):
    """Compatibility helper for a bounded tail; scan+atomic cursor is preferred by hooks."""
    selected = dict(data)
    if event_type:
        selected["hook_event_name"] = event_type
    return scan(selected, project_root)[0]


def resolve(descriptor, project_root):
    """Resolve one frozen byte range and its digest. A model never supplies a source path."""
    if descriptor.get("event_type") == "source_gap":
        raise SourceError("source-gap")
    if descriptor.get("via"):
        return _resolve_dialog(descriptor, project_root)
    path = approved_path(descriptor.get("path"), project_root, descriptor.get("session_id"))
    subagent = os.sep + "subagents" + os.sep in path
    start, end = descriptor.get("offset"), descriptor.get("end")
    if type(start) is not int or type(end) is not int or end <= start:
        raise SourceError("source-range")
    raw = _read(path, start, end - start)
    if hashlib.sha256(raw).hexdigest() != descriptor.get("sha256"):
        raise SourceError("source-changed")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SourceError("source-format")
    role, text, typed, control = _content(record)
    if role != descriptor.get("role") or _identity(descriptor["session_id"], role, raw, record, subagent) != descriptor.get("source_id"):
        raise SourceError("source-identity")
    if descriptor.get("content_sha256") and descriptor["content_sha256"] != _content_hash(record, role, subagent):
        raise SourceError("source-identity")
    if not text:
        raise SourceError("source-empty")
    return {"id": descriptor["source_id"], "source_id": descriptor["source_id"], "role": role, "origin": role,
            "text": text, "direct_user": bool(typed and not subagent), "control_user": bool(control and not subagent),
            "session_id": descriptor["session_id"],
            "at": record.get("timestamp") if isinstance(record.get("timestamp"), str) else datetime.datetime.fromtimestamp(descriptor["observed_at"], datetime.timezone.utc).isoformat(),
            "sha256": descriptor["sha256"], "message_id": record.get("uuid"), "subagent": subagent,
            "sidechain": bool(record.get("isSidechain")), "via": "", "dialog_user": False}


PLAN_ANSWERS = {"approved": "Approved the plan in the plan dialog.", "rejected": "Rejected the plan in the plan dialog."}


def _resolve_dialog(descriptor, project_root):
    """Resolve one dialog source: the frozen range of its own record, and for the user's answer the range of the record
    that holds its call, both checked against their digests; the ids must match and both must be records of the main
    session. The text is built from fixed words and what the call offered, never from a free-text answer or from
    feedback typed with a rejected plan. A mismatch is "source-identity"."""
    session, role, via, key = (descriptor.get(name) for name in ("session_id", "role", "via", "call_id"))
    path = approved_path(descriptor.get("path"), project_root, session)
    if (os.sep + "subagents" + os.sep in path or os.path.basename(path) != session + ".jsonl" or via not in VIAS
            or role not in ("user", "assistant") or not isinstance(key, str) or not CALL_ID.fullmatch(key)):
        raise SourceError("source-identity")
    start, end = descriptor.get("offset"), descriptor.get("end")
    if type(start) is not int or type(end) is not int or end <= start:
        raise SourceError("source-range")
    raw = _read(path, start, end - start)
    if hashlib.sha256(raw).hexdigest() != descriptor.get("sha256"):
        raise SourceError("source-changed")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SourceError("source-format")
    tool_id = CALL_ID.fullmatch(key).group(1)
    if (not _main_record(record, role) or _dialog_id(session, role, raw, record, key) != descriptor.get("source_id")
            or descriptor.get("content_sha256") != _dialog_hash(record, role, key, via)):
        raise SourceError("source-identity")
    if role == "assistant":
        match = [p for p in _dialog(record) if p["side"] == "call" and not p.get("unread") and p["key"] == key]
        if len(match) != 1 or match[0]["via"] != via or not match[0]["text"] or match[0]["own"]:
            raise SourceError("source-identity")
        text = match[0]["text"]
    else:
        pair_start, pair_end, pair_sha = (descriptor.get(name) for name in ("pair_offset", "pair_end", "pair_sha256"))
        if type(pair_start) is not int or type(pair_end) is not int or pair_end <= pair_start or pair_start >= start \
                or not isinstance(pair_sha, str):
            raise SourceError("source-identity")
        paired = _read(path, pair_start, pair_end - pair_start)
        if hashlib.sha256(paired).hexdigest() != pair_sha:
            raise SourceError("source-identity")
        try:
            call_record = json.loads(paired)
        except (ValueError, UnicodeError):
            raise SourceError("source-identity")
        results = [p for p in _dialog(record) if p["side"] == "result" and p["tool_id"] == tool_id]
        asked = [p for p in _dialog(call_record) if p["side"] == "call" and not p.get("unread") and p["key"] == key]
        if len(results) != 1 or len(asked) != 1 or asked[0]["tool_id"] != tool_id or asked[0]["via"] != via \
                or asked[0]["own"]:
            raise SourceError("source-identity")
        outcome = results[0]["outcome"]
        if via == "plan_dialog":
            if asked[0]["name"] != PLAN_TOOL or outcome not in PLAN_ANSWERS:
                raise SourceError("source-identity")
            text = PLAN_ANSWERS[outcome]
        else:
            label = (results[0]["answers"] or {}).get(asked[0]["question"])
            if outcome != "answered" or asked[0]["text"] is None or label not in asked[0]["labels"]:
                raise SourceError("source-identity")
            text = 'Chose "%s" when asked "%s"' % (_flat(label), _flat(asked[0]["question"]))
    return {"id": descriptor["source_id"], "source_id": descriptor["source_id"], "role": role, "origin": role,
            "text": text, "direct_user": False, "control_user": False, "session_id": session,
            "at": record.get("timestamp") if isinstance(record.get("timestamp"), str) else datetime.datetime.fromtimestamp(
                descriptor["observed_at"], datetime.timezone.utc).isoformat(),
            "sha256": descriptor["sha256"], "message_id": record.get("uuid"), "subagent": False, "sidechain": False,
            "via": via, "call_id": key, "dialog_user": role == "user"}

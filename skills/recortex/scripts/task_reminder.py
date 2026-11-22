"""The task-time reminder on its own (1.7.0): one fixed line on a prompt of the main session when the prompt names work that
rests on a decision the record later replaced, and no review verdict exists for it yet.

It is the line that observer_advisory.change_pointer builds, with a light opt-in of its own instead of the observer's: a setup
question and a local setting, per repository on this machine. It needs what the brief hooks need (a configured, trusted
project and the local index) and nothing else: no observer entry, no level, no model, no runtime check, no capture readiness.
It makes no model call and posts nothing; it reads the prompt and the local copy of the record.

The setting is the user's own, in task-reminders.json in the private folder of the configuration top (storage.py), written
through a lock exactly as writing.py writes its file. The project file holds nothing of it. A file that cannot be read is never
replaced, and the hook then says nothing. No grant is needed to switch it on: it raises no level and takes no action (the
setup session records the user's answer with `setup.py task-reminder set --mode on --yes`).

Python 3 standard library only. The line carries addresses of the trusted repository, whole numbers and ids of fixed forms
(FORM) and the plugin's own words, never a word of the record: a pointer that does not fit FORM is not printed."""

import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import field_sheet  # noqa: E402
import names  # noqa: E402
import recortex  # noqa: E402
import storage  # noqa: E402

FORM_VERSION = 1
MODES = ("on", "off")
FILE_CAP = 20000
LOCK = "task-reminders.lock"
CAP = 900                    # characters of the line; it has a cap of its own (brief.CAPS is not touched)
PROMPT_CHARS = 48000
REMEMBERED = 20              # the fingerprints a session keeps, so that one item is said once a session
FINGERPRINT = re.compile(r"[0-9a-f]{16}")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
NOT_READABLE = {"storage": "the folder or the file is not safe to read", "form": "the file is not in the form the plugin "
                "writes", "large": "the file is larger than the plugin writes"}
ENTRY_KEYS = {"mode", "day"}


def path():
    return os.path.join(storage.config_top(), storage.PRIVATE, names.TASK_REMINDERS)


def empty():
    return {"form": FORM_VERSION, "repos": {}}


def real_day(value):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def load(create=False):
    """The validated file: {"form": 1, "repos": {"owner/name": {"mode": "on" | "off", "day": day}}}. Raises ValueError with a
    key of NOT_READABLE for a file that does not fit, and storage.StorageError for refused storage. With create=False nothing
    is made or tightened (the hooks)."""
    private = storage.folder(storage.config_top(), create=create, tighten=create)
    if private is None:
        return empty()
    target = os.path.join(private, names.TASK_REMINDERS)
    info = storage.check_path(target, "file")
    if not info:
        return empty()
    if info.st_size > FILE_CAP:
        raise ValueError("large")
    try:
        data = json.loads(storage.read_file(target) or "")
    except (ValueError, RecursionError):
        raise ValueError("form")
    if not isinstance(data, dict) or set(data) != {"form", "repos"} or type(data["form"]) is not int \
            or data["form"] != FORM_VERSION or not isinstance(data["repos"], dict):
        raise ValueError("form")
    for repo, entry in data["repos"].items():
        if not recortex.valid_repo(repo) or repo != repo.lower() or not isinstance(entry, dict) or set(entry) != ENTRY_KEYS \
                or entry["mode"] not in MODES or not real_day(entry["day"]):
            raise ValueError("form")
    return data


def read(repo):
    """{"state": "none" | "chosen" | "unreadable", "mode": "on" | "off" (off unless chosen), "day": the day or "", "why": a key
    of NOT_READABLE or ""} for `repo` on this machine. Read only; never raises."""
    try:
        data = load()
    except storage.StorageError:
        return {"state": "unreadable", "mode": "off", "day": "", "why": "storage"}
    except ValueError as error:
        return {"state": "unreadable", "mode": "off", "day": "", "why": str(error) if str(error) in NOT_READABLE else "form"}
    except OSError:
        return {"state": "unreadable", "mode": "off", "day": "", "why": "storage"}
    entry = data["repos"].get(repo.lower()) if isinstance(repo, str) else None
    if entry is None:
        return {"state": "none", "mode": "off", "day": "", "why": ""}
    return {"state": "chosen", "mode": entry["mode"], "day": entry["day"], "why": ""}


def record(repo, mode, day=None):
    """Record the user's choice for `repo` on this machine; other repositories' entries stay. A file that cannot be read stops
    it and is never replaced. Returns the day."""
    if mode not in MODES or not recortex.valid_repo(repo):
        raise ValueError("form")
    day = day or datetime.date.today().isoformat()
    with field_sheet.approvals_lock(name=LOCK):
        data = load(create=True)
        data["repos"][repo.lower()] = {"mode": mode, "day": day}
        text = json.dumps(data, indent=1, sort_keys=True) + "\n"
        if len(text.encode("utf-8")) > FILE_CAP:
            raise ValueError("large")
        storage.write_file(os.path.join(storage.folder(storage.config_top()), names.TASK_REMINDERS), text)
    return day


def sentence(repo, found=None):
    """One fixed sentence for what is chosen here."""
    found = found or read(repo)
    if found["state"] == "unreadable":
        return ("task reminder: the setting of this machine cannot be read (%s), so the line is not added to prompts. Run "
                "hooks.py --doctor." % NOT_READABLE[found["why"]])
    if found["state"] == "none":
        return "task reminder: no choice has been made for %s on this machine, so the line is not added to prompts." % repo
    return "task reminder: for %s on this machine it is %s (chosen on %s)." % (repo, found["mode"], found["day"])


# ---------------------------------------------------------------- the line

def form(repo):
    """The pattern that the line must match whole: the fixed wording of observer_advisory.change_pointer with its holes: one of
    its two sentences of why, three addresses in the trusted repository (an issue, with or without one of its comments), a group
    id and an item id of 64 hex digits. Explicit ASCII classes, matched with fullmatch; only the repository's name is matched
    without regard to case."""
    import observer_advisory
    url = (r"https://github\.com/(?i:" + re.escape(repo) + r")/issues/[1-9][0-9]{0,17}(?:#issuecomment-[1-9][0-9]{0,17})?")
    why = "(?:%s|%s)" % (re.escape(observer_advisory.WHY_DIRECT), re.escape(observer_advisory.WHY_SUGGESTED))
    return re.compile(re.escape(observer_advisory.POINTER_HEAD) + why + re.escape(" Earlier: ") + url + re.escape(" Current: ") +
                      url + re.escape(" Work: ") + url + re.escape(". Read guidance.py context ") + r"impact-[1-9][0-9]{0,18}" +
                      " " + r"[0-9a-f]{64}" + re.escape(observer_advisory.POINTER_TAIL))


def line(db, repo, allowed, prompt, entry, seconds, now=None):
    """(text, slow): the line to add to this prompt, or "", and whether the check ran out of time (then the caller says so
    once). `entry` is the session's entry of the prompt state: the fingerprints already told are kept in it, so that each
    item is told once a session. Nothing is printed that does not fit FORM and CAP."""
    import decision_impact
    import guidance
    import observer_advisory
    import observer_context
    if not isinstance(prompt, str) or not prompt.strip():
        return "", False
    fresh = observer_context.freshness(db, now)
    try:
        with decision_impact.time_limit(seconds, db):
            pointer = observer_advisory.change_pointer(db, repo, allowed, {"freshness": fresh}, prompt[:PROMPT_CHARS])
    except decision_impact.TooSlow:
        guidance.note_skip(repo, "prompt-submit", seconds)
        return "", True
    except Exception:  # noqa: BLE001 - a reminder that cannot be worked out is no reminder; the brief goes on
        return "", False
    if not pointer or not isinstance(pointer.get("fingerprint"), str):
        return "", False
    told = [item for item in entry.get("reminded_ids", []) if isinstance(item, str) and FINGERPRINT.fullmatch(item)]
    mark = pointer["fingerprint"][:16]
    if not FINGERPRINT.fullmatch(mark) or mark in told:
        return "", False
    entry["reminded_ids"] = (told + [mark])[-REMEMBERED:]
    text = pointer.get("text")
    if not isinstance(text, str) or len(text) > CAP or not form(repo).fullmatch(text):
        return "", False
    return text, False

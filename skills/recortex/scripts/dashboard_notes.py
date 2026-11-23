"""What the user did in the dashboard that a session may not have read (1.7.0).

The dashboard's local service (dashboard_action.py) records things on GitHub for the user: a completed step, a map edit, a
review verdict, an applied edit. A session that began before that does not know of it, and its local copy of the record may be
behind. Each of those writes one small note here, and the prompt hook (hooks.py) says once, on the next prompt of each session,
that something was recorded since the session's last prompt, with fixed words, counts and issue numbers, and the command that
reads it.

The file is `dashboard-notes.json` in the repository's folder of the hook state, beside prompt-state.json (storage.py: a private
folder, mode 0600, no link followed): {"form": 1, "notes": [{"at": seconds, "kind": one of KINDS, "issue": a whole number}]},
the newest 200, none older than seven days, at most 32,768 bytes. It holds no text of the record. The writer takes a lock of its
own; a note that cannot be written never fails a dashboard action (add returns False, and keeps the day of the failure in a file of
its own, `dashboard-notes-failed.json`, for hooks.py --doctor: a session that was never told is not silent about it there). The
reader is strict: a file that does not fit as a whole gives no note at all, and nothing is created by reading.

Standard library only."""

import json
import math
import os
import time

import field_sheet
import storage

FILE = "dashboard-notes.json"
FAILED_FILE = "dashboard-notes-failed.json"
FAILED_CAP = 200
LOCK = "dashboard-notes.lock"
FORM = 1
KINDS = ("step", "map", "verdict", "edit")
KEEP = 200
DAYS = 7
CAP_BYTES = 32768
CAP = 320                  # characters of the line it prints; it has a cap of its own
MOST_ISSUES = 6
ISSUE_MAX = 10 ** 18
TIME_MAX = 4102444800
WORDS = {"step": ("completed step", "completed steps"), "map": ("map edit", "map edits"),
         "verdict": ("review verdict", "review verdicts"), "edit": ("applied edit", "applied edits")}
HEAD = "recortex: since your last prompt the user recorded in the dashboard: "
TAIL = (". They are on GitHub and may not be in what you read: run status.py --sync and read them before you go on.")


def _folder(repo, create=False):
    return storage.folder(storage.state_top(), storage.repo_folder(repo.lower()), create=create, tighten=create)


def _fits(note):
    return (isinstance(note, dict) and set(note) == {"at", "kind", "issue"}
            and type(note["at"]) in (int, float) and math.isfinite(note["at"]) and 0 <= note["at"] < TIME_MAX
            and note["kind"] in KINDS and type(note["issue"]) is int and 1 <= note["issue"] <= ISSUE_MAX)


def _read(repo):
    """The notes of the file, or None when it does not fit as a whole (or cannot be read); [] when there is none. Read only."""
    folder = _folder(repo)
    if folder is None:
        return []
    path = os.path.join(folder, FILE)
    info = storage.check_path(path, "file")
    if not info:
        return []
    if info.st_size > CAP_BYTES:
        return None
    try:
        data = json.loads(storage.read_file(path) or "")
    except (ValueError, RecursionError):
        return None
    if (not isinstance(data, dict) or set(data) != {"form", "notes"} or data["form"] != FORM
            or not isinstance(data["notes"], list) or len(data["notes"]) > KEEP
            or not all(_fits(note) for note in data["notes"])):
        return None
    return data["notes"]


def note_failed(repo, now=None):
    """Keep the second at which a note could not be written: {"at": seconds}, beside the notes. True when kept. Never raises."""
    try:
        storage.write_file(os.path.join(_folder(repo, create=True), FAILED_FILE),
                           json.dumps({"at": int(time.time() if now is None else now)}) + "\n")
        return True
    except Exception:  # noqa: BLE001 - the mark is for the doctor; when it cannot be kept either, the action still goes on
        return False


def clear_failed(repo):
    """Remove the mark of a failure (a note was kept since). Never raises, and costs one look at the folder when there is none."""
    try:
        folder = _folder(repo)
        if folder is not None and os.path.lexists(os.path.join(folder, FAILED_FILE)):
            storage.remove_file(folder, FAILED_FILE)
    except Exception:  # noqa: BLE001
        pass


def failed(repo):
    """The second of the mark, or None when there is none or it is not exactly what note_failed writes. Read only."""
    try:
        folder = _folder(repo)
        path = os.path.join(folder, FAILED_FILE) if folder else None
        info = storage.check_path(path, "file") if path else False
        if not info or info.st_size > FAILED_CAP:
            return None
        value = json.loads(storage.read_file(path) or "")
        if isinstance(value, dict) and set(value) == {"at"} and type(value["at"]) is int and 0 < value["at"] < TIME_MAX:
            return value["at"]
    except Exception:  # noqa: BLE001
        pass
    return None


def add(repo, kind, issue, now=None):
    """Keep one note: this happened on `issue`. True when written; False when it was not (a kind off the list, an issue that is
    no whole number from 1 to 10 ** 18, a file that cannot be read or written; the last is kept for the doctor, see
    note_failed). Never raises: a dashboard action goes on."""
    try:
        if kind not in KINDS or type(issue) is not int or not 1 <= issue <= ISSUE_MAX:
            return False
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now < TIME_MAX:
            return False
        top = storage.state_top()
        with field_sheet.approvals_lock(top, LOCK):
            notes = _read(repo)
            if notes is None:
                notes = []   # a file that does not fit is not believed, and is replaced here: it holds nothing but these notes
            fresh = [note for note in notes if note["at"] > now - DAYS * 86400]
            fresh.append({"at": now, "kind": kind, "issue": issue})
            text = json.dumps({"form": FORM, "notes": fresh[-KEEP:]}) + "\n"
            storage.write_file(os.path.join(_folder(repo, create=True), FILE), text)
        clear_failed(repo)
        return True
    except Exception:  # noqa: BLE001 - a note must never fail the action it follows
        note_failed(repo)
        return False


def since(repo, after):
    """The notes made later than `after` (seconds), oldest first, that fit the form strictly; [] when there is none or the file
    does not fit as a whole or cannot be read. Read only; nothing is created."""
    try:
        if type(after) not in (int, float) or not math.isfinite(after):
            return []
        notes = _read(repo)
        return sorted((note for note in notes or [] if note["at"] > after), key=lambda note: note["at"])
    except Exception:  # noqa: BLE001 - no note, no line
        return []


def line(notes):
    """The one fixed line for `notes`, or "": the kinds that occurred, in the order of KINDS, each a count and its word; at most
    six distinct issue numbers, rising, then ", and more". Within CAP: when the numbers do not fit they are left out, and when
    even the counts do not, there is no line."""
    if not notes or not all(_fits(note) for note in notes):
        return ""
    counts = [(kind, sum(1 for note in notes if note["kind"] == kind)) for kind in KINDS]
    said = ", ".join("%d %s" % (count, WORDS[kind][0 if count == 1 else 1]) for kind, count in counts if count)
    issues = sorted({note["issue"] for note in notes})
    for shown in range(min(len(issues), MOST_ISSUES), -1, -1):
        where = ""
        if shown:
            numbers = ", ".join("#%d" % number for number in issues[:shown])
            more = ", and more" if len(issues) > shown else ""
            where = " (issue%s %s%s)" % ("" if len(issues) == 1 and shown == 1 else "s", numbers, more)
        text = HEAD + said + where + TAIL
        if len(text) <= CAP:
            return text
    return ""

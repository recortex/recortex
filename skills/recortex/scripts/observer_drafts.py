"""`observer.py drafts`: the drafts of the paper's text that wait for the user's answer.

The keeper (observer_keeper.py) only drafts and checks. This module is the way a draft becomes text, and it takes the
user's answer to do it: `drafts apply ID` needs the one-time grant that the hook records only from the user's own
"Approve" to the session's question about that draft (observer_hooks._grant_draft), bound to the hash of the bytes the
keeper recorded for it; without it the command is refused (`draft_unanswered`). The draft's files are checked against that
hash and copied into a fresh folder first, and a draft whose files changed is stale (`draft_stale`): nothing is written.
With it, `write.py apply` does what it always does: a fresh sync, the plan's rules again, the
live-body check, the backup, the version commit and the `text` comment in the existing chat form. No approval form is
added. `drafts later` and `drafts drop` record the other two answers and need no grant: declining writes nothing.
"""

import json
import os
import subprocess
import sys
import time

import keeper_rules
import recortex

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
WORDS = "words.txt"
# What write.py check and apply read from a folder. They are copied, as bytes, into a fresh folder of this draft's own,
# and the copy is used only when draft.md, used.json and memory.json have the hash the keeper recorded: the user
# approves exactly the bytes they were shown, whatever happens to the keeper's folder meanwhile.
COPIED = ("base.md", "draft.md", "used.json", "memory.json", "start.json")
FILE_CAP = 4 * 1024 * 1024
STEP_SECONDS = 240
# What `write.py apply` says when the text, or what it rests on, changed while the draft waited: the draft is stale.
STALE_SIGNS = ("what this text rests on changed while it waited",
               "not posted: the live body of",
               "is not one that this folder's own write.py start made")
NOTE = ("Ask the user about each draft with the multiple-choice question tool, one question per draft, from the "
        "question listed here (give it exactly as listed) with the options Approve, Not now and Drop it. Show what "
        "would change first, from observer.py drafts --show ID. For Approve, run observer.py drafts apply ID right "
        "after the answer, alone in its command, with no second question and no dry run in between. For Not now, run "
        "observer.py drafts later ID; for Drop it, observer.py drafts drop ID.")


def emit(value):
    print(json.dumps(value, sort_keys=True, ensure_ascii=True))


def _item(draft):
    return {"id": draft["id"], "topic": draft["topic"], "headings": draft["headings"], "state": draft["state"],
            "question": keeper_rules.question(draft["id"], draft["headings"], draft["topic"]),
            "options": list(keeper_rules.DRAFT_OPTIONS)}


def listing(store):
    """The drafts that wait (those put off with "Not now" whose time has passed included), as the session asks them."""
    waiting = [_item(entry["draft"]) for entry in store.waiting() if entry["kind"] == "draft"]
    emit({"drafts": waiting, "note": NOTE})
    return 0


def _run(args, project_root):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_root
    try:
        ran = subprocess.run([sys.executable, os.path.join(SCRIPTS, "write.py")] + args, capture_output=True, text=True,
                             cwd=project_root, env=env, timeout=STEP_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return ran.returncode, ran.stdout + ran.stderr


def _open(store, ident):
    draft = store.draft(ident) if isinstance(ident, str) else None
    return draft if draft is not None and draft["state"] in ("waiting", "later") else None


def _bytes(path):
    """The bytes of a plain file, read without following a link; None for anything else or a file over FILE_CAP."""
    import stat
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_CAP:
            return None
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > FILE_CAP:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def approved_copy(store, repo, draft):
    """(folder, content hash) of a fresh copy of the draft's files whose approved bytes have the hash the keeper
    recorded, or (None, None) when they do not (or the hash or a file is missing): the draft is then stale."""
    import storage
    content = keeper_rules.recorded(store.get_control(keeper_rules.CONTENTS, []), draft["id"])
    files = {name: _bytes(os.path.join(draft["folder"], name)) for name in COPIED}
    if content is None or any(value is None for value in files.values()) or keeper_rules.content_hash(files) != content:
        return None, None
    copy = storage.cache_folder(repo, "keeper", draft["id"] + "-approved")
    recortex.write_working_files(copy, files)   # the bytes just checked, never re-read from the keeper's folder
    return copy, content


def _stale(store, ident):
    store.set_draft(ident, "stale", reason="stale")
    emit({"code": "draft_stale", "note": keeper_rules.STALE_LINE})
    return 1


def show(store, ident, repo, project_root):
    """What `write.py check` prints for the draft: the change by paragraph and why. Text of the record: under the data
    header, as data."""
    import index
    draft = _open(store, ident)
    if draft is None:
        emit({"code": "draft_unknown"})
        return 1
    try:
        copy, _ = approved_copy(store, repo, draft)
    except SystemExit:
        copy = None
    if copy is None:
        return _stale(store, ident)
    print(index.DATA_HEADER)
    code, output = _run(["check", "--repo", repo, "--dir", copy], project_root)
    print(output.rstrip())
    return 0 if code == 0 else 1


def later(store, ident, now=None):
    draft = _open(store, ident)
    if draft is None:
        emit({"code": "draft_unknown"})
        return 1
    until = (time.time() if now is None else now) + keeper_rules.LATER_DAYS * 86400
    store.set_draft(ident, "later", until=until)
    emit({"action": "later", "draft": ident, "until_days": keeper_rules.LATER_DAYS})
    return 0


def drop(store, ident):
    draft = _open(store, ident)
    if draft is None:
        emit({"code": "draft_unknown"})
        return 1
    store.set_draft(ident, "dropped", reason="dropped")
    emit({"action": "drop", "draft": ident})
    return 0


def apply(store, ident, repo, project_root):
    """Write the draft, once, on the user's recorded "Approve". Returns the exit code."""
    draft = _open(store, ident)
    if draft is None:
        emit({"code": "draft_unknown"})
        return 1
    try:
        copy, content = approved_copy(store, repo, draft)   # before anything is written: the bytes the user was shown
    except SystemExit:
        copy, content = None, None
    if copy is None:
        return _stale(store, ident)
    if not store.take_grant(keeper_rules.grant_key(ident, content)):
        import observer_grants
        emit(observer_grants.refused(
            {"code": "draft_unanswered"}, note="No answer of the user's counts for this draft: ask the question listed by "
            "observer.py drafts, and run this command right after the user answers Approve."))
        return 1
    try:
        recortex.write_working_files(copy, {WORDS: "Approve\n"})
    except SystemExit:
        emit({"code": "draft_folder"})
        return 1
    code, output = _run(["apply", "--repo", repo, "--dir", copy, "--words-file", os.path.join(copy, WORDS),
                         "--who", "the author"], project_root)
    lines = [line for line in output.splitlines() if line.strip()][-12:]
    if code == 0:
        store.set_draft(ident, "applied")
        store.log_act("draft_applied", issue=draft["topic"])   # the text is written: a failed log is counted, nothing more
        emit({"action": "apply", "draft": ident, "output": lines})
        return 0
    if any(sign in output for sign in STALE_SIGNS):
        store.set_draft(ident, "stale", reason="stale")
        emit({"code": "draft_stale", "note": keeper_rules.STALE_LINE})
        return 1
    emit({"code": "not_written", "draft": ident, "output": lines})
    return 1

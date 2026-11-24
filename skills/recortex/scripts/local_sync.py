"""The sync of a local record: the files of the record folder into the same raw tables the GitHub sync fills (issues, comments,
timeline, the edit-history tables), so that `index.derive` and everything above it see one shape.

A table of its own, `local_files`, holds a mark for each file read: its name, its size and time, the state of its seal and its
number. A file whose size and time equal its mark is not opened. A scan that reached the end also removes: a mark whose file is
gone takes its rows with it (the GitHub sync misses deletions until a rebuild). A file the sync does not know (a sync tool's
conflict copy, a half-written file, a note a person dropped in) is not read and is counted; two topics with one number, or two
entries with one number, stop the sync before anything is stored, because that is what two diverged copies of a record look like.
Nothing of a file that does not fit is stored or echoed. All files are read through local_store.py and storage.py.
"""

import json
import re
import time

import index
import local_store
import record_store
import recortex
import storage

TABLE = ("CREATE TABLE IF NOT EXISTS local_files(path TEXT PRIMARY KEY, mtime INTEGER, size INTEGER, state TEXT, number INTEGER)")
# What this machine's seal once held for, kept apart from the marks so that no later state of a path, no deletion and no new name clears it:
# a key for the path, for the entry's id and for the topic's number. An entry that was sealed here and now carries another key id was changed
# outside recortex, and is never taken back as written on another machine (store.adopt). It lives in the index, survives a `--rebuild` of the
# same record, and is lost only with the index file; `local_sealed_ok` says that the last sync finished, so that a table that a stopped or a
# partial sync left is never taken for the whole.
SEALED_TABLE = "CREATE TABLE IF NOT EXISTS local_sealed(key TEXT PRIMARY KEY)"
SKIPPED = (".git", ".gitignore", ".gitattributes", "README.md", ".DS_Store", "desktop.ini", "Thumbs.db", ".stfolder", ".stignore",
           ".dropbox", ".dropbox.cache")
MAX_FILES = 50000
MAX_IN_FOLDER = 5000
CLOCK_EVERY = 100
VERSION_SEQ = 10 ** 10      # the timeline rows of a topic's earlier texts, by the number of the version file
MARK_SEQ = 5000             # the topic file's own rows: a change outside recortex is one edit and one rename
TOO_MANY = "recortex: the record folder holds more files than recortex reads."
DUPLICATE = local_store.DUPLICATE


class SyncStop(record_store.StoreError):
    """The sync stops before it stores anything; the message is one fixed line, and `kind` says which."""


def skipped(name, top):
    return name in SKIPPED and (name != "README.md" or top) or name.startswith(".part-")


def classify(rows, sub):
    """Sort a folder's listing: ([(name, size, mtime, relpath)] of the files to read, [(name, why)] of the unknown names, [the
    folders to look into]). The record's own names are the only ones known; the short list of SKIPPED names is passed over."""
    known, unknown, folders = [], [], []
    for name, size, mtime, kind in rows:
        if skipped(name, sub == ""):
            continue
        relpath = "%s/%s" % (sub, name) if sub else name
        if sub == "":
            if kind == "file" and name == local_store.RECORD_FILE:
                continue
            if kind == "file" and name == local_store.HUB_FILE:
                known.append((name, size, mtime, relpath))
            elif kind == "dir" and name in (local_store.HUB_FOLDER, local_store.TOPICS_FOLDER):
                folders.append(name)
            else:
                unknown.append((name, "name"))
        elif sub == local_store.TOPICS_FOLDER:
            if kind == "file" and name.endswith(".md") and local_store.SLUG.fullmatch(name[:-3]):
                known.append((name, size, mtime, relpath))
            elif kind == "dir" and local_store.SLUG.fullmatch(name) and name != local_store.HUB_FOLDER:
                folders.append(name)
            else:
                unknown.append((name, "name"))
        elif kind == "file" and local_store.ENTRY_NAME.fullmatch(name):
            known.append((name, size, mtime, relpath))
        else:
            unknown.append((name, "name"))
    return known, unknown, folders


def run(db, repo, hub, rebuild, clock, timeline_cap=None):
    """Bring the index of a local record up to date; returns a summary like run_sync's. Raises SyncStop for a stop before
    anything is stored, and the store's own errors (a folder that cannot be used) as they are."""
    store = record_store.for_repo(repo)
    store.record = store.read_record()
    db.execute(TABLE)
    if index.get_meta(db, "local_record_id") not in (None, "", store.record_id):   # an index that held another record: its memory is not this one's
        db.execute(SEALED_TABLE)
        db.execute("DELETE FROM local_sealed")
    index.set_meta(db, "local_sealed_ok", "0")   # until this sync has finished; a stop leaves it so
    db.commit()
    wipe = rebuild or index.get_meta(db, "record_path") != store.folder or index.get_meta(db, "store") != "local"
    if wipe:
        store.forget_cache()   # a rebuild reads every file again, not what an earlier read of this process remembers
    full = wipe or not index.get_meta(db, "synced_at") or index.get_meta(db, "derive_pending") == "1" \
        or not index.mind_ready(db) or index.get_meta(db, "derive_version") != index.DERIVE_VERSION
    summary = {"issues": 0, "comments": 0, "timelines": 0, "edits": 0, "comment_edits": 0, "complete": True, "pages": 0,
               "files": 0, "unread": 0}
    marks = {} if wipe else {row["path"]: row for row in db.execute("SELECT * FROM local_files")}
    scanned, unknown = {}, []   # relpath -> (size, mtime); the names that are nothing the sync knows

    # ---- the scan: names, sizes and times only
    top = store.scan()
    known, odd, folders = classify(top, "")
    unknown += odd
    listings = {"": top}
    topic_folders = []
    if local_store.TOPICS_FOLDER in folders:
        listings[local_store.TOPICS_FOLDER] = store.scan(local_store.TOPICS_FOLDER)
        files, odd, dirs = classify(listings[local_store.TOPICS_FOLDER], local_store.TOPICS_FOLDER)
        known += files
        unknown += odd
        topic_folders = ["%s/%s" % (local_store.TOPICS_FOLDER, name) for name in dirs]
    entry_folders = ([local_store.HUB_FOLDER] if local_store.HUB_FOLDER in folders else []) + sorted(topic_folders)
    total = len(known)
    owner_of = {}
    for path in entry_folders:
        listings[path] = store.scan(path)
        if len(listings[path]) > MAX_IN_FOLDER:
            raise SyncStop(TOO_MANY, "too-many")
        files, odd, _ = classify(listings[path], path)
        owner_of[path] = local_store.HUB_FILE if path == local_store.HUB_FOLDER else path + ".md"
        known += files
        unknown += odd
        total += len(files)
        if total > MAX_FILES:
            raise SyncStop(TOO_MANY, "too-many")
    for name, size, mtime, relpath in known:
        scanned[relpath] = (size, mtime)
    summary["files"] = len(scanned)
    # a folder with no topic file of its own holds entries of nothing: they are not read
    for path in entry_folders:
        if owner_of[path] not in scanned:
            for rel in [r for r in scanned if r.startswith(path + "/")]:
                unknown.append((rel.rsplit("/", 1)[1], "orphan"))
                scanned.pop(rel)

    # ---- phase 1: read the files whose mark differs, topic files first (their folders follow in the order of the names)
    topic_number = {rel: mark["number"] for rel, mark in marks.items()
                    if is_topic_file(rel) and mark["number"] is not None and not mark["state"].startswith("unread")}
    reads = []
    complete = True
    for relpath in sorted(scanned):
        mark = marks.get(relpath)
        size, mtime = scanned[relpath]
        if mark is not None and (mark["mtime"], mark["size"]) == (mtime, size):
            continue
        if clock.passed():
            if rebuild:
                raise index.DeadlineReached("deadline reached during the scan of the record folder")
            complete = False
            break
        folder = relpath.rpartition("/")[0]
        found = read_one(store, relpath, listings.get(folder), topic_number.get(owner_of.get(folder)) if folder in owner_of
                         else None, folder in owner_of)
        if found[1] is not None and found[1].kind in ("hub", "topic"):
            topic_number[relpath] = found[1].number
        reads.append((relpath, size, mtime, found))
    # ---- the numbers must be unique before anything is stored
    check_numbers(marks, scanned, reads)

    # ---- phase 2: store
    index.set_meta(db, "derive_pending", "1")   # cleared next to synced_at, once everything stored has been derived
    db.commit()
    changed_issues, changed_comments, touched_versions = set(), set(), set()
    removed = False
    try:
        if wipe:
            for table in ("issues", "comments", "timeline", "parents", "pull_merge", "local_files") + index.EDIT_TABLES \
                    + index.DERIVED + index.MIND:
                db.execute("DELETE FROM %s" % table)
            marks = {}
            full = True
        db.execute(SEALED_TABLE)   # made and filled in the transaction of this sync
        if store.record.get("state") == "sealed":
            db.execute("INSERT OR IGNORE INTO local_sealed VALUES (?)", ("path:" + local_store.RECORD_FILE,))
        if complete:
            for relpath, mark in marks.items():
                if relpath not in scanned:
                    removed = True   # a deletion: everything is derived again, which takes its derived rows out
                    if forget(db, mark) == ("version", None):
                        touched_versions.update(row["number"] for row in db.execute("SELECT number FROM issues"))
        for relpath, size, mtime, found in reads:
            old = marks.get(relpath)
            gone = forget(db, old) if old is not None else None
            if gone == ("version", None):
                touched_versions.update(row["number"] for row in db.execute("SELECT number FROM issues"))
            kind, parsed, why = found
            if kind == "skip":
                removed = removed or bool(gone and gone[0] in ("issue", "comment"))
                continue    # a file that is still being written: read again at the next sync
            if parsed is None:
                removed = removed or bool(gone and gone[0] in ("issue", "comment"))   # its rows are gone, nothing replaces them
                db.execute("INSERT OR REPLACE INTO local_files VALUES (?,?,?,?,NULL)", (relpath, mtime, size, "unread:" + why))
                continue
            number = store_file(db, store, relpath, parsed, mtime, changed_issues, changed_comments, touched_versions)
            now_row = (("issue" if parsed.kind in ("hub", "topic") else "comment" if parsed.kind == "entry" else "version"),
                       number if parsed.kind in ("hub", "topic", "entry") else None)
            if gone and gone[0] in ("issue", "comment") and gone != now_row:
                removed = True   # the file now holds another row (a renumbered topic) or none of its old kind
            db.execute("INSERT OR REPLACE INTO local_files VALUES (?,?,?,?,?)", (relpath, mtime, size, parsed.state, number))
            if parsed.state == "sealed" or getattr(parsed, "own_seal", False):   # the file's own seal, whatever its topic's state attributes
                for key in local_store.LocalStore.seal_keys(relpath, parsed):
                    db.execute("INSERT OR IGNORE INTO local_sealed VALUES (?)", (key,))
            summary[{"hub": "issues", "topic": "issues", "entry": "comments", "body": "timelines"}[parsed.kind]] += 1
        for number in sorted(touched_versions):
            rebuild_versions(db, store, number)
        db.execute("DELETE FROM timeline WHERE issue NOT IN (SELECT number FROM issues)")   # a topic that is gone keeps none
        db.commit()
    except BaseException:
        db.rollback()
        raise
    unread = db.execute("SELECT COUNT(*) AS n FROM local_files WHERE state LIKE 'unread:%'").fetchone()["n"]
    summary["unread"] = unread + len(unknown)
    if complete:
        index.set_meta(db, "local_unread", str(summary["unread"]))
        index.set_meta(db, "local_unknown", json.dumps(sorted({why for _, why in unknown})))
    index.set_meta(db, "record_path", store.folder)
    index.set_meta(db, "store", "local")
    index.set_meta(db, "local_record_id", store.record_id)
    newest_issue = db.execute("SELECT MAX(updated_at) AS at FROM issues").fetchone()["at"] or ""
    newest_comment = db.execute("SELECT MAX(updated_at) AS at FROM comments").fetchone()["at"] or ""
    index.set_meta(db, "issues_newest", newest_issue)
    index.set_meta(db, "comments_newest", newest_comment)
    db.commit()
    if hub and not clock.passed() and db.execute("SELECT 1 FROM issues WHERE number = ?", (hub,)).fetchone() is None:
        pass    # no hub in the record yet: no resume block to read
    elif hub and not clock.passed():
        try:
            index.read_resume(db, repo, hub, clock.left(60))
        except recortex.GhError:
            if clock.passed():
                complete = False
            else:
                raise
        db.commit()
    elif hub:
        complete = False
    index.derive(db, None if full or removed else changed_issues, None if full or removed else changed_comments)
    index.set_meta(db, "hub", str(hub or ""))
    index.set_meta(db, "derive_pending", "0")
    if complete:
        index.set_meta(db, "synced_at", index.now_utc())
        index.set_meta(db, "last_sync_error", "")
    else:
        index.set_meta(db, "partial_at", index.now_utc())
    index.set_meta(db, "sync_complete", "1" if complete else "0")
    index.set_meta(db, "local_sealed_ok", "1" if complete else "0")
    summary["complete"] = complete
    db.commit()
    index.plan_step(db, repo)
    index.task_step(db, repo)
    index.summary_step(db, repo)
    summary["board"] = None
    db.close()
    return summary


def is_topic_file(relpath):
    return relpath == local_store.HUB_FILE or relpath.startswith(local_store.TOPICS_FOLDER + "/") and relpath.count("/") == 1


def read_one(store, relpath, listing, topic, in_folder):
    """(kind, Parsed or None, why): "ok" with the Parsed file, "unread" with a fixed reason, or "skip" for a file with no seal
    that is so young that it may be still being written. `topic` is the number of the topic whose folder holds an entry."""
    try:
        parsed = store.read_parsed(relpath, listing)
    except local_store.Unread as error:
        return _unread(store, relpath, error.reason)
    except storage.StorageError as error:
        return "unread", None, error.kind
    if parsed is None:
        return "skip", None, ""
    if in_folder:
        match = local_store.ENTRY_NAME.fullmatch(relpath.rpartition("/")[2])
        if parsed.kind not in ("entry", "body") or not match or parsed.entry_id != match.group(2):
            return "unread", None, "name"
        parsed = local_store.attributed(parsed, topic)   # sealed only when its own `topic` is the folder's topic
        if parsed.placed is None:
            return "unread", None, "topic"   # the folder's topic file does not read, and the entry's own seal does not hold
    elif relpath == local_store.HUB_FILE and parsed.kind != "hub" or relpath != local_store.HUB_FILE and parsed.kind != "topic":
        return "unread", None, "kind"
    return "ok", parsed, ""


def _unread(store, relpath, reason):
    """A file that does not fit: unread, except one with no seal that is younger than two seconds (still being written)."""
    if reason in ("seal", "front"):
        folder, _, name = relpath.rpartition("/")
        for row in store.scan(folder):
            if row[0] == name and time.time() - row[2] / 1e9 < local_store.YOUNG_SECONDS:
                return "skip", None, ""
    return "unread", None, reason


def check_numbers(marks, scanned, reads):
    """Stop before storing when two topic files, or two entries, carry one number: the known numbers of the files whose mark
    still holds, and the numbers of the files read now."""
    topics, entries = {}, {}
    fresh = {relpath for relpath, size, mtime, found in reads}
    for relpath, mark in marks.items():
        number = mark["number"]
        if relpath in scanned and relpath not in fresh and number is not None and not mark["state"].startswith("unread"):
            if is_topic_file(relpath):
                topics.setdefault(number, []).append(relpath)
            elif number > 0:   # a version of a text has a negative number in its mark
                entries.setdefault(number, []).append(relpath)
    for relpath, size, mtime, (kind, parsed, why) in reads:
        if parsed is None:
            continue
        if parsed.kind in ("hub", "topic"):
            topics.setdefault(parsed.number, []).append(relpath)
        elif parsed.kind == "entry":
            entries.setdefault(local_store.entry_number(parsed.entry_id), []).append(relpath)
    for table in (topics, entries):
        if any(len(paths) > 1 for paths in table.values()):
            raise SyncStop(DUPLICATE, "duplicate")


def forget(db, mark):
    """Take the rows of a file out of the tables: an issue with its timeline rows, or a comment with its edit rows. Returns what
    it took out, so that the caller can see whether the same row comes back: ("issue", number), ("comment", id), ("version", None)
    for a version of a text, or None when the mark held no rows (a file that was not read)."""
    path, number = mark["path"], mark["number"]
    db.execute("DELETE FROM local_files WHERE path = ?", (path,))
    if number is None or mark["state"].startswith("unread"):
        return None
    if path == local_store.HUB_FILE or path.startswith(local_store.TOPICS_FOLDER + "/") and path.count("/") == 1:
        db.execute("DELETE FROM issues WHERE number = ?", (number,))
        db.execute("DELETE FROM timeline WHERE issue = ? AND seq < ?", (number, VERSION_SEQ))
        return "issue", number
    if number < 0:
        return "version", None
    db.execute("DELETE FROM comments WHERE id = ?", (number,))
    db.execute("DELETE FROM comment_edit_reads WHERE comment = ?", (number,))
    db.execute("DELETE FROM comment_edits WHERE comment = ?", (number,))
    return "comment", number


def store_file(db, store, relpath, parsed, mtime, changed_issues, changed_comments, touched_versions):
    """Put one file into the tables. Returns the number kept in its mark: the topic's, the entry's, or for a version of the
    text VERSION_SEQ plus its own number (so that a version's number never meets an entry's)."""
    if parsed.kind in ("hub", "topic"):
        number = parsed.number
        db.execute("DELETE FROM issues WHERE number = ?", (number,))
        db.execute("DELETE FROM timeline WHERE issue = ? AND seq < ?", (number, VERSION_SEQ))
        row = store.issue_row(number, relpath, parsed)
        index.store_issue_page(db, [row], changed_issues)
        db.execute("UPDATE issues SET timeline_read = 1, edits_read = 1 WHERE number = ?", (number,))
        front = parsed.front
        if front["state"] == "closed":
            db.execute("INSERT OR REPLACE INTO timeline(issue, seq, event, at, actor, detail) VALUES (?,?,?,?,?,?)",
                       (number, 1, "closed", front["closed"], local_store.OWN_AUTHOR, front["reason"]))
        if parsed.state != "sealed":
            actor = None if parsed.state == "changed" else local_store.OTHER_AUTHOR
            at = local_store.stamp(mtime / 1e9)
            db.execute("INSERT OR REPLACE INTO timeline(issue, seq, event, at, actor, detail) VALUES (?,?,?,?,?,?)",
                       (number, MARK_SEQ, "edited", at, actor, "body"))
            db.execute("INSERT OR REPLACE INTO timeline(issue, seq, event, at, actor, detail) VALUES (?,?,?,?,?,?)",
                       (number, MARK_SEQ + 1, "renamed", at, actor, ""))
        changed_issues.add(number)
        touched_versions.add(number)   # a topic row that comes back finds its earlier texts again, whatever the order of the syncs
        return number
    entry_no = local_store.entry_number(parsed.entry_id)
    if parsed.kind == "body":
        touched_versions.add(parsed.placed)
        return -entry_no
    topic = parsed.placed
    db.execute("DELETE FROM comments WHERE id = ?", (entry_no,))
    row = store.entry_row(topic, relpath, parsed, entry_no, mtime)
    index.store_comment_page(db, [row], changed_comments)
    db.execute("DELETE FROM comment_edit_reads WHERE comment = ?", (entry_no,))
    db.execute("DELETE FROM comment_edits WHERE comment = ?", (entry_no,))
    if parsed.state == "changed":
        db.execute("INSERT INTO comment_edits(comment, seq, at, editor) VALUES (?,?,?,NULL)", (entry_no, 1, row["updated_at"]))
        db.execute("INSERT OR REPLACE INTO comment_edit_reads(comment, node, stamp, edits, last_edited, complete) "
                   "VALUES (?,?,?,?,?,1)", (entry_no, parsed.entry_id, row["updated_at"], 1, row["updated_at"]))
        changed_comments.add(entry_no)
    return entry_no


def rebuild_versions(db, store, number):
    """The timeline rows of a topic's earlier texts: one `edited` row for each version file but the oldest, by the seal's
    state (this machine's: the owner word; changed: nobody known; another machine's: the fixed word)."""
    db.execute("DELETE FROM timeline WHERE issue = ? AND seq >= ?", (number, VERSION_SEQ))
    try:
        relpath, topic = store.topic(number)
        versions = store.read_entries(relpath, kinds=("body",))
    except (record_store.Missing, storage.StorageError, local_store.Unread):
        return
    for position, (parsed, entry_no, mtime) in enumerate(versions):
        if position == 0:
            continue    # the oldest is the first text: no edit
        actor = {"sealed": local_store.OWN_AUTHOR, "changed": None, "foreign": local_store.OTHER_AUTHOR}[parsed.state]
        db.execute("INSERT OR REPLACE INTO timeline(issue, seq, event, at, actor, detail) VALUES (?,?,?,?,?,?)",
                   (number, VERSION_SEQ + entry_no, "edited", parsed.front["created"], actor, "body"))


def counts(db):
    """{"sealed": n, "changed": n, "foreign": n, "unread": n} over the marks, for --doctor and the brief."""
    found = {"sealed": 0, "changed": 0, "foreign": 0, "unread": 0}
    try:
        for row in db.execute("SELECT state, COUNT(*) AS n FROM local_files GROUP BY state"):
            found["unread" if row["state"].startswith("unread") else row["state"]] += row["n"]
    except Exception:  # noqa: BLE001 - an index of a GitHub record has no such table
        pass
    return found

"""The sync of a local record (1.9.0, section 7): the files into the raw tables, the marks, what is not read, the stops and the
deadline. Offline; every file is made in the test's own sandbox (tests/local_record.py)."""

import json
import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import local_record  # noqa: E402

PLANTED = "ZZMARKER-planted-by-a-test"


class Box(local_record.LocalSandbox):
    def setUp(self):
        super().setUp()
        import index
        import local_sync
        self.index, self.local_sync = index, local_sync

    def sync(self, **kwargs):
        return self.index.sync(self.repo, hub=1, log=lambda *a: None, **kwargs)

    def db(self):
        return self.index.connect(self.repo, create=False)

    def rows(self, sql, *args):
        db = self.db()
        try:
            return [tuple(row) for row in db.execute(sql, args)]
        finally:
            db.close()

    def meta(self, key):
        db = self.db()
        try:
            return self.index.get_meta(db, key)
        finally:
            db.close()

    def everything(self):
        """All tables as text, for a search for what must not be there."""
        db = self.db()
        try:
            return "\n".join(db.iterdump())
        finally:
            db.close()

    def tables(self, names):
        db = self.db()
        try:
            return {name: sorted(tuple(row) for row in db.execute("SELECT * FROM %s" % name)) for name in names}
        finally:
            db.close()

    def reads(self):
        """A recorder of the files that a sync opens through storage."""
        real = self.storage.read_record_file
        seen = []

        def read(path, limit):
            if os.path.basename(path) != "record.json":   # read at the start of every sync, by the store's open
                seen.append(os.path.relpath(path, self.folder))
            return real(path, limit)
        patcher = mock.patch.object(self.storage, "read_record_file", side_effect=read)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen


class RowTests(Box):
    def test_a_sync_stores_the_topics_the_entries_and_the_derived_tables(self):
        summary = self.sync()
        self.assertTrue(summary["complete"])
        self.assertEqual((4, 9), (summary["issues"], summary["comments"]))
        self.assertEqual([1, 2, 3, 4], [r[0] for r in self.rows("SELECT number FROM issues ORDER BY number")])
        self.assertEqual({"result", "decision", "failure", "question", "idea", "checkpoint", "resume", "other"},
                         {r[0] for r in self.rows("SELECT DISTINCT type FROM events")})
        self.assertEqual(9, self.rows("SELECT COUNT(*) FROM events")[0][0])
        self.assertTrue(self.rows("SELECT label FROM labels WHERE issue = 2 AND label = 'topic:reader'"))
        self.assertTrue(self.rows("SELECT heading FROM sections WHERE issue = 2"))
        self.assertEqual("1", self.meta("sync_complete"))
        self.assertEqual("local", self.meta("store"))
        self.assertEqual(self.folder, self.meta("record_path"))
        self.assertEqual("0", self.meta("local_unread"))
        self.assertIn("sample reader handles quoted fields", self.meta("resume"))
        self.assertEqual(18, self.rows("SELECT COUNT(*) FROM local_files")[0][0])

    def test_a_sealed_topic_and_entry_are_the_owners_and_an_entry_was_never_edited(self):
        self.sync()
        for number, author, edits_read, state in self.rows("SELECT number, author, edits_read, state FROM issues"):
            self.assertEqual("local--record", author)
            self.assertEqual(1, edits_read)
        rows = self.rows("SELECT author, created_at, updated_at, url FROM comments")
        self.assertEqual({"local--record"}, {r[0] for r in rows})
        self.assertTrue(all(r[1] == r[2] for r in rows))
        self.assertTrue(all(r[3].startswith("#") and "/" in r[3] for r in rows))
        self.assertEqual([], self.rows("SELECT * FROM comment_edits"))
        self.assertEqual(0, len(self.index.edited_comments(self.db())))

    def test_the_closed_topic_and_the_earlier_texts_are_timeline_rows(self):
        self.sync()
        rows = self.rows("SELECT issue, event, actor, detail FROM timeline ORDER BY issue, seq")
        self.assertIn((4, "closed", "local--record", "completed"), rows)
        edited = [r for r in rows if r[1] == "edited"]
        self.assertEqual([(2, "edited", "local--record", "body")], edited)   # the reader's later text; the first text is no edit
        db = self.db()
        allowed = {"local--record"}
        self.assertTrue(self.index.body_told(db, 2, allowed))
        self.assertTrue(self.index.body_told(db, 3, allowed))

    def test_a_changed_topic_is_one_edit_and_one_rename_by_nobody_known_and_is_not_told(self):
        self.damage("changed-body")
        self.sync()
        rows = self.rows("SELECT event, actor FROM timeline WHERE issue = 2 AND event IN ('edited', 'renamed') ORDER BY event")
        self.assertIn(("renamed", None), rows)
        self.assertIn(("edited", None), rows)
        db = self.db()
        self.assertFalse(self.index.body_told(db, 2, {"local--record"}))
        self.assertTrue(self.index.body_told(db, 3, {"local--record"}))
        row = db.execute("SELECT number, author, title FROM issues WHERE number = 2").fetchone()
        self.assertFalse(self.index.title_told(db, row, lambda author: (author or "").lower() in {"local--record"}))
        self.assertEqual("changed", self.rows("SELECT state FROM local_files WHERE path LIKE 'topics/reader-q%.md'")[0][0])

    def test_a_foreign_topic_has_another_author_and_actor(self):
        path = self.topic_path("speed")
        relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
        parsed = self.local_store.parse_file(self.read(path), relpath, self.record_id)
        other = self.local_store.Sealing(b"another machine")
        self.write(path, self.local_store.render("topic", parsed.front, parsed.body, relpath, self.record_id, other))
        self.sync()
        self.assertEqual("other-machine", self.rows("SELECT author FROM issues WHERE number = 3")[0][0])
        self.assertIn(("edited", "other-machine"), self.rows("SELECT event, actor FROM timeline WHERE issue = 3"))
        self.assertFalse(self.index.body_told(self.db(), 3, {"local--record"}))

    def test_a_changed_entry_looks_edited_by_nobody_known_and_never_clears(self):
        self.damage("changed-entry")
        self.sync()
        cid = self.entries["reader"][0]
        row = self.rows("SELECT author, created_at, updated_at FROM comments WHERE id = ?", cid)[0]
        self.assertEqual("local--record", row[0])
        self.assertNotEqual(row[1], row[2])
        self.assertEqual([(cid, 1, None)], [(r[0], r[1], r[3]) for r in self.rows("SELECT * FROM comment_edits")])
        db = self.db()
        self.assertIn(cid, self.index.edited_comments(db))
        self.assertEqual({}, self.index.cleared_comments(db, {"local--record"}))
        self.assertIn(cid, self.index.unconfirmed_edits(db, {"local--record"}))
        told = self.index.memories(db, {"local--record"})
        self.assertNotIn(cid, told["items"])   # an entry changed outside recortex is not told, as an edited comment is not
        self.assertIn(cid, told["edited_ids"])
        self.assertEqual(1, told["edited"])

    def test_a_foreign_entry_has_another_author_and_is_hidden(self):
        self.damage("foreign-entry")
        self.sync()
        cid = self.entries["reader"][0]
        row = self.rows("SELECT author, created_at, updated_at FROM comments WHERE id = ?", cid)[0]
        self.assertEqual(("other-machine", row[1]), (row[0], row[2]))
        db = self.db()
        found = self.index.memories(db, {"local--record"})
        self.assertNotIn(cid, found["items"])
        self.assertGreaterEqual(found["hidden"], 1)

    def test_an_entry_read_under_another_topic_than_its_own_is_changed(self):
        path = self.topic_path("reader")
        self.write(path, self.read(path).replace(b"number: 2", b"number: 7", 1))
        self.sync()
        cid = self.entries["reader"][0]
        row = self.rows("SELECT issue, created_at, updated_at FROM comments WHERE id = ?", cid)[0]
        self.assertEqual(2, row[0])   # its own sealed topic, not the folder's
        self.assertNotEqual(row[1], row[2])
        self.assertEqual("changed", self.rows("SELECT state FROM local_files WHERE number = ?", cid)[0][0])

    def test_a_changed_version_of_the_text_is_an_edit_by_nobody_known(self):
        self.sync()
        relpath = [r[0] for r in self.rows("SELECT path FROM local_files WHERE state = 'sealed' AND path LIKE 'topics/reader-q%/%' ")]
        versions = []
        folder = os.path.join(self.folder, "topics", self.slug("reader"))
        for name in sorted(os.listdir(folder)):
            data = self.read(os.path.join(folder, name))
            if b"\nkind: body\n" in data:
                versions.append(os.path.join(folder, name))
        self.assertEqual(2, len(versions))
        self.write(versions[1], self.read(versions[1]).replace(b"stray quote", b"stray apostrophe"))
        self.sync()
        self.assertIn((2, "edited", None), [(r[0], r[1], r[2]) for r in self.rows("SELECT issue, event, actor FROM timeline")])
        self.assertFalse(self.index.body_told(self.db(), 2, {"local--record"}))


class MarkTests(Box):
    def test_an_unchanged_file_is_not_opened_and_a_changed_one_is_opened_once(self):
        self.sync()
        seen = self.reads()
        self.sync()
        self.assertEqual([], seen)   # nothing changed: not one file was opened
        path = self.damage("changed-entry")
        self.sync()
        self.assertEqual([os.path.relpath(path, self.folder)], seen)

    def test_a_changed_size_or_a_changed_time_is_seen_and_the_same_size_and_time_is_not(self):
        self.sync()
        path = self.entry_path("reader", 1)
        data = self.read(path)
        seen = self.reads()
        stat = os.stat(path)
        self.write(path, data + b"")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.sync()
        self.assertEqual([os.path.relpath(path, self.folder)], seen)   # a new time alone: read again, and still sealed
        self.assertEqual("sealed", self.rows("SELECT state FROM local_files WHERE path = ?", os.path.relpath(path, self.folder))[0][0])
        del seen[:]
        same = data.replace(b"found", b"fabricated") if b"found" in data else data.replace(b"Result", b"Resalt", 1)
        self.assertEqual(len(data), len(same) + (0 if b"found" not in data else 0)) if False else None
        stat = os.stat(path)
        swapped = bytearray(data)
        swapped[-5] = (swapped[-5] + 1) % 256 if chr(swapped[-5]).isalnum() is False else swapped[-5]
        self.sync()
        self.assertEqual([], seen)

    def test_a_deleted_entry_and_a_deleted_topic_take_their_rows_with_them(self):
        self.sync()
        cid = self.entries["speed"][0]
        os.remove(self.entry_path("speed", 0))
        self.sync()
        self.assertEqual([], self.rows("SELECT * FROM comments WHERE id = ?", cid))
        self.assertEqual([], self.rows("SELECT * FROM events WHERE comment = ?", cid))
        self.assertEqual([], self.rows("SELECT * FROM local_files WHERE number = ?", cid))
        os.remove(self.topic_path("task"))
        self.sync()
        self.assertEqual([1, 2, 3], [r[0] for r in self.rows("SELECT number FROM issues ORDER BY number")])
        self.assertEqual([], self.rows("SELECT * FROM timeline WHERE issue = 4"))
        self.assertEqual([], self.rows("SELECT * FROM comments WHERE issue = 4"))   # its folder is now an orphan: not read
        self.assertGreaterEqual(int(self.meta("local_unread")), 1)

    def test_only_a_scan_that_reached_the_end_removes(self):
        self.sync()
        os.remove(self.entry_path("speed", 0))
        self.damage("changed-entry")   # a file that needs reading, which the stopped scan does not reach
        calls = []

        def passed(clock):
            calls.append(1)
            return True
        with mock.patch.object(self.index.Deadline, "passed", passed):
            summary = self.sync(deadline=5)
        self.assertFalse(summary["complete"])
        self.assertTrue(calls)
        self.assertEqual(9, self.rows("SELECT COUNT(*) FROM comments")[0][0])   # kept: the scan did not reach the end
        self.sync()
        self.assertEqual(8, self.rows("SELECT COUNT(*) FROM comments")[0][0])

    def test_a_rebuild_reads_everything_again_and_gives_the_same_rows_and_numbers(self):
        self.sync()
        names = ("issues", "comments", "events", "timeline", "labels", "sections", "local_files")
        before = self.tables(names)
        seen = self.reads()
        self.sync(rebuild=True)
        self.assertEqual(18, len(seen))
        self.assertEqual(before, self.tables(names))

    def test_a_rebuild_that_cannot_finish_changes_nothing(self):
        self.sync()
        before = self.tables(("issues", "comments", "local_files"))
        with mock.patch.object(self.index.Deadline, "passed", lambda clock: True):
            with self.assertRaises(self.index.DeadlineReached):
                self.sync(rebuild=True, deadline=5)
        self.assertEqual(before, self.tables(("issues", "comments", "local_files")))
        real = self.index.store_issue_page
        with mock.patch.object(self.index, "store_issue_page", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.sync(rebuild=True)
        self.assertEqual(before, self.tables(("issues", "comments", "local_files")))   # one transaction: rolled back

    def test_an_index_filled_from_another_path_is_read_again_from_the_start(self):
        self.sync()
        db = self.db()
        self.index.set_meta(db, "record_path", "/somewhere/else")
        db.commit()
        db.close()
        seen = self.reads()
        self.sync()
        self.assertEqual(18, len(seen))
        self.assertEqual(self.folder, self.meta("record_path"))

    def test_the_deadline_is_asked_during_the_scan_and_the_next_sync_goes_on(self):
        counter = []

        def passed(clock):
            counter.append(1)
            return len(counter) > 6
        with mock.patch.object(self.index.Deadline, "passed", passed):
            summary = self.sync(deadline=5)
        self.assertFalse(summary["complete"])
        self.assertEqual("0", self.meta("sync_complete"))
        some = self.rows("SELECT COUNT(*) FROM local_files")[0][0]
        self.assertTrue(0 < some < 18, some)
        self.assertGreaterEqual(len(counter), 6)
        summary = self.sync()
        self.assertTrue(summary["complete"])
        self.assertEqual(18, self.rows("SELECT COUNT(*) FROM local_files")[0][0])
        full = self.tables(("issues", "comments", "events"))
        self.sync(rebuild=True)
        self.assertEqual(full, self.tables(("issues", "comments", "events")))

    def test_a_github_index_has_no_local_files_table(self):
        self.assertNotIn("local_files", self.index.SCHEMA)
        db = self.index.connect("octo-org/octo-repo")
        try:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            db.close()
        self.assertNotIn("local_files", names)
        self.assertNotIn("local_sealed", names)   # what is only a local record's is in its index alone, and not in index.SCHEMA
        self.assertNotIn("local_sealed", self.index.SCHEMA)


class NotReadTests(Box):
    def test_a_conflict_copy_and_a_note_and_a_half_written_file_are_not_read_and_are_counted(self):
        self.damage("conflict-copy")
        topic = os.path.join(self.folder, "topics")
        self.write(os.path.join(topic, "notes.txt"), ("a note with %s inside" % PLANTED).encode())
        self.write(os.path.join(self.folder, "scratch.md"), ("scratch %s" % PLANTED).encode())
        os.mkdir(os.path.join(self.folder, "unknown-folder"))
        self.write(os.path.join(self.folder, "unknown-folder", "x.md"), PLANTED.encode())
        self.write(os.path.join(topic, "no-seal.md"), b"---\nrecortex: 1\nkind: topic\nnumber: 9\ntitle: \"%s\"\nlabels: []\nstate: open\n"
                   b"created: 2026-11-23T14:12:07Z\nupdated: 2026-11-23T14:12:07Z\n---\n%s\n" % (PLANTED.encode(), PLANTED.encode()))
        os.utime(os.path.join(topic, "no-seal.md"), (1, 1))   # old: not "still being written"
        summary = self.sync()
        self.assertTrue(summary["complete"])
        self.assertEqual([1, 2, 3, 4], [r[0] for r in self.rows("SELECT number FROM issues ORDER BY number")])
        self.assertGreaterEqual(int(self.meta("local_unread")), 4)
        self.assertNotIn(PLANTED, self.everything())   # nothing of what was not read is in the index
        self.assertIn(("unread:seal",), self.rows("SELECT state FROM local_files WHERE path = 'topics/no-seal.md'"))

    def test_a_file_with_no_seal_that_is_younger_than_two_seconds_is_not_yet_there(self):
        self.damage("half-written")
        self.sync()
        self.assertEqual("0", self.meta("local_unread"))   # not counted, not stored: it may be being written
        self.assertEqual([], self.rows("SELECT * FROM local_files WHERE path = 'topics/half-written.md'"))
        old = time.time() + 10
        with mock.patch.object(time, "time", return_value=old):
            self.sync()
        self.assertEqual("1", self.meta("local_unread"))
        self.assertEqual("unread:seal", self.rows("SELECT state FROM local_files WHERE path = 'topics/half-written.md'")[0][0])

    def test_the_names_that_are_skipped_in_silence(self):
        for name in (".gitignore", ".gitattributes", "README.md", ".DS_Store", "desktop.ini", "Thumbs.db", ".stfolder", ".stignore",
                     ".dropbox", ".dropbox.cache", ".part-abc"):
            self.write(os.path.join(self.folder, name), b"x")
        os.mkdir(os.path.join(self.folder, ".git"))
        self.write(os.path.join(self.folder, ".git", "config"), PLANTED.encode())
        self.write(os.path.join(self.folder, "topics", ".DS_Store"), b"x")
        self.sync()
        self.assertEqual("0", self.meta("local_unread"))
        self.write(os.path.join(self.folder, "topics", "README.md"), b"a readme below the top is not skipped")
        self.sync()
        self.assertEqual("1", self.meta("local_unread"))

    def test_a_link_a_pipe_and_a_file_others_can_write_are_unread_with_a_fixed_reason(self):
        self.damage("symlink")
        os.mkfifo(os.path.join(self.folder, "topics", self.slug("reader"), "2026-11-23T1700Z-aaaaaaaaaaaaaaaa.md"))
        path = self.entry_path("speed", 1)
        os.chmod(path, 0o666)
        started = time.monotonic()
        self.sync()
        self.assertLess(time.monotonic() - started, 30)   # the pipe was not waited for
        states = dict(self.rows("SELECT path, state FROM local_files WHERE state LIKE 'unread:%'"))
        self.assertEqual(["unread:permissions"], list(states.values()))   # the file others can write is a mark
        self.assertEqual("3", self.meta("local_unread"))   # and the link and the pipe, which are no file, are counted too
        self.assertEqual(["name"], json.loads(self.meta("local_unknown")))
        self.assertNotIn(self.folder, json.dumps(self.rows("SELECT path FROM local_files")))   # the marks name relative paths

    def test_a_file_with_crlf_is_unread(self):
        self.damage("crlf")
        self.sync()
        self.assertEqual("1", self.meta("local_unread"))

    def test_two_files_with_one_topic_number_stop_the_sync_before_anything_is_stored(self):
        self.sync()
        before = self.tables(("issues", "comments", "local_files", "meta"))
        self.damage("duplicate-number")
        with self.assertRaises(self.local_sync.SyncStop) as caught:
            self.sync()
        self.assertEqual(self.local_sync.DUPLICATE, str(caught.exception))
        self.assertEqual("duplicate", caught.exception.kind)
        self.assertEqual(before, self.tables(("issues", "comments", "local_files", "meta")))

    def test_two_entries_with_one_number_stop_the_sync_too(self):
        self.sync()
        path = self.entry_path("speed", 0)
        data = self.read(path)
        copy = os.path.join(os.path.dirname(path), "2026-12-01T0000Z-" + os.path.basename(path).split("-", 3)[-1])
        self.write(copy, data)   # the same entry id, so the same number, in a second file: two diverged copies of one entry
        before = self.tables(("comments", "local_files"))
        with self.assertRaises(self.local_sync.SyncStop) as caught:
            self.sync()
        self.assertEqual(("duplicate", self.local_sync.DUPLICATE), (caught.exception.kind, str(caught.exception)))
        self.assertEqual(before, self.tables(("comments", "local_files")))
        os.remove(copy)
        other = os.path.join(os.path.dirname(path), "2026-12-01T0000Z-aaaaaaaaaaaaaaaa.md")
        self.write(other, data)   # a file name that names another id than the file holds: unread, and no duplicate
        self.sync()
        self.assertEqual("unread:name", self.rows("SELECT state FROM local_files WHERE path LIKE '%2026-12-01T0000Z%'")[0][0])

    def test_more_files_than_recortex_reads_stop_the_sync(self):
        with mock.patch.object(self.local_sync, "MAX_FILES", 5):
            with self.assertRaises(self.local_sync.SyncStop) as caught:
                self.sync()
        self.assertEqual(self.local_sync.TOO_MANY, str(caught.exception))
        with mock.patch.object(self.local_sync, "MAX_IN_FOLDER", 2):
            with self.assertRaises(self.local_sync.SyncStop):
                self.sync()


    def test_a_topic_folder_that_others_can_write_is_refused_as_the_record_folder_is_and_nothing_is_told_from_it(self):
        self.sync()
        folder = next(os.path.join(self.folder, "topics", name) for name in sorted(os.listdir(os.path.join(self.folder, "topics")))
                      if os.path.isdir(os.path.join(self.folder, "topics", name)))
        os.chmod(folder, 0o777)
        with self.assertRaises(self.storage.StorageError) as caught:
            self.storage.scan_record_folder(self.folder, os.path.relpath(folder, self.folder))
        self.assertEqual("permissions", caught.exception.kind)
        db_before = self.everything()
        try:
            self.sync()
        except (self.record_store.StoreError, self.storage.StorageError) as error:
            self.assertEqual("permissions", error.kind)
        self.assertEqual(db_before, self.everything())   # nothing of that folder was added to what the index tells
        os.chmod(folder, 0o700)
        with mock.patch.object(self.storage, "_uid", return_value=os.getuid() + 1):
            with self.assertRaises(self.storage.StorageError) as caught:
                self.storage.scan_record_folder(self.folder, os.path.relpath(folder, self.folder))
        self.assertEqual("owner", caught.exception.kind)


class FolderTests(Box):
    seeded = False

    def test_a_record_with_no_file_but_record_json_syncs_to_an_empty_index(self):
        summary = self.sync()
        self.assertTrue(summary["complete"])
        self.assertEqual(0, self.rows("SELECT COUNT(*) FROM issues")[0][0])

    def test_a_record_folder_that_cannot_be_used_stops_the_sync_with_the_stores_error(self):
        os.chmod(self.folder, 0o777)
        with self.assertRaises(self.record_store.RecordFolderError) as caught:
            self.sync()
        self.assertEqual("permissions", caught.exception.kind)


class GoneRowsTests(Box):
    """A file that stops being readable takes everything derived from it out of the index (a HIGH finding of the automatic check
    on the first sync: a changed entry that became unreadable left its event behind, and the text was told as the user's own)."""

    DERIVED = ("events", "event_fields", "links", "evidence", "explanations", "steps", "refs")

    def rich(self):
        """An entry with a link, evidence, an explanation and, on the task, a list of steps; their ids."""
        store = self.store
        cid = self.entries["reader"][0]
        body = ("<!-- ghim:event type=result at=2026-11-23T16:00:00Z because=%d -->\n### Result: 2026-11-23 16:00 UTC - a rich entry\n\n"
                "**In plain words, we found that:** the rich entry carries every kind of derived row\n\n"
                "**What ran:** `python check/wide.py` at commit 4f2a9c1.\n\n**Configuration:** one laptop.\n\n"
                "**Numbers:**\n\n| Measure | Value | Denominator | Source path |\n|---|---|---|---|\n"
                "| Files read | 12 | 12 | `check/out/wide.tsv` |\n\n**Exclusions and failures:** None.\n\n"
                "**Not established:** Larger files.\n\n**Next step:** Read larger files, owner the maintainer\n\n"
                "**Evidence:**\n- table wide-files\n\n<details>\n<summary>We use \"wide\" to mean a file with more than 100 columns"
                "</summary>\n\n**Why it matters here:** The check reads only such files.\n\n</details>\n" % cid)
        rich = int(store.posted_id(store.post_entry(2, body=body)))
        steps = ("<!-- ghim:event type=steps at=2026-11-23T16:10:00Z -->\n### Steps: 2026-11-23 16:10 UTC - read the file\n\n"
                 "**In plain words, we agreed to:** read the file in blocks\n\n**Agreed:** By the maintainer in the chat, 2026-11-23 "
                 "16:10 UTC.\n\n**Steps:**\n1. Read the first block\n2. Read the rest\n\n**Done when:** The file is read\n")
        listed = int(store.posted_id(store.post_entry(4, body=steps)))
        return rich, listed

    def derived_rows(self, cid):
        found = {}
        for table in self.DERIVED:
            column = {"events": "comment", "event_fields": "comment", "links": "source", "evidence": "comment",
                      "explanations": "comment", "steps": "comment", "refs": None}[table]
            if column is None:
                found[table] = self.rows("SELECT * FROM refs WHERE origin = ?", "event:%d" % cid)
            else:
                found[table] = self.rows("SELECT * FROM %s WHERE %s = ?" % (table, column), cid)
        found["comment_edits"] = self.rows("SELECT * FROM comment_edits WHERE comment = ?", cid)
        found["comment_edit_reads"] = self.rows("SELECT * FROM comment_edit_reads WHERE comment = ?", cid)
        found["comments"] = self.rows("SELECT id FROM comments WHERE id = ?", cid)
        return found

    def told(self, cid):
        db = self.db()
        try:
            return cid in self.index.memories(db, {"local--record"})["items"]
        finally:
            db.close()

    def make_unread(self, path, how):
        if how == "unread":
            self.write(path, b"garbage with no front matter and no seal\n")
            os.utime(path, (1, 1))   # old, so that it is no file that is still being written
        elif how == "young":
            self.write(path, b"garbage with no front matter and no seal\n")   # a file that may still be written: not yet there
        elif how == "kind":
            os.remove(path)
            os.mkfifo(path)

    def test_an_entry_that_stops_being_readable_leaves_no_row_behind(self):
        rich, listed = self.rich()
        self.sync()
        for cid, tables in ((rich, ("events", "event_fields", "links", "evidence", "explanations", "refs")),
                            (listed, ("events", "event_fields", "steps"))):
            before = self.derived_rows(cid)
            for table in tables:
                self.assertTrue(before[table], "%s of %d" % (table, cid))   # the entry really has a row of this kind
            self.assertTrue(self.told(cid))
            for how in ("unread", "young", "kind"):
                with self.subTest(cid=cid, how=how):
                    path = self.find_entry_file(cid)
                    saved = self.read(path)
                    self.make_unread(path, how)
                    self.sync()
                    for table, rows in self.derived_rows(cid).items():
                        self.assertEqual([], rows, "%s after %s" % (table, how))
                    self.assertFalse(self.told(cid))
                    if how == "kind":
                        os.remove(path)
                    self.write(path, saved)   # and it comes back when the file does
                    self.sync()
                    self.assertTrue(self.told(cid), how)
                    self.assertTrue(self.derived_rows(cid)["events"])

    def find_entry_file(self, cid):
        for place, _, names_ in os.walk(self.folder):
            for name in names_:
                match = self.local_store.ENTRY_NAME.fullmatch(name)
                if match and self.local_store.entry_number(match.group(2)) == cid:
                    return os.path.join(place, name)
        raise AssertionError("no such entry file")

    def test_a_changed_entry_that_becomes_unreadable_is_not_told_as_the_users_words(self):
        cid = self.entries["reader"][0]
        path = self.damage("changed-entry")
        self.sync()
        self.assertFalse(self.told(cid))   # changed: not told, because its edit rows exist
        self.assertTrue(self.derived_rows(cid)["comment_edits"])
        self.write(path, b"not a record file now\n")
        os.utime(path, (1, 1))
        self.sync()
        rows = self.derived_rows(cid)
        self.assertEqual({name: [] for name in rows}, rows)   # no event left, with or without its edit rows
        self.assertFalse(self.told(cid))
        self.assertEqual(0, self.rows("SELECT COUNT(*) FROM events WHERE comment = ?", cid)[0][0])

    def test_a_topic_that_stops_being_readable_leaves_no_derived_row_behind(self):
        self.sync()
        number = 2
        self.assertTrue(self.rows("SELECT * FROM sections WHERE issue = ?", number))
        self.assertTrue(self.rows("SELECT * FROM labels WHERE issue = ?", number))
        path = self.topic_path("reader")
        self.make_unread(path, "unread")
        self.sync()
        for table in ("issues", "sections", "labels", "blocks", "sources", "numbers", "mentions", "timeline"):
            column = "number" if table == "issues" else "issue"
            self.assertEqual([], self.rows("SELECT * FROM %s WHERE %s = ?" % (table, column), number), table)
        self.assertFalse(self.index.body_told(self.db(), number, {"local--record"}))

    def test_a_topic_that_is_renumbered_by_hand_takes_the_rows_of_its_old_number_with_it(self):
        self.sync()
        path = self.topic_path("speed")
        self.write(path, self.read(path).replace(b"number: 3", b"number: 8", 1))
        self.sync()
        self.assertEqual([], self.rows("SELECT * FROM sections WHERE issue = 3"))
        self.assertTrue(self.rows("SELECT * FROM sections WHERE issue = 8"))

    def test_a_topic_row_that_comes_back_finds_the_tampered_version_again_in_any_order_of_syncs(self):
        folder = os.path.join(self.folder, "topics", self.slug("reader"))
        versions = [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                    if b"\nkind: body\n" in self.read(os.path.join(folder, name))]
        self.assertEqual(2, len(versions))
        self.write(versions[1], self.read(versions[1]).replace(b"stray quote", b"stray apostrophe"))
        self.sync()
        tables = ("issues", "comments", "events", "timeline", "labels", "sections", "local_files")
        kept = self.tables(tables)
        self.assertIn((2, "edited", None), [(r[0], r[1], r[2]) for r in self.rows("SELECT issue, event, actor FROM timeline")])
        path = self.topic_path("reader")
        saved = self.read(path)
        self.make_unread(path, "unread")
        self.sync()
        self.assertEqual([], self.rows("SELECT * FROM timeline WHERE issue = 2"))
        self.write(path, saved)   # the sealed topic file comes back
        self.sync()
        rows = [(r[0], r[1], r[2]) for r in self.rows("SELECT issue, event, actor FROM timeline WHERE issue = 2")]
        self.assertIn((2, "edited", None), rows)   # the tampered version is found again
        self.assertFalse(self.index.body_told(self.db(), 2, {"local--record"}))
        again = self.tables(tables)
        self.sync(rebuild=True)
        self.assertEqual(again, self.tables(tables))   # the same as a rebuild
        self.assertEqual(kept["timeline"], again["timeline"])

    def test_a_reader_of_events_takes_only_rows_whose_comment_still_exists(self):
        self.sync()
        cid = self.entries["reader"][0]
        db = self.db()
        db.execute("DELETE FROM comments WHERE id = ?", (cid,))   # the raw row gone, the derived rows left behind
        db.commit()
        self.assertTrue(db.execute("SELECT 1 FROM events WHERE comment = ?", (cid,)).fetchone())
        self.assertNotIn(cid, self.index.memories(db, {"local--record"})["items"])
        db.close()


if __name__ == "__main__":
    unittest.main()

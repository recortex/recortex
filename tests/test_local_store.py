"""The record folder's rules (1.9.0, section 6 of the spec): where a local record may be, what is checked on the way, how a file is
read and written, the write lock and the machine's binding of a record id to a folder. Offline; every folder is made in the test's
own temporary folder, and HOME and the XDG folders point into it."""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "recortex", "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.dont_write_bytecode = True
import local_record  # noqa: E402
import storage  # noqa: E402

ID = "k3f5x2m7q4w6z5ab"


class Box(unittest.TestCase):
    """A sandbox: the homes in a folder under .tmp/, and a place for record folders in the system's temporary folder (the
    folders above a record are judged, and a checkout made with a loose umask is not a place the rules accept)."""

    def setUp(self):
        base = os.path.join(ROOT, ".tmp")
        os.makedirs(base, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="test-lstore-", dir=base)
        self.place = tempfile.mkdtemp(prefix="recortex-place-")
        os.chmod(self.place, 0o755)
        self.addCleanup(self.forget)
        environment = {"HOME": self.dir, "XDG_STATE_HOME": os.path.join(self.dir, "state"),
                       "XDG_CACHE_HOME": os.path.join(self.dir, "cache"), "XDG_CONFIG_HOME": os.path.join(self.dir, "config"),
                       "XDG_DATA_HOME": os.path.join(self.dir, "data")}
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("RECORTEX_TESTING", "RECORTEX_DATA_HOME"):
            os.environ.pop(name, None)

    def forget(self):
        import shutil
        shutil.rmtree(self.dir, True)
        shutil.rmtree(self.place, True)

    def folder(self, name="rec", mode=0o755):
        path = os.path.join(self.place, name)
        os.makedirs(path)
        os.chmod(path, mode)
        return path

    def put(self, path, data=b"x", mode=0o644):
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)
        return path


class PlaceTests(Box):
    def test_the_records_top_follows_the_data_home_and_ignores_a_relative_one(self):
        self.assertEqual(os.path.join(self.dir, "data", "recortex"), storage.records_top())
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "relative/path"}):
            self.assertEqual(os.path.join(self.dir, ".local", "share", "recortex"), storage.records_top())
        with mock.patch.dict(os.environ, {"RECORTEX_TESTING": "1", "RECORTEX_DATA_HOME": os.path.join(self.dir, "tests")}):
            self.assertEqual(os.path.join(self.dir, "tests", "recortex"), storage.records_top())
        with mock.patch.dict(os.environ, {"RECORTEX_DATA_HOME": os.path.join(self.dir, "tests")}):
            self.assertEqual(os.path.join(self.dir, "data", "recortex"), storage.records_top())   # only in a test run

    def test_the_own_folder_is_private_and_made_on_request(self):
        self.assertIsNone(storage.own_record_folder(ID))
        path = storage.own_record_folder(ID, create=True)
        self.assertEqual(os.path.join(self.dir, "data", "recortex", "private", "records", ID), path)
        self.assertEqual(0o700, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(path, storage.check_record_folder(path, "own"))
        os.chmod(path, 0o750)
        with self.assertRaises(storage.StorageError) as caught:
            storage.check_record_folder(path, "own")
        self.assertEqual("permissions", caught.exception.kind)

    def test_the_project_folder_is_dot_recortex_beside_the_project(self):
        root = self.folder("project")
        self.assertEqual(os.path.join(os.path.realpath(root), ".recortex"), storage.record_path(root))


class FolderRuleTests(Box):
    def refused(self, path, kind, place="chosen", create=False):
        with self.assertRaises(storage.StorageError) as caught:
            storage.check_record_folder(path, place, create)
        self.assertEqual(kind, caught.exception.kind)
        self.assertNotIn(self.place, str(caught.exception))   # no path in a message

    def test_a_folder_that_is_read_by_others_is_accepted_where_the_user_chose_the_place(self):
        path = self.folder("rec", 0o755)
        self.assertEqual(path, storage.check_record_folder(path, "chosen"))
        self.assertEqual(path, storage.check_record_folder(path, "project"))
        os.chmod(path, 0o700)
        self.assertEqual(path, storage.check_record_folder(path, "chosen"))

    def test_a_write_bit_for_group_or_others_is_refused(self):
        for mode in (0o775, 0o757, 0o777, 0o770):
            path = self.folder("rec%o" % mode, 0o755)
            os.chmod(path, mode)
            with self.subTest(mode=oct(mode)):
                self.refused(path, "permissions")

    def test_a_link_is_refused_at_the_folder_and_above_it(self):
        real = self.folder("real")
        link = os.path.join(self.place, "link")
        os.symlink(real, link)
        self.refused(link, "symlink")
        inner = os.path.join(real, "rec")
        os.mkdir(inner)
        os.chmod(inner, 0o755)
        self.refused(os.path.join(link, "rec"), "symlink")   # a link on the way to the folder
        self.assertEqual(inner, storage.check_record_folder(inner, "chosen"))

    def test_a_file_or_a_missing_folder_is_no_record_folder(self):
        self.put(os.path.join(self.place, "file"))
        self.refused(os.path.join(self.place, "file"), "not-folder")
        self.refused(os.path.join(self.place, "none"), "missing")

    def test_create_makes_the_last_folder_with_mode_0700_and_nothing_above(self):
        path = os.path.join(self.place, "new")
        self.assertEqual(path, storage.check_record_folder(path, "chosen", create=True))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(path).st_mode))
        self.refused(os.path.join(self.place, "a", "b"), "missing", create=True)
        self.assertFalse(os.path.exists(os.path.join(self.place, "a")))

    def test_an_unusable_name_is_refused_without_looking(self):
        for name in ("relative/path", "", os.path.join(self.place, "..", "x"), self.place + "/bad\nname",
                     self.place + "//double", None, 5):
            with self.subTest(name=name):
                self.refused(name, "name")
        self.refused(self.place, "name", place="elsewhere")

    def test_another_owner_is_refused(self):
        path = self.folder("rec")
        with mock.patch.object(storage, "_uid", return_value=os.getuid() + 1):
            self.refused(path, "owner")
        # a folder of another real user cannot be made here (no root); the check above stands in for it, and the guard test of
        # the owner of a file below does the same: both read the owner from lstat/fstat and compare it with _uid().

    def test_a_folder_above_that_anyone_can_write_is_refused_unless_the_user_trusted_it_exactly(self):
        outer = self.folder("outer", 0o755)
        inner = os.path.join(outer, "rec")
        os.mkdir(inner)
        os.chmod(inner, 0o755)
        self.assertEqual(inner, storage.check_record_folder(inner, "chosen"))
        os.chmod(outer, 0o777)
        self.refused(inner, "ancestor-permissions")
        import observer_sources
        with mock.patch.object(observer_sources.Trust, "accepts", return_value=True) as accepts:
            self.assertEqual(inner, storage.check_record_folder(inner, "chosen"))
        self.assertTrue(accepts.called)
        os.chmod(outer, 0o755)
        with mock.patch.object(storage, "_uid", return_value=os.getuid() + 1):
            # the user's own folder above is then "another account's": the same code, and the same way out
            self.refused(inner, "owner")


class ScanTests(Box):
    def test_the_kinds_come_from_lstat_and_no_file_is_opened(self):
        path = self.folder("rec")
        self.put(os.path.join(path, "a.md"), b"abc")
        os.mkdir(os.path.join(path, "sub"))
        os.symlink("a.md", os.path.join(path, "link.md"))
        os.mkfifo(os.path.join(path, "pipe.md"))   # opening it for reading would wait for a writer
        rows = storage.scan_record_folder(path)
        self.assertEqual(["a.md", "link.md", "pipe.md", "sub"], [row[0] for row in rows])
        self.assertEqual(["file", "link", "other", "dir"], [row[3] for row in rows])
        self.assertEqual(3, rows[0][1])
        self.assertIsInstance(rows[0][2], int)

    def test_a_subfolder_a_missing_folder_and_a_link_to_a_folder(self):
        path = self.folder("rec")
        os.mkdir(os.path.join(path, "topics"), 0o700)   # a folder of the record that others can write is refused on a scan
        self.put(os.path.join(path, "topics", "x.md"))
        self.assertEqual(["x.md"], [row[0] for row in storage.scan_record_folder(path, "topics")])
        self.assertEqual([], storage.scan_record_folder(path, "nothing"))
        os.symlink(os.path.join(path, "topics"), os.path.join(path, "hub"))
        with self.assertRaises(storage.StorageError) as caught:
            storage.scan_record_folder(path, "hub")
        self.assertEqual("symlink", caught.exception.kind)


class ReadTests(Box):
    def setUp(self):
        super().setUp()
        self.path = self.folder("rec")

    def read(self, name, limit=1000):
        return storage.read_record_file(os.path.join(self.path, name), limit)

    def refused(self, name, kind, limit=1000):
        with self.assertRaises(storage.StorageError) as caught:
            self.read(name, limit)
        self.assertEqual(kind, caught.exception.kind)
        self.assertNotIn(self.place, str(caught.exception))

    def test_a_regular_file_is_read_whole_and_a_missing_one_is_none(self):
        self.put(os.path.join(self.path, "a.md"), "one\nwörd\n".encode("utf-8"))
        self.assertEqual("one\nwörd\n".encode("utf-8"), self.read("a.md"))
        self.assertIsNone(self.read("none.md"))
        self.put(os.path.join(self.path, "empty.md"), b"")
        self.assertEqual(b"", self.read("empty.md"))

    def test_a_link_a_pipe_a_folder_and_a_file_others_can_write_are_refused(self):
        self.put(os.path.join(self.path, "a.md"))
        os.symlink("a.md", os.path.join(self.path, "link.md"))
        self.refused("link.md", "symlink")
        os.mkfifo(os.path.join(self.path, "pipe.md"))
        started = time.monotonic()
        self.refused("pipe.md", "not-folder")
        self.assertLess(time.monotonic() - started, 3)   # refused, not waited for
        os.mkdir(os.path.join(self.path, "dir.md"))
        self.refused("dir.md", "not-folder")
        for mode in (0o664, 0o646, 0o666):
            self.put(os.path.join(self.path, "w.md"), mode=mode)
            with self.subTest(mode=oct(mode)):
                self.refused("w.md", "permissions")

    def test_a_file_of_another_owner_is_refused(self):
        self.put(os.path.join(self.path, "a.md"))
        with mock.patch.object(storage, "_uid", return_value=os.getuid() + 1):
            self.refused("a.md", "owner")

    def test_a_file_over_the_limit_is_refused_before_and_while_it_is_read(self):
        self.put(os.path.join(self.path, "big.md"), b"x" * 101)
        self.refused("big.md", "size", limit=100)
        self.put(os.path.join(self.path, "ok.md"), b"x" * 100)
        self.assertEqual(100, len(self.read("ok.md", limit=100)))

    def test_a_file_that_grows_while_it_is_read_is_refused(self):
        target = self.put(os.path.join(self.path, "a.md"), b"x" * 10)
        real = os.read
        grown = []

        def read(descriptor, size):
            data = real(descriptor, size)
            if not grown:
                grown.append(1)
                with open(target, "ab") as handle:
                    handle.write(b"more")
            return data
        with mock.patch.object(storage.os, "read", side_effect=read):
            self.refused("a.md", "changed")

    def test_a_file_that_is_replaced_while_it_is_read_is_refused(self):
        target = self.put(os.path.join(self.path, "a.md"), b"x" * 10)
        other = self.put(os.path.join(self.path, "b.md"), b"y" * 10)
        real = os.read

        done = []

        def read(descriptor, size):
            data = real(descriptor, size)
            if not done:
                done.append(1)
                os.replace(other, target)
            return data
        with mock.patch.object(storage.os, "read", side_effect=read):
            self.refused("a.md", "changed")


class WriteTests(Box):
    def setUp(self):
        super().setUp()
        self.path = self.folder("rec")

    def names(self):
        return sorted(os.listdir(self.path))

    def test_a_new_file_is_made_whole_with_mode_0600_and_never_replaces_one(self):
        target = os.path.join(self.path, "e.md")
        self.assertTrue(storage.link_new_record_file(target, b"first"))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(target).st_mode))
        self.assertFalse(storage.link_new_record_file(target, b"second"))
        with open(target, "rb") as handle:
            self.assertEqual(b"first", handle.read())
        self.assertEqual(["e.md"], self.names())   # no temporary file is left

    def test_where_a_hard_link_is_refused_the_name_is_made_with_an_exclusive_open(self):
        target = os.path.join(self.path, "e.md")
        with mock.patch.object(storage.os, "link", side_effect=PermissionError("no links here")):
            self.assertTrue(storage.link_new_record_file(target, b"first"))
            self.assertFalse(storage.link_new_record_file(target, b"second"))
        with open(target, "rb") as handle:
            self.assertEqual(b"first", handle.read())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(target).st_mode))
        self.assertEqual(["e.md"], self.names())

    def test_replace_puts_the_whole_file_in_place_and_does_not_follow_a_link(self):
        target = os.path.join(self.path, "b.md")
        self.put(target, b"old")
        storage.replace_record_file(target, b"new")
        with open(target, "rb") as handle:
            self.assertEqual(b"new", handle.read())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(target).st_mode))
        outside = self.put(os.path.join(self.place, "outside"), b"keep")
        os.remove(target)
        os.symlink(outside, target)
        storage.replace_record_file(target, b"again")
        with open(outside, "rb") as handle:
            self.assertEqual(b"keep", handle.read())
        self.assertFalse(os.path.islink(target))
        self.assertEqual(["b.md"], self.names())

    def test_a_failed_write_leaves_no_temporary_file(self):
        target = os.path.join(self.path, "b.md")
        with mock.patch.object(storage.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                storage.replace_record_file(target, b"x")
        self.assertEqual([], self.names())

    def test_a_folder_others_can_write_or_a_link_is_not_written_into(self):
        os.chmod(self.path, 0o777)
        with self.assertRaises(storage.StorageError) as caught:
            storage.link_new_record_file(os.path.join(self.path, "e.md"), b"x")
        self.assertEqual("permissions", caught.exception.kind)
        os.chmod(self.path, 0o755)
        link = os.path.join(self.place, "link")
        os.symlink(self.path, link)
        with self.assertRaises(storage.StorageError) as caught:
            storage.replace_record_file(os.path.join(link, "e.md"), b"x")
        self.assertEqual("symlink", caught.exception.kind)

    def test_a_topic_folder_is_made_with_mode_0700_inside_the_record_only(self):
        sub = storage.make_record_folder(os.path.join(self.path, "reader"))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(sub).st_mode))
        self.assertEqual(sub, storage.make_record_folder(sub))
        os.symlink(self.place, os.path.join(self.path, "other"))
        with self.assertRaises(storage.StorageError):
            storage.make_record_folder(os.path.join(self.path, "other"))


class LockTests(Box):
    def test_two_writers_take_the_lock_one_after_the_other(self):
        order = []
        first_in = threading.Event()

        def first():
            with storage.record_lock(ID):
                first_in.set()
                time.sleep(0.4)
                order.append("first")

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(first_in.wait(5))
        with storage.record_lock(ID):
            order.append("second")
        thread.join(5)
        self.assertEqual(["first", "second"], order)

    def test_a_lock_that_is_not_released_is_refused_after_the_wait(self):
        held = threading.Event()
        release = threading.Event()

        def hold():
            with storage.record_lock(ID):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        with mock.patch.object(storage, "WRITE_WAIT", 0.3):
            with self.assertRaises(storage.StorageError) as caught:
                with storage.record_lock(ID):
                    pass
        self.assertEqual("busy", caught.exception.kind)

    def test_the_lock_is_in_the_state_folder_and_never_in_the_record(self):
        with storage.record_lock(ID):
            pass
        path = os.path.join(self.dir, "state", "recortex", "private", "local", ID, "write.lock")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))


class BindingTests(Box):
    def test_a_row_binds_an_id_to_a_folder_and_is_read_back(self):
        self.assertEqual([], storage.record_bindings())
        path = self.folder("rec")
        row = storage.bind_record(ID, path, "chosen")
        self.assertEqual((ID, path, "chosen"), (row["id"], row["path"], row["place"]))
        self.assertEqual([row], storage.record_bindings())
        self.assertEqual([row], storage.record_bindings(ID))
        self.assertEqual([], storage.record_bindings("a" * 16))
        info = os.stat(path)
        self.assertEqual((info.st_uid, stat.S_IMODE(info.st_mode)), (row["uid"], row["mode"]))

    def test_another_folder_adds_a_row_and_the_same_folder_replaces_its_own(self):
        first, second = self.folder("one"), self.folder("two")
        storage.bind_record(ID, first, "project")
        storage.bind_record(ID, second, "project")
        storage.bind_record(ID, first, "chosen")
        rows = storage.record_bindings(ID)
        self.assertEqual(sorted([first, second]), sorted(row["path"] for row in rows))
        self.assertEqual("chosen", [row for row in rows if row["path"] == first][0]["place"])

    def test_an_edited_row_a_row_signed_with_another_key_and_a_damaged_file_bind_nothing(self):
        import json
        path = self.folder("rec")
        storage.bind_record(ID, path, "chosen")
        file = os.path.join(self.dir, "state", "recortex", "private", "local-records.json")
        with open(file, encoding="utf-8") as handle:
            data = json.load(handle)
        data["rows"][0]["path"] = os.path.join(self.place, "elsewhere")
        with open(file, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.assertEqual([], storage.record_bindings(ID))
        data["rows"][0]["path"] = path
        data["rows"][0]["mode"] = 0o777
        with open(file, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.assertEqual([], storage.record_bindings(ID))
        with open(file, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual([], storage.record_bindings(ID))
        storage.bind_record(ID, path, "chosen")
        key = os.path.join(self.dir, "state", "recortex", "private", "key")
        with open(key, "w") as handle:
            handle.write("ab" * 32 + "\n")
        self.assertEqual([], storage.record_bindings(ID))   # a lost or replaced key binds nothing

    def test_the_file_is_private_and_the_reader_never_writes(self):
        path = self.folder("rec")
        storage.bind_record(ID, path, "own")
        file = os.path.join(self.dir, "state", "recortex", "private", "local-records.json")
        self.assertEqual(0o600, stat.S_IMODE(os.stat(file).st_mode))
        before = os.stat(file).st_mtime_ns
        storage.record_bindings()
        self.assertEqual(before, os.stat(file).st_mtime_ns)


class SandboxTests(Box):
    def test_every_storage_top_resolves_inside_the_sandbox(self):
        for name in ("records_top", "state_top", "cache_top", "config_top"):
            self.assertTrue(getattr(storage, name)().startswith(self.dir + os.sep), name)
        self.assertTrue(storage.key_path().startswith(self.dir + os.sep))

    def test_no_script_opens_a_record_folder_outside_storage(self):
        """The guard of 12.4: local_store.py and local_sync.py call none of the functions that open, list, move or remove a file,
        and import none of the modules that do (storage.py is where every file of a record is touched)."""
        import ast
        banned_os = {"open", "scandir", "listdir", "replace", "rename", "link", "unlink", "remove", "mkdir", "makedirs", "walk",
                     "symlink", "stat", "lstat", "fdopen"}
        for name in ("local_store.py", "local_sync.py"):
            path = os.path.join(SCRIPTS, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            found = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    function = node.func
                    if isinstance(function, ast.Name) and function.id == "open":
                        found.append("open(")
                    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name) \
                            and function.value.id == "os" and function.attr in banned_os:
                        found.append("os." + function.attr)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = [alias.name for alias in node.names] + [getattr(node, "module", None) or ""]
                    found += [module for module in modules if module.split(".")[0] in ("shutil", "pathlib", "glob", "tempfile")]
            self.assertEqual([], found, name)
        # the scan itself sees what it should
        tree = ast.parse("import os\nos.listdir('x')\nopen('y')\nreopen(1)")
        calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
        self.assertEqual(2, len([c for c in calls if (isinstance(c, ast.Name) and c.id == "open") or
                                 (isinstance(c, ast.Attribute) and c.attr in banned_os)]))



class Grammar(local_record.LocalSandbox):
    """The file grammar: front matter, body and the seal as the last line."""

    seeded = False

    def relpath(self, kind="entry"):
        return {"entry": "topics/reader/2026-11-23T1412Z-p7d2n5v3c5x4b6aq.md", "topic": "topics/reader.md", "hub": "hub.md"}[kind]

    def front(self, kind, **more):
        base = {"entry": {"recortex": 1, "kind": "entry", "id": "p7d2n5v3c5x4b6aq", "topic": 2, "created": "2026-11-23T14:12:07Z"},
                "topic": {"recortex": 1, "kind": "topic", "number": 2, "title": "Reader: \"quoting\" and caf\u00e9",
                          "labels": ["final-record", "topic:reader"], "state": "open", "created": "2026-11-23T14:12:07Z",
                          "updated": "2026-11-23T14:12:07Z"},
                "hub": {"recortex": 1, "kind": "hub", "number": 1, "title": "Hub", "labels": ["hub"], "state": "open",
                        "created": "2026-11-23T14:12:07Z", "updated": "2026-11-23T14:12:07Z"}}[kind]
        return dict(base, **more)

    def make(self, kind="entry", body="text\n", **more):
        front = self.front(kind, **more)
        return self.local_store.render(front["kind"], front, body, self.relpath(kind), self.record_id)

    def parse(self, data, kind="entry"):
        return self.local_store.parse_file(data, self.relpath(kind), self.record_id)

    def unread(self, data, reason, kind="entry"):
        with self.assertRaises(self.local_store.Unread) as caught:
            self.parse(data, kind)
        self.assertEqual(reason, caught.exception.reason)
        self.assertNotIn("quoting", str(caught.exception))   # a fixed word, never the file's text

    def test_each_kind_round_trips_with_its_body_exactly(self):
        for kind, body in (("entry", "text\n"), ("entry", ""), ("topic", "## A\n\n---\n\nbody with --- and a \u00e9\n\n\n"),
                           ("hub", "x"), ("entry", "<!-- recortex:seal v1 key=%s mac=%s -->\nnot the last line" % ("0" * 16, "0" * 64))):
            with self.subTest(kind=kind, body=body[:20]):
                parsed = self.parse(self.make(kind, body), kind)
                self.assertEqual((kind, body, "sealed"), (parsed.kind, parsed.body, parsed.state))
        parsed = self.parse(self.make("topic"), "topic")
        self.assertEqual("Reader: \"quoting\" and caf\u00e9", parsed.front["title"])
        self.assertEqual(["final-record", "topic:reader"], parsed.front["labels"])
        self.assertEqual(2, parsed.number)

    def test_a_closed_topic_has_its_reason_and_time_before_created(self):
        data = self.make("topic", state="closed", reason="not planned", closed="2026-11-24T10:00:00Z")
        lines = data.decode("utf-8").split("\n")
        self.assertEqual(["state: closed", "reason: not planned", "closed: 2026-11-24T10:00:00Z", "created: 2026-11-23T14:12:07Z"],
                         lines[6:10])
        self.assertEqual("closed", self.parse(data, "topic").front["state"])
        self.unread(self.make("topic").replace(b"state: open", b"state: open\nreason: completed"), "front", "topic")
        self.unread(data.replace(b"reason: not planned\n", b""), "front", "topic")

    def test_the_seal_is_the_last_line_and_only_the_last_line(self):
        data = self.make()
        self.assertTrue(data.endswith(b" -->\n"))
        self.assertRegex(data.decode().split("\n")[-2], self.local_store.SEAL_LINE)
        self.unread(data + b"text after the seal\n", "seal")
        self.unread(data[:-1], "seal")
        self.unread(b"---\n" + data.split(b"---\n", 1)[1].rsplit(b"<!--", 1)[0], "seal")

    def test_front_matter_that_does_not_fit_is_not_read(self):
        good = self.make().decode()
        broken = {
            "first line": good.replace("---\n", "--\n", 1),
            "key missing": good.replace("topic: 2\n", ""),
            "extra key": good.replace("topic: 2\n", "topic: 2\nextra: 1\n"),
            "wrong order": good.replace("id: p7d2n5v3c5x4b6aq\ntopic: 2\n", "topic: 2\nid: p7d2n5v3c5x4b6aq\n"),
            "duplicate": good.replace("topic: 2\n", "topic: 2\ntopic: 2\n"),
            "bad id": good.replace("p7d2n5v3c5x4b6aq\n", "P7D2N5V3C5X4B6AQ\n", 1),
            "bad number": good.replace("topic: 2", "topic: 02"),
            "zero": good.replace("topic: 2", "topic: 0"),
            "bad stamp": good.replace("2026-11-23T14:12:07Z", "2026-11-23 14:12:07", 1),
            "version": good.replace("recortex: 1", "recortex: 2"),
            "unknown kind": good.replace("kind: entry", "kind: note"),
            "no space": good.replace("topic: 2", "topic:2"),
            "unclosed": good.replace("created: 2026-11-23T14:12:07Z\n---\n", "created: 2026-11-23T14:12:07Z\n"),
        }
        for what, text in broken.items():
            with self.subTest(what=what):
                with self.assertRaises(self.local_store.Unread):
                    self.parse(text.encode("utf-8"))
        for what, change in (("hub number", ("number: 1", "number: 2")), ("title not json", ("title: \"Hub\"", "title: Hub")),
                             ("title control", ("title: \"Hub\"", "title: \"Hub\\n\"")), ("title empty", ("title: \"Hub\"", "title: \" \"")),
                             ("label", ("labels: [\"hub\"]", "labels: [\"Hub!\"]")), ("labels type", ("labels: [\"hub\"]", "labels: \"hub\"")),
                             ("state", ("state: open", "state: shut"))):
            with self.subTest(what=what):
                text = self.make("hub").decode().replace(*change)
                self.unread(text.encode("utf-8"), "front", "hub")
        self.unread(self.make("topic").replace(b"number: 2", b"number: 1"), "front", "topic")

    def test_encoding_size_and_line_endings(self):
        good = self.make()
        self.unread(good.replace(b"\n", b"\r\n"), "encoding")
        self.unread(good.replace(b"text", b"te\xffxt"), "encoding")
        self.unread(good.replace(b"text", b"te\x00xt"), "encoding")
        with self.assertRaises(self.local_store.Refused):
            self.make("entry", "x" * (self.record_store.recortex.BODY_LIMIT + 1))
        self.unread(b"x" * (self.local_store.FILE_LIMIT + 1), "size")

    def test_the_four_states(self):
        good = self.make()
        self.assertEqual("sealed", self.parse(good).state)
        self.assertEqual("changed", self.parse(good.replace(b"text", b"tex2")).state)
        other = self.local_store.Sealing(b"another machine")
        foreign = self.local_store.render("entry", self.front("entry"), "text\n", self.relpath(), self.record_id, other)
        self.assertEqual("foreign", self.parse(foreign).state)
        # a changed key id by hand turns changed into foreign, and both are untrusted
        key = self.local_store.sealing().key_id.encode()
        self.assertEqual("foreign", self.parse(good.replace(key, b"0" * 16)).state)
        self.unread(good.rsplit(b"<!--", 1)[0], "seal")

    def test_the_path_and_the_record_are_part_of_the_seal(self):
        good = self.make()
        copied = self.local_store.parse_file(good, "topics/reader/2026-11-23T1413Z-p7d2n5v3c5x4b6aq.md", self.record_id)
        self.assertEqual("changed", copied.state)   # a sealed file copied over another one fails
        elsewhere = self.local_store.parse_file(good, self.relpath(), OTHER)
        self.assertEqual("changed", elsewhere.state)   # and a file of another record does not count here

    def test_a_seal_line_in_the_middle_of_a_body_is_text(self):
        line = "<!-- recortex:seal v1 key=%s mac=%s -->" % (self.local_store.sealing().key_id, "a" * 64)
        parsed = self.parse(self.make("entry", "before\n%s\nafter\n" % line))
        self.assertEqual(("sealed", "before\n%s\nafter\n" % line), (parsed.state, parsed.body))

    def test_slugs_are_made_once_from_the_title(self):
        slug = self.local_store.slug_of
        self.assertEqual("reader-quoting-and-line-endings", slug("Reader: quoting and line endings", 2, set()))
        self.assertEqual("topic-7", slug("!!!", 7, set()))
        self.assertEqual("topic-7", slug("Reader", 7, {"reader"}))
        self.assertEqual("topic-7", slug("hub", 7, set()))
        self.assertEqual("a" * 60, slug("a" * 100, 3, set()))
        self.assertEqual("caf", slug("caf\u00e9", 3, set()))
        self.assertRegex(slug("Weird  --  title  ", 3, set()), r"^[a-z0-9][a-z0-9-]{0,59}$")


OTHER = "q7c2m5t3v4x6b2da"


class Numbers(local_record.LocalSandbox):
    seeded = False

    def test_the_hub_is_one_and_the_next_topic_is_the_highest_number_plus_one(self):
        store = self.store
        self.assertEqual("#1", store.create_issue("Hub", ["hub"], body="x"))
        self.assertEqual("#2", store.create_issue("First", ["final-record"], body="x"))
        self.assertEqual("#3", store.create_issue("Second", ["final-record"], body="x"))
        os.remove(self.topic_path_of("second"))   # a topic whose file is gone is still named by its folder: its number is not given again
        self.assertEqual("#4", store.create_issue("Third", ["final-record"], body="x"))
        with self.assertRaises(self.local_store.Refused):
            store.create_issue("Another hub", ["hub"], body="x")

    def topic_path_of(self, slug):
        return os.path.join(self.folder, "topics", slug + ".md")

    def test_a_topic_number_is_read_from_the_files_never_from_a_remembered_count(self):
        store = self.store
        store.create_issue("Hub", ["hub"], body="x")
        store.create_issue("First", ["final-record"], body="x")
        other = self.record_store.for_repo(self.repo, self.config)   # the same store; a second process would read the same files
        self.assertEqual("#3", other.create_issue("Second", ["final-record"], body="x"))

    def test_an_entry_number_is_52_bits_of_the_hash_of_its_id_at_most_16_digits(self):
        for entry_id in ("aaaaaaaaaaaaaaaa", "zzzzzzzzzzzzzzzz", "p7d2n5v3c5x4b6aq"):
            number = self.local_store.entry_number(entry_id)
            self.assertLess(number, 2 ** 52)
            self.assertLessEqual(len(str(number)), 16)
            self.assertEqual(number, self.local_store.entry_number(entry_id))
        self.assertEqual(int(__import__("hashlib").sha256(b"p7d2n5v3c5x4b6aq").hexdigest()[:13], 16),
                         self.local_store.entry_number("p7d2n5v3c5x4b6aq"))
        ids = {self.local_store.new_id() for _ in range(200)}
        self.assertEqual(200, len(ids))
        for entry_id in ids:
            self.assertRegex(entry_id, r"^[a-z2-7]{16}$")

    def test_a_zero_or_a_taken_number_is_drawn_again(self):
        store = self.store
        store.create_issue("Hub", ["hub"], body="x")
        first = store.post_entry(1, body="one")
        taken = int(store.posted_id(first))
        real = self.local_store.entry_number
        calls = []

        def number(entry_id):
            calls.append(entry_id)
            return 0 if len(calls) == 1 else taken if len(calls) == 2 else real(entry_id)
        with mock.patch.object(self.local_store, "entry_number", side_effect=number):
            second = store.post_entry(1, body="two")
        self.assertGreaterEqual(len(calls), 3)
        self.assertNotIn(int(store.posted_id(second)), (0, taken))

    def test_the_numbers_are_in_the_files_so_a_second_store_gives_the_same_ones(self):
        self.seed()
        again = self.local_store.LocalStore(self.repo, self.config, self.folder, "own")
        self.assertEqual([1, 2, 3, 4], [number for number, _, _ in again.topics()])
        rows = json.loads(again.list_entries(2))
        self.assertEqual([e for e in self.entries["reader"]], [row["id"] for row in rows])



class Operations(local_record.LocalSandbox):
    """Every operation of the interface on a seeded record, with its read-back."""

    def test_the_record_reads_back_in_the_rows_the_callers_check(self):
        store = self.store
        row = store.read_issue(2)
        self.assertEqual(["number", "title", "state", "state_reason", "body", "labels", "user", "created_at", "updated_at",
                          "closed_at", "html_url", "state_seal"], list(row))
        self.assertEqual((2, "Reader: quoting and line endings", "open", None, "#2"),
                         (row["number"], row["title"], row["state"], row["state_reason"], row["html_url"]))
        self.assertEqual([{"name": "final-record"}, {"name": "rel:changed"}, {"name": "topic:reader"}], row["labels"])
        self.assertEqual({"login": "local--record"}, row["user"])
        self.assertNotIn("pull_request", row)
        self.assertIn("A stray quote stops the read", row["body"])   # the later text
        self.assertEqual("2\n", store.read_issue(2, jq=".number"))
        closed = store.read_issue(4)
        self.assertEqual(("closed", "completed"), (closed["state"], closed["state_reason"]))
        self.assertIsNotNone(closed["closed_at"])
        with self.assertRaises(self.record_store.Missing) as caught:
            store.read_issue(99)
        self.assertTrue(store.is_missing(caught.exception))
        self.assertFalse(store.is_missing(self.record_store.StoreError("other")))
        with self.assertRaises(self.record_store.Unsupported):
            store.read_issue(2, jq=".title")

    def test_an_entry_reads_back_with_the_keys_of_a_comment(self):
        store = self.store
        cid = self.entries["reader"][0]
        row = store.read_entry(cid)
        self.assertEqual({"id", "node_id", "issue_url", "html_url", "user", "body", "created_at", "updated_at", "state_seal"}, set(row))
        self.assertEqual((cid, "local:%s/issues/2" % self.repo, "#2/%d" % cid), (row["id"], row["issue_url"], row["html_url"]))
        self.assertEqual(row["created_at"], row["updated_at"])   # a sealed entry was never edited
        self.assertEqual({"login": "local--record"}, row["user"])
        self.assertTrue(row["body"].startswith("<!-- ghim:event type=decision"))
        self.assertEqual(row, store.read_entry(str(cid)))
        for missing in (1, "x", None, self.entries["speed"][0] + 1):
            with self.assertRaises(self.record_store.Missing):
                store.read_entry(missing)

    def test_list_entries_is_json_in_the_order_they_were_made(self):
        rows = json.loads(self.store.list_entries(2))
        self.assertEqual(self.entries["reader"], [row["id"] for row in rows])
        self.assertEqual(1, len(json.loads(self.store.list_entries(1))))   # the hub's resume block
        self.assertEqual("[]", json.dumps(json.loads(self.store.list_entries(4))[1:]))

    def test_the_hub_entries_are_the_dicts_read_hub_comments_returns_newest_first(self):
        hub = self.store.hub_entries(1)
        self.assertEqual(1, len(hub))
        self.assertEqual({"url", "created", "body", "author", "last_edited", "editor", "edit_total", "edit_editors", "local"}, set(hub[0]))
        self.assertEqual(("local--record", None, 0, []), (hub[0]["author"], hub[0]["last_edited"], hub[0]["edit_total"],
                                                         hub[0]["edit_editors"]))
        self.store.post_entry(1, body="a later entry")
        later = self.store.hub_entries(1)
        self.assertEqual(["a later entry"], [later[0]["body"]])
        self.assertEqual(2, len(later))
        self.assertGreater(later[0]["created"], later[1]["created"])
        self.assertEqual(hub[0]["url"], later[1]["url"])
        self.assertRegex(hub[0]["url"], r"^#1/[0-9]{10,16}$")

    def test_a_changed_entry_is_one_edit_by_nobody_known_and_a_foreign_one_has_another_author(self):
        self.damage("changed-entry")
        self.damage("foreign-entry", "speed")
        changed = self.store.read_entry(self.entries["reader"][0])
        self.assertNotEqual(changed["created_at"], changed["updated_at"])
        self.assertEqual(("local--record", "changed"), (changed["user"]["login"], changed["state_seal"]))
        foreign = self.store.read_entry(self.entries["speed"][0])
        self.assertEqual(("other-machine", "foreign", foreign["created_at"]), (foreign["user"]["login"], foreign["state_seal"],
                                                                                foreign["updated_at"]))
        answer = json.loads(self.store.histories({self.entries["reader"][0]: ("n", None), self.entries["reader"][1]: ("n", None)}))
        first, second = answer["data"]["c%d" % self.entries["reader"][0]], answer["data"]["c%d" % self.entries["reader"][1]]
        self.assertEqual(str(self.entries["reader"][0]), first["fullDatabaseId"])
        self.assertEqual(1, len(first["userContentEdits"]["nodes"]))
        self.assertIsNone(first["userContentEdits"]["nodes"][0]["editor"])
        self.assertIsNotNone(first["lastEditedAt"])
        self.assertEqual(([], None, False), (second["userContentEdits"]["nodes"], second["lastEditedAt"],
                                             second["userContentEdits"]["pageInfo"]["hasNextPage"]))
        hub = self.store.hub_entries(2)
        self.assertEqual([1, 0, 0, 0, 0], [row["edit_total"] for row in sorted(hub, key=lambda r: r["created"])])
        self.assertEqual([[None], [], [], [], []], [row["edit_editors"] for row in sorted(hub, key=lambda r: r["created"])])

    def test_the_finders_name_the_keys_of_their_shape(self):
        store = self.store
        self.assertEqual({"number", "title", "body", "url"}, set(store.find_issues("mine")[0]))
        self.assertEqual([1, 2, 3, 4], [row["number"] for row in store.find_issues("mine")])
        self.assertEqual([], store.find_issues("active"))   # the task is closed
        store.reopen(4)
        active = store.find_issues("active")
        self.assertEqual([4], [row["number"] for row in active])
        self.assertEqual([{"name": "active"}, {"name": "topic:reader"}], active[0]["labels"])
        self.assertEqual([], store.find_issues("plan"))
        self.assertEqual({"number", "title", "createdAt", "closedAt", "url"}, set(store.find_issues("dated")[0]))
        with self.assertRaises(ValueError):
            store.find_issues("other")
        self.damage("changed-body")
        self.assertEqual([1, 3, 4], [row["number"] for row in store.find_issues("mine")])   # only what is the user's own

    def test_the_labels_are_what_the_profiles_define(self):
        labels = self.store.list_labels("name,color,description")
        self.assertIn("hub", [label["name"] for label in labels])
        self.assertEqual({"name", "color", "description"}, set(labels[0]))
        self.assertEqual([{"name": label["name"]} for label in labels], self.store.list_labels())
        import setup_labels
        self.assertEqual([label["name"] for label in setup_labels.wanted_labels(["software", "research"], [])],
                         [label["name"] for label in labels])
        self.config["plans"] = {"research": 3}
        self.assertIn("plan", [label["name"] for label in self.store.list_labels()])

    def test_set_labels_adds_and_removes_in_the_front_matter_and_keeps_the_text(self):
        store = self.store
        before = store.read_issue(3)
        store.set_labels(3, ["rd:methods"], ["rd:results"])
        row = store.read_issue(3)
        self.assertEqual(["final-record", "topic:speed", "rd:methods"], [label["name"] for label in row["labels"]])
        self.assertEqual(before["body"], row["body"])
        self.assertNotEqual(before["updated_at"], row["updated_at"])
        self.assertEqual(1, len(store.versions(3)))   # no version file for a change of labels
        with self.assertRaises(self.record_store.StoreError):
            store.set_labels(3, ["not-a-defined-label"], [])
        self.assertEqual(row["labels"], store.read_issue(3)["labels"])

    def test_close_and_reopen_change_the_front_matter_only(self):
        store = self.store
        store.close(3, "not planned")
        row = store.read_issue(3)
        self.assertEqual(("closed", "not_planned"), (row["state"], row["state_reason"]))
        self.assertIn(b"reason: not planned", self.read(self.topic_path("speed")))
        store.reopen(3)
        row = store.read_issue(3)
        self.assertEqual(("open", None, None), (row["state"], row["state_reason"], row["closed_at"]))
        self.assertNotIn(b"reason:", self.read(self.topic_path("speed")))
        self.assertEqual(1, len(store.versions(3)))
        with self.assertRaises(self.local_store.Refused):
            store.close(3, "done")

    def test_edit_body_keeps_the_earlier_text_as_a_version_newest_first(self):
        store = self.store
        old = store.read_issue(2)["body"]
        store.edit_body(2, body="## Changed\n\nThird text.\n")
        versions = store.versions(2)
        self.assertEqual(3, len(versions))
        self.assertEqual(["## Changed\n\nThird text.\n", old], [versions[0][2], versions[1][2]])
        self.assertEqual(["sealed", "sealed", "sealed"], [row[1] for row in versions])
        self.assertEqual(sorted(row[0] for row in versions)[::-1], [row[0] for row in versions])
        self.assertEqual("## Changed\n\nThird text.\n", store.read_issue(2)["body"])
        self.assertEqual([], store.versions(1)[1:])

    def test_a_text_that_was_changed_or_written_elsewhere_is_not_replaced_or_sealed_over(self):
        self.damage("changed-body")
        before = self.read(self.topic_path("reader"))
        for call in (lambda: self.store.edit_body(2, body="new"), lambda: self.store.set_labels(2, [], ["rel:changed"]),
                     lambda: self.store.close(2, "completed"), lambda: self.store.reopen(2)):
            with self.assertRaises(self.local_store.Refused) as caught:
                call()
            self.assertIn("changed outside recortex", str(caught.exception))
            self.assertNotIn("lenient", str(caught.exception))
        self.assertEqual(before, self.read(self.topic_path("reader")))
        self.assertEqual(2, len(self.store.versions(2)))
        self.store.post_entry(2, body="an entry on a changed topic is still made")   # it is an entry, not a change of the text

    def test_the_posted_address_and_what_reads_it(self):
        store = self.store
        address = store.post_entry(3, body="another entry")
        self.assertRegex(address, r"^#3/[0-9]{10,16}$")
        self.assertEqual(address.split("/")[1], store.posted_id(address))
        self.assertEqual(address.split("/")[1], store.posted_id("noise\n" + address + "\n", 3))
        self.assertIsNone(store.posted_id(address, 4))
        self.assertIsNone(store.posted_id("#3"))
        self.assertEqual(3, store.created_number("#3"))
        for bad in ("#3/1234567890", "x", "", "#0x3"):
            with self.assertRaises(self.record_store.recortex.GhError):
                store.created_number(bad)
        self.assertEqual((3, int(store.posted_id(address))), store.parse_address(address))
        self.assertEqual((3, 0), store.parse_address("#3"))
        for bad in ("https://github.com/a/b/issues/3", "#0", "#3/12", "", None, "#3 "):
            self.assertIsNone(store.parse_address(bad), bad)
        self.assertEqual(address, store.safe_address(address))
        self.assertIsNone(store.safe_address("file:///etc/passwd"))

    def test_a_body_file_is_read_through_recortex(self):
        path = os.path.join(self.dir, "draft.md")
        with open(path, "w") as handle:
            handle.write("from a file\n")
        address = self.store.post_entry(3, body_file=path)
        self.assertEqual("from a file\n", self.store.read_entry(self.store.posted_id(address))["body"])
        with self.assertRaises(self.local_store.Refused):
            self.store.post_entry(3)
        with self.assertRaises(self.local_store.Refused):
            self.store.post_entry(3, body="x" * (self.record_store.recortex.BODY_LIMIT + 1))

    def test_a_new_topic_gets_its_first_version_and_a_slug(self):
        store = self.store
        self.assertEqual("#5", store.create_issue("Speed again", ["final-record"], body="b"))
        self.assertIn("speed-again.md", self.files("topics"))
        self.assertEqual(1, len(store.versions(5)))
        self.assertEqual("#6", store.create_issue("Speed again", ["final-record"], body="b"))
        self.assertIn("topic-6.md", self.files("topics"))
        for bad in ("", "  ", "a\nb", "x" * 257):
            with self.assertRaises(self.local_store.Refused):
                store.create_issue(bad, [], body="b")
        with self.assertRaises(self.local_store.Refused):
            store.create_issue("T", ["Not a label!"], body="b")

    def test_the_words_the_dry_runs_use_hold_numbers_and_no_gh(self):
        store = self.store
        self.assertEqual("local record: add an entry to #5", store.show("post_entry", 5, body_file="/x/y.md", note="12 characters"))
        self.assertEqual("local record: replace the text of #<number of NEW-reader>",
                         store.show("edit_body", "<number of NEW-reader>", note="x"))
        self.assertEqual("local record: replace the text of #a topic", store.show("edit_body", "../../etc", note="x"))
        self.assertEqual("local record: make a topic", store.show("create_issue", "Title", ["hub"]))
        self.assertEqual("local record: close #4", store.show("close", 4, "completed"))
        self.assertEqual("the entry in the record folder", store.read_back("entry"))
        self.assertEqual("topic #7 in the record folder", store.read_back("issue", 7))
        with self.assertRaises(ValueError):
            store.show("nothing", 1)
        for text in (store.show("post_entry", 5), store.read_back("entry")):
            self.assertNotIn("gh", text)
            self.assertNotIn("GitHub", text)

    def test_the_account_is_the_owner_word_and_no_server_is_reached(self):
        store = self.store
        self.assertEqual("local--record", store.whoami())
        self.assertEqual(("ready", "local--record"), store.account_state())
        self.assertEqual("", store.visibility())
        self.assertTrue(store.is_pinned(1))
        self.assertFalse(store.is_pinned(2))
        for feature in self.record_store.FEATURES:
            self.assertFalse(store.supports(feature), feature)
        with self.assertRaises(self.record_store.Unsupported):
            store.github(["api", "user"])
        with self.assertRaises(self.record_store.Unsupported):
            store.dump(10)
        with self.assertRaises(self.record_store.Unsupported):
            store.create_label("x", "ffffff", "d")
        self.assertIsNone(store.pin(1))

    def test_a_feature_that_is_off_prints_its_line_and_exits_with_status_0(self):
        import io
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            self.store.require("board")
        self.assertEqual(0, caught.exception.code)
        self.assertEqual(self.record_store.OFF["board"] + "\n", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            self.store.require("figure-pictures")
        self.assertEqual("", out.getvalue())   # a feature with no line of its own says nothing

    def test_the_words_of_the_local_store(self):
        self.assertEqual("the local record", self.store.words["name"])
        self.assertEqual("local", self.store.kind)
        self.assertNotIn("GitHub", " ".join(self.store.words.values()))


class Writes(local_record.LocalSandbox):
    """What a write may and may not do."""

    def test_no_method_edits_or_removes_an_entry(self):
        public = {name for name in dir(self.local_store.LocalStore) if not name.startswith("_")}
        for word in ("delete", "remove", "unlink", "edit_entry", "edit_comment", "update_entry", "replace_entry", "rewrite"):
            self.assertFalse([name for name in public if word in name], word)
        written = []
        real = self.storage.replace_record_file
        with mock.patch.object(self.storage, "replace_record_file",
                               side_effect=lambda path, data: (written.append(path), real(path, data))[1]):
            store = self.store
            store.post_entry(2, body="x")
            store.edit_body(2, body="new")
            store.set_labels(2, [], ["rel:changed"])
            store.close(3, "completed")
            store.reopen(3)
            store.create_issue("More", [], body="b")
        self.assertTrue(written)
        for path in written:
            self.assertEqual(os.path.join(self.folder, "topics"), os.path.dirname(path))   # a topic's own file, never an entry

    def test_an_entry_name_that_is_taken_is_not_replaced(self):
        store = self.store
        taken = self.entry_path("reader", 0)
        with open(taken, "rb") as handle:
            before = handle.read()
        self.assertFalse(self.storage.link_new_record_file(taken, b"other"))
        with open(taken, "rb") as handle:
            self.assertEqual(before, handle.read())
        ids = iter(["aaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])
        with mock.patch.object(self.local_store, "new_id", side_effect=lambda: next(ids)), \
                mock.patch.object(self.local_store, "stamp", return_value="2026-12-01T10:00:00Z"):
            store.post_entry(3, body="one")
            store.post_entry(3, body="two")
        self.assertEqual(2, len([n for n in self.files("topics/read-speed-against-the-standard-library") if n.startswith("2026-12-01")]))

    def test_two_writers_make_two_entries_and_two_versions_and_no_number_twice(self):
        results, errors = [], []

        def entry(n):
            try:
                results.append(self.store.post_entry(2, body="entry %d" % n))
            except Exception as error:   # noqa: BLE001
                errors.append(error)

        def edit(n):
            try:
                self.store.edit_body(3, body="text %d" % n)
            except Exception as error:   # noqa: BLE001
                errors.append(error)
        threads = [threading.Thread(target=entry, args=(n,)) for n in range(6)] + \
                  [threading.Thread(target=edit, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([], errors)
        self.assertEqual(6, len(set(results)))
        rows = json.loads(self.store.list_entries(2))
        self.assertEqual(len(self.entries["reader"]) + 6, len({row["id"] for row in rows}))
        self.assertEqual(1 + 4, len(self.store.versions(3)))   # the first text and the four edits, each kept
        self.assertIn(self.store.read_issue(3)["body"], ["text %d" % n for n in range(4)])

    def test_an_edit_that_stops_between_its_two_steps_leaves_a_version_and_the_old_text(self):
        store = self.store
        old = store.read_issue(3)["body"]
        with mock.patch.object(self.local_store.LocalStore, "_write_topic", side_effect=OSError("stopped")):
            with self.assertRaises(OSError):
                store.edit_body(3, body="lost")
        self.assertEqual(old, store.read_issue(3)["body"])
        self.assertEqual(["lost", old], [row[2] for row in store.versions(3)])   # harmless: the next edit goes on
        store.edit_body(3, body="found")
        self.assertEqual("found", store.read_issue(3)["body"])
        self.assertEqual(3, len(store.versions(3)))

    def test_a_record_json_that_does_not_pass_stops_every_read(self):
        record = os.path.join(self.folder, "record.json")
        data = self.read(record)
        for name, change in (("name edited", data.replace(b"sample project", b"another")), ("id", data.replace(self.record_id.encode(), OTHER.encode())),
                             ("not json", b"{"), ("missing key", data.replace(b'"format"', b'"fmt"'))):
            with self.subTest(what=name):
                self.write(record, change)
                with self.assertRaises(self.record_store.RecordFolderError) as caught:
                    self.local_store.LocalStore(self.repo, self.config, self.folder, "own")
                self.assertEqual(self.local_store.RECORD_JSON_REFUSED, str(caught.exception))
        self.write(record, data)
        self.local_store.LocalStore(self.repo, self.config, self.folder, "own")

    def test_a_record_json_sealed_on_another_machine_is_read_and_marked_foreign(self):
        record = os.path.join(self.folder, "record.json")
        plain = {"format": 1, "id": self.record_id, "name": "sample project", "profiles": ["software"], "created": "2026-11-23T14:00:00Z"}
        other = self.local_store.Sealing(b"another machine")
        self.write(record, json.dumps(dict(plain, seal={"key": other.key_id, "mac": other.json_mac(plain)})).encode())
        store = self.local_store.LocalStore(self.repo, self.config, self.folder, "own")
        self.assertEqual("foreign", store.record["state"])

    def test_the_sandbox_is_the_only_place_written(self):
        for name in ("records_top", "state_top", "cache_top", "config_top"):
            self.assertTrue(getattr(self.storage, name)().startswith(self.dir + os.sep), name)
        self.assertTrue(self.storage.key_path().startswith(self.dir + os.sep))
        self.assertTrue(self.folder.startswith(self.dir + os.sep))



class Hardening(local_record.LocalSandbox):
    """The four findings of the automatic check on the first version of the store (24 November 2026)."""

    def tree(self):
        found = {}
        for place, _, names_ in os.walk(self.folder):
            for name in names_:
                path = os.path.join(place, name)
                found[os.path.relpath(path, self.folder)] = self.read(path)
        return found

    # ---- 1. creating a topic never replaces a file
    def test_a_stray_file_or_folder_with_the_name_of_the_new_topic_is_never_replaced(self):
        for stray, make, title, expected in (("topic-5.md", "file", "!!!", "topic-5-2.md"),
                                             ("sample-title.md", "file", "Sample title", "topic-6.md"),
                                             ("sample-title", "folder", "Sample title", "topic-7.md")):
            with self.subTest(stray=stray):
                path = os.path.join(self.folder, "topics", stray)
                if make == "file":
                    self.write(path, b"not a record file at all")
                else:
                    os.mkdir(path, 0o700)   # a folder of the record that others can write is refused, which is not what this is about
                before = self.tree()
                self.store.create_issue(title, ["final-record"], body="b")
                self.assertIn(expected, self.files("topics"))
                after = self.tree()
                for name, data in before.items():
                    self.assertEqual(data, after[name], name)   # nothing that was there is touched
                os.remove(path) if make == "file" else os.rmdir(path)

    def test_the_fallback_name_is_tried_with_a_suffix_until_one_is_free(self):
        store = self.store
        for name in ("topic-5.md", "topic-5-2.md", "topic-5-3.md"):
            self.write(os.path.join(self.folder, "topics", name), b"stray")
        store.create_issue("!!!", [], body="b")
        self.assertIn("topic-5-4.md", self.files("topics"))
        self.assertEqual(self.local_store.slug_of("x", 9, {"x", "topic-9", "topic-9-2"}), "topic-9-3")
        with self.assertRaises(self.local_store.Refused):
            self.local_store.slug_of("x", 9, {"x"} | {"topic-9"} | {"topic-9-%d" % n for n in range(2, 60)})

    def test_a_name_made_since_the_listing_is_not_replaced_either(self):
        real = self.storage.link_new_record_file
        calls = []

        def link(path, data):
            calls.append(path)
            if len(calls) == 1:
                real(path, b"made by someone else meanwhile")
            return real(path, data)
        with mock.patch.object(self.storage, "link_new_record_file", side_effect=link):
            self.store.create_issue("Fresh topic", [], body="b")
        self.assertEqual(b"made by someone else meanwhile", self.read(os.path.join(self.folder, "topics", "fresh-topic.md")))
        self.assertIn("topic-5.md", self.files("topics"))

    def test_a_second_hub_is_refused_whatever_state_hub_md_is_in(self):
        for what in ("changed", "unread"):
            with self.subTest(what=what):
                path = os.path.join(self.folder, "hub.md")
                saved = self.read(path)
                if what == "changed":
                    self.write(path, saved.replace(b"sample project", b"edited project"))
                else:
                    self.write(path, b"garbage")
                before = self.read(path)
                with self.assertRaises(self.local_store.Refused):
                    self.store.create_issue("Another hub", ["hub"], body="x")
                self.assertEqual(before, self.read(path))
                self.write(path, saved)

    def test_a_number_from_a_file_that_is_not_sealed_is_kept_out_of_use_but_never_moves_the_counter(self):
        store = self.store
        # sealed topics are 1 to 4; an unread file that claims 999999999 and one that claims 5 change nothing but the use of 5
        self.write(os.path.join(self.folder, "topics", "planted.md"),
                   b"---\nrecortex: 1\nkind: topic\nnumber: 999999999\nbroken")
        self.write(os.path.join(self.folder, "topics", "claims-five.md"),
                   b"---\nrecortex: 1\nkind: topic\nnumber: 5\nbroken")
        self.assertEqual("#6", store.create_issue("After the planted ones", [], body="b"))
        changed = self.damage("changed-body", "speed")
        self.write(changed, self.read(changed).replace(b"number: 3", b"number: 7", 1))   # a changed file keeps its number
        self.assertEqual("#8", store.create_issue("After the changed one", [], body="b"))   # 6 is the sealed new one; 7 is kept
        other = self.local_store.Sealing(b"another machine")
        relpath = "topics/foreign.md"
        front = dict(self.local_store.parse_file(self.read(self.topic_path("task")), "topics/%s.md" % self.slug("task"),
                                                 self.record_id).front, number=9)
        self.write(os.path.join(self.folder, relpath), self.local_store.render("topic", front, "x", relpath, self.record_id, other))
        self.assertEqual("#10", store.create_issue("After the foreign one", [], body="b"))

    def test_a_number_is_read_only_from_the_front_matter_of_a_file_that_begins_with_it(self):
        store = self.store
        self.write(os.path.join(self.folder, "topics", "no-front.md"), b"text first\nnumber: 5\n---\n")
        self.write(os.path.join(self.folder, "topics", "in-body.md"),
                   b"---\nrecortex: 1\nkind: topic\n---\nnumber: 5\n")
        self.write(os.path.join(self.folder, "topics", "late.md"),
                   b"---\n" + b"x: 1\n" * 1200 + b"---\nnumber: 5\n")
        self.assertEqual("#5", store.create_issue("Not moved by those", [], body="b"))

    def test_a_topic_whose_file_is_gone_is_still_named_by_the_newest_sealed_file_of_its_folder(self):
        store = self.store
        os.remove(self.topic_path("task"))   # the highest topic, #4; its entry stays in its folder
        self.assertEqual("#5", store.create_issue("After the one that is gone", [], body="b"))
        os.remove(self.topic_path("speed"))   # #3: a remembered number keeps itself out of use and never moves the counter
        self.assertEqual("#6", store.create_issue("And after the second one", [], body="b"))

    def test_a_remembered_number_is_kept_out_of_use_and_never_moves_the_counter(self):
        os.remove(self.topic_path("task"))   # #4 is gone, and a folder that remembers #900 is there too
        task = self.entry_path("task", 0)
        relpath = "topics/gone-nine-hundred/" + os.path.basename(task)
        parsed = self.local_store.parse_file(self.read(task), "topics/%s/%s" % (self.slug("task"), os.path.basename(task)), self.record_id)
        os.mkdir(os.path.join(self.folder, "topics", "gone-nine-hundred"), 0o700)
        self.write(os.path.join(self.folder, relpath),
                   self.local_store.render(parsed.kind, dict(parsed.front, topic=900), parsed.body, relpath, self.record_id))
        self.assertEqual("#5", self.store.create_issue("Next", [], body="b"))   # #4 is kept; #900 moves nothing
        self.assertEqual("#6", self.store.create_issue("And next", [], body="b"))

    def test_a_folder_is_consulted_only_when_its_topic_file_is_missing(self):
        folder = os.path.join(self.folder, "topics", self.slug("speed"))   # its topic file is there
        os.chmod(folder, 0o777)   # a folder that cannot be listed safely
        self.assertEqual("#5", self.store.create_issue("Made", [], body="b"))
        os.chmod(folder, 0o700)

    def test_many_planted_newer_names_do_not_hide_the_older_sealed_file_that_names_the_topic(self):
        os.remove(self.topic_path("task"))
        folder = os.path.join(self.folder, "topics", self.slug("task"))
        for number in range(30):   # more than any short look would reach; none of them is sealed
            self.write(os.path.join(folder, "2099-01-%02dT%02d00Z-%s.md" % (1 + number % 28, number % 24, ("abcdefghijklmnopqrstuvwxyz234567"[number] * 16))),
                       b"---\nrecortex: 1\nkind: entry\ntopic: 900\nbroken")
        self.assertEqual("#5", self.store.create_issue("After the planted names", [], body="b"))   # #4 is kept; 900 moves nothing

    def test_a_folder_that_cannot_be_listed_or_read_safely_refuses_a_new_topic_and_so_does_a_limit_reached(self):
        store = self.store
        before = self.tree()
        os.remove(self.topic_path("task"))
        folder = os.path.join(self.folder, "topics", self.slug("task"))
        os.chmod(folder, 0o777)   # a folder that others can write is not listed
        with self.assertRaises(self.record_store.RecordFolderError) as caught:
            store.create_issue("Not made", [], body="b")
        self.assertEqual(self.local_store.UNREADABLE_TOPIC, str(caught.exception))
        self.assertEqual("unreadable", caught.exception.kind)
        os.chmod(folder, 0o700)
        names_ = sorted(os.listdir(folder))
        self.assertTrue(names_)
        for name in names_:   # no file of the folder reads safely: none can name the topic
            os.chmod(os.path.join(folder, name), 0o666)
        with self.assertRaises(self.record_store.RecordFolderError):
            store.create_issue("Not made", [], body="b")
        for name in names_:
            os.chmod(os.path.join(folder, name), 0o600)
        self.assertEqual("#5", store.create_issue("Made", [], body="b"))
        # the look goes as far as a sync reads (MAX_IN_FOLDER); a folder with more names than that and no sealed file among them is refused
        os.remove(self.topic_path("speed"))
        speed = os.path.join(self.folder, "topics", self.slug("speed"))
        for name in os.listdir(speed):
            path = os.path.join(speed, name)
            self.write(path, b"---\nrecortex: 1\nkind: entry\ntopic: 3\nbroken")
        with mock.patch.object(self.local_store, "MAX_IN_FOLDER", 1):
            self.write(os.path.join(speed, "2099-01-01T0000Z-aaaaaaaaaaaaaaaa.md"), b"---\nrecortex: 1\nkind: entry\ntopic: 3\nbroken")
            with self.assertRaises(self.record_store.RecordFolderError):
                store.create_issue("Not made either", [], body="b")

    def alone(self, number):
        """The topic file of `number` with nothing else of it in the record: its folder (the earlier text it was made with) is gone."""
        slug = self.slug_of(number)
        folder = os.path.join(self.folder, "topics", slug)
        for name in os.listdir(folder):
            os.remove(os.path.join(folder, name))
        os.rmdir(folder)
        return os.path.join(self.folder, "topics", slug + ".md")

    def test_a_topic_file_that_a_sync_tool_gave_line_endings_or_a_mark_keeps_its_number_out_of_use(self):
        store = self.store
        self.assertEqual("#5", store.create_issue("Fifth", [], body="b"))
        path = self.alone(5)
        self.write(path, self.read(path).replace(b"\n", b"\r\n"))   # the file no longer reads as sealed
        self.assertEqual("#6", store.create_issue("Sixth", [], body="b"))
        path = self.alone(6)
        self.write(path, b"\xef\xbb\xbf" + self.read(path).replace(b"\n", b"\r\n"))
        self.assertEqual("#7", store.create_issue("Seventh", [], body="b"))

    def slug_of(self, number):
        for name in sorted(os.listdir(os.path.join(self.folder, "topics"))):
            if name.endswith(".md") and ("number: %d\n" % number).encode() in self.read(os.path.join(self.folder, "topics", name)):
                return name[:-3]
        self.fail("no topic file carries number %d" % number)

    def test_the_next_number_is_still_the_highest_sealed_number_plus_one(self):
        self.assertEqual("#5", self.store.create_issue("Next", [], body="b"))
        self.assertEqual("#6", self.store.create_issue("Next again", [], body="b"))

    # ---- 1. a topic file that cannot be read is no reason to guess
    def test_a_topic_file_that_cannot_be_read_safely_refuses_a_new_topic_and_other_names_do_not(self):
        store = self.store
        before = self.tree()
        path = self.topic_path("speed")
        os.chmod(path, 0o666)
        with self.assertRaises(self.record_store.RecordFolderError) as caught:
            store.create_issue("Not made", [], body="b")
        self.assertEqual(self.local_store.UNREADABLE_TOPIC, str(caught.exception))
        self.assertEqual("unreadable", caught.exception.kind)
        os.chmod(path, 0o600)
        link = os.path.join(self.folder, "topics", "linked.md")
        os.symlink(path, link)
        with self.assertRaises(self.record_store.RecordFolderError):
            store.create_issue("Not made", [], body="b")
        os.remove(link)
        os.mkfifo(os.path.join(self.folder, "topics", "piped.md"))
        started = time.monotonic()
        with self.assertRaises(self.record_store.RecordFolderError):
            store.create_issue("Not made", [], body="b")
        self.assertLess(time.monotonic() - started, 5)
        os.remove(os.path.join(self.folder, "topics", "piped.md"))
        # a conflict copy and a name that is no slug are not topic files: whatever they are, they do not stop a new topic
        for name in ("reader (conflicted copy).md", "Upper-Case.md", "notes.txt"):
            os.symlink(path, os.path.join(self.folder, "topics", name))
        self.assertEqual("#5", store.create_issue("Made", [], body="b"))
        self.assertIn("made.md", self.files("topics"))
        after = self.tree()
        for name, data in before.items():
            self.assertEqual(data, after[name], name)   # and nothing that was there changed

    # ---- 2. an entry's topic is its own sealed one
    def test_an_entry_read_under_another_topic_than_its_own_is_changed(self):
        path = self.topic_path("reader")
        self.write(path, self.read(path).replace(b"number: 2", b"number: 7", 1))   # the folder now belongs to topic 7
        store = self.store
        rows = json.loads(store.list_entries(7))
        self.assertEqual(len(self.entries["reader"]), len(rows))
        for row in rows:
            self.assertEqual("changed", row["state_seal"])
            self.assertEqual("local:%s/issues/2" % self.repo, row["issue_url"])   # its own sealed topic, not the folder's
            self.assertTrue(row["html_url"].startswith("#2/"))
            self.assertNotEqual(row["created_at"], row["updated_at"])
        one = store.read_entry(self.entries["reader"][0])
        self.assertEqual(("changed", "local:%s/issues/2" % self.repo), (one["state_seal"], one["issue_url"]))
        sealed = self.local_store.attributed(self.local_store.parse_file(self.read(self.entry_path("speed", 0)), os.path.relpath(
            self.entry_path("speed", 0), self.folder), self.record_id), 3)
        self.assertEqual("sealed", sealed.state)
        self.assertEqual("changed", self.local_store.attributed(sealed, 9).state)
        self.assertEqual("foreign", self.local_store.attributed(self.local_store.Parsed("entry", {"topic": 3}, "", "x", "foreign", "k", "m"), 9).state)

    def test_an_entry_that_is_not_sealed_is_placed_under_the_topic_of_its_folder_never_under_its_own_claim(self):
        store = self.store
        path = self.entry_path("reader", 0)
        relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
        parsed = self.local_store.parse_file(self.read(path), relpath, self.record_id)
        claim = dict(parsed.front, topic=3)
        cid = int(self.local_store.entry_number(parsed.entry_id))
        # foreign: sealed under another key, claiming topic 3
        other = self.local_store.Sealing(b"another machine")
        self.write(path, self.local_store.render("entry", claim, parsed.body, relpath, self.record_id, other))
        foreign = store.read_entry(cid)
        self.assertEqual(("foreign", "local:%s/issues/2" % self.repo, "other-machine"),
                         (foreign["state_seal"], foreign["issue_url"], foreign["user"]["login"]))
        # changed: edited by hand to claim topic 3, so its seal fails
        self.write(path, self.local_store.render("entry", parsed.front, parsed.body, relpath, self.record_id))
        self.write(path, self.read(path).replace(b"topic: 2\n", b"topic: 3\n", 1))
        changed = store.read_entry(cid)
        self.assertEqual(("changed", "local:%s/issues/2" % self.repo), (changed["state_seal"], changed["issue_url"]))
        self.assertTrue(changed["html_url"].startswith("#2/"))
        self.assertEqual(cid, [row["id"] for row in json.loads(store.list_entries(2))][0])
        self.assertEqual([], [row for row in json.loads(store.list_entries(3)) if row["id"] == cid])
        # sealed under our key, but placed in the folder of another topic: its own claim holds, and it is changed
        self.write(path, self.local_store.render("entry", claim, parsed.body, relpath, self.record_id))
        moved = store.read_entry(cid)
        self.assertEqual(("changed", "local:%s/issues/3" % self.repo), (moved["state_seal"], moved["issue_url"]))
        self.assertEqual(3, self.local_store.attributed(self.local_store.parse_file(self.read(path), relpath, self.record_id), 2).placed)

    # ---- 3. a number cannot be shadowed
    def test_two_files_with_one_topic_number_are_refused_by_every_read_and_write(self):
        self.damage("duplicate-number")
        store = self.store
        for call in (lambda: store.topic(2), lambda: store.read_issue(2), lambda: store.read_issue(3), lambda: store.list_entries(2),
                     lambda: store.find_issues("mine"), lambda: store.read_entry(self.entries["reader"][0]),
                     lambda: store.post_entry(3, body="x"), lambda: store.edit_body(3, body="x"),
                     lambda: store.create_issue("More", [], body="b"), lambda: store.hub_entries(1)):
            with self.assertRaises(self.record_store.RecordFolderError) as caught:
                call()
            self.assertEqual(self.local_store.DUPLICATE, str(caught.exception))
            self.assertEqual("duplicate", caught.exception.kind)

    def test_an_entry_may_still_be_posted_into_a_topic_that_is_changed_or_foreign(self):
        self.damage("changed-body")
        address = self.store.post_entry(2, body="posted into a changed topic")
        self.assertEqual("sealed", self.store.read_entry(self.store.posted_id(address))["state_seal"])
        with self.assertRaises(self.local_store.Refused):
            self.store.edit_body(2, body="the text itself waits")

    # ---- 4. the writer refuses what the reader refuses
    def test_nothing_is_written_that_the_reader_would_refuse(self):
        store = self.store
        before = self.tree()
        refused = [
            lambda: store.create_issue("T\u2028x", [], body="b"), lambda: store.create_issue("T\x00", [], body="b"),
            lambda: store.create_issue("ok", [], body="a\x00b"), lambda: store.create_issue("ok", [], body="\ud800"),
            lambda: store.create_issue("ok", ["Bad Label"], body="b"), lambda: store.create_issue("x" * 257, [], body="b"),
            lambda: store.post_entry(2, body="a\x00b"), lambda: store.post_entry(2, body="\udc80"),
            lambda: store.edit_body(3, body="a\x00b"), lambda: store.edit_body(3, body="\ud800"),
        ]
        for call in refused:
            with self.assertRaises(self.local_store.Refused) as caught:
                call()
            self.assertNotIn("\x00", str(caught.exception))
        self.assertEqual(before, self.tree())   # not a file, not a version file, not a folder

    def test_every_write_path_reads_back_sealed_with_the_same_front_and_body(self):
        store = self.store
        odd = "caf\u00e9 \u4e2d\u6587\n---\nline with\rcarriage return\n<!-- recortex:seal v1 key=%s mac=%s -->\n\n\n" % ("a" * 16, "b" * 64)
        store.create_issue("Odd \"title\" \u00fcber", ["final-record", "topic:odd"], body=odd)
        number = 5
        store.edit_body(number, body=odd + "more")
        address = store.post_entry(number, body=odd)
        store.set_labels(number, ["rd:methods"], [])
        store.close(number, "completed")
        store.reopen(number)
        self.assertIn("odd-title-ber.md", self.files("topics"))
        problems = []
        for place, _, names_ in os.walk(self.folder):
            for name in names_:
                if name == "record.json":
                    continue
                path = os.path.join(place, name)
                relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
                parsed = self.local_store.parse_file(self.read(path), relpath, self.record_id)
                if parsed.state != "sealed":
                    problems.append(relpath)
        self.assertEqual([], problems)
        self.assertEqual(odd, store.read_entry(store.posted_id(address))["body"])
        self.assertEqual(odd + "more", store.read_issue(number)["body"])
        self.assertEqual("Odd \"title\" \u00fcber", store.read_issue(number)["title"])

    def test_the_check_refuses_when_the_two_halves_disagree(self):
        front = {"recortex": 1, "kind": "entry", "id": "p7d2n5v3c5x4b6aq", "topic": 2, "created": "2026-11-23T14:12:07Z"}
        relpath = "topics/reader/2026-11-23T1412Z-p7d2n5v3c5x4b6aq.md"
        self.assertTrue(self.local_store.checked("entry", front, "ok\n", relpath, self.record_id))
        real = self.local_store.parse_file

        def other_body(data, path, record):
            parsed = real(data, path, record)
            parsed.body = "something else\n"
            return parsed
        with mock.patch.object(self.local_store, "parse_file", side_effect=other_body):
            with self.assertRaises(self.local_store.Refused):
                self.local_store.checked("entry", front, "ok\n", relpath, self.record_id)

        def other_state(data, path, record):
            parsed = real(data, path, record)
            parsed.state = "changed"
            return parsed
        with mock.patch.object(self.local_store, "parse_file", side_effect=other_state):
            with self.assertRaises(self.local_store.Refused):
                self.local_store.checked("entry", front, "ok\n", relpath, self.record_id)
        with self.assertRaises(self.local_store.Refused):
            self.local_store.checked("entry", dict(front, id="BAD"), "ok\n", relpath, self.record_id)


if __name__ == "__main__":
    unittest.main()

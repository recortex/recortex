"""The record folder's rules (1.9.0, section 6 of the spec): where a local record may be, what is checked on the way, how a file is
read and written, the write lock and the machine's binding of a record id to a folder. Offline; every folder is made in the test's
own temporary folder, and HOME and the XDG folders point into it."""

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
sys.dont_write_bytecode = True
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
        os.mkdir(os.path.join(path, "topics"))
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
        # the guard of 12.4: local_store.py and local_sync.py name none of the calls that open, list, move or remove a file
        banned = ("open(", "os.open", "os.scandir", "os.listdir", "os.replace", "os.rename", "os.link", "os.unlink",
                  "os.remove", "shutil", "pathlib")
        for name in ("local_store.py", "local_sync.py"):
            path = os.path.join(SCRIPTS, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                lines = [line.split("#", 1)[0] for line in handle if not line.lstrip().startswith(('"""', "#"))]
            code = "\n".join(lines)
            for word in banned:
                self.assertNotIn(word, code, "%s uses %s" % (name, word))


if __name__ == "__main__":
    unittest.main()

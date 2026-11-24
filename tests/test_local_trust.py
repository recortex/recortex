"""The trust of a local record (1.9.0, section 8): which folder is used, what is read as the user's own, the two marks, and that
no text of a record file reaches a payload outside the data frame. Offline; every folder is made in the test's own sandbox."""

import io
import json
import os
import re
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import local_record  # noqa: E402

PLANT = "ZZPLANT"


class Box(local_record.LocalSandbox):
    def setUp(self):
        super().setUp()
        import brief
        import hooks
        import index
        self.brief, self.hooks, self.index = brief, hooks, index

    def sync(self, **kwargs):
        return self.index.sync(self.repo, hub=1, log=lambda *a: None, **kwargs)

    def payloads(self):
        self.sync()
        db = self.index.connect(self.repo, create=False)
        try:
            return self.brief.all_payloads(db, self.config, self.repo, True)
        finally:
            db.close()

    def refused(self, kind):
        with self.assertRaises(self.record_store.RecordFolderError) as caught:
            self.record_store.for_repo(self.repo, self.config)
        self.assertEqual(kind, caught.exception.kind)
        self.assertNotIn(self.folder, str(caught.exception))
        return caught.exception


class OpenOwn(Box):
    """The folder is used when all hold: the name is trusted, a row binds the id to the folder, the folder passes the rules."""

    seeded = False

    def test_the_store_opens_when_the_row_the_folder_and_record_json_agree(self):
        store = self.record_store.for_repo(self.repo, self.config)
        self.assertEqual(("local", self.folder), (store.kind, store.folder))
        self.assertIs(store, self.record_store.for_repo(self.repo, self.config))   # one per process and name

    def test_a_local_name_opens_nothing_in_a_project_that_is_not_that_record(self):
        for config in ({"repo": "octo-org/octo-repo"}, {"repo": "local--record/q7c2m5t3v4x6b2da", "store": "local", "place": "own"},
                       {"repo": self.repo}, {}):
            with self.subTest(config=config):
                with self.assertRaises(SystemExit):
                    self.record_store.for_repo(self.repo, config)

    def test_a_machine_with_no_row_binds_nothing(self):
        os.remove(os.path.join(self.dir, "state", "recortex", "private", "local-records.json"))
        self.assertEqual("unbound", self.refused("unbound").kind)

    def test_a_row_that_was_edited_or_signed_with_another_key_binds_nothing(self):
        file = os.path.join(self.dir, "state", "recortex", "private", "local-records.json")
        data = json.loads(self.read(file))
        data["rows"][0]["path"] = os.path.join(self.dir, "elsewhere")
        self.write(file, json.dumps(data).encode())
        self.refused("unbound")

    def test_the_trusted_list_gives_a_local_name_the_owner_word_and_no_login(self):
        recortex = self.record_store.recortex
        self.assertEqual({"local--record"}, recortex.allowed_users(self.repo))
        self.write_trusted(" octocat hubot")
        self.assertEqual({"local--record"}, recortex.allowed_users(self.repo))
        os.remove(self.trusted_file)
        self.assertEqual(set(), recortex.allowed_users(self.repo))   # not trusted: nobody

    def test_a_symlink_a_write_bit_and_another_owner_stop_the_store(self):
        moved = self.folder + ".real"
        os.rename(self.folder, moved)
        os.symlink(moved, self.folder)
        self.refused("symlink")
        os.remove(self.folder)
        os.rename(moved, self.folder)
        os.chmod(self.folder, 0o777)
        self.refused("permissions")
        os.chmod(self.folder, 0o700)
        self.record_store.for_repo(self.repo, self.config)
        rows = [dict(row, uid=row["uid"] + 1) for row in self.storage.record_bindings(self.record_id)]
        with mock.patch.object(self.storage, "record_bindings", return_value=rows):
            error = self.refused("moved")   # the folder's owner is not the one this machine approved
        self.assertIn("owner changed", str(error))

    def test_a_folder_that_is_missing_stops_the_store(self):
        os.rename(self.folder, self.folder + ".gone")
        self.assertIn(self.refused("missing").kind, ("missing",))

    def test_record_json_that_does_not_pass_stops_the_store(self):
        record = os.path.join(self.folder, "record.json")
        self.write(record, self.read(record).replace(b"sample project", b"edited"))
        with self.assertRaises(self.record_store.RecordFolderError) as caught:
            self.local_store.LocalStore(self.repo, self.config, self.folder, "own")
        self.assertEqual("record-json", caught.exception.kind)


class OpenProject(OpenOwn):
    place = "project"

    def test_a_copied_checkout_has_the_id_bound_to_another_folder_and_reads_nothing(self):
        import shutil
        copy = os.path.join(self.outside, "copy")
        shutil.copytree(self.project, copy)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": copy}):
            error = self.refused("moved")
        self.assertIn("bound to another folder", str(error))

    def test_a_cloned_project_with_its_own_record_has_an_id_this_machine_never_trusted(self):
        other = os.path.join(self.outside, "clone")
        os.makedirs(os.path.join(other, ".recortex"))
        os.chmod(os.path.join(other, ".recortex"), 0o755)
        os.chmod(other, 0o755)
        self.local_store.LocalStore.create(os.path.join(other, ".recortex"), "q7c2m5t3v4x6b2da", "clone", ["software"])
        config = {"repo": "local--record/q7c2m5t3v4x6b2da", "hub": 1, "store": "local", "place": "project"}
        with open(os.path.join(other, ".recortex.json"), "w") as handle:
            json.dump(config, handle)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": other}):
            out = self.run_hook("session-start")
            self.assertIn("this repository is not in your trusted list", out)   # as for an untrusted GitHub repository: no brief
            self.assertNotIn("BEGIN PROJECT STATUS DATA", out)
            self.write_trusted("\nlocal--record/q7c2m5t3v4x6b2da")
            with self.assertRaises(self.record_store.RecordFolderError) as caught:
                self.record_store.for_repo(config["repo"], config)
            self.assertEqual("unbound", caught.exception.kind)

    def run_hook(self, event):
        done = self.run_script("hooks.py", event, stdin=json.dumps({"session_id": "s1", "source": "startup"}))
        return done.stdout

    def test_a_folder_above_that_another_account_can_replace_is_refused_until_it_is_trusted_as_it_is(self):
        os.chmod(self.outside, 0o777)
        self.assertIn(self.refused("ancestor-permissions").kind, ("ancestor-permissions",))
        import observer_sources
        with mock.patch.object(observer_sources.Trust, "accepts", return_value=True):
            self.record_store.for_repo(self.repo, self.config)


class OpenChosen(OpenOwn):
    place = "chosen"

    def setUp(self):
        super().setUp()
        self.config["place"] = "own"   # the project file names `own`; a chosen folder is the one the row binds

    def test_a_link_above_the_folder_is_refused(self):
        real = os.path.join(self.outside, "real-parent")
        os.mkdir(real)
        os.chmod(real, 0o755)
        os.rename(self.folder, os.path.join(real, "chosen-record"))
        os.symlink(real, os.path.join(self.outside, "linked-parent"))
        row = self.storage.bind_record(self.record_id, os.path.join(self.outside, "linked-parent", "chosen-record"), "chosen")
        self.assertEqual("chosen", row["place"])
        self.assertEqual("symlink", self.refused("symlink").kind)


class Marks(Box):
    """A foreign key and a changed entry are never the user's words: not a decision, not a resume block, not a ledger row."""

    def test_a_foreign_decision_is_no_decision_and_the_recall_says_whose(self):
        self.damage("foreign-entry", "reader")
        self.sync()
        cid = self.entries["reader"][0]
        db = self.index.connect(self.repo, create=False)
        found = self.index.memories(db, {"local--record"})
        self.assertNotIn(cid, found["items"])
        self.assertEqual(1, found["hidden"])
        recall = self.run_script("index.py", "query", "recall", "--issue", "2")
        self.assertEqual(0, recall.returncode, recall.stderr)
        self.assertIn("1 memory written on another machine is not listed.", recall.stdout)
        self.assertIn("Data read from the local record follows", recall.stdout)
        self.assertNotIn("GitHub", recall.stdout)
        self.assertNotIn("Make strict the default", recall.stdout)

    def test_a_changed_entry_is_counted_as_changed_outside_recortex_and_not_told(self):
        self.damage("changed-entry")
        self.sync()
        recall = self.run_script("index.py", "query", "recall", "--issue", "2")
        self.assertIn("1 memory changed outside recortex is not listed.", recall.stdout)
        self.assertNotIn("Decision (edited)", recall.stdout)
        self.assertNotIn("changed on GitHub", recall.stdout)
        mind = self.run_script("index.py", "query", "mind")
        self.assertIn("not told: 0 written on another machine, 1 changed outside recortex", mind.stdout)

    def test_a_foreign_resume_block_is_not_shown_and_the_brief_says_why(self):
        path = self.entry_path("hub", 0)
        relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
        parsed = self.local_store.parse_file(self.read(path), relpath, self.record_id)
        other = self.local_store.Sealing(b"another machine")
        self.write(path, self.local_store.render("entry", parsed.front, parsed.body, relpath, self.record_id, other))
        payloads = self.payloads()
        self.assertNotIn("sample reader handles quoted fields", payloads["session-start"])
        self.assertIn("written on another machine", payloads["session-start"])
        self.assertIn("Resume block:", payloads["session-start"])

    def test_a_changed_resume_block_is_not_shown_either(self):
        path = self.entry_path("hub", 0)
        self.write(path, self.read(path).replace(b"Speed of the reader", b"Speed of nothing"))
        payloads = self.payloads()
        self.assertNotIn("Speed of nothing", payloads["session-start"])
        self.assertIn("changed outside recortex", payloads["session-start"])

    def test_a_changed_or_foreign_ledger_row_does_not_count_and_a_sealed_one_does(self):
        import project_map_io
        marker = self.index.LEDGER_MARKERS[0]
        store = self.store
        made = [int(store.posted_id(store.post_entry(1, body=marker + " row %d\n" % n))) for n in range(3)]
        hub = os.path.join(self.folder, "hub")
        files = sorted(name for name in os.listdir(hub) if self.local_store.entry_number(
            self.local_store.ENTRY_NAME.fullmatch(name).group(2)) in made)
        changed = os.path.join(hub, files[0])
        self.write(changed, self.read(changed).replace(b"row 0", b"row X"))
        foreign = os.path.join(hub, files[1])
        relpath = "hub/" + files[1]
        parsed = self.local_store.parse_file(self.read(foreign), relpath, self.record_id)
        other = self.local_store.Sealing(b"another machine")
        self.write(foreign, self.local_store.render("entry", parsed.front, parsed.body, relpath, self.record_id, other))
        rows = project_map_io.verified_rows(self.repo, 1, marker)
        self.assertEqual([made[2]], [row["id"] for row in rows])


class Frame(Box):
    """Text from a record file reaches a payload only inside the data frame, and what is not told reaches none."""

    def plant(self):
        """A topic whose title, labels and slug carry a marker; entries sealed, changed and foreign that carry one; a file with
        a marker in its name; a record.json name with one."""
        store = self.store
        store.create_issue("%s title of a topic" % PLANT, ["final-record", "zzplant-label"], body="## Changed\n\n%s body text\n" % PLANT)
        store.post_entry(2, body="<!-- ghim:event type=result at=2026-11-23T17:00:00Z -->\n### Result: 2026-11-23 17:00 UTC - %s sealed"
                         "\n\n**In plain words, we found that:** %s sealed entry text\n\n**What ran:** `x` at commit 4f2a9c1.\n\n"
                         "**Configuration:** one.\n\n**Numbers:**\n\n| Measure | Value | Denominator | Source path |\n|---|---|---|---|\n"
                         "| A | 1 | 1 | `a.tsv` |\n\n**Exclusions and failures:** None.\n\n**Not established:** Nothing.\n\n"
                         "**Next step:** None, owner nobody\n" % (PLANT, PLANT))
        self.damage("changed-entry")
        path = self.entry_path("speed", 0)
        text = self.read(path).replace(b"**What ran:**", ("**What ran:** %sCHANGED" % PLANT).encode(), 1)
        self.write(path, text)
        relpath = os.path.relpath(self.entry_path("speed", 1), self.folder).replace(os.sep, "/")
        parsed = self.local_store.parse_file(self.read(self.entry_path("speed", 1)), relpath, self.record_id)
        other = self.local_store.Sealing(b"another machine")
        self.write(self.entry_path("speed", 1), self.local_store.render("entry", parsed.front, "%sFOREIGN text" % PLANT, relpath,
                                                                       self.record_id, other))
        self.write(os.path.join(self.folder, "topics", "%s-notes.txt" % PLANT.lower()), b"x")
        record = os.path.join(self.folder, "record.json")
        data = json.loads(self.read(record))
        data["name"] = "%s name" % PLANT
        plain = {k: v for k, v in data.items() if k != "seal"}
        data["seal"]["mac"] = self.local_store.sealing().json_mac(plain)
        self.write(record, json.dumps(data).encode())

    def outside_and_inside(self, text):
        begin = text.index(self.brief.BEGIN) if self.brief.BEGIN in text else None
        end = text.index(self.brief.END) if self.brief.END in text else None
        if begin is None:
            return text, ""
        return text[:begin] + (text[end:] if end is not None else ""), text[begin:end]

    def test_no_marker_stands_outside_the_frame_in_any_of_the_four_payloads(self):
        self.plant()
        payloads = self.payloads()
        self.assertEqual(4, len(payloads))
        for name, text in payloads.items():
            outside, inside = self.outside_and_inside(text)
            for marker in (PLANT, PLANT.lower(), "reader-quoting", "topics/", self.folder, "record.json"):
                self.assertNotIn(marker, outside, "%s holds %s outside the frame" % (name, marker))
        start = payloads["session-start"]
        self.assertIn("local--record/k3f5x2m7q4w6z5ab", start)   # the record's name is the one thing from the record outside it
        for name, text in payloads.items():
            self.assertNotIn("GitHub", text.replace("GitHub issues of", ""), name)
            self.assertNotIn("%sCHANGED" % PLANT, text, name)   # a changed entry's text is told nowhere
            self.assertNotIn("%sFOREIGN" % PLANT, text, name)   # nor a foreign one's
        self.assertIn("%s title of a topic" % PLANT, self.outside_and_inside(start)[1])   # a sealed title is told, inside the frame

    def test_the_hook_prints_no_marker_outside_the_frame_either(self):
        self.plant()
        self.sync()
        done = self.run_script("hooks.py", "session-start", stdin=json.dumps({"session_id": "s1", "source": "startup"}))
        self.assertEqual(0, done.returncode, done.stderr)
        outside, inside = self.outside_and_inside(done.stdout)
        self.assertIn(self.brief.BEGIN, done.stdout)
        for marker in (PLANT, PLANT.lower(), self.folder):
            self.assertNotIn(marker, outside)
        self.assertIn("files in the record folder were not read", outside)   # the count, in fixed words and a number


class Hooks(Box):
    seeded = False

    def hook(self, event="session-start"):
        self.sync()
        return self.run_script("hooks.py", event, stdin=json.dumps({"session_id": "s1", "source": "startup"}))

    def test_a_record_folder_that_cannot_be_used_is_one_fixed_line_by_its_kind(self):
        self.sync()
        os.chmod(self.folder, 0o777)
        done = self.run_script("hooks.py", "session-start", stdin=json.dumps({"session_id": "s1", "source": "startup"}))
        self.assertIn("record brief not shown: %s. Run hooks.py --doctor." % self.hooks.STORAGE_REASONS["permissions"], done.stdout)
        self.assertIn("Run hooks.py --doctor.", done.stdout)
        self.assertNotIn(self.folder, done.stdout)

    def test_each_kind_of_a_refused_record_folder_has_a_fixed_sentence(self):
        for kind in ("missing", "unbound", "moved", "symlink", "owner", "permissions", "ancestor-owner", "ancestor-permissions",
                     "record-json", "duplicate", "too-many", "place", "unreadable", "not-folder", "size", "busy"):
            with self.subTest(kind=kind):
                self.assertIn(kind, self.hooks.STORAGE_REASONS)
                self.assertTrue(self.hooks.STORAGE_REASONS[kind].islower() or self.hooks.STORAGE_REASONS[kind][0].islower())

    def test_a_record_name_and_a_kind_that_disagree_keep_the_hooks_silent(self):
        self.config["store"] = "github"
        self.write_config(self.config)
        done = self.run_script("hooks.py", "session-start", stdin=json.dumps({"session_id": "s1", "source": "startup"}))
        self.assertIn("off in this session", done.stdout)   # the one line of the status notice, and no brief
        self.assertNotIn("BEGIN PROJECT STATUS DATA", done.stdout)
        self.assertFalse(self.hooks.kind_agrees(self.config))

    def test_the_doctor_says_gh_is_not_needed_and_names_the_folder_and_what_was_not_read(self):
        self.store.create_issue("Hub", ["hub"], body="x")
        self.write(os.path.join(self.folder, "topics", "stray-note.txt") if os.path.isdir(os.path.join(self.folder, "topics"))
                   else os.path.join(self.folder, "stray-note.txt"), b"x")
        self.sync()
        done = self.run_script("hooks.py", "--doctor")
        self.assertIn("gh: not needed for a local record", done.stdout)
        self.assertNotIn("gh: found", done.stdout)
        self.assertIn("record: a local record (place own)", done.stdout)
        self.assertIn("record folder in use: %s" % self.folder, done.stdout)
        self.assertIn("files by seal: 2 sealed, 0 changed outside recortex, 0 written under another key, 0 not read", done.stdout)
        self.assertIn("not read: stray-note.txt (name)", done.stdout)

    def test_the_status_notice_names_the_record_folder_when_it_could_not_be_read(self):
        notice = self.hooks.NOTICE_FORM
        for line in ("recortex: on. Record local--record/k3f5x2m7q4w6z5ab, read just now; the record folder could not be read now.",
                     "recortex: on. Record octo-org/octo-repo, read just now; GitHub could not be read now."):
            self.assertRegex(line, notice)


class Discovery(local_record.LocalSandbox):
    """The discovery line is offered at the top of a git checkout with a GitHub remote or with no remote, and stays silent elsewhere."""

    seeded = False
    trusted = False

    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.project, ".recortex.json"))   # a project with no record yet

    def checkout(self, remote=None):
        root = os.path.join(self.dir, "checkout")
        os.makedirs(root)
        subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
        if remote:
            subprocess.run(["git", "-C", root, "remote", "add", "origin", remote], check=True, capture_output=True)
        return root

    def hook(self, root):
        return self.run_script("hooks.py", "session-start", stdin=json.dumps({"session_id": "s1", "source": "startup"}),
                               env={"CLAUDE_PROJECT_DIR": root}).stdout

    def test_a_git_checkout_with_no_remote_gets_the_line_once(self):
        root = self.checkout()
        first = self.hook(root)
        self.assertIn("this project has no record yet", first)
        self.assertEqual("", self.hook(root))

    def test_a_github_remote_keeps_its_line_and_another_remote_stays_silent(self):
        self.assertIn("this project has no record yet", self.hook(self.checkout("git@github.com:octo-org/x.git")))
        os.rename(os.path.join(self.dir, "checkout"), os.path.join(self.dir, "first"))
        self.assertEqual("", self.hook(self.checkout("https://gitlab.example.org/a/b.git")))


if __name__ == "__main__":
    unittest.main()

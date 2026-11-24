"""The three acts a local record adds (1.9.0, spec 9.3 and 9.5): `record-in-project` (a record inside the project, asked before the
project is set up), `folder` (this machine has not approved the folder a record is at) and `adopt` (take back as the user's own
what was changed outside recortex, or written under another key). Each is a question printed by `acts.py`, answered in the
dialog, and taken once from a grant bound to exactly its values. Offline; the failing `gh` of the sandbox must stay unused."""

import contextlib
import io
import json
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_observer_acts as base  # noqa: E402  (the class is used by its module's name: it holds no tests of its own)
import acts  # noqa: E402
import observer_grants  # noqa: E402
import record_store  # noqa: E402
import storage  # noqa: E402
import local_store  # noqa: E402
import setup  # noqa: E402

LOCAL_ID = "k3f5x2m7q4w6z5ab"
LOCAL = "local--record/" + LOCAL_ID
SCRIPTS = base.SCRIPTS


class LocalActsBox(base.ActsBox):
    """The acts box with a local record in it: bound to this machine, trusted, with a hub, two topics and an entry in each."""

    place = "own"

    def setUp(self):
        super().setUp()
        self.gh_log = os.path.join(self.base, "gh-calls.log")
        script = os.path.join(self.base, "no-gh.sh")
        with open(script, "w") as handle:
            handle.write("#!/bin/sh\necho \"$@\" >> %s\nexit 1\n" % self.gh_log)
        os.chmod(script, 0o755)
        patch = mock.patch.dict(os.environ, {"RECORTEX_GH": script, "RECORTEX_SETUP_QUESTIONS": "0", "RECORTEX_LINK_PAUSE": "0",
                                             "PYTHONDONTWRITEBYTECODE": "1", "RECORTEX_TRUSTED_REPOS": "%s,%s,%s" % (
                                                 base.REPO, base.OTHER, LOCAL)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.no_gh_was_started)
        self.moment = 1795000000
        patcher = mock.patch.object(local_store, "now", side_effect=self.tick)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {"repo": LOCAL, "hub": 1, "store": "local", "place": self.place, "profiles": ["software"]}
        with open(os.path.join(self.project, ".recortex.json"), "w") as handle:
            json.dump(self.config, handle)
        self.folder_path = self.make_record(self.project)
        self.numbers = self.seed()

    def tick(self):
        self.moment += 60
        return self.moment

    def no_gh_was_started(self):
        self.assertFalse(os.path.exists(self.gh_log), "a gh call was started for a local record")

    def make_record(self, project):
        if self.place == "project":
            path = storage.check_record_folder(storage.record_path(project), "project", create=True)
        else:
            path = storage.check_record_folder(storage.own_record_folder(LOCAL_ID, create=True), "own")
        local_store.LocalStore.create(path, LOCAL_ID, "sample project", ["software"])
        storage.bind_record(LOCAL_ID, path, self.place)
        return path

    @property
    def store(self):
        return record_store.for_repo(LOCAL, self.config)

    def seed(self):
        store = self.store
        hub = store.created_number(store.create_issue("Project record hub", ["hub"], body="The hub of the sample project.\n"))
        first = store.created_number(store.create_issue("Reader quoting", ["final-record", "topic:reader"],
                                                        body="The reader accepts strict quoting only.\n"))
        second = store.created_number(store.create_issue("Read speed", ["final-record", "topic:speed"],
                                                         body="The reader runs at the speed of the standard library.\n"))
        store.post_entry(first, body="### Result: strict quoting\n\nThe reader refused a stray quote in 3 of 3 files.\n")
        store.post_entry(second, body="### Result: speed\n\nThe reader ran 1.2 times slower on a 10 MB file.\n")
        return {"hub": hub, "reader": first, "speed": second}

    def path_of(self, key):
        return os.path.join(self.folder_path, "hub.md" if key == "hub" else os.path.join(
            "topics", "reader-quoting.md" if key == "reader" else "read-speed.md"))

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def write(self, path, data):
        os.chmod(path, 0o600)
        with open(path, "wb") as handle:
            handle.write(data)

    def command_for(self, *words):
        return self.command("setup.py", *words)


class ActTableTests(LocalActsBox):
    def test_the_three_acts_are_in_the_table_and_in_the_grant_kinds(self):
        for kind in ("record-in-project", "folder", "adopt"):
            self.assertIn(kind, acts.ACTS)
            self.assertIn(kind, observer_grants.ACT_KINDS)
        grant_rows = sorted(kind for kind, act in acts.ACTS.items() if act.grant)
        self.assertEqual(grant_rows, sorted(observer_grants.ACT_KINDS))

    def test_every_option_description_fits_the_fixed_form_and_the_question_is_printed(self):
        self.damage_topic("reader")
        for argv in (("record-in-project", "public"), ("record-in-project", "private"), ("record-in-project", "unknown"),
                     ("record-in-project", "unknown", "--path", "/tmp/answers.json"), ("folder",),
                     ("adopt", "topic", str(self.numbers["reader"]))):
            with self.subTest(argv=argv):
                code, printed = self.ask_main(*argv, "--json")
                self.assertEqual(0, code, printed)
                body = json.loads(printed)
                for option in body["options"]:
                    self.assertRegex(option["description"], acts.DESCRIPTION)
                self.assertEqual("choice", body["form"])
                self.assertLessEqual(len(body["header"]), 12)

    def damage_topic(self, key):
        path = self.path_of(key)
        self.write(path, self.read(path).replace(b"strict quoting", b"lenient quoting", 1))

    def test_the_words_of_each_question_are_fixed(self):
        values = {"project": self.project, "state": "unknown", "answers": None}
        self.assertEqual("recortex: the record will be in .recortex inside this project. If you commit it, it is committed with "
                         "your code, and in a public repository anyone can read it. Keep the record inside the project?",
                         acts.ACTS["record-in-project"].text(values))
        self.assertEqual(["Keep it inside the project", "Use its own folder instead"], acts.ACTS["record-in-project"].labels(values))
        folder = {"id": LOCAL_ID, "path": "0" * 64}
        self.assertEqual("recortex: this project's local record is now at a folder this machine has not approved for it (record %s). "
                         "Use the record at its new place?" % LOCAL_ID, acts.ACTS["folder"].text(folder))
        self.assertEqual(["Use the record there", "Leave it unused"], acts.ACTS["folder"].labels(folder))
        topic = {"mode": "topic", "topic": 3, "lines": 4, "entries": 0, "topics": 0, "digest": "0" * 64}
        self.assertEqual("recortex: topic #3 of the local record was changed outside recortex (4 lines differ from the last version "
                         "recortex wrote). Take the text as it is now as yours?", acts.ACTS["adopt"].text(topic))
        self.assertEqual(["Take topic #3 as mine", "Leave it as it is"], acts.ACTS["adopt"].labels(topic))
        foreign = {"mode": "foreign", "topic": 0, "lines": 0, "entries": 2, "topics": 1, "digest": "0" * 64}
        self.assertEqual("recortex: 2 entries and 1 topics of this record were written under another key (another machine, or this "
                         "one before its key changed). Seal them as yours on this machine?", acts.ACTS["adopt"].text(foreign))
        self.assertEqual(["Seal them as mine", "Leave them as they are"], acts.ACTS["adopt"].labels(foreign))

    def test_no_question_prints_a_path_or_a_text_of_the_record(self):
        self.damage_topic("reader")
        for argv in (("folder",), ("adopt", "topic", str(self.numbers["reader"]))):
            code, printed = self.ask_main(*argv)
            self.assertEqual(0, code)
            for secret in (self.folder_path, self.base, "lenient quoting", "strict quoting"):
                self.assertNotIn(secret, printed)

    def test_the_question_of_a_record_inside_the_project_needs_no_project_file_and_the_others_do(self):
        os.remove(os.path.join(self.project, ".recortex.json"))
        self.assertEqual(0, self.ask_main("record-in-project", "unknown")[0])
        for argv in (("folder",), ("adopt", "foreign")):
            code, printed = self.ask_main(*argv)
            self.assertEqual((1, acts.NOT_SET_UP + "\n"), (code, printed))

    def test_the_value_of_an_in_project_answer_is_the_project_folder_alone(self):
        one = {"project": "/srv/one", "state": "public", "answers": None}
        two = {"project": "/srv/two", "state": "public", "answers": None}
        again = {"project": "/srv/one", "state": "private", "answers": "/srv/one/a.json"}
        act = acts.ACTS["record-in-project"]
        self.assertNotEqual(act.value(one), act.value(two))
        self.assertEqual(act.value(one), act.value(again))   # what is known of the repository is not part of the value
        self.assertEqual(act.text(one), act.text(again))      # nor of the question: the answer is to this text

    def test_a_folder_values_bind_the_id_and_the_real_path(self):
        act = acts.ACTS["folder"]
        values = act.parse(mock.Mock(values=[], path=None), acts.Context())
        self.assertEqual(LOCAL_ID, values["id"])
        other = dict(values, path="1" * 64)
        self.assertNotEqual(act.value(values), act.value(other))
        self.assertNotIn(self.folder_path, json.dumps(values))


class InProjectGrantTests(LocalActsBox):
    """9.3: a session's `setup.py apply` for a record inside the project takes the user's answer, once, for this project."""

    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.base, "other-project")
        os.mkdir(self.other, 0o700)
        self.values = {"project": os.path.realpath(self.project), "state": "unknown", "answers": None}

    def answers_file(self, project, place="project"):
        path = os.path.join(project, "answers.json")
        with open(path, "w") as handle:
            json.dump({"record": "local", "place": place, "deliverables": [{"kind": "software"}]}, handle)
        return path

    def apply_command(self, project, flag="--yes"):
        return self.command_for("apply", "--answers", self.answers_file(project), "--root", project, flag)

    def test_the_command_asks_for_the_grant_of_its_own_project_and_labels_it(self):
        found = acts.asks(self.apply_command(self.project), self.project)
        self.assertEqual(["record-in-project"], [item["kind"] for item in found])
        self.assertEqual("Keep it inside the project", found[0]["label"])
        self.assertEqual(acts.ACTS["record-in-project"].value(self.values), found[0]["value"])

    def test_a_dry_run_an_own_folder_and_another_kind_of_answers_ask_for_nothing(self):
        self.assertEqual([], acts.asks(self.apply_command(self.project, "--dry-run"), self.project))
        own = self.command_for("apply", "--answers", self.answers_file(self.project, place="own"), "--root", self.project, "--yes")
        self.assertEqual([], acts.asks(own, self.project))
        github = os.path.join(self.project, "github.json")
        with open(github, "w") as handle:
            json.dump({"repo": "octo-org/octo-repo", "deliverables": [{"kind": "software"}]}, handle)
        command = self.command_for("apply", "--answers", github, "--root", self.project, "--yes")
        self.assertEqual([], acts.asks(command, self.project))

    def test_without_the_answer_nothing_is_taken_and_the_refusal_names_the_question(self):
        self.assertIsNone(self.hook(self.apply_command(self.project)))
        self.assertFalse(acts.allowed("record-in-project", self.values))
        note = acts.refusal("record-in-project", self.values)["note"]
        self.assertIn("acts.py ask record-in-project", note)

    def test_the_answer_yes_is_taken_once(self):
        self.yes("record-in-project", self.values)
        self.assertIsNone(self.hook(self.apply_command(self.project)))
        self.assertTrue(self.said("record-in-project", self.values))
        self.assertTrue(acts.allowed("record-in-project", self.values))
        self.assertFalse(acts.allowed("record-in-project", self.values))   # one time only

    def test_an_answer_about_this_project_covers_no_other_folder(self):
        other = dict(self.values, project=os.path.realpath(self.other))
        self.assertEqual([], acts.asks(self.apply_command(self.other), self.project))   # another --root: no grant is asked for
        self.yes("record-in-project", self.values)
        self.assertIsNone(self.hook(self.apply_command(self.other)))
        self.assertFalse(self.said("record-in-project", self.values))
        self.assertFalse(self.said("record-in-project", other))
        self.assertFalse(acts.allowed("record-in-project", other))
        self.assertIsNone(self.hook(self.apply_command(self.project)))
        self.assertTrue(self.said("record-in-project", self.values))
        self.assertFalse(acts.allowed("record-in-project", other))   # the grant of this project is not the other's

    def test_the_other_answer_and_a_reworded_question_grant_nothing(self):
        act = acts.ACTS["record-in-project"]
        self.answer("record-in-project", self.values, "Use its own folder instead")
        self.hook(self.apply_command(self.project))
        self.assertFalse(self.said("record-in-project", self.values))
        self.answer("record-in-project", self.values, act.label(self.values), text=act.text(self.values).replace("anyone", "someone"))
        self.hook(self.apply_command(self.project))
        self.assertFalse(self.said("record-in-project", self.values))
        self.answer("record-in-project", self.values, act.label(self.values), labels=["Keep it inside the project", "Use its own "
                                                                                    "folder instead", "Ask me later"])
        self.hook(self.apply_command(self.project))
        self.assertFalse(self.said("record-in-project", self.values))

    def test_a_subagents_call_records_no_grant(self):
        self.yes("record-in-project", self.values)
        self.hook(self.apply_command(self.project), agent_id="agent-1", agent_type="general-purpose")
        self.assertFalse(self.said("record-in-project", self.values))   # the main session's call alone records one
        self.assertIsNone(self.hook(self.apply_command(self.project, "--dry-run"), agent_id="agent-1"))   # a dry run is no act

    def test_apply_in_a_session_takes_the_grant_and_refuses_without_it(self):
        project = os.path.realpath(self.project)
        os.remove(os.path.join(project, ".recortex.json"))
        argv = ["apply", "--answers", self.answers_file(project), "--root", project, "--yes"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            setup.main(argv)
        self.assertIn("needs your answer to its question", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(project, ".recortex")))
        self.yes("record-in-project", self.values)
        self.hook(self.apply_command(project))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(argv)
        self.assertEqual(0, code, out.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(project, ".recortex", "record.json")))
        self.assertFalse(self.said("record-in-project", self.values))   # spent


class FolderTests(LocalActsBox):
    """9.5 `folder`: a record at a folder this machine has not approved."""

    def unbind(self):
        os.remove(storage._binding_file())

    def test_a_record_with_no_row_for_its_folder_is_refused_and_the_question_names_the_id(self):
        self.unbind()
        with self.assertRaises(record_store.StoreError) as caught:
            self.store
        self.assertEqual("unbound", caught.exception.kind)
        code, printed = self.ask_main("folder")
        self.assertEqual(0, code)
        self.assertIn("(record %s)" % LOCAL_ID, printed)
        self.assertNotIn(self.folder_path, printed)

    def run_command(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(list(words))
        return code, out.getvalue()

    def test_it_is_refused_without_the_answer_and_taken_once_with_it(self):
        self.unbind()
        code, printed = self.run_command("record-folder", "--yes")
        self.assertEqual(1, code)
        self.assertIn("acts.py ask folder", printed)
        with self.assertRaises(record_store.StoreError):
            self.store
        values = acts.ACTS["folder"].parse(mock.Mock(values=[], path=None), acts.Context())
        self.yes("folder", values)
        self.assertIsNone(self.hook(self.command_for("record-folder", "--yes")))
        code, printed = self.run_command("record-folder", "--yes")
        self.assertEqual(0, code, printed)
        self.assertEqual(LOCAL_ID, self.store.record_id)
        self.assertEqual(self.numbers["reader"], self.store.topic(self.numbers["reader"])[1].number)
        code, printed = self.run_command("record-folder", "--yes")   # the grant was spent
        self.assertEqual(1, code)

    def test_without_yes_it_writes_nothing(self):
        self.unbind()
        with self.assertRaises(SystemExit) as caught:
            self.run_command("record-folder")
        self.assertIn("--yes", str(caught.exception))
        self.assertEqual([], storage.record_bindings() if os.path.exists(storage._binding_file()) else [])

    def test_the_grant_is_for_the_folder_it_was_recorded_for_and_not_for_another(self):
        self.unbind()
        values = acts.ACTS["folder"].parse(mock.Mock(values=[], path=None), acts.Context())
        self.yes("folder", values)
        self.hook(self.command_for("record-folder", "--yes"))
        self.assertTrue(self.said("folder", values))
        self.assertFalse(acts.allowed("folder", dict(values, path="f" * 64)))   # a folder swapped in after the hook
        self.assertTrue(self.said("folder", values))                             # and the grant is still there, unused
        self.assertTrue(acts.allowed("folder", values))

    def test_a_project_with_no_local_record_has_nothing_to_approve(self):
        with open(os.path.join(self.project, ".recortex.json"), "w") as handle:
            json.dump({"repo": base.REPO, "hub": 1}, handle)
        code, printed = self.ask_main("folder")
        self.assertEqual((1, "acts: this project keeps no local record.\n"), (code, printed))


class FolderInProjectTests(LocalActsBox):
    place = "project"

    def test_a_copy_of_the_project_is_a_new_checkout_that_the_user_approves_separately(self):
        moved = os.path.join(self.base, "moved-project")
        shutil.copytree(self.project, moved)
        for folder, names_, files in os.walk(moved):
            os.chmod(folder, 0o700)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": moved}):
            with self.assertRaises(record_store.StoreError) as caught:
                record_store.for_repo(LOCAL, self.config)
            self.assertIn(caught.exception.kind, ("unbound", "moved"))
            values = acts.ACTS["folder"].parse(mock.Mock(values=[], path=None), acts.Context())
            self.assertEqual(LOCAL_ID, values["id"])
            self.yes("folder", values)
            with mock.patch.object(acts, "allowed", return_value=True):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(0, setup.main(["record-folder", "--yes", ]))
            self.assertEqual(LOCAL_ID, record_store.for_repo(LOCAL, self.config).record_id)
        self.assertEqual(LOCAL_ID, self.store.record_id)   # the first checkout still reads its own
        self.assertEqual(2, len([row for row in storage.record_bindings() if row["id"] == LOCAL_ID]))


class AdoptTests(LocalActsBox):
    """9.5 `adopt`: what the record does not vouch for, taken back on the user's yes, bound to the bytes."""

    def take(self, *words, answered=True):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(acts, "allowed", return_value=answered):
            code = setup.main(["adopt"] + list(words) + ["--yes"])
        return code, out.getvalue()

    def change(self, key="reader"):
        path = self.path_of(key)
        self.write(path, self.read(path).replace(b"strict quoting", b"lenient quoting", 1))
        return path

    def foreign_file(self, path):
        relpath = os.path.relpath(path, self.folder_path).replace(os.sep, "/")
        parsed = local_store.parse_file(self.read(path), relpath, LOCAL_ID)
        data = local_store.render(parsed.kind, parsed.front, parsed.body, relpath, LOCAL_ID, local_store.Sealing(b"another key"))
        self.write(path, data)
        return relpath

    def entry_of(self, key):
        folder = os.path.join(self.folder_path, "topics", os.path.basename(self.path_of(key))[:-3])
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if b"\nkind: entry\n" in self.read(path):
                return path
        raise AssertionError("no entry")

    def test_a_topic_changed_outside_recortex_is_planned_by_counts_and_a_digest(self):
        number = self.numbers["reader"]
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("topic", number)   # nothing was changed yet
        self.change()
        plan = self.store.adopt_plan("topic", number)
        self.assertEqual(("topic", number, 0, 0), (plan["mode"], plan["topic"], plan["entries"], plan["topics"]))
        self.assertGreaterEqual(plan["lines"], 2)
        self.assertRegex(plan["digest"], r"[0-9a-f]{64}")
        self.assertEqual(plan, self.store.adopt_plan("topic", number))   # the same bytes give the same plan

    def test_taking_a_topic_writes_a_version_of_the_text_first_and_then_seals_it(self):
        number = self.numbers["reader"]
        path = self.change()
        before = sorted(os.listdir(os.path.join(self.folder_path, "topics", "reader-quoting")))
        plan = self.store.adopt_plan("topic", number)
        self.assertEqual("changed", self.store.topic(number)[1].state)
        code, printed = self.take("--topic", str(number))
        self.assertEqual(0, code, printed)
        self.assertIn("Sealed 1 file", printed)
        parsed = self.store.topic(number)[1]
        self.assertEqual("sealed", parsed.state)
        self.assertIn("lenient quoting", parsed.body)
        after = sorted(os.listdir(os.path.join(self.folder_path, "topics", "reader-quoting")))
        self.assertEqual(len(before) + 1, len(after))
        versions = self.store.versions(number)
        self.assertEqual("sealed", versions[0][1])
        self.assertIn("lenient quoting", versions[0][2])   # the newest version is the text that was taken
        self.assertTrue(any("strict quoting" in text for created, state, text in versions))   # the earlier ones stay
        self.assertEqual("sealed", local_store.parse_file(self.read(path), "topics/reader-quoting.md", LOCAL_ID).state)

    def test_a_file_that_changed_after_the_question_refuses_the_act_and_changes_nothing(self):
        number = self.numbers["reader"]
        path = self.change()
        plan = self.store.adopt_plan("topic", number)
        self.write(path, self.read(path).replace(b"lenient quoting", b"no quoting", 1))
        before = self.read(path)
        with self.assertRaises(record_store.StoreError) as caught:
            self.store.adopt("topic", number, plan["digest"])
        self.assertIn("changed since the question was asked", str(caught.exception))
        self.assertEqual(before, self.read(path))
        self.assertEqual("changed", self.store.topic(number)[1].state)

    def test_a_topic_that_is_not_marked_as_changed_cannot_be_adopted(self):
        with self.assertRaises(record_store.StoreError) as caught:
            self.store.adopt_plan("topic", self.numbers["speed"])
        self.assertIn("not marked as changed", str(caught.exception))
        with self.assertRaises(record_store.Missing):
            self.store.adopt_plan("topic", 987654)

    def test_foreign_files_are_counted_and_sealed_by_rewriting_only_their_seal_line(self):
        entry = self.entry_of("reader")
        self.foreign_file(entry)
        self.foreign_file(self.path_of("speed"))
        plan = self.store.adopt_plan("foreign")
        self.assertEqual((1, 1), (plan["entries"], plan["topics"]))
        entry_before, topic_before = self.read(entry), self.read(self.path_of("speed"))
        code, printed = self.take("--foreign")
        self.assertEqual(0, code, printed)
        self.assertIn("Sealed 2 files", printed)
        for path, before in ((entry, entry_before), (self.path_of("speed"), topic_before)):
            after = self.read(path)
            self.assertEqual(before.rsplit(b"\n<!-- recortex:seal", 1)[0], after.rsplit(b"\n<!-- recortex:seal", 1)[0])
            self.assertNotEqual(before, after)
            self.assertEqual("sealed", local_store.parse_file(after, os.path.relpath(path, self.folder_path).replace(os.sep, "/"),
                                                              LOCAL_ID).state)
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("foreign")   # nothing is left to take

    def test_a_changed_entry_is_never_adopted_whatever_else_is(self):
        changed = self.entry_of("reader")
        self.write(changed, self.read(changed).replace(b"3 of 3", b"2 of 3", 1))
        other = self.entry_of("speed")
        self.foreign_file(other)
        bytes_before = self.read(changed)
        code, printed = self.take("--foreign")
        self.assertEqual(0, code, printed)
        self.assertEqual(bytes_before, self.read(changed))
        relpath = os.path.relpath(changed, self.folder_path).replace(os.sep, "/")
        self.assertEqual("changed", local_store.parse_file(self.read(changed), relpath, LOCAL_ID).state)
        self.assertFalse(hasattr(self.store, "adopt_entry"))
        for name in ("edit_entry", "edit_comment", "delete_entry", "remove_entry"):
            self.assertFalse(hasattr(local_store.LocalStore, name), name)

    def test_a_foreign_file_changed_after_the_question_refuses_the_act(self):
        entry = self.entry_of("reader")
        self.foreign_file(entry)
        plan = self.store.adopt_plan("foreign")
        self.write(entry, self.read(entry) + b"\n")
        with self.assertRaises(record_store.StoreError):
            self.store.adopt("foreign", 0, plan["digest"])

    def test_a_foreign_record_json_is_sealed_with_the_files_and_not_counted(self):
        path = os.path.join(self.folder_path, "record.json")
        record = json.loads(self.read(path))
        plain = {k: v for k, v in record.items() if k != "seal"}
        other = local_store.Sealing(b"another key")
        record["seal"] = {"key": other.key_id, "mac": other.json_mac(plain)}
        self.write(path, (json.dumps(record, sort_keys=True, indent=1) + "\n").encode())
        self.assertEqual("foreign", self.store.read_record()["state"])
        plan = self.store.adopt_plan("foreign")
        self.assertEqual((0, 0), (plan["entries"], plan["topics"]))
        code, printed = self.take("--foreign")
        self.assertEqual(0, code, printed)
        self.assertEqual("sealed", self.store.record["state"])

    def test_the_command_names_one_thing_and_needs_the_answer(self):
        for argv in (["adopt", "--yes"], ["adopt", "--topic", "2", "--foreign", "--yes"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                setup.main(argv)
        with self.assertRaises(SystemExit) as caught:
            setup.main(["adopt", "--foreign"])
        self.assertIn("--yes", str(caught.exception))
        self.foreign_file(self.entry_of("reader"))
        code, printed = self.take("--foreign", answered=False)
        self.assertEqual(1, code)
        self.assertIn("acts.py ask adopt", printed)
        self.assertEqual("foreign", local_store.parse_file(self.read(self.entry_of("reader")),
                                                           os.path.relpath(self.entry_of("reader"), self.folder_path).replace(
                                                               os.sep, "/"), LOCAL_ID).state)

    def test_the_grant_is_the_users_answer_to_exactly_this_question_and_these_bytes(self):
        number = self.numbers["reader"]
        path = self.change()
        values = self.store.adopt_plan("topic", number)
        command = self.command_for("adopt", "--topic", str(number), "--yes")
        found = acts.asks(command, self.project)
        self.assertEqual(["adopt"], [item["kind"] for item in found])
        self.assertEqual("Take topic #%d as mine" % number, found[0]["label"])
        self.yes("adopt", values)
        self.assertIsNone(self.hook(command))
        self.assertTrue(self.said("adopt", values))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, setup.main(["adopt", "--topic", str(number), "--yes"]))
        self.assertEqual("sealed", self.store.topic(number)[1].state)
        self.assertFalse(self.said("adopt", values))   # spent
        # an answer to the question about the text as it was does not cover a text that changed since
        second = self.numbers["speed"]
        speed = self.path_of("speed")
        self.write(speed, self.read(speed).replace(b"standard library", b"standard lib", 1))
        asked = self.store.adopt_plan("topic", second)
        self.yes("adopt", asked)
        self.write(speed, self.read(speed).replace(b"standard lib", b"stdlib", 1))
        self.hook(self.command_for("adopt", "--topic", str(second), "--yes"))
        self.assertFalse(self.said("adopt", asked))

    def test_a_subagents_call_records_no_adopt_grant(self):
        number = self.numbers["reader"]
        self.change()
        values = self.store.adopt_plan("topic", number)
        self.yes("adopt", values)
        reason = self.hook(self.command_for("adopt", "--topic", str(number), "--yes"), agent_id="a1", agent_type="general-purpose")
        self.assertEqual(base.pretool_guard.SUBAGENT_DENIAL, reason)
        self.assertFalse(self.said("adopt", values))
        for words in (("adopt", "--foreign", "--yes"), ("record-folder", "--yes")):
            self.assertEqual(base.pretool_guard.SUBAGENT_DENIAL, self.hook(self.command_for(*words), agent_id="a1"), words)
        for words in (("detect", "--json"), ("record-question",), ("preflight",)):   # what only reads is not an act
            self.assertIsNone(self.hook(self.command_for(*words), agent_id="a1"), words)


if __name__ == "__main__":
    unittest.main()

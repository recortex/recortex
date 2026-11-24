"""The three acts a local record adds (1.9.0, spec 9.3 and 9.5): `inproject` (a record inside the project, asked before the
project is set up), `folder` (this machine has not approved the folder a record is at) and `adopt` (take back as the user's own
what was changed outside recortex, or written under another key). Each is a question printed by `acts.py`, answered in the
dialog, and taken once from a grant bound to exactly its values. Offline; the failing `gh` of the sandbox must stay unused."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
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
        self.config = {"repo": LOCAL, "hub": 1, "store": "local", "place": "own" if self.place == "chosen" else self.place,
                       "profiles": ["software"]}
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
        if self.place == "chosen":
            parent = os.path.join(self.base, "chosen-parent")
            os.mkdir(parent, 0o755)
            path = storage.check_record_folder(os.path.join(parent, "rec"), "chosen", create=True)
            local_store.LocalStore.create(path, LOCAL_ID, "sample project", ["software"])
            storage.bind_record(LOCAL_ID, path, "chosen")
            return path
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

    def sync(self):
        """index.py sync in the project, in this environment: the marks of the files as they are now."""
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, "index.py"), "sync"], capture_output=True, text=True,
                              cwd=self.project, input="")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)


class ActTableTests(LocalActsBox):
    def test_the_three_acts_are_in_the_table_and_in_the_grant_kinds(self):
        for kind in ("inproject", "folder", "adopt"):
            self.assertIn(kind, acts.ACTS)
            self.assertIn(kind, observer_grants.ACT_KINDS)
        grant_rows = sorted(kind for kind, act in acts.ACTS.items() if act.grant)
        self.assertEqual(grant_rows, sorted(observer_grants.ACT_KINDS))

    def test_every_option_description_fits_the_fixed_form_and_the_question_is_printed(self):
        self.damage_topic("reader")
        for state in ("public", "private", "unknown"):
            for argv in (("inproject",), ("inproject", "--path", "/tmp/answers.json"), ("folder",),
                         ("adopt", "topic", str(self.numbers["reader"]))):
                with self.subTest(state=state, argv=argv), mock.patch.object(record_store, "project_visibility", return_value=state):
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
                         acts.ACTS["inproject"].text(values))
        self.assertEqual(["Keep it inside the project", "Use its own folder instead"], acts.ACTS["inproject"].labels(values))
        folder = {"id": LOCAL_ID, "path": "0" * 64}
        self.assertEqual("recortex: this project's local record is now at a folder this machine has not approved for it (record %s). "
                         "Use the record at its new place?" % LOCAL_ID, acts.ACTS["folder"].text(folder))
        self.assertEqual(["Use the record there", "Leave it unused"], acts.ACTS["folder"].labels(folder))
        topic = {"mode": "topic", "topic": 3, "lines": 4, "entries": 0, "topics": 0, "labels": 2, "state": "open", "digest": "0" * 64}
        self.assertEqual("recortex: topic #3 of the local record was changed outside recortex (4 lines of its text differ from the "
                         "last version recortex wrote; it has 2 labels and is open). Take the text, the title and the labels as they "
                         "are now as yours?", acts.ACTS["adopt"].text(topic))
        token = acts.ACTS["adopt"].value(topic)[:16]
        self.assertEqual(["Take topic #3 as mine " + token, "Leave it as it is"], acts.ACTS["adopt"].labels(topic))
        foreign = {"mode": "foreign", "topic": 0, "lines": 0, "entries": 2, "topics": 1, "versions": 3, "record": 0, "digest": "0" * 64}
        self.assertEqual("recortex: 2 entries, 1 topics and 3 earlier versions of a text of this record carry a seal this machine "
                         "cannot check (they were written on another machine, or here before the key changed). Seal them as yours on "
                         "this machine?", acts.ACTS["adopt"].text(foreign))
        self.assertIn("check, and so does the record's own file (they were", acts.ACTS["adopt"].text(dict(foreign, record=1)))
        token = acts.ACTS["adopt"].value(foreign)[:16]
        self.assertEqual(["Seal them as mine " + token, "Leave them as they are"], acts.ACTS["adopt"].labels(foreign))

    def test_no_question_prints_a_path_or_a_text_of_the_record(self):
        self.damage_topic("reader")
        for argv in (("folder",), ("adopt", "topic", str(self.numbers["reader"]))):
            code, printed = self.ask_main(*argv)
            self.assertEqual(0, code)
            for secret in (self.folder_path, self.base, "lenient quoting", "strict quoting"):
                self.assertNotIn(secret, printed)

    def test_the_question_of_a_record_inside_the_project_needs_no_project_file_and_the_others_do(self):
        os.remove(os.path.join(self.project, ".recortex.json"))
        self.assertEqual(0, self.ask_main("inproject")[0])
        for argv in (("folder",), ("adopt", "foreign")):
            code, printed = self.ask_main(*argv)
            self.assertEqual((1, acts.NOT_SET_UP + "\n"), (code, printed))

    def test_the_value_of_an_in_project_answer_is_the_project_folder_alone(self):
        one = {"project": "/srv/one", "state": "public", "answers": None}
        two = {"project": "/srv/two", "state": "public", "answers": None}
        again = {"project": "/srv/one", "state": "private", "answers": "/srv/one/a.json"}
        act = acts.ACTS["inproject"]
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


class TrustAboveTests(LocalActsBox):
    """8.2: a folder above the record that another account could replace is trusted through the question of `trust`, as exactly as it
    is, and not through a question of its own."""

    place = "chosen"

    def setUp(self):
        super().setUp()
        self.parent = os.path.dirname(self.folder_path)
        os.chmod(self.parent, 0o777)

    def refused_kind(self):
        with self.assertRaises(record_store.StoreError) as caught:
            self.store
        return caught.exception.kind

    def command_trust(self, *words):
        return self.command("observer.py", "trust-ancestor", "--record", *words)

    def test_the_record_is_refused_and_the_question_is_the_one_of_trust_with_the_folder_above_it(self):
        self.assertEqual("ancestor-permissions", self.refused_kind())
        code, printed = self.ask_main("trust", "--scope", "record", "--json")
        self.assertEqual(0, code, printed)
        body = json.loads(printed)
        self.assertEqual("trust", body["id"])
        self.assertIn(self.parent, body["question"])
        yes = [item for item in body["options"] if item["answer"] == "yes"][0]
        self.assertIn("observer.py trust-ancestor --record --failing --yes", yes["run"][0])
        code, printed = self.ask_main("trust", "--scope", "elsewhere")
        self.assertEqual(1, code)

    def test_without_a_record_or_for_a_folder_that_is_not_above_it_nothing_is_asked(self):
        code, printed = self.ask_main("trust", "--scope", "record", "/")
        self.assertEqual(1, code)
        code, printed = self.ask_main("trust", "--scope", "record", self.folder_path)   # the record folder itself is no ancestor
        self.assertEqual(1, code)
        with open(os.path.join(self.project, ".recortex.json"), "w") as handle:
            json.dump({"repo": base.REPO, "hub": 1}, handle)
        code, printed = self.ask_main("trust", "--scope", "record")
        self.assertEqual(1, code)

    def test_a_yes_trusts_the_folder_as_it_is_and_a_change_of_its_mode_refuses_the_record_again(self):
        values = acts.ACTS["trust"].parse(mock.Mock(values=[], scope="record", path=None), acts.Context())
        self.assertEqual([self.parent], [row["path"] for row in values["rows"]])
        self.yes("trust", values)
        command = self.command_trust("--failing", "--yes")
        self.assertIsNone(self.hook(command))
        self.assertTrue(self.said("trust", values))
        code, result = self.run_observer("trust-ancestor", "--record", "--failing", "--yes")
        self.assertEqual(0, code, result)
        self.assertTrue(result["approved"])
        self.assertEqual(LOCAL_ID, self.store.record_id)   # the record opens now
        os.chmod(self.parent, 0o757)   # the folder is no longer as it was trusted, and others may still write it
        self.assertEqual("ancestor-permissions", self.refused_kind())

    def test_without_the_users_answer_nothing_is_trusted(self):
        code, result = self.run_observer("trust-ancestor", "--record", "--failing", "--yes")
        self.assertEqual(1, code)
        self.assertIn("trust_unanswered", result["code"])
        self.assertEqual("ancestor-permissions", self.refused_kind())
        code, result = self.run_observer("trust-ancestor", "--record", "--failing")   # without --yes it only shows
        self.assertEqual(0, code)
        self.assertFalse(result["approved"])

    def test_a_subagents_call_records_no_grant(self):
        values = acts.ACTS["trust"].parse(mock.Mock(values=[], scope="record", path=None), acts.Context())
        self.yes("trust", values)
        self.assertEqual(base.pretool_guard.SUBAGENT_DENIAL, self.hook(self.command_trust("--failing", "--yes"), agent_id="a1"))
        self.assertFalse(self.said("trust", values))

    def record_values(self):
        return acts.ACTS["trust"].parse(mock.Mock(values=[], scope="record", path=None), acts.Context())

    def transcript_values(self):
        return dict(self.record_values(), record=False)

    def test_the_record_scope_has_its_own_words_labels_and_descriptions(self):
        record, transcripts = self.record_values(), self.transcript_values()
        act = acts.ACTS["trust"]
        self.assertTrue(act.text(record).startswith("Trust these folders above this project's local record, so that recortex can read and "
                                                    "write the record on this machine? "))
        self.assertIn("Another account that can write to one of them could replace the record's folder.", act.text(record))
        self.assertNotIn("transcripts", act.text(record))
        self.assertIn("Claude Code's transcripts", act.text(transcripts))   # the 1.7.0 and 1.8.0 question, word for word
        self.assertTrue(act.labels(record)[0].startswith("Trust the record's folders "))
        self.assertTrue(act.labels(transcripts)[0].startswith("Trust these folders "))
        self.assertNotEqual(act.options(record)[0]["description"], act.options(transcripts)[0]["description"])
        for item in act.options(record):
            self.assertRegex(item["description"], acts.DESCRIPTION)
        self.assertIn("record", act.text(record).lower())

    def test_a_grant_for_the_transcripts_does_not_pass_for_the_record_and_the_other_way_round(self):
        record, transcripts = self.record_values(), self.transcript_values()
        act = acts.ACTS["trust"]
        self.assertNotEqual(act.value(record), act.value(transcripts))
        self.yes("trust", transcripts)
        self.hook(self.command("observer.py", "trust-ancestor", "--failing", "--yes"))   # the transcripts' own command
        self.hook(self.command_trust("--failing", "--yes"))
        self.assertFalse(self.said("trust", record))   # the answer to the other question is no answer to this one
        self.assertFalse(acts.allowed("trust", record))
        self.yes("trust", record)
        self.hook(self.command_trust("--failing", "--yes"))
        self.assertTrue(self.said("trust", record))
        self.assertFalse(acts.allowed("trust", transcripts))
        self.assertTrue(acts.allowed("trust", record))

    def test_a_trusted_folder_is_for_its_scope_alone(self):
        import observer_sources
        rows = [{"path": self.parent}]
        observer_sources.trust_add(rows, self.project, self.folder_path, "record")
        self.assertEqual(LOCAL_ID, self.store.record_id)   # trusted for the record
        info = os.lstat(self.parent)
        record, transcripts = observer_sources.Trust("record"), observer_sources.Trust("transcripts")
        self.assertTrue(record.accepts(self.parent, info, self.folder_path))
        self.assertFalse(transcripts.accepts(self.parent, info, self.folder_path))
        entry = observer_sources.read_trusted(scope="record")[1][self.parent]
        self.assertEqual("record", entry["scope"])
        self.assertEqual({}, observer_sources.read_trusted(scope="transcripts")[1])   # the transcripts' file holds nothing of it
        # an entry carried over from one file to the other trusts nothing: its HMAC holds the scope
        self.assertNotEqual(entry["mac"], observer_sources._mac(self.parent, entry["uid"], entry["gid"], entry["mode"], "transcripts"))
        self.assertEqual(entry["mac"], observer_sources._mac(self.parent, entry["uid"], entry["gid"], entry["mode"], "record"))
        mutated = dict(entry, scope="transcripts")
        self.assertFalse(observer_sources._valid_entry(self.parent, mutated, "record"))
        self.assertFalse(observer_sources._valid_entry(self.parent, dict(entry, scope="record"), "transcripts"))

    def test_the_transcripts_value_and_mac_are_what_they_were(self):
        import observer_sources
        rows = [{"path": "/srv/a", "uid": 1, "gid": 2, "mode": "0755", "world": False}]
        self.assertEqual(acts.digest("trust", [["/srv/a", 1, 2, "0755"]]), acts.ACTS["trust"].value({"rows": rows, "path": ""}))
        self.assertEqual(acts.digest("trust", "record", [["/srv/a", 1, 2, "0755"]]),
                         acts.ACTS["trust"].value({"rows": rows, "path": "", "record": True}))
        # the transcripts' MAC is computed from the four fields it always was computed from
        import hmac, hashlib, json
        key = observer_sources.storage.user_key(create=True)
        expected = hmac.new(key, json.dumps(["ancestor", "/srv/a", 1, 2, 493], sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=False).encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        self.assertEqual(expected, observer_sources._mac("/srv/a", 1, 2, 493))


class TargetOwn(LocalActsBox):
    """8.2: the folder whose ancestors are trusted is the folder the store opens: one resolver, record_store.local_folder_target."""

    def target(self):
        return acts.record_folder(self.project)

    def test_the_trust_target_and_the_opened_folder_are_the_same(self):
        self.assertEqual(os.path.realpath(self.target()), os.path.realpath(self.store.folder))
        self.assertEqual((self.target(), self.place if self.place != "chosen" else "chosen"),
                         (record_store.local_folder_target(LOCAL, self.config)[0], record_store.local_folder_target(LOCAL, self.config)[2]))


class TargetChosen(TargetOwn):
    place = "chosen"

    def test_a_row_of_another_place_for_the_same_id_does_not_move_the_target(self):
        other = os.path.join(self.base, "elsewhere")
        os.mkdir(other, 0o700)
        storage.bind_record(LOCAL_ID, other, "project")   # a row of a record inside some project: not for `own`
        self.assertEqual(self.folder_path, self.target())
        self.assertEqual(os.path.realpath(self.folder_path), os.path.realpath(self.store.folder))
        newer = os.path.join(self.base, "chosen-parent", "rec-two")
        local_store.LocalStore.create(storage.check_record_folder(newer, "chosen", create=True), LOCAL_ID, "sample project", ["software"])
        storage.bind_record(LOCAL_ID, newer, "chosen")   # the last row of `own` or `chosen` is the one both use
        self.assertEqual(newer, self.target())
        self.assertEqual(os.path.realpath(newer), os.path.realpath(self.store.folder))


class TargetProject(TargetOwn):
    place = "project"

    def test_a_copied_project_has_its_own_target_and_is_not_opened_there(self):
        moved = os.path.join(self.base, "moved-project")
        shutil.copytree(self.project, moved)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": moved}):
            self.assertEqual(os.path.join(os.path.realpath(moved), ".recortex"), acts.record_folder(moved))   # not the first checkout's
            with self.assertRaises(record_store.StoreError) as caught:
                record_store.for_repo(LOCAL, self.config)
            self.assertIn(caught.exception.kind, ("moved", "unbound"))
        self.assertEqual(os.path.realpath(self.target()), os.path.realpath(self.store.folder))


class TargetUnknownPlace(LocalActsBox):
    def test_a_place_this_version_does_not_know_has_no_target_and_no_store(self):
        config = dict(self.config, place="elsewhere")
        self.assertIsNone(record_store.local_folder_target(LOCAL, config))
        with self.assertRaises(record_store.StoreError) as caught:
            record_store.for_repo(LOCAL, config)
        self.assertEqual("place", caught.exception.kind)
        with open(os.path.join(self.project, ".recortex.json"), "w") as handle:
            json.dump(config, handle)
        self.assertIsNone(acts.record_folder(self.project))


class ParseTests(unittest.TestCase):
    """`acts.py ask` takes its words and its options in any order, on every Python the plugin supports: argparse before 3.12 stops reading
    positionals at the first option, so a form like `ask trust --scope record /folder` failed with an exit code 2 on 3.8."""

    def test_the_words_and_the_options_of_ask_may_come_in_any_order(self):
        for argv, expected in ((["ask", "trust", "--scope", "record", "/srv/folder"], ("trust", ["/srv/folder"], "record")),
                               (["ask", "trust", "/srv/folder", "--scope", "record"], ("trust", ["/srv/folder"], "record")),
                               (["ask", "--scope", "record", "trust", "/srv/folder"], ("trust", ["/srv/folder"], "record")),
                               (["ask", "adopt", "topic", "3", "--json"], ("adopt", ["topic", "3"], None)),
                               (["ask", "adopt", "--json", "topic", "3"], ("adopt", ["topic", "3"], None)),
                               (["ask", "inproject", "--path", "/p/answers.json"], ("inproject", [], None)),
                               (["ask", "trust", "--typed"], ("trust", [], None))):
            args = acts.parse(argv)
            self.assertEqual(expected, (args.kind, args.values, args.scope), argv)
            self.assertEqual("ask", args.command)
        self.assertEqual("/p/answers.json", acts.parse(["ask", "inproject", "--path", "/p/answers.json"]).path)
        self.assertEqual("list", acts.parse(["list"]).command)

    def test_every_form_of_ask_that_a_document_or_a_printed_question_writes_parses(self):
        import shlex
        forms = []
        for folder in (os.path.join(base.ROOT, "skills", "recortex"), os.path.join(base.ROOT, "commands"), os.path.join(base.ROOT, "docs"),
                       os.path.join(base.ROOT, "agents")):
            for place, dirs, files in os.walk(folder):
                for name in files:
                    if name.endswith(".md"):
                        with open(os.path.join(place, name), encoding="utf-8") as handle:
                            forms += re.findall(r"acts\.py ask ([A-Za-z0-9 .<>_/:=-]+?)(?=[`.,;)]|$)", handle.read(), re.M)
        self.assertGreater(len(forms), 5)
        for form in forms:
            words = shlex.split(form.replace("<", "").replace(">", ""))
            acts.parse(["ask"] + words)   # an exit code 2 here is a SystemExit

    def test_the_command_lines_that_the_questions_print_parse(self):
        for kind, values in (("trust", {"rows": [{"path": "/srv/a", "uid": 1, "gid": 2, "mode": "0755", "world": False}], "path": "/srv/a",
                                        "record": True}),
                             ("inproject", {"project": "/p", "state": "unknown", "answers": "/p/a.json"}),
                             ("adopt", {"mode": "topic", "topic": 3, "lines": 1, "entries": 0, "topics": 0, "labels": 1, "state": "open",
                                        "digest": "0" * 64})):
            words = ["ask", kind] + acts.ACTS[kind].ask_words(values)
            self.assertEqual(kind, acts.parse(words).kind, words)


class InProjectStateTests(LocalActsBox):
    """9.3: what the question says of the project's repository is computed by acts.py from the remotes and GitHub, never taken from
    an argument, and read by an exact check of each remote's host."""

    PRIVATE_URL = "https://github.com/octo-org/octo-repo.git"
    PUBLIC_URL = "git@github.com:octo-org/octo-pages.git"

    def setUp(self):
        super().setUp()
        subprocess.run(["git", "-C", self.project, "init", "-q", "."], check=True)
        self.asked = []
        self.real_visibility = record_store.GithubStore.visibility
        self.visibility = {"octo-org/octo-repo": "true", "octo-org/octo-pages": "false"}

        def visibility(store, timeout=30, host=False):
            self.assertTrue(host, "the check of a remote names github.com")
            self.asked.append(store.repo)
            return self.visibility.get(store.repo, "")
        for patch in (mock.patch.object(record_store.GithubStore, "visibility", visibility),
                      mock.patch.object(record_store.recortex, "gh_account", return_value=("ready", "octocat"))):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, *args):
        subprocess.run(["git", "-C", self.project] + list(args), check=True, capture_output=True)

    def remote(self, name, url, push=False):
        self.git("remote", "add", name, url)
        if push:
            self.git("config", "remote.%s.pushurl" % name, push)

    def state(self):
        return record_store.project_visibility(self.project)

    def test_a_state_given_as_an_argument_is_refused_whatever_it_says(self):
        self.remote("origin", self.PUBLIC_URL)
        for word in ("private", "public", "unknown", "anything"):
            code, printed = self.ask_main("inproject", word)
            self.assertEqual(1, code, word)
            self.assertIn("takes no state", printed)
            self.assertNotIn("question inproject", printed)
        code, printed = self.ask_main("inproject", "--json")
        body = json.loads(printed)
        self.assertIn("The project's repository is public.", body["options"][0]["description"])

    def test_a_remote_that_only_holds_the_word_github_is_not_github(self):
        for url in ("https://evil.example/github.com/octo-org/octo-repo", "https://github.com.evil.example/octo-org/octo-repo",
                    "https://evilgithub.com/octo-org/octo-repo", "https://github.com@evil.example/octo-org/octo-repo",
                    "git@github.com.evil.example:octo-org/octo-repo", "https://github.com/octo-org/octo-repo?x=github.com",
                    "https://github.com/octo-org/octo-repo/extra", "file:///srv/github.com/octo-org/octo-repo"):
            with self.subTest(url=url):
                self.assertIsNone(record_store.github_repo_of(url))
        self.remote("origin", "https://evil.example/github.com/octo-org/octo-repo")
        self.assertEqual("unknown", self.state())
        self.assertEqual([], self.asked)   # GitHub is not asked about a repository that no remote names
        code, printed = self.ask_main("inproject", "--json")
        self.assertIn("could not tell", json.loads(printed)["options"][0]["description"])

    def test_the_forms_of_a_github_remote_are_read_exactly(self):
        for url in ("https://github.com/octo-org/octo-repo", "https://github.com/octo-org/octo-repo.git", "http://github.com/octo-org/octo-repo/",
                    "git@github.com:octo-org/octo-repo.git", "github.com:octo-org/octo-repo", "ssh://git@github.com/octo-org/octo-repo.git",
                    "ssh://git@github.com:22/octo-org/octo-repo", "git://github.com/octo-org/octo-repo.git",
                    "https://octocat:token@github.com/octo-org/octo-repo"):
            with self.subTest(url=url):
                self.assertEqual("octo-org/octo-repo", record_store.github_repo_of(url))

    def test_one_public_remote_makes_it_public_whatever_the_others_are(self):
        self.remote("origin", self.PRIVATE_URL)
        self.remote("upstream", self.PUBLIC_URL)
        self.assertEqual("public", self.state())
        self.git("remote", "add", "elsewhere", "https://git.example/team/thing.git")
        self.assertEqual("public", self.state())

    def test_private_needs_every_remote_to_be_a_github_repository_confirmed_private(self):
        self.remote("origin", self.PRIVATE_URL)
        self.assertEqual("private", self.state())
        self.remote("other", "https://github.com/octo-org/octo-docs.git")
        self.visibility["octo-org/octo-docs"] = "true"
        self.assertEqual("private", self.state())
        self.visibility["octo-org/octo-docs"] = ""   # GitHub did not answer for one of them
        self.assertEqual("unknown", self.state())
        self.visibility["octo-org/octo-docs"] = "true"
        self.git("remote", "add", "mirror", "https://git.example/team/thing.git")   # one that is not GitHub's
        self.assertEqual("unknown", self.state())

    def test_a_push_url_counts_as_a_remote(self):
        self.remote("origin", self.PRIVATE_URL, push=self.PUBLIC_URL)
        self.assertEqual("public", self.state())
        self.git("config", "remote.origin.pushurl", "https://evil.example/github.com/octo-org/octo-repo")
        self.assertEqual("unknown", self.state())   # a private url with a push url elsewhere is not known to be private

    def test_the_host_is_pinned_so_that_another_server_cannot_answer_for_github(self):
        self.remote("origin", self.PRIVATE_URL)
        calls = []

        def gh(args, **kwargs):
            calls.append(list(args))
            return "true\n"
        with mock.patch.dict(os.environ, {"GH_HOST": "ghe.example"}), mock.patch.object(record_store.GithubStore, "visibility",
                                                                                       self.real_visibility), \
                mock.patch.object(record_store.recortex, "gh", gh):
            self.assertEqual("private", self.state())
            self.assertEqual([["api", "repos/octo-org/octo-repo", "--hostname", "github.com", "--jq", ".private"]], calls)
            calls.clear()
            record_store.GithubStore("octo-org/octo-repo").visibility()   # the other call sites keep today's argument list
            self.assertEqual([["api", "repos/octo-org/octo-repo", "--jq", ".private"]], calls)

    def test_without_a_ready_gh_or_a_remote_nothing_is_asked_and_it_is_unknown(self):
        self.assertEqual("unknown", self.state())
        self.remote("origin", self.PRIVATE_URL)
        with mock.patch.object(record_store.recortex, "gh_account", return_value=("signed-out", None)):
            self.assertEqual("unknown", self.state())
        self.assertEqual([], self.asked)

    def test_only_a_few_repositories_are_asked_about_and_the_rest_is_unknown(self):
        for number in range(record_store.REMOTE_LIMIT + 2):
            self.remote("r%d" % number, "https://github.com/octo-org/octo-p%d.git" % number)
            self.visibility["octo-org/octo-p%d" % number] = "true"
        self.assertEqual("unknown", self.state())
        self.assertEqual(record_store.REMOTE_LIMIT, len(self.asked))

    def test_the_question_of_setup_and_of_acts_use_the_same_function(self):
        with mock.patch.object(record_store, "project_visibility", return_value="private") as one:
            code, printed = self.ask_main("inproject", "--json")
            self.assertEqual(0, code)
            self.assertIn("The project's repository is private now.", printed)
            self.assertEqual(os.path.realpath(self.project), os.path.realpath(one.call_args[0][0]))
            self.assertEqual("private", setup.in_project_state(self.project))
        self.assertEqual(2, one.call_count)


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
        self.assertEqual(["inproject"], [item["kind"] for item in found])
        self.assertEqual("Keep it inside the project", found[0]["label"])
        self.assertEqual(acts.ACTS["inproject"].value(self.values), found[0]["value"])

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
        self.assertFalse(acts.allowed("inproject", self.values))
        note = acts.refusal("inproject", self.values)["note"]
        self.assertIn("acts.py ask inproject", note)

    def test_the_answer_yes_is_taken_once(self):
        self.yes("inproject", self.values)
        self.assertIsNone(self.hook(self.apply_command(self.project)))
        self.assertTrue(self.said("inproject", self.values))
        self.assertTrue(acts.allowed("inproject", self.values))
        self.assertFalse(acts.allowed("inproject", self.values))   # one time only

    def test_an_answer_about_this_project_covers_no_other_folder(self):
        other = dict(self.values, project=os.path.realpath(self.other))
        self.assertEqual([], acts.asks(self.apply_command(self.other), self.project))   # another --root: no grant is asked for
        self.yes("inproject", self.values)
        self.assertIsNone(self.hook(self.apply_command(self.other)))
        self.assertFalse(self.said("inproject", self.values))
        self.assertFalse(self.said("inproject", other))
        self.assertFalse(acts.allowed("inproject", other))
        self.assertIsNone(self.hook(self.apply_command(self.project)))
        self.assertTrue(self.said("inproject", self.values))
        self.assertFalse(acts.allowed("inproject", other))   # the grant of this project is not the other's

    def test_the_other_answer_and_a_reworded_question_grant_nothing(self):
        act = acts.ACTS["inproject"]
        self.answer("inproject", self.values, "Use its own folder instead")
        self.hook(self.apply_command(self.project))
        self.assertFalse(self.said("inproject", self.values))
        self.answer("inproject", self.values, act.label(self.values), text=act.text(self.values).replace("anyone", "someone"))
        self.hook(self.apply_command(self.project))
        self.assertFalse(self.said("inproject", self.values))
        self.answer("inproject", self.values, act.label(self.values), labels=["Keep it inside the project", "Use its own "
                                                                                    "folder instead", "Ask me later"])
        self.hook(self.apply_command(self.project))
        self.assertFalse(self.said("inproject", self.values))

    def test_a_subagents_call_records_no_grant(self):
        self.yes("inproject", self.values)
        self.hook(self.apply_command(self.project), agent_id="agent-1", agent_type="general-purpose")
        self.assertFalse(self.said("inproject", self.values))   # the main session's call alone records one
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
        self.yes("inproject", self.values)
        self.hook(self.apply_command(project))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup.main(argv)
        self.assertEqual(0, code, out.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(project, ".recortex", "record.json")))
        self.assertFalse(self.said("inproject", self.values))   # spent


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

    def foreign_file(self, path, sync=True):
        relpath = os.path.relpath(path, self.folder_path).replace(os.sep, "/")
        parsed = local_store.parse_file(self.read(path), relpath, LOCAL_ID)
        data = local_store.render(parsed.kind, parsed.front, parsed.body, relpath, LOCAL_ID, local_store.Sealing(b"another key"))
        self.write(path, data)
        if sync:
            self.sync()   # what this machine knows of the files: this one was never seen sealed, so it was written elsewhere
        return relpath

    def entries_of(self, key):
        folder = os.path.join(self.folder_path, "topics", os.path.basename(self.path_of(key))[:-3])
        return [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                if b"\nkind: entry\n" in self.read(os.path.join(folder, name))]

    def entry_of(self, key):
        found = self.entries_of(key)
        if not found:
            raise AssertionError("no entry")
        return found[0]

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
        self.foreign_file(entry, sync=False)
        self.foreign_file(self.path_of("speed"), sync=False)
        self.sync()
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
        self.sync()
        plan = self.store.adopt_plan("foreign")
        self.assertEqual((0, 0, 0, 1), (plan["entries"], plan["topics"], plan["versions"], plan["record"]))   # its own file is counted
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
        self.assertEqual("Take topic #%d as mine %s" % (number, acts.ACTS["adopt"].value(values)[:16]), found[0]["label"])
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

    def refused_adopt(self, *words):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return setup.main(["adopt"] + list(words) + ["--yes"])

    def test_a_change_that_keeps_the_counts_gets_no_grant_for_a_topic_and_seals_nothing(self):
        number = self.numbers["reader"]
        path = self.change()
        asked = self.store.adopt_plan("topic", number)
        self.yes("adopt", asked)   # the user's yes to the question about these bytes
        self.write(path, self.read(path).replace(b"lenient quoting", b"planted quoting", 1))   # the same counts, other bytes
        new = self.store.adopt_plan("topic", number)
        self.assertEqual((asked["lines"], asked["labels"], asked["state"]), (new["lines"], new["labels"], new["state"]))
        self.assertNotEqual(asked["digest"], new["digest"])
        self.assertNotEqual(acts.ACTS["adopt"].labels(asked)[0], acts.ACTS["adopt"].labels(new)[0])   # the label is the bytes' own
        self.assertEqual(acts.ACTS["adopt"].text(asked), acts.ACTS["adopt"].text(new))   # the words alone would not tell them apart
        self.assertIsNone(self.hook(self.command_for("adopt", "--topic", str(number), "--yes")))
        self.assertFalse(self.said("adopt", new))
        self.assertFalse(self.said("adopt", asked))
        before = self.read(path)
        self.assertEqual(1, self.refused_adopt("--topic", str(number)))
        self.assertEqual(before, self.read(path))
        self.assertEqual("changed", self.store.topic(number)[1].state)

    def test_a_change_that_keeps_the_counts_gets_no_grant_for_the_foreign_files_either(self):
        entry = self.entry_of("reader")
        self.foreign_file(entry)
        asked = self.store.adopt_plan("foreign")
        self.yes("adopt", asked)
        relpath = os.path.relpath(entry, self.folder_path).replace(os.sep, "/")
        parsed = local_store.parse_file(self.read(entry), relpath, LOCAL_ID)
        other = local_store.render(parsed.kind, parsed.front, parsed.body + "\nplanted\n", relpath, LOCAL_ID,
                                   local_store.Sealing(b"another key again"))
        self.write(entry, other)   # one foreign entry swapped for another: the same counts
        new = self.store.adopt_plan("foreign")
        self.assertEqual((asked["entries"], asked["topics"]), (new["entries"], new["topics"]))
        self.assertNotEqual(asked["digest"], new["digest"])
        self.assertIsNone(self.hook(self.command_for("adopt", "--foreign", "--yes")))
        self.assertFalse(self.said("adopt", new))
        self.assertEqual(1, self.refused_adopt("--foreign"))
        self.assertEqual(other, self.read(entry))

    def test_a_change_of_the_front_matter_shows_in_the_question(self):
        number = self.numbers["reader"]
        path = self.change()
        one = self.store.adopt_plan("topic", number)
        self.write(path, self.read(path).replace(b'labels: ["final-record", "topic:reader"]',
                                                 b'labels: ["final-record", "topic:reader", "topic:extra", "rel:fixed"]', 1))
        two = self.store.adopt_plan("topic", number)
        self.assertEqual((2, 4), (one["labels"], two["labels"]))
        self.assertIn("it has 2 labels and is open", acts.ACTS["adopt"].text(one))
        self.assertIn("it has 4 labels and is open", acts.ACTS["adopt"].text(two))
        self.assertIn("Take the text, the title and the labels as they are now as yours?", acts.ACTS["adopt"].text(two))
        self.write(path, self.read(path).replace(b'title: "Reader quoting"', b'title: "A planted title"', 1))   # a title alone: the label differs
        three = self.store.adopt_plan("topic", number)
        self.assertEqual(acts.ACTS["adopt"].text(two), acts.ACTS["adopt"].text(three))
        self.assertNotEqual(acts.ACTS["adopt"].labels(two)[0], acts.ACTS["adopt"].labels(three)[0])
        self.assertEqual(1, self.store.adopt("topic", number, three["digest"]))
        self.assertEqual("A planted title", self.store.topic(number)[1].front["title"])   # the title was sealed with the text, as asked

    def test_an_entry_this_machine_sealed_and_that_now_carries_another_key_is_not_adoptable(self):
        written_elsewhere = self.entry_of("speed")
        self.foreign_file(written_elsewhere)   # never sealed here as far as the index knows
        self.sync()
        sealed_here = self.entry_of("reader")
        self.foreign_file(sealed_here)   # sealed here at the last sync, rewritten under another key id since
        plan = self.store.adopt_plan("foreign")
        self.assertEqual((1, 0), (plan["entries"], plan["topics"]))   # only the file that was never sealed here
        before = self.read(sealed_here)
        self.assertEqual(1, self.store.adopt("foreign", 0, plan["digest"]))
        self.assertEqual(before, self.read(sealed_here))   # the rewritten one is still as it was
        self.assertEqual("sealed", local_store.parse_file(self.read(written_elsewhere), os.path.relpath(
            written_elsewhere, self.folder_path).replace(os.sep, "/"), LOCAL_ID).state)
        with self.assertRaises(record_store.StoreError) as caught:
            self.store.adopt_plan("foreign")
        self.assertIn("nothing in this record carries a seal that was written on another machine", str(caught.exception))
        with self.assertRaises(SystemExit):   # no plan: the command says so and stops
            self.refused_adopt("--foreign")

    def rewritten_under_another_key(self, path):
        """The seal line of a file replaced by one with another key id: all that anyone who can write the folder needs to do."""
        relpath = os.path.relpath(path, self.folder_path).replace(os.sep, "/")
        parsed = local_store.parse_file(self.read(path), relpath, LOCAL_ID)
        self.write(path, local_store.render(parsed.kind, parsed.front, parsed.body, relpath, LOCAL_ID,
                                            local_store.Sealing(b"a key that is not this machine's")))

    def test_what_this_machine_sealed_stays_remembered_through_later_syncs_a_deletion_and_a_new_name(self):
        entry = self.entry_of("reader")
        self.sync()   # seen sealed
        self.rewritten_under_another_key(entry)
        self.sync()
        self.sync()   # the mark of the path is now foreign: it no longer says that the file was sealed here
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("foreign")
        with self.assertRaises(SystemExit):   # no plan: the command says so and stops
            self.refused_adopt("--foreign")
        # deleted and written again under the same name, and moved to another name with the same entry id
        data = self.read(entry)
        os.remove(entry)
        self.sync()
        with open(entry, "wb") as handle:
            handle.write(data)
        os.chmod(entry, 0o600)
        self.sync()
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("foreign")
        moved = os.path.join(os.path.dirname(entry), "2026-11-24T1000Z-" + os.path.basename(entry).rsplit("-", 1)[1])
        os.rename(entry, moved)
        self.sync()
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("foreign")

    def test_a_topic_this_machine_sealed_and_that_now_carries_another_key_is_not_adoptable_either(self):
        path = self.path_of("speed")
        self.sync()
        self.rewritten_under_another_key(path)
        self.sync()
        self.sync()
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("foreign")
        keys = local_store.LocalStore.seal_keys("topics/read-speed.md", local_store.parse_file(self.read(path), "topics/read-speed.md", LOCAL_ID))
        self.assertEqual({"path:topics/read-speed.md", "number:%d" % self.numbers["speed"]}, keys)

    def test_an_index_that_cannot_be_read_refuses_and_is_never_an_empty_answer(self):
        self.foreign_file(self.entry_of("reader"))   # adoptable: never seen sealed
        self.assertEqual(1, self.store.adopt_plan("foreign")["entries"])
        import index
        for patch in (mock.patch.object(index, "connect", return_value=None),
                      mock.patch.object(index, "connect", side_effect=storage.StorageError("x", "owner"))):
            with patch, self.assertRaises(record_store.StoreError) as caught:
                self.store.adopt_plan("foreign")
            self.assertIn("cannot be read", str(caught.exception))
            with patch, self.assertRaises(record_store.StoreError):
                self.store.adopt("foreign", 0, "0" * 64)
        db = index.connect(LOCAL, create=False)
        db.execute("DROP TABLE local_sealed")
        db.commit()
        db.close()
        with self.assertRaises(record_store.StoreError) as caught:
            self.store.adopt_plan("foreign")
        self.assertIn("cannot be read", str(caught.exception))

    def test_a_rebuild_keeps_what_was_sealed_here_and_only_the_loss_of_the_index_file_forgets_it(self):
        entry = self.entry_of("reader")
        self.sync()
        self.rewritten_under_another_key(entry)
        self.sync()
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("foreign")
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, "index.py"), "sync", "--rebuild"], capture_output=True, text=True,
                              cwd=self.project, input="")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        with self.assertRaises(record_store.StoreError):   # the rebuild keeps the memory of the same record
            self.store.adopt_plan("foreign")
        import index
        os.chmod(index.index_path(LOCAL), 0o600)
        os.rename(index.index_path(LOCAL), index.index_path(LOCAL) + ".lost")   # the index file itself is gone
        self.sync()
        self.assertEqual(1, self.store.adopt_plan("foreign")["entries"])   # the limit: nothing is known of what was sealed before

    def test_an_entry_whose_own_seal_holds_is_remembered_even_when_its_topic_could_not_be_read_at_the_first_sync(self):
        topic = self.path_of("reader")
        good = self.read(topic)
        self.write(topic, b"half of a topic")   # the topic does not read: the entries under it are attributed as changed
        entry = self.entry_of("reader")
        self.sync()   # the entry's own MAC holds, though its folder's topic does not read
        self.rewritten_under_another_key(entry)
        self.write(topic, good)   # the topic reads again
        self.sync()
        with self.assertRaises(record_store.StoreError) as caught:
            self.store.adopt_plan("foreign")
        self.assertIn("nothing in this record carries a seal that was written on another machine", str(caught.exception))

    def test_an_index_whose_last_sync_stopped_or_that_holds_another_folder_refuses(self):
        self.foreign_file(self.entry_of("speed"))   # adoptable, and the index says so
        self.assertEqual(1, self.store.adopt_plan("foreign")["entries"])
        import index
        topic = self.path_of("speed")
        relpath = "topics/zz-copy.md"
        copy = os.path.join(self.folder_path, relpath)
        parsed = local_store.parse_file(self.read(topic), "topics/read-speed.md", LOCAL_ID)
        with open(copy, "wb") as handle:   # two topic files that carry one number: the sync stops before it stores anything
            handle.write(local_store.render(parsed.kind, parsed.front, parsed.body, relpath, LOCAL_ID))
        os.chmod(copy, 0o600)
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, "index.py"), "sync"], capture_output=True, text=True,
                              cwd=self.project, input="")
        self.assertNotEqual(0, done.returncode)
        with self.assertRaises(record_store.StoreError) as caught:
            self.store.adopt_plan("foreign")
        self.assertIn("cannot be read", str(caught.exception))
        os.remove(copy)
        self.sync()
        self.assertEqual(1, self.store.adopt_plan("foreign")["entries"])   # a sync that finished makes it whole again
        db = index.connect(LOCAL, create=False)   # an index of another folder is no memory of this one
        index.set_meta(db, "record_path", "/somewhere/else")
        db.commit()
        db.close()
        with self.assertRaises(record_store.StoreError):
            self.store.adopt_plan("foreign")

    def test_the_plan_counts_earlier_versions_of_a_text_and_the_records_own_file(self):
        number = self.numbers["reader"]
        self.store.edit_body(number, body="A second text.\n")
        folder = os.path.join(self.folder_path, "topics", os.path.basename(self.path_of("reader"))[:-3])
        version = next(os.path.join(folder, name) for name in sorted(os.listdir(folder))
                       if b"\nkind: body\n" in self.read(os.path.join(folder, name)))
        self.foreign_file(version, sync=False)
        path = os.path.join(self.folder_path, "record.json")
        record = json.loads(self.read(path))
        plain = {k: v for k, v in record.items() if k != "seal"}
        other = local_store.Sealing(b"another key")
        record["seal"] = {"key": other.key_id, "mac": other.json_mac(plain)}
        self.write(path, (json.dumps(record, sort_keys=True, indent=1) + "\n").encode())
        self.sync()
        plan = self.store.adopt_plan("foreign")
        self.assertEqual((0, 0, 1, 1), (plan["entries"], plan["topics"], plan["versions"], plan["record"]))
        text = acts.ACTS["adopt"].text(plan)
        self.assertIn("0 entries, 0 topics and 1 earlier versions of a text", text)
        self.assertIn("and so does the record's own file", text)
        self.assertEqual(2, self.store.adopt("foreign", 0, plan["digest"]))

    def test_a_file_changed_between_the_check_and_the_seal_is_refused(self):
        number = self.numbers["reader"]
        path = self.change()
        plan = self.store.adopt_plan("topic", number)
        original = local_store.checked
        done = []

        def swapped(*args, **kwargs):
            data = original(*args, **kwargs)
            if not done:
                done.append(True)
                self.write(path, self.read(path).replace(b"lenient quoting", b"swapped quoting", 1))   # after the check, before the seal
            return data
        with mock.patch.object(local_store, "checked", swapped):
            with self.assertRaises(record_store.StoreError) as caught:
                self.store.adopt("topic", number, plan["digest"])
        self.assertIn("changed while it was being sealed", str(caught.exception))
        self.assertIn(b"swapped quoting", self.read(path))
        self.assertEqual("changed", self.store.topic(number)[1].state)

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

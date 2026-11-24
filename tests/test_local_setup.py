"""Setting up a local record (1.9.0, spec section 9): the question about where the record is kept and its three forms, the answers a
local record takes and refuses, and `setup.py apply` for a local record in each place, with its dry run, its refusals before
anything is written, and the rule that no `gh` is started. Offline; the sandbox's `gh` is a script that logs and fails, and each
test ends by asserting that the log is empty (except where the test names a fake gh for the question, which is never the sandbox's)."""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_record import FIXTURE, LocalSandbox, ROOT, SCRIPTS  # noqa: E402,F401

FAKE_GH = os.path.join(ROOT, "tests", "fake_gh.py")
ID_NAME = re.compile(r"local--record/[a-z2-7]{16}")


class Fresh(LocalSandbox):
    """A project with no record yet: the sandbox's folders and its failing gh, nothing seeded."""

    seeded = False
    trusted = False

    def make_folder(self):
        return None   # setup makes the record: the sandbox makes none

    def write_config(self, config):
        pass

    def setUp(self):
        super().setUp()
        import setup
        self.setup = setup

    def answers(self, **changes):
        data = {"record": "local", "place": "own", "deliverables": [{"kind": "software"}], "decision_makers": ["the project lead"],
                "timezone": "UTC", "trust": True, "project": "sample project"}
        data.update(changes)
        path = os.path.join(self.dir, "answers.json")
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    def apply(self, *flags, **changes):
        return self.run_script("setup.py", "apply", "--answers", self.answers(**changes), "--root", self.project, *flags)

    def read_config(self):
        with open(os.path.join(self.project, ".recortex.json")) as handle:
            return json.load(handle)

    def repo_made(self):
        return self.read_config()["repo"]

    def fake_gh(self, **env):
        state = os.path.join(self.dir, "fake-state.json")
        with open(state, "w") as handle:
            json.dump({"repo": "octo-org/octo-repo"}, handle)
        return dict(RECORTEX_GH="python3 %s" % FAKE_GH, FAKE_GH_STATE=state, **env)


class RecordQuestionTests(Fresh):
    """9.1 to 9.3: the questions are printed by the script, word for word, the recommended option first."""

    def question(self, *args, **env):
        done = self.run_script("setup.py", "record-question", *args, env=env)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout.splitlines()

    def test_with_gh_ready_the_github_option_comes_first_and_names_the_account(self):
        lines = self.question(**self.fake_gh())
        self.assertEqual("recortex: where should this project's record be kept?", lines[0])
        self.assertEqual("- GitHub issues (Recommended): Issues in a repository, as account octocat; needs the GitHub CLI.", lines[1])
        self.assertEqual("- A local record: Markdown files in a folder on this machine; needs no account.", lines[2])
        self.assertEqual(self.setup.LOCAL_OFF, lines[3])
        self.assertEqual(4, len(lines))

    def test_with_gh_missing_or_signed_out_the_local_record_comes_first_and_github_says_why(self):
        cases = {"missing": dict(RECORTEX_GH=os.path.join(self.dir, "no-such-gh")),
                 "signed-out": self.fake_gh(FAKE_GH_SIGNED_OUT="1"), "unnamed": self.fake_gh(FAKE_GH_LOGIN="not a login!")}
        for state, env in cases.items():
            with self.subTest(state=state):
                lines = self.question(**env)
                self.assertEqual("- A local record (Recommended): Markdown files in a folder on this machine; needs no account.",
                                 lines[1])
                prefix = "- GitHub issues: "
                self.assertTrue(lines[2].startswith(prefix), lines[2])
                description = lines[2][len(prefix):]
                self.assertEqual(self.setup.NOT_READY[state][:120], description)
                self.assertLessEqual(len(description), 120)
                self.assertNotIn("octocat", "\n".join(lines))

    def test_the_login_is_printed_only_when_it_fits_a_github_login(self):
        lines = self.question(**self.fake_gh(FAKE_GH_LOGIN="octo-cat"))
        self.assertIn("as account octo-cat;", lines[1])
        lines = self.question(**self.fake_gh(FAKE_GH_LOGIN="-bad-"))
        self.assertNotIn("-bad-", "\n".join(lines))

    def test_the_question_about_the_folder_has_two_named_places_and_the_dialogs_other_answer(self):
        lines = self.question("--location")
        self.assertEqual("recortex: where should the local record's folder be?", lines[0])
        self.assertTrue(lines[1].startswith("- Its own folder (Recommended): Private to your account on this machine, outside the "
                                            "project."))
        self.assertTrue(lines[2].startswith("- Inside the project: A folder named .recortex in the project, committed with your "
                                            "code if you commit it."))
        self.assertIn("absolute path", lines[3])

    def test_the_question_about_a_record_inside_the_project_keeps_one_text_and_names_what_is_known(self):
        self.git("init", "-q", ".")   # a project of its own: the remotes of a checkout above it are not this project's
        text = ("recortex: the record will be in .recortex inside this project. If you commit it, it is committed with your code, "
                "and in a public repository anyone can read it. Keep the record inside the project?")
        lines = self.question("--in-project")
        self.assertEqual(["question inproject", "  " + text], lines[:2])
        self.assertTrue(lines[2].startswith("  - Keep it inside the project: Create .recortex here; nothing is added to .gitignore."))
        self.assertEqual("  - Use its own folder instead: Keep the record private, outside the project.", lines[3])

    def git(self, *args):
        subprocess.run(["git", "-C", self.project] + list(args), check=True, capture_output=True)

    def test_what_is_known_of_the_repository_comes_from_the_remotes_and_one_read_of_github(self):
        setup = self.setup
        self.git("init", "-q", ".")
        self.assertEqual("unknown", setup.in_project_state(self.project))   # no remote: no read
        self.git("remote", "add", "origin", "https://example.org/team/thing.git")
        self.assertEqual("unknown", setup.in_project_state(self.project))   # not GitHub's
        self.git("remote", "remove", "origin")
        self.git("remote", "add", "origin", "git@github.com:octo-org/octo-repo.git")
        calls = []

        def visibility(store, timeout=30):
            calls.append(store.repo)
            return answer[0]
        for state, value in (("private", "true"), ("public", "false"), ("unknown", "")):
            answer = [value]
            with mock.patch.object(setup.recortex, "gh_account", return_value=("ready", "octocat")), \
                    mock.patch.object(setup.record_store.GithubStore, "visibility", visibility):
                self.assertEqual(state, setup.in_project_state(self.project))
        self.assertEqual(["octo-org/octo-repo"] * 3, calls)
        with mock.patch.object(setup.recortex, "gh_account", return_value=("signed-out", None)):
            self.assertEqual("unknown", setup.in_project_state(self.project))   # gh not ready: GitHub is not asked
        self.assertEqual(3, len(calls))


class AnswerTests(Fresh):
    """9.4: what check_answers takes and refuses for a local record."""

    def check(self, **changes):
        data = {"record": "local", "deliverables": [{"kind": "software"}], "decision_makers": ["the project lead"]}
        data.update(changes)
        return self.setup.check_answers(data)

    def refuses(self, text, **changes):
        with self.assertRaises(self.setup.AnswerError) as caught:
            self.check(**changes)
        self.assertIn(text, str(caught.exception))

    def test_a_local_record_names_no_repository_and_defaults_to_its_own_folder(self):
        out = self.check()
        self.assertEqual(("own", None), out["_local"])
        self.assertIsNone(out["repo"])
        self.assertEqual(("project", None), self.check(place="project")["_local"])
        self.assertEqual(("own", "/srv/sample/record"), self.check(path="/srv/sample/record")["_local"])

    def test_a_board_a_triage_workflow_and_a_bad_place_or_path_are_refused(self):
        self.refuses("a local record has no board", board="new")
        self.refuses("a local record has no board", board="existing")
        self.refuses("a local record has no triage workflow", triage_workflow=True)
        self.refuses("place must be own or project", place="repo")
        for path in ("relative/record", "/srv/../etc/record", "/srv//record", "/srv/rec\nord", "/srv/rec\x00ord", "/" + "a" * 1000, 7):
            with self.subTest(path=ascii(path)):
                self.refuses("path must be a normal absolute path", path=path)
        self.refuses("path must be a normal absolute path", place="project", path="/srv/record")

    def test_a_github_record_still_needs_its_repository_and_nothing_else_changed(self):
        with self.assertRaises(self.setup.AnswerError):
            self.setup.check_answers({"deliverables": [{"kind": "software"}], "decision_makers": ["lead"]})
        out = self.setup.check_answers({"repo": "octo-org/octo-repo", "deliverables": [{"kind": "software"}],
                                        "decision_makers": ["lead"]})
        self.assertEqual("octo-org/octo-repo", out["repo"])
        self.assertNotIn("_local", out)


class ApplyLocalTests(Fresh):
    """9.4: setup.py apply for a local record, in order."""

    def test_a_dry_run_prints_the_steps_and_writes_nothing(self):
        before = self.snapshot()
        done = self.apply("--dry-run")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], self.storage.record_bindings())
        self.assertFalse(os.path.exists(self.trusted_file))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex.json")))
        for text in ("ready: Python", "would write: ", "would make: the record folder", "labels: a local record keeps each topic's "
                     "labels in the topic; nothing to create.", "would add: local--record/", "would run: ", "create_issues.py",
                     "index.py", "dry run: nothing was written"):
            self.assertIn(text, done.stdout)
        self.assertNotIn("gh auth", done.stdout)

    def snapshot(self):
        found = []
        for base in (self.dir,):
            for folder, names_, files in os.walk(base):
                names_[:] = [n for n in names_ if n not in ("claude", "state")]   # the state folder holds the grants and the key
                found += [os.path.join(folder, n) for n in names_ + files if n not in ("gh-calls.log", "answers.json")]
        return sorted(found)

    def test_a_local_record_in_its_own_folder_is_made_bound_trusted_and_synced(self):
        done = self.apply("--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("setup applied", done.stdout)
        config = self.read_config()
        self.assertRegex(config["repo"], ID_NAME)
        self.assertEqual((1, "local", "own"), (config["hub"], config["store"], config["place"]))
        record_id = config["repo"].split("/")[1]
        folder = self.storage.own_record_folder(record_id)
        self.assertTrue(os.path.isdir(folder), folder)
        self.assertEqual(["hub", "hub.md", "record.json"], sorted(os.listdir(folder)))
        self.assertTrue(any(row["id"] == record_id and row["place"] == "own" for row in self.storage.record_bindings()))
        with open(self.trusted_file) as handle:
            self.assertIn(config["repo"], handle.read().split())
        store = self.record_store.for_repo(config["repo"], config)
        self.assertEqual("hub", store.topic(1)[1].kind)
        kinds = sorted(self.local_store.parse_file(self.read(os.path.join(folder, "hub", n)), "hub/" + n, record_id).kind
                       for n in os.listdir(os.path.join(folder, "hub")))
        self.assertEqual(["body", "entry"], kinds)   # the hub's first text, and the first resume block
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex")))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".gitignore")))
        self.assertIn("labels: a local record keeps each topic's labels in the topic; nothing to create.", done.stdout)
        self.assertNotIn("notice: this record is inside the project", done.stdout)

    def test_the_hub_and_the_first_resume_block_read_back_through_the_record(self):
        self.assertEqual(0, self.apply("--yes").returncode)
        brief = self.run_script("resume_block.py", "show")
        self.assertEqual(0, brief.returncode, brief.stderr)
        self.assertIn("The record of this project has just been set up", brief.stdout)
        self.assertIn("by local--record", brief.stdout)

    def test_a_chosen_folder_outside_the_project_keeps_the_record_and_its_place(self):
        path = os.path.join(self.outside, "synced-record")
        os.mkdir(path, 0o700)
        done = self.apply("--yes", path=path)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        record_id = self.repo_made().split("/")[1]
        self.assertTrue(os.path.isfile(os.path.join(os.path.realpath(path), "record.json")))
        self.assertIn(("chosen", os.path.realpath(path)),
                      [(row["place"], row["path"]) for row in self.storage.record_bindings() if row["id"] == record_id])
        self.assertFalse(os.path.exists(os.path.join(self.storage.records_top(), "private", "records", record_id)))

    def test_a_chosen_folder_that_others_can_write_is_refused_and_nothing_is_written(self):
        path = os.path.join(self.outside, "open-record")
        os.mkdir(path)
        os.chmod(path, 0o777)
        before = self.snapshot()
        done = self.apply("--yes", path=path)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("the record folder was refused", done.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex.json")))
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], os.listdir(path))
        self.assertEqual([], self.storage.record_bindings())

    def test_a_folder_inside_the_project_is_refused_as_a_path(self):
        done = self.apply("--yes", path=os.path.join(self.project, "inner"))
        self.assertNotEqual(0, done.returncode)
        self.assertIn("lies outside the project", done.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex.json")))

    def test_a_project_file_that_already_names_a_record_is_not_changed(self):
        for existing in ("octo-org/octo-repo", "local--record/k3f5x2m7q4w6z5ab"):
            with self.subTest(existing=existing):
                with open(os.path.join(self.project, ".recortex.json"), "w") as handle:
                    json.dump({"repo": existing, "hub": 4}, handle)
                before = self.snapshot()
                done = self.apply("--yes")
                self.assertNotEqual(0, done.returncode)
                self.assertIn("already names %s" % existing, done.stderr)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(existing, self.read_config()["repo"])

    def test_a_board_or_a_triage_workflow_is_refused_before_anything_is_written(self):
        before = self.snapshot()
        for changes in (dict(board="new"), dict(triage_workflow=True)):
            done = self.apply("--yes", **changes)
            self.assertNotEqual(0, done.returncode)
            self.assertIn("a local record has no", done.stderr)
        self.assertEqual(before, self.snapshot())

    def test_no_gh_is_started_and_the_step_that_would_need_one_is_not_reached(self):
        done = self.apply("--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertNotIn("gh ", done.stdout.replace("the user's gh", ""))
        self.assertFalse(os.path.exists(self.gh_log))   # the base class asserts it again at the end

    def test_a_record_inside_the_project_is_refused_without_the_users_answer_and_makes_nothing(self):
        before = self.snapshot()
        done = self.apply("--yes", place="project")
        self.assertNotEqual(0, done.returncode)
        self.assertIn("needs your answer to its question", done.stderr)
        self.assertIn("acts.py ask inproject", done.stdout)
        self.assertEqual([], self.storage.record_bindings())
        self.assertEqual(before, self.snapshot())
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex")))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex.json")))

    def apply_in_process(self, **changes):
        """setup.py apply in this process with the answer to the in-project question given: the grant itself is the test of
        test_local_acts."""
        import acts
        out = io.StringIO()
        argv = ["setup.py", "apply", "--answers", self.answers(**changes), "--root", self.project, "--yes"]
        with contextlib.redirect_stdout(out), mock.patch.object(acts, "allowed", return_value=True) as allowed, \
                mock.patch.object(sys, "argv", argv):
            code = self.setup.main()
        return code, out.getvalue(), allowed

    def test_a_record_inside_the_project_gets_its_folder_a_gitattributes_and_the_notice_and_nothing_in_git(self):
        subprocess.run(["git", "-C", self.project, "init", "-q", "."], check=True)
        code, text, allowed = self.apply_in_process(place="project")
        self.assertEqual(0, code, text)
        allowed.assert_called_once()
        self.assertEqual("inproject", allowed.call_args[0][0])
        self.assertEqual(os.path.realpath(self.project), allowed.call_args[0][1]["project"])
        folder = os.path.join(os.path.realpath(self.project), ".recortex")
        self.assertEqual([".gitattributes", "hub", "hub.md", "record.json"], sorted(os.listdir(folder)))
        with open(os.path.join(folder, ".gitattributes"), "rb") as handle:
            self.assertEqual(b"* -text\n", handle.read())
        self.assertFalse(os.path.exists(os.path.join(self.project, ".gitignore")))
        listed = subprocess.run(["git", "-C", self.project, "ls-files"], capture_output=True, text=True).stdout
        self.assertEqual("", listed)   # nothing was added
        self.assertEqual("project", self.read_config()["place"])
        self.assertIn("notice: this record is inside the project; if the project's repository is public, anyone can read every "
                      "entry in it.", text)

    def test_the_check_of_the_place_comes_before_the_project_file_and_a_link_is_refused(self):
        os.symlink(self.outside, os.path.join(self.project, ".recortex"))
        code, text, allowed = None, "", None
        with self.assertRaises(SystemExit) as caught:
            code, text, allowed = self.apply_in_process(place="project")
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex.json")))
        self.assertEqual([], os.listdir(self.outside))

    def test_an_existing_folder_with_something_in_it_is_not_written_into(self):
        os.mkdir(os.path.join(self.project, ".recortex"), 0o700)
        with open(os.path.join(self.project, ".recortex", "notes.md"), "w") as handle:
            handle.write("a file of the user's\n")
        with self.assertRaises(SystemExit) as caught:
            self.apply_in_process(place="project")
        self.assertIn("exists and is not empty", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".recortex.json")))
        self.assertEqual(["notes.md"], os.listdir(os.path.join(self.project, ".recortex")))


if __name__ == "__main__":
    unittest.main()

"""Who may turn the observer on (S3 of the 1.5.0 review): `observer.py enable --yes` and `setup.py observer --yes` run by a
session need the one-time grant that the hook records from the user's own recorded answer to the level question (for that
level) or to the paper-proposals question (for the stronger model); the user's terminal needs none; lowering anything
needs none. And the line that says a session switched it on names the level and the stronger model. Offline."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_setup as tj  # noqa: E402
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import observer  # noqa: E402
import observer_grants as grants  # noqa: E402
import observer_hooks  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import observer_sources  # noqa: E402
import pretool_guard  # noqa: E402

REPO = tj.REPO
STORED = {"enabled": True, "mode": "automatic", "level": "assistant", "strong_model": ""}


class NeedsTests(unittest.TestCase):
    def test_what_each_change_needs(self):
        table = (
            ({}, "everything", None, "shadow", [("level", "everything")]),            # a new folder
            ({"enabled": True, "mode": "automatic"}, "everything", None, "automatic", [("level", "everything")]),   # 1.4.0: no level
            (STORED, "everything", None, "automatic", [("level", "everything")]),     # raised
            (STORED, "assistant", None, "automatic", []),                              # the same
            (STORED, "notes", None, "automatic", []),                                  # lowered
            (STORED, None, None, "automatic", []),                                     # no level given: the folder's own
            (dict(STORED, mode="shadow"), "assistant", None, "automatic", [("level", "assistant")]),   # posting starts
            (dict(STORED, mode="shadow"), "assistant", None, "shadow", []),
            (STORED, "assistant", "opus", "automatic", [("strong", "opus")]),          # the stronger model is new
            (dict(STORED, strong_model="opus"), "assistant", "opus", "automatic", []),
            (dict(STORED, strong_model="opus"), "assistant", None, "automatic", []),   # removing it is less
            (dict(STORED, strong_model="opus"), "everything", "opus", "automatic", [("level", "everything")]),
            ({}, "notes", "opus", "shadow", [("level", "notes"), ("strong", "opus")]),
        )
        for stored, level, strong, mode, wanted in table:
            self.assertEqual(wanted, grants.needs(stored, level, strong, mode), (stored, level, strong, mode))
            self.assertEqual(wanted, grants.needs(stored, level, strong, mode, holds=True), (stored, level, strong, mode))

    def test_an_approval_that_does_not_hold_here_is_no_authority_for_its_level(self):
        # A stored approval bound to another host (holds=False): the same level, a lower one, or none named is a new approval.
        table = (
            (STORED, "assistant", None, "automatic", [("level", "assistant")]),   # the same level
            (STORED, "notes", None, "automatic", [("level", "notes")]),           # lowered
            (STORED, None, None, "automatic", [("level", "assistant")]),          # none named: the folder's own is used
            (STORED, "everything", None, "automatic", [("level", "everything")]),
            (dict(STORED, mode="shadow"), None, None, "shadow", [("level", "assistant")]),
            (dict(STORED, strong_model="opus"), "assistant", "opus", "automatic", [("level", "assistant")]),
            ({}, None, None, "shadow", []),                                        # nothing stored and no level: nothing to grant
        )
        for stored, level, strong, mode, wanted in table:
            self.assertEqual(wanted, grants.needs(stored, level, strong, mode, holds=False), (stored, level, strong, mode))


class FileTests(tj.Programs, tj.tq.Box):
    """The grants file: a level grant holds for its level and 10 minutes, once; a pass covers one chain."""

    def setUp(self):
        super().setUp()
        self.in_process()
        self.folder = runtime.folder_key(self.project)

    def test_a_grant_is_for_one_level_for_ten_minutes_and_is_used_once(self):
        self.assertTrue(grants.put(self.folder, "level", "everything", "toolu_1"))
        self.assertTrue(grants.held(self.folder, [("level", "everything")]))
        self.assertFalse(grants.held(self.folder, [("level", "assistant")]))
        self.assertFalse(grants.held(self.folder, [("level", "everything"), ("strong", "opus")]))
        self.assertFalse(grants.take(self.folder, [("level", "assistant")]))
        self.assertTrue(grants.take(self.folder, [("level", "everything")]))
        self.assertFalse(grants.held(self.folder, [("level", "everything")]))   # spent
        self.assertFalse(grants.take(self.folder, [("level", "everything")]))

    def test_an_old_grant_is_not_one(self):
        grants.put(self.folder, "level", "notes", "toolu_1", now=time.time() - grants.SECONDS - 5)
        self.assertFalse(grants.held(self.folder, [("level", "notes")]))

    def test_a_grant_holds_for_its_folder_only(self):
        grants.put(self.folder, "level", "notes", "toolu_1")
        self.assertFalse(grants.held(os.path.join(self.folder, "other"), [("level", "notes")]))

    def test_nothing_is_recorded_for_a_level_or_model_the_questions_do_not_offer(self):
        self.assertFalse(grants.put(self.folder, "level", "most", "toolu_1"))
        self.assertFalse(grants.put(self.folder, "strong", "haiku", "toolu_1"))   # another model is typed in the Other box
        self.assertFalse(grants.put(self.folder, "level", "notes", "not an id; rm"))
        self.assertFalse(grants.held(self.folder, [("level", "notes")]))

    def test_a_pass_covers_the_target_of_one_chain_and_ends(self):
        word = grants.start_pass(self.folder, "assistant", "opus")
        self.assertTrue(grants.in_pass(self.folder, [("level", "assistant")], word))
        self.assertTrue(grants.in_pass(self.folder, [("strong", "opus"), ("level", "assistant")], word))
        self.assertFalse(grants.in_pass(self.folder, [("level", "everything")], word))
        self.assertFalse(grants.in_pass(self.folder, [("level", "assistant")], "0" * 32))
        self.assertFalse(grants.in_pass(self.folder, [("level", "assistant")], None))
        grants.end_pass(self.folder, word)
        self.assertFalse(grants.in_pass(self.folder, [("level", "assistant")], word))

    def test_a_spent_answer_grants_nothing_again(self):
        self.assertTrue(grants.put(self.folder, "level", "everything", "toolu_replay"))
        self.assertTrue(grants.put(self.folder, "level", "everything", "toolu_replay"))   # seen again before use: no change
        self.assertTrue(grants.take(self.folder, [("level", "everything")]))
        self.assertFalse(grants.put(self.folder, "level", "everything", "toolu_replay"))   # the transcript still holds it
        self.assertFalse(grants.put(self.folder, "level", "notes", "toolu_replay"))
        self.assertFalse(grants.held(self.folder, [("level", "everything")]))
        self.assertTrue(grants.put(self.folder, "level", "everything", "toolu_new"))      # a new answer does

    def test_the_spent_answers_are_kept_and_bounded(self):
        for number in range(grants.SPENT + 5):
            grants.put(self.folder, "level", "notes", "toolu_%d" % number)
            grants.take(self.folder, [("level", "notes")])
        with open(os.path.join(runtime.approvals_folder(), grants.FILE)) as handle:
            spent = json.load(handle)["spent"]
        self.assertEqual(grants.SPENT, len(spent))
        self.assertEqual("toolu_%d" % (grants.SPENT + 4), spent[-1])

    def test_the_file_is_private_and_holds_no_text(self):
        grants.put(self.folder, "level", "notes", "toolu_1")
        path = os.path.join(runtime.approvals_folder(), grants.FILE)
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
        with open(path) as handle:
            value = json.load(handle)
        self.assertEqual({"form", "folders", "spent"}, set(value))
        self.assertEqual({"value", "answer", "until"}, set(value["folders"][self.folder]["level"]))


class CommandTests(tj.Programs, tj.tq.Box):
    """The commands as a session runs them: no terminal, the answer planted where the hook leaves it."""

    def setUp(self):
        super().setUp()
        self.bin, self.log = self.stand_in()
        self.env_claude = self.with_path(self.bin)

    def enable(self, *args):
        return self.tool("observer.py", "--repo", self.repo_name, "enable", "--model", "sonnet", *args, "--yes",
                         env=self.env_claude)

    def code(self, done):
        return json.loads(done.stdout.strip().splitlines()[-1]).get("code")

    def entry(self):
        return self.status(env=self.env_claude)

    def test_without_the_users_answer_a_session_turns_nothing_on(self):
        done = self.enable("--level", "everything")
        self.assertEqual((1, "enable_unanswered"), (done.returncode, self.code(done)))
        self.assertIn("answer to the question", json.loads(done.stdout.strip().splitlines()[-1])["note"])
        self.assertFalse(self.entry()["approved"])

    def test_with_it_the_level_the_user_chose_is_turned_on_once(self):
        self.answer(level="everything")
        done = self.enable("--level", "everything")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual("everything", self.entry()["level"])
        self.assertEqual(0, self.enable("--level", "notes").returncode)   # lowering needs no answer
        again = self.enable("--level", "everything")                       # raising it again: the answer was used
        self.assertEqual((1, "enable_unanswered"), (again.returncode, self.code(again)))
        self.assertEqual("notes", self.entry()["level"])

    def test_the_answer_is_for_its_own_level_and_a_session_cannot_choose_another(self):
        self.answer(level="assistant")
        done = self.enable("--level", "everything")
        self.assertEqual((1, "enable_unanswered"), (done.returncode, self.code(done)))
        self.assertFalse(self.entry()["approved"])
        self.assertEqual(0, self.enable("--level", "assistant").returncode)

    def test_lowering_the_level_needs_no_answer(self):
        self.answer(level="everything")
        self.assertEqual(0, self.enable("--level", "everything").returncode)
        self.assertEqual(0, self.enable("--level", "notes").returncode)
        self.assertEqual("notes", self.entry()["level"])

    def test_the_stronger_model_needs_the_answer_about_it_and_the_level_answer_does_not_cover_it(self):
        self.answer(level="assistant")
        self.assertEqual(0, self.enable("--level", "assistant").returncode)
        self.answer(level="assistant")   # the level question's answer again: not the paper-proposals one
        done = self.enable("--level", "assistant", "--strong-model", "opus")
        self.assertEqual((1, "enable_unanswered"), (done.returncode, self.code(done)))
        self.answer(strong="opus")
        self.assertEqual(0, self.enable("--level", "assistant", "--strong-model", "opus").returncode)
        self.assertEqual("opus", self.entry()["settings"]["strong_model"])

    def test_an_old_answer_does_not_count(self):
        self.in_process()
        grants.put(runtime.folder_key(self.project), "level", "notes", "toolu_1", now=time.time() - 700)
        done = self.enable("--level", "notes")
        self.assertEqual((1, "enable_unanswered"), (done.returncode, self.code(done)))

    def test_a_1_4_0_approval_stays_paused_for_a_session_with_no_answer(self):
        self.approval(observer_mode="automatic")   # what 1.4.0 stored: on, no level
        done = self.enable("--level", "notes")
        self.assertEqual((1, "enable_unanswered"), (done.returncode, self.code(done)))
        self.assertTrue(self.entry()["paused"])
        self.answer(level="notes")
        self.assertEqual(0, self.enable("--level", "notes").returncode)
        self.assertFalse(self.entry()["paused"])

    def test_the_session_can_ask_the_question_word_for_word_and_the_wizard_row_says_so(self):
        for flag, kind in (((), "level"), (("--strong",), "strong")):
            done = self.tool("setup.py", "observer-question", *flag, env=self.env_claude)
            self.assertEqual(0, done.returncode, done.stderr)
            text, labels = grants.canonical(kind)
            self.assertEqual([text] + ["- " + label for label in labels], done.stdout.splitlines())
        with open(os.path.join(ts.SKILL, "references", "setup.md")) as handle:
            self.assertIn("setup.py observer-question", handle.read())

    def test_the_users_terminal_needs_no_answer(self):
        import observer
        self.in_process()
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        with mock.patch.object(observer, "on_terminal", return_value=True), mock.patch.object(observer.recortex, "load_config",
                                                                                              return_value=self.config), \
                mock.patch("sys.stdout", new=__import__("io").StringIO()):
            code = observer.main(["--repo", self.repo_name, "enable", "--model", "sonnet", "--level", "everything", "--yes"])
        self.assertEqual(0, code)

    def test_the_chain_of_setup_takes_the_answer_once_and_gives_its_commands_a_pass_nobody_sees(self):
        done = self.tool("setup.py", "observer", "--level", "notes", "--yes", env=self.env_claude)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("observer: not turned on. ", done.stdout)
        self.assertNotIn("run: ", done.stdout)   # nothing was started
        self.assertEqual([], self.calls(self.log))
        self.assertFalse(self.entry()["approved"])
        self.answer(level="notes")
        done = self.tool("setup.py", "observer", "--level", "notes", "--yes", env=self.env_claude)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("observer: on at the level Notes only", done.stdout)
        self.assertNotIn("--pass", done.stdout)            # the pass is on no printed line
        self.assertRegex(done.stdout, r"enable --mode shadow .* --yes   # ")
        self.assertTrue(self.entry()["approved"])
        raised = self.tool("setup.py", "observer", "--level", "everything", "--yes", env=self.env_claude)   # the answer was used
        self.assertIn("observer: not turned on. ", raised.stdout)
        self.assertEqual("notes", self.entry()["level"])
        self.in_process()
        self.assertFalse(grants.held(runtime.folder_key(self.project), [("level", "notes")]))

    def test_a_second_run_of_the_chain_with_nothing_to_raise_still_ends_in_the_mode_it_began_in(self):
        self.answer(level="notes")
        self.assertEqual(0, self.tool("setup.py", "observer", "--level", "notes", "--yes", env=self.env_claude).returncode)
        self.assertEqual("automatic", self.entry()["settings"]["mode"])
        done = self.tool("setup.py", "observer", "--level", "notes", "--yes", env=self.env_claude)   # no new answer, nothing raised
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("observer: on at the level Notes only in automatic mode", done.stdout)
        self.assertEqual("automatic", self.entry()["settings"]["mode"])   # not left in shadow by its own first step

    def test_the_users_own_terminal_runs_the_whole_chain_with_no_answer(self):
        import observer
        import setup as wizard
        self.in_process()
        with mock.patch.object(observer, "on_terminal", return_value=True), \
                mock.patch.object(grants, "take", side_effect=AssertionError("a terminal needs no grant")):
            word = wizard.chain_pass(runtime.folder_key(self.project), "notes", None, [("level", "notes")])
        self.assertTrue(grants.in_pass(runtime.folder_key(self.project), [("level", "notes")], word))
        grants.end_pass(runtime.folder_key(self.project), word)

    def test_after_the_chain_the_pass_is_gone(self):
        self.answer(level="notes")
        self.assertEqual(0, self.tool("setup.py", "observer", "--level", "notes", "--yes", env=self.env_claude).returncode)
        self.in_process()
        with open(os.path.join(runtime.approvals_folder(), grants.FILE)) as handle:
            entries = json.load(handle)["folders"].get(runtime.folder_key(self.project), {})
        self.assertNotIn("pass", entries)

    def test_the_strong_step_of_setup_needs_its_own_answer(self):
        self.answer(level="everything")
        self.assertEqual(0, self.tool("setup.py", "observer", "--level", "everything", "--yes", env=self.env_claude).returncode)
        self.answer(level="everything")   # not the paper-proposals answer
        done = self.tool("setup.py", "observer", "--strong", "opus", "--yes", env=self.env_claude)
        self.assertIn("observer: not turned on. ", done.stdout)
        self.assertEqual("", self.entry()["settings"]["strong_model"])
        self.answer(strong="opus")
        done = self.tool("setup.py", "observer", "--strong", "opus", "--yes", env=self.env_claude)
        self.assertIn("with sonnet and opus for the paper's text", done.stdout)


class TranscriptBox(unittest.TestCase):
    """A native transcript in the system's temporary folder, which the observer's checks accept (as HookBox builds it),
    for the hook that records the grant from the user's answer."""

    def setUp(self):
        from observer_source_fixture import fixture_trusted_ancestors
        tmp = tempfile.TemporaryDirectory(prefix="observer-enable-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.base = os.path.realpath(tmp.name)
        metadata = fixture_trusted_ancestors(__import__("pathlib").Path(self.base))
        metadata.__enter__()
        self.addCleanup(metadata.__exit__, None, None, None)
        self.project = os.path.join(self.base, "project")
        os.mkdir(self.project, 0o700)
        patch = mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.base, "state"), "HOME": os.path.join(self.base, "home"),
                                             "XDG_CACHE_HOME": os.path.join(self.base, "cache"),
                                             "XDG_CONFIG_HOME": os.path.join(self.base, "config"),
                                             "CLAUDE_CONFIG_DIR": os.path.join(self.base, "claude"),
                                             "CLAUDE_PROJECT_DIR": self.project}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.path = os.path.join(observer_sources.project_directory(self.project), "session1.jsonl")
        os.makedirs(os.path.dirname(self.path), mode=0o700)
        for folder in (os.path.join(self.base, "claude"), os.path.join(self.base, "claude", "projects"), os.path.dirname(self.path)):
            os.chmod(folder, 0o700)
        with open(self.path, "w"):
            pass
        os.chmod(self.path, 0o600)
        self.folder = runtime.folder_key(self.project)
        self.count = 0

    def ask(self, kind, answer, text=None, labels=None):
        import setup_questions
        question = next(q for q in setup_questions.QUESTIONS if q.id == grants.QUESTION_OF[kind])
        text = text or question.text.format(sort=80, middle=70, strong=8, proposals=2)
        labels = labels or [option.label for option in question.options]
        self.count += 1
        ident = "toolu_01enable%d" % self.count
        asked = {"question": text, "header": "Observer", "multiSelect": False,
                 "options": [{"label": label, "description": label} for label in labels]}
        use = {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": ident, "name": "AskUserQuestion", "input": {"questions": [asked]}}]}}
        result = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident,
             "content": 'User has answered your questions: "%s"="%s". You can now continue with the user\'s answers in mind.'
                        % (text, answer)}]},
                  "toolUseResult": {"questions": [asked], "answers": {text: answer}}}
        with open(self.path, "a") as handle:
            handle.write(json.dumps(use) + "\n" + json.dumps(result) + "\n")

    def hook(self, command):
        data = {"hook_event_name": "PreToolUse", "session_id": "session1", "transcript_path": self.path, "cwd": self.project,
                "tool_name": "Bash", "tool_input": {"command": command}}
        self.assertIsNone(pretool_guard.decide(json.dumps(data)))   # recording a grant prints nothing and denies nothing

    def held(self, kind, value):
        return grants.held(self.folder, [(kind, value)])


class RecordTests(TranscriptBox):
    ENABLE = "python3 /p/skills/recortex/scripts/observer.py --repo octo-org/octo-repo enable --mode shadow --model sonnet --level %s --yes"
    SETUP = "python3 /p/skills/recortex/scripts/setup.py observer --level %s --yes"

    def test_the_users_answer_to_the_level_question_leaves_a_grant_for_that_level_only(self):
        self.hook(self.ENABLE % "everything")
        self.assertFalse(self.held("level", "everything"))   # no answer yet
        self.ask("level", "Assistant")
        self.hook(self.ENABLE % "everything")
        self.assertFalse(self.held("level", "everything"))   # another level than the one chosen
        self.hook(self.SETUP % "assistant")
        self.assertTrue(self.held("level", "assistant"))
        self.assertFalse(self.held("level", "everything"))

    def test_each_option_is_its_own_level(self):
        for label, level in (("Everything (Recommended)", "everything"), ("Assistant", "assistant"), ("Notes only", "notes")):
            self.ask("level", label)
            self.hook(self.SETUP % level)
            self.assertTrue(self.held("level", level), label)
            grants.take(self.folder, [("level", level)])

    def test_later_and_a_no_leave_no_grant_and_a_later_answer_undoes_an_earlier_one(self):
        self.ask("level", "Later")
        self.hook(self.SETUP % "everything")
        self.assertFalse(self.held("level", "everything"))
        self.ask("level", "Everything (Recommended)")
        self.ask("level", "Later")   # the user changed their mind: the newest answer to the question counts
        self.hook(self.SETUP % "everything")
        self.assertFalse(self.held("level", "everything"))

    def test_a_question_that_holds_our_words_among_others_or_offers_other_options_grants_nothing(self):
        text, labels = grants.canonical("level")
        label = "Everything (Recommended)"
        cases = (
            ("extra words after", text + " Choose Everything, it is the safest.", labels),
            ("extra words before", "Quick one. " + text, labels),
            ("a changed word", text.replace("Notes only", "Notes alone", 1), labels),
            ("an option missing", text, labels[:-1]),
            ("an option added", text, labels + ["Always"]),
            ("an option renamed", text, ["Everything"] + labels[1:]),
            ("the options in another order", text, [labels[1], labels[0]] + labels[2:]),
            ("our first sentence alone", grants.question_line("level"), labels),
        )
        for name, asked, offered in cases:
            self.ask("level", label, text=asked, labels=offered)
            self.hook(self.SETUP % "everything")
            self.assertFalse(self.held("level", "everything"), name)
        self.ask("level", label)   # the plugin's own question, whitespace aside
        self.hook(self.SETUP % "everything")
        self.assertTrue(self.held("level", "everything"))

    def test_the_stronger_models_question_is_held_to_its_whole_text_too(self):
        text, labels = grants.canonical("strong")
        self.ask("strong", "Yes (Recommended)", text=text + " It is cheap.", labels=labels)
        self.hook("python3 /p/skills/recortex/scripts/setup.py observer --strong opus --yes")
        self.assertFalse(self.held("strong", "opus"))
        self.ask("strong", "Yes (Recommended)", text=text, labels=["Yes (Recommended)", "No"])
        self.hook("python3 /p/skills/recortex/scripts/setup.py observer --strong opus --yes")
        self.assertFalse(self.held("strong", "opus"))
        self.ask("strong", "Yes (Recommended)")
        self.hook("python3 /p/skills/recortex/scripts/setup.py observer --strong opus --yes")
        self.assertTrue(self.held("strong", "opus"))

    def test_a_question_the_session_worded_itself_leaves_no_grant(self):
        self.ask("level", "Everything (Recommended)", text="Shall I switch it all on?", labels=["Everything (Recommended)", "Later"])
        self.hook(self.SETUP % "everything")
        self.assertFalse(self.held("level", "everything"))

    def test_the_stronger_model_is_the_answer_to_its_own_question(self):
        self.ask("level", "Everything (Recommended)")
        self.hook("python3 /p/skills/recortex/scripts/setup.py observer --strong opus --yes")
        self.assertFalse(self.held("strong", "opus"))   # the level answer is not that one
        self.ask("strong", "Yes (Recommended)")
        self.hook("python3 /p/skills/recortex/scripts/setup.py observer --strong opus --yes")
        self.assertTrue(self.held("strong", "opus"))
        self.hook("python3 /p/skills/recortex/scripts/setup.py observer --strong haiku --yes")
        self.assertFalse(self.held("strong", "haiku"))   # the question names opus; another model is the user's own command

    def test_the_wizards_answers_file_and_the_enable_command_are_read_for_what_they_ask(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=self.base, delete=False) as handle:
            json.dump({"observer": {"mode": "automatic", "model": "sonnet", "level": "notes"}}, handle)
        self.ask("level", "Notes only")
        self.hook("python3 /p/skills/recortex/scripts/setup.py apply --answers %s --yes" % handle.name)
        self.assertTrue(self.held("level", "notes"))

    def test_an_answer_cannot_be_used_twice_through_the_transcript(self):
        self.ask("level", "Notes only")
        self.hook(self.SETUP % "notes")
        self.assertTrue(grants.take(self.folder, [("level", "notes")]))
        self.hook(self.SETUP % "notes")   # the same answer is still in the transcript
        self.assertFalse(self.held("level", "notes"))
        self.ask("level", "Notes only")   # a new answer is a new grant
        self.hook(self.SETUP % "notes")
        self.assertTrue(self.held("level", "notes"))

    def test_a_subagents_call_records_nothing_whatever_the_user_answered(self):
        self.ask("level", "Notes only")
        for extra in ({"agent_id": "a1b2c3"}, {"agent_type": "general-purpose"}, {"agent_id": "a1", "agent_type": "recortex:record-drafter"}):
            data = {"hook_event_name": "PreToolUse", "session_id": "session1", "transcript_path": self.path, "cwd": self.project,
                    "tool_name": "Bash", "tool_input": {"command": self.SETUP % "notes"}}
            data.update(extra)
            self.assertIsNone(pretool_guard.decide(json.dumps(data)))
            self.assertFalse(self.held("level", "notes"), extra)
        self.hook(self.SETUP % "notes")   # the main session's own call does
        self.assertTrue(self.held("level", "notes"))

    def test_other_commands_leave_nothing(self):
        self.ask("level", "Everything (Recommended)")
        for command in ("python3 /p/skills/recortex/scripts/observer.py enable --level everything --model sonnet",   # a preview
                        "python3 /p/skills/recortex/scripts/setup.py observer --level everything --dry-run",
                        "echo observer.py enable --level everything --yes; ls",
                        "python3 /p/skills/recortex/scripts/observer.py status"):
            self.hook(command)
        self.assertFalse(self.held("level", "everything"))
        self.assertIsNone(grants.requested("python3 /p/scripts/observer.py enable --level everything --yes extra \""))

    def test_a_transcript_that_cannot_be_read_leaves_no_grant_and_no_error(self):
        self.ask("level", "Everything (Recommended)")
        os.chmod(self.path, 0o666)   # not the user's own private file: refused
        self.hook(self.SETUP % "everything")
        self.assertFalse(self.held("level", "everything"))


class MapGrantTests(TranscriptBox):
    """7.3: the user's recorded answer to the map-upkeep question lets a session raise the map mode, once."""

    MAP = "python3 /p/skills/recortex/scripts/observer.py --repo octo-org/octo-repo map-mode %s --yes"

    def test_the_answer_automatic_grants_automatic_and_not_the_other_mode(self):
        self.ask("map", "Automatic (Recommended)")
        self.hook(self.MAP % "shadow")
        self.assertFalse(self.held("map", "shadow"))   # a "shadow" request on an "Automatic" answer
        self.hook(self.MAP % "automatic")
        self.assertTrue(self.held("map", "automatic"))
        self.assertFalse(self.held("map", "shadow"))

    def test_suggest_only_grants_shadow_and_not_automatic(self):
        self.ask("map", "Suggest only")
        self.hook(self.MAP % "automatic")
        self.assertFalse(self.held("map", "automatic"))
        self.hook(self.MAP % "shadow")
        self.assertTrue(self.held("map", "shadow"))

    def test_no_later_and_a_later_change_of_mind_grant_nothing(self):
        for answer in ("No", "Later"):
            self.ask("map", answer)
            self.hook(self.MAP % "automatic")
            self.assertFalse(self.held("map", "automatic"), answer)
        self.ask("map", "Automatic (Recommended)")
        self.ask("map", "No")
        self.hook(self.MAP % "automatic")
        self.assertFalse(self.held("map", "automatic"))

    def test_a_reworded_question_or_other_options_grant_nothing(self):
        text, labels = grants.canonical("map")
        for name, asked, offered in (("extra words", text + " It is safe.", labels), ("a missing option", text, labels[:-1]),
                                     ("another order", text, labels[::-1]), ("the first sentence alone", grants.question_line("map"), labels),
                                     ("our own wording", "Shall I let the observer keep the map?", labels)):
            self.ask("map", "Automatic (Recommended)", text=asked, labels=offered)
            self.hook(self.MAP % "automatic")
            self.assertFalse(self.held("map", "automatic"), name)
        self.ask("map", "Automatic (Recommended)")
        self.hook(self.MAP % "automatic")
        self.assertTrue(self.held("map", "automatic"))

    def test_a_subagents_call_makes_no_grant_and_the_main_sessions_own_does(self):
        self.ask("map", "Automatic (Recommended)")
        for extra in ({"agent_id": "a1b2c3"}, {"agent_type": "general-purpose"}):
            data = {"hook_event_name": "PreToolUse", "session_id": "session1", "transcript_path": self.path, "cwd": self.project,
                    "tool_name": "Bash", "tool_input": {"command": self.MAP % "automatic"}}
            data.update(extra)
            self.assertIsNone(pretool_guard.decide(json.dumps(data)))
            self.assertFalse(self.held("map", "automatic"), extra)
        self.hook(self.MAP % "automatic")
        self.assertTrue(self.held("map", "automatic"))

    def test_an_answer_grants_once_whatever_the_transcript_still_holds(self):
        self.ask("map", "Automatic (Recommended)")
        self.hook(self.MAP % "automatic")
        self.assertTrue(grants.take(self.folder, [("map", "automatic")]))
        self.hook(self.MAP % "automatic")
        self.assertFalse(self.held("map", "automatic"))

    def test_off_asks_for_nothing_and_the_value_must_be_a_mode(self):
        self.assertFalse(grants.put(self.folder, "map", "off", "toolu_1"))
        self.assertFalse(grants.put(self.folder, "map", "everything", "toolu_2"))
        self.assertTrue(grants.put(self.folder, "map", "shadow", "toolu_3"))
        self.assertEqual({"map": "shadow"}, grants.requested(self.MAP % "shadow"))
        self.assertIsNone(grants.requested(self.MAP.replace(" --yes", "") % "shadow"))
        self.assertEqual({}, grants.requested(self.MAP % "off"))
        self.assertEqual("Automatic (Recommended)", grants.label_for("map", "automatic"))
        self.assertEqual("Suggest only", grants.label_for("map", "shadow"))
        self.assertIsNone(grants.label_for("map", "off"))


class OneCallTests(TranscriptBox):
    """Two grant questions answered in one question call share the call's tool id: each grant is spent by its own kind, so the
    second one is honored (the 1.7.0 review, S2)."""

    MAP = "python3 /p/skills/recortex/scripts/observer.py --repo octo-org/octo-repo map-mode automatic --yes"
    STRONG = "python3 /p/skills/recortex/scripts/setup.py observer --strong opus --yes"

    def ask_both(self):
        import setup_questions
        asked, answers = [], {}
        for kind, answer in (("strong", "Yes (Recommended)"), ("map", "Automatic (Recommended)")):
            question = next(q for q in setup_questions.QUESTIONS if q.id == grants.QUESTION_OF[kind])
            text = question.text.format(sort=80, middle=70, strong=8, proposals=2)
            asked.append({"question": text, "header": kind, "multiSelect": False,
                          "options": [{"label": o.label, "description": o.label} for o in question.options]})
            answers[text] = answer
        ident = "toolu_01bothquestions"
        use = {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": ident, "name": "AskUserQuestion", "input": {"questions": asked}}]}}
        result = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident, "content": "User has answered your questions: ..."}]},
                  "toolUseResult": {"questions": asked, "answers": answers}}
        with open(self.path, "a") as handle:
            handle.write(json.dumps(use) + "\n" + json.dumps(result) + "\n")

    def test_the_second_grant_of_one_call_is_honored_after_the_first_was_used(self):
        self.ask_both()
        self.hook(self.STRONG)
        self.assertTrue(self.held("strong", "opus"))
        self.assertTrue(grants.take(self.folder, [("strong", "opus")]))   # the chain used the first
        self.hook(self.MAP)
        self.assertTrue(self.held("map", "automatic"))
        self.assertTrue(grants.take(self.folder, [("map", "automatic")]))

    def test_it_works_in_the_other_order_too(self):
        self.ask_both()
        self.hook(self.MAP)
        self.assertTrue(grants.take(self.folder, [("map", "automatic")]))
        self.hook(self.STRONG)
        self.assertTrue(self.held("strong", "opus"))

    def test_each_grant_of_the_call_is_still_used_once(self):
        self.ask_both()
        self.hook(self.STRONG)
        self.hook(self.MAP)
        self.assertTrue(grants.take(self.folder, [("strong", "opus"), ("map", "automatic")]))
        self.hook(self.STRONG)
        self.hook(self.MAP)
        self.assertFalse(self.held("strong", "opus"))      # the same answer grants the same kind no second time
        self.assertFalse(self.held("map", "automatic"))

    def test_the_bare_id_stays_spent_for_the_level_and_the_stronger_model_as_the_earlier_version_reads_it(self):
        self.assertTrue(grants.put(self.folder, "level", "everything", "toolu_a"))
        self.assertTrue(grants.take(self.folder, [("level", "everything")]))
        spent = grants._state()["spent"]
        self.assertIn("toolu_a", spent)           # what 1.6.0 reads
        self.assertIn("level-toolu_a", spent)
        self.assertFalse(grants.put(self.folder, "strong", "opus", "toolu_a"))   # a bare id spent: refused, as before
        self.assertFalse(grants.put(self.folder, "level", "everything", "toolu_a"))
        self.assertTrue(grants.put(self.folder, "map", "automatic", "toolu_a"))   # the map has no earlier reader

    def test_a_longest_answer_id_stays_spent_with_its_kind_in_front(self):
        longest = "a" * 80
        self.assertTrue(grants.put(self.folder, "map", "shadow", longest))
        self.assertTrue(grants.take(self.folder, [("map", "shadow")]))
        self.assertIn("map-" + longest, grants._state()["spent"])   # the form of the list still fits the longest id
        self.assertFalse(grants.put(self.folder, "map", "shadow", longest))


class RequestedTests(unittest.TestCase):
    def test_a_command_gives_the_kinds_it_names_and_leaves_the_others_out(self):
        table = (
            ("python3 /p/observer.py enable --model sonnet --level everything --yes", {"level": "everything"}),
            ("python3 /p/observer.py enable --level assistant --strong-model opus --yes", {"level": "assistant", "strong": "opus"}),
            ("python3 /p/observer.py enable --model sonnet --yes", {}),
            ("python3 /p/setup.py observer --level notes --yes", {"level": "notes"}),
            ("python3 /p/setup.py observer --strong opus --yes", {"strong": "opus"}),
            ("python3 /p/observer.py map-mode automatic --yes", {"map": "automatic"}),
            ("python3 /p/observer.py --repo octo-org/octo-repo map-mode shadow --yes", {"map": "shadow"}),
            ("python3 /p/observer.py map-mode off --yes", {}),
            ("python3 /p/observer.py map-mode everything --yes", {}),
            ("python3 /p/observer.py map-mode", None),
            ("python3 /p/observer.py map-mode automatic", None),
            ("python3 /p/observer.py status --yes", None),
            ("echo hello", None),
        )
        for command, expected in table:
            self.assertEqual(expected, grants.requested(command), command)

    def test_one_table_names_the_question_of_each_kind(self):
        self.assertEqual({"level": "observer-level", "strong": "paper-proposals", "map": "map-upkeep"}, grants.QUESTION_OF)
        for kind in grants.QUESTION_OF:
            text, labels = grants.canonical(kind)
            self.assertTrue(text.endswith(".") or "?" in text)
            self.assertIn("Later", labels)

    def test_map_mode_is_a_grant_row_and_a_session_may_run_it(self):
        self.assertEqual("grant", observer_hooks.COMMANDS["map-mode"])
        self.assertNotIn("map-mode", observer_hooks.ASKED)   # it has a setup question of its own, not an act's
        self.assertNotIn("map-mode", pretool_guard.ASKED)
        self.assertIsNone(pretool_guard.decide(json.dumps({"tool_name": "Bash", "tool_input": {
            "command": "python3 /p/skills/recortex/scripts/observer.py map-mode automatic --yes"}})))


class SwitchedLineTests(unittest.TestCase):
    """The line after a session switched automatic mode on: its date, the level's name and whether paper proposals are on,
    and again when the level or the stronger model changes."""

    def store(self):
        values = {}
        return mock.Mock(get_control=lambda key, default=None: values.get(key, default),
                         set_control=lambda key, value: values.__setitem__(key, value))

    def entry(self, **settings):
        settings = dict({"mode": "automatic", "level": "everything", "strong_model": ""}, **settings)
        return {"given": "session", "approved_at": 1794900000.0, "settings": settings}

    def test_it_names_the_level_and_the_stronger_model_and_is_said_again_when_either_changes(self):
        store = self.store()
        said = []
        for entry in (self.entry(), self.entry(), self.entry(strong_model="opus"), self.entry(strong_model="opus"),
                      self.entry(level="assistant", strong_model="opus"), self.entry(level="assistant", strong_model="opus")):
            with mock.patch.object(observer_hooks.observer_runtime, "stored_approval", return_value=entry):
                said.append(observer_hooks._switched(store))
        self.assertEqual("", said[1])
        self.assertEqual("", said[3])
        self.assertEqual("", said[5])
        self.assertIn("at the level Everything, with paper proposals off;", said[0])
        self.assertIn("at the level Everything, with paper proposals on;", said[2])
        self.assertIn("at the level Assistant, with paper proposals on;", said[4])
        self.assertTrue(all(line.startswith("recortex observer: automatic mode was switched on for this folder on 2026-")
                            for line in (said[0], said[2], said[4])))

    def test_a_folder_the_user_turned_on_in_their_terminal_says_nothing(self):
        with mock.patch.object(observer_hooks.observer_runtime, "stored_approval", return_value=dict(self.entry(), given="terminal")):
            self.assertEqual("", observer_hooks._switched(self.store()))


class InsideClaudeTests(unittest.TestCase):
    """The user's own terminal is outside every Claude Code session: CLAUDECODE in the environment, or a claude program
    among the parents, makes the process a session's, whatever terminal it has."""

    def terminal(self):
        tty = mock.MagicMock()
        tty.__enter__.return_value = tty
        return mock.patch.object(observer, "terminal", return_value=tty)

    def test_claudecode_in_the_environment_is_a_session(self):
        self.assertTrue(observer.inside_claude(environ={"CLAUDECODE": "1"}, parents=[]))
        self.assertFalse(observer.inside_claude(environ={}, parents=[]))
        self.assertFalse(observer.inside_claude(environ={"CLAUDECODE": ""}, parents=["bash"]))

    def test_a_claude_program_among_the_parents_is_a_session(self):
        self.assertTrue(observer.inside_claude(environ={}, parents=["python3", "zsh", "claude", "zsh", "tmux"]))
        self.assertFalse(observer.inside_claude(environ={}, parents=["python3", "zsh", "tmux", "sshd", "systemd"]))
        self.assertFalse(observer.inside_claude(environ={}, parents=["claude-code-docs", "claudette"]))   # another name

    def test_a_terminal_inside_a_session_is_refused_and_the_users_own_is_not(self):
        with self.terminal(), mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertFalse(observer.on_terminal(parents=[]))
        environment = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
        with self.terminal(), mock.patch.dict(os.environ, environment, clear=True):
            self.assertFalse(observer.on_terminal(parents=["bash", "claude"]))
            self.assertTrue(observer.on_terminal(parents=["bash", "tmux", "sshd"]))
            self.assertTrue(observer.on_terminal(parents=[]))

    def test_setup_py_takes_the_same_answer_on_a_terminal_inside_a_session_as_on_none(self):
        import setup as wizard
        folder = os.path.join(tempfile.gettempdir(), "no-such-folder-for-the-chain-pass")
        with self.terminal(), mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), \
                mock.patch.object(grants, "take", return_value=False) as take:
            self.assertIsNone(wizard.chain_pass(folder, "notes", None, [("level", "notes")]))   # a terminal in a session is none
            take.assert_called_once()
        environment = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
        with self.terminal(), mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(observer, "ancestry", return_value=["bash", "tmux"]), \
                mock.patch.object(grants, "take", return_value=False) as take, \
                mock.patch.object(grants, "start_pass", return_value="0" * 32):
            self.assertEqual("0" * 32, wizard.chain_pass(folder, "notes", None, [("level", "notes")]))   # the user's own
            take.assert_not_called()

    def test_the_ancestry_is_read_from_the_parents_and_is_bounded(self):
        names = observer.ancestry()
        self.assertIsInstance(names, list)
        self.assertTrue(all(isinstance(name, str) for name in names))
        self.assertLessEqual(len(names), 2 * observer.ANCESTORS)
        if os.path.isdir("/proc/%d" % os.getppid()):
            with open("/proc/%d/stat" % os.getppid()) as handle:
                stat = handle.read()
            self.assertEqual(stat[stat.index("(") + 1:stat.rindex(")")], names[0])   # the nearest parent first
        with mock.patch.object(observer.os, "getppid", return_value=1):
            self.assertEqual([], observer.ancestry())

    def test_the_commands_that_ask_for_the_users_terminal_use_it(self):
        import inspect
        source = inspect.getsource(observer)
        for place in ("answered=on_terminal()", "terminal() if on_terminal() else None", "on_terminal() or store.take_grant"):
            self.assertIn(place, source)
        self.assertEqual(3, source.count("terminal() if on_terminal() else None"))   # settle, release and disable
        import acts
        self.assertIn("observer.on_terminal()", inspect.getsource(acts.allowed))


class TableTests(unittest.TestCase):
    def test_enable_is_a_grant_row_and_the_hook_still_refuses_only_the_users_own_commands(self):
        self.assertEqual("grant", observer_hooks.COMMANDS["enable"])
        self.assertNotIn("enable", observer_hooks.ASKED)


if __name__ == "__main__":
    unittest.main()

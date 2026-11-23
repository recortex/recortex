"""The acts that need the user's yes (1.8.0): `acts.py` prints one question for each, the PreToolUse hook records a one-time grant
from the user's recorded answer to exactly that question, and the act takes the grant once or refuses and names the question.
Also the typed answer, the two diagnostics that pass when the holds cannot be read, and the guard of the main session's calls
against a subagent's. Offline: native transcripts in the system's temporary folder, no network."""

import contextlib
import io
import json
import os
import re
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "skills", "recortex", "scripts"))
import acts  # noqa: E402
import observer  # noqa: E402
import observer_grants as grants  # noqa: E402
import observer_holds  # noqa: E402
import observer_hooks  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import observer_sources  # noqa: E402
import observer_store  # noqa: E402
import pretool_guard  # noqa: E402
from observer_source_fixture import fixture_trusted_ancestors  # noqa: E402

REPO = "octo-org/octo-repo"
OTHER = "octo-org/octo-pages"
SCRIPTS = os.path.join(ROOT, "skills", "recortex", "scripts")


class ActsBox(unittest.TestCase):
    """A native transcript the observer's checks accept, a configured and trusted project, and the helpers: the user's answer
    to a question, the hook before a Bash call, and observer.py in this process with no terminal."""

    def setUp(self):
        import pathlib
        tmp = tempfile.TemporaryDirectory(prefix="observer-acts-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.base = os.path.realpath(tmp.name)
        metadata = fixture_trusted_ancestors(pathlib.Path(self.base))
        metadata.__enter__()
        self.addCleanup(metadata.__exit__, None, None, None)
        self.project = os.path.join(self.base, "project")
        os.mkdir(self.project, 0o700)
        with open(os.path.join(self.project, ".recortex.json"), "w") as handle:
            json.dump({"repo": REPO, "hub": 1, "profiles": ["software"]}, handle)
        patch = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": os.path.join(self.base, "state"), "HOME": os.path.join(self.base, "home"),
            "XDG_CACHE_HOME": os.path.join(self.base, "cache"), "XDG_CONFIG_HOME": os.path.join(self.base, "config"),
            "CLAUDE_CONFIG_DIR": os.path.join(self.base, "claude"), "CLAUDE_PROJECT_DIR": self.project,
            "RECORTEX_TRUSTED_REPOS": "%s,%s" % (REPO, OTHER)}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.path = os.path.join(observer_sources.project_directory(self.project), "session1.jsonl")
        os.makedirs(os.path.dirname(self.path), mode=0o700)
        for folder in (os.path.join(self.base, "claude"), os.path.join(self.base, "claude", "projects"),
                       os.path.dirname(self.path)):
            os.chmod(folder, 0o700)
        with open(self.path, "w"):
            pass
        os.chmod(self.path, 0o600)
        self.folder = runtime.folder_key(self.project)
        self.count = 0
        patch = mock.patch.object(observer, "terminal", return_value=None)   # no terminal: a session's own call
        patch.start()
        self.addCleanup(patch.stop)

    # ---- the user's answer, the hook, observer.py

    def answer(self, kind, values, label, text=None, labels=None):
        """The transcript records the user's answer `label` to the question of `kind` for `values`."""
        act = acts.ACTS[kind]
        text = text if text is not None else act.text(values)
        labels = labels if labels is not None else act.labels(values)
        self.count += 1
        ident = "toolu_01acts%d" % self.count
        asked = {"question": text, "header": act.header, "multiSelect": False,
                 "options": [{"label": item, "description": item} for item in labels]}
        use = {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": ident, "name": "AskUserQuestion", "input": {"questions": [asked]}}]}}
        result = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident,
             "content": 'User has answered your questions: "%s"="%s". You can now continue with the user\'s answers in mind.'
                        % (text, label)}]},
                  "toolUseResult": {"questions": [asked], "answers": {text: label}}}
        with open(self.path, "a") as handle:
            handle.write(json.dumps(use) + "\n" + json.dumps(result) + "\n")

    def hook(self, command, **extra):
        data = dict({"hook_event_name": "PreToolUse", "session_id": "session1", "transcript_path": self.path,
                     "cwd": self.project, "tool_name": "Bash", "tool_input": {"command": command}}, **extra)
        out = pretool_guard.decide(json.dumps(data))
        return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None

    def run_observer(self, *args):
        """observer.py in this process: (exit code, the last JSON line it printed)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = observer.main(list(args))
        lines = [line for line in out.getvalue().splitlines() if line.startswith("{")]
        return code, json.loads(lines[-1]) if lines else {}

    def held(self, kind, value):
        return grants.held(self.folder, [(kind, value)])

    def command(self, *words):
        return "python3 %s %s" % (os.path.join(SCRIPTS, words[0]), " ".join(shlex.quote(w) for w in words[1:]))

    # ---- the state an act is about

    def approve(self, repo=REPO, **settings):
        options = {"enabled": True, "level": "everything", "model": "haiku", "mode": "shadow",
                   "publisher_id": runtime.publisher_id(create=True)}
        options.update(settings)
        runtime.approve(repo, {"observer": options}, self.project)

    def hold(self, repo=REPO, action="git-push"):
        source = {"source_id": "source1", "role": "user", "direct_user": True, "text": "/recortex hold " + action}
        made = observer_holds.make_hold(source, repo, action)
        with observer_store.Store(repo) as store:
            store.update_holds(lambda holds: holds + [made])
        return made["id"]

    def pause(self, scope="capture"):
        self.assertEqual(0, self.run_observer("pause", scope)[0])

    def refusal_names(self, result, kind):
        self.assertEqual(kind + "_unanswered", result["code"])
        self.assertIn("acts.py ask " + kind, result["note"])
        self.assertNotIn("terminal", result["note"])


class MechanismTests(ActsBox):
    """The mechanism, shown with `resume`, the simplest act: a pause the user made, lifted on their answer."""

    VALUES = {"repo": REPO, "scope": "capture"}

    def paused(self):
        with observer_store.Store(REPO, create=False, readonly=True) as store:
            return bool(store.get_control("capture_paused", False))

    def test_without_an_answer_the_act_changes_nothing_and_names_its_question(self):
        self.pause()
        code, result = self.run_observer("resume", "capture")
        self.assertEqual(1, code)
        self.refusal_names(result, "resume")
        self.assertTrue(self.paused())

    def test_the_yes_label_to_the_whole_question_lets_the_command_through_once(self):
        self.pause()
        self.answer("resume", self.VALUES, "Resume the observer")
        self.assertIsNone(self.hook(self.command("observer.py", "resume", "capture")))
        self.assertTrue(self.held("resume", acts.ACTS["resume"].value(self.VALUES)))
        self.assertEqual(0, self.run_observer("resume", "capture")[0])
        self.assertFalse(self.paused())
        self.pause()
        code, result = self.run_observer("resume", "capture")   # the grant was spent
        self.assertEqual(1, code)
        self.refusal_names(result, "resume")
        self.assertTrue(self.paused())

    def test_a_question_reworded_by_one_word_or_with_an_option_added_or_another_label_grants_nothing(self):
        self.pause()
        text = acts.ACTS["resume"].text(self.VALUES)
        cases = (("a word changed", dict(text=text.replace("goes on", "carries on")), "Resume the observer"),
                 ("an option added", dict(labels=["Resume the observer", "Keep it paused", "Ask me later"]), "Resume the observer"),
                 ("another label", {}, "Keep it paused"),
                 ("another label of no meaning", dict(labels=["Resume the observer", "Not that"]), "Not that"))
        for name, kwargs, label in cases:
            self.answer("resume", self.VALUES, label, **kwargs)
            self.hook(self.command("observer.py", "resume", "capture"))
            self.assertFalse(self.held("resume", acts.ACTS["resume"].value(self.VALUES)), name)
        self.assertEqual(1, self.run_observer("resume", "capture")[0])
        self.assertTrue(self.paused())

    def test_a_command_with_other_values_is_not_covered_by_the_answer(self):
        self.pause("capture")
        self.pause("publication")
        self.answer("resume", self.VALUES, "Resume the observer")
        self.hook(self.command("observer.py", "resume", "publication"))
        self.hook(self.command("observer.py", "--repo", OTHER, "resume", "capture"))
        self.assertFalse(self.held("resume", acts.ACTS["resume"].value(self.VALUES)))
        self.assertFalse(self.held("resume", acts.ACTS["resume"].value({"repo": REPO, "scope": "publication"})))
        code, result = self.run_observer("resume", "publication")
        self.assertEqual(1, code)
        self.refusal_names(result, "resume")

    def test_a_subagents_call_is_denied_and_records_no_grant_even_with_the_answer_in_the_main_transcript(self):
        self.pause()
        self.answer("resume", self.VALUES, "Resume the observer")
        reason = self.hook(self.command("observer.py", "resume", "capture"), agent_id="agent-1", agent_type="general-purpose")
        self.assertEqual(pretool_guard.SUBAGENT_DENIAL, reason)
        self.assertFalse(self.held("resume", acts.ACTS["resume"].value(self.VALUES)))
        self.assertIsNone(self.hook(self.command("observer.py", "status"), agent_id="agent-1"))   # only the acts are denied

    def test_a_refusal_when_the_transcript_cannot_be_read_names_the_trust_question(self):
        self.pause()
        with mock.patch.object(grants, "unread_reason", return_value="source-owner"):
            code, result = self.run_observer("resume", "capture")
        self.assertEqual((1, "resume_answer_unreadable"), (code, result["code"]))
        self.assertIn("acts.py ask trust", result["note"])

    def test_the_users_own_terminal_needs_no_answer_and_types_its_word_as_before(self):
        self.pause()
        tty = mock.MagicMock()
        tty.__enter__.return_value = tty
        tty.ask.return_value = "resume"
        with mock.patch.object(observer, "terminal", return_value=tty):
            self.assertEqual(0, self.run_observer("resume", "capture")[0])
        self.assertFalse(self.paused())
        self.pause()
        tty.ask.return_value = "no"
        with mock.patch.object(observer, "terminal", return_value=tty):
            code, result = self.run_observer("resume", "capture")
        self.assertEqual((1, "not-confirmed"), (code, result["code"]))
        self.assertTrue(self.paused())

    def test_a_refusal_for_another_reason_spends_no_grant(self):
        self.answer("resume", self.VALUES, "Resume the observer")
        self.hook(self.command("observer.py", "resume", "capture"))
        self.assertTrue(self.held("resume", acts.ACTS["resume"].value(self.VALUES)))
        self.run_observer("resume", "capture")   # nothing is paused: nothing to resume
        self.assertTrue(self.held("resume", acts.ACTS["resume"].value(self.VALUES)))

    def test_ask_prints_the_question_its_options_and_the_command_of_each(self):
        self.pause()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, acts.main(["ask", "resume", "--scope", "capture", "--json"]))
        body = json.loads(out.getvalue())
        self.assertEqual(acts.ACTS["resume"].text(self.VALUES), body["question"])
        self.assertEqual(["Resume the observer", "Keep it paused"], [o["label"] for o in body["options"]])
        self.assertEqual("choice", body["form"])
        self.assertLessEqual(len(body["header"]), 12)
        self.assertEqual("python3 %s resume capture" % os.path.join(SCRIPTS, "observer.py"), body["options"][0]["run"][0])
        self.assertEqual([], body["options"][1]["run"])
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            acts.main(["ask", "resume", "--scope", "capture"])
        self.assertIn("question resume", text.getvalue())
        self.assertIn("- Keep it paused: ", text.getvalue())
        self.assertIn("Run: nothing", text.getvalue())

    def test_ask_prints_one_fixed_line_and_no_question_when_the_act_is_not_possible(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(1, acts.main(["ask", "resume", "--scope", "capture"]))   # nothing is paused
        self.assertEqual("acts: nothing is paused in that scope, so there is nothing to resume.\n", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(1, acts.main(["ask", "release", "not-an-id"]))
        self.assertNotIn("question", out.getvalue())


if __name__ == "__main__":
    unittest.main()

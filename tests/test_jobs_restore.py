"""What 1.7.0 restored of 1.5.1 and what 1.6.0 had weakened (1.7.0): the setup questions and the items that wait for the user are
asked each as one multiple-choice question, the status is told in its old order, the three fixed lines of the observer are the
1.5.1 words again, and the dashboard's "In short" opens the page again. Nothing here reaches GitHub; the scripts run against
tests/fake_gh.py."""

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import brief  # noqa: E402
import guidance  # noqa: E402
import hooks  # noqa: E402
import observer_hooks  # noqa: E402
import observer_waiting  # noqa: E402
import recortex  # noqa: E402

SKILL = os.path.join(ROOT, "skills", "recortex")
# The 1.5.1 words (commit cf02936), written out here so that a change of the code's text cannot move the pin.
WAITING_151 = ("recortex observer: %s for the user's answer. Load the recortex skill first, run its observer.py pending "
               "--waiting, and ask each as one multiple-choice question from the template it prints. Do not offer them in "
               "prose.")
REMINDER_151 = ("recortex observer: %s for the user's answer. Before this reply ends, load the recortex skill, run its "
                "observer.py pending --waiting and ask each as a multiple-choice question.")
NOTE_151_START = ("Ask the user about each item with the multiple-choice question tool, one question per item: the whole "
                  "question as listed, word for word, with exactly its options in that order; an answer to any other wording "
                  "counts for nothing.")


def read(*parts):
    return recortex.read_text(os.path.join(ROOT, *parts))


class InstructionTests(unittest.TestCase):
    """The files that tell the session when to ask."""

    def setUp(self):
        self.skill = read("skills", "recortex", "SKILL.md")

    def section(self, title):
        return self.skill.split("### " + title, 1)[1].split("\n### ", 1)[0]

    def test_step_six_and_what_waits_ask_each_item_as_one_multiple_choice_question(self):
        start = self.skill.split("## Session protocol", 1)[1].split("\n## ", 1)[0]
        step6 = next(line for line in start.splitlines() if line.startswith("6. "))
        self.assertIn("ask each item as one multiple-choice question", step6)
        self.assertIn("Do the same at the end of a work stretch.", step6)
        waits = self.section("What waits for the user")
        self.assertIn("Ask each item as one multiple-choice question: the whole question it prints, word for word", waits)
        self.assertIn("Do not offer them in prose.", waits)
        self.assertIn("You may add one line on why an item matters, never in place of its question.", waits)
        self.assertIn("a map change (`Project Map <short>: <line>. Apply this change?`, options Apply change, Not now, Drop it)", waits)
        self.assertIn("`observer.py act SHORT` for Open it, Close it and Apply change", waits)
        for gone in ("surface the one item", "the most relevant item", "When the user wants to act", "natural pause"):
            self.assertNotIn(gone, self.skill, gone)

    def test_step_five_keeps_the_one_line_and_guidance_never_replaces_the_questions(self):
        start = self.skill.split("## Session protocol", 1)[1].split("\n## ", 1)[0]
        step5 = next(line for line in start.splitlines() if line.startswith("5. "))
        self.assertTrue(step5.startswith("5. Only then act. State in one line what was read."), step5)
        self.assertIn("what changed since the last handoff", step5)
        self.assertIn("never replaces or delays the setup questions or what waits for the user", step5)

    def test_the_commands_intro_and_the_dashboard_say_what_they_said_in_1_5_1(self):
        self.assertIn("They are `/recortex:` commands, and nothing promises a bare `/record`.", self.skill)
        opened = self.section("Open the project dashboard")
        self.assertIn("The page is for a person. Do not read `mind.html` yourself: to answer a question about history, use "
                      "`index.py query recall`", opened)

    def test_the_status_command_names_the_parts_in_order_and_asks_each_waiting_item(self):
        body = read("commands", "status.md").split("---\n", 2)[2]
        marks = ["\"In short\" paragraphs", "open judgment about a changed decision", "open tasks and their next steps",
                 "what is due in the paper's text", "what waits for a yes"]
        places = [body.index(mark) for mark in marks]
        self.assertEqual(sorted(places), places)
        self.assertIn("ask each item it lists as one multiple-choice question", body)
        self.assertIn("status.py --sync", body)
        self.assertIn("GitHub could not be read", body)
        self.assertIn("shapes the wording, never what is asked", body)

    def test_guidance_scopes_its_question_and_says_it_does_not_move_the_plugins_own(self):
        text = read("skills", "recortex", "references", "guidance.md")
        self.assertTrue(text.split("\n", 2)[2].startswith("This page shapes handoffs and advice. It does not change when the "
                                                           "plugin's own questions are asked"), text[:300])
        self.assertIn("In your own advice, ask one question only if the answer would change the next move.", text)
        self.assertNotIn("Ask one question only if", text)

    def test_the_end_of_a_stretch_asks_what_waits_and_the_list_changed_by_hand(self):
        text = read("skills", "recortex", "references", "session-protocol.md")
        step4 = next(line for line in text.splitlines() if line.startswith("4. With the observer on"))
        self.assertIn("as at session start (step 6 above)", step4)
        self.assertIn("\"Record it\" (`tasks.py mark`)", step4)
        self.assertIn("\"Put it back\"", step4)

    def test_the_reference_names_five_kinds_and_quotes_the_fixed_line_as_the_code_has_it(self):
        text = read("skills", "recortex", "references", "observer.md")
        self.assertIn("Five kinds of item wait for you. The session asks each as one multiple-choice question", text)
        self.assertNotIn("Four kinds of item wait for you", text)
        self.assertIn('("%s")' % (WAITING_151 % "N items wait"), text)
        self.assertIn("A reminder follows at the next prompts while items wait", text)
        self.assertIn("At the level Assistant the question about a task comes with the next prompt, not at a pause.", text)
        self.assertNotIn("natural pause", text)

    def test_the_mind_reference_says_resume_work_opens_with_in_short(self):
        text = read("skills", "recortex", "references", "mind.md")
        self.assertIn('"Resume work" opens with the newest "In short"', text)


class FixedStringTests(unittest.TestCase):
    def test_the_three_fixed_lines_are_the_1_5_1_words(self):
        self.assertEqual(WAITING_151, observer_hooks.WAITING)
        self.assertEqual(REMINDER_151, observer_hooks.REMINDER)
        self.assertTrue(observer_waiting.NOTE.startswith(NOTE_151_START), observer_waiting.NOTE)
        self.assertIn("After each answer, run the command listed for it, alone in its command: yes, later or no.",
                      " ".join(observer_waiting.NOTE.split()))

    def test_the_comment_above_the_session_start_line_says_it_asks_each_item(self):
        source = read("skills", "recortex", "scripts", "observer_hooks.py")
        self.assertIn("and asks each item from the template `observer.py pending --waiting` prints.", source)
        self.assertNotIn("offers a relevant item", source)

    def test_no_prompt_text_says_to_wait_for_a_pause(self):
        for text in (observer_hooks.WAITING, observer_hooks.REMINDER, observer_waiting.NOTE):
            self.assertNotIn("At a pause", text)
            self.assertNotIn("natural pause", text)


class StatusOrderTests(ts.MindSandbox):
    """status.py tells In short, then the open judgment about a changed decision, then the tasks."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from examples.dashboard import preview
        with mock.patch.object(sys, "path", [str(Path(ROOT))] + sys.path):
            preview.add_decision_change(self, "software")
        self.ready()
        db = sqlite3.connect(self.index_file())
        try:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('resume', ?)",
                       ("**In short:** The release gate moved to the amber corpus.\n\n**Last updated:** now\n",))
            db.commit()
        finally:
            db.close()

    def test_the_judgment_comes_after_in_short_and_before_the_tasks(self):
        done = self.tool("status.py", gh="false")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        where = {name: next((i for i, line in enumerate(lines) if line.startswith(name)), None)
                 for name in ("In short", "Open judgment:", "Tasks", "Text", "Waiting")}
        self.assertTrue(all(value is not None for value in where.values()), done.stdout)
        self.assertEqual(sorted(where.values()), list(where.values()), where)

    def test_the_docstring_lists_the_same_order(self):
        import status
        order = [line.split()[0] for line in status.__doc__.splitlines() if re.match(r"  (In short|Judgment|Tasks|Text|Waiting) ", line)]
        self.assertEqual(["In", "Judgment", "Tasks", "Text", "Waiting"], order)


class PageOrderTests(unittest.TestCase):
    def test_in_short_is_written_before_the_pause_in_the_view_and_the_summary_fold_is_gone(self):
        source = read("skills", "recortex", "scripts", "mind.py")
        view = source.split("    def now_view(self):", 1)[1].split("    def project_map_view", 1)[0]
        self.assertLess(view.index("self.summary_block("), view.index("We last paused"))
        self.assertNotIn('"Project summary"', view)


class UpdatedProjectTests(unittest.TestCase):
    """An update does not stop asking: a project whose machine holds an observer approval of 1.4.0 (no level), no status line
    and no answers file gets the setup line, the return pointer and the handoff hint in one session-start payload, and the
    reminder on the second prompt."""

    def setUp(self):
        self.case = ts.MindSandbox()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        with mock.patch.object(sys, "path", [str(Path(ROOT))] + sys.path):
            from examples.dashboard import preview
            preview.add_decision_change(self.case, "software")
        self.case.ready()
        self.case.in_process(extra={"CLAUDE_PROJECT_DIR": self.case.project, "RECORTEX_NOTICE": None,
                                    "CLAUDE_CONFIG_DIR": os.path.join(self.case.dir, "claude"),
                                    "RECORTEX_SETUP_QUESTIONS": None})
        self.config = recortex.load_config()
        self.repo, self.hub = self.config["repo"], self.config["hub"]
        import storage
        folder = storage.folder(storage.state_top(for_key=True))
        path = os.path.join(folder, "observer-approvals.json")
        root = os.path.realpath(self.case.project)
        entry = {"repo": self.repo.lower(), "root": root, "settings": {"enabled": True, "mode": "shadow", "model": "sonnet"}}
        storage.write_file(path, json.dumps({"form": 1, "folders": {root: entry}}))

    def produce(self, event, session):
        with mock.patch.object(hooks, "pointer_seconds", return_value=30):
            return hooks.produce(event, {"session_id": session}, 10, self.config, self.repo, self.hub)

    def test_session_start_carries_the_pointer_the_hint_and_the_question_line_with_its_count(self):
        guidance.change(self.repo, "set", "short")
        text = self.produce("session-start", "s1")
        lines = [line for line in text.splitlines() if re.fullmatch(re.escape(brief.SETUP_QUESTIONS).replace("%d", "[1-9]").replace("%s", "s?"), line)]
        self.assertEqual(1, len(lines), text)
        count = int(re.search(r"([0-9]+) setup question", lines[0]).group(1))
        self.assertGreaterEqual(count, 2)   # the status line and the observer's level at least
        self.assertIn("Open judgment:", text)
        self.assertIn(guidance.HINTS["short"], text)
        positions = [text.index(part) for part in (guidance.HINTS["short"], "Open judgment:", lines[0])]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("run its setup.py questions and ask what it lists as one multiple-choice batch", lines[0])

    def test_the_second_prompt_carries_the_reminder_with_the_same_count(self):
        start = self.produce("session-start", "s2")
        count = int(re.search(r"([0-9]+) setup question", start).group(1))
        first = self.produce("prompt-submit", "s2")
        self.assertNotIn("setup.py questions has not run in this session", first)
        second = self.produce("prompt-submit", "s2")
        self.assertIn(brief.SETUP_REMINDER % (count, "s" if count != 1 else ""), second)


if __name__ == "__main__":
    unittest.main()

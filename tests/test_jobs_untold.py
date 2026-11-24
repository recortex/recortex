"""Text from an account that is not allowed never leaves the task rows (1.5.0): an untold title, an untold steps
memory and an untold marking memory are in no output that reads `index.tasks`: the two query forms, `tasks.py status`,
the mind page and the session hook. Nothing here reaches GitHub; the scripts run against tests/fake_gh.py."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_tasks as tt  # noqa: E402
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import recortex  # noqa: E402

TITLE = "MARKUNTOLDTITLE"
STEP = "MARKUNTOLDSTEP"
PLAIN = "MARKUNTOLDPLAIN"
GHOST = "ghost"   # an account that is not in the trusted line


class UntoldBox(tt.TaskBox):
    """The task record of TaskBox, with the title of task #7 renamed by an account that is not allowed, a second steps
    memory of that task by such an account, and a result by such an account that marks a step of the told memory."""

    def setUp(self):
        super().setUp()
        data = self.repo()
        data["issues"]["7"]["timeline"] = [{"event": "renamed", "created_at": "2026-04-11T00:30:00Z",
                                            "actor": {"login": GHOST}, "rename": {"from": "a", "to": TITLE}}]
        data["issues"]["7"]["title"] = TITLE
        data["issues"]["7"]["updated_at"] = "2026-04-11T00:30:00Z"
        self.save(data)
        steps = recortex.read_text(os.path.join(tt.CHAIN, "6-steps.md"))
        steps = steps.replace("2026-03-12T11:00-08:00", "2026-03-13T11:00-08:00").replace(
            "1. Carry the quote state across physical lines\n2. Rerun the conformance set",
            "1. %s\n2. %s two" % (STEP, STEP))
        self.put(9120, 7, steps, user=GHOST)
        result = recortex.read_text(os.path.join(tt.CHAIN, "7-result.md"))
        result = result.replace("done=9108:1", "done=9108:2").replace(
            "the reader now carries an open quote from one line to the next, and 9 of the 12 quoted-newline files pass",
            PLAIN)
        self.put(9121, 5, result, user=GHOST)
        self.ready()

    def outputs(self):
        """Everything that reads the rows of index.tasks, as one dict of name to text."""
        out = {}
        for name, args in (("query tasks --json", ("index.py", "query", "tasks", "--json")),
                           ("query tasks", ("index.py", "query", "tasks")),
                           ("tasks status", ("tasks.py", "status")),
                           ("tasks status --json", ("tasks.py", "status", "--json"))):
            done = self.tool(*args)
            self.assertEqual(0, done.returncode, name + ": " + done.stdout + done.stderr)
            out[name] = done.stdout
        built = self.tool("mind.py", gh="false")
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        # the reader shows what is not told, shut under its mark: the page's other views must not
        out["mind page"] = ts.outside_reader(recortex.read_text(os.path.join(self.data, "private", self.slug(), "mind.html")))
        for event in ("SessionStart", "UserPromptSubmit"):
            done = self.hook(event)
            out["hook " + event] = done.stdout + done.stderr
        return out


class UntoldTaskTextTests(UntoldBox):
    def test_the_row_holds_no_title_that_is_not_told(self):
        task = self.task_at(7)
        self.assertFalse(task["title_told"])
        self.assertIsNone(task["title"])
        self.assertNotIn(TITLE, json.dumps(self.report()))

    def test_no_byte_of_an_untold_title_step_or_marking_memory_is_in_any_output(self):
        for name, text in self.outputs().items():
            for planted in (TITLE, STEP, PLAIN):
                self.assertNotIn(planted, text, "%s leaks %s" % (name, planted))

    def test_the_told_steps_and_title_are_still_there_without_the_untold_ones(self):
        task = self.task_at(7)
        self.assertEqual(["Carry the quote state across physical lines", "Rerun the conformance set"],
                         [step["text"] for step in task["steps"]])
        self.assertTrue(all(step["by"] is None or PLAIN not in json.dumps(step["by"]) for step in task["steps"]))

    def test_a_title_that_is_told_is_still_given(self):
        data = self.repo()
        data["issues"]["7"]["timeline"] = []
        self.save(data)
        data = self.repo()
        data["issues"]["7"]["updated_at"] = self.tick(data)
        self.save(data)
        self.ready()
        task = self.task_at(7)
        self.assertTrue(task["title_told"])
        self.assertEqual(TITLE, task["title"])


if __name__ == "__main__":
    unittest.main()

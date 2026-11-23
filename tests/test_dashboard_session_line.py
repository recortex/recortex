"""The line from the dashboard to the session (1.7.0): when the user records something in the dashboard (a completed step, a map
edit, a review verdict, an applied edit), a session that began before it is told once, on its next prompt, in one fixed line
that says to read it. The notes are small and strict, and a note that cannot be written never fails the action. No model runs;
the scripts run against tests/fake_gh.py."""

import json
import os
import re
import stat
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_tasks as tj  # noqa: E402
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import dashboard_action as action  # noqa: E402
import dashboard_notes as notes  # noqa: E402
import decision_review_io as review_io  # noqa: E402
import hooks  # noqa: E402
import recortex  # noqa: E402
import storage  # noqa: E402

REPO = ts.REPO
WORDS = re.compile(r"recortex: since your last prompt the user recorded in the dashboard: .*run status\.py --sync and read them before "
                   r"you go on\.")


def note(at, kind="step", issue=7):
    return {"at": at, "kind": kind, "issue": issue}


class Box(unittest.TestCase):
    def setUp(self):
        self.case = ts.MindSandbox()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.ready()   # the index is built, so that a session start prints the brief and not the building line
        self.case.in_process(extra={"CLAUDE_PROJECT_DIR": self.case.project, "RECORTEX_NOTICE": None})
        self.repo = REPO

    def path(self):
        return os.path.join(storage.folder(storage.state_top(), storage.repo_folder(self.repo.lower())), notes.FILE)

    def put(self, value, raw=None, mode=0o600):
        folder = storage.folder(storage.state_top(), storage.repo_folder(self.repo.lower()))
        path = os.path.join(folder, notes.FILE)
        if os.path.lexists(path):
            os.unlink(path)
        Path(path).write_text(raw if raw is not None else json.dumps(value))
        os.chmod(path, mode)
        return path


class NoteFileTests(Box):
    def test_add_keeps_the_note_in_a_private_file_beside_the_prompt_state(self):
        self.assertTrue(notes.add(self.repo, "step", 12, now=1000))
        info = os.lstat(self.path())
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        self.assertEqual({"form": 1, "notes": [note(1000, "step", 12)]}, json.loads(Path(self.path()).read_text()))
        self.assertEqual(os.path.dirname(self.path()), os.path.dirname(hooks.state_path(self.repo)))

    def test_it_keeps_the_newest_two_hundred_none_older_than_seven_days_and_under_the_size_cap(self):
        for number in range(230):
            notes.add(self.repo, "map", 4, now=1000.0 + number)
        kept = json.loads(Path(self.path()).read_text())["notes"]
        self.assertEqual(200, len(kept))
        self.assertEqual(1030.0, kept[0]["at"])
        self.assertLess(os.path.getsize(self.path()), notes.CAP_BYTES)
        notes.add(self.repo, "edit", 9, now=1000.0 + 8 * 86400)
        self.assertEqual([("edit", 9)], [(n["kind"], n["issue"]) for n in json.loads(Path(self.path()).read_text())["notes"]])

    def test_a_kind_off_the_list_or_a_bad_issue_or_time_writes_nothing(self):
        for kind, issue, now in (("other", 7, 5), (None, 7, 5), ("step", 0, 5), ("step", -1, 5), ("step", True, 5),
                                 ("step", "7", 5), ("step", 10 ** 18 + 1, 5), ("step", 7, float("nan")),
                                 ("step", 7, 10 ** 12), ("step", 7, "5")):
            self.assertFalse(notes.add(self.repo, kind, issue, now=now), (kind, issue, now))
        self.assertFalse(os.path.exists(self.path()))

    def test_a_note_that_cannot_be_written_is_false_and_never_raises(self):
        with mock.patch.object(storage, "write_file", side_effect=OSError("disk full")):
            self.assertFalse(notes.add(self.repo, "step", 7, now=5))
        self.put(None, raw="{")
        os.chmod(self.path(), 0o644)   # not private: refused, never replaced
        self.assertFalse(notes.add(self.repo, "step", 7, now=5))
        self.assertEqual("{", Path(self.path()).read_text())

    def machine_row(self):
        config = recortex.load_config()
        found = [line for line in hooks.machine_summary(self.repo, config, [])
                 if line.strip().startswith("line about the dashboard on prompts:")]
        return found[0].split(":", 1)[1].strip()

    def test_a_note_that_could_not_be_kept_is_marked_for_the_doctor_and_a_later_note_clears_it(self):
        self.assertEqual("on", self.machine_row())
        real = storage.write_file

        def full(path, text, *args, **kwargs):   # the notes file cannot be written; the small mark beside it can
            if os.path.basename(path) == notes.FILE:
                raise OSError("disk full")
            return real(path, text, *args, **kwargs)
        with mock.patch.object(storage, "write_file", side_effect=full):
            self.assertFalse(notes.add(self.repo, "step", 7, now=5))
        self.assertIsNotNone(notes.failed(self.repo))
        self.assertRegex(self.machine_row(), r"^on, but a note of what you recorded in the dashboard could not be kept on "
                                             r"[0-9]{4}-[0-9]{2}-[0-9]{2}, so a session was not told of it$")
        self.assertTrue(notes.add(self.repo, "step", 7, now=6))
        self.assertIsNone(notes.failed(self.repo))
        self.assertEqual("on", self.machine_row())

    def test_a_bad_argument_is_not_a_failure_of_this_machine_and_the_mark_is_read_strictly(self):
        self.assertFalse(notes.add(self.repo, "other", 7, now=5))
        self.assertIsNone(notes.failed(self.repo))
        folder = storage.folder(storage.state_top(), storage.repo_folder(self.repo.lower()))
        for text in ('{"at": "x"}', '{"at": 5, "more": 1}', "[1]", "{", '{"at": true}'):
            Path(os.path.join(folder, notes.FAILED_FILE)).write_text(text)
            self.assertIsNone(notes.failed(self.repo), text)
        self.assertEqual("on", self.machine_row())

    def test_since_reads_strictly_and_a_file_that_does_not_fit_gives_nothing(self):
        self.put({"form": 1, "notes": [note(5), note(10, "map", 4), note(20, "verdict", 4)]})
        self.assertEqual([10, 20], [n["at"] for n in notes.since(self.repo, 5)])
        self.assertEqual([], notes.since(self.repo, 20))
        bad = ({"form": 2, "notes": []}, {"form": 1, "notes": [note(5, "other")]}, {"form": 1, "notes": [note(5, "step", "7")]},
               {"form": 1, "notes": [note(5, "step", 10 ** 19)]}, {"form": 1, "notes": [note(float("inf"))]},
               {"form": 1, "notes": [dict(note(5), more=1)]}, {"form": 1, "notes": [note(5)], "extra": 1},
               {"form": 1, "notes": [note(5)] * 201}, {"form": 1, "notes": "x"}, [1], {"form": 1, "notes": [note(-1)]},
               {"form": 1, "notes": [note(5), note(6, "step", 0)]})
        for value in bad:
            self.put(value)
            self.assertEqual([], notes.since(self.repo, 0), value)
        self.put(None, raw="x" * (notes.CAP_BYTES + 1))
        self.assertEqual([], notes.since(self.repo, 0))
        self.put({"form": 1, "notes": [note(9)]}, mode=0o644)
        self.assertEqual([], notes.since(self.repo, 0))

    def test_since_creates_nothing(self):
        before = sorted(os.listdir(storage.folder(storage.state_top(), storage.repo_folder(self.repo.lower()))))
        self.assertEqual([], notes.since(self.repo, 0))
        self.assertEqual([], notes.since("octo-org/other", 0))
        self.assertFalse(os.path.exists(self.path()))
        self.assertEqual(before, sorted(os.listdir(storage.folder(storage.state_top(), storage.repo_folder(self.repo.lower())))))
        self.assertEqual([], notes.since(self.repo, "later"))
        self.assertEqual([], notes.since(self.repo, float("nan")))


class LineTests(unittest.TestCase):
    def test_the_counts_the_plurals_and_the_issue_numbers(self):
        text = notes.line([note(1, "step", 12), note(2, "step", 15), note(3, "map", 4), note(4, "verdict", 4), note(5, "edit", 12)])
        self.assertEqual("recortex: since your last prompt the user recorded in the dashboard: 2 completed steps, 1 map edit, "
                         "1 review verdict, 1 applied edit (issues #4, #12, #15). They are on GitHub and may not be in what you "
                         "read: run status.py --sync and read them before you go on.", text)
        self.assertRegex(text, WORDS)

    def test_one_of_each_is_singular_and_one_issue_is_named_in_the_singular(self):
        text = notes.line([note(1, "step", 12)])
        self.assertIn("1 completed step (issue #12).", text)
        self.assertNotIn("steps", text)
        self.assertIn("2 map edits", notes.line([note(1, "map", 4), note(2, "map", 4)]))
        self.assertIn("(issue #4)", notes.line([note(1, "map", 4), note(2, "map", 4)]))

    def test_only_the_kinds_that_occurred_and_in_the_fixed_order(self):
        text = notes.line([note(1, "edit", 3), note(2, "step", 1)])
        self.assertLess(text.index("completed step"), text.index("applied edit"))
        self.assertNotIn("map edit", text)
        self.assertNotIn("review verdict", text)

    def test_at_most_six_distinct_issues_in_rising_order_then_and_more(self):
        many = [note(n, "step", 100 - n) for n in range(1, 10)]
        text = notes.line(many)
        self.assertIn("(issues #91, #92, #93, #94, #95, #96, and more)", text)
        self.assertEqual(text.count("#"), 6)
        six = notes.line([note(n, "step", n) for n in range(1, 7)])
        self.assertIn("(issues #1, #2, #3, #4, #5, #6)", six)
        self.assertNotIn("and more", six)

    def test_the_line_holds_its_cap_and_leaves_the_numbers_out_before_the_counts(self):
        worst = [note(n, kind, 10 ** 17 + n) for n, kind in enumerate(notes.KINDS * 50, 1)]
        text = notes.line(worst)
        self.assertLessEqual(len(text), notes.CAP)
        self.assertRegex(text, WORDS)
        self.assertIn("50 completed steps, 50 map edits, 50 review verdicts, 50 applied edits", text)
        self.assertLessEqual(len(notes.line([note(1, "step", 5)])), notes.CAP)

    def test_nothing_in_the_line_is_text_of_the_record_and_a_bad_note_gives_none(self):
        self.assertEqual("", notes.line([]))
        self.assertEqual("", notes.line([note(1, "<b>")]))
        self.assertEqual("", notes.line([{"at": 1, "kind": "step", "issue": "x"}]))
        self.assertEqual("", notes.line(None))


class WriterTests(tj.TaskBox):
    """Each of the four events writes its note, and a replay writes none."""

    def setUp(self):
        super().setUp()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})
        self.actions = action.Actions()

    def told(self):
        return [(n["kind"], n["issue"]) for n in notes.since(REPO, 0)]

    def test_a_completed_step_writes_one_note_for_its_issue_and_a_replay_none(self):
        preview = self.actions.preview("7:9108:2", "I reran the conformance set and all twelve files passed.")
        self.assertEqual([], self.told())   # a preview records nothing
        result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual("recorded", result["state"])
        self.assertEqual([("step", 7)], self.told())
        self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual([("step", 7)], self.told())

    def test_a_note_that_cannot_be_written_does_not_fail_the_action(self):
        preview = self.actions.preview("7:9108:2", "I reran the conformance set and all twelve files passed.")
        real = storage.write_file

        def refuse(path, text):
            if os.path.basename(path) == notes.FILE:
                raise OSError("disk full")
            return real(path, text)
        with mock.patch.object(storage, "write_file", side_effect=refuse):
            result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual("recorded", result["state"])
        self.assertEqual([], self.told())

    def test_a_map_edit_writes_one_note_for_the_hub_and_a_replay_none(self):
        old = self.actions.map_state()["state"]
        checked = self.actions.map_preview({"action": "add", "id": "stable", "title": "Parser"}, old["revision"])
        self.assertEqual([], self.told())
        saved = self.actions.map_commit(checked["id"])
        self.assertEqual("Parser", saved["state"]["nodes"]["stable"]["title"])
        self.assertEqual([("map", self.config["hub"])], self.told())
        self.assertEqual(saved, self.actions.map_commit(checked["id"]))
        self.assertEqual([("map", self.config["hub"])], self.told())


class ReviewWriterTests(ts.MindSandbox):
    def setUp(self):
        super().setUp()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})
        self.actions = action.Actions()
        self.service = review_io.Service(self.actions)
        self.base = dict(group="impact-18", target="b" * 64, revision="a" * 64, previous=0)
        ref = lambda n: "https://github.com/%s/issues/5#issuecomment-%d" % (ts.REPO, n)
        self.item = {"id": self.base["target"], "revision": self.base["revision"], "kind": "task", "label": "Format",
                     "relation": "direct", "branch": "format", "url": ref(18), "draft": "Update the branch evidence.", "issue": 5}
        self.group = {"id": self.base["group"], "old": {"id": 18, "url": ref(18)}, "successors": [{"id": 21, "url": ref(21)}],
                      "conflict": False, "items": [self.item]}
        patch = mock.patch.object(self.service, "state", return_value={"groups": [self.group]})
        patch.start()
        self.addCleanup(patch.stop)

    def told(self):
        return [(n["kind"], n["issue"]) for n in notes.since(ts.REPO, 0)]

    def test_a_saved_verdict_writes_one_note_for_the_hub_and_a_replay_none(self):
        op = self.service.stage(dict(self.base, verdict="defer", note="", draft=""))
        self.assertEqual([], self.told())
        saved = self.service.commit(op["id"])
        self.assertEqual("defer", saved["review"]["verdict"])
        self.assertEqual([("verdict", self.config["hub"])], self.told())
        self.assertEqual(saved, self.service.commit(op["id"]))
        self.assertEqual([("verdict", self.config["hub"])], self.told())


class EditWriterTests(ts.MindSandbox):
    def test_an_applied_edit_writes_one_note_for_the_issue_that_was_edited_and_a_replay_none(self):
        self.write_config(dict(self.config, profiles=["software", "research", "manuscript"]))
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})
        actions = action.Actions()
        self.add_chain()
        data = self.repo()
        old = "https://github.com/%s/issues/5#issuecomment-9102" % ts.REPO
        paragraph = "The release follows the reader policy in %s." % old
        base = data["issues"]["5"]["body"].replace("\r\n", "\n").replace("## Changed\n", "## Changed\n\n" + paragraph + "\n")
        data["issues"]["5"]["body"] = base
        self.save(data)
        service = review_io.Service(actions)
        result = service.state()
        changed = next(g for g in result["groups"] if g["id"] == "impact-9102")
        item = next(item for item in changed["items"] if item.get("edit_before") == paragraph)
        preview = service.stage(dict(group=changed["id"], target=item["id"], revision=item["revision"], previous=0,
                                     verdict="needs_update", note="", draft=item["edit_text"]))
        service.commit(preview["id"])
        told = lambda: [(n["kind"], n["issue"]) for n in notes.since(ts.REPO, 0)]
        self.assertEqual([("verdict", self.config["hub"])], told())
        applied = service.apply(preview["id"])
        self.assertTrue(applied["applied"])
        self.assertEqual([("verdict", self.config["hub"]), ("edit", 5)], told())
        service.apply(preview["id"])
        self.assertEqual([("verdict", self.config["hub"]), ("edit", 5)], told())


class PromptTests(Box):
    """The prompt hook says it once to each session."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        self.config = recortex.load_config()
        self.hub = self.config["hub"]

    def produce(self, event, session="s1", prompt="go on", **extra):
        data = dict({"session_id": session}, **({"prompt": prompt} if event == "prompt-submit" else {}), **extra)
        return hooks.produce(event, data, 10, self.config, self.repo, self.hub)

    def started(self, session="s1"):
        self.produce("session-start", session)

    def said(self, text):
        return [line for line in text.splitlines() if line.startswith("recortex: since your last prompt")]

    def test_nothing_that_came_before_the_sessions_start_is_told(self):
        notes.add(self.repo, "step", 7, now=time.time() - 30)
        self.started()
        self.assertEqual([], self.said(self.produce("prompt-submit")))

    def test_a_note_made_after_the_start_is_told_on_the_next_prompt_once(self):
        self.started()
        notes.add(self.repo, "step", 7)
        notes.add(self.repo, "step", 8)
        notes.add(self.repo, "map", 4)
        text = self.produce("prompt-submit")
        found = self.said(text)
        self.assertEqual(1, len(found), text)
        self.assertIn("2 completed steps, 1 map edit (issues #4, #7, #8)", found[0])
        self.assertRegex(found[0], WORDS)
        self.assertEqual([], self.said(self.produce("prompt-submit")))
        notes.add(self.repo, "verdict", 4)
        found = self.said(self.produce("prompt-submit"))
        self.assertEqual(1, len(found))
        self.assertIn("1 review verdict (issue #4)", found[0])
        self.assertNotIn("completed step", found[0])   # what was told is not told again

    def test_two_sessions_are_each_told(self):
        self.started("s1")
        self.started("s2")
        notes.add(self.repo, "edit", 9)
        for session in ("s1", "s2"):
            self.assertEqual(1, len(self.said(self.produce("prompt-submit", session))), session)
            self.assertEqual([], self.said(self.produce("prompt-submit", session)), session)

    def test_a_session_without_a_start_has_its_cursor_set_to_now_and_is_told_nothing(self):
        notes.add(self.repo, "step", 7, now=time.time() - 10)
        self.assertEqual([], self.said(self.produce("prompt-submit", "fresh")))   # its first prompt is a full brief
        notes.add(self.repo, "step", 8)
        self.assertEqual(1, len(self.said(self.produce("prompt-submit", "fresh"))))

    def test_the_first_full_brief_of_a_session_that_began_without_one_carries_it_after_the_brief(self):
        hooks.remember(self.repo, "late", None, brief=False)   # the session was seen, but its brief was not given (a build)
        notes.add(self.repo, "step", 7)
        text = self.produce("prompt-submit", "late")
        self.assertIn("===== END PROJECT STATUS DATA", text)
        self.assertEqual(1, len(self.said(text)), text)
        self.assertGreater(text.index("recortex: since your last prompt"), text.index("===== END PROJECT STATUS DATA"))

    def test_a_subagents_prompt_is_not_told_and_the_note_stays_for_the_session(self):
        self.started()
        notes.add(self.repo, "step", 7)
        for extra in ({"agent_id": "a1"}, {"agent_type": "general-purpose"}):
            self.assertEqual([], self.said(self.produce("prompt-submit", **extra)), extra)
        self.assertEqual(1, len(self.said(self.produce("prompt-submit"))))

    def test_with_the_prompt_hook_off_nothing_is_said_and_the_note_waits(self):
        self.started()
        notes.add(self.repo, "step", 7)
        self.config["prompt_hook"] = {"enabled": False}
        self.assertEqual("", self.produce("prompt-submit").strip())
        self.config.pop("prompt_hook")
        self.assertEqual(1, len(self.said(self.produce("prompt-submit"))))

    def test_a_file_that_does_not_fit_gives_no_line_and_prints_none_of_it(self):
        self.started()
        self.put({"form": 1, "notes": [{"at": time.time() + 5, "kind": "<img onerror=1>", "issue": 7}]})
        out = self.produce("prompt-submit")
        self.assertEqual([], self.said(out))
        self.assertNotIn("onerror", out)
        self.put({"form": 1, "notes": [{"at": time.time() + 5, "kind": "step", "issue": "7; rm -rf"}]})
        self.assertNotIn("rm -rf", self.produce("prompt-submit"))

    def test_the_line_comes_after_the_prompt_payload_and_before_the_setup_reminder_and_the_task_reminder(self):
        self.started()
        notes.add(self.repo, "step", 7)
        with mock.patch.object(hooks, "setup_reminder", return_value="recortex: setup reminder"), \
                mock.patch.object(hooks, "task_reminder_line", return_value=("Decision change. x", False)):
            lines = self.produce("prompt-submit").splitlines()
        marks = [next(i for i, line in enumerate(lines) if line.startswith(start)) for start in
                 ("recortex: since your last prompt", "recortex: setup reminder", "Decision change.")]
        self.assertEqual(sorted(marks), marks)
        self.assertEqual("Decision change. x", lines[-1])

    def test_it_takes_no_room_from_the_setup_reminder(self):
        seen = {}

        def reminder(repo, config, record, entry, session, room):
            seen[session] = room
            return ""
        self.started("a")
        self.started("b")
        with mock.patch.object(hooks, "setup_reminder", side_effect=reminder):
            with mock.patch.object(hooks, "dashboard_line", return_value="x" * notes.CAP):
                self.produce("prompt-submit", "a")
            with mock.patch.object(hooks, "dashboard_line", return_value=""):
                self.produce("prompt-submit", "b")
        self.assertEqual(seen["a"], seen["b"])   # the same room with a line told and with none

    def test_the_helper_never_raises(self):
        entry = {}
        with mock.patch.object(notes, "since", side_effect=RuntimeError("boom")):
            self.assertEqual("", hooks.dashboard_line(self.repo, entry, {"prompt": "x"}, {}))
        self.assertEqual("", hooks.dashboard_line(self.repo, {}, "not a dict", {}))
        self.assertIsInstance(entry, dict)
        fresh = {}
        self.assertEqual("", hooks.dashboard_line(self.repo, fresh, {}, {}))   # no cursor: set to now, nothing said
        self.assertIsInstance(fresh["dash"], float)


if __name__ == "__main__":
    unittest.main()

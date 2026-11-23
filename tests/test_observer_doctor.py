"""The summary at the end of `hooks.py --doctor` (1.7.0): what works on this machine, in plain words, for four kinds of machine: all
working, capture not ready, an approval that does not hold, and no observer. Fixed words only; a part that cannot be worked out says so."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import hooks  # noqa: E402
import observer_lease  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import observer_sources  # noqa: E402
import recortex  # noqa: E402
import storage  # noqa: E402
import task_reminder  # noqa: E402
import usage  # noqa: E402

REPO = ts.REPO


class Box(ts.RecordSandbox):
    def setUp(self):
        super().setUp()
        self.ready()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})
        self.claude = tempfile.mkdtemp(prefix="doctor-claude-", dir=os.path.join(ts.ROOT, ".tmp"))
        os.chmod(self.claude, 0o700)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.claude, True))

    def doctor(self, env=None, **kwargs):
        done = self.tool("hooks.py", "--doctor", env=dict({"CLAUDE_CONFIG_DIR": self.claude}, **(env or {})), **kwargs)
        return done.stdout

    def block(self, text):
        lines = text.splitlines()
        start = lines.index("on this machine:")
        out = []
        for line in lines[start + 1:]:
            if not line.startswith("  "):
                break
            out.append(line)
        return out, lines[:start], lines[start + 1 + len(out):]

    def row(self, text, label):
        rows, _, _ = self.block(text)
        found = [row for row in rows if row.startswith("  %s: " % label)]
        self.assertEqual(1, len(found), (label, rows))
        return found[0][len("  %s: " % label):]

    def approve(self, **settings):
        options = dict({"enabled": True, "level": "everything", "model": "sonnet",
                        "publisher_id": runtime.publisher_id(create=True)}, **settings)
        return runtime.approve(REPO, {"observer": options}, self.project)


class MachineTests(Box):
    def test_a_machine_where_everything_works(self):
        task_reminder.record(REPO, "on")
        self.approve()
        text = self.doctor()
        rows, before, after = self.block(text)
        self.assertEqual("works", self.row(text, "record brief (session start, prompts, subagents)"))
        self.assertEqual("on", self.row(text, "task reminder"))
        self.assertEqual("on", self.row(text, "line about the dashboard on prompts"))
        self.assertEqual("ready once a session has run here", self.row(text, "observer notes (capture)"))
        self.assertEqual("can be read", self.row(text, "answers given in a session (grants)"))
        self.assertEqual("measured", self.row(text, "usage of the plugin's subagents"))
        self.assertEqual("holds here (%s)" % runtime.BINDING_NOTES["shared"], self.row(text, "observer approval"))
        self.assertIn("  several hosts: a shared home folder needs file locks that all hosts see (NFS, or Lustre mounted with "
                      "flock)", rows)
        self.assertEqual("result: the hooks can show the brief", after[-1])
        self.assertEqual([], [line for line in after if line.startswith("problem:")])

    def test_a_machine_with_no_observer_and_nothing_chosen(self):
        text = self.doctor()
        self.assertEqual("off (setup.py task-reminder set --mode on --yes turns it on)", self.row(text, "task reminder"))
        self.assertEqual("none for this folder", self.row(text, "observer approval"))
        self.assertEqual("can be read", self.row(text, "answers given in a session (grants)"))

    def test_the_block_comes_before_the_problems_and_the_result(self):
        with open(self.trusted, "w") as handle:
            handle.write("")
        text = self.doctor()
        lines = text.splitlines()
        self.assertLess(lines.index("on this machine:"), min(i for i, line in enumerate(lines) if line.startswith("problem:")))
        self.assertEqual("does not work: 1 problem(s) below", self.row(text, "record brief (session start, prompts, subagents)"))

    def test_capture_not_ready_names_the_code_the_boundary_and_the_command(self):
        os.chmod(self.claude, 0o777)
        self.addCleanup(os.chmod, self.claude, 0o700)
        text = self.doctor()
        words = self.row(text, "observer notes (capture)")
        self.assertRegex(words, r"^not ready: source-permissions at native-config\. Run in your own terminal: python3 \S+/observer\.py "
                                r"trust-ancestor --failing ; then the same with --yes$")
        self.assertEqual("cannot be read, for the same reason; run the observer's commands in your own terminal until then",
                         self.row(text, "answers given in a session (grants)"))
        self.assertNotIn(self.claude, text.split("on this machine:")[1].split("problem:")[0].replace(
            "trust-ancestor", ""))   # the summary names no folder of the user's: only the plugin's own script

    def test_a_failure_the_command_cannot_cover_names_the_other_command(self):
        os.chmod(self.claude, 0o777)
        self.addCleanup(os.chmod, self.claude, 0o700)
        folder = observer_sources.project_directory(os.path.realpath(self.project))
        os.makedirs(os.path.join(self.claude, "projects"), mode=0o700)
        os.makedirs(os.path.join(self.claude, "projects", os.path.basename(folder)), mode=0o777)
        os.chmod(os.path.join(self.claude, "projects", os.path.basename(folder)), 0o777)
        words = self.row(self.doctor(), "observer notes (capture)")
        self.assertIn("observer.py status --source-details", words)
        self.assertNotIn("trust-ancestor", words)

    def test_an_approval_that_does_not_hold_says_why_in_fixed_words(self):
        self.approve()
        path = os.path.join(runtime.approvals_folder(), runtime.APPROVALS)
        data = json.loads(Path(path).read_text())
        entry = next(iter(data["folders"].values()))
        entry.pop("key_file")
        entry["publisher_id"] = entry["settings"]["publisher_id"] = "0" * 32   # an approval of 1.6.0, made on another host
        storage.write_file(path, json.dumps(data))
        words = self.row(self.doctor(), "observer approval")
        self.assertEqual("does not hold: " + runtime.APPROVAL_NOTES["host-old"], words)
        entry["key_file"] = "0" * 32   # one of 1.7.0, with a key file that does not follow here
        storage.write_file(path, json.dumps(data))
        self.assertEqual("does not hold: " + runtime.APPROVAL_NOTES["host"], self.row(self.doctor(), "observer approval"))

    def test_an_approval_of_another_folder_is_none_for_this_one(self):
        runtime.approve(REPO, {"observer": {"enabled": True, "level": "everything", "model": "sonnet",
                                            "publisher_id": runtime.publisher_id(create=True)}}, os.path.join(self.dir, "other"))
        self.assertEqual("none for this folder", self.row(self.doctor(), "observer approval"))

    def test_a_subagent_that_could_not_be_measured_is_dated_and_coded(self):
        usage.note_unmeasured(REPO, "source-owner", now=1794945600)
        self.assertEqual("not measured since 2026-11-17 (source-owner)", self.row(self.doctor(), "usage of the plugin's subagents"))

    def test_the_task_reminder_row_in_each_state(self):
        task_reminder.record(REPO, "off")
        self.assertIn("off (", self.row(self.doctor(), "task reminder"))
        task_reminder.record(REPO, "on")
        self.assertEqual("on", self.row(self.doctor(), "task reminder"))
        self.approve(advisory_mode="deliver")
        self.assertEqual("delivered by the observer", self.row(self.doctor(), "task reminder"))
        path = task_reminder.path()
        os.chmod(path, 0o644)
        self.assertEqual("cannot be read", self.row(self.doctor(), "task reminder"))

    def test_the_dashboard_line_follows_the_prompt_hook(self):
        self.write_config(dict(self.config, prompt_hook={"enabled": False}))
        self.assertEqual("off with the prompt hook", self.row(self.doctor(), "line about the dashboard on prompts"))
        self.write_config(dict(self.config, prompt_hook={"enabled": True, "every": 5}))
        self.assertEqual("on", self.row(self.doctor(), "line about the dashboard on prompts"))


class WordsTests(Box):
    def summary(self, problems=()):
        return hooks.machine_summary(REPO, recortex.load_config(), list(problems))

    def test_a_part_that_cannot_be_worked_out_says_so_and_nothing_raises(self):
        with mock.patch.object(task_reminder, "read", side_effect=RuntimeError("boom")), \
                mock.patch.object(observer_sources, "preflight", side_effect=OSError("boom")), \
                mock.patch.object(usage, "unmeasured", side_effect=ValueError("boom")), \
                mock.patch.object(runtime, "local_config", side_effect=ValueError("boom")):
            lines = self.summary()
        for label in ("task reminder", "observer notes (capture)", "answers given in a session (grants)",
                      "usage of the plugin's subagents", "observer approval"):
            self.assertIn("  %s: could not be checked" % label, lines)
        self.assertIn("  record brief (session start, prompts, subagents): works", lines)
        self.assertEqual("on this machine:", lines[0])

    def test_a_mount_that_keeps_its_locks_on_the_host_adds_one_fixed_line(self):
        with mock.patch.object(observer_lease, "mount_note", return_value=observer_lease.LOCAL_LOCKS):
            lines = self.summary()
        self.assertIn("  several hosts: " + observer_lease.LOCAL_LOCKS, lines)
        self.assertEqual(2, sum(1 for line in lines if line.startswith("  several hosts:")))
        with mock.patch.object(observer_lease, "mount_note", return_value=""):
            self.assertEqual(1, sum(1 for line in self.summary() if line.startswith("  several hosts:")))

    def test_another_hosts_worker_is_one_line_without_a_host_name(self):
        import observer_store
        with observer_store.Store(REPO, config=recortex.load_config()) as store:
            folder = store.folder
        with mock.patch.object(observer_lease, "hostname", lambda: "login-1"):
            lease = observer_lease.acquire(folder)
        with mock.patch.object(observer_lease, "hostname", lambda: "node-17"):
            lines = self.summary()
        found = [line for line in lines if line.startswith("  observer worker:")]
        self.assertEqual(1, len(found), lines)
        self.assertRegex(found[0], r"^  observer worker: another host's observer worker holds this folder until "
                                   r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2} )?[0-9]{2}:[0-9]{2} UTC$")   # with the date past midnight
        self.assertNotIn("login-1", "\n".join(lines))
        self.assertEqual("taken", lease.state)

    def test_no_name_from_the_account_database_and_no_path_of_the_project_is_in_it(self):
        text = "\n".join(self.summary())
        for word in (self.project, self.dir, os.path.expanduser("~")):
            self.assertNotIn(word, text)

    def test_every_row_is_one_line_of_plain_ascii(self):
        for line in self.summary():
            self.assertTrue(line.isascii() and "\n" not in line, line)


if __name__ == "__main__":
    unittest.main()

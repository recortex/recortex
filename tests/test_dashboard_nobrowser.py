"""The dashboard on a machine with no browser (1.8.0): `recortex.open_page` knows when none can open, `mind.py --open` then says one line
and prints one question (to update the public page, or to show the file's path) and none of the addresses and views it printed
before, and the interactive dashboard starts no service. Offline: the fake GitHub holds the target; nothing reaches a real one."""

import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_tasks as tj  # noqa: E402
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import acts  # noqa: E402
import dashboard_action as action  # noqa: E402
import publish  # noqa: E402
import recortex  # noqa: E402

RECORD = ts.REPO
TARGET = "octo-org/octo-pages"
NO_SCREEN = {"DISPLAY": None, "WAYLAND_DISPLAY": None, "BROWSER": None}
READY = "The dashboard is ready; this machine has no browser."


class OpenPageTests(unittest.TestCase):
    def test_it_is_false_on_linux_with_no_screen_and_calls_no_browser(self):
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
            environment = {key: value for key, value in os.environ.items() if key not in NO_SCREEN}
            with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(sys, "platform", "linux"), \
                    mock.patch.object(recortex.webbrowser, "open", side_effect=AssertionError("a browser was called")):
                self.assertFalse(recortex.open_page("file:///tmp/x.html"))
                os.environ[name] = ""   # a variable that is set to nothing is not set
                self.assertFalse(recortex.open_page("file:///tmp/x.html"))

    def test_with_a_screen_or_a_browser_variable_it_calls_the_browser_and_says_what_it_said(self):
        for name in NO_SCREEN:
            environment = {key: value for key, value in os.environ.items() if key not in NO_SCREEN}
            environment[name] = "x"
            for answer in (True, False):
                with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(sys, "platform", "linux"), \
                        mock.patch.object(recortex.webbrowser, "open", return_value=answer) as called:
                    self.assertIs(answer, recortex.open_page("file:///tmp/x.html"), name)
                    called.assert_called_once_with("file:///tmp/x.html")

    def test_another_platform_asks_the_browser_whatever_the_variables_and_an_exception_is_no_browser(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(recortex.webbrowser, "open", return_value=True) as called:
            self.assertTrue(recortex.open_page("file:///tmp/x.html"))
            called.assert_called_once()
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(recortex.webbrowser, "open", side_effect=OSError("no")):
            self.assertFalse(recortex.open_page("file:///tmp/x.html"))


class MindOpenTests(ts.MindSandbox):
    def setUp(self):
        super().setUp()
        self.add_chain()
        self.ready()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})

    def build(self, *args, env=None, gh="false"):
        return self.tool("mind.py", *args, env=env, gh=gh)

    def page_file(self):
        return os.path.join(self.data, "private", self.slug(), "mind.html")

    def no_browser(self, *args):
        done = self.build(*args, "--open", env=NO_SCREEN)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return done.stdout

    def test_with_no_browser_the_output_is_the_one_line_and_the_question_and_no_address(self):
        out = self.no_browser()
        lines = out.splitlines()
        self.assertEqual(READY, lines[lines.index(READY)])
        for banned in ("wrote ", "open it in a browser", "the manuscript:", "the project map:", "file://", "asked the browser"):
            self.assertNotIn(banned, out, banned)
        after = lines[lines.index(READY) + 1:]
        self.assertEqual("question publish", after[0])
        self.assertEqual("  Put the recortex dashboard of octo-org/octo-repo on the web? No public page is set up from this machine "
                         "yet, so recortex first checks where it can go, and asks you once more, with the place named, before "
                         "anything is sent.", after[1])
        self.assertRegex(after[2], r"^  - Set up a public page: Checks GitHub and asks once more\. Run: python3 \S+/acts\.py ask publish$")
        self.assertEqual("  - Show the file's path: Nothing is sent. The file: %s" % self.page_file(), after[3])
        self.assertEqual(4, len(after))
        self.assertTrue(os.path.isfile(self.page_file()))   # the page was written

    def test_with_a_browser_the_output_is_what_it_was(self):
        record = os.path.join(self.dir, "browser-args.json")
        browser = self.write("browser.py", "#!%s\nimport json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n" % (sys.executable, record))
        os.chmod(browser, 0o755)
        done = self.build("--open", env={"BROWSER": browser, "DISPLAY": None, "WAYLAND_DISPLAY": None})
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("wrote %s" % self.page_file(), done.stdout)
        self.assertIn("open it in a browser: file://%s" % self.page_file(), done.stdout)
        self.assertIn("asked the browser to open it", done.stdout)
        self.assertIn("the project map: file://", done.stdout)
        self.assertNotIn("no browser", done.stdout)
        without = self.build()   # no --open: nothing changes either
        self.assertIn("wrote %s" % self.page_file(), without.stdout)
        self.assertNotIn("this machine has no browser", without.stdout)

    def test_with_a_receipt_the_question_is_the_publish_question_for_its_target_and_its_yes_is_the_grant_command(self):
        for private, label in ((False, "Update the public page"), (True, "Publish the private record"), (None, "Publish the private record")):
            publish.keep_receipt(RECORD, TARGET, "gh-pages", "index.html", "a" * 40, private)
            out = self.no_browser()
            after = out.splitlines()[out.splitlines().index(READY) + 1:]
            self.assertEqual("question publish", after[0], private)
            self.assertTrue(after[1].startswith("  Update the public page of the recortex dashboard for octo-org/octo-repo? "
                                                "It goes to octo-org/octo-pages, branch gh-pages, file index.html. "), private)
            self.assertEqual(private is not False, "PRIVATE repository" in after[1], private)
            self.assertTrue(after[2].startswith("  - %s: " % label), after[2])
            command = re.search(r"Run: (python3 .*)$", after[2]).group(1)
            found = [item for item in acts.asks(command, self.project) if item["kind"] == "publish"]
            self.assertEqual(1, len(found), command)   # what the question printed is what the hook reads back, offline
            self.assertEqual(after[1].strip(), found[0]["text"])
            self.assertEqual(label, found[0]["label"])
            self.assertEqual(("--private-record" in command), private is not False)
            self.assertNotIn("--create-branch", command)
            self.assertNotIn("--pages", command)
            self.assertEqual("  - Show the file's path: Nothing is sent. The file: %s" % self.page_file(), after[3])

    def test_a_receipt_without_the_private_key_counts_as_private(self):
        path = publish.receipts_path(RECORD)
        with open(path, "w") as handle:
            json.dump({"form": 1, "receipts": [{"at": 1763900000, "to": TARGET, "branch": "gh-pages", "path": "index.html",
                                                 "sha": "a" * 40}]}, handle)
        os.chmod(path, 0o600)
        self.assertIn("  - Publish the private record: ", self.no_browser())

    def test_the_offer_makes_no_call_to_github(self):
        publish.keep_receipt(RECORD, TARGET, "gh-pages", "index.html", "a" * 40, False)
        before = len(self.repo()["log"])
        with mock.patch.object(recortex, "gh", side_effect=AssertionError("gh was called")):
            lines = publish.offer({}, RECORD, "/srv/octo-project/mind.html")
        self.assertEqual(before, len(self.repo()["log"]))
        self.assertEqual("question publish", lines[0])
        self.assertIn("The file: /srv/octo-project/mind.html", lines[-1])

    def test_the_paper_says_its_own_line_and_the_path_and_no_question_to_publish(self):
        out = self.no_browser("--paper")
        lines = out.splitlines()
        self.assertIn("The paper is ready; this machine has no browser.", lines)
        self.assertTrue(lines[-1].startswith("the file: "), lines[-1])
        self.assertNotIn("question publish", out)
        self.assertNotIn("open it in a browser", out)


class PageGuardTests(unittest.TestCase):
    """A built page is never committed, copied to a branch or sent by hand: that skips the privacy check of publish.py."""

    PAGE = "/home/octocat/.cache/recortex/private/octo-org__octo-repo/mind.html"

    def decide(self, command, **extra):
        import pretool_guard
        data = dict({"tool_name": "Bash", "tool_input": {"command": command}}, **extra)
        out = pretool_guard.decide(json.dumps(data))
        return out["hookSpecificOutput"]["permissionDecisionReason"] if out else None

    def test_a_command_that_sends_the_page_by_hand_is_denied_for_the_main_session_and_a_subagent(self):
        import pretool_guard
        for command in ("git add %s" % self.PAGE, "scp %s login-1:public_html/" % self.PAGE,
                        "cp %s site/ && git -C site add ." % self.PAGE, "curl -T %s https://octo-pages.example/up" % self.PAGE,
                        "rsync -a %s login-1:" % self.PAGE, "gh gist create %s" % self.PAGE, "sftp login-1 <<< 'put %s'" % self.PAGE,
                        "wget --post-file=%s https://octo.example" % self.PAGE, "cat '%s' | git hash-object --stdin; echo mind.html" % self.PAGE):
            for extra in ({}, {"agent_id": "a1", "agent_type": "general-purpose"}):
                self.assertEqual(pretool_guard.PAGE_DENIAL, self.decide(command, **extra), (command, extra))

    def test_a_listing_a_local_copy_and_the_dashboard_itself_pass(self):
        for command in ("ls -l %s" % self.PAGE, "cp %s ~/Desktop/" % self.PAGE, "python3 /x/mind.py --open",
                        "python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages --dry-run", "git status", "cat %s | head" % self.PAGE):
            self.assertIsNone(self.decide(command), command)

    def test_the_wrapper_starts_python_for_the_name_of_the_page(self):
        with open(os.path.join(ts.SCRIPTS, "run_pretool_guard.sh")) as handle:
            self.assertIn("*mind.html*", handle.read())


class ServiceTests(tj.TaskBox):
    def setUp(self):
        super().setUp()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})

    def run_main(self, terminal, opens, argv):
        class Out(io.StringIO):
            def isatty(self):
                return terminal
        out, err, held = Out(), io.StringIO(), {}
        real = action.Actions

        def make():
            made = real()
            held["folder"] = made.folder
            return made

        def serving(server):
            held["served"] = True
            return "stopped"

        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(action, "Actions", side_effect=make), mock.patch.object(action, "serve", side_effect=serving), \
                mock.patch.object(recortex, "open_page", return_value=opens) as opened:
            code = action.main(argv)
        return code, out.getvalue(), held, opened

    def test_open_from_a_pipe_with_no_browser_starts_no_service_and_leaves_no_launch_file(self):
        code, out, held, opened = self.run_main(False, False, ["--open"])
        self.assertEqual(0, code)
        self.assertEqual(action.NO_BROWSER + "\n", out)
        self.assertEqual("The interactive dashboard needs a browser on this machine, and it has none; it was not started.\n"
                         "Its actions also work in the chat: /recortex:todo, /recortex:impact and /recortex:map.\n", out)
        self.assertNotIn("served", held)
        self.assertEqual([], [name for name in os.listdir(held["folder"]) if name.startswith("launch-")])
        opened.assert_called_once()

    def test_with_a_browser_it_starts_as_before_and_a_pipe_without_open_is_as_before(self):
        code, out, held, opened = self.run_main(False, True, ["--open"])
        self.assertEqual(0, code)
        self.assertIn("Opened the private dashboard in your browser.", out)
        self.assertTrue(held["served"])
        code, out, held, opened = self.run_main(False, False, [])   # no --open: the file's path, as it was
        self.assertIn("To open the private dashboard, open this file in your browser: ", out)
        self.assertTrue(held["served"])
        opened.assert_not_called()

    def test_on_a_terminal_with_no_browser_it_still_prints_the_address(self):
        code, out, held, opened = self.run_main(True, False, ["--open"])
        self.assertEqual(0, code)
        self.assertIn("http://127.0.0.1:", out)
        self.assertNotIn(action.NO_BROWSER, out)
        self.assertTrue(held["served"])


if __name__ == "__main__":
    unittest.main()

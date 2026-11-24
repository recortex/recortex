"""A test run leaves no live dashboard server behind (1.9.0).

The session-start hook starts a detached `dashboard_action.py serve` that stays up for 24 hours, so in a test run
(RECORTEX_TESTING=1) the hook starts none unless a test that is about the server sets RECORTEX_TEST_SERVE=1. The guard reads
/proc for a process that names the sandbox; where /proc is missing the test is skipped. Offline."""

import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import dashboard_server  # noqa: E402
import hooks  # noqa: E402

HAS_PROC = os.path.isdir("/proc/self") and os.path.exists("/proc/self/environ")


def servers_of(folder):
    """The pids of live dashboard servers started from `folder`: a process whose command line names dashboard_action.py and
    whose environment names the folder (the hook gives the server its own environment, so a sandbox's folder is in it). Read
    from /proc; a process that cannot be read is skipped."""
    found = []
    wanted = folder.encode("utf-8")
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                command = handle.read()
            if b"dashboard_action.py" not in command:
                continue
            with open("/proc/%s/environ" % entry, "rb") as handle:
                environment = handle.read()
        except OSError:
            continue
        if wanted in environment or wanted in command:
            found.append(int(entry))
    return found


def stop(pids):
    """Stop these processes and wait until they are gone (a stray server of a failing test must not outlive it)."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + 10
    while time.time() < end and any(os.path.exists("/proc/%d" % pid) and not is_zombie(pid) for pid in pids):
        time.sleep(0.05)


def is_zombie(pid):
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return True


@unittest.skipUnless(HAS_PROC, "reads /proc; a platform without it cannot check that no server is left")
class NoServerLeftTests(unittest.TestCase):
    """The session-start hook, started the way Claude Code starts it, in a test run that did not ask for a server."""

    AS_CLAUDE_STARTS_IT = [("bash", ["bash"]), ("claude", ["claude"])]

    def setUp(self):
        self.case = ts.MindSandbox()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.ready()
        self.case.in_process(extra={"CLAUDE_PROJECT_DIR": self.case.project, "RECORTEX_NOTICE": None,
                                    "SSH_CONNECTION": None, "SSH_CLIENT": None, "SSH_TTY": None, "TERM_PROGRAM": None,
                                    "VSCODE_IPC_HOOK_CLI": None, "RECORTEX_DASHBOARD": None})
        # registered before anything starts, so a failing run is cleaned up too
        self.addCleanup(lambda: stop(servers_of(self.case.dir)))

    def session_start(self):
        out = io.StringIO()
        stdin = io.StringIO(json.dumps({"source": "startup", "session_id": "s" * 8}))
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(hooks, "STARTED", time.monotonic()), \
                mock.patch("observer_grants.ancestors", return_value=self.AS_CLAUDE_STARTS_IT):
            hooks.main(["session-start"])
        return out.getvalue()

    def test_a_session_start_in_a_test_run_leaves_no_server_process(self):
        self.assertNotIn("RECORTEX_TEST_SERVE", os.environ)
        self.assertEqual([], servers_of(self.case.dir))
        out = self.session_start()
        self.assertIn("recortex", out)   # the hook ran and printed its brief
        time.sleep(0.5)                  # a detached start would be visible by now
        self.assertEqual([], servers_of(self.case.dir))
        self.assertIsNone(dashboard_server.read_state())

    def test_ensure_starts_nothing_in_a_test_run_and_start_refuses(self):
        with mock.patch.object(dashboard_server.subprocess, "Popen") as popen:
            self.assertEqual("off", dashboard_server.ensure(self.case.project))
            with self.assertRaises(OSError):
                dashboard_server.start()
        popen.assert_not_called()

    def test_a_test_that_is_about_the_server_asks_for_one_with_its_own_variable(self):
        with mock.patch.dict(os.environ, {"RECORTEX_TEST_SERVE": "1"}), \
                mock.patch.object(dashboard_server, "start") as start:
            self.assertTrue(dashboard_server.may_start())
            self.assertEqual("started", dashboard_server.ensure(self.case.project))
        start.assert_called_once()
        for value in ("0", "yes", "true", "2"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"RECORTEX_TEST_SERVE": value}):
                self.assertFalse(dashboard_server.may_start())
        with mock.patch.dict(os.environ, {"RECORTEX_TESTING": "0"}, clear=False):
            self.assertTrue(dashboard_server.may_start())   # outside a test run the variable decides nothing
        with mock.patch.dict(os.environ, {"RECORTEX_TEST_SERVE": "1"}):
            os.environ.pop("RECORTEX_TESTING", None)
            self.assertTrue(dashboard_server.may_start())

    def test_the_scan_finds_a_server_that_a_run_left_and_stops_it(self):
        """The scan itself: a stand-in process with a server's command line and the sandbox in its environment."""
        environment = dict(os.environ, XDG_STATE_HOME=os.path.join(self.case.dir, "xdg-state"))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "dashboard_action.py", "serve"],
                                 env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        end = time.time() + 5
        while time.time() < end and child.pid not in servers_of(self.case.dir):
            time.sleep(0.05)
        self.assertIn(child.pid, servers_of(self.case.dir))
        self.assertEqual([], servers_of(os.path.join(self.case.dir, "another")))
        stop([child.pid])
        self.assertNotIn(child.pid, [pid for pid in servers_of(self.case.dir) if not is_zombie(pid)])


if __name__ == "__main__":
    unittest.main()

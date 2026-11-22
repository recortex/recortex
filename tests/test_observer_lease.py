"""The host lease (1.7.0): with one approval valid on every host of a shared mount, at most one host's observer worker works on a
project's queue at a time, also where file locks do not reach across hosts. Two hosts are simulated by replacing the lease's
host-name function and its clock; no second machine, no model and no network are used."""

import json
import multiprocessing
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "recortex" / "scripts"))
sys.path.insert(0, str(ROOT))
import observer  # noqa: E402
import observer_lease as lease  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import observer_sources as sources  # noqa: E402
import observer_store as store  # noqa: E402
import observer_worker as worker  # noqa: E402
import storage  # noqa: E402
from tests.observer_source_fixture import fixture_trusted_ancestors  # noqa: E402

REPO = "octo-org/octo-repo"
LOGIN, NODE = "login-1", "node-17"
NOW = 1795000000


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class Box(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="recortex-lease-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"), "XDG_CACHE_HOME": str(self.base / "cache"),
                                           "XDG_CONFIG_HOME": str(self.base / "config"), "HOME": str(self.base / "home")},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.folder = storage.folder(storage.state_top(), "octo-org__octo-repo", "observer")
        self.path = os.path.join(self.folder, lease.LEASE)
        self.clock = Clock()
        self.host = LOGIN
        for target, value in (("hostname", lambda: self.host), ("clock", self.clock)):
            patcher = mock.patch.object(lease, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def as_host(self, name):
        self.host = name

    def record(self):
        return json.loads(Path(self.path).read_text())

    def write(self, text, mode=0o600, age=0):
        if os.path.lexists(self.path):
            os.unlink(self.path)
        Path(self.path).write_text(text)
        os.chmod(self.path, mode)
        moment = self.clock() - age
        os.utime(self.path, (moment, moment))


class TakingTests(Box):
    def test_a_free_folder_gives_the_lease_whole_and_private(self):
        taken = lease.acquire(self.folder)
        self.assertEqual(("taken", True), (taken.state, taken.held))
        record = self.record()
        self.assertEqual({"host", "pid", "token", "expires"}, set(record))
        self.assertEqual((LOGIN, os.getpid(), NOW + 600, taken.token), (record["host"], record["pid"], record["expires"],
                                                                         record["token"]))
        self.assertRegex(record["token"], "^[0-9a-f]{32}$")
        self.assertEqual(0o600, stat.S_IMODE(os.lstat(self.path).st_mode))
        self.assertEqual(lease.LEASE, [n for n in os.listdir(self.folder) if not n.startswith(".")][0])
        self.assertEqual([], [n for n in os.listdir(self.folder) if n.startswith(".part-")])   # no temporary file stays

    def test_a_second_hosts_worker_is_held_off_and_the_line_says_until_when_and_never_who(self):
        lease.acquire(self.folder)
        self.as_host(NODE)
        other = lease.acquire(self.folder)
        self.assertEqual(("other", False, NOW + 600), (other.state, other.held, other.until))
        self.assertEqual(NOW + 600, lease.peek(self.folder))
        line = lease.status(self.folder)
        self.assertEqual("another host's observer worker holds this folder until %s UTC" % time.strftime("%H:%M", time.gmtime(NOW + 600)),
                         line)
        for name in (LOGIN, NODE):
            self.assertNotIn(name, line)
        self.assertEqual(LOGIN, self.record()["host"])   # nothing was changed

    def test_the_same_host_is_not_held_off_and_takes_its_own_old_lease(self):
        first = lease.acquire(self.folder)
        self.assertIsNone(lease.peek(self.folder))
        again = lease.acquire(self.folder)   # the local lock says that no worker of this host is working
        self.assertEqual("taken", again.state)
        self.assertNotEqual(first.token, again.token)
        self.assertEqual("", lease.status(self.folder))
        self.assertFalse(first.renew())   # the earlier worker no longer holds it
        self.assertTrue(again.renew())

    def test_another_host_takes_over_only_after_the_expiry_and_the_skew(self):
        lease.acquire(self.folder)
        self.as_host(NODE)
        self.clock.value = NOW + 600 + 120   # the last second that still holds
        self.assertEqual("other", lease.acquire(self.folder).state)
        self.assertEqual(LOGIN, self.record()["host"])
        self.clock.value = NOW + 600 + 121
        self.assertIsNone(lease.peek(self.folder))
        taken = lease.acquire(self.folder)
        self.assertEqual("taken", taken.state)
        self.assertEqual((NODE, taken.token, self.clock.value + 600), (self.record()["host"], self.record()["token"],
                                                                      self.record()["expires"]))
        self.assertEqual([], [n for n in os.listdir(self.folder) if n.endswith(".claim")])   # the claim is gone

    def test_the_token_is_checked_on_renew_and_on_release(self):
        mine = lease.acquire(self.folder)
        self.clock.value = NOW + 300
        self.assertTrue(mine.renew())
        self.assertEqual(NOW + 900, self.record()["expires"])
        self.clock.value = NOW + 900 + 121
        self.as_host(NODE)
        yours = lease.acquire(self.folder)
        self.assertEqual("taken", yours.state)
        self.as_host(LOGIN)
        self.assertFalse(mine.renew())          # the old holder is told it lost the lease
        mine.release()                          # and removes nothing of the new holder's
        self.assertEqual(yours.token, self.record()["token"])
        self.as_host(NODE)
        self.assertTrue(yours.renew())
        yours.release()
        self.assertFalse(os.path.lexists(self.path))
        yours.release()                          # twice is nothing

    def test_a_lease_that_is_not_the_holders_is_not_renewed_by_a_pass_that_never_held_it(self):
        other = lease.Lease(self.folder, "other", until=NOW)
        self.assertFalse(other.held)
        other.release()   # a worker that held nothing removes nothing
        taken = lease.acquire(self.folder)
        lease.Lease(self.folder, "other", until=NOW).release()
        self.assertTrue(os.path.lexists(self.path))
        self.assertTrue(taken.renew())


class ContentionTests(Box):
    def test_two_contenders_for_an_expired_lease_exactly_one_wins(self):
        context = multiprocessing.get_context("fork")
        for round_ in range(4):
            lease.acquire(self.folder)   # a lease of the login host
            self.clock.value = NOW + 10 ** 4 * (round_ + 1)
            receiver, sender = context.Pipe(duplex=False)
            names = ["node-%d" % n for n in range(6)]
            gate = context.Barrier(len(names))

            def contend(name):
                lease.hostname = lambda: name
                gate.wait(5)
                found = lease.acquire(self.folder)
                sender.send((name, found.state))
            workers = [context.Process(target=contend, args=(name,)) for name in names]
            for process in workers:
                process.start()
            for process in workers:
                process.join(20)
            results = [receiver.recv() for _ in names]
            states = sorted(state for _, state in results)
            self.assertEqual(["other"] * 5 + ["taken"], states, (round_, results))
            winner = next(name for name, state in results if state == "taken")
            self.assertEqual(winner, self.record()["host"])
            self.assertEqual([], [n for n in os.listdir(self.folder) if n.endswith(".claim")])
            os.unlink(self.path)

    def test_a_claim_left_by_a_judge_that_died_is_dropped_and_the_lease_taken_over(self):
        lease.acquire(self.folder)
        self.as_host(NODE)
        self.clock.value = NOW + 10 ** 4
        os.link(self.path, self.path + ".claim")   # a judge died holding its claim
        with mock.patch.object(lease, "CLAIM_LEFT", 0.05):
            taken = lease.acquire(self.folder)
        self.assertEqual("taken", taken.state)
        self.assertFalse(os.path.lexists(self.path + ".claim"))

    def test_a_live_claim_makes_a_contender_wait_and_then_see_the_new_lease(self):
        lease.acquire(self.folder)
        self.as_host(NODE)
        self.clock.value = NOW + 10 ** 4
        real_link = os.link
        done = []

        def judge(src, dst, *args, **kwargs):
            real_link(src, dst, *args, **kwargs)
            if dst.endswith(".claim") and not done:
                done.append(1)
                self.as_host("node-99")           # another contender arrives while the claim stands
                try:
                    self.assertEqual("other", lease.acquire(self.folder).state)
                finally:
                    self.as_host(NODE)
        with mock.patch.object(os, "link", side_effect=judge), mock.patch.object(lease, "TRY_SECONDS", 0.3):
            taken = lease.acquire(self.folder)
        self.assertEqual("taken", taken.state)


class DamagedTests(Box):
    def test_a_lease_that_does_not_fit_counts_as_held_until_its_time_plus_the_lease_and_the_skew(self):
        for text in ("{", "[]", "", json.dumps({"host": "login-1"}), json.dumps({"host": "x y", "pid": 1, "token": "0" * 32,
                                                                                   "expires": NOW}),
                     json.dumps({"host": LOGIN, "pid": 1, "token": "0" * 32, "expires": "1"}),
                     json.dumps({"host": LOGIN, "pid": 1, "token": "0" * 32, "expires": NOW, "more": 1}), "x" * 600):
            with self.subTest(text[:20]):
                self.write(text, age=0)
                self.as_host(NODE)
                self.assertEqual("other", lease.acquire(self.folder).state)
                self.assertEqual(NOW + 600, lease.peek(self.folder))
                self.assertIn("until", lease.status(self.folder))
                self.clock.value = NOW + 600 + 120 + 1
                taken = lease.acquire(self.folder)
                self.assertEqual("taken", taken.state)
                self.assertEqual(taken.token, self.record()["token"])
                self.clock.value = NOW
                os.unlink(self.path)

    def test_a_lease_that_is_a_link_or_has_loose_permissions_is_held_then_replaced_and_never_followed(self):
        target = self.base / "elsewhere"
        target.write_text("keep")
        os.symlink(target, self.path)
        os.utime(self.path, (NOW - 700, NOW - 700), follow_symlinks=False)
        self.as_host(NODE)
        self.clock.value = NOW + 10 ** 4
        taken = lease.acquire(self.folder)
        self.assertEqual("taken", taken.state)
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual("keep", target.read_text())   # nothing was written through the link
        os.unlink(self.path)
        self.write(json.dumps({"host": LOGIN, "pid": 4, "token": "1" * 32, "expires": NOW + 600}), mode=0o644)
        self.clock.value = NOW
        self.assertEqual("other", lease.acquire(self.folder).state)   # not believed, not crashed on: held for the lease time

    def test_a_folder_that_cannot_make_the_file_leaves_the_worker_its_own_lock_and_says_so(self):
        with mock.patch.object(storage, "link_new_file", side_effect=OSError("no hard links here")):
            taken = lease.acquire(self.folder)
        self.assertEqual(("unavailable", True), (taken.state, taken.held))
        self.assertTrue(taken.renew())
        taken.release()

    def test_nothing_raises_whatever_the_file_holds(self):
        for text in ("\x00" * 10, "�" * 30, "{\"host\": \"" + "a" * 400):
            self.write(text)
            self.assertIn(lease.read(self.folder)[0], ("bad", "ok"))
            lease.peek(self.folder)
            lease.status(self.folder)


class MountTests(unittest.TestCase):
    PROC = ("tmpfs /run tmpfs rw,nosuid 0 0\n"
            "10.0.0.2:/export/home /home nfs4 rw,vers=4.2,local_lock=none 0 0\n"
            "10.0.0.3@tcp:/lustre /lustre lustre rw,flock 0 0\n"
            "10.0.0.3@tcp:/scratch /scratch lustre rw,localflock 0 0\n"
            "10.0.0.4:/export/work /work nfs rw,local_lock=all 0 0\n"
            "10.0.0.4:/export/work/shared /work/shared nfs rw,local_lock=none 0 0\n"
            "/dev/sda1 / ext4 rw 0 0\n")

    def note(self, path):
        return lease.mount_note(path, self.PROC)

    def test_localflock_and_local_lock_print_the_one_fixed_line_and_the_others_nothing(self):
        for path in ("/scratch/octocat/.local/state", "/work/octocat/state"):
            self.assertEqual(lease.LOCAL_LOCKS, self.note(path), path)
        for path in ("/home/octocat", "/lustre/octocat", "/work/shared/octocat", "/run/x", "/etc"):
            self.assertEqual("", self.note(path), path)

    def test_the_line_says_what_it_means_in_fixed_words(self):
        self.assertIn("SQLite and the file locks of this mount do not stop two hosts", lease.LOCAL_LOCKS)
        self.assertIn("run sessions for one project on one host at a time", lease.LOCAL_LOCKS)
        self.assertIn("hooks that write on two hosts at once can still conflict", lease.LOCAL_LOCKS)

    def test_an_unreadable_proc_mounts_prints_nothing(self):
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertEqual("", lease.mount_note("/scratch/x"))
        self.assertEqual("", lease.mount_note("/scratch/x", ""))

    def test_a_mount_point_is_matched_by_whole_folders_only(self):
        self.assertEqual("", lease.mount_note("/scratchy/x", self.PROC))


class WorkerTests(unittest.TestCase):
    """run_once with the lease: another host's lease stops the pass before any lock or model call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        metadata = fixture_trusted_ancestors(self.base)
        metadata.__enter__()
        self.addCleanup(metadata.__exit__, None, None, None)
        self.project = str(self.base / "project")
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"), "XDG_CACHE_HOME": str(self.base / "cache"),
                                           "XDG_CONFIG_HOME": str(self.base / "config"), "HOME": str(self.base / "home"),
                                           "CLAUDE_CONFIG_DIR": str(self.base / "claude"), "CLAUDE_PROJECT_DIR": self.project},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.config = {"repo": REPO, "observer": {"enabled": True, "level": "everything",
                                                   "publisher_id": runtime.publisher_id(create=True)}}
        self.folder = Path(sources.project_directory(self.project))
        self.folder.mkdir(parents=True)
        for directory in (self.folder, self.folder.parent, self.folder.parent.parent):
            directory.chmod(0o700)
        with store.Store(REPO, config=self.config) as db:
            db.set_control("runtime_gate", {"fake_test_only": True})
            self.state = db.folder
        runtime.approve(REPO, self.config)
        patcher = mock.patch.object(worker.recortex, "allowed_users", return_value={"octocat"})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(worker, "refresh_record", return_value=True)
        self.refreshed = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(runtime, "gate_valid", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(worker, "record_context", return_value={"targets": [], "memories": [], "incomplete": False})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = LOGIN
        for target, value in (("hostname", lambda: self.host),):
            patcher = mock.patch.object(lease, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []

    def run_worker(self):
        return worker.run_once(REPO, self.config, self.project, lambda *args, **kwargs: self.calls.append(args) or {"ok": False})

    def lease_path(self):
        return os.path.join(self.state, lease.LEASE)

    def test_another_hosts_lease_ends_the_pass_quietly_before_any_record_read_or_call(self):
        taken = lease.acquire(self.state)
        self.host = NODE
        with mock.patch.object(worker, "_process", side_effect=AssertionError("worked")):
            result = self.run_worker()
        self.assertEqual(("other-host", []), (result["code"], result["ready"]))
        self.refreshed.assert_not_called()
        self.assertEqual([], self.calls)
        self.assertEqual(taken.token, json.loads(Path(self.lease_path()).read_text())["token"])

    def test_the_lock_of_the_worker_is_not_taken_when_another_host_holds_the_lease(self):
        lease.acquire(self.state)
        self.host = NODE
        names, real = [], store._exclusive_lock

        def watch(folder, name, *args, **kwargs):
            names.append(name)
            return real(folder, name, *args, **kwargs)
        with mock.patch.object(store, "_exclusive_lock", side_effect=watch):
            self.assertEqual("other-host", self.run_worker()["code"])
        self.assertNotIn("worker.lock", names)

    def test_this_hosts_pass_takes_the_lease_works_and_removes_it_when_done(self):
        result = self.run_worker()
        self.assertNotEqual("other-host", result["code"])
        self.assertFalse(os.path.lexists(self.lease_path()))
        with mock.patch.object(worker, "_process", side_effect=lambda *a, **k: self.assertTrue(
                json.loads(Path(self.lease_path()).read_text())["host"] == LOGIN) or {"code": "ok", "ready": []}):
            self.run_worker()
        self.assertFalse(os.path.lexists(self.lease_path()))

    def test_a_lease_this_host_left_does_not_stop_it(self):
        lease.acquire(self.state)   # a worker of this host that died
        result = self.run_worker()
        self.assertNotEqual("other-host", result["code"])

    def test_a_pass_that_lost_its_lease_stops_with_the_same_quiet_code(self):
        def steal(*args, **kwargs):
            self.host = NODE
            os.unlink(self.lease_path())
            lease.acquire(self.state)
            self.host = LOGIN
            return {"code": "ok", "ready": []}
        with mock.patch.object(worker, "_process", side_effect=steal):
            result = self.run_worker()
        self.assertEqual("other-host", result["code"])
        self.assertEqual(NODE, json.loads(Path(self.lease_path()).read_text())["host"])   # the new holder's lease is left alone

    def test_a_file_system_that_cannot_make_the_lease_still_runs_and_status_says_so(self):
        with mock.patch.object(storage, "link_new_file", side_effect=OSError("no links")):
            result = self.run_worker()
        self.assertNotEqual("other-host", result["code"])
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual("unavailable", db.get_control("worker_lease"))
        self.run_worker()
        with store.Store(REPO, config=self.config) as db:
            self.assertEqual("", db.get_control("worker_lease"))

    def status(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(observer.recortex, "load_config",
                                                                return_value=self.config), \
                mock.patch.object(observer.recortex, "allowed_users", return_value={"octocat"}):
            observer.main(["status"])
        return json.loads(out.getvalue())

    def test_status_prints_the_one_fixed_line_while_another_hosts_worker_holds_the_folder(self):
        self.assertNotIn("host_lease", self.status())
        lease.acquire(self.state)
        self.host = NODE
        value = self.status()
        self.assertRegex(value["host_lease"], r"^another host's observer worker holds this folder until [0-9]{2}:[0-9]{2} UTC$")
        self.assertNotIn(LOGIN, json.dumps(value["host_lease"]))
        self.host = LOGIN
        self.assertNotIn("host_lease", self.status())


if __name__ == "__main__":
    unittest.main()

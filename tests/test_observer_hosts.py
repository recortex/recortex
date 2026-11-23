"""The approval follows the key file (1.7.0): an approval made on one host holds on every host that sees the same key file, with
the same inode at the same real path, and `--hosts any` is the user's own fallback for mounts that number files per client.
The hosts are simulated by the host name and by the key file's identity; nothing here reaches GitHub or starts a model."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "recortex" / "scripts"))
sys.path.insert(0, str(ROOT))
import names  # noqa: E402
import observer  # noqa: E402
import observer_grants  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import storage  # noqa: E402

REPO = "octo-org/octo-repo"
LOGIN, NODE = "login-1", "node-17"


def host(name):
    """The process runs on the host `name`."""
    return mock.patch("socket.gethostname", return_value=name)


def key_file(path=None, inode=None):
    """The key file as another client of the mount sees it: another real path, another inode, or both."""
    real_path, real_inode = storage.key_identity()
    return mock.patch.object(storage, "key_identity", return_value=(path or real_path, inode or real_inode))


class Box(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="observer-hosts-", dir=str(ROOT / ".tmp"))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"),
                                               "XDG_CACHE_HOME": str(self.base / "cache"),
                                               "XDG_CONFIG_HOME": str(self.base / "config")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = str(self.base / "project")
        os.makedirs(self.folder)
        (Path(self.folder) / ".recortex.json").write_text(json.dumps({"repo": REPO, "hub": 4}))
        self.trusted = self.base / "trusted"
        self.trusted.write_text("%s octocat\n" % REPO)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.folder,
                                               names.ENV_PREFIX + "TRUSTED_FILE": str(self.trusted)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args, terminal=False):
        out = io.StringIO()
        with mock.patch("observer_grants.needs", return_value=[]), contextlib.redirect_stdout(out), \
                mock.patch.object(observer, "on_terminal", return_value=terminal):
            code = observer.main(list(args))
        return code, json.loads(out.getvalue())

    def enable(self, *extra, terminal=False):
        return self.run_cli("enable", "--model", "sonnet", "--level", "everything", "--yes", *extra, terminal=terminal)

    def entry(self):
        return runtime.stored_approval(self.folder)

    def config(self):
        return runtime.local_config({"repo": REPO}, self.folder)

    def reason(self):
        return runtime.approval_reason(REPO, self.config(), root=self.folder)

    def save(self, entry):
        path = os.path.join(runtime.approvals_folder(create=True), runtime.APPROVALS)
        storage.write_file(path, json.dumps({"form": 2, "folders": {entry["root"]: entry}}))


class BindingTests(Box):
    def test_an_approval_made_on_the_login_node_holds_on_a_compute_node_with_the_same_key_file(self):
        with host(LOGIN):
            self.assertEqual(0, self.enable()[0])
            self.assertEqual("host", runtime.binding(self.entry()))
            self.assertEqual("", self.reason())
        with host(NODE):
            self.assertEqual("key-file", runtime.binding(self.entry()))
            self.assertEqual("", self.reason())
            self.assertTrue(runtime.approval_valid(REPO, self.config(), root=self.folder))

    def test_a_copy_of_the_state_folder_has_another_inode_and_the_approval_does_not_follow_it(self):
        with host(LOGIN):
            self.enable()
        with host(NODE), key_file(inode=storage.key_identity()[1] + 1):
            self.assertEqual("", runtime.binding(self.entry()))
            self.assertEqual("host", self.reason())
            self.assertFalse(runtime.approval_valid(REPO, self.config(), root=self.folder))

    def test_another_real_path_of_the_key_file_does_not_hold_either(self):
        with host(LOGIN):
            self.enable()
        with host(NODE), key_file(path="/elsewhere/home/.local/state/recortex/private/key"):
            self.assertEqual("host", self.reason())

    def test_the_device_number_is_not_part_of_the_identity(self):
        with host(LOGIN):
            self.enable()
            before = self.entry()["key_file"]
        self.assertEqual(2, len(storage.key_identity()))   # a path and an inode, never st_dev
        real = os.lstat(storage.key_path())
        other_device = os.stat_result((real.st_mode, real.st_ino, real.st_dev + 5, real.st_nlink, real.st_uid, real.st_gid,
                                       real.st_size, int(real.st_atime), int(real.st_mtime), int(real.st_ctime)))
        real_lstat, key = os.lstat, storage.key_path()

        def lstat(path, *args, **kwargs):
            return other_device if path == key else real_lstat(path, *args, **kwargs)
        with host(NODE), mock.patch.object(os, "lstat", side_effect=lstat):
            self.assertEqual(other_device.st_dev, storage.check_path(key, "file").st_dev)
            self.assertEqual(before, runtime.key_file_id())

    def test_an_approval_of_1_6_0_holds_on_its_host_and_says_what_to_run_elsewhere(self):
        with host(LOGIN):
            self.enable()
            entry = {key: value for key, value in self.entry().items() if key != "key_file"}   # what 1.6.0 wrote
            self.save(entry)
            self.assertEqual("", self.reason())
            self.assertEqual("host", runtime.binding(self.entry()))
        with host(NODE):
            self.assertEqual("host", self.reason())
            self.assertEqual(runtime.APPROVAL_NOTES["host-old"], runtime.approval_note("host", self.entry()))
            self.assertIn("setup.py observer", runtime.APPROVAL_NOTES["host-old"])
            self.assertIn("--yes once in your own terminal, or answer the level question in a session",
                          runtime.APPROVAL_NOTES["host-old"])   # a session needs the answer (RebindTests)
        with host(NODE):   # one enable there (on the user's answer: RebindTests) rewrites it with the key file
            self.enable()
            self.assertEqual("", self.reason())
        with host(LOGIN):
            self.assertEqual("", self.reason())

    def test_an_entry_with_a_wrong_key_file_or_a_wrong_type_does_not_hold(self):
        with host(LOGIN):
            self.enable()
            entry = self.entry()
        for bad in ("0" * 32, "", None, 7, ["x"], entry["key_file"].upper()):
            with host(NODE):
                self.save(dict(entry, key_file=bad))
                self.assertEqual("host", self.reason(), bad)

    def test_hosts_any_written_by_hand_without_a_valid_mark_does_not_hold(self):
        with host(LOGIN):
            self.enable()
            entry = self.entry()
        with host(NODE), key_file(inode=storage.key_identity()[1] + 1):
            for extra in ({"hosts": "any"}, {"hosts": "any", "any_host": "0" * 32}, {"hosts": "any", "any_host": 5},
                          {"any_host": runtime.any_host_id(REPO, entry["root"], entry["publisher_id"])}):
                self.save(dict(entry, **extra))
                self.assertEqual("host", self.reason(), extra)
            self.save(dict(entry, hosts="any", any_host=runtime.any_host_id(REPO, entry["root"], entry["publisher_id"])))
            self.assertEqual("any", runtime.binding(self.entry()))
            self.assertEqual("", self.reason())
            self.save(dict(entry, hosts="any", any_host=runtime.any_host_id("octo-org/other", entry["root"],
                                                                           entry["publisher_id"])))
            self.assertEqual("host", self.reason())   # the mark is for this repository, this folder and this host id

    def test_a_forged_publisher_id_does_not_make_the_key_file_binding_hold(self):
        with host(LOGIN):
            self.enable()
            entry = self.entry()
        with host(NODE), key_file(inode=storage.key_identity()[1] + 1):
            self.save(dict(entry, publisher_id="0" * 32, settings=dict(entry["settings"], publisher_id="0" * 32)))
            self.assertEqual("host", self.reason())


class ReasonTests(Box):
    def test_each_code_in_its_order(self):
        config = {"observer": {"enabled": True, "level": "everything", "model": "sonnet", "bogus": 1}}
        self.assertEqual("settings-invalid", runtime.approval_reason(REPO, config, root=self.folder))
        with mock.patch.object(runtime, "stored_approval", side_effect=storage.StorageError("x", "owner")):
            self.assertEqual("unreadable", runtime.approval_reason(REPO, {"observer": {"enabled": True}}, root=self.folder))
        with mock.patch.object(runtime, "stored_approval", side_effect=ValueError("observer_approvals")):
            self.assertEqual("unreadable", runtime.approval_reason(REPO, {"observer": {"enabled": True}}, root=self.folder))
        self.assertEqual("off", runtime.approval_reason(REPO, {}, root=self.folder))
        self.assertEqual("none", runtime.approval_reason(REPO, {"observer": {"enabled": True}}, root=self.folder))
        self.enable()
        self.assertEqual("", self.reason())
        self.assertEqual("repo", runtime.approval_reason("octo-org/other", self.config(), root=self.folder))
        entry = self.entry()
        self.assertEqual("folder", runtime.approval_reason(REPO, self.config(), approval=dict(entry, root="/elsewhere"),
                                                           root=self.folder))
        self.assertEqual("level", runtime.approval_reason(REPO, dict(self.config(), observer=dict(self.config()["observer"],
                                                                                              level="")), root=self.folder))
        changed = dict(self.config(), observer=dict(self.config()["observer"], max_sort=3))
        self.assertEqual("settings", runtime.approval_reason(REPO, changed, root=self.folder))
        self.assertEqual("settings", runtime.approval_reason(REPO, self.config(), approval=dict(entry, publisher_id="1" * 32),
                                                             root=self.folder))
        with host(NODE), key_file(inode=storage.key_identity()[1] + 1):
            self.assertEqual("host", self.reason())

    def test_an_unreadable_key_is_the_unreadable_code_and_never_a_crash(self):
        self.enable()
        with mock.patch.object(storage, "key_identity", side_effect=storage.StorageError("no", "no-key")), \
                host(NODE):
            self.assertEqual("unreadable", self.reason())
            self.assertEqual("", runtime.binding(self.entry()))
            self.assertFalse(runtime.approval_valid(REPO, self.config(), root=self.folder))

    def test_every_code_has_fixed_words(self):
        for code in ("settings-invalid", "unreadable", "off", "none", "repo", "folder", "level", "settings", "host"):
            self.assertTrue(runtime.APPROVAL_NOTES[code].endswith("."), code)
            self.assertEqual(runtime.APPROVAL_NOTES[code], runtime.approval_note(code, {"key_file": "0" * 32}), code)
        self.assertEqual("", runtime.approval_note("", None))

    def test_a_1_4_0_entry_with_no_level_is_the_level_code_and_paused(self):
        self.enable()
        entry = self.entry()
        old = {key: value for key, value in entry["settings"].items() if key not in ("level",)}
        self.save(dict(entry, settings=old))
        self.assertEqual("level", self.reason())
        self.assertTrue(runtime.paused(REPO, {"repo": REPO}, self.folder))


class WriteTests(Box):
    def test_every_record_carries_the_key_file_and_nothing_new_is_in_the_settings(self):
        from tests.fixtures import observer_settings_151 as old
        self.assertEqual(0, self.enable()[0])
        entry = self.entry()
        self.assertRegex(entry["key_file"], "^[0-9a-f]{32}$")
        self.assertEqual(entry["key_file"], runtime.key_file_id())
        self.assertNotIn("hosts", entry)
        self.assertNotIn("any_host", entry)
        self.assertEqual(entry["settings"], old.settings({"observer": entry["settings"]}))   # 1.5.1 reads the settings as they are

    def test_1_6_0_reads_what_this_version_writes_on_its_host(self):
        """The 1.6.0 check, written out: an entry with the new keys is still valid there, and the file's form is unchanged."""
        self.enable()
        entry = self.entry()
        configured = runtime.settings(self.config())
        self.assertTrue(bool(configured["enabled"] and configured["level"] in runtime.LEVELS and isinstance(entry, dict)
                             and entry.get("repo") == REPO and entry.get("root") == runtime.folder_key(self.folder)
                             and isinstance(entry.get("settings"), dict) and runtime.complete(entry["settings"]) == configured
                             and configured["publisher_id"] == runtime.publisher_id()
                             and entry.get("publisher_id") == configured["publisher_id"]))
        self.assertEqual(2, runtime.read_approvals()["form"])
        self.assertEqual({"form", "folders"}, set(runtime.read_approvals()))

    def test_hosts_any_stores_a_mark_that_holds_on_a_host_with_another_inode(self):
        code, value = self.enable("--hosts", "any", terminal=True)
        self.assertEqual((0, True), (code, value["approved"]))
        self.assertEqual("any", self.entry()["hosts"])
        self.assertEqual(runtime.BINDING_NOTES["any"], value["binding"])
        with host(NODE), key_file(inode=storage.key_identity()[1] + 1):
            self.assertEqual("any", runtime.binding(self.entry()))
            self.assertEqual("", self.reason())

    def test_hosts_any_off_a_terminal_is_refused_and_stores_nothing(self):
        code, value = self.enable("--hosts", "any")
        self.assertEqual((1, "terminal-required"), (code, value["code"]))
        self.assertEqual(observer.HOSTS_ANY, value["note"])
        self.assertIsNone(self.entry())
        code, value = self.run_cli("enable", "--model", "sonnet", "--level", "everything", "--hosts", "any")   # a preview
        self.assertEqual((0, False), (code, value["approved"]))

    def test_hosts_shared_writes_no_any_mark_and_drops_an_earlier_one(self):
        self.enable("--hosts", "any", terminal=True)
        self.assertEqual(0, self.enable("--hosts", "shared")[0])
        self.assertNotIn("hosts", self.entry())
        self.assertNotIn("any_host", self.entry())

    def test_limits_and_advisory_keep_the_key_file_and_a_valid_any_mark(self):
        self.enable("--hosts", "any", terminal=True)
        before = self.entry()
        self.assertEqual(0, self.run_cli("limits", "--middle", "30")[0])
        after = self.entry()
        self.assertEqual((before["key_file"], "any"), (after["key_file"], after["hosts"]))
        self.assertEqual(runtime.any_host_id(REPO, after["root"], after["publisher_id"]), after["any_host"])
        self.assertEqual(0, self.run_cli("advisory", "shadow", "--yes", terminal=True)[0])
        again = self.entry()
        self.assertEqual("any", again["hosts"])
        self.assertEqual("shadow", again["settings"]["advisory_mode"])
        with host(NODE), key_file(inode=storage.key_identity()[1] + 1):
            self.assertEqual("", self.reason())

    def test_an_any_mark_of_another_repository_is_not_carried_to_a_new_one(self):
        self.enable("--hosts", "any", terminal=True)
        entry = self.entry()
        self.save(dict(entry, repo="octo-org/other"))
        self.assertEqual(0, self.enable(terminal=False)[0])
        self.assertNotIn("hosts", self.entry())

    def test_enable_on_a_second_host_makes_the_old_approval_hold_on_both(self):
        with host(LOGIN):
            self.enable()
            self.save({key: value for key, value in self.entry().items() if key != "key_file"})
        with host(NODE):
            self.assertEqual("host", self.reason())
            self.assertEqual(0, self.enable()[0])
            self.assertEqual("", self.reason())
        with host(LOGIN):
            self.assertEqual("key-file", runtime.binding(self.entry()))   # the entry now names the node; the key file holds


class StatusTests(Box):
    def status(self):
        return self.run_cli("status")[1]

    def test_status_says_why_it_is_not_approved_and_what_the_binding_is(self):
        value = self.status()
        self.assertEqual((False, "off"), (value["approved"], value["approval_reason"]))
        self.assertEqual(runtime.APPROVAL_NOTES["off"], value["approval_note"])
        self.assertEqual({"applies": "none", "stored": "none", "note": ""}, value["binding"])
        with host(LOGIN):
            self.enable()
            value = self.status()
            self.assertEqual((True, ""), (value["approved"], value["approval_reason"]))
            self.assertNotIn("approval_note", value)
            self.assertEqual({"applies": "host", "stored": "shared", "note": runtime.BINDING_NOTES["shared"]}, value["binding"])
        with host(NODE):
            self.assertEqual("key-file", self.status()["binding"]["applies"])
        with host(NODE), key_file(inode=storage.key_identity()[1] + 9):
            value = self.status()
            self.assertEqual((False, "host", "none"), (value["approved"], value["approval_reason"], value["binding"]["applies"]))
            self.assertEqual(runtime.APPROVAL_NOTES["host"], value["approval_note"])

    def test_status_of_an_approval_from_before_1_7_0_names_the_one_command_to_run(self):
        with host(LOGIN):
            self.enable()
            self.save({key: value for key, value in self.entry().items() if key != "key_file"})
            self.assertEqual({"applies": "host", "stored": "host", "note": runtime.BINDING_NOTES["host"]},
                             self.status()["binding"])
        with host(NODE):
            value = self.status()
            self.assertEqual(runtime.APPROVAL_NOTES["host-old"], value["approval_note"])


class RebindTests(Box):
    """On a host where this folder's approval does not hold (a copy of the state folder, another inode of the key file), a
    session does not make it hold with the stored level: that is a new approval, and it needs the user's answer, as on a
    new folder (the 1.7.0 review, S3). `needs` is not replaced here."""

    def cli(self, *args, terminal=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(observer, "on_terminal", return_value=terminal):
            code = observer.main(list(args))
        return code, json.loads(out.getvalue())

    def session_enable(self, *extra, terminal=False):
        return self.cli("enable", "--model", "sonnet", "--yes", *extra, terminal=terminal)

    def made_on_the_login_host(self):
        with host(LOGIN):
            self.assertEqual(0, self.session_enable("--level", "everything", terminal=True)[0])
            return self.entry()

    def copy(self):
        """The copy's host: another name and another inode of the key file."""
        inode = storage.key_identity()[1] + 1
        stack = contextlib.ExitStack()
        stack.enter_context(host(NODE))
        stack.enter_context(key_file(inode=inode))
        return stack

    def folder_key(self):
        return runtime.folder_key(self.folder)

    def test_a_session_on_a_copy_does_not_make_the_approval_hold_without_the_users_answer(self):
        before = self.made_on_the_login_host()
        with self.copy():
            self.assertEqual("host", self.reason())
            for extra in (("--level", "everything"), ()):   # with the stored level named, and with it left to the folder
                code, result = self.session_enable(*extra)
                self.assertEqual((1, "enable_unanswered", False), (code, result["code"], result["approved"]), extra)
                self.assertEqual(before, self.entry())
                self.assertEqual("host", self.reason())
        with host(LOGIN):
            self.assertEqual("", self.reason())   # and the host that made it still holds

    def test_a_lower_level_on_a_copy_is_a_new_approval_too(self):
        self.made_on_the_login_host()
        with self.copy():
            code, result = self.session_enable("--level", "notes")
            self.assertEqual((1, "enable_unanswered"), (code, result["code"]))

    def test_with_the_users_recorded_answer_the_session_makes_it_hold_there_once(self):
        self.made_on_the_login_host()
        with self.copy():
            self.assertTrue(observer_grants.put(self.folder_key(), "level", "everything", "toolu_rebind"))
            code, result = self.session_enable("--level", "everything")
            self.assertEqual((0, True), (code, result["approved"]))
            self.assertEqual("", self.reason())   # it holds here now
            self.assertFalse(observer_grants.held(self.folder_key(), [("level", "everything")]))   # the answer was used once
            self.assertEqual("session", self.entry()["given"])

    def test_the_users_own_terminal_needs_no_answer_to_make_it_hold(self):
        self.made_on_the_login_host()
        with self.copy():
            code, result = self.session_enable("--level", "everything", terminal=True)
            self.assertEqual((0, True), (code, result["approved"]))
            self.assertEqual("", self.reason())
            self.assertEqual("terminal", self.entry()["given"])

    def test_where_the_approval_holds_a_session_enables_the_same_level_as_before(self):
        self.made_on_the_login_host()
        with host(NODE):   # the same key file: the approval follows it
            self.assertEqual("", self.reason())
            code, result = self.session_enable("--level", "everything")
            self.assertEqual((0, True), (code, result["approved"]))

    def test_the_setup_chain_stops_before_its_steps_on_a_copy_without_the_answer(self):
        import setup
        self.made_on_the_login_host()
        before = self.entry()
        answers = {"repo": REPO, "observer": {"mode": "shadow", "model": "sonnet", "level": "everything", "sort_model": "haiku",
                                              "strong": ""}}
        out = io.StringIO()
        with self.copy(), mock.patch.object(observer, "on_terminal", return_value=False), \
                mock.patch.object(setup, "run_observer_steps") as steps, contextlib.redirect_stdout(out):
            setup.observer_step(answers, self.folder, sys.executable, False)
        self.assertIn("observer: not turned on. " + observer_grants.UNANSWERED, out.getvalue())
        steps.assert_not_called()
        self.assertEqual(before, self.entry())

    def test_binding_holds_says_whether_the_stored_approval_follows_this_host(self):
        self.assertTrue(runtime.binding_holds(REPO, self.folder))   # no approval at all: nothing to carry over
        self.made_on_the_login_host()
        self.assertTrue(runtime.binding_holds(REPO, self.folder))
        self.assertTrue(runtime.binding_holds("octo-org/other", self.folder))   # another repository's: not this one's
        with self.copy():
            self.assertFalse(runtime.binding_holds(REPO, self.folder))
        with mock.patch.object(runtime, "stored_approval", side_effect=OSError("no")):
            self.assertTrue(runtime.binding_holds(REPO, self.folder))   # nothing can be read: no stored settings either


class LabelAndPassTests(Box):
    """Where the approval was given: the chain's pass carries it, because the child's output is a pipe."""

    def folder_key(self):
        return runtime.folder_key(self.folder)

    def test_a_chain_started_on_a_terminal_stores_terminal_though_its_child_is_not_on_one(self):
        import setup
        with mock.patch.object(observer, "on_terminal", return_value=True):
            word = setup.chain_pass(self.folder_key(), "everything", None, [("level", "everything")])
        code, value = self.run_cli("enable", "--model", "sonnet", "--level", "everything", "--yes", "--pass", word)
        self.assertEqual((0, True), (code, value["approved"]))
        self.assertEqual("terminal", self.entry()["given"])

    def test_a_chain_started_in_a_session_stores_session(self):
        import setup
        with mock.patch.object(observer, "on_terminal", return_value=False), \
                mock.patch.object(observer_grants, "take", return_value=True):
            word = setup.chain_pass(self.folder_key(), "everything", None, [("level", "everything")])
        code, value = self.run_cli("enable", "--model", "sonnet", "--level", "everything", "--yes", "--pass", word)
        self.assertEqual(0, code)
        self.assertEqual("session", self.entry()["given"])

    def test_a_child_given_a_forged_pass_word_gets_neither_the_label_nor_hosts_any(self):
        import setup
        with mock.patch.object(observer, "on_terminal", return_value=True):
            setup.chain_pass(self.folder_key(), "everything", None, [], hosts="any")
        forged = "ab" * 16
        code, value = self.enable("--pass", forged)
        self.assertEqual(0, code)
        self.assertEqual("session", self.entry()["given"])
        code, value = self.enable("--hosts", "any", "--pass", forged)
        self.assertEqual((1, "terminal-required"), (code, value["code"]))
        self.assertEqual("session", self.entry()["given"])
        self.assertNotIn("hosts", self.entry())

    def test_hosts_any_goes_through_a_chain_only_when_it_was_started_on_a_terminal_with_the_flag(self):
        import setup
        with mock.patch.object(observer, "on_terminal", return_value=True):
            plain = setup.chain_pass(self.folder_key(), "everything", None, [])
        code, value = self.enable("--hosts", "any", "--pass", plain)
        self.assertEqual((1, "terminal-required"), (code, value["code"]))   # the chain had no --hosts any
        with mock.patch.object(observer, "on_terminal", return_value=True):
            word = setup.chain_pass(self.folder_key(), "everything", None, [], hosts="any")
        code, value = self.enable("--hosts", "any", "--pass", word)
        self.assertEqual((0, True), (code, value["approved"]))
        self.assertEqual(("terminal", "any"), (self.entry()["given"], self.entry()["hosts"]))
        with mock.patch.object(observer, "on_terminal", return_value=False):   # started in a session: the flag is dropped
            word = setup.chain_pass(self.folder_key(), "everything", None, [], hosts="any")
        code, value = self.enable("--hosts", "any", "--pass", word)
        self.assertEqual((1, "terminal-required"), (code, value["code"]))

    def test_setup_stops_before_any_step_for_hosts_any_off_a_terminal(self):
        import setup
        argv = ["observer", "--level", "everything", "--hosts", "any", "--dry-run"]
        out = io.StringIO()
        with mock.patch.object(observer, "on_terminal", return_value=False), contextlib.redirect_stdout(out):
            code = setup.main(argv)
        self.assertEqual(1, code)
        self.assertIn(observer.HOSTS_ANY, out.getvalue())
        self.assertNotIn("would run", out.getvalue())

    def test_the_label_comes_from_on_terminal_not_from_stdin_and_stdout(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        with mock.patch.object(observer.sys, "stdin", Tty()):
            self.enable()
        self.assertEqual("session", self.entry()["given"])   # a tty on stdin alone says nothing about the user's terminal
        self.enable(terminal=True)
        self.assertEqual("terminal", self.entry()["given"])
        self.run_cli("limits", "--middle", "30")
        self.assertEqual("terminal", self.entry()["given"])   # limits keeps where it was given


class MapModeTests(Box):
    """7.3 at the command: raising the map mode from a session needs the grant of the user's recorded answer, once."""

    def setUp(self):
        super().setUp()
        self.assertEqual(0, self.enable(terminal=True)[0])
        self.folder_key = runtime.folder_key(self.folder)

    def mode(self):
        return self.entry()["settings"].get("map_mode", "off")

    def map_mode(self, mode, terminal=False):
        return self.run_cli("map-mode", mode, "--yes", terminal=terminal)

    def test_without_the_users_answer_a_session_cannot_raise_it_and_nothing_changes(self):
        code, value = self.map_mode("automatic")
        self.assertEqual((1, "map_unanswered"), (code, value["code"]))
        self.assertEqual(observer_grants.MAP_UNANSWERED, value["note"])
        self.assertIn("map-upkeep", value["note"])
        self.assertEqual("off", self.mode())

    def test_the_grant_of_the_right_mode_raises_it_once_and_keeps_where_it_was_given(self):
        before = self.entry()
        observer_grants.put(self.folder_key, "map", "automatic", "toolu_map1")
        code, value = self.map_mode("automatic")
        self.assertEqual((0, True), (code, value["approved"]))
        self.assertEqual("automatic", self.mode())
        after = self.entry()
        self.assertEqual((before["given"], before["approved_at"], before["key_file"]),
                         (after["given"], after["approved_at"], after["key_file"]))
        self.run_cli("map-mode", "off", "--yes")
        self.assertEqual("off", self.mode())
        self.assertEqual("map_unanswered", self.map_mode("automatic")[1]["code"])   # the grant was used once

    def test_a_grant_for_the_other_mode_does_not_raise_it(self):
        observer_grants.put(self.folder_key, "map", "shadow", "toolu_map2")
        self.assertEqual("map_unanswered", self.map_mode("automatic")[1]["code"])
        self.assertEqual("off", self.mode())
        self.assertEqual(0, self.map_mode("shadow")[0])
        self.assertEqual("shadow", self.mode())

    def test_lowering_and_the_same_mode_need_nothing_and_a_terminal_needs_no_grant(self):
        self.assertEqual(0, self.map_mode("automatic", terminal=True)[0])
        self.assertEqual("automatic", self.mode())
        self.assertEqual(0, self.map_mode("automatic")[0])      # not raised
        self.assertEqual(0, self.map_mode("shadow")[0])         # lowered
        self.assertEqual("shadow", self.mode())
        self.assertEqual(0, self.map_mode("off")[0])
        self.assertEqual("off", self.mode())

    def test_a_preview_changes_nothing_and_needs_nothing(self):
        code, value = self.run_cli("map-mode", "automatic")
        self.assertEqual((0, False), (code, value["approved"]))
        self.assertEqual("off", self.mode())

    def test_the_notes_level_has_no_map(self):
        self.assertEqual(0, self.run_cli("enable", "--model", "sonnet", "--level", "notes", "--yes", terminal=True)[0])
        observer_grants.put(self.folder_key, "map", "automatic", "toolu_map3")
        code, value = self.map_mode("automatic")
        self.assertEqual((1, "approval"), (code, value["code"]))

    def test_when_the_answer_cannot_be_read_the_refusal_says_so(self):
        with mock.patch.object(observer_grants, "unread_reason", return_value="source-owner"):
            code, value = self.map_mode("automatic")
        self.assertEqual((1, "map_answer_unreadable", "source-owner"), (code, value["code"], value["reason"]))
        self.assertEqual(observer_grants.UNREADABLE % "source-owner", value["note"])
        self.assertEqual("off", self.mode())


class NoticeTests(Box):
    def test_a_host_that_does_not_hold_the_approval_gets_its_own_session_start_line(self):
        import observer_hooks
        self.assertIn("approval-host", observer_hooks.NOTICES)
        self.assertNotIn(runtime.APPROVAL_NOTES["host"], observer_hooks.NOTICES["approval-host"])
        self.assertEqual("recortex observer: this folder's approval does not hold on this host; observation is inactive. "
                         "Run observer.py status.", observer_hooks.NOTICES["approval-host"])
        with host(LOGIN):
            self.enable()
        config = {"repo": REPO, "hub": 1, "observer": dict(self.config()["observer"])}
        data = {"session_id": "s1", "cwd": self.folder}
        with host(NODE), key_file(inode=storage.key_identity()[1] + 1), \
                mock.patch("observer_hooks.recortex.trusted_repos", return_value={REPO: set()}), \
                mock.patch("observer_hooks.recortex.allowed_users", return_value={"octocat"}):
            out = observer_hooks.handle("session-start", data, config, REPO)
        self.assertIn(observer_hooks.NOTICES["approval-host"], out.get("systemMessage", ""))
        self.assertNotIn(observer_hooks.NOTICES["approval"], out.get("systemMessage", "").split("\n"))
        with host(NODE), mock.patch("observer_hooks.recortex.trusted_repos", return_value={REPO: set()}), \
                mock.patch("observer_hooks.recortex.allowed_users", return_value={"octocat"}):
            out = observer_hooks.handle("session-start", data, config, REPO)
        self.assertNotIn(observer_hooks.NOTICES["approval-host"], out.get("systemMessage", ""))


class CaptureLineTests(Box):
    def line(self, result):
        import observer_sources
        import setup
        with mock.patch.object(observer_sources, "preflight", return_value=result):
            return setup.capture_line(self.folder)

    def test_the_dry_run_says_whether_notes_can_be_taken_and_what_to_run(self):
        self.assertEqual("capture: ready", self.line({"ready": True, "code": "ok"}))
        self.assertEqual("capture: ready once a session has run in this folder",
                         self.line({"ready": False, "code": "source-missing"}))
        text = self.line({"ready": False, "code": "source-owner", "boundary": "native-projects", "fix": "trust-ancestor",
                          "issues": [{"code": "source-owner", "boundary": "native-projects"}]})
        self.assertRegex(text, r"^capture: not ready \(source-owner at native-projects\); notes cannot be taken, and a session "
                               r"cannot read your answer here\. Run in your own terminal: python3 \S+/observer\.py "
                               r"trust-ancestor --failing ; then the same with --yes$")
        other = self.line({"ready": False, "code": "source-symlink", "boundary": "source-directory",
                           "issues": [{"code": "source-symlink", "boundary": "source-directory"}]})
        self.assertIn("observer.py status --source-details", other)
        self.assertNotIn("trust-ancestor", other)

    def test_a_scripts_folder_with_a_space_is_quoted_in_every_command_that_is_printed(self):
        # The 1.7.0 review, N9: the user pastes these, and a space must not split the path.
        import hooks
        import observer_sources
        import setup
        spaced = "/home/octo cat/.claude/plugins/recortex/skills/recortex/scripts"
        quoted = "'%s/observer.py'" % spaced
        commands = observer_sources.trust_commands(spaced)
        self.assertEqual(["python3 %s trust-ancestor --failing" % quoted, "python3 %s trust-ancestor --failing --yes" % quoted],
                         commands)
        self.assertEqual(spaced + "/observer.py", __import__("shlex").split(commands[0])[1])
        failing = {"ready": False, "code": "source-symlink", "boundary": "source-directory",
                   "issues": [{"code": "source-symlink", "boundary": "source-directory"}]}
        with mock.patch.object(setup, "HERE", spaced):
            self.assertIn("python3 %s status --source-details" % quoted, self.line(failing))
            self.assertIn("python3 %s status --source-details." % quoted,
                          setup.blocked_text([{"id": "observer-level", "code": "source-symlink", "commands": []}]))
        with mock.patch.object(hooks, "__file__", spaced + "/hooks.py"), \
                mock.patch.object(observer_sources, "preflight", return_value=failing):
            rows = hooks.machine_summary(REPO, {"repo": REPO, "hub": 4}, [])
        self.assertTrue(any("python3 %s status --source-details" % quoted in row for row in rows), rows)

    def test_the_capture_line_comes_before_the_binding_line_and_the_steps(self):
        import setup
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.observer_step({"repo": REPO, "observer": {"mode": "automatic", "model": "sonnet", "level": "everything",
                                                           "sort_model": "haiku", "strong": ""}}, self.folder, sys.executable, True)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("capture: "), lines[0])
        self.assertTrue(lines[1].startswith("binding: "), lines[1])
        self.assertTrue(lines[2].startswith("would run: "), lines[2])


class DryRunTests(Box):
    def test_the_dry_run_prints_the_binding_the_approval_will_get(self):
        import setup
        stored = {"repo": REPO, "observer": {"mode": "automatic", "model": "sonnet", "level": "everything",
                                             "sort_model": "haiku", "strong": ""}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.observer_step(stored, self.folder, sys.executable, True)
        lines = out.getvalue().splitlines()
        self.assertIn("binding: " + runtime.BINDING_NOTES["shared"], lines)
        self.assertLess(lines.index("binding: " + runtime.BINDING_NOTES["shared"]),
                        min(i for i, line in enumerate(lines) if line.startswith("would run")))
        stored["observer"]["hosts"] = "any"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.observer_step(stored, self.folder, sys.executable, True)
        self.assertIn("binding: " + runtime.BINDING_NOTES["any"], out.getvalue().splitlines())
        self.assertIn("--hosts any", out.getvalue())


if __name__ == "__main__":
    unittest.main()

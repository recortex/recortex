"""Trusted ancestors (1.7.0): on a shared network filesystem the folders above Claude Code's transcripts belong to another
account, and the user can trust such a folder exactly as it is. The folder is accepted only while its owner, group and mode
match, never the transcript, its project folder or what is in it. Also the command that records the trust, the session's
inability to run it, and the usage count that no longer fails in silence. Folders of other accounts are modeled by changing
what `os.lstat` reports for one path; nothing is run as another user and no model is called."""

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "recortex" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
import hooks  # noqa: E402
import observer  # noqa: E402
import observer_hooks  # noqa: E402
import observer_sources as sources  # noqa: E402
import pretool_guard  # noqa: E402
import recortex  # noqa: E402
import storage  # noqa: E402
import usage  # noqa: E402
from tests.observer_source_fixture import fixture_trusted_ancestors  # noqa: E402
import test_jobs_usage as tu  # noqa: E402

FOREIGN = 4000011
REAL_COMPONENT_PROBLEM = sources._component_problem   # before any test replaces it with a model of the system folders


def seen_as(path, uid=None, gid=None, mode=None, kind=None):
    """What lstat says about `path`: another owner, group or mode, and with `kind` (stat.S_IFLNK) another kind of file (the folder
    itself is not changed)."""
    real = os.lstat
    target = os.fspath(path)

    def lstat(candidate, *args, **kwargs):
        info = real(candidate, *args, **kwargs)
        if os.fspath(candidate) != target:
            return info
        return SimpleNamespace(st_mode=((kind or stat.S_IFMT(info.st_mode)) | mode) if mode is not None else info.st_mode,
                               st_uid=info.st_uid if uid is None else uid, st_gid=info.st_gid if gid is None else gid,
                               st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size)

    return mock.patch.object(os, "lstat", side_effect=lstat)


class Box(unittest.TestCase):
    """A scratch tree under /tmp: a shared folder (the one that another account owns), the Claude configuration below it,
    this project's native folder, a transcript, and a state folder with a user key."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="recortex-trust-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(os.path.realpath(self.temporary.name))
        for name in ("etc", "state", "project"):
            (self.base / name).mkdir(mode=0o700)
        fixture = fixture_trusted_ancestors(self.base)
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.shared = self.base / "shared"
        self.native = self.shared / "home" / ".claude"
        self.project = self.base / "project"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.native), "XDG_STATE_HOME": str(self.base / "state"),
                                           "XDG_CACHE_HOME": str(self.base / "cache"), "XDG_CONFIG_HOME": str(self.base / "config"),
                                           "CLAUDE_PROJECT_DIR": str(self.project)})
        env.start()
        self.addCleanup(env.stop)
        self.folder = Path(sources.project_directory(str(self.project)))
        self.folder.mkdir(parents=True)
        for directory in (self.shared, self.shared / "home", self.native, self.native / "projects", self.folder):
            directory.chmod(0o700)
        self.path = self.folder / "session-1.jsonl"
        self.path.write_text('{"type":"user","message":{"role":"user","content":"Choose blue."}}\n')
        self.path.chmod(0o600)
        storage.user_key(create=True)
        self.home = self.shared / "home"

    def foreign(self, path=None, mode=0o755, **changes):
        return seen_as(path or self.shared, uid=changes.pop("uid", FOREIGN), mode=mode, **changes)

    def preflight(self, **kwargs):
        return sources.preflight(str(self.project), **kwargs)

    def approved(self):
        return sources.approved_path(str(self.path), str(self.project), "session-1")

    def trust(self, path=None, **seen):
        """Trust a folder as it looks while `foreign` is in force (call inside it)."""
        return sources.trust_add([str(path or self.shared)], str(self.project))

    def trusted_file(self):
        return Path(storage.folder(storage.state_top(for_key=True))) / sources.TRUSTED


class AcceptTests(Box):
    def test_a_folder_of_another_account_is_refused_until_the_user_trusts_it_exactly_as_it_is(self):
        with self.foreign():
            self.assertEqual("source-owner", self.preflight()["code"])
            with self.assertRaisesRegex(sources.SourceError, "source-owner"):
                self.approved()
            stored = self.trust()
            self.assertEqual({str(self.shared)}, set(stored))
            result = self.preflight()
            self.assertTrue(result["ready"])
            self.assertEqual((1, "ok"), (result["trusted"], result["code"]))
            self.assertNotIn("trust", result)
            self.assertEqual(str(self.path), self.approved())

    def test_the_owner_the_group_and_the_mode_are_each_part_of_what_was_trusted(self):
        with self.foreign(gid=777):
            self.trust()
            self.assertTrue(self.preflight()["ready"])
        for case, change in (("owner", dict(uid=FOREIGN + 1, gid=777)), ("group", dict(uid=FOREIGN, gid=778)),
                             ("mode", dict(uid=FOREIGN, gid=777, mode=0o775))):
            with self.subTest(case):
                with seen_as(self.shared, **dict(change, mode=change.pop("mode", 0o755))):
                    result = self.preflight()
                    self.assertFalse(result["ready"])
                    self.assertEqual("changed", result["trust"], case)
                    with self.assertRaises(sources.SourceError):
                        self.approved()

    def test_a_group_writable_shared_folder_of_the_user_is_trusted_by_its_mode_too(self):
        with seen_as(self.shared, mode=0o2775, gid=123):
            files = self.base / "etc"
            with mock.patch.multiple(sources, PASSWD=str(files / "none"), GROUP=str(files / "none")):
                self.assertEqual("source-permissions", self.preflight()["code"])
                self.trust()
                self.assertTrue(self.preflight()["ready"])
                self.assertEqual(str(self.path), self.approved())

    def test_a_tampered_entry_is_refused(self):
        with self.foreign():
            self.trust()
            data = json.loads(self.trusted_file().read_text())
            entry = data["entries"][str(self.shared)]
            for field, value in (("mac", "0" * 32), ("uid", FOREIGN + 5), ("mode", 0o777), ("gid", 9)):
                bad = json.loads(json.dumps(data))
                bad["entries"][str(self.shared)] = dict(entry, **{field: value})
                self.trusted_file().write_text(json.dumps(bad))
                self.trusted_file().chmod(0o600)
                result = self.preflight()
                self.assertFalse(result["ready"], field)
                self.assertEqual("source-owner", result["code"], field)

    def test_an_entry_made_with_another_key_trusts_nothing(self):
        with self.foreign():
            self.trust()
            (Path(storage.key_path())).write_text("a" * 64 + "\n")
            self.assertFalse(self.preflight()["ready"])

    def test_nothing_is_read_from_the_trusted_file_unless_a_component_fails(self):
        with mock.patch.object(sources, "read_trusted", side_effect=AssertionError("read")):
            self.assertTrue(self.preflight()["ready"])
            self.assertEqual(str(self.path), self.approved())


class NeverTests(Box):
    """The transcript, its project folder and what is in it, and every other failure, never pass by an entry."""

    def entry_for(self, path, uid, gid, mode):
        entry = {"uid": uid, "gid": gid, "mode": mode, "mac": sources._mac(str(path), uid, gid, mode), "added": 1}
        data = {"form": 1, "entries": {str(path): entry}}
        folder = storage.folder(storage.state_top(for_key=True))
        storage.write_file(os.path.join(folder, sources.TRUSTED), json.dumps(data))

    def test_an_entry_written_by_hand_for_the_project_folder_changes_nothing(self):
        with seen_as(self.folder, uid=FOREIGN, mode=0o755):
            self.entry_for(self.folder, FOREIGN, os.lstat(self.folder).st_gid, 0o755)
            self.assertEqual("source-owner", self.preflight()["code"])
            with self.assertRaisesRegex(sources.SourceError, "source-owner"):
                self.approved()

    def test_nor_for_a_folder_inside_it_nor_for_the_transcript(self):
        nested = self.folder / "session-1" / "subagents"
        nested.mkdir(parents=True, mode=0o700)
        (self.folder / "session-1").chmod(0o700)
        report = nested / "agent-a1.jsonl"
        report.write_text("{}\n")
        report.chmod(0o600)
        with seen_as(nested, uid=FOREIGN, mode=0o755):
            self.entry_for(nested, FOREIGN, os.lstat(nested).st_gid, 0o755)
            with self.assertRaisesRegex(sources.SourceError, "source-owner"):
                sources.approved_path(str(report), str(self.project), "session-1")
        with seen_as(self.path, uid=FOREIGN, mode=0o644):
            self.entry_for(self.path, FOREIGN, os.lstat(self.path).st_gid, 0o644)
            with self.assertRaises(sources.SourceError):
                self.approved()

    def test_an_entry_for_a_folder_that_is_not_above_the_project_folder_is_never_used(self):
        other = self.base / "other"
        other.mkdir(mode=0o700)
        with seen_as(other, uid=FOREIGN, mode=0o755):
            trust = sources.Trust()
            self.entry_for(other, FOREIGN, os.lstat(other).st_gid, 0o755)
            info = os.lstat(other)
            self.assertFalse(trust.accepts(str(other), info, str(self.folder), str(self.path)))

    def test_a_symlink_cannot_be_trusted_and_other_failures_are_not_covered(self):
        link = self.base / "link"
        link.symlink_to(self.shared)
        with self.assertRaises(sources.TrustError):
            sources.trust_add([str(link)], str(self.project))
        self.assertFalse(self.trusted_file().exists())
        with seen_as(self.shared, uid=FOREIGN, mode=0o755):
            trust = sources.Trust()
            info = os.lstat(self.shared)
            self.assertEqual("source-symlink", sources._problem(str(self.shared), SimpleNamespace(
                st_mode=stat.S_IFLNK | 0o777, st_uid=FOREIGN, st_gid=0), str(self.folder), None, trust))
            self.assertEqual("source-path", sources._problem(str(self.shared), SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(), st_gid=0), str(self.folder), None, trust))
            self.assertEqual(stat.S_IFDIR, stat.S_IFMT(info.st_mode))

    def test_a_link_with_a_matching_entry_is_still_refused(self):
        link = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=FOREIGN, st_gid=0)
        self.entry_for(self.shared, FOREIGN, 0, 0o777)
        trust = sources.Trust()
        self.assertFalse(trust.accepts(str(self.shared), link, str(self.folder)))
        self.assertEqual("source-symlink", sources._problem(str(self.shared), link, str(self.folder), None, trust))
        plain = SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=FOREIGN, st_gid=0)
        self.assertTrue(sources.Trust().accepts(str(self.shared), plain, str(self.folder)))   # the same entry holds for a folder

    def test_an_ancestor_that_is_a_link_is_refused_by_trust_add_even_when_it_is_above_the_project_folder(self):
        with seen_as(self.shared, uid=FOREIGN, mode=0o777, kind=stat.S_IFLNK):
            with self.assertRaises(sources.TrustError):
                sources.trust_add([str(self.shared)], str(self.project))
        self.assertFalse(self.trusted_file().exists())

    def test_the_project_folder_and_the_transcript_are_refused_by_trust_add(self):
        with self.assertRaises(sources.TrustError):
            sources.trust_add([str(self.folder)], str(self.project))
        with self.assertRaises(sources.TrustError):
            sources.trust_add([str(self.path)], str(self.project))
        with self.assertRaises(sources.TrustError):
            sources.trust_add(["relative/path"], str(self.project))


class FileTests(Box):
    def test_a_trusted_file_with_loose_permissions_trusts_nothing_and_says_so(self):
        with self.foreign():
            self.trust()
            self.trusted_file().chmod(0o644)
            result = self.preflight()
            self.assertFalse(result["ready"])
            self.assertEqual("unreadable", result["trust"])
            with self.assertRaisesRegex(sources.SourceError, "source-owner"):
                self.approved()
            self.trusted_file().chmod(0o600)
            self.assertTrue(self.preflight()["ready"])

    def test_a_file_that_does_not_fit_the_form_trusts_nothing_and_is_never_replaced(self):
        with self.foreign():
            self.trust()
            for text in ("{", "[]", json.dumps({"form": 2, "entries": {}}), json.dumps({"form": 1, "entries": []}),
                         json.dumps({"form": 1, "entries": {"relative": {}}}), "x" * (sources.TRUSTED_CAP + 1)):
                self.trusted_file().write_text(text)
                self.trusted_file().chmod(0o600)
                self.assertEqual("unreadable", self.preflight()["trust"], text[:20])
                with self.assertRaisesRegex(sources.TrustError, "unreadable"):
                    sources.trust_add([str(self.shared)], str(self.project))
                self.assertEqual(text, self.trusted_file().read_text())

    def test_an_entry_with_an_odd_value_makes_the_whole_file_unusable(self):
        with self.foreign():
            self.trust()
            data = json.loads(self.trusted_file().read_text())
            base = data["entries"][str(self.shared)]
            for field, value in (("uid", "0"), ("uid", -1), ("uid", True), ("gid", 2 ** 33), ("mode", 0o10000), ("mac", "XYZ"),
                                 ("added", "now")):
                bad = {"form": 1, "entries": {str(self.shared): dict(base, **{field: value})}}
                self.trusted_file().write_text(json.dumps(bad))
                self.assertEqual(("unreadable", {}), sources.read_trusted(), (field, value))

    def test_at_most_sixty_four_entries(self):
        with self.foreign():
            entries = {"/a%d" % n: {"uid": 1, "gid": 1, "mode": 0o755, "mac": "0" * 32, "added": 1} for n in range(65)}
            self.trusted_file().parent.mkdir(exist_ok=True)
            self.trusted_file().write_text(json.dumps({"form": 1, "entries": entries}))
            self.trusted_file().chmod(0o600)
            self.assertEqual("unreadable", sources.read_trusted()[0])

    def test_the_file_is_private_and_in_the_private_folder_beside_the_key(self):
        with self.foreign():
            self.trust()
        info = os.lstat(self.trusted_file())
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        self.assertEqual(os.path.dirname(storage.key_path()), str(self.trusted_file().parent))

    def test_forget_removes_one_entry_and_nothing_else(self):
        with self.foreign():
            self.trust()
        self.assertTrue(sources.trust_forget(str(self.shared)))
        self.assertFalse(sources.trust_forget(str(self.shared)))
        self.assertEqual(("ok", {}), sources.read_trusted())


class AnsweredTests(Box):
    def test_a_recorded_answer_is_read_through_a_trusted_ancestor_and_was_refused_before(self):
        data = {"session_id": "session-1", "transcript_path": str(self.path), "cwd": str(self.project)}
        with self.foreign():
            with self.assertRaisesRegex(sources.SourceError, "source-owner"):
                sources.answered(data, str(self.project), "abcd1234", "line")
            self.trust()
            self.assertIsNone(sources.answered(data, str(self.project), "abcd1234", "line"))   # read, and nothing answered


class DetailsTests(Box):
    def test_details_name_the_owner_and_group_numbers_and_the_fix(self):
        with self.foreign(gid=777):
            result = self.preflight(details=True)
            self.assertEqual("trust-ancestor", result["fix"])
            issue = result["issues"][0]
            self.assertEqual((FOREIGN, 777, str(self.shared)), (issue["uid"], issue["gid"], issue["path"]))
            self.assertNotIn("uid", self.preflight()["issues"][0])   # numbers are for the detailed view

    def test_no_fix_is_offered_for_a_failure_the_command_cannot_cover(self):
        with self.foreign(), seen_as(self.folder, uid=FOREIGN, mode=0o755):
            self.assertNotIn("fix", self.preflight())
        with seen_as(self.shared, uid=os.getuid(), mode=0o777):
            self.assertEqual("trust-ancestor", self.preflight()["fix"])

    def test_failing_lists_what_the_walk_refuses_with_who_may_write(self):
        with seen_as(self.shared, uid=FOREIGN, mode=0o777):
            rows = sources.failing(str(self.project))
            row = next(row for row in rows if row["path"] == str(self.shared))
            self.assertEqual(("source-owner", "0777", True, True), (row["code"], row["mode"], row["trustable"], row["world"]))
        with seen_as(self.shared, uid=FOREIGN, mode=0o1777):
            self.assertFalse(next(row for row in sources.failing(str(self.project)) if row["path"] == str(self.shared))["world"])
        with seen_as(self.folder, uid=FOREIGN, mode=0o755):
            row = next(row for row in sources.failing(str(self.project)) if row["path"] == str(self.folder))
            self.assertFalse(row["trustable"])

    def test_a_world_writable_folder_without_the_sticky_bit_can_be_trusted(self):
        with seen_as(self.shared, uid=FOREIGN, mode=0o777):
            self.trust()
            self.assertTrue(self.preflight()["ready"])


class CommandBox(Box):
    def setUp(self):
        super().setUp()
        (self.project / ".recortex.json").write_text(json.dumps({"repo": "octo-org/octo-repo", "hub": 4}))
        self.trusted = self.base / "trusted-repos"
        self.trusted.write_text("octo-org/octo-repo octocat\n")
        env = mock.patch.dict(os.environ, {recortex.names.ENV_PREFIX + "TRUSTED_FILE": str(self.trusted)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args, terminal=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(observer, "on_terminal", return_value=terminal):
            code = observer.main(list(args))
        return code, json.loads(out.getvalue())


class CommandTests(CommandBox):
    def test_without_yes_it_shows_and_changes_nothing_and_never_needs_an_approval(self):
        with self.foreign():
            code, value = self.run_cli("trust-ancestor", str(self.shared))
            self.assertEqual((0, False), (code, value["approved"]))
            row = value["entries"][0]
            self.assertEqual((str(self.shared), FOREIGN, "0755", "source-owner", False),
                             (row["path"], row["uid"], row["mode"], row["code"], row["world"]))
            self.assertRegex(row["owner"], r"^(|[A-Za-z0-9._-]{1,32})$")
            self.assertIn("Each folder is accepted only while its owner, group and mode stay as shown", value["note"])
            self.assertIn("Run the same command with --yes to trust them.", value["note"])
            self.assertFalse(self.trusted_file().exists())

    def test_yes_needs_the_users_own_terminal_and_then_stores_the_folder(self):
        with self.foreign():
            code, value = self.run_cli("trust-ancestor", str(self.shared), "--yes")
            self.assertEqual((1, "terminal-required"), (code, value["code"]))
            self.assertIn("separate terminal, outside Claude Code", value["note"])
            self.assertFalse(self.trusted_file().exists())
            code, value = self.run_cli("trust-ancestor", str(self.shared), "--yes", terminal=True)
            self.assertEqual((0, True), (code, value["approved"]))
            self.assertTrue(value["capture_preflight"]["ready"])
            self.assertEqual(0, self.run_cli("status")[0])

    def test_a_world_writable_folder_adds_the_fixed_warning(self):
        with seen_as(self.shared, uid=FOREIGN, mode=0o777):
            code, value = self.run_cli("trust-ancestor", "--failing")
            self.assertIn("Anyone on this machine can replace what is below a folder that all may write and that has no "
                          "sticky bit.", value["note"])
            self.assertTrue(value["entries"][0]["world"])

    def test_failing_trusts_the_trustable_rows_and_lists_the_others(self):
        with self.foreign(), seen_as(self.folder, uid=FOREIGN, mode=0o755):
            code, value = self.run_cli("trust-ancestor", "--failing")
            paths = {row["path"]: row["trustable"] for row in value["entries"]}
            self.assertEqual({str(self.shared): True, str(self.folder): False}, paths)
            code, value = self.run_cli("trust-ancestor", "--failing", "--yes", terminal=True)
            self.assertEqual((0, True), (code, value["approved"]))
            self.assertEqual({str(self.shared)}, set(sources.read_trusted()[1]))   # the project folder was not stored

    def test_nothing_to_trust_and_not_an_ancestor(self):
        code, value = self.run_cli("trust-ancestor", "--failing")
        self.assertEqual((1, "nothing-to-trust"), (code, value["code"]))
        for path in ("relative", str(self.folder), str(self.path), str(self.base / "nowhere"), str(self.shared) + "/../shared"):
            code, value = self.run_cli("trust-ancestor", path)
            self.assertEqual((1, "not-an-ancestor"), (code, value["code"]), path)
        link = self.base / "link"
        link.symlink_to(self.shared)
        self.assertEqual("not-an-ancestor", self.run_cli("trust-ancestor", str(link))[1]["code"])
        code, value = self.run_cli("trust-ancestor", str(self.shared))   # fine as it is: nothing to trust
        self.assertEqual((1, "nothing-to-trust"), (code, value["code"]))

    def test_exactly_one_of_the_four_forms(self):
        for args in ((), ("--failing", "--list"), (str(self.shared), "--failing"), ("--forget", str(self.shared), "--yes"),
                     ("--list", "--yes")):
            code, value = self.run_cli("trust-ancestor", *args)
            self.assertEqual((1, "invalid-arguments"), (code, value["code"]), args)

    def test_list_shows_each_entry_and_whether_it_still_matches(self):
        with self.foreign():
            self.run_cli("trust-ancestor", str(self.shared), "--yes", terminal=True)
            value = self.run_cli("trust-ancestor", "--list")[1]
            self.assertEqual("ok", value["state"])
            self.assertEqual([(str(self.shared), "0755", True)], [(r["path"], r["mode"], r["matches"]) for r in value["entries"]])
        with seen_as(self.shared, uid=FOREIGN, mode=0o700):
            value = self.run_cli("trust-ancestor", "--list")[1]
            self.assertFalse(value["entries"][0]["matches"])

    def test_forget_needs_the_terminal(self):
        with self.foreign():
            self.run_cli("trust-ancestor", str(self.shared), "--yes", terminal=True)
            code, value = self.run_cli("trust-ancestor", "--forget", str(self.shared))
            self.assertEqual((1, "terminal-required"), (code, value["code"]))
            self.assertEqual(1, len(sources.read_trusted()[1]))
            code, value = self.run_cli("trust-ancestor", "--forget", str(self.shared), terminal=True)
            self.assertEqual((0, True), (code, value["forgotten"]))
            self.assertEqual(({}, 1), (sources.read_trusted()[1], self.run_cli("trust-ancestor", "--forget", str(self.shared),
                                                                               terminal=True)[0]))

    def test_an_unreadable_trusted_file_is_a_code_and_is_not_replaced(self):
        with self.foreign():
            self.trust()
            self.trusted_file().chmod(0o644)
            code, value = self.run_cli("trust-ancestor", str(self.shared), "--yes", terminal=True)
            self.assertEqual((1, "unreadable"), (code, value["code"]))
            self.assertEqual(0o644, stat.S_IMODE(os.lstat(self.trusted_file()).st_mode))

    def test_a_repository_that_is_not_trusted_is_refused_before_anything(self):
        self.trusted.write_text("")
        code, value = self.run_cli("trust-ancestor", "--list")
        self.assertEqual((1, "repository-not-trusted"), (code, value["code"]))

    def test_the_names_of_an_owner_are_printed_only_when_plain(self):
        hostile = SimpleNamespace(pw_name="x\x1b[31m; rm -rf")
        with self.foreign(), mock.patch.object(observer, "_pwd", return_value=SimpleNamespace(getpwuid=lambda n: hostile)):
            row = self.run_cli("trust-ancestor", "--failing")[1]["entries"][0]
            self.assertEqual("", row["owner"])
            self.assertNotIn("rm -rf", json.dumps(row))

    def test_status_prints_the_two_commands_with_the_scripts_full_path(self):
        with self.foreign():
            value = self.run_cli("status")[1]
            fix = value["capture_fix"]
            script = os.path.join(os.path.dirname(os.path.abspath(observer.__file__)), "observer.py")
            self.assertEqual(["python3 %s trust-ancestor --failing" % script,
                              "python3 %s trust-ancestor --failing --yes" % script], fix["commands"])
            self.assertEqual("Run them in your own terminal, outside Claude Code.", fix["note"])
            self.assertNotIn("capture_note", value)
            self.run_cli("trust-ancestor", "--failing", "--yes", terminal=True)
        with seen_as(self.shared, uid=FOREIGN + 3, mode=0o755):
            value = self.run_cli("status")[1]
            self.assertEqual("changed", value["capture_preflight"]["trust"])
            self.assertEqual("A folder you trusted changed its owner, group or mode and is refused again. Look at it, then run "
                             "trust-ancestor again.", value["capture_note"])

    def test_the_command_is_a_terminal_row_of_the_hooks_table(self):
        self.assertEqual("terminal", observer_hooks.COMMANDS["trust-ancestor"])
        self.assertIn("trust-ancestor", observer_hooks.TERMINAL_ONLY)
        self.assertTrue(observer_hooks._undo({"tool_name": "Bash", "tool_input": {"command": "python3 x/observer.py trust-ancestor"}}))


class RootFolderTests(CommandBox):
    """The root folder is an ancestor like the others (the 1.7.0 review, S4). In a container with a user namespace the files of
    the host's root show as the overflow user, so `/` fails with source-owner there, and it must be possible to trust it."""

    OVERFLOW = 65534

    def root_owned_by(self, uid):
        """The walk sees `/` owned by `uid`: the fixture's model of the system folders (root's) is not applied to it."""
        stack = contextlib.ExitStack()
        current = sources._component_problem

        def modeled(path, metadata, folder, source_file=None):
            if path == "/":
                return REAL_COMPONENT_PROBLEM(path, metadata, folder, source_file)
            return current(path, metadata, folder, source_file)
        stack.enter_context(mock.patch.object(sources, "_component_problem", side_effect=modeled))
        stack.enter_context(seen_as("/", uid=uid, mode=0o755))
        return stack

    def test_the_root_folder_is_listed_as_one_that_can_be_trusted(self):
        with self.root_owned_by(self.OVERFLOW):
            self.assertEqual("source-owner", self.preflight()["code"])
            rows = {row["path"]: row for row in sources.failing(str(self.project))}
            self.assertEqual(("source-owner", True), (rows["/"]["code"], rows["/"]["trustable"]))

    def test_the_root_folder_is_trusted_exactly_as_it_is_and_then_capture_is_ready(self):
        with self.root_owned_by(self.OVERFLOW):
            self.assertEqual({"/"}, set(self.trust("/")))
            result = self.preflight()
            self.assertTrue(result["ready"], result)
            self.assertEqual((1, "ok"), (result["trusted"], result["code"]))
            self.assertEqual(str(self.path), self.approved())

    def test_a_changed_owner_of_the_root_folder_refuses_it_again(self):
        with self.root_owned_by(self.OVERFLOW):
            self.trust("/")
        with self.root_owned_by(self.OVERFLOW + 1):
            result = self.preflight()
            self.assertFalse(result["ready"])
            self.assertEqual("changed", result["trust"])
            with self.assertRaisesRegex(sources.SourceError, "source-owner"):
                self.approved()

    def test_the_command_trusts_the_root_folder_by_name_and_among_the_failing(self):
        with self.root_owned_by(self.OVERFLOW):
            code, value = self.run_cli("trust-ancestor", "/", "--yes", terminal=True)
            self.assertEqual((0, True), (code, value["approved"]))
            self.assertEqual({"/"}, set(sources.read_trusted()[1]))
            self.assertTrue(self.preflight()["ready"])
        sources.trust_forget("/")
        with self.root_owned_by(self.OVERFLOW):
            code, value = self.run_cli("trust-ancestor", "--failing", "--yes", terminal=True)
            self.assertEqual((0, True), (code, value["approved"]))
            self.assertIn("/", sources.read_trusted()[1])

    def test_the_folders_that_are_not_above_the_project_folder_are_still_refused(self):
        with self.root_owned_by(self.OVERFLOW):
            for path in ("//", "/nowhere", str(self.folder), str(self.path), str(self.folder) + "/.."):
                code, value = self.run_cli("trust-ancestor", path)
                self.assertEqual((1, "not-an-ancestor"), (code, value["code"]), path)
            with self.assertRaises(sources.TrustError):
                sources.trust_add([str(self.folder)], str(self.project))


class GuardTests(unittest.TestCase):
    def decide(self, command, **extra):
        data = dict({"tool_name": "Bash", "tool_input": {"command": command}}, **extra)
        return pretool_guard.decide(json.dumps(data))

    def test_the_session_cannot_run_the_users_own_commands_in_any_project(self):
        for command in ("python3 /x/skills/recortex/scripts/observer.py trust-ancestor --failing --yes",
                        "python3 /x/observer.py --repo octo-org/octo-repo trust-ancestor /home",
                        "python3 /x/observer.py enable --model sonnet --level everything --hosts any --yes",
                        "python3 /x/observer.py enable --hosts=any --yes", "python3 /x/setup.py observer --level everything --hosts any --yes",
                        "python3 /x/setup.py observer --hosts 'any' --dry-run", "python3 /x/publish.py --to octo-org/octo-repo --branch pages",
                        "cd /x && python3 publish.py --list", "python3 /x/observer.py release abc", "python3 /x/observer.py disable"):
            for extra in ({}, {"agent_id": "a1", "agent_type": "general-purpose"}):
                out = self.decide(command, **extra)
                self.assertIsNotNone(out, (command, extra))
                self.assertEqual("deny", out["hookSpecificOutput"]["permissionDecision"])
                self.assertEqual(pretool_guard.USERS_DENIAL, out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_command_over_several_lines_or_with_a_shortened_option_is_stopped_too(self):
        # The 1.7.0 review, N1: a line break (with a quote that is never closed, so that the shell's words cannot be told) and a
        # shortened option (argparse read --ho as --hosts before the parsers refused abbreviations).
        for command in ("python3 /x/observer.py \\\n  trust-ancestor --failing --yes",
                        "python3 /x/observer.py 'a\ntrust-ancestor --failing --yes",
                        "python3 /x/setup.py observer --level everything \\\n  --hosts any --yes",
                        "python3 /x/setup.py observer 'a\n --hosts any",
                        "python3 /x/observer.py enable --model sonnet --level everything --host any --yes",
                        "python3 /x/observer.py enable --ho any --yes", "python3 /x/observer.py enable --hos=any --yes",
                        "python3 /x/setup.py observer --level everything --ho 'any' --yes"):
            out = self.decide(command)
            self.assertIsNotNone(out, command)
            self.assertEqual(pretool_guard.USERS_DENIAL, out["hookSpecificOutput"]["permissionDecisionReason"], command)

    def test_the_parsers_of_observer_and_setup_take_no_shortened_option(self):
        import argparse
        import setup
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for argv in (["enable", "--model", "sonnet", "--ho", "any"], ["enable", "--model", "sonnet", "--host=any"],
                         ["status", "--source"], ["trust-ancestor", "--fail"]):
                with self.assertRaises(SystemExit, msg=argv):
                    observer.parser().parse_args(argv)
        self.assertIn("unrecognized arguments", stderr.getvalue())
        args = observer.parser().parse_args(["enable", "--model", "sonnet", "--hosts", "any", "--yes"])   # the whole option
        self.assertEqual("any", args.hosts)
        # setup.py builds its parser inside main: take it as it is parsed, and look at it and at every sub-command
        seen = {}

        def take(parser, *args, **kwargs):
            seen["parser"] = parser
            raise SystemExit(0)
        with mock.patch.object(setup.Parser, "parse_args", take):
            with self.assertRaises(SystemExit):
                setup.main(["preflight"])

        def every(parser):
            yield parser
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for child in action.choices.values():
                        yield from every(child)
        parsers = list(every(seen["parser"]))
        self.assertGreater(len(parsers), 15)
        self.assertEqual([], [parser.prog for parser in parsers if parser.allow_abbrev])

    def test_the_reads_and_the_grants_go_through(self):
        for command in ("python3 /x/observer.py status", "python3 /x/observer.py pending --waiting",
                        "python3 /x/observer.py enable --model sonnet --level everything --hosts shared --yes",
                        "python3 /x/setup.py questions", "git commit -m 'Publish notes about the observer'",
                        "python3 /x/observer.py status --source-details", "ls /home/octocat/publish"):
            self.assertIsNone(self.decide(command), command)

    def test_the_list_equals_the_terminal_rows_of_the_hooks_table(self):
        self.assertEqual(tuple(sorted(pretool_guard.USERS_ONLY)), observer_hooks.TERMINAL_ONLY)
        self.assertEqual(sorted(pretool_guard.USERS_ONLY), list(pretool_guard.USERS_ONLY))

    def test_the_wrapper_starts_python_for_publish_and_this_file_does_not_import_the_hooks(self):
        shell = (ROOT / "skills" / "recortex" / "scripts" / "run_pretool_guard.sh").read_text()
        self.assertIn("*publish.py*", shell)
        source = (ROOT / "skills" / "recortex" / "scripts" / "pretool_guard.py").read_text()
        self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+observer_hooks\b")


class UnmeasuredTests(tu.SubagentBox):
    """A subagent whose transcript did not pass the ownership checks leaves one fixed code, not silence."""

    def foreign_native(self):
        return seen_as(self.native, uid=FOREIGN, mode=0o755)

    def test_a_refused_transcript_leaves_a_note_and_usage_show_says_so_and_a_measured_run_removes_it(self):
        self.install()
        with self.foreign_native():
            self.stop()
            self.assertEqual([], self.rows("agents"))
            note = usage.unmeasured(tu.ts.REPO)
            self.assertEqual("source-owner", note[1])
            line = usage.UNMEASURED_LINE % "source-owner"
            self.assertIn("  " + line, usage.lines(usage.report(tu.ts.REPO, tu.ts.EXAMPLE_CONFIG), 7, "source-owner"))
            self.assertEqual("The plugin's subagents could not be measured on this machine: their transcripts did not pass "
                             "the ownership checks (source-owner). Run hooks.py --doctor.", line)
            sources.trust_add([str(self.native)], self.project)
            self.stop()
            self.assertEqual(1, len(self.rows("agents")))
            self.assertIsNone(usage.unmeasured(tu.ts.REPO))

    def test_the_note_holds_a_time_and_a_code_of_the_fixed_set_only(self):
        for code in ("source-owner", "source-unavailable"):
            self.assertTrue(usage.note_unmeasured(tu.ts.REPO, code, now=tu.NOW))
        for code in ("source-missing", "<script>", None, 5, "source-owner\n"):
            self.assertFalse(usage.note_unmeasured(tu.ts.REPO, code), code)
        self.assertEqual((tu.NOW, "source-unavailable"), usage.unmeasured(tu.ts.REPO))
        folder = storage.folder(storage.state_top(), storage.repo_folder(tu.ts.REPO))
        path = os.path.join(folder, usage.UNMEASURED)
        for text in ('{"at": 1, "code": "source-owner", "more": 1}', '{"at": "1", "code": "source-owner"}', "[]", "{",
                     '{"at": 5, "code": "<img>"}', '{"at": 99999999999, "code": "source-owner"}'):
            storage.write_file(path, text)
            self.assertIsNone(usage.unmeasured(tu.ts.REPO), text)

    def test_a_note_that_cannot_be_written_never_fails_the_hook(self):
        self.install()
        with self.foreign_native(), mock.patch.object(usage.storage, "write_file", side_effect=OSError("full")):
            self.stop()
        self.assertEqual([], self.rows("agents"))


class UnreadableAnswerTests(CommandBox):
    """6.1: when the user's answer cannot be read here, the refusal says so, and asking again is not what it asks for."""

    ENABLE = "python3 /x/skills/recortex/scripts/observer.py enable --model sonnet --level everything --yes"

    def hook_data(self):
        return {"tool_name": "Bash", "tool_input": {"command": self.ENABLE}, "session_id": "session-1",
                "transcript_path": str(self.path), "cwd": str(self.project)}

    def folder_key(self):
        import observer_runtime
        return observer_runtime.folder_key(str(self.project))

    def enable(self):
        return self.run_cli("enable", "--model", "sonnet", "--level", "everything", "--yes")

    def test_the_hook_leaves_a_note_for_each_code_and_a_readable_transcript_clears_it(self):
        import observer_grants
        for code in observer_grants.UNREADABLE_CODES:
            with mock.patch.object(sources, "answered", side_effect=sources.SourceError(code)):
                observer_grants.record_from_hook(self.hook_data())
            self.assertEqual(code, observer_grants.unread_reason(self.folder_key(), str(self.project)), code)
        with mock.patch.object(sources, "answered", return_value=None):
            observer_grants.record_from_hook(self.hook_data())
        self.assertEqual("", observer_grants.unread_reason(self.folder_key(), str(self.project)))

    def test_the_set_of_codes_is_the_sources_own_and_a_code_off_it_leaves_no_note(self):
        import observer_grants
        self.assertEqual(sources.UNREADABLE_CODES, observer_grants.UNREADABLE_CODES)
        self.assertEqual(("source-owner", "source-permissions", "source-symlink", "source-path", "source-file",
                          "source-changed", "source-incomplete", "source-unavailable", "source-config"),
                         observer_grants.UNREADABLE_CODES)
        with mock.patch.object(sources, "answered", side_effect=sources.SourceError("source-truncated")):
            observer_grants.record_from_hook(self.hook_data())
        self.assertFalse(observer_grants.note_unreadable(self.folder_key(), "<markup>"))
        self.assertEqual("", observer_grants.unread_reason(self.folder_key(), str(self.project)))

    def test_the_note_lasts_ten_minutes(self):
        import observer_grants
        observer_grants.note_unreadable(self.folder_key(), "source-owner", now=1000.0)
        self.assertEqual("source-owner", observer_grants.unread_reason(self.folder_key(), str(self.project), now=1500.0))
        self.assertEqual("", observer_grants.unread_reason(self.folder_key(), str(self.project), now=1000.0 + 601))

    def test_enable_says_the_answer_cannot_be_read_when_the_hook_could_not_read_it(self):
        import observer_grants
        with self.foreign():
            observer_grants.record_from_hook(self.hook_data())
            code, value = self.enable()
            self.assertEqual((1, "enable_answer_unreadable", "source-owner"), (code, value["code"], value["reason"]))
            self.assertEqual(observer_grants.UNREADABLE % "source-owner", value["note"])
            self.assertIn("observer.py trust-ancestor --failing", value["note"])
            self.assertFalse(value["approved"])

    def test_the_note_sends_to_trust_ancestor_only_for_the_codes_it_can_mend(self):
        # The 1.7.0 review, N8: a link, a path that is no folder or a transcript that changed is not covered by trust-ancestor.
        import observer_grants
        for code in observer_grants.UNREADABLE_CODES:
            note = observer_grants.unreadable_note(code)
            self.assertIn("(%s)" % code, note)
            if code in sources.TRUSTABLE:
                self.assertEqual(observer_grants.UNREADABLE % code, note)
                self.assertIn("observer.py trust-ancestor --failing", note)
            else:
                self.assertEqual(observer_grants.UNREADABLE_OTHER % code, note, code)
                self.assertNotIn("trust-ancestor --failing", note)
                self.assertIn("observer.py status --source-details", note)
                self.assertIn("CLAUDE_CONFIG_DIR", note)
        self.assertEqual(("source-owner", "source-permissions"), sources.TRUSTABLE)

    def test_a_refusal_for_a_link_says_so_with_the_note_that_does_not_mention_trust_ancestor(self):
        import observer_grants
        with mock.patch.object(observer_grants, "unread_reason", return_value="source-symlink"):
            code, value = self.enable()
        self.assertEqual((1, "enable_answer_unreadable", "source-symlink"), (code, value["code"], value["reason"]))
        self.assertEqual(observer_grants.unreadable_note("source-symlink"), value["note"])
        self.assertNotIn("trust-ancestor --failing", value["note"])

    def test_enable_says_the_answer_cannot_be_read_from_the_capture_check_alone(self):
        with self.foreign():   # no hook ran, the transcript folder fails the check now
            code, value = self.enable()
            self.assertEqual((1, "enable_answer_unreadable"), (code, value["code"]))

    def test_with_a_readable_transcript_and_no_answer_it_is_still_the_old_refusal(self):
        import observer_grants
        code, value = self.enable()
        self.assertEqual((1, "enable_unanswered"), (code, value["code"]))
        self.assertEqual(observer_grants.UNANSWERED, value["note"])
        self.assertNotIn("reason", value)

    def test_a_folder_with_no_transcript_yet_is_not_an_unreadable_answer(self):
        import observer_grants
        shutil.rmtree(self.folder)
        self.assertEqual("", observer_grants.unread_reason(self.folder_key(), str(self.project)))
        self.assertEqual("enable_unanswered", self.enable()[1]["code"])

    def test_the_refusal_of_a_drop_a_later_and_a_close_says_so_too(self):
        import observer_grants
        import observer_waiting
        item = {"kind": "task_open", "short": "abcd1234", "key": "a" * 32}
        fake = mock.Mock()
        fake.take_grant.return_value = False
        with self.foreign(), mock.patch.object(observer_waiting, "find_any", return_value=item):
            value = observer.declined(fake, "abcd1234", "no")
            self.assertEqual(("answer_unreadable", "source-owner", observer_grants.UNREADABLE % "source-owner"),
                             (value["code"], value["reason"], value["note"]))
            fake.waiting.return_value = [item]
            self.assertEqual("later_answer_unreadable", observer_waiting.later(fake, "abcd1234", 1.0)["code"])
            close = {"kind": "task_close", "short": "abcd1234", "key": "b" * 32, "issue": 5}
            fake.waiting.return_value = [close]
            fake.has_grant.return_value = False
            value = observer_waiting.act(fake, "octo-org/octo-repo", {}, "abcd1234", lambda issue: {})
            self.assertEqual(("close_answer_unreadable", "source-owner"), (value["code"], value["reason"]))
        with mock.patch.object(observer_waiting, "find_any", return_value=item):   # readable: the old codes
            self.assertEqual({"code": "answer_unanswered", "note": observer_waiting.UNANSWERED},
                             observer.declined(fake, "abcd1234", "no"))
            fake.waiting.return_value = [item]
            self.assertEqual({"code": "later_unanswered", "note": observer_waiting.UNANSWERED},
                             observer_waiting.later(fake, "abcd1234", 1.0))

    def test_only_a_refusal_for_want_of_an_answer_is_changed(self):
        import observer_waiting
        with self.foreign():
            self.assertEqual({"code": "not_waiting"}, observer_waiting._unreadable({"code": "not_waiting"}))
            self.assertEqual({"code": "opened", "x": 1}, observer_waiting._unreadable({"code": "opened", "x": 1}))

    def test_the_setup_chain_prints_the_unreadable_note_and_not_the_old_one(self):
        import observer_grants
        import setup
        out = io.StringIO()
        with self.foreign(), contextlib.redirect_stdout(out), mock.patch.object(observer, "on_terminal", return_value=False):
            setup.observer_step({"repo": "octo-org/octo-repo", "observer": {"mode": "automatic", "model": "sonnet",
                                                                           "level": "everything", "sort_model": "haiku",
                                                                           "strong": ""}}, str(self.project), sys.executable,
                                False)
        self.assertIn("observer: not turned on. " + observer_grants.UNREADABLE % "source-owner", out.getvalue())
        self.assertNotIn(observer_grants.UNANSWERED, out.getvalue())
        out = io.StringIO()   # a code that trust-ancestor cannot mend gets the note that says what to look at instead
        with mock.patch.object(observer_grants, "unread_reason", return_value="source-symlink"), \
                contextlib.redirect_stdout(out), mock.patch.object(observer, "on_terminal", return_value=False):
            setup.observer_step({"repo": "octo-org/octo-repo", "observer": {"mode": "automatic", "model": "sonnet",
                                                                           "level": "everything", "sort_model": "haiku",
                                                                           "strong": ""}}, str(self.project), sys.executable,
                                False)
        self.assertIn("observer: not turned on. " + observer_grants.UNREADABLE_OTHER % "source-symlink", out.getvalue())


if __name__ == "__main__":
    unittest.main()

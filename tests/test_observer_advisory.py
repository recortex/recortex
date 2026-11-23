"""Deterministic advisory eligibility, separate receipts and explicit opt-in; no live calls."""
import copy
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests import test_observer_context as fixtures
import observer
import observer_advisory as advisory
import observer_runtime as runtime
import observer_store


class Controls:
    def __init__(self):
        self.values = {}

    @contextlib.contextmanager
    def edit_controls(self):
        yield self.set_control

    def get_control(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def set_control(self, key, value):
        self.values[key] = copy.deepcopy(value)


class ObserverAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ObserverContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.memory(1, "failure", "parser loses quoted lines", fields=[("Cause", "The splitter cuts quoted newlines")])
        self.fixture.memory(2, plain="use streaming for quoted lines", fields=[("Decision", "Keep strict quoting; do not retry the splitter")])
        self.fixture.link(2, "because", 1)
        self.store = Controls()
        self.query = "Should we use the splitter for quoted lines?"

    def packet(self, **kwargs):
        return self.fixture.packet(query=kwargs.pop("query", self.query), **kwargs)

    def tell(self, mode="deliver", packet=None, query=None):
        return advisory.deliver(self.store, packet or self.packet(), query or self.query, "session", mode=mode, now=100)

    def test_default_off_has_no_output_or_receipt(self):
        self.assertEqual("off", runtime.settings({})["advisory_mode"])
        self.assertEqual("", self.tell("off"))
        self.assertEqual({}, self.store.values)

    def test_existing_approval_without_advisory_field_stays_valid_and_off(self):
        old = runtime.settings({"observer": {"enabled": True, "level": "notes", "mode": "automatic",
                                             "model": "haiku", "publisher_id": "a" * 32}})
        old.pop("advisory_mode")
        entry = {"repo": fixtures.REPO, "root": "/invented/project", "publisher_id": "a" * 32, "settings": old}
        with mock.patch.object(runtime, "folder_key", return_value="/invented/project"), \
                mock.patch.object(runtime, "publisher_id", return_value="a" * 32), \
                mock.patch.object(runtime, "stored_approval", return_value=entry):
            loaded = runtime.local_config({"repo": fixtures.REPO})
            self.assertTrue(runtime.approval_valid(fixtures.REPO, loaded))
            self.assertEqual("off", runtime.settings(loaded)["advisory_mode"])

    def test_shadow_never_issues_or_consumes_a_delivered_receipt(self):
        self.assertEqual("", self.tell("shadow"))
        self.assertIn(advisory.SHADOW, self.store.values)
        self.assertNotIn(advisory.DELIVERED, self.store.values)
        text = self.tell()
        for words in ("#issuecomment-2", "#issuecomment-1", "briefly tell the user once", "material scope or uncertainty",
                      "Continue the task without asking for confirmation", "not newly verified"):
            self.assertIn(words, text)
        self.assertIn(advisory.DELIVERED, self.store.values)
        self.assertEqual("", self.tell())
        self.assertNotIn(self.query, json.dumps(self.store.values))

    def test_paraphrases_suppress_but_identifiable_task_and_evidence_changes_are_distinct(self):
        self.assertTrue(self.tell())
        self.assertEqual("", self.tell(query="For quoted lines, should we use the splitter?"))
        self.assertTrue(self.tell(packet=self.packet(issue=3)))
        self.assertEqual("", self.tell(packet=self.packet(issue=3), query="Remind me why we dropped the splitter"))
        self.fixture.db.execute("UPDATE event_fields SET text='Keep strict quoting and preserve empty lines' "
                                "WHERE comment=2 AND label='Decision'")
        self.assertTrue(self.tell())
        self.assertEqual("", self.tell())

    def test_sync_time_alone_does_not_repeat_advice(self):
        self.assertTrue(self.tell())
        self.fixture.db.execute("UPDATE meta SET value='2026-03-20T09:00:00Z' WHERE key='synced_at'")
        self.assertEqual("", self.tell())

    def test_no_relationship_no_relevant_query_or_incomplete_sync_is_silent(self):
        for query in ("prepare the budget spreadsheet", "Should we use the spreadsheet?",
                      "Please remind me of the previous decision", "What should I do next?"):
            self.assertEqual([], self.packet(query=query)["advisories"], query)
        self.fixture.db.execute("UPDATE meta SET value='0' WHERE key='sync_complete'")
        self.assertEqual("", self.tell())
        self.fixture.db.execute("DELETE FROM links")
        self.assertEqual([], self.packet()["advisories"])

    def test_stale_complete_future_or_unknown_snapshot_does_not_issue_advice(self):
        for stamp in ("2026-03-10T10:00:00Z", "2026-03-21T10:00:00Z", ""):
            self.fixture.db.execute("UPDATE meta SET value=? WHERE key='synced_at'", (stamp,))
            self.assertEqual("", self.tell(), stamp)
        self.assertEqual({}, self.store.values)

    def test_one_matching_action_does_not_turn_an_unrelated_task_into_advice(self):
        self.fixture.db.execute("UPDATE event_fields SET text='Exclude the parser version that loses quoted lines' "
                                "WHERE comment=2 AND label='Decision'")
        self.assertEqual([], self.packet(query="Should we exclude the expensive spreadsheet?")["advisories"])

    def test_missing_or_changed_authority_and_oversized_evidence_are_silent(self):
        self.fixture.db.execute("UPDATE comments SET author='outsider' WHERE id=1")
        self.fixture.db.execute("UPDATE events SET author='outsider' WHERE comment=1")
        self.assertEqual([], self.packet()["advisories"])
        self.fixture.db.execute("UPDATE comments SET author='octocat' WHERE id=1")
        self.fixture.db.execute("UPDATE events SET author='octocat' WHERE comment=1")
        self.fixture.db.execute("UPDATE event_fields SET text=? WHERE comment=1 AND label='Cause'", ("long cause " * 2000,))
        self.assertEqual([], self.packet(max_chars=2000)["advisories"])

    def test_a_replaced_decision_is_not_recommended(self):
        self.fixture.memory(3, plain="use a different input format")
        self.fixture.link(3, "replaces", 2)
        self.assertEqual([], self.packet()["advisories"])

    def test_receipts_are_bounded_and_status_contains_no_task_or_record_text(self):
        packet = self.packet()
        for n in range(120):
            packet["scope"] = n + 1
            advisory.deliver(self.store, packet, "task %d" % n, "session", mode="deliver", now=n)
        self.assertEqual(100, len(self.store.values[advisory.DELIVERED]))
        self.assertEqual({"retained_receipts": 100, "last_at": 119}, advisory.status(self.store)["delivered"])

    def test_history_contains_only_cited_identifiers_and_cannot_be_muted_by_guess_or_legacy_receipt(self):
        packet = self.packet()
        item = packet["advisories"][0]
        legacy = advisory._digest(["session", "", ["topic", item["issue"]], item["fingerprint"]])
        self.store.set_control(advisory.DELIVERED, {legacy: 71})
        self.assertEqual("", self.tell())
        self.assertEqual([], advisory.history(self.store))
        with self.assertRaises(KeyError):
            advisory.feedback(self.store, legacy, True)
        self.assertEqual("", self.tell("shadow"))
        self.assertEqual([], advisory.history(self.store))
        self.fixture.db.execute("UPDATE comments SET issue=3 WHERE id=1")
        self.fixture.db.execute("UPDATE events SET issue=3 WHERE comment=1")
        self.assertTrue(self.tell())
        row, = advisory.history(self.store)
        self.assertEqual(("decision_failure", "topic", 2, 100, False),
                         (row["reason"], row["scope_kind"], row["scope_issue"], row["at"], row["muted"]))
        self.assertEqual([{"kind": "decision", "issue": 2, "comment": 2},
                          {"kind": "failure", "issue": 3, "comment": 1}], row["sources"])
        self.assertNotIn("quoted lines", json.dumps(self.store.values))
        self.assertNotIn(self.query, json.dumps(self.store.values))
        with self.assertRaises(ValueError):
            advisory.feedback(self.store, row["id"], 1)

    def test_mute_applies_across_sessions_only_for_this_folder_task_branch_and_evidence_revision(self):
        packet = self.packet(issue=3)
        item = packet["advisories"][0]
        item["branch"] = "branch-" + "a" * 24
        def send(session, root="/invented/one"):
            return advisory.deliver(self.store, packet, self.query, session, mode="deliver", now=50, root=root)
        self.assertTrue(send("one"))
        row, = advisory.history(self.store, "/invented/one")
        for _ in range(2):
            self.assertTrue(advisory.feedback(self.store, row["id"], True, root="/invented/one")["muted"])
        self.assertFalse(send("two"))
        self.assertFalse(advisory.history(self.store, "/invented/two"))
        with self.assertRaises(KeyError):
            advisory.feedback(self.store, row["id"], True, root="/invented/two")
        self.assertTrue(send("one", "/invented/two"))
        item["branch"] = "branch-" + "b" * 24
        self.assertTrue(send("one"))
        item["branch"] = row["branch"]
        packet["scope"] = 4
        self.assertTrue(send("one"))
        packet["scope"] = 3
        item["fingerprint"] = "e" * 64
        self.assertTrue(send("one"))
        item["fingerprint"] = self.packet(issue=3)["advisories"][0]["fingerprint"]
        self.assertFalse(advisory.feedback(self.store, row["id"], False, root="/invented/one")["muted"])
        self.assertTrue(send("two"))
        self.assertFalse(send("two"))

    def test_history_retains_mutes_across_rotation_and_undo_is_idempotent(self):
        packet = self.packet()
        with mock.patch.object(advisory, "KEPT", 2), mock.patch.object(runtime, "folder_key", return_value="/invented/one"):
            def send(n):
                packet["scope"] = n
                advisory.deliver(self.store, packet, self.query, "one", mode="deliver", now=n)
                return next(row for row in advisory.history(self.store) if row["scope_issue"] == n)["id"]
            ids = [send(1), send(2)]
            for ident in ids:
                advisory.feedback(self.store, ident, True)
            newest = send(3)
            with self.assertRaisesRegex(ValueError, "mute_limit"):
                advisory.feedback(self.store, newest, True)
            for n in range(4, 9):
                send(n)
            self.assertEqual({1, 2, 7, 8}, {row["scope_issue"] for row in advisory.history(self.store)})
            for _ in range(2):
                self.assertFalse(advisory.feedback(self.store, ids[0], False)["muted"])
            self.assertEqual({1, 2, 8}, {row["scope_issue"] for row in advisory.history(self.store)})

    def test_corrupt_history_never_returns_project_text_or_disables_an_existing_mute(self):
        self.assertTrue(self.tell())
        row, = advisory.history(self.store)
        row["note"] = "Untrusted arbitrary text"
        key = advisory._folder_control(None)
        self.store.set_control(key, {row["id"]: row})
        with self.assertRaisesRegex(ValueError, "invalid_advisory_history"):
            advisory.history(self.store)
        with self.assertRaisesRegex(ValueError, "invalid_advisory_history"):
            advisory.feedback(self.store, row["id"], False)
        with self.assertRaisesRegex(ValueError, "invalid_advisory_history"):
            self.tell()
        self.assertIn("note", self.store.get_control(key)[row["id"]])

    def test_atomic_hook_issuance_and_failure_rollback_on_two_connections(self):
        packet = self.packet()
        with tempfile.TemporaryDirectory(dir=Path(fixtures.ROOT) / ".tmp") as temp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": temp, "CLAUDE_PROJECT_DIR": temp}, clear=True):
            with observer_store.Store(fixtures.REPO, config={}) as store:
                original = store._write_control
                def fail_history(key, value):
                    if key.startswith("advisory_history_"):
                        raise observer_store.StoreError("capacity")
                    original(key, value)
                with mock.patch.object(store, "_write_control", side_effect=fail_history), \
                        self.assertRaises(observer_store.StoreError):
                    advisory.deliver(store, packet, self.query, "one", mode="deliver")
                self.assertEqual([], advisory.history(store))
                self.assertEqual({}, store.get_control(advisory.DELIVERED, {}))
            barrier = threading.Barrier(2)
            def issue():
                with observer_store.Store(fixtures.REPO, create=False, config={}) as store:
                    barrier.wait(timeout=5)
                    return advisory.deliver(store, packet, self.query, "one", mode="deliver")
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(issue) for _ in range(2)]
                self.assertEqual(1, sum(bool(f.result()) for f in futures))
            with observer_store.Store(fixtures.REPO, create=False, config={}) as store:
                row, = advisory.history(store)
                self.assertTrue(advisory.feedback(store, row["id"], True)["muted"])
            with observer_store.Store(fixtures.REPO, create=False, config={}) as store:
                self.assertFalse(advisory.deliver(store, packet, self.query, "two", mode="deliver"))
                self.assertTrue(advisory.history(store)[0]["muted"])
                self.assertEqual(1, len(store.get_control(advisory.DELIVERED)))

    def test_a_session_cannot_mute_a_reminder_or_read_a_local_history_after_approval_is_revoked(self):
        config = {"repo": fixtures.REPO}
        with mock.patch.object(observer.recortex, "load_config", return_value=config), \
                mock.patch.object(runtime, "local_config", return_value=config), \
                mock.patch.object(observer.recortex, "resolve_repo", return_value=fixtures.REPO), \
                mock.patch.object(observer.recortex, "allowed_users", return_value={"octocat"}), \
                mock.patch.object(observer, "on_terminal", return_value=False), \
                mock.patch.object(runtime, "approval_valid", return_value=False), \
                mock.patch.object(observer_store, "Store") as opened, mock.patch.object(observer, "emit") as emit:
            self.assertEqual(1, observer.main(["advisory", "mute", "f" * 64]))
            self.assertEqual("approval", emit.call_args[0][0]["code"])
            self.assertEqual(1, observer.main(["advisory", "list"]))
            self.assertEqual("approval", emit.call_args[0][0]["code"])
            opened.assert_not_called()

    def test_opt_in_preview_is_read_only_and_choice_preserves_other_settings(self):
        config = {"observer": {"enabled": True, "level": "notes", "mode": "shadow", "model": "haiku"}}
        with mock.patch.object(runtime, "approval_valid", return_value=True), \
                mock.patch.object(runtime, "stored_approval", return_value={"approved_at": 12, "given": "terminal"}), \
                mock.patch.object(runtime, "approve") as save:
            self.assertFalse(observer.advisory(fixtures.REPO, config, "deliver")["approved"])
            save.assert_not_called()
            self.assertTrue(observer.advisory(fixtures.REPO, config, "deliver", yes=True)["approved"])
            options = save.call_args[0][1]["observer"]
            self.assertEqual(("deliver", "notes", "shadow"), (options["advisory_mode"], options["level"], options["mode"]))
        with self.assertRaises(ValueError):
            runtime.settings({"observer": {"advisory_mode": "automatic"}})

    def test_project_configuration_cannot_opt_in_and_session_cannot_save_a_choice(self):
        config = {"repo": fixtures.REPO, "observer": {"enabled": True, "advisory_mode": "deliver"}}
        with mock.patch.object(runtime, "stored_approval", return_value=None):
            self.assertEqual("off", runtime.settings(runtime.local_config(config))["advisory_mode"])
        quiet = {"repo": fixtures.REPO, "observer": {"enabled": True}}   # what the folder's own settings say: advisory is off
        with mock.patch.object(observer.recortex, "load_config", return_value=quiet), \
                mock.patch.object(runtime, "local_config", return_value=quiet), \
                mock.patch.object(observer.recortex, "resolve_repo", return_value=fixtures.REPO), \
                mock.patch.object(observer.recortex, "allowed_users", return_value={"octocat"}), \
                mock.patch.object(observer, "on_terminal", return_value=False), \
                mock.patch.object(runtime, "approval_valid", return_value=True), \
                mock.patch.object(runtime, "approve") as save, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, observer.main(["advisory", "deliver", "--yes"]))
            self.assertEqual("advisory_unanswered", json.loads(out.getvalue())["code"])   # the user's answer is what it needs
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()

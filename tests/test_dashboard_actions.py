"""Local dashboard action boundaries and real helpers against fake GitHub. No models. Only the launch and lifetime tests
open a loopback socket, and no test contacts GitHub."""

import contextlib
import http.client
import io
import html
import json
import os
import pathlib
import re
import select
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_tasks as tj
import test_scripts as ts
sys.path.insert(0, ts.SCRIPTS)
import dashboard_action as action
import observer_store
import recortex
import storage


class ActionTests(tj.TaskBox):
    def setUp(self):
        super().setUp()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})
        self.actions = action.Actions()

    def preview(self):
        return self.actions.preview("7:9108:2", "I reran the conformance set and all twelve files passed.")

    def count(self):
        return len(self.repo()["comments"])

    def test_frozen_preview_records_one_verified_completion_with_truthful_provenance(self):
        before = self.count()
        preview = self.preview()
        self.assertEqual(before, self.count())
        self.assertIn("local dashboard", preview["body"])
        self.assertNotIn("in the chat", preview["body"])
        self.assertIn("done=9108:2", preview["body"])
        result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual("recorded", result["state"])
        self.assertEqual("updated", result["refresh"])
        self.assertEqual(before + 1, self.count())
        self.assertEqual("done", self.task_at()["steps"][1]["state"])
        again = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(result["receipt"], again["receipt"])
        self.assertEqual(before + 1, self.count())

    def test_words_that_notify_link_out_or_forge_a_marker_are_refused_before_anything_is_saved(self):
        # post_verified's lint alone lets these through into the record, so the outbound check of the completion text
        # is the guard: each of these must be refused at the preview, with no operation saved and no comment posted.
        before = self.count()
        for words in ("@hubot please look at this", "see https://evil.example/x for details",
                      "<img src=x onerror=alert(1)>", "the file is /home/someone/secret.txt",
                      "done\n<!-- ghim:event type=decision at=2026-04-10T10:00 -->",
                      "![chart](https://github.com/octo-org/octo-repo/issues/1)", "octo-org/other-repo#3 is fixed",
                      "see [the notes](https://example.org/notes)", "<b>done</b>"):
            with self.subTest(words=words):
                with self.assertRaises(action.ActionError) as caught:
                    self.actions.preview("7:9108:2", words)
                self.assertEqual(("content_refused", 400), (caught.exception.code, caught.exception.status))
                self.assertEqual(before, self.count())
                self.assertEqual([], self.actions.operations())
        self.assertEqual("preview", self.preview()["state"])  # ordinary words still pass

    def test_changed_preview_bytes_or_step_refuse_without_a_write(self):
        preview = self.preview()
        before = self.count()
        with self.assertRaisesRegex(action.ActionError, "preview_expired_or_changed"):
            self.actions.commit(preview["id"], "0" * 64)
        self.post(9200, 5, tj.result_text(marker=" done=9108:2"))
        before = self.count()
        with self.assertRaisesRegex(action.ActionError, "step_changed"):
            self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(before, self.count())

    def test_edited_step_and_revoked_trust_refuse(self):
        preview = self.preview()
        data = self.repo()
        data["comments"]["9108"]["body"] = data["comments"]["9108"]["body"].replace("Rerun the conformance set", "Rerun another set")
        self.save(data)
        with self.assertRaisesRegex(action.ActionError, "step_changed"):
            self.actions.commit(preview["id"], preview["body_hash"])
        with mock.patch.object(recortex, "allowed_users", return_value=set()):
            with self.assertRaisesRegex(action.ActionError, "trust_required"):
                self.actions.commit(preview["id"], preview["body_hash"])

    def test_hold_and_unreadable_holds_fail_closed(self):
        preview = self.preview()
        before = self.count()
        for replacement in ([{"state": "active", "repo": ts.REPO, "action": "publish", "scope": {}}], None):
            kwargs = {"return_value": replacement} if replacement is not None else {"side_effect": observer_store.StoreError("unreadable")}
            with mock.patch.object(observer_store, "read_holds", **kwargs):
                with self.assertRaises(action.ActionError):
                    self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(before, self.count())

    def test_unrelated_command_hold_does_not_block_but_exact_publication_does(self):
        preview = self.preview()
        hold = {"state": "active", "repo": ts.REPO, "action": "command", "scope": {}, "command": "git push"}
        with mock.patch.object(observer_store, "read_holds", return_value=[hold]):
            self.actions.holds(7, self.actions.load(preview["id"]))
        hold["command"] = recortex.show_command(["issue", "comment", "7", "--repo", ts.REPO, "--body-file",
                                                 os.path.join(self.actions.folder, preview["id"] + ".md")])
        before = self.count()
        with mock.patch.object(observer_store, "read_holds", return_value=[hold]):
            with self.assertRaisesRegex(action.ActionError, "held"):
                self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(before, self.count())

    def test_lost_response_is_reconciled_without_a_second_append_after_restart(self):
        preview = self.preview()
        before = self.count()
        original = self.actions.post
        def lost(operation, dry_run=False):
            result = original(operation, dry_run)
            if not dry_run:
                raise recortex.GhError("reply lost")
            return result
        with mock.patch.object(self.actions, "post", side_effect=lost), \
                mock.patch.object(self.actions, "_reconcile", side_effect=lambda op: self.actions.public(op)):
            result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual("uncertain", result["state"])
        restarted = action.Actions()
        result = restarted.reconcile(preview["id"])
        self.assertEqual("recorded", result["state"])
        self.assertEqual(before + 1, self.count())

    def test_unconfirmed_send_is_never_automatically_retried(self):
        preview = self.preview()
        before = self.count()
        original = self.actions.post
        def failed(operation, dry_run=False):
            if dry_run:
                return original(operation, True)
            raise recortex.GhError("disconnected")
        with mock.patch.object(self.actions, "post", side_effect=failed) as post:
            result = self.actions.commit(preview["id"], preview["body_hash"])
            self.assertEqual("uncertain", result["state"])
            calls = post.call_count
            self.actions.commit(preview["id"], preview["body_hash"])
            self.assertEqual(calls, post.call_count)
        with self.assertRaisesRegex(action.ActionError, "reconcile_first"):
            self.preview()
        self.assertEqual(before, self.count())

    def test_refresh_failure_does_not_erase_recorded_receipt(self):
        preview = self.preview()
        original = self.actions.sync
        calls = []
        def sync():
            calls.append(1)
            if len(calls) > 1:
                raise recortex.GhError("offline after acceptance")
            return original()
        with mock.patch.object(self.actions, "sync", side_effect=sync):
            result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(("recorded", "failed"), (result["state"], result["refresh"]))

    def test_existing_previews_cannot_bypass_another_launches_pending_send(self):
        first = self.preview()
        second = self.preview()
        other = action.Actions()
        third = other.preview("7:9108:2", "I independently checked all twelve files.")
        before = self.count()
        pending = self.actions.load(first["id"])
        for state in ("sending", "uncertain"):
            pending["state"] = state
            self.actions.save(pending)
            for backend, preview in ((self.actions, second), (other, third)):
                with self.subTest(state=state, same_launch=backend is self.actions), \
                        mock.patch.object(backend, "post") as post:
                    with self.assertRaisesRegex(action.ActionError, "reconcile_first"):
                        backend.commit(preview["id"], preview["body_hash"])
                    post.assert_not_called()
                    self.assertEqual("preview", backend.load(preview["id"])["state"])
        self.assertEqual(before, self.count())

    def test_account_change_during_task_render_preserves_receipt_without_body_write(self):
        preview = self.preview()
        before = self.repo()["issues"]["7"]["body"]
        original = action.tasks.render_task
        def switched(*args, **kwargs):
            # The newly active account is trusted too, but was not the account
            # bound to this operation. Change it after the render's live read.
            read = recortex.read_issue_body
            def changed(*read_args, **read_kwargs):
                body = read(*read_args, **read_kwargs)
                account.return_value = ("ready", "other")
                return body
            with mock.patch.object(recortex, "gh_account", return_value=("ready", "octocat")) as account, \
                    mock.patch.object(recortex, "allowed_users", return_value={"octocat", "other"}), \
                    mock.patch.object(recortex, "read_issue_body", side_effect=changed):
                return original(*args, **kwargs)
        with mock.patch.object(action.tasks, "render_task", side_effect=switched):
            result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(("recorded", "failed"), (result["state"], result["refresh"]))
        self.assertEqual(before, self.repo()["issues"]["7"]["body"])
        self.assertEqual(result["receipt"], self.actions.commit(preview["id"], preview["body_hash"])["receipt"])

    def test_trust_revoked_during_task_render_preserves_receipt_without_body_write(self):
        preview = self.preview()
        before = self.repo()["issues"]["7"]["body"]
        original = action.tasks.render_task
        def revoked(*args, **kwargs):
            read = recortex.read_issue_body
            def changed(*read_args, **read_kwargs):
                body = read(*read_args, **read_kwargs)
                allowed.return_value = set()
                return body
            with mock.patch.object(recortex, "allowed_users", return_value={"octocat"}) as allowed, \
                    mock.patch.object(recortex, "read_issue_body", side_effect=changed):
                return original(*args, **kwargs)
        with mock.patch.object(action.tasks, "render_task", side_effect=revoked):
            result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(("recorded", "failed"), (result["state"], result["refresh"]))
        self.assertEqual(before, self.repo()["issues"]["7"]["body"])

    def test_account_change_during_dry_run_cannot_send(self):
        preview = self.preview()
        before = self.count()
        original = self.actions.post
        def switched(operation, dry_run=False):
            result = original(operation, dry_run)
            account.return_value = "other"
            return result
        with mock.patch.object(self.actions, "account", return_value="octocat") as account, \
                mock.patch.object(self.actions, "post", side_effect=switched) as post:
            with self.assertRaisesRegex(action.ActionError, "account_changed"):
                self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(1, post.call_count)
        self.assertEqual(before, self.count())

    def test_expired_and_restarted_previews_cannot_be_confirmed(self):
        preview = self.preview()
        with mock.patch.object(action.time, "time", return_value=preview["created"] + 601):
            with self.assertRaisesRegex(action.ActionError, "preview_expired_or_changed"):
                self.actions.commit(preview["id"], preview["body_hash"])
        with self.assertRaisesRegex(action.ActionError, "preview_expired_or_changed"):
            action.Actions().commit(preview["id"], preview["body_hash"])

    def test_ledger_tampering_and_parallel_writer_are_refused(self):
        preview = self.preview()
        path = self.actions.path(preview["id"])
        signed = json.loads(storage.read_file(path))
        signed["operation"]["target"]["issue"] = 5
        storage.write_file(path, json.dumps(signed))
        with self.assertRaisesRegex(action.ActionError, "operation_invalid"):
            self.actions.commit(preview["id"], preview["body_hash"])
        with observer_store._exclusive_lock(self.actions.locks, "worker.lock"):
            with self.assertRaisesRegex(action.ActionError, "busy"):
                self.preview()

    def test_only_expired_unsent_previews_are_pruned(self):
        preview = self.preview()
        old = self.actions.load(preview["id"])
        old["created"] -= 601
        self.actions.save(old)
        pending = dict(old, id="a" * 32, state="uncertain")
        # A receipt's signed identity must remain valid after restart; its body
        # is irrelevant to pruning and is never sent in this test.
        self.actions.save(pending)
        self.actions.prune_previews()
        self.assertFalse(os.path.exists(self.actions.path(old["id"])))
        self.assertEqual("uncertain", self.actions.load(pending["id"])["state"])

    def test_cannot_send_when_durable_send_intent_cannot_be_saved(self):
        preview = self.preview()
        before = self.count()
        original = self.actions.save
        def save(operation):
            if operation["state"] == "sending":
                raise OSError("disk full")
            return original(operation)
        with mock.patch.object(self.actions, "save", side_effect=save):
            with self.assertRaises(OSError):
                self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(before, self.count())

    def test_partial_sync_and_changed_account_cannot_write(self):
        preview = self.preview()
        before = self.count()
        with mock.patch.object(action.index, "sync", return_value={"complete": False}):
            with self.assertRaisesRegex(action.ActionError, "sync_incomplete"):
                self.actions.commit(preview["id"], preview["body_hash"])
        with mock.patch.object(recortex, "gh_account", return_value=("ready", "untrusted")):
            with self.assertRaisesRegex(action.ActionError, "allowed_account_required"):
                self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(before, self.count())

    def test_authenticated_snapshot_has_scoped_step_links_without_changing_export(self):
        path = os.path.join(action.index.index_dir(ts.REPO), "mind.html")
        before = storage.read_file(path)
        page = self.actions.snapshot("/" + "a" * 32)
        self.assertIn('/' + 'a' * 32 + '/actions#step-7-9108-2', page)
        self.assertIn('/' + 'a' * 32 + '/suggestions', page)
        self.assertIn("Local interactive session.", page)
        self.assertIn("Report completed steps from Work.", page)
        self.assertTrue("connect-src 'none'" in html.unescape(page))
        self.assertEqual(before, storage.read_file(path))

    def test_suggestion_feedback_uses_current_trusted_sources_and_survives_reopen(self):
        import observer_context
        config = dict(self.actions.config, observer={"enabled": True, "advisory_mode": "deliver"})
        db = action.index.connect(ts.REPO, create=False)
        try:
            packet = observer_context.build_packet(db, ts.REPO, self.actions.authority(),
                                                   "Rewrite the parser for quoted lines in the line splitter", issue=7)
        finally:
            db.close()
        self.assertTrue(packet["advisories"])
        with observer_store.Store(ts.REPO, config=config) as store:
            self.assertTrue(action.observer_advisory.deliver(store, packet, "a private prompt", "session A",
                            mode="deliver", root=self.actions.root))
        with mock.patch.object(action.observer_runtime, "local_config", return_value=config), \
                mock.patch.object(action.observer_runtime, "approval_valid", return_value=True):
            data = self.actions.suggestions()
            self.assertEqual("deliver", data["mode"])
            row, = data["suggestions"]
            self.assertEqual(["decision", "failure"], [source["kind"] for source in row["sources"]])
            self.assertIn("#issuecomment-9105", row["sources"][0]["url"])
            self.assertNotIn("a private prompt", json.dumps(data))
            self.assertTrue(self.actions.suggestions(row["id"], True)["muted"])
            self.assertTrue(action.Actions().suggestions()["suggestions"][0]["muted"])
            with mock.patch.object(action.index, "memories", return_value={"items": {}}):
                redacted, = self.actions.suggestions()["suggestions"]
                self.assertEqual({"id", "at", "muted", "available"}, set(redacted))
                self.assertFalse(redacted["available"])
                with self.assertRaisesRegex(action.ActionError, "sources_unavailable"):
                    self.actions.suggestions(row["id"], True)
                self.assertFalse(self.actions.suggestions(row["id"], False)["muted"])
            with self.assertRaisesRegex(action.ActionError, "suggestion_missing"):
                self.actions.suggestions("b" * 64, True)
            with mock.patch.object(self.actions, "authority", side_effect=action.ActionError("trust_required", 403)):
                with self.assertRaisesRegex(action.ActionError, "trust_required"):
                    self.actions.suggestions(row["id"], False)

    def test_suggestion_history_requires_folder_approval_and_an_existing_issue(self):
        with mock.patch.object(action.observer_runtime, "approval_valid", return_value=False), \
                mock.patch.object(observer_store, "Store") as open_store:
            self.assertEqual({"repo": ts.REPO.lower(), "mode": "disabled", "suggestions": []}, self.actions.suggestions())
            with self.assertRaisesRegex(action.ActionError, "observer_disabled"):
                self.actions.suggestions("a" * 64, True)
            open_store.assert_not_called()
        with self.assertRaisesRegex(action.ActionError, "invalid_fields"):
            self.actions.suggestions("malformed", True)

    def test_receipt_with_changed_author_is_uncertain(self):
        preview = self.preview()
        with mock.patch.object(self.actions, "_reconcile", side_effect=lambda op: self.actions.public(op)):
            self.actions.commit(preview["id"], preview["body_hash"])
        data = self.repo()
        comment = next(c for c in data["comments"].values() if "eventkey=" + preview["id"] in c["body"])
        comment["user"] = "untrusted"
        self.save(data)
        before = self.count()
        self.assertEqual("uncertain", self.actions.reconcile(preview["id"])["state"])
        self.assertEqual(before, self.count())

    def test_unresolved_receipt_stays_visible_after_twenty_newer_previews(self):
        preview = self.preview()
        pending = self.actions.load(preview["id"])
        pending["state"] = "uncertain"
        self.actions.save(pending)
        for n in range(25):
            newer = dict(pending, id="%032x" % n, state="preview", created=pending["created"] + n + 1)
            self.actions.save(newer)
        states = self.actions.state()["operations"]
        self.assertEqual(21, len(states))
        self.assertEqual(preview["id"], states[0]["id"])
        self.assertEqual("uncertain", states[0]["state"])
        self.assertEqual({"issue", "comment", "ordinal"}, set(states[0]["target"]))

    def test_recorded_completion_does_not_claim_a_missing_task_list_was_updated(self):
        self.set_body(7, self.body(7).replace("## Plan", "## Other"))
        preview = self.preview()
        result = self.actions.commit(preview["id"], preview["body_hash"])
        self.assertEqual(("recorded", "no_task_list"), (result["state"], result["refresh"]))

    def test_missing_index_is_explicit_and_cli_configuration_failures_are_concise(self):
        with mock.patch.object(action.index, "connect", return_value=None):
            with self.assertRaisesRegex(action.ActionError, "index_unavailable"):
                self.actions.state()
        for config in ({}, {"repo": "not/a/repository"}):
            error = io.StringIO()
            with mock.patch.object(recortex, "load_config", return_value=config), contextlib.redirect_stderr(error):
                self.assertEqual(1, action.main([]))
            self.assertIn("configured project folder", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())


class Terminal(io.StringIO):
    """Standard output as a session sees it (not a terminal) or as a user's terminal shows it."""

    def __init__(self, terminal):
        super().__init__()
        self.terminal = terminal

    def isatty(self):
        return self.terminal


class LaunchTests(tj.TaskBox):
    """The address holds the token, which is the whole capability. It reaches only a terminal; a session that starts
    the service gets the path of a private launch file, and the browser is pointed at that file."""

    TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")

    def setUp(self):
        super().setUp()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})

    def start(self, argv, terminal):
        """Run main() in this process with serve() replaced by a look at the files the service holds while it runs.
        Returns (output, errors, what the service held, the browser's calls)."""
        out, err, held, opened = Terminal(terminal), io.StringIO(), {}, []

        def serving(server):
            held["server"] = server
            for name in os.listdir(held["folder"]):
                if action.LAUNCH.fullmatch(name):
                    path = os.path.join(held["folder"], name)
                    held["files"] = held.get("files", []) + [(path, stat.S_IMODE(os.stat(path).st_mode),
                                                              pathlib.Path(path).read_text(encoding="utf-8"))]
            return "stopped"

        real = action.Actions

        def make():
            made = real()
            held["folder"] = made.folder
            return made

        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(action, "Actions", side_effect=make), \
                mock.patch.object(action, "serve", side_effect=serving), \
                mock.patch.object(action.recortex, "open_page", side_effect=lambda url: opened.append(url) or True):
            code = action.main(argv)
        self.assertEqual(0, code, err.getvalue())
        return out.getvalue(), err.getvalue(), held, opened

    def secrets_of(self, held):
        server = held["server"]
        return [server.token, server.mount.strip("/"), str(server.server_port), server.host]

    def test_a_start_from_a_session_prints_only_the_path_of_a_private_file(self):
        output, errors, held, opened = self.start(["--open"], terminal=False)
        (path, mode, page), = held["files"]
        for secret in self.secrets_of(held):
            self.assertNotIn(secret, output + errors)
        self.assertNotIn("http://", output + errors)
        self.assertIsNone(self.TOKEN.search(output + errors))
        self.assertIn(path, output)
        self.assertEqual(0o600, mode)
        self.assertEqual(held["folder"], os.path.dirname(path))
        server = held["server"]
        address = "http://%s%s/#%s" % (server.host, server.mount, server.token)
        self.assertIn('<meta http-equiv="refresh" content="0;url=%s">' % address, page)
        self.assertEqual([pathlib.Path(path).as_uri()], opened)
        self.assertFalse(os.path.exists(path), "the launch file goes when the service stops")

    def test_a_session_without_open_gets_the_path_and_no_browser_starts(self):
        output, errors, held, opened = self.start([], terminal=False)
        (path, _, _), = held["files"]
        self.assertIn(path, output)
        self.assertIn("open this file in your browser", output)
        self.assertEqual([], opened)
        for secret in self.secrets_of(held):
            self.assertNotIn(secret, output + errors)

    def test_a_terminal_shows_the_address_but_the_browser_gets_the_file_never_the_token(self):
        output, _, held, opened = self.start(["--open"], terminal=True)
        server = held["server"]
        self.assertIn("http://%s%s/#%s" % (server.host, server.mount, server.token), output)
        (path, _, _), = held["files"]
        self.assertEqual([pathlib.Path(path).as_uri()], opened)
        for url in opened:
            self.assertNotIn(server.token, url)
            self.assertNotIn(server.mount, url)
        output, _, held, opened = self.start([], terminal=True)
        self.assertEqual(([], None), (opened, held.get("files")))   # no browser and no file are needed

    def test_a_launch_file_that_cannot_be_written_stops_a_session_start_without_the_address(self):
        err, out = io.StringIO(), Terminal(False)
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(action.storage, "write_file", side_effect=OSError("disk full")), \
                mock.patch.object(action, "serve") as serve:
            self.assertEqual(1, action.main([]))
        serve.assert_not_called()
        self.assertEqual("", out.getvalue())
        self.assertIn("launch file could not be written", err.getvalue())

    def test_launch_files_of_cut_off_services_are_removed_at_the_next_start(self):
        folder = action.Actions().folder
        old, recent = os.path.join(folder, "launch-00000000.html"), os.path.join(folder, "launch-11111111.html")
        for path in (old, recent):
            action.storage.write_file(path, "<p>old</p>")
        stale = time.time() - action.LIFETIME_SECONDS - 3600
        os.utime(old, (stale, stale))
        path = action.write_launch(folder, "/" + "a" * 32, "http://127.0.0.1:1/x#y")
        self.assertEqual("launch-aaaaaaaa.html", os.path.basename(path))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))

    def test_a_caller_that_reads_the_output_cannot_reach_preview_or_commit(self):
        """The real command, started as a background task is: its output piped. Everything that output says is all the
        caller knows; the user's browser, which reads the file, can use the service."""
        proc = subprocess.Popen([sys.executable, os.path.join(ts.SCRIPTS, "dashboard_action.py")], cwd=self.project,
                                env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        seen, quiet_since = b"", None
        deadline = time.time() + 30
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.3)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                seen, quiet_since = seen + chunk, time.time()
            elif quiet_since and time.time() - quiet_since > 1.5:
                break
        output = seen.decode("utf-8")
        self.assertEqual(0, len(self.TOKEN.findall(output)), output)
        self.assertNotIn("http://", output)
        self.assertNotIn("127.0.0.1", output)
        path = re.search(r"open this file in your browser: (\S+launch-[0-9a-f]{8}\.html)", output)
        self.assertTrue(path, output)
        path = path.group(1)
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        # What the user's browser does: read the file and follow it.
        address = re.search(r'content="0;url=http://([0-9.]+):([0-9]+)(/[0-9a-f]{32})/#([A-Za-z0-9_-]{43})"',
                            pathlib.Path(path).read_text(encoding="utf-8"))
        host, port, mount, token = address.group(1), int(address.group(2)), address.group(3), address.group(4)
        for secret in (token, mount.strip("/"), str(port)):
            self.assertNotIn(secret, output)

        def call(method, route, data=None, **headers):
            connection = http.client.HTTPConnection(host, port, timeout=60)
            sent = {"Host": "%s:%d" % (host, port)}
            sent.update(headers)
            body = None
            if data is not None:
                body = json.dumps(data).encode()
                sent.update({"Origin": "http://%s:%d" % (host, port), "Content-Type": "application/json"})
            connection.request(method, route, body=body, headers=sent)
            response = connection.getresponse()
            result = response.status, response.read().decode("utf-8")
            connection.close()
            return result

        before = self.count()
        # A caller that holds only the output can find the port but neither the mount nor the token.
        self.assertEqual(404, call("POST", "/api/preview", {"step": "7:9108:2", "words": "Done."})[0])
        self.assertEqual(404, call("POST", "/api/commit", {"id": "a" * 32, "body_hash": "0" * 64})[0])
        self.assertEqual(403, call("POST", mount + "/api/preview", {"step": "7:9108:2", "words": "Done."})[0])
        self.assertEqual(before, self.count())
        # The browser, with the file's address, reaches the service; and Stop ends it and removes the file.
        bearer = {"Authorization": "Bearer " + token}
        self.assertEqual(200, call("GET", mount + "/", **bearer)[0])
        self.assertEqual(200, call("POST", mount + "/api/stop", {}, **bearer)[0])
        self.assertEqual(0, proc.wait(30))
        self.assertIn("was stopped", proc.stdout.read().decode("utf-8"))
        self.assertFalse(os.path.exists(path))

    def count(self):
        return len(self.repo()["comments"])


class LifetimeTests(unittest.TestCase):
    """The service ends after 30 idle minutes, at 8 hours however busy, or when the user chooses Stop."""

    def fake(self, step=None):
        clock, server = [0.0], types.SimpleNamespace(started=0.0, last_seen=0.0, stop_requested=False, timeout=None, calls=0)

        def handle_request():
            clock[0] += 100.0
            server.calls += 1
            if server.calls > 1000:   # a loop with no end fails the test instead of hanging it
                raise AssertionError("the service never ended")
            if step:
                step(server, clock[0])

        server.handle_request = handle_request
        return server, clock

    def serve(self, server, clock, **limits):
        with mock.patch.object(action.time, "monotonic", side_effect=lambda: clock[0]):
            return action.serve(server, **limits)

    def test_the_cutoffs_are_thirty_minutes_idle_and_eight_hours_in_all(self):
        self.assertEqual(30 * 60, action.IDLE_SECONDS)
        self.assertEqual(8 * 60 * 60, action.LIFETIME_SECONDS)

    def test_no_authorized_request_for_the_idle_time_ends_the_service(self):
        server, clock = self.fake()
        self.assertEqual("idle", self.serve(server, clock, idle=250, limit=10000))
        self.assertEqual(3, server.calls)
        server, clock = self.fake()
        with mock.patch.object(action, "IDLE_SECONDS", 150):
            self.assertEqual("idle", self.serve(server, clock, limit=10000))   # the module's value is the default
        self.assertEqual(2, server.calls)

    def test_requests_keep_the_service_alive_but_never_past_the_limit(self):
        server, clock = self.fake(lambda s, now: setattr(s, "last_seen", now))
        self.assertEqual("limit", self.serve(server, clock, idle=250, limit=1000))
        self.assertEqual(10, server.calls)
        server, clock = self.fake(lambda s, now: setattr(s, "last_seen", now))
        with mock.patch.object(action, "LIFETIME_SECONDS", 450):
            self.assertEqual("limit", self.serve(server, clock, idle=250))
        self.assertEqual(5, server.calls)

    def test_a_stop_request_ends_the_loop_after_that_request(self):
        server, clock = self.fake(lambda s, now: setattr(s, "stop_requested", now >= 200))
        self.assertEqual("stopped", self.serve(server, clock, idle=10000, limit=10000))
        self.assertEqual(2, server.calls)

    def service(self, mount="/" + "c" * 32):
        server = action.http.server.HTTPServer(("127.0.0.1", 0), action.Handler)
        self.addCleanup(server.server_close)
        server.actions, server.token, server.mount = mock.Mock(), "t" * 43, mount
        server.actions.state.return_value = {}
        server.host = "127.0.0.1:%d" % server.server_port
        server.started = server.last_seen = time.monotonic()
        server.stop_requested = False
        return server

    def run_in_thread(self, server, **limits):
        done = []
        thread = threading.Thread(target=lambda: done.append(action.serve(server, tick=0.05, **limits)), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, done

    def post_stop(self, server, **changes):
        headers = {"Host": server.host, "Origin": "http://" + server.host, "Content-Type": "application/json",
                   "Authorization": "Bearer " + server.token}
        headers.update(changes)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request("POST", server.mount + "/api/stop", body=b"{}", headers=headers)
        status = connection.getresponse().status
        connection.close()
        return status

    def test_a_real_service_refuses_a_stop_from_another_origin_and_ends_on_the_users_own(self):
        server = self.service()
        thread, done = self.run_in_thread(server, idle=60, limit=60)
        for changes in ({"Origin": "http://attacker.invalid"}, {"Origin": "http://127.0.0.1:1"},
                        {"Host": "attacker.invalid"}, {"Authorization": "Bearer wrong"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(403, self.post_stop(server, **changes), changes)
        thread.join(0.5)
        self.assertTrue(thread.is_alive() and not done, "a refused stop must leave the service running")
        self.assertEqual(200, self.post_stop(server))
        thread.join(10)
        self.assertEqual(["stopped"], done)

    def test_a_real_service_ends_when_idle_and_at_its_limit_while_busy(self):
        server = self.service()
        thread, done = self.run_in_thread(server, idle=0.4, limit=60)
        thread.join(10)
        self.assertEqual(["idle"], done)
        server = self.service()
        thread, done = self.run_in_thread(server, idle=0.8, limit=2.0)   # busy for longer than the idle time
        headers = {"Host": server.host, "Authorization": "Bearer " + server.token}
        deadline = time.monotonic() + 10
        while not done and time.monotonic() < deadline:   # a busy user: a request every 0.1 s
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)   # one sent after the end waits
            try:
                connection.request("GET", server.mount + "/api/state", headers=headers)
                connection.getresponse().read()
            except OSError:
                break
            finally:
                connection.close()
            time.sleep(0.1)
        thread.join(10)
        self.assertEqual(["limit"], done)


PAGE_STATES = r"""
// The action page's own script, run against a small stand-in for the document and for fetch, to see what the page shows while
// a completion is being recorded, then after the receipt, and after a failure.
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.env.PAGE_JS, 'utf8');
const made = [];
function element(tag) {
  const el = {tag, children: [], listeners: {}, attrs: {}, hidden: false, disabled: false, textContent: '', value: '', open: false,
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    prepend(child) { this.children.unshift(child); },
    addEventListener(name, handler) { (this.listeners[name] = this.listeners[name] || []).push(handler); },
    setAttribute(name, value) { this.attrs[name] = value; },
    removeAttribute(name) { delete this.attrs[name]; },
    focus() {}};
  made.push(el);
  return el;
}
const ids = {};
['project', 'back', 'tasks', 'preview', 'review-step', 'review-statement', 'body', 'exact-record', 'confirm', 'cancel',
 'refresh', 'status', 'operations'].forEach(id => { ids[id] = element('x'); });
ids.preview.hidden = true;
const main = element('main');
const document = {getElementById: id => ids[id], querySelector: () => main, createElement: element};
const window = {location: {hash: '', pathname: '/mount/actions', replace() {}}, history: {replaceState() {}}};
const state = {repo: 'octo-org/octo-repo', omitted_tasks: 0, operations: [],
  tasks: [{issue: 7, title: 'Task', steps: [{id: '7:9108:2', text: 'Rerun the set', state: 'open'}]}]};
let pending = null;
const calls = [];
function reply(ok, data) { return {ok, json: async () => data}; }
const fetch = (path, options) => {
  calls.push(path);
  if (path === 'api/state') return Promise.resolve(reply(true, state));
  if (path === 'api/preview') return Promise.resolve(reply(true, {id: 'a'.repeat(32), body_hash: '0'.repeat(64), body: 'the body',
    statement: 'I ran it.', step_text: 'Rerun the set', target: {issue: 7}}));
  if (path === 'api/commit') return new Promise((resolve, reject) => { pending = {resolve, reject}; });
  return Promise.reject(new Error('unexpected ' + path));
};
const context = vm.createContext({document, window, fetch, JSON, Number, Promise, Error, console});
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
let sent = 0;   // the commits of earlier scenarios
const shot = () => ({status: ids.status.textContent, confirm_disabled: ids.confirm.disabled, cancel_disabled: ids.cancel.disabled,
  preview_hidden: ids.preview.hidden, busy: main.attrs['aria-busy'] || '', commits: calls.filter(c => c === 'api/commit').length - sent,
  recovery: ids.operations.children.some(c => c.textContent === 'Check this operation’s receipt')});
async function record(outcome) {
  sent = calls.filter(c => c === 'api/commit').length;
  const button = made.filter(el => el.textContent === 'Preview completion').pop();
  made.filter(el => el.tag === 'textarea').pop().value = 'I ran it.';
  button.listeners.click[0]();
  await tick();
  const out = {reviewing: shot()};
  ids.confirm.listeners.click[0]();
  await tick();
  out.recording = shot();
  ids.confirm.listeners.click[0]();   // a second click that a browser would have refused still must not send a second request
  if (!ids.cancel.disabled) ids.cancel.listeners.click[0]();   // a disabled button gets no click
  await tick();
  out.during_second_click = shot();
  outcome();
  await tick();
  out.after = shot();
  return out;
}
(async () => {
  vm.runInContext(source, context);
  await tick();
  const result = {};
  result.recorded = await record(() => pending.resolve(reply(true, {state: 'recorded', refresh: 'updated', receipt: {url: 'x'}})));
  result.failed = await record(() => pending.resolve(reply(false, {code: 'unavailable'})));
  console.log(JSON.stringify(result));
})();
"""


class ConfirmStatesTests(unittest.TestCase):
    """What the action page shows while "Record this completion" waits on GitHub (the live check found 30 seconds of the
    words that the preview showed): a fixed "Recording" line at once, in the polite live region, with the buttons off; then
    the receipt, or one fixed failure line with the way to check the receipt."""

    def source(self):
        with open(os.path.join(ROOT, "skills", "recortex", "page", "dashboard_action.js"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_page_has_a_polite_live_status_and_the_script_orders_the_states(self):
        self.assertIn('<p id="status" role="status" aria-live="polite"></p>', action.HTML)
        code = self.source()
        confirm = code[code.index("document.getElementById('confirm').addEventListener('click'"):]
        for earlier, later in (("lockConfirm(true)", "status.textContent = RECORDING"),
                               ("status.textContent = RECORDING", "api('api/commit'"),
                               ("api('api/commit'", "closePreview()"),
                               ("closePreview()", "showResult(result)")):
            self.assertLess(confirm.index(earlier), confirm.index(later), (earlier, later))
        self.assertIn("finally {\n      lockConfirm(false);", confirm)
        self.assertIn("var RECORDING = 'Recording this completion on GitHub…", code)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to run the page's script")
    def test_the_recording_state_shows_at_once_then_the_receipt_or_one_failure_line(self):
        done = subprocess.run(["node", "-"], input=PAGE_STATES, universal_newlines=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60,
                              env=dict(os.environ, PAGE_JS=os.path.join(ROOT, "skills", "recortex", "page", "dashboard_action.js")))
        self.assertEqual(0, done.returncode, done.stderr)
        shots = json.loads(done.stdout)
        for name in ("recorded", "failed"):
            seen = shots[name]
            self.assertFalse(seen["reviewing"]["confirm_disabled"], name)
            self.assertFalse(seen["reviewing"]["preview_hidden"], name)
            # the moment after the click, before GitHub has answered
            self.assertTrue(seen["recording"]["status"].startswith("Recording this completion on GitHub…"), seen["recording"])
            self.assertTrue(seen["recording"]["confirm_disabled"] and seen["recording"]["cancel_disabled"], name)
            self.assertFalse(seen["recording"]["preview_hidden"], name)   # the statement stays in view while it is recorded
            self.assertEqual("true", seen["recording"]["busy"], name)
            self.assertEqual(1, seen["recording"]["commits"], name)
            self.assertEqual(1, seen["during_second_click"]["commits"], name)   # a second click sends nothing
            self.assertTrue(seen["during_second_click"]["status"].startswith("Recording"), name)
            self.assertFalse(seen["during_second_click"]["preview_hidden"], name)
            self.assertFalse(seen["after"]["confirm_disabled"] or seen["after"]["cancel_disabled"], name)
            self.assertTrue(seen["after"]["preview_hidden"], name)
            self.assertEqual("", seen["after"]["busy"], name)
        self.assertIn("Completion recorded and the task list updated", shots["recorded"]["after"]["status"])
        self.assertFalse(shots["recorded"]["after"]["recovery"])
        failed = shots["failed"]["after"]
        self.assertIn("Check this operation’s receipt before starting another completion.", failed["status"])
        self.assertTrue(failed["recovery"])
        self.assertNotIn("Recording this completion", failed["status"])


class HttpTests(unittest.TestCase):
    def request(self, path="/api/preview", data=None, changes=None):
        handler = object.__new__(action.Handler)
        handler.server = types.SimpleNamespace(host="127.0.0.1:1234", token="secret", actions=mock.Mock(),
                                               mount="/" + "a" * 32, server_port=1234, stop_requested=False,
                                               last_seen=0.0)
        body = json.dumps(data if data is not None else {"step": "7:9108:2", "words": "done"}).encode()
        handler.headers = {"Host": "127.0.0.1:1234", "Origin": "http://127.0.0.1:1234",
                           "Content-Type": "application/json", "Authorization": "Bearer secret", "Content-Length": str(len(body))}
        handler.headers.update(changes or {})
        handler.path, handler.rfile = handler.server.mount + path, io.BytesIO(body)
        handler.respond = mock.Mock()
        handler.server.actions.preview.return_value = {"state": "preview"}
        handler.dispatch("POST")
        return handler

    def test_cross_origin_missing_token_and_dns_rebinding_never_reach_backend(self):
        for changes in ({"Host": "attacker.invalid:1234"}, {"Origin": "https://attacker.invalid"},
                        {"Origin": None}, {"Authorization": ""}, {"Sec-Fetch-Site": "cross-site"},
                        {"Content-Type": "text/plain"}, {"Authorization": "Bearer sécret"}):
            handler = self.request(changes=changes)
            self.assertEqual(403, handler.respond.call_args[0][0])
            handler.server.actions.preview.assert_not_called()

    def test_bounded_json_exact_operation_schema_and_no_paths_or_shell(self):
        for path, data, changes in (("/api/preview", {"step": "7:9108:2", "words": "done", "repo": "evil/repo"}, {}),
                                    ("/api/preview", None, {"Content-Length": "999999"}),
                                    ("/api/preview", None, {"Transfer-Encoding": "chunked"}),
                                    ("/../../settings.json", None, {}), ("/api/shell", {"cmd": "pwd"}, {})):
            handler = self.request(path, data, changes)
            self.assertIn(handler.respond.call_args[0][0], (400, 404))
            handler.server.actions.preview.assert_not_called()
        handler = self.request()
        self.assertEqual(200, handler.respond.call_args[0][0])
        handler.server.actions.preview.assert_called_once_with("7:9108:2", "done")

    def test_cookie_is_scoped_to_unpredictable_launch_prefix_and_wrong_mount_is_refused(self):
        handler = self.request("/api/session", {})
        self.assertTrue(handler.respond.call_args[1]["session"])
        handler.respond = types.MethodType(action.Handler.respond, handler)
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        handler.wfile = io.BytesIO()
        handler.respond(200, "{}", session=True)
        cookies = [c.args[1] for c in handler.send_header.call_args_list if c.args[0] == "Set-Cookie"]
        self.assertEqual(1, len(cookies))
        self.assertIn("Path=/" + "a" * 32 + "/;", cookies[0])
        self.assertIn("HttpOnly; SameSite=Strict", cookies[0])
        handler.respond = mock.Mock()
        handler.path = "/" + "b" * 32 + "/api/state"
        handler.dispatch("GET")
        self.assertEqual(404, handler.respond.call_args.args[0])
        handler.server.actions.state.assert_not_called()

    def test_snapshot_requires_auth_and_only_delivers_context_after_auth(self):
        handler = self.request(changes={"Authorization": ""})
        handler.path = handler.server.mount + "/snapshot"
        handler.dispatch("GET")
        self.assertEqual(403, handler.respond.call_args.args[0])
        handler.server.actions.snapshot.assert_not_called()
        handler.headers["Cookie"] = "recortex_action=secret"
        handler.server.actions.snapshot.return_value = "<p>trusted context</p>"
        handler.dispatch("GET")
        self.assertEqual(200, handler.respond.call_args.args[0])
        handler.server.actions.snapshot.assert_called_once_with(handler.server.mount)

    def test_personal_suggestions_require_same_origin_and_an_authenticated_session(self):
        path, data = "/api/suggestions/feedback", {"id": "1" * 64, "muted": True}
        for changed in ({"Origin": None}, {"Authorization": ""}, {"Origin": "http://foreign.invalid"}):
            handler = self.request(path, data, changed)
            self.assertEqual(403, handler.respond.call_args.args[0])
            handler.server.actions.suggestions.assert_not_called()
        handler = self.request(path, dict(data, muted=1))
        self.assertEqual(400, handler.respond.call_args.args[0])
        handler.server.actions.suggestions.assert_not_called()
        handler = self.request(changes={"Authorization": ""})
        handler.path = handler.server.mount + "/api/suggestions"
        handler.dispatch("GET")
        self.assertEqual(403, handler.respond.call_args.args[0])
        handler.server.actions.suggestions.assert_not_called()
        handler.server.actions.suggestions.return_value = {"suggestions": []}
        handler.headers["Cookie"] = "recortex_action=secret"
        handler.dispatch("GET")
        self.assertEqual(200, handler.respond.call_args.args[0])

    def test_stop_needs_the_checks_of_every_post_and_a_refused_request_never_keeps_the_service_alive(self):
        for changes in ({"Host": "attacker.invalid:1234"}, {"Origin": "https://attacker.invalid"}, {"Origin": None},
                        {"Authorization": ""}, {"Sec-Fetch-Site": "cross-site"}, {"Content-Type": "text/plain"},
                        {"Authorization": "Bearer wrong"}):
            handler = self.request("/api/stop", {}, changes)
            self.assertEqual(403, handler.respond.call_args[0][0], changes)
            self.assertFalse(handler.server.stop_requested, changes)
            self.assertEqual(0.0, handler.server.last_seen, changes)
        handler = self.request("/api/stop", {"after": "five"})
        self.assertEqual(400, handler.respond.call_args[0][0])
        self.assertFalse(handler.server.stop_requested)
        handler = self.request("/api/stop", {}, {"Authorization": ""})   # no token, no cookie
        self.assertFalse(handler.server.stop_requested)
        handler = self.request("/api/stop", {}, {"Authorization": "", "Cookie": "recortex_action=secret"})
        self.assertEqual(200, handler.respond.call_args[0][0])
        self.assertTrue(handler.server.stop_requested)
        handler = self.request("/api/stop", {})
        self.assertEqual(200, handler.respond.call_args[0][0])
        self.assertEqual({"stopped": True}, json.loads(handler.respond.call_args[0][1]))
        self.assertTrue(handler.server.stop_requested)
        self.assertGreater(handler.server.last_seen, 0.0)
        handler = self.request("/api/stop", {})
        handler.server.stop_requested = False
        handler.path = handler.server.mount + "/api/stop"
        handler.dispatch("GET")   # a stop is a POST only
        self.assertEqual(404, handler.respond.call_args[0][0])
        self.assertFalse(handler.server.stop_requested)

    def test_every_page_with_actions_carries_the_stop_control_and_its_script_is_served_without_record_text(self):
        handler = self.request()
        for route in ("/", "/actions", "/map-editor", "/suggestions", "/impact-review"):
            handler.path = handler.server.mount + route
            handler.dispatch("GET")
            status, body = handler.respond.call_args.args[:2]
            self.assertEqual(200, status, route)
            self.assertIn('<script src="stop.js" defer></script>', body, route)
        handler.path = handler.server.mount + "/stop.js"
        handler.dispatch("GET")
        status, body, kind = handler.respond.call_args.args[:3]
        self.assertEqual((200, "text/javascript; charset=utf-8"), (status, kind))
        self.assertIn("api/stop", body)
        for call in ("innerHTML", "eval(", "localStorage", "sessionStorage"):
            self.assertNotIn(call, body)

    def test_first_snapshot_without_index_keeps_refresh_controls_available(self):
        handler = self.request()
        handler.path = handler.server.mount + "/snapshot"
        handler.server.actions.snapshot.side_effect = action.ActionError("index_unavailable")
        handler.dispatch("GET")
        self.assertEqual(200, handler.respond.call_args.args[0])
        self.assertIn('id="refresh"', handler.respond.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

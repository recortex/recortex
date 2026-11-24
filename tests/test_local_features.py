"""Every feature of a local record (1.9.0, spec 10): the ones that are on work through the store and start no `gh`, the ones that are off
print their one fixed line (10.2), and nothing a local record prints says GitHub except those lines and the choice of setup (9.1).
Offline, inside the sandbox of tests/local_record.py; each test ends by asserting that the sandbox's failing `gh` was never started."""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_record import LocalSandbox, ROOT  # noqa: E402
from test_jobs_handoff import PHRASES  # noqa: E402

GITHUB = re.compile(r"github", re.I)


class Features(LocalSandbox):
    """The sandbox with a record in it, a runner that keeps every output, and the scan for the word GitHub."""

    ALLOWED = ("record_store.OFF", "setup.LOCAL_OFF")

    def setUp(self):
        super().setUp()
        self.outputs = []
        self.addCleanup(self.scan_for_github)
        os.makedirs(os.path.join(self.project, ".tmp"), exist_ok=True)

    def scan_for_github(self):
        allowed = set(line for line in self.record_store.OFF.values() if line)
        import setup
        allowed.add(setup.LOCAL_OFF)
        allowed.add(self.record_store.SNAPSHOT_OFF)
        for text in self.outputs:
            for line in text.splitlines():
                if GITHUB.search(line) and line.strip() not in allowed:
                    self.fail("a local record's output says GitHub: %r" % line[:200])

    def invoke(self, *args, **kwargs):
        done = self.run_script(*args, **kwargs)
        self.outputs.append(done.stdout + done.stderr)
        return done

    def ok(self, *args, **kwargs):
        done = self.invoke(*args, **kwargs)
        self.assertEqual(0, done.returncode, "%s: %s" % (" ".join(args[:2]), (done.stdout + done.stderr)[-1500:]))
        return done.stdout

    def example(self, name):
        """A draft of examples/octo-repo with its NEW-<key> placeholders made the numbers of this record."""
        text = open(os.path.join(ROOT, "examples", "octo-repo", name), encoding="utf-8").read()
        for key, number in (("NEW-task-newlines", "task"), ("NEW-reader", "reader"), ("NEW-speed", "speed"), ("NEW-hub", "hub")):
            text = text.replace(key, "#%d" % self.numbers[number])
        return text

    def draft(self, name, text):
        path = os.path.join(self.project, ".tmp", name)
        with open(path, "w") as handle:
            handle.write(text)
        return path


class OffLinesTests(Features):
    """10.2: the fixed line of each feature a local record does not have, at its entry point, with no change and no gh."""

    def off(self, feature):
        return self.record_store.OFF[feature] + "\n"

    def test_the_board_and_its_steps_print_their_line(self):
        for args in (("plan",), ("apply", "--dry-run"), ("setup", "--mode", "new", "--dry-run")):
            with self.subTest(args=args):
                done = self.invoke("board.py", *args)
                self.assertEqual((0, self.off("board")), (done.returncode, done.stdout))

    def test_sub_issue_links_print_their_line(self):
        for args in (("plan",), ("apply", "--dry-run")):
            with self.subTest(args=args):
                done = self.invoke("sub_issues.py", *args)
                self.assertEqual((0, self.off("sub-issues")), (done.returncode, done.stdout))

    def test_the_triage_query_prints_its_line_and_not_an_empty_list(self):
        self.ok("index.py", "sync")
        self.assertEqual(self.off("triage-workflow"), self.ok("index.py", "query", "triage"))

    def test_publishing_to_pages_prints_its_line_and_the_question_is_not_asked(self):
        self.assertEqual(self.off("pages"), self.ok("publish.py", "--list"))
        self.assertEqual(self.off("pages"), self.ok("publish.py", "--to", "octo-org/octo-pages", "--branch", "gh-pages"))
        done = self.invoke("acts.py", "ask", "publish")
        self.assertEqual((1, self.off("pages")), (done.returncode, done.stdout))

    def test_every_command_of_versions_prints_its_line(self):
        for args in (("status",), ("sync", "--dry-run"), ("doctor",), ("log",), ("fetch",), ("meta", "--dry-run")):
            with self.subTest(args=args):
                self.assertEqual(self.off("versions-branch"), self.ok("versions.py", *args))
        import versions
        self.assertFalse(versions.kept_here(self.repo))

    def test_approval_by_pull_request_prints_its_line(self):
        folder = os.path.join(self.project, ".tmp", "draft-dir")
        os.makedirs(folder, exist_ok=True)
        self.assertEqual(self.off("pull-requests"), self.ok("write.py", "propose", "--dir", folder))
        self.assertEqual(self.off("pull-requests"), self.ok("write.py", "approvals"))

    def test_a_snapshot_has_nothing_to_save(self):
        done = self.invoke("snapshot.py", "--out", os.path.join(self.project, ".tmp", "snap"))
        self.assertIn(self.record_store.SNAPSHOT_OFF, done.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.project, ".tmp", "snap", "issues.json")))

    def test_the_label_catalog_has_nothing_to_create(self):
        self.assertIn(self.record_store.OFF["label-catalog"], self.ok("setup_labels.py", "--dry-run"))

    def test_a_figure_has_no_picture_and_the_page_falls_back(self):
        import mind_figures
        images, reasons = mind_figures.load([(self.repo, "v1", "figures/a.png")], self.repo, self.repo, True)
        self.assertEqual({}, images)
        self.assertEqual({(self.repo, "v1", "figures/a.png"): "local-record"}, reasons)
        self.assertNotRegex(mind_figures.REASONS["local-record"], GITHUB)

    def test_the_page_is_offered_as_a_file_and_nothing_else(self):
        import publish
        lines = publish.offer(self.config, self.repo, os.path.join(self.project, "page.html"))
        text = "\n".join(lines)
        self.assertIn("Save the page as a file", text)
        self.assertIn("Not now", text)
        self.assertEqual(2, sum(1 for line in lines if line.startswith("  - ")))
        self.assertNotRegex(text, GITHUB)

    def test_no_line_tells_the_user_to_run_something(self):
        for line in list(self.record_store.OFF.values()) + [self.record_store.SNAPSHOT_OFF]:
            if line:
                self.assertIsNone(PHRASES.search(line), line)
                self.assertNotRegex(line, r"(?i)\brun\b")


class RecordingTests(Features):
    """10.1: recording, resume, recall, compile and the brief on a local record."""

    def test_a_verified_post_is_read_back_and_found_by_recall_under_its_address(self):
        number = self.numbers["speed"]
        text = open(os.path.join(ROOT, "examples", "octo-repo", "result-speed-1.md")).read()
        draft = self.draft("result.md", text.replace("**Evidence:**\n- figure read-time\n\n", ""))
        dry = self.ok("post_verified.py", "comment", "--issue", str(number), "--kind", "result", "--body-file", draft, "--dry-run")
        self.assertIn("would run:", dry)
        self.assertEqual(3, len([n for n in os.listdir(os.path.join(self.folder, "topics", "read-speed-against-the-standard-library"))
                                 if n.endswith(".md")]))   # nothing was written by the dry run
        said = self.ok("post_verified.py", "comment", "--issue", str(number), "--kind", "result", "--body-file", draft)
        self.assertRegex(said, r"verified: comment #%d/[0-9]+ matches the draft" % number)
        self.ok("index.py", "sync")
        recall = self.ok("index.py", "query", "recall", "--issue", str(number))
        memory = re.search(r"memory id: ([0-9]+)", said).group(1)
        self.assertIn(memory, recall)
        self.assertIn("read the 100 MB file", recall)

    def test_a_body_is_replaced_under_its_lock_and_the_old_text_is_kept_as_a_version(self):
        number = self.numbers["reader"]
        before = self.store.read_issue(number)["body"]
        base = self.draft("base.md", before)
        draft = self.draft("new.md", self.example("topic-reader.md"))
        said = self.ok("post_verified.py", "body", "--issue", str(number), "--kind", "final-record", "--body-file", draft,
                       "--expected-body-file", base, "--backup", os.path.join(self.project, ".tmp", "backup.md"))
        self.assertIn("Strict quoting is the default", self.store.read_issue(number)["body"])
        self.assertGreaterEqual(len(self.store.versions(number)), 2)
        self.assertTrue(said)

    def test_a_topic_is_made_from_a_manifest_and_a_rerun_makes_no_second(self):
        self.write_config(dict(self.config, topics=["reader"]))   # a topic's label is one of the project's
        body = self.draft("task-new.md", self.example("task-newlines.md"))
        manifest = self.draft("manifest.json", json.dumps({"repo": self.repo, "issues": [
            {"key": "extra", "kind": "active", "title": "An extra task", "labels": ["active", "topic:reader"], "body_file": body}]}))
        self.ok("create_issues.py", "--manifest", manifest)
        self.ok("create_issues.py", "--manifest", manifest)
        titles = [title for title in (self.store.read_issue(n)["title"] for n in range(1, 8) if self.safe(n))]
        self.assertEqual(1, titles.count("An extra task"))

    def safe(self, number):
        try:
            self.store.read_issue(number)
            return True
        except self.record_store.StoreError:
            return False

    def test_the_resume_block_is_posted_and_read_from_the_local_record(self):
        text = open(os.path.join(ROOT, "examples", "octo-repo", "resume-1.md")).read()
        for key, number in (("NEW-task-newlines", self.numbers["task"]), ("NEW-reader", self.numbers["reader"]),
                            ("NEW-speed", self.numbers["speed"])):
            text = text.replace(key, "#%d" % number)
        text = text.replace("this hub", "#%d" % self.numbers["hub"])
        draft = self.draft("resume.md", text)
        self.ok("resume_block.py", "update", "--file", draft)
        shown = self.ok("resume_block.py", "show")
        self.assertIn("The release is close", shown)
        self.assertIn("Read from the local record just now.", shown)

    def test_the_index_the_deliverable_and_the_brief_are_built_from_the_files(self):
        self.ok("index.py", "sync")
        out = os.path.join(self.project, ".tmp", "compiled")
        self.ok("compile.py", "--out", out)
        files = [out] if os.path.isfile(out) else [os.path.join(folder, name) for folder, names, found in os.walk(out) for name in found]
        written = "".join(open(path, encoding="utf-8").read() for path in files)
        self.assertIn("strict quoting", written)
        printed = self.ok("hooks.py", "--print")
        self.assertIn("the local record of this project", printed)
        doctor = self.ok("hooks.py", "--doctor")
        self.assertIn("gh: not needed for a local record", doctor)

    def test_a_task_is_added_to_the_standing_list(self):
        words = self.draft("todo.md", "Check the wide-file case again.\n")
        self.ok("index.py", "sync")
        self.ok("tasks.py", "add", "--text-file", words)
        self.ok("index.py", "sync")
        self.assertIn("Check the wide-file case again", self.ok("tasks.py", "status"))

    def test_the_catch_up_inventory_reads_the_project_and_asks_no_one(self):
        out = os.path.join(self.project, ".tmp", "inventory")
        self.ok("catchup.py", "inventory", "--out", out)
        self.assertTrue(os.path.exists(out + ".md"), os.listdir(os.path.join(self.project, ".tmp")))

    def test_usage_and_the_status_line_need_nothing(self):
        self.ok("index.py", "sync")
        self.ok("usage.py", "show")
        self.ok("statusline.py")
        self.ok("status.py")


class PaperTests(Features):
    """10.1: the plan, the writing in the chat, the author's own file and the paper's page work on a local record; the branch and
    the pull request do not."""

    def setUp(self):
        super().setUp()
        self.config = dict(self.config, profiles=["manuscript", "software"], topics=["reader"])
        self.write_config(self.config)
        self.ok("index.py", "sync")

    def test_the_plan_is_a_topic_and_is_not_linked_under_the_hub(self):
        dry = self.ok("plan.py", "setup", "--dry-run")
        self.assertIn("would run: local record: make a topic", dry)
        self.assertNotIn("sub-issue", dry)
        self.ok("plan.py", "setup")
        with open(os.path.join(self.project, ".recortex.json")) as handle:
            plans = json.load(handle)["plans"]
        self.assertEqual("manuscript", list(plans)[0])
        self.ok("index.py", "sync")
        self.assertIn("Plan issue #%d" % plans["manuscript"], self.ok("plan.py", "status"))
        self.assertEqual("plan", [label["name"] for label in self.store.read_issue(plans["manuscript"])["labels"]][0])

    def test_writing_in_the_chat_keeps_no_commit_even_in_review_mode(self):
        import versions
        import writing
        writing.record(self.repo, "review")
        self.assertFalse(versions.kept_here(self.repo))
        self.assertIn(self.invoke("write.py", "status").returncode, (0, 1))
        self.assertEqual(versions.REASON["off"], versions.commit(self.config, self.repo, self.numbers["reader"], "text\n"))
        self.assertIn("review", self.ok("setup.py", "writing", "show"))

    def test_the_authors_own_file_and_the_papers_page_need_nothing_more(self):
        self.ok("own_paper.py", "where")
        self.assertIn("sections", self.ok("mind.py", "--paper"))


class LedgerTests(Features):
    """10.1: the map's ledger, a decision review's ledger and the observer's publisher, each end to end against a local record: what they
    post is an entry of the hub or of a topic, read back through the store, and counted only when it is the user's own."""

    def setUp(self):
        super().setUp()
        self.hub = self.numbers["hub"]
        self.ok("index.py", "sync")

    def test_a_map_change_is_previewed_posted_to_the_hub_and_replayed_from_the_record(self):
        import project_map_io as api
        before = len(self.store.hub_entries(self.hub))
        branches = [{"id": "goal", "title": "Goal", "description": "What the project is for."},
                    {"id": "method", "title": "Method", "parent": "goal", "evidence": ["issue:%d" % self.numbers["reader"]]}]
        preview = api.preview(self.repo, self.hub, {"action": "seed", "branches": branches})
        self.assertEqual(before, len(self.store.hub_entries(self.hub)))   # a preview posts nothing
        done = api.commit(self.repo, self.hub, preview["operation"])
        self.assertEqual(before + 1, len(self.store.hub_entries(self.hub)))   # one entry of the hub
        state = api.current(self.repo, self.hub)
        self.assertEqual(1, len(state["history"]))
        self.assertEqual(1, len([row for row in api.verified_rows(self.repo, self.hub, api.model.MARKER) if row]))

    def test_a_changed_map_entry_does_not_count(self):
        import project_map_io as api
        preview = api.preview(self.repo, self.hub, {"action": "seed", "branches": [{"id": "goal", "title": "Goal"}]})
        api.commit(self.repo, self.hub, preview["operation"])
        self.assertEqual(1, len(api.current(self.repo, self.hub)["history"]))
        path = self.entry_path("hub", -1)
        self.write(path, self.read(path).replace(b"Goal", b"Gain", 1))   # a change outside recortex
        self.assertEqual(0, len(api.current(self.repo, self.hub)["history"]))

    def test_a_decision_reviews_ledger_entry_is_posted_read_back_and_replayed(self):
        import decision_review as ledger
        import project_map_io as map_io
        event = dict(id="0" * 32, group="impact-18", target="b" * 64, revision="a" * 64, previous=0, verdict="needs_update",
                     note="The branch cites the old decision.", draft="Update the citation.", action="map", receipt=None)
        self.assertTrue(ledger.valid(event))
        body = ledger.encode(self.repo, self.hub, event)
        self.store.post_entry(self.hub, body=body)
        rows = map_io.verified_rows(self.repo, self.hub, ledger.MARKER)
        self.assertEqual(1, len(rows))
        history = ledger.replay(self.repo, self.hub, {"local--record"}, rows)
        self.assertEqual(["needs_update"], [row["verdict"] for row in history])
        self.assertEqual([], ledger.replay(self.repo, self.hub, {"octocat"}, rows))   # an account that is not the record's counts for nothing
        path = self.entry_path("hub", -1)
        self.write(path, self.read(path).replace(b"cites the old", b"cites an old", 1))
        self.assertEqual([], ledger.replay(self.repo, self.hub, {"local--record"}, map_io.verified_rows(self.repo, self.hub, ledger.MARKER)))

    def event(self, body, key="k1"):
        import hashlib
        return {"candidate": {"issue": self.numbers["speed"]}, "account": "local--record", "body": body, "eventkey": key,
                "payload_hash": hashlib.sha256(body.encode("utf-8")).hexdigest()}

    def test_the_observers_publisher_posts_one_entry_and_reconciles_it_by_its_event_key(self):
        import observer_publisher as publisher
        body = ("<!-- ghim:event type=idea at=2026-11-24T10:00:00Z eventkey=k1 -->\n### Idea: 2026-11-24 10:00 UTC - read in blocks\n\n"
                "**In plain words, we had the idea to:** read the file in blocks\n\n**The idea:** Read the file in blocks of 64 KB.\n")
        self.assertEqual("local--record", publisher._account(self.repo, self.config))
        event = self.event(body)
        self.assertIsNone(publisher._find_remote(self.repo, event, config=self.config))   # nothing was posted yet
        posted = self.store.post_entry(self.numbers["speed"], body=body)
        cid = int(self.store.posted_id(posted))
        found = publisher._find_remote(self.repo, event, config=self.config)
        self.assertEqual(cid, found["id"])
        data = publisher._remote_comment(self.repo, cid, self.config)
        receipt = publisher._check_remote(self.repo, event, data, config=self.config)
        self.assertEqual((cid, "#%d/%d" % (self.numbers["speed"], cid), "local--record"),
                         (receipt["comment_id"], receipt["url"], receipt["account"]))

    def test_a_changed_or_foreign_entry_is_not_reconciled_as_the_publishers_own(self):
        import observer_publisher as publisher
        body = ("<!-- ghim:event type=idea at=2026-11-24T10:00:00Z eventkey=k2 -->\n### Idea: 2026-11-24 10:00 UTC - read in blocks\n\n"
                "**In plain words, we had the idea to:** read the file in blocks\n\n**The idea:** Read the file in blocks of 64 KB.\n")
        event = self.event(body, "k2")
        cid = int(self.store.posted_id(self.store.post_entry(self.numbers["speed"], body=body)))
        path = self.entry_path("speed", -1)
        original = self.read(path)
        self.write(path, original.replace(b"64 KB", b"4 KB", 1))   # changed outside recortex
        data = publisher._remote_comment(self.repo, cid, self.config)
        with self.assertRaises(publisher.PublishError) as caught:
            publisher._check_remote(self.repo, event, data, config=self.config)
        self.assertRegex(str(caught.exception), "remote_edited|remote_changed")
        self.write(path, original)
        relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
        parsed = self.local_store.parse_file(original, relpath, self.record_id)
        self.write(path, self.local_store.render(parsed.kind, parsed.front, parsed.body, relpath, self.record_id,
                                                 self.local_store.Sealing(b"another machine's key")))
        data = publisher._remote_comment(self.repo, cid, self.config)
        with self.assertRaises(publisher.PublishError) as caught:
            publisher._check_remote(self.repo, event, data, config=self.config)
        self.assertIn("wrong_author", str(caught.exception))


class LiveServerTests(Features):
    """10.1: the live dashboard serves a local record: the main page with the Record pane, and one topic's view at /read/<N>. The
    only test of this round that starts a server: in this process, on a free loopback port, stopped with addCleanup first."""

    MOUNT, VIEW, CHANGE = "a" * 32, "b" * 32, "c" * 32

    def setUp(self):
        super().setUp()
        import http.client
        import threading
        import dashboard_action as action
        import dashboard_server
        self.http, self.dashboard_server = http.client, dashboard_server
        os.environ["RECORTEX_TEST_SERVE"] = "1"
        self.addCleanup(os.environ.pop, "RECORTEX_TEST_SERVE", None)
        self.ok("index.py", "sync")
        self.server = action.Server(("127.0.0.1", 0), action.Handler, self.MOUNT, self.VIEW, self.CHANGE)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.addCleanup(self.stop)   # registered before the first start, so the server is stopped whatever an assertion does
        self.server.register(self.project)
        self.thread.start()
        self.ident = dashboard_server.project_id(self.project)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def get(self, path, cookie=True):
        port = self.server.server_port
        headers = {"Host": "127.0.0.1:%d" % port}
        if cookie:
            headers["Cookie"] = "recortex_view=%s" % self.VIEW
        connection = self.http.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            connection.request("GET", "/%s/p/%s%s" % (self.MOUNT, self.ident, path), headers=headers)
            answer = connection.getresponse()
            return answer.status, answer.read().decode("utf-8")
        finally:
            connection.close()

    def test_the_main_page_holds_the_pane_and_a_topics_view_is_served_whole(self):
        status, page = self.get("/snapshot")
        self.assertEqual(200, status)
        self.assertIn('id="record"', page)
        self.assertNotRegex(page, r'<section class="view" id="read-[0-9]+"')
        number = self.numbers["reader"]
        self.assertRegex(page, r'href="/%s/p/%s/read/%d"' % (self.MOUNT, self.ident, number))
        status, view = self.get("/read/%d" % number)
        self.assertEqual(200, status)
        self.assertIn('data-start="read-%d"' % number, view)
        self.assertIn("strict quoting", view)
        self.assertNotIn("file:", view)
        self.assertNotIn(self.folder, view)
        self.assertEqual(403, self.get("/read/%d" % number, cookie=False)[0])
        self.assertEqual(404, self.get("/read/999")[0])
        self.assertEqual(404, self.get("/read/0")[0])


class PageTests(Features):
    """10.1: the dashboard's page of a local record."""

    def test_the_page_tells_the_record_with_local_addresses_and_no_file_address(self):
        self.ok("index.py", "sync")
        printed = self.ok("mind.py")
        self.assertTrue(printed)
        found = [os.path.join(folder, name) for folder, names, files in os.walk(os.path.join(self.dir, "cache")) for name in files
                 if name.endswith(".html")]
        self.assertTrue(found, printed)
        page = open(found[0], encoding="utf-8").read()
        self.assertIn("strict", page)
        self.assertNotIn("file:", page)
        self.assertNotIn(self.folder, page)
        self.assertNotRegex(page, r"https://github\.com/local--record")
        self.assertEqual("", "\n".join(match for match in re.findall(r"[^\n]*github\.com/[^\n]*", page, re.I) if self.repo in match))


if __name__ == "__main__":
    unittest.main()

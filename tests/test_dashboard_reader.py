"""The reader in the live dashboard (1.9.0, spec 10.3): the main page carries the Record pane and not every view; GET /read/<N> serves
one topic's view at the view-only authority, behind the same Host and Origin checks as every route; a number that is not a topic gets
the fixed not-found answer; what is not told is shut under its mark for a GitHub record too; the reader inserts no text by script.
Offline: the server runs in this process on a free loopback port, as in test_dashboard_live.py."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_dashboard_live as live  # noqa: E402  (used by its module's name: its tests are not collected here)

ROOT = live.ROOT
MIND_JS = os.path.join(ROOT, "skills", "recortex", "page", "mind.js")
PLAIN_NOTE = "<!-- ghim:event type=idea at=2026-04-10T12:00:00Z -->\n### Idea: 2026-04-10 12:00 UTC - read in blocks\n\nA note."


class ReaderBox(live.LiveBox):
    def view_page(self, number, **kwargs):
        status, headers, text = self.call("GET", "/read/%d" % number, level=kwargs.pop("level", "view"), **kwargs)
        return status, headers, text

    def topic_view(self, text, number):
        start = text.index('<section class="view" id="read-%d"' % number)
        return text[start:text.index("</section>", text.index('id="read-%d-entries"' % number)) + 10]


class RouteTests(ReaderBox):
    def test_the_main_page_has_the_pane_and_links_to_the_route_and_holds_no_view(self):
        status, _, text = self.call("GET", "/snapshot", level="view")
        self.assertEqual(200, status)
        self.assertIn('id="record"', text)
        self.assertNotRegex(text, r'<section class="view" id="read-[0-9]+"')   # the five-second reload stays light
        base = "/%s/p/%s" % (live.MOUNT, self.ident)
        self.assertRegex(text, r'href="%s/read/[1-9][0-9]*"' % re.escape(base))

    def test_one_topics_view_arrives_in_the_live_shell_at_the_view_only_authority(self):
        status, headers, text = self.view_page(5)
        self.assertEqual(200, status)
        self.assertEqual("text/html; charset=utf-8", headers["Content-Type"])
        self.assertIn('data-start="read-5"', text)
        self.assertEqual(1, len(re.findall(r'<section class="view" id="read-[0-9]+"', text)))
        self.assertIn('id="read-5-text"', text)
        self.assertIn('id="read-5-entries"', text)
        self.assertIn("live-stopped", text)   # the live shell (its script and its stop line)
        self.assertNotIn("file:", text)
        self.assertIn("Open the whole issue on GitHub", text)   # a GitHub record keeps its link, and gains the reader
        # the view-only page copies requests into the chat: nothing here changes the record
        self.assertNotIn("/actions", self.topic_view(text, 5))

    def test_a_request_with_no_key_or_a_wrong_key_is_refused(self):
        status, _, text = self.call("GET", "/read/5", level=None)
        self.assertEqual(403, status)
        self.assertNotIn("read-5", text)
        status, _, text = self.call("GET", "/read/5", level="view", extra={"Cookie": "recortex_view=" + "d" * 32})
        self.assertEqual(403, status)

    def test_another_host_or_a_cross_origin_is_refused(self):
        for kwargs in (dict(host="evil.example"), dict(host="localhost.evil.example"), dict(extra={"Origin": "http://evil.example"}),
                       dict(extra={"Sec-Fetch-Site": "cross-site"})):
            with self.subTest(kwargs=kwargs):
                status, _, text = self.view_page(5, **kwargs)
                self.assertEqual(403, status)
                self.assertNotIn("read-5", text)

    def test_a_number_that_is_not_a_topic_gets_the_fixed_answer(self):
        for number in ("0", "-1", "1234567890", "01", "1/../2", "abc", "5%2f..%2f6", "5.html", "5?x=1", "99999", ""):
            with self.subTest(number=number):
                status, _, text = self.call("GET", "/read/" + number, level="view")
                self.assertIn(status, (403, 404), number)
                self.assertNotIn('id="read-', text)
                if status == 404:
                    self.assertEqual({"code": "not_found"}, __import__("json").loads(text))


class MarkTests(ReaderBox):
    """A GitHub record: what is not told sits shut under a fixed mark, and a told entry does not."""

    def test_an_account_outside_the_list_and_a_changed_comment_are_shut_under_their_marks(self):
        import mind_reader
        self.put(9400, 5, PLAIN_NOTE, user="hubot")
        self.edit_comment(9103)
        self.ready()
        status, _, text = self.view_page(5)
        self.assertEqual(200, status, text[:200])
        view = self.topic_view(text, 5)
        shut = re.findall(r'<details class="readentry folded" id="read-5-e([0-9]+)">(.*?)</details>', view, re.S)
        marks = {cid: body for cid, body in shut}
        self.assertEqual({"9400", "9103"}, set(marks))
        self.assertIn(mind_reader.GITHUB_OUTSIDE, marks["9400"])
        self.assertIn(mind_reader.GITHUB_CHANGED, marks["9103"])
        opened = re.findall(r'<details class="readentry" id="read-5-e([0-9]+)" open="open">', view)
        self.assertTrue(opened)
        self.assertFalse({"9400", "9103"} & set(opened))
        self.assertEqual(0, len(re.findall(r'class="readentry folded"[^>]*open', view)))


class ScriptTests(unittest.TestCase):
    def test_the_reader_inserts_no_text_by_script(self):
        with open(MIND_JS, encoding="utf-8") as handle:
            script = handle.read()
        for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(word, script)
        self.assertIn("data-start", script)   # the one thing the reader's route asks of the script

    def test_a_reading_view_holds_no_button_form_or_field(self):
        import mind
        model = {"repo": "octo-org/octo-repo", "memories": [], "threads": [], "words": [], "cited": [],
                 "reader": {"local": False, "mode": "views", "only": 5, "counts": {"entries": 1, "folded": 0}, "unread": 0,
                            "topics": [{"n": 5, "title": "A topic", "state": "open", "closed": None, "labels": ["final-record"],
                                        "updated": "2026-04-10 12:00 UTC", "hub": False, "count": 1, "folded": 0, "body_mark": None,
                                        "untold": False, "capped": False, "body": None, "versions": [], "edits": [],
                                        "entries": [{"id": 9, "at": "2026-04-10 12:00 UTC", "mark": None, "kind": "result",
                                                     "doc": {"blocks": [{"p": [{"t": "A result."}]}], "cut": False},
                                                     "note": False}]}]}}
        page = mind.Page(model).read_view(model["reader"]["topics"][0])
        for tag in ("<button", "<form", "<input", "<select", "<textarea"):
            self.assertNotIn(tag, page)


if __name__ == "__main__":
    unittest.main()

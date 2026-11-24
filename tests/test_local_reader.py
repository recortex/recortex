"""The reader of the dashboard for a local record (1.9.0, spec 10.3): one view for each topic with its whole text, its earlier
texts and every entry in time order; what is not told is shut under a fixed mark; nothing the reader shows feeds anything else; it
never builds a file address and never shows a path. Offline, inside the sandbox of tests/local_record.py."""

import datetime
import html
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_record import LocalSandbox  # noqa: E402
from test_jobs_handoff import PHRASES  # noqa: E402

MARKER = "PLANTEDMARKERQ7"


class Reader(LocalSandbox):
    def setUp(self):
        super().setUp()
        import index
        import mind
        import mind_model
        import mind_reader
        self.index, self.mind, self.mind_model, self.mind_reader = index, mind, mind_model, mind_reader

    def build(self, reader="all"):
        """The model of the page, built in this process from the synced index."""
        done = self.run_script("index.py", "sync")
        self.assertEqual(0, done.returncode, done.stderr)
        db = self.index.connect(self.repo, create=False)
        try:
            return self.mind_model.build(db, self.repo, {"local--record"}, datetime.date.today(), [], self.config, reader)
        finally:
            db.close()

    def page(self):
        model = self.build()
        text, _ = self.mind.finish(model, self.config, self.repo)
        return text

    def view(self, page, number):
        """The markup of the reading view of a topic."""
        start = page.index('<section class="view" id="read-%d"' % number)
        return page[start:page.index("</section>", page.index('id="read-%d-entries"' % number)) + 10]

    def plain(self, markup):
        return " ".join(re.sub(r"<[^>]+>", " ", markup).split())

    def plant(self, name, key="reader"):
        """Put MARKER into the text of the entry that `damage` hurts, as a change by hand or by another machine would."""
        path = self.damage(name, key)
        relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
        parsed = self.local_store.parse_file(self.read(path), relpath, self.record_id)
        if name == "foreign-entry":
            data = self.local_store.render(parsed.kind, parsed.front, parsed.body + "\n%s\n" % MARKER, relpath, self.record_id,
                                           self.local_store.Sealing(b"another machine's key"))
            self.write(path, data)
        else:
            self.write(path, self.read(path).replace(b"**Exact words:**", ("**Exact words:** %s" % MARKER).encode(), 1))
        return path


class ViewTests(Reader):
    def test_a_topics_view_holds_its_whole_text_and_every_entry_in_time_order(self):
        page = self.page()
        number = self.numbers["reader"]
        text = self.plain(self.view(page, number))
        self.assertIn("Current text", text)
        self.assertIn("Past versions of the text", text)
        self.assertIn("Entries", text)
        order = [text.index(word) for word in ("Decision 2026", "Result 2026", "Failure 2026")]
        self.assertEqual(sorted(order), order)
        self.assertEqual(5, self.view(page, number).count('class="readentry'))   # every entry of the topic, whole
        for entry in self.entries["reader"]:
            self.assertIn("#%d/%d" % (number, entry), text)   # each told entry ends with its address

    def test_the_pane_lists_the_hub_first_with_its_counts_and_links_every_view(self):
        page = self.page()
        start = page.index('<section class="view" id="record"')
        pane = self.plain(page[start:page.index("</section>", start)])
        self.assertTrue(pane.index("#1") < pane.index("#%d" % self.numbers["reader"]))
        self.assertIn("4 topics, 9 entries", pane)
        for number in self.numbers.values():
            self.assertIn('href="#read-%d"' % number, page)
        self.assertIn('data-nav="record"', page)

    def test_the_past_versions_come_newest_first_each_folded(self):
        page = self.page()
        view = self.view(page, self.numbers["reader"])
        past = view[view.index('id="read-%d-past"' % self.numbers["reader"]):view.index('id="read-%d-entries"' % self.numbers["reader"])]
        times = re.findall(r"<summary>([0-9-]{10} [0-9:]{5}) UTC", past)
        self.assertEqual(2, len(times))
        self.assertEqual(sorted(times, reverse=True), times)
        self.assertNotIn(" open", past)   # no version is open by itself

    def test_what_is_not_told_is_shut_under_its_mark_and_a_told_entry_is_open(self):
        self.damage("changed-entry")
        self.damage("foreign-entry", "speed")
        page = self.page()
        reader_view, speed_view = self.view(page, self.numbers["reader"]), self.view(page, self.numbers["speed"])
        changed = self.mind_reader.LOCAL_CHANGED
        foreign = self.mind_reader.LOCAL_FOREIGN
        shut = re.findall(r'<details class="readentry folded" id="read-[0-9]+-e[0-9]+">(.*?)</details>', reader_view, re.S)
        self.assertEqual(1, len(shut))
        self.assertIn(changed, shut[0])
        shut = re.findall(r'<details class="readentry folded" id="read-[0-9]+-e[0-9]+">(.*?)</details>', speed_view, re.S)
        self.assertEqual(1, len(shut))
        self.assertIn(foreign, shut[0])
        self.assertEqual(4, len(re.findall(r'<details class="readentry" id="read-[0-9]+-e[0-9]+" open="open">', reader_view)))
        self.assertNotIn('class="readentry folded" id="read-%d-e%d" open' % (self.numbers["reader"], 0), reader_view)
        self.assertEqual(0, len(re.findall(r'class="readentry folded"[^>]*open', page)))   # never open by itself

    def test_a_topic_with_what_is_not_told_ends_with_the_line_that_says_it_will_be_asked_about(self):
        self.damage("changed-entry")
        page = self.page()
        self.assertIn(self.mind_reader.TAKE_BACK, self.view(page, self.numbers["reader"]))
        self.assertNotIn(self.mind_reader.TAKE_BACK, self.view(page, self.numbers["speed"]))
        self.assertIsNone(PHRASES.search(self.mind_reader.TAKE_BACK))
        self.assertNotRegex(self.mind_reader.TAKE_BACK, r"(?i)\b(run|command|terminal)\b")

    def test_a_text_changed_outside_recortex_is_shut_under_its_mark(self):
        self.damage("changed-body")
        page = self.page()
        view = self.view(page, self.numbers["reader"])
        start = view.index('id="read-%d-text"' % self.numbers["reader"])
        text = view[start:view.index('id="read-%d-past"' % self.numbers["reader"])]
        self.assertIn('<details class="readfold">', text)
        self.assertIn(self.mind_reader.LOCAL_CHANGED, text)
        self.assertNotIn(" open", text)
        self.assertIn(self.mind_reader.TAKE_BACK, view)

    def test_the_files_that_were_not_read_are_counted_in_the_panes_words(self):
        self.damage("conflict-copy")
        page = self.page()
        pane = html.unescape(self.plain(page[page.index('id="record"'):page.index("</section>", page.index('id="record"'))]))
        self.assertIn(self.mind_reader.UNREAD % 1, pane)
        self.assertNotIn("--doctor", pane)

    def test_past_the_cap_the_newest_topics_keep_their_views_and_the_others_show_their_first_lines(self):
        original = self.mind_model.CAP_READER
        self.mind_model.CAP_READER = 3000
        try:
            model = self.build()
        finally:
            self.mind_model.CAP_READER = original
        capped = [t["n"] for t in model["reader"]["topics"] if t.get("capped")]
        kept = [t["n"] for t in model["reader"]["topics"] if not t.get("capped")]
        self.assertTrue(capped and kept)
        newest = max(model["reader"]["topics"], key=lambda t: (t["updated"], t["n"]))["n"]
        self.assertIn(newest, kept)
        text, _ = self.mind.finish(model, self.config, self.repo)
        self.assertIn("The full text of this topic is in the live dashboard.", text)
        self.assertEqual(len(capped), text.count("The full text of this topic is in the live dashboard."))


    def test_a_capped_topic_whose_text_is_not_told_shows_its_mark_and_no_text(self):
        self.damage("changed-body")
        path = self.topic_path("speed")
        relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
        parsed = self.local_store.parse_file(self.read(path), relpath, self.record_id)
        self.write(path, self.local_store.render(parsed.kind, parsed.front, parsed.body, relpath, self.record_id,
                                                 self.local_store.Sealing(b"another machine's key")))
        original = self.mind_model.CAP_READER
        self.mind_model.CAP_READER = 1
        try:
            model = self.build()
        finally:
            self.mind_model.CAP_READER = original
        marked = [t for t in model["reader"]["topics"] if t.get("capped") and t.get("body_mark")]
        self.assertTrue(marked, "no topic with a text that is not told was capped")
        text, _ = self.mind.finish(model, self.config, self.repo)
        for topic in marked:
            self.assertIsNone(topic["first"])
            view = self.view_of(text, topic["n"])
            self.assertIn(topic["body_mark"], view)
            self.assertNotIn("lenient quoting", view)
            self.assertNotIn("The reader runs at the speed", view)
            self.assertNotRegex(view, r"readtext")
            self.assertIn("The full text of this topic is in the live dashboard.", view)

    def view_of(self, page, number):
        start = page.index('<section class="view" id="read-%d"' % number)
        return page[start:page.index("</section>", start) + 10]


    def test_a_text_change_shows_no_body_hash_and_no_version_commit(self):
        number = self.numbers["reader"]
        commit, digest = "cd4b2c3972b0365122dad15e3bb07650a6cd5724", "0123456789abcdef" * 4
        self.store.post_entry(number, body="<!-- ghim:event type=text at=2026-11-24T10:00:00Z -->\n### Text: 2026-11-24 10:00 UTC - the reader's "
                              "text\n\n**In plain words, we changed the text to say that:** the text of the reader is shorter\n\n"
                              "**Section:** Changed\n\n**What changed:** a sentence\n\n**Why:** the author asked\n\n**Approved:** in the chat\n\n"
                              "**Kept from the author:** nothing\n\n**Body:** before sha256 `none`; after sha256 `%s`\n\n"
                              "**Version:** `%s`\n" % (digest, commit))
        page = self.page()
        view = self.view(page, number)
        self.assertIn("the text of the reader is shorter", view)
        self.assertNotIn(commit, page)
        self.assertNotIn(digest, page)
        self.assertNotRegex(page, r"[0-9a-f]{40}")


class NothingElseTests(Reader):
    def test_no_file_address_no_path_and_no_file_name_is_in_the_page(self):
        self.damage("changed-entry")
        page = self.page()
        self.assertNotIn("file:", page)
        self.assertNotIn(self.folder, page)
        self.assertNotIn(self.dir, page)
        names = [name for _, _, files in os.walk(self.folder) for name in files]
        self.assertTrue(names)
        for name in names:
            self.assertNotIn(name, page)
        for slug in ("reader-quoting-and-line-endings", "read-speed-against-the-standard-library"):
            self.assertNotIn(slug + ".md", page)
        self.assertNotRegex(page, r"https://github\.com/")

    def test_a_local_address_is_text_and_a_link_inside_the_page_and_never_a_link_out(self):
        page = self.page()
        self.assertNotRegex(page, r'href="https?://')
        self.assertEqual(0, len(re.findall(r"Open the original comment on GitHub|Open the whole issue on GitHub", page)))
        self.assertIn("Read it in the record", page)   # the cited issues' boxes

    def test_text_that_only_the_reader_shows_feeds_nothing_else(self):
        self.plant("changed-entry")
        self.plant("foreign-entry", "speed")
        model = self.build()
        reader = json.dumps(model["reader"])
        self.assertEqual(2, reader.count(MARKER))
        for key, value in model.items():
            if key != "reader":
                self.assertNotIn(MARKER, json.dumps(value), key)   # not the suggestions, the impact, the map, the decisions, the plan
        page, _ = self.mind.finish(model, self.config, self.repo)
        self.assertEqual(2, page.count(MARKER))
        self.assertTrue(all(MARKER in self.view(page, number) for number in (self.numbers["reader"], self.numbers["speed"])))
        for argument in ("--print",):
            printed = self.run_script("hooks.py", argument)
            self.assertEqual(0, printed.returncode, printed.stderr)
            self.assertNotIn(MARKER, printed.stdout + printed.stderr)

    def test_the_page_made_for_the_public_carries_no_reader(self):
        model = self.build()
        self.assertTrue(model["reader"]["topics"])
        page, _ = self.mind.finish(model, self.config, self.repo, public=True)
        self.assertNotIn('id="record"', page)
        self.assertNotIn('id="read-', page)
        self.assertNotIn('data-nav="record"', page)


if __name__ == "__main__":
    unittest.main()

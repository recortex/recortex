"""Publishing the static dashboard page (1.7.0, 1.8.0): an opt-in, public publish that builds its own page, checks it, shows what
will be public and reads the file back. On the user's own terminal it asks for a typed word; in a session it goes on the user's
answer to the question of acts.py (a one-time grant), may make the branch and switch Pages on when that question said so, and
prints the page's link. The fake GitHub holds the target; no model runs and nothing reaches a real GitHub."""

import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import acts  # noqa: E402
import mind  # noqa: E402
import observer  # noqa: E402
import observer_grants as grants  # noqa: E402
import observer_runtime  # noqa: E402
import observer_store  # noqa: E402
import publish  # noqa: E402
import recortex  # noqa: E402
import usage  # noqa: E402

TARGET = "octo-org/octo-pages"
RECORD = ts.REPO
PAGE = ("<!doctype html><html><body><p class=\"snapshot quiet\">Snapshot built on an unknown day. Static snapshot.</p>"
        "<p>We chose strict quoting.</p><style>.x{color:red}</style><script>var a = 1;</script></body></html>")
COUNTS = {"memories": 12, "threads": 3}


class Tty:
    """The controlling terminal of the user: what they type, and what was asked."""

    def __init__(self, *typed):
        self.typed, self.asked = list(typed), []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ask(self, question):
        self.asked.append(question)
        return self.typed.pop(0) if self.typed else ""


class Box(ts.MindSandbox):
    def setUp(self):
        super().setUp()
        self.ready()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})
        data = self.repo()
        data.update(branches=["main", "gh-pages"], repos={TARGET: {"private": False, "push": True}, RECORD: {"private": False}})
        self.save(data)
        self.tty = Tty("publish")

    def puts(self):
        return self.repo().get("puts", [])

    def sends(self):
        return [entry for entry in self.repo()["log"] if entry[:3] == ["api", "-X", "PUT"]]

    def calls(self):
        return [entry for entry in self.repo()["log"] if entry and entry[0] == "api"]

    def run_publish(self, *argv, terminal=True, tty="default", page=PAGE, counts=COUNTS):
        out = io.StringIO()
        tty = self.tty if tty == "default" else tty
        with contextlib.redirect_stdout(out), mock.patch.object(observer, "on_terminal", return_value=terminal), \
                mock.patch.object(observer, "terminal", return_value=tty), \
                (mock.patch.object(mind, "public_text", return_value=(page, counts)) if page is not False
                 else contextlib.nullcontext()):
            try:
                code = publish.main(list(argv))
            except SystemExit as error:
                code = error.code
        return code, out.getvalue()

    def go(self, *extra, **kwargs):
        return self.run_publish("--to", TARGET, "--branch", "gh-pages", *extra, **kwargs)

    def stored(self, key="gh-pages:index.html"):
        entry = self.repo().get("files", {}).get(key)
        return base64.b64decode(entry) if entry else None


class OffATerminalTests(Box):
    def test_the_reads_need_no_answer_and_a_publish_without_one_names_its_question_and_sends_nothing(self):
        code, text = self.run_publish("--list", terminal=False)
        self.assertEqual((0, publish.NOTHING + "\n"), (code, text))
        code, text = self.run_publish("--to", TARGET, "--branch", "gh-pages", "--dry-run", terminal=False, tty=None)
        self.assertEqual(0, code, text)
        self.assertIn("dry run: nothing was sent", text)
        code, text = self.run_publish("--to", TARGET, "--branch", "gh-pages", terminal=False)
        self.assertEqual(1, code)
        self.assertIn("acts.py ask publish --to octo-org/octo-pages --branch gh-pages --path index.html", text)
        self.assertNotIn("terminal", text)
        self.assertEqual([], self.puts())
        self.assertEqual([], self.tty.asked)
        code, text = self.run_publish(terminal=False)
        self.assertEqual((1, "publish: say where with --to OWNER/NAME and --branch BRANCH (or --list).\n"), (code, text))

    def test_help_needs_no_terminal(self):
        code, text = self.run_publish("--help", terminal=False)
        self.assertEqual(0, code)
        self.assertIn("publish.py --to OWNER/NAME --branch BRANCH", text)


class PublishTests(Box):
    def test_a_public_record_asks_for_the_word_and_sends_the_page_that_public_text_returned(self):
        code, text = self.go()
        self.assertEqual(0, code, text)
        self.assertEqual(PAGE.encode("utf-8"), self.stored())
        self.assertEqual(1, len(self.puts()))
        self.assertEqual(1, len(self.sends()))
        self.assertEqual(["Type the word publish to confirm: "], self.tty.asked)
        self.assertIn("published and read back: https://github.com/octo-org/octo-pages/blob/gh-pages/index.html", text)
        self.assertIn(publish.NO_SITE, text)   # no site is on for the branch, and the question did not say it would be

    def test_the_warning_is_printed_whole_before_the_question(self):
        code, text = self.go()
        lines = text.splitlines()
        self.assertEqual("This page will be public. Anyone who has its address can read it, and search engines may list it.", lines[0])
        self.assertEqual("It holds what the dashboard tells: the text of 12 memories in 3 threads, the topic bodies, the tasks and the "
                         "project map of octo-org/octo-repo (1 KB).", lines[1])
        self.assertEqual("It goes to octo-org/octo-pages, branch gh-pages, file index.html, which it creates.", lines[2])
        self.assertIn("Not sent: the interactive dashboard, its address and token, launch files, your usage numbers and the "
                      "observer's log.", lines)
        self.assertIn("To take it down, delete the file from the branch. Copies that others made stay.", lines)
        self.assertNotIn("is PRIVATE", text)
        self.assertNotIn("The target repository is private", text)

    def test_a_private_target_and_a_private_record_add_their_lines(self):
        data = self.repo()
        data["repos"][TARGET]["private"] = True
        self.save(data)
        code, text = self.go()
        self.assertIn("The target repository is private. A site published from it can still be public; treat the page as public.",
                      text)

    def test_the_commit_message_is_one_plain_line_with_no_trailer(self):
        self.go()
        message = self.puts()[0]["message"]
        self.assertEqual("Publish the project dashboard page", message)
        for word in ("Co-Authored-By", "Generated", "claude", "Claude", "session"):
            self.assertNotIn(word, message)

    def test_a_replace_sends_the_sha_of_the_file_that_is_there(self):
        self.go()
        first = self.puts()[0]
        self.assertFalse(first["sha_given"])
        self.tty.typed.append("publish")
        code, text = self.go(page=PAGE.replace("strict", "relaxed"))
        self.assertEqual(0, code, text)
        self.assertIn("which it replaces.", text)
        second = self.puts()[1]
        self.assertTrue(second["sha_given"])
        self.assertEqual(PAGE.replace("strict", "relaxed").encode("utf-8"), self.stored())

    def test_the_path_may_be_a_nested_html_file(self):
        code, text = self.go("--path", "dashboards/sample-project.html")
        self.assertEqual(0, code, text)
        self.assertIsNotNone(self.stored("gh-pages:dashboards/sample-project.html"))

    def test_the_receipt_is_listed_and_strict(self):
        self.go()
        code, text = self.run_publish("--list")
        self.assertEqual(0, code)
        self.assertRegex(text, r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} UTC  octo-org/octo-pages  branch gh-pages  file "
                               r"index\.html  sha [0-9a-f]{40}\n$")
        self.assertIn(publish.blob_sha(PAGE.encode("utf-8")), text)
        path = publish.receipts_path(RECORD)
        with open(path, "w") as handle:
            handle.write('{"form": 1, "receipts": [{"at": 1, "to": "bad", "branch": "x", "path": "y", "sha": "z"}]}')
        os.chmod(path, 0o600)
        self.assertEqual((0, publish.NOTHING + "\n"), self.run_publish("--list"))

    def test_nothing_published_says_so(self):
        self.assertEqual((0, publish.NOTHING + "\n"), self.run_publish("--list"))

    def test_the_newest_twenty_receipts_are_kept(self):
        for number in range(23):
            publish.keep_receipt(RECORD, TARGET, "gh-pages", "index.html", "%040x" % number)
        rows = publish.read_receipts(RECORD)
        self.assertEqual(20, len(rows))
        self.assertEqual("%040x" % 22, rows[-1]["sha"])

    def test_a_receipt_that_cannot_be_written_is_one_line_and_the_publish_stands(self):
        with mock.patch.object(publish.storage, "write_file", side_effect=OSError("disk full")):
            code, text = self.go()
        self.assertEqual(0, code)
        self.assertIn("the receipt could not be kept on this machine; the page is published.", text)
        self.assertEqual(PAGE.encode("utf-8"), self.stored())


class DryRunTests(Box):
    def test_it_sends_nothing_asks_nothing_and_prints_the_commands(self):
        code, text = self.go("--dry-run", tty=None)
        self.assertEqual(0, code, text)
        self.assertEqual([], self.sends())
        self.assertEqual([], self.puts())
        digest = hashlib.sha256(PAGE.encode("utf-8")).hexdigest()
        self.assertIn("would run: gh api repos/octo-org/octo-pages", text)
        self.assertIn("would run: gh api repos/octo-org/octo-pages/branches/gh-pages", text)
        self.assertIn("would run: gh api repos/octo-org/octo-pages/contents/index.html?ref=gh-pages", text)
        self.assertIn("would run: gh api repos/octo-org/octo-pages/pages", text)
        self.assertIn("would run: gh api repos/octo-org/octo-repo", text)
        self.assertIn("would run: gh api -X PUT repos/octo-org/octo-pages/contents/index.html --input -", text)
        self.assertIn("<%d bytes, sha256 %s>" % (len(PAGE.encode("utf-8")), digest), text)
        self.assertNotIn(base64.b64encode(PAGE.encode("utf-8")).decode("ascii"), text)
        self.assertIn("dry run: nothing was sent", text)
        self.assertIn("This page will be public.", text)

    def test_the_dry_run_of_a_replace_names_the_sha(self):
        self.go()
        code, text = self.go("--dry-run", tty=None)
        self.assertIn('"sha": "%s"' % publish.blob_sha(PAGE.encode("utf-8")), text)


class StopTests(Box):
    def stops(self, line, **kwargs):
        code, text = self.go(**kwargs)
        self.assertEqual(1, code, text)
        self.assertIn(line, text)
        self.assertEqual([], self.sends(), text)
        self.assertEqual([], self.puts())

    def test_a_branch_that_does_not_exist_is_said_with_the_way_to_make_it(self):
        data = self.repo()
        data["branches"] = ["main"]
        self.save(data)
        self.stops(publish.BRANCH_NOT_SAID % ("gh-pages", TARGET))

    def test_an_account_that_may_not_write_stops_it(self):
        data = self.repo()
        data["repos"][TARGET]["push"] = False
        self.save(data)
        self.stops("publish: your account may not write to octo-org/octo-pages; nothing was sent.")

    def test_a_hold_of_the_users_on_publishing_stops_it_and_unreadable_holds_do_too(self):
        hold = {"state": "active", "action": "publish", "repo": RECORD.lower(), "scope": {}}
        with mock.patch.object(observer_store, "read_holds", return_value=[hold]):
            self.stops(publish.HELD)
        with mock.patch.object(observer_store, "read_holds", side_effect=observer_store.StoreError("unreadable")):
            self.stops(publish.HOLDS_UNREADABLE)
        with mock.patch.object(observer_store, "read_holds", return_value=["x"]):
            self.stops(publish.HOLDS_UNREADABLE)
        released = dict(hold, state="released")
        with mock.patch.object(observer_store, "read_holds", return_value=[released, {"state": "active", "action": "command"}]):
            code, text = self.go()
            self.assertEqual(0, code, text)   # another hold, or a released one, does not stop it
        self.assertEqual(1, len(self.puts()))

    def test_a_missing_queue_is_no_hold(self):
        self.assertEqual([], observer_store.read_holds(RECORD))
        code, text = self.go()
        self.assertEqual(0, code, text)

    def test_a_page_that_is_not_the_static_one_stops_it(self):
        addresses = ("http://127.0.0.1:8765/" + "a" * 32,)
        for page in (PAGE.replace("Static snapshot.", ""), PAGE + "Local interactive session.",
                     PAGE.replace("</body>", "<a href=\"%s/map-editor\">x</a></body>" % addresses[0])):
            self.stops(publish.NOT_STATIC, page=page)
        self.assertEqual("", publish.public_problem(PAGE))
        self.assertEqual("", publish.public_problem(PAGE.replace("</body>", "http://127.0.0.1:8765/x</body>")))   # no token: allowed text

    def test_a_secret_or_a_restricted_pattern_in_the_page_text_stops_it_and_is_never_printed(self):
        token = "ghp_" + "A1b2C3d4" * 5
        self.stops(publish.NOT_PRIVATE, page=PAGE.replace("We chose", "Use the token " + token + ". We chose"))
        code, text = self.go(page=PAGE.replace("We chose", "Use the token " + token + ". We chose"))
        self.assertNotIn(token, text)
        self.write_config(dict(self.config, restricted_patterns=[{"name": "instrument", "pattern": "Thermal calibration rig"}]))
        self.stops(publish.NOT_PRIVATE, page=PAGE.replace("We chose", "About the Thermal calibration rig. We chose"))
        self.assertEqual(0, self.go()[0])   # the page without it goes

    def test_only_the_text_is_checked_not_the_style_or_the_script(self):
        page = PAGE.replace("var a = 1;", "var token = 'ghp_" + "A1b2C3d4" * 5 + "';")
        self.assertNotIn("ghp_", publish.visible_text(page))
        self.assertIn("We chose strict quoting.", publish.visible_text(page))
        self.assertIn("&", publish.visible_text("<p>a &amp; b</p>"))   # entities are resolved

    def test_a_page_over_twenty_five_megabytes_stops_it(self):
        self.stops(publish.TOO_BIG, page=PAGE + "x" * (25 * 1024 * 1024))

    def test_a_wrong_typed_word_or_no_terminal_stops_it(self):
        for typed in ("", "yes", "Publish", "publish ", "publish\n"):
            self.stops(publish.NOT_CONFIRMED, tty=Tty(typed))
        self.stops(publish.NOT_CONFIRMED, tty=None)

    def test_a_target_off_the_forms_stops_it_before_any_call(self):
        before = len(self.repo()["log"])
        for argv in (["--to", "bad", "--branch", "gh-pages"], ["--to", TARGET, "--branch", "../x"],
                     ["--to", TARGET, "--branch", "a..b"], ["--to", TARGET, "--branch=-x"],
                     ["--to", TARGET, "--branch", "gh-pages", "--path", "../x.html"],
                     ["--to", TARGET, "--branch", "gh-pages", "--path", "/etc/x.html"],
                     ["--to", TARGET, "--branch", "gh-pages", "--path", "index.htm"],
                     ["--to", TARGET, "--branch", "gh-pages", "--path", "a/b/c/d/e.html"],
                     ["--to", TARGET, "--branch", "gh-pages", "--path", "a b.html"],
                     ["--to", TARGET, "--branch", "x\ny"]):
            code, text = self.run_publish(*argv)
            self.assertEqual((1, publish.BAD_TARGET + "\n"), (code, text), argv)
        self.assertEqual(before, len(self.repo()["log"]))
        self.assertEqual((1, "publish: say where with --to OWNER/NAME and --branch BRANCH (or --list).\n"),
                         self.run_publish("--to", TARGET))

    def test_a_project_that_is_not_set_up_or_not_trusted_stops_it(self):
        with open(self.trusted, "w") as handle:
            handle.write("")
        code, text = self.go()
        self.assertEqual(1, code)
        self.assertTrue(text.startswith("publish: "), text)
        self.assertEqual([], self.sends())

    def test_github_that_cannot_be_read_stops_it(self):
        with mock.patch.dict(os.environ, {"FAKE_GH_FAIL_ON": "api repos/octo-org/octo-pages"}):
            self.stops(publish.UNREADABLE_GITHUB)

    def test_a_write_that_github_refuses_publishes_nothing(self):
        with mock.patch.dict(os.environ, {"FAKE_GH_PUT_FAIL": "1"}):
            code, text = self.go()
        self.assertEqual((1, True), (code, publish.NOT_SENT in text))
        self.assertEqual([], self.puts())   # nothing was stored
        self.assertEqual([], publish.read_receipts(RECORD))

    def test_a_write_that_timed_out_is_not_said_to_have_published_nothing(self):
        # The 1.7.0 review, N6: gh gave up waiting, and the write may have landed.
        real = publish.recortex.gh

        def slow(args, *more, **kwargs):
            if args[:3] == ["api", "-X", "PUT"]:
                raise publish.recortex.GhTimeout("gh timed out after 120s: api -X PUT")
            return real(args, *more, **kwargs)
        with mock.patch.object(publish.recortex, "gh", side_effect=slow):
            code, text = self.go()
        self.assertEqual(1, code)
        self.assertIn(publish.UNCONFIRMED, text)
        self.assertNotIn("nothing was published", text)
        self.assertIn("look at the branch", publish.UNCONFIRMED)
        self.assertEqual([], publish.read_receipts(RECORD))   # nothing is claimed that was not read back

    def test_a_file_that_is_not_what_was_sent_is_said_and_no_receipt_is_kept(self):
        with mock.patch.dict(os.environ, {"FAKE_GH_PUT_CORRUPT": "1"}):
            code, text = self.go()
        self.assertEqual(1, code)
        self.assertIn(publish.MISMATCH, text)
        self.assertNotIn("published and read back", text)
        self.assertEqual([], publish.read_receipts(RECORD))


class SplitPatternTests(Box):
    """A restricted pattern or a secret that the markup splits, hides in an attribute or keeps only in the model is still found."""

    def restrict(self, pattern):
        self.write_config(dict(self.config, restricted_patterns=[{"name": "identifier", "pattern": pattern}]))

    def refused(self, text, **kwargs):
        before = len(self.puts())
        code, out = self.go(**kwargs)
        self.assertEqual((1, True), (code, publish.NOT_PRIVATE in out), out)
        self.assertEqual(before, len(self.puts()))
        self.assertNotIn("0042", out)   # what was found is never printed
        return out

    def test_a_pattern_split_across_two_inline_elements_is_found(self):
        self.restrict("SAMPLE-0042")
        page = PAGE.replace("We chose", "Participant SAMPLE-<mark>0042</mark> said we chose")
        self.assertNotIn("SAMPLE-0042", publish.page_texts(page)[0])   # joined with a line break it is split ...
        self.assertIn("SAMPLE-0042", publish.page_texts(page)[1])      # ... joined with nothing it is whole
        self.refused(page, page=page)

    def test_a_pattern_only_in_an_attribute_is_found_in_each_kind_of_attribute(self):
        self.restrict("SAMPLE-0042")
        for attribute in ("title", "alt", "aria-label", "value", "data-note", "placeholder"):
            page = PAGE.replace("<p>We chose", '<p %s="Participant SAMPLE-0042">We chose' % attribute)
            self.assertIn("SAMPLE-0042", publish.page_texts(page)[2], attribute)
            self.refused(page, page=page)
        self.assertEqual(0, self.go()[0])   # the page without it goes

    def test_a_secret_in_an_attribute_is_found_too(self):
        token = "ghp_" + "A1b2C3d4" * 5
        self.refused(PAGE, page=PAGE.replace("<p>We chose", '<p title="%s">We chose' % token))

    def test_style_and_script_are_still_left_out(self):
        self.restrict("SAMPLE-0042")
        page = PAGE.replace("var a = 1;", "var note = 'SAMPLE-0042';").replace(".x{color:red}", ".x::after{content:'SAMPLE-0042'}")
        for text in publish.page_texts(page):
            self.assertNotIn("SAMPLE-0042", text)
        code, out = self.go(page=page)
        self.assertEqual(0, code, out)

    def test_a_pattern_only_in_the_model_is_found_before_any_page_is_made(self):
        self.restrict("strict quoting")
        made = []
        real = mind.finish

        def finish(*args, **kwargs):
            made.append(1)
            return real(*args, **kwargs)
        with mock.patch.object(mind, "finish", side_effect=finish):
            self.refused(None, page=False)
        self.assertEqual([], made)   # the refusal came before the page was built

    def test_a_pattern_in_the_model_that_the_page_does_not_show_in_a_matching_form_is_found(self):
        self.restrict("strict quoting")
        with mock.patch.object(mind, "finish", return_value=(PAGE, [])):   # a page that shows nothing of it
            self.refused(None, page=False)

    def test_the_model_check_alone_stops_it_and_the_page_check_alone_stops_it(self):
        self.restrict("strict quoting")
        with mock.patch.object(publish, "page_problem", return_value=False):   # only the check of the record's own text is left
            self.refused(None, page=False)
        with mock.patch.object(publish, "source_problem", return_value=False):   # only the check of the page is left
            self.refused(None, page=False)
        self.write_config(dict(self.config))   # no rule: both pass and the page goes
        self.assertEqual(0, self.go(page=False)[0])

    def test_a_check_that_fails_stops_it_and_never_sends(self):
        import observer_worker
        with mock.patch.object(observer_worker, "privacy_problem", side_effect=RuntimeError("boom")):
            self.assertTrue(publish.source_problem(["x"], self.config))
            self.assertTrue(publish.page_problem(PAGE, self.config))
            self.refused(None, page=False)
        self.assertEqual([], self.puts())

    def test_the_clean_page_still_publishes_through_the_fake(self):
        self.restrict("SAMPLE-0042")
        code, out = self.go(page=False)   # the real page, built from the record, checked in every form
        self.assertEqual(0, code, out)
        self.assertEqual(1, len(self.puts()))

    def test_model_strings_reaches_everything_once_and_survives_a_cycle(self):
        loop = {"name": "loop"}
        loop["self"] = loop
        model = {"a": ["one", ("two", {"three"})], "b": {"key": "four", 5: [None, True, 1.5, "five"]}, "c": loop,
                 "d": frozenset(["six"]), "e": object.__new__(type("Odd", (), {"__str__": lambda self: "seven"}))}
        found = mind.model_strings(model)
        for word in ("one", "two", "three", "key", "four", "five", "six", "loop", "self", "seven", "a", "b", "c"):
            self.assertIn(word, found, word)
        self.assertEqual(1, found.count("loop"))   # a container that holds itself is walked once
        self.assertEqual([], [part for part in found if part in ("None", "True", "1.5")])   # numbers and flags are no text

    def test_the_real_models_strings_include_the_memories_text(self):
        strings = []
        mind.public_text(recortex.load_config(), RECORD, recortex.allowed_users(RECORD), refuse=lambda found: strings.extend(found) or False)
        self.assertTrue(any("strict quoting" in text for text in strings))


class PrivateRecordTests(Box):
    def private(self, value=True):
        data = self.repo()
        data["repos"][RECORD]["private"] = value
        self.save(data)

    def test_without_the_flag_it_stops_after_the_warning_and_asks_nothing(self):
        self.private()
        code, text = self.go()
        self.assertEqual(1, code)
        self.assertIn("The record's repository octo-org/octo-repo is PRIVATE. Publishing makes its contents public.", text)
        self.assertIn(publish.PRIVATE_NOT_SAID, text)
        self.assertEqual([], self.tty.asked)
        self.assertEqual([], self.sends())

    def test_with_the_flag_the_user_types_the_records_full_name_and_the_word_does_not_do(self):
        self.private()
        code, text = self.go("--private-record", tty=Tty("publish"))
        self.assertEqual((1, True), (code, publish.NOT_CONFIRMED in text))
        self.assertEqual([], self.sends())
        for typed in ("octo-org", "octo-repo", "octo-org/other-repo"):
            code, text = self.go("--private-record", tty=Tty(typed))
            self.assertEqual(1, code, typed)
            self.assertEqual([], self.sends(), typed)
        tty = Tty("OCTO-ORG/octo-repo")
        code, text = self.go("--private-record", tty=tty)
        self.assertEqual(0, code, text)
        self.assertEqual(["Type the record's full name, octo-org/octo-repo, to confirm: "], tty.asked)
        self.assertEqual(1, len(self.puts()))

    def test_a_visibility_that_cannot_be_read_counts_as_private(self):
        real = publish.read_repository

        def read(name):
            if name == RECORD:
                raise recortex.GhError("no answer")
            return real(name)
        with mock.patch.object(publish, "read_repository", side_effect=read):
            code, text = self.go()
            self.assertEqual(1, code)
            self.assertIn("is PRIVATE", text)
            self.assertIn(publish.PRIVATE_NOT_SAID, text)
            code, text = self.go("--private-record", tty=Tty(RECORD))
        self.assertEqual(0, code, text)

    def test_the_dry_run_of_a_private_record_needs_no_flag_and_sends_nothing(self):
        self.private()
        code, text = self.go("--dry-run", tty=None)
        self.assertEqual(0, code, text)
        self.assertIn("is PRIVATE", text)
        self.assertEqual([], self.sends())


class PageTests(Box):
    """public_text is the page mind.py builds, without this machine's own numbers and without the interactive part."""

    def test_the_page_is_static_has_no_usage_numbers_or_observer_log_and_none_is_fetched(self):
        with mock.patch.object(usage, "report", side_effect=AssertionError("usage read")), \
                mock.patch.object(usage, "acts_report", side_effect=AssertionError("acts read")), \
                mock.patch.object(mind.mind_figures, "load", wraps=mind.mind_figures.load) as load:
            text, counts = mind.public_text(recortex.load_config(), RECORD, recortex.allowed_users(RECORD))
        self.assertIn("Static snapshot.", text)
        self.assertNotIn("Local interactive session.", text)
        self.assertEqual("", publish.public_problem(text))
        self.assertGreater(counts["memories"], 0)
        self.assertEqual([], ts.page_problems(text))
        self.assertFalse(load.call_args.kwargs.get("fetch"))
        self.assertIsNone(publish.SERVICE_ADDRESS.search(text))

    def test_a_page_that_cannot_be_built_says_the_fixed_line_and_nothing_is_sent(self):
        os.remove(self.index_file())
        code, text = self.go(page=False)   # the real page is built, from an index that is gone
        self.assertEqual((1, mind.NO_INDEX + "\n"), (code, text))
        self.assertEqual([], self.sends())

    def test_mind_py_and_publish_build_the_same_page_from_the_same_steps(self):
        import inspect
        source = inspect.getsource(mind.main)
        self.assertIn("load_model(", source)
        self.assertIn("finish(", source)
        self.assertIn("load_model(", inspect.getsource(mind.public_text))
        self.assertIn("finish(", inspect.getsource(mind.public_text))

    def test_the_real_page_goes_through_publish_whole(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(observer, "on_terminal", return_value=True), \
                mock.patch.object(observer, "terminal", return_value=self.tty):
            code = publish.main(["--to", TARGET, "--branch", "gh-pages"])
        self.assertEqual(0, code, out.getvalue())
        sent = self.stored().decode("utf-8")
        self.assertIn("Static snapshot.", sent)
        self.assertEqual("", publish.public_problem(sent))
        self.assertIn("octo-org/octo-repo", sent)


class SessionTests(Box):
    """A session's publish goes on the user's answer: a one-time grant for exactly this target and these flags. GitHub's own
    answers are held against the flags, and the privacy checks, the static guard and the hold check still stop it."""

    def folder(self):
        return observer_runtime.folder_key()

    def values(self, **flags):
        base = {"record": RECORD.lower(), "to": TARGET, "branch": "gh-pages", "path": "index.html", "create": False, "pages": False,
                "private": False, "nosite": False}
        base.update(flags)
        return base

    def grant(self, **flags):
        values = self.values(**flags)
        self.assertTrue(grants.put(self.folder(), "publish", acts.ACTS["publish"].value(values), "toolu_01p%d" % len(self.repo()["log"])))
        return values

    def session(self, *extra, **kwargs):
        return self.go(*extra, terminal=False, tty=None, **kwargs)

    def test_the_answer_is_used_once_and_the_page_is_sent_with_no_question_on_a_terminal(self):
        self.grant()
        code, text = self.session()
        self.assertEqual(0, code, text)
        self.assertEqual(PAGE.encode("utf-8"), self.stored())
        self.assertEqual(1, len(self.puts()))
        code, text = self.session(page=PAGE.replace("strict", "relaxed"))   # the grant was spent
        self.assertEqual(1, code)
        self.assertIn("acts.py ask publish", text)
        self.assertEqual(1, len(self.puts()))

    def test_a_grant_for_the_public_question_does_not_publish_a_private_record(self):
        data = self.repo()
        data["repos"][RECORD]["private"] = True
        self.save(data)
        self.grant()
        code, text = self.session()
        self.assertEqual((1, True), (code, publish.PRIVATE_NOT_SAID in text))   # GitHub says private; the question did not
        self.assertEqual([], self.puts())
        code, text = self.session("--private-record")   # the flag, but a grant for another question
        self.assertEqual(1, code)
        self.assertIn("acts.py ask publish", text)
        self.assertEqual([], self.puts())
        self.grant(private=True)
        code, text = self.session("--private-record")
        self.assertEqual(0, code, text)
        self.assertEqual(1, len(self.puts()))
        self.assertTrue(publish.read_receipts(RECORD)[-1]["private"])

    def test_a_grant_without_create_branch_does_not_create_a_branch(self):
        data = self.repo()
        data["branches"] = ["main"]
        self.save(data)
        self.grant()
        code, text = self.session()
        self.assertEqual((1, True), (code, publish.BRANCH_NOT_SAID % ("gh-pages", TARGET) in text))
        self.assertEqual(["main"], self.repo()["branches"])
        self.assertNotIn("created_branches", self.repo())
        code, text = self.session("--create-branch")   # the flag, but a grant that did not say it
        self.assertEqual(1, code)
        self.assertNotIn("created_branches", self.repo())

    def test_create_branch_makes_one_commit_with_no_parent_that_holds_the_page_and_nojekyll(self):
        data = self.repo()
        data["branches"] = ["main"]
        self.save(data)
        self.grant(create=True)
        code, text = self.session("--create-branch")
        self.assertEqual(0, code, text)
        made = self.repo()["created_branches"]
        self.assertEqual(1, len(made))
        self.assertEqual({"branch": "gh-pages", "parents": [], "message": publish.MESSAGE, "paths": [".nojekyll", "index.html"]},
                         made[0])
        for word in ("Co-Authored-By", "Generated", "claude", "Claude", "session"):
            self.assertNotIn(word, made[0]["message"])
        self.assertEqual(PAGE.encode("utf-8"), self.stored())
        self.assertEqual("", self.repo()["files"]["gh-pages:.nojekyll"])   # an empty file
        self.assertIn("gh-pages", self.repo()["branches"])
        self.assertEqual([], self.puts())   # no contents write: the page went up with the commit
        self.assertIn("It makes the branch gh-pages there, holding only this page.", text)
        self.assertEqual(1, len(publish.read_receipts(RECORD)))

    def test_a_repository_with_no_commit_gives_one_line_and_makes_nothing(self):
        data = self.repo()
        data.update(branches=["main"], empty_repo=True)
        self.save(data)
        self.grant(create=True)
        code, text = self.session("--create-branch")
        self.assertEqual((1, True), (code, publish.EMPTY_REPOSITORY % TARGET in text))
        self.assertNotIn("created_branches", self.repo())
        self.assertEqual([], publish.read_receipts(RECORD))

    def test_pages_is_switched_on_only_with_the_flag_and_a_refusal_is_one_line_and_the_publish_stands(self):
        self.grant(pages=True)
        code, text = self.session("--pages")
        self.assertEqual(0, code, text)
        self.assertEqual({"branch": "gh-pages", "path": "/", "html_url": "https://octo-org.github.io/octo-pages/"},
                         self.repo()["pages"])
        self.assertIn("the page: https://octo-org.github.io/octo-pages/", text.splitlines())
        self.assertIn(publish.MINUTE, text)
        self.assertNotIn("github.com/octo-org/octo-pages/blob", text)
        self.assertIn("It switches GitHub Pages on for that branch, which makes the branch a public website.", text)
        data = self.repo()
        data.pop("pages")
        data["pages_refused"] = True
        self.save(data)
        self.grant(pages=True)
        code, text = self.session("--pages", page=PAGE.replace("strict", "relaxed"))
        self.assertEqual(0, code, text)
        self.assertIn(publish.PAGES_REFUSED % TARGET, text)
        self.assertEqual(PAGE.replace("strict", "relaxed").encode("utf-8"), self.stored())   # the page is on the branch
        self.assertNotIn(publish.NO_SITE, text)

    def test_without_the_flag_a_site_that_is_off_stays_off_and_the_line_says_how_to_get_one(self):
        self.grant()
        code, text = self.session()
        self.assertEqual(0, code, text)
        self.assertNotIn("pages", self.repo())
        self.assertIn(publish.NO_SITE, text)
        self.assertIn("published and read back: https://github.com/octo-org/octo-pages/blob/gh-pages/index.html", text)

    def test_the_link_is_printed_only_for_an_address_of_the_form_and_a_source_that_is_this_branch(self):
        good = {"branch": "gh-pages", "path": "/", "html_url": "https://octo-org.github.io/octo-pages/"}
        for case, pages, link in (
                ("good", good, True), ("a nested file", good, True),
                ("another branch", dict(good, branch="main"), False), ("another folder", dict(good, path="/docs"), False),
                ("characters off the form", dict(good, html_url="https://evil.example/a b/"), False),
                ("another scheme", dict(good, html_url="javascript:alert(1)//"), False),
                ("no slash at the end", dict(good, html_url="https://octo-org.github.io/octo-pages"), False),
                ("a script in the path", dict(good, html_url="https://octo-org.github.io/<x>/"), False),
                ("a deeper path", dict(good, html_url="https://octo-org.github.io/a/b/"), False),
                ("a text instead", dict(good, html_url="Visit https://x.test now"), False)):
            data = self.repo()
            data["pages"] = pages
            self.save(data)
            path = "dashboards/sample-project.html" if case == "a nested file" else "index.html"
            self.grant(path=path)
            code, text = self.session("--path", path, page=PAGE + case)
            self.assertEqual(0, code, (case, text))
            if link:
                self.assertIn("the page: " + good["html_url"] + ("" if path == "index.html" else path), text.splitlines(), case)
            else:
                self.assertNotIn("the page:", text, case)
                self.assertIn("published and read back: https://github.com/octo-org/octo-pages/blob/gh-pages/%s" % path, text, case)
            self.assertNotIn("evil.example", text, case)
            self.assertNotIn("javascript", text, case)

    def test_the_privacy_check_the_static_guard_and_the_hold_check_each_still_stop_a_session_with_a_valid_grant(self):
        token = "ghp_" + "A1b2C3d4" * 5
        hold = {"state": "active", "action": "publish", "repo": RECORD.lower(), "scope": {}}
        cases = (("a secret in the page", {"page": PAGE.replace("We chose", "Use " + token + ". We chose")}, publish.NOT_PRIVATE),
                 ("not the static page", {"page": PAGE.replace("Static snapshot.", "")}, publish.NOT_STATIC),
                 ("a page too big", {"page": PAGE + "x" * (25 * 1024 * 1024)}, publish.TOO_BIG))
        for name, kwargs, line in cases:
            self.grant()
            code, text = self.session(**kwargs)
            self.assertEqual((1, True), (code, line in text), name)
            self.assertNotIn(token, text)
            self.assertEqual([], self.puts(), name)
            self.assertTrue(grants.held(self.folder(), [("publish", acts.ACTS["publish"].value(self.values()))]), name)   # unspent
        with mock.patch.object(observer_store, "read_holds", return_value=[hold]):
            code, text = self.session()
        self.assertEqual((1, True), (code, publish.HELD in text))
        self.assertEqual([], self.puts())
        with mock.patch.dict(os.environ, {"FAKE_GH_PUT_CORRUPT": "1"}):
            code, text = self.session()
        self.assertEqual((1, True), (code, publish.MISMATCH in text))   # the read-back
        self.assertEqual([], publish.read_receipts(RECORD))

    def test_a_subagent_cannot_publish_and_dry_run_and_list_are_not_denied_to_it(self):
        import pretool_guard
        for command in ("python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages",
                        "python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages --create-branch --pages"):
            out = pretool_guard.decide(json.dumps({"tool_name": "Bash", "agent_id": "a1", "tool_input": {"command": command}}))
            self.assertEqual(pretool_guard.SUBAGENT_DENIAL, out["hookSpecificOutput"]["permissionDecisionReason"], command)
        for command in ("python3 /x/publish.py --list", "python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages --dry-run"):
            self.assertIsNone(pretool_guard.decide(json.dumps({"tool_name": "Bash", "agent_id": "a1",
                                                               "tool_input": {"command": command}})), command)

    def test_a_receipt_with_and_without_the_private_key_is_read(self):
        path = publish.receipts_path(RECORD)
        rows = [{"at": 1763900000, "to": TARGET, "branch": "gh-pages", "path": "index.html", "sha": "a" * 40},
                {"at": 1763900001, "to": TARGET, "branch": "gh-pages", "path": "index.html", "sha": "b" * 40, "private": True},
                {"at": 1763900002, "to": TARGET, "branch": "gh-pages", "path": "index.html", "sha": "c" * 40, "private": None},
                {"at": 1763900003, "to": TARGET, "branch": "gh-pages", "path": "index.html", "sha": "d" * 40, "private": "yes"},
                {"at": 1763900004, "to": TARGET, "branch": "gh-pages", "path": "index.html", "sha": "e" * 40, "private": False,
                 "extra": 1}]
        with open(path, "w") as handle:
            json.dump({"form": 1, "receipts": rows}, handle)
        os.chmod(path, 0o600)
        self.assertEqual(["a" * 40, "b" * 40, "c" * 40], [row["sha"] for row in publish.read_receipts(RECORD)])
        publish.keep_receipt(RECORD, TARGET, "gh-pages", "index.html", "f" * 40, False)
        self.assertIs(False, publish.read_receipts(RECORD)[-1]["private"])


class AskPublishTests(Box):
    """`acts.py ask publish` reads GitHub and sets the flags; the hook rebuilds the same question from the command alone."""

    def ask(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = acts.main(["ask", "publish", "--json"] + list(argv))
        return code, out.getvalue()

    def body(self, *argv):
        code, printed = self.ask(*argv)
        self.assertEqual(0, code, printed)
        return json.loads(printed)

    def roundtrip(self, body):
        yes = [o for o in body["options"] if o["answer"] == "yes"][0]
        found = [i for i in acts.asks(yes["run"][0], self.project) if i["kind"] == "publish"]
        self.assertEqual(1, len(found), yes["run"])
        self.assertEqual(body["question"], found[0]["text"])
        self.assertEqual([o["label"] for o in body["options"]], found[0]["labels"])
        self.assertEqual(yes["label"], found[0]["label"])

    def test_a_target_that_exists_is_the_plain_update(self):
        data = self.repo()
        data["pages"] = {"branch": "gh-pages", "path": "/", "html_url": "https://octo-org.github.io/octo-pages/"}
        self.save(data)
        body = self.body("--to", TARGET)
        self.assertEqual(["Update the public page", "Show the file's path"], [o["label"] for o in body["options"]])
        self.assertTrue(body["question"].startswith(
            "Update the public page of the recortex dashboard for octo-org/octo-repo? It goes to octo-org/octo-pages, branch gh-pages, "
            "file index.html. Anyone who has its address can read it, and search engines may list it. It holds what the dashboard "
            "tells: the memories, the topic texts, the tasks and the project map. Your usage numbers, the observer's log and the "
            "interactive dashboard are not sent."))
        self.assertEqual(["python3 %s --to octo-org/octo-pages --branch gh-pages --path index.html"
                          % os.path.join(ts.SCRIPTS, "publish.py")], body["options"][0]["run"])
        self.roundtrip(body)

    def test_each_thing_that_is_missing_adds_its_flag_its_sentence_and_the_label(self):
        data = self.repo()
        data["branches"] = ["main"]
        self.save(data)
        body = self.body("--to", TARGET)
        self.assertEqual("Set up the public page", body["options"][0]["label"])
        self.assertIn("recortex creates the branch gh-pages there, holding only this page. recortex switches GitHub Pages on for "
                      "that branch, which makes the branch a public website. Anyone", body["question"])
        self.assertTrue(body["options"][0]["run"][0].endswith("--path index.html --create-branch --pages"))
        self.roundtrip(body)
        data = self.repo()
        data["repos"][RECORD]["private"] = True
        self.save(data)
        body = self.body("--to", TARGET)
        self.assertEqual("Publish the private record", body["options"][0]["label"])
        self.assertIn("octo-org/octo-repo is a PRIVATE repository: this makes what its dashboard tells public.", body["question"])
        self.assertTrue(body["options"][0]["run"][0].endswith("--create-branch --pages --private-record"))
        self.roundtrip(body)

    def test_pages_that_serves_another_branch_is_said_and_gets_no_pages_flag(self):
        data = self.repo()
        data["pages"] = {"branch": "main", "path": "/", "html_url": "https://octo-org.github.io/octo-pages/"}
        self.save(data)
        body = self.body("--to", TARGET)
        self.assertIn("GitHub Pages serves another branch of octo-org/octo-pages, so you get the file's address on GitHub, not a "
                      "site.", body["question"])
        self.assertTrue(body["options"][0]["run"][0].endswith("--path index.html --no-site"))
        self.assertNotIn("--pages", body["options"][0]["run"][0])
        self.roundtrip(body)

    def test_where_the_target_cannot_be_used_one_fixed_line_and_no_question(self):
        data = self.repo()
        data["repos"][TARGET]["push"] = False
        self.save(data)
        code, printed = self.ask("--to", TARGET)
        self.assertEqual((1, publish.NO_PUSH % TARGET + "\n"), (code, printed))
        data["repos"][TARGET]["push"] = True
        self.save(data)
        with mock.patch.dict(os.environ, {"FAKE_GH_FAIL_ON": "api repos/octo-org/octo-pages"}):
            code, printed = self.ask("--to", TARGET)
        self.assertEqual(1, code)
        self.assertNotIn("question", printed)
        self.assertEqual(1, self.ask("--to", "bad")[0])
        self.assertEqual(1, self.ask("--to", TARGET, "--branch", "../x")[0])

    def test_the_default_is_the_newest_receipt_else_the_record_itself(self):
        body = self.body("--branch", "gh-pages", "--to", RECORD)
        self.assertIn("It goes to octo-org/octo-repo, branch gh-pages", body["question"])
        data = self.repo()
        data["branches"] = ["main", "gh-pages"]
        self.save(data)
        publish.keep_receipt(RECORD, TARGET, "gh-pages", "dashboards/sample-project.html", "a" * 40, False)
        body = self.body()
        self.assertIn("It goes to octo-org/octo-pages, branch gh-pages, file dashboards/sample-project.html.", body["question"])

    def test_the_command_of_a_dry_run_or_a_list_has_no_question(self):
        for command in ("python3 /x/publish.py --to octo-org/octo-pages --branch gh-pages --dry-run", "python3 /x/publish.py --list",
                        "python3 /x/publish.py --to octo-org/octo-pages"):
            self.assertEqual([], [i for i in acts.asks(command, self.project) if i["kind"] == "publish"], command)


if __name__ == "__main__":
    unittest.main()

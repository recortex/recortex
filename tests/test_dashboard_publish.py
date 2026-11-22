"""Publishing the static dashboard page (1.7.0): an opt-in, terminal-only, public publish that builds its own page, checks it, shows
what will be public, asks for a typed confirmation and reads the file back. The fake GitHub holds the target; no model runs and
nothing reaches a real GitHub."""

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
import mind  # noqa: E402
import observer  # noqa: E402
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
    def test_every_form_stops_with_one_line_and_touches_nothing(self):
        before = len(self.repo()["log"])
        for argv in (["--list"], ["--to", TARGET, "--branch", "gh-pages"], ["--to", TARGET, "--branch", "gh-pages", "--dry-run"],
                     ["--to", TARGET, "--branch", "gh-pages", "--private-record"], []):
            code, text = self.run_publish(*argv, terminal=False)
            self.assertEqual((1, publish.NOT_TERMINAL + "\n"), (code, text), argv)
        self.assertEqual(before, len(self.repo()["log"]))
        self.assertEqual([], self.puts())
        self.assertEqual([], self.tty.asked)

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
        self.assertIn("If octo-org/octo-pages serves this branch with GitHub Pages, the page is at its Pages address; publish.py "
                      "does not switch Pages on.", text)

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
        self.stops("publish: the branch gh-pages does not exist in octo-org/octo-pages. publish.py never creates a branch, a "
                   "repository or a site. Create it yourself, for example: git switch --orphan gh-pages; git commit "
                   "--allow-empty -m 'Start the page'; git push origin gh-pages. Then run this again.")

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
        self.assertIn(publish.PRIVATE_RECORD, text)
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
            self.assertIn(publish.PRIVATE_RECORD, text)
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


if __name__ == "__main__":
    unittest.main()

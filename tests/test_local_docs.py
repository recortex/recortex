"""The documents of a local record (1.9.0, spec 14): every operation of the record interface is named, every line of the off features is
in the reference, the new text holds none of the banned words or phrases and shows no real project, and SKILL.md links the page."""

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_jobs_docs import BANNED  # noqa: E402
from test_jobs_handoff import PHRASES  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "recortex")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
import local_store  # noqa: E402
import record_store  # noqa: E402

HISTORY = re.compile(r"(?i)\bnew in\b|\bnow also\b|\bused to\b|\bpreviously\b|\bno longer\b")
NEW_DOCS = [("skills", "recortex", "references", "local-record.md")]
TOUCHED = [("README.md",), ("CONTRIBUTING.md",), ("docs", "install.md"), ("docs", "how-it-works.md"), ("docs", "security.md"),
           ("docs", "reference.md"), ("docs", "existing-work.md")] + [
    ("skills", "recortex", "references", name) for name in ("local-record.md", "setup.md", "issue-model.md", "hooks-and-index.md",
                                                            "posting-and-verification.md", "board.md", "triage.md", "versions.md",
                                                            "writing.md", "deliverables.md", "migration.md", "pitfalls.md", "mind.md")]


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def local_section(text):
    """The part of a page that is about a local record: the whole of local-record.md, else what follows its heading."""
    return text


class OperationTests(unittest.TestCase):
    def test_every_operation_of_the_record_interface_is_named_in_the_page_or_in_contributing(self):
        names = [name for name, _ in inspect.getmembers(record_store.GithubStore, inspect.isfunction) if not name.startswith("_")]
        names += [name for name, _ in inspect.getmembers(local_store.LocalStore, inspect.isfunction)
                  if not name.startswith("_") and name in ("create_issue", "edit_body", "post_entry", "versions", "adopt", "adopt_plan")]
        documented = read("skills", "recortex", "references", "local-record.md") + read("CONTRIBUTING.md")
        for name in sorted(set(names)):
            self.assertIn(name, documented, "the operation %s is named in neither local-record.md nor CONTRIBUTING.md" % name)

    def test_the_interface_of_a_local_record_has_every_operation_of_the_github_one(self):
        github = {name for name, _ in inspect.getmembers(record_store.GithubStore, inspect.isfunction) if not name.startswith("_")}
        local = {name for name, _ in inspect.getmembers(local_store.LocalStore, inspect.isfunction) if not name.startswith("_")}
        self.assertEqual(set(), github - local - {"github", "command", "create_label", "edit_label", "pin", "is_pinned", "visibility", "dump", "dump_commands"},
                         "an operation of GithubStore that LocalStore lacks, and the page does not say it is off")


class OffLineTests(unittest.TestCase):
    def test_every_line_of_the_off_features_is_in_the_reference(self):
        reference = read("docs", "reference.md")
        for key, line in record_store.OFF.items():
            if line and key not in ("label-catalog", "visibility"):   # these two are said by setup, in its own words
                self.assertIn(line, reference, key)

    def test_the_off_lines_name_no_command_and_no_terminal(self):
        for key, line in list(record_store.OFF.items()) + [("snapshot", record_store.SNAPSHOT_OFF)]:
            if line:
                self.assertIsNone(PHRASES.search(line), key)
                self.assertNotRegex(line, r"(?i)\brun\b")


class WordsTests(unittest.TestCase):
    def test_the_documents_hold_none_of_the_banned_words_or_the_history_words(self):
        for path in TOUCHED:
            text = read(*path)
            for word in BANNED:
                self.assertNotIn(word, text.lower(), "/".join(path))
            if path in NEW_DOCS:
                self.assertIsNone(HISTORY.search(text), "/".join(path))

    def test_no_page_written_for_a_local_record_tells_the_user_to_use_a_terminal(self):
        for path in NEW_DOCS + [("README.md",)]:
            self.assertIsNone(PHRASES.search(read(*path)), "/".join(path))

    def test_the_documents_show_no_real_project(self):
        for path in TOUCHED:
            text = read(*path)
            self.assertNotRegex(text, r"/home/[a-z]+/", "/".join(path))
            self.assertNotRegex(text, r"(?i)mr-eyes|mabuelanin|abuelanin", "/".join(path))
        text = read("skills", "recortex", "references", "local-record.md")
        self.assertIn("sample project", text)
        self.assertIn("local--record/k3f5x2m7q4w6z5ab", text)


class LinkTests(unittest.TestCase):
    def test_skill_md_links_the_page_and_names_the_three_questions(self):
        skill = read("skills", "recortex", "SKILL.md")
        self.assertIn("references/local-record.md", skill)
        for kind in ("`inproject`", "`folder`", "`adopt`"):
            self.assertIn(kind, skill)
        observer = read("skills", "recortex", "references", "observer.md")
        for kind in ("`inproject`", "`folder`", "`adopt`"):
            self.assertIn("| %s |" % kind, observer)

    def test_the_page_describes_every_state_of_a_file_and_the_two_questions(self):
        text = read("skills", "recortex", "references", "local-record.md")
        for word in ("sealed", "changed", "foreign", "unread", "#N/ID", "acts.py ask folder", "acts.py ask adopt", "hooks.py --doctor"):
            self.assertIn(word, text)

    def test_the_readme_names_both_homes_and_stays_short(self):
        text = read("README.md")
        self.assertIn("Where the record lives", text)
        self.assertIn("a folder of Markdown files", text)
        self.assertLess(len(text.split()), 1000)


if __name__ == "__main__":
    unittest.main()

"""Parity (1.9.0, spec 12.3): the example record is built twice from the same manifest and drafts, once on `fake_gh.py` and once on a
local record; both are synced, and the tables the index derives from the text (events, sections, blocks, sources, links, labels,
steps) are compared with numbers, ids, addresses and authors mapped. A difference fails with the table and the row. Then the four
hook payloads are compared line by line after the same mapping and after the store's words are replaced. Offline."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_record import LocalSandbox, ROOT, SCRIPTS  # noqa: E402

FAKE_GH = os.path.join(ROOT, "tests", "fake_gh.py")
EXAMPLE = os.path.join(ROOT, "examples", "octo-repo")
GH_REPO = "octo-org/octo-repo"
NUMBERS = re.compile(r"#[0-9]+(?:/[0-9]+)?|(?<=Issue )[0-9]+")


def normal(text):
    """Numbers, ids and addresses of an issue or an entry are mapped to #N."""
    return NUMBERS.sub("#N", text or "")


class Kept(dict):
    """issue number -> title; a number of an issue that is left out answers None, and the rows of such an issue are dropped."""

    def __missing__(self, number):
        return None


def collect(rows):
    """The rows of an issue that is kept (the first part of a row names it), in an order that any part may take."""
    return sorted((row for row in rows if row[0] is not None), key=repr)


class Parity(LocalSandbox):
    seeded = False
    trusted = True

    def make_folder(self):
        folder = super().make_folder()
        return folder

    def setUp(self):
        super().setUp()
        self.config = dict(self.config, topics=["reader", "speed"])
        self.write_config(self.config)
        # the GitHub side: the same example in the fake, in a project of its own, with its own folders
        self.gh = os.path.join(self.dir, "github-side")
        self.gh_project = os.path.join(self.gh, "project")
        os.makedirs(self.gh_project)
        self.gh_state = os.path.join(self.gh, "state.json")
        with open(os.path.join(ROOT, "tests", "fixtures", "repo-state.json")) as handle:
            state = json.load(handle)
        state["issues"], state["comments"], state["next_issue"] = {}, {}, 1   # the example alone, so that the hub is #1 on both sides
        with open(self.gh_state, "w") as handle:
            json.dump(state, handle)
        self.gh_trusted = os.path.join(self.gh, "trusted")
        with open(self.gh_trusted, "w") as handle:
            handle.write("%s octocat\n" % GH_REPO)
        with open(os.path.join(self.gh_project, ".recortex.json"), "w") as handle:
            json.dump({"repo": GH_REPO, "hub": 1, "profiles": ["software", "research"], "topics": ["reader", "speed"]}, handle)
        self.gh_env = dict(os.environ, RECORTEX_GH="python3 %s" % FAKE_GH, FAKE_GH_STATE=self.gh_state, RECORTEX_TRUSTED_FILE=self.gh_trusted,
                           CLAUDE_PROJECT_DIR=self.gh_project, XDG_STATE_HOME=os.path.join(self.gh, "xs"),
                           XDG_CACHE_HOME=os.path.join(self.gh, "xc"), XDG_CONFIG_HOME=os.path.join(self.gh, "xf"),
                           XDG_DATA_HOME=os.path.join(self.gh, "xd"), HOME=self.gh)

    def run_github(self, script, *args, cwd=None):
        done = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + list(args), capture_output=True, text=True,
                              env=self.gh_env, cwd=cwd or self.gh_project, input="", timeout=240)
        self.assertEqual(0, done.returncode, "github side %s: %s" % (script, (done.stdout + done.stderr)[-1500:]))
        return done.stdout

    def build_both(self):
        drafts = os.path.join(self.gh, "drafts")
        shutil.copytree(EXAMPLE, drafts)
        self.run_github("setup_labels.py", "--repo", GH_REPO, "--profile", "software,research", "--topic", "reader", "--topic", "speed",
                        cwd=drafts)
        self.run_github("create_issues.py", "--manifest", os.path.join(drafts, "manifest.json"), cwd=drafts)
        self.run_github("index.py", "sync")
        local_drafts = os.path.join(self.project, ".tmp", "drafts")
        os.makedirs(os.path.dirname(local_drafts), exist_ok=True)
        shutil.copytree(EXAMPLE, local_drafts)
        manifest = os.path.join(local_drafts, "manifest.json")
        with open(manifest) as handle:
            data = json.load(handle)
        data["repo"] = self.repo
        with open(manifest, "w") as handle:
            json.dump(data, handle)
        for args in (("create_issues.py", "--manifest", manifest), ("index.py", "sync")):
            done = self.run_script(*args)
            self.assertEqual(0, done.returncode, "local side %s: %s" % (args[0], (done.stdout + done.stderr)[-1500:]))

    def database(self, side):
        if side == "github":
            path = os.path.join(self.gh, "xc", "recortex", "private", GH_REPO.replace("/", "__"), "index.sqlite")
        else:
            path = os.path.join(self.dir, "cache", "recortex", "private", self.repo.replace("/", "__"), "index.sqlite")
        self.assertTrue(os.path.exists(path), path)
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    @staticmethod
    def derived(db, only=None):
        """The derived rows of the issues titled in `only` (the fake's fixture holds three older issues the example does not)."""
        titles = Kept({row["number"]: row["title"] for row in db.execute("SELECT number, title FROM issues")
                       if only is None or row["title"] in only})
        kinds = {row["comment"]: (titles[row["issue"]], row["type"]) for row in db.execute("SELECT comment, issue, type FROM events")
                 if row["issue"] in titles}
        out = {
            "issues": collect((row["title"], row["state"], normal(row["body"])) for row in db.execute("SELECT title, state, body FROM issues")
                             if only is None or row["title"] in only),
            "labels": collect((titles[row["issue"]], row["label"]) for row in db.execute("SELECT issue, label FROM labels")),
            "events": collect((titles[row["issue"]], row["type"], row["at"], row["title"]) for row in
                             db.execute("SELECT issue, type, at, title FROM events")),
            "sections": collect((titles[row["issue"]], row["ordinal"], row["heading"], normal(row["text"])) for row in
                               db.execute("SELECT issue, ordinal, heading, text FROM sections")),
            "blocks": collect((titles[row["issue"]], row["kind"], row["id"], row["title"], row["section"], normal(row["caption"])) for row in
                             db.execute("SELECT issue, kind, id, title, section, caption FROM blocks")),
            "sources": collect((titles[row["issue"]], row["ordinal"], normal(row["statement"]), normal(row["source"])) for row in
                              db.execute("SELECT issue, ordinal, statement, source FROM sources")),
            "links": collect((kinds.get(row["source"]), row["kind"], kinds.get(row["target"])) for row in
                            db.execute("SELECT source, kind, target FROM links")),
            "steps": collect((kinds.get(row["comment"]), row["ordinal"], row["text"]) for row in
                            db.execute("SELECT comment, ordinal, text FROM steps")),
            "event_fields": collect((kinds.get(row["comment"]), row["ordinal"], row["label"], normal(row["text"])) for row in
                                   db.execute("SELECT comment, ordinal, label, text FROM event_fields")),
        }
        return out


class DerivedTablesTests(Parity):
    def test_the_example_record_derives_the_same_tables_on_both_stores(self):
        self.build_both()
        local_db = self.database("local")
        example = {row["title"] for row in local_db.execute("SELECT title FROM issues")}
        github, local = self.derived(self.database("github"), example), self.derived(local_db, example)
        self.assertEqual(sorted(github), sorted(local))
        self.assertTrue(github["events"] and github["sections"] and github["labels"])
        for table in sorted(github):
            only_github = [row for row in github[table] if row not in local[table]]
            only_local = [row for row in local[table] if row not in github[table]]
            self.assertEqual(([], []), (only_github, only_local), "the table %s differs: first on GitHub only, then local only" % table)
            self.assertEqual(len(github[table]), len(local[table]), table)


class PayloadTests(Parity):
    def payloads(self, side):
        if side == "github":
            return self.run_github("hooks.py", "--print")
        done = self.run_script("hooks.py", "--print")
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout

    def mapped(self, text, side):
        """A payload with what belongs to one store alone replaced: the project's name, the store's words for its place and its way of
        reading, the authors, the addresses, the numbers and the sizes."""
        text = normal(text)
        text = re.sub(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?Z?", "TIME", text)
        text = re.sub(r"[0-9]+ characters", "N characters", text)
        text = re.sub(r"https://github\.com/\S+?(?=[.,]?(?:\s|$))", "ADDRESS", text)
        text = re.sub(r"(?m)^(Hub: #N .*?) (?:ADDRESS|#N)$", r"\1 ADDRESS", text)
        text = re.sub(r"by (?:octocat|local--record), never edited, (?:ADDRESS|#N)\.", "by AUTHOR, never edited, ADDRESS.", text)
        text = re.sub(r"Rules: (?:The GitHub issues of \S+ are|The local record of this project is)", "Rules: the record is", text)
        text = re.sub(r"is (?:the GitHub issues of \S+|the local record of this project),", "is the record,", text)
        text = text.replace("read from GitHub.", "read from the record.").replace("read from the local record.", "read from the record.")
        text = text.replace("(gh issue view N --comments)", "(the recall of the skill)").replace("(the recortex skill's recall)", "(the recall of the skill)")
        text = text.replace(GH_REPO, "REPO").replace(self.repo, "REPO")
        text = re.sub(r"\bthe GitHub issues of \S+?(?=[,.;)]|\s)|\bthe local record of this project\b", "the record", text)
        text = re.sub(r"\bGitHub\b|\bthe local record\b", "the record", text)   # the store's own words, last
        lines = [line.rstrip() for line in text.splitlines() if line.strip() and not line.startswith("config:")]
        # a task that is old is old by the clock of the fixture and not by the store
        return [line for line in lines if not re.fullmatch(r"[0-9]+ tasks? had no memory in 14 days: .*", line)]

    def test_the_four_payloads_say_the_same_after_the_stores_words_are_replaced(self):
        self.build_both()
        github, local = self.mapped(self.payloads("github"), "github"), self.mapped(self.payloads("local"), "local")
        self.assertTrue(github and local)
        self.assertEqual(len(github), len(local), "the payloads differ in length")
        for number, (a, b) in enumerate(zip(github, local), 1):
            self.assertEqual(a, b, "line %d of the payloads differs: first the GitHub record, then the local one" % number)


if __name__ == "__main__":
    unittest.main()

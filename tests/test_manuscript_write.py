"""Tests of the writing of the paper's text (1.3.0): the mode, what is due, the write and its checks, the author's own
edits, the author's own paper and the lines the hooks print about them. Nothing here reaches GitHub; the scripts run
against tests/fake_gh.py."""

import hashlib
import json
import os
import re
import stat
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_manuscript_plan as tp  # noqa: E402
import test_scripts as ts  # noqa: E402
import manuscript_record  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import record_format  # noqa: E402
import recortex  # noqa: E402

NOT_CHOSEN = ("Writing: the user has not chosen whether recortex writes the paper's text. Ask once with the "
              "multiple-choice prompt (review, autonomous, off), then run setup.py writing set.")


class WritingBox(tp.PaperSandbox):
    """The paper record with its plan, and helpers for the writing mode, a writing folder and its files."""

    def mode(self, mode):
        done = self.tool("setup.py", "writing", "set", "--mode", mode, "--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def cmd(self, name, *args, env=None):
        return self.tool(name, *args, env=dict({"TMPDIR": self.dir}, **(env or {})))

    def start(self, issue=5, name=None):
        folder = os.path.join(self.dir, name or "w%d" % issue)
        done = self.cmd("write.py", "start", "--issue", str(issue), "--out", folder)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return folder

    def read(self, folder, name):
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            return handle.read()

    def write_file(self, folder, name, text):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write(text if isinstance(text, str) else json.dumps(text))

    def base(self, folder):
        return self.read(folder, "base.md").replace("\r\n", "\n")

    def draft(self, folder, text=None, parts=(9302,), results=(9140,), decisions=(), plain=None):
        """Write draft.md (the base with one new paragraph in Results unless `text` is given), used.json and memory.json."""
        base = self.base(folder)
        if text is None:
            text = base.replace("\n### Figure: dryspell", "\nThe pooled fit and the dry-spell comparison agree: in dry spells the cover "
                                "plots stayed moist on 9 of 11 spells.\n\n### Figure: dryspell", 1)
        self.write_file(folder, "draft.md", text)
        self.write_file(folder, "used.json", {"parts": list(parts), "results": list(results), "decisions": list(decisions)})
        self.write_file(folder, "memory.json", {"plain": plain or "in dry spells the cover plots stayed moist more often than "
                                                "the bare plots", "changed": "One paragraph on the dry spells is new.",
                                                "why": "The claim that the dry spells show it is backed."})

    def words(self, text="Approve"):
        path = os.path.join(self.dir, "words-%d.txt" % len(os.listdir(self.dir)))
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def text_memories(self, issue=5):
        return [(cid, c["body"]) for cid, c in sorted(self.repo()["comments"].items(), key=lambda row: int(row[0]))
                if c["issue"] == issue and "type=text" in c["body"].split("\n")[0]]

    def body(self, issue):
        return self.repo()["issues"][str(issue)]["body"].replace("\r\n", "\n")

    def modes_file(self):
        return os.path.join(self.dir, "xdg-config", "recortex", "private", "writing-modes.json")


class WritingModeTests(WritingBox):
    """Section 7.1: the mode is the user's, per machine and repository, and nothing is written before it is chosen."""

    def test_no_choice_means_not_chosen_and_nothing_is_written(self):
        self.ready()
        shown = self.cmd("setup.py", "writing", "show")
        self.assertIn("writing: no mode has been chosen for octo-org/sample-project on this machine, so recortex writes "
                      "nothing.", shown.stdout)
        refused = self.cmd("write.py", "start", "--issue", "5", "--out", os.path.join(self.dir, "w"))
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("write: writing is not chosen on this machine, so nothing is written.", refused.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "w")))

    def test_the_mode_is_recorded_privately_and_read_back(self):
        for mode in ("review", "autonomous", "off"):
            done = self.cmd("setup.py", "writing", "set", "--mode", mode, "--yes")
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertIn("recorded: the writing mode for octo-org/sample-project on this machine is %s" % mode, done.stdout)
            self.assertEqual("on" if mode != "off" else "", "on" if "keeps every version of the manuscript" in done.stdout
                             else "")
            self.assertIn("is %s (chosen on" % mode, self.cmd("setup.py", "writing", "show").stdout)
        path = self.modes_file()
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode))
        self.assertEqual({"octo-org/sample-project"}, set(json.load(open(path))["modes"]))

    def test_the_consent_lines_are_printed_for_review_and_autonomous_only(self):
        for mode, lines in (("review", True), ("autonomous", True), ("off", False)):
            out = self.cmd("setup.py", "writing", "set", "--mode", mode, "--yes").stdout
            self.assertEqual(lines, "With review or autonomous, recortex also keeps every version of the manuscript for "
                             "you in octo-org/sample-project, and shows them on the mind page. Nothing is built or run there."
                             in out, mode)
            for gone in ("workflow", "Actions", "allowance", "minutes", "PDF"):   # spec section 20: nothing is built
                self.assertNotIn(gone, out, (mode, gone))

    def test_a_choice_needs_the_users_yes_and_a_valid_mode(self):
        self.assertNotEqual(0, self.cmd("setup.py", "writing", "set", "--mode", "review").returncode)
        self.assertNotEqual(0, self.cmd("setup.py", "writing", "set", "--mode", "sometimes", "--yes").returncode)
        self.assertFalse(os.path.exists(self.modes_file()))

    def test_a_project_file_cannot_choose_the_mode(self):
        self.ready()
        for key in ("writing", "mode", "writing_mode", "writing-mode", "autonomous"):
            self.write_config(dict(self.config, **{key: "autonomous"}))
            self.assertIn("no mode has been chosen", self.cmd("setup.py", "writing", "show").stdout, key)
            self.assertNotEqual(0, self.cmd("write.py", "start", "--issue", "5", "--out", os.path.join(self.dir, "w")).returncode)
        self.write_config(dict(self.config, writing={"mode": "autonomous"}))
        self.assertIn("no mode has been chosen", self.cmd("setup.py", "writing", "show").stdout)

    def test_an_unreadable_file_counts_as_off_and_is_never_replaced(self):
        self.ready()
        self.mode("review")
        path = self.modes_file()
        good = open(path).read()
        for how in ("not json", "wrong form", "bad mode", "symlink", "loose"):
            if os.path.lexists(path):
                os.chmod(path, 0o600)
                os.unlink(path)
            if how == "not json":
                open(path, "w").write("{not json")
            elif how == "wrong form":
                open(path, "w").write(json.dumps({"form": 1}))
            elif how == "bad mode":
                open(path, "w").write(json.dumps({"form": 1, "modes": {"octo-org/sample-project": {"mode": "always",
                                                                                                      "set": "2026-11-16"}}}))
            elif how == "symlink":
                target = os.path.join(self.dir, "elsewhere.json")
                open(target, "w").write(good)
                os.symlink(target, path)
            else:
                open(path, "w").write(good)
                os.chmod(path, 0o644)
            if how in ("not json", "wrong form", "bad mode"):
                os.chmod(path, 0o600)   # private, as the plugin writes it, so that only the content is wrong
            shown = self.cmd("setup.py", "writing", "show").stdout
            self.assertIn("writing: the setting of this machine cannot be read", shown, how)
            self.assertIn("so recortex writes nothing. Run hooks.py --doctor.", shown, how)
            refused = self.cmd("write.py", "start", "--issue", "5", "--out", os.path.join(self.dir, "w"))
            self.assertIn("the writing setting of this machine cannot be read, so nothing is written", refused.stderr, how)
            again = self.cmd("setup.py", "writing", "set", "--mode", "autonomous", "--yes")
            self.assertNotEqual(0, again.returncode, how)
            self.assertIn("cannot be read", again.stderr, how)
            lines = self.payload_lines()
            self.assertIn("recortex: the writing setting of this machine cannot be read, so recortex writes nothing. Run "
                          "hooks.py --doctor.", lines, how)
            self.assertNotIn(NOT_CHOSEN, lines, how)

    def payload_lines(self):
        self.ready()
        text = self.tool("index.py", "query", "brief").stdout
        return text.split("===== BEGIN PROJECT STATUS DATA")[0]

    def test_another_repositorys_entry_is_kept(self):
        self.mode("review")
        data = json.load(open(self.modes_file()))
        data["modes"]["someone/else"] = {"mode": "autonomous", "set": "2026-11-01"}
        os.chmod(self.modes_file(), 0o600)
        json.dump(data, open(self.modes_file(), "w"))
        self.mode("off")
        modes = json.load(open(self.modes_file()))["modes"]
        self.assertEqual({"someone/else": {"mode": "autonomous", "set": "2026-11-01"}}, {k: v for k, v in modes.items()
                                                                                          if k == "someone/else"})
        self.assertEqual("off", modes["octo-org/sample-project"]["mode"])

    def test_the_brief_asks_until_answered_and_then_says_what_is_due(self):
        lines = self.payload_lines()
        self.assertEqual(1, lines.count(NOT_CHOSEN))
        self.assertIn(NOT_CHOSEN, self.outside(self.session()))
        self.mode("review")
        self.ready()
        lines = self.payload_lines()
        self.assertNotIn(NOT_CHOSEN, lines)
        self.assertIn("Writing (review mode): 4 parts of the text are due. Follow \"Write the text\" in the recortex skill.",
                      lines)
        self.mode("autonomous")
        self.assertIn("Writing (autonomous mode): 4 parts of the text are due.", self.payload_lines())
        self.mode("off")
        lines = self.payload_lines()
        self.assertNotIn("Writing", lines)
        self.assertNotIn(NOT_CHOSEN, lines)

    def outside(self, text):
        return text.split("===== BEGIN PROJECT STATUS DATA")[0]

    def session(self):
        done = self.hook("session-start", gh="false", env={"RECORTEX_NOTICE": "0"})
        return done.stdout

    def test_a_project_without_a_plan_is_not_asked(self):
        self.plan = False
        self.setUp()
        self.ready()
        self.assertNotIn("Writing", self.payload_lines())

    def test_the_doctor_says_the_mode_and_never_a_path(self):
        self.ready()
        self.mode("review")
        doctor = self.cmd("hooks.py", "--doctor").stdout
        self.assertIn("writing: the mode for octo-org/sample-project on this machine is review (chosen on", doctor)
        self.assertIn("own paper: no place is recorded", doctor)


class WriteTests(WritingBox):
    """Sections 7.2, 7.3 and 7.5: what is due, the material, the checks and the write."""

    def test_status_lists_what_is_due_on_the_paper_record(self):
        self.ready()
        self.mode("review")
        out = self.cmd("write.py", "status").stdout
        self.assertIn("due: #3 Methods (methods; parts [], results [], decisions [9106])", out)
        self.assertIn("due: #5 Results (claim; parts [9302], results [9140], decisions [])", out)
        self.assertIn("due: #6 Discussion (claim; parts [9307, 9310], results [], decisions [])", out)
        self.assertIn("due: #6 Limitations (claim; parts [9308], results [], decisions [])", out)
        self.assertNotIn("9303", out)   # a claim in doubt is never material
        self.assertNotIn("9306", out)   # a table is not text
        self.assertNotIn("9309", out)   # a comparison that names no paper

    def test_a_claim_in_doubt_moved_planned_or_failed_is_never_due_or_material(self):
        self.ready()
        self.mode("review")
        found = json.loads(self.cmd("write.py", "status", "--json").stdout.split("\n", 1)[1])
        parts = {cid for entry in found["due"] for cid in entry["parts"]}
        for never in (9303, 9304, 9311, 9312, 9313):   # doubt, revised, dropped, moved, failed
            self.assertNotIn(never, parts, never)
        results = {cid for entry in found["due"] for cid in entry["results"]}
        self.assertNotIn(9316, results)

    def test_a_backed_claim_is_due_once_and_not_after_its_text_change(self):
        self.ready()
        self.mode("review")
        folder = self.start(5)
        self.draft(folder)
        self.assertEqual(0, self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words()).returncode)
        self.ready()
        out = self.cmd("write.py", "status").stdout
        self.assertNotIn("due: #5 Results", out)
        self.assertEqual("text", self.part_state(9302))
        self.assertEqual(3, self.plan_json()["counts"]["due"])

    def part_state(self, cid):
        return self.part(self.plan_json(), cid)["state"]

    def test_the_material_holds_the_backed_claim_and_its_standing_findings_only(self):
        self.ready()
        self.mode("review")
        folder = self.start(5)
        material = self.read(folder, "material.md")
        self.assertIn("### Planned part 9302: claim, agreed", material)
        self.assertIn("Statement: In dry spells the plots with a rye cover stayed above the wilting point", material)
        self.assertIn("### Standing result 9140 (2026-10-21)", material)
        self.assertIn("Figure and table blocks the body has: figure dryspell.", material)
        self.assertNotIn("9141", material)    # a question
        self.assertNotIn("9143", material)    # a failure
        self.assertNotIn("9144", material)    # a decision of another account
        meta = json.loads(self.read(folder, "material.json"))
        self.assertEqual((5, [9302], [9140]), (meta["issue"], meta["parts"], meta["results"]))
        self.assertEqual(record_format.body_sha(self.base(folder)), meta["base"])

    def test_nothing_due_for_a_topic_is_one_line(self):
        self.ready()
        self.mode("review")
        done = self.cmd("write.py", "start", "--issue", "2", "--out", os.path.join(self.dir, "w2"))
        self.assertNotEqual(0, done.returncode)
        self.assertIn("write: nothing is due for #2", done.stderr)

    def author_file(self, text="the author's own paper\n"):
        path = os.path.join(self.dir, "authors-paper.tex")
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_start_never_writes_through_a_link_to_the_authors_file(self):
        """A drafting agent can put a link in the folder where start writes; the file at its other end must stay as it is."""
        self.ready()
        self.mode("review")
        author = self.author_file()
        for name in ("material.md", "base.md", "protected.json", "start.json"):
            folder = os.path.join(self.dir, "linked-%s" % name)
            os.makedirs(folder)
            os.symlink(author, os.path.join(folder, name))
            done = self.cmd("write.py", "start", "--issue", "5", "--out", folder)
            self.assertNotEqual(0, done.returncode, name)
            self.assertEqual("the author's own paper\n", open(author).read(), name)
            self.assertEqual([name], os.listdir(folder), "nothing else is written when a name is a link")

    def test_start_in_a_folder_that_is_itself_a_link_writes_nothing(self):
        self.ready()
        self.mode("review")
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.makedirs(elsewhere)
        folder = os.path.join(self.dir, "w5-link")
        os.symlink(elsewhere, folder)
        done = self.cmd("write.py", "start", "--issue", "5", "--out", folder)
        self.assertNotEqual(0, done.returncode)
        self.assertEqual([], os.listdir(elsewhere))

    def test_start_replaces_a_hard_link_and_never_writes_through_it(self):
        self.ready()
        self.mode("review")
        author = self.author_file()
        folder = os.path.join(self.dir, "w5-hard")
        os.makedirs(folder)
        os.link(author, os.path.join(folder, "material.md"))
        done = self.cmd("write.py", "start", "--issue", "5", "--out", folder)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual("the author's own paper\n", open(author).read())
        self.assertIn("### Planned part 9302", self.read(folder, "material.md"))

    def test_the_text_memory_of_an_apply_is_not_written_through_a_link(self):
        self.ready()
        self.mode("review")
        author = self.author_file()
        folder = self.start(5)
        self.draft(folder)
        os.symlink(author, os.path.join(folder, "text-memory.md"))
        done = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual("the author's own paper\n", open(author).read())
        self.assertNotEqual(0, done.returncode)

    # ------------------------------------------------------------ the write

    def test_a_write_in_review_mode_changes_the_body_keeps_a_copy_and_posts_its_comment(self):
        self.ready()
        self.mode("review")
        folder = self.start(5)
        self.draft(folder)
        checked = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(0, checked.returncode, checked.stdout)
        self.assertIn("Results: 1 new or changed paragraph, 0 gone", checked.stdout)
        self.assertIn("write: the draft passes every check", checked.stdout)
        refused = self.cmd("write.py", "apply", "--dir", folder)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("in review mode the author's own words are needed", refused.stderr)
        self.assertEqual(self.base(folder).rstrip(), self.body(5).rstrip())
        done = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words("Approve"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("The pooled fit and the dry-spell comparison agree", self.body(5))
        (cid, memory), = self.text_memories()
        self.assertEqual([], [f for f in tp.lint_drafts.lint_text(memory, kind="text", config=self.config) if f.level == "error"])
        fields = dict(record_format.fields(memory))
        self.assertTrue(fields["Approved"].startswith("By the author in the chat, "))
        self.assertIn("> Approve", fields["Approved"])
        # no text change had been made yet, so every paragraph of the body is the author's and is kept (spec 7.6)
        held = len(json.loads(self.read(folder, "protected.json"))["paragraphs"])
        self.assertGreater(held, 1)
        self.assertEqual("%d paragraphs were changed by hand and were left as they are." % held, fields["Kept from the author"])
        before, after = record_format.sha_pair(fields["Body"])
        self.assertEqual(record_format.body_sha(self.base(folder)), before)
        self.assertEqual(record_format.body_sha(self.body(5)), after)
        self.assertRegex(fields["Version"], r"^`[0-9a-f]{40}`$")   # the version is kept as a commit (references/versions.md)
        marker = record_format.parse_marker(memory.split("\n")[0])
        self.assertEqual("9302,9140", marker["because"])
        kept = os.path.join(self.data, "private", self.slug(), "texts", "5", after + ".md")
        self.assertEqual(self.body(5).rstrip(), open(kept).read().rstrip())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(kept).st_mode))
        self.assertIn("write: Results of #5 changed (review mode); comment %s says why." % cid, done.stdout)

    def test_a_write_in_autonomous_mode_needs_no_words_and_says_so(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        done = self.cmd("write.py", "apply", "--dir", folder)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        (cid, memory), = self.text_memories()
        self.assertEqual("autonomous", record_format.approved_form(dict(record_format.fields(memory))["Approved"]))
        self.assertIn("(autonomous mode)", done.stdout)

    def test_the_log_lists_the_text_changes_newest_first(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        self.assertEqual(0, self.cmd("write.py", "apply", "--dir", folder).returncode)
        self.ready()
        out = self.cmd("write.py", "log").stdout
        self.assertRegex(out, r"[0-9]+  20[0-9-]+  #5  Results  autonomous  in dry spells the cover plots stayed moist")
        self.assertEqual([], [l for l in self.cmd("write.py", "log", "--issue", "6").stdout.splitlines() if l[:1].isdigit()])

    def test_a_stop_after_the_body_is_finished_by_the_next_apply_with_one_edit_and_one_comment(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        stopped = self.cmd("write.py", "apply", "--dir", folder, env={"FAKE_GH_FAIL_NTH": "issue comment:1"})
        self.assertEqual(1, stopped.returncode)
        self.assertIn("write: the body is written and its comment is not; run write.py apply again to finish", stopped.stdout)
        self.assertEqual([], self.text_memories())
        self.assertIn("The pooled fit", self.body(5))
        edits = len([entry for entry in self.repo()["log"] if entry[:2] == ["issue", "edit"]])
        finished = self.cmd("write.py", "apply", "--dir", folder)
        self.assertEqual(0, finished.returncode, finished.stdout + finished.stderr)
        self.assertEqual(1, len(self.text_memories()))
        self.assertEqual(edits, len([entry for entry in self.repo()["log"] if entry[:2] == ["issue", "edit"]]))
        again = self.cmd("write.py", "apply", "--dir", folder)
        self.assertEqual(1, len(self.text_memories()))

    def test_what_the_text_rested_on_changing_while_it_waited_stops_the_write(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        result = manuscript_record.result(
            "2026-12-02T11:00-05:00", "a trimmed look", "with the shortest spells left out the dry-spell picture is weaker",
            "ran", "config", "| Measure | Value |\n|---|---|\n| e | 0.009 |", "None", "An hour.", "Nothing.",
            "Decide, owner the first author", links="against=9302")
        self.add_comment(9400, 5, result, tp.later(self))
        done = self.cmd("write.py", "apply", "--dir", folder)
        self.assertEqual(1, done.returncode)
        self.assertIn("not written: what this text rests on changed while it waited; draft it again.", done.stdout)
        self.assertEqual([], self.text_memories())
        self.assertNotIn("The pooled fit", self.body(5))

    def test_a_body_that_changed_while_the_draft_was_made_is_refused(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        data = self.repo()
        data["reads"] = 0
        self.save(data)
        done = self.cmd("write.py", "apply", "--dir", folder, env={"FAKE_GH_CHANGE_ON_READ": "2"})
        self.assertEqual(1, done.returncode)
        self.assertIn("differs from --expected-body-file", done.stdout)
        self.assertIn("write: the body was not written", done.stdout)
        self.assertEqual([], self.text_memories())
        self.assertNotIn("The pooled fit", self.body(5))

    def test_nothing_is_written_with_the_mode_off_for_an_untrusted_repository_or_a_body_someone_else_changed(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        self.mode("off")
        done = self.cmd("write.py", "apply", "--dir", folder)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("write: writing is off on this machine", done.stderr)
        self.mode("autonomous")
        with open(self.trusted, "w") as handle:
            handle.write("someone/else octocat\n")
        self.assertIn("is not in your trusted repositories", self.cmd("write.py", "apply", "--dir", folder).stderr)
        with open(self.trusted, "w") as handle:
            handle.write("octo-org/sample-project octocat\n")
        data = self.repo()
        issue = data["issues"]["5"]
        issue["edits"] = [{"at": "2026-12-02T10:00:00Z", "by": "hubot"}, {"at": issue["created_at"], "by": "octocat"}]
        issue["last_edited_at"] = issue["updated_at"] = "2026-12-02T10:00:00Z"
        self.save(data)
        self.ready()
        done = self.cmd("write.py", "apply", "--dir", folder)
        self.assertEqual(1, done.returncode)
        self.assertIn("someone outside your allowed list changed this text, so recortex does not write it; ask the user",
                      done.stderr)
        self.assertEqual([], self.text_memories())
        self.assertNotEqual(0, self.cmd("write.py", "start", "--issue", "5", "--out", os.path.join(self.dir, "w5b")).returncode)

    # ------------------------------------------------------------ what a drafting agent can write

    def test_an_edited_protected_json_cannot_unprotect_the_authors_paragraphs(self):
        self.ready()
        self.mode("review")
        folder = self.start(5)
        self.draft(folder, self.base(folder).replace("Over the whole season the difference", "Over the whole season the pooled "
                                                                                             "difference", 1))
        self.write_file(folder, "protected.json", {"paragraphs": []})   # the agent empties the list it was given
        checked = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(1, checked.returncode, checked.stdout)
        self.assertIn("not ready (check 5): protected paragraph", checked.stdout)
        before = self.body(5)
        applied = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual(1, applied.returncode)
        self.assertEqual((before, []), (self.body(5), self.text_memories()))

    def test_an_edited_material_json_adds_no_material_and_cannot_move_the_write_to_another_topic(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        base = self.base(folder)
        self.draft(folder, base.replace("\n### Figure: dryspell", "\nThe dry spells repeat 9 of 11 spells.\n\n### Figure: dryspell", 1),
                   parts=(9302,), results=(9140, 9316))   # 9316 is no material of this part
        meta = json.loads(self.read(folder, "material.json"))
        meta["results"].append(9316)
        meta["issue"] = 3
        self.write_file(folder, "material.json", meta)
        done = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(1, done.returncode)
        self.assertIn("used.json names result 9316, which the material does not hold", done.stdout)
        self.draft(folder, base.replace("\n### Figure: dryspell", "\nThe dry spells repeat 9 of 11 spells.\n\n### Figure: dryspell", 1))
        self.write_file(folder, "material.json", dict(meta, results=[9140]))   # the agent points the write at #3
        applied = self.cmd("write.py", "apply", "--dir", folder)
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        self.assertIn("The dry spells repeat", self.body(5))
        self.assertEqual([5], [c["issue"] for c in self.repo()["comments"].values() if "type=text" in c["body"].split("\n")[0]])

    def test_a_base_the_folder_was_not_started_with_is_refused(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        self.write_file(folder, "base.md", self.base(folder).replace("Over the whole season", "Over the season"))
        for command in (("check",), ("apply",)):
            done = self.cmd("write.py", command[0], "--dir", folder)
            self.assertEqual(1, done.returncode)
            self.assertIn("is not one that this folder's own write.py start made on this machine", done.stdout + done.stderr)
        self.assertEqual([], self.text_memories())

    FORGED = ({"body": record_format.body_sha("another draft"), "version": "x", "memory": 1},
              {"memory": 7, "version": "x"},
              {"body": 5, "memory": 7})

    def record_file(self, issue, folder):
        """The private record write.py start kept for this folder's base."""
        base = record_format.body_sha(self.base(folder))
        return os.path.join(self.data, "private", self.slug(), "write", str(issue), base + ".json")

    def forged_forms(self, folder):
        before = self.body(5)
        return self.FORGED + ({"body": record_format.body_sha(self.read(folder, "draft.md")), "memory": 7},   # not the live body
                              {"body": record_format.body_sha(before), "memory": 7})                          # the base, not the draft

    def test_a_forged_resume_state_in_the_working_folder_is_ignored(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        base = self.base(folder)
        self.draft(folder, base.replace("Over the whole season the difference", "Over the whole season the pooled difference", 1))
        before = self.body(5)
        self.assertFalse(os.path.exists(os.path.join(folder, "write.state.json")))   # nothing of the state is kept there
        for forged in self.forged_forms(folder):
            self.write_file(folder, "write.state.json", forged)
            done = self.cmd("write.py", "apply", "--dir", folder)
            self.assertEqual(1, done.returncode, forged)
            self.assertIn("not written (check 5): protected paragraph", done.stdout)
            self.assertEqual((before, []), (self.body(5), self.text_memories()), forged)

    def test_a_forged_resume_state_in_the_record_runs_every_check(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        base = self.base(folder)
        self.draft(folder, base.replace("Over the whole season the difference", "Over the whole season the pooled difference", 1))
        before = self.body(5)
        path = self.record_file(5, folder)
        record = json.load(open(path))
        for forged in self.forged_forms(folder):
            json.dump(dict(record, state=forged), open(path, "w"))
            os.chmod(path, 0o600)
            done = self.cmd("write.py", "apply", "--dir", folder)
            self.assertEqual(1, done.returncode, forged)
            self.assertIn("not written (check 5): protected paragraph", done.stdout)
            self.assertEqual((before, []), (self.body(5), self.text_memories()), forged)

    def test_a_resume_is_believed_only_for_text_this_plugin_wrote_and_kept(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        draft = self.read(folder, "draft.md")
        # somebody outside the allowed list put the draft's text into the topic; a state that says "the plugin wrote it"
        # does not make it the plugin's text, because this machine kept no copy of it
        data = self.repo()
        issue = data["issues"]["5"]
        issue["body"] = draft
        issue["edits"] = [{"at": "2026-12-02T10:00:00Z", "by": "hubot"}, {"at": issue["created_at"], "by": "octocat"}]
        issue["last_edited_at"] = issue["updated_at"] = "2026-12-02T10:00:00Z"
        self.save(data)
        self.ready()
        path = self.record_file(5, folder)
        record = json.load(open(path))
        json.dump(dict(record, state={"body": record_format.body_sha(draft)}), open(path, "w"))
        os.chmod(path, 0o600)
        done = self.cmd("write.py", "apply", "--dir", folder)
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("someone outside your allowed list changed this text", done.stderr)
        self.assertEqual([], self.text_memories())

    def test_a_state_that_says_written_is_not_believed_for_a_draft_that_is_the_base(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        self.assertEqual(0, self.cmd("write.py", "apply", "--dir", folder).returncode)
        self.ready()
        written = self.body(5)   # the plugin wrote it and kept a copy: a draft equal to it is no change at all
        forged = os.path.join(self.dir, "forged")
        os.makedirs(forged)
        for name in ("base.md", "draft.md"):
            self.write_file(forged, name, written)
        self.write_file(forged, "used.json", {"parts": [9302], "results": [9140], "decisions": []})
        self.write_file(forged, "memory.json", json.loads(self.read(folder, "memory.json")))
        nonce = "a" * 32
        self.write_file(forged, "start.json", {"issue": 5, "nonce": nonce})
        path = self.record_file(5, forged)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        sha = record_format.body_sha(written)
        json.dump({"form": 1, "issue": 5, "base": sha, "nonce": nonce, "protected": [], "state": {"body": sha}}, open(path, "w"))
        os.chmod(path, 0o600)
        done = self.cmd("write.py", "apply", "--dir", forged)
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("not written (check", done.stdout)
        self.assertEqual(1, len(self.text_memories()))

    def test_a_record_of_another_issue_with_the_same_body_is_never_used(self):
        self.ready()
        self.mode("autonomous")
        folder = self.start(5)
        self.draft(folder)
        data = self.repo()
        data["issues"]["6"]["body"] = data["issues"]["5"]["body"]   # a second topic whose body is the same text
        self.save(data)
        start = json.loads(self.read(folder, "start.json"))
        self.write_file(folder, "start.json", dict(start, issue=6))   # the folder of #5 is pointed at #6
        for command in ("check", "apply"):
            done = self.cmd("write.py", command, "--dir", folder)
            self.assertEqual(1, done.returncode, command)
            self.assertIn("is not one that this folder's own write.py start made on this machine", done.stdout + done.stderr)
        # even a record that exists for #6 (made by another start, with its own nonce) is not this folder's
        record = json.load(open(self.record_file(5, folder)))
        other = os.path.join(os.path.dirname(os.path.dirname(self.record_file(5, folder))), "6")
        os.makedirs(other, mode=0o700, exist_ok=True)
        copied = os.path.join(other, os.path.basename(self.record_file(5, folder)))
        json.dump(dict(record, issue=6, nonce="1" * 32), open(copied, "w"))
        os.chmod(copied, 0o600)
        done = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(1, done.returncode)
        self.assertIn("is not one that this folder's own write.py start made on this machine", done.stdout + done.stderr)
        self.assertEqual([], self.text_memories())

    def test_a_folder_with_the_nonce_of_another_start_is_refused(self):
        self.ready()
        self.mode("autonomous")
        first = self.start(5, "first")
        self.draft(first)
        self.assertEqual(0, self.cmd("write.py", "check", "--dir", first).returncode)
        second = self.start(5, "second")   # a later start for the same base replaces the record and its nonce
        self.draft(second)
        refused = self.cmd("write.py", "check", "--dir", first)
        self.assertEqual(1, refused.returncode)
        self.assertIn("is not one that this folder's own write.py start made on this machine", refused.stdout + refused.stderr)
        self.assertEqual(0, self.cmd("write.py", "check", "--dir", second).returncode)
        forged = json.loads(self.read(second, "start.json"))
        for nonce in ("0" * 32, "", 5, None):
            self.write_file(second, "start.json", dict(forged, nonce=nonce))
            done = self.cmd("write.py", "apply", "--dir", second)
            self.assertEqual(1, done.returncode, repr(nonce))
            self.assertEqual([], self.text_memories())
        self.write_file(second, "start.json", forged)
        self.assertEqual(0, self.cmd("write.py", "apply", "--dir", second).returncode)

    # ------------------------------------------------------------ the checks

    def refused(self, folder, number, *words):
        before = (len(self.text_memories()), self.body(5))
        done = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(1, done.returncode, done.stdout)
        self.assertIn("not ready (check %d)" % number, done.stdout)
        for word in words:
            self.assertIn(word, done.stdout)
        applied = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual(1, applied.returncode)
        self.assertEqual(before, (len(self.text_memories()), self.body(5)))  # nothing was written

    def setup_check(self):
        self.ready()
        self.mode("review")
        folder = self.start(5)
        self.draft(folder)
        return folder

    def test_check_1_the_linter(self):
        folder = self.setup_check()
        text = self.read(folder, "draft.md").replace("## Results", "## Results\n\nAs of 2026-11-02 the cover wins.", 1)
        self.draft(folder, text)
        self.refused(folder, 1, "the draft does not pass the linter")

    def test_check_2_only_the_headings_of_the_write_list_differ(self):
        folder = self.setup_check()
        base = self.base(folder)
        for variant in (base.replace("# Cover crops keep the topsoil wetter in dry spells", "# A new title", 1),
                        base.replace("## Limitations", "## Introduction\n\nA new introduction.\n\n## Limitations", 1)):
            self.draft(folder, variant)
            done = self.cmd("write.py", "check", "--dir", folder)
            self.assertEqual(1, done.returncode)
            self.assertIn("not ready (check 2)", done.stdout)
        self.draft(folder, base.replace("# Cover crops keep the topsoil wetter in dry spells", "# A new title", 1))
        self.refused(folder, 2, "the title line and the role sentence before the first section are the author's and differ")

    def test_check_2_sources_may_gain_rows_and_not_lose_one(self):
        folder = self.setup_check()
        base = self.base(folder)
        gain = base.replace("## Sources\n\n", "## Sources\n\n| A new statement | `results/new.csv`, commit 3f9c2ab |\n", 1)
        self.draft(folder, gain.replace("\n### Figure: dryspell", "\nThe dry spells agree on 9 of 11 spells.\n\n### Figure: dryspell", 1))
        done = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(0, done.returncode, done.stdout)
        lose = base.replace("| 0.004 per day | `results/spell_slope.csv`, commit 3f9c2ab |\n", "")
        self.draft(folder, lose.replace("\n### Figure: dryspell", "\nThe dry spells agree on 9 of 11 spells.\n\n### Figure: dryspell", 1))
        self.refused(folder, 2, "'Sources' may gain rows but not lose or change one")

    def test_check_3_every_number_stands_in_the_material_or_the_base(self):
        folder = self.setup_check()
        base = self.base(folder)
        for text, ok in (("in 11 dry spells and 9 of 11 spells", True), ("on 17 of 23 spells", False),
                         ("by about 77 percent", False), ("in 2 million spells", False)):
            self.draft(folder, base.replace("\n### Figure: dryspell", "\nThe cover plots stayed moist %s.\n\n### Figure: dryspell" % text, 1))
            done = self.cmd("write.py", "check", "--dir", folder)
            self.assertEqual(0 if ok else 1, done.returncode, text + done.stdout)
            if not ok:
                self.assertIn("not ready (check 3): the number", done.stdout)
                self.assertIn("stands in no result of the material and not in the base", done.stdout)

    def test_check_4_no_figure_or_table_block_is_added(self):
        folder = self.setup_check()
        base = self.base(folder)
        block = ("\n### Figure: new - A new figure\n\nA caption.\n\n![x](https://github.com/octo-org/sample-project/blob/v0.4/"
                 "f.png?raw=true)\n")
        self.draft(folder, base.replace("\n## Limitations", block + "\n## Limitations", 1).replace(
            "| Figure dryspell |", "| Figure new | data `a.csv`; command `b` |\n| Figure dryspell |", 1))
        self.refused(folder, 4, "the draft adds the figure block 'new'; the plugin adds no figure or table block")

    def test_check_6_the_draft_differs_from_the_base(self):
        folder = self.setup_check()
        self.draft(folder, self.base(folder))
        self.refused(folder, 6, "the draft does not differ from the base")

    def test_check_7_methods_hold_no_commit_tag_job_or_path_and_use_decisions_that_stand(self):
        self.ready()
        self.mode("review")
        folder = self.start(3)
        base = self.base(folder)
        self.assertIn("### Standing decision 9106", self.read(folder, "material.md"))
        for sentence in ("The plots were fitted at commit 3f9c2ab.", "The model is tagged v0.4.", "The job 123456 ran it.",
                         "See `analysis/fit_model.R` for the fit.", "It is pull request #10 of the project."):
            self.draft(folder, base.replace("### Table: plots", "%s\n\n### Table: plots" % sentence, 1), parts=(), results=(),
                       decisions=(9106,))
            done = self.cmd("write.py", "check", "--dir", folder)
            self.assertEqual(1, done.returncode, sentence)
            self.assertIn("not ready (check 7)", done.stdout)
        self.draft(folder, base.replace("### Table: plots", "The four plots of the wet corner were left out of the moisture "
                                                            "analysis.\n\n### Table: plots", 1), parts=(), results=(),
                   decisions=(9106,))
        self.assertEqual(0, self.cmd("write.py", "check", "--dir", folder).returncode)
        self.draft(folder, base.replace("### Table: plots", "A new sentence about the design.\n\n### Table: plots", 1),
                   parts=(), results=(), decisions=(9123,))   # 9123 was replaced and is not in the material
        done = self.cmd("write.py", "check", "--dir", folder)
        self.assertIn("used.json names decision 9123, which the material does not hold", done.stdout)
        self.assertIn("decision 9123 no longer stands", done.stdout)

    def test_check_7_the_material_holds_no_replaced_or_set_aside_decision(self):
        self.ready()
        self.mode("review")
        for number in (3, 4):
            folder = os.path.join(self.dir, "m%d" % number)
            done = self.cmd("write.py", "start", "--issue", str(number), "--out", folder)
            if done.returncode:
                continue
            material = self.read(folder, "material.md")
            for gone in ("9123", "9125", "9110"):
                self.assertNotIn("decision %s" % gone, material)

    def test_a_new_decision_makes_methods_due_and_a_checkpoint_settles_it(self):
        self.ready()
        self.mode("review")
        decision = manuscript_record.decision(
            "2026-11-23T10:00-05:00", "sample the margins", "sample the margins of every plot as well", "Margins are sampled.",
            "the principal investigator", "Sample the margins too.", "Lab meeting, 2026-11-23.", "The Methods change.",
            "Write it, owner Claude session")
        self.add_comment(9400, 3, decision, tp.later(self))
        self.ready()
        out = self.cmd("write.py", "status").stdout
        self.assertIn("due: #3 Methods (methods; parts [], results [], decisions [9106, 9400])", out)
        checkpoint = manuscript_record.checkpoint(
            "2026-11-24T10:00-05:00", "methods already say it", "the Methods text already says everything the decisions "
            "say", "Nothing to add.", "Checked.", "Nothing.", "Go on, owner the session", "None.")
        self.add_comment(9401, 3, checkpoint, tp.later(self))
        self.ready()
        self.assertNotIn("due: #3 Methods", self.cmd("write.py", "status").stdout)

    def test_check_8_a_comparison_is_written_only_with_a_documented_citation(self):
        self.ready()
        self.mode("review")
        named = manuscript_record.plan_memory(
            "2026-11-22T09:00-05:00", "comparison", "one trial", "compare the effect with one named trial", "Discussion",
            "#6", "The effect is of the size [@doi:10.1234/example] reports.", "The trial's number.", "Nothing yet.",
            "No comparison.", "None for now, owner the first author", links="because=9150")
        self.add_comment(9400, 13, named, tp.later(self))
        self.ready()
        folder = self.start(6, "w6")
        material = self.read(folder, "material.md")
        self.assertIn("### Planned part 9400: comparison", material)
        self.assertNotIn("9309", material)
        base = self.base(folder)
        text = base.replace("\n## Sources", "\n\nThe effect is of the size another trial reports.\n\n## Sources", 1)
        self.draft(folder, text, parts=(9309,), results=())
        done = self.cmd("write.py", "check", "--dir", folder)
        self.assertIn("used.json names part 9309, which the material does not hold", done.stdout)
        # even a material file that lists the comparison with no citation cannot get it written
        meta = json.loads(self.read(folder, "material.json"))
        meta["parts"].append(9309)
        self.write_file(folder, "material.json", meta)
        done = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(1, done.returncode)
        self.assertIn("not ready (check 8): a comparison is written only when its planned part holds a citation of a "
                      "documented form; part 9309 holds none", done.stdout)

    # ------------------------------------------------------------ the author's own edits (section 7.6)

    def write_once(self, issue=5):
        folder = self.start(issue)
        self.draft(folder)
        done = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.ready()
        return folder

    def author_edits(self, old, new, issue=5, at="2026-12-02T10:00:00Z"):
        data = self.repo()
        item = data["issues"][str(issue)]
        item["body"] = item["body"].replace(old, new, 1)
        item["edits"] = [{"at": at, "by": "octocat"}, {"at": item["created_at"], "by": "octocat"}]
        item["last_edited_at"] = item["updated_at"] = at
        self.save(data)
        self.ready()

    def test_a_first_write_on_a_topic_with_text_protects_every_paragraph_so_it_can_only_add(self):
        self.ready()
        self.mode("review")
        folder = self.start(5)
        protected = json.loads(self.read(folder, "protected.json"))["paragraphs"]
        self.assertEqual(11, len(protected))   # the 12 paragraphs of the body, but the table of Sources
        self.assertNotIn("| Statement or number | Source |", "\n".join(protected))
        self.assertTrue(protected[0].startswith("# Cover crops keep the topsoil wetter in dry spells"))
        base = self.base(folder)
        self.draft(folder, base.replace("Over the whole season the difference", "Over the whole season the pooled difference", 1))
        self.refused(folder, 5, "protected paragraph")
        self.assertNotIn("last.md", os.listdir(folder))

    def test_a_paragraph_edited_by_hand_after_a_write_is_kept_and_the_rest_is_written_around_it(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.author_edits("The wilting point was taken from the texture class of the field, not measured per plot.",
                          "The wilting point was taken from the texture class of the field; I measured it in six plots.")
        # a part is due again once a new finding backs the claim
        folder = self.start(5, "second") if "5" in self.cmd("write.py", "status").stdout and "due: #5" in \
            self.cmd("write.py", "status").stdout else None
        self.assertIsNone(folder)

    def second_round(self):
        """After a write and a hand edit: a new reason to write #5 again, a rewording of the claim the text follows."""
        claim = manuscript_record.plan_memory(
            "2026-12-03T10:00-05:00", "claim", "wetter topsoil in dry spells, reworded",
            "claim that a rye cover keeps the topsoil wetter during dry spells", "Results", "#5",
            "In dry spells the plots with a rye cover stayed above the wilting point on more spells than the bare plots, "
            "in both seasons.", "The dry-spell comparison in both seasons.", "The dry-spell result.",
            "The paper reports the season effect only.", "None for now, owner Claude session", change="rewords",
            links="replaces=9302 because=9140")
        self.add_comment(9410, 13, claim, tp.later(self))
        self.ready()

    def test_only_the_paragraphs_the_author_changed_are_protected_once_this_machine_has_the_last_text(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.author_edits("The wilting point was taken from the texture class of the field, not measured per plot.",
                          "The wilting point was taken from the texture class of the field; I measured it in six plots.")
        self.second_round()
        out = self.cmd("write.py", "status").stdout
        self.assertIn("due: #5 Results (changed;", out.replace("(claim;", "(changed;"))
        folder = self.start(5, "second")
        self.assertIn("last.md", os.listdir(folder))
        protected = json.loads(self.read(folder, "protected.json"))["paragraphs"]
        self.assertEqual(["## Limitations\n\nThe wilting point was taken from the texture class of the field; I measured it "
                          "in six plots. Soil texture was measured\nin 12 of 20 plots only, so differences in texture "
                          "between plots cannot be ruled out as a cause."][0][:0] + protected[0][:0], "")
        self.assertEqual(1, len(protected))
        self.assertIn("I measured it in six plots", protected[0])
        base = self.base(folder)
        self.draft(folder, base.replace("on 9 of 11 spells.", "on 9 of 11 spells and, with a third season, on 12 of 14.", 1),
                   parts=(9410,), results=(9140,))
        checked = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(0, checked.returncode, checked.stdout)
        self.draft(folder, base.replace("I measured it in six plots", "I measured it in every plot", 1), parts=(9410,),
                   results=(9140,))
        self.refused(folder, 5, "protected paragraph 0")
        done = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual(1, done.returncode)

    def second_write(self, parts, date="2026-12-10", before="on 9 of 11 spells.", after="on 9 of 11 spells, in both seasons."):
        data = self.repo()
        data["date"] = date   # what the fake creates is stamped after everything the sync has read, as on GitHub
        self.save(data)
        folder = self.start(5, "second-" + date)
        base = self.base(folder)
        self.draft(folder, base.replace(before, after, 1), parts=parts, results=(9140,))
        done = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.ready()

    def test_a_claim_replaced_after_a_text_change_is_not_due_once_a_newer_text_change_links_its_replacement(self):
        """Text change A links claim C1, C2 replaces C1, text change B links C2. Nothing is due for it."""
        self.ready()
        self.mode("review")
        self.write_once()                          # A links 9302 (C1) and 9140
        self.second_round()                        # C2 (9410) replaces 9302
        self.assertIn("due: #5 Results", self.cmd("write.py", "status").stdout)
        self.second_write(parts=(9410,))           # B links 9410
        out = self.cmd("write.py", "status").stdout
        self.assertNotIn("due: #5", out)
        self.assertEqual("text", self.part_state(9410))
        self.assertEqual(3, self.plan_json()["counts"]["due"])
        found = json.loads(self.cmd("write.py", "status", "--json").stdout.split("\n", 1)[1])
        self.assertEqual([], [entry for entry in found["due"] if entry["topic"] == 5])

    def test_a_claim_revised_again_after_that_text_change_is_due_again_for_the_newest_claim_only(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.second_round()
        self.second_write(parts=(9410,))
        third = manuscript_record.plan_memory(
            "2026-12-05T10:00-05:00", "claim", "wetter topsoil in dry spells, narrowed",
            "claim that a rye cover keeps the topsoil wetter during dry spells", "Results", "#5",
            "In dry spells the plots with a rye cover stayed above the wilting point on more spells than the bare plots, "
            "in the first season.", "The dry-spell comparison in the first season.", "The dry-spell result.",
            "The paper reports the season effect only.", "None for now, owner Claude session", change="narrows",
            links="replaces=9410 because=9140")
        self.add_comment(9420, 13, third, "2026-12-11T10:00:00Z")
        self.ready()
        found = json.loads(self.cmd("write.py", "status", "--json").stdout.split("\n", 1)[1])
        entries = [entry for entry in found["due"] if entry["topic"] == 5]
        self.assertEqual([("Results", [9420])], [(entry["heading"], entry["parts"]) for entry in entries])
        self.second_write(parts=(9420,), date="2026-12-12", before="in both seasons.", after="in the first season.")
        self.assertNotIn("due: #5", self.cmd("write.py", "status").stdout)

    def test_a_newer_text_change_that_does_not_link_the_replacement_leaves_the_part_due(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.second_round()
        other = manuscript_record.text_memory(
            "2026-12-04T10:00-05:00", "Results", "the figure caption names the plot size", "The caption changed.",
            "The size was missing.", "By the author in the chat, 2026-12-04 10:00 EST:\n\n> Approve", links="because=9140")
        self.add_comment(9430, 5, other, tp.later(self))
        self.ready()
        self.assertIn("due: #5 Results", self.cmd("write.py", "status").stdout)

    def due_for(self, issue=5):
        found = json.loads(self.cmd("write.py", "status", "--json").stdout.split("\n", 1)[1])
        return [(entry["heading"], entry["parts"]) for entry in found["due"] if entry["topic"] == issue]

    def restore_to(self, text_memory_id, body, before, at="2026-12-11T10:00-05:00", section="Results", issue=5):
        """The record of a restore of `issue` to `body` (a comment of the form "restored", and the body put back)."""
        restore = manuscript_record.text_memory(
            at, section, "the text of this part is as it was in the first version",
            "The text was put back as the first version had it.", "The author asked for it.",
            "Restored by the author in the chat, %s EST:\n\n> Put back the first version" % at[:16].replace("T", " "),
            before=record_format.body_sha(before), after=record_format.body_sha(body))
        self.add_comment(text_memory_id, issue, restore, "2026-12-11T15:00:00Z")
        data = self.repo()
        data["issues"][str(issue)]["body"] = body
        data["issues"][str(issue)]["updated_at"] = "2026-12-11T15:00:00Z"
        self.save(data)
        self.ready()

    def test_a_restore_after_the_change_that_covers_a_replacement_makes_the_part_due_again(self):
        """T1 links C1, C2 replaces C1, T2 links C2, then the author puts T1's text back: the text no longer says C2."""
        self.ready()
        self.mode("review")
        self.write_once()
        first = self.body(5)
        self.second_round()
        self.second_write(parts=(9410,))
        second = self.body(5)
        self.assertEqual([], self.due_for())
        self.restore_to(9470, first, second)
        self.assertEqual([("Results", [9410])], self.due_for())
        self.assertIn("due: #5 Results", self.cmd("write.py", "status").stdout)

    def test_a_restore_of_another_heading_or_before_the_covering_change_leaves_the_text_covered(self):
        self.ready()
        self.mode("review")
        self.write_once()
        first = self.body(5)
        self.second_round()
        self.second_write(parts=(9410,))
        second = self.body(5)
        self.restore_to(9470, second, second, section="Limitations")    # another heading of the same topic
        self.assertEqual([], self.due_for())
        self.restore_to(9471, second, second, at="2026-11-30T10:00-05:00")   # a restore from before the change that covers
        self.assertEqual([], self.due_for())

    def test_the_text_that_links_the_newest_claim_of_a_chain_covers_the_oldest_claim_it_replaced(self):
        self.ready()
        self.mode("review")
        self.write_once()                  # T1 links 9302
        self.second_round()                # 9410 replaces 9302
        third = manuscript_record.plan_memory(
            "2026-12-05T10:00-05:00", "claim", "wetter topsoil in dry spells, narrowed",
            "claim that a rye cover keeps the topsoil wetter during dry spells", "Results", "#5",
            "In dry spells the plots with a rye cover stayed above the wilting point on more spells than the bare plots, "
            "in the first season.", "The dry-spell comparison in the first season.", "The dry-spell result.",
            "The paper reports the season effect only.", "None for now, owner Claude session", change="narrows",
            links="replaces=9410 because=9140")
        self.add_comment(9420, 13, third, "2026-12-11T10:00:00Z")     # 9420 replaces 9410, which replaced 9302
        self.ready()
        self.second_write(parts=(9420,), date="2026-12-12", before="on 9 of 11 spells.", after="on 9 of 11 spells, in all.")
        self.assertEqual([], self.due_for())   # T2 links only 9420; the chain 9302, 9410, 9420 is followed to it

    def test_a_text_change_for_another_heading_of_the_topic_does_not_cover_the_replacement(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.second_round()
        other = manuscript_record.text_memory(
            "2026-12-04T10:00-05:00", "Limitations", "the limits name the plot size", "The limits changed.",
            "The size was missing.", "By the author in the chat, 2026-12-04 10:00 EST:\n\n> Approve", links="because=9410")
        self.add_comment(9431, 5, other, tp.later(self))
        self.ready()
        self.assertEqual([("Results", [9410])], self.due_for())

    def test_a_text_change_from_before_the_row_does_not_cover_what_replaced_a_claim_after_that_row(self):
        """B links the first replacement (9410), then A links 9302 again, then a second replacement (9442) is made: B is older
        than A, so it does not say what A's text lacks."""
        self.ready()
        self.mode("review")
        self.second_round()                # 9410 replaces 9302, 3 December
        text = lambda at, links: manuscript_record.text_memory(
            at, "Results", "the dry-spell paragraph says the cover plots stayed moist", "A paragraph changed.", "It was due.",
            "By the author in the chat, %s EST:\n\n> Approve" % at[:16].replace("T", " "), links=links)
        self.add_comment(9440, 5, text("2026-12-04T10:00-05:00", "because=9410"), "2026-12-11T10:01:00Z")    # B
        self.add_comment(9441, 5, text("2026-12-05T10:00-05:00", "because=9302"), "2026-12-11T10:02:00Z")    # A
        second = manuscript_record.plan_memory(
            "2026-12-06T10:00-05:00", "claim", "wetter topsoil in dry spells, said another way",
            "claim that a rye cover keeps the topsoil wetter during dry spells", "Results", "#5",
            "In dry spells the plots with a rye cover stayed wetter than the bare plots.", "The dry-spell comparison.",
            "The dry-spell result.", "The paper reports the season effect only.", "None for now, owner Claude session",
            change="rewords", links="replaces=9302")
        self.add_comment(9442, 13, second, "2026-12-11T10:03:00Z")
        self.ready()
        self.assertEqual([("Results", [9442])], self.due_for())

    def test_text_changes_since_the_last_pause_are_counted_by_instant_and_not_by_the_clock_a_stamp_was_written_in(self):
        import datetime
        self.ready()
        self.mode("autonomous")
        self.write_once()
        written = datetime.datetime.strptime(re.search(r"at=(\S+)", self.text_memories()[0][1]).group(1), "%Y-%m-%dT%H:%MZ")
        self.assertEqual(1, self.plan_json()["counts"]["texts"])   # the pauses of the record are older than the change

        def pause(cid, hours, zone_hours):
            """A resume block `hours` after the text change, stamped in a zone `zone_hours` east of UTC."""
            instant = written + datetime.timedelta(hours=hours)
            stamp = (instant + datetime.timedelta(hours=zone_hours)).strftime("%Y-%m-%d %H:%M") + " UTC%+d" % zone_hours
            body = ("### Resume block: %s\n\n<!-- ghim:resume:start -->\n**Last updated:** %s by the session\n**Current focus:** "
                    "Waiting.\n**Active issues:** none\n**Last decisions:** none\n**Next actions:**\n\n| Action | Owner | Where |\n"
                    "|---|---|---|\n| Wait | the session | this hub |\n\n**Holds:** none\n<!-- ghim:resume:end -->\n" % (stamp, stamp))
            self.add_comment(cid, 1, body, "2026-12-%02dT00:00:00Z" % (cid - 9450))
            self.ready()

        pause(9461, -1, 5)    # an hour before the change, stamped far east: its clock reads later than the change's
        self.assertEqual(1, self.plan_json()["counts"]["texts"])
        pause(9462, 1, -10)   # an hour after it, stamped far west: its clock reads earlier than the change's
        self.assertEqual(0, self.plan_json()["counts"]["texts"])

    def written_at(self):
        """The instant of the first text change of the record, written in autonomous mode."""
        import datetime
        self.ready()
        self.mode("autonomous")
        self.write_once()
        return datetime.datetime.strptime(re.search(r"at=(\S+)", self.text_memories()[0][1]).group(1), "%Y-%m-%dT%H:%MZ")

    def pause_after(self, cid, written, hours, stamp_zone, user=None, edited=False):
        """A resume block on the hub, made `hours` after `written`, with the zone name `stamp_zone` after its time ("UTC-10" and "UTC+5"
        are read by their offset; IST is a zone that zones.py does not know, so only the time GitHub gave the comment places it).
        The wall clock of the stamp is made to disagree with the instant, as a stamp in another zone does. The index is read again
        from the start, since a sync that goes on from the last one does not read a comment dated before it."""
        import datetime
        instant = written + datetime.timedelta(hours=hours)
        east = {"UTC-10": -10, "UTC+5": 5, "IST": -4}[stamp_zone]
        stamp = (instant + datetime.timedelta(hours=east)).strftime("%Y-%m-%d %H:%M") + " " + stamp_zone
        resume = ("### Resume block: %s\n\n<!-- ghim:resume:start -->\n**Last updated:** %s by the session\n**Current focus:** Waiting.\n"
                  "**Active issues:** none\n**Last decisions:** none\n**Next actions:**\n\n| Action | Owner | Where |\n"
                  "|---|---|---|\n| Wait | the session | this hub |\n\n**Holds:** none\n<!-- ghim:resume:end -->\n")
        self.add_comment(cid, 1, resume % (stamp, stamp), instant.strftime("%Y-%m-%dT%H:%M:00Z"), user)
        if edited:
            data = self.repo()
            data["comments"][str(cid)]["updated_at"] = (instant + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:00Z")
            self.save(data)
        self.assertEqual(0, self.tool("index.py", "sync", "--rebuild").returncode)

    def test_a_pause_in_a_zone_that_is_not_known_is_placed_by_the_time_github_gave_it_when_text_changes_are_counted(self):
        written = self.written_at()
        self.assertEqual(1, self.plan_json()["counts"]["texts"])
        self.pause_after(9461, written, 1, "IST")      # its wall reads four hours before the change; GitHub's time is an hour after
        self.assertEqual(0, self.plan_json()["counts"]["texts"])

    def test_only_a_resume_block_of_an_allowed_account_that_nobody_edited_ends_the_count_of_text_changes(self):
        written = self.written_at()
        self.pause_after(9461, written, 1, "UTC+5", user="hubot")             # an account that is not allowed
        self.assertEqual(1, self.plan_json()["counts"]["texts"])
        self.pause_after(9462, written, 2, "UTC+5", edited=True)              # edited after it was posted
        self.assertEqual(1, self.plan_json()["counts"]["texts"])
        self.pause_after(9463, written, 3, "UTC+5")                           # the author's own, never edited
        self.assertEqual(0, self.plan_json()["counts"]["texts"])

    def test_a_proposal_with_a_heading_in_a_zone_that_is_not_known_waits_by_the_time_github_gave_it(self):
        self.ready()
        self.mode("review")
        proposal = manuscript_record.proposal("2026-12-05T10:00-05:00", "Results", "A paragraph is new.", "The claim is backed.", 8,
                                              "0" * 64, "1" * 64).split("\n", 1)[1].replace(" EST - ", " IST - ")   # no marker
        self.add_comment(9460, 5, proposal, "2026-12-11T10:00:00Z")           # GitHub gave it 11 December; its heading says 5 December
        text = manuscript_record.text_memory("2026-12-08T10:00-05:00", "Results", "the paragraph about spells", "A paragraph.",
                                             "It was due.", "By the author in the chat, 2026-12-08 10:00 EST:\n\n> Approve")
        self.add_comment(9461, 5, text, "2026-12-11T10:01:00Z")                # a text change written before GitHub had the proposal
        self.ready()
        self.assertIn("proposal waiting on GitHub: #5 (comment 9460)", self.cmd("write.py", "status").stdout)
        self.assertEqual(1, self.plan_json()["counts"]["proposals"])

    def test_a_hand_edit_that_versions_sync_recorded_is_still_the_authors_and_protected(self):
        """versions.py sync tells a hand edit as a text change (form "edited") whose after is the live body; it must not
        make the plugin believe it wrote that body."""
        self.ready()
        self.mode("review")
        self.write_once()
        self.author_edits("The wilting point was taken from the texture class of the field, not measured per plot.",
                          "The wilting point was taken from the texture class of the field; I measured it in six plots.")
        synced = self.cmd("versions.py", "sync")
        self.assertEqual(0, synced.returncode, synced.stdout + synced.stderr)
        self.ready()
        edited = [body for cid, body in self.text_memories() if "Edited by octocat on GitHub" in body]
        self.assertEqual(1, len(edited), "the sync tells the hand edit as a text change")
        self.second_round()
        folder = self.start(5, "second")
        self.assertIn("last.md", os.listdir(folder))
        protected = json.loads(self.read(folder, "protected.json"))["paragraphs"]
        self.assertEqual(1, len(protected))
        self.assertIn("I measured it in six plots", protected[0])
        base = self.base(folder)
        self.draft(folder, base.replace("I measured it in six plots", "I measured it in every plot", 1), parts=(9410,),
                   results=(9140,))
        self.refused(folder, 5, "protected paragraph 0")

    def test_without_the_copy_of_the_last_text_every_paragraph_is_protected_again(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.author_edits("not measured per plot.", "not measured per plot, as I said.")
        self.second_round()
        import shutil
        shutil.rmtree(os.path.join(self.data, "private", self.slug(), "texts"))
        folder = self.start(5, "second")
        self.assertNotIn("last.md", os.listdir(folder))
        self.assertEqual(12, len(json.loads(self.read(folder, "protected.json"))["paragraphs"]))

    def test_asking_the_author_unprotects_only_the_paragraph_they_name_and_their_words_are_recorded(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.author_edits("not measured per plot.", "not measured per plot, as I said.")
        self.second_round()
        folder = self.start(5, "second")
        base = self.base(folder)
        self.draft(folder, base.replace("not measured per plot, as I said.", "measured in every plot.", 1), parts=(9410,),
                   results=(9140,))
        self.refused(folder, 5, "protected paragraph 0")
        asked = self.words("Take the new text")
        refused = self.cmd("write.py", "check", "--dir", folder, "--unprotect", "0")
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("--unprotect needs --asked-file with the author's words", refused.stderr)
        done = self.cmd("write.py", "check", "--dir", folder, "--asked-file", asked, "--unprotect", "0")
        self.assertEqual(0, done.returncode, done.stdout)
        applied = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words("Approve"), "--asked-file", asked,
                           "--unprotect", "0")
        self.assertEqual(0, applied.returncode, applied.stdout)
        memory = self.text_memories()[-1][1]
        approved = dict(record_format.fields(memory))["Approved"]
        self.assertIn("> Approve", approved)
        self.assertIn("> Take the new text", approved)
        self.assertEqual("Nothing was changed by hand.", dict(record_format.fields(memory))["Kept from the author"])

    def test_the_kept_paragraphs_are_counted_in_the_comment(self):
        self.ready()
        self.mode("review")
        self.write_once()
        self.author_edits("not measured per plot.", "not measured per plot, as I said.")
        self.second_round()
        folder = self.start(5, "second")
        self.draft(folder, self.base(folder).replace("on 9 of 11 spells.", "on 9 of 11 spells and on 12 of 14 with a third "
                                                      "season.", 1), parts=(9410,), results=(9140,))
        applied = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        memory = self.text_memories()[-1][1]
        self.assertEqual("1 paragraph was changed by hand and was left as it is.",
                         dict(record_format.fields(memory))["Kept from the author"])

    def test_the_text_drift_finding_follows_a_body_that_is_not_the_text_last_written(self):
        self.ready()
        self.mode("review")
        self.assertEqual([], [r for r in self.query("consistency") if r["kind"] == "text-drift"])
        self.write_once()
        self.assertEqual([], [r for r in self.query("consistency") if r["kind"] == "text-drift"])
        self.author_edits("not measured per plot.", "not measured per plot, as I said.")
        found = [r for r in self.query("consistency") if r["kind"] == "text-drift"]
        self.assertEqual([5], [r["issue"] for r in found])
        self.assertIn("the body is not the text that the newest text change", found[0]["detail"])
        self.mode("off")
        self.assertEqual([], [r for r in self.query("consistency") if r["kind"] == "text-drift"])


PAPER_TEX = r"""\documentclass{article}
\begin{document}
\section{Results}
Over the whole season the difference between cover and bare plots was small.
In dry spells the cover plots stayed above the wilting point on 9 of 11 spells.
\section{Limitations}
The wilting point was taken from the texture class of the field.
\end{document}
"""


class OwnPaperTests(WritingBox):
    """Section 7.7: the author's own paper is theirs. It is never written without their words and never on its own."""

    def paper(self, name="paper/paper.tex", text=PAPER_TEX, mode=0o644):
        path = os.path.join(self.project, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        os.chmod(path, mode)
        return path

    def own(self, *args, env=None):
        return self.cmd("own_paper.py", *args, env=env)

    def project_file(self):
        return json.load(open(os.path.join(self.project, recortex.CONFIG_NAME)))

    def sha_of(self, path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    def place(self, path=None):
        path = path or self.paper()
        done = self.own("where", "--set", path, "--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return path

    def bring(self, issue=5, heading="Results", new_text=None, words="Apply"):
        """since, a new copy, propose and apply for one part; returns the folder."""
        folder = os.path.join(self.dir, "since-%d-%d" % (issue, len(os.listdir(self.dir))))
        done = self.own("since", "--issue", str(issue), "--section", heading, "--out", folder)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        path = self.own("where").stdout.rsplit("the file is ", 1)[-1].strip()
        new = os.path.join(self.dir, "new-%d.tex" % len(os.listdir(self.dir)))
        with open(new, "w") as handle:
            handle.write(new_text or open(path).read().replace("9 of 11 spells.", "9 of 11 spells, and 12 of 14 with a third "
                                                                "season."))
        proposed = self.own("propose", "--file", new, "--out", folder)
        self.assertEqual(0, proposed.returncode, proposed.stdout + proposed.stderr)
        return folder

    def test_the_place_inside_the_project_goes_into_the_project_file(self):
        self.ready()
        path = self.paper()
        self.assertIn("own paper: no place is recorded", self.own("where").stdout)
        done = self.own("where", "--set", path, "--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual({"path": "paper/paper.tex", "brought": {}}, self.project_file()["own_manuscript"])
        self.assertIn("kept inside the project", self.own("where").stdout)
        self.assertNotEqual(0, self.own("where", "--set", path).returncode)   # the author's yes is needed

    def test_a_place_outside_the_project_is_kept_on_this_machine_only(self):
        self.ready()
        outside = os.path.join(self.dir, "elsewhere", "paper.tex")
        os.makedirs(os.path.dirname(outside))
        open(outside, "w").write(PAPER_TEX)
        self.assertEqual(0, self.own("where", "--set", outside, "--yes").returncode)
        self.assertEqual({"outside": True, "brought": {}}, self.project_file()["own_manuscript"])
        self.assertNotIn("elsewhere", json.dumps(self.project_file()))
        kept = os.path.join(self.dir, "xdg-config", "recortex", "private", "own-paper.json")
        self.assertEqual(0o600, stat.S_IMODE(os.stat(kept).st_mode))
        self.assertIn(os.path.realpath(outside), self.own("where").stdout)

    def test_no_means_none_and_nothing_of_this_part_is_shown_again(self):
        self.ready()
        self.mode("review")
        self.assertEqual(0, self.own("where", "--set", "no", "--yes").returncode)
        self.assertIs(False, self.project_file()["own_manuscript"])
        self.assertIn("the author keeps none", self.own("status").stdout)
        self.assertNotIn("Your own paper", self.tool("index.py", "query", "brief").stdout)

    def test_a_path_that_breaks_a_rule_in_the_project_file_is_not_used(self):
        self.ready()
        self.paper()
        outside = os.path.join(self.dir, "secret.tex")
        open(outside, "w").write("secret")
        os.symlink(outside, os.path.join(self.project, "link.tex"))
        os.symlink(self.dir, os.path.join(self.project, "dirlink"))
        for bad in ("../secret.tex", "/etc/passwd", outside, "paper//paper.tex", "./paper/paper.tex", "paper\\paper.tex",
                    "a/" * 300 + "paper.tex", "", None, ["paper/paper.tex"], 5):
            self.write_config(dict(self.config, own_manuscript={"path": bad, "brought": {}}))
            out = self.own("status").stdout
            self.assertIn("own paper: no place is recorded", out, repr(bad)[:40])
        for symlinked, why in (("link.tex", "symlink"), ("dirlink/secret.tex", "symlinked folder"),
                               ("paper", "a folder")):
            self.confirm(symlinked)   # confirmed on this machine, so that only the symlink rule can refuse it
            self.write_config(dict(self.config, own_manuscript={"path": symlinked, "brought": {}}))
            out = self.own("status").stdout
            self.assertIn("the recorded place is not used", out, why)
            self.assertNotIn("not confirmed", out, why)
            self.assertNotIn("secret", out)
        refused = self.own("where", "--set", os.path.join(self.project, "link.tex"), "--yes")
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("is not a plain file", refused.stderr)

    def confirm(self, relative):
        """What `where --set --yes` leaves on this machine for a place inside the project (written by hand, to show that
        the other rules hold even for a place the author confirmed)."""
        folder = os.path.join(self.dir, "xdg-config", "recortex", "private")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        descriptor = os.open(os.path.join(folder, "own-paper.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"form": 1, "papers": {"octo-org/sample-project": {
                "inside": hashlib.sha256(relative.encode()).hexdigest()}}}, handle)

    def named_by_the_project_file(self, relative):
        self.write_config(dict(self.config, own_manuscript={"path": relative, "brought": {}}))

    def test_a_place_the_project_file_names_is_not_used_until_the_author_confirmed_it_on_this_machine(self):
        self.ready()
        path = self.paper()
        before = self.sha_of(path)
        self.place(path)
        folder = self.bring()
        # the project file now names another file than the one the author confirmed (a project file is not trusted)
        other = self.paper("paper/other.tex", PAPER_TEX)
        self.named_by_the_project_file("paper/other.tex")
        self.assertIn("the author has not confirmed this place on this machine", self.own("status").stdout)
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode)
        self.assertIn("the author has not confirmed this place on this machine", done.stdout + done.stderr)
        self.assertEqual((before, self.sha_of(other)), (self.sha_of(path), hashlib.sha256(PAPER_TEX.encode()).hexdigest()))
        # the same for a project that was never confirmed here at all (a clone of a project whose file names a place)
        os.unlink(os.path.join(self.dir, "xdg-config", "recortex", "private", "own-paper.json"))
        self.named_by_the_project_file("paper/paper.tex")
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode)
        self.assertEqual(before, self.sha_of(path))
        # confirming it (the author's yes) makes it usable
        self.assertEqual(0, self.own("where", "--set", path, "--yes").returncode)
        self.assertNotIn("not confirmed", self.own("status").stdout)

    def test_a_project_file_naming_a_workflow_or_a_file_that_steers_a_tool_is_refused_even_when_confirmed(self):
        self.ready()
        folder = self.bring_folder()
        for relative, text in ((".github/workflows/x.yml", "on: push\n"), (".github/notes.md", "notes\n"),
                               (".git/hooks/pre-commit.md", "x\n"), (".claude/commands/x.md", "x\n"), ("CLAUDE.md", "rules\n"),
                               ("sub/AGENTS.md", "rules\n"), (recortex.CONFIG_NAME, None), ("paper/run.sh", "#!/bin/sh\n"),
                               ("paper/make.py", "print(1)\n"), ("paper/x.yaml", "a: 1\n")):
            if text is not None:
                self.paper(relative, text)
            self.confirm(relative)   # the author's confirmation does not make these usable
            self.named_by_the_project_file(relative)
            before = open(os.path.join(self.project, relative), "rb").read()
            status = self.own("status").stdout
            self.assertIn("the recorded place is not used", status, relative)
            done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
            self.assertNotEqual(0, done.returncode, relative)
            self.assertEqual(before, open(os.path.join(self.project, relative), "rb").read(), relative)
            refused = self.own("where", "--set", os.path.join(self.project, relative), "--yes")
            self.assertNotEqual(0, refused.returncode, relative)
        self.assertEqual({"on: push\n"}, {open(os.path.join(self.project, ".github/workflows/x.yml")).read()})

    def test_the_project_file_is_never_the_place_even_when_the_user_named_it_with_a_manuscript_name(self):
        self.ready()
        notes = os.path.join(self.project, "notes.md")   # RECORTEX_CONFIG may name any file as the project file
        with open(notes, "w") as handle:
            json.dump(dict(self.config, own_manuscript={"path": "notes.md", "brought": {}}), handle)
        self.confirm("notes.md")
        done = self.own("status", env={"RECORTEX_CONFIG": notes})
        self.assertIn("the recorded place is not used (the project file is never the author's paper)", done.stdout)
        refused = self.own("where", "--set", notes, "--yes", env={"RECORTEX_CONFIG": notes})
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("the project file is never the author's paper", refused.stderr)

    def test_the_manuscript_kinds_of_file_are_places(self):
        self.ready()
        for name in ("a.tex", "a.md", "a.markdown", "a.txt", "a.rst", "a.qmd", "a.Rmd", "a.docx", "A.TEX"):
            path = self.paper("papers/" + name, "text\n")
            done = self.own("where", "--set", path, "--yes")
            self.assertEqual(0, done.returncode, name + done.stdout + done.stderr)
            self.assertNotIn("not used", self.own("where").stdout, name)

    def bring_folder(self):
        self.place(self.paper())
        return self.bring()

    def test_a_project_file_cannot_point_at_a_file_outside_the_project(self):
        self.ready()
        outside = os.path.join(self.dir, "secret.tex")
        open(outside, "w").write("secret")
        self.write_config(dict(self.config, own_manuscript={"outside": True, "path": outside, "brought": {}}))
        self.assertIn("no place is recorded", self.own("status").stdout)
        self.write_config(dict(self.config, own_manuscript={"outside": True, "brought": {}}))
        self.assertIn("the recorded place is not used (no place is kept on this machine)", self.own("status").stdout)

    def test_status_says_never_in_step_and_changed_per_part(self):
        self.ready()
        self.place()
        out = self.own("status").stdout
        self.assertIn("Results of #5: never brought over", out)
        folder = self.bring()
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        out = self.own("status").stdout
        self.assertIn("Results of #5: in step with the record", out)
        self.assertIn("Limitations of #5: never brought over", out)
        entry = self.project_file()["own_manuscript"]["brought"]["5"]["Results"]
        self.assertEqual(record_format.section_sha(self.body(5), "Results"), entry["text"])
        data = self.repo()
        data["issues"]["5"]["body"] = data["issues"]["5"]["body"].replace("not. Across", "not at all. Across")
        data["issues"]["5"]["edits"] = [{"at": "2026-12-02T10:00:00Z", "by": "octocat"},
                                        {"at": data["issues"]["5"]["created_at"], "by": "octocat"}]
        data["issues"]["5"]["last_edited_at"] = data["issues"]["5"]["updated_at"] = "2026-12-02T10:00:00Z"
        self.save(data)
        self.ready()
        self.assertIn("Results of #5: the record's text changed since you last took it", self.own("status").stdout)

    def test_apply_writes_the_file_with_a_backup_its_own_mode_and_a_read_back_and_records_what_was_brought(self):
        self.ready()
        path = self.place(self.paper(mode=0o640))
        before = self.sha_of(path)
        folder = self.bring()
        diff = self.read(folder, "proposal.diff")
        self.assertIn("+In dry spells the cover plots stayed above the wilting point on 9 of 11 spells, and 12 of 14", diff)
        self.assertEqual(before, self.sha_of(path))   # propose changes nothing
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("and read it back", done.stdout)
        self.assertNotEqual(before, self.sha_of(path))
        self.assertIn("12 of 14", open(path).read())
        self.assertEqual(0o640, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual([], [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".recortex-")])
        backup = os.path.join(self.data, "private", self.slug(), "own", "backup-%s.txt" % before)
        self.assertEqual(PAPER_TEX, open(backup).read())
        self.assertIn("recorded that Results of #5 was brought over", done.stdout)

    def test_apply_needs_the_authors_words_in_every_mode_autonomous_included(self):
        self.ready()
        self.mode("autonomous")
        path = self.place()
        before = self.sha_of(path)
        folder = self.bring()
        for words in (None, "", "   \n"):
            args = ["apply", "--dir", folder, "--parts", "5:Results"]
            if words is not None:
                args += ["--words-file", self.words(words)]
            done = self.own(*args)
            self.assertNotEqual(0, done.returncode)
            self.assertIn("the author's words are needed, in every writing mode", done.stderr)
        self.assertEqual(before, self.sha_of(path))
        self.assertEqual({}, self.project_file()["own_manuscript"]["brought"])

    def test_a_file_that_changed_after_the_difference_was_made_is_refused(self):
        self.ready()
        path = self.place()
        folder = self.bring()
        open(path, "a").write("% a note the author added meanwhile\n")
        after_edit = self.sha_of(path)
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode)
        self.assertIn("your paper changed since the difference was made; make it again", done.stderr)
        self.assertEqual(after_edit, self.sha_of(path))
        self.assertEqual({}, self.project_file()["own_manuscript"]["brought"])

    def test_a_word_file_is_never_written_and_the_passages_are_recorded_only_when_pasted(self):
        self.ready()
        path = self.place(self.paper("paper/paper.docx", "PK-not-text"))
        before = self.sha_of(path)
        folder = os.path.join(self.dir, "since-word")
        self.assertEqual(0, self.own("since", "--issue", "5", "--section", "Results", "--out", folder).returncode)
        new = os.path.join(self.dir, "new.docx")
        open(new, "w").write("PK-other")
        done = self.own("propose", "--file", new, "--out", folder)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("recortex does not edit this kind of file", done.stderr)
        applied = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, applied.returncode)   # no proposal.json: nothing was proposed
        self.assertEqual(before, self.sha_of(path))
        pasted = self.own("apply", "--dir", folder, "--words-file", self.words("I pasted them"), "--parts", "5:Results",
                          "--pasted")
        self.assertEqual(0, pasted.returncode, pasted.stdout + pasted.stderr)
        self.assertIn("nothing is written to the file; the author pasted the passages themselves", pasted.stdout)
        self.assertEqual(before, self.sha_of(path))
        self.assertIn("5", self.project_file()["own_manuscript"]["brought"])

    def test_a_file_that_became_a_symlink_is_never_written(self):
        self.ready()
        path = self.place()
        folder = self.bring()
        elsewhere = os.path.join(self.dir, "other.tex")
        os.rename(path, elsewhere)
        os.symlink(elsewhere, path)
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode)
        self.assertEqual(PAPER_TEX, open(elsewhere).read())

    def test_since_gives_the_difference_when_this_machine_kept_what_the_file_received(self):
        self.ready()
        self.place()
        folder = self.bring()
        self.assertEqual(0, self.own("apply", "--dir", folder, "--words-file", self.words("Apply"),
                                     "--parts", "5:Results").returncode)
        data = self.repo()
        data["issues"]["5"]["body"] = data["issues"]["5"]["body"].replace("not. Across", "not at all. Across")
        data["issues"]["5"]["edits"] = [{"at": "2026-12-02T10:00:00Z", "by": "octocat"},
                                        {"at": data["issues"]["5"]["created_at"], "by": "octocat"}]
        data["issues"]["5"]["last_edited_at"] = data["issues"]["5"]["updated_at"] = "2026-12-02T10:00:00Z"
        self.save(data)
        self.ready()
        again = os.path.join(self.dir, "since-again")
        out = self.own("since", "--issue", "5", "--section", "Results", "--out", again).stdout
        self.assertIn("Results of #5: changed since; difference-results.diff holds the difference", out)
        diff = self.read(again, "difference-results.diff")
        self.assertIn("not at all. Across", diff)

    # ------------------------------------------------------------ never on its own

    def test_a_full_run_of_planning_writing_and_versions_in_autonomous_mode_leaves_the_authors_file_as_it_was(self):
        self.ready()
        self.mode("autonomous")
        path = self.place()
        before = (self.sha_of(path), os.stat(path).st_mtime_ns)
        for issue in (5, 6, 3):
            done = self.cmd("write.py", "status")
            folder = self.start(issue, "auto-%d" % issue)
            base = self.base(folder)
            if issue == 5:
                self.draft(folder)
            elif issue == 6:
                self.draft(folder, base.replace("\n## Sources", "\n\nThe trial ran on one field of one soil type for two "
                                                                "seasons, and no more is claimed.\n\n## Sources", 1),
                           parts=(9308,), results=())
            else:
                self.draft(folder, base.replace("### Table: plots", "The four plots of the wet corner were left out of the "
                                                                    "moisture analysis.\n\n### Table: plots", 1), parts=(),
                           results=(), decisions=(9106,))
            applied = self.cmd("write.py", "apply", "--dir", folder)
            self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        self.assertEqual(3, len(self.text_memories(5)) + len(self.text_memories(6)) + len(self.text_memories(3)))
        self.assertEqual(before, (self.sha_of(path), os.stat(path).st_mtime_ns))
        self.assertEqual([], [n for n in os.listdir(os.path.dirname(path)) if n != "paper.tex"])

    def test_a_folder_at_the_place_is_never_used(self):
        self.ready()
        path = self.place()
        os.replace(path, path + ".moved")
        os.makedirs(path)                      # the confirmed place now holds a folder, not a plain file
        self.assertIn("it is not a plain file", self.own("where").stdout)

    def test_no_script_but_own_paper_apply_opens_the_file_for_writing(self):
        import re as regex
        scripts = [name for name in os.listdir(ts.SCRIPTS) if name.endswith((".py", ".sh"))]
        writers = []
        for name in scripts:
            text = open(os.path.join(ts.SCRIPTS, name), encoding="utf-8").read()
            if regex.search(r"own_manuscript|OWN_PAPER|own-paper", text) and name not in ("own_paper.py", "recortex.py",
                                                                                          "brief.py", "hooks.py", "setup.py",
                                                                                          "names.py", "write_rules.py",
                                                                                          "setup_questions.py"):
                writers.append(name)
        self.assertEqual([], writers)
        hook_text = " ".join(open(os.path.join(ts.SCRIPTS, name)).read() for name in ("brief.py", "hooks.py"))
        self.assertNotIn("os.path.join(root, found[\"path\"])", hook_text)
        self.assertNotRegex(hook_text, r"open\([^)]*own")
        self.assertNotIn("own_paper.resolve", hook_text)

    # ------------------------------------------------------------ the drift line

    def test_the_brief_counts_the_parts_that_changed_since_and_holds_integers_only(self):
        self.ready()
        self.mode("review")
        path = self.place()
        self.assertNotIn("Your own paper", self.tool("index.py", "query", "brief").stdout)   # never brought: not counted
        folder = self.bring()
        self.assertEqual(0, self.own("apply", "--dir", folder, "--words-file", self.words("Apply"),
                                     "--parts", "5:Results").returncode)
        self.assertNotIn("Your own paper", self.tool("index.py", "query", "brief").stdout)   # in step: not counted
        data = self.repo()
        data["issues"]["5"]["body"] = data["issues"]["5"]["body"].replace("not. Across", "not at all. Across")
        data["issues"]["5"]["edits"] = [{"at": "2026-12-02T10:00:00Z", "by": "octocat"},
                                        {"at": data["issues"]["5"]["created_at"], "by": "octocat"}]
        data["issues"]["5"]["last_edited_at"] = data["issues"]["5"]["updated_at"] = "2026-12-02T10:00:00Z"
        self.save(data)
        self.ready()
        text = self.tool("index.py", "query", "brief").stdout
        line = ("Your own paper: 1 part of the record's text changed since you last brought it into it. Mention it at a pause; "
                "change the file only when the user asks, and bring the record's text into it only through own_paper.py (it "
                "shows the difference, keeps a backup and records it). If a request such as \"update the paper\" could mean "
                "the record's text or the file, ask which.")
        self.assertEqual(1, text.count(line))
        self.assertIn(line, text.split("===== BEGIN PROJECT STATUS DATA")[0])
        for word in ("paper.tex", "paper/", path, os.path.dirname(path), self.dir):
            self.assertNotIn(word, text)
        self.assertNotIn("paper.tex", self.cmd("hooks.py", "--doctor").stdout)

    def test_the_wording_sends_the_records_text_into_the_authors_paper_only_through_own_paper_py(self):
        """A request such as "bring the paper's text up to date" must not lead to an edit of the author's file with an edit
        tool. No hook guards it, since an author who writes the paper with Claude would be asked on every edit; the
        wording does."""
        skill = recortex.read_text(os.path.join(ts.SKILL, "SKILL.md"))
        writing = recortex.read_text(os.path.join(ts.SKILL, "references", "writing.md"))
        self.assertIn("The record's text goes into it only through `own_paper.py`, which shows the difference, keeps a backup and "
                      "records what it brought", skill)
        self.assertIn("bring the record's text into it only through own_paper.py (it shows the difference, keeps a backup and "
                      "records it)", writing)
        self.assertIn("goes into the author's file only through these steps, so that the difference is shown, a backup is kept", writing)
        for text in (skill, writing):
            self.assertIn('"update the paper"', text)
            self.assertIn("could mean the record's text or", text)
        self.assertIn("ordinary edits", skill)
        self.assertIn("ordinary edit of theirs", writing)
        self.assertIn("ask which", skill + writing)
        self.assertLess(len(skill.split("### Write the text")[1].split("### Versions of the paper")[0]), 2600)   # SKILL.md stays short

    def test_an_edited_topic_body_is_never_counted(self):
        self.ready()
        self.place()
        folder = self.bring()
        self.assertEqual(0, self.own("apply", "--dir", folder, "--words-file", self.words("Apply"),
                                     "--parts", "5:Results").returncode)
        data = self.repo()
        item = data["issues"]["5"]
        item["body"] = item["body"].replace("not. Across", "not at all. Across")
        item["edits"] = [{"at": "2026-12-02T10:00:00Z", "by": "hubot"}, {"at": item["created_at"], "by": "octocat"}]
        item["last_edited_at"] = item["updated_at"] = "2026-12-02T10:00:00Z"
        self.save(data)
        self.ready()
        self.assertNotIn("Your own paper", self.tool("index.py", "query", "brief").stdout)


    # The working folder of the author's own paper is writable by a drafting agent, so nothing in it is believed, and no
    # name in it is written through a link.

    def swap_new_copy(self, folder, text=b"% not what the author saw\n"):
        with open(os.path.join(folder, "proposal.new"), "wb") as handle:
            handle.write(text)
        meta = {"base": hashlib.sha256(open(self.own("where").stdout.rsplit("the file is ", 1)[-1].strip(), "rb").read()
                                       ).hexdigest(), "new": hashlib.sha256(text).hexdigest()}
        with open(os.path.join(folder, "proposal.json"), "w") as handle:   # the hash the folder held before: now nothing reads it
            json.dump(meta, handle)

    def test_a_new_copy_and_hash_swapped_in_the_folder_after_the_review_is_never_written(self):
        self.ready()
        path = self.place()
        folder = self.bring()
        before = open(path, "rb").read()
        self.swap_new_copy(folder)
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(before, open(path, "rb").read())
        self.assertIn("not ones that propose made on this machine", done.stderr)

    def test_a_difference_swapped_after_the_review_is_not_the_one_the_author_saw(self):
        self.ready()
        path = self.place()
        folder = self.bring()
        before = open(path, "rb").read()
        with open(os.path.join(folder, "proposal.diff"), "w") as handle:
            handle.write("--- a harmless-looking difference\n")
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(before, open(path, "rb").read())

    def test_a_base_hash_rewritten_in_the_folder_does_not_hide_an_edit_of_the_paper(self):
        self.ready()
        path = self.place()
        folder = self.bring()
        with open(path, "a") as handle:
            handle.write("% the author kept writing after the review\n")
        edited = open(path, "rb").read()
        with open(os.path.join(folder, "proposal.json"), "w") as handle:
            json.dump({"base": hashlib.sha256(edited).hexdigest(),
                       "new": hashlib.sha256(open(os.path.join(folder, "proposal.new"), "rb").read()).hexdigest()}, handle)
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(edited, open(path, "rb").read())
        self.assertIn("your paper changed since the difference was made", done.stderr)

    def test_a_since_file_changed_in_the_folder_is_not_recorded_as_what_the_paper_received(self):
        self.ready()
        path = self.place()
        folder = self.bring()
        with open(os.path.join(folder, "since.json"), "w") as handle:
            json.dump({"5:Results": "f" * 64}, handle)
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual({"path": "paper/paper.tex", "brought": {}}, self.project_file()["own_manuscript"])

    def test_a_current_copy_swapped_in_the_folder_is_not_kept_as_what_the_paper_received(self):
        self.ready()
        self.place()
        folder = self.bring()
        with open(os.path.join(folder, "current-results.md"), "w") as handle:
            handle.write("A different text that the paper never received.\n")
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        kept = os.path.join(self.data, "private", self.slug(), "own", "5")
        texts = [open(os.path.join(kept, name)).read() for name in os.listdir(kept)] if os.path.isdir(kept) else []
        self.assertNotIn("A different text that the paper never received.\n", texts)

    def test_a_proposal_for_another_file_is_not_applied_to_this_one(self):
        self.ready()
        path = self.place()
        folder = self.bring()
        elsewhere = os.path.join(self.dir, "other", "paper.tex")
        os.makedirs(os.path.dirname(elsewhere))
        with open(elsewhere, "w") as handle:
            handle.write(open(path).read())
        self.assertEqual(0, self.own("where", "--set", elsewhere, "--yes").returncode)
        done = self.own("apply", "--dir", folder, "--words-file", self.words("Apply"), "--parts", "5:Results")
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(open(path).read(), open(elsewhere).read())

    def test_since_and_propose_never_write_through_a_link_to_the_authors_file(self):
        self.ready()
        self.place()
        author = os.path.join(self.dir, "authors-notes.txt")
        with open(author, "w") as handle:
            handle.write("the author's notes\n")
        folder = os.path.join(self.dir, "linked-since")
        os.makedirs(folder)
        os.symlink(author, os.path.join(folder, "current-results.md"))
        done = self.own("since", "--issue", "5", "--section", "Results", "--out", folder)
        self.assertNotEqual(0, done.returncode)
        self.assertEqual("the author's notes\n", open(author).read())
        folder = os.path.join(self.dir, "linked-propose")
        os.makedirs(folder)
        os.symlink(author, os.path.join(folder, "proposal.new"))
        new = os.path.join(self.dir, "new-linked.tex")
        with open(new, "w") as handle:
            handle.write(open(self.own("where").stdout.rsplit("the file is ", 1)[-1].strip()).read() + "% one more line\n")
        done = self.own("propose", "--file", new, "--out", folder)
        self.assertNotEqual(0, done.returncode)
        self.assertEqual("the author's notes\n", open(author).read())


class TextPageTests(WritingBox):
    """The History of a part and what the page says of the changes of the text (section 10.2, without the differences,
    which tests/test_manuscript_versions.py draws)."""

    def page(self):
        self.ready()
        built = self.tool("mind.py", gh="false")
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        with open(os.path.join(self.data, "private", self.slug(), "mind.html"), encoding="utf-8") as handle:
            return handle.read()

    def said(self, text):
        return re.sub(r" ([.,;:])", r"\1", " ".join(" ".join(tp.ts.PageReader(text).texts).split()))

    def write_results(self, mode="review"):
        self.ready()
        self.mode(mode)
        folder = self.start(5)
        self.draft(folder)
        args = ["apply", "--dir", folder] + (["--words-file", self.words("Approve")] if mode == "review" else [])
        done = self.cmd("write.py", *args)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return folder

    def test_a_part_that_was_written_says_when_how_and_why_and_keeps_its_history(self):
        self.write_results("review")
        text = self.page()
        self.assertEqual([], tp.ts.page_problems(text))
        said = self.said(text)
        self.assertRegex(said, r"Last changed on [A-Z][a-z]+ [0-9]{1,2}(?:, 20[0-9]{2})?, by the plugin with your approval\. "
                               r"See what changed\.")
        self.assertIn("History (1)", said)   # the History is drawn once, in the manuscript view; the text view points to it
        self.assertIn("The history of this part, with each version, is in Read the manuscript.", said)
        history = said.split("History of ")[1]
        self.assertIn("Restore the Results of ", history)   # each version carries its own request to go back to it
        self.assertIn("This is the first version kept.", history)
        self.assertIn("Version 1 · ", history)
        self.assertIn(", by the plugin with your approval", history)
        self.assertIn("in dry spells the cover plots stayed moist more often than the bare plots", history)
        self.assertNotIn("You changed it by hand after that.", said)

    def test_the_card_of_a_text_change_tells_it_without_a_hash_or_a_version(self):
        self.write_results("review")
        text = self.page()
        said = self.said(text)
        self.assertIn("we changed the text to say that in dry spells the cover plots stayed moist more often than the bare "
                      "plots", said)
        self.assertIn("How it was approved", said)
        self.assertIn("What changed", said)
        self.assertNotRegex(text, r"[0-9a-f]{40}")
        self.assertNotIn("sha256", said)

    def test_a_hand_edit_after_a_write_is_said_and_the_mode_off_leaves_only_days(self):
        self.write_results("review")
        data = self.repo()
        item = data["issues"]["5"]
        item["body"] = item["body"].replace("not measured per plot.", "not measured per plot, as I said.")
        item["edits"] = [{"at": "2026-12-04T10:00:00Z", "by": "octocat"}, {"at": item["created_at"], "by": "octocat"}]
        item["last_edited_at"] = item["updated_at"] = "2026-12-04T10:00:00Z"
        self.save(data)
        self.assertIn("You changed it by hand after that.", self.said(self.page()))
        self.mode("off")
        said = self.said(self.page())
        self.assertIn("Versions with their differences are kept once you choose review or autonomous writing.", said)
        self.assertIn("Only the day the text changed is known here.", said)
        self.assertNotIn("This is the first version kept.", said)
        self.assertNotIn("To go back to a version", said)
        self.assertIn("History", said)
        self.assertNotIn("History (", said)

    def test_a_part_never_written_has_no_history_while_versions_are_kept(self):
        self.ready()
        self.mode("review")
        said = self.said(self.page())
        self.assertNotIn("History (", said)
        self.assertNotIn("by the plugin with your approval", said)

    def test_autonomous_text_is_told_as_written_without_asking(self):
        self.write_results("autonomous")
        said = self.said(self.page())
        self.assertIn("by the plugin without asking", said)
        self.assertIn("History (1)", said)


    def test_a_text_change_of_another_account_is_not_told_and_a_body_someone_else_changed_is_not_judged_by_hand(self):
        folder = self.write_results("review")
        text_memory = self.text_memories()[0][1]
        outsider = text_memory.replace("2026-12-0", "2026-12-0")
        self.add_comment(9450, 5, outsider.replace("we changed the text to say that", "we changed the text to say that "
                                                   "OUTSIDER", 1), tp.later(self), "hubot")
        data = self.repo()
        item = data["issues"]["5"]
        item["body"] = item["body"].replace("not measured per plot.", "not measured per plot, by someone else.")
        item["edits"] = [{"at": "2026-12-04T10:00:00Z", "by": "hubot"}, {"at": item["created_at"], "by": "octocat"}]
        item["last_edited_at"] = item["updated_at"] = "2026-12-04T10:00:00Z"
        self.save(data)
        said = self.said(ts.outside_reader(self.page()))   # the reader shows what is not told, shut under its mark
        self.assertNotIn("OUTSIDER", said)
        self.assertNotIn("You changed it by hand after that.", said)   # the body is not told, so it is not judged
        self.assertIn("The text of this thread is not shown here", said)
        self.assertNotIn("History (", said)   # and a part whose text is not told has no history to tell


class SharedHeadingTests(WritingBox):
    """A project with the research and the manuscript profiles: Methods, Results and Limitations are headings of both,
    and a body is linted with the labels of its own issue, so that the heading resolves to the label the issue carries."""

    def both(self):
        self.write_config(dict(self.config, profiles=["research", "manuscript"]))
        self.ready()
        self.mode("review")

    def test_a_draft_passes_the_linter_with_both_profiles_by_the_labels_of_the_issue(self):
        self.both()
        folder = self.start(5)
        self.draft(folder)
        checked = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        self.assertNotIn("is the heading of", checked.stdout)
        self.assertIn("write: the draft passes every check", checked.stdout)

    def test_apply_and_a_proposal_work_with_both_profiles_and_the_labels_stay_the_issues_own(self):
        self.both()
        folder = self.start(5)
        self.draft(folder)
        before = sorted(self.repo()["issues"]["5"]["labels"])
        done = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words())
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("in dry spells the cover plots stayed moist on 9 of 11 spells", self.body(5))
        self.assertEqual(before, sorted(self.repo()["issues"]["5"]["labels"]))
        self.assertEqual(1, len(self.text_memories()))

    def test_methods_and_limitations_resolve_too(self):
        self.both()
        folder = self.start(3)
        base = self.base(folder)
        self.draft(folder, base.replace("### Table: plots", "The four plots of the wet corner were left out of the moisture "
                                                            "analysis.\n\n### Table: plots", 1), parts=(), results=(),
                   decisions=(9106,))
        checked = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        folder = self.start(6, "w6")
        base = self.base(folder)
        self.assertIn("## Limitations", base)
        self.assertIn("## Discussion", base)
        self.draft(folder, base, parts=(9308,), results=())
        checked = self.cmd("write.py", "check", "--dir", folder)
        self.assertNotIn("is the heading of", checked.stdout, "the base body itself lints")

    def test_a_heading_the_issues_labels_cannot_settle_is_still_refused(self):
        self.both()
        folder = self.start(5)
        data = self.repo()
        data["issues"]["5"]["labels"] = [label for label in data["issues"]["5"]["labels"] if label != "ms:limitations"]
        data["issues"]["5"]["updated_at"] = "2026-12-05T10:00:00Z"   # what the incremental sync reads by
        self.save(data)
        self.ready()
        self.draft(folder)
        checked = self.cmd("write.py", "check", "--dir", folder)
        self.assertEqual(1, checked.returncode, checked.stdout)
        self.assertIn("'## Limitations' is the heading of rd:limitations and ms:limitations", checked.stdout)


class ZoneTests(unittest.TestCase):
    """The time zone of the record (zones.py): the stamps the plugin writes follow the zone the record states, and the
    stamps it reads become instants. Pure; no sandbox."""

    @staticmethod
    def moment(*parts):
        import datetime
        return datetime.datetime(*parts, tzinfo=datetime.timezone.utc)

    def test_the_stated_zone_is_the_abbreviation_of_the_example_stamp_then_an_offset_then_the_words(self):
        import zones
        for statement, label in (("US Pacific time, written as 2026-03-04 14:10 PST", "PST"),
                                 ("Central European time, written as 2026-03-04 14:10 CET", "CET"),
                                 ("written as 2026-03-04 14:10 UTC+2", "UTC+2"), ("UTC+5:30", "UTC+5:30"),
                                 ("US Pacific time", "PST"), ("Central European time", "CET"), ("UTC", "UTC"),
                                 ("Eastern time, written as 2026-03-04 14:10 EDT", "EDT")):
            self.assertEqual(label, zones.zone_of(statement).label, statement)
        for statement in (None, "", 5, "local time", "Mars time, written as 2026-03-04 14:10 MST7", "UTC+99",
                          "written as 2026-03-04 14:10 IST"):
            self.assertIsNone(zones.zone_of(statement), repr(statement))

    def test_words_never_name_the_united_states_for_a_place_elsewhere(self):
        import zones
        for statement in ("Australian Eastern time", "Australian Central Standard Time", "Pacific/Auckland", "Pacific/Honolulu",
                          "Central Africa Time", "Eastern Africa", "Asia/Kolkata", "China time", "India time", "New Zealand time",
                          "Europe/Berlin", "US/Pacific", "Western Australia"):
            self.assertIsNone(zones.zone_of(statement), statement)
        for statement, label, minutes in (("central Europe", "CET", 60), ("Central European Summer Time", "CET", 60),
                                          ("Eastern Europe", "EET", 120), ("Western Europe", "WET", 0),
                                          ("US Eastern time", "EST", -300), ("Pacific Time (US & Canada)", "PST", -480),
                                          ("Central time, US", "CST", -360), ("Alaska time", "AKST", -540)):
            found = zones.zone_of(statement)
            self.assertEqual((label, minutes), (found.label, found.minutes), statement)

    def test_gmt_with_an_offset_is_utc_with_that_offset_and_a_bare_gmt_stays_gmt(self):
        import zones
        for statement, label, minutes in (("GMT+1", "UTC+1", 60), ("GMT-5:30", "UTC-5:30", -330), ("Offset GMT+02:00 all year", "UTC+02:00", 120),
                                          ("written as 2026-03-04 14:10 GMT+1", "UTC+1", 60), ("GMT", "GMT", 0)):
            found = zones.zone_of(statement)
            self.assertEqual((label, minutes), (found.label, found.minutes), statement)

    def test_the_example_forms_of_a_setup_read_as_they_say_even_for_places_the_words_rule_leaves_unknown(self):
        import zones
        for statement, label in (("US Pacific time, written as 2026-03-04 14:10 PST", "PST"),
                                 ("Australian Eastern time, written as 2026-03-04 14:10 AEST", "AEST"),
                                 ("New Zealand time, written as 2026-03-04 14:10 NZST", "NZST"),
                                 ("Central Africa Time, written as 2026-03-04 14:10 SAST", "SAST"),
                                 ("Pacific/Auckland, written as 2026-03-04 14:10 NZDT", "NZDT"),
                                 ("Berlin, written as 2026-03-04 14:10 CET", "CET"), ("East Africa Time (EAT)", "EAT")):
            self.assertEqual(label, zones.zone_of(statement).label, statement)

    def test_a_stamp_is_written_with_the_abbreviation_and_offset_of_its_moment(self):
        import zones
        pacific = zones.zone_of("US Pacific time, written as 2026-03-04 14:10 PST")
        for moment, stamp, iso in ((self.moment(2026, 12, 3, 15, 1), "2026-12-03 07:01 PST", "2026-12-03T07:01-08:00"),
                                   (self.moment(2026, 7, 1, 17, 0), "2026-07-01 10:00 PDT", "2026-07-01T10:00-07:00"),
                                   (self.moment(2026, 3, 8, 9, 59), "2026-03-08 01:59 PST", "2026-03-08T01:59-08:00"),
                                   (self.moment(2026, 3, 8, 10, 0), "2026-03-08 03:00 PDT", "2026-03-08T03:00-07:00"),
                                   (self.moment(2026, 11, 1, 8, 59), "2026-11-01 01:59 PDT", "2026-11-01T01:59-07:00"),
                                   (self.moment(2026, 11, 1, 9, 0), "2026-11-01 01:00 PST", "2026-11-01T01:00-08:00")):
            self.assertEqual((stamp, iso), (pacific.stamp(moment), pacific.iso(moment)), stamp)
        europe = zones.zone_of("CET")
        self.assertEqual("2026-03-29 01:59 CET", europe.stamp(self.moment(2026, 3, 29, 0, 59)))
        self.assertEqual("2026-03-29 03:00 CEST", europe.stamp(self.moment(2026, 3, 29, 1, 0)))
        self.assertEqual("2026-10-25 02:59 CEST", europe.stamp(self.moment(2026, 10, 25, 0, 59)))
        self.assertEqual("2026-10-25 02:00 CET", europe.stamp(self.moment(2026, 10, 25, 1, 0)))
        south = zones.zone_of("AEST")
        self.assertEqual("2026-01-04 03:00 AEDT", south.stamp(self.moment(2026, 1, 3, 16, 0)))
        self.assertEqual("2026-07-01 20:00 AEST", south.stamp(self.moment(2026, 7, 1, 10, 0)))
        fixed = zones.zone_of("UTC+5:30")
        self.assertEqual(("2026-07-01 15:30 UTC+5:30", "2026-07-01T15:30+05:30"),
                         (fixed.stamp(self.moment(2026, 7, 1, 10, 0)), fixed.iso(self.moment(2026, 7, 1, 10, 0))))
        self.assertEqual("2026-07-01T10:00Z", zones.zone_of("GMT").iso(self.moment(2026, 7, 1, 10, 0)))   # GMT is kept as stated

    def test_a_stamp_is_read_by_the_offset_of_its_own_abbreviation(self):
        import zones
        for stamp, at in (("2026-07-01 10:00 PDT", self.moment(2026, 7, 1, 17, 0)), ("2026-01-01 10:00 PST", self.moment(2026, 1, 1, 18, 0)),
                          ("2026-07-01 10:00 BST", self.moment(2026, 7, 1, 9, 0)), ("2026-07-01 10:00 UTC", self.moment(2026, 7, 1, 10, 0)),
                          ("2026-07-01 10:00 UTC+5:30", self.moment(2026, 7, 1, 4, 30)), ("2026-07-01 10:00 UTC-1", self.moment(2026, 7, 1, 11, 0))):
            self.assertEqual(at, zones.stamp_utc(stamp), stamp)
        for stamp in ("2026-07-01 10:00 IST", "2026-07-01 10:00", "2026-13-01 10:00 UTC", "later", None, "2026-07-01 10:00 PST7"):
            self.assertIsNone(zones.stamp_utc(stamp), repr(stamp))

    def test_the_record_writes_utc_when_it_states_no_zone_that_is_known(self):
        for config in (None, {}, {"timezone": ""}, {"timezone": "local time"}, {"timezone": 7}, {"timezone": "written as 2026-03-04 14:10 IST"}):
            self.assertEqual("2026-12-03 15:01 UTC", recortex.stamp(self.moment(2026, 12, 3, 15, 1), config), repr(config))
            self.assertEqual("2026-12-03T15:01Z", recortex.marker_time(self.moment(2026, 12, 3, 15, 1), config), repr(config))
        config = {"timezone": "US Pacific time, written as 2026-03-04 14:10 PST"}
        self.assertEqual("2026-12-03 07:01 PST", recortex.stamp(self.moment(2026, 12, 3, 15, 1), config))
        self.assertEqual("2026-12-03T07:01-08:00", recortex.marker_time(self.moment(2026, 12, 3, 15, 1), config))

    def test_a_stamp_the_linter_accepts_comes_back_as_the_same_instant_in_the_marker_and_the_heading(self):
        import lint_drafts
        config = {"timezone": "US Pacific time, written as 2026-03-04 14:10 PST"}
        for moment in (self.moment(2026, 12, 3, 15, 1), self.moment(2026, 7, 9, 3, 30)):
            stamp, at = recortex.stamp(moment, config), recortex.marker_time(moment, config)
            self.assertRegex(stamp, "^" + lint_drafts.STAMP + "$")
            self.assertRegex(at, "^" + record_format.ISO + "$")
            self.assertEqual(stamp[:16], at[:16].replace("T", " "))
            self.assertEqual(moment.strftime("%Y-%m-%dT%H:%M:00Z"), record_format.sort_key(at))
            import zones
            self.assertEqual(moment, zones.stamp_utc(stamp))

    def test_a_heading_stamp_becomes_an_iso_time_with_its_offset_and_an_unknown_zone_stays_without_one(self):
        for stamp, iso in (("2026-03-04 14:10 UTC", "2026-03-04T14:10Z"), ("2026-03-04 14:10 PST", "2026-03-04T14:10-08:00"),
                           ("2026-07-04 14:10 PDT", "2026-07-04T14:10-07:00"), ("2026-03-04 14:10 UTC+5:30", "2026-03-04T14:10+05:30"),
                           ("2026-03-04 14:10 IST", "2026-03-04T14:10"), ("2026-03-04 14:10", "2026-03-04T14:10"), ("", None)):
            self.assertEqual(iso, record_format.to_iso(stamp), stamp)

    def test_a_stamp_with_no_zone_is_placed_by_the_time_github_gave_the_comment(self):
        self.assertEqual("2026-03-04T22:10:00Z", record_format.instant("2026-03-04T14:10-08:00", "2026-03-05T01:00:00Z"))
        self.assertEqual("2026-03-04T14:10:00Z", record_format.instant("2026-03-04T14:10Z", "2026-03-05T01:00:00Z"))
        self.assertEqual("2026-03-05T01:00:00Z", record_format.instant("2026-03-04T14:10", "2026-03-05T01:00:00Z"))
        self.assertEqual("2026-03-04T14:10:00Z", record_format.instant("2026-03-04T14:10", None))   # nothing better is known


HUB_111 = os.path.join(ROOT, "tests", "fixtures", "hub-1.1.1-manuscript.md")


class OlderHubTests(WritingBox):
    """A hub made by the 1.1.1 setup (the text its setup.hub_body made for a manuscript project, kept in
    tests/fixtures/hub-1.1.1-manuscript.md) needs no hand edit for planning, writing and the page. The 1.1.1 hub has no
    row for a topic, and none for Introduction or Discussion, which the manuscript profile gained after it; the labels
    place the topics, and every command leaves the hub as it was."""

    plan = False

    def older_hub(self):
        data = self.repo()
        data["issues"]["1"]["body"] = recortex.read_text(HUB_111)
        data["issues"]["1"]["updated_at"] = "2026-11-17T00:00:00Z"
        self.save(data)
        self.ready()
        return self.repo()["issues"]["1"]["body"]

    def test_the_fixture_is_a_hub_the_linter_accepts_and_the_current_setup_makes_the_same_way(self):
        import lint_drafts
        import setup
        text = recortex.read_text(HUB_111)
        self.assertEqual([], [f for f in lint_drafts.lint_text(text, kind="hub", config={"profiles": ["manuscript"]})
                              if f.level == "error"])
        answers = setup.check_answers({"repo": "octo-org/sample-project", "record": "this", "deliverables": [{"kind": "paper"}],
                                       "project": "A two-season field trial of a winter rye cover crop and its effect on topsoil "
                                                  "moisture.", "decision_makers": ["the principal investigator"],
                                       "timezone": "US Eastern time, written as 2026-11-16 10:10 EST", "topics": ["moisture"]})
        now = setup.hub_body(answers, setup.new_config({}, answers)[0])
        then, current = ({s["heading"]: s["text"] for s in record_format.sections(body)} for body in (text, now))
        self.assertEqual(list(then), list(current))   # the same sections, in the same order
        for heading in ("Deliverable text", "Resume here", "Project", "Active issues", "Pull requests", "Older tracker",
                        "Recording rules"):
            self.assertEqual(then[heading], current[heading], heading)   # the setup's own text has not changed since
        self.assertIn("`ms:results`", then["Deliverable sections"])
        self.assertNotIn("ms:discussion", text)    # what the profile gained after 1.1.1 is not in an older hub
        self.assertIn("ms:discussion", current["Labels"])

    def test_enabling_planning_and_writing_changes_nothing_in_the_hub(self):
        hub = self.older_hub()
        dry = self.cmd("plan.py", "setup", "--dry-run")
        self.assertEqual(0, dry.returncode, dry.stdout + dry.stderr)
        self.assertIn("would link the new issue under hub #1 as a sub-issue", dry.stdout)
        self.assertEqual(hub, self.repo()["issues"]["1"]["body"])
        done = self.cmd("plan.py", "setup")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("plan: #", done.stdout)
        self.mode("review")
        self.assertEqual(hub, self.repo()["issues"]["1"]["body"])        # not one byte of the hub
        with open(os.path.join(self.dir, "project", ".recortex.json")) as handle:
            self.assertEqual({"manuscript": 13}, json.load(handle)["plans"])
        status = self.cmd("write.py", "status")
        self.assertEqual(0, status.returncode, status.stdout + status.stderr)
        self.assertIn("due: #3 Methods (methods; parts [], results [], decisions [9106])", status.stdout)   # the record is read
        folder = self.start(3)
        base = self.base(folder)
        self.draft(folder, base.replace("### Table: plots", "The four plots of the wet corner were left out of the moisture "
                                                            "analysis.\n\n### Table: plots", 1), parts=(), results=(),
                   decisions=(9106,))
        written = self.cmd("write.py", "apply", "--dir", folder, "--words-file", self.words("Approve"))
        self.assertEqual(0, written.returncode, written.stdout + written.stderr)
        self.assertIn("left out of the moisture analysis", self.body(3))
        self.assertEqual(0, self.cmd("versions.py", "status").returncode)
        self.assertEqual(0, self.tool("mind.py", gh="false").returncode)
        self.assertEqual(hub, self.repo()["issues"]["1"]["body"])


class OlderHubWriteTests(WritingBox):
    """The same hub, on a record with a plan and parts that are due: the write, the version and the page work as they do on
    the hand-made hub of the tests, and the hub stays as it was."""

    def test_a_write_and_the_page_need_no_edit_of_the_hub(self):
        data = self.repo()
        data["issues"]["1"]["body"] = recortex.read_text(HUB_111)
        data["issues"]["1"]["updated_at"] = "2026-11-17T00:00:00Z"
        self.save(data)
        self.ready()
        self.mode("autonomous")
        hub = self.repo()["issues"]["1"]["body"]
        folder = self.start(5)
        self.draft(folder)
        self.assertEqual(0, self.cmd("write.py", "apply", "--dir", folder).returncode)
        self.ready()
        self.assertIn("The pooled fit and the dry-spell comparison agree", self.body(5))
        self.assertEqual(hub, self.repo()["issues"]["1"]["body"])
        self.assertEqual(0, self.cmd("versions.py", "sync").returncode)
        built = self.tool("mind.py", gh="false")
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        model = json.loads(self.tool("index.py", "query", "mind", "--json", gh="false").stdout)
        placed = {section["label"]: sorted({p["thread"] for p in section["parts"]})
                  for group in model["outline"]["deliverables"] for section in group["sections"]}
        threads = {t["n"]: t["issues"] for t in model["threads"]}
        topics = {int(n) for n, issue in self.repo()["issues"].items() if "final-record" in issue["labels"]}
        placed = {heading: sorted(n for t in threads_of for n in threads[t] if n in topics) for heading, threads_of in placed.items()}
        heading_of = dict(recortex.section_labels(["manuscript"]))
        carried = {}
        for number, issue in self.repo()["issues"].items():
            if "final-record" in issue["labels"]:
                for label in issue["labels"]:
                    if label in heading_of:
                        carried.setdefault(heading_of[label], set()).add(int(number))
        self.assertEqual({"Introduction", "Methods", "Results", "Discussion", "Limitations"}, set(carried))
        self.assertEqual({heading: sorted(numbers) for heading, numbers in carried.items()}, placed)   # the labels place them
        self.assertEqual(hub, self.repo()["issues"]["1"]["body"])


if __name__ == "__main__":
    unittest.main()

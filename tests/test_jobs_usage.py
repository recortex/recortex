"""Tests of the usage view (1.5.0): usage.db, the subagent-stop hook, the injected-characters count, `usage.py show` and
the two views of the mind page. Nothing here reaches GitHub; the scripts run against tests/fake_gh.py."""

import html
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402
from observer_source_fixture import fixture_trusted_ancestors  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import hooks  # noqa: E402
import observer_sources  # noqa: E402
import recortex  # noqa: E402
import storage  # noqa: E402
import usage  # noqa: E402

TRANSCRIPT = os.path.join(ROOT, "tests", "fixtures", "subagent-transcript.jsonl")
SESSION = "5b0c7f1e-2d44-4c1a-9e53-0a6d1f8b7c20"
AGENT_ID = "a3f19c7b2e8d4a05c"
DAY = 86400
NOW = 1794945600   # 2026-11-17 20:00 UTC


class UsageBox(ts.RecordSandbox):
    """The example record in a fake repository, a configured and trusted project folder, and the state in this test's own
    folder (in-process, as the scripts see it)."""

    def setUp(self):
        super().setUp()
        self.native = tempfile.mkdtemp(prefix="usage-native-", dir="/tmp")
        os.chmod(self.native, 0o700)
        self.addCleanup(shutil.rmtree, self.native, True)
        ancestors = fixture_trusted_ancestors(self.native)
        ancestors.__enter__()
        self.addCleanup(ancestors.__exit__, None, None, None)
        self.in_process({"CLAUDE_CONFIG_DIR": self.native, "CLAUDE_PROJECT_DIR": self.project})
        self.env["CLAUDE_CONFIG_DIR"] = self.native

    def rows(self, table):
        path = usage.db_path(ts.REPO)
        if not path or not os.path.exists(path):
            return []
        db = sqlite3.connect(path)
        try:
            return db.execute("SELECT * FROM %s ORDER BY rowid" % table).fetchall()
        finally:
            db.close()

    def state_folder(self):
        return storage.folder(storage.state_top(), storage.repo_folder(ts.REPO))


class UsageFileTests(UsageBox):
    def test_a_subagent_run_keeps_four_whole_numbers_and_a_model_name(self):
        self.assertTrue(usage.note_agent(ts.REPO, "recortex:record-auditor", "claude-haiku-5-5",
                                         {"input_tokens": 5, "output_tokens": 201, "cache_read": 35003,
                                          "cache_write": 1200}, now=NOW))
        self.assertEqual([(NOW, "record-auditor", "claude-haiku-5-5", 5, 201, 35003, 1200)], self.rows("agents"))

    def test_numbers_that_are_not_whole_numbers_become_zero_or_are_clamped(self):
        usage.note_agent(ts.REPO, "recortex:ask", "x<img src=x onerror=1>",
                         {"input_tokens": "9", "output_tokens": -5, "cache_read": 3.5, "cache_write": 10 ** 12}, now=NOW)
        usage.note_agent(ts.REPO, "recortex:ask", "claude-haiku-5-5",
                         {"input_tokens": True, "output_tokens": None, "cache_read": [1], "cache_write": {"a": 1}}, now=NOW)
        self.assertEqual([(NOW, "ask", "", 0, 0, 0, 10 ** 9), (NOW, "ask", "claude-haiku-5-5", 0, 0, 0, 0)],
                         self.rows("agents"))

    def test_an_agent_that_is_not_on_the_list_writes_nothing(self):
        for agent in ("recortex:other", "recortex:record-auditor; DROP TABLE agents", "other:ask", "", None, 7,
                      "recortex:ask-more", "xrecortex:ask"):
            self.assertFalse(usage.note_agent(ts.REPO, agent, "m", {"input_tokens": 1}), agent)
        self.assertEqual([], self.rows("agents"))

    def test_injected_characters_add_up_by_day_and_event(self):
        for characters in (120, 80):
            self.assertTrue(usage.note_injected(ts.REPO, "prompt-submit", characters, now=NOW))
        self.assertTrue(usage.note_injected(ts.REPO, "session-start", 3000, now=NOW))
        self.assertTrue(usage.note_injected(ts.REPO, "prompt-submit", 50, now=NOW + DAY))
        self.assertEqual([("2026-11-17", "prompt-submit", 200, 2), ("2026-11-17", "session-start", 3000, 1),
                          ("2026-11-18", "prompt-submit", 50, 1)], sorted(self.rows("injected")))

    def test_an_event_off_the_list_or_a_bad_count_writes_nothing(self):
        for event, count in (("pre-tool", 10), ("session-start; x", 10), ("session-start", 0), ("session-start", -3),
                             ("session-start", True), ("session-start", "40"), ("session-start", 2.5), (None, 5)):
            self.assertFalse(usage.note_injected(ts.REPO, event, count), (event, count))
        self.assertEqual([], self.rows("injected"))

    def test_the_file_is_private_in_a_private_folder(self):
        usage.note_injected(ts.REPO, "session-start", 10)
        folder = self.state_folder()
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(folder).st_mode))
        self.assertEqual(0o600, stat.S_IMODE(os.lstat(os.path.join(folder, "usage.db")).st_mode))

    def test_a_file_with_loose_permissions_is_not_used(self):
        usage.note_injected(ts.REPO, "session-start", 10)
        path = os.path.join(self.state_folder(), "usage.db")
        os.chmod(path, 0o644)
        self.assertFalse(usage.note_injected(ts.REPO, "session-start", 10))
        self.assertIn("refused", usage.problem(ts.REPO))
        os.chmod(path, 0o600)
        self.assertEqual("", usage.problem(ts.REPO))

    def test_a_symlink_is_not_followed(self):
        folder = self.state_folder()
        target = os.path.join(folder, "elsewhere.db")
        sqlite3.connect(target).close()
        os.chmod(target, 0o600)
        os.symlink(target, os.path.join(folder, "usage.db"))
        self.assertFalse(usage.note_agent(ts.REPO, "recortex:ask", "m", {"input_tokens": 1}))
        self.assertFalse(usage.note_injected(ts.REPO, "session-start", 10))
        self.assertIn("refused", usage.problem(ts.REPO))
        db = sqlite3.connect(target)
        try:
            self.assertEqual([], db.execute("SELECT name FROM sqlite_master").fetchall())
        finally:
            db.close()

    def test_a_locked_file_makes_a_writer_give_up_in_time(self):
        usage.note_injected(ts.REPO, "session-start", 10)
        holder = sqlite3.connect(os.path.join(self.state_folder(), "usage.db"), isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        self.addCleanup(holder.close)
        started = time.monotonic()
        self.assertFalse(usage.note_injected(ts.REPO, "prompt-submit", 10))
        self.assertFalse(usage.note_agent(ts.REPO, "recortex:ask", "m", {"input_tokens": 1}))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual("locked or not writable", usage.problem(ts.REPO))
        holder.execute("ROLLBACK")
        self.assertEqual("", usage.problem(ts.REPO))

    def test_the_problem_line_changes_nothing(self):
        self.assertEqual("", usage.problem(ts.REPO))
        folder = storage.folder(storage.state_top(), storage.repo_folder(ts.REPO), create=False)
        self.assertTrue(folder is None or not os.path.exists(os.path.join(folder, "usage.db")))

    def test_rows_older_than_35_days_are_removed(self):
        usage.note_agent(ts.REPO, "recortex:ask", "m", {"input_tokens": 1}, now=NOW - 36 * DAY)
        usage.note_agent(ts.REPO, "recortex:ask", "m", {"input_tokens": 2}, now=NOW - 34 * DAY)
        usage.note_injected(ts.REPO, "session-start", 10, now=NOW - 36 * DAY)
        usage.note_injected(ts.REPO, "session-start", 20, now=NOW - 34 * DAY)
        self.assertEqual(2, usage.cleanup(ts.REPO, now=NOW))
        self.assertEqual([2], [row[3] for row in self.rows("agents")])
        self.assertEqual([20], [row[2] for row in self.rows("injected")])

    def test_cleanup_with_no_file_does_nothing(self):
        self.assertEqual(0, usage.cleanup(ts.REPO, now=NOW))

    def test_the_database_holds_no_text_but_a_fixed_name_a_model_and_a_day(self):
        usage.note_agent(ts.REPO, "recortex:record-drafter", "claude-haiku-5-5", {"input_tokens": 1}, now=NOW)
        usage.note_injected(ts.REPO, "prompt-submit", 5, now=NOW)
        for table in ("agents", "injected"):
            for row in self.rows(table):
                for value in row:
                    self.assertTrue(isinstance(value, int) or re.fullmatch(r"[A-Za-z0-9_.:-]+", value), value)


def store_row(day, tier, job, calls, tokens_in, tokens_out, model="claude-haiku-5-5"):
    return {"day": day, "tier": tier, "job": job, "model": model, "calls": calls, "input_tokens": tokens_in,
            "output_tokens": tokens_out}


class ReportTests(UsageBox):
    def rows_of_the_store(self):
        return [store_row("2026-11-17", "sort", "sort", 12, 80000, 24000),
                store_row("2026-11-17", "middle", "memories", 5, 50000, 9000, "claude-sonnet-5-5"),
                store_row("2026-11-17", "middle", "status", 1, 3000, 700, "claude-sonnet-5-5"),
                store_row("2026-11-14", "sort", "sort", 6, 40000, 12000),
                store_row("2026-11-12", "strong", "paper", 1, 40000, 9000, "claude-opus-5-5"),
                store_row("2026-10-20", "sort", "sort", 9, 60000, 18000)]

    def report(self, rows=None, today=None):
        return usage.report(ts.REPO, ts.EXAMPLE_CONFIG, now=NOW, rows=self.rows_of_the_store() if rows is None else rows,
                            today=today if today is not None else {"sort": {"calls": 12, "tokens": 104000},
                                                                   "middle": {"calls": 6, "tokens": 62700}})

    def test_sums_by_tier_job_and_day(self):
        data = self.report()
        seven = {(row["tier"], row["job"]): (row["calls"], row["input"], row["output"]) for row in data["periods"]["7"]}
        self.assertEqual({("sort", "sort"): (18, 120000, 36000), ("middle", "memories"): (5, 50000, 9000),
                          ("middle", "status"): (1, 3000, 700), ("strong", "paper"): (1, 40000, 9000)}, seven)
        thirty = {(row["tier"], row["job"]): row["calls"] for row in data["periods"]["30"]}
        self.assertEqual(27, thirty[("sort", "sort")])
        self.assertEqual(30, len(data["days"]))
        self.assertEqual("2026-11-17", data["days"][-1]["day"])
        self.assertEqual("2026-10-19", data["days"][0]["day"])
        last = data["days"][-1]
        self.assertEqual((104000, 62700, 0), (last["sort"], last["middle"], last["strong"]))
        self.assertEqual(49000, data["days"][-6]["strong"])

    def test_the_five_largest_of_the_week_include_subagents(self):
        usage.note_agent(ts.REPO, "recortex:record-reviewer", "claude-sonnet-5-5",
                         {"input_tokens": 500, "output_tokens": 100, "cache_read": 100000, "cache_write": 2000}, now=NOW - DAY)
        data = self.report()
        names = [(entry["kind"], entry["name"]) for entry in data["largest"]]
        self.assertEqual(("observer", "sort sort"), names[0])
        self.assertEqual(("agent", "record-reviewer"), names[1])
        self.assertLessEqual(len(names), 5)
        self.assertEqual(sorted((entry["tokens"] for entry in data["largest"]), reverse=True),
                         [entry["tokens"] for entry in data["largest"]])

    def test_subagents_by_agent_for_7_and_30_days(self):
        usage.note_agent(ts.REPO, "recortex:ask", "m", {"input_tokens": 10, "output_tokens": 5, "cache_read": 100}, now=NOW - DAY)
        usage.note_agent(ts.REPO, "recortex:ask", "m", {"input_tokens": 1, "output_tokens": 1}, now=NOW - 20 * DAY)
        data = self.report()
        self.assertEqual([{"agent": "ask", "runs": 1, "input": 110, "output": 5}], data["agents"]["7"])
        self.assertEqual([{"agent": "ask", "runs": 2, "input": 111, "output": 6}], data["agents"]["30"])

    def test_the_injected_estimate_is_characters_over_four(self):
        usage.note_injected(ts.REPO, "session-start", 4001, now=NOW)
        usage.note_injected(ts.REPO, "prompt-submit", 400, now=NOW - 2 * DAY)
        usage.note_injected(ts.REPO, "prompt-submit", 400, now=NOW - 20 * DAY)
        data = self.report()
        self.assertEqual([{"event": "session-start", "characters": 4001, "count": 1, "tokens": 1000},
                          {"event": "prompt-submit", "characters": 400, "count": 1, "tokens": 100}], data["estimated"])

    def test_today_names_each_tier_with_its_cap_and_budget(self):
        data = self.report()
        sort = data["tiers"][0]
        self.assertEqual(("sort", 12, 104000), (sort["tier"], sort["calls"], sort["tokens"]))
        self.assertEqual(["sort", "middle", "strong"], [entry["tier"] for entry in data["tiers"]])
        self.assertEqual((80, 800000), (sort["cap"], sort["budget"]))
        self.assertEqual((70, 700000), (data["tiers"][1]["cap"], data["tiers"][1]["budget"]))
        self.assertEqual((8, 320000), (data["tiers"][2]["cap"], data["tiers"][2]["budget"]))

    def test_hostile_rows_are_dropped_and_hostile_numbers_clamped(self):
        rows = [store_row("2026-11-17", "sort", "sort", 1, 10, 10, "<script>alert(1)</script>"),
                store_row("2026-11-17", "weird", "sort", 1, 10, 10),
                store_row("2026-11-17", "sort", "<b>x</b>", 1, 10, 10),
                store_row("not a day", "sort", "sort", 1, 10, 10),
                store_row("2026-11-17", "sort", "sort", "7", -3, 10 ** 15),
                "not a row", None]
        data = self.report(rows=rows)
        seven = data["periods"]["7"]
        self.assertEqual([{"tier": "sort", "job": "sort", "calls": 1, "input": 10, "output": 10 + 10 ** 9}], seven)
        self.assertEqual("claude-haiku-5-5", data["tiers"][0]["model"])   # the hostile name was not taken
        text = json.dumps(data)
        for planted in ("<script>", "<b>", "weird", "not a day"):
            self.assertNotIn(planted, text)

    def test_the_stores_calls_are_summed_by_day_tier_job_and_model(self):
        calls = [{"at": NOW - 100, "tier": "sort", "job": "sort", "model": "claude-haiku-5-5", "success": True,
                  "input_tokens": 100, "output_tokens": 10, "cache_read": 60, "cache_write": 0},
                 {"at": NOW - 50, "tier": "sort", "job": "sort", "model": "claude-haiku-5-5", "success": False,
                  "input_tokens": 50, "output_tokens": 0, "cache_read": 0, "cache_write": 0},
                 {"at": NOW - DAY, "tier": "middle", "job": "memories", "model": "", "success": True,
                  "input_tokens": 7, "output_tokens": 3, "cache_read": 0, "cache_write": 0},
                 {"at": NOW, "tier": "other", "job": "sort", "model": "x", "input_tokens": 1, "output_tokens": 1},
                 {"at": NOW, "tier": "sort", "job": "weird", "model": "x", "input_tokens": 1, "output_tokens": 1},
                 {"at": "soon", "tier": "sort", "job": "sort", "model": "x", "input_tokens": 1, "output_tokens": 1},
                 {"at": NOW, "tier": "sort", "job": "sort", "model": "<b>", "input_tokens": "9", "output_tokens": -1},
                 "not a call"]
        self.assertEqual([store_row("2026-11-16", "middle", "memories", 1, 7, 3, ""),
                          store_row("2026-11-17", "sort", "sort", 1, 0, 0, ""),
                          store_row("2026-11-17", "sort", "sort", 2, 150, 10)],
                         sorted(usage.aggregate(calls), key=lambda row: (row["day"], row["model"])))

    def test_the_numbers_come_from_the_observers_own_store(self):
        import observer_store
        with observer_store.Store(ts.REPO) as store:
            first = store.reserve_invocation("sort")
            store.complete_invocation(first, True, usage={"input_tokens": 10, "cache_creation_input_tokens": 5,
                                                          "cache_read_input_tokens": 100, "output_tokens": 20},
                                      model="claude-haiku-5-5")
            second = store.reserve_invocation("draft")
            store.complete_invocation(second, True, usage={"input_tokens": 1, "output_tokens": 2}, model="claude-sonnet-5-5")
        data = usage.report(ts.REPO, ts.EXAMPLE_CONFIG)
        today = {entry["tier"]: (entry["calls"], entry["tokens"]) for entry in data["tiers"]}
        self.assertEqual({"sort": (1, 135), "middle": (1, 3), "strong": (0, 0)}, today)
        week = {(row["tier"], row["job"]): (row["calls"], row["input"], row["output"]) for row in data["periods"]["7"]}
        self.assertEqual({("sort", "sort"): (1, 115, 20), ("middle", "memories"): (1, 1, 2)}, week)
        self.assertFalse(data["empty"])
        self.assertEqual(["claude-haiku-5-5", "claude-sonnet-5-5"], [e["model"] for e in data["tiers"][:2]])

    def test_with_no_data_the_report_says_so(self):
        data = usage.report(ts.REPO, ts.EXAMPLE_CONFIG, now=NOW, rows=[], today={})
        self.assertTrue(data["empty"])
        self.assertEqual("off", data["observer"])
        self.assertEqual({"7": [], "30": []}, data["periods"])
        self.assertEqual("2026-11-17 20:00 UTC", data["built"])

    def test_the_report_holds_no_money(self):
        text = json.dumps(self.report()).lower() + "\n".join(usage.lines(self.report())).lower()
        for word in ("$", "dollar", "usd", "price", "cost"):
            if word == "price":
                continue   # the first line says that no price applies
            self.assertNotIn(word, text)

    def test_the_terminal_lines_tell_the_same_numbers(self):
        out = "\n".join(usage.lines(self.report()))
        self.assertIn("Tokens and calls, not money", out)
        self.assertIn("18 calls, 120000 input tokens, 36000 output tokens", out)
        self.assertIn("Not measured:", out)

    def test_show_runs_from_the_terminal_with_gh_off(self):
        self.ready()
        shown = self.tool("usage.py", "show", gh="false")
        self.assertEqual(0, shown.returncode, shown.stdout + shown.stderr)
        self.assertIn("Tokens and calls, not money", shown.stdout)
        self.assertIn("The observer is off here", shown.stdout)
        as_json = self.tool("usage.py", "show", "--json", "--days", "30", gh="false")
        self.assertEqual(0, as_json.returncode, as_json.stdout + as_json.stderr)
        self.assertEqual({"agents", "built", "days", "empty", "estimated", "largest", "observer", "periods", "tiers"},
                         set(json.loads(as_json.stdout)))


class SubagentBox(UsageBox):
    """A native transcript of a subagent in this project's own folder under the Claude configuration folder."""

    def setUp(self):
        super().setUp()
        self.folder = observer_sources.project_directory(self.project)
        os.makedirs(os.path.join(self.folder, SESSION, "subagents"), mode=0o700)
        for path in (os.path.join(self.native, "projects"), self.folder, os.path.join(self.folder, SESSION),
                     os.path.join(self.folder, SESSION, "subagents")):
            os.chmod(path, 0o700)
        self.path = os.path.join(self.folder, SESSION, "subagents", "agent-%s.jsonl" % AGENT_ID)

    def install(self, lines=None, mode=0o600):
        with open(TRANSCRIPT, encoding="utf-8") as handle:
            text = handle.read() if lines is None else "".join(line for line in handle.readlines()[:lines])
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(self.path, mode)

    def payload(self, **changes):
        data = {"session_id": SESSION, "transcript_path": "unused", "cwd": self.project, "hook_event_name": "SubagentStop",
                "agent_id": AGENT_ID, "agent_type": "recortex:record-auditor", "agent_transcript_path": self.path,
                "stop_hook_active": False, "last_assistant_message": "ready"}
        data.update(changes)
        return data

    def stop(self, **changes):
        hooks.subagent_stop(self.payload(**changes))


class SubagentStopTests(SubagentBox):
    def test_a_stop_of_a_listed_agent_keeps_one_row_summed_once_per_message(self):
        self.install()
        self.stop()
        rows = self.rows("agents")
        self.assertEqual(1, len(rows))
        self.assertEqual(("record-auditor", "claude-haiku-5-5", 5, 201, 35003, 1200), rows[0][1:])

    def test_every_agent_of_the_list_is_kept_and_nothing_else(self):
        self.install()
        for name in ("record-auditor", "record-drafter", "record-reviewer", "record-planner", "record-planner-session", "ask"):
            self.stop(agent_type="recortex:" + name)
        self.assertEqual(6, len(self.rows("agents")))
        for name in ("ask", "other:ask", "recortex:", "recortex:record-auditor2", "Recortex:ask", "recortex:ask\n", None, 5):
            self.stop(agent_type=name)
        self.assertEqual(6, len(self.rows("agents")))

    def test_an_agent_off_the_list_is_never_read(self):
        self.install()
        with mock.patch.object(usage, "measure", side_effect=AssertionError("read")) as measured:
            self.stop(agent_type="other:ask")
            self.stop(agent_type="ask")
        measured.assert_not_called()
        self.assertEqual([], self.rows("agents"))

    def test_a_path_outside_this_projects_native_folder_is_refused(self):
        self.install()
        elsewhere = tempfile.mkdtemp(prefix="usage-else-", dir="/tmp")
        os.chmod(elsewhere, 0o700)
        self.addCleanup(shutil.rmtree, elsewhere, True)
        other = os.path.join(elsewhere, "agent-x.jsonl")
        shutil.copy(self.path, other)
        os.chmod(other, 0o600)
        sibling = os.path.join(self.native, "projects", "-some-other-project", SESSION, "subagents")
        os.makedirs(sibling, mode=0o700)
        copied = os.path.join(sibling, "agent-%s.jsonl" % AGENT_ID)
        shutil.copy(self.path, copied)
        os.chmod(copied, 0o600)
        with mock.patch.object(usage, "measure", side_effect=AssertionError("read")) as measured:
            for path in (other, copied, self.path + "/../agent-x.jsonl", "relative/agent-x.jsonl", None, 7,
                         os.path.join(self.folder, SESSION + ".jsonl.extra")):
                self.stop(agent_transcript_path=path)
            self.stop(session_id="another-session")   # the path names this session, the hook another one
            self.stop(session_id="bad id/..")
        measured.assert_not_called()
        self.assertEqual([], self.rows("agents"))

    def test_a_symlink_is_refused(self):
        self.install()
        real = os.path.join(self.folder, SESSION, "subagents", "agent-real.jsonl")
        os.rename(self.path, real)
        os.symlink(real, self.path)
        with mock.patch.object(usage, "measure", side_effect=AssertionError("read")) as measured:
            self.stop()
        measured.assert_not_called()
        self.assertEqual([], self.rows("agents"))

    def test_a_file_another_user_owns_is_refused(self):
        self.install()
        original = os.lstat
        foreign = max(os.getuid(), 1) + 10000

        def inspect(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            if os.fspath(candidate) == self.path:
                values = {key: getattr(info, key) for key in ("st_mode", "st_dev", "st_ino", "st_size")}
                from types import SimpleNamespace
                return SimpleNamespace(st_uid=foreign, st_gid=info.st_gid, **values)
            return info

        with mock.patch.object(observer_sources.os, "lstat", side_effect=inspect):
            self.stop()
        self.assertEqual([], self.rows("agents"))

    def test_a_transcript_other_users_can_write_is_refused(self):
        self.install(mode=0o666)
        self.stop()
        self.assertEqual([], self.rows("agents"))

    def test_only_integers_and_a_model_name_leave_the_transcript(self):
        with open(TRANSCRIPT, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle]
        for record in records:
            message = record.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                message["model"] = "<img src=x onerror=alert(1)>"
                message["content"] = [{"type": "text", "text": "CANARY-TEXT-OF-THE-SUBAGENT"}]
                message["usage"]["input_tokens"] = "12"
                message["usage"]["note"] = "CANARY-USAGE-TEXT"
        with open(self.path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        os.chmod(self.path, 0o600)
        self.stop()
        rows = self.rows("agents")
        self.assertEqual(1, len(rows))
        self.assertEqual(("record-auditor", "", 0, 201, 35003, 1200), rows[0][1:])
        with open(os.path.join(self.state_folder(), "usage.db"), "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"CANARY", raw)
        self.assertNotIn(b"onerror", raw)

    def test_a_record_that_is_not_json_or_not_an_assistant_is_skipped(self):
        self.install()
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("not json at all\n[1, 2]\n{\"type\": \"assistant\", \"message\": 5}\n")
        self.stop()
        self.assertEqual(("record-auditor", "claude-haiku-5-5", 5, 201, 35003, 1200), self.rows("agents")[0][1:])

    def test_the_hook_waits_for_a_transcript_that_fills_up_later(self):
        self.install(lines=6)    # the records before the first assistant record, as at the moment the hook fires
        timer = threading.Timer(0.35, self.install)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        self.stop()
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 1.5)
        self.assertEqual(1, len(self.rows("agents")))
        self.assertEqual(5, self.rows("agents")[0][3])

    def test_a_transcript_that_never_fills_up_writes_no_row_and_returns_in_time(self):
        self.install(lines=6)
        started = time.monotonic()
        self.stop()
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 1.4)
        self.assertLess(waited, 2.5)
        self.assertEqual([], self.rows("agents"))

    def test_an_assistant_record_with_no_stop_reason_is_not_yet_complete(self):
        with open(TRANSCRIPT, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        partial = []
        for line in lines:
            record = json.loads(line)
            if record.get("type") == "assistant":
                record["message"]["stop_reason"] = None
            partial.append(json.dumps(record))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(partial) + "\n")
        os.chmod(self.path, 0o600)
        with mock.patch.object(usage, "WAIT_FOR_TRANSCRIPT", 0.3):
            self.stop()
        self.assertEqual([], self.rows("agents"))

    def test_the_tail_is_read_from_the_end_and_the_cut_line_is_dropped(self):
        self.install()
        with open(self.path, "r", encoding="utf-8") as handle:
            text = handle.read()
        pad = ("x" * 1000 + "\n") * 40   # lines of no kind, before the real records
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(pad + text)
        os.chmod(self.path, 0o600)
        lines = usage.read_tail(self.path, limit=len(text.encode()) + 500)
        self.assertEqual(0, sum(1 for line in lines if line.startswith(b"x")))   # 500 bytes of the cut pad line: dropped
        total, model, complete = usage.sum_transcript(lines)
        self.assertTrue(complete)
        self.assertEqual(("claude-haiku-5-5", 5), (model, total["input_tokens"]))

    def test_nothing_is_done_when_the_repository_is_not_trusted(self):
        self.install()
        with open(self.trusted, "w") as handle:
            handle.write("")
        with mock.patch.object(usage, "measure", side_effect=AssertionError("read")) as measured:
            self.stop()
        measured.assert_not_called()
        self.assertEqual([], self.rows("agents"))

    def test_nothing_is_done_when_the_project_is_not_set_up(self):
        self.install()
        os.unlink(os.path.join(self.project, recortex.CONFIG_NAME))
        with mock.patch.object(usage, "measure", side_effect=AssertionError("read")) as measured:
            self.stop()
        measured.assert_not_called()

    @unittest.skipUnless(os.lstat("/tmp").st_uid == 0, "the native folder needs a /tmp that root owns, as the hook checks it")
    def test_a_locked_usage_file_makes_the_hook_give_up_in_time_and_print_nothing(self):
        self.install()
        usage.note_injected(ts.REPO, "session-start", 10)
        holder = sqlite3.connect(os.path.join(self.state_folder(), "usage.db"), isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        self.addCleanup(holder.close)
        started = time.monotonic()
        done = self.run_hook()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((0, ""), (done.returncode, done.stdout))
        holder.execute("ROLLBACK")
        self.assertEqual([], self.rows("agents"))

    @unittest.skipUnless(os.lstat("/tmp").st_uid == 0, "the native folder needs a /tmp that root owns, as the hook checks it")
    def test_the_hook_script_prints_nothing_and_keeps_the_row(self):
        self.install()
        done = self.run_hook()
        self.assertEqual((0, "", ""), (done.returncode, done.stdout, done.stderr))
        self.assertEqual(1, len(self.rows("agents")))

    def run_hook(self, **changes):
        full_env = ts.environment_for(os.environ, {"RECORTEX_GH": "false", "PYTHONDONTWRITEBYTECODE": "1"})
        full_env.update(XDG_CACHE_HOME=os.path.join(self.dir, "xdg-cache"), XDG_STATE_HOME=os.path.join(self.dir, "xdg-state"),
                        XDG_CONFIG_HOME=os.path.join(self.dir, "xdg-config"))
        full_env = ts.overlay(full_env, self.env)
        return subprocess.run(["bash", os.path.join(ts.SCRIPTS, "run_hook.sh"), "subagent-stop"],
                              input=json.dumps(self.payload(**changes)), capture_output=True, text=True, env=full_env,
                              cwd=self.project)


class RegistrationTests(unittest.TestCase):
    """hooks/hooks.json and the settings snippet register SubagentStop with an anchored matcher and the runner's time."""

    def entries(self, path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)["hooks"]["SubagentStop"]

    def test_the_matcher_is_anchored_and_names_the_plugins_subagents(self):
        for path in (os.path.join(ROOT, "hooks", "hooks.json"),
                     os.path.join(ts.TEMPLATES, "settings-hook-snippet.json")):
            entries = self.entries(path)
            self.assertEqual(1, len(entries))
            matcher = entries[0]["matcher"]
            self.assertTrue(matcher.startswith("^") and matcher.endswith("$"), matcher)
            for name in ("record-auditor", "record-drafter", "record-reviewer", "record-planner", "record-planner-session", "ask"):
                self.assertTrue(re.search(matcher, "recortex:" + name), name)
            for name in ("ask", "recortex:ask2", "other:record-auditor", "recortex:", "xrecortex:ask", "recortex:record-planner-x"):
                self.assertFalse(re.search(matcher, name), name)
            self.assertEqual(5, entries[0]["hooks"][0]["timeout"])
            self.assertIn("run_hook.sh", entries[0]["hooks"][0]["command"])
            self.assertTrue(entries[0]["hooks"][0]["command"].endswith("subagent-stop"))

    def test_the_hook_list_in_the_code_and_the_matcher_say_the_same(self):
        matcher = self.entries(os.path.join(ROOT, "hooks", "hooks.json"))[0]["matcher"]
        listed = set(re.search(r"\^recortex:\((.*)\)\$", matcher).group(1).split("|"))
        self.assertEqual(listed, set(usage.AGENTS))
        for name in listed:
            self.assertTrue(hooks.STOP_AGENTS.fullmatch("recortex:" + name))


class InjectedCountTests(UsageBox):
    def test_a_printed_session_start_is_counted_in_characters(self):
        self.ready()
        done = self.tool("hooks.py", "session-start", stdin="{}")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        rows = self.rows("injected")
        self.assertEqual(1, len(rows))
        self.assertEqual("session-start", rows[0][1])
        self.assertEqual(len(done.stdout.rstrip("\n")), rows[0][2])

    def test_a_subagent_start_counts_the_context_inside_its_json(self):
        self.ready()
        done = self.tool("hooks.py", "subagent-start", stdin="{}")
        context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        rows = self.rows("injected")
        self.assertEqual([("subagent-start", len(context))], [(row[1], row[2]) for row in rows])

    def test_the_payload_is_the_same_with_the_count(self):
        self.ready()
        first = self.tool("hooks.py", "pre-compact", stdin="{}")
        usage_db = os.path.join(self.state_folder(), "usage.db")
        os.unlink(usage_db)
        os.chmod(self.state_folder(), 0o500)
        try:
            second = self.tool("hooks.py", "pre-compact", stdin="{}")
        finally:
            os.chmod(self.state_folder(), 0o700)
        self.assertEqual(first.stdout, second.stdout)   # a count that cannot be written changes nothing the user sees
        self.assertEqual(0, second.returncode)

    def test_the_observers_context_is_counted_and_its_result_is_not_changed(self):
        import observer_hooks
        result = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "x" * 123}}
        self.assertIs(result, observer_hooks._counted("SessionStart", ts.REPO, result))
        observer_hooks._counted("PreToolUse", ts.REPO, {"hookSpecificOutput": {"permissionDecision": "deny"}})
        observer_hooks._counted("Stop", ts.REPO, {"hookSpecificOutput": {"additionalContext": "not counted"}})
        observer_hooks._counted("SessionStart", "not a repo", result)
        observer_hooks._counted("SessionStart", ts.REPO, {})
        observer_hooks._counted("SessionStart", ts.REPO, None)
        self.assertEqual([("observer-session-start", 123)], [(row[1], row[2]) for row in self.rows("injected")])

    def test_the_observers_handler_counts_what_it_adds(self):
        import observer_hooks
        with mock.patch.object(observer_hooks, "_handle", return_value={"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": "y" * 40}}):
            observer_hooks.handle("prompt-submit", {}, {"repo": ts.REPO}, ts.REPO)
        self.assertEqual([("observer-prompt-submit", 40)], [(row[1], row[2]) for row in self.rows("injected")])

    def test_the_doctor_says_whether_the_file_can_be_written(self):
        self.ready()
        done = self.tool("hooks.py", "--doctor")
        self.assertIn("usage: usage.db can be written", done.stdout)
        self.tool("hooks.py", "session-start", stdin="{}")
        path = os.path.join(self.state_folder(), "usage.db")
        os.chmod(path, 0o644)
        done = self.tool("hooks.py", "--doctor")
        self.assertIn("usage: usage.db cannot be written (refused (permissions))", done.stdout)
        self.assertNotIn(self.state_folder(), [line for line in done.stdout.splitlines() if line.startswith("usage:")][0])


# ---------------------------------------------------------------- the page

class UsagePageBox(ts.MindSandbox):
    """The example record's model, rendered with a usage report put into it, as mind.py puts one in."""

    def setUp(self):
        super().setUp()
        self.add_chain()
        self.ready()
        self.in_process()
        import mind
        self.mind = mind

    def model(self):
        result = self.tool("index.py", "query", "mind", "--json", gh="false")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def render(self, data="absent", model=None):
        model = model or self.model()
        if data != "absent":
            model["usage"] = data
        return self.mind.render(model)

    def view(self, text, ident="usage"):
        """The markup of one view alone: from its opening tag to the next view or the end of main."""
        start = text.index('<section class="view" id="%s"' % ident)
        stops = [text.find(marker, start + 10) for marker in ('<section class="view"', "</main>")]
        return text[start:min(stop for stop in stops if stop > 0)]

    def words(self, markup):
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def full_report():
    rows = [store_row("2026-11-17", "sort", "sort", 12, 80000, 24000),
            store_row("2026-11-17", "middle", "memories", 5, 50000, 9000, "claude-sonnet-5-5"),
            store_row("2026-11-17", "middle", "status", 1, 3000, 700, "claude-sonnet-5-5"),
            store_row("2026-11-14", "sort", "sort", 6, 40000, 12000),
            store_row("2026-11-12", "strong", "paper", 1, 40000, 9000, "claude-opus-5-5")]
    data = usage.report("octo-org/octo-repo", ts.EXAMPLE_CONFIG, now=NOW, rows=rows,
                        today={"sort": {"calls": 12, "tokens": 104000}, "middle": {"calls": 66, "tokens": 680000}})
    data["observer"] = "on"
    data["agents"] = {"7": [{"agent": "record-auditor", "runs": 2, "input": 90000, "output": 1200}],
                      "30": [{"agent": "record-auditor", "runs": 3, "input": 120000, "output": 1800},
                             {"agent": "ask", "runs": 1, "input": 9000, "output": 300}]}
    data["estimated"] = [{"event": "session-start", "characters": 40000, "count": 7, "tokens": 10000}]
    return data


class UsagePageTests(UsagePageBox):
    def test_the_view_with_numbers_keeps_the_contract_and_tells_them(self):
        text = self.render(full_report())
        self.assertEqual([], ts.page_problems(text))
        words = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", self.view(text))).split())
        for told in ("On this machine, as of 2026-11-17 20:00 UTC. Tokens and calls, not money: under a Claude "
                     "subscription these count against your usage limits, and no price applies.",
                     "Sort (claude-haiku-5-5)", "Middle (claude-sonnet-5-5)", "Strong (claude-opus-5-5)",
                     "12 of 80 calls", "104,000 of 800,000 tokens", "66 of 70 calls", "680,000 of 700,000 tokens",
                     "The last 7 days", "The last 30 days", "Sorting a stretch", "Drafts of the paper's text",
                     "Record auditor", "Ask", "The brief at session start: 7 times, about 10,000 tokens",
                     "Not measured", "is not measured here."):
            self.assertIn(told, words)
        self.assertNotIn("$", words)

    def test_usage_remains_a_side_bar_utility_after_search(self):
        text = self.render(full_report())
        entries = [dict(attrs)["data-nav"] for tag, attrs in ts.PageReader(text).elements
                   if tag == "a" and "data-nav" in dict(attrs)]
        self.assertNotIn("usage", entries)
        side = text[text.index('<nav class="side fold"'):text.index('</nav>')]
        self.assertIn('<a href="#usage">Recortex usage</a>', side)
        self.assertLess(side.index('href="#ask"'), side.index('href="#usage"'))

    def test_meters_are_drawn_from_whole_numbers_with_no_inline_style(self):
        text = self.render(full_report())
        view = self.view(text)
        self.assertNotIn("style=", view)
        self.assertIn('<rect class="mfg" width="15" height="4"/>', view)        # 12 of 80 calls
        self.assertIn('<rect class="mfg hot" width="94" height="4"/>', view)    # 66 of 70 calls
        self.assertIn('aria-hidden="true"', view)
        bars = re.findall(r'<rect class="bar-[a-z]+" x="([0-9]+)" y="([0-9]+)" width="8" height="([0-9]+)"/>', view)
        self.assertTrue(bars)
        for x, y, height in bars:
            self.assertLessEqual(int(y) + int(height), 60)
            self.assertLess(int(x), 300)

    def test_with_no_data_at_all_the_view_is_still_drawn_with_one_fixed_line(self):
        for absent in ("absent", None, {}, "nonsense", [], {"empty": True}):
            text = self.render(absent)
            self.assertEqual([], ts.page_problems(text), absent)
            view = self.view(text)
            self.assertIn("Nothing has been counted on this machine yet.", view)
            self.assertIn("Not measured", view)
            self.assertNotIn("<table", view)
        self.assertIn("as of an unknown time", self.view(self.render("absent")))

    def test_with_the_observer_off_only_the_hooks_and_the_subagents_show(self):
        data = full_report()
        data["observer"] = "off"
        view = self.view(self.render(data))
        self.assertIn("The observer is off here, so there are no background calls to count.", view)
        self.assertNotIn("Today", view)
        self.assertNotIn("The last 7 days", view)
        self.assertIn("The plugin&#x27;s subagents", view)   # the page escapes the apostrophe
        self.assertIn("Record auditor", view)
        data["observer"] = "paused"
        self.assertIn("paused here until you choose a level", self.view(self.render(data)))

    def test_hostile_values_arrive_as_text_or_are_dropped(self):
        evil = "ZZMARK<img src=x onerror=alert(1)>"
        data = full_report()
        data["built"] = evil
        data["tiers"][0]["model"] = evil
        data["tiers"][1].update(calls="12", cap=-5, tokens=10 ** 15, budget=True)
        data["tiers"][2]["tier"] = evil
        data["periods"]["7"] += [{"tier": evil, "job": "sort", "calls": 1, "input": 1, "output": 1},
                                 {"tier": "sort", "job": evil, "calls": 1, "input": 1, "output": 1}, "ZZMARK", None]
        data["days"][3]["day"] = evil
        data["days"][4]["sort"] = evil
        data["largest"] = [{"kind": "agent", "name": evil, "tokens": 5}, {"kind": "observer", "name": evil, "tokens": 5},
                           {"kind": "observer", "name": "sort sort extra", "tokens": 5}, {"kind": "x", "name": "ask", "tokens": 5}]
        data["agents"]["7"].append({"agent": evil, "runs": 1, "input": 1, "output": 1})
        data["estimated"].append({"event": evil, "characters": 1, "count": 1, "tokens": 1})
        data["observer"] = "on"
        text = self.render(data)
        self.assertEqual([], ts.page_problems(text, marker="ZZMARK"))
        self.assertNotIn("ZZMARK", text)
        self.assertNotIn("onerror", text)
        view = self.view(text)
        self.assertIn("as of an unknown time", view)
        self.assertIn("1,000,000,000 of 0 tokens", " ".join(re.sub(r"<[^>]+>", " ", view).split()))   # a huge number is clamped

    def test_a_number_that_is_not_a_whole_number_is_zero(self):
        data = full_report()
        data["tiers"][0].update(calls=3.7, cap="80", tokens=None, budget=[1])
        words = " ".join(re.sub(r"<[^>]+>", " ", self.view(self.render(data))).split())
        self.assertIn("0 of 0 calls", words)
        self.assertIn("0 of 0 tokens", words)

    def test_the_page_makes_no_request_and_uses_no_storage_for_the_view(self):
        text = self.render(full_report())
        self.assertEqual([], ts.page_problems(text))
        _, script, _ = self.mind.fixed_files()
        for word in ts.SCRIPT_BANNED:
            self.assertNotIn(word, script)
        self.assertNotIn("usage", script)      # the script is the one fixed file; it learns no number and no name of this view
        self.assertEqual(1, text.count("<script>"))

    def test_the_text_of_the_view_is_never_an_attribute_value(self):
        view = self.view(self.render(full_report()))
        values = re.findall(r'="([^"]*)"', view)
        for value in values:
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9 _.,:;#/()'+=?-]*", value), value)
        self.assertNotIn("claude-haiku-5-5", " ".join(values))   # the model name is in the text of the view, in no attribute

    def test_mind_py_puts_the_report_in_the_page(self):
        usage.note_agent(ts.REPO, "recortex:record-reviewer", "claude-sonnet-5-5",
                         {"input_tokens": 500, "output_tokens": 100, "cache_read": 4000, "cache_write": 0})
        usage.note_injected(ts.REPO, "session-start", 8000)
        built = self.tool("mind.py", gh="false")
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        page = recortex.read_text(os.path.join(self.data, "private", self.slug(), "mind.html"))
        self.assertEqual([], ts.page_problems(page))
        view = self.view(page)
        self.assertIn("The observer is off here", view)
        self.assertIn("Record reviewer", view)
        self.assertIn("The brief at session start: 1 time, about 2,000 tokens", " ".join(re.sub(r"<[^>]+>", " ", view).split()))


# ---------------------------------------------------------------- the to-do view

CHAIN = os.path.join(ts.EXAMPLE, "chain")


class TodoBox(UsagePageBox):
    """The example record's chain, the steps memory of the task #7 and a result on topic #5 that checks its first step."""

    def setUp(self):
        ts.MindSandbox.setUp(self)
        self.add_chain()
        self.put(9108, 7, recortex.read_text(os.path.join(CHAIN, "6-steps.md")))
        self.put(9109, 5, recortex.read_text(os.path.join(CHAIN, "7-result.md")))
        self.ready()
        self.in_process()
        import mind
        self.mind = mind

    def page(self):
        built = self.tool("mind.py", gh="false")
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        return recortex.read_text(os.path.join(self.data, "private", self.slug(), "mind.html"))


class TodoPageTests(TodoBox):
    def test_the_view_tells_a_task_with_its_checked_and_open_steps(self):
        page = self.page()
        self.assertEqual([], ts.page_problems(page))
        view = self.view(page, "todo")
        words = self.words(view)
        self.assertIn("What is there to do?", words)
        self.assertIn("Task: support newlines inside quoted fields 1 of 2 steps done Open it on GitHub", words)
        self.assertIn("Done Carry the quote state across physical lines", words)
        self.assertIn("Open Rerun the conformance set", words)
        self.assertIn("Marked by: the reader now carries an open quote from one line to the next", words)
        self.assertIn('class="step s-done"', view)
        self.assertIn('class="step s-open"', view)
        self.assertIn('data-m="9109"', view)               # the memory is on the page: the link opens it there
        self.assertIn("https://github.com/octo-org/octo-repo/issues/7", view)
        self.assertNotIn("style=", view)

    def test_work_is_primary_navigation_and_keeps_the_legacy_view(self):
        page = self.page()
        entries = [dict(attrs)["data-nav"] for tag, attrs in ts.PageReader(page).elements
                   if tag == "a" and "data-nav" in dict(attrs)]
        self.assertEqual(["now", "project-map", "work", "history", "record", "outputs"], entries)
        self.assertIn('<a href="#work" data-nav="work">Work</a>', page)
        self.assertIn('<section class="view" id="todo"', page)

    def test_hostile_step_text_title_and_plain_line_arrive_as_text(self):
        evil = 'ZZMARK<img src=x onerror=alert(1)> "quoted" & </li><script>x</script>'
        data = self.repo()
        data["issues"]["7"]["title"] = evil
        self.save(data)
        steps = recortex.read_text(os.path.join(CHAIN, "6-steps.md")).replace(
            "Carry the quote state across physical lines", "ZZMARK-step <img src=x onerror=alert(1)> &amp; \"q\"")
        self.put(9120, 7, steps.replace("2026-03-12T11:00", "2026-03-14T11:00").replace("2026-03-12 11:00", "2026-03-14 11:00"))
        result = recortex.read_text(os.path.join(CHAIN, "7-result.md")).replace(
            "the reader now carries an open quote", "ZZMARK-plain <b>bold</b> the reader now carries an open quote").replace(
            "done=9108:1", "done=9120:1")
        self.put(9121, 5, result.replace("2026-03-12T15:30", "2026-03-14T15:30").replace("2026-03-12 15:30", "2026-03-14 15:30"))
        self.ready()
        page = self.page()
        self.assertEqual([], ts.page_problems(page, marker="ZZMARK"))
        view = self.view(page, "todo")
        self.assertNotIn("<img", view)
        self.assertNotIn("<script", view)
        self.assertNotIn("onerror=alert(1)>", view.replace("&lt;img src=x onerror=alert(1)&gt;", ""))
        self.assertIn("ZZMARK-step", self.words(view))
        for value in re.findall(r'="([^"]*)"', view):
            self.assertNotIn("ZZMARK", value)

    def test_a_task_with_no_title_told_is_named_by_its_number_alone(self):
        model = self.model()
        next(task for task in model["tasks"]["open"] if task["issue"] == 7)["title"] = ""
        words = self.words(self.view(self.mind.render(model), "todo"))
        self.assertIn("A task whose title is not told here 1 of 2 steps done", words)

    def test_a_list_changed_by_hand_is_said_so(self):
        model = self.model()
        next(task for task in model["tasks"]["open"] if task["issue"] == 7)["hand"] = True
        self.assertIn("The list on GitHub was changed by hand", self.words(self.view(self.mind.render(model), "todo")))

    def test_with_no_task_the_view_says_so(self):
        model = self.model()
        for absent in ({}, None, "x", {"open": "x", "closed": 5}):
            model["tasks"] = absent
            text = self.mind.render(model)
            self.assertEqual([], ts.page_problems(text))
            self.assertIn("No task is open.", self.words(self.view(text, "todo")))

    def test_unavailable_work_stays_distinct_from_empty_work(self):
        model = self.model()
        model["tasks"] = {"available": False, "open": [], "closed": []}
        text = self.mind.render(model)
        for ident in ("now", "todo"):
            view = self.words(self.view(text, ident))
            self.assertIn("Work could not be read", view)
            self.assertNotIn("No task is open.", view)

    def test_overview_skips_malformed_tasks_and_unfinished_steps(self):
        model = self.model()
        model["last"] = None
        good = {"issue": 7, "todo": False, "steps": [None, "x", {"state": "open", "text": 5},
                {"state": "open", "text": "The valid next step", "comment": 9108, "ordinal": 2}]}
        model["tasks"] = {"open": [None, "x", {"issue": "7", "steps": good["steps"]},
                                     {"issue": True, "steps": good["steps"]}, {"issue": 6, "steps": "x"}, good]}
        text = self.mind.Page(model).now_view()
        self.assertIn("The valid next step", text)
        self.assertIn('href="#work-step-7-9108-2"', text)

    def test_resume_links_require_one_checked_numeric_task_and_whole_step(self):
        model = self.model()
        step = {"n": 2, "text": "The valid next step", "comment": 9108, "ordinal": 2}
        task = {"issue": 7, "steps": [None, "x", {}, dict(step, n="2"), step]}
        model["tasks"] = {"open": [None, "x", {"issue": "7"}, task], "closed": 5}
        page = self.mind.Page(model)
        row = {"task_refs": [7], "action_text": "Step 2: The valid next step"}
        self.assertIn('href="#work-step-7-9108-2"', page.resume_work_link(row, True))
        for invalid in (None, "x", {}, {"task_refs": "7"}, {"task_refs": [True, "7"]}):
            self.assertEqual("", page.resume_work_link(invalid, True))
        model["tasks"]["closed"] = [{"issue": 7}]
        self.assertEqual("", page.resume_work_link(row, True))
        model["tasks"]["closed"] = []
        model["tasks"]["available"] = False
        self.assertEqual("", page.resume_work_link(row, True))

    def test_a_task_closed_in_the_last_30_days_is_listed_with_its_day(self):
        import datetime
        day = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        data = self.repo()
        data["issues"]["7"].update(state="closed", closed_at=day + "T10:00:00Z", updated_at=day + "T10:00:00Z")
        self.save(data)
        self.ready()
        view = self.view(self.mind.render(self.model()), "todo")
        words = self.words(view)
        self.assertIn("Closed in the last 30 days", words)
        self.assertIn("1 of 2 steps done, closed on %s" % day, words)
        closed = view[view.index("Closed in the last 30 days"):]
        self.assertNotIn('class="step', closed)       # a closed task shows no steps, only its count
        self.assertNotIn("Carry the quote state", closed)

    def test_a_task_closed_longer_ago_is_not_listed(self):
        data = self.repo()
        data["issues"]["7"].update(state="closed", closed_at="2026-01-02T10:00:00Z", updated_at="2026-01-02T10:00:00Z")
        self.save(data)
        self.ready()
        self.assertNotIn("Closed in the last 30 days", self.words(self.view(self.mind.render(self.model()), "todo")))

    def test_hostile_values_in_the_model_are_dropped(self):
        evil = "ZZMARK<img src=x onerror=alert(1)>"
        model = self.model()
        task = next(task for task in model["tasks"]["open"] if task["issue"] == 7)
        task["steps"] += [{"n": 3, "text": evil, "state": evil, "by": None}, "ZZMARK", None,
                          {"n": 4, "text": "fine", "state": "done", "by": {"id": evil, "issue": evil, "plain": evil}},
                          {"n": 5, "text": "fine two", "state": "done", "by": {"id": 5 ** 30, "issue": evil, "plain": evil}}]
        task["counts"] = {"steps": evil, "done": 10 ** 15, "open": [1]}
        task["issue"] = evil
        model["tasks"]["closed"] = [{"issue": 8, "title": evil, "closed": evil, "counts": {}, "steps": []}]
        model["acts"] = [{"kind": evil, "day": "2026-11-17", "issue": 1, "comment": 1},
                         {"kind": "task_closed", "day": evil, "issue": 7, "comment": 0}, "x", None,
                         {"kind": "task_closed", "day": "2026-11-17", "issue": evil, "comment": evil, "undone": True}]
        text = self.mind.render(model)
        self.assertEqual([], ts.page_problems(text, marker="ZZMARK"))
        self.assertNotIn("<img", self.view(text, "todo"))

    def test_the_observers_acts_end_the_view_with_a_kind_a_day_and_a_link(self):
        model = self.model()
        model["acts"] = [{"kind": "task_closed", "day": "2026-11-17", "issue": 7, "comment": 0, "undone": False},
                         {"kind": "memory", "day": "2026-11-16", "issue": 5, "comment": 9109, "undone": True},
                         {"kind": "board", "day": "2026-11-16", "issue": 0, "comment": 0, "undone": False}]
        view = self.view(self.mind.render(model), "todo")
        words = self.words(view)
        self.assertIn("What the observer did in the last 7 days", words)
        self.assertIn("Closed a task, 2026-11-17 Open it on GitHub", words)
        self.assertIn("Posted a memory, 2026-11-16 (undone) Open it on GitHub", words)
        self.assertIn("Updated the board, 2026-11-16", words)
        self.assertIn("https://github.com/octo-org/octo-repo/issues/5#issuecomment-9109", view)
        self.assertTrue(words.index("Closed a task") < words.index("Posted a memory"))

    def test_the_act_kinds_are_the_stores(self):
        import observer_store
        self.assertEqual(tuple(observer_store.ACT_KINDS), usage.ACT_KINDS)
        self.assertEqual(set(observer_store.ACT_KINDS), set(self.mind.TODO_ACTS))

    def test_the_acts_come_from_the_observers_store(self):
        import observer_store
        with observer_store.Store(ts.REPO) as store:
            store.add_act("task_closed", issue=7, numbers=[2, 2])
            store.add_act("step_marked", issue=7, comment=9109, numbers=[1])
        found = usage.acts_report(ts.REPO)
        self.assertEqual(["step_marked", "task_closed"], sorted(act["kind"] for act in found))
        self.assertEqual({"day", "kind", "issue", "comment", "undone"}, set(found[0]))
        page = self.page()
        self.assertIn("Checked a step", self.words(self.view(page, "todo")))
        self.assertEqual([], ts.page_problems(page))


if __name__ == "__main__":
    unittest.main()

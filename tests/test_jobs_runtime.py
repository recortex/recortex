"""The 1.5.0 runtime: tiers and their models, one proof per model, the caps and token budgets, the ledger's new
columns and tables, and the approvals (form 2, the level, and the paused state of a 1.4.0 approval). Offline: the
model's process is a fake, the queue and the approvals are in a scratch state folder."""

import inspect
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "recortex" / "scripts"))
import names  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import observer_store as store  # noqa: E402
import storage  # noqa: E402

REPO = "octo-org/octo-repo"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
FINGERPRINT = "a" * 64
# What 1.4.0's `enable --yes` stored for a folder: its settings had none of the keys 1.5.0 added.
OLD_SETTINGS = {"enabled": True, "mode": "automatic", "publisher_id": "", "model": "sonnet", "max_invocations": 60,
                "window_hours": 24, "max_input_chars": 48000, "timeout_seconds": 90, "max_candidates": 4,
                "max_pending_bytes": 134217728, "max_pending_observations": 10000, "retention_days": 7,
                "sync_interval_seconds": 300, "max_posts": 25}


class Box(unittest.TestCase):
    """A scratch state folder (so the user key, the approvals and the queue are this test's own) and a fixed
    fingerprint of the claude program."""

    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="jobs-runtime-", dir=str(ROOT / ".tmp"))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"),
                                               "XDG_CACHE_HOME": str(self.base / "cache"),
                                               "XDG_CONFIG_HOME": str(self.base / "config")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        finger = mock.patch.object(runtime, "fingerprint", return_value=FINGERPRINT)
        finger.start()
        self.addCleanup(finger.stop)
        self.folder = str(self.base / "project")
        os.makedirs(self.folder)

    def proof(self, model, actual=None, **changes):
        """A signed runtime proof for `model`, as check_runtime makes it after a good smoke call."""
        body = {"schema": 1, "ok": True, "version": "2.1.327", "executable_fingerprint": FINGERPRINT,
                "structured_exact": True, "isolation_checked": True, "recursion_checked": True, "canary_absent": True,
                "isolation_version": 1, "requested_model": model, "actual_model": actual or ("claude-" + model),
                "usage": {}}
        body.update(changes)
        body["mark"] = runtime._proof_mark(body, create=True)
        return body

    def options(self, **settings):
        raw = {"enabled": True, "mode": "automatic", "level": "everything", "model": "sonnet",
               "publisher_id": runtime.publisher_id(create=True)}
        raw.update(settings)
        return runtime.settings({"observer": raw})

    def gated(self, options, sort=None, middle=None, strong=None):
        """`options` with the proofs of the tiers beside them, as the worker passes them to invoke."""
        out = dict(options)
        out[runtime.gate_control("sort", options)] = sort
        out["runtime_gate"] = middle
        out["runtime_gate_strong"] = strong
        return out


def fake_output(model, usage=None):
    rows = [{"type": "system", "subtype": "init", "tools": ["StructuredOutput"], "mcp_servers": [], "skills": [],
             "plugins": [], "model": model},
            {"type": "result", "is_error": False, "structured_output": {"ok": True},
             "usage": usage or {"input_tokens": 3, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7,
                                "output_tokens": 11}}]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


class TierTests(Box):
    def test_every_purpose_with_a_prompt_has_a_tier_and_the_tiers_are_fixed(self):
        self.assertEqual(("sort", "middle", "strong"), runtime.TIERS)
        self.assertEqual({"sort": "sort", "draft": "middle", "review": "middle", "summary": "middle",
                          "summary_review": "middle", "text": "strong", "text_review": "strong",
                          "map_draft": "middle", "map_review": "middle"},
                         runtime.TIER_OF)
        self.assertEqual(set(runtime.PROMPTS) - {"smoke"}, set(runtime.TIER_OF))
        self.assertEqual({"sort": 32000, "middle": 48000, "strong": 120000}, runtime.INPUT_CAPS)

    def test_no_caller_passes_a_model_and_no_setting_moves_a_purpose_to_another_tier(self):
        self.assertEqual(["payload", "schema", "purpose", "config", "tier"],
                         list(inspect.signature(runtime.invoke).parameters))
        self.assertFalse([key for key in runtime.DEFAULTS if "purpose" in key or "tier" in key])
        with self.assertRaises(ValueError):
            runtime.settings({"observer": {"draft_model": "haiku"}})   # no key moves drafting to the sort model

    def test_the_models_of_the_tiers(self):
        options = self.options(model="sonnet")
        self.assertEqual("sonnet", runtime.model_for(options, "middle"))
        self.assertEqual("sonnet", runtime.model_for(options, "sort"))   # no sort model: the middle one sorts
        self.assertEqual("", runtime.model_for(options, "strong"))       # no strong model: the keeper is off
        options = self.options(model="sonnet", sort_model="haiku", strong_model="opus")
        self.assertEqual(["haiku", "sonnet", "opus"], [runtime.model_for(options, t) for t in runtime.TIERS])

    def test_each_tier_runs_its_own_model_under_its_own_cap_and_timeout(self):
        options = self.options(model="sonnet", sort_model="haiku", strong_model="opus")
        proofs = self.gated(options, sort=self.proof("haiku"), middle=self.proof("sonnet"), strong=self.proof("opus"))
        seen = []

        def run(argv, payload, folder, timeout):
            seen.append((argv[argv.index("--model") + 1], timeout))
            return 0, fake_output("claude-" + argv[argv.index("--model") + 1]), None

        with mock.patch.object(runtime, "_run", side_effect=run), \
                mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
            for purpose, model, timeout in (("sort", "haiku", 90), ("draft", "sonnet", 90), ("review", "sonnet", 90),
                                            ("summary", "sonnet", 90), ("text", "opus", 300), ("text_review", "opus", 300)):
                result = runtime.invoke({}, SCHEMA, purpose, proofs)
                self.assertEqual(("ok", runtime.TIER_OF[purpose]), (result["code"], result["tier"]), purpose)
                self.assertEqual((model, timeout), seen[-1], purpose)

    def test_a_lowered_timeout_applies_to_every_tier(self):
        options = self.options(timeout_seconds=20)
        self.assertEqual([20, 20, 20], [runtime.call_timeout(options, t) for t in runtime.TIERS])
        options = self.options()
        self.assertEqual([90, 90, 300], [runtime.call_timeout(options, t) for t in runtime.TIERS])

    def test_input_caps_differ_by_tier(self):
        options = self.options(model="sonnet", sort_model="haiku", strong_model="opus")
        proofs = self.gated(options, sort=self.proof("haiku"), middle=self.proof("sonnet"), strong=self.proof("opus"))
        payload = {"text": "x" * 40000}
        with mock.patch.object(runtime, "_run", side_effect=lambda argv, *a: (0, fake_output("claude-sonnet"), None)), \
                mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
            self.assertEqual("input-limit", runtime.invoke(payload, SCHEMA, "sort", proofs)["code"])   # over 32,000
            self.assertEqual("ok", runtime.invoke(payload, SCHEMA, "draft", proofs)["code"])           # under 48,000
            big = {"text": "x" * 100000}
            self.assertEqual("input-limit", runtime.invoke(big, SCHEMA, "draft", proofs)["code"])
            with mock.patch.object(runtime, "_run", side_effect=lambda argv, *a: (0, fake_output("claude-opus"), None)):
                self.assertEqual("ok", runtime.invoke(big, SCHEMA, "text", proofs)["code"])             # under 120,000

    def test_the_prompts_say_what_their_schemas_hold(self):
        for name in ("sort", "draft", "review"):
            text = runtime.PROMPTS[name]
            self.assertNotRegex(text, r"\b(ids?|IDs?)\b", name)   # messages and plan parts are cited by number
            self.assertIn("untrusted", text)
        self.assertIn("by their numbers", runtime.PROMPTS["sort"])
        self.assertIn("step_done label and a memory label", runtime.PROMPTS["sort"])
        self.assertIn("I'll ask", runtime.PROMPTS["sort"])
        self.assertIn("recortex's own questions", runtime.PROMPTS["sort"])
        self.assertNotIn("to-do tool", runtime.PROMPTS["sort"])
        self.assertIn("{key: checks, part: n}", runtime.PROMPTS["draft"])
        self.assertIn("no checks=, against= or done=", runtime.PROMPTS["draft"])
        self.assertIn("not_agreed" if "not_agreed" in runtime.PROMPTS["review"] else "not agreed", runtime.PROMPTS["review"])
        for name in ("text", "text_review"):
            self.assertIn("untrusted", runtime.PROMPTS[name])


class GateTests(Box):
    def test_a_proof_binds_the_model_of_its_tier(self):
        options = self.options(model="sonnet", sort_model="haiku", strong_model="opus")
        sonnet = self.proof("sonnet")
        self.assertTrue(runtime.gate_valid(sonnet, options, "middle"))
        self.assertFalse(runtime.gate_valid(sonnet, options, "sort"))     # it proves sonnet, not haiku
        self.assertFalse(runtime.gate_valid(sonnet, options, "strong"))
        self.assertTrue(runtime.gate_valid(self.proof("opus"), options, "strong"))
        self.assertTrue(runtime.gate_valid(sonnet))                       # no config: no model binding

    def test_a_proof_is_kept_where_its_tier_reads_it(self):
        options = self.options(model="sonnet")
        self.assertEqual({"sort": "runtime_gate", "middle": "runtime_gate", "strong": "runtime_gate_strong"},
                         {t: runtime.gate_control(t, options) for t in runtime.TIERS})   # the middle model sorts
        options = self.options(model="sonnet", sort_model="haiku")
        self.assertEqual("runtime_gate_sort", runtime.gate_control("sort", options))
        options = self.options(model="sonnet", sort_model="sonnet")
        self.assertEqual("runtime_gate", runtime.gate_control("sort", options))          # the same model: the same proof

    def test_an_invalid_gate_stops_only_its_tier(self):
        options = self.options(model="sonnet", sort_model="haiku", strong_model="opus")
        controls = {"runtime_gate_sort": self.proof("haiku"), "runtime_gate": self.proof("sonnet"),
                    "runtime_gate_strong": self.proof("opus")}

        def state(**changes):
            held = dict(controls, **changes)
            fake = mock.Mock()
            fake.get_control.side_effect = lambda key, default=None: held.get(key, default)
            return {tier: info["gate"] for tier, info in runtime.tier_state(options, fake).items()}

        self.assertEqual({"sort": True, "middle": True, "strong": True}, state())
        self.assertEqual({"sort": False, "middle": True, "strong": True}, state(runtime_gate_sort=None))
        self.assertEqual({"sort": True, "middle": False, "strong": True}, state(runtime_gate=self.proof("sonnet", ok=False)))
        self.assertEqual({"sort": True, "middle": True, "strong": False}, state(runtime_gate_strong=self.proof("sonnet")))
        # a proof for another model than the one now chosen stops that tier and no other
        options = self.options(model="sonnet", sort_model="haiku", strong_model="fable")
        self.assertEqual({"sort": True, "middle": True, "strong": False}, state())

    def test_a_tier_with_no_model_is_not_configured_and_has_no_gate(self):
        options = self.options(model="sonnet")
        report = runtime.tier_state(options, None)
        self.assertEqual({"sort": True, "middle": True, "strong": False}, {t: i["configured"] for t, i in report.items()})
        self.assertFalse(any(info["gate"] for info in report.values()))
        self.assertEqual("", report["strong"]["model"])

    def test_invoke_refuses_a_purpose_whose_tier_gate_is_not_valid_and_launches_nothing(self):
        options = self.options(model="sonnet", sort_model="haiku", strong_model="opus")
        proofs = self.gated(options, sort=None, middle=self.proof("sonnet"), strong=self.proof("opus"))
        with mock.patch.object(runtime.subprocess, "Popen") as launch, \
                mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
            self.assertEqual("runtime-gate", runtime.invoke({}, SCHEMA, "sort", proofs)["code"])
            launch.assert_not_called()
        with mock.patch.object(runtime, "_run", side_effect=lambda argv, *a: (0, fake_output("claude-sonnet"), None)), \
                mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
            self.assertEqual("ok", runtime.invoke({}, SCHEMA, "draft", proofs)["code"])   # the middle tier goes on
        wrong = self.gated(options, sort=self.proof("haiku"), middle=self.proof("sonnet"), strong=self.proof("sonnet"))
        with mock.patch.object(runtime.subprocess, "Popen") as launch, \
                mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
            self.assertEqual("runtime-gate", runtime.invoke({}, SCHEMA, "text", wrong)["code"])
            launch.assert_not_called()

    def test_a_model_that_answers_as_another_model_than_proved_is_refused(self):
        options = self.options(model="sonnet")
        proofs = self.gated(options, middle=self.proof("sonnet", actual="claude-sonnet"))
        with mock.patch.object(runtime, "_run", side_effect=lambda argv, *a: (0, fake_output("claude-other"), None)), \
                mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
            self.assertEqual("model-changed", runtime.invoke({}, SCHEMA, "draft", proofs)["code"])

    def test_the_smoke_call_runs_the_model_of_the_tier_it_proves(self):
        options = self.options(model="sonnet", sort_model="haiku")
        argv = runtime.command(SCHEMA, options, "sort")
        self.assertEqual("haiku", argv[argv.index("--model") + 1])
        argv = runtime.command(SCHEMA, options, "middle")
        self.assertEqual("sonnet", argv[argv.index("--model") + 1])
        self.assertNotIn("--model", runtime.command(SCHEMA, {}, "strong"))
        with mock.patch.object(runtime, "invoke", return_value={"ok": False, "code": "runtime-failed", "usage": {},
                                                                 "elapsed": 0}) as call, \
                mock.patch.object(runtime.subprocess, "run", side_effect=OSError("no claude")):
            proof = runtime.check_runtime(options, "sort")
        self.assertEqual("haiku", proof["requested_model"])
        self.assertEqual("sort", call.call_args_list[0][1]["tier"])


class SettingsTests(Box):
    TABLE = {"max_sort": 80, "max_invocations": 70, "max_strong": 8, "sort_tokens": 800000, "middle_tokens": 700000,
             "strong_tokens": 320000, "max_candidates": 6, "max_posts": 25, "max_tasks": 3, "max_proposals": 2,
             "max_summaries": 4, "max_renders": 30}

    def test_the_defaults_are_the_table_of_the_spec(self):
        for key, value in self.TABLE.items():
            self.assertEqual(value, runtime.DEFAULTS[key], key)
        self.assertEqual("", runtime.DEFAULTS["level"])
        self.assertEqual(("", ""), (runtime.DEFAULTS["sort_model"], runtime.DEFAULTS["strong_model"]))

    def test_a_cap_is_lowered_and_never_raised(self):
        for key, value in self.TABLE.items():
            self.assertEqual(value, runtime.settings({"observer": {key: value * 10}})[key], key)
            self.assertEqual(1, runtime.settings({"observer": {key: 1}})[key], key)
            for bad in (0, -3, True, 2.5, "9"):
                with self.assertRaises(ValueError, msg="%s=%r" % (key, bad)):
                    runtime.settings({"observer": {key: bad}})

    def test_unknown_keys_and_bad_values_are_refused(self):
        for raw in ({"shell": "bad"}, {"level": "all"}, {"level": 3}, {"sort_model": "two words"},
                    {"strong_model": "bad;model"}, {"model": 4}, {"mode": "always"}):
            with self.assertRaises(ValueError, msg=str(raw)):
                runtime.settings({"observer": raw})
        for level in ("", "notes", "assistant", "everything"):
            self.assertEqual(level, runtime.settings({"observer": {"level": level}})["level"])

    def test_the_project_file_cannot_set_or_raise_the_level_a_model_or_a_cap(self):
        shared = {"repo": REPO, "observer": {"enabled": True, "level": "everything", "strong_model": "opus",
                                              "max_strong": 8, "sort_tokens": 1}}
        local = runtime.local_config(shared, self.folder)
        self.assertNotIn("observer", local)       # the entry is this machine's, and there is none
        self.assertEqual("", runtime.settings(local)["level"])
        self.assertFalse(runtime.approval_valid(REPO, local, root=self.folder))
        runtime.approve(REPO, {"observer": {"enabled": True, "level": "notes", "model": "sonnet",
                                            "publisher_id": runtime.publisher_id(create=True)}}, self.folder)
        local = runtime.local_config(shared, self.folder)
        self.assertEqual(("notes", ""), (local["observer"]["level"], local["observer"]["strong_model"]))
        self.assertNotIn("observer", runtime.local_config({"repo": REPO, "observer": False}, self.folder))   # switched off

    def test_usage_note_names_the_tiers_models_and_caps_and_no_money(self):
        text = runtime.usage_note(self.options(model="sonnet", sort_model="haiku", strong_model="opus"))
        for part in ("at most 80 sort calls (haiku)", "70 middle calls (sonnet)", "8 strong calls (opus)",
                     "a typical day is about 36 calls", "Claude Code sign-in", "usage limits"):
            self.assertIn(part, text)
        self.assertNotRegex(text, r"[$]|dollar|cost|saving")
        self.assertIn("off until you enable paper proposals", runtime.usage_note(self.options()))


class ApprovalTests(Box):
    def write_form1(self, settings=None, repo=REPO, folder=None):
        """The approvals file 1.4.0 wrote: form 1, one entry whose settings have none of the keys added since."""
        folder = folder or runtime.folder_key(self.folder)
        entry = {"repo": repo, "settings": dict(settings or OLD_SETTINGS, publisher_id=runtime.publisher_id(create=True)),
                 "publisher_id": runtime.publisher_id(create=True), "root": folder, "approved_at": 1794900000.0,
                 "given": "terminal"}
        private = runtime.approvals_folder(create=True)
        storage.write_file(os.path.join(private, runtime.APPROVALS), json.dumps({"form": 1, "folders": {folder: entry}}))
        return entry

    def raw(self):
        path = os.path.join(runtime.approvals_folder(), runtime.APPROVALS)
        return json.loads(Path(path).read_text())

    def test_a_1_4_0_approvals_file_is_read_and_its_settings_completed(self):
        self.write_form1()
        self.assertEqual(1, runtime.read_approvals()["form"])
        entry = runtime.stored_approval(self.folder)
        self.assertNotIn("level", entry["settings"])           # the file is read as it is
        config = runtime.local_config({"repo": REPO}, self.folder)
        options = runtime.settings(config)
        self.assertEqual(("", "", "", 70, 80, 8), (options["level"], options["sort_model"], options["strong_model"],
                                                   options["max_invocations"] + 10, options["max_sort"], options["max_strong"]))
        self.assertEqual(60, options["max_invocations"])      # what the user had stays
        self.assertEqual(4, options["max_candidates"])

    def test_a_1_4_0_approval_is_not_a_working_approval_until_a_level_is_chosen(self):
        self.write_form1()
        config = runtime.local_config({"repo": REPO}, self.folder)
        self.assertTrue(runtime.settings(config)["enabled"])
        self.assertFalse(runtime.approval_valid(REPO, config, root=self.folder))   # no hook captures, no worker runs
        self.assertTrue(runtime.paused(REPO, {"repo": REPO}, self.folder))
        self.assertFalse(runtime.paused("octo-org/other", {"repo": REPO}, self.folder))   # another repository
        self.assertFalse(runtime.paused(REPO, {"repo": REPO, "observer": False}, self.folder))   # off, not paused
        self.assertEqual("recortex observer: paused until you choose a level.", runtime.PAUSED)

    def test_a_folder_with_no_approval_is_not_paused(self):
        self.assertFalse(runtime.paused(REPO, {"repo": REPO}, self.folder))
        self.write_form1(folder=runtime.folder_key(str(self.base / "elsewhere")))
        self.assertFalse(runtime.paused(REPO, {"repo": REPO}, self.folder))

    def test_answering_with_a_level_makes_a_form_2_file_that_works(self):
        self.write_form1()
        stored = runtime.local_config({"repo": REPO}, self.folder)["observer"]
        settings = dict(stored, level="assistant", publisher_id=runtime.publisher_id(create=True))
        runtime.approve(REPO, {"observer": settings}, self.folder)
        self.assertEqual(2, self.raw()["form"])
        config = runtime.local_config({"repo": REPO}, self.folder)
        self.assertEqual("assistant", config["observer"]["level"])
        self.assertTrue(runtime.approval_valid(REPO, config, root=self.folder))
        self.assertFalse(runtime.paused(REPO, {"repo": REPO}, self.folder))

    def test_the_file_stays_form_1_until_the_next_approval_is_written(self):
        self.write_form1()
        for _ in range(3):
            runtime.read_approvals()
            runtime.local_config({"repo": REPO}, self.folder)
            runtime.paused(REPO, {"repo": REPO}, self.folder)
        self.assertEqual(1, self.raw()["form"])

    def test_forms_other_than_1_and_2_are_refused(self):
        private = runtime.approvals_folder(create=True)
        for form in (0, 3, "1", True):
            storage.write_file(os.path.join(private, runtime.APPROVALS), json.dumps({"form": form, "folders": {}}))
            with self.assertRaises(ValueError, msg=repr(form)):
                runtime.read_approvals()

    def test_an_entry_written_before_the_new_keys_existed_compares_equal_once_completed(self):
        # The completion of form 1: an entry that lacks the new keys (here with only the level added) is the same
        # settings as the configured ones, which settings() completes.
        publisher = runtime.publisher_id(create=True)
        stored = dict(OLD_SETTINGS, publisher_id=publisher, level="notes")
        config = {"observer": dict(stored)}
        approval = {"repo": REPO, "settings": stored, "publisher_id": publisher, "root": runtime.folder_key(self.folder)}
        self.assertTrue(runtime.approval_valid(REPO, config, approval, self.folder))
        other = dict(approval, settings=dict(stored, model="opus"))
        self.assertFalse(runtime.approval_valid(REPO, config, other, self.folder))

    def test_a_1_4_0_approval_runs_no_model_call_and_captures_nothing(self):
        import observer_hooks
        import observer_worker
        import recortex
        trusted = self.base / "trusted"
        trusted.write_text("%s octocat\n" % REPO)
        self.write_form1()
        shared = {"repo": REPO, "hub": 1}
        with mock.patch.dict(os.environ, {names.ENV_PREFIX + "TRUSTED_FILE": str(trusted)}), \
                mock.patch.object(recortex, "project_root", return_value=self.folder):
            config = runtime.local_config(shared, self.folder)
            self.assertTrue(runtime.settings(config)["enabled"])
            out = observer_hooks.handle("UserPromptSubmit", {"session_id": "s1", "prompt": "hello",
                                                             "transcript_path": str(self.base / "none.jsonl")},
                                        config, REPO)
            self.assertEqual({}, out)                        # not even a notice at a prompt
            start = observer_hooks.handle("SessionStart", {"session_id": "s1"}, config, REPO)
            self.assertEqual({"systemMessage": runtime.PAUSED}, start)   # one line at session start: paused until a level is chosen
            with self.assertRaises(store.StoreError):        # and nothing was captured: no queue was made
                store.Store(REPO, create=False, readonly=True, config=config)
            called = []
            report = observer_worker.run_once(REPO, config, self.folder, invoke_fn=lambda *a, **k: called.append(a))
            self.assertEqual("approval", report["code"])
            self.assertEqual([], called)                     # no call of any tier
            with mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
                self.assertEqual("level", runtime.invoke({}, SCHEMA, "sort", dict(runtime.settings(config)))["code"])

    def test_invoke_makes_no_call_without_a_level_even_with_every_proof_valid(self):
        options = self.options(model="sonnet", level="")
        proofs = self.gated(options, middle=self.proof("sonnet"))
        with mock.patch.object(runtime.subprocess, "Popen") as launch, \
                mock.patch.object(runtime.shutil, "which", return_value=sys.executable):
            for purpose in ("sort", "draft", "review", "summary", "text"):
                self.assertEqual("level", runtime.invoke({}, SCHEMA, purpose, proofs)["code"], purpose)
            launch.assert_not_called()


class LedgerTests(Box):
    """The queue's ledger: caps and token budgets per tier, the new columns and tables, format 4."""

    def open(self, **kw):
        return store.Store(REPO, config={}, **kw)

    def fill(self, db, tier, count, tokens=0, purpose=None, age=0):
        """`count` finished calls of the tier, each with `tokens` input tokens, `age` seconds old."""
        purpose = purpose or {"sort": "sort", "middle": "draft", "strong": "text"}[tier]
        for _ in range(count):
            ident = db.reserve_invocation(purpose, 1000, 86400, tier=tier, tokens=10 ** 9)
            db.complete_invocation(ident, True, usage={"input_tokens": tokens})
            if age:
                db.db.execute("UPDATE invocations SET created=? WHERE id=?", (time.time() - age, ident))

    def test_the_call_cap_stops_a_tier_on_its_own(self):
        with self.open() as db:
            self.fill(db, "strong", 8)
            self.assertIsNone(db.reserve_invocation("text", 8, 86400))                   # 8 of 8
            self.assertIsNotNone(db.reserve_invocation("draft", 70, 86400))              # other tiers are not counted
            self.assertIsNotNone(db.reserve_invocation("sort", 80, 86400))
            self.assertIsNone(db.reserve_invocation("text", 3, 86400))                   # a lowered cap

    def test_a_cap_in_the_settings_never_raises_the_cap_of_the_tier(self):
        with self.open() as db:
            self.fill(db, "strong", 8)
            self.assertIsNone(db.reserve_invocation("text", 500, 86400))
            self.fill(db, "sort", 80)
            self.assertIsNone(db.reserve_invocation("sort", 9999, 86400))
            self.fill(db, "middle", 70)
            self.assertIsNone(db.reserve_invocation("review", 9999, 86400))

    def test_the_token_budget_stops_the_next_call_on_its_own(self):
        with self.open() as db:
            self.fill(db, "middle", 1, tokens=499990)
            self.assertIsNotNone(db.reserve_invocation("draft", 70, 86400, tokens=700000))
            self.fill(db, "middle", 1, tokens=200010)          # 700,000 recorded: at the budget
            self.assertIsNone(db.reserve_invocation("draft", 70, 86400, tokens=700000))
            self.assertIsNotNone(db.reserve_invocation("sort", 80, 86400, tokens=800000))   # the sort tier has its own
            self.assertIsNone(db.reserve_invocation("review", 70, 86400, tokens=700000))
            self.assertIsNone(db.reserve_invocation("review", 70, 86400, tokens=900000))     # never above the default

    def test_a_budget_never_exceeds_the_default_and_a_lower_one_binds(self):
        with self.open() as db:
            self.fill(db, "strong", 2, tokens=160000)
            self.assertIsNone(db.reserve_invocation("text", 8, 86400, tokens=10 ** 12))   # 320,000 is the ceiling
            self.assertIsNone(db.reserve_invocation("text", 8, 86400))                    # no budget given: the default
        with self.open() as db:
            self.assertIsNone(db.reserve_invocation("text", 8, 86400, tokens=1000))       # 320,000 recorded, 1,000 allowed

    def test_the_window_is_rolling_and_old_calls_do_not_count(self):
        with self.open() as db:
            self.fill(db, "strong", 8, tokens=40000, age=90000)
            self.assertIsNotNone(db.reserve_invocation("text", 8, 86400))

    def test_calls_recorded_before_format_4_count_as_middle_calls(self):
        with self.open() as db:
            for _ in range(70):
                db.db.execute("INSERT INTO invocations(purpose,created,completed,success) VALUES ('extract',?,?,1)",
                              (time.time() - 60, time.time() - 59))
            self.assertIsNone(db.reserve_invocation("draft", 70, 86400))
            self.assertIsNotNone(db.reserve_invocation("sort", 80, 86400))
            self.assertEqual(70, db.usage_today()["middle"]["calls"])

    def test_the_other_purposes_the_worker_names_count_on_the_middle_tier(self):
        with self.open() as db:
            ident = db.reserve_invocation("reviewer", 70, 86400)
            self.assertEqual("middle", db.db.execute("SELECT tier FROM invocations WHERE id=?", (ident,)).fetchone()[0])
            ident = db.reserve_invocation("runtime_check", 8, 86400, tier="strong")
            self.assertEqual(("strong", "check"), tuple(db.db.execute("SELECT tier,job FROM invocations WHERE id=?",
                                                                      (ident,)).fetchone()))

    def test_the_job_follows_the_purpose(self):
        with self.open() as db:
            jobs = {}
            for purpose in ("sort", "draft", "review", "summary", "summary_review", "text", "text_review"):
                ident = db.reserve_invocation(purpose, 80, 86400)
                jobs[purpose] = db.db.execute("SELECT job FROM invocations WHERE id=?", (ident,)).fetchone()[0]
            self.assertEqual({"sort": "sort", "draft": "memories", "review": "memories", "summary": "status",
                              "summary_review": "status", "text": "paper", "text_review": "paper"}, jobs)

    def test_token_columns_are_stored_from_the_usage_and_summed_as_the_evaluation_sums_them(self):
        with self.open() as db:
            ident = db.reserve_invocation("draft", 70, 86400)
            db.complete_invocation(ident, True, "0" * 64, usage={"input_tokens": 3, "cache_creation_input_tokens": 5,
                                                                "cache_read_input_tokens": 7, "output_tokens": 11,
                                                                "service_tier": "standard"}, model="claude-sonnet-5-5")
            row = dict(db.db.execute("SELECT * FROM invocations WHERE id=?", (ident,)).fetchone())
            self.assertEqual((15, 11, 7, 5, "claude-sonnet-5-5", "middle", "memories"),
                             (row["input_tokens"], row["output_tokens"], row["cache_read"], row["cache_write"],
                              row["model"], row["tier"], row["job"]))

    def test_a_count_that_is_not_an_integer_in_range_is_stored_as_zero(self):
        with self.open() as db:
            def stored(usage, model=""):
                ident = db.reserve_invocation("draft", 1000, 86400, tokens=10 ** 9)
                db.complete_invocation(ident, True, usage=usage, model=model)
                row = db.db.execute("SELECT input_tokens,output_tokens,cache_read,cache_write,model FROM invocations "
                                    "WHERE id=?", (ident,)).fetchone()
                db.db.execute("UPDATE invocations SET created=? WHERE id=?", (time.time() - 3 * 86400, ident))
                return tuple(row)

            self.assertEqual((0, 0, 0, 0, ""), stored({"input_tokens": "12", "output_tokens": -5,
                                                       "cache_read_input_tokens": 3.5,
                                                       "cache_creation_input_tokens": True}))
            self.assertEqual((0, 0, 0, 0, ""), stored({"input_tokens": 10 ** 9 + 1, "output_tokens": 10 ** 30,
                                                       "cache_read_input_tokens": float("inf"),
                                                       "cache_creation_input_tokens": None}))
            self.assertEqual((10 ** 9, 10 ** 9, 0, 0, ""), stored({"input_tokens": 10 ** 9, "output_tokens": 10 ** 9}))
            self.assertEqual((0, 0, 0, 0, ""), stored("not a dict"))
            self.assertEqual((0, 0, 0, 0, ""), stored(None, model="<b>markup</b>"))
            self.assertEqual((0, 0, 0, 0, ""), stored({}, model="x" * 101))
            self.assertEqual((0, 0, 0, 0, "claude-haiku-5-5"), stored({}, model="claude-haiku-5-5"))

    def test_a_reservation_is_completed_once(self):
        with self.open() as db:
            ident = db.reserve_invocation("sort", 80, 86400)
            db.complete_invocation(ident, True)
            with self.assertRaises(store.StoreError):
                db.complete_invocation(ident, True)

    def test_usage_rows_and_cleanup_after_35_days(self):
        with self.open() as db:
            self.fill(db, "sort", 1, tokens=100)
            self.fill(db, "strong", 1, tokens=200, age=36 * 86400)
            self.fill(db, "middle", 1, tokens=300, age=10 * 86400)
            rows = db.usage_rows(days=35)
            self.assertEqual(["sort", "middle"], [row["tier"] for row in rows])       # the 36-day-old one is out of range
            self.assertEqual(2, len(db.usage_rows(days=99 * 99)))                      # the days are clamped to 35
            self.assertEqual(1, db.expire_usage())                                     # only the 36-day-old one is zeroed
            self.assertEqual([100, 300], sorted(row["input_tokens"] for row in db.usage_rows(days=35)))
            self.assertEqual(0, db.db.execute("SELECT input_tokens FROM invocations WHERE tier='strong'").fetchone()[0])
            self.assertEqual(3, db.db.execute("SELECT COUNT(*) FROM invocations").fetchone()[0])   # the times stay

    def test_the_daily_cleanup_zeroes_old_token_counts(self):
        with self.open() as db:
            self.fill(db, "middle", 1, tokens=300, age=40 * 86400)
            db.expire_handled(7)
            self.assertEqual(0, db.db.execute("SELECT input_tokens FROM invocations").fetchone()[0])

    def test_usage_today_has_every_tier(self):
        with self.open() as db:
            self.fill(db, "sort", 2, tokens=10)
            today = db.usage_today()
            self.assertEqual(["sort", "middle", "strong"], list(today))
            self.assertEqual({"calls": 2, "input_tokens": 20, "output_tokens": 0}, today["sort"])
            self.assertEqual({"calls": 0, "input_tokens": 0, "output_tokens": 0}, today["strong"])

    def test_a_new_queue_is_format_4_with_the_new_tables(self):
        with self.open() as db:
            self.assertEqual("4", db.format)
            names_ = {row[0] for row in db.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"acts", "drafts", "labels"} <= names_)
            self.assertEqual([], db.waiting())
            self.assertIn("tiers_24h", db.status())

    def downgrade(self, db):
        """Make the queue what a 1.4.0 one is (format 3) by taking away what format 4 added."""
        path = db.path
        db.close()
        connection = sqlite3.connect(path)
        for statement in store.INDEXES[len(store.INDEXES_3):]:
            connection.execute("DROP INDEX %s" % statement.split()[5])
        for table in ("acts", "drafts", "labels"):
            connection.execute("DROP TABLE %s" % table)
        for name, _ in reversed(store.INVOCATION_ADDED):
            connection.execute("ALTER TABLE invocations DROP COLUMN %s" % name)
        connection.execute("UPDATE meta SET value='3' WHERE key='format'")
        connection.commit()
        connection.close()

    def test_a_format_3_queue_is_migrated_and_its_calls_kept_as_middle_calls(self):
        with self.open() as db:
            self.fill(db, "middle", 2, tokens=5)
            self.downgrade(db)
        with self.open(readonly=True) as db:   # read only, it is read as it is
            self.assertEqual("3", db.format)
            self.assertEqual([], db.waiting_for_answer())   # the lookups of format 3 still work
            with self.assertRaises(store.StoreError):
                db.reserve_invocation("sort", 80, 86400)
        with self.open() as db:
            self.assertEqual("4", db.format)
            self.assertEqual(2, db.usage_today()["middle"]["calls"])
            self.assertEqual(0, db.usage_today()["middle"]["input_tokens"])
            ident = db.reserve_invocation("text", 8, 86400)
            db.complete_invocation(ident, True, usage={"output_tokens": 9})
            self.assertEqual(9, db.usage_today()["strong"]["output_tokens"])
        with self.open(readonly=True) as db:   # and it validates as format 4 afterwards
            self.assertEqual("4", db.format)

    def test_a_queue_whose_new_columns_were_altered_is_refused(self):
        with self.open() as db:
            path = db.path
        connection = sqlite3.connect(path)
        connection.execute("ALTER TABLE invocations ADD COLUMN stray TEXT")
        connection.commit()
        connection.close()
        with self.assertRaises(store.StoreError):
            self.open(readonly=True)

    def test_acts_are_kept_with_numbers_only(self):
        with self.open() as db:
            first = db.add_act("memory", issue=5, comment=9001, numbers=[1])
            second = db.add_act("task_rendered", issue=14, numbers=[3, 5], backup="task-14-1.md")
            acts = db.acts(days=7)
            self.assertEqual([second, first], [a["id"] for a in acts])
            self.assertEqual(([3, 5], "task-14-1.md", False), (acts[0]["numbers"], acts[0]["backup"], acts[0]["undone"]))
            self.assertTrue(db.mark_undone(second))
            self.assertFalse(db.mark_undone(second))
            self.assertTrue(db.act(second)["undone"])
            for bad in (dict(kind="bold"), dict(kind="memory", issue=-1), dict(kind="memory", numbers=["3"]),
                        dict(kind="memory", numbers=[True]), dict(kind="memory", backup="../x"),
                        dict(kind="memory", numbers=list(range(21))), dict(kind="memory", issue=10 ** 10)):
                with self.assertRaises(store.StoreError, msg=str(bad)):
                    db.add_act(**bad)

    def test_drafts_wait_for_the_users_answer_and_come_back_after_later(self):
        with self.open() as db:
            ident = db.add_draft(5, ["Results"], "/private/keeper/abc", "a" * 64, "b" * 64)
            self.assertRegex(ident, r"^[0-9a-f]{8}$")
            refused = db.add_draft(6, ["Methods"], "/private/keeper/def", "c" * 64, state="refused", reason="check_failed")
            self.assertEqual([ident], [item["key"] for item in db.waiting()])
            self.assertEqual("draft", db.waiting()[0]["kind"])
            self.assertEqual("refused", db.draft(refused)["state"])
            self.assertTrue(db.set_draft(ident, "later", until=time.time() + 3600))
            self.assertEqual([], db.waiting())
            self.assertTrue(db.set_draft(ident, "later", until=time.time() - 1))
            self.assertEqual([ident], [item["key"] for item in db.waiting()])
            self.assertTrue(db.set_draft(ident, "applied"))
            self.assertEqual([], db.waiting())
            for bad in (dict(topic=0, headings=["Results"], folder="/x", base_hash="a" * 64),
                        dict(topic=5, headings=[], folder="/x", base_hash="a" * 64),
                        dict(topic=5, headings=["Results"], folder="/x", base_hash="short"),
                        dict(topic=5, headings=["Results"], folder="/x\x00", base_hash="a" * 64),
                        dict(topic=5, headings=["Results"], folder="/x", base_hash="a" * 64, state="applied")):
                with self.assertRaises(store.StoreError, msg=str(bad)):
                    db.add_draft(**bad)
            self.assertIsNone(db.draft("nothex"))
            self.assertFalse(db.set_draft("0" * 8, "dropped"))

    def test_labels_wait_for_a_draft_until_they_are_finished(self):
        with self.open() as db:
            db.db.execute("INSERT INTO observations VALUES ('s1','{}','observed','',1,1)")
            db.save_labels("a" * 32, ["s1"], [{"job": "memory", "kind": "result", "sources": [1]}])
            self.assertEqual(["a" * 32], [item["batch"] for item in db.waiting_labels()])
            self.assertTrue(db.finish_labels("a" * 32))
            self.assertEqual([], db.waiting_labels())
            with self.assertRaises(store.StoreError):
                db.save_labels("zz", ["s1"], [])

    def test_held_memories_are_on_the_one_list_of_what_waits(self):
        with self.open() as db:
            self.assertEqual([], db.waiting())
            with mock.patch.object(store.Store, "waiting_for_answer", return_value=[
                    {"eventkey": "1" * 32, "created": 5.0, "candidate": {}}]):
                items = db.waiting()
            self.assertEqual([("held", "1" * 8)], [(item["kind"], item["short"]) for item in items])


class CliTests(Box):
    """observer.py: enable with a level and models, status by tier, runtime-check per distinct model, limits."""

    def setUp(self):
        super().setUp()
        import contextlib
        import io
        import recortex
        self.contextlib, self.io = contextlib, io
        (Path(self.folder) / ".recortex.json").write_text(json.dumps({"repo": REPO}))
        self.trusted = self.base / "trusted"
        self.trusted.write_text("%s octocat\n" % REPO)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.folder,
                                               names.ENV_PREFIX + "TRUSTED_FILE": str(self.trusted)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recortex = recortex

    def run_cli(self, *args):
        import observer
        out = self.io.StringIO()
        # These tests are about what enable stores, not about who may run it: the gate has its own tests (test_jobs_enable).
        with mock.patch("observer_grants.needs", return_value=[]), self.contextlib.redirect_stdout(out):
            code = observer.main(list(args))
        return code, json.loads(out.getvalue())

    def stored(self):
        return runtime.stored_approval(self.folder)

    def enable(self, *extra):
        return self.run_cli("enable", "--model", "sonnet", "--level", "everything", "--yes", *extra)

    def test_enable_needs_a_level_and_previews_it_with_the_usage_of_each_tier(self):
        code, value = self.run_cli("enable", "--model", "sonnet")
        self.assertEqual((1, "level_required"), (code, value["reason"]))
        code, value = self.run_cli("enable", "--model", "sonnet", "--level", "assistant", "--sort-model", "haiku")
        self.assertEqual(0, code)
        self.assertEqual((False, "assistant", "Assistant"), (value["approved"], value["level"], value["level_label"]))
        for part in ("at most 80 sort calls (haiku)", "70 middle calls (sonnet)", "8 strong calls (off until you enable "
                     "paper proposals)", "typical day is about 36 calls"):
            self.assertIn(part, value["usage"])
        self.assertNotRegex(value["usage"], r"[$]|dollar")
        self.assertIsNone(self.stored())   # a preview writes nothing

    def test_enable_with_a_level_writes_a_form_2_approval_and_status_shows_the_tiers(self):
        code, value = self.enable("--sort-model", "haiku", "--strong-model", "opus")
        self.assertEqual((0, True), (code, value["approved"]))
        path = os.path.join(runtime.approvals_folder(), runtime.APPROVALS)
        self.assertEqual(2, json.loads(Path(path).read_text())["form"])
        self.assertEqual(("everything", "haiku", "opus"), tuple(self.stored()["settings"][k] for k in
                                                                ("level", "sort_model", "strong_model")))
        code, status = self.run_cli("status")
        self.assertEqual((True, False, "Everything"), (status["approved"], status["paused"], status["level_label"]))
        tiers = status["tiers"]
        order = ("sort", "middle", "strong")
        self.assertEqual(set(order), set(tiers))
        self.assertEqual(["haiku", "sonnet", "opus"], [tiers[t]["model"] for t in order])
        self.assertEqual([80, 70, 8], [tiers[t]["call_cap"] for t in order])
        self.assertEqual([800000, 700000, 320000], [tiers[t]["token_budget"] for t in order])
        self.assertEqual([False] * 3, [tiers[t]["gate"] for t in order])   # nothing is proved yet
        self.assertEqual([0] * 3, [tiers[t]["calls_24h"] for t in order])
        self.assertFalse(status["runtime_ready"])

    def test_the_level_of_an_earlier_approval_stands_when_none_is_given(self):
        self.enable()
        code, value = self.run_cli("enable", "--model", "haiku", "--yes")
        self.assertEqual((0, "everything", "haiku"), (code, value["level"], self.stored()["settings"]["model"]))

    def test_automatic_mode_needs_a_valid_proof_for_every_tier_that_has_a_model(self):
        self.enable()
        code, value = self.run_cli("enable", "--mode", "automatic", "--model", "sonnet", "--level", "everything", "--yes")
        self.assertEqual((1, "runtime-gate"), (code, value["code"]))

    def test_runtime_check_makes_one_call_per_distinct_model_and_each_tier_keeps_its_own_proof(self):
        self.enable("--sort-model", "haiku", "--strong-model", "opus")
        asked = []

        def check(options, tier):
            asked.append(tier)
            return self.proof(runtime.model_for(options, tier))

        with mock.patch.object(runtime, "check_runtime", side_effect=check):
            code, value = self.run_cli("runtime-check")
        self.assertEqual((0, ["middle", "sort", "strong"]), (code, asked))
        self.assertEqual({"middle", "sort", "strong"}, set(value["tiers"]))
        self.assertEqual(["sonnet", "haiku", "opus"], [value["tiers"][t]["model"] for t in ("middle", "sort", "strong")])
        code, status = self.run_cli("status")
        self.assertEqual([True] * 3, [status["tiers"][t]["gate"] for t in ("sort", "middle", "strong")])
        self.assertEqual([1] * 3, [status["tiers"][t]["calls_24h"] for t in ("sort", "middle", "strong")])
        with store.Store(REPO, create=False, readonly=True, config={}) as db:
            self.assertEqual({"check"}, {row["job"] for row in db.usage_rows(days=1)})
            self.assertEqual({"sort", "middle", "strong"}, {row["tier"] for row in db.usage_rows(days=1)})
        # now the automatic switch is allowed, and the strong proof is part of what it needs
        code, value = self.run_cli("enable", "--mode", "automatic", "--model", "sonnet", "--level", "everything",
                                   "--sort-model", "haiku", "--strong-model", "opus", "--yes")
        self.assertEqual((0, True), (code, value["approved"]))

    def test_one_model_for_several_tiers_is_checked_once(self):
        self.enable("--sort-model", "sonnet")
        asked = []

        def check(options, tier):
            asked.append(tier)
            return self.proof(runtime.model_for(options, tier))

        with mock.patch.object(runtime, "check_runtime", side_effect=check):
            code, value = self.run_cli("runtime-check")
        self.assertEqual((0, ["middle"]), (code, asked))
        self.assertEqual({"middle", "sort"}, set(value["tiers"]))
        with store.Store(REPO, create=False, readonly=True, config={}) as db:
            self.assertEqual(db.get_control("runtime_gate")["requested_model"], "sonnet")
            self.assertIsNone(db.get_control("runtime_gate_sort"))     # the sort tier reads the middle proof

    def test_a_failed_check_clears_only_its_tier_and_the_others_go_on(self):
        self.enable("--sort-model", "haiku", "--strong-model", "opus")

        def check(options, tier):
            model = runtime.model_for(options, tier)
            return self.proof(model, ok=False) if model == "opus" else self.proof(model)

        with mock.patch.object(runtime, "check_runtime", side_effect=check):
            code, value = self.run_cli("runtime-check")
        self.assertEqual(1, code)
        code, status = self.run_cli("status")
        self.assertEqual([True, True, False], [status["tiers"][t]["gate"] for t in ("sort", "middle", "strong")])

    def test_a_tier_at_its_cap_is_not_checked_and_the_others_are(self):
        self.enable("--strong-model", "opus")
        with store.Store(REPO, config={}) as db:
            for _ in range(8):
                db.complete_invocation(db.reserve_invocation("text", 8, 86400), True)
        with mock.patch.object(runtime, "check_runtime", side_effect=lambda o, t: self.proof(runtime.model_for(o, t))):
            code, value = self.run_cli("runtime-check")
        self.assertEqual(1, code)
        self.assertEqual("budget", value["tiers"]["strong"]["code"])
        self.assertTrue(value["ok"])

    def test_limits_lower_the_call_caps_and_keep_the_approvals_time_and_source(self):
        self.enable()
        before = self.stored()
        code, value = self.run_cli("limits", "--sort", "40", "--middle", "30", "--strong", "4")
        self.assertEqual((0, {"sort": 40, "middle": 30, "strong": 4}), (code, value["caps"]))
        after = self.stored()
        self.assertEqual((40, 30, 4), tuple(after["settings"][k] for k in ("max_sort", "max_invocations", "max_strong")))
        self.assertEqual((before["approved_at"], before["given"]), (after["approved_at"], after["given"]))
        self.assertEqual(before["settings"]["level"], after["settings"]["level"])
        self.assertEqual(2, json.loads(Path(os.path.join(runtime.approvals_folder(), runtime.APPROVALS)).read_text())["form"])
        code, status = self.run_cli("status")
        self.assertTrue(status["approved"])
        self.assertEqual([40, 30, 4], [status["tiers"][t]["call_cap"] for t in ("sort", "middle", "strong")])
        self.assertIn("at most 40 sort calls", status["usage"])

    def test_limits_refuse_a_higher_number_and_a_number_below_one_and_change_nothing(self):
        self.enable()
        self.run_cli("limits", "--sort", "40")
        before = self.stored()
        code, value = self.run_cli("limits", "--sort", "41")
        self.assertEqual((1, "limit-higher", 40), (code, value["code"], value["cap"]))
        code, value = self.run_cli("limits", "--middle", "0")
        self.assertEqual((1, "limit-invalid"), (code, value["code"]))
        code, value = self.run_cli("limits", "--middle", "70", "--strong", "9")
        self.assertEqual((1, "limit-higher"), (code, value["code"]))   # one number too high refuses the whole change
        self.assertEqual(before, self.stored())
        code, value = self.run_cli("limits")
        self.assertEqual((1, "nothing-to-change"), (code, value["code"]))

    def test_1_5_1_can_read_its_approval_after_the_limits_change(self):
        from tests.fixtures import observer_settings_151 as old
        options = old.settings({"observer": {"enabled": True, "mode": "automatic", "level": "assistant",
                                              "model": "sonnet", "publisher_id": runtime.publisher_id(create=True)}})
        root = runtime.folder_key(self.folder)
        entry = {"repo": REPO, "settings": options, "publisher_id": options["publisher_id"], "root": root,
                 "approved_at": 1794900000.0, "given": "terminal"}
        storage.write_file(os.path.join(runtime.approvals_folder(create=True), runtime.APPROVALS),
                           json.dumps({"form": 2, "folders": {root: entry}}))
        self.assertEqual(0, self.run_cli("limits", "--middle", "30")[0])
        saved = self.stored()
        # 1.7.0 adds one key to the entry (never to its settings): the identity of the key file, 32 hex digits
        self.assertRegex(saved["key_file"], "^[0-9a-f]{32}$")
        self.assertEqual(dict(entry, settings=dict(options, max_invocations=30), key_file=saved["key_file"]), saved)
        self.assertEqual(saved["settings"], old.settings({"observer": saved["settings"]}))
        self.assertTrue(runtime.approval_valid(REPO, runtime.local_config({})))

    def test_only_enabled_1_6_modes_require_the_new_reader(self):
        from tests.fixtures import observer_settings_151 as old
        self.enable()
        with mock.patch("observer.on_terminal", return_value=True):
            for command, key, value in (("advisory", "advisory_mode", "deliver"),
                                        ("map-mode", "map_mode", "automatic")):
                with self.subTest(command=command):
                    self.assertEqual(0, self.run_cli(command, value, "--yes")[0])
                    self.assertEqual(0, self.run_cli("limits", "--middle", "30")[0])
                    options = self.stored()["settings"]
                    self.assertEqual(value, options[key])
                    self.assertTrue(runtime.approval_valid(REPO, runtime.local_config({})))
                    with self.assertRaisesRegex(ValueError, "observer_settings"):
                        old.settings({"observer": options})
                    self.assertEqual(0, self.run_cli(command, "off", "--yes")[0])
                    saved = self.stored()["settings"]
                    self.assertEqual(saved, old.settings({"observer": saved}))

    def test_limits_change_an_approved_folder_only(self):
        code, value = self.run_cli("limits", "--sort", "10")
        self.assertEqual((1, "approval"), (code, value["code"]))
        self.assertIsNone(self.stored())

    def test_lowered_caps_stay_lowered_when_the_folder_is_enabled_again(self):
        self.enable()
        self.run_cli("limits", "--sort", "40", "--strong", "2")
        self.enable("--strong-model", "opus")
        settings = self.stored()["settings"]
        self.assertEqual((40, 2, 70), (settings["max_sort"], settings["max_strong"], settings["max_invocations"]))

    def test_the_caps_of_a_1_4_0_approval_are_its_defaults_and_give_way_to_the_new_ones(self):
        root = runtime.folder_key(self.folder)
        old = dict(OLD_SETTINGS, publisher_id=runtime.publisher_id(create=True))
        self.assertEqual(60, old["max_invocations"])   # what 1.4.0 stored for every folder: its default, never a choice
        entry = {"repo": REPO, "settings": old, "publisher_id": old["publisher_id"], "root": root,
                 "approved_at": 1794900000.0, "given": "terminal"}
        storage.write_file(os.path.join(runtime.approvals_folder(create=True), runtime.APPROVALS),
                           json.dumps({"form": 1, "folders": {root: entry}}))
        code, value = self.run_cli("enable", "--model", "sonnet", "--level", "notes", "--yes")
        self.assertEqual((0, True), (code, value["approved"]))
        self.assertEqual(70, self.stored()["settings"]["max_invocations"])

    def test_status_says_a_1_4_0_approval_is_paused_until_a_level_is_chosen(self):
        root = runtime.folder_key(self.folder)
        old = dict(OLD_SETTINGS, publisher_id=runtime.publisher_id(create=True))
        entry = {"repo": REPO, "settings": old, "publisher_id": old["publisher_id"], "root": root,
                 "approved_at": 1794900000.0, "given": "terminal"}
        storage.write_file(os.path.join(runtime.approvals_folder(create=True), runtime.APPROVALS),
                           json.dumps({"form": 1, "folders": {root: entry}}))
        code, status = self.run_cli("status")
        self.assertEqual((False, True, "not chosen"), (status["approved"], status["paused"], status["level_label"]))
        self.assertIn(runtime.PAUSED, status["note"])
        code, value = self.run_cli("runtime-check")
        self.assertEqual((1, "approval"), (code, value["code"]))    # no call is made for a folder without a level
        code, value = self.run_cli("limits", "--sort", "10")
        self.assertEqual("approval", value["code"])
        code, value = self.run_cli("enable", "--model", "sonnet", "--level", "notes", "--yes")
        self.assertEqual((0, True), (code, value["approved"]))
        code, status = self.run_cli("status")
        self.assertEqual((True, False), (status["approved"], status["paused"]))


if __name__ == "__main__":
    unittest.main()

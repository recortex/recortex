"""The setup questions of 1.5.0: `observer-level` (which replaces the `observer` question of 1.1.0), `paper-proposals`, and the
commands `setup.py observer --level` and `setup.py observer --strong`. Offline: the record is the fake repository of
tests/test_setup_questions.py, and the model's process is a stand-in program on the PATH.

The owner's answer 2 is here as one test: an observer enabled before 1.5.0 does nothing until the user answers the level
question, and the brief's setup line counts that question."""

import json
import os
import stat
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402
import test_setup_questions as tq  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import brief  # noqa: E402
import observer_runtime  # noqa: E402
import setup_questions  # noqa: E402

REPO, PAPER = tq.REPO, tq.PAPER
LEVEL = "observer-level"
QUIET = {name: None for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK")}


def program(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, 0o755)


class Programs:
    """A stand-in for `claude` that passes the runtime check with no model call and writes a line to a log for each call,
    and one that only records that it was started."""

    def stand_in(self, name="working"):
        log = os.path.join(self.dir, "claude-%s.log" % name)
        open(log, "w").close()   # the folder is the fixture's, shared by the tests of a class: start with an empty log
        init = {"type": "system", "subtype": "init", "tools": ["StructuredOutput"], "mcp_servers": [], "skills": [],
                "plugins": [], "model": "claude-test", "apiKeySource": "none"}
        done = {"type": "result", "is_error": False, "usage": {"input_tokens": 4}, "structured_output": {
            "capture": True, "event_type": "decision", "quote": "For this invented project, choose blue for the sample card."}}
        program(os.path.join(self.dir, "bin-" + name, "claude"),
                "#!/bin/sh\nif [ \"$1\" = --version ]; then echo '2.1.400 (Claude Code)'; exit 0; fi\n"
                "echo \"$@\" >> '%s'\ncat >/dev/null\nprintf '%%s\\n' '%s' '%s'\n" % (log, json.dumps(init), json.dumps(done)))
        return os.path.join(self.dir, "bin-" + name), log

    def answer(self, level=None, strong=None):
        """The user's recorded answers a `setup.py observer ... --yes` of a session goes on (its gate has its own tests:
        test_jobs_enable). Planted for this folder in the sandbox's private state, where the command reads them."""
        import observer_grants
        self.in_process()
        folder = observer_runtime.folder_key(self.project)
        for kind, value in (("level", level), ("strong", strong)):
            if value:
                self.assertTrue(observer_grants.put(folder, kind, value, "toolu_test%d" % time.time_ns()))

    def with_path(self, folder, **extra):
        env = dict(QUIET, PATH=folder + os.pathsep + os.environ["PATH"])
        env.update(extra)
        return env

    def calls(self, log):
        """The lines the stand-in logged: one for each `claude -p` call (the version check is not logged)."""
        if not os.path.exists(log):
            return []
        with open(log) as handle:
            return handle.read().splitlines()

    def models_of(self, log):
        found = []
        for line in self.calls(log):
            words = line.split()
            if "--model" in words:
                found.append(words[words.index("--model") + 1])
        return found

    def status(self, env=None):
        done = self.tool("observer.py", "--repo", self.repo_name, "status", env=env)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def stored_settings(self):
        path = os.path.join(self.dir, "xdg-state", "recortex", "private", "observer-approvals.json")
        with open(path) as handle:
            data = json.load(handle)
        return data, next(iter(data["folders"].values()))["settings"]


class LevelQuestionTests(Programs, tq.Box):
    """`observer-level`: open for a new folder and for an approval of 1.4.0, settled by a level, closed by a 1.4.0 "no"."""

    def test_it_is_open_for_a_folder_with_no_approval(self):
        self.assertEqual(["status-line", LEVEL, "task-reminder"], self.open_ids())
        self.assertEqual("1.5.0", setup_questions.find(LEVEL).since)

    def test_it_is_open_for_an_approval_written_before_1_5_0_and_the_brief_counts_it(self):
        self.approval(observer_mode="automatic")   # the form 1 entry of 1.4.0: no level
        self.assertEqual(["status-line", LEVEL, "task-reminder"], self.open_ids())
        self.assertIn(LEVEL, self.groups()["open"])
        self.assertEqual([brief.SETUP_QUESTIONS % (3, "s")], self.lines(self.start()))

    def test_it_is_settled_by_each_level_in_the_approval(self):
        for level in observer_runtime.LEVELS:
            self.approval(level=level)
            self.assertNotIn(LEVEL, self.open_ids(), level)
            self.assertIn(LEVEL, self.groups()["answered"], level)

    def test_a_level_that_is_not_one_of_the_three_settles_nothing(self):
        for level in ("", "most", 3, None):
            path = self.approval(level="notes")
            with open(path) as handle:
                data = json.load(handle)
            for entry in data["folders"].values():
                entry["settings"]["level"] = level
            tq.put_json(path, data)
            self.assertIn(LEVEL, self.open_ids(), repr(level))

    def test_a_1_4_0_no_or_later_still_counts_and_a_1_4_0_yes_does_not(self):
        # The old question said yes to 1.4.0's observer; the new one asks the user again.
        self.put_state(repos={REPO: {"observer": self.entry("no")}})
        self.assertNotIn(LEVEL, self.open_ids())
        self.assertIn(LEVEL, self.groups()["answered"])
        self.put_state(repos={REPO: {"observer": self.entry("yes")}})
        self.assertIn(LEVEL, self.open_ids())
        self.put_state(repos={REPO: {"observer": self.entry("later")}})
        self.assertNotIn(LEVEL, self.open_ids())
        self.assertIn(LEVEL, self.groups()["deferred"])
        self.put_state(repos={REPO: {"observer": self.entry("later", 9)}})   # nine days ago: asked again
        self.assertIn(LEVEL, self.open_ids())

    def test_the_new_id_wins_over_the_old_one(self):
        self.put_state(repos={REPO: {"observer": self.entry("no"), LEVEL: self.entry("later")}})
        self.assertIn(LEVEL, self.groups()["deferred"])
        self.put_state(repos={REPO: {"observer": self.entry("later"), LEVEL: self.entry("no")}})
        self.assertIn(LEVEL, self.groups()["answered"])

    def test_a_1_4_0_answers_file_loads_and_keeps_what_it_holds(self):
        self.put_state(machine={"status-line": self.entry("no")},
                       repos={REPO: {"observer": self.entry("no"), "labels": self.entry("yes")}})
        self.assertEqual(["task-reminder"], self.open_ids())   # the one question of 1.7.0 that applies
        done = self.tool("setup.py", "answer", "--id", "status-line", "--answer", "later")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        stored = self.stored()
        self.assertEqual(self.entry("no"), stored["repos"][REPO]["observer"])   # the old entry is kept as it was
        self.assertEqual(self.entry("yes"), stored["repos"][REPO]["labels"])

    def test_the_retired_id_is_still_accepted_and_recorded_under_the_new_one(self):
        done = self.tool("setup.py", "answer", "--id", "observer", "--answer", "no")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("setup question observer-level is answered no", done.stdout)
        self.assertEqual({LEVEL: self.entry("no")}, self.stored()["repos"][REPO])
        self.assertNotIn(LEVEL, self.open_ids())
        reset = self.tool("setup.py", "answer", "--id", "observer", "--answer", "reset")
        self.assertEqual(0, reset.returncode, reset.stdout + reset.stderr)
        self.assertIn(LEVEL, self.open_ids())

    def test_a_reset_also_takes_away_the_answer_kept_under_the_retired_id(self):
        self.put_state(repos={REPO: {"observer": self.entry("no")}})
        self.assertNotIn(LEVEL, self.open_ids())
        done = self.tool("setup.py", "answer", "--id", LEVEL, "--answer", "reset")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertNotIn("observer", self.stored()["repos"].get(REPO, {}))
        self.assertIn(LEVEL, self.open_ids())

    def test_the_question_names_the_levels_the_limits_and_never_the_papers_text_on_its_own(self):
        done = self.tool("setup.py", "questions", "--json")
        question = next(q for q in json.loads(done.stdout)["open"] if q["id"] == LEVEL)
        caps = observer_runtime.DEFAULTS
        for words in ("Notes only", "Assistant", "Everything", "never writes the paper's text on its own",
                      "at most %d quick sorting calls, %d note-taking calls and %d paper-text calls a day"
                      % (caps["max_sort"], caps["max_invocations"], caps["max_strong"]),
                      "count against your usage", "trusted list", "stays paused until you choose"):
            self.assertIn(words, question["question"])
        self.assertEqual(["Everything (Recommended)", "Assistant", "Notes only", "Later"],
                         [o["label"] for o in question["options"]])   # four: the most the prompt takes
        self.assertEqual([True, False, False, False], [o["recommended"] for o in question["options"]])
        self.assertEqual(["yes", "yes", "yes", "later"], [o["answer"] for o in question["options"]])
        for option, level in zip(question["options"], ("everything", "assistant", "notes")):
            dry, real = option["commands"]
            self.assertTrue(dry.endswith("setup.py observer --level %s --dry-run" % level), dry)   # the dry run first
            self.assertTrue(real.endswith("setup.py observer --level %s --yes" % level), real)
            self.assertIn('"shadow" turns it on at Notes only without posting', option["note"])
            self.assertIn("the user may name other models", option["note"])
        self.assertNotIn("(Recommended)", question["question"])

    def test_it_is_not_asked_when_the_project_file_switches_the_observer_off_or_the_user_said_no(self):
        self.project_file(observer=False)
        self.assertNotIn(LEVEL, self.open_ids())
        self.project_file()
        self.assertEqual(0, self.tool("setup.py", "answer", "--id", LEVEL, "--answer", "no").returncode)
        self.assertNotIn(LEVEL, self.open_ids())


class PaperProposalsTests(Programs, tq.Box):
    """`paper-proposals`: asked only under its four conditions."""

    KIND = "plan"
    ID = "paper-proposals"

    def ready(self, mode="review", level="everything", **settings):
        self.assertEqual(0, self.tool("setup.py", "writing", "set", "--mode", mode, "--yes").returncode)
        self.approval(level=level, **settings)

    def asked(self):
        return self.ID in self.open_ids()

    def test_it_is_asked_when_all_four_conditions_hold(self):
        self.ready()
        self.assertTrue(self.asked())
        self.assertIn(self.ID, self.groups()["open"])
        self.assertEqual("1.5.0", setup_questions.find(self.ID).since)

    def test_it_is_asked_in_autonomous_mode_and_at_the_level_assistant(self):
        self.ready(mode="autonomous", level="assistant")
        self.assertTrue(self.asked())

    def test_without_a_writing_mode_or_with_it_off_it_is_not_asked(self):
        self.approval(level="everything")
        self.assertFalse(self.asked())   # no mode chosen
        self.assertIn(self.ID, self.groups()["not-asked"])
        self.assertEqual(0, self.tool("setup.py", "writing", "set", "--mode", "off", "--yes").returncode)
        self.assertFalse(self.asked())   # off: nothing is drafted, so nothing is proposed

    def test_an_unreadable_writing_file_means_it_is_not_asked(self):
        self.approval(level="everything")
        tq.put_json(os.path.join(self.config_private, "writing-modes.json"), None, raw="{nope")
        self.assertFalse(self.asked())

    def test_at_the_level_notes_or_with_no_level_it_is_not_asked(self):
        self.ready(level="notes")
        self.assertFalse(self.asked())
        self.approval()   # an approval of 1.4.0: no level
        self.assertFalse(self.asked())
        os.unlink(os.path.join(self.dir, "xdg-state", "recortex", "private", "observer-approvals.json"))
        self.assertFalse(self.asked())   # no approval at all

    def test_with_a_strong_model_set_it_is_settled(self):
        self.ready(strong_model="opus")
        self.assertFalse(self.asked())
        self.assertIn(self.ID, self.groups()["answered"])

    def test_when_the_project_file_switches_the_observer_off_it_is_not_asked(self):
        self.ready()
        self.project_file(observer=False)
        self.assertFalse(self.asked())

    def test_the_question_says_what_it_does_and_has_three_options(self):
        self.ready()
        done = self.tool("setup.py", "questions", "--json")
        question = next(q for q in json.loads(done.stdout)["open"] if q["id"] == self.ID)
        caps = observer_runtime.DEFAULTS
        self.assertEqual("May recortex draft a change of the paper's text in the background when something notable changes "
                         "for a part, and ask you before anything is written? It uses a stronger model (opus), at most "
                         "%d calls a day, and proposes at most %d changes a day." % (caps["max_strong"], caps["max_proposals"]),
                         question["question"])
        self.assertEqual(["Yes (Recommended)", "No", "Later"], [o["label"] for o in question["options"]])
        dry, real = question["options"][0]["commands"]
        self.assertTrue(dry.endswith("setup.py observer --strong opus --dry-run"), dry)
        self.assertTrue(real.endswith("setup.py observer --strong opus --yes"), real)
        self.assertEqual([], question["options"][1]["commands"])   # a No runs nothing


class StepTests(Programs, tq.Box):
    """`setup.py observer --level L` and `--strong M`, with a stand-in for the model's program."""

    def setUp(self):
        super().setUp()
        self.bin, self.log = self.stand_in()
        self.env_claude = self.with_path(self.bin)

    def observer(self, *args, env=None):
        if "--yes" in args:
            self.answer(args[args.index("--level") + 1] if "--level" in args else None,
                        args[args.index("--strong") + 1] if "--strong" in args else None)
        return self.tool("setup.py", "observer", *args, env=dict(self.env_claude, **(env or {})))

    def test_the_dry_run_prints_the_steps_and_makes_no_write_and_no_model_call(self):
        before = self.tree(self.dir)
        done = self.observer("--level", "everything", "--dry-run")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        steps = [line for line in done.stdout.splitlines() if line.startswith("would run: ")]
        script = os.path.join(ts.SCRIPTS, "observer.py")
        self.assertEqual(3, len(steps), done.stdout)
        self.assertIn("%s %s --repo %s enable --mode shadow --model sonnet --sort-model haiku --level everything --yes"
                      % (sys.executable, script, REPO), steps[0])
        self.assertIn("runtime-check", steps[1])
        self.assertIn("haiku, sonnet", steps[1])   # one check for each model
        self.assertIn("enable --mode automatic --model sonnet --sort-model haiku --level everything --yes", steps[2])
        self.assertEqual(before, self.tree(self.dir))
        self.assertEqual([], self.calls(self.log))

    def test_the_dry_run_makes_no_github_write(self):
        before = len(self.repo()["log"])
        self.assertEqual(0, self.observer("--level", "assistant", "--dry-run").returncode)
        self.assertEqual(before, len(self.repo()["log"]))

    def test_yes_runs_the_checks_for_each_model_and_leaves_a_working_approval_at_the_level(self):
        done = self.observer("--level", "everything", "--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("observer: on at the level Everything in automatic mode, with sonnet: it posts reviewed memories on "
                      "its own", done.stdout)
        self.assertEqual(["haiku", "sonnet"], sorted(self.models_of(self.log)))   # one call for each distinct model
        data, settings = self.stored_settings()
        self.assertEqual(2, data["form"])
        self.assertEqual(("everything", "automatic", "sonnet", "haiku", ""),
                         (settings["level"], settings["mode"], settings["model"], settings["sort_model"],
                          settings["strong_model"]))
        status = self.status(env=self.env_claude)
        self.assertEqual((True, False, "Everything"), (status["approved"], status["paused"], status["level_label"]))
        self.assertEqual([True, True], [status["tiers"][t]["gate"] for t in ("sort", "middle")])
        self.assertFalse(status["tiers"]["strong"]["configured"])   # the keeper is off until paper-proposals is answered
        self.assertNotIn(LEVEL, self.open_ids())

    def test_a_folder_in_shadow_mode_stays_in_shadow_mode(self):
        self.assertEqual(0, self.observer("--level", "notes", "--mode", "shadow", "--yes").returncode)
        self.assertEqual("shadow", self.stored_settings()[1]["mode"])
        done = self.observer("--level", "assistant", "--yes")   # no --mode: the folder's own stays
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(2, len([l for l in done.stdout.splitlines() if l.startswith("run: ")]))   # shadow and the check
        self.assertIn("in shadow mode", done.stdout)
        settings = self.stored_settings()[1]
        self.assertEqual(("shadow", "assistant"), (settings["mode"], settings["level"]))

    def test_a_1_4_0_shadow_approval_stays_shadow_when_the_user_answers(self):
        self.approval(observer_mode="shadow")   # enabled by 1.4.0, in shadow mode: paused until a level is chosen
        dry = self.observer("--level", "assistant", "--dry-run")
        self.assertEqual(2, dry.stdout.count("would run: "))   # shadow mode: no third step that switches posting on
        self.assertEqual(0, self.observer("--level", "assistant", "--yes").returncode)
        self.assertEqual(("shadow", "assistant"), tuple(self.stored_settings()[1][k] for k in ("mode", "level")))
        self.assertNotIn(LEVEL, self.open_ids())

    def test_the_models_can_be_named(self):
        done = self.observer("--level", "notes", "--model", "opus", "--sort-model", "sonnet", "--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        settings = self.stored_settings()[1]
        self.assertEqual(("opus", "sonnet"), (settings["model"], settings["sort_model"]))
        self.assertEqual(["opus", "sonnet"], sorted(self.models_of(self.log)))

    def test_it_makes_no_change_for_a_repository_that_is_not_trusted(self):
        with open(self.trusted, "w") as handle:
            handle.write("")
        done = self.observer("--level", "everything", "--yes")
        self.assertNotEqual(0, done.returncode)
        self.assertEqual([], self.calls(self.log))

    def test_a_check_that_fails_leaves_shadow_mode_on_and_says_what_to_run(self):
        broken = os.path.join(self.dir, "bin-broken")
        program(os.path.join(broken, "claude"), "#!/bin/sh\nexit 1\n")
        done = self.observer("--level", "everything", "--yes", env=self.with_path(broken))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("observer: taking notes in shadow mode, but reviewing and posting nothing yet (the check failed). Run "
                      "observer.py runtime-check; then observer.py enable --mode automatic --model sonnet --sort-model "
                      "haiku --level everything --yes.", done.stdout)
        settings = self.stored_settings()[1]
        self.assertEqual(("shadow", "everything"), (settings["mode"], settings["level"]))

    def test_the_command_checks_its_words(self):
        for args in (("--dry-run",), ("--level", "most", "--dry-run"), ("--level", "notes"), ("--level", "notes", "--yes", "--dry-run"),
                     ("--level", "notes", "--model", "two words", "--dry-run"), ("--level", "notes", "--mode", "off", "--dry-run"),
                     ("--strong", "two words", "--dry-run")):
            self.assertNotEqual(0, self.observer(*args).returncode, args)
        self.assertEqual([], self.calls(self.log))

    # ---- --strong: the paper-proposals step

    def test_strong_needs_a_level_of_assistant_or_everything_first(self):
        for words in (None, "notes"):
            if words:
                self.assertEqual(0, self.observer("--level", words, "--yes").returncode)
            done = self.observer("--strong", "opus", "--dry-run")
            self.assertNotEqual(0, done.returncode, words)
            self.assertIn("choose the observer's level first", done.stderr + done.stdout)

    def test_strong_keeps_the_level_the_mode_and_the_models_and_checks_the_new_one(self):
        self.assertEqual(0, self.observer("--level", "assistant", "--model", "sonnet", "--yes").returncode)
        open(self.log, "w").close()
        dry = self.observer("--strong", "opus", "--dry-run")
        self.assertEqual(0, dry.returncode, dry.stdout + dry.stderr)
        self.assertEqual(3, dry.stdout.count("would run: "))
        self.assertIn("--level assistant --strong-model opus --yes", dry.stdout)
        self.assertEqual([], self.calls(self.log))   # the dry run made no call
        done = self.observer("--strong", "opus", "--yes")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("with sonnet and opus for the paper's text", done.stdout)
        self.assertIn("opus", self.models_of(self.log))
        settings = self.stored_settings()[1]
        self.assertEqual(("assistant", "automatic", "sonnet", "haiku", "opus"),
                         tuple(settings[k] for k in ("level", "mode", "model", "sort_model", "strong_model")))
        status = self.status(env=self.env_claude)
        self.assertEqual([True, True, True], [status["tiers"][t]["gate"] for t in ("sort", "middle", "strong")])
        self.assertEqual("opus", status["tiers"]["strong"]["model"])
        self.assertTrue(status["approved"])

    def test_strong_in_a_shadow_folder_stays_in_shadow_mode(self):
        self.assertEqual(0, self.observer("--level", "everything", "--mode", "shadow", "--yes").returncode)
        self.assertEqual(0, self.observer("--strong", "opus", "--yes").returncode)
        settings = self.stored_settings()[1]
        self.assertEqual(("shadow", "everything", "opus"), (settings["mode"], settings["level"], settings["strong_model"]))

    def test_answering_the_level_again_keeps_the_strong_model_unless_the_level_drops_to_notes(self):
        self.assertEqual(0, self.observer("--level", "assistant", "--yes").returncode)
        self.assertEqual(0, self.observer("--strong", "opus", "--yes").returncode)
        self.assertEqual(0, self.observer("--level", "everything", "--yes").returncode)
        self.assertEqual("opus", self.stored_settings()[1]["strong_model"])
        self.assertEqual(0, self.observer("--level", "notes", "--yes").returncode)
        self.assertEqual("", self.stored_settings()[1]["strong_model"])   # the keeper stays off at Notes only


class PlanStepTests(Programs, tq.Box):
    """The same step in a project with a plan: after `--strong`, the question is settled."""

    KIND = "plan"

    def test_the_strong_step_settles_paper_proposals(self):
        bin_folder, log = self.stand_in()
        env = self.with_path(bin_folder)
        self.assertEqual(0, self.tool("setup.py", "writing", "set", "--mode", "review", "--yes").returncode)
        self.answer(level="everything")
        self.assertEqual(0, self.tool("setup.py", "observer", "--level", "everything", "--yes", env=env).returncode)
        self.assertIn("paper-proposals", self.open_ids())
        self.answer(strong="opus")
        self.assertEqual(0, self.tool("setup.py", "observer", "--strong", "opus", "--yes", env=env).returncode)
        self.assertNotIn("paper-proposals", self.open_ids())
        self.assertIn("paper-proposals", self.groups()["answered"])


class PausedUntilALevelTests(Programs, tq.Box):
    """The owner's answer 2. An observer enabled before 1.5.0 runs no observer call after the update until the user
    answers the level question, and the brief's setup line counts the question."""

    def observer_hook(self, event, payload, env=None):
        full = ts.environment_for(os.environ, {"RECORTEX_GH": ts.FAKE, "FAKE_GH_STATE": self.state,
                                               "PYTHONDONTWRITEBYTECODE": "1"})
        full.update(XDG_CACHE_HOME=os.path.join(self.dir, "xdg-cache"), XDG_STATE_HOME=os.path.join(self.dir, "xdg-state"),
                    XDG_CONFIG_HOME=os.path.join(self.dir, "xdg-config"))
        full = ts.overlay(full, self.env, env)
        return subprocess.run(["bash", os.path.join(ts.SCRIPTS, "run_observer_hook.sh"), event], capture_output=True,
                              text=True, env=full, cwd=self.project, input=json.dumps(dict(payload, cwd=self.project)))

    def test_a_1_4_0_approval_with_the_observer_on_runs_no_call_until_a_level_is_answered(self):
        recording, log = self.stand_in("recording")
        env = self.with_path(recording)
        self.approval(observer_mode="automatic")   # what 1.4.0's `enable --yes` stored: the observer is on, there is no level
        with open(os.path.join(self.dir, "xdg-state", "recortex", "private", "observer-approvals.json")) as handle:
            self.assertEqual(1, json.load(handle)["form"])
        # the hooks of the observer: nothing is captured, nothing is started
        for event, payload in (("session-start", {"hook_event_name": "SessionStart", "session_id": "s1"}),
                               ("prompt-submit", {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hello"}),
                               ("stop", {"hook_event_name": "Stop", "session_id": "s1"})):
            done = self.observer_hook(event, payload, env=env)
            self.assertEqual(0, done.returncode, (event, done.stderr))
        # the worker and the runtime check
        for args in (("run-worker",), ("runtime-check",)):
            self.tool("observer.py", "--repo", self.repo_name, *args, env=env)
        self.assertEqual([], self.calls(log))   # no observer call of any tier
        status = self.status(env=env)
        self.assertEqual((False, True), (status["approved"], status["paused"]))
        queue = status["queue"]   # nothing was captured, no event was made and no call was counted
        self.assertEqual(({}, {}, 0), (queue.get("observations", {}), queue.get("events", {}), queue.get("invocations_24h", 0)))
        # the doctor and the brief say so, and the brief's setup line counts the question
        self.assertIn("paused until you choose a level", self.tool("hooks.py", "--doctor").stdout)
        self.assertEqual([brief.SETUP_QUESTIONS % (3, "s")], self.lines(self.start()))
        self.assertIn(LEVEL, self.open_ids())
        # the user answers with a level: now the observer works
        bin_folder, working_log = self.stand_in()
        self.answer(level="notes")
        done = self.tool("setup.py", "observer", "--level", "notes", "--yes", env=self.with_path(bin_folder))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        status = self.status(env=self.with_path(bin_folder))
        self.assertEqual((True, False, "notes"), (status["approved"], status["paused"], status["level"]))
        self.assertNotIn(LEVEL, self.open_ids())
        self.assertEqual([brief.SETUP_QUESTIONS % (2, "s")], self.lines(self.start()))   # the status line and the task reminder
        self.assertEqual([], self.calls(log))   # the paused stretch made no call; the answer's checks used the other program
        self.assertTrue(self.calls(working_log))

    def test_the_user_who_says_no_leaves_the_observer_paused_and_is_not_asked_again(self):
        self.approval(observer_mode="automatic")
        self.assertEqual(0, self.tool("setup.py", "answer", "--id", LEVEL, "--answer", "no").returncode)
        self.assertNotIn(LEVEL, self.open_ids())
        self.assertTrue(self.status()["paused"])   # a no is a no: it stays paused, and asks nothing


class DocsTests(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_reference_has_a_row_for_each_new_question_and_says_the_old_one_is_retired(self):
        text = self.read("skills", "recortex", "references", "setup.md")
        table = text.split("## Questions added after setup", 1)[1]
        for question_id in ("observer-level", "paper-proposals"):
            self.assertIn("| `%s` | 1.5.0 |" % question_id, table)
        self.assertNotIn("| `observer` | 1.1.0 |", table)
        self.assertIn("setup.py observer --level L --dry-run", table)
        self.assertIn("setup.py observer --strong opus --dry-run", table)
        self.assertIn("paused", table)
        self.assertIn("`setup.py answer --id observer` is accepted and recorded under `observer-level`", table)

    def test_the_skill_says_a_paused_observer_waits_for_the_level_answer(self):
        skill = self.read("skills", "recortex", "SKILL.md")
        self.assertIn("stays paused until the user answers the level question", skill)
        self.assertIn("approves that question's step", skill)


if __name__ == "__main__":
    unittest.main()

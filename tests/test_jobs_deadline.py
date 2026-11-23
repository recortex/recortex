"""The worker's time (M1 of the 1.5.0 review): the keeper leaves a strong call for the next pass when too little of the
worker's life is left, a call the alarm cut is charged its whole room to the strong tier's budget, and the housekeeping jobs
have a deadline of their own. Offline, with a fake clock."""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_jobs as tjj  # noqa: E402
import test_jobs_keeper as tk  # noqa: E402
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import observer_jobs as jobs  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import observer_store as store_module  # noqa: E402
import observer_worker as worker  # noqa: E402


class Fake:
    """A clock's `now`, moved by the test."""

    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class ClockTests(unittest.TestCase):
    def test_remaining_counts_down_and_arm_sets_what_is_left_from_now(self):
        now = Fake()
        clock = worker.Clock(100, now=now)
        self.assertEqual(100, clock.remaining())
        now.value += 30
        self.assertEqual(70, clock.remaining())
        clock.arm(120)
        self.assertEqual(120, clock.remaining())
        now.value += 121
        self.assertLess(clock.remaining(), 0)

    def test_an_unlimited_clock_never_runs_out_and_sets_no_alarm(self):
        with mock.patch("signal.setitimer") as alarm:
            clock = worker.Clock()
            self.assertEqual(float("inf"), clock.remaining())
            clock.arm(5)
            alarm.assert_not_called()   # not armed: a test or a caller with no alarm of its own

    def test_an_armed_clock_sets_the_alarm_each_time(self):
        with mock.patch("signal.setitimer") as alarm:
            clock = worker.Clock(250, armed=True)
            clock.arm(120)
        self.assertEqual([mock.call(__import__("signal").ITIMER_REAL, 250), mock.call(__import__("signal").ITIMER_REAL, 120)],
                         alarm.call_args_list)

    def test_a_worker_lives_the_passes_time_and_with_a_strong_model_the_keepers_too(self):
        self.assertEqual(250, worker.lifetime({"strong_model": ""}))
        self.assertEqual(250 + 2 * 300 + 60, worker.lifetime({"strong_model": "opus"}))   # the strong tier keeps its own 300 s
        self.assertEqual(250 + 2 * 60 + 60, worker.lifetime({"strong_model": "opus", "timeout_seconds": 60}))


class KeeperTimeTests(tk.KeeperBox):
    def clock(self, left):
        now = Fake()
        clock = worker.Clock(left, now=now)
        return clock

    def need(self):
        return 2 * runtime.call_timeout(self.runtime.settings({"observer": self.options}), "strong") + 60

    def test_with_too_little_time_left_no_strong_call_is_made_or_reserved(self):
        self.assertEqual("unstable", self.consider()["code"])   # the row is seen
        second = self.consider(now=self.t0 + 31 * 60, clock=self.clock(self.need() - 1))
        self.assertEqual("time", second["code"], second)
        self.assertEqual([], self.calls)
        with self.open_store() as store:
            self.assertEqual(0, store.db.execute("SELECT COUNT(*) FROM invocations").fetchone()[0])
        # the next pass, with its time, drafts the same part
        third = self.consider(now=self.t0 + 32 * 60, clock=self.clock(self.need()))
        self.assertEqual("drafted", third["code"], third)

    def test_a_cut_strong_call_is_charged_its_whole_room(self):
        worst = self.runtime.worst_case_usage(self.runtime.settings({"observer": self.options}), "strong")
        self.assertEqual({"input_tokens": 40000, "output_tokens": 40000}, worst)
        for code in ("interrupted", "timeout"):
            self.setUp()   # a fresh store and folders for each
            self.cut = code
            real = self.invoke

            def invoke(payload, schema, purpose, options, real=real):
                if purpose == "text":
                    self.calls.append((purpose, payload))
                    return {"ok": False, "code": self.cut, "usage": {}, "structured": None}
                return real(payload, schema, purpose, options)
            self.invoke = invoke
            self.consider()
            outcome = self.consider(now=self.t0 + 31 * 60)
            self.assertEqual("call_failed", outcome["code"], outcome)
            with self.open_store() as store:
                strong = store.usage_today()["strong"]
            self.assertEqual((1, 80000), (strong["calls"], strong["input_tokens"] + strong["output_tokens"]), code)

    def test_a_call_that_failed_for_another_reason_is_charged_what_it_reported(self):
        real = self.invoke

        def invoke(payload, schema, purpose, options):
            if purpose == "text":
                self.calls.append((purpose, payload))
                return {"ok": False, "code": "runtime-failed", "usage": {}, "structured": None}
            return real(payload, schema, purpose, options)
        self.invoke = invoke
        self.consider()
        self.assertEqual("call_failed", self.consider(now=self.t0 + 31 * 60)["code"])
        with self.open_store() as store:
            strong = store.usage_today()["strong"]
        self.assertEqual((1, 0), (strong["calls"], strong["input_tokens"] + strong["output_tokens"]))

    def test_the_alarm_that_cuts_a_call_is_a_result_not_an_error_so_the_worker_goes_on_to_the_jobs(self):
        options = self.runtime.settings({"observer": self.options})
        with mock.patch.object(self.runtime.shutil, "which", return_value="/usr/bin/true"), \
                mock.patch.object(self.runtime, "_run", side_effect=TimeoutError("deadline")), \
                mock.patch.object(self.runtime, "gate_valid", return_value=True):
            out = self.runtime.invoke({"x": 1}, self.keeper.KEEPER_DRAFT, "text",
                                      dict(options, runtime_gate_strong={"actual_model": "x"}))
        self.assertEqual("interrupted", out["code"])
        self.assertIn("interrupted", worker.TRANSIENT)


class JobsDeadlineTests(tjj.JobsBox):
    def run_jobs(self, clock, **kw):
        self.use()
        options = runtime.settings(self.pcfg)
        with store_module.Store(tjj.REPO, config=self.pcfg) as db:
            return jobs.run(tjj.REPO, self.pcfg, db, options, refresh=lambda: True, posted=True, clock=clock, **kw)

    def test_a_worker_that_lost_its_host_lease_runs_no_step_that_writes(self):
        asked = []
        steps = self.run_jobs(None, holds=lambda: asked.append(1) or False)
        self.assertEqual({"render": "lease", "close": "lease", "hub": "lease", "links": "lease", "board": "lease"},
                         {key: steps[key] for key in ("render", "close", "hub", "links", "board")})
        self.assertEqual([], self.writes())
        self.assertEqual(5, len(asked))   # asked before each of the five steps that can write to GitHub

    def test_a_lease_that_runs_out_during_a_step_stops_the_steps_after_it(self):
        left = [True, False]
        steps = self.run_jobs(None, holds=lambda: left.pop(0) if left else False)
        self.assertEqual("ok", steps["render"])
        self.assertEqual({"close": "lease", "hub": "lease", "links": "lease", "board": "lease"},
                         {key: steps[key] for key in ("close", "hub", "links", "board")})
        self.assertTrue(all(tuple(call[:2]) != ("issue", "close") for call in self.writes()))

    def test_a_lease_that_holds_changes_nothing(self):
        self.assertEqual("ok", self.run_jobs(None, holds=lambda: True)["render"])

    def test_with_no_time_left_no_step_runs_and_nothing_is_written(self):
        steps = self.run_jobs(worker.Clock(-1, now=Fake()))
        self.assertEqual({"sync": "ok", "keeper": "time", "render": "time", "close": "time", "hub": "time", "links": "time",
                          "board": "time"}, steps)   # keeper: the cleanup of 1.5.1, a step like the others
        self.assertEqual([], self.writes())

    def test_a_step_that_runs_past_the_deadline_stops_the_ones_after_it(self):
        now = Fake()
        clock = worker.Clock(10, now=now)
        real = jobs.render_step

        def slow(*args):
            out = real(*args)
            now.value += 20   # the render took longer than the jobs may
            return out
        with mock.patch.object(jobs, "render_step", slow):
            steps = self.run_jobs(clock)
        self.assertEqual("ok", steps["render"])
        self.assertEqual({"close": "time", "hub": "time", "links": "time", "board": "time"},
                         {key: steps[key] for key in ("close", "hub", "links", "board")})

    def test_without_a_clock_every_step_runs_as_before(self):
        self.assertEqual("ok", self.run_jobs(None)["render"])


class WorkerWiringTests(tjj.JobsBox):
    def test_the_keeper_gets_the_clock_and_the_jobs_a_deadline_of_their_own(self):
        import observer_worker
        self.use()
        seen = {}
        now = Fake()
        clock = worker.Clock(900, now=now)

        def keeper(*args, **kw):
            seen["keeper"] = kw.get("clock")
            now.value += 800   # a long pass: almost nothing is left for the jobs
            return {"code": "time"}

        def run(*args, **kw):
            seen["jobs"] = kw.get("clock"), kw["clock"].remaining()
            return {}
        with mock.patch.object(observer_worker.observer_keeper, "consider", side_effect=keeper), \
                mock.patch.object(observer_worker.observer_jobs, "run", side_effect=run):
            observer_worker.run_once(tjj.REPO, self.pcfg, project_root=self.project, clock=clock)
        self.assertIs(clock, seen["keeper"])
        self.assertIs(clock, seen["jobs"][0])
        self.assertEqual(observer_worker.JOBS_SECONDS, seen["jobs"][1])   # set again for the jobs, whatever was left


class RunWorkerTests(unittest.TestCase):
    def test_the_alarm_of_run_worker_is_the_workers_lifetime(self):
        with open(os.path.join(ts.SCRIPTS, "observer.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("observer_worker.Clock(observer_worker.lifetime(options), armed=True)", text)
        self.assertNotIn("setitimer(signal.ITIMER_REAL, 250)", text)


if __name__ == "__main__":
    unittest.main()

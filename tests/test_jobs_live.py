"""A stretch the first 1.5.0 wording of the models left unposted: the user types a result and a failure of their own runs
and asks the agent to just acknowledge them, the agent says it posted nothing as the user asked earlier, and nothing
else of the stretch was recorded by the main session. The answers of the models are the ones real Haiku (sort) and
Sonnet (draft, review) calls gave to this invented text with the corrected wording (tests/fixtures/observer-stretches/
u01.json); the pass is replayed through the real worker and publisher against the fake gh. The models are not run here:
the tests hold what the pass does with answers like theirs, and that the prompts keep the lines the answers rest on."""

import contextlib
import copy
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_jobs_jobs as tjj  # noqa: E402
import test_jobs_publisher as tp  # noqa: E402
import test_jobs_tasks as tj  # noqa: E402
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import observer  # noqa: E402
import observer_holds  # noqa: E402
import observer_jobs as jobs  # noqa: E402
import observer_keeper  # noqa: E402
import observer_publisher as publisher  # noqa: E402
import observer_runtime as runtime  # noqa: E402
import observer_store as store  # noqa: E402
import observer_worker as worker  # noqa: E402
import record_format  # noqa: E402

REPO = ts.REPO
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "observer-stretches", "u01.json")
PAIR = os.path.join(ROOT, "tests", "fixtures", "observer-stretches", "u02.json")
RELEASED = os.path.join(ROOT, "tests", "fixtures", "observer-stretches", "u03.json")


class LiveBox(tp.PublisherBox):
    """The example record with the observer approved at the level Everything in automatic mode, and the stretch
    captured as the hooks leave it: eight messages, in the order they happened."""

    fixture, session = FIXTURE, "sess-u01"

    def setUp(self):
        super().setUp()
        with open(self.fixture, encoding="utf-8") as handle:
            self.data = json.load(handle)
        self.by_id = {s["id"]: dict(s, subagent=False, sidechain=False, control_user=False, message_id=None)
                      for s in self.data["sources"]}

    def capture(self, session=None):
        session = session or self.session
        now = time.time() - 60
        rows = [{"source_id": s["id"], "session_id": session, "role": s["role"], "event_type": "Stop",
                 "path": "/example/session.jsonl", "offset": i * 1000, "end": i * 1000 + 900, "sha256": "0" * 64,
                 "content_sha256": "1" * 64, "observed_at": now + i} for i, s in enumerate(self.data["sources"])]
        with store.Store(REPO, config=self.pcfg) as db:
            db.capture(rows)

    def models(self, sort=None, draft=None, review=None, calls=None):
        """The calls of the pass, answered with what the real models answered (or what a test gives)."""
        answers = {"sort": sort or self.data["sort"], "draft": {"candidates": draft if draft is not None else self.data["draft"]}}
        verdicts = list(self.data["reviews"] if review is None else review)

        def invoke(payload, schema, purpose, config):
            if calls is not None:
                calls.append((purpose, payload))
            if purpose in answers:
                return {"ok": True, "structured": answers[purpose]}
            if purpose == "review":
                return {"ok": True, "structured": dict(verdicts.pop(0), candidate_hash=payload["candidate_hash"])}
            return {"ok": False, "code": "runtime-failed", "structured": None}   # the status summary is not part of this
        return invoke

    def run_pass(self, **models):
        self.ready()
        self.capture()
        calls = models.setdefault("calls", [])
        with mock.patch.object(worker.sources, "resolve", side_effect=lambda meta, root: self.by_id[meta["source_id"]]):
            report = worker.run_once(REPO, self.pcfg, self.project, self.models(**models))
        return report, [purpose for purpose, _ in calls], calls

    def posted(self):
        """The comments the pass wrote to the record, with the observer's event key in their marker."""
        return [(c["issue"], c["body"]) for _, c in sorted(self.repo()["comments"].items(), key=lambda row: int(row[0]))
                if "eventkey=" in c["body"]]


class UserFindingTests(LiveBox):
    def test_a_result_and_a_failure_the_user_typed_are_posted_at_everything(self):
        report, purposes, calls = self.run_pass()
        self.assertEqual(["sort", "draft", "review", "review"], [p for p in purposes if p != "summary"][:4])
        self.assertEqual((2, 2, 4), (report["labels"], report["reviewed"], report["ignored"]))
        self.assertEqual(2, len(report["ready"]))
        posted = self.posted()
        self.assertEqual([(6, "result"), (6, "failure")], [(issue, body.split("type=")[1].split(" ")[0]) for issue, body in posted])
        for _, body in posted:
            self.assertIn("the agent did not run it", body)   # the user's run is told as the user's
        with store.Store(REPO, config=self.pcfg) as db:
            self.assertEqual(["verified", "verified"], sorted(db.event(key)["state"] for key in report["ready"]))
            first, second = (db.event(key)["candidate"] for key in report["ready"])
        ids = [s["id"] for s in self.data["sources"]]
        self.assertEqual([[ids[0], ids[1]], [ids[2], ids[3]]], [sorted(c["sources"], key=ids.index) for c in (first, second)])
        self.assertEqual(["ignored"] * 4, [db_state for db_state in self.states(ids[4:])])   # the rest was set aside

    def states(self, ids):
        with store.Store(REPO, config=self.pcfg) as db:
            return [db.observation(sid)["state"] for sid in ids]

    def test_both_memories_cite_the_users_message_and_the_agents_message(self):
        report, purposes, calls = self.run_pass()
        draft = next(payload for purpose, payload in calls if purpose == "draft")
        self.assertEqual([[1, 2], [3, 4]], [label["sources"] for label in draft["labels"]])
        self.assertEqual([True, False, True, False], [view["direct_user"] for view in draft["sources"]][:4])
        with store.Store(REPO, config=self.pcfg) as db:
            for key in report["ready"]:
                roles = {self.by_id[sid]["role"] for sid in db.event(key)["candidate"]["sources"]}
                self.assertEqual({"user", "assistant"}, roles)   # a result needs the agent's message, which the user's backs

    def test_a_finding_the_user_typed_needs_the_agents_message_before_it_is_posted(self):
        # The user's own words alone are not a result: code asks for an agent message among the cited ones.
        cut = copy.deepcopy(self.data["draft"])
        for candidate in cut:
            candidate["sources"] = [candidate["sources"][0]]
        report, purposes, calls = self.run_pass(draft=cut)
        self.assertEqual((["sort", "draft"], []), (purposes[:2], report["ready"]))
        self.assertEqual([], self.posted())

    def at_level(self, level):
        self.use(level=level)
        report, purposes, calls = self.run_pass()
        self.assertEqual(2, len(report["ready"]), level)
        self.assertEqual(2, len(self.posted()), level)

    def test_the_level_notes_only_posts_the_two_memories_too(self):
        self.at_level("notes")

    def test_the_level_assistant_posts_the_two_memories_too_and_asks_about_nothing(self):
        self.at_level("assistant")
        with store.Store(REPO, config=self.pcfg) as db:
            self.assertEqual([], db.waiting())

    def test_the_session_is_not_posted_when_the_user_typed_that_it_is_off_the_record(self):
        # The same stretch with a typed request in it: the hold the hook records for the phrase withholds the session
        # before any call is made, as before.
        text = "For this session do not post anything to the record."
        first = self.data["sources"][0]
        self.by_id[first["id"]]["text"] = text
        self.ready()
        self.capture()
        hold = observer_holds.make_hold({"source_id": first["id"], "role": "user", "direct_user": True, "text": "do not post"},
                                        REPO, "publish", {"session_id": "sess-u01"}, None)
        with store.Store(REPO, config=self.pcfg) as db:
            db.save_holds([hold])
        calls = []
        with mock.patch.object(worker.sources, "resolve", side_effect=lambda meta, root: self.by_id[meta["source_id"]]):
            report = worker.run_once(REPO, self.pcfg, self.project, self.models(calls=calls))
        self.assertEqual(([], []), ([p for p, _ in calls if p in ("sort", "draft", "review")], report["ready"]))
        self.assertEqual([], self.posted())
        with store.Store(REPO, config=self.pcfg) as db:
            self.assertEqual({"withheld"}, {db.observation(s["id"])["reason"] for s in self.data["sources"]})


class PairTests(LiveBox):
    """A message that holds a failure and the decision it led to (tests/fixtures/observer-stretches/u02.json): the sorter
    labels both, the drafter writes the failure and then the decision, whose `because` is the failure's place in its list,
    and code writes `because=<the failure's comment>` into the decision's marker once the failure is posted. The decision
    rests on the user's typed words as ever; the failure on the agent's message that takes the finding in."""

    fixture, session = PAIR, "sess-u02"

    def comments(self):
        """[(comment id, issue, body)] of what the pass wrote, in the order it was written."""
        return [(int(cid), c["issue"], c["body"]) for cid, c in sorted(self.repo()["comments"].items(), key=lambda row: int(row[0]))
                if "eventkey=" in c["body"]]

    def marker(self, body):
        return record_format.parse_marker(record_format.first_lines(body)[0])

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(observer, "on_terminal", return_value=True):
            code = observer.main(list(args))
        return code, out.getvalue()

    def queued(self):
        """The pass run in shadow mode: (the failure's key, the decision's key), both ready and none posted."""
        self.use(mode="shadow")
        report, _, _ = self.run_pass()
        failure, decision = report["ready"]
        self.assertEqual([], self.comments())
        self.use()   # the publisher is run by hand from here, in automatic mode
        return failure, decision

    def test_the_failure_and_then_the_decision_it_led_to_are_posted_and_linked_at_everything(self):
        report, purposes, calls = self.run_pass()
        self.assertEqual(["sort", "draft", "review", "review"], [p for p in purposes if p != "summary"][:4])
        self.assertEqual((2, 2, 6), (report["labels"], report["reviewed"], report["ignored"]))
        posted = self.comments()
        self.assertEqual(["failure", "decision"], [self.marker(body)["type"] for _, _, body in posted])
        (failure_id, failure_issue, failure), (_, decision_issue, decision) = posted
        self.assertEqual((6, 6), (failure_issue, decision_issue))
        self.assertNotIn("because", self.marker(failure))
        self.assertEqual(str(failure_id), self.marker(decision)["because"])   # written by code: the model wrote no because=
        self.assertIn("The user reported the run; the agent did not run it", failure)
        self.assertIn("> The C extension crashed on the Windows build, so I dropped it. We stay pure Python.", decision)
        failure_key, decision_key = report["ready"]
        with store.Store(REPO, config=self.pcfg) as db:
            self.assertEqual(["verified", "verified"], [db.event(key)["state"] for key in (failure_key, decision_key)])
            self.assertEqual(failure_key, db.event(decision_key)["candidate"]["because_with"])
            self.assertNotIn("because_with", db.event(failure_key)["candidate"])
            held = lambda key: sorted(row[0] for row in db.db.execute("SELECT source_id FROM event_sources WHERE eventkey=?", (key,)))
            ids = [s["id"] for s in self.data["sources"]]
            self.assertEqual(sorted([ids[2], ids[3]]), held(failure_key))   # the user's message stays the failure's row
            self.assertEqual([], held(decision_key))                        # the decision's only message is shared with it
            self.assertEqual({"verified"}, {db.observation(sid)["state"] for sid in ids[2:4]})
            self.assertEqual(ids[2], db.event(decision_key)["source_id"])

    def test_the_drafts_answer_is_valid_and_the_decision_cites_the_users_message(self):
        draft = self.data["draft"]
        self.assertTrue(runtime.validate({"candidates": draft}, worker.DRAFT))
        self.assertEqual(["failure", "decision"], [c["kind"] for c in draft])
        self.assertEqual(1, draft[1]["because"])
        for candidate in draft:
            self.assertNotIn("because=", record_format.first_lines(candidate["body"])[0])   # the model writes no because=

    def test_the_reviewer_is_told_the_failure_in_words_and_never_its_key(self):
        report, purposes, calls = self.run_pass()
        reviews = [payload for purpose, payload in calls if purpose == "review"]
        self.assertNotIn("because", reviews[0]["candidate"])
        shown = reviews[1]["candidate"]
        self.assertNotIn("because_with", shown)
        self.assertEqual({"kind": "failure", "plain": "the optional C extension crashed on the Windows build"}, shown["because"])
        self.assertNotIn(report["ready"][0], runtime.canonical(reviews[1]))

    def at_level(self, level):
        self.use(level=level)
        report, purposes, calls = self.run_pass()
        posted = self.comments()
        self.assertEqual(["failure", "decision"], [self.marker(body)["type"] for _, _, body in posted], level)
        self.assertEqual(str(posted[0][0]), self.marker(posted[1][2])["because"], level)

    def test_the_level_notes_only_posts_the_linked_pair_too(self):
        self.at_level("notes")

    def test_the_level_assistant_posts_the_linked_pair_too_and_asks_about_nothing(self):
        self.at_level("assistant")
        with store.Store(REPO, config=self.pcfg) as db:
            self.assertEqual([], db.waiting())

    def test_the_decision_waits_in_the_queue_until_its_failure_is_posted(self):
        failure, decision = self.queued()
        out = self.pub(decision)
        self.assertEqual("depends", out["waiting"], out)
        self.assertEqual(("ready", []), (self.event(decision)["state"], self.comments()))
        done = self.pub(failure)
        self.assertEqual("verified", done["state"])
        out = self.pub(decision)
        self.assertEqual("verified", out["state"], out)
        body = self.comments()[-1][2]
        self.assertEqual(str(done["receipt"]["comment_id"]), self.marker(body)["because"])

    def test_a_held_failure_holds_its_decision_off_the_list_and_its_post_lets_it_go(self):
        failure, decision = self.queued()
        with store.Store(REPO, config=self.pcfg) as db:
            db.transition(failure, "deferred", reason="status_change")   # held for the user's answer
        out = self.pub(decision)
        self.assertEqual("depends", out["waiting"], out)
        self.assertEqual(("deferred", "depends"), (self.event(decision)["state"], self.event(decision)["reason"]))
        with store.Store(REPO, config=self.pcfg) as db:
            db.transition(failure, "ready")   # the user's answer let it go
        done = self.pub(failure)
        self.assertEqual("verified", done["state"], done)
        self.assertEqual("ready", self.event(decision)["state"])   # released by the post of the failure
        self.assertEqual("verified", self.pub(decision)["state"])
        self.assertEqual(str(done["receipt"]["comment_id"]), self.marker(self.comments()[-1][2])["because"])

    def test_a_failure_the_user_drops_lets_its_decision_stand_alone(self):
        failure, decision = self.queued()
        with store.Store(REPO, config=self.pcfg) as db:
            db.transition(failure, "deferred", reason="status_change")
        self.assertEqual("depends", self.pub(decision)["waiting"])
        code, text = self.cli("discard", failure)
        self.assertEqual(0, code, text)
        self.assertEqual("rejected", self.event(failure)["state"])
        self.assertEqual(("ready", ""), (self.event(decision)["state"], self.event(decision)["reason"]))   # not rejected
        self.assertEqual("verified", self.pub(decision)["state"])
        posted = self.comments()
        self.assertEqual(["decision"], [self.marker(body)["type"] for _, _, body in posted])
        self.assertNotIn("because", self.marker(posted[0][2]))

    def test_a_failure_the_main_session_recorded_itself_leaves_the_decision_to_stand_alone(self):
        failure, decision = self.queued()
        with store.Store(REPO, config=self.pcfg) as db:
            db.transition(failure, "deferred", reason="main_session")
        self.assertEqual("verified", self.pub(decision)["state"])   # it does not wait for a post that will not come
        self.assertNotIn("because", self.marker(self.comments()[-1][2]))

    def test_a_task_that_is_dropped_does_not_reject_a_decision_that_waits_for_a_failure(self):
        # The decision has no `with`: a mark of task 0 without one follows any task, a decision never does.
        opener, _ = self.task_event()
        failure, decision = self.queued()
        with store.Store(REPO, config=self.pcfg) as db:
            db.transition(failure, "deferred", reason="status_change")
            self.assertEqual("depends", self.pub(decision)["waiting"])
            self.assertEqual([], publisher.release_dependents(db, db.event(opener), False))
            self.assertEqual([], publisher.release_dependents(db, db.event(opener), True))
        self.assertEqual(("deferred", "depends"), (self.event(decision)["state"], self.event(decision)["reason"]))

    def test_the_decision_may_stand_on_another_topic_than_its_failure(self):
        apart = copy.deepcopy(self.data["draft"])
        apart[1]["issue"] = 5
        report, purposes, calls = self.run_pass(draft=apart)
        posted = self.comments()
        self.assertEqual([(6, "failure"), (5, "decision")], [(issue, self.marker(body)["type"]) for _, issue, body in posted])
        self.assertEqual(str(posted[0][0]), self.marker(posted[1][2])["because"])

    def test_a_decision_the_drafter_wrote_first_still_follows_its_failure(self):
        swapped = [copy.deepcopy(self.data["draft"][1]), copy.deepcopy(self.data["draft"][0])]
        swapped[0]["because"] = 2   # the failure is the second of the draft: it is still queued, and reviewed, first
        report, purposes, calls = self.run_pass(draft=swapped)
        posted = self.comments()
        self.assertEqual(["failure", "decision"], [self.marker(body)["type"] for _, _, body in posted])
        self.assertEqual(str(posted[0][0]), self.marker(posted[1][2])["because"])
        self.assertEqual(["failure", "decision"], [payload["candidate"]["kind"] for purpose, payload in calls if purpose == "review"])

    def test_a_failure_the_reviewer_refuses_leaves_the_decision_posted_alone(self):
        report, purposes, calls = self.run_pass(review=[{"accepted": False, "reason": "unsupported"},
                                                        {"accepted": True, "reason": "supported"}])
        self.assertEqual((2, 1), (report["reviewed"], len(report["ready"])))
        posted = self.comments()
        self.assertEqual(["decision"], [self.marker(body)["type"] for _, _, body in posted])
        self.assertNotIn("because", self.marker(posted[0][2]))   # no failure was posted: nothing to link to
        ids = [s["id"] for s in self.data["sources"]]
        with store.Store(REPO, config=self.pcfg) as db:
            self.assertEqual("verified", db.observation(ids[2])["state"])

    def test_a_failure_that_rests_on_the_users_words_alone_is_not_posted_and_the_decision_is(self):
        # The failure's guard stands: a failure is what the agent told the user it found, or a finding the user typed that
        # the agent's message takes in; the user's message alone is neither.
        cut = copy.deepcopy(self.data["draft"])
        cut[0]["sources"], cut[0]["excerpts"] = [3], cut[0]["excerpts"][:1]
        report, purposes, calls = self.run_pass(draft=cut, review=[self.data["reviews"][1]])
        self.assertEqual(["sort", "draft", "review"], [p for p in purposes if p != "summary"][:3])
        self.assertEqual(["decision"], [self.marker(body)["type"] for _, _, body in self.comments()])

    def test_a_decision_that_rests_on_the_agents_message_alone_is_not_written(self):
        # The decision's authority is the user's own typed words, or a choice in a dialog: the agent's message is neither.
        cut = copy.deepcopy(self.data["draft"])
        cut[1]["sources"], cut[1]["excerpts"] = [4], []
        report, purposes, calls = self.run_pass(draft=cut, review=[self.data["reviews"][0]])
        self.assertEqual((["failure"], 1), ([self.marker(body)["type"] for _, _, body in self.comments()], report["deferred"]))
        by_n = {n: dict(s, subagent=False, sidechain=False) for n, s in enumerate(self.data["sources"], 1)}
        labels = [{"job": "memory", "kind": "decision", "sources": [4], "task": 0, "steps": [], "parts": []}]
        kept, dropped = worker.check_labels(labels, by_n, {"tasks": {}, "parts": {}}, "everything")
        self.assertEqual(([], {"decision": 1}), (kept, dropped))

    def test_a_decision_that_cites_both_of_its_failures_messages_keeps_to_the_users_typed_words(self):
        both = copy.deepcopy(self.data["draft"])
        both[1]["sources"] = [3, 4]   # the failure's own two messages: the same key
        report, purposes, calls = self.run_pass(draft=both)
        posted = self.comments()
        self.assertEqual(["failure", "decision"], [self.marker(body)["type"] for _, _, body in posted])
        with store.Store(REPO, config=self.pcfg) as db:
            ids = [s["id"] for s in self.data["sources"]]
            self.assertEqual([ids[2]], db.event(report["ready"][1])["candidate"]["sources"])

    def test_a_decision_the_drafter_does_not_tie_to_the_failure_is_not_linked_and_is_left_out_as_before(self):
        # A source backs one event: with no because the decision is not the failure's, and its message is the failure's.
        loose = copy.deepcopy(self.data["draft"])
        loose[1]["because"] = 0
        report, purposes, calls = self.run_pass(draft=loose, review=[self.data["reviews"][0]])
        self.assertEqual((["failure"], 1), ([self.marker(body)["type"] for _, _, body in self.comments()], report["deferred"]))

    def odd_link(self, place):
        odd = copy.deepcopy(self.data["draft"])
        odd[1]["because"] = place
        report, purposes, calls = self.run_pass(draft=odd, review=[self.data["reviews"][0]])
        self.assertEqual(["failure"], [self.marker(body)["type"] for _, _, body in self.comments()])
        self.assertEqual(1, len(report["ready"]))

    def test_a_link_to_the_decision_itself_ties_nothing(self):
        self.odd_link(2)

    def test_a_link_beyond_the_draft_ties_nothing(self):
        self.odd_link(5)

    def test_the_pair_is_not_posted_in_an_off_the_record_session(self):
        text = "For this session do not post anything to the record."
        first = self.data["sources"][0]
        self.by_id[first["id"]]["text"] = text
        self.ready()
        self.capture()
        hold = observer_holds.make_hold({"source_id": first["id"], "role": "user", "direct_user": True, "text": "do not post"},
                                        REPO, "publish", {"session_id": "sess-u02"}, None)
        with store.Store(REPO, config=self.pcfg) as db:
            db.save_holds([hold])
        calls = []
        with mock.patch.object(worker.sources, "resolve", side_effect=lambda meta, root: self.by_id[meta["source_id"]]):
            report = worker.run_once(REPO, self.pcfg, self.project, self.models(calls=calls))
        self.assertEqual(([], []), ([p for p, _ in calls if p in ("sort", "draft", "review")], report["ready"]))
        self.assertEqual([], self.comments())


class ReleasedTests(LiveBox):
    """A session the user kept off the record by a typed request, and then released in their own terminal
    (tests/fixtures/observer-stretches/u03.json: the first message says "Don't post this" about a result, a failure follows).
    Releasing a hold offers the sources it withheld to the sort again, each marked hold_released with the quoted words of
    that hold in what the sorter, the drafter and the reviewer are shown: that request no longer applies, and the moment is
    judged on its own; any other request not to post still keeps a moment off the record. Without the mark the sorter set
    the result aside and the reviewer rejected it as withheld; the recorded answers of the fixture were made with it. A hold
    still active, and a request typed after the release, keep the session off the record."""

    fixture, session = RELEASED, "sess-u03"
    QUOTE = "don't post"   # what the hook keeps of the first message: the phrase

    def setUp(self):
        super().setUp()
        self.ids = [s["id"] for s in self.data["sources"]]

    def hold(self, text=None, sid=None, session="own"):
        """The hold the hook makes of a message that says it: the phrase, as the hook passes it, of this session."""
        text = text or self.data["sources"][0]["text"]
        scope = {"session_id": self.session if session == "own" else session} if session else None
        return observer_holds.make_hold({"source_id": sid or self.ids[0], "role": "user", "direct_user": True,
                                         "text": observer_holds.withheld(text)}, REPO, "publish", scope, None)

    def keep(self, *holds):
        with store.Store(REPO, config=self.pcfg) as db:
            db.save_holds(list(holds))

    def release(self, hold):
        with store.Store(REPO, config=self.pcfg) as db:
            db.update_holds(lambda holds: observer_holds.release(holds, hold["id"]))
            return db.offered_again

    def marked(self):
        with store.Store(REPO, config=self.pcfg) as db:
            return db.released_sources()

    def states(self, ids=None):
        with store.Store(REPO, config=self.pcfg) as db:
            return [(db.observation(sid)["state"], db.observation(sid)["reason"]) for sid in ids or self.ids]

    def kinds(self):
        return [self.marker(body)["type"] for _, body in self.posted()]

    def marker(self, body):
        return record_format.parse_marker(record_format.first_lines(body)[0])

    def model_calls(self, calls):
        return [(purpose, payload) for purpose, payload in calls if purpose in ("sort", "draft", "review")]

    def typed_later(self, ident, text):
        """A message typed in this session after the others, as the hook leaves it."""
        extra = {"at": "2026-11-18T15:54:00.000Z", "dialog_user": False, "direct_user": True, "id": ident * 64, "role": "user",
                 "session_id": self.session, "text": text, "via": None}
        self.data["sources"].append(extra)
        self.by_id[extra["id"]] = dict(extra, subagent=False, sidechain=False, control_user=False, message_id=None)
        return extra["id"]

    def another_session(self, session="sess-other", ids=("other-1", "other-2")):
        """Sources of another session, which the pass never needs to read while a hold covers them."""
        rows = [{"source_id": sid, "session_id": session, "role": "user", "event_type": "Stop", "path": "/example/other.jsonl",
                 "offset": n * 1000, "end": n * 1000 + 900, "sha256": "0" * 64, "content_sha256": "%064x" % (n + 1),
                 "observed_at": time.time() - 90 + n} for n, sid in enumerate(ids)]
        with store.Store(REPO, config=self.pcfg) as db:
            db.capture(rows)
        return list(ids)

    def views(self, calls, purpose="sort"):
        return dict((view["n"], view) for view in next(p for name, p in calls if name == purpose)["sources"])

    def test_a_released_session_is_offered_again_marked_sorted_and_posted_once(self):
        hold = self.hold()
        self.keep(hold)
        report, purposes, calls = self.run_pass()
        self.assertEqual(([], []), (self.model_calls(calls), report["ready"]))   # the active hold is checked before any call
        self.assertEqual([("deferred", "withheld")] * 8, self.states())
        self.assertEqual({}, self.marked())                                      # nothing is marked while the hold stands
        self.assertEqual(8, self.release(hold))
        self.assertEqual([("observed", "")] * 8, self.states())
        self.assertEqual({sid: self.QUOTE for sid in self.ids}, self.marked())
        report, purposes, calls = self.run_pass()
        self.assertEqual(["sort", "draft", "review", "review"], [p for p in purposes if p != "summary"][:4])
        self.assertEqual(["result", "failure"], self.kinds())
        for purpose, payload in self.model_calls(calls):   # every source of every call carries the quoted request
            views = payload["sources"] + payload.get("context", [])
            self.assertTrue(views and all(view.get("hold_released") == self.QUOTE for view in views), purpose)
        self.assertEqual({}, self.marked())               # handled: the marks are gone
        again, purposes, calls = self.run_pass()
        self.assertEqual(([], ["result", "failure"]), ([p for p in purposes if p != "summary"], self.kinds()))

    def test_a_hold_that_is_still_active_keeps_the_session_off_the_record_and_marks_nothing(self):
        self.keep(self.hold())
        for _ in range(2):
            report, purposes, calls = self.run_pass()
            self.assertEqual(([], []), (self.model_calls(calls), self.posted()))
        self.assertEqual(([("deferred", "withheld")] * 8, {}), (self.states(), self.marked()))

    def test_a_request_typed_after_the_release_keeps_the_session_off_the_record_again(self):
        first = self.hold()
        self.keep(first)
        self.run_pass()
        self.assertEqual(8, self.release(first))
        text = "Keep this off the record from now on."
        extra = self.typed_later("f", text)
        second = self.hold(text, extra)   # the hook's hold for the new request, which is not the one that was lifted
        self.keep(second)
        report, purposes, calls = self.run_pass()
        self.assertEqual(([], [], []), (self.model_calls(calls), self.posted(), report["ready"]))
        self.assertEqual([("deferred", "withheld")] * 9, self.states(self.ids + [extra]))

    def test_the_mark_is_on_the_sources_a_release_offered_again_and_on_no_other(self):
        hold = self.hold()
        self.keep(hold)
        self.run_pass()
        self.release(hold)
        self.typed_later("e", "ok, thanks")   # typed after the release: no hold ever covered it
        report, purposes, calls = self.run_pass()
        views = self.views(calls)
        self.assertEqual([self.QUOTE] * 8, [views[n].get("hold_released") for n in range(1, 9)])
        self.assertNotIn("hold_released", views[9])   # the key is absent, not false or empty
        self.assertEqual("ok, thanks", views[9]["text"])

    def test_only_the_hold_that_was_released_offers_its_sources_and_lends_its_words(self):
        # Two sessions, two typed requests: lifting one lifts that one only, and the other session stays off the record.
        mine = self.hold()
        theirs = self.hold("Please do not record this one.", "z" * 64, session="sess-other")
        others = self.another_session()
        self.keep(mine, theirs)
        self.run_pass()
        self.assertEqual([("deferred", "withheld")] * 10, self.states(self.ids + others))
        legacy = self.another_session("sess-legacy", ("legacy-1",))   # withheld by a verdict of 1.5.0: no hold covers it
        with store.Store(REPO, config=self.pcfg) as db:
            db.defer(legacy[0], "withheld")
        self.assertEqual(8, self.release(mine))
        self.assertEqual({sid: self.QUOTE for sid in self.ids}, self.marked())   # the other sessions have no mark
        self.assertEqual([("deferred", "withheld")] * 3, self.states(others + legacy))
        report, purposes, calls = self.run_pass()
        self.assertEqual(8, len(next(p for name, p in calls if name == "sort")["sources"]))   # none of the other session's
        self.assertEqual(["result", "failure"], self.kinds())
        self.assertEqual([("deferred", "withheld")] * 2, self.states(others))
        self.assertEqual(2, self.release(theirs))
        self.assertEqual({sid: "do not record" for sid in others}, self.marked())   # its own words, not the first request's

    def test_two_requests_in_one_session_are_lifted_one_release_at_a_time_and_both_are_told(self):
        first = self.hold()
        text = "Keep this off the record: the 2 MB run took 0.1 s."
        later = self.typed_later("d", text)
        second = self.hold(text, later)
        self.keep(first, second)
        self.run_pass()
        self.assertEqual(0, self.release(first))   # the second request still covers the session: nothing is offered
        self.assertEqual([("deferred", "withheld")] * 9, self.states(self.ids + [later]))
        report, purposes, calls = self.run_pass()
        self.assertEqual(([], []), (self.model_calls(calls), self.posted()))
        self.assertEqual(9, self.release(second))
        self.assertEqual({sid: self.QUOTE + " | off the record" for sid in self.ids + [later]}, self.marked())
        report, purposes, calls = self.run_pass()
        self.assertEqual(["result", "failure"], self.kinds())
        self.assertEqual({self.QUOTE + " | off the record"}, {view["hold_released"] for view in self.views(calls).values()})

    def test_a_hold_of_the_whole_repository_frees_its_sources_and_marks_none(self):
        wide = self.hold(session=None)
        self.keep(wide)
        self.run_pass()
        self.assertEqual(8, self.release(wide))
        self.assertEqual({}, self.marked())   # its words say nothing about what this session typed
        report, purposes, calls = self.run_pass()
        self.assertEqual(["result", "failure"], self.kinds())   # (the models' answers are the recorded ones)
        sort = self.views(calls)
        self.assertEqual(8, len(sort))
        self.assertTrue(all("hold_released" not in view for view in sort.values()))

    def test_the_words_of_a_session_hold_survive_the_release_of_a_repository_hold_that_still_covered_it(self):
        mine, wide = self.hold(), self.hold("do not post", "y" * 64, session=None)
        self.keep(mine, wide)
        self.run_pass()
        self.assertEqual(0, self.release(mine))
        self.assertEqual(8, self.release(wide))
        self.assertEqual({sid: self.QUOTE for sid in self.ids}, self.marked())   # the first request was lifted, the second hold had none

    def test_a_release_that_offers_nothing_marks_nothing(self):
        other = observer_holds.make_hold({"source_id": self.ids[0], "role": "user", "direct_user": True, "text": "do not post"},
                                         REPO, "publish", {"session_id": "sess-else"}, None)
        self.keep(other)
        self.assertEqual((0, {}), (self.release(other), self.marked()))

    def test_a_marked_source_stays_marked_until_it_is_handled_and_a_hold_that_covers_it_again_keeps_it_withheld(self):
        hold = self.hold()
        self.keep(hold)
        self.run_pass()
        self.release(hold)
        with store.Store(REPO, config=self.pcfg) as db:
            db.defer(self.ids[0], "uncertain")        # tried again later: still waiting for its turn
            db.mark_handled(self.ids[1])              # set aside
            db.defer(self.ids[2], "withheld")         # covered by a hold again
        self.assertEqual(set(self.ids) - {self.ids[1]}, set(self.marked()))
        with store.Store(REPO, config=self.pcfg) as db:
            for sid in self.ids[3:]:
                db.mark_handled(sid)
        self.assertEqual({self.ids[0], self.ids[2]}, set(self.marked()))

    def test_the_quoted_words_are_one_clean_line_of_bounded_length(self):
        # A hold is valid only when its quote makes it again (one of the phrases), so the words are short already; what the
        # control holds is still read as data: a form it does not know is left out, the rest is cleaned and capped.
        self.capture()   # the sources are in the queue, observed
        wide = "don't\u202e post\n\x00 now " + "x" * 500
        with store.Store(REPO, config=self.pcfg) as db:
            db.set_control(store.RELEASED, {self.ids[0]: [wide, 5, None, "second"], self.ids[1]: "text", self.ids[2]: [],
                                            self.ids[3]: ["", " \n "], "no-such-source": ["x"]})
            found = db.released_sources()
        self.assertEqual([self.ids[0]], list(found))
        self.assertEqual("don't post now " + "x" * (store.QUOTE_MAX - len("don't post now ")) + " | second", found[self.ids[0]])
        self.assertLessEqual(len(found[self.ids[0]]), store.QUOTES_MAX)
        self.assertEqual("", store.clean_quote(None))
        self.assertEqual("", store.clean_quote(True))
        self.assertEqual("", store.clean_quote(" \n\t\x00 "))
        self.assertEqual("a b", store.clean_quote("a\x07  b"))

    def test_only_a_quoted_request_reaches_a_view(self):
        source = {"role": "user", "text": "t", "direct_user": True, "via": None, "at": "x"}
        self.assertNotIn("hold_released", worker._view(source, 1))
        for odd in (True, False, 1, None, "", "  ", "\x00", ["don't post"]):
            self.assertNotIn("hold_released", worker._view(dict(source, hold_released=odd), 1), repr(odd))
        self.assertEqual("don't post", worker._view(dict(source, hold_released="don't post"), 1)["hold_released"])
        self.assertEqual("a b", worker._view(dict(source, hold_released="a\n\u202e b"), 1)["hold_released"])


class BecauseSchemaTests(tp.PublisherBox):
    """The store takes `because_with` in its strict form only, and the marker is composed from it by code."""

    def setUp(self):
        super().setUp()
        with open(PAIR, encoding="utf-8") as handle:
            self.pair = json.load(handle)

    def failure(self):
        key, source = self.queue("failure", 6, self.pair["draft"][0]["body"])
        return key

    def refused(self, kind="decision", **extra):
        self.sources += 1
        source = "source%d" % self.sources
        body = self.pair["draft"][1]["body"]
        candidate = dict(tp.candidate_for(kind, 6, body, [source]), **extra)
        with store.Store(REPO, config=self.pcfg) as db:
            db.capture([self.metadata(source)])
            ident = db.reserve_invocation("review")
            db.complete_invocation(ident, True, output_hash=store.candidate_hash(candidate))
            with self.assertRaisesRegex(store.StoreError, "invalid_candidate"):
                db.review_complete(source, candidate, {"reservation_id": ident, "accepted": True,
                                                       "candidate_hash": store.candidate_hash(candidate)})

    def test_the_key_of_a_failure_of_the_queue_links_a_decision(self):
        key = self.failure()
        decision, _ = self.queue("decision", 6, self.pair["draft"][1]["body"], because_with=key)
        self.assertEqual(key, self.event(decision)["candidate"]["because_with"])

    def test_a_link_with_no_failure_behind_it_is_refused(self):
        key = self.failure()
        self.refused(because_with="0" * 32)                    # no such event
        self.refused(because_with="not-a-key")
        self.refused(because_with=key.upper())
        self.refused(because_with=7)
        self.refused(because_with=None)
        self.refused(kind="result", because_with=key)          # only a decision is made because of something
        self.refused(kind="failure", because_with=key)
        result, _ = self.queue("result", 6, tj.result_text())
        self.refused(because_with=result)                      # the event is not a failure

    def test_a_decision_may_rest_on_the_messages_of_its_failure_and_no_other_event_holds(self):
        with store.Store(REPO, config=self.pcfg) as db:
            for source in ("a", "b", "c"):
                db.capture([self.metadata(source)])
            candidate = tp.candidate_for("failure", 6, self.pair["draft"][0]["body"], ["a", "b"])
            ident = db.reserve_invocation("review")
            db.complete_invocation(ident, True, output_hash=store.candidate_hash(candidate))
            failure = db.review_complete("a", candidate, {"reservation_id": ident, "accepted": True,
                                                          "candidate_hash": store.candidate_hash(candidate)})
            other = tp.candidate_for("result", 6, tj.result_text(), ["c"])
            ident = db.reserve_invocation("review")
            db.complete_invocation(ident, True, output_hash=store.candidate_hash(other))
            db.review_complete("c", other, {"reservation_id": ident, "accepted": True, "candidate_hash": store.candidate_hash(other)})
            body = self.pair["draft"][1]["body"]
            for sources, taken in ((["a"], True), (["a", "c"], False)):
                made = tp.candidate_for("decision", 6, body, sources, because_with=failure)
                ident = db.reserve_invocation("review")
                db.complete_invocation(ident, True, output_hash=store.candidate_hash(made))
                key = db.review_complete(sources[0], made, {"reservation_id": ident, "accepted": True,
                                                            "candidate_hash": store.candidate_hash(made)})
                self.assertEqual(taken, key is not None, sources)   # "c" belongs to another event: the decision is refused it

    def event_of(self, **extra):
        key, _ = self.queue("decision", 6, self.pair["draft"][1]["body"], **extra)
        return self.event(key)

    def test_compose_writes_the_link_once_and_only_for_a_decision_drafted_with_a_failure(self):
        key = self.failure()
        event = self.event_of(because_with=key)
        failure = {"comment": 9104, "at": "2026-11-18T15:17+00:00"}
        marker = self.marker(publisher.compose(event, None, set(), None, failure))
        self.assertEqual(("9104", event["eventkey"]), (marker["because"], marker["eventkey"]))
        self.assertEqual(event["body"], publisher.compose(event, None, set(), None, None))   # no failure posted: no link
        plain = self.event_of()   # a decision with no failure behind it
        self.assertEqual(plain["body"], publisher.compose(plain, None, set(), None, failure))
        self.assertEqual(publisher.compose(event, None, set(), None, failure), publisher.compose(event, None, set(), None, failure))

    def test_compose_keeps_a_link_the_body_has_and_never_writes_a_fifth_or_a_link_into_the_past(self):
        key = self.failure()
        event = self.event_of(because_with=key)
        failure = {"comment": 9104, "at": "2026-11-18T15:17+00:00"}
        event["candidate"]["body"] = event["candidate"]["body"].replace(" -->", " because=7,8 -->", 1)
        self.assertEqual("7,8,9104", self.marker(publisher.compose(event, None, set(), None, failure))["because"])
        again = dict(failure, comment=8)
        self.assertEqual("7,8", self.marker(publisher.compose(event, None, set(), None, again))["because"])   # already there
        event["candidate"]["body"] = event["candidate"]["body"].replace("because=7,8", "because=1,2,3,4", 1)
        self.assertEqual("1,2,3,4", self.marker(publisher.compose(event, None, set(), None, failure))["because"])
        later = dict(failure, at="2026-11-18T15:18+00:00")   # stamped after the decision: the linter's rule is back in time only
        fresh = self.event_of(because_with=key)
        self.assertNotIn("because", self.marker(publisher.compose(fresh, None, set(), None, later)))
        self.assertIn("because", self.marker(publisher.compose(fresh, None, set(), None, failure)))

    def test_a_link_in_a_failure_body_is_never_written_for_it(self):
        key = self.failure()
        failure_event = self.event(key)
        self.assertEqual(failure_event["body"], publisher.compose(failure_event, None, set(), None,
                                                                  {"comment": 9104, "at": "2026-11-18T15:17+00:00"}))

    def marker(self, body):
        return record_format.parse_marker(record_format.first_lines(body)[0])


REAL_COMMENT = 6972770100   # where GitHub's comment numbers stood in 2026: the fake's own start at 9104 hid a bound of 10 ** 9


class ActsTests(tjj.JobsBox):
    """The acts log of a record whose comments are numbered as GitHub numbers them: a memory, a task opened and a step
    checked were left out of it when the number of the comment was above the store's bound for counts."""

    def setUp(self):
        super().setUp()
        data = self.repo()
        data["next_comment"] = REAL_COMMENT
        self.save(data)

    def test_an_act_keeps_a_comment_number_above_a_billion_and_refuses_what_is_no_number(self):
        with store.Store(REPO, config=self.pcfg) as db:
            ident = db.add_act("memory", issue=6, comment=REAL_COMMENT)
            self.assertEqual(REAL_COMMENT, db.act(ident)["comment"])
            for comment in (-1, 2 ** 53 + 1, True, "6972770100", 1.5):
                with self.assertRaises(store.StoreError, msg=repr(comment)):
                    db.add_act("memory", issue=6, comment=comment)
            for issue in (-1, 10 ** 9 + 1, False):
                with self.assertRaises(store.StoreError, msg=repr(issue)):
                    db.add_act("memory", issue=issue, comment=REAL_COMMENT)
            with self.assertRaises(store.StoreError):
                db.add_act("memory", issue=6, comment=REAL_COMMENT, numbers=[10 ** 9 + 1])

    def test_a_task_opened_its_mark_and_a_memory_are_in_the_log_with_their_addresses(self):
        opener, source = self.task_event()
        marked, _ = self.result(marks=[{"task": 0, "steps": [1]}], **{"with": source})
        memory, _ = self.result()
        opened = self.pub(opener)
        self.assertEqual("verified", opened["state"], opened)
        self.assertGreater(opened["receipt"]["comment_id"], 10 ** 9)
        self.assertEqual("verified", self.pub(marked)["state"])
        self.assertEqual("verified", self.pub(memory)["state"])
        kinds = {act["kind"]: act for act in self.acts()}
        self.assertEqual({"task_opened", "step_marked", "memory"}, set(kinds))
        for act in kinds.values():
            self.assertGreater(act["comment"], 10 ** 9)
        self.use()
        code, text = self.cli("log")
        self.assertEqual(0, code)
        for word in ("opened a task", "checked steps off", "posted a memory"):
            self.assertIn(word, text)
        self.assertIn("#issuecomment-%d" % opened["receipt"]["comment_id"], text)
        self.assertEqual(3, text.count("#issuecomment-"))


class UndoNoteTests(tjj.JobsBox):
    """An undo that writes nothing says why, in fixed words, and a body GitHub still shows as it was before a newer act
    is told as changed since, not as a bare "failed"."""

    def two_hub_renders(self):
        self.jobs()
        first = self.acts(["hub_rendered"])[0]
        self.ready()
        self.finish_task()
        self.jobs()   # closes the task; the hub shows it gone once the record is read again
        self.ready()
        self.jobs()
        second = self.acts(["hub_rendered"])[0]
        self.assertNotEqual(first["id"], second["id"])
        return first, second

    def undo_cli(self, act):
        with mock.patch.object(observer, "confirmed", return_value=None):
            return self.cli("undo", str(act))

    def test_a_render_a_later_act_replaced_is_told_as_changed_since_with_a_reason(self):
        first, second = self.two_hub_renders()
        self.use()
        before = self.body(4)
        code, text = self.undo_cli(first["id"])
        self.assertEqual(1, code)
        answer = json.loads(text)
        self.assertEqual("changed_since", answer["code"])
        self.assertEqual(jobs.UNDO_NOTES["changed_since"], answer["note"])
        self.assertEqual(before, self.body(4))   # nothing was written
        with store.Store(REPO, config=self.pcfg) as db:
            self.assertFalse(db.act(first["id"])["undone"])

    def test_a_read_that_still_shows_the_body_from_before_the_newer_act_is_told_as_changed_since(self):
        # GitHub's API may answer a moment after a write with the body from before it. The check of the write then finds the
        # body changed (its own read is the new one): the user is told that, not "failed".
        first, second = self.two_hub_renders()
        self.use()
        stale = jobs.recortex.read_text(os.path.join(jobs.tasks.scratch(REPO), jobs.after_name(first["backup"])))
        real, reads = jobs.recortex.read_issue_body, []

        def read(repo, number, *args, **kwargs):
            reads.append(number)
            return stale if len(reads) == 1 else real(repo, number, *args, **kwargs)
        before = self.body(4)
        with mock.patch.object(jobs.recortex, "read_issue_body", side_effect=read):
            code, text = self.undo_cli(first["id"])
        self.assertEqual((1, "changed_since"), (code, json.loads(text)["code"]), text)
        self.assertIn("no longer the one this act wrote", json.loads(text)["note"])
        self.assertEqual(before, self.body(4))

    def test_every_refusal_has_a_line_and_a_success_has_none(self):
        self.assertEqual({"changed_since", "no_backup", "failed", "not_closed", "not_open", "already_undone", "no_such_act"},
                         set(jobs.UNDO_NOTES))
        for code in ("restored", "reopened", "closed_not_planned", "not_reversible", "terminal-required"):
            self.assertEqual("", jobs.undo_note(code), code)
        for code, note in jobs.UNDO_NOTES.items():
            self.assertTrue(note.endswith("."), code)
        self.use()
        code, text = self.undo_cli(999999)
        self.assertEqual("no_such_act", json.loads(text)["code"])
        self.assertEqual(jobs.UNDO_NOTES["no_such_act"], json.loads(text)["note"])

    def test_a_kind_that_is_corrected_another_way_keeps_its_own_note(self):
        self.use()
        with store.Store(REPO, config=self.pcfg) as db:
            ident = db.add_act("memory", issue=5, comment=9109)
        code, text = self.undo_cli(ident)
        answer = json.loads(text)
        self.assertEqual(("not_reversible", jobs.CORRECTED["memory"]), (answer["code"], answer["note"]))


class CommentNumberTests(unittest.TestCase):
    def test_the_keepers_draft_names_the_comments_it_used_by_the_numbers_github_gives(self):
        # The id lists were bounded at 10 ** 9, below any comment number a real record holds, so a real draft was invalid.
        draft = {"draft": "# Title\n\nText.\n", "used": {"parts": [REAL_COMMENT], "results": [REAL_COMMENT + 1], "decisions": []},
                 "memory": {"plain": "the text now says it", "changed": "A sentence.", "why": "A result."}}
        self.assertTrue(runtime.validate(draft, observer_keeper.KEEPER_DRAFT))
        for bad in (0, -1, 2 ** 53 + 1, True, "6972770100", 1.5):
            self.assertFalse(runtime.validate(dict(draft, used=dict(draft["used"], parts=[bad])), observer_keeper.KEEPER_DRAFT), bad)


class PromptTests(unittest.TestCase):
    """The lines the recorded answers rest on, in each of the three wordings: without one of them the models went back to
    ignoring, drafting nothing or withholding the stretch."""

    def test_the_sorter_counts_the_users_own_finding_and_is_not_put_off_by_what_the_user_asks_of_the_agent(self):
        text = runtime.PROMPTS["sort"]
        self.assertIn("A finding the user typed about their own work", text)
        self.assertIn("\"just acknowledge it\"", text)
        self.assertIn("does not make it routine", text)

    def test_the_drafter_writes_the_users_finding_as_the_users_report(self):
        text = runtime.PROMPTS["draft"]
        self.assertIn("a finding the user typed about their own work", text)
        self.assertIn("say in What ran that the user reported the run", text)

    def test_the_reviewer_accepts_the_users_finding_told_as_the_users_report_and_rejects_it_as_the_agents_own(self):
        text = runtime.PROMPTS["review"]
        self.assertIn("a finding the user typed about their own work", text)
        self.assertIn("reject it with attribution when it presents the run as the agent's own", text)

    def test_only_the_users_own_typed_words_keep_a_moment_off_the_record_in_all_three_wordings(self):
        for purpose in ("sort", "draft", "review"):
            text = runtime.PROMPTS[purpose]
            self.assertIn("not to post or record" if purpose != "review" else "not to post or record", text, purpose)
            self.assertIn("leave the recording to recortex or the observer", text, purpose)
        for purpose in ("sort", "draft"):
            self.assertIn("only the user's own typed words can say so", runtime.PROMPTS[purpose], purpose)
            self.assertIn("the agent's account of one", runtime.PROMPTS[purpose], purpose)
        self.assertIn("the agent's account of such a request withholds nothing", runtime.PROMPTS["review"])
        self.assertIn("the agent saying that it posted nothing", runtime.PROMPTS["review"])

    def test_the_three_wordings_say_that_a_message_can_hold_a_failure_and_the_decision_it_led_to(self):
        sort = runtime.PROMPTS["sort"]
        self.assertIn("One message can hold both a failure and the decision it led to", sort)
        self.assertIn("give it both labels", sort)
        self.assertIn("a decision rests on the user's typed words or a dialog choice, never on the agent's message alone", sort)
        draft = runtime.PROMPTS["draft"]
        self.assertIn("When one message holds a failure and the decision it led to", draft)
        self.assertIn("write both, the failure first, and set the decision's because to the failure's place in your list", draft)
        self.assertIn("and its other fields say only what those words say", draft)
        self.assertIn("Write no because= in the marker: code writes the link once the failure is posted", draft)
        review = runtime.PROMPTS["review"]
        self.assertIn("A decision may carry because, the plain line of a failure of the same stretch that it is linked to", review)
        self.assertIn("reject with unsupported when they do not", review)

    def test_the_three_wordings_say_what_a_source_marked_hold_released_is(self):
        # Without it the sorter set the result aside after the user lifted the hold, and the reviewer rejected it as withheld;
        # and only the request the field quotes is lifted: any other request not to post still keeps a moment off the record.
        for purpose in ("sort", "draft", "review"):
            flat = " ".join(runtime.PROMPTS[purpose].split())
            self.assertIn(("a source" if purpose == "sort" else "A source")
                          + " with a hold_released field was kept off the record by a hold that the user lifted themselves, "
                          "and the field quotes that request: ", flat, purpose)
            self.assertIn("that request no longer applies, so write the moments it covered like any other" if purpose == "draft"
                          else "only the quoted request no longer applies", flat, purpose)
            for words in ("any other request not to post", "worded differently from the quote", "in that source or another",
                          "even when it is typed in a source that carries the quote"):
                self.assertIn(words, flat, purpose)

    def test_the_sorter_leaves_a_moment_the_agent_says_it_just_recorded(self):
        self.assertIn("a moment that an agent message in the stretch says it has just recorded, with the address of its comment",
                      runtime.PROMPTS["sort"])


if __name__ == "__main__":
    unittest.main()

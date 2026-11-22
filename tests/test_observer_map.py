"""Map learning, independent review, recovery and task context. All projects and model outputs are invented."""
import json
import unittest
from unittest import mock

from tests import test_observer_advisory as advice_fixture
from tests import test_observer_context as recall
from tests import test_observer_worker as fixture
import index
import observer
import observer_advisory
import observer_context
import observer_map as mapper
import observer_runtime as runtime
import observer_store
import observer_waiting
import project_map as model

REPO = fixture.REPO


def candidate(action="add", **kwargs):
    values = dict(action=action, node="", target="", title="", branches=[], sources=[1], memories=[])
    values.update(kwargs)
    return values


def item(title="Thermal calibration"):
    return {"comment": 10, "issue": 2, "kind": "decision", "text": title + " needs another sample series."}


def source(text, at="2026-10-29T13:00:00Z"):
    return {"text": text, "source_id": "a" * 64, "role": "user", "direct_user": True, "at": at}


def add(state, key, title, **kwargs):
    return model.apply(state, model.prepare(state, dict(action="add", id=key, title=title, **kwargs)))


class MapJudgmentTests(unittest.TestCase):
    def setUp(self):
        self.state = model.empty()
        self.people = [source("Thermal calibration is now its own research question for the next cycle.")]
        self.memories = {10: item()}

    def assess(self, raw, judgment="supported", shadow=False, bad_hash=False):
        self.payloads = []
        def call(purpose, payload, schema):
            self.payloads.append(payload)
            if purpose == "map_draft":
                return raw
            return {"judgment": judgment, "candidate_hash": "0" * 64 if bad_hash else payload["candidate_hash"]}
        return mapper.assess(REPO, 1, self.state, self.memories, self.people, [], {}, shadow, call)

    def test_review_binds_sources_and_automatic_changes_to_current_conversation_and_memory(self):
        raw = candidate(title="Thermal calibration", memories=[10])
        status, row = self.assess(raw)
        self.assertEqual(("ready", "observer"), (status, row["operation"]["source"]))
        self.assertEqual(["comment:10"], row["change"]["evidence"])
        self.assertEqual(row["operation"], self.assess(raw)[1]["operation"])
        self.assertEqual("shadow", self.assess(raw, shadow=True)[0])
        self.assertEqual("review", self.assess(raw, judgment="uncertain")[0])
        self.assertEqual("quiet", self.assess(raw, judgment="reject")[0])
        self.assertEqual("review_unavailable", self.assess(raw, bad_hash=True)[0])
        self.assertEqual("quiet", self.assess(dict(raw, sources=[8]))[0])
        self.assertEqual("quiet", self.assess(dict(raw, memories=[5555]))[0])
        self.people[0].update(direct_user=False, role="assistant")
        self.assertEqual("review", self.assess(raw)[0])

    def test_scoped_add_requires_parent_evidence_to_qualify_for_unattended_placement(self):
        self.state = add(self.state, "methods", "Instrument methods")
        raw = candidate(title="Thermal calibration", memories=[10], target="methods")
        self.assertEqual("review", self.assess(raw)[0])
        self.people[0]["text"] += " Put Thermal calibration under Instrument methods."
        self.assertEqual("ready", self.assess(raw)[0])

    def test_manual_rename_can_never_be_undone_automatically(self):
        self.state = add(self.state, "cal", "Probe calibration")
        self.people = [source("Rename Probe calibration to Thermal calibration for the new experimental series.")]
        raw = candidate("rename", node="cal", title="Thermal calibration", memories=[10])
        self.assertEqual("ready", self.assess(raw)[0])
        self.state = model.apply(self.state, model.prepare(self.state, dict(action="rename", id="cal", title="Probe tests")))
        self.people = [source("Rename Probe tests to Thermal calibration.")]
        self.assertEqual("review", self.assess(raw)[0])
        self.assertEqual("quiet", self.assess(candidate(title="Probe calibration"))[0])

    def test_review_dialog_names_stable_ids_and_preview_lists_affected_descendants(self):
        self.state = add(add(model.empty(), "a", "Devices"), "b", "Standards")
        self.state = add(add(self.state, "left", "Calibration", parent="a"), "right", "Calibration", parent="b")
        self.state = add(self.state, "child", "Dates", parent="left")
        self.people = [source("Delete the Calibration branch under Devices and move its children up.")]
        _, row = self.assess(candidate("delete", node="left"))
        entry = observer_waiting.entry(dict(kind="map", key=row["key"], short=row["key"][:8], line=row["line"]))
        self.assertIn("Calibration [left]", entry["question"])
        self.assertNotIn("[right]", entry["question"])
        impacts = {change["id"]: change for change in row["affected"]}
        self.assertEqual({"left", "child"}, set(impacts))
        self.assertEqual("a", impacts["child"]["after"]["parent"])
        self.assertTrue(impacts["left"]["after"]["deleted"])

    def test_oversized_message_cannot_rerun_a_draft_that_will_never_fit(self):
        self.people = [source("An elaborated observation about a rarely used component. " * 1500)]
        self.assertEqual(("input_limit", None), self.assess(candidate(title="unused")))
        self.assertFalse(self.payloads)

    def test_old_conversation_cannot_recreate_or_suggest_restoring_a_removed_branch(self):
        self.state = add(self.state, "cal", "Thermal calibration")
        self.state = model.apply(self.state, model.prepare(self.state, dict(action="delete", id="cal", children="keep")),
                                 at="2026-11-01T08:00:00Z")
        self.assertEqual("quiet", self.assess(candidate(title="Thermal calibration"))[0])
        self.assertEqual("quiet", self.assess(candidate("restore", node="cal"))[0])
        self.people = [source("We are back to Thermal calibration and need to repeat the measurements.", "2026-11-18T08:00:00Z")]
        self.assertEqual("review", self.assess(candidate("restore", node="cal"))[0])

    def test_destructive_changes_require_review_and_quiet_branches_stay(self):
        self.state = add(add(self.state, "a", "Instruments"), "b", "Samples")
        for raw in (candidate("move", node="a", target="b"), candidate("merge", node="a", target="b"),
                    candidate("split", node="a", branches=[dict(title="Probes", children=[]),
                                                          dict(title="Thermometers", children=[])])):
            with self.subTest(action=raw["action"]):
                self.assertEqual("review", self.assess(raw)[0])
        self.assertEqual("quiet", self.assess(candidate("delete", node="a"))[0])
        self.people = [source("Please delete the Instruments branch from the hierarchy.")]
        self.assertEqual("review", self.assess(candidate("delete", node="a"))[0])


class MapReturnTests(unittest.TestCase):
    def setUp(self):
        self.f = recall.ObserverContextTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.memory(40, kind="question", plain="Is the amber sample series complete?")
        self.state = add(model.empty(), "cal", "Thermal calibration", evidence=["comment:40"])

    def test_distinct_branch_recovers_an_open_question_with_evidence_and_advice_is_optional(self):
        with mock.patch.object(model, "read_map", return_value=self.state):
            packet = self.f.packet(query="Resume Thermal calibration. Which check did we agree to perform next?")
        self.assertEqual("Thermal calibration", packet["map_branch"])
        self.assertIn(40, packet["selected"])
        self.assertIn("Project Map branch", packet["text"])
        self.assertEqual(40, packet["advisories"][0]["question"])
        controls = advice_fixture.Controls()
        query = "Resume Thermal calibration"
        self.assertEqual("", observer_advisory.deliver(controls, packet, query, "session", mode="off"))
        self.assertIn("#issuecomment-40", observer_advisory.deliver(controls, packet, query, "session", mode="deliver"))
        self.assertFalse(observer_advisory.deliver(controls, packet, query, "session", mode="deliver"))
        self.f.memory(50, kind="result", plain="The amber sample series is complete.")
        self.f.link(50, "checks", 40)
        with mock.patch.object(model, "read_map", return_value=self.state):
            answered = self.f.packet(query=query)
        self.assertFalse(answered["map_branch"])
        self.assertFalse(answered["advisories"])

    def test_a_single_named_branch_matches_terminal_punctuation_and_command_dots_survive(self):
        state = add(model.empty(), "cal", "Calibration", evidence=["comment:40"])
        with mock.patch.object(model, "read_map", return_value=state):
            self.assertEqual("Calibration", self.f.packet(query="Resume Calibration.")["map_branch"])
        self.assertIn("relay.probe", observer_context.tokens("Check relay.probe."))

    def test_a_whole_issue_reference_can_supply_context_but_cannot_prompt_an_arbitrary_question(self):
        state = add(model.empty(), "cal", "Calibration", evidence=["issue:2"])
        with mock.patch.object(model, "read_map", return_value=state):
            packet = self.f.packet(query="Return to Calibration.")
        self.assertIn(40, packet["selected"])
        self.assertFalse(packet["advisories"])

    def test_ambiguous_stale_or_incomplete_map_falls_back_to_normal_retrieval(self):
        second = add(self.state, "other", "Documentation")
        second = add(second, "homonym", "Thermal calibration", parent="other")
        with mock.patch.object(model, "read_map", return_value=second):
            self.assertFalse(self.f.packet(query="Resume Thermal calibration. Which check did we agree to perform next?")["map_branch"])
        for key, stale in (("synced_at", "2020-01-01T00:00:00Z"), ("sync_complete", "0")):
            self.f.db.execute("UPDATE meta SET value=? WHERE key=?", (stale, key))
            with mock.patch.object(model, "read_map", return_value=self.state) as read:
                self.assertFalse(self.f.packet(query="Resume Thermal calibration. Which check did we agree to perform next?")["map_branch"])
                read.assert_not_called()


class MapWorkerTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.WorkerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config["hub"] = 1
        config_read = mock.patch.object(mapper.recortex, "load_config", return_value=self.f.config)
        config_read.start()
        self.addCleanup(config_read.stop)
        self.f.config["observer"]["map_mode"] = "shadow"
        runtime.approve(REPO, self.f.config)
        self.raw = candidate(title="Thermal calibration", memories=[10])
        self.sid = self.f.capture("Thermal calibration is a distinct project branch for our measured samples.")
        self.calls = []
        pair = mock.patch.object(mapper, "record", return_value=(model.empty(), {10: item()}))
        self.record = pair.start()
        self.addCleanup(pair.stop)

    def model(self, payload, schema, purpose, options):
        self.calls.append(purpose)
        if purpose == "sort":
            result = {"labels": [], "ignored": [1]}
        elif purpose == "map_draft":
            result = self.raw
        else:
            self.assertEqual("map_review", purpose)
            result = {"judgment": "supported", "candidate_hash": payload["candidate_hash"]}
        return {"ok": True, "structured": result}

    def test_worker_reaches_two_metered_calls_and_private_shadow_queue_survives_reopen(self):
        self.assertEqual("shadow", self.f.run_worker(self.model)["map"])
        with observer_store.Store(REPO, config=self.f.config) as store:
            before = mapper.rows(store)
            self.assertEqual(["shadow"], [r["state"] for r in before])
            self.assertEqual([], mapper.waiting(store))
            self.assertEqual(["map_draft", "map_review"], [r[0] for r in store.db.execute(
                "SELECT purpose FROM invocations WHERE job='map' AND success=1 ORDER BY id")])
            self.assertNotIn("distinct project branch", json.dumps(before))
        self.assertEqual("quiet", self.f.run_worker(self.model)["map"])
        self.assertEqual(1, self.calls.count("map_draft"))

    def test_large_stretch_is_processed_as_complete_messages_across_bounded_batches(self):
        self.f.config["observer"]["max_input_chars"] = 7000
        for i in range(6):
            self.f.capture(("Measure the current probe against the reference sample. " * 17) + "Series %d." % i)
        seen = []
        with observer_store.Store(REPO, config=self.f.config) as store:
            for _ in range(8):
                sources, cursor = mapper.material(store, REPO, self.f.config, self.f.project)
                if not cursor:
                    break
                for s in sources:
                    self.assertEqual(self.f.texts[s["source_id"]], s["text"])
                    seen.append(s["source_id"])
                store.set_control(mapper.control("cursor"), cursor)
        self.assertEqual(len(self.f.texts), len(set(seen)))
        self.assertEqual(len(seen), len(set(seen)))

    def test_publication_checks_hold_and_pause_again_at_write_boundary(self):
        self.assertEqual("shadow", self.f.run_worker(self.model)["map"])
        self.f.config["observer"].update(mode="automatic", map_mode="automatic")
        options = runtime.settings(self.f.config)
        with observer_store.Store(REPO, config=self.f.config) as store:
            candidate = dict(mapper.rows(store)[0], state="ready", generation="automatic")
            candidate["key"] = mapper.review_key(REPO, candidate)
            def sync_then_pause(repo, hub, op, guard):
                store.set_control("publication_paused", True)
                guard()
                raise AssertionError("must stop before an actual write")
            with mock.patch.object(mapper.recortex, "load_config", return_value=self.f.config), \
                    mock.patch.object(mapper.observer_publisher, "_gate", side_effect=lambda db, config: (
                    options if not db.get_control("publication_paused") else self.deny())), \
                    mock.patch.object(mapper.project_map_io, "remember"), \
                    mock.patch.object(mapper.project_map_io, "commit", side_effect=sync_then_pause):
                self.assertEqual("publication_paused", mapper.publish(store, REPO, self.f.config, candidate)["code"])
            self.assertEqual("shadow", mapper.rows(store)[0]["state"])

    def deny(self):
        raise mapper.observer_publisher.PublishError("publication_paused")

    def test_ordinary_level_cannot_enable_map_from_project_file_or_unattended_cli(self):
        with mock.patch.object(runtime, "stored_approval", return_value=None):
            self.assertEqual("off", runtime.settings(runtime.local_config(self.f.config))["map_mode"])
        with mock.patch.object(observer.recortex, "load_config", return_value=self.f.config), \
                mock.patch.object(observer, "on_terminal", return_value=False), mock.patch.object(observer, "emit") as out:
            self.assertEqual(1, observer.main(["map-mode", "automatic", "--yes"]))
            self.assertEqual("map_unanswered", out.call_args[0][0]["code"])   # a session needs the user's answer to map-upkeep

    def test_revoking_an_approved_file_after_drafting_refuses_the_reviewer_before_spending(self):
        import project_map_sources
        hint = dict(path="README.md", headings=[{"level": 1, "title": "Probes"}], digest="a" * 64)
        with mock.patch.object(project_map_sources, "snapshot", side_effect=[[hint], [hint], []]):
            self.assertEqual("review_unavailable", self.f.run_worker(self.model)["map"])
        with observer_store.Store(REPO, config=self.f.config) as store:
            self.assertEqual([], mapper.rows(store))
            self.assertFalse(store.get_control(mapper.control("cursor")))
            self.assertEqual(0, store.db.execute("SELECT COUNT(*) FROM invocations WHERE purpose='map_review'").fetchone()[0])

    def test_new_restricted_pattern_between_model_calls_keeps_the_reviewer_from_receiving_the_packet(self):
        disk_config = dict(self.f.config)
        def change_privacy(payload, schema, purpose, options):
            result = self.model(payload, schema, purpose, options)
            if purpose == "map_draft":
                disk_config["restricted_patterns"] = [{"name": "instrument", "pattern": "Thermal calibration"}]
            return result
        with mock.patch.object(mapper.recortex, "load_config", side_effect=lambda: dict(disk_config)):
            self.assertEqual("review_unavailable", self.f.run_worker(change_privacy)["map"])
        self.assertNotIn("map_review", self.calls)
        with observer_store.Store(REPO, config=self.f.config) as store:
            self.assertFalse(mapper.rows(store))
            self.assertFalse(store.get_control(mapper.control("cursor")))

    def test_unrelated_explicit_command_hold_does_not_consume_map_opportunities(self):
        with observer_store.Store(REPO, config=self.f.config) as store:
            original = {"source_id": self.sid, "session_id": "session-1", "role": "user", "direct_user": True,
                        "text": "/recortex hold command git push origin main"}
            hold = mapper.observer_holds.make_hold(original, REPO, "command", command="git push origin main")
            store.save_holds([hold])
        self.assertEqual("shadow", self.f.run_worker(self.model)["map"])

    def test_off_record_input_is_never_sent_to_map_models(self):
        with observer_store.Store(REPO, config=self.f.config) as store:
            original = {"source_id": self.sid, "session_id": "session-1", "role": "user", "direct_user": True, "text": "keep this off the record"}
            hold = mapper.observer_holds.compile_prompt_control(
                original["text"], original, REPO, session_id="session-1")["hold"]
            store.save_holds([hold])
        self.assertEqual("quiet", self.f.run_worker(self.model)["map"])
        self.assertNotIn("map_draft", self.calls)


class MapAuthoritativePublishTests(unittest.TestCase):
    def setUp(self):
        from tests import test_scripts as ts
        self.f = ts.RecordSandbox()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.in_process(extra={"CLAUDE_PROJECT_DIR": self.f.project})
        self.config = dict(self.f.config, observer={"enabled": True, "mode": "automatic", "level": "assistant",
                                                   "map_mode": "automatic", "publisher_id": runtime.publisher_id(create=True)})
        self.hub = self.config["hub"]
        self.comment = 987
        self.sid = "a" * 64
        self.f.add_comment(self.comment, self.hub, fixture.BODY, "2026-03-01T12:00:00Z")
        self.sync()
        state, memories = mapper.record(REPO, self.hub, "strict quoting", requested={self.comment})
        self.assertIn(self.comment, memories)
        src = source("Strict quoting will be a distinct branch for the parser project.")
        raw = candidate(title="Strict quoting", memories=[self.comment])
        self.item = mapper.proposal(REPO, self.hub, state, memories, [src], raw, "supported", False)
        self.assertEqual("ready", self.item["state"])
        self.db = observer_store.Store(REPO, config=self.config)
        self.addCleanup(self.db.close)
        self.db.capture([dict(source_id=self.sid, session_id="session-1", role="user", event_type="prompt",
                              path="/fictional/s", offset=0, end=2, sha256="9" * 64, observed_at=1234)])
        mapper.save(self.db, self.item)
        # Model isolation proofs are a separate suite. These tests use a real configured folder and the fake GitHub CLI.
        runtime.approve(REPO, self.config)
        gate = mock.patch.object(runtime, "gate_valid", return_value=True)
        gate.start()
        self.addCleanup(gate.stop)

    def sync(self):
        result = index.sync(REPO, self.hub, rebuild=True, log=lambda *args: None)
        self.assertTrue(result["complete"])

    def test_normal_verified_publish_can_retry_without_a_second_post(self):
        self.assertEqual("map_applied", mapper.publish(self.db, REPO, self.config, self.item)["code"])
        count = len(self.f.repo()["comments"])
        self.assertEqual("map_applied", mapper.publish(self.db, REPO, self.config, self.item)["code"])
        self.assertEqual(count, len(self.f.repo()["comments"]))
        self.assertEqual(1, len(self.db.get_control("automatic_posts")))

    def test_corroborating_comment_removed_remotely_is_refused_even_if_the_local_snapshot_still_includes_it(self):
        state = self.f.repo()
        state["comments"].pop(str(self.comment))
        self.f.save(state)
        result = mapper.publish(self.db, REPO, self.config, self.item)
        self.assertEqual("stale_map_evidence", result["code"])
        self.assertFalse(mapper.project_map_io.current(REPO, self.hub)["nodes"])
        self.assertFalse(self.db.get_control("automatic_posts"))

    def test_project_off_during_remote_revalidation_prevents_the_append(self):
        real = index.sync
        def disable(*args, **kwargs):
            result = real(*args, **kwargs)
            self.f.write_config(dict(self.f.config, observer=False))
            return result
        with mock.patch.object(index, "sync", side_effect=disable):
            self.assertEqual("not_approved", mapper.publish(self.db, REPO, self.config, self.item)["code"])
        self.assertFalse(mapper.project_map_io.current(REPO, self.hub)["nodes"])

    def test_stale_user_reviewed_proposal_is_closed_without_consuming_new_grants(self):
        manual = mapper.proposal(REPO, self.hub, model.empty(), {self.comment: item("Strict quoting")},
                                 [source("Strict quoting is worth a separate branch.")],
                                 candidate(title="Strict quoting", memories=[self.comment]), "uncertain", False)
        mapper.save(self.db, manual)
        state = self.f.repo()
        state["comments"].pop(str(self.comment))
        self.f.save(state)
        with mock.patch.object(self.db, "has_grant", return_value=True):
            response = mapper.answer(self.db, REPO, self.config, manual["key"])
        self.assertEqual("stale_map_evidence", response["code"])
        self.assertEqual("stale", next(row["state"] for row in mapper.rows(self.db) if row["key"] == manual["key"]))
        self.assertFalse(mapper.waiting(self.db))

    def test_an_existing_grant_cannot_apply_other_bytes_or_silently_promote_a_shadow_candidate(self):
        with mock.patch.object(self.db, "has_grant", return_value=True), mock.patch.object(mapper.project_map_io, "commit") as send:
            for altered in (dict(self.item, line="Unreviewed change"), dict(self.item, generation="shadow"),
                            dict(self.item, change=dict(self.item["change"], title="Replacement"))):
                self.assertEqual("invalid_map_review", mapper.publish(self.db, REPO, self.config, altered)["code"])
            send.assert_not_called()

    def test_same_second_outsider_edit_to_corroborating_memory_is_refused(self):
        state = self.f.repo()
        comment = state["comments"][str(self.comment)]
        comment["edits"] = [{"at": comment["created_at"], "by": "outsider"}]
        self.f.save(state)
        self.assertEqual("stale_map_evidence", mapper.publish(self.db, REPO, self.config, self.item)["code"])
        self.assertFalse(mapper.project_map_io.current(REPO, self.hub)["nodes"])

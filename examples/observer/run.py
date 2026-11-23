#!/usr/bin/env python3
"""Replay 60 synthetic days offline. This measures mechanics, not model efficacy.

The real source scanner, queue, worker, publisher and hold compiler run against
private temporary files. Model replies and record-context availability are
scripted test doubles. When actual ancestor ownership is unavailable, private
fixture metadata is explicitly simulated; a real hook benchmark stays unavailable.
All gh calls are forced through tests/fake_gh.py.
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/recortex/scripts"))
import index
import names
import observer_context
import observer_holds
import observer_hooks
import observer_publisher
import observer_runtime
import observer_sources
import observer_store
import observer_worker
import project_map
import recortex
from tests.observer_source_fixture import fixture_trusted_ancestors

REPO = "octo-org/octo-repo"
FIXTURE = ROOT / "tests/fixtures/observer-longitudinal.json"


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def percentile(values, percentage):
    """Nearest-rank percentile; never interpolate a fabricated measurement."""
    return sorted(values)[max(0, math.ceil(len(values) * percentage / 100) - 1)]


class Replay:
    def __init__(self, base, fixture, simulate_ancestors=True):
        self.base, self.fixture = Path(base), fixture
        self.start = datetime.datetime.fromisoformat(fixture["start"])
        self.now = self.start.timestamp()
        self.day = 1
        self.labels, self.records = {}, {}
        self.checks, self.capture_ms, self.hook_ms = [], [], []
        self.model_calls, self.daily = [], []
        self.decisions = {}
        self.context_available = True
        self.fail_extract, self.fail_review = False, False   # the sort call fails; the review call fails
        self.by_text = {}
        self.project = self.base / "project"
        self.project.mkdir(mode=0o700)
        self.remote = self.base / "fake-github.json"
        shutil.copyfile(ROOT / "tests/fixtures/repo-state.json", self.remote)
        self.config = {"repo": REPO, "observer": {"enabled": True, "mode": "automatic", "level": "everything"}}
        self.stack = contextlib.ExitStack()
        self.simulate_ancestors = simulate_ancestors
        self.source_ancestry = {"simulated": False, "changed_boundaries": []}

    def __enter__(self):
        # Clear inherited local authorization and faults; an override cannot turn
        # this executable into a live publication or load a real user's source.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith((names.ENV_PREFIX, "FAKE_GH_", "CLAUDE_"))}
        trusted = self.base / "trusted"
        trusted.write_text(REPO + " octocat\n", encoding="utf-8")
        env.update(XDG_STATE_HOME=str(self.base / "state"), XDG_CACHE_HOME=str(self.base / "cache"),
                   XDG_CONFIG_HOME=str(self.base / "config"), CLAUDE_CONFIG_DIR=str(self.base / "native"),
                   CLAUDE_PROJECT_DIR=str(self.project), RECORTEX_TRUSTED_FILE=str(trusted),
                   RECORTEX_GH=shlex.join([sys.executable, str(ROOT / "tests/fake_gh.py")]),
                   FAKE_GH_STATE=str(self.remote), RECORTEX_LINK_PAUSE="0")
        self.stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        source_folder = Path(observer_sources.project_directory(str(self.project)))
        source_folder.mkdir(parents=True, mode=0o700)
        for ancestor in source_folder.parents:
            if ancestor == self.base:
                break
            ancestor.chmod(0o700)
        self.source_preflight = observer_sources.preflight(str(self.project))
        if self.simulate_ancestors and not self.source_preflight["ready"]:
            # Model only a synthetic fixture's system-ancestor ownership. The
            # production scanner and real subprocesses retain their strict policy.
            self.source_ancestry = self.stack.enter_context(fixture_trusted_ancestors(self.base))
            require(observer_sources.preflight(str(self.project))["ready"],
                    "the explicitly modeled private fixture must pass its source preflight")
        self.source_ancestry = dict(self.source_ancestry, actual_preflight=self.source_preflight,
                                   native_ownership_established=self.source_preflight["ready"])
        self.config["observer"]["publisher_id"] = observer_runtime.publisher_id(create=True)
        observer_runtime.approve(REPO, self.config, str(self.project))
        with self.store() as store:
            store.set_control("runtime_gate", {"offline_simulation": True})
            store.set_control("publication_paused", True)
        # A patched gate is expressly not evidence that the actual runtime passed.
        self.stack.enter_context(mock.patch.object(observer_runtime, "gate_valid", return_value=True))
        self.stack.enter_context(mock.patch.object(observer_worker, "record_context", side_effect=self.context))
        self.stack.enter_context(mock.patch.object(observer_worker, "refresh_record", return_value=True))
        self.stack.enter_context(mock.patch.object(time, "time", side_effect=lambda: self.now))
        # Mechanics, not batching: each pass takes what is queued, without waiting for a stretch of the session.
        self.stack.enter_context(mock.patch.object(observer_worker, "ENOUGH", 1))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def store(self, **kwargs):
        return observer_store.Store(REPO, config=self.config, **kwargs)

    def check(self, condition, name):
        require(condition, name)
        self.checks.append(name)

    def context(self, *args, **kwargs):
        return {"targets": [{"issue": 3, "title": "Synthetic work", "body": "Retain supported work"}],
                "memories": [{"id": cid} for cid in self.decisions.values()], "context": "Synthetic test context",
                "incomplete": not self.context_available, "synced_at": self.start.isoformat()}

    def append(self, session, text, label="routine", domain="coding", role="assistant", identity=None,
               subagent=False, tool_name="Bash"):
        folder = Path(observer_sources.project_directory(str(self.project)))
        path = folder / (session + ".jsonl")
        if subagent:
            path = folder / session / "subagents/agent-analysis.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for ancestor in (path.parent, *path.parents):
            if ancestor == self.base:
                break
            ancestor.chmod(0o700)
        identity = identity or str(uuid.uuid5(uuid.NAMESPACE_URL, "%s:%s:%s:%s:%s" %
                                             (self.day, session, label, text, len(self.records))))
        tool_id = "call-" + identity
        content = [{"type": "tool_result", "tool_use_id": tool_id, "content": text,
                    "is_error": label == "failure"}] if role == "tool" else text
        record = {"uuid": identity, "timestamp": datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).isoformat(),
                  "type": "user" if role == "tool" else role, "userType": "external",
                  "message": {"role": "user" if role == "tool" else role, "content": content}}
        with path.open("a", encoding="utf-8") as handle:
            if role == "tool":
                invocation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity + ":invocation"))
                tool_input = {"command": "python3 tests/validate.py --summary"} if tool_name == "Bash" else {"file_path": "notes.txt"}
                invocation = dict(record, uuid=invocation_id, type="assistant", message={"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input}]})
                handle.write(json.dumps(invocation) + "\n")
                self.labels[invocation_id] = {"label": "tool_invocation", "domain": domain,
                                              "text": json.dumps(tool_input), "day": self.day}
                self.records[invocation_id] = invocation
            handle.write(json.dumps(record) + "\n")
        path.chmod(0o600)
        self.labels[identity] = {"label": label, "domain": domain, "text": text, "day": self.day}
        self.by_text.setdefault(text, self.labels[identity])
        self.records[identity] = record
        data = {"session_id": session, "hook_event_name": "SubagentStop" if subagent else "Stop",
                "agent_transcript_path" if subagent else "transcript_path": str(path)}
        return data, identity

    def capture(self, data, hooks=False):
        if data.get("agent_transcript_path"):
            return  # no hook reads a subagent's transcript: it is not the conversation with the user
        path = data["transcript_path"]
        stream = hashlib.sha256(path.encode()).hexdigest()
        # Includes opening/validating/closing the durable store as well as scan
        # and synchronous capture transaction. Transcript creation is excluded.
        started = time.perf_counter_ns()
        with self.store() as store:
            if hooks:
                observer_hooks._capture(store, data["hook_event_name"], data, str(self.project), REPO, self.config)
            else:
                old = store.cursor(stream)
                rows, cursor = observer_sources.scan(data, str(self.project), cursor=old or 0)
                store.capture(rows, stream, cursor, expected_cursor=old)
                require(store.cursor(stream) == Path(path).stat().st_size,
                        "ordinary bounded replay must capture each complete appended record")
        elapsed = (time.perf_counter_ns() - started) / 1e6
        (self.hook_ms if hooks else self.capture_ms).append(elapsed)

    def candidate(self, entry):
        label, domain, text = entry["label"], entry["domain"], entry["text"]
        stamp = self.start + datetime.timedelta(days=entry["day"] - 1)
        iso, title = stamp.strftime("%Y-%m-%dT12:00:00Z"), stamp.strftime("%Y-%m-%d 12:00 UTC")
        kind = "decision" if label in ("decision", "replacement") else "failure" if label == "failure" else "checkpoint"
        marker = "<!-- ghim:event type=%s at=%s -->\n### %s: %s - %s work\n\n" % (kind, iso, kind.title(), title, domain)
        if label == "replacement":
            marker = marker.replace(" -->", " replaces=%d -->" % self.decisions[domain], 1)
        if kind == "decision":
            body = ("**In plain words, we decided to:** " + text.rstrip(".").lower() + "\n\n"
                    "**Decision:** " + text + "\n\n**Decided by:** the project lead, " + title + "\n\n"
                    "**Exact words:**\n\n> " + text + "\n\n**Where recorded:** Original user transcript for this day.\n\n"
                    "**Effect:** The next work follows this constraint.\n\n**Next step:** Verify the stated checks, owner project lead\n")
        elif kind == "failure":
            body = ("**In plain words, what went wrong:** " + text.rstrip(".").lower() + "\n\n"
                    "**What failed:** Four of six checks failed.\n\n**What ran:** The recorded validation command.\n\n"
                    "**Cause:** Not established.\n\n**Impact:** Validation remains incomplete.\n\n"
                    "**Not established:** Whether remaining cases pass.\n\n**Next step:** Investigate failed cases, owner project lead\n")
        else:
            body = ("**In plain words, we paused here:** " + text.rstrip(".").lower() + "\n\n"
                    "**State:** " + text + "\n\n**Done since the last checkpoint:** Reviewed the current task.\n\n"
                    "**In progress:** Validation remains open.\n\n**Next actions:** Project lead reviews remaining checks.\n\n"
                    "**Open questions:** Whether the remaining checks pass.\n")
        return {"kind": kind, "issue": 3, "task_title": "", "topic": 0, "body": marker + body, "excerpts": [text],
                "marks": [], "links": []}

    def invoke(self, payload, schema, purpose, options):
        self.model_calls.append({"day": self.day, "purpose": purpose})
        if purpose == "sort":
            if self.fail_extract:
                self.fail_extract = False
                return {"ok": False, "code": "simulated-interruption"}
            labels, ignored = [], []
            for view in payload["sources"]:
                label = self.by_text[view["text"]]["label"]
                if label in ("routine", "control", "tool_invocation"):
                    ignored.append(view["n"])
                else:
                    kind = "decision" if label in ("decision", "replacement") else "failure" if label == "failure" else "idea"
                    labels.append({"job": "memory", "kind": kind, "sources": [view["n"]], "task": 0, "steps": [], "parts": []})
            return {"ok": True, "structured": {"labels": labels, "ignored": ignored}}
        if purpose == "draft":
            candidates = []
            for label in payload["labels"]:
                if len(candidates) < options["max_candidates"]:
                    view = next(view for view in payload["sources"] if view["n"] == label["sources"][0])
                    candidates.append(dict(self.candidate(self.by_text[view["text"]]), sources=[view["n"]]))
            return {"ok": True, "structured": {"candidates": candidates}}
        if self.fail_review:
            self.fail_review = False
            return {"ok": False, "code": "simulated-interruption"}
        entry = self.by_text[payload["sources"][0]["text"]]
        accepted = entry["label"] != "unsupported"
        return {"ok": True, "structured": {"candidate_hash": payload["candidate_hash"], "accepted": accepted,
                                            "reason": "supported" if accepted else "unsupported"}}

    def work(self, passes=1):
        return [observer_worker.run_once(REPO, self.config, str(self.project), self.invoke) for _ in range(passes)]

    def publication_interruption(self):
        previous = json.loads(self.remote.read_text()).get("log", [])
        before = sum(call[:2] == ["issue", "comment"] for call in previous)
        with self.store() as store:
            event = store.events(states=["ready"], limit=1)[0]
            store.set_control("publication_paused", False)
        with mock.patch.dict(os.environ, {"FAKE_GH_FAIL_AFTER": "issue comment"}):
            result = observer_publisher.publish(REPO, event["eventkey"], self.config)
        self.check(result["state"] == "uncertain", "accepted fake publication without receipt remains uncertain")
        # New Store instances and the publisher's fresh reads model restart.
        result = observer_publisher.reconcile(REPO, event["eventkey"], self.config)
        self.check(result["state"] == "verified", "interrupted fake publication reconciles after reopen")
        observer_publisher.publish(REPO, event["eventkey"], self.config)
        remote = json.loads(self.remote.read_text())
        self.check(sum(call[:2] == ["issue", "comment"] for call in remote["log"]) == before + 1,
                   "reconciliation and repeat publication do not append twice")
        with self.store() as store:
            store.set_control("publication_paused", True)

    def publish_initial_decisions(self):
        """Give each replacement an actual fake-record predecessor, not an invented id."""
        with self.store() as store:
            events = store.events(states=["ready"])
            store.set_control("publication_paused", False)
        for event in events:
            result = observer_publisher.publish(REPO, event["eventkey"], self.config)
            self.check(result["state"] == "verified", "initial synthetic decision has a fake-record receipt")
            domain = "coding" if "- coding work" in event["body"] else "research"
            self.decisions[domain] = result["receipt"]["comment_id"]
        with self.store() as store:
            store.set_control("publication_paused", True)

    def control(self, text, session="owner-controls"):
        data, identity = self.append(session, text, "control", role="user")
        self.capture(data, hooks=True)
        return identity

    def release(self, hold_id, session="owner-controls"):
        """A release typed into the session releases nothing; observer.py release, which the user confirms in a
        terminal of their own, does."""
        data, identity = self.append(session, "/recortex release " + hold_id, "control", role="user")
        self.capture(data, hooks=True)
        with self.store() as store:
            require(any(hold["id"] == hold_id and hold["state"] == "active" for hold in store.holds()),
                    "a release typed into the session must not release the hold")
            store.update_holds(lambda holds: observer_holds.release(holds, hold_id))
        return identity

    def hold_checks(self, released=False):
        with self.store() as store:
            records = store.holds()
        cases = [("git push origin main", not released), ("git push --dry-run", False),
                 ("git tag v0.3.0", True), ("git tag --list", False),
                 ("gh release create v0.3.0", True), ("gh release view v0.3.0", False),
                 ("python publish.py --release", True), ("python publish.py --preview", False)]
        for command, denied in cases:
            answer = observer_holds.check_action(records, REPO, "Bash", {"command": command})
            self.check((answer["decision"] == "deny") == denied, "day %02d explicit hold: %s" % (self.day, command))
        self.check(observer_holds.check_action(records, "octo-org/another", "Bash", {"command": "git push"})["decision"] == "allow",
                   "day %02d hold remains scoped to its repository" % self.day)

    def run(self):
        schedule = self.fixture["schedule"]
        for self.day in range(1, self.fixture["simulated_days"] + 1):
            self.now = self.start.timestamp() + (self.day - 1) * 86400
            self.context_available = self.day != schedule["stale_context_day"]
            self.fail_extract = self.day == schedule["extract_interrupt_day"]
            self.fail_review = self.day == schedule["review_interrupt_day"]
            today = []
            for domain, corpus in self.fixture["domains"].items():
                # The same native session grows for all 60 days, without any
                # SessionStart/resume trigger. Concurrent local sessions also run.
                session = domain + "-continuous"
                label = "routine"
                if self.day in schedule["decision_days"]:
                    label = "decision" if self.day == 1 else "replacement"
                elif self.day in schedule["failure_days"]:
                    label = "failure"
                elif self.day in schedule["unsupported_days"]:
                    label = "unsupported"
                elif self.day % schedule["checkpoint_interval"] == 0:
                    label = "checkpoint"
                # The observer takes notes of the conversation: a failure is what the agent told the user it found.
                role = "user" if label in ("decision", "replacement") else "assistant"
                data, identity = self.append(session, corpus[label], label, domain, role,
                                             subagent=domain == "research" and self.day == 35)
                self.append(session, corpus["routine"], domain=domain)
                self.append(session, "Read the current task again; no new evidence.", domain=domain)
                self.capture(data)
                today.append((data, identity))
                parallel, _ = self.append("%s-local-%02d" % (domain, self.day), corpus["routine"], domain=domain)
                self.capture(parallel)
            if self.day == schedule["fork_day"]:
                original = today[0][1]
                before = len(self.observations())
                data, _ = self.append("forked-session", self.labels[original]["text"], identity=original)
                self.capture(data)
                self.capture(today[0][0])
                self.check(len(self.observations()) == before, "repeated capture and a fork retain one native source identity")
            if self.day == schedule["hold_day"]:
                for action in ("git-push", "git-tag", "gh-release", "command python publish.py --release"):
                    self.control("/recortex hold " + action)
            if self.day == schedule["release_day"]:
                with self.store() as store:
                    hold_id = next(item["id"] for item in store.holds() if item["action"] == "git-push")
                self.release(hold_id)
            if self.day >= schedule["hold_day"]:
                self.control("Continue with the current task.")
                self.hold_checks(released=self.day >= schedule["release_day"])
            if self.day == schedule["backlog_day"]:
                for n in range(schedule["backlog_routine_sources"]):
                    data, _ = self.append("backlog-%02d" % n, "Read queued task %d; nothing changed." % n)
                    self.capture(data)
                for n in range(schedule["backlog_meaningful_sources"]):
                    data, _ = self.append("backlog-meaningful-%02d" % n,
                                          "Queued work item %d paused with validation still open." % n, "checkpoint")
                    self.capture(data)
            if self.day == schedule["budget_day"]:
                with self.store() as store:
                    while store.reserve_invocation("sort") is not None:   # failed sort calls count, as the others do
                        pass
                calls = len(self.model_calls)
                result = self.work()[0]
                self.check(result["code"] == "budget" and calls == len(self.model_calls),
                           "rolling budget includes failed attempts and prevents another model call")
                self.check(any(row["reason"] == "budget" for row in self.observations()), "budget deferral retains pending work")
            if self.day == schedule["mutated_source_day"]:
                path = Path(today[0][0]["transcript_path"])
                lines = path.read_text().splitlines(keepends=True)
                path.write_text("".join(line.replace("three", "eight") if json.loads(line)["uuid"] == today[0][1]
                                        else line for line in lines), encoding="utf-8")
            reports = self.work(passes=5 if self.day == schedule["backlog_day"] else 2)
            if self.day == 1:
                self.publish_initial_decisions()
            if self.day == schedule["extract_interrupt_day"]:
                self.check(any(row["reason"] == "sort_failed" for row in self.observations()),
                           "interrupted extraction leaves its sources pending for a later pass")
            if self.day == schedule["review_interrupt_day"]:
                interrupted = [row for row in self.observations() if row["reason"] == "review_failed"]
                self.check(bool(interrupted), "interrupted review leaves source pending")
                with self.store() as store:
                    pending = {row["source_id"] for row in interrupted}
                    self.check(not any(pending & set(e["candidate"]["sources"]) for e in store.events()),
                               "interrupted review stores no unreviewed draft or excerpt")
            if self.day == schedule["stale_context_day"]:
                self.check(any(row["reason"] == "record_unavailable" for row in self.observations()),
                           "incomplete record context postpones extraction")
            if self.day == schedule["publication_interrupt_day"]:
                self.publication_interruption()
            with self.store() as store:
                store.expire_handled()
                self.daily.append({"day": self.day, "observations": store.status()["observations"],
                                   "events": store.status()["events"], "worker_codes": [item["code"] for item in reports]})
        self.check(any(row["reason"] == "review_rejected" for row in self.observations()),
                   "unsupported scripted claims remain unrecorded after independent rejection")
        self.check(any(row["reason"] == "source_unavailable" for row in self.observations()),
                   "mutated source pointer cannot be reread as the original evidence")
        transient = {"budget", "sort_failed", "draft_failed", "review_failed", "record_unavailable", "worker_budget", "candidate_limit"}
        self.check(not any(row["reason"] in transient for row in self.observations()),
                   "interrupted extraction, review, stale context and budget backlogs eventually drain")
        outcome_counts = {"routine_or_control": 0, "supported": 0, "supporting_invocations": 0,
                          "unsupported": 0, "changed_source": 0}
        for row in self.observations():
            if row["reason"] == "source_unavailable":
                outcome_counts["changed_source"] += 1
                continue
            source = observer_sources.resolve(row["metadata"], str(self.project))
            label = self.labels[source["message_id"]]["label"]
            if label in ("routine", "control"):
                outcome_counts["routine_or_control"] += 1
                require(row["state"] == "ignored", "every scripted routine source must be handled without an event")
            elif label == "unsupported":
                outcome_counts["unsupported"] += 1
                require(row["reason"] == "review_rejected", "every scripted unsupported claim must stay unrecorded")
            elif label == "tool_invocation":
                outcome_counts["supporting_invocations"] += 1
                require(row["state"] in ("ready", "verified"), "retained execution evidence must include its native invocation")
            else:
                outcome_counts["supported"] += 1
                require(row["state"] in ("ready", "verified"), "every scripted supported source must eventually retain its event")
        self.check(outcome_counts["changed_source"] == 1, "only the deliberately changed transcript remains unavailable")
        continuous = []
        for domain in sorted(self.fixture["domains"]):
            records = [row for row in self.observations() if row["metadata"]["session_id"] == domain + "-continuous"]
            retained_days = []
            for row in records:
                if row["state"] not in ("ready", "verified"):
                    continue
                source = observer_sources.resolve(row["metadata"], str(self.project))
                retained_days.append(self.labels[source["message_id"]]["day"])
            self.check(1 in retained_days and 60 in retained_days,
                       domain + " session retains first-day and day-60 meaningful work without resume")
            continuous.append({"domain": domain, "days": 60, "session_count": 1, "resume_hooks": 0,
                               "captured_sources": len(records), "retained_meaningful_days": sorted(set(retained_days))})
        with self.store() as store:
            events = store.events(limit=10000)
            self.check(all(e["review"].get("accepted") for e in events), "every retained event has an accepted review")
            self.check(any(e["state"] == "ready" and e["created"] < self.now - 7 * 86400 and e["candidate"].get("excerpts") for e in events),
                       "pending reviewed excerpts survive beyond seven simulated days")
            for tier, cap in (("sort", 80), ("middle", 70)):
                reservations = [row[0] for row in store.db.execute(
                    "SELECT created FROM invocations WHERE tier=? ORDER BY created", (tier,))]
                maximum = max([sum(t - 86400 < other <= t for other in reservations) for t in set(reservations)] or [0])
                self.check(maximum <= cap, "all rolling simulated 24-hour windows stay within %d %s calls" % (cap, tier))
            status = store.status()
        return {"days": self.fixture["simulated_days"], "domains": sorted(self.fixture["domains"]),
                "native_sessions": len({row["metadata"]["session_id"] for row in self.observations()}),
                "model_calls": len(self.model_calls), "retained_events": len(events), "final_queue": status,
                "continuous_sessions": continuous, "scripted_expected_outcomes": outcome_counts,
                "daily": self.daily, "checks": self.checks}

    def observations(self):
        with self.store() as store:
            return [store.observation(row[0]) for row in store.db.execute("SELECT source_id FROM observations")]


def context_replay(replay):
    """Real retrieval over 60 synthetic days, independent of scripted selection."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(index.SCHEMA)
    db.executemany("INSERT INTO meta VALUES (?,?)", [("mind_derived", "1"), ("sync_complete", "1"),
                                                   ("synced_at", "2026-01-01T12:00:00Z"), ("hub", "1")])
    db.executemany("INSERT INTO labels VALUES (?,?)", [(3, "topic:reader"), (4, "topic:assay")])
    def memory(cid, issue, text, kind="decision", day=1):
        stamp = (datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=day - 1)).isoformat()
        db.execute("INSERT INTO comments VALUES (?,?,?,?,?,?,?)", (cid, issue, "octocat", text, stamp, stamp, ""))
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", (cid, issue, kind, stamp, "", 0, text, "octocat", "", stamp))
        db.execute("INSERT INTO event_fields VALUES (?,0,'plain',?)", (cid, text))
    memory(1, 3, "pipe reader uses strict quoting")
    memory(2, 3, "pipe reader requires line and column warnings", day=30)
    memory(3, 4, "assay comparison retains stated exclusions")
    memory(4, 4, "assay comparison adds sensitivity analysis", day=30)
    db.executemany("INSERT INTO links VALUES (?,?,?)", [(2, "replaces", 1), (4, "replaces", 3)])
    for day in range(1, 61):
        for n in range(4):
            memory(100 + day * 4 + n, 9, "unrelated formatting adjustment", "result", day)
    now = datetime.datetime(2026, 3, 2, 12, tzinfo=datetime.timezone.utc)
    outcomes = []
    for query, issue, pair in (("pipe strict quoting", 3, {1, 2}), ("assay exclusions", 4, {3, 4})):
        packet = observer_context.build_packet(db, REPO, {"octocat"}, query=query, issue=issue, now=now)
        require(pair <= set(packet["selected"]), "old decision and replacement must survive recent distractors")
        require("recorded state: replaced" in packet["text"], "retrieval must identify replaced decisions")
        require("2026-01-01T12:00:00Z" in packet["text"] and "not revalidated" in packet["text"],
                "old snapshot age and lack of revalidation must remain visible")
        outcomes.append({"query": query, "required_ids": sorted(pair), "selected_ids": packet["selected"],
                         "snapshot_age_days": 60, "live_revalidated": packet["freshness"]["live_revalidated"], "passed": True})
    # Exercise delivery receipts in the very same session, without a restart:
    # unchanged packets suppress repetition; a new replacement changes the packet.
    class BorrowedConnection:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    data = {"session_id": "coding-continuous", "prompt": "pipe strict quoting"}
    with replay.store() as store, mock.patch.object(index, "connect", return_value=BorrowedConnection()):
        first = observer_hooks._context(store, "UserPromptSubmit", data, REPO, {"octocat"})
        repeated = observer_hooks._context(store, "UserPromptSubmit", data, REPO, {"octocat"})
        memory(5, 3, "pipe strict quoting requires escaped delimiter checks", day=60)
        db.execute("INSERT INTO links VALUES (5,'replaces',2)")
        changed = observer_hooks._context(store, "UserPromptSubmit", data, REPO, {"octocat"})
        require(first and not repeated, "unchanged task context must be deduplicated in the same ongoing session")
        require("escaped delimiter checks" in changed and changed != first,
                "a new replacement must refresh task context in the same ongoing session")
    db.close()
    return outcomes, {"session_id": "coding-continuous", "resume_hooks": 0,
                      "unchanged_context_deduplicated": True, "replacement_refreshes_context": True}


def full_hook_latency(fixture, samples=30):
    """Measure the shipped shell entry point with actual capture and no runtime gate."""
    timings = []
    with tempfile.TemporaryDirectory(prefix="observer-hook-latency-") as folder:
        with Replay(folder, fixture, simulate_ancestors=False) as replay:
            # This preflight reads real metadata for this distinct fixture tree.
            # Never measure a mocked subprocess or reuse a pre-hardening result.
            actual_preflight = observer_sources.preflight(str(replay.project))
            if not actual_preflight["ready"]:
                return {"status": "unavailable", "reason": actual_preflight["code"],
                        "preflight": actual_preflight, "source_ancestry_simulated": False,
                        "samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None,
                        "target_ms": 100, "target_met": None, "observations_captured": 0,
                        "invocations": 0, "gh_calls": 0, "measurements": [],
                        "limitation": "Actual native-source ownership or permissions could not be established; no hook benchmark ran."}
            replay.config["observer"]["mode"] = "shadow"
            # The project file names the repository only; the settings are this machine's, for this folder.
            (replay.project / ".recortex.json").write_text(json.dumps({"repo": REPO}), encoding="utf-8")
            observer_runtime.approve(REPO, replay.config, str(replay.project))
            with replay.store() as store:
                # The child is a fresh interpreter: no mock, runtime proof,
                # terminal events or maintenance trigger can start a worker.
                store.set_control("runtime_gate", None)
            for n in range(samples):
                data, _ = replay.append("latency-session", "Ordinary capture measurement %d" % n, role="user")
                event = "prompt-submit" if n % 2 else "stop"
                data["hook_event_name"] = "UserPromptSubmit" if n % 2 else "Stop"
                data["prompt"] = "Ordinary capture measurement %d" % n
                started = time.perf_counter_ns()
                done = subprocess.run(["bash", str(ROOT / "skills/recortex/scripts/run_observer_hook.sh"), event],
                                      input=json.dumps(data), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      cwd=str(replay.project), env=dict(os.environ), timeout=10)
                elapsed = (time.perf_counter_ns() - started) / 1e6
                require(done.returncode == 0, "full hook must finish successfully")
                with replay.store() as store:
                    require(len(store.pending()) == n + 1, "full hook timing sample must durably capture its fresh source")
                    require(store.status()["invocations_24h"] == 0, "latency measurement must not invoke a model")
                timings.append({"event": event, "ms": round(elapsed, 3)})
            require(not json.loads(replay.remote.read_text()).get("log"), "full hook must make no gh call")
    values = [item["ms"] for item in timings]
    p95 = percentile(values, 95)
    return {"status": "available", "source_ancestry_simulated": False, "preflight": actual_preflight,
            "samples": len(values), "p50_ms": round(percentile(values, 50), 3), "p95_ms": round(p95, 3),
            "max_ms": round(max(values), 3), "target_ms": 100, "target_met": p95 < 100,
            "observations_captured": samples, "invocations": 0, "gh_calls": 0, "measurements": timings,
            "mode": "approved shadow capture; runtime gate unavailable; local index absent",
            "scope": "shell launch, Python startup, configuration, source scan, holds, SQLite and hook output",
            "environment": {"python": sys.version.split()[0], "platform": sys.platform,
                            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None},
            "limitation": "Local repeated-process measurement; filesystem cache and concurrent system load affect timing."}


def project_map_replay(days):
    """Two readers rebuild a mixed project from the same chronological hub comments."""
    schedule = {
        1: ("user", {"action": "add", "id": "instruments", "title": "Instruments"}),
        2: ("observer", {"action": "add", "id": "reader", "title": "Field reader", "parent": "instruments",
                         "evidence": ["comment:4002"]}),
        8: ("user", {"action": "add", "id": "study", "title": "Moisture variation"}),
        9: ("user", {"action": "move", "id": "reader", "parent": "study"}),
        14: ("user", {"action": "rename", "id": "reader", "title": "Calibration"}),
        17: ("user", {"action": "add", "id": "paper", "title": "Manuscript", "links": ["study"]}),
        23: ("user", {"action": "delete", "id": "reader", "children": "subtree"}),
        52: ("user", {"action": "restore", "id": "reader"}),
    }
    log = []
    working = project_map.empty()
    for day in range(1, days + 1):
        if day in schedule:
            source, change = schedule[day]
            op = project_map.prepare(working, change, source, "change-%d" % day)
            row = {"id": 5000 + day * 2, "author": "octocat", "created_at": "day-%d" % day,
                   "body": project_map.encode(REPO, 4, op)}
            log.append(row)
            working = project_map.apply(working, op, row["created_at"], row["author"], row["id"])
            if day == 14:  # the acknowledgment was lost; another client tries the same correction
                log.append(dict(row, id=row["id"] + 1))
        rebuilt = project_map.replay(REPO, 4, ["octocat"], log)
        require(rebuilt == working, "both devices rebuild the identical map on day %d" % day)
        if 23 <= day < 52:
            require(rebuilt["nodes"]["reader"]["deleted"], "a quiet, deleted branch stays deleted")
        if day == 40:
            try:
                project_map.prepare(rebuilt, {"action": "add", "id": "again", "title": "Calibration",
                                              "parent": "study", "evidence": ["comment:4002"]}, "observer")
            except project_map.MapError:
                pass
            else:
                raise AssertionError("old evidence recreated a deleted branch")
    require(working["nodes"]["reader"]["title"] == "Calibration" and
            not working["nodes"]["reader"]["deleted"] and working["nodes"]["reader"]["parent"] == "study",
            "deliberate restoration keeps the corrected branch and its place")
    return {"days_rebuilt": days, "accepted_changes": len(working["history"]), "ledger_entries": len(log),
            "project_branches": len(working["nodes"]), "model_calls": 0}


def run_replay():
    started = time.perf_counter()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Native source security stays strict. Only the synthetic replay may model
    # known fixture ancestry when this executor cannot establish actual owners.
    with tempfile.TemporaryDirectory(prefix="observer-replay-") as folder:
        with Replay(folder, fixture) as replay:
            simulated = replay.run()
            ancestry = replay.source_ancestry
            latency = {}
            for name, samples in (("scan_store_capture", replay.capture_ms), ("hook_capture_controls", replay.hook_ms)):
                p95 = percentile(samples, 95)
                latency[name] = {"status": "fixture_only" if ancestry["simulated"] else "available",
                                 "source_ancestry_simulated": ancestry["simulated"],
                                 "samples": len(samples), "p50_ms": round(percentile(samples, 50), 3),
                                 "p95_ms": round(p95, 3), "max_ms": round(max(samples), 3),
                                 "target_ms": 100, "target_met": None if ancestry["simulated"] else p95 < 100}
            retrieval, delivery = context_replay(replay)
    latency["full_hook_process"] = full_hook_latency(fixture)
    return {"schema_version": 2, "kind": "deterministic_offline_longitudinal_replay", "simulated": simulated,
            "source_ancestry": ancestry, "project_map": project_map_replay(fixture["simulated_days"]),
            "elapsed_wall_seconds": round(time.perf_counter() - started, 3), "capture_latency": latency,
            "context_retrieval": retrieval, "continuous_context_delivery": delivery,
            "real_model_invocations": 0, "live_github_calls": 0,
            "runtime_feasibility": "not established by this harness; the test gate is patched",
            "efficacy": {"recording_precision": {"target": 0.98, "measured": None},
                         "meaningful_recall": {"target": 0.90, "measured": None},
                         "advice_relevance": {"target": 0.90, "measured": None},
                         "status": "pending independent human-labeled live coding and research trials"},
            "limitations": ["Synthetic time is not elapsed trial time.",
                            "Scripted selection and review test queue mechanics, not semantic judgment.",
                            "Worker index refresh and context availability are scripted; retrieval is separately exercised.",
                            "Simulated fixture ancestry does not establish real native capture or the latency target.",
                            "A failed actual source preflight leaves the full hook benchmark unavailable with null timing and target fields.",
                            "Inner latency excludes process startup; full_hook_process includes the shipped shell/Python path.",
                            "All latency samples exclude transcript writing, model work, and GitHub.",
                            "Timing and machine identity vary by environment; scenario outcomes are deterministic."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the measured replay report as JSON")
    args = parser.parse_args()
    report = run_replay()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("PASS: 60 synthetic coding/research days; %d checks, %.3f seconds actual elapsed time." %
              (len(report["simulated"]["checks"]), report["elapsed_wall_seconds"]))
        print("%s capture p95: %.3f ms; hook capture/control p95: %.3f ms." %
              ("Fixture-only" if report["source_ancestry"]["simulated"] else "Native",
               report["capture_latency"]["scan_store_capture"]["p95_ms"],
               report["capture_latency"]["hook_capture_controls"]["p95_ms"]))
        full = report["capture_latency"]["full_hook_process"]
        if full["status"] == "unavailable":
            print("Full hook process benchmark unavailable: %s. The 100 ms target is not established." % full["reason"])
        else:
            print("Full hook process p95: %.3f ms; 100 ms target %s." %
                  (full["p95_ms"], "met" if full["target_met"] else "not met"))
        print("Real model efficacy and live multi-day trials: not measured.")


if __name__ == "__main__":
    main()

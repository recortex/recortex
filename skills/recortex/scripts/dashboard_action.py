#!/usr/bin/env python3
"""The live dashboard server of a machine, and the commands that reach it.

  dashboard_action.py open [--suggestions | --impacts [ITEM]]   what /recortex:dashboard runs
  dashboard_action.py link                                      the link for this project (view only from a session)
  dashboard_action.py status                                    running or not, and where
  dashboard_action.py stop                                      stops the server
  dashboard_action.py serve                                     the server itself; the session-start hook starts it detached

One server serves every project of the user on one host, on 127.0.0.1 only, behind an unpredictable address prefix and two keys:
a view key (the session may be told its link; it opens no change) and a change key (its link reaches the user only in the hook's
systemMessage, the state file, a launch file and their own terminal). The server stops 24 hours after the last session pinged
it, or on Stop. Every request names a project, which is checked again against the same rules as a hook. The exported mind page
stays offline and static; the service previews and records task completions and project map edits, decision reviews and
reminder choices, each after the user's confirmation in the page.
"""

import argparse
import contextlib
import hashlib
import hmac
import http.cookies
import http.server
import io
import json
import os
import pathlib
import re
import secrets
import sys
import time

import dashboard_notes
import decision_review_io
import index
import observer_advisory
import observer_publisher
import observer_runtime
import observer_store
import post_verified
import project_map
import record_store
import recortex
import storage
import dashboard_server
import tasks

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "page")
IDENT = re.compile(r"[0-9a-f]{32}")
IMPACT_TARGET = re.compile(r"impact-[1-9][0-9]{0,18}-[0-9a-f]{64}")
STEP = re.compile(r"([1-9][0-9]{0,12}):([1-9][0-9]{0,15}):([1-9][0-9]?)")
MAX_BODY = 16384
PREVIEW_SECONDS = 600
TICK_SECONDS = 1.0              # how often the serve loop looks at the clock when no request arrives
LAUNCH = re.compile(r"launch-[0-9a-f]{8}\.html")
LAUNCH_SECONDS = 60             # a launch file is removed this long after it was made
PROJECTS_KEPT = 50
HOST = re.compile(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?")
POSTS = ("/api/preview", "/api/commit", "/api/reconcile", "/api/refresh", "/api/map/preview", "/api/map/commit",
         "/api/suggestions/feedback", "/api/impact/preview", "/api/impact/commit", "/api/impact/resume",
         "/api/impact/apply", "/api/stop")   # every address that changes anything: each needs the change key
PROJECT_PATH = re.compile(r"/p/([0-9a-f]{16})(/.*)?")
STOPPED = {"stopped": "The live dashboard was stopped.",
           "idle": "The live dashboard stopped 24 hours after the last session.",
           "interrupted": "The live dashboard was interrupted."}
SWITCHED_OFF = "The live dashboard is switched off here (RECORTEX_DASHBOARD or the plugin's settings.json)."
MAYBE_NOT_REACHED = "If the link does not open on your machine, the page can also go on the web."
VIEW_LINE = "live dashboard, view only: %s"
ABOVE = "Your private link with changes on is in the line recortex showed above this reply."
POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
          "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")
HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recortex task actions</title><link rel="stylesheet" href="action.css">
<script src="action.js" defer></script><script src="stop.js" defer></script><main><h1>Task actions</h1>
<p id="project">Connect using the private address from the terminal or the launch file that started the service.</p>
<p><a id="back" href="snapshot#todo">Back to Work</a></p>
<p>This page records your statement that one step is complete. It does not independently verify the work.
The saved dashboard remains an offline snapshot.</p>
<p>If the observer is enabled at Everything, finishing the last step can let it close the task later.</p>
<button id="refresh" type="button">Refresh from GitHub</button>
<p id="status" role="status" aria-live="polite"></p><section id="tasks" aria-label="Task steps"></section>
<section id="preview" hidden aria-labelledby="review-title"><h2 id="review-title">Review completion</h2>
<p id="review-step"></p><blockquote id="review-statement"></blockquote>
<details id="exact-record"><summary>Read the exact checkpoint</summary><pre id="body"></pre></details>
<button id="confirm" type="button">Record this completion</button>
<button id="cancel" type="button">Cancel</button></section>
<section aria-label="Recent operations"><h2>Recent operations</h2><div id="operations"></div></section>
</main></html>"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ActionError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def quiet(function, *args, **kwargs):
    # Existing helpers print record text and filesystem paths for terminal use.
    # They never become HTTP error messages or request logs.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **kwargs)


class Actions:
    def __init__(self):
        self.root = os.path.realpath(recortex.project_root())
        self.config = recortex.load_config()
        if not os.path.isdir(self.root) or not recortex.valid_repo(self.config.get("repo")):
            raise ActionError("configured_project_required")
        self.repo = recortex.resolve_repo(None, self.config).lower()
        self.records = record_store.for_repo(self.repo, self.config)
        self.config_hash = digest(canonical(self.config))
        self.session = secrets.token_hex(16)
        self.map_previews = {}
        self.map_edited = False
        self.impact_dirty = False
        self.authority()
        scope = digest(self.root)[:32]
        self.folder = storage.folder(storage.state_top(), storage.repo_folder(self.repo), "dashboard", scope)
        self.locks = storage.folder(storage.state_top(), storage.repo_folder(self.repo), "observer")
        self.key = storage.user_key()

    def authority(self):
        config = recortex.load_config()
        if (os.path.realpath(recortex.project_root()) != self.root or
                recortex.resolve_repo(None, config).lower() != self.repo or
                digest(canonical(config)) != self.config_hash):
            raise ActionError("project_changed")
        allowed = recortex.allowed_users(self.repo)
        if not allowed:
            raise ActionError("trust_required", 403)
        return allowed

    def account(self):
        state, login = self.records.account_state()
        if state != "ready" or login.lower() not in {name.lower() for name in self.authority()}:
            raise ActionError("allowed_account_required", 403)
        return login

    @contextlib.contextmanager
    def lock(self):
        # Share the worker lock so another local observer pass cannot race a
        # dashboard mark. The dashboard lock also covers previews/reconciliation.
        try:
            with observer_store._exclusive_lock(self.folder, "actions.lock"), \
                    observer_store._exclusive_lock(self.locks, "worker.lock"), \
                    observer_store._exclusive_lock(self.locks, "publication.lock"):
                yield
        except observer_store.StoreError as error:
            raise ActionError("busy") from error

    def holds(self, issue, operation=None):
        try:
            held = observer_store.read_holds(self.repo)
        except (observer_store.StoreError, storage.StorageError, OSError, ValueError):
            raise ActionError("holds_unavailable", 403)
        # This surface has no native session identity. Publication holds apply
        # conservatively; an unrelated exact-command hold must not block a mark.
        command = recortex.show_command(["issue", "comment", str(issue), "--repo", self.repo, "--body-file",
                                        os.path.join(self.folder, operation["id"] + ".md")]) if operation else None
        for hold in held:
            if not isinstance(hold, dict):
                raise ActionError("holds_unavailable", 403)
            if (hold.get("state") == "active" and hold.get("repo") == self.repo and
                    hold.get("scope", {}).get("issue", issue) == issue and
                    (hold.get("action") == "publish" or (command is not None and hold.get("action") == "command" and
                     hold.get("command", "").strip() == command.strip()))):
                raise ActionError("held", 403)

    def sync(self):
        self.authority()
        result = quiet(index.sync, self.repo, self.config.get("hub"), rebuild=True, timeout=30, deadline=120)
        if not result.get("complete"):
            raise ActionError("sync_incomplete")
        self.authority()
        return result

    def report(self):
        allowed = self.authority()
        try:
            return tasks.report_of(self.repo, allowed)
        except (SystemExit, storage.StorageError):
            raise ActionError("index_unavailable")

    def step(self, ident):
        match = STEP.fullmatch(ident) if isinstance(ident, str) else None
        if not match:
            raise ActionError("invalid_step", 400)
        issue, comment, ordinal = map(int, match.groups())
        task = tasks.find_task(self.report(), issue)
        if task is None or task["state"] != "open":
            raise ActionError("step_changed")
        row = next((s for s in task["steps"] if s["comment"] == comment and s["ordinal"] == ordinal), None)
        if row is None or row["state"] != "open":
            raise ActionError("step_changed")
        # The complete derived target is bound, not the unstable displayed number.
        target = {"issue": issue, "comment": comment, "ordinal": ordinal,
                  "text": row["text"], "title": task["title"] or "Title withheld", "state": row["state"]}
        target["task_body_hash"] = digest(recortex.read_issue_body(self.repo, issue))
        target["steps_body_hash"] = digest(recortex.read_comment_body(self.repo, comment))
        return target

    def path(self, ident):
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            raise ActionError("invalid_operation", 400)
        return os.path.join(self.folder, ident + ".json")

    def save(self, operation):
        payload = canonical(operation)
        signed = {"operation": operation, "signature": hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()}
        path = self.path(operation["id"])
        storage.write_file(path, canonical(signed))
        # Persist send intent before any remote write, including across a crash.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        descriptor = os.open(self.folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def load(self, ident):
        raw = storage.read_file(self.path(ident))
        if raw is None:
            raise ActionError("operation_unknown", 404)
        if len(raw) > 65536:
            raise ActionError("operation_invalid")
        signed = json.loads(raw)
        operation = signed["operation"]
        signature = hmac.new(self.key, canonical(operation).encode(), hashlib.sha256).hexdigest()
        if (not hmac.compare_digest(signature, signed.get("signature", "")) or
                operation.get("id") != ident or operation.get("repo") != self.repo or
                operation.get("root") != self.root or operation.get("schema") != 1 or
                digest(operation.get("body", "")) != operation.get("body_hash")):
            raise ActionError("operation_invalid")
        return operation

    def operations(self):
        entries = [n[:-5] for n in os.listdir(self.folder) if n.endswith(".json") and IDENT.fullmatch(n[:-5])]
        return [self.load(n) for n in entries]

    def prune_previews(self):
        # Only unsent, expired previews are disposable. Receipts and ambiguous
        # sends remain durable, regardless of age or operation count.
        for operation in self.operations():
            if operation["state"] in ("preview", "refused") and time.time() - operation["created"] > PREVIEW_SECONDS:
                for suffix in (".json", ".md"):
                    storage.remove_file(self.folder, operation["id"] + suffix)

    def require_no_pending_step(self, step_id):
        if any(o["state"] in ("sending", "uncertain") and o["step_id"] == step_id for o in self.operations()):
            raise ActionError("reconcile_first")

    def require_publisher(self, operation):
        # Called again at each write boundary; credentials can change while a
        # live read or dry run is in progress.
        if self.account() != operation["account"]:
            raise ActionError("account_changed", 403)
        self.holds(operation["target"]["issue"], operation)

    @staticmethod
    def public(operation):
        result = {k: operation[k] for k in ("id", "state", "created", "receipt", "refresh") if k in operation}
        # Receipt history is metadata. Do not redisplay cached titles or step
        # wording after the record's authorship/trust may have changed.
        result["target"] = {k: operation["target"][k] for k in ("issue", "comment", "ordinal")}
        return result

    def state(self):
        report = self.report()
        shown = []
        for task in report["tasks"]:
            if task["state"] != "open":
                continue
            shown.append({"issue": task["issue"], "title": task["title"] or "Title withheld", "steps": [
                {"id": "%d:%d:%d" % (task["issue"], s["comment"], s["ordinal"]),
                 "text": s["text"], "state": s["state"]} for s in task["steps"]]})
        history = sorted(self.operations(), key=lambda o: o["created"], reverse=True)
        unresolved = [o for o in history if o["state"] in ("sending", "uncertain")]
        recent = [o for o in history if o["state"] not in ("sending", "uncertain")][:20]
        ops = unresolved + recent  # an unresolved operation must never lose its recovery control
        return {"repo": self.repo, "tasks": shown[:100], "omitted_tasks": max(0, len(shown) - 100),
                "operations": [self.public(o) for o in ops]}

    def preview(self, step_id, words):
        if (not isinstance(words, str) or not 1 <= len(words.strip()) <= 1000 or
                re.search(r"[\x00-\x08\x0b-\x1f\x7f]", words)):
            raise ActionError("completion_words_required", 400)
        with self.lock():
            self.sync()
            target = self.step(step_id)
            self.holds(target["issue"])
            account = self.account()
            self.prune_previews()
            self.require_no_pending_step(step_id)
            ident = secrets.token_hex(16)
            body = completion_body(self.config, target, words.strip(), account, ident)
            if observer_publisher.outbound_problem(body, self.repo):
                raise ActionError("content_refused", 400)
            operation = {"schema": 1, "id": ident, "repo": self.repo, "root": self.root, "session": self.session,
                         "config": self.config_hash, "account": account, "step_id": step_id, "target": target,
                         "created": time.time(), "state": "preview", "body": body, "body_hash": digest(body)}
            self.save(operation)
            self.holds(target["issue"], operation)
            # The normal live linter, source binding and queued-copy checks apply.
            if self.post(operation, dry_run=True) != 0:
                operation["state"] = "refused"
                self.save(operation)
                raise ActionError("content_refused")
            return dict(self.public(operation), body=body, body_hash=operation["body_hash"],
                        statement=words.strip(), step_text=target["text"])

    def post(self, operation, dry_run=False):
        path = os.path.join(self.folder, operation["id"] + ".md")
        storage.write_file(path, operation["body"])
        argv = ["comment", "--repo", self.repo, "--issue", str(operation["target"]["issue"]),
                "--kind", "checkpoint", "--body-file", path]
        return quiet(post_verified.main, argv + (["--dry-run"] if dry_run else []))

    def commit(self, ident, body_hash):
        with self.lock():
            self.authority()
            operation = self.load(ident)
            if operation["state"] in ("recorded", "sending", "uncertain"):
                return self._reconcile(operation)
            if operation["state"] != "preview":
                raise ActionError("preview_required")
            if (operation["session"] != self.session or operation["config"] != self.config_hash or
                    time.time() - operation["created"] > PREVIEW_SECONDS or
                    not isinstance(body_hash, str) or not hmac.compare_digest(operation["body_hash"], body_hash)):
                raise ActionError("preview_expired_or_changed")
            # A second preview may predate an ambiguous send by this or another
            # launch. A fresh index cannot prove that send was not accepted.
            self.require_no_pending_step(operation["step_id"])
            self.sync()
            if self.step(operation["step_id"]) != operation["target"]:
                raise ActionError("step_changed")
            self.holds(operation["target"]["issue"], operation)
            if self.account() != operation["account"]:
                raise ActionError("account_changed", 403)
            if self.post(operation, dry_run=True) != 0:
                raise ActionError("content_refused")
            self.authority()
            self.holds(operation["target"]["issue"], operation)
            if self.step(operation["step_id"]) != operation["target"]:
                raise ActionError("step_changed")
            self.require_publisher(operation)
            operation["state"] = "sending"
            self.save(operation)
            try:
                self.post(operation)
            except (Exception, SystemExit):
                # A timeout may have occurred after acceptance. Never append again.
                pass
            operation["state"] = "uncertain"
            self.save(operation)
            return self._reconcile(operation)

    def reconcile(self, ident):
        with self.lock():
            self.authority()
            operation = self.load(ident)
            if operation["state"] not in ("sending", "uncertain", "recorded"):
                raise ActionError("not_sent")
            return self._reconcile(operation)

    def _reconcile(self, operation):
        if operation["state"] == "recorded":
            return self.public(operation)
        try:
            issue = operation["target"]["issue"]
            if operation["account"].lower() not in {n.lower() for n in self.authority()}:
                raise ActionError("allowed_account_required")
            rows = recortex.parse_json_stream(self.records.list_entries(issue))
            matches = [r for r in rows if isinstance(r, dict) and
                       "eventkey=" + operation["id"] in (r.get("body") or "")]
            if len(matches) != 1 or type(matches[0].get("id")) is not int:
                raise ActionError("receipt_unavailable")
            number = matches[0]["id"]
            row = self.records.read_entry(number)
            expected_issue = self.records.api_issue(issue)
            if (row.get("issue_url", "").lower() != expected_issue or
                    row.get("user", {}).get("login") != operation["account"] or
                    recortex.normalize(row.get("body", "")) != recortex.normalize(operation["body"]) or
                    row.get("created_at") != row.get("updated_at")):
                raise ActionError("receipt_mismatch")
            operation["state"] = "recorded"
            operation["receipt"] = {"comment": number,
                                    "url": self.records.address(issue, number)}
            operation["refresh"] = "pending"
            self.save(operation)
            dashboard_notes.add(self.repo, "step", issue)   # a session that began before this learns of it on its next prompt
        except (Exception, SystemExit):
            operation["state"] = "uncertain"
            self.save(operation)
            return self.public(operation)
        # These are derived copies. Their failure cannot turn a posted memory into
        # an unposted action or authorize another append.
        try:
            self.sync()
            self.holds(issue, operation)
            rendered = quiet(tasks.render_task, self.repo, self.config, issue,
                             before_write=lambda: self.require_publisher(operation))
            if not rendered["ok"]:
                operation["refresh"] = "task_copy_failed"
            elif rendered["reason"] in ("no plan", "no steps"):
                operation["refresh"] = "no_task_list"
            else:
                operation["refresh"] = "updated" if rendered["wrote"] else "current"
        except (Exception, SystemExit):
            operation["refresh"] = "failed"
        self.save(operation)
        return self.public(operation)

    def refresh(self):
        with self.lock():
            self.sync()
            import mind
            if quiet(mind.main, []) != 0:
                raise ActionError("snapshot_failed")
            return self.state()

    @staticmethod
    def map_response(state):
        import mind
        return {"state": state, "anchors": {key: mind.branch_anchor(key) for key in state["nodes"]}}

    def map_state(self):
        import project_map_io
        self.authority()
        self.account()
        return self.map_response(project_map_io.current(self.repo, self.config["hub"]))

    def map_preview(self, change, revision):
        import project_map_io
        if not isinstance(change, dict) or not isinstance(revision, str):
            raise ActionError("invalid_fields", 400)
        account = self.account()
        result = project_map_io.preview(self.repo, self.config["hub"], change)
        operation = result["operation"]
        if operation["base"] != revision:
            raise ActionError("map_changed")
        self.map_previews = {k: v for k, v in self.map_previews.items() if time.monotonic() < v["expires"]}
        if len(self.map_previews) >= 32:
            del self.map_previews[next(iter(self.map_previews))]
        token = secrets.token_hex(16)
        self.map_previews[token] = {"operation": operation, "account": account, "expires": time.monotonic() + PREVIEW_SECONDS}
        return {"id": token, "affected": result["affected"], "state": result["state"]}

    def map_commit(self, token):
        import project_map_io
        if not isinstance(token, str) or not IDENT.fullmatch(token):
            raise ActionError("invalid_fields", 400)
        saved = self.map_previews.get(token)
        if not saved or time.monotonic() >= saved["expires"]:
            raise ActionError("preview_expired_or_changed")
        if self.account() != saved["account"]:
            raise ActionError("account_changed", 403)
        result = project_map_io.commit(self.repo, self.config["hub"], saved["operation"])
        self.map_edited = True
        if not saved.get("noted"):   # a replay of the same preview writes no second note
            saved["noted"] = True
            dashboard_notes.add(self.repo, "map", self.config["hub"])
        return dict(self.map_response(result["state"]), comment=result["comment"])

    def suggestions(self, ident=None, muted=None):
        """Read personal receipts in this folder. Only the current trusted index supplies record text."""
        if ident is not None and (not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{64}", ident)
                                  or type(muted) is not bool):
            raise ActionError("invalid_fields", 400)
        allowed = self.authority()
        config = observer_runtime.local_config(self.config, self.root)
        enabled = observer_runtime.approval_valid(self.repo, config, root=self.root)
        mode = observer_runtime.settings(config)["advisory_mode"] if enabled else "disabled"
        result = {"repo": self.repo, "mode": mode, "suggestions": []}
        if not enabled:
            if ident is not None:
                raise ActionError("observer_disabled", 403)
            return result
        try:
            with observer_store.Store(self.repo, config=config, create=False, readonly=ident is None) as store:
                rows = observer_advisory.history(store, root=self.root)
                if ident is not None:
                    rows = [row for row in rows if row["id"] == ident]
                    if not rows:
                        raise ActionError("suggestion_missing", 404)
                if rows:
                    db = index.connect(self.repo, create=False)
                    if db is None:
                        raise ActionError("index_unavailable")
                    try:
                        if not index.mind_ready(db):
                            raise ActionError("index_unavailable")
                        memories = index.memories(db, allowed)["items"]
                    finally:
                        db.close()
                    for row in rows:
                        sources = []
                        for ref in row["sources"]:
                            memory = memories.get(ref["comment"])
                            if not memory or (memory["issue"], memory["kind"]) != (ref["issue"], ref["kind"]):
                                break
                            sources.append(dict(ref, summary=(memory["plain"] or memory["title"] or "")[:400],
                                                state=memory["state"],
                                                url=self.records.address(ref["issue"], ref["comment"])))
                        else:
                            result["suggestions"].append(dict(row, available=True, sources=sources))
                            continue
                        result["suggestions"].append({"id": row["id"], "muted": row["muted"],
                                                      "at": row["at"], "available": False})
                if ident is not None:
                    row = result["suggestions"][0]
                    if muted and not row["available"]:
                        raise ActionError("sources_unavailable")
                    try:
                        updated = observer_advisory.feedback(store, ident, muted, root=self.root)
                    except KeyError:
                        raise ActionError("suggestion_missing", 404)
                    except ValueError as error:
                        if str(error) == "mute_limit":
                            raise ActionError("mute_limit")
                        raise
                    row["muted"] = updated["muted"]
                    return row
        except observer_store.StoreError as error:
            if error.reason != "missing":
                raise
            if ident is not None:
                raise ActionError("suggestion_missing", 404)
        return result

    def project_model(self):
        import mind_model
        allowed = self.authority()
        db = index.connect(self.repo, create=False)
        if db is None:
            raise ActionError("index_unavailable")
        try:
            if not index.mind_ready(db):
                raise ActionError("index_unavailable")
            return mind_model.build(db, self.repo, allowed, tasks.now().date(),
                                     recortex.shipped_profiles(self.config.get("profiles")), self.config)
        finally:
            db.close()

    def snapshot(self, base="", interactive=True, live=False):
        """Render authenticated project context; never change the standalone export. `interactive` False is the view-only page
        (its buttons copy requests into the chat); `live` is the page of the live server, which follows the record."""
        import mind
        import mind_figures
        import usage
        if self.impact_dirty:
            self.sync()
            self.impact_dirty = False
        model = self.project_model()
        if self.map_edited:
            model["project_map"] = self.map_state()["state"]
        model["interactive_actions"] = bool(interactive)
        model["interactive_base"] = base
        if live:
            model["live"] = True
        try:
            model["usage"] = usage.report(self.repo, self.config)
            model["acts"] = usage.acts_report(self.repo)
        except Exception:
            model["usage"], model["acts"] = None, []
        pictures, _ = mind_figures.load(mind_figures.wanted(model), self.repo, None, fetch=False)
        return mind.render(model, pictures)


def completion_body(config, target, words, account, ident):
    moment = tasks.now()
    at, stamp = recortex.marker_time(moment, config), recortex.stamp(moment, config)
    return ("<!-- ghim:event type=checkpoint at=%s done=%d:%d eventkey=%s -->\n"
            "### Checkpoint: %s - one task step reported complete\n\n"
            "**In plain words, we paused here:** one task step is complete, as the user reported\n\n"
            "**State:** %s confirmed this in the local dashboard, %s:\n\n%s\n\n"
            "**Done since the last checkpoint:** %s. Completion is the user's report, not an independent check.\n\n"
            "**In progress:** Not stated.\n\n**Next actions:** Not stated.\n\n**Open questions:** Not stated.\n"
            % (at, target["comment"], target["ordinal"], ident, stamp, account, stamp,
               tasks.quoted(words), target["text"]))


def cookie_value(headers, name):
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(headers.get("Cookie", ""))
    except http.cookies.CookieError:
        return None
    found = jar.get(name)
    return found.value if found is not None and found.value.isascii() else None


def origin_ok(headers):
    """The Host header must be 127.0.0.1 or localhost with any port; an Origin, when there is one, must be that same host; and
    Sec-Fetch-Site, when there is one, must say same-origin or none. A foreign host name (a rebinding attack) is refused."""
    host = headers.get("Host")
    if not isinstance(host, str) or not HOST.fullmatch(host):
        return False
    if headers.get("Origin") not in (None, "http://" + host):
        return False
    return headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")


def authorized(method, path, headers, view, change):
    """The pure HTTP boundary for a request of a project (path relative to it) or of the server. "open" for what holds no
    record (the shell pages and their scripts, which read the key from the address and post it to /api/session), "view" for a
    request that carries the view cookie, "change" for one that carries the change cookie, None for a refusal. Only /snapshot and
    /api/ need a cookie. Every POST needs the change cookie, the exact Origin and the JSON content type: the view key opens no
    POST."""
    if not origin_ok(headers):
        return None
    if not (path.startswith("/api/") or path == "/snapshot"):
        return "open"
    level = None
    for name, key, rank in (("recortex_change", change, "change"), ("recortex_view", view, "view")):
        stored = cookie_value(headers, name)
        if stored is not None and hmac.compare_digest(stored, key):
            level = rank
            break
    if level is None:
        return None
    host = headers.get("Host")
    if method == "POST" and (level != "change" or headers.get("Origin") != "http://" + host
                             or headers.get("Content-Type") != "application/json"):
        return None
    return level


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # no capability, private content or URLs in access logs

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status, body, kind="application/json; charset=utf-8", policy=POLICY, cookies=()):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def read_json(self, expected):
        length = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not length.isdigit() or not 0 < int(length) <= MAX_BODY:
            raise ActionError("invalid_body", 400)
        raw = self.rfile.read(int(length))
        data = json.loads(raw) if len(raw) == int(length) else None
        if not isinstance(data, dict) or not expected <= set(data) <= expected | {"stop", "probe"}:
            raise ActionError("invalid_fields", 400)
        return data

    def hook(self):
        """A session's ping: valid only with the HMAC under the user key, a time within a minute and a nonce not used before. The
        answer is the pong that only this user's own server can make. It registers the project and keeps the server alive."""
        data = self.read_json({"root", "at", "nonce", "mac"})
        server = self.server
        root, at, nonce, tag = data["root"], data["at"], data["nonce"], data["mac"]
        now = time.time()
        server.nonces = {key: seen for key, seen in server.nonces.items() if now - seen < 120}
        if not (isinstance(root, str) and dashboard_server.ROOT_TEXT.fullmatch(root) and type(at) is int
                and abs(now - at) <= dashboard_server.PING_WINDOW and isinstance(nonce, str)
                and dashboard_server.HEX32.fullmatch(nonce) and nonce not in server.nonces and isinstance(tag, str)
                and hmac.compare_digest(tag, dashboard_server.hook_mac(root, at, nonce))):
            return self.respond(403, canonical({"code": "unauthorized"}))
        server.nonces[nonce] = now
        if data.get("probe") is True:   # --doctor: only the pong, nothing is registered and the day is not extended
            return self.respond(200, canonical({"pong": dashboard_server.pong_mac(nonce), "version": dashboard_server.version()}))
        server.last_ping = server.clock()
        if data.get("stop") is True:
            server.stop_requested = True
        elif project_ok(root):
            server.register(root)
        self.respond(200, canonical({"pong": dashboard_server.pong_mac(nonce), "version": dashboard_server.version()}))

    def session(self):
        """POST /api/session with Authorization: Bearer <key>: the view key sets the view cookie, the change key both. Both are
        HttpOnly, SameSite=Strict and for this server's address prefix only, with no expiry beyond the server's life."""
        self.read_json(set())
        host, bearer = self.headers.get("Host"), self.headers.get("Authorization", "")
        if self.headers.get("Origin") != "http://" + host or self.headers.get("Content-Type") != "application/json":
            return self.respond(403, canonical({"code": "unauthorized"}))
        server = self.server
        given = bearer[7:] if isinstance(bearer, str) and bearer.isascii() and bearer.startswith("Bearer ") else ""
        tail = "; HttpOnly; SameSite=Strict; Path=/%s/" % server.mount
        cookies = []
        if hmac.compare_digest(given, server.change):
            cookies = ["recortex_view=%s%s" % (server.view, tail), "recortex_change=%s%s" % (server.change, tail)]
        elif hmac.compare_digest(given, server.view):
            cookies = ["recortex_view=%s%s" % (server.view, tail)]
        if not cookies:
            return self.respond(403, canonical({"code": "unauthorized"}))
        self.respond(200, canonical({"connected": True, "change": len(cookies) == 2}), cookies=cookies)

    def dispatch(self, method):
        server = self.server
        # The unpredictable address prefix keeps the ambient cookies off unrelated applications on other loopback ports
        # (cookies have no port boundary): anything else is 404 with an empty body, also for another account that found the port.
        prefix = "/" + server.mount
        if not self.path.startswith(prefix + "/"):
            return self.respond(404, "")
        path = self.path[len(prefix):]
        try:
            if not origin_ok(self.headers):
                return self.respond(403, canonical({"code": "unauthorized"}))
            if method == "POST" and path == "/api/hook":
                return self.hook()
            if method == "POST" and path == "/api/session":
                return self.session()
            found = PROJECT_PATH.fullmatch(path)
            if found:
                project, path = found.group(1), found.group(2) or "/"
                if method == "POST" and path == "/api/session":
                    return self.session()
            else:
                project = None
            if method == "GET" and path.startswith("/impact-review?item=") and IMPACT_TARGET.fullmatch(path.partition("=")[2]):
                path = "/impact-review"
            if project is None:   # the server's own two answers need a key like a project's page does
                level = authorized(method, "/snapshot" if path == "/" else path, self.headers, server.view, server.change)
                if level is None:
                    return self.respond(403, canonical({"code": "unauthorized"}))
                if method == "GET" and path == "/api/projects":
                    return self.respond(200, canonical(server.listing()))
                if method == "GET" and path == "/":
                    return self.landing()
                return self.respond(404, canonical({"code": "not_found"}))
            level = authorized(method, path, self.headers, server.view, server.change)
            if level is None:
                return self.respond(403, canonical({"code": "unauthorized"}))
            info = server.projects.get(project)
            if info is None or not project_ok(info["root"]):
                server.projects.pop(project, None)
                server.actions.pop(project, None)
                return self.respond(404, canonical({"code": "unavailable"}))
            with dashboard_server.project(info["root"]):
                server.current = server.actions_of(project, info["root"])
                server.actions_now = server.current
                self.project_dispatch(method, path, level, project)
        except ActionError as error:
            self.respond(error.status, canonical({"code": error.code}))
        except (Exception, SystemExit):
            self.respond(503, canonical({"code": "unavailable"}))

    def landing(self):
        """The server's own page: the projects it knows, each by its repository name, checked on this request."""
        rows = self.server.listing()
        if len(rows) == 1:
            self.send_response(302)
            self.send_header("Location", "p/%s/snapshot" % rows[0]["id"])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        items = "".join('<li><a href="p/%s/snapshot">%s</a></li>' % (row["id"], row["repo"]) for row in rows)
        self.respond(200, '<!doctype html><html lang="en"><meta charset="utf-8"><title>Recortex projects</title><main><h1>Projects</h1>'
                          "<ul>%s</ul></main></html>" % items, "text/html; charset=utf-8")

    def project_dispatch(self, method, path, level, project):
        try:
            if method == "GET" and path in ("/", "/actions"):
                return self.respond(200, HTML, "text/html; charset=utf-8")
            if method == "GET" and path == "/snapshot":
                # The renderer supplies its original hash-based CSP in the HTML. The view-only page copies requests into the
                # chat; the page of the change key records. Add framing protection without overriding those inline hashes.
                return self.respond(200, self.server.snapshot_of(project, level), "text/html; charset=utf-8",
                                    policy="frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if method == "GET" and path == "/api/changed":
                return self.respond(200, canonical({"mark": self.server.mark_of(project)}))
            pages = {"/map-editor": "dashboard_map.html", "/suggestions": "dashboard_suggestions.html",
                     "/impact-review": "dashboard_impact.html"}
            if method == "GET" and path in pages:
                name = pages[path]
                with open(os.path.join(PAGE, name), encoding="utf-8") as handle:
                    return self.respond(200, handle.read(), "text/html; charset=utf-8")
            assets = {"/impact.js": ("dashboard_impact.js", "text/javascript; charset=utf-8"),
                      "/map-editor.js": ("dashboard_map.js", "text/javascript; charset=utf-8"),
                      "/suggestions.js": ("dashboard_suggestions.js", "text/javascript; charset=utf-8"),
                      "/action.js": ("dashboard_action.js", "text/javascript; charset=utf-8"),
                      "/stop.js": ("dashboard_stop.js", "text/javascript; charset=utf-8"),
                      "/action.css": ("dashboard_action.css", "text/css; charset=utf-8")}
            if method == "GET" and path in assets:
                name, kind = assets[path]
                with open(os.path.join(PAGE, name), encoding="utf-8") as handle:
                    return self.respond(200, handle.read(), kind)
            if method == "GET" and path == "/api/suggestions":
                return self.respond(200, canonical(self.server.actions_now.suggestions()))
            if method == "GET" and path == "/api/impact":
                return self.respond(200, canonical(decision_review_io.Service(self.server.actions_now).state()))
            if method == "GET" and path == "/api/map":
                return self.respond(200, canonical(self.server.actions_now.map_state()))
            if method == "GET" and path == "/api/state":
                return self.respond(200, canonical(self.server.actions_now.state()))
            if method != "POST" or path not in POSTS:
                raise ActionError("not_found", 404)
            length = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not length.isdigit() or not 0 < int(length) <= MAX_BODY:
                raise ActionError("invalid_body", 400)
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise ActionError("invalid_body", 400)
            data = json.loads(raw)
            expected = {"/api/preview": {"step", "words"}, "/api/commit": {"id", "body_hash"},
                        "/api/suggestions/feedback": {"id", "muted"},
                        "/api/impact/preview": {"group", "target", "revision", "previous", "verdict", "note", "draft"},
                        "/api/impact/resume": {"group", "target", "revision", "previous"},
                        "/api/impact/commit": {"id"}, "/api/impact/apply": {"id"},
                        "/api/map/preview": {"change", "revision"}, "/api/map/commit": {"id"},
                        "/api/reconcile": {"id"}, "/api/refresh": set(), "/api/stop": set()}[path]
            if not isinstance(data, dict) or set(data) != expected:
                raise ActionError("invalid_fields", 400)
            actions = self.server.actions_now
            if path == "/api/stop":
                # The loop in serve() ends after this answer is sent; same Host, Origin and token checks as any POST.
                self.server.stop_requested = True
                return self.respond(200, canonical({"stopped": True}))
            elif path == "/api/suggestions/feedback":
                if not isinstance(data["id"], str) or type(data["muted"]) is not bool:
                    raise ActionError("invalid_fields", 400)
                result = actions.suggestions(data["id"], data["muted"])
            elif path in ("/api/impact/preview", "/api/impact/resume"):
                result = decision_review_io.Service(actions).stage(data, resume=path.endswith("/resume"))
            elif path == "/api/impact/commit":
                result = decision_review_io.Service(actions).commit(data["id"])
            elif path == "/api/impact/apply":
                result = decision_review_io.Service(actions).apply(data["id"])
            elif path == "/api/map/preview":
                result = actions.map_preview(data["change"], data["revision"])
            elif path == "/api/map/commit":
                result = actions.map_commit(data["id"])
            elif path == "/api/preview":
                result = actions.preview(data["step"], data["words"])
            elif path == "/api/commit":
                result = actions.commit(data["id"], data["body_hash"])
            elif path == "/api/reconcile":
                result = actions.reconcile(data["id"])
            else:
                result = actions.refresh()
            self.server.counters[project] = self.server.counters.get(project, 0) + 1   # the mark of the live page changes
            self.respond(200, canonical(result))
        except decision_review_io.ReviewError as error:
            self.respond(409, canonical({"code": "review_refused", "message": str(error)}))
        except project_map.MapError as error:
            self.respond(409, canonical({"code": "map_refused", "message": str(error)}))
        except ActionError as error:
            if method == "GET" and path == "/snapshot" and error.code == "index_unavailable":
                # A first launch must retain a usable Refresh control.
                return self.respond(200, HTML, "text/html; charset=utf-8")
            self.respond(error.status, canonical({"code": error.code}))
        except (Exception, SystemExit):
            self.respond(503, canonical({"code": "unavailable"}))




class Server(http.server.HTTPServer):
    """The one server: keys, address prefix, the projects it knows and what it kept for each. Single-threaded on purpose, so that
    switching CLAUDE_PROJECT_DIR for one request is safe."""

    def __init__(self, address, handler, mount, view, change):
        super().__init__(address, handler)
        self.mount, self.view, self.change = mount, view, change
        self.projects, self.actions, self.counters, self.nonces = {}, {}, {}, {}
        self.pages = {}
        self.clock = time.time   # the wall clock: a laptop's sleep counts toward the day
        self.last_ping = self.clock()
        self.stop_requested = False
        self.actions_now = None

    def register(self, root):
        ident = dashboard_server.project_id(root)
        self.projects[ident] = {"root": os.path.realpath(root), "ping": time.monotonic()}
        for old in sorted(self.projects, key=lambda key: self.projects[key]["ping"])[:-PROJECTS_KEPT]:
            self.projects.pop(old)
            self.actions.pop(old, None)

    def actions_of(self, ident, root):
        """The Actions object of a project, made inside its folder's context; a kept one checks the project again first."""
        kept = self.actions.get(ident)
        if kept is not None:
            try:
                kept.authority()
                return kept
            except ActionError:   # the project's file or trust changed: look at it afresh, as a new one
                self.actions.pop(ident, None)
                self.pages.pop((ident, "view"), None)
                self.pages.pop((ident, "change"), None)
        made = Actions()
        self.actions[ident] = made
        return made

    def listing(self):
        """[{"id", "repo"}] of the projects that still pass every check, the repository names only."""
        rows = []
        for ident, info in list(self.projects.items()):
            if project_ok(info["root"]):
                with dashboard_server.project(info["root"]):
                    rows.append({"id": ident, "repo": recortex.load_config()["repo"].lower()})
            else:
                self.projects.pop(ident, None)
                self.actions.pop(ident, None)
        return rows

    def mark_of(self, ident):
        """A digest of the size and time of the few files that change when the record does, and of this server's own counter for
        the project: no file is opened."""
        repo = self.actions_now.repo
        files = [index.index_path(repo), index.index_path(repo) + "-wal"]
        observer = storage.folder(storage.state_top(), storage.repo_folder(repo), "observer", create=False, tighten=False)
        notes = storage.folder(storage.state_top(), storage.repo_folder(repo), create=False, tighten=False)
        files += [os.path.join(observer, "pending.sqlite"), os.path.join(observer, "pending.sqlite-wal")] if observer else []
        files += [os.path.join(notes, dashboard_notes.FILE)] if notes else []
        seen = [self.counters.get(ident, 0)]
        for path in files:
            try:
                info = os.stat(path)
                seen.append([info.st_size, info.st_mtime_ns])
            except OSError:
                seen.append([0, 0])
        return hashlib.sha256(canonical(seen).encode("utf-8")).hexdigest()[:16]

    def snapshot_of(self, ident, level):
        """The page of a project for a level: built once for each mark, and served again while the mark is the same. The view-only
        page copies requests into the chat; the change page records them."""
        def key():
            # the mark of the record, and who the user lets be told: an account that leaves the trusted list must not stay on a
            # page that was built while it was on it
            return self.mark_of(ident), tuple(sorted(recortex.allowed_users(self.actions_now.repo)))
        kept = self.pages.get((ident, level))
        if kept is not None and kept[0] == key():
            return kept[1]
        page = self.actions_now.snapshot("/%s/p/%s" % (self.mount, ident), interactive=level == "change", live=True)
        self.pages[(ident, level)] = (key(), page)
        return page


def project_ok(root):
    """The checks of a hook, for one request: the folder exists, its project file loads and names a repository in GitHub's form,
    and the repository is in the user's trusted list. Nothing about why is said."""
    try:
        with dashboard_server.project(root):
            config = recortex.load_config()
            return (os.path.isdir(root) and bool(config.get("_path")) and recortex.valid_repo(config.get("repo"))
                    and bool(recortex.allowed_users(config["repo"])))
    except (OSError, ValueError, SystemExit, storage.StorageError):
        return False


def prune_launch_files(folder):
    """A launch file is removed a minute after it was made."""
    now = time.time()
    try:
        for name in os.listdir(folder):
            if LAUNCH.fullmatch(name) and now - os.stat(os.path.join(folder, name), follow_symlinks=False).st_mtime > LAUNCH_SECONDS:
                storage.remove_file(folder, name)
    except (OSError, storage.StorageError):
        pass


def launch_folder(create=True):
    return storage.folder(storage.state_top(for_key=True), "dashboard-launch", create=create, tighten=create)


def serve_command(args):
    """The server itself. It takes this host's lock without waiting (another server then lives, and this one exits quietly),
    binds a port (47000 plus the user id modulo 1000, the 20 after it, then any free one), makes the address prefix and the two
    keys, writes the state and serves until 24 hours after the last ping, a valid stop or the Stop button."""
    try:
        descriptor = dashboard_server.take_lock()
    except (OSError, storage.StorageError):
        dashboard_server.note_failure("lock")
        return 1
    if descriptor is None:
        return 0
    server = None
    try:
        for port in ([args.port] if args.port is not None else dashboard_server.ports()):
            try:
                server = Server(("127.0.0.1", port), Handler, secrets.token_hex(16), secrets.token_hex(16), secrets.token_hex(16))
                break
            except OSError:
                continue
        if server is None:
            dashboard_server.note_failure("port")
            return 1
        try:
            dashboard_server.write_state({"form": dashboard_server.FORM, "pid": os.getpid(), "port": server.server_port,
                                          "mount": server.mount, "view": server.view, "change": server.change,
                                          "version": dashboard_server.version(), "started": int(time.time())})
        except (OSError, storage.StorageError):
            dashboard_server.note_failure("state")
            return 1
        dashboard_server.clear_failure()
        try:
            reason = serve(server)
        except KeyboardInterrupt:
            reason = "interrupted"
        print(STOPPED[reason])
        return 0
    finally:
        if server is not None:
            server.server_close()
            dashboard_server.remove_state()
        os.close(descriptor)


def serve(server, life=None, tick=None):
    """Handle requests until the user chooses Stop, a valid stop came, or 24 hours passed since the last session's ping. Returns
    "stopped" or "idle". A page request does not extend the life: only a session's ping does. One thread: a stop cannot race the
    loop."""
    life = dashboard_server.LIFE_SECONDS if life is None else life
    server.timeout = TICK_SECONDS if tick is None else tick
    while True:
        server.handle_request()  # waits up to server.timeout for one request, then returns
        if server.stop_requested:
            return "stopped"
        if server.clock() - server.last_ping >= life:
            return "idle"
        folder = launch_folder(create=False)   # looked for at each tick: the first `open` may make it after the server started
        if folder:
            prune_launch_files(folder)


def running_state(root):
    """The state of a server of ours that knows this project: one is started when none answers, and waited for up to 3 seconds."""
    first = dashboard_server.ensure(root, 1.0)
    if first == "off":
        return None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        state = dashboard_server.read_state()
        if state is not None and dashboard_server.ping(state, root, 0.5)[0] == "running":
            return state
        time.sleep(0.1)
    return None


def write_launch(folder, address):
    """The private launch file: a 0600 page that sends the browser to the address (the pattern Jupyter uses for its token). The
    server removes it after a minute. Returns the file's path."""
    prune_launch_files(folder)
    path = os.path.join(folder, "launch-%s.html" % secrets.token_hex(4))
    storage.write_file(path, '<!doctype html><html lang="en"><meta charset="utf-8">'
                             '<meta http-equiv="refresh" content="0;url=%s"><title>Recortex</title>'
                             '<p>Opening the live dashboard. If nothing happens, <a href="%s">open it here</a>.</p>'
                             '</html>\n' % (address, address))
    return path


def project_problem():
    """None when this folder is a configured and trusted project; else the one line that says what to do."""
    try:
        config = recortex.load_config()
        if not isinstance(config, dict) or not recortex.valid_repo(config.get("repo")):
            return "run from a configured project folder"
        if not recortex.allowed_users(config["repo"]):
            return "this project is not in your trusted repositories"
    except (OSError, ValueError, SystemExit):
        return "the project is unavailable"
    return None


def open_command(args):
    problem = project_problem()
    if problem:
        print("dashboard_action.py: " + problem, file=sys.stderr)
        return 1
    mode = dashboard_server.reach()
    if not dashboard_server.enabled():
        print(SWITCHED_OFF)
        return 3
    root = os.path.realpath(recortex.project_root())
    state = running_state(root)
    if state is None:
        print("The live dashboard could not be started. Run hooks.py --doctor.")
        return 1
    landing = "suggestions" if args.suggestions else "impact-review" if args.impacts else ""
    if args.impacts and args.impacts != "all":
        landing += "?item=" + args.impacts
    view = dashboard_server.link(state, root, state["view"])
    if mode == "local":
        address = dashboard_server.link(state, root, state["change"])
        if landing:
            address = address.replace("/#", "/" + landing + "#")
        opened = recortex.open_page(pathlib.Path(write_launch(launch_folder(), address)).as_uri())
        print("Opened the live dashboard in your browser." if opened else VIEW_LINE % view)
        if opened:
            print(VIEW_LINE % view)
        else:
            print(ABOVE)
    else:
        opened = False
        print(VIEW_LINE % view)
        print(ABOVE)
    if mode == "none" or (mode == "local" and not opened):
        # Where nothing shows that the browser reaches the port, the link comes first and Part B's one question after it.
        return offer_after_link()
    return 0


def offer_after_link():
    """The one question of Part B (update or set up the public page, or show the file's path) for a freshly built page, printed
    after the link. The page is built the way `mind.py` builds it; a build that fails prints its own fixed line, and the link
    above stands. Returns the exit code of `open`."""
    import subprocess
    import publish
    done = subprocess.run([sys.executable, os.path.join(HERE, "mind.py")], capture_output=True, text=True, timeout=240)
    if done.returncode != 0:
        for line in (done.stdout + done.stderr).splitlines()[:3]:
            print(line[:300])
        return 0
    config = recortex.load_config()
    repo = recortex.resolve_repo(None, config)
    print(MAYBE_NOT_REACHED)
    for line in publish.offer(config, repo, os.path.join(index.index_dir(repo.lower()), "mind.html")):
        print(line)
    return 0


def link_command(args):
    """The view-only link, to a session and to a terminal alike: the link with changes on is the hook's line and the browser that
    `open` opens, never this command's output."""
    problem = project_problem()
    if problem:
        print("dashboard_action.py: " + problem, file=sys.stderr)
        return 1
    root = os.path.realpath(recortex.project_root())
    state = dashboard_server.read_state()
    if state is None or dashboard_server.ping(state, root, 0.5)[0] != "running":
        state = running_state(root)
    if state is None:
        print("The live dashboard is not running here.")
        return 1
    print(VIEW_LINE % dashboard_server.link(state, root, state["view"]))
    return 0


def status_command(args):
    state = dashboard_server.read_state()
    if state is None or dashboard_server.ping(state, os.path.realpath(recortex.project_root()), 0.5)[0] == "stale":
        print("The live dashboard is not running on this machine; the next session starts it.")
        return 0
    print("The live dashboard is running on this machine, on port %d (version %s), since %s UTC."
          % (state["port"], state["version"] or "unknown", time.strftime("%Y-%m-%d %H:%M", time.gmtime(state["started"]))))
    return 0


def stop_command(args):
    state = dashboard_server.read_state()
    if state is None:
        print("The live dashboard is not running.")
        return 0
    answer, _ = dashboard_server.ping(state, os.path.realpath(recortex.project_root()), 1.0, stop=True)
    print("The live dashboard was stopped." if answer != "stale" else "The live dashboard is not running.")
    return 0


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("allow_abbrev", False)
            super().__init__(*args, **kwargs)
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    opened = sub.add_parser("open", help="open the live dashboard in the browser (a private launch file), or say where it is")
    group = opened.add_mutually_exclusive_group()
    group.add_argument("--suggestions", action="store_true", help="open the review of the observer's sources and reminders")
    group.add_argument("--impacts", nargs="?", const="all", metavar="ITEM", help="open the review of decision changes")
    sub.add_parser("link", help="print the link for this project: view only from a session")
    sub.add_parser("status", help="say whether the live dashboard is running")
    sub.add_parser("stop", help="stop the live dashboard")
    serving = sub.add_parser("serve", help="the server itself (the session-start hook starts it)")
    serving.add_argument("--port", type=int, default=None, help="a fixed port, 0 for any free one (default: 47000 plus the user id modulo 1000)")
    args = parser.parse_args(argv)
    if getattr(args, "impacts", None) and args.impacts != "all" and not IMPACT_TARGET.fullmatch(args.impacts):
        parser.error("the review item must be a decision group followed by its target identifier")
    if args.command == "serve" and args.port is not None and not 0 <= args.port <= 65535:
        parser.error("port must be from 0 through 65535")
    return {"open": open_command, "link": link_command, "status": status_command, "stop": stop_command,
            "serve": serve_command}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Explicit local dashboard actions for one configured project.

Run dashboard_action.py [--open] from the project folder. The capability URL is
private and lasts only while this process runs: it stops after 30 idle minutes, after 8 hours, or when the user
chooses Stop on one of its pages. The address and its token are printed only to a terminal; started from a session
(output that is not a terminal) the service writes a private launch file and prints only that file's path. The
exported mind page stays offline. The service previews and records task completions and project map edits, decision
reviews and reminder choices.
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
import recortex
import storage
import tasks

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "page")
IDENT = re.compile(r"[0-9a-f]{32}")
IMPACT_TARGET = re.compile(r"impact-[1-9][0-9]{0,18}-[0-9a-f]{64}")
STEP = re.compile(r"([1-9][0-9]{0,12}):([1-9][0-9]{0,15}):([1-9][0-9]?)")
MAX_BODY = 16384
PREVIEW_SECONDS = 600
IDLE_SECONDS = 30 * 60          # no authorized request for this long stops the service
LIFETIME_SECONDS = 8 * 60 * 60  # the service never runs longer than this, however busy
TICK_SECONDS = 1.0              # how often the serve loop looks at the clock when no request arrives
LAUNCH = re.compile(r"launch-[0-9a-f]{8}\.html")
STOPPED = {"stopped": "The private dashboard service was stopped.",
           "idle": "The private dashboard service stopped after 30 idle minutes.",
           "limit": "The private dashboard service stopped at its 8 hour limit.",
           "interrupted": "The private dashboard service was interrupted."}
NO_BROWSER = ("The interactive dashboard needs a browser on this machine, and it has none; it was not started.\n"
              "Its actions also work in the chat: /recortex:todo, /recortex:impact and /recortex:map.")
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
        state, login = recortex.gh_account()
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
            pages = recortex.gh(["api", "--paginate", "repos/%s/issues/%d/comments?per_page=100" % (self.repo, issue)])
            rows = recortex.parse_json_stream(pages)
            matches = [r for r in rows if isinstance(r, dict) and
                       "eventkey=" + operation["id"] in (r.get("body") or "")]
            if len(matches) != 1 or type(matches[0].get("id")) is not int:
                raise ActionError("receipt_unavailable")
            number = matches[0]["id"]
            row = json.loads(recortex.gh(["api", "repos/%s/issues/comments/%d" % (self.repo, number)]))
            expected_issue = "https://api.github.com/repos/%s/issues/%d" % (self.repo, issue)
            if (row.get("issue_url", "").lower() != expected_issue or
                    row.get("user", {}).get("login") != operation["account"] or
                    recortex.normalize(row.get("body", "")) != recortex.normalize(operation["body"]) or
                    row.get("created_at") != row.get("updated_at")):
                raise ActionError("receipt_mismatch")
            operation["state"] = "recorded"
            operation["receipt"] = {"comment": number,
                                    "url": "https://github.com/%s/issues/%d#issuecomment-%d" % (self.repo, issue, number)}
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
                                                url="https://github.com/%s/issues/%d#issuecomment-%d" %
                                                    (self.repo, ref["issue"], ref["comment"])))
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

    def snapshot(self, base=""):
        """Render authenticated project context; never change the standalone export."""
        import mind
        import mind_figures
        import usage
        if self.impact_dirty:
            self.sync()
            self.impact_dirty = False
        model = self.project_model()
        if self.map_edited:
            model["project_map"] = self.map_state()["state"]
        model["interactive_actions"] = True
        model["interactive_base"] = base
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


def authorized(method, path, headers, host, token):
    """Pure HTTP boundary; Host protects against DNS rebinding, Origin against cross-site requests."""
    if headers.get("Host") != host:
        return False
    origin = "http://" + host
    if headers.get("Origin") not in (None, origin):
        return False
    if headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
        return False
    if path.startswith("/api/") or path == "/snapshot":
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        stored = cookie.get("recortex_action")
        authorization = headers.get("Authorization", "")
        bearer = isinstance(authorization, str) and authorization.isascii() and hmac.compare_digest(authorization, "Bearer " + token)
        session = stored is not None and stored.value.isascii() and hmac.compare_digest(stored.value, token)
        if not (bearer or session):
            return False
        if method == "POST" and (headers.get("Origin") != origin or
                                 headers.get("Content-Type") != "application/json"):
            return False
    return True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # no capability, private content or URLs in access logs

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status, body, kind="application/json; charset=utf-8", policy=POLICY, session=False):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if session:
            self.send_header("Set-Cookie", "recortex_action=%s; HttpOnly; SameSite=Strict; Path=%s/; Max-Age=3600" %
                             (self.server.token, self.server.mount))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        # The unpredictable launch prefix keeps the ambient cookie off unrelated
        # applications on other loopback ports (cookies have no port boundary).
        if not self.path.startswith(self.server.mount + "/"):
            return self.respond(404, canonical({"code": "not_found"}))
        path = self.path[len(self.server.mount):]
        if method == "GET" and path.startswith("/impact-review?item=") and IMPACT_TARGET.fullmatch(path.partition("=")[2]):
            path = "/impact-review"
        if not authorized(method, path, self.headers, self.server.host, self.server.token):
            return self.respond(403, canonical({"code": "unauthorized"}))
        self.server.last_seen = time.monotonic()  # only an authorized request keeps the service alive
        try:
            if method == "GET" and path in ("/", "/actions"):
                return self.respond(200, HTML, "text/html; charset=utf-8")
            if method == "GET" and path == "/snapshot":
                # The renderer supplies its original hash-based CSP in the HTML.
                # Add framing protection without overriding those inline hashes.
                return self.respond(200, self.server.actions.snapshot(self.server.mount), "text/html; charset=utf-8",
                                    policy="frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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
                return self.respond(200, canonical(self.server.actions.suggestions()))
            if method == "GET" and path == "/api/impact":
                return self.respond(200, canonical(decision_review_io.Service(self.server.actions).state()))
            if method == "GET" and path == "/api/map":
                return self.respond(200, canonical(self.server.actions.map_state()))
            if method == "GET" and path == "/api/state":
                return self.respond(200, canonical(self.server.actions.state()))
            if method != "POST" or path not in ("/api/session", "/api/preview", "/api/commit", "/api/reconcile", "/api/refresh",
                                                 "/api/map/preview", "/api/map/commit", "/api/suggestions/feedback",
                                                 "/api/impact/preview", "/api/impact/commit",
                                                 "/api/impact/resume", "/api/impact/apply", "/api/stop"):
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
                        "/api/reconcile": {"id"}, "/api/refresh": set(), "/api/session": set(),
                        "/api/stop": set()}[path]
            if not isinstance(data, dict) or set(data) != expected:
                raise ActionError("invalid_fields", 400)
            actions = self.server.actions
            if path == "/api/session":
                actions.authority()
                return self.respond(200, canonical({"connected": True}), session=True)
            elif path == "/api/stop":
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="loopback port; default chooses a free port")
    parser.add_argument("--open", action="store_true", help="open the private local address in a browser")
    views = parser.add_mutually_exclusive_group()
    views.add_argument("--impacts", nargs="?", const="all", metavar="ITEM", help="open shared decision reviews or a specific item")
    views.add_argument("--suggestions", action="store_true", help="open the observer suggestions for this folder")
    args = parser.parse_args(argv)
    if args.impacts and args.impacts != "all" and not IMPACT_TARGET.fullmatch(args.impacts):
        parser.error("the review item must be a decision group followed by its target identifier")
    if not 0 <= args.port <= 65535:
        parser.error("port must be from 0 through 65535")
    try:
        actions = Actions()
        server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    except ActionError as error:
        message = {"configured_project_required": "run from a configured project folder",
                   "trust_required": "this project is not in your trusted repositories",
                   "project_changed": "the project changed; start again from its configured folder"}.get(
                       error.code, "the project is unavailable")
        print("dashboard actions: " + message + ".", file=sys.stderr)
        return 1
    except (OSError, ValueError, storage.StorageError, SystemExit):
        print("dashboard actions: the project, private storage, or loopback port is unavailable.", file=sys.stderr)
        return 1
    server.actions, server.token = actions, secrets.token_urlsafe(32)
    server.host = "127.0.0.1:%d" % server.server_port
    server.mount = "/" + secrets.token_hex(16)
    server.started = server.last_seen = time.monotonic()
    server.stop_requested = False
    landing = "suggestions" if args.suggestions else "impact-review" if args.impacts else ""
    if args.impacts and args.impacts != "all":
        landing += "?item=" + args.impacts
    address = "http://%s%s/%s#%s" % (server.host, server.mount, landing, server.token)
    # The address holds the token, which is the whole capability: it is printed only to a terminal, where the user
    # reads it. Whatever else reads this output (a session that started the service in the background) gets the path
    # of a private file instead. A browser is pointed at that file, never at the address, so the token is on no
    # command line either.
    terminal = bool(getattr(sys.stdout, "isatty", lambda: False)())
    launch = ""
    if args.open or not terminal:
        try:
            launch = write_launch(actions.folder, server.mount, address)
        except (OSError, storage.StorageError):
            print("dashboard actions: the private launch file could not be written.", file=sys.stderr)
            if not terminal:
                server.server_close()
                return 1
    if terminal:
        print("Private dashboard actions for %s. It stops after 30 idle minutes, or when you choose Stop on one of "
              "its pages (Ctrl-C also stops it here). Do not share this address." % actions.repo)
        print(address, flush=True)
        if args.open and launch:
            recortex.open_page(pathlib.Path(launch).as_uri())
    else:
        opened = recortex.open_page(pathlib.Path(launch).as_uri()) if args.open else False
        if args.open and not opened:   # nobody can reach this machine's 127.0.0.1: no service is started, no file is left
            server.server_close()
            try:
                storage.remove_file(actions.folder, os.path.basename(launch))
            except (OSError, storage.StorageError):
                pass
            print(NO_BROWSER)
            return 0
        print("Private dashboard actions for %s are running. The private address is not printed here." % actions.repo)
        print("Opened the private dashboard in your browser." if opened else
              "To open the private dashboard, open this file in your browser: %s" % launch, flush=True)
        if opened:
            print("To open it again, open this file in your browser: %s" % launch, flush=True)
        print("The service stops after 30 idle minutes, or when you choose Stop on one of its pages.", flush=True)
    try:
        reason = serve(server)
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        server.server_close()
        if launch:
            try:
                storage.remove_file(actions.folder, os.path.basename(launch))
            except (OSError, storage.StorageError):
                pass
    print(STOPPED[reason])
    return 0


def serve(server, idle=None, limit=None, tick=None):
    """Handle requests until the user chooses Stop, no authorized request has come for the idle cutoff, or the
    lifetime limit is reached. Returns "stopped", "idle" or "limit". One thread: a stop cannot race the loop."""
    idle = IDLE_SECONDS if idle is None else idle
    limit = LIFETIME_SECONDS if limit is None else limit
    server.timeout = TICK_SECONDS if tick is None else tick
    while True:
        server.handle_request()  # waits up to server.timeout for one request, then returns
        if server.stop_requested:
            return "stopped"
        now = time.monotonic()
        if now - server.started >= limit:
            return "limit"
        if now - server.last_seen >= idle:
            return "idle"


def write_launch(folder, mount, address):
    """The private launch file: a 0600 page that sends the browser to the address (the pattern Jupyter uses for its
    token). Older files of earlier launches that were cut off are removed first. Returns the file's path."""
    now = time.time()
    for name in os.listdir(folder):
        if LAUNCH.fullmatch(name) and now - os.stat(os.path.join(folder, name), follow_symlinks=False).st_mtime > LIFETIME_SECONDS + 60:
            storage.remove_file(folder, name)
    name = "launch-%s.html" % mount[1:9]
    path = os.path.join(folder, name)
    storage.write_file(path, '<!doctype html><html lang="en"><meta charset="utf-8">'
                             '<meta http-equiv="refresh" content="0;url=%s"><title>Recortex</title>'
                             '<p>Opening the private dashboard. If nothing happens, <a href="%s">open it here</a>.</p>'
                             '</html>\n' % (address, address))
    return path


if __name__ == "__main__":
    raise SystemExit(main())

"""Preview a human verdict, append it to the hub, and separately verify an approved target edit."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time

import dashboard_notes
import decision_review as ledger
import index
import observer_publisher
import observer_worker
import post_verified
import project_map
import project_map_io as map_io
import recortex
import storage


class ReviewError(ValueError):
    pass


def normalized_digest(body):
    return hashlib.sha256(recortex.normalize(body).encode("utf-8")).hexdigest()


def head(history, event):
    return next((r for r in reversed(history) if all(r[k] == event[k] for k in ("group", "target", "revision"))), None)


def plain_paragraph(text):
    # Keep edits within the selected paragraph. Structured Markdown changes use a cited follow-up.
    return bool(text) and all(line.strip() and not line.startswith(("    ", "\t"))
                              and not re.match(r"\s*(?:[#>|=~`]|[-*+]\s|[0-9]+[.)]\s|[-*_]{3,}|\[[^]]+\]:)", line)
                              for line in text.split("\n"))


class Service:
    def __init__(self, actions):
        self.actions = actions
        self.repo = actions.repo
        self.hub = actions.config.get("hub")
        if type(self.hub) is not int or self.hub <= 0:
            raise ReviewError("A configured project hub is required.")
        self.folder = storage.folder(storage.state_top(), storage.repo_folder(self.repo), "impact-reviews",
                                     project_map.digest([actions.root, self.hub])[:32])

    def history(self):
        rows = map_io.verified_rows(self.repo, self.hub, ledger.MARKER)
        return ledger.replay(self.repo, self.hub, self.actions.authority(), rows)

    def state(self):
        self.actions.account()
        self.actions.sync()
        model = self.actions.project_model()["decision_impacts"]
        return dict(model, repo=self.repo)

    def check_target(self, fields):
        if (not ledger.matches(ledger.GROUP, fields["group"]) or not ledger.matches(ledger.HASH, fields["target"])
                or not ledger.matches(ledger.HASH, fields["revision"]) or type(fields["previous"]) is not int
                or fields["previous"] < 0):
            raise ReviewError("Invalid review target.")

    def target(self, fields):
        for group in self.state()["groups"]:
            if group["id"] == fields["group"]:
                for item in group["items"]:
                    if item["id"] == fields["target"] and item["revision"] == fields["revision"]:
                        return group, item
        raise ReviewError("The decision or target changed. Refresh and review the current evidence.")

    def path(self, token, suffix=".json"):
        if not ledger.matches(ledger.IDENT, token):
            raise ReviewError("Invalid preview identifier.")
        return os.path.join(self.folder, token + suffix)

    def save(self, op):
        map_io.save(self.path(op["id"]), op)

    def load(self, token):
        raw = storage.read_file(self.path(token))
        signed = json.loads(raw) if raw else {}
        op = signed.get("data", {})
        who = self.actions.account().lower()
        if (not hmac.compare_digest(map_io.signature(op), signed.get("signature", "")) or
                op.get("id") != token or op.get("repo") != self.repo or op.get("root") != self.actions.root
                or op.get("hub") != self.hub or op.get("account") != who):
            raise ReviewError("Reopen this review using the current project and account.")
        return op

    def check_text(self, event):
        for value in (event["note"], event["draft"]):
            if (observer_worker.privacy_problem(value, self.actions.config) or
                    observer_publisher.outbound_problem(value, self.repo)):
                raise ReviewError("The note or draft was refused by the project's publication checks.")

    def context(self, group, item):
        # Historical receipts retain only checked local citations and a display label safe to publish.
        def citation(url):
            found = self.actions.records.parse_address(url)
            if found is None:
                raise ReviewError("The cited record could not be verified for this project.")
            return {"issue": found[0], "comment": found[1]}
        label = item["label"][:200]
        if (not ledger.words(label, 200) or observer_worker.privacy_problem(label, self.actions.config)
                or observer_publisher.outbound_problem(label, self.repo)):
            label = item["kind"].capitalize()
        return dict(citation(item["url"]), label=label, kind=item["kind"],
                    decisions=[citation(s["url"]) for s in [group["old"]] + group["successors"]])

    def protected(self, item, labels):
        import decision_impact
        config = self.actions.config
        plan = None
        if recortex.plan_profile(recortex.shipped_profiles(config.get("profiles"))):
            db = index.connect(self.repo, create=False)
            if db is None:
                return True
            try:
                plan = index.planned(db, config, self.actions.authority())
            finally:
                db.close()
        return decision_impact.protected_section(config, item["issue"], item["heading"], labels, plan)

    def plan(self, group, item, event, op_id):
        """Recompute native edits from trusted live records. A request is always available for other workflows."""
        draft = event["draft"]
        detail = {"before": "", "after": "", "request": ""}
        if event["verdict"] != "needs_update":
            return "review", detail, {}
        refs = [group["old"]["url"]] + [s["url"] for s in group["successors"]]
        steps = {"branch": "Review the proposed branch and preview its evidence update in Project Map.",
                 "task": "Compare the task goal with the decision and preview any change to the task with its owner.",
                 "step": "Compare the agreed step with the decision and preview a replacement task plan.",
                 "section": "Show the revised passage in context and follow the output's approval process.",
                 "source": "Check what the cited source supports, then show the exact revised statement and citation.",
                 "record": "Draft a cited follow-up record and show how it changes the earlier conclusion."}
        detail["request"] = "%s\n%s\nTarget: %s\nDecisions: %s" % (
            draft, steps[item["kind"]], item["url"], " ".join(refs))
        if group["conflict"] or len(group["successors"]) != 1 or item["relation"] != "direct":
            return "followup", detail, {}
        if item["kind"] == "branch" and draft == item["draft"]:
            state = map_io.current(self.repo, self.hub)
            node = state["nodes"].get(item["branch"])
            old = "comment:%s" % group["old"]["id"]
            new = "comment:%s" % group["successors"][0]["id"]
            if node and not node["deleted"] and old in node["evidence"]:
                evidence = list(dict.fromkeys(new if ref == old else ref for ref in node["evidence"]))
                change = {"action": "update", "id": item["branch"], "evidence": evidence}
                # Keep only the target's expected state in the cross-device comparison. Unrelated map edits may proceed.
                prepared = project_map.prepare(state, change, op_id=op_id)
                detail.update(before="\n".join(node["evidence"]), after="\n".join(evidence))
                return "map", detail, {"change": change, "expected": prepared["expected"]}
        elif (item["kind"] == "section" and item.get("edit_before") and item.get("edit_text")
              and plain_paragraph(draft)):
            issue = recortex.read_issue(self.repo, item["issue"])
            if self.protected(item, issue["labels"]):
                return "followup", detail, {}
            body, before = issue["body"], item["edit_before"]
            profiles = recortex.shipped_profiles(self.actions.config.get("profiles"))
            labels = recortex.plan_section_labels(issue["labels"], body, profiles)
            if normalized_digest(body) != item["body_digest"]:
                raise ReviewError("The output body changed. Refresh the target before drafting an edit.")
            raw_before = before if before in body else before.replace("\n", "\r\n")
            if (body.count(raw_before) == 1 and before != draft and refs[0] in before
                    and not any(labels[k] for k in ("add", "remove", "stop", "foreign", "problems"))):
                raw_draft = draft.replace("\n", "\r\n") if "\r\n" in raw_before else draft
                after = body.replace(raw_before, raw_draft, 1)
                detail.update(before=before, after=draft)
                return "body", detail, {"issue": item["issue"], "base": body, "after": after}
        return "followup", detail, {}

    def stage(self, data, resume=False):
        with self.actions.lock():
            who = self.actions.account().lower()
            self.check_target(data)
            prior = head(self.history(), data) or {}
            if prior.get("comment", 0) != data["previous"]:
                raise ReviewError("Another review was recorded. Refresh before deciding.")
            if prior.get("verdict") == "applying":
                if not resume or prior["reviewer"].lower() != who:
                    raise ReviewError("A target change is awaiting verification. Check the pending change.")
                reference = storage.read_file(self.path(prior["id"], ".claim"))
                if reference:
                    op = self.load(reference)
                    if op.get("claim") == prior["id"]:
                        return self.output(op, prior)
                raise ReviewError("Open this pending change on the device that started the target edit to verify it.")
            group, item = self.target(data)
            token = secrets.token_hex(16)
            if resume:
                if prior.get("verdict") != "needs_update" or prior["action"] not in ledger.NATIVE:
                    raise ReviewError("This review has no pending native edit.")
                event = {k: v for k, v in prior.items() if k not in ("reviewer", "at", "comment")}
            else:
                if (data["verdict"] not in ledger.VERDICTS or not ledger.words(data["draft"], 4000)
                        or not ledger.words(data["note"], 1500)):
                    raise ReviewError("Enter a supported verdict, note, and draft.")
                if data["verdict"] == "needs_update" and group["conflict"]:
                    raise ReviewError("Resolve the conflicting replacement decisions before drafting an update.")
                event = dict(data, id=token, action="review", receipt=None, context=self.context(group, item))
                if data["verdict"] == "needs_update":
                    event["draft"] = data["draft"].strip() or item.get("edit_text") or item["draft"]
                else:
                    event["draft"] = ""
            action, detail, native = self.plan(group, item, event, token)
            if resume and action != event["action"]:
                raise ReviewError("The target changed. Preview a new review.")
            event["action"] = action
            if not ledger.valid(event):
                raise ReviewError("The review text is incomplete or too long.")
            self.check_text(event)
            try:
                ledger.encode(self.repo, self.hub, event)
            except ValueError as error:
                raise ReviewError(str(error)) from error
            op = {"id": token, "root": self.actions.root, "repo": self.repo, "hub": self.hub, "account": who,
                  "created": time.time(), "session": self.actions.session, "event": event, "native": native,
                  "detail": detail, "sent": [], "accepted": prior["comment"] if resume else None}
            if action == "body":
                self.body_files(op)
                if not self.body_command(op, dry=True):
                    if resume:
                        raise ReviewError("The checked output editor cannot apply the accepted draft. Review it in chat.")
                    event["action"], op["native"] = "followup", {}
            self.save(op)
            if resume:
                return self.output(op, prior)
            return dict(id=token, summary="Record %s for %s." % (event["verdict"].replace("_", " "), item["label"]),
                        draft=event["draft"], action=event["action"], detail=detail)

    def append(self, op, event):
        if self.actions.account().lower() != op["account"]:
            raise ReviewError("The publishing account changed.")
        history = self.history()
        found = next((r for r in history if r["id"] == event["id"]), None)
        if found:
            if found["reviewer"].lower() != op["account"] or any(found[k] != value for k, value in event.items()):
                raise ReviewError("This operation belongs to a different review.")
            self.actions.impact_dirty = True
            return found
        if (head(history, event) or {}).get("comment", 0) != event["previous"]:
            raise ReviewError("Another review won the race. Refresh the target.")
        self.check_text(event)
        command = ["issue", "comment", str(self.hub), "--repo", self.repo, "--body-file", "-"]   # what a hold is matched against
        map_io.holds(self.repo, self.hub, command)
        if event["id"] in op["sent"]:
            raise ReviewError("The earlier send is unconfirmed. Reconcile the original preview.")
        body = ledger.encode(self.repo, self.hub, event)
        if self.actions.account().lower() != op["account"]:
            raise ReviewError("The publishing account changed.")
        op["sent"].append(event["id"])
        self.save(op)
        try:
            self.actions.records.post_entry(self.hub, body=body)
        except recortex.GhError:
            pass
        self.actions.impact_dirty = True
        for found in self.history():
            if (found["id"] == event["id"] and found["reviewer"].lower() == op["account"]
                    and all(found[k] == v for k, v in event.items())):
                return found
        raise ReviewError("The append was not verified. Reconcile the original preview and check for conflicts.")

    def fresh(self, op):
        if op["session"] != self.actions.session or time.time() - op["created"] > 600:
            raise ReviewError("The preview expired. Open a fresh preview.")

    def same_target(self, op):
        event = op["event"]
        group, item = self.target(event)
        action, _, native = self.plan(group, item, event, op["id"])
        if action != event["action"] or native != op["native"]:
            # The exact protected/manual request can always be recorded when a native dry-run refused it.
            if event["action"] != "followup" or op["native"] or action != "body":
                raise ReviewError("The target or proposed edit changed. Preview again.")

    def output(self, op, review):
        return {"id": op["id"], "review": review, "applied": review["verdict"] == "applied",
                "action": review["action"], "detail": op["detail"], "receipt": review.get("receipt")}

    def commit(self, token):
        with self.actions.lock():
            op = self.load(token)
            event = op["event"]
            if not op["sent"] and not op["accepted"]:
                self.fresh(op)
                self.same_target(op)
            history = self.history()
            if op["accepted"]:
                found = head(history, event)
                if not found or found["comment"] != op["accepted"]:
                    raise ReviewError("A newer review is already recorded. Refresh the target.")
            else:
                found = self.append(op, event)
                op["accepted"] = found["comment"]
                self.save(op)
                dashboard_notes.add(self.repo, "verdict", self.hub)   # told to a session once, on its next prompt
            return self.output(op, found)

    def body_files(self, op):
        storage.write_file(self.path(op["id"], ".base.md"), op["native"]["base"])
        storage.write_file(self.path(op["id"], ".after.md"), op["native"]["after"])

    def body_command(self, op, dry, guard=None):
        from dashboard_action import quiet
        native, token = op["native"], op["id"]
        args = ["body", "--repo", self.repo, "--issue", str(native["issue"]), "--kind", "final-record",
                "--expected-body-file", self.path(token, ".base.md"), "--body-file", self.path(token, ".after.md"),
                "--backup", self.path(token, ".backup.md")]
        if dry:
            args.append("--dry-run")
        try:
            return quiet(post_verified.main, args, body_guard=guard or self.actions.authority) == 0
        except SystemExit:
            return False

    def apply(self, token):
        with self.actions.lock():
            op = self.load(token)
            event = op["event"]
            if not op["accepted"] or event["verdict"] != "needs_update" or event["action"] not in ledger.NATIVE:
                raise ReviewError("Save a supported target edit before applying it.")
            current = head(self.history(), event) or {}
            if op.get("done") and current.get("id") == op["done"]:
                return self.output(op, current)
            if not op.get("claim"):
                self.fresh(op)
                if current.get("comment") != op["accepted"]:
                    raise ReviewError("A newer review is already recorded. Refresh.")
                self.same_target(op)
                native_issue = op["native"].get("issue", self.hub)
                if event["action"] == "body":
                    command = ["issue", "edit", str(native_issue), "--repo", self.repo,
                               "--body-file", self.path(token, ".after.md")]
                    map_io.holds(self.repo, native_issue, command)
                op.update(claim=secrets.token_hex(16), done=secrets.token_hex(16))
                self.save(op)
                storage.write_file(self.path(op["claim"], ".claim"), op["id"])
            claim = dict(event, id=op["claim"], previous=op["accepted"], verdict="applying")
            held = self.append(op, claim)
            def guard():
                latest = head(self.history(), event) or {}
                if self.actions.account().lower() != op["account"] or latest.get("id") != held["id"]:
                    raise ReviewError("The account or active review changed.")
            guard()
            native = op["native"]
            if event["action"] == "map":
                operation = op.get("map_operation")
                if operation is None:
                    operation = map_io.preview(self.repo, self.hub, native["change"], op_id=op["claim"])["operation"]
                    if operation["expected"] != native["expected"]:
                        raise ReviewError("The map branch changed after preview.")
                    op["map_operation"] = operation
                    self.save(op)
                def map_guard():
                    self.same_target(op)
                    guard()
                result = map_io.commit(self.repo, self.hub, operation, guard=map_guard)
                self.actions.map_edited = True
                receipt = {"issue": self.hub, "comment": result["comment"]}
            else:
                issue = native["issue"]
                before, after = normalized_digest(native["base"]), normalized_digest(native["after"])
                live = normalized_digest(recortex.read_issue_body(self.repo, issue))
                if not op.get("body_sending"):
                    if live != before:
                        raise ReviewError("The output body changed. No edit was sent.")
                    self.same_target(op)
                    self.body_files(op)
                    command = ["issue", "edit", str(issue), "--repo", self.repo, "--body-file", self.path(token, ".after.md")]
                    map_io.holds(self.repo, issue, command)
                    guard()
                    def body_guard():
                        guard()
                        map_io.holds(self.repo, issue, command)
                        op["body_sending"] = True
                        self.save(op)
                    try:
                        self.body_command(op, dry=False, guard=body_guard)
                    except recortex.GhError:
                        pass
                    live = normalized_digest(recortex.read_issue_body(self.repo, issue))
                if not op.get("body_sending"):
                    raise ReviewError("The output edit was refused before sending. Check the live target before retrying.")
                if live != after:
                    raise ReviewError("The target edit is unconfirmed. Check the live body before further editing.")
                receipt = {"issue": issue, "sha256": after}
            done = dict(event, id=op["done"], previous=held["comment"], verdict="applied", receipt=receipt)
            verified = self.append(op, done)
            dashboard_notes.add(self.repo, "edit", receipt["issue"])
            return self.output(op, verified)

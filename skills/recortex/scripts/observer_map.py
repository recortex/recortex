"""Reviewed Project Map maintenance. The private queue keeps source pointers and reviewed operations."""
import datetime
import json
import re
import time

import index
import mind_model
import observer_context
import observer_holds
import observer_publisher
import record_store
import observer_runtime as runtime
import observer_sources
import observer_store
import project_map
import project_map_io
import recortex

BATCH = 8
QUEUE_LIMIT = 64
ACTIONS = ("none", "add", "rename", "move", "merge", "split", "delete", "restore")
TEXT = {"type": "string", "maxLength": 120}
NODE = {"type": "string", "maxLength": 64}


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def array(items, cap):
    return {"type": "array", "items": items, "maxItems": cap}


DRAFT = obj({"action": dict(TEXT, enum=list(ACTIONS)), "node": NODE, "target": NODE, "title": TEXT,
             "branches": array(obj({"title": TEXT, "children": array(NODE, 12)}), 6),
             "sources": array({"type": "integer", "minimum": 1, "maximum": BATCH}, BATCH),
             "memories": array({"type": "integer", "minimum": 1}, 6)})
REVIEW = obj({"candidate_hash": {"type": "string", "pattern": "[0-9a-f]{64}"},
              "judgment": dict(TEXT, enum=["supported", "uncertain", "reject"])})
RENAME = re.compile(r"\b(?:rename[ds]?|renaming|now called|new name|formerly called)\b", re.I)


def control(name):
    return "map_" + name + "_" + runtime.candidate_hash(runtime.folder_key())[:16]


def rows(store):
    return store.get_control(control("queue"), [])


def review_key(repo, item):
    """Bind the dialog grant and publication to every part of the displayed, reviewed operation."""
    fields = ("hub", "operation", "change", "sources", "proof", "judgment", "routine", "line", "affected", "generation", "cited")
    return runtime.candidate_hash([repo.lower()] + [item[k] for k in fields])[:32]


def rejection_key(row):
    change = dict(row["change"])
    if change["action"] == "add":
        change.pop("id", None)
    if change["action"] == "split":
        change["branches"] = [{k: v for k, v in b.items() if k != "id"} for b in change["branches"]]
    return "map_drop_" + runtime.candidate_hash([runtime.folder_key(), row["hub"], change, row["proof"]])[:48]


def save(store, proposal):
    records = [row for row in rows(store) if row["key"] != proposal["key"]] + [proposal]
    active = [row for row in records if row["state"] in ("ready", "review")]
    closed = [row for row in records if row["state"] not in ("ready", "review")]
    store.set_control(control("queue"), (closed[-QUEUE_LIMIT:] + active))


def listing(store):
    """Local display includes the exact proposed change, its references and the independently reviewed disposition."""
    visible = []
    for row in rows(store):
        if held(store, row["sources"], row["hub"], publication=False):
            continue
        fields = ("key", "line", "state", "judgment", "routine", "change", "created", "support", "cited", "affected",
                  "last_result")
        visible.append({key: row[key] for key in fields if key in row})
    return visible


def waiting(store):
    options = runtime.settings(store.config)
    if options["mode"] != "automatic" or options["map_mode"] != "automatic" or options["level"] == "notes":
        return []
    return [{"key": row["key"], "short": row["key"][:8], "kind": "map", "created": row["created"], "line": row["line"]}
            for row in rows(store) if row["state"] == "review" and not store.put_off_until(row["key"][:8]) > time.time()
            and not held(store, row["sources"], row["hub"])]


def held(store, ids, hub, publication=True):
    """Fail closed if a pointer is gone, its session is withheld, or an explicit command hold could cover the write."""
    holds = store.holds()
    sessions = set()
    for sid in ids:
        item = store.observation(sid)
        if not item or item["reason"] in ("withheld", "withheld_review", "privacy", "privacy_context"):
            return True
        sessions.add(item["metadata"].get("session_id"))
        linked = store.db.execute("SELECT eventkey FROM event_sources WHERE source_id=?", (sid,)).fetchone()
        if linked:
            event = store.event(linked[0])
            if event and (event["held"] or observer_publisher.withheld_review(event)):
                return True
    if not sessions or None in sessions or observer_holds.publication_withheld(holds, store.repo, sessions):
        return True
    if not publication:
        return False
    command = recortex.show_command(["issue", "comment", str(hub), "--repo", store.repo, "--body-file", "-"])
    for session in sessions:
        answer = observer_holds.check_action(holds, store.repo, "Bash", {"command": command}, session_id=session, issue=hub)
        if answer["decision"] == "deny":
            return True
    return False


def material(store, repo, config, root):
    """One ordered, bounded stretch. A retry re-resolves the original descriptors under the current folder's boundary."""
    import observer_worker
    since, last = store.get_control(control("cursor"), [time.time() - 86400, ""])
    batch = store.db.execute("SELECT source_id,created FROM observations WHERE created>? OR "
                             "(created=? AND source_id>?) ORDER BY created,source_id LIMIT ?",
                             (since, since, last, BATCH)).fetchall()
    if not batch:
        return [], None
    sources = []
    cursor = None
    total = 0
    source_room = max(0, (runtime.input_cap(runtime.settings(config), "middle") - 3000) // 2)
    for sid, created in batch:
        previous, cursor = cursor, [created, sid]
        item = store.observation(sid)
        if held(store, [sid], config.get("hub"), publication=False) or item["metadata"].get("role") not in ("assistant", "user"):
            continue
        try:
            source = observer_sources.resolve(item["metadata"], root)
        except (observer_sources.SourceError, OSError, ValueError):
            continue
        if (observer_worker._said(source) and not source.get("subagent") and not source.get("sidechain")
                and not observer_holds.withheld(source["text"]) and not observer_worker.source_privacy_problem(source, config)):
            size = len(runtime.canonical(source["text"])) + 180
            if size > source_room:
                continue  # keep a source whole, including any qualification that appears at its end
            if total + size > source_room:
                return sources, previous  # this message heads the next batch
            sources.append(source)
            total += size
    return sources, cursor


def record(repo, hub, query, requested=None):
    allowed = recortex.allowed_users(repo)
    if not allowed or type(hub) is not int:
        return None
    db = index.connect(repo, create=False)
    if db is None:
        return None
    try:
        db.execute("BEGIN")
        if not index.mind_ready(db) or index.get_meta(db, "sync_complete") != "1" or index.get_meta(db, "hub") != str(hub):
            return None
        stamp = index.get_meta(db, "synced_at") or ""
        if not index.ISO_UTC.fullmatch(stamp):
            return None
        instant = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        if not 0 <= time.time() - instant.timestamp() <= 86400:
            return None
        state = project_map.read_map(db, hub, allowed)
        project = mind_model.Build(db, repo, {user.lower() for user in allowed}, None)
        project.read_issues()
        found = [m for m in index.memories(db, allowed)["items"].values() if m["state"] in ("stands", "open", "untried")
                 and not m["reconstructed"] and m["issue"] in project.issues and not project.issues[m["issue"]]["pull"]
                 and project.title_shown(m["issue"]) and project.body_shown(m["issue"])
                 and (requested is None or m["id"] in requested)]
        wanted = observer_context.task_terms(query)
        found.sort(key=lambda m: (len(wanted & observer_context.task_terms(m["plain"] or m["title"])),
                                  m["time"], m["id"]), reverse=True)
        memories = {m["id"]: {"comment": m["id"], "issue": m["issue"], "kind": m["kind"],
                              "text": (m["plain"] or m["title"])} for m in found[:18] if len(m["plain"] or m["title"]) <= 750}
        for cid, memory in memories.items():
            parent = project.issues[memory["issue"]]
            comment = db.execute("SELECT issue,author,body,created_at,updated_at FROM comments WHERE id=?", (cid,)).fetchone()
            if comment is None:
                return None
            memory["comment_hash"] = runtime.candidate_hash(list(comment))
            memory["parent_hash"] = runtime.candidate_hash([parent["author"], parent["title"], parent["state"], parent["body"]])
        return state, memories
    finally:
        db.close()


def ordinal_sources(candidate, sources, memories):
    numbers = candidate["sources"]
    refs = candidate["memories"]
    if (not numbers or len(set(numbers)) != len(numbers) or any(n > len(sources) for n in numbers)
            or len(set(refs)) != len(refs) or any(key not in memories for key in refs)):
        return []
    return [sources[n - 1] for n in numbers]


def change_for(candidate):
    action = candidate["action"]
    node, target, title = (candidate[k] for k in ("node", "target", "title"))
    refs = ["comment:%d" % cid for cid in sorted(candidate["memories"])]
    if action == "add":
        return {"action": action, "title": title, "parent": target or None, "evidence": refs}
    change = {"action": action, "id": node}
    if action == "rename":
        change.update(title=title, evidence=refs)
    elif action == "move":
        change["parent"] = target or None
    elif action == "merge":
        change["into"] = target
    elif action == "split":
        change["branches"] = candidate["branches"]
    elif action == "delete":
        change["children"] = "keep"
    return change


def routine(change, sources, memories, state):
    """A fresh reviewer, typed evidence, and matching standing project evidence are all needed for unattended writes."""
    if change["action"] not in ("add", "rename") or not memories:
        return False
    title = " ".join(change["title"].casefold().split())
    typed = [" ".join(s["text"].casefold().split()) for s in sources if s.get("direct_user") is True]
    if not title:
        return False
    named = re.compile(r"(?<!\w)" + re.escape(title) + r"(?!\w)")
    if not any(named.search(text) for text in typed) or not any(named.search(m["text"].casefold()) for m in memories):
        return False
    if change.get("parent"):
        parent = state["nodes"].get(change["parent"])
        if not parent:
            return False
        if not (any(parent["title"].casefold() in text for text in typed) or any(
                "comment:%d" % m["comment"] in parent["evidence"] or "issue:%d" % m["issue"] in parent["evidence"]
                for m in memories)):
            return False
    if change["action"] == "rename":
        node = state["nodes"].get(change["id"], {})
        old = node.get("title", "").casefold()
        return bool(old and not node.get("manual") and any(old in text and title in text and RENAME.search(text)
                                                           for text in typed))
    return True


def line_for(change, state, affected):
    def title(node):
        if node is None:
            return "project root"
        return "%s [%s]" % (state["nodes"].get(node, {}).get("title", node), node)
    act = change["action"]
    line = {"add": 'Add "%s" under "%s"' % (change.get("title", ""), title(change.get("parent"))),
            "rename": 'Rename "%s" to "%s"' % (title(change.get("id")), change.get("title", "")),
            "move": 'Move "%s" under "%s"' % (title(change.get("id")), title(change.get("parent"))),
            "delete": 'Remove "%s" and keep its children under its parent' % title(change.get("id")),
            "restore": 'Restore "%s"' % title(change.get("id")),
            "merge": 'Merge "%s" into "%s"' % (title(change.get("id")), title(change.get("into"))),
            "split": 'Split "%s" into %s' % (title(change.get("id")), ", ".join(
                '"%s"%s' % (b["title"], " with " + ", ".join(title(child) for child in b["children"]) if b["children"] else "")
                for b in change.get("branches", [])))}[act]
    return line + " (%d affected)" % len(affected)


def iso_time(value):
    try:
        stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else 0
    except (ValueError, AttributeError, OverflowError, OSError):
        return 0


def proposal(repo, hub, state, memories, sources, candidate, judgment, shadow):
    cited = ordinal_sources(candidate, sources, memories)
    if not cited or judgment == "reject":
        return None
    support = {str(key): runtime.candidate_hash(memories[key]) for key in candidate["memories"]}
    change = change_for(candidate)
    if change["action"] == "delete" and not any(
            s.get("direct_user") and re.search(r"\b(?:delet(?:e|ing)|remov(?:e|ing)|retir(?:e|ing)|drop)\b", s["text"], re.I)
            for s in cited):
        return None
    if change["action"] == "restore":
        deleted = max([iso_time(h["at"]) for h in state["history"] if h["action"] == "delete" and
                       change["id"] in h["affected"]] or [0])
        node = state["nodes"].get(change["id"], {})
        title = node.get("title", "").casefold()
        if not deleted or not title or not any(iso_time(s["at"]) > deleted and title in s["text"].casefold() for s in cited):
            return None
    sid = sorted(s["source_id"] for s in sources)  # every session seen by either call must still permit publication
    key = runtime.candidate_hash([repo.lower(), hub, state["revision"], change, sid, support])[:32]
    if change["action"] == "add" and any(change["title"].casefold() in [a.casefold() for a in n["aliases"]] or
                                         (n["deleted"] and n["title"].casefold() == change["title"].casefold())
                                         for n in state["nodes"].values()):
        return None  # resurfacing this concept requires a restore preview with new evidence
    approved = judgment == "supported" and routine(change, cited, [memories[c] for c in candidate["memories"]], state)
    if change["action"] == "add":
        change["id"] = runtime.candidate_hash([key, "added"])[:32]
    elif change["action"] == "split":
        change["branches"] = [dict(branch, id=runtime.candidate_hash([key, i])[:32])
                              for i, branch in enumerate(change["branches"])]
    try:
        envelope = project_map.prepare(state, change, source="observer" if approved else "user", op_id=key)
        after = project_map.apply(state, envelope)
    except project_map.MapError:
        return None
    affected = after["history"][-1]["affected"]
    def details(node):
        if node is None:
            return None
        return {k: node[k] for k in ("title", "parent", "deleted")}
    impact = [{"id": nid, "before": details(state["nodes"].get(nid)), "after": details(after["nodes"][nid])}
              for nid in affected]
    line = line_for(change, state, affected)
    if len(runtime.canonical(impact).encode("utf-8")) > 4800 or len(line) > 440 or any(char in line for char in "\u202a\u202b\u202d\u202e\u2066\u2067\u2068\u2069"):  # the whole review must fit the actual dialog
        return None
    citations = {"conversation": [{"message": s["source_id"], "role": s["role"], "at": s.get("at")} for s in cited],
                 "record": [record_store.address_of(repo, memories[k]["issue"], k)
                            for k in candidate["memories"]]}
    item = {"created": time.time(), "hub": hub, "line": line, "change": change, "support": sorted(support), "cited": citations,
            "proof": support, "sources": sid, "operation": envelope, "judgment": judgment, "affected": impact,
            "state": "shadow" if shadow else "ready" if approved else "review", "routine": approved,
            "generation": "shadow" if shadow else "automatic"}
    return dict(item, key=review_key(repo, item))


def call(store, invoke, options, purpose, payload, schema):
    rid = store.reserve_invocation(purpose, options["max_invocations"], options["window_hours"] * 3600,
                                   tokens=options["middle_tokens"])
    if rid is None:
        return None
    result = invoke(payload, schema, purpose, options)
    data = result.get("structured")
    valid = result.get("ok") is True and runtime.validate(data, schema)
    store.complete_invocation(rid, valid, runtime.candidate_hash(data) if valid else "",
                              result.get("usage"), result.get("model"))
    return data if valid else None


def assess(repo, hub, state, memories, sources, hints, config, shadow, call_fn):
    """Same evidence, two independent calls and admission in production and in blinded offline trials."""
    import observer_worker
    wanted = observer_context.task_terms(" ".join(source["text"] for source in sources))
    nodes = sorted(state["nodes"].values(), key=lambda n: (
        -len(wanted & observer_context.task_terms(n["title"])), n["id"]))
    outline = [{k: n[k] for k in ("id", "title", "parent", "deleted", "manual")} for n in nodes[:80]]
    materials = {"sources": [dict(observer_worker._view(s, i), text=s["text"]) for i, s in enumerate(sources, 1)],
                 "outline": outline, "memories": [{k: m[k] for k in ("comment", "issue", "kind", "text")}
                                                  for m in memories.values()]}
    for hint in hints[:6]:
        part = {"path": hint["path"], "headings": [h["title"][:120] for h in hint["headings"][:8]]}
        if not observer_worker.source_privacy_problem(part, config):
            materials.setdefault("file_headings", []).append(part)
    options = runtime.settings(config)
    cap = runtime.input_cap(options, "middle")
    def fits(purpose, payload, schema):
        return len(runtime.canonical(payload)) + len(runtime.canonical(schema)) + len(runtime.PROMPTS[purpose]) + 100 <= cap
    for field in ("file_headings", "outline", "memories"):
        while materials.get(field) and not fits("map_draft", materials, DRAFT):
            materials[field].pop()
    if not fits("map_draft", materials, DRAFT):
        return "input_limit", None
    memories = {m["comment"]: memories[m["comment"]] for m in materials["memories"]}
    if observer_worker.source_privacy_problem(materials, config):
        return "privacy", None
    candidate = call_fn("map_draft", materials, DRAFT)
    if not runtime.validate(candidate, DRAFT):
        return "call_unavailable", None
    if candidate["action"] != "none" and ordinal_sources(candidate, sources, memories):
        change = change_for(candidate)
        text = " ".join([candidate["title"]] + [branch["title"] for branch in candidate["branches"]])
        if not (observer_worker.source_privacy_problem(change, config) or observer_publisher.outbound_problem(text, repo)):
            digest = runtime.candidate_hash(candidate)
            review = dict(materials, candidate=candidate, candidate_hash=digest)
            if not fits("map_review", review, REVIEW):
                needed = {candidate["node"], candidate["target"]}
                needed.update(child for branch in candidate["branches"] for child in branch["children"])
                review = dict(review, outline=[n for n in outline if n["id"] in needed],
                              memories=[m for m in materials["memories"] if m["comment"] in candidate["memories"]])
                review.pop("file_headings", None)
            if not fits("map_review", review, REVIEW):
                return "input_limit", None
            answer = call_fn("map_review", review, REVIEW)
            if not runtime.validate(answer, REVIEW) or answer["candidate_hash"] != digest:
                return "review_unavailable", None
            item = proposal(repo, hub, state, memories, sources, candidate, answer["judgment"], shadow)
            if item:
                return item["state"], item
    return "quiet", None


def consider(store, repo, config, options, invoke, root):
    import observer_worker
    import project_map_sources
    if options["map_mode"] == "off" or options["level"] not in ("assistant", "everything"):
        return "off"
    if not runtime.approval_valid(repo, config) or store.get_control("capture_paused", False):
        return "paused"
    with observer_store._exclusive_lock(store.folder, "project-map.lock"):
        pending = rows(store)
        if options["mode"] == options["map_mode"] == "automatic":
            ready = sorted((p for p in pending if p["state"] == "ready" and not held(store, p["sources"], p["hub"])),
                           key=lambda p: (p.get("last_attempt", 0), p["created"]))
            if ready and not store.get_control("publication_paused", False):
                item = ready[0]
                outcome = publish(store, repo, config, item)["code"]
                if outcome == "map_applied":
                    return outcome
                save(store, dict(item, last_attempt=time.time(), last_result=outcome, state="stale" if outcome in
                                 ("stale_map_evidence", "map_conflict") else "ready"))
        if sum(1 for p in pending if p["state"] in ("ready", "review")) >= QUEUE_LIMIT:
            return "queue_full"
        sources, cursor = material(store, repo, config, root)
        if not sources:
            if cursor:
                store.set_control(control("cursor"), cursor)
            return "quiet"
        hub = config.get("hub")
        evidence = record(repo, hub, " ".join(s["text"] for s in sources))
        if evidence is None:
            return "record_unavailable"
        state, memories = evidence
        approved_options = runtime.with_gates(options, store)
        source_ids = [s["source_id"] for s in sources]
        hints = project_map_sources.snapshot(root, repo)
        expected_files = {hint["path"]: runtime.candidate_hash(hint) for hint in hints}
        approved_users = set(recortex.allowed_users(repo))
        shared_paths = set()
        sent_payloads = []

        def checked_call(purpose, payload, schema):
            fresh = runtime.local_config(recortex.load_config())
            if (fresh.get("hub") != hub or not recortex.valid_repo(fresh.get("repo")) or fresh["repo"].lower() != repo.lower()
                    or not runtime.approval_valid(repo, fresh)
                    or runtime.settings(fresh) != options or store.get_control("capture_paused", False)
                    or not approved_users or set(recortex.allowed_users(repo)) != approved_users
                    or held(store, source_ids, hub, publication=False)
                    or observer_worker.source_privacy_problem([payload] + sent_payloads, fresh)):
                return None
            # The reviewer may receive a shorter packet. Its candidate still depends on files shown to the drafter.
            shared_paths.update(hint["path"] for hint in payload.get("file_headings", []))
            if shared_paths:
                confirmed = {hint["path"]: runtime.candidate_hash(hint) for hint in project_map_sources.snapshot(root, repo)}
                if any(confirmed.get(path) != expected_files[path] for path in shared_paths):
                    return None
            sent_payloads.append(payload)
            return call(store, invoke, approved_options, purpose, payload, schema)
        result, item = assess(repo, hub, state, memories, sources, hints, config,
                              options["mode"] != "automatic" or options["map_mode"] != "automatic", checked_call)
        if result in ("call_unavailable", "review_unavailable"):
            return result
        if item and store.get_control(rejection_key(item), False):
            result, item = "previously_declined", None
        if item and not held(store, item["sources"], hub, publication=False) and not any(p["key"] == item["key"] for p in pending):
            save(store, item)
        store.set_control(control("cursor"), cursor)
        return result


def verify_comments(repo, memories, config=None):
    """REST timestamps alone cannot distinguish an outsider's edit made in the same second as the post."""
    if not memories:
        return
    nodes = {}
    allowed = {login.lower() for login in recortex.allowed_users(repo)}
    records = record_store.for_repo(repo, config)
    try:
        for cid, memory in memories.items():
            raw = observer_publisher._remote_comment(repo, cid, config)
            author = (raw.get("user") or {}).get("login")
            actual = [memory["issue"], author, raw.get("body"), raw.get("created_at"), raw.get("updated_at")]
            if (type(raw.get("id")) is not int or raw["id"] != cid or not isinstance(author, str)
                    or author.lower() not in allowed or runtime.candidate_hash(actual) != memory["comment_hash"]
                    or str(raw.get("issue_url", "")).lower() != records.api_issue(memory["issue"]).lower()):
                raise observer_publisher.PublishError("stale_map_evidence")
            node = raw.get("node_id")
            if not isinstance(node, str) or not index.COMMENT_NODE.fullmatch(node):
                raise observer_publisher.PublishError("map_evidence_unavailable")
            nodes[cid] = (node, None)
        response = json.loads(records.histories(nodes))
        if response.get("errors"):
            raise observer_publisher.PublishError("map_evidence_unavailable")
        for cid in nodes:
            history = (response.get("data") or {}).get("c%d" % cid) or {}
            changes = history.get("userContentEdits") or {}
            edits = changes.get("nodes")
            if (str(history.get("fullDatabaseId")) != str(cid) or "lastEditedAt" not in history or
                    (changes.get("pageInfo") or {}).get("hasNextPage") is not False or not isinstance(edits, list)
                    or bool(edits) != bool(history["lastEditedAt"])):
                raise observer_publisher.PublishError("map_evidence_unavailable")
            if any(not isinstance(edit, dict) or not isinstance(edit.get("editor"), dict)
                   or str(edit["editor"].get("login", "")).lower() not in allowed for edit in edits):
                raise observer_publisher.PublishError("stale_map_evidence")
    except (recortex.GhError, OSError, ValueError) as error:
        raise observer_publisher.PublishError("map_evidence_unavailable") from error


def publish(store, repo, config, item, manual=False):
    """The write callback runs only after a complete fresh hub replay and reconciliation of the same operation id."""
    key, hub = item["key"], item["hub"]

    def check():
        import observer_worker
        if (item.get("generation") != "automatic" or key != review_key(repo, item)
                or item["change"] != item["operation"]["change"]):
            raise observer_publisher.PublishError("invalid_map_review")
        fresh_config = runtime.local_config(recortex.load_config())
        if not recortex.valid_repo(fresh_config.get("repo")) or fresh_config["repo"].lower() != repo.lower() or fresh_config.get("hub") != hub:
            raise observer_publisher.PublishError("not_approved")
        settings = observer_publisher._gate(store, fresh_config)
        if observer_worker.source_privacy_problem(item["change"], fresh_config):
            raise observer_publisher.PublishError("privacy")
        if (settings["map_mode"] != "automatic" or settings["level"] not in ("assistant", "everything")
                or store.get_control("capture_paused", False)):
            raise observer_publisher.PublishError("not_approved")
        if held(store, item["sources"], hub):
            raise observer_publisher.PublishError("withheld")
        if manual and not store.has_grant(key):
            raise observer_publisher.PublishError("map_unanswered")
        if not manual and (item["state"] != "ready" or not item["routine"] or item["change"]["action"] not in ("add", "rename")):
            raise observer_publisher.PublishError("map_unanswered")
        return settings

    try:
        with store.publication_lock():
            check()
            project_map_io.remember(repo, hub, item["operation"], channel="observer")

            def guard():
                settings = check()
                if item["proof"]:
                    try:
                        fresh = index.sync(repo, hub, rebuild=True, timeout=15, deadline=90, log=lambda *args: None)
                    except (index.SyncBusy, recortex.GhError, OSError) as error:
                        raise observer_publisher.PublishError("map_evidence_unavailable") from error
                    if fresh.get("complete") is not True:
                        raise observer_publisher.PublishError("map_evidence_unavailable")
                evidence = record(repo, hub, "", requested={int(k) for k in item["proof"]})
                if evidence is None or item["proof"] != {str(k): runtime.candidate_hash(m) for k, m in evidence[1].items()}:
                    raise observer_publisher.PublishError("stale_map_evidence")
                verify_comments(repo, evidence[1], config)
                check()  # permissions and holds may have changed during the rebuild
                observer_publisher._daily_cap(store, settings)
            result = project_map_io.commit(repo, hub, item["operation"], guard=guard)
            save(store, dict(item, state="published", comment=result["comment"]))
            if manual:
                store.take_grant(key)
            store.set_control("sync_needed", True)
            return {"code": "map_applied", "comment": result["comment"]}
    except observer_publisher.PublishError as error:
        return {"code": error.reason}
    except project_map.MapError as error:
        # An uncertain send or an unavailable account must keep the same id for eventual reconciliation.
        try:
            state = project_map_io.current(repo, hub)
        except (project_map.MapError, recortex.GhError):
            return {"code": "map_unverified", "note": str(error)}
        try:
            project_map.apply(state, item["operation"])
        except project_map.MapError:
            return {"code": "map_conflict", "note": str(error)}
        return {"code": "map_unverified", "note": str(error)}


def answer(store, repo, config, key, drop=False):
    with observer_store._exclusive_lock(store.folder, "project-map.lock"):
        item = next((p for p in rows(store) if p["key"] == key and p["state"] == "review"), None)
        if not item:
            return {"code": "not_waiting"}
        if drop:
            store.set_control(rejection_key(item), True)
            save(store, dict(item, state="dropped"))
            return {"code": "dropped"}
        result = publish(store, repo, config, item, manual=True)
        if result["code"] in ("stale_map_evidence", "map_conflict"):
            save(store, dict(item, state="stale"))
        return result

"""Read-only review of current project material after an explicitly recorded decision change."""

import collections
import contextlib
import datetime
import hashlib
import re
import signal
import sqlite3
import time

import index
import project_map
import record_store
import recortex
import record_format

MAX_GROUPS = 32
MAX_TARGETS = 1200
MAX_RESULTS = 40
MAX_PASSAGE = 12000
MAX_CHAIN = 128
MAX_AGE = 86400
HISTORY = re.compile(r"^(?:change ?log|history|archived|historical|superseded|revision history)\b", re.I)
COMPARISON = re.compile(r"\b(?:baseline|formerly|previous(?:ly)?|historical|compar(?:e|ing|ison)|original)\b", re.I)
TERMS = re.compile(r"[a-z][a-z0-9-]*|[.!?;\n]")
COMMON = set(("the and for was are but can could did does has had how its our out over put set use way why who "
              "about after again agreed before between change changed check choose chosen compare current decision "
              "default draft each every first follow from have into like make must need next only open other our "
              "project record recorded repeat report require return review same should since still task team test "
              "that their them then there these they this those through under until update used using verify what "
              "when where which will with would").split())


class TooSlow(Exception):
    """Work that a hook began did not finish in the time the hook gave it."""


class _Ring(BaseException):
    """Raised by the alarm. A BaseException, so that no `except Exception` inside the work swallows it."""


def _ring(signum, frame):
    raise _Ring()


@contextlib.contextmanager
def time_limit(seconds, db=None):
    """Stop the work inside the block with TooSlow after `seconds`. A hook cannot wait for a projection of a large record:
    this keeps it a fraction of the hook's budget, so that the brief is not lost with it.

    The alarm (SIGALRM, where the platform has it and this is the main thread) interrupts Python code; `db`'s progress
    handler interrupts a long query, which the alarm cannot reach until the query returns. A timer that the caller already
    set is kept: it is not given more than half of what it had left, and it is armed again, shortened by what this block
    used, on the way out (the observer's hook runs its whole work under one)."""
    deadline = time.monotonic() + seconds
    armed, previous, outer = False, None, (0.0, 0.0)
    if hasattr(signal, "setitimer"):
        try:
            outer = signal.getitimer(signal.ITIMER_REAL)
            if outer[0]:
                seconds = min(seconds, outer[0] / 2.0)
                deadline = time.monotonic() + seconds
            previous = signal.signal(signal.SIGALRM, _ring)
            armed = True
            signal.setitimer(signal.ITIMER_REAL, seconds)
        except ValueError:  # a thread that is not the main one cannot take a signal; the progress handler still applies
            pass
    started, interrupted = time.monotonic(), []

    def progress():
        if time.monotonic() < deadline:
            return 0
        interrupted.append(True)
        return 1  # stops the running query with sqlite3.OperationalError("interrupted")

    watched = db is not None and hasattr(db, "set_progress_handler")   # a stand-in for a connection may have no such hook
    if watched:
        db.set_progress_handler(progress, 10000)
    try:
        yield
        if interrupted:  # the work caught the interrupted query and went on: its result is not whole
            raise TooSlow("out of time")
    except _Ring:
        raise TooSlow("out of time") from None
    except sqlite3.OperationalError:
        if interrupted:
            raise TooSlow("out of time") from None
        raise
    finally:
        if watched:
            db.set_progress_handler(None, 0)
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous if previous is not None else signal.SIG_DFL)
            if outer[0]:
                signal.setitimer(signal.ITIMER_REAL, max(0.001, outer[0] - (time.monotonic() - started)), outer[1])


def suggestions_enabled(repo, config, root=None):
    """Only a locally approved observer with the advisory feature enabled can propose inferred connections."""
    import observer_runtime
    import storage
    try:
        local = observer_runtime.local_config(config, root)
        return (observer_runtime.approval_valid(repo, local, root=root)
                and observer_runtime.settings(local)["advisory_mode"] in ("shadow", "deliver"))
    except (ValueError, OSError, storage.StorageError):
        return False


def _url(repo, issue, comment=None):
    return record_store.address_of(repo, issue, comment or None)


def _short(text, size=400):
    from mind_model import short
    return short(text, cap=size)


def _visible(text):
    """Keep offsets while excluding code, deleted or quoted text, and hidden Markdown comments."""
    text = record_format.masked(text)
    hidden = re.compile(r"<!--.*?(?:-->|$)|<details\b.*?(?:</details>|$)|~~[^\n]*?~~", re.I | re.S)
    text = hidden.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)
    lines = text.split("\n")
    skip = record_format.quote_lines(lines, set())
    return "\n".join(" " * len(line) if n in skip or re.match(r"^\s*[-*+]\s+\[[xX]\]", line) else line
                     for n, line in enumerate(lines))


def _terms(text):
    return TERMS.findall(record_format.masked(re.sub(r"https?://\S+", " ", text)).lower())


def _pairs(words):
    return {(first, second) for first, second in zip(words, words[1:])
            if min(len(first), len(second)) >= 3 and first != second and first not in COMMON and second not in COMMON}


def _quote(text, offset):
    """Keep the cited words in view, even when the citation falls late in a long paragraph."""
    start = max(text.rfind("\n", 0, offset) + 1, offset - 150)
    end = text.find("\n", offset)
    return ("…" if start > 0 and text[start - 1] != "\n" else "") + _short(text[start:end if end >= 0 else None])


def _fresh(db):
    if index.get_meta(db, "sync_complete") != "1" or index.get_meta(db, "derive_pending") == "1" or not index.derived_current(db):
        return False
    try:
        stamp = datetime.datetime.strptime(index.get_meta(db, "synced_at") or "", "%Y-%m-%dT%H:%M:%SZ")
        age = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - stamp).total_seconds()
        return 0 <= age <= MAX_AGE
    except ValueError:
        return False


def _changes(items):
    children = collections.defaultdict(set)
    for memory in items.values():
        if memory["kind"] in ("decision", "correction") and not memory["reconstructed"]:
            for relation, old in memory["links"]:
                if relation == "replaces" and old in items and not items[old]["reconstructed"]:
                    children[old].add(memory["id"])
    return children


def _successors(old, children, items):
    """Walk replacements to standing decisions/corrections. Cycles and overlong chains give no verdict."""
    seen, active, standing = set(), set(), set()
    def visit(current):
        if current in active or len(seen) + len(active) >= MAX_CHAIN:
            return False
        if current in seen:
            return True
        active.add(current)
        if items[current]["state"] == "stands":
            standing.add(current)
        elif items[current]["state"] == "replaced":
            if not all(visit(child) for child in children.get(current, ())):
                return False
        active.remove(current)
        seen.add(current)
        return True

    if not visit(old):
        return None
    return sorted(standing)


def _section_labels(config):
    profiles = recortex.shipped_profiles((config or {}).get("profiles"))
    return recortex.section_labels(profiles, shipped_only=True)


def protected_section(config, issue, heading, labels, plan_state=None, section_labels=None):
    """Author-owned or manuscript text uses the existing paper workflow even in a mixed-output project."""
    key = heading.casefold()
    if issue == recortex.plan_issue(config, "manuscript"):
        return True
    if recortex.is_brought_section(config, issue, heading):
        return True
    if plan_state and any(part["standing"] and part["topic"] == issue and part["section"].casefold() == key
                          for part in plan_state["parts"].values()):
        return True
    labels = {label.lower() for label in labels}
    sections = _section_labels(config) if section_labels is None else section_labels
    matching = {label.lower() for label, name in sections if key == name.casefold()}
    matched = matching & labels
    if any(label.startswith("ms:") for label in matched):
        return True
    if matched:
        return False  # A named software/report section of this issue.
    return any(label.startswith("ms:") for label in matching | labels)


def _candidates(build, found, tasks, map_state, present, report):
    db, repo = build.db, build.repo

    def candidate(kind, key, label, text, issue, anchor="", comment_url=None, refs=(), material=None, **extra):
        if len(text) > MAX_PASSAGE:
            report("Some long passages exceeded the review window.")
        passage = text[:MAX_PASSAGE]
        visible = _visible(passage)
        citations = {(row["issue"], row["comment"]): row["start"] for row in record_format.citations(visible, repo)
                     if row["kind"] == "memory"}
        return {"id": project_map.digest([repo.lower(), kind, key]), "kind": kind, "label": _short(label, 200),
                "issue": issue, "url": _url(repo, issue, comment_url), "anchor": anchor,
                "_text": passage, "_visible": visible, "_cites": citations, "_refs": set(refs), "_pairs": None,
                "_version": project_map.digest([text, material]), **extra}

    drops = collections.defaultdict(set)
    for source, target, ordinal in db.execute("SELECT source, target, step FROM marks WHERE kind='dropped'"):
        memory = found.get(source)
        if memory and memory["kind"] == "decision" and not memory["reconstructed"]:
            drops[target, ordinal].add(source)
    raw_steps = {(cid, ordinal): text for cid, ordinal, text in db.execute("SELECT comment, ordinal, text FROM steps")}
    for task in tasks["open"]:
        issue = task["issue"]
        for step in task["steps"]:
            if step["state"] not in ("open", "blocked"):
                continue
            cid, ordinal = step["comment"], step["ordinal"]
            # The dashboard shortens steps for display. Use the full agreed wording for review.
            passage = raw_steps.get((cid, ordinal), step["text"])
            yield candidate("step", [cid, ordinal], "Step %d in task #%d" % (step["n"], issue),
                            passage, issue, "work-step-%d-%d-%d" % (issue, cid, ordinal), cid,
                            refs=drops[cid, ordinal], material=step["state"], comment=cid, ordinal=ordinal, n=step["n"])

        if task["steps"] and not any(step["state"] in ("open", "blocked") for step in task["steps"]):
            continue
        if not build.body_shown(issue):
            report("Some current task text could not be verified.")
            continue
        step_texts = {raw_steps.get((step["comment"], step["ordinal"]), step["text"]).strip() for step in task["steps"]}
        seen = collections.Counter()
        for row in record_format.sections(build.issues[issue]["body"]):
            heading, text = row["heading"], row["text"].strip()
            key = heading.casefold()
            if key not in ("goal", "scope", "done when", "acceptance criteria") or not text or text in step_texts:
                continue
            seen[key] += 1
            yield candidate("task", [issue, key, seen[key]], "%s in task #%d" % (heading, issue),
                            text, issue, "work-task-%d" % issue, heading=_short(heading))

    changes = {}
    for event in map_state["history"]:
        for key in event["affected"]:
            changes[key] = event.get("comment")
    for key, node in sorted(map_state["nodes"].items()):
        if node["deleted"]:
            continue
        refs = [int(value.split(":")[1]) for value in node["evidence"] if value.startswith("comment:")]
        yield candidate("branch", key, node["title"], node["description"] or node["title"], build.hub,
                        project_map.branch_anchor(key), changes.get(key), refs=refs, material=node, branch=key)

    threads = {t["topic"]: t["n"] for t in getattr(build, "threads", []) if t["topic"] is not None}
    config = build.config or {"profiles": build.profiles}
    section_labels = _section_labels(config)
    plan_state = getattr(build, "plan_state", None)
    for issue, entry in sorted(build.issues.items()):
        if entry["state"] != "open" or not build.is_topic(issue):
            continue
        if not build.body_shown(issue):
            report("Some current topic text could not be verified.")
            continue
        anchor = "thread-%d" % threads[issue] if issue in threads else ""
        body = entry["body"]
        seen = collections.Counter()
        for row in db.execute("SELECT heading, text FROM sections WHERE issue=? ORDER BY ordinal", (issue,)):
            heading, text = row
            if heading.casefold() == recortex.SOURCES_HEADING.casefold() or HISTORY.match(heading) or not text.strip():
                continue
            seen[heading] += 1
            yield candidate("section", [issue, heading, seen[heading]], "%s · #%d" % (heading, issue),
                            text, issue, anchor, heading=_short(heading), _body=body,
                            _protected=(build.config is None and not build.profiles) or
                            protected_section(config, issue, heading, build.labels.get(issue, ()), plan_state, section_labels))
        source_table = record_format.section_text(body, recortex.SOURCES_HEADING) or ""
        table = "\n".join(raw if visible.strip().startswith("|") else ""
                          for raw, visible in zip(source_table.split("\n"), _visible(source_table).split("\n")))
        for ordinal, row in enumerate(record_format.table_rows(table), 1):
            statement, source = row[0], " | ".join(row[1:])
            yield candidate("source", [issue, ordinal], "Source for %s" % statement,
                            statement + " | " + source, issue, anchor, heading=recortex.SOURCES_HEADING)

    record_fields = collections.defaultdict(list)
    for row in db.execute("SELECT comment,label,text FROM event_fields ORDER BY comment,ordinal"):
        if row["comment"] in found:
            record_fields[row["comment"]].append((row["label"], row["text"]))
    for cid, memory in sorted(found.items(), reverse=True):
        if memory["kind"] not in ("question", "idea", "decision", "result", "correction") or memory["reconstructed"]:
            continue
        if memory["state"] not in ("stands", "open", "untried") or build.issues.get(memory["issue"], {}).get("state") != "open":
            continue
        refs = [target for kind, target in memory["links"] if kind in ("because", "checks")]
        fields = record_fields.get(cid, [])
        # Keep current conclusions separate from quotations and attribution.
        operative = {"decision", "effect", "next step", "open question", "not established", "what ran"}
        passages = [memory["plain"] or memory["title"]] + [value for label, value in fields if label.casefold() in operative]
        text = "\n\n".join(part for part in passages if part)
        yield candidate("record", cid, memory["title"] or memory["kind"].capitalize(), text, memory["issue"],
                        "m-%d" % cid if present and cid in present else "", cid, refs=refs,
                        material=[memory["links"], memory["state"], fields], comment=cid)


def _source(repo, memory, present):
    cid = memory["id"]
    return {"id": cid, "issue": memory["issue"], "kind": memory["kind"], "text": _short(memory["plain"] or memory["title"]),
            "url": _url(repo, memory["issue"], cid), "anchor": "m-%d" % cid if present and cid in present else ""}


# A citation-only draft is safe for a reference that delegates the choice to the cited record.
# Keep the vocabulary deliberately narrow. Other prose stays available for a human-authored revision.
_REFERENCE_NOUN = r"(?:(?:current|standing|adopted|approved|latest|documented) )?(?:(?:reader|release-gate|sampling|reporting|project) )?(?:decision|policy|rule|source)"
_CHOICE_NOUN = r"(?:(?:release|reporting|input|output|sampling|test) )?(?:corpus|population|format|dataset|threshold|setting|method|protocol|option)"
REFERENCE_ONLY = re.compile(
    r"(?:@|(?:the )?" + _REFERENCE_NOUN + r"[: ]+@|\[" + _REFERENCE_NOUN + r"\]\(@\)|"
    r"(?:see|consult|follow|refer to) (?:the )?" + _REFERENCE_NOUN + r"(?: in| at|:) @|"
    r"(?:choose|select|determine) the " + _CHOICE_NOUN + r" according to the " + _REFERENCE_NOUN + r" \(@\)|"
    r"(?:the|this) (?:release|project|report|workflow|process) follows the " + _REFERENCE_NOUN + r" in @)\.?", re.I)


def _reference_only(paragraph, url):
    if "@" in paragraph or paragraph.count(url) != 1:
        return False
    return bool(REFERENCE_ONLY.fullmatch(paragraph.replace(url, "@").strip()))


def _propose_edit(target, old, successors, item):
    if (target["kind"] != "section" or target["_protected"] or len(successors) != 1
            or item["relation"] != "direct"):
        return
    body = recortex.normalize(target["_body"])
    # Match the actual visible citation within this exact paragraph, including when the body contains code blocks.
    verified_at = target["_cites"][(old["issue"], old["id"])]
    for paragraph in re.split(r"\n[ \t]*\n", target["_text"]):
        start = target["_text"].find(paragraph)
        if not start <= verified_at < start + len(paragraph):
            continue
        if (not _reference_only(paragraph, old["url"]) or not 0 < len(paragraph) <= 2000 or body.count(paragraph) != 1
                or COMPARISON.search(paragraph) or not all(re.match(r"^[\w*\[]", line) for line in paragraph.splitlines())
                or re.search(r"[<>|`]|!\[|\n\s*[0-9]+\.", paragraph)):
            continue
        item.update(edit_before=paragraph, edit_text=paragraph.replace(old["url"], successors[0]["url"]),
                    body_digest=hashlib.sha256(body.encode()).hexdigest())
        return


def _impact(group, target, pairs, suggestions, source_version):
    old, successors = group["old"], group["successors"]
    if target["kind"] == "record" and target["comment"] in [memory["id"] for memory in successors]:
        return None
    cited = target["_cites"].get((old["issue"], old["id"]))
    offset = cited or 0
    if old["id"] in target["_refs"]:
        relation = "direct"
        reason = {"branch": "The current branch lists the earlier decision as evidence.",
                  "step": "The earlier decision dropped this step. The task now shows it as open.",
                  "record": "This standing record explicitly links to the earlier decision."}[target["kind"]]
    elif cited is not None:
        relation, reason = "direct", "The quoted passage explicitly cites the earlier decision."
    elif suggestions:
        if any((m["issue"], m["id"]) in target["_cites"] or m["id"] in target["_refs"] for m in successors):
            return None
        if target["_pairs"] is None:
            target["_pairs"] = _pairs(_terms(target["_visible"]))
        shared = sorted(pairs & target["_pairs"])
        if not shared:
            return None
        phrase = " ".join(shared[0])
        offset = max(0, target["_visible"].lower().find(shared[0][0]))
        relation, reason = "suggested", 'Both passages mention “%s”. Does this work rely on the earlier decision?' % phrase
    else:
        return None
    item = {key: value for key, value in target.items() if not key.startswith("_")}
    item.update(quote=_quote(target["_text"], offset), relation=relation, reason=reason, review=None,
                revision=project_map.digest([source_version, target["_version"], relation, reason]))
    action = {"task": "Check whether the task requirement should change",
              "step": "Check whether to restate this open step while keeping the rest of the agreed plan",
              "branch": "Check whether the branch should cite the replacement decision as evidence",
              "section": "Check whether to revise this passage or keep its original scope",
              "source": "Verify whether the statement still needs its earlier citation",
              "record": "Check whether this conclusion needs a cited follow-up"}[item["kind"]]
    item["draft"] = "%s for “%s” given the recorded change to “%s”." % (action, item["label"], successors[0]["text"])
    if group["conflict"]:
        item["draft"] = "Clarify which replacement applies to “%s” before proposing a change." % item["label"]
    elif relation == "suggested":
        item["draft"] = "Check whether “%s” depends on the earlier decision before changing its wording." % item["label"]
    _propose_edit(target, old, successors, item)
    return item


def from_index(db, repo, allowed, *, suggestions=False, builder=None, tasks=None, map_state=None):
    """Project the trusted local index and replay accepted shared reviews. Never queries GitHub or writes."""
    began = not db.in_transaction
    if began:
        db.execute("BEGIN")
    try:
        return _project(db, repo, allowed, suggestions, builder, tasks, map_state)
    finally:
        if began and db.in_transaction:
            db.execute("ROLLBACK")


def _project(db, repo, allowed, suggestions, builder, tasks, map_state):
    import decision_review
    import mind_model
    coverage = []

    def report(message):
        if message not in coverage:
            coverage.append(message)

    result = {"groups": [], "partial": False, "coverage": coverage}
    if not allowed or not recortex.valid_repo(repo) or (index.get_meta(db, "repo") or "").lower() != repo.lower():
        report("This project has no verified local record for review.")
    elif not _fresh(db):
        report("Refresh the local index to check current work against changed decisions.")
    if coverage:
        result["partial"] = True
        return result

    if builder is None:
        builder = mind_model.Build(db, repo, {name.lower() for name in allowed}, datetime.date.today())
        builder.read_issues()
    cache = getattr(builder, "found", None) or index.memories(db, allowed)
    found, present = cache["items"], getattr(builder, "m", None)
    inaccessible = db.execute("SELECT source, target FROM links WHERE kind='replaces'")
    if any(source in found and not found[source]["reconstructed"] and found[source]["kind"] in ("decision", "correction")
           and target not in found for source, target in inaccessible):
        report("An earlier record named by a replacement could not be verified in this index.")
    children = _changes(found)
    olds = sorted((old for old in children if found[old]["kind"] == "decision"),
                  key=lambda old: max((found[c]["time"], c) for c in children[old]), reverse=True)
    if len(olds) > MAX_GROUPS:
        report("Review covers the most recent %d changed decisions." % MAX_GROUPS)
    groups = result["groups"]
    decision_versions = {}

    def version(cid):
        if cid not in decision_versions:
            row = db.execute("SELECT body FROM comments WHERE id=?", (cid,)).fetchone()
            decision_versions[cid] = project_map.digest([cid, row[0] if row else found[cid]["plain"], found[cid]["links"]])
        return decision_versions[cid]

    if olds:
        tasks = tasks if tasks is not None else builder.tasks_model() if hasattr(builder, "found") else None
        if tasks is None:
            tasks = {"open": [t for t in index.tasks(db, allowed)["tasks"] if t["state"] == "open"]}
        if tasks.get("available") is False:
            report("Current task steps were unavailable.")
        if map_state is None:
            map_state = project_map.read_map(db, builder.hub, allowed)
        targets = []
        for target in _candidates(builder, found, tasks, map_state, present, report):
            if len(targets) == MAX_TARGETS:
                report("Review covers up to %d current targets." % MAX_TARGETS)
                break
            targets.append(target)
        for cid in olds[:MAX_GROUPS]:
            live = _successors(cid, children, found)
            if live is None:
                report("An ambiguous or unusually long replacement chain could not be checked.")
                continue
            if not live:
                continue
            old = _source(repo, found[cid], present)
            successors = [_source(repo, found[c], present) for c in live]
            group = {"id": "impact-%d" % cid, "hub": builder.hub, "old": old, "successors": successors, "conflict": len(live) > 1,
                     "items": [], "review_history": []}
            source_version = project_map.digest([version(c) for c in [cid] + live])
            old_terms = set()
            if suggestions:
                old_terms = _pairs(_terms(found[cid]["plain"] or found[cid]["title"]))
                for successor in live:
                    # Vocabulary retained by the new policy cannot distinguish a reliance on the earlier choice.
                    old_terms -= _pairs(_terms(found[successor]["plain"] or found[successor]["title"]))
            matches = [_impact(group, target, old_terms, suggestions, source_version) for target in targets]
            matches = sorted((m for m in matches if m), key=lambda m: m["relation"] != "direct")
            if len(matches) > MAX_RESULTS:
                report("Each decision shows up to %d current targets." % MAX_RESULTS)
            group["items"] = matches[:MAX_RESULTS]
            groups.append(group)
    events = decision_review.read(db, repo, builder.hub, allowed) if builder.hub and groups else []
    decision_review.attach(groups, events)
    result["groups"] = [g for g in groups if g["items"] or g["review_history"] or g["conflict"]]
    result["partial"] = bool(coverage)
    return result

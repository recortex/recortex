"""Finite sorting, drafting and fresh review. Unreviewed source/draft text stays in memory.

A pass is three kinds of call. The sorter (the sort tier) labels a stretch of the conversation with the jobs it holds and
the messages each rests on, and writes no record text. The drafter (the middle tier) writes the candidates the labels
point at, and a fresh reviewer judges each one. Code checks every label and every candidate (label_problem,
candidate_problem) before a call is spent on it. Messages and plan parts are cited by short numbers, never by their
64-character ids; the worker maps the numbers back."""
import hashlib
import datetime
import json
import os
import re
import signal
import sqlite3
import subprocess
import time
import unicodedata

import index
import mind_model
import observer_context
import observer_holds
import observer_jobs
import observer_keeper
import observer_lease
import observer_publisher
import lint_drafts
import names
import observer_runtime as runtime
import observer_sources as sources
import observer_store
import recortex
import record_format
import storage

JOBS = ("memory", "plan_agreed", "step_done", "to_do", "paper_part")
# The jobs each level lets the pass draft. A label whose job is not in the level costs the sort call and no more.
LEVEL_JOBS = {"notes": ("memory",), "assistant": JOBS, "everything": JOBS}
KINDS = ("result", "decision", "failure", "correction", "idea", "question", "checkpoint", "steps")
LABEL_KINDS = ("result", "decision", "failure", "correction", "idea", "question", "none")
MARK_KINDS = ("result", "failure", "correction", "checkpoint")
MAX_SOURCES = 16  # queued observations offered to one sort call
MAX_PARTS = 60  # plan parts shown to the sorter and the drafter
MAX_STEPS = 20
STRING = {"type": "string", "maxLength": 24000}
ID = {"type": "string", "pattern": "[0-9a-f]{64}"}
ORDINAL = {"type": "integer", "minimum": 1, "maximum": MAX_SOURCES}
PART_NUMBER = {"type": "integer", "minimum": 1, "maximum": MAX_PARTS}
STEP_NUMBER = {"type": "integer", "minimum": 1, "maximum": MAX_STEPS}
TASK_NUMBER = {"type": "integer", "minimum": 0, "maximum": 100000}
SORT = {"type": "object", "properties": {
    "labels": {"type": "array", "maxItems": 24, "items": {"type": "object", "properties": {
        "job": {"type": "string", "enum": list(JOBS)},
        "kind": {"type": "string", "enum": list(LABEL_KINDS)},
        "sources": {"type": "array", "items": ORDINAL, "minItems": 1, "maxItems": 8},
        "task": TASK_NUMBER,
        "steps": {"type": "array", "items": STEP_NUMBER, "maxItems": MAX_STEPS},
        "parts": {"type": "array", "items": PART_NUMBER, "maxItems": 8}},
        "required": ["job", "kind", "sources", "task", "steps", "parts"], "additionalProperties": False}},
    "ignored": {"type": "array", "items": ORDINAL, "maxItems": MAX_SOURCES}},
    "required": ["labels", "ignored"], "additionalProperties": False}
MARK = {"type": "object", "properties": {"task": TASK_NUMBER,
                                         "steps": {"type": "array", "items": STEP_NUMBER, "minItems": 1,
                                                   "maxItems": MAX_STEPS}},
        "required": ["task", "steps"], "additionalProperties": False}
LINK_KEYS = ("checks", "against")
LINK = {"type": "object", "properties": {"key": {"type": "string", "enum": list(LINK_KEYS)}, "part": PART_NUMBER},
        "required": ["key", "part"], "additionalProperties": False}
EXCERPTS = {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 8}
# What the drafter returns: messages and plan parts are numbers.
CANDIDATE = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": list(KINDS)}, "issue": {"type": "integer", "minimum": 0},
    "task_title": {"type": "string", "maxLength": 120}, "topic": {"type": "integer", "minimum": 0},
    "body": STRING, "sources": {"type": "array", "items": ORDINAL, "minItems": 1, "maxItems": 8},
    "excerpts": EXCERPTS, "marks": {"type": "array", "items": MARK, "maxItems": 6},
    "links": {"type": "array", "items": LINK, "maxItems": 4},
    "because": {"type": "integer", "minimum": 0, "maximum": 6}},   # optional: a decision's failure, by its place in the list
    "required": ["kind", "issue", "task_title", "topic", "body", "sources", "excerpts", "marks", "links"],
    "additionalProperties": False}
DRAFT = {"type": "object", "properties": {"candidates": {"type": "array", "items": CANDIDATE, "maxItems": 6}},
         "required": ["candidates"], "additionalProperties": False}
# What the queue keeps: the real source ids, and a plan part by the comment id of its plan memory. task_title and topic
# only for a steps memory that opens a task (issue 0); with, the first source of the steps memory that opens the task a
# mark of task 0 is about; because_with, the key of the event of the failure of the same pass that a decision was made
# because of (the drafter says it by the failure's place in its list; code looks it up and writes the link).
STORED = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": list(KINDS)}, "issue": {"type": "integer", "minimum": 0},
    "task_title": {"type": "string", "maxLength": 120}, "topic": {"type": "integer", "minimum": 0},
    "body": STRING, "sources": {"type": "array", "items": ID, "minItems": 1, "maxItems": 8},
    "excerpts": EXCERPTS, "marks": {"type": "array", "items": MARK, "maxItems": 6},
    "links": {"type": "array", "items": {"type": "object", "properties": {
        "key": {"type": "string", "enum": list(LINK_KEYS)}, "part": {"type": "integer", "minimum": 1}},
        "required": ["key", "part"], "additionalProperties": False}, "maxItems": 4},
    "with": ID, "because_with": {"type": "string", "pattern": "[0-9a-f]{32}"}},
    "required": ["kind", "issue", "body", "sources", "excerpts", "marks", "links"], "additionalProperties": False}
REVIEW = {"type": "object", "properties": {
    "candidate_hash": ID, "accepted": {"type": "boolean"},
    "reason": {"type": "string", "enum": ["supported", "unsupported", "attribution", "privacy", "duplicate", "uncertain",
                                          "withheld", "marks", "not_agreed"]}},
    "required": ["candidate_hash", "accepted", "reason"], "additionalProperties": False}
MIXED_REVIEW = dict(REVIEW, properties=dict(REVIEW["properties"], qualification={
    "type": "string", "enum": ["separate_action", "same_decision", "unclear"]}),
    required=REVIEW["required"] + ["qualification"])


SUMMARY = {"type": "object", "properties": {
    "paragraphs": {"type": "array", "items": {"type": "string", "maxLength": 1200}, "minItems": 1, "maxItems": 2}},
    "required": ["paragraphs"], "additionalProperties": False}
SUMMARY_REVIEW = {"type": "object", "properties": {
    "summary_hash": ID, "accepted": {"type": "boolean"},
    "reason": {"type": "string", "enum": ["supported", "unsupported", "numbers", "privacy", "form"]}},
    "required": ["summary_hash", "accepted", "reason"], "additionalProperties": False}


def privacy_problem(text, config):
    findings = lint_drafts.lint_text(text, kind="generic", config=config)
    return any(f.level == "error" and f.code in ("secret", "restricted") for f in findings)


def source_privacy_problem(value, config):
    """Check original string values, preserving tabs/anchors lost in JSON escaping."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and privacy_problem(item, config):
            return True
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def record_context(repo, config, query=""):
    """Task-ranked trusted destinations and intact evidence groups from the checked index."""
    db = index.connect(repo, create=False)
    if db is None:
        return None
    try:
        allowed = recortex.allowed_users(repo)
        if not allowed or not index.mind_ready(db):
            return None
        probe = mind_model.Build(db, repo, {name.lower() for name in allowed}, None)
        probe.read_issues()
        wanted = observer_context.tokens(query)
        ranked = []
        for number, issue in probe.issues.items():
            if (issue["state"] != "open" or issue["pull"] or not probe.has_label(number, "final-record")
                    or not probe.body_shown(number) or not probe.title_shown(number)):
                continue
            score = len(wanted & observer_context.tokens(issue["title"] + " " + issue["body"]))
            if score or not wanted:
                ranked.append((score, number, issue))
        targets, used, omitted = [], 0, 0
        for _, number, issue in sorted(ranked, key=lambda item: (-item[0], item[1])):
            target = {"issue": number, "title": issue["title"], "body": issue["body"]}
            size = len(runtime.canonical(target))
            if used + size > 6000 or len(targets) >= 8:
                omitted += 1
                continue  # Never truncate a destination's constraints mid-claim.
            targets.append(target); used += size
        packet = observer_context.build_packet(db, repo, allowed, query=query, max_chars=8000)
        return {"targets": targets, "memories": [{"id": cid, "kind": packet["selected_kinds"][cid]} for cid in packet["selected"]],
                "context": packet["text"], "recall_incomplete": packet["incomplete"],
                "omitted_targets": omitted, "synced_at": packet["freshness"]["synced_at"],
                "incomplete": not packet["freshness"]["sync_complete"]}
    finally:
        db.close()


RECENT_ON_ISSUE = 8  # the newest memories on a candidate's issue the reviewer sees, to reject a repeat


def recent_on_issue(repo, issues):
    """{issue: the newest memories on it by allowed accounts (id, date, kind, plain line or title), at most
    RECENT_ON_ISSUE, checkpoints aside}, for the reviewer to reject a repeat. {} when the index cannot be read: the
    reviewer then judges without it."""
    db = index.connect(repo, create=False)
    if db is None:
        return {}
    try:
        allowed = {name.lower() for name in recortex.allowed_users(repo)}
        found = list(index.memories(db, allowed)["items"].values()) if allowed else []
        out = {}
        for issue in issues:
            mine = sorted((m for m in found if m["issue"] == issue and m["kind"] != "checkpoint"),
                          key=lambda m: (m["time"], m["id"]), reverse=True)[:RECENT_ON_ISSUE]
            out[issue] = [{"id": m["id"], "at": (m["at"] or "")[:16], "kind": m["kind"],
                           "said": m["plain"] or m["title"]} for m in mine]
        return out
    finally:
        db.close()


def template_context():
    base = os.path.join(os.path.dirname(__file__), "..", "templates")
    templates = {}
    for kind in KINDS:
        with open(os.path.join(base, "comment-" + kind + ".md"), encoding="utf-8") as handle:
            templates[kind] = handle.read().split("<!-- Optional parts", 1)[0].strip()
    return templates


# ---------------------------------------------------------------- what the sorter and the drafter see of the record

TASKS_SHOWN, MEMORIES_SHOWN, LINE = 10, 12, 160
RECORD_LIMIT = 9000   # characters of the sorter's record beyond the topics; a larger one is trimmed, newest kept
LABELS_ROOM = 3000    # what the labels may take of the drafter's call
MIN_ROOM = 8000       # the room a call keeps for the stretch before the recalled memories are left out of the drafter's


def _line(text, limit=LINE):
    return mind_model.short(mind_model.clean(text or ""), limit)


def index_record(repo, config):
    """What the sorter and the drafter see of the checked index beside the topics: the open tasks with their open steps
    (numbered across the task's standing steps memories), the number of the standing "To do" task, the plan's standing
    parts and the newest told memories. {} when the index cannot be read; the parts then are none and a label that
    names one is dropped."""
    db = index.connect(repo, create=False)
    if db is None:
        return {}
    try:
        allowed = recortex.allowed_users(repo)
        if not allowed or not index.mind_ready(db):
            return {}
        report = index.tasks(db, allowed)
        tasks = []
        for task in report["tasks"]:
            steps = [{"n": step["n"], "text": _line(step["text"])} for step in task["steps"]
                     if step["state"] in ("open", "blocked")]
            if task["state"] == "open" and not task["todo"] and steps:
                tasks.append({"issue": task["issue"], "title": _line(task["title"] if task["title_told"] else "", 120),
                              "steps": steps})
        state = index.planned(db, config, allowed)
        found = state["found"] if state else index.memories(db, allowed)
        items = found["items"]
        parts = []
        if state:
            standing = [(items[cid]["time"], cid, fields) for cid, fields in state["parts"].items()
                        if fields["standing"] and cid in items]
            for _, cid, fields in sorted(standing, key=lambda row: (row[0], row[1])):
                parts.append({"id": cid, "kind": fields["part"], "plain": _line(items[cid]["plain"]),
                              "state": fields["state"]})
        newest = sorted(items.values(), key=lambda m: (m["time"], m["id"]), reverse=True)[:MEMORIES_SHOWN]
        return {"tasks": sorted(tasks, key=lambda task: task["issue"]), "todo": report["todo"] or 0, "parts": parts,
                "memories": [{"kind": m["kind"], "plain": _line(m["plain"] or m["title"])} for m in newest]}
    finally:
        db.close()


def fit_record(extra, limit=RECORD_LIMIT):
    """`extra` (index_record) trimmed until its size is at most `limit`: the older memories first, then the plan's later
    parts, then each task's later steps and the later tasks. Numbers are positions, so a trimmed list keeps its
    numbers."""
    extra = {"tasks": [dict(t, steps=list(t["steps"])) for t in extra.get("tasks", [])[:TASKS_SHOWN]],
             "todo": extra.get("todo", 0), "parts": list(extra.get("parts", []))[:MAX_PARTS],
             "memories": list(extra.get("memories", []))[:MEMORIES_SHOWN]}
    steps = [MAX_STEPS, 10, 5]
    for stage in range(6):
        shown = len(runtime.canonical([extra["tasks"], [dict(p, id=0) for p in extra["parts"]], extra["memories"]]))
        if shown <= limit:
            break
        if stage == 0:
            extra["memories"] = extra["memories"][:6]
        elif stage == 1:
            extra["parts"] = extra["parts"][:30]
        elif stage == 2:
            for task in extra["tasks"]:
                task["steps"] = task["steps"][:steps[1]]
        elif stage == 3:
            extra["memories"] = []
            extra["tasks"] = extra["tasks"][:5]
        elif stage == 4:
            for task in extra["tasks"]:
                task["steps"] = task["steps"][:steps[2]]
            extra["parts"] = extra["parts"][:15]
    return extra


def sort_record(context, extra):
    """(record, aux): the record the sorter and the drafter are shown beside a stretch, and what code checks labels and
    candidates against, which the models never see: the open tasks and their open step numbers, the To do task, and the
    plan part numbers with the comment ids of their plan memories."""
    topics = [{"issue": target["issue"], "title": _line(target["title"], 120)} for target in context["targets"]]
    record = {"topics": topics, "tasks": extra["tasks"], "todo": extra["todo"],
              "plan": [{"n": n, "kind": part["kind"], "plain": part["plain"], "state": part["state"]}
                       for n, part in enumerate(extra["parts"], 1)],
              "memories": extra["memories"]}
    aux = {"tasks": {task["issue"]: {"title": task["title"], "steps": {step["n"]: step["text"] for step in task["steps"]}}
                     for task in extra["tasks"]},
           "todo": extra["todo"], "parts": {n: part["id"] for n, part in enumerate(extra["parts"], 1)},
           "topics": {topic["issue"] for topic in topics}}
    return record, aux


MIN_WORDS = 6  # a shorter typed message ("go ahead", "this is fine") cannot back a change of status
APPROVED = "Approved the plan in the plan dialog."


def _main_agent(source):
    """The main session's agent writing to the user: an assistant record of the main transcript, not a subagent's or a
    sidechain's (observer_sources drops meta, compaction and transcript-only records before this)."""
    return source.get("role") == "assistant" and source.get("subagent") is False and source.get("sidechain") is False


def _plain_agent(source):
    """A message of the main session's agent that the agent wrote as a message, not a plan or a question it showed in a
    dialog: what a result, a failure or a mark rests on."""
    return _main_agent(source) and not source.get("via")


def _typed(source):
    """A prompt the user typed."""
    return source.get("role") == "user" and source.get("direct_user") is True


def _choice(source):
    """The user's answer to a dialog: approved or rejected a plan, or chose an option. Weaker than typed words."""
    return source.get("role") == "user" and source.get("dialog_user") is True


def _said(source):
    """The conversation the observer takes notes of: a prompt the user typed, an answer the user gave in a dialog, or the
    main session's agent writing to the user. Nothing else reaches the sorter, the drafter or the reviewer."""
    return _typed(source) or _choice(source) or _main_agent(source)


def _users(candidate, supplied):
    return [source["text"] for sid, source in supplied.items() if sid in candidate["sources"] and _typed(source)]


def _agent(candidate, supplied):
    """True when the candidate cites the agent's own message to the user."""
    return any(_plain_agent(supplied[source]) for source in candidate["sources"])


def _quoted(candidate):
    """The user's words the memory quotes for itself: a decision's Exact words, or the quoted lines of another kind."""
    if candidate["kind"] == "decision":
        return observer_publisher.exact_words(candidate["body"]).strip()
    return "\n".join(line.lstrip()[1:].strip() for line in candidate["body"].splitlines()
                     if line.lstrip().startswith(">")).strip()


# Read the complete cited user message so an excerpt cannot discard a scope qualifier.
LIMITED_SCOPE = re.compile(
    r"\b(?:what if|just exploring|not (?:yet )?decided|undecided|haven't decided|"
    r"(?:just |only )?(?:for|during) (?:this|one|a single) (?:run|trial|experiment|session|test)|"
    r"(?:this|one) (?:run|trial|session) only|just this once|(?:as a |a )one[- ]off|for now|"
    r"(?:try|test) [^.!?\n]{0,75}\bonce|(?:do not|don['’]t|never) change (?:the |our )?default|"
    r"keep (?:our |the )?(?:old |existing )?default|haven['’]t settled|let['’]s explore)\b", re.I)


def standing_source(quote, messages):
    # The worker supplies messages in transcript order. A later qualification takes precedence.
    for text in reversed(messages):
        if quote in text:
            return not LIMITED_SCOPE.search(text) or separate_scope(quote, text)
        if LIMITED_SCOPE.search(text):
            return False
    return False


def separate_scope(quote, text):
    """Let the independent reviewer assess distinct statements in a fully visible source. A clipped quote,
    an attached qualification or a bare amendment such as 'For this run only.' keeps the ordinary veto."""
    if len(text) > SOURCE_TEXT or text.count(quote) != 1:
        return False
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    for n, sentence in enumerate(sentences):
        if sentence.rstrip(".!") == quote.strip().rstrip(".!") and not sentence.endswith("?"):
            decision = n
            break
    else:
        return False
    for n, sentence in enumerate(sentences):
        limit = LIMITED_SCOPE.search(sentence)
        if not limit:
            continue
        if n == decision or limit.start() != 0 or LIMITED_SCOPE.search(sentence, limit.end()):
            return False
        if sentence.endswith("?") and limit.group().lower() == "what if":
            continue
        # A leading time limit with its own instruction can govern a separate action. A bare limit,
        # an undecided answer or an instruction to keep the default still qualifies the choice.
        instruction = sentence[limit.end():]
        if not re.match(r"(?:for|during)\b", limit.group(), re.I) or not re.match(
                r"\s*(?:only\s*)?[,:—]\s*\w+\s+\w+", instruction, re.I):
            return False
    return True


def review_schema(candidate, supplied, context):
    """Mixed scope needs an explicit finding from the independent reviewer even when it accepts the draft."""
    if candidate["kind"] == "decision" or replaces_standing_decision(candidate, context):
        quote = _quoted(candidate)
        if quote and any(quote in text and LIMITED_SCOPE.search(text) for text in decision_messages(candidate, supplied)):
            return MIXED_REVIEW
    return REVIEW


def decision_messages(candidate, supplied):
    selected = set(candidate["sources"])
    seen_sessions = set()
    messages = []
    for sid, source in supplied.items():
        text = source["text"]
        typed = _typed(source)
        if sid in selected and (typed or (_choice(source) and source.get("via") == "question_dialog")):
            messages.append(text)
            seen_sessions.add(source.get("session_id"))
        elif typed and source.get("session_id") in seen_sessions and re.match(
                r"^\s*(?:(?:actually|sorry|correction)[,:]?\s*)?(?:just |only )?(?:for (?:this|one) "
                r"(?:run|trial|session)(?: only)?|this (?:run|trial|session) only)[.!]?\s*$", text, re.I):
            # A short correction in the same stretch still qualifies the statement when the draft omits its id.
            messages.append(text)
    return messages


def replaces_standing_decision(candidate, context):
    if candidate["kind"] not in ("decision", "correction"):
        return False
    marker = record_format.parse_marker(record_format.first_lines(candidate["body"])[0])
    protected = {m["id"] for m in context["memories"] if m.get("kind") in ("decision", "correction")}
    return any(key == "replaces" and (candidate["kind"] == "decision" or target in protected)
               for key, target in record_format.links(marker))


def status_authority(candidate, supplied, requires_user=False):
    """What a memory that changes an earlier one's status rests on, all of it said in the conversation. "user": the
    memory quotes, in its own field (a decision's Exact words, another kind's quoted lines), at least MIN_WORDS words
    and at least half of one typed message it cites, so that the user's words back this very change. "agent": the
    agent's message to the user, cited. "" otherwise (a short phrase such as "go ahead" on its own, a long message
    the memory does not quote, or an answer in a dialog, which is weaker than typed words): it then waits for the main
    session."""
    if not all(_said(supplied[source]) for source in candidate["sources"]):
        return ""
    quoted = _quoted(candidate)
    if len(quoted.split()) >= MIN_WORDS and any(quoted in text and 2 * len(quoted) >= len(text.strip())
                                               for text in _users(candidate, supplied)):
        return "user"
    return "agent" if not requires_user and _agent(candidate, supplied) else ""


# ---------------------------------------------------------------- the sorter's labels, checked by code

def label_problem(label, by_n, aux, plan_agreed=False):
    """The fixed reason a sorter's label is dropped (the rows of spec 4.3), or None. `by_n` is {message number: source},
    `aux` what sort_record kept (open tasks, plan parts), `plan_agreed` whether the same output holds a plan_agreed label
    that stands (which lets step_done name task 0, the task that plan opens)."""
    cited = label["sources"]
    if not cited or len(set(cited)) != len(cited) or any(n not in by_n for n in cited):
        return "ordinal"
    messages = [by_n[n] for n in cited]
    job = label["job"]
    if job == "memory":
        if label["kind"] == "none":
            return "kind"
        if label["kind"] == "decision" and not any(
                _typed(s) or (_choice(s) and s.get("via") == "question_dialog") for s in messages):
            return "decision"
    elif job == "to_do":
        if not any(_typed(s) for s in messages):
            return "to_do"
    elif job == "plan_agreed":
        if not any(_typed(s) or _choice(s) for s in messages) or not any(_main_agent(s) for s in messages):
            return "plan_agreed"
    elif job == "step_done":
        if not label["steps"] or not (label["task"] in aux["tasks"] or (label["task"] == 0 and plan_agreed)):
            return "task"
        if not any(_plain_agent(s) for s in messages):
            return "step_done"
    elif job == "paper_part":
        if not label["parts"] or any(part not in aux["parts"] for part in label["parts"]):
            return "paper_part"
    return None


def check_labels(labels, by_n, aux, level):
    """(kept, dropped): the labels that stand and {reason: count} of those that do not. A label whose job is not in the
    level is dropped before anything else, so that no draft is spent on it."""
    allowed = LEVEL_JOBS.get(level, ())
    dropped = {}

    def refuse(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    agreed = any(label["job"] == "plan_agreed" and label["job"] in allowed
                 and label_problem(label, by_n, aux) is None for label in labels)
    kept = []
    for label in labels:
        if label["job"] not in allowed:
            refuse("level")
            continue
        problem = label_problem(label, by_n, aux, plan_agreed=agreed)
        if problem:
            refuse(problem)
        else:
            kept.append(label)
    return kept, dropped


# ---------------------------------------------------------------- the drafter's candidates, checked by code

def _flat(text):
    return " ".join((text or "").split())


TITLE_MAX = 120
TITLE_SHAPES = re.compile(r"[<>`|\\]|!\[|\]\(|@|#[0-9]|://|www\.", re.ASCII)


def title_problem(title, config, repo):
    """Why the title a model gave a new task is refused, or None. A title is model text that GitHub shows as an issue's
    name, so it goes through what a body goes through: no secret and none of the project's restricted patterns
    ("privacy"), nothing of this machine, no address, mention, markup or reference (outbound_problem), no banned word
    ("title"); and it must be one plain line of at most TITLE_MAX characters, with no control or bidi character."""
    if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
        return "title"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in title) or TITLE_SHAPES.search(title):
        return "title"
    if privacy_problem(title, config):
        return "privacy"
    if observer_publisher.outbound_problem(title, repo, status_ok=True):
        return "title"
    if any(f.level == "error" for f in lint_drafts.lint_text(title, kind="generic", config=config)):
        return "title"
    return None


def _agreed(body):
    """(form, quote) of a steps memory's Agreed field: the form as record_format.agreed_form reads it ("dialog", "chat" or
    None) and the user's quoted words, whitespace collapsed."""
    text = record_format.field_text(body, "Agreed") or ""
    quote = _flat(" ".join(line.lstrip()[1:] for line in text.split("\n")[1:] if line.lstrip().startswith(">")))
    return record_format.agreed_form(text), quote


def _steps_lines_ok(body):
    """True when the Steps field holds 1 to MAX_STEPS lines that each are one numbered step: no longer step, no stray line."""
    lines = [line.strip() for line in (record_format.field_text(body, "Steps") or "").split("\n") if line.strip()]
    return 1 <= len(lines) <= MAX_STEPS and all(record_format.STEP_LINE.match(line) for line in lines)


def _steps_problem(candidate, supplied, role):
    """The refusal of a steps candidate (the rows steps-authority, steps-form and todo-authority of spec 4.5), or None."""
    cited = [supplied[sid] for sid in candidate["sources"]]
    form, quote = _agreed(candidate["body"])
    typed_quote = bool(quote) and any(quote in _flat(source["text"]) for source in cited if _typed(source))
    approved = any(s["role"] == "user" and s.get("via") == "plan_dialog" and s["text"] == APPROVED for s in cited)
    chosen = any(_choice(s) and s.get("via") == "question_dialog" for s in cited)
    if role == "todo":
        if form != "chat" or not typed_quote:
            return "todo-authority"
    else:
        shown = any(_main_agent(s) for s in cited)
        if not shown or not (approved or chosen or typed_quote):
            return "steps-authority"
        if form == "dialog" and not approved:
            return "steps-form"
        if form == "chat" and not typed_quote:
            return "steps-form"
    if form is None or not _steps_lines_ok(candidate["body"]):
        return "steps-form"
    return None


def candidate_problem(candidate, supplied, context, config, repo=None, plan=None):
    """Why a drafted candidate is not offered to a reviewer, a fixed reason, or None. `supplied` is {source id: source};
    `plan` is what code knows of the record's tasks and plan parts and of the stretch (level, tasks, parts, role, new_steps),
    and is left out by a caller that has none (everything is then allowed)."""
    plan = plan or {}
    if not runtime.validate(candidate, STORED):
        return "candidate-schema"
    selected = candidate["sources"]
    if len(set(selected)) != len(selected) or not set(selected) <= set(supplied):
        return "source-binding"
    kind, marks, links = candidate["kind"], candidate["marks"], candidate["links"]
    allowed = LEVEL_JOBS.get(plan.get("level", "everything"), ())
    if (kind == "steps" and not {"plan_agreed", "to_do"} & set(allowed)) or (marks and "step_done" not in allowed) \
            or (links and "paper_part" not in allowed):
        return "level"
    if "task_title" in candidate:
        problem = title_problem(candidate["task_title"], config, repo or config.get("repo", ""))
        if problem:
            return problem
    targets = {target["issue"] for target in context["targets"]}
    if kind == "steps":
        role = plan.get("role") or ("task" if candidate.get("task_title") else "todo")
        if role == "task":
            if candidate["issue"] != 0 or candidate.get("topic") not in targets or not candidate.get("task_title", "").strip():
                return "destination"
        elif candidate["issue"] not in (0, plan.get("todo") or 0):
            return "destination"
    elif kind == "checkpoint" and marks:
        tasks = {mark["task"] for mark in marks}
        if len(tasks) != 1 or candidate["issue"] not in tasks:
            return "mark-binding" if len(tasks) != 1 else "destination"
    elif candidate["issue"] not in targets:
        return "destination"
    event = record_format.parse_event(candidate["body"])
    if event["type"] != kind or event.get("reconstructed"):
        return "candidate-event"
    if privacy_problem(candidate["body"] + "\n" + "\n".join(candidate["excerpts"]), config):
        return "privacy"
    # Images, HTML, @mentions and outside addresses wait for the main session; a change of status is checked when
    # the event is posted, against what it rests on (status_authority).
    outbound = observer_publisher.outbound_problem(candidate["body"], repo or config.get("repo", ""), status_ok=True)
    if outbound:
        return outbound
    if any(not any(excerpt in supplied[source]["text"] for source in selected) for excerpt in candidate["excerpts"]):
        return "excerpt-binding"
    if kind == "decision":
        quoted = observer_publisher.exact_words(candidate["body"])
        messages = decision_messages(candidate, supplied)
        if not quoted or not any(quoted in supplied[sid]["text"] for sid in selected
                                 if _typed(supplied[sid]) or (_choice(supplied[sid])
                                                             and supplied[sid].get("via") == "question_dialog")):
            return "decision-authority"
        if not standing_source(quoted, messages):
            return "decision-scope"
    if kind in ("result", "failure") and not _agent(candidate, supplied):
        return "agent-account"  # a result or a failure is what the agent told the user it found
    marker = record_format.parse_marker(record_format.first_lines(candidate["body"])[0])
    if kind == "correction" and replaces_standing_decision(candidate, context):
        quoted = _quoted(candidate)
        messages = decision_messages(candidate, supplied)
        if quoted and any(quoted in text for text in messages) and not standing_source(quoted, messages):
            return "decision-scope"
    known = {m["id"] for m in context["memories"]}
    if any(target not in known for _, target in record_format.links(marker)):
        return "link-binding"
    if kind == "steps":
        problem = _steps_problem(candidate, supplied, role)
        if problem:
            return problem
    if marks:
        if kind not in MARK_KINDS:
            return "mark-kind"
        if not _agent(candidate, supplied):
            return "mark-authority"
        known_tasks = plan.get("tasks", {})
        for mark in marks:
            if mark["task"] == 0:
                count = plan.get("new_steps")
                if not count or any(step > count for step in mark["steps"]):
                    return "mark-binding"
            elif mark["task"] not in known_tasks or any(step not in known_tasks[mark["task"]]["steps"]
                                                       for step in mark["steps"]):
                return "mark-binding"
    if links:
        parts = plan.get("parts", set())
        for link in links:
            if link["part"] not in parts or kind == "checkpoint" or (link["key"] == "against" and kind not in ("result", "failure")):
                return "part-binding"
    # Local structural checks run before review; record lookups and posting checks run in the publisher.
    findings = lint_drafts.lint_text(candidate["body"], kind=kind, config=config)
    if any(f.level == "error" for f in findings):
        return "candidate-lint"
    return None


SOURCE_TEXT = 6000  # characters of one source a model sees; a longer one keeps its start and its end
RECORD_ROOM = 18000  # what the record context may take of a call (destinations, the task packet and their quoting)
REVIEW_CONTEXT = 12000  # characters of the rest of the stretch a reviewer is shown beside a candidate's cited messages
# Deferrals tried again at the next pass, up to this many attempts in all: the pass was full (input_limit,
# candidate_limit); the sorter neither used nor set aside the source (uncertain), often because what it belongs with
# had not happened yet; or the drafter's candidate failed a mechanical check. Each attempt is counted in the reason
# ("uncertain_2"); a source that has had its attempts stays deferred, in `pending`, for `record --source`.
RETRY_SOON = {"input_limit": 6, "candidate_limit": 6, "uncertain": 3, "candidate_lint": 2, "excerpt_binding": 2,
              "agent_account": 2, "decision_authority": 2, "outbound_local": 2, "outbound_address": 2,
              "link_binding": 2, "candidate_event": 2, "destination": 2, "steps_authority": 2, "steps_form": 2,
              "todo_authority": 2, "mark_authority": 2, "mark_binding": 2, "mark_kind": 2, "part_binding": 2}
# An uncertain source is offered again once its session has gone on (new sources of that session are in the pass), or
# after five minutes; the others in RETRY_SOON at the next pass.
WAITING = ("uncertain",)
# Deferrals tried again after five minutes: a failed call, the budget, the record. extract_failed is the reason of the
# 1.4.0 call and is kept so that a source it left deferred is still tried.
RETRY_LATER = ("budget", "sort_failed", "draft_failed", "extract_failed", "review_failed", "worker_budget",
               "record_unavailable")
# Failed calls are tried again after five minutes, at most this many times for a source shown alone; a failed batch is
# offered again in halves, so that a source the model cannot answer for is found in a few calls and stops there.
RETRY_COUNTED = dict(RETRY_SOON, sort_failed=3, draft_failed=3, extract_failed=3, review_failed=3)
# Failures of a call that say nothing about its input: the batch is kept whole and tried again after five minutes.
TRANSIENT = frozenset(("timeout", "interrupted", "runtime-unavailable", "runtime-gate", "recursion", "model-changed", "isolation",
                       "level"))
# A pass waits for at least this many sources, or for one that has waited ten minutes, so that a call sees a stretch
# of the session and the day's calls last a working day.
ENOUGH, PATIENCE = 8, 600


def _attempts(reason):
    """(kind, attempts so far) of a deferral reason: "uncertain_2" is the second uncertain pass of a source."""
    kind, _, count = (reason or "").rpartition("_")
    if kind in RETRY_COUNTED and count.isdigit():
        return kind, int(count)
    return reason or "", 1


def _again(kind, previous=""):
    """The reason for one more deferral as `kind`, counting the automatic attempts the source has had."""
    base, count = _attempts(previous)
    count = count + 1 if base in RETRY_COUNTED else 1
    return kind if count == 1 else "%s_%d" % (kind, count)


def _order(item):
    meta = item["metadata"]
    return (meta.get("observed_at", 0), meta.get("path", ""), meta.get("offset", 0), item["source_id"])


def _queued(store):
    """(items, half): the observations offered to one pass, oldest first: deferrals due again soon, new ones and older
    failures, with places kept for each so that none starves the others. The halves of a failed batch come first, one
    half a pass and alone (half True), so that a source the model cannot answer for is found in a few calls."""
    batches = store.get_control("retry_halves", [])
    while isinstance(batches, list) and batches:
        batch = batches.pop(0)
        items = [store.observation(sid) for sid in batch if isinstance(sid, str)] if isinstance(batch, list) else []
        items = [item for item in items if item and item["state"] == "deferred"]
        if items:
            store.set_control("retry_halves", batches)
            return sorted(items, key=_order), True
    if batches != []:
        store.set_control("retry_halves", [])
    now = time.time()
    fresh = sorted((store.observation(row[0]) for row in store.db.execute(
        "SELECT source_id FROM observations WHERE state='observed' ORDER BY created,source_id LIMIT 64")), key=_order)
    going_on = {item["metadata"].get("session_id") for item in fresh}
    soon, later = [], []
    rows = store.db.execute(
        "SELECT source_id,reason,updated FROM observations WHERE state='deferred' AND (reason IN (%s) OR %s) "
        "ORDER BY created,source_id LIMIT 256" % (",".join("?" * len(RETRY_LATER)),
                                                  " OR ".join("reason LIKE ?" for _ in RETRY_COUNTED)),
        RETRY_LATER + tuple(kind + "%" for kind in RETRY_COUNTED)).fetchall()
    for sid, reason, updated in rows:
        kind, count = _attempts(reason)
        if kind in RETRY_SOON and count < RETRY_SOON[kind]:
            item = store.observation(sid)
            if kind not in WAITING or item["metadata"].get("session_id") in going_on or updated < now - 300:
                soon.append(item)
        elif (kind in RETRY_LATER and (kind not in RETRY_COUNTED or count < RETRY_COUNTED[kind])
              and updated < now - 300):
            later.append(store.observation(sid))
    soon.sort(key=_order)
    chosen = soon[:6] + fresh[:7] + later[:3]
    chosen += [item for item in fresh[7:] + soon[6:] + later[3:] if item not in chosen][:MAX_SOURCES - len(chosen)]
    return chosen[:MAX_SOURCES], False


def _view(source, n):
    """What a model is shown of one source: its number in the stretch, who said it, when, and its text (a long text keeps
    its start and its end, marked). The worker's checks use the whole source. No 64-character id is shown."""
    text = source["text"]
    if len(text) > SOURCE_TEXT:
        text = text[:4000] + "\n[... %d characters left out ...]\n" % (len(text) - 5500) + text[-1500:]
    view = {"n": n, "role": source["role"], "via": source.get("via") or "", "direct_user": source.get("direct_user") is True,
            "at": source.get("at"), "text": text}
    lifted = observer_store.clean_quote(source.get("hold_released"), observer_store.QUOTES_MAX)
    if lifted:
        view["hold_released"] = lifted   # only on a source a hold of this session the user released offered again: its words
    return view


def _stretch(candidate, supplied, order, number):
    """The other messages of the candidate's sessions in this pass, in order, as the reviewer's context: what else was
    said around the cited messages (the command the user named, the commit the agent gave). Bounded by REVIEW_CONTEXT;
    status_authority still rests on the cited messages alone."""
    cited = set(candidate["sources"])
    sessions = {supplied[sid].get("session_id") for sid in cited}
    out, used = [], 0
    for sid in sorted(supplied, key=lambda key: order[key]):
        if sid in cited or supplied[sid].get("session_id") not in sessions:
            continue
        view = _view(supplied[sid], number[sid])
        size = len(runtime.canonical(view))
        if used + size <= REVIEW_CONTEXT:
            out.append(view)
            used += size
    return out


def _account(repo):
    """The account a decision's words came from, when the record allows only one: the publisher can post as no other,
    and on one machine with one account it is the person typing. "" otherwise."""
    allowed = recortex.allowed_users(repo)
    return next(iter(allowed)) if len(allowed) == 1 else ""


def _zone(config):
    """(zone, offset) of the record's time zone now, as the drafter writes a time: ("EST", "-05:00")."""
    now = datetime.datetime.now(datetime.timezone.utc)
    zone = recortex.stamp(now, config).split(" ")[-1]
    match = re.search(r"([+-][0-9]{2}:[0-9]{2}|Z)$", recortex.marker_time(now, config))
    return zone, ("+00:00" if not match or match.group(1) == "Z" else match.group(1))


def _batch(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode("ascii")).hexdigest()[:32]


def _failed(store, offered, result, kind):
    """A call of the pass failed: counted only for a source shown alone; a larger batch is offered again in halves, so
    that a source the model cannot answer for is found in a few calls (RETRY_COUNTED). A transient failure keeps it
    whole."""
    if not offered:
        return
    alone = len(offered) == 1
    for item in offered:
        previous = item["reason"] if item["state"] == "deferred" else ""
        kept = previous if _attempts(previous)[0] == kind else kind
        store.defer(item["source_id"], _again(kind, previous) if alone else kept)
    if not alone and result.get("code") not in TRANSIENT:
        ids, middle = [item["source_id"] for item in offered], len(offered) // 2
        batches = store.get_control("retry_halves", [])
        store.set_control("retry_halves", ([ids[:middle], ids[middle:]] + (batches if isinstance(batches, list)
                                                                           else []))[:32])


def _bind(raw, sid_of, aux, labels, config):
    """(candidate, role) from a drafted candidate whose messages and plan parts are numbers: the real source ids, the
    comment ids of the plan parts, and the destination code sets (a to-do item goes on the To do task, a new task is
    issue 0, a checkpoint that marks goes on the task it marks). (None, reason) when a number is not the stretch's."""
    if any(n not in sid_of for n in raw["sources"]) or len(set(raw["sources"])) != len(raw["sources"]):
        return None, "source-binding"
    ids = [sid_of[n] for n in raw["sources"]]
    if any(link["part"] not in aux["parts"] for link in raw["links"]):
        return None, "part-binding"
    candidate = {"kind": raw["kind"], "issue": raw["issue"], "body": raw["body"], "sources": ids,
                 "excerpts": raw["excerpts"],
                 "marks": [{"task": mark["task"], "steps": sorted(set(mark["steps"]))} for mark in raw["marks"]],
                 "links": [{"key": link["key"], "part": aux["parts"][link["part"]]} for link in raw["links"]]}
    role = None
    if raw["kind"] == "steps":
        jobs = {label["job"] for label in labels if set(label["sources"]) & set(raw["sources"])}
        role = "task" if "plan_agreed" in jobs else "todo" if "to_do" in jobs else ("task" if raw["task_title"] else "todo")
        if role == "todo" and aux["todo"]:
            candidate["issue"] = aux["todo"]   # an item on the standing To do task: no title, no topic
        elif role == "todo":
            candidate.update(issue=0, task_title="To do", topic=0)   # there is none yet: this item opens it
        else:
            candidate.update(issue=0, task_title=raw["task_title"].strip(), topic=raw["topic"])
    elif raw["kind"] == "checkpoint" and candidate["marks"]:
        tasks = {mark["task"] for mark in candidate["marks"]}
        if len(tasks) == 1:
            candidate["issue"] = next(iter(tasks))
    return candidate, role


def _shown(candidate, number, parts_n):
    """The candidate as the reviewer reads it: its messages and plan parts by number, as the drafter wrote them."""
    return dict(candidate, sources=[number[sid] for sid in candidate["sources"]],
                links=[{"key": link["key"], "part": parts_n[link["part"]]} for link in candidate["links"]])


def _process(repo, config, project_root=None, invoke_fn=None):
    """One pass: up to MAX_SOURCES queued observations of the conversation (the user's typed prompts, the agent's
    messages to the user and the two dialogs the user answered), in the order they happened. One sort call labels the
    stretch; a label whose job is outside the level, or that code refuses, is dropped. With no label left the pass ends
    there. Otherwise one draft call writes the candidates the labels point at, and a fresh review call judges each of
    at most six. A pass waits for ENOUGH sources or for one older than PATIENCE."""
    invoke = invoke_fn or runtime.invoke
    options = runtime.settings(config)
    report = {"code": "disabled", "selected": 0, "reviewed": 0, "ready": [], "deferred": 0, "ignored": 0, "labels": 0,
             "dropped": {}}
    if names.env(runtime.GUARD) == "1":
        return dict(report, code="recursion")
    if not options["enabled"] or not recortex.allowed_users(repo):
        return report
    root = project_root or recortex.project_root()
    with observer_store.Store(repo, max_bytes=options["max_pending_bytes"], max_pending=options["max_pending_observations"], config=config) as store:
        if not runtime.approval_valid(repo, config):
            return dict(report, code="approval")
        if store.get_control("capture_paused", False):
            return dict(report, code="paused")
        state = runtime.tier_state(options, store)
        if not state["sort"]["gate"] or not state["middle"]["gate"]:
            return dict(report, code="runtime-gate")   # a pass needs the sort tier, and the middle tier to draft
        options = runtime.with_gates(options, store)
        level = options["level"]
        window = options["window_hours"] * 3600
        templates = template_context()
        supplied, offered = {}, []
        queued, half = _queued(store)
        holds, kept = store.holds(), []
        for item in queued:
            if observer_holds.publication_withheld(holds, repo, {item["metadata"].get("session_id")}):
                # The user kept that session off the record: nothing of it is selected, and no call is spent on it.
                # As a "withheld" review, it is not offered again.
                store.defer(item["source_id"], "withheld"); report["deferred"] += 1
            else:
                kept.append(item)
        queued = kept
        if not half and len(queued) < ENOUGH and all(time.time() - item["created"] < PATIENCE for item in queued):
            return dict(report, code="no-sources")  # wait for a stretch of the session
        released = store.released_sources()   # {source: the request the user lifted}: the models are told it, and it only
        for item in queued:
            sid, previous = item["source_id"], item["reason"] if item["state"] == "deferred" else ""
            meta = item["metadata"]
            if meta.get("event_type") == "source_gap":
                store.defer(sid, "source_gap"); report["deferred"] += 1
                continue
            if meta.get("role") not in ("user", "assistant") or os.sep + "subagents" + os.sep in meta.get("path", ""):
                # Tool output and subagents' transcripts are not the conversation (a queue from before 1.1.0).
                store.mark_handled(sid, reason="not_conversation"); report["ignored"] += 1
                continue
            try:
                source = sources.resolve(meta, root)
            except (sources.SourceError, OSError, ValueError):
                store.defer(sid, "source_unavailable"); report["deferred"] += 1
                continue
            if not _said(source):
                # A user record that is not a typed prompt (a command's wrapper): it may set a hold, nothing else.
                store.mark_handled(sid, reason="not_typed"); report["ignored"] += 1
                continue
            if source_privacy_problem(source, config):
                store.defer(sid, "privacy"); report["deferred"] += 1
                continue
            supplied[sid] = dict(source, hold_released=released[sid]) if sid in released else source
            offered.append(item)
        if not offered:
            return dict(report, code="no-sources")
        context = record_context(repo, config, query="\n".join(source["text"] for source in supplied.values()))
        if not context or not context["targets"] or context["incomplete"]:
            for item in offered: store.defer(item["source_id"], "record_unavailable")
            return dict(report, code="record-unavailable")
        if source_privacy_problem(context, config):
            for item in offered: store.defer(item["source_id"], "privacy_context")
            return dict(report, code="privacy")
        try:
            extra = fit_record(index_record(repo, config))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            extra = fit_record({})   # the topics alone; a label that names a task or a part is dropped
        record, aux = sort_record(context, extra)
        if source_privacy_problem(record, config):
            for item in offered: store.defer(item["source_id"], "privacy_context")
            return dict(report, code="privacy")
        started = time.monotonic()
        account = _account(repo)
        # In the order they happened: a turn reads as it ran. The numbers are positions in this order.
        order = {item["source_id"]: _order(item) for item in offered}
        ordered = sorted(supplied, key=lambda key: order[key])
        # What of the stretch fits both calls beside the record: a source that does not goes first next time.
        sort_room = (runtime.input_cap(options, "sort") - len(runtime.PROMPTS["sort"]) - len(runtime.canonical(SORT))
                     - len(runtime.canonical(record)) - 300)

        def draft_room(shown):
            return (runtime.input_cap(options, "middle") - len(runtime.PROMPTS["draft"]) - len(runtime.canonical(DRAFT))
                    - len(runtime.canonical(templates)) - len(runtime.canonical(dict(shown, tasks=record["tasks"],
                                                                                      plan=record["plan"])))
                    - LABELS_ROOM - 600)
        if draft_room(context) < MIN_ROOM:
            context = dict(context, context="")   # the recalled memories go before a source does
        room = min(sort_room, draft_room(context))
        fitted, used = [], 0
        for sid in ordered:
            size = len(runtime.canonical(_view(supplied[sid], 1)))
            if fitted and used + size > room:
                item = next(item for item in offered if item["source_id"] == sid)
                previous = item["reason"] if item["state"] == "deferred" else ""
                store.defer(sid, _again("input_limit", previous)); report["deferred"] += 1
            else:
                fitted.append(sid)
                used += size
        ordered = fitted
        offered = [item for item in offered if item["source_id"] in set(fitted)]
        supplied = {sid: supplied[sid] for sid in fitted}
        number = {sid: n for n, sid in enumerate(ordered, 1)}
        sid_of = {n: sid for sid, n in number.items()}
        by_n = {n: supplied[sid] for n, sid in sid_of.items()}
        views = [_view(supplied[sid], number[sid]) for sid in ordered]
        # The observer does not judge confidentiality: no field sheet, mark or pattern goes to the models. The
        # mechanical refusals (secrets, the project's restricted patterns, this machine's details) and the linter
        # apply to every body.
        batch = _batch(ordered)
        saved = next((entry for entry in store.waiting_labels() if entry["batch"] == batch), None)
        dropped = {}
        if saved is not None:
            # The same stretch was sorted before and its draft waited (the budget, a failed call): its labels are
            # checked again against the record as it is now, and the sort is not paid for twice.
            labels, dropped = check_labels(saved["labels"], by_n, aux, level)
            ignored = []
        else:
            reservation = store.reserve_invocation("sort", options["max_sort"], window, tokens=options["sort_tokens"])
            if reservation is None:
                for item in offered: store.defer(item["source_id"], "budget")
                return dict(report, code="budget")
            result = invoke({"sources": views, "record": record}, SORT, "sort", options)
            valid = result.get("ok") is True and runtime.validate(result.get("structured"), SORT)
            store.complete_invocation(reservation, valid, runtime.candidate_hash(result["structured"]) if valid else "",
                                      usage=result.get("usage"), model=result.get("model"))
            if not valid:
                _failed(store, offered, result, "sort_failed")
                return dict(report, code="sort-failed")
            labels, dropped = check_labels(result["structured"]["labels"][:24], by_n, aux, level)
            ignored = result["structured"]["ignored"]
            if labels:
                store.save_labels(batch, ordered, labels)
        report["labels"] = len(labels)
        report["dropped"] = dropped
        cited = {n for label in labels for n in label["sources"]}
        ignored_ids = {sid_of[n] for n in set(ignored) - cited if n in sid_of}
        handled = set()
        if not labels:
            handled = ignored_ids   # nothing to draft: what the sorter set aside is done with, the rest waits

        def done_with(ids):
            for sid in ids:
                if store.observation(sid)["state"] in ("observed", "deferred"):
                    store.mark_handled(sid); report["ignored"] += 1
        done_with(handled)
        candidates, raws = [], []
        if labels:
            reservation = store.reserve_invocation("draft", options["max_invocations"], window,
                                                   tokens=options["middle_tokens"])
            if reservation is None:
                for item in offered:
                    if item["source_id"] not in handled:
                        store.defer(item["source_id"], "budget")
                return dict(report, code="budget")
            zone, offset = _zone(config)
            draft_record = dict(context, tasks=record["tasks"], todo=record["todo"], plan=record["plan"], zone=zone,
                                offset=offset)
            payload = {"sources": views, "record": draft_record, "labels": labels, "templates": templates,
                       "max_candidates": options["max_candidates"], "account": account}
            result = invoke(payload, DRAFT, "draft", options)
            valid = result.get("ok") is True and runtime.validate(result.get("structured"), DRAFT)
            store.complete_invocation(reservation, valid, runtime.candidate_hash(result["structured"]) if valid else "",
                                      usage=result.get("usage"), model=result.get("model"))
            if not valid:
                done_with(ignored_ids)
                _failed(store, [item for item in offered if item["source_id"] not in ignored_ids], result, "draft_failed")
                return dict(report, code="draft-failed")
            store.finish_labels(batch)
            raws = result["structured"]["candidates"][:options["max_candidates"]]
            new_steps = max((len(record_format.steps_list(raw["body"])) for raw in raws if raw["kind"] == "steps"
                             and any(label["job"] == "plan_agreed" and set(label["sources"]) & set(raw["sources"])
                                     for label in labels)), default=0)
            plan = {"level": level, "tasks": aux["tasks"], "todo": aux["todo"], "parts": set(aux["parts"].values()),
                    "new_steps": new_steps or None}
            for place, raw in enumerate(raws, 1):
                candidate, role = _bind(raw, sid_of, aux, labels, config)
                # A candidate that cannot be bound carries its reason in place of a role, and the source it was drafted from.
                # "at" is its place in the draft, and "because" (a decision's) the place of the failure it rests on.
                candidates.append((candidate, role, dict(plan, role=role, at=place,
                                                         because=raw.get("because", 0) if raw["kind"] == "decision" else 0),
                                   next((sid_of[n] for n in raw["sources"] if n in sid_of), None)))
            # The steps memories first: a mark of task 0 waits for the one that opens its task. A decision that rests on a
            # failure of the draft comes after all the others, so that the failure is queued before it.
            candidates.sort(key=lambda row: 0 if row[1] == "task" else 2 if row[2]["because"] else 1)
        elif saved is not None:
            store.finish_labels(batch)
        report["selected"] = len(candidates)
        if labels:
            # What the sorter set aside is done with, unless the drafter found something in it after all.
            handled = ignored_ids - {sid for candidate, _, _, _ in candidates if candidate for sid in candidate["sources"]}
            done_with(handled)
        used = set()  # the sources of this pass's events
        parts_n = {cid: n for n, cid in aux["parts"].items()}
        try:
            recent = recent_on_issue(repo, {c["issue"] for c, _, _, _ in candidates if c and c["issue"] >= 1})
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            recent = {}  # the reviewer judges without it; the record is read again next pass
        review_record = dict(context, tasks=record["tasks"], todo=record["todo"], plan=record["plan"])
        opener = None   # the first source of the steps memory of this pass that opens a task, once it is queued
        failures = {}   # the key of the event of each failure of this pass, by its place in the draft
        stood_on = {}   # the messages each failure of the draft rests on, queued or not, by its place in the draft
        for candidate, role, checks, first in candidates:
            if candidate is None:
                reason = role.replace("-", "_")
                if first and first not in handled and store.observation(first)["state"] in ("observed", "deferred"):
                    before = store.observation(first)
                    store.defer(first, _again(reason, before["reason"] if before["state"] == "deferred" else "")
                                if reason in RETRY_SOON else reason)
                    handled.add(first)
                report["deferred"] += 1
                continue
            # One agent message often tells several moments (a result and the idea it raised). A source backs one
            # event: a candidate whose other sources remain drops the ones an event already holds.
            # The one exception is a decision made because of a failure of this pass (the user's one message holds both):
            # it may rest on the messages that failure rests on, whether the failure is queued or was refused, and on no
            # other that an event holds. A refused failure leaves the decision to stand alone.
            parent = failures.get(checks["because"]) if checks["because"] and candidate["kind"] == "decision" else None
            shared = stood_on.get(checks["because"], set()) if candidate["kind"] == "decision" else set()
            if parent and set(candidate["sources"]) == shared:   # the failure's own key: the decision keeps the typed words
                candidate = dict(candidate, sources=[sid for sid in candidate["sources"] if _typed(supplied[sid])]
                                 or candidate["sources"])
            taken = [sid for sid in candidate["sources"] if sid not in shared and (sid in used or store.db.execute(
                "SELECT 1 FROM event_sources WHERE source_id=?", (sid,)).fetchone())]
            if taken and len(taken) < len(candidate["sources"]):
                candidate = dict(candidate, sources=[sid for sid in candidate["sources"] if sid not in taken])
            if candidate["kind"] == "failure":
                stood_on[checks["at"]] = set(candidate["sources"])
            primary = next((sid for sid in candidate["sources"] if sid in supplied), None)
            problem = candidate_problem(candidate, supplied, context, config, repo, checks)
            if not problem and any(mark["task"] == 0 for mark in candidate["marks"]):
                # A mark of task 0 rests on the steps memory of this pass that opens that task, which must itself have
                # been queued; the publisher posts the mark after the task exists.
                if opener is None:
                    problem = "mark-binding"
                else:
                    candidate = dict(candidate, **{"with": opener})
            if parent and not problem:
                candidate = dict(candidate, because_with=parent)   # code writes because=<the failure's comment> at the post
            if problem or not primary or (set(candidate["sources"]) - shared) & handled:
                if primary and primary not in handled:
                    reason = (problem or "source-overlap").replace("-", "_")
                    before = store.observation(primary)
                    store.defer(primary, _again(reason, before["reason"] if before["state"] == "deferred" else "")
                                if reason in RETRY_SOON else reason)
                    handled.add(primary)
                report["deferred"] += 1
                continue
            newest = max(store.observation(sid)["metadata"].get("observed_at", 0) for sid in candidate["sources"])
            if candidate["issue"] >= 1 and store.main_post_after(candidate["issue"], candidate["kind"], newest):
                # The main session posted this kind for this issue after these sources: not posted twice.
                for sid in candidate["sources"]:
                    if store.observation(sid)["state"] in ("observed", "deferred"):
                        store.mark_handled(sid, reason="main_session")
                handled.update(candidate["sources"]); report["ignored"] += 1
                continue
            if time.monotonic() - started + options["timeout_seconds"] > 240:
                store.defer(primary, "worker_budget"); report["deferred"] += 1
                handled.add(primary)
                continue
            review_id = store.reserve_invocation("review", options["max_invocations"], window,
                                                 tokens=options["middle_tokens"])
            if review_id is None:
                store.defer(primary, "budget"); report["deferred"] += 1
                handled.add(primary)
                continue
            digest = runtime.candidate_hash(candidate)
            shown = _shown(candidate, number, parts_n)
            if parent:   # the reviewer is told the failure in words, never its key
                shown.pop("because_with")
                shown["because"] = {"kind": "failure", "plain": _line(record_format.plain(store.event(parent)["body"], "failure") or "")}
            schema = review_schema(candidate, supplied, context)
            reviewed = invoke({"candidate": shown, "candidate_hash": digest,
                               "sources": [_view(supplied[sid], number[sid]) for sid in candidate["sources"]],
                               "context": _stretch(candidate, supplied, order, number), "record": review_record,
                               "recent_on_issue": recent.get(candidate["issue"], []), "account": account},
                              schema, "review", options)
            answer = reviewed.get("structured")
            valid = reviewed.get("ok") is True and runtime.validate(answer, schema) and answer["candidate_hash"] == digest
            store.complete_invocation(review_id, valid, digest if valid else "", usage=reviewed.get("usage"),
                                      model=reviewed.get("model"))
            report["reviewed"] += 1
            if not valid:
                before = store.observation(primary)
                store.defer(primary, _again("review_failed", before["reason"] if before["state"] == "deferred" else ""))
                report["deferred"] += 1
                handled.add(primary)
                continue
            if (not answer["accepted"] or answer["reason"] != "supported" or
                    (schema is MIXED_REVIEW and answer["qualification"] != "separate_action")):
                if answer["reason"] == "withheld":
                    # The reviewer read the moment as kept off the record: by the user's typed words, or by mistake. Either
                    # way nothing is lost: the candidate waits as a held memory for the user's Post or Drop, and posts on
                    # no other path (a hold of the session still blocks even that, until the user releases it).
                    kept = {"accepted": False, "reason": "withheld", "candidate_hash": digest, "reservation_id": review_id}
                    if store.review_complete(primary, candidate, kept, hold="withheld_review") is not None:
                        handled.update(candidate["sources"])
                        used.update(candidate["sources"])
                else:
                    store.defer(primary, "review_rejected")
                report["deferred"] += 1
                handled.add(primary)
                continue
            review = {"accepted": True, "candidate_hash": digest, "reservation_id": review_id}
            if observer_publisher.status_change(candidate["body"]) or any(
                    link["key"] in observer_publisher.STATUS_LINKS for link in candidate["links"]):
                review["status_authority"] = status_authority(candidate, supplied, replaces_standing_decision(candidate, context))
            eventkey = store.review_complete(primary, candidate, review)
            if eventkey is None:
                report["deferred"] += 1
                handled.add(primary)
                continue
            report["ready"].append(eventkey)
            handled.update(candidate["sources"])
            used.update(candidate["sources"])
            if role == "task" and opener is None:
                opener = primary
            if candidate["kind"] == "failure":
                failures[checks["at"]] = eventkey
        # What the sorter neither used nor set aside is offered again, a bounded number of times.
        leftover = "candidate_limit" if len(raws) == options["max_candidates"] else "uncertain"
        for item in offered:
            current = store.observation(item["source_id"])
            if item["source_id"] in handled or current["state"] not in ("observed", "deferred"):
                continue
            if current["state"] == "observed" or _attempts(current["reason"])[0] in RETRY_SOON:
                store.defer(item["source_id"], _again(leftover, current["reason"] if current["state"] == "deferred"
                                                      else ""))
        store.set_control("worker_last", {"at": time.time(), "code": "ok", "selected": report["selected"],
                                          "reviewed": report["reviewed"], "ready": len(report["ready"]),
                                          "labels": report["labels"], "dropped": dropped})
    return dict(report, code="ok")


# ---------------------------------------------------------------- the summary of the project's status (spec 1.3.0, section 21)

SUMMARY_MEMORIES = 30   # the newest told memories since the last resume block that the summary may rest on
SUMMARY_SOURCES = 6     # the newest typed prompts and agent messages shown to it
SUMMARY_RETRY = 6 * 3600   # seconds before a refresh that did not post (refused, rejected, failed) is tried again


def summary_wanted(repo, config):
    """True when the summary of the project's status is stale by the brief's own rule (brief.summary_line): enough told
    memories since the newest resume block, or a day since it with at least one. Judged from the index; nothing is read from
    GitHub here."""
    import brief
    db = index.connect(repo, create=False)
    if db is None:
        return False
    try:
        return bool(index.mind_ready(db) and brief.summary_line(brief.Record(db, config, repo)))
    finally:
        db.close()


def summary_material(repo, config):
    """What a refreshed summary may rest on, from the checked index: the newest resume block (the allowed, never edited one the
    sync stored), the memories told since it, as kind, day and plain line, and the plan's integers. None when there is no
    block to refresh."""
    db = index.connect(repo, create=False)
    if db is None:
        return None
    try:
        allowed = recortex.allowed_users(repo)
        block, posted = index.get_meta(db, "resume") or "", index.get_meta(db, "resume_posted") or ""
        if not allowed or not block or not index.POSTED.fullmatch(posted):
            return None
        told = index.memories(db, allowed)["items"]
        created = {row["comment"]: row["created_at"] or "" for row in db.execute("SELECT comment, created_at FROM events")}
        since = [(item["time"], cid, item) for cid, item in told.items() if created.get(cid, "") > posted]
        memories = [{"kind": item["kind"], "day": (item["at"] or "")[:10],
                     "plain": mind_model.short(mind_model.clean(item["plain"] or item["title"] or ""), 300)}
                    for _, _, item in sorted(since, key=lambda row: (row[0], row[1]))[-SUMMARY_MEMORIES:]]
        plan = {}
        try:
            plan = {k: v for k, v in json.loads(index.get_meta(db, "plan_counts") or "{}").items() if type(v) is int}
        except (ValueError, AttributeError):
            plan = {}
        return {"block": block[:4000], "posted": posted, "memories": memories, "plan": plan,
                "fields": code_fields(db, allowed, told)}
    finally:
        db.close()


STATUS_TASKS, STATUS_DECISIONS = 5, 3


def code_fields(db, allowed, told):
    """What the block's Active issues, Next actions and Last decisions say, built by code from the told memories and the
    index, never by a model: the open `active` issues with their step counts, the next open step of up to
    STATUS_TASKS open tasks, and the STATUS_DECISIONS newest standing decisions (day, plain line, address)."""
    active, upcoming = [], []
    for task in index.tasks(db, allowed)["tasks"]:
        if task["state"] != "open":
            continue
        counts = task["counts"]
        active.append({"issue": task["issue"], "done": counts["done"], "steps": counts["steps"]})
        step = next((row for row in task["steps"] if row["state"] in ("open", "blocked")), None)
        if step is not None and len(upcoming) < STATUS_TASKS:
            upcoming.append({"issue": task["issue"], "n": step["n"], "text": _line(step["text"], 120)})
    urls = {row["comment"]: row["url"] for row in db.execute("SELECT comment, url FROM events")}
    decisions = sorted((item for item in told.values() if item["kind"] == "decision" and item["state"] == "stands"),
                       key=lambda item: (item["time"], item["id"]), reverse=True)[:STATUS_DECISIONS]
    return {"active": active, "next": upcoming,
            "decisions": [{"day": (item["at"] or "")[:10], "plain": _line(item["plain"] or item["title"], 120),
                           "address": urls.get(item["id"]) or ""} for item in decisions]}


def summary_problem(paragraphs, config):
    """Why a drafted summary is not posted, or "": one or two paragraphs with words in them, at most 1,200 characters in all,
    no issue number, address or mention, and none of the mechanical refusals (secrets, the project's restricted patterns)."""
    if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 2 or not all(isinstance(p, str) and p.strip() for p in paragraphs):
        return "form"
    text = "\n\n".join(p.strip() for p in paragraphs)
    if len(text) > record_format.IN_SHORT_LIMIT or record_format.ISSUE_NUMBER_IN_PROSE.search(text) \
            or re.search(r"https?://|www\.|@[A-Za-z]|\*\*|<", text) or re.search(r"(?m)^\s*(?:[-*]|[0-9]+\.)\s", text):
        return "form"
    return "privacy" if privacy_problem(text, config) else ""


RESUME_FIELD = re.compile(r"^\*\*(In short|Last updated|Current focus|Active issues|Last decisions|Next actions|Holds):\*\*")


def resume_fields(block):
    """{label: text as written} of the fields of a resume block; a field runs to the next label."""
    fields, label, lines = {}, None, []
    for line in block.replace("\r\n", "\n").split("\n"):
        match = RESUME_FIELD.match(line)
        if match:
            if label:
                fields[label] = "\n".join(lines).strip("\n")
            label, lines = match.group(1), [line[match.end():].strip()]
        elif label:
            lines.append(line)
    if label:
        fields[label] = "\n".join(lines).strip("\n")
    return fields


def _cell(text):
    return text.replace("|", "/")


def code_text(fields):
    """The three fields code writes, as text: ("Active issues", "Last decisions", the rows of "Next actions")."""
    active = "; ".join("#%d, %d of %d steps" % (task["issue"], task["done"], task["steps"]) if task["steps"]
                       else "#%d" % task["issue"] for task in fields["active"]) or "none"
    decisions = "; ".join("%s, %s, %s" % (row["day"] or "no date", row["plain"], row["address"] or "no address")
                          for row in fields["decisions"]) or "none"
    rows = "\n".join("| Step %d: %s | not stated | #%d |" % (row["n"], _cell(row["text"]), row["issue"])
                     for row in fields["next"]) or "| none | not stated | none |"
    return active, decisions, rows


def summary_block(material, paragraphs, moment):
    """The text of the new resume block: the model's In short, the time stamp renewed ("by the observer"), "Current focus"
    and "Holds" copied from the newest block word for word, and "Active issues", "Last decisions" and "Next actions" built
    by code from the index (material["fields"]). A material with no code fields (an index that cannot be read) keeps the
    newest block's fields as they were."""
    code = material.get("fields")
    if code is None:
        keep, inside = [], False
        for line in material["block"].replace("\r\n", "\n").split("\n"):
            if line.startswith(record_format.RESUME_LABELS[0]):
                inside = True
                continue
            if inside and line.startswith(record_format.RESUME_LABELS[1:]):
                inside = False
            if inside:
                continue
            if line.startswith("**Last updated:**"):
                line = "**Last updated:** %s by the observer" % moment
            keep.append(line)
        return "**In short:** %s\n\n%s\n" % ("\n\n".join(p.strip() for p in paragraphs), "\n".join(keep).strip("\n"))
    written = resume_fields(material["block"])
    active, decisions, rows = code_text(code)
    return ("**In short:** %s\n\n**Last updated:** %s by the observer\n**Current focus:** %s\n**Active issues:** %s\n"
            "**Last decisions:** %s\n**Next actions:**\n\n| Action | Owner | Where |\n|---|---|---|\n%s\n\n"
            "**Holds:** %s\n" % ("\n\n".join(p.strip() for p in paragraphs), moment,
                                  written.get("Current focus") or "not stated", active, decisions, rows,
                                  written.get("Holds") or "none"))


def post_summary(repo, config, text):
    """Post the new resume block through resume_block.py update (the linter, the post and the read back are its own); the
    comment is the user's own account's, as every resume block is, and an allowed one. True when it was posted and read
    back."""
    import subprocess
    import sys
    import tempfile
    hub = config.get("hub") if type(config.get("hub")) is int else None
    if hub is None:
        return False
    with tempfile.TemporaryDirectory(prefix="summary-", dir=storage.folder(storage.state_top(), "observer-runtime")) as folder:
        path = os.path.join(folder, "resume.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        done = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_block.py"),
                               "update", "--file", path, "--repo", repo, "--hub", str(hub)],
                              capture_output=True, text=True, timeout=120)
    return done.returncode == 0 and "posted:" in done.stdout and "MISMATCH" not in done.stdout


def recent_views(store, repo, root, config):
    """The newest typed prompts and agent messages the worker still holds, as the sorter sees them: the sources of a
    session the user kept off the record are left out, and so is whatever the mechanical privacy check refuses."""
    queued, _ = _queued(store)
    holds, views = store.holds(), []
    for item in sorted(queued, key=_order)[-SUMMARY_SOURCES * 3:]:
        meta = item["metadata"]
        if observer_holds.publication_withheld(holds, repo, {meta.get("session_id")}) \
                or meta.get("role") not in ("user", "assistant") or os.sep + "subagents" + os.sep in meta.get("path", ""):
            continue
        try:
            source = sources.resolve(meta, root)
        except (sources.SourceError, OSError, ValueError):
            continue
        if _said(source) and not source_privacy_problem(source, config):
            views.append(_view(source, len(views) + 1))
    return [dict(view, n=n) for n, view in enumerate(views[-SUMMARY_SOURCES:], 1)]


def refresh_summary(repo, config, store, options, invoke, root):
    """One refresh of the project's status summary, by the observer: when it is stale, one call that writes it from the
    index's told memories and the conversation the worker holds, one fresh call that reviews it, and the post of a new resume
    block (its other fields built by code, see summary_block). Both calls are middle-tier calls of the job "status", counted
    in the daily cap and token budget of that tier, and at most max_summaries blocks are posted a day. Returns a code;
    nothing is posted unless every step passed."""
    if not summary_wanted(repo, config):
        return "current"
    options = runtime.with_gates(options, store)
    posts = [at for at in store.get_control("summary_posts", []) if type(at) in (int, float) and time.time() - at < 86400]
    if len(posts) >= options["max_summaries"]:
        return "cap"
    last = store.get_control("summary_last", None)   # a refresh that did not post costs no further call for a while
    if isinstance(last, dict) and last.get("code") != "posted" and type(last.get("at")) in (int, float) \
            and 0 <= time.time() - last["at"] < SUMMARY_RETRY:
        return "waiting"
    material = summary_material(repo, config)
    if material is None:
        return "no-block"
    views = recent_views(store, repo, root, config)
    payload = {"previous": material["block"], "memories": material["memories"], "plan": material["plan"], "sources": views}
    if source_privacy_problem(payload, config):
        return "privacy"
    window = options["window_hours"] * 3600
    reservation = store.reserve_invocation("summary", options["max_invocations"], window, tokens=options["middle_tokens"])
    if reservation is None:
        return "budget"
    result = invoke(payload, SUMMARY, "summary", options)
    valid = result.get("ok") is True and runtime.validate(result.get("structured"), SUMMARY)
    store.complete_invocation(reservation, valid, runtime.candidate_hash(result["structured"]) if valid else "",
                              usage=result.get("usage"), model=result.get("model"))
    if not valid:
        return "summary-failed"
    paragraphs = [p.strip() for p in result["structured"]["paragraphs"]]
    problem = summary_problem(paragraphs, config)
    if problem:
        return "summary-" + problem
    review_id = store.reserve_invocation("summary_review", options["max_invocations"], window,
                                         tokens=options["middle_tokens"])
    if review_id is None:
        return "budget"
    digest = runtime.candidate_hash({"paragraphs": paragraphs})
    reviewed = invoke(dict(payload, summary=paragraphs, summary_hash=digest), SUMMARY_REVIEW, "summary_review", options)
    answer = reviewed.get("structured")
    valid = reviewed.get("ok") is True and runtime.validate(answer, SUMMARY_REVIEW) and answer["summary_hash"] == digest
    store.complete_invocation(review_id, valid, digest if valid else "", usage=reviewed.get("usage"),
                              model=reviewed.get("model"))
    if not valid or not answer["accepted"] or answer["reason"] != "supported":
        return "summary-rejected"
    moment = recortex.stamp(datetime.datetime.now(datetime.timezone.utc), config)
    if not post_summary(repo, config, summary_block(material, paragraphs, moment)):
        return "post-failed"
    store.set_control("sync_needed", True)   # the next refresh reads the new block, which makes the summary current
    store.set_control("summary_posts", posts + [time.time()])
    return "posted"


SYNC_RETRY = 30  # seconds before the next worker tries again after a refresh that failed
BUSY_WAITS = (3, 5, 8)  # seconds to wait for another process's sync (the brief's, at session start) to finish


def refresh_record(repo, config, store, options):
    """Bounded incremental refresh in the worker; hooks never wait on GitHub. A refresh that failed is tried again
    after SYNC_RETRY seconds, not after the full interval, and a sync another process holds (the brief hook's, at
    session start) is waited for briefly."""
    now = time.time()
    ok = store.get_control("sync_ok", False)
    interval = options["sync_interval_seconds"] if ok else SYNC_RETRY
    due = now - store.get_control("sync_last_attempt", 0) >= interval
    if not due and not store.get_control("sync_needed", False):
        return ok
    store.set_control("sync_last_attempt", now)
    complete = False
    for wait in BUSY_WAITS + (None,):
        try:
            result = index.sync(repo, config.get("hub"), timeout=10, deadline=10, log=lambda *args: None)
            complete = result.get("complete") is True
            break
        except index.SyncBusy:
            if wait is None:
                break
            time.sleep(wait)
        except (index.DeadlineReached, recortex.GhError, OSError, ValueError):
            break
    store.set_control("sync_ok", complete)
    if complete:
        store.set_control("sync_needed", False)
    return complete


SETTLE_SECONDS = 3.0  # how long the worker waits for the transcripts it knows to stop growing
SETTLE_QUIET = 0.4  # and how long one must stay the same size to count as settled
KNOWN_HOURS = 6  # a transcript it knows: one with an observation this recent


def settle(store, repo, config, project_root, seconds=SETTLE_SECONDS, quiet=SETTLE_QUIET):
    """Capture what the hooks could not. Claude Code writes a turn's final message after its Stop hook has run, so the
    worker started there waits until the transcripts it knows stop growing (at most `seconds`), then scans their unread
    tails as a hook does, holds included. The transcript stays the only source. Returns the number of transcripts
    read; one that cannot be read is left to the next hook."""
    import observer_hooks  # here: the worker needs it only for this
    since = time.time() - KNOWN_HOURS * 3600
    known = {}
    for (metadata,) in store.db.execute("SELECT metadata FROM observations WHERE updated>? ORDER BY updated DESC "
                                        "LIMIT 2000", (since,)):
        meta = json.loads(metadata)
        path, session = meta.get("path"), meta.get("session_id")
        if isinstance(path, str) and isinstance(session, str) and os.path.basename(path) == session + ".jsonl":
            known.setdefault(path, session)
    known = dict(list(known.items())[:20])

    def sizes():
        out = {}
        for path in known:
            try:
                out[path] = os.stat(path, follow_symlinks=False).st_size
            except OSError:
                out[path] = None
        return out
    last, changed, until = sizes(), time.monotonic(), time.monotonic() + seconds
    while known and time.monotonic() < until:
        time.sleep(0.05)
        now = sizes()
        if now != last:
            last, changed = now, time.monotonic()
        elif time.monotonic() - changed >= quiet:
            break
    read = 0
    paused = store.get_control("capture_paused", False)
    for path, session in known.items():
        data = {"session_id": session, "transcript_path": path, "hook_event_name": "Stop"}
        try:
            observer_hooks._capture(store, "Stop", data, project_root, repo, config, capture=not paused)
            read += 1
        except (sources.SourceError, observer_store.StoreError, OSError, ValueError):
            continue
    return read


WORKER_SECONDS = 250   # what a worker may take for the pass, the summary and the publisher
JOBS_SECONDS = 120     # and what the housekeeping jobs may take after the keeper, whatever came before


class Clock:
    """The worker's lifetime. `remaining()` is what is left; `arm(seconds)` sets what is left from now on, and with `armed`
    the alarm (SIGALRM) that observer.py's run-worker relies on is set again to it. A fake `now` makes it testable. With no
    `seconds` it is unlimited and sets no alarm."""

    def __init__(self, seconds=None, armed=False, now=time.monotonic):
        self.now, self.armed, self.end = now, armed, None
        if seconds is not None:
            self.arm(seconds)

    def remaining(self):
        return float("inf") if self.end is None else self.end - self.now()

    def arm(self, seconds):
        self.end = self.now() + seconds
        if self.armed:
            signal.setitimer(signal.ITIMER_REAL, seconds)


def lifetime(options):
    """How long a worker may live: the pass's time, and with a stronger model set the room of the keeper's two calls."""
    return WORKER_SECONDS + (2 * runtime.call_timeout(options, "strong") + 60 if options.get("strong_model") else 0)


def run_once(repo, config, project_root=None, invoke_fn=None, settle_first=False, clock=None):
    """One locked finite pass; existing ready events survive restarts and capture pauses. With settle_first (the
    worker a hook starts), the known transcripts are captured again first (settle). `clock` is the worker's lifetime
    (Clock); the keeper is left for the next pass when too little of it is left for a strong call, and the housekeeping
    jobs get a deadline of their own."""
    clock = clock or Clock()
    options = runtime.settings(config)
    if names.env(runtime.GUARD) == "1" or not options["enabled"] or not recortex.allowed_users(repo):
        return {"code": "disabled", "ready": []}
    with observer_store.Store(repo, config=config, max_bytes=options["max_pending_bytes"], max_pending=options["max_pending_observations"]) as store:
        # The host lease is a gate in front of the worker's own lock and never replaces it: with one approval valid on every
        # host of a shared mount, only one host's worker works on this queue at a time (observer_lease). Another host's lease
        # that holds: leave quietly, take no lock and call no model.
        if observer_lease.peek(store.folder) is not None:
            return {"code": "other-host", "ready": []}
        lease = None
        try:
            with observer_store._exclusive_lock(store.folder, "worker.lock"):
                lease = observer_lease.acquire(store.folder)
                if not lease.held:
                    return {"code": "other-host", "ready": []}
                store.set_control("worker_lease", "unavailable" if lease.state == "unavailable" else "")
                if not runtime.approval_valid(repo, config):
                    store.set_control("worker_degraded", "approval")
                    return {"code": "approval", "ready": []}
                billing = runtime.billing_variables()
                if store.get_control("call_environment", []) != billing:
                    store.set_control("call_environment", billing)  # names only, for status
                if time.time() - store.get_control("retention_last", 0) > 86400:
                    store.expire_handled(options["retention_days"])   # also zeroes the token counts past 35 days
                    try:
                        import usage
                        usage.cleanup(repo)   # the estimates' file (agents, injected context): rows past 35 days
                    except Exception:  # noqa: BLE001 - a file of estimates must never stop the worker
                        pass
                    store.set_control("retention_last", time.time())
                tiers = runtime.tier_state(options, store)
                if not tiers["sort"]["gate"] or not tiers["middle"]["gate"]:
                    store.set_control("worker_degraded", "runtime_gate")
                    return {"code": "runtime-gate", "ready": []}
                if settle_first:
                    settle(store, repo, config, project_root or recortex.project_root())
                if not refresh_record(repo, config, store, options):
                    store.set_control("worker_degraded", "record_unavailable")
                    return {"code": "record-unavailable", "ready": []}
                if not lease.renew():
                    return {"code": "other-host", "ready": []}
                report = _process(repo, config, project_root, invoke_fn)
                store.set_control("worker_degraded", "" if report["code"] in ("ok", "no-sources", "paused")
                                  else report["code"].replace("-", "_"))
                if options["map_mode"] != "off" and clock.remaining() > 2 * runtime.call_timeout(options, "middle") + 5:
                    if not lease.renew():   # the map step may post to GitHub, and the pass before it may have run long
                        return dict(report, code="other-host")
                    import observer_map
                    try:
                        report["map"] = observer_map.consider(store, repo, config, options, invoke_fn or runtime.invoke,
                                                              project_root or recortex.project_root())
                    except (OSError, ValueError, sqlite3.Error, recortex.GhError, observer_store.StoreError, storage.StorageError,
                            subprocess.TimeoutExpired):
                        report["map"] = "map_unavailable"
                    store.set_control(observer_map.control("last"), {"at": time.time(), "code": report["map"]})
                if not lease.renew():
                    return dict(report, code="other-host")
                approved = runtime.approval_valid(repo, config)
                if (approved and runtime.tier_state(options, store)["middle"]["gate"]
                        and options["mode"] == "automatic" and not store.get_control("publication_paused", False)):
                    try:   # the status summary, when it is stale: the observer's own refresh (the hook stays silent)
                        code = refresh_summary(repo, config, store, options, invoke_fn or runtime.invoke,
                                               project_root or recortex.project_root())
                    except (OSError, ValueError, sqlite3.Error, recortex.GhError, observer_store.StoreError,
                            subprocess.TimeoutExpired):
                        code = "summary-error"
                    if code not in ("current", "waiting"):   # waiting does not renew the time
                        store.set_control("summary_last", {"at": time.time(), "code": code})
                    # Includes reviewed events left ready by a previous interrupted worker.
                    for event in store.events(states=["ready"], limit=4):
                        if not lease.renew():   # before every post: an earlier post or the summary may have run long
                            return dict(report, code="other-host")
                        try:
                            observer_publisher.publish(repo, event["eventkey"], config)
                        except (observer_publisher.PublishError, recortex.GhError) as error:
                            # One event's own problem defers that event and the next one is tried; anything else
                            # (the daily cap, a marked project, GitHub unavailable) stops the pass. Both are kept.
                            reason = getattr(error, "reason", "github_unavailable")
                            if reason not in observer_holds.HELD_REASONS:
                                # A hold or a held-back event is what the user asked for, not a fault: no "pending".
                                store.set_control("worker_degraded", "publish_" + reason)
                            if reason in observer_publisher.EVENT_PROBLEMS:
                                store.transition(event["eventkey"], "deferred", reason=reason)
                                continue
                            break
                    if not lease.renew():
                        return dict(report, code="other-host")
                    posted = bool(store.get_control("sync_needed", False))   # a verified post set it; the sync clears it
                    # The manuscript keeper (spec 7) is considered here, after the pass and the publisher; it never raises
                    # and never writes the paper's text, it only leaves a draft that waits for the user's answer.
                    observer_keeper.consider(repo, config, store, options, invoke_fn or runtime.invoke,
                                             session_started=settle_first,
                                             project_root=project_root or recortex.project_root(), clock=clock,
                                             holds=lease.renew)
                    if not lease.renew():   # the keeper's two strong calls may have used the lease up: no job writes then
                        return dict(report, code="other-host")
                    # The housekeeping jobs (spec 8.2): after a pass that posted something, and once a day. No model call.
                    # They have their own deadline: an alarm that a cut call swallowed is spent, so it is set again.
                    clock.arm(JOBS_SECONDS)
                    try:
                        steps = observer_jobs.run(repo, config, store, options, posted=posted,
                                                  refresh=lambda: refresh_record(repo, config, store, options), clock=clock,
                                                  holds=lease.renew)
                        if observer_jobs.LEASE_LOST in steps.values():   # a step stopped before its write: stop the pass
                            return dict(report, code="other-host")
                    except (OSError, ValueError, sqlite3.Error, recortex.GhError, observer_store.StoreError,
                            subprocess.TimeoutExpired):
                        store.set_control("jobs_last", {"at": time.time(), "steps": {"run": "failed"}})
                    if store.get_control("sync_needed", False) and not refresh_record(repo, config, store, options):
                        store.set_control("worker_degraded", "record_unavailable")
                return report
        except observer_store.StoreError as error:
            if error.reason == "publisher_busy":
                return {"code": "worker-busy", "ready": []}
            store.set_control("worker_degraded", "store_failure")
            raise
        finally:
            if lease is not None:
                lease.release()

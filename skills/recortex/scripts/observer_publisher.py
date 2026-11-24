"""Publish reviewed queue events only, with uncertain-outcome reconciliation.

Only comment append requests are allowed. No issue, body, hub, board or label
mutation is available here. A local lock prevents concurrent local sends; this
does not promise exactly-once physical comments across machines.
"""

import contextlib
import hashlib
import html
import io
import json
import os
import posixpath
import re
import sqlite3
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit

import lint_drafts
import lookup
import observer_holds
import observer_runtime
from observer_store import Store, StoreError, candidate_hash, exact_words  # noqa: F401 (exact_words: the worker's)
import observer_store
import record_store
import recortex
import record_format


class PublishError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("observer publication deferred: " + reason)


class Waiting(PublishError):
    """Nothing is wrong: the event waits for another event of the queue (the one that opens the task its marks are
    about). With `defer`, that event is itself held for the user's answer, so this one leaves the ready list instead of
    crowding the next ones out of it."""

    def __init__(self, reason="depends", defer=False):
        super().__init__(reason)
        self.defer = defer


# What the user is asked about a task, in fixed words (spec 8.5); a title in the question is model text, shown to the
# user in the dialog and never placed in a hook payload. observer_sources.OWN_DIALOG matches both templates, so the
# user's answer to either is never a source.
TASK_OPEN_QUESTION = 'Task %s: open a task "%s" with %d steps?'
TASK_OPEN_OPTIONS = ("Open it", "Not now", "Drop it")
TASK_CLOSE_QUESTION = "Task %s: every step of #%d is done. Close it?"
TASK_CLOSE_OPTIONS = ("Close it", "Not now")
MARK_KINDS = record_format.MARK_RULES["done"]   # the kinds of memory that may carry the steps they check off
PLAN_LINK_KINDS = {"checks": ("result", "failure"), "against": ("result",)}
FOLLOW_WINDOW = 1800   # without a `with`, a task-0 mark follows the task opened this many seconds before it, at most
TITLE_CAP = 120        # the longest title of a task the publisher opens


# Checked on the raw body, code included: GitHub's own rules for code spans (backslash escapes, backtick runs of equal
# length) decide what renders as a live mention, so nothing is taken for code here. An @ after a word character is an
# email address or a path, never a mention.
MENTION = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9]")
ADDRESS = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>()\]]+")
# Targets of inline links and images, and of reference definitions; each is resolved as a browser would.
LINK_TARGET = re.compile(r"\]\(\s*<?([^\s)>]*)")
REFERENCE = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*\n?[ \t]*<?([^\s>]+)")
ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")
# Not every Markdown form of a link can be matched with the standard library, so these are refused outright once this
# repository's own addresses are taken out: a reference definition ("]:" anywhere), "//", a backslash pair, "www.",
# and anything shaped like a scheme ("data:", "javascript:"; a field label ends in ":**" or ": ", a time in a digit).
OUTSIDE = re.compile(r"\]:|//|\\\\|(?i:www\.)|(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*:(?=[^\s*0-9])")
# owner/repo#123 and owner/repo@<sha> link, and can notify, another repository; user@<sha> names a fork's commit.
CROSS = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:#[0-9]+|@[0-9a-fA-F]{7,40}\b)")
FORK = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9-]+)@[0-9a-fA-F]{7,40}\b")
# Checked on the body without its first-line event marker: any other HTML comment, a second marker included, can
# carry text that GitHub does not show.
MARKUP = re.compile(r"!\[|<(?!/?(?:details|summary)>)[A-Za-z!/?]")
# This machine's own details never go out in an automatic body: an email address, an absolute local path (two or more
# components after a leading / or ~/, not part of an address), the home folder and the local user name.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9_.~:/%-])(?:~?/(?:[A-Za-z0-9_.@+%-]+/)+[A-Za-z0-9_.@+%-]*|~/)")
# A user name that is also an ordinary word would refuse ordinary text; such a name is left to the reviewer.
GENERIC_USERS = frozenset(("user", "users", "admin", "root", "ubuntu", "dev", "test", "guest", "runner", "node",
                           "owner", "home", "data", "work", "code", "app"))
STATUS_LINKS = ("replaces", "instead", "checks", "against")  # checks= answers a question, marks an idea tried or backs a plan part; against= speaks against a plan part
STATUS_AUTHORITY = ("user", "agent")  # the user's typed words, or the agent's message to the user
# A problem of the event itself: the event is deferred with this reason and the next one is tried. Every reason that
# keeps an event from posting on its own is one (observer_holds.HELD_REASONS); the queue marks such an event held.
EVENT_PROBLEMS = frozenset(("lint_failed", "invalid_target", "outbound_markup", "outbound_mention", "outbound_address",
                            "outbound_reference", "outbound_local", "payload_changed", "review_changed", "mark_binding",
                            "mark_kind", "part_binding", "level", "title_form",
                            "title_restricted")) | frozenset(observer_holds.HELD_REASONS)


def off_repository(target, repo):
    """True unless `target`, decoded as Markdown decodes a link destination and resolved against the repository's page
    as a browser resolves it, is a page of this repository on github.com."""
    value = ESCAPED.sub(r"\1", html.unescape(target)).replace("\\", "/")
    if any(ord(char) < 32 or char.isspace() for char in value):
        return True
    home = "https://github.com/%s/" % repo.lower()
    if value.lower().startswith("www."):
        value = "https://" + value
    try:
        parts = urlsplit(urljoin(home, value))
    except ValueError:
        return True
    path = posixpath.normpath(unquote(parts.path) or "/")
    return (parts.scheme.lower() != "https" or parts.netloc.lower() != "github.com"
            or not (path.lower() + "/").startswith("/%s/" % repo.lower()))


def kept(store, event, config):
    """True when this event is, or once was, kept from posting on its own: marked held in the queue, or held now by
    the user (hold_reason) or by the automatic gate (held_back). Recorded when the event is discarded, so that a copy
    of it stays refused."""
    return bool(event.get("held")) or bool(hold_reason(store, event)) or bool(held_back(store, event, config))


def queued_decision(store, draft):
    """An observer event that already holds the decision in `draft` (the same exact words), or None. A discarded
    event counts only when it was held: then the same decision is not posted around it. The whole queue is searched
    by digest; a search that cannot complete raises StoreError, and the post is refused."""
    return store.decision_of(draft)


def queued_copy(store, draft):
    """An observer event, in any state, whose reviewed body is this draft, its eventkey aside, or None. Such a draft
    is posted with --eventkey, which keeps the event's holds and the session's withhold; a copy of a discarded event
    is not posted at all. A rewritten memory is the session's own. Searched as queued_decision is."""
    return store.copy_of(draft)


def register_main(repo, config, issue, kind, comment_id):
    """Record a memory the main session posted itself in the observer's queue; the keys of the events it sets
    aside are returned."""
    with Store(repo, config=config, create=False) as store:
        return store.register_main(issue, kind, comment_id)


def _without_marker(body):
    """The body without its event marker, when the first line is exactly one clean marker; else the body unchanged."""
    lines = body.split("\n")
    first = next((n for n, line in enumerate(lines) if line.strip()), None)
    if first is not None:
        line = lines[first].strip()
        if (record_format.parse_marker(line) and line.count("<") == 1 and line.count(">") == 1
                and line.count("--") == 2):
            return "\n".join(lines[:first] + lines[first + 1:])
    return body


def status_change(body):
    """True for a memory that changes what an earlier one means: a correction, or a replaces=, instead= or checks= key
    (checks= closes a question or marks an idea tried)."""
    marker = record_format.parse_marker(record_format.first_lines(body)[0]) or {}
    return marker.get("type") == "correction" or any(key in marker for key in STATUS_LINKS)


def local_names():
    """This machine's home folders and user names, as the account database and the environment give them."""
    homes, users = {os.path.expanduser("~")}, {os.environ.get("USER", ""), os.environ.get("LOGNAME", "")}
    try:
        import pwd
        account = pwd.getpwuid(os.getuid())
        homes.add(account.pw_dir)
        users.add(account.pw_name)
    except (ImportError, KeyError, AttributeError, OSError):
        pass
    return ({home.rstrip("/") for home in homes if len(home.rstrip("/")) > 1},
            {user for user in users if len(user) >= 3 and user.lower() not in GENERIC_USERS})


def local_problem(body):
    """True when the body holds an email address, an absolute local path, the home folder or the local user name, as
    written, with entities decoded, or as GitHub shows it once Markdown's backslash escapes are dropped (\\/home\\/x)."""
    shown = html.unescape(body)
    texts = (body, shown, ESCAPED.sub(r"\1", shown))
    homes, users = local_names()
    if any(EMAIL.search(text) or LOCAL_PATH.search(ADDRESS.sub(" ", text)) for text in texts):
        return True
    return any(home in text for home in homes for text in texts) or any(
        re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(user) + r"(?![A-Za-z0-9_-])", text, re.IGNORECASE)
        for user in users for text in texts)


def outbound_problem(body, repo, status_ok=False):
    """What a model wrote stays inside the record. A memory that changes what an earlier one means posts on its own
    only when it rests on the user's own words or on a command that ran (status_ok); otherwise it waits for the main
    session. No image and no HTML beyond the event marker and folds; no @mention; no address other than this
    repository on github.com; nothing of this machine (local_problem). None when the body passes."""
    if status_change(body) and not status_ok:
        return "status_change"
    if MARKUP.search(_without_marker(body)):
        return "outbound_markup"
    # GitHub decodes entities (&#64;, &commat;, &#47;) before it looks for mentions and links, so both forms are checked.
    texts = (body, html.unescape(body))
    if any(MENTION.search(text) for text in texts):
        return "outbound_mention"
    targets = [target for text in texts for target in ADDRESS.findall(text) + LINK_TARGET.findall(text) + REFERENCE.findall(text)]
    if any(off_repository(target, repo) for target in targets):
        return "outbound_address"
    # Every address left is this repository's; the event marker is the record's own.
    rests = [ADDRESS.sub(" ", text).replace("<!-- ghim:event ", "<!-- ", 1) for text in texts]
    if any(OUTSIDE.search(rest) for rest in rests):
        return "outbound_address"
    owner = repo.split("/")[0].lower()
    if any(any((first + "/" + second).lower() != repo.lower() for first, second in CROSS.findall(rest))
           or any(user.lower() != owner for user in FORK.findall(rest)) for rest in rests):
        return "outbound_reference"
    if local_problem(body):
        return "outbound_local"
    return None


def withheld_review(event):
    """True for an event kept from a candidate the reviewer rejected with the reason withheld: its review says so (the
    store makes one only for that, observer_store.review_complete with hold)."""
    review = event.get("review") or {}
    return review.get("accepted") is False and review.get("reason") == "withheld"


def held_back(store, event, config):
    """Why the automatic gate holds this event back, as reasons of observer_holds.ANSWER_REASONS, which the main
    session passes on the user's answer to its question: a candidate the reviewer rejected as withheld (the user's
    typed words, or the reviewer's mistake: the user decides), and a change of an earlier memory's status that rests
    neither on the user's typed words nor on the agent's message to the user. The observer does not judge
    confidentiality: a project marked as handling sensitive material is held back for nothing more."""
    reasons = []
    if withheld_review(event):
        reasons.append("withheld_review")
    if status_change(event["body"]) and event["review"].get("status_authority") not in STATUS_AUTHORITY:
        reasons.append("status_change")
    return reasons


def _gate(store, config, main_session=False):
    settings = observer_runtime.settings(config)
    if store.repo not in recortex.trusted_repos():
        raise PublishError("not_trusted")
    if settings["mode"] != "automatic" or not observer_runtime.approval_valid(store.repo, config):
        raise PublishError("not_approved")
    if not observer_runtime.gate_valid(store.get_control("runtime_gate"), settings):
        raise PublishError("runtime_unavailable")
    if store.get_control("publication_paused", False):
        raise PublishError("publication_paused")
    return settings


# ---------------------------------------------------------------- tasks, steps and marks (1.5.0)

def opens_task(candidate):
    """True for a steps memory that opens a task: kind steps and issue 0 (the task is the issue it creates)."""
    return candidate["kind"] == "steps" and candidate["issue"] == 0


def task_open_question(item):
    """The fixed question for a "task_open" item of Store.waiting(): its short id, its title as stored and its number of
    steps; the options are TASK_OPEN_OPTIONS."""
    return TASK_OPEN_QUESTION % (item["short"], " ".join(str(item["title"]).split()), item["steps"])


def task_close_question(short, issue):
    """The fixed question for closing a finished task; the options are TASK_CLOSE_OPTIONS."""
    return TASK_CLOSE_QUESTION % (short, issue)


def _marker_split(body):
    """(lines, index of the marker line, its fields as a dict in the order written) of a body whose first line is one
    event marker; PublishError("lint_failed") for any other body, which can not be composed."""
    lines = body.split("\n")
    first = next((n for n, line in enumerate(lines) if line.strip()), None)
    marker = record_format.parse_marker(lines[first].strip()) if first is not None else None
    if not marker:
        raise PublishError("lint_failed")
    return lines, first, marker


def _group_value(groups):
    return ";".join("%d:%s" % (comment, ",".join(str(n) for n in sorted(numbers)))
                    for comment, numbers in sorted(groups.items()))


def _resolve_marks(marks, tasks, opened):
    """{steps comment id: {step numbers inside it}} for candidate marks ({"task", "steps"}, task-level step numbers).
    Task 0 is the task a steps memory of the same pass opened (`opened`: its first steps memory, whose numbers are its
    own); another task must be an open task of the index and each step an open one of it. Anything else is
    mark_binding: the mark is about nothing the record holds."""
    groups = {}
    for mark in marks:
        if mark["task"] == 0:
            if opened is None or any(n > opened["steps"] for n in mark["steps"]):
                raise PublishError("mark_binding")
            groups.setdefault(opened["comment"], set()).update(mark["steps"])
            continue
        task = tasks.get(mark["task"])
        if task is None or task["state"] != "open":
            raise PublishError("mark_binding")
        by_number = {step["n"]: step for step in task["steps"]}
        for number in mark["steps"]:
            step = by_number.get(number)
            if step is None or step["state"] not in ("open", "blocked"):
                raise PublishError("mark_binding")
            groups.setdefault(step["comment"], set()).add(step["ordinal"])
    if len(groups) > 4 or any(len(numbers) > 20 for numbers in groups.values()):
        raise PublishError("mark_binding")
    return groups


def _stamped_after(failure_at, at):
    """True when the failure's marker time is later than the decision's: a link points back in time only (the linter's
    rule), so such a pair is not linked."""
    if not (re.fullmatch(record_format.ISO, failure_at or "") and re.fullmatch(record_format.ISO, at or "")):
        return False
    return record_format.sort_key(failure_at) > record_format.sort_key(at)


def _link_because(marker, candidate, because):
    """Write `because=<the failure's comment id>` into the parsed marker of a decision drafted with a failure of its pass
    (candidate["because_with"]), once that failure is posted (`because`). True when the marker changed. Nothing is
    written when the failure is gone, when the id is there already or four ids are, or when the failure is stamped
    after the decision."""
    if not because or not candidate.get("because_with") or candidate["kind"] != "decision":
        return False
    current = marker.get("because", "")
    kept = current.split(",") if current and record_format.LINK_VALUE.fullmatch(current) else []
    wanted = str(because["comment"])
    if wanted in kept or len(kept) >= 4 or _stamped_after(because.get("at"), marker.get("at")):
        return False
    marker["because"] = ",".join(kept + [wanted])
    return True


def compose(event, db, allowed=None, opened=None, because=None):
    """The body an event is posted with: the reviewed candidate's body with its eventkey, and its structured marks and
    plan links written into the marker by code (spec 4.6). A pure function of the reviewed candidate, the index `db`
    (read for the tasks the marks name; None when no mark names a task of the index), the accounts `allowed`,
    `opened` (the steps memory of the task a task-0 mark is about: {"comment", "steps"}) and `because` (the failure a
    decision was drafted with, once posted: {"comment", "at"}; code writes `because=<comment>` from it, the model
    never does), so the same inputs give the same bytes. The model wrote no checks=, against=, done= or dropped= (a body
    that holds one is refused), and a plan link joins a checks= a body held before 1.5.0, and an against= written by a
    model is refused. Raises PublishError with mark_kind (marks on a kind that carries
    none), mark_binding (a mark that points at no open step) or part_binding (a plan link a kind may not carry, or more
    than four ids in one key)."""
    candidate = event["candidate"]
    base = observer_store._with_key(candidate["body"], event["eventkey"])
    lines, first, marker = _marker_split(base)
    if "done" in marker or "dropped" in marker:
        raise PublishError("mark_binding")
    marks, links = candidate.get("marks", []), candidate.get("links", [])
    if "against" in marker:
        raise PublishError("part_binding")   # only code writes it, from the candidate's links
    linked = _link_because(marker, candidate, because)
    if not marks and not links and not linked:
        return base
    if marks and candidate["kind"] not in MARK_KINDS:
        raise PublishError("mark_kind")
    for link in links:
        if candidate["kind"] not in PLAN_LINK_KINDS[link["key"]]:
            raise PublishError("part_binding")
    for key in ("checks", "against"):
        parts = []
        for link in links:
            if link["key"] == key and str(link["part"]) not in parts:
                parts.append(str(link["part"]))
        if not parts:
            continue
        current = marker.get(key, "")
        kept_ids = current.split(",") if current and record_format.LINK_VALUE.fullmatch(current) else []
        ids = kept_ids + [part for part in parts if part not in kept_ids]
        if len(ids) > 4:
            raise PublishError("part_binding")
        marker[key] = ",".join(ids)
    if marks:
        tasks = {}
        if any(mark["task"] != 0 for mark in marks):
            import index
            if db is None:
                raise PublishError("index_unavailable")
            try:
                tasks = {task["issue"]: task for task in index.tasks(db, allowed or set())["tasks"]}
            except sqlite3.Error:
                raise PublishError("index_unavailable")
        marker["done"] = _group_value(_resolve_marks(marks, tasks, opened))
    ending = "\r" if lines[first].endswith("\r") else ""
    lines[first] = "<!-- ghim:event " + " ".join("%s=%s" % pair for pair in marker.items()) + " -->" + ending
    return "\n".join(lines)


def _opener(store, event):
    """The event of the queue that opens the task `event` is about (a mark of task 0, or a checkpoint on issue 0): the one
    whose first source is the candidate's `with`, else the newest steps memory that opens a task and was queued at most
    FOLLOW_WINDOW seconds before it. None when the candidate is about no new task; mark_binding when there is none."""
    candidate = event["candidate"]
    if not any(mark["task"] == 0 for mark in candidate.get("marks", [])) and not (
            candidate["issue"] == 0 and candidate["kind"] != "steps"):
        return None
    found = store.event_by_source(candidate["with"]) if candidate.get("with") else next(
        (other for other in store.recent_events(event["created"] - FOLLOW_WINDOW)
         if other["created"] <= event["created"] and opens_task(other["candidate"])), None)
    if found is None or found["eventkey"] == event["eventkey"] or not opens_task(found["candidate"]):
        raise PublishError("mark_binding")
    return found


def _opened(store, event):
    """What a task-0 mark resolves against: {"issue", "comment", "steps", "key"} of the opener once it is verified; None
    for an event about no new task. A queue event that is still on its way (ready, deferred, sending, uncertain) makes
    this one wait (Waiting), a dropped one makes its mark mark_binding."""
    found = _opener(store, event)
    if found is None:
        return None
    if found["state"] == "verified":
        issue, comment = found["receipt"].get("issue"), found["receipt"].get("comment_id")
        if type(issue) is not int or type(comment) is not int:
            raise PublishError("mark_binding")
        return {"issue": issue, "comment": comment, "steps": len(record_format.steps_list(found["candidate"]["body"])),
                "key": found["eventkey"]}
    if found["state"] in ("rejected", "ignored"):
        raise PublishError("mark_binding")
    raise Waiting("depends", defer=found["state"] == "deferred")


def _because(store, event):
    """What a decision's because link resolves against: {"comment", "at"} of the failure it was drafted with, once that
    failure is posted; None when the event carries no such link, or the failure is gone (dropped, or left to the main
    session, which recorded its own): the decision then stands alone, as any decision does. A failure still on its way
    makes this one wait (Waiting), as a mark waits for its task; a held one makes it leave the ready list."""
    key = event["candidate"].get("because_with")
    found = store.event(key) if key else None
    if found is None:
        return None
    if found["state"] == "verified":
        comment = found["receipt"].get("comment_id")
        if type(comment) is not int or comment < 1:
            return None
        marker = record_format.parse_marker(record_format.first_lines(found["body"])[0]) or {}
        return {"comment": comment, "at": marker.get("at", "")}
    if found["state"] in ("rejected", "ignored") or (found["state"] == "deferred" and found["reason"] == "main_session"):
        return None
    raise Waiting("depends", defer=found["state"] == "deferred")


def _issue_of(store, event):
    """The issue the event is posted on: the candidate's, else (a checkpoint on the task its pass opened) the one it
    was resolved to; 0 while there is none, and for a steps memory that opens a task, which names no issue yet."""
    number = event["candidate"]["issue"]
    if number:
        return number
    found = store.get_control("resolved_issue", {})
    value = found.get(event["eventkey"]) if isinstance(found, dict) else None
    return value if type(value) is int and value > 0 else 0


def _composed(store, event):
    """(the composed body, the opened task or None) of a ready or deferred event, read-only. Raises Waiting and
    PublishError as compose and _opened do."""
    candidate = event["candidate"]
    opened = _opened(store, event)
    linked = _because(store, event)
    db = None
    allowed = recortex.allowed_users(store.repo)
    try:
        if any(mark["task"] != 0 for mark in candidate.get("marks", [])):
            import index
            db = index.connect(store.repo, create=False)
            if db is None:
                raise PublishError("index_unavailable")
        return compose(event, db, allowed, opened, linked), opened
    finally:
        if db is not None:
            db.close()


def _compose(store, event):
    """Compose the event's body, freeze it in the queue when it differs from the one stored, and remember the issue a
    checkpoint on a new task is posted on. Returns the event as stored."""
    body, opened = _composed(store, event)
    if body != event["body"]:
        store.recompose(event["eventkey"], body)
        event = store.event(event["eventkey"])
    if opened is not None and not event["candidate"]["issue"]:
        resolved = store.get_control("resolved_issue", {})
        resolved = dict(list(resolved.items())[-199:]) if isinstance(resolved, dict) else {}
        resolved[event["eventkey"]] = opened["issue"]
        store.set_control("resolved_issue", resolved)
    return event


def _waits(store, event, wait):
    """What publish returns for an event that waits for another: it stays ready (nothing changes), or, when what it waits
    for is held for the user's answer, it is deferred with the reason `depends` until that event is opened or dropped
    (release_dependents)."""
    if wait.defer and event["state"] == "ready":
        store.transition(event["eventkey"], "deferred", reason="depends")
        event = store.event(event["eventkey"])
    return {"eventkey": event["eventkey"], "state": event["state"], "waiting": wait.reason}


def release_dependents(store, opener, opened):
    """After the event that opens a task is verified (`opened` True: its dependents become ready again) or dropped (False:
    they are discarded with mark_binding, there being no task to mark). Returns the keys of the events moved."""
    moved = []
    for event in store.events(states=["deferred"], limit=1000):
        if event["reason"] != "depends":
            continue
        wanted = event["candidate"].get("with")
        if (wanted is not None and wanted != opener["source_id"]) or (wanted is None and "because_with" in event["candidate"]):
            continue   # a decision that waits for a failure (release_because) waits for no task
        if opened:
            store.transition(event["eventkey"], "ready")
        else:
            store.transition(event["eventkey"], "rejected", reason="mark_binding")
        moved.append(event["eventkey"])
    return moved


def release_because(store, failure):
    """After a failure is posted or dropped, the decisions drafted with it (candidate["because_with"]) go on: ready again.
    A posted failure gives them their link; a dropped one leaves each to stand alone. Returns the keys moved."""
    moved = []
    for event in store.events(states=["deferred"], limit=1000):
        if event["reason"] == "depends" and event["candidate"].get("because_with") == failure["eventkey"]:
            store.transition(event["eventkey"], "ready")
            moved.append(event["eventkey"])
    return moved


def _level(settings, event, main_session):
    """The level gate (spec 3.1), the publisher's second guard after the worker's. Notes only keeps memories and the
    status summary: no steps memory and no mark. Assistant keeps to-do items, marks, renders and reports on its own and
    asks before it opens a task (level_ask: the event is held; the user's "Open it" lets the main session publish it,
    with the grant the answer made). Everything opens tasks on its own."""
    candidate, level = event["candidate"], settings["level"]
    if level == "notes" and (candidate["kind"] == "steps" or candidate.get("marks")):
        raise PublishError("level")
    if level == "assistant" and opens_task(candidate):
        answered = main_session and event["state"] == "deferred" and event["reason"] in observer_store.TASK_ASKS
        if not answered:
            raise PublishError("level_ask")


def _tasks_full(store, settings):
    """True when max_tasks tasks were opened in the last 24 hours."""
    now = time.time()
    return len([at for at in store.get_control("automatic_tasks", []) if type(at) in (int, float)
                and 0 <= now - at < 86400]) >= settings["max_tasks"]


def _note_task(store):
    now = time.time()
    store.set_control("automatic_tasks", [at for at in store.get_control("automatic_tasks", [])
                                          if type(at) in (int, float) and 0 <= now - at < 86400] + [now])


def title_problem(title, repo, config):
    """Why a task's title may not go to GitHub, or None. The title is a model's text that reaches GitHub apart from the
    body, so it gets the body's checks. Its form: one line of at most TITLE_CAP characters, with no control character,
    no format character (the bidirectional controls and zero-width ones are format characters), no line or paragraph
    separator and no space but the plain one: title_form. Then the outbound checks (no markup, mention, address, outside
    reference, nothing of this machine: outbound_problem) and the project's own (a secret, a restricted pattern, a
    banned word, a placeholder or an unfilled template field: title_restricted)."""
    if (not isinstance(title, str) or not title.strip() or len(title) > TITLE_CAP
            or any(unicodedata.category(char)[0] == "C" or unicodedata.category(char) in ("Zl", "Zp")
                   or (char.isspace() and char != " ") for char in title)):
        return "title_form"
    problem = outbound_problem(title, repo, status_ok=True)
    if problem:
        return problem
    import lint_drafts as lint
    if any(f.level == "error" for f in lint.lint_text(title, kind="generic", config=config)):
        return "title_restricted"
    return None


def _task_command(repo, event):
    return ["issue", "create", "--repo", repo, "--title", event["candidate"]["task_title"], "--body-file", "-"]


def _run_open_task(store, event, config):
    """Open the task through tasks.open_task (create_issues.py with a manifest in a private folder named after the
    drafts, so that a rerun finds its state file and opens no second task) and read the first comment back. Returns the
    receipt: the comment's, with the issue it opened. Nothing the scripts print is kept or echoed."""
    import tasks
    candidate, repo = event["candidate"], store.repo
    folder = tasks.scratch(repo, "observer", event["eventkey"])
    tasks.write(folder, "steps.md", event["body"])
    tasks.write(folder, "task.json", json.dumps({"title": candidate["task_title"], "topic": candidate["topic"]}))
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        result = tasks.open_task(repo, folder, config=config)
    if not result.get("ok") or type(result.get("issue")) is not int or type(result.get("comment")) is not int:
        raise PublishError("task_failed")
    receipt = _check_remote(repo, event, _remote_comment(repo, result["comment"], config), issue=result["issue"],
                            config=config)
    receipt["issue"] = result["issue"]
    return receipt


def _send_task(store, event, config):
    key = event["eventkey"]
    try:
        receipt = _run_open_task(store, event, config)
    except (recortex.GhError, ValueError, KeyError, TypeError, OSError, SystemExit, PublishError) as error:
        return _uncertain(store, key, getattr(error, "reason", "task_failed"))
    store.transition(key, "verified", receipt=receipt)
    _after_verified(store, event, receipt)
    return {"eventkey": key, "state": "verified", "receipt": receipt}


def _after_verified(store, event, receipt):
    """What follows a verified post: the record is read again, the act is logged (never a reason to fail the post) and,
    when the event opened a task, the events that waited for it go on."""
    store.set_control("sync_needed", True)
    candidate = event["candidate"]
    try:
        if opens_task(candidate):
            store.add_act("task_opened", issue=receipt.get("issue", 0), comment=receipt["comment_id"],
                          numbers=[len(record_format.steps_list(candidate["body"]))])
        elif candidate["kind"] == "steps":
            store.add_act("steps_added", issue=candidate["issue"], comment=receipt["comment_id"],
                          numbers=[len(record_format.steps_list(candidate["body"]))])
        elif candidate.get("marks"):
            store.add_act("step_marked", issue=_issue_of(store, event), comment=receipt["comment_id"],
                          numbers=[sum(len(mark["steps"]) for mark in candidate["marks"])])
        else:
            store.add_act("memory", issue=candidate["issue"], comment=receipt["comment_id"])
    except (observer_store.StoreError, ValueError, TypeError):
        _note_act_failed(store)   # the post stands; the failure is counted and told (notice, status, --doctor)
    if opens_task(candidate):
        release_dependents(store, event, True)
    if candidate["kind"] == "failure":
        release_because(store, event)


def _note_act_failed(store):
    """Count a write of the acts log that failed (Store.note_act_failure). Nothing here can fail the post."""
    try:
        store.note_act_failure()
    except Exception:  # noqa: BLE001 - a log that cannot say it failed is the log's problem, never the post's
        pass


def _daily_cap(store, settings):
    """At most max_posts automatic comments in any 24 hours for this project; the rest waits in the queue. Counted
    under the publication lock, before the request."""
    now = time.time()
    sent = [at for at in store.get_control("automatic_posts", [])
            if type(at) in (int, float) and 0 <= now - at < 86400]
    if len(sent) >= settings["max_posts"]:
        raise PublishError("daily_cap")
    store.set_control("automatic_posts", sent + [now])


def _payload_bound(event):
    """The frozen published body survives private excerpt retention unchanged."""
    if event is None:
        raise PublishError("missing_event")
    if hashlib.sha256(event["body"].encode("utf-8")).hexdigest() != event["payload_hash"]:
        raise PublishError("payload_changed")
    marker = record_format.parse_marker(record_format.first_lines(event["body"])[0]) or {}
    if marker.get("eventkey") != event["eventkey"] or marker.get("type") != event["candidate"]["kind"]:
        raise PublishError("payload_changed")
    if event["state"] == "verified" and (event["receipt"].get("payload_hash") != event["payload_hash"] or
                                           event["receipt"].get("account") != event["account"]):
        raise PublishError("receipt_changed")


def _bound(event, store):
    _payload_bound(event)
    if (event["review"].get("accepted") is not True and not withheld_review(event)) \
            or event["review"].get("candidate_hash") != candidate_hash(event["candidate"]):
        raise PublishError("review_changed")   # a withheld review passes here only for a post the user's answer allows
    if not store.review_valid(event):
        raise PublishError("review_changed")


def _account(repo, config=None):
    me = record_store.for_repo(repo, config).whoami(host=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", me) or me.lower() not in recortex.allowed_users(repo):
        raise PublishError("account_not_allowed")
    return me.lower()


def _command(repo, event, issue=None):
    return ["issue", "comment", str(issue or event["candidate"]["issue"]), "--repo", repo, "--body-file", "-"]


def hold_reason(store, event):
    """The user's own hold on this event's comment, as a reason of observer_holds.HELD_REASONS, or None: a /recortex
    hold that covers the command (explicit_hold), or the session kept off the record (withheld)."""
    issue = _issue_of(store, event)
    command = recortex.show_command(_task_command(store.repo, event) if opens_task(event["candidate"])
                                    else _command(store.repo, event, issue))
    observation = store.observation(event["source_id"])
    session = observation["metadata"].get("session_id") if observation else None
    holds = store.holds()
    answer = observer_holds.check_action(holds, store.repo, "Bash", {"command": command},
                                         session_id=session, issue=issue or event["candidate"].get("topic", 0))
    if answer["decision"] == "deny":
        return "explicit_hold"
    # The user asked in a session that its moments not be posted ("don't post", "keep this off the record"): nothing
    # of it posts, the main session's --eventkey included, until the user releases that hold in their own terminal
    # (observer.py release HOLD_ID), which is the later, explicit request.
    sessions = {item["metadata"].get("session_id") for item in (store.observation(sid) for sid in
                                                                 event["candidate"]["sources"]) if item}
    if observer_holds.publication_withheld(holds, store.repo, sessions | {session}):
        return "withheld"
    return None


def _hold(store, event, main_session=False):
    reason = hold_reason(store, event)
    if reason:
        raise PublishError(reason)


def _remote_comment(repo, comment_id, config=None):
    data = record_store.for_repo(repo, config).read_entry(int(comment_id))
    if not isinstance(data, dict):
        raise PublishError("invalid_receipt")
    return data


def _check_remote(repo, event, data, issue=None, config=None):
    cid = data.get("id")
    if isinstance(cid, bool) or not isinstance(cid, int) or cid < 1:
        raise PublishError("invalid_receipt")
    issue = issue or event["candidate"]["issue"]
    records = record_store.for_repo(repo, config)
    expected = records.api_issue(issue)
    if data.get("issue_url", "").lower() != expected.lower():
        raise PublishError("wrong_target")
    if (data.get("user") or {}).get("login", "").lower() != event["account"]:
        raise PublishError("wrong_author")
    if event["account"] not in recortex.allowed_users(repo):
        raise PublishError("account_not_allowed")
    if not data.get("created_at") or data.get("created_at") != data.get("updated_at"):
        raise PublishError("remote_edited")
    if recortex.normalize(data.get("body") or "") != recortex.normalize(event["body"]):
        raise PublishError("remote_changed")
    return {"comment_id": cid, "url": records.address(issue, cid),
            "payload_hash": event["payload_hash"], "account": event["account"]}


def _find_remote(repo, event, issue=None, config=None):
    number = issue or event["candidate"]["issue"]
    rows = recortex.parse_json_stream(record_store.for_repo(repo, config).list_entries(number))
    found = []
    for row in rows:
        if not isinstance(row, dict):
            raise PublishError("invalid_remote")
        marker = record_format.parse_marker(record_format.first_lines(row.get("body") or "")[0]) or {}
        if marker.get("eventkey") == event["eventkey"]:
            found.append(row)
    if len(found) > 1:
        raise PublishError("duplicate_event")
    return found[0] if found else None


def _uncertain(store, key, reason):
    event = store.event(key)
    if event["state"] == "sending":
        store.transition(key, "uncertain", reason=reason)
    return {"eventkey": key, "state": "uncertain", "reason": reason}


def _reconcile_locked(store, event, config=None):
    if event["state"] == "verified":
        _payload_bound(event)
        return {"eventkey": event["eventkey"], "state": "verified", "receipt": event["receipt"]}
    _bound(event, store)
    if event["state"] not in ("sending", "uncertain"):
        raise PublishError("not_uncertain")
    if opens_task(event["candidate"]):
        # The task's state file (create_issues.py's) knows what was made: running it again opens no second task, and it
        # adopts a comment an earlier run posted and did not finish reading back.
        return _send_task(store, event, config if config is not None else {})
    try:
        found = _find_remote(store.repo, event, _issue_of(store, event), config)
        if found is None:
            return _uncertain(store, event["eventkey"], "remote_not_found")
        # Verify the individual comment, not only a potentially stale listing.
        receipt = _check_remote(store.repo, event, _remote_comment(store.repo, found["id"], config),
                                issue=_issue_of(store, event), config=config)
        store.transition(event["eventkey"], "verified", receipt=receipt)
        _after_verified(store, event, receipt)
        return {"eventkey": event["eventkey"], "state": "verified", "receipt": receipt}
    except (recortex.GhError, ValueError, KeyError, TypeError, PublishError) as error:
        return _uncertain(store, event["eventkey"], getattr(error, "reason", "remote_unavailable"))


def _dry_run(repo, store, event, config):
    """What publish would send, without network and without writing: the command and the hash of the body composed now."""
    candidate = event["candidate"]
    records = record_store.for_repo(repo, config)
    note = ("a task from the frozen reviewed steps memory, which is then its first comment" if opens_task(candidate)
            else "frozen reviewed body from stdin")
    shown = (records.show("create_issue", candidate["task_title"], [], note=note) if opens_task(candidate)
             else records.show("post_entry", _issue_of(store, event) or candidate["issue"], note=note))
    out = {"eventkey": event["eventkey"], "state": event["state"], "dry_run": True,
           "command": shown, "payload_hash": event["payload_hash"]}
    if opens_task(candidate):
        problem = title_problem(candidate["task_title"], repo, config)
        if problem:
            out["title"] = problem
    if event["state"] in ("ready", "deferred"):
        try:
            body, _ = _composed(store, event)
            out["payload_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        except Waiting as wait:
            out["waiting"] = wait.reason
        except PublishError as error:
            out["compose"] = error.reason
    return out


def publish(repo, eventkey, config, dry_run=False, main_session=False):
    """Append one frozen reviewed event. A dry run returns the exact command without network or writes.
    main_session=True is post_verified.py --eventkey, run by the main session after its own review: it may post a
    deferred event, one the automatic gate held back (held_back) included, which it posts on the user's answer to its
    question. The user's own holds (hold_reason) bind it as they bind the automatic publisher.

    An event whose candidate opens a task (a steps memory on issue 0) opens it through tasks.open_task, then reads the
    first comment back; one whose marks name that task waits until it is verified (the return holds "waiting")."""
    settings = observer_runtime.settings(config)
    with Store(repo, config=config, create=False, readonly=dry_run, max_bytes=settings["max_pending_bytes"],
               max_pending=settings["max_pending_observations"]) as store:
        event = store.event(eventkey)
        if event is None:
            raise PublishError("missing_event")
        if dry_run:
            return _dry_run(repo, store, event, config)
        with store.publication_lock():
            settings = _gate(store, config, main_session)
            event = store.event(eventkey)
            if event["state"] == "verified":
                _payload_bound(event)
                return {"eventkey": eventkey, "state": "verified", "receipt": event["receipt"]}
            if event["state"] in ("sending", "uncertain"):
                return _reconcile_locked(store, event, config)
            # A deferred event waits for the main session, which may post it after its own review.
            if event["state"] != "ready" and not (main_session and event["state"] == "deferred"):
                raise PublishError("not_ready")
            _level(settings, event, main_session)
            try:
                event = _compose(store, event)
            except Waiting as wait:
                return _waits(store, event, wait)
            waiting = held_back(store, event, config)
            if waiting and not main_session:
                raise PublishError(waiting[0])  # deferred and marked held: it waits for the user's answer
            # The main session posts a held event only on the user's own answer, which the hook before its Bash call
            # found in the transcript and turned into a one-time grant (observer_hooks._grant).
            answer_needed = main_session and bool(waiting or event["reason"] in observer_holds.ANSWER_REASONS)
            if answer_needed and not store.has_grant(eventkey):
                raise PublishError("held_unanswered")
            _bound(event, store)
            problem = outbound_problem(event["body"], repo, status_ok=main_session or
                                       event["review"].get("status_authority") in STATUS_AUTHORITY)
            if problem:
                raise PublishError(problem)
            candidate, opens = event["candidate"], opens_task(event["candidate"])
            problem = title_problem(candidate["task_title"], repo, config) if opens else None
            if problem:
                raise PublishError(problem)   # the title is the model's text too: nothing is created
            _hold(store, event, main_session)
            me = _account(repo, config)
            issue = candidate["topic"] if opens else _issue_of(store, event)
            if opens and not issue and not task_rules_todo(candidate["task_title"]):
                raise PublishError("invalid_target")   # a task with no topic is the standing "To do" list only
            # Target existence and draft rules are checked on the live record before sending.
            record = None
            if issue:
                target = record_store.for_repo(repo, config).read_issue(issue)
                if not isinstance(target, dict) or target.get("pull_request"):
                    raise PublishError("invalid_target")
                record = lookup.github(repo, issue)
            try:
                findings = lint_drafts.lint_text(event["body"], kind=candidate["kind"], config=config,
                                                number_checker=lambda n: recortex.number_exists(repo, n), lookup=record)
            finally:
                if record is not None:
                    record.close()
            if any(f.level == "error" for f in findings):
                raise PublishError("lint_failed")
            settings = _gate(store, config, main_session)
            _hold(store, event, main_session)
            if _account(repo, config) != me:
                raise PublishError("account_changed")
            if not main_session:
                if opens and _tasks_full(store, settings):
                    store.set_control("task_cap_at", time.time())   # said once, by the one who reads it (status, brief)
                    return {"eventkey": eventkey, "state": event["state"], "waiting": "daily_tasks"}
                _daily_cap(store, settings)
                if opens:
                    _note_task(store)
            if answer_needed and not store.take_grant(eventkey):
                raise PublishError("held_unanswered")  # consumed at the last step: one answer, one post
            # Account and intent are durable before the first possible remote write.
            store.transition(eventkey, "sending", account=me)
            event = store.event(eventkey)
            if opens:
                _gate(store, config, main_session)
                _hold(store, event, main_session)
                return _send_task(store, event, config)
            try:
                prior = _find_remote(repo, event, issue, config)
                if prior is not None:
                    return _reconcile_locked(store, event, config)
                _gate(store, config, main_session)
                _hold(store, event, main_session)
                if _account(repo, config) != event["account"]:
                    raise PublishError("account_changed")
                records = record_store.for_repo(repo, config)
                output = records.post_entry(issue or event["candidate"]["issue"], body=event["body"])
                posted = records.posted_id(output, issue or event["candidate"]["issue"])
                if not posted:
                    return _uncertain(store, eventkey, "receipt_missing")
                receipt = _check_remote(repo, event, _remote_comment(repo, int(posted), config), issue=issue,
                                        config=config)
                store.transition(eventkey, "verified", receipt=receipt)
                _after_verified(store, event, receipt)
                return {"eventkey": eventkey, "state": "verified", "receipt": receipt}
            except (recortex.GhError, ValueError, KeyError, TypeError, PublishError) as error:
                return _uncertain(store, eventkey, getattr(error, "reason", "request_failed"))


def task_rules_todo(title):
    """True for the title of the standing to-do list ("To do"), the one task that names no topic."""
    import task_rules
    return task_rules.is_todo_title(title)


def reconcile(repo, eventkey, config, dry_run=False):
    """Read the remote target for an uncertain send; never append or blindly retry. (A task that was being opened is
    taken up again from the state file of its own creation, which opens no second one: that is the one write here, and
    it needs the automatic gate.)"""
    with Store(repo, config=config, create=False, readonly=dry_run) as store:
        event = store.event(eventkey)
        if event is None:
            raise PublishError("missing_event")
        if dry_run:
            return {"eventkey": eventkey, "state": event["state"], "dry_run": True,
                    "command": record_store.for_repo(repo, config).show(
                        "list_entries", _issue_of(store, event) or event["candidate"]["issue"])}
        with store.publication_lock():
            # A pause stops new writes, not read-only receipt reconciliation.
            if repo.lower() not in recortex.trusted_repos():
                raise PublishError("not_trusted")
            if not observer_runtime.approval_valid(repo, config):
                raise PublishError("not_approved")
            if opens_task(event["candidate"]) and event["state"] in ("sending", "uncertain"):
                _gate(store, config)
            return _reconcile_locked(store, event, config)

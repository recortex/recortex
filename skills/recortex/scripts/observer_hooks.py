"""Opted-in local capture and explicit holds; no network or inference in a hook.

The native transcript, not hook prompt text or a model result, establishes the
source role. Collection and controls are separate from the disposable index.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import shlex
import subprocess
import sys
import time

import datetime
import keeper_rules
import names
import observer_holds
import observer_runtime
import observer_sources
import observer_store
import pretool_guard
import record_store
import recortex
import storage

EVENTS = {"session-start": "SessionStart", "subagent-start": "SubagentStart",
          "prompt-submit": "UserPromptSubmit", "pre-compact": "PreCompact",
          "pre-tool": "PreToolUse", "stop": "Stop"}  # what hooks/hooks.json registers
NOTICES = {
    "approval": "recortex observer: local approval is missing or settings changed; observation is inactive.",
    "approval-host": "recortex observer: this folder's approval does not hold on this host; observation is inactive. "
                     "Run observer.py status.",
    "capture": "recortex observer: capture is incomplete; pending work remains local. Run observer.py status.",
    "source": "recortex observer: transcript sources are unavailable or incomplete; new holds may not be active. Run observer.py status.",
    "runtime": "recortex observer: independent checks are unavailable; captured work remains queued. Run observer.py status.",
    "context": "recortex observer: task context is unavailable; read the current record before relying on it.",
    "pointer": "recortex observer: the reminder about a changed decision was left out: checking the record took too long. "
               "The rest of the context is shown. Run hooks.py --doctor.",
    "paused": "recortex observer: capture is paused; earlier holds remain active. Run observer.py status.",
    "control": "recortex observer: an explicit hold was recorded. Run observer.py holds.",
    "worker": "recortex observer: the worker could not start; captured work remains queued. Run observer.py status.",
    "pending": "recortex observer: captured work is still pending; it is not yet verified in GitHub. Run observer.py status.",
    "budget": "recortex observer: the model budget is exhausted; captured work remains queued. Run observer.py status.",
    "control-refused": "recortex observer: an explicit control could not be stored safely; that control is not active. Run observer.py status.",
    "cap": "recortex observer: today's limit of automatic comments is reached; reviewed memories wait in the queue. Run observer.py pending.",
    "release-refused": "recortex observer: a hold is released on the user's answer to its question. Run acts.py ask release HOLD_ID and ask it. Run observer.py holds.",
    "held": "recortex observer: items wait for your answer; the session asks you about each. observer.py pending --waiting lists them.",
}
# At session start, for the main session, while anything waits for the user's answer (the number is the only value in
# it): a held memory, a task to open or close, a draft of the paper's text. The session loads the skill, which gives the
# scripts folder, and asks each item from the template `observer.py pending --waiting` prints.
WAITING = ("recortex observer: %s for the user's answer. Load the recortex skill first, run its observer.py pending "
           "--waiting, and ask each as one multiple-choice question from the template it prints. Do not offer them in "
           "prose.")
# On a later prompt of the same session, while items wait and `pending --waiting` has not run in the session: 1.4.0's live
# runs showed that Sonnet and Haiku act on a reminder at the next prompt, not on the line at session start alone.
REMINDER = ("recortex observer: %s for the user's answer. Before this reply ends, load the recortex skill, run its "
            "observer.py pending --waiting and ask each as a multiple-choice question.")
# The bookkeeping is setup_questions.reminder's (a test holds the numbers equal): at most REMIND_MAX reminders to one
# session, the first with its REMIND_FROM-th prompt, REMIND_GAP prompts apart, none once `pending --waiting` ran.
REMIND_MAX, REMIND_FROM, REMIND_GAP = 3, 2, 3
REMIND = "waiting_remind"   # store control: {session hash: {"count", "reminded", "at", "ran"}}, the 50 newest sessions
# At session start: what the observer did on its own since the last time this line was shown (fixed words and integers
# only), at most once in ACTS_HOURS per session.
ACTS = "recortex observer: since %s it %s. observer.py log lists them."
ACTS_HOURS = 6
ACTS_TOLD = "acts_told"     # store control: {"at": when the line was last shown for the folder, "sessions": {hash: at}}
ACTS_DAYS = 7               # the line looks back this far at most
TASKS_CAP = ("recortex observer: today's limit of automatic tasks is reached; a task waits in the queue. Run "
             "observer.py pending.")
# At session start, while the log of what the observer did keeps failing (a post stands and its act could not be written):
# one fixed line with a whole number, at most once a day. The count and the day of the last failure are the store's control.
ACTS_FAILED = "recortex observer: the log of what it did could not be written %d time(s); run hooks.py --doctor."
SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def _items(count):
    return "1 item waits" if count == 1 else "%d items wait" % count


def waiting_line(count):
    """WAITING for `count` items: the number, in the singular for one, is its only value."""
    return WAITING % _items(count)


def reminder_line(count):
    """REMINDER for `count` items still waiting."""
    return REMINDER % ("1 item still waits" if count == 1 else "%d items still wait" % count)


def _joined(parts):
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def acts_text(acts):
    """What the acts say, in fixed words and whole numbers: posted N memories, checked N steps, opened N tasks, closed
    task #14 ..., or "" when there is nothing to say. `acts` are the rows of Store.acts (undone ones are left out)."""
    def count(kind):
        return [row for row in acts if row["kind"] == kind and not row["undone"]]
    parts = []

    def plural(n, one, many):
        return "%d %s" % (n, one if n == 1 else many)
    if count("memory"):
        parts.append("posted " + plural(len(count("memory")), "memory", "memories"))
    steps = sum(n for row in count("step_marked") for n in row["numbers"][:1])
    if steps:
        parts.append("checked " + plural(steps, "step", "steps"))
    if count("task_opened"):
        parts.append("opened " + plural(len(count("task_opened")), "task", "tasks"))
    closed = count("task_closed")
    if closed:
        parts.append("closed task #%d" % closed[0]["issue"] if len(closed) == 1 and closed[0]["issue"]
                     else "closed " + plural(len(closed), "task", "tasks"))
    if count("task_rendered"):
        parts.append("rendered " + plural(len(count("task_rendered")), "task list", "task lists"))
    if count("hub_rendered"):
        parts.append("rendered the hub's active issues")
    if count("board"):
        parts.append("updated the board")
    return _joined(parts[:6]) if parts else ""


def _acts_line(store, data, config):
    """The acts line, or "": at a session start, when the observer did something since the line was last shown here, and
    not more than once in ACTS_HOURS for a session. The stamp comes from the record's own zone (recortex.stamp)."""
    session = data.get("session_id", "")
    if not isinstance(session, str) or not observer_sources.SESSION.fullmatch(session):
        return ""
    key = hashlib.sha256(session.encode()).hexdigest()[:24]
    now = time.time()
    told = store.get_control(ACTS_TOLD, {})
    told = told if isinstance(told, dict) else {}
    sessions = told.get("sessions") if isinstance(told.get("sessions"), dict) else {}
    last = sessions.get(key)
    if type(last) in (int, float) and now - last < ACTS_HOURS * 3600:
        return ""
    since = told.get("at")
    since = float(since) if type(since) in (int, float) and math.isfinite(since) and since > 0 else 0.0
    since = max(since, now - ACTS_DAYS * 86400)
    text = acts_text([row for row in store.acts(days=ACTS_DAYS) if row["at"] > since])
    if not text:
        return ""
    sessions[key] = now
    store.set_control(ACTS_TOLD, {"at": now, "sessions": dict(sorted(sessions.items(), key=lambda pair: pair[1])[-50:])})
    moment = datetime.datetime.fromtimestamp(since, datetime.timezone.utc)
    return ACTS % (recortex.stamp(moment, config), text)


FIGURES_DAYS = 3   # a count the keeper made longer ago than this is not told


def _figures_line(store):
    """keeper_rules.figures_line for the count the keeper left in the store (an integer and its time), while it is recent;
    else "". Nothing is computed from the plan here."""
    found = store.get_control("keeper_figures", {})
    if not isinstance(found, dict) or type(found.get("n")) is not int or type(found.get("at")) not in (int, float):
        return ""
    if not 0 < found["n"] <= 10 ** 6 or time.time() - found["at"] > FIGURES_DAYS * 86400:
        return ""
    return keeper_rules.figures_line(found["n"])


def _tasks_cap_line(store):
    """TASKS_CAP once for each time the publisher met the daily cap of tasks (the store control it sets), else ""."""
    at, told = store.get_control("task_cap_at", 0), store.get_control("task_cap_told", 0)
    if type(at) in (int, float) and math.isfinite(at) and at > 0 and not (type(told) in (int, float) and told >= at):
        store.set_control("task_cap_told", at)
        return TASKS_CAP
    return ""


def _acts_failed_line(store):
    """ACTS_FAILED with the count while the log of acts keeps failing (its last failure is within ACTS_FAILED_DAYS), once a
    UTC day; else "". Nothing but the control's whole number reaches the line."""
    failed = store.acts_failed()
    now = time.time()
    if failed is None or failed["day"] < observer_store.day_of(now - observer_store.ACTS_FAILED_DAYS * 86400):
        return ""
    today = observer_store.day_of(now)
    if store.get_control(observer_store.ACTS_FAILED_TOLD) == today:
        return ""
    store.set_control(observer_store.ACTS_FAILED_TOLD, today)
    return ACTS_FAILED % failed["n"]


def _ran_waiting(data):
    """True for a Bash call that runs `observer.py pending --waiting` or `status.py --waiting`, read as the shell splits it."""
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str) or "--waiting" not in command:
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    names_ = [os.path.basename(word) for word in words]
    return "--waiting" in words and ("observer.py" in names_ and "pending" in words or "status.py" in names_)


def _session_state(store, data):
    """(key, the session's entry of the REMIND control as a dict, the whole control) or (None, None, None)."""
    session = data.get("session_id", "")
    if not isinstance(session, str) or not observer_sources.SESSION.fullmatch(session):
        return None, None, None
    key = hashlib.sha256(session.encode()).hexdigest()[:24]
    control = store.get_control(REMIND, {})
    control = control if isinstance(control, dict) else {}
    entry = control.get(key) if isinstance(control.get(key), dict) else {}
    entry = {"count": entry.get("count") if type(entry.get("count")) is int else 0,
             "reminded": entry.get("reminded") if type(entry.get("reminded")) is int else 0,
             "at": entry.get("at") if type(entry.get("at")) is int else 0,
             "ran": entry.get("ran") is True, "time": time.time()}
    return key, entry, control


def _save_state(store, key, entry, control):
    control[key] = entry
    store.set_control(REMIND, dict(sorted(control.items(), key=lambda pair: pair[1].get("time", 0))[-50:]))


def _note_ran(store, data):
    """Note that `pending --waiting` ran in this session (the Bash call that runs it), so that no reminder follows."""
    key, entry, control = _session_state(store, data)
    if key is not None and not entry["ran"]:
        entry["ran"] = True
        _save_state(store, key, entry, control)


def _reminder(store, data):
    """The reminder line for this prompt, or "": counts the session's prompts, and gives REMINDER while items wait, the
    session's `pending --waiting` has not run, fewer than REMIND_MAX were given, from the REMIND_FROM-th prompt and
    REMIND_GAP prompts apart. Nothing is read from the store but the count of what waits."""
    key, entry, control = _session_state(store, data)
    if key is None:
        return ""
    entry["count"] += 1
    line = ""
    if (not entry["ran"] and entry["count"] >= REMIND_FROM and entry["reminded"] < REMIND_MAX
            and (not entry["reminded"] or entry["count"] - entry["at"] >= REMIND_GAP)):
        waiting = len(store.waiting())
        if waiting:
            line = reminder_line(waiting)
            entry["reminded"], entry["at"] = entry["reminded"] + 1, entry["count"]
    _save_state(store, key, entry, control)
    return line


# At the next session start after a session switched automatic mode on, and again whenever the level or the stronger model
# changes: the date, the level's name and whether paper proposals (the stronger model) are on are the only values in it.
SWITCHED = ("recortex observer: automatic mode was switched on for this folder on %s from a session, at the level %s, "
            "with paper proposals %s; tell the session to turn it off.")
TOOL_EVENTS = ("PreToolUse",)
SCAN_ROUNDS = 16  # bounded scans one hook makes to catch up with its transcript, at the edges of a turn
SCAN_SECONDS = 1.0  # and the time they may take; before a Bash call one scan only, so that the hold check is never late
STALE = 900  # seconds after which new captured work that no worker has taken up is worth a notice
UNCHECKED = {"reason": "recortex observer: this machine's holds could not be checked, so every command but pwd, ls and "
                       "cat is stopped. Run observer.py status by its full path with python3 and nothing else on the line, and "
                       "tell the user what it says."}
# Lines that answer something the user just did (a hold, a release); each arises once, so it is never held back.
ANSWERS = frozenset(("control", "control-refused", "release-refused"))


def _notice(code, repo=None):
    """The fixed line of a notice; for a local record it names the record where it would name GitHub."""
    text = NOTICES[code]
    return text.replace("verified in GitHub", "verified in the record") if repo and record_store.is_local(repo) else text


def _output(event, context="", notices=(), denial=None, lines=(), repo=None):
    """The hook's JSON. `lines` are notices built here from fixed words and a checked value (SWITCHED)."""
    if event == "PreCompact":
        return {}  # Claude Code discards a PreCompact hook's systemMessage; its capture has run
    out = {}
    if notices or lines:
        out["systemMessage"] = "\n".join(dict.fromkeys([_notice(code, repo) for code in notices] + list(lines)))
    if context or denial:
        specific = {"hookEventName": event}
        if context and event not in ("Stop", "PreCompact"):
            specific["additionalContext"] = context
        if denial:
            specific.update(permissionDecision="deny", permissionDecisionReason=denial["reason"])
        if len(specific) > 1:
            out["hookSpecificOutput"] = specific
    return out


def launch_worker(project_root):
    """Detach a finite worker; its repository lock prevents competing workers."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "observer.py")
    subprocess.Popen([sys.executable, path, "run-worker"], cwd=project_root,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def _controls(store, rows, project_root, repo, config):
    """Holds from the user's messages in the transcript. A release found there is never applied: only observer.py
    release, run by the user in a terminal, releases a hold. When it names a hold that is still active, the third value
    says so, and the session is told how to release it."""
    controls, refused, unproven = [], False, False
    for row in rows:
        if row.get("role") != "user":
            continue
        source = observer_sources.resolve(row, project_root)
        control = observer_holds.compile_prompt_control(source["text"], source, repo)
        if control and control["operation"] == "release":
            unproven = unproven or any(hold.get("id") == control["hold_id"] and hold.get("state") == "active"
                                       for hold in store.holds())
            continue
        if control:
            import lint_drafts  # only when a control is found: every Bash call starts this hook
            # What is stored is the hold's quote: the whole message, or only the phrase that withheld a session.
            if any(f.code in ("secret", "restricted") for f in lint_drafts.lint_text(control["hold"]["quote"],
                                                                                     config=config)):
                refused = True
                continue
            controls.append((control, source))
    if not controls:
        return False, refused, unproven

    def change(holds):
        changed = list(holds)
        for control, source in controls:
            hold = control["hold"]
            if not any(item.get("id") == hold["id"] for item in changed):
                changed.append(hold)
        return changed

    store.update_holds(change)
    return True, refused, unproven


def _capture(store, event, data, project_root, repo, config, capture=True, rounds=SCAN_ROUNDS):
    """(controlled, behind, refused, unproven, stale). Before a Bash call (one round), a transcript left unread past
    the scan is searched for controls too (observer_sources.control_tail); stale is True when that search could not
    reach its end in time, so that holds typed there may be missing."""
    # PreToolUse rescans the persisted transcript synchronously: never wait for a
    # model/worker to compile a recognized original-user hold before an action.
    scan_data = dict(data, hook_event_name=event)
    path = scan_data.get("transcript_path")
    if not isinstance(path, str):
        raise observer_sources.SourceError("source-path")
    stream = hashlib.sha256(path.encode("utf-8")).hexdigest()
    start = store.cursor(stream)
    if start is None:
        # A transcript seen for the first time is read from its last observer_sources.TAIL_BYTES; the part left out
        # is one source_gap, which status shows.
        start = observer_sources.first_cursor(scan_data, project_root)
        if start and capture:
            store.capture([observer_sources.skipped(scan_data, start)], stream=stream, cursor=start, expected_cursor=None)
    if not capture:
        stream = "controls_" + stream
    controlled = refused = unproven = False
    # The plan and question dialogs the user answered are sources too (observer_sources): a result is matched with its
    # call by call id, and the last calls of a transcript are kept with it. Only the capturing stream reads them.
    calls = observer_sources.load_calls(store, stream) if capture else None
    kept = dict(calls) if calls is not None else None
    stats = {} if capture else None
    # Each scan is bounded; a few in a row catch up with a transcript that grew by a large turn (attachments alone
    # can pass a scan's 256 KiB), so that lag is not reported as a problem. They stop at SCAN_SECONDS too.
    until = time.monotonic() + SCAN_SECONDS
    for _ in range(rounds):
        old = store.cursor(stream)
        try:
            rows, cursor = observer_sources.scan(scan_data, project_root, cursor=old if old is not None else start,
                                                 calls=calls, stats=stats)
        except observer_sources.SourceError as error:
            if rounds != 1 or str(error) != "source-format":
                raise
            # A damaged line (a writer stopped mid-line): capture stays where it is, and the search of the tail below
            # passes over that line, so a hold typed after it still applies, or the command waits (stale).
            cursor = old if old is not None else start
            break
        # Holds are saved first; a crash can repeat an idempotent control, but must
        # never advance a cursor past a hold that has not been compiled.
        found = _controls(store, rows, project_root, repo, config)
        controlled, refused, unproven = controlled or found[0], refused or found[1], unproven or found[2]
        if rows or old is None or cursor != old:  # nothing new since the last hook: no write, no sync to disk
            store.capture(rows if capture else [], stream=stream, cursor=cursor, expected_cursor=old)
        if (cursor == (old if old is not None else start) or os.stat(path, follow_symlinks=False).st_size <= cursor
                or time.monotonic() > until):
            break
    if capture:
        if calls != kept:   # nothing new since the last hook: no write
            observer_sources.save_calls(store, stream, calls)
        observer_sources.add_unread(store, stats.get("dialogs_unread", 0))
    # What is left is a line Claude Code is still writing, or a backlog the next hook goes on with.
    behind, stale = os.stat(path, follow_symlinks=False).st_size > cursor, False
    if behind and rounds == 1:
        tail, complete = observer_sources.control_tail(scan_data, project_root, cursor)
        found = _controls(store, tail, project_root, repo, config)
        controlled, refused, unproven = controlled or found[0], refused or found[1], unproven or found[2]
        stale = not complete
    return controlled, behind, refused, unproven, stale


def _notices(store, notices, data, event):
    """Bounded suppression state holds only hashes and times, never source text."""
    session = data.get("session_id", "")
    if not isinstance(session, str) or not observer_sources.SESSION.fullmatch(session):
        return notices
    key = hashlib.sha256(session.encode()).hexdigest()[:24]
    previous = store.get_control("hook_notices", {})
    if not isinstance(previous, dict):
        previous = {}
    now = time.time()
    old = previous.get(key, {})
    shown = set(old.get("codes", [])) if isinstance(old.get("codes"), list) else set()
    # Each line is shown once per session, whatever event carries it: a turn alternates between sets (a prompt
    # without "pending", a stop with it), so comparing whole sets would show both lines on every turn. A line that
    # answers something the user just did is always shown.
    fresh = [code for code in notices if code not in shown or code in ANSWERS]
    if not fresh:
        return []
    previous[key] = {"codes": sorted(shown | set(notices)), "at": now}
    previous = dict(sorted(previous.items(), key=lambda pair: pair[1].get("at", 0))[-50:])
    store.set_control("hook_notices", previous)
    return fresh


def _switched(store):
    """SWITCHED with its date, once, after a session switched automatic mode on for this folder; else "". The date is
    the approval's own time, checked and formatted here; nothing else of the entry is shown."""
    try:
        entry = observer_runtime.stored_approval()
    except (ValueError, storage.StorageError, OSError):
        return ""
    settings = entry.get("settings") if isinstance(entry, dict) else None
    if not isinstance(settings, dict) or entry.get("given") != "session" or settings.get("mode") != "automatic":
        return ""
    at = entry.get("approved_at")
    if type(at) not in (int, float) or not math.isfinite(at) or not 0 < at < 4102444800:
        return ""
    level = settings.get("level") if settings.get("level") in observer_runtime.LEVELS else ""
    strong = bool(settings.get("strong_model"))
    told = [at, level, strong]   # told again when the level or the stronger model changes, not only on a new approval
    if store.get_control("switch_told") == told:
        return ""
    store.set_control("switch_told", told)
    return SWITCHED % (time.strftime("%Y-%m-%d", time.gmtime(at)),
                       observer_runtime.LEVEL_LABELS.get(level, "not chosen"), "on" if strong else "off")


def _posted_key(data):
    """The eventkey of a Bash call that runs post_verified.py with --eventkey, read as the shell splits it, or None."""
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str) or "post_verified" not in command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not any(os.path.basename(word) == "post_verified.py" for word in words):
        return None
    keys = [words[n + 1] for n, word in enumerate(words[:-1]) if word == "--eventkey"]
    keys += [word.split("=", 1)[1] for word in words if word.startswith("--eventkey=")]
    return keys[0] if len(keys) == 1 and observer_store.EVENT_KEY.fullmatch(keys[0]) else None


def _canonical(item):
    """(the whole question, its options in order) the session asks about `item`, as `observer.py pending --waiting` lists
    it (observer_waiting.entry): a recorded answer counts only to a question that is this one and no other, so that a
    question of the session's own that holds the same line among other words, or offers other options, grants nothing."""
    import observer_waiting
    found = observer_waiting.entry(item)
    return found["question"], found["options"]


def _grant(store, data, project_root):
    """Before post_verified.py --eventkey K: when K waits for the user's answer and the user answered "Post" to the
    session's question naming K's short id and quoting its plain line (observer_sources.answered), a one-time grant for K, which the publisher
    consumes. Nothing else is written; without the answer the post is refused (held_unanswered)."""
    key = _posted_key(data)
    if key is None:
        return
    event = store.event(key)
    if event is None or event["state"] not in ("ready", "deferred"):
        return
    line = observer_store.description(event["candidate"]["kind"], event["body"])
    import observer_waiting
    item = next((item for item in store.waiting() if item["key"] == key and item["kind"] == "held"), None)
    if item is None:
        return   # not a memory that waits for the user: no question about it can be the plugin's own
    text_is, labels_are = _canonical(item)
    answer = observer_sources.answered(data, project_root, key[:8], line, text_is=text_is, labels_are=labels_are)
    if answer:
        store.grant_answer(key, answer)


def _applied_draft(data):
    """The short id of a Bash call that runs `observer.py drafts apply ID`, read as the shell splits it, or None."""
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str) or "drafts" not in command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not any(os.path.basename(word) == "observer.py" for word in words):
        return None
    for n, word in enumerate(words[:-2]):
        if word == "drafts" and words[n + 1] == "apply" and observer_store.DRAFT_ID.fullmatch(words[n + 2]):
            return words[n + 2]
    return None


def _grant_draft(store, data, project_root):
    """Before observer.py drafts apply ID: when the draft waits and the user answered "Approve" to the session's question
    naming its short id and quoting its fixed line (keeper_rules.question_line; observer_sources.answered), a one-time
    grant bound to the hash of the bytes the keeper recorded for it, which drafts apply consumes. Without it the command
    is refused (draft_unanswered)."""
    short = _applied_draft(data)
    if short is None:
        return
    draft = store.draft(short)
    if draft is None or draft["state"] not in ("waiting", "later"):
        return
    content = keeper_rules.recorded(store.get_control(keeper_rules.CONTENTS, []), short)
    if content is None:
        return   # no recorded bytes: nothing the user could approve
    import observer_waiting
    text_is, labels_are = _canonical({"kind": "draft", "key": draft["id"], "short": draft["id"], "draft": draft})
    answer = observer_sources.answered(data, project_root, short,
                                       keeper_rules.question_line(draft["headings"], draft["topic"]), label="Approve",
                                       text_is=text_is, labels_are=labels_are)
    if answer:
        store.grant_answer(keeper_rules.grant_key(short, content), answer)


def _acted(data):
    """The short id of a Bash call that runs `observer.py act SHORT`, read as the shell splits it, or None."""
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str) or "act" not in command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not any(os.path.basename(word) == "observer.py" for word in words):
        return None
    for n, word in enumerate(words[:-1]):
        if word == "act" and observer_store.DRAFT_ID.fullmatch(words[n + 1]):
            return words[n + 1]
    return None


def _grant_act(store, data, project_root):
    """Before observer.py act SHORT: when SHORT is a task the session was to open or close and the user answered the
    label of that item ("Open it" or "Close it") to the session's question naming its short id and quoting its line
    (observer_waiting.line; observer_sources.answered), a one-time grant for the item's key, which `act` consumes
    (the publisher for a task to open, observer_waiting.act for one to close). Without the answer `act` is refused. An
    answer to another item's question, to a question the session worded itself, or "Not now" or "Drop it", grants
    nothing."""
    short = _acted(data)
    if short is None:
        return
    import observer_waiting
    for item in store.waiting():
        if item["short"] == short and item["kind"] in ("task_open", "task_close", "map"):
            text_is, labels_are = _canonical(item)
            answer = observer_sources.answered(data, project_root, short, observer_waiting.line(item),
                                               label=observer_waiting.LABELS[item["kind"]], text_is=text_is,
                                               labels_are=labels_are)
            if answer:
                store.grant_answer(item["key"], answer)
            return


def _verb(data):
    """(subcommand, its first two words after it) of a Bash call to observer.py, read as the shell splits it, or None."""
    command = (data.get("tool_input") or {}).get("command") if isinstance(data.get("tool_input"), dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str) or "observer" not in command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for n, word in enumerate(words):
        if os.path.basename(word) == "observer.py":
            rest, skip = [], False
            for w in words[n + 1:]:
                if skip:
                    skip = False
                elif w == "--repo":   # the only option before the subcommand: it takes a value
                    skip = True
                elif not w.startswith("--"):
                    rest.append(w)
            return (rest[0], rest[1:3]) if rest else None
    return None


def _declined(data):
    """(answer, item id) of a Bash call that declines something that waits, or None: `observer.py later SHORT` ("later"),
    `observer.py discard KEY` ("no") and `observer.py drafts later|drop ID` ("later", "no")."""
    found = _verb(data)
    if found is None:
        return None
    verb, rest = found
    if verb == "later" and rest and observer_store.DRAFT_ID.fullmatch(rest[0]):
        return "later", rest[0]
    if verb == "discard" and rest and observer_store.EVENT_KEY.fullmatch(rest[0]):
        return "no", rest[0]
    if verb == "drafts" and len(rest) == 2 and rest[0] in ("later", "drop") and observer_store.DRAFT_ID.fullmatch(rest[1]):
        return ("later" if rest[0] == "later" else "no"), rest[1]
    return None


def _grant_decline(store, data, project_root):
    """Before observer.py later SHORT, discard KEY, or drafts later|drop ID: when that item waits and the user answered the
    label of that answer ("Not now", "Drop" or "Drop it") to the session's question naming its short id and quoting its
    line (observer_waiting.line; observer_sources.answered), a one-time grant under the answer's own key
    (observer_waiting.answer_key), which the command consumes. Without it the command is refused: a session cannot
    postpone or drop what waits for the user on its own."""
    found = _declined(data)
    if found is None:
        return
    import observer_waiting
    answer, ident = found
    item = observer_waiting.find_any(store, ident)
    label = observer_waiting.ANSWERS.get(item["kind"], {}).get(answer) if item is not None else None
    if label is None:
        return
    text_is, labels_are = _canonical(item)
    given = observer_sources.answered(data, project_root, item["short"], observer_waiting.line(item), label=label,
                                      text_is=text_is, labels_are=labels_are)
    if given:
        store.grant_answer(observer_waiting.answer_key(item, answer), given)


def _entry_repo():
    """The repository whose holds apply here: the one this folder's private entry names, never the project file's,
    which a single edit could change. None without an entry; False when the approvals cannot be read or the entry
    names no valid repository, so that the check fails closed."""
    try:
        entry = observer_runtime.stored_approval()
    except (ValueError, storage.StorageError, OSError):
        return False
    if entry is None:
        return None
    repo = entry.get("repo") if isinstance(entry, dict) else None
    return repo if recortex.valid_repo(repo) else False


BEHIND = {"reason": "recortex observer: the transcript is too far ahead of the check for holds; it catches up at the end "
                    "of each turn. Every command but pwd, ls and cat waits until then."}


def diagnostic(data):
    """True for a Bash command that is exactly `python3 <this scripts folder>/observer.py status` (also with --source-details)
    or `python3 <this scripts folder>/hooks.py --doctor`, by the folder's own absolute path, with no operator and nothing else
    (the form pretool_guard.allowed_command checks for the ask agent): two commands that only read, which the session runs to
    tell the user why the holds cannot be checked."""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if data.get("tool_name") not in ("Bash", "bash") or not isinstance(command, str) or not command.strip() \
            or len(command) > 4000 or pretool_guard.OPERATORS.search(command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    if len(words) < 3 or words[0] != "python3" or not os.path.isabs(words[1]):
        return False
    for script, rest in (("observer.py", (["status"], ["status", "--source-details"])), ("hooks.py", (["--doctor"],))):
        if words[1] == os.path.join(here, script) and words[2:] in rest:
            try:
                return os.path.realpath(words[1]) == os.path.realpath(os.path.join(here, script))
            except (OSError, ValueError):
                return False
    return False


def _unchecked(data, denial=UNCHECKED):
    """The decision when the holds cannot be known: every Bash command but the plainest reads, and the two diagnostics
    (diagnostic), is denied."""
    tool_name, tool_input = data.get("tool_name"), data.get("tool_input")
    if observer_holds.classify_action(tool_name, tool_input)["command"] is None:
        return None
    return None if observer_holds.read_only(tool_name, tool_input) or diagnostic(data) else denial


# One row for each subcommand of observer.py: how a session's own call to it is kept in check. "safe": it only reads, or
# only does what the settings and the caps already allow, or only does less. "grant": it changes what waits for the user
# or GitHub, or the user's own settings (turns the observer on, raises its level, adds the stronger model, lifts a hold or a
# pause, switches it off, undoes an act, trusts folders): and the command refuses without the one-time grant the hook
# records from the user's own recorded answer to that exact question (an item that waits, a setup question, or an act's
# question from acts.py), or without the user's own terminal. A test holds this table, the parser of observer.py and the
# pattern together; observer.py states each row's reason at its definition.
COMMANDS = {"status": "safe", "holds": "safe", "pending": "safe", "log": "safe", "context": "safe", "hook": "safe",
            "advisory": "grant", "map-mode": "grant", "map-proposals": "safe",
            "runtime-check": "safe", "run-worker": "safe", "record": "safe", "reconcile": "safe", "limits": "safe",
            "pause": "safe",
            "enable": "grant", "act": "grant", "later": "grant", "discard": "grant", "drafts": "grant",
            "release": "grant", "disable": "grant", "resume": "grant", "undo": "grant",
            "trust-ancestor": "grant"}
# The subcommands that perform an act with a question of its own (acts.py): pretool_guard.ASKED, written out there too, and a
# test holds the two and the acts together.
ASKED = ("advisory", "disable", "release", "resume", "trust-ancestor", "undo")


def _held(repo, data):
    """The decision before a tool call, from holds read without any lock (observer_store.read_holds). When they cannot
    be read, every Bash command is denied with one fixed reason, except pwd, ls and cat with plain path arguments
    (observer_holds.read_only), and those too when the held exact commands, still known beside the queue, name them.
    No hold applies to another tool."""
    tool_name, tool_input = data.get("tool_name"), data.get("tool_input")
    try:
        holds = observer_store.read_holds(repo)
    except (observer_store.StoreError, storage.StorageError, OSError, ValueError, KeyError, TypeError):
        command = observer_holds.classify_action(tool_name, tool_input)["command"]
        if command is None:
            return None  # not a shell command: no hold applies to it
        if not observer_holds.read_only(tool_name, tool_input):
            return UNCHECKED
        try:
            return UNCHECKED if observer_store._command_digest(command) in observer_store.held_commands(repo) else None
        except observer_store.StoreError:
            return None  # the plainest reads go on even when the held commands are unknown
    result = observer_holds.check_action(holds, repo, tool_name, tool_input, session_id=data.get("session_id"))
    return result if result["decision"] == "deny" else None


def _degraded(store, reason):
    try:
        if store.get_control("hook_degraded") != reason:  # a write syncs to disk: only when it changes
            store.set_control("hook_degraded", reason)
    except observer_store.StoreError:
        pass  # a full queue must not prevent checking an already stored hold


POINTER_SECONDS = 1.0   # what the decision reminder may take of the hook's 3.5 s; slower, and the hook goes on without it


def _context(store, event, data, repo, allowed, root=None, notices=None):
    """The task context for a prompt or a subagent, or "". `notices`, when given, is a list that this adds a code to
    when part of the context was left out for lack of time."""
    if event not in ("UserPromptSubmit", "SubagentStart", "PreToolUse"):
        return ""
    query = data.get("prompt", "")
    if event == "PreToolUse":
        # Only before a supported git or gh action, where a hold can apply; other calls would add up to 4,000
        # characters whenever their words match another group.
        if not observer_holds.classify_action(data.get("tool_name"), data.get("tool_input"))["actions"]:
            return ""
        query = json.dumps(data.get("tool_input", {}), sort_keys=True)
    if not isinstance(query, str):
        query = ""
    import index  # here, not at the top: most hook calls never build a context packet
    import observer_context
    db = index.connect(repo, create=False, tighten=False)
    if db is None:
        raise ValueError("context-unavailable")
    main_prompt = event == "UserPromptSubmit" and not _from_subagent(data)
    mode = observer_runtime.settings(store.config)["advisory_mode"] if main_prompt else "off"
    try:
        active = [row[0] for row in db.execute("SELECT issue FROM labels WHERE label='active' ORDER BY issue")]
        packet = observer_context.build_packet(db, repo, allowed, query=query[:48000],
                                               issue=active[0] if len(active) == 1 else None, max_chars=4000)
        if mode in ("shadow", "deliver") and query.strip():
            import decision_impact
            import guidance
            import observer_advisory
            try:
                with decision_impact.time_limit(POINTER_SECONDS, db):
                    pointer = observer_advisory.change_pointer(db, repo, allowed, packet, query[:48000])
            except decision_impact.TooSlow:
                # The packet is kept without the pointer; the user is told once a session, and --doctor says when.
                pointer = None
                guidance.note_skip(repo, "observer", POINTER_SECONDS)
                if notices is not None:
                    notices.append("pointer")
            except (ValueError, OSError, sqlite3.Error):
                pointer = None
            if pointer:
                packet["advisories"] = [pointer]
    finally:
        db.close()
    session = data.get("session_id", "")
    if not isinstance(session, str) or not observer_sources.SESSION.fullmatch(session):
        return ""
    advice = ""
    # Advice is a separate opt-in at the start of a main-session task, never a tool denial or an approval request.
    if main_prompt:
        import observer_advisory
        try:
            advice = observer_advisory.deliver(store, packet, query[:48000], session, mode=mode, root=root)
        except (observer_store.StoreError, ValueError):
            # An unavailable local preference cannot issue a reminder. Keep the ordinary trusted context usable.
            advice = ""
    # The selection, not the text: the text carries the index's sync time, so every sync would make it new.
    digest = hashlib.sha256(json.dumps([packet.get("selected"), packet.get("resume_id")], sort_keys=True,
                                       default=str).encode("utf-8")).hexdigest()
    key = hashlib.sha256((session + ":" + str(data.get("agent_id", ""))).encode()).hexdigest()[:24]
    seen = store.get_control("hook_context", {})
    if not isinstance(seen, dict):
        seen = {}
    if seen.get(key, {}).get("digest") == digest:
        # Same ids can contain newly approved edits. An issued pointer brings its current evidence, not old context.
        return packet["text"] + "\n" + advice if advice else ""
    seen[key] = {"digest": digest, "at": time.time()}
    store.set_control("hook_context", dict(sorted(seen.items(), key=lambda pair: pair[1].get("at", 0))[-50:]))
    return packet["text"] + ("\n" + advice if advice else "")


def _budget_reached(settings, used, tiers):
    """True when a tier that runs has used its calls or its tokens of the day (the caps of observer_runtime.CALL_CAPS and
    TOKEN_BUDGETS; a tier that is not configured does not count). `used` is Store.status()["tiers_24h"]."""
    if not isinstance(used, dict):
        return False
    for tier, row in used.items():
        if tier not in observer_runtime.TIERS or not isinstance(row, dict) or not tiers.get(tier, {}).get("configured"):
            continue
        calls, tokens = row.get("calls", 0), row.get("input_tokens", 0) + row.get("output_tokens", 0)
        if calls >= settings[observer_runtime.CALL_CAPS[tier]] or tokens >= settings[observer_runtime.TOKEN_BUDGETS[tier]]:
            return True
    return False


def handle(event, data, config, repo):
    """Return hook JSON. Empty means no observer decision, never tool approval.

The CLI must call this before model/worker dispatch. Inputs are native Claude
hook JSON; they do not confer authority to stored quotes or arbitrary files.
Before a Bash call the hold decision comes first, from holds read without a
lock; capture and context follow. When they fail or run out of time, the
decision made is still returned, and a call whose holds were never decided is
stopped as when they cannot be read (fail_closed).
"""
    if names.env(observer_runtime.GUARD, ""):
        return {}
    event = EVENTS.get(event, event)
    if event not in EVENTS.values() or not isinstance(data, dict) or not isinstance(config, dict):
        return {}
    decision = {}
    try:
        return _counted(event, repo, _handle(event, data, config, repo, decision))
    except TimeoutError:
        if event != "PreToolUse":
            return _output(event, notices=[] if event in TOOL_EVENTS else ["capture"])  # out of time: say so
        # The final decision when it was made; else a denial the stored holds already gave; else, since holds may have
        # been typed that were not read, every command a hold could apply to is stopped.
        if "held" in decision:
            return _output(event, denial=decision["held"])
        return _output(event, denial=decision.get("early") or _unchecked(data))


# What the usage view calls the context each observer event can add (usage.INJECTED).
COUNTED = {"SessionStart": "observer-session-start", "SubagentStart": "observer-subagent-start",
           "UserPromptSubmit": "observer-prompt-submit", "PreToolUse": "observer-pre-tool"}


def _counted(event, repo, result):
    """`result` as it is. The characters of its context go to the usage view, an estimate that never raises and waits at most
    50 ms (usage.py)."""
    try:
        context = result.get("hookSpecificOutput", {}).get("additionalContext") if isinstance(result, dict) else None
        if isinstance(context, str) and context and event in COUNTED and recortex.valid_repo(repo):
            import usage
            usage.note_injected(repo, COUNTED[event], len(context))
    except Exception:  # noqa: BLE001
        pass
    return result


def fail_closed(data):
    """The output before a Bash call whose holds could not be decided in time: as for unreadable holds, every command
    but the plainest reads is stopped."""
    return _output("PreToolUse", denial=_unchecked(data) if isinstance(data, dict) else UNCHECKED)


def _from_subagent(data):
    """True for a tool call made by a subagent: Claude Code puts agent_id (and agent_type) in its input and in no call of the
    main session. No subagent mints a grant, whatever the transcript holds."""
    return bool(data.get("agent_id") or data.get("agent_type"))


def _handle(event, data, config, repo, decision):
    # A hold stays in force while this folder has an entry on this machine, whatever the project file says (its
    # repository, its trust, or "observer": false): one edit of a shared file must not lift what the user held. The
    # holds of the repository the project file names apply too, whether or not this folder's approval holds.
    held, checked = None, False
    if event == "PreToolUse":
        held_repo = _entry_repo()
        if held_repo is not None:
            held, checked = (_held(held_repo, data) if held_repo else _unchecked(data)), True
            if not held and recortex.valid_repo(repo) and repo.lower() != held_repo:
                held = _held(repo.lower(), data)
            decision["early"] = held
    if not recortex.valid_repo(repo) or repo.lower() not in recortex.trusted_repos():
        return _output(event, denial=held) if held else {}
    raw = config.get("observer", {})
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return _output(event, denial=held) if held else {}
    notices, context, denial, lines = [], "", held, []
    try:
        settings = observer_runtime.settings(config)
        project_root = recortex.project_root()
        if not observer_runtime.approval_valid(repo, config):
            reason = observer_runtime.approval_reason(repo, config)
            # This machine's entry for the folder does not hold (another repository or machine key, settings that no
            # longer validate, or an approval that does not follow this host). Nothing is created, and the line shows at
            # session start only.
            if observer_runtime.paused(repo, config):
                # A 1.4.0 approval holds no level: the observer waits for the user's answer to the level question.
                return _output(event, lines=[observer_runtime.PAUSED] if event == "SessionStart" else [], denial=held)
            notice = "approval-host" if reason == "host" else "approval"
            return _output(event, notices=[notice] if event == "SessionStart" else [], denial=held, repo=repo)
        if event == "PreToolUse" and not checked:
            denial = decision["early"] = _held(repo, data)
        with observer_store.Store(repo, max_bytes=settings["max_pending_bytes"], wait=0.1,
                                  max_pending=settings["max_pending_observations"], config=config) as store:
            paused = store.get_control("capture_paused", False)
            if paused:
                notices.append("paused")
            stale = False
            transcript = data.get("transcript_path")
            if event == "UserPromptSubmit" and isinstance(transcript, str) and not os.path.lexists(transcript):
                pass  # a session's first prompt: Claude Code writes the transcript after this hook. Nothing yet.
            elif event in observer_sources.EVENTS:
                try:
                    controlled, incomplete, refused, unproven, stale = _capture(
                        store, event, data, project_root, repo, config, capture=not paused,
                        rounds=1 if event == "PreToolUse" else SCAN_ROUNDS)
                    if controlled:
                        notices.append("control")
                    # A transcript still being written, or a backlog the next hook goes on with, is not worth a line;
                    # a hold typed now that the transcript does not show yet is: it may not be active.
                    waiting = False
                    if event == "UserPromptSubmit" and not controlled and isinstance(data.get("prompt"), str):
                        typed = observer_holds._control(data["prompt"].strip())
                        waiting = bool(typed and typed["operation"] == "hold")
                    if unproven:
                        notices.append("release-refused")
                    if waiting:
                        notices.append("source")
                        _degraded(store, "source-incomplete")
                    elif refused:
                        notices.append("control-refused")
                        _degraded(store, "control-unavailable")
                    else:
                        _degraded(store, "")
                except TimeoutError:
                    raise  # The one-shot hook deadline must end work, not start more I/O.
                except (observer_sources.SourceError, OSError):
                    notices.append("source")
                    _degraded(store, "source-unavailable")
                except ValueError:
                    notices.append("control-refused")
                    _degraded(store, "control-unavailable")
                except observer_store.StoreError:
                    notices.append("capture")
                    _degraded(store, "capture-unavailable")
            if event == "PreToolUse":
                result = observer_holds.check_action(store.holds(), repo, data.get("tool_name"),
                                                     data.get("tool_input"), session_id=data.get("session_id"))
                denial = result if result["decision"] == "deny" else denial  # nothing in a session releases a hold
                if not denial and stale:
                    denial = _unchecked(data, BEHIND)  # holds typed in the part not read may apply
                decision["held"] = denial
                if not denial and not _from_subagent(data):   # a grant is made by the main session's own call only
                    try:
                        _grant(store, data, project_root)
                        _grant_draft(store, data, project_root)
                        _grant_act(store, data, project_root)
                        _grant_decline(store, data, project_root)
                        if _ran_waiting(data):
                            _note_ran(store, data)
                    except TimeoutError:
                        raise
                    except (observer_sources.SourceError, observer_store.StoreError, storage.StorageError, OSError,
                            ValueError):
                        pass  # no grant: the publisher refuses the held event (held_unanswered)
            try:
                context = _context(store, event, data, repo, recortex.allowed_users(repo), root=project_root,
                                   notices=notices)
            except TimeoutError:
                raise
            except Exception:
                notices.append("context")
            if event == "SessionStart":
                items = store.waiting()
                if items:
                    notices.append("held")
                    # Every session start, compaction included: the context is new. The count is every item that waits
                    # (the drafts too); the draft line beside it names the topics, and is the keeper's own pointer to the
                    # skill's "Drafts that wait" section.
                    context = waiting_line(len(items))
                    drafts = [item["draft"]["topic"] for item in items if item["kind"] == "draft"]
                    if drafts:
                        context += "\n" + keeper_rules.draft_line(len(drafts), drafts)
                figures = _figures_line(store)
                if figures:
                    context = (context + "\n" if context else "") + figures   # a line only: nothing is drafted for it
                switched = _switched(store)
                if switched:
                    lines.append(switched)
                acts = _acts_line(store, data, config)
                if acts:
                    lines.append(acts)
                lost = _acts_failed_line(store)
                if lost:
                    lines.append(lost)
            if event in ("SessionStart", "UserPromptSubmit"):
                cap = _tasks_cap_line(store)
                if cap:
                    lines.append(cap)
            if event == "UserPromptSubmit":
                reminded = _reminder(store, data)
                if reminded:
                    context = (context + "\n" if context else "") + reminded
            tiers = observer_runtime.tier_state(settings, store)
            # A pass needs the sort tier and the middle tier; a strong tier whose proof is not valid stops the keeper only.
            runtime_ready = tiers["sort"]["gate"] and tiers["middle"]["gate"]
            if not runtime_ready:
                notices.append("runtime")
            status = store.status()
            retention_last = store.get_control("retention_last", 0)
            maintenance_due = (type(retention_last) not in (int, float) or
                               time.time() - retention_last >= 86400)
            has_handled = any(status["events"].get(state, 0) for state in observer_store.TERMINAL)
            # Local retention remains available while models are unavailable or
            # capture is paused. The worker checks its model/network gates itself.
            # Tool events fire on every call; the worker starts from the edges of a turn only.
            # Not at every prompt: the worker waits for a stretch of the session anyway (observer_worker.ENOUGH).
            if event in ("SessionStart", "Stop", "PreCompact") and (
                    runtime_ready or (maintenance_due and has_handled)):
                try:
                    launch_worker(project_root)
                except OSError:
                    notices.append("worker")
            if store.get_control("hook_degraded") and not any(code in notices for code in ("source", "capture")):
                notices.append("capture")
            if _budget_reached(settings, status.get("tiers_24h"), tiers):
                notices.append("budget")
            # Pending work is worth a line only when the worker cannot take it up (a degraded worker), or when new
            # captured work has waited longer than STALE; ordinary backlog at a stop is not. Once per session.
            degraded = store.get_control("worker_degraded")
            if degraded:
                notices.append("cap" if degraded == "publish_daily_cap" else "pending")
            elif event in ("Stop", "PreCompact"):
                oldest = store.db.execute("SELECT MIN(created) FROM observations WHERE state='observed'").fetchone()[0]
                if oldest is not None and time.time() - oldest > STALE:
                    notices.append("pending")
            notices = _notices(store, notices, data, event)
    except TimeoutError:
        raise  # handle() gives the decision made, or stops what a hold could apply to
    except Exception:
        # Fixed text only: exception strings can contain source text or paths. Tool events fire on every call and
        # cannot record a suppression when the queue is what failed, so the notice waits for the prompt or stop.
        if event not in TOOL_EVENTS:
            notices.append("capture")
    return _output(event, context=context, notices=notices, denial=denial, lines=lines, repo=repo)

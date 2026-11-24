"""The setup questions an update adds to a project that was set up before, and the answers of this machine to them.

Python 3 standard library only. A question is a row of QUESTIONS. Whether it is still open is read from the current
state (the project file, the user's Claude Code settings, the private files of this machine, the record's index),
never from a stored setup version, so a project set up with any earlier version gets the right list with no
migration. `since` says which version added the question; it decides nothing.

Three answers are remembered per machine, in the private folder of the configuration top (storage.config_top), in
setup-answers.json, written through storage.py under a lock exactly as writing.py writes its file:

  yes, no   the question is closed on this machine for good (`reset` removes the entry)
  later     the question is asked again at the first session start seven or more days later

A project-wide "yes" settles the question for everyone through the project file or GitHub. A project-wide "no" is
remembered on this machine only, so each collaborator is asked it once; no key is added to the project file. Nothing
the project file holds is read as an answer.

The hook (hooks.py, session start only) counts the open questions from local files alone and prints one fixed line
once per session; it makes no GitHub call and prints nothing it read. `setup.py questions` is what the session then
runs: it reads GitHub once at most (the labels), prints each open question with the exact commands of its options,
and writes nothing but the note that no label is missing and the time it ran. `setup.py answer` records one answer. The hooks
only read the file. A file that cannot be read (a symlink, loose permissions, not JSON, not of the form, too large) is never
replaced and is reported in one fixed line that points to hooks.py --doctor.

A session that does not ask before its first reply ends is reminded up to three times: the prompt-submit hook (hooks.py) adds one
fixed line with the count (reminder below) to a later prompt, while questions are open and `setup.py questions` has not run since
the session started. The time of the session's start is in the prompt state, and the time `setup.py questions` last ran is a small
file beside it (storage.py, names.SETUP_RUN); both are numbers, and nothing read from them is printed.

Switched off with RECORTEX_SETUP_QUESTIONS set to 0, off, no or false, or "setup_questions": false in the user's
settings.json of the plugin (recortex.settings_file). The switch stops the hook's line and its reminder; `setup.py questions`
still answers when it is asked.
"""

import datetime
import json
import os
import re
import shlex
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import field_sheet  # noqa: E402
import names  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402
import storage  # noqa: E402
import writing  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FORM = 1
ANSWERS = ("yes", "no", "later")
FILE_CAP = 20000
LOCK = "setup-answers.lock"
LATER_DAYS = 7
SETTINGS_CAP = 1024 * 1024
ID = re.compile(r"[a-z][a-z-]{0,39}")
EVIDENCE = re.compile(r"[A-Za-z0-9 ./*_-]{1,40}")
OPEN, SETTLED, NOT_ASKED = "open", "settled", "not-asked"
NOT_READABLE = {"storage": "the folder or the file is not safe to read", "form": "the file is not in the form the plugin "
                "writes", "large": "the file is larger than the plugin writes"}
# Why a question is not asked, in the plugin's fixed words (for --doctor and `setup.py questions`).
REASONS = {
    "no-index": "the record has not been read yet",
    "no-plan": "no usable plan issue",
    "writing-unreadable": "the writing setting of this machine cannot be read",
    "project-file": "the project file does not fit the form the plugin writes",
    "no-manuscript": "the project has no manuscript profile",
    "no-paper-files": "no paper or manuscript files",
    "settings-unusable": "the Claude Code settings file is a symlink, too large or not a JSON object",
    "observer-off": "the project file switches the observer off",
    "writing-not-on": "the writing mode is not review or autonomous",
    "no-level": "the observer has no level of Assistant or Everything for this folder",
    "approvals-unreadable": "the approvals of the observer cannot be read",
    "check-failed": "the check could not be made",
    "reminder-unreadable": "the task reminder setting of this machine cannot be read",
    "no-map-files": "no project document with a name the map looks for",
    "no-other-status-line": "the settings have no status line of another tool",
    "capture-not-ready": "notes cannot be taken on this machine yet: a folder above Claude Code's transcripts belongs to "
                         "another account or can be written by a shared group",
}


# ---------------------------------------------------------------- the switch

def off():
    """True when the user switched the questions off: RECORTEX_SETUP_QUESTIONS set to 0, off, no or false, or
    "setup_questions": false in the plugin's settings.json (modeled on hooks.discovery_off)."""
    if names.env("SETUP_QUESTIONS", "").strip().lower() in ("0", "off", "no", "false"):
        return True
    try:
        with open(recortex.settings_file(), encoding="utf-8") as handle:
            return json.load(handle).get("setup_questions") is False
    except (OSError, ValueError, AttributeError, RecursionError):
        return False


# ---------------------------------------------------------------- the state of this machine

def path():
    return os.path.join(storage.config_top(), storage.PRIVATE, names.SETUP_ANSWERS)


def empty():
    return {"form": FORM, "machine": {}, "repos": {}}


def entries_fit(entries):
    """{id: {"answer": yes|no|later, "day": a real day}}. An id the plugin does not know is kept, so that an older
    version reads a newer file, when it has the form of an id."""
    if not isinstance(entries, dict):
        return False
    for key, entry in entries.items():
        if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(entry, dict) \
                or set(entry) != {"answer", "day"} or entry["answer"] not in ANSWERS or not writing.real_day(entry["day"]):
            return False
    return True


def load(create=False):
    """The validated file: {"form": 1, "machine": {id: entry}, "repos": {"owner/name": {id: entry}}}. Raises ValueError
    with a key of NOT_READABLE for a file that does not fit, and storage.StorageError for refused storage. With
    create=False nothing is made or tightened (the hooks)."""
    private = storage.folder(storage.config_top(), create=create, tighten=create)
    if private is None:
        return empty()
    target = os.path.join(private, names.SETUP_ANSWERS)
    info = storage.check_path(target, "file")
    if not info:
        return empty()
    if info.st_size > FILE_CAP:
        raise ValueError("large")
    try:
        data = json.loads(storage.read_file(target) or "")
    except (ValueError, RecursionError):
        raise ValueError("form")
    if not isinstance(data, dict) or set(data) != {"form", "machine", "repos"} or type(data["form"]) is not int \
            or data["form"] != FORM or not entries_fit(data["machine"]) or not isinstance(data["repos"], dict):
        raise ValueError("form")
    for repo, entries in data["repos"].items():
        if not recortex.valid_repo(repo) or repo != repo.lower() or not entries_fit(entries):
            raise ValueError("form")
    return data


def read():
    """{"state": "ok" | "unreadable", "data": the file (empty when unreadable), "why": a key of NOT_READABLE or ""}. Read
    only."""
    try:
        return {"state": "ok", "data": load(), "why": ""}
    except storage.StorageError:
        return {"state": "unreadable", "data": empty(), "why": "storage"}
    except ValueError as error:
        return {"state": "unreadable", "data": empty(), "why": str(error) if str(error) in NOT_READABLE else "form"}


def entry_of(data, question, repo):
    """The stored answer of `question` for `repo` on this machine, or None. An answer kept under a retired id counts for
    the question that replaced it only when it is "no" or "later": a 1.4.0 "yes" to the old observer question said yes to
    1.4.0's observer, and the new one asks the user again."""
    where = data["machine"] if question.scope == "machine" else data["repos"].get(repo.lower(), {})
    if question.id in where:
        return where[question.id]
    for old, new in RETIRED.items():
        if new == question.id and old in where and where[old]["answer"] in ("no", "later"):
            return where[old]
    return None


def record(repo, question_id, answer, day=None):
    """Record one answer of the user on this machine: yes, no or later with a day (default today), or reset, which
    removes the entry. Entries of other questions, other repositories and ids this version does not know stay. A file
    that cannot be read stops it and is never replaced. Returns the day."""
    question = find(question_id)
    if question is None or answer not in ANSWERS + ("reset",):
        raise ValueError("id")
    day = day or datetime.date.today().isoformat()
    with field_sheet.approvals_lock(name=LOCK):
        data = load(create=True)
        where = data["machine"] if question.scope == "machine" else data["repos"].setdefault(repo.lower(), {})
        if answer == "reset":
            kept = [key for key in (question.id,) + tuple(old for old, new in RETIRED.items() if new == question.id)
                    if key in where]
            if not kept:
                return day   # nothing was kept: the file is left as it is
            for key in kept:   # an answer kept under a retired id goes with it
                del where[key]
        else:
            where[question.id] = {"answer": answer, "day": day}
        data["repos"] = {key: value for key, value in data["repos"].items() if value}
        text = json.dumps(data, indent=1, sort_keys=True) + "\n"
        if len(text.encode("utf-8")) > FILE_CAP:
            raise ValueError("large")
        private = storage.folder(storage.config_top())
        storage.write_file(os.path.join(private, names.SETUP_ANSWERS), text)
    return day


def deferred(entry, today):
    """True while a `later` is in force: until today is its day plus LATER_DAYS or later, in local dates. A day in the
    future counts as deferred."""
    then = datetime.datetime.strptime(entry["day"], "%Y-%m-%d").date()
    return today < then + datetime.timedelta(days=LATER_DAYS)


# ---------------------------------------------------------------- the checks

class Context:
    """What the checks look at, read once: the repository, the project file the caller parsed, the project folder and
    the record (a brief.Record, or None when there is no index)."""

    def __init__(self, repo, config, root, record=None):
        self.repo, self.config, self.root, self.record = repo, config, root, record
        self.cache = {}

    def once(self, key, make):
        if key not in self.cache:
            self.cache[key] = make()
        return self.cache[key]

    def plan_gate(self):
        """"" when the plan issue is usable (the gate Record.writing_lines uses), else the key of a reason."""
        if self.record is None:
            return "no-index"
        counts, bad = self.record.plan_counts() if self.record.plan() else (None, False)
        return "" if counts and counts["issue"] and not bad else "no-plan"


def check_writing_mode(ctx):
    why = ctx.plan_gate()
    if why:
        return NOT_ASKED, why, None
    found = writing.read(ctx.repo)
    if found["state"] == "unreadable":
        return NOT_ASKED, "writing-unreadable", None
    return (SETTLED if found["state"] == "chosen" else OPEN), "", None


def check_own_paper(ctx):
    why = ctx.plan_gate()
    if why:
        return NOT_ASKED, why, None
    if writing.read(ctx.repo)["state"] == "unreadable":
        return NOT_ASKED, "writing-unreadable", None
    return (OPEN if recortex.own_manuscript(ctx.config)["state"] == "none" else SETTLED), "", None


def project_file_fits(ctx):
    import setup
    try:
        setup.check_existing(ctx.config)
    except setup.AnswerError:
        return False
    return True


def check_manuscript_profile(ctx):
    import setup
    if not project_file_fits(ctx):
        return NOT_ASKED, "project-file", None
    if "manuscript" in setup.covered_profiles(ctx.config):
        return SETTLED, "", None
    found = ctx.once("signals", lambda: setup.uncovered_signals(ctx.root, ctx.config))
    for item in found:
        if item["kind"] == "paper":
            return OPEN, "", [e for e in item["evidence"] if EVIDENCE.fullmatch(e)][:6]
    return NOT_ASKED, "no-paper-files", None


def check_labels(ctx):
    """Open for a project with the manuscript profile; whether a label is missing is for `setup.py questions` to read
    from GitHub (the hook makes no call, so it counts this question as one to check)."""
    import setup
    if "manuscript" not in setup.covered_profiles(ctx.config):
        return NOT_ASKED, "no-manuscript", None
    if not project_file_fits(ctx):
        return NOT_ASKED, "project-file", None
    return OPEN, "", None


def claude_settings(ctx):
    """("missing", None), ("unusable", None) or ("ok", the settings as a dict): the user's Claude Code settings.json, read once
    for the questions about the status line (a symlink, a file over SETTINGS_CAP, a file that does not parse and one that is not
    an object are "unusable")."""
    import setup

    def read_it():
        target = setup.claude_settings_file()
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "unusable", None
        if not stat.S_ISREG(info.st_mode) or info.st_size > SETTINGS_CAP:
            return "unusable", None
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                data = json.loads(handle.read(SETTINGS_CAP + 1))
        except (OSError, ValueError, RecursionError):
            return "unusable", None
        return ("ok", data) if isinstance(data, dict) else ("unusable", None)
    return ctx.once("claude-settings", read_it)


def check_status_line(ctx):
    state, data = claude_settings(ctx)
    if state == "missing":
        return OPEN, "", None
    if state == "unusable":
        return NOT_ASKED, "settings-unusable", None
    return (SETTLED if "statusLine" in data else OPEN), "", None


def check_status_line_combine(ctx):
    """Asked when the user's settings already hold a status line that is not recortex's: today that case counts as settled for
    `status-line` and the user never learns of the snippet that combines the two. Settled when the status line is an object
    whose command holds both `recortex` and `statusline.py`."""
    state, data = claude_settings(ctx)
    if state == "unusable":
        return NOT_ASKED, "settings-unusable", None
    if state == "missing" or "statusLine" not in data:
        return NOT_ASKED, "no-other-status-line", None
    line = data["statusLine"]
    command = line.get("command") if isinstance(line, dict) else None
    if isinstance(command, str) and "recortex" in command and "statusline.py" in command:
        return SETTLED, "", None
    return OPEN, "", None


def observer_entry(ctx):
    """(state, reason, settings): the observer's stored settings for this folder and repository, completed with the
    defaults of the keys 1.5.0 added (a 1.4.0 approval has no level). `settings` is {} when there is no approval for this
    repository here; state is NOT_ASKED with a reason when the project file switches the observer off or the approvals
    cannot be read, else OPEN, which only says that the settings were read."""
    import observer_runtime
    if observer_runtime.project_off(ctx.config):
        return NOT_ASKED, "observer-off", {}
    try:
        approval = observer_runtime.stored_approval(ctx.root)
    except (storage.StorageError, ValueError, OSError):
        return NOT_ASKED, "approvals-unreadable", {}
    if not isinstance(approval, dict) or approval.get("repo") != ctx.repo.lower() \
            or not isinstance(approval.get("settings"), dict):
        return OPEN, "", {}
    return OPEN, "", observer_runtime.complete(approval["settings"])


def capture_gate(ctx):
    """"" when notes can be taken here or no session has run in this folder yet (the transcript folder is missing), else
    "capture-not-ready": a folder above Claude Code's transcripts belongs to another account or is written by a shared group
    (observer_sources.preflight). The questions that turn the observer on are not asked then, because the user's answer could
    not be read and nothing could be noted; `setup.py questions` says so once, with the command that mends it."""
    import observer_sources
    result = ctx.once("capture", lambda: observer_sources.preflight(ctx.root))
    return "" if result.get("ready") or result.get("code") == "source-missing" else "capture-not-ready"


def check_observer_level(ctx):
    """Settled when this folder's approval holds a chosen level. A folder enabled before 1.5.0 has an approval with none,
    so the question is open for it, and the observer stays paused until the user answers (observer_runtime.paused). Not
    asked where capture is not ready (capture_gate)."""
    import observer_runtime
    state, reason, found = observer_entry(ctx)
    if state == NOT_ASKED:
        return state, reason, None
    if found.get("level") in observer_runtime.LEVELS:
        return SETTLED, "", None
    gate = capture_gate(ctx)
    return (NOT_ASKED, gate, None) if gate else (OPEN, "", None)


def check_paper_proposals(ctx):
    """Asked only when the plan issue is usable, the writing mode is review or autonomous, the observer's level is
    Assistant or Everything and no strong model is set; settled once one is."""
    import observer_runtime
    why = ctx.plan_gate()
    if why:
        return NOT_ASKED, why, None
    found = writing.read(ctx.repo)
    if found["state"] == "unreadable":
        return NOT_ASKED, "writing-unreadable", None
    if found["state"] != "chosen" or found["mode"] not in ("review", "autonomous"):
        return NOT_ASKED, "writing-not-on", None
    state, reason, stored = observer_entry(ctx)
    if state == NOT_ASKED:
        return state, reason, None
    if stored.get("level") not in ("assistant", "everything"):
        return NOT_ASKED, "no-level", None
    if stored.get("strong_model"):
        return SETTLED, "", None
    gate = capture_gate(ctx)
    return (NOT_ASKED, gate, None) if gate else (OPEN, "", None)


def check_task_reminder(ctx):
    """Asked until the user chose, here or in a terminal under 1.6.0 (the observer's advisory mode of this folder is shadow or
    deliver): the reminder has its own setting and needs no observer."""
    import task_reminder
    found = task_reminder.read(ctx.repo)
    if found["state"] == "unreadable":
        return NOT_ASKED, "reminder-unreadable", None
    if found["state"] == "chosen":
        return SETTLED, "", None
    state, reason, stored = observer_entry(ctx)
    if state == OPEN and stored.get("advisory_mode") in ("shadow", "deliver"):
        return SETTLED, "", None
    return OPEN, "", None


def check_map_files(ctx):
    """Settled once this folder and repository have a selection (an empty one counts). Else asked only when a file of the folder
    has a name the map looks for (project_map_sources.SUGGESTED_NAMES), found from names alone: no file is read and no program
    is started."""
    import project_map_sources
    import setup
    try:
        path = project_map_sources.scope_path(ctx.root, ctx.repo)
        if path and storage.check_path(path, "file"):
            return SETTLED, "", None
    except (storage.StorageError, ValueError, OSError):
        return NOT_ASKED, "check-failed", None

    def names_found():
        return [name for name, _ in setup.names_in(ctx.root) if project_map_sources.valid(name)
                and os.path.splitext(os.path.basename(name))[0].lower() in project_map_sources.SUGGESTED_NAMES]
    shown = [name for name in ctx.once("map-files", names_found) if EVIDENCE.fullmatch(name)][:6]
    return (OPEN, "", shown) if shown else (NOT_ASKED, "no-map-files", None)


def check_map_upkeep(ctx):
    """Asked at Assistant and Everything, until the stored map mode is shadow or automatic; not where capture is not ready."""
    state, reason, stored = observer_entry(ctx)
    if state == NOT_ASKED:
        return state, reason, None
    if stored.get("level") not in ("assistant", "everything"):
        return NOT_ASKED, "no-level", None
    if stored.get("map_mode") in ("shadow", "automatic"):
        return SETTLED, "", None
    gate = capture_gate(ctx)
    return (NOT_ASKED, gate, None) if gate else (OPEN, "", None)


# ---------------------------------------------------------------- the registry

class Option:
    """One answer the user can pick: its label, the answer to record (yes, no or later), whether it is the one the
    session recommends, and the commands to run first (each a tuple of words; a word in <angle brackets> is for the
    session to fill in)."""

    def __init__(self, label, answer, commands=(), recommended=False, note="", description=""):
        self.label, self.answer, self.commands, self.recommended, self.note = label, answer, commands, recommended, note
        self.description = description   # fixed, ASCII, 20 to 120 characters: Claude Code's question tool needs one for each option


class Question:
    def __init__(self, id, since, scope, where, check, text, options):
        self.id, self.since, self.scope, self.where, self.check = id, since, scope, where, check
        self.text, self.options = text, options


def dry_then_real(*words):
    return (tuple(words) + ("--dry-run",), tuple(words) + ("--yes",))


LATER = "Ask again in seven days."


def level_option(level, label, recommended, description=""):
    return Option(label, "yes", dry_then_real("setup.py", "observer", "--level", level), recommended,
                  "Other: \"no\" is recorded as no for good; \"shadow\" turns it on at Notes only without posting "
                  "(--level notes --mode shadow); the user may name other models (--model for the note-taking calls, "
                  "--sort-model for the sorting calls).", description)


QUESTIONS = (
    Question(
        "writing-mode", "1.3.0", "repo", "this machine and this repository", check_writing_mode,
        "Should recortex write the text of this project's paper (Methods, Results, Discussion and Limitations of the "
        "topic bodies, from the plan and the record)? Review: it drafts, shows each change and writes it after your yes "
        "in the chat. Autonomous: it writes without asking. Off: it writes nothing. {consent}",
        (Option("Review", "yes", (("setup.py", "writing", "set", "--mode", "review", "--yes"),), True,
                description="Drafts each change, shows it, and writes it after your yes in the chat."),
         Option("Autonomous", "yes", (("setup.py", "writing", "set", "--mode", "autonomous", "--yes"),),
                description="Writes the text without asking; every change stays in the record's history."),
         Option("Off", "no", (("setup.py", "writing", "set", "--mode", "off", "--yes"),),
                description="Writes nothing; you write the paper's text yourself."),
         Option("Later", "later", description=LATER))),
    Question(
        "own-paper", "1.3.0", "repo", "the project file", check_own_paper,
        "Does the author keep a manuscript file of their own (LaTeX, Markdown or Word) that recortex should know about? "
        "recortex never changes it on its own: it says when the record's text has moved on, and brings changes over "
        "only when the author asks.",
        (Option("Yes", "yes", (("own_paper.py", "where", "--set", "<path of the file>", "--dry-run"),
                               ("own_paper.py", "where", "--set", "<path of the file>", "--yes")),
                note="The user names the file; no recommendation.",
                description="You name the file; recortex only reads it and says when the record's text has moved on."),
         Option("No", "no", (("own_paper.py", "where", "--set", "no", "--yes"),),
                description="There is no such file; recortex does not ask again."),
         Option("Later", "later", description=LATER))),
    Question(
        "manuscript-profile", "1.3.0", "repo", "the project file", check_manuscript_profile,
        "recortex: paper or manuscript files now exist in this project ({evidence}), and its setup has no manuscript profile. Add "
        "it? That adds the profile and its labels to the project file, with the plan of the paper and the writing of "
        "its text to follow; the deliverables the project has stay as they are.",
        (Option("Yes", "yes", dry_then_real("setup.py", "apply", "--answers", "<the answers file below>"), True,
                description="Adds the manuscript profile and its labels; what the project has stays as it is."),
         Option("No", "no", description="Keeps the setup as it is; recortex does not ask again."),
         Option("Later", "later", description=LATER))),
    Question(
        "labels", "1.2.0", "repo", "GitHub, noted on this machine", check_labels,
        "recortex: labels that this project's profiles use are missing on GitHub ({missing}). Create them? setup_labels.py "
        "creates only the missing ones and never changes or deletes an existing label.",
        (Option("Yes", "yes", (("setup_labels.py", "--dry-run"), ("setup_labels.py",)), True,
                description="Creates only the missing labels; never changes or deletes one."),
         Option("No", "no", description="Leaves the labels on GitHub as they are."),
         Option("Later", "later", description=LATER))),
    Question(
        "status-line", "1.2.0", "machine", "this machine", check_status_line,
        "Show recortex in the Claude Code status line, with the record's age? It copies one small script to your "
        "configuration folder and adds a statusLine entry to your Claude Code settings.json, only when that file has "
        "none.",
        (Option("Yes", "yes", dry_then_real("setup.py", "status-line"), True,
                description="Copies one small script and adds a statusLine entry to your Claude Code settings."),
         Option("No", "no", description="Leaves your settings as they are."),
         Option("Later", "later", description=LATER))),
    Question(
        "observer-level", "1.5.0", "repo", "this machine and this folder", check_observer_level,
        "Turn on the recortex observer (experimental)? It takes notes of the conversation (the prompts you type, the agent's "
        "messages to you, and the plans and questions you answered in a dialog) and sorts them in the background. "
        "Choose what it may do. Notes only: it keeps memories and the status summary, and asks you before it posts a "
        "memory it holds. Assistant: it also keeps a to-do list with checkmarks, checks off steps the agent reports as "
        "done and keeps the task lists current, and asks you before it opens or closes a task. Everything: it also "
        "opens and closes tasks and keeps the board and the sub-issue links. At every level it never writes the "
        "paper's text on its own: a draft waits for your yes. Its calls run under your Claude sign-in and count against "
        "your usage, at most {sort} quick sorting calls, {middle} note-taking calls and {strong} paper-text calls a day "
        "for the project (with an API key or another provider set, they are billed to it instead), and it needs the "
        "repository in your trusted list. An observer you turned on with an earlier version stays paused until you "
        "choose.",
        (level_option("everything", "Everything (Recommended)", True,
                      "Notes and to-dos, and it opens and closes tasks and keeps the board and links itself."),
         level_option("assistant", "Assistant", False,
                      "Notes and a to-do list with checkmarks; it asks before it opens or closes a task."),
         level_option("notes", "Notes only", False,
                      "Memories and the status summary; it asks before it posts a memory it holds."),
         Option("Later", "later", description=LATER))),
    Question(
        "paper-proposals", "1.5.0", "repo", "this machine and this folder", check_paper_proposals,
        "May recortex draft a change of the paper's text in the background when something notable changes for a part, "
        "and ask you before anything is written? It uses a stronger model (opus), at most {strong} calls a day, and "
        "proposes at most {proposals} changes a day.",
        (Option("Yes (Recommended)", "yes", dry_then_real("setup.py", "observer", "--strong", "opus"), True,
                "Other: the user names another model, and it replaces opus in both commands.",
                description="Drafts changes of the paper's text with opus; nothing is written without your yes."),
         Option("No", "no", description="No drafts of the paper's text in the background."),
         Option("Later", "later", description=LATER))),
    Question(
        "task-reminder", "1.7.0", "repo", "this machine and this repository", check_task_reminder,
        "Remind this session when a prompt touches work that rests on a decision the record later changed? recortex "
        "then adds one line to that prompt with the addresses of the earlier decision, the current one and the work, so "
        "that the session checks before it advises. It reads your prompt and the local copy of the record on this machine, "
        "makes no model call, posts nothing, and needs no observer.",
        (Option("Yes (Recommended)", "yes", (("setup.py", "task-reminder", "set", "--mode", "on", "--yes"),), True,
                description="Adds one line to a matching prompt; no model call, nothing posted, kept on this machine."),
         Option("No", "no", (("setup.py", "task-reminder", "set", "--mode", "off", "--yes"),),
                description="No reminders on prompts."),
         Option("Later", "later", description=LATER))),
    Question(
        "map-files", "1.7.0", "repo", "this machine and this folder", check_map_files,
        "May recortex read the headings of project documents you choose for the Project Map ({files})? Only the "
        "files you pick are read, only their headings, on this machine, and nothing is posted until you accept a map "
        "change.",
        (Option("Choose files", "yes", (("setup.py", "map-files"), ("setup.py", "map-files", "--select", "<each chosen path>",
                                                                    "--yes")), True,
                "Ask which of the listed files as one multi-select question; repeat --select for each.",
                description="You pick the files; only their headings are read, on this machine."),
         Option("No files", "no", (("setup.py", "map-files", "--clear", "--yes"),),
                description="The map is built from your goals and the record only."),
         Option("Later", "later", description=LATER))),
    Question(
        "map-upkeep", "1.7.0", "repo", "this machine and this folder", check_map_upkeep,
        "Let the recortex observer keep the Project Map current? Automatic: it adds a branch or renames one on its "
        "own when your typed words and a trusted record both support it and a second check agrees; every move, merge, "
        "split, deletion and uncertain change waits for your yes. Suggest only: it keeps its proposals on this machine and "
        "changes nothing. Both use the note-taking calls, within the same daily caps.",
        (Option("Automatic (Recommended)", "yes", (("observer.py", "map-mode", "automatic"),
                                                   ("observer.py", "map-mode", "automatic", "--yes")), True,
                description="Adds and renames branches after a second check; every other change waits for your yes."),
         Option("Suggest only", "yes", (("observer.py", "map-mode", "shadow"), ("observer.py", "map-mode", "shadow", "--yes")),
                description="Keeps proposals on this machine for you to read; changes nothing on GitHub."),
         Option("No", "no", description="The map changes only when you ask."),
         Option("Later", "later", description=LATER))),
    Question(
        "status-line-combine", "1.7.0", "machine", "this machine", check_status_line_combine,
        "Your Claude Code settings already have a status line that does not show recortex. Show the one line to add "
        "to it, so that it also shows whether the record is on and how old it is? recortex changes no file for this.",
        (Option("Show how (Recommended)", "yes", (("setup.py", "status-line", "--dry-run"),), True,
                "Show the user the snippet it prints; change their status line only if they ask.",
                description="Prints the one line to add to your own status line; changes no file."),
         Option("No", "no", description="Leaves your status line as it is."),
         Option("Later", "later", description=LATER))),
)
IDS = tuple(question.id for question in QUESTIONS)
# A question that a later version replaced: its id is still accepted by `setup.py answer`, and an answers file from an
# earlier version that holds it still loads. The 1.4.0 `observer` question became `observer-level` in 1.5.0.
RETIRED = {"observer": "observer-level"}
ACCEPTED = IDS + tuple(RETIRED)


def find(question_id):
    """The question with this id; a retired id finds the question that replaced it."""
    question_id = RETIRED.get(question_id, question_id)
    return next((question for question in QUESTIONS if question.id == question_id), None)


# ---------------------------------------------------------------- what is open

class Result:
    """One question, as it stands: `status` is open, answered, deferred, not-asked, or unread (the labels, when GitHub
    could not be read); `reason` a key of REASONS for not-asked, or "complete" for labels found on GitHub; `detail` the
    evidence of the profile question or the missing labels; `entry` the stored answer, if any."""

    def __init__(self, question, status, reason="", detail=None, entry=None):
        self.question, self.status, self.reason, self.detail, self.entry = question, status, reason, detail, entry


def evaluate(ctx, data, today=None, labels=None):
    """A Result for each question of QUESTIONS, in order. `data` is the state of this machine. A stored answer
    overrides open: yes and no close the question, and later closes it for LATER_DAYS days. `labels` is None for the hook,
    which counts the labels as one to check, or a function that reads GitHub and returns the names of the wanted
    labels that are missing, [] when none is, or None when GitHub could not be read; it runs only when the labels
    would otherwise be open."""
    today = today or datetime.date.today()
    out = []
    for question in QUESTIONS:
        try:
            state, reason, detail = question.check(ctx)
        except Exception:  # noqa: BLE001 - a check that fails is a question that is not asked; --doctor says so
            state, reason, detail = NOT_ASKED, "check-failed", None
        entry = entry_of(data, question, ctx.repo)
        if state == NOT_ASKED:
            out.append(Result(question, "not-asked", reason, entry=entry))
        elif state == SETTLED:
            out.append(Result(question, "answered", entry=entry))
        elif entry is not None and entry["answer"] in ("yes", "no"):
            out.append(Result(question, "answered", entry=entry))
        elif entry is not None and deferred(entry, today):
            out.append(Result(question, "deferred", entry=entry))
        elif question.id == "labels" and labels is not None:
            missing = labels()
            if missing is None:
                out.append(Result(question, "unread", entry=entry))
            elif not missing:
                out.append(Result(question, "answered", "complete", entry=entry))
            else:
                out.append(Result(question, "open", detail=missing, entry=entry))
        else:
            out.append(Result(question, "open", detail=detail, entry=entry))
    return out


LABEL_SHOWN = re.compile(r"[A-Za-z0-9:._ -]{1,50}")


def missing_labels(repo, config):
    """The names of the labels the project's profiles and topics want that GitHub does not list: one label listing,
    through the record store. [] when none is missing, and for a record that keeps no label catalog; None when the record
    could not be read (or the profiles do not fit), which is one fixed line for the session and never a stop."""
    import setup_labels
    try:
        wanted = setup_labels.wanted_labels(list(config.get("profiles") or []), config.get("topics") or [])
        store = record_store.for_repo(repo, config)
        if not store.supports("label-catalog"):
            return []
        listed = store.list_labels("name,color,description", timeout=30)
    except (recortex.GhError, ValueError, RecursionError, SystemExit, TypeError, AttributeError):
        return None
    if not isinstance(listed, list):
        return None
    have = {label["name"].lower() for label in listed if isinstance(label, dict) and isinstance(label.get("name"), str)}
    return [label["name"] for label in wanted if label["name"].lower() not in have]


def open_questions(repo, config, root, record, data=None, today=None, labels=None):
    """The Results that are open now. `data` defaults to the file of this machine; a file that cannot be read raises
    what load raises."""
    data = load() if data is None else data
    return [r for r in evaluate(Context(repo, config, root, record), data, today, labels) if r.status == "open"]


# ---------------------------------------------------------------- the hook

def hook_parts(repo, config, record, session, room, state_io=None):
    """(line, skip): the one fixed line the session start prints for the setup questions, "" when there is none, and the
    set of deliverable kinds whose signal line stays quiet because a question already asks about them. Nothing read
    is printed. The line is "" when the questions are switched off, when none is open, when it does not fit `room`
    (it is then not marked as shown), and when it was already shown for this session id. SETUP_UNREADABLE comes at
    every session start while the file of answers cannot be read and a question would be open without it. Other
    failures give no line: the brief is shown as usual, and hooks.py --doctor has the detail."""
    import brief
    if off():
        return "", set()
    root = os.path.realpath(recortex.project_root())
    ctx = Context(repo, config, root, record)
    found = read()
    if found["state"] == "unreadable":
        would_open = any(r.status == "open" for r in evaluate(ctx, empty()))
        line = brief.SETUP_UNREADABLE
        return (line if would_open and len(line) + 1 <= room else ""), set()
    results = evaluate(ctx, found["data"])
    by_id = {r.question.id: r for r in results}
    skip = {"paper"} if by_id["manuscript-profile"].status in ("open", "deferred") else set()
    count = sum(1 for r in results if r.status == "open")
    if not count:
        return "", skip
    line = brief.SETUP_QUESTIONS % (count, "" if count == 1 else "s")
    if len(line) + 1 > room:
        return "", skip
    try:
        if not mark_asked(repo, session, state_io):
            return "", skip
    except Exception:  # noqa: BLE001 - not marked, so not shown
        return "", skip
    return line, skip


def hook_line(repo, config, record, session, room, state_io=None):
    """The line of hook_parts."""
    return hook_parts(repo, config, record, session, room, state_io)[0]


def mark_asked(repo, session, state_io=None):
    """True when the line was not yet shown in this session; then `asked: true` is stored in the session's entry of the
    prompt state before it returns, as the offer of the field is. An empty session id is never marked, so the line is
    shown each time. `state_io` is (load, save) of hooks.py."""
    if not session:
        return True
    if state_io is None:
        import hooks
        state_io = (hooks.load_state, hooks.save_state)
    load_state, save_state = state_io
    state = load_state(repo)
    entry = state.get(session, {"count": 0})
    if entry.get("asked") is True:
        return False
    entry["asked"] = True
    entry["seen"] = time.time()
    state[session] = entry
    save_state(repo, state)
    return True


# ---------------------------------------------------------------- the reminder of the prompt hook

REMIND_MAX = 3     # reminders to one session
REMIND_FROM = 2    # the prompt of the session (counted by the prompt hook) that may carry the first
REMIND_GAP = 3     # prompts between one reminder and the next


def run_path(repo):
    """Where `setup.py questions` notes when it ran, in the repository's folder of the hook state (the folder that holds
    prompt-state.json, through storage.py)."""
    return os.path.join(storage.folder(storage.state_top(), storage.repo_folder(repo)), names.SETUP_RUN)


def note_run(repo, now=None):
    """Note that `setup.py questions` ran just now: {"ran": seconds} in a private file written through storage.py. Raises what
    storage raises; the caller does not let a failure stop the command."""
    storage.write_file(run_path(repo), json.dumps({"ran": time.time() if now is None else now}))


def number(value):
    """value when it is a finite positive number that is no bool, else None."""
    if type(value) in (int, float) and 0 < value < 1e12:
        return float(value)
    return None


def last_run(repo):
    """When `setup.py questions` last ran for the repository, in seconds, or None when it never did or the file does not hold
    exactly that. Raises storage.StorageError for an unsafe file (the reminder then stays out)."""
    text = storage.read_file(run_path(repo))
    try:
        data = json.loads(text) if text else None
    except (ValueError, RecursionError):
        return None
    return number(data.get("ran")) if isinstance(data, dict) and set(data) == {"ran"} else None


def reminder(repo, config, record, entry, room, now=None):
    """The one fixed line that reminds a session of the setup questions on a later prompt, or "". `entry` is the session's entry of
    the prompt state (hooks.load_state): `start` (when the session's first start was seen), `count` (its prompts), `reminded` and
    `reminded_at`. The line is given only when the questions are not switched off, the session has a start and has had REMIND_FROM
    prompts, fewer than REMIND_MAX reminders (REMIND_GAP prompts apart), `setup.py questions` has not run since the session started,
    a question is open now (counted from local files, as the session start counts them) and the line fits `room`. When it is
    given, `reminded` and `reminded_at` are set in `entry`, which the caller then saves. It holds fixed words and a number, and
    makes no GitHub call. Cheap checks come first, so that most prompts read nothing."""
    import brief
    now = time.time() if now is None else now
    start, prompts = number(entry.get("start")), entry.get("count")
    reminded, at = entry.get("reminded", 0), entry.get("reminded_at", 0)
    if start is None or start > now + 60 or type(prompts) is not int or prompts < REMIND_FROM:
        return ""
    if type(reminded) is not int or not 0 <= reminded < REMIND_MAX or type(at) is not int:
        return ""
    if reminded and prompts - at < REMIND_GAP:
        return ""
    if off():
        return ""
    ran = last_run(repo)
    if ran is not None and ran >= start:
        return ""
    found = read()
    if found["state"] == "unreadable":
        return ""
    results = evaluate(Context(repo, config, os.path.realpath(recortex.project_root()), record), found["data"])
    count = sum(1 for r in results if r.status == "open")
    if not count:
        return ""
    line = brief.SETUP_REMINDER % (count, "" if count == 1 else "s")
    if len(line) + 1 > room:
        return ""
    entry["reminded"], entry["reminded_at"] = reminded + 1, prompts
    return line


def doctor_lines(repo, config, record):
    """For --doctor: one line with the ids that are open, answered and deferred, and why the others are not asked, in
    fixed words. Never a path, a name or a value read from a file. The labels count as open (the hook counts them so; setup.py
    questions reads GitHub)."""
    if off():
        return ["setup questions: switched off (RECORTEX_SETUP_QUESTIONS, or \"setup_questions\": false in %s)"
                % recortex.settings_file()]
    found = read()
    if found["state"] == "unreadable":
        return ["setup questions: the answers of this machine cannot be read (%s), so no question is asked; the file's "
                "text is not shown" % NOT_READABLE[found["why"]]]
    results = evaluate(Context(repo, config, os.path.realpath(recortex.project_root()), record), found["data"])
    groups = []
    for status in ("open", "answered", "deferred"):
        ids = [r.question.id for r in results if r.status == status]
        if ids:
            groups.append("%s: %s" % (status, ", ".join(ids)))
    skipped = ["%s (%s)" % (r.question.id, REASONS.get(r.reason, "not applicable")) for r in results
               if r.status == "not-asked"]
    if skipped:
        groups.append("not asked: %s" % ", ".join(skipped))
    return ["setup questions: %s" % "; ".join(groups)]


# ---------------------------------------------------------------- what `setup.py questions` prints

def render_command(words):
    """A command of an option as the session runs it: the script by its full path, every word quoted, a word in angle
    brackets (or the three answers, for the command that records one) left for the session to fill in."""
    script, rest = words[0], words[1:]
    first = "python3 %s" % shlex.quote(os.path.join(HERE, script))
    return " ".join([first] + [word if re.fullmatch(r"<[a-z ]{1,40}>|yes\|no\|later", word) else shlex.quote(word)
                               for word in rest])


def answers_file(repo, config):
    """The answers file that `setup.py apply` takes for the manuscript profile: the repository and one deliverable of
    kind paper, `prompt_hook` and `read_transcripts` copied from the project file when they fit (new_config would
    otherwise reset them), and the optional flags that no question of the session asks false. The status line and the
    observer are left out: apply reads an answer written in so many words as the user's own, and they have questions of
    their own. Nothing else of the project file is repeated: apply keeps the rest."""
    import setup
    answers = {"repo": repo, "deliverables": [{"kind": "paper"}]}
    hook = config.get("prompt_hook")
    if isinstance(hook, dict):
        try:
            setup.check_answers(dict(answers, prompt_hook=hook))
            answers["prompt_hook"] = hook
        except setup.AnswerError:
            pass
    if isinstance(config.get("read_transcripts"), bool):
        answers["read_transcripts"] = config["read_transcripts"]
    for flag in ("catch_up", "triage_workflow", "claude_md", "trust"):
        answers[flag] = False
    # "status_line" and "observer" are left out (apply reads both as no): written as false, apply would record a "no" to
    # two questions the user was not asked
    return answers


def question_text(result, repo):
    """The fixed question wording, with the repository (a name that passed valid_repo), the evidence of the profile
    question (words checked against EVIDENCE) and the labels that are missing (names checked against LABEL_NAME)."""
    text = result.question.text
    if "{consent}" in text:
        text = text.replace("{consent}", " ".join(writing.consent_lines(repo)))
    if "{sort}" in text or "{strong}" in text:
        import observer_runtime
        text = text.format(sort=observer_runtime.DEFAULTS["max_sort"], middle=observer_runtime.DEFAULTS["max_invocations"],
                           strong=observer_runtime.DEFAULTS["max_strong"],
                           proposals=observer_runtime.DEFAULTS["max_proposals"])
    if "{files}" in text:
        text = text.replace("{files}", ", ".join(result.detail or []) or "see setup.py map-files")
    if "{evidence}" in text:
        text = text.replace("{evidence}", ", ".join(result.detail or []) or "see setup.py detect")
    if "{missing}" in text:
        names_shown = [name for name in (result.detail or []) if LABEL_SHOWN.fullmatch(name)][:8]
        total = len(result.detail or [])
        text = text.replace("{missing}", "%d: %s%s" % (total, ", ".join(names_shown) or "see setup_labels.py --dry-run",
                                                      ", and more" if total > len(names_shown) else ""))
    return text


def describe(result, repo, config):
    """The lines for one open question: its id, since and scope in words, the question, one line per option with the
    exact commands, and the command that records the answer."""
    question = result.question
    lines = ["question %s (added in %s; the answer is kept for: %s)" % (question.id, question.since, question.where),
             "  " + question_text(result, repo)]
    if question.id == "manuscript-profile":
        lines.append("  the answers file for apply (write it to a scratch file, then run the commands below with it):")
        lines.append("  " + json.dumps(answers_file(repo, config)))
    for option in question.options:
        steps = [render_command(words) for words in option.commands] or ["nothing to run"]
        steps.append(render_command(record_words(question, option.answer)))
        what = "  - %s%s: %s Run: %s" % (option.label, " (recommended)" if option.recommended else "", option.description,
                                          " ; then ".join(steps))
        lines.append(what + (" (%s)" % option.note if option.note else ""))
    lines.append("  record the answer: %s" % render_command(record_words(question, "yes|no|later")))
    return lines


def record_words(question, answer):
    """The command that records `answer` for `question`."""
    return ("setup.py", "answer", "--id", question.id, "--answer", answer)

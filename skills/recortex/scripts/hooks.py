#!/usr/bin/env python3
"""Hook entry point: print the record brief for one Claude Code hook event.

Events:
  session-start   syncs the index from GitHub within the hook's time limit, then
                  prints the record brief; if GitHub cannot be read it prints
                  the brief from the index and says how old it is
  subagent-start  prints a short brief for a subagent as JSON additionalContext
                  (index only, no network)
  prompt-submit   prints one or two lines from the index (no network), only when
                  they changed since the last time in this session or every
                  N prompts; off with "prompt_hook": {"enabled": false} in
                  .recortex.json, N set with "every"
  pre-compact     prints instructions for the compaction summary (index only)

Every event prints nothing and exits 0 when the project has no
.recortex.json, when its repository is not in the user's trusted list,
when there is no index yet, or on any error, with two exceptions of fixed
wording: at session start in the top folder of a git checkout with a GitHub
remote and no .recortex.json, one line offers to set up the record,
once per project (off with RECORTEX_DISCOVERY=0 or "discovery": false in
~/.config/recortex/settings.json); and in a project that is set up and
trusted, one line says why the brief is not shown. Nothing here writes to GitHub.

A project's field sheet (field_sheet.py) reaches a subagent brief only when the
user approved that exact text on this machine, in its own frame between BEGIN and
END FIELD GUIDANCE lines. The session brief gets one fixed line about it: that it
is shown, or why it is not, with no text of the sheet. In a project with no
fields entry, the session start carries one fixed line, once per project and
machine, that offers to set the field.

At session start the brief may also carry one fixed line, once per session, saying how many setup questions an update
added that this project and this machine have not answered yet (setup_questions.py); it holds a number and fixed words,
reads only local files and makes no GitHub call. If the session has not run `setup.py questions` by its second prompt, one
more fixed line with the count is added to a later prompt (prompt-submit), at most three times a session and only while the prompt
hook is on. Off with RECORTEX_SETUP_QUESTIONS=0 or "setup_questions": false in the settings file of the plugin.

--print shows the four payloads and their sizes, for the user to check.
--doctor says why a brief is not shown: configuration, trust, whether gh is
found and signed in, the storage folders with owner and mode, the index and its
age, the last sync failure and the payload sizes. Of GitHub it asks only which
account gh is signed in as and, for a project with a board, about the board;
it changes nothing, and never prints the key or other secrets.

Examples:
  hooks.py --print
  hooks.py --doctor
  echo '{"session_id": "abc"}' | hooks.py prompt-submit
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recortex  # noqa: E402
import names  # noqa: E402
import index  # noqa: E402
import brief  # noqa: E402
import storage  # noqa: E402
import guidance
import field_sheet  # noqa: E402
import board_api  # noqa: E402
import board_rules  # noqa: E402
import usage  # noqa: E402

EVENTS = ("session-start", "subagent-start", "prompt-submit", "pre-compact")
STOP_EVENT = "subagent-stop"   # prints nothing; see subagent_stop()
STOP_AGENTS = re.compile(r"recortex:(?:record-auditor|record-drafter|record-reviewer|record-planner|record-planner-session|ask)")
# Why recortex is not working for a project folder, as the fixed words of the line at session start. The status line
# (statusline.py) has its own short words for the same codes; a test holds the two sets of codes equal.
STATUS_REASONS = {
    "not-trusted": "this repository is not in your trusted list",
    "no-hub": "the project file names no hub or cannot be read",
    "storage": "the local storage was refused",
    "no-index": "the record has not been read yet",
    "error": "an unexpected error",
}
NOTICE_REPO = r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9._-]{1,100}"
NOTICE_READ = r"read (?:just now|[0-9]{1,6} (?:minutes?|hours|days) ago|at an unknown time)(?:; GitHub could not be read now)?"
DASHBOARD_HINT = "Ask to open your project dashboard or type /recortex:dashboard."
NOTICE_FORM = re.compile(
    r"recortex: (?:on\. (?:(?:Record %s, %s\.|The record was %s\.)(?: %s)?|The record(?: of %s)? is being read for the first time; "
    r"the brief follows with your next message\.)|off in this session: (?:%s)\. Run hooks\.py --doctor\.)"
    % (NOTICE_REPO, NOTICE_READ, NOTICE_READ, re.escape(DASHBOARD_HINT), NOTICE_REPO,
       "|".join(re.escape(words) for words in STATUS_REASONS.values())))
START_SOURCES = ("startup", "resume", "clear")   # a start after a compaction or a fork repeats inside one sitting
HOOK_NAMES = {"session-start": "SessionStart", "subagent-start": "SubagentStart",
              "prompt-submit": "UserPromptSubmit", "pre-compact": "PreCompact"}


def read_input():
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def state_path(repo):
    """The prompt-hook state file, in a checked folder outside the project tree (see storage.py)."""
    private = storage.folder(storage.state_top(), storage.repo_folder(repo))
    return os.path.join(private, "prompt-state.json")


def load_state(repo):
    """Raises storage.StorageError when the state file is a symlink or not safe; unreadable JSON counts as empty."""
    text = storage.read_file(state_path(repo))
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save_state(repo, state):
    def seen(item):
        value = item[1].get("seen", 0)
        return value if isinstance(value, (int, float)) else 0
    sessions = sorted(state.items(), key=seen)[-50:]  # keep the 50 sessions used most recently
    storage.write_file(state_path(repo), json.dumps(dict(sessions)))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class OutOfTime(BaseException):
    """Raised by the alarm; a BaseException so that no `except Exception` inside the sync swallows it."""


@contextlib.contextmanager
def alarm(seconds):
    """Interrupt the block after `seconds`, where the platform allows it, so a slow GitHub cannot eat the budget."""
    if not hasattr(signal, "setitimer"):
        yield
        return

    def ring(signum, frame):
        raise OutOfTime()

    previous = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


LAST = {}   # what the last run learned, for the status file and the line at session start: state, reason, fresh, synced_at


def notice_off():
    """True when the user switched the line at session start off: "notice": false in the settings file that holds
    "discovery", or RECORTEX_NOTICE set to 0, off, no or false."""
    if names.env("NOTICE", "").strip().lower() in ("0", "off", "no", "false"):
        return True
    try:
        with open(recortex.settings_file(), encoding="utf-8") as handle:
            return json.load(handle).get("notice") is False
    except (OSError, ValueError, AttributeError):
        return False


def when(synced, fresh):
    """The words after "read": just now for a sync made at this start, else the age of the last one, only from a stamp
    of the UTC form."""
    if fresh is True:
        return "just now"
    age = index.age_text(synced) if isinstance(synced, str) and index.ISO_UTC.fullmatch(synced) else ""
    return age[:-4] + " ago" if age.endswith(" old") else "at an unknown time"


def notice(status, source=None):
    """The visible status line, including how to open the dashboard at startup when a trusted record is available."""
    repo = status.get("repo") if recortex.valid_repo(status.get("repo")) else ""
    if status.get("state") == "off":
        words = STATUS_REASONS.get(status.get("reason"), STATUS_REASONS["error"])
        return "recortex: off in this session: %s. Run hooks.py --doctor." % words
    if status.get("state") == "building":
        named = "recortex: on. The record of %s is being read for the first time; the brief follows with your next "                 "message." % repo
        bare = "recortex: on. The record is being read for the first time; the brief follows with your next message."
    else:
        read = "read %s%s" % (when(status.get("synced_at"), status.get("fresh")),
                              "; GitHub could not be read now" if status.get("fresh") is False else "")
        named, bare = "recortex: on. Record %s, %s." % (repo, read), "recortex: on. The record was %s." % read
        if source == "startup" and status.get("state") == "on" and repo:
            named += " " + DASHBOARD_HINT
            bare += " " + DASHBOARD_HINT
    return named if repo and len(named) <= 200 else bare


def refresh_statusline():
    """Keep the copy of statusline.py that setup.py made current when the plugin's file differs. A hook never creates a
    copy, and never touches settings.json."""
    try:
        copy = os.path.join(recortex.config_home(), names.NAME, "statusline.py")
        if os.path.islink(copy) or not os.path.isfile(copy):
            return
        text = recortex.read_text(os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline.py"))
        if recortex.read_text(copy) != text:
            storage.write_file(copy, text)
    except (OSError, ValueError, storage.StorageError):
        pass


def write_status(state, reason="", repo="", synced="", observer="", steps=None):
    """The state of the record for this project folder, for the status line: {"v", "state", "reason", "repo",
    "synced_at", "observer"} and nothing else, written through storage.write_file. `observer` is "paused" while an
    observer approved before 1.5.0 waits for the user to choose a level, else "". `steps` is None, or (steps done, steps)
    of the open tasks that have steps: two checked integers, "steps_done" and "steps_total", added to the file only then.
    A hook never fails over it."""
    try:
        path = storage.status_path(recortex.project_root())
        data = {"v": 1, "state": state, "reason": reason, "repo": repo, "synced_at": synced, "observer": observer}
        if steps and all(type(n) is int for n in steps) and 0 <= steps[0] <= steps[1] <= 99999:
            data.update(steps_done=steps[0], steps_total=steps[1])
        storage.write_file(path, json.dumps(data))
    except (OSError, ValueError, storage.StorageError):
        pass


def why_not_configured():
    """None when the folder has no project file; else why recortex is not working here: no-hub for a file that cannot
    be read or names no hub, not-trusted for a repository that is not in the trusted list."""
    try:
        if not recortex.find_config():
            return None
        config = recortex.load_config()
    except Exception:  # noqa: BLE001
        return "no-hub"
    repo, hub = config.get("repo"), config.get("hub")
    if not recortex.valid_repo(repo) or isinstance(hub, bool) or not isinstance(hub, int) or hub < 1:
        return "no-hub"
    return "not-trusted"


def configured():
    """(config, repo, hub) when the project is set up and trusted, else None."""
    config = recortex.load_config()
    repo, hub = config.get("repo"), config.get("hub")
    if not recortex.valid_repo(repo) or isinstance(hub, bool) or \
            not isinstance(hub, int) or hub < 1:
        return None
    if repo.lower() not in recortex.trusted_repos():
        return None
    return config, repo, hub


NOT_SHOWN = "recortex: record brief not shown: %s. Run hooks.py --doctor."
# One fixed line, outside the data frame and outside brief.CAPS, added to the prompt hook's output when the user's message is the
# typed answer to a question of acts.py that the session showed as text (observer_grants.typed): no value in it.
TYPED = ("recortex: the user's typed answer to the plugin's question was recorded. Run the command that the chosen option "
         "names.")


def typed_answer(data):
    """True when this prompt, the main session's own (not a subagent's), is the typed "yes" to a question that `acts.py ask
    --typed` noted for this same session: the grant for that option is then recorded (observer_grants.typed), only when this
    process was started the way Claude Code starts a hook (plausibly_a_hook), and the act confirms the words against the
    transcript before it runs (confirm_typed). A typed "no" removes the note and the grant, and prints nothing. Never raises."""
    try:
        if not isinstance(data, dict) or data.get("agent_id") or data.get("agent_type"):
            return False
        prompt = data.get("prompt")
        if not isinstance(prompt, str):
            return False
        import observer_grants
        import observer_runtime
        return observer_grants.typed(observer_runtime.folder_key(), prompt, data.get("session_id"),
                                     plausible=observer_grants.plausibly_a_hook) == "yes"
    except Exception:  # noqa: BLE001 - a hook never fails over a typed answer
        return False


class Unavailable(Exception):
    """The project is set up and trusted, but there is no brief to show."""


def run(event, data, budget):
    """The text a hook prints. Silent when the project is not set up or not trusted; one line when the brief fails."""
    LAST.clear()
    try:
        found = configured()
    except Exception:  # noqa: BLE001 - a project file that cannot be read
        found = None
    if found is None:
        code = why_not_configured() if event == "session-start" else None
        if code:
            LAST.update(state="off", reason=code)
            if event == "session-start":
                write_status("off", code)
        return discovery() if event == "session-start" else ""
    config, repo, hub = found
    LAST.update(state="on", reason="", repo=repo)
    code = "error"
    told = TYPED if event == "prompt-submit" and typed_answer(data) else ""   # first: whatever the brief does, the answer counts
    try:
        text = produce(event, data, budget, config, repo, hub)
        if event in ("session-start", "prompt-submit"):
            building = LAST.get("building") is True
            write_status("building" if building else "on", "", repo, LAST.get("synced_at") or "",
                         "paused" if observer_paused(repo, config) else "", LAST.get("steps"))
            LAST["state"] = "building" if building else "on"
        return text + ("\n" + told if text and told else told)
    except Unavailable as error:
        reason, code = str(error), "no-index"
    except storage.StorageError as error:
        code = "storage"
        reason = "the local storage was refused (%s)" % STORAGE_REASONS.get(getattr(error, "kind", ""),
                                                                            "a storage folder or file is not safe to use")
    except Exception as error:  # noqa: BLE001 - a hook never fails a session
        reason = "an unexpected %s" % type(error).__name__
    LAST.update(state="off", reason=code)
    if event in ("session-start", "prompt-submit"):
        write_status("off", code, repo)
    line = NOT_SHOWN % reason
    if event == "session-start":
        return line
    if event == "subagent-start":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": line}})
    return told


def subagent_stop(data):
    """SubagentStop: keep what one of the plugin's own subagents used, as four whole numbers and its model name, in
    usage.db. Prints nothing and returns no context. Nothing is done unless the project is set up and trusted, `agent_type`
    is one of the plugin's subagents (an anchored match, so another plugin's agent called "ask" is not), and the transcript
    path is this project's own native subagent transcript (observer_sources.approved_path: the session's
    subagents/agent-*.jsonl in this project's folder, owner and mode checked, no symlink). The transcript is not complete
    when the hook fires, so usage.measure reads it again for at most 1.5 s until the subagent's last message is there; if it
    never is, no row is written. Only integers and a model name of a fixed form leave the transcript."""
    found = configured()
    if found is None or not isinstance(data, dict):
        return
    agent, path, session = data.get("agent_type"), data.get("agent_transcript_path"), data.get("session_id")
    if not isinstance(agent, str) or not STOP_AGENTS.fullmatch(agent):
        return
    import observer_sources
    try:
        approved = observer_sources.approved_path(path, recortex.project_root(), session)
        measured = usage.measure(approved)
    except observer_sources.SourceError as error:
        # not a silent failure: one fixed code is kept, usage.py show and --doctor say so
        if str(error) in observer_sources.UNREADABLE_CODES:
            usage.note_unmeasured(found[1], str(error))
        return
    except (OSError, ValueError):
        # a transcript that cannot be read at all: the same note, with the code for it (usage.py show and --doctor say so)
        usage.note_unmeasured(found[1], "source-unavailable")
        return
    if measured is not None and usage.note_agent(found[1], agent, measured[1], measured[0]):
        usage.clear_unmeasured(found[1])


def note_context(event, text):
    """Count what a printed hook payload added to the context, for the usage view (an estimate): the characters of `text`,
    or of the context in the JSON a subagent start prints. Never raises, never slow (usage.py waits at most 50 ms)."""
    repo = LAST.get("repo")
    if not text or not recortex.valid_repo(repo):
        return
    context = text
    if event == "subagent-start":
        try:
            context = json.loads(text)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError, TypeError):
            context = text
    if isinstance(context, str):
        usage.note_injected(repo, event, len(context))


DISCOVERY = ("recortex is installed, and this project has no record yet. Ask to set up the record (a few "
             "questions), or to catch up on earlier work. This line is shown once per project.")
SIGNAL = ("recortex: the project now has %s (%s), a deliverable the setup does not cover. Ask the user once "
          "whether to add it with setup.py apply.")
SIGNAL_WORDS = {"paper": "paper or manuscript files", "thesis": "thesis files", "software": "software files",
                "dataset": "dataset files", "report": "analysis files", "prototype": "prototype files",
                "proposal": "proposal files", "docs": "documentation files"}


def discovery_off():
    if names.env("DISCOVERY", "").strip().lower() in ("0", "off", "no", "false"):
        return True
    try:
        with open(recortex.settings_file(), encoding="utf-8") as handle:
            return json.load(handle).get("discovery") is False
    except (OSError, ValueError, AttributeError):
        return False


def discovery():
    """One fixed line, once per project, for a git repository with a GitHub remote and no setup. No network; no
    project file is read (git reports the remotes)."""
    import subprocess
    if recortex.find_config() or discovery_off():
        return ""
    root = os.path.realpath(recortex.project_root())
    try:
        top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=3).stdout.strip()
        if not top or os.path.realpath(top) != root:
            return ""  # only the top folder of a git checkout is a project here
        remotes = subprocess.run(["git", "-C", root, "config", "--get-regexp", r"^remote\..*\.url$"],
                                 capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if "github.com" not in remotes:
        return ""
    private = storage.folder(storage.state_top(for_key=True))
    path = os.path.join(private, "discovery.json")
    try:
        seen = json.loads(storage.read_file(path) or "{}")
    except ValueError:
        seen = {}
    seen = {k: v for k, v in seen.items() if re.fullmatch(r"[0-9a-f]{16}", k) and isinstance(v, (int, float))} \
        if isinstance(seen, dict) else {}
    key = digest(root)
    if key in seen:
        return ""
    seen[key] = time.time()
    storage.write_file(path, json.dumps(seen))
    return DISCOVERY


def new_signal(repo, config, room, skip=()):
    """One fixed line naming a deliverable that files now suggest and the setup does not cover, once per kind. The
    line is optional: any failure (a malformed setup, refused storage, an unreadable folder) gives no line, and the
    brief is shown as usual. A kind in `skip` is left out: a setup question already asks about it."""
    try:
        import setup
        path = setup.told_path(repo)
        told = setup.read_told(path)
        covered = setup.covered_profiles(config)
        pending = {kind for kind, (profile, _) in setup.KINDS.items()
                   if kind in SIGNAL_WORDS and kind not in told and profile not in covered and kind not in skip}
        if not pending:
            return ""  # every kind is covered or already mentioned: the project folder is not looked at
        for item in setup.uncovered_signals(os.path.realpath(recortex.project_root()), config):
            if item["kind"] not in pending:
                continue
            line = SIGNAL % (SIGNAL_WORDS[item["kind"]], ", ".join(e for e in item["evidence"] if re.fullmatch(
                r"[\w ./*-]{1,40}", e))[:120])
            if len(line) + 1 > room:
                return ""
            storage.write_file(path, json.dumps(told + [item["kind"]]))
            return line
    except Exception:  # noqa: BLE001 - the line is optional; the brief does not depend on it
        return ""
    return ""


def setup_line(repo, config, record, session, room):
    """(line, skip): the one fixed line about the setup questions an update added, and the kinds of deliverable whose
    signal line stays quiet because a question asks about them (see setup_questions.py). Never raises: any failure gives no
    line and no skip, and the brief is shown as usual."""
    try:
        import setup_questions
        return setup_questions.hook_parts(repo, config, record, session, room, state_io=(load_state, save_state))
    except Exception:  # noqa: BLE001 - the line is optional; the brief does not depend on it
        return "", set()


def setup_reminder(repo, config, record, entry, session, room):
    """The reminder about the setup questions for a later prompt (setup_questions.reminder): one fixed line with a number, at most
    three times a session, and only while questions are open and `setup.py questions` has not run since the session started. It has
    to fit `room`, the rest of the cap of the prompt payload, so that it adds to no payload that is already full. Never raises:
    any failure gives no line, and the prompt's payload is as it was."""
    if not session:
        return ""
    try:
        import setup_questions
        return setup_questions.reminder(repo, config, record, entry, room)
    except Exception:  # noqa: BLE001 - the line is optional; the prompt hook does not depend on it
        return ""


def field_offer(repo, record, room):
    """The offer to set the field: one fixed line, once per project and machine, in a project with no fields entry.
    It is remembered before it is returned. The line is optional: any failure (refused storage, an unreadable file)
    gives no line, and the brief is shown as usual."""
    try:
        if record.field.get("state") != "none" or field_sheet.off() or len(field_sheet.OFFER) + 1 > room:
            return ""
        if field_sheet.offered(repo):
            return ""
        field_sheet.mark_offered(repo)
        return field_sheet.OFFER
    except Exception:  # noqa: BLE001 - the line is optional; the brief does not depend on it
        return ""


BUILDING = ("recortex: record brief not shown: building the local index in the background (first run); "
            "the brief appears at your next prompt. Run hooks.py --doctor for status.")
REBUILDING = ("recortex: record brief not shown: the local index was refused (often a key that differs between "
              "environments, such as a terminal and a hook with another XDG_STATE_HOME or HOME); it is moved aside and "
              "rebuilt in the background, and the brief appears at your next prompt. Run hooks.py --doctor for status.")
# Fixed sentences for the notice: the error messages themselves hold paths from the environment and stay in --doctor.
STORAGE_REASONS = {
    "symlink": "a storage folder or file is a symlink",
    "owner": "a storage folder or file belongs to another user",
    "permissions": "a storage folder or file has permissions for group or others",
    "parent": "the storage folder is in a folder that others can write to",
    "not-folder": "a storage path is not the kind of file expected",
    "key": "the plugin's key is empty or damaged",
    "changed": "the index changed while it was being opened",
    "name": "the repository name cannot be used as a folder name",
    "lock": "the sync lock could not be taken",
}


def open_usable(repo):
    """(index or None, refused). Raises storage.StorageError for unsafe storage."""
    try:
        db = index.connect(repo, create=False)
    except index.RefusedIndex:
        return None, True  # a sync moves it aside and builds a new one
    if db is not None and not index.get_meta(db, "synced_at"):
        db.close()
        return None, False
    return db, False


def start_background(repo, config):
    """Start the detached background sync unless one is running. Returns "started" or "running"."""
    import subprocess
    if index.running(repo):
        return "running"
    storage.folder(storage.cache_top(), storage.repo_folder(repo))
    budget = index.background_budget(config)
    if not index.take_lock(repo, budget):  # taken here, so a second hook sees it at once
        return "running"
    log = index.background_files(repo)["log"]
    index.log_line(repo, "starting background sync from a hook")
    descriptor = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        child = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.py"), "--repo", repo,
             "sync", "--background", "--lock-held", "--deadline", str(budget)],
            cwd=recortex.project_root(), stdin=subprocess.DEVNULL, stdout=descriptor, stderr=descriptor,
            start_new_session=True, close_fds=True)
    finally:
        os.close(descriptor)
    index.hand_lock(repo, child.pid, budget)
    return "started"


def produce(event, data, budget, config, repo, hub):
    session = str(data.get("session_id") or "")
    db, refused = open_usable(repo)
    fresh, failure = None, ""
    if event == "session-start":
        if db is None:
            building = index.running(repo)  # a file being written by a running build is not a refused index
            start_background(repo, config)
            remember(repo, session, None, brief=False)
            LAST["building"] = True
            return REBUILDING if refused and not building else BUILDING
        db.close()
        if index.running(repo):
            fresh = None  # a background sync is writing the index; use it as it is
        else:
            share = max(1.0, budget / 2.0)  # the sync gets half the budget; the rest is for the brief
            try:
                with alarm(share + max(1.0, budget / 4.0)):  # a safety net; the sync stops itself at `share`
                    summary = index.sync(repo, hub, timeout=share, deadline=share, timeline_cap=10, log=lambda *a: None)
                fresh = True
                if summary["complete"] and summary.get("board") == "budget":
                    start_background(repo, config)  # the board did not fit in the half budget; the background reads it
                if not summary["complete"]:
                    if summary.get("pages"):
                        fresh = "partial"  # part was read; the rest continues in the background
                        failure = "more to read than fits in a session start; continuing in the background"
                    else:
                        fresh = False  # GitHub did not answer in time; nothing was read
                        failure = "GitHub could not be read (timed out)"
                    start_background(repo, config)  # a large backlog catches up without blocking sessions
            except storage.StorageError:
                raise
            except index.SyncBusy:
                fresh = None  # another sync holds the lock; use the index as it is
            except (Exception, OutOfTime) as error:  # noqa: BLE001 - the cached index is used
                fresh = False
                slow = isinstance(error, OutOfTime) or "timed out" in str(error) or "deadline" in str(error)
                failure = "GitHub could not be read (%s)" % ("timed out" if slow else "gh failed"
                                                             if isinstance(error, recortex.GhError) else
                                                             type(error).__name__)
                if slow:
                    start_background(repo, config)
        db, refused = open_usable(repo)
        if db is None:
            building = index.running(repo)
            start_background(repo, config)
            remember(repo, session, None, brief=False)
            LAST["building"] = True
            return REBUILDING if refused and not building else BUILDING
    elif db is None:
        if event == "subagent-start" and index.running(repo):
            return json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": BUILDING}})
        if event == "prompt-submit":
            if not index.running(repo):
                start_background(repo, config)  # a build that was stopped or killed goes on; starting it is quick
            LAST["building"] = True
            return ""
        raise Unavailable("there is no local index yet; the next session start builds it")
    try:
        if failure:
            index.set_meta(db, "last_sync_error", "%s at %s" % (failure, index.now_utc()))
            db.commit()
        record = brief.Record(db, config, repo)
        LAST.update(synced_at=record.synced or "", fresh=fresh, steps=record.step_totals())
        if event == "session-start":
            # the line about the setup questions is made first, so that the brief's lists and resume block make room for it
            questions, held = setup_line(repo, config, record, session, brief.CAPS["session-start"] // 2)
            pointer, skipped = guidance.bounded_pointer(db, repo, record.allowed, pointer_seconds(budget, event),
                                                        "session-start")
            LAST["pointer_skipped"] = bool(skipped)
            orientation = (guidance.hint(repo), pointer, skipped)
            questions = "\n".join(filter(None, (*orientation, questions)))
            text = brief.session_payload(record, fresh, reserve=len(questions) + 1 if questions else 0)
            remember(repo, session, digest(record.key()), brief=True)
            text += "\n" + questions if questions else ""
            signal = new_signal(repo, config, brief.CAPS["session-start"] - len(text), held)
            text += "\n" + signal if signal else ""
            offer = field_offer(repo, record, brief.CAPS["session-start"] - len(text))
            return text + ("\n" + offer if offer else "")
        if event == "subagent-start":
            return json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                                      "additionalContext": brief.subagent_payload(record)}})
        if event == "pre-compact":
            return brief.precompact_payload(record)
        settings = config.get("prompt_hook") if isinstance(config.get("prompt_hook"), dict) else {}
        every = settings.get("every") if isinstance(settings.get("every"), int) and settings.get("every") > 0 else 10
        key = digest(record.key())
        state = load_state(repo)
        entry = state.get(session, {"count": 0, "key": None})
        entry["count"] = entry.get("count", 0) + 1
        entry["seen"] = time.time()
        if entry.get("brief") is not True:
            # The session started without a brief (first build, or no SessionStart): give the full brief once,
            # also when the one-line reminder is switched off. Build the text first, then record that it was given.
            pointer, skipped = guidance.bounded_pointer(db, repo, record.allowed, pointer_seconds(budget, event),
                                                        "prompt-submit")
            orientation = "\n".join(filter(None, [guidance.hint(repo), pointer, skipped]))
            text = brief.session_payload(record, None, reserve=len(orientation) + 1 if orientation else 0)
            if orientation:
                text += "\n" + orientation
            entry.update(brief=True, key=key)
            entry.setdefault("start", entry["seen"])
            told = dashboard_line(repo, entry, data, settings)
            if told:
                text += "\n" + told   # after the brief; it has a cap of its own
            state[session] = entry
            save_state(repo, state)
            return text
        # The task reminder has its own opt-in and needs no observer. It comes last, after what else the prompt carries, also when
        # the prompt hook is off and when nothing else is printed; it has a cap of its own (task_reminder.CAP).
        extra, problem = task_reminder_line(repo, config, db, record, data, entry, budget)
        if problem in ("unreadable", "failed"):   # one fixed line a session, the reason in --doctor
            import task_reminder
            extra = (task_reminder.UNREADABLE_LINE if problem == "unreadable" else task_reminder.FAILED_LINE) \
                if not entry.get("reminder_told") else ""
            entry["reminder_told"] = True
        elif problem:
            extra = guidance.SKIPPED if not entry.get("skip_told") else ""
            entry["skip_told"] = True
        if settings.get("enabled") is False:
            state[session] = entry
            save_state(repo, state)
            return extra
        show = entry.get("key") != key or entry["count"] % every == 0
        if show:
            entry["key"] = key
        nudge = ""
        line = brief.summary_line(record)
        last = entry.get("summary_nudge")
        if line and not (isinstance(last, (int, float)) and time.time() - last < brief.SUMMARY_EVERY) \
                and not observer_refreshes(repo, config):
            entry["summary_nudge"] = time.time()   # at most once in six hours for this session
            nudge = line
        parts = [part for part in (brief.prompt_payload(record) if show else "", nudge) if part]
        used = len("\n".join(parts)) + (1 if parts else 0)
        asked = setup_reminder(repo, config, record, entry, session, brief.CAPS["prompt-submit"] - used)
        recorded = dashboard_line(repo, entry, data, settings)   # its own cap, no room taken from the setup reminder
        state[session] = entry   # the reminder is marked in the entry before it is returned
        save_state(repo, state)
        return "\n".join(parts + ([recorded] if recorded else []) + ([asked] if asked else []) + ([extra] if extra else []))
    finally:
        db.close()


def dashboard_line(repo, entry, data, settings):
    """The one fixed line for what the user recorded in the dashboard since this session's last prompt (dashboard_notes.py), or
    "". The session's cursor is entry["dash"], else the time it started (entry["start"]); with neither, the cursor is set to now
    and nothing is said. Each note is told once to each session: the cursor then moves to the newest note told. Not for a
    subagent's prompt, and not when the prompt hook is off. A failure gives no line; nothing here raises."""
    try:
        if not isinstance(data, dict) or data.get("agent_id") or data.get("agent_type") or settings.get("enabled") is False:
            return ""
        import dashboard_notes
        cursor = entry.get("dash")
        if type(cursor) not in (int, float):
            cursor = entry.get("start")
        if type(cursor) not in (int, float):
            entry["dash"] = time.time()
            return ""
        notes = dashboard_notes.since(repo, cursor)
        if not notes:
            return ""
        entry["dash"] = max(note["at"] for note in notes)
        return dashboard_notes.line(notes)
    except Exception:  # noqa: BLE001 - a hook never fails over a line about the dashboard
        return ""


def observer_delivers(repo, config):
    """True when this folder's observer approval holds and its advisory mode is `deliver`: the observer's own hook then delivers
    the reminder about a changed decision, and the brief hook stays quiet so that nothing is said twice. Any doubt (no approval,
    settings that do not validate, an error) is False."""
    try:
        import observer_runtime as runtime
        effective = runtime.local_config(config)
        options = runtime.settings(effective)
        return bool(options["enabled"] and options["advisory_mode"] == "deliver" and runtime.approval_valid(repo, effective))
    except Exception:  # noqa: BLE001 - a hook never fails over a reminder
        return False


def task_reminder_line(repo, config, db, record, data, entry, budget):
    """(text, problem): the task reminder for this prompt of the main session, "" when there is none, and what kept it from
    being worked out: "" for nothing, "slow" (it ran out of time), "unreadable" (this machine's setting cannot be read) or
    "failed" (the check raised: a note is kept for --doctor). The caller says each problem once a session, in a fixed line.
    Nothing is done unless this machine chose it for this repository (task_reminder.read), the input holds no agent_id or
    agent_type (a subagent's prompt), and the observer does not deliver it itself. It reads only the prompt and the local index:
    no model call, no network. A failure never fails the hook."""
    try:
        if not isinstance(data, dict) or data.get("agent_id") or data.get("agent_type"):
            return "", ""
        import task_reminder
        found = task_reminder.read(repo)
        if found["state"] == "unreadable":   # the user may have chosen "on": say that it cannot be known
            return "", "" if observer_delivers(repo, config) else "unreadable"
        if found["state"] != "chosen" or found["mode"] != "on" or observer_delivers(repo, config):
            return "", ""
        return task_reminder.line(db, repo, record.allowed, data.get("prompt"), entry, pointer_seconds(budget, "prompt-submit"))
    except Exception:  # noqa: BLE001 - a hook never fails over a reminder
        try:
            import task_reminder
            task_reminder.note_failed(repo)
        except Exception:  # noqa: BLE001
            pass
        return "", "failed"


def pointer_seconds(budget, event):
    """The time the reminder about a changed decision may take before the hook goes on without it: 2 s at a session start
    and 1 s at a prompt, and never more than a fifth of the hook's budget (RECORTEX_HOOK_TIMEOUT)."""
    return max(0.2, min(2.0 if event == "session-start" else 1.0, budget / 5.0))


def observer_paused(repo, config):
    """True when this folder holds an observer approval from before 1.5.0 and the user has not yet chosen a level: the
    observer does nothing until they do (observer_runtime.paused). Any doubt is False."""
    try:
        import observer_runtime as runtime
        return bool(runtime.paused(repo, config))
    except Exception:  # noqa: BLE001 - a hook never fails over a status flag
        return False


def observer_refreshes(repo, config):
    """True when the observer is on for this folder and posts what it reviews (automatic mode): its worker then refreshes the
    status summary in the background, and the hook says nothing about it. Any doubt (no approval, a setting that does not
    validate, an error) is False: the session is told."""
    try:
        import observer_runtime as runtime
        effective = runtime.local_config(config)
        options = runtime.settings(effective)
        return bool(options["enabled"] and options["mode"] == "automatic" and runtime.approval_valid(repo, effective))
    except Exception:  # noqa: BLE001 - a hook never fails over a reminder
        return False


def remember(repo, session, key, brief):
    """Record in the session's state whether it got the full brief, and the fingerprint it saw."""
    try:
        state = load_state(repo)
        entry = state.get(session, {"count": 0})
        entry["brief"] = brief
        if brief:
            entry["key"] = key
        entry["seen"] = time.time()
        entry.setdefault("start", entry["seen"])   # when this session was first seen, for the reminder of the setup questions
        state[session] = entry
        save_state(repo, state)
    except (OSError, storage.StorageError):
        pass


def print_all():
    config = recortex.load_config()
    repo = recortex.resolve_repo(None, config)
    print(recortex.describe(config, repo))
    if repo.lower() not in recortex.trusted_repos():
        print("note: %s is not in your trusted repositories (%s), so the hooks print nothing for it; the payloads "
              "below show what they would print once it is trusted." % (repo, recortex.trusted_target()))
    try:
        db = index.connect(repo, create=False)
    except storage.StorageError as error:
        sys.exit("error: %s" % error)
    if db is None:
        sys.exit("error: no index for %s at %s; run index.py sync first" % (repo, index.index_path(repo)))
    total = 0
    for name, text in brief.all_payloads(db, config, repo).items():
        print("===== %s (%s): %d characters, cap %d =====" % (name, HOOK_NAMES[name], len(text), brief.CAPS[name]))
        print(text)
        total += len(text)
    print("===== total: %d characters =====" % total)
    return 0


def lock_lines(repo, active, problems):
    """For --doctor: a line for a sync lock that is there but not live, and one for a claim that is there. Only fixed
    wording, the paths and numbers from a lock that passed index.valid_lock are printed, never the lock's text."""
    lock = index.background_files(repo)["lock"]
    claim = lock + ".claim"
    if not active and os.path.lexists(lock):
        try:
            storage.check_path(lock, "file")
        except storage.StorageError as error:
            problems.append("sync lock refused: %s" % error)
            print("sync lock: refused (%s); every sync stops at it until it is fixed or removed" % error)
        else:
            data = index.valid_lock(index.read_lock(lock))
            if data:
                print("sync lock: %s is there but not live: pid %d on %s, started %s UTC, budget %d s; the next sync "
                      "removes it" % (lock, int(data["pid"]), "this host" if data["host"] == index.HOST else
                                      "another host", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(data["started"])),
                                      int(data["budget"])))
            else:
                print("sync lock: %s is there but is not a valid lock (its text is not shown); the next sync removes "
                      "it" % lock)
    if not os.path.lexists(claim):
        return
    if index.claim_not_a_file(claim):
        problems.append("the sync lock claim %s is not a file; a sync that finds a lock in its way stops with an "
                        "error until the claim is removed" % claim)
        print("sync lock claim: %s is not a file (a folder, for example); a sync that finds a lock in its way stops "
              "with an error until the claim is removed" % claim)
        return
    try:
        age = max(0, int(time.time() - os.lstat(claim).st_mtime))
    except OSError:
        return
    print("sync lock claim: %s is there, time stamp %d s ago; a process holds it while it judges the lock, and a "
          "claim left by a process that died is removed by the next sync that finds a lock in its way, after about "
          "two seconds" % (claim, age))


def field_line(config, repo):
    """For --doctor: one line on the field guidance. Never a problem, and never text of a sheet that is not approved:
    field names only when approved; places, codes, fingerprints and sizes otherwise."""
    info = field_sheet.state(config, repo)
    marks = ", ".join(field_sheet.sensitive_marks(config, repo)) or "none"
    kind = info["state"]
    if kind == "none":
        text = "none (no fields entry in the configuration file)"
    elif kind == "off":
        text = "off (\"field_guidance\": false in %s)" % recortex.settings_file()
    elif kind == "mark":
        return "field guidance: no sheet; sensitive marks: %s" % marks
    elif kind == "approved":
        size = len("\n".join(brief.field_frame(info, config)))
        return ("field guidance: shown to subagents; approved %s; fingerprint %s; frame %d of %d characters; fields: "
                "%s; sensitive marks: %s" % (info["approved"], info["fingerprint"][:12], size, field_sheet.FRAME_CAP,
                                             ", ".join(info["entry"]["names"]), marks))
    elif kind == "changed":
        text = ("not shown: the sheet changed since it was approved on %s (approved fingerprint %s, now %s). Review it "
                "with setup.py fields show" % (info["approved"], info["approved_fingerprint"][:12],
                                               info["fingerprint"][:12]))
    elif kind == "new":
        text = ("not shown: no approval for this repository in %s. Review it with setup.py fields show"
                % field_sheet.approvals_path())
    elif kind == "malformed":
        text = "not shown: the fields entry is refused: %s" % field_sheet.problem_text(info["problems"])
    else:
        text = "not shown: the approvals cannot be read: %s. The file's text is not shown" % info["reason"]
    return "field guidance: %s%s" % (text, "; sensitive marks: %s" % marks if marks != "none" else "")


def board_read_at(db):
    stamp = index.get_meta(db, "board_read_at")
    return stamp if stamp and index.ISO_UTC.fullmatch(stamp) else "at an unknown time"


def board_lines(db, config, repo):
    """For --doctor: what the sync stored about the board, then what GitHub says now. Fixed wording and numbers
    only: nothing from the board is printed. Nothing at all for a project without a board."""
    state = board_api.entry(config, repo)[0]
    if state == "none":
        return []
    lines = []
    code = index.get_meta(db, "board_error")
    if code in board_api.REASONS:
        lines.append("board: not read at the last sync: %s" % board_api.REASONS[code])
    cards = db.execute("SELECT COUNT(*) AS n FROM board_cards").fetchone()["n"]
    if cards:
        found = board_rules.plan(db, recortex.allowed_users(repo))
        lines.append("board cards: %d read %s; %d moved by hand, %d behind the record, %d to add, %d without an issue"
                     % (cards, board_read_at(db), len(found["moved"]),
                        len(found["behind"]), len(found["missing"]), len(found["gone"])))
    return lines + board_api.doctor_lines(config, repo)


def plan_doctor_lines(db, config, repo):
    """For --doctor: the plan's counts the sync kept, whether planning is on, and what is wrong with them. Fixed wording
    and integers only. Nothing at all for a project whose profiles have no plan."""
    record = brief.Record(db, config, repo)
    plan = recortex.plan_profile(record.profiles)
    if plan is None:
        return []
    off = not recortex.planning_on(config) or recortex.settings_off("planning")
    counts, bad = record.plan_counts()
    state = "off (\"planning\": false in the project file or in %s)" % recortex.settings_file() if off else "on"
    if bad:
        return ["plan: planning is %s; the counts the sync keeps are not in the form the plugin writes; run index.py "
                "sync" % state]
    if counts is None:
        return ["plan: planning is %s; no counts yet (the sync keeps them for a project that has a plan issue)" % state]
    entry = recortex.plan_issue(config, plan["profile"])
    if not counts["issue"]:
        return ["plan: planning is %s; %s" % (state, "the plan issue named in the project file is not usable (not in the "
                                               "index, no label plan, or opened by an account that is not allowed)"
                                               if entry is not None else "no plan issue yet")]
    return ["plan: planning is %s; issue #%d; %s" % (
        state, counts["issue"], "; ".join("%s %d" % (key, counts[key]) for key in index.PLAN_COUNT_KEYS[1:]))]


def task_doctor_lines(db, config, repo):
    """For --doctor: the task counts the sync kept, which lines of the brief they give, and the two reports. Fixed wording and
    integers only; never a word of a step or a title."""
    record = brief.Record(db, config, repo)
    counts, bad = record.task_counts()
    if bad:
        return ["tasks: the counts the sync keeps are not in the form the plugin writes, so the brief shows no line about "
                "tasks; run index.py sync"]
    if counts is None:
        return ["tasks: no counts yet (the sync keeps them), so the brief shows no line about tasks"]
    lines = []
    if counts["tasks"]:
        said = "tasks: %d open with steps, %d of %d steps done" % (counts["tasks"], counts["done"], counts["steps"])
        if counts["next_task"]:
            said += "; next is step %d of #%d" % (counts["next_step"], counts["next_task"])
        lines.append(said + "; the session brief and the status line show them, and the brief lists up to %d next steps "
                     "inside the data frame (the first lines to give way when it nears %d characters)"
                     % (brief.TASK_LINES, brief.CAPS["session-start"]))
    else:
        lines.append("tasks: no open task has steps, so the brief shows no line about them")
    if counts["hand"]:
        lines.append("tasks: %d list%s changed by hand on GitHub; the brief says so and asks the session to ask the user"
                     % (counts["hand"], " was" if counts["hand"] == 1 else "s were"))
    idle, stale = record.idle_tasks(), record.stale_bodies()
    lines.append("reports: %d task%s with no memory in %d days, %d topic bod%s older than %s newest result (the brief "
                 "names them at session start, after the task lines)" % (
                     len(idle), "" if len(idle) == 1 else "s", brief.IDLE_DAYS, len(stale),
                     "y" if len(stale) == 1 else "ies", "its" if len(stale) == 1 else "their"))
    return lines


def writing_doctor_lines(db, config, repo):
    """For --doctor: the writing mode of this machine and the state of the author's own paper, in fixed words. Never a
    path, a login or text from the record. Nothing for a project whose profiles have no plan."""
    record = brief.Record(db, config, repo)
    if recortex.plan_profile(record.profiles) is None:
        return []
    import writing
    lines = [writing.sentence(repo)]
    own = recortex.own_manuscript(config)
    lines.append("own paper: %s" % {
        "none": "no place is recorded", "no": "the author keeps none",
        "inside": "a place inside the project is recorded (the path is not shown here)",
        "outside": "a place outside the project is kept on this machine (the path is not shown here)"}[own["state"]])
    extra = record.own_paper_lines()
    if extra:
        lines.append("own paper: parts of the record's text changed since they were brought into it (the brief says "
                     "how many)")
    lines += versions_doctor_lines(repo)
    return lines


def versions_doctor_lines(repo):
    """For --doctor: the last refusal of a version, in the plugin's fixed words (the key and the time were kept by versions.py
    and are checked here). The hooks never contact the branch, so nothing here is
    about the branch itself: versions.py doctor asks GitHub."""
    import versions
    state = versions.read_state(repo)
    refusal = state.get("refusal") if isinstance(state.get("refusal"), dict) else None
    lines = []
    if refusal and refusal.get("kind") in versions.LINES and re.fullmatch(r"[0-9T:Z-]{1,20}", str(refusal.get("at", ""))):
        line = versions.LINES[refusal["kind"]]
        lines.append("versions: last refusal on %s: %s" % (refusal["at"][:10], line % repo if "%s" in line else line))
    return lines


def setup_doctor_lines(repo, config, record):
    """For --doctor: one line on the setup questions an update added (setup_questions.doctor_lines): ids and fixed words
    only. Never a problem."""
    try:
        import setup_questions
        return setup_questions.doctor_lines(repo, config, record)
    except Exception as error:  # noqa: BLE001 - --doctor says what it could not do
        return ["setup questions: could not be checked (an unexpected %s)" % type(error).__name__]


GH_STATES = {
    "ready": "signed in to github.com as %s",
    "signed-out": "not signed in to github.com; run `gh auth login` in a terminal",
    "slow": "no answer within %d seconds" % recortex.GH_CHECK_SECONDS,
    "unnamed": "GitHub did not name the signed-in account: the token may be invalid, or GitHub could not be reached "
               "(an answer that is not a GitHub login is not shown)",
}


def gh_line():
    """For --doctor: one line on gh, found or not, signed in to github.com as <login> or not. Never a problem: the brief
    can come from the index without gh. Of gh's output, only a login that fits recortex.LOGIN is printed."""
    import shutil
    where = (names.env("GH") or shutil.which("gh") or "").split()
    shown = where[0] if where else "not on PATH"
    state, login = recortex.gh_account()
    if state == "missing":
        return "gh: not found (%s); install it from https://cli.github.com" % shown
    return "gh: found (%s); %s" % (shown, GH_STATES[state] % login if state == "ready" else GH_STATES[state])


def observer_log_lines(repo, config):
    """For --doctor: one line when the observer's log of what it did could not take a post (observer_store.note_act_failure):
    fixed words, the count and the UTC day of the last failure, never text. Nothing when there is no failure, no
    observer store, or one that cannot be read."""
    try:
        import observer_store
        with observer_store.Store(repo, create=False, readonly=True, config=config) as store:
            failed = store.acts_failed()
    except Exception:  # noqa: BLE001 - --doctor says what it can
        return []
    if failed is None:
        return []
    return ["observer log: the log of what the observer did could not be written %d time(s), the last on %s (UTC); the "
            "comments it posted stand in GitHub. observer.py status shows the count." % (failed["n"], failed["day"])]


COULD_NOT_CHECK = "could not be checked"
SEVERAL_HOSTS = "a shared home folder needs file locks that all hosts see (NFS, or Lustre mounted with flock)"


def _row(label, work):
    """One line of the summary: `work()` gives its words, and a part that cannot be worked out says so."""
    try:
        return "  %s: %s" % (label, work())
    except Exception:  # noqa: BLE001 - the summary never raises, whatever it reads
        return "  %s: %s" % (label, COULD_NOT_CHECK)


def machine_summary(repo, config, problems):
    """What works on this machine, in plain words, for --doctor: the record brief, the task reminder, the line about the dashboard,
    taking notes, the answers given in a session, the usage of the plugin's subagents, the observer's approval and the hosts.
    Fixed words, and numbers and codes from fixed sets; a path appears only inside the command to run, which is the plugin's own
    script. Never raises: a part that cannot be worked out prints that it could not be checked."""
    import observer_runtime as runtime
    import observer_sources
    import task_reminder
    root = os.path.realpath(recortex.project_root())

    def capture():
        result = observer_sources.preflight(root)
        if result.get("ready"):
            return "ready", "", ""
        if result.get("code") == "source-missing":
            return "ready once a session has run here", "", ""
        issue = result.get("issues", [result])[0]
        run = ("Ask the trust question: %s" % observer_sources.trust_commands()[0] if result.get("fix") else
               "run python3 %s status --source-details and tell the user what it shows"
               % shlex.quote(os.path.join(os.path.dirname(os.path.abspath(__file__)), "observer.py")))
        return ("not ready: %s at %s. %s" % (result.get("code"), issue.get("boundary", "ancestor"), run),
                result.get("code"), "x")

    def reminder():
        found = task_reminder.read(repo)
        if found["state"] == "unreadable":
            return "cannot be read"
        if observer_delivers(repo, config):
            return "delivered by the observer"
        if found["state"] == "chosen" and found["mode"] == "on":
            at = task_reminder.failed(repo)
            if at is not None:
                return ("on, but the last check failed on %s and no line was added for that prompt; the line tells the session "
                        "once" % time.strftime("%Y-%m-%d", time.gmtime(at)))
            return "on"
        return "off (setup.py task-reminder set --mode on --yes turns it on)"

    def dashboard():
        import dashboard_notes
        hook = config.get("prompt_hook")
        if isinstance(hook, dict) and hook.get("enabled") is False:
            return "off with the prompt hook"
        at = dashboard_notes.failed(repo)
        if at is not None:
            return ("on, but a note of what you recorded in the dashboard could not be kept on %s, so a session was not told of "
                    "it" % time.strftime("%Y-%m-%d", time.gmtime(at)))
        return "on"

    def grants():
        words, code, blocked = capture()
        return ("cannot be read, for the same reason; ask the trust question first (acts.py ask trust)"
                if blocked else "can be read")

    def measured():
        note = usage.unmeasured(repo)
        if note is None:
            return "measured"
        return "not measured since %s (%s)" % (time.strftime("%Y-%m-%d", time.gmtime(note[0])), note[1])

    def approval():
        local = runtime.local_config(config)
        entry = runtime.stored_approval()
        if entry is None:
            return "none for this folder"
        reason = runtime.approval_reason(repo, local)
        if not reason:
            return "holds here (%s)" % runtime.BINDING_NOTES[runtime.stored_binding(entry)]
        return "does not hold: %s" % runtime.approval_note(reason, entry)

    lines = ["on this machine:",
             "  record brief (session start, prompts, subagents): %s" % (
                 "works" if not problems else "does not work: %d problem(s) below" % len(problems)),
             _row("task reminder", reminder), _row("line about the dashboard on prompts", dashboard),
             _row("observer notes (capture)", lambda: capture()[0]),
             _row("answers given in a session (grants)", grants),
             _row("usage of the plugin's subagents", measured), _row("observer approval", approval),
             "  several hosts: %s" % SEVERAL_HOSTS]
    try:
        import observer_lease
        note = observer_lease.mount_note(storage.state_top(for_key=True))
        if note:
            lines.append("  several hosts: %s" % note)
        folder = storage.folder(storage.state_top(), storage.repo_folder(repo.lower()), "observer", create=False, tighten=False)
        held = observer_lease.status(folder) if folder else ""
        if held:
            lines.append("  observer worker: %s" % held)
        try:
            import observer_store
            with observer_store.Store(repo, create=False, readonly=True, config=config) as store:
                mark = store.get_control("worker_lease")
        except Exception:  # noqa: BLE001 - no observer store yet, or one that cannot be read: nothing to add
            mark = ""
        if observer_lease.note_for(mark):   # the last pass could not make or read the lease
            lines.append("  observer worker: %s" % observer_lease.note_for(mark))
    except Exception:  # noqa: BLE001
        lines.append("  several hosts: %s" % COULD_NOT_CHECK)
    return lines


def doctor():
    """Print what the hooks depend on. Returns 1 when something keeps the brief from being shown."""
    problems = []
    try:
        config = recortex.load_config()
    except (ValueError, OSError) as error:
        print("config: unreadable (%s)" % error)
        return 1
    print("config: %s" % (config.get("_path") or "none found at the project root (%s)" % recortex.project_root()))
    repo, hub = config.get("repo"), config.get("hub")
    print("repository: %s   hub: %s" % (repo or "not set", hub if hub is not None else "not set"))
    if not config.get("_path"):
        print("result: the hooks stay silent in this project because it is not set up")
        return 1
    if not (recortex.valid_repo(repo) and isinstance(hub, int)):
        print("result: the hooks stay silent because \"repo\" or \"hub\" is missing or malformed")
        return 1
    trusted = repo.lower() in recortex.trusted_repos()
    allowed = sorted(recortex.allowed_users(repo))
    variable = names.ENV_PREFIX + "TRUSTED_REPOS"
    sources = recortex.trusted_sources(repo)
    print("trusted: %s (trusted lists read: %s; %s %s%s); allowed authors: %s"
          % ("yes" if trusted else "no", ", ".join(path + ("" if os.path.lexists(path) else " (missing)")
                                                   for path in recortex.trusted_files()),
             variable, "set" if names.env("TRUSTED_REPOS") else "not set",
             "; listed in %s" % ", ".join(sources) if sources else "", ", ".join(allowed) or "none"))
    if not trusted:
        problems.append("the repository is not trusted, so the hooks stay silent")
    print(gh_line())
    print("storage: index under %s, state and key under %s; hooks and terminal scripts resolve the same folders "
          "(CLAUDE_PLUGIN_DATA is not used)%s" % (storage.cache_top(), storage.state_top(for_key=True),
                                                 "; RECORTEX_TESTING and RECORTEX_DATA_HOME are set (for tests only): index and state under %s"
                                                 % storage.test_home() if storage.test_home() else ""))
    name = storage.repo_folder(repo)
    rows = [("index top folder", storage.cache_top(), "top"),
            ("index private folder", os.path.join(storage.cache_top(), storage.PRIVATE), "private"),
            ("index folder", index.index_dir(repo), "private"),
            ("index file", index.index_path(repo), "file"),
            ("state top folder", storage.state_top(), "top"),
            ("state folder", os.path.join(storage.state_top(), storage.PRIVATE, name), "private"),
            ("prompt state file", os.path.join(storage.state_top(), storage.PRIVATE, name, "prompt-state.json"), "file"),
            ("setup questions run note", os.path.join(storage.state_top(), storage.PRIVATE, name, names.SETUP_RUN), "file"),
            ("key top folder", storage.state_top(for_key=True), "top"),
            ("key file (contents not shown)", storage.key_path(), "file")]
    seen = set()
    for label, path, kind in rows:
        if (path, kind) in seen:
            continue
        seen.add((path, kind))
        exists, owner, mode, problem = storage.describe(path)
        note = problem
        if exists and not problem and mode:
            bits = int(mode, 8)
            if kind == "top" and bits & 0o077:
                note = "loose; tightened to 700 on next use" if owner else note
            elif kind in ("private", "file") and bits & 0o077:
                note = "permissions for group or others: refused"
                problems.append("%s %s is mode %s" % (label, path, mode))
        if problem and problem != "missing":
            problems.append("%s %s: %s" % (label, path, problem))
        print("  %-30s %s  %s" % (label, path, ("owner %s, mode %s%s" % (owner, mode, ", " + note if note else ""))
                                  if exists else "missing"))
    if observer_paused(repo, config):
        import observer_runtime
        print(observer_runtime.PAUSED)
    for line in observer_log_lines(repo, config):
        print(line)
    for line in guidance.skip_lines(repo):
        print(line)
    reason = usage.problem(repo)
    print("usage: %s" % ("usage.db can be written (the hooks' estimates and the plugin's subagents are counted there)"
                         if not reason else "usage.db cannot be written (%s); the usage view will miss what the hooks and "
                         "the plugin's subagents used" % reason))
    try:
        db = index.connect(repo, create=False, tighten=False)
    except storage.StorageError as error:
        db = None
        problems.append("index refused: %s" % error)
        print("index: refused (%s)" % error)
    else:
        if db is None:
            problems.append("no index yet; a session start or `index.py sync` makes it")
            print("index: none yet")
    if db is None:
        print(field_line(config, repo))  # after the storage rows, also when there is no index
        for line in setup_doctor_lines(repo, config, None):
            print(line)
    if db is not None:
        synced = index.get_meta(db, "synced_at")
        print("index: synced %s (%s)%s; %s" % (synced or "never", index.age_text(synced),
                                               "" if index.get_meta(db, "sync_complete") != "0" else ", incomplete",
                                               ", ".join("%s %d" % item for item in index.describe_index(db).items())))
        moved = index.get_meta(db, "moved_aside") if db is not None else ""
        if moved:
            print("an earlier index was refused and moved aside to %s" % moved)
        failure = index.get_meta(db, "last_sync_error")
        print("last sync failure: %s" % (failure or "none since the last good sync"))
        print("resume block: %s" % (index.get_meta(db, "resume_note") or "not read"))
        print(field_line(config, repo))
        for line in board_lines(db, config, repo):
            print(line)
        for line in plan_doctor_lines(db, config, repo):
            print(line)
        for line in task_doctor_lines(db, config, repo):
            print(line)
        for line in writing_doctor_lines(db, config, repo):
            print(line)
        for line in setup_doctor_lines(repo, config, brief.Record(db, config, repo)):
            print(line)
        if synced:
            for event, text in brief.all_payloads(db, config, repo).items():
                print("payload %-15s %5d characters (cap %d)" % (event, len(text), brief.CAPS[event]))
        else:
            problems.append("the index was never filled")
        db.close()
    try:
        active, last, error_line = index.background_status(repo)
    except storage.StorageError as error:
        active, last, error_line = None, None, "unreadable: %s" % error
    if active:
        print("background sync: running (pid %d, started %s UTC, budget %d s)"
              % (int(active["pid"]), time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(active["started"])),
                 int(active["budget"])))
    else:
        print("background sync: not running")
    lock_lines(repo, active, problems)
    if last:
        print("last background sync: finished %s, %s" % (index.clean(last.get("finished"), 40),
                                                          index.clean(last.get("result"), 120)))
    else:
        print("last background sync: none recorded")
    if error_line:
        print("last error line of the sync log: %s" % index.clean(error_line, 300))
    for line in machine_summary(repo, config, problems):
        print(line)
    for problem in problems:
        print("problem: %s" % problem)
    print("result: %s" % ("the hooks can show the brief" if not problems else "%d problem(s) above" % len(problems)))
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("event", nargs="?", choices=EVENTS + (STOP_EVENT,))
    parser.add_argument("--print", action="store_true", help="show every payload and its size")
    parser.add_argument("--doctor", action="store_true", help="say why the brief is or is not shown; changes nothing")
    args = parser.parse_args(argv)
    if args.doctor:
        return doctor()
    if args.print:
        return print_all()
    if not args.event:
        parser.error("name an event or pass --print")
    if args.event == STOP_EVENT:
        try:
            subagent_stop(read_input())
        except Exception:  # noqa: BLE001 - a hook never fails a session, and this one prints nothing
            pass
        return 0
    try:
        budget = int(names.env("HOOK_TIMEOUT", "10"))
    except ValueError:
        budget = 10
    data = read_input()
    if args.event == "session-start":
        refresh_statusline()
    try:
        text = run(args.event, data, budget)
        line = notice(LAST, data.get("source")) if args.event == "session-start" and LAST.get("state") else ""
        if line and LAST.get("pointer_skipped"):
            line += "\n" + guidance.SKIPPED   # fixed words, a line of its own beside the status line
    except Exception:  # noqa: BLE001 - hooks are silent on any failure
        return 0
    if line and data.get("source") in START_SOURCES and not notice_off():
        # one JSON object: the line for the user, and exactly the text a plain start prints for the model
        print(json.dumps({"systemMessage": line, "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                         "additionalContext": text}}))
    elif text:
        print(text)
    try:
        sys.stdout.flush()
        note_context(args.event, text)
    except Exception:  # noqa: BLE001 - an estimate for the usage view; never a failed hook
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

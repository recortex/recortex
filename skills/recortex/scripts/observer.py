#!/usr/bin/env python3
"""Explicit observer controls and bounded hook entry point. No automatic opt-in."""
import json
import os
import re
import signal
import sys
import time

import names
import observer_holds
import observer_runtime as runtime
import observer_store
import observer_sources
import recortex
import storage


def emit(value):
    print(json.dumps(value, sort_keys=True, ensure_ascii=True))


def parser():
    import argparse  # the modules only some commands need are imported where they are used: a hook starts this

    class Parser(argparse.ArgumentParser):
        """No abbreviated options: --ho would otherwise be read as --hosts, which the guard that keeps a session from choosing
        it by name would not see. The sub-commands are of this class too."""

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("allow_abbrev", False)
            super().__init__(*args, **kwargs)
    root = Parser(description=__doc__)
    root.add_argument("--repo")
    sub = root.add_subparsers(dest="command", required=True)
    # How a session's own call to each subcommand is kept in check (observer_hooks.COMMANDS holds the same table, and a test
    # holds the two together): safe, or grant: the command refuses without the one-time grant the hook records from the user's
    # recorded answer to that item's question (an item that waits, a setup question, or an act's question: acts.py), unless it
    # runs on the user's own terminal, which asks for a typed word where it always did.
    # status: safe, it only reads.
    status = sub.add_parser("status")
    status.add_argument("--source-details", action="store_true",
                        help="show native directory paths, modes and ownership for capture diagnosis")
    # advisory: grant. Raising it (off, shadow, deliver) from a session needs the user's recorded answer to the question of
    # acts.py (kind advisory); lowering it to off needs none. Muting or showing one reminder again is kind reminder.
    advisory = sub.add_parser("advisory", help="choose optional task-time evidence pointers on this machine")
    advisory.add_argument("mode", choices=("off", "shadow", "deliver", "list", "mute", "unmute"),
                          help="choose a delivery mode, list issued reminders, or edit a reminder preference")
    advisory.add_argument("id", nargs="?", help="reminder ID from advisory list, for mute or unmute")
    advisory.add_argument("--yes", action="store_true", help="save this choice (in a session, on the user's answer)")
    # map-mode: grant. Raising it (off, shadow, automatic) from a session needs the user's recorded answer to the map-upkeep
    # question (observer_grants, kind "map") or the user's terminal; switching it down or off needs neither.
    mode = sub.add_parser("map-mode", help="choose private Project Map shadow review or automatic maintenance")
    mode.add_argument("mode", choices=("off", "shadow", "automatic"))
    mode.add_argument("--yes", action="store_true", help="save this choice (in a session, on the user's answer)")
    # map-proposals: safe, read the reviewed proposals on this device.
    sub.add_parser("map-proposals", help="read reviewed Project Map changes and shadow outcomes on this device")
    # trust-ancestor: grant. It makes a folder above Claude Code's transcripts count as safe for as long as its owner,
    # group and mode stay as they are now, so it is the user's own decision: --yes needs the user's answer to the question of
    # acts.py (kind trust) in a session. --forget only does less, and needs none.
    trust = sub.add_parser("trust-ancestor", help="trust, exactly as it is now, a folder above Claude Code's transcripts "
                           "that belongs to another account or is written by a shared group (on the user's answer in a "
                           "session)")
    trust.add_argument("path", nargs="?", metavar="PATH", help="the folder (an absolute path)")
    trust.add_argument("--failing", action="store_true", help="every folder the capture check refuses now")
    trust.add_argument("--record", action="store_true", help="the folders above the project's local record, not the transcripts'")
    trust.add_argument("--list", action="store_true", help="what is trusted, and whether each still matches")
    trust.add_argument("--forget", metavar="PATH", help="remove one entry")
    trust.add_argument("--yes", action="store_true", help="trust them (without it nothing is changed)")
    # holds: safe, it only reads. runtime-check: safe, at most one small call per model, counted in the day's caps.
    for name in ("holds", "runtime-check"):
        sub.add_parser(name)
    # pending: safe, it only reads (--show-body prints a reviewed comment; --waiting lists what waits).
    pending = sub.add_parser("pending")
    pending.add_argument("--eventkey")
    pending.add_argument("--show-body", action="store_true", help="explicitly display the reviewed comment for this event key")
    pending.add_argument("--held", action="store_true",
                         help="list the reviewed memories that wait for the user's answer: post each, or drop it")
    pending.add_argument("--waiting", action="store_true",
                         help="everything that waits for the user's answer: held memories, tasks to open or close, "
                              "drafts of the paper's text, each with its question")
    pending.add_argument("--json", action="store_true", help="with --waiting: JSON instead of text")
    # enable: grant. Turning the observer on, raising its level, starting posting or adding the stronger model needs the
    # user's recorded answer to the question about it (observer_grants: the hook records a one-time grant for exactly the
    # level, or the stronger model, the user chose) or the user's terminal; lowering anything needs neither. --hosts any needs
    # the user's answer to the question of acts.py (kind hosts), and replacing the entry of another repository whose holds apply
    # the one of kind replace, each in the same call. It cannot be
    # run by a project file, and the user sees and removes it with status and disable.
    enable = sub.add_parser("enable", help="preview this machine's settings for this folder; --yes approves and saves them")
    enable.add_argument("--mode", choices=("shadow", "automatic"), default="shadow")
    enable.add_argument("--model", help="required: the Claude model alias or ID for the middle tier (drafts, reviews, summaries)")
    enable.add_argument("--level", choices=runtime.LEVELS,
                        help="what the observer may do on its own: notes, assistant or everything (required unless this "
                             "folder's approval already holds one)")
    enable.add_argument("--sort-model", help="the model that sorts each stretch of the conversation (default: the middle model)")
    enable.add_argument("--strong-model", help="the model that drafts the paper's text for the user to accept (default: none, "
                                               "the manuscript keeper stays off)")
    enable.add_argument("--hosts", choices=("shared", "any"),
                        help="where the approval holds: shared (the default: this host, and any host that sees the same key "
                             "file, as a shared home folder does) or any (any host that can read this state folder, also a "
                             "copy of it; in a session, on the user's answer)")
    enable.add_argument("--yes", action="store_true")
    enable.add_argument("--pass", dest="enable_pass", help=argparse.SUPPRESS)   # the word of setup.py's chain (observer_grants)
    # limits: safe, it only lowers the day's caps (a higher number is refused): a session can make the observer do less, never more.
    limits = sub.add_parser("limits", help="lower this folder's daily call caps (a new approval of the same settings); "
                                           "a higher number is refused")
    for tier in runtime.TIERS:
        limits.add_argument("--" + tier, type=int, metavar="N", help="calls a day on the %s tier" % tier)
    # disable: grant, it removes the folder's approval and ends its holds, so a session needs the user's answer to the question
    # of acts.py (kind disable), with or without active holds.
    sub.add_parser("disable", help="remove this machine's settings and approval for this folder")
    # pause: safe, it only stops capture or publication; resume: grant (kind resume), it ends a pause the user made.
    for name in ("pause", "resume"):
        command = sub.add_parser(name)
        command.add_argument("scope", choices=("capture", "publication", "all"), default="all", nargs="?")
    # run-worker, record, reconcile: safe, the worker's own steps (the hooks start it), under its lock, the caps and the
    # level; reconcile only reads GitHub to settle an event that was being sent.
    for name in ("run-worker", "record", "reconcile"):
        command = sub.add_parser(name)
        command.add_argument("--dry-run", action="store_true")
        if name == "record":
            command.add_argument("--source", required=True, help="retry a known deferred source through independent review")
        if name == "reconcile":
            command.add_argument("eventkey")
    # release: grant (kind release), it ends a hold the user made.
    release = sub.add_parser("release", help="release a hold (in a session, on the user's answer)")
    release.add_argument("hold_id")
    # act, later: grant, the user's recorded "Open it" or "Close it" (act) or "Not now" (later) to that item's question.
    for name, text in (("act", "carry out the user's yes to a task that waits: open it, or close the finished one"),
                       ("later", "put a task that waits off for a day (the user's Not now)")):
        command = sub.add_parser(name, help=text)
        command.add_argument("short", metavar="SHORT", help="the item's short id, as pending --waiting lists it")
    # log: safe, it only reads.
    log = sub.add_parser("log", help="what the observer did on its own, with the address of each act")
    log.add_argument("--days", type=int, default=7, help="how many days back (1 to 35, default 7)")
    # undo: grant (kind undo), it changes GitHub (reopens a task, restores a body, closes a task).
    undo = sub.add_parser("undo", help="reverse an act where it can be reversed (see observer.py log for the numbers); "
                                       "in a session, on the user's answer")
    undo.add_argument("act", type=int, metavar="ACT")
    # discard: grant, the user's recorded "Drop" or "Drop it" to that item's question (or the user's terminal).
    discard = sub.add_parser("discard", help="drop a reviewed event that will not be posted; its excerpts then expire")
    discard.add_argument("eventkey")
    # drafts: grant, apply needs the user's recorded "Approve", later and drop their "Not now" and "Drop it" (listing and
    # --show only read).
    drafts = sub.add_parser("drafts", help="the drafts of the paper's text that wait for the user's answer: list them, "
                                          "--show ID what would change, or apply, later, drop ID")
    drafts.add_argument("action", nargs="?", choices=("apply", "later", "drop"))
    drafts.add_argument("draft", nargs="?", metavar="ID")
    drafts.add_argument("--show", metavar="ID")
    # context: safe, it only reads. hook: safe, the hook's own entry point (no model or network call).
    context = sub.add_parser("context")
    context.add_argument("--query", default="")
    context.add_argument("--issue", type=int)
    hook = sub.add_parser("hook")
    hook.add_argument("event")
    return root


TRUST_NOTE = ("Each folder is accepted only while its owner, group and mode stay as shown; when one changes it is refused "
              "again. Its owner, and its group where the mode lets the group write, can put another folder in its place. "
              "Trusting them is the user's answer to a question: run acts.py ask trust and ask it.")
TRUST_WORLD = ("Anyone on this machine can replace what is below a folder that all may write and that has no sticky bit.")
TRUST_CHANGED = ("A folder you trusted changed its owner, group or mode and is refused again. Look at it, then run "
                 "acts.py ask trust and ask the question it prints.")
NAME = re.compile(r"[A-Za-z0-9._-]{1,32}")
REASONS = frozenset(("model_required", "level_required", "folder_name", "observer_settings", "observer_mode",
                     "observer_publisher", "observer_model", "observer_level", "observer_limit", "observer_approvals"))
NOTES = {
    "model_required": "Choose the model for the background calls with --model (a Claude alias or model ID).",
    "level_required": "Choose what the observer may do with --level notes, assistant or everything.",
    "folder_name": "The project folder's path holds a quote, a backslash or a control character; the observer cannot "
                   "be enabled there.",
    "observer_approvals": "This machine's private observer approvals file is too large, not JSON or not of the "
                          "expected form; it is not replaced on its own. Move it aside, then enable again.",
}


def _stored_settings(repo, root=None):
    """This folder's stored settings for `repo` as a dict, or {} (no entry, another repository, or one that cannot be
    read). Read, never validated here."""
    try:
        entry = runtime.stored_approval(root)
    except (ValueError, storage.StorageError, OSError):
        return {}
    if not isinstance(entry, dict) or entry.get("repo") != repo.lower() or not isinstance(entry.get("settings"), dict):
        return {}
    return entry["settings"]


def _tier_gates_valid(repo, config, options):
    """True when every tier that has a model holds a valid proof for it, read from this folder's queue."""
    try:
        with observer_store.Store(repo, create=False, readonly=True, config=config) as store:
            state = runtime.tier_state(options, store)
    except observer_store.StoreError as error:
        if error.reason != "missing":
            raise
        state = runtime.tier_state(options, None)
    return all(item["gate"] for item in state.values() if item["configured"])


def enable(repo, config, mode, yes=False, model=None, root=None, confirm=None, given="terminal", level=None,
           sort_model=None, strong_model=None, hosts=None):
    """Preview, or with yes=True record, this machine's settings for this project folder. Nothing is written to the
    project's shared configuration file, and nothing is created before every check has passed. `confirm`, when given,
    is asked last, before anything is written; a code it returns stops the write. The level is the user's choice (one of
    runtime.LEVELS); this folder's earlier choice stands when none is given, and without either nothing is enabled. The
    sort model defaults to the middle model and the strong model to none (the manuscript keeper stays off). Caps the
    user lowered with `limits` stay lowered when the same repository is enabled again. `hosts` is "shared", "any" (the
    user's own terminal only: the caller checks) or None, which keeps an "any" this folder's entry already holds."""
    root = runtime.folder_key(root)
    problem = runtime.folder_problem(root)
    if problem:
        raise ValueError(problem)
    if not model:
        raise ValueError("model_required")
    stored = _stored_settings(repo, root)
    level = level or (stored.get("level") if stored.get("level") in runtime.LEVELS else "")
    if level not in runtime.LEVELS:
        raise ValueError("level_required")
    raw = {"enabled": True, "mode": mode, "model": model, "level": level}
    raw["advisory_mode"] = stored.get("advisory_mode", "off")
    raw["map_mode"] = stored.get("map_mode", "off") if level in ("assistant", "everything") else "off"
    if sort_model:
        raw["sort_model"] = sort_model
    if strong_model:
        raw["strong_model"] = strong_model
    caps = set(runtime.CALL_CAPS.values()) | set(runtime.TOKEN_BUDGETS.values())
    if stored.get("level") in runtime.LEVELS:   # an entry of 1.4.0 holds that version's defaults, never the user's choice
        raw.update({key: stored[key] for key in caps if key in stored})
    options = runtime.settings({"observer": raw})
    try:
        identity = runtime.publisher_id(create=False)
    except (storage.StorageError, OSError):
        identity = None
    options["publisher_id"] = identity or "allocated_on_explicit_approval"
    kept = hosts is None and runtime.keeps_any(repo, root)   # as approve keeps it: the mark must verify
    preview = {"action": "enable", "repo": repo, "folder": root, "settings": options, "approved": False,
               "binding": runtime.BINDING_NOTES["any" if hosts == "any" or kept else "shared"],
               "level": level, "level_label": runtime.LEVEL_LABELS[level], "usage": runtime.usage_note(options),
               "note": "Settings stay on this machine for this folder; the project file can only switch the observer "
                       "off. An isolated runtime check must succeed for each model before independent checks or "
                       "automatic posting."}
    billing = runtime.billing_variables()
    if billing:
        preview["billing"] = runtime.BILLING_NOTE % ", ".join(billing)
    if runtime.project_off(config):
        preview["project_switch"] = "off"
    if not yes:
        return preview
    if runtime.project_off(config):
        return dict(preview, code="project-off")
    if mode == "automatic" and not _tier_gates_valid(repo, config, options):
        return dict(preview, code="runtime-gate")
    refused = confirm() if confirm is not None else None
    if refused:
        return dict(preview, code=refused)
    options["publisher_id"] = runtime.publisher_id(create=True)
    with observer_store.Store(repo, config=dict(config, observer=options), max_bytes=options["max_pending_bytes"],
                              max_pending=options["max_pending_observations"]):
        pass  # the queue, so that status and the hooks find it
    runtime.approve(repo, {"observer": options}, root, given=given, hosts=hosts)
    return dict(preview, settings=options, approved=True)


def limits(repo, config, wanted, given="terminal"):
    """Lower this folder's daily call caps: a new approval of the same settings with the lower numbers. `wanted` maps a
    tier to a number. A number above the cap in force is refused (code "limit-higher"), and a number below 1 is
    refused too; nothing else changes, and the approval keeps its own time and where it was given."""
    if not wanted:
        return {"code": "nothing-to-change", "caps": {tier: runtime.settings(config)[key] for tier, key in
                                                       runtime.CALL_CAPS.items()}}
    options = runtime.settings(config)
    if not runtime.approval_valid(repo, config):
        return {"code": "approval", "note": "Run observer.py enable first; the limits change an approved folder."}
    changed = dict(options)
    for tier, number in wanted.items():
        key = runtime.CALL_CAPS[tier]
        if type(number) is not int or number < 1:
            return {"code": "limit-invalid", "tier": tier}
        if number > options[key]:
            return {"code": "limit-higher", "tier": tier, "cap": options[key],
                    "note": "A cap can be lowered here and never raised."}
        changed[key] = number
    entry = runtime.stored_approval() or {}
    kept = entry.get("given") if entry.get("given") in ("terminal", "session") else given
    runtime.approve(repo, {"observer": changed}, given=kept, approved_at=entry.get("approved_at"))
    return {"action": "limits", "caps": {tier: changed[key] for tier, key in runtime.CALL_CAPS.items()}, "approved": True}


def advisory(repo, config, mode, yes=False):
    """Preview or save a local advisory choice; never enables capture or publication."""
    import observer_advisory
    if mode not in observer_advisory.MODES:
        raise ValueError("observer_advisory_mode")
    result = {"action": "advisory", "mode": mode, "approved": False,
              "note": "Off by default. Shadow keeps hidden local receipts; deliver adds a nonblocking evidence pointer "
                      "to a matching user prompt. No model call or project record change."}
    if not runtime.approval_valid(repo, config):
        return dict(result, code="approval")
    if not yes:
        return result
    options = dict(runtime.settings(config), advisory_mode=mode)
    entry = runtime.stored_approval() or {}
    runtime.approve(repo, {"observer": options}, given=entry.get("given", "terminal"),
                    approved_at=entry.get("approved_at"))
    return dict(result, approved=True)


ADVISORY_RANK = {"off": 0, "shadow": 1, "deliver": 2}
HELD_NOTE = ("Ask the user about each with the multiple-choice question tool, one question per memory, from the "
             "template: Memory SHORT (KIND) on #ISSUE: \"LINE\". It would change: CHANGES. Post it, or drop it? Give "
             "the short id and the line exactly as listed here, and the options Post and Drop. For each Post, run "
             "post_verified.py comment --issue N --kind KIND --body-file <its body, from pending --eventkey KEY "
             "--show-body> --eventkey KEY right after; for each Drop, observer.py discard KEY.")

class Terminal:
    """The controlling terminal, read and written directly, never through stdin or stdout."""

    def __init__(self, descriptor):
        self.descriptor = descriptor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.descriptor)

    def ask(self, question):
        os.write(self.descriptor, question.encode("utf-8"))
        answer = b""
        while not answer.endswith(b"\n") and len(answer) < 256:
            chunk = os.read(self.descriptor, 256)
            if not chunk:
                break
            answer += chunk
        return answer.decode("utf-8", "replace").strip()


def terminal():
    """The user's own terminal, opened for the confirmation, or None. A command run by Claude Code's Bash tool has no
    controlling terminal and pipes for stdin and stdout, so a model cannot release a hold."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        return Terminal(os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0)))
    except OSError:
        return None


CLAUDE_NAMES = ("claude",)   # what a Claude Code process is called (its name, or the first word of its command line)
ANCESTORS = 64              # how far up the parents the check looks


def ancestry(limit=ANCESTORS):
    """The names of this process's parents, nearest first, at most `limit`: from /proc on Linux, from `ps` elsewhere. Each is
    the process's own name and the first word of its command line, so that a renamed or relinked program shows either way.
    Whatever cannot be read ends the walk."""
    names, pid = [], os.getppid()
    for _ in range(limit):
        if pid <= 1:
            break
        try:
            with open("/proc/%d/stat" % pid) as handle:
                stat = handle.read()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as handle:
                    first = handle.read(512).split(b"\0")[0].decode("utf-8", "replace")
            except OSError:
                first = ""
        except (OSError, ValueError, IndexError):
            import subprocess
            try:
                done = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
                fields = done.stdout.split(None, 1)
                parent, comm, first = int(fields[0]), fields[1].strip(), fields[1].strip()
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                break
        names += [os.path.basename(comm), os.path.basename(first)]
        pid = parent
    return names


def inside_claude(environ=None, parents=None):
    """True when this command runs inside a Claude Code session: the environment carries CLAUDECODE, or a parent process is
    the claude program. The user's own terminal is neither; a `!` command typed inside Claude Code is, and is refused too.
    `parents` is the ancestry (ancestry() by default), for a test."""
    if (os.environ if environ is None else environ).get("CLAUDECODE"):
        return True
    return any(name in CLAUDE_NAMES for name in (ancestry() if parents is None else parents))


def on_terminal(parents=None):
    """True when this command runs on the user's own terminal: a controlling terminal and a tty for stdin and stdout, which a
    command run by Claude Code's Bash tool does not have, and outside any Claude Code session, whatever terminal one has
    made for it (inside_claude). A guard against mistakes: a session with shell access can find another way."""
    if inside_claude(parents=parents):
        return False
    tty = terminal()
    if tty is None:
        return False
    with tty:
        return True


def declined(store, ident, answer):
    """None when the user may decline item `ident` ("later" or "no") now: they run the command on their own terminal, or
    their recorded answer to that item's question left the one-time grant, which is consumed here. Else the refusal that
    is emitted. A session that was not answered, or answered another question, cannot postpone or drop what waits."""
    import observer_waiting
    item = observer_waiting.find_any(store, ident)
    if item is None:
        return {"code": "not_waiting"}
    if answer not in observer_waiting.ANSWERS[item["kind"]]:
        return {"code": "other_kind", "kind": item["kind"]}
    if on_terminal() or store.take_grant(observer_waiting.answer_key(item, answer)):
        return None
    import observer_grants
    return observer_grants.refused({"code": "answer_unanswered"}, note=observer_waiting.UNANSWERED)


def entry_holds(keep=None):
    """The active holds that replacing this folder's entry would stop applying here: those of the repository the
    entry names, [] without an entry or when it names `keep` (the replacement names it too, so they still apply), and
    [None] when the approvals or the holds cannot be read, since those holds may be active."""
    try:
        entry = runtime.stored_approval()
    except (ValueError, storage.StorageError, OSError):
        return [None]
    if entry is None:
        return []
    name = entry.get("repo") if isinstance(entry, dict) else None
    if not recortex.valid_repo(name):
        return [None]
    if keep and name.lower() == keep.lower():
        return []
    try:
        return [hold for hold in observer_store.read_holds(name) if hold.get("state") == "active"]
    except (observer_store.StoreError, storage.StorageError, OSError, ValueError, KeyError, TypeError):
        return [None]


def confirmed(word, question):
    """None when the user typed `word` on their own terminal; else the code that stops the action. A command run by
    Claude Code's Bash tool has no terminal, so it is refused."""
    tty = terminal()
    if tty is None:
        return "terminal-required"
    with tty:
        return None if tty.ask(question + " Type %s to confirm: " % word) == word else "not-confirmed"


def spend_unused(*kinds):
    """The act was done on the user's own terminal, which needs no answer: a grant of that kind that nobody used is spent with
    its answer (observer_grants.spend), so that the answer cannot be replayed later."""
    import observer_grants
    for kind in kinds:
        observer_grants.spend(runtime.folder_key(), kind)


def settle(kind, word, question, values):
    """None when the act may go on; else the refusal to emit. On the user's own terminal the user types `word` to confirm
    `question`; anywhere else the act needs the one-time grant for exactly `values` (acts.allowed), which the hook records from
    the user's answer to the question of acts.py, and the refusal names that question. Called after every other check has
    passed and right before the first change, so that a refusal for another reason spends nothing. The branch is chosen by
    on_terminal() (outside every Claude Code session), never by whether /dev/tty can be opened: a session can give a command a
    pseudo-terminal of its own, and then type the word itself."""
    tty = terminal() if on_terminal() else None
    if tty is not None:
        with tty:
            if tty.ask(question + " Type %s to confirm: " % word) != word:
                return {"code": "not-confirmed"}
        spend_unused(kind)
        return None
    import acts
    return None if acts.allowed(kind, values) else acts.refusal(kind, values)


def release(store, hold_id, tty=None):
    """Release one active hold: after the user typed the confirmation word on their terminal (`tty`), or, anywhere else, after
    the user's answer to the question of acts.py (kind release)."""
    hold = next((item for item in store.holds() if item.get("id") == hold_id and item.get("state") == "active"), None)
    if hold is None:
        return {"code": "hold-unavailable"}
    if tty is not None:
        question = "Release hold %s (%s%s) of %s? Type release to confirm: " % (
            hold_id, hold.get("action"), " " + json.dumps(hold["command"]) if hold.get("command") else "", store.repo)
        if tty.ask(question) != "release":
            return {"code": "not-confirmed", "hold_id": hold_id}
        spend_unused("release")
    else:
        import acts
        values = {"repo": store.repo, "hold": hold_id, "action": hold.get("action")}
        if hold.get("action") not in acts.HOLD_WORDS or not acts.allowed("release", values):
            return dict(acts.refusal("release", values), hold_id=hold_id)
    store.update_holds(lambda holds: observer_holds.release(holds, hold_id))
    # The moments the hold kept off the record go back to the sort (no other hold covering them): a whole number.
    return {"action": "release", "hold_id": hold_id, "released": True, "offered_again": store.offered_again}


def _alarm(signum, frame):
    raise TimeoutError("deadline")


TOOL_EVENTS = ("pre-tool",)


def hook(event, config=None, repo=None):
    """One hook event. Without `config`, the project file and this machine's settings for this folder are read. A
    failure prints one fixed line, except on tool events, which fire on every call and stay silent."""
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, 3.5)
    trusted = unreadable = False
    holds = event == "pre-tool"  # the holds follow this folder's private entry, whatever the project file says
    data = None
    try:
        if config is None:
            try:
                loaded = recortex.load_config()
            except (OSError, ValueError, RecursionError):
                if not holds:
                    raise
                loaded = {}
            repo = repo or loaded.get("repo")
            if not recortex.valid_repo(repo) and not holds:
                return 0
            trusted = recortex.valid_repo(repo) and bool(recortex.allowed_users(repo))
            try:
                config = runtime.local_config(loaded)
            except (ValueError, storage.StorageError):
                unreadable = True  # this machine's approvals exist but cannot be read
                if not holds:
                    raise
                config = {key: value for key, value in loaded.items() if key != "observer"}
        repo = repo or config.get("repo")
        if not recortex.valid_repo(repo) and not holds:
            return 0
        raw = sys.stdin.read(8388609)
        if len(raw) > 8388608:
            raise ValueError("hook_input_limit")
        if len(raw) > 262144 and event != "pre-tool":
            return 0  # a large Write, Edit or tool result; the next hook scans its transcript lines
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("hook_input")
        import observer_hooks
        result = observer_hooks.handle(event, data, config, repo)
        if result:
            emit(result)
        return 0
    except (TimeoutError, OSError, ValueError, observer_store.StoreError, storage.StorageError) as error:
        if holds and isinstance(error, TimeoutError):
            # Out of time before the holds were decided: stopped as when they cannot be read, never let through.
            import observer_hooks
            result = observer_hooks.fail_closed(data)
            if result:
                emit(result)
            return 0
        raw_settings = (config or {}).get("observer", {})
        enabled = unreadable or isinstance(raw_settings, dict) and raw_settings.get("enabled") is True
        if (event not in TOOL_EVENTS and enabled and recortex.valid_repo(repo)
                and (trusted or recortex.allowed_users(repo))):
            message = "recortex observer unavailable: hook deadline or local state failure; run observer.py status."
            emit({"systemMessage": message})
        return 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def tiers_report(options, store):
    """Per tier: its model, whether it holds a valid proof for it, the day's calls and tokens, and the caps."""
    state = runtime.tier_state(options, store)
    today = store.usage_today() if store is not None and store.format == observer_store.FORMAT else {}
    out = {}
    for tier in runtime.TIERS:
        used = today.get(tier, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        out[tier] = dict(state[tier], calls_24h=used["calls"], tokens_24h=used["input_tokens"] + used["output_tokens"],
                         call_cap=options[runtime.CALL_CAPS[tier]], token_budget=options[runtime.TOKEN_BUDGETS[tier]])
    return out


def _status(repo, config, loaded, store, options, details):
    import observer_advisory
    variables = runtime.billing_variables()
    last = store.get_control("runtime_last") if store else None
    seen = store.get_control("call_environment", []) if store else []
    called = sorted(set(variables) | {name for name in seen if name in runtime.BILLING})
    reason = runtime.approval_reason(repo, config)
    out = {"settings": options, "approved": reason == "", "approval_reason": reason, "folder": runtime.folder_key(),
           "level": options["level"], "level_label": runtime.LEVEL_LABELS.get(options["level"], "not chosen"),
           "paused": runtime.paused(repo, loaded), "usage": runtime.usage_note(options),
           "capture_preflight": observer_sources.preflight(recortex.project_root(), details=details)}
    try:
        entry = runtime.stored_approval()
    except (ValueError, storage.StorageError, OSError):
        entry = None
    if reason:
        out["approval_note"] = runtime.approval_note(reason, entry)
    out["binding"] = ({"applies": runtime.binding(entry) or "none", "stored": runtime.stored_binding(entry),
                       "note": runtime.BINDING_NOTES[runtime.stored_binding(entry)]} if isinstance(entry, dict)
                      else {"applies": "none", "stored": "none", "note": ""})
    capture = out["capture_preflight"]
    if capture.get("fix") == "trust-ancestor":
        out["capture_fix"] = {"commands": observer_sources.trust_commands(os.path.dirname(os.path.abspath(__file__))),
                              "note": observer_sources.TRUST_RUN}
    if capture.get("trust") == "changed":
        out["capture_note"] = TRUST_CHANGED
    if out["paused"]:
        out["note"] = runtime.PAUSED + " Answer the level question (setup.py questions), or run observer.py enable --level."
    if called:
        out["billing"] = runtime.BILLING_NOTE % ", ".join(called)
    if runtime.project_off(loaded):
        out["project_switch"] = "off"
    if store is None:
        return dict(out, code="empty", action="status", dry_run=False, queue={}, tiers=tiers_report(options, None))
    import observer_lease
    held = observer_lease.status(store.folder)
    if held:
        out["host_lease"] = held   # another host's worker holds this folder: one fixed line, without the host's name
    if observer_lease.note_for(store.get_control("worker_lease")):
        out["host_lease_note"] = observer_lease.note_for(store.get_control("worker_lease"))
    failed = store.acts_failed()   # acts the log could not take (their posts stand): a count and the UTC day of the last
    return dict(out, runtime_ready=runtime.tier_state(options, store)["middle"]["gate"],
                tiers=tiers_report(options, store), acts_failed=failed["n"] if failed else 0,
                acts_failed_day=failed["day"] if failed else "",
                runtime_last=last, capture_paused=store.get_control("capture_paused", False),
                publication_paused=store.get_control("publication_paused", False),
                worker_degraded=store.get_control("worker_degraded", ""), queue=store.status(),
                worker_last=store.get_control("worker_last"), dialogs_unread=observer_sources.unread(store),
                jobs_last=store.get_control("jobs_last"), advisory=observer_advisory.status(store))


def runtime_check(repo, options, store):
    """One smoke call per distinct model of the tiers that have one, each on a reservation of its own tier. A proof is
    kept for every tier of its model; a failed check clears only those tiers' proofs, so the other tiers go on.
    Returns (ok, {tier: safe summary})."""
    done, out, ok = {}, {}, True
    for tier in ("middle", "sort", "strong"):
        model = runtime.model_for(options, tier)
        if tier == "strong" and not model:
            continue   # no strong model: the keeper is off, and there is nothing to prove
        if model not in done:
            reservation = store.reserve_invocation("runtime_check", options[runtime.CALL_CAPS[tier]],
                                                   options["window_hours"] * 3600, tier=tier,
                                                   tokens=options[runtime.TOKEN_BUDGETS[tier]])
            if reservation is None:
                done[model] = None
                out[tier] = {"code": "budget"}
                ok = False
                continue
            proof = runtime.check_runtime(options, tier)
            valid = runtime.gate_valid(proof, options, tier)
            store.complete_invocation(reservation, valid, runtime.candidate_hash(proof), usage=proof.get("usage"),
                                      model=proof.get("actual_model"))
            done[model] = (proof, valid)
        elif done[model] is None:
            out[tier] = {"code": "budget"}
            ok = False
            continue
        proof, valid = done[model]
        store.set_control(runtime.gate_control(tier, options), proof if valid else None)
        out[tier] = {key: proof.get(key) for key in ("ok", "code", "version", "elapsed", "usage", "isolation_checked",
                                                      "canary_absent", "structured_exact", "requested_model",
                                                      "actual_model", "api_key_source", "billing_variables")}
        out[tier]["model"] = model
        ok = ok and valid
    return ok, out


def _acts(args, store, repo, config, options):
    """act, later, log and undo: what the observer waits to be told, and what it did. The "yes" of act and the writes of
    undo need an approved folder in automatic mode; the grant an act needs is the user's recorded answer (the hook
    records it, observer_hooks)."""
    import observer_jobs
    import observer_waiting
    if args.command == "log":
        for text in observer_jobs.log_lines(store, repo, config, max(1, min(35, args.days))) or ["No act in this period."]:
            print(text)
        return 0
    if args.command == "later":
        result = observer_waiting.later(store, args.short, time.time(), answered=on_terminal())
        emit(result)
        return int(result["code"] != "later")
    if args.command == "undo":
        # It changes GitHub (reopens a task, restores a body, closes a task): on the user's own terminal after a typed word,
        # anywhere else on the user's answer to the question of acts.py. An act that cannot be reversed (none with that number,
        # one undone before, one corrected another way) changes nothing and needs no answer.
        import acts
        row = store.act(args.act)
        values = acts.undo_values(repo, row)
        if values is not None:
            refused = settle("undo", "undo", "Undo act %d of %s?" % (args.act, repo), values)
            if refused:
                emit(refused)
                return 1
        result = observer_jobs.undo(store, repo, config, args.act) if values is not None or row is None or row["undone"] \
            or row["kind"] in observer_jobs.CORRECTED else {"code": "not_reversible"}
        note = result.get("note") or observer_jobs.undo_note(result["code"])
        emit(dict(result, note=note) if note else result)
        return int(result["code"] not in ("restored", "reopened", "closed_not_planned"))
    if not runtime.approval_valid(repo, config) or options["mode"] != "automatic":
        emit({"code": "approval", "note": "Only an approved folder in automatic mode acts."}); return 1
    import tasks
    close = lambda issue: observer_jobs.quiet(tasks.close_task, repo, config, issue)[0]   # its words are not the JSON
    result = observer_waiting.act(store, repo, config, args.short, close)
    emit(result)
    return int(result["code"] not in ("opened", "closed", "map_applied"))


def _account(table, number, field):
    """The name of an owner or a group from the account database, only when it is plain; else "" (never echoed)."""
    try:
        name = getattr(table(number), field)
    except (KeyError, OverflowError, OSError, AttributeError):
        return ""
    return name if isinstance(name, str) and NAME.fullmatch(name) else ""


def _trust_rows(rows):
    try:
        import grp
        import pwd
    except ImportError:
        grp = pwd = None
    return [{"path": row["path"], "uid": row["uid"], "owner": _account(pwd.getpwuid, row["uid"], "pw_name") if pwd else "",
             "gid": row["gid"], "group": _account(grp.getgrgid, row["gid"], "gr_name") if grp else "",
             "mode": row["mode"], "code": row["code"], "world": row["world"],
             "trustable": row["trustable"]} for row in rows]


def trust_ancestor(args):
    """observer.py trust-ancestor: one of PATH, --failing, --list or --forget PATH. Without --yes it shows and changes
    nothing; --yes needs the user's answer to the question of acts.py (kind trust), or their own terminal; --forget only
    does less and needs neither."""
    root = recortex.project_root()
    chosen = [bool(args.path), args.failing, args.list, bool(args.forget)]
    if sum(chosen) != 1 or (args.yes and (args.list or args.forget)) or (args.list and args.forget):
        emit({"code": "invalid-arguments", "note": "Give exactly one of PATH, --failing, --list or --forget PATH."})
        return 1
    scope = "record" if args.record else "transcripts"
    if args.list:
        state, rows = observer_sources.trusted_listing(scope)
        emit({"action": "trust-ancestor", "state": state,
              "entries": [dict(row, owner=_account(_pwd().getpwuid, row["uid"], "pw_name") if _pwd() else "",
                               group=_account(_grp().getgrgid, row["gid"], "gr_name") if _grp() else "")
                          for row in rows]})
        return 0
    if args.forget:
        try:
            gone = observer_sources.trust_forget(args.forget, scope)
        except observer_sources.TrustError as error:
            emit({"code": str(error)}); return 1
        emit({"action": "trust-ancestor", "forgotten": gone})
        return 0 if gone else 1
    import acts
    found = acts.trust_rows(root, None if args.failing else args.path, args.record)
    if found is None:
        emit({"code": "not-an-ancestor"}); return 1
    failing, chosen_rows = found
    rows = failing if args.failing else [row for row in failing if row["path"] == args.path and row["trustable"]]
    if not chosen_rows:
        emit({"code": "nothing-to-trust", "entries": _trust_rows(rows)}); return 1
    note = TRUST_NOTE + (" " + TRUST_WORLD if any(row["world"] for row in chosen_rows) else "")
    stored = False
    if args.yes:
        values = acts.ACTS["trust"]._values(chosen_rows)
        if values is not None and args.record:
            values["record"] = True
        if values is None:   # a path with characters the question does not print: no answer can be for it
            emit({"code": "trust_unanswerable", "note": "A folder path holds characters recortex does not print."}); return 1
        if not acts.allowed("trust", values):   # on the user's own terminal it needs none, and spends one
            emit(acts.refusal("trust", values, root)); return 1
        try:
            observer_sources.trust_add(chosen_rows, root, acts.record_folder(root) if args.record else None, scope)
        except observer_sources.TrustError as error:
            emit({"code": str(error)}); return 1
        stored = True
        note = "Trusted as shown." if args.record else "Trusted as shown. Run observer.py status to see whether notes can be taken now."
    emit({"action": "trust-ancestor", "entries": _trust_rows(rows), "approved": stored, "note": note,
          "capture_preflight": None if args.record else observer_sources.preflight(root)})
    return 0


def _pwd():
    try:
        import pwd
        return pwd
    except ImportError:
        return None


def _grp():
    try:
        import grp
        return grp
    except ImportError:
        return None


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "hook":
        return hook(args.event, repo=args.repo)
    import index
    import observer_context
    import observer_publisher
    try:
        loaded = recortex.load_config()
        if args.command == "disable":
            # The holds to check are those of the repository this folder's private entry names, never the project
            # file's; an entry that cannot be read, or names no valid repository, may have active holds.
            try:
                entry = runtime.stored_approval()
            except (ValueError, storage.StorageError, OSError):
                entry = {}
            name = entry.get("repo") if isinstance(entry, dict) else None
            try:
                active = [] if entry is None else [h for h in observer_store.read_holds(name)
                                                   if h.get("state") == "active"] if recortex.valid_repo(name) else [None]
            except (observer_store.StoreError, storage.StorageError, OSError, ValueError, KeyError, TypeError):
                active = [None]  # holds that cannot be read may be active
            # On the user's own terminal a typed word confirms, as it did, while holds apply; anywhere else the act needs the
            # user's answer to the question of acts.py (kind disable), with or without holds.
            tty = terminal() if on_terminal() else None   # the user's own terminal only: see settle
            if tty is not None:
                with tty:
                    if active and tty.ask("Holds of %s stop applying in this folder. Type disable to confirm: " % name) != "disable":
                        emit({"code": "not-confirmed"}); return 1
                spend_unused("disable")
            else:
                import acts
                values = acts.ACTS["disable"].values(acts.Context())
                if not acts.allowed("disable", values):
                    emit(acts.refusal("disable", values)); return 1
            emit({"action": "disable", "folder": runtime.folder_key(), "was_enabled": runtime.forget()}); return 0
        if args.command == "trust-ancestor":
            # It needs a configured, trusted project and no observer approval, so it does not read the approvals.
            repo = recortex.resolve_repo(args.repo, loaded)
            if not recortex.allowed_users(repo):
                emit({"code": "repository-not-trusted"}); return 1
            return trust_ancestor(args)
        config = runtime.local_config(loaded)
        repo = recortex.resolve_repo(args.repo, config)
        if not recortex.allowed_users(repo):
            emit({"code": "repository-not-trusted"}); return 1
        options = runtime.settings(config)
        if args.command == "map-mode":
            if not runtime.approval_valid(repo, config) or (args.mode != "off" and options["level"] == "notes"):
                emit({"code": "approval", "note": "Choose the Assistant or Everything observer level first."})
                return 1
            if args.yes:
                import observer_grants
                raised = observer_grants.MAP_RANK[args.mode] > observer_grants.MAP_RANK[options["map_mode"]]
                if raised and not on_terminal() and not observer_grants.take(runtime.folder_key(), [("map", args.mode)]):
                    emit(observer_grants.refused({"code": "map_unanswered"}, note=observer_grants.MAP_UNANSWERED))
                    return 1
                if not raised or on_terminal():
                    spend_unused("map")   # lowered, or done on the user's own terminal: an answer nobody used is not kept
                entry = runtime.stored_approval() or {}
                runtime.approve(repo, {"observer": dict(options, map_mode=args.mode)}, given=entry["given"],
                                approved_at=entry["approved_at"])
            emit({"mode": args.mode, "approved": args.yes, "note": "Uses recent eligible conversation, trusted memories "
                  "and approved headings. Shadow stores local proposals. Automatic can add branches or rename them "
                  "after independent review when the observer is also in automatic mode."})
            return 0
        if args.command == "advisory":
            if args.mode in ("list", "mute", "unmute"):
                import observer_advisory
                changing = args.mode != "list"
                if args.yes or bool(args.id) != changing:
                    emit({"code": "invalid-advisory-arguments"})
                    return 1
                if not runtime.approval_valid(repo, config):
                    emit({"code": "approval"})
                    return 1
                try:
                    with observer_store.Store(repo, create=False, readonly=not changing, config=config) as store:
                        if changing:
                            muted = args.mode == "mute"
                            if args.id not in {row["id"] for row in observer_advisory.history(store)}:
                                raise KeyError(args.id)
                            import acts
                            values = {"repo": repo.lower(), "id": args.id, "muted": muted}
                            if not acts.allowed("reminder", values):   # on the user's own terminal it needs none, and spends one
                                emit(acts.refusal("reminder", values)); return 1
                            row = observer_advisory.feedback(store, args.id, muted)
                            emit({"id": row["id"], "muted": row["muted"]})
                        else:
                            emit({"reminders": observer_advisory.history(store)})
                except KeyError:
                    emit({"code": "reminder-unavailable"})
                    return 1
                except observer_store.StoreError as error:
                    if error.reason != "missing":
                        raise
                    emit({"code": "reminder-unavailable"} if changing else {"reminders": []})
                    return int(changing)
                return 0
            if args.id:
                emit({"code": "unexpected-advisory-id"})
                return 1
            if args.yes and runtime.approval_valid(repo, config):
                import acts   # raising it needs the user's answer; lowering it to off, or leaving it, needs none
                values = {"mode": args.mode}
                if ADVISORY_RANK[args.mode] > ADVISORY_RANK[options["advisory_mode"]]:
                    if not acts.allowed("advisory", values):   # on the user's own terminal it needs none, and spends one
                        emit(acts.refusal("advisory", values)); return 1
                else:
                    spend_unused("advisory")
            result = advisory(repo, config, args.mode, args.yes)
            emit(result)
            return int(args.yes and not result.get("approved"))
        if args.command == "enable":
            # Automatic mode is the user's answer to the setup question, which the setup wizard's session records
            # here, as it records the field sheet's approval: no terminal step. Replacing this folder's entry with one
            # for another repository, so that the holds of the one it names stop applying here, and --hosts any, are each the
            # user's answer to a question of acts.py in a session (kinds replace and hosts), and a typed word on their own
            # terminal.
            import acts
            import observer_grants
            folder = runtime.folder_key()
            here = on_terminal()
            state, replacing = acts.replace_state(repo) if args.yes else ("none", None)
            confirm = None
            if state != "none" and here:   # holds that apply, or that cannot be read and may: the user types the word
                confirm = lambda: confirmed("enable", "This replaces this folder's settings while holds apply.")
            # Where the approval is given: the user's own terminal, or a session (Claude Code's Bash tool has none). A command
            # of setup.py's chain has captured output, so the chain says where it was started (its pass, observer_grants).
            chain = observer_grants.pass_entry(folder, args.enable_pass) if args.enable_pass else None
            given = chain["given"] if chain and chain.get("given") in ("terminal", "session") else \
                "terminal" if here else "session"
            wanted = []
            if args.yes and not here:
                wanted = observer_grants.needs(_stored_settings(repo), args.level, args.strong_model, args.mode,
                                               holds=runtime.binding_holds(repo), hosts=args.hosts,
                                               stored_hosts="any" if runtime.keeps_any(repo) else "",
                                               replace=acts.ACTS["replace"].value(replacing) if replacing else None)
            if state == "unknown" and not here:   # never passed over: holds that cannot be read may be active
                confirm = lambda: "holds_unreadable"
            elif wanted:   # asked last, with the hold check, once every setting has been checked: the answer is for this level
                holds_confirm = confirm

                def confirm():
                    for kind, value in sorted(wanted, key=lambda want: {"replace": 0, "hosts": 1}.get(want[0], 2)):
                        if not (observer_grants.in_pass(folder, [(kind, value)], args.enable_pass)
                                or observer_grants.held(folder, [(kind, value)])):
                            return {"hosts": "hosts_unanswered", "replace": "replace_unanswered"}.get(kind, "enable_unanswered")
                    return holds_confirm() if holds_confirm else None
            result = enable(repo, loaded, args.mode, args.yes, args.model, confirm=confirm, given=given, level=args.level,
                            sort_model=args.sort_model, strong_model=args.strong_model, hosts=args.hosts)
            if wanted and result.get("approved") and not observer_grants.in_pass(folder, wanted, args.enable_pass):
                observer_grants.take(folder, wanted)   # the user's answer is used once
            if result.get("code") == "enable_unanswered":
                result = observer_grants.refused(result)
            elif result.get("code") == "holds_unreadable":
                result = dict(result, note=acts.HOLDS_UNREADABLE)
            elif result.get("code") in ("hosts_unanswered", "replace_unanswered"):
                kind = result["code"].split("_")[0]
                result = dict(result, **acts.refusal(kind, {"hosts": {"hosts": "any"}, "replace": replacing}[kind]))
            if result.get("approved"):   # what was lowered, or done on the user's own terminal, spends an answer nobody used
                spend_unused(*[kind for kind in ("level", "strong", "hosts", "replace") if kind not in {k for k, _ in wanted}])
            emit(result)
            return int(args.yes and not result["approved"])
        if args.command == "limits":
            given = "terminal" if on_terminal() else "session"
            result = limits(repo, config, {tier: getattr(args, tier) for tier in runtime.TIERS
                                           if getattr(args, tier) is not None}, given=given)
            emit(result)
            return int("approved" not in result)
        if args.command == "context":
            db = index.connect(repo, create=False)
            if db is None:
                emit({"code": "record-unavailable"}); return 1
            try:
                packet = observer_context.build_packet(db, repo, recortex.allowed_users(repo), query=args.query, issue=args.issue)
                print(packet["text"])
            finally:
                db.close()
            return 0
        if args.command == "reconcile":
            emit(observer_publisher.reconcile(repo, args.eventkey, config, dry_run=args.dry_run)); return 0
        if args.command == "runtime-check" and not runtime.approval_valid(repo, config):
            emit({"code": "approval", "note": "Run observer.py enable --model MODEL --level LEVEL --yes first; the check is "
                                              "bound to the models chosen there."}); return 1
        readonly = (args.command in ("status", "pending", "holds", "log", "map-proposals") or getattr(args, "dry_run", False)
                    or (args.command == "drafts" and args.action is None and not args.show))
        try:
            opened = observer_store.Store(repo, create=not readonly, readonly=readonly,
                         max_bytes=options["max_pending_bytes"], max_pending=options["max_pending_observations"], config=config)
        except observer_store.StoreError as error:
            if readonly and error.reason == "missing":
                if args.command == "pending" and args.waiting:
                    import index
                    import observer_waiting
                    return observer_waiting.print_listing(None, args.json, emit, index.clean, index.DATA_HEADER)
                if args.command == "status":
                    emit(_status(repo, config, loaded, None, options, args.source_details)); return 0
                emit({"code": "empty", "action": args.command, "dry_run": getattr(args, "dry_run", False), "queue": {}, "capture_preflight": observer_sources.preflight(recortex.project_root(), details=getattr(args, "source_details", False))}); return 0
            raise
        with opened as store:
            if args.command == "map-proposals":
                import observer_map
                emit({"data_header": index.DATA_HEADER, "proposals": observer_map.listing(store),
                      "last": store.get_control(observer_map.control("last"), {})})
                return 0
            if args.command == "status":
                emit(_status(repo, config, loaded, store, options, args.source_details)); return 0
            if args.command == "pending":
                if args.waiting:
                    import index
                    import observer_waiting
                    return observer_waiting.print_listing(store, args.json, emit, index.clean, index.DATA_HEADER)
                if args.held:
                    import record_format
                    emit({"held": [{"eventkey": event["eventkey"], "short": event["eventkey"][:8],
                                    "kind": event["candidate"]["kind"],
                                    "issue": event["candidate"]["issue"], "reason": event["reason"],
                                    "line": observer_store.description(event["candidate"]["kind"], event["body"]),
                                    "changes": {key: value for key, value in (record_format.parse_marker(
                                        record_format.first_lines(event["body"])[0]) or {}).items()
                                                if key in ("replaces", "instead", "checks")}}
                                   for event in store.waiting_for_answer()],
                          "note": HELD_NOTE}); return 0
                if args.show_body:
                    if not args.eventkey:
                        emit({"code": "eventkey-required"}); return 1
                    event = store.event(args.eventkey)
                    if event is None:
                        emit({"code": "reviewed-event-unavailable"}); return 1
                    if event["state"] == "verified":
                        observer_publisher._payload_bound(event)
                    else:
                        observer_publisher._bound(event, store)
                    print(event["body"], end=""); return 0
                if args.eventkey:
                    event = store.event(args.eventkey)
                    emit({key: event[key] for key in ("eventkey", "state", "reason")} if event else {"code": "event-unavailable"}); return int(event is None)
                emit({"sources": [{key: row[key] for key in ("source_id", "state", "reason", "created", "updated")}
                                  for row in store.pending(limit=100)],
                      "events": [{key: row[key] for key in ("eventkey", "state", "reason")} for row in store.events(limit=100)]}); return 0
            if args.command == "release":
                tty = terminal() if on_terminal() else None   # the user's own terminal only: see settle
                if tty is None:
                    result = release(store, args.hold_id)
                else:
                    with tty:
                        result = release(store, args.hold_id, tty)
                emit(result); return int(not result.get("released"))
            if args.command == "discard":
                if options["map_mode"] != "off":
                    import observer_map
                    if any(p["key"] == args.eventkey and p["state"] == "review" for p in observer_map.rows(store)):
                        refusal = declined(store, args.eventkey, "no")
                        if refusal:
                            emit(refusal)
                            return 1
                        result = observer_map.answer(store, repo, config, args.eventkey, drop=True)
                        emit(result)
                        return int(result["code"] != "dropped")
                event = store.event(args.eventkey)
                if event is None or event["state"] not in ("ready", "deferred"):
                    emit({"code": "event-not-discardable"}); return 1
                # Dropping changes what waits for the user: on their own terminal, or after their recorded "Drop" (a held
                # memory) or "Drop it" (a task to open) to that item's question. A session cannot drop a memory on its own.
                if not on_terminal():
                    import observer_waiting
                    item = next((row for row in store.waiting() if row["key"] == args.eventkey), None)
                    if item is None:
                        import observer_grants
                        emit(observer_grants.refused({"code": "answer_unanswered"}, note=observer_waiting.UNANSWERED))
                        return 1
                    refused = declined(store, args.eventkey, "no")
                    if refused:
                        emit(refused); return 1
                # Dropping an event posts nothing; one that was held stays marked so, and its copies stay refused.
                store.transition(args.eventkey, "rejected", reason="discarded",
                                 held=observer_publisher.kept(store, event, config))
                if observer_publisher.opens_task(event["candidate"]):
                    # The task will not be opened: the marks that waited for it have nothing to mark.
                    observer_publisher.release_dependents(store, event, False)
                if event["candidate"]["kind"] == "failure":
                    observer_publisher.release_because(store, event)   # a decision drafted with it stands alone
                emit({"action": "discard", "eventkey": args.eventkey}); return 0
            if args.command == "holds":
                emit({"holds": store.holds()}); return 0
            if args.command in ("act", "later", "log", "undo"):
                return _acts(args, store, repo, config, options)
            if args.command == "drafts":
                import observer_drafts
                if args.show or args.action is None:
                    return observer_drafts.show(store, args.show, repo, recortex.project_root()) if args.show \
                        else observer_drafts.listing(store)
                if args.draft is None:
                    emit({"code": "draft_required"}); return 1
                if args.action == "apply":
                    return observer_drafts.apply(store, args.draft, repo, recortex.project_root())
                refused = declined(store, args.draft, "later" if args.action == "later" else "no")
                if refused and not (refused["code"] == "not_waiting"):   # an unknown draft says so itself, below
                    emit(refused); return 1
                return {"later": observer_drafts.later, "drop": observer_drafts.drop}[args.action](store, args.draft)
            if args.command in ("pause", "resume"):
                scopes = ("capture", "publication") if args.scope == "all" else (args.scope,)
                if args.command == "resume" and any(store.get_control(scope + "_paused", False) for scope in scopes):
                    # A pause is the user's control; undoing it is a typed word on their own terminal, and anywhere else
                    # their answer to the question of acts.py (kind resume).
                    import acts
                    paused, at = acts.ACTS["resume"].on_store(store, args.scope)
                    refused = settle("resume", "resume", "Resume %s for %s?" % (" and ".join(scopes), repo),
                                     {"repo": repo.lower(), "scope": args.scope, "at": at})
                    if refused:
                        emit(refused); return 1
                for scope in scopes:
                    store.set_control(scope + "_paused", args.command == "pause")
                    # when it was paused: the question that lifts it names that moment (acts.py, kind resume), so that an answer
                    # about an earlier pause does not count for this one
                    store.set_control(scope + "_paused_at", int(time.time()) if args.command == "pause" else 0)
                emit({"action": args.command, "scope": args.scope}); return 0
            if args.command == "runtime-check":
                ok, tiers = runtime_check(repo, options, store)
                middle = tiers.get("middle", {})
                safe = dict(middle, tiers=tiers)
                if middle.get("code") != "budget":
                    store.set_control("runtime_last", safe)
                emit(safe); return int(not ok)
            if args.command in ("run-worker", "record") and args.dry_run:
                emit({"action": args.command, "dry_run": True, "queue": store.status(),
                      "note": "No model call, queue transition or publication is performed."}); return 0
            if args.command == "record":
                item = store.observation(args.source)
                if item is None or item["state"] not in ("observed", "deferred"):
                    emit({"code": "source-not-retryable"}); return 1
                if not runtime.approval_valid(repo, config):
                    emit({"code": "approval"}); return 1
                with store.transaction():
                    store.db.execute("UPDATE observations SET state='observed',reason='',updated=? WHERE source_id=? AND state IN ('observed','deferred')", (time.time(), args.source))
        if args.command in ("run-worker", "record"):
            import observer_worker
            previous = signal.signal(signal.SIGALRM, _alarm)
            clock = observer_worker.Clock(observer_worker.lifetime(options), armed=True)   # sets the alarm
            try:
                emit(observer_worker.run_once(repo, config, settle_first=args.command == "run-worker", clock=clock))
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0); signal.signal(signal.SIGALRM, previous)
            return 0
    except (OSError, ValueError, TimeoutError, storage.StorageError, observer_store.StoreError, observer_publisher.PublishError) as error:
        out = {"code": "observer-unavailable", "note": "Configuration, private state or runtime could not be validated."}
        reason = str(error) if isinstance(error, ValueError) else ""
        if reason in REASONS:
            out.update(reason=reason, note=NOTES.get(reason, "The observer settings did not validate; nothing was "
                                                                "recorded."))
        emit(out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

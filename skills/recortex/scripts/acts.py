#!/usr/bin/env python3
"""The question for each act that needs the user's yes, and the command each answer runs.

  acts.py ask KIND [VALUE ...] [--scope S] [--to OWNER/NAME] [--branch B] [--path P] [--json] [--typed]
  acts.py list

A session never tells the user to run a command. When the user wants an act (lifting a hold, switching the observer off,
publishing the dashboard page, and the rest of ACTS below), or a script's refusal names one, the session runs `acts.py ask
KIND`, asks the question it prints with the question tool, word for word and with its options in that order, and runs the
command printed under the option the user chose. Before that Bash call the PreToolUse hook (pretool_guard.py) rebuilds the
question that belongs to this very command from the command's own words and from local state (`asks`), looks in the main
transcript for the user's recorded answer to it (observer_sources.answered), and records a one-time grant (observer_grants)
bound to the question's text, its options, the chosen option and this command's values. The act takes the grant once
(`allowed`); without it nothing changes and the refusal names the question (`refusal`). The user's own terminal needs none.

Where the answer cannot be read on this machine (the transcript does not pass the ownership checks), the trust question alone
has a typed form: `acts.py ask trust --typed` prints it as text and notes it, and the user's typed words, equal to the label of
its yes option, are then the answer (observer_grants.typed, called by the prompt hook, and confirmed against the transcript
before the act). Every other question is answered in the question dialog only; where this session has none, the session says so
in one line (NEEDS_DIALOG).

A question holds fixed words and checked values only: a repository name, a hold id, a number, a mode, a path that
trust-ancestor accepts. No title, no comment text, no command text of a hold, no text from GitHub.

Standard library only; the observer's own modules are imported where they are used."""

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import names  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402

ASK = ("recortex: this is done on the user's answer. Run acts.py ask %s, ask the question it prints word for word with its "
       "options, and run this command again after a yes.")
NOT_SET_UP = "acts: this project is not set up, or its repository is not in your trusted list, so nothing is asked."
TYPED_LINE = "To choose, reply with the words of one option, for example: %s"
NEEDS_DIALOG = ("acts: this question is answered in the question dialog only. Where this session has none, tell the user so in "
                "one line; recortex takes no typed answer for it.")
TYPED_UNRECORDED = ("acts: a typed answer cannot be recorded here, because the plugin's hook did not see this command, so "
                    "nothing is asked.")
DESCRIPTION = re.compile(r"[A-Za-z0-9 .,;:'()/_#-]{20,120}")
KIND = re.compile(r"[a-z]{1,10}")
ASCII_PATH = re.compile(r"/[A-Za-z0-9 ._/+@%=,:~-]{0,300}")
HOLD_WORDS = {"git-push": "pushing with git", "git-tag": "changing git tags", "gh-release": "changing GitHub releases",
              "command": "running one exact command (observer.py holds shows it)", "publish": "posting to the record"}
SCOPE_WORDS = {"capture": "note taking", "publication": "posting", "all": "note taking and posting"}
EFFECT = {"task_closed": "reopens task #%d", "task_opened": "closes task #%d as not planned",
          "task_rendered": "puts back the body of #%d from its backup", "hub_rendered": "puts back the hub's body from its backup"}
ROWS_SHOWN = 6
HOLDS_UNREADABLE = ("recortex observer: this machine's observer approval or its holds could not be read, so it is not known "
                    "whether holds would stop applying; nothing was replaced. Run hooks.py --doctor and tell the user what it "
                    "shows.")
OPERATORS = frozenset(";&|(){}<>`") | {"&&", "||", ";;", "|&", ">>", "<<"}


class ActError(Exception):
    """One fixed line, printed by `ask` and never a question."""


def _repo_of(words_before, config):
    """The repository of a command: its `--repo` option, else the project file's."""
    for number, word in enumerate(words_before):
        if word == "--repo" and number + 1 < len(words_before):
            return words_before[number + 1]
        if word.startswith("--repo="):
            return word.split("=", 1)[1]
    return config.get("repo")


def _short(text):
    return " ".join(text.split())


def moment(seconds):
    """A time as the questions print it: UTC, to the minute. Only a whole number of seconds is printed."""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(seconds)) + " UTC"


def digest(kind, *values):
    """The value a grant of `kind` is bound to: the first 32 hex digits of the SHA-256 of [kind, checked values...]."""
    return hashlib.sha256(json.dumps([kind] + list(values), separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


def option(label, description, answer, commands=(), value=None):
    return {"label": label, "description": description, "answer": answer, "commands": list(commands), "value": value}


class Context:
    """The project the command runs in: its folder, configuration and repository (None when it is not set up)."""

    def __init__(self, root=None):
        self.root = os.path.abspath(root or recortex.project_root())
        try:
            self.config = recortex.load_config()
        except (OSError, ValueError):
            self.config = {}
        repo = self.config.get("repo")
        self.repo = repo if recortex.valid_repo(repo) else None


class Act:
    """One kind of act. `values` is a dict of checked values; `text`, `options` and `value` use nothing else."""

    kind = ""
    header = "Question"
    grant = True        # a one-time grant is recorded for it (observer_grants); sign-in has none
    pre_setup = False   # asked before the project is set up (the project file does not exist yet)
    typed = False       # only the trust question has a typed form: the answer to it can never be read back from the transcript

    def parse(self, args, ctx):
        """The values for `ask` from its arguments, or raise ActError with the one fixed line why nothing is asked."""
        raise NotImplementedError

    def from_command(self, words, ctx):
        """The values of the act that the command's words perform, or None. Reads local state only, never GitHub."""
        return None

    def text(self, values):
        raise NotImplementedError

    def options(self, values):
        raise NotImplementedError

    def value(self, values):
        raise NotImplementedError

    def ask_words(self, values):
        """The words after `acts.py ask KIND` that name these values again (what the refusal tells the session to run)."""
        return []

    def labels(self, values):
        return [item["label"] for item in self.options(values)]

    def label(self, values):
        """The yes label that chooses this command's value."""
        value = self.value(values)
        for item in self.options(values):
            if item["answer"] == "yes" and (item["value"] is None or item["value"] == value):
                return item["label"]
        return None


# ---------------------------------------------------------------- reading a command

def segments(command):
    """The commands of a shell line, each a list of words: split at ; & | and the like, quotes taken out. [] when it does not split."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return []
    found, current = [], []
    for word in words:
        if word in OPERATORS or (word and set(word) <= set(";&|(){}<>")):
            if current:
                found.append(current)
            current = []
        else:
            current.append(word)
    if current:
        found.append(current)
    return found


def script_args(words, name):
    """(the words before the script, the words after it) for the first word whose last path part is `name`, or None."""
    for number, word in enumerate(words):
        if os.path.basename(word) == name:
            return words[:number], words[number + 1:]
    return None


def observer_call(words):
    """(repo option or None, subcommand, the words after it) of an observer.py command, or None."""
    found = script_args(words, "observer.py")
    if found is None:
        return None
    rest, repo = found[1], None
    while rest and (rest[0] == "--repo" or rest[0].startswith("--repo=")):
        if rest[0] == "--repo":
            repo, rest = (rest[1] if len(rest) > 1 else None), rest[2:]
        else:
            repo, rest = rest[0].split("=", 1)[1], rest[1:]
    return (repo, rest[0], rest[1:]) if rest else None


def option_value(rest, name):
    for number, word in enumerate(rest):
        if word == name and number + 1 < len(rest):
            return rest[number + 1]
        if word.startswith(name + "="):
            return word.split("=", 1)[1]
    return None


def positionals(rest, flags_with_values=()):
    """The words that are neither an option nor the value of one of `flags_with_values`."""
    out, skip = [], False
    for word in rest:
        if skip:
            skip = False
        elif word in flags_with_values:
            skip = True
        elif not word.startswith("--"):
            out.append(word)
    return out


# ---------------------------------------------------------------- the acts of observer.py

class Release(Act):
    kind, header = "release", "Hold"

    def _hold(self, repo, hold_id):
        import observer_holds
        import observer_store
        if not (recortex.valid_repo(repo) and observer_holds.HOLD_ID.fullmatch(hold_id or "")):
            return None
        try:
            holds = observer_store.read_holds(repo)
        except Exception:  # noqa: BLE001 - holds that cannot be read give no question
            return None
        return next((h for h in holds if isinstance(h, dict) and h.get("id") == hold_id and h.get("state") == "active"
                     and h.get("action") in HOLD_WORDS), None)

    def _values(self, repo, hold_id):
        hold = self._hold(repo, hold_id)
        return None if hold is None else {"repo": repo.lower(), "hold": hold_id, "action": hold["action"]}

    def parse(self, args, ctx):
        if len(args.values) != 1 or ctx.repo is None:
            raise ActError("acts: release takes one hold id, as observer.py holds lists them.")
        found = self._values(ctx.repo, args.values[0])
        if found is None:
            raise ActError("acts: no active hold has that id; observer.py holds lists the holds.")
        return found

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "release" or len(positionals(call[2])) != 1:
            return None
        return self._values(call[0] or ctx.config.get("repo"), positionals(call[2])[0])

    def text(self, values):
        return ("Release your hold %s for %s? You set it in the chat to stop this session from %s. After a yes the recortex "
                "session can do that again, and the moments the hold kept off the record are offered to the observer again."
                % (values["hold"][:8], values["repo"], HOLD_WORDS[values["action"]]))

    def options(self, values):
        return [option("Release hold %s" % values["hold"][:8], "The stop ends now; the session may do it again.", "yes",
                       [("observer.py", "release", values["hold"])]),
                option("Keep hold %s" % values["hold"][:8], "Nothing changes; the stop stays.", "no")]

    def value(self, values):
        return digest(self.kind, values["repo"], values["hold"])

    def ask_words(self, values):
        return [values["hold"]]


class Disable(Act):
    kind, header = "disable", "Observer"

    def parse(self, args, ctx):
        import observer_runtime as runtime
        if args.values:
            raise ActError("acts: disable takes no value.")
        try:
            entry = runtime.stored_approval(ctx.root)
        except Exception:  # noqa: BLE001
            entry = None
        if entry is None:
            raise ActError("acts: the observer is not on for this folder, so there is nothing to turn off.")
        return self.values(ctx)

    def values(self, ctx):
        """The folder and the time its approval was made: the question names that approval, so that an answer about an earlier
        one does not count for this."""
        import observer_runtime as runtime
        try:
            entry = runtime.stored_approval(ctx.root)
            at = entry.get("approved_at") if isinstance(entry, dict) else None
        except Exception:  # noqa: BLE001
            at = None
        return {"folder": runtime.folder_key(ctx.root), "approved": int(at) if type(at) in (int, float) and 0 < at < 2 ** 40 else 0}

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "disable":
            return None
        return self.values(ctx)

    def text(self, values):
        return ("Turn the recortex observer off for this folder? It stops taking notes, drafting and posting here. What it "
                "posted stays on GitHub and its queue is kept. Holds you set for this folder's repository stop applying "
                "here." + (" Its approval was made at %s." % moment(values["approved"]) if values["approved"] else ""))

    def options(self, values):
        return [option("Turn the observer off", "No notes, drafts or posts from this folder until you turn it on again.",
                       "yes", [("observer.py", "disable")]),
                option("Leave it on", "Nothing changes; the observer stays on in this folder.", "no")]

    def value(self, values):
        return digest(self.kind, values["folder"], values["approved"])


class Resume(Act):
    kind, header = "resume", "Pause"

    def on_store(self, store, scope):
        """(whether a pause of the scope is on, when the newest was made: whole seconds, 0 when unknown) in an open store."""
        scopes = ("capture", "publication") if scope == "all" else (scope,)
        on = [name for name in scopes if store.get_control(name + "_paused", False)]
        times = [store.get_control(name + "_paused_at", 0) for name in on]
        times = [t for t in times if type(t) is int and 0 < t < 2 ** 40]
        return bool(on), max(times) if times else 0

    def _paused(self, repo, scope, config):
        import observer_store
        try:
            with observer_store.Store(repo, create=False, readonly=True, config=config) as store:
                return self.on_store(store, scope)
        except Exception:  # noqa: BLE001
            return False, 0

    def parse(self, args, ctx):
        scope = args.scope or "all"
        if args.values or scope not in SCOPE_WORDS or ctx.repo is None:
            raise ActError("acts: resume takes no value, and --scope capture, publication or all.")
        paused, at = self._paused(ctx.repo, scope, ctx.config)
        if not paused:
            raise ActError("acts: nothing is paused in that scope, so there is nothing to resume.")
        return {"repo": ctx.repo.lower(), "scope": scope, "at": at}

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "resume":
            return None
        rest = positionals(call[2])
        scope = rest[0] if rest else "all"
        repo = call[0] or ctx.config.get("repo")
        if scope not in SCOPE_WORDS or len(rest) > 1 or not recortex.valid_repo(repo):
            return None
        return {"repo": repo.lower(), "scope": scope, "at": self._paused(repo, scope, ctx.config)[1]}

    def text(self, values):
        when = " at %s" % moment(values["at"]) if values["at"] else ""
        return ("Resume the recortex observer's %s for %s? You paused it%s. After a yes it goes on from where it stopped."
                % (SCOPE_WORDS[values["scope"]], values["repo"], when))

    def options(self, values):
        words = ("observer.py", "resume") + (() if values["scope"] == "all" else (values["scope"],))
        return [option("Resume the %s" % SCOPE_WORDS[values["scope"]], "It takes up the paused work again.", "yes", [words]),
                option("Keep it paused", "Nothing changes; the pause stays.", "no")]

    def value(self, values):
        return digest(self.kind, values["repo"], values["scope"], values["at"])

    def ask_words(self, values):
        return ["--scope", values["scope"]]


class Undo(Act):
    kind, header = "undo", "Undo"

    def _row(self, repo, number):
        import observer_store
        if not recortex.valid_repo(repo) or type(number) is not int or number < 1:
            return None
        try:
            with observer_store.Store(repo, create=False, readonly=True, config={}) as store:
                row = store.act(number)
        except Exception:  # noqa: BLE001 - anything that cannot be read gives no question and no grant
            return None
        return self.values_of(repo, row)

    def values_of(self, repo, row):
        """The values of the act in `row` (observer_store.Store.act), or None when there is nothing to reverse: no such act,
        one undone before, or a kind that is corrected another way."""
        import observer_jobs
        if row is None or row["undone"] or row["kind"] in observer_jobs.CORRECTED or row["kind"] not in EFFECT:
            return None
        issue = row["issue"] if type(row["issue"]) is int and row["issue"] >= 0 else 0
        return {"repo": repo.lower(), "act": row["id"], "akind": row["kind"], "issue": issue}

    def parse(self, args, ctx):
        if len(args.values) != 1 or not re.fullmatch(r"[0-9]{1,9}", args.values[0]) or ctx.repo is None:
            raise ActError("acts: undo takes one act number, as observer.py log lists them.")
        found = self._row(ctx.repo, int(args.values[0]))
        if found is None:
            raise ActError("acts: that act cannot be undone (no such act, undone before, or one that is corrected "
                           "another way); observer.py log lists the acts.")
        return found

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "undo":
            return None
        rest = positionals(call[2])
        if len(rest) != 1 or not re.fullmatch(r"[0-9]{1,9}", rest[0]):
            return None
        return self._row(call[0] or ctx.config.get("repo"), int(rest[0]))

    def text(self, values):
        effect = EFFECT[values["akind"]]
        return ("Undo act %d of the recortex observer for %s? It %s. This changes GitHub."
                % (values["act"], values["repo"], effect % values["issue"] if "%d" in effect else effect))

    def options(self, values):
        return [option("Undo act %d" % values["act"], "The change is made on GitHub now.", "yes",
                       [("observer.py", "undo", str(values["act"]))]),
                option("Leave it", "Nothing changes; the act stands.", "no")]

    def value(self, values):
        return digest(self.kind, values["repo"], values["act"], values["akind"], values["issue"])

    def ask_words(self, values):
        return [str(values["act"])]


def record_folder(root):
    """The real folder of the project's local record where it is meant to be, from the project file alone: the record's own folder,
    `.recortex` of the project, or the folder this machine's row binds. None for a project with no local record."""
    try:
        config = recortex.load_config()
    except (OSError, ValueError, SystemExit):
        return None
    repo = config.get("repo")
    if not record_store.is_local(repo) or config.get("store") != "local":
        return None
    target = record_store.local_folder_target(repo, config, root)   # the rule that opening the store uses too
    return None if target is None else target[0]


def trust_rows(root, path=None, record=False):
    """(every folder the capture check refuses, the ones to trust): all the trustable ones, or the one at `path`. None when
    `path` is not an absolute, normalized, plain folder above the project's transcripts (or, with `record`, above the project's
    local record: the same question, the same trust, for the folders on the way to the record folder)."""
    import observer_sources
    folder = None
    if record:
        folder = record_folder(root)
        if folder is None:
            return None
    failing = observer_sources.failing(root, folder, "record" if record else "transcripts")
    rows = failing
    if path is not None:
        if not (os.path.isabs(path) and os.path.normpath(path) == path and os.path.isdir(path) and not os.path.islink(path)
                and observer_sources._above(path, folder or observer_sources.project_directory(root))):
            return None
        rows = [row for row in failing if row["path"] == path and row["trustable"]]
    return failing, [row for row in rows if row["trustable"]]


class Trust(Act):
    kind, header = "trust", "Folders"
    typed = True

    def _values(self, chosen):
        rows = [{"path": row["path"], "uid": row["uid"], "gid": row["gid"], "mode": row["mode"],
                 "world": bool(row["world"])} for row in chosen]
        rows.sort(key=lambda row: row["path"])
        if not rows or any(not ASCII_PATH.fullmatch(row["path"]) for row in rows):
            return None
        return {"rows": rows}

    def parse(self, args, ctx):
        if len(args.values) > 1 or args.scope not in (None, "record"):
            raise ActError("acts: trust takes at most one folder, and --scope record for the folders above a local record.")
        record = args.scope == "record"
        found = trust_rows(ctx.root, args.values[0] if args.values else None, record)
        if found is None:
            raise ActError("acts: that is not a folder above Claude Code's transcripts that trust-ancestor can trust."
                           if not record else "acts: that is not a folder above this project's local record that trust-ancestor can "
                           "trust.")
        values = self._values(found[1])
        if values is None:
            raise ActError("acts: no folder above Claude Code's transcripts needs trusting, or one holds characters "
                           "recortex does not print.")
        values["path"] = args.values[0] if args.values else ""
        if record:
            values["record"] = True
        return values

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "trust-ancestor" or "--yes" not in call[2]:
            return None
        record = "--record" in call[2]
        if "--list" in call[2] or option_value(call[2], "--forget") is not None:
            return None
        rest = positionals(call[2])
        path = rest[0] if rest else None
        if (path is None) == ("--failing" not in call[2]) or len(rest) > 1:
            return None
        found = trust_rows(ctx.root, path, record)
        values = None if found is None else self._values(found[1])
        if values is not None and record:
            values["record"] = True
        return values

    def text(self, values):
        rows = values["rows"]
        shown = " ".join("%s (owner %d, group %d, mode %s)." % (row["path"], row["uid"], row["gid"], row["mode"])
                         for row in rows[:ROWS_SHOWN])
        if len(rows) > ROWS_SHOWN:
            shown += " and %d more (observer.py trust-ancestor --failing lists them)." % (len(rows) - ROWS_SHOWN)
        world = (" Anyone on this machine can replace what is below a folder that all may write and that has no sticky "
                 "bit.") if any(row["world"] for row in rows) else ""
        if values.get("record"):
            return ("Trust these folders above this project's local record, so that recortex can read and write the record on this "
                    "machine? %s Each stays trusted only while its owner, group and mode stay as shown. Another account that can write "
                    "to one of them could replace the record's folder.%s" % (shown, world))
        return ("Trust these folders above Claude Code's transcripts, so that recortex can take notes and read your answers "
                "on this machine? %s Each stays trusted only while its owner, group and mode stay as shown. Its owner, and "
                "its group where the group may write, can put another folder in its place.%s" % (shown, world))

    def options(self, values):
        words = ("observer.py", "trust-ancestor") + (("--record",) if values.get("record") else ()) + (
            (values["path"],) if values.get("path") else ("--failing",)) + ("--yes",)
        # the label carries a short token of the folders it is for, so that words typed for one set of folders are no answer
        # for another
        if values.get("record"):
            return [option("Trust the record's folders %s" % self.value(values)[:8],
                           "The record is read and written here while the folders stay as they are.", "yes", [words]),
                    option("Do not trust them", "Nothing changes; the record stays unread on this machine.", "no")]
        return [option("Trust these folders %s" % self.value(values)[:8],
                       "Notes and answers work here while the folders stay as they are.", "yes", [words]),
                option("Do not trust them", "Nothing changes; the observer stays off on this machine.", "no")]

    def value(self, values):
        rows = [[row["path"], row["uid"], row["gid"], row["mode"]] for row in values["rows"]]
        return digest(self.kind, "record", rows) if values.get("record") else digest(self.kind, rows)   # the scope is in the value

    def ask_words(self, values):
        return (["--scope", "record"] if values.get("record") else []) + ([values["path"]] if values.get("path") else [])


class Hosts(Act):
    kind, header = "hosts", "Hosts"

    def parse(self, args, ctx):
        import observer_runtime as runtime
        if args.values or ctx.repo is None:
            raise ActError("acts: hosts takes no value.")
        if runtime.keeps_any(ctx.repo, ctx.root):
            raise ActError("acts: this folder's approval already holds on any host.")
        return {"hosts": "any"}

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is not None and call[1] == "enable" and "--yes" in call[2] and option_value(call[2], "--hosts") == "any":
            return {"hosts": "any"}
        found = script_args(words, "setup.py")
        if found and positionals(found[1])[:1] == ["observer"] and "--yes" in found[1] \
                and option_value(found[1], "--hosts") == "any":
            return {"hosts": "any"}
        return None

    def text(self, values):
        return ("Let this folder's recortex observer approval hold on any host that can read your recortex state folder? Use "
                "it where each host numbers files differently, as some mounts and containers do. A copy of the state folder "
                "to another machine then carries the approval too.")

    def options(self, values):
        return [option("Allow any host", "The approval holds wherever the state folder can be read.", "yes",
                       [("setup.py", "observer", "--level", "<level>", "--hosts", "any", "--yes")]),
                option("Keep it to this key file", "It holds only on hosts that see the same key file.", "no")]

    def value(self, values):
        return "any"


class Advisory(Act):
    kind, header = "advisory", "Reminders"
    MODES = {"deliver": "Deliver the reminders", "shadow": "Try it silently"}

    def parse(self, args, ctx):
        import observer_runtime as runtime
        if args.values not in ([], ["deliver"], ["shadow"]) or ctx.repo is None:
            raise ActError("acts: advisory takes deliver or shadow, or nothing.")
        try:
            on = runtime.approval_valid(ctx.repo, runtime.local_config(ctx.config))
        except Exception:  # noqa: BLE001
            on = False
        if not on:
            raise ActError("acts: the observer is not on for this folder, so its reminders are not offered.")
        return {"mode": args.values[0] if args.values else "deliver"}

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "advisory" or "--yes" not in call[2]:
            return None
        rest = positionals(call[2])
        return {"mode": rest[0]} if rest and rest[0] in self.MODES and len(rest) == 1 else None

    def text(self, values):
        return ("Let the recortex observer look at each prompt for work that rests on a decision the record later changed? "
                "Deliver: it adds a pointer to the earlier and the current decision to that prompt. Try silently: it only "
                "keeps a note of what it would have said, on this machine. Neither calls a model or changes the record.")

    def options(self, values):
        return [option(self.MODES["deliver"], "Matching prompts get one pointer to the decisions involved.", "yes",
                       [("observer.py", "advisory", "deliver", "--yes")], "deliver"),
                option(self.MODES["shadow"], "Nothing is added to prompts; local notes show what would have been.", "yes",
                       [("observer.py", "advisory", "shadow", "--yes")], "shadow"),
                option("Leave it off", "Nothing changes; the observer adds nothing to prompts.", "no")]

    def value(self, values):
        return values["mode"]

    def ask_words(self, values):
        return []


class Reminder(Act):
    kind, header = "reminder", "Reminder"
    ID = re.compile(r"[0-9a-f]{64}")

    def parse(self, args, ctx):
        import observer_advisory
        import observer_store
        if len(args.values) != 2 or args.values[0] not in ("mute", "unmute") or not self.ID.fullmatch(args.values[1]) \
                or ctx.repo is None:
            raise ActError("acts: reminder takes mute or unmute and a reminder id, as observer.py advisory list shows it.")
        muted = args.values[0] == "mute"
        try:
            with observer_store.Store(ctx.repo, create=False, readonly=True, config=ctx.config) as store:
                rows = {row["id"]: row for row in observer_advisory.history(store)}
        except Exception:  # noqa: BLE001
            rows = {}
        if args.values[1] not in rows or rows[args.values[1]]["muted"] == muted:
            raise ActError("acts: no reminder with that id is there to %s." % ("mute" if muted else "show again"))
        return {"repo": ctx.repo.lower(), "id": args.values[1], "muted": muted}

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "advisory" or "--yes" in call[2]:
            return None
        rest = positionals(call[2])
        repo = call[0] or ctx.config.get("repo")
        if len(rest) != 2 or rest[0] not in ("mute", "unmute") or not self.ID.fullmatch(rest[1]) or not recortex.valid_repo(repo):
            return None
        return {"repo": repo.lower(), "id": rest[1], "muted": rest[0] == "mute"}

    def text(self, values):
        if values["muted"]:
            return ("Stop the recortex observer's reminder %s for this task? It is not shown again here until you ask for it."
                    % values["id"][:8])
        return "Show the recortex observer's reminder %s again?" % values["id"][:8]

    def options(self, values):
        if values["muted"]:
            return [option("Mute reminder %s" % values["id"][:8], "It is no longer added to prompts for this task.", "yes",
                           [("observer.py", "advisory", "mute", values["id"])]),
                    option("Keep showing it", "Nothing changes; the reminder keeps coming.", "no")]
        return [option("Show reminder %s again" % values["id"][:8], "It is added to matching prompts for this task again.", "yes",
                       [("observer.py", "advisory", "unmute", values["id"])]),
                option("Keep it muted", "Nothing changes; the reminder stays quiet.", "no")]

    def value(self, values):
        return digest(self.kind, values["repo"], values["id"], values["muted"])

    def ask_words(self, values):
        return ["mute" if values["muted"] else "unmute", values["id"]]


class Replace(Act):
    kind, header = "replace", "Settings"

    def state(self, new, ctx):
        """("none", None): no holds would stop applying; ("known", values): holds of another repository would, and they are
        read; ("unknown", None): the approval or the holds cannot be read, so holds may be active (what entry_holds reports as
        [None]). The unknown state is never passed over: the enable that meets it refuses with HOLDS_UNREADABLE."""
        import observer
        import observer_runtime as runtime
        if not recortex.valid_repo(new):
            return "none", None
        held = observer.entry_holds(keep=new)
        if not held:
            return "none", None
        if held == [None]:
            return "unknown", None
        try:
            entry = runtime.stored_approval(ctx.root)
        except Exception:  # noqa: BLE001
            return "unknown", None
        old = entry.get("repo") if isinstance(entry, dict) else None
        return ("known", {"old": old, "new": new.lower()}) if recortex.valid_repo(old) else ("unknown", None)

    def _values(self, new, ctx):
        state, values = self.state(new, ctx)
        return values if state == "known" else None

    def parse(self, args, ctx):
        new = args.to or ctx.repo
        found = None if args.values else self._values(new, ctx)
        if found is None:
            raise ActError("acts: this folder's settings are not for another repository whose holds apply, so there is "
                           "nothing to replace.")
        return found

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is not None and call[1] == "enable" and "--yes" in call[2]:
            return self._values(call[0] or ctx.config.get("repo"), ctx)
        found = script_args(words, "setup.py")
        if found and positionals(found[1])[:1] == ["observer"] and "--yes" in found[1]:
            return self._values(option_value(found[1], "--repo") or ctx.config.get("repo"), ctx)
        return None

    def text(self, values):
        return ("Replace this folder's recortex observer settings, which are for %s, with settings for %s? Holds you set for "
                "%s then stop applying in this folder." % (values["old"], values["new"], values["old"]))

    def options(self, values):
        return [option("Replace the settings", "The folder follows %s from now on; holds for %s no longer apply here."
                       % (values["new"], values["old"]), "yes",
                       [("observer.py", "--repo", values["new"], "enable", "--model", "<model>", "--level", "<level>", "--yes")]),
                option("Keep the settings", "Nothing changes; the folder keeps its present settings.", "no")]

    def value(self, values):
        return digest(self.kind, values["old"], values["new"])

    def ask_words(self, values):
        return ["--to", values["new"]]


class Publish(Act):
    """The public page of the dashboard. Its values are the target and the four flags of publish.py; GitHub's answers (a missing
    branch, no site, a private record) are read by `ask` and become flags, and publish.py holds them against the flags again."""

    kind, header = "publish", "Public page"

    def parse(self, args, ctx):
        import publish
        if args.values or ctx.repo is None:
            raise ActError("acts: publish takes no value; say another place with --to, --branch and --path.")
        if record_store.is_local(ctx.repo):
            raise ActError(record_store.OFF["pages"])
        record = ctx.repo
        rows = publish.read_receipts(record)
        newest = rows[-1] if rows else None
        to = args.to or (newest["to"] if newest else record)
        branch = args.branch or (newest["branch"] if newest else "gh-pages")
        path = args.path or (newest["path"] if newest else "index.html")
        if not publish.checked_target(to, branch, path):
            raise ActError(publish.BAD_TARGET)
        try:
            target_private, may_push = publish.read_repository(to)
            if not may_push:
                raise ActError(publish.NO_PUSH % to)
            exists = True
            try:
                record_store.for_repo(to).github(["api", "repos/%s/branches/%s" % (to, branch)])
            except recortex.GhError as error:
                if not publish.missing(error):
                    raise
                exists = False
            site, _ = publish.pages_state(to, branch)
            record_private = publish.read_repository(record)[0]
        except (recortex.GhError, ValueError, TypeError, KeyError):
            raise ActError(publish.UNREADABLE_GITHUB.replace("nothing was sent", "nothing is asked"))
        return {"record": record.lower(), "to": to.lower(), "branch": branch, "path": path, "create": not exists,
                "pages": site == "off", "private": record_private is not False, "nosite": site == "other"}

    def from_command(self, words, ctx):
        found = script_args(words, "publish.py")
        if found is None or "--dry-run" in found[1] or "--list" in found[1] or "--help" in found[1] or ctx.repo is None:
            return None
        import publish
        rest = found[1]
        to, branch, path = option_value(rest, "--to"), option_value(rest, "--branch"), option_value(rest, "--path") or "index.html"
        if not to or not branch or not publish.checked_target(to, branch, path):
            return None
        return {"record": ctx.repo.lower(), "to": to.lower(), "branch": branch, "path": path, "create": "--create-branch" in rest,
                "pages": "--pages" in rest, "private": "--private-record" in rest, "nosite": "--no-site" in rest}

    def text(self, values):
        more = ""
        if values["create"]:
            more += "recortex creates the branch %s there, holding only this page. " % values["branch"]
        if values["pages"]:
            more += "recortex switches GitHub Pages on for that branch, which makes the branch a public website. "
        if values["private"]:
            more += "%s is a PRIVATE repository: this makes what its dashboard tells public. " % values["record"]
        if values["nosite"]:
            more += ("GitHub Pages serves another branch of %s, so you get the file's address on GitHub, not a site. "
                     % values["to"])
        return ("Update the public page of the recortex dashboard for %s? It goes to %s, branch %s, file %s. %sAnyone who has its "
                "address can read it, and search engines may list it. It holds what the dashboard tells: the memories, the topic "
                "texts, the tasks and the project map. Your usage numbers, the observer's log and the interactive dashboard are "
                "not sent." % (values["record"], values["to"], values["branch"], values["path"], more))

    def label_for(self, values):
        if values["private"]:
            return "Publish the private record", "Makes this private record's dashboard readable by anyone with the address."
        if values["create"] or values["pages"]:
            return "Set up the public page", "Creates what is missing on GitHub, sends the page and gives you its link."
        return "Update the public page", "Sends the page as it is now and gives you its link."

    def options(self, values):
        label, description = self.label_for(values)
        command = ["publish.py", "--to", values["to"], "--branch", values["branch"], "--path", values["path"]]
        for flag, key in (("--create-branch", "create"), ("--pages", "pages"), ("--private-record", "private"),
                          ("--no-site", "nosite")):
            if values[key]:
                command.append(flag)
        return [option(label, description, "yes", [tuple(command)]),
                option("Show the file's path", "Nothing is sent; the page stays on this machine.", "no")]

    def value(self, values):
        return digest(self.kind, values["record"], values["to"], values["branch"], values["path"], values["create"],
                      values["pages"], values["private"], values["nosite"])

    def ask_words(self, values):
        return ["--to", values["to"], "--branch", values["branch"], "--path", values["path"]]


# ---------------------------------------------------------------- the acts of a local record (setup.py)

def setup_call(words):
    """(the words before it, the subcommand, the words after it) of a setup.py command, or None."""
    found = script_args(words, "setup.py")
    if found is None or not found[1]:
        return None
    return found[0], found[1][0], found[1][1:]


class InProject(Act):
    """The record kept inside the project, in `.recortex`: committed with the project's code if the user commits it, so a public
    repository shows it to everyone. Asked before anything is made, so it needs no project file; its value is bound to the
    project folder. The words about the repository (public, private, not known) are in the option's description, because the
    command that carries the answer cannot know them, and the question text must be the same for both."""

    kind, header = "inproject", "Record"
    pre_setup = True
    STATES = {"public": "The project's repository is public.", "private": "The project's repository is private now.",
              "unknown": "recortex could not tell if its repository is public."}
    ANSWERS = re.compile(r"[^\x00-\x1f\x7f]{1,300}")

    def parse(self, args, ctx):
        answers = args.path
        if args.values or (answers is not None and not self.ANSWERS.fullmatch(answers)):
            raise ActError("acts: inproject takes no state (it is read from the project's remotes), only --path with the answers "
                           "file.")
        # the state is computed here, by the function that `setup.py record-question --in-project` prints: never taken from an argument
        return {"project": os.path.realpath(ctx.root), "state": record_store.project_visibility(ctx.root), "answers": answers}

    def from_command(self, words, ctx):
        call = setup_call(words)
        if call is None or call[1] != "apply" or "--dry-run" in call[2]:
            return None
        path = option_value(call[2], "--answers")
        if path is None:
            return None
        root = option_value(call[2], "--root") or ctx.root
        if os.path.realpath(root) != os.path.realpath(ctx.root):
            return None   # the answer was to the question about this session's project, and covers no other folder
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(data, dict) or data.get("record") != "local" or data.get("place") != "project":
            return None
        return {"project": os.path.realpath(root), "state": "unknown", "answers": path}

    def text(self, values):
        return ("recortex: the record will be in .recortex inside this project. If you commit it, it is committed with your "
                "code, and in a public repository anyone can read it. Keep the record inside the project?")

    def options(self, values):
        command = [("setup.py", "apply", "--answers", values["answers"], "--yes")] if values.get("answers") else []
        return [option("Keep it inside the project", "Create .recortex here; nothing is added to .gitignore. " +
                       self.STATES[values["state"]], "yes", command),
                option("Use its own folder instead", "Keep the record private, outside the project.", "no")]

    def value(self, values):
        return digest(self.kind, values["project"])

    def ask_words(self, values):
        return ["--path", values["answers"]] if values.get("answers") else []


class Folder(Act):
    """Use the record at the folder this machine has not approved for it: the question when a record moved (or was copied with
    its project). Its value is the id and a hash of the folder's real path, which the question does not print."""

    kind, header = "folder", "Folder"

    def _values(self, ctx):
        import record_store
        import storage
        config = ctx.config
        if config.get("store") != "local" or not record_store.is_local(config.get("repo") or ""):
            raise ActError("acts: this project keeps no local record.")
        record_id = record_store.LOCAL_NAME.fullmatch(config["repo"]).group(1)
        if config.get("place") == "project":
            path = storage.record_path(ctx.root)
        elif config.get("place") == "own":
            path = os.path.join(storage.records_top(), storage.PRIVATE, names.RECORDS, record_id)
        else:
            raise ActError("acts: this project's record is in a place this version does not know.")
        if not os.path.isdir(path):
            raise ActError("acts: there is no record folder at the place the project file names.")
        return {"id": record_id, "path": hashlib.sha256(os.path.realpath(path).encode("utf-8")).hexdigest()}

    def parse(self, args, ctx):
        if args.values:
            raise ActError("acts: folder takes no value.")
        values = self._values(ctx)
        if ctx.config.get("place") == "project":   # a record inside the project: what is known of its repository, as `inproject` says it
            values["state"] = record_store.project_visibility(ctx.root)
        return values

    def from_command(self, words, ctx):
        call = setup_call(words)
        if call is None or call[1] != "record-folder" or "--yes" not in call[2]:
            return None
        try:
            return self._values(ctx)
        except ActError:
            return None

    def text(self, values):
        return ("recortex: this project's local record is now at a folder this machine has not approved for it (record %s). "
                "Use the record at its new place?" % values["id"])

    def options(self, values):
        said = " " + InProject.STATES[values["state"]] if values.get("state") in InProject.STATES else ""
        return [option("Use the record there", "This machine then reads and writes the record at that folder." + said, "yes",
                       [("setup.py", "record-folder", "--yes")]),
                option("Leave it unused", "Nothing changes; the record stays unread until you say.", "no")]

    def value(self, values):
        return digest(self.kind, values["id"], values["path"])


class Adopt(Act):
    """Take back as the user's own what recortex does not vouch for: a topic's text that was changed outside recortex, or every
    file that was written under another key. The question shows fixed words and counts, never text; the value binds the bytes
    of the files as they are, so a file that changed since the question refuses the act. A changed entry is never adopted."""

    kind, header = "adopt", "Record"

    def _plan(self, mode, number, ctx):
        import record_store
        try:
            store = record_store.for_repo(ctx.repo, ctx.config)
        except (record_store.StoreError, SystemExit):
            raise ActError("acts: this project's local record cannot be read.")
        if getattr(store, "kind", "") != "local":
            raise ActError("acts: this project keeps no local record.")
        try:
            return store.adopt_plan(mode, number)
        except record_store.StoreError as error:
            raise ActError(str(error))

    def parse(self, args, ctx):
        words = args.values
        if words == ["foreign"]:
            return self._plan("foreign", 0, ctx)
        if len(words) == 2 and words[0] == "topic" and re.fullmatch(r"[1-9][0-9]{0,8}", words[1]):
            return self._plan("topic", int(words[1]), ctx)
        raise ActError("acts: adopt takes `topic N` or `foreign`.")

    def from_command(self, words, ctx):
        call = setup_call(words)
        if call is None or call[1] != "adopt" or "--yes" not in call[2] or ctx.repo is None:
            return None
        topic = option_value(call[2], "--topic")
        try:
            if "--foreign" in call[2]:
                return self._plan("foreign", 0, ctx)
            if topic is not None and re.fullmatch(r"[1-9][0-9]{0,8}", topic):
                return self._plan("topic", int(topic), ctx)
        except ActError:
            return None
        return None

    def text(self, values):
        if values["mode"] == "topic":
            return ("recortex: topic #%d of the local record was changed outside recortex (%d lines of its text differ from the last "
                    "version recortex wrote; it has %d labels and is %s). Take the text, the title and the labels as they are now as "
                    "yours?" % (values["topic"], values["lines"], values["labels"], values["state"]))
        return ("recortex: %d entries, %d topics and %d earlier versions of a text of this record carry a seal this machine cannot "
                "check%s (they were written on another machine, or here before the key changed). Seal them as yours on this machine?"
                % (values["entries"], values["topics"], values["versions"],
                   ", and so does the record's own file" if values["record"] else ""))

    def options(self, values):
        # the yes label carries a token of the bytes asked about, so that an answer is an answer to those bytes and no others: a file
        # that changed since, with the same counts, makes a label that the user never chose
        token = self.value(values)[:16]   # 64 bits: not a prefix that can be searched for a second set of files
        if values["mode"] == "topic":
            return [option("Take topic #%d as mine %s" % (values["topic"], token), "recortex seals the text, the title and the labels "
                           "as they are now, and keeps the last version it wrote.", "yes",
                           [("setup.py", "adopt", "--topic", str(values["topic"]), "--yes")]),
                    option("Leave it as it is", "Nothing changes; the text stays marked as changed outside recortex.", "no")]
        return [option("Seal them as mine %s" % token, "recortex seals those files under this machine's key, and changes nothing else "
                       "in them.", "yes", [("setup.py", "adopt", "--foreign", "--yes")]),
                option("Leave them as they are", "Nothing changes; they stay marked as written elsewhere.", "no")]

    def value(self, values):
        return digest(self.kind, values["mode"], values["topic"], values["digest"])

    def ask_words(self, values):
        return ["topic", str(values["topic"])] if values["mode"] == "topic" else ["foreign"]


# ---------------------------------------------------------------- the table

ACTS = {}


def register(act):
    ACTS[act.kind] = act
    return act


for _act in (Release(), Disable(), Resume(), Undo(), Trust(), Hosts(), Advisory(), Reminder(), Replace(), Publish(),
             InProject(), Folder(), Adopt()):
    register(_act)


def replace_state(new, root=None):
    """The three states of replacing this folder's observer settings with settings for `new`: ("none", None), ("known", values)
    and ("unknown", None); see Replace.state. Unknown is a state of its own, never "none"."""
    return ACTS["replace"].state(new, Context(root))


def replace_values(new, root=None):
    """The values of the act that replaces this folder's observer settings, which are for another repository whose holds apply,
    with settings for `new`, or None when no hold would stop applying or they cannot be read (replace_state tells those apart)."""
    return replace_state(new, root)[1]


def undo_values(repo, row):
    """The values of undoing the act in `row`, or None when there is nothing to reverse."""
    return ACTS["undo"].values_of(repo, row)


def asks(command, root=None):
    """For a Bash `command`, the grants it needs and the one question each belongs to: a list of {"kind", "value", "text",
    "labels", "label"} where `label` is the yes label that chooses this command's value. Read from the command's words and
    local state only. [] for a command that performs no act, and for anything that cannot be read."""
    if not isinstance(command, str) or not command or len(command) > 4000 or not any(
            name in command for name in ("observer", "setup.py", "publish.py")):
        return []
    ctx = Context(root)
    found = []
    for words in segments(command):
        for act in ACTS.values():
            if not act.grant:
                continue
            try:
                values = act.from_command(words, ctx)
                if values is None:
                    continue
                label = act.label(values)
                if label is None:
                    continue
                found.append({"kind": act.kind, "value": act.value(values), "text": act.text(values),
                              "labels": act.labels(values), "label": label})
            except Exception:  # noqa: BLE001 - a hook check never raises; the command is then refused for want of a grant
                continue
    return found


def asking(command):
    """True for a Bash command that runs `acts.py ask`: the hook notes the session that asks (observer_grants.note_asking)."""
    if not isinstance(command, str) or "acts.py" not in command or len(command) > 4000:
        return False
    for words in segments(command):
        found = script_args(words, "acts.py")
        if found is not None and positionals(found[1])[:1] == ["ask"]:
            return True
    return False


def allowed(kind, values, root=None):
    """True on the user's own terminal, else when a live one-time grant for exactly this act and these values is there (it
    is spent). Called right before the first change, after every other check passed."""
    import observer
    import observer_grants
    import observer_runtime as runtime
    if observer.on_terminal():
        observer_grants.spend(runtime.folder_key(root), kind)   # an answer nobody used is not kept for a later replay
        return True
    return observer_grants.take(runtime.folder_key(root), [(kind, ACTS[kind].value(values))])


def refusal(kind, values=None, root=None):
    """The refusal for want of the user's answer: {"code": "<kind>_unanswered", "note": ASK}. `values` makes the note name the
    values to run `acts.py ask` with. When the user's answer cannot be read on this machine the code is
    `<kind>_answer_unreadable` and the note names the trust question."""
    import observer_grants
    words = [kind] + (ACTS[kind].ask_words(values) if values else [])
    return observer_grants.refused({"code": kind + "_unanswered"}, root=root, note=ASK % " ".join(shlex.quote(w) for w in words))


# ---------------------------------------------------------------- ask

def render(words):
    import setup_questions
    return setup_questions.render_command(words)


def form_of(act, root):
    """"typed" for the trust question when the answer cannot be read on this machine (the transcript does not pass the checks), else
    "choice": no other question has a typed form."""
    if not (act.grant and act.typed):
        return "choice"
    import observer_grants
    import observer_runtime as runtime
    try:
        return "typed" if observer_grants.unread_reason(runtime.folder_key(root), root) else "choice"
    except Exception:  # noqa: BLE001
        return "choice"


def describe(act, values, form):
    """The question as a dict: id, header, question, options (label, description, answer, run) and form."""
    out = []
    for item in act.options(values):
        out.append({"label": item["label"], "description": item["description"], "answer": item["answer"],
                    "run": [render(words) for words in item["commands"]]})
    return {"id": act.kind, "header": act.header, "question": act.text(values), "options": out, "form": form}


def question_lines(body, bare_no=False):
    """The text form of a question: its id, the question, one line for each option with the command it runs, and for the typed
    form the line that says how to answer. With `bare_no` an option that runs nothing is its label and description alone."""
    lines = ["question %s" % body["id"], "  " + body["question"]]
    for item in body["options"]:
        if bare_no and not item["run"]:
            lines.append("  - %s: %s" % (item["label"], item["description"]))
        else:
            lines.append("  - %s: %s Run: %s" % (item["label"], item["description"], " ; then ".join(item["run"]) or "nothing"))
    if body.get("form") == "typed":
        lines.append("  " + TYPED_LINE % next(item["label"] for item in body["options"] if item["answer"] == "yes"))
    return lines


def ask_main(args):
    kind = args.kind
    if not KIND.fullmatch(kind) or kind not in ACTS:
        print("acts: unknown kind; acts.py list names them.")
        return 1
    act = ACTS[kind]
    ctx = Context()
    if not act.pre_setup and (ctx.repo is None or ctx.repo.lower() not in recortex.trusted_repos()):
        print(NOT_SET_UP)
        return 1
    try:
        values = act.parse(args, ctx)
    except ActError as error:
        print(str(error))
        return 1
    if args.typed and not (act.grant and act.typed):
        print(NEEDS_DIALOG)
        return 1
    form = "typed" if args.typed else form_of(act, ctx.root)
    if act.grant and act.typed and form != "typed":
        import observer_grants
        import observer_runtime as runtime
        observer_grants.forget_typed(runtime.folder_key(ctx.root), kind)   # a new question ends an older typed answer to it
    body = describe(act, values, form)
    for item in body["options"]:
        if not DESCRIPTION.fullmatch(item["description"]):
            print("acts: a description is not in its fixed form.")
            return 1
    if form == "typed":
        import observer_grants
        import observer_runtime as runtime
        choices = [[item["label"], item["value"] or act.value(values)] for item in act.options(values)
                   if item["answer"] == "yes"]
        nos = [item["label"] for item in act.options(values) if item["answer"] == "no"]
        if not observer_grants.asked_put(runtime.folder_key(ctx.root), kind, choices, nos):
            print(TYPED_UNRECORDED)
            return 1
    if args.json:
        print(json.dumps(body, sort_keys=True))
        return 0
    for line in question_lines(body):
        print(line)
    return 0


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def add_ask_arguments(ask):
    ask.add_argument("kind")
    ask.add_argument("values", nargs="*")
    ask.add_argument("--scope")
    ask.add_argument("--to")
    ask.add_argument("--branch")
    ask.add_argument("--path")
    ask.add_argument("--json", action="store_true")
    ask.add_argument("--typed", action="store_true",
                     help="print the question as text to show in the chat, and take the user's typed words as the answer")


def parser():
    root = Parser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    add_ask_arguments(sub.add_parser("ask", help="the question for one act and the command of each option"))
    sub.add_parser("list", help="the kinds of act, one line each")
    return root


def parse(argv):
    """The arguments of a command line. `ask KIND [VALUES...]` takes its words and its options in any order (`ask trust --scope record
    /folder` and `ask trust /folder --scope record` alike): argparse before 3.12 stops reading positionals after the first option, so
    the words of `ask` are read by parse_intermixed_args, which a parser with subparsers cannot use."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["ask"]:
        ask = Parser(prog="acts.py ask", description="the question for one act and the command of each option")
        add_ask_arguments(ask)
        args = ask.parse_intermixed_args(argv[1:])
        args.command = "ask"
        return args
    return parser().parse_args(argv)


def main(argv=None):
    args = parse(argv)
    if args.command == "list":
        for kind, act in ACTS.items():
            print("%s: %s" % (kind, act.header))
        return 0
    return ask_main(args)


if __name__ == "__main__":
    sys.exit(main())

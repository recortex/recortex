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

Where the question tool is not there, or the answer cannot be read on this machine, `ask` prints the same question as text
and notes it (`--typed`): the user's typed words, equal to an option's label, are then the answer (observer_grants.typed,
called by the prompt hook).

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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import recortex  # noqa: E402

ASK = ("recortex: this is done on the user's answer. Run acts.py ask %s, ask the question it prints word for word with its "
       "options, and run this command again after a yes.")
NOT_SET_UP = "acts: this project is not set up, or its repository is not in your trusted list, so nothing is asked."
TYPED_LINE = "To choose, reply with the words of one option, for example: %s"
DESCRIPTION = re.compile(r"[A-Za-z0-9 .,;:'()/_#-]{20,120}")
KIND = re.compile(r"[a-z]{1,10}")
ASCII_PATH = re.compile(r"/[A-Za-z0-9 ._/+@%=,:~-]{0,300}")
HOLD_WORDS = {"git-push": "pushing with git", "git-tag": "changing git tags", "gh-release": "changing GitHub releases",
              "command": "running one exact command (observer.py holds shows it)", "publish": "posting to the record"}
SCOPE_WORDS = {"capture": "note taking", "publication": "posting", "all": "note taking and posting"}
EFFECT = {"task_closed": "reopens task #%d", "task_opened": "closes task #%d as not planned",
          "task_rendered": "puts back the body of #%d from its backup", "hub_rendered": "puts back the hub's body from its backup"}
ROWS_SHOWN = 6
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
    typed = True        # the typed answer may stand in for the question tool

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
        return [option("Release the hold", "The stop ends now; the session may do it again.", "yes",
                       [("observer.py", "release", values["hold"])]),
                option("Keep the hold", "Nothing changes; the stop stays.", "no")]

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
        return {"folder": runtime.folder_key(ctx.root)}

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "disable":
            return None
        import observer_runtime as runtime
        return {"folder": runtime.folder_key(ctx.root)}

    def text(self, values):
        return ("Turn the recortex observer off for this folder? It stops taking notes, drafting and posting here. What it "
                "posted stays on GitHub and its queue is kept. Holds you set for this folder's repository stop applying "
                "here.")

    def options(self, values):
        return [option("Turn the observer off", "No notes, drafts or posts from this folder until you turn it on again.",
                       "yes", [("observer.py", "disable")]),
                option("Leave it on", "Nothing changes; the observer stays on in this folder.", "no")]

    def value(self, values):
        return digest(self.kind, values["folder"])


class Resume(Act):
    kind, header = "resume", "Pause"

    def parse(self, args, ctx):
        import observer_store
        scope = args.scope or "all"
        if args.values or scope not in SCOPE_WORDS or ctx.repo is None:
            raise ActError("acts: resume takes no value, and --scope capture, publication or all.")
        scopes = ("capture", "publication") if scope == "all" else (scope,)
        try:
            with observer_store.Store(ctx.repo, create=False, readonly=True, config=ctx.config) as store:
                paused = any(store.get_control(name + "_paused", False) for name in scopes)
        except Exception:  # noqa: BLE001
            paused = False
        if not paused:
            raise ActError("acts: nothing is paused in that scope, so there is nothing to resume.")
        return {"repo": ctx.repo.lower(), "scope": scope}

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "resume":
            return None
        rest = positionals(call[2])
        scope = rest[0] if rest else "all"
        repo = call[0] or ctx.config.get("repo")
        if scope not in SCOPE_WORDS or len(rest) > 1 or not recortex.valid_repo(repo):
            return None
        return {"repo": repo.lower(), "scope": scope}

    def text(self, values):
        return ("Resume the recortex observer's %s for %s? You paused it. After a yes it goes on from where it stopped."
                % (SCOPE_WORDS[values["scope"]], values["repo"]))

    def options(self, values):
        words = ("observer.py", "resume") + (() if values["scope"] == "all" else (values["scope"],))
        return [option("Resume the observer", "It takes up the paused work again.", "yes", [words]),
                option("Keep it paused", "Nothing changes; the pause stays.", "no")]

    def value(self, values):
        return digest(self.kind, values["repo"], values["scope"])

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


def trust_rows(root, path=None):
    """(every folder the capture check refuses, the ones to trust): all the trustable ones, or the one at `path`. None when
    `path` is not an absolute, normalized, plain folder above the project's transcripts."""
    import observer_sources
    failing = observer_sources.failing(root)
    rows = failing
    if path is not None:
        if not (os.path.isabs(path) and os.path.normpath(path) == path and os.path.isdir(path) and not os.path.islink(path)
                and observer_sources._above(path, observer_sources.project_directory(root))):
            return None
        rows = [row for row in failing if row["path"] == path and row["trustable"]]
    return failing, [row for row in rows if row["trustable"]]


class Trust(Act):
    kind, header = "trust", "Folders"

    def _values(self, chosen):
        rows = [{"path": row["path"], "uid": row["uid"], "gid": row["gid"], "mode": row["mode"],
                 "world": bool(row["world"])} for row in chosen]
        rows.sort(key=lambda row: row["path"])
        if not rows or any(not ASCII_PATH.fullmatch(row["path"]) for row in rows):
            return None
        return {"rows": rows}

    def parse(self, args, ctx):
        if len(args.values) > 1:
            raise ActError("acts: trust takes at most one folder.")
        found = trust_rows(ctx.root, args.values[0] if args.values else None)
        if found is None:
            raise ActError("acts: that is not a folder above Claude Code's transcripts that trust-ancestor can trust.")
        values = self._values(found[1])
        if values is None:
            raise ActError("acts: no folder above Claude Code's transcripts needs trusting, or one holds characters "
                           "recortex does not print.")
        values["path"] = args.values[0] if args.values else ""
        return values

    def from_command(self, words, ctx):
        call = observer_call(words)
        if call is None or call[1] != "trust-ancestor" or "--yes" not in call[2]:
            return None
        if "--list" in call[2] or option_value(call[2], "--forget") is not None:
            return None
        rest = positionals(call[2])
        path = rest[0] if rest else None
        if (path is None) == ("--failing" not in call[2]) or len(rest) > 1:
            return None
        found = trust_rows(ctx.root, path)
        return None if found is None else self._values(found[1])

    def text(self, values):
        rows = values["rows"]
        shown = " ".join("%s (owner %d, group %d, mode %s)." % (row["path"], row["uid"], row["gid"], row["mode"])
                         for row in rows[:ROWS_SHOWN])
        if len(rows) > ROWS_SHOWN:
            shown += " and %d more (observer.py trust-ancestor --failing lists them)." % (len(rows) - ROWS_SHOWN)
        world = (" Anyone on this machine can replace what is below a folder that all may write and that has no sticky "
                 "bit.") if any(row["world"] for row in rows) else ""
        return ("Trust these folders above Claude Code's transcripts, so that recortex can take notes and read your answers "
                "on this machine? %s Each stays trusted only while its owner, group and mode stay as shown. Its owner, and "
                "its group where the group may write, can put another folder in its place.%s" % (shown, world))

    def options(self, values):
        words = ("observer.py", "trust-ancestor") + ((values["path"],) if values.get("path") else ("--failing",)) + ("--yes",)
        return [option("Trust these folders", "Notes and answers work here while the folders stay as they are.", "yes", [words]),
                option("Do not trust them", "Nothing changes; the observer stays off on this machine.", "no")]

    def value(self, values):
        return digest(self.kind, [[row["path"], row["uid"], row["gid"], row["mode"]] for row in values["rows"]])

    def ask_words(self, values):
        return [values["path"]] if values.get("path") else []


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
            return [option("Mute this reminder", "It is no longer added to prompts for this task.", "yes",
                           [("observer.py", "advisory", "mute", values["id"])]),
                    option("Keep showing it", "Nothing changes; the reminder keeps coming.", "no")]
        return [option("Show this reminder again", "It is added to matching prompts for this task again.", "yes",
                       [("observer.py", "advisory", "unmute", values["id"])]),
                option("Keep it muted", "Nothing changes; the reminder stays quiet.", "no")]

    def value(self, values):
        return digest(self.kind, values["repo"], values["id"], values["muted"])

    def ask_words(self, values):
        return ["mute" if values["muted"] else "unmute", values["id"]]


class Replace(Act):
    kind, header = "replace", "Settings"

    def _values(self, new, ctx):
        import observer
        import observer_runtime as runtime
        if not recortex.valid_repo(new):
            return None
        held = observer.entry_holds(keep=new)
        if not held or held == [None]:
            return None
        try:
            entry = runtime.stored_approval(ctx.root)
        except Exception:  # noqa: BLE001
            return None
        old = entry.get("repo") if isinstance(entry, dict) else None
        return {"old": old, "new": new.lower()} if recortex.valid_repo(old) else None

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


# ---------------------------------------------------------------- the table

ACTS = {}


def register(act):
    ACTS[act.kind] = act
    return act


for _act in (Release(), Disable(), Resume(), Undo(), Trust(), Hosts(), Advisory(), Reminder(), Replace()):
    register(_act)


def replace_values(new, root=None):
    """The values of the act that replaces this folder's observer settings, which are for another repository whose holds apply,
    with settings for `new`, or None when no hold would stop applying."""
    return ACTS["replace"]._values(new, Context(root))


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


def allowed(kind, values, root=None):
    """True on the user's own terminal, else when a live one-time grant for exactly this act and these values is there (it
    is spent). Called right before the first change, after every other check passed."""
    import observer
    if observer.on_terminal():
        return True
    import observer_grants
    import observer_runtime as runtime
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
    """"typed" when the answer cannot be read on this machine (the transcript does not pass the checks), else "choice"."""
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


def ask_main(args):
    kind = args.kind
    if not KIND.fullmatch(kind) or kind not in ACTS:
        print("acts: unknown kind; acts.py list names them.")
        return 1
    act = ACTS[kind]
    ctx = Context()
    if ctx.repo is None or ctx.repo.lower() not in recortex.trusted_repos():
        print(NOT_SET_UP)
        return 1
    try:
        values = act.parse(args, ctx)
    except ActError as error:
        print(str(error))
        return 1
    form = "typed" if args.typed and act.grant and act.typed else form_of(act, ctx.root)
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
        observer_grants.asked_put(runtime.folder_key(ctx.root), kind, choices)
    if args.json:
        print(json.dumps(body, sort_keys=True))
        return 0
    print("question %s" % kind)
    print("  " + body["question"])
    for item in body["options"]:
        print("  - %s: %s Run: %s" % (item["label"], item["description"], " ; then ".join(item["run"]) or "nothing"))
    if form == "typed":
        print("  " + TYPED_LINE % body["options"][0]["label"])
    return 0


def parser():
    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("allow_abbrev", False)
            super().__init__(*args, **kwargs)
    root = Parser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="the question for one act and the command of each option")
    ask.add_argument("kind")
    ask.add_argument("values", nargs="*")
    ask.add_argument("--scope")
    ask.add_argument("--to")
    ask.add_argument("--branch")
    ask.add_argument("--path")
    ask.add_argument("--json", action="store_true")
    ask.add_argument("--typed", action="store_true",
                     help="print the question as text to show in the chat, and take the user's typed words as the answer")
    sub.add_parser("list", help="the kinds of act, one line each")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "list":
        for kind, act in ACTS.items():
            print("%s: %s" % (kind, act.header))
        return 0
    return ask_main(args)


if __name__ == "__main__":
    sys.exit(main())

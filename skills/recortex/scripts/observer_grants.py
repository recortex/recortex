"""One-time grants for turning the observer on, or raising its level or its stronger model, from a session.

The observer's level and its stronger model are the user's choices, made by answering the setup questions
(`observer-level`, `paper-proposals`). A session that runs `observer.py enable ... --yes` or `setup.py observer ...
--yes` does so on that answer only: the hook (pretool_guard.py, before the Bash call, observer on or off) looks in the
main transcript for the user's recorded answer to the plugin's own question, as for a held memory (observer_sources.
answered), and records a grant for exactly the level the chosen option names, or for the stronger model. The command
refuses without it unless it runs on the user's own terminal. A grant holds 10 minutes for this folder and is used
once; `setup.py observer`, which runs a chain of commands, takes it once and gives the chain a pass (a random word, for
its commands only, ended when the chain ends).

The grants and the pass are one small private file beside the approvals (storage.py), never in the project tree. Nothing
in it is text: folders, kinds, values from fixed lists, times and ids."""

import contextlib
import json
import os
import re
import secrets
import shlex
import time

import field_sheet
import observer_runtime as runtime
import observer_sources
import recortex
import storage

FILE = "enable-grants.json"
LOCK = "enable-grants.lock"
SECONDS = 600
CAP = 65536
KEPT = 20            # entries of the file, the newest
SPENT = 200          # answers already used for a grant, the newest: the same answer never grants twice
STRONG_MODEL = "opus"  # the model the paper-proposals question names; another one is the user's own command
RANK = {"notes": 1, "assistant": 2, "everything": 3}
LEVEL_QUESTION, STRONG_QUESTION, MAP_QUESTION = "observer-level", "paper-proposals", "map-upkeep"
QUESTION_OF = {"level": LEVEL_QUESTION, "strong": STRONG_QUESTION, "map": MAP_QUESTION}   # the kinds of grant and their questions
MAP_RANK = {"off": 0, "shadow": 1, "automatic": 2}
MAP_UNANSWERED = ("recortex observer: turning Project Map upkeep on, or raising it, needs your answer to the question about "
                  "it. Ask it as setup.py questions prints it (map-upkeep), and then run the command again. You can also run "
                  "it yourself in a terminal outside Claude Code.")
UNANSWERED = ("recortex observer: turning the observer on, or raising its level or its stronger model, needs your "
              "answer to the question about it. Ask it as setup.py questions prints it (observer-level, or "
              "paper-proposals for the stronger model), and then run the command again. You can also run it yourself "
              "in a terminal outside Claude Code.")
# The codes of a transcript that cannot be read here (observer_sources.UNREADABLE_CODES: the one fixed set). When the user's
# answer cannot be read for one of them, asking again cannot help, and the refusal says so (UNREADABLE) instead of UNANSWERED.
UNREADABLE_CODES = observer_sources.UNREADABLE_CODES
UNREADABLE = ("recortex observer: your answer could not be read on this machine, so asking again will not help: Claude "
              "Code's transcript did not pass the ownership checks (%s). In your own terminal, outside Claude Code, run "
              "observer.py status --source-details to see why and observer.py trust-ancestor --failing to trust the "
              "folders above it, or run this command there.")
PASS = re.compile(r"[0-9a-f]{32}")
ANSWER = re.compile(r"[A-Za-z0-9_-]{1,80}")
# What the file's list of used answers holds: the grant's kind and the answer's tool id ("map-toolu_01..."), since several
# questions answered in one question call share one id, and for the level and the stronger model also the bare id, as 1.6.0
# spent it (a 1.6.0 hook after a downgrade still refuses the replay).
SPENT_ITEM = re.compile(r"(?:(?:level|strong|map)-)?[A-Za-z0-9_-]{1,80}")
BARE_TOO = ("level", "strong")


# ---------------------------------------------------------------- what a command needs

def needs(stored, level, strong, mode):
    """The grants a command that sets `level`, `strong` (a model, or None) and `mode` needs, given this folder's stored
    settings: [("level", level)] when the folder has no level yet (a new folder, or an approval of 1.4.0), or the level
    is raised, or posting goes from shadow to automatic; [("strong", model)] when the stronger model is new or
    changes. Lowering anything needs none."""
    stored = stored if isinstance(stored, dict) else {}
    old = stored.get("level") if stored.get("level") in runtime.LEVELS else ""
    out = []
    new = level or old
    if new in runtime.LEVELS and (not old or RANK[new] > RANK[old]
                                  or (mode == "automatic" and not (stored.get("enabled") and stored.get("mode") == "automatic"))):
        out.append(("level", new))
    if strong and strong != stored.get("strong_model"):
        out.append(("strong", strong))
    return out


# ---------------------------------------------------------------- the file

def _path(create=False):
    folder = storage.folder(storage.state_top(for_key=True), create=create, tighten=create)
    return None if folder is None else os.path.join(folder, FILE)


def _state():
    """{"folders": {...}, "spent": [answer ids]}: what the file holds, or nothing when it is missing or unreadable."""
    empty = {"folders": {}, "spent": []}
    path = _path()
    if path is None:
        return empty
    info = storage.check_path(path, "file")
    if not info or info.st_size > CAP:
        return empty
    try:
        data = json.loads(storage.read_file(path) or "")
    except (ValueError, RecursionError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("folders"), dict):
        return empty
    spent = [item for item in data.get("spent", []) if isinstance(item, str) and SPENT_ITEM.fullmatch(item)]
    return {"folders": data["folders"], "spent": spent[-SPENT:]}


def _read():
    return _state()["folders"]


@contextlib.contextmanager
def _writer():
    top = storage.state_top(for_key=True)
    with field_sheet.approvals_lock(top, LOCK):
        state = _state()
        yield state
        folders = state["folders"]
        trimmed = dict(sorted(folders.items(), key=lambda row: max([e.get("until", 0) for e in row[1].values()
                                                                    if isinstance(e, dict)] or [0]))[-KEPT:])
        storage.write_file(_path(create=True), json.dumps({"form": 2, "folders": trimmed, "spent": state["spent"][-SPENT:]},
                                                          sort_keys=True) + "\n")


def _live(entry, now):
    return isinstance(entry, dict) and type(entry.get("until")) in (int, float) and now < entry["until"] < now + 2 * SECONDS


def put(folder, kind, value, answer, now=None):
    """Record the grant for this folder: the user's answer `answer` (a tool id) to the question about `kind`. An answer that
    was already used for a grant of that kind grants nothing again, whatever the transcript still holds. Another kind's grant
    from the same call is not refused: two questions answered in one question call share one tool id."""
    if kind not in QUESTION_OF or not ANSWER.fullmatch(str(answer)):
        return False
    if (kind == "level" and value not in runtime.LEVELS or kind == "strong" and value != STRONG_MODEL
            or kind == "map" and value not in ("shadow", "automatic")):
        return False
    now = time.time() if now is None else now
    with _writer() as state:
        if _used(state["spent"], kind, answer):
            return False
        entry = state["folders"].setdefault(folder, {}).get(kind)
        if _live(entry, now) and entry.get("answer") == answer and entry.get("value") == value:
            return True   # the same answer, seen again before it was used: nothing changes
        state["folders"][folder][kind] = {"value": value, "answer": answer, "until": now + SECONDS}
    return True


def _used(spent, kind, answer):
    """True when `answer` was already used for a grant of `kind` (for the level and the stronger model also as the bare id)."""
    return "%s-%s" % (kind, answer) in spent or kind in BARE_TOO and answer in spent


def held(folder, wanted, now=None):
    """True when every grant of `wanted` ([(kind, value)]) is there and live for this folder."""
    now = time.time() if now is None else now
    entries = _read().get(folder)
    entries = entries if isinstance(entries, dict) else {}
    return all(_live(entries.get(kind), now) and entries[kind].get("value") == value for kind, value in wanted)


def take(folder, wanted, now=None):
    """Use the grants of `wanted` once: True when every one was live (they are then spent, and so are their answers), else
    False and nothing is spent."""
    now = time.time() if now is None else now
    with _writer() as state:
        entries = state["folders"].get(folder)
        entries = entries if isinstance(entries, dict) else {}
        if not all(_live(entries.get(kind), now) and entries[kind].get("value") == value for kind, value in wanted):
            return False
        for kind, _ in wanted:
            answer = entries.pop(kind)["answer"]
            state["spent"].append("%s-%s" % (kind, answer))
            if kind in BARE_TOO:
                state["spent"].append(answer)
    return True


def note_unreadable(folder, code, now=None):
    """Note, for SECONDS, that the user's answer could not be read for the fixed `code`: {"code", "until"} under the folder's
    entry. True when kept; False for a code off the set."""
    if code not in UNREADABLE_CODES:
        return False
    now = time.time() if now is None else now
    with _writer() as state:
        state["folders"].setdefault(folder, {})["unreadable"] = {"code": code, "until": now + SECONDS}
    return True


def clear_unreadable(folder):
    """Remove the note once an answer could be read again (it is only written when there is one to remove)."""
    entries = _read().get(folder)
    if isinstance(entries, dict) and "unreadable" in entries:
        with _writer() as state:
            entries = state["folders"].get(folder)
            if isinstance(entries, dict):
                entries.pop("unreadable", None)


def unread_reason(folder, root=None, now=None):
    """The code of the transcript problem that keeps the user's answer from being read, or "": the live note a hook left,
    else the code that the capture check reports now when it is not ready and the code is in the set (`source-missing` is
    not: a terminal run before any session has no transcript folder yet). Never raises."""
    now = time.time() if now is None else now
    try:
        entry = _read().get(folder)
        note = entry.get("unreadable") if isinstance(entry, dict) else None
        if _live(note, now) and note.get("code") in UNREADABLE_CODES:
            return note["code"]
        result = observer_sources.preflight(root or recortex.project_root())
        if not result.get("ready") and result.get("code") in UNREADABLE_CODES:
            return result["code"]
    except Exception:  # noqa: BLE001 - a diagnosis never stops the refusal it explains
        pass
    return ""


def refused(result, root=None, note=UNANSWERED):
    """`result`, a refusal whose code ends in `_unanswered`, as it is emitted: unchanged but for `note` when the user's answer
    was simply not given, and with the code `..._answer_unreadable` (`answer_unreadable` for `answer_unanswered`), the fixed
    `reason` and the UNREADABLE note when the answer cannot be read on this machine, which asking again does not mend."""
    code = result.get("code", "")
    reason = unread_reason(runtime.folder_key(root), root) if code.endswith("unanswered") else ""
    if not reason:
        return dict(result, note=note)
    new = "answer_unreadable" if code == "answer_unanswered" else code.replace("_unanswered", "_answer_unreadable")
    return dict(result, code=new, reason=reason, note=UNREADABLE % reason)


def start_pass(folder, level, strong, now=None, given="session", hosts=""):
    """The word that lets the commands of one chain through, after `take` spent the grants: for the level and the stronger
    model the chain sets (its first command may put posting back to shadow before the last one raises it again, so the
    pass covers the target, not each command's own need). The pass also carries what only setup.py can know, because the
    commands of its chain run with their output captured: `given`, where the chain was started ("terminal" or "session"),
    and `hosts`, "any" when it was started on the user's own terminal with --hosts any."""
    now = time.time() if now is None else now
    word = secrets.token_hex(16)
    with _writer() as state:
        state["folders"].setdefault(folder, {})["pass"] = {
            "word": word, "until": now + SECONDS, "level": level or "", "strong": strong or "",
            "given": given if given in ("terminal", "session") else "session", "hosts": "any" if hosts == "any" else ""}
    return word


def pass_entry(folder, word, now=None):
    """The live pass entry of this folder when `word` is its word, else None."""
    now = time.time() if now is None else now
    if not isinstance(word, str) or not PASS.fullmatch(word):
        return None
    entries = _read().get(folder)
    entry = entries.get("pass") if isinstance(entries, dict) else None
    return entry if _live(entry, now) and entry.get("word") == word else None


def in_pass(folder, wanted, word, now=None):
    """True when `word` is the live pass of this folder and its target is what `wanted` asks for."""
    now = time.time() if now is None else now
    if not isinstance(word, str) or not PASS.fullmatch(word):
        return False
    entries = _read().get(folder)
    entry = entries.get("pass") if isinstance(entries, dict) else None
    return (_live(entry, now) and entry.get("word") == word
            and all(entry.get(kind) == value for kind, value in wanted))


def end_pass(folder, word):
    with _writer() as state:
        entries = state["folders"].get(folder)
        if isinstance(entries, dict) and isinstance(entries.get("pass"), dict) and entries["pass"].get("word") == word:
            entries.pop("pass", None)


# ---------------------------------------------------------------- the hook's side

def _question(identifier):
    import setup_questions
    return next(question for question in setup_questions.QUESTIONS if question.id == identifier)


def question_line(kind):
    """The fixed first sentence of the question about `kind`: what the session's question must hold for the user's answer
    to count (setup.py questions prints the whole text; the wizard asks the same first sentence)."""
    text = " ".join(_question(QUESTION_OF[kind]).text.split())
    return text.split("?", 1)[0] + "?"


def canonical(kind):
    """(the whole text, the option labels in order) of the plugin's own question about `kind`, as setup.py questions prints
    them: a recorded answer counts only to a question that is this one and no other."""
    import types
    import setup_questions
    question = _question(QUESTION_OF[kind])
    text = setup_questions.question_text(types.SimpleNamespace(question=question, detail=None), "")
    return " ".join(text.split()), [option.label for option in question.options]


def label_for(kind, value):
    """The label of the option that chooses `value`: the level's own option, or "Yes (Recommended)" for the stronger model
    (opus only: another model is named in the "Other" box, which no recorded label can show). None when no option does."""
    if kind == "level":
        for option in _question(LEVEL_QUESTION).options:
            words = [word for step in option.commands for word in step]
            if "--level" in words and words[words.index("--level") + 1] == value:
                return option.label
        return None
    if kind == "strong" and value == STRONG_MODEL:
        return next((option.label for option in _question(STRONG_QUESTION).options if option.answer == "yes"), None)
    if kind == "map":
        for option in _question(MAP_QUESTION).options:
            for step in option.commands:
                if "map-mode" in step and step.index("map-mode") + 1 < len(step) and step[step.index("map-mode") + 1] == value:
                    return option.label
    return None


def _option(words, name):
    for number, word in enumerate(words):
        if word == name and number + 1 < len(words):
            return words[number + 1]
        if word.startswith(name + "="):
            return word.split("=", 1)[1]
    return None


def requested(command):
    """The grants a Bash `command` asks for, read as the shell splits it: a dict of the kinds `level`, `strong` and `map`
    with the value each names (a kind it does not name is left out), or None when it is not an `observer.py enable --yes`, a
    `setup.py observer --yes`, a `setup.py apply --answers FILE --yes` or an `observer.py map-mode shadow|automatic --yes`.
    A guard against mistakes: a session with shell access can write what it likes."""
    if not isinstance(command, str) or "observer" not in command and "apply" not in command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if "--yes" not in words:
        return None
    for number, word in enumerate(words):
        base = os.path.basename(word)
        rest = [w for w in words[number + 1:] if not w.startswith("--")]
        if base == "observer.py" and "enable" in rest[:2]:
            return _present(level=_option(words, "--level"), strong=_option(words, "--strong-model"))
        if base == "observer.py" and "map-mode" in rest[:2]:
            at = words.index("map-mode", number)
            mode = words[at + 1] if at + 1 < len(words) else None
            return _present(map=mode if mode in ("shadow", "automatic") else None)
        if base == "setup.py" and rest[:1] == ["observer"]:
            return _present(level=_option(words, "--level"), strong=_option(words, "--strong"))
        if base == "setup.py" and rest[:1] == ["apply"]:
            path = _option(words, "--answers")
            try:
                with open(path, encoding="utf-8") as handle:
                    answers = json.loads(handle.read(CAP))
                given = answers.get("observer") if isinstance(answers, dict) else None
            except (TypeError, OSError, ValueError, RecursionError):
                return None
            if isinstance(given, dict) and given.get("mode") not in (None, "off"):
                strong = given.get("strong") if isinstance(given.get("strong"), str) else None
                return _present(level=given.get("level") if isinstance(given.get("level"), str) else None, strong=strong)
            return None
    return None


def _present(**kinds):
    """The kinds that have a value, as a dict."""
    return {kind: value for kind, value in kinds.items() if value is not None}


def record_from_hook(data):
    """Before the Bash call `data`: for the level and the stronger model the command asks for, the grant when the user's
    recorded answer to the question about it chose that very level (or the stronger model). Raises what answered raises; the
    hook swallows it, and then the command is refused for want of a grant."""
    import observer_sources
    asks = requested((data.get("tool_input") or {}).get("command"))
    if asks is None:
        return
    root = recortex.project_root()
    folder = runtime.folder_key(root)
    for kind, value in asks.items():
        label = label_for(kind, value) if isinstance(value, str) else None
        if label is None:
            continue
        text, labels = canonical(kind)
        try:
            answer = observer_sources.answered(data, root, None, question_line(kind), label=label, text_is=text, labels_are=labels)
        except observer_sources.SourceError as error:
            if str(error) in UNREADABLE_CODES:
                note_unreadable(folder, str(error))   # the refusal then says that the answer cannot be read here
            return
        clear_unreadable(folder)
        if answer:
            put(folder, kind, value, answer)

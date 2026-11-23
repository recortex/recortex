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
# The kinds of act that have a question in acts.py (a test holds this equal to the grant rows of acts.ACTS). Each one's value is
# a hash of the act's checked values (acts.digest), except `hosts` ("any") and `advisory` (the mode).
ACT_KINDS = ("release", "disable", "resume", "undo", "trust", "hosts", "advisory", "reminder", "replace", "publish")
TYPED_KINDS = ("trust",)   # the one question whose answer can never be read back from the transcript has a typed form
HASH = re.compile(r"[0-9a-f]{32}")
MAP_UNANSWERED = ("recortex observer: turning Project Map upkeep on, or raising it, needs your answer to the question about "
                  "it. Ask it as setup.py questions prints it (map-upkeep), and then run the command again.")
UNANSWERED = ("recortex observer: turning the observer on, or raising its level or its stronger model, needs your "
              "answer to the question about it. Ask it as setup.py questions prints it (observer-level, or "
              "paper-proposals for the stronger model), and then run the command again.")
# The codes of a transcript that cannot be read here (observer_sources.UNREADABLE_CODES: the one fixed set). When the user's
# answer cannot be read for one of them, asking again cannot help, and the refusal says so (UNREADABLE) instead of UNANSWERED.
UNREADABLE_CODES = observer_sources.UNREADABLE_CODES
UNREADABLE = ("recortex observer: your answer could not be read on this machine, so asking again will not help: Claude "
              "Code's transcript did not pass the ownership checks (%s). Run acts.py ask trust and ask the question it "
              "prints; after a yes, run this command again.")
# The other codes (a link, a path that is no folder, a transcript that changed, a missing or unavailable one): trust-ancestor cannot
# cover them, so the note does not send the user there.
UNREADABLE_OTHER = ("recortex observer: your answer could not be read on this machine, so asking again will not help: Claude "
                    "Code's transcript did not pass the checks (%s), and trusting folders cannot mend that. Run observer.py "
                    "status --source-details and tell the user what it shows. A home folder reached through a link is one "
                    "cause; a private folder of the user's own, named in CLAUDE_CONFIG_DIR, avoids it.")
PASS = re.compile(r"[0-9a-f]{32}")
ANSWER = re.compile(r"[A-Za-z0-9_-]{1,80}")
# What the file's list of used answers holds: the grant's kind and the answer's tool id ("map-toolu_01..."), since several
# questions answered in one question call share one id, and for the level and the stronger model also the bare id, as 1.6.0
# spent it (a 1.6.0 hook after a downgrade still refuses the replay).
SPENT_ITEM = re.compile(r"(?:(?:level|strong|map|%s)-)?[A-Za-z0-9_-]{1,80}" % "|".join(ACT_KINDS))
BARE_TOO = ("level", "strong")


# ---------------------------------------------------------------- what a command needs

def needs(stored, level, strong, mode, holds=True, hosts=None, stored_hosts="", replace=None):
    """The grants a command that sets `level`, `strong` (a model, or None) and `mode` needs, given this folder's stored
    settings: [("level", level)] when the folder has no level yet (a new folder, or an approval of 1.4.0), or the level
    is raised, or posting goes from shadow to automatic; [("strong", model)] when the stronger model is new or
    changes. Lowering anything needs none. `holds` is False when the stored approval is bound to another host (its binding
    does not hold here, as in a copy of the state folder or on a host that sees another inode of the key file): it gives
    this host no authority, so a command that sets the stored level again is a new approval and needs the level grant, as
    a new folder does (the user's own terminal needs none: the caller does not ask). `hosts` "any" (the command's --hosts) adds
    ("hosts", "any") unless `stored_hosts` says the folder's entry already holds a valid "any"; `replace`, the value of the
    replace act (acts.ACTS["replace"].value), adds ("replace", value): the entry of another repository whose holds apply is
    being replaced."""
    stored = stored if isinstance(stored, dict) else {}
    kept = stored.get("level") if stored.get("level") in runtime.LEVELS else ""
    old = kept if holds else ""
    out = []
    new = level or kept
    if new in runtime.LEVELS and (not old or RANK[new] > RANK[old]
                                  or (mode == "automatic" and not (stored.get("enabled") and stored.get("mode") == "automatic"))):
        out.append(("level", new))
    if strong and strong != stored.get("strong_model"):
        out.append(("strong", strong))
    if hosts == "any" and stored_hosts != "any":
        out.append(("hosts", "any"))
    if replace:
        out.append(("replace", replace))
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
    if (kind not in QUESTION_OF and kind not in ACT_KINDS) or not ANSWER.fullmatch(str(answer)) or not _value_fits(kind, value):
        return False
    now = time.time() if now is None else now
    with _writer() as state:
        return _grant(state, folder, kind, value, answer, now)


def _value_fits(kind, value):
    """True when `value` is one the grant of `kind` may hold."""
    if kind == "level":
        return value in runtime.LEVELS
    if kind == "strong":
        return value == STRONG_MODEL
    if kind == "map":
        return value in ("shadow", "automatic")
    if kind == "hosts":
        return value == "any"
    if kind == "advisory":
        return value in ("shadow", "deliver")
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _grant(state, folder, kind, value, answer, now, typed=None):
    """Record the grant in an open writer: False when this answer was already used for `kind`. `typed`, for a grant that comes
    from the user's typed words (typed), is {"label": the words, "offset": where the transcript was when the question was asked,
    or None}: confirm_typed checks it against the transcript before the act."""
    if _used(state["spent"], kind, answer):
        return False
    entry = state["folders"].setdefault(folder, {}).get(kind)
    if _live(entry, now) and entry.get("answer") == answer and entry.get("value") == value:
        return True   # the same answer, seen again before it was used: nothing changes
    made = {"value": value, "answer": answer, "until": now + SECONDS}
    if typed:
        made["typed"] = typed
    state["folders"][folder][kind] = made
    return True


def drop(folder, kind, typed_too=False):
    """Remove the live grant of `kind` for this folder: the newest answer to its question is no longer a yes (or the user said
    no), so an earlier yes that nobody used must not count. A grant that came from typed words stays unless `typed_too`: it has
    no answer in the transcript to be found again. True when one was removed."""
    entries = _read().get(folder)
    entry = entries.get(kind) if isinstance(entries, dict) else None
    if not isinstance(entry, dict) or (entry.get("typed") and not typed_too):
        return False
    with _writer() as state:
        entries = state["folders"].get(folder)
        entry = entries.get(kind) if isinstance(entries, dict) else None
        if not isinstance(entry, dict) or (entry.get("typed") and not typed_too):
            return False
        entries.pop(kind)
    return True


def spend(folder, kind):
    """The act `kind` was done on the user's own terminal, which needs no answer: a live grant of that kind that nobody used is
    spent, with its answer, so that the answer cannot be replayed later. True when there was one."""
    entries = _read().get(folder)
    if not isinstance(entries, dict) or not isinstance(entries.get(kind), dict):
        return False
    with _writer() as state:
        entries = state["folders"].get(folder)
        entry = entries.pop(kind, None) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return False
        state["spent"].append("%s-%s" % (kind, entry.get("answer")))
        if kind in BARE_TOO:
            state["spent"].append(entry.get("answer"))
    return True


def _used(spent, kind, answer):
    """True when `answer` was already used for a grant of `kind` (for the level and the stronger model also as the bare id)."""
    return "%s-%s" % (kind, answer) in spent or kind in BARE_TOO and answer in spent


def _usable(entry, now):
    """A live grant that the act may take: one that came from typed words counts only once the hook has confirmed it
    (confirm_typed marks it), so that the check is bound to the place where the grant is spent."""
    return _live(entry, now) and (not entry.get("typed") or entry.get("confirmed") is True)


def held(folder, wanted, now=None):
    """True when every grant of `wanted` ([(kind, value)]) is there, live and usable for this folder."""
    now = time.time() if now is None else now
    entries = _read().get(folder)
    entries = entries if isinstance(entries, dict) else {}
    return all(_usable(entries.get(kind), now) and entries[kind].get("value") == value for kind, value in wanted)


def take(folder, wanted, now=None):
    """Use the grants of `wanted` once: True when every one was live (they are then spent, and so are their answers), else
    False and nothing is spent."""
    now = time.time() if now is None else now
    with _writer() as state:
        entries = state["folders"].get(folder)
        entries = entries if isinstance(entries, dict) else {}
        if not all(_usable(entries.get(kind), now) and entries[kind].get("value") == value for kind, value in wanted):
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


def unreadable_note(reason):
    """The fixed note for the code of a transcript that cannot be read: UNREADABLE for the two that trust-ancestor covers
    (observer_sources.TRUSTABLE), UNREADABLE_OTHER for the rest. `reason` is one of UNREADABLE_CODES."""
    return (UNREADABLE if reason in observer_sources.TRUSTABLE else UNREADABLE_OTHER) % reason


def refused(result, root=None, note=UNANSWERED):
    """`result`, a refusal whose code ends in `_unanswered`, as it is emitted: unchanged but for `note` when the user's answer
    was simply not given, and with the code `..._answer_unreadable` (`answer_unreadable` for `answer_unanswered`), the fixed
    `reason` and the UNREADABLE note when the answer cannot be read on this machine, which asking again does not mend."""
    code = result.get("code", "")
    reason = unread_reason(runtime.folder_key(root), root) if code.endswith("unanswered") else ""
    if not reason:
        return dict(result, note=note)
    new = "answer_unreadable" if code == "answer_unanswered" else code.replace("_unanswered", "_answer_unreadable")
    return dict(result, code=new, reason=reason, note=unreadable_note(reason))


def start_pass(folder, level, strong, now=None, given="session", hosts="", replace=""):
    """The word that lets the commands of one chain through, after `take` spent the grants: for the level and the stronger
    model the chain sets (its first command may put posting back to shadow before the last one raises it again, so the
    pass covers the target, not each command's own need). The pass also carries what only setup.py can know, because the
    commands of its chain run with their output captured: `given`, where the chain was started ("terminal" or "session"),
    `hosts`, "any" when --hosts any was answered for (on the user's own terminal, or by the grant of kind hosts taken for the
    chain), and `replace`, the value of the replace grant taken for the chain, or "" for none."""
    now = time.time() if now is None else now
    word = secrets.token_hex(16)
    with _writer() as state:
        state["folders"].setdefault(folder, {})["pass"] = {
            "word": word, "until": now + SECONDS, "level": level or "", "strong": strong or "",
            "given": given if given in ("terminal", "session") else "session", "hosts": "any" if hosts == "any" else "",
            "replace": replace if isinstance(replace, str) and HASH.fullmatch(replace) else ""}
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
    """Before the Bash call `data` of the main session: for the level and the stronger model the command asks for, the grant
    when the user's recorded answer to the question about it chose that very level (or the stronger model); and for each act
    the command performs (acts.asks), the grant when the user's recorded answer to the plugin's own question about it, whole and
    with its options in order, chose the option that goes with this command's values. When the newest answer to a question is
    no yes, a live grant of that kind that nobody used is removed (an earlier yes does not outlive a later no). A grant that
    came from the user's typed words is first checked against the transcript (confirm_typed). A call of `acts.py ask` notes the
    session that asks (note_asking). Raises what answered raises; the hook swallows it, and then the command is refused for want
    of a grant."""
    import observer_sources
    if data.get("agent_id") or data.get("agent_type"):
        return
    command = (data.get("tool_input") or {}).get("command")
    root = recortex.project_root()
    folder = runtime.folder_key(root)
    import acts
    if acts.asking(command):
        note_asking(folder, data.get("session_id"), _transcript_size(data, root))
    confirm_typed(data, root, folder)   # for every call that reaches the hook, before any early return
    asks = requested(command) or {}
    acted = acts.asks(command, root)
    if not asks and not acted:
        return
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
        else:
            drop(folder, kind)
    for item in acted:
        try:
            answer = observer_sources.answered(data, root, None, item["text"].split("?", 1)[0] + "?", label=item["label"],
                                               text_is=item["text"], labels_are=item["labels"])
        except observer_sources.SourceError as error:
            if str(error) in UNREADABLE_CODES:
                note_unreadable(folder, str(error))
            return
        clear_unreadable(folder)
        if answer:
            put(folder, item["kind"], item["value"], answer)
        else:
            drop(folder, item["kind"])


# ---------------------------------------------------------------- the typed answer

SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}")
ASKING_SECONDS = 120   # how long the mark of a call of `acts.py ask` is read by the command itself
CLAUDE_NAMES = ("claude",)   # what a Claude Code process is called (its name, or the first word of its command line)
# A word in a parent's command line that shows the Bash tool's own shell (it runs the command with eval, after a snapshot of
# the user's shell): a hook of Claude Code is run by the program itself, never through such a shell.
TOOL_SHELL = re.compile(r"shell-snapshots|(?:^|[\s;&|'\"(])eval(?:[\s'\"]|$)")


def words_of(text):
    """What a typed reply is compared as: its white space and one closing period or exclamation mark taken off, no case."""
    said = text.strip()
    if said[-1:] in (".", "!"):
        said = said[:-1].rstrip()
    return said.casefold()


def _transcript_size(data, root):
    """Where the main transcript ends now, or None when it cannot be read (the typed answer is then not confirmed)."""
    try:
        return os.stat(observer_sources.approved_path(data.get("transcript_path"), root, data.get("session_id")),
                       follow_symlinks=False).st_size
    except Exception:  # noqa: BLE001
        return None


def note_asking(folder, session, offset, now=None):
    """The hook's note, before a call of `acts.py ask` by the main session, of the session that asks and where its transcript
    ends: the command itself reads it (asked_put), so that a typed answer counts only from that session. Nothing in it is text."""
    if not isinstance(session, str) or not SESSION.fullmatch(session) or (offset is not None and type(offset) is not int):
        return False
    now = time.time() if now is None else now
    with _writer() as state:
        state["folders"].setdefault(folder, {})["asking"] = {"session": session, "offset": offset, "until": now + ASKING_SECONDS}
    return True


def asked_put(folder, kind, choices, nos=(), now=None):
    """Note, for SECONDS, that the question about the act `kind` (only the trust question has a typed form: TYPED_KINDS) was shown as
    text (acts.py ask trust --typed): `choices` are the [yes label, value] pairs of its options and `nos` the labels of its no
    options. It is bound to the session that asked (the hook's note, note_asking: without one nothing is noted and this is False)
    and to where its transcript ended. One note a folder: a live note of other values is removed with any grant that came from
    typing, so that its answer cannot count for these values, and a new question ends an older typed grant of its kind. False for
    a kind that has no typed form or a choice off its form."""
    if kind not in TYPED_KINDS or not isinstance(choices, (list, tuple)) or not 1 <= len(choices) <= 2 \
            or not isinstance(nos, (list, tuple)) or len(nos) > 2:
        return False
    clean = []
    for pair in choices:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2 or not isinstance(pair[0], str)
                or not re.fullmatch(r"[A-Za-z0-9 ]{3,60}", pair[0]) or len(pair[0].split()) < 3 or not _value_fits(kind, pair[1])):
            return False
        clean.append([pair[0], pair[1]])
    if any(not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9 ]{3,60}", label) for label in nos):
        return False
    now = time.time() if now is None else now
    with _writer() as state:
        entries = state["folders"].setdefault(folder, {})
        mark = entries.get("asking")
        if not _live(mark, now) or not isinstance(mark.get("session"), str) or not SESSION.fullmatch(mark["session"]):
            return False
        entries.pop("asked", None)   # the words of an older question no longer match anything
        if isinstance(entries.get(kind), dict) and entries[kind].get("typed"):
            entries.pop(kind)
        entries["asked"] = {"kind": kind, "choices": clean, "nos": list(nos), "session": mark["session"],
                            "offset": mark.get("offset"), "until": now + SECONDS}
    return True


def forget_typed(folder, kind):
    """A new question about `kind` was asked in the dialog: a typed answer to an older one, and its note, end."""
    entries = _read().get(folder)
    if not isinstance(entries, dict) or not (isinstance(entries.get(kind), dict) and entries[kind].get("typed")
                                             or isinstance(entries.get("asked"), dict) and entries["asked"].get("kind") == kind):
        return
    with _writer() as state:
        entries = state["folders"].get(folder)
        if isinstance(entries, dict):
            if isinstance(entries.get(kind), dict) and entries[kind].get("typed"):
                entries.pop(kind)
            if isinstance(entries.get("asked"), dict) and entries["asked"].get("kind") == kind:
                entries.pop("asked")


def ancestors(limit=32):
    """[(name, words)] of this process's parents, nearest first, at most `limit`: the process's own name and the words of its
    command line, from /proc (Linux) or `ps`. Whatever cannot be read ends the walk."""
    rows, pid = [], os.getppid()
    for _ in range(limit):
        if pid <= 1:
            break
        try:
            with open("/proc/%d/stat" % pid) as handle:
                stat = handle.read()
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                words = [word.decode("utf-8", "replace") for word in handle.read(4096).split(b"\0") if word]
        except (OSError, ValueError, IndexError):
            import subprocess
            try:
                done = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
                fields = done.stdout.split(None, 1)
                parent, words = int(fields[0]), fields[1].split() if len(fields) > 1 else []
                name = os.path.basename(words[0]) if words else ""
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                break
        rows.append((os.path.basename(name), words))
        pid = parent
    return rows


def plausibly_a_hook(parents=None):
    """True when this process was started the way Claude Code starts a hook: exactly one parent is the claude program, and none is
    the Bash tool's own shell (a command line with `eval` or `shell-snapshots`). The whole ancestry is read: a nested `claude -p`
    or `claude --resume` that a session starts has the tool's shell and a second claude above it, and its hooks are refused. The
    other way round from observer.on_terminal(). A process that was started by hand has no claude above it, and neither has one
    that left its parent (setsid, a daemon). `parents` is ancestors() by default, for a test. A guard against mistakes: a session
    with shell access can find another way."""
    claudes = 0
    for name, words in (ancestors() if parents is None else parents):
        if any(TOOL_SHELL.search(word) for word in words):
            return False
        if name in CLAUDE_NAMES or (words and os.path.basename(words[0]) in CLAUDE_NAMES):
            claudes += 1
    return claudes == 1


def typed(folder, prompt, session, plausible=None, now=None):
    """"yes", "no" or "" for `prompt`, the user's own message as the prompt hook of `session` received it. It is the typed answer
    when it equals, whole, the label of an option of the trust question that was shown as text and noted (asked_put), the note is
    live and was made by this same session, and `plausible()` (plausibly_a_hook) says this is Claude Code's own hook, a first one
    and not a nested session's: "yes" removes the note and records the grant for that option's value under an answer id that is
    only this typed reply's (unconfirmed until the hook of the act has found the words in the transcript, confirm_typed); "no"
    removes the note and any live grant of that kind. The prompt is read as words_of reads it; a prompt with one more word records
    nothing, and a prompt that is no label leaves the note. Never raises."""
    try:
        if not isinstance(prompt, str) or len(prompt) > 200 or not isinstance(session, str) or not SESSION.fullmatch(session):
            return ""
        said = words_of(prompt)
        now = time.time() if now is None else now
        with _writer() as state:
            entries = state["folders"].get(folder)
            note = entries.get("asked") if isinstance(entries, dict) else None
            if (not _live(note, now) or note.get("kind") not in TYPED_KINDS or note.get("session") != session
                    or not isinstance(note.get("choices"), list)):
                return ""
            nos = [label.casefold() for label in note.get("nos", []) if isinstance(label, str)]
            yes = [(label, value) for label, value in note["choices"] if said == label.casefold()]
            if not (said in nos or yes):
                return ""
            if plausible is not None and not plausible():
                return ""   # not a hook of Claude Code: the note stays, nothing is recorded or removed
            entries.pop("asked", None)
            if said in nos:
                entries.pop(note["kind"], None)
                return "no"
            label, value = yes[0]
            made = _grant(state, folder, note["kind"], value, "typed-" + secrets.token_hex(8), now,
                          typed={"label": label, "offset": note.get("offset")})
            return "yes" if made else ""
    except Exception:  # noqa: BLE001 - a hook never fails over a typed answer
        return ""


def confirm_typed(data, root, folder):
    """Before every Bash call of the main session that reaches the hook: a grant that came from the user's typed words is marked
    confirmed when the transcript holds those words as a prompt the user typed after the question was asked
    (observer_sources.typed_words), and when the transcript cannot be read now for a reason of the fixed set
    (observer_sources.UNREADABLE_CODES), which is the case the typed answer exists for. Any other result, an error of any kind, or
    a grant with no place in the transcript to start from, removes the grant. take() and held() use only a confirmed one. Each
    kind is judged alone, and nothing raises."""
    try:
        entries = _read().get(folder)
        pending = [kind for kind, entry in (entries or {}).items()
                   if kind in TYPED_KINDS and isinstance(entry, dict) and isinstance(entry.get("typed"), dict)
                   and entry.get("confirmed") is not True]
    except Exception:  # noqa: BLE001
        return
    for kind in pending:
        try:
            entry = entries[kind]
            offset = entry["typed"].get("offset")
            verdict = False
            try:
                if type(offset) is int:
                    verdict = words_of(entry["typed"]["label"]) in observer_sources.typed_words(data, root, offset)
                else:
                    # the transcript could not be read when the question was asked: only a transcript that still cannot be read
                    # leaves the grant standing
                    observer_sources.approved_path(data.get("transcript_path"), root, data.get("session_id"))
            except observer_sources.SourceError as error:
                verdict = str(error) in UNREADABLE_CODES
            if verdict:
                with _writer() as state:
                    held_entry = (state["folders"].get(folder) or {}).get(kind)
                    if isinstance(held_entry, dict) and held_entry.get("answer") == entry.get("answer"):
                        held_entry["confirmed"] = True
            else:
                drop(folder, kind, typed_too=True)
        except Exception:  # noqa: BLE001 - a doubt removes the grant
            try:
                drop(folder, kind, typed_too=True)
            except Exception:  # noqa: BLE001
                pass

#!/usr/bin/env python3
"""The author's own paper: a file of their own (LaTeX, Markdown, Word) that recortex knows about and never changes on its
own, in any writing mode.

The writing modes (write.py) govern the plugin's own manuscript, which is the record. The author's own paper is theirs:
this script says when the record's text has moved on since the file was last brought up to date, and brings changes over
only when the author asks, as a difference they approve. It looks for no file; it learns the place when the author
names it. No command of the plugin other than `apply` here writes that file, `apply` always needs the author's words,
no hook, no agent and no other script opens it for writing, and nothing is staged, committed or pushed.

where    [--set PATH --yes | --set no --yes]: the place, or record it. A path inside the project folder goes into the
         project file (relative, no `..`, a plain file that is not a symlink) and its hash is confirmed on this machine;
         a path outside it is kept on this machine only, and the project file holds {"outside": true}, so that a project
         file can never point at a file outside the project. The project file is untrusted: a place inside the project is
         used only when this machine holds the author's confirmation of that very path, and never when it lies inside
         .git, .github or .claude, is a file that steers a tool (CLAUDE.md, AGENTS.md, GEMINI.md, the project file) or is
         no manuscript kind of file. "no" says there is none and nothing of this part is shown again.
status   per part (a topic and a heading): never brought over, in step, or changed since.
since    --issue N [--section H] --out DIR: what the file last received (when this machine kept it), what the record says
         now, and their difference, for the session to work from.
propose  --file NEW --out DIR: checks NEW (the author's file with only the changes the record's changes call for) and
         writes the difference against the file as it is now.
apply    --dir DIR --words-file F --parts N:Heading[,...] [--pasted]: the one write, in the order of references/writing.md.

Examples:
  own_paper.py where --set paper/paper.tex --yes
  own_paper.py status
  own_paper.py since --issue 5 --out .tmp/since-5
  own_paper.py propose --file .tmp/new-paper.tex --out .tmp/since-5
  own_paper.py apply --dir .tmp/since-5 --words-file .tmp/words.txt --parts 5:Results
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import field_sheet  # noqa: E402
import index  # noqa: E402
import names  # noqa: E402
import recortex  # noqa: E402
import record_format  # noqa: E402
import storage  # noqa: E402
import write_rules  # noqa: E402

FILE_CAP = 5 * 1024 * 1024
LOCK = "own-paper.lock"
BINARY = (".docx", ".doc", ".odt", ".pdf", ".rtf", ".pages", ".pptx", ".xlsx")
# The kinds of file a place may be. A Word file is a place the plugin can read about but never writes (BINARY above).
PLACE_EXTENSIONS = (".tex", ".md", ".markdown", ".txt", ".rst", ".qmd", ".rmd", ".docx")
# Folders and files that steer a tool, never a manuscript: git's own files, a workflow, the session's instructions.
KEPT_AWAY = (".git", ".github", ".claude")
KEPT_NAMES = ("claude.md", "agents.md", "gemini.md", names.CONFIG_NAME)
NOT_EDITED = "recortex does not edit this kind of file"
WORDS_CAP = 600


def say(text):
    print(text)


def load():
    try:
        config = recortex.load_config()
    except (OSError, ValueError) as error:
        sys.exit("error: the configuration file cannot be read (%s)" % error)
    repo = config.get("repo")
    if not config.get("_path") or not recortex.valid_repo(repo):
        sys.exit("error: this project is not set up yet (no configuration file with a valid repo at %s)" % recortex.project_root())
    print(recortex.describe(config, repo))
    return config, repo


# ---------------------------------------------------------------- the place

def outside_path():
    return os.path.join(storage.config_top(), storage.PRIVATE, names.OWN_PAPER)


def read_place(repo):
    """The entry kept on this machine for `repo` ({"path": absolute path} or {"inside": hash of a relative path}), or {}.
    Only `where --set --yes` writes it, so it says what the author confirmed here; whatever the project file says is not
    that. A file that cannot be read gives {}."""
    try:
        private = storage.folder(storage.config_top(), create=False, tighten=False)
        if private is None or not storage.check_path(os.path.join(private, names.OWN_PAPER), "file"):
            return {}
        data = json.loads(storage.read_file(os.path.join(private, names.OWN_PAPER)) or "")
    except (storage.StorageError, OSError, ValueError, RecursionError):
        return {}
    entry = data.get("papers", {}).get(repo.lower()) if isinstance(data, dict) and isinstance(data.get("papers"), dict) else None
    return entry if isinstance(entry, dict) else {}


def read_outside(repo):
    """The absolute path kept on this machine for `repo`, or None."""
    path = read_place(repo).get("path")
    return path if isinstance(path, str) and os.path.isabs(path) and "\0" not in path and len(path) <= 4000 else None


def inside_hash(relative):
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()


def confirmed_inside(repo, relative):
    """True when the author confirmed this relative path on this machine."""
    kept = read_place(repo).get("inside")
    return isinstance(kept, str) and re.fullmatch(r"[0-9a-f]{64}", kept) is not None and kept == inside_hash(relative)


def write_place(repo, entry):
    with field_sheet.approvals_lock(name=LOCK):
        try:
            private = storage.folder(storage.config_top())
            current = json.loads(storage.read_file(os.path.join(private, names.OWN_PAPER)) or "{}")
        except (ValueError, RecursionError):
            sys.exit("error: %s cannot be read; move it aside by hand and run again" % outside_path())
        papers = current.get("papers") if isinstance(current.get("papers"), dict) else {}
        papers[repo.lower()] = entry
        storage.write_file(os.path.join(private, names.OWN_PAPER), json.dumps({"form": 1, "papers": papers}, indent=1) + "\n")


def refusal(path, config=None):
    """Why a path (relative with `/`, or absolute) may not be the author's own paper, or "": a folder or file that steers a
    tool (.git, .github, .claude, the session's instruction files, the project file), or a kind of file that is no manuscript."""
    parts = [part for part in re.split(r"[\\/]", path) if part]
    if any(part.lower() in KEPT_AWAY for part in parts):
        return "a file inside %s is never the author's paper" % ", ".join(KEPT_AWAY)
    if not parts or parts[-1].lower() in KEPT_NAMES:
        return "that file steers a tool and is never the author's paper"
    if config and config.get("_path") and os.path.realpath(path if os.path.isabs(path) else os.path.join(
            recortex.project_root(), path)) == os.path.realpath(config["_path"]):
        return "the project file is never the author's paper"
    if not parts[-1].lower().endswith(PLACE_EXTENSIONS):
        return "only a manuscript file is a place (%s)" % ", ".join(PLACE_EXTENSIONS)
    return ""


def resolve(config, repo):
    """{"state": ..., "path": the absolute path or "", "problem": a reason or ""}: where the author's file is, after the
    checks: inside the project only by a relative path with no symlink on the way, a plain file."""
    found = recortex.own_manuscript(config)
    state, path = found["state"], ""
    if state == "inside":
        # the project file is not trusted to choose the file: the author must have confirmed this very path on this machine
        why = refusal(found["path"], config)
        if why:
            return {"state": state, "path": "", "problem": why, "found": found}
        if not confirmed_inside(repo, found["path"]):
            return {"state": state, "path": "", "problem": "the author has not confirmed this place on this machine; ask them, "
                    "then run where --set PATH --yes", "found": found}
        root = os.path.realpath(recortex.project_root())
        full = os.path.join(root, found["path"])
        if os.path.realpath(full) != os.path.normpath(full):
            return {"state": state, "path": "", "problem": "the path goes through a symlink or leaves the project folder",
                    "found": found}
        path = full
    elif state == "outside":
        path = read_outside(repo) or ""
        if not path:
            return {"state": state, "path": "", "problem": "no place is kept on this machine", "found": found}
        why = refusal(path, config)
        if why:
            return {"state": state, "path": "", "problem": why, "found": found}
    if path:
        try:
            info = os.lstat(path)
        except OSError:
            return {"state": state, "path": "", "problem": "there is no file there", "found": found}
        if not stat.S_ISREG(info.st_mode):
            return {"state": state, "path": "", "problem": "it is not a plain file (a symlink or a folder is never used)",
                    "found": found}
    return {"state": state, "path": path, "problem": "", "found": found}


def is_text(path, data):
    return not path.lower().endswith(BINARY) and b"\0" not in data[:8192]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def where_command(args):
    config, repo = load()
    placed = resolve(config, repo)
    if not args.set:
        found = placed["found"]
        if found["state"] == "none":
            say("own paper: no place is recorded; ask the author whether they keep a manuscript file of their own")
        elif found["state"] == "no":
            say("own paper: the author keeps none")
        elif placed["problem"]:
            say("own paper: the recorded place is not used (%s)" % placed["problem"])
        else:
            say("own paper: %s; the file is %s" % ("kept inside the project" if found["state"] == "inside" else
                                                   "kept outside the project, on this machine only", placed["path"]))
        return 0
    if not args.yes and not args.dry_run:
        sys.exit("error: pass --yes once the author named the place in the conversation")
    import setup
    path = config["_path"]
    data = json.loads(recortex.read_text(path))
    if args.set.lower() == "no":
        if args.dry_run:
            say("would write: %s   # own_manuscript set to false: the author keeps no paper of their own\ndry run: nothing was "
                "written" % path)
            return 0
        data["own_manuscript"] = False
        setup.write_text(path, json.dumps(data, indent=2) + "\n", False, "the own paper entry", os.path.dirname(path))
        say("own paper: nothing of this part will be shown again")
        return 0
    given = args.set
    full = os.path.abspath(given if os.path.isabs(given) else os.path.join(os.getcwd(), given))
    try:
        info = os.lstat(full)
    except OSError:
        sys.exit("error: %s is not a file" % given)
    if not stat.S_ISREG(info.st_mode):
        sys.exit("error: %s is not a plain file (a symlink is never used)" % given)
    root = os.path.realpath(recortex.project_root())
    brought = recortex.own_manuscript(config)["brought"]
    keep = {str(k): v for k, v in brought.items()}
    inside = os.path.realpath(full) == os.path.normpath(full) and os.path.commonpath([root, os.path.realpath(full)]) == root
    if inside:
        relative = os.path.relpath(os.path.realpath(full), root).replace(os.sep, "/")
        if not recortex.own_path_ok(relative):
            sys.exit("error: that path is not usable (relative, at most %d characters, no '..')" % recortex.OWN_PATH_LIMIT)
        if refusal(relative, config):
            sys.exit("error: %s is not usable: %s" % (relative, refusal(relative, config)))
        if args.dry_run:
            say("would write: this machine's confirmation of %s, then %s   # the path goes into the project file\ndry run: every "
                "check passed; nothing was written" % (relative, path))
            return 0
        write_place(repo, {"inside": inside_hash(relative)})   # first: the confirmation on this machine, then the project file
        data["own_manuscript"] = {"path": relative, "brought": keep}
        say("own paper: %s, inside the project; the path goes into the project file and is confirmed on this machine" % relative)
    else:
        if refusal(os.path.realpath(full), config):
            sys.exit("error: %s is not usable: %s" % (given, refusal(os.path.realpath(full), config)))
        if args.dry_run:
            say("would write: this machine's record of the place, and %s   # outside: the project file holds no path\ndry run: every "
                "check passed; nothing was written" % path)
            return 0
        write_place(repo, {"path": os.path.realpath(full)})
        data["own_manuscript"] = {"outside": True, "brought": keep}
        say("own paper: outside the project folder; the place is kept on this machine only")
    setup.write_text(path, json.dumps(data, indent=2) + "\n", False, "the own paper entry", os.path.dirname(path))
    return 0


# ---------------------------------------------------------------- status and since

def sections_now(db, config, allowed, plan):
    """{(topic, heading): body section text} for the topics of the paper whose body is told."""
    told = index.told_bodies(db, allowed)
    out = {}
    for topic in index.plan_topics(db, plan, config):
        if topic not in told:
            continue
        body = db.execute("SELECT body FROM issues WHERE number = ?", (topic,)).fetchone()["body"] or ""
        for item in record_format.sections(body):
            if item["heading"].lower() != recortex.SOURCES_HEADING.lower():
                out[(topic, item["heading"])] = body
    return out


def part_states(config, db, allowed, plan):
    """[(topic, heading, state)] with state never, in step or changed, for every part of the paper's text."""
    brought = recortex.own_manuscript(config)["brought"]
    rows = []
    for (topic, heading), body in sorted(sections_now(db, config, allowed, plan).items()):
        entry = brought.get(topic, {}).get(heading)
        now = record_format.section_sha(body, heading)
        rows.append((topic, heading, "never" if entry is None else "in step" if entry["text"] == now else "changed"))
    return rows


def open_index(repo):
    db = index.connect(repo, create=False)
    if db is None or not index.mind_ready(db):
        sys.exit("error: no usable local index yet; run index.py sync first")
    return db


def status_command(args):
    config, repo = load()
    placed = resolve(config, repo)
    if placed["found"]["state"] in ("none", "no"):
        say("own paper: %s" % ("no place is recorded" if placed["found"]["state"] == "none" else "the author keeps none"))
        return 0
    if placed["problem"]:
        say("own paper: the recorded place is not used (%s)" % placed["problem"])
        return 0
    plan = recortex.plan_profile(recortex.shipped_profiles(config.get("profiles")))
    allowed = recortex.allowed_users(repo)
    if plan is None or not allowed:
        say("own paper: %s" % placed["path"])
        return 0
    db = open_index(repo)
    try:
        rows = part_states(config, db, allowed, plan)
    finally:
        db.close()
    say("own paper: %s" % placed["path"])
    for topic, heading, state in rows:
        say("%s of #%d: %s" % (heading, topic, {"never": "never brought over", "in step": "in step with the record",
                                                  "changed": "the record's text changed since you last took it"}[state]))
    return 0


def slug(heading):
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def since_command(args):
    config, repo = load()
    placed = resolve(config, repo)
    if not placed["path"]:
        sys.exit("own paper: %s" % (placed["problem"] or "no place is recorded"))
    plan = recortex.plan_profile(recortex.shipped_profiles(config.get("profiles")))
    allowed = recortex.allowed_users(repo)
    if plan is None or not allowed:
        sys.exit("error: needs a project with a plan and a trusted repository")
    db = open_index(repo)
    try:
        sections = sections_now(db, config, allowed, plan)
    finally:
        db.close()
    brought = placed["found"]["brought"]
    wanted = [(t, h) for (t, h) in sections if t == args.issue and (not args.section or h.lower() == args.section.lower())]
    if not wanted:
        sys.exit("own paper: #%d has no told section%s" % (args.issue, " '%s'" % args.section if args.section else ""))
    record, written, notes = {}, {}, []
    for topic, heading in wanted:
        body = sections[(topic, heading)]
        current = record_format.section_text(body, heading) or ""
        entry = brought.get(topic, {}).get(heading)
        record["%d:%s" % (topic, heading)] = record_format.section_sha(body, heading)
        written["current-%s.md" % slug(heading)] = current + "\n"
        received = None
        if entry is not None:
            try:
                received = storage.read_cached(repo, ("own", str(topic)), "%s-%s.md" % (slug(heading), entry["text"]))
            except (storage.StorageError, OSError, UnicodeDecodeError):
                received = None
        if received is not None:
            written["received-%s.md" % slug(heading)] = received
            diff = "\n".join(difflib.unified_diff(received.split("\n"), (current + "\n").split("\n"), "what your file last "
                                                  "received", "what the record says now", lineterm=""))
            written["difference-%s.diff" % slug(heading)] = diff + "\n"
        notes.append("%s of #%d: %s" % (heading, topic, "never brought over; current-%s.md holds the record's text" % slug(heading)
                                         if entry is None else "in step" if entry["text"] == record["%d:%s" % (topic, heading)] else
                                         "changed since; %s" % ("difference-%s.diff holds the difference" % slug(heading)
                                                                if received is not None else
                                                                "this machine kept no copy of what your file received; read the "
                                                                "file against current-%s.md" % slug(heading))))
    written["since.json"] = json.dumps(record, indent=1)
    recortex.write_working_files(args.out, written)   # no name is written through a link
    # The folder is a working folder that an agent can write: apply believes a since.json only when this record, kept here on
    # this machine by since, names the same text.
    storage.write_cached(repo, ("own", "since"), "%s.json" % sha(written["since.json"].encode("utf-8")), written["since.json"])
    for note in notes:
        say(note)
    return 0


# ---------------------------------------------------------------- propose and apply

def propose_command(args):
    config, repo = load()
    placed = resolve(config, repo)
    if not placed["path"]:
        sys.exit("own paper: %s" % (placed["problem"] or "no place is recorded"))
    current = open(placed["path"], "rb").read()
    if len(current) > FILE_CAP or not is_text(placed["path"], current):
        sys.exit("own paper: " + NOT_EDITED + "; write the new passages to a file for the author to paste, and record "
                 "them with apply --pasted once they say they did")
    try:
        text = current.decode("utf-8")
        new = open(args.file, "rb").read()
        new_text = new.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        sys.exit("error: the file and the new copy are read as UTF-8 text")
    if len(new) > FILE_CAP or b"\0" in new:
        sys.exit("error: the new copy is not a text file within the size limit")
    if new == current:
        sys.exit("own paper: the new copy does not differ from the file")
    diff = "\n".join(difflib.unified_diff(text.split("\n"), new_text.split("\n"), "your paper now", "your paper with the "
                                          "record's changes", lineterm=""))
    recortex.write_working_files(args.out, {"proposal.diff": diff + "\n", "proposal.new": new})
    # The folder is a working folder that an agent can write, and the author reviews what is in it: apply believes a
    # proposal only when this record of it, kept here on this machine by propose, names the same new copy, the same
    # difference, the same base and the same file. Nothing it says is read from the folder.
    storage.write_cached(repo, ("own", "proposals"), "%s.json" % sha(new), json.dumps(
        {"base": sha(current), "new": sha(new), "diff": sha((diff + "\n").encode("utf-8")), "path": placed["path"]}))
    say("propose: the difference is in %s; the file's hash now is %s" % (os.path.join(args.out, "proposal.diff"), sha(current)[:12]))
    return 0


def private_record(repo, kind, name):
    """The text of a record that since or propose kept on this machine, or None (unreadable, unsafe or not there)."""
    try:
        return storage.read_cached(repo, ("own", kind), "%s.json" % name)
    except (storage.StorageError, OSError, UnicodeDecodeError):
        return None


def parse_parts(value, since):
    out = []
    for item in (value or "").split(","):
        match = re.fullmatch(r"([1-9][0-9]{0,9}):(.{1,60})", item.strip())
        if not match or "%s:%s" % (match.group(1), match.group(2)) not in since:
            sys.exit("error: --parts names parts of since.json as N:Heading (%s)" % ", ".join(sorted(since)))
        out.append((int(match.group(1)), match.group(2)))
    if not out:
        sys.exit("error: name the parts that were brought over with --parts N:Heading")
    return out


def apply_command(args):
    config, repo = load()
    placed = resolve(config, repo)
    if not placed["path"]:
        sys.exit("own paper: %s" % (placed["problem"] or "no place is recorded"))
    if not args.words_file or not os.path.isfile(args.words_file) or not recortex.read_text(args.words_file).strip():
        sys.exit("error: the author's words are needed, in every writing mode: pass --words-file with their answer")
    since_path = os.path.join(args.dir, "since.json")
    if not os.path.isfile(since_path):
        sys.exit("error: %s has no since.json; run since first" % args.dir)
    since_bytes = open(since_path, "rb").read()
    if private_record(repo, "since", sha(since_bytes)) is None:
        sys.exit("error: since.json is not one that since made on this machine; run since again")
    since = json.loads(since_bytes.decode("utf-8"))
    parts = parse_parts(args.parts, since)
    current = open(placed["path"], "rb").read()
    if not args.pasted:
        if not os.path.isfile(os.path.join(args.dir, "proposal.new")):
            sys.exit("error: %s has no proposal.new; run propose first" % args.dir)
        new = open(os.path.join(args.dir, "proposal.new"), "rb").read()
        shown = open(os.path.join(args.dir, "proposal.diff"), "rb").read() if os.path.isfile(
            os.path.join(args.dir, "proposal.diff")) else b""
        try:   # the folder is not trusted: the hashes that count are the ones propose kept here, for this very copy
            meta = json.loads(private_record(repo, "proposals", sha(new)) or "")
        except (ValueError, RecursionError):
            meta = None
        if not isinstance(meta, dict) or meta.get("new") != sha(new) or meta.get("path") != placed["path"] \
                or not isinstance(meta.get("base"), str) or meta.get("diff") != sha(shown):
            sys.exit("error: the new copy and its difference are not ones that propose made on this machine for this file; "
                     "make them again")
        if sha(current) != meta["base"]:
            sys.exit("own paper: your paper changed since the difference was made; make it again")
        if len(current) > FILE_CAP or not is_text(placed["path"], current):
            sys.exit("own paper: " + NOT_EDITED)
        if args.dry_run:
            say("would write: %s   # %d bytes, through a temporary file after a backup kept on this machine, then read back; and "
                "the project file records that %s %s brought over\ndry run: every check passed; nothing was written" % (
                    placed["path"], len(new), ", ".join("%s of #%d" % (h, t) for t, h in parts),
                    "was" if len(parts) == 1 else "were"))
            return 0
        storage.write_cached(repo, ("own",), "backup-%s.txt" % sha(current), current.decode("utf-8"))
        folder = os.path.dirname(placed["path"])
        mode = stat.S_IMODE(os.lstat(placed["path"]).st_mode)
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".recortex-")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(new)
            if os.path.islink(placed["path"]) or not stat.S_ISREG(os.lstat(placed["path"]).st_mode):
                sys.exit("own paper: the file became a symlink or not a plain file; nothing was written")
            os.replace(temporary, placed["path"])
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        if sha(open(placed["path"], "rb").read()) != meta["new"]:
            sys.exit("own paper: the file does not read back as written; the backup is in %s" % os.path.join(
                storage.cache_folder(repo, "own"), "backup-%s.txt" % sha(current)))
        say("own paper: wrote %s and read it back" % placed["path"])
    elif args.dry_run:
        say("would write: the project file only, to record that %s %s brought over; the author's file is not written\ndry run: "
            "every check passed; nothing was written" % (", ".join("%s of #%d" % (h, t) for t, h in parts),
                                                          "was" if len(parts) == 1 else "were"))
        return 0
    else:
        say("own paper: nothing is written to the file; the author pasted the passages themselves")
    commit = ""
    db = open_index(repo)
    try:
        allowed = recortex.allowed_users(repo)
        state = index.planned(db, config, allowed, recortex.plan_profile(recortex.shipped_profiles(config.get("profiles"))))
        texts = write_rules.text_memories(db, state["found"]["items"]) if state else []
    finally:
        db.close()
    import setup
    data = json.loads(recortex.read_text(config["_path"]))
    entry = data.get("own_manuscript")
    if not isinstance(entry, dict):
        sys.exit("error: the project file has no own_manuscript entry to record in")
    brought = entry.setdefault("brought", {})
    for topic, heading in parts:
        newest = write_rules.newest_text(texts, topic)
        commit = newest["version"] if newest and newest["version"] else ""
        brought.setdefault(str(topic), {})[heading] = {"text": since["%d:%s" % (topic, heading)], "commit": commit}
        current_text = open(os.path.join(args.dir, "current-%s.md" % slug(heading)), encoding="utf-8").read() \
            if os.path.isfile(os.path.join(args.dir, "current-%s.md" % slug(heading))) else None
        if current_text is not None and sha(current_text.strip("\n").encode("utf-8")) == since["%d:%s" % (topic, heading)]:
            try:
                storage.write_cached(repo, ("own", str(topic)), "%s-%s.md" % (slug(heading), since["%d:%s" % (topic, heading)]),
                                     current_text)
            except (storage.StorageError, OSError):
                pass
    setup.write_text(config["_path"], json.dumps(data, indent=2) + "\n", False, "what was brought into your paper",
                     os.path.dirname(config["_path"]))
    say("own paper: recorded that %s %s brought over" % (", ".join("%s of #%d" % (h, t) for t, h in parts),
                                                          "was" if len(parts) == 1 else "were"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("where")
    p.add_argument("--set", help="the path of the file, or no")
    p.add_argument("--yes", action="store_true", help="the author named it in the conversation")
    p.add_argument("--dry-run", action="store_true", help="run every check and say what would be written; write nothing")
    sub.add_parser("status")
    p = sub.add_parser("since")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--section")
    p.add_argument("--out", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("apply")
    p.add_argument("--dir", required=True)
    p.add_argument("--words-file")
    p.add_argument("--parts")
    p.add_argument("--pasted", action="store_true", help="the author pasted the passages; nothing is written to the file")
    p.add_argument("--dry-run", action="store_true", help="run every check and say what would be written; write nothing")
    args = parser.parse_args(argv)
    return {"where": where_command, "status": status_command, "since": since_command, "propose": propose_command,
            "apply": apply_command}[args.command](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (recortex.GhError, storage.StorageError) as error:
        sys.exit("error: %s" % error)

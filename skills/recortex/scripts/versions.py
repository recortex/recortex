#!/usr/bin/env python3
"""The versions of the plugin's own manuscript: one commit per change of a topic's text, on one branch of the record's
repository (references/versions.md).

The topic body stays the deliverable text and the issues stay the record. The branch `recortex-manuscript` is a copy of
the text as files, in Manubot's layout (paper_layout.py), one commit per change, so that git gives every version, the
difference between any two and the names (tags). Nothing is read from the branch into the record except through the
checked paths: a restore (the file's hash must be the "after" value of a told text change) and a merged proposal
(write.py). Nothing is cloned and the project's working tree is never touched: every write is an API call through
the record store's raw call (the git data endpoints), the head is read, one tree and one commit are posted, and the branch is moved
forward without force. A head that moved meanwhile is read again and the commit is made once more.

The writing mode is the consent: versions are kept exactly when the mode of this machine is review or autonomous, and
no write to the branch happens before that. The hooks never contact the branch.

status        per topic: in step, the body changed since its last commit, or the file changed without the body; the names
sync          for each body that changed outside the plugin: one commit and one text change that says who edited it
log           [--issue N] [--json]: the versions, newest first
diff          A B [--issue N]: the difference between two versions (commits or names)
name          NAME --words-file F: names the paper as it stands (a decision on the plan issue, then a tag)
restore       --issue N --to A [--section H] --out DIR: prepares a restore of the text of a topic (write.py takes it on)
fetch         [--issue N]: brings the vouched versions' files into the private cache (the mind page calls it)
doctor        the mode, the permission, the branch, the last refusal with its reason
meta          [--authors "A; B"] [--keywords "x, y"] --yes: the authors and keywords of the paper's metadata

Examples:
  versions.py status
  versions.py log --issue 5
  versions.py diff paper-submitted HEAD --issue 5
  versions.py name submitted --words-file .tmp/words.txt
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import names  # noqa: E402
import paper_layout  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402
import record_format  # noqa: E402
import storage  # noqa: E402
import write_rules  # noqa: E402
import writing  # noqa: E402

BRANCH = names.MANUSCRIPT_BRANCH
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(HERE), "templates")
STATE_FILE = "state.json"
# A name of a version is also the tag paper-<name>, so it must be a name git accepts as a ref: no "..", no end in "." and
# none in ".lock" (the letters, digits, "." and "-" it is made of are otherwise allowed in a ref name).
NAME = re.compile(r"(?!.*\.\.)(?!.*\.$)(?!.*\.lock$)[a-z0-9][a-z0-9.-]{0,39}")
SHA40 = re.compile(r"[0-9a-f]{40}")
FILE_CAP = 2 * 1024 * 1024

# The fixed lines of references/versions.md (section 8.6 of the spec), each with the key `doctor` stores.
LINES = {
    "push": "versions: this account may not write files to %s, so this version was not kept as a file. The text itself is "
            "saved. Run versions.py doctor.",
    "rule": "versions: a branch rule of %s refused the write to " + BRANCH + ", so this version was not kept as a file. The "
            "text itself is saved. Run versions.py doctor.",
    "moved": "versions: the branch changed while writing; run versions.py sync.",
}
REASON = {"push": record_format.NOT_KEPT[0], "rule": record_format.NOT_KEPT[1], "moved": record_format.NOT_KEPT[2],
          "off": record_format.NOT_KEPT[3]}
OFF_LINE = "versions are kept once you choose review or autonomous writing."


class GitError(Exception):
    """An answer of GitHub that was not a success: the HTTP status (0 when none), and what gh printed (kept for decisions
    only, never shown)."""

    def __init__(self, status, text=""):
        super().__init__("GitHub answered %s" % (status or "nothing"))
        self.status, self.text = status, text


class Refused(Exception):
    """A write of the branch that GitHub refused, as one of the keys of LINES."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def say(text):
    print(text)


# ---------------------------------------------------------------- GitHub

def api(repo, method, path, payload=None, raw=False):
    """One call to the REST API through the record store's raw call: parsed JSON, or the bytes of a file with raw=True."""
    args = ["api", "-X", method, "repos/%s/%s" % (repo, path)]
    if raw:
        args += ["-H", "Accept: application/vnd.github.raw+json"]
    if payload is not None:
        args += ["--input", "-"]
    try:
        out = record_store.for_repo(repo).github(args, stdin=json.dumps(payload) if payload is not None else None, timeout=60,
                                                 binary=raw)
    except recortex.GhError as error:
        found = re.search(r"HTTP ([0-9]{3})", str(error))
        raise GitError(int(found.group(1)) if found else 0, str(error))
    except (recortex.GhMissing, recortex.GhTimeout) as error:
        raise GitError(0, str(error))
    if raw:
        return out
    try:
        return json.loads(out) if out.strip() else {}
    except ValueError:
        raise GitError(0, "the answer was not JSON")


def can_push(repo):
    try:
        out = record_store.for_repo(repo).github(["api", "repos/%s" % repo, "--jq", ".permissions.push"], timeout=30)
    except (recortex.GhError, recortex.GhMissing, recortex.GhTimeout):
        return False
    return out.strip() == "true"


def head_of(repo, ref=BRANCH):
    """The commit a branch (or tag, `ref` as heads/NAME or tags/NAME) points at, or None when it does not exist."""
    prefix = ref if "/" in ref else "heads/" + ref
    try:
        found = api(repo, "GET", "git/ref/" + prefix)
    except GitError as error:
        if error.status == 404:
            return None
        raise
    sha = (found.get("object") or {}).get("sha") if isinstance(found, dict) else None
    return sha if isinstance(sha, str) and SHA40.fullmatch(sha) else None


def tree_of(repo, commit):
    found = api(repo, "GET", "git/commits/%s" % commit)
    sha = (found.get("tree") or {}).get("sha") if isinstance(found, dict) else None
    if not isinstance(sha, str) or not SHA40.fullmatch(sha):
        raise GitError(0, "no tree")
    return sha


def listing(repo, tree):
    """The paths of the files of a tree, recursively."""
    found = api(repo, "GET", "git/trees/%s?recursive=1" % tree)
    return sorted(item["path"] for item in found.get("tree", []) if isinstance(item, dict) and item.get("type", "blob") == "blob"
                  and isinstance(item.get("path"), str))


def file_at(repo, ref, path):
    """The text of a file at a commit, a tag or the branch, or None when it is not there."""
    try:
        data = api(repo, "GET", "contents/%s?ref=%s" % (path, ref), raw=True)
    except GitError as error:
        if error.status == 404:
            return None
        raise
    if len(data) > FILE_CAP:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def post_commit(repo, files, message, head, delete=(), ref=None):
    """One tree on top of `head`'s tree with `files` ({path: text}) and without `delete`, one commit and the move of the
    branch (made when there is no head). Raises GitError with the status of the step that failed in `.step`."""
    entries = [{"path": path, "mode": "100644", "type": "blob", "content": text} for path, text in sorted(files.items())]
    entries += [{"path": path, "mode": "100644", "type": "blob", "sha": None} for path in sorted(delete)]
    payload = {"tree": entries}
    if head:
        payload["base_tree"] = tree_of(repo, head)
    try:
        tree = api(repo, "POST", "git/trees", payload)["sha"]
        commit = api(repo, "POST", "git/commits", {"message": message, "tree": tree, "parents": [head] if head else []})["sha"]
    except (KeyError, TypeError):
        raise GitError(0, "no sha")
    try:
        if ref:   # a branch of its own (a proposal), made from the head and never moved afterwards
            api(repo, "POST", "git/refs", {"ref": "refs/" + ref, "sha": commit})
        elif head:
            api(repo, "PATCH", "git/refs/heads/%s" % BRANCH, {"sha": commit, "force": False})
        else:
            api(repo, "POST", "git/refs", {"ref": "refs/heads/%s" % BRANCH, "sha": commit})
    except GitError as error:
        error.step = "ref"
        raise
    return commit


def write_refusal(error):
    """The key of LINES for a GitError of a write."""
    step = getattr(error, "step", "")
    if step == "ref":
        if error.status == 422 and "fast forward" in error.text.lower():
            return "moved"
        if error.status == 422 and "already exists" in error.text.lower():
            return "moved"
        return "rule"
    return "push"


def commit_files(repo, files, message, delete_stale=True):
    """Commit `files` to the branch and return the new commit. `files` is {path: text}, or a function of the head that makes
    it: a head that moved is read again once, and the function is called again with the head that was accepted, so that the
    retry never lays down a snapshot made from a head that is gone (it would overwrite what another session just kept).
    Raises Refused."""
    for attempt in range(2):
        try:
            head = head_of(repo)
            made = files(head) if callable(files) else files
            stale = ()
            if head and delete_stale:
                present = listing(repo, tree_of(repo, head))
                stale = [p for p in present if re.fullmatch(r"content/[0-9][0-9]\.[a-z0-9-]+\.md", p) and p not in made]
            return post_commit(repo, made, message, head, stale)
        except GitError as error:
            kind = write_refusal(error)
            if kind == "moved" and attempt == 0:
                continue
            raise Refused(kind)
    raise Refused("moved")


# ---------------------------------------------------------------- what the record says

def load(need_mode=False):
    try:
        config = recortex.load_config()
    except (OSError, ValueError) as error:
        sys.exit("error: the configuration file cannot be read (%s)" % error)
    repo = config.get("repo")
    if not config.get("_path") or not recortex.valid_repo(repo):
        sys.exit("error: this project is not set up yet (no configuration file with a valid repo at %s)" % recortex.project_root())
    allowed = recortex.allowed_users(repo)
    if not allowed:
        sys.exit("error: %s is not in your trusted repositories (%s); nothing is read or written for it" % (
            repo, recortex.trusted_target()))
    mode = writing.read(repo)["mode"]
    if need_mode and mode == "off":
        sys.exit("versions: " + OFF_LINE)
    plan = recortex.plan_profile(recortex.shipped_profiles(config.get("profiles")))
    hub = config.get("hub") if type(config.get("hub")) is int and config.get("hub") >= 1 else None
    return config, repo, hub, plan, allowed, mode


def kept_here(repo):
    """True when this machine's mode is review or autonomous (a mode that cannot be read, or none chosen, is off)."""
    return writing.read(repo)["mode"] in ("review", "autonomous")


def open_index(repo):
    try:
        db = index.connect(repo, create=False)
    except Exception:  # noqa: BLE001 - refused storage is one fixed line
        return None
    return db if db is not None and index.mind_ready(db) else None


def keep_state(repo, **values):
    try:
        found = json.loads(storage.read_cached(repo, (storage.VERSIONS,), STATE_FILE) or "{}")
    except (storage.StorageError, OSError, ValueError):
        found = {}
    found = found if isinstance(found, dict) else {}
    found.update(values)
    try:
        storage.write_cached(repo, (storage.VERSIONS,), STATE_FILE, json.dumps(found, sort_keys=True))
    except (storage.StorageError, OSError):
        pass


def read_state(repo):
    try:
        found = json.loads(storage.read_cached(repo, (storage.VERSIONS,), STATE_FILE) or "{}")
    except (storage.StorageError, OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


LAST_REFUSAL = [None]   # the key of the refusal this run said last, for the caller that only needs its reason


def refuse(repo, kind):
    """Say the fixed line of a refusal once, keep its key for doctor and return the Version field's reason."""
    LAST_REFUSAL[0] = kind
    line = LINES[kind] % repo if "%s" in LINES[kind] else LINES[kind]
    say(line)
    keep_state(repo, refusal={"kind": kind, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")})
    return REASON.get(kind, "")


def shipped_front_matter():
    with open(os.path.join(TEMPLATES, "manubot-front-matter.md"), encoding="utf-8") as handle:
        return handle.read()


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hub_title(db, hub, allowed):
    row = db.execute("SELECT title FROM issues WHERE number = ?", (hub,)).fetchone() if hub else None
    return row["title"] if row and index.body_told(db, hub, allowed) and row["title"] else None


def told_texts(db, config, allowed, plan):
    """The told text changes, oldest first (write_rules.text_memories), from the index; [] without a plan."""
    state = index.planned(db, config, allowed, plan) if plan else None
    return write_rules.text_memories(db, state["found"]["items"]) if state else []


def vouched_text(repo, number, known, texts):
    """The last text of a topic that the record vouches for, when its file on the branch was changed by hand: the file of the
    newest told text change whose "after" is the hash VERSION.json holds, read at the commit that change names, and kept only
    when it has that hash; None when there is none. Nothing from the changed file is ever used."""
    for row in reversed(texts or []):
        if row["issue"] == number and row["after"] == known.get("sha256") and SHA40.fullmatch(row["version"] or ""):
            found = file_at(repo, row["version"], "topics/%d.md" % number)
            return found if found is not None and record_format.body_sha(found) == known.get("sha256") else None
    return None


def paper_bodies(db, config, repo, hub, allowed, plan, replace, head_versions=None, head=None, texts=None):
    """({issue: body}, {issue: entry}): the bodies the layout is made from, the paper's told topics, `replace` ({issue: text})
    winning; and the topics the snapshot leaves as the branch has them, with the entry VERSION.json holds for each.

    A topic whose file at `head` is not what VERSION.json says of it (somebody changed the file on the branch alone) is held:
    its file and its entry are left as they are, so that no commit puts the file back before the author decided (references/
    versions.md, "A file edited directly"), and nothing from the file reaches the layout, the manifest, the page or the
    compiled paper: its text in the layout is the last one the record vouches for (the live body when that has the hash
    VERSION.json holds, else the file of the text change that does, `vouched_text`), and it has none when there is none. A
    topic whose live body is not the text the branch holds for it (a hand edit that `sync` has not recorded) keeps the branch's
    text, so that one commit never records a change that has no memory yet."""
    profiles = recortex.shipped_profiles(config.get("profiles"))
    told = index.told_bodies(db, allowed)
    bodies, held = {}, {}
    for _, _, numbers in index.deliverable_order(db, hub, profiles):
        for number in numbers:
            if number in bodies:
                continue
            if number in replace:
                bodies[number] = replace[number]
                continue
            if number not in told:
                continue
            live = db.execute("SELECT body FROM issues WHERE number = ?", (number,)).fetchone()["body"] or ""
            known = ((head_versions or {}).get("topics") or {}).get(str(number))
            if head and known and isinstance(known, dict):
                kept = file_at(repo, head, "topics/%d.md" % number)
                if kept is not None and record_format.body_sha(kept) != known.get("sha256"):
                    held[number] = known
                    vouched = live if record_format.body_sha(live) == known.get("sha256") else vouched_text(
                        repo, number, known, texts)
                    if vouched is not None:
                        bodies[number] = vouched
                    continue
                if kept is not None and known.get("sha256") != record_format.body_sha(live):
                    bodies[number] = kept
                    continue
            bodies[number] = live
    return bodies, held


def layout_files(db, config, repo, hub, allowed, plan, bodies, metadata=None, texts=None, held=None, order=None):
    """{path: text} of the whole branch: topics/, content/ and VERSION.json. `held` ({issue: entry}) are
    the topics whose file and entry stay as the branch has them; `order` ([(label, heading, [issues])]) is the outline to
    lay out (a named version's own, from its VERSION.json), else the outline as the index has it now."""
    profiles = recortex.shipped_profiles(config.get("profiles"))
    titles = {row["number"]: row["title"] for row in db.execute("SELECT number, title FROM issues")}
    sections, outline = [], []
    for label, heading, numbers in order if order is not None else index.deliverable_order(db, hub, profiles):
        topics = [(n, titles.get(n, ""), record_format.section_text(bodies[n], heading) or "") for n in numbers if n in bodies]
        sections.append((heading, topics))
        outline.append({"label": label, "heading": heading, "topics": [n for n, _, _ in topics]})
    authors, keywords = metadata or ([], [])
    files, _ = paper_layout.layout(repo, hub, hub_title(db, hub, allowed) or repo.split("/")[-1], sections, authors, keywords,
                                   front_matter=shipped_front_matter())
    texts = texts if texts is not None else {}
    for number, body in sorted(bodies.items()):
        if number not in (held or {}):
            files["topics/%d.md" % number] = record_format.body_bytes(body).decode("utf-8")
    files["VERSION.json"] = version_json(repo, hub, bodies, texts, held, outline)
    return files


def version_json(repo, hub, bodies, texts, held=None, outline=None):
    """VERSION.json: per topic the body's SHA-256 and its newest text change (a held topic keeps the entry the branch has),
    and the outline the sections were laid out in, so that a named version is compiled in the sections it had."""
    held = held or {}
    found = {"repository": repo, "hub": hub, "topics": {
        str(n): held[n] if n in held else {"sha256": record_format.body_sha(bodies[n]), "text": texts.get(n)}
        for n in sorted(set(bodies) | set(held))}}
    if outline is not None:
        found["layout"] = outline
    return json.dumps(found, indent=1, sort_keys=True) + "\n"


def metadata_at(repo, ref):
    text = file_at(repo, ref, "content/metadata.yaml") if ref else None
    return paper_layout.read_metadata(text)


def version_at(repo, ref):
    """VERSION.json at a ref, or None when it is missing or not in the form this plugin writes: the branch is not trusted,
    and a file somebody changed there never stops a write or a sync."""
    text = file_at(repo, ref, "VERSION.json") if ref else None
    try:
        found = json.loads(text) if text else None
    except (ValueError, RecursionError):
        return None
    if not isinstance(found, dict) or not isinstance(found.get("topics"), dict) \
            or not all(isinstance(entry, dict) for entry in found["topics"].values()):
        return None
    return found


# ---------------------------------------------------------------- ensure and commit

def ensure(config, repo):
    """Make the branch when it is missing (with the first commit of the current text). Only with a mode of
    review or autonomous, only when the account can write, and never with force. Prints one line. Returns "off", "kept",
    "refused" or "made". A repository that is not in the trusted list is never written to, whoever calls: the check is here
    and not only in the commands that call this, because setup and the writers call it directly."""
    if not kept_here(repo) or not recortex.allowed_users(repo):
        return "off"
    if not can_push(repo):
        refuse(repo, "push")
        return "refused"
    db = open_index(repo)
    if db is None:
        say("versions: no local index yet, so the branch is made by the next run of versions.py sync.")
        return "refused"
    try:
        allowed = recortex.allowed_users(repo)
        plan = recortex.plan_profile(recortex.shipped_profiles(config.get("profiles")))
        hub = config.get("hub") if type(config.get("hub")) is int else None
        try:
            head = head_of(repo)
        except GitError:
            refuse(repo, "push")
            return "refused"
        if head is not None:
            return "kept"
        rows = told_texts(db, config, allowed, plan)
        texts = {row["issue"]: write_rules.newest_text(rows, row["issue"])["id"] for row in rows}

        def files(now):   # made again for the head a retry accepts: the branch may have been made meanwhile
            bodies, held = paper_bodies(db, config, repo, hub, allowed, plan, {}, version_at(repo, now) if now else None, now,
                                        rows)
            return layout_files(db, config, repo, hub, allowed, plan, bodies, metadata_at(repo, now) if now else None, texts,
                                held)
        try:
            commit_files(repo, files, "Start the versions of the manuscript")
        except Refused as error:
            refuse(repo, error.kind)
            return "refused"
        say("versions: recortex keeps every version of the manuscript in %s from now on." % repo)
        keep_state(repo, refusal=None)
        return "made"
    finally:
        db.close()


def commit(config, repo, issue, text):
    """The version commit of one topic's new text: the 40-character commit id, or one of record_format.NOT_KEPT (the reason is
    said in one fixed line first). Used by write.py and post_verified.py after the body was written. The mode is checked
    first: with the mode off or not chosen nothing is written and no branch is made, and a repository that is not in the
    trusted list is never written to."""
    if not kept_here(repo) or not recortex.allowed_users(repo):
        return REASON["off"]
    db = open_index(repo)
    if db is None:
        return REASON["off"]
    try:
        allowed = recortex.allowed_users(repo)
        plan = recortex.plan_profile(recortex.shipped_profiles(config.get("profiles")))
        hub = config.get("hub") if type(config.get("hub")) is int else None
        if not can_push(repo):
            return refuse(repo, "push")
        try:
            head = head_of(repo)
        except GitError:
            return refuse(repo, "push")
        if head is None:
            made = ensure(config, repo)
            if made != "made":
                return REASON.get(LAST_REFUSAL[0], REASON["push"]) if made == "refused" else REASON["off"]
            head = head_of(repo)
        rows = told_texts(db, config, allowed, plan)
        texts = {row["issue"]: write_rules.newest_text(rows, row["issue"])["id"] for row in rows}

        def files(now):   # made again for the head a retry accepts: another session may have kept a version meanwhile
            bodies, held = paper_bodies(db, config, repo, hub, allowed, plan, {issue: text}, version_at(repo, now), now, rows)
            return layout_files(db, config, repo, hub, allowed, plan, bodies, metadata_at(repo, now), texts, held)
        headings = changed_headings(repo, head, issue, text, config)
        message = "Text: %s of topic %d" % (", ".join(headings)[:60] or "text", issue)
        try:
            new = commit_files(repo, files, message)
        except Refused as error:
            return refuse(repo, error.kind)
        keep_state(repo, refusal=None, last_commit=new)
        return new
    finally:
        db.close()


# ---------------------------------------------------------------- reading the record

def told_items(db, config, allowed, plan):
    state = index.planned(db, config, allowed, plan) if plan else None
    return state, (state["found"]["items"] if state else {})


def named_versions(db, items):
    """[{"name", "commit", "manifest", "id", "time"}] from the told decisions whose Effect starts with the fixed line."""
    out = []
    for cid, item in items.items():
        if item["kind"] != "decision":
            continue
        found = record_format.version_name(index.event_field(db, cid, "Effect"))
        if found:
            out.append({"name": found[0], "commit": found[1], "manifest": found[2], "id": cid, "time": item["time"]})
    return sorted(out, key=lambda row: (row["time"], row["id"]))


def paper_topics(db, config, hub, allowed):
    """The told topics of the paper, in the outline's order."""
    profiles = recortex.shipped_profiles(config.get("profiles"))
    told = index.told_bodies(db, allowed)
    seen, out = set(), []
    for _, _, numbers in index.deliverable_order(db, hub, profiles):
        for number in numbers:
            if number in told and number not in seen:
                seen.add(number)
                out.append(number)
    return out


def live_body(db, number):
    return db.execute("SELECT body FROM issues WHERE number = ?", (number,)).fetchone()["body"] or ""


def resolve_version(repo, db, items, value):
    """A commit from a 40-hex id, HEAD, a name of the told decisions or the tag paper-<name>; None when it is none."""
    if value.upper() == "HEAD":
        return head_of(repo)
    if SHA40.fullmatch(value):
        return value
    if NAME.fullmatch(value):
        for row in reversed(named_versions(db, items)):
            if row["name"] == value:
                return row["commit"]
    return None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)


def stamp(moment, config=None):
    """The heading's time of a comment this script writes: `moment` in the zone the record states, else in UTC."""
    return recortex.stamp(moment, config)


def edited_stamp(when, moment, config=None):
    """The stamp of a hand edit: `when` is GitHub's time of the edit (ISO, UTC); `moment` is used when it cannot be read."""
    try:
        at = datetime.datetime.strptime((when or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        at = moment
    return stamp(at, config)


def post_comment(repo, issue, kind, text, folder):
    """Post a comment through post_verified.py (lint, dry-run-free, read back); returns its id or None."""
    import subprocess
    path = os.path.join(folder, "versions-comment-%s.md" % hashlib.sha256(text.encode("utf-8")).hexdigest()[:12])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    ran = subprocess.run([sys.executable, os.path.join(HERE, "post_verified.py"), "comment", "--repo", repo, "--issue", str(issue),
                          "--kind", kind, "--body-file", path], capture_output=True, text=True)
    found = re.search(r"^memory id: ([0-9]+)$", ran.stdout, re.M) or re.search(r"^posted: \S+#issuecomment-([0-9]+)$", ran.stdout, re.M)
    if ran.returncode != 0 or not found:
        say((ran.stdout.strip() or ran.stderr.strip())[:600])
        return None
    return int(found.group(1))


def scratch_folder(repo):
    return storage.cache_folder(repo, "versions-work")


# ---------------------------------------------------------------- status and sync

def compare(repo, db, config, hub, allowed):
    """{issue: state} for the paper's topics against the branch's head: "in step", "body changed" (the body is not the
    file's text), "file changed" (the file is not what was last committed while the body is), "both changed", "not kept"
    (the branch has no file for it); and the head, VERSION.json."""
    head = head_of(repo)
    if head is None:
        return None, None, {}
    versions = version_at(repo, head) or {}
    recorded = (versions.get("topics") or {}) if isinstance(versions.get("topics"), dict) else {}
    found = {}
    for number in paper_topics(db, config, hub, allowed):
        live = record_format.body_sha(live_body(db, number))
        known = recorded.get(str(number)) if isinstance(recorded.get(str(number)), dict) else None
        text = file_at(repo, head, "topics/%d.md" % number)
        if known is None or text is None:
            found[number] = "not kept"
            continue
        fileside = record_format.body_sha(text)
        was = known.get("sha256")
        found[number] = ("in step" if live == was and fileside == was else "body changed" if fileside == was or fileside == live
                         else "file changed" if live == was else "both changed")
    return head, versions, found


def status_command(args):
    config, repo, hub, plan, allowed, mode = load()
    print(recortex.describe(config, repo))
    if mode == "off":
        say("versions: " + OFF_LINE)
        return 0
    db = open_index(repo)
    if db is None:
        sys.exit("error: no usable local index yet; run index.py sync first")
    try:
        state, items = told_items(db, config, allowed, plan)
        try:
            head, versions, found = compare(repo, db, config, hub, allowed)
        except GitError:
            say("versions: GitHub could not be read just now.")
            return 1
        if head is None:
            say("versions: the branch does not exist yet; it is made when the next version is kept (setup.py writing set).")
            return 0
        titles = {n: db.execute("SELECT title FROM issues WHERE number = ?", (n,)).fetchone()["title"] for n in found}
        for number, how in sorted(found.items()):
            say("#%d %s: %s" % (number, re.sub(r"[^ -~]", "?", titles[number])[:60], {
                "in step": "in step with its file", "body changed": "the text changed since its last version (versions.py sync "
                "records it)", "file changed": "the file of this part was changed on the branch and the text was not; ask the "
                "author whether to take the change into the text or put the file back",
                "both changed": "the text and its file both changed; ask the author", "not kept": "no version of it is "
                "kept yet"}[how]))
        for row in named_versions(db, items):
            say("name %s: commit %s (decision %d)" % (row["name"], row["commit"][:12], row["id"]))
        return 0
    finally:
        db.close()


def put_back_command(args):
    """The file of a topic was changed on the branch and the text was not, and the author said to put the file back: a new
    commit of the body over the file. Needs the author's yes; nothing is read from the file."""
    config, repo, hub, plan, allowed, mode = load(need_mode=True)
    print(recortex.describe(config, repo))
    if not args.yes and not args.dry_run:
        sys.exit("error: pass --yes once the author said to put the file back")
    try:
        index.sync(repo, hub, timeout=60, deadline=300)
    except (recortex.GhError, index.SyncBusy) as error:
        sys.exit("error: the index could not be brought up to date (%s); nothing was written" % error)
    db = open_index(repo)
    if db is None:
        sys.exit("error: no usable local index yet; run index.py sync first")
    try:
        try:
            head, versions, found = compare(repo, db, config, hub, allowed)
        except GitError:
            sys.exit("versions: GitHub could not be read just now; nothing was written")
        if found.get(args.issue) != "file changed":
            sys.exit("versions: the file of #%d was not changed on its own, so there is nothing to put back" % args.issue)
        body = live_body(db, args.issue)
    finally:
        db.close()
    if args.dry_run:
        say("would run: a version commit of the live body of #%d over its file on the branch %s" % (args.issue, BRANCH))
        say("dry run: nothing was written")
        return 0
    result = commit(config, repo, args.issue, body)
    if not SHA40.fullmatch(result):
        return 1
    say("versions: the file of #%d was put back to the text (commit %s)." % (args.issue, result[:12]))
    return 0


def last_editor(db, number):
    row = db.execute("SELECT actor, at FROM timeline WHERE issue = ? AND event = 'edited' ORDER BY at DESC, seq DESC LIMIT 1",
                     (number,)).fetchone()
    return (row["actor"], row["at"]) if row else (None, None)


def sync_command(args):
    config, repo, hub, plan, allowed, mode = load(need_mode=True)
    dry = getattr(args, "dry_run", False)
    print(recortex.describe(config, repo))
    try:
        index.sync(repo, hub, timeout=60, deadline=300)
    except (recortex.GhError, index.SyncBusy) as error:
        sys.exit("error: the index could not be brought up to date (%s); nothing was written" % error)
    db = open_index(repo)
    if db is None:
        sys.exit("error: no usable local index yet; run index.py sync first")
    made = 0
    try:
        state, items = told_items(db, config, allowed, plan)
        texts = write_rules.text_memories(db, items) if state else []
        if head_of(repo) is None:
            if dry:
                say("would run: make the branch %s with a first commit of the current text (versions.ensure)" % BRANCH)
                say("dry run: nothing was written")
                return 0
            db.close()
            db = None
            result = ensure(config, repo)
            say("versions: sync %s." % ("made the branch" if result == "made" else "could not make the branch"))
            if result == "made":
                db = open_index(repo)
                made_head = head_of(repo)
                for number in paper_topics(db, config, hub, allowed) if db is not None and made_head else []:
                    newest = write_rules.newest_text(texts, number)
                    here = file_at(repo, made_head, "topics/%d.md" % number)
                    if newest is not None and not SHA40.fullmatch(newest["version"] or "") and here is not None \
                            and record_format.body_sha(here) == newest["after"]:
                        tell_kept_later(repo, number, newest, made_head, config)
            return 0 if result == "made" else 1
        head, versions, found = compare(repo, db, config, hub, allowed)
        for number, how in sorted(found.items()):
            if how == "in step":
                continue
            if how in ("file changed", "both changed"):
                say("versions: the file of #%d was changed on the branch; nothing is taken from it. Ask the author." % number)
                continue
            body = live_body(db, number)
            sha = record_format.body_sha(body)
            newest = write_rules.newest_text(texts, number)
            before = (versions.get("topics") or {}).get(str(number), {}).get("sha256")
            before = before if isinstance(before, str) and re.fullmatch(r"[0-9a-f]{64}", before) else None
            actor, when = last_editor(db, number)
            if dry:
                say("would run: the version commit of #%d to the branch %s, and one text change on #%d that says who edited it" % (
                    number, BRANCH, number))
                made += 1
                continue
            result = commit(config, repo, number, body)
            if not SHA40.fullmatch(result):
                return 1
            made += 1
            if newest is not None and newest["after"] == sha:
                # The text change already says why and by whom. Its Version field says "Not kept" and a comment is never
                # edited, so a new text change, in the form "kept", tells which commit holds the file now: without it the
                # version could never be fetched, shown or restored.
                if SHA40.fullmatch(newest["version"] or ""):
                    say("versions: #%d is now in step; its version was missing and its text change already says why." % number)
                else:
                    tell_kept_later(repo, number, newest, result, config)
                continue
            if actor is None or actor.lower() not in allowed:
                say("versions: #%d changed, and the account that edited it is not known here; the version was kept without a "
                    "text change." % number)
                continue
            headings = changed_headings(repo, head, number, body, config)
            moment = utc_now()
            memory = "\n".join([
                "<!-- ghim:event type=text at=%s -->" % recortex.marker_time(moment, config),
                "### Text: %s - %s" % (stamp(moment, config), "%s of #%d" % (", ".join(headings)[:60], number)), "",
                "**In plain words, we changed the text to say that:** the text of this part was edited by hand on GitHub; its "
                "new wording is the body of the topic", "",
                "**Section:** %s" % ", ".join(headings), "", "**What changed:** The author edited the text by hand.", "",
                "**Why:** Not recorded: the text was edited by hand.", "",
                "**Approved:** Edited by %s on GitHub, %s." % (actor, edited_stamp(when, moment, config)), "",
                "**Kept from the author:** Nothing was changed by hand.", "",
                "**Body:** before sha256 `%s`; after sha256 `%s`" % (before or "none", sha), "",
                "**Version:** `%s`" % result, ""])
            cid = post_comment(repo, number, "text", memory, scratch_folder(repo))
            say("versions: #%d recorded as a hand edit%s." % (number, " (comment %d)" % cid if cid else "; its comment was not posted"))
        if dry:
            say("dry run: sync would make %d version%s; nothing was written" % (made, "" if made == 1 else "s"))
            return 0
        say("versions: sync made %d version%s." % (made, "" if made == 1 else "s"))
        return 0
    finally:
        if db is not None:
            db.close()


def tell_kept_later(repo, number, newest, commit, config=None):
    """Post the text change that ties `commit` to the earlier text change `newest` (whose Version field says "Not kept")."""
    cid = post_comment(repo, number, "text", kept_later_text(newest, newest["after"], commit, utc_now(), config),
                       scratch_folder(repo))
    say("versions: #%d is now in step; its file was kept afterwards%s." % (
        number, " (comment %d)" % cid if cid else ", but the comment that says so was not posted"))


def kept_later_text(newest, sha, commit, moment, config=None):
    """The text change that ties the commit made later to the text change whose Version field says "Not kept": the same
    text (the same body hash before and after), the commit in its Version field, and the fixed form "Kept as a file later"."""
    return "\n".join([
        "<!-- ghim:event type=text at=%s -->" % recortex.marker_time(moment, config),
        "### Text: %s - %s" % (stamp(moment, config), "%s of #%d" % (", ".join(newest["sections"])[:60], newest["issue"])), "",
        "**In plain words, we changed the text to say that:** the text of this part is as the earlier change said; its file on the "
        "branch was kept afterwards", "",
        "**Section:** %s" % ", ".join(newest["sections"]), "", "**What changed:** Nothing; the file of the earlier text change was "
        "kept afterwards.", "", "**Why:** Not recorded: the file could not be kept when the text was written.", "",
        "**Approved:** Kept as a file later, for the text change of comment %d." % newest["id"], "",
        "**Kept from the author:** Nothing was changed by hand.", "",
        "**Body:** before sha256 `%s`; after sha256 `%s`" % (sha, sha), "", "**Version:** `%s`" % commit, ""])


def changed_headings(repo, head, number, body, config=None):
    """The headings of the sections whose text differs between the branch's file and the body, among the headings of the
    project's profiles (the Section field of a text change names only those); when none differs among them, the profile
    headings the body has."""
    profiles = recortex.shipped_profiles((config or {}).get("profiles")) if config is not None else None
    valid = [h for _, h in recortex.section_labels(profiles)] if profiles else []
    old = file_at(repo, head, "topics/%d.md" % number) if head else ""
    before = {s["heading"]: s["text"].strip() for s in record_format.sections(old or "")}
    after = {s["heading"]: s["text"].strip() for s in record_format.sections(body)}
    found = [h for h in after if before.get(h) != after[h]] + [h for h in before if h not in after]
    if valid:
        found = [h for h in found if h in valid] or [h for h in after if h in valid]
    return found or ["Results"]


# ---------------------------------------------------------------- log and diff

HOW = {"autonomous": "written by the plugin without asking", "chat": "written by the plugin with your approval",
       "merged": "approved by you on GitHub", "edited": "edited by you by hand", "restored": "restored by you",
       "kept": "kept as a file afterwards", None: "recorded"}


def log_rows(db, items, texts, issue=None):
    names_by_commit = {}
    for row in named_versions(db, items):
        names_by_commit.setdefault(row["commit"], []).append(row["name"])
    rows = []
    for row in reversed(texts):
        if issue is not None and row["issue"] != issue:
            continue
        rows.append({"id": row["id"], "issue": row["issue"], "day": (row["at"] or row["time"] or "")[:10],
                     "how": HOW.get(row["form"], "recorded"), "sections": row["sections"], "plain": row["plain"],
                     "commit": row["version"], "names": names_by_commit.get(row["version"], [])})
    return rows


def log_command(args):
    config, repo, hub, plan, allowed, mode = load()
    if not args.json:
        print(recortex.describe(config, repo))
    db = open_index(repo)
    if db is None:
        sys.exit("error: no usable local index yet; run index.py sync first")
    try:
        state, items = told_items(db, config, allowed, plan)
        texts = write_rules.text_memories(db, items) if state else []
        rows = log_rows(db, items, texts, args.issue)
    finally:
        db.close()
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for row in rows:
        say("%d  %s  #%d  %s  %s  %s%s" % (row["id"], row["day"], row["issue"], ", ".join(row["sections"]), row["how"],
                                            re.sub(r"[^ -~]", "?", row["plain"])[:100],
                                            "  [%s]" % ", ".join(row["names"]) if row["names"] else ""))
    if not rows:
        say("versions: no text change is recorded%s." % (" for #%d" % args.issue if args.issue else ""))
    return 0


def diff_command(args):
    config, repo, hub, plan, allowed, mode = load()
    print(recortex.describe(config, repo))
    db = open_index(repo)
    if db is None:
        sys.exit("error: no usable local index yet; run index.py sync first")
    try:
        state, items = told_items(db, config, allowed, plan)
        texts = write_rules.text_memories(db, items) if state else []
        try:
            first, second = (resolve_version(repo, db, items, v) for v in (args.a, args.b))
        except GitError:
            sys.exit("versions: GitHub could not be read just now.")
        if first is None or second is None:
            sys.exit("error: a version is a 40-character commit id, HEAD or a name of this paper")
        numbers = [args.issue] if args.issue else paper_topics(db, config, hub, allowed)
        shown = 0
        for number in numbers:
            before, after = (file_at(repo, c, "topics/%d.md" % number) for c in (first, second))
            if before is None and after is None or before == after:
                continue
            shown += 1
            say("\n".join(difflib.unified_diff((before or "").split("\n"), (after or "").split("\n"),
                                              "#%d at %s" % (number, first[:12]), "#%d at %s" % (number, second[:12]), lineterm="")))
            times = {row["version"]: row["time"] for row in texts if row["issue"] == number and row["version"]}
            low, high = times.get(first, ""), times.get(second, "9999")
            for row in texts:
                if row["issue"] == number and low < row["time"] <= high:
                    say("text change %d (%s): %s" % (row["id"], HOW.get(row["form"], "recorded"),
                                                    re.sub(r"[^ -~]", "?", row["plain"])[:100]))
        if not shown:
            say("versions: no difference%s." % (" in #%d" % args.issue if args.issue else ""))
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------- names

def decision_text(name, commit, manifest, words, moment, issue_title, config=None):
    quoted = "\n".join(("> " + line) if line.strip() else ">" for line in re.sub(r"[ \t]+", " ", words).strip().split("\n"))
    return "\n".join([
        "<!-- ghim:event type=decision at=%s -->" % recortex.marker_time(moment, config),
        "### Decision: %s - name this version of the paper %s" % (stamp(moment, config), name), "",
        "**In plain words, we decided to:** call this version of the paper %s" % name, "",
        "**Decision:** The paper as it stands is named %s." % name, "",
        "**Decided by:** the author, %s" % stamp(moment, config), "",
        "**Exact words:**", "", quoted, "",
        "**Where recorded:** chat", "",
        "**Effect:** Version name: %s; commit %s; manifest sha256 %s" % (name, commit, manifest), "",
        "**Next step:** Keep writing, owner the session", ""])


def name_command(args):
    config, repo, hub, plan, allowed, mode = load(need_mode=True)
    print(recortex.describe(config, repo))
    if not NAME.fullmatch(args.name):
        sys.exit("error: a name is 1 to 40 characters of a-z, 0-9, . and -, starting with a letter or a digit")
    if not args.words_file or not os.path.isfile(args.words_file) or not recortex.read_text(args.words_file).strip():
        sys.exit("error: the author's words are needed: pass --words-file with their answer in the chat")
    words = recortex.read_text(args.words_file).strip()
    if len(words) > 600:
        sys.exit("error: the words are longer than 600 characters; give the author's answer as they wrote it, shortened")
    try:
        index.sync(repo, hub, timeout=60, deadline=300)
    except (recortex.GhError, index.SyncBusy) as error:
        sys.exit("error: the index could not be brought up to date (%s); nothing was written" % error)
    db = open_index(repo)
    if db is None:
        sys.exit("error: no usable local index yet; run index.py sync first")
    try:
        state, items = told_items(db, config, allowed, plan)
        try:
            head, versions, found = compare(repo, db, config, hub, allowed)
            tag_taken = head_of(repo, "tags/paper-%s" % args.name) is not None
        except GitError:
            sys.exit("versions: GitHub could not be read just now; nothing was written")
        if head is None:
            sys.exit("versions: there is no branch of versions yet, so there is nothing to name")
        if any(row["name"] == args.name for row in named_versions(db, items)) or tag_taken:
            sys.exit("error: the name %s is taken; a name is never moved or reused" % args.name)
        differing = sorted(number for number, how in found.items() if how != "in step")
        if differing:
            sys.exit("versions: the text and its files differ for %s; run versions.py sync first (and ask the author about a "
                     "file that was changed on the branch)" % ", ".join("#%d" % n for n in differing))
        manifest = text_sha(file_at(repo, head, "VERSION.json") or "")
        target = index.plan_issue_of(db, config, plan, allowed) or hub
        if target is None:
            sys.exit("error: the project file has no hub or plan issue to hold the decision")
        title = db.execute("SELECT title FROM issues WHERE number = ?", (target,)).fetchone()["title"]
    finally:
        db.close()
    memory = decision_text(args.name, head, manifest, words, utc_now(), title, config)
    if getattr(args, "dry_run", False):
        say("would run: " + recortex.show_command(["issue", "comment", str(target), "--repo", repo, "--body-file", "-"],
                                                  "the decision that names %s, %d characters" % (args.name, len(memory))))
        say("would run: gh api -X POST repos/%s/git/refs   # refs/tags/paper-%s at %s" % (repo, args.name, head[:12]))
        say("dry run: every check passed; nothing was written")
        return 0
    cid = post_comment(repo, target, "decision", memory, scratch_folder(repo))
    if cid is None:
        say("versions: the decision was not posted, so the version was not named")
        return 1
    try:
        api(repo, "POST", "git/refs", {"ref": "refs/tags/paper-%s" % args.name, "sha": head})
    except GitError:
        say("versions: the decision %d names the version, but the tag paper-%s could not be made (the account may not write "
            "tags here); the name still works for compile.py and the page" % (cid, args.name))
        return 1
    say("versions: named %s (decision %d, tag paper-%s). mind.py --paper --version %s writes that version as a page." % (
        args.name, cid, args.name, args.name))
    return 0


# ---------------------------------------------------------------- a named version, for compile.py

class NotVouched(Exception):
    """A named version that the record does not vouch for: the message is one fixed line."""


def vouched_version(config, repo, db, allowed, plan, name):
    """What compile.py --version needs, after every check: (bodies {issue: text}, decision, authors, keywords, layout). The name must
    be one that a told decision of an allowed account carries in the fixed line of its Effect; the tag paper-<name> must
    point at the commit that decision names; VERSION.json at that commit must have the sha256 the decision names; and each
    topics/<issue>.md at that commit must have the hash VERSION.json holds. No body is read from the index."""
    if not NAME.fullmatch(name or ""):
        raise NotVouched("compile: a name is 1 to 40 characters of a-z, 0-9, . and -.")
    state, items = told_items(db, config, allowed, plan)
    found = [row for row in named_versions(db, items) if row["name"] == name]
    if not found:
        raise NotVouched("compile: no decision of an allowed account names a version called %s." % name)
    decision = found[0]
    try:
        if head_of(repo, "tags/paper-%s" % name) != decision["commit"]:
            raise NotVouched("compile: the tag paper-%s does not point at the commit its decision names." % name)
        manifest = file_at(repo, decision["commit"], "VERSION.json")
        if manifest is None or text_sha(manifest) != decision["manifest"]:
            raise NotVouched("compile: VERSION.json at that commit is not the one its decision names.")
        try:
            recorded = json.loads(manifest)
            topics = recorded["topics"]
            ok = isinstance(topics, dict) and recorded.get("repository", "").lower() == repo.lower()
        except (ValueError, KeyError, AttributeError, TypeError):
            ok = False
        if not ok:
            raise NotVouched("compile: VERSION.json at that commit is not in the form of this plugin.")
        bodies = {}
        for key, entry in sorted(topics.items(), key=lambda row: int(row[0]) if row[0].isdigit() else 0):
            if not re.fullmatch(r"[1-9][0-9]{0,9}", key) or not isinstance(entry, dict) or not re.fullmatch(
                    r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                raise NotVouched("compile: VERSION.json at that commit is not in the form of this plugin.")
            text = file_at(repo, decision["commit"], "topics/%s.md" % key)
            if text is None or record_format.body_sha(text) != entry["sha256"]:
                raise NotVouched("compile: the file of #%s at that commit is not the text VERSION.json says." % key)
            bodies[int(key)] = text
        authors, keywords = metadata_at(repo, decision["commit"])
    except GitError:
        raise NotVouched("compile: GitHub could not be read just now; nothing was written.")
    return bodies, decision, authors, keywords, recorded_layout(recorded.get("layout"), set(bodies))


def recorded_layout(value, topics):
    """The outline a version was laid out in, [(label, heading, [issues])], from the "layout" of its VERSION.json; None when
    it has none (a version kept before the layout was recorded) or when it is not in the form this plugin writes: each section
    names topics of the version only, each at most once (a topic may stand in several sections)."""
    if not isinstance(value, list) or len(value) > 60:
        return None
    out, seen = [], []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"label", "heading", "topics"} or not isinstance(row["label"], str) \
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_. -]{0,59}", row["label"]) or not isinstance(row["heading"], str) \
                or not re.fullmatch(r"[^\x00-\x1f\x7f]{1,80}", row["heading"]) or not isinstance(row["topics"], list) \
                or not all(type(n) is int and n in topics for n in row["topics"]):
            return None
        if len(set(row["topics"])) != len(row["topics"]):
            return None
        out.append((row["label"], row["heading"], list(row["topics"])))
    return out


# ---------------------------------------------------------------- doctor, metadata, commit

# ---------------------------------------------------------------- restore

def put_sections(live, version_body, headings):
    """`live` with the text of the sections named in `headings` taken from `version_body`; every other line of `live` stays as
    it is. A heading the live body lacks is added before "Sources" (or at the end)."""
    lines = live.replace("\r\n", "\n").split("\n")
    found = record_format.sections(live)
    wanted = {h.lower() for h in headings}
    theirs = {s["heading"].lower(): s["text"] for s in record_format.sections(version_body) if s["heading"].lower() in wanted}
    out = lines[:found[0]["line"] - 1] if found else list(lines)
    seen = set()
    for position, section in enumerate(found):
        start = section["line"] - 1
        stop = found[position + 1]["line"] - 1 if position + 1 < len(found) else len(lines)
        key = section["heading"].lower()
        if key in theirs:
            seen.add(key)
            out += [lines[start], ""] + theirs[key].split("\n") + [""]
        else:
            if key == recortex.SOURCES_HEADING.lower() and set(theirs) - seen:
                for missing in [h for h in headings if h.lower() in theirs and h.lower() not in seen]:
                    seen.add(missing.lower())
                    out += ["## " + missing, ""] + theirs[missing.lower()].split("\n") + [""]
            out += lines[start:stop]
    for missing in [h for h in headings if h.lower() in theirs and h.lower() not in seen]:
        out += ["", "## " + missing, ""] + theirs[missing.lower()].split("\n")
    return "\n".join(out).rstrip("\n") + "\n"


def restore_command(args):
    import secrets
    import write  # noqa: E402 - the folder is the one write.py start makes, and check and apply take it from there
    config, repo, hub, plan, allowed, mode = load(need_mode=True)
    print(recortex.describe(config, repo))
    if plan is None:
        sys.exit("versions: none of this project's profiles has a plan, so there is nothing to restore")
    if os.path.exists(os.path.join(args.out, "base.md")):
        sys.exit("error: %s already holds a base.md; use a new folder" % args.out)
    try:
        index.sync(repo, hub, timeout=60, deadline=300)
    except (recortex.GhError, index.SyncBusy) as error:
        sys.exit("error: the index could not be brought up to date (%s); nothing was done" % error)
    db = open_index(repo)
    if db is None:
        sys.exit("error: no usable local index yet; run index.py sync first")
    try:
        state, items = told_items(db, config, allowed, plan)
        texts = write_rules.text_memories(db, items) if state else []
        try:
            commit = resolve_version(repo, db, items, args.to)
            text = file_at(repo, commit, "topics/%d.md" % args.issue) if commit else None
        except GitError:
            sys.exit("versions: GitHub could not be read just now; nothing was done")
        if commit is None or text is None or not any(
                row["issue"] == args.issue and row["after"] == record_format.body_sha(text) for row in texts):
            sys.exit("versions: that file is not a version the record holds, so it is not restored; copy it by hand if it is yours.")
        if not index.body_told(db, args.issue, allowed):
            sys.exit("versions: " + WRITE_UNTOLD)
        vouched = next(row for row in texts if row["issue"] == args.issue and row["after"] == record_format.body_sha(text))
        headings = [h for h in ([args.section] if args.section else plan["write"]) if h]
        have = {s["heading"].lower() for s in record_format.sections(text)}
        headings = [h for h in headings if h.lower() in have]
        if not headings or (args.section and args.section.lower() not in {h.lower() for h in plan["write"]}):
            sys.exit("versions: that section is not one the plugin writes or the version has no such section")
        live = recortex.read_issue_body(repo, args.issue)
        draft = put_sections(live, text, headings)
        if recortex.normalize(draft) == recortex.normalize(live):
            sys.exit("versions: the text already says what that version said; nothing to restore")
        newest, last = write.last_written(repo, args.issue, texts)
        # The author asks, in the chat and in their own words, for these sections to say what they said: the paragraphs the
        # restore replaces are theirs to give up, and every other paragraph of theirs stays protected.
        mine = {h.lower() for h in headings}
        given = {p for key, (_, section) in write.sections_of(live)[0].items() if key in mine for p in write.paragraphs(section)}
        protected = [p for p in write.protected_paragraphs(live, newest, last) if p not in given]
    finally:
        db.close()
    day = (vouched["at"] or vouched["time"] or "")[:10]
    nonce = secrets.token_hex(16)
    plain = "the text of %s is back to what it said on %s" % (", ".join(headings), day)
    recortex.write_working_files(args.out, {   # no name is written through a link
        "base.md": live, "draft.md": draft,
        "used.json": json.dumps({"parts": [], "results": [], "decisions": []}),
        "memory.json": json.dumps({"plain": plain[:200], "changed": "The text of %s was restored from the version of %s." % (
            ", ".join(headings), day), "why": "The author asked for the earlier text back."}),
        "start.json": json.dumps({"issue": args.issue, "nonce": nonce})})
    write.write_record(repo, {"form": 1, "issue": args.issue, "base": record_format.body_sha(live), "nonce": nonce,
                              "protected": protected, "state": {},
                              "restore": {"after": record_format.body_sha(text), "headings": headings, "text": text,
                                          "day": day}})
    say("restore: the version of %s is prepared for %s of #%d in %s (%d paragraph%s of the author's are protected). Ask the author, "
        "then run write.py check --dir %s and write.py apply --dir %s --words-file F." % (
            day, ", ".join(headings), args.issue, args.out, len(protected), "" if len(protected) == 1 else "s", args.out, args.out))
    return 0


WRITE_UNTOLD = "someone outside your allowed list changed this text, so recortex does not write it; ask the user"


# ---------------------------------------------------------------- fetch

FETCH_BYTES = 300 * 1024
FETCH_FILES = 200
FETCH_TOTAL = 20 * 1024 * 1024
FETCH_SECONDS = 60


def cached_version(repo, commit, issue):
    """The text of topics/<issue>.md at a commit kept on this machine, or None. The caller checks its hash."""
    if not SHA40.fullmatch(commit or ""):
        return None
    try:
        return storage.read_cached(repo, (storage.VERSIONS, commit), "%d.md" % issue)
    except (storage.StorageError, OSError, UnicodeDecodeError):
        return None


def fetch(config, repo, issue=None, clock=None):
    """Bring the vouched versions' files into the private cache, newest first, inside the budgets. Only the file topics/<issue>.md at a commit that a told text change of that issue names is asked for,
    and a file is kept only when its hash is that change's "after" value. Returns {"fetched", "cached", "left", "failed"}."""
    import time
    clock = clock or time.monotonic
    started = clock()
    counts = {"fetched": 0, "cached": 0, "left": 0, "failed": 0}
    if not kept_here(repo):
        return counts
    db = open_index(repo)
    if db is None:
        return counts
    try:
        allowed = recortex.allowed_users(repo)
        plan = recortex.plan_profile(recortex.shipped_profiles(config.get("profiles")))
        state, items = told_items(db, config, allowed, plan)
        texts = write_rules.text_memories(db, items) if state else []
        names_now = named_versions(db, items)
    finally:
        db.close()
    total = 0
    for row in reversed(texts):
        if issue is not None and row["issue"] != issue:
            continue
        if not SHA40.fullmatch(row["version"] or "") or not row["after"]:
            continue
        have = cached_version(repo, row["version"], row["issue"])
        if have is not None:
            counts["cached"] += 1
            continue
        if counts["fetched"] >= FETCH_FILES or total >= FETCH_TOTAL or clock() - started > FETCH_SECONDS:
            counts["left"] += 1
            continue
        try:
            data = api(repo, "GET", "contents/topics/%d.md?ref=%s" % (row["issue"], row["version"]), raw=True)
            if len(data) > FETCH_BYTES:
                counts["failed"] += 1
                continue
            text = data.decode("utf-8")
        except (GitError, UnicodeDecodeError):
            counts["failed"] += 1
            continue
        if record_format.body_sha(text) != row["after"]:
            counts["failed"] += 1
            continue
        try:
            storage.write_cached(repo, (storage.VERSIONS, row["version"]), "%d.md" % row["issue"], text)
        except (storage.StorageError, OSError):
            counts["failed"] += 1
            continue
        counts["fetched"] += 1
        total += len(data)
    if counts["left"] or counts["failed"]:
        say("versions: %d version%s not fetched (%d past the budget, %d failed); build the page again with --sync." % (
            counts["left"] + counts["failed"], " was" if counts["left"] + counts["failed"] == 1 else "s were", counts["left"], counts["failed"]))
    return counts


def fetch_command(args):
    config, repo, hub, plan, allowed, mode = load()
    print(recortex.describe(config, repo))
    counts = fetch(config, repo, args.issue)
    say("versions: fetched %(fetched)d, already kept %(cached)d." % counts)
    return 0


def doctor_command(args):
    config, repo, hub, plan, allowed, mode = load()
    print(recortex.describe(config, repo))
    found = writing.read(repo)
    say("mode: %s" % (found["mode"] if found["state"] == "chosen" else "not chosen" if found["state"] == "none" else "unreadable"))
    if found["mode"] == "off":
        say("versions: " + OFF_LINE)
        return 0
    say("permission: %s" % ("this account may write files to %s" % repo if can_push(repo) else
                           "this account may NOT write files to %s" % repo))
    try:
        head = head_of(repo)
        say("branch: %s" % ("%s at %s" % (BRANCH, head[:12]) if head else "%s does not exist" % BRANCH))
    except GitError:
        say("branch: GitHub could not be read just now")
    state = read_state(repo)
    refusal = state.get("refusal") if isinstance(state.get("refusal"), dict) else None
    if refusal and refusal.get("kind") in LINES and re.fullmatch(r"[0-9T:Z-]{1,20}", str(refusal.get("at", ""))):
        say("last refusal (%s): %s" % (refusal["at"], LINES[refusal["kind"]] % repo if "%s" in LINES[refusal["kind"]] else
                                      LINES[refusal["kind"]]))
    else:
        say("last refusal: none")
    return 0


def meta_command(args):
    config, repo, hub, plan, allowed, mode = load(need_mode=True)
    if not args.yes and not args.dry_run:
        sys.exit("error: pass --yes once the author gave the names or keywords in the conversation")
    head = head_of(repo)
    if head is None:
        sys.exit("versions: there is no branch of versions yet")
    authors, keywords = metadata_at(repo, head)
    if args.authors is not None:
        authors = [a.strip() for a in args.authors.split(";") if a.strip()][:50]
    if args.keywords is not None:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()][:30]
    db = open_index(repo)
    try:
        title = (hub_title(db, hub, allowed) if db is not None else None) or repo.split("/")[-1]
    finally:
        if db is not None:
            db.close()
    if args.dry_run:
        say("would run: a commit of content/metadata.yaml to the branch %s (%d author%s, %d keyword%s)" % (
            BRANCH, len(authors), "" if len(authors) == 1 else "s", len(keywords), "" if len(keywords) == 1 else "s"))
        say("dry run: nothing was written")
        return 0
    try:
        commit_files(repo, {"content/metadata.yaml": paper_layout.metadata(title, authors, keywords)}, "Set the authors and keywords",
                     delete_stale=False)
    except Refused as error:
        refuse(repo, error.kind)
        return 1
    say("versions: the paper's authors and keywords are set.")
    return 0


def commit_command(args):
    config, repo, hub, plan, allowed, mode = load()
    text = recortex.read_text(args.body_file)
    if recortex.normalize(text) != recortex.normalize(recortex.read_issue_body(repo, args.issue)):
        sys.exit("versions: the file is not the live body of #%d; this command keeps the version of the text that stands, so "
                 "write the text first" % args.issue)
    if args.dry_run:
        say("would run: the version commit of #%d to the branch %s (%d characters)" % (args.issue, BRANCH, len(text)))
        say("dry run: nothing was written")
        return 0
    result = commit(config, repo, args.issue, text)
    say(result if SHA40.fullmatch(result) else "Not kept: " + result)
    return 0 if SHA40.fullmatch(result) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(run=status_command)
    p = sub.add_parser("sync")
    p.add_argument("--dry-run", action="store_true", help="say what would be committed and posted; write nothing")
    p.set_defaults(run=sync_command)
    p = sub.add_parser("log")
    p.add_argument("--issue", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=log_command)
    p = sub.add_parser("diff")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--issue", type=int)
    p.set_defaults(run=diff_command)
    p = sub.add_parser("name")
    p.add_argument("name")
    p.add_argument("--words-file")
    p.add_argument("--dry-run", action="store_true", help="run every check and say what would be written; write nothing")
    p.set_defaults(run=name_command)
    sub.add_parser("doctor").set_defaults(run=doctor_command)
    p = sub.add_parser("put-back", help="after a file was changed on the branch alone: commit the text over it (needs --yes)")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="run every check and say what would be written; write nothing")
    p.set_defaults(run=put_back_command)
    p = sub.add_parser("restore")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--to", required=True, help="a commit or a name of this paper")
    p.add_argument("--section")
    p.add_argument("--out", required=True)
    p.set_defaults(run=restore_command)
    p = sub.add_parser("fetch")
    p.add_argument("--issue", type=int)
    p.set_defaults(run=fetch_command)
    p = sub.add_parser("meta")
    p.add_argument("--authors")
    p.add_argument("--keywords")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="run every check and say what would be written; write nothing")
    p.set_defaults(run=meta_command)
    p = sub.add_parser("commit", help="make the version commit of a topic's new text (used by the other scripts)")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--dry-run", action="store_true", help="run every check and say what would be written; write nothing")
    p.set_defaults(run=commit_command)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (recortex.GhError, storage.StorageError, index.SyncBusy) as error:
        sys.exit("error: %s" % error)

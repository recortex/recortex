"""The local record: the record kept as Markdown files in a folder, behind the interface of record_store.py.

The layout, the file grammar and the seal are in references/local-record.md. In short: `record.json`, `hub.md` and its `hub/` folder
of entries, `topics/<slug>.md` and `topics/<slug>/` for each topic, every file with front matter, a body and a last line that seals
them under the key of this machine. A file whose seal holds is the user's own; a file whose seal fails (`changed`), or that was
sealed under another key (`foreign`), is untrusted text; a file that does not fit the grammar is not read at all (`unread`). No
method of this class edits or removes an entry: an entry is made once and never opened for writing again.

Every file of the record is opened, listed, made and moved by storage.py; this module calls none of those functions' system
calls itself (a test scans for that), and nothing of a file is put in a message. Standard library only."""

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import time

import names
import record_store
import recortex
import storage

FORM = 1
RECORD_FILE = "record.json"
HUB_FILE = "hub.md"
HUB_FOLDER = "hub"
TOPICS_FOLDER = "topics"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
ENTRY_ID = re.compile(r"[a-z2-7]{16}")
ENTRY_NAME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}Z)-([a-z2-7]{16})\.md")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
SEAL_LINE = re.compile(r"<!-- recortex:seal v1 key=([0-9a-f]{16}) mac=([0-9a-f]{64}) -->")
NUMBER_MAX = 999999999
FRONT_LIMIT = 4096
FILE_LIMIT = recortex.BODY_LIMIT * 4 + FRONT_LIMIT + 512
RECORD_LIMIT = 65536
LABELS_MAX = 50
TITLE_MAX = 256
YOUNG_SECONDS = 2        # a file with no seal that is this young may be one that is still being written
UNREADABLE_TOPIC = ("recortex: a topic file of the record could not be read, so no new topic was made. hooks.py --doctor "
                    "names it.")
NOT_FIT = "recortex: the text does not fit a record file; nothing was written."
DUPLICATE = ("recortex: two files of the record carry the same number, as after two copies of the record were changed apart. "
             "Nothing was read. hooks.py --doctor names them.")
SLUG_TRIES = 50
RECORD_JSON_REFUSED = "recortex: the record folder's record.json does not pass its check; nothing was read."
OWN_AUTHOR = record_store.LOCAL_OWNER
OTHER_AUTHOR = "other-machine"
STATES = ("sealed", "changed", "foreign")

FRONT_ORDER = {
    "hub": ["recortex", "kind", "number", "title", "labels", "state", "created", "updated"],
    "topic": ["recortex", "kind", "number", "title", "labels", "state", "created", "updated"],
    "entry": ["recortex", "kind", "id", "topic", "created"],
    "body": ["recortex", "kind", "id", "topic", "created"],
}

SHOW_WORDS = {
    "post_entry": "add an entry to #%s", "edit_body": "replace the text of #%s", "create_issue": "make a topic",
    "set_labels": "change the labels of #%s", "close": "close #%s", "reopen": "reopen #%s", "pin": "mark #%s as the hub",
    "create_label": "make a label", "edit_label": "change a label", "list_entries": "read the entries of #%s",
}
PLACEHOLDER = re.compile(r"<number of NEW-[a-z0-9]+(?:-[a-z0-9]+)*>")


class Unread(Exception):
    """A file that does not fit the grammar: not read, counted, never echoed. `reason` is a fixed word."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Refused(record_store.StoreError):
    """A write that the store will not make; the message is one fixed line."""


def now():
    return time.time()


def stamp(moment=None):
    return datetime.datetime.fromtimestamp(now() if moment is None else moment, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def name_stamp(created):
    """The file-name form of a stamp: 2026-11-23T14:12:07Z -> 2026-11-23T1412Z."""
    return created[:13] + created[14:16] + "Z"


def entry_number(entry_id):
    """The number of an entry: 52 bits of the SHA-256 of its id, at most 16 digits (a browser's numbers and SQLite hold it)."""
    return int(hashlib.sha256(entry_id.encode("ascii")).hexdigest()[:13], 16)


def new_id():
    return base64.b32encode(os.urandom(10)).decode("ascii").lower()


# ---------------------------------------------------------------- the seal

class Sealing:
    """What seals and checks the files of this machine: HMAC(user key, "recortex local record v1") is the sealing key, so that
    the user key itself never signs a record; the key id is the first 16 hex digits of a hash of it."""

    def __init__(self, user_key):
        self.key = hmac.new(user_key, b"recortex local record v1", hashlib.sha256).digest()
        self.key_id = hashlib.sha256(b"recortex seal key id\n" + self.key).hexdigest()[:16]

    def mac(self, record_id, relpath, signed):
        message = record_id.encode("ascii") + b"\n" + relpath.encode("utf-8") + b"\n" + signed
        return hmac.new(self.key, message, hashlib.sha256).hexdigest()

    def line(self, record_id, relpath, signed):
        return "<!-- recortex:seal v1 key=%s mac=%s -->" % (self.key_id, self.mac(record_id, relpath, signed))

    def state(self, record_id, relpath, signed, key_id, mac):
        if key_id != self.key_id:
            return "foreign"
        return "sealed" if hmac.compare_digest(mac, self.mac(record_id, relpath, signed)) else "changed"

    def json_mac(self, value):
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hmac.new(self.key, b"record.json\n" + data, hashlib.sha256).hexdigest()


_SEALINGS = {}


def sealing():
    key = storage.user_key()
    found = _SEALINGS.get(key)
    if found is None:
        found = _SEALINGS[key] = Sealing(key)
    return found


# ---------------------------------------------------------------- the file grammar

class Parsed:
    """One file of the record, read: its kind, front matter (checked values), body, relative path and the state of its seal."""

    def __init__(self, kind, front, body, relpath, state, key_id, mac):
        self.kind, self.front, self.body, self.relpath = kind, front, body, relpath
        self.state, self.key_id, self.mac = state, key_id, mac
        self.own_seal = state == "sealed"   # the file's own seal holds: its front matter is what was sealed
        self.placed = front.get("topic")    # the topic an entry or a version is placed under (see attributed)

    @property
    def number(self):
        return self.front.get("number")

    @property
    def entry_id(self):
        return self.front.get("id")


def front_block(data):
    """The bytes of the front matter of a file that begins with `---`: up to the line that closes it, or, when it does not close
    within FRONT_LIMIT bytes (a copy that stopped half way), the first FRONT_LIMIT bytes. None for any other file."""
    if not data.startswith(b"---\n"):
        return None
    end = data.find(b"\n---\n", 3, FRONT_LIMIT + 8)
    return data[4:FRONT_LIMIT] if end < 0 else data[4:end]


def keys_for(kind, front):
    """The keys of a file's front matter, in their order; a closed topic has its reason and time before `created`."""
    keys = list(FRONT_ORDER[kind])
    if kind in ("hub", "topic") and front.get("state") == "closed":
        keys[keys.index("created"):keys.index("created")] = ["reason", "closed"]
    return keys


def _value(key, text):
    """The checked value of one front matter line, or raises Unread("front")."""
    try:
        if key == "recortex":
            if text != "1":
                raise ValueError
            return 1
        if key == "kind":
            if text not in FRONT_ORDER:
                raise ValueError
            return text
        if key in ("number", "topic"):
            if not re.fullmatch(r"[1-9][0-9]{0,8}", text):
                raise ValueError
            return int(text)
        if key == "id":
            if not ENTRY_ID.fullmatch(text):
                raise ValueError
            return text
        if key == "title":
            value = json.loads(text)
            if not isinstance(value, str) or not value.strip() or len(value) > TITLE_MAX or any(
                    ord(char) < 32 or ord(char) == 127 or char in "\u2028\u2029" for char in value):
                raise ValueError
            return value
        if key == "labels":
            value = json.loads(text)
            if not isinstance(value, list) or len(value) > LABELS_MAX or not all(
                    isinstance(item, str) and recortex.LABEL_NAME.fullmatch(item) for item in value):
                raise ValueError
            return value
        if key == "state":
            if text not in ("open", "closed"):
                raise ValueError
            return text
        if key == "reason":
            if text not in ("completed", "not planned"):
                raise ValueError
            return text
        if key in ("created", "updated", "closed"):
            if not STAMP.fullmatch(text):
                raise ValueError
            return text
    except (ValueError, RecursionError):
        pass
    raise Unread("front")


def parse_file(data, relpath, record_id, seal=None):
    """The Parsed file for the bytes `data` of the file at `relpath` (with "/"), or raises Unread. The seal is checked, and the
    state says which of sealed, changed and foreign it is; a file that does not fit is never given a state."""
    if len(data) > FILE_LIMIT:
        raise Unread("size")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Unread("encoding")
    if "\x00" in text or "\r" in text.split("\n", 1)[0]:
        raise Unread("encoding")
    if not text.endswith("\n"):
        raise Unread("seal")
    stripped = text[:-1]
    cut = stripped.rfind("\n")
    found = SEAL_LINE.fullmatch(stripped[cut + 1:])
    if cut < 0 or not found:
        raise Unread("seal")
    signed_text = text[:cut + 1]
    if not text.startswith("---\n"):
        raise Unread("front")
    end = text.find("\n---\n", 3)
    if end < 0 or end > FRONT_LIMIT or end + 5 > cut + 1:
        raise Unread("front")
    front = {}
    for line in text[4:end].split("\n") if end > 3 else []:
        key, sep, value = line.partition(": ")
        if not sep or not re.fullmatch(r"[a-z]+", key) or key in front:
            raise Unread("front")
        front[key] = _value(key, value)
    kind = front.get("kind")
    if kind not in FRONT_ORDER:
        raise Unread("front")
    if list(front) != keys_for(kind, front):
        raise Unread("front")
    if kind == "hub" and front["number"] != 1 or kind == "topic" and front["number"] < 2:
        raise Unread("front")
    body_part = text[end + 5:cut + 1]
    if not body_part.endswith("\n"):
        raise Unread("front")
    body = body_part[:-1]
    if len(body) > recortex.BODY_LIMIT:
        raise Unread("size")
    state = (seal or sealing()).state(record_id, relpath, signed_text.encode("utf-8"), found.group(1), found.group(2))
    return Parsed(kind, front, body, relpath, state, found.group(1), found.group(2))


def render(kind, front, body, relpath, record_id, seal=None):
    """The bytes of a file: front matter in its order, the body, and the seal as the last line."""
    if len(body) > recortex.BODY_LIMIT:
        raise Refused("recortex: the text is longer than %d characters, the most a record file holds." % recortex.BODY_LIMIT)
    lines = []
    for key in keys_for(kind, front):
        value = front[key]
        lines.append("%s: %s" % (key, json.dumps(value, ensure_ascii=False) if key in ("title", "labels") else value))
    signed = ("---\n" + "\n".join(lines) + "\n---\n" + body + "\n").encode("utf-8")
    seal_line = (seal or sealing()).line(record_id, relpath, signed)
    return signed + seal_line.encode("ascii") + b"\n"


def checked(kind, front, body, relpath, record_id):
    """The bytes of a file, made and then read the way the reader will read them: they must come back sealed, with the same kind,
    front matter and body, or nothing is written. One rule, so that a writer and the reader cannot drift apart (a title, a label,
    a NUL or text that is not UTF-8 that the reader would refuse is refused here, before a file exists)."""
    try:
        data = render(kind, front, body, relpath, record_id)
        back = parse_file(data, relpath, record_id)
    except (UnicodeError, Unread, ValueError, TypeError, KeyError):
        raise Refused(NOT_FIT)
    if back.state != "sealed" or back.kind != kind or back.front != front or back.body != body:
        raise Refused(NOT_FIT)
    return data


def slug_of(title, number, taken):
    """A slug from a title, once, at creation: lowercase, runs of other characters to one hyphen, cut to 60. When nothing is
    left, or the slug is reserved or taken (`taken` holds every name in the topics folder, a file or a folder, read or not),
    topic-<number>, then topic-<number>-2, -3 and so on until a name is free; Refused after SLUG_TRIES."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")
    if slug and SLUG.fullmatch(slug) and slug != HUB_FOLDER and slug not in taken:
        return slug
    for attempt in range(1, SLUG_TRIES + 1):
        candidate = "topic-%d" % number if attempt == 1 else "topic-%d-%d" % (number, attempt)
        if candidate not in taken:
            return candidate
    raise Refused("recortex: a name for the topic's file could not be found.")


def attributed(parsed, number):
    """`parsed`, an entry or a version of a topic's text, as it counts when it is read under topic `number` (the number of the
    topic whose folder holds it), as a copy that says where it is placed. The folder is no seal, and an entry's own `topic` is
    one only while the entry's own seal holds: a file that is foreign, or changed because its seal fails, is placed under the topic
    of the folder that holds it, since its text is untrusted and must not choose where it appears. A file whose seal holds is
    placed under its own `topic`, and counts as sealed only when that is `number`; else it is changed."""
    copy = Parsed(parsed.kind, parsed.front, parsed.body, parsed.relpath, parsed.state, parsed.key_id, parsed.mac)
    copy.own_seal = parsed.own_seal
    copy.placed = parsed.front.get("topic") if parsed.own_seal else number
    if parsed.state == "sealed" and parsed.front.get("topic") != number:
        copy.state = "changed"
    return copy


# ---------------------------------------------------------------- the store

class LocalStore:
    """The record in a folder. `folder` is a real path that passed storage.check_record_folder; `config` is the project file's."""

    kind = "local"
    words = record_store.WORDS["local"]

    def __init__(self, repo, config, folder, place):
        self.repo, self.config, self.folder, self.place = repo, config, folder, place
        match = record_store.LOCAL_NAME.fullmatch(repo)
        if not match:
            raise record_store.StoreError("a local record is named local--record/<id>")
        self.record_id = match.group(1)
        self._cache = {}
        self.record = self.read_record()

    @classmethod
    def create(cls, folder, record_id, name, profiles):
        """Make record.json in a record folder that passed storage.check_record_folder: the form, the id, the project's name
        (cut to 80 characters), the profiles and the seal. False when one is there already."""
        plain = {"format": FORM, "id": record_id, "name": str(name)[:80], "profiles": list(profiles), "created": stamp()}
        seal = sealing()
        record = dict(plain, seal={"key": seal.key_id, "mac": seal.json_mac(plain)})
        data = (json.dumps(record, sort_keys=True, indent=1) + "\n").encode("utf-8")
        return storage.link_new_record_file(os.path.join(folder, RECORD_FILE), data)

    # ------------------------------------------------------------ files
    def forget_cache(self):
        self._cache.clear()

    def path(self, relpath):
        return os.path.join(self.folder, *relpath.split("/"))

    def read_record(self):
        """record.json, checked: its id must be this record's, and its seal sealed or foreign."""
        try:
            data = storage.read_record_file(self.path(RECORD_FILE), RECORD_LIMIT)
            record = json.loads(data.decode("utf-8")) if data is not None else None
        except (storage.StorageError, ValueError, UnicodeDecodeError, RecursionError):
            record = None
        problem = record_store.RecordFolderError(RECORD_JSON_REFUSED, "record-json")
        if not isinstance(record, dict) or set(record) != {"format", "id", "name", "profiles", "created", "seal"} \
                or record.get("format") != FORM or record.get("id") != self.record_id or not isinstance(record["seal"], dict) \
                or set(record["seal"]) != {"key", "mac"} or not STAMP.fullmatch(str(record.get("created"))) \
                or not isinstance(record.get("name"), str) or not isinstance(record.get("profiles"), list):
            raise problem
        seal = sealing()
        plain = {key: value for key, value in record.items() if key != "seal"}
        if record["seal"]["key"] == seal.key_id and not hmac.compare_digest(str(record["seal"]["mac"]), seal.json_mac(plain)):
            raise problem
        if record["seal"]["key"] != seal.key_id and not re.fullmatch(r"[0-9a-f]{16}", str(record["seal"]["key"])):
            raise problem
        record["state"] = "sealed" if record["seal"]["key"] == seal.key_id else "foreign"
        return record

    def scan(self, sub=""):
        return storage.scan_record_folder(self.folder, sub)

    def read_parsed(self, relpath, rows=None):
        """The Parsed file at relpath; None when it does not exist; raises Unread (a fixed reason) when it does not fit and
        storage.StorageError when it cannot be read safely. Cached by the file's size and time, which a listing gives."""
        folder, _, name = relpath.rpartition("/")
        listing = rows if rows is not None else self.scan(folder)
        row = next((r for r in listing if r[0] == name), None)
        if row is None or row[3] != "file":
            if row is not None:
                raise Unread("kind")
            return None
        key = (relpath, row[1], row[2])
        found = self._cache.get(key)
        if found is not None:
            return found
        data = storage.read_record_file(self.path(relpath), FILE_LIMIT)
        if data is None:
            return None
        found = parse_file(data, relpath, self.record_id)
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = found
        return found

    def topics(self):
        """[(number, relpath, Parsed)] of the hub and every topic that reads, by number; a file that does not read is left out."""
        found = []
        try:
            hub = self.read_parsed(HUB_FILE)
        except (Unread, storage.StorageError):
            hub = None
        if hub is not None and hub.kind == "hub":
            found.append((1, HUB_FILE, hub))
        rows = self.scan(TOPICS_FOLDER)
        for name, size, mtime, kind in rows:
            if kind != "file" or not name.endswith(".md") or not SLUG.fullmatch(name[:-3]):
                continue
            relpath = "%s/%s" % (TOPICS_FOLDER, name)
            try:
                parsed = self.read_parsed(relpath, rows)
            except (Unread, storage.StorageError):
                continue
            if parsed is not None and parsed.kind == "topic":
                found.append((parsed.number, relpath, parsed))
        found.sort(key=lambda item: (item[0], item[1]))
        if len({item[0] for item in found}) != len(found):
            raise record_store.RecordFolderError(DUPLICATE, "duplicate")   # the sync's rule: two diverged copies are not read
        return found

    def topic(self, number):
        """(relpath, Parsed) of topic `number`, or raises Missing; two files that carry one number are refused (topics)."""
        for found, relpath, parsed in self.topics():
            if found == number:
                return relpath, parsed
        raise record_store.Missing("recortex: the record has no topic #%d." % number if isinstance(number, int) else
                                   "recortex: the record has no such topic.")

    def folder_of(self, relpath):
        """The folder that holds a topic's entries and body versions."""
        return HUB_FOLDER if relpath == HUB_FILE else relpath[:-3]

    def entry_rows(self, relpath):
        """[(created from the name, entry id, number, name, size, mtime_ns)] of the files of a topic's folder whose names are an
        entry's, by name (created, then id); nothing is opened."""
        found = []
        for name, size, mtime, kind in self.scan(self.folder_of(relpath)):
            match = ENTRY_NAME.fullmatch(name)
            if kind == "file" and match:
                found.append((match.group(1), match.group(2), entry_number(match.group(2)), name, size, mtime))
        return sorted(found)

    def topic_number_of(self, relpath):
        """The number of the topic file at relpath as it reads now, or None when it does not read."""
        try:
            parsed = self.read_parsed(relpath)
        except (Unread, storage.StorageError):
            return None
        return None if parsed is None else (1 if parsed.kind == "hub" else parsed.number)

    def read_entries(self, relpath, kinds=("entry",)):
        """[(Parsed, number, mtime_ns)] of the files of kind in `kinds` in a topic's folder that read, oldest first. A file counts
        as sealed only when its own sealed `topic` is the number of the topic it is read under (attributed)."""
        folder = self.folder_of(relpath)
        rows = self.scan(folder)
        topic = self.topic_number_of(relpath)
        result = []
        for created, entry_id, number, name, size, mtime in self.entry_rows(relpath):
            try:
                parsed = self.read_parsed("%s/%s" % (folder, name), rows)
            except (Unread, storage.StorageError):
                continue
            if parsed is None or parsed.kind not in kinds or parsed.entry_id != entry_id:
                continue
            result.append((attributed(parsed, topic), number, mtime))
        result.sort(key=lambda item: (item[0].front["created"], item[2], item[0].relpath))
        return result

    # ------------------------------------------------------------ what a state is read as
    @staticmethod
    def author_of(parsed):
        return OTHER_AUTHOR if parsed.state == "foreign" else OWN_AUTHOR

    @staticmethod
    def edited_stamp(parsed, mtime_ns, created):
        """The time of a change outside recortex: the file's, never equal to the creation time."""
        edited = stamp(mtime_ns / 1e9)
        return edited if edited != created else stamp(mtime_ns / 1e9 + 1)

    def issue_row(self, number, relpath, parsed):
        front = parsed.front
        closed = front["state"] == "closed"
        return {
            "number": number, "title": front["title"], "state": front["state"],
            "state_reason": None if not closed else ("completed" if front["reason"] == "completed" else "not_planned"),
            "body": parsed.body, "labels": [{"name": label} for label in front["labels"]],
            "user": {"login": self.author_of(parsed)}, "created_at": front["created"], "updated_at": front["updated"],
            "closed_at": front.get("closed") if closed else None, "html_url": self.address(number), "state_seal": parsed.state,
        }

    def entry_row(self, number, relpath, parsed, entry_no, mtime_ns):
        """The row of an entry as a comment. Its topic is its own sealed `topic`, never the folder it was found in."""
        created = parsed.front["created"]
        updated = created if parsed.state != "changed" else self.edited_stamp(parsed, mtime_ns, created)
        topic = parsed.placed
        return {"id": entry_no, "node_id": parsed.entry_id, "issue_url": self.api_issue(topic),
                "html_url": self.address(topic, entry_no), "user": {"login": self.author_of(parsed)}, "body": parsed.body,
                "created_at": created, "updated_at": updated, "state_seal": parsed.state}

    # ------------------------------------------------------------ the interface: what the store has
    def supports(self, feature):
        return False

    def require(self, feature):
        """At the entry point of a script that is a GitHub-only feature: its fixed line, and an exit with status 0."""
        line = record_store.OFF.get(feature)
        if line:
            print(line)
        sys.exit(0)

    def github(self, args, stdin=None, timeout=60, errors=None, binary=False):
        raise record_store.Unsupported(record_store.NO_SERVER)

    # ------------------------------------------------------------ reads
    def whoami(self, host=False, timeout=60):
        return OWN_AUTHOR

    def account_state(self, timeout=None):
        return "ready", OWN_AUTHOR

    def read_issue(self, number, timeout=60, jq=None):
        relpath, parsed = self.topic(number)
        if jq is not None:
            if jq == ".number":
                return "%d\n" % number
            raise record_store.Unsupported("recortex: this question of the record is not supported.")
        return self.issue_row(number, relpath, parsed)

    def find_entry(self, cid):
        """(topic number, relpath of the topic, Parsed, entry number, mtime_ns) of entry `cid`, or raises Missing."""
        try:
            wanted = int(cid)
        except (TypeError, ValueError):
            raise record_store.Missing("recortex: the record has no such entry.")
        for number, relpath, topic in self.topics():
            for created, entry_id, entry_no, name, size, mtime in self.entry_rows(relpath):
                if entry_no != wanted:
                    continue
                try:
                    parsed = self.read_parsed("%s/%s" % (self.folder_of(relpath), name))
                except (Unread, storage.StorageError):
                    continue
                if parsed is not None and parsed.kind == "entry" and parsed.entry_id == entry_id:
                    return number, relpath, attributed(parsed, number), entry_no, mtime
        raise record_store.Missing("recortex: the record has no such entry.")

    def read_entry(self, cid, timeout=60):
        number, relpath, parsed, entry_no, mtime = self.find_entry(cid)
        return self.entry_row(number, relpath, parsed, entry_no, mtime)

    def list_entries(self, issue, timeout=60):
        relpath, topic = self.topic(issue)
        rows = [self.entry_row(issue, relpath, parsed, number, mtime) for parsed, number, mtime in self.read_entries(relpath)]
        return json.dumps(rows)

    def hub_entries(self, hub, timeout=60):
        relpath, topic = self.topic(hub)
        rows = []
        for parsed, number, mtime in self.read_entries(relpath):
            changed = parsed.state == "changed"
            rows.append({
                "url": self.address(parsed.placed, number), "created": parsed.front["created"], "body": parsed.body,
                "author": self.author_of(parsed),
                "last_edited": self.edited_stamp(parsed, mtime, parsed.front["created"]) if changed else None,
                "editor": None, "edit_total": 1 if changed else 0, "edit_editors": [None] if changed else [],
                "local": True,
            })
        rows = rows[-recortex.COMMENTS_READ:]
        order = sorted(range(len(rows)), key=lambda i: (rows[i]["created"], i), reverse=True)
        return [rows[i] for i in order]

    def histories(self, nodes, timeout=60):
        """The answer of the edit-history query, from the files: one entry per id in `nodes`."""
        data = {}
        by_number = {}
        for cid in nodes:
            try:
                number, relpath, parsed, entry_no, mtime = self.find_entry(cid)
            except record_store.Missing:
                data["c%d" % cid] = None
                continue
            changed = parsed.state == "changed"
            edit = self.edited_stamp(parsed, mtime, parsed.front["created"]) if changed else None
            data["c%d" % cid] = {"fullDatabaseId": str(cid), "lastEditedAt": edit,
                                 "userContentEdits": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                      "nodes": [{"editedAt": edit, "editor": None}] if changed else []}}
        return json.dumps({"data": data})

    def find_issues(self, shape, timeout=60):
        if shape not in record_store.FIELD_SHAPES:
            raise ValueError("no such shape: %s" % shape)
        found = []
        for number, relpath, parsed in self.topics():
            if parsed.state != "sealed":
                continue
            front = parsed.front
            if shape == "mine":
                found.append({"number": number, "title": front["title"], "body": parsed.body, "url": self.address(number)})
            elif shape == "active" and front["state"] == "open" and "active" in front["labels"]:
                found.append({"number": number, "labels": [{"name": label} for label in front["labels"]]})
            elif shape == "plan" and front["state"] == "open" and "plan" in front["labels"]:
                found.append({"number": number})
            elif shape == "dated":
                found.append({"number": number, "title": front["title"], "createdAt": front["created"],
                              "closedAt": front.get("closed") if front["state"] == "closed" else None,
                              "url": self.address(number)})
        return found

    def list_labels(self, fields="name", timeout=60):
        import setup_labels
        wanted = setup_labels.wanted_labels(list(self.config.get("profiles") or []), self.config.get("topics") or [])
        if self.config.get("plans") and not any(label["name"] == "plan" for label in wanted):
            import plan
            wanted = wanted + [dict(zip(("name", "color", "description"), plan.LABEL))]
        names_wanted = [name for name in fields.split(",") if name]
        return [{name: label[name] for name in names_wanted if name in label} for label in wanted]

    def is_pinned(self, number):
        return number == 1

    def visibility(self, timeout=30):
        return ""

    def dump(self, timeout):
        raise record_store.Unsupported(record_store.SNAPSHOT_OFF)

    def dump_commands(self):
        raise record_store.Unsupported(record_store.SNAPSHOT_OFF)

    def versions(self, issue):
        """[(created, state, text)] of the earlier versions of a topic's text, newest first: the `body` files of its folder.
        A file that does not read is left out (the sync counts it)."""
        relpath, topic = self.topic(issue)
        found = [(parsed.front["created"], parsed.state, parsed.body)
                 for parsed, number, mtime in self.read_entries(relpath, kinds=("body",))]
        return sorted(found, key=lambda row: row[0], reverse=True)

    # ------------------------------------------------------------ writes
    def _text(self, body, body_file):
        if body is None:
            if body_file is None:
                raise Refused("recortex: nothing to write.")
            body = recortex.read_text(body_file)
        return body

    def _own_topic(self, number):
        """(relpath, Parsed) of a topic that the user's own seal vouches for: a write over a text that was changed outside
        recortex, or written elsewhere, would seal it as the user's, so it waits for the user's answer."""
        relpath, parsed = self.topic(number)
        if parsed.state != "sealed":
            raise Refused("recortex: topic #%d was %s, so it is not sealed as yours; a change waits for the question about "
                          "taking it back." % (number, "changed outside recortex" if parsed.state == "changed" else
                                                       "written on another machine"))
        return relpath, parsed

    def _write_topic(self, relpath, front, body):
        data = checked(front["kind"], front, body, relpath, self.record_id)
        storage.replace_record_file(self.path(relpath), data)
        self._cache.clear()

    def _put_version(self, relpath, number, body, created):
        folder = self.folder_of(relpath)
        storage.make_record_folder(self.path(folder))
        for _ in range(20):
            entry_id = new_id()
            name = "%s-%s.md" % (name_stamp(created), entry_id)
            rel = "%s/%s" % (folder, name)
            front = {"recortex": 1, "kind": "body", "id": entry_id, "topic": number, "created": created}
            if storage.link_new_record_file(self.path(rel), checked("body", front, body, rel, self.record_id)):
                return entry_id
        raise Refused("recortex: a version of the text could not be made.")

    def _next_number(self):
        """The number of the next topic: the smallest number above the highest number that a topic file whose own seal holds
        carries, that no other topic file carries. A file that was changed, written elsewhere or does not read keeps its number
        out of use (a number used twice stops the sync) but never moves the counter: its text is untrusted, and a planted
        `number: 999999999` must not push the record to the most topics there can be. A number is looked for only in a file that
        begins with `---` and only inside its front matter. A topic file (a slug and .md, or hub.md) that exists and cannot be
        read safely is refused, with one fixed line; other names (a sync tool's copies) are not topic files."""
        sealed, other = {1}, set()
        names_ = [HUB_FILE] + ["%s/%s" % (TOPICS_FOLDER, row[0]) for row in self.scan(TOPICS_FOLDER)
                               if row[3] != "dir" and row[0].endswith(".md") and SLUG.fullmatch(row[0][:-3])]
        for relpath in names_:
            try:
                data = storage.read_record_file(self.path(relpath), FILE_LIMIT)
            except storage.StorageError:
                raise record_store.RecordFolderError(UNREADABLE_TOPIC, "unreadable")
            if data is None:
                continue
            try:
                parsed = parse_file(data, relpath, self.record_id)
            except Unread:
                parsed = None
            if parsed is not None and parsed.state == "sealed" and parsed.kind in ("hub", "topic"):
                sealed.add(parsed.number)
                continue
            block = front_block(data)
            found = re.search(rb"^number: ([1-9][0-9]{0,8})$", block, re.M) if block else None
            if found:
                other.add(int(found.group(1)))
        number = max(sealed) + 1
        while number in other or number in sealed:
            number += 1
        return number

    def create_issue(self, title, labels, body=None, body_file=None):
        text = self._text(body, body_file)
        labels = list(labels)
        with storage.record_lock(self.record_id):
            self.topics()   # two files with one number: nothing is made on top of that
            created = stamp()
            if "hub" in labels:
                if any(row[0] == HUB_FILE for row in self.scan()):
                    raise Refused("recortex: this record has a hub already.")   # in any state: hub.md is taken
                number, relpath, kind = 1, HUB_FILE, "hub"
                tries = [relpath]
            else:
                number = self._next_number()
                if number > NUMBER_MAX:
                    raise Refused("recortex: the record holds the most topics there can be.")
                kind = "topic"
                taken = set()
                for row in self.scan(TOPICS_FOLDER):   # every name there is, a file or a folder, whether it reads or not
                    taken.add(row[0][:-3] if row[0].endswith(".md") else row[0])
                tries = None
            if kind == "topic":
                storage.make_record_folder(self.path(TOPICS_FOLDER))
            front = {"recortex": 1, "kind": kind, "number": number, "title": title, "labels": labels, "state": "open",
                     "created": created, "updated": created}
            for _ in range(SLUG_TRIES):
                if tries is None:
                    slug = slug_of(title, number, taken)
                    relpath = "%s/%s.md" % (TOPICS_FOLDER, slug)
                data = checked(kind, front, text, relpath, self.record_id)
                if storage.link_new_record_file(self.path(relpath), data):   # never replaces what is there
                    break
                if tries is not None:
                    raise Refused("recortex: this record has a hub already.")
                taken.add(slug)   # made by someone since the listing: the next free name
            else:
                raise Refused("recortex: a name for the topic's file could not be found.")
            self._cache.clear()
            self._put_version(relpath, number, text, created)
        return self.address(number)

    def edit_body(self, number, body=None, body_file=None):
        text = self._text(body, body_file)
        with storage.record_lock(self.record_id):
            relpath, parsed = self._own_topic(number)
            created = stamp()
            front = dict(parsed.front, updated=created)
            checked(front["kind"], front, text, relpath, self.record_id)   # refused before the version file is made
            self._put_version(relpath, number, text, created)
            self._write_topic(relpath, front, text)

    def post_entry(self, issue, body=None, body_file=None):
        text = self._text(body, body_file)
        with storage.record_lock(self.record_id):
            relpath, parsed = self.topic(issue)
            folder = self.folder_of(relpath)
            checked("entry", {"recortex": 1, "kind": "entry", "id": "aaaaaaaaaaaaaaaa", "topic": issue, "created": stamp()},
                    text, "%s/2000-01-01T0000Z-aaaaaaaaaaaaaaaa.md" % folder, self.record_id)   # refused before a folder is made
            storage.make_record_folder(self.path(folder))
            created = stamp()
            used = {number for _, _, number, _, _, _ in self.entry_rows(relpath)}
            for _ in range(50):
                entry_id = new_id()
                entry_no = entry_number(entry_id)
                if entry_no == 0 or entry_no in used or entry_no in self._all_numbers():
                    continue
                rel = "%s/%s-%s.md" % (folder, name_stamp(created), entry_id)
                front = {"recortex": 1, "kind": "entry", "id": entry_id, "topic": issue, "created": created}
                if storage.link_new_record_file(self.path(rel), checked("entry", front, text, rel, self.record_id)):
                    return self.address(issue, entry_no)
        raise Refused("recortex: an entry could not be made.")

    def _all_numbers(self):
        found = set()
        for number, relpath, parsed in self.topics():
            found.update(item[2] for item in self.entry_rows(relpath))
        return found

    def set_labels(self, number, add, remove):
        with storage.record_lock(self.record_id):
            relpath, parsed = self._own_topic(number)
            known = {label["name"] for label in self.list_labels("name")}
            for label in add:
                if label not in known:
                    raise Refused("recortex: a label that the profiles do not define cannot be set.")
            labels = [label for label in parsed.front["labels"] if label not in set(remove)]
            labels += [label for label in add if label not in labels]
            self._write_topic(relpath, dict(parsed.front, labels=labels, updated=stamp()), parsed.body)

    def create_label(self, name, color, description):
        raise record_store.Unsupported(record_store.OFF["label-catalog"])

    edit_label = create_label

    def pin(self, number):
        return None

    def close(self, number, reason):
        if reason not in ("completed", "not planned"):
            raise Refused("recortex: a topic is closed as completed or as not planned.")
        with storage.record_lock(self.record_id):
            relpath, parsed = self._own_topic(number)
            now_ = stamp()
            front = {key: value for key, value in parsed.front.items() if key not in ("reason", "closed")}
            front.update(state="closed", reason=reason, closed=now_, updated=now_)
            self._write_topic(relpath, front, parsed.body)

    def reopen(self, number):
        with storage.record_lock(self.record_id):
            relpath, parsed = self._own_topic(number)
            front = {key: value for key, value in parsed.front.items() if key not in ("reason", "closed")}
            front.update(state="open", updated=stamp())
            self._write_topic(relpath, front, parsed.body)

    # ------------------------------------------------------------ taking back what is not vouched for (adopt)
    def _files(self):
        """[(relpath, Parsed)] of every file of the record that reads, by path, with its own state (not attributed): the hub, the
        topics, and each topic's entries and versions of its text."""
        found = []
        for number, relpath, parsed in self.topics():
            found.append((relpath, parsed))
            folder = self.folder_of(relpath)
            rows = self.scan(folder)
            for created, entry_id, entry_no, name, size, mtime in self.entry_rows(relpath):
                try:
                    inner = self.read_parsed("%s/%s" % (folder, name), rows)
                except (Unread, storage.StorageError):
                    continue
                if inner is not None and inner.kind in ("entry", "body") and inner.entry_id == entry_id:
                    found.append(("%s/%s" % (folder, name), inner))
        return sorted(found, key=lambda item: item[0])

    def _digest(self, relpaths):
        """The SHA-256 of the bytes of these files, in path order: what an answer to an adopt question is bound to."""
        sha = hashlib.sha256()
        for relpath in sorted(relpaths):
            data = storage.read_record_file(self.path(relpath), FILE_LIMIT) or b""
            sha.update(relpath.encode("utf-8") + b"\0" + data + b"\0")
        return sha.hexdigest()

    def adopt_plan(self, mode, number=0):
        """The values of an adopt question: for `mode` "topic" the topic whose text was changed outside recortex (how many lines
        differ from the last version recortex wrote), for "foreign" the files that were written under another key (how many
        entries and topics). Counts and a hash of the bytes, never text. Raises a StoreError when there is nothing to take back.
        A topic whose seal holds, and an entry that was changed, are never adopted: an entry is never edited."""
        if mode == "topic":
            self.record = self.read_record()   # a record.json changed since this store was opened is read again
            relpath, parsed = self.topic(number)
            if parsed.state != "changed":
                raise Refused("recortex: topic #%d is not marked as changed outside recortex; nothing to take back." % number)
            sealed = [text for created, state, text in self.versions(number) if state == "sealed"]
            import difflib
            before = sealed[0].split("\n") if sealed else []
            lines = sum(1 for row in difflib.ndiff(before, parsed.body.split("\n")) if row[:1] in ("+", "-"))
            return {"mode": "topic", "topic": number, "lines": lines, "entries": 0, "topics": 0, "digest": self._digest([relpath])}
        self.record = self.read_record()
        found = [(relpath, parsed) for relpath, parsed in self._files() if parsed.state == "foreign"]
        paths = [relpath for relpath, parsed in found]
        if self.record["state"] == "foreign":
            paths.append(RECORD_FILE)
        if not paths:
            raise Refused("recortex: nothing in this record was written under another key.")
        return {"mode": "foreign", "topic": 0, "lines": 0, "digest": self._digest(paths),
                "entries": sum(1 for relpath, parsed in found if parsed.kind == "entry"),
                "topics": sum(1 for relpath, parsed in found if parsed.kind in ("hub", "topic"))}

    def adopt(self, mode, number, expected):
        """Seal what the user took back. With the hash of the bytes the question was bound to: a file that changed since then
        refuses the act. A topic's text first becomes a version file (the text as it was taken), then is sealed; foreign files get
        their seal line rewritten and nothing else of them. Returns how many files were sealed."""
        with storage.record_lock(self.record_id):
            plan = self.adopt_plan(mode, number)
            if plan["digest"] != expected:
                raise Refused("recortex: the files changed since the question was asked; nothing was sealed.")
            if mode == "topic":
                relpath, parsed = self.topic(number)
                created = stamp()
                front = dict(parsed.front, updated=created)
                checked(front["kind"], front, parsed.body, relpath, self.record_id)
                self._put_version(relpath, number, parsed.body, created)
                self._write_topic(relpath, front, parsed.body)
                return 1
            done = 0
            for relpath, parsed in self._files():
                if parsed.state != "foreign":
                    continue
                data = checked(parsed.kind, parsed.front, parsed.body, relpath, self.record_id)
                storage.replace_record_file(self.path(relpath), data)
                done += 1
            if self.record["state"] == "foreign":
                plain = {key: value for key, value in self.record.items() if key not in ("seal", "state")}
                seal = sealing()
                record = dict(plain, seal={"key": seal.key_id, "mac": seal.json_mac(plain)})
                storage.replace_record_file(self.path(RECORD_FILE), (json.dumps(record, sort_keys=True, indent=1) + "\n").encode())
                done += 1
            self._cache.clear()
            self.record = self.read_record()
            return done

    # ------------------------------------------------------------ reading what a write returned, and saying what one would do
    def show(self, op, *args, note="", **kwargs):
        words = SHOW_WORDS.get(op)
        if words is None:
            raise ValueError("no such operation: %s" % op)
        text = "local record: " + (words % self._number_word(args[0]) if "%s" in words else words)
        return text

    @staticmethod
    def _number_word(value):
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value if isinstance(value, str) and PLACEHOLDER.fullmatch(value) else "a topic"

    def read_back(self, what, number=None):
        if what == "entry":
            return "the entry in the record folder"
        return "topic #%s in the record folder" % self._number_word(number)

    def posted_id(self, output, issue=None):
        """The number of the entry a post made, from the address it returned (`#N/ID`), or None."""
        found = re.fullmatch(r"#([0-9]{1,9})/([0-9]{10,19})", output.strip().split("\n")[-1]) if isinstance(output, str) else None
        if found is None or (issue is not None and int(found.group(1)) != issue):
            return None
        return found.group(2)

    def created_number(self, output):
        found = re.fullmatch(r"#([0-9]{1,9})", output.strip().split("\n")[-1]) if isinstance(output, str) else None
        if found is None:
            raise recortex.GhError("could not read a topic number from the record's answer")
        return int(found.group(1))

    def address(self, issue, entry=None):
        return record_store.address_of(self.repo, issue, entry)

    def api_issue(self, issue):
        return record_store.api_issue_of(self.repo, issue)

    def safe_address(self, text):
        return record_store.safe_address_of(self.repo, text)

    def parse_address(self, text):
        found = re.fullmatch(r"#([1-9][0-9]{0,8})(?:/([1-9][0-9]{9,18}))?", text) if isinstance(text, str) else None
        return None if found is None else (int(found.group(1)), int(found.group(2) or 0))

    def is_missing(self, error):
        return isinstance(error, record_store.Missing)

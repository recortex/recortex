"""The reader of the dashboard (1.9.0, spec 10.3): the whole record inside the page, for a local record and a GitHub record alike.

This is the part of the model that holds a reading view for each topic: the title, the state and the labels, the current text, the
earlier versions of the text, and every entry in time order, each with the mark that says why it is not told when it is not. It
follows `index.memories` and `index.body_told` and adds no rule of its own: what is told is shown open, what is not told is shown
shut under a fixed mark. Text shown here feeds nothing else: no suggestion, no impact, no map, no plan and no hook payload reads it.
The mixin is used by mind_model.Build, which supplies `db`, `repo`, `allowed`, `config` and `doc`."""

import re

import index
import record_store

LOCAL_CHANGED = "Changed outside recortex. Not counted as your words."
LOCAL_FOREIGN = "Written on another machine. Not counted as your words."
GITHUB_OUTSIDE = "By an account outside your allowed list. Not counted as your words."
GITHUB_CHANGED = "Changed on GitHub after it was posted. Not counted as your words."
TAKE_BACK = "recortex will ask in your session whether to take these back as yours."
UNREAD = "recortex: %d files in the record folder were not read (a sync tool's copies, or files that are not recortex's)."
TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2})")
KIND = re.compile(r"[a-z]{1,20}")
MARKER = re.compile(r"\A[ \t]*<!--[^\n]*-->[ \t]*\n")   # the event marker of a memory is not text of the reader


def stamp_text(value):
    """A time of the index as the page tells it: `2026-11-24 10:00 UTC`, or the first characters of what it is."""
    found = TIME.match(value or "")
    return "%s %s UTC" % (found.group(1), found.group(2)) if found else (value or "")[:40]


def mark_for(local, author_allowed, unconfirmed):
    """The fixed words that say why an entry or a text is not told, or None."""
    if not author_allowed:
        return LOCAL_FOREIGN if local else GITHUB_OUTSIDE
    if unconfirmed:
        return LOCAL_CHANGED if local else GITHUB_CHANGED
    return None


class ReaderMixin:
    def reader_model(self, mode, cap):
        """The reader's part of the model. `mode` is "all" (every topic's view, up to `cap` characters in all, the newest topics
        first), "rows" (the list of topics alone: the live dashboard's main page) or a topic number (that one view, whole)."""
        local = record_store.is_local(self.repo)
        db = self.db
        edited = index.edited_comments(db)
        unconfirmed = index.unconfirmed_edits(db, self.allowed)
        kinds = {row["comment"]: row["type"] for row in db.execute("SELECT comment, type FROM events")}
        states = {}
        if local:
            states = {row["number"]: row["state"] for row in db.execute("SELECT number, state FROM local_files")}
        entries = {}
        for row in db.execute("SELECT id, issue, author, body, created_at FROM comments ORDER BY created_at, id"):
            entries.setdefault(row["issue"], []).append(row)
        counts = {"entries": 0, "folded": 0}
        topics = []
        rows = db.execute("SELECT number, title, state, author, body, created_at, updated_at, closed_at FROM issues "
                          "WHERE is_pr = 0 ORDER BY number").fetchall()
        hub = self.config.get("hub") if isinstance(self.config, dict) else None
        used = 0
        order = sorted(rows, key=lambda row: (row["updated_at"] or row["created_at"] or "", row["number"]), reverse=True)
        place = {row["number"]: position for position, row in enumerate(order)}
        for row in rows:
            number = row["number"]
            folded = 0
            listed = []
            for entry in entries.get(number, []):
                author_ok = self.ok(entry["author"])
                mark = mark_for(local, author_ok, entry["id"] in unconfirmed)
                folded += 1 if mark else 0
                listed.append((entry, mark))
            counts["entries"] += len(listed)
            counts["folded"] += folded
            # the title follows the page's title rule and the labels the account that opened the topic: what is not told is "Topic N"
            topic = {"n": number, "title": self.clean_text(row["title"], 300) if self.title_shown(number) and (row["title"] or "").strip()
                     else "Topic %d" % number, "state": "closed" if row["state"] == "closed" else "open",
                     "closed": stamp_text(row["closed_at"]) if row["closed_at"] and row["state"] == "closed" else None,
                     "labels": [self.clean_text(label, 60) for label in index.issue_labels(db, number)][:20] if self.ok(row["author"]) else [],
                     "updated": stamp_text(row["updated_at"] or row["created_at"]), "hub": number == hub,
                     "count": len(listed), "folded": folded}
            body_mark = None
            if not index.body_told(db, number, self.allowed):
                if local:
                    state = states.get(number)
                    body_mark = LOCAL_FOREIGN if state == "foreign" else LOCAL_CHANGED
                else:
                    body_mark = GITHUB_OUTSIDE if not self.ok(row["author"]) else GITHUB_CHANGED
            topic["body_mark"] = body_mark
            topic["untold"] = bool(body_mark) or folded > 0
            wanted = mode == "all" or mode == number
            if wanted:
                size = len(row["body"] or "") + sum(len(entry["body"] or "") for entry, mark in listed)
                topic["capped"] = mode == "all" and used + size > self.reader_cap(cap) and place[number] > 0
                used += 0 if topic["capped"] else size
                self.read_topic(topic, row, listed, local, edited, unconfirmed, kinds)
            topics.append(topic)
        unread = 0
        if local:
            try:
                unread = int(index.get_meta(db, "local_unread") or 0)
            except ValueError:
                unread = 0
        return {"local": local, "topics": topics, "counts": counts, "unread": unread, "mode": "rows" if mode == "rows" else "views",
                "only": mode if isinstance(mode, int) and not isinstance(mode, bool) else None}

    @staticmethod
    def reader_cap(cap):
        return cap if isinstance(cap, int) and cap > 0 else 2000000

    @staticmethod
    def clean_text(value, cap):
        import mind_model
        return mind_model.short(value or "", cap)

    def read_topic(self, topic, row, listed, local, edited, unconfirmed, kinds):
        """The text, the entries and the earlier versions of one topic, for its view."""
        from mind_model import CAP_FIRST_LINES, CAP_TEXT   # the caps live beside the page's other caps
        body = row["body"] or ""
        if topic["capped"]:
            topic["first"] = self.doc(body, set(), cap=CAP_FIRST_LINES)
            topic["entries"], topic["versions"], topic["edits"] = [], [], []
            return
        topic["body"] = self.doc(body, set(), cap=CAP_TEXT) if body.strip() else None
        topic["entries"] = []
        for entry, mark in listed:
            kind = kinds.get(entry["id"])
            number = topic["n"]
            item = {"id": entry["id"], "at": stamp_text(entry["created_at"]), "mark": mark,
                    "kind": kind if isinstance(kind, str) and KIND.fullmatch(kind) else None,
                    "doc": self.doc(MARKER.sub("", entry["body"] or ""), set(), cap=CAP_TEXT),
                    "note": bool(not mark and entry["id"] in edited and not local)}
            if local:
                item["address"] = "#%d/%d" % (number, entry["id"])
            topic["entries"].append(item)
        topic["versions"], topic["edits"] = [], []
        if local:
            topic["versions"] = self.local_versions(topic["n"])
        else:
            topic["edits"] = [stamp_text(at) for (at,) in self.db.execute(
                "SELECT at FROM timeline WHERE issue = ? AND event = 'edited' ORDER BY at DESC, seq DESC LIMIT 50", (topic["n"],))]

    def local_versions(self, number):
        """The earlier texts of a local topic, newest first, each with its mark: the body files of its folder, read through the
        store (the index holds only their times). A version that cannot be read is left out."""
        from mind_model import CAP_TEXT
        try:
            found = record_store.for_repo(self.repo, self.config).versions(number)
        except (record_store.StoreError, SystemExit, OSError, ValueError, TypeError, AttributeError):
            return []
        out = []
        for created, state, text in found[:50]:
            mark = None if state == "sealed" else LOCAL_FOREIGN if state == "foreign" else LOCAL_CHANGED
            out.append({"at": stamp_text(created), "mark": mark, "doc": self.doc(text, set(), cap=CAP_TEXT)})
        return out

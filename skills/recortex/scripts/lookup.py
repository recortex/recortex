"""What the linter may ask about the record, answered from GitHub or from the local index.

Python 3 standard library only. Read-only. One class, two sources, no state besides a cache for one run:

  lookup.github(repo, number)  reads the record through the record store; what it cannot answer in one call (the
                               topic issue of an active task, words and thread openings already explained) comes
                               from the local index when it is current, and is UNKNOWN otherwise
  lookup.local(repo, number)   reads the local index only, with no network; raises Unavailable when there is no
                               usable index

`number` is the issue the draft goes to (None when unknown). Every answer is a value, None (there is no such
thing) or UNKNOWN (this source cannot tell). A GitHub failure other than "Not Found" is raised as recortex.GhError,
so a post stops instead of counting a target as present or absent.

Explanations and link targets by accounts outside the allowed list do not count when the repository is in the
user's trusted list; when it is not, every account counts, since there is no list to filter by.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recortex  # noqa: E402
import record_store  # noqa: E402
import record_format  # noqa: E402


class Unknown:
    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = Unknown()


class Unavailable(Exception):
    """There is no usable local index; the message says why in fixed words."""


class Lookup:
    UNKNOWN = UNKNOWN

    def __init__(self, repo, number, online, db=None):
        self.repo, self.number, self.online, self.db = repo, number, online, db
        self.store = record_store.for_repo(repo) if online else None
        self.source = self.store.words["name"] if online else "the local index"
        trusted = recortex.trusted_repos()
        self.trusted = repo.lower() in trusted
        self.allowed = recortex.allowed_users(repo, trusted) if self.trusted else set()
        self.cache = {}
        self.index_tried = db is not None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def counts(self, author):
        """Whether a memory or explanation by `author` counts (see the module's docstring)."""
        return not self.trusted or (author or "").lower() in self.allowed

    def remember(self, key, compute):
        if key not in self.cache:
            self.cache[key] = compute()
        return self.cache[key]

    def index(self):
        """The local index when it is usable and current, else None (GitHub source only; never an exit)."""
        if not self.index_tried:
            self.index_tried = True
            try:
                import index
                db = index.connect(self.repo, create=False)
                if db is not None and index.mind_ready(db):
                    self.db = db
            except Exception:  # noqa: BLE001 - a refused, unreadable or missing index only means UNKNOWN
                self.db = None
        return self.db

    # ------------------------------------------------------------ answers

    def memory(self, cid):
        """The comment `cid` of this repository: dict(type, time, at, issue, author, edited), or None when there is
        none. edited is True when GitHub's updated_at of the comment differs from its created_at, unless the local
        index holds a complete edit history for that updated_at that shows only allowed accounts (index.cleared_comments)."""
        return self.remember(("memory", cid), lambda: self._memory(cid))

    def steps_memory(self, cid):
        """The comment `cid` as a target of a mark: what memory() says, plus count (how many steps it lists; 0 when it is
        no steps memory) and active (True when its issue carries the label `active`), or None when there is no such
        comment."""
        return self.remember(("steps", cid), lambda: self._steps(cid))

    def issue(self, number):
        """Issue or pull request `number`: dict(labels, pull, blocks={"table": set, "figure": set}), or None."""
        return self.remember(("issue", number), lambda: self._issue(number))

    def topic_issue(self, number):
        """The issues labeled final-record that share a topic: label with `number`, as a list ([number] when it
        carries final-record itself, [] when none does)."""
        return self.remember(("topic", number), lambda: self._topic_issue(number))

    def word(self, key):
        """The standing explanation of a word key: dict(comment, date, meaning), or None when none."""
        return self.remember(("word", key), lambda: self._from_index(lambda db, index: self._standing(
            index.standing_words(db, self.allowed).get(key))))

    def opening(self, number):
        """The standing "what this thread is about" of the thread of issue `number`: dict(comment, date, meaning),
        or None."""
        return self.remember(("opening", number), lambda: self._from_index(lambda db, index: self._standing(
            index.standing_opening(db, number, self.allowed))))

    def plan_agreed(self, cid):
        """Whether the plan memory `cid` is agreed (plan_rules: a told decision of an allowed account links it, or it
        narrows or rewords an agreed part): True, False, or UNKNOWN when the local index is not usable."""
        return self.remember(("agreed", cid), lambda: self._from_index(lambda db, index: self._agreed(db, index, cid)))

    def _agreed(self, db, index, cid):
        try:
            config = recortex.load_config()
        except (OSError, ValueError):
            return UNKNOWN
        state = index.planned(db, config, self.allowed) if self.trusted else None
        return bool(state and state["parts"].get(cid, {}).get("agreed"))

    # ------------------------------------------------------------ sources

    @staticmethod
    def _standing(row):
        return None if row is None else {"comment": row["comment"], "date": (row["at"] or "")[:10],
                                         "meaning": row["meaning"]}

    def _from_index(self, ask):
        db = self.index()
        if db is None:
            return UNKNOWN
        import index
        return ask(db, index)

    def _edited(self, cid, created, updated):
        """True when a comment was changed after it was posted: GitHub's two times differ, or one is missing; but not
        when the index read the comment's edit history for this updated_at and every edit is by an allowed account."""
        if created and updated and created == updated:
            return False
        if not created or not updated or not self.allowed:
            return True
        found = self.remember(("cleared",), lambda: self._from_index(
            lambda db, index: index.cleared_comments(db, self.allowed)))
        return found is UNKNOWN or found.get(cid) != updated

    def _memory(self, cid):
        if not self.online:
            row = self.db.execute("SELECT e.type, e.at, e.issue, e.author, c.created_at, c.updated_at FROM events e "
                                  "LEFT JOIN comments c ON c.id = e.comment WHERE e.comment = ?", (cid,)).fetchone()
            if row is None:
                return None
            return {"type": row["type"], "at": row["at"] or "", "time": record_format.instant(row["at"], row["created_at"]),
                    "issue": row["issue"], "author": row["author"] or "",
                    "edited": self._edited(cid, row["created_at"], row["updated_at"])}
        try:
            data = self.store.read_entry(cid)
        except recortex.GhError as error:
            if self.store.is_missing(error):
                return None
            raise
        except ValueError:
            raise recortex.GhError("GitHub's answer for comment %d could not be read" % cid)
        if not isinstance(data, dict):
            raise recortex.GhError("GitHub's answer for comment %d could not be read" % cid)
        match = re.search(r"/issues/([0-9]+)$", data.get("issue_url") or "")
        if not match:
            return None  # not a comment of an issue or pull request
        event = record_format.parse_event(data.get("body") or "")
        at = record_format.event_at(event, data.get("created_at"))
        return {"type": event["type"], "at": at or "", "time": record_format.instant(at, data.get("created_at")),
                "issue": int(match.group(1)), "author": (data.get("user") or {}).get("login") or "",
                "edited": self._edited(cid, data.get("created_at"), data.get("updated_at"))}

    def _steps(self, cid):
        found = self.memory(cid)
        if found is None:
            return None
        if found["type"] != "steps":
            return dict(found, count=0, active=False)
        issue = self.issue(found["issue"])
        active = bool(issue) and any(label.lower() == "active" for label in issue["labels"])
        if not self.online:
            import index
            if not index.derived_current(self.db):
                return UNKNOWN   # an index that an earlier version derived holds no steps yet: it cannot say
            count = self.db.execute("SELECT COUNT(*) AS n FROM steps WHERE comment = ?", (cid,)).fetchone()["n"]
        else:
            try:
                data = self.store.read_entry(cid)
            except ValueError:
                raise recortex.GhError("GitHub's answer for comment %d could not be read" % cid)
            count = len(record_format.steps_list((data or {}).get("body") or "")) if isinstance(data, dict) else 0
        return dict(found, count=count, active=active)

    def _issue(self, number):
        if not self.online:
            row = self.db.execute("SELECT labels, is_pr FROM issues WHERE number = ?", (number,)).fetchone()
            if row is None:
                return None
            blocks = {"table": set(), "figure": set()}
            for block in self.db.execute("SELECT kind, id FROM blocks WHERE issue = ?", (number,)):
                blocks.setdefault(block["kind"], set()).add(block["id"])
            try:
                labels = [str(label) for label in json.loads(row["labels"] or "[]")]
            except (ValueError, TypeError):
                labels = []
            return {"labels": labels, "pull": bool(row["is_pr"]), "blocks": blocks}
        kind, data = recortex.issue_kind(self.repo, number)
        if kind is None:
            return None
        blocks = {"table": set(), "figure": set()}
        if kind == "issue":
            for block in record_format.blocks(data.get("body") or ""):
                blocks[block["kind"]].add(block["id"])
        return {"labels": [label.get("name", "") for label in data.get("labels") or []], "pull": kind == "pull",
                "blocks": blocks}

    def _topic_issue(self, number):
        if self.online:
            found = self.issue(number)
            if found is None:
                return []
            if "final-record" in {label.lower() for label in found["labels"]}:
                return [number]
            return self._from_index(lambda db, index: index.topic_issues(db, number))
        import index
        return index.topic_issues(self.db, number)


def github(repo, number):
    """A lookup that reads GitHub, and the local index for what GitHub cannot answer in one call."""
    return Lookup(repo, number, online=True)


def local(repo, number):
    """A lookup that reads the local index only. Raises Unavailable when there is no index, it cannot be read, or a
    version of the plugin without these tables wrote it last."""
    import index
    import storage
    try:
        db = index.connect(repo, create=False)
    except storage.StorageError as error:
        raise Unavailable("the local index of %s cannot be read (%s)" % (repo, error.kind or "refused"))
    if db is None:
        raise Unavailable("there is no local index of %s; run index.py sync" % repo)
    if not index.mind_ready(db):
        db.close()
        raise Unavailable(index.NOT_READY)
    return Lookup(repo, number, online=False, db=db)

#!/usr/bin/env python3
"""The mind of a project: what the mind page tells, derived from the local index alone.

Python 3 standard library only. No network, no hook, no write. build(db, repo, allowed, today) reads the index in one
transaction and returns one dict that json.dumps can print; mind.py renders it and derives nothing itself.

Who is told (references/mind.md): the memories of allowed accounts that were never edited, or edited only by allowed
accounts (their edit history read), as index.memories decides;
a topic body only when an allowed account wrote it, its edit history is read and every edit was made by an allowed
account; another issue's title only under the title rule of the brief. Everything else is counted, never told.

Every string the model takes from the index passes clean(). Text is kept as text: the model never builds markup and
never reads an address from the index (no url or image column); the page builds every address from checked parts.

Fixed wording lives in PHRASES (the sentences, the link words, the chips) and FIELD_NAMES (the human names of the
fields of a memory). Record text is structured for the page as a doc: a list of blocks, each a dict with one key
("p", "ul", "quote" or "table"), whose inline parts are runs, each a dict with one key: "t" (text), "code", "w" (a
project word, with its number "n") or "cite" (a citation told in words, with "n" and "text").
"""

import datetime
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decision_impact  # noqa: E402
import index  # noqa: E402
import mind_plan  # noqa: E402
import mind_sheet  # noqa: E402
import mind_text  # noqa: E402
import project_map  # noqa: E402
import record_store  # noqa: E402
import recortex  # noqa: E402
import record_format  # noqa: E402
import task_rules  # noqa: E402

CAP_MEMORIES = 2000       # the newest told memories on the page; older ones are counted
CAP_MAP_THREADS = 24      # rows of the map; the quietest threads fold into one row
CAP_WORDS = 400
CAP_FIELD = 1200          # characters of a field
CAP_STORY = 3000          # characters of a word's longer story
CAP_SECTION = 6000        # characters of a section of a topic body in a field or a first line
CAP_TEXT = 40000          # characters of a section of a topic body in the view of the text
CAP_TABLE_ROWS = 40
CAP_TABLE_COLUMNS = 12
CAP_FIRST_LINES = 600     # characters of a cited issue's first lines
CAP_LINE = 300            # characters of a name, a title, a plain line or a pause line

KINDS = ("result", "decision", "failure", "idea", "question", "correction", "checkpoint", "text")  # the kinds the page knows
# A plan comment is not one of them: the plan is told in its own part of the model (mind_plan.py), not as a card.
THREAD_KINDS = ("decision", "result", "correction", "failure")   # what can make a part's text out of date

PHRASES = {
    # a sentence: the lead is fixed, the words are the record's, the full stop is the page's
    "lead.result": "On %s we found that ",
    "lead.decision": "On %s we decided to ",
    "lead.failure": "On %s, ",
    "lead.idea": "On %s we had the idea to ",
    "lead.question": "On %s we asked ",
    "lead.correction": "On %s we corrected ourselves: ",
    "lead.checkpoint": "On %s we paused here: ",
    # a memory without a plain line: the lead, then the record's own short words in quotation marks
    "old.result": "On %s we recorded a result, noted as ",
    "old.decision": "On %s we decided: ",
    "old.failure": "On %s something went wrong: ",
    "old.idea": "On %s we noted an idea: ",
    "old.question": "On %s we noted a question: ",
    "old.correction": "On %s we corrected ourselves: ",
    "old.checkpoint": "On %s we paused: ",
    # two linked memories told as one passage
    "join.because": ", so ",
    "join.instead": " That was set aside. Instead, ",
    "join.replaces": " Later that changed: ",
    "join.checks.idea": " We tried it: ",
    "join.checks.question": " The answer came later: ",
    "join.checks.decision": " We checked it: ",
    # the words between two memories of a chain, and before a linked memory
    "link.because": "so",
    "link.instead": "that was set aside; instead",
    "link.replaces": "later that changed",
    "link.checks.idea": "we tried it",
    "link.checks.question": "the answer",
    "link.checks.decision": "we checked it",
    "link.back": ", going back to ",
    "link.thread": ", from the thread ",
    "link.then": "then",
    # chips
    "chip.replaced": "Replaced on %s",
    "chip.aside": "Set aside on %s",
    "chip.answered": "Answered on %s",
    "chip.tried": "Tried on %s",
    "chip.question": "Still open",
    "chip.idea": "Not tested yet",
    "chip.taken": "Taken up on %s",
    "chip.nothing": "Nothing followed yet",
    "chip.led": "Led to a change",
    "chip.stands": "Still stands",
    "latest.decision": "The latest decision",
    "latest.result": "The latest result",
    "latest.replaced": "; it replaced an earlier one",
    # threads
    "status.aside": "Set aside",
    "status.open": "1 open point",
    "status.opens": "%d open points",
    "status.settled": "Nothing open",
    "thread.loose": "Other notes",
    "thread.unnamed": "a thread",
    # what stands in for text that is not told
    "not told": "an earlier memory that is not told here",
    "cite.issue": "an issue on GitHub",
    "cite.comment": "a comment elsewhere on GitHub",
    "cite.untold": "a comment that is not told here",
    "cite.memory": "the memory of %s",
    "cite.someone": "an issue someone else opened",
    "cite.pull": "A pull request",
    "account": "an account",
    "day.unknown": "an unknown day",
    "short.title": "In short", "short.asof": "As of %s.",
    "short.none": "No summary of where the project stands has been written yet. The next resume block carries one.",
}

# the "What changed" lines versions.py posts for a hand edit and for a file kept later: the page tells them in fixed words
TEXT_FORMS = {"The author edited the text by hand.": "lead.text.hand",
              "Nothing; the file of the earlier text change was kept afterwards.": "lead.text.kept"}
PHRASES.update(mind_plan.PHRASES)
PHRASES.update(mind_sheet.PHRASES)

FIELD_NAMES = {
    "Exact words": "In their exact words",
    "Decided by": "Who decided",
    "Effect": "What it changes",
    "What failed": "What we saw",
    "Cause": "Why it happened",
    "Impact": "What it cost",
    "Not established": "What this does not show",
    "Was": "What we said before",
    "Is": "What is right",
    "Why": "How the mistake happened",
    "Numbers": "The numbers",
}
KIND_FIELD_NAMES = {"text": {"Why": "Why we changed it", "Approved": "How it was approved", "Section": "Which part",
                             "What changed": "What changed", "Kept from the author": "What we left as you wrote it"}}
HIDDEN_FIELDS = {"text": ("Body", "Version")}   # hashes and commits are never drawn on the page
WHERE_FIELDS = ("Corrects", "Where recorded")          # shown under "Where this was said"
PEOPLE_FIELDS = ("Decided by",)
OLD_FIELD = {"decision": "Decision", "failure": "What failed", "correction": "Is", "checkpoint": "State"}
LINK_ORDER = ("because", "instead", "replaces", "checks")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")

BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
WALL = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:T([0-9]{2}):([0-9]{2}))?")
CODE = re.compile(r"`([^`\n]+)`")
IMAGE_LINE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
SEPARATOR = re.compile(r":?-{3,}:?")
BLOCK_HEADING = re.compile(r"^### (Figure|Table): ([A-Za-z0-9][A-Za-z0-9_.-]*) - ")
HEADING = re.compile(r"^#{1,3} ")
GOAL = "goal"
PLAN_LABEL = "plan:paper"   # the label the plan issue's thread goes by on the page (not a label of GitHub)


def clean(value):
    """A string from the record as the model keeps it: CR LF and lone CR become LF, the line and paragraph
    separators become LF, and control characters and characters that reorder or hide text become U+FFFD."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    return BAD.sub("�", text)


def short(text, cap=CAP_LINE):
    text = " ".join(clean(text).split())
    return text if len(text) <= cap else text[:cap - 3].rstrip() + "..."


def text_title(body):
    """The title a topic's text gives itself: the first level-1 heading of its body, as the model keeps it, or None when it has
    none. The view's name for a part and the part title of paper.html (mind.py --paper) are this one rule."""
    title = next((line[2:] for line in clean(body).split("\n") if line.startswith("# ")), "")
    return short(title) if title.strip() else None


def login(value):
    """An account name as GitHub spells one, else None (the page says "an account")."""
    value = clean(value)
    return value if LOGIN.fullmatch(value) else None


def wall(at):
    """(year, month, day, hour, minute) of a time as written in the record, without converting its zone, or None."""
    match = WALL.match(clean(at))
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour, minute = int(match.group(4) or 0), int(match.group(5) or 0)
    try:
        datetime.datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    return year, month, day, hour, minute


def wall_key(at):
    parts = wall(at)
    return "%04d-%02d-%02dT%02d:%02d" % parts if parts else ""


def day_words(at, years):
    parts = wall(at)
    if not parts:
        return PHRASES["day.unknown"]
    text = "%s %d" % (MONTHS[parts[1] - 1], parts[2])
    return text + ", %d" % parts[0] if years else text


def full_day(value):
    parts = wall(value)
    return "%s %d, %d" % (MONTHS[parts[1] - 1], parts[2], parts[0]) if parts else PHRASES["day.unknown"]


def lower_first(text):
    return text[:1].lower() + text[1:] if text.startswith("On ") else text


def plural(count, one, many):
    return one if count == 1 else many % count


# ---------------------------------------------------------------- record text as blocks and runs

def split_cells(line):
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def is_table_line(line):
    stripped = line.strip()
    return len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|")


def table_of(lines):
    """(head, rows, cut) of a Markdown table given as its lines (head line, separator, rows), or None."""
    if len(lines) < 2:
        return None
    separator = split_cells(lines[1])
    if not separator or not all(SEPARATOR.fullmatch(cell) for cell in separator if cell):
        return None
    head = split_cells(lines[0])
    rows = [split_cells(line) for line in lines[2:]]
    cut = len(rows) > CAP_TABLE_ROWS or len(head) > CAP_TABLE_COLUMNS or any(
        len(row) > CAP_TABLE_COLUMNS for row in rows)
    return head[:CAP_TABLE_COLUMNS], [row[:CAP_TABLE_COLUMNS] for row in rows[:CAP_TABLE_ROWS]], cut


def first_table(text):
    lines = clean(text).split("\n")
    start = next((i for i, line in enumerate(lines) if is_table_line(line)), None)
    if start is None:
        return None
    end = start
    while end < len(lines) and is_table_line(lines[end]):
        end += 1
    return table_of(lines[start:end])


class Words:
    """The project words of the mind and the matcher that finds them in a text."""

    def __init__(self, entries):
        names = {}
        for n, spelled in entries:
            for name in spelled:
                name = " ".join(clean(name).split())
                if not name or len(name) > 60:
                    continue
                key = name if len(name) < 4 else name.lower()
                names.setdefault(key, (name, n))
        ordered = sorted(names.values(), key=lambda pair: (-len(pair[0]), pair[0]))
        self.owner, parts = {}, []
        for number, (name, n) in enumerate(ordered):
            group = "g%d" % number
            # the words of a name are joined by a space or a hyphen, and a final "s" or "es" still matches
            body = "(?:[ ]+|-)".join(re.escape(word) for word in re.split(r"[ -]+", name) if word)
            if name[-1].isalpha():
                body += "(?:e?s)?"
            parts.append("(?P<%s>%s)" % (group, body if len(name) < 4 else "(?i:%s)" % body))
            self.owner[group] = n
        # a letter, a digit or a hyphen right before or after a name makes it part of another word
        self.pattern = re.compile(r"(?<![A-Za-z0-9-])(?:%s)(?![A-Za-z0-9-])" % "|".join(parts)) if parts else None

    def find(self, text, seen):
        """[(start, end, n)] of the words in `text`, the first appearance of each word not already in `seen`."""
        out = []
        if self.pattern is None:
            return out
        for match in self.pattern.finditer(text):
            n = self.owner[match.lastgroup]
            if n in seen:
                continue
            seen.add(n)
            out.append((match.start(), match.end(), n))
        return out


# ---------------------------------------------------------------- the build

def build(db, repo, allowed, today, profiles=(), config=None):
    """The model of the mind of `repo` (a dict that json.dumps can print), read from the open index `db` in one
    transaction that is rolled back. `allowed` is recortex.allowed_users(repo) and must not be empty; `today` is the
    day of the build (a datetime.date), from which ages are counted; `profiles` is recortex.shipped_profiles of the
    project file's profiles, which give the deliverable's sections. `config` is the project file as the hooks parse it:
    with it a project that has a plan gets the plan in the model (mind_plan.py); without it the model is what it was."""
    if not allowed:
        raise ValueError("the mind is built only for a repository in the user's trusted list")
    began = not db.in_transaction
    if began:
        db.execute("BEGIN")
    try:
        return Build(db, repo, {name.lower() for name in allowed}, today, list(profiles), config).model()
    finally:
        if began and db.in_transaction:
            db.execute("ROLLBACK")


class Build(mind_plan.PlanMixin, mind_sheet.SheetMixin):
    def __init__(self, db, repo, allowed, today, profiles=(), config=None):
        self.db, self.repo, self.allowed, self.today, self.profiles = db, repo, allowed, today, list(profiles)
        self.config = config
        self.cited, self.first_lines = {}, []

    def ok(self, author):
        return bool(author) and clean(author).lower() in self.allowed

    def meta(self, key):
        return clean(index.get_meta(self.db, key) or "")

    # ------------------------------------------------------------ issues

    def read_issues(self):
        self.issues = {}
        for row in self.db.execute("SELECT number, title, state, is_pr, author, body, created_at, closed_at, "
                                   "last_edited_at, timeline_read, edits_read FROM issues"):
            self.issues[row["number"]] = {"number": row["number"], "title": row["title"] or "", "state": row["state"],
                                          "pull": bool(row["is_pr"]), "author": row["author"], "body": row["body"] or "",
                                          "created": row["created_at"], "closed": row["closed_at"],
                                          "edited": row["last_edited_at"],
                                          "timeline_read": bool(row["timeline_read"]),
                                          "edits_read": bool(row["edits_read"]), "renamers": [], "editors": []}
        for row in self.db.execute("SELECT issue, event, actor FROM timeline WHERE event IN ('renamed', 'edited')"):
            issue = self.issues.get(row["issue"])
            if issue is not None:
                issue["renamers" if row["event"] == "renamed" else "editors"].append(row["actor"])
        self.labels = {}
        for row in self.db.execute("SELECT issue, label FROM labels ORDER BY issue, label"):
            self.labels.setdefault(row["issue"], []).append(row["label"] or "")
        self.parent = {row["issue"]: row["parent"] for row in self.db.execute("SELECT issue, parent FROM parents")
                       if row["parent"] is not None}
        hub = self.meta("hub")
        self.hub = int(hub) if hub.isdigit() else None

    def body_shown(self, number):
        """The topic-body rule: an allowed account opened the issue, its edit history is read, and every edit of
        its body was made by an allowed account."""
        issue = self.issues.get(number)
        return bool(issue) and self.ok(issue["author"]) and issue["edits_read"] and all(
            self.ok(actor) for actor in issue["editors"])

    def title_shown(self, number):
        """The title rule of the brief: an allowed account opened it, its history is read, nobody else renamed it."""
        issue = self.issues.get(number)
        return bool(issue) and self.ok(issue["author"]) and issue["timeline_read"] and all(
            self.ok(actor) for actor in issue["renamers"])

    def has_label(self, number, name):
        return name in {label.lower() for label in self.labels.get(number, [])}

    # ------------------------------------------------------------ memories

    def read_memories(self):
        found = index.memories(self.db, self.allowed)
        every = sorted((m for m in found["items"].values() if m["kind"] in KINDS), key=lambda m: (m["time"], m["id"]))
        # a kind this version does not know: left out, counted. A plan comment is known: the plan part of the model tells it,
        # and a steps memory is known: the to-do view tells it.
        self.unknown_kinds = len([m for m in found["items"].values() if m["kind"] not in KINDS
                                  and m["kind"] not in ("plan", "steps")])
        self.older = max(0, len(every) - CAP_MEMORIES)
        kept = every[self.older:]
        self.found = found
        self.years = len({wall(m["at"])[0] for m in kept if wall(m["at"])}) > 1
        self.fields = {}
        for row in self.db.execute("SELECT comment, ordinal, label, text FROM event_fields ORDER BY comment, ordinal"):
            if row["comment"] in found["items"]:
                self.fields.setdefault(row["comment"], []).append((clean(row["label"]), clean(row["text"])))
        self.evidence = {}
        for row in self.db.execute("SELECT comment, ordinal, kind, ident, repo, ref, path, line_from, line_to "
                                   "FROM evidence ORDER BY comment, ordinal"):
            if row["comment"] in found["items"]:
                self.evidence.setdefault(row["comment"], []).append(row)
        self.m = {}
        posted = {row["comment"]: clean(row["created_at"] or "")
                  for row in self.db.execute("SELECT comment, created_at FROM events")}
        strange = {cid for cid, m in found["items"].items() if m["kind"] not in KINDS}  # the plan comments
        self.plan_links = {item["id"]: [(key, target) for key, target in item["links"] if target in strange]
                           for item in kept if any(target in strange for _, target in item["links"])}
        for item in kept:
            self.m[item["id"]] = {
                "id": item["id"], "issue": item["issue"], "kind": item["kind"], "at": clean(item["at"]),
                "time": item["time"], "wall": wall_key(item["at"]), "day": day_words(item["at"], self.years),
                "plain": short(item["plain"]).rstrip(".").strip() or None if item["plain"] is not None else None,
                "title": short(item["title"]), "state": item["state"], "author": login(item["author"]),
                "changed": bool(item.get("changed")),
                "posted": posted.get(item["id"], ""), "reconstructed": bool(item.get("reconstructed")),
                "links": [(key, target) for key, target in item["links"] if key in LINK_ORDER and target not in strange],
            }
        self.order = [self.m[item["id"]] for item in kept]
        self.incoming = {}
        for memory in self.order:
            for key, target in memory["links"]:
                if target in self.m:
                    self.incoming.setdefault(target, []).append((key, memory["id"]))

    def field(self, cid, label):
        return next((text for name, text in self.fields.get(cid, []) if name == label), None)

    def sentence(self, memory):
        kind, day = memory["kind"], memory["day"]
        if kind == "text":   # two changes of the text that the plugin posts in fixed words are told in fixed words
            fixed = TEXT_FORMS.get((self.field(memory["id"], "What changed") or "").strip())
            if fixed:
                return {"lead": PHRASES[fixed] % day, "words": "", "quoted": False}
        if memory["plain"]:
            return {"lead": PHRASES["lead." + kind] % day, "words": memory["plain"], "quoted": False}
        words = self.field(memory["id"], OLD_FIELD.get(kind, "")) if kind in OLD_FIELD else None
        if words and kind == "decision":
            words = re.split(r"(?<=[.!?])\s", words.strip(), maxsplit=1)[0]
        words = short((words or "").strip().split("\n")[0] or memory["title"]).rstrip(".").strip()
        return {"lead": PHRASES["old." + kind] % day, "words": words, "quoted": True}

    @staticmethod
    def text_of(sentence):
        if sentence["quoted"]:
            return sentence["lead"] + "“" + sentence["words"] + ".”"
        return sentence["lead"] + sentence["words"] + "."

    def link_phrase(self, key, target):
        if key == "checks":
            kind = self.m[target]["kind"] if target in self.m else ""
            return PHRASES.get("link.checks." + kind, PHRASES["link.checks.decision"])
        return PHRASES["link." + key]

    def joined(self, earlier, key, later):
        first, second = self.text_of(earlier["sentence"]), self.text_of(later["sentence"])
        if key == "because":
            return first[:-1] + PHRASES["join.because"] + lower_first(second)
        if key == "checks":
            return first + PHRASES.get("join.checks." + earlier["kind"], PHRASES["join.checks.decision"]) + \
                lower_first(second)
        return first + PHRASES["join." + key] + lower_first(second)

    def passage(self, memory):
        """The memory told with what led to it, or else with what came of it; a memory without a plain line is never
        joined."""
        if not memory["old"]:
            for key, target in sorted(memory["links"], key=lambda pair: LINK_ORDER.index(pair[0])):
                other = self.m.get(target)
                if other is not None and not other["old"]:
                    return self.joined(other, key, memory)
            for key, source in self.incoming.get(memory["id"], []):
                other = self.m[source]
                if not other["old"]:
                    return self.joined(memory, key, other)
        return self.text_of(memory["sentence"])

    def state_of(self, memory):
        """(css, chip) of a memory: css is one of stands, replaced, aside, answered, tried, taken, open, led, pause,
        plain. A checkpoint is a pause, not a claim: it has no chip."""
        incoming = self.incoming.get(memory["id"], [])

        def first(key, kinds=None):
            sources = sorted((self.m[s] for k, s in incoming if k == key and (kinds is None or self.m[s]["kind"] in kinds)),
                             key=lambda m: (m["time"], m["id"]))
            return sources[0]["day"] if sources else PHRASES["day.unknown"]

        state, kind = memory["state"], memory["kind"]
        if kind == "checkpoint":
            return "pause", ""
        if kind == "text":
            return "plain", ""
        if state == "replaced":
            return "replaced", PHRASES["chip.replaced"] % first("replaces")
        if state == "set aside":
            return "aside", PHRASES["chip.aside"] % first("instead")
        if state == "answered":
            return "answered", PHRASES["chip.answered"] % first("checks")
        if state == "tried":
            return "tried", PHRASES["chip.tried"] % first("checks")
        if state == "taken up":
            return "taken", PHRASES["chip.taken"] % first("because", ("decision", "result"))
        if state == "open":
            return "open", PHRASES["chip.question"]
        if state == "untried":
            return "open", PHRASES["chip.idea"]
        if memory["old"] and not incoming and not memory["links"]:
            return "plain", ""
        if kind == "failure":
            if incoming:
                return "led", PHRASES["chip.led"]
            return "open", PHRASES["chip.nothing"]
        return "stands", PHRASES["chip.stands"]

    @staticmethod
    def explain_of(memory):
        """The key of the fixed explanation behind a memory's chip (mind.py holds the entries), or None."""
        css, kind = memory["css"], memory["kind"]
        if css == "open":
            return {"idea": "state-untested", "failure": "state-nothing"}.get(kind, "state-open")
        return {"stands": "state-stands", "replaced": "state-replaced", "aside": "state-aside",
                "answered": "state-answered", "tried": "state-tried", "taken": "state-taken", "led": "state-led",
                "pause": "kind-checkpoint"}.get(css)

    # ------------------------------------------------------------ text with words and citations

    def runs(self, text, seen):
        """The inline runs of one line or paragraph of record text: the reader's runs (bold, code, formulas, links)
        with project words and citations marked in the text between them."""
        return self.marked(mind_text.inline(clean(text)), seen)

    def marked(self, runs, seen):
        """`runs` of mind_text with the project words and citations marked inside each text run."""
        out = []
        for run in runs:
            if "t" in run:
                flags = {key: True for key in ("b", "i", "sup", "sub") if run.get(key)}
                for piece in self.text_runs(run["t"], seen):
                    piece.update(flags)
                    out.append(piece)
            elif "link" in run:
                out.append(dict(run, link=self.marked(run["link"], seen)))
            else:
                out.append(run)
        return out

    def text_runs(self, text, seen):
        spans = [(item["start"], item["end"], self.citation(item, text[item["start"]:item["end"]]))
                 for item in record_format.citations(text, self.repo, local=record_store.is_local(self.repo))]
        out, at = [], 0
        for start, end, run in sorted(spans, key=lambda span: span[0]):
            if start < at:
                continue
            self.plain_runs(text[at:start], seen, out)
            out.append(run)
            at = end
        self.plain_runs(text[at:], seen, out)
        return out

    def plain_runs(self, text, seen, out):
        at = 0
        for start, end, n in self.words.find(text, seen):
            if start > at:
                out.append({"t": text[at:start]})
            out.append({"w": text[start:end], "n": n})
            at = end
        if at < len(text):
            out.append({"t": text[at:]})

    def citation(self, item, written):
        if item["kind"] == "memory":
            target = self.m.get(item["comment"])
            if target is None:
                return {"cite": "none", "n": 0, "text": PHRASES["cite.untold"]}
            return {"cite": "memory", "n": target["id"], "text": PHRASES["cite.memory"] % target["day"]}
        if item["kind"] == "elsewhere":
            word = "cite.comment" if "#issuecomment-" in written else "cite.issue"
            return {"cite": "none", "n": 0, "text": PHRASES[word]}
        number = item["issue"]
        thread = self.topic_thread.get(number)
        if thread is not None:
            return {"cite": "thread", "n": thread["n"], "text": thread["name"]}
        if number not in self.issues:
            return {"cite": "none", "n": 0, "text": PHRASES["cite.issue"]}
        return {"cite": "issue", "n": number, "text": self.cite_issue(number)}

    def cite_issue(self, number):
        if number not in self.cited:
            issue = self.issues[number]
            shown = self.title_shown(number)
            title = short(issue["title"]) if shown and issue["title"].strip() else (
                PHRASES["cite.pull"] if issue["pull"] else PHRASES["cite.someone"])
            self.cited[number] = {"n": number, "title": title, "pull": issue["pull"],
                                  "author": login(issue["author"]), "opened": full_day(issue["created"]),
                                  "open": issue["state"] == "open", "closed": full_day(issue["closed"])
                                  if issue["state"] != "open" and issue["closed"] else None, "first": None}
            if not issue["pull"] and self.body_shown(number):
                lines = [re.sub(r"^#{1,6}\s+", "", line) for line in clean(issue["body"]).split("\n")
                         if not line.lstrip().startswith("<!--")]
                text = "\n".join(lines).strip()
                if text:
                    self.first_lines.append((number, text))  # read by read_first_lines: a body may cite further issues
        return self.cited[number]["title"]

    def read_first_lines(self):
        """The first lines of the cited issues, one after another and not one inside the other, so that a long chain
        of issues that cite each other is walked without recursion."""
        while self.first_lines:
            number, text = self.first_lines.pop(0)
            self.cited[number]["first"] = self.doc(text, set(), cap=CAP_FIRST_LINES)

    def doc(self, text, seen, cap=CAP_FIELD):
        """Record text as blocks (mind_text.blocks) with project words and citations marked in their runs. Cut at
        its last line break before `cap` characters, so that a table loses whole rows; when that break lies in the
        first half, at the last space instead, and else at `cap`; then "cut" is true."""
        text = clean(text).strip("\n")
        cut = len(text) > cap
        if cut:
            text = text[:cap]
            stop = text.rfind("\n")
            if stop < cap // 2:
                stop = text.rfind(" ")  # a short first line before a long paragraph keeps the paragraph's start
            text = text[:stop] if stop >= cap // 2 else text
        return {"blocks": [self.block(block, seen) for block in mind_text.blocks(text)], "cut": cut}

    def block(self, block, seen):
        """One block of mind_text with its runs marked."""
        if "p" in block or "quote" in block:
            key = "p" if "p" in block else "quote"
            return {key: self.marked(block[key], seen)}
        if "h" in block:
            return {"h": {"level": block["h"]["level"], "r": self.marked(block["h"]["r"], seen)}}
        for key in ("ul", "ol"):
            if key in block:
                items = [{"r": self.marked(item["r"], seen), "sub": self.block(item["sub"], seen) if item["sub"] else None}
                         for item in block[key]]
                return dict({key: items}, **({"start": block["start"]} if "start" in block else {}))
        if "table" in block:
            table = block["table"]
            cells = lambda rows: [[dict(cell, r=self.marked(cell["r"], seen)) for cell in row] for row in rows]
            return {"table": {"head": cells(table["head"]), "rows": cells(table["rows"]), "cut": table["cut"],
                              "caption": self.marked(table["caption"], seen) if table.get("caption") else None}}
        return block

    # ------------------------------------------------------------ threads

    def topic_labels(self, number):
        return sorted(label for label in self.labels.get(number, []) if label.lower().startswith("topic:"))

    def is_topic(self, number):
        """An issue that holds deliverable text: a final-record issue that is no pull request and not the hub."""
        issue = self.issues.get(number)
        return bool(issue) and not issue["pull"] and number != self.hub and self.has_label(number, "final-record")

    def read_threads(self):
        """Threads, one per topic: label. An issue belongs to the thread of the nearest topic above it in `parents`
        (up to four steps, a loop stops the climb); else to the thread of its own first topic: label. With several
        labels and no such parent, it goes to the first by name, and that thread carries the others as `also`."""
        belongs, also = {}, {}
        for number in sorted(self.issues):
            label, current, seen = None, number, {number}
            for _ in range(4):
                current = self.parent.get(current)
                if current is None or current in seen:
                    break
                seen.add(current)
                if self.is_topic(current) and self.topic_labels(current):
                    label = self.topic_labels(current)[0]
                    break
            mine = self.topic_labels(number)
            if label is None and mine:
                label = mine[0]
                if len(mine) > 1:
                    also.setdefault(label, set()).update(mine[1:])
            if label is not None:
                belongs[number] = label
        if self.plan_issue in self.issues:
            belongs[self.plan_issue] = PLAN_LABEL  # the plan issue is the thread of the plan of the paper
        self.belongs = belongs
        labels = sorted(set(belongs.values()))
        threads, taken = [], set()
        for label in labels:
            issues = sorted(n for n, mine in belongs.items() if mine == label)
            finals = [n for n in issues if self.is_topic(n) and self.issues[n]["state"] == "open"]
            closed = [n for n in issues if self.issues[n]["state"] != "open" and not self.issues[n]["pull"]
                      and n != self.hub and not self.has_label(n, "active")]  # a closed topic names a set-aside thread
            threads.append({"label": label, "issues": issues, "topic": finals[0] if finals else None,
                            "closed_topic": closed[0] if closed else None, "own": issues,
                            "also_labels": sorted(also.get(label, ()))})
        for memory in self.order:
            memory["thread_label"] = belongs.get(memory["issue"])
        loose = [m for m in self.order if m["thread_label"] is None]
        for thread in threads:
            thread["memories"] = [m for m in self.order if m["thread_label"] == thread["label"]]
        threads = [t for t in threads if t["memories"] or t["topic"] is not None or t["label"] == PLAN_LABEL]
        if loose:
            threads.append({"label": None, "issues": sorted({m["issue"] for m in loose}), "topic": None,
                            "closed_topic": None, "own": [], "memories": loose, "also_labels": []})
        position = {m["id"]: i for i, m in enumerate(self.order)}
        threads.sort(key=lambda t: (t["label"] is None, position[t["memories"][0]["id"]] if t["memories"]
                                    else len(self.order), t["label"] or ""))
        for count, thread in enumerate(threads):
            candidates = ([thread["topic"]] if thread["topic"] else []) + thread["own"] + thread["issues"]
            if thread["label"] is None:
                candidates = [0]
            n = next((c for c in candidates if c not in taken), 10 ** 10 + count)
            taken.add(n)
            thread["n"] = n
        self.threads = threads
        self.topic_thread = {}
        for thread in threads:
            label = thread["label"]
            shown = thread["topic"] is not None and self.body_shown(thread["topic"])
            name, source = None, "label"
            if shown:
                title = text_title(self.issues[thread["topic"]]["body"])
                if title:
                    name, source = title, "body"
            closed = thread["closed_topic"]
            if name is None and label is not None and thread["topic"] is None and closed is not None \
                    and self.title_shown(closed) and self.issues[closed]["title"].strip():
                name, source = short(self.issues[closed]["title"]), "title"
            if label == PLAN_LABEL:
                name, source = PHRASES["thread.plan"], "fixed"
            elif name is None and label is None:
                name, source = PHRASES["thread.loose"], "fixed"
            elif name is None:
                rest = label.split(":", 1)[1]
                if recortex.LABEL_NAME.fullmatch(label) and rest:
                    name = rest
                else:
                    name, source = PHRASES["thread.unnamed"], "fixed"
            set_aside = label not in (None, PLAN_LABEL) and thread["topic"] is None
            if set_aside and closed is None:
                # An open project task can have its own recorded story before there is any deliverable text.
                set_aside = not any(n != self.hub and not self.issues[n]["pull"] and self.issues[n]["state"] == "open"
                                    and self.ok(self.issues[n]["author"]) and self.has_label(n, "active")
                                    for n in thread["issues"])
            thread.update(name=name, name_from=source, shown=shown, set_aside=set_aside, loose=label is None)
            if thread["topic"] is not None and thread["topic"] not in self.topic_thread:
                self.topic_thread[thread["topic"]] = thread
            for memory in thread["memories"]:
                memory["thread"] = thread["n"]
        self.thread_of = {t["n"]: t for t in threads}
        self.thread_by_label = {t["label"]: t["n"] for t in threads if t["label"] is not None}
        for thread in threads:
            thread["also"] = [self.thread_by_label[l] for l in thread["also_labels"] if l in self.thread_by_label]

    # ------------------------------------------------------------ words and openings

    def read_words(self):
        rows = index.explained(self.db, self.allowed, "word")
        by_key = {}
        for row in rows:
            by_key.setdefault(row["key"], []).append(row)
        keys = sorted(by_key)[:CAP_WORDS]
        self.words_cut = max(0, len(by_key) - CAP_WORDS)
        self.word_rows = [(n, by_key[key]) for n, key in enumerate(keys, 1)]
        entries = []
        for n, rows_of_key in self.word_rows:
            newest = rows_of_key[-1]
            entries.append((n, [newest["word"]] + list(newest["also"])))
        self.words = Words(entries)
        self.openings = {}
        for row in index.explained(self.db, self.allowed, "thread"):  # oldest first: the newest stands
            n = self.thread_by_label.get(self.belongs.get(row["issue"]))
            if n is not None:
                self.openings[n] = row

    # ------------------------------------------------------------ the parts of the model

    def memory_model(self, memory):
        cid = memory["id"]
        seen = set()
        sentence = memory["sentence"]
        sentence["marks"] = [list(span) for span in self.words.find(sentence["words"], seen)]
        fields, where = [], []
        names = KIND_FIELD_NAMES.get(memory["kind"], {})
        for label, text in self.fields.get(cid, []):
            if label == "plain" or (label == "Decision" and memory["plain"]) or label in HIDDEN_FIELDS.get(memory["kind"], ()):
                continue
            if label in PEOPLE_FIELDS:
                entry = {"label": label, "name": FIELD_NAMES.get(label, label), "type": "people",
                         "people": short(text, CAP_FIELD)}
            elif label == "Numbers":
                table = first_table(text)
                entry = {"label": label, "name": FIELD_NAMES[label], "type": "numbers",
                         "doc": self.doc(text, seen)}
                entry["type"] = "numbers" if table else "text"
            else:
                entry = {"label": label, "name": names.get(label) or FIELD_NAMES.get(label, label),
                         "type": "quote" if label == "Exact words" else "text", "doc": self.doc(text, seen)}
            (where if label in WHERE_FIELDS else fields).append(entry)
        out = []
        for key, target in memory["links"]:
            other = self.m.get(target)
            out.append({"kind": key, "id": target, "told": other is not None, "phrase": self.link_phrase(key, target)})
        incoming = [{"kind": key, "id": source, "phrase": self.link_phrase(key, cid)}
                    for key, source in sorted(self.incoming.get(cid, []), key=lambda p: (self.m[p[1]]["time"], p[1]))]
        parts = wall(memory["at"])
        thread = self.thread_of[memory["thread"]]
        return {
            "id": cid, "issue": memory["issue"], "thread": memory["thread"], "kind": memory["kind"],
            "at": memory["at"], "day": memory["day"], "plain": memory["plain"], "title": memory["title"],
            "event_time": memory["time"] if index.ISO_UTC.fullmatch(memory["time"]) else None,
            "posted": memory["posted"] if index.ISO_UTC.fullmatch(memory["posted"]) else None,
            "reconstructed": memory["reconstructed"],
            "time_basis": "event" if re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", memory["at"]) else "posting",
            "old": memory["old"], "changed": memory["changed"], "state": memory["state"], "css": memory["css"], "chip": memory["chip"],
            "latest": memory.get("latest", ""), "latest_note": memory.get("latest_note", ""),
            "explain": memory["explain"], "group": memory["group"], "sentence": sentence,
            "passage": memory["passage"], "out": out, "in": incoming, "fields": fields, "where": where,
            "evidence": [self.evidence_model(row, thread) for row in self.evidence.get(cid, [])],
            "author": memory["author"],
            "written": {"time": "%02d:%02d" % parts[3:5] if parts else None, "day": full_day(memory["at"])},
            "side": memory["side"], "remind": self.remind(memory),
            "plan_note": self.plan_note(cid),
        }

    def plan_note(self, cid):
        """"bore" when this memory carries checks= to a planned part, "against" when it carries against=, else None (the
        page says it in one fixed sentence on the card)."""
        found = self.plan_links.get(cid, []) if self.plan_state is not None and self.m[cid]["kind"] == "result" else []
        if any(key == "against" for key, _ in found):
            return "against"
        return "bore" if any(key == "checks" for key, _ in found) else None

    def block_parts(self, number):
        """{(kind, id): block} of the figure and table blocks of a topic body, from the index, with the table cells
        and the figure's repository read from the body."""
        body = clean(self.issues[number]["body"])
        lines = body.split("\n")
        texts, repos = {}, {}
        for i, line in enumerate(lines):
            match = BLOCK_HEADING.match(line)
            if match:
                end = i + 1
                while end < len(lines) and not HEADING.match(lines[end]):
                    end += 1
                texts[(match.group(1).lower(), match.group(2))] = "\n".join(lines[i + 1:end])
        for block in record_format.blocks(body):
            repos[(block["kind"], block["id"])] = block["repo"]
        out = {}
        for row in self.db.execute("SELECT kind, id, title, section, caption, ref, path, data, command FROM blocks "
                                   "WHERE issue = ? ORDER BY rowid", (number,)):
            key = (row["kind"], row["id"])
            entry = {"kind": row["kind"], "id": short(row["id"], 60), "title": short(row["title"]),
                     "section": clean(row["section"] or ""), "caption": short(row["caption"], CAP_FIELD),
                     "data": short(row["data"] or ""), "command": short(row["command"] or "")}
            if row["kind"] == "table":
                found = [block for block in mind_text.blocks(texts.get(key, "")) if "table" in block]
                entry["table"] = self.block(found[0], set())["table"] if found else None
            else:
                entry.update(repo=clean(repos.get(key) or ""), ref=clean(row["ref"] or ""),
                             path=clean(row["path"] or ""))
            out[key] = entry
        return out

    def evidence_model(self, row, thread):
        kind = row["kind"]
        if kind in ("table", "figure"):
            topic = thread["topic"]
            if topic is None or not thread["shown"]:
                return {"kind": kind, "found": False, "shown": False}  # no text of this thread is on the page
            block = self.blocks_of(topic).get((kind, row["ident"]))
            if block is None:
                return {"kind": kind, "found": False, "shown": True}
            headings = self.heading_list(topic)
            ordinal = headings.index(block["section"].lower()) if block["section"].lower() in headings else None
            return dict(block, found=True, shown=True, place={"thread": thread["n"], "ordinal": ordinal})
        if kind == "code":
            return {"kind": "code", "repo": clean(row["repo"] or "") or self.repo, "ref": clean(row["ref"] or ""),
                    "path": clean(row["path"] or ""), "from": row["line_from"], "to": row["line_to"]}
        number = int(row["ident"]) if str(row["ident"]).isdigit() else 0
        other = clean(row["repo"] or "")
        title = PHRASES["cite.pull"]
        if not other and number in self.issues and self.issues[number]["pull"] and self.title_shown(number):
            title = short(self.issues[number]["title"]) or title
        return {"kind": "pull", "repo": other or self.repo, "number": number, "title": title}

    def heading_list(self, number):
        """The lower-case headings of a topic body's sections except Sources, in order: the ordinals of its parts."""
        if not hasattr(self, "heading_cache"):
            self.heading_cache = {}
        if number not in self.heading_cache:
            self.heading_cache[number] = [clean(row["heading"]).lower() for row in self.db.execute(
                "SELECT heading FROM sections WHERE issue = ? ORDER BY ordinal", (number,))
                if clean(row["heading"]).lower() != recortex.SOURCES_HEADING.lower()]
        return self.heading_cache[number]

    def blocks_of(self, number):
        if not hasattr(self, "block_cache"):
            self.block_cache = {}
        if number not in self.block_cache:
            self.block_cache[number] = self.block_parts(number)
        return self.block_cache[number]

    def remind(self, memory):
        before = [p for p in self.pauses if p["time"] and memory["time"] and p["time"] < memory["time"]
                  and p["id"] != memory["id"]]
        root, hops = memory, 0
        while hops < 12:
            nxt = [self.m[t] for k, t in sorted(root["links"], key=lambda p: LINK_ORDER.index(p[0])) if t in self.m]
            if not nxt:
                break
            root, hops = nxt[0], hops + 1
        thread = self.thread_of[memory["thread"]]["memories"]
        earlier = [m["id"] for m in thread if (m["time"], m["id"]) < (memory["time"], memory["id"])][-2:]
        return {"pause": before[-1]["index"] if before else None, "root": root["id"] if hops > 1 else None,
                "before": earlier}

    def read_pauses(self, tasks=None):
        """Earlier pauses (resume comments of allowed accounts never edited, and told checkpoints) and the last one,
        from meta.resume, which the sync stores only after its author and never-edited checks. The stored block is told
        only when the hub comment that holds it is one of the told pauses of this build: the allowed list may have
        changed since the sync."""
        hub = self.meta("hub")
        hub = int(hub) if hub.isdigit() else None
        edited = index.edited_comments(self.db)
        pauses = []
        resume = clean(index.get_meta(self.db, "resume") or "").strip()
        last_id, last_posted = None, None
        if hub is not None:
            for row in self.db.execute("SELECT e.comment, e.at, e.author, e.created_at, c.body FROM events e JOIN comments c ON "
                                       "c.id = e.comment WHERE e.type = 'resume' AND e.issue = ? ORDER BY e.comment",
                                       (hub,)):
                block = recortex.extract_resume_block(row["body"] or "")
                if block is None:
                    continue
                if resume and clean(block).strip() == resume:
                    last_id = row["comment"]
                    last_posted = row["created_at"]
                if not self.ok(row["author"]) or row["comment"] in edited:
                    continue
                focus = next((text for label, text in record_format.fields(block) if label == "Current focus"), "")
                pauses.append({"kind": "resume", "id": row["comment"], "at": clean(row["at"]),
                               "time": record_format.instant(row["at"], row["created_at"]),
                               "wall": wall_key(row["at"]), "day": day_words(row["at"], self.years),
                               "focus": short(focus, CAP_FIELD)})
        for memory in self.order:
            if memory["kind"] == "checkpoint":
                focus = memory["plain"] or self.field(memory["id"], "State") or memory["title"]
                pauses.append({"kind": "checkpoint", "id": memory["id"], "at": memory["at"], "time": memory["time"],
                               "wall": memory["wall"], "day": memory["day"], "focus": short(focus, CAP_FIELD)})
        pauses.sort(key=lambda p: (p["time"], p["id"]))   # by the instant, not the clock a stamp was written in
        for i, pause in enumerate(pauses):
            pause["index"] = i
        self.pauses = pauses
        self.last, self.pause_untold = None, False
        last = next((p for p in pauses if p["kind"] == "resume" and p["id"] == last_id), None)
        if resume and last is None:
            # The sync stored this block for the allowed list of its day. The hub comment that holds it is not one
            # this build may tell (its author is not allowed now, it was edited, or it is not in the index yet).
            self.pause_untold = True
        elif resume:
            values = dict(record_format.fields(resume))
            seen = set()
            next_rows = []
            candidates = []
            table = first_table(values.get("Next actions", ""))
            if table:
                head = [cell.lower() for cell in table[0]]
                column = lambda name, default: head.index(name) if name in head else default
                spots = (column("action", 0), column("owner", 1), column("where", 2))
                for row in table[1]:
                    cell = lambda i: row[i] if i < len(row) else ""
                    citations = record_format.citations(cell(spots[0]) + " " + cell(spots[2]), self.repo,
                                                        local=record_store.is_local(self.repo))
                    refs = sorted({item["issue"] for item in citations if item["kind"] == "issue"})
                    next_rows.append({"action": self.runs(cell(spots[0]), seen), "owner": short(cell(spots[1])),
                                      "action_text": short(cell(spots[0])), "task_refs": refs, "work_step": None,
                                      "where": self.runs(cell(spots[2]), seen)})
                    action = " ".join(clean(cell(spots[0])).split())
                    if len(refs) == 1 and not any(item["kind"] == "elsewhere" for item in citations) \
                            and re.fullmatch(r"Step [1-9][0-9]*: .+", action):
                        candidates.append((next_rows[-1], action))
            summary = record_format.resume_in_short(resume)
            holds = values.get("Holds", "").strip()
            if holds.casefold().strip(" .-*_`") in ("", "none", "none yet", "no holds", "no holds recorded", "n/a", "nothing"):
                holds = ""
            self.last = {"id": last_id, "day": last["day"], "wall": last["wall"], "time": last["time"],
                         "issue": hub, "automatic": values.get("Last updated", "").strip().endswith(" by the observer"),
                         "posted": self.meta("resume_posted") if index.ISO_UTC.fullmatch(self.meta("resume_posted")) else None,
                         "focus": self.doc(values.get("Current focus", ""), seen),
                         "next": next_rows, "holds": self.doc(holds, seen) if holds else None,
                         # the "In short" of the block, as text: two paragraphs at most, each cut like a field, marked with
                         # the day the block says it was written (told like the block: allowed author, never edited)
                         "summary": {"paras": [short(paragraph, CAP_FIELD) for paragraph in summary.split("\n\n")[:2]],
                                     "asof": full_day(last["at"])} if summary else None}
            if candidates and tasks and tasks["available"] and self.meta("sync_complete") == "1" \
                    and self.meta("derive_pending") != "1" and index.derived_current(self.db) \
                    and last_posted and last_posted == self.last["posted"]:
                self.bind_handoff_steps(candidates, tasks, last_posted)

    def handoff_steps_at(self, issues, boundary):
        """Recover the numbered steps and their marks as they stood when the handoff was posted.

        Task ordering uses the event's stated time, but a backdated comment could only affect a handoff after it
        was actually posted. A later edit to a steps list leaves its earlier wording and numbering unknown.
        """
        items = self.found["items"]
        withheld = set(self.found["edited_ids"])
        mark_kinds = record_format.MARK_RULES["done"] | record_format.MARK_RULES["dropped"]
        marks_known = True
        uncertain, replaced, aside = set(), set(), set()
        earlier = {}
        for row in self.db.execute("SELECT e.comment, e.issue, e.type, e.author, c.created_at, c.updated_at FROM events e "
                                   "LEFT JOIN comments c ON c.id = e.comment"):
            cid, issue = row["comment"], row["issue"]
            posted, updated = row["created_at"] or "", row["updated_at"] or ""
            if index.ISO_UTC.fullmatch(posted) and posted > boundary:
                continue
            item = items.get(cid)
            if self.ok(row["author"]) and posted != updated and not (index.ISO_UTC.fullmatch(updated) and updated < boundary):
                # The old body is unavailable: even an ordinary comment here could have been a steps list.
                uncertain.add(issue)
                if row["type"] in mark_kinds:
                    # An erased mark could have named a step on another issue.
                    marks_known = False
            if item is None:
                if cid in withheld and row["type"] == "steps":
                    uncertain.add(issue)
                continue
            earlier[cid] = dict(item, state="stands")
            if not (index.ISO_UTC.fullmatch(posted) and index.ISO_UTC.fullmatch(updated)
                    and posted <= updated < boundary):
                # Include its current marks conservatively; we cannot use its links to revoke an earlier mark.
                if item["kind"] == "steps":
                    uncertain.add(issue)
                    uncertain.update(items[t]["issue"] for k, t in item["links"] if k == "replaces" and t in items)
                continue
            replaced.update(t for k, t in item["links"] if k == "replaces")
            aside.update(t for k, t in item["links"] if k == "instead")
        for cid, item in earlier.items():
            if cid in replaced or cid in aside:
                item["state"] = "replaced" if cid in replaced else "set aside"

        certain = issues - uncertain
        plans = {cid: {"comment": cid, "time": item["time"], "state": item["state"], "texts": [],
                       "replaces": [t for k, t in item["links"] if k == "replaces"]}
                 for cid, item in earlier.items() if item["kind"] == "steps" and item["issue"] in certain}
        for row in self.db.execute("SELECT comment, text FROM steps ORDER BY comment, ordinal"):
            if row["comment"] in plans:
                plans[row["comment"]]["texts"].append(row["text"])
        marks = [tuple(row) for row in self.db.execute("SELECT source, kind, target, step FROM marks")
                 if row["target"] in plans and row["source"] in earlier]
        out = {}
        for issue in certain:
            numbered = task_rules.numbered([plan for cid, plan in plans.items() if earlier[cid]["issue"] == issue])
            for step in task_rules.assess(numbered, marks, earlier):
                if not earlier[step["comment"]]["reconstructed"]:
                    words = " ".join(clean(step["text"]).split())
                    if self.last["automatic"]:
                        words = words.replace("|", "/")
                    out[(issue, "Step %d: %s" % (step["n"], words))] = step
        return out, marks_known

    def bind_handoff_steps(self, candidates, tasks, boundary):
        """Join an explicit handoff action to a stable steps comment and ordinal, then to its current evidence."""
        current = {task["issue"]: (task, group == "closed") for group in ("open", "closed") for task in tasks[group]}
        issues = {row["task_refs"][0] for row, _ in candidates} & current.keys()
        if not issues:
            return
        history, marks_known = self.handoff_steps_at(issues, boundary)
        for row, action in candidates:
            issue = row["task_refs"][0]
            old = history.get((issue, action))
            if old is None:
                continue
            task, closed = current[issue]
            step = next((s for s in task["steps"]
                         if (s["comment"], s["ordinal"]) == (old["comment"], old["ordinal"])), None)
            if step is None:
                continue
            row["work_step"] = {"issue": issue, "comment": step["comment"], "ordinal": step["ordinal"]}
            if not marks_known or old["state"] != "open" or step["state"] != "done" or not step["by"]:
                continue
            memory = self.m.get(step["by"]["id"])
            if memory and index.ISO_UTC.fullmatch(memory["posted"]) and memory["posted"] > boundary:
                row["completion"] = {"source": memory["id"], "task_closed": closed,
                                     "next_step": None if closed else task_rules.next_open(task["steps"])}

    # ------------------------------------------------------------ the model

    def model(self):
        self.read_issues()
        self.read_memories()
        self.read_plan(self.found)
        for memory in self.order:
            memory["old"] = memory["plain"] is None
        self.read_threads()
        self.read_words()
        for memory in self.order:
            memory["sentence"] = self.sentence(memory)
        for memory in self.order:
            memory["passage"] = self.passage(memory)
            memory["css"], memory["chip"] = self.state_of(memory)
            memory["explain"] = self.explain_of(memory)
            memory["group"] = {"replaced": "changed", "aside": "changed", "open": "open", "led": "wrong",
                               "answered": "done", "tried": "done", "taken": "done", "pause": "pause"}.get(
                memory["css"], "wrong" if memory["kind"] == "failure" else "stands")
        for memory in self.order:
            memory["side"] = memory["css"] == "aside" or any(
                key == "checks" and target in self.m and self.m[target]["css"] == "aside"
                for key, target in memory["links"])
        for thread in self.threads:
            seen = set()
            for memory in reversed(thread["memories"]):
                kind = memory["kind"]
                if memory["state"] == "stands" and kind in ("decision", "result") and kind not in seen:
                    seen.add(kind)
                    memory["latest"] = PHRASES["latest." + kind]
                    memory["latest_note"] = PHRASES["latest.replaced"] if any(
                        key == "replaces" for key, _ in memory["links"]) else ""
        tasks = self.tasks_model()
        self.read_pauses(tasks)
        self.read_outline(self.profiles)
        self.plan = self.plan_model()
        memories = [self.memory_model(memory) for memory in self.order]
        threads = [self.thread_model(thread) for thread in self.threads]
        outline = self.outline_model(threads)
        self.threads_for_versions = threads
        self.versions = self.versions_model(outline)
        self.sheet = self.sheet_model(outline, threads)
        tree = self.tree_model(outline)
        self.read_word_uses(threads)
        words = [self.word_model(n, rows) for n, rows in self.word_rows]
        opened = self.open_model()
        not_told = self.not_told_model()
        self.read_first_lines()
        recorded = sorted(memories, key=lambda m: (m["posted"] or "", m["id"]), reverse=True)
        boundary = (self.last or {}).get("posted")
        hierarchy = project_map.read_map(self.db, self.hub, self.allowed) if self.hub else project_map.empty()
        impacts = decision_impact.from_index(self.db, self.repo, self.allowed, builder=self, tasks=tasks,
                                             map_state=hierarchy,
                                             suggestions=decision_impact.suggestions_enabled(self.repo, self.config))
        return {
            "repo": self.repo, "name": self.repo.split("/")[1],
            "built": {"day": self.today.isoformat(), "words": full_day(self.today.isoformat()),
                      "synced_at": self.meta("synced_at"), "complete": self.meta("sync_complete") != "0"},
            "years": self.years,
            "first": self.order[0]["day"] if self.order else None,
            "latest": self.order[-1]["day"] if self.order else None,
            "memories": memories, "threads": threads, "outline": outline, "tree": tree, "words": words,
            "pauses": [{key: p[key] for key in ("kind", "id", "day", "focus", "wall")} for p in self.pauses],
            "last": self.last,
            "since": [m["id"] for m in self.order if self.last and self.last["time"] and m["time"] > self.last["time"]],
            "recorded": [m["id"] for m in recorded],
            "recorded_since": [m["id"] for m in recorded if boundary and m["posted"] and m["posted"] > boundary],
            "outputs_available": any((p if isinstance(p, str) else p.get("name")) != "core" for p in self.profiles),
            "latest_of": self.latest_of(),
            "cited": [self.cited[n] for n in sorted(self.cited)],
            "open": opened, "map": self.map_model(),
            "project_map": hierarchy, "decision_impacts": impacts, "plan": self.plan, "versions": self.versions,
            "sheet": self.sheet,
            "counts": {"memories": len(self.order), "threads": len(self.threads), "words": len(words),
                       "open": sum(1 for m in self.order if m["css"] == "open")},
            "not_told": not_told, "tasks": tasks,
        }

    def tasks_model(self):
        """The to-do view's tasks: each open task with its steps (state, and for a step that was marked the memory that marked
        it: its id, its issue and its plain line) and the tasks closed in the last 30 days, from index.tasks, which reads told
        memories alone. A title that someone outside the allowed accounts changed is left out. An index that cannot give them
        reports unavailable work rather than an empty task list."""
        none = {"todo": None, "open": [], "closed": [], "available": False}
        try:
            now = datetime.datetime.combine(self.today, datetime.time(23, 59, 59), datetime.timezone.utc)
            report = index.tasks(self.db, self.allowed, closed_days=30, now=now)
        except Exception:  # noqa: BLE001 - an index of an older form, or one that is not read yet
            return none
        items = self.found["items"]

        def marked(cid):
            memory = items.get(cid)
            if memory is None:
                return None
            return {"id": cid, "issue": memory["issue"], "plain": short(memory["plain"] or "")}

        def task(entry):
            closed = (entry["closed_at"] or "")[:10]
            return {"issue": entry["issue"], "title": short(entry["title"]) if entry["title_told"] else "",
                    "todo": entry["todo"] is True, "hand": entry["hand"] is True, "counts": dict(entry["counts"]),
                    "blocked": entry["blocked"] is True, "blocking_failure": marked(entry.get("blocking_failure")),
                    "closed": closed if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", closed) else "",
                    "steps": [{"n": step["n"], "text": short(step["text"]), "state": step["state"],
                               "comment": step["comment"], "ordinal": step["ordinal"],
                               "by": marked(step["by"]["id"]) if step["by"] else None} for step in entry["steps"]]}

        opened = [task(entry) for entry in report["tasks"] if entry["state"] == "open"]
        closed = [task(entry) for entry in report["tasks"] if entry["state"] == "closed"]
        return {"todo": report["todo"], "available": True,
                "open": sorted(opened, key=lambda t: (not t["todo"], t["issue"])),
                "closed": sorted(closed, key=lambda t: (t["closed"], t["issue"]), reverse=True)}

    def tree_model(self, outline):
        """The map of what we know: the project's question in the middle, the sections around it (the first half to the
        right, the rest to the left, each in the order of the deliverable), under each its topics, and on each topic
        what was found, what was decided and what is still open, newest first, six at most with the full count. No
        coordinates: the page lays it out with its style."""
        def entry(n):
            memories = self.thread_of[n]["memories"]

            def pick(test):
                ids = [m["id"] for m in reversed(memories) if test(m)]
                return {"ids": ids[:6], "count": len(ids)}

            return {"thread": n, "found": pick(lambda m: m["kind"] == "result" and m["css"] == "stands"),
                    "decided": pick(lambda m: m["kind"] == "decision" and m["css"] == "stands"),
                    "open": pick(lambda m: m["css"] == "open")}

        sections = []
        for group in outline["deliverables"]:
            for section in group["sections"]:
                topics = [entry(p["thread"]) for p in section["parts"] if p["first"]]
                if topics:
                    entry_of_section = {"n": section["n"], "key": section["key"], "label": section["label"],
                                        "topics": topics}
                    if self.plan_state is not None:
                        entry_of_section["plan"] = self.plan_branch(section["label"])
                    sections.append(entry_of_section)
        if outline["unplaced"]:
            sections.append({"n": None, "key": "topics", "label": "Topics",
                             "topics": [entry(n) for n in outline["unplaced"]]})
        half = (len(sections) + 1) // 2
        question = None
        if self.hub in self.issues and self.body_shown(self.hub):
            text = record_format.section_text(self.issues[self.hub]["body"], "Project")
            paragraph = " ".join(clean(text or "").strip().split("\n\n")[0].split())
            first = re.split(r"(?<=[.!?])\s", paragraph, maxsplit=1)[0] if paragraph else ""
            question = self.doc(first, set(), cap=300) if first else None
        return {"question": question, "right": sections[:half], "left": sections[half:],
                "aside": [t["n"] for t in self.threads if t["set_aside"]],
                "other": entry(0) if 0 in self.thread_of else None}

    def latest_of(self):
        out = {}
        for kind in ("decision", "result", "failure", "question"):
            found = [m["id"] for m in self.order if m["kind"] == kind]
            out[kind] = found[-1] if found else None
        return out

    NOUNS = {"manuscript": ("does", "the manuscript"), "thesis": ("does", "the thesis"),
             "research": ("does", "the report"), "prototyping": ("does", "the report"),
             "proposal": ("does", "the proposal"), "software": ("do", "the release notes"),
             "docs": ("does", "the documentation"), "dataset": ("does", "the documentation")}

    def read_outline(self, profiles):
        """Where each topic's text goes: the deliverable's sections in the order of the hub's table, which is read only
        when the hub's text may be shown, and else in the order of the profiles. Sets `place` and `feeds` of the
        threads and sorts them: by the first section they feed, then the sections' own order, then the topics that
        feed none, then the set-aside threads, then the other notes."""
        shown = self.hub is not None and self.body_shown(self.hub)
        self.unreadable = False
        try:
            plan = index.deliverable_order(self.db, self.hub if shown else None, profiles)
        except ValueError:
            plan, self.unreadable = [], True
        table = shown and record_format.section_text(self.issues[self.hub]["body"], "Deliverable sections") is not None
        self.order_from = "hub" if table else "profile"
        profile_of, output_of = {}, {}
        self.outputs = []
        for entry in profiles:
            try:
                labels = [label for label, _ in recortex.section_labels([entry])]
            except ValueError:
                self.unreadable = True
                continue
            name = entry if isinstance(entry, str) else entry.get("name")
            ident = name if isinstance(entry, str) else name + "-" + hashlib.sha256("\n".join(sorted(labels)).encode()).hexdigest()[:12]
            raw_profiles = (self.config or {}).get("profiles", [])
            original = next((p for p in (raw_profiles if isinstance(raw_profiles, list) else []) if isinstance(p, dict)
                             and recortex.custom_sections(p) and p.get("name") == name
                             and sorted(label for label, _ in p["sections"]) == sorted(labels)), None)
            title = (original or {}).get("deliverable") or (entry.get("deliverable") if isinstance(entry, dict) else None)
            title = title if isinstance(title, str) and recortex.HEADING_TEXT.fullmatch(title) else None
            if name != "core":
                self.outputs.append({"id": ident, "profile": name, "title": clean(title) if title else self.NOUNS.get(name, (None, name))[1].capitalize()})
            for label in labels:
                profile_of.setdefault(label, name)
                output_of.setdefault(label, ident)
        self.plan_rows = []
        for label, heading, numbers in plan:
            threads = [self.topic_thread[n]["n"] for n in numbers if n in self.topic_thread]
            if threads:
                self.plan_rows.append({"key": label, "heading": clean(heading), "profile": profile_of.get(label),
                                       "output": output_of.get(label),
                                       "threads": threads})
        for thread in self.threads:
            thread["place"], thread["feeds_keys"] = None, []
        for k, row in enumerate(self.plan_rows):
            for p, n in enumerate(row["threads"]):
                thread = self.thread_of[n]
                if thread["place"] is None:
                    thread["place"] = (k, p)
                thread["feeds_keys"].append(row["key"])
        position = {m["id"]: i for i, m in enumerate(self.order)}

        def rank(thread):
            began = position[thread["memories"][0]["id"]] if thread["memories"] else len(self.order)
            if thread["label"] == PLAN_LABEL:
                return (-1, 0, 0)   # the plan of the paper comes first
            if thread["loose"]:
                return (3, 0, 0)
            if thread["set_aside"]:
                return (2, began, 0)
            if thread["place"] is not None:
                return (0,) + thread["place"]
            return (1, began, 0)

        self.threads.sort(key=rank)

    def part_status(self, model, heading=None):
        """The status line of a part of the text: a list of sentences, each {"key", "segs"}, so that a later round can
        append one. A seg is {"t": words}, {"thread": n}, {"m": memory id, "t": words} or {"open": a group of
        "What is still open?", "t": words}."""
        text = model["text"]
        sentences = [{"key": "from", "segs": [{"t": "From "}, {"thread": model["n"]}, {"t": "."}]},
                     {"key": "changed", "segs": [{"t": "Current text, last changed on %s." % text["changed"]}]}]
        after = text["after"]
        if not after:
            sentences.append({"key": "after", "segs": [{"t": "Nothing recorded since then bears on it."}]})
        else:
            segs = [{"t": plural(len(after), "1 decision or finding was", "%d decisions or findings were") +
                     " recorded in this thread after that change: "}]
            shown = after[:4]
            for i, link in enumerate(shown):
                if i:
                    segs.append({"t": " and " if i == len(shown) - 1 and len(after) == len(shown) else ", "})
                segs.append({"m": link["id"], "t": link["text"]})
            if len(after) > len(shown):
                segs.append({"t": " and %d more" % (len(after) - len(shown))})
            segs.append({"t": ". Check that the text says what they changed."})
            sentences.append({"key": "after", "segs": segs})
        counts = [("questions", len(text["questions"]), "1 open question", "%d open questions"),
                  ("failures", len(text["failures"]), "1 failure nothing followed", "%d failures nothing followed"),
                  ("tasks", text["tasks"], "1 task", "%d tasks")]
        counts = [(key, plural(n, one, many)) for key, n, one, many in counts if n]
        if counts:
            segs = []
            for i, (key, words) in enumerate(counts):
                segs.append({"t": ", " if 0 < i < len(counts) - 1 else " and " if i else ""} if i else {"t": ""})
                segs.append({"open": key, "t": words})
            total = len(text["questions"]) + len(text["failures"]) + text["tasks"]
            segs.append({"t": " bears on it." if total == 1 else " bear on it."})
            sentences.append({"key": "open", "segs": [s for s in segs if s["t"] != "" or "open" in s]})
        if heading is not None and self.plan_state is not None:
            sentences += self.part_plan_sentences(model["topic"], heading)
        return sentences

    def thread_text(self, thread):
        """What a reader needs to judge a thread's text: when its body last changed, the decisions and findings
        recorded after that (the rule of query consistency for a stale body), and what is still open on it."""
        issue = self.issues.get(thread["topic"]) if thread["topic"] is not None else None
        if issue is None:
            return None
        stamp = issue["edited"] or issue["created"]
        body_time = record_format.sort_key(stamp or "")
        after = [{"id": m["id"], "text": "the %s of %s" % ({"result": "finding"}.get(m["kind"], m["kind"]), m["day"])}
                 for m in thread["memories"] if m["kind"] in THREAD_KINDS and m["time"] > body_time]
        return {"changed": full_day(stamp), "after": after,
                "questions": [m["id"] for m in thread["memories"] if m["kind"] == "question" and m["css"] == "open"],
                "failures": [m["id"] for m in thread["memories"] if m["kind"] == "failure" and m["css"] == "open"],
                "tasks": sum(1 for task in self.tasks() if task["thread"] == thread["n"] and task["open"])}

    def outline_model(self, models):
        """The outline of the deliverable for the page, built from the thread models (their sections give the
        ordinal of each part): deliverables, each with its sections and their parts; the parts of a text that no
        section takes; the topics placed nowhere."""
        by_n = {m["n"]: m for m in models}
        used, first, deliverables, count = {}, {}, [], 0
        for row in self.plan_rows:
            parts = []
            for n in row["threads"]:
                model = by_n[n]
                ordinal = next((i for i, s in enumerate(model["sections"]) if s["heading"].lower() == row["heading"].lower()),
                               None)
                if model["shown"] and ordinal is None:
                    continue  # the body carries the label and has no such section
                part = {"thread": n, "ordinal": ordinal, "first": n not in first,
                        "status": self.part_status(model, row["heading"])}
                if self.plan_state is not None and ordinal is not None:
                    part.update(planned=self.planned_for_part(model["topic"], row["heading"]),
                                by_hand=bool(self.newest_text(model["topic"], row["heading"]) and self.by_hand(
                                    model["topic"], self.newest_text(model["topic"], row["heading"]))),
                                history=self.history_model(model["topic"], row["heading"]))
                parts.append(part)
                if ordinal is not None:
                    used.setdefault(n, set()).add(ordinal)
            if not parts:
                continue
            count += 1
            for part in parts:
                first.setdefault(part["thread"], count)
                by_n[part["thread"]]["feeds"].append({"n": count, "label": row["heading"], "ordinal": part["ordinal"]})
            name = row["profile"] or "deliverable"
            ident = row.get("output") or name
            group = next((g for g in deliverables if g["id"] == ident), None)
            if group is None:
                aux, noun = self.NOUNS.get(name, (None, None))
                output = next((o for o in self.outputs if o["id"] == ident), {})
                group = {"id": ident, "profile": name, "noun": noun, "title": output.get("title") or (noun or name).capitalize(),
                         "sections": []}
                deliverables.append(group)
            group["sections"].append({"key": row["key"], "label": row["heading"], "n": count,
                                      "before": self.plan_before(row["heading"]), "parts": parts})
        names = [p if isinstance(p, str) else p.get("name") for p in self.profiles]
        if len(names) == 1 and names[0] in self.NOUNS:
            aux, noun = self.NOUNS[names[0]]
            nav, title = "What %s %s say?" % (aux, noun), noun.capitalize()
            if recortex.plan_profile(self.profiles) is not None:
                nav = title = mind_sheet.PHRASES["sheet.deliver"]   # the manuscript has a view of its own: this one holds the plan
        else:
            noun, nav, title = None, "What do we deliver?", "What we deliver"
        other, unplaced = [], []
        for model in models:
            if model["set_aside"] or model["loose"] or model["label"] == PLAN_LABEL:
                continue
            if model["n"] not in first:
                unplaced.append(model["n"])
            if model["topic"] is None:
                continue  # Task-only stories belong on the project map, and contribute no text to Outputs.
            if model["shown"]:
                other += [{"thread": model["n"], "ordinal": i} for i in range(len(model["sections"]))
                          if i not in used.get(model["n"], ())]
            elif model["n"] not in first:
                other.append({"thread": model["n"], "ordinal": None})
        return {"order_from": self.order_from, "nav": nav, "title": title, "noun": noun, "deliverables": deliverables,
                "outputs": self.outputs,
                "other": other, "unplaced": unplaced, "unreadable": self.unreadable, "sections": count}

    def thread_model(self, thread):
        memories = thread["memories"]
        opened = sum(1 for m in memories if m["css"] == "open")
        if thread["set_aside"]:
            status, css, explain = PHRASES["status.aside"], "aside", "thread-aside"
        elif opened:
            status, css, explain = PHRASES["status.open"] if opened == 1 else PHRASES["status.opens"] % opened, \
                "open", "thread-open"
        else:
            status, css, explain = PHRASES["status.settled"], "stands", "thread-clear"
        opening = self.openings.get(thread["n"])
        seen = set()
        sections, sources, blocks = [], [], []
        if thread["shown"]:
            topic = thread["topic"]
            parts = self.blocks_of(topic)
            for row in self.db.execute("SELECT ordinal, heading, text FROM sections WHERE issue = ? ORDER BY ordinal",
                                       (topic,)):
                heading = clean(row["heading"])
                if heading.lower() == recortex.SOURCES_HEADING.lower():
                    continue
                sections.append({"heading": short(heading), "doc": self.doc(row["text"], seen, cap=CAP_TEXT)})
            blocks = [dict(block) for block in parts.values()]
            for row in self.db.execute("SELECT statement, source FROM sources WHERE issue = ? ORDER BY ordinal",
                                       (topic,)):
                sources.append({"statement": self.runs(short(row["statement"], CAP_FIELD), seen),
                                "source": self.runs(short(row["source"], CAP_FIELD), seen)})
        tasks = {task["issue"]: task for task in self.tasks() if task["thread"] == thread["n"]}
        steps, groups, anchored, previous = [], [], set(), None
        noted = False
        for memory in memories:
            step = {"id": memory["id"], "conn": None, "note": False, "task": None, "group": None}
            if previous is not None and previous["kind"] != "checkpoint" and memory["kind"] != "checkpoint":
                direct = [(k, t) for k, t in memory["links"] if t == previous["id"]]
                back = [(k, t) for k, t in memory["links"] if t in self.m and self.m[t]["thread"] == memory["thread"]]
                away = [(k, t) for k, t in memory["links"] if t in self.m]
                if direct:
                    step["conn"] = {"phrase": self.link_phrase(*direct[0]), "to": None}
                elif back:
                    key, target = back[0]
                    step["conn"] = {"phrase": self.link_phrase(key, target) + PHRASES["link.back"],
                                    "to": {"id": target, "day": self.m[target]["day"]}}
                elif away:
                    key, target = away[0]
                    other = self.thread_of[self.m[target]["thread"]]
                    step["conn"] = {"phrase": self.link_phrase(key, target) + PHRASES["link.thread"],
                                    "to": {"thread": other["n"], "name": other["name"]}}
                else:
                    step["conn"] = {"phrase": PHRASES["link.then"], "to": None, "time": True}
            if memory["old"] and not noted:
                step["note"] = noted = True
            if memory["kind"] == "checkpoint":
                after = next((m for m in memories if m["kind"] != "checkpoint"
                              and (m["time"], m["id"]) > (memory["time"], memory["id"])), None)
                focus = memory["plain"] or self.field(memory["id"], "State") or memory["title"]
                step["pause"] = {"day": memory["day"], "focus": short(focus, CAP_FIELD),
                                 "next": after["id"] if after else None, "next_day": after["day"] if after else None}
            task = memory["issue"] if memory["issue"] in tasks else None
            if task is not None:
                step["task"] = task
                if groups and groups[-1]["task"] == task and previous is not None and previous["issue"] == task:
                    groups[-1]["count"] += 1
                    groups[-1]["last"] = memory["day"]
                else:
                    info = tasks[task]
                    groups.append({"task": task, "title": info["title"], "count": 1, "first": memory["day"],
                                   "last": memory["day"], "open": info["open"], "closed": info["closed"],
                                   "anchor": task not in anchored})
                    anchored.add(task)
                step["group"] = len(groups) - 1
            steps.append(step)
            previous = memory
        aside_by = [m for m in memories if m["kind"] == "decision"]
        latest = {kind: next((m["id"] for m in reversed(memories) if m.get("latest") and m["kind"] == kind), None)
                  for kind in ("decision", "result")}
        open_tasks = [task for task in tasks.values() if task["open"]]
        return {
            "n": thread["n"], "label": clean(thread["label"]) if thread["label"] else None, "name": thread["name"],
            "name_from": thread["name_from"], "topic": thread["topic"], "issues": thread["issues"],
            "set_aside": thread["set_aside"], "loose": thread["loose"], "shown": thread["shown"],
            "status": status, "status_css": css, "status_explain": explain, "open": opened, "also": thread["also"],
            "opening": {"comment": opening["comment"], "day": day_words(opening["at"], self.years),
                        "about": self.doc(opening["meaning"], seen), "why": self.doc(opening["why"], seen)}
            if opening else None,
            "sections": sections, "blocks": blocks, "sources": sources, "feeds": [],
            "text": self.thread_text(thread),
            "stands": {"decision": latest["decision"], "result": latest["result"],
                       "open": [m["id"] for m in memories if m["css"] == "open"], "tasks": open_tasks},
            "memories": [m["id"] for m in memories], "steps": steps, "groups": groups,
            "began": memories[0]["day"] if memories else None, "ended": memories[-1]["day"] if memories else None,
            "aside_by": aside_by[-1]["id"] if thread["set_aside"] and aside_by else None,
            "rests": self.rests_on(memories) if not thread["set_aside"] else [],
            "tasks": list(tasks.values()),
        }

    def rests_on(self, memories):
        """The standing decisions, results and corrections of a thread, newest first, each with what it replaced."""
        out = []
        for memory in reversed(memories):
            if memory["state"] != "stands" or memory["kind"] not in ("decision", "result", "correction"):
                continue
            older, cursor = [], memory
            while len(older) < 50:
                prior = [t for k, t in cursor["links"] if k == "replaces" and t in self.m]
                if not prior or prior[0] in older:
                    break
                older.append(prior[0])
                cursor = self.m[prior[0]]
            out.append({"id": memory["id"], "replaced": older})
        return out

    def tasks(self):
        """Every task (an `active` issue that is no pull request), open and closed, with its place in a thread, its
        title under the title rule, its goal when its text may be shown, and the ids of the memories written on it."""
        if not hasattr(self, "task_cache"):
            out = []
            for number, issue in sorted(self.issues.items()):
                if issue["pull"] or not self.has_label(number, "active"):
                    continue
                shown, goal = self.body_shown(number), None
                if shown:
                    row = self.db.execute("SELECT text FROM sections WHERE issue = ? AND lower(heading) = ? "
                                          "ORDER BY ordinal LIMIT 1", (number, GOAL)).fetchone()
                    line = next((l.strip() for l in clean(row["text"] if row else "").split("\n") if l.strip()), "")
                    if line:
                        goal = self.runs(short(line), set())
                label = self.belongs.get(number)
                opened = issue["state"] == "open"
                out.append({"issue": number, "thread": self.thread_by_label.get(label) if label else 0,
                            "title": short(issue["title"]) if self.title_shown(number) and issue["title"].strip()
                            else None, "open": opened,
                            "closed": full_day(issue["closed"]) if not opened and issue["closed"] else None,
                            "goal": goal, "shown": shown,
                            "memories": [m["id"] for m in self.order if m["issue"] == number]})
            self.task_cache = out
        return self.task_cache

    def read_word_uses(self, threads):
        """Where each project word is used besides the sentences: in the parts of the text (thread and ordinal) and
        in the fields of memories. Found in the record's text itself, so a word is listed for every part that holds it."""
        self.word_in_text, self.word_in_fields = {}, {}
        if self.words.pattern is None:
            return
        for thread in threads:
            if not thread["shown"]:
                continue
            ordinal = 0
            for row in self.db.execute("SELECT heading, text FROM sections WHERE issue = ? ORDER BY ordinal",
                                       (thread["topic"],)):
                if clean(row["heading"]).lower() == recortex.SOURCES_HEADING.lower():
                    continue
                for _, _, n in self.words.find(clean(row["text"]), set()):
                    self.word_in_text.setdefault(n, []).append({"thread": thread["n"], "ordinal": ordinal})
                ordinal += 1
        for cid in sorted(self.fields):
            if cid not in self.m:
                continue
            for label, text in self.fields[cid]:
                for _, _, n in self.words.find(text, set()):
                    ids = self.word_in_fields.setdefault(n, [])
                    if cid not in ids and len(ids) < 200:
                        ids.append(cid)

    def word_model(self, n, rows):
        newest = rows[-1]
        seen = {n}
        uses = [m["id"] for m in self.order if any(mark[2] == n for mark in m["sentence"]["marks"])]
        first = rows[0]
        thread = next((t for t in self.threads if first["issue"] in t["issues"]), None)
        story = self.doc(newest["story"], seen, cap=CAP_STORY) if newest["story"] else None
        return {
            "n": n, "word": short(newest["word"], 60), "meaning": short(newest["meaning"]),
            "why": self.doc(newest["why"], seen) if newest["why"] else None,
            "also": [short(name, 60) for name in newest["also"]], "story": story,
            "first": {"comment": first["comment"], "day": day_words(first["at"], self.years),
                      "told": first["comment"] in self.m,
                      "thread": thread["n"] if thread else None, "thread_name": thread["name"] if thread else None},
            "earlier": [{"comment": row["comment"], "meaning": short(row["meaning"]),
                         "day": day_words(row["at"], self.years)} for row in rows[:-1]],
            "uses": uses,
            "in_text": self.word_in_text.get(n, []), "in_fields": self.word_in_fields.get(n, []),
        }

    def open_model(self):
        set_aside = {t["n"] for t in self.threads if t["set_aside"]}
        unknown = []
        for memory in self.order:
            text = self.field(memory["id"], "Not established")
            if text and memory["kind"] == "result" and memory["state"] == "stands" and memory["thread"] not in set_aside:
                after = [source for _, source in self.incoming.get(memory["id"], [])]
                unknown.append({"id": memory["id"], "doc": self.doc(text, set()), "followed": after[0] if after else None})
        unknown.sort(key=lambda entry: entry["followed"] is not None)  # what nothing followed first; the sort is stable

        def age(memory):
            parts = wall(memory["at"])
            if not parts:
                return None
            return max(0, (self.today - datetime.date(*parts[:3])).days)

        pick = lambda test: [{"id": m["id"], "age": age(m)} for m in self.order if test(m)]
        return {
            "questions": pick(lambda m: m["kind"] == "question" and m["state"] == "open"),
            "ideas": pick(lambda m: m["kind"] == "idea" and m["state"] == "untried"),
            "failures": pick(lambda m: m["kind"] == "failure" and m["css"] == "open"),
            "unknown": unknown,
            "holds": self.last["holds"] if self.last else None,
            "tasks": [task for task in self.tasks() if task["open"]],
            "plan": self.plan_open(),
        }

    def not_told_model(self):
        edited = []
        for cid in self.found["edited_ids"]:
            row = self.db.execute("SELECT issue FROM events WHERE comment = ?", (cid,)).fetchone()
            if row is not None:
                edited.append({"id": cid, "issue": row["issue"]})
        bodies = [{"thread": t["n"], "topic": t["topic"]} for t in self.threads if t["topic"] and not t["shown"]]
        return {"others": self.found["hidden"], "edited": self.found["edited"], "edited_ids": edited,
                "older": self.older, "words": self.words_cut, "bodies": bodies, "pause": self.pause_untold,
                "kinds": self.unknown_kinds + self.unknown_marked(),
                "count": self.found["hidden"] + self.found["edited"] + self.older + self.words_cut + len(bodies)
                + (1 if self.pause_untold else 0) + (1 if self.meta("sync_complete") == "0" else 0)
                + self.unknown_kinds + self.unknown_marked()}

    def unknown_marked(self):
        """How many comments of allowed accounts carry a marker of a kind this version does not know (a plan, for
        example): they are not memories here, so they are left out and counted."""
        known = set(record_format.EVENT_TYPES) | {"resume"}
        count = 0
        for row in self.db.execute("SELECT substr(c.body, 1, 400) AS head, e.author AS author FROM events e JOIN "
                                   "comments c ON c.id = e.comment WHERE e.type = 'other'"):
            marker = record_format.parse_marker((row["head"] or "").split("\n", 1)[0])
            if marker and marker.get("type") and marker["type"] not in known and self.ok(row["author"]):
                count += 1
        return count

    # ------------------------------------------------------------ the map

    NAME_CHARS = 34   # a thread's name on the map is cut to this many characters; the whole name is in the row's title

    def map_model(self):
        width, right, top, gap = 1080, 30, 64, 104
        timed = [m for m in self.order if wall(m["at"])]
        if not timed:
            return None
        moment = lambda at: datetime.datetime(*wall(at))
        start = min(moment(m["at"]) for m in timed).replace(hour=0, minute=0)
        end = max(moment(m["at"]) for m in timed)
        span = (end - start).total_seconds() + 86400
        rows = [t for t in self.threads if t["memories"]]
        folded = []
        if len(rows) > CAP_MAP_THREADS:
            keep = sorted(rows, key=lambda t: -len(t["memories"]))[:CAP_MAP_THREADS - 1]
            folded = [t for t in rows if t not in keep]
            rows = [t for t in rows if t in keep]
        lines = [(t["n"], t["name"], t["memories"]) for t in rows]
        longest = max([min(self.NAME_CHARS, len(name or "")) for _, name, _ in lines] + [12])
        left = max(150, int(longest * 8.6) + 40)   # the name column is as wide as the longest name, cut at 34 characters
        scale = lambda value: left + (width - left - right) * (value - start).total_seconds() / span
        if folded:
            lines.append((None, None, sorted((m for t in folded for m in t["memories"]),
                                             key=lambda m: (m["time"], m["id"]))))
        height = top + gap * len(lines) + 30
        days = (end.date() - start.date()).days + 1
        step = max(1, -(-days // 8))
        ticks = []
        for offset in range(0, days + 1, step):
            value = start + datetime.timedelta(days=offset)
            ticks.append({"x": round(scale(value), 1), "day": day_words(value.strftime("%Y-%m-%d"), self.years)})
        position, rows_out = {}, []
        for row, (n, name, memories) in enumerate(lines):
            y, last_x = top + gap * row + 30, 0
            xs = []
            for memory in memories:
                if not wall(memory["at"]):
                    continue
                x = max(scale(moment(memory["at"])), last_x + 26)
                last_x = x
                position[memory["id"]] = (round(x, 1), y + (34 if memory["side"] else 0), memory["side"], y)
                xs.append(x)
            status = None
            if n is not None:
                thread = self.thread_of[n]
                opened = sum(1 for m in thread["memories"] if m["css"] == "open")
                status = "aside" if thread["set_aside"] else "open" if opened else "stands"
            short_name = name if name is None or len(name) <= self.NAME_CHARS else name[:self.NAME_CHARS - 1].rstrip() + "…"
            rows_out.append({"n": n, "name": name, "name_short": short_name, "y": y, "x1": round(min(xs) - 14, 1) if xs else left,
                             "x2": round(max(xs) + 14, 1) if xs else left, "status_css": status,
                             "folded": len(folded) if n is None else 0})
        width = max(width, int(max([p[0] for p in position.values()] or [0]) + right + 10))
        dots = [{"id": m["id"], "x": position[m["id"]][0], "y": position[m["id"]][1], "side": position[m["id"]][2],
                 "row_y": position[m["id"]][3], "kind": m["kind"], "css": m["css"]}
                for m in self.order if m["id"] in position]
        links = []
        for memory in self.order:
            for key, target in memory["links"]:
                if target in position and memory["id"] in position:
                    links.append({"a": target, "b": memory["id"], "kind": key,
                                  "x1": position[target][0], "y1": position[target][1],
                                  "x2": position[memory["id"]][0], "y2": position[memory["id"]][1]})
        pauses, last_label = [], None
        for p in sorted((p for p in self.pauses if wall(p["at"]) and start <= datetime.datetime(*wall(p["at"]))
                         <= end + datetime.timedelta(days=1)), key=lambda p: (p["wall"], p["id"])):
            x = round(scale(datetime.datetime(*wall(p["at"]))), 1)
            label = last_label is None or x - last_label >= 60  # a label is drawn only 60 units after the one before
            if label:
                last_label = x
            pauses.append({"x": x, "label": label})
        return {"width": width, "height": height, "top": top, "left": left, "rows": rows_out, "dots": dots, "links": links,
                "ticks": ticks, "pauses": pauses, "folded": len(folded)}

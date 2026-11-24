"""Read the formats of the record: event comments, deliverable bodies, figure and table blocks, sources.

Python 3 standard library only. Nothing here touches GitHub; the linter, the
index and compile.py all parse the record through these functions, so they
agree on what a section, an event or a figure is.

A memory is an event comment of one of the seven MEMORY_TYPES. From 0.6.0 on it
starts with a line in plain words (PLAIN_LABELS), may name earlier memories in
its marker (LINK_KINDS, LINK_RULES), may list its evidence (an "Evidence" field)
and may end with folded explanations of a project word or of its thread. The
readers here are lenient: a part that does not match its form is ignored, and
the comment stays an event of its type. The linter is the strict side.
"""

import datetime
import decimal
import re

import recortex
import zones

EVENT_TYPES = ("result", "decision", "failure", "checkpoint", "correction", "idea", "question", "record", "triage",
               "plan", "text", "proposal", "steps")
HEADED_TYPES = ("result", "decision", "failure", "checkpoint", "correction", "idea", "question", "plan", "text", "steps")
MEMORY_TYPES = ("result", "decision", "failure", "checkpoint", "correction", "idea", "question", "plan", "text", "steps")
# The eight kinds of memory that the observer may queue and post (since 1.5.0 a task's steps among them); a plan and a
# text change are the main session's.
OBSERVED_TYPES = ("result", "decision", "failure", "checkpoint", "correction", "idea", "question", "steps")
ISO = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} (?:[A-Z]{2,5}|UTC[+-][0-9]{1,2}(?::[0-9]{2})?)\b"
MARKER = re.compile(r"^<!-- ghim:event ((?:[a-z]+=\S+ )*[a-z]+=\S+) -->\s*$")
EVENT_HEADING = re.compile(r"^### (Result|Decision|Failure|Checkpoint|Correction|Idea|Question|Triage|Plan|Text|Proposal|Steps)"
                           r"( \(reconstructed\))?: "
                           r"(%s) - (.+?)\s*$"
                           % STAMP)
BLOCK_HEADING = re.compile(r"^### (Figure|Table): ([A-Za-z0-9][\w.-]*) - (.+?)\s*$")
IMAGE = re.compile(r"!\[[^\]]*\]\((https://github\.com/([\w.-]+/[\w.-]+)/blob/([^/\s]+)/([^\s?)]+)\?raw=true)\)")
LINK = re.compile(r"\]\((https://github\.com/([\w.-]+/[\w.-]+)/blob/([^/\s]+)/([^\s?)#]+)[^)]*)\)")
BRANCH_REFS = {"main", "master", "head", "develop", "dev", "trunk"}
CODE_SPAN = re.compile(r"`([^`\n]+)`")
COMMIT = re.compile(r"(?<![\w/.-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![\w-])")
PULL = re.compile(r"(?:\bpull request\s+#([0-9]+)|/pull/([0-9]+))", re.I)
URL = re.compile(r"https?://[^\s)>|`]+")
MENTION = re.compile(r"(?<![\w/&#])#([0-9]{1,5})\b(?:\s*\(([^)\n]{1,60})\))?")
STOP = {"of", "the", "a", "an", "such", "that", "which", "in", "on", "for", "and", "or", "to", "with", "by", "at",
        "all", "each", "are", "is", "were", "was", "be", "its", "their", "this", "these", "those"}
NUMBER = re.compile(r"(?<![\w.])([0-9][0-9,]*(?:\.[0-9]+)?)\s+of\s+(?:the\s+)?([0-9][0-9,]*)\b((?:[ \t]+[A-Za-z][\w-]*){0,6})")


def parse_marker(line):
    """The fields of a `<!-- ghim:event ... -->` line as a dict, or None."""
    match = MARKER.match(line.strip())
    if not match:
        return None
    return dict(part.split("=", 1) for part in match.group(1).split())


def first_lines(text, count=2):
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    return lines[:count] + [""] * (count - len(lines[:count]))


def parse_event(body):
    """What a comment is: type, times, title, as far as its text says.

    Returns a dict with type (an event type, "resume", "record" or "other"), at
    (from the marker, else None), heading_time (the stamp of the heading),
    recorded, reconstructed (bool), title and marker (the marker fields or None).
    A comment that is none of these keeps type "other"; nothing is dropped.
    """
    first, second = first_lines(body)
    marker = parse_marker(first)
    heading_line = second if marker else first
    out = {"type": "other", "at": None, "heading_time": None, "recorded": None, "reconstructed": False,
           "title": "", "marker": marker}
    if heading_line.startswith(recortex.RESUME_HEADING):
        out.update(type="resume", heading_time=heading_line[len(recortex.RESUME_HEADING):].strip() or None,
                   title="Resume block")
        return out
    if heading_line.startswith(recortex.RECORD_HEADING):
        out.update(type="record", title="Former issue body kept as history")
    match = EVENT_HEADING.match(heading_line)
    if match:
        out.update(type=match.group(1).lower(), heading_time=match.group(3), title=match.group(4),
                   reconstructed=bool(match.group(2)))
    if marker:
        # The marker types a comment only together with a matching heading; a marker alone leaves it "other".
        if out["type"] != "other" and out["type"] != marker.get("type"):
            out["type"] = "other"
        out["at"] = marker.get("at") if re.fullmatch(ISO, marker.get("at", "")) else None
        recorded = marker.get("recorded", "")
        out["recorded"] = recorded if re.fullmatch(ISO, recorded) else None
        out["reconstructed"] = out["reconstructed"] or out["recorded"] is not None
    if out["type"] == "other":
        out["title"] = re.sub(r"^#+\s*", "", heading_line)[:120]
    return out


def sections(body):
    """The level-2 sections of a body as a list of dicts: heading, line (1-based), text."""
    lines = body.replace("\r\n", "\n").split("\n")
    found = recortex.body_sections(body)
    out = []
    for index, (number, heading) in enumerate(found):
        stop = found[index + 1][0] - 1 if index + 1 < len(found) else len(lines)
        out.append({"heading": heading, "line": number, "text": "\n".join(lines[number:stop]).strip("\n")})
    return out


def section_text(body, heading):
    for section in sections(body):
        if section["heading"].lower() == heading.lower():
            return section["text"]
    return None


def replace_section(body, heading, text):
    """`body` with the text under the level-2 section `heading` (compared without case) replaced by `text`, or None when
    the body has no such section. The heading line and every line outside the section stay as they are (line endings
    become LF); the new text stands after one blank line and before one blank line and the next heading."""
    lines = body.replace("\r\n", "\n").split("\n")
    found = recortex.body_sections(body)
    for position, (number, name) in enumerate(found):
        if name.lower() != heading.lower():
            continue
        stop = found[position + 1][0] - 1 if position + 1 < len(found) else len(lines)
        new = [line.rstrip() for line in text.strip("\n").split("\n")] if text.strip() else []
        more = lines[stop:]
        return "\n".join(lines[:number] + [""] + new + ([""] if more else []) + more)
    return None


def table_rows(text):
    """Rows of the first Markdown table in `text`, header and separator removed, as lists of cells."""
    rows, started = [], False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)[1:-1]]
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
                started = True
                continue
            if started:
                rows.append(cells)
        elif started:
            break
    return rows


def sources(body):
    """Rows of the "Sources" table: a list of dicts with statement and source."""
    text = section_text(body, recortex.SOURCES_HEADING) or ""
    return [{"statement": row[0], "source": " | ".join(row[1:])} for row in table_rows(text) if row]


def refs(text):
    """Paths, commits, pull requests and URLs named in a piece of text, as (kind, value) pairs."""
    found = []
    for span in CODE_SPAN.findall(text):
        if "/" in span or re.search(r"\.\w{1,5}$", span):
            found.append(("path", span.strip()))
    for match in COMMIT.finditer(text):
        found.append(("commit", match.group(0)))
    for match in PULL.finditer(text):
        found.append(("pull", match.group(1) or match.group(2)))
    for match in URL.finditer(text):
        found.append(("url", match.group(0).rstrip(".,;")))
    for match in IMAGE.finditer(text):
        found.append(("path", match.group(4)))
        found.append(("commit" if COMMIT.fullmatch(match.group(3)) else "ref", match.group(3)))
    seen, out = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def blocks(body):
    """Figure and table blocks: `### Figure: <id> - <title>` or `### Table: ...` inside a section.

    Each is a dict: kind, id, title, section, line, caption, image (URL), repo,
    ref, path, has_table, source (the Sources row naming it, or None), data,
    command.
    """
    lines = body.replace("\r\n", "\n").split("\n")
    rows = sources(body)
    out, current_section = [], None
    headings = {number: heading for number, heading in recortex.body_sections(body)}
    for index, line in enumerate(lines):
        if index + 1 in headings:
            current_section = headings[index + 1]
        match = BLOCK_HEADING.match(line)
        if not match:
            continue
        end = index + 1
        while end < len(lines) and not re.match(r"^#{1,3} ", lines[end]):
            end += 1
        text = "\n".join(lines[index + 1:end])
        image = IMAGE.search(text)
        link = image or LINK.search(text)
        caption = "\n".join(l for l in text.split("\n")
                            if l.strip() and not l.strip().startswith(("|", "![")) and not IMAGE.search(l)).strip()
        kind, ident = match.group(1).lower(), match.group(2)
        source = next((row for row in rows if re.search(r"\b%s %s\b" % (kind, re.escape(ident)), row["statement"], re.I)),
                      None)
        data = command = ""
        if source:
            spans = re.findall(r"(data|command)\s+`([^`]+)`", source["source"], re.I)
            data = "; ".join(value for name, value in spans if name.lower() == "data")
            command = "; ".join(value for name, value in spans if name.lower() == "command")
        out.append({
            "kind": kind, "id": ident, "title": match.group(3), "section": current_section, "line": index + 1,
            "caption": caption, "image": image.group(1) if image else (link.group(1) if link else ""),
            "repo": link.group(2) if link else "", "ref": link.group(3) if link else "",
            "path": link.group(4) if link else "", "has_table": bool(table_rows(text)), "source": source,
            "data": data, "command": command,
        })
    return out


def numbers(body):
    """Candidate quantities "<value> of <denominator> <phrase>" in the text sections, for consistency review."""
    out = []
    for section in sections(body):
        if section["heading"].lower() == recortex.SOURCES_HEADING.lower():
            continue
        for sentence in re.split(r"(?<=[.;])\s+|\n+", section["text"]):
            for match in NUMBER.finditer(sentence):
                words = [w.lower() for w in match.group(3).split() if w.lower() not in STOP]
                out.append({"section": section["heading"], "value": match.group(1).replace(",", ""),
                            "denominator": match.group(2).replace(",", ""), "phrase": " ".join(words[:3]),
                            "sentence": sentence.strip()[:300]})
    return out


def mentions(body):
    """`#N` pointers outside code, as dicts: target, cited (the text in parentheses after it, if any), line."""
    out, fence = [], None
    for number, line in enumerate(body.replace("\r\n", "\n").split("\n"), 1):
        stripped = line.strip()
        opening = re.match(r"^(```+|~~~+)", stripped)
        if fence is None and opening:
            fence = opening.group(1)
            continue
        if fence is not None:
            if stripped.startswith(fence) and stripped.strip(fence[0]) == "":
                fence = None
            continue
        line = re.sub(r"`[^`\n]*`", " ", line)
        for match in MENTION.finditer(line):
            if len(match.group(1)) == 6:
                continue
            out.append({"target": int(match.group(1)), "cited": (match.group(2) or "").strip(), "line": number})
    return out


# ---------------------------------------------------------------- times

def to_iso(stamp):
    """'2026-03-04 14:10 PST' to ISO with the offset of its abbreviation ('2026-03-04T14:10-08:00'; UTC and any zone at
    offset zero give 'Z'). A zone that zones.py does not know is kept as local date and time without an offset: the time
    GitHub gave the comment orders it (see instant)."""
    if not stamp:
        return None
    match = re.match(r"([0-9]{4}-[0-9]{2}-[0-9]{2}) ([0-9]{2}:[0-9]{2})(?: ([A-Z]{2,5}|UTC[+-][0-9]{1,2}(?::[0-9]{2})?)(?![A-Za-z0-9+:-]))?",
                     stamp)
    if not match:
        return None
    minutes = zones.offset_of(match.group(3)) if match.group(3) else None
    if minutes is None:
        return "%sT%s" % (match.group(1), match.group(2))
    if minutes == 0:
        return "%sT%sZ" % (match.group(1), match.group(2))
    return "%sT%s%s%02d:%02d" % (match.group(1), match.group(2), "-" if minutes < 0 else "+", abs(minutes) // 60,
                                  abs(minutes) % 60)


def sort_key(at):
    """Order ISO times with offsets, Z and naive local times on one UTC axis (naive counts as UTC)."""
    if not at:
        return ""
    try:
        value = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return at


def instant(at, created_at=None):
    """The time of a stamp on one UTC axis, sortable: `at` itself when it carries a zone (a marker's offset, Z, or what
    to_iso made of a heading's abbreviation), else `created_at`, the time GitHub gave the comment, when that is known.
    A stamp with no zone cannot be placed by its own words, so it is never taken for UTC."""
    if at and re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", at):
        return sort_key(at)
    return sort_key(created_at) if created_at else sort_key(at)


def event_at(event, created_at):
    """The time of an event as the index keeps it: the marker's time, else the heading's, else GitHub's."""
    return event["at"] or to_iso(event["heading_time"]) or created_at


# ---------------------------------------------------------------- memories: links

LINK_KINDS = ("because", "replaces", "instead", "checks", "against")
LINK_VALUE = re.compile(r"[1-9][0-9]{0,18}(?:,[1-9][0-9]{0,18}){0,3}")  # one to four comment ids; use fullmatch
# Since 1.5.0 a memory may also mark steps of a task: `done=` on a result, a failure, a correction or a checkpoint, and
# `dropped=` on a decision. The value names one to four steps memories, each with the numbers of its steps that the
# mark is about: `done=<steps comment id>:<n>[,<n>...][;<steps comment id>:<n>...]`. A mark is not a link: a checkpoint
# still carries no link, and what a mark counts for is decided by task_rules.
MARK_KEYS = ("done", "dropped")
STEP_NUMBER = r"(?:[1-9]|1[0-9]|20)"
MARK_GROUP = r"[1-9][0-9]{0,18}:%s(?:,%s){0,19}" % (STEP_NUMBER, STEP_NUMBER)
MARK_VALUE = re.compile(r"%s(?:;%s){0,3}" % (MARK_GROUP, MARK_GROUP))   # use fullmatch
MARK_RULES = {"done": frozenset(("result", "failure", "correction", "checkpoint")), "dropped": frozenset(("decision",))}
STEPS_MAX = 20
MARKER_KEYS = ("type", "at", "recorded", "source", "eventkey") + LINK_KINDS + MARK_KEYS
SIX = ("result", "decision", "failure", "correction", "idea", "question")  # the memories of 0.6.0 that may link
LINKABLE = SIX + ("plan", "text", "steps")  # a checkpoint never links; a text change links only as the carrier of because; a task's steps only restate (replaces)
# key -> the kind of the memory that carries it -> the kinds it may point at. Nothing points at a text change.
LINK_RULES = {
    "because": dict({kind: frozenset(SIX + ("plan",)) for kind in SIX + ("plan",)},
                    text=frozenset(("result", "decision", "correction", "plan"))),
    "replaces": dict({kind: frozenset([kind]) for kind in SIX + ("plan", "steps")}, correction=frozenset(SIX)),
    "instead": dict({kind: frozenset(["idea", "decision"]) for kind in ("decision", "idea", "result")},
                    decision=frozenset(["idea", "decision", "plan"]), plan=frozenset(["plan"])),
    "checks": {"result": frozenset(["question", "idea", "decision", "plan"]),
               "failure": frozenset(["question", "idea", "decision", "plan"]),
               "decision": frozenset(["question"])},
    "against": {"result": frozenset(["plan"])},
}


def link_allowed(source_kind, key, target_kind):
    """True when a memory of `source_kind` may carry `key` pointing at a memory of `target_kind`."""
    return target_kind in LINK_RULES.get(key, {}).get(source_kind, ())


def links(marker):
    """[(key, comment id)] of the link keys of a parsed marker, in the order written. A key whose whole value is not
    one to four comment ids is ignored; the others are kept."""
    out = []
    for key, value in (marker or {}).items():
        if key in LINK_KINDS and LINK_VALUE.fullmatch(value or ""):
            for part in value.split(","):
                if (key, int(part)) not in out:
                    out.append((key, int(part)))
    return out


def marks(marker):
    """[(key, steps comment id, step number)] of the mark keys of a parsed marker, in the order written ("done" or
    "dropped"). A key whose whole value is not in the form of MARK_VALUE is ignored; the others are kept. The same
    step is listed once."""
    out = []
    for key, value in (marker or {}).items():
        if key in MARK_KEYS and MARK_VALUE.fullmatch(value or ""):
            for group in value.split(";"):
                cid, numbers = group.split(":")
                for number in numbers.split(","):
                    item = (key, int(cid), int(number))
                    if item not in out:
                        out.append(item)
    return out


# ---------------------------------------------------------------- memories: the lines of a comment

PLAIN_LABELS = {
    "plan": "In plain words, we plan to",
    "text": "In plain words, we changed the text to say that",
    "result": "In plain words, we found that",
    "decision": "In plain words, we decided to",
    "failure": "In plain words, what went wrong",
    "idea": "In plain words, we had the idea to",
    "question": "In plain words, we asked",
    "correction": "In plain words, we corrected ourselves",
    "checkpoint": "In plain words, we paused here",
    "steps": "In plain words, we agreed to",
}
FIELD = re.compile(r"^\*\*([A-Z][^*\n]{0,60}):\*\*")
EVIDENCE_LABEL = "Evidence"
EVIDENCE_TYPES = ("result", "decision", "failure", "correction")  # the kinds that may list Evidence
FENCE_LINE = re.compile(r"^\s*(```+|~~~+)(.*)$")  # as lint_drafts.split_regions reads a fence
FOLD_START = re.compile(r"^\s*<details", re.I)
FOLD_END = re.compile(r"^\s*</details", re.I)


def body_lines(body):
    return (body or "").replace("\r\n", "\n").split("\n")


def code_lines(lines):
    """Indexes of the lines inside fenced code blocks, the fence lines included."""
    inside, fence = set(), None
    for index, line in enumerate(lines):
        match = FENCE_LINE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                inside.add(index)
            continue
        inside.add(index)
        if match and match.group(1).startswith(fence) and not match.group(2).strip():
            fence = None
    return inside


def quote_lines(lines, code):
    """Indexes of the lines of block quotes: each line that starts with '>', and the lines that continue its paragraph
    without a blank line between (GitHub shows those inside the quote too)."""
    inside, open_quote = set(), False
    for index, line in enumerate(lines):
        if index in code:
            open_quote = False
        elif line.lstrip(" ").startswith(">"):
            inside.add(index)
            open_quote = True
        elif open_quote and line.strip() and not FOLD_START.match(line):
            inside.add(index)
        else:
            open_quote = False
    return inside


def comment_lines(lines, code):
    """Indexes of the lines of HTML comments that start a line (<!-- ... -->), which GitHub does not show."""
    inside, open_comment = set(), False
    for index, line in enumerate(lines):
        if index in code:
            continue
        if not open_comment and line.lstrip(" ").startswith("<!--"):
            open_comment = True
        if open_comment:
            inside.add(index)
            if "-->" in line[line.find("<!--") + 4 if "<!--" in line else 0:]:
                open_comment = False
    return inside


def fold_spans(body):
    """[(first, last)] line indexes (0-based, inclusive) of every <details> block outside code, well formed or not:
    from a line that starts with <details to the next line that starts with </details, or to the last line."""
    lines = body_lines(body)
    code = code_lines(lines)
    spans, start = [], None
    for index, line in enumerate(lines):
        if index in code:
            continue
        if start is None and FOLD_START.match(line):
            start = index
        elif start is not None and FOLD_END.match(line):
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(lines) - 1))
    return spans


def scan_fields(body):
    """The fields before the first fold, as dicts: label, first (the text on the label's line), text, line (index of
    the label's line) and parts, the (line index, text) pieces the text is made of."""
    lines = body_lines(body)
    code = code_lines(lines)
    quote = quote_lines(lines, code)
    hidden = comment_lines(lines, code)
    spans = fold_spans(body)
    stop = spans[0][0] if spans else len(lines)
    out = []
    for index in range(stop):
        line = lines[index]
        if index in hidden:
            continue
        match = None if index in code or index in quote else FIELD.match(line)
        if match:
            out.append({"label": match.group(1), "first": line[match.end():].strip(), "line": index,
                        "parts": [(index, line[match.end():])]})
        elif out:
            out[-1]["parts"].append((index, line))
    for field in out:
        field["text"] = "\n".join(text for _, text in field["parts"]).strip()
    return out


def fields(body):
    """The `**Label:**` fields of an event comment in order, as (label, text). A field starts at a line that begins
    with `**Label:**`, outside code, outside a block quote and before the first fold, and runs to the next field;
    HTML comments are left out. The plain line is one of them, under its own label."""
    return [(field["label"], field["text"]) for field in scan_fields(body)]


def plain(body, kind):
    """The plain line of a memory of `kind`: the text after the kind's label on the line of its first such field,
    or None."""
    label = PLAIN_LABELS.get(kind)
    return next((field["first"] for field in scan_fields(body) if field["label"] == label), None)


# ---------------------------------------------------------------- memories: words and citations in the text

EXTENSIONS = ("py", "md", "json", "tsv", "csv", "txt", "yml", "yaml", "sh", "toml", "ipynb", "png", "pdf", "html", "log")
FILE_WORD = re.compile(r".+\.(?:%s)" % "|".join(EXTENSIONS), re.I)
ISSUE_NUMBER = re.compile(r"(?<![A-Za-z0-9_/&#])#[0-9]+\b")
NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9.,_])(?<![A-Za-z]-)[0-9]+(?:[.,][0-9]+)*")


def looks_like_path(token):
    """A token with / that also holds a ., a digit or a second /, or a word that ends in a file extension. Two whole
    numbers around one / ("1/3", "24/7") are a fraction or a rate, not a path."""
    token = token.strip(",;:!?()[]{}\"'")
    if re.fullmatch(r"[0-9]+/[0-9]+", token):
        return False
    slashed = token.rstrip(".")
    if "/" in slashed and ("." in slashed or re.search(r"[0-9]", slashed) or slashed.count("/") > 1):
        return True
    return bool(FILE_WORD.fullmatch(token))


def line_problems(text):
    """What a plain line or a fold's summary may not hold: a backtick, an issue number, a URL, a path, a commit."""
    found = []
    if "`" in text:
        found.append("a backtick")
    if ISSUE_NUMBER.search(text):
        found.append("an issue number (#N)")
    if URL.search(text):
        found.append("a URL")
    if any(looks_like_path(token) for token in text.split()):
        found.append("a path or file name")
    if COMMIT.search(text):
        found.append("a commit hash")
    return found


def number_tokens(text):
    """The numbers in a text, as strings with thousands commas removed: "UTF-8", "v2" and "sha256" hold none; "22%",
    "4GB", "3-5" and "0.3.0" do."""
    return {match.group(0).replace(",", "") for match in NUMBER_TOKEN.finditer(text or "")}


FIELD_NUMBER = re.compile(r"(?<![A-Za-z0-9.,_])[0-9]+(?:[.,][0-9]+)*")  # NUMBER_TOKEN without its hyphen rule
SCALE_WORD = re.compile(r"[ \t]+(thousand|million|billion)(?![A-Za-z0-9])", re.I)
SCALES = {"thousand": 3, "million": 6, "billion": 9}
ROUNDING_TOLERANCE = decimal.Decimal("0.1")


def field_numbers(text):
    """The numbers in a field, with thousands commas removed. Unlike number_tokens, a number after a letter and a
    hyphen counts ("release-0.3.0" holds 0.3.0), so that a number the field holds in that spelling is found."""
    return {match.group(0).replace(",", "") for match in FIELD_NUMBER.finditer(text or "")}


def plain_numbers(text):
    """The numbers of a plain line as (as written, token, power of ten): "2 million" is ("2 million", "2", 6)."""
    found = []
    for match in NUMBER_TOKEN.finditer(text or ""):
        scale = SCALE_WORD.match(text, match.end())
        found.append((match.group(0) + (scale.group(0) if scale else ""), match.group(0).replace(",", ""),
                      SCALES[scale.group(1).lower()] if scale else 0))
    return found


def number_stands(token, power, fields):
    """True when a number of the plain line stands in the fields: as written; as the start of a longer version
    number ("0.3" for "0.3.0"); or rounded, when a field holds a number that rounds to it at the precision the plain
    line gives and differs from it by at most a tenth ("about 3 times" for 3.05, "40 MB" for 38 and 41 MB,
    "2 million" for 2,000,000)."""
    if not power and (token in fields or any(other.startswith(token + ".") and other.count(".") > 1 for other in fields)):
        return True
    if token.count(".") > 1:
        return False
    value = decimal.Decimal(token).scaleb(power)
    if not value or value == 100:
        return False  # 100 says "all" (100%, all 100 files): it stands only as written, never for 98
    # the precision the plain line gives: its last decimal as written, or its last digit that is not a zero
    step = decimal.Decimal(1).scaleb(power - len(token.split(".")[1]) if "." in token
                                     else value.normalize().as_tuple().exponent)
    for other in fields:
        if other.count(".") > 1 or re.fullmatch(r"0[0-9].*|(?:19|20)[0-9]{2}", other):
            continue  # a version number, the 03 of a date, or its year
        number = decimal.Decimal(other)
        rounded = (number / step).quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP) * step
        if rounded == value and abs(number - value) <= ROUNDING_TOLERANCE * value:
            return True
    return False


GITHUB_ADDRESS = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9._-]{1,100})/(?:issues|pull)/"
                            r"([0-9]{1,9})(?:#issuecomment-([0-9]{1,19}))?(?![A-Za-z0-9_/-])")
SHORT_REFERENCE = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9._-]{1,100})#([0-9]{1,9})"
                             r"(?![0-9])")
BARE_ISSUE = re.compile(r"(?<![A-Za-z0-9_/&#])#([0-9]+)\b")
# an entry of a local record, cited as #N/ID: its topic and its number (read only with citations(local=True))
LOCAL_ENTRY = re.compile(r"(?<![A-Za-z0-9_/&#])#([0-9]{1,9})/([0-9]{10,19})(?![0-9])")


def masked(text):
    """The text with fenced code and inline code replaced by spaces, so that offsets stay the same."""
    lines = body_lines(text)
    code = code_lines(lines)
    out = []
    for index, line in enumerate(lines):
        out.append(" " * len(line) if index in code else re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group(0)), line))
    return "\n".join(out)


def citations(text, repo, local=False):
    """Issues and memories cited in a text, outside code, in order, as dicts with kind, issue, comment, start and end
    (offsets in the text with LF line endings): kind "issue" for #N, for an issue or pull request address of `repo`
    and for `repo`#N; "memory" for a comment address of `repo`; "elsewhere" for any other github.com issue or comment
    address or owner/name#N. With `local` (the record is a local one) #N/ID is a memory of the topic N, read before the
    bare issues; without it every result is as it always was."""
    plain_text = masked(text)
    home = (repo or "").lower()
    found, taken = [], []
    if local:
        for match in LOCAL_ENTRY.finditer(plain_text):
            found.append({"kind": "memory", "issue": int(match.group(1)), "comment": int(match.group(2)),
                          "start": match.start(), "end": match.end()})
            taken.append((match.start(), match.end()))
    for match in GITHUB_ADDRESS.finditer(plain_text):
        same = match.group(1).lower() == home
        kind = ("memory" if match.group(3) else "issue") if same else "elsewhere"
        found.append({"kind": kind, "issue": int(match.group(2)) if same else None,
                      "comment": int(match.group(3)) if same and match.group(3) else None,
                      "start": match.start(), "end": match.end()})
        taken.append((match.start(), match.end()))
    inside = lambda position: any(start <= position < end for start, end in taken)
    for match in SHORT_REFERENCE.finditer(plain_text):
        if inside(match.start()):
            continue
        same = match.group(1).lower() == home
        found.append({"kind": "issue" if same else "elsewhere", "issue": int(match.group(2)) if same else None,
                      "comment": None, "start": match.start(), "end": match.end()})
        taken.append((match.start(), match.end()))
    for match in BARE_ISSUE.finditer(plain_text):
        if inside(match.start()) or len(match.group(1)) == 6:  # six digits after # read as a hex color
            continue
        found.append({"kind": "issue", "issue": int(match.group(1)), "comment": None,
                      "start": match.start(), "end": match.end()})
    return sorted(found, key=lambda item: item["start"])


# ---------------------------------------------------------------- memories: evidence

EVIDENCE_LINE = re.compile(
    r"- (?:(table|figure) ([A-Za-z0-9][A-Za-z0-9_.-]{0,59})"
    r"|code ([A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*)#L([1-9][0-9]{0,6})(?:-L([1-9][0-9]{0,6}))? at ([0-9a-f]{7,40})"
    r"(?: in ([^ ]+))?"
    r"|pull ([1-9][0-9]{0,6})(?: in ([^ ]+))?)")
EVIDENCE_MAX_LINES = 6
CODE_MAX_LINES = 120


def evidence_line(line):
    """(row, None) for a line of an Evidence field in one of the four forms, else (None, why)."""
    match = EVIDENCE_LINE.fullmatch(line.rstrip())
    if not match:
        return None, ("not one of the four forms: '- table <id>', '- figure <id>', "
                      "'- code <path>#L<a>-L<b> at <commit>' or '- pull <n>'")
    block, ident, path, first, last, commit, code_repo, pull, pull_repo = match.groups()
    other = code_repo or pull_repo
    if other is not None and not recortex.valid_repo(other):
        return None, "'in' names a repository as owner/name"
    if block:
        return {"kind": block, "ident": ident, "repo": "", "ref": "", "path": "", "line_from": None,
                "line_to": None}, None
    if pull:
        return {"kind": "pull", "ident": pull, "repo": other or "", "ref": "", "path": "", "line_from": None,
                "line_to": None}, None
    if len(path) > 200 or any(part in (".", "..") for part in path.split("/")):
        return None, "a path is relative to the repository, at most 200 characters, with no '.' or '..' part"
    first, last = int(first), int(last or first)
    if last < first:
        return None, "the line range ends before it starts"
    if last - first + 1 > CODE_MAX_LINES:
        return None, "the line range holds more than %d lines" % CODE_MAX_LINES
    return {"kind": "code", "ident": "", "repo": other or "", "ref": commit, "path": path, "line_from": first,
            "line_to": last}, None


def evidence_text(body):
    """The text of the first Evidence field, or None."""
    for label, text in fields(body):
        if label == EVIDENCE_LABEL:
            return text
    return None


def evidence(body):
    """The lines of the Evidence field in one of the four forms, as dicts: kind (table, figure, code or pull), ident,
    repo, ref, path, line_from, line_to. Other lines are left out."""
    rows = []
    for line in (evidence_text(body) or "").split("\n"):
        row, problem = evidence_line(line) if line.strip() else (None, None)
        if row:
            rows.append(row)
    return rows


# ---------------------------------------------------------------- memories: plans and changes of the text

PLAN_CHANGES = ("narrows", "rewords", "widens", "reverses")   # what a revised plan memory does to the one it replaces
SHA256 = r"[0-9a-f]{64}"
BODY_HASHES = re.compile(r"before sha256 `(%s|none)`; after sha256 `(%s)`" % (SHA256, SHA256))
COMMIT_ID = re.compile(r"[0-9a-f]{40}")
LOGIN_TEXT = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?"
WHO = r"[A-Za-z][A-Za-z0-9 .'-]{0,59}"
# The six forms that the Approved field of a text change starts with, as (key, pattern, a quote follows). The key is
# what the index and the page read; the words are fixed, so that nothing of the record's own text is told.
APPROVED_FORMS = (
    ("autonomous", re.compile(r"Written without asking, under the autonomous mode\."), False),
    ("chat", re.compile(r"By %s in the chat, %s:" % (WHO, STAMP)), True),
    ("merged", re.compile(r"By %s on GitHub, by merging pull request [1-9][0-9]{0,8}\." % LOGIN_TEXT), False),
    ("edited", re.compile(r"Edited by %s on GitHub, %s\." % (LOGIN_TEXT, STAMP)), False),
    ("restored", re.compile(r"Restored by %s in the chat, %s:" % (WHO, STAMP)), True),
    ("kept", re.compile(r"Kept as a file later, for the text change of comment [1-9][0-9]{0,9}\."), False),
)
# The reasons a version was not kept as a file (references/versions.md): the Version field says "Not kept: <reason>".
NOT_KEPT = ("this account may not write files to the repository", "a branch rule refused the write",
            "the branch changed while writing", "versions are not kept here")
KEPT_FROM_AUTHOR = re.compile(r"(?:[1-9][0-9]{0,3} paragraphs? (?:was|were) changed by hand and (?:was|were) left as "
                              r"(?:it is|they are)\.|Nothing was changed by hand\.)")
VERSION_NAME = re.compile(r"Version name: ([a-z0-9][a-z0-9.-]{0,39}); commit (%s); manifest sha256 (%s)" % ("[0-9a-f]{40}", SHA256))


# The two forms that the Agreed field of a steps memory starts with, as (key, pattern, a quote follows): the user approved
# the plan in the plan dialog, or said so in the chat, in words the memory quotes.
AGREED_FORMS = (
    ("dialog", re.compile(r"Approved in the plan dialog, %s\." % STAMP), False),
    ("chat", re.compile(r"By %s in the chat, %s:" % (WHO, STAMP)), True),
)
STEP_LINE = re.compile(r"^([0-9]{1,3})\. (\S.*)$")
STEP_TEXT_MAX = 300


def agreed_form(text):
    """The key of the form that the text of an Agreed field starts with ("dialog" or "chat"), or None. Only the first
    line is read; the chat form needs a quote after it."""
    lines = (text or "").split("\n")
    for key, pattern, quoted in AGREED_FORMS:
        if pattern.fullmatch(lines[0].strip()):
            if quoted and not any(line.lstrip().startswith(">") and line.lstrip(" >").strip() for line in lines[1:]):
                return None
            return key
    return None


def steps_items(body):
    """[(written number, text)] of the lines `N. text` in the **Steps:** field of a steps memory, in order; other lines
    of the field are left out. The number is as written (the linter checks it)."""
    text = field_text(body, "Steps") or ""
    return [(int(match.group(1)), match.group(2).strip()) for match in
            (STEP_LINE.match(line.strip()) for line in text.split("\n")) if match]


def steps_list(body):
    """The steps of a steps memory as texts, in order: the first is step 1. At most STEPS_MAX; the reader is lenient
    (the numbers as written are not used) and the linter is strict."""
    return [text for _, text in steps_items(body)][:STEPS_MAX]


def body_bytes(text):
    """The bytes a topic body is kept and hashed as: its normalized text (LF endings, no trailing blanks) and one final
    line break. The hashes of the Body field of a text comment, the files of the versions and every check agree on
    this form."""
    return (recortex.normalize(text or "") + "\n").encode("utf-8")


def body_sha(text):
    """The SHA-256 of body_bytes(text), 64 lowercase hexadecimal characters."""
    import hashlib
    return hashlib.sha256(body_bytes(text)).hexdigest()


def section_sha(body, heading):
    """The SHA-256 of the text of one section of a body (its text with the surrounding blank lines removed), 64
    hexadecimal characters, or None when the body has no such section. What was last brought into the author's own
    paper is remembered by this value."""
    import hashlib
    text = section_text(body, heading)
    return None if text is None else hashlib.sha256(text.strip("\n").encode("utf-8")).hexdigest()


def field_text(body, label):
    """The text of the first field called `label` in an event comment, or None."""
    for found, text in fields(body):
        if found == label:
            return text
    return None


RESUME_LABELS = ("**In short:**", "**Last updated:**", "**Current focus:**", "**Active issues:**", "**Last decisions:**",
                 "**Next actions:**", "**Holds:**")
IN_SHORT_LIMIT = 1200   # characters of the "In short" field of a resume block: two short plain paragraphs
ISSUE_NUMBER_IN_PROSE = re.compile(r"#[0-9]")


def resume_in_short(block):
    """The "In short" field of a resume block (the text between its markers), its paragraphs joined by one blank line, or
    None when the block has none or it is empty. The field runs from after its label to the line that starts the next
    field of the block. Untrusted text: the caller cleans it before it is shown."""
    lines = (block or "").replace("\r\n", "\n").split("\n")
    out, inside = [], False
    for line in lines:
        if not inside:
            if line.startswith(RESUME_LABELS[0]):
                inside = True
                out.append(line[len(RESUME_LABELS[0]):])
            continue
        if line.startswith(RESUME_LABELS[1:]):
            break
        out.append(line)
    paragraphs, current = [], []
    for line in out:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) if paragraphs else None


def approved_form(text):
    """The key of the form that the text of an Approved field starts with ("autonomous", "chat", "merged", "edited",
    "restored" or "kept"), or None. Only the first line is read; a form that wants a quote needs one after it."""
    lines = (text or "").split("\n")
    for key, pattern, quoted in APPROVED_FORMS:
        if pattern.fullmatch(lines[0].strip()):
            if quoted and not any(line.lstrip().startswith(">") and line.lstrip(" >").strip() for line in lines[1:]):
                return None
            return key
    return None


def sha_pair(text):
    """(before, after) of a Body field, each a 64-character hash, before None when it says none; or None when the text
    is not in the form 'before sha256 `<hash or none>`; after sha256 `<hash>`'."""
    match = BODY_HASHES.fullmatch((text or "").strip())
    if not match:
        return None
    return (None if match.group(1) == "none" else match.group(1)), match.group(2)


def version_name(text):
    """(name, commit, manifest hash) from the first line of the Effect field of the decision that names a version
    ('Version name: <name>; commit <40 hex>; manifest sha256 <64 hex>'), or None."""
    first = (text or "").split("\n")[0].strip()
    match = VERSION_NAME.fullmatch(first)
    return match.groups() if match else None


COPY_MADE = re.compile(r"^Copy made: (%s)[ \t]*$" % STAMP, re.M)


def copy_made(body):
    """The ISO time of the 'Copy made: <date time zone>' line of a plan issue's body, or None."""
    match = COPY_MADE.search((body or "").replace("\r\n", "\n"))
    return to_iso(match.group(1)) if match else None


# ---------------------------------------------------------------- citations in Manubot's forms

# The identifiers a prefix takes, as far as the plugin checks them: the form only, never the literature. The prefixes
# are USAGE.md's (doi, pmc, pubmed, arxiv, isbn, wikidata) and the spellings Manubot's own code accepts for the same
# sources (pmid, pmcid; url, http and https for a web address; manubot/cite/pubmed.py and url.py at the commit its
# build environment pins).
MANUBOT_IDENTIFIERS = {
    "doi": re.compile(r"10\.[0-9]{4,9}/[^\s;\[\]]+"),
    "pmc": re.compile(r"PMC[0-9]+"),
    "pmcid": re.compile(r"PMC[0-9]+"),
    "pubmed": re.compile(r"[0-9]+"),
    "pmid": re.compile(r"[0-9]+"),
    "url": re.compile(r"https?://[^\s;\[\]]+"),
    "arxiv": re.compile(r"(?:[0-9]{4}\.[0-9]{4,5}|[a-z-]+(?:\.[A-Z]{2})?/[0-9]{7})(?:v[0-9]+)?"),
    "isbn": re.compile(r"[0-9][0-9X-]{8,16}"),
    "wikidata": re.compile(r"Q[0-9]+"),
}
CITE_GROUP = re.compile(r"\[((?:-?@[^\s;\[\]]+)(?:[ \t]*;[ \t]*-?@[^\s;\[\]]+)*)\](?!:)")
CITE_DEFINITION = re.compile(r"^\[@([A-Za-z0-9_-]+)\]:[ \t]*(\S.*)$", re.M)


def manubot_identifier(prefix, rest):
    """True when `rest` has the form of an identifier of the documented `prefix`."""
    pattern = MANUBOT_IDENTIFIERS.get(prefix)
    return bool(pattern and pattern.fullmatch(rest))


def manubot_definitions(text):
    """{name: what the line defines} of the named references a body defines on lines of its own, '[@name]: doi:...',
    outside code."""
    return {match.group(1): match.group(2).strip() for match in CITE_DEFINITION.finditer(masked(text))}


def citations_manubot(text):
    """The citations of a text in Pandoc's and Manubot's form, '[@key]' and '[@key; @key]', outside code, as dicts:
    key (without '@'), line (1-based), start and end. A line '[@name]: ...' defines a name; it cites nothing."""
    out = []
    plain_text = masked(text)
    for match in CITE_GROUP.finditer(plain_text):
        line = plain_text.count("\n", 0, match.start()) + 1
        for part in match.group(1).split(";"):
            key = part.strip().lstrip("-")[1:]
            out.append({"key": key, "line": line, "start": match.start(), "end": match.end()})
    return out


# ---------------------------------------------------------------- memories: folds

SUMMARY_LINE = re.compile(r"<summary>(.*)</summary>")
WORD_STEM = re.compile(r'We (now )?use "([^"]*)" to mean (.*)')
THREAD_STEM = re.compile(r"This thread is (now )?about (.*)")
WORD_TEXT = re.compile(r"[A-Za-z0-9][^\"<>&`\x00-\x1f\x7f]{0,59}")
WHY_LABELS = {"word": "Why it matters here", "thread": "Why we care"}
ALSO_LABEL, STORY_LABEL = "Also called", "The longer story"
HEADING_LINE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
FOLD_TAG = re.compile(r"</?(?:details|summary)\b", re.I)
WHY_LIMIT, STORY_LIMIT, MEANING_RANGE, ALSO_LIMIT = 400, 2500, (20, 200), 5


def word_key(word):
    """How a word is known: lowercase, inner spaces collapsed, a leading "the", "a" or "an" dropped."""
    key = " ".join((word or "").lower().split())
    return re.sub(r"^(?:the|a|an) ", "", key)


def word_problems(word):
    if not WORD_TEXT.fullmatch(word) or word != word.strip():
        return ['a word is 1 to 60 characters that start with a letter or digit, without ", <, >, & or a backtick']
    return []


def meaning_problems(meaning):
    found = []
    if not MEANING_RANGE[0] <= len(meaning) <= MEANING_RANGE[1]:
        found.append("the meaning is %d characters; write %d to %d" % ((len(meaning),) + MEANING_RANGE))
    if meaning.endswith("."):
        found.append("the meaning is one sentence without its period")
    if re.search(r"[<>&]", meaning):
        found.append("the meaning holds <, > or &")
    found += ["the meaning holds %s" % what for what in line_problems(meaning)]
    return found


def parse_fold(lines):
    """(explanation, problems) for the lines of one <details> block. The explanation is a dict (kind "word" or
    "thread", word, revised, meaning, why, also, story) or None; problems are (offset, message) pairs, and the
    explanation is given only when there are none."""
    problems = []

    def bad(offset, message):
        problems.append((offset, message))

    count = len(lines)
    if lines[0].rstrip() != "<details>":
        bad(0, "a fold starts with a line that holds only <details>")
    closed = count > 1 and FOLD_END.match(lines[-1]) is not None
    if not closed or lines[-1].rstrip() != "</details>":
        bad(count - 1, "a fold ends with a line that holds only </details>")
    inner = list(enumerate(lines[1:count - 1 if closed else count], 1))
    fold = {"kind": None, "word": "", "revised": False, "meaning": "", "why": "", "also": [], "story": ""}
    summary = SUMMARY_LINE.fullmatch(inner[0][1].rstrip()) if inner else None
    if not summary:
        bad(1, "the second line of a fold is <summary>...</summary>, on one line")
    else:
        text = summary.group(1)
        word, thread = WORD_STEM.fullmatch(text), THREAD_STEM.fullmatch(text)
        if word:
            fold.update(kind="word", revised=bool(word.group(1)), word=word.group(2), meaning=word.group(3))
            for message in word_problems(word.group(2)):
                bad(1, message)
        elif thread:
            fold.update(kind="thread", revised=bool(thread.group(1)), meaning=thread.group(2))
        else:
            bad(1, 'a summary is \'We use "<word>" to mean <meaning>\', \'We now use "<word>" to mean <meaning>\', '
                   "'This thread is about <meaning>' or 'This thread is now about <meaning>'")
        for message in meaning_problems(fold["meaning"]) if fold["kind"] else []:
            bad(1, message)
        if FOLD_TAG.search(text):
            bad(1, "the summary holds a <details> or <summary> tag")
    if len(inner) > 1 and inner[1][1].strip():
        bad(inner[1][0], "a blank line follows the summary line, or GitHub does not show what follows")
    body = inner[2:]
    paragraphs, current = [], []
    for offset, line in body:
        if line.strip():
            current.append((offset, line))
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    starts = set()
    why = WHY_LABELS.get(fold["kind"], "Why it matters here")
    step = 0
    if paragraphs and paragraphs[0][0][1].startswith("**%s:**" % why):
        part = paragraphs[0]
        starts.add(part[0][0])
        fold["why"] = "\n".join([part[0][1][len(why) + 5:]] + [line for _, line in part[1:]]).strip()
        if not fold["why"]:
            bad(part[0][0], "'%s' is empty" % why)
        if len(fold["why"]) > WHY_LIMIT:
            bad(part[0][0], "'%s' is %d characters; at most %d" % (why, len(fold["why"]), WHY_LIMIT))
        step = 1
    elif fold["kind"]:
        bad(paragraphs[0][0][0] if paragraphs else count - 1,
            "after the summary and a blank line comes '**%s:** <one paragraph>'" % why)
    if step == 1 and fold["kind"] == "word" and len(paragraphs) > 1 \
            and paragraphs[1][0][1].startswith("**%s:**" % ALSO_LABEL):
        part = paragraphs[1]
        starts.add(part[0][0])
        if len(part) > 1:
            bad(part[1][0], "'%s' is one line" % ALSO_LABEL)
        names = [name.strip() for name in part[0][1][len(ALSO_LABEL) + 5:].split(",")]
        if len(names) > ALSO_LIMIT:
            bad(part[0][0], "'%s' names at most %d names" % (ALSO_LABEL, ALSO_LIMIT))
        for name in names:
            for message in word_problems(name):
                bad(part[0][0], "in '%s': %s" % (ALSO_LABEL, message))
        fold["also"] = names
        step = 2
    if step and len(paragraphs) > step and paragraphs[step][0][1].rstrip() == "**%s:**" % STORY_LABEL:
        part = paragraphs[step]
        starts.add(part[0][0])
        if len(part) > 1:
            bad(part[1][0], "a blank line follows '**%s:**'" % STORY_LABEL)
        story = [line for offset, line in body if offset > part[0][0]]
        fold["story"] = "\n".join(story).strip()
        if not fold["story"]:
            bad(part[0][0], "'%s' is empty" % STORY_LABEL)
        if len(fold["story"]) > STORY_LIMIT:
            bad(part[0][0], "'%s' is %d characters; at most %d" % (STORY_LABEL, len(fold["story"]), STORY_LIMIT))
        step = len(paragraphs)
    for part in paragraphs[step:] if step else []:
        bad(part[0][0], "unexpected text in a fold: after '%s' come only '%s' (for a word) and '%s'"
            % (why, ALSO_LABEL, STORY_LABEL))
    for offset, line in body:
        if HEADING_LINE.match(line):
            bad(offset, "a fold holds no heading")
        elif line.lstrip().startswith("<"):
            bad(offset, "a line inside a fold does not start with <")
        elif FENCE_LINE.match(line):
            bad(offset, "a fold holds no code fence")
        elif FOLD_TAG.search(re.sub(r"`[^`\n]*`", "", line)):
            bad(offset, "a fold holds no other <details> or <summary> tag")
        elif FIELD.match(line) and offset not in starts:
            bad(offset, "a fold holds no field of the memory; fields stand before the folds")
    if problems:
        return None, problems
    return fold, []


def folds(body):
    """Every <details> block of a comment: dicts with first, last (line indexes), explanation and problems."""
    lines = body_lines(body)
    out = []
    for first, last in fold_spans(body):
        explanation, problems = parse_fold(lines[first:last + 1])
        out.append({"first": first, "last": last, "explanation": explanation,
                    "problems": [(first + offset, message) for offset, message in problems]})
    return out


def explanations(body):
    """The folds of a memory that match the grammar, as dicts: kind ("word" or "thread"), word, revised (bool),
    meaning, why, also (list), story. A fold that does not match is left out."""
    return [item["explanation"] for item in folds(body) if item["explanation"]]

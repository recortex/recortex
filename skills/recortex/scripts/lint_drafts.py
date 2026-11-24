#!/usr/bin/env python3
"""Lint issue and comment drafts before they are posted to GitHub.

Checks: required sections for the kind of draft, placeholders left in the text,
bare #N references, body length, Mermaid rules, the resume block of a hub,
verbatim quotes in decisions, secrets and project-defined restricted patterns.
A final-record draft is deliverable text: it may hold no history (no "As of"
line, no <details> block, no headings for decisions, open work, provenance or
next steps, no dated headings), and its section headings must belong to the
project's profiles and agree with the labels the issue will carry.

A memory (result, decision, failure, checkpoint, correction, idea, question)
starts with its plain line; its marker may link to earlier memories; it may
list Evidence and end with folded explanations. What only the record can tell
(link targets, cited comments, the blocks named in Evidence, words and threads
already explained) is checked with --index (the local index, no network) or
--online (GitHub) with --issue N; without either, a draft that holds any of it
gets one "not-checked" warning.

Read-only. With --online it asks GitHub whether each bare #N exists; without it
no network access happens.

Examples:
  lint_drafts.py --kind final-record drafts/topic-parser.md
  lint_drafts.py --kind final-record --labels ms:methods,ms:results drafts/topic-parser.md
  lint_drafts.py --kind result --repo owner/name --online drafts/result.md
  lint_drafts.py --kind decision --index --issue 5 drafts/decision.md
  lint_drafts.py --manifest drafts/manifest.json
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recortex  # noqa: E402
import record_format  # noqa: E402
import record_store  # noqa: E402

# A final record holds the deliverable's sections, named by the profile, then Sources.
FINAL_RECORD_SECTIONS = ["## " + recortex.SOURCES_HEADING]

# Headings that carry history or planning, which belong in comments and active issues.
# The whole heading must match, so "Next-generation sequencing" or "Provenance tracking" stay allowed.
HISTORY_HEADING = re.compile(
    r"^(?:decisions?|decision (?:record|log|table)|open (?:work|questions|items)|provenance"
    r"|next(?: (?:steps?|actions?|work))?|current state|not established|code and evidence|related topics"
    r"|history|status)\s*$", re.I)
DATE = re.compile(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b")
# "As of" followed by a date, a month or a year, at the start of a line or of a list item.
AS_OF = re.compile(r"^\s*(?:(?:[-*+]|[0-9]+\.)\s+)?[*_]*as of\s+[*_]*"
                   r"(?:the\s+)?(?:[0-9]{1,2}(?:st|nd|rd|th)?\s+)?(?:(?:19|20)[0-9]{2}\b|[0-9]{1,2}[/.][0-9]|(?:jan(?:uary)?"
                   r"|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?"
                   r"|nov(?:ember)?|dec(?:ember)?)\b)", re.I)

RESUME_FIELDS = [
    "**In short:**",
    "**Last updated:**",
    "**Current focus:**",
    "**Active issues:**",
    "**Last decisions:**",
    "**Next actions:**",
    "**Holds:**",
]



def plain_field(kind):
    """The label of a memory's plain line, as it stands in the comment: **In plain words, ...:**"""
    return "**%s:**" % record_format.PLAIN_LABELS[kind]


KINDS = {
    "hub": {
        "sections": [
            "## Deliverable text",
            "## Resume here",
            "## Project",
            "## Map",
            "## Deliverable sections",
            "## Active issues",
            "## Pull requests",
            "## Labels",
            "## Older tracker",
            "## Recording rules",
        ],
    },
    "final-record": {"sections": FINAL_RECORD_SECTIONS},
    "plan-body": {
        "sections": ["## Message", "## Claims", "## Figures and tables", "## Discussion", "## Open gaps"],
    },
    "active": {
        "sections": ["## Goal", "## Destination topic", "## Done when", "## Plan", "## Result"],
    },
    "result": {
        "header": "Result",
        "sections": [
            plain_field("result"),
            "**What ran:**",
            "**Configuration:**",
            "**Numbers:**",
            "**Exclusions and failures:**",
            "**Cost:**",
            "**Not established:**",
            "**Next step:**",
        ],
    },
    "decision": {
        "header": "Decision",
        "sections": [
            plain_field("decision"),
            "**Decision:**",
            "**Decided by:**",
            "**Exact words:**",
            "**Where recorded:**",
            "**Effect:**",
            "**Next step:**",
        ],
    },
    "failure": {
        "header": "Failure",
        "sections": [
            plain_field("failure"),
            "**What failed:**",
            "**What ran:**",
            "**Cause:**",
            "**Impact:**",
            "**Not established:**",
            "**Next step:**",
        ],
    },
    "checkpoint": {
        "header": "Checkpoint",
        "sections": [
            plain_field("checkpoint"),
            "**State:**",
            "**Done since the last checkpoint:**",
            "**In progress:**",
            "**Next actions:**",
            "**Open questions:**",
        ],
    },
    "correction": {
        "header": "Correction",
        "sections": [
            plain_field("correction"),
            "**Corrects:**",
            "**Was:**",
            "**Is:**",
            "**Why:**",
            "**Also updated:**",
        ],
    },
    "idea": {
        "header": "Idea",
        "sections": [
            plain_field("idea"),
            "**Why it might help:**",
            "**What it would take:**",
            "**Next step:**",
        ],
    },
    "question": {
        "header": "Question",
        "sections": [
            plain_field("question"),
            "**Why it matters:**",
            "**What would answer it:**",
            "**Next step:**",
        ],
    },
    "plan": {
        "header": "Plan",
        "sections": [
            plain_field("plan"),
            "**Part:**",
            "**Section:**",
            "**Topic:**",
            "**Statement:**",
            "**It needs:**",
            "**We have:**",
            "**If it does not hold:**",
            "**Next step:**",
        ],
    },
    "text": {
        "header": "Text",
        "sections": [
            plain_field("text"),
            "**Section:**",
            "**What changed:**",
            "**Why:**",
            "**Approved:**",
            "**Kept from the author:**",
            "**Body:**",
            "**Version:**",
        ],
    },
    "steps": {
        "header": "Steps",
        "sections": [
            plain_field("steps"),
            "**Agreed:**",
            "**Steps:**",
            "**Done when:**",
        ],
    },
    "proposal": {
        "header": "Proposal",
        "sections": [
            "**Section:**",
            "**What would change:**",
            "**Why:**",
            "**Pull request:**",
            "**Base:**",
            "**Text:**",
        ],
    },
    "triage": {
        "header": "Triage",
        "sections": [
            "**Opened by:**",
            "**Their words:**",
            "**Type:**",
            "**Route:**",
            "**Reason:**",
            "**Needs confirmation:**",
        ],
    },
    "resume": {"header": "Resume block", "sections": RESUME_FIELDS},
    "continuation": {"sections": []},
    "pointer": {"sections": []},
    "generic": {"sections": []},
}

# Date, time and zone, for example "2026-03-04 14:10 PST" or "2026-03-04 14:10 UTC+1".
STAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} (?:[A-Z]{2,5}|UTC[+-][0-9]{1,2}(?::[0-9]{2})?)\b"

SECRET_PATTERNS = [
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    ("GitHub fine-grained token", r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("API key", r"\bsk-[A-Za-z0-9_-]{32,}"),
]

BARE_REF = re.compile(r"(?<![\w/&#])#([0-9]+)\b")
TEMPLATE_FIELD = re.compile(r"\{\{[^{}\n]*\}\}")
FENCE = re.compile(r"^\s*(```+|~~~+)(.*)$")

ARROW = re.compile(r"<?(?:-\.+->?|-{2,}[>ox]?|={2,}[>ox]?)")
NODE_ID = re.compile(r"^[A-Za-z0-9_]+$")
SKIP_PREFIXES = ("classDef ", "class ", "style ", "linkStyle ", "click ", "direction ")


class Finding:
    def __init__(self, level, code, line, message):
        self.level, self.code, self.line, self.message = level, code, line, message

    def __str__(self):
        where = "line %d" % self.line if self.line else "whole text"
        return "%s [%s] %s: %s" % (self.level.upper(), self.code, where, self.message)


def split_regions(text):
    """Yield (line_number, line, region) where region is prose, code or mermaid.

    Fence lines themselves are reported as region "fence".
    """
    fence, region = None, "prose"
    for number, line in enumerate(text.split("\n"), 1):
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            region = "mermaid" if match.group(2).strip().lower() == "mermaid" else "code"
            yield number, line, "fence"
            continue
        if fence is not None and match and match.group(1).startswith(fence) and not match.group(2).strip():
            fence, region = None, "prose"
            yield number, line, "fence"
            continue
        yield number, line, region


def strip_inline_code(line):
    return re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group(0)), line)


def lint_mermaid(block):
    """Check one Mermaid block. `block` is a list of (line_number, line)."""
    findings = []

    def add(level, code, line, message):
        findings.append(Finding(level, code, line, message))

    for number, line in block:
        if "#" in line:
            add("error", "mermaid-hash", number,
                "'#' inside a Mermaid block breaks rendering on GitHub; write 'Issue N'")
        if "(" in line or ")" in line:
            add("error", "mermaid-paren", number,
                "parentheses inside a Mermaid block; use [\"...\"] nodes and plain words")

    content = [(n, l.strip()) for n, l in block if l.strip() and not l.strip().startswith("%%")]
    if not content:
        add("error", "mermaid-empty", block[0][0] if block else 0, "empty Mermaid block")
        return findings
    if not re.match(r"^(flowchart|graph)\b", content[0][1]):
        return findings  # node rules below are written for flowcharts only

    defined, used, first_seen = {}, set(), {}
    for number, line in content[1:]:
        if line == "end" or line.startswith(SKIP_PREFIXES):
            continue
        if line.startswith("subgraph"):
            rest = line[len("subgraph"):].strip()
            if "[" in rest and not re.search(r'\["[^"]*"\]\s*$', rest):
                add("error", "mermaid-quote", number, "subgraph title must be double-quoted: id[\"Title\"]")
            continue
        for statement in line.split(";"):
            statement = statement.strip()
            if not statement:
                continue
            for label in re.findall(r"\|([^|]*)\|", statement):
                if not re.fullmatch(r'\s*"[^"]*"\s*', label):
                    add("error", "mermaid-quote", number, "edge label must be double-quoted: |\"%s\"|" % label.strip())
            statement = re.sub(r"\|[^|]*\|", " ", statement)
            for opener, closer in (("[", "]"), ("{", "}")):
                pattern = re.escape(opener) + r"+([^" + re.escape(opener + closer) + r"]*)" + re.escape(closer) + r"+"
                for label in re.findall(pattern, statement):
                    if not re.fullmatch(r'"[^"]*"', label.strip()):
                        add("error", "mermaid-quote", number,
                            "node label must be double-quoted: %s\"%s\"%s" % (opener, label.strip(), closer))
                statement = re.sub(pattern, "\x00", statement)
            statement = re.sub(r":::[A-Za-z0-9_-]+", "", statement)
            segments = [s.strip() for s in ARROW.split(statement)]
            is_edge = len(segments) > 1
            for segment in segments:
                for item in segment.split("&"):
                    item = item.strip()
                    if not item:
                        add("error", "mermaid-parse", number, "edge with a missing end")
                        continue
                    has_label = item.endswith("\x00")
                    node = item.rstrip("\x00").strip()
                    if not NODE_ID.match(node):
                        add("error", "mermaid-parse", number,
                            "cannot read node id %r; use letters, digits and underscores" % node)
                        continue
                    first_seen.setdefault(node, number)
                    if has_label:
                        defined.setdefault(node, number)
                    if is_edge:
                        used.add(node)
    for node, number in sorted(first_seen.items(), key=lambda kv: kv[1]):
        if node in used and node not in defined:
            add("error", "mermaid-undefined", number, "node %s is used in an edge but never given a label" % node)
        if node in defined and node not in used:
            add("error", "mermaid-unused", number, "node %s is defined but appears in no edge" % node)
    return findings


def check_sections(text, kind, template, add):
    spec = KINDS[kind]
    rows = [(n, l) for n, l, region in split_regions(text) if region == "prose"]
    header = spec.get("header")
    if header:
        nonblank = [(n, l) for n, l in rows if l.strip()]
        first = nonblank[0] if nonblank else (0, "")
        if record_format.parse_marker(first[1]) or first[1].lstrip().startswith("<!-- ghim:event"):
            first = nonblank[1] if len(nonblank) > 1 else (0, "")
        expected = r"^### %s(?: \(reconstructed\))?: " % header
        if not re.match(expected, first[1]):
            add("error", "header", first[0], "first line must start with '### %s: <date time zone> - <title>'" % header)
        elif not (template and "{{" in first[1]) and not re.match(expected + STAMP, first[1]):
            add("error", "timestamp", first[0],
                "header needs date, time and zone, for example '### %s: 2026-03-04 14:10 PST - title'" % header)

    positions, cursor = [], 0
    for marker in spec["sections"]:
        found = None
        for index in range(cursor, len(rows)):
            line = rows[index][1].strip()
            if line == marker or line.startswith(marker + " ") or (marker.startswith("**") and line.startswith(marker)):
                found = index
                break
        if found is None:
            elsewhere = any(l.strip().startswith(marker) for _, l in rows)
            add("error", "section-order" if elsewhere else "section-missing", 0,
                "section '%s' is %s" % (marker, "out of order" if elsewhere else "missing"))
            continue
        positions.append((marker, found))
        cursor = found + 1

    if template:
        return
    everything = list(split_regions(text))
    for order, (marker, index) in enumerate(positions):
        start = rows[index][0]
        stop = rows[positions[order + 1][1]][0] if order + 1 < len(positions) else len(everything) + 1
        own = rows[index][1].strip()[len(marker):].strip()
        filled = bool(own)
        for number, line, region in everything[start:stop - 1]:
            if marker.startswith("## ") and region == "prose" and re.match(r"^#{1,2} ", line):
                break
            if line.strip() and region != "fence":
                filled = True
                break
        if not filled:
            add("error", "section-empty", start,
                "section '%s' is empty; write 'None' and why rather than leaving it blank" % marker)


def check_final_record(text, template, labels, profiles, add):
    """Deliverable text only: a title line, the profile's sections, Sources last, and no history."""
    rows = [(n, l) for n, l, region in split_regions(text) if region == "prose"]
    first = next(((n, l) for n, l in rows if l.strip()), (0, ""))
    if not re.match(r"^# \S", first[1]):
        add("error", "title-line", first[0], "a final record starts with a title line: '# <topic as the deliverable names it>'")

    sections = recortex.body_sections(text)
    source = recortex.SOURCES_HEADING
    sources_at = next((n for n, title in sections if title.lower() == source.lower()), None)
    for number, line in rows:
        line = strip_inline_code(line)
        in_sources = sources_at is not None and number >= sources_at
        if AS_OF.match(line):
            add("error", "as-of", number,
                "no 'As of' line: the body is the current text only; dates belong in the comments")
        if re.search(r"<details\b", line, re.I):
            add("error", "details-block", number,
                "no <details> block: replaced text is not kept in the body; GitHub's edit history and the "
                "dated comment that caused the change are the trail")
        if not in_sources and re.search(STAMP, line):
            add("error", "dated-text", number,
                "a date and time in the text: the body holds the current text only; the time of a change "
                "belongs in the comment that records it")
        heading = re.match(r"^#{1,6} +(.*?)\s*#*\s*$", line)
        if heading:
            title = heading.group(1).strip().rstrip(":").strip()
            if HISTORY_HEADING.match(title):
                add("error", "history-heading", number,
                    "heading '%s' holds history or planning: decisions go in decision comments, open work and "
                    "next steps in active issues, provenance in comments or Sources" % title)
            if DATE.search(title) and not in_sources:
                add("error", "dated-heading", number,
                    "dated heading '%s': the body holds the current text only, without dates" % title)

    # A line that looks like a section heading but is not one would make its section, and its label, disappear.
    try:
        names = {heading.lower() for _, heading in recortex.section_labels(profiles)}
    except ValueError:
        names = set()  # reported below
    names.add(source.lower())
    for number, line, region in split_regions(text):
        if number == first[0] or region == "fence" or (region == "prose" and re.match(r"^## +\S", line)):
            continue
        if region == "code" and not line.lstrip().startswith("##"):
            continue
        candidate = re.sub(r"[\s#:]*$", "", re.sub(r"^\s*#*\s*", "", line))
        if candidate and candidate.lower() in names:
            where = "inside a code block; close the block before it" if region == "code" else \
                "write it as '## %s' at the start of a line, or reword the line" % candidate
            add("error", "near-heading", number,
                "'%s' looks like the section heading '## %s' but is not one, so the section and its label would "
                "be lost; %s" % (line.strip(), candidate, where))

    names = [title for _, title in sections]
    if not [title for title in names if title.lower() != source.lower()]:
        add("error", "no-sections", 0, "no '## ' section before '## %s'; write the text for at least one "
            "section of the deliverable" % source)
    if names and names[-1].lower() != source.lower() and any(t.lower() == source.lower() for t in names):
        add("error", "sources-last", 0, "'## %s' must be the last section" % source)
    if template:
        return

    everything = text.split("\n")
    for index, (number, title) in enumerate(sections):
        stop = sections[index + 1][0] if index + 1 < len(sections) else len(everything) + 1
        if title.lower() == source.lower():
            continue  # checked with the required sections
        if not any(line.strip() for line in everything[number:stop - 1]):
            add("error", "section-empty", number, "section '## %s' is empty; a section without text is "
                "removed, together with its label" % title)

    if not profiles:
        return
    try:
        wanted, problems = recortex.labels_from_headings(text, profiles, intended=labels)
    except ValueError as error:
        add("error", "profile", 0, str(error))
        return
    for number, message in problems:
        add("error", "section-heading", number, message)
    if labels is None or problems:
        return
    headings = dict(recortex.section_labels(profiles))
    for label in wanted:
        if label not in labels:
            add("error", "section-labels", 0,
                "the body has the section for %s, but the issue's labels do not include it" % label)
    for label in labels:
        if label in headings and label not in wanted:
            add("error", "section-labels", 0,
                "the issue is labeled %s, but the body has no '## %s' section; remove the label or add "
                "the section" % (label, headings[label]))


def check_event_marker(text, kind, template, add):
    """An event comment starts with `<!-- ghim:event type=<kind> at=<ISO time> -->`, then its heading; both agree."""
    first, second = record_format.first_lines(text)
    marker = record_format.parse_marker(first)
    if marker is None:
        add("error", "event-marker", 1, "an event comment starts with the line "
            "'<!-- ghim:event type=%s at=<YYYY-MM-DDTHH:MM+HH:MM> -->', then its heading" % kind)
        return
    if marker.get("type") != kind:
        add("error", "event-marker", 1, "the marker says type=%s, but this is a %s comment" % (marker.get("type"), kind))
    at = marker.get("at", "")
    if template and "{{" in at:
        return
    if not re.fullmatch(record_format.ISO, at):
        add("error", "event-marker", 1, "at=%s is not an ISO 8601 time with its offset, for example "
            "at=2026-03-04T14:10-08:00" % (at or "(missing)"))
        return
    stamp = re.search(STAMP, second)
    if stamp and stamp.group(0)[:16] != at[:16].replace("T", " "):
        add("error", "event-marker", 1, "the marker time %s and the heading time %s disagree; write the same local "
            "date and time in both" % (at, stamp.group(0)))
    reconstructed = "(reconstructed):" in second
    if reconstructed:
        if not re.fullmatch(record_format.ISO, marker.get("recorded", "")):
            add("error", "event-reconstructed", 1, "a reconstructed event's marker gives recorded=<ISO time it was "
                "recorded>")
        if not re.fullmatch(r"(commit|pr|issue|file|transcript):[\w./#:@+-]{1,200}", marker.get("source", "")):
            add("error", "event-reconstructed", 1, "a reconstructed event's marker gives its dated source: "
                "source=commit:<hash>, pr:<n>, issue:<n>, file:<path> or transcript:<path>")
    elif "recorded" in marker or "source" in marker:
        add("error", "event-reconstructed", 1, "recorded= and source= belong to a reconstructed event; write its "
            "heading as '### %s (reconstructed): <original time> - <title>'" % kind.capitalize())


ARTICLE = {"result": "a result", "decision": "a decision", "failure": "a failure", "correction": "a correction",
           "idea": "an idea", "question": "a question", "checkpoint": "a checkpoint", "triage": "a triage comment",
           "plan": "a planned part", "text": "a change of the text", "proposal": "a proposal",
           "steps": "a task's steps"}
EVIDENCE_KINDS = record_format.EVIDENCE_TYPES
STEM_REPEATS = {
    "result": ("that", "we found"),
    "decision": ("to", "we decided"),
    "idea": ("to", "we had"),
    "failure": ("what went wrong",),
    "correction": ("we corrected",),
    "checkpoint": ("we paused",),
    "question": ("we asked",),
    "plan": ("to", "we plan"),
    "text": ("that", "we changed"),
    "steps": ("to", "we agreed"),
}
QUESTION_WORDS = ("whether", "why", "how", "what", "which", "who", "when", "where", "if")
PLAIN_RANGE = (20, 200)
NOT_CHECKED = ("links", "comment addresses", "evidence", "explanations", "plan topic", "marks")


def is_reconstructed(text):
    first, second = record_format.first_lines(text)
    marker = record_format.parse_marker(first) or {}
    return "(reconstructed):" in second or "recorded" in marker


def listed(items):
    items = list(items)
    return items[0] if len(items) == 1 else "%s and %s" % (", ".join(items[:-1]), items[-1])


def starts_with_words(text, phrase):
    words = phrase.split()
    return re.match(r"%s(?![A-Za-z0-9])" % "[ \t]+".join(re.escape(w) for w in words), text, re.I) is not None


def check_marker_links(text, kind, add):
    """Marker keys, link values, and who may carry which link (record_format.LINK_RULES)."""
    first, second = record_format.first_lines(text)
    match = record_format.MARKER.match(first.strip())
    if not match:
        return
    pairs = [part.split("=", 1) for part in match.group(1).split()]
    names = [key for key, _ in pairs]
    for key in sorted({key for key in names if key not in record_format.MARKER_KEYS}):
        add("error", "marker-key", 1, "the marker key %s= is not one of %s; a misspelled key would be lost silently"
            % (key, ", ".join(k + "=" for k in record_format.MARKER_KEYS)))
    for key in sorted({key for key in names if names.count(key) > 1}):
        add("error", "marker-key", 1, "the marker key %s= appears more than once; write each key once" % key)
    for key, value in pairs:
        if key == "eventkey" and not re.fullmatch(r"[0-9a-f]{32}", value):
            add("error", "event-key", 1, "eventkey= is exactly 32 lowercase hexadecimal characters")
    under = {}
    for key, value in pairs:
        if key not in record_format.LINK_KINDS:
            continue
        if not record_format.LINK_VALUE.fullmatch(value):
            add("error", "link-value", 1, "%s=%s is not one to four comment ids separated by commas, for example "
                "%s=2900000124 or %s=2900000124,2900000125" % (key, value[:60], key, key))
            continue
        for ident in value.split(","):
            under.setdefault(ident, []).append(key)
    for ident, keys in sorted(under.items()):
        if len(keys) > 1:
            add("error", "link-value", 1, "comment %s stands %s; an id stands under one key, once"
                % (ident, "twice under %s=" % keys[0] if len(set(keys)) == 1 else "under %s"
                   % listed(k + "=" for k in keys)))
    carried = [key for key in record_format.LINK_KINDS if key in names]
    if not carried:
        return
    if kind == "checkpoint":
        add("error", "link-kind", 1, "a checkpoint carries no link (%s): a pause is a moment, not a step"
            % listed(k + "=" for k in carried))
    elif kind not in record_format.LINKABLE:
        add("error", "link-kind", 1, "%s carries no link (%s); links belong to memories"
            % (ARTICLE.get(kind, "this kind of comment"), listed(k + "=" for k in carried)))
    elif is_reconstructed(text):
        add("error", "link-kind", 1, "a reconstructed memory carries no link (%s) in this version; links for "
            "reconstructed history wait for the later owner-approved pass" % listed(k + "=" for k in carried))
    else:
        for key in carried:
            if kind not in record_format.LINK_RULES[key]:
                carriers = [ARTICLE[k] for k in record_format.LINKABLE if k in record_format.LINK_RULES[key]]
                add("error", "link-kind", 1, "%s does not carry %s=; it is carried by %s"
                    % (ARTICLE[kind], key, listed(carriers).replace(" and ", " or ")))


def check_plain(text, kind, template, add):
    """The plain line: the first field, one line that completes its label, in words a reader away for a month can
    follow, with every number also standing in a field."""
    label = record_format.PLAIN_LABELS[kind]
    lines = record_format.body_lines(text)
    scanned = record_format.scan_fields(text)
    position = next((index for index, field in enumerate(scanned) if field["label"] == label), None)
    if position is None:
        add("error", "plain", 0, "the first field is the plain line, '**%s:** <one line>', directly under the "
            "heading" % label)
        return
    field = scanned[position]
    number, value = field["line"] + 1, field["first"]
    if template and "{{" in lines[field["line"]]:
        return
    if position:
        add("error", "plain", number, "the plain line is the first field, directly under the heading")
    if sum(1 for other in scanned if other["label"] == label) > 1:
        add("error", "plain", number, "a memory has one plain line; '**%s:**' stands more than once" % label)
    if not value:
        add("error", "plain", number, "the plain line is empty; it completes '%s'" % label)
        return
    following = next((index for index in range(field["line"] + 1, len(lines)) if lines[index].strip()), None)
    sections = KINDS[kind]["sections"]
    if field["line"] + 1 < len(lines) and lines[field["line"] + 1].strip() or following is not None and \
            len(sections) > 1 and not lines[following].strip().startswith(sections[1]):
        add("error", "plain", number, "the plain line is one line; a blank line and then '%s' follow it"
            % (sections[1] if len(sections) > 1 else "the next field"))
    if not PLAIN_RANGE[0] <= len(value) <= PLAIN_RANGE[1]:
        add("error", "plain", number, "the plain line is %d characters; write %d to %d" % ((len(value),) + PLAIN_RANGE))
    if value.endswith("."):
        add("error", "plain", number, "the plain line ends without a period")
    for what in record_format.line_problems(value):
        add("error", "plain", number, "the plain line holds %s; it is plain words only: cite issues, paths and "
            "commits in the fields below" % what)
    stem = label[len("In plain words, "):]
    for phrase in STEM_REPEATS[kind] + ("in plain words",):
        if starts_with_words(value, phrase):
            add("error", "plain", number, "the plain line repeats its label: '%s:' already says '%s'" % (label, stem))
            break
    if kind == "question" and not any(starts_with_words(value, word) for word in QUESTION_WORDS):
        add("error", "plain", number, "the plain line of a question starts with %s"
            % listed(QUESTION_WORDS).replace(" and ", " or "))
    others = set()
    for index, other in enumerate(scanned):
        if index != position:
            others |= record_format.field_numbers(other["text"])
    missing = []
    for written, token, power in record_format.plain_numbers(value):
        if written not in missing and not record_format.number_stands(token, power, others):
            missing.append(written)
    if missing:
        add("error", "plain", number, "the number%s %s in the plain line stand%s in no field of this memory, neither "
            "as written nor rounded; put each number in a field, with its source, or leave it out of the plain line"
            % ("s" if len(missing) > 1 else "", listed(missing), "" if len(missing) > 1 else "s"))


def check_quoted_fields(text, add):
    """A field label right after a quote line, with no blank line between, is part of the quote on GitHub."""
    lines = record_format.body_lines(text)
    code = record_format.code_lines(lines)
    for index in sorted(record_format.quote_lines(lines, code)):
        match = record_format.FIELD.match(lines[index])
        if match:
            add("error", "field-quote", index + 1, "'**%s:**' follows a quote line with no blank line between, so "
                "GitHub shows it inside the quote; leave a blank line before it" % match.group(1))


# ---------------------------------------------------------------- planned parts, changes of the text, proposals

FIELD_CAP = 800            # characters of a planned part's or a text change's free field
TOPIC_FIELD = re.compile(r"#[1-9][0-9]{0,8}|None yet")
DISPLAY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}")
SHA_FIELD = re.compile(r"sha256 `[0-9a-f]{64}`")
PULL_FIELD = re.compile(r"#[1-9][0-9]{0,8}")
NOT_KEPT_FIELD = re.compile(r"Not kept: (.+)")


def field_map(text):
    """{label: field} of the first field of each label, and the labels that stand more than once."""
    found, repeated = {}, set()
    for field in record_format.scan_fields(text):
        if field["label"] in found:
            repeated.add(field["label"])
        else:
            found[field["label"]] = field
    return found, repeated


def need_text(found, label, add, cap=FIELD_CAP, one_line=False):
    """The text of a required field, or None after an error when it is empty, too long or (one_line) not one line."""
    field = found.get(label)
    if field is None:
        return None  # check_sections reports a missing field
    value = field["text"].strip()
    if not value:
        add("error", "field-empty", field["line"] + 1, "'**%s:**' is empty; say what it holds" % label)
        return None
    if len(value) > cap:
        add("error", "field-length", field["line"] + 1, "'**%s:**' is %d characters; at most %d" % (label, len(value), cap))
    if one_line and "\n" in value:
        add("error", "field-form", field["line"] + 1, "'**%s:**' is one line" % label)
    return value


def plan_of(profiles):
    """The plan of the project's profiles, else the manuscript's when the project names none (a draft linted alone)."""
    return recortex.plan_profile(profiles) or (None if profiles else recortex.plan_profile(["manuscript"]))


def check_plan(text, template, profiles, add):
    """A planned part: its part and section belong to the profile's plan, its topic is #N or None yet, a display part
    carries the id of its block, and a revision says what it does to the part it replaces."""
    found, repeated = field_map(text)
    for label in sorted(repeated):
        add("error", "field-repeat", found[label]["line"] + 1, "'**%s:**' stands more than once" % label)
    plan = plan_of(profiles)
    first, _ = record_format.first_lines(text)
    marker = record_format.parse_marker(first) or {}
    if template:
        return
    if plan is None:
        add("error", "plan-profile", 0, "none of this project's profiles has a plan (a profile's \"plan\" key names its "
            "parts); a planned part is written for a project that writes a paper")
        return
    part = need_text(found, "Part", add, 40, True)
    if part is not None and part not in plan["parts"]:
        add("error", "plan-part", found["Part"]["line"] + 1, "the part is one of %s, not '%s'" % (
            ", ".join(plan["parts"]), part[:40]))
        part = None
    section = need_text(found, "Section", add, 60, True)
    if section is not None and part is not None:
        if part == "message" and section != "All":
            add("error", "plan-section", found["Section"]["line"] + 1, "the message belongs to the whole paper: "
                "'**Section:** All'")
        elif part != "message" and section not in plan["sections"]:
            add("error", "plan-section", found["Section"]["line"] + 1, "the section is one of %s, not '%s'" % (
                ", ".join(plan["sections"]), section[:60]))
    topic = need_text(found, "Topic", add, 20, True)
    if topic is not None and not TOPIC_FIELD.fullmatch(topic):
        add("error", "plan-topic", found["Topic"]["line"] + 1, "the topic is '#N' (the issue that holds the text of "
            "this part) or 'None yet'")
    ident = found.get("Id")
    if part is not None:
        if part in plan["display"] and ident is None:
            add("error", "plan-id", 0, "a %s carries the id its block will have: '**Id:** <id>' after the topic" % part)
        elif part not in plan["display"] and ident is not None:
            add("error", "plan-id", ident["line"] + 1, "only a %s carries an id" % listed(plan["display"]).replace(
                " and ", " or "))
    if ident is not None and not DISPLAY_ID.fullmatch(ident["text"].strip()):
        add("error", "plan-id", ident["line"] + 1, "an id is letters, digits, '.', '_' or '-', starting with a letter "
            "or digit, at most 60 characters")
    if ident is not None and topic is not None and part in (plan["display"] if part else ()):
        order = [field["label"] for field in record_format.scan_fields(text)]
        if order.index("Id") != order.index("Topic") + 1:
            add("error", "plan-id", ident["line"] + 1, "the id follows the topic directly")
    for label in ("Statement", "It needs", "We have", "If it does not hold"):
        need_text(found, label, add)
    need_text(found, "Next step", add, 300)
    change = found.get("Change")
    replaces = "replaces" in marker
    if change is not None and not replaces:
        add("error", "plan-change", change["line"] + 1, "'**Change:**' belongs to a revision: the marker carries "
            "replaces=<the part it revises>")
    elif replaces and change is None:
        add("error", "plan-change", 1, "a revision says what it does to the part it replaces: '**Change:** %s'"
            % "', '".join(record_format.PLAN_CHANGES))
    elif change is not None and change["text"].strip() not in record_format.PLAN_CHANGES:
        add("error", "plan-change", change["line"] + 1, "the change is exactly one of %s" % ", ".join(
            record_format.PLAN_CHANGES))
    elif change is not None:
        order = [field["label"] for field in record_format.scan_fields(text)]
        if order.index("Change") != order.index("If it does not hold") + 1:
            add("error", "plan-change", change["line"] + 1, "the change follows 'If it does not hold' directly")


def check_text(text, template, profiles, add):
    """A change of the text: its sections, how it was approved, what the body was and became, which version holds it."""
    found, repeated = field_map(text)
    for label in sorted(repeated):
        add("error", "field-repeat", found[label]["line"] + 1, "'**%s:**' stands more than once" % label)
    if template:
        return
    headings = [heading for _, heading in recortex.section_labels(profiles)] if profiles else []
    section = need_text(found, "Section", add, 200, True)
    if section is not None and headings:
        for name in [part.strip() for part in section.split(",")]:
            if name not in headings:
                add("error", "text-section", found["Section"]["line"] + 1, "'%s' is not a section heading of this "
                    "project's profiles (%s)" % (name[:60], ", ".join(headings)))
    need_text(found, "What changed", add)
    need_text(found, "Why", add)
    approved = found.get("Approved")
    if approved is not None:
        form = record_format.approved_form(approved["text"])
        if form is None:
            add("error", "text-approved", approved["line"] + 1, "'**Approved:**' starts with one of six forms: 'Written "
                "without asking, under the autonomous mode.'; 'By <name or role> in the chat, <date time zone>:' and a "
                "quote; 'By <login> on GitHub, by merging pull request <n>.'; 'Edited by <login> on GitHub, <date time "
                "zone>.'; 'Restored by <name or role> in the chat, <date time zone>:' and a quote; or 'Kept as a file later, for the "
                "text change of comment <id>.'")
    kept = need_text(found, "Kept from the author", add, 200, True)
    if kept is not None and not record_format.KEPT_FROM_AUTHOR.fullmatch(kept):
        add("error", "text-kept", found["Kept from the author"]["line"] + 1, "'**Kept from the author:**' is '<n> "
            "paragraphs were changed by hand and were left as they are.' or 'Nothing was changed by hand.'")
    body = need_text(found, "Body", add, 200, True)
    if body is not None and record_format.sha_pair(body) is None:
        add("error", "text-body", found["Body"]["line"] + 1, "'**Body:**' is 'before sha256 `<64 hex>`; after sha256 "
            "`<64 hex>`', with `none` for the before of a first text")
    version = need_text(found, "Version", add, 200, True)
    if version is not None:
        commit = re.fullmatch(r"`([0-9a-f]{40})`", version)
        reason = NOT_KEPT_FIELD.fullmatch(version)
        if not commit and not (reason and reason.group(1).rstrip(".") in record_format.NOT_KEPT):
            add("error", "text-version", found["Version"]["line"] + 1, "'**Version:**' is a 40-character commit id in "
                "backticks, or 'Not kept: ' and one of: %s" % "; ".join(record_format.NOT_KEPT))


def check_proposal(text, template, profiles, add):
    """A proposal of a change of the text, the anchor of an approval on GitHub."""
    found, repeated = field_map(text)
    for label in sorted(repeated):
        add("error", "field-repeat", found[label]["line"] + 1, "'**%s:**' stands more than once" % label)
    if template:
        return
    headings = [heading for _, heading in recortex.section_labels(profiles)] if profiles else []
    section = need_text(found, "Section", add, 60, True)
    if section is not None and headings and section not in headings:
        add("error", "text-section", found["Section"]["line"] + 1, "'%s' is not a section heading of this project's "
            "profiles (%s)" % (section[:60], ", ".join(headings)))
    need_text(found, "What would change", add)
    need_text(found, "Why", add)
    pull = need_text(found, "Pull request", add, 20, True)
    if pull is not None and not PULL_FIELD.fullmatch(pull):
        add("error", "proposal-pull", found["Pull request"]["line"] + 1, "'**Pull request:**' is '#<number>'")
    for label in ("Base", "Text"):
        value = need_text(found, label, add, 100, True)
        if value is not None and not SHA_FIELD.fullmatch(value):
            add("error", "proposal-hash", found[label]["line"] + 1, "'**%s:**' is 'sha256 `<64 hex>`'" % label)


def check_marks(text, kind, add):
    """The mark keys of the marker (record_format.MARK_KEYS): on which kinds, in what form. What a mark points at is
    checked with the record (check_record)."""
    first, _ = record_format.first_lines(text)
    match = record_format.MARKER.match(first.strip())
    if not match:
        return
    pairs = [part.split("=", 1) for part in match.group(1).split()]
    carried = [(key, value) for key, value in pairs if key in record_format.MARK_KEYS]
    if not carried:
        return
    for key, value in carried:
        if kind not in record_format.MARK_RULES[key]:
            carriers = [ARTICLE[k] for k in sorted(record_format.MARK_RULES[key])]
            add("error", "mark-kind", 1, "%s does not carry %s=; it is carried by %s"
                % (ARTICLE.get(kind, "this kind of comment"), key, listed(carriers).replace(" and ", " or ")))
        elif is_reconstructed(text):
            add("error", "mark-kind", 1, "a reconstructed memory carries no mark (%s=) in this version" % key)
        if not record_format.MARK_VALUE.fullmatch(value):
            add("error", "mark-value", 1, "%s=%s is not one to four groups '<steps comment id>:<n>[,<n>...]' joined by "
                "';', with step numbers from 1 to 20, for example %s=2900000124:2,3" % (key, value[:60], key))
            continue
        seen = set()
        for group in value.split(";"):
            cid, numbers = group.split(":")
            listed_here = numbers.split(",")
            if len(set(listed_here)) != len(listed_here) or any((cid, n) in seen for n in listed_here):
                add("error", "mark-value", 1, "%s=%s names the same step twice; name each step once" % (key, value[:60]))
                break
            seen.update((cid, n) for n in listed_here)


def check_steps(text, template, add):
    """The steps of a task: how the user agreed to them, the numbered one-line steps, the check that ends the task."""
    found, repeated = field_map(text)
    for label in sorted(repeated):
        add("error", "field-repeat", found[label]["line"] + 1, "'**%s:**' stands more than once" % label)
    if template:
        return
    agreed = found.get("Agreed")
    if agreed is not None:
        if not agreed["text"].strip():
            add("error", "field-empty", agreed["line"] + 1, "'**Agreed:**' is empty; say how the user agreed")
        elif record_format.agreed_form(agreed["text"]) is None:
            add("error", "steps-agreed", agreed["line"] + 1, "'**Agreed:**' starts with 'Approved in the plan dialog, "
                "<date time zone>.' or with 'By <name or role> in the chat, <date time zone>:' and the user's words "
                "quoted on lines that start with >")
    steps = found.get("Steps")
    if steps is not None:
        rows = [(number + 1, line) for number, line in steps["parts"][1:] if line.strip()]
        items = []
        for number, line in rows:
            match = record_format.STEP_LINE.match(line.strip())
            if match is None:
                add("error", "steps-list", number, "a line of '**Steps:**' is '<n>. <one line>'; this one is neither a "
                    "numbered step nor blank")
            else:
                items.append((number, int(match.group(1)), match.group(2).strip()))
        if not items:
            add("error", "steps-list", steps["line"] + 1, "'**Steps:**' lists at least one step, numbered from 1")
        if len(items) > record_format.STEPS_MAX:
            add("error", "steps-list", steps["line"] + 1, "'**Steps:**' lists %d steps; at most %d"
                % (len(items), record_format.STEPS_MAX))
        for position, (number, written, step) in enumerate(items, 1):
            if written != position:
                add("error", "steps-list", number, "the steps are numbered 1, 2, 3, ... in order; this one is %d, "
                    "not %d" % (written, position))
                break
        for number, written, step in items:
            if len(step) > record_format.STEP_TEXT_MAX:
                add("error", "steps-list", number, "a step is one line of at most %d characters"
                    % record_format.STEP_TEXT_MAX)
    need_text(found, "Done when", add, 300, True)


PLAN_ITEM = re.compile(r"^- \[( |x)\] ([0-9]{1,3})\. (\S.*)$")


def check_active(text, add):
    """A task may hold a task list under '## Plan' (tasks.py render writes it): each line that starts with '- [' is
    '- [ ] <n>. <step>' or '- [x] <n>. <step>', numbered from 1 in order."""
    sections = recortex.body_sections(text)
    lines = text.replace("\r\n", "\n").split("\n")
    for position, (number, heading) in enumerate(sections):
        if heading.strip().lower() != "plan":
            continue
        stop = sections[position + 1][0] - 1 if position + 1 < len(sections) else len(lines)
        expected = 1
        for offset in range(number, stop):
            line = lines[offset]
            if not line.startswith("- ["):
                continue
            match = PLAN_ITEM.match(line.rstrip())
            if match is None:
                add("error", "plan-list", offset + 1, "a task-list line under '## Plan' is '- [ ] <n>. <step>' or "
                    "'- [x] <n>. <step>'")
            elif int(match.group(2)) != expected:
                add("error", "plan-list", offset + 1, "the steps under '## Plan' are numbered 1, 2, 3, ... in order; "
                    "this one is %s, not %d" % (match.group(2), expected))
            expected += 1


def check_plan_body(text, template, add):
    """The readable copy of the plan, kept in the body of the plan issue: a title line, the role sentence, the time the
    copy was made, then the sections. It holds no history and no comment text beyond the plan's own lines."""
    rows = [(n, l) for n, l, region in split_regions(text) if region == "prose"]
    first = next(((n, l) for n, l in rows if l.strip()), (0, ""))
    if not re.match(r"^# The plan of the \S", first[1]):
        add("error", "title-line", first[0], "the plan's body starts with '# The plan of the <paper>'")
    if template:
        return
    if "This body is a readable copy of the plan." not in text:
        add("error", "plan-role", 0, "the plan's body says what it is: 'This body is a readable copy of the plan.'")
    if record_format.copy_made(text) is None:
        add("error", "plan-copy", 0, "the plan's body has a line 'Copy made: <date time zone>'")
    for number, line in rows:
        if re.search(r"<details\b", line, re.I):
            add("error", "details-block", number, "no <details> block in the copy of the plan")


def check_plan_record(text, lookup, add, skipped):
    """What only the record can tell about a planned part: its topic is a topic issue, the message and every change
    that widens, reverses or drops an agreed part link a decision."""
    unknown = lookup.UNKNOWN
    first, _ = record_format.first_lines(text)
    marker = record_format.parse_marker(first) or {}
    found, _ = field_map(text)
    topic = found.get("Topic")
    topic_text = topic["text"].strip() if topic else ""
    if re.fullmatch(r"#[1-9][0-9]{0,8}", topic_text):
        target = lookup.issue(int(topic_text[1:]))
        if target is unknown:
            skipped.add("plan topic")
        elif target is None:
            add("error", "plan-topic", topic["line"] + 1, "%s: there is no issue %s in %s" % (topic_text, topic_text,
                                                                                            lookup.repo))
        elif "final-record" not in {label.lower() for label in target["labels"]}:
            add("error", "plan-topic", topic["line"] + 1, "%s is not a topic issue (labeled final-record), so it holds no "
                "text for this part" % topic_text)
    part = (found.get("Part") or {}).get("text", "").strip()
    change = (found.get("Change") or {}).get("text", "").strip()
    pairs = record_format.links(marker)
    decisions = False
    for key, target in pairs:
        if key != "because":
            continue
        memory = lookup.memory(target)
        if memory is unknown:
            skipped.add("links")
        elif memory is not None and memory["type"] == "decision":
            decisions = True
    needs = []
    if part == "message":
        needs.append("the message of the paper is agreed in a decision of the author")
    for key, target in pairs:
        if key == "replaces" and change in ("widens", "reverses"):
            needs.append("a revision that %s a claim is agreed in a decision of the author" % change)
        if key == "instead":
            agreed = lookup.plan_agreed(target)
            if agreed is unknown:
                skipped.add("links")
            elif agreed:
                needs.append("only a decision drops an agreed part, and comment %d is agreed" % target)
    if needs and not decisions:
        add("error", "plan-decision", 1, "%s: link that decision with because=<its comment id>" % needs[0])
    elif needs and any(key == "instead" and lookup.plan_agreed(target) is True for key, target in pairs):
        add("error", "plan-decision", 1, "a planned part never drops an agreed part; a decision of the author does")


def check_citations(text, add):
    """Citations in Manubot's forms in a final record: a key of a form Manubot reads passes (doi:, pmc:, pubmed:,
    arxiv:, isbn:, wikidata:, the spellings pmid:, pmcid: and url: that Manubot's code accepts, an address, with or
    without braces, and a name the body defines). A name that nothing defines, a reference to a figure, table or
    equation the body does not hold and a key of no known form warn. A citation is never checked against the
    literature."""
    defined = record_format.manubot_definitions(text)
    ids = {"fig": {b["id"] for b in record_format.blocks(text) if b["kind"] == "figure"},
           "tbl": {b["id"] for b in record_format.blocks(text) if b["kind"] == "table"},
           "eq": set(re.findall(r"\{#eq:([A-Za-z0-9_.-]+)\}", record_format.masked(text)))}
    for item in record_format.citations_manubot(text):
        key = item["key"]
        prefix, _, rest = key.partition(":")
        braced = re.fullmatch(r"\{(https?://[^\s{}]+)\}", key)
        if record_format.manubot_identifier(prefix, rest) or braced or re.fullmatch(r"https?://[^\s{}]+", key) \
                or re.fullmatch(r"10\.[0-9]{4,9}/[^\s;\[\]]+", key):
            continue  # an unprefixed DOI is one Manubot infers
        if prefix in ids and ":" in key:
            if rest not in ids[prefix]:
                add("warning", "citation-ref", item["line"], "'@%s' refers to a %s that this body does not hold" % (
                    key[:60], {"fig": "figure", "tbl": "table", "eq": "equation"}[prefix]))
        elif re.fullmatch(r"[A-Za-z0-9_-]+", key):
            if key not in defined:
                add("warning", "citation-name", item["line"], "'@%s' is a named reference that nothing in this body "
                    "defines; define it in a paragraph of its own as '[@%s]: doi:...'" % (key[:60], key[:60]))
        else:
            add("warning", "citation-form", item["line"], "'@%s' is not a citation form this check knows (doi:, pmc:, "
                "pubmed:, arxiv:, isbn:, wikidata:, url:, an address, or a named reference)" % key[:60])


NOTHING_TO_SHOW = re.compile(r"(?:- )?(?:none|n/a|nothing)\b.*", re.I)


def check_evidence(text, kind, template, add):
    """The optional Evidence field: last, at most six lines, each one of the four forms."""
    scanned = record_format.scan_fields(text)
    found = [index for index, field in enumerate(scanned) if field["label"] == record_format.EVIDENCE_LABEL]
    if not found:
        return
    field = scanned[found[0]]
    number = field["line"] + 1
    if kind not in EVIDENCE_KINDS:
        add("error", "evidence", number, "only a result, a decision, a failure or a correction lists Evidence")
        return
    if is_reconstructed(text):
        add("error", "evidence", number, "a reconstructed memory lists no Evidence in this version")
        return
    if len(found) > 1:
        add("error", "evidence", scanned[found[1]]["line"] + 1, "a memory has one Evidence field")
    if found[0] != len(scanned) - 1:
        add("error", "evidence", number, "Evidence is the last field, after every field of the %s" % kind)
    if template:
        return
    count = 0
    for index, part in field["parts"]:
        if not part.strip():
            continue
        count += 1
        row, why = record_format.evidence_line(part)
        if row is None and NOTHING_TO_SHOW.fullmatch(part.strip()):
            add("error", "evidence", index + 1, "Evidence lists what there is to show; delete the **Evidence:** field "
                "when there is nothing (\"None\" belongs to the other fields)")
        elif row is None:
            add("error", "evidence", index + 1, "an Evidence line is %s" % why if why.startswith("not ") else why)
    if not count:
        add("error", "evidence", number, "Evidence is empty; delete the field when there is nothing to show")
    if count > record_format.EVIDENCE_MAX_LINES:
        add("error", "evidence", number, "Evidence has %d lines; at most %d" % (count, record_format.EVIDENCE_MAX_LINES))


def word_pattern(name):
    words = record_format.word_key(name).split()
    return re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % "[ \t\n]+".join(re.escape(w) for w in words), re.I) \
        if words else None


def check_folds(text, kind, add):
    """Folds: only on memories, only after the last field, each well formed, within the limits."""
    lines = record_format.body_lines(text)
    code = record_format.code_lines(lines)
    found = record_format.folds(text)
    covered = {index for item in found for index in range(item["first"], item["last"] + 1)}
    for index, line in enumerate(lines):
        if index not in code and index not in covered and record_format.FOLD_TAG.search(strip_inline_code(line)):
            add("error", "fold-format", index + 1, "a <details> or <summary> tag outside a fold; a fold starts with a "
                "line that holds only <details>")
    if not found:
        return
    if kind not in record_format.MEMORY_TYPES:
        add("error", "fold-format", found[0]["first"] + 1, "folds belong to memories; %s holds none"
            % ARTICLE.get(kind, "this kind of comment"))
        return
    for item in found:
        for index, message in item["problems"]:
            add("error", "fold-format", index + 1, message)
    stray = [index for index in range(found[0]["first"], len(lines)) if index not in covered and lines[index].strip()]
    if stray:
        if any(record_format.FIELD.match(lines[index]) and index not in code for index in stray):
            add("error", "fold-format", found[0]["first"] + 1, "a fold stands before a field; folds come after the "
                "last field")
        else:
            add("error", "fold-format", stray[0] + 1, "only folds follow the first fold; move this text into a field")
    explained = [item for item in found if item["explanation"]]
    words = [item for item in explained if item["explanation"]["kind"] == "word"]
    threads = [item for item in explained if item["explanation"]["kind"] == "thread"]
    if len(words) > 3:
        add("error", "fold-format", words[3]["first"] + 1, "at most three words are explained in one memory")
    if len(threads) > 1:
        add("error", "fold-format", threads[1]["first"] + 1, "a memory holds at most one fold about its thread")
    keys = {}
    for item in words:
        word = item["explanation"]["word"]
        key = record_format.word_key(word)
        if key in keys:
            add("error", "fold-format", item["first"] + 1, "\"%s\" is explained twice in this memory" % word)
        keys.setdefault(key, item)
        outside = "\n".join(line for index, line in enumerate(lines) if not item["first"] <= index <= item["last"])
        names = [word] + item["explanation"]["also"]
        if not any(word_pattern(name) and word_pattern(name).search(outside) for name in names):
            add("error", "fold-format", item["first"] + 1, "\"%s\" is not used in this memory outside its own fold; "
                "explain a word where the memory uses it" % word)


def needs_record(text, kind):
    """What a draft holds that only the record can check: the subset of NOT_CHECKED."""
    first, _ = record_format.first_lines(text)
    found = set()
    if record_format.links(record_format.parse_marker(first)):
        found.add("links")
    if any(item["kind"] == "memory" or item["kind"] == "elsewhere" and item["comment"] is None and
           "#issuecomment-" in text[item["start"]:item["end"]]
           for item in record_format.citations(text, None)):
        found.add("comment addresses")
    if any(row["kind"] in ("table", "figure") or row["kind"] == "pull" and not row["repo"]
           for row in record_format.evidence(text)):
        found.add("evidence")
    if record_format.explanations(text):
        found.add("explanations")
    if kind == "plan" and re.search(r"^\*\*Topic:\*\* *#[1-9]", text, re.M):
        found.add("plan topic")
    if kind in {k for kinds in record_format.MARK_RULES.values() for k in kinds} and record_format.marks(
            record_format.parse_marker(first)):
        found.add("marks")
    return found


LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")


def shown_login(login):
    return login if LOGIN.fullmatch(login or "") else "an account whose login is not shown"


def check_record(text, kind, lookup, add):
    """What only the record can tell, asked through `lookup` (lookup.py). Returns the names of NOT_CHECKED that the
    lookup could not answer."""
    unknown, skipped = lookup.UNKNOWN, set()
    repo, number = lookup.repo, lookup.number
    sync = "" if lookup.online else " (run index.py sync if it was posted since the last sync)"
    first, second = record_format.first_lines(text)
    marker = record_format.parse_marker(first) or {}
    at = marker.get("at", "")
    draft_time = record_format.sort_key(at) if re.fullmatch(record_format.ISO, at) else None
    linkable = kind in record_format.LINKABLE and not is_reconstructed(text)
    for key, target in record_format.links(marker) if linkable else []:
        found = lookup.memory(target)
        if found is unknown:
            skipped.add("links")
        elif found is None:
            add("error", "link-target", 1, "%s=%d: there is no comment %d in %s%s; take ids from index.py query recall "
                "or from the memory id: line of post_verified.py" % (key, target, target, repo, sync))
        elif found["type"] not in record_format.MEMORY_TYPES:
            add("error", "link-target", 1, "%s=%d: comment %d is not a memory (it is a comment of type %s); a link "
                "points at a result, decision, failure, correction, idea or question" % (key, target, target,
                                                                                         found["type"]))
        else:
            if draft_time and found["time"] > draft_time:
                add("error", "link-target", 1, "%s=%d: memory %d is later (%s) than this one (%s); a link points back in "
                    "time" % (key, target, target, found["at"][:40], at))
            if not record_format.link_allowed(kind, key, found["type"]):
                allowed = sorted(record_format.LINK_RULES[key].get(kind, ()))
                add("error", "link-target", 1, "%s=%d points at %s; %s %s= points at %s" % (
                    key, target, ARTICLE.get(found["type"], found["type"]), ARTICLE[kind], key,
                    listed(ARTICLE[k] for k in allowed).replace(" and ", " or ") if allowed else "nothing"))
            if not lookup.counts(found["author"]):
                add("error", "link-target", 1, "%s=%d: memory %d was written by %s, an account outside your allowed list, "
                    "so the link would lead nowhere; if that account belongs to the project, add its login to the line "
                    "of %s in your trusted list" % (key, target, target, shown_login(found["author"]), repo))
            elif lookup.trusted and found.get("edited"):
                add("error", "link-target", 1, "%s=%d: memory %d was edited after it was posted, so it is not told and "
                    "the link would lead nowhere; link to the memory that corrects it, or leave the link out"
                    % (key, target, target))
    for key, target, step in record_format.marks(marker) if not is_reconstructed(text) else []:
        if kind not in record_format.MARK_RULES[key]:
            continue  # check_marks says so
        found = lookup.steps_memory(target)
        if found is unknown:
            skipped.add("marks")
        elif found is None:
            add("error", "mark-target", 1, "%s=%d:%d: there is no comment %d in %s%s; take the id from tasks.py status "
                "or from the memory id: line of post_verified.py" % (key, target, step, target, repo, sync))
        elif found["type"] != "steps":
            add("error", "mark-target", 1, "%s=%d:%d: comment %d is not a steps memory (it is a comment of type %s)"
                % (key, target, step, target, found["type"]))
        elif not found["active"]:
            add("error", "mark-target", 1, "%s=%d:%d: the steps memory %d is on #%d, which is not an active task"
                % (key, target, step, target, found["issue"]))
        elif step > found["count"]:
            add("error", "mark-target", 1, "%s=%d:%d: the steps memory %d lists %d step%s, not %d"
                % (key, target, step, target, found["count"], "" if found["count"] == 1 else "s", step))
        elif not lookup.counts(found["author"]):
            add("error", "mark-target", 1, "%s=%d:%d: the steps memory %d was written by %s, an account outside your "
                "allowed list, so the mark would count for nothing" % (key, target, step, target,
                                                                       shown_login(found["author"])))
        elif lookup.trusted and found.get("edited"):
            add("error", "mark-target", 1, "%s=%d:%d: the steps memory %d was edited after it was posted, so it is not "
                "told and the mark would count for nothing" % (key, target, step, target))
    if kind == "plan" and not is_reconstructed(text):
        check_plan_record(text, lookup, add, skipped)
    for item in record_format.citations(text, repo, local=record_store.is_local(repo)):
        if item["kind"] != "memory":
            continue
        found = lookup.memory(item["comment"])
        if found is unknown:
            skipped.add("comment addresses")
        elif found is None or found["issue"] != item["issue"]:
            add("error", "citation", 0, "the address of comment %d on #%d: there is no such comment on that issue in "
                "%s%s" % (item["comment"], item["issue"], repo, sync))
    rows = record_format.evidence(text) if kind in EVIDENCE_KINDS and not is_reconstructed(text) else []
    for row in rows:
        name = "%s %s" % (row["kind"], row["ident"])
        if row["kind"] in ("table", "figure"):
            topics = lookup.topic_issue(number) if number is not None else unknown
            bodies = unknown if topics is unknown else [lookup.issue(n) for n in topics]
            if bodies is unknown or unknown in bodies:
                skipped.add("evidence")
            elif not topics:
                add("error", "evidence", 0, "%s: #%d shares no topic: label with an issue labeled final-record, so "
                    "there is no topic body to hold it" % (name, number))
            elif not any(body and row["ident"] in body["blocks"].get(row["kind"], ()) for body in bodies):
                add("error", "evidence", 0, "%s: the topic body (%s) has no '### %s: %s' block" % (
                    name, ", ".join("#%d" % n for n in topics), row["kind"].capitalize(), row["ident"]))
        elif row["kind"] == "pull" and not row["repo"]:
            found = lookup.issue(int(row["ident"]))
            if found is unknown:
                skipped.add("evidence")
            elif found is None:
                add("error", "evidence", 0, "%s: there is no pull request %s in %s%s" % (name, row["ident"], repo, sync))
            elif not found["pull"]:
                add("error", "evidence", 0, "%s: #%s is an issue, not a pull request" % (name, row["ident"]))
    if kind not in record_format.MEMORY_TYPES:
        return skipped
    explanations = record_format.explanations(text)
    threads = [item for item in explanations if item["kind"] == "thread"]
    if threads and number is None:
        skipped.add("explanations")
    elif threads:
        target = lookup.issue(number)
        topics = [] if target in (None, unknown) else [l for l in target["labels"] if l.lower().startswith("topic:")]
        if target is unknown:
            skipped.add("explanations")
        elif target is None:
            add("error", "thread-issue", 0, "there is no issue #%d in %s%s, so a fold cannot say what its thread is about"
                % (number, repo, sync))
        elif len(topics) > 1:
            add("error", "thread-issue", 0, "#%d carries %d topic: labels, so a fold cannot say which thread it opens; "
                "write what a thread is about in a memory on an issue with one topic: label" % (number, len(topics)))
        opening = lookup.opening(number)
        if opening is unknown:
            skipped.add("explanations")
        elif not threads[0]["revised"] and opening:
            add("error", "thread-known", 0, "memory %s (%s) already says what this thread is about; write 'This thread "
                "is now about' only if that changed, else leave the fold out" % (opening["comment"], opening["date"]))
        elif threads[0]["revised"] and not opening:
            add("error", "thread-new", 0, "the local index holds no memory that says what this thread is about; if one "
                "was posted since the last sync, run index.py sync, else write 'This thread is about', not 'is now "
                "about'")
    for item in explanations:
        if item["kind"] != "word":
            continue
        found = lookup.word(record_format.word_key(item["word"]))
        if found is unknown:
            skipped.add("explanations")
        elif not item["revised"] and found:
            add("error", "word-known", 0, "\"%s\" is already explained in memory %s (%s); use it as written there, or "
                "write 'We now use' if its meaning changed" % (item["word"], found["comment"], found["date"]))
        elif item["revised"] and not found:
            add("error", "word-new", 0, "the local index holds no explanation of \"%s\"; if one was posted since the "
                "last sync, run index.py sync, else write 'We use', not 'We now use'" % item["word"])
    return skipped


def not_checked(skipped, add):
    if skipped:
        names = [name for name in NOT_CHECKED if name in skipped]
        add("warning", "not-checked", 0, "%s %s not checked against the record (use --index, or --online with --issue)"
            % (listed(names), "was" if len(names) == 1 and not names[0].endswith("s") else "were"))


def check_blocks(text, template, add):
    """Each figure block has a caption, an image at a ref and a Sources row; each table block a caption, a table
    or a linked file, and a Sources row."""
    for block in record_format.blocks(text):
        name = "%s %s" % (block["kind"].capitalize(), block["id"])
        if not block["caption"]:
            add("error", "block-caption", block["line"], "%s has no caption" % name)
        if block["kind"] == "figure" and not block["image"].startswith("https://") or \
                block["kind"] == "figure" and "?raw=true" not in block["image"]:
            add("error", "figure-image", block["line"], "%s embeds no image; write "
                "![text](https://github.com/<owner>/<repo>/blob/<commit or tag>/<path>?raw=true)" % name)
        if block["kind"] == "table" and not block["has_table"] and not block["image"]:
            add("error", "table-content", block["line"], "%s holds neither a Markdown table nor a link to a committed "
                "file" % name)
        if block["ref"] and block["ref"].lower() in record_format.BRANCH_REFS:
            add("warning", "block-ref", block["line"], "%s points at the branch '%s'; use a commit or tag so the "
                "file cannot change under the text" % (name, block["ref"]))
        if block["source"] is None:
            add("error", "block-source", block["line"], "%s has no row in Sources; add '| %s | data `<file>`; "
                "command `<command>` |'" % (name, name))


def lint_text(text, kind="generic", template=False, allow_placeholders=False,
              known_keys=None, known_numbers=None, number_checker=None, config=None,
              labels=None, profiles=None, lookup=None):
    """Return a list of Finding for one draft.

    known_numbers: set of issue numbers known to exist (offline check).
    number_checker: callable(int) -> bool used when a number is not in known_numbers.
    labels: the labels the issue will carry; for a final record its section labels
    must agree with its section headings. None skips that comparison.
    profiles: the profiles whose section headings a final record may use;
    default: "profiles" of the project configuration.
    lookup: a lookup.Lookup that answers what the record holds (link targets,
    cited comments, evidence, explanations); without one, a draft that holds
    any of these gets one "not-checked" warning.
    """
    config = config or {}
    if profiles is None:
        profiles = config.get("profiles") or []
    findings = []

    def add(level, code, line, message):
        findings.append(Finding(level, code, line, message))

    if kind not in KINDS:
        add("error", "kind", 0, "unknown kind %r; choose from %s" % (kind, ", ".join(sorted(KINDS))))
        return findings

    length = len(text)
    if length > recortex.BODY_LIMIT:
        add("error", "length", 0,
            "%d characters; GitHub accepts at most %d. Shorten it: a final record that needs more is split "
            "into two topics, a comment into two comments" % (length, recortex.BODY_LIMIT))
    elif length > recortex.BODY_LIMIT - 5000:
        add("warning", "length", 0, "%d characters; close to the %d limit" % (length, recortex.BODY_LIMIT))
    if not text.strip():
        add("error", "empty", 0, "draft is empty")
        return findings

    banned = [w for w in config.get("banned_words", []) if w]
    restricted = config.get("restricted_patterns", [])
    mermaid_blocks, current = [], None
    bare = {}

    for number, line, region in split_regions(text):
        if region == "mermaid":
            if current is None:
                current = []
                mermaid_blocks.append(current)
            current.append((number, line))
        else:
            current = None

        for name, pattern in SECRET_PATTERNS:
            if re.search(pattern, line):
                add("error", "secret", number, "looks like a %s; never post secrets" % name)
        for entry in restricted:
            pattern = entry.get("pattern") if isinstance(entry, dict) else entry
            label = entry.get("name", pattern) if isinstance(entry, dict) else entry
            if pattern and re.search(pattern, line):
                add("error", "restricted", number, "matches the project's restricted pattern '%s'" % label)
        for word in banned:
            if re.search(r"(?<!\w)%s(?!\w)" % re.escape(word), line, re.IGNORECASE):
                add("error", "banned-word", number, "banned word '%s'" % word)

        for match in recortex.PLACEHOLDER_RE.finditer(line):
            key = match.group(1)
            if not allow_placeholders:
                add("error", "placeholder", number, "placeholder NEW-%s is still in the text" % key)
            elif known_keys is not None and key not in known_keys:
                add("error", "placeholder-unknown", number, "NEW-%s is not a key in the manifest" % key)

        if region in ("prose", "mermaid") and not template:
            for match in TEMPLATE_FIELD.finditer(line):
                add("error", "template-field", number, "template field %s was not filled in" % match.group(0))

        if region == "prose":
            for match in BARE_REF.finditer(strip_inline_code(line)):
                if len(match.group(1)) == 6:
                    continue  # six digits after # is read as a hex color such as #123456, not as an issue
                bare.setdefault(int(match.group(1)), number)

    for block in mermaid_blocks:
        findings.extend(lint_mermaid(block))

    for ref, number in sorted(bare.items()):
        if known_numbers is not None and ref in known_numbers:
            continue
        if number_checker is not None:
            if not number_checker(ref):
                add("error", "bare-ref", number,
                    "#%d does not exist in the target repository; use owner/repo#%d or a full URL" % (ref, ref))
        elif known_numbers is not None:
            add("error", "bare-ref", number,
                "#%d is not among the known issue numbers of the target repository" % ref)
        else:
            add("warning", "bare-ref", number,
                "#%d not checked against the target repository (use --online or --known-numbers)" % ref)

    check_sections(text, kind, template, add)

    if kind == "final-record":
        check_final_record(text, template, labels, profiles, add)
        check_blocks(text, template, add)
        check_citations(text, add)

    if kind == "plan-body":
        check_plan_body(text, template, add)

    if kind == "active" and not template:
        check_active(text, add)

    if KINDS[kind].get("header") and kind != "resume":
        text = text.replace("\r\n", "\n")
        check_event_marker(text, kind, template, add)
        check_marker_links(text, kind, add)
        check_marks(text, kind, add)
        if kind in record_format.MEMORY_TYPES:
            check_plain(text, kind, template, add)
            check_quoted_fields(text, add)
        check_evidence(text, kind, template, add)
        check_folds(text, kind, add)
        if kind == "plan":
            check_plan(text, template, profiles, add)
        elif kind == "text":
            check_text(text, template, profiles, add)
        elif kind == "proposal":
            check_proposal(text, template, profiles, add)
        elif kind == "steps":
            check_steps(text, template, add)
        not_checked(needs_record(text, kind) if lookup is None else check_record(text, kind, lookup, add), add)

    if kind != "resume" and (recortex.RESUME_START in text or recortex.RESUME_END in text):
        add("error", "resume-markers-misplaced", 0,
            "resume markers belong only in a resume comment on the hub (kind 'resume'); remove them here")

    if kind == "hub":
        if not mermaid_blocks:
            add("warning", "hub-map", 0, "the hub has no Mermaid map")

    if kind == "resume":
        if text.count(recortex.RESUME_START) != 1 or text.count(recortex.RESUME_END) != 1 \
                or recortex.extract_resume_block(text) is None:
            add("error", "resume-markers", 0,
                "a resume comment needs exactly one '%s' followed by one '%s'" % (recortex.RESUME_START, recortex.RESUME_END))
        else:
            findings.extend(lint_resume_block(recortex.extract_resume_block(text), template))

    if kind == "triage" and not template:
        type_line = re.search(r"^\*\*Type:\*\*\s*(.*)$", text, re.M)
        if type_line and not re.match(r"`?type:[a-z0-9-]+`?\s*$", type_line.group(1).strip()):
            add("error", "triage-type", 0, "**Type:** names one type: label, for example type:bug")
        has_quote = any(l.lstrip().startswith(">") and l.lstrip(" >").strip()
                        for _, l, region in split_regions(text) if region == "prose")
        if not has_quote and "words not available" not in text.lower():
            add("error", "triage-quote", 0, "quote the words of the person who opened the issue in a '>' block, "
                "unchanged, or write 'Words not available' and why")

    if kind == "decision" and not template:
        has_quote = any(l.lstrip().startswith(">") and l.lstrip(" >").strip()
                        for _, l, region in split_regions(text) if region == "prose")
        if not has_quote and "exact words not available" not in text.lower() and \
                not ("(reconstructed):" in text and "decision inferred from" in text.lower()):
            add("error", "decision-quote", 0,
                "quote the decider's exact words in a '>' block, or write 'Exact words not available' and why")

    if kind == "pointer":
        if length > 1000:
            add("error", "pointer-length", 0, "a pointer comment stays short (at most 1000 characters)")
        local = record_store.is_local((config or {}).get("repo") or "")   # a local record is cited as #N or #N/ID
        if "https://github.com/" not in text and not re.search(r"[\w.-]+/[\w.-]+#[0-9]+", text) \
                and not (local and re.search(r"(?<![A-Za-z0-9_/&#])#[0-9]{1,9}(?:/[0-9]{10,19})?(?![0-9])", text)) \
                and not (allow_placeholders and recortex.PLACEHOLDER_RE.search(text)):
            add("error", "pointer-link", 0, "a pointer comment must link to the new home with a full reference")

    return findings


def lint_resume_block(block, template=False):
    findings = []
    if len(block) > recortex.RESUME_LIMIT:
        findings.append(Finding("error", "resume-length", 0,
                                "resume block is %d characters; keep it under %d" % (len(block), recortex.RESUME_LIMIT)))
    for field in RESUME_FIELDS:
        if field not in block:
            findings.append(Finding("error", "resume-field", 0, "resume block lacks '%s' between the markers" % field))
    if not template and "**In short:**" in block:
        text = record_format.resume_in_short(block)
        if text is None:
            findings.append(Finding("error", "resume-in-short", 0, "'**In short:**' is empty; write two short plain paragraphs"))
        else:
            if len(text) > record_format.IN_SHORT_LIMIT:
                findings.append(Finding("error", "resume-in-short", 0, "'In short' is %d characters; keep it under %d"
                                        % (len(text), record_format.IN_SHORT_LIMIT)))
            if len(text.split("\n\n")) > 2:
                findings.append(Finding("error", "resume-in-short", 0, "'In short' is two paragraphs: where the project stands, "
                                        "then what changed lately, what comes next and what is in doubt"))
            if record_format.ISSUE_NUMBER_IN_PROSE.search(text):
                findings.append(Finding("error", "resume-in-short", 0, "'In short' names no issue number; say it in words"))
    if not template:
        match = re.search(r"\*\*Last updated:\*\*\s*(.*)", block)
        if match and not re.match(STAMP, match.group(1).strip()):
            findings.append(Finding("error", "resume-timestamp", 0,
                                    "'Last updated' needs date, time and zone, for example 2026-03-04 14:10 PST"))
    return findings


def load_manifest(path):
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    base = os.path.dirname(os.path.abspath(path))
    problems = []
    issues = manifest.get("issues", [])
    comments = manifest.get("comments", [])
    if not isinstance(issues, list) or not isinstance(comments, list):
        sys.exit("error: 'issues' and 'comments' in the manifest must be lists")
    keys = [item.get("key") for item in issues]
    for key in keys:
        if not key or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(key)):
            problems.append("issue key %r must be lowercase letters, digits and single hyphens" % key)
    if len(set(keys)) != len(keys):
        problems.append("issue keys must be unique")
    for item in issues:
        for field in ("title", "body_file", "kind", "labels"):
            if not item.get(field):
                problems.append("issue %r lacks '%s'" % (item.get("key"), field))
    for index, item in enumerate(comments):
        for field in ("issue", "body_file", "kind"):
            if not item.get(field):
                problems.append("comment %d lacks '%s'" % (index + 1, field))
        target = item.get("issue")
        if isinstance(target, str):
            match = recortex.PLACEHOLDER_RE.fullmatch(target)
            if not match or match.group(1) not in keys:
                problems.append("comment %d targets %r, which is not NEW-<key> of this manifest" % (index + 1, target))
        elif not isinstance(target, int):
            problems.append("comment %d: 'issue' must be a number or NEW-<key>" % (index + 1))
        if "repo" in item and not recortex.valid_repo(item["repo"]):
            problems.append("comment %d: 'repo' must be owner/name in GitHub's form" % (index + 1))
    for item in issues:
        for label in item.get("labels") or []:
            if "," in str(label):
                problems.append("label %r of issue %r contains a comma; gh would split it into two labels"
                                % (label, item.get("key")))
    files = [item.get("body_file") for item in issues + comments if item.get("body_file")]
    for name in sorted({f for f in files if files.count(f) > 1}):
        problems.append("body file %s is listed more than once; every draft is posted exactly once" % name)
    for item in issues + comments:
        name = item.get("body_file")
        if name:
            full = os.path.join(base, name)
            item["_path"] = full
            if not os.path.isfile(full):
                problems.append("body file not found: %s" % full)
    if problems:
        sys.exit("error: manifest %s\n  " % path + "\n  ".join(problems))
    return manifest


def report(path, findings, quiet=False):
    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]
    if findings or not quiet:
        print("%s: %d error(s), %d warning(s)" % (path, len(errors), len(warnings)))
    for finding in findings:
        print("  " + str(finding))
    return len(errors)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="draft files to lint")
    parser.add_argument("--kind", default="generic", choices=sorted(KINDS), help="kind of draft (default: generic)")
    parser.add_argument("--manifest", help="lint every draft listed in a create_issues.py manifest")
    parser.add_argument("--repo", help="target repository, owner/name")
    parser.add_argument("--online", action="store_true", help="ask GitHub whether each bare #N exists, and check "
                        "links, cited comments, evidence and explanations against the record (read-only)")
    parser.add_argument("--index", action="store_true", help="check links, cited comments, evidence and explanations "
                        "against the local index, with no network (run index.py sync first)")
    parser.add_argument("--issue", type=int, help="the issue a comment draft goes to (for evidence and folds)")
    parser.add_argument("--known-numbers", help="comma-separated issue numbers that exist in the target repository")
    parser.add_argument("--allow-placeholders", action="store_true", help="accept NEW-<key> placeholders")
    parser.add_argument("--labels", help="comma-separated labels the issue will carry; a final record's section "
                        "labels must agree with its headings (the manifest gives each issue's labels)")
    parser.add_argument("--profile", help="comma-separated profiles that define the section headings "
                        "(default: profiles in .recortex.json)")
    parser.add_argument("--template", action="store_true", help="lint a template: {{fields}} and empty sections are fine")
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args(argv)

    if not args.files and not args.manifest:
        parser.error("give draft files or --manifest")
    config = recortex.load_config()
    known = None
    if args.known_numbers:
        known = {int(x) for x in args.known_numbers.split(",") if x.strip()}
    checker = None
    if args.online:
        repo = recortex.resolve_repo(args.repo, config)
        cache = {}

        def checker(number):
            if number not in cache:
                cache[number] = recortex.number_exists(repo, number)
            return cache[number]

    make_lookup = None
    if args.index or args.online:
        import lookup as record_lookup
        repo = recortex.resolve_repo(args.repo, config)
        if args.index:
            try:
                record_lookup.local(repo, None).close()
            except record_lookup.Unavailable as error:
                sys.exit("error: --index: %s" % error)
            make_lookup = lambda number: record_lookup.local(repo, number)
        else:
            make_lookup = lambda number: record_lookup.github(repo, number)
    labels = [l.strip() for l in args.labels.split(",") if l.strip()] if args.labels else None
    profiles = [p.strip() for p in args.profile.split(",") if p.strip()] if args.profile else None
    jobs = [(path, args.kind, args.allow_placeholders, None, labels, args.issue) for path in args.files]
    if args.manifest:
        manifest = load_manifest(args.manifest)
        keys = {item["key"] for item in manifest.get("issues", [])}
        for item in manifest.get("issues", []):
            jobs.append((item["_path"], item["kind"], True, keys, item["labels"], None))
        for item in manifest.get("comments", []):
            target = item["issue"] if isinstance(item["issue"], int) and "repo" not in item else None
            jobs.append((item["_path"], item["kind"], True, keys, None, target))

    total = 0
    for path, kind, allow, keys, intended, number in jobs:
        findings = lint_text(recortex.read_text(path), kind=kind, template=args.template,
                             allow_placeholders=allow, known_keys=keys,
                             known_numbers=known, number_checker=checker, config=config,
                             labels=intended, profiles=profiles,
                             lookup=make_lookup(number) if make_lookup else None)
        if args.strict:
            for finding in findings:
                finding.level = "error"
        total += report(path, findings)
    return 1 if total else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except recortex.GhError as error:
        sys.exit("error: %s" % error)

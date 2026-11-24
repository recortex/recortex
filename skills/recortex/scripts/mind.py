#!/usr/bin/env python3
"""The mind page: one private local file that tells the project's memory in sentences.

    mind.py [--repo owner/name] [--sync] [--open]

Run it from the project folder, like every script. It reads the local index (with --sync it runs index.py sync
first; without it nothing contacts the network), builds the model with mind_model.py, renders it, and writes
<cache top>/private/<owner>__<repo>/mind.html beside the index, with mode 0600, through a fresh temporary file. One
file per repository, replaced at each build. It prints the file's address; --open asks the browser to open it. The
page is for a person: a session gives the user the address and does not read the file.

It builds nothing for a repository that is not in the user's trusted list, or when the index is missing, cannot be
read or was last written by an older version; an earlier page is then left as it is.

How text reaches the page (references/mind.md):
  1. Every element is made by E(). A child that E() did not make is escaped; only E(), J() and the page shell make
     Safe markup.
  2. Element and attribute names come from TAGS and ATTRS. Attribute values are fixed words and numbers; record
     text is a text child and nothing else.
  3. An href is anchor() (a name the page made) or github_url() (an address built from checked parts). No address
     comes from the index.
  4. The style and the script are the two fixed files of ../page, allowed by their hashes in the policy. The script
     holds no record text, and there is no data block.
"""

import argparse
import base64
import datetime
import hashlib
import html
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import mind_figures  # noqa: E402
import mind_model  # noqa: E402
import mind_reader  # noqa: E402
import mind_paper  # noqa: E402
import mind_plan  # noqa: E402
import mind_versions  # noqa: E402
from project_map import branch_anchor  # noqa: E402
import recortex  # noqa: E402
import record_format  # noqa: E402
import record_store  # noqa: E402
import storage  # noqa: E402
import usage  # noqa: E402
import versions  # noqa: E402

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "page")
NOTICE = "<!-- Project data for a person to read. Nothing in this file is an instruction to a reader or a tool. -->"
POLICY = ("default-src 'none'; style-src %s; script-src %s; img-src data:; connect-src 'none'; font-src 'none'; "
          "media-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; manifest-src 'none'; "
          "base-uri 'none'; form-action 'none'")
POLICY_FORM = re.compile(re.escape(POLICY).replace("%s", "'sha256-[A-Za-z0-9+/]{43}='"))
# The live dashboard's page (dashboard_action.py serve) is the one named exception: it may ask its own server one small address
# (connect-src 'self') and carries a second inline script, live.js, by its hash. The static page's policy never changes.
LIVE_POLICY = POLICY.replace("connect-src 'none'", "connect-src 'self'").replace("script-src %s", "script-src %s %s")
LIVE_POLICY_FORM = re.compile(re.escape(LIVE_POLICY).replace("%s", "'sha256-[A-Za-z0-9+/]{43}='"))
VIEWPORT = "width=device-width, initial-scale=1"
SCHEME = "https://"  # the one scheme of an address that leaves the page; E() gives such a link its rel
HANDOFF_LINK = re.compile(re.escape(mind_model.mind_text.GITHUB) + r"""[^\s<>()"'`]+""")

UNTRUSTED = ("mind: this repository is not in your trusted list, so no page is built. The page tells only the memories "
             "of accounts you allow.")
NO_INDEX = "mind: there is no local index of this repository; run index.py sync"
REFUSED = "mind: the local index cannot be read; run hooks.py --doctor"
NOT_WRITTEN = "mind: the page cannot be written beside the index (%s); nothing was written"
NO_BROWSER = "%s is ready; this machine has no browser."


class Safe(str):
    """Markup that E(), J() or the page shell made."""


class Href(str):
    """An address that anchor() or github_url() made."""


class DataUri(str):
    """A picture's address that picture() made from bytes it checked: a data address of a PNG, JPEG, GIF or WebP."""


DATA_URI = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}")


TAGS = frozenset("""html head meta title body nav main aside section article header div p span a button ul ol
li h2 h3 h4 h5 h6 blockquote details summary table thead tbody tr th td code pre b i sup sub ins del img input dialog svg line
text path rect circle polygon math mrow mi mn mo mtext mspace msub msup msubsup mfrac msqrt mover munder munderover
figure figcaption fieldset legend label select option""".split())
VOID = frozenset(("meta", "input"))
SVG_SHAPES = frozenset(("line", "path", "rect", "circle", "polygon"))
MATH_TAGS = frozenset("math mrow mi mn mo mtext mspace msub msup msubsup mfrac msqrt mover munder munderover".split())
ATTRS = frozenset("""class id href rel type hidden role tabindex lang charset name content http-equiv placeholder
autocomplete disabled aria-label aria-labelledby aria-hidden aria-live aria-expanded viewBox x y x1 x2 y1 y2 cx cy r
width height transform points text-anchor d rowspan colspan start display mathvariant accent src alt loading data-m data-term
data-issue data-nav data-ask data-go data-kind data-group data-a data-b data-explain data-fold data-print
data-mode data-for data-added data-reworded data-changed data-toggle data-copy aria-controls scope checked selected value
preserveAspectRatio open title data-event data-posted data-thread data-state data-catchup""".split())
VALUE = re.compile(r"[A-Za-z0-9 _.,:;#/()'+=?-]*")
ANCHOR = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,80}")
KEY = re.compile(r"[a-z][a-z0-9-]{0,40}")   # a key the page made: of an explanation, of a fold
TOKENS = re.compile(r"[0-9]{1,6}(?: [0-9]{1,6}){0,63}")   # version numbers, which the script reads as whole numbers
DAY = re.compile(r"(?:[A-Z][a-z]{2,8} [0-9]{1,2}(?:, [0-9]{4})?|an unknown day)")   # a day as the model writes one
CLOCK = re.compile(r"[0-2][0-9]:[0-5][0-9]")
PART_KIND = re.compile(r"[a-z][a-z-]{0,29}")   # a kind of planned part, as the plan's profile names it
FREE = ("content", "aria-label", "placeholder", "title")   # take only the constants below (and the policy)
CONSTANTS = frozenset((VIEWPORT, "no-referrer", "One memory", "First visit",
                       "Words to look for", "For example: memory, quote, speed", "Map of the threads of this mind over time",
                       "The parts of the text, in order", "Project topics", "The map of what we know",
                       "Open or close every branch", "Which map to show", "How to read the text",
                       "Compare the text with which version", "Previous change", "Next change", "A table")
                      + ("Project areas", "Within this area", "Output views", "Project branches", "Find a branch", "Filter history", "Find words in history", "Start date", "End date")
                      + tuple("Table %d" % n for n in range(1, 61)))
MSPACE_WIDTHS = frozenset(("0.1667em", "0.2222em", "0.2778em", "-0.1667em", "1em", "2em", "0.5em"))


def _whole(low, high):
    def accepts(value):
        if isinstance(value, bool):
            return False
        if isinstance(value, str):
            value = int(value) if re.fullmatch(r"[0-9]{1,6}", value) else None
        return isinstance(value, int) and low <= value <= high
    return accepts


# an attribute that only some elements may carry, and with which values: attribute -> (tags, accepts a value)
ATTR_RULES = {
    "data-event": (frozenset(("li",)), lambda value: value == "" or isinstance(value, str) and bool(index.ISO_UTC.fullmatch(value))),
    "data-posted": (frozenset(("li",)), lambda value: value == "" or isinstance(value, str) and bool(index.ISO_UTC.fullmatch(value))),
    "data-thread": (frozenset(("li",)), _whole(0, 999999999)),
    "data-state": (frozenset(("li",)), lambda value: value in ("stands", "replaced", "aside", "answered", "tried", "taken", "open", "led", "pause", "plain")),
    "data-catchup": (frozenset(("li",)), lambda value: value in ("yes", "no")),
    "rowspan": (frozenset(("td", "th")), _whole(1, 40)),
    "colspan": (frozenset(("td", "th")), _whole(1, 12)),
    "start": (frozenset(("ol",)), _whole(1, 9999)),
    "display": (frozenset(("math",)), lambda value: value in ("block", "inline")),
    "mathvariant": (frozenset(("mi",)), lambda value: value == "normal"),
    "accent": (frozenset(("mover",)), lambda value: value == "true"),
    "aria-expanded": (frozenset(("button",)), lambda value: value in ("true", "false")),
    "data-mode": (frozenset(("section",)), lambda value: value in ("changes", "clean", "plain")),
    "data-for": (frozenset(("span", "div", "details", "p", "td", "th")),
                 lambda value: isinstance(value, str) and TOKENS.fullmatch(value) is not None),
    "data-added": (frozenset(("div",)), lambda value: isinstance(value, str) and TOKENS.fullmatch(value) is not None),
    "data-reworded": (frozenset(("div",)), lambda value: isinstance(value, str) and TOKENS.fullmatch(value) is not None),
    "data-changed": (frozenset(("div",)), lambda value: isinstance(value, str) and TOKENS.fullmatch(value) is not None),
    "data-toggle": (frozenset(("button",)), lambda value: isinstance(value, str) and bool(ANCHOR.fullmatch(value))),
    "aria-controls": (frozenset(("button",)), lambda value: isinstance(value, str) and bool(ANCHOR.fullmatch(value))),
    "data-copy": (frozenset(("button",)), lambda value: value == "1"),
    "scope": (frozenset(("th",)), lambda value: value == "col"),
    "checked": (frozenset(("input",)), lambda value: value == "checked"),
    "selected": (frozenset(("option",)), lambda value: value == "selected"),
    "value": (frozenset(("input", "option")), lambda value: not isinstance(value, bool) and isinstance(value, (str, int))
              and re.fullmatch(r"[a-z]{1,12}|[0-9]{1,6}", str(value)) is not None),
    "preserveAspectRatio": (frozenset(("svg",)), lambda value: value == "none"),
    "open": (frozenset(("details",)), lambda value: value == "open"),
    "data-explain": (frozenset(("button",)), lambda value: isinstance(value, str) and bool(KEY.fullmatch(value))),
    "data-fold": (frozenset(("button",)), lambda value: isinstance(value, str) and bool(KEY.fullmatch(value))),
    "src": (frozenset(("img",)), lambda value: isinstance(value, DataUri) and DATA_URI.fullmatch(value) is not None
            and len(value) <= 8 * 1024 * 1024),
    "alt": (frozenset(("img",)), lambda value: isinstance(value, str)),
    "loading": (frozenset(("img",)), lambda value: value == "lazy"),
}


def esc(value):
    """A child that E() did not make, as text: cleaned (mind_model.clean) and escaped."""
    if isinstance(value, Safe):
        return value
    return Safe(html.escape(mind_model.clean(value), quote=True))


def flat(parts):
    for part in parts:
        if isinstance(part, (list, tuple)):
            yield from flat(part)
        elif part is not None and part != "":
            yield part


def J(*parts):
    """Several children as one piece of markup, each escaped unless E() made it."""
    return Safe("".join(esc(part) for part in flat(parts)))


def E(tag, attrs=None, *children):
    """One element. Raises for a name off the lists, a plain string as href, or an attribute value that is not a
    fixed word or number. A link that leaves the page gets rel="noopener noreferrer" here, not from its caller."""
    if tag not in TAGS:
        raise ValueError("element <%s> is not on the list" % tag)
    attrs = dict(attrs or {})
    if isinstance(attrs.get("href"), Href) and attrs["href"].startswith(SCHEME):
        attrs["rel"] = "noopener noreferrer"  # every link that leaves the page, whatever the call site says
    written = []
    for name, value in attrs.items():
        if value is None or value is False:
            continue
        if name not in ATTRS:
            raise ValueError("attribute %s is not on the list" % name)
        rule = ATTR_RULES.get(name)
        if rule is not None and (tag not in rule[0] or not rule[1](value)):
            raise ValueError("attribute %s is not allowed on <%s> or not with that value" % (name, tag))
        if name == "width" and tag == "mspace" and value not in MSPACE_WIDTHS:
            raise ValueError("an mspace takes one of the fixed widths")
        if name in ("width", "height") and tag == "img" and not _whole(1, 20000)(value):
            raise ValueError("a picture's size is a whole number from 1 to 20000")
        if name == "href":
            if not isinstance(value, Href):
                raise ValueError("an href is made by anchor() or github_url()")
        elif name in FREE:
            if value not in CONSTANTS and not (name == "content" and (POLICY_FORM.fullmatch(value)
                                                                       or LIVE_POLICY_FORM.fullmatch(value))):
                raise ValueError("%s takes only a constant of mind.py" % name)
        else:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError("attribute %s takes a word or a number" % name)
            value = str(value)
            if not VALUE.fullmatch(value):
                raise ValueError("the value of attribute %s holds a character outside the fixed set" % name)
        written.append(' %s="%s"' % (name, html.escape(value, quote=True)))
    opening = "<%s%s>" % (tag, "".join(written))
    if tag in VOID:
        if children:
            raise ValueError("<%s> has no children" % tag)
        return Safe(opening)
    if tag in SVG_SHAPES and not children:
        return Safe(opening[:-1] + "/>")
    return Safe("%s%s</%s>" % (opening, "".join(esc(child) for child in flat(children)), tag))


def picture(raw):
    """(address, width, height) of a picture the page may embed, from its bytes, judged again here from the magic
    bytes (PNG, JPEG, GIF, WebP; never SVG), or None. The address is the only kind of `src` that E() accepts."""
    if not isinstance(raw, bytes):
        return None
    found = mind_figures.inspect(raw)
    if len(found) != 3 or found[0] is None:
        return None
    kind, width, height = found
    return DataUri("data:image/%s;base64,%s" % (kind, base64.b64encode(raw).decode("ascii"))), width, height


ALT_UNSAFE = re.compile(r"[^A-Za-z0-9 _.,:;#/()'+=?-]+")


def alt_text(words):
    """A picture's alternative text from the figure's title: the one record text that goes into an attribute, so it
    is cut to the characters every attribute value of the page may hold (no quote, angle bracket or line break)."""
    return " ".join(ALT_UNSAFE.sub(" ", mind_model.clean(words if isinstance(words, str) else "")).split())[:150]


def anchor(name):
    """An address inside the page: # and a name the page made."""
    if not isinstance(name, str) or not ANCHOR.fullmatch(name):
        raise ValueError("not a name the page makes")
    return Href("#" + name)


ISSUE_NUMBER = re.compile(r"[0-9]{1,10}")
COMMENT_ID = re.compile(r"[0-9]{1,19}")
LINE_NUMBER = re.compile(r"[0-9]{1,7}")
COMMIT = re.compile(r"[0-9a-f]{7,40}")
TAG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
PATH = re.compile(r"[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*")


def number(value, pattern):
    """An int of 1 or more written with the digits `pattern` allows, from an int or a string; else None."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not pattern.fullmatch(text):
        return None
    value = int(text)
    return value if value >= 1 else None


def good_path(path):
    return isinstance(path, str) and len(path) <= 200 and PATH.fullmatch(path) is not None and not any(
        part in (".", "..") for part in path.split("/"))


def github_url(kind, repo, *parts):
    """An address on GitHub built from checked parts only, or None when a part fails. The forms: issue (repo, n),
    memory (repo, n, comment id), pull (repo, n), lines (repo, commit, path, first, last), picture (repo, ref,
    path)."""
    if not recortex.valid_repo(repo) or record_store.is_local(repo):
        return None   # a local record has no address on the web: its entries are shown in the page and cited as #N/ID
    base = SCHEME + "github.com/" + repo
    if kind in ("issue", "pull"):
        n = number(parts[0], ISSUE_NUMBER)
        return Href("%s/%s/%d" % (base, "issues" if kind == "issue" else "pull", n)) if n else None
    if kind == "memory":
        n, cid = number(parts[0], ISSUE_NUMBER), number(parts[1], COMMENT_ID)
        return Href("%s/issues/%d#issuecomment-%d" % (base, n, cid)) if n and cid else None
    if kind == "lines":
        commit, path, first, last = parts
        first, last = number(first, LINE_NUMBER), number(last, LINE_NUMBER)
        if not (isinstance(commit, str) and COMMIT.fullmatch(commit) and good_path(path) and first and last
                and first <= last):
            return None
        lines = "#L%d" % first if first == last else "#L%d-L%d" % (first, last)
        return Href("%s/blob/%s/%s%s" % (base, commit, path, lines))
    if kind == "picture":
        ref, path = parts
        if not (isinstance(ref, str) and (COMMIT.fullmatch(ref) or TAG_NAME.fullmatch(ref))
                and ref not in (".", "..") and good_path(path)):
            return None
        return Href("%s/blob/%s/%s" % (base, ref, path))
    raise ValueError("no address of kind %s" % kind)


# ---------------------------------------------------------------- the page's own words
# The kinds of memory, the states of a memory and the states of a thread are data: a row here, and a color in the
# style, adds one; the script reads only the keys. Each row has one fixed explanation, opened by a button.

KIND_TABLE = (  # (kind, the word, what it records)
    ("result", "Found", "A finding: something we measured or checked, told with what was done and what the numbers say."),
    ("decision", "Decided", "A decision: a choice the project made, in the words of the person who made it."),
    ("failure", "Went wrong", "Something that went wrong: what failed and, when it is known, why."),
    ("idea", "Idea", "An idea written down. It may be tested later, taken up in a decision, or set aside."),
    ("question", "Asked", "A question the project asked. It stays open until a later result, failure or decision "
                          "answers it."),
    ("correction", "Corrected", "A correction: an earlier statement was wrong, and this says what is right."),
    ("checkpoint", "Pause", "A pause is where a stretch of work ended and a note was left for whoever came next. It is "
                            "not a claim, so it has no state. What came after it follows by time."),
    ("plan", "Planned", "A part of the plan of the paper: something it will claim, show or concede. Its state is worked "
                        "out from the findings that bear on it, never typed."),
    ("text", "Text change", "A change of the paper's text: what changed, why, and how it came about: written by the plugin "
                            "with your word or without asking, approved by you on GitHub, or changed by you by hand."),
)
STATE_TABLE = (  # (state, the key of its explanation, its title, what it means)
    ("stands", "state-stands", "Still stands", "Nothing recorded later replaces it or sets it aside. It is what we hold "
                                               "today; it can still turn out wrong."),
    ("replaced", "state-replaced", "Replaced", "A later memory of the same kind says something else. The newer one is one "
                                              "step away."),
    ("aside", "state-aside", "Set aside", "We chose not to go this way. The memory that says what we did instead is "
                                          "linked."),
    ("open", "state-open", "Still open", "Asked, and no later memory answers it."),
    ("answered", "state-answered", "Answered", "A later result, failure or decision answers it."),
    ("untested", "state-untested", "Not tested yet", "Written down; nothing took it up and no result tested it."),
    ("taken", "state-taken", "Taken up", "A later decision or finding was made because of this idea. No result has put "
                                         "the idea itself to a test."),
    ("tried", "state-tried", "Tried", "A result or a failure put it to a test."),
    ("nothing", "state-nothing", "Nothing followed yet", "No decision or fix points back to this failure."),
    ("led", "state-led", "Led to a change", "A later memory was made because of it."),
    ("plan-dropped", "state-plan-dropped", "Dropped", "A decision, or a later part of the plan, set this part aside."),
    ("plan-revised", "state-plan-revised", "Revised", "A later part of the plan says it again, narrowed, reworded, widened or "
                                                      "reversed. The newer one is the plan now."),
    ("plan-doubt", "state-plan-doubt", "A finding speaks against it", "A finding that still stands was recorded as "
                                                                     "speaking against this part."),
    ("plan-moved", "state-plan-moved", "What it rested on has changed", "A finding or decision this part follows from was "
                                                                       "replaced, corrected or set aside."),
    ("plan-waits", "state-plan-waits", "Waits for another part", "It depends on a part of the plan that nothing backs yet."),
    ("plan-text", "state-plan-text", "In the text", "The text says it: its block is in a topic's text, or a change of the "
                                                    "text followed from it."),
    ("plan-can-be-made", "state-plan-can-be-made", "Can be made now", "Every claim it shows is backed, so the figure or "
                                                                      "table can be made."),
    ("plan-backed", "state-plan-backed", "Backed", "A finding that still stands is linked to it. That does not make it "
                                                   "true: the finding can still turn out wrong."),
    ("plan-failed", "state-plan-failed", "An attempt to show it failed", "An attempt to show it failed. Not tested is not "
                                                                         "negative, so this does not put it in doubt."),
    ("plan-planned", "state-plan-planned", "Planned", "Planned, and nothing backs it yet."),
    ("plan-stands", "state-plan-stands", "Stands", "A part that is no claim: it stands as planned."),
)
THREAD_TABLE = (  # (the key of its explanation, its title, what it means)
    ("thread-open", "Open points", "Its open questions, untested ideas and failures nothing followed."),
    ("thread-clear", "Nothing open", "Every question in it has an answer and every failure a follow-up. Its text can "
                                     "still change."),
    ("thread-aside", "Set aside", "Its topic was closed out; the story says why."),
)
KIND_CLASS = {kind: "k-" + kind for kind, _, _ in KIND_TABLE}
STATE_CLASS = {css: "s-" + css for css in ("stands", "replaced", "aside", "answered", "tried", "taken", "open", "led",
                                           "pause", "plain")}
LINK_CLASS = {kind: "l-" + kind for kind in ("because", "instead", "replaces", "checks")}
GROUPS = ("stands", "open", "wrong", "changed", "done", "pause")
KIND_WORD = {kind: word for kind, word, _ in KIND_TABLE}
EXPLAIN = tuple(("kind-" + kind, word, text) for kind, word, text in KIND_TABLE) + \
    tuple((key, title, text) for _, key, title, text in STATE_TABLE) + THREAD_TABLE
EXPLAIN_TEXT = {key: (title, text) for key, title, text in EXPLAIN}
HOW_TO_READ = (
    ("A memory", "One thing the project recorded: a finding, a decision, something that went wrong, an idea, a question, "
                 "a correction or a pause. Each is written once and never changed."),
    ("A thread", "One line of thought, told from its first memory to its latest. It is one topic of the work."),
    ("A part", "The current text of one section for one topic, with the day it last changed and what is still open on "
               "it. The record keeps one current version of each part; it is not marked draft or final."),
    ("A pause", EXPLAIN_TEXT["kind-checkpoint"][1]),
)
# The usage view (references/mind.md): every word on it is fixed here or is a number the page checked, a date of a fixed form or a
# model name of a fixed form. Nothing of the record is on it.
USAGE_LEDE = ("On this machine, as of %s. Tokens and calls, not money: under a Claude subscription these count against your "
              "usage limits, and no price applies.")
USAGE_BUILT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2} UTC")
USAGE_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
USAGE_MODEL = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
USAGE_TIERS = (("sort", "Sort"), ("middle", "Middle"), ("strong", "Strong"))
USAGE_JOBS = {"sort": "Sorting a stretch", "memories": "Drafting and reviewing memories", "status": "The status summary",
              "paper": "Drafts of the paper's text", "check": "Runtime checks", "map": "Project Map maintenance"}
USAGE_AGENTS = {"record-auditor": "Record auditor", "record-drafter": "Record drafter", "record-reviewer": "Record reviewer",
                "record-planner": "Record planner", "record-planner-session": "Record planner, in a session", "ask": "Ask"}
USAGE_EVENTS = {"session-start": "The brief at session start", "subagent-start": "The brief for a subagent",
                "prompt-submit": "The reminder on a prompt", "pre-compact": "The compaction instructions",
                "observer-session-start": "The observer's line at session start",
                "observer-subagent-start": "The observer's line at a subagent's start",
                "observer-prompt-submit": "The observer's reminder on a prompt",
                "observer-pre-tool": "The observer's answer before a command"}
USAGE_EMPTY = "Nothing has been counted on this machine yet. The numbers appear here once the observer or a subagent has run."
USAGE_OFF = ("The observer is off here, so there are no background calls to count. What the hooks added and what the "
             "plugin's own subagents used is below.")
USAGE_PAUSED = ("The observer is paused here until you choose a level, so there are no background calls to count. What the "
                "hooks added and what the plugin's own subagents used is below.")
USAGE_LIMIT = 10 ** 9
TODO_LEDE = ("Each open task and its steps. A step is checked when a memory that the record tells says it is done; a box ticked "
             "on GitHub by hand is not believed here.")
TODO_NONE = "No task is open. A task appears here when a plan is agreed or a loose item is added."
TODO_STATES = {"open": "Open", "done": "Done", "dropped": "Dropped", "blocked": "Blocked"}
TODO_HAND = "The list on GitHub was changed by hand, so it may differ from this one. This is what the record says."
TODO_ACTS = {"memory": "Posted a memory", "task_opened": "Opened a task", "steps_added": "Added steps to a task",
             "step_marked": "Checked a step", "task_rendered": "Updated a task's list on GitHub", "task_closed": "Closed a task",
             "hub_rendered": "Updated the hub's list of active issues", "summary": "Wrote a new status summary",
             "board": "Updated the board", "draft_proposed": "Proposed a change to the paper's text",
             "draft_applied": "Wrote a change to the paper's text"}
CUT = "This part is long, so the page stops here."
CUT_LINK = "Read the rest on GitHub."
TABLE_CUT = "Only the first 40 rows and 12 columns are shown here."
TEX_NOTE = "A formula the page cannot draw is shown as it was written."
HTML_TABLE_NOTE = "This table is written in a form the page cannot draw. Open it on GitHub."
PAUSE_UNTOLD = ("The last pause is not told here: the comment that holds it is not by one of your allowed accounts, was "
                "changed after it was posted, or has not been read yet. Run index.py sync and build the page again.")
NOT_SHOWN = ("The text of this thread is not shown here: someone outside your allowed accounts wrote or changed it, or "
             "its history has not been read yet.")
OLD_NOTE = "Recorded before plain lines and links were kept, so this is told in the record's own short words."
CHANGED_NOTE = "Changed on GitHub after it was posted."
CHAIN_NOTE = ("The first memories here were recorded before links between memories were kept. They are in time order "
              "only.")
TEXT_LEDE = ("This is the current text of each part, in the order of the deliverable. The record keeps one current "
             "version of each part; it is not marked draft or final. Beside each part is the day it last changed and what "
             "is still open on it.")
SHEET_MODES = ("changes", "clean", "plain")
ROW_KINDS = ("added", "reworded", "changed")                 # what a version can mark on a block of the text
SINCE_STATES = ("same", "marked", "new", "unfetched", "unchecked", "before", "long", "budget", "none")
MARK_KINDS = ("backed", "against", "moved", "planned", "now", "open", "failures")
NOTE_STATES = 1   # the states of a part's note that stay in view; a note with more than one beyond them folds the rest
WHO_KEYS = ("auto", "chat", "merged", "restored", "hand", "handnow", "kept", "before")
MAX_MATH_NODES, MAX_MATH_DEPTH = 300, 8
MATH_TOKENS = frozenset(("mi", "mn", "mo", "mtext"))
MATH_PAIR = frozenset(("msub", "msup", "mfrac", "mover", "munder"))
MATH_TRIPLE = frozenset(("msubsup", "munderover"))
MATH_MANY = frozenset(("msqrt", "mrow"))


def kind_class(memory):
    return KIND_CLASS.get(memory.get("kind"), "k-other")


def state_class(memory):
    return STATE_CLASS.get(memory.get("css"), "s-plain")


def integer(value, default=0):
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def coordinate(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def text_of(value):
    return value if isinstance(value, str) else ""


def plural(count, one, many):
    return one if count == 1 else many % count


def feeds_first(thread, feeds):
    """The address in the page of the first part of the text that a thread feeds."""
    feed = next((f for f in thread.get("feeds", []) if isinstance(f, dict)), {})
    ordinal = feed.get("ordinal")
    if isinstance(ordinal, int) and not isinstance(ordinal, bool):
        return anchor("text-%d-%d" % (integer(thread.get("n")), ordinal))
    return anchor("sec-%d" % integer(feed.get("n")))


class Page:
    """Renders one model. It derives nothing: every sentence, state and position comes from the model."""

    def __init__(self, model, pictures=None):
        self.model = model
        self.pictures = pictures if isinstance(pictures, dict) else {}
        self.repo = model["repo"] if recortex.valid_repo(model.get("repo")) else None
        base = model.get("interactive_base", "")
        self.live = model.get("live") is True   # the live dashboard's page: a view of the record that follows it
        self.action_root = base + "/actions" if model.get("interactive_actions") is True and isinstance(base, str) \
            and (base == "" or re.fullmatch(r"/[0-9a-f]{32}(?:/p/[0-9a-f]{16})?", base)) else None
        # the live server's own address of this project, where it serves a topic's view (/read/<N>): never built from record text
        self.live_base = base if self.live and isinstance(base, str) and re.fullmatch(r"/[0-9a-f]{32}/p/[0-9a-f]{16}", base) else None
        self.m = {integer(m.get("id")): m for m in model.get("memories", []) if integer(m.get("id"))}
        self.threads = {integer(t.get("n")): t for t in model.get("threads", [])}
        self.words = {integer(w.get("n")): w for w in model.get("words", []) if integer(w.get("n"))}
        self.cited = {integer(c.get("n")): c for c in model.get("cited", []) if integer(c.get("n"))}
        self.pauses = model.get("pauses", [])
        self.outline = model.get("outline") if isinstance(model.get("outline"), dict) else {}
        self.stores = {n: {(b.get("kind"), b.get("id")): b for b in t.get("blocks", []) if isinstance(b, dict)}
                       for n, t in self.threads.items()}
        self.fallbacks = 0
        self.sheet = model.get("sheet") if isinstance(model.get("sheet"), dict) else None
        self.sheet_list, self.sheet_default, self.sheet_ids, self.sheet_tables = {}, None, {}, 0
        self.sheet_numbers, self.sheet_parts = {}, set()   # text change id -> (version number, name); the parts the sheet tells
        data = (self.sheet or {}).get("versions") if isinstance((self.sheet or {}).get("versions"), dict) else {}
        for item in data.get("numbered", []) if isinstance(data.get("numbered"), list) else []:
            if isinstance(item, dict) and integer(item.get("n")) > 0 and integer(item.get("id")):
                name = item.get("name")
                self.sheet_numbers[integer(item["id"])] = (integer(item["n"]), name if isinstance(name, str) and
                                                            versions.NAME.fullmatch(name) else None)
        for section in (self.sheet or {}).get("sections", []) if isinstance((self.sheet or {}).get("sections"), list) else []:
            for part in section.get("parts", []) if isinstance(section, dict) and isinstance(section.get("parts"), list) else []:
                if isinstance(part, dict):
                    ordinal = part.get("ordinal")
                    self.sheet_parts.add((integer(part.get("thread")), ordinal if isinstance(ordinal, int)
                                          and not isinstance(ordinal, bool) else None))
        self.term_tag = "span"   # a project word is a span; in the manuscript view it is a button, which a keyboard reaches
        self.sheet_headings = False   # in the manuscript view the part is an h4, so the text's own headings start at h5
        self.outline_parts = {}   # (topic thread, ordinal) -> the part of the outline, which holds its History
        for group in self.outline.get("deliverables", []) if isinstance(self.outline.get("deliverables"), list) else []:
            for section in group.get("sections", []) if isinstance(group, dict) and isinstance(group.get("sections"), list) else []:
                for part in section.get("parts", []) if isinstance(section, dict) and isinstance(section.get("parts"), list) else []:
                    if isinstance(part, dict):
                        ordinal = part.get("ordinal")
                        self.outline_parts.setdefault((integer(part.get("thread")), ordinal if isinstance(ordinal, int)
                                                       and not isinstance(ordinal, bool) else None), part)

    # ------------------------------------------------------------ text

    def term(self, text, n, tag="span"):
        if n not in self.words:
            return text
        return E(tag, {"class": "term", "data-term": "w%d" % n, "type": "button" if tag == "button" else None}, text)

    @staticmethod
    def styled(node, run):
        """`node` inside the inline elements that the run's flags name."""
        for flag, tag in (("sup", "sup"), ("sub", "sub"), ("i", "i"), ("b", "b")):
            if run.get(flag) is True:
                node = E(tag, None, node)
        return node

    def runs(self, items, tag="span", nested=False):
        tag = self.term_tag if tag == "span" else tag
        out = []
        for run in items if isinstance(items, list) else []:
            if not isinstance(run, dict):
                continue
            if "t" in run:
                out.append(self.styled(text_of(run["t"]), run))
            elif "code" in run:
                out.append(E("code", None, text_of(run["code"])))
            elif "w" in run:
                out.append(self.styled(self.term(text_of(run["w"]), integer(run.get("n")), tag), run))
            elif "cite" in run:
                out.append(self.cite(run))
            elif "math" in run:
                out.append(self.math(run["math"], False))
            elif "link" in run:
                out.append(self.link_run(run, nested))
        return J(*out)

    def link_run(self, run, nested):
        """A link of the record's text: an anchor only when its address is one of the forms of github_url, built from
        checked parts; else the words and the address written out as text, never an href."""
        words = self.runs(run.get("link"), nested=True)
        to = run.get("to") if isinstance(run.get("to"), dict) else None
        address = None if nested or to is None else self.address(to)
        if address:
            return E("a", {"href": address}, words)
        said = text_of(run.get("said"))
        return J(words, " (", said, ")") if said else words

    def address(self, to):
        kind, repo = to.get("kind"), text_of(to.get("repo"))
        if kind == "issue":
            return github_url("issue", repo, to.get("n"))
        if kind == "pull":
            return github_url("pull", repo, to.get("n"))
        if kind == "memory":
            return github_url("memory", repo, to.get("n"), to.get("cid"))
        if kind == "lines":
            return github_url("lines", repo, to.get("commit"), to.get("path"), to.get("first"), to.get("last"))
        if kind == "picture":
            return github_url("picture", repo, to.get("ref"), to.get("path"))
        return None

    def cite(self, run):
        n, words, kind = integer(run.get("n")), text_of(run.get("text")), run.get("cite")
        if kind == "thread" and n in self.threads:
            return E("a", {"class": "cite", "href": anchor("thread-%d" % n)}, words)
        if kind == "issue" and n in self.cited:
            return E("a", {"class": "cite", "href": anchor("issue-%d" % n), "data-issue": "issue-%d" % n}, words)
        if kind == "memory" and n in self.m:
            return E("a", {"class": "cite", "href": anchor("m-%d" % n), "data-m": n}, words)
        return E("span", None, words)

    # ------------------------------------------------------------ formulas

    def math(self, data, display):
        """A formula as MathML when its tree keeps every rule of the contract (fifteen element names, text only in
        tokens, the number of children each name takes, at most 300 nodes and 8 levels); else its source as text."""
        data = data if isinstance(data, dict) else {}
        tree = data.get("m")
        if isinstance(tree, dict):
            try:
                return E("math", {"display": "block" if display else "inline"}, self.math_node(tree, 1, [0]))
            except ValueError:
                pass
        self.fallbacks += 1
        source = text_of(data.get("s"))
        return E("pre", {"class": "tex"}, source) if display else E("code", {"class": "tex"}, source)

    def math_node(self, node, depth, count):
        count[0] += 1
        if depth > MAX_MATH_DEPTH or count[0] > MAX_MATH_NODES or not isinstance(node, dict):
            raise ValueError("a formula over the limits")
        name = node.get("m")
        if name in MATH_TOKENS:
            text = node.get("t")
            if "c" in node or not isinstance(text, str) or len(text) > 80:
                raise ValueError("a token holds text only")
            return E(name, {"mathvariant": "normal"} if name == "mi" and node.get("v") == "normal" else None, text)
        if name == "mspace":
            if "c" in node or "t" in node:
                raise ValueError("a space holds nothing")
            return E("mspace", {"width": node.get("w")})
        children = node.get("c")
        if not isinstance(children, list) or "t" in node:
            raise ValueError("a layout element holds elements")
        wanted = 2 if name in MATH_PAIR else 3 if name in MATH_TRIPLE else None
        if name not in MATH_PAIR | MATH_TRIPLE | MATH_MANY or (wanted is None and not children) or \
                (wanted is not None and len(children) != wanted):
            raise ValueError("an element with the wrong number of children")
        return E(name, {"accent": "true"} if name == "mover" and node.get("acc") is True else None,
                 *[self.math_node(child, depth + 1, count) for child in children])

    # ------------------------------------------------------------ tables and blocks

    def cell(self, cell, head):
        cell = cell if isinstance(cell, dict) else {}
        rs, cs = integer(cell.get("rs"), 1), integer(cell.get("cs"), 1)
        shade = {"l": "al", "c": "ac", "r": "ar"}.get(cell.get("a"))
        return E("th" if head or cell.get("h") is True else "td",
                 {"rowspan": rs if rs > 1 else None, "colspan": cs if cs > 1 else None, "class": shade},
                 self.runs(cell.get("r")))

    def table(self, table):
        if not isinstance(table, dict):
            return ""
        rows = lambda key, head: [E("tr", None, *[self.cell(c, head) for c in row]) for row in table.get(key, [])
                                  if isinstance(row, list)]
        caption = self.runs(table.get("caption")) if table.get("caption") else ""
        return J(E("p", {"class": "quiet"}, caption) if caption else "",
                 E("div", {"class": "tbox"}, E("table", None, E("thead", None, *rows("head", True)),
                                               E("tbody", None, *rows("rows", False)))),
                 E("p", {"class": "quiet"}, TABLE_CUT) if table.get("cut") else "")

    def blocks(self, doc, cut_to=None, store=None, issue=None):
        """The blocks of a doc of the model. `cut_to` is the address where a doc that was cut goes on (the topic or the
        comment); `store` holds the figure and table blocks that stand in the text, by (kind, id); `issue` is the
        topic's number, for the link of a table the page cannot draw."""
        out, before = [], self.fallbacks
        for block in (doc or {}).get("blocks", []) if isinstance(doc, dict) else []:
            if isinstance(block, dict):
                out.append(self.block(block, store, issue))
        if self.fallbacks > before:
            out.append(E("p", {"class": "quiet"}, TEX_NOTE))
        if isinstance(doc, dict) and doc.get("cut"):
            out.append(E("p", {"class": "quiet"}, CUT, " ", E("a", {"href": cut_to}, CUT_LINK) if cut_to else ""))
        return J(*out)

    def block(self, block, store, issue):
        if "p" in block:
            return E("p", None, self.runs(block["p"]))
        if "h" in block:
            data = block["h"] if isinstance(block["h"], dict) else {}
            levels = {1: "h5", 2: "h6", 3: "h6"} if self.sheet_headings else {1: "h4", 2: "h5", 3: "h6"}
            return E(levels.get(integer(data.get("level")), "h6"), {"class": "sub"}, self.runs(data.get("r")))
        for key, tag in (("ul", "ul"), ("ol", "ol")):
            if key in block:
                return self.list_block(block, key, tag)
        if "quote" in block:
            return E("blockquote", None, self.runs(block["quote"]))
        if "code" in block:
            return E("pre", None, E("code", None, text_of(block["code"])))
        if "math" in block:
            return E("div", {"class": "formula"}, self.math(block["math"], True))
        if "table" in block:
            return self.table(block["table"])
        if "plan" in block:
            return self.plan_table(block["plan"])
        if "note" in block:
            address = github_url("issue", self.repo, issue)
            return E("p", {"class": "quiet"}, HTML_TABLE_NOTE, " ", E("a", {"href": address}, "Open it on GitHub")
                     if address else "")
        if "block" in block and store is not None:
            data = block["block"] if isinstance(block["block"], dict) else {}
            found = store.get((data.get("kind"), data.get("id")))
            if found:
                return self.table_block(found) if found.get("kind") == "table" else self.figure_block(found)
        return ""

    def list_block(self, block, key, tag):
        items = []
        for item in block[key] if isinstance(block[key], list) else []:
            item = item if isinstance(item, dict) else {}
            sub = item.get("sub") if isinstance(item.get("sub"), dict) else None
            items.append(E("li", None, self.runs(item.get("r")),
                           self.list_block(sub, "ul" if "ul" in sub else "ol", "ul" if "ul" in sub else "ol")
                           if sub and ("ul" in sub or "ol" in sub) else ""))
        start = integer(block.get("start"), 1) if tag == "ol" else 1
        return E(tag, {"class": "plainlist", "start": start if start > 1 else None}, *items)

    def first_runs(self, doc):
        """The runs of the first paragraph of a doc (a meaning, an opening)."""
        for block in (doc or {}).get("blocks", []) if isinstance(doc, dict) else []:
            if isinstance(block, dict) and "p" in block:
                return block["p"]
        return []

    def sentence(self, memory, tag="span"):
        """A memory's sentence: the fixed lead, the record's words with the project words marked, the page's stop."""
        told = memory.get("sentence") or {}
        words, quoted = text_of(told.get("words")), told.get("quoted") is True
        out, at = [text_of(told.get("lead"))], 0
        if quoted:
            out.append("“")
        for mark in told.get("marks", []) if isinstance(told.get("marks"), list) else []:
            if not (isinstance(mark, list) and len(mark) == 3 and all(integer(v, -1) >= 0 for v in mark)):
                continue
            start, end, n = mark
            if start < at or end <= start or end > len(words):
                continue
            out += [words[at:start], self.term(words[start:end], n, tag)]
            at = end
        out.append(words[at:])
        out.append(".”" if quoted else ".")
        return J(*out)

    def said(self, memory, cls="said", text=None):
        """A memory as a sentence that opens the memory when chosen."""
        mid = integer(memory.get("id"))
        return E("a", {"class": cls, "href": anchor("m-%d" % mid), "data-m": mid},
                 text if text is not None else self.sentence(memory))

    def said_id(self, mid, cls="inl"):
        memory = self.m.get(integer(mid))
        return self.said(memory, cls) if memory else mind_model.PHRASES["not told"]

    def thread_link(self, n):
        thread = self.threads.get(integer(n))
        if not thread:
            return mind_model.PHRASES["thread.unnamed"]
        return E("a", {"href": anchor("thread-%d" % integer(n))}, text_of(thread.get("name")))

    def thread_name(self, n):
        thread = self.threads.get(integer(n))
        return text_of(thread.get("name")) if thread else mind_model.PHRASES["thread.unnamed"]

    def section_link(self, n, label):
        return E("a", {"href": anchor("sec-%d" % integer(n))}, text_of(label))

    # ------------------------------------------------------------ pieces

    @staticmethod
    def explain_button(key, text, cls):
        """A word that explains itself when chosen: a button whose key names a fixed explanation."""
        if key in EXPLAIN_TEXT or key == "how":
            return E("button", {"type": "button", "class": cls, "data-explain": key}, text)
        return E("span", {"class": cls}, text)

    @staticmethod
    def mark(memory_or_kind):
        kind = memory_or_kind if isinstance(memory_or_kind, str) else memory_or_kind.get("kind")
        return E("span", {"class": "mk " + KIND_CLASS.get(kind, "k-other"), "aria-hidden": "true"})

    def chip(self, memory, thread=False):
        words = text_of(memory.get("latest")) + ("" if thread else text_of(memory.get("latest_note"))) \
            or text_of(memory.get("chip"))
        if not words:
            return ""
        return self.explain_button(text_of(memory.get("explain")), words, "chip " + state_class(memory))

    def meta(self, memory, thread=False, date=False):
        """A card's first line: the kind, where, the day and the state. A latest decision or result says so in its
        chip; on a page about the whole project it names its thread, as a link, after the chip."""
        latest, note = text_of(memory.get("latest")), text_of(memory.get("latest_note"))
        tail = E("span", {"class": "in"}, " in ", self.thread_link(memory.get("thread")), note) \
            if latest and thread else ""
        return E("div", {"class": "meta"}, self.mark(memory),
                 self.explain_button("kind-" + text_of(memory.get("kind")), KIND_WORD.get(memory.get("kind"), "Noted"),
                                     "kw"),
                 E("span", {"class": "in"}, "in ", self.thread_link(memory.get("thread"))) if thread and not latest
                 else "", E("span", {"class": "date"}, text_of(memory.get("day"))) if date else "",
                 self.chip(memory, thread), tail)

    def card(self, memory, thread=True):
        return E("div", {"class": "card %s %s" % (kind_class(memory), state_class(memory))},
                 self.meta(memory, thread=thread), self.said(memory))

    def view(self, ident, title, lede, *children):
        local = []
        if ident in ("todo", "open"):
            local = [("work", "Tasks"), ("open", "Open questions and limits")]
        elif ident in ("history", "settled", "threads", "lessons", "map"):
            local = [("history", "Timeline"), ("settled", "Current understanding"), ("threads", "Topic stories"),
                     ("lessons", "Lessons"), ("map", "Memory maps")]
        elif ident == "text" and self.sheet is None:
            local = [("outputs", "Current text and sources")]
        alias = {"todo": "work", "text": "outputs"}.get(ident)
        return E("section", {"class": "view", "id": ident, "aria-labelledby": ident + "-h"},
                 E("span", {"id": alias}) if alias else "",
                 E("nav", {"class": "area-links noprint", "aria-label": "Within this area"},
                   *[E("a", {"href": anchor(key), "class": "here" if key == (alias or ident) else ""}, label)
                     for key, label in local]) if local else "",
                 E("h2", {"id": ident + "-h"}, title), E("p", {"class": "lede"}, lede) if lede else "", *children)

    @staticmethod
    def show(summary, *children):
        return E("details", {"class": "ev"}, E("summary", None, summary), E("div", {"class": "evbody"}, *children))

    @staticmethod
    def made_from(block, cls="quiet"):
        if not text_of(block.get("data")):
            return ""
        return E("p", {"class": cls}, "Made from ", E("code", None, block["data"]),
                 J(" with ", E("code", None, text_of(block.get("command")))) if text_of(block.get("command")) else "",
                 ".")

    def table_block(self, block):
        """A table block drawn open where the author put it."""
        return E("div", {"class": "blockfig"}, E("p", {"class": "evtitle"}, text_of(block.get("title"))),
                 E("p", None, text_of(block.get("caption"))), self.table(block.get("table")), self.made_from(block))

    def table_ref(self, block):
        place = block.get("place") if isinstance(block.get("place"), dict) else {}
        ordinal = place.get("ordinal")
        where = E("a", {"href": anchor("text-%d-%d" % (integer(place.get("thread")), ordinal))}, "See it in the text") \
            if isinstance(ordinal, int) and not isinstance(ordinal, bool) and integer(place.get("thread")) in self.threads \
            else ""
        return self.show("Show the table", E("p", {"class": "evtitle"}, text_of(block.get("title"))),
                         E("p", None, text_of(block.get("caption"))), self.table(block.get("table")),
                         self.made_from(block),
                         E("p", {"class": "quiet"}, "This is the table as it is in the current text. ", where))

    def picture_of(self, block):
        """The embedded picture of a figure block: an img built from bytes that mind.picture checked, else nothing."""
        key = (text_of(block.get("repo")) or self.repo, text_of(block.get("ref")), text_of(block.get("path")))
        made = picture(self.pictures.get(key))
        if made is None:
            return ""
        uri, width, height = made
        return E("img", {"src": uri, "alt": alt_text(block.get("title")), "width": width, "height": height,
                         "loading": "lazy", "class": "figpic"})

    def figure_body(self, block, embed_picture=True):
        address = github_url("picture", text_of(block.get("repo")) or self.repo, block.get("ref"), block.get("path"))
        return [E("p", {"class": "evtitle"}, text_of(block.get("title"))), E("p", None, text_of(block.get("caption"))),
                self.picture_of(block) if embed_picture else "",
                E("p", {"class": "quiet"}, "The picture is ", E("code", None, text_of(block.get("path"))),
                  " at ", text_of(block.get("ref")), ".") if text_of(block.get("path")) else "",
                self.made_from(block),
                E("a", {"href": address}, "Open the picture on GitHub")
                if address else E("p", {"class": "nothing"}, "This memory names a figure that this page cannot link to.")]

    def figure_block(self, block):
        return E("div", {"class": "blockfig"}, *self.figure_body(block))

    def figure_ref(self, block, embed_picture=True):
        return self.show("Show the figure" if embed_picture else "Figure source", *self.figure_body(block, embed_picture))

    def code_ref(self, item):
        first, last = integer(item.get("from")), integer(item.get("to"))
        address = github_url("lines", text_of(item.get("repo")), item.get("ref"), item.get("path"), first, last)
        span = "line %d" % first if first == last else "lines %d to %d" % (first, last)
        return self.show("Show the code", E("p", {"class": "evtitle"}, E("code", None, text_of(item.get("path"))),
                                            ", %s, as they were at the change " % span, text_of(item.get("ref"))[:12]),
                         E("a", {"href": address}, "Open these lines on GitHub")
                         if address else E("p", {"class": "nothing"}, "This memory names code that this page cannot "
                                                                       "link to."))

    def pull_ref(self, item):
        address = github_url("pull", text_of(item.get("repo")), item.get("number"))
        return self.show("Show the change that was merged", E("p", {"class": "evtitle"}, text_of(item.get("title"))),
                         E("a", {"href": address}, "Open the pull request on GitHub")
                         if address else E("p", {"class": "nothing"}, "This memory names a change that this page "
                                                                       "cannot link to."))

    def references(self, memory, embed_pictures=True):
        out = []
        for item in memory.get("evidence", []):
            kind = item.get("kind")
            if kind in ("table", "figure") and not item.get("found"):
                out.append(E("p", {"class": "quiet"}, (
                    "This memory pointed to a %s that is no longer in the current text." if item.get("shown") else
                    "This memory points to a %s in a text that this page does not show.") % kind))
            elif kind == "table":
                out.append(self.table_ref(item))
            elif kind == "figure":
                out.append(self.figure_ref(item, embed_pictures))
            elif kind == "code":
                out.append(self.code_ref(item))
            elif kind == "pull":
                out.append(self.pull_ref(item))
        return out

    # ------------------------------------------------------------ the opened memory

    def remind(self, memory):
        rows, remind = [], memory.get("remind") or {}
        thread = self.threads.get(integer(memory.get("thread"))) or {}
        opening = thread.get("opening")
        if opening:
            rows.append(E("div", {"class": "f"}, E("span", {"class": "lab"}, "What this thread is about"),
                          E("p", None, "This thread is about ", self.runs(self.first_runs(opening.get("about")),
                                                                           "button"), "."),
                          self.blocks(opening.get("why"))))
        pause = remind.get("pause")
        if isinstance(pause, int) and not isinstance(pause, bool) and 0 <= pause < len(self.pauses):
            before = self.pauses[pause]
            rows.append(E("div", {"class": "f"}, E("span", {"class": "lab"}, "How things stood at the pause before, on "
                                                   + text_of(before.get("day"))),
                          E("p", None, text_of(before.get("focus")))))
        if integer(remind.get("root")) in self.m:
            rows.append(E("div", {"class": "f"}, E("span", {"class": "lab"}, "Where this line of thought started"),
                          E("p", None, self.said_id(remind["root"]))))
        before = [mid for mid in remind.get("before", []) if integer(mid) in self.m]
        if before:
            rows.append(E("div", {"class": "f"}, E("span", {"class": "lab"}, "Just before this, in the same thread"),
                          *[E("p", None, self.said_id(mid)) for mid in before]))
        if not rows:
            return ""
        return E("details", {"class": "remind"}, E("summary", None, "Remind me what was going on"), *rows)

    def field(self, entry, cut_to=None):
        if entry.get("type") == "people":
            body = E("p", {"class": "people"}, text_of(entry.get("people")))
        else:
            body = self.blocks(entry.get("doc"), cut_to)
        return E("div", {"class": "f"}, E("span", {"class": "lab"}, text_of(entry.get("name"))), body)

    def links(self, title, pairs):
        items = []
        for pair in pairs:
            other = self.m.get(integer(pair.get("id")))
            items.append(E("li", None, E("span", {"class": "ph " + LINK_CLASS.get(pair.get("kind"), "l-because")},
                                         text_of(pair.get("phrase"))),
                           self.said(other, cls="inl") if other else mind_model.PHRASES["not told"]))
        if not items:
            return ""
        return E("div", {"class": "f"}, E("span", {"class": "lab"}, title), E("ul", {"class": "rel"}, *items))

    def detail(self, memory):
        mid = integer(memory.get("id"))
        written = memory.get("written") or {}
        who = text_of(memory.get("author")) or mind_model.PHRASES["account"]
        when = text_of(written.get("day"))
        if text_of(written.get("time")):
            when = "%s on %s" % (written["time"], when)
        address = github_url("memory", self.repo, memory.get("issue"), mid)
        refs = self.references(memory)
        group = memory.get("group") if memory.get("group") in GROUPS else "stands"
        return E("article", {"class": "mem %s %s" % (kind_class(memory), state_class(memory)), "id": "m-%d" % mid,
                             "data-kind": kind_class(memory)[2:], "data-group": group, "tabindex": "-1"},
                 self.meta(memory, date=True),
                 E("p", {"class": "big"}, self.sentence(memory, "button")),
                 self.links("Later recorded change", [p for p in memory.get("in", []) if p.get("kind") in ("replaces", "instead")])
                 if memory.get("css") in ("replaced", "aside") else "",
                 self.remind(memory),
                 E("p", {"class": "quiet plannote"}, {"bore": "This bore out a planned claim.",
                                                     "against": "This spoke against a planned claim."}[memory["plan_note"]])
                 if memory.get("plan_note") in ("bore", "against") else "",
                 E("p", {"class": "ans"}, text_of(memory.get("passage"))),
                 E("p", {"class": "inthread"}, "Part of the thread ", self.thread_link(memory.get("thread")), "."),
                 *[self.field(entry, address) for entry in memory.get("fields", [])],
                 self.links("What led here", memory.get("out", [])),
                 self.links("What came of it", memory.get("in", [])),
                 E("div", {"class": "f refs"}, E("span", {"class": "lab"}, "If you want to see for yourself"), *refs)
                 if refs else "",
                 E("p", {"class": "quiet oldnote"}, OLD_NOTE) if memory.get("old") is True else "",
                 E("p", {"class": "quiet changednote"}, CHANGED_NOTE) if memory.get("changed") is True else "",
                 E("details", {"class": "where"}, E("summary", None, "Where this was said"),
                   *[self.field(entry) for entry in memory.get("where", [])],
                   E("p", None, "Recorded by ", E("span", {"class": "people"}, who), ". Event stamp: ", when,
                     ". Posted to the record: ", self.history_stamp(memory.get("posted")), ".",
                     " Reconstructed later." if memory.get("reconstructed") else ""),
                   E("a", {"href": address}, "Open the original comment on GitHub")
                   if address else "",
                   " " if address else "", self.record_link(integer(memory.get("issue")), integer(memory.get("id")))))

    # ------------------------------------------------------------ the views

    def untold_line(self):
        """The last line of every view but the first: how many things the page does not tell, and where it says why."""
        count = integer((self.model.get("not_told") or {}).get("count"))
        if not count:
            return ""
        return E("p", {"class": "quiet untoldline"}, plural(count, "1 thing is not told on this page. ",
                                                            "%d things are not told on this page. "),
                 E("a", {"href": anchor("untold")}, "See why"))

    @staticmethod
    def summary_block(summary):
        """The newest "In short" (the first field of the resume block), as text and marked with its day; one fixed line when
        the record holds none."""
        paras = [text_of(p) for p in summary.get("paras", []) if text_of(p)] if isinstance(summary, dict) \
            and isinstance(summary.get("paras"), list) else []
        if not paras:
            return E("p", {"class": "quiet", "id": "in-short"}, mind_model.PHRASES["short.none"])
        return E("section", {"class": "summary", "id": "in-short"}, E("h3", None, mind_model.PHRASES["short.title"]),
                 *[E("p", None, words) for words in paras],
                 E("p", {"class": "quiet"}, mind_model.PHRASES["short.asof"] % text_of(summary.get("asof"))))

    def task_data(self):
        tasks = self.model.get("tasks")
        return tasks if isinstance(tasks, dict) else {}

    def resume_work_link(self, row, automatic):
        tasks = self.task_data()
        if not isinstance(row, dict) or tasks.get("available") is False:
            return ""
        refs = {integer(n) for n in row.get("task_refs", []) if number(integer(n), ISSUE_NUMBER)} \
            if isinstance(row.get("task_refs"), list) else set()
        opened = tasks.get("open") if isinstance(tasks.get("open"), list) else []
        closed = tasks.get("closed") if isinstance(tasks.get("closed"), list) else []
        matches = [task for task in opened + closed if isinstance(task, dict) and integer(task.get("issue")) in refs]
        if len(matches) != 1:
            return ""
        task = matches[0]
        target = row.get("work_step")
        checked = self.step_id(target.get("issue"), target) if isinstance(target, dict) else None
        for step in task.get("steps", []) if task in opened and isinstance(task.get("steps"), list) else []:
            if not isinstance(step, dict) or integer(step.get("n")) < 1 or integer(step.get("comment")) < 1 \
                    or integer(step.get("ordinal")) < 1:
                continue
            ident = self.step_id(task.get("issue"), step)
            if "work_step" in row:
                if ident == checked:
                    return E("a", {"class": "lk", "href": anchor(ident)}, "Review step")
            elif automatic:  # Older models only carried the observer's short action and its issue reference.
                words = text_of(step.get("text"))
                if words and len(words) <= 120 and text_of(row.get("action_text")) == \
                        "Step %d: %s" % (integer(step.get("n")), words.replace("|", "/")):
                    return E("a", {"class": "lk", "href": anchor(ident)}, "Review step")
        return E("a", {"class": "lk", "href": anchor("work-task-%d" % integer(task.get("issue")))}, "Open task")

    def handoff_update(self, row):
        completion, target = row.get("completion"), row.get("work_step")
        if not isinstance(completion, dict) or not isinstance(target, dict):
            return ""
        memory = self.m.get(integer(completion.get("source")))
        if not memory or memory.get("kind") not in record_format.MARK_RULES["done"]:
            return ""
        kind = memory["kind"]
        address = github_url("memory", self.repo, memory.get("issue"), memory.get("id"))
        author = text_of(memory.get("author"))
        evidence = self.show("Read " + kind,
                             E("p", {"class": "quiet"}, "Recorded ", self.history_stamp(memory.get("posted")),
                               " by " + author if author else "", "."),
                             *[self.field(entry, address) for entry in memory.get("fields", [])],
                             *self.references(memory, embed_pictures=False),
                             *[self.field(entry, address) for entry in memory.get("where", [])],
                             E("a", {"href": address}, "Original " + kind + " on GitHub") if address else "")
        following = completion.get("next_step")
        if completion.get("task_closed") is True:
            next_line = E("p", {"class": "quiet"}, "Task closed.")
        elif isinstance(following, dict):
            ident = self.step_id(target.get("issue"), following)
            words = text_of(following.get("text"))
            label = "Next step is blocked" if following.get("state") == "blocked" else "Next open step"
            next_line = E("p", {"class": "handoff-follow"}, E("span", {"class": "lab"}, label),
                          E("a", {"href": anchor(ident)}, words) if ident else words)
        else:
            next_line = E("p", {"class": "quiet"}, "No open steps in this task.")
        return E("div", {"class": "handoff-update"}, E("b", {"class": "handoff-state"}, "Completed since handoff"),
                 E("p", {"class": "handoff-outcome"}, self.sentence(memory)), evidence, next_line)

    def handoff_row(self, row, automatic):
        update = self.handoff_update(row)
        action = J(self.runs(row.get("action")), J(" (", self.runs(row.get("where")), ")") if row.get("where") else "")
        owner = text_of(row.get("owner"))
        if update:
            return E("li", {"class": "handoff-completed"}, E("div", None, action),
                     E("span", {"class": "who people"}, "Owner: ", owner) if owner else "", update)
        return E("li", None, E("span", {"class": "who people"}, owner),
                 E("span", None, action, " ", self.resume_work_link(row, automatic)))

    IMPACT_VERDICTS = {"still_correct": "Still correct", "unrelated": "Unrelated", "defer": "Deferred",
                       "needs_update": "Update accepted", "applying": "Verification pending", "applied": "Update verified"}

    @staticmethod
    def pending_impact(item):
        return (item.get("review") or {}).get("verdict") in (None, "needs_update", "applying")

    def record_address(self, url):
        """Rebuild a same-project citation from issue and comment identifiers."""
        parts = mind_model.mind_text.github_parts(url) or {}
        if parts.get("repo") == self.repo and parts.get("kind") in ("memory", "issue"):
            return self.address(parts)
        return None

    def decision_line(self, label, entry):
        mid = integer(entry.get("id"))
        address = github_url("memory", self.repo, entry.get("issue"), mid)
        words = text_of(entry.get("text"))
        if mid in self.m:
            link = self.said(self.m[mid], "inl", words)
        else:
            link = E("a", {"href": address}, words) if address else words
        return E("p", {"class": "impact-decision"}, E("span", {"class": "lab"}, label), link)

    @staticmethod
    def guidance_prompts(subject):
        prompts = []
        for title, question in (("Help me decide", "Help me decide what to do"),
                                ("What am I missing?", "What am I missing")):
            words = "/recortex:guide %s about %s?" % (question, subject)
            prompts.append(E("details", {"class": "impact-prompt"}, E("summary", None, title),
                             E("code", None, words),
                             E("button", {"type": "button", "class": "impact-copy"}, "Copy question"),
                             E("span", {"class": "quiet", "aria-live": "polite"})))
        return prompts

    def impact_editor(self):
        if self.action_root:
            return self.action_root.rsplit("/", 1)[0] + "/impact-review"
        sample = self.model.get("sample_impact_editor")
        return sample + "-review.html" if sample in ("software", "research", "manuscript") else ""

    def impact_item(self, group, item, history):
        ident, revision = text_of(item.get("id")), text_of(item.get("revision"))
        if not all(re.fullmatch("[a-f0-9]{64}", value) for value in (ident, revision)):
            return ""
        old = integer(group["old"]["id"])
        label, quote = text_of(item.get("label")), text_of(item.get("quote"))
        address, target = self.record_address(item.get("url")), text_of(item.get("anchor"))
        jump = E("a", {"href": anchor(target)}, "Open in dashboard") if target and ANCHOR.fullmatch(target) else ""
        source = E("a", {"href": address}, "Original on GitHub") if address else ""
        review = item.get("review") or {}
        state = self.IMPACT_VERDICTS.get(review.get("verdict"), "")
        prompts = self.guidance_prompts("impact-%d, item %s in %s" % (old, ident, self.repo))
        editor = self.impact_editor()
        if editor:
            location = "%s#impact-%d-%s%s" % (editor, old, ident, "" if history else "-overview")
            review_link = E("a", {"class": "impact-review-link", "href": Href(location)},
                            "Review change" if not state else "Open review")
        else:
            review_link = E("details", {"class": "impact-prompt"}, E("summary", None, "Review change"),
                            E("p", None, "In the project chat, ask to review this item. You can inspect the exact change before saving."),
                            E("code", None, "/recortex:impact impact-%d %s" % (old, ident)),
                            E("button", {"type": "button", "class": "impact-copy"}, "Copy request"),
                            E("span", {"class": "quiet", "aria-live": "polite"}))
        return E("li", {"class": "impact-item"}, E("h5", None, label),
                 E("blockquote", None, quote) if quote else "",
                 E("p", None, text_of(item.get("reason"))),
                 E("p", {"class": "impact-sources"}, jump, source),
                 E("p", {"class": "quiet"}, state, " · ", text_of(review.get("reviewer"))) if state else "",
                 E("div", {"class": "impact-choices"}, review_link, *prompts))

    def impact_group(self, group, history=False):
        old = group["old"]
        current = group.get("successors") or []
        items = group.get("items") or []
        pending = [item for item in items if self.pending_impact(item)]
        verifications = group.get("pending_verifications", [])
        if not current or not (history or pending or verifications or group.get("conflict")):
            return ""
        ident = ("history-impact-" if history else "impact-") + str(integer(old.get("id")))
        entries = items if history else pending
        sections = []
        for kind, caption in (("direct", "Explicit connections"), ("suggested", "Suggested connections")):
            selected = [self.impact_item(group, item, history) for item in entries if item.get("relation") == kind]
            if selected:
                sections.append(E("div", {"class": "impact-section"}, E("h4", None, caption), E("ul", None, *selected)))
        for event in verifications:
            key, cid = text_of(event.get("target")), number(event.get("comment"), COMMENT_ID)
            if not re.fullmatch("[0-9a-f]{64}", key) or not cid:
                continue
            editor = self.impact_editor()
            target = (event.get("context") or {}).get("label") or "Earlier item"
            path = "impact-%d-%s-pending-%d" % (integer(old.get("id")), key, cid)
            receipt = github_url("memory", self.repo, group.get("hub"), cid)
            sections.append(E("div", {"class": "impact-section"}, E("h4", None, "Verification pending"),
                              E("p", None, target),
                              E("a", {"href": Href(editor + "#" + path + ("" if history else "-overview"))}, "Check pending change") if editor else
                              E("a", {"href": receipt}, "Read shared review") if receipt else ""))
        log = []
        if history:
            for event in group.get("review_history", []):
                state = self.IMPACT_VERDICTS.get(event.get("verdict"))
                if state:
                    key = event.get("target")
                    current_item = next((item for item in items if item["id"] == key), {})
                    name = (event.get("context") or {}).get("label") or current_item.get("label") or "Earlier item"
                    receipt = github_url("memory", self.repo, group.get("hub"), event.get("comment"))
                    log.append(E("li", None, E("b", None, name), " · ", state, " · ", text_of(event.get("reviewer")),
                                 " · ", self.history_stamp(event.get("at")),
                                 E("p", None, text_of(event.get("note"))) if event.get("note") else "",
                                 E("a", {"href": receipt}, "Read shared review") if receipt else ""))
        count = len(pending) + len(verifications)
        summary = plural(count, "1 item to review", "%d items to review") if count else "No items awaiting review"
        if group.get("conflict"):
            summary = "Choose which replacement governs"
        return E("details", {"class": "impact-group", "id": ident},
                 E("summary", None, E("span", {"class": "impact-count"}, "" if history else "Changed decision · ", summary), E("b", None, text_of(current[0].get("text")))
                   if len(current) == 1 else E("b", None, text_of(old.get("text")))),
                 E("div", {"class": "impact-body"}, self.decision_line("Earlier decision", old),
                   *[self.decision_line("Recorded replacement", source) for source in current],
                   E("p", {"class": "impact-notice"}, "Multiple recorded replacements are in force. Confirm which one governs this work in the project chat.")
                   if group.get("conflict") else "",
                   *sections, E("p", {"class": "quiet"}, "No current connections were found within the reviewed record.")
                   if not items and not verifications else "",
                   self.show("Review history", E("ul", None, *log)) if log else "",
                   E("a", {"href": anchor("history-impact-%d" % integer(old.get("id")))}, "See review history") if not history else ""))

    def impact_coverage(self):
        coverage = self.model.get("decision_impacts") or {}
        notes = [text_of(value) for value in coverage.get("coverage", []) if text_of(value)]
        if coverage.get("partial"):
            return E("p", {"class": "impact-notice"}, "Review covers part of the project record. ", " ".join(notes))
        return ""

    def overview_attention(self, last, opened):
        """Show at most three entries from the recorded handoff and unresolved task failures."""
        rows = []
        if last and last.get("holds"):
            source = github_url("memory", self.repo, last.get("issue"), last.get("id"))
            rows.append(E("li", None, E("span", {"class": "lab"}, "Hold from the last handoff"),
                          self.blocks(last["holds"], cut_to=source),
                          E("a", {"class": "lk", "href": source}, "Read the handoff on GitHub") if source else ""))
        blocked = [task for task in opened if task.get("blocked") is True]
        room = 3 - len(rows)
        for task in blocked[:room]:
            issue = integer(task["issue"])
            steps = task.get("steps") if isinstance(task.get("steps"), list) else []
            step = next((s for s in steps if isinstance(s, dict) and s.get("state") == "blocked"), {})
            target = self.step_id(issue, step) or "work-task-%d" % issue
            title = text_of(task.get("title")) or "Task #%d" % issue
            failure = task.get("blocking_failure") if isinstance(task.get("blocking_failure"), dict) else {}
            memory = self.m.get(integer(failure.get("id")))
            if memory and memory.get("kind") == "failure":
                evidence = self.said(memory, "lk", "Read the blocking failure")
            else:
                address = github_url("memory", self.repo, failure.get("issue"), failure.get("id"))
                evidence = E("a", {"class": "lk", "href": address}, "Read the failure on GitHub") if address else ""
            words = text_of(step.get("text"))
            rows.append(E("li", None, E("span", {"class": "lab"}, "Blocked step" if words else "Blocked task",
                                       " · ", title if words else "#%d" % issue),
                          E("a", {"class": "said", "href": anchor(target)}, words or title),
                          evidence))
        groups = (self.model.get("decision_impacts") or {}).get("groups", [])
        impacts = [panel for panel in (self.impact_group(group) for group in groups) if panel]
        if not rows and not impacts:
            return ""
        more = len(blocked) - room
        return E("section", {"class": "attention", "aria-labelledby": "attention-h"},
                 E("h3", {"id": "attention-h"}, "Needs your attention"), *impacts,
                 self.impact_coverage() if impacts else "", E("ul", None, *rows) if rows else "",
                 E("a", {"class": "lk", "href": anchor("work")},
                   plural(more, "See 1 more blocked task in Work", "See %d more blocked tasks in Work")) if more > 0 else "")

    def handoff_excerpt(self, line):
        """Show a checked project citation by name while keeping its address for the portable handoff."""
        if len(line) > 360:
            line = line[:359].rstrip() + "…"
        parts, start = [], 0
        for match in HANDOFF_LINK.finditer(line):
            written = match.group().rstrip(".,;:!?")
            address = self.record_address(written)
            if address and address == written:
                parts.extend([line[start:match.start()], E("a", {"href": address}, "cited record")])
                start = match.start() + len(written)
        return J(parts, line[start:])

    def handoff_brief(self, opened):
        """A portable excerpt of the trusted snapshot. Separate the dated handoff from later task evidence."""
        built = self.model.get("built") or {}
        last = self.model.get("last") or {}
        source = github_url("memory", self.repo, last.get("issue"), last.get("id"))
        notes = []
        if source:
            status = ["Recorded handoff · " + (text_of(last.get("day")) or "date unknown")]
            for key, label in (("focus", "Focus"), ("holds", "Holds")):
                field = last.get(key)
                if isinstance(field, dict) and field.get("blocks"):
                    words = " ".join(mind_versions.block_text(block) for block in field["blocks"] if isinstance(block, dict))
                    status.append(label + " — " + (words.strip() if words.strip() and not field.get("cut")
                                                   else "Read the full field in the original handoff."))
            actions = last.get("next") or []
            for row in actions[:3]:
                action = text_of(row.get("action_text")) or mind_versions.run_text(row.get("action"))
                if action:
                    status.append("Planned at handoff — " + action)
            if len(actions) > 3:
                status.append("More planned actions are in the original handoff.")
            status.append(E("a", {"href": source}, "Read the original handoff"))
            notes.append(status)
            for row in actions[:3]:
                memory = self.m.get(integer((row.get("completion") or {}).get("source")))
                if memory and memory.get("kind") in record_format.MARK_RULES["done"]:
                    receipt = github_url("memory", self.repo, memory.get("issue"), memory.get("id"))
                    if receipt:
                        notes.append(["Completed since handoff — " + text_of(memory.get("plain") or memory.get("title")),
                                      E("a", {"href": receipt}, "Read the completion record")])
        tasks = self.task_data()
        if tasks.get("available") is True and built.get("complete") is not False:
            current = []
            for task in opened:
                step = next((s for s in task.get("steps", []) if isinstance(s, dict)
                             and s.get("state") in ("open", "blocked")), None)
                if step and text_of(step.get("text")):
                    receipt = github_url("memory", self.repo, task["issue"], step.get("comment"))
                    if receipt:
                        label = "Blocked step" if step["state"] == "blocked" else "Open step"
                        title = text_of(task.get("title"))
                        current.append([label + (" · " + title if title else "") + " — " + step["text"],
                                        E("a", {"href": receipt}, "Read the recorded step")])
            notes.extend(current[:3])
            if len(current) > 3:
                notes.append(["More tasks with unfinished steps are listed in Work."])
        groups = (self.model.get("decision_impacts") or {}).get("groups", [])
        impacts = []
        for group in groups:
            pending = [item for item in group.get("items", []) if self.pending_impact(item)]
            waiting = group.get("pending_verifications", [])
            if group.get("conflict") or pending or waiting:
                replacements = group["successors"][:2]
                sources = [self.record_address(entry.get("url")) for entry in [group["old"]] + replacements]
                lines = ["Earlier decision — " + text_of(group["old"].get("text"))]
                lines.extend("Recorded replacement — " + text_of(entry.get("text")) for entry in replacements)
                if len(group["successors"]) > len(replacements):
                    lines.append("See History for additional replacements.")
                if group.get("conflict"):
                    lines.append("Next judgment — confirm which recorded replacement governs.")
                elif waiting:
                    event = waiting[0]
                    label = (event.get("context") or {}).get("label") or "the earlier item"
                    lines.append("Next step — verify the pending change for " + label + ".")
                    sources.append(github_url("memory", self.repo, group.get("hub"), event.get("comment")))
                else:
                    item = pending[0]
                    state = (item.get("review") or {}).get("verdict")
                    if state == "needs_update":
                        lines.append("Next step — reopen the accepted draft for " + text_of(item.get("label")) + ".")
                    elif state == "applying":
                        lines.append("Next step — verify the pending change for " + text_of(item.get("label")) + ".")
                    else:
                        lines.append("Next judgment — review " + text_of(item.get("label")) + ". " + text_of(item.get("reason")))
                    sources.append(self.record_address(item.get("url")))
                sources = list(dict.fromkeys(address for address in sources if address))
                lines.extend(E("a", {"href": address}, "Read decision review source %d" % number)
                             for number, address in enumerate(sources, 1))
                impacts.append(lines)
        notes.extend(impacts[:2])
        if len(impacts) > 2:
            notes.append(["Additional decision changes await review in Overview."])
        if not notes:
            return ""
        stamp = self.history_stamp(built.get("synced_at"))
        lines = ["Project handoff · " + self.repo, "Record last read · " + stamp]
        if built.get("complete") is False or tasks.get("available") is False:
            lines.append("Current task state is unavailable from this snapshot.")
        brief = ["\n".join(lines)]
        for note in notes:
            brief.append("\n\n")
            for pos, line in enumerate(note):
                if pos:
                    brief.append("\n")
                if isinstance(line, Safe):  # A source link built above; retain the whole address for copying.
                    brief.append(line)
                else:
                    for n, field_line in enumerate(line.splitlines()):
                        if n:
                            brief.append("\n")
                        brief.append(self.handoff_excerpt(field_line))
        return E("div", {"class": "handoff-share noprint"},
                 E("button", {"type": "button", "id": "handoff-copy"}, "Copy handoff"),
                 E("details", {"id": "handoff-preview"}, E("summary", None, "Read handoff text"),
                   E("pre", {"id": "handoff-text", "tabindex": "-1"}, brief),
                   E("pre", {"id": "handoff-manual-copy", "tabindex": "-1", "hidden": "hidden"})),
                 E("p", {"class": "quiet", "id": "handoff-copy-status", "aria-live": "polite"}))

    def now_view(self):
        last, out = self.model.get("last"), [self.snapshot_line()]
        out.append(self.summary_block(last.get("summary") if isinstance(last, dict) else None))
        tasks = self.task_data()
        opened = sorted((task for task in tasks.get("open", []) if isinstance(task, dict)
                         and number(integer(task.get("issue")), ISSUE_NUMBER)),
                        key=lambda task: (task.get("todo") is True, integer(task["issue"]))) if isinstance(tasks.get("open"), list) else []
        complete = (self.model.get("built") or {}).get("complete") is not False
        out.append(self.overview_attention(last, opened if complete and tasks.get("available") is True else []))
        if tasks.get("available") is False:
            out.append(E("p", {"class": "nothing"}, "Work could not be read from this snapshot. Sync the record and rebuild the page."))
        elif not last:
            first = next(((task, step) for task in opened
                          for step in (task.get("steps") if isinstance(task.get("steps"), list) else [])
                          if isinstance(step, dict) and step.get("state") in ("open", "blocked") and text_of(step.get("text"))), None)
            if first and not (first[0].get("blocked") and complete and tasks.get("available") is True):
                task, step = first
                target = self.step_id(task["issue"], step) or "work-task-%d" % task["issue"]
                receipt = github_url("memory", self.repo, task["issue"], step.get("comment"))
                guidance = self.guidance_prompts("the unfinished task step in %s at %s" % (self.repo, receipt)) if receipt else []
                out.append(E("section", {"class": "resume-step"}, E("h3", None, "First unfinished recorded step"),
                             E("a", {"class": "said", "href": anchor(target)}, text_of(step["text"])),
                             E("p", {"class": "quiet"}, "Blocked. " if step.get("state") == "blocked" else "",
                               "From the recorded task order. ", E("a", {"href": anchor("work")}, "See all work")),
                             E("div", {"class": "impact-choices"}, *guidance) if guidance else ""))
        if complete and tasks.get("available") is True and not any((self.m, self.threads, opened, tasks.get("closed"),
                (self.model.get("project_map") or {}).get("nodes"), (self.model.get("not_told") or {}).get("count"))):
            out.append(E("section", {"class": "resume-step"}, E("h3", None, "Start your project record"),
                         E("p", None, "For an existing project, say “Catch up the record” in Claude Code to review earlier work. ",
                           "For a new project, open the ", E("a", {"href": anchor("project-map")}, "Project Map"),
                           " to organize your first goals.")))
        if last:
            day = text_of(last.get("day"))
            source = github_url("memory", self.repo, last.get("issue"), last.get("id"))
            caption = "Status automatically refreshed on %s." % day if last.get("automatic") is True else \
                "We last paused on %s. This is how things stood:" % day if day else "Recorded status."
            out.append(E("p", {"class": "paused"}, caption, " ", E("a", {"href": source}, "Original status") if source else ""))
            out.append(E("blockquote", {"class": "focus"}, self.blocks(last.get("focus"))))
            steps = [self.handoff_row(row, last.get("automatic") is True) for row in last.get("next", [])]
            out.append(E("section", {"class": "handoff-next"}, E("h3", None, "What we meant to do next"),
                         E("ul", {"class": "next"}, *steps) if steps else
                         E("p", {"class": "quiet"}, "Nothing was written down for next.")))
            out.append(E("p", {"class": "quiet"}, "Automatic status: focus and holds were carried forward." if last.get("automatic") is True
                         else "From the latest recorded status, not your last visit."))
            out.append(self.handoff_brief(opened))
            if last.get("day"):
                since = [self.card(self.m[mid]) for mid in self.model.get("since", []) if mid in self.m]
                out.append(self.show("Since that pause: event sequence", E("div", {"class": "stack"}, *since) if since else
                                     E("p", {"class": "quiet"}, "No later events are included after this status time.")))
            additions = [self.card(self.m[mid]) for mid in self.model.get("recorded_since", [])[:5] if mid in self.m]
            if additions:
                out += [E("h3", None, "Added after that status was posted"), E("div", {"class": "stack"}, *additions),
                        E("p", {"class": "quiet"}, "Includes earlier events recorded later. ",
                          E("a", {"href": anchor("history")}, "Browse all included history"))]
        else:
            out.append(E("p", {"class": "nothing"}, PAUSE_UNTOLD if (self.model.get("not_told") or {}).get("pause")
                         is True else "No pause has been recorded yet."))
            out.append(self.handoff_brief(opened))
        out.append(E("nav", {"class": "area-links noprint", "aria-label": "Project areas"},
                     E("a", {"href": anchor("project-map")}, "Explore the project map"),
                     E("a", {"href": anchor("settled")}, "Current understanding"),
                     E("a", {"href": anchor("history")}, "Catch up"),
                     E("a", {"href": anchor("work")}, "All work"),
                     E("a", {"href": anchor("text")}, "Current topic text") if not self.model.get("outputs_available") and
                     (self.outline.get("deliverables") or self.outline.get("other")) else ""))
        latest = []
        for kind, label in (("decision", "The last thing we decided"), ("result", "The last thing we found"),
                            ("failure", "The last thing that went wrong"), ("question", "The last thing we asked")):
            mid = integer((self.model.get("latest_of") or {}).get(kind))
            if mid in self.m:
                latest.append(E("li", None, E("span", {"class": "lab"}, label), self.card(self.m[mid])))
        if latest:
            out += [E("h3", None, "The latest of each kind"), E("ul", {"class": "latest"}, *latest)]
        return self.view("now", "Resume work", None, *out, self.untold())

    def project_map_view(self):
        state = self.model.get("project_map") or {}
        nodes = {key: node for key, node in state.get("nodes", {}).items()
                 if isinstance(node, dict) and not node.get("deleted")}
        edit = E("a", {"class": "lk", "href": Href(self.action_root.rsplit("/", 1)[0] + "/map-editor")},
                 "Edit project map") if self.action_root else ""
        if not nodes:
            return self.view("project-map", "Project Map", None, E("p", {"class": "nothing"}, "No branches yet."),
                             E("p", None, "Use ", E("code", None, "/recortex:map"),
                               " to start with your goals or choose project files for Recortex to read."), edit)
        children = {}
        for ident, node in nodes.items():
            children.setdefault(node["parent"], []).append(ident)
        for group in children.values():
            group.sort(key=lambda key: (nodes[key]["title"].casefold(), key))
        # Build bottom-up without using the Python call stack for user-defined nesting depth.
        order = []
        pending = list(reversed(children.get(None, [])))
        while pending:
            ident = pending.pop()
            order.append(ident)
            pending.extend(reversed(children.get(ident, [])))
        rows, details = {}, []
        history = state.get("history", [])
        history_by_id = {}
        for event in reversed(history):
            for ident in event.get("affected", []):
                history_by_id.setdefault(ident, []).append(event)
        for ident in reversed(order):
            node, nested = nodes[ident], children.get(ident, [])
            dest = branch_anchor(ident)
            toggle = E("button", {"class": "branch-toggle", "type": "button", "aria-expanded": "true",
                                  "aria-controls": dest + "-children"},
                       E("span", {"aria-hidden": "true"}, "▸"), E("span", {"class": "branch-sr"}, "Branches under ", node["title"]))
            rows[ident] = E("li", {"class": "branch-row branch-parent" if nested else "branch-row"},
                            E("div", {"class": "branch-label"}, toggle if nested else E("span", {"class": "branch-dot"}),
                              E("a", {"href": anchor(dest)}, node["title"]),
                              E("span", {"class": "branch-count"}, str(len(nested))) if nested else ""),
                            E("ul", {"id": dest + "-children"}, [rows.pop(k) for k in nested]) if nested else "")
            parent = node["parent"]
            back = E("a", {"href": anchor(branch_anchor(parent))}, nodes[parent]["title"]) if parent in nodes else None
            related = [E("li", None, E("a", {"href": anchor(branch_anchor(k))}, nodes[k]["title"]))
                       for k in node.get("links", []) if k in nodes]
            evidence = [self.branch_evidence(ref) for ref in node.get("evidence", [])]
            details.append(E("article", {"class": "branch-detail", "id": dest, "tabindex": "-1"},
                             E("p", {"class": "branch-path"}, E("a", {"href": anchor("project-map")}, "Project"),
                               J(" / ", back) if back else ""),
                             E("h3", {"class": "branch-title"}, node["title"]),
                             E("p", {"class": "branch-description"}, node["description"]) if node.get("description") else "",
                             E("h4", None, "Evidence"), E("ul", None, evidence) if evidence else
                             E("p", {"class": "quiet"}, "No evidence links recorded."),
                             J(E("h4", None, "Related branches"), E("ul", None, related)) if related else "",
                             self.branch_history(history_by_id.get(ident, [])[:6], nodes, selected=ident),
                             E("a", {"href": Href(self.action_root.rsplit("/", 1)[0] + "/map-editor#" + dest)},
                               "Edit this branch") if self.action_root else ""))
        roots = children.get(None, [])
        home = E("article", {"class": "branch-detail branch-home"}, E("p", {"class": "branch-path"}, "Project"),
                 E("h3", {"class": "branch-title"}, self.model.get("name") or "This project"),
                 E("p", {"class": "quiet"}, "%d main %s · %d %s in total" % (
                     len(roots), "branch" if len(roots) == 1 else "branches", len(order),
                     "branch" if len(order) == 1 else "branches")),
                 self.branch_history(list(reversed(history[-6:])), nodes, show_branch=True), edit)
        outline = E("nav", {"class": "branch-outline", "aria-label": "Project branches"},
                    E("a", {"class": "branch-root", "href": anchor("project-map")}, self.model.get("name") or "Project"),
                    E("ul", {"class": "branch-tree"}, [rows[k] for k in roots]))
        demo = ""
        if self.model.get("sample_map_edits") is True:
            demo = E("details", {"class": "branch-demo"}, E("summary", None, "Try renaming a sample branch"),
                     E("p", {"class": "quiet"}, "Choose a branch, enter a new name, and preview the change. "
                       "This example keeps edits only until you reload."),
                     E("label", None, "New branch name", E("input", {"id": "branch-new-name", "type": "text"})),
                     E("button", {"type": "button", "id": "branch-try"}, "Preview rename"),
                     E("p", {"id": "branch-sample-status", "role": "status"}),
                     E("button", {"type": "button", "id": "branch-apply", "hidden": "hidden"}, "Apply to sample"))
        return self.view("project-map", "Project Map", "Explore the branches of this project and the evidence behind them.",
                         E("div", {"class": "branch-tools"}, E("label", {"class": "branch-search"},
                           E("span", {"class": "lab"}, "Find a branch"),
                           E("input", {"id": "branch-search", "type": "search", "aria-label": "Find a branch"})),
                           E("div", {"class": "branch-modes", "role": "group", "aria-label": "Project branches"},
                             E("button", {"type": "button", "id": "branch-diagram"}, "Branch map"),
                             E("button", {"type": "button", "id": "branch-list"}, "Outline")),
                           E("p", {"id": "branch-matches", "class": "quiet", "role": "status"})),
                         E("div", {"class": "branch-layout"}, outline, E("div", {"class": "branch-inspector"},
                           home, list(reversed(details)), demo)))

    def branch_evidence(self, ref):
        kind, _, value = ref.partition(":")
        if kind == "comment" and value.isascii() and value.isdigit() and len(value) < 20:
            memory = self.m.get(int(value))
            if memory:
                return E("li", None, self.said(memory, "inl"), E("span", {"class": "lab"}, memory.get("day") or ""))
        if kind == "issue" and number(value, ISSUE_NUMBER):
            n = int(value)
            opened = self.task_data().get("open")
            task = next((t for t in opened if isinstance(t, dict) and t.get("issue") == n), None) \
                if isinstance(opened, list) else None
            if task:
                return E("li", None, E("a", {"href": anchor("work-task-%d" % n)}, task.get("title") or "Task #%d" % n))
            return E("li", None, E("a", {"href": github_url("issue", self.repo, n)}, "Issue #%d" % n))
        return E("li", None, E("code", None, value) if kind == "file" else ref)

    @staticmethod
    def branch_history(events, nodes, show_branch=False, selected=None):
        if not events:
            return ""
        verbs = {"add": "Added", "rename": "Renamed", "move": "Moved", "update": "Updated", "delete": "Removed",
                 "restore": "Restored", "merge": "Merged", "split": "Split", "seed": "Started the map with"}
        entries = []
        for event in events:
            ident = event.get("node")
            title = event.get("title") or nodes.get(ident, {}).get("title") or "branch"
            name = E("a", {"href": anchor(branch_anchor(ident))}, title) if ident in nodes else title
            action = event.get("action")
            detail = J(" ", name) if show_branch or ident != selected else ""
            if action == "rename" and event.get("before_title"):
                detail = J(" ", event["before_title"], " to ", name)
            elif action == "move" and event.get("origin"):
                detail = J(detail, " from ", event["origin"], " to ", event["destination"])
            elif action == "merge" and event.get("destination"):
                detail = J(detail, " into ", event["destination"])
            elif action == "seed":
                count = event.get("count")
                detail = J(" ", "%d branch%s" % (count, "" if count == 1 else "es")) \
                    if type(count) is int and 0 < count <= 200 else J(" ", "a first set of branches")
            entries.append(E("li", None, verbs.get(action, "Changed"), detail,
                             E("span", {"class": "lab"}, (event.get("at") or "")[:10], " · ",
                               "Observer via " if event.get("source") == "observer" else "", event.get("author") or "")))
        return J(E("h4", None, "Recent changes"), E("ul", {"class": "branch-history"}, entries))

    def snapshot_line(self):
        built = self.model.get("built") or {}
        synced = text_of(built.get("synced_at"))
        return E("p", {"class": "snapshot quiet"}, "Snapshot built ", text_of(built.get("words")) or "on an unknown day",
                 ". Record last read ", synced if index.ISO_UTC.fullmatch(synced) else "at an unknown time",
                 ". ", "Incomplete read. " if built.get("complete") is False else "",
                 "Local interactive session." if self.action_root else "Live view." if self.live else "Static snapshot.")

    @staticmethod
    def history_stamp(value):
        return value.replace("T", " ").replace("Z", " UTC") if isinstance(value, str) and index.ISO_UTC.fullmatch(value) else "Time unknown"

    @staticmethod
    def history_select(ident, label, choices):
        return E("label", None, E("span", {"class": "lab"}, label), E("select", {"id": ident},
                 *[E("option", {"value": key}, words) for key, words in choices]))

    def history_view(self):
        rows = []
        ordered = sorted(self.m.values(), key=lambda m: (m.get("posted") or "", integer(m.get("id"))), reverse=True)
        since = set(self.model.get("recorded_since", []))
        boundary = (self.model.get("last") or {}).get("posted")
        kinds = (("all", "All event kinds"), ("decision", "Decisions"), ("result", "Findings"), ("failure", "Failures"),
                 ("correction", "Corrections"), ("question", "Questions"), ("idea", "Ideas"), ("checkpoint", "Checkpoints"),
                 ("text", "Text changes"))
        states = (("all", "All states"), ("stands", "Still stands"), ("open", "Still open"), ("replaced", "Replaced"),
                  ("aside", "Set aside"), ("answered", "Answered"), ("tried", "Tried"), ("taken", "Taken up"),
                  ("led", "Led to a change"), ("pause", "Checkpoints"))
        topics = [("all", "All topics")] + [(n, text_of(t.get("name"))) for n, t in self.threads.items()]
        for memory in ordered:
            mid = integer(memory.get("id"))
            event = memory.get("event_time") or ""
            posted = memory.get("posted") or ""
            event = event if index.ISO_UTC.fullmatch(event) else ""
            posted = posted if index.ISO_UTC.fullmatch(posted) else ""
            dates = [E("span", {"class": "h-posted"}, "Recorded ", self.history_stamp(posted)),
                     E("span", {"class": "h-event"}, "Event ", self.history_stamp(event))]
            if memory.get("time_basis") == "posting":
                dates.append(E("span", {"class": "quiet"}, "Event time unavailable; ordered by posting time."))
            if memory.get("reconstructed"):
                dates.append(E("span", {"class": "quiet"}, "Reconstructed later"))
            rows.append(E("li", {"class": "history-row " + kind_class(memory), "id": "history-%d" % mid,
                                  "data-kind": kind_class(memory)[2:],
                                  "data-state": state_class(memory)[2:], "data-thread": integer(memory.get("thread")),
                                  "data-event": event, "data-posted": posted, "data-catchup": "yes" if mid in since else "no"},
                          E("div", {"class": "history-date"}, *dates),
                          E("div", {"class": "history-content"}, self.meta(memory, thread=True), self.said(memory))))
        controls = E("fieldset", {"class": "history-controls noprint"}, E("legend", None, "Filter history"),
                     self.history_select("history-order", "Order", (("newest", "Newest first"), ("oldest", "Oldest first"))),
                     self.history_select("history-topic", "Topic", topics),
                     self.history_select("history-kind", "Event kind", kinds),
                     self.history_select("history-state", "Recorded state", states),
                     self.history_select("history-scope", "Range", (("all", "All included history"),) +
                                         ((("since", "After the latest status"),) if boundary else ())),
                     E("label", None, E("span", {"class": "lab"}, "Words"),
                       E("input", {"id": "history-query", "type": "search", "aria-label": "Find words in history"})),
                     E("label", None, E("span", {"class": "lab"}, "From (UTC)"),
                       E("input", {"id": "history-from", "type": "date", "aria-label": "Start date"})),
                     E("label", None, E("span", {"class": "lab"}, "Through (UTC)"),
                       E("input", {"id": "history-to", "type": "date", "aria-label": "End date"})),
                     E("button", {"id": "history-reset", "type": "button"}, "Reset filters"))
        groups = (self.model.get("decision_impacts") or {}).get("groups", [])
        return self.view("history", "History", "Follow the record, then open a memory to see its evidence and what changed.",
                         E("section", {"class": "impact-history"}, E("h3", None, "Changed decisions"),
                           *[self.impact_group(group, history=True) for group in groups]) if groups else "",
                         self.impact_coverage(),
                         E("div", {"class": "history-tools noprint"},
                           self.history_select("history-clock", "Read by", (("posted", "Recently recorded"), ("event", "Event sequence"))),
                           E("details", {"class": "history-filters"}, E("summary", {"id": "history-filter-label"}, "Filters"), controls,
                             E("p", {"class": "quiet", "id": "history-boundary"},
                               "Latest status posted " + self.history_stamp(boundary) + ". This boundary changes when a new status is posted."
                               if boundary else "No checked status boundary is available. All included history is shown."))),
                         E("p", {"id": "history-count", "aria-live": "polite"}, "%d included memories" % len(rows)),
                         E("p", {"class": "quiet", "id": "history-selected", "hidden": "hidden"}),
                         E("ol", {"class": "history-list", "id": "history-list"}, *rows),
                         E("p", {"id": "history-empty", "hidden": "hidden" if rows else None}, "No memories are included yet. "
                           "The record may be empty or its memories unavailable."), self.untold_line())

    # ------------------------------------------------------------ the text

    OPEN_GROUPS = ("questions", "ideas", "failures", "unknown", "plan-unbacked", "plan-doubt", "plan-unused", "holds", "tasks")

    def sentences(self, items):
        """The sentences of a part's status line, each a list of segments, as plain nodes."""
        out = []
        for sentence in items if isinstance(items, list) else []:
            for seg in sentence.get("segs", []) if isinstance(sentence, dict) and isinstance(sentence.get("segs"), list) \
                    else []:
                if not isinstance(seg, dict):
                    continue
                words = text_of(seg.get("t"))
                if "gh" in seg:
                    out.append(self.plan_link(seg, words))
                elif "thread" in seg and integer(seg["thread"]) in self.threads:
                    out.append(self.thread_link(seg["thread"]))
                elif "m" in seg and integer(seg["m"]) in self.m:
                    out.append(E("a", {"class": "inl", "href": anchor("m-%d" % integer(seg["m"])),
                                       "data-m": integer(seg["m"])}, words))
                elif "open" in seg and seg["open"] in self.OPEN_GROUPS:
                    out.append(E("a", {"href": anchor("open-%s" % seg["open"])}, words))
                else:
                    out.append(words)
            out.append(" ")
        return out

    # ------------------------------------------------------------ the plan and the history of a part

    def plan_link(self, seg, words=None):
        """A planned part as a sentence that opens its comment on GitHub: the address is built from checked numbers."""
        words = words if words is not None else text_of(seg.get("t"))
        address = github_url("memory", self.repo, seg.get("issue"), seg.get("gh"))
        return E("a", {"class": "inl", "href": address}, words) if address else words

    def plan_cell(self, segs):
        out = []
        for seg in segs if isinstance(segs, list) else []:
            if not isinstance(seg, dict):
                continue
            words = text_of(seg.get("t"))
            if "gh" in seg:
                out.append(self.plan_link(seg, words))
            elif "m" in seg and integer(seg["m"]) in self.m:
                out.append(E("a", {"class": "inl", "href": anchor("m-%d" % integer(seg["m"])), "data-m": integer(seg["m"])},
                             words))
            else:
                out.append(words)
        return J(*out)

    def plan_table(self, data):
        """"What we plan to say here": one row per planned part, three cells, each a sentence."""
        rows = [row for row in (data.get("rows") if isinstance(data, dict) else []) or [] if isinstance(row, dict)]
        head = mind_model.PHRASES["plan.table.head"]
        body = []
        for row in rows:
            cells = row.get("cells") if isinstance(row.get("cells"), list) else []
            body.append(E("tr", {"class": "p-" + (row.get("state") if row.get("state") in STATE_CLASS else "plain")},
                          *[E("td", None, self.plan_cell(cells[i] if i < len(cells) else [])) for i in range(3)]))
        return E("div", {"class": "plantable"}, E("table", None, E("thead", None, E("tr", None, *[E("th", None, w) for w in head])),
                                                   E("tbody", None, *body)))

    def plan_summary(self):
        """"The plan against the text": the message, then one row per section with its counts, each a link."""
        plan = self.model.get("plan")
        if not isinstance(plan, dict):
            return ""
        number = {}
        for group in (self.model.get("outline") or {}).get("deliverables", []):
            for section in group.get("sections", []) if isinstance(group, dict) else []:
                if isinstance(section, dict):
                    number.setdefault(text_of(section.get("label")).lower(), integer(section.get("n")))
        rows = []
        for entry in plan.get("summary", []) if isinstance(plan.get("summary"), list) else []:
            if not isinstance(entry, dict):
                continue
            label = text_of(entry.get("section"))
            n = number.get(label.lower())
            words = []
            if integer(entry.get("planned")):
                words.append(plural(integer(entry.get("planned")), "1 planned part", "%d planned parts"))
                words.append("%d backed" % integer(entry.get("backed")))
                if integer(entry.get("doubt")):
                    words.append(plural(integer(entry.get("doubt")), "1 with a finding against it or resting on something "
                                        "that changed", "%d with a finding against them or resting on something that changed"))
            else:
                words.append("nothing planned for it")
            words.append("text written" if entry.get("has_text") is True else "no text yet")
            rows.append(E("li", None, E("a", {"href": anchor("sec-%d" % n)}, label) if n else label, ": " + "; ".join(words) + "."))
        message = plan.get("message") if isinstance(plan.get("message"), dict) else None
        return E("section", {"class": "plansum", "id": "plan-against-text"},
                 E("h3", None, mind_model.PHRASES["plan.summary"]),
                 E("p", None, E("span", {"class": "lab"}, mind_model.PHRASES["plan.message"] + ": "),
                   self.plan_link({"gh": message.get("id"), "issue": message.get("issue")}, text_of(message.get("plain")))
                   if message else "not planned yet", "."),
                 E("ul", {"class": "plansummary"}, *rows) if rows else "")

    def history_items(self, part, restore=None):
        """(kept, entries, items) of a part's History, newest version first: one list item for each version, with its day, how it
        came about, the reason and the names it carries; the difference is drawn by Page.diff() (mind_versions) when the model
        holds one. With `restore` (the heading of the part's section) each version is headed by its number in the paper's
        versions and also carries its own request for the chat."""
        data = part.get("history") if isinstance(part.get("history"), dict) else None
        entries = [e for e in (data.get("entries") if data else []) or [] if isinstance(e, dict)]
        kept = bool(data) and data.get("kept") is True
        items = []
        for entry in entries:
            how = text_of(entry.get("how"))
            words = mind_model.PHRASES.get("how." + how) if how in mind_plan.HOW else None
            numbered = self.sheet_numbers.get(integer(entry.get("id"))) if restore is not None else None
            head = mind_model.PHRASES["history.version"] % (numbered[0], self.day_of(entry.get("day"))) if numbered else \
                text_of(entry.get("day"))
            if numbered and numbered[1]:
                head += " \u201c%s\u201d" % numbered[1]
            lines = [E("p", {"class": "histhead"}, E("b", None, head), ", " + words if words else "")]
            memory = self.m.get(integer(entry.get("id")))
            if memory is not None:
                lines.append(E("p", None, self.said(memory, "inl"), "  ", text_of(entry.get("why"))))
            for name in entry.get("names", []) if isinstance(entry.get("names"), list) else []:
                if isinstance(name, dict):
                    lines.append(E("p", {"class": "quiet"}, "Named %s on %s." % (text_of(name.get("name")), text_of(name.get("day")))))
            note = {"days": "hist.nodiff.days", "unfetched": "hist.nodiff.unfetched", "unchecked": "hist.nodiff.unchecked",
                    "before": "hist.nodiff.before", "budget": "hist.nodiff.budget"}.get(entry.get("no_diff"))
            diff = entry.get("diff")
            if isinstance(diff, list) and diff:
                lines.append(self.show(mind_model.PHRASES["hist.changed"], self.diff(diff)))
            elif note:
                lines.append(E("p", {"class": "quiet"}, mind_model.PHRASES[note]))
            if restore is not None and kept and memory is not None:
                day, clock, shown = self.day_of(entry.get("day")), text_of(entry.get("time")), self.copy_heading(restore)
                came = mind_model.PHRASES["history.how"] % words if words else ""
                request = mind_model.PHRASES["history.restore"] % (shown, day, clock) if CLOCK.fullmatch(clock) else \
                    mind_model.PHRASES["history.restore.day"] % (shown, day)
                name = mind_model.PHRASES["history.copy"] % (shown, day, clock) if CLOCK.fullmatch(clock) else \
                    mind_model.PHRASES["history.copy.day"] % (shown, day)
                lines.append(E("p", {"class": "askline"}, self.sheet_copy(request[:-1] + came + ".", name)))
            items.append(E("li", {"class": "hist"}, *lines))
        return kept, entries, items

    def history(self, part):
        """The History of a part in the text view. A part that the manuscript view tells has its History there, in the box that
        its row opens, and only that: here one fixed line says so and links to it. Any other part has its own fold, newest version
        first (history_items)."""
        kept, entries, items = self.history_items(part)
        if not entries:
            return ""
        n, ordinal = integer(part.get("thread")), part.get("ordinal")
        if (n, ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None) in self.sheet_parts and \
                isinstance(ordinal, int) and not isinstance(ordinal, bool):
            return E("p", {"class": "quiet"}, mind_model.PHRASES["hist.paper"], " ",
                     E("a", {"class": "lk", "href": anchor("hist-%d-%d" % (n, ordinal))},
                       mind_model.PHRASES["sheet.nav"] % (text_of((self.sheet or {}).get("noun")) or "the paper")), ".")
        title = plural(len(entries), "History (1 version)", "History (%d versions)") if kept else "History"
        first = E("p", {"class": "quiet"}, mind_model.PHRASES["hist.first"]) if kept else ""
        guide = E("p", {"class": "quiet"}, mind_model.PHRASES["hist.restore"] % text_of(entries[0].get("day"))) if kept else \
            E("p", {"class": "quiet"}, mind_model.PHRASES["hist.days"])
        return self.show(title, guide, E("ol", {"class": "histlist"}, *items), first)

    def diff(self, ops):
        """A difference between two versions of a section's text, drawn from the model's operations. This is the one place
        that makes an `ins` or a `del` element, and only from a run of type add or del: an operation or a run type that is not
        on the lists of mind_versions is drawn as plain text, and every text is text (through E())."""
        out = []
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                continue
            kind = op.get("op")
            if kind == "same" and "n" in op:
                count = integer(op.get("n"))
                out.append(E("p", {"class": "dsame"}, mind_model.PHRASES["diff.same.one"] if count == 1 else
                             mind_model.PHRASES["diff.same"] % count))
            elif kind == "cut":
                out.append(E("p", {"class": "quiet"}, mind_model.PHRASES["diff.cut"]))
            else:
                runs = op.get("runs") if isinstance(op.get("runs"), list) else []
                cls = "d-" + kind if kind in ("add", "del", "change") else "d-plain"
                out.append(E("p", {"class": cls}, *self.diff_runs(runs, kind in mind_versions.OPS)))
        return E("div", {"class": "diff"}, *out)

    @staticmethod
    def diff_runs(runs, marked):
        """The nodes of the runs of a difference: an `ins` for a run of type add and a `del` for one of type del when `marked`, else
        and for any other run its words as text. The only place that makes an `ins` or a `del` element, from a run of those two types
        only; the words are text (through E())."""
        parts = []
        for run in runs if isinstance(runs, list) else []:
            if not isinstance(run, dict):
                continue
            words = text_of(run.get("text"))
            if marked and run.get("t") == "add":
                parts.append(E("ins", None, words))
            elif marked and run.get("t") == "del":
                parts.append(E("del", None, words))
            else:
                parts.append(words)
        return parts

    def versions_block(self):
        """"Versions of the paper": each named version with its day and the author's words, under it what changed since the
        name before (a fold per part that changed, and a count of the parts that did not)."""
        data = self.model.get("versions")
        if not isinstance(data, dict) or data.get("kept") is not True:
            return ""
        names = [n for n in data.get("names", []) if isinstance(n, dict)] if isinstance(data.get("names"), list) else []
        items = []
        for entry in names:
            lines = [E("p", {"class": "histhead"}, E("b", None, text_of(entry.get("name"))), "  ",
                       mind_model.PHRASES["ver.named"] % text_of(entry.get("day")))]
            if text_of(entry.get("words")):
                lines.append(E("p", {"class": "quiet"}, "\u201c" + text_of(entry.get("words")) + "\u201d"))
            since = entry.get("since") if isinstance(entry.get("since"), dict) else None
            if since is not None:
                inner = []
                for part in since.get("parts", []) if isinstance(since.get("parts"), list) else []:
                    if not isinstance(part, dict):
                        continue
                    label = "%s: %s" % (self.thread_name(integer(part.get("thread"))), text_of(part.get("label")))
                    if isinstance(part.get("diff"), list) and part["diff"]:
                        inner.append(self.show(label, self.diff(part["diff"])))
                    else:
                        note = {"unfetched": "hist.nodiff.unfetched", "unchecked": "hist.nodiff.unchecked",
                                "budget": "hist.nodiff.budget"}.get(part.get("no_diff"))
                        inner.append(E("p", {"class": "quiet"}, label + ". " + (mind_model.PHRASES[note] if note else "")))
                same = integer(since.get("same"))
                if same:
                    inner.append(E("p", {"class": "quiet"}, mind_model.PHRASES["ver.same.one"] if same == 1 else
                                   mind_model.PHRASES["ver.same"] % same))
                lines.append(self.show(mind_model.PHRASES["ver.since"] % text_of(since.get("name")), *inner))
            items.append(E("li", {"class": "hist"}, *lines))
        return E("section", {"class": "versions", "id": "versions-of-the-paper"}, E("h3", None, mind_model.PHRASES["ver.title"]),
                 E("ol", {"class": "histlist"}, *items) if items else E("p", {"class": "quiet"}, mind_model.PHRASES["ver.none"]))

    def open_plan_group(self, key, title, hint, rows, empty):
        return E("section", {"class": "ogroup", "id": "open-%s" % key}, E("h3", None, title),
                 E("p", {"class": "quiet"}, hint), E("ul", {"class": "notes"}, *rows) if rows else
                 E("p", {"class": "nothing"}, empty))

    def part_link(self, part):
        n, ordinal = integer(part.get("thread")), part.get("ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            return E("a", {"href": anchor("text-%d-%d" % (n, ordinal))}, self.thread_name(n))
        return self.thread_link(n)

    def part(self, part):
        n, ordinal = integer(part.get("thread")), part.get("ordinal")
        thread = self.threads.get(n)
        if not thread:
            return ""
        sections = thread.get("sections", [])
        text = sections[ordinal] if isinstance(ordinal, int) and not isinstance(ordinal, bool) and \
            0 <= ordinal < len(sections) and isinstance(sections[ordinal], dict) else None
        topic = thread.get("topic")
        address = github_url("issue", self.repo, topic)
        if thread.get("shown") is True and text is not None:
            body = self.blocks(text.get("doc"), address, self.stores.get(n), topic)
            ident = "text-%d-%d" % (n, ordinal)
        else:
            body = J(E("p", None, NOT_SHOWN), E("a", {"href": address}, "Open it on GitHub") if address else "")
            ident = None
        return E("article", {"class": "part", "id": ident},
                 E("p", {"class": "status"}, *self.sentences(part.get("status"))), E("div", {"class": "parttext"}, body),
                 self.history(part),
                 E("p", {"class": "partlinks"}, E("a", {"href": anchor("settled-%d" % n)}, "Why it says this"), "  ",
                   E("a", {"href": anchor("thread-%d" % n)}, "How we got here")))

    # ------------------------------------------------------------ the manuscript, read as a paper (1.4.0)

    @staticmethod
    def whole_numbers(values):
        """The whole numbers of a list that may stand for a version (1 to 999999), in order and without repeats; at most 64."""
        out = []
        for value in values if isinstance(values, list) else []:
            if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 999999 and value not in out:
                out.append(value)
        return out[:64]

    @staticmethod
    def tokens(numbers):
        return " ".join(str(n) for n in numbers)

    @staticmethod
    def day_of(value):
        """A day as the model writes one, else "an unknown day"."""
        return value if isinstance(value, str) and DAY.fullmatch(value) else mind_model.PHRASES["day.unknown"]

    def sheet_name(self, n):
        """The name of a numbered version, only when versions.NAME accepts it in full; else None."""
        name = (self.sheet_list.get(integer(n)) or {}).get("name")
        return name if isinstance(name, str) and versions.NAME.fullmatch(name) else None

    def sheet_kv(self, key, numbers):
        """A fixed phrase whose "%d" is the version the reader chose: the phrase around a span that the script keeps at that
        number, then the name of each named version of `numbers` in a span that shows with its version."""
        default = self.sheet_default
        first = default if default in numbers else numbers[0]
        before, _, after = mind_model.PHRASES[key].partition("%d")
        names = [E("span", {"class": "kn" + (" on" if n == default else ""), "data-for": str(n)},
                   J(", “", self.sheet_name(n), "”")) for n in numbers if self.sheet_name(n)]
        return J(before, E("span", {"class": "kv"}, str(first)), *names, after)

    def sheet_view(self):
        """The manuscript view, with the project words as buttons (a keyboard reaches them) in all of it."""
        self.term_tag, self.sheet_headings = "button", True
        try:
            return self.sheet_drawn()
        finally:
            self.term_tag, self.sheet_headings = "span", False

    def sheet_drawn(self):
        """The manuscript as a paper: a sheet with the text in the order of the deliverable and a quiet margin beside each part and
        paragraph, a bar that says which text it shows, the planned parts the text does not say yet, and the sources. The model
        gives every sentence, count and state; the style and the script only show some of it, by mode and by version."""
        sheet = self.sheet
        data = sheet.get("versions") if isinstance(sheet.get("versions"), dict) else {}
        listed = [v for v in data.get("list", []) if isinstance(v, dict) and integer(v.get("n")) > 0] \
            if isinstance(data.get("list"), list) and sheet.get("mode") == "changes" else []
        self.sheet_list = {integer(v["n"]): v for v in listed}
        default = integer(data.get("default"))
        self.sheet_default = default if default in self.sheet_list else (integer(listed[0]["n"]) if listed else None)
        mode = "changes" if listed else "plain"
        noun = text_of(sheet.get("noun")) or "the paper"
        title = text_of(sheet.get("title")) or noun.capitalize()
        built = text_of((self.model.get("built") or {}).get("words"))
        sections = [s for s in sheet.get("sections", []) if isinstance(s, dict)] if isinstance(sheet.get("sections"), list) else []
        body = [self.sheet_section(s, k + 1) for k, s in enumerate(sections)]
        said = []
        line = text_of(data.get("line"))
        note = mind_model.PHRASES["line." + line] if line in ("off", "none", "first", "unchecked") and mode == "plain" else ""
        if mode == "changes" and integer(data.get("omitted")) > 0:
            said.append(plural(integer(data["omitted"]), *mind_model.PHRASES["line.omitted"]))
        if mode == "changes" and data.get("dropped") is True:
            said.append(mind_model.PHRASES["line.dropped"])
        if integer(sheet.get("unfetched")):
            said.append(mind_model.PHRASES["line.unfetched"] % plural(integer(sheet["unfetched"]), "1 part", "%d parts"))
        message = text_of(sheet.get("message"))
        rows = [r for r in sheet.get("sources", []) if isinstance(r, dict)] if isinstance(sheet.get("sources"), list) else []
        sourced = ""
        if rows:
            sourced = J(E("div", {"class": "row sechead srcs-head"}, E("h3", {"class": "endh"}, mind_model.PHRASES["src.title"])),
                        E("div", {"class": "row"}, E("div", None, E("ol", {"class": "refs2"}, *[
                            E("li", None, E("b", None, self.runs(r.get("statement"))), ". ", self.runs(r.get("source")), ".")
                            for r in rows]))))
        name = text_of(sheet.get("name"))
        front = E("header", {"class": "front"},
                  E("h2", {"id": "paper-h", "class": "ptitle"}, title),
                  E("p", {"class": "psub" if name else "psub noprint"}, name,
                    E("span", {"class": "noprint"}, " · " if name else "",
                      E("a", {"class": "lk", "href": anchor("project-map")}, "Project Map"))),
                  E("p", {"class": "dateline"}, mind_model.PHRASES["sheet.dateline"] % built),
                  E("p", {"class": "fronttools noprint"}, E("a", {"class": "lk", "href": anchor("text")}, mind_model.PHRASES["sheet.deliver"]),
                    E("button", {"type": "button", "data-print": "1"}, mind_model.PHRASES["sheet.print"])),
                  E("div", {"class": "row"},
                    E("div", None, E("div", {"class": "pmsg noprint"}, E("p", {"class": "lab"}, mind_model.PHRASES["sheet.message"]),
                                     E("p", None, message, "  ", E("a", {"class": "lk", "href": anchor("plan-against-text")},
                                                                    mind_model.PHRASES["sheet.plan"]))) if message else "")),
                  self.sheet_legend(bool(sheet.get("plan")), mode == "changes"))
        return E("section", {"class": "view", "id": "paper", "aria-labelledby": "paper-h", "data-mode": mode},
                 E("a", {"class": "skip", "href": anchor("paper-text")}, mind_model.PHRASES["sheet.skip"]),
                 self.sheet_bar(mode, listed, note), E("p", {"class": "sr", "id": "copystatus", "aria-live": "polite"}),
                 E("article", {"class": "sheet"},
                   E("p", {"class": "rh"}, E("span", None, name), E("span", {"class": "rh-clean"},
                                                                     mind_model.PHRASES["sheet.rh"] % (title, built)),
                     E("span", {"class": "rh-changes"}, self.sheet_kv("sheet.rh.changes", list(self.sheet_list))
                       if self.sheet_default else "")),
                   front, *[E("p", {"class": "quiet noprint"}, words) for words in said],
                   E("div", {"id": "paper-text"}, *body), sourced,
                   E("p", {"class": "sheetfoot noprint"}, mind_model.PHRASES["sheet.hint"], " ", mind_model.PHRASES["foot"])))

    def sheet_bar(self, mode, listed, note=""):
        """The bar that stays at the top while the paper is read: the two ways to show it (the changes since a version, or a clean
        copy), which version, a line that says what is shown, the buttons for the next change, the way to the plan and the print.
        `note` is the fixed line that stands in the line where there is no picker (versions off, none kept, one kept)."""
        controls, lines = "", []
        phrases = mind_model.PHRASES
        if mode == "changes":
            options = []
            for entry in listed:
                n = integer(entry["n"])
                label = phrases["sheet.version"] % (n, self.day_of(entry.get("day")))
                label += phrases["sheet.previous"] if entry.get("previous") is True else ""
                label += " \u201c%s\u201d" % self.sheet_name(n) if self.sheet_name(n) else ""
                options.append(E("option", {"value": n, "selected": "selected" if n == self.sheet_default else None}, label))
                counts = entry.get("changed") if isinstance(entry.get("changed"), dict) else {}
                changes, parts = integer(counts.get("changes")), integer(counts.get("parts"))
                before, other = integer(counts.get("before")), integer(counts.get("other"))
                when = self.day_of(entry.get("day"))
                said = phrases["line.changed"] % (plural(changes, *phrases["line.changes"]), plural(parts, *phrases["line.parts"]), n, when) \
                    if changes else (phrases["line.same.some"] if before or other else phrases["line.same"]) % (n, when)
                if before:
                    said += "; " + plural(before, *phrases["line.before"])
                if other:
                    said += "; " + plural(other, *phrases["line.not"])
                lines.append(E("span", {"class": "ml mlc" + (" on" if n == self.sheet_default else ""), "data-for": str(n)}, said))
            controls = E("fieldset", {"class": "seg"}, E("legend", None, phrases["sheet.view"]),
                         E("label", {"class": "si"}, E("input", {"type": "radio", "name": "pmode", "value": "changes",
                                                                "checked": "checked"}),
                           E("span", None, phrases["sheet.changes"])),
                         E("select", {"class": "pick", "id": "pick", "aria-label": "Compare the text with which version"},
                           *options),
                         E("label", {"class": "si"}, E("input", {"type": "radio", "name": "pmode", "value": "clean"}),
                           E("span", None, phrases["sheet.clean"])))
        lines.append(E("span", {"class": "ml mln"}, phrases["line.clean"], ". " + note if note else ""))
        buttons = E("span", {"class": "chgnav"},
                    E("button", {"type": "button", "id": "chgprev", "title": "Previous change"}, phrases["sheet.prev"]),
                    E("button", {"type": "button", "id": "chgnext", "title": "Next change"}, phrases["sheet.next"])) \
            if mode == "changes" else ""
        return E("div", {"class": "sbar noprint"}, controls, E("span", {"class": "grow"}),
                 E("span", {"class": "bartools"}, E("a", {"class": "lk barlink", "href": anchor("text")}, phrases["sheet.deliver"]),
                   E("button", {"type": "button", "class": "barprint", "data-print": "1"}, phrases["sheet.print"])),
                 E("div", {"class": "barline"}, E("span", {"class": "modeline", "aria-live": "polite"}, *lines), buttons))

    @staticmethod
    def sheet_mark(kind, words, explain=None):
        """A state in a margin: its sign, then its words. The words explain themselves (a button) when the state has an
        explanation of the page."""
        text = Page.explain_button(explain, words, "stw") if isinstance(explain, str) and explain in EXPLAIN_TEXT \
            else E("span", {"class": "stw"}, words)
        return E("span", {"class": "st st-" + kind}, text)

    def sheet_legend(self, plan, changes):
        """What the signs in the margin mean, in a fold that the script shuts on a phone; nothing when no sign is drawn. The line
        about change marks is there only when the text can be compared, and the one about dashed frames with a plan."""
        signs = ("backed", "against", "planned", "now", "open") if plan else ()
        rows = [E("p", None, self.sheet_mark(kind, mind_model.PHRASES["leg." + kind])) for kind in signs]
        if changes:
            rows.append(E("p", {"class": "lg-chg"}, self.sheet_mark("changed", mind_model.PHRASES["leg.changed"])))
        if plan:
            rows.append(E("p", {"class": "lg-slot"}, mind_model.PHRASES["leg.slot"]))
        if not rows:
            return ""
        return E("details", {"class": "legend noprint", "open": "open"}, E("summary", None, mind_model.PHRASES["sheet.legend"]),
                 E("div", {"class": "lgrows"}, *rows))

    def sheet_contents(self):
        """The sections of the paper as indented items of the side bar, which the script shows while the paper is open and marks
        as the reader scrolls."""
        sections = [x for x in self.sheet.get("sections", []) if isinstance(x, dict)] if isinstance(self.sheet.get("sections"), list) \
            else []
        return E("ul", {"class": "subnav"}, *[E("li", None, E("a", {"href": anchor("p-sec-%d" % (k + 1))}, text_of(x.get("label"))))
                                                for k, x in enumerate(sections)]) if sections else ""

    def sheet_id(self, prefix):
        self.sheet_ids[prefix] = self.sheet_ids.get(prefix, 0) + 1
        return "%s-%d" % (prefix, self.sheet_ids[prefix])

    @staticmethod
    def copy_heading(value):
        """A section heading as a request for the chat may carry it: only a heading of the form the profiles' headings have
        (recortex.HEADING_TEXT), else the word "section". What a copied request holds is the plugin's own fixed words, a
        heading, a number and a checked id, never a word of the record: the request becomes the user's own message."""
        return value if isinstance(value, str) and recortex.HEADING_TEXT.fullmatch(value) else "section"

    def sheet_copy(self, request, short_name):
        """A request for the chat in quotation marks and a button, named by the request, that copies it. The script copies the words
        of the element before the button, so it holds none."""
        return J(E("span", {"class": "req"}, request),
                 E("button", {"type": "button", "class": "ask", "data-copy": "1"}, mind_model.PHRASES["copy"] % short_name))

    def sheet_section(self, section, k):
        n, label = integer(section.get("n")), text_of(section.get("label"))
        parts = [p for p in section.get("parts", []) if isinstance(p, dict)] if isinstance(section.get("parts"), list) else []
        plan = section.get("plan") if isinstance(section.get("plan"), dict) else None
        has_text = section.get("text") is True
        untold = section.get("untold") is True   # its text is there and may not be told: never "No text yet", never a request
        note = []
        if plan is not None:
            planned, backed = integer(plan.get("planned")), integer(plan.get("backed"))
            if planned:
                note.append(E("span", None, mind_model.PHRASES["sec.plan"] % (backed, planned)))
                note.append(E("svg", {"class": "meter", "viewBox": "0 0 100 4", "preserveAspectRatio": "none", "aria-hidden": "true"},
                              E("rect", {"class": "mbg", "width": 100, "height": 4}),
                              E("rect", {"class": "mfg", "width": round(100 * min(backed, planned) / planned), "height": 4})))
            else:
                note.append(E("span", None, mind_model.PHRASES["sec.none"]))
            if not has_text and not untold and planned:
                done = mind_model.PHRASES["empty.backed"]
                note.append(E("span", None, mind_model.PHRASES["empty.plan"] % (
                    plural(planned, *mind_model.PHRASES["empty.are"]), done[0] if not backed else done[1] if backed == 1 else
                    done[2] % backed)))
            note.append(E("a", {"class": "lk", "href": anchor("sec-%d" % n if n > 0 else "plan-against-text")},
                          mind_model.PHRASES["sec.link"] % label))
        head = E("div", {"class": "row sechead"}, E("h3", {"class": "sech", "id": "p-sec-%d" % k}, label),
                 E("div", {"class": "mn", "role": "note"}, *note) if note else "")
        empty = ""
        if not has_text and not untold:
            shown = self.copy_heading(label)
            ask = J(E("span", None, mind_model.PHRASES["empty.draft"]), self.sheet_copy(
                mind_model.PHRASES["empty.request"] % shown, mind_model.PHRASES["copy.draft"] % shown)) \
                if section.get("write") is True else ""
            empty = E("div", {"class": "row"}, E("div", None, E("p", {"class": "notext"}, mind_model.PHRASES["empty.line"])),
                      E("div", {"class": "mn", "role": "note"}, ask) if ask else "")
        gone = [g for g in section.get("gone", []) if isinstance(g, dict)] if isinstance(section.get("gone"), list) else []
        return E("section", {"class": "psec" + ("" if has_text else " sec-empty")}, head, empty,
                 *[self.sheet_gone(g) for g in gone], *[self.sheet_part(p, label, k) for p in parts],
                 self.sheet_slots(section.get("slots"), label, integer(section.get("more"))))

    def sheet_gone(self, gone):
        """A part that an earlier text had and the topic no longer has: its old heading and one quiet line."""
        numbers = self.whole_numbers(gone.get("for"))
        if not numbers:
            return ""
        return E("div", {"class": "row r-removed gone" + (" on" if self.sheet_default in numbers else ""),
                         "data-for": self.tokens(numbers)},
                 E("div", {"class": "ptx"}, E("h4", {"class": "parth"}, text_of(gone.get("name"))),
                   E("p", {"class": "quiet"}, self.sheet_kv("since.removed.part", numbers))), E("div", {"class": "mn"}))

    def sheet_slots(self, slots, heading, more):
        """The planned parts that the text does not say yet, in boxes with dashed frames under a heading, with in the margin how many
        they are; "and N more in the plan" when the section has more than are shown."""
        items = [s for s in slots.get("items", []) if isinstance(s, dict)] if isinstance(slots, dict) and \
            isinstance(slots.get("items"), list) else []
        if not items and not more:
            return ""
        label, rows = self.sheet_id("hole"), []
        for slot in items:
            kind = text_of(slot.get("part"))
            # the request holds the heading and the plan comment's id only; the plan's own line, which is record text, stays
            # beside the button and the session reads it from the index (index.py query plan) as data
            part_id = integer(slot.get("id"))
            ask = self.sheet_copy(mind_model.PHRASES["slot.request"] % (self.copy_heading(slot.get("heading")), part_id),
                                  mind_model.PHRASES["slot.write"]) if slot.get("write") is True and part_id > 0 else ""
            plain, kind = text_of(slot.get("plain")), kind if PART_KIND.fullmatch(kind) else "part"
            doubled = plain.lower().split(None, 1)[:1] == [kind]   # the plan's line starts with the kind: it is said once
            rows.append(E("li", {"class": "hole"}, E("span", {"class": "kind"}, mind_model.PHRASES["slot.kind.bare"] % text_of(slot.get("tail"))
                                                    if doubled else mind_model.PHRASES["slot.kind"] % (kind, text_of(slot.get("tail")))),
                          plain, ask))
        margin = slots.get("margin") if isinstance(slots, dict) and isinstance(slots.get("margin"), dict) else {}
        count, kinds = integer(margin.get("count")), text_of(margin.get("kinds"))
        one, many = mind_model.PHRASES["slot.margin"]
        note = [E("span", {"class": "st st-planned"}, E("span", {"class": "stw"}, one % kinds if count == 1 else many % (count, kinds)))] \
            if count else []
        if more:
            note.append(E("a", {"class": "lk", "href": anchor("plan-against-text")}, mind_model.PHRASES["slot.more"] % more))
        return E("div", {"class": "row holes noprint"},
                 E("div", None, E("h5", {"class": "lab", "id": label}, mind_model.PHRASES["slot.label"] % heading),
                   E("ul", {"class": "holelist", "aria-labelledby": label}, *rows) if rows else ""),
                 E("div", {"class": "mn", "role": "note"}, *note))

    def sheet_part(self, part, heading, k):
        """A part: its heading and its margin (with its first paragraph when it has one), the box of its history, the rest of its
        paragraphs as rows, and its slots."""
        n, ordinal = integer(part.get("thread")), part.get("ordinal")
        ordinal = ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None
        thread = self.threads.get(n) or {}
        name = text_of(part.get("name")) or text_of(thread.get("name"))
        ident = "%d-%s" % (n, ordinal) if ordinal is not None else "%d-x%d" % (n, k)
        outline = self.outline_parts.get((n, ordinal)) or {}
        title = E("h4", {"class": "parth", "id": "p-" + ident}, name)
        items = self.sheet_part_note(part, outline, n, ident)
        note = E("div", {"class": "mn pnote", "role": "note"}, *items) if items else ""
        box = self.sheet_history(outline, ident, name, heading)
        topic, rows = thread.get("topic"), part.get("rows") if isinstance(part.get("rows"), list) else None
        sections = thread.get("sections") if isinstance(thread.get("sections"), list) else []
        doc = sections[ordinal].get("doc") if ordinal is not None and 0 <= ordinal < len(sections) and \
            isinstance(sections[ordinal], dict) else None
        slots = self.sheet_slots(part.get("slots"), heading, 0)
        if part.get("shown") is not True:
            address = github_url("issue", self.repo, topic)
            body, empty = [E("div", {"class": "row untoldrow noprint"}, E("div", None, E("p", {"class": "quiet"}, NOT_SHOWN, " ",
                           E("a", {"href": address}, "Open it on GitHub") if address else "")), E("div", {"class": "mn"}))], True
        elif part.get("long") is True or not rows or not isinstance(doc, dict):
            blocks = self.blocks(doc, github_url("issue", self.repo, topic), self.stores.get(n), topic) \
                if isinstance(doc, dict) and doc.get("blocks") else ""
            body, empty = [E("div", {"class": "row"}, E("div", {"class": "ptx"}, blocks if blocks != "" else E(
                "p", {"class": "notext"}, mind_model.PHRASES["empty.line"])),
                           E("div", {"class": "mn", "role": "note"}, E("span", None, mind_model.PHRASES["since.long"]))
                           if part.get("long") is True else E("div", {"class": "mn"}))], blocks == ""
        else:
            return E("section", {"class": "part"}, *self.sheet_rows(part, rows, doc, n, topic, (title, note, box)), slots)
        return E("section", {"class": "part part-empty" if empty else "part"}, E("div", {"class": "row partrow"}, title, note), box,
                 *body, slots)

    def sheet_part_note(self, part, outline, n, ident):
        """The margin of a part's heading: its states in one wrapping group, how it stands against the versions the picker offers, and
        one line with when it last changed and the ways to its history and its story."""
        states = []
        for mark in part.get("marks", []) if isinstance(part.get("marks"), list) else []:
            if not isinstance(mark, dict) or mark.get("k") not in MARK_KINDS:
                continue
            kind, words, m, count = mark["k"], text_of(mark.get("text")), integer(mark.get("m")), integer(mark.get("n"))
            if kind in ("open", "failures"):
                link = E("a", {"class": "stw lk", "href": anchor("m-%d" % m), "data-m": m}, words) \
                    if kind == "open" and count == 1 and m in self.m else \
                    E("a", {"class": "stw lk", "href": anchor("open-questions" if kind == "open" else "open-failures")}, words)
                states.append(E("span", {"class": "st st-open"}, link))
                continue
            sign = self.sheet_mark(kind, words, mark.get("explain"))
            states.append(J(sign, " ", E("a", {"class": "lk", "href": anchor("m-%d" % m), "data-m": m}, mind_model.PHRASES["mark.see"]))
                          if kind == "against" and m in self.m else sign)
        if len(states) > NOTE_STATES + 1:
            # a longer list keeps its first states and folds the rest into the note: built open, so that without the script the
            # note is as it was; the script shuts the fold, so that a note stays about eight lines tall at the margin's width
            states = states[:NOTE_STATES] + [E("details", {"class": "stmore", "open": "open"}, E("summary", None, plural(
                len(states) - NOTE_STATES, *mind_model.PHRASES["part.more"])), E("div", {"class": "sts"}, *states[NOTE_STATES:]))]
        out = [E("div", {"class": "sts"}, *states)] if states else []
        since = part.get("since") if isinstance(part.get("since"), list) else []
        out += [line for line in (self.sheet_since_note(g) for g in since if isinstance(g, dict)) if line != ""]
        links = []   # when it last changed and the ways to its history and its story, in one flowing line
        last = part.get("last") if isinstance(part.get("last"), dict) else None
        if last is not None:
            how = text_of(last.get("how"))
            links.append(E("span", {"class": "lc"}, mind_model.PHRASES["part.changed"] % self.day_of(last.get("day")),
                           ", " + mind_model.PHRASES["how." + how] if how in mind_plan.HOW else ""))
        history = outline.get("history") if isinstance(outline.get("history"), dict) else {}
        entries = [e for e in history.get("entries", []) if isinstance(e, dict)] if isinstance(history.get("entries"), list) else []
        if entries:
            links.append(E("button", {"type": "button", "class": "lk", "data-toggle": "hist-" + ident, "aria-controls": "hist-" + ident,
                                      "aria-expanded": "false"}, mind_model.PHRASES["part.history"] % len(entries) if history.get("kept") is True
                         else mind_model.PHRASES["part.history.days"]))
        if n in self.threads:
            links.append(E("a", {"class": "lk", "href": anchor("thread-%d" % n)}, mind_model.PHRASES["part.how"]))
        if links:
            out.append(E("span", {"class": "plinks"}, *links))
        return out

    def sheet_since_note(self, group):
        """How a part stands against the versions of a group: what changed, or why nothing is marked. It shows with those versions."""
        numbers, state = self.whole_numbers(group.get("for")), group.get("state")
        if not numbers or state not in SINCE_STATES:
            return ""
        attrs = {"class": "pn", "data-for": self.tokens(numbers)}
        if self.sheet_default in numbers:
            attrs["class"] += " on"
        counts = group.get("counts") if isinstance(group.get("counts"), dict) else {}
        if state == "marked":
            said = ", ".join(plural(integer(counts.get(kind)), *mind_model.PHRASES["count." + kind])
                             for kind in ("reworded", "added", "removed", "changed") if integer(counts.get(kind)))
            return E("span", dict(attrs, **{"class": attrs["class"] + " st st-changed chg"}), E("span", {"class": "stw"},
                                                                                             said, self.sheet_kv("count.since", numbers)))
        if state == "new":
            return E("span", dict(attrs, **{"class": attrs["class"] + " st st-changed chg"}),
                     E("span", {"class": "stw"}, self.sheet_kv("since.new", numbers)))
        key = {"same": "since.same", "unfetched": "since.unfetched", "unchecked": "since.unchecked"}.get(state)   # "before" is counted in the bar
        if key is not None:
            return E("span", attrs, self.sheet_kv(key, numbers))
        return E("span", attrs, mind_model.PHRASES["since.long" if state == "long" else "since.budget"]) \
            if state in ("long", "budget") else ""

    def sheet_history(self, outline, ident, name, heading):
        """The history of a part as a box under its heading that its button opens (the script shuts it at the start and moves the
        focus into it), with every version's own request for the chat and a button that closes it."""
        kept, entries, items = self.history_items(outline, restore=heading)
        if not entries:
            return ""
        box = "hist-" + ident
        return E("div", {"class": "histbox noprint", "id": box, "tabindex": "-1", "aria-labelledby": box + "-h"},
                 E("h5", {"class": "lab", "id": box + "-h"}, mind_model.PHRASES["history.title"] % name),
                 E("p", {"class": "quiet"}, mind_model.PHRASES["hist.first"] if kept and len(entries) == 1 else
                   mind_model.PHRASES["hist.days"] if not kept else ""),
                 E("ol", {"class": "histlist"}, *items),
                 E("p", {"class": "askline"}, E("button", {"type": "button", "class": "ask", "data-toggle": box},
                                                mind_model.PHRASES["history.close"])))

    def sheet_rows(self, part, rows, doc, n, topic, lead):
        """The rows of a part: each block of its current text, with what the versions mark on it (the class of the version the picker
        starts at, and for each kind of mark the versions it applies to), and the blocks only an earlier text had where they stood.
        The heading and the margin of the part (`lead`) stand in the first row, and the box of its history follows that row."""
        blocks = doc.get("blocks") if isinstance(doc.get("blocks"), list) else []
        store = self.stores.get(n)
        gone = {}
        for place in part.get("removed", []) if isinstance(part.get("removed"), list) else []:
            if isinstance(place, dict) and self.whole_numbers(place.get("for")):
                gone.setdefault(min(integer(place.get("at")), len(blocks)), []).append(place)
        title, note, box = lead
        out, merged = [], False
        if gone.get(0):   # earlier blocks come before the first one: the heading has a row of its own
            out += [E("div", {"class": "row partrow"}, title, note), box]
            merged = True
        for i in range(len(blocks) + 1):
            out += [self.sheet_removed(place, topic) for place in gone.get(i, [])]
            if i == len(blocks) or not isinstance(blocks[i], dict):
                continue
            row = rows[i] if i < len(rows) and isinstance(rows[i], dict) else {}
            if merged:
                out.append(self.sheet_row(blocks[i], row, store, topic))
            else:
                out += [self.sheet_row(blocks[i], row, store, topic, (title, note)), box]
                merged = True
        if not merged:
            out += [E("div", {"class": "row partrow"}, title, note), box]
        return out

    def sheet_row(self, block, row, store, topic, lead=None):
        """One block with its margin. In the changes view a reworded paragraph shows as the marked phrases, an added paragraph and a
        changed block with a bar; the clean copy, the print and the plain view show the block as it is."""
        kinds = {kind: self.whole_numbers(row.get(kind)) for kind in ROW_KINDS}
        default = self.sheet_default
        shown = next((kind for kind in ROW_KINDS if default in kinds[kind]), None)
        attrs = {"class": "row rw" + (" r-" + shown if shown else "")}
        for kind in ROW_KINDS:
            if kinds[kind]:
                attrs["data-" + kind] = self.tokens(kinds[kind])
        ptx = []
        for kind in ("added", "changed"):   # what a screen reader hears before the block: the bar and the note come after it
            if kinds[kind]:
                ptx.append(E("span", {"class": "vh vh-" + kind}, self.sheet_kv("since." + kind, kinds[kind]), ": "))
        cells = {}   # the cells of a table that differ from an earlier version of it: (head or rows, row, column) -> [(versions, was)]
        for then in row.get("then", []) if isinstance(row.get("then"), list) else []:
            numbers = self.whole_numbers(then.get("for")) if isinstance(then, dict) else []
            for cell in then.get("cells", []) if numbers and isinstance(then.get("cells"), list) else []:
                if isinstance(cell, dict) and cell.get("k") in ("head", "rows"):
                    cells.setdefault((cell["k"], integer(cell.get("r")), integer(cell.get("c"))), []).append(
                        (numbers, text_of(cell.get("was"))))
        ptx.append(E("div", {"class": "v-now"}, self.sheet_block(block, store, topic, cells)))
        for mark in row.get("marks", []) if isinstance(row.get("marks"), list) else []:
            numbers = self.whole_numbers(mark.get("for")) if isinstance(mark, dict) else []
            if numbers:
                ptx.append(E("div", {"class": "v-marks" + (" on" if default in numbers else ""), "data-for": self.tokens(numbers)},
                             E("p", None, *self.diff_runs(mark.get("runs"), True))))
        for then in row.get("then", []) if isinstance(row.get("then"), list) else []:
            numbers = self.whole_numbers(then.get("for")) if isinstance(then, dict) else []
            if numbers:
                ptx.append(E("details", {"class": "ev v-then" + (" on" if default in numbers else ""), "data-for": self.tokens(numbers)},
                             E("summary", None, mind_model.PHRASES["since.then"]),
                             E("div", {"class": "evbody"}, self.sheet_then(then, topic))))
        note = []
        who = text_of(row.get("who"))
        if who in WHO_KEYS:
            note.append(E("span", {"class": "who"}, mind_model.PHRASES["who." + who] % self.day_of(row.get("day"))
                          if who == "kept" else mind_model.PHRASES["who." + who]))
        for kind in ROW_KINDS:
            if kinds[kind]:
                note.append(E("span", {"class": "chg c-" + kind}, self.sheet_kv("since." + kind, kinds[kind])))
        mine = E("div", {"class": "mn", "role": "note"}, *note) if note else ""
        if lead is None:
            return E("div", attrs, E("div", {"class": "ptx"}, *ptx), mine or E("div", {"class": "mn"}))
        attrs["class"] += " hrow"
        return E("div", attrs, lead[0], E("div", {"class": "ptx"}, *ptx), E("div", {"class": "mnotes"}, lead[1], mine))

    def sheet_removed(self, place, topic):
        """A block that an earlier text had and the text no longer has: one quiet line with the earlier block in a fold, struck."""
        numbers = self.whole_numbers(place.get("for"))
        return E("div", {"class": "row r-removed" + (" on" if self.sheet_default in numbers else ""),
                         "data-for": self.tokens(numbers)},
                 E("div", {"class": "ptx"}, E("p", {"class": "quiet"}, self.sheet_kv("since.removed", numbers)),
                   self.show(mind_model.PHRASES["since.show"], E("div", {"class": "struck"}, self.sheet_then(place, topic)))),
                 E("div", {"class": "mn"}))

    def sheet_then(self, place, topic):
        """An earlier block as it stood: a figure or table from the entry the model kept of it, anything else as the page draws it."""
        block = place.get("block") if isinstance(place.get("block"), dict) else {}
        entry = place.get("entry") if isinstance(place.get("entry"), dict) else None
        if "block" in block and entry is not None:
            return self.sheet_table_figure(entry, False) if entry.get("kind") == "table" else self.sheet_figure(entry, False)
        return self.block(block, None, topic) if block else ""

    def sheet_block(self, block, store, topic, cells=None):
        """A block of the current text: a figure or table of the paper as a figure with a caption, a table as a box that scrolls on
        its own, anything else as the page draws it. `cells` are the cells of a table that an earlier version had otherwise."""
        data = block.get("block") if isinstance(block.get("block"), dict) else None
        if data is not None and store is not None:
            found = store.get((data.get("kind"), data.get("id")))
            if found:
                return self.sheet_table_figure(found, True, cells) if found.get("kind") == "table" else self.sheet_figure(found, True)
        if "table" in block:
            return self.sheet_table_box(block["table"], None, None, cells)
        return self.block(block, store, topic)

    def sheet_cell(self, cell, head, was=None):
        """A cell. `was` lists, for the versions that held it otherwise, its earlier words: the cell is then washed while one of
        them is chosen, and a screen reader is told what it said."""
        cell = cell if isinstance(cell, dict) else {}
        rs, cs = integer(cell.get("rs"), 1), integer(cell.get("cs"), 1)
        shade = {"l": "al", "c": "ac", "r": "ar"}.get(cell.get("a"))
        header = head or cell.get("h") is True
        attrs = {"rowspan": rs if rs > 1 else None, "colspan": cs if cs > 1 else None, "class": shade,
                 "scope": "col" if head else None}
        notes = []
        if was:
            every = self.whole_numbers([n for numbers, _ in was for n in numbers])
            attrs["class"] = ((shade + " ") if shade else "") + "cell-chg" + (" on" if self.sheet_default in every else "")
            attrs["data-for"] = self.tokens(every)
            for numbers, words in was:
                notes.append(E("span", {"class": "cw" + (" on" if self.sheet_default in numbers else ""), "data-for": self.tokens(numbers)},
                               mind_model.PHRASES["cell.was"] % words))
        return E("th" if header else "td", attrs, self.runs(cell.get("r")), *notes)

    def sheet_table_box(self, table, number=None, label=None, cells=None):
        """A table in a box that scrolls on its own, which a keyboard reaches and a screen reader names (the region by its number,
        the table by its caption, `label` the id of that caption); a note when it was cut."""
        if not isinstance(table, dict):
            return ""
        cells = cells or {}
        rows = lambda key, head: [E("tr", None, *[self.sheet_cell(c, head, cells.get((key, r, k))) for k, c in enumerate(row)])
                                  for r, row in enumerate(table.get(key, [])) if isinstance(row, list)]
        name = "Table %d" % number if number and number <= 60 else "A table"
        caption = self.runs(table.get("caption")) if table.get("caption") else ""
        return J(E("p", {"class": "quiet"}, caption) if caption else "",
                 E("div", {"class": "tbox", "tabindex": "0", "role": "region", "aria-label": name},
                   E("table", {"aria-labelledby": label}, E("thead", None, *rows("head", True)), E("tbody", None, *rows("rows", False)))),
                 E("p", {"class": "quiet"}, TABLE_CUT) if table.get("cut") else "")

    def sheet_table_figure(self, block, counted, cells=None):
        """A table of the paper: its caption (the numbering is the style's), the table, and where it was made from."""
        number = None
        if counted:
            self.sheet_tables += 1
            number = self.sheet_tables
        label = self.sheet_id("tcap")
        return E("figure", {"class": "tab" if counted else "tab then-tab"},
                 self.sheet_caption(block, label),
                 self.sheet_table_box(block.get("table"), number, label, cells), self.made_from(block, "quiet made"))

    @staticmethod
    def sheet_caption(block, ident=None):
        """The caption of a figure or table: its title, a full stop unless the title ends in one, and the caption."""
        title, caption = text_of(block.get("title")), text_of(block.get("caption"))
        return E("figcaption", {"id": ident}, E("span", {"class": "ft"}, title + ("" if not title or title[-1] in ".!?" else ".")), " ", caption)

    def sheet_figure(self, block, counted):
        """A figure of the paper: the picture the build kept (or one fixed line and the link when it kept none), its caption, where
        the picture is and what it was made from."""
        address = github_url("picture", text_of(block.get("repo")) or self.repo, block.get("ref"), block.get("path"))
        pic = self.picture_of(block)
        link = E("a", {"class": "lk", "href": address}, "Open the picture on GitHub") if address else \
            E("span", {"class": "nothing"}, "This memory names a figure that this page cannot link to.")
        return E("figure", {"class": ("fig" if counted else "fig then-fig") + ("" if pic != "" else " nopic")},
                 pic if pic != "" else E("p", {"class": "nopic-line"}, mind_model.PHRASES["fig.missing"], " ", link),
                 self.sheet_caption(block),
                 E("p", {"class": "quiet where"}, "The picture is ", E("code", None, text_of(block.get("path"))), " at ",
                   text_of(block.get("ref")), ".") if text_of(block.get("path")) else "", self.made_from(block, "quiet made"),
                 link if pic != "" else "")

    @staticmethod
    def print_line():
        """The button that asks the browser to print the text: the page's own style leaves the paper alone on the sheet (no
        side bar, no folds, a page break before each section), and the browser's dialog offers "Save as PDF"."""
        return E("p", {"class": "noprint"}, E("button", {"type": "button", "class": "primary", "data-print": "1"},
                                               "Print or save as PDF"), "  ",
                 E("span", {"class": "quiet"}, "Prints the text alone. mind.py --paper writes it as a page of its own."))

    @staticmethod
    def read_line():
        """The line that stands where the print button was, once the manuscript has a view of its own."""
        return E("p", {"class": "noprint"}, E("a", {"class": "lk", "href": anchor("paper")}, mind_model.PHRASES["sheet.read"]))

    def text_view(self):
        outline = self.outline
        groups = [g for g in outline.get("deliverables", []) if isinstance(g, dict)] \
            if isinstance(outline.get("deliverables"), list) else []
        other = [o for o in outline.get("other", []) if isinstance(o, dict)] if isinstance(outline.get("other"), list) \
            else []
        title = text_of(outline.get("title")) or "What we deliver"
        if self.model.get("outputs_available") is False:
            title = "Current topic text"
        noun = text_of(outline.get("noun")) or "the deliverable"
        if not groups and not other:
            return self.view("text", title, TEXT_LEDE,
                             E("ul", {"class": "plainlist"}, *[E("li", None, text_of(o.get("title")), ": no current text yet.")
                               for o in outline.get("outputs", []) if isinstance(o, dict)]),
                             E("p", {"class": "quiet"}, "No part of %s has been written into the record yet." % noun),
                             self.untold_line())
        contents, body, sourced, drawn_parts = [], [], [], set()
        group_ids = []
        for i, group in enumerate(groups):
            ident = "output-" + str(group.get("id") or group.get("profile") or "text")
            if not ANCHOR.fullmatch(ident) or ident in group_ids:
                ident = "output-unknown-%d" % (i + 1)
                while ident in group_ids:
                    ident += "-x"
            group_ids.append(ident)

        def draw_part(part):
            key = (integer(part.get("thread")), part.get("ordinal"))
            if key in drawn_parts:
                return E("p", {"class": "quiet"}, "Shared current text: ", self.part_link(part))
            drawn_parts.add(key)
            return self.part(part)
        for group, ident in zip(groups, group_ids):
            sections = [s for s in group.get("sections", []) if isinstance(s, dict)]
            body.append(E("h3", {"class": "deliv", "id": ident}, text_of(group.get("title"))))
            for section in sections:
                parts = [p for p in section.get("parts", []) if isinstance(p, dict)]
                n = integer(section.get("n"))
                contents.append(E("li", None, self.section_link(n, section.get("label")),
                                  E("ul", None, *[E("li", None, self.part_link(p)) for p in parts])))
                body.append(E("section", {"class": "sec", "id": "sec-%d" % n}, E("h3", None, text_of(section.get("label"))),
                              self.blocks(section.get("before")), *[draw_part(p) for p in parts]))
                sourced += [integer(p.get("thread")) for p in parts]
        by_thread = []
        for entry in other:
            n = integer(entry.get("thread"))
            if not by_thread or by_thread[-1][0] != n:
                by_thread.append((n, []))
            by_thread[-1][1].append(entry)
        for n, entries in by_thread:
            heading = E("h3", None, text_of((self.threads.get(n) or {}).get("name"))) if not groups else \
                E("h3", None, "Other text of ", self.thread_link(n))
            if not groups:
                contents.append(E("li", None, E("a", {"href": anchor("other-%d" % n)}, self.thread_name(n))))
            body.append(E("section", {"class": "sec", "id": "other-%d" % n}, heading, *[draw_part(p) for p in entries]))
            sourced.append(n)
        numbers = []
        for n in dict.fromkeys(sourced):
            sources = (self.threads.get(n) or {}).get("sources")
            if sources:
                numbers.append(self.show(J("Where the numbers of ", self.thread_name(n), " come from"), E("ul", {
                    "class": "srcs"}, *[E("li", None, E("b", None, self.runs(row.get("statement"))), ": ",
                                          self.runs(row.get("source"))) for row in sources])))
        return self.view("text", title, TEXT_LEDE, self.read_line() if self.sheet else self.print_line(), self.plan_summary(),
                         E("nav", {"class": "area-links", "aria-label": "Within this area"}, *[
                           E("a", {"href": anchor(ident)}, text_of(g.get("title")))
                           for g, ident in zip(groups, group_ids)]) if len(groups) > 1 else "",
                         self.versions_block(),
                         E("ul", {"class": "contents", "aria-label": "The parts of the text, in order"}, *contents),
                         *body, E("h3", None, "Where the numbers come from") if numbers else "", *numbers,
                         self.untold_line())

    # ------------------------------------------------------------ the story

    def story_line(self, thread):
        began = thread.get("began")
        return "It began on %s; the latest memory is from %s." % (text_of(began), text_of(thread.get("ended"))) \
            if began else "It holds no memories yet."

    def station(self, memory, mark_id=None):
        return E("li", {"class": "station %s %s%s" % (kind_class(memory), state_class(memory),
                                                        " deadend" if memory.get("side") is True else ""),
                        "id": mark_id}, self.meta(memory, date=True), self.said(memory))

    def divider(self, memory, step, mark_id=None):
        pause = step.get("pause") if isinstance(step.get("pause"), dict) else {}
        after = self.m.get(integer(pause.get("next")))
        return E("li", {"class": "pause", "id": mark_id},
                 E("p", {"class": "pausehead"}, "We paused here on %s." % text_of(pause.get("day"))),
                 E("p", {"class": "pausefocus"}, text_of(pause.get("focus"))),
                 E("p", {"class": "quiet"}, J("The work in this thread picked up again on ",
                                              self.said(after, "inl", text_of(after.get("day"))), ".")
                   if after else "Nothing has been recorded in this thread since this pause.", "  ",
                   self.explain_button("kind-checkpoint", "What is a pause?", "ask")))

    def group_node(self, thread, index, nodes):
        groups = thread.get("groups", [])
        group = groups[index] if 0 <= index < len(groups) and isinstance(groups[index], dict) else {}
        task, opened = integer(group.get("task")), group.get("open") is True
        first, last = text_of(group.get("first")), text_of(group.get("last"))
        span = first if first == last else "%s to %s" % (first, last)
        state = "still open" if opened else ("closed on %s" % text_of(group.get("closed")) if group.get("closed")
                                             else "closed")
        return E("li", {"class": "group fold" + ("" if opened else " closed"),
                        "id": "task-%d" % task if group.get("anchor") is True and task else None},
                 E("button", {"type": "button", "class": "foldbtn", "data-fold": "g%d-%d" % (integer(thread.get("n")), index),
                              "aria-expanded": "true"},
                   E("span", {"class": "gt"}, "While working on: ", text_of(group.get("title")) or "a task"),
                   E("span", {"class": "gm"}, "%s · %s · %s" % (plural(integer(group.get("count")), "1 memory",
                                                                        "%d memories"), span, state))),
                 E("ol", {"class": "chain foldbody"}, *nodes))

    def chain(self, thread):
        n = integer(thread.get("n"))
        steps = [s for s in thread.get("steps", []) if isinstance(s, dict) and integer(s.get("id")) in self.m]
        items, nodes, current = [], [], None

        def flush():
            if nodes and current is not None:
                items.append(self.group_node(thread, current, list(nodes)))
            del nodes[:]

        for at, step in enumerate(steps):
            memory = self.m[integer(step.get("id"))]
            mark_id = "latest-%d" % n if at == len(steps) - 1 else None
            made = []
            conn = step.get("conn")
            if conn:
                label, to = [text_of(conn.get("phrase"))], conn.get("to") or {}
                if integer(to.get("id")) in self.m:
                    label.append(E("a", {"href": anchor("m-%d" % to["id"]), "data-m": to["id"]}, text_of(to.get("day"))))
                elif "thread" in to and integer(to["thread"]) in self.threads:  # the loose thread is number 0
                    label.append(self.thread_link(to["thread"]))
                made.append(E("li", {"class": "conn time" if conn.get("time") else "conn"}, *label))
            if step.get("note"):
                made.append(E("li", {"class": "note"}, CHAIN_NOTE))
            made.append(self.divider(memory, step, mark_id) if memory.get("kind") == "checkpoint"
                        else self.station(memory, mark_id))
            group = step.get("group")
            group = group if isinstance(group, int) and not isinstance(group, bool) else None
            if group != current:
                flush()
                current = group
            if group is None:
                items += made
            else:
                nodes += made
        flush()
        return E("ol", {"class": "chain"}, *items)

    def feeds_line(self, thread):
        links = []
        for feed in thread.get("feeds", []):
            if not isinstance(feed, dict):
                continue
            ordinal = feed.get("ordinal")
            links.append(E("a", {"href": anchor("text-%d-%d" % (integer(thread.get("n")), ordinal))}
                           if isinstance(ordinal, int) and not isinstance(ordinal, bool) else
                           {"href": anchor("sec-%d" % integer(feed.get("n")))}, text_of(feed.get("label"))))
        return links

    @staticmethod
    def join(nodes, last=" and "):
        out = []
        for i, node in enumerate(nodes):
            out += [", " if 0 < i < len(nodes) - 1 else last if i else "", node]
        return out

    def thread_article(self, thread):
        n = integer(thread.get("n"))
        feeds = self.feeds_line(thread)
        opening = thread.get("opening")
        stands = thread.get("stands") if isinstance(thread.get("stands"), dict) else {}
        cards = [E("li", None, E("span", {"class": "lab"}, label), self.card(self.m[integer(stands.get(key))], thread=False))
                 for key, label in (("decision", "The latest decision"), ("result", "The latest result"))
                 if integer(stands.get(key)) in self.m]
        points = [E("li", None, self.said_id(mid)) for mid in stands.get("open", []) if integer(mid) in self.m]
        tasks = [self.task_note(task, with_story=True) for task in stands.get("tasks", []) if isinstance(task, dict)]
        if tasks and thread.get("status_explain") == "thread-clear":
            status = E("span", {"class": "quiet"}, plural(len(tasks), "1 open task", "%d open tasks"))
        else:
            status = self.explain_button(text_of(thread.get("status_explain")), text_of(thread.get("status")),
                                         "chip " + STATE_CLASS.get(thread.get("status_css"), "s-plain"))
        return E("article", {"class": "thread", "id": "thread-%d" % n, "tabindex": "-1"},
                 E("a", {"class": "story-back", "href": anchor("threads")}, "All topic stories"),
                 E("h3", None, text_of(thread.get("name")), " ", status),
                 E("div", {"class": "about"}, E("h4", {"class": "sh"}, "What this thread is about"),
                   E("p", {"class": "aboutsay"}, "This thread is about ", self.runs(self.first_runs(opening["about"])), ".")
                   if opening else E("p", {"class": "aboutsay"}, "Nobody has written what this thread is about yet."),
                   self.blocks(opening.get("why")) if opening else "", E("p", None, self.story_line(thread))),
                 E("div", {"class": "standing"}, E("h4", {"class": "sh"}, "Where it stands"),
                   E("ul", {"class": "latest"}, *cards) if cards else "",
                   E("p", {"class": "lab"}, "Still open here") if points else "",
                   E("ul", {"class": "plainlist"}, *points) if points else "", *tasks,
                   E("p", {"class": "quiet"}, E("span", {"class": "lab"}, "Related output text"), *self.join(feeds)) if feeds else ""),
                 E("h4", {"class": "sh"}, "The story, oldest first"),
                 E("p", None, E("a", {"class": "jump", "href": anchor("latest-%d" % n)}, "Jump to the latest"))
                 if thread.get("steps") else "", self.chain(thread))

    def task_note(self, task, with_story=False):
        if task.get("goal"):
            words = J("We are working on this: ", self.runs(task["goal"]))
        elif task.get("shown") is True:
            words = "An open task has no goal written yet."
        else:
            words = "An open task whose text is not shown here."
        address = github_url("issue", self.repo, task.get("issue"))
        story = with_story and bool(task.get("memories")) and integer(task.get("thread")) in self.threads
        return E("div", {"class": "onote task k-checkpoint"},
                 E("p", {"class": "said"}, words),
                 E("a", {"class": "quiet", "href": anchor("task-%d" % integer(task.get("issue")))}, "See it in the story")
                 if story else "", " " if story else "",
                 E("a", {"class": "quiet", "href": address}, "Open the task on GitHub") if address else "")

    def threads_view(self):
        # The model keeps deliverable order for Outputs. A story's place in History follows its included records.
        stories = []
        for thread in self.threads.values():
            memories = [self.m[mid] for mid in thread.get("memories", []) if mid in self.m]
            latest = max((m["posted"] for m in memories if index.ISO_UTC.fullmatch(text_of(m.get("posted")))), default="")
            stories.append((thread, latest, len(memories)))
        stories.sort(key=lambda row: (text_of(row[0].get("name")).casefold(), integer(row[0].get("n"))))
        stories.sort(key=lambda row: row[1], reverse=True)
        entries = []
        for thread, latest, count in stories:
            words = plural(count, "1 record", "%d records") if count else "No records in this snapshot"
            if count:
                words += " · " + (mind_model.full_day(latest) if latest else "Posting date unknown")
            entries.append(E("li", None, E("div", None, self.thread_link(thread["n"]),
                                            E("span", {"class": "story-aside"}, "Set aside") if thread.get("set_aside") else ""),
                             E("span", {"class": "story-activity"}, words)))
        directory = E("nav", {"class": "story-topics", "aria-label": "Project topics"}, E("ul", None, *entries)) \
            if entries else E("p", {"class": "nothing"}, "No topic stories are included in this snapshot.")
        return self.view("threads", "Topic stories", "Choose a topic to follow its decisions, findings and open questions. "
                         "Topics with the most recently posted records appear first." if entries else None,
                         directory, *[self.thread_article(thread) for thread, _, _ in stories], self.untold_line())

    # ------------------------------------------------------------ lessons, what is settled, what is open

    def lessons_view(self):
        rows = []
        failures = [m for m in self.model.get("memories", []) if m.get("kind") == "failure"]
        for memory in reversed(failures):
            after = [self.m[integer(pair.get("id"))] for pair in memory.get("in", []) if integer(pair.get("id")) in self.m]
            told = {integer(other.get("id")) for other in after}
            if after:
                right = [self.card(other, thread=False) for other in after]
                for other in after:
                    for css, key, lead in (("replaced", "replaces", "That changed again later: "),
                                           ("aside", "instead", "That was set aside. Instead: ")):
                        newer = [pair for pair in other.get("in", []) if pair.get("kind") == key]
                        if other.get("css") == css and newer and integer(newer[0].get("id")) in self.m \
                                and integer(newer[0].get("id")) not in told:  # a decision is told once, not twice
                            right.append(E("p", {"class": "quiet"}, lead, self.said_id(newer[0]["id"])))
            elif memory.get("old") is True:
                thread = self.threads.get(integer(memory.get("thread"))) or {}
                ids = [mid for mid in thread.get("memories", []) if mid in self.m]
                later = ids[ids.index(memory["id"]) + 1:] if memory.get("id") in ids else []
                right = [E("p", {"class": "quiet"}, "What followed was not recorded as a link. The next memory in this "
                           "thread was:")] + ([self.card(self.m[later[0]], thread=False)] if later else [])
            else:
                right = [E("p", {"class": "nothing"}, "Nothing has followed from this yet.")]
            cause = next((entry for entry in memory.get("fields", []) if entry.get("label") == "Cause"), None)
            rows.append(E("li", {"class": "lesson"},
                          E("div", {"class": "wrong"}, E("span", {"class": "lab"}, "What went wrong, in ",
                                                         self.thread_link(memory.get("thread"))),
                            self.said(memory),
                            E("div", {"class": "why"}, E("span", {"class": "lab"}, "Why it happened"),
                              self.blocks(cause.get("doc"))) if cause else ""),
                          E("div", {"class": "arrow", "aria-hidden": "true"}),
                          E("div", {"class": "did"}, E("span", {"class": "lab"}, "What we did about it"), *right)))
        return self.view("lessons", "What we learned the hard way", "Every failure in this mind, newest first, with "
                         "what it led to.", E("ol", {"class": "lessons"}, *rows) if rows else
                         E("p", {"class": "quiet"}, "Nothing has gone wrong in this record yet."), self.untold_line())

    REPLACED_WORD = {"decision": "decision", "result": "finding", "correction": "correction"}

    def standing(self, thread, heading=True):
        """What stands in one thread, with its links to the story and to the text. No text of the topic is here."""
        n = integer(thread.get("n"))
        rests = []
        for rest in thread.get("rests", []):
            memory = self.m.get(integer(rest.get("id")))
            if memory is None:
                continue
            older = [mid for mid in rest.get("replaced", []) if integer(mid) in self.m]
            word = self.REPLACED_WORD.get(memory.get("kind"), "memory")
            extra = E("details", {"class": "older"},
                      E("summary", None, ("It replaced an earlier %s. See it" % word) if len(older) == 1 else
                        "It replaced %d earlier %ss. See them" % (len(older), word)),
                      *[E("p", {"class": "struck"}, self.said_id(mid)) for mid in older]) if older else ""
            rests.append(E("li", {"class": "rest " + kind_class(memory)}, self.mark(memory),
                           E("div", None, self.said(memory), extra)))
        stands = thread.get("stands") if isinstance(thread.get("stands"), dict) else {}
        opened = len([mid for mid in stands.get("open", []) if integer(mid) in self.m])
        nothing = E("p", {"class": "quiet"}, "Nothing here stands yet.", " ",
                    E("a", {"href": anchor("thread-%d" % n)}, plural(opened, "1 point is open", "%d points are open"))
                    if opened else "")
        feeds = self.feeds_line(thread)
        return E("article", {"class": "settled", "id": "settled-%d" % n},
                 E("h4", {"class": "sh"}, self.thread_link(n)) if heading else "",
                 E("p", {"class": "quiet"}, "Also feeds ", *self.join(feeds[1:])) if len(feeds) > 1 else "",
                 E("ul", {"class": "restson"}, *rests) if rests else nothing,
                 E("p", {"class": "partlinks"}, E("a", {"href": anchor("thread-%d" % n)}, "How we got here"),
                   "  " if feeds else "", E("a", {"href": feeds_first(thread, feeds)}, "The text") if feeds else ""))

    def settled_view(self):
        out = []
        groups = [g for g in self.outline.get("deliverables", []) if isinstance(g, dict)] \
            if isinstance(self.outline.get("deliverables"), list) else []
        for group in groups:
            if len(groups) > 1:
                out.append(E("h3", {"class": "deliv"}, text_of(group.get("title"))))
            for section in group.get("sections", []):
                firsts = [self.threads[integer(p.get("thread"))] for p in section.get("parts", [])
                          if isinstance(p, dict) and p.get("first") is True and integer(p.get("thread")) in self.threads]
                if firsts:
                    out.append(E("section", {"class": "setsec"}, E("h3", None, self.section_link(section.get("n"),
                                                                                                   section.get("label"))),
                                 *[self.standing(t) for t in firsts]))
        unplaced = [self.threads[n] for n in self.outline.get("unplaced", []) if integer(n) in self.threads]
        if unplaced:
            out.append(E("section", {"class": "setsec"}, E("h3", None, "Other topics"),
                         *[self.standing(t) for t in unplaced]))
        loose = [t for t in self.model.get("threads", []) if t.get("loose") is True and t.get("rests")]
        if loose:
            out.append(E("section", {"class": "setsec"}, E("h3", None, "About the project as a whole"),
                         *[self.standing(t, heading=False) for t in loose]))
        left = []
        for thread in self.model.get("threads", []):
            if thread.get("set_aside") is True:
                left.append(E("li", None, self.thread_link(thread.get("n")), ". ",
                              self.said_id(thread["aside_by"]) if integer(thread.get("aside_by")) in self.m
                              else "Left out."))
        text_link = E("a", {"href": anchor("text")}, text_of(self.outline.get("title")) or "the text")
        return self.view("settled", "What is settled", J(
            "What we hold today: the decisions, findings and corrections that nothing recorded later replaces or sets "
            "aside. The text they hold up is under ", text_link, "."), *out,
                         E("h3", None, "Set aside"),
                         E("ul", {"class": "leftout"}, *left) if left else
                         E("p", {"class": "quiet"}, "No thread has been set aside."), self.untold_line())

    @staticmethod
    def age(days):
        if not isinstance(days, int) or isinstance(days, bool):
            return ""
        return "open since today" if days == 0 else "open for 1 day" if days == 1 else "open for %d days" % days

    def open_plan_groups(self, opened):
        """The three groups the plan adds to "What is still open?" (none for a project without a plan)."""
        plan = opened.get("plan") if isinstance(opened.get("plan"), dict) else None
        if plan is None:
            return []
        parts = {integer(p.get("id")): p for p in (self.model.get("plan") or {}).get("parts", []) if isinstance(p, dict)}

        def sentence(part_id):
            part = parts.get(integer(part_id))
            return self.plan_link({"gh": part.get("id"), "issue": part.get("issue")}, "%s: %s" % (
                text_of(part.get("part")).capitalize(), text_of(part.get("plain")))) if part else ""

        unbacked = []
        for entry in plan.get("unbacked", []):
            if not isinstance(entry, dict):
                continue
            ideas = [self.said_id(i) for i in entry.get("ideas", [])]
            unbacked.append(E("li", {"class": "onote"}, sentence(entry.get("part")),
                              J(" The experiment we would run: ", *ideas) if ideas else ""))
        doubt = []
        for entry in plan.get("doubt", []):
            if not isinstance(entry, dict):
                continue
            against = [self.said_id(i) for i in entry.get("against", [])]
            doubt.append(E("li", {"class": "onote"}, sentence(entry.get("part")),
                           J(" Against it: ", *against) if against else ""))
        unused = [E("li", {"class": "onote"}, self.said_id(i)) for i in plan.get("unused", [])]
        return [
            self.open_plan_group("plan-unbacked", "Planned claims nothing backs yet", "No finding that still stands is linked "
                                 "to them. Where we thought of an experiment, it is named.", unbacked,
                                 "Every planned claim has a finding behind it."),
            self.open_plan_group("plan-doubt", "Planned claims a finding speaks against", "A finding that still stands was "
                                 "recorded against them.", doubt, "No finding speaks against a planned claim."),
            self.open_plan_group("plan-unused", "Findings the plan does not use", "Recorded since the plan last changed, and "
                                 "linked to no part of it.", unused, "Every recent finding is used by the plan."),
        ]

    def open_view(self):
        opened = self.model.get("open") or {}

        def group(key, title, hint, rows, empty):
            return E("section", {"class": "ogroup", "id": "open-%s" % key}, E("h3", None, title),
                     E("p", {"class": "quiet"}, hint),
                     E("ul", {"class": "notes"}, *rows) if rows else E("p", {"class": "nothing"}, empty))

        def note(entry):
            memory = self.m.get(integer(entry.get("id")))
            if memory is None:
                return ""
            return E("li", {"class": "onote " + kind_class(memory)},
                     E("div", {"class": "meta"}, self.mark(memory), E("span", {"class": "in"},
                                                                      self.thread_link(memory.get("thread"))),
                       E("span", {"class": "age"}, self.age(entry.get("age")))), self.said(memory))

        unknown = []
        for entry in opened.get("unknown", []):
            memory = self.m.get(integer(entry.get("id")))
            if memory is None:
                continue
            after = self.m.get(integer(entry.get("followed")))
            unknown.append(E("li", {"class": "onote k-result"},
                             E("div", {"class": "meta"}, self.mark("result"),
                               E("span", {"class": "in"}, self.thread_link(memory.get("thread")))),
                             E("div", {"class": "said"}, E("span", {"class": "lab"}, "We have not established"),
                               self.blocks(entry.get("doc"))),
                             E("p", {"class": "quiet"}, "From ", self.said(memory, "inl", "what we found on %s"
                                                                           % text_of(memory.get("day"))), ". ",
                               J("A later memory follows from this result: ", self.said(after, "inl", text_of(
                                   after.get("day"))), ".") if after else "")))
        holds = opened.get("holds")
        return self.view(
            "open", "What is still open", "Thoughts nobody has closed. Each one stays here until a later memory answers "
            "it, tries it or sets it aside.",
            group("questions", "Questions without an answer", "Asked, and nothing recorded since answers them. Oldest "
                  "first.", [note(entry) for entry in opened.get("questions", [])], "No question is waiting for an answer."),
            group("ideas", "Ideas we have not tested", "Written down; nothing took them up and no result tested them. "
                  "Oldest first.", [note(entry) for entry in opened.get("ideas", [])],
                  "Every idea has been tested, taken up or set aside."),
            group("failures", "Went wrong, and nothing followed", "Failures that no decision or fix points back to. "
                  "Oldest first.", [note(entry) for entry in opened.get("failures", [])],
                  "Everything that went wrong has been followed up."),
            group("unknown", "What our results do not show", "Said by the results that still stand, in their own words. "
                  "What nothing followed comes first.", unknown, "No result that still stands leaves a gap open."),
            *self.open_plan_groups(opened),
            group("holds", "Waiting for a yes", "From the last pause.",
                  [E("li", {"class": "onote k-checkpoint"}, E("div", {"class": "said"}, self.blocks(holds)))]
                  if holds else [], "Nothing waits for a yes."),
            group("tasks", "Work in progress", "Open tasks, from their goal.",
                  [E("li", None, self.task_note(task, with_story=True)) for task in opened.get("tasks", [])],
                  "No task is open."), self.untold_line())


    # ------------------------------------------------------------ the stores behind the box

    def word_store(self):
        entries = []
        for word in self.model.get("words", []):
            n = integer(word.get("n"))
            if not n:
                continue
            uses = [self.m[mid] for mid in word.get("uses", []) if integer(mid) in self.m]
            first = word.get("first") or {}
            story = [E("li", None, E("span", {"class": "when"}, self.mark(memory), " ", text_of(memory.get("day"))), " ",
                       self.said(memory, cls="inl")) for memory in uses]
            earlier = [E("li", None, E("span", {"class": "when"}, "Until a later memory, from ",
                                       text_of(entry.get("day"))), " we used it to mean ",
                         text_of(entry.get("meaning")), ".") for entry in word.get("earlier", [])]
            in_text = [E("li", None, self.part_link(use)) for use in word.get("in_text", [])
                       if isinstance(use, dict) and isinstance(use.get("ordinal"), int)
                       and integer(use.get("thread")) in self.threads]
            used = {integer(m.get("id")) for m in uses}
            in_fields = [E("li", None, self.said(self.m[mid], cls="inl")) for mid in word.get("in_fields", [])
                         if integer(mid) in self.m and integer(mid) not in used]
            also = [text_of(name) for name in word.get("also", []) if text_of(name)]
            entries.append(E("div", {"class": "entry", "id": "w%d" % n},
                             E("p", {"class": "wt"}, text_of(word.get("word"))),
                             E("p", {"class": "def"}, "We use “", E("b", None, text_of(word.get("word"))),
                               "” to mean ", text_of(word.get("meaning")), "."),
                             E("div", {"class": "why"}, self.blocks(word.get("why"))) if word.get("why") else "",
                             E("p", {"class": "quiet"}, "Also called: ", ", ".join(also), ".") if also else "",
                             E("div", {"class": "more"}, E("p", {"class": "lab"}, "The longer story"),
                               self.blocks(word.get("story"))) if word.get("story") else "",
                             E("div", {"class": "uses"}, E("p", {"class": "lab"}, "What it meant before"),
                               E("ol", None, *earlier)) if earlier else "",
                             E("div", {"class": "uses"},
                               E("p", {"class": "lab"}, "Where it came up (%d), oldest first" % len(story)),
                               E("ol", None, *story)) if story else "",
                             E("div", {"class": "uses"}, E("p", {"class": "lab"}, "In the text:"),
                               E("ol", None, *in_text)) if in_text else "",
                             E("div", {"class": "uses"}, E("p", {"class": "lab"}, "In what other memories say:"),
                               E("ol", None, *in_fields)) if in_fields else "",
                             E("p", {"class": "quiet"}, "First explained on %s" % text_of(first.get("day")),
                               J(", in the thread ", self.thread_link(first["thread"]))
                               if "thread" in first and integer(first["thread"]) in self.threads else "", ".")))
        return E("div", {"id": "words", "hidden": "hidden"}, *entries)

    def explain_store(self):
        """The fixed explanations behind every kind, state and mark, and the box "How to read this page". No record
        text is in it."""
        entries = [E("div", {"class": "entry", "id": "x-%s" % key}, E("p", {"class": "wt"}, title),
                     E("p", {"class": "def"}, text)) for key, title, text in EXPLAIN]
        how = [E("p", {"class": "wt"}, "How to read this page"),
               E("p", {"class": "def"}, "Every sentence here comes from what the project recorded. Words that explain "
                                        "themselves are buttons: choose one to read what it means."),
               E("p", {"class": "lab"}, "Project areas"),
               *[E("p", None, E("a", {"href": anchor(go)}, name), ": ", words)
                 for go, name, words in self.navigation_guide()],
               *[E("p", None, E("b", None, name), ": ", text) for name, text in HOW_TO_READ],
               E("p", {"class": "lab"}, "What each kind of memory records"),
               E("ul", {"class": "plainlist"}, *[E("li", None, E("b", None, word), ": ", text)
                                                   for _, word, text in KIND_TABLE]),
               E("p", {"class": "lab"}, "What each state means"),
               E("ul", {"class": "plainlist"}, *[E("li", None, E("b", None, title), ": ", text)
                                                   for _, _, title, text in STATE_TABLE]),
               E("p", {"class": "lab"}, "What a thread's mark means"),
               E("ul", {"class": "plainlist"}, *[E("li", None, E("b", None, title), ": ", text)
                                                   for _, title, text in THREAD_TABLE])]
        return E("div", {"id": "explain", "hidden": "hidden"}, *entries, E("div", {"class": "entry", "id": "x-how"}, *how))


    def issue_store(self):
        entries = []
        for issue in self.model.get("cited", []):
            n = integer(issue.get("n"))
            if not n:
                continue
            who = text_of(issue.get("author")) or mind_model.PHRASES["account"]
            state = "still open" if issue.get("open") is True else (
                "closed on %s" % text_of(issue.get("closed")) if issue.get("closed") else "closed")
            address = github_url("pull" if issue.get("pull") is True else "issue", self.repo, n)
            entries.append(E("div", {"class": "entry", "id": "issue-%d" % n},
                             E("p", {"class": "wt"}, text_of(issue.get("title"))),
                             E("p", {"class": "quiet"}, "Opened by ", E("span", {"class": "people"}, who),
                               " on %s; %s." % (text_of(issue.get("opened")), state)),
                             E("div", {"class": "why"}, self.blocks(issue.get("first"))) if issue.get("first") else "",
                             E("p", None, E("a", {"href": address}, "Open the whole issue on GitHub") if address else "",
                               " " if address else "", self.record_link(n))))
        return E("div", {"id": "issues", "hidden": "hidden"}, *entries)

    def tree_topic(self, entry):
        n = integer(entry.get("thread"))
        thread = self.threads.get(n)
        if not thread:
            return ""
        branches = []
        for key, title in (("found", "We found"), ("decided", "We decided"), ("open", "Still open")):
            data = entry.get(key) if isinstance(entry.get(key), dict) else {}
            count = integer(data.get("count"))
            ids = [mid for mid in data.get("ids", []) if integer(mid) in self.m]
            if not count:
                continue
            items = [E("li", None, self.said(self.m[integer(mid)], "inl")) for mid in ids]
            if count > len(ids):
                items.append(E("li", None, E("a", {"href": anchor("thread-%d" % n)},
                                             "and %d more, in the story" % (count - len(ids)))))
            branches.append(E("li", {"class": "fold closed lf"},
                              E("button", {"type": "button", "class": "foldbtn", "data-fold": "b%d-%s" % (n, key),
                                           "aria-expanded": "true"}, "%s (%d)" % (title, count)),
                              E("ul", {"class": "foldbody"}, *items)))
        return E("li", {"class": "fold tp"},
                 E("button", {"type": "button", "class": "foldbtn", "data-fold": "t%d" % n, "aria-expanded": "true"},
                   text_of(thread.get("name"))), " ",
                 self.explain_button(text_of(thread.get("status_explain")), text_of(thread.get("status")),
                                     "chip " + STATE_CLASS.get(thread.get("status_css"), "s-plain")), " ",
                 E("a", {"class": "quiet", "href": anchor("thread-%d" % n)}, "the story"),
                 E("ul", {"class": "foldbody"}, *branches) if branches else "")

    def tree_plan(self, data, number):
        """The branch "We plan to say (4)" under a section of the map: the planned parts as sentences."""
        if not isinstance(data, dict) or not integer(data.get("count")):
            return ""
        parts = {integer(p.get("id")): p for p in (self.model.get("plan") or {}).get("parts", []) if isinstance(p, dict)}
        items = []
        for pid in data.get("ids", []):
            part = parts.get(integer(pid))
            if part:
                items.append(E("li", None, self.plan_link({"gh": part.get("id"), "issue": part.get("issue")}, "%s: %s" % (
                    text_of(part.get("part")).capitalize(), text_of(part.get("plain"))))))
        if integer(data.get("count")) > len(items):
            items.append(E("li", None, E("a", {"href": anchor("sec-%d" % integer(number))}, "and %d more, in the text" % (
                integer(data.get("count")) - len(items)))))
        return E("li", {"class": "fold closed lf"},
                 E("button", {"type": "button", "class": "foldbtn", "data-fold": "p%s" % (
                     integer(number) if number is not None else "x"), "aria-expanded": "true"},
                   "We plan to say (%d)" % integer(data.get("count"))), E("ul", {"class": "foldbody"}, *items))

    def tree_side(self, sections, cls):
        out = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            number = section.get("n")
            out.append(E("li", {"class": "fold sc"},
                         E("button", {"type": "button", "class": "foldbtn", "data-fold": "s%s" % (
                             integer(number) if number is not None else "x"), "aria-expanded": "true"},
                           text_of(section.get("label"))), " ",
                         E("a", {"class": "quiet", "href": anchor("sec-%d" % integer(number))}, "its text")
                         if number is not None else "",
                         E("ul", {"class": "foldbody"}, self.tree_plan(section.get("plan"), number),
                           *[self.tree_topic(t) for t in section.get("topics", []) if isinstance(t, dict)])))
        return E("ul", {"class": "tside " + cls}, *out)

    def tree_pane(self):
        tree = self.model.get("tree") if isinstance(self.model.get("tree"), dict) else {}
        question = tree.get("question")
        middle = [E("p", {"class": "lab"}, "What we are finding out" if question else "The project"),
                  E("div", {"class": "question"}, self.blocks(question)) if question else
                  E("p", {"class": "question"}, text_of(self.model.get("name")))]
        aside = [self.threads[integer(n)] for n in tree.get("aside", []) if integer(n) in self.threads]
        extra = []
        if aside:
            extra.append(E("li", {"class": "fold closed sc"},
                           E("button", {"type": "button", "class": "foldbtn", "data-fold": "aside", "aria-expanded": "true"},
                             "Set aside (%d)" % len(aside)),
                           E("ul", {"class": "foldbody"}, *[E("li", None, self.thread_link(t.get("n"))) for t in aside])))
        other = tree.get("other") if isinstance(tree.get("other"), dict) else None
        if other:
            extra.append(E("li", {"class": "fold sc"},
                           E("button", {"type": "button", "class": "foldbtn", "data-fold": "other", "aria-expanded": "true"},
                             mind_model.PHRASES["thread.loose"]),
                           E("ul", {"class": "foldbody"}, self.tree_topic(other))))
        return E("div", {"class": "tree"},
                 E("div", {"class": "treebar"}, E("button", {"type": "button", "data-fold": "open-all"}, "Open everything"),
                   E("button", {"type": "button", "data-fold": "close-all"}, "Close everything")),
                 E("div", {"class": "treecols", "aria-label": "The map of what we know"},
                   self.tree_side(tree.get("left", []), "tleft"),
                   E("div", {"class": "hubnode"}, *middle, E("ul", {"class": "tside tmid"}, *extra) if extra else ""),
                   self.tree_side(tree.get("right", []), "tright")))

    def time_pane(self):
        layout = self.model.get("map")
        if not layout:
            return E("p", {"class": "quiet"}, "There is nothing to draw yet.")
        width, height, top = integer(layout.get("width"), 1080), integer(layout.get("height"), 300), \
            integer(layout.get("top"), 64)
        parts = []
        for tick in layout.get("ticks", []):
            x = coordinate(tick.get("x"))
            parts.append(E("line", {"class": "tick", "x1": "%.1f" % x, "x2": "%.1f" % x, "y1": top - 30,
                                    "y2": height - 26}))
            parts.append(E("text", {"class": "axis", "x": "%.1f" % x, "y": height - 8, "text-anchor": "middle"},
                           text_of(tick.get("day"))))
        for pause in layout.get("pauses", []):
            x = coordinate(pause.get("x"))
            parts.append(E("line", {"class": "pause", "x1": "%.1f" % x, "x2": "%.1f" % x, "y1": top - 34,
                                    "y2": height - 26}))
            if pause.get("label") is True:
                parts.append(E("text", {"class": "pauselab", "x": "%.1f" % x, "y": top - 40, "text-anchor": "middle"},
                               "pause"))
        for row in layout.get("rows", []):
            y = coordinate(row.get("y"))
            css = {"aside": "r-aside", "open": "r-open", "stands": "r-stands"}.get(row.get("status_css"), "r-stands")
            parts.append(E("path", {"class": "river " + css, "d": "M%.1f %.1f H%.1f" % (
                coordinate(row.get("x1")), y, coordinate(row.get("x2")))}))
            n = row.get("n")
            if integer(n) in self.threads and n is not None:
                thread = self.threads[integer(n)]
                parts.append(E("a", {"href": anchor("thread-%d" % integer(n))}, E("title", None, text_of(row.get("name"))),
                               E("text", {"class": "lane", "x": 12, "y": "%.1f" % (y - 2)},
                                 text_of(row.get("name_short")) or text_of(row.get("name"))),
                               E("text", {"class": "lanestate", "x": 12, "y": "%.1f" % (y + 15)},
                                 text_of(thread.get("status")).lower())))
            else:
                parts.append(E("text", {"class": "lane", "x": 12, "y": "%.1f" % (y - 2)},
                               plural(integer(row.get("folded")), "1 other thread", "%d other threads")))
        for dot in layout.get("dots", []):
            if dot.get("side") is True:
                x, y, row_y = coordinate(dot.get("x")), coordinate(dot.get("y")), coordinate(dot.get("row_y"))
                parts.append(E("path", {"class": "branch", "d": "M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f" % (
                    x - 20, row_y, x - 8, row_y, x - 12, y, x, y)}))
        for link in layout.get("links", []):
            x1, y1, x2, y2 = (coordinate(link.get(k)) for k in ("x1", "y1", "x2", "y2"))
            if abs(y1 - y2) < 1:
                lift = min(34, 10 + (x2 - x1) * 0.12)
                d = "M%.1f %.1f Q%.1f %.1f %.1f %.1f" % (x1, y1 - 9, (x1 + x2) / 2, y1 - 9 - lift, x2, y2 - 9)
            else:
                d = "M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f" % (x1, y1, (x1 + x2) / 2, y1, (x1 + x2) / 2, y2, x2, y2)
            parts.append(E("path", {"class": "link " + LINK_CLASS.get(link.get("kind"), "l-because"), "d": d,
                                    "data-a": integer(link.get("a")), "data-b": integer(link.get("b"))}))
        for dot in layout.get("dots", []):
            memory = self.m.get(integer(dot.get("id")))
            if memory is None:
                continue
            x, y, kind = coordinate(dot.get("x")), coordinate(dot.get("y")), memory.get("kind")
            if kind in ("decision", "correction"):
                shape = E("rect", {"x": "%.1f" % (x - 6), "y": "%.1f" % (y - 6), "width": 12, "height": 12,
                                   "transform": "rotate(45 %.1f %.1f)" % (x, y)})
            elif kind == "failure":
                shape = E("path", {"d": "M%.1f %.1f l12 12 m0 -12 l-12 12" % (x - 6, y - 6), "class": "x"})
            elif kind == "idea":
                shape = E("polygon", {"points": "%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (x, y - 8, x + 8, y + 6, x - 8,
                                                                                   y + 6)})
            elif kind == "question":
                shape = E("circle", {"cx": "%.1f" % x, "cy": "%.1f" % y, "r": 6, "class": "ring"})
            else:
                shape = E("circle", {"cx": "%.1f" % x, "cy": "%.1f" % y, "r": 7})
            mid = integer(memory.get("id"))
            parts.append(E("a", {"href": anchor("m-%d" % mid), "data-m": mid,
                                 "class": "dot %s %s" % (kind_class(memory), state_class(memory))},
                           E("title", None, text_of(memory.get("passage"))),
                           E("circle", {"class": "hit", "cx": "%.1f" % x, "cy": "%.1f" % y, "r": 13}), shape))
        legend = E("ul", {"class": "legend"},
                   *[E("li", None, self.mark(k), KIND_WORD[k]) for k in ("result", "decision", "failure", "idea",
                                                                          "question", "correction")],
                   E("li", None, E("span", {"class": "ln pauseln"}), "a pause: the work stopped and a note was left"),
                   E("li", None, E("span", {"class": "ln l-because"}), "so"),
                   E("li", None, E("span", {"class": "ln l-replaces"}), "replaced"),
                   E("li", None, E("span", {"class": "ln l-instead"}), "set aside; instead"),
                   E("li", None, E("span", {"class": "ln l-checks"}), "tried, checked or answered"),
                   E("li", None, E("span", {"class": "fade"}), "faded: replaced or set aside"))
        return J(E("p", {"class": "lede"}, "Each line is a thread, running left to right through time. A branch that drops "
                   "away is something we set aside. Point at a mark, or choose it, to read it."),
                 E("p", {"class": "quiet hintnarrow"}, "Scroll sideways to follow time."),
                         E("div", {"class": "mapbox"},
                           E("svg", {"viewBox": "0 0 %d %d" % (width, height), "role": "img", "class": "mindmap",
                                     "aria-label": "Map of the threads of this mind over time"}, *parts)),
                         E("p", {"class": "reading", "id": "reading", "aria-live": "polite"},
                           "Point at a mark on the map to read that memory here."), legend)

    def map_view(self):
        return self.view(
            "map", "The whole mind", "Two pictures of the same record: what we know, as a tree, and the threads over time.",
            E("div", {"class": "switch", "aria-label": "Which map to show"},
              E("button", {"type": "button", "class": "on", "data-fold": "pane-know"}, "What we know"),
              E("button", {"type": "button", "data-fold": "pane-time"}, "Over time")),
            E("div", {"class": "mappane on", "id": "map-know"}, self.tree_pane()),
            E("div", {"class": "mappane", "id": "map-time"}, self.time_pane()), self.untold_line())

    def ask_view(self):
        chips = (("decided", "What have we decided?"), ("wrong", "What went wrong?"), ("open", "What is still open?"),
                 ("changed", "Where did we change our mind?"))
        return self.view("ask", "Search memories", "Match words in the included memories and their evidence. "
                         "Task steps and output text are not searched here.",
                         E("div", {"class": "askbar"},
                           E("input", {"type": "search", "id": "q", "placeholder": "For example: memory, quote, speed",
                                       "aria-label": "Words to look for", "autocomplete": "off"})),
                         E("div", {"class": "qchips"}, *[E("button", {"type": "button", "data-ask": key}, label)
                                                          for key, label in chips]),
                         E("div", {"id": "answers", "aria-live": "polite"}), self.untold_line())

    # ------------------------------------------------------------ the usage view

    @staticmethod
    def whole(value):
        """A count the report gave, as a whole number from 0 to 10^9; anything else is 0."""
        return min(USAGE_LIMIT, max(0, integer(value)))

    @classmethod
    def count(cls, value):
        return "{:,}".format(cls.whole(value))

    @staticmethod
    def percent(used, limit):
        return min(100, (100 * used) // limit) if limit > 0 else 0

    @staticmethod
    def meter(used, limit):
        """The meter of the manuscript view, from two whole numbers: a bar of 100 units, filled to used / limit."""
        width = Page.percent(used, limit)
        return E("svg", {"class": "umeter", "viewBox": "0 0 100 4", "preserveAspectRatio": "none", "aria-hidden": "true"},
                 E("rect", {"class": "mbg", "width": 100, "height": 4}),
                 E("rect", {"class": "mfg hot" if width >= 90 else "mfg", "width": width, "height": 4}))

    @staticmethod
    def table_of(heads, rows, numeric_from=0):
        """A table of fixed column heads and cells that are text; the columns from `numeric_from` on are numbers."""
        def cell(tag, position, content):
            return E(tag, {"class": "num" if position >= numeric_from else None, "scope": "col" if tag == "th" else None}, content)
        return E("div", {"class": "tbox", "tabindex": "0", "role": "region", "aria-label": "A table"},
                 E("table", None, E("thead", None, E("tr", None, *[cell("th", n, head) for n, head in enumerate(heads)])),
                 E("tbody", None, *[E("tr", None, *[cell("td", n, value) for n, value in enumerate(row)]) for row in rows])))

    def usage_today(self, tiers):
        out = []
        for entry in tiers:
            tier = entry.get("tier") if isinstance(entry, dict) else None
            words = dict(USAGE_TIERS).get(tier)
            if words is None:
                continue
            model = text_of(entry.get("model"))
            model = model if USAGE_MODEL.fullmatch(model) else ""
            calls, cap, tokens, budget = (self.whole(entry.get(key)) for key in ("calls", "cap", "tokens", "budget"))
            name = [words, " (", model, ")"] if model else [words, " (not turned on)" if tier == "strong" else " (no model chosen)"]
            out.append(E("div", {"class": "utier"}, E("p", {"class": "uname"}, *name),
                         E("p", {"class": "quiet"}, "%s of %s calls" % (self.count(calls), self.count(cap))),
                         self.meter(calls, cap),
                         E("p", {"class": "quiet"}, "%s of %s tokens" % (self.count(tokens), self.count(budget))),
                         self.meter(tokens, budget)))
        return out

    def usage_period(self, days, rows):
        table = []
        for row in rows if isinstance(rows, list) else []:
            tier, job = (row.get("tier"), row.get("job")) if isinstance(row, dict) else (None, None)
            if tier not in dict(USAGE_TIERS) or job not in USAGE_JOBS:
                continue
            table.append((dict(USAGE_TIERS)[tier], USAGE_JOBS[job], self.count(row.get("calls")), self.count(row.get("input")),
                          self.count(row.get("output"))))
        return [E("h3", None, "The last %d days" % days),
                self.table_of(("Tier", "Job", "Calls", "Input tokens", "Output tokens"), table, 2) if table
                else E("p", {"class": "quiet"}, "No background calls in this time.")]

    def usage_bars(self, days):
        """One bar a day for 30 days, split by tier, drawn from the report's whole numbers; the same numbers follow as a table."""
        lines = [line for line in days if isinstance(line, dict) and USAGE_DAY.fullmatch(text_of(line.get("day")))][-30:]
        totals = [sum(self.whole(line.get(tier)) for tier, _ in USAGE_TIERS) for line in lines]
        if not lines or not max(totals):
            return []
        top, shapes = max(totals), []
        for place, line in enumerate(lines):
            floor = 60
            for tier, _ in USAGE_TIERS:
                height = (60 * self.whole(line.get(tier))) // top
                if height:
                    floor -= height
                    shapes.append(E("rect", {"class": "bar-" + tier, "x": place * 10 + 1, "y": floor, "width": 8,
                                             "height": height}))
        table = [(text_of(line["day"]),) + tuple(self.count(line.get(tier)) for tier, _ in USAGE_TIERS) for line in lines]
        return [E("h3", None, "Tokens a day, for 30 days"),
                E("svg", {"class": "ubars", "viewBox": "0 0 %d 60" % (10 * len(lines)), "preserveAspectRatio": "none",
                          "aria-hidden": "true"}, *shapes),
                E("p", {"class": "ukey quiet"}, *[E("span", {"class": "key-" + tier}, words) for tier, words in USAGE_TIERS]),
                E("details", {"class": "ev"}, E("summary", None, "The numbers for each day"),
                  E("div", {"class": "evbody"}, self.table_of(("Day",) + tuple(words for _, words in USAGE_TIERS), table, 1)))]

    def usage_agents(self, agents):
        seven, thirty = (agents.get(key) if isinstance(agents.get(key), list) else [] for key in ("7", "30"))
        found = {}
        for slot, rows in ((0, seven), (1, thirty)):
            for row in rows:
                name = row.get("agent") if isinstance(row, dict) else None
                if name in USAGE_AGENTS:
                    found.setdefault(name, [(0, 0), (0, 0)])[slot] = (self.whole(row.get("runs")),
                                                                      self.whole(row.get("input")) + self.whole(row.get("output")))
        table = [(USAGE_AGENTS[name], self.count(pair[0][0]), self.count(pair[0][1]), self.count(pair[1][0]),
                  self.count(pair[1][1])) for name, pair in sorted(found.items())]
        return [E("h3", None, "The plugin's subagents"),
                self.table_of(("Subagent", "Runs, 7 days", "Tokens, 7 days", "Runs, 30 days", "Tokens, 30 days"), table, 1)
                if table else E("p", {"class": "quiet"}, "No run of the plugin's subagents has been measured on this machine.")]

    def usage_view(self):
        data = self.model.get("usage") if isinstance(self.model.get("usage"), dict) else {}
        built = text_of(data.get("built"))
        lede = USAGE_LEDE % (built if USAGE_BUILT.fullmatch(built) else "an unknown time")
        if not data or data.get("empty") is True:
            return self.view("usage", "What did recortex use?", lede, E("p", {"class": "usage-empty"}, USAGE_EMPTY),
                             E("h3", None, "Not measured"), E("p", None, usage.NOT_MEASURED))
        state = data.get("observer")
        parts = []
        if state == "on":
            parts += [E("h3", None, "Today, in the last 24 hours")] + self.usage_today(
                data.get("tiers") if isinstance(data.get("tiers"), list) else [])
        else:
            parts.append(E("p", None, USAGE_PAUSED if state == "paused" else USAGE_OFF))
        periods = data.get("periods") if isinstance(data.get("periods"), dict) else {}
        if state == "on":
            parts += self.usage_period(7, periods.get("7")) + self.usage_period(30, periods.get("30"))
            parts += self.usage_bars(data.get("days") if isinstance(data.get("days"), list) else [])
        largest = []
        for entry in data.get("largest", []) if isinstance(data.get("largest"), list) else []:
            if not isinstance(entry, dict) or not self.whole(entry.get("tokens")):
                continue
            kind, name = entry.get("kind"), text_of(entry.get("name"))
            if kind == "agent" and name in USAGE_AGENTS:
                words = USAGE_AGENTS[name]
            elif kind == "observer" and name.split(" ")[0] in dict(USAGE_TIERS) and name.split(" ")[-1] in USAGE_JOBS \
                    and len(name.split(" ")) == 2:
                words = "%s, %s" % (dict(USAGE_TIERS)[name.split(" ")[0]], USAGE_JOBS[name.split(" ")[1]])
            else:
                continue
            largest.append(E("li", None, "%s: %s tokens" % (words, self.count(entry.get("tokens")))))
        if largest:
            parts += [E("h3", None, "The largest users this week"), E("ol", {"class": "plainlist"}, *largest[:5])]
        parts += self.usage_agents(data.get("agents") if isinstance(data.get("agents"), dict) else {})
        estimated = []
        for entry in data.get("estimated", []) if isinstance(data.get("estimated"), list) else []:
            event = entry.get("event") if isinstance(entry, dict) else None
            if event in USAGE_EVENTS:
                times = self.whole(entry.get("count"))
                estimated.append(E("li", None, "%s: %s, about %s tokens" % (
                    USAGE_EVENTS[event], plural(times, "1 time", "%d times"), self.count(entry.get("tokens")))))
        if estimated:
            parts += [E("h3", None, "Added to your sessions by the hooks"),
                      E("p", {"class": "quiet"}, "Estimated at 4 characters a token, for the last 7 days."),
                      E("ul", {"class": "plainlist"}, *estimated)]
        parts += [E("h3", None, "Not measured"), E("p", None, usage.NOT_MEASURED)]
        return self.view("usage", "What did recortex use?", lede, *parts)

    # ------------------------------------------------------------ the to-do view

    @staticmethod
    def step_id(issue, step):
        identity = (integer(issue), integer(step.get("comment")), integer(step.get("ordinal")))
        return "work-step-%d-%d-%d" % identity if all(identity) else None

    def todo_task(self, task, closed=False):
        n = integer(task.get("issue"))
        title = text_of(task.get("title"))
        address = github_url("issue", self.repo, n)
        counts = task.get("counts") if isinstance(task.get("counts"), dict) else {}
        steps, done = self.whole(counts.get("steps")), self.whole(counts.get("done"))
        head = E("h3", {"class": "tname"}, title or "A task whose title is not told here")
        line = "%d of %d steps done" % (done, steps) if steps else "No steps yet"
        day = text_of(task.get("closed"))
        if closed and USAGE_DAY.fullmatch(day):
            line += ", closed on %s" % day
        body = [head, E("p", {"class": "quiet"}, line, J(" ", E("a", {"class": "lk", "href": address}, "Open it on GitHub"))
                                                          if address else "")]
        if steps and not closed:
            body.append(self.meter(done, steps))
        if task.get("hand") is True and not closed:
            body.append(E("p", {"class": "quiet"}, TODO_HAND))
        items = []
        for step in task.get("steps", []) if isinstance(task.get("steps"), list) and not closed else []:
            if not isinstance(step, dict) or step.get("state") not in TODO_STATES:
                continue
            state = step["state"]
            made = step.get("by") if isinstance(step.get("by"), dict) else None
            by = ""
            if made is not None and integer(made.get("id")):
                words = text_of(made.get("plain")) or "A memory"
                if integer(made["id"]) in self.m:
                    by = E("a", {"class": "lk", "href": anchor("m-%d" % integer(made["id"])), "data-m": integer(made["id"])}, words)
                else:
                    address = github_url("memory", self.repo, made.get("issue"), made.get("id"))
                    by = E("a", {"class": "lk", "href": address}, words) if address else words
            items.append(E("li", {"class": "step s-" + state, "id": self.step_id(n, step)}, E("span", {"class": "sk"}, TODO_STATES[state]),
                           E("span", {"class": "sbody"}, E("span", {"class": "stx"}, text_of(step.get("text"))),
                             E("span", {"class": "by"}, E("span", {"class": "quiet"}, "Marked by: "), by) if by != "" else "",
                             E("a", {"class": "lk", "href": Href(self.action_root + "#step-%d-%d-%d" % (n, integer(step.get("comment")), integer(step.get("ordinal"))))},
                               "Review task action") if self.action_root and state in ("open", "blocked")
                             and n and integer(step.get("comment")) and integer(step.get("ordinal")) else "")))
        if items:
            body.append(E("ol", {"class": "steps"}, *items))
        return E("section", {"class": "utask", "id": "work-task-%d" % n}, *body)

    def todo_view(self):
        data = self.task_data()
        opened = [t for t in data.get("open", []) if isinstance(t, dict)] if isinstance(data.get("open"), list) else []
        closed = [t for t in data.get("closed", []) if isinstance(t, dict)] if isinstance(data.get("closed"), list) else []
        parts = [self.todo_task(task) for task in opened] or [E("p", {"class": "usage-empty"},
                 "Work could not be read. Sync the record and rebuild the page." if data.get("available") is False else TODO_NONE)]
        if self.action_root:
            parts.insert(0, E("p", {"class": "quiet"}, E("a", {"href": Href(self.action_root)}, "Open task actions"),
                              ". Actions are checked against the current record before they are saved."))
        if closed:
            parts += [E("h3", None, "Closed in the last 30 days")] + [self.todo_task(task, True) for task in closed]
        acts = []
        for act in self.model.get("acts", []) if isinstance(self.model.get("acts"), list) else []:
            kind, day = act.get("kind") if isinstance(act, dict) else None, text_of(act.get("day")) if isinstance(act, dict) else ""
            if kind not in TODO_ACTS or not USAGE_DAY.fullmatch(day):
                continue
            address = github_url("memory", self.repo, act.get("issue"), act.get("comment")) if integer(act.get("comment")) \
                else github_url("issue", self.repo, act.get("issue")) if integer(act.get("issue")) else None
            acts.append(E("li", None, "%s, %s" % (TODO_ACTS[kind], day), " (undone)" if act.get("undone") is True else "",
                          J(" ", E("a", {"href": address}, "Open it on GitHub")) if address else ""))
        if acts:
            parts += [E("h3", None, "What the observer did in the last 7 days"), E("ul", {"class": "plainlist"}, *acts)]
        return self.view("todo", "What is there to do?", TODO_LEDE, *parts)

    # ------------------------------------------------------------ the reader (1.9.0)

    def reader_data(self):
        data = self.model.get("reader")
        return data if isinstance(data, dict) and isinstance(data.get("topics"), list) else None

    def read_href(self, n, entry=None):
        """Where a topic's view (or one of its entries) is read: inside the page, or at the live server's own route. Never a file
        address and never a path."""
        if self.live_base and not self.reader_here(n):
            return Href("%s/read/%d%s" % (self.live_base, n, "#read-%d-e%d" % (n, entry) if entry else ""))
        return anchor("read-%d-e%d" % (n, entry) if entry else "read-%d" % n)

    def reader_here(self, n):
        """True when the view of topic `n` is on this page."""
        data = self.reader_data()
        return bool(data) and data.get("mode") == "views" and any(
            integer(t.get("n")) == n and (t.get("entries") is not None and "entries" in t) for t in data["topics"])

    def reader_has(self, n):
        data = self.reader_data()
        return bool(data) and any(integer(t.get("n")) == n for t in data["topics"])

    def record_link(self, n, entry=None, text="Read it in the record"):
        """The link that opens a topic (or an entry) in the reader, or "" when the record the page tells has no such topic."""
        if not self.reader_has(n):
            return ""
        return E("a", {"href": self.read_href(n, entry)}, text)

    def record_home(self):
        return Href(self.live_base + "/snapshot#record") if self.live_base else anchor("record")

    def kind_words(self, kind):
        return "a note" if kind in (None, "other") or not isinstance(kind, str) else kind.capitalize() if kind != "text" else "A text change"

    def record_view(self):
        """The Record pane: one row for each topic (the hub first), with the counts and the notices above the list."""
        data = self.reader_data()
        if not data:
            return ""
        topics = sorted((t for t in data["topics"] if isinstance(t, dict) and integer(t.get("n"))),
                        key=lambda t: (not t.get("hub"), integer(t.get("n"))))
        counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
        folded = integer(counts.get("folded"))
        line = plural(len(topics), "1 topic", "%d topics") + ", " + plural(integer(counts.get("entries")), "1 entry", "%d entries")
        if folded:
            line += ", %d shut under a mark" % folded
        children = [E("p", {"class": "quiet"}, line + ".")]
        if folded or any(t.get("body_mark") for t in topics):
            children.append(E("p", {"class": "quiet"}, "An entry or a text that recortex does not count as your words is shut under a "
                                                         "mark that says why. Open it to read it."))
        if data.get("local") and integer(data.get("unread")):
            children.append(E("p", {"class": "quiet"}, mind_reader.UNREAD % integer(data.get("unread"))))
        rows = []
        for topic in topics:
            n = integer(topic.get("n"))
            cell = [E("a", {"href": self.read_href(n)}, text_of(topic.get("title")) or "Topic %d" % n)]
            labels = [E("span", {"class": "chip"}, text_of(label)) for label in topic.get("labels", [])[:6] if text_of(label)]
            state = "closed on %s" % text_of(topic.get("closed")) if topic.get("state") == "closed" and topic.get("closed") else \
                text_of(topic.get("state")) or "open"
            entries = plural(integer(topic.get("count")), "1 entry", "%d entries")
            if integer(topic.get("folded")):
                entries += ", %d folded" % integer(topic.get("folded"))
            rows.append(E("tr", None, E("td", None, "#%d" % n), E("td", None, J(*cell), " ", J(*labels)), E("td", None, state),
                          E("td", None, entries), E("td", None, text_of(topic.get("updated")))))
        table = E("table", {"class": "recordlist"},
                  E("thead", None, E("tr", None, *[E("th", {"scope": "col"}, name) for name in ("Topic", "Title", "State", "Entries",
                                                                                                "Last change")])),
                  E("tbody", None, *rows))
        return self.view("record", "Record", "Every topic of the record, whole: its text, the earlier versions of its text, and "
                                              "every entry in the order it was written.", *children, table)

    def read_views(self):
        """The reading views the page holds: every topic's (the saved page), the one that was asked for (the live route), or none."""
        data = self.reader_data()
        if not data or data.get("mode") != "views":
            return ""
        return J(*[self.read_view(t) for t in data["topics"] if isinstance(t, dict) and integer(t.get("n"))
                   and (t.get("entries") is not None and "entries" in t)])

    def read_view(self, topic):
        n = integer(topic.get("n"))
        ident = "read-%d" % n
        local = bool((self.reader_data() or {}).get("local"))
        title = text_of(topic.get("title")) or "Topic %d" % n
        state = "Closed on %s" % text_of(topic.get("closed")) if topic.get("state") == "closed" and topic.get("closed") else "Open"
        chips = [E("span", {"class": "chip"}, state)]
        chips += [E("span", {"class": "chip"}, text_of(label)) for label in topic.get("labels", [])[:12] if text_of(label)]
        chips.append(E("span", {"class": "chip"}, "Last change " + text_of(topic.get("updated"))))
        if self.live:
            chips.append(E("span", {"class": "chip s-open"}, "Live"))
        address = None if local else github_url("issue", self.repo, n)
        chips.append(E("span", {"class": "chip"}, "Cite it as #%d" % n) if local else
                     E("a", {"class": "chip", "href": address}, "Open the whole issue on GitHub") if address else "")
        trail = E("p", {"class": "trail"}, E("a", {"href": self.record_home()}, "Record"), " / #%d" % n)
        if topic.get("capped"):
            return E("section", {"class": "view", "id": ident, "aria-labelledby": ident + "-h"}, trail,
                     E("h2", {"id": ident + "-h"}, title), E("p", {"class": "chips"}, *chips),
                     E("p", {"class": "markword"}, text_of(topic.get("body_mark"))) if topic.get("body_mark") else
                     E("div", {"class": "readtext"}, self.blocks(topic.get("first"))),
                     E("p", {"class": "quiet"}, "The full text of this topic is in the live dashboard."))
        parts, contents = [], []
        # the current text
        body_mark = text_of(topic.get("body_mark"))
        text = self.blocks(topic.get("body"), cut_to=address, issue=n) if topic.get("body") else E("p", {"class": "quiet"},
                                                                                                 "This topic has no text yet.")
        if body_mark:
            text = E("details", {"class": "readfold"}, E("summary", None, E("span", {"class": "markword"}, body_mark),
                                                         " Shut. Open it to read it."), E("div", {"class": "readtext"}, text))
        else:
            text = E("div", {"class": "readtext"}, text)
        parts.append(E("section", {"class": "readpart", "id": ident + "-text"}, E("h3", None, "Current text"), text))
        contents.append((ident + "-text", "Current text"))
        # the earlier versions of the text
        versions = [v for v in topic.get("versions", []) if isinstance(v, dict)] if isinstance(topic.get("versions"), list) else []
        edits = [text_of(at) for at in topic.get("edits", [])] if isinstance(topic.get("edits"), list) else []
        past = []
        for version in versions:
            mark = text_of(version.get("mark"))
            past.append(E("details", {"class": "readfold" if mark else "readold"},
                          E("summary", None, text_of(version.get("at")), " ", E("span", {"class": "markword"}, mark) if mark else ""),
                          E("div", {"class": "readtext"}, self.blocks(version.get("doc")))))
        if not versions and not local and edits:
            past.append(E("p", None, "The text was edited on " + ", ".join(edits[:12]) + ("." if len(edits) <= 12 else " and later.")))
            past.append(E("p", {"class": "quiet"}, "Earlier versions are kept on GitHub.", " ",
                          E("a", {"href": address}, "Open the whole issue on GitHub") if address else ""))
        if not past:
            past.append(E("p", {"class": "quiet"}, "The text has not changed since it was first written."))
        parts.append(E("section", {"class": "readpart", "id": ident + "-past"}, E("h3", None, "Past versions of the text"), *past))
        contents.append((ident + "-past", "Past versions of the text"))
        # the entries, oldest first
        entries = [e for e in topic.get("entries", []) if isinstance(e, dict) and integer(e.get("id"))]
        shown = []
        for entry in entries:
            cid = integer(entry.get("id"))
            mark = text_of(entry.get("mark"))
            kind = self.kind_words(entry.get("kind"))
            head = E("summary", None, E("span", {"class": "kindword"}, kind), " ", text_of(entry.get("at")), " ",
                     E("span", {"class": "markword"}, mark) if mark else "")
            if mark:
                inner = [E("div", {"class": "readtext"}, self.blocks(entry.get("doc"))), E("p", {"class": "markwhy"}, mark)]
            else:
                where = E("code", {"class": "entryaddress"}, text_of(entry.get("address"))) if local and entry.get("address") else (
                    E("a", {"href": github_url("memory", self.repo, n, cid)}, "Open the original comment on GitHub")
                    if github_url("memory", self.repo, n, cid) else "")
                inner = [E("div", {"class": "readtext"}, self.blocks(entry.get("doc"))),
                         E("p", {"class": "quiet"}, CHANGED_NOTE) if entry.get("note") else "", E("p", {"class": "entryend"}, where)]
            shown.append(E("details", {"class": "readentry folded" if mark else "readentry", "id": "%s-e%d" % (ident, cid),
                                       "open": None if mark else "open"}, head, *inner))
        if not shown:
            shown.append(E("p", {"class": "quiet"}, "No entry has been written on this topic."))
        elif local and (topic.get("untold")):
            shown.append(E("p", {"class": "quiet"}, mind_reader.TAKE_BACK))
        parts.append(E("section", {"class": "readpart", "id": ident + "-entries"}, E("h3", None, "Entries"), *shown))
        contents.append((ident + "-entries", "Entries"))
        side = E("nav", {"class": "readcontents", "aria-label": "Within this area"},
                 *[E("a", {"href": anchor(target)}, label) for target, label in contents])
        return E("section", {"class": "view", "id": ident, "aria-labelledby": ident + "-h"}, trail, E("h2", {"id": ident + "-h"}, title),
                 E("p", {"class": "chips"}, *chips), E("div", {"class": "readbody"}, side, E("div", {"class": "readparts"}, *parts)))

    def untold(self):
        """The page's last lines: what is not told, and why."""
        missing, lines = self.model.get("not_told") or {}, []
        if (self.model.get("built") or {}).get("complete") is False:
            lines.append(E("p", None, "The record was still being read when this page was built."))
        words = record_store.words_for(self.repo)
        local = record_store.is_local(self.repo)
        others = integer(missing.get("others"))
        if others and local:
            lines.append(E("p", None, record_store.count_line(words["page_hidden"], others)))
        elif others:
            lines.append(E("p", None, plural(others, "1 memory by an account outside your allowed list is not told "
                                                     "here.",
                                             "%d memories by accounts outside your allowed list are not told here.")))
        edited = integer(missing.get("edited"))
        if edited:
            links = [github_url("memory", self.repo, entry.get("issue"), entry.get("id"))
                     for entry in missing.get("edited_ids", [])]
            lines.append(E("p", None, record_store.count_line(words["page_changed"], edited) if local else
                           plural(edited, "1 memory was changed on GitHub after it was posted, so it is not told here.",
                                  "%d memories were changed on GitHub after they were posted, so they are not told here.")))
            lines.append(E("ul", {"class": "plainlist"}, *[E("li", None, E("a", {"href": address},
                                                                         "Open it on GitHub"))
                                                            for address in links if address]))
        older = integer(missing.get("older"))
        if older:
            lines.append(E("p", None, plural(older, "1 older memory is not on this page; compile.py writes the whole "
                                                    "timeline.",
                                             "%d older memories are not on this page; compile.py writes the whole "
                                             "timeline.")))
        if integer(missing.get("words")):
            lines.append(E("p", None, plural(integer(missing["words"]), "1 more word is explained in the record than "
                                                                         "this page shows.",
                                             "%d more words are explained in the record than this page shows.")))
        for body in missing.get("bodies", []):
            address = github_url("issue", self.repo, body.get("topic"))
            lines.append(E("p", None, "The text of the thread ", self.thread_link(body.get("thread")),
                           " is not shown: someone outside your allowed accounts wrote or changed it, or its history "
                           "has not been read yet. ",
                           E("a", {"href": address}, "Open it on GitHub") if address
                           else ""))
        if integer(missing.get("kinds")):
            lines.append(E("p", None, plural(integer(missing["kinds"]), "1 comment is of a kind this version of the "
                                                                          "plugin does not know, so it is not told here.",
                                             "%d comments are of a kind this version of the plugin does not know, so they "
                                             "are not told here.")))
        if not lines:
            lines.append(E("p", None, "Every memory of the record by your allowed accounts is told here."))
        return E("section", {"class": "untold", "id": "untold"}, E("h3", None, "What this page leaves out"), *lines)

    @staticmethod
    def word_box():
        return J(E("div", {"id": "wordtip", "class": "wordtip", "role": "tooltip", "hidden": "hidden"}),
                 E("dialog", {"id": "wordbox", "class": "wordbox", "aria-labelledby": "wordhead"},
                   E("div", {"class": "wordtop"}, E("span", {"id": "wordkind", "class": "lab"},
                                                    "A word from this project"),
                     E("h2", {"id": "wordhead", "class": "wordhead"})),
                   E("div", {"id": "wordbody", "class": "wordbody", "tabindex": "0"}),
                   E("div", {"class": "wordbar"}, E("button", {"type": "button", "id": "wordclose", "class": "primary"},
                                                    "Close"))))

    def navigation_guide(self):
        """The same short guide in the optional tour and the help box; only visible areas are introduced."""
        areas = [
            ("now", "Overview", "Resume with the recorded focus, next actions, and holds. The project summary is below them. "
             "The status date shows when that handoff was recorded; automatic refreshes are labeled."),
            ("work", "Work", J("Read recorded tasks and steps, then open the memory that marked a step. ",
              E("a", {"href": anchor("open")}, "Open questions and limits"),
              " holds unanswered questions, untested ideas, and what the results do not show.")),
            ("history", "History", J("Start with Recently recorded, or choose Event sequence. Filters narrow by topic, event kind, "
              "state, words, or date. Open a memory for evidence and what led to it. Other views: ",
              E("a", {"href": anchor("settled")}, "Current understanding"), ", ",
              E("a", {"href": anchor("threads")}, "Topic stories"), ", ",
              E("a", {"href": anchor("lessons")}, "Lessons"), ", and ", E("a", {"href": anchor("map")}, "Maps"), ".")),
        ]
        if self.model.get("outputs_available", bool(self.outline.get("deliverables") or self.outline.get("other"))):
            areas.append(("outputs", "Outputs", J("Read the project's current text and sources, with each output named separately. "
                          "Choose “Why it says this” for the memories behind a part. ",
                          J(E("a", {"href": anchor("paper")}, "Read the manuscript"), " opens its paper reader.")
                          if self.sheet is not None else "")))
        areas.append(("ask", "Search memories", "Find words in included memories and their evidence, or use the buttons for "
                      "decisions, failures, open questions, and changes. Task steps and output text are not searched here. "
                      "Choose a memory sentence for its full detail; Close or Back returns you to the list."))
        return areas

    def tour(self):
        return E("div", {"id": "tour", "class": "tour", "role": "dialog", "aria-label": "First visit", "hidden": "hidden"},
                 *[E("p", {"class": "step", "data-go": "#" + go, "hidden": "hidden"}, E("b", None, name), ": ", words)
                   for go, name, words in self.navigation_guide()],
                 E("div", {"class": "tourbar"}, E("span", {"id": "tourn", "class": "quiet"}),
                   E("button", {"type": "button", "id": "tourback"}, "Back"),
                   E("button", {"type": "button", "id": "tournext", "class": "primary"}, "Next"),
                   E("button", {"type": "button", "id": "tourend"}, "Leave the tour")))

    def paper_page(self, paper, style, script):
        """The paper alone as a page of its own (mind.py --paper): its title, the sections with each topic's text, the lists of
        where the numbers come from. Same style, same Content-Security-Policy form and the same rules as the mind page; the one
        script only asks the browser to print. `paper` is mind_paper.paper(); its text is drawn as text through blocks()."""
        digest = lambda text: "'sha256-%s'" % base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()
        policy = POLICY % (digest(style), digest(script))
        title = text_of(paper.get("title"))
        head = E("head", None, E("meta", {"charset": "utf-8"}),
                 E("meta", {"http-equiv": "Content-Security-Policy", "content": policy}),
                 E("meta", {"name": "referrer", "content": "no-referrer"}),
                 E("meta", {"name": "viewport", "content": VIEWPORT}), E("title", None, title),
                 Safe("<style>%s</style>" % style))
        sections = []
        for section in paper.get("sections", []) if isinstance(paper.get("sections"), list) else []:
            parts = [p for p in section.get("parts", []) if isinstance(p, dict)] if isinstance(section, dict) else []
            inner = []
            for part in parts:
                n = integer(part.get("topic"))
                if text_of(part.get("name")):
                    inner.append(E("h4", {"class": "parth"}, text_of(part.get("name"))))
                inner.append(E("div", {"class": "parttext"}, self.blocks(part.get("doc"), github_url("issue", self.repo, n),
                                                                          self.stores.get(n), n)))
            sections.append(E("section", {"class": "sec papersec"}, E("h3", None, text_of(section.get("heading"))), *inner))
        rows = [r for r in paper.get("sources", []) if isinstance(r, dict)] if isinstance(paper.get("sources"), list) else []
        sourced = E("section", {"class": "sec papersec srcsec"}, E("h3", None, "Where the numbers come from"),
                    E("ul", {"class": "srcs"}, *[E("li", None, E("b", None, self.runs(r.get("statement"))), ": ",
                                                    self.runs(r.get("source"))) for r in rows])) if rows else ""
        main = E("main", {"id": "main", "class": "paperdoc"},
                 E("div", {"class": "papertop noprint"},
                   E("button", {"type": "button", "class": "primary", "data-print": "1"}, "Print or save as PDF"),
                   E("span", {"class": "quiet"}, "The browser's print dialog offers \"Save as PDF\".")),
                 E("h2", {"class": "papertitle"}, title),
                 E("p", {"class": "papername"}, text_of(paper.get("name"))) if text_of(paper.get("name")) else "",
                 E("p", {"class": "papernote"}, text_of(paper.get("note"))),
                 *sections, sourced,
                 E("p", {"class": "paperfoot"}, "Written from the record by recortex. Every word is the record's own text."))
        body = E("body", {"class": "paper"}, main, Safe("<script>%s</script>" % script))
        return "<!doctype html>\n%s\n<html lang=\"en\">%s%s</html>\n" % (NOTICE, head, body)

    def page(self, style, script, icon):
        digest = lambda text: "'sha256-%s'" % base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()
        live = live_script() if self.live else None
        policy = LIVE_POLICY % (digest(style), digest(script), digest(live)) if live is not None \
            else POLICY % (digest(style), digest(script))
        built = self.model.get("built") or {}
        head = E("head", None, E("meta", {"charset": "utf-8"}),
                 E("meta", {"http-equiv": "Content-Security-Policy", "content": policy}),
                 E("meta", {"name": "referrer", "content": "no-referrer"}),
                 E("meta", {"name": "viewport", "content": VIEWPORT}),
                 E("title", None, "The mind of ", text_of(self.model.get("name"))), Safe("<style>%s</style>" % style))
        logo = E("svg", {"viewBox": "144 144 968 968", "class": "logo", "aria-hidden": "true"},
                 E("path", {"class": "lg-a", "d": icon[0]}), E("path", {"class": "lg-b", "d": icon[1]}))
        labels = {"now": "Overview", "project-map": "Project Map", "work": "Work", "history": "History", "record": "Record",
                  "outputs": "Outputs"}
        keys = ["now", "project-map", "work", "history"] + (["record"] if self.reader_data() else [])
        if self.model.get("outputs_available", bool(self.outline.get("deliverables") or self.outline.get("other"))):
            keys.append("outputs")
        side = E("nav", {"class": "side fold", "aria-label": "Project areas"}, logo,
                 E("p", {"class": "mindname"}, text_of(self.model.get("name"))),
                 E("p", {"class": "mindsub"}, "the cortex of one project"),
                 E("button", {"type": "button", "class": "foldbtn waysbtn", "data-fold": "ways",
                              "aria-expanded": "true", "aria-controls": "project-tools"}, "Project tools"),
                 E("ul", {"class": "project-tabs"}, *[E("li", None, E("a", {"href": anchor(key), "data-nav": key}, labels[key])) for key in keys]),
                 E("div", {"class": "sidefoot foldbody", "id": "project-tools"},
                   E("a", {"href": anchor("ask")}, "Search memories"),
                   E("a", {"href": anchor("usage")}, "Recortex usage"),
                   E("a", {"href": Href(self.action_root.rsplit("/", 1)[0] + "/suggestions")}, "Observer suggestions")
                   if self.action_root else E("details", None, E("summary", None, "Observer suggestions"),
                       E("p", None, "Run ", E("code", None, "/recortex:dashboard suggestions"),
                         " in the project to review sources and manage reminders on this device.")),
                   self.explain_button("how", "How to read this page", "howbtn"),
                   E("button", {"type": "button", "id": "tourstart"}, "First visit? Take the tour"),
                   E("button", {"type": "button", "id": "theme"}, "Switch light and dark"),
                   E("a", {"href": Href("../../")}, "Projects") if self.live else "",
                   E("p", {"id": "live-stopped", "class": "quiet", "hidden": "hidden"}) if self.live else "",
                   E("p", {"class": "quiet"}, "Read from the record on %s. " % text_of(built.get("words")),
                     "Report completed steps from Work." if self.action_root else "This page never changes the record.")))
        output_views = E("nav", {"id": "output-views", "class": "area-links noprint", "aria-label": "Output views"},
                         E("a", {"href": anchor("outputs")}, "Current text and sources"),
                         E("a", {"href": anchor("paper")}, "Read the manuscript")) if self.sheet is not None else ""
        only = (self.reader_data() or {}).get("only")
        if only:   # the live server's own route for one topic: that view and the pane, not the whole page again
            main = E("main", {"id": "main"}, self.record_view(), self.read_views())
        else:
            main = E("main", {"id": "main"}, self.now_view(), self.project_map_view(), self.history_view(), output_views,
                     self.text_view(), self.sheet_view() if self.sheet is not None else "",
                     self.threads_view(), self.settled_view(),
                     self.lessons_view(), self.open_view(), self.todo_view(), self.map_view(), self.ask_view(), self.usage_view(),
                     self.record_view(), self.read_views())
        panel = E("aside", {"id": "panel", "class": "panel", "aria-label": "One memory"},
                  E("button", {"type": "button", "id": "close", "class": "close"}, "Close"),
                  E("h2", {"class": "noscript-h"}, "Every memory"),
                  *[self.detail(memory) for memory in self.model.get("memories", []) if integer(memory.get("id"))])
        body = E("body", None, side, main, panel, self.tour(), self.word_store(), self.explain_store(), self.issue_store(),
                 self.word_box(), Safe("<script>%s</script>" % script),
                 Safe("<script>%s</script>" % live) if live is not None else "")
        start = " data-start=\"read-%d\"" % only if isinstance(only, int) and not isinstance(only, bool) and only > 0 else ""
        return "<!doctype html>\n%s\n<html lang=\"en\"%s>%s%s</html>\n" % (NOTICE, start, head, body)


ICON_LINE = re.compile(r"[MCZ0-9 ,-]{1,8000}")


def fixed_files():
    """The style, the script and the icon, read from ../page; refused when the style or the script could end its
    element or open a comment, or when the icon is not exactly two lines of path data made of M, C, Z, digits,
    spaces, commas and minus signs."""
    with open(os.path.join(PAGE, "mind.css"), encoding="utf-8") as handle:
        style = handle.read()
    with open(os.path.join(PAGE, "mind.js"), encoding="utf-8") as handle:
        script = handle.read()
    for text in (style, script):
        if "</" in text or "<!--" in text:
            raise ValueError("the fixed style or script holds </ or <!--")
    with open(os.path.join(PAGE, "icon.txt"), encoding="utf-8") as handle:
        icon = handle.read().split("\n")
    if len(icon) != 3 or icon[2] != "" or not all(ICON_LINE.fullmatch(line) for line in icon[:2]):
        raise ValueError("the fixed icon is not two lines of path data")
    return style, script, icon[:2]


def live_script():
    """The fixed script of the live dashboard's page, read from ../page; refused when it could end its element or open a comment."""
    with open(os.path.join(PAGE, "live.js"), encoding="utf-8") as handle:
        script = handle.read()
    if "</" in script or "<!--" in script:
        raise ValueError("the fixed live script holds </ or <!--")
    return script


def paper_script():
    """The fixed script of paper.html, read from ../page; refused when it could end its element or open a comment."""
    with open(os.path.join(PAGE, "paper.js"), encoding="utf-8") as handle:
        script = handle.read()
    if "</" in script or "<!--" in script:
        raise ValueError("the fixed paper script holds </ or <!--")
    return script


def render_paper(paper, pictures=None):
    """paper.html for mind_paper.paper() data, as a string. Pure, like render(): the same data and pictures give the same
    text. The pictures are the ones mind_figures.load() kept, checked again by the page."""
    style, _, _ = fixed_files()
    model = {"repo": paper.get("repo"), "threads": paper.get("threads", [])}
    return Page(model, pictures).paper_page(paper, style, paper_script())


def render(model, pictures=None):
    """The whole page for a model, as a string. Pure: the same model and pictures give the same text. `pictures` maps
    (repository, ref, path) to the bytes of a figure that mind_figures loaded; the page checks them again."""
    style, script, icon = fixed_files()
    return Page(model, pictures).page(style, script, icon)


PAPER_UNVOUCHED = "mind: %s"


def plan_labels(profiles, plan):
    """The section labels (lowercase) of the profile whose plan the manuscript view tells, from the shipped profile of the
    project's entry for it: what the view's own outline holds for that profile and nothing of another one."""
    for entry in profiles if isinstance(profiles, list) else []:
        if (entry if isinstance(entry, str) else entry.get("name") if isinstance(entry, dict) else None) == plan["profile"]:
            try:
                return {label.lower() for label, _ in recortex.section_labels([entry], shipped_only=True)}
            except (ValueError, OSError):
                return set()
    return set()


def write_paper(db, config, repo, args):
    """mind.py --paper [--version NAME]: the paper alone as paper.html (or paper-NAME.html for a named version), beside mind.html in
    the private folder. The current paper is the topics' text that the page tells; a named version is only what the record vouches
    for (versions.vouched_version: the decision, the tag, VERSION.json and every topic file by hash) in the sections it had. The
    text is drawn as the page draws it, under the same policy. Returns the exit code."""
    allowed = recortex.allowed_users(repo)
    profiles = recortex.shipped_profiles(config.get("profiles"))
    hub = config.get("hub") if type(config.get("hub")) is int and config.get("hub") >= 1 else None
    probe = mind_model.Build(db, repo, {name.lower() for name in allowed}, None)
    probe.read_issues()
    names = {n: issue["title"] for n, issue in probe.issues.items() if probe.title_shown(n)}
    plan = recortex.plan_profile(profiles)
    if plan is not None:
        # the view's rule (references/mind.md, "The manuscript"): "The manuscript" with the project's name under it, never the hub's
        # title, and the sections of that profile only
        title, subtitle = mind_model.Build.NOUNS.get(plan["profile"], (None, "the " + plan["noun"]))[1].capitalize(), repo.split("/")[-1]
    else:
        title, subtitle = names.get(hub) or repo.split("/")[-1], ""
    if args.version:
        import versions  # noqa: E402 - the versions the record vouches for (references/versions.md)
        if plan is None:
            print("mind: this project has no plan, so no version of its paper is named")
            return 1
        try:
            bodies, decision, _, _, layout = versions.vouched_version(config, repo, db, allowed, plan, args.version)
        except versions.NotVouched as error:
            print(str(error).replace("compile:", "mind:", 1))
            return 1
        order = layout if layout is not None else index.deliverable_order(db, hub, profiles)
        note = "Version %s, named on %s." % (args.version, mind_model.full_day(decision["time"]))
        target = "paper-%s.html" % args.version
    else:
        told = index.told_bodies(db, allowed)
        bodies = {row["number"]: row["body"] or "" for row in db.execute("SELECT number, body FROM issues")
                  if row["number"] in told}
        order = index.deliverable_order(db, hub, profiles)
        if plan is not None:
            ours = plan_labels(profiles, plan)
            order = [row for row in order if row[0].lower() in ours]
        note = "As the record says on %s." % mind_model.full_day(datetime.date.today().isoformat() + "T00:00:00Z")
        target = "paper.html"
    paper = mind_paper.paper(repo, title, note, order, bodies, names, subtitle)
    wanted = mind_figures.wanted({"repo": repo, "threads": paper["threads"]})
    pictures, reasons = mind_figures.load(wanted, repo, None, fetch=bool(args.sync or args.figures))
    text = render_paper(paper, pictures)
    path = os.path.join(index.index_dir(repo), target)
    try:
        storage.write_file(path, text)
    except storage.StorageError as error:
        print(NOT_WRITTEN % error.kind)
        return 1
    address = pathlib.Path(path).as_uri()
    opened = recortex.open_page(address) if args.open else False   # right after the file is written
    quiet = bool(args.open and not opened)   # no browser: the session says that one line and the path, and nothing else
    if not quiet:
        print("wrote %s (%d KB): %s" % (path, (len(text.encode("utf-8")) + 1023) // 1024,
                                        plural(len(paper["sections"]), "1 section", "%d sections")))
    for line in mind_figures.lines(reasons):
        print(line)
    if quiet:
        print(NO_BROWSER % "The paper")
        print("the file: %s" % path)
        return 0
    print("open it in a browser: %s" % address)
    if opened:
        print("asked the browser to open it")
    return 0


class PageError(ValueError):
    """A page that cannot be built, with one of this module's own fixed lines as its text."""


def load_model(config, repo, allowed, early=None, reader="all"):
    """(model, None) for the page of `repo`, built from its local index: the index is opened, checked and closed here, and the
    one place that does it for `mind.py` and for `public_text` alike. `early(db)`, when given, runs while the index is open and
    may return a value that ends the work (main's --paper): then (None, that value). Raises PageError with a fixed line when
    the index is refused, missing or not ready."""
    try:
        db = index.connect(repo, create=False)
    except storage.StorageError:
        raise PageError(REFUSED)
    if db is None:
        raise PageError(NO_INDEX)
    try:
        if not index.mind_ready(db):
            raise PageError(index.NOT_READY)
        if early is not None:
            done = early(db)
            if done is not None:
                return None, done
        return mind_model.build(db, repo, allowed, datetime.date.today(),
                                recortex.shipped_profiles(config.get("profiles")), config, reader), None
    finally:
        db.close()


def finish(model, config, repo, fetch=False, public=False):
    """(the page's text, the reasons figures were not shown) for a built model. The page that is made for the public has none
    of this machine's own numbers: no usage view and no log of what the observer did. The pictures are the ones kept here, and
    only with `fetch` are the missing ones fetched (never for a public page)."""
    if public:
        model["usage"], model["acts"], model["reader"] = None, [], None   # the whole text of the record is not for a public page
    else:
        try:
            model["usage"] = usage.report(repo, config)   # numbers and fixed words; the page tells them as they are
        except Exception:  # noqa: BLE001 - the page is built without the usage view's numbers, never not at all
            model["usage"] = None
        try:
            model["acts"] = usage.acts_report(repo)   # what the observer did, for the end of the to-do view
        except Exception:  # noqa: BLE001
            model["acts"] = []
    wanted = mind_figures.wanted(model)
    pictures, reasons = mind_figures.load(wanted, repo, None, fetch=fetch and not public)
    return render(model, pictures), reasons


class PrivateError(PageError):
    """The record's own text, before any page is built from it, holds what the project's privacy rules refuse."""


def model_strings(model):
    """Every string of a built model, whatever it holds: the dictionary keys and values, the members of lists, tuples and sets,
    and the text of any other object, each once. The page is made from these, so a rule applied to them looks at the record's
    text itself and not at the markup it becomes (a word split by an element, a value kept in an attribute). Iterative, and
    safe against a container that holds itself."""
    found, seen, todo = [], set(), [model]
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, (dict, list, tuple, set, frozenset)):
            if id(item) in seen:
                continue
            seen.add(id(item))
            todo.extend(list(item.keys()) + list(item.values()) if isinstance(item, dict) else list(item))
        elif item is not None and not isinstance(item, (bool, int, float)):
            found.append(str(item))
    return found


def public_text(config, repo, allowed, refuse=None):
    """(text, counts) of the static page as it would be published: the same steps as mind.py, from the index to render, with
    no usage numbers, no log of the observer's acts and no interactive part, and only the pictures that are kept. Nothing is
    written and no file is read. `counts` holds the numbers of memories and threads. `refuse`, when given, is called with
    model_strings of the built model before any page is made, and a true answer raises PrivateError. Raises PageError with one
    of this module's fixed lines when the page cannot be built."""
    model, _ = load_model(config, repo, allowed, reader=None)   # the page for the public holds no reader
    model["usage"], model["acts"] = None, []
    if refuse is not None and refuse(model_strings(model)):
        raise PrivateError("the record's own text holds what the project's privacy rules keep off the record")
    text, _ = finish(model, config, repo, public=True)
    return text, {"memories": model["counts"]["memories"], "threads": model["counts"]["threads"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="owner/name (default: from .recortex.json)")
    parser.add_argument("--sync", action="store_true", help="run index.py sync first (reads GitHub)")
    parser.add_argument("--figures", action="store_true", help="fetch the pictures of figures not yet kept (--sync does "
                                                              "too); otherwise only the ones kept are shown")
    parser.add_argument("--versions", action="store_true", help="bring the versions of the text that the record vouches for "
                                                                "into the cache first (--sync does too)")
    parser.add_argument("--open", action="store_true", help="ask the browser to open the page")
    parser.add_argument("--paper", action="store_true", help="write the paper alone as paper.html beside the page (print it or "
                                                              "save it as PDF from the browser); the mind page is left as it is")
    parser.add_argument("--version", help="with --paper: the paper as of this named version, written as paper-NAME.html")
    args = parser.parse_args(argv)
    config = recortex.load_config()
    repo = recortex.resolve_repo(args.repo, config)
    print(recortex.describe(config, repo))
    if args.sync:
        hub = None  # a project file with no hub syncs without the resume block, as index.py sync does
        if config.get("repo", "").lower() == repo.lower() and "hub" in config:
            hub = recortex.resolve_hub(None, config)  # a hub that is no issue number stops with its fixed line
        index.sync(repo, hub)
    allowed = recortex.allowed_users(repo)
    if not allowed:
        print(UNTRUSTED)
        return 1
    if args.sync or args.versions:
        try:
            import versions  # noqa: E402 - the page reads what this brings (references/versions.md); a failure never stops it
            versions.fetch(config, repo)
        except Exception as error:  # noqa: BLE001
            print("versions: the versions of the text could not be fetched (%s); the page shows what is kept." % type(error).__name__)
    def early(db):
        if args.version and not args.paper:
            print("mind: --version goes with --paper")
            return 1
        if args.paper:
            return write_paper(db, config, repo, args)
        return None
    try:
        model, done = load_model(config, repo, allowed, early=early)
    except PageError as error:
        print(str(error))
        return 1
    if done is not None:
        return done
    text, reasons = finish(model, config, repo, fetch=bool(args.sync or args.figures))
    path = os.path.join(index.index_dir(repo), "mind.html")
    try:
        storage.write_file(path, text)
    except storage.StorageError as error:
        print(NOT_WRITTEN % error.kind)
        return 1
    counts, missing = model["counts"], model["not_told"]
    address = pathlib.Path(path).as_uri()
    opened = recortex.open_page(address) if args.open else False   # right after the file is written
    quiet = bool(args.open and not opened)   # no browser: the session says that one line and asks one question
    if not quiet:
        print("wrote %s (%d KB): %s in %s" % (path, (len(text.encode("utf-8")) + 1023) // 1024,
                                              plural(counts["memories"], "1 memory", "%d memories"),
                                              plural(counts["threads"], "1 thread", "%d threads")))
    untold = missing["others"] + missing["edited"] + missing["older"]
    if untold:
        print("%s not told; the page's last lines say why" % plural(untold, "1 memory is", "%d memories are"))
    for line in mind_figures.lines(reasons):
        print(line)
    if not quiet:
        print("open it in a browser: %s" % address)
    sheet = model.get("sheet")
    if isinstance(sheet, dict):
        if integer(sheet.get("unfetched")):
            print("%s of the manuscript show no marks because the text of an earlier version is not kept here; run mind.py "
                  "--versions" % plural(integer(sheet["unfetched"]), "1 part", "%d parts"))
        if not quiet:
            print("the manuscript: %s#paper" % address)
    if opened:
        print("asked the browser to open it")
    if quiet:
        import publish
        print(NO_BROWSER % "The dashboard")
        for line in publish.offer(config, repo, path):
            print(line)
        return 0
    print("the project map: %s#project-map" % address)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (recortex.GhError, storage.StorageError, index.SyncBusy) as error:
        sys.exit("error: %s" % error)

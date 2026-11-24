#!/usr/bin/env python3
"""Everything that talks to a GitHub Projects board, and the user's approval of one.

The board is a view of the record, never part of it. Everything read from it is untrusted: option names, draft
titles, text fields, the README and view names. What this module returns from a board is item ids, the kind of an
item, whether it is archived, the number and repository of its issue (checked against the record's), the plugin's
own status name found by exact option name ("other" for any other column), and the Why text, which callers compare
and never print. Project and field ids are read from GitHub at run time and checked against GH_ID before they are
put into a query.

Every call goes through record_store.github(). A failure is a BoardError with a fixed reason code (REASONS); the text of an
error from GitHub is never shown.

The approval, kept on the user's side (<configuration top>/private/board-approvals.json, through storage.py), ties
the `board` entry of the project file to the owner and number the user chose at setup. A project file alone cannot
point the plugin at somebody else's board.
"""

import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recortex  # noqa: E402
import record_store  # noqa: E402
import storage  # noqa: E402

STATUSES = ("To triage", "Todo", "In Progress", "Waiting for a yes", "Blocked", "Done", "Set aside")
EXISTING_STATUSES = ("Todo", "In Progress", "Done")   # the names a new project already has
COLORS = {"To triage": "YELLOW", "Todo": "GRAY", "In Progress": "BLUE", "Waiting for a yes": "ORANGE",
          "Blocked": "RED", "Done": "GREEN", "Set aside": "PURPLE"}
COLOR_NAMES = ("BLUE", "GRAY", "GREEN", "ORANGE", "PINK", "PURPLE", "RED", "YELLOW")
WORKFLOWS = ("Item closed", "Pull request merged", "Auto-close issue", "Auto-add to project", "Auto-archive items")
WHY = "Why"
VIEWS = (("Board", "BOARD_LAYOUT", None),
         ("Waiting on you", "TABLE_LAYOUT", 'status:"Waiting for a yes","Blocked","To triage"'),
         ("By thread", "TABLE_LAYOUT", None))
README = ("Kept current by the recortex plugin from the project's record in GitHub issues. The record is the source "
          "of truth: a card moved by hand is reported to the next session, and nothing changes until the user answers.")
DESCRIPTION = "The work of the project, kept current from its record."
APPROVALS = "board-approvals.json"
APPROVALS_LOCK = "board-approvals.lock"
FORM = 1
GH_ID = re.compile(r"[A-Za-z0-9_=-]{1,100}")
OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
PAGE = 100
WHY_CAP = 300
CURSOR = re.compile(r"[A-Za-z0-9+/=_-]{1,400}")  # a page cursor from GitHub, checked before it goes into a query
MAX_PAGES = 500          # pages of cards read at most (50,000 cards, the most a board holds)
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

REASONS = {
    "scope": "the gh login lacks the project scope",
    "notfound": "the project was not found",
    "field": "a field of the board is missing",
    "public": "the board is public while the record is private",
    "github": "GitHub could not be read",
    "budget": "the budget ran out",
    "unapproved": "the board is not approved on this machine",
    "entry": "the board entry of the project file does not fit",
    "setup": "the board does not read back as it was set up",
}
SCOPE_LINE = ("the gh login lacks the project scope; it is added with `gh auth refresh -s project`, then run "
              "setup again. The scope lets gh read and write all of your GitHub projects; the plugin touches only the "
              "one board you chose.")


class BoardError(Exception):
    """A board failure with a fixed reason code from REASONS; `str()` is the fixed reason, never GitHub's text."""

    def __init__(self, code):
        super().__init__(REASONS[code])
        self.code = code


# ---------------------------------------------------------------- the entry and its approval

def entry(config, repo):
    """(state, owner, number) for the project file's `board` entry. State: none (no entry), entry (does not fit),
    owner (the board's owner is not the record's), unapproved (not what the user approved here), approved."""
    value = config.get("board")
    if value is None:
        return "none", None, None
    if not isinstance(value, dict) or set(value) != {"owner", "number"}:
        return "entry", None, None
    owner, number = value["owner"], value["number"]
    if not isinstance(owner, str) or not OWNER.fullmatch(owner) or type(number) is not int or not 1 <= number <= 10 ** 8:
        return "entry", None, None
    if not recortex.valid_repo(repo) or owner.lower() != repo.split("/")[0].lower():
        return "owner", owner, number
    try:
        found = read_approvals()["approvals"].get(repo.lower())
    except (storage.StorageError, ValueError, OSError):
        return "unapproved", owner, number
    if not isinstance(found, dict) or str(found.get("owner", "")).lower() != owner.lower() \
            or found.get("number") != number:
        return "unapproved", owner, number
    return "approved", owner, number


def approvals_path():
    return os.path.join(storage.config_top(), storage.PRIVATE, APPROVALS)


def read_approvals(create=False):
    """The validated top level, {"form": 1, "approvals": {repo: {"owner", "number", "approved"}}}; an empty one when
    there is no file. Raises storage.StorageError for refused storage and ValueError for a file of another form."""
    private = storage.folder(storage.config_top(), create=create, tighten=create)
    empty = {"form": FORM, "approvals": {}}
    if private is None:
        return empty
    path = os.path.join(private, APPROVALS)
    info = storage.check_path(path, "file")
    if not info:
        return empty
    if info.st_size > 65536:
        raise ValueError("the approvals file is too large")
    try:
        data = json.loads(storage.read_file(path) or "")
    except (ValueError, RecursionError):
        raise ValueError("the approvals file is not JSON")
    if not isinstance(data, dict) or set(data) != {"form", "approvals"} or data["form"] != FORM \
            or not isinstance(data["approvals"], dict):
        raise ValueError("the approvals file is not of the expected form")
    return data


def record_approval(repo, owner, number):
    """The user's choice of board for this record on this machine; the entries of other records are kept. Read,
    changed and written under the lock that keeps two writers of an approvals file apart, then read back."""
    import field_sheet  # the lock of the field approvals, on a file of its own
    with field_sheet.approvals_lock(name=APPROVALS_LOCK):
        data = read_approvals(create=True)
        data["approvals"][repo.lower()] = {"owner": owner, "number": number,
                                           "approved": datetime.date.today().isoformat()}
        private = storage.folder(storage.config_top())
        storage.write_file(os.path.join(private, APPROVALS), json.dumps(data, indent=1) + "\n")
    if entry({"board": {"owner": owner, "number": number}}, repo)[0] != "approved":
        raise ValueError("the approval does not read back")


# ---------------------------------------------------------------- calls

def graphql(query, timeout=60):
    """The `data` of one GraphQL operation, through `gh api graphql`. Raises BoardError."""
    try:
        text = record_store.github(["api", "graphql", "-f", "query=" + query], timeout=timeout)
    except recortex.GhError as error:
        raise BoardError(classify(str(error)))
    try:
        answer = json.loads(text)
    except ValueError:
        raise BoardError("github")
    if not isinstance(answer, dict):
        raise BoardError("github")
    if answer.get("errors") and not answer.get("data"):
        raise BoardError(classify(json.dumps(answer["errors"])))
    return answer.get("data") or {}


def classify(text):
    low = text.lower()
    if "timed out" in low:
        return "budget"
    if "scope" in low and ("token" in low or "required" in low):
        return "scope"
    if "could not resolve to a projectv2" in low:
        return "notfound"
    return "github"


def quote(value, ascii_only=True):
    """A string literal. JSON escapes are GraphQL's; ascii_only=False keeps an emoji of the user's own option names
    as itself rather than as a surrogate pair."""
    return json.dumps(value, ensure_ascii=ascii_only)


def ident(value):
    if not isinstance(value, str) or not GH_ID.fullmatch(value):
        raise BoardError("github")
    return value


def scope_probe(timeout=60):
    """The login, from a question that needs the project scope. Raises BoardError('scope') when it is missing."""
    data = graphql("query { viewer { login projectsV2(first: 1) { totalCount } } }", timeout)
    login = ((data.get("viewer") or {}).get("login"))
    return login if isinstance(login, str) and OWNER.fullmatch(login) else ""


PROJECT_FIELDS = ("id number closed public title readme shortDescription "
                  "repositories(first: 20) { nodes { nameWithOwner } } "
                  "fields(first: 50) { nodes { ... on ProjectV2Field { id name dataType } ... on ProjectV2SingleSelectField "
                  "{ id name options { id name color description } } } } "
                  "workflows(first: 20) { nodes { name enabled } } "
                  "views(first: 20) { nodes { id name filter } }")


def project_query(owner, number):
    body = "projectV2(number: %d) { %s }" % (number, PROJECT_FIELDS)
    return ("query { repositoryOwner(login: %s) { id login ... on User { %s } ... on Organization { %s } } }"
            % (quote(owner), body, body))


def read_project(owner, number, timeout=60):
    """What the plugin needs of a board, checked: {"id", "owner_id", "closed", "public", "status": {"id", "options":
    {id: status name or "other"}, "ids": {status name: id}}, "why": id or None, "workflows": {name: enabled},
    "views": {name: id}}. Raises BoardError('notfound') when there is no such project, ('field') when the Status
    field is missing."""
    data = graphql(project_query(owner, number), 60 if timeout is None else timeout)
    holder = data.get("repositoryOwner") or {}
    found = holder.get("projectV2")
    if not isinstance(found, dict) or found.get("number") != number:
        raise BoardError("notfound")
    if not isinstance(holder.get("login"), str) or holder["login"].lower() != owner.lower():
        raise BoardError("notfound")
    out = {"id": ident(found.get("id")), "owner_id": ident(holder.get("id")), "closed": bool(found.get("closed")),
           "public": found.get("public") is not False, "status": None, "why": None, "workflows": {}, "views": {},
           "filters": {},  # view name: True when its filter is the plugin's (compared, never shown)
           "texts": found.get("readme") == README and found.get("shortDescription") == DESCRIPTION,
           "repos": {r["nameWithOwner"].lower() for r in (found.get("repositories") or {}).get("nodes") or []
                     if isinstance(r, dict) and recortex.valid_repo(r.get("nameWithOwner"))}}
    for node in (found.get("fields") or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if node.get("name") == "Status" and isinstance(node.get("options"), list):
            ids, names = {}, {}
            for option in node["options"]:
                if not isinstance(option, dict) or not isinstance(option.get("id"), str) \
                        or not GH_ID.fullmatch(option["id"]):
                    continue
                label = option.get("name") if option.get("name") in STATUSES else "other"
                names[option["id"]] = label
                if label != "other":
                    ids[label] = option["id"]
            full = []
            for option in node["options"]:
                if isinstance(option, dict) and option.get("id") in names:
                    color = option.get("color") if option.get("color") in COLOR_NAMES else "GRAY"
                    description = option.get("description") if isinstance(option.get("description"), str) else ""
                    full.append((option["id"], option.get("name") if isinstance(option.get("name"), str) else "",
                                 color, description[:100]))
            out["status"] = {"id": ident(node.get("id")), "options": names, "ids": ids, "full": full}
        elif node.get("name") == WHY and "options" not in node and node.get("dataType") == "TEXT":
            out["why"] = ident(node.get("id"))
    for node in (found.get("workflows") or {}).get("nodes") or []:
        if isinstance(node, dict) and isinstance(node.get("name"), str):
            label = node["name"] if node["name"] in WORKFLOWS else "other"
            out["workflows"][label] = out["workflows"].get(label, False) or bool(node.get("enabled"))
    for node in (found.get("views") or {}).get("nodes") or []:
        if isinstance(node, dict) and node.get("name") in [v[0] for v in VIEWS] and isinstance(node.get("id"), str):
            out["views"][node["name"]] = ident(node["id"])
            out["filters"][node["name"]] = (node.get("filter") or "") == ({v[0]: v[2] for v in VIEWS}[node["name"]] or "")
    return out


def cards_query(project_id, after):
    if after is not None and not CURSOR.fullmatch(after):
        raise BoardError("github")
    paging = "first: %d, archivedStates: [ARCHIVED, NOT_ARCHIVED]%s" % (PAGE, ", after: %s" % quote(after) if after
                                                                     else "")
    content = "number repository { nameWithOwner }"
    return ("query { node(id: %s) { ... on ProjectV2 { items(%s) { pageInfo { hasNextPage endCursor } nodes { id "
            "isArchived type content { ... on Issue { %s } ... on PullRequest { %s } } "
            "status: fieldValueByName(name: \"Status\") { ... on ProjectV2ItemFieldSingleSelectValue { optionId } } "
            "why: fieldValueByName(name: \"Why\") { ... on ProjectV2ItemFieldTextValue { text } } } } } } }"
            % (quote(ident(project_id)), paging, content, content))


def read_cards(project, repo, left=None, timeout=60):
    """The cards of the record's repository: {number: {"item", "kind", "archived", "status" ("" for none, a plugin
    status or "other"), "why"}}. Items of other repositories, drafts and redacted items are left out. `left` is a
    function that gives the seconds left (None for no limit) and raises BoardError('budget') when there are none."""
    cards, after, pages = {}, None, 0
    names = project["status"]["options"] if project.get("status") else {}
    while True:
        seconds = timeout if left is None else left()
        data = graphql(cards_query(project["id"], after), seconds)
        items = (((data.get("node") or {}).get("items")) or {})
        for node in items.get("nodes") or []:
            if not isinstance(node, dict) or node.get("type") not in ("ISSUE", "PULL_REQUEST"):
                continue
            content = node.get("content") or {}
            number, where = content.get("number"), (content.get("repository") or {}).get("nameWithOwner")
            if type(number) is not int or not 1 <= number <= 10 ** 7 or not recortex.valid_repo(where) \
                    or where.lower() != repo.lower() or not isinstance(node.get("id"), str) \
                    or not GH_ID.fullmatch(node["id"]):
                continue
            option = (node.get("status") or {}).get("optionId")
            text = (node.get("why") or {}).get("text")
            cards[number] = {"item": node["id"], "kind": "pull" if node["type"] == "PULL_REQUEST" else "issue",
                             "archived": node.get("isArchived") is True,
                             "status": "" if option is None else names.get(option, "other") if isinstance(option, str)
                             else "other",
                             "why": text[:WHY_CAP] if isinstance(text, str) else ""}
        page = items.get("pageInfo") or {}
        pages += 1
        if not page.get("hasNextPage") or not isinstance(page.get("endCursor"), str) or page["endCursor"] == after \
                or pages >= MAX_PAGES:
            return cards
        after = page["endCursor"]


# ---------------------------------------------------------------- writes

def mutation(name, fields, selection, timeout=60):
    """One mutation. `fields` is the literal text of the input object's body."""
    return graphql("mutation { %s(input: {%s}) { %s } }" % (name, fields, selection), timeout)


def repo_ids(repo, numbers, timeout=60):
    """({repository id, private flag, owner id}, {number: node id}) for issues and pull requests of `repo`."""
    owner, name = repo.split("/")
    aliases = " ".join("i%d: issueOrPullRequest(number: %d) { ... on Issue { id } ... on PullRequest { id } }" % (n, n)
                       for n in sorted(numbers))
    data = graphql("query { repository(owner: %s, name: %s) { id isPrivate owner { id login } %s } }"
                   % (quote(owner), quote(name), aliases), timeout)
    found = data.get("repository") or {}
    info = {"id": ident(found.get("id")), "private": found.get("isPrivate") is not False,
            "owner_id": ident((found.get("owner") or {}).get("id"))}
    nodes = {}
    for number in numbers:
        node = found.get("i%d" % number)
        if isinstance(node, dict) and isinstance(node.get("id"), str) and GH_ID.fullmatch(node["id"]):
            nodes[number] = node["id"]
    return info, nodes


def add_item(project_id, content_id, timeout=60):
    data = mutation("addProjectV2ItemById", "projectId: %s, contentId: %s" % (quote(ident(project_id)),
                                                                              quote(ident(content_id))),
                    "item { id }", timeout)
    return ident(((data.get("addProjectV2ItemById") or {}).get("item") or {}).get("id"))


def values_text(project_id, item_id, values, check=True):
    """One mutation that writes fields of one card: [(field id, ("option", option id) or ("text", text))]. `check`
    False is for a dry run, whose item can be a placeholder."""
    same = ident if check else str
    parts = []
    for position, (field_id, (kind, content)) in enumerate(values):
        literal = "singleSelectOptionId: %s" % quote(same(content)) if kind == "option" else "text: %s" % quote(content)
        parts.append("f%d: updateProjectV2ItemFieldValue(input: {projectId: %s, itemId: %s, fieldId: %s, value: {%s}}) "
                     "{ projectV2Item { id } }" % (position, quote(same(project_id)), quote(same(item_id)),
                                                  quote(same(field_id)), literal))
    return "mutation { %s }" % " ".join(parts)


def set_values(project_id, item_id, values, timeout=60):
    """Status and Why of one card in one request. GitHub refuses a request whole (a rate limit) or runs all of it, so
    a stopped run does not leave a new Status under an old Why, which board_rules.compare would read as a hand move."""
    graphql(values_text(project_id, item_id, values), timeout)


def lines_for(project):
    """Fixed wording about a read project: no text of the board, only booleans, counts and the plugin's own names."""
    on = sorted(name for name, enabled in project["workflows"].items() if enabled and name != "other")
    others = "yes" if project["workflows"].get("other") else "no"
    lines = ["board project: %s, %s; Status field %s with %d of 7 options of the plugin; Why field %s; views present: %s"
             % ("closed" if project["closed"] else "open", "public" if project["public"] else "private",
                "there" if project["status"] else "missing",
                len(project["status"]["ids"]) if project["status"] else 0, "there" if project["why"] else "missing",
                ", ".join(sorted(project["views"])) or "none"),
             "board workflows on (names the plugin knows): %s; other workflows on: %s" % (", ".join(on) or "none",
                                                                                          others)]
    if "Auto-close issue" in on:
        lines.append("warning: the workflow that closes an issue when its Status changes is on; a dragged card would "
                     "close an issue. Turn it off in the board's settings.")
    return lines


def doctor_lines(config, repo, timeout=30):
    """For --doctor: what can be said about the board by asking GitHub. Never GitHub's text."""
    state, owner, number = entry(config, repo)
    if state == "none":
        return ["board: none configured"]
    words = {"entry": "the entry does not fit", "owner": "its owner is not the record's owner",
             "unapproved": "not approved on this machine (run board.py setup --mode existing --number %s --yes)"
             % (number if type(number) is int else "N"), "approved": "approved here"}
    lines = ["board: %s%s" % (words[state], "; %s/%d" % (owner, number) if owner else "")]
    if state != "approved":
        return lines
    try:
        scope_probe(timeout)
        project = read_project(owner, number, timeout)
    except BoardError as error:
        return lines + ["board read: %s" % error]
    return lines + lines_for(project)

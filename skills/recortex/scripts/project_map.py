"""Version-one, append-only operations for the shared project hierarchy."""

import copy
import hashlib
import json
import re
import uuid

import recortex

MARKER = "<!-- recortex:project-map v1 -->"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
HASH = re.compile(r"[0-9a-f]{64}")
ACTIONS = {
    "add": {"id", "title", "parent", "description", "evidence", "links"},
    "rename": {"id", "title", "evidence"},
    "update": {"id", "description", "evidence", "links"},
    "move": {"id", "parent"}, "delete": {"id", "children"}, "restore": {"id"},
    "merge": {"id", "into"}, "split": {"id", "branches"},
    # A first map in one operation: only for a map with no branch at all, and only by a user (1.7.0). `id` names the change.
    "seed": {"id", "branches"},
}
SEED_MAX = 200
SEED_BRANCH = {"id", "title", "parent", "description", "evidence", "links"}


class MapError(ValueError):
    """An invalid, stale, or unverified map change."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def branch_anchor(ident):
    """A stable, opaque deep link independent of wording, parent, and the shape of an input ID."""
    return "branch-" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:24]


def empty():
    return {"nodes": {}, "history": [], "revision": digest([])}


def ident(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise MapError("Invalid map identifier.")
    return value


def words(value, limit, required=False):
    if (not isinstance(value, str) or len(value) > limit or (required and not value.strip()) or
            any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        raise MapError("Invalid map text.")
    return value.strip()


def strings(values, check):
    if not isinstance(values, list) or len(values) > 64:
        raise MapError("Expected a short list.")
    result = [check(v) for v in values]
    if len(set(result)) != len(result):
        raise MapError("The change contains duplicates.")
    return result


def new_id():
    return uuid.uuid4().hex


def _transform(state, change, source, op_id):
    """Compute the replacement rows and every node on which their validity depends."""
    if not isinstance(change, dict) or not isinstance(change.get("action"), str):
        raise MapError("Expected a map change.")
    action = change["action"]
    if action not in ACTIONS or set(change) - ACTIONS[action] - {"action"}:
        raise MapError("Unknown map action or field.")
    if source not in ("user", "observer") or (source == "observer" and action not in ("add", "rename", "seed")):
        raise MapError("This change requires user review.")
    nodes, changed, touched = state["nodes"], {}, set()
    key = ident(change.get("id"))

    def get(key, active=True):
        ident(key)
        touched.add(key)
        node = changed.get(key, nodes.get(key))
        if node is None or (active and node["deleted"]):
            raise MapError("The branch is missing or deleted.")
        return node

    def edit(key, **values):
        changed[key] = dict(get(key, active=False), **values)

    def child_ids(key):
        return [n for n, val in nodes.items() if not val["deleted"] and val["parent"] == key]

    def parent_for(key, parent):
        seen = {key}
        while parent is not None:
            ident(parent)
            if parent in seen:
                raise MapError("A branch cannot be its own ancestor.")
            seen.add(parent)
            parent = get(parent)["parent"]

    def refs(value):
        return strings(value, lambda v: words(v, 300, True))

    def links_for(key, value):
        links = strings(value, ident)
        for other in links:
            if other == key:
                raise MapError("A branch cannot link to itself.")
            get(other)
        return links

    def check_observer_title(title):
        if source == "observer" and any(
                (n["deleted"] and title.casefold() == n["title"].casefold()) or
                any(title.casefold() == alias.casefold() for alias in n["aliases"])
                for n in nodes.values()):
            raise MapError("A previously corrected or deleted branch requires explicit review.")

    def add(key, title, parent, description="", evidence=None, links=None):
        key = ident(key)
        touched.add(key)
        if key in nodes or key in changed:
            raise MapError("The branch identifier is already in use.")
        parent_for(key, parent)
        title = words(title, 160, True)
        check_observer_title(title)
        for other in nodes.values():
            if other["parent"] == parent and title.casefold() in [t.casefold() for t in [other["title"]] + other["aliases"]]:
                if not other["deleted"] or source == "observer":
                    raise MapError("This branch already exists, was renamed, or needs explicit restoration.")
        changed[key] = {"id": key, "title": title, "parent": parent, "description": words(description, 1600),
                        "evidence": refs(evidence if evidence is not None else []),
                        "links": links_for(key, links if links is not None else []), "deleted": False, "manual": False,
                        "aliases": [], "deleted_by": None}

    def seed():
        """A whole first tree: each branch added in order (a parent comes before its children, and a title is used once under
        a parent), then the links, which may name any branch of the list."""
        if source != "user":
            raise MapError("A first map is made by a user.")
        if nodes:
            raise MapError("The map already has branches. Add to it one change at a time.")
        branches = change.get("branches")
        if (not isinstance(branches, list) or not 1 <= len(branches) <= SEED_MAX
                or not all(isinstance(b, dict) and set(b) <= SEED_BRANCH for b in branches)):
            raise MapError("Choose one to %d branches for the first map." % SEED_MAX)
        for branch in branches:
            add(branch.get("id"), branch.get("title"), branch.get("parent"), branch.get("description", ""),
                branch.get("evidence", []))
        for branch in branches:
            edit(branch["id"], links=links_for(branch["id"], branch.get("links", [])))

    if source == "observer" and action != "seed" and not refs(change.get("evidence")):
        raise MapError("Automatic map changes need supporting evidence.")
    if action == "add":
        add(key, change.get("title"), change.get("parent"), change.get("description", ""),
            change.get("evidence", []), change.get("links", []))
        return changed, touched

    node = None if action == "seed" else get(key, active=action != "restore")
    if action == "seed":
        seed()
    elif action == "rename":
        title = words(change.get("title"), 160, True)
        if title == node["title"]:
            raise MapError("The title is already current.")
        check_observer_title(title)
        if source == "observer" and node["manual"]:
            raise MapError("This title was corrected by a user.")
        if any(n["id"] != key and not n["deleted"] and n["parent"] == node["parent"] and
               n["title"].casefold() == title.casefold() for n in nodes.values()):
            raise MapError("The parent already has a branch with this title.")
        evidence = list(dict.fromkeys(node["evidence"] + refs(change.get("evidence", []))))
        edit(key, title=title, aliases=list(dict.fromkeys(node["aliases"] + [node["title"]])),
             evidence=evidence, manual=source == "user")
    elif action == "update":
        values = {}
        if "description" in change:
            values["description"] = words(change["description"], 1600)
        if "links" in change:
            values["links"] = links_for(key, change["links"])
        if "evidence" in change:
            values["evidence"] = refs(change["evidence"])
        if not values or all(v == node[k] for k, v in values.items()):
            raise MapError("No branch details changed.")
        edit(key, **values)
    elif action == "move":
        if "parent" not in change or change["parent"] == node["parent"]:
            raise MapError("Choose a different parent.")
        parent_for(key, change["parent"])
        edit(key, parent=change["parent"])
    elif action == "delete":
        policy = change.get("children")
        if policy not in ("subtree", "keep"):
            raise MapError("Choose whether to keep children or delete the subtree.")
        edit(key, deleted=True, deleted_by=op_id)
        queue = child_ids(key)
        while queue:
            child = queue.pop()
            if policy == "keep":
                edit(child, parent=node["parent"])
            else:
                edit(child, deleted=True, deleted_by=op_id)
                queue.extend(child_ids(child))
    elif action == "restore":
        if not node["deleted"]:
            raise MapError("The branch is already visible.")
        for other in nodes.values():
            if other["deleted_by"] == node["deleted_by"]:
                edit(other["id"], deleted=False, deleted_by=None)
    elif action == "merge":
        into = ident(change.get("into"))
        parent_for(key, into)
        target = get(into)
        evidence = list(dict.fromkeys(target["evidence"] + node["evidence"]))
        links = list(dict.fromkeys(n for n in target["links"] + node["links"] if n not in (key, into)))
        edit(into, links=links, evidence=evidence)
        edit(key, deleted=True, deleted_by=op_id)
        for other in nodes.values():
            if other["deleted"] or other["id"] in (key, into):
                continue
            if other["parent"] == key:
                edit(other["id"], parent=into)
            if key in other["links"]:
                edit(other["id"], links=list(dict.fromkeys(into if n == key else n for n in other["links"])))
    else:  # split: keep the original as the organizing branch and assign its children to new branches
        branches = change.get("branches")
        if not isinstance(branches, list) or not 2 <= len(branches) <= 24:
            raise MapError("Choose two or more branches for the split.")
        assigned, titles = set(), set()
        for branch in branches:
            if not isinstance(branch, dict) or set(branch) - {"id", "title", "children"}:
                raise MapError("Invalid split branch.")
            title = words(branch.get("title"), 160, True)
            if title.casefold() in titles:
                raise MapError("Give each split branch a distinct title.")
            titles.add(title.casefold())
            child = ident(branch.get("id"))
            add(child, title, key)
            for moved in strings(branch.get("children", []), ident):
                if moved in assigned or get(moved)["parent"] != key or moved not in nodes:
                    raise MapError("A split can assign each existing direct child only once.")
                assigned.add(moved)
                edit(moved, parent=child)
    # Moves, merges, restoration, and promoting children can bring existing siblings into conflict.
    labels = set()
    for n in nodes.keys() | changed.keys():
        current = changed.get(n, nodes.get(n))
        if current["deleted"]:
            continue
        label = (current["parent"], current["title"].casefold())
        if label in labels:
            raise MapError("The resulting parent would have duplicate branch titles.")
        labels.add(label)
        if n in changed:
            parent_for(n, current["parent"])
    return changed, touched


def prepare(state, change, source="user", op_id=None):
    change = copy.deepcopy(change)
    if isinstance(change, dict):
        if change.get("action") in ("add", "seed"):
            change.setdefault("id", new_id())
        if change.get("action") == "seed" and isinstance(change.get("branches"), list):
            for branch in change["branches"]:
                if isinstance(branch, dict):
                    branch.setdefault("id", new_id())
        if change.get("action") == "split" and isinstance(change.get("branches"), list):
            for branch in change["branches"]:
                if isinstance(branch, dict):
                    branch.setdefault("id", new_id())
    op_id = ident(new_id() if op_id is None else op_id)
    _, touched = _transform(state, change, source, op_id)
    return {"version": 1, "id": op_id, "source": source, "base": state["revision"], "change": change,
            "expected": {key: digest(state["nodes"].get(key)) for key in sorted(touched)}}


def apply(state, envelope, at="", author="", comment=None):
    if (not isinstance(envelope, dict) or set(envelope) != {"version", "id", "source", "base", "change", "expected"}
            or type(envelope["version"]) is not int or envelope["version"] != 1 or
            not isinstance(envelope["base"], str) or not HASH.fullmatch(envelope["base"])):
        raise MapError("Unknown or invalid map operation.")
    op_id = ident(envelope["id"])
    fingerprint = digest(envelope)
    old = next((event for event in reversed(state["history"]) if event["id"] == op_id), None)
    if old:
        if old["digest"] != fingerprint:
            raise MapError("This operation identifier belongs to a different change.")
        return state
    changed, touched = _transform(state, envelope["change"], envelope["source"], op_id)
    if envelope["expected"] != {k: digest(state["nodes"].get(k)) for k in touched}:
        raise MapError("The branch changed after preview. Review a new preview.")
    nodes = dict(state["nodes"], **changed)
    change = envelope["change"]
    if change["action"] == "seed":   # the change names no branch: the event names the first one and counts them all
        first = nodes[change["branches"][0]["id"]]
        event = {"id": op_id, "digest": fingerprint, "node": first["id"], "action": "seed", "title": first["title"],
                 "before_title": None, "count": len(change["branches"]), "affected": sorted(changed), "at": at,
                 "author": author, "comment": comment, "source": envelope["source"]}
    else:
        event = {"id": op_id, "digest": fingerprint, "node": change["id"], "action": change["action"],
                 "title": nodes[change["id"]]["title"], "before_title": state["nodes"].get(change["id"], {}).get("title"),
                 "affected": sorted(changed), "at": at, "author": author, "comment": comment, "source": envelope["source"]}
    if change["action"] in ("move", "merge"):
        destination = change.get("parent") if change["action"] == "move" else change["into"]
        event["destination"] = nodes[destination]["title"] if destination is not None else "the project root"
    if change["action"] == "move":
        before = state["nodes"][change["id"]]["parent"]
        event["origin"] = state["nodes"][before]["title"] if before is not None else "the project root"
    return {"nodes": nodes, "history": state["history"] + [event], "revision": digest([state["revision"], fingerprint])}


def encode(repo, hub, operation):
    # A single-line JSON code block keeps authored titles from becoming Markdown images, mentions or links on GitHub.
    data = canonical({"repo": repo.lower(), "hub": hub, "operation": operation})
    return MARKER + "\n```json\n" + data + "\n```"


def decode(body, repo, hub):
    marker, _, raw = body.replace("\r\n", "\n").partition("\n")
    if marker.rstrip("\r") != MARKER or len(raw) > 62000:
        raise MapError("Unrecognized map record.")
    if not raw.startswith("```json\n") or not raw.endswith("\n```"):
        raise MapError("Invalid map record body.")
    data = json.loads(raw[len("```json\n"):-len("\n```")])
    if (not isinstance(data, dict) or data.get("repo") != repo.lower() or type(data.get("hub")) is not int or
            data["hub"] != hub):
        raise MapError("This record belongs to another project.")
    return data.get("operation")


def replay(repo, hub, allowed, rows):
    state = empty()
    logins = {u.lower() for u in allowed}
    for row in rows:  # callers supply verified immutable rows in increasing GitHub comment ID order
        if not isinstance(row["author"], str) or row["author"].lower() not in logins:
            continue
        try:
            op = decode(row["body"], repo, hub)
            state = apply(state, op, row["created_at"], row["author"], row["id"])
        except (ValueError, TypeError, RecursionError, UnicodeError):
            continue  # a malformed, conflicting, or out-of-scope entry cannot change the map
    return state


def read_map(db, hub, allowed):
    if not allowed or type(hub) is not int:
        return empty()
    found = db.execute("SELECT value FROM meta WHERE key='repo'").fetchone()
    if not found or not recortex.valid_repo(found[0]):
        return empty()
    rows = db.execute("SELECT c.id, c.body, c.author, c.created_at FROM comments c "
                      "JOIN comment_edit_reads r ON r.comment=c.id AND r.stamp=c.updated_at "
                      "WHERE c.issue=? AND c.created_at=c.updated_at AND r.complete=1 AND r.edits=0 "
                      "AND r.last_edited IS NULL AND c.body LIKE ? ORDER BY c.id", (hub, MARKER + "%"))
    return replay(found[0], hub, allowed, rows)

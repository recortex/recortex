#!/usr/bin/env python3
"""Show the record's structure as GitHub sub-issues: the hub's topics under the hub, each `active` task under its
destination topic.

The links are computed from the record (the hub's tables, a task's "Destination topic"), never believed from GitHub:
a task that already has another parent, or a topic someone moved by hand, is reported and left alone. The writing
needs only the `repo` scope that gh already has. Nothing is removed and no issue is edited.

plan     the links the record wants, and which are missing or differ on GitHub (reads GitHub; writes nothing).
apply    adds each missing link, then reads every one back through Issue.parent. --dry-run prints the exact commands.

create_issues.py calls link() for a topic or task it creates.

Examples:
  sub_issues.py plan
  sub_issues.py apply --dry-run
  sub_issues.py apply
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_api  # noqa: E402
import index  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402

BATCH = 40
LIMIT = 60   # links one `apply` writes at most; the rest waits for the next run
UNREAD = "sub-issues: GitHub could not be read just now; run the command again."
REFUSED = ("sub-issues: GitHub refused the link (an older GitHub Enterprise Server, or more than 100 sub-issues "
           "under one parent); the hub's tables stay the structure.")
MENTION = re.compile(r"(?<![\w/&#])#([0-9]{1,7})\b")


def destination(db, number):
    """The topic a task points at: the first #N of its "Destination topic" section that is an issue labeled
    final-record; else the only final-record issue that shares the task's topic: label; else None."""
    row = db.execute("SELECT text FROM sections WHERE issue = ? AND heading = 'Destination topic' ORDER BY ordinal",
                     (number,)).fetchone()
    topics = {r["issue"] for r in db.execute("SELECT issue FROM labels WHERE lower(label) = 'final-record'")}
    for match in MENTION.finditer(row["text"] if row else ""):
        if int(match.group(1)) in topics:
            return int(match.group(1))
    found = index.topic_issues(db, number)
    return found[0] if len(found) == 1 else None


def wanted(db, hub):
    """[(parent, child)] the record wants, hub to topics first, then topic to tasks."""
    links = []
    labelled = {}
    for row in db.execute("SELECT issue, lower(label) AS label FROM labels"):
        labelled.setdefault(row["label"], set()).add(row["issue"])
    for topic in sorted(labelled.get("final-record", ())):
        if hub and topic != hub:
            links.append((hub, topic))
    for plan_issue in sorted(labelled.get("plan", ())):
        if hub and plan_issue != hub:
            links.append((hub, plan_issue))  # the plan of the paper sits under the hub, beside the topics
    for task in sorted(labelled.get("active", ())):
        topic = destination(db, task)
        if topic is not None and topic != task:
            links.append((topic, task))
    return links


def read_parents(repo, numbers, timeout=60):
    """({number: node id}, {number: parent number or None}) read from GitHub in batches."""
    owner, name = repo.split("/")
    ids, parents = {}, {}
    numbers = sorted(set(numbers))
    for start in range(0, len(numbers), BATCH):
        part = numbers[start:start + BATCH]
        aliases = " ".join("i%d: issue(number: %d) { id parent { number repository { nameWithOwner } } }" % (n, n)
                           for n in part)
        data = board_api.graphql("query { repository(owner: %s, name: %s) { %s } }"
                                 % (board_api.quote(owner), board_api.quote(name), aliases), timeout)
        found = data.get("repository") or {}
        for number in part:
            node = found.get("i%d" % number)
            if not isinstance(node, dict) or not isinstance(node.get("id"), str) \
                    or not board_api.GH_ID.fullmatch(node["id"]):
                continue
            ids[number] = node["id"]
            parent = node.get("parent")
            same = isinstance(parent, dict) and type(parent.get("number")) is int and \
                str((parent.get("repository") or {}).get("nameWithOwner", "")).lower() == repo.lower()
            parents[number] = parent["number"] if same else (-1 if isinstance(parent, dict) else None)
    return ids, parents


def add_text(parent_id, child_id):
    return ("mutation { addSubIssue(input: {issueId: %s, subIssueId: %s}) { issue { id } } }"
            % (board_api.quote(board_api.ident(parent_id)), board_api.quote(board_api.ident(child_id))))


def link(repo, parent, child, timeout=60):
    """Make `child` a sub-issue of `parent`. "linked", "already", "other" (it has another parent, left alone),
    "refused" (the add failed and the link is not there), "mismatch" (GitHub took the add and the link does not read
    back) or "unread" (GitHub could not be read before or after). Never raises."""
    try:
        ids, parents = read_parents(repo, [parent, child], timeout)
    except board_api.BoardError:
        return "unread"
    if parent not in ids or child not in ids:
        return "unread"
    if parents.get(child) == parent:
        return "already"
    if parents.get(child) is not None:
        return "other"
    try:
        board_api.graphql(add_text(ids[parent], ids[child]), timeout)
        failed = False
    except board_api.BoardError:
        failed = True  # it may have acted before it failed: the read below says
    try:
        after = read_parents(repo, [child], timeout)[1].get(child)
    except board_api.BoardError:
        return "unread"
    if after == parent:
        return "linked"
    return "refused" if failed else "mismatch"


def difference(repo, links):
    """(rows, ids): each row is (parent, child, state) with state ok, missing, other or unknown."""
    numbers = {n for pair in links for n in pair}
    ids, parents = read_parents(repo, numbers)
    rows = []
    for parent, child in links:
        if child not in ids or parent not in ids:
            rows.append((parent, child, "unknown"))
        elif parents.get(child) == parent:
            rows.append((parent, child, "ok"))
        elif parents.get(child) is None:
            rows.append((parent, child, "missing"))
        else:
            rows.append((parent, child, "other"))
    return rows, ids


def show(rows):
    words = {"missing": "is not linked yet", "other": "has another parent on GitHub (left alone)",
             "unknown": "was not found on GitHub"}
    for parent, child, state in rows:
        if state != "ok":
            print("#%d under #%d: %s" % (child, parent, words[state]))
    done = sum(1 for r in rows if r[2] == "ok")
    print("sub-issues: %d of %d link(s) are in place" % (done, len(rows)))


def main(argv=None, result=None):
    """The command. `result`, when a dict is given, is filled for a caller that needs more than the exit code: "linked"
    (links written and read back in place) and "waiting" (links left for the next run by --limit). The output is the same."""
    result = result if isinstance(result, dict) else {}
    result.update(linked=0, waiting=0)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--repo", help="owner/name (default: from the project file)")
        p.add_argument("--hub", type=int, help="hub issue number (default: from the project file)")
        if name == "apply":
            p.add_argument("--dry-run", action="store_true", help="print the exact commands; write nothing")
            p.add_argument("--pause", type=float, default=1.0, help="seconds between writes")
            p.add_argument("--limit", type=int, default=LIMIT, metavar="N",
                           help="links written at most in one run (default %d); run again for the rest" % LIMIT)
    args = parser.parse_args(argv)
    off = record_store.off_line_of_project("sub-issues")
    if off:
        print(off)
        return 0
    config = recortex.load_config()
    repo = recortex.resolve_repo(args.repo, config)
    hub = recortex.resolve_hub(args.hub, config)
    print(recortex.describe(config, repo))
    db = index.connect(repo, create=False)
    if db is None:
        sys.exit("error: no local index yet; run index.py sync first")
    try:
        links = wanted(db, hub)
    finally:
        db.close()
    if not links:
        print("sub-issues: the record names no topic or task to link")
        return 0
    try:
        rows, ids = difference(repo, links)
    except board_api.BoardError:
        print(UNREAD)
        return 1
    show(rows)
    if args.command == "plan":
        return 0
    todo = [(p, c) for p, c, state in rows if state == "missing"]
    if args.dry_run:
        for parent, child in todo[:args.limit]:
            print("would run: " + recortex.show_command(["api", "graphql", "-f", "query=" + add_text(ids[parent],
                                                                                                      ids[child])]))
        print("dry run: nothing was written")
        return 0
    failed, written, stopped = 0, [], False
    for parent, child in todo[:args.limit]:
        written.append((parent, child))  # read back even when the call fails: GitHub may have acted first
        try:
            board_api.graphql(add_text(ids[parent], ids[child]))
        except board_api.BoardError:
            stopped = True
            break
        if args.pause > 0:
            time.sleep(args.pause)
    if written:
        try:
            _, parents = read_parents(repo, [c for _, c in written])
        except board_api.BoardError:
            print(UNREAD)
            return 1
        for position, (parent, child) in enumerate(written):
            if parents.get(child) == parent:
                result["linked"] += 1
                print("linked: #%d is under #%d" % (child, parent))
            elif not (stopped and position == len(written) - 1):
                print("MISMATCH: #%d does not read back under #%d" % (child, parent))
                failed += 1
    if stopped:
        print(REFUSED)
        failed += 1
    if len(todo) > args.limit:
        result["waiting"] = len(todo) - args.limit
        print("sub-issues: %d more link(s) wait; at most %d are written at a time; run again"
              % (len(todo) - args.limit, args.limit))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

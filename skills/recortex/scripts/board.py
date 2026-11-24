#!/usr/bin/env python3
"""Keep a private GitHub Projects board of the work in step with the record. The record stays authoritative.

The board holds a card for each open `active` task, each open pull request and each issue waiting for triage, and for
those finished in the last 30 days. Where a card belongs is computed from the record by board_rules.py (the table is
in references/board.md); the board is a copy that can be deleted and filled again.

plan     offline, from the index: the cards that differ from the record, as behind, moved by hand (someone moved the
         card since the plugin wrote it) or without an issue in the record. Changes nothing.
apply    syncs the index, reads the board, adds the missing cards, writes Status and Why for the cards that are behind,
         leaves a hand-moved card alone and lists it, then reads every changed card back. --dry-run prints the exact
         commands. --restore N puts one hand-moved card back, after the user's answer. Only the main session runs it;
         hooks never write to GitHub.
setup    creates or links the board (private, with the Status options, the Why field and three views), records the
         user's approval of owner and number on this machine and writes the `board` entry of the project file.
         Needs the `project` scope of the gh login, which the plugin never asks for on its own.

Examples:
  board.py plan
  board.py apply --dry-run
  board.py apply
  board.py apply --restore 13
  board.py setup --mode new --yes
  board.py setup --mode existing --number 3 --dry-run
"""

import argparse
import contextlib
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_api  # noqa: E402
import board_rules  # noqa: E402
import index  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402

STATUS_NAMES = board_api.STATUSES


def failure(code):
    """A fixed line for a board failure, with the one command to look further."""
    return "board: not done: %s. Run hooks.py --doctor." % board_api.REASONS[code]


def context(args, need_approved=True):
    """(config, repo, hub, owner, number) after the checks every command makes. Exits with a fixed line."""
    try:
        config = recortex.load_config()
    except (OSError, ValueError) as error:
        sys.exit("error: the configuration file cannot be read (%s)" % error)
    repo = recortex.resolve_repo(getattr(args, "repo", None), config)
    if repo.lower() not in recortex.trusted_repos():
        sys.exit("error: %s is not in your trusted repositories (%s); the board is not used for it"
                 % (repo, recortex.trusted_target()))
    state, owner, number = board_api.entry(config, repo)
    if state == "none":
        sys.exit("board: this project has no board; ask to set one up (board.py setup)")
    if need_approved and state != "approved":
        code = {"entry": "entry", "owner": "entry", "unapproved": "unapproved"}[state]
        sys.exit(failure(code))
    return config, repo, recortex.resolve_hub(getattr(args, "hub", None), config), owner, number


def say_cards(title, rows, words):
    if rows:
        print("%s: %s" % (title, ", ".join("#%d" % r["number"] if isinstance(r, dict) else "#%d" % r for r in rows)))
    for row in rows:
        if isinstance(row, dict) and words:
            print("  #%d: %s" % (row["number"], words(row)))


def moved_words(row):
    board = row["board"] if row.get("board") in STATUS_NAMES else "another column"
    return "the record says %s; the board says %s" % (row["status"], board)


def print_plan(found):
    """The differences, as fixed wording, numbers and the plugin's own status names; nothing from the board."""
    say_cards("moved by hand", found["moved"], moved_words)
    say_cards("behind the record", found["behind"], lambda r: "the record says %s" % r["status"])
    say_cards("not on the board yet", found["missing"], lambda r: "the record says %s" % r["status"])
    say_cards("on the board with no issue in the record", found["gone"], None)
    if not (found["moved"] or found["behind"] or found["missing"] or found["gone"]):
        print("board: every card is where the record puts it")


def open_index(repo):
    db = index.connect(repo, create=False)
    if db is None:
        sys.exit("error: no local index yet; run index.py sync first")
    return db


def plan_command(args):
    config, repo, hub, owner, number = context(args)
    db = open_index(repo)
    try:
        if not db.execute("SELECT 1 FROM board_cards LIMIT 1").fetchone():
            print("board: the sync has not read the board yet; `board.py apply` reads it and fills it in")
        found = board_rules.plan(db, recortex.allowed_users(repo))
    finally:
        db.close()
    print_plan(found)
    return 0


def deadline_left(seconds):
    end = time.time() + seconds

    def left():
        remaining = end - time.time()
        if remaining <= 0:
            raise board_api.BoardError("budget")
        return remaining

    return left


def read_board(owner, number, repo):
    """The checked project, its cards, and the repository's ids. Raises BoardError with a fixed reason."""
    board_api.scope_probe()
    project = board_api.read_project(owner, number)
    if project["closed"]:
        raise board_api.BoardError("notfound")
    if not project["status"] or not project["why"] or any(n not in project["status"]["ids"] for n in STATUS_NAMES):
        raise board_api.BoardError("field")
    info, _ = board_api.repo_ids(repo, [])
    if info["private"] and project["public"]:
        raise board_api.BoardError("public")
    cards = board_api.read_cards(project, repo, deadline_left(120))
    return project, cards, info


def apply_command(args):
    config, repo, hub, owner, number = context(args)
    print(recortex.describe(config, repo))
    try:
        summary = index.sync(repo, hub, timeout=60)
        if not summary.get("complete"):
            print("note: the index was read in part; the plan below is from what it holds")
    except index.SyncBusy:
        print("note: another sync is running; the plan is from the index as it is")
    except recortex.GhError:
        print("note: GitHub could not be read for the record; the plan is from the index as it is")
    try:
        project, cards, info = read_board(owner, number, repo)
    except board_api.BoardError as error:
        print(failure(error.code))
        return 1
    db = open_index(repo)
    try:
        allowed = recortex.allowed_users(repo)
        wanted = board_rules.desired(db, allowed)
        known = {row["number"] for row in db.execute("SELECT number FROM issues")}
        closed = {row["number"] for row in db.execute("SELECT number FROM issues WHERE state = 'closed'")}
    finally:
        db.close()
    found = board_rules.compare(wanted, cards, known, closed)
    by_number = {w["number"]: w for w in wanted}
    if args.restore is not None:
        want = by_number.get(args.restore)
        if want is None:
            print("board: #%d is not a card the record wants (an `active` task, a pull request, or an issue "
                  "waiting for triage)" % args.restore)
            return 1
        found = {"missing": [want] if args.restore not in cards else [], "behind": [dict(want, why_only=False)]
                 if args.restore in cards else [], "moved": [], "gone": []}
        work = found["missing"] + found["behind"]
    else:
        print_plan(found)
        work = found["missing"] + found["behind"]
    if not work:
        print("board: nothing to write")
        moved_note(found, args)
        return 0
    skipped = max(0, len(work) - args.limit)
    work = work[:args.limit]
    need = [w["number"] for w in work if w["number"] not in cards]
    contents = {}
    if need:
        try:
            _, contents = board_api.repo_ids(repo, need)
        except board_api.BoardError as error:
            print(failure(error.code))
            return 1
    status_ids, why_field, status_field = project["status"]["ids"], project["why"], project["status"]["id"]
    if args.dry_run:
        for want in work:
            number = want["number"]
            if number in cards:
                item = cards[number]["item"]
            else:
                item = "<item of #%d>" % number
                print("would run: " + recortex.show_command(["api", "graphql", "-f", "query=" + add_text(
                    project["id"], contents.get(number, "<node of #%d>" % number))]))
            values = [] if want.get("why_only") else [(status_field, ("option", status_ids[want["status"]]))]
            print("would run: " + recortex.show_command(["api", "graphql", "-f", "query=" + board_api.values_text(
                project["id"], item, values + [(why_field, ("text", want["mark"]))], check=False)]))
        if skipped:
            print("would leave %d card(s) for the next run (at most %d are written at a time)" % (skipped, args.limit))
        print("dry run: nothing was written")
        return 0
    wrote, errors = {}, 0
    for position, want in enumerate(work):
        number = want["number"]
        try:
            if number in cards or number in contents:
                wrote[number] = want  # read back even when a write below fails: GitHub may have acted first
            if number in cards:
                item = cards[number]["item"]
            else:
                if number not in contents:
                    print("board: #%d was not found on GitHub; left out" % number)
                    errors += 1
                    continue
                item = board_api.add_item(project["id"], contents[number])
                pause(args)
            values = [] if want.get("why_only") else [(status_field, ("option", status_ids[want["status"]]))]
            board_api.set_values(project["id"], item, values + [(why_field, ("text", want["mark"]))])
            pause(args)
        except board_api.BoardError as error:
            print("board: #%d was not finished: %s; run again to go on" % (number, board_api.REASONS[error.code]))
            errors += 1
            break
    if wrote:
        try:
            after = board_api.read_cards(project, repo, deadline_left(120))
        except board_api.BoardError as error:
            print(failure(error.code))
            return 1
        for number, want in sorted(wrote.items()):
            card = after.get(number)
            if not card or card["status"] != want["status"] or card["why"] != want["mark"]:
                print("MISMATCH: #%d does not read back as %s" % (number, want["status"]))
                errors += 1
            else:
                print("written: #%d is in %s" % (number, want["status"]))
        if not index.store_board(repo, after):
            print("note: another sync is running; board.py plan shows the board as that sync reads it")
    if skipped:
        print("board: %d more card(s) wait; run again" % skipped)
    moved_note(found, args)
    return 1 if errors else 0


APPLY_CODES = ("ok", "nothing", "partial", "failed", "no-board", "entry", "unapproved", "untrusted", "refused", "error")


def apply_for(repo, config, options=None):
    """The worker's entry to `board.py apply` (level Everything): the same run for this project, as a short fixed-code
    result, never an exception and never a word of what GitHub or the board said.

    options: {"limit": cards at most (never above board_rules.WRITE_CAP), "pause": seconds between writes (default 1),
    "dry_run": bool}. Returns {"code", "reason", "written", "waiting"}: code is one of APPLY_CODES ("ok" when it wrote
    cards, "nothing" when every card was where the record puts it, "partial" when a write did not finish or did not read
    back, "failed" for a failure of the board, with `reason` a key of board_api.REASONS, "no-board", "entry" and
    "unapproved" for the project file's entry, "untrusted" for a repository that is not trusted, "refused" when a check
    of the command stopped it, "error" for anything else); `written` is the number of cards written and read back,
    `waiting` the number left for the next run (the cap of one run). Cards moved by hand are left alone, as in the
    command, and nothing here writes to the record."""
    options = options if isinstance(options, dict) else {}
    out = {"code": "error", "reason": "", "written": 0, "waiting": 0}
    try:
        if not isinstance(repo, str) or repo.lower() not in recortex.trusted_repos():
            return dict(out, code="untrusted")
        state, owner, number = board_api.entry(config if isinstance(config, dict) else {}, repo)
        if state != "approved":
            return dict(out, code={"none": "no-board", "entry": "entry", "owner": "entry", "unapproved": "unapproved"}[state])
        limit, pause = options.get("limit"), options.get("pause")
        args = argparse.Namespace(
            repo=repo, hub=None, dry_run=bool(options.get("dry_run")), restore=None,
            pause=float(pause) if type(pause) in (int, float) and 0 <= pause <= 5 else 1.0,
            limit=min(limit, board_rules.WRITE_CAP) if type(limit) is int and limit >= 1 else board_rules.WRITE_CAP)
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                status = apply_command(args)
        except SystemExit:
            return dict(out, code="refused")
        except board_api.BoardError as error:
            return dict(out, code="failed", reason=error.code)
        except recortex.GhError:
            return dict(out, code="failed", reason="github")
        text = printed.getvalue()
        written = len(re.findall(r"^written: #[0-9]+ is in ", text, re.M))
        waiting = re.search(r"^board: ([0-9]+) more card\(s\) wait", text, re.M)
        out.update(written=written, waiting=int(waiting.group(1)) if waiting else 0)
        failed = re.search(r"^board: not done: (.*)\. Run hooks\.py --doctor\.$", text, re.M)
        if failed:
            reasons = [key for key, words in board_api.REASONS.items() if words == failed.group(1)]
            return dict(out, code="failed", reason=reasons[0] if reasons else "")
        if status:
            return dict(out, code="partial")
        return dict(out, code="ok" if written else "nothing")
    except Exception:  # noqa: BLE001 - the worker never fails over the board
        return out


def moved_note(found, args):
    if found["moved"] and args.restore is None:
        print("board: %d card(s) were moved by hand and left alone; ask the user whether to record what the move "
              "means or to put each one back (board.py apply --restore N)" % len(found["moved"]))


def pause(args):
    if args.pause > 0:
        time.sleep(args.pause)


def add_text(project_id, content_id):
    return "mutation { addProjectV2ItemById(input: {projectId: %s, contentId: %s}) { item { id } } }" % (
        board_api.quote(project_id), board_api.quote(content_id))


# ---------------------------------------------------------------- setup

def set_up(args):
    """Create or link the board, make it ready, record the approval and the entry. Exit 2: the scope is missing."""
    try:
        config = recortex.load_config()
    except (OSError, ValueError) as error:
        sys.exit("error: the configuration file cannot be read (%s)" % error)
    repo = recortex.resolve_repo(args.repo, config)
    owner = repo.split("/")[0]
    if args.mode == "existing" and not args.number:
        sys.exit("error: --number names the existing board")
    print(recortex.describe(config, repo))
    steps = ["check that the gh login has the project scope",
             "create a private board owned by %s and link it to %s" % (owner, repo) if args.mode == "new" else
             "use board %d of %s, set it private, and add what is missing (nothing is removed or renamed)"
             % (args.number or 0, owner),
             "add the Status options (%s), the Why field and the views (%s)" % (
                 ", ".join(n for n in STATUS_NAMES if n not in board_api.EXISTING_STATUSES),
                 ", ".join(v[0] for v in board_api.VIEWS)),
             "record your approval of this board on this machine and write the board entry of the project file"]
    if args.dry_run:
        for step in steps:
            print("would: " + step)
        print("dry run: nothing was written")
        return 0
    try:
        board_api.scope_probe()
    except board_api.BoardError as error:
        if error.code == "scope":
            print(board_api.SCOPE_LINE)
            return 2
        print(failure(error.code))
        return 1
    made_number = None
    try:
        info, _ = board_api.repo_ids(repo, [])
        if args.mode == "new":
            name = repo.split("/")[1]
            made = board_api.graphql(
                "mutation { createProjectV2(input: {ownerId: %s, title: %s, repositoryId: %s}) { projectV2 { id number } } }"
                % (board_api.quote(info["owner_id"]), board_api.quote("%s record" % name), board_api.quote(info["id"])))
            number = ((made.get("createProjectV2") or {}).get("projectV2") or {}).get("number")
            if type(number) is not int:
                raise board_api.BoardError("github")
            made_number = number
            print("created: a new board, number %d, linked to %s" % (number, repo))
        else:
            number = args.number
        project = board_api.read_project(owner, number)
        if project["closed"]:
            raise board_api.BoardError("notfound")
        texts = ", shortDescription: %s, readme: %s" % (board_api.quote(board_api.DESCRIPTION),
                                                        board_api.quote(board_api.README)) if args.mode == "new" else ""
        board_api.graphql("mutation { updateProjectV2(input: {projectId: %s, public: false%s}) { projectV2 { id } } }"
                          % (board_api.quote(project["id"]), texts))
        project = board_api.read_project(owner, number)
        if project["public"]:
            raise board_api.BoardError("public")
        if not project["status"]:
            raise board_api.BoardError("field")
        missing = [n for n in STATUS_NAMES if n not in project["status"]["ids"]]
        if missing:
            options = ", ".join("{id: %s, name: %s, color: %s, description: %s}" % (
                board_api.quote(ident), board_api.quote(label or "", False), color or "GRAY",
                board_api.quote(desc or "", False))
                for ident, label, color, desc in project["status"]["full"])
            options += (", " if options else "") + ", ".join("{name: %s, color: %s, description: \"\"}" % (
                board_api.quote(n), board_api.COLORS[n]) for n in missing)
            board_api.graphql("mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) "
                              "{ projectV2Field { ... on ProjectV2SingleSelectField { id } } } }"
                              % (board_api.quote(project["status"]["id"]), options))
            print("added: the Status options %s" % ", ".join(missing))
        if not project["why"]:
            board_api.graphql("mutation { createProjectV2Field(input: {projectId: %s, dataType: TEXT, name: \"Why\"}) "
                              "{ projectV2Field { ... on ProjectV2Field { id } } } }" % board_api.quote(project["id"]))
            print("added: the Why field")
        made_views = []
        for view, layout, filter_text in board_api.VIEWS:
            if view in project["views"]:
                continue
            made_views.append(view)
            made = board_api.graphql("mutation { createProjectV2View(input: {projectId: %s, name: %s, layout: %s}) "
                                     "{ projectV2View { id } } }" % (board_api.quote(project["id"]),
                                                                    board_api.quote(view), layout))
            view_id = (((made.get("createProjectV2View") or {}).get("projectV2View")) or {}).get("id")
            if filter_text and view_id:
                board_api.graphql("mutation { updateProjectV2View(input: {viewId: %s, filter: %s}) "
                                  "{ projectV2View { id } } }" % (board_api.quote(board_api.ident(view_id)),
                                                                 board_api.quote(filter_text)))
            print("added: the view %s" % view)
        project = board_api.read_project(owner, number)
        if project["public"]:
            raise board_api.BoardError("public")
        if not project["why"] or any(n not in project["status"]["ids"] for n in STATUS_NAMES):
            raise board_api.BoardError("field")
        if any(v[0] not in project["views"] for v in board_api.VIEWS) \
                or not all(project["filters"].get(v) for v in made_views) \
                or (args.mode == "new" and (not project["texts"] or repo.lower() not in project["repos"])):
            raise board_api.BoardError("setup")
    except board_api.BoardError as error:
        if error.code == "scope":  # a login that reads projects but cannot write them fails at the first write
            print(board_api.SCOPE_LINE)
        else:
            print(failure(error.code))
        if args.mode == "new" and type(made_number) is int:
            print("board: the new board is number %d; run `board.py setup --mode existing --number %d --yes` to "
                  "finish it" % (made_number, made_number))
        return 1
    print("private: the board is private (read back)")
    for line in board_api.lines_for(project):
        print(line)
    print("in the browser, optional and not required by the plugin: keep the two default workflows (closed and merged "
          "give Done); add an auto-add workflow with the filter `is:issue label:active` and one with `is:pr`; leave "
          "off any workflow that closes an issue when its Status changes, because it would turn a dragged card into "
          "a change of the record. For people who are not you, invite them under Manage access: a board of a user "
          "account does not include the repository's collaborators.")
    try:
        board_api.record_approval(repo, owner, number)
    except Exception as error:  # noqa: BLE001 - one fixed line
        sys.exit("error: your approval of the board was not recorded (%s); the project file was not changed"
                 % type(error).__name__)
    print("recorded: your approval of board %s/%d on this machine" % (owner, number))
    write_entry(config, owner, number)
    print("board: %s/%d" % (owner, number))
    return 0


def write_entry(config, owner, number):
    import setup
    path = config.get("_path")
    if not path:
        print("note: there is no project file here, so the board entry was not written; add "
              "\"board\": {\"owner\": \"%s\", \"number\": %d} to it" % (owner, number))
        return
    data = json.loads(recortex.read_text(path))
    data["board"] = {"owner": owner, "number": number}
    setup.write_text(path, json.dumps(data, indent=2) + "\n", False, "the board entry", os.path.dirname(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "setup"):
        p = sub.add_parser(name)
        p.add_argument("--repo", help="owner/name (default: from the project file)")
        if name != "setup":
            p.add_argument("--hub", type=int, help="hub issue number (default: from the project file)")
        if name == "apply":
            p.add_argument("--dry-run", action="store_true", help="print the exact commands; write nothing")
            p.add_argument("--restore", type=int, metavar="N", help="put the hand-moved card of issue N back")
            p.add_argument("--pause", type=float, default=1.0, help="seconds between writes (GitHub asks for one)")
            p.add_argument("--limit", type=int, default=board_rules.WRITE_CAP, metavar="N",
                           help="cards written at most in one run (default %d); run again for the rest"
                           % board_rules.WRITE_CAP)
        if name == "setup":
            p.add_argument("--mode", choices=("new", "existing"), required=True)
            p.add_argument("--number", type=int, help="the number of an existing board")
            p.add_argument("--dry-run", action="store_true", help="say what would be done; write nothing")
            p.add_argument("--yes", action="store_true", help="the user chose a board and agreed")
    args = parser.parse_args(argv)
    off = record_store.off_line_of_project("board")
    if off:
        print(off)
        return 0
    if args.command == "setup" and not args.dry_run and not args.yes:
        sys.exit("error: pass --dry-run to see the steps, or --yes once the user agreed")
    try:
        if args.command == "plan":
            return plan_command(args)
        return apply_command(args) if args.command == "apply" else set_up(args)
    except recortex.GhError:
        print("board: not done: %s. Run hooks.py --doctor." % board_api.REASONS["github"])
        return 1


if __name__ == "__main__":
    sys.exit(main())

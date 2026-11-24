#!/usr/bin/env python3
"""Tasks and their steps: the task list under "## Plan" of a task's body, opening and closing a task, to-do items, and
steps checked or dropped by the user's word.

The record stays authoritative. A task is an `active` issue; its steps are `steps` memories on it; a step is checked by
the `done=` mark of a later memory and dropped by the `dropped=` mark of a decision (references/issue-model.md, "Tasks,
steps and marks"; the rules are task_rules.py). The list in the body is a copy that code writes from the memories, never
believed the other way round: a box ticked on GitHub is not a mark.

status   the tasks, their steps with the memory that checked each, and what is out of step. Read-only, from the index.
render   --issue N: replace only the text under "## Plan" with the computed list, through post_verified.py body
         --section, which refuses a body that changed meanwhile and reads back. A list that was changed by hand is not
         rendered; --restore puts it back on purpose.
close    --issue N: when every step is done or dropped (and one is done), write "Result" (one line per memory that
         checked a step), render, and close the issue as completed. A task with no steps is never closed.
reopen   --issue N: reopen it and put "Not finished" back in "Result".
open     --dir D: open a task from a drafted steps memory (D holds steps.md and task.json {"title", "topic"}): one
         `active` issue and the steps memory as its first comment, through create_issues.py, so a rerun opens no second.
add      --text-file F: put a to-do item (the user's words) on the standing "To do" task, which is opened first when
         there is none.
mark     --task N --steps 2,3 --words-file F: a checkpoint on the task with done= for those steps, by the user's words.
drop     --task N --steps 4 --words-file F: a decision on the task with dropped= for those steps, by the user's words.

Every command that writes has --dry-run, which lints and prints the exact gh commands and writes nothing to GitHub.
After a post, add, mark and drop bring the index up to date and render the task, unless --no-render.

Examples:
  tasks.py status
  tasks.py render --issue 14 --dry-run
  tasks.py close --issue 14
  tasks.py add --text-file .tmp/item.txt
  tasks.py mark --task 14 --steps 2,3 --words-file .tmp/words.txt
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import lint_drafts  # noqa: E402
import post_verified  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402
import record_format  # noqa: E402
import storage  # noqa: E402
import task_rules  # noqa: E402

FOLDER = "tasks"          # <index folder>/tasks: the drafts, manifests, state files and backups of this script
ITEM_CAP = 300
WORDS_CAP = 1200
TITLE_CAP = 120
TODO_PLAIN = "keep one more item on the to-do list"
BY_HAND = ("not rendered: the list under ## Plan of #%d was changed by hand on GitHub%s. A box ticked there is not a "
           "mark. To keep what the user did, record it (tasks.py mark --task %d --steps N --words-file F); to put the "
           "list back, run tasks.py render --issue %d --restore.")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def say(text):
    print(text)


def load(args, trusted=True):
    """(config, repo, allowed) after the checks every command makes. Exits with one line."""
    try:
        config = recortex.load_config()
    except (OSError, ValueError) as error:
        sys.exit("error: the configuration file cannot be read (%s)" % error)
    repo = recortex.resolve_repo(getattr(args, "repo", None), config)
    allowed = recortex.allowed_users(repo)
    if trusted and not allowed:
        sys.exit("error: %s is not in your trusted repositories (%s); tasks are told only from the accounts you allow"
                 % (repo, recortex.trusted_target()))
    return config, repo, allowed


def open_index(repo):
    try:
        db = index.connect(repo, create=False)
    except Exception as error:  # noqa: BLE001 - refused storage is one fixed line
        sys.exit("error: the local index cannot be read (%s); run index.py sync" % type(error).__name__)
    if db is None:
        sys.exit("error: no local index yet; run index.py sync first")
    if not index.derived_current(db):
        sys.exit(index.NOT_READY)
    return db


def report_of(repo, allowed, closed_days=index.CLOSED_DAYS):
    db = open_index(repo)
    try:
        return index.tasks(db, allowed, closed_days=closed_days)
    finally:
        db.close()


def find_task(report, number):
    return next((task for task in report["tasks"] if task["issue"] == number), None)


def refresh(repo, config):
    """Bring the index up to date after a post; a failure is one line, never a failed post."""
    hub = config.get("hub") if type(config.get("hub")) is int and config.get("hub") >= 1 else None
    try:
        index.sync(repo, hub, timeout=60, deadline=300)
    except (recortex.GhError, index.SyncBusy) as error:
        say("note: the index was not brought up to date (%s); render the task later with tasks.py render" % error)
        return False
    return True


def scratch(repo, *parts):
    """A private folder of this script's files in the index's folder."""
    return storage.cache_folder(repo, FOLDER, *parts)


def unique(prefix, suffix):
    return "%s-%s-%s%s" % (prefix, now().strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3), suffix)


def write(folder, name, text):
    path = os.path.join(folder, name)
    storage.write_file(path, text)
    return path


def one_line(text, cap):
    value = " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text or "").split())
    return value if len(value) <= cap else value[:cap - 3] + "..."


def quoted(words):
    """The user's words as a block quote, line for line, nothing changed."""
    lines = (words or "").replace("\r\n", "\n").strip("\n").split("\n")
    return "\n".join(("> " + line).rstrip() for line in lines)


def read_words(path, cap=WORDS_CAP):
    if not path or not os.path.isfile(path):
        sys.exit("error: not a file: %s" % path)
    text = recortex.read_text(path).strip()
    if not text:
        sys.exit("error: %s is empty; it holds the user's own words" % path)
    if len(text) > cap:
        sys.exit("error: %s holds %d characters; at most %d" % (path, len(text), cap))
    return text


def numbers_words(numbers):
    """[2, 3, 4] as 'steps 2, 3 and 4'; [2] as 'step 2'."""
    numbers = ["%d" % n for n in numbers]
    if len(numbers) == 1:
        return "step " + numbers[0]
    return "steps " + ", ".join(numbers[:-1]) + " and " + numbers[-1]


def parse_steps(text):
    found = []
    for part in (text or "").split(","):
        if not re.fullmatch(r"[1-9][0-9]{0,2}", part.strip()):
            sys.exit("error: --steps is a list of step numbers such as 2,3")
        if int(part) not in found:
            found.append(int(part))
    return sorted(found)


# ---------------------------------------------------------------- the commands that read

def status_command(args):
    config, repo, allowed = load(args)
    report = report_of(repo, allowed)
    if args.json:
        say(json.dumps(report, indent=1, sort_keys=True))
        return 0
    index.print_tasks(report)
    for task in report["tasks"]:
        if task["hand"]:
            which = " (%s differ%s)" % (numbers_words(task["hand_steps"]), "s" if len(task["hand_steps"]) == 1 else "") \
                if task["hand_steps"] else ""
            say(BY_HAND % (task["issue"], which, task["issue"], task["issue"]))
    if report["todo"] is None:
        say("There is no standing \"To do\" task yet; tasks.py add opens it with the first item.")
    else:
        say("The standing \"To do\" task is #%d." % report["todo"])
    return 0


# ---------------------------------------------------------------- render, close, reopen

def post_body(repo, issue, section, text, base, backup, dry_run):
    """post_verified.py body --section: the text of one section replaces what is there and every other line stays.
    Returns post_verified's exit code."""
    folder = scratch(repo)
    path = write(folder, unique("%s-%d" % (section.lower(), issue), ".md"), text)
    argv = ["body", "--repo", repo, "--issue", str(issue), "--kind", "active", "--section", section, "--body-file", path,
            "--expected-body-file", base]
    if dry_run:
        argv.append("--dry-run")
    else:
        argv += ["--backup", backup]
    code = post_verified.main(argv)
    if not dry_run and os.path.isfile(backup):
        os.chmod(backup, 0o600)   # post_verified wrote it with the umask's mode; it holds a body, so it is private
    return code


def render_task(repo, config, issue, report=None, expected=None, restore=False, dry_run=False, before_write=None):
    """Make the list under "## Plan" of task `issue` the one the memories compute, and say what happened. Returns a dict:
    ok (nothing is wrong), wrote (the body was replaced), reason (a fixed word: "written", "no change", "no steps",
    "no plan", "by hand", "changed", "failed"), backup (the file that holds the replaced body, or None).

    The live body is read now and passed on as the body the write expects, so that post_verified.py refuses a body that
    changed meanwhile; `expected` is a body read earlier, which must still be the live one. An optional `before_write`
    callback lets an interactive caller recheck its authority after the reads and before the write helper."""
    out = {"ok": True, "wrote": False, "reason": "no change", "backup": None}
    allowed = recortex.allowed_users(repo)
    report = report if report is not None else report_of(repo, allowed)
    task = find_task(report, issue)
    if task is None:
        say("not rendered: #%d is not a task of the record (an open `active` issue that an allowed account opened)" % issue)
        return dict(out, ok=False, reason="not a task")
    live = recortex.read_issue_body(repo, issue)
    if expected is not None and recortex.normalize(live) != recortex.normalize(expected):
        say("not rendered: the live body of #%d changed since it was read; read it again and rerun" % issue)
        return dict(out, ok=False, reason="changed")
    if not task["steps"]:
        say("not rendered: #%d has no told steps memory yet, so there is no list to write" % issue)
        return dict(out, reason="no steps")
    if task_rules.hand_changed(live) and not restore:
        say(BY_HAND % (issue, "", issue, issue))
        return dict(out, ok=False, reason="by hand")
    if task_rules.plan_list(live)[2] is False:
        say("not rendered: the body of #%d has no '## Plan' section, so it is left alone" % issue)
        return dict(out, reason="no plan")
    if task_rules.list_agrees(live, task["steps"]):
        say("no change: the list under ## Plan of #%d is already current" % issue)
        return out
    folder = scratch(repo)
    base = write(folder, unique("base-%d" % issue, ".md"), live)
    backup = os.path.join(folder, unique("render-%d" % issue, ".md"))
    if before_write is not None:
        before_write()
    code = post_body(repo, issue, "Plan", task_rules.plan_section(task["steps"]), base, backup, dry_run)
    if code != 0:
        return dict(out, ok=False, reason="failed")
    return dict(out, wrote=not dry_run, reason="written", backup=None if dry_run else backup)


def render_command(args):
    config, repo, allowed = load(args)
    expected = recortex.read_text(args.expected_body_file) if args.expected_body_file else None
    result = render_task(repo, config, args.issue, expected=expected, restore=args.restore, dry_run=args.dry_run)
    return 0 if result["ok"] else 1


def close_task(repo, config, issue, dry_run=False, report=None):
    """Close task `issue` as completed when every step is done or dropped (and one is done): write its Result, render its
    list, close it and read it back. Says what happened, and returns {"ok", "reason", "backup"}: reason is a fixed word
    ("closed", "not a task", "not finished", "by hand", "failed", "dry run"), backup the file that holds the replaced body
    (None when nothing was written). A task with no steps is never closed by code, and the standing "To do" task is not
    closed by the observer's jobs (they never ask for it)."""
    out = {"ok": False, "reason": "failed", "backup": None}
    report = report if report is not None else report_of(repo, recortex.allowed_users(repo))
    task = find_task(report, issue)
    if task is None or task["state"] != "open":
        say("not closed: #%d is not an open task of the record" % issue)
        return dict(out, reason="not a task")
    if not task_rules.finished(task["steps"]):
        left = [row["n"] for row in task["steps"] if row["state"] in ("open", "blocked")]
        if not task["steps"]:
            say("not closed: #%d has no steps, and a task with no steps is never closed by code" % issue)
        elif left:
            say("not closed: #%d still has %s open" % (issue, numbers_words(left)))
        else:
            say("not closed: no step of #%d is done; a task whose steps were all dropped is not finished" % issue)
        return dict(out, reason="not finished")
    live = recortex.read_issue_body(repo, issue)
    if task_rules.hand_changed(live):
        say(BY_HAND % (issue, "", issue, issue))
        return dict(out, reason="by hand")
    rows, seen = [], set()
    for row in task["steps"]:
        by = row["by"]
        if by is not None and by["id"] not in seen and row["state"] == "done":
            seen.add(by["id"])
            rows.append((by["plain"], by["url"] or "comment %d" % by["id"]))
    stamp = recortex.stamp(now(), config)
    folder = scratch(repo)
    base = write(folder, unique("base-%d" % issue, ".md"), live)
    backup = os.path.join(folder, unique("close-%d" % issue, ".md"))
    if post_body(repo, issue, "Result", task_rules.result_section(rows, stamp), base, backup, dry_run) != 0:
        return out
    if dry_run:
        say("would run: " + record_store.for_repo(repo).show("close", issue, "completed"))
        say("(the task list under ## Plan is rendered after the Result is written)")
        return dict(out, ok=True, reason="dry run")
    result = render_task(repo, config, issue, report=report)
    if not result["ok"]:
        say("the Result is written, but the list could not be rendered; the task stays open")
        return dict(out, backup=backup)
    records = record_store.for_repo(repo)
    records.close(issue, "completed")
    state = records.read_issue(issue).get("state")
    if state != "closed":
        say("MISMATCH: #%d reads back as %s; it was not closed" % (issue, state))
        return dict(out, backup=backup)
    counts = task["counts"]
    say("closed: #%d as completed, %d of %d steps done; backup of the earlier body: %s"
        % (issue, counts["done"], counts["steps"], backup))
    return {"ok": True, "reason": "closed", "backup": backup}


def close_command(args):
    config, repo, allowed = load(args)
    return 0 if close_task(repo, config, args.issue, dry_run=args.dry_run, report=report_of(repo, allowed))["ok"] else 1


def reopen_task(repo, config, issue, dry_run=False):
    """Reopen task `issue` and put "Not finished" (or the Result the close replaced) back from the backup the close left.
    Returns {"ok", "backup"} (the file that holds the replaced body, None when nothing was written)."""
    folder = scratch(repo)
    live = recortex.read_issue_body(repo, issue)
    before = sorted(name for name in os.listdir(folder) if name.startswith("close-%d-" % issue))
    earlier = None
    if before:
        earlier = record_format.section_text(recortex.read_text(os.path.join(folder, before[-1])), "Result")
    if earlier is None or not earlier.strip() or earlier.startswith("Finished"):
        earlier = "%s Reopened %s." % (task_rules.NOT_FINISHED, recortex.stamp(now(), config))
    if dry_run:
        say("would run: " + record_store.for_repo(repo).show("reopen", issue))
    else:
        records = record_store.for_repo(repo)
        records.reopen(issue)
        state = records.read_issue(issue).get("state")
        if state != "open":
            say("MISMATCH: #%d reads back as %s; it was not reopened" % (issue, state))
            return {"ok": False, "backup": None}
    base = write(folder, unique("base-%d" % issue, ".md"), live)
    backup = os.path.join(folder, unique("reopen-%d" % issue, ".md"))
    if post_body(repo, issue, "Result", earlier, base, backup, dry_run) != 0:
        return {"ok": False, "backup": None}
    if not dry_run:
        say("reopened: #%d; its Result says \"Not finished\" again" % issue)
    return {"ok": True, "backup": None if dry_run else backup}


def reopen_command(args):
    config, repo, allowed = load(args)
    return 0 if reopen_task(repo, config, args.issue, dry_run=args.dry_run)["ok"] else 1


# ---------------------------------------------------------------- open a task, and the to-do list

def steps_goal(text):
    return record_format.plain(text, "steps") or ""


def open_task(repo, directory, dry_run=False, config=None):
    """Open a task from the drafts in `directory`: steps.md (a steps memory) and task.json ({"title", "topic"}, topic the
    number of the destination topic or 0). One `active` issue whose body code writes (task_rules.task_body), and the steps
    memory as its first comment, through create_issues.py with a manifest in this script's private folder. The folder is
    named after the drafts, so a rerun finds create_issues.py's state file and opens no second task.

    Returns {"ok", "issue", "comment", "folder"}; issue and comment are None when nothing was created (a dry run, or a
    failure)."""
    import create_issues
    config = config if config is not None else recortex.load_config()
    steps_path, task_path = os.path.join(directory, "steps.md"), os.path.join(directory, "task.json")
    for path in (steps_path, task_path):
        if not os.path.isfile(path):
            say("not opened: %s is missing" % path)
            return {"ok": False, "issue": None, "comment": None, "folder": None}
    memory = recortex.read_text(steps_path)
    try:
        spec = json.loads(recortex.read_text(task_path))
        title, topic = spec["title"], spec["topic"]
        if not isinstance(title, str) or not title.strip() or type(topic) is not int or topic < 0 or set(spec) - {"title", "topic"}:
            raise ValueError("form")
    except (ValueError, KeyError, TypeError):
        say("not opened: task.json is {\"title\": \"...\", \"topic\": N} (N is 0 for the standing to-do list)")
        return {"ok": False, "issue": None, "comment": None, "folder": None}
    title = one_line(title, TITLE_CAP)
    steps = record_format.steps_list(memory)
    if not steps:
        say("not opened: steps.md lists no steps")
        return {"ok": False, "issue": None, "comment": None, "folder": None}
    assessed = [{"n": number, "comment": 0, "ordinal": number, "text": text, "state": "open", "by": None}
                for number, text in enumerate(steps, 1)]
    body = task_rules.task_body(steps_goal(memory), topic, record_format.field_text(memory, "Done when") or "",
                                assessed, recortex.stamp(now(), config))
    key = hashlib.sha256(json.dumps([repo.lower(), title, topic, recortex.normalize(memory)]).encode("utf-8")).hexdigest()[:16]
    folder = scratch(repo, key)
    earlier = os.path.join(folder, "task.md")
    if os.path.isfile(earlier):
        body = recortex.read_text(earlier)   # a rerun keeps the body the issue was created with, so create_issues.py adopts it
    write(folder, "steps.md", memory)
    write(folder, "task.md", body)
    manifest = write(folder, "manifest.json", json.dumps({
        "repo": repo, "issues": [{"key": "task", "kind": "active", "title": title, "labels": ["active"],
                                  "body_file": "task.md"}],
        "comments": [{"issue": "NEW-task", "kind": "steps", "body_file": "steps.md"}]}, indent=1))
    argv = ["--manifest", manifest, "--repo", repo] + (["--dry-run"] if dry_run else [])
    code = create_issues.main(argv)
    out = {"ok": code == 0, "issue": None, "comment": None, "folder": folder}
    if dry_run:
        return out
    state_path = manifest + ".state.json"
    state = {}
    if os.path.isfile(state_path):
        os.chmod(state_path, 0o600)   # create_issues.py wrote it with the umask's mode, in this private folder
        state = json.loads(recortex.read_text(state_path))
    out["issue"] = (state.get("issues", {}).get("task") or {}).get("number")
    for slot, entry in (state.get("comments") or {}).items():
        match = re.search(r"#issuecomment-([0-9]+)", entry.get("url") or "")
        if match:
            out["comment"] = int(match.group(1))
    return out


def open_command(args):
    config, repo, allowed = load(args, trusted=False)
    result = open_task(repo, args.dir, dry_run=args.dry_run, config=config)
    if result["ok"] and not args.dry_run and result["issue"]:
        say("opened: task #%d, with its steps memory %s" % (result["issue"], result["comment"] or "(see above)"))
        if not args.no_render:
            say("the list under ## Plan is already written; tasks.py status shows the task once the index has synced")
    return 0 if result["ok"] else 1


def comment_command(repo, issue, kind, text, dry_run):
    """post_verified.py comment for one drafted comment; returns its exit code."""
    folder = scratch(repo)
    path = write(folder, unique("%s-%d" % (kind, issue), ".md"), text)
    argv = ["comment", "--repo", repo, "--issue", str(issue), "--kind", kind, "--body-file", path]
    if dry_run:
        argv.append("--dry-run")
    return post_verified.main(argv)


def after_post(repo, config, issue, args):
    if args.dry_run or args.no_render:
        return 0
    if not refresh(repo, config):
        return 0
    result = render_task(repo, config, issue)
    return 0 if result["ok"] else 1


def add_command(args):
    config, repo, allowed = load(args)
    words = read_words(args.text_file)
    item = " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", words).split())
    if len(item) > ITEM_CAP:
        sys.exit("error: a to-do item is one line of at most %d characters" % ITEM_CAP)
    moment = now()
    stamp, at = recortex.stamp(moment, config), recortex.marker_time(moment, config)
    who = one_line(args.who, 60)
    memory = ("<!-- ghim:event type=steps at=%s -->\n### Steps: %s - a to-do item\n\n"
              "**In plain words, we agreed to:** %s\n\n**Agreed:** By %s in the chat, %s:\n\n%s\n\n"
              "**Steps:**\n1. %s\n\n**Done when:** The item is done and a memory says so.\n"
              % (at, stamp, TODO_PLAIN, who, stamp, quoted(words), item))
    report = report_of(repo, allowed)
    if report["todo"] is None:
        folder = scratch(repo, "add-" + hashlib.sha256(memory.encode("utf-8")).hexdigest()[:12])
        write(folder, "steps.md", memory)
        write(folder, "task.json", json.dumps({"title": "To do", "topic": 0}))
        result = open_task(repo, folder, dry_run=args.dry_run, config=config)
        if result["ok"] and not args.dry_run:
            say("opened: the standing \"To do\" task #%s with this item" % result["issue"])
        return 0 if result["ok"] else 1
    code = comment_command(repo, report["todo"], "steps", memory, args.dry_run)
    if code != 0:
        return code
    return after_post(repo, config, report["todo"], args)


def marked_steps(args, report, wanted):
    """(task, [step rows], groups text) for the steps of task args.task that a mark may name now, or exits."""
    task = find_task(report, args.task)
    if task is None or task["state"] != "open":
        sys.exit("error: #%d is not an open task of the record" % args.task)
    by_number = {row["n"]: row for row in task["steps"]}
    missing = [n for n in parse_steps(args.steps) if n not in by_number]
    if missing:
        sys.exit("error: #%d has no %s (it has %d step%s)" % (args.task, numbers_words(missing), len(by_number),
                                                              "" if len(by_number) == 1 else "s"))
    rows = [by_number[n] for n in parse_steps(args.steps) if by_number[n]["state"] not in wanted]
    if not rows:
        sys.exit("nothing to record: %s of #%d %s already" % (numbers_words(parse_steps(args.steps)), args.task, wanted[0]))
    groups = {}
    for row in rows:
        groups.setdefault(row["comment"], []).append(row["ordinal"])
    if len(groups) > 4:
        sys.exit("error: a mark names at most four steps memories; record the steps in two commands")
    value = ";".join("%d:%s" % (cid, ",".join(str(n) for n in groups[cid])) for cid in sorted(groups))
    return task, rows, value


def mark_command(args):
    config, repo, allowed = load(args)
    words = read_words(args.words_file)
    report = report_of(repo, allowed)
    task, rows, value = marked_steps(args, report, ("done", "dropped"))
    numbers = [row["n"] for row in rows]
    moment = now()
    stamp, at = recortex.stamp(moment, config), recortex.marker_time(moment, config)
    who = one_line(args.who, 60)
    memory = ("<!-- ghim:event type=checkpoint at=%s done=%s -->\n### Checkpoint: %s - %s done\n\n"
              "**In plain words, we paused here:** %s of the task %s done, as the user said\n\n"
              "**State:** %s said so in the chat, %s, in these words:\n\n%s\n\n"
              "**Done since the last checkpoint:** %s of this task, checked by the user's word.\n\n"
              "**In progress:** None recorded.\n\n**Next actions:** None recorded.\n\n**Open questions:** None.\n"
              % (at, value, stamp, numbers_words(numbers), numbers_words(numbers), "is" if len(numbers) == 1 else "are",
                 who.capitalize(), stamp, quoted(words), numbers_words(numbers).capitalize()))
    code = comment_command(repo, args.task, "checkpoint", memory, args.dry_run)
    return code if code != 0 else after_post(repo, config, args.task, args)


def drop_command(args):
    config, repo, allowed = load(args)
    words = read_words(args.words_file)
    report = report_of(repo, allowed)
    task, rows, value = marked_steps(args, report, ("dropped",))
    numbers = [row["n"] for row in rows]
    moment = now()
    stamp, at = recortex.stamp(moment, config), recortex.marker_time(moment, config)
    who = one_line(args.who, 60)
    memory = ("<!-- ghim:event type=decision at=%s dropped=%s -->\n### Decision: %s - drop %s\n\n"
              "**In plain words, we decided to:** drop %s of this task\n\n"
              "**Decision:** %s of this task %s dropped: no longer counted, so the task can finish without %s.\n\n"
              "**Decided by:** %s, %s\n\n**Exact words:**\n\n%s\n\n**Where recorded:** In the chat, %s.\n\n"
              "**Effect:** The task's list under Plan shows %s as dropped.\n\n**Next step:** None, the task goes on with "
              "its other steps\n"
              % (at, value, stamp, numbers_words(numbers), numbers_words(numbers), numbers_words(numbers).capitalize(),
                 "is" if len(numbers) == 1 else "are", "it" if len(numbers) == 1 else "them", who, stamp, quoted(words),
                 stamp, numbers_words(numbers)))
    code = comment_command(repo, args.task, "decision", memory, args.dry_run)
    return code if code != 0 else after_post(repo, config, args.task, args)


# ---------------------------------------------------------------- command line

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, help_text, writes=True):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", help="owner/name (default: from the project file)")
        if writes:
            p.add_argument("--dry-run", action="store_true", help="lint and print the gh commands; write nothing to GitHub")
        return p

    p = command("status", "the tasks, their steps and what is out of step (read-only)", writes=False)
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p = command("render", "write the computed list under ## Plan of a task")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--expected-body-file", help="the body as read earlier; it must still be the live one")
    p.add_argument("--restore", action="store_true", help="put the list back although it was changed by hand")
    p = command("close", "close a finished task")
    p.add_argument("--issue", type=int, required=True)
    p = command("reopen", "reopen a task")
    p.add_argument("--issue", type=int, required=True)
    p = command("open", "open a task from a drafted steps memory")
    p.add_argument("--dir", required=True, help="the folder with steps.md and task.json")
    p.add_argument("--no-render", action="store_true", help=argparse.SUPPRESS)
    for name, text in (("add", "put a to-do item on the standing To do task"),
                       ("mark", "check steps by the user's words"), ("drop", "drop steps by the user's words")):
        p = command(name, text)
        p.add_argument("--no-render", action="store_true", help="do not bring the index up to date and render afterwards")
        p.add_argument("--who", default="the user", help="the name or role the words are quoted under")
        if name == "add":
            p.add_argument("--text-file", required=True, help="the user's words: the item")
        else:
            p.add_argument("--task", type=int, required=True)
            p.add_argument("--steps", required=True, help="the numbers of the steps, as the task's list shows them: 2,3")
            p.add_argument("--words-file", required=True, help="the user's own words")
    args = parser.parse_args(argv)
    return {"status": status_command, "render": render_command, "close": close_command, "reopen": reopen_command,
            "open": open_command, "add": add_command, "mark": mark_command, "drop": drop_command}[args.command](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (recortex.GhError, storage.StorageError, index.SyncBusy) as error:
        sys.exit("error: %s" % error)

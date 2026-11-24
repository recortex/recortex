"""The observer's housekeeping jobs (spec 8.2): copies of the record that code keeps current. No model call, no hook.

After a pass that posted something, and once a day, the worker runs `run` under its lock. Each step is bounded by the
day's `max_renders`, is recorded as an act when it writes (`store.add_act`, which `observer.py log` lists and `undo`
reverses where it can) and stops only itself when it fails: the reason is one fixed word and the next step goes on.

1. sync    the record is read again (the worker's own refresh).
2. render  the task list under "## Plan" of every open task whose computed list differs from its body and was not changed
           by hand (tasks.render_task: it reads the live body and refuses one that changed).
3. close   a finished task is closed (level Everything), or the user is asked to close it (Assistant). Never the standing
           "To do" task, never a task with no steps, never one whose list was changed by hand.
4. hub     the hub's "## Active issues" table, from the open `active` issues, when the hub body has that heading.
5. links   sub_issues.py apply, for tasks the observer opened (Everything): "ok" when it linked something, "nothing" when
           there was nothing to link (or only links another parent holds, which it leaves alone), "waiting" when its cap
           of one run left links for the next run, "failed" when GitHub refused or could not be read.
6. board   board.apply_for, when this machine has a board for the project (Everything).
Steps 2 to 4 need the level Assistant or Everything; Notes only runs none of 2 to 6. In shadow mode nothing runs at all.
Once a day, before step 1, one more step takes away the manuscript keeper's old working folders (clean_keeper): a plain
local cleanup of the plugin's own private files, no act, counted in `jobs_last`.
Text from the record reaches a page of GitHub here only as a title told by an allowed account, cleaned to one line.
"""
import contextlib
import io
import os
import re
import time

import board
import index
import observer_runtime as runtime
import post_verified
import record_store
import recortex
import record_format
import storage
import sub_issues
import task_rules
import tasks

DAY = 86400
RENDER_ACTS = ("task_rendered", "task_closed", "hub_rendered", "board")   # what the day's max_renders counts
JOBS_DAY = "jobs_day"       # store control: when the daily run last happened
LEASE_LOST = "lease"        # the step word of a step that did not run because the worker no longer held the host lease
JOBS_LAST = "jobs_last"     # store control: {"at", "steps": {step: fixed word}, "cleaned": integer}
LINKS_LAST = "links_last"   # store control: when sub_issues apply last ran
CODES = ("ok", "nothing", "waiting", "asked", "failed", "level", "sync", "no_section", "shadow", "skipped", "by_hand", "lease")
HUB_HEADING = "Active issues"
HUB_HEAD = ["| Issue | Work in development | Destination topic |", "|---|---|---|"]
NOT_TOLD = "(title not told)"
TITLE_CAP = 120
HUB_ROWS = 60
KEEPER = "keeper"            # the folder of the keeper's working folders, beside the index: <repo>/keeper/<token>
KEEPER_FAILED = "keeper_failed"   # store control of the keeper (observer_keeper.FAILED): {topic: {"key", "at"}}
KEEPER_DAYS = 14             # a working folder goes this long after its last change, whatever the state of its draft
KEEPER_GONE = ("applied", "dropped", "stale", "refused")   # the states of a draft whose folder is no longer wanted
KEEPER_NAME = re.compile(r"[0-9a-f]{16}|[0-9a-f]{8}-approved")   # what the keeper makes: its token, a draft's approved copy


def spent(store, now):
    """The acts of the last day that max_renders counts."""
    return len([row for row in store.acts(days=1, kinds=RENDER_ACTS) if row["at"] > now - DAY])


def one_line(text, cap=TITLE_CAP):
    """A title as one plain line for a table cell: whitespace folded, no pipe, no control character, cut at `cap`."""
    clean = "".join(c if c.isprintable() else " " for c in str(text or ""))
    return " ".join(clean.replace("|", "/").split())[:cap].strip()


def quiet(function, *args, **kwargs):
    """(result, what it printed): the scripts the jobs call tell a user what they do; the worker keeps none of it."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            return function(*args, **kwargs), out.getvalue()
    except SystemExit as error:
        return {"ok": False, "reason": "failed", "exit": error.code}, out.getvalue()


def backup_name(path):
    """The name of a backup file (never its folder), or "" when there is none."""
    return os.path.basename(path) if isinstance(path, str) and path else ""


def keep_after(repo, issue, backup):
    """Next to the backup of a body, a copy of the body as it is now, so that `undo` can tell a body that changed since."""
    if not backup:
        return
    try:
        folder = tasks.scratch(repo)
        tasks.write(folder, after_name(backup_name(backup)), recortex.read_issue_body(repo, issue))
    except (OSError, recortex.GhError, storage.StorageError, ValueError):
        pass


def after_name(name):
    return re.sub(r"\.md$", "", name) + ".after.md"


def bodies(db):
    return {row["number"]: row["body"] or "" for row in db.execute("SELECT number, body FROM issues WHERE is_pr = 0")}


# ---------------------------------------------------------------- the steps

def render_step(repo, config, store, report, texts, room, now):
    """Step 2. Returns (fixed word, rendered, left over for the day)."""
    done, waiting = 0, 0
    for task in report["tasks"]:
        if task["state"] != "open" or not task["steps"] or task["hand"]:
            continue
        body = texts.get(task["issue"], "")
        items, mark, has = task_rules.plan_list(body)
        if not has or task_rules.list_agrees(body, task["steps"]):
            continue
        if room - done <= 0:
            waiting += 1
            continue
        result, _ = quiet(tasks.render_task, repo, config, task["issue"], report=report)
        if result.get("wrote"):
            done += 1
            backup = backup_name(result.get("backup"))
            keep_after(repo, task["issue"], result.get("backup"))
            store.log_act("task_rendered", issue=task["issue"], backup=backup, at=now)
        elif not result.get("ok"):
            return ("failed" if not done else "ok"), done, waiting
    return ("waiting" if waiting else "ok" if done else "nothing"), done, waiting


def close_step(repo, config, store, options, report, room, now):
    """Step 3. Everything closes finished tasks; Assistant asks the user to (the question joins Store.waiting()), and
    never again for a task already put off until its time has passed. Returns (fixed word, closed)."""
    finished = [task for task in report["tasks"] if task["state"] == "open" and not task["todo"] and not task["hand"]
                and task_rules.finished(task["steps"])]
    if options["level"] != "everything":
        store.set_close_items([task["issue"] for task in finished], now=now)
        return ("asked" if finished else "nothing"), 0
    store.set_close_items([], now=now)
    closed = 0
    for task in finished:
        if room - closed <= 0:
            return "waiting", closed
        result, _ = quiet(tasks.close_task, repo, config, task["issue"], report=report)
        if result.get("reason") == "closed":
            closed += 1
            backup = result.get("backup")
            store.log_act("task_closed", issue=task["issue"], backup=backup_name(backup), at=now)
            keep_after(repo, task["issue"], backup)
        else:
            return ("failed" if not closed else "ok"), closed
    return ("ok" if closed else "nothing"), closed


def hub_rows(db, report):
    """The rows of the hub's active table: `| #N | title | #topic |` for each open task, "None yet" when there is none.
    A title that is not told is not shown."""
    rows = []
    for task in report["tasks"]:
        if task["state"] != "open":
            continue
        topic = sub_issues.destination(db, task["issue"])
        title = one_line(task["title"]) if task["title_told"] and task["title"] else NOT_TOLD
        rows.append("| #%d | %s | %s |" % (task["issue"], title, "#%d" % topic if type(topic) is int else "none yet"))
    return rows[:HUB_ROWS] or ["| None yet | | |"]


def hub_step(repo, config, store, db, report, room, now):
    """Step 4. Returns the fixed word."""
    hub = config.get("hub") if type(config.get("hub")) is int and config.get("hub") >= 1 else None
    if hub is None:
        return "nothing"
    live = recortex.read_issue_body(repo, hub)
    section = record_format.section_text(live, HUB_HEADING)
    if section is None:
        return "no_section"
    wanted = "\n".join(HUB_HEAD + hub_rows(db, report))
    if recortex.normalize(section).strip() == recortex.normalize(wanted).strip():
        return "nothing"
    if room <= 0:
        return "waiting"
    folder = tasks.scratch(repo)
    base = tasks.write(folder, tasks.unique("base-hub-%d" % hub, ".md"), live)
    backup = os.path.join(folder, tasks.unique("hub-%d" % hub, ".md"))
    path = tasks.write(folder, tasks.unique("hub-section-%d" % hub, ".md"), wanted + "\n")
    argv = ["body", "--repo", repo, "--issue", str(hub), "--kind", "hub", "--section", HUB_HEADING, "--body-file", path,
            "--expected-body-file", base, "--backup", backup]
    code, _ = quiet(post_verified.main, argv)
    if code != 0:
        return "failed"
    if os.path.isfile(backup):
        os.chmod(backup, 0o600)
    keep_after(repo, hub, backup)
    store.log_act("hub_rendered", issue=hub, backup=backup_name(backup), at=now)
    return "ok"


def links_step(repo, config, store, now):
    """Step 5: sub_issues.py apply, when a task the observer opened is newer than the last run. Returns the fixed word."""
    opened = [row for row in store.acts(days=35, kinds=("task_opened",)) if row["at"] > store.get_control(LINKS_LAST, 0)]
    if not opened:
        return "nothing"
    result = {}
    code, text = quiet(sub_issues.main, ["apply", "--repo", repo, "--pause", "0"], result=result)
    if code not in (0, None):
        return "failed"
    if result.get("waiting"):
        return "waiting"   # the cap of one run left links: the time of this run is not kept, so the next run goes on
    store.set_control(LINKS_LAST, now)
    return "ok" if result.get("linked") or not result else "nothing"


def board_step(repo, config, store, now):
    """Step 6: board.apply_for, which respects board_rules.WRITE_CAP and never raises. Returns the fixed word."""
    result = board.apply_for(repo, config, {"pause": 0})
    code = result.get("code")
    if code == "ok":
        store.log_act("board", numbers=[min(max(int(result.get("written", 0)), 0), 10 ** 6)], at=now)
        return "waiting" if result.get("waiting") else "ok"
    if code in ("nothing", "no-board"):
        return "nothing"
    return "failed"


def _keeper_failed_at(store):
    """The newest time the keeper recorded a failed attempt (observer_keeper._failed), or 0."""
    found = store.get_control(KEEPER_FAILED, {})
    times = [row.get("at") for row in found.values() if isinstance(row, dict)] if isinstance(found, dict) else []
    return max([at for at in times if type(at) in (int, float) and at == at and abs(at) < 1e12], default=0)


def clean_keeper(repo, store, now):
    """Take away the keeper's working folders that nothing needs any more. Returns (fixed word, how many were removed).

    Only the folders of <private folder of this repository>/keeper that are named as the keeper names them (16 hex digits,
    or 8 and "-approved") are looked at, and each is removed through storage.remove_folder, which follows no link and
    refuses a folder that holds anything but the user's own regular files. A folder goes when
    - its draft is applied, dropped, stale or refused (a draft that waits or is put off keeps it), or
    - 14 days have passed since the draft was last changed or the folder was last written, whatever the state; a draft
      that still waited is then made stale, since its files are gone, or
    - it has no draft (an attempt that never recorded one), and either 14 days have passed or the keeper recorded a failed
      attempt after the folder was last written.
    A folder that cannot be removed is counted and the rest go on: the word is "failed"."""
    try:
        parent = storage.cache_folder(repo, KEEPER, create=False)
        names = sorted(os.listdir(parent)) if parent else []
    except (storage.StorageError, OSError):
        return "failed", 0
    names = [name for name in names if KEEPER_NAME.fullmatch(name)]
    if not names:
        return "nothing", 0
    owners = {}
    for draft in store.drafts():
        folder = os.path.basename(str(draft["folder"]))
        for name in (folder, draft["id"] + "-approved"):
            if KEEPER_NAME.fullmatch(name):
                owners.setdefault(name, []).append(draft)
    failed_at, removed, failed = _keeper_failed_at(store), 0, 0
    for name in names:
        try:
            info = os.lstat(os.path.join(parent, name))
        except FileNotFoundError:
            continue
        except OSError:
            failed += 1
            continue
        drafts = owners.get(name, [])
        changed = max([info.st_mtime] + [draft["updated"] for draft in drafts])
        old = now - changed >= KEEPER_DAYS * DAY
        if drafts:
            due = old or all(draft["state"] in KEEPER_GONE for draft in drafts)
        else:
            due = old or failed_at >= info.st_mtime
        if not due:
            continue
        try:
            if not storage.remove_folder(parent, name):
                continue
        except (storage.StorageError, OSError):
            failed += 1
            continue
        removed += 1
        for draft in drafts:
            if draft["state"] in ("waiting", "later"):
                try:
                    store.set_draft(draft["id"], "stale", reason="stale")
                except Exception:  # noqa: BLE001 - the files are gone; the next answer finds that out
                    pass
    return ("failed" if failed else "ok" if removed else "nothing"), removed


# ---------------------------------------------------------------- the run

def run(repo, config, store, options, refresh=None, posted=False, now=None, clock=None, holds=None):
    """The jobs of one worker run, as {step: fixed word}. Nothing runs unless the settings hold a level and the mode is
    automatic, and then only after a pass that posted something or when a day has passed since the last run. `refresh`
    is the worker's own refresh of the index (a callable returning True when the record was read); without it the index
    is taken as it is. `clock` is the worker's lifetime (observer_worker.Clock): a step that would start with none left is
    skipped ("time"), so that a slow step cannot take the jobs past it. `holds` renews the worker's host lease and says whether
    it still holds (observer_lease); it is asked before every step that can write to GitHub, and a step it refuses is not run
    and is recorded as LEASE_LOST, after which no later step runs either."""
    level = options.get("level")
    if level not in runtime.LEVELS or options.get("mode") != "automatic":
        return {"code": "shadow" if options.get("mode") != "automatic" else "level"}
    now = time.time() if now is None else now
    daily = now - _number(store.get_control(JOBS_DAY, 0)) >= DAY
    if not (posted or daily):
        return {"code": "not_due"}
    if daily:
        store.set_control(JOBS_DAY, now)
    steps = {"sync": "skipped", "keeper": "skipped"}
    cleaned = 0
    if daily:   # local files only: no GitHub, no level, and no hold on it but the gates the worker applied before the jobs
        if clock is not None and clock.remaining() <= 0:
            steps["keeper"] = "time"
        else:
            steps["keeper"], cleaned = _guard(clean_keeper, repo, store, now)

    def go(function, *args):
        if clock is not None and clock.remaining() <= 0:
            return {render_step: ("time", 0, 0), close_step: ("time", 0)}.get(function, "time")
        if holds is not None and not holds():
            return {render_step: (LEASE_LOST, 0, 0), close_step: (LEASE_LOST, 0)}.get(function, LEASE_LOST)
        return _guard(function, *args)

    if refresh is not None:
        try:
            steps["sync"] = "ok" if refresh() else "failed"
        except Exception:  # noqa: BLE001 - one step's trouble stops that step only
            steps["sync"] = "failed"
    if steps["sync"] == "failed":
        steps.update(render="sync", close="sync", hub="sync", links="sync", board="sync")
        return _keep(store, steps, now, cleaned)
    if level == "notes":
        steps.update(render="level", close="level", hub="level", links="level", board="level")
        return _keep(store, steps, now, cleaned)
    room = max(0, options.get("max_renders", 30) - spent(store, now))
    try:
        db = index.connect(repo, create=False, tighten=False)
    except Exception:  # noqa: BLE001
        db = None
    report = texts = None
    if db is not None:
        try:
            if index.derived_current(db):
                report = index.tasks(db, recortex.allowed_users(repo), closed_days=0)
                texts = bodies(db)
        except Exception:  # noqa: BLE001
            report = None
    if report is None:
        if db is not None:
            db.close()
        steps.update(render="failed", close="failed", hub="failed")
    else:
        try:
            steps["render"], rendered, _ = go(render_step, repo, config, store, report, texts, room, now)
            room -= rendered
            steps["close"], closed = go(close_step, repo, config, store, options, report, room, now)
            room -= closed
            steps["hub"] = go(hub_step, repo, config, store, db, report, room, now)
        finally:
            db.close()
    if level == "everything":
        steps["links"] = go(links_step, repo, config, store, now)
        steps["board"] = go(board_step, repo, config, store, now)
    else:
        steps["links"] = steps["board"] = "level"
    return _keep(store, steps, now, cleaned)


def _guard(function, *args):
    """What `function` returns, or "failed" in the shape it returns, when it raises: the rest of the steps go on."""
    try:
        return function(*args)
    except Exception:  # noqa: BLE001 - fixed word only, never an exception string
        shapes = {render_step: ("failed", 0, 0), close_step: ("failed", 0), clean_keeper: ("failed", 0)}
        return shapes.get(function, "failed")


def _number(value):
    return float(value) if type(value) in (int, float) and value == value and abs(value) < 1e12 else 0.0


def _keep(store, steps, now, cleaned=0):
    # `cleaned`: the keeper's folders this run removed
    store.set_control(JOBS_LAST, {"at": now, "steps": steps, "cleaned": cleaned})
    return steps


# ---------------------------------------------------------------- the acts log and undo (spec 8.4)

KIND_WORDS = {"memory": "posted a memory", "task_opened": "opened a task", "steps_added": "added steps to a task",
              "step_marked": "checked steps off", "task_rendered": "rendered a task list", "task_closed": "closed a task",
              "hub_rendered": "rendered the hub's active issues", "summary": "wrote the status summary",
              "board": "updated the board", "draft_proposed": "proposed a change of the paper's text",
              "draft_applied": "wrote a change of the paper's text"}
# What to say for an act that undo does not reverse: how it is corrected instead (fixed words).
CORRECTED = {"memory": "A posted comment is never edited or deleted: correct a memory with a new memory of kind correction.",
             "steps_added": "A posted comment is never edited or deleted: drop a step with tasks.py drop.",
             "step_marked": "A posted comment is never edited or deleted: drop the step's mark with tasks.py drop, or "
                            "correct the memory that carries it.",
             "summary": "A resume block is never edited: the next one replaces it.",
             "board": "The board is a copy of the record: change the record, or put a card back with board.py apply "
                      "--restore N.",
             "draft_proposed": "Nothing was written: answer the draft's question, or drop it with observer.py drafts "
                               "drop ID.",
             "draft_applied": "A text change is undone with versions.py restore."}
# Why undo wrote nothing, in fixed words, for the code it prints (observer.py adds the line to a code that has one).
UNDO_NOTES = {"changed_since": "Nothing was written: the body is no longer the one this act wrote, because a later act or "
                               "someone else changed it. Undo the newer act first, or put the text back by hand.",
              "no_backup": "Nothing was written: the copy of the body this act kept is gone, so there is nothing to put "
                           "back. Put the text back by hand.",
              "failed": "Nothing was written: GitHub or the check before the write refused it. observer.py log lists the "
                        "acts; try again in a minute.",
              "not_closed": "Nothing was written: that task is not closed now.",
              "not_open": "Nothing was written: that task is not open now.",
              "already_undone": "That act was undone before.",
              "no_such_act": "No act has that number: observer.py log lists them."}


def undo_note(code):
    """The fixed line that says why an undo changed nothing; "" for a code that has none (a success, or a kind that is
    corrected another way, which carries its own note)."""
    return UNDO_NOTES.get(code, "")


def address(repo, row):
    """The GitHub address of what an act is about, from checked integers."""
    if not row["issue"]:
        return ""
    return record_store.address_of(repo, row["issue"], row["comment"] or None)


def log_lines(store, repo, config, days):
    """One line for each act of the last `days` days, newest first: when, what, the address, the numbers and "(undone)"."""
    import datetime
    lines = []
    for row in store.acts(days=days):
        moment = datetime.datetime.fromtimestamp(row["at"], datetime.timezone.utc)
        text = "act %d  %s  %s" % (row["id"], recortex.stamp(moment, config), KIND_WORDS.get(row["kind"], row["kind"]))
        where = address(repo, row)
        if where:
            text += "  " + where
        if row["numbers"]:
            text += "  numbers " + " ".join(str(n) for n in row["numbers"])
        lines.append(text + ("  (undone)" if row["undone"] else ""))
    return lines


def _live_state(repo, issue):
    return record_store.for_repo(repo).read_issue(issue).get("state")


def _restore(repo, config, row, kind):
    """Put a body back from the backup an act left, when the body is still the one the act wrote. Returns a code."""
    folder = tasks.scratch(repo)
    name = row["backup"]
    if not name:
        return "no_backup"
    path, after = os.path.join(folder, name), os.path.join(folder, after_name(name))
    try:
        before_text, after_text = recortex.read_text(path), recortex.read_text(after)
    except (OSError, ValueError):
        return "no_backup"
    live = recortex.read_issue_body(repo, row["issue"])
    if recortex.normalize(live) != recortex.normalize(after_text):
        return "changed_since"
    base = tasks.write(folder, tasks.unique("base-undo-%d" % row["issue"], ".md"), live)
    kept = os.path.join(folder, tasks.unique("undo-%d" % row["issue"], ".md"))
    code, printed = quiet(post_verified.main, ["body", "--repo", repo, "--issue", str(row["issue"]), "--kind", kind,
                                               "--body-file", path, "--expected-body-file", base, "--backup", kept])
    if code != 0:
        # GitHub may still answer with the body from before a newer act (a read a moment after a write): the write's own
        # check then finds the body changed, and that is what the user is told, not a bare "failed".
        return "changed_since" if "differs from --expected-body-file" in printed else "failed"
    if os.path.isfile(kept):
        os.chmod(kept, 0o600)
    return "restored"


def undo(store, repo, config, ident):
    """Reverse act `ident`, where it can be: a closed task is reopened, a rendered body is restored from its backup
    (refused when the body changed since), a task the observer opened is closed as not planned. Any other kind says how it
    is corrected. Returns {"code": ..., "note": ...}; the act is marked undone only when it was reversed."""
    row = store.act(ident)
    if row is None:
        return {"code": "no_such_act"}
    if row["undone"]:
        return {"code": "already_undone"}
    kind = row["kind"]
    if kind in CORRECTED:
        return {"code": "not_reversible", "note": CORRECTED[kind]}
    try:
        if kind in ("task_rendered", "hub_rendered"):
            code = _restore(repo, config, row, "hub" if kind == "hub_rendered" else "active")
            done = code == "restored"
        elif kind == "task_closed":
            if _live_state(repo, row["issue"]) != "closed":
                return {"code": "not_closed"}
            done = quiet(tasks.reopen_task, repo, config, row["issue"])[0].get("ok") is True
            code = "reopened" if done else "failed"
        else:   # task_opened
            if _live_state(repo, row["issue"]) != "open":
                return {"code": "not_open"}
            record_store.for_repo(repo).close(row["issue"], "not planned")
            done = _live_state(repo, row["issue"]) == "closed"
            code = "closed_not_planned" if done else "failed"
    except (recortex.GhError, OSError, ValueError):
        return {"code": "failed"}
    if done:
        store.mark_undone(ident)
    return {"code": code}

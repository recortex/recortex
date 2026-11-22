#!/usr/bin/env python3
"""What recortex used on this machine, in tokens and calls, never in money.

Three kinds of numbers, from three places (references/observer.md, "Usage"):
  exact       the observer's own background calls, kept in its private store (observer_store.py: tier, job, model and the
              four token counts of every call). That store is also the ledger of the daily caps.
  exact       the plugin's own subagents (record-auditor and the others), summed from each subagent's own transcript when
              it stops (hooks.py subagent-stop), kept in usage.db.
  estimated   what the hooks add to a session's context: characters divided by 4, kept in usage.db.
The main session's own work on the record is not measured.

usage.db is a small SQLite file in the repository's private state folder (storage.py: folder 0700, file 0600, no symlink
followed). It holds integers, a tier, a job, an agent name from a fixed list, a model name of a fixed form and a day, never
text. A writer waits at most 50 ms for the file and then gives up: these are estimates, and a hook must not run late.
Rows older than 35 days are removed by cleanup(), which the observer's daily cleanup calls.

  usage.py show [--days 7|30] [--json]
"""

import argparse
import contextlib
import datetime
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recortex  # noqa: E402
import storage  # noqa: E402

DB_NAME = "usage.db"
WAIT_SECONDS = 0.05          # a writer waits at most this long for the file
KEEP_DAYS = 35
LIMIT = 10 ** 9              # a token count is a whole number from 0 to this
TIERS = ("sort", "middle", "strong")
JOBS = ("sort", "memories", "status", "paper", "map", "check")
AGENTS = ("record-auditor", "record-drafter", "record-reviewer", "record-planner", "record-planner-session", "ask")
AGENT_NAME = re.compile(r"(?:recortex:)?(record-auditor|record-drafter|record-reviewer|record-planner|record-planner-session|ask)")
MODEL = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Hook events whose context is counted: the four brief events and the observer's events that can carry context.
INJECTED = ("session-start", "subagent-start", "prompt-submit", "pre-compact",
            "observer-session-start", "observer-subagent-start", "observer-prompt-submit", "observer-pre-tool")
CHARS_PER_TOKEN = 4
NOT_MEASURED = ("The main session's own work on the record, what you and Claude do in the conversation itself, is not "
                "measured here.")
CAP_KEYS = {"sort": ("max_sort", "sort_tokens", 80, 800000), "middle": ("max_invocations", "middle_tokens", 70, 700000),
            "strong": ("max_strong", "strong_tokens", 8, 320000)}
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS agents(at INTEGER NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL, "
    "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_read INTEGER NOT NULL, "
    "cache_write INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS injected(day TEXT NOT NULL, event TEXT NOT NULL, characters INTEGER NOT NULL, "
    "count INTEGER NOT NULL, PRIMARY KEY(day, event))",
)


def whole(value):
    """A token count as a whole number from 0 to 10^9; anything else (text, a float, a negative, a bool) is 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(LIMIT, max(0, value))


def utc(now=None):
    return datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)


def day_of(now=None):
    return utc(now).strftime("%Y-%m-%d")


def model_name(value):
    return value if isinstance(value, str) and MODEL.fullmatch(value) else ""


# ---------------------------------------------------------------- the file

def db_path(repo, create=False):
    """<state top>/private/<owner>__<name>/usage.db, or None when the folder is missing and create is False. Raises
    storage.StorageError for a folder that is not safe."""
    folder = storage.folder(storage.state_top(), storage.repo_folder(repo), create=create)
    return None if folder is None else os.path.join(folder, DB_NAME)


def connect(repo, create=False):
    """The open database, or None when there is none and create is False. The file is made with mode 0600 and never
    through a symlink; a file with other permissions or another owner is refused (storage.StorageError). A writer waits at
    most WAIT_SECONDS; sqlite3.OperationalError then ends the attempt."""
    path = db_path(repo, create)
    if path is None:
        return None
    before = storage.check_path(path, "file")
    if not before:
        if not create:
            return None
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        before = storage.check_path(path, "file")
    db = sqlite3.connect(path, timeout=WAIT_SECONDS)
    try:
        if not storage.same_file(path, before):
            raise storage.StorageError("%s changed while it was being opened; it is not used" % path, "changed")
        db.execute("PRAGMA busy_timeout = %d" % int(WAIT_SECONDS * 1000))
        if create:
            with db:
                for statement in SCHEMA:
                    db.execute(statement)
    except BaseException:
        db.close()
        raise
    return db


@contextlib.contextmanager
def writer(repo):
    """The database for a write, or None when it cannot be had in time or the storage is refused; never raises."""
    db = None
    try:
        db = connect(repo, create=True)
    except (sqlite3.Error, OSError, storage.StorageError, ValueError):
        db = None
    try:
        yield db
    finally:
        if db is not None:
            db.close()


def note_agent(repo, agent, model, usage, now=None):
    """Keep one subagent run: its name (of the fixed list), its model (of the fixed form, else empty) and four whole
    numbers. `usage` is a dict with input_tokens, output_tokens, cache_read and cache_write (or the transcript's own
    cache_read_input_tokens and cache_creation_input_tokens). True when the row was written; False when the agent is not
    on the list or the file cannot be had in time."""
    match = AGENT_NAME.fullmatch(agent) if isinstance(agent, str) else None
    if not match or not isinstance(usage, dict):
        return False
    row = (int(time.time() if now is None else now), match.group(1), model_name(model), whole(usage.get("input_tokens")),
           whole(usage.get("output_tokens")), whole(usage.get("cache_read", usage.get("cache_read_input_tokens"))),
           whole(usage.get("cache_write", usage.get("cache_creation_input_tokens"))))
    with writer(repo) as db:
        if db is None:
            return False
        try:
            with db:
                db.execute("INSERT INTO agents(at, agent, model, input_tokens, output_tokens, cache_read, cache_write) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", row)
        except sqlite3.Error:
            return False
    return True


UNMEASURED = "subagents-unmeasured.json"
UNMEASURED_CAP = 512


def _unmeasured_path(repo, create=False):
    folder = storage.folder(storage.state_top(), storage.repo_folder(repo), create=create, tighten=create)
    return folder, None if folder is None else os.path.join(folder, UNMEASURED)


def note_unmeasured(repo, code, now=None):
    """Keep that a subagent's transcript did not pass the ownership checks: {"at": seconds, "code": code}, the code one of
    the fixed set of observer_sources.UNREADABLE_CODES, in the repository's state folder. True when it was kept; False for
    another code or a file that cannot be written. Never raises."""
    try:
        import observer_sources
        if code not in observer_sources.UNREADABLE_CODES or not recortex.valid_repo(repo):
            return False
        _, path = _unmeasured_path(repo, create=True)
        storage.write_file(path, json.dumps({"at": int(time.time() if now is None else now), "code": code}) + "\n")
        return True
    except (OSError, ValueError, storage.StorageError, ImportError):
        return False


def clear_unmeasured(repo):
    """Remove the note (a measured run). Never raises."""
    try:
        folder, _ = _unmeasured_path(repo)
        if folder is not None:
            storage.remove_file(folder, UNMEASURED)
    except (OSError, ValueError, storage.StorageError):
        pass


def unmeasured(repo):
    """(at, code) of the note, or None when there is none or it is not exactly what note_unmeasured writes. Read only."""
    try:
        import observer_sources
        folder, path = _unmeasured_path(repo)
        info = storage.check_path(path, "file") if path else False
        if not info or info.st_size > UNMEASURED_CAP:
            return None
        value = json.loads(storage.read_file(path) or "")
        if (isinstance(value, dict) and set(value) == {"at", "code"} and type(value["at"]) is int
                and 0 < value["at"] < 4102444800 and value["code"] in observer_sources.UNREADABLE_CODES):
            return value["at"], value["code"]
    except (OSError, ValueError, RecursionError, storage.StorageError, ImportError):
        pass
    return None


UNMEASURED_LINE = ("The plugin's subagents could not be measured on this machine: their transcripts did not pass the "
                   "ownership checks (%s). Run hooks.py --doctor.")


def note_injected(repo, event, characters, now=None):
    """Add what a hook put into a session's context: the event (of the fixed list) and its character count, to today's row.
    True when written; False for an event off the list, a count that is not a positive whole number, or a file that cannot
    be had in time."""
    if event not in INJECTED or isinstance(characters, bool) or not isinstance(characters, int) or characters < 1:
        return False
    characters = whole(characters)
    day = day_of(now)
    with writer(repo) as db:
        if db is None:
            return False
        try:
            with db:
                done = db.execute("UPDATE injected SET characters = MIN(characters + ?, ?), count = count + 1 "
                                  "WHERE day = ? AND event = ?", (characters, LIMIT, day, event))
                if done.rowcount == 0:
                    db.execute("INSERT INTO injected(day, event, characters, count) VALUES (?, ?, ?, 1)",
                               (day, event, characters))
        except sqlite3.Error:
            return False
    return True


def cleanup(repo, now=None):
    """Remove the rows older than 35 days; the number removed. 0 when there is no file or it cannot be had in time."""
    moment = time.time() if now is None else now
    cutoff = int(moment) - KEEP_DAYS * 86400
    oldest = day_of(cutoff)
    removed = 0
    with writer(repo) as db:
        if db is None:
            return 0
        try:
            with db:
                removed += db.execute("DELETE FROM agents WHERE at < ?", (cutoff,)).rowcount
                removed += db.execute("DELETE FROM injected WHERE day < ?", (oldest,)).rowcount
        except sqlite3.Error:
            return 0
    return removed


def problem(repo):
    """For --doctor, which changes nothing: "" when usage.db can be written (or there is none yet and its folder can be), else
    one fixed phrase (never a path)."""
    try:
        folder = storage.folder(storage.state_top(), storage.repo_folder(repo), create=False)
        if folder is None:
            return ""
        path = os.path.join(folder, DB_NAME)
        if not storage.check_path(path, "file"):
            return "" if os.access(folder, os.W_OK) else "its folder is not writable"
    except storage.StorageError as error:
        return "refused (%s)" % (getattr(error, "kind", "") or "unsafe")
    if not os.access(path, os.W_OK):
        return "not writable"
    try:
        db = sqlite3.connect(path, timeout=WAIT_SECONDS, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("ROLLBACK")
        finally:
            db.close()
    except sqlite3.Error:
        return "locked or not writable"
    return ""


# ---------------------------------------------------------------- the transcript of a subagent

READ_TAIL = 8 * 1024 * 1024
POLL_SECONDS = 0.1
WAIT_FOR_TRANSCRIPT = 1.5


def read_tail(path, limit=READ_TAIL):
    """The last `limit` bytes of a native transcript as lines (the first partial line is dropped), read through
    observer_sources' checked reader in pieces. Raises observer_sources.SourceError for a file that is not safe."""
    import observer_sources
    size = os.lstat(path).st_size
    start = max(0, size - limit)
    pieces, offset = [], start
    while offset < size:
        step = min(observer_sources.MAX_READ, size - offset)
        pieces.append(observer_sources._read(path, offset, step))
        offset += step
    data = b"".join(pieces)
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def sum_transcript(lines):
    """(usage, model, complete): the four integer fields of `message.usage` of the assistant records, once per
    `message.id`, and the model of the last of them. Nothing else of a record is read. `complete` is True when an
    assistant record with a stop_reason was seen (the subagent has finished writing)."""
    seen, total, model, complete = {}, {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}, "", False
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("stop_reason"):
            complete = True
        ident = message.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            continue
        found = message.get("usage")
        if not isinstance(found, dict):
            continue
        seen[ident] = True
        total["input_tokens"] += whole(found.get("input_tokens"))
        total["output_tokens"] += whole(found.get("output_tokens"))
        total["cache_read"] += whole(found.get("cache_read_input_tokens"))
        total["cache_write"] += whole(found.get("cache_creation_input_tokens"))
        model = model_name(message.get("model")) or model
    return {key: min(LIMIT, value) for key, value in total.items()}, model, complete


def measure(path, wait=WAIT_FOR_TRANSCRIPT, poll=POLL_SECONDS, clock=time.monotonic, sleep=time.sleep):
    """Sum a subagent's transcript. The transcript is not complete at the moment the hook fires, so this reads it again
    every `poll` seconds, for at most `wait` seconds, until an assistant record with a stop_reason is there. None when it
    never shows. Raises observer_sources.SourceError for a file that is not safe."""
    deadline = clock() + wait
    while True:
        try:
            lines = read_tail(path)
        except FileNotFoundError:
            lines = []
        total, model, complete = sum_transcript(lines)
        if complete:
            return total, model
        if clock() >= deadline:
            return None
        sleep(poll)


# ---------------------------------------------------------------- the report

def _query(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def agent_rows(repo, since):
    """[(agent, model, input, output, cache_read, cache_write, at)] since the time `since`."""
    try:
        db = connect(repo, create=False)
    except (sqlite3.Error, OSError, storage.StorageError):
        return []
    if db is None:
        return []
    try:
        return _query(db, "SELECT agent, model, input_tokens, output_tokens, cache_read, cache_write, at FROM agents "
                          "WHERE at >= ?", (int(since),))
    finally:
        db.close()


def injected_rows(repo, since_day):
    try:
        db = connect(repo, create=False)
    except (sqlite3.Error, OSError, storage.StorageError):
        return []
    if db is None:
        return []
    try:
        return _query(db, "SELECT day, event, characters, count FROM injected WHERE day >= ?", (since_day,))
    finally:
        db.close()


def aggregate(calls):
    """The observer's calls (observer_store.Store.usage_rows: one dict per call with `at`, tier, job, model and the token counts)
    as rows by day, tier, job and model: dicts with day, tier, job, model, calls, input_tokens, output_tokens. A call of a tier
    or job off the fixed lists, or with no time, is dropped."""
    table = {}
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict) or call.get("tier") not in TIERS or call.get("job") not in JOBS:
            continue
        at = call.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at or at < 0 or at > 10 ** 11:
            continue
        key = (day_of(at), call["tier"], call["job"], model_name(call.get("model")))
        slot = table.setdefault(key, [0, 0, 0])
        slot[0] += 1
        slot[1] += whole(call.get("input_tokens"))
        slot[2] += whole(call.get("output_tokens"))
    return [{"day": day, "tier": tier, "job": job, "model": model, "calls": slot[0], "input_tokens": slot[1],
             "output_tokens": slot[2]} for (day, tier, job, model), slot in sorted(table.items())]


def _store(repo):
    import observer_store
    return observer_store.Store(repo, create=False, readonly=True)


def store_rows(repo, days):
    """The observer's calls by day, tier, job and model (see aggregate), from its private store. [] when there is no store, or
    it is of a format that holds no such columns (a project the observer never ran in)."""
    try:
        store = _store(repo)
    except Exception:  # noqa: BLE001 - usage is read-only and never stops a page
        return []
    try:
        return aggregate(store.usage_rows(days))
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def store_today(repo):
    """{tier: {"calls": n, "tokens": n}} for the rolling 24 hours, as the caps count them (input plus output tokens); {} when
    it cannot be read."""
    try:
        store = _store(repo)
    except Exception:  # noqa: BLE001
        return {}
    try:
        return {tier: {"calls": whole(row.get("calls")), "tokens": whole(row.get("input_tokens")) + whole(row.get("output_tokens"))}
                for tier, row in store.usage_today().items() if tier in TIERS and isinstance(row, dict)}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


ACT_KINDS = ("memory", "task_opened", "steps_added", "step_marked", "task_rendered", "task_closed", "hub_rendered", "summary",
             "board", "draft_proposed", "draft_applied")   # observer_store.ACT_KINDS; a test holds the two equal


def acts_report(repo, days=7, limit=50):
    """What the observer did in the last `days` days, newest first, for the to-do view: dicts with day, kind (one of ACT_KINDS),
    issue and comment (whole numbers, 0 for none) and whether it was undone. [] when there is no store or it holds no acts."""
    try:
        store = _store(repo)
    except Exception:  # noqa: BLE001
        return []
    try:
        out = []
        for act in store.acts(days):
            at = act.get("at")
            if act.get("kind") not in ACT_KINDS or isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= at < 10 ** 11:
                continue
            out.append({"day": day_of(at), "kind": act["kind"], "issue": whole(act.get("issue")),
                        "comment": whole(act.get("comment")), "undone": act.get("undone") is True})
        return out[:limit]
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def observer_state(repo, config):
    """("on" | "paused" | "off", settings dict). The settings are this machine's for this folder; a missing key takes the
    default of the table of caps."""
    try:
        import observer_runtime
        local = observer_runtime.local_config(config)
        try:
            options = observer_runtime.settings(local)
        except ValueError:
            options = {}
        if observer_runtime.approval_valid(repo, local):
            return "on", options
        paused = getattr(observer_runtime, "paused", None)
        if paused is not None and paused(repo, local):
            return "paused", options
        return "off", options
    except Exception:  # noqa: BLE001 - a machine with unreadable approvals has the observer off, for this page
        return "off", {}


def model_of(options, tier):
    """The model a tier is set to run on (observer_runtime.model_for), "" when none is set or it cannot be told."""
    try:
        import observer_runtime
        return observer_runtime.model_for(options, tier)
    except Exception:  # noqa: BLE001
        return options.get({"sort": "sort_model", "middle": "model", "strong": "strong_model"}[tier], "") if options else ""


def period(rows, days, now):
    """{(tier, job): [calls, input, output]} for the last `days` UTC days."""
    first = day_of(now - (days - 1) * 86400)
    out = {}
    for row in rows:
        if row["day"] < first:
            continue
        key = (row["tier"], row["job"])
        slot = out.setdefault(key, [0, 0, 0])
        slot[0] += row["calls"]
        slot[1] += row["input_tokens"]
        slot[2] += row["output_tokens"]
    return out


def clean_store_rows(rows):
    """Only rows of the fixed tiers and jobs with a real day and whole numbers; everything else is dropped."""
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        tier, job, day = row.get("tier"), row.get("job"), row.get("day")
        if tier not in TIERS or job not in JOBS or not isinstance(day, str) or not DAY.fullmatch(day):
            continue
        out.append({"day": day, "tier": tier, "job": job, "model": model_name(row.get("model")),
                    "calls": whole(row.get("calls")), "input_tokens": whole(row.get("input_tokens")),
                    "output_tokens": whole(row.get("output_tokens"))})
    return out


def report(repo, config=None, now=None, rows=None, today=None):
    """Everything the page and `usage.py show` tell, as whole numbers and fixed words: no dollars, no text of the record.
    `rows` and `today` are the observer's numbers (store_rows and store_today read them when not given)."""
    moment = int(time.time() if now is None else now)
    state, options = observer_state(repo, config or {})
    rows = clean_store_rows(store_rows(repo, 30) if rows is None else rows)
    if today is None:
        today = store_today(repo)
    models = {}
    for row in rows:
        if row["model"]:
            models[row["tier"]] = row["model"]
    tiers = []
    for tier in TIERS:
        calls_key, tokens_key, calls_default, tokens_default = CAP_KEYS[tier]
        cap, budget = options.get(calls_key), options.get(tokens_key)
        used = today.get(tier) if isinstance(today, dict) and isinstance(today.get(tier), dict) else {}
        tiers.append({"tier": tier, "model": model_name(model_of(options, tier)) or models.get(tier, ""),
                      "calls": whole(used.get("calls")), "cap": whole(cap) if isinstance(cap, int) else calls_default,
                      "tokens": whole(used.get("tokens")), "budget": whole(budget) if isinstance(budget, int) else tokens_default})
    periods = {}
    for days in (7, 30):
        table = period(rows, days, moment)
        periods[str(days)] = [{"tier": tier, "job": job, "calls": slot[0], "input": slot[1], "output": slot[2]}
                              for (tier, job), slot in sorted(table.items(), key=lambda item: (TIERS.index(item[0][0]),
                                                                                              JOBS.index(item[0][1])))]
    per_day = []
    for back in range(29, -1, -1):
        day = day_of(moment - back * 86400)
        line = {"day": day}
        for tier in TIERS:
            line[tier] = sum(row["input_tokens"] + row["output_tokens"] for row in rows
                             if row["day"] == day and row["tier"] == tier)
        per_day.append(line)
    agents = {}
    for days in (7, 30):
        since = moment - days * 86400
        table = {}
        for agent, model, tokens_in, tokens_out, cache_read, cache_write, at in agent_rows(repo, since):
            if agent not in AGENTS:
                continue
            slot = table.setdefault(agent, [0, 0, 0])
            slot[0] += 1
            slot[1] += whole(tokens_in) + whole(cache_read) + whole(cache_write)
            slot[2] += whole(tokens_out)
        agents[str(days)] = [{"agent": agent, "runs": slot[0], "input": slot[1], "output": slot[2]}
                             for agent, slot in sorted(table.items())]
    largest = [{"kind": "observer", "name": "%s %s" % (tier, job), "tokens": calls_in + calls_out}
               for (tier, job), (calls, calls_in, calls_out) in period(rows, 7, moment).items()]
    largest += [{"kind": "agent", "name": row["agent"], "tokens": row["input"] + row["output"]} for row in agents["7"]]
    largest = sorted((entry for entry in largest if entry["tokens"] > 0), key=lambda entry: (-entry["tokens"], entry["name"]))[:5]
    injected = {}
    for day, event, characters, count in injected_rows(repo, day_of(moment - 6 * 86400)):
        if event in INJECTED and isinstance(day, str) and DAY.fullmatch(day):
            slot = injected.setdefault(event, [0, 0])
            slot[0] += whole(characters)
            slot[1] += whole(count)
    estimated = [{"event": event, "characters": slot[0], "count": slot[1], "tokens": slot[0] // CHARS_PER_TOKEN}
                 for event, slot in sorted(injected.items(), key=lambda item: INJECTED.index(item[0]))]
    empty = not rows and not agents["30"] and not estimated and not any(entry["calls"] for entry in tiers)
    return {"built": utc(moment).strftime("%Y-%m-%d %H:%M UTC"), "observer": state, "tiers": tiers, "periods": periods,
            "days": per_day, "largest": largest, "agents": agents, "estimated": estimated, "empty": empty}


# ---------------------------------------------------------------- the terminal

def lines(data, days=7, unmeasured_code=""):
    """The same numbers as the page, as terminal lines. `unmeasured_code`, when a subagent's transcript did not pass the
    ownership checks, adds the one fixed line that says so under the subagents heading."""
    out = ["recortex usage on this machine, as of %s. Tokens and calls, not money: under a Claude subscription these "
           "count against your usage limits, and no price applies." % data["built"]]
    if data["observer"] == "off":
        out.append("The observer is off here, so there are no background calls to count.")
    elif data["observer"] == "paused":
        out.append("The observer is paused here until you choose a level, so there are no background calls to count.")
    else:
        out.append("Today (the last 24 hours):")
        for entry in data["tiers"]:
            out.append("  %-6s %-10s %d of %d calls, %d of %d tokens" % (
                entry["tier"], entry["model"] or "(no model)", entry["calls"], entry["cap"], entry["tokens"],
                entry["budget"]))
    out.append("The last %d days, by tier and job:" % days)
    table = data["periods"][str(days)]
    if not table:
        out.append("  no background calls")
    for row in table:
        out.append("  %-6s %-9s %d calls, %d input tokens, %d output tokens" % (row["tier"], row["job"], row["calls"],
                                                                                 row["input"], row["output"]))
    out.append("The plugin's subagents, the last %d days:" % days)
    if unmeasured_code:
        out.append("  " + UNMEASURED_LINE % unmeasured_code)
    table = data["agents"][str(days)]
    if not table:
        out.append("  none measured")
    for row in table:
        out.append("  %-24s %d runs, %d input tokens, %d output tokens" % (row["agent"], row["runs"], row["input"],
                                                                         row["output"]))
    if data["largest"]:
        out.append("The largest this week:")
        for entry in data["largest"]:
            out.append("  %s: %d tokens" % (entry["name"], entry["tokens"]))
    if data["estimated"]:
        out.append("Added to your sessions' context by the hooks, estimated at 4 characters a token, the last 7 days:")
        for entry in data["estimated"]:
            out.append("  %-26s %d times, about %d tokens" % (entry["event"], entry["count"], entry["tokens"]))
    out.append("Not measured: " + NOT_MEASURED)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="print the numbers the usage view tells")
    show.add_argument("--days", type=int, choices=(7, 30), default=7)
    show.add_argument("--json", action="store_true")
    show.add_argument("--repo", help="owner/name (default: from the project file)")
    args = parser.parse_args(argv)
    config = recortex.load_config()
    repo = recortex.resolve_repo(args.repo, config)
    if args.json:
        print(json.dumps(report(repo, config), indent=1, sort_keys=True))
        return 0
    print(recortex.describe(config, repo))
    note = unmeasured(repo)
    print("\n".join(lines(report(repo, config), args.days, note[1] if note else "")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (recortex.GhError, storage.StorageError) as error:
        sys.exit("error: %s" % error)

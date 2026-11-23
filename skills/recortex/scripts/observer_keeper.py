"""The manuscript keeper: when something notable changes for a part of the paper's text, it drafts the change in the
background, has it reviewed, checks it with write.py, and leaves it waiting for the user's answer.

It never writes the paper's text. A draft that passes every check is a row in the observer store (state `waiting`); the
text changes only when the user answers "Approve" to the session's question and `observer.py drafts` runs write.py
with that answer (the one-time grant, observer_hooks). Nothing in this module runs the write of write.py, and a test
reads this file's source to keep it that way.

The worker calls `consider(...)` once after its pass. It runs only when all of these hold: the project has a plan and
a usable plan issue; this machine's writing mode for the repository is `review` or `autonomous` (off, not chosen and
unreadable draft nothing and reserve no call; in `autonomous` the keeper still asks); the level is Assistant or
Everything, the mode automatic, publication not paused; a strong model is set and its proof is valid; and something is
notable (keeper_rules), stable and not held off. Two calls on the strong tier (`text`, then a fresh `text_review` bound
to the draft's hash, no repair) and `write.py check` decide; any refusal is kept as a row with a fixed reason and the
due row's hash, so that the same material is not tried again.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time

import index
import keeper_rules
import names
import observer_runtime as runtime
import record_format
import recortex
import storage
import write_rules
import writing

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
LEVELS = ("assistant", "everything")
SEEN, NONCES, FAILED = "keeper_seen", "keeper_nonces", "keeper_failed"   # store controls
FIGURES = "keeper_figures"   # store control: {"n": planned figures and tables not in the text, "at": when counted}
NONCES_KEPT = 64
RETRY_AFTER = 3600         # seconds before a topic whose call failed for a transient reason is tried again
STEP_SECONDS = 180         # one write.py call
HEX = re.compile(r"[0-9a-f]{64}")
CHECK_LINE = re.compile(r"^not ready \(check ([0-9]+)\): ", re.M)
REVIEW_REASONS = ("supported", "unsupported", "numbers", "protected", "scope", "privacy")

IDS = {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 2 ** 53}, "maxItems": 200}   # comment numbers: about 7 * 10 ** 9 on GitHub
KEEPER_DRAFT = {"type": "object", "properties": {
    "draft": {"type": "string", "minLength": 1, "maxLength": 118000},
    "used": {"type": "object", "properties": {"parts": IDS, "results": IDS, "decisions": IDS},
             "required": ["parts", "results", "decisions"], "additionalProperties": False},
    "memory": {"type": "object", "properties": {
        "plain": {"type": "string", "maxLength": 300}, "changed": {"type": "string", "maxLength": 1200},
        "why": {"type": "string", "maxLength": 1200}},
        "required": ["plain", "changed", "why"], "additionalProperties": False}},
    "required": ["draft", "used", "memory"], "additionalProperties": False}
KEEPER_REVIEW = {"type": "object", "properties": {
    "draft_hash": {"type": "string", "pattern": "[0-9a-f]{64}"}, "accepted": {"type": "boolean"},
    "reason": {"type": "string", "enum": list(REVIEW_REASONS)}},
    "required": ["draft_hash", "accepted", "reason"], "additionalProperties": False}


# ---------------------------------------------------------------- what the keeper reads

def gather(db, config, allowed, state):
    """The plain facts keeper_rules.notable takes, from the index: the due rows with their keys, the plan parts, the
    kinds that carry a block and the told text changes."""
    items = state["found"]["items"]
    due = [dict(row, key=write_rules.due_key(row)) for row in write_rules.due_parts(db, config, allowed, state)]
    parts = {}
    for cid, found in state["parts"].items():
        backed = [items[r]["time"] for r in found["backs"] if r in items]
        parts[cid] = {"part": found["part"], "state": found["state"], "topic": found["topic"],
                      "standing": found["standing"], "text": found["text"], "time": items[cid]["time"],
                      "backed_at": min(backed, default=""), "replaces": list(found["replaces"])}
    texts = [{"issue": row["issue"], "sections": row["sections"], "time": row["time"], "links": row["links"]}
             for row in write_rules.text_memories(db, items)]
    return {"due": due, "parts": parts, "display": list(state["plan"]["display"]), "texts": texts}


def last_source(store):
    """When the newest source was captured (the observations' creation time), or None when there is none."""
    return store.db.execute("SELECT MAX(created) FROM observations").fetchone()[0]


def start_records(repo):
    """[{"issue", "nonce", "at"}]: the write.py start records this machine holds for the repository (the main session's
    and the keeper's own), which write.py keeps in the private folder write/<issue>/<hash of the base>.json. Unreadable
    ones are left out."""
    out = []
    try:
        top = storage.cache_folder(repo, storage.WRITE, create=False)
        issues = sorted(os.listdir(top)) if top else []
    except (storage.StorageError, OSError):
        return out
    for number in issues:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", number):
            continue
        try:
            folder = storage.cache_folder(repo, storage.WRITE, number, create=False)
            files = sorted(os.listdir(folder)) if folder else []
        except (storage.StorageError, OSError):
            continue
        for name in files:
            if not re.fullmatch(r"[0-9a-f]{64}\.json", name):
                continue
            try:
                path = os.path.join(folder, name)
                info = storage.check_path(path, "file")
                found = json.loads(storage.read_file(path) or "")
                if info and isinstance(found, dict) and found.get("issue") == int(number) \
                        and isinstance(found.get("nonce"), str) and re.fullmatch(r"[0-9a-f]{32}", found["nonce"]):
                    out.append({"issue": int(number), "nonce": found["nonce"], "at": info.st_mtime})
            except (storage.StorageError, OSError, ValueError, RecursionError):
                continue
    return out


def _write(args, project_root, timeout=STEP_SECONDS):
    """Run write.py with `args` in the project folder: (return code, stdout). The keeper only starts and checks."""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_root
    try:
        ran = subprocess.run([sys.executable, os.path.join(SCRIPTS, "write.py")] + args, capture_output=True,
                             text=True, cwd=project_root, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return ran.returncode, ran.stdout


def _read(folder, name):
    path = os.path.join(folder, name)
    return recortex.read_text(path) if os.path.isfile(path) else None


# ---------------------------------------------------------------- the draft

CUT = ("timeout", "interrupted")   # the codes of a call whose time ran out or that the worker's alarm cut


def _used(result, options):
    """The usage to record for a strong call: what it reported, or when it was cut short and reported none, its whole room
    (runtime.worst_case_usage), so that the day's budget sees what it may have cost."""
    if result.get("code") in CUT and not result.get("usage"):
        return runtime.worst_case_usage(options, "strong")
    return result.get("usage")


def run_draft(repo, config, store, options, invoke, entry, state, now, project_root, holds=None):
    """Draft one topic's change and leave it waiting or refused. `entry` is one of keeper_rules.notable's topics.
    Returns {"code": "drafted", "draft": short id} for a draft that waits, {"code": "refused", "reason": ..., "draft":
    short id} for one the checks refused, or a code without a draft ("start_failed", "budget", "call_failed"): nothing
    is kept for those, and the next pass may try again. `holds` is a callable that renews the worker's host lease (True
    while it holds): asked before the proposal is kept, since the two strong calls may have used the lease up; when it says
    no, the code is "other-host" and nothing is kept."""
    topic = entry["topic"]
    token = secrets.token_hex(8)
    folder = storage.cache_folder(repo, "keeper", token)
    code, _ = _write(["start", "--repo", repo, "--issue", str(topic), "--out", folder], project_root)
    if code != 0 or _read(folder, "base.md") is None:
        _failed(store, topic, entry["key"], now)
        return {"code": "start_failed"}
    try:
        started = json.loads(_read(folder, "start.json") or "{}")
        material = json.loads(_read(folder, "material.json") or "{}")
        protected = json.loads(_read(folder, "protected.json") or "{}")["paragraphs"]
        base_hash = material["base"]
        nonce = started["nonce"]
        if not HEX.fullmatch(base_hash) or not isinstance(nonce, str):
            raise ValueError("start")
    except (ValueError, KeyError, TypeError, RecursionError):
        _failed(store, topic, entry["key"], now)
        return {"code": "start_failed"}
    own = store.get_control(NONCES, [])
    store.set_control(NONCES, ((own if isinstance(own, list) else []) + [nonce])[-NONCES_KEPT:])
    headings = list(material.get("headings") or entry["headings"])[:12]

    def refuse(reason):
        ident = store.add_draft(topic, headings, folder, base_hash, due_hash=entry["key"], state="refused",
                                reason=reason)
        return {"code": "refused", "reason": reason, "draft": ident}

    base, material_md, last = _read(folder, "base.md"), _read(folder, "material.md"), _read(folder, "last.md")
    plan_parts = index.planned_for(state, {topic})
    payload = {"issue": topic, "headings": headings, "base": base, "material": material_md, "last": last,
               "protected": protected, "parts": plan_parts}
    window = options["window_hours"] * 3600
    reservation = store.reserve_invocation("text", options["max_strong"], window, tokens=options["strong_tokens"])
    if reservation is None:
        return {"code": "budget"}
    result = invoke(payload, KEEPER_DRAFT, "text", options)
    valid = result.get("ok") is True and runtime.validate(result.get("structured"), KEEPER_DRAFT)
    store.complete_invocation(reservation, valid, runtime.candidate_hash(result["structured"]) if valid else "",
                              usage=_used(result, options), model=result.get("model") or "")
    if not valid:
        if result.get("code") in ("input-limit", "invalid-output"):
            return refuse(result["code"].replace("-", "_"))
        _failed(store, topic, entry["key"], now)   # a transient failure: the proof, the runtime, a timeout
        return {"code": "call_failed"}
    draft = result["structured"]
    try:
        recortex.write_working_files(folder, {"draft.md": draft["draft"], "used.json": json.dumps(draft["used"]),
                                              "memory.json": json.dumps(draft["memory"])})
    except SystemExit:
        return refuse("draft_files")
    digest = hashlib.sha256(draft["draft"].encode("utf-8")).hexdigest()
    review = {"issue": topic, "headings": headings, "base": base, "material": material_md, "protected": protected,
              "draft": draft["draft"], "used": draft["used"], "memory": draft["memory"], "draft_hash": digest}
    reservation = store.reserve_invocation("text_review", options["max_strong"], window, tokens=options["strong_tokens"])
    if reservation is None:
        return {"code": "budget"}   # the draft is on disk; it is made again from the same material next time
    answer = invoke(review, KEEPER_REVIEW, "text_review", options)
    judged = answer.get("ok") is True and runtime.validate(answer.get("structured"), KEEPER_REVIEW)
    store.complete_invocation(reservation, judged, runtime.candidate_hash(answer["structured"]) if judged else "",
                              usage=_used(answer, options), model=answer.get("model") or "")
    if not judged:
        if answer.get("code") in ("input-limit", "invalid-output"):
            return refuse("review_" + answer["code"].replace("-", "_"))
        _failed(store, topic, entry["key"], now)
        return {"code": "call_failed"}
    verdict = answer["structured"]
    if verdict["draft_hash"] != digest:
        return refuse("review_hash")
    if verdict["accepted"] is not True or verdict["reason"] != "supported":
        return refuse("rejected_" + (verdict["reason"] if verdict["reason"] != "supported" else "unsupported"))
    code, output = _write(["check", "--repo", repo, "--dir", folder], project_root)
    if code != 0:
        found = CHECK_LINE.search(output)
        return refuse("check_" + found.group(1) if found else "check_failed")
    # The bytes the user will be asked about are the ones written above, from the model's answer: their hash is kept, and
    # observer.py drafts shows and writes only a copy whose files still have it (observer_drafts.approved_copy).
    content = keeper_rules.content_hash({"draft.md": draft["draft"].encode("utf-8"),
                                         "used.json": json.dumps(draft["used"]).encode("utf-8"),
                                         "memory.json": json.dumps(draft["memory"]).encode("utf-8")})
    if holds is not None and not holds():
        return {"code": "other-host"}   # another host's worker may be on this queue now: keep no proposal from this one
    ident = store.add_draft(topic, headings, folder, base_hash, due_hash=entry["key"], state="waiting")
    store.set_control(keeper_rules.CONTENTS,
                      keeper_rules.remember(store.get_control(keeper_rules.CONTENTS, []), ident, content))
    store.log_act("draft_proposed", issue=topic)   # a log that cannot take it is counted, never a failed draft
    return {"code": "drafted", "draft": ident}


def _failed(store, topic, key, now):
    """Remember that a draft of this due row could not be made for a reason that may pass (a failed start, a failed
    call): the topic is tried again after RETRY_AFTER seconds or when its due row changes."""
    failed = store.get_control(FAILED, {})
    failed = failed if isinstance(failed, dict) else {}
    failed[str(topic)] = {"key": key, "at": now}
    store.set_control(FAILED, failed)


def _held_after_failure(store, topic, key, now):
    failed = store.get_control(FAILED, {})
    row = failed.get(str(topic)) if isinstance(failed, dict) else None
    return (isinstance(row, dict) and row.get("key") == key and type(row.get("at")) in (int, float)
            and now - row["at"] < RETRY_AFTER)


# ---------------------------------------------------------------- the entry point

def consider(repo, config, store, options, invoke_fn=None, now=None, session_started=False, project_root=None, clock=None,
             holds=None):
    """Consider the paper's text once, after the worker's pass. `config` is the effective configuration, `options` the
    observer's settings (runtime.settings(config)), `store` the open observer store (the worker holds its lock) and
    `invoke_fn` the model call (default runtime.invoke); `session_started` says a session just started (the keeper
    may then draft without waiting for ten quiet minutes). Returns {"code": ...} and, when a draft was made, {"draft":
    its short id}; it never raises. Codes: mode, level, paused, no_strong, strong_gate, writing, no_plan, not_trusted,
    no_index, nothing (nothing notable), one of keeper_rules.REASONS for a topic that waits or is held off, and the
    codes of run_draft. `clock` is the worker's lifetime (observer_worker.Clock): with less left than a draft and its review
    may take (two strong calls and a minute), the code is "time" and the next pass looks again. `holds` renews the worker's
    host lease (see run_draft); a draft that finds it gone is not kept ("other-host")."""
    try:
        return _consider(repo, config, store, options, invoke_fn or runtime.invoke,
                         time.time() if now is None else now, session_started, project_root, clock, holds)
    except Exception:  # noqa: BLE001 - the keeper must never stop the worker; the next pass looks again
        return {"code": "error"}


def _consider(repo, config, store, options, invoke, now, session_started, project_root, clock=None, holds=None):
    if names.env(runtime.GUARD) == "1" or not options.get("enabled") or options.get("mode") != "automatic":
        return {"code": "mode"}   # shadow mode proposes nothing either
    if options.get("level") not in LEVELS:
        return {"code": "level"}
    if store.get_control("publication_paused", False):
        return {"code": "paused"}
    if not options.get("strong_model"):
        return {"code": "no_strong"}
    options = runtime.with_gates(options, store)   # the stored proofs beside the settings, as invoke reads them
    if not runtime.tier_state(options, store)["strong"]["gate"]:
        return {"code": "strong_gate"}
    found = writing.read(repo)
    if found["state"] != "chosen" or found["mode"] not in ("review", "autonomous"):
        return {"code": "writing"}   # off, not chosen or unreadable: nothing is drafted and no call is reserved
    plan = recortex.plan_profile(recortex.shipped_profiles(config.get("profiles")))
    if plan is None:
        return {"code": "no_plan"}
    allowed = recortex.allowed_users(repo)
    if not allowed:
        return {"code": "not_trusted"}
    root = project_root or recortex.project_root()
    db = index.connect(repo, create=False)
    if db is None:
        return {"code": "no_index"}
    try:
        if not index.mind_ready(db):
            return {"code": "no_index"}
        state = index.planned(db, config, allowed, plan)
        if state is None:
            return {"code": "no_plan"}
        report = keeper_rules.notable(gather(db, config, allowed, state))
        # N3's "line only": the count of planned figures and tables that exist and are in no text, for the session-start
        # line (the hook reads this integer and nothing else; it computes nothing from the plan itself).
        store.set_control(FIGURES, {"n": report["figures"], "at": now})
        topics = report["topics"]
        seen = keeper_rules.settle(store.get_control(SEEN, {}), {row["topic"]: row["key"] for row in topics}, now)
        store.set_control(SEEN, seen)
        if not topics:
            return {"code": "nothing"}
        drafts = store.drafts()
        own = store.get_control(NONCES, [])
        records = start_records(repo)
        why = "nothing"
        for entry in topics:
            topic, key = entry["topic"], entry["key"]
            why = keeper_rules.held_off(topic, key, drafts, now, options["max_proposals"])
            if not why and _held_after_failure(store, topic, key, now):
                why = "unstable"
            if not why and not keeper_rules.stable(seen, topic, key, now):
                why = "unstable"
            if not why and not keeper_rules.quiet(last_source(store), now, session_started):
                why = "quiet"
            if not why and keeper_rules.foreign_start(records, topic, own if isinstance(own, list) else [], now):
                why = "busy"
            if why:
                continue
            if clock is not None and clock.remaining() < 2 * runtime.call_timeout(options, "strong") + 60:
                return {"code": "time"}   # a strong call cut by the worker's alarm costs the budget and makes no draft
            outcome = run_draft(repo, config, store, options, invoke, entry, state, now, root, holds)
            return dict(outcome, topic=topic)
        return {"code": why}
    finally:
        db.close()

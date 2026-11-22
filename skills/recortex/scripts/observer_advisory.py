"""Optional task-time pointers to an explicit decision and its recorded failure.

No model, network, permission decision or record mutation. Shadow and delivered
receipts are separate, bounded local controls; neither stores the user's prompt.
"""

import hashlib
import json
import re
import time

import observer_runtime

MODES = ("off", "shadow", "deliver")
KEPT = 100
MAX_AGE = 86400  # Advice needs a recent complete snapshot; older context remains available for explicit reading.
MAX_NOTE = 1000
SHADOW = "advisory_shadow"
DELIVERED = "advisory_delivered"

ID = re.compile(r"[0-9a-f]{64}")
BRANCH = re.compile(r"branch-[0-9a-f]{24}")
SOURCE_KINDS = {"decision_failure": ("decision", "failure"), "map_question": ("question",),
                "decision_change": ("decision", ("decision", "correction")),
                "possible_change": ("decision", ("decision", "correction"))}
ROW_FIELDS = {"id", "at", "scope_kind", "scope_issue", "reason", "sources", "muted"}


def _digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


def _folder_control(root):
    # The queue is per repository. These private preferences are per approved checkout.
    return "advisory_history_" + _digest(observer_runtime.folder_key(root))[:40]


def _positive(value):
    return type(value) is int and 0 < value < 2 ** 63


def _timestamp(value):
    return type(value) in (float, int) and 0 <= value < 2 ** 53


def _rows(store, control):
    """Only fixed reason codes, source IDs and finite timestamps may leave private storage."""
    rows = store.get_control(control, {})
    if not isinstance(rows, dict) or len(rows) > KEPT * 2:
        raise ValueError("invalid_advisory_history")
    for ident, row in rows.items():
        if not (isinstance(ident, str) and ID.fullmatch(ident) and isinstance(row, dict)
                and set(row) in (ROW_FIELDS, ROW_FIELDS | {"branch"})
                and ident == row["id"] and _timestamp(row["at"]) and type(row["muted"]) is bool
                and row["scope_kind"] in ("task", "topic") and _positive(row["scope_issue"])
                and ("branch" not in row or isinstance(row["branch"], str) and BRANCH.fullmatch(row["branch"]))):
            raise ValueError("invalid_advisory_history")
        kinds = SOURCE_KINDS.get(row["reason"]) if isinstance(row["reason"], str) else None
        refs = row["sources"]
        if not (kinds and isinstance(refs, list) and len(refs) == len(kinds)
                and all(isinstance(ref, dict) and set(ref) == {"kind", "issue", "comment"} and (ref["kind"] in kind if isinstance(kind, tuple) else ref["kind"] == kind)
                        and _positive(ref["comment"]) and _positive(ref["issue"]) for ref, kind in zip(refs, kinds))):
            raise ValueError("invalid_advisory_history")
    return rows


def history(store, root=None):
    """Most recent distinct issued reminders in this project folder, including each explicitly muted reminder."""
    return sorted(_rows(store, _folder_control(root)).values(), key=lambda row: (row["at"], row["id"]), reverse=True)


def _trim(rows, keep):
    # Keep every explicit mute even while new reminders rotate through the visible history.
    unmuted = sorted((row for row in rows.values() if not row["muted"]), key=lambda row: (row["at"], row["id"]))
    if len(unmuted) > KEPT:
        oldest = next(row for row in unmuted if row["id"] != keep)
        del rows[oldest["id"]]


def feedback(store, ident, muted, root=None):
    """Set the preference for this existing evidence revision and scope. Unknown IDs cannot create a preference."""
    if not isinstance(ident, str) or not ID.fullmatch(ident) or type(muted) is not bool:
        raise ValueError("invalid_advisory_feedback")
    control = _folder_control(root)
    with store.edit_controls() as write:
        rows = _rows(store, control)
        if ident not in rows:
            raise KeyError(ident)
        row = rows[ident]
        if row["muted"] != muted:
            if muted and sum(other["muted"] for other in rows.values()) >= KEPT:
                raise ValueError("mute_limit")
            row["muted"] = muted
            _trim(rows, ident)
            write(control, rows)
        return row


# The fixed wording of the pointer. task_reminder.form builds from these, so that the two cannot drift apart.
POINTER_HEAD = "Decision change. "
WHY_SUGGESTED = "Possible connection by shared wording. Ask whether this work relies on the old decision."
WHY_DIRECT = "The current work explicitly refers to the old decision. Check whether it still applies."
POINTER_TAIL = " before advising. In chat, name the two choices and the affected words and cite their sources."


def change_pointer(db, repo, allowed, packet, query):
    """One open target in fresh trusted material that the user specifically named."""
    import decision_impact
    import observer_context

    fresh = packet["freshness"]
    age = fresh["age_seconds"]
    if not fresh["sync_complete"] or type(age) not in (int, float) or not 0 <= age <= MAX_AGE:
        return None
    ignored = observer_context.ADVISORY_GENERIC | {"read", "work", "update", "review", "standing", "open",
                                                   "decision", "step", "section", "source", "branch", "issue"}

    def match_terms(text):
        return observer_context.task_terms(re.sub(r"https?://\S+", " ", text))

    scoped = {int(n) for n in re.findall(r"(?:\B#|\bissue\s+#?)([1-9][0-9]{0,17})\b", query, re.I)}
    scoped.update(int(n) for n in re.findall(re.escape("https://github.com/" + repo + "/issues/") +
                                             r"([1-9][0-9]{0,17})\b", query, re.I))
    terms = match_terms(query) - observer_context.task_terms(" ".join(ignored))
    if len(terms) < 2 or not next(iter(db.execute("SELECT 1 FROM links WHERE kind='replaces' LIMIT 1")), None):
        return None
    matches = []
    projection = decision_impact.from_index(db, repo, allowed, suggestions=True)
    if projection["partial"]:
        return None
    for group in projection["groups"]:
        if group["conflict"] or len(group["successors"]) != 1:
            continue
        for item in group["items"]:
            if item.get("review") or (scoped and item["issue"] not in scoped):
                continue
            overlap = terms & match_terms(item["label"] + " " + item["quote"])
            if len(overlap) >= 2:
                matches.append(((len(overlap), item["relation"] == "direct"), group, item))
    matches.sort(key=lambda row: row[0], reverse=True)
    if not matches or (len(matches) > 1 and matches[0][0] == matches[1][0]):
        return None
    _, group, item = matches[0]
    old, new = group["old"], group["successors"][0]
    suggested = item["relation"] == "suggested"
    why = WHY_SUGGESTED if suggested else WHY_DIRECT
    return {"reason": "possible_change" if suggested else "decision_change", "issue": old["issue"], "decision": old["id"],
            "successor_kind": new["kind"], "successor_issue": new["issue"], "successor": new["id"],
            "target_issue": item["issue"], "fingerprint": _digest([group["id"], item["id"], item["revision"]]),
            "text": (POINTER_HEAD + "%s Earlier: %s Current: %s Work: %s. Read guidance.py context %s %s" + POINTER_TAIL)
                    % (why, old["url"], new["url"], item["url"], group["id"], item["id"])}


def deliver(store, packet, query, session, agent="", mode="off", now=None, root=None):
    """Issue a pointer once in a session. A mute follows the evidence revision in this task or topic across sessions."""
    if mode not in ("shadow", "deliver") or not isinstance(query, str) or not query.strip():
        return ""
    fresh = packet.get("freshness", {})
    age = fresh.get("age_seconds")
    if not fresh.get("sync_complete") or type(age) not in (int, float) or not 0 <= age <= MAX_AGE:
        return ""
    items = packet.get("advisories", [])
    if not items:
        return ""
    item = items[0]
    if not (isinstance(item, dict) and isinstance(item.get("text"), str) and len(item["text"]) <= MAX_NOTE
            and isinstance(item.get("fingerprint"), str) and ID.fullmatch(item["fingerprint"]) and _positive(item.get("issue"))):
        return ""
    scope = ["task", packet["scope"]] if packet.get("scope") is not None else ["topic", item["issue"]]
    reason = item.get("reason", "map_question" if "question" in item else "decision_failure")
    if reason not in SOURCE_KINDS:
        return ""
    if not _positive(scope[1]):
        return ""
    if reason in ("decision_change", "possible_change"):
        if item.get("successor_kind") not in ("decision", "correction"):
            return ""
        sources = [{"kind": "decision", "issue": item["issue"], "comment": item.get("decision")},
                   {"kind": item["successor_kind"], "issue": item.get("successor_issue"), "comment": item.get("successor")}]
        scope = ["topic", item.get("target_issue")]
    else:
        sources = [{"kind": kind, "issue": item.get("failure_issue") if kind == "failure" else item["issue"],
                    "comment": item.get(kind)} for kind in SOURCE_KINDS[reason]]
    if not _positive(scope[1]) or not all(_positive(ref["issue"]) and _positive(ref["comment"]) for ref in sources):
        return ""
    branch = item.get("branch")
    if branch is not None and not (isinstance(branch, str) and BRANCH.fullmatch(branch)):
        return ""
    folder = _folder_control(root)
    ident = _digest([folder, scope, branch, item["fingerprint"]])
    key = _digest([session, agent, ident])
    # Pre-feedback builds stored this hash with a numeric timestamp, without the folder or branch.
    legacy = _digest([session, agent, scope, item["fingerprint"]])
    control = SHADOW if mode == "shadow" else DELIVERED
    at = time.time() if now is None else now
    if not _timestamp(at):
        raise ValueError("invalid_advisory_time")
    with store.edit_controls() as write:
        rows = _rows(store, folder)
        if ident in rows and rows[ident]["muted"]:
            return ""
        seen = store.get_control(control, {})
        if not isinstance(seen, dict):
            raise ValueError("invalid_advisory_receipts")
        if key in seen or legacy in seen:
            return ""
        seen[key] = at
        write(control, dict(sorted(seen.items(), key=lambda pair: pair[1])[-KEPT:]))
        if mode == "shadow":
            return ""
        row = {"id": ident, "reason": reason, "scope_kind": scope[0], "scope_issue": scope[1],
               "at": at, "sources": sources, "muted": False}
        if branch:
            row["branch"] = branch
        rows[ident] = row
        _trim(rows, ident)
        write(folder, rows)
    return item["text"]


def status(store):
    """Counts and last times only. Receipts record hook issuance, not that a user saw or benefited from advice."""
    out = {}
    for mode, key in (("shadow", SHADOW), ("delivered", DELIVERED)):
        found = store.get_control(key, {})
        times = [at for at in found.values() if type(at) in (int, float)] if isinstance(found, dict) else []
        out[mode] = {"retained_receipts": len(times), "last_at": max(times, default=None)}
    return out

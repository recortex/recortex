"""The second way to approve a change of the text in review mode: a pull request on GitHub (references/writing.md).

`write.py propose --dir DIR` puts the checked draft on a branch of its own, opens a pull request into the manuscript
branch and posts the `proposal` comment on the topic. `write.py approvals [--apply]` lists the proposals and, with --apply,
writes the ones that were approved. An approval is never text in a comment that someone could edit: it is a merge by an
allowed account of content whose hash an unedited comment of an allowed account names. `--apply` writes a proposal's text
only when every row of this table holds, each read live from GitHub at that moment and none from the index:

  ours            the comment's author is an allowed account, and it is the newest proposal on that topic
  not edited      the comment was never edited (its edit history is empty); an edited proposal is void for good
  approved        the pull request is merged into the manuscript branch and `merged_by` is an allowed login
  what we sent    the topic's file at the merge commit has the sha256 of the proposal's Text
  not moved       the live body has the sha256 of the proposal's Base (a body that already is the proposal's text, or a text
                  change made after the proposal that records it as written, means the proposal was applied: it is done
                  and nothing more is said)
  still fit       write.py check passes again on that text against the live body, and what it rests on still stands
  this machine    the mode here is review or autonomous and the repository is trusted

Python 3 standard library only. The work of the write itself is write.apply_command's, so a proposal is written in the same
order, with the same checks and the same state; only its Approved field differs ("By <login> on GitHub, by merging pull
request <n>.").
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import recortex  # noqa: E402
import record_format  # noqa: E402
import record_store  # noqa: E402
import storage  # noqa: E402
import versions  # noqa: E402
import write  # noqa: E402
import write_rules  # noqa: E402

SAY = write.say
BRANCH_NAME = re.compile(r"recortex-proposal-([1-9][0-9]{0,9})-([0-9a-f]{8})")
CLOSED_COMMENT = ("recortex: the text of this topic changed after this change was proposed, so the proposal is no longer "
                  "applied. The change will be drafted again from the live text.")
LINES = {
    "not-ours": "approvals: the proposal comment %d is not by an allowed account or is not the newest proposal on #%d; "
                "nothing is written.",
    "edited": "approvals: the proposal comment %d was edited, so it is void; nothing is written, and the session drafts the "
              "change anew.",
    "waiting": "approvals: the pull request #%d is open; the change waits for the author to merge or close it.",
    "closed": "approvals: the pull request #%d was closed without merging, so the proposal is dropped.",
    "stranger": "approvals: the pull request #%d was merged by an account that is not allowed; nothing is written.",
    "other": "approvals: the pull request #%d was merged with other text than proposed; show the author the difference and ask "
             "whether to use their version; nothing is written on its own.",
    "moved": "approvals: the text of #%d changed after the proposal was made; the proposal is not applied.",
    "unfit": "approvals: the proposed text no longer passes the checks (%s); nothing is written.",
    "elsewhere": "approvals: the proposal of #%d was made on another machine; apply it there.",
    "gone": "approvals: the pull request #%d cannot be read; nothing is written.",
    "waits": "approvals: the file of the topic of pull request #%d was changed on the branch and the text was not; the proposal "
             "waits until the author answers (versions.py status).",
}


def live_comment(repo, cid):
    """{"login", "node"} of a comment read live, or None."""
    try:
        data = record_store.for_repo(repo).read_entry(cid, timeout=60)
    except (recortex.GhError, ValueError):
        return None
    login = (data.get("user") or {}).get("login") if isinstance(data, dict) else None
    node = data.get("node_id") if isinstance(data, dict) else None
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(login, str) or not isinstance(node, str) or not index.COMMENT_NODE.fullmatch(node) or not isinstance(body, str):
        return None
    return {"login": login, "node": node, "body": body, "created": str(data.get("created_at") or "")}


def never_edited(repo, cid, live):
    """True when the comment's edit history, read live, holds nothing but (at most) the entry of its creation."""
    try:
        raw = record_store.for_repo(repo).histories({cid: (live["node"], None)}, timeout=60)
        node = (json.loads(raw).get("data") or {}).get("c%d" % cid)
    except (recortex.GhError, ValueError):
        return False
    if not isinstance(node, dict) or str(node.get("fullDatabaseId")) != str(cid):
        return False
    edits = node.get("userContentEdits") or {}
    entries = edits.get("nodes") if isinstance(edits, dict) else None
    if not isinstance(entries, list) or (edits.get("pageInfo") or {}).get("hasNextPage"):
        return False
    skipped = False
    for entry in entries:
        at = (entry or {}).get("editedAt") or ""
        editor = (((entry or {}).get("editor") or {}).get("login") or "").lower()
        if not skipped and at[:16] == live["created"][:16] and editor == live["login"].lower():
            skipped = True   # the entry for the creation of the comment: the author's own, in the minute of the post
            continue
        return False
    return True


def pull_of(repo, number):
    try:
        data = versions.api(repo, "GET", "pulls/%d" % number)
    except versions.GitError:
        return None
    if not isinstance(data, dict) or type(data.get("number")) is not int:
        return None
    merged_by = (data.get("merged_by") or {}).get("login") if isinstance(data.get("merged_by"), dict) else None
    return {"state": data.get("state"), "merged": data.get("merged") is True, "merged_by": merged_by if isinstance(merged_by, str)
            else None, "commit": data.get("merge_commit_sha") if isinstance(data.get("merge_commit_sha"), str) else None,
            "head": ((data.get("head") or {}).get("ref") if isinstance(data.get("head"), dict) else None),
            "base": ((data.get("base") or {}).get("ref") if isinstance(data.get("base"), dict) else None)}


# ---------------------------------------------------------------- propose

def topic_file_differs(config, repo, hub, allowed, issue, merged=None):
    """True when the file of a topic on the branch was changed on its own (or both changed): until the author answers, the
    proposals for that topic wait (references/versions.md). `merged` is the SHA-256 of the text a merged proposal put in the
    file: a file that holds exactly that text is the proposal's own merge, not a change somebody made on the side."""
    db = write.open_index(repo)
    try:
        try:
            head, _, found = versions.compare(repo, db, config, hub, allowed)
        except versions.GitError:
            return False
        if found.get(issue) not in ("file changed", "both changed"):
            return False
        if merged:
            try:
                text = versions.file_at(repo, head, "topics/%d.md" % issue)
            except versions.GitError:
                return False
            if text is not None and record_format.body_sha(text) == merged:
                return False
        return True
    finally:
        db.close()


def proposal_text(heading, changed, why, pull, base, text, moment, config=None):
    return "\n".join([
        "<!-- ghim:event type=proposal at=%s -->" % recortex.marker_time(moment, config),
        "### Proposal: %s - %s" % (versions.stamp(moment, config), heading), "",
        "**Section:** %s" % heading, "", "**What would change:** %s" % write.one_line(changed), "",
        "**Why:** %s" % write.one_line(why), "", "**Pull request:** #%d" % pull, "",
        "**Base:** sha256 `%s`" % base, "", "**Text:** sha256 `%s`" % text, ""])


def propose_command(args):
    config, repo, hub, plan, allowed, mode = write.load(args)
    if mode != "review":
        sys.exit("write: a proposal on GitHub is for review mode; in autonomous mode the text is written without asking")
    files = write.read_dir(args.dir)
    write.fresh(repo, hub)
    db = write.open_index(repo)
    try:
        state = index.planned(db, config, allowed, plan)
        issue, record = write.trusted_inputs(repo, db, config, allowed, plan, state, files)
        if not index.body_told(db, issue, allowed):
            sys.exit("write: " + write.UNTOLD)
        if files.get("restore") is not None:
            sys.exit("write: a restore is always asked in the chat, so it has no proposal on GitHub")
        if not write.stands(state, state["found"]["items"] if state else {}, files["used.json"]):
            sys.exit("write: what this text rests on changed while it waited; draft it again.")
        problems = write.check_files(db, config, plan, state, repo, files)
    finally:
        db.close()
    if problems:
        for number, line in problems:
            SAY("not proposed (check %d): %s" % (number, line))
        return 1
    headings = [h for h in write.changed_paragraphs(files["base.md"], files["draft.md"])
                if h.lower() != recortex.SOURCES_HEADING.lower()]
    if not headings:
        sys.exit("write: nothing to propose")
    base, text = record_format.body_sha(files["base.md"]), record_format.body_sha(files["draft.md"])
    if not versions.kept_here(repo) or not versions.can_push(repo):
        sys.exit("write: the proposal needs the branch of versions and a sign-in that may write to it; approval on GitHub is not "
                 "available here, so ask in the chat.")
    try:
        head = versions.head_of(repo)
    except versions.GitError:
        head = None
    if head is None:
        sys.exit("write: the branch of versions does not exist yet; approval on GitHub is not available here, so ask in the chat.")
    if topic_file_differs(config, repo, hub, allowed, issue):
        sys.exit("write: the file of #%d was changed on the branch and the text was not; ask the author whether to take the change "
                 "or put the file back (versions.py status), then propose again." % issue)
    if record.get("proposal", {}).get("text") == text and record["proposal"].get("pull"):
        SAY("propose: pull request #%d already holds this change" % record["proposal"]["pull"])
        return 0
    branch = "recortex-proposal-%d-%s" % (issue, text[:8])
    if getattr(args, "dry_run", False):
        SAY("would run: a commit of topics/%d.md on the new branch %s, cut from the head of %s" % (issue, branch, versions.BRANCH))
        SAY("would run: " + recortex.show_command(["api", "-X", "POST", "repos/%s/pulls" % repo, "--input", "-"],
                                                  "one pull request into %s" % versions.BRANCH))
        SAY("would run: " + recortex.show_command(["issue", "comment", str(issue), "--repo", repo, "--body-file", "-"],
                                                  "the proposal comment"))
        SAY("dry run: every check passed; nothing was written")
        return 0
    try:
        commit = versions.post_commit(repo, {"topics/%d.md" % issue: record_format.body_bytes(files["draft.md"]).decode("utf-8")},
                                      "Proposed text: %s of topic %d" % (", ".join(headings)[:60], issue), head, ref="heads/" + branch)
        opened = versions.api(repo, "POST", "pulls", {
            "title": "recortex proposes a change to %s of topic %d" % (", ".join(headings)[:80], issue),
            "head": branch, "base": versions.BRANCH,
            "body": "recortex proposes a change to the text of topic #%d. Read the difference, then merge to approve it or close "
                    "to decline it. Nothing is written to the topic until it is merged." % issue})
        pull = opened["number"]
    except versions.GitError:
        sys.exit("write: GitHub refused the pull request; approval on GitHub is not available here, so ask in the chat.")
    except (KeyError, TypeError):
        sys.exit("write: GitHub did not give the pull request a number; ask in the chat.")
    moment = versions.utc_now()
    memory = files["memory.json"]
    cid = versions.post_comment(repo, issue, "proposal", proposal_text(
        ", ".join(headings), memory.get("changed", ""), memory.get("why", ""), pull, base, text, moment, config),
        versions.scratch_folder(repo))
    if cid is None:
        SAY("write: the pull request #%d is open and its proposal comment was not posted; close the pull request and try again" % pull)
        return 1
    record["proposal"] = {"pull": pull, "comment": cid, "branch": branch, "text": text, "headings": headings,
                          "used": files["used.json"], "memory": memory, "head": commit}
    write.write_record(repo, record)
    SAY("propose: pull request #%d holds the change; comment %d names it. The author reads the difference on GitHub and merges "
        "or closes." % (pull, cid))
    return 0


# ---------------------------------------------------------------- approvals

def newest_proposals(db, allowed):
    """[(comment id, issue, author)] of the newest proposal comment of each topic written by an allowed account. A proposal
    comment of any other account never counts, newer or not: anyone who can comment on a public repository could otherwise
    block the author's approval with one comment (the brief counts only allowed accounts' proposals too)."""
    rows = db.execute("SELECT comment, issue, author, at, created_at FROM events WHERE type = 'proposal' "
                      "ORDER BY issue, created_at, comment").fetchall()
    newest = {}
    for row in rows:
        if (row["author"] or "").lower() in allowed:
            newest[row["issue"]] = row
    return [(row["comment"], row["issue"], (row["author"] or "").lower()) for row in newest.values()]


def applied(db, state, allowed, issue, base, text, cid):
    """True when a told text change of the topic records this proposal as written: made after the proposal comment `cid`, with
    its body going from the proposal's Base to the proposal's Text (what write.apply_command records). The body may have
    moved on since. Both times are the machine's own clock, to the minute, so a skew against GitHub does not matter."""
    posted = db.execute("SELECT at, created_at FROM events WHERE comment = ?", (cid,)).fetchone()
    if posted is None:
        return False
    since = record_format.instant(posted["at"], posted["created_at"])
    items = state["found"]["items"] if state else index.memories(db, allowed)["items"]
    return any(row["issue"] == issue and row["before"] == base and row["after"] == text and row["time"] >= since
               for row in write_rules.text_memories(db, items))


def evaluate(config, repo, hub, plan, allowed, db, state, cid, issue, author):
    """(status key, detail) for one proposal; status "ready" carries the folder facts in the detail, and "done" says that the
    proposal was applied already (approvals says nothing of it and closes nothing)."""
    if author not in allowed:
        return "not-ours", cid
    live = live_comment(repo, cid)
    if live is None or live["login"].lower() not in allowed:
        return "not-ours", cid
    if not never_edited(repo, cid, live):
        return "edited", cid
    body = live["body"]
    fields = dict(record_format.fields(body))
    pull = re.fullmatch(r"#([1-9][0-9]{0,8})", (fields.get("Pull request") or "").strip())
    base = re.fullmatch(r"sha256 `([0-9a-f]{64})`", (fields.get("Base") or "").strip())
    text = re.fullmatch(r"sha256 `([0-9a-f]{64})`", (fields.get("Text") or "").strip())
    if not (pull and base and text):
        return "not-ours", cid
    number = int(pull.group(1))
    found = pull_of(repo, number)
    if found is None:
        return "gone", number
    if not found["merged"] and found["state"] != "open":
        return "closed", number
    if not found["merged"]:   # still open: a base that moved means it will not be applied, so it is closed
        current = recortex.read_issue_body(repo, issue)
        return ("moved", number) if record_format.body_sha(current) != base.group(1) else ("waiting", number)
    if (found["merged_by"] or "").lower() not in allowed or found["base"] != versions.BRANCH or not BRANCH_NAME.fullmatch(found["head"] or ""):
        return "stranger", number
    if not found["commit"] or not re.fullmatch(r"[0-9a-f]{40}", found["commit"]):
        return "gone", number
    try:
        merged = versions.file_at(repo, found["commit"], "topics/%d.md" % issue)
    except versions.GitError:
        return "gone", number
    if merged is None or record_format.body_sha(merged) != text.group(1):
        return "other", number
    current = recortex.read_issue_body(repo, issue)
    if record_format.body_sha(current) == text.group(1) or applied(db, state, allowed, issue, base.group(1), text.group(1), cid):
        return "done", number   # the text is in the body, or a told text change says it was written: nothing is left to judge
    if record_format.body_sha(current) != base.group(1):
        return "moved", number
    if topic_file_differs(config, repo, hub, allowed, issue, text.group(1)):
        return "waits", number
    return "ready", {"pull": number, "login": found["merged_by"], "text": merged, "base": current, "base_sha": base.group(1),
                     "comment": cid, "issue": issue}


def approvals_command(args):
    config, repo, hub, plan, allowed, mode = write.load(args)
    write.fresh(repo, hub)
    db = write.open_index(repo)
    results = []
    try:
        state = index.planned(db, config, allowed, plan)
        closed = write_rules.closed_pulls(db, allowed)   # rejections the record already holds
        for cid, issue, author in newest_proposals(db, allowed):
            if args.issue and issue != args.issue:
                continue
            if not index.body_told(db, issue, allowed):
                results.append((issue, "not-ours", cid))
                continue
            results.append((issue,) + evaluate(config, repo, hub, plan, allowed, db, state, cid, issue, author))
    finally:
        db.close()
    results = [found for found in results if found[1] != "done"]
    refused = 0
    written = 0
    for issue, status, detail in results:
        if status == "ready":
            if args.apply and args.dry_run:
                SAY("would apply: #%d, merged as pull request #%d by %s: the body, the version commit and the text memory (write.py "
                    "apply, with the form of a merge on GitHub)" % (issue, detail["pull"], detail["login"]))
                continue
            if not args.apply:
                SAY("approvals: #%d is approved on GitHub (pull request #%d merged by %s); run write.py approvals --apply" % (
                    issue, detail["pull"], detail["login"]))
                continue
            written += 1 if apply_proposal(config, repo, hub, plan, allowed, mode, detail) == 0 else 0
            continue
        if status in ("not-ours", "edited"):
            refused += 1
            SAY(LINES[status] % (detail, issue) if status == "not-ours" else LINES[status] % detail)
        elif status == "unfit":
            SAY(LINES[status] % detail)
        elif status in ("moved", "elsewhere"):
            SAY(LINES[status] % issue)
            if status == "moved" and args.apply and args.dry_run:
                SAY("would close: pull request #%d, with a fixed comment" % detail)
            elif status == "moved" and args.apply:
                close_proposal(repo, detail)
        else:
            SAY(LINES[status] % detail)
            if status == "closed" and args.apply and detail not in closed:
                if args.dry_run:
                    SAY("would post: a checkpoint on #%d that says pull request #%d was closed without merging" % (issue, detail))
                else:
                    drop_proposal(config, repo, issue, detail)
            if status in ("stranger", "other"):
                refused += 1
    if refused:
        SAY("approvals: %d proposal%s refused" % (refused, "" if refused == 1 else "s"))
    if not results:
        SAY("approvals: no proposal waits")
    if args.apply and args.dry_run:
        SAY("dry run: nothing was written")
    return 0


def close_proposal(repo, pull):
    """Close the open pull request of a proposal whose base moved, with a fixed comment; the draft is made again."""
    state = pull_of(repo, pull)
    if state is None or state["merged"] or state["state"] != "open":
        return   # a merged or closed pull request is left as it is
    try:
        record_store.for_repo(repo).github(["issue", "comment", str(pull), "--repo", repo, "--body-file", "-"],
                                           stdin=CLOSED_COMMENT)
        versions.api(repo, "PATCH", "pulls/%d" % pull, {"state": "closed"})
        SAY("approvals: pull request #%d was closed with a fixed comment" % pull)
    except (recortex.GhError, versions.GitError):
        SAY("approvals: pull request #%d could not be closed; close it by hand" % pull)


def proposal_record(repo, issue):
    """The record a proposal of this topic was made from on this machine, found through the live body's hash (the Base)."""
    try:
        live = recortex.read_issue_body(repo, issue)
    except recortex.GhError:
        return None
    found = write.read_record(repo, issue, record_format.body_sha(live))
    return found if found and isinstance(found.get("proposal"), dict) else None


def drop_proposal(config, repo, issue, pull):
    """A checkpoint on the topic says that a proposal was closed without being merged."""
    moment = versions.utc_now()
    text = "\n".join([
        "<!-- ghim:event type=checkpoint at=%s -->" % recortex.marker_time(moment, config),
        "### Checkpoint: %s - proposal closed" % versions.stamp(moment, config), "",
        "**In plain words, we paused here:** a proposed change of the text was closed on GitHub without being merged", "",
        "**State:** The author closed pull request %d without merging it, so the proposed change is dropped and the text is as it "
        "was." % pull, "",
        "**Done since the last checkpoint:** None", "", "**In progress:** None", "",
        "**Next actions:** Draft the change again when the record asks for it, owner the session", "",
        "**Open questions:** None", ""])
    if versions.post_comment(repo, issue, "checkpoint", text, versions.scratch_folder(repo)) is None:
        SAY("approvals: the checkpoint of the closed proposal was not posted")


def apply_proposal(config, repo, hub, plan, allowed, mode, found):
    """Write an approved proposal through write.apply_command, with the form of a merge on GitHub. The files of the folder are
    made here from the live body, the merged text and what the proposal's own record kept; the checks, the resume state, the
    body, the version and the memory are write.py's."""
    import secrets
    import argparse as ap
    issue = found["issue"]
    record = write.read_record(repo, issue, found["base_sha"])
    if record is None or not isinstance(record.get("proposal"), dict) or record["proposal"].get("pull") != found["pull"] \
            or record["proposal"].get("comment") != found["comment"]:
        SAY(LINES["elsewhere"] % issue)
        return 1
    proposal = record["proposal"]
    folder = os.path.join(versions.scratch_folder(repo), "apply-%d-%s" % (issue, found["base_sha"][:8]))
    os.makedirs(folder, exist_ok=True)
    nonce = secrets.token_hex(16)
    record["nonce"] = nonce
    write.write_record(repo, record)
    for name, content in (("base.md", found["base"]), ("draft.md", found["text"]),
                          ("used.json", json.dumps(proposal.get("used") or {"parts": [], "results": [], "decisions": []})),
                          ("memory.json", json.dumps(proposal.get("memory") or {})),
                          ("start.json", json.dumps({"issue": issue, "nonce": nonce}))):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write(content)
    args = ap.Namespace(repo=repo, dir=folder, words_file=None, asked_file=None, unprotect=None, who="the author",
                        merged=(found["login"], found["pull"]))
    return write.apply_command(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return 0

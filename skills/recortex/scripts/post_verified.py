#!/usr/bin/env python3
"""Post one comment, or replace one issue body, with lint and read-back.

comment: lints the draft, posts it with `gh issue comment --body-file`, fetches
the stored comment from the API and compares it with the draft. A memory's
links, cited comments, evidence and explanations are checked against GitHub
(against the local index with --dry-run --offline). A memory that links to no
earlier one gets a note before it is posted; after a verified post the line
`memory id: <id>` gives the id to link to it.

body: lints the draft, prints a diff against the body now on GitHub, saves the
old body to a backup file, edits the issue and reads it back. A replacement
requires --expected-body-file, the live body saved before drafting; a changed
body stops the write. A second read narrows the race but is not an atomic lock.
Use it when what the deliverable says changes: the draft is the whole new text, with no folded
old text. With --section HEADING the draft is only the new text of one `## ` section: the live body is read, that
section replaced and every other line kept as it is, and the whole result is what is linted, diffed, backed up and
written (tasks.py uses it for a task's "Plan" and "Result"). For a final record the issue must be labeled final-record (not hub
or active), and the section labels then follow the headings: the labels of new
sections are added, those of removed sections are removed (only with
--allow-label-removal), and the labels are read back. To convert a body of an
older form and keep it as a comment, use convert_body.py.

Examples:
  post_verified.py comment --issue 12 --kind result --body-file .tmp/result.md --dry-run
  post_verified.py comment --issue 12 --kind decision --body-file .tmp/decision.md
  post_verified.py body --issue 12 --kind final-record --body-file .tmp/topic.md --expected-body-file .tmp/topic-12.base.md --backup .tmp/topic-12.before.md --dry-run
  post_verified.py body --issue 14 --kind active --section Plan --body-file .tmp/plan.md --expected-body-file .tmp/task-14.base.md --backup .tmp/task-14.before.md
"""

import argparse
import difflib
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recortex  # noqa: E402
import record_store  # noqa: E402
import lint_drafts  # noqa: E402
import lookup as record_lookup  # noqa: E402
import record_format  # noqa: E402

NO_LINK = ("note: this memory links to no earlier one; if it follows from, replaces or tests an earlier memory, add "
           "the link first (index.py query recall --issue %d)")


AGAINST_NOTE = "This result speaks against a planned part: follow \"Keep the plan current\"."
FEEDS_NOTE = "This thread feeds %d planned part%s and this result links none; check the recall block."


def plan_notes(repo, config, issue, kind, draft):
    """The two reminders about the plan (references/planning.md), printed before a result is posted: it carries against=
    and so speaks against a planned part; or its thread feeds planned parts and it links none of them. Local and
    best-effort: a project without a plan, or an index that cannot be read, gets none."""
    if kind != "result":
        return []
    pairs = record_format.links(record_format.parse_marker(record_format.first_lines(draft)[0]))
    notes = [AGAINST_NOTE] if any(key == "against" for key, _ in pairs) else []
    try:
        import index
        db = index.connect(repo, create=False)
        if db is None:
            return notes
        try:
            allowed = recortex.allowed_users(repo)
            state = index.planned(db, config, allowed) if allowed and recortex.planning_on(config) else None
            fed = index.planned_for(state, set(index.thread_of(db, issue)[1])) if state else []
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - a reminder is never a reason to stop a post
        return notes
    if fed and not any(target in state["parts"] for _, target in pairs):
        notes.append(FEEDS_NOTE % (len(fed), "" if len(fed) == 1 else "s"))
    return notes


def lint(path, text, kind, repo, config, offline, labels=None, profiles=None, lookup=None):
    cache = {}

    def checker(number):
        if number not in cache:
            cache[number] = recortex.number_exists(repo, number)
        return cache[number]

    findings = lint_drafts.lint_text(text, kind=kind, config=config, labels=labels, profiles=profiles,
                                     number_checker=None if offline else checker, lookup=lookup)
    return lint_drafts.report(path, findings)


REFUSALS = {
    "not_approved": "Posting a reviewed event needs this folder's approval in automatic mode. Shadow mode retains "
                    "reviewed events locally.",
    "not_trusted": "The repository is not in your trusted list.",
    "runtime_unavailable": "Run observer.py runtime-check first.",
    "publication_paused": "Publication is paused. Run acts.py ask resume --scope publication and ask the question it prints.",
    "explicit_hold": "The user has asked that this action not be taken.",
    "not_ready": "Only a reviewed event that is ready or deferred can be posted.",
    "lint_failed": "The reviewed body does not pass the record's rules.",
    "withheld": "The user asked in that session that its moments not be posted. Only the user lifts that. Run acts.py "
                "ask release HOLD_ID and ask the question it prints (observer.py holds lists the holds).",
    "held_unanswered": "This memory waits for the user's answer, and no answer that counts was found for it. It "
                       "counts when the question names the memory's short id and quotes its line exactly as "
                       "observer.py pending --held gives it (the template in the recortex skill), with the options "
                       "Post and Drop, the user answered Post less than 10 minutes ago, and this command posts one "
                       "--eventkey only. Ask again, then run this right after; after a Drop, run observer.py discard "
                       "KEY.",
    "outbound_local": "The body names something of this machine (a local path, the home folder, a user name or an "
                      "email address); discard the event and post your own memory instead.",
}


def observer_comment(args, draft, repo, config):
    """Use the same frozen event and receipt as the automatic publisher.

    None selects the legacy path; "register" selects it too, for a memory the
    main session writes itself while this machine's observer is approved for
    this folder: once verified, the post is registered in the observer's queue,
    so that the observer does not post the same memory a second time.
    """
    import observer_publisher
    import observer_runtime
    import observer_store
    import storage

    marker = record_format.parse_marker(record_format.first_lines(draft)[0]) or {}
    memory = args.kind in record_format.OBSERVED_TYPES or marker.get("type") in record_format.OBSERVED_TYPES
    if not args.eventkey and not memory:
        return None
    try:
        config = observer_runtime.local_config(config)  # this machine's settings for this folder
    except (storage.StorageError, ValueError):
        print("not posted: this machine's observer settings could not be read; run observer.py status.")
        return 1
    raw = config.get("observer", {})
    if not args.eventkey and (not isinstance(raw, dict) or raw.get("enabled") is not True):
        return None
    try:
        with observer_store.Store(repo, config=config, create=False, readonly=True) as store:
            approved = observer_runtime.approval_valid(repo, config)
            if not args.eventkey:
                if not approved:
                    return None
                # A copy of a queued event goes through that event, so that its holds, the session's withhold and
                # the user's answer to a held-back one apply; a memory the session writes itself still posts.
                held = (observer_publisher.queued_copy(store, draft) or observer_publisher.queued_decision(store, draft)
                        or store.held_change_of(draft))
                if held is not None and held["state"] == "rejected":
                    print("not posted: this copies a memory of the observer's that was discarded (event %s). A copy "
                          "of a queued memory is posted only through its event; write the memory in your own words "
                          "if the user asks for it." % held["eventkey"])
                    return 1
                if held is not None:
                    print("not posted: the observer's queue already holds this memory (event %s, %s). Post that "
                          "event with --eventkey %s, so that it is not recorded twice." % (
                              held["eventkey"], held["state"], held["eventkey"]))
                    return 1
                return "register"
            event = store.event(args.eventkey)
            if (event is None or not memory or args.issue != event["candidate"]["issue"] or
                    args.kind != event["candidate"]["kind"] or
                    draft not in (event["candidate"]["body"], event["body"])):
                print("not posted: --eventkey must match this repository, issue, kind and exact reviewed draft.")
                return 1
            if event["state"] == "verified":
                observer_publisher._payload_bound(event)
            else:
                observer_publisher._bound(event, store)
        # The main session, after its own review: it may post a deferred event, and the daily cap of automatic
        # comments is not its. An event the automatic gate held back (a change of status that rests on neither the
        # user's words nor the agent's message) it posts on the user's "Post" answer to its question at a session
        # start or a pause (observer.py pending --held), which the hook turns into a one-time grant; the user's own
        # holds still bind.
        result = observer_publisher.publish(repo, args.eventkey, config, dry_run=args.dry_run, main_session=True)
    except observer_store.StoreError as error:
        if not args.eventkey and error.reason == "missing":
            return None
        print("not posted: the observer queue could not be validated; run observer.py status.")
        return 1
    except observer_publisher.PublishError as error:
        print("not posted: the observer refused this event (%s); run observer.py status. %s" % (
            error.reason if re.fullmatch(r"[a-z_]{1,40}", error.reason or "") else "unknown",
            REFUSALS.get(error.reason, "")))
        return 1
    except (storage.StorageError, OSError, ValueError, KeyError, TypeError):
        print("not posted: the reviewed event or this machine's observer state could not be read; run observer.py "
              "status. Shadow mode retains reviewed events locally.")
        return 1
    if args.dry_run:
        print("would run: " + result["command"])
        print("would use shared observer event: " + result["eventkey"])
        return 0
    if result["state"] == "verified":
        print("verified: comment " + result["receipt"]["url"])
        print("memory id: %d" % result["receipt"]["comment_id"])
        return 0
    print("not verified: observer event %s remains %s; use observer.py reconcile %s. "
          "Do not post another copy." % (args.eventkey, result["state"], args.eventkey))
    return 1


def register_with_observer(repo, config, issue, kind, comment_id):
    """After a verified post of the main session's own memory: tell the observer's queue, or say plainly that it could
    not be told. The post itself stands either way."""
    import observer_publisher
    import observer_runtime
    import observer_store
    import storage
    try:
        waiting = observer_publisher.register_main(repo, observer_runtime.local_config(config), issue, kind, comment_id)
    except (observer_store.StoreError, storage.StorageError, OSError, ValueError):
        print("note: the comment is verified, but the observer's queue could not record it; run observer.py status. "
              "Before the observer posts a %s on #%d, check observer.py pending." % (kind, issue))
        return
    print("observer: registered; the observer leaves this %s on #%d to you" % (kind, issue))
    for key in waiting:
        print("observer: event %s, a %s it had queued for #%d, now waits for you (observer.py pending --eventkey %s "
              "--show-body; post it with --eventkey, or observer.py discard)" % (key, kind, issue, key))


def main(argv=None, *, body_guard=None):
    """A body_guard restricts an embedded caller to a body-only edit and rechecks its approval before sending."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("comment", "body"):
        p = sub.add_parser(name)
        p.add_argument("--repo", help="owner/name (default: from .recortex.json)")
        p.add_argument("--issue", type=int, required=True, help="issue or pull request number")
        p.add_argument("--body-file", required=True, help="the one draft file to post")
        p.add_argument("--kind", required=True, choices=sorted(lint_drafts.KINDS), help="kind of draft, for lint")
        p.add_argument("--dry-run", action="store_true", help="lint and print the gh command; nothing is written")
        p.add_argument("--offline", action="store_true",
                       help="with --dry-run: no network access at all (bare #N is not checked; a comment's links, "
                            "cited comments, evidence and explanations are checked against the local index)")
        if name == "comment":
            p.add_argument("--eventkey", help="publish the exact independently reviewed event from the observer queue")
        if name == "body":
            p.add_argument("--backup", help="file that receives the body being replaced (required unless --dry-run)")
            p.add_argument("--expected-body-file", help="live body saved before drafting; required for a real "
                           "replacement, checked in an online dry run when supplied")
            p.add_argument("--section", help="the --body-file holds only the new text under this `## ` heading; every other "
                           "line of the live body is kept (needs the live body, so not with --offline)")
            p.add_argument("--profile", help="comma-separated profiles that define a final record's section "
                           "headings (default: profiles in .recortex.json)")
            p.add_argument("--allow-label-removal", action="store_true",
                           help="let the run remove section labels whose sections the body no longer has")
            p.add_argument("--allow-other-profiles", action="store_true",
                           help="go on although the issue carries section labels of other profiles; they are left alone")
            p.add_argument("--text-memory", help="a text change (kind text) whose Version field says `pending`: when versions "
                           "are kept, the body edit gets its commit and this comment is posted with that commit in the Version "
                           "field (references/versions.md)")
            p.add_argument("--no-version", action="store_true", help="make no version commit and say nothing about versions "
                           "(write.py makes its own)")
    args = parser.parse_args(argv)
    if args.offline and not args.dry_run:
        parser.error("--offline only makes sense with --dry-run")

    config = recortex.load_config()
    repo = recortex.resolve_repo(args.repo, config)
    records = record_store.for_repo(repo, config)
    print(recortex.describe(config, repo))
    if not os.path.isfile(args.body_file):
        sys.exit("error: not a file: %s" % args.body_file)
    draft = recortex.read_text(args.body_file)
    register = False
    if args.command == "comment":
        queued = observer_comment(args, draft, repo, config)
        register = queued == "register"
        if queued is not None and not register:
            return queued
    composed_from = None
    if args.command == "body" and args.section:
        if args.offline:
            parser.error("--section needs the live body, so it cannot be used with --offline")
        composed_from = recortex.read_issue_body(repo, args.issue)
        composed = record_format.replace_section(composed_from, args.section, draft)
        if composed is None:
            print("not posted: %s#%d has no '## %s' section, so nothing was replaced" % (repo, args.issue, args.section))
            return 1
        args.body_file += ".body"
        with open(args.body_file, "w", encoding="utf-8") as handle:
            handle.write(composed)
        draft = composed
    expected = None
    if args.command == "body" and args.expected_body_file:
        if not os.path.isfile(args.expected_body_file):
            sys.exit("error: not a file: %s" % args.expected_body_file)
        expected = recortex.read_text(args.expected_body_file)
    chosen = getattr(args, "profile", None)
    profiles = [p.strip() for p in chosen.split(",") if p.strip()] if chosen else list(config.get("profiles") or [])
    follow = args.command == "body" and args.kind == "final-record" and not args.offline
    plan = None
    if follow:
        try:
            plan = recortex.plan_section_labels(recortex.read_issue(repo, args.issue)["labels"], draft, profiles)
        except ValueError as error:
            sys.exit("error: %s" % error)
        for reason in plan["stop"]:
            print("not posted: %s#%d: %s" % (repo, args.issue, reason))
        if plan["stop"]:
            return 1
        if plan["foreign"] and not args.allow_other_profiles:
            print("not posted: %s#%d carries section labels of profiles not in use here (%s; profiles in use: %s). "
                  "Pass --profile with the profiles of those labels, or --allow-other-profiles to leave them as they are"
                  % (repo, args.issue, ", ".join(plan["foreign"]), ", ".join(profiles) or "none"))
            return 1
    wanted = plan["wanted"] if plan and plan["checked"] and not plan["problems"] else None
    record = None
    if args.command == "comment" and args.offline:
        try:
            record = record_lookup.local(repo, args.issue)
        except record_lookup.Unavailable as error:
            print("note: %s; links, cited comments, evidence and explanations are not checked" % error)
    elif args.command == "comment":
        record = record_lookup.github(repo, args.issue)
    if lint(args.body_file, draft, args.kind, repo, config, args.offline, labels=wanted, profiles=profiles,
            lookup=record):
        print("not posted: fix the errors above")
        return 1

    if args.command == "comment":
        marker = record_format.parse_marker(record_format.first_lines(draft)[0]) or {}
        if args.kind in record_format.LINKABLE and not lint_drafts.is_reconstructed(draft) \
                and not any(key in marker for key in record_format.LINK_KINDS):
            print(NO_LINK % args.issue)
        for note in plan_notes(repo, config, args.issue, args.kind, draft):
            print("note: " + note)
        if args.dry_run:
            print("would run: " + records.show("post_entry", args.issue, body_file=args.body_file,
                                                  note="%d characters" % len(draft)))
            print("would read back: %s and compare with %s" % (records.read_back("entry"), args.body_file))
            return 0
        url = records.post_entry(args.issue, body_file=args.body_file).strip().split("\n")[-1]
        print("posted: %s" % url)
        posted = records.posted_id(url)
        if not posted:
            print("MISMATCH: could not find the comment id in gh output, so the comment was not read back")
            return 1
        ok = recortex.verify_body(draft, recortex.read_comment_body(repo, posted), "comment %s" % url)
        if ok and args.kind in record_format.MEMORY_TYPES:
            print("memory id: %s" % posted)
        if ok and register:
            register_with_observer(repo, config, args.issue, args.kind if args.kind in record_format.OBSERVED_TYPES
                                   else marker.get("type"), int(posted))
        return 0 if ok else 1

    edit_note = "%d characters" % len(draft)
    if args.dry_run and args.offline:
        print("would run: " + records.show("edit_body", args.issue, body_file=args.body_file, note=edit_note))
        print("expected body not checked: --offline reads nothing from GitHub")
        if args.kind == "final-record":
            print("section labels not checked: --offline reads nothing from GitHub")
        return 0
    labels_change = None
    if follow and plan["checked"]:
        add, remove = plan["add"], plan["remove"]
        if body_guard is not None and (add or remove):
            print("not posted: the reviewed change authorized a body edit only. Refresh the target.")
            return 1
        if add:
            have = {l["name"].lower() for l in records.list_labels()}
            missing = [label for label in add if label.lower() not in have]
            if missing:
                print("not posted: these labels do not exist in %s: %s; run setup_labels.py first"
                      % (repo, ", ".join(missing)))
                return 1
        if remove:
            print("removes: %s (the body has no section for %s)"
                  % (", ".join(remove), "it" if len(remove) == 1 else "them"))
            if not args.dry_run and not args.allow_label_removal:
                print("not posted: check that each of those sections left the deliverable on purpose, then rerun "
                      "with --allow-label-removal")
                return 1
        if add or remove:
            labels_change = (add, remove)
    old = recortex.read_issue_body(repo, args.issue)
    if composed_from is not None and recortex.normalize(old) != recortex.normalize(composed_from):
        print("stopped: the live body of %s#%d changed while the section was being replaced; nothing was written. Read "
              "the current body and rerun." % (repo, args.issue))
        return 1
    diff = list(difflib.unified_diff(recortex.normalize(old).split("\n"), recortex.normalize(draft).split("\n"),
                                     "%s#%d now" % (repo, args.issue), args.body_file, lineterm=""))
    if not diff and not labels_change:
        print("no change: %s#%d already matches %s" % (repo, args.issue, args.body_file))
        return 0
    if diff:
        if expected is None and not args.dry_run:
            print("not posted: --expected-body-file FILE is required for a body replacement; use the live body "
                  "saved before drafting. Read the current body, merge your changes, and rerun.")
            return 1
        if expected is not None and recortex.normalize(old) != recortex.normalize(expected):
            print("not posted: the live body of %s#%d differs from --expected-body-file; "
                  "read the current body and merge your changes before rerunning. No body or labels were changed."
                  % (repo, args.issue))
            return 1
    print("\n".join(diff))
    if args.dry_run:
        if diff and expected is None:
            print("note: a real replacement requires --expected-body-file with the body saved before drafting")
        if diff:
            print("would run: " + records.show("edit_body", args.issue, body_file=args.body_file, note=edit_note))
        if labels_change:
            print("would run: " + records.show("set_labels", args.issue, *labels_change,
                                               note="section labels follow the headings"))
            if plan["remove"] and not args.allow_label_removal:
                print("note: the real run removes section labels only with --allow-label-removal")
        elif follow and not plan["checked"]:
            print("section labels not checked: no profile defines any")
        return 0
    if not diff:
        return follow_labels(repo, args.issue, labels_change, plan, profiles, draft)
    if not args.backup:
        sys.exit("error: --backup FILE is required so the replaced body is kept")
    if os.path.exists(args.backup):
        sys.exit("error: backup file exists and is not overwritten: %s" % args.backup)
    if body_guard is not None:
        body_guard()
    # There is still a race between this read and GitHub's write; do not claim an atomic compare-and-swap.
    if not body_still_matches(repo, args.issue, old, "the body was not replaced and no labels were changed"):
        return 1
    try:
        with open(args.backup, "x", encoding="utf-8") as handle:
            handle.write(old)
    except FileExistsError:
        sys.exit("error: backup file exists and is not overwritten: %s" % args.backup)
    print("backup of the replaced body: %s" % args.backup)
    records.edit_body(args.issue, body_file=args.body_file)
    print("ran: " + records.show("edit_body", args.issue, body_file=args.body_file))
    ok = recortex.verify_body(draft, recortex.read_issue_body(repo, args.issue), "%s#%d" % (repo, args.issue))
    if not ok:
        return 1
    result = follow_labels(repo, args.issue, labels_change, plan, profiles, draft) if follow else 0
    if result == 0 and not args.no_version:
        result = keep_version(config, repo, args, draft)
    return result


def keep_version(config, repo, args, draft):
    """After a verified body edit of a topic of the paper, when versions are kept: the commit of the new text and, when the
    session passed one, its text change with the commit in the Version field. Without a text change the branch is left for
    `versions.py sync`, which records the edit with its account and time."""
    try:
        import versions  # noqa: E402
    except ImportError:
        return 0
    if args.kind != "final-record" or not versions.kept_here(repo) or recortex.plan_profile(
            recortex.shipped_profiles(config.get("profiles"))) is None:
        return 0
    if not args.text_memory:
        print("versions: this body change has no text change of its own; pass --text-memory FILE next time, or "
              "versions.py sync records it as an edit.")
        return 0
    memory = recortex.read_text(args.text_memory)
    after = record_format.sha_pair(record_format.field_text(memory, "Body") or "")
    if after is None or after[1] != record_format.body_sha(draft) or not re.search(r"^\*\*Version:\*\* pending\s*$", memory, re.M):
        print("not posted: the text change must hold '**Body:** before sha256 `...`; after sha256 `...`' of this body and "
              "'**Version:** pending'; the body was written and has no text change yet")
        return 1
    result = versions.commit(config, repo, args.issue, draft)
    field = "`%s`" % result if re.fullmatch(r"[0-9a-f]{40}", result) else "Not kept: %s" % result
    memory = re.sub(r"^\*\*Version:\*\* pending\s*$", lambda _m: "**Version:** " + field, memory, count=1, flags=re.M)
    filled = args.text_memory + ".filled"
    with open(filled, "w", encoding="utf-8") as handle:
        handle.write(memory)
    ran = subprocess.run([sys.executable, os.path.abspath(__file__), "comment", "--repo", repo, "--issue", str(args.issue),
                          "--kind", "text", "--body-file", filled], capture_output=True, text=True)
    print(ran.stdout.rstrip())
    return ran.returncode


def body_still_matches(repo, number, expected, consequence):
    """Refuse a write if another edit appeared since the body was checked."""
    if recortex.normalize(recortex.read_issue_body(repo, number)) == recortex.normalize(expected):
        return True
    print("stopped: the live body of %s#%d changed during this run; %s. "
          "Read the current body and merge your changes before rerunning." % (repo, number, consequence))
    return False


def follow_labels(repo, number, change, plan, profiles, draft):
    """Apply the section-label change, read the labels back and compare."""
    if not plan["checked"]:
        print("section labels not checked: no profile defines any")
        return 0
    wanted = plan["wanted"]
    if change:
        if not body_still_matches(repo, number, draft, "section labels were not changed"):
            return 1
        records = record_store.for_repo(repo)
        records.set_labels(number, *change)
        print("ran: " + records.show("set_labels", number, *change))
    now = recortex.read_issue(repo, number)["labels"]
    add, remove = recortex.label_changes(now, wanted, profiles)
    if add or remove:
        print("MISMATCH: labels of %s#%d read back as %s; still to add: %s; still to remove: %s"
              % (repo, number, ", ".join(now) or "none", ", ".join(add) or "none", ", ".join(remove) or "none"))
        return 1
    print("verified: section labels of %s#%d follow the headings (%s)" % (repo, number, ", ".join(wanted) or "none"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except recortex.GhError as error:
        sys.exit("error: %s" % error)

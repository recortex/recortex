#!/usr/bin/env python3
"""Create interlinked issues and comments from drafts listed in a manifest.

Drafts refer to issues that do not exist yet as NEW-<key>. This script creates
the issues, replaces every NEW-<key> with the real number (#N in prose,
"Issue N" inside Mermaid), writes the final bodies, and reads each one back
from the API to confirm it matches the draft.

Every draft is linted before anything is posted, with no network; each one is
linted again just before it is posted, when bare #N and a memory's links, cited
comments, evidence and explanations are checked against GitHub. A comment of
the manifest cannot link to another comment of the same manifest, whose id does
not exist yet: post such a memory afterwards with post_verified.py.

Only files named in the manifest are posted. Nothing is ever deleted. Progress
is kept in <manifest>.state.json, so a rerun continues where a failed run
stopped:
- before each creation a pending entry is written; if the run dies before the
  result is saved, the rerun first looks on GitHub for the item it may already
  have created (same author, same title or body) and adopts it;
- a body is edited only while it still is what this script wrote (the stub or
  the last text it posted); a body changed by anyone else is left alone;
- a comment is identified by its target and its draft file, not by its
  position, so the manifest can grow;
- a missing pin is retried on every run;
- each topic is made a sub-issue of the hub, and each active task a sub-issue of its destination topic (a "Destination
  topic" section naming NEW-<key> or #N); a link GitHub refuses is reported in one fixed line and the hub's tables
  stay the structure.

Manifest (JSON):
  {"repo": "owner/name",
   "issues":   [{"key": "hub", "kind": "hub", "title": "...", "labels": ["hub"],
                 "body_file": "hub.md", "pin": true}],
   "comments": [{"issue": "NEW-hub", "kind": "checkpoint", "body_file": "c1.md"},
                {"issue": 7, "repo": "owner/old-tracker", "kind": "pointer", "body_file": "p7.md"}]}

Examples:
  create_issues.py --manifest drafts/manifest.json --dry-run
  create_issues.py --manifest drafts/manifest.json
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import names  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402
import lint_drafts  # noqa: E402
import lookup as record_lookup  # noqa: E402
import record_format  # noqa: E402
import sub_issues  # noqa: E402

STUB = "This issue is being created. Its full text follows within minutes."


def digest(text):
    return hashlib.sha256(recortex.normalize(text).encode("utf-8")).hexdigest()[:16]


def save_state(path, state):
    with open(path + ".part", "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(path + ".part", path)


def number_from_url(url, records):
    return records.created_number(url)


def comment_id_from_url(url, records):
    found = records.posted_id(url)
    if found is None:
        raise recortex.GhError("could not read a comment id from gh output: %r" % url)
    return found


DESTINATION = re.compile(r"^## Destination topic[ \t]*\n(.*?)(?=^## |\Z)", re.M | re.S)
TARGET = re.compile(r"NEW-([a-z0-9]+(?:-[a-z0-9]+)*)|(?<![A-Za-z0-9_/&#])#([0-9]{1,7})\b")


def link_pause():
    """Seconds between two links: RECORTEX_LINK_PAUSE, digits with an optional fraction, at most 10, default 1."""
    value = names.env("LINK_PAUSE", "1")
    return min(10.0, float(value)) if re.fullmatch(r"[0-9]{1,2}(\.[0-9]{1,2})?", value) else 1.0


def sub_issue_links(manifest, numbers, config, repo):
    """[(child key, child number, parent number)]: each topic under the hub (the manifest's, else the project's),
    each active task under the topic its "Destination topic" section names."""
    hub = next((numbers[i["key"]] for i in manifest["issues"] if i["kind"] == "hub" and i["key"] in numbers), None)
    if hub is None and str(config.get("repo", "")).lower() == repo.lower() and type(config.get("hub")) is int:
        hub = config["hub"]
    links = []
    kinds = {item["key"]: item["kind"] for item in manifest["issues"]}
    for item in manifest["issues"]:
        child = numbers.get(item["key"])
        if child is None:
            continue
        if item["kind"] == "final-record" and hub:
            links.append((item["key"], child, hub))
        elif item["kind"] == "active":
            section = DESTINATION.search(recortex.read_text(item["_path"]))
            found = TARGET.search(section.group(1)) if section else None
            parent = None
            if found and found.group(1):  # a NEW- key counts only when it is a topic of this manifest
                parent = numbers.get(found.group(1)) if kinds.get(found.group(1)) == "final-record" else None
            elif found:
                parent = int(found.group(2))
            if parent and parent != child:
                links.append((item["key"], child, parent))
    return links


def lint_all(manifest, config):
    keys = {item["key"] for item in manifest["issues"]}
    errors = 0
    for item in manifest["issues"] + manifest["comments"]:
        findings = lint_drafts.lint_text(recortex.read_text(item["_path"]), kind=item["kind"],
                                         allow_placeholders=True, known_keys=keys, config=config,
                                         labels=item.get("labels") if item in manifest["issues"] else None)
        # offline: bare #N and what only the record can tell are checked at posting, against GitHub
        findings = [f for f in findings if not (f.code in ("bare-ref", "not-checked") and f.level == "warning")]
        errors += lint_drafts.report(item["_path"], findings, quiet=True)
    return errors


def observer_registrar(config, repo):
    """For a folder whose observer this machine approved: a function that registers each verified memory comment of
    the manifest in the observer's queue, so that the observer does not post it a second time. None when the observer
    is off here; False (with a line) when this machine's settings cannot be read."""
    import observer_publisher
    import observer_runtime
    import observer_store
    import storage

    try:
        config = observer_runtime.local_config(config)  # this machine's settings for this folder
    except (storage.StorageError, ValueError):
        print("nothing posted: this machine's observer settings could not be read; run observer.py status.")
        return False
    if not observer_runtime.approval_valid(repo, config):
        return None

    def register(target, number, kind, comment_id):
        if target.lower() != repo.lower():
            return  # the approval names one repository
        try:
            for key in observer_publisher.register_main(repo, config, number, kind, comment_id):
                print("observer: event %s, a %s it had queued for #%d, now waits for you (observer.py pending)"
                      % (key, kind, number))
        except (observer_store.StoreError, storage.StorageError, OSError, ValueError):
            print("note: comment %d is verified, but the observer's queue could not record it; run observer.py "
                  "status" % comment_id)
    return register


def final_lint(text, item, repo, numbers, config, checked, number=None):
    def checker(number):
        if number in numbers.values() and repo == item.get("_home"):
            return True
        if (repo, number) not in checked:
            checked[(repo, number)] = recortex.number_exists(repo, number)
        return checked[(repo, number)]

    findings = lint_drafts.lint_text(text, kind=item["kind"], number_checker=checker, config=config,
                                     labels=item.get("labels") if "key" in item else None,
                                     lookup=None if number is None else record_lookup.github(repo, number))
    return lint_drafts.report(item["_path"], findings, quiet=True)


def find_created_issue(repo, title, bodies):
    """Look for an issue this account already created with this title and one of these bodies."""
    rows = record_store.for_repo(repo).find_issues("mine")
    wanted = {recortex.normalize(body) for body in bodies}
    return [row for row in rows if row.get("title") == title and recortex.normalize(row.get("body") or "") in wanted]


def find_posted_comment(repo, number, body):
    """Look for a comment by this account on the issue with exactly this body."""
    records = record_store.for_repo(repo)
    me = records.whoami()
    rows = recortex.parse_json_stream(records.list_entries(number))
    return [row for row in rows if (row.get("user") or {}).get("login") == me
            and recortex.normalize(row.get("body") or "") == recortex.normalize(body)]


def is_pinned(repo, number):
    return record_store.for_repo(repo).is_pinned(number)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, help="JSON manifest listing the drafts to post")
    parser.add_argument("--repo", help="owner/name (default: manifest, then .recortex.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="lint and print every gh command; no network access, nothing written")
    args = parser.parse_args(argv)

    config = recortex.load_config()
    manifest = lint_drafts.load_manifest(args.manifest)
    manifest.setdefault("issues", [])
    manifest.setdefault("comments", [])
    repo = recortex.resolve_repo(args.repo or manifest.get("repo"), config)
    records = record_store.for_repo(repo, config)
    print(recortex.describe(config, repo))
    state_path = args.manifest + ".state.json"
    state = {"repo": repo, "issues": {}, "comments": {}}
    if os.path.isfile(state_path):
        state = json.loads(recortex.read_text(state_path))
        if state.get("repo") != repo:
            sys.exit("error: %s belongs to %s, not %s" % (state_path, state.get("repo"), repo))

    print("manifest: %s   repository: %s   issues: %d   comments: %d"
          % (args.manifest, repo, len(manifest["issues"]), len(manifest["comments"])))
    if lint_all(manifest, config):
        print("nothing posted: fix the lint errors above")
        return 1
    registrar = observer_registrar(config, repo)
    if registrar is False:
        return 1
    print("lint: all %d drafts pass" % (len(manifest["issues"]) + len(manifest["comments"])))

    if args.dry_run:
        return dry_run(manifest, repo, state)

    wanted = sorted({label for item in manifest["issues"] for label in item["labels"]})
    have = {l["name"].lower() for l in records.list_labels()}
    missing = [label for label in wanted if label.lower() not in have]
    if missing:
        print("nothing posted: these labels do not exist in %s: %s" % (repo, ", ".join(missing)))
        print("run setup_labels.py first")
        return 1

    failures, checked = 0, {}

    # 1. Create every issue that does not exist yet.
    for item in manifest["issues"]:
        key = item["key"]
        entry = state["issues"].get(key) or {}
        if entry.get("number"):
            print("exists: NEW-%s is #%d" % (key, entry["number"]))
            continue
        draft = recortex.read_text(item["_path"])
        direct = not recortex.PLACEHOLDER_RE.search(draft)
        first_body = draft if direct else STUB
        if entry.get("pending"):
            found = find_created_issue(repo, item["title"], [STUB, draft])
            if len(found) > 1:
                print("stopped: an earlier run may have created NEW-%s, and %d issues by this account match its "
                      "title and text (%s). Put the right number into %s as issues.%s.number and rerun."
                      % (key, len(found), ", ".join("#%d" % row["number"] for row in found), state_path, key))
                return 1
            if found:
                state["issues"][key] = {"number": found[0]["number"], "url": found[0]["url"],
                                        "written": digest(found[0].get("body") or ""), "verified": None}
                save_state(state_path, state)
                print("adopted: NEW-%s is #%d, created by an earlier run that did not finish"
                      % (key, found[0]["number"]))
                continue
        state["issues"][key] = {"pending": True}
        save_state(state_path, state)
        url = records.create_issue(item["title"], item["labels"], body=first_body).strip().split("\n")[-1]
        number = number_from_url(url, records)
        state["issues"][key] = {"number": number, "url": url, "written": digest(first_body), "verified": None}
        save_state(state_path, state)
        print("created: NEW-%s is #%d  %s" % (key, number, url))

    numbers = {key: entry["number"] for key, entry in state["issues"].items() if entry.get("number")}

    # 2. Write the final bodies, read them back, pin.
    for item in manifest["issues"]:
        key, number = item["key"], numbers[item["key"]]
        entry = state["issues"][key]
        item["_home"] = repo
        body = recortex.substitute_placeholders(recortex.read_text(item["_path"]), numbers, repo, repo)
        if entry.get("verified") == digest(body):
            print("already verified: #%d (%s)" % (number, key))
        elif final_lint(body, item, repo, numbers, config, checked):
            print("not written: #%d (%s) fails lint after substitution" % (number, key))
            failures += 1
        else:
            current = recortex.read_issue_body(repo, number)
            if digest(current) != digest(body):
                if digest(current) not in (digest(STUB), entry.get("written")):
                    print("not written: #%d (%s) was changed on GitHub since this script wrote it, so it is left "
                          "alone. To replace it on purpose, with a diff and a backup, use post_verified.py body."
                          % (number, key))
                    failures += 1
                    current = None
                else:
                    records.edit_body(number, body=body)
                    entry["written"] = digest(body)
                    save_state(state_path, state)
            if current is not None:
                if recortex.verify_body(body, recortex.read_issue_body(repo, number), "#%d (%s)" % (number, key)):
                    entry["written"] = entry["verified"] = digest(body)
                    save_state(state_path, state)
                else:
                    failures += 1
        if item.get("pin") and records.supports("pinning"):
            try:
                if not is_pinned(repo, number):
                    records.pin(number)
                pinned = is_pinned(repo, number)
            except recortex.GhError as error:
                print("pin failed: %s" % error)
                pinned = False
            entry["pinned"] = pinned
            save_state(state_path, state)
            if pinned:
                print("pinned: #%d" % number)
            else:
                print("NOT PINNED: #%d (%s); rerun to try again" % (number, key))
                failures += 1

    # 2b. Sub-issues. A refusal is one fixed line, not a failure: the hub's tables stay the structure.
    for key, child, parent in (sub_issue_links(manifest, numbers, config, repo) if records.supports("sub-issues") else []):
        entry = state["issues"][key]
        if entry.get("parent") == parent:
            continue
        result = sub_issues.link(repo, parent, child)
        if result in ("linked", "already"):
            entry["parent"] = parent
            save_state(state_path, state)
            print("sub-issue: #%d is under #%d" % (child, parent))
            if result == "linked":
                time.sleep(link_pause())  # GitHub asks for a second between writing requests
        elif result == "other":
            print("sub-issue: #%d already has another parent on GitHub; left alone" % child)
        elif result == "mismatch":
            print("MISMATCH: #%d does not read back under #%d; rerun to try again" % (child, parent))
            failures += 1
        else:
            print(sub_issues.UNREAD if result == "unread" else sub_issues.REFUSED)
            break

    # 3. Post the comments. Each is known by its target and draft file.
    for index, item in enumerate(manifest["comments"]):
        target_repo = item.get("repo", repo)
        item["_home"] = repo
        target = item["issue"]
        number = target if isinstance(target, int) else numbers[recortex.PLACEHOLDER_RE.fullmatch(target).group(1)]
        label = "comment %s on %s#%d" % (item["body_file"], target_repo, number)
        body = recortex.substitute_placeholders(recortex.read_text(item["_path"]), numbers, target_repo, repo)
        slot = "%s#%d:%s" % (target_repo, number, item["body_file"])
        entry = state["comments"].get(slot) or {}
        if entry.get("url") and entry.get("verified") == digest(body):
            print("already posted: %s" % label)
            continue
        if entry.get("url") and entry.get("written") not in (None, digest(body)):
            print("not posted: %s was posted before with different text (%s). "
                  "Post a correction comment instead of changing it." % (label, entry["url"]))
            failures += 1
            continue
        if not entry.get("url"):
            if final_lint(body, item, target_repo, numbers if target_repo == repo else {}, config, checked, number):
                print("not posted: %s fails lint after substitution" % label)
                failures += 1
                continue
            found = find_posted_comment(target_repo, number, body) if entry.get("pending") else []
            if found:
                entry = {"url": found[0]["html_url"], "written": digest(body), "verified": None}
                print("adopted: %s, posted by an earlier run that did not finish" % label)
            else:
                state["comments"][slot] = {"pending": True}
                save_state(state_path, state)
                url = record_store.for_repo(target_repo).post_entry(number, body=body).strip().split("\n")[-1]
                entry = {"url": url, "written": digest(body), "verified": None}
            state["comments"][slot] = entry
            save_state(state_path, state)
        target_records = record_store.for_repo(target_repo)
        returned = recortex.read_comment_body(target_repo, comment_id_from_url(entry["url"], target_records))
        if recortex.verify_body(body, returned, label):
            entry["verified"] = digest(body)
            save_state(state_path, state)
            marker = record_format.parse_marker(record_format.first_lines(body)[0]) or {}
            kind = item["kind"] if item["kind"] in record_format.OBSERVED_TYPES else marker.get("type")
            if registrar and kind in record_format.OBSERVED_TYPES:
                registrar(target_repo, number, kind, int(comment_id_from_url(entry["url"], target_records)))
        else:
            failures += 1

    print("")
    print("key -> issue")
    for item in manifest["issues"]:
        print("  NEW-%s -> #%d  %s" % (item["key"], numbers[item["key"]], state["issues"][item["key"]]["url"]))
    print("state file: %s" % state_path)
    if failures:
        print("%d item(s) failed; fix and rerun the same command" % failures)
        return 1
    print("all items posted and verified")
    return 0


def dry_run(manifest, repo, state):
    records = record_store.for_repo(repo)
    print("dry run: no network access, nothing written")
    print("note: commands are listed issue by issue. The real run creates all issues first, then writes "
          "the bodies, then posts the comments.")
    known = {key: entry["number"] for key, entry in state.get("issues", {}).items() if entry.get("number")}
    for item in manifest["issues"]:
        key = item["key"]
        draft = recortex.read_text(item["_path"])
        refs = sorted(set(recortex.PLACEHOLDER_RE.findall(draft)))
        if key in known:
            print("exists: NEW-%s is #%d (from the state file)" % (key, known[key]))
        else:
            note = "body: %s" % ("a one-line stub, replaced in the next step" if refs
                                 else "%s, %d characters" % (item["body_file"], len(draft)))
            print("would run: " + records.show("create_issue", item["title"], item["labels"], note=note))
        if refs or key in known:
            print("would run: " + records.show(
                "edit_body", "<number of NEW-%s>" % key,
                note="%s, %d characters, with %s replaced by real numbers"
                % (item["body_file"], len(draft), ", ".join("NEW-" + r for r in refs) or "no placeholders")))
        print("would read back: %s and compare with %s"
              % (records.read_back("issue", "<number of NEW-%s>" % key), item["body_file"]))
        if item.get("pin") and records.supports("pinning"):
            print("would run: " + records.show("pin", "<number of NEW-%s>" % key))
    for item in manifest["issues"]:
        if item["kind"] == "final-record":
            print("would link: NEW-%s as a sub-issue of the hub (GraphQL addSubIssue, read back through Issue.parent)"
                  % item["key"])
        elif item["kind"] == "active":
            section = DESTINATION.search(recortex.read_text(item["_path"]))
            found = TARGET.search(section.group(1)) if section else None
            if found:
                print("would link: NEW-%s as a sub-issue of %s (GraphQL addSubIssue, read back through Issue.parent)"
                      % (item["key"], "NEW-" + found.group(1) if found.group(1) else "#" + found.group(2)))
    for index, item in enumerate(manifest["comments"]):
        target = item["issue"]
        shown = str(target) if isinstance(target, int) else "<number of %s>" % target
        target_repo = item.get("repo", repo)
        target_records = record_store.for_repo(target_repo)
        print("would run: " + target_records.show(
            "post_entry", shown, note="%s, %d characters" % (item["body_file"], len(recortex.read_text(item["_path"])))))
        print("would read back: %s and compare with %s" % (target_records.read_back("entry"), item["body_file"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except recortex.GhError as error:
        sys.exit("error: %s\nrerun the same command to continue; finished items are skipped" % error)

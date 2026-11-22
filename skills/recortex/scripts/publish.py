#!/usr/bin/env python3
"""Put the project's static dashboard page on the web, from your own terminal. It is opt-in, public and yours to run: nothing
in a session runs it (pretool_guard.py stops a session's call, and every form of this command needs your terminal).

  publish.py --to OWNER/NAME --branch BRANCH [--path index.html] [--private-record] [--dry-run]
  publish.py --list

It builds the page itself (mind.public_text, the same steps as `mind.py` with no usage numbers, no log of the observer and no
interactive part), so it can never be pointed at another file, and it sends the page to one file of one branch of a repository
you name through GitHub's contents API. It creates no repository, no branch and no site: the branch must exist, and the page is
served only if you turned GitHub Pages on for that branch yourself. Before anything is sent it checks that the page is the
static one, that it holds nothing the project's privacy rules keep off the record (a secret, a restricted pattern), that none
of your holds stops publishing, that the account may write there and that the branch exists; it prints what will be public,
and asks you to type the word `publish` (or, when the record's repository is private or its visibility could not be read, to
run it with --private-record and type that repository's full name). It reads the file back and compares its hash.

To take the page down, delete the file from the branch. Copies that others made stay.

Examples (invented):
  publish.py --to octo-org/octo-pages --branch gh-pages --dry-run
  publish.py --to octo-org/octo-pages --branch gh-pages --path dashboards/sample-project.html
"""

import argparse
import base64
import hashlib
import html.parser
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recortex  # noqa: E402
import storage  # noqa: E402

BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
PATH = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,63}/){0,3}[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.html")
MESSAGE = "Publish the project dashboard page"   # the commit message: nothing else, and no trailer
SIZE_MAX = 25 * 1024 * 1024
RECEIPTS = "published.json"
RECEIPTS_KEPT = 20
NOT_TERMINAL = "publish: this is your own command. Run it in your own terminal, outside Claude Code."
BAD_TARGET = ("publish: the target must be a repository as owner/name, the branch a plain branch name and the path a plain "
              ".html path (at most four parts); nothing was sent.")
HELD = "publish: a hold of yours stops publishing; nothing was sent."
HOLDS_UNREADABLE = "publish: your holds could not be read, so publishing is stopped; nothing was sent."
NOT_STATIC = "publish: the page is not the static page; nothing was sent."
NOT_PRIVATE = ("publish: the page holds text that the project's rules keep off the record (a secret or a restricted pattern); "
               "nothing was sent.")
TOO_BIG = "publish: the page is larger than 25 MB; nothing was sent."
UNREADABLE_GITHUB = "publish: GitHub could not be read just now; nothing was sent."
NO_PUSH = "publish: your account may not write to %s; nothing was sent."
NO_BRANCH = ("publish: the branch %s does not exist in %s. publish.py never creates a branch, a repository or a site. Create "
             "it yourself, for example: git switch --orphan %s; git commit --allow-empty -m 'Start the page'; git push origin "
             "%s. Then run this again.")
PRIVATE_RECORD = ("publish: the record is private. Run again with --private-record if you mean to make this page public.")
NOT_CONFIRMED = "publish: not confirmed; nothing was sent."
MISMATCH = "publish: the file on the branch is not the page that was sent; look at the branch."
NOT_SENT = "publish: GitHub did not take the page; nothing was published."
NOTHING = "publish: nothing has been published from this machine."
STATIC = "Static snapshot."
INTERACTIVE = "Local interactive session."
SERVICE_ADDRESS = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}/[0-9a-f]{32}")


class Visible(html.parser.HTMLParser):
    """The page's visible text: tags taken out, entities resolved, and the style and the script left out (they hold no text
    of the record; the page's own rules keep it out of them)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible_text(page):
    parser = Visible()
    parser.feed(page)
    parser.close()
    return "\n".join(parser.parts)


def public_problem(page):
    """"" when the page is the static one: it says "Static snapshot.", never "Local interactive session.", and holds no address
    of the dashboard's local service; else the fixed line that stops publishing."""
    if STATIC not in page or INTERACTIVE in page or SERVICE_ADDRESS.search(page):
        return NOT_STATIC
    return ""


def blob_sha(data):
    """The git blob hash of some bytes: what GitHub calls the sha of a file."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def checked_target(to, branch, path):
    return (recortex.valid_repo(to) and BRANCH.fullmatch(branch) is not None and ".." not in branch
            and not branch.endswith(("/", ".lock")) and "//" not in branch and PATH.fullmatch(path) is not None)


def read_repository(name):
    """(private, may_push) of a repository as GitHub says it; private is None when that could not be read."""
    data = json.loads(recortex.gh(["api", "repos/%s" % name]))
    private = data.get("private") if isinstance(data, dict) and isinstance(data.get("private"), bool) else None
    permissions = data.get("permissions") if isinstance(data, dict) else None
    push = permissions.get("push") if isinstance(permissions, dict) else None
    return private, push is True


def missing(error):
    """True when a gh error is GitHub's 404: the text names it; nothing of it is printed."""
    text = str(error)
    return "404" in text or "Not Found" in text or "not found" in text


def held(record):
    """None when no hold of the user's stops publishing, else the fixed line. A queue this machine has not made is no hold."""
    import observer_store
    try:
        holds = observer_store.read_holds(record)
    except (observer_store.StoreError, storage.StorageError, OSError, ValueError):
        return HOLDS_UNREADABLE
    for hold in holds:
        if not isinstance(hold, dict):
            return HOLDS_UNREADABLE
        if hold.get("state") == "active" and hold.get("action") == "publish":
            return HELD
    return None


def receipts_path(record):
    folder = storage.folder(storage.state_top(), storage.repo_folder(record.lower()))
    return os.path.join(folder, RECEIPTS)


def read_receipts(record):
    """The receipts kept, newest last, or [] when there are none or the file is not what publish.py writes."""
    try:
        text = storage.read_file(receipts_path(record))
        data = json.loads(text) if text else {}
    except (OSError, ValueError, storage.StorageError, RecursionError):
        return []
    rows = data.get("receipts") if isinstance(data, dict) and data.get("form") == 1 else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows[-RECEIPTS_KEPT:] if isinstance(row, dict) and set(row) == {"at", "to", "branch", "path", "sha"}
            and isinstance(row["to"], str) and recortex.valid_repo(row["to"]) and isinstance(row["branch"], str)
            and BRANCH.fullmatch(row["branch"]) and isinstance(row["path"], str) and PATH.fullmatch(row["path"])
            and isinstance(row["sha"], str) and re.fullmatch(r"[0-9a-f]{40}", row["sha"])
            and type(row["at"]) is int and 0 < row["at"] < 4102444800]


def keep_receipt(record, to, branch, path, sha):
    """Keep what was published: the newest 20 of {at, to, branch, path, sha}, through storage.py. False when it could not be
    written (one line is said; the publish stands)."""
    try:
        rows = read_receipts(record) + [{"at": int(time.time()), "to": to, "branch": branch, "path": path, "sha": sha}]
        storage.write_file(receipts_path(record), json.dumps({"form": 1, "receipts": rows[-RECEIPTS_KEPT:]}) + "\n")
        return True
    except (OSError, ValueError, storage.StorageError):
        return False


def listing(record):
    rows = read_receipts(record)
    if not rows:
        print(NOTHING)
        return 0
    for row in rows:
        print("%s UTC  %s  branch %s  file %s  sha %s" % (time.strftime("%Y-%m-%d %H:%M", time.gmtime(row["at"])), row["to"],
                                                         row["branch"], row["path"], row["sha"]))
    return 0


def warning(counts, record, kilobytes, to, branch, path, creates, target_private, record_private):
    lines = ["This page will be public. Anyone who has its address can read it, and search engines may list it.",
             "It holds what the dashboard tells: the text of %d memories in %d threads, the topic bodies, the tasks and the "
             "project map of %s (%d KB)." % (counts["memories"], counts["threads"], record, kilobytes),
             "It goes to %s, branch %s, file %s, which it %s." % (to, branch, path, "creates" if creates else "replaces")]
    if target_private:
        lines.append("The target repository is private. A site published from it can still be public; treat the page as "
                     "public.")
    if record_private is not False:
        lines.append("The record's repository %s is PRIVATE. Publishing makes its contents public." % record)
    lines.append("Not sent: the interactive dashboard, its address and token, launch files, your usage numbers and the "
                 "observer's log.")
    lines.append("To take it down, delete the file from the branch. Copies that others made stay.")
    return lines


def run(args):
    import observer
    if not observer.on_terminal():
        print(NOT_TERMINAL)
        return 1
    import setup
    config, record, line = setup.questions_project()
    if config is None:
        print("publish: " + line.split(": ", 1)[1])
        return 1
    if args.list:
        return listing(record)
    if not args.to or not args.branch:
        print("publish: say where with --to OWNER/NAME and --branch BRANCH (or --list).")
        return 1
    if not checked_target(args.to, args.branch, args.path):
        print(BAD_TARGET)
        return 1
    problem = held(record)
    if problem:
        print(problem)
        return 1
    import mind
    allowed = recortex.allowed_users(record)
    try:
        page, counts = mind.public_text(config, record, allowed)
    except (mind.PageError, OSError, ValueError, storage.StorageError) as error:
        print(str(error) if isinstance(error, mind.PageError) else "publish: the page could not be built; nothing was sent.")
        return 1
    data = page.encode("utf-8")
    if len(data) > SIZE_MAX:
        print(TOO_BIG)
        return 1
    problem = public_problem(page)
    if problem:
        print(problem)
        return 1
    import observer_worker
    try:
        private_text = observer_worker.privacy_problem(visible_text(page), config)
    except Exception:  # noqa: BLE001 - a check that cannot be made is a stop, never a send
        private_text = True
    if private_text:
        print(NOT_PRIVATE)
        return 1
    try:
        target_private, may_push = read_repository(args.to)
        if not may_push:
            print(NO_PUSH % args.to)
            return 1
        try:
            recortex.gh(["api", "repos/%s/branches/%s" % (args.to, args.branch)])
        except recortex.GhError as error:
            if missing(error):
                print(NO_BRANCH % (args.branch, args.to, args.branch, args.branch))
                return 1
            raise
        sha = None
        try:
            found = json.loads(recortex.gh(["api", "repos/%s/contents/%s?ref=%s" % (args.to, args.path, args.branch)]))
            if isinstance(found, dict) and isinstance(found.get("sha"), str) and re.fullmatch(r"[0-9a-f]{40}", found["sha"]):
                sha = found["sha"]
            elif isinstance(found, dict) and found.get("type") == "file":
                raise ValueError("no sha")
            else:
                print("publish: %s is not a file on that branch; nothing was sent." % args.path)
                return 1
        except recortex.GhError as error:
            if not missing(error):
                raise
        try:
            record_private = read_repository(record)[0]
        except (recortex.GhError, ValueError, TypeError):
            record_private = None   # a visibility that cannot be read counts as private
    except (recortex.GhError, ValueError, TypeError, KeyError):
        print(UNREADABLE_GITHUB)
        return 1
    kilobytes = (len(data) + 1023) // 1024
    for text in warning(counts, record, kilobytes, args.to, args.branch, args.path, sha is None, target_private is True,
                        record_private):
        print(text)
    body = {"message": MESSAGE, "branch": args.branch, "content": base64.b64encode(data).decode("ascii")}
    if sha:
        body["sha"] = sha
    command = ["api", "-X", "PUT", "repos/%s/contents/%s" % (args.to, args.path), "--input", "-"]
    if args.dry_run:
        shown = dict(body, content="<%d bytes, sha256 %s>" % (len(data), hashlib.sha256(data).hexdigest()))
        print("would run: gh api repos/%s" % args.to)
        print("would run: gh api repos/%s/branches/%s" % (args.to, args.branch))
        print("would run: gh api repos/%s/contents/%s?ref=%s" % (args.to, args.path, args.branch))
        print("would run: gh api repos/%s" % record)
        print("would run: gh %s   # %s" % (" ".join(command), json.dumps(shown, sort_keys=True)))
        print("dry run: nothing was sent")
        return 0
    if record_private is not False and not args.private_record:
        print(PRIVATE_RECORD)
        return 1
    word = "publish" if record_private is False else record
    tty = observer.terminal()
    if tty is None:
        print(NOT_CONFIRMED)
        return 1
    with tty:
        typed = tty.ask("Type %s to confirm: " % ("the word publish" if record_private is False else
                                                  "the record's full name, %s," % record))
    if (typed != word) if record_private is False else (typed.lower() != record.lower()):
        print(NOT_CONFIRMED)
        return 1
    try:
        recortex.gh(command, stdin=json.dumps(body), timeout=120)
    except recortex.GhError:
        print(NOT_SENT)
        return 1
    try:
        back = json.loads(recortex.gh(["api", "repos/%s/contents/%s?ref=%s" % (args.to, args.path, args.branch)]))
        stored = back.get("sha") if isinstance(back, dict) else None
    except (recortex.GhError, ValueError, TypeError):
        stored = None
    expected = blob_sha(data)
    if stored != expected:
        print(MISMATCH)
        return 1
    print("published and read back: https://github.com/%s/blob/%s/%s" % (args.to, args.branch, args.path))
    print("If %s serves this branch with GitHub Pages, the page is at its Pages address; publish.py does not switch Pages on."
          % args.to)
    if not keep_receipt(record, args.to, args.branch, args.path, expected):
        print("publish: the receipt could not be kept on this machine; the page is published.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", metavar="OWNER/NAME", help="the repository that gets the page")
    parser.add_argument("--branch", metavar="BRANCH", help="its branch (it must exist)")
    parser.add_argument("--path", default="index.html", help="the file in that branch (default index.html)")
    parser.add_argument("--private-record", action="store_true",
                        help="you mean to make a page public that comes from a private record")
    parser.add_argument("--dry-run", action="store_true", help="read GitHub and print what would be sent; send nothing")
    parser.add_argument("--list", action="store_true", help="what was published from this machine")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (recortex.GhError, storage.StorageError, OSError, ValueError) as error:
        print("publish: it stopped (%s); nothing was published unless a line above says so." % type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())

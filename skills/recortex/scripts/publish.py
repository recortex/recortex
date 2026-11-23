#!/usr/bin/env python3
"""Put the project's static dashboard page on the web. It is opt-in, public and the user's own decision: it goes on the user's
answer to the question that `acts.py ask publish` prints (in a session), or on the typed word `publish` on their own terminal.

  publish.py --to OWNER/NAME --branch BRANCH [--path index.html] [--create-branch] [--pages] [--private-record] [--no-site]
             [--dry-run]
  publish.py --list

It builds the page itself (mind.public_text, the same steps as `mind.py` with no usage numbers, no log of the observer and no
interactive part), so it can never be pointed at another file, and it sends the page to one file of one branch of a repository
through GitHub's contents API. The flags are what the question that was answered said, and GitHub's own answers are held
against them: the branch is made only with --create-branch (one commit with no parent that holds the page and an empty
.nojekyll), GitHub Pages is switched on only with --pages, and a private record is published only with --private-record.
Before anything is sent it checks that the page is the static one, that it holds nothing the project's privacy rules keep off
the record (a secret, a restricted pattern), that none of the user's holds stops publishing and that the account may write
there; it prints what will be public. It reads the file back and compares its hash. --dry-run and --list read and send nothing.

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
BRANCH_NOT_SAID = ("publish: the branch %s does not exist in %s, and the question that was answered did not say so. Run "
                   "acts.py ask publish again; nothing was sent.")
PRIVATE_NOT_SAID = ("publish: the record is private, and the question that was answered did not say so. Run acts.py ask publish "
                    "again; nothing was sent.")
NO_SITE = "GitHub Pages is not on for this branch; ask to set the public page up to get a site."
EMPTY_REPOSITORY = "publish: %s has no commit yet, so a branch cannot be made there; nothing was sent."
PAGES_REFUSED = ("publish: GitHub did not switch Pages on for %s (the account may lack the right, or the plan serves no Pages "
                 "from a private repository). The page is on the branch.")
SET_UP_FIRST = "publish: this is done on the user's answer; run acts.py ask publish and ask the question it prints."
MINUTE = "GitHub may take a minute to show the new version."
PAGES_ADDRESS = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9.-]{0,252}(?:/[A-Za-z0-9._-]{1,100})?/")
NOT_CONFIRMED = "publish: not confirmed; nothing was sent."
MISMATCH = "publish: the file on the branch is not the page that was sent; look at the branch."
NOT_SENT = "publish: GitHub did not take the page; nothing was published."
UNCONFIRMED = ("publish: GitHub did not confirm the page in time, so it may be on the branch; look at the branch before you run "
               "this again.")
NOTHING = "publish: nothing has been published from this machine."
STATIC = "Static snapshot."
INTERACTIVE = "Local interactive session."
SERVICE_ADDRESS = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}/[0-9a-f]{32}")


class Visible(html.parser.HTMLParser):
    """The page's text as a reader could meet it: the text of every element, with tags taken out and entities resolved, and
    every attribute value (a title, an alt, an aria-label, a value, a data attribute: anything the page keeps in markup); the
    style and the script are left out (they hold no text of the record; the page's own rules keep it out of them)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.attributes, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        self.attributes.extend(value for _, value in attrs if isinstance(value, str) and value)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def page_texts(page):
    """The texts the page is checked as, each by the privacy rules: the text of its elements joined with a line break (as a
    reader sees blocks), the same joined with nothing (so that a word split by an inline element, `SAMPLE-<mark>0042</mark>`, is
    whole again) and every attribute value."""
    parser = Visible()
    parser.feed(page)
    parser.close()
    return ["\n".join(parser.parts), "".join(parser.parts), "\n".join(parser.attributes)]


def visible_text(page):
    return page_texts(page)[0]


def source_problem(strings, config):
    """True when a string of the record's own text, before any markup is made of it, holds what the privacy rules keep off the
    record (a secret or a restricted pattern). Any failure of the check is a problem: it fails closed."""
    import observer_worker
    try:
        return any(observer_worker.privacy_problem(text, config) for text in strings)
    except Exception:  # noqa: BLE001 - a check that cannot be made is a stop, never a send
        return True


def page_problem(page, config):
    """True when the page's text, in any of the forms of page_texts, holds what the privacy rules keep off the record. Any
    failure of the check is a problem."""
    import observer_worker
    try:
        return any(observer_worker.privacy_problem(text, config) for text in page_texts(page))
    except Exception:  # noqa: BLE001
        return True


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


def pages_state(to, branch):
    """("on", address or None) when GitHub Pages serves this branch of `to` from its root, ("off", None) when there is no site,
    ("other", None) when it serves another branch or folder. The address is kept only when it is of the form of PAGES_ADDRESS;
    nothing else of GitHub's answer is kept. Raises what gh raises for a failure that is not a 404."""
    try:
        data = json.loads(recortex.gh(["api", "repos/%s/pages" % to]))
    except recortex.GhError as error:
        if missing(error):
            return "off", None
        raise
    source = data.get("source") if isinstance(data, dict) else None
    if not (isinstance(source, dict) and source.get("branch") == branch and source.get("path") == "/"):
        return "other", None
    address = data.get("html_url")
    return "on", address if isinstance(address, str) and PAGES_ADDRESS.fullmatch(address) else None


def create_branch_with_page(to, branch, path, data):
    """One commit with no parent that holds the page and an empty .nojekyll, made through GitHub's git data calls, and the branch
    that points at it. Raises recortex.GhError for a refusal; the caller says one fixed line."""
    def post(what, body):
        return json.loads(recortex.gh(["api", "-X", "POST", "repos/%s/git/%s" % (to, what), "--input", "-"],
                                      stdin=json.dumps(body), timeout=120))
    page = post("blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})["sha"]
    empty = post("blobs", {"content": "", "encoding": "utf-8"})["sha"]
    tree = post("trees", {"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": page},
                                   {"path": ".nojekyll", "mode": "100644", "type": "blob", "sha": empty}]})["sha"]
    commit = post("commits", {"message": MESSAGE, "tree": tree, "parents": []})["sha"]
    post("refs", {"ref": "refs/heads/%s" % branch, "sha": commit})


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
    base = {"at", "to", "branch", "path", "sha"}
    return [row for row in rows[-RECEIPTS_KEPT:] if isinstance(row, dict) and set(row) in (base, base | {"private"})
            and (row.get("private") is None or type(row.get("private")) is bool)
            and isinstance(row["to"], str) and recortex.valid_repo(row["to"]) and isinstance(row["branch"], str)
            and BRANCH.fullmatch(row["branch"]) and isinstance(row["path"], str) and PATH.fullmatch(row["path"])
            and isinstance(row["sha"], str) and re.fullmatch(r"[0-9a-f]{40}", row["sha"])
            and type(row["at"]) is int and 0 < row["at"] < 4102444800]


def keep_receipt(record, to, branch, path, sha, private=None):
    """Keep what was published: the newest 20 of {at, to, branch, path, sha, private}, through storage.py; `private` says whether
    the record was private (true), public (false) or unread (null), so that the question of the next publish can say it offline.
    False when it could not be written (one line is said; the publish stands)."""
    try:
        rows = read_receipts(record) + [{"at": int(time.time()), "to": to, "branch": branch, "path": path, "sha": sha,
                                         "private": private if type(private) is bool else None}]
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


def offer(config, record, path):
    """The lines that end `mind.py --open` on a machine with no browser: one question, built here with no network call. When a page
    was published from this machine before (the newest receipt), it is the question of `acts.py ask publish` for that target (the
    receipt says whether the record was private), whose yes is the grant for exactly that command. Else it is a question that
    does nothing by itself: where the page can go is checked, and asked once more, with the place named, before anything is
    sent. The second option shows the path of the built file. `path` is that file."""
    import acts
    rows = read_receipts(record)
    shown = "Nothing is sent. The file: %s" % path
    if rows:
        row = rows[-1]
        values = {"record": record.lower(), "to": row["to"].lower(), "branch": row["branch"], "path": row["path"], "create": False,
                  "pages": False, "private": row.get("private") is not False, "nosite": False}
        body = acts.describe(acts.ACTS["publish"], values, "choice")
    else:
        body = {"id": "publish", "header": "Public page", "form": "choice",
                "question": ("Put the recortex dashboard of %s on the web? No public page is set up from this machine yet, so "
                             "recortex first checks where it can go, and asks you once more, with the place named, before "
                             "anything is sent." % record),
                "options": [{"label": "Set up a public page", "description": "Checks GitHub and asks once more.", "answer": "yes",
                             "run": [acts.render(("acts.py", "ask", "publish"))]},
                            {"label": "Show the file's path", "description": shown, "answer": "no", "run": []}]}
    for item in body["options"]:
        if item["answer"] == "no":
            item["description"] = shown
    return acts.question_lines(body, bare_no=True)


def run(args):
    import observer
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
        # the record's own text is checked before any page is made from it
        page, counts = mind.public_text(config, record, allowed, refuse=lambda strings: source_problem(strings, config))
    except mind.PrivateError:
        print(NOT_PRIVATE)
        return 1
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
    if page_problem(page, config):
        print(NOT_PRIVATE)
        return 1
    try:
        target_private, may_push = read_repository(args.to)
        if not may_push:
            print(NO_PUSH % args.to)
            return 1
        branch_exists, sha = True, None
        try:
            recortex.gh(["api", "repos/%s/branches/%s" % (args.to, args.branch)])
        except recortex.GhError as error:
            if not missing(error):
                raise
            branch_exists = False
        if branch_exists:
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
        pages, _ = pages_state(args.to, args.branch)
        try:
            record_private = read_repository(record)[0]
        except (recortex.GhError, ValueError, TypeError):
            record_private = None   # a visibility that cannot be read counts as private
    except (recortex.GhError, ValueError, TypeError, KeyError):
        print(UNREADABLE_GITHUB)
        return 1
    switches = args.pages and pages == "off"
    kilobytes = (len(data) + 1023) // 1024
    for text in warning(counts, record, kilobytes, args.to, args.branch, args.path, sha is None, target_private is True,
                        record_private):
        print(text)
    if not branch_exists:
        print("It makes the branch %s there, holding only this page." % args.branch)
    if switches:
        print("It switches GitHub Pages on for that branch, which makes the branch a public website.")
    body = {"message": MESSAGE, "branch": args.branch, "content": base64.b64encode(data).decode("ascii")}
    if sha:
        body["sha"] = sha
    command = ["api", "-X", "PUT", "repos/%s/contents/%s" % (args.to, args.path), "--input", "-"]
    if args.dry_run:
        shown = dict(body, content="<%d bytes, sha256 %s>" % (len(data), hashlib.sha256(data).hexdigest()))
        print("would run: gh api repos/%s" % args.to)
        print("would run: gh api repos/%s/branches/%s" % (args.to, args.branch))
        if branch_exists:
            print("would run: gh api repos/%s/contents/%s?ref=%s" % (args.to, args.path, args.branch))
        print("would run: gh api repos/%s/pages" % args.to)
        print("would run: gh api repos/%s" % record)
        if branch_exists:
            print("would run: gh %s   # %s" % (" ".join(command), json.dumps(shown, sort_keys=True)))
        else:
            for what in ("blobs", "blobs", "trees", "commits", "refs"):
                print("would run: gh api -X POST repos/%s/git/%s --input -" % (args.to, what))
            print("   # one commit with no parent: %s and an empty .nojekyll, message %s" % (args.path, json.dumps(MESSAGE)))
        if switches:
            print("would run: gh api -X POST repos/%s/pages --input -" % args.to)
        print("dry run: nothing was sent")
        return 0
    # What GitHub says is held against the flags of the question that was answered, and never the flags against the user: a
    # private record that the question did not say was private, and a branch it did not say would be made, stop the publish.
    if record_private is not False and not args.private_record:
        print(PRIVATE_NOT_SAID)
        return 1
    if not branch_exists and not args.create_branch:
        print(BRANCH_NOT_SAID % (args.branch, args.to))
        return 1
    if observer.on_terminal():
        # the user's own terminal: a typed word, as it was
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
        import observer_grants
        import observer_runtime
        observer_grants.spend(observer_runtime.folder_key(), "publish")   # an answer nobody used is not kept for a later replay
    else:
        import acts
        values = {"record": record.lower(), "to": args.to.lower(), "branch": args.branch, "path": args.path,
                  "create": args.create_branch, "pages": args.pages, "private": args.private_record, "nosite": args.no_site}
        if not acts.allowed("publish", values):
            print(acts.refusal("publish", values)["note"])
            return 1
    try:
        if branch_exists:
            recortex.gh(command, stdin=json.dumps(body), timeout=120)
        else:
            create_branch_with_page(args.to, args.branch, args.path, data)
    except recortex.GhTimeout:   # the write may have landed after gh gave up waiting
        print(UNCONFIRMED)
        return 1
    except recortex.GhError as error:
        print(EMPTY_REPOSITORY % args.to if not branch_exists and ("409" in str(error) or "empty" in str(error).lower())
              else NOT_SENT)
        return 1
    except (ValueError, TypeError, KeyError):
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
    refused = False
    if switches:
        try:
            recortex.gh(["api", "-X", "POST", "repos/%s/pages" % args.to, "--input", "-"],
                        stdin=json.dumps({"source": {"branch": args.branch, "path": "/"}}), timeout=60)
        except recortex.GhError:   # never a failed publish: the page is on the branch
            refused = True
            print(PAGES_REFUSED % args.to)
    try:
        state, address = pages_state(args.to, args.branch)
    except (recortex.GhError, ValueError, TypeError):
        state, address = "other", None
    if state == "on" and address:
        print("published and read back.")
        print("the page: " + address + ("" if args.path == "index.html" else args.path))
        print(MINUTE)
    else:
        print("published and read back: https://github.com/%s/blob/%s/%s" % (args.to, args.branch, args.path))
        if state == "off" and not args.pages and not refused:
            print(NO_SITE)
    if not keep_receipt(record, args.to, args.branch, args.path, expected, record_private):
        print("publish: the receipt could not be kept on this machine; the page is published.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", metavar="OWNER/NAME", help="the repository that gets the page")
    parser.add_argument("--branch", metavar="BRANCH", help="its branch")
    parser.add_argument("--path", default="index.html", help="the file in that branch (default index.html)")
    parser.add_argument("--create-branch", action="store_true",
                        help="the question said the branch is made, holding only the page, when it does not exist")
    parser.add_argument("--pages", action="store_true",
                        help="the question said GitHub Pages is switched on for the branch when no site is on")
    parser.add_argument("--private-record", action="store_true",
                        help="the question said it makes a page public that comes from a private record")
    parser.add_argument("--no-site", action="store_true",
                        help="the question said GitHub Pages serves another branch, so the answer is the file's address")
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

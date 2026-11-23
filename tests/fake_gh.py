#!/usr/bin/env python3
"""A local stand-in for the gh CLI, used only by the tests.

It keeps a small repository in a JSON file (FAKE_GH_STATE) and answers the
subset of gh commands the scripts use, in the formats the real gh prints.
A comment's "edits" list follows GitHub: newest first, and for an edited
comment it ends with an entry for the creation of the comment. A comment that
was never edited has no "edits" (GitHub returns an empty history and a null
lastEditedAt for it).
`issue close [--reason completed|"not planned"]` and `issue reopen` change an issue's state and add the timeline event.
Fault switches:
  FAKE_GH_FAIL_ON="issue edit"   exit 1 for commands starting with these words
  FAKE_GH_FAIL_AFTER="issue create"  do the work, then exit 1 (a timeout after GitHub acted)
  FAKE_GH_FAIL_NTH="issue comment:3"  exit 1 for the 3rd call of that command in the state's log, and only for it
  FAKE_GH_CHANGE_ON_READ=2       change the issue body just before the 2nd REST read of an issue
  FAKE_GH_COMMENT_ORDER=reversed list comments newest first in GraphQL answers
  FAKE_GH_CORRUPT=1              store bodies with one character changed
  FAKE_GH_FAIL_ON_LABELS=1       exit 1 for an issue edit that adds or removes labels
  FAKE_GH_IGNORE_LABELS=1        accept label changes in an issue edit but do not apply them
  FAKE_GH_FAIL_ON_PAGE=2         exit 1 when page 2 of a listing is asked for
  FAKE_GH_SLEEP=5                sleep before answering
  FAKE_GH_RESUME_SLEEP=8         sleep before answering the hub comments query (the GraphQL call with a number)
  FAKE_GH_RESUME_FAIL=1          exit 1 for the hub comments query
  FAKE_GH_NODE_FAIL=1            exit 1 for a GraphQL query that reads comments by node id (the edit histories)
  FAKE_GH_NO_PARENT_FIELD=1      a query that asks for an issue's parent fails as on a GitHub without sub-issues
  FAKE_GH_PARENT_REPO=owner/name a parent is answered as an issue of that repository
  FAKE_GH_SIGNED_OUT=1           act as a gh that is not signed in: `auth status` exits 1 and every other command 4,
                                 with gh's own messages
  FAKE_GH_LOGIN=name             the login that `api user` answers and `auth status` names (default octocat); a test
                                 sets one that is not a GitHub login
  FAKE_GH_NOT_TEXT="auth status" that command (or "api user") answers with bytes that are not UTF-8, and exits 0
`auth status` answers as a gh signed in to github.com, and `api user` with the login.
A state "date" (YYYY-MM-DD) moves the stamps the fake gives to what it creates (default 2026-04-02 for issues and edits,
2026-04-01 for comments), so that a record whose own comments are dated later is still synced incrementally.
Git data, contents at a commit or a branch and pull requests (tests/fake_git.py answers them, and lists its
own fault switches).
Projects and sub-issues (tests/fake_projects.py answers them, and lists its own fault switches): a closed issue has
"closed_at", a merged pull request "merged_at", a sub-issue "parent" (the parent's number).
The repository itself (`api repos/<owner>/<name>`, no further path) answers from the state's "private" key, default
true: `--jq .private` prints true or false, and without --jq the object {"private": ...} is printed.
Edit histories come in pages of at most 100 entries, as GitHub's userContentEdits does: `first`, `after` and a
`pageInfo` with an opaque cursor are answered. A comment is found by its node id, "IC_kwDOfake<id>", which the REST
listing and the single-comment read give as `node_id`; a node id of a comment that does not exist answers the error
"Could not resolve to a node with the global id of '<id>'".
A state comment may set "node_id" (its node id), "database_id" (what the node answers as fullDatabaseId; databaseId
is null, with an error, above 2^31 - 1, as GraphQL's Int is), "cursor" (the endCursor it answers), "gone" (its node no
longer resolves) and "resolves" (how many more times its node resolves), to test hostile answers.
A comment of another repository is one whose state entry has a "repo" other than the repository in the request:
reading it by id answers "Not Found", as for an id that does not exist, and the listings leave it out. A comment
without "repo" belongs to every repository.
Files and commits (the mind page's pictures, mind_figures.py): `api repos/<owner>/<name>/contents/<path>?ref=<ref>`
answers from the state's "files", a map from "<ref>:<path>" to the file's bytes as base64, or to
{"base64": <the first bytes as base64>, "size": N} for a file that is N bytes long (the rest is zero bytes, so that a
test need not store megabytes). With the raw media type (`-H "Accept: application/vnd.github.raw"`) the bytes are
printed as they are; without it the JSON of the contents API (type, encoding, size, content). A key that is not in
"files" answers "Not Found". `api repos/<owner>/<name>/commits/<ref>` answers {"sha": ...}, or the bare sha with
`--jq .sha`, from the state's "commits" (a map from a tag or a short commit to whatever it answers), or a 40-digit
commit that is the ref of some key in "files" (it answers itself); any other ref answers "Not Found". The calls are in
the state's "log" like every other; FAKE_GH_FAIL_ON="api repos/octo-org/octo-repo/contents" fails the fetches alone,
and FAKE_GH_SLEEP slows every call.
Publishing a file (publish.py): `api -X PUT repos/<owner>/<name>/contents/<path> --input -` takes the JSON {"message", "branch",
"content" (base64), "sha"} on standard input and stores the file as state["files"]["<branch>:<path>"]. A branch that is not in
state["branches"] (default ["main"]) answers 404 "Branch not found"; a file that is there already is refused with 422 "\"sha\"
wasn't supplied" when no sha comes with it and with 409 when the sha is not the file's own (the git blob hash of its bytes); the
answer holds {"content": {"path", "sha"}, "commit": {"sha", "message"}}. What each write asked for is kept in state["puts"]
(path, branch, message, whether a sha was given, the bytes' sha). The contents answer (without the raw media type) gains "sha",
the blob hash of the file. `api repos/<owner>/<name>/branches/<name>` answers {"name": ...} for a branch in state["branches"] and
"Not Found" for another. The repository answer gains "permissions": {"push": ...} from the state's "push" (default true; false also
when FAKE_GH_GIT_NO_PUSH is 403 or 404). A state "repos" entry ("owner/name": {"private": ..., "push": ...}) answers for that
repository alone. FAKE_GH_PUT_FAIL=1 makes the write itself exit 1 after storing nothing; FAKE_GH_PUT_CORRUPT=1
stores the file with its last byte changed, as a GitHub that does not keep what it was sent.
GitHub Pages and the first publish (publish.py, 1.8.0; the shapes are GitHub's public REST API as it was read, and only a live
check can prove them): `api repos/<owner>/<name>/pages` answers 404 "Not Found" while the state has no "pages", and else
{"source": {"branch", "path"}, "html_url"} from state["pages"] ({"branch", "path", "html_url"}). `api -X POST
repos/<owner>/<name>/pages --input -` takes {"source": {"branch", "path"}} and sets state["pages"] with the html_url
https://<owner>.github.io/<name>/ (or the state's "pages_url"); a state "pages_refused" makes it answer 422. The four git data calls of
a branch made with one commit that has no parent: `-X POST .../git/blobs` ({"content", "encoding"}: base64 or utf-8) answers {"sha"}
(the git blob hash) and keeps the bytes in state["publish_blobs"]; `.../git/trees` (entries {"path", "mode", "type", "sha"} of kept
blobs), `.../git/commits` ({"message", "tree", "parents"}) and `.../git/refs` ({"ref": "refs/heads/<branch>", "sha"}) keep their
objects in state["publish_trees"] and state["publish_commits"]; a ref that is new adds the branch to state["branches"] and stores
the files of its tree as state["files"]["<branch>:<path>"], and a ref that exists answers 422 "Reference already exists". What each
made is kept in state["created_branches"] ({"branch", "parents", "message", "paths"}). A state "empty_repo" (true) makes the blob call
answer 409 "Git Repository is empty.", as GitHub does for a repository with no commit. These calls are not tests/fake_git.py's:
a tree of entries with a "sha", a commit of such a tree and a ref of such a commit are answered here.
"""

import base64
import hashlib
import io
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_git  # noqa: E402
import fake_projects  # noqa: E402
import graphql_check  # noqa: E402

ME = "octocat"


def load():
    with open(os.environ["FAKE_GH_STATE"], encoding="utf-8") as handle:
        return json.load(handle)


def save(state):
    with open(os.environ["FAKE_GH_STATE"], "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1)


def blob_sha(data):
    """The git blob hash of some bytes: what GitHub calls the sha of a file."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def option(args, name, default=None, many=False):
    values = [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]
    if many:
        return values
    return values[-1] if values else default


def body_from(args):
    path = option(args, "--body-file")
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    if os.environ.get("FAKE_GH_CORRUPT"):
        text = text.replace("e", "E", 1)
    return text.replace("\n", "\r\n")  # bodies can come back with CRLF line endings


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


def api_issue(state, repo, number):
    issue = state["issues"].get(str(number))
    if issue is None:
        sys.stdout.write('{"message":"Not Found","status":"404"}')
        fail("gh: Not Found (HTTP 404)")
    kind = "pull" if issue.get("pull_request") else "issues"
    out = {
        "number": int(number), "title": issue["title"], "state": issue.get("state", "open"),
        "body": issue["body"], "labels": [{"name": n} for n in issue.get("labels", [])],
        "user": {"login": issue.get("user", "octocat")},
        "created_at": issue.get("created_at", "2026-01-05T17:00:00Z"),
        "updated_at": issue.get("updated_at", "2026-01-06T18:30:00Z"), "closed_at": issue.get("closed_at"),
        "state_reason": issue.get("state_reason"),
        "html_url": "https://github.com/%s/%s/%s" % (repo, kind, number),
    }
    if issue.get("pull_request"):
        out["pull_request"] = {"url": "https://api.github.com/repos/%s/pulls/%s" % (repo, number),
                               "merged_at": issue.get("merged_at")}
    return out


def elsewhere(comment, repo):
    """True for a comment of another repository than `repo`."""
    return "repo" in comment and comment["repo"].lower() != repo.lower()


def node_id(cid, state=None):
    """The node id of comment `cid`; a state entry may set its own "node_id" (a hostile one, in a test)."""
    return ((state or {}).get("comments", {}).get(str(cid)) or {}).get("node_id") or "IC_kwDOfake%s" % cid


def cursor_for(offset):
    return base64.b64encode(("cursor:%d" % offset).encode()).decode()


def offset_of(cursor):
    return int(base64.b64decode(cursor.strip('"')).decode().split(":")[1])


def edit_connection(edits, arguments, selection, cursor=None):
    """The answer for userContentEdits(first:, after:) { totalCount pageInfo nodes } over `edits` (newest first)."""
    start = offset_of(arguments["after"]) if "after" in arguments else 0
    rows = edits[start:start + min(100, int(arguments.get("first", 100)))]
    end = start + len(rows)
    out = {}
    for alias, name, _, children in selection:
        if name == "totalCount":
            out[alias] = len(edits)
        elif name == "pageInfo":
            info = {"endCursor": cursor or cursor_for(end), "hasNextPage": end < len(edits)}
            out[alias] = {a: info[n] for a, n, _, _ in children}
        elif name == "nodes":
            out[alias] = [{a: ({"login": e["by"]} if n == "editor" and e["by"] else None if n == "editor" else e["at"])
                           for a, n, _, c in children} for e in rows]
    return out


def api_comment(state, repo, cid):
    comment = state["comments"].get(str(cid))
    if comment is None or elsewhere(comment, repo):  # GitHub scopes the route to the repository
        sys.stdout.write('{"message":"Not Found","status":"404"}')
        fail("gh: Not Found (HTTP 404)")
    return {
        "id": int(cid), "node_id": node_id(cid, state), "body": comment["body"], "user": {"login": comment.get("user", ME)},
        "created_at": comment.get("created_at", "2026-01-07T09:15:00Z"),
        "updated_at": comment.get("updated_at", comment.get("created_at", "2026-01-07T09:15:00Z")),
        "issue_url": "https://api.github.com/repos/%s/issues/%d" % (repo, comment["issue"]),
        "html_url": "https://github.com/%s/issues/%d#issuecomment-%s" % (repo, comment["issue"], cid),
    }


def page_of(path):
    match = re.search(r"[?&]page=(\d+)", path)
    return int(match.group(1)) if match else None


def one_page(rows, path):
    """The page a listing call asks for, as GitHub serves it; FAKE_GH_FAIL_ON_PAGE fails on one page."""
    page = page_of(path)
    if os.environ.get("FAKE_GH_FAIL_ON_PAGE") == str(page):
        fail("gh: simulated failure on page %d (HTTP 502)" % page)
    size = int((re.search(r"[?&]per_page=(\d+)", path) or [None, "30"])[1])
    return rows[(page - 1) * size:page * size]


def repo_of(fields):
    return "%s/%s" % (fields.get("owner", "octo"), fields.get("name", "demo"))


def publish_calls(args, path, state):
    """Pages and the four git data calls of a first publish (see the docstring). True when one was answered."""
    method = (args[args.index("-X") + 1] if "-X" in args else "GET").upper()
    repo = "/".join(path.split("/")[1:3])

    def refuse(status, message):
        sys.stdout.write(json.dumps({"message": message, "status": str(status)}))
        fail("gh: %s (HTTP %d)" % (message, status))

    if re.fullmatch(r"repos/[^/]+/[^/?]+/pages", path):
        if method == "GET":
            pages = state.get("pages")
            if not pages:
                refuse(404, "Not Found")
            print(json.dumps({"source": {"branch": pages["branch"], "path": pages["path"]}, "html_url": pages.get("html_url")}))
            return True
        if method == "POST":
            sent = json.loads(sys.stdin.read() or "{}")
            if state.get("pages_refused"):
                refuse(422, "Validation Failed")
            source = sent.get("source") or {}
            owner, name = repo.split("/")
            state["pages"] = {"branch": source.get("branch"), "path": source.get("path"),
                              "html_url": state.get("pages_url") or "https://%s.github.io/%s/" % (owner, name)}
            print(json.dumps({"source": source, "html_url": state["pages"]["html_url"]}))
            return True
        return False
    made = re.fullmatch(r"repos/[^/]+/[^/]+/git/(blobs|trees|commits|refs)", path)
    if not made or method != "POST":
        return False
    kind = made.group(1)
    raw = sys.stdin.read()
    sent = json.loads(raw or "{}")
    mine = {"blobs": True,
            "trees": isinstance(sent.get("tree"), list) and sent["tree"] and all(isinstance(e.get("sha"), str) for e in sent["tree"]),
            "commits": sent.get("tree") in state.get("publish_trees", {}),
            "refs": sent.get("sha") in state.get("publish_commits", {})}[kind]
    if not mine:
        sys.stdin = io.StringIO(raw)   # not a publish call: fake_git reads the same body
        return False
    if os.environ.get("FAKE_GH_GIT_NO_PUSH") in ("403", "404"):
        refuse(int(os.environ["FAKE_GH_GIT_NO_PUSH"]), "Resource not accessible by personal access token")
    if kind == "blobs":
        if state.get("empty_repo"):
            refuse(409, "Git Repository is empty.")
        data = base64.b64decode(sent.get("content", "")) if sent.get("encoding") == "base64" else str(sent.get("content", "")).encode("utf-8")
        sha = blob_sha(data)
        state.setdefault("publish_blobs", {})[sha] = base64.b64encode(data).decode("ascii")
        print(json.dumps({"sha": sha}))
    elif kind == "trees":
        entries = {}
        for entry in sent["tree"]:
            if entry["sha"] not in state.get("publish_blobs", {}) or not entry.get("path") or entry["path"].startswith("/"):
                refuse(422, "Validation Failed")
            entries[entry["path"]] = entry["sha"]
        sha = hashlib.sha1(("tree" + json.dumps(entries, sort_keys=True)).encode("utf-8")).hexdigest()
        state.setdefault("publish_trees", {})[sha] = entries
        print(json.dumps({"sha": sha}))
    elif kind == "commits":
        parents = list(sent.get("parents") or [])
        sha = hashlib.sha1(("commit" + sent["tree"] + json.dumps(parents) + str(sent.get("message"))).encode("utf-8")).hexdigest()
        state.setdefault("publish_commits", {})[sha] = {"tree": sent["tree"], "parents": parents, "message": sent.get("message")}
        print(json.dumps({"sha": sha}))
    else:
        match = re.fullmatch(r"refs/heads/([A-Za-z0-9._/-]+)", str(sent.get("ref", "")))
        if not match:
            refuse(422, "Validation Failed")
        branch = match.group(1)
        if branch in state.get("branches", ["main"]):
            refuse(422, "Reference already exists")
        commit = state["publish_commits"][sent["sha"]]
        tree = state["publish_trees"][commit["tree"]]
        state["branches"] = list(state.get("branches", ["main"])) + [branch]
        for where, blob in tree.items():
            state.setdefault("files", {})["%s:%s" % (branch, where)] = state["publish_blobs"][blob]
        state.setdefault("created_branches", []).append({"branch": branch, "parents": commit["parents"],
                                                         "message": commit["message"], "paths": sorted(tree)})
        print(json.dumps({"ref": sent["ref"], "object": {"sha": sent["sha"]}}))
    return True


def main():
    args = sys.argv[1:]
    if os.environ.get("FAKE_GH_SLEEP"):
        time.sleep(float(os.environ["FAKE_GH_SLEEP"]))
    state = load()
    state.setdefault("log", []).append(args)
    save(state)
    words = " ".join(args[:2])
    if os.environ.get("FAKE_GH_SIGNED_OUT"):  # what gh prints when no account is signed in
        if words == "auth status":
            fail("You are not logged into any GitHub hosts. To log in, run: gh auth login")
        fail("To get started with GitHub CLI, please run:  gh auth login\nAlternatively, populate the GH_TOKEN "
             "environment variable with a GitHub API authentication token.", 4)
    if os.environ.get("FAKE_GH_NOT_TEXT") == words:  # an answer that is not UTF-8
        sys.stdout.flush()
        sys.stdout.buffer.write(b"\xff\xfe signed in\n" if words == "auth status" else b"\xffocto\n")
        return
    fail_on = os.environ.get("FAKE_GH_FAIL_ON")
    if fail_on and words.startswith(fail_on):
        fail("gh: simulated failure (HTTP 502)")
    nth = os.environ.get("FAKE_GH_FAIL_NTH")
    if nth:
        command, _, count = nth.rpartition(":")
        if words == command and sum(1 for entry in state["log"] if " ".join(entry[:2]) == command) == int(count):
            fail("gh: simulated failure (HTTP 502)")
    repo = option(args, "--repo", "octo/demo")
    login = os.environ.get("FAKE_GH_LOGIN", ME)

    if words == "auth status":
        print("github.com\n  - Logged in to github.com account %s (keyring)\n  - Active account: true" % login)
    elif words == "label list":
        print(json.dumps(state["labels"]))
    elif words == "label create":
        state["labels"].append({"name": args[2], "color": option(args, "--color", ""),
                                "description": option(args, "--description", "")})
    elif words == "label edit":
        for label in state["labels"]:
            if label["name"] == args[2]:
                label["color"] = option(args, "--color", label["color"])
                label["description"] = option(args, "--description", label["description"])
    elif words == "issue create":
        number = state["next_issue"]
        state["next_issue"] += 1
        state["clock"] = state.get("clock", 0) + 1
        stamp = "%sT%02d:%02d:00Z" % (state.get("date", "2026-04-02"), *divmod(state["clock"], 60))
        state["issues"][str(number)] = {"title": option(args, "--title"), "body": body_from(args),
                                        "labels": option(args, "--label", many=True),
                                        "created_at": stamp, "updated_at": stamp}
        print("https://github.com/%s/issues/%d" % (repo, number))
    elif words == "issue edit":
        if args[2] not in state["issues"]:
            fail("gh: Not Found (HTTP 404)")
        issue = state["issues"][args[2]]
        changes_labels = "--add-label" in args or "--remove-label" in args
        if changes_labels and os.environ.get("FAKE_GH_FAIL_ON_LABELS"):
            fail("gh: simulated failure while changing labels (HTTP 502)")
        if changes_labels and os.environ.get("FAKE_GH_IGNORE_LABELS"):
            args = [a for i, a in enumerate(args) if a not in ("--add-label", "--remove-label")
                    and (i == 0 or args[i - 1] not in ("--add-label", "--remove-label"))]
        names = {l["name"].lower() for l in state["labels"]}
        for label in option(args, "--add-label", many=True):
            if label.lower() not in names:
                fail("could not add label: '%s' not found" % label)
        if "--body-file" in args:
            issue["body"] = body_from(args)
            state["clock"] = state.get("clock", 0) + 1
            stamp = "%sT%02d:%02d:00Z" % (state.get("date", "2026-04-02"), *divmod(state["clock"], 60))
            if not issue.get("edits"):
                issue["edits"] = [{"at": issue.get("created_at", "2026-01-05T17:00:00Z"), "by": issue.get("user", ME)}]
            issue["edits"].insert(0, {"at": stamp, "by": ME})
            issue["last_edited_at"] = issue["updated_at"] = stamp
        for label in option(args, "--add-label", many=True):
            if label not in issue.setdefault("labels", []):
                issue["labels"].append(label)
        removed = option(args, "--remove-label", many=True)
        issue["labels"] = [l for l in issue.get("labels", []) if l not in removed]
        print("https://github.com/%s/issues/%s" % (repo, args[2]))
    elif words == "issue comment":
        if args[2] not in state["issues"]:
            fail("gh: Not Found (HTTP 404)")
        cid = state["next_comment"]
        state["next_comment"] += 1
        state["comments"][str(cid)] = {"issue": int(args[2]), "body": body_from(args),
                                       "created_at": "%sT%02d:%02d:00Z" % (state.get("date", "2026-04-01"), *divmod(cid - 9100, 60))}
        state["issues"][args[2]]["updated_at"] = state["comments"][str(cid)]["created_at"]
        print("https://github.com/%s/issues/%s#issuecomment-%d" % (repo, args[2], cid))
    elif words in ("issue close", "issue reopen"):
        if args[2] not in state["issues"]:
            fail("gh: Not Found (HTTP 404)")
        issue = state["issues"][args[2]]
        state["clock"] = state.get("clock", 0) + 1
        stamp = "%sT%02d:%02d:00Z" % (state.get("date", "2026-04-02"), *divmod(state["clock"], 60))
        if words == "issue close":
            reason = option(args, "--reason", "completed").replace(" ", "_")
            issue.update(state="closed", closed_at=stamp, updated_at=stamp, state_reason=reason)
            issue.setdefault("timeline", []).append({"event": "closed", "created_at": stamp, "state_reason": reason,
                                                     "actor": {"login": ME}})
        else:
            issue.update(state="open", closed_at=None, updated_at=stamp, state_reason="reopened")
            issue.setdefault("timeline", []).append({"event": "reopened", "created_at": stamp, "actor": {"login": ME}})
        print("https://github.com/%s/issues/%s" % (repo, args[2]))
    elif words == "issue pin":
        state["issues"][args[2]]["pinned"] = True
    elif words == "issue view":
        print(json.dumps({"isPinned": bool(state["issues"][args[2]].get("pinned"))}))
    elif words == "issue list":
        label, author = option(args, "--label"), option(args, "--author")
        rows = []
        for n, i in sorted(state["issues"].items(), key=lambda kv: -int(kv[0])):
            if label and (label not in i.get("labels", []) or i.get("state", "open") != "open"):
                continue
            if author == "@me" and i.get("user", ME) != ME:
                continue
            rows.append({"number": int(n), "title": i["title"], "body": i["body"],
                         "url": "https://github.com/%s/issues/%s" % (repo, n),
                         "labels": [{"name": name} for name in i.get("labels", [])]})
        print(json.dumps(rows))
    elif args[:2] == ["api", "graphql"]:
        fields = dict(a.split("=", 1) for a in option(args, "-f", many=True) + option(args, "-F", many=True))
        problems = graphql_check.check(fields["query"])
        if os.environ.get("FAKE_GH_NO_PARENT_FIELD") and " parent {" in fields["query"]:  # a GitHub without sub-issues
            problems.append("Field 'parent' doesn't exist on type 'Issue'")
        if problems:  # the real API rejects the whole query
            print(json.dumps({"errors": [{"message": m} for m in problems]}))
            fail("gh: " + problems[0])
        if fake_projects.wanted(fields["query"]):  # Projects, sub-issues, and the repository's ids
            try:
                response = fake_projects.handle(state, state.get("repo", fake_projects.DEFAULT_REPO), fields["query"])
            except fake_projects.Refused as refusal:
                print(json.dumps({"errors": [{"message": str(refusal)}]}))
                save(state)
                fail("gh: " + str(refusal), refusal.code)
            print(json.dumps(response))
            after = state.pop("_fail_after", None)
            save(state)
            if after:
                fail("gh: " + after)
            return
        top = graphql_check.parse_query(fields["query"])
        if top and top[0][1] == "node":  # comments named by node id, for their edit histories
            if os.environ.get("FAKE_GH_NODE_FAIL"):
                fail("gh: simulated failure of the edit history query (HTTP 502)")
            out, errors = {}, []
            for alias, name, arguments, children in top:
                wanted = arguments["id"].strip('"')
                found = [c for c in state["comments"] if node_id(c, state) == wanted and not state["comments"][c].get("gone")]
                if found and "resolves" in state["comments"][found[0]]:  # a test: it resolves this many more times
                    state["comments"][found[0]]["resolves"] -= 1
                    found = found if state["comments"][found[0]]["resolves"] >= 0 else []
                if not found:
                    out[alias] = None
                    errors.append({"type": "NOT_FOUND", "path": [alias], "message":
                                   "Could not resolve to a node with the global id of '%s'." % wanted})
                    continue
                comment = state["comments"][found[0]]
                edits = comment.get("edits", [])
                newest = edits[0] if edits else None
                database_id = int(comment.get("database_id", int(found[0])))
                values = {"databaseId": database_id if database_id < 2 ** 31 else None, "fullDatabaseId": str(database_id),
                          "lastEditedAt": comment.get("last_edited_at", newest["at"] if newest else None)}
                if database_id >= 2 ** 31 and any(n == "databaseId" for _, n, _, _ in children[0][3]):
                    errors.append({"path": [alias, "databaseId"], "message":  # GraphQL's Int holds 32 bits only
                                   "Int cannot represent non 32-bit signed integer value: %d" % database_id})
                out[alias] = {}
                for a, n, arguments_of, kids in children[0][3]:
                    out[alias][a] = edit_connection(edits, arguments_of, kids, comment.get("cursor")) \
                        if n == "userContentEdits" else values[n]
            print(json.dumps(dict({"data": out}, **({"errors": errors} if errors else {}))))
            save(state)
            if errors:
                fail("gh: " + errors[0]["message"])
            return
        if "number" not in fields:  # issues named in the query by aliases, for their body edits
            out, errors = {}, []
            for alias, name, arguments, children in top[0][3]:
                issue = state["issues"].get(arguments.get("number"))
                if issue is None or (name == "issue" and issue.get("pull_request")):
                    out[alias] = None  # GitHub: "Could not resolve to an Issue with the number of N."
                    errors.append({"type": "NOT_FOUND", "path": ["repository", alias], "message":
                                   "Could not resolve to an Issue with the number of %s." % arguments.get("number")})
                    continue
                edits = issue.get("edits", [])
                out[alias] = {}
                for a, n, arguments_of, kids in children:
                    if n == "userContentEdits":
                        out[alias][a] = edit_connection(edits, arguments_of, kids)
                    elif n == "parent":  # sub-issues: the parent, in the same repository
                        top = graphql_check.parse_query(fields["query"])[0][2]
                        where = os.environ.get("FAKE_GH_PARENT_REPO") or "%s/%s" % (graphql_check.literal(top["owner"]),
                                                                               graphql_check.literal(top["name"]))
                        out[alias][a] = None if issue.get("parent") is None else {
                            "number": issue["parent"], "repository": {"nameWithOwner": where}}
                    else:
                        out[alias][a] = issue.get("last_edited_at")
            print(json.dumps(dict({"data": {"repository": out}}, **({"errors": errors} if errors else {}))))
            save(state)
            if errors:
                fail("gh: " + errors[0]["message"])
            return
        if os.environ.get("FAKE_GH_RESUME_SLEEP"):  # the hub comments query, read for the resume block
            time.sleep(float(os.environ["FAKE_GH_RESUME_SLEEP"]))
        if os.environ.get("FAKE_GH_RESUME_FAIL"):
            fail("gh: simulated failure of the hub comments query (HTTP 502)")
        issue = state["issues"].get(fields["number"])
        if issue is None:
            print(json.dumps({"data": {"repository": {"issue": None}}}))
        else:
            person = lambda name: {"login": name} if name else None

            def comment_node(cid, comment, selection):
                edits = comment.get("edits", [])
                newest = edits[0] if edits else None  # newest first, as GitHub lists them
                values = {
                    "id": "IC_%s" % cid,
                    "url": "https://github.com/%s/issues/%s#issuecomment-%s" % (repo_of(fields), comment["issue"], cid),
                    "createdAt": comment.get("created_at", "2026-01-07T09:15:00Z"),
                    "body": comment["body"],
                    "author": person(comment.get("user", ME)),
                    "editor": person(comment.get("editor", newest["by"] if newest else None)),
                    "lastEditedAt": comment.get("last_edited_at", newest["at"] if newest else None),
                }
                out = {}
                for alias, name, arguments, children in selection:
                    if name == "userContentEdits":
                        rows = edits[:int(arguments["first"])] if "first" in arguments else edits[-int(arguments["last"]):]
                        out[alias] = {"totalCount": comment.get("edit_total", len(edits)),
                                      "nodes": [{"editedAt": e["at"], "editor": person(e["by"])} for e in rows]}
                    else:
                        out[alias] = values[name]
                return out

            out = {}
            for alias, name, arguments, children in graphql_check.parse_query(fields["query"])[0][3][0][3]:
                if name == "comments":
                    mine = [(c, state["comments"][c]) for c in sorted(state["comments"], key=int)
                            if state["comments"][c]["issue"] == int(fields["number"])
                            and not elsewhere(state["comments"][c], repo_of(fields))]
                    if os.environ.get("FAKE_GH_COMMENT_ORDER") == "reversed":
                        mine.reverse()
                    rows = mine[-int(arguments["last"]):] if "last" in arguments else mine[:int(arguments["first"])]
                    nodes_selection = [c for c in children if c[1] == "nodes"]
                    out[alias] = {"totalCount": len(mine)}
                    if nodes_selection:
                        out[alias]["nodes"] = [comment_node(c, body, nodes_selection[0][3]) for c, body in rows]
            print(json.dumps({"data": {"repository": {"issue": out}}}))
    elif args[0] == "api":
        if "user" in args[1:]:
            print(login if option(args, "--jq") == ".login" else json.dumps({"login": login}))
            return
        path = [a for a in args[1:] if a.startswith("repos/")][0]
        if publish_calls(args, path, state):
            save(state)
            return
        try:
            if fake_git.handle(args, path, state, path.split("/")[1] + "/" + path.split("/")[2], login):
                save(state)
                return
        except fake_git.Refused as refusal:
            save(state)
            sys.stdout.write(json.dumps({"message": refusal.message, "status": str(refusal.status)}))
            fail("gh: %s (HTTP %d)" % (refusal.message, refusal.status))
        jq = option(args, "--jq")
        if re.fullmatch(r"repos/[^/]+/[^/?]+", path):
            about = state.get("repos", {}).get(path.split("/", 1)[1], {})   # one repository's own "private" and "push"
            private = bool(about.get("private", state.get("private", True)))
            push = bool(about.get("push", state.get("push", True))) and os.environ.get("FAKE_GH_GIT_NO_PUSH") not in ("403", "404")
            print(("true" if private else "false") if jq == ".private" else
                  json.dumps({"private": private, "permissions": {"push": push}}))
            return
        branch = re.fullmatch(r"repos/[^/]+/[^/]+/branches/([^?]+)", path)
        if branch:
            if branch.group(1) not in state.get("branches", ["main"]):
                sys.stdout.write('{"message":"Branch not found","status":"404"}')
                fail("gh: Branch not found (HTTP 404)")
            print(json.dumps({"name": branch.group(1)}))
            return
        put = re.fullmatch(r"repos/[^/]+/[^/]+/contents/([^?]+)", path)
        if put and "-X" in args and option(args, "-X") == "PUT":
            sent = json.loads(sys.stdin.read() or "{}")
            name, where = str(sent.get("branch", "")), put.group(1)
            if name not in state.get("branches", ["main"]):
                sys.stdout.write('{"message":"Branch %s not found","status":"404"}' % name)
                fail("gh: Branch not found (HTTP 404)")
            data = base64.b64decode(sent.get("content", ""))
            key = "%s:%s" % (name, where)
            old = state.get("files", {}).get(key)
            if old is not None:
                held = base64.b64decode(old["base64"] if isinstance(old, dict) else old)
                if not sent.get("sha"):
                    sys.stdout.write('{"message":"Invalid request. \\"sha\\" wasn\'t supplied.","status":"422"}')
                    fail("gh: Invalid request. \"sha\" wasn't supplied. (HTTP 422)")
                if sent["sha"] != blob_sha(held):
                    sys.stdout.write('{"message":"%s does not match","status":"409"}' % where)
                    fail("gh: %s does not match (HTTP 409)" % where)
            if os.environ.get("FAKE_GH_PUT_FAIL"):
                fail("gh: simulated failure (HTTP 502)")
            stored = data[:-1] + bytes([data[-1] ^ 1]) if os.environ.get("FAKE_GH_PUT_CORRUPT") and data else data
            state.setdefault("files", {})[key] = base64.b64encode(stored).decode("ascii")
            state.setdefault("puts", []).append({"path": where, "branch": name, "message": sent.get("message"),
                                                 "sha_given": bool(sent.get("sha")), "sha": blob_sha(data)})
            print(json.dumps({"content": {"path": where, "sha": blob_sha(stored)},
                              "commit": {"sha": blob_sha(b"commit" + data), "message": sent.get("message")}}))
            save(state)
            return
        match = re.fullmatch(r"repos/([^/]+/[^/]+)/issues/comments/(\d+)", path)
        listing = re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)/comments(\?.*)?", path)
        timeline = re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)/timeline(\?.*)?", path)
        since = (re.search(r"[?&]since=([^&]+)", path) or [None, None])[1]
        if timeline:
            sys.stdout.write(json.dumps(state["issues"].get(timeline.group(2), {}).get("timeline", [])))
            save(state)
            return
        if listing:
            rows = [api_comment(state, listing.group(1), c) for c in sorted(state["comments"], key=int)
                    if state["comments"][c]["issue"] == int(listing.group(2))
                    and not elsewhere(state["comments"][c], listing.group(1))]
            sys.stdout.write(json.dumps(rows))
            save(state)
            return
        if match:
            out = api_comment(state, match.group(1), match.group(2))
        elif re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)", path):
            match = re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)", path)
            state["reads"] = state.get("reads", 0) + 1
            if str(state["reads"]) == os.environ.get("FAKE_GH_CHANGE_ON_READ"):
                state["issues"][match.group(2)]["body"] += "\nEdited by someone else meanwhile."
            out = api_issue(state, match.group(1), match.group(2))
            if jq == ".number":
                print(out["number"])
                save(state)
                return
        elif re.fullmatch(r"repos/([^/]+/[^/]+)/issues/comments\?.*", path):
            api_repo = path.split("/issues")[0][6:]
            rows = [api_comment(state, api_repo, c) for c in sorted(state["comments"], key=int)
                    if not elsewhere(state["comments"][c], api_repo)]
            rows = [r for r in rows if not since or r["updated_at"] >= since]
            rows.sort(key=lambda r: (r["updated_at"], r["id"]))
            if page_of(path):
                sys.stdout.write(json.dumps(one_page(rows, path)))
                save(state)
                return
            # the real gh prints one JSON array per page, back to back
            sys.stdout.write(json.dumps(rows[:1]) + json.dumps(rows[1:]))
            save(state)
            return
        elif re.fullmatch(r"repos/([^/]+/[^/]+)/issues\?.*", path):
            api_repo = path.split("/issues")[0][6:]
            rows = [api_issue(state, api_repo, n) for n in sorted(state["issues"], key=int)]
            rows = [r for r in rows if not since or r["updated_at"] >= since]
            if page_of(path):
                rows.sort(key=lambda r: (r["updated_at"], r["number"]))
                sys.stdout.write(json.dumps(one_page(rows, path)))
                save(state)
                return
            sys.stdout.write(json.dumps(rows[:2]) + json.dumps(rows[2:]))
            save(state)
            return
        elif re.fullmatch(r"repos/[^/]+/[^/]+/commits/[^/?]+", path):
            ref = path.rsplit("/", 1)[1]
            sha = state.get("commits", {}).get(ref)
            if sha is None and re.fullmatch(r"[0-9a-f]{40}", ref) and any(
                    key.split(":", 1)[0] == ref for key in state.get("files", {})):
                sha = ref
            if sha is None:
                sys.stdout.write('{"message":"Not Found","status":"404"}')
                fail("gh: Not Found (HTTP 404)")
            print(sha if jq == ".sha" else json.dumps({"sha": sha}))
            return
        elif re.fullmatch(r"repos/[^/]+/[^/]+/contents/[^?]+\?ref=[^&]+", path):
            filepath, ref = re.fullmatch(r"repos/[^/]+/[^/]+/contents/([^?]+)\?ref=([^&]+)", path).groups()
            entry = state.get("files", {}).get("%s:%s" % (ref, filepath))
            if entry is None:
                sys.stdout.write('{"message":"Not Found","status":"404"}')
                fail("gh: Not Found (HTTP 404)")
            data = base64.b64decode(entry["base64"] if isinstance(entry, dict) else entry)
            if isinstance(entry, dict) and entry.get("size", 0) > len(data):
                data += b"\0" * (entry["size"] - len(data))
            if any("vnd.github.raw" in value for value in option(args, "-H", many=True)):
                sys.stdout.flush()
                sys.stdout.buffer.write(data)
            else:
                print(json.dumps({"type": "file", "encoding": "base64", "size": len(data), "sha": blob_sha(data),
                                  "content": base64.b64encode(data).decode("ascii")}))
            return
        else:
            fail("fake gh: unsupported api path %s" % path, 3)
        print(json.dumps(out))
    else:
        fail("fake gh: unsupported command %s" % words, 3)
    save(state)
    fail_after = os.environ.get("FAKE_GH_FAIL_AFTER")
    if fail_after and words.startswith(fail_after):
        fail("gh: simulated timeout after the request was carried out")


if __name__ == "__main__":
    main()

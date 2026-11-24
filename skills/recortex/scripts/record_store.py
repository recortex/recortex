"""The record's interface: what a script asks of the record, whether it lives in GitHub issues or in a local folder.

`for_repo(repo)` gives the store of a record. `GithubStore` runs `gh` and builds each argument list exactly as the scripts built
it before there was a second store; `LocalStore` (local_store.py) keeps the same record as Markdown files. A script asks the store
for an operation (`read_issue`, `post_entry`, ...) and never builds a `gh` argument list of its own: `recortex.gh(` stands only
here and in recortex.py, where it is defined (a test scans for that). The raw call `store.github(args)` is for the features that
only GitHub has (the board, Pages, the versions branch) and for the GitHub sync; a local store refuses it.

This module imports recortex and names. A local store is imported only when one is opened. recortex.py imports this module inside
its functions, never at the top.

Standard library only."""

import json
import os
import re
import subprocess
import urllib.parse

import names
import recortex

LOCAL_OWNER = "local--record"
RECORD_ID = re.compile(r"[a-z2-7]{16}")                    # used with fullmatch
LOCAL_NAME = re.compile(r"local--record/([a-z2-7]{16})")   # used with fullmatch
KINDS = ("github", "local")
FEATURES = ("github-api", "board", "sub-issues", "triage-workflow", "pages", "pinning", "label-catalog",
            "pull-requests", "versions-branch", "figure-pictures", "visibility")
FIELD_SHAPES = ("mine", "active", "plan", "dated")

NOT_AGREE = "recortex: the project file's record name and its kind of record do not agree; nothing was read."
UNKNOWN_KIND = "recortex: the project file names a kind of record this version does not know."
ONLY_ITS_OWN = ("recortex: this project keeps a local record, so a record named another way is refused; nothing was "
                "read or written.")
NO_SERVER = "recortex: a local record makes no call to a server."
NO_LOCAL = "recortex: a local record cannot be opened here; nothing was read."


class StoreError(recortex.GhError):
    """Any refusal or failure of a store; a GhError, so that every `except recortex.GhError` keeps catching. `kind` is a
    fixed word for the reason (hooks print one fixed sentence per kind)."""

    def __init__(self, message, kind="store"):
        super().__init__(message)
        self.kind = kind


class RecordFolderError(StoreError):
    """The record folder cannot be used: it is missing or moved, a check of storage.py failed, or record.json does not pass."""


class Missing(StoreError):
    """The topic or entry does not exist."""


class Unsupported(StoreError):
    """A GitHub-only call on a local store; str() is the fixed line of OFF."""


class ConfigRefused(SystemExit):
    """The project file's kind of record cannot be used: one fixed line, and nothing was read or written."""


# What each store calls things. The GitHub row holds the sentences the scripts printed before there was a local store, so
# that a format string built from it prints the same line byte for byte; the local row is in local_words (loaded with the
# local store).
WORDS = {
    "github": {
        "name": "GitHub",
        "place": "the GitHub issues of %s",
        "unread": "GitHub could not be read",
        "hidden": "%d memories by accounts outside your allowed list are not listed.",
        "changed": "%d memories edited after they were posted and are not listed.",
        "on": "on GitHub",
        "read_from": "GitHub",
        "changed_mark": "  [changed on GitHub after it was posted, by allowed accounts only]",
        "page_hidden": "%d memories by accounts outside your allowed list are not told here.",
        "page_changed": "%d memories were changed on GitHub after they were posted, so they are not told here.",
        "dash_hidden": "by other accounts",
        "dash_changed": "edited",
        "problem_author": "written by a non-allowed account",
        "problem_edited": "edited after posting",
        "sync_failed": "GitHub could not be read",
    },
    "local": {
        "name": "the local record",
        "place": "the local record of this project",
        "unread": "the record folder could not be read",
        "hidden": "%d memories written on another machine are not listed.",
        "changed": "%d memories changed outside recortex are not listed.",
        "on": "in the local record",
        "read_from": "the local record",
        "changed_mark": "",
        "page_hidden": "%d memories written on another machine are not told here.",
        "page_changed": "%d memories were changed outside recortex, so they are not told here.",
        "dash_hidden": "written on another machine",
        "dash_changed": "changed outside recortex",
        "problem_author": "written on another machine",
        "problem_edited": "changed outside recortex",
        "sync_failed": "the record folder could not be read",
    },
}

# The line a GitHub-only feature prints on a local record (10.2). A feature with no line of its own has None.
OFF = {
    "board": "recortex: the board is on GitHub Projects, and this project keeps a local record. The dashboard shows the same "
             "work.",
    "sub-issues": "recortex: sub-issue links are a GitHub feature. In a local record a topic lists its tasks in its text.",
    "triage-workflow": "recortex: the triage workflow labels issues that other people open on GitHub. A local record has none.",
    "pages": "recortex: publishing to GitHub Pages needs a repository on GitHub. For a local record the page is saved as a file.",
    "versions-branch": "recortex: the manuscript's versions branch is kept on GitHub. A local record keeps every version of a "
                       "topic's text in the topic's folder.",
    "pull-requests": "recortex: approval by pull request is a GitHub feature. In a local record a change of the text is "
                     "approved in the chat.",
    "figure-pictures": None,
    "label-catalog": "labels: a local record keeps each topic's labels in the topic; nothing to create.",
    "pinning": None,
    "visibility": "notice: this record is inside the project; if the project's repository is public, anyone can read every "
                  "entry in it.",
    "github-api": None,
}
SNAPSHOT_OFF = "recortex: a local record is Markdown files already; there is nothing to save."


def is_local(repo):
    """True when `repo` is the name of a local record: a name, not a decision (for_repo decides)."""
    return isinstance(repo, str) and LOCAL_NAME.fullmatch(repo) is not None


def kind_of(config):
    """"github" or "local" for a project file's configuration, or a refusal: the `store` key and the name must agree both
    ways, because no name is provably free forever, so the name is never the proof."""
    store = config.get("store") if isinstance(config, dict) else None
    repo = config.get("repo") if isinstance(config, dict) else None
    if store not in (None, "github", "local"):
        raise ConfigRefused(UNKNOWN_KIND)
    named_local = isinstance(repo, str) and repo.split("/", 1)[0].lower() == LOCAL_OWNER
    if store == "local":
        if is_local(repo):
            return "local"
        raise ConfigRefused(NOT_AGREE)
    if named_local:
        raise ConfigRefused(NOT_AGREE)
    return "github"


def github_account_state(timeout):
    """Whether gh can be run and is signed in to github.com: (state, login), as recortex.gh_account describes them."""
    try:
        recortex.gh(["auth", "status", "--hostname", "github.com"], timeout=timeout, errors="replace")
    except (recortex.GhMissing, ValueError):  # ValueError: a RECORTEX_GH that does not split into a command
        return "missing", None
    except recortex.GhTimeout:
        return "slow", None
    except recortex.GhError:
        return "signed-out", None
    try:
        # the host named, so that GH_HOST cannot give the login of another host
        login = recortex.gh(["api", "user", "--hostname", "github.com", "--jq", ".login"], timeout=timeout,
                            errors="replace").strip()
    except recortex.GhTimeout:
        return "slow", None
    except recortex.GhError:
        return "unnamed", None
    return ("ready", login) if recortex.LOGIN.fullmatch(login) else ("unnamed", None)


# ---------------------------------------------------------------- what is known of a project's repository (setup, 9.3)

REMOTE_SCHEMES = ("https", "http", "ssh", "git", "git+ssh", "ssh+git")
REMOTE_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REMOTE_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
SCP_REMOTE = re.compile(r"(?:[A-Za-z0-9._-]{1,40}@)?github\.com:([^/:@\s]+)/([^/:@\s]+)")
REMOTE_LIMIT = 8   # the repositories that GitHub is asked about: more than this are not looked at


def github_repo_of(value):
    """`owner/name` when a git remote URL names a repository on github.com, by an exact check of its host, else None. The URL
    forms are https://, http://, ssh://, git:// (with an optional user and port) and the scp form git@github.com:owner/name. A
    host that only holds the word (`evil.example/github.com/...`, `github.com.evil.example`, `evilgithub.com`) is not GitHub."""
    if not isinstance(value, str) or len(value) > 500 or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
        return None
    if "://" in value:
        try:
            parts = urllib.parse.urlsplit(value)
            host = parts.hostname
            parts.port   # a port that is not a number raises here
        except ValueError:
            return None
        if parts.scheme.lower() not in REMOTE_SCHEMES or host != "github.com" or parts.query or parts.fragment:
            return None
        owner, _, name = parts.path.strip("/").partition("/")
    else:
        found = SCP_REMOTE.fullmatch(value)
        if not found:
            return None
        owner, name = found.groups()
    name = name[:-1] if name.endswith("/") else name
    name = name[:-4] if name.endswith(".git") else name
    if not REMOTE_OWNER.fullmatch(owner) or not REMOTE_NAME.fullmatch(name) or name in (".", ".."):
        return None
    return "%s/%s" % (owner, name)


def project_remotes(root):
    """The values of every `remote.<name>.url` and `remote.<name>.pushurl` of the checkout that holds `root`, read locally."""
    try:
        done = subprocess.run(["git", "-C", root, "config", "--null", "--get-regexp", r"^remote\..*\.(url|pushurl)$"],
                              capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    values = []
    for row in done.stdout.split("\0"):
        key, _, value = row.partition("\n")
        if key and value:
            values.append(value)
    return values


def project_visibility(root):
    """"public", "private" or "unknown" for the repository of the project at `root`: its remotes are read locally, each by an
    exact check of the host; GitHub is asked about each repository it names (at most REMOTE_LIMIT) only when gh is ready. "public"
    when any one is public; "private" only when every remote is a GitHub repository confirmed private; else "unknown"."""
    repos = [github_repo_of(value) for value in project_remotes(root)]
    named = sorted({repo for repo in repos if repo})
    if not named or recortex.gh_account()[0] != "ready":
        return "unknown"
    seen = {}
    for repo in named[:REMOTE_LIMIT]:
        answer = GithubStore(repo).visibility()
        seen[repo] = {"false": "public", "true": "private"}.get(answer, "unknown")
        if seen[repo] == "public":
            return "public"
    if None not in repos and len(named) <= REMOTE_LIMIT and all(state == "private" for state in seen.values()):
        return "private"
    return "unknown"


def words_for(repo):
    """The words of the store that holds the record named `repo`, from its name alone (it opens nothing, so a hook can use it)."""
    return WORDS["local" if is_local(repo) else "github"]


def place_of(repo):
    """Where the record is, in the words of a payload: the GitHub issues of a repository, or the local record of the project."""
    words = words_for(repo)
    return words["place"] % repo if "%s" in words["place"] else words["place"]


def count_line(template, count):
    """A fixed count line from WORDS, with the singular form for one (the plural form's "memories" and "are" are the template's)."""
    if count == 1:
        template = (template.replace("%d memories", "1 memory").replace(" are not", " is not").replace(" were ", " was ")
                    .replace("so they are", "so it is"))
        return template
    return template % count


def address_of(repo, issue, entry=None):
    """The address of a topic (`entry` None) or of an entry in the text of the record, from checked integers: the GitHub
    address for a GitHub name, `#N` or `#N/ID` for a local one. A pure function of the name: it opens nothing, so a hook
    can use it."""
    if is_local(repo):
        return "#%d" % issue if entry is None else "#%d/%d" % (issue, entry)
    base = "https://github.com/%s/issues/%d" % (repo, issue)
    return base if entry is None else "%s#issuecomment-%d" % (base, entry)


def safe_address_of(repo, text):
    """`text` when it is a whole address of the record's kind that may be printed (GitHub: letters, digits and ./#- after
    github.com/; local: `#N` or `#N/ID`), else None. ASCII only."""
    if not isinstance(text, str):
        return None
    pattern = r"#[0-9]{1,9}(?:/[0-9]{10,19})?" if is_local(repo) else r"https://github\.com/[\w./#-]+"
    return text if re.fullmatch(pattern, text, re.ASCII) else None


def api_issue_of(repo, issue):
    """The address of a topic as an entry's `issue_url` names it (what the stores' reads carry)."""
    return ("local:%s/issues/%d" if is_local(repo) else "https://api.github.com/repos/%s/issues/%d") % (repo, issue)


class GithubStore:
    """The record in GitHub issues. Every method builds the argument list the call site built before, and passes it to
    recortex.gh; what it returns is what gh returned, parsed no further than the call site parsed it."""

    kind = "github"
    words = WORDS["github"]

    def __init__(self, repo):
        if not isinstance(repo, str) or repo.split("/", 1)[0].lower() == LOCAL_OWNER:
            raise StoreError("a local record's name is never a GitHub repository")
        self.repo = repo

    # ------------------------------------------------------------ what the store has
    def supports(self, feature):
        return feature in FEATURES

    def require(self, feature):
        """At the entry point of a script that is a GitHub-only feature: a GitHub store has every feature."""
        return None

    def github(self, args, stdin=None, timeout=60, errors=None, binary=False):
        """The raw call, for what only GitHub has: recortex.gh with the same arguments."""
        return recortex.gh(args, stdin=stdin, timeout=timeout, errors=errors, binary=binary)

    # ------------------------------------------------------------ the argument lists
    def command(self, op, *args, **kwargs):
        """The argument list of a write or a read, by name, as `show` prints it and the operation runs it."""
        repo = self.repo
        if op == "post_entry":
            (issue,) = args
            return ["issue", "comment", str(issue), "--repo", repo, "--body-file", kwargs.get("body_file") or "-"]
        if op == "edit_body":
            (number,) = args
            return ["issue", "edit", str(number), "--repo", repo, "--body-file", kwargs.get("body_file") or "-"]
        if op == "create_issue":
            title, labels = args
            command = ["issue", "create", "--repo", repo, "--title", title, "--body-file", kwargs.get("body_file") or "-"]
            for label in labels:
                command += ["--label", label]
            return command
        if op == "set_labels":
            number, add, remove = args
            return recortex.label_command(repo, number, add, remove)
        if op == "create_label":
            name, color, description = args
            return ["label", "create", name, "--repo", repo, "--color", color, "--description", description]
        if op == "edit_label":
            name, color, description = args
            return ["label", "edit", name, "--repo", repo, "--color", color, "--description", description]
        if op == "pin":
            (number,) = args
            return ["issue", "pin", str(number), "--repo", repo]
        if op == "close":
            number, reason = args
            return ["issue", "close", str(number), "--repo", repo, "--reason", reason]
        if op == "reopen":
            (number,) = args
            return ["issue", "reopen", str(number), "--repo", repo]
        if op == "list_entries":
            (issue,) = args
            return ["api", "--paginate", "repos/%s/issues/%d/comments?per_page=100" % (repo, issue)]
        raise ValueError("no such operation: %s" % op)

    def show(self, op, *args, note="", **kwargs):
        """The line a dry run prints for an operation: today's `gh ...` text."""
        return recortex.show_command(self.command(op, *args, **kwargs), note)

    # ------------------------------------------------------------ reads
    def whoami(self, host=False, timeout=60):
        args = ["api", "user"] + (["--hostname", "github.com"] if host else []) + ["--jq", ".login"]
        return recortex.gh(args, timeout=timeout).strip()

    def account_state(self, timeout=None):
        """(state, login) as recortex.gh_account returns them (called the way the scripts called it)."""
        if timeout is None or timeout == recortex.GH_CHECK_SECONDS:
            return recortex.gh_account()
        return recortex.gh_account(timeout)

    def read_issue(self, number, timeout=60, jq=None):
        args = ["api", "repos/%s/issues/%d" % (self.repo, number)] + (["--jq", jq] if jq else [])
        text = recortex.gh(args, timeout=timeout)
        return text if jq else json.loads(text)

    def read_entry(self, cid, timeout=60):
        return json.loads(recortex.gh(["api", "repos/%s/issues/comments/%s" % (self.repo, cid)], timeout=timeout))

    def list_entries(self, issue, timeout=60):
        return recortex.gh(self.command("list_entries", issue), timeout=timeout)

    def hub_entries(self, hub, timeout=60):
        """The newest comments of the hub, newest first, as recortex.read_hub_comments describes them."""
        owner, name = self.repo.split("/")
        raw = recortex.gh(["api", "graphql", "-f", "query=" + recortex.RESUME_QUERY, "-f", "owner=" + owner,
                           "-f", "name=" + name, "-F", "number=%d" % hub], timeout=timeout)
        issue = (((json.loads(raw).get("data") or {}).get("repository") or {}).get("issue"))
        if not issue:
            raise recortex.GhError("hub %s#%d not found" % (self.repo, hub))

        def login(node):
            return (node or {}).get("login")

        comments = []
        for node in ((issue.get("comments") or {}).get("nodes") or []):
            if not node:
                continue
            edits = node.get("userContentEdits") or {}
            comments.append({
                "url": str(node.get("url") or ""),
                "created": str(node.get("createdAt") or ""),
                "body": node.get("body") or "",
                "author": login(node.get("author")),
                "last_edited": node.get("lastEditedAt"),
                "editor": login(node.get("editor")),
                "edit_total": edits.get("totalCount") or 0,
                "edit_editors": [login(e.get("editor")) for e in (edits.get("nodes") or []) if e],
            })
        # newest first; of two comments created in the same second, the one the API lists later wins
        order = sorted(range(len(comments)), key=lambda i: (comments[i]["created"], i), reverse=True)
        return [comments[i] for i in order]

    def histories(self, nodes, timeout=60):
        """The raw answer of one GraphQL call for the edit histories of `nodes` ({comment id: (node id, cursor)})."""
        import index
        return recortex.gh(["api", "graphql", "-f", "query=" + index.comment_edits_query(nodes)], timeout=timeout)

    def find_issues(self, shape, timeout=60):
        repo = self.repo
        if shape == "mine":
            return json.loads(recortex.gh(["issue", "list", "--repo", repo, "--author", "@me", "--state", "all",
                                           "--json", "number,title,body,url", "--limit", "200"], timeout=timeout))
        if shape == "active":
            return json.loads(recortex.gh(["issue", "list", "--repo", repo, "--label", "active", "--state", "open",
                                           "--json", "number,labels", "--limit", "50"], timeout=timeout))
        if shape == "plan":
            return json.loads(recortex.gh(["issue", "list", "--repo", repo, "--label", "plan", "--state", "open",
                                           "--json", "number", "--limit", "100"], timeout=timeout))
        if shape == "dated":
            return json.loads(recortex.gh(["issue", "list", "--repo", repo, "--state", "all", "--json",
                                           "number,title,createdAt,closedAt,url", "--limit", "1000"],
                                          timeout=timeout) or "[]")
        raise ValueError("no such shape: %s" % shape)

    def list_labels(self, fields="name", timeout=60):
        return json.loads(recortex.gh(["label", "list", "--repo", self.repo, "--json", fields, "--limit", "1000"],
                                      timeout=timeout))

    def is_pinned(self, number):
        return bool(json.loads(recortex.gh(["issue", "view", str(number), "--repo", self.repo, "--json", "isPinned"]))
                    .get("isPinned"))

    def visibility(self, timeout=30):
        """"true" (private), "false" (public) or "" (not read)."""
        try:
            return recortex.gh(["api", "repos/%s" % self.repo, "--jq", ".private"], timeout=timeout).strip()
        except recortex.GhError:
            return ""

    def dump(self, timeout):
        """The two raw listings that a snapshot saves: every issue, every comment."""
        issues = recortex.gh(["api", "--paginate", "repos/%s/issues?state=all&per_page=100" % self.repo], timeout=timeout)
        comments = recortex.gh(["api", "--paginate", "repos/%s/issues/comments?per_page=100" % self.repo],
                               timeout=timeout)
        return issues, comments

    def dump_commands(self):
        """The two argument lists of `dump`, for a dry run."""
        return (["api", "--paginate", "repos/%s/issues?state=all&per_page=100" % self.repo],
                ["api", "--paginate", "repos/%s/issues/comments?per_page=100" % self.repo])

    def versions(self, issue):
        """The earlier versions of a topic's text: GitHub keeps them, not this plugin."""
        return []

    # ------------------------------------------------------------ writes
    def create_issue(self, title, labels, body=None, body_file=None):
        return recortex.gh(self.command("create_issue", title, labels, body_file=body_file), stdin=body)

    def edit_body(self, number, body=None, body_file=None):
        recortex.gh(self.command("edit_body", number, body_file=body_file), stdin=body)

    def post_entry(self, issue, body=None, body_file=None):
        return recortex.gh(self.command("post_entry", issue, body_file=body_file), stdin=body)

    def set_labels(self, number, add, remove):
        recortex.gh(self.command("set_labels", number, add, remove))

    def create_label(self, name, color, description):
        recortex.gh(self.command("create_label", name, color, description))

    def edit_label(self, name, color, description):
        recortex.gh(self.command("edit_label", name, color, description))

    def pin(self, number):
        recortex.gh(self.command("pin", number))

    def close(self, number, reason):
        recortex.gh(self.command("close", number, reason))

    def reopen(self, number):
        recortex.gh(self.command("reopen", number))

    # ------------------------------------------------------------ reading what a write returned
    def posted_id(self, output, issue=None):
        """The id of the entry a post made, from gh's output (its last line), or None."""
        if issue is None:
            match = re.search(r"#issuecomment-([0-9]+)", output)
        else:
            match = re.search(r"https://github\.com/" + re.escape(self.repo) + r"/issues/" + str(issue)
                              + r"#issuecomment-([0-9]+)", output)
        return match.group(1) if match else None

    def created_number(self, output):
        match = re.search(r"/(?:issues|pull)/([0-9]+)", output)
        if not match:
            raise recortex.GhError("could not read an issue number from gh output: %r" % output)
        return int(match.group(1))

    def read_back(self, what, number=None):
        """The words of a dry run's "would read back:" line: "entry" (the comment just posted) or "issue" (a topic's body)."""
        if what == "entry":
            return "gh api repos/%s/issues/comments/<id>" % self.repo
        return "gh api repos/%s/issues/%s" % (self.repo, number)

    def address(self, issue, entry=None):
        return address_of(self.repo, issue, entry)

    def api_issue(self, issue):
        return api_issue_of(self.repo, issue)

    def parse_address(self, text):
        """(issue, entry or 0) of an address this store made (address()), or None."""
        match = re.compile(r"https://github\.com/" + re.escape(self.repo) + r"/issues/([1-9][0-9]*)"
                           r"(?:#issuecomment-([1-9][0-9]*))?", re.I).fullmatch(text) if isinstance(text, str) else None
        return None if match is None else (int(match[1]), int(match[2] or 0))

    def safe_address(self, text):
        return safe_address_of(self.repo, text)

    def is_missing(self, error):
        return recortex.not_found(error)


_STORES = {}


def _config_used(config):
    """The configuration a store is decided by: the given one, else the project's, else None (no project file, or one that
    cannot be read: the caller that needs it reports that, and a GitHub name then works as it always did)."""
    if config is not None:
        return config
    try:
        return recortex.load_config()
    except (OSError, ValueError, RecursionError):
        return None


def for_repo(repo, config=None):
    """The store of the record named `repo`, one per process and name.

    Before any store is built, `kind_of` judges the configuration used (the given one, else the project's): a refusal there
    refuses every name. A local configuration opens its own record and refuses every other name, a GitHub name included, so
    that a project kept in a local record makes no `gh` call through a stray --repo; a name of a local record opens only
    when the configuration is local and names that same record. With a GitHub configuration, or none, a GitHub name gives a
    GithubStore."""
    used = _config_used(config)
    kind = kind_of(used) if isinstance(used, dict) else "github"
    if kind == "local":
        if repo != used.get("repo"):
            raise ConfigRefused(NOT_AGREE if is_local(repo) else ONLY_ITS_OWN)
        return _open_local(repo, used)
    if is_local(repo):
        raise ConfigRefused(NOT_AGREE)
    found = _STORES.get(repo)
    if found is None:
        found = _STORES[repo] = GithubStore(repo)
    return found


_LOCAL_STORES = {}


def _open_local(repo, config):
    """The LocalStore of the configuration's record. The machine must know the folder: a row of local-records.json (storage.
    record_bindings) binds the record's id to the real path of the folder that the project file's `place` names (for `own`,
    the row's path is the place; for `project`, the real path of <project root>/.recortex), the folder passes the record rules
    of storage.py, and record.json holds that id. Otherwise a RecordFolderError with a fixed kind: unbound (no row), moved
    (the id is bound, but to another folder), place (the project file's place is not one this version knows), or a kind of
    storage.StorageError (a link, another owner, a write bit, a folder above that another account can replace, missing)."""
    import local_store
    import storage
    place = config.get("place")
    if place not in ("own", "project"):
        raise RecordFolderError("recortex: the project file names a place for the record that this version does not know; "
                                "nothing was read.", "place")
    record_id = LOCAL_NAME.fullmatch(repo).group(1)
    rows = storage.record_bindings(record_id)
    if place == "project":
        wanted = storage.record_path(recortex.project_root())
        row = next((r for r in rows if r["path"] == wanted and r["place"] == "project"), None)
        if row is None:
            raise RecordFolderError("recortex: this project's local record is not bound to this folder on this machine." if not rows
                                    else "recortex: this project's local record is bound to another folder on this machine.",
                                    "moved" if rows else "unbound")
    else:
        mine = [r for r in rows if r["place"] in ("own", "chosen")]
        if not mine:
            raise RecordFolderError("recortex: this machine has no folder for this local record." if not rows else
                                    "recortex: this local record is bound to another place on this machine.",
                                    "unbound" if not rows else "moved")
        row = mine[-1]
    try:
        folder = storage.check_record_folder(row["path"], "own" if row["place"] == "own" else row["place"])
        info = os.lstat(folder)
    except (storage.StorageError, OSError) as error:
        raise RecordFolderError("recortex: the record folder cannot be used; nothing was read.",
                                getattr(error, "kind", "missing"))
    if (info.st_uid, getattr(info, "st_gid", 0)) != (row["uid"], row["gid"]):
        raise RecordFolderError("recortex: the record folder's owner changed since this machine approved it; nothing was read.",
                                "moved")
    key = (repo, folder, row["mac"])
    found = _LOCAL_STORES.get(key)
    if found is None:
        found = _LOCAL_STORES[key] = local_store.LocalStore(repo, config, folder, row["place"])
    return found


def github(args, stdin=None, timeout=60, errors=None, binary=False):
    """The raw call for code that has no repository at hand (a GraphQL query, a picture): recortex.gh, refused in a project that
    keeps a local record. The features that call it (the board, sub-issues, the page's pictures) stop at their entry points
    first, and this is the second lock."""
    used = _config_used(None)
    if isinstance(used, dict) and kind_of(used) == "local":
        raise Unsupported(NO_SERVER)
    return recortex.gh(args, stdin=stdin, timeout=timeout, errors=errors, binary=binary)


def for_config(config):
    """The store of the project file's own record: for_repo, so it has the same checks."""
    return for_repo(config["repo"], config)

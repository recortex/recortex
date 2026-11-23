"""Shared helpers for the recortex scripts.

Python 3 standard library only. All GitHub access goes through the gh CLI.
Set RECORTEX_GH to replace the gh command (the tests point it at a local fake).
The plugin's names, and its environment variables, are read through names.py.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import webbrowser

import names
import zones

CONFIG_NAME = names.CONFIG_NAME
BODY_LIMIT = 65536
RESUME_START = "<!-- ghim:resume:start -->"
RESUME_END = "<!-- ghim:resume:end -->"
RESUME_HEADING = "### Resume block: "
RESUME_LIMIT = 4000
PLACEHOLDER_RE = re.compile(r"\bNEW-([a-z0-9]+(?:-[a-z0-9]+)*)")
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profiles")
SOURCES_HEADING = "Sources"
RECORD_HEADING = "### Record: former issue body, kept as history (not deliverable text)"


class GhError(RuntimeError):
    pass


class GhMissing(GhError):
    """gh could not be started: not installed, or RECORTEX_GH names something that cannot be run."""


class GhTimeout(GhError):
    """gh did not finish within its time limit."""


def gh_command():
    return shlex.split(names.env("GH", "gh"))


def open_page(uri):
    """Ask a browser to open `uri`: True when one was asked. False, without a call, on Linux when none of DISPLAY, WAYLAND_DISPLAY
    and BROWSER is set to a value (a machine with no screen: webbrowser would answer true for a text browser that opens nothing
    there), and False when webbrowser raises or says no."""
    if sys.platform.startswith("linux") and not any(os.environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER")):
        return False
    try:
        return bool(webbrowser.open(uri))
    except Exception:  # noqa: BLE001 - no browser is no error
        return False


def gh(args, stdin=None, timeout=60, errors=None, binary=False):
    """Run gh with the given arguments and return stdout. Raise GhError on failure. errors="replace" reads an answer
    that is not text in the locale's encoding with replacement characters; by default it raises UnicodeDecodeError.
    binary=True returns stdout as bytes, for a file (the mind page's pictures); `errors` is then not used."""
    cmd = gh_command() + list(args)
    try:
        proc = subprocess.run(
            cmd, input=stdin, capture_output=True, text=not binary, timeout=timeout,
            errors=None if binary else errors
        )
    except FileNotFoundError:
        raise GhMissing("gh CLI not found (looked for: %s)" % cmd[0])
    except OSError:
        raise GhMissing("gh CLI cannot be run (looked for: %s)" % cmd[0])
    except subprocess.TimeoutExpired:
        raise GhTimeout("gh timed out after %ss: %s" % (timeout, " ".join(args[:3])))
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace") if binary else proc.stderr
        raise GhError(
            "gh %s failed (exit %d): %s"
            % (" ".join(args[:3]), proc.returncode, stderr.strip())
        )
    return proc.stdout


# A GitHub account's login: 1 to 39 ASCII letters, digits or -, starting with a letter or digit (the form of
# board_api.OWNER).
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
GH_CHECK_SECONDS = 10


def gh_account(timeout=GH_CHECK_SECONDS):
    """Whether gh can be run and is signed in to github.com, for `setup.py preflight` and `hooks.py --doctor`.

    Returns (state, login): "missing" (gh cannot be started), "slow" (a call took longer than `timeout` seconds),
    "signed-out" (`gh auth status` fails for github.com), "unnamed" (`gh auth status` passes, but `gh api user` fails
    or gives no login of GitHub's form: a token that is invalid or expired, or GitHub out of reach), or "ready" with the
    login. Nothing else that gh prints is returned: its output is untrusted, and the login is returned only when it
    fits LOGIN.
    """
    try:
        gh(["auth", "status", "--hostname", "github.com"], timeout=timeout, errors="replace")
    except (GhMissing, ValueError):  # ValueError: a RECORTEX_GH that does not split into a command
        return "missing", None
    except GhTimeout:
        return "slow", None
    except GhError:
        return "signed-out", None
    try:
        # the host named, so that GH_HOST cannot give the login of another host
        login = gh(["api", "user", "--hostname", "github.com", "--jq", ".login"], timeout=timeout,
                   errors="replace").strip()
    except GhTimeout:
        return "slow", None
    except GhError:
        return "unnamed", None
    return ("ready", login) if LOGIN.fullmatch(login) else ("unnamed", None)


def show_command(args, note=""):
    """Format a gh command for dry-run output."""
    text = "gh " + " ".join(shlex.quote(a) for a in args)
    return text + ("   # " + note if note else "")


def project_root():
    """The one folder a configuration file is read from.

    CLAUDE_PROJECT_DIR when set (Claude Code sets it for hooks, not for
    commands an agent runs), else the top of the git checkout that contains the
    current folder, else the current folder. Parent folders are never searched
    for a configuration file: a file planted above the project must not be
    able to configure it.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return os.path.abspath(env)
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return os.path.abspath(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.getcwd()


def find_config():
    """Path of the project's configuration file, or None.

    RECORTEX_CONFIG, set by the user, names a file explicitly. Otherwise only
    the project root is considered: <project root>/.recortex.json. A parent
    folder is never searched.
    """
    env = names.env("CONFIG")
    if env:
        return env if os.path.isfile(env) else None
    root = project_root()
    candidate = os.path.join(root, names.CONFIG_NAME)
    return candidate if os.path.isfile(candidate) else None


def load_config():
    path = find_config()
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        cfg = json.load(handle)
    if not isinstance(cfg, dict):
        raise ValueError("%s must hold a JSON object" % path)
    cfg["_path"] = path
    return cfg


def config_home():
    """$XDG_CONFIG_HOME when it is an absolute path, else ~/.config. A relative value would resolve against the
    current folder, which can be a project's, so it is not used."""
    value = os.environ.get("XDG_CONFIG_HOME")
    return value if value and os.path.isabs(value) else os.path.join(os.path.expanduser("~"), ".config")


def trusted_files():
    """The trusted lists that are read: RECORTEX_TRUSTED_FILE alone when it is an absolute path (the default list
    is then not read); else <config home>/recortex/trusted-repos, which belongs to the user."""
    env = names.env("TRUSTED_FILE")
    if env and os.path.isabs(env):
        return [env]
    return [os.path.join(config_home(), names.NAME, "trusted-repos")]


def trusted_target():
    """The trusted list a new entry goes to: RECORTEX_TRUSTED_FILE when it is set, else the default list."""
    return trusted_files()[0]


def settings_file():
    """The user's settings (`"discovery": false`): <config home>/recortex/settings.json."""
    return os.path.join(config_home(), names.NAME, "settings.json")


def read_trusted_file(path):
    """{"owner/name": {logins}} from one trusted list (one "owner/name [login ...]" per line, # for comments); empty
    when the file cannot be read. Keys and logins are lowercase."""
    trusted = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parts = line.split("#", 1)[0].split()
                if parts and valid_repo(parts[0]):
                    trusted.setdefault(parts[0].lower(), set()).update(p.lower() for p in parts[1:])
    except OSError:
        pass
    return trusted


def trusted_from_variable():
    """The repositories in RECORTEX_TRUSTED_REPOS (comma-separated owner/name), lowercase."""
    found = set()
    for name in names.env("TRUSTED_REPOS", "").split(","):
        name = name.strip()
        if valid_repo(name):  # checked before lowercase, which turns the Kelvin sign into k
            found.add(name.lower())
    return found


def trusted_repos():
    """Repositories the user has trusted, as {"owner/name": {extra allowed logins}}.

    Read from trusted_files() and from RECORTEX_TRUSTED_REPOS. Both belong to the
    user; nothing inside a project can add to them. Keys and logins are
    lowercase.
    """
    trusted = {}
    for path in trusted_files():
        for repo, logins in read_trusted_file(path).items():
            trusted.setdefault(repo, set()).update(logins)
    for repo in trusted_from_variable():
        trusted.setdefault(repo, set())
    return trusted


def trusted_sources(repo):
    """For --doctor and setup: the trusted lists that list `repo`, and the name of the variable when it lists it."""
    found = [path for path in trusted_files() if repo.lower() in read_trusted_file(path)]
    if repo.lower() in trusted_from_variable():
        found.append(names.ENV_PREFIX + "TRUSTED_REPOS")
    return found


def allowed_users(repo, trusted=None):
    """Logins whose hub text may be shown.

    Empty unless the user trusted the repository. For a trusted repository: its
    owner plus the logins listed for it in the user-level trusted file.
    """
    trusted = trusted_repos() if trusted is None else trusted
    if repo.lower() not in trusted:
        return set()
    return {repo.split("/")[0].lower()} | trusted[repo.lower()]


COMMENTS_READ = 50
EDITS_READ = 100

RESUME_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(last: 50) {
        totalCount
        nodes {
          id
          url
          createdAt
          body
          author { login }
          editor { login }
          lastEditedAt
          userContentEdits(first: 100) { totalCount nodes { editedAt editor { login } } }
        }
      }
    }
  }
}"""


def read_hub_comments(repo, number, timeout=60):
    """The newest COMMENTS_READ comments of the hub, newest first by creation time.

    One GraphQL call returns each comment's text together with who wrote it and
    its edit history, so what is shown is what was checked. Each item is a dict
    with url, created, body, author, last_edited, editor, edit_total and
    edit_editors (a list of logins; None stands for an unknown account).
    """
    owner, name = repo.split("/")
    raw = gh(["api", "graphql", "-f", "query=" + RESUME_QUERY, "-f", "owner=" + owner,
              "-f", "name=" + name, "-F", "number=%d" % number], timeout=timeout)
    issue = (((json.loads(raw).get("data") or {}).get("repository") or {}).get("issue"))
    if not issue:
        raise GhError("hub %s#%d not found" % (repo, number))

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


def resume_comment_problem(comment, allowed):
    """Why a comment that carries the resume markers may not be shown, or None.

    Fail closed. Resume comments are append-only: the author must be allowed
    and the comment must never have been edited, by anyone. (On GitHub an
    unedited comment has no lastEditedAt and an empty edit history; an edited
    one lists its edits plus an entry for its creation.)
    """
    if extract_resume_block(comment["body"]) is None:
        return "malformed markers"
    author = comment["author"]
    if not author or author.lower() not in allowed:
        return "written by a non-allowed account"
    if comment["last_edited"] or comment["edit_total"] > 0 or comment["edit_editors"]:
        return "edited after posting"
    return None


def pick_resume_comment(comments, allowed, any_author=False):
    """Choose the newest usable resume comment.

    Returns (comment or None, {reason: count of newer marker comments ignored}).
    `comments` must be newest first.
    """
    ignored = {}
    for comment in comments:
        if RESUME_START not in comment["body"]:
            continue
        problem = resume_comment_problem(comment, allowed)
        if any_author and problem != "malformed markers":
            problem = None
        if problem is None:
            return comment, ignored
        ignored[problem] = ignored.get(problem, 0) + 1
    return None, ignored


def describe(config, repo):
    """The line every writing or dry-running script prints first."""
    return "config: %s  repo: %s" % (config.get("_path") or "none (repository given on the command line or in the manifest)", repo)


def config_name(cfg):
    """For messages: the name of the configuration file that was read, else the current name."""
    return os.path.basename(cfg.get("_path") or "") or CONFIG_NAME


def resolve_repo(cli_value, cfg):
    repo = cli_value or cfg.get("repo")
    if not valid_repo(repo):
        sys.exit(
            "error: no repository. Pass --repo owner/name or set \"repo\" in %s"
            % config_name(cfg)
        )
    return repo


def resolve_hub(cli_value, cfg):
    hub = cli_value if cli_value is not None else cfg.get("hub")
    if not isinstance(hub, int) or hub < 1:
        sys.exit(
            "error: no hub issue number. Pass --hub N or set \"hub\" in %s"
            % config_name(cfg)
        )
    return hub


def normalize(text):
    """Form used to compare a draft with what GitHub returns.

    Line endings become LF and trailing whitespace is dropped from every line
    and from the end of the text. Nothing else changes.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip()


def first_difference(expected, actual):
    """Describe the first place two normalized texts differ."""
    exp, act = expected.split("\n"), actual.split("\n")
    for index in range(max(len(exp), len(act))):
        left = exp[index] if index < len(exp) else "<end of text>"
        right = act[index] if index < len(act) else "<end of text>"
        if left != right:
            return "line %d\n  sent:     %r\n  returned: %r" % (index + 1, left, right)
    return "no difference"


def parse_json_stream(text):
    """Parse the output of `gh api --paginate`: one JSON value per page."""
    decoder = json.JSONDecoder()
    items, pos, length = [], 0, len(text)
    while pos < length:
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            break
        value, pos = decoder.raw_decode(text, pos)
        if isinstance(value, list):
            items.extend(value)
        else:
            items.append(value)
    return items


def read_issue(repo, number, timeout=60):
    """The issue's body and label names, read from the REST API."""
    data = json.loads(gh(["api", "repos/%s/issues/%d" % (repo, number)], timeout=timeout))
    return {"body": data.get("body") or "",
            "labels": [label.get("name", "") for label in data.get("labels") or []]}


def read_issue_body(repo, number, timeout=60):
    return read_issue(repo, number, timeout)["body"]


def load_profile(name, shipped_only=False):
    """A profile from profiles/<name>.json, or, unless shipped_only, from a file path. Raises ValueError when none.

    A plain name always means the shipped profile, even when a file of that name
    is in the current folder; only a name that is not a shipped profile is read
    as a path (setup_labels.py --profile path/to/custom.json).
    """
    if not isinstance(name, str):
        raise ValueError("a profile is named by a string, not %s" % type(name).__name__)
    shipped = os.path.join(PROFILE_DIR, name + ".json")
    if PROFILE_NAME.fullmatch(name) and os.path.isfile(shipped):
        path = shipped
    elif shipped_only:
        raise ValueError("no shipped profile %r" % name)
    else:
        path = name
    if not os.path.isfile(path):
        available = sorted(f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".json"))
        raise ValueError("no profile %r. Available: %s" % (name, ", ".join(available)))
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


LABEL_NAME = re.compile(r"[a-z0-9][a-z0-9:._-]{0,49}")
# GitHub's form of owner/name: the owner is 1 to 39 ASCII letters, digits, - or _ (managed accounts have _),
# starting with a letter or digit; the name is 1 to 100 of those and ., and not . or ..
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}")
HEADING_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ,.'()-]{0,59}")
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def valid_repo(value):
    """True when `value` is a repository name in GitHub's form, owner/name in ASCII; see REPO_NAME."""
    return isinstance(value, str) and REPO_NAME.fullmatch(value) is not None


RESERVED_PREFIXES = {"topic", "type"}
SECTION_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def is_shipped(name):
    return isinstance(name, str) and bool(PROFILE_NAME.fullmatch(name)) and \
        os.path.isfile(os.path.join(PROFILE_DIR, name + ".json"))


def profile_prefix(name):
    """The section-label prefix of a shipped profile, for example "ms" for manuscript."""
    profile = load_profile(name, shipped_only=True)
    prefix = profile.get("prefix")
    return prefix if isinstance(prefix, str) and re.fullmatch(r"[a-z]{1,10}", prefix) else None


def custom_sections(entry):
    """(profile name, [(label, heading)]) for a profile entry with sections chosen at setup, or None if invalid.

    The entry names a shipped profile and lists [label, heading] pairs; an entry
    of the custom profile also names its deliverable ("deliverable", a plain
    heading). Each label must carry that profile's prefix and a plain slug, each
    heading a plain form; duplicates, "Sources" and more than 20 sections are
    refused, so a project file cannot name role, topic or type labels or carry
    other text.
    """
    if not isinstance(entry, dict) or set(entry) - {"name", "sections", "deliverable"} or \
            not is_shipped(entry.get("name")):
        return None
    if "deliverable" in entry and not (isinstance(entry["deliverable"], str) and
                                       HEADING_TEXT.fullmatch(entry["deliverable"])):
        return None
    prefix = profile_prefix(entry["name"])
    sections = entry.get("sections")
    if not prefix or prefix in RESERVED_PREFIXES or not isinstance(sections, list) or not 1 <= len(sections) <= 20:
        return None
    out, labels, headings = [], set(), set()
    for pair in sections:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            return None
        label, heading = pair
        if not (label.startswith(prefix + ":") and SECTION_SLUG.fullmatch(label[len(prefix) + 1:]) and
                LABEL_NAME.fullmatch(label) and HEADING_TEXT.fullmatch(heading)):
            return None
        if label in labels or heading.lower() in headings or heading.lower() == SOURCES_HEADING.lower():
            return None
        labels.add(label)
        headings.add(heading.lower())
        out.append((label, heading))
    return entry["name"], out


def shipped_profiles(names):
    """The profile entries a hook may use: names of profiles shipped in profiles/, and entries with sections
    chosen at setup that pass custom_sections. Hooks never load a profile from a path a project names."""
    out = []
    for entry in names if isinstance(names, list) else []:
        if isinstance(entry, str) and is_shipped(entry):
            out.append(entry)
        elif isinstance(entry, dict):
            parsed = custom_sections(entry)
            if parsed:
                out.append({"name": parsed[0], "sections": [list(pair) for pair in parsed[1]]})
    return out


def profile_names(profiles):
    """The profile names of a profiles list that may hold entries with sections chosen at setup. Entries that are
    neither a name nor a dict with a name are left out."""
    if not isinstance(profiles, list):
        return []
    return [p["name"] if isinstance(p, dict) else p for p in profiles
            if isinstance(p, str) or isinstance(p, dict) and isinstance(p.get("name"), str)]


def section_labels(profiles, shipped_only=False):
    """The deliverable-section labels of the given profiles, as a list of (label, heading).

    A section label is a profile label with a "heading": the level-2 heading
    that a topic body uses for the text of that section. Labels and headings
    that do not fit a plain pattern (letters, digits, a few punctuation marks,
    a length limit) are left out, so a custom profile cannot carry other text.
    """
    out = []
    for name in profiles:
        if isinstance(name, dict):  # a profile with sections chosen at setup
            parsed = custom_sections(name)
            if parsed is None:
                raise ValueError("a profile entry with sections is not valid (name, prefix, labels or headings)")
            out.extend(parsed[1])
            continue
        for label in load_profile(name, shipped_only).get("labels", []):
            heading, label_name = label.get("heading"), label.get("name")
            if isinstance(heading, str) and isinstance(label_name, str) and \
                    HEADING_TEXT.fullmatch(heading) and LABEL_NAME.fullmatch(label_name):
                out.append((label_name, heading))
    return out


PLAN_PART = re.compile(r"[a-z][a-z-]{0,29}")


def plan_profile(profiles):
    """The plan of the project's deliverable, from the first shipped profile of `profiles` that has a "plan" key, or
    None. A dict: profile, noun, parts, evidence, display, write, write_from_record and sections (the headings of
    that profile in this project). `write` is cut to the headings the project has, `write_from_record` to those among
    them. Only shipped profiles are read: a project file cannot name a part. An entry with sections chosen at setup
    keeps its profile's parts."""
    for entry in profiles if isinstance(profiles, list) else []:
        name = entry if isinstance(entry, str) else entry.get("name") if isinstance(entry, dict) else None
        if not is_shipped(name) or isinstance(entry, dict) and custom_sections(entry) is None:
            continue
        try:
            plan = load_profile(name, shipped_only=True).get("plan")
            headings = [heading for _, heading in section_labels([entry], shipped_only=True)]
        except (ValueError, OSError):
            continue
        if not isinstance(plan, dict):
            continue
        parts = [p for p in plan.get("parts", []) if isinstance(p, str) and PLAN_PART.fullmatch(p)]
        noun = plan.get("noun") if isinstance(plan.get("noun"), str) and HEADING_TEXT.fullmatch(plan["noun"]) else "paper"
        lowered = {h.lower(): h for h in headings}
        write = [lowered[h.lower()] for h in plan.get("write", []) if isinstance(h, str) and h.lower() in lowered]
        from_record = [h for h in write if h.lower() in {x.lower() for x in plan.get("write_from_record", [])
                                                         if isinstance(x, str)}]
        return {"profile": name, "noun": noun, "parts": parts,
                "evidence": [p for p in plan.get("evidence", []) if p in parts],
                "display": [p for p in plan.get("display", []) if p in parts],
                "write": write, "write_from_record": from_record, "sections": headings}
    return None


OWN_PATH_LIMIT = 400
OWN_HEADING = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ,.'()-]{0,59}")


def own_path_ok(value):
    """True for a path the project file may give for the author's own paper: relative, at most 400 characters, no
    absolute form, no `..` and no empty or `.` part, no NUL or backslash. Whether it names a plain file inside the
    project is checked when it is used (own_paper.py)."""
    if not isinstance(value, str) or not value or len(value) > OWN_PATH_LIMIT or "\0" in value or "\\" in value \
            or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


def own_brought(value):
    """{issue number: {heading: {"text": 64 hex, "commit": 40 hex or ""}}} from a `brought` entry; every value is
    checked against its pattern and what does not fit is left out."""
    out = {}
    for key, sections in (value.items() if isinstance(value, dict) else []):
        if not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", key) or not isinstance(sections, dict):
            continue
        for heading, entry in sections.items():
            if not isinstance(heading, str) or not OWN_HEADING.fullmatch(heading) or not isinstance(entry, dict):
                continue
            text, commit = entry.get("text"), entry.get("commit", "")
            if isinstance(text, str) and re.fullmatch(r"[0-9a-f]{64}", text) and isinstance(commit, str) \
                    and re.fullmatch(r"(?:[0-9a-f]{40})?", commit):
                out.setdefault(int(key), {})[heading] = {"text": text, "commit": commit}
    return out


def own_manuscript(config):
    """What the project file says of the author's own paper, checked: {"state": "none" | "no" | "inside" | "outside",
    "path": the relative path or "", "brought": own_brought(...)}. A value that breaks a rule counts as none: "no" is
    `false`; a path inside the project is relative; "outside" says the place is kept per machine."""
    value = (config or {}).get("own_manuscript")
    if value is False:
        return {"state": "no", "path": "", "brought": {}}
    if not isinstance(value, dict):
        return {"state": "none", "path": "", "brought": {}}
    brought = own_brought(value.get("brought"))
    if value.get("outside") is True and "path" not in value:
        return {"state": "outside", "path": "", "brought": brought}
    if "path" in value and own_path_ok(value["path"]) and "outside" not in value:
        return {"state": "inside", "path": value["path"], "brought": brought}
    return {"state": "none", "path": "", "brought": {}}


def is_brought_section(config, issue, heading):
    """Whether this record section belongs to a part imported from the author's paper."""
    brought = own_manuscript(config)["brought"].get(issue, {})
    return any(part.casefold() == heading.casefold() for part in brought)


def record_zone(config):
    """The time zone the record states, a zones.Zone, or None. The statement is the project file's `timezone`, the one time
    convention of the tracker in words, which the hub repeats ("US Pacific time, written as 2026-03-04 14:10 PST");
    None when the project states none, or one that zones.py does not know."""
    return zones.zone_of((config or {}).get("timezone"))


def stamp(moment, config=None):
    """`moment` (aware) as the record writes a time: '2026-03-04 14:10 PST' in the zone the record states, with the
    abbreviation of that moment, else '2026-03-04 22:10 UTC'. Every stamp a script writes into the record is made here."""
    return (record_zone(config) or zones.UTC).stamp(moment)


def marker_time(moment, config=None):
    """The same moment as the `at=` of an event marker, with its offset: '2026-03-04T14:10-08:00', else
    '2026-03-04T22:10Z'. The heading's stamp and the marker's time always come from the same zone."""
    return (record_zone(config) or zones.UTC).iso(moment)


def plan_issue(config, profile):
    """The number of the plan issue of `profile` in the project file ("plans": {"manuscript": 12}), or None. Only an
    integer from 1 to 2147483647 counts, never a bool; whether that issue carries the label `plan` and was opened by
    an allowed account is the reader's to check."""
    plans = (config or {}).get("plans")
    value = plans.get(profile) if isinstance(plans, dict) else None
    return value if type(value) is int and 1 <= value <= 2147483647 else None


def settings_off(name):
    """True when the user's settings.json holds `"<name>": false` (the form of the discovery and field-guidance
    switches); False when the file is missing, unreadable or says anything else."""
    try:
        with open(settings_file(), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(value, dict) and value.get(name) is False


def planning_on(config):
    """False when the project file holds "planning": false. The user's own settings.json has the same switch, read by
    the hooks (hooks.planning_off), since the project file can switch planning off but never on."""
    return (config or {}).get("planning") is not False


def body_sections(text):
    """The level-2 headings of a body outside fenced blocks, as a list of (line number, heading text)."""
    found, fence = [], None
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        stripped = line.strip()
        opening = re.match(r"^(```+|~~~+)", stripped)
        if fence is None and opening:
            fence = opening.group(1)
            continue
        if fence is not None:
            if stripped.startswith(fence) and stripped.strip(fence[0]) == "":
                fence = None
            continue
        match = re.match(r"^## +(.*?)\s*#*\s*$", line)
        if match:
            found.append((number, match.group(1).strip()))
    return found


def labels_from_headings(text, profiles, intended=None):
    """The section labels a body's headings call for.

    Returns (labels, problems). When the profiles define section labels, every
    level-2 heading other than "Sources" must be the heading of one of them. When several profiles
    use the same heading, the labels in `intended` choose between them;
    without a choice the heading is reported as ambiguous.
    """
    by_heading = {}
    for label, heading in section_labels(profiles):
        by_heading.setdefault(heading.lower(), []).append(label)
    labels, problems = [], []
    if not by_heading:
        return labels, problems  # no section labels: the headings are free
    for number, heading in body_sections(text):
        if heading.lower() == SOURCES_HEADING.lower():
            continue
        candidates = by_heading.get(heading.lower())
        if not candidates:
            problems.append((number, "'## %s' is not a section heading of the profiles %s; use one of: %s"
                             % (heading, ", ".join(profiles) or "(none)",
                                ", ".join(sorted({h for _, h in section_labels(profiles)})) or "(none)")))
            continue
        if len(candidates) > 1 and intended is not None:
            chosen = [label for label in candidates if label.lower() in {l.lower() for l in intended}]
            candidates = chosen or candidates
        if len(candidates) > 1:
            problems.append((number, "'## %s' is the heading of %s; name the profile or the intended labels"
                             % (heading, " and ".join(candidates))))
            continue
        if candidates[0] not in labels:
            labels.append(candidates[0])
    return labels, problems


def label_changes(current, wanted, profiles):
    """Section labels to add and to remove so that an issue's labels follow its body's headings.

    Only section labels of the given profiles are ever removed; role and topic
    labels are left as they are.
    """
    known = {label.lower() for label, _ in section_labels(profiles)}
    have = {label.lower() for label in current}
    want = {label.lower() for label in wanted}
    add = [label for label in wanted if label.lower() not in have]
    remove = [label for label in current if label.lower() in known and label.lower() not in want]
    return add, remove


def shipped_section_labels():
    """Lowercase names of the section labels of every profile shipped in profiles/."""
    names = set()
    for name in sorted(os.listdir(PROFILE_DIR)):
        if name.endswith(".json"):
            names.update(label.lower() for label, _ in section_labels([name[:-5]]))
    return names


def plan_section_labels(labels, body, profiles):
    """Check an issue before it gets a deliverable body, and plan its section labels.

    Returns a dict:
      stop     reasons the issue may not get a deliverable body (role labels)
      foreign  section labels on the issue from profiles other than `profiles`
      checked  False when the profiles define no section labels (nothing is changed)
      wanted, add, remove, problems  as from labels_from_headings and label_changes
    Raises ValueError for an unknown profile.
    """
    lower = {label.lower() for label in labels}
    stop = []
    for role in ("hub", "active"):
        if role in lower:
            stop.append("the issue is labeled %s; only a topic issue labeled final-record holds deliverable text" % role)
    if "final-record" not in lower:
        stop.append("the issue is not labeled final-record; label it first if it is a topic of the deliverable")
    own = {label.lower() for label, _ in section_labels(profiles)}
    foreign = [label for label in labels if label.lower() in shipped_section_labels() and label.lower() not in own]
    plan = {"stop": stop, "foreign": foreign, "checked": bool(own), "wanted": [], "add": [], "remove": [],
            "problems": []}
    if own:
        plan["wanted"], plan["problems"] = labels_from_headings(body, profiles, intended=labels)
        if not plan["problems"]:
            plan["add"], plan["remove"] = label_changes(labels, plan["wanted"], profiles)
    return plan


def label_command(repo, number, add, remove):
    command = ["issue", "edit", str(number), "--repo", repo]
    for label in add:
        command += ["--add-label", label]
    for label in remove:
        command += ["--remove-label", label]
    return command


def read_comment_body(repo, comment_id):
    data = json.loads(gh(["api", "repos/%s/issues/comments/%s" % (repo, comment_id)]))
    return data.get("body") or ""


def not_found(error):
    """True when a GhError is GitHub's "Not Found". Only the status gh prints counts: the message also holds the
    path of the request, and an issue number, a comment id or a repository name may hold 404."""
    return "(HTTP 404)" in str(error)


def number_exists(repo, number):
    """True when issue or pull request `number` exists in `repo`."""
    try:
        gh(["api", "repos/%s/issues/%d" % (repo, number), "--jq", ".number"])
        return True
    except GhError as error:
        if not_found(error):
            return False
        raise


def issue_kind(repo, number):
    """What `number` is in `repo`, from one REST read: ("issue" or "pull", the issue's data), or (None, None) when
    GitHub answers Not Found. Any other failure raises GhError."""
    try:
        data = json.loads(gh(["api", "repos/%s/issues/%d" % (repo, number)]))
    except GhError as error:
        if not_found(error):
            return None, None
        raise
    except ValueError:
        raise GhError("GitHub's answer for %s#%d could not be read" % (repo, number))
    if not isinstance(data, dict):
        raise GhError("GitHub's answer for %s#%d could not be read" % (repo, number))
    return ("pull" if data.get("pull_request") else "issue"), data


def verify_body(expected, actual, what):
    """Return True when the returned text equals the sent text; print the gap otherwise."""
    left, right = normalize(expected), normalize(actual)
    if left == right:
        print("verified: %s matches the draft (%d characters)" % (what, len(left)))
        return True
    print("MISMATCH: %s differs from the draft at %s" % (what, first_difference(left, right)))
    return False


def substitute_placeholders(text, numbers, target_repo, home_repo):
    """Replace NEW-<key> with the real reference.

    In prose the reference is #N (or owner/repo#N when the text is posted to a
    repository other than the one the issues live in). Inside a Mermaid block it
    is "Issue N", because # breaks Mermaid on GitHub. Inline code spans and
    fenced blocks other than Mermaid are left as they are.
    """
    out, region, fence = [], "prose", None

    def repl(match):
        key = match.group(1)
        if key not in numbers:
            return match.group(0)
        if region == "mermaid":
            return "Issue %d" % numbers[key]
        if target_repo != home_repo:
            return "%s#%d" % (home_repo, numbers[key])
        return "#%d" % numbers[key]

    for line in text.split("\n"):
        stripped = line.strip()
        opening = re.match(r"^(```+|~~~+)", stripped)
        if fence is None and opening:
            fence = opening.group(1)
            region = "mermaid" if stripped[len(fence):].strip().lower() == "mermaid" else "code"
            out.append(line)
        elif fence is not None and stripped.startswith(fence) and stripped.strip(fence[0]) == "":
            fence, region = None, "prose"
            out.append(line)
        elif region == "code":
            out.append(line)
        elif region == "mermaid":
            out.append(PLACEHOLDER_RE.sub(repl, line))
        else:
            parts = re.split(r"(`[^`\n]*`)", line)
            out.append("".join(part if part.startswith("`") and part.endswith("`") and len(part) > 1
                               else PLACEHOLDER_RE.sub(repl, part) for part in parts))
    return "\n".join(out)


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_working_files(folder, files):
    """Write the files of a working folder, {name: text or bytes}: the folder a script is told to fill and a drafting agent may
    also write. Nothing is written through a link, whatever the agent does while this runs:

    - no part of the folder's path may be a symlink (a trailing slash or a link in a parent folder included): the path must
      be what realpath says it is;
    - the folder is opened once, without following a link, and its device and inode must be those of the folder that was
      checked; every file is then made and moved into place through that open folder, so a link put in place of the folder
      afterwards redirects nothing;
    - no name may be a symlink, and each file is a fresh temporary file (mode 0600, made exclusively and without following
      a link) moved over the name, so that a hard link at the name is replaced and never written through.

    The names are all checked before the first is written. Exits with one line when something is refused, and where this
    system lacks O_DIRECTORY, O_NOFOLLOW or the calls that take a folder's descriptor, which are what makes this safe."""
    import secrets
    import stat
    if not (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd and os.unlink in os.supports_dir_fd and os.rename in os.supports_dir_fd):
        sys.exit("error: this system cannot write a working folder without following links, so nothing was written")
    folder = os.fspath(folder)
    where = os.path.abspath(folder)
    if not folder or "\0" in folder or os.path.realpath(folder) != where:
        sys.exit("error: %s, or a folder on its path, is a link or not a usable folder; use a new folder of your own" % folder)
    for name in files:
        if not name or name in (".", "..") or os.sep in name or "\0" in name:
            sys.exit("error: %r is not a file name of a working folder" % name)
    try:
        os.makedirs(where, exist_ok=True)
        checked = os.lstat(where)
    except OSError:
        sys.exit("error: %s is not a usable folder; use a new folder of your own" % folder)
    if not stat.S_ISDIR(checked.st_mode) or os.path.realpath(where) != where:
        sys.exit("error: %s, or a folder on its path, is a link or not a usable folder; use a new folder of your own" % folder)
    try:
        descriptor = os.open(where, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        sys.exit("error: %s could not be opened as a folder without following a link; use a new folder of your own" % folder)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            sys.exit("error: %s changed while it was being opened; nothing was written" % folder)
        for name in files:
            try:
                found = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(found.st_mode):
                sys.exit("error: %s in %s is a link and is never written through; use a new folder of your own" % (name, folder))
        for name, text in files.items():
            temporary = ".part-%s" % secrets.token_hex(8)
            handle_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            try:
                with os.fdopen(handle_fd, "wb") as handle:
                    handle.write(text.encode("utf-8") if isinstance(text, str) else text)
                os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except OSError:
                    pass
                raise
    finally:
        os.close(descriptor)


def extract_resume_block(body):
    """Return the text between the resume markers, or None when the text is not a well-formed resume comment.

    Well-formed means: the first non-blank line starts with the resume heading;
    each marker occurs exactly once in the whole text; each stands alone on its
    own line at column 0, outside any fenced block, start before end. A marker
    inside a blockquote, a code span or a fenced block therefore never counts.
    """
    if body.count(RESUME_START) != 1 or body.count(RESUME_END) != 1:
        return None
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = next((line for line in lines if line.strip()), "")
    if not first.startswith(RESUME_HEADING):
        return None
    start = end = None
    fence = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        opening = re.match(r"^(```+|~~~+)", stripped)
        if fence is None and opening:
            fence = opening.group(1)
            continue
        if fence is not None:
            if stripped.startswith(fence) and stripped.strip(fence[0]) == "":
                fence = None
            continue
        if line.rstrip() == RESUME_START:
            start = index
        elif line.rstrip() == RESUME_END:
            end = index
    if start is None or end is None or end < start:
        return None
    return "\n".join(lines[start + 1:end]).strip("\n")

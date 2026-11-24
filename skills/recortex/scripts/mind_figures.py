#!/usr/bin/env python3
"""The pictures of the figure blocks that the mind page embeds, fetched and checked at build time.

Python 3 standard library only. This module is called by mind.py when it builds the page, and by nothing else: no
hook, no view of the page and no model builder reads it. It never raises for odd input; a picture that cannot be
embedded gets a reason code (REASONS), and the page falls back to the figure's title, caption and link.

What it allows (spec 12, the owner's decision D10):

  Source   an image named by a figure block, at https://github.com/<owner>/<repo>/blob/<commit or tag>/<path>. The
           repository must be the record's own repository or the project's code repository. The path is checked part
           by part. A tag, or a short commit, is resolved to its full commit, and the commit is what gets fetched and
           cached; a branch name is never a version, so it is refused.
  Fetch    only here, and only when the caller says so (mind.py with --sync or --figures): through record_store.github(),
           `gh api repos/<owner>/<repo>/contents/<path>?ref=<commit>` with the raw media type, under one time budget.
           Each picture is at most 5 MB and all of them at most 40 MB.
  Cache    <index folder>/figures/<commit>/<sha256 of the path>, in private folders (storage.py), mode 0600, written
           through a fresh temporary file. A commit never changes, so a cached picture is not fetched again, and a
           build that does not fetch uses the cache alone. <index folder>/figures/refs/<sha256 of repository and
           ref> keeps the commit that a tag or a short commit resolved to, so that an offline build finds a
           picture that a tag names.
  Check    the type is judged from the first bytes and nothing else: PNG, JPEG, GIF or WebP. SVG, which can carry
           markup and script, and everything else is refused. The width and the height are read from the header and
           must be from 1 to 20000. A cached file is judged again at every read.

The bytes that load() returns have passed all of this. mind.py still builds the data: address itself, from the
bytes, and judges them again (inspect()); nothing here is trusted by the page.
"""

import hashlib
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record_format  # noqa: E402
import recortex  # noqa: E402
import record_store  # noqa: E402
import storage  # noqa: E402

MAX_IMAGE = 5 * 1024 * 1024
MAX_TOTAL = 40 * 1024 * 1024
MAX_SIDE = 20000
MAX_PATH = 400
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
COMMIT = re.compile(r"[0-9a-f]{7,40}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
PART = re.compile(r"[A-Za-z0-9_.@+-]+")
FOLDER = "figures"
REFS = "refs"

REASONS = {
    "svg": "a drawing in SVG is not embedded",
    "not-an-image": "the file is not a PNG, JPEG, GIF or WebP picture",
    "bad-size": "the picture's width or height is not between 1 and 20000",
    "too-big": "the picture is larger than 5 MB",
    "budget": "the pictures of this page would be larger than 40 MB together",
    "not-allowed-source": "the picture is not in this project's own repositories, or is not at a fixed version",
    "bad-path": "the path of the picture is not one the page uses",
    "unknown-tag": "the version of the picture could not be found",
    "not-found": "the picture is not at that path in that version",
    "fetch-failed": "the picture could not be fetched",
    "timeout": "the pictures were not fetched in the time allowed",
    "offline": "the picture is not in the local copy, and this build did not fetch",
    "local-record": "a local record keeps no pictures of its own",
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


# ---------------------------------------------------------------- the check of the bytes

def png_size(raw):
    if len(raw) >= 24 and raw[12:16] == b"IHDR":
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    return None


def gif_size(raw):
    if len(raw) >= 10:
        return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
    return None


def jpeg_size(raw):
    i, n = 2, len(raw)
    while i + 4 <= n:
        if raw[i] != 0xFF:
            return None
        while i < n and raw[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return None
        marker = raw[i]
        i += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # a marker without a length
            continue
        if marker in (0xD9, 0xDA) or i + 2 > n:  # the end, or the scan, before any frame header
            return None
        length = (raw[i] << 8) | raw[i + 1]
        if length < 2:
            return None
        if marker in SOF:
            if length < 8 or i + 7 > n:
                return None
            return (raw[i + 5] << 8) | raw[i + 6], (raw[i + 3] << 8) | raw[i + 4]
        i += length
    return None


def webp_size(raw):
    kind = raw[12:16]
    if kind == b"VP8 " and len(raw) >= 30 and raw[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(raw[26:28], "little") & 0x3FFF, int.from_bytes(raw[28:30], "little") & 0x3FFF
    if kind == b"VP8L" and len(raw) >= 25 and raw[20] == 0x2F:
        bits = int.from_bytes(raw[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X" and len(raw) >= 30:
        return int.from_bytes(raw[24:27], "little") + 1, int.from_bytes(raw[27:30], "little") + 1
    return None


def inspect(raw):
    """(kind, width, height) of a picture, or (None, reason code). The type comes from the first bytes alone, never
    from a name or a header: "png", "jpeg", "gif" or "webp". SVG, HTML and anything else give a reason. The width and
    the height are read from the header and must be from 1 to 20000. The size limit of a file is the caller's."""
    if not isinstance(raw, (bytes, bytearray)):
        return None, "not-an-image"
    raw = bytes(raw)
    if raw[:8] == PNG_SIGNATURE:
        kind, size = "png", png_size(raw)
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        kind, size = "gif", gif_size(raw)
    elif raw[:3] == b"\xff\xd8\xff":
        kind, size = "jpeg", jpeg_size(raw)
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind, size = "webp", webp_size(raw)
    else:
        return None, "svg" if b"<svg" in raw[:2048].lower() else "not-an-image"
    if size is None:
        return None, "not-an-image"
    if not (1 <= size[0] <= MAX_SIDE and 1 <= size[1] <= MAX_SIDE):
        return None, "bad-size"
    return kind, size[0], size[1]


# ---------------------------------------------------------------- the check of the source

def source_problem(repo, ref, record_repo, code_repo):
    """"not-allowed-source" when the repository is neither the record's nor the code repository, or when the ref is
    neither a commit (7 to 40 lowercase hexadecimal digits) nor a tag name; else None. A branch name is not a version."""
    if not recortex.valid_repo(repo) or not isinstance(ref, str):
        return "not-allowed-source"
    allowed = [name.lower() for name in (record_repo, code_repo) if recortex.valid_repo(name)]
    if repo.lower() not in allowed:
        return "not-allowed-source"
    if COMMIT.fullmatch(ref):
        return None
    if TAG.fullmatch(ref) and ".." not in ref and ref.lower() not in record_format.BRANCH_REFS:
        return None
    return "not-allowed-source"


def path_problem(path):
    """"bad-path" when the path is not made of non-empty parts from [A-Za-z0-9_.@+-], none of them . or .., with no
    leading slash, in at most 400 characters; else None."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH:
        return "bad-path"
    for part in path.split("/"):
        if part in ("", ".", "..") or PART.fullmatch(part) is None:
            return "bad-path"
    return None


# ---------------------------------------------------------------- the cache

def hashed(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_folder(record_repo, *parts, create):
    """The private folder figures/<parts> beside the index of `record_repo`, or None when it is missing (create is
    False) or cannot be used (a symlink, another owner, loose permissions)."""
    try:
        return storage.folder(storage.cache_top(), storage.repo_folder(record_repo), FOLDER, *parts, create=create)
    except (storage.StorageError, OSError):
        return None


def read_cached(record_repo, commit, path):
    """The bytes of a cached picture, judged again, or None."""
    folder = cache_folder(record_repo, commit, create=False)
    if folder is None:
        return None
    target = os.path.join(folder, hashed(path))
    try:
        info = storage.check_path(target, "file")
        if not info or info.st_size > MAX_IMAGE:
            return None
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_IMAGE + 1)
    except (storage.StorageError, OSError):
        return None
    return raw if len(raw) <= MAX_IMAGE and inspect(raw)[0] else None


def write_cached(record_repo, commit, path, raw):
    """Keep a checked picture: a fresh temporary file with mode 0600, moved into place. A cache that cannot be written
    costs nothing but the next fetch."""
    folder = cache_folder(record_repo, commit, create=True)
    if folder is None:
        return
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".part-")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
        os.replace(temporary, os.path.join(folder, hashed(path)))  # replaces a symlink there, never follows it
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def read_alias(record_repo, repo, ref):
    folder = cache_folder(record_repo, REFS, create=False)
    if folder is None:
        return None
    try:
        text = storage.read_file(os.path.join(folder, hashed(repo.lower() + "\0" + ref)))
    except (storage.StorageError, OSError, UnicodeDecodeError):
        return None
    commit = (text or "").strip()
    return commit if FULL_COMMIT.fullmatch(commit) else None


def write_alias(record_repo, repo, ref, commit):
    folder = cache_folder(record_repo, REFS, create=True)
    if folder is None:
        return
    try:
        storage.write_file(os.path.join(folder, hashed(repo.lower() + "\0" + ref)), commit + "\n")
    except (storage.StorageError, OSError):
        pass


# ---------------------------------------------------------------- the fetch

class Stop(Exception):
    """One picture's end, with its reason code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def call(args, deadline, binary=False):
    """gh through record_store.github() within what is left of the budget. Raises Stop("timeout") when nothing is left or
    gh is too slow, Stop("not-found") for a 404 and Stop("fetch-failed") for any other failure; no text of gh or of
    GitHub is kept."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise Stop("timeout")
    try:
        return record_store.github(args, timeout=max(1, int(left + 0.999)), binary=binary)
    except recortex.GhTimeout:
        raise Stop("timeout")
    except recortex.GhError as error:
        raise Stop("not-found" if "HTTP 404" in str(error) else "fetch-failed")
    except (ValueError, OSError):  # a gh command that does not split, or cannot be run
        raise Stop("fetch-failed")


def resolve(repo, ref, deadline):
    """The 40-digit commit of a tag or short commit, through GitHub."""
    try:
        answer = call(["api", "repos/%s/commits/%s" % (repo, ref), "--jq", ".sha"], deadline)
    except Stop as stop:
        raise Stop("unknown-tag" if stop.reason == "not-found" else stop.reason)
    commit = answer.strip() if isinstance(answer, str) else ""
    if not FULL_COMMIT.fullmatch(commit):
        raise Stop("unknown-tag")
    return commit


def get_file(repo, commit, path, deadline):
    raw = call(["api", "repos/%s/contents/%s?ref=%s" % (repo, path, commit), "-H", "Accept: application/vnd.github.raw"],
               deadline, binary=True)
    return raw if isinstance(raw, bytes) else b""


def wanted(model):
    """[(repo, ref, path)] of the pictures a model asks for, in first-seen order and without repeats: the figure
    blocks of its threads that name a path, and the figures of its memories that are found and shown. An empty
    repository is the record's own (model["repo"]), as the page reads it. The values are untrusted until load()."""
    out, seen = [], set()
    home = model.get("repo") if isinstance(model, dict) else None

    def add(item):
        if not isinstance(item, dict) or item.get("kind") != "figure":
            return
        repo, ref, path = item.get("repo") or home, item.get("ref"), item.get("path")
        if not all(isinstance(value, str) for value in (repo, ref, path)) or not path:
            return
        if (repo, ref, path) not in seen:
            seen.add((repo, ref, path))
            out.append((repo, ref, path))

    threads = model.get("threads") if isinstance(model, dict) else None
    for thread in threads if isinstance(threads, list) else []:
        blocks = thread.get("blocks") if isinstance(thread, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            add(block)
    sheet = model.get("sheet") if isinstance(model, dict) else None
    then = sheet.get("then_figures") if isinstance(sheet, dict) else None
    for item in then if isinstance(then, list) else []:   # the figures that an earlier text of the manuscript view had
        add(item)
    memories = model.get("memories") if isinstance(model, dict) else None
    for memory in memories if isinstance(memories, list) else []:
        evidence = memory.get("evidence") if isinstance(memory, dict) else None
        for item in evidence if isinstance(evidence, list) else []:
            if isinstance(item, dict) and item.get("found") and item.get("shown"):
                add(item)
    return out


def load(wanted_items, record_repo, code_repo, fetch, budget_seconds=60):
    """(images, reasons) for the pictures in `wanted_items`, a list of (repo, ref, path).

    images maps each picture that may be embedded to its bytes, checked as above; reasons maps every other wanted
    item to a code of REASONS. With fetch False nothing is fetched and the cache alone answers. With it True,
    GitHub is asked only for what the cache lacks, within `budget_seconds` in all. Never raises for odd input."""
    if record_store.is_local(record_repo):   # no repository to fetch from: every picture falls back as one that cannot be embedded
        return {}, {tuple(item): "local-record" for item in wanted_items if isinstance(item, (list, tuple)) and len(item) == 3
                    and all(isinstance(part, str) for part in item)}
    images, reasons, total = {}, {}, 0
    deadline = time.monotonic() + max(0.0, float(budget_seconds) if isinstance(budget_seconds, (int, float)) else 0.0)
    resolved = {}
    for item in wanted_items if isinstance(wanted_items, (list, tuple)) else []:
        try:
            repo, ref, path = item
            key = (repo, ref, path)
            hash(key)
        except (TypeError, ValueError):
            continue
        if key in images or key in reasons:
            continue
        try:
            raw, fresh = one(key, record_repo, code_repo, fetch, deadline, resolved)
            if total + len(raw) > MAX_TOTAL:
                raise Stop("budget")
            images[key] = raw
            total += len(raw)
            if fresh:  # what is refused is not kept
                write_cached(record_repo, fresh, key[2], raw)
        except Stop as stop:
            reasons[key] = stop.reason
        except Exception:  # an odd input is a fallback, never a failed build
            reasons[key] = "fetch-failed"
    return images, reasons


def one(key, record_repo, code_repo, fetching, deadline, resolved):
    """(the checked bytes of one picture, the commit to keep them under or None when they came from the cache), or
    raises Stop(reason)."""
    repo, ref, path = key
    problem = source_problem(repo, ref, record_repo, code_repo) or path_problem(path)
    if problem:
        raise Stop(problem)
    if FULL_COMMIT.fullmatch(ref):
        commit = ref
    elif not fetching:
        commit = read_alias(record_repo, repo, ref)
        if commit is None:
            raise Stop("offline")
    else:
        if (repo.lower(), ref) not in resolved:  # once for each tag, a failure too
            try:
                resolved[(repo.lower(), ref)] = resolve(repo, ref, deadline)
                write_alias(record_repo, repo, ref, resolved[(repo.lower(), ref)])
            except Stop as stop:
                resolved[(repo.lower(), ref)] = stop
        commit = resolved[(repo.lower(), ref)]
        if isinstance(commit, Stop):
            raise Stop(commit.reason)
    raw = read_cached(record_repo, commit, path)
    if raw is not None:
        return raw, None
    if not fetching:
        raise Stop("offline")
    raw = get_file(repo, commit, path, deadline)
    if len(raw) > MAX_IMAGE:
        raise Stop("too-big")
    kind, *rest = inspect(raw)
    if kind is None:
        raise Stop(rest[0])
    return raw, commit


def lines(reasons):
    """One fixed line per distinct reason, for the command's output, with how many pictures it holds."""
    counts = {}
    for code in reasons.values():
        counts[code] = counts.get(code, 0) + 1
    return ["%d figure%s shown without the picture: %s" % (count, "" if count == 1 else "s", REASONS.get(code, ""))
            for code, count in sorted(counts.items()) if code in REASONS]

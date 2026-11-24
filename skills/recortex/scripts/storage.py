"""Where the plugin keeps its local files, and the checks that keep a planted file from being read.

The index and the hook state never live in the project tree, which a cloned
repository controls. They live under two top folders, the same for every entry
point (hooks, the session's terminal, the user's shell):
$XDG_CACHE_HOME/recortex for the index and
$XDG_STATE_HOME/recortex for the state and the key, by default under
~/.cache and ~/.local/state. CLAUDE_PLUGIN_DATA is not used: Claude Code sets it
for hooks but not for the session's Bash tool, so it would split the storage
between the two. An index left in an older plugin data folder is ignored; the
first sync builds a new one.

Each top folder is <base>/recortex. Whatever is there gets every check below; a
symlink or a file at that name is refused.

RECORTEX_DATA_HOME is for tests only, and is read only when RECORTEX_TESTING=1
is also set: the index and the state then go to $RECORTEX_DATA_HOME/recortex (the
key stays under XDG_STATE_HOME). Like every top folder, only the folder the
plugin names is created or tightened, never a folder above it.

A third top folder, $XDG_CONFIG_HOME/recortex (by default ~/.config/recortex),
is the folder of the user's
trusted list. The plugin keeps one file of its own in its private folder: the
user's approvals of field sheets (field_sheet.py), under the same checks,
written by one process at a time (a lock file beside it). The hooks only read
it, without creating or tightening anything.

Inside the top folder the plugin keeps everything in a private folder it creates
and controls, `private/`, with mode 0700:

- top folder: a real folder (not a symlink), owned by the user or by root. A top
  folder the user owns that was created with a loose umask (group-writable with
  umask 0002) is tightened to 0700, and the plugin goes on. A top folder owned by another user, or by root and
  writable by others, is refused.
- private/ and everything below it: real folders and files owned by the user,
  with no permissions for group or others (folders 0700, files 0600), checked at
  every read and write.

Files are written to a fresh temporary file with mode 0600 and moved into place,
and read without following symlinks. remove_file and remove_folder (below) take away what the plugin made, through the
open folder and with the same checks, never through a link. A per-user key in the state folder lets the
plugin recognize the index files it created; an index without the right mark is
not read, so a swapped folder or a forged index is refused.
"""

import hashlib
import hmac
import os
import stat
import tempfile

import names

PRIVATE = "private"
KEY_NAME = "key"


class StorageError(Exception):
    """A folder or file the plugin would use is not safe to use.

    `kind` names the reason in a fixed word (symlink, owner, permissions, parent,
    not-folder, key, refused, changed, name); hooks print a fixed sentence per
    kind and never the message, which may hold paths from the environment.
    """

    def __init__(self, message, kind="unsafe"):
        super().__init__(message)
        self.kind = kind


def _env_dir(name, default):
    value = os.environ.get(name)
    return value if value and os.path.isabs(value) else default


def test_home():
    """$RECORTEX_DATA_HOME/recortex, for tests only: one top folder for the index and the state. Read only when
    RECORTEX_TESTING=1 is also set and the value is an absolute path; None otherwise."""
    value = names.env("DATA_HOME")
    if names.env("TESTING") != "1" or not value or not os.path.isabs(value):
        return None
    return os.path.join(value, "recortex")


def choose_top(base):
    """<base>/recortex. A symlink or a file at that path is refused by check_top."""
    return os.path.join(base, names.NAME)


def records_top():
    """The top folder of the local records the plugin keeps itself: $XDG_DATA_HOME/recortex, by default under ~/.local/share.
    In a test run (RECORTEX_TESTING=1 with RECORTEX_DATA_HOME) it is test_home(), as for the cache and the state."""
    return test_home() or choose_top(_env_dir("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")))


def cache_top():
    """The top folder of the index: $XDG_CACHE_HOME/recortex, the same for hooks and the terminal."""
    return test_home() or choose_top(_env_dir("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")))


def state_top(for_key=False):
    """The top folder of the state: $XDG_STATE_HOME/recortex. The key always lives there."""
    if not for_key and test_home():
        return test_home()
    return choose_top(_env_dir("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state")))


def config_top():
    """The plugin's top folder in the user's configuration folder: $XDG_CONFIG_HOME/recortex,
    where the trusted list is. It holds the field approvals in its private folder. RECORTEX_DATA_HOME and
    RECORTEX_TRUSTED_FILE do not move it."""
    return choose_top(_env_dir("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")))


def status_path(root):
    """The file where the hooks write whether the record is on for the project folder `root`, for the status line:
    <state top>/private/status/<first 32 hex digits of the SHA-256 of the folder's path>.json, under the same top as
    every other state file (state_top). The status line, which imports nothing, repeats this rule in statusline.py and a
    test holds the two equal."""
    import hashlib
    folder_path = folder(state_top(), "status")
    return os.path.join(folder_path, hashlib.sha256(root.encode("utf-8")).hexdigest()[:32] + ".json")


def repo_folder(repo):
    name = repo.replace("/", "__")
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise StorageError("unusable repository name %r" % repo, "name")
    return name


def _uid():
    return os.getuid() if hasattr(os, "getuid") else None


def check_top(path, create=True, tighten=True):
    """Check the top folder, tightening it to 0700 when the user owns it. False when it is missing and create is False.

    With tighten=False (for --doctor) a loose top folder owned by the user is accepted as it is and left unchanged.
    """
    parent = os.path.dirname(os.path.abspath(path))
    try:
        above = os.stat(parent)
        sticky = above.st_mode & stat.S_ISVTX
        if above.st_mode & stat.S_IWOTH and not sticky:
            raise StorageError("%s is in %s, which anyone can write to without the sticky bit" % (path, parent),
                               "parent")
        if above.st_mode & stat.S_IWGRP and not sticky and _uid() is not None and above.st_uid != _uid():
            raise StorageError("%s is in %s, which a group can write to and another user owns" % (path, parent),
                               "parent")
    except FileNotFoundError:
        pass
    if not os.path.lexists(path):
        if not create:
            return False
        os.makedirs(path, mode=0o700, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.path.islink(path):
            raise StorageError("%s is a symlink; the plugin does not follow symlinks in its storage" % path, "symlink")
        raise StorageError("%s is not a folder the plugin can open" % path, "not-folder")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise StorageError("%s is not a folder" % path, "not-folder")
        uid = _uid()
        if uid is None or info.st_uid == uid:
            if info.st_mode & 0o077 and tighten:
                # created with a loose umask; the user owns it, so tighten it, through the open descriptor
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o700)
                else:
                    os.chmod(path, 0o700)
        elif info.st_uid == 0:
            if info.st_mode & 0o022:
                raise StorageError("%s is owned by root and writable by others" % path, "permissions")
        else:
            raise StorageError("%s is owned by another user" % path, "owner")
    finally:
        os.close(descriptor)
    return True


def check_path(path, kind="folder"):
    """Check a private folder or file: real, owned by the user, no permissions for group or others.

    Returns False when it does not exist, else the os.lstat result it checked;
    raises StorageError when it is not safe.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise StorageError("%s is a symlink; the plugin does not follow symlinks in its storage" % path, "symlink")
    if kind == "folder" and not stat.S_ISDIR(info.st_mode) or kind == "file" and not stat.S_ISREG(info.st_mode):
        raise StorageError("%s is not a %s" % (path, kind), "not-folder")
    uid = _uid()
    if uid is not None and info.st_uid != uid:
        raise StorageError("%s is not owned by the current user" % path, "owner")
    if info.st_mode & 0o077:
        raise StorageError("%s has permissions for group or others (mode %o); the plugin keeps it private. Run: "
                           "chmod %s %s" % (path, info.st_mode & 0o777, "700" if kind == "folder" else "600", path),
                           "permissions")
    return info


def folder(top, *parts, create=True, tighten=True):
    """top/private/parts, created with mode 0700 where missing and checked at every level. None when missing."""
    if not check_top(top, create, tighten):
        return None
    path = top
    for part in (PRIVATE,) + parts:
        path = os.path.join(path, part)
        if not check_path(path):
            if not create:
                return None
            os.makedirs(path, mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
            check_path(path)
    return path


def read_file(path):
    """Text of a private file read without following a symlink, or None when it does not exist."""
    if not check_path(path, "file"):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return handle.read()


def write_file(path, text):
    """Write through a fresh temporary file with mode 0600 in the same private folder, then move it into place."""
    target_folder = os.path.dirname(path)
    check_path(target_folder)
    if os.path.lexists(path):
        check_path(path, "file")
    descriptor, temporary = tempfile.mkstemp(dir=target_folder, prefix=".part-")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def key_path():
    return os.path.join(state_top(for_key=True), PRIVATE, KEY_NAME)


def link_new_file(path, text):
    """Create the private file `path` holding `text` in one step: written whole to a fresh temporary file with mode 0600 in
    the same private folder, then hard-linked into place, so that nobody sees it half written and of two creators exactly one
    wins (a link is atomic on NFS and on the cluster file systems, where an exclusive open is not). True when this call
    created it, False when something is there already."""
    target_folder = os.path.dirname(path)
    check_path(target_folder)
    descriptor, temporary = tempfile.mkstemp(dir=target_folder, prefix=".part-")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def replace_file(path, text):
    """write_file for a target that may be a damaged file or a link: what is there is not looked at and never followed, it is
    replaced (the rename puts the new file in the place of the name). The folder is checked as for write_file."""
    target_folder = os.path.dirname(path)
    check_path(target_folder)
    descriptor, temporary = tempfile.mkstemp(dir=target_folder, prefix=".part-")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def key_identity():
    """(real path, inode) of the user key as this host sees it, the identity that an approval follows from host to host
    on a shared home folder. The device number is left out on purpose: a network mount gives each client its own.
    Raises StorageError("...", "no-key") when there is no key, and as check_path does when the file is not safe."""
    path = key_path()
    info = check_path(path, "file")
    if not info:
        raise StorageError("no key yet at %s" % path, "no-key")
    return os.path.realpath(path), info.st_ino


def user_key(create=True, tighten=True):
    """The per-user secret that marks index files this plugin created; made on first use."""
    private = folder(state_top(for_key=True), create=create, tighten=tighten)
    if private is None:
        raise StorageError("no key yet at %s" % key_path(), "no-key")
    path = os.path.join(private, KEY_NAME)
    text = read_file(path)
    if text is None:
        if not create:
            raise StorageError("no key yet at %s" % path, "no-key")
        # Write the whole key to a temporary file, then link it into place: a reader never sees a half-written key,
        # and of two first uses at the same moment one link wins and both read the same key.
        descriptor, temporary = tempfile.mkstemp(dir=private, prefix=".key-")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(os.urandom(32).hex() + "\n")
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        text = read_file(path)
    key = (text or "").strip()
    if len(key) < 32 or not all(c in "0123456789abcdef" for c in key):
        raise StorageError("the key at %s is empty or damaged; the plugin does not replace it on its own. Move it "
                           "aside by hand; the indexes made with it are then rebuilt" % path, "key")
    return key.encode("ascii")


def same_file(path, before):
    """True when `path` is still the regular file (device and inode) that `before` describes."""
    try:
        now = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(now.st_mode) and (now.st_dev, now.st_ino) == (before.st_dev, before.st_ino)


def lstat_file(path):
    return os.lstat(path)


def mark(path, repo, create=True, tighten=True):
    """The mark an index file of `repo` at `path` carries when this plugin, for this user, created it."""
    message = ("%s\n%s" % (os.path.abspath(path), repo.lower())).encode("utf-8")
    return hmac.new(user_key(create, tighten), message, hashlib.sha256).hexdigest()


def describe(path):
    """For --doctor: (exists, owner, mode, problem or "") of a path, never raising and never reading contents."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False, "", "", "missing"
    except OSError as error:
        return False, "", "", str(error)
    try:
        import pwd
        owner = pwd.getpwuid(info.st_uid).pw_name
    except (ImportError, KeyError):
        owner = str(info.st_uid)
    problem = ""
    if stat.S_ISLNK(info.st_mode):
        problem = "symlink"
    elif _uid() is not None and info.st_uid not in (_uid(), 0):
        problem = "owned by another user"
    return True, owner, "%o" % (info.st_mode & 0o7777), problem


# ---------------------------------------------------------------- the texts and versions kept beside the index

TEXTS = "texts"        # texts/<issue>/<sha256>.md: the body the plugin last wrote, to tell what the author changed
VERSIONS = "versions"  # versions/<commit>/<issue>.md: a vouched version of a topic body, fetched for the mind page
WRITE = "write"        # write/<sha256 of the base>.json: what write.py start decided for that base (the issue, the paragraphs
                       # that are the author's); a drafting agent can write the working folder, never this


def cache_folder(repo, *parts, create=True):
    """<index folder>/<parts>, a private folder (mode 0700) made where missing; None when it is missing and create is
    False. Raises StorageError for a folder that is not safe to use."""
    return folder(cache_top(), repo_folder(repo), *parts, create=create)


def read_cached(repo, parts, name):
    """The text of <index folder>/<parts>/<name>, or None when it is not there. Nothing is made; a symlink or a file
    that others can read is refused."""
    path = cache_folder(repo, *parts, create=False)
    if path is None:
        return None
    return read_file(os.path.join(path, name))


def write_cached(repo, parts, name, text):
    """Write <index folder>/<parts>/<name> through a fresh temporary file with mode 0600."""
    write_file(os.path.join(cache_folder(repo, *parts), name), text)


# ---------------------------------------------------------------- removing what the plugin made

REMOVE_MAX = 200   # the files a folder may hold for remove_folder to take it away: more is not what the plugin made


def _plain_name(name):
    """`name` when it is one plain name inside a folder (no separator, not "." or ".."), else a StorageError."""
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise StorageError("%r is not a plain name inside a private folder" % (name,), "name")
    return name


def _can_remove_without_links():
    return (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd and os.unlink in os.supports_dir_fd and os.rmdir in os.supports_dir_fd
            and os.listdir in os.supports_fd)


def _check_entry(label, info, kind):
    """The rules of check_path for an entry that was read through its folder's open descriptor (an os.stat result that
    did not follow a link): real, of the kind, owned by the user, no permissions for group or others."""
    if stat.S_ISLNK(info.st_mode):
        raise StorageError("%s is a symlink; the plugin does not follow symlinks in its storage" % label, "symlink")
    if kind == "folder" and not stat.S_ISDIR(info.st_mode) or kind == "file" and not stat.S_ISREG(info.st_mode):
        raise StorageError("%s is not a %s" % (label, kind), "not-folder")
    uid = _uid()
    if uid is not None and info.st_uid != uid:
        raise StorageError("%s is not owned by the current user" % label, "owner")
    if info.st_mode & 0o077:
        raise StorageError("%s has permissions for group or others (mode %o); nothing was removed"
                           % (label, info.st_mode & 0o777), "permissions")


def _open_private_folder(path):
    """The descriptor of the private folder `path`, opened without following a link and checked against the folder
    check_path saw (its device and inode), or None when there is no such folder. The caller closes the descriptor."""
    before = check_path(path)
    if not before:
        return None
    if not _can_remove_without_links():
        raise StorageError("this system cannot remove files without following links, so nothing was removed", "refused")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise StorageError("%s could not be opened without following a link" % path,
                           "symlink" if os.path.islink(path) else "not-folder")
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(descriptor)
        raise StorageError("%s changed while it was being opened; nothing was removed" % path, "changed")
    return descriptor


def remove_file(parent, name):
    """Remove the regular file `name` of the private folder `parent`. True when it was removed, False when there was none.

    The folder is opened without following a link, and the file is looked at and removed through that descriptor, so
    nothing is removed through a link at either place. A file that is a link, is not a regular file, belongs to another
    user or can be read by others is refused (StorageError) and stays."""
    descriptor = _open_private_folder(parent)
    if descriptor is None:
        return False
    try:
        name = _plain_name(name)
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        _check_entry(os.path.join(parent, name), info, "file")
        os.unlink(name, dir_fd=descriptor)
        return True
    finally:
        os.close(descriptor)


def remove_folder(parent, name):
    """Remove the private folder `name` of the private folder `parent`, with the regular files in it. True when it was
    removed, False when there was none.

    Everything is looked at before the first file goes: the folder must be a real folder of the user's with no
    permissions for group or others, and holds only regular files of the user's (at most REMOVE_MAX), never a link, a
    folder or another kind of file. If anything else is in it, StorageError is raised and nothing is removed. Nothing is
    followed or removed through a link: the two folders are opened without following one and every name is looked at and
    removed through the descriptor of its folder. Never recurses."""
    outer = _open_private_folder(parent)
    if outer is None:
        return False
    try:
        name = _plain_name(name)
        path = os.path.join(parent, name)
        try:
            before = os.stat(name, dir_fd=outer, follow_symlinks=False)
        except FileNotFoundError:
            return False
        _check_entry(path, before, "folder")
        try:
            inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=outer)
        except OSError:
            raise StorageError("%s could not be opened without following a link" % path, "not-folder")
        try:
            opened = os.fstat(inner)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise StorageError("%s changed while it was being opened; nothing was removed" % path, "changed")
            entries = os.listdir(inner)
            if len(entries) > REMOVE_MAX:
                raise StorageError("%s holds more than %d entries, which the plugin did not put there" % (path, REMOVE_MAX),
                                   "refused")
            for entry in entries:
                _check_entry(os.path.join(path, entry), os.stat(_plain_name(entry), dir_fd=inner, follow_symlinks=False),
                             "file")
            for entry in entries:
                os.unlink(entry, dir_fd=inner)
        finally:
            os.close(inner)
        os.rmdir(name, dir_fd=outer)
        return True
    finally:
        os.close(outer)


# ---------------------------------------------------------------- the local record (1.9.0)
#
# A local record is a folder of Markdown files (local_store.py). Where it lives decides the rules: the folder the plugin makes
# under records_top() follows the private rules above (mode 0700 and 0600). A folder inside the project (`.recortex`) or one the
# user named is also read by sync tools and by git, which keep no modes: a clone gives 0644 and 0755. For those two places the
# rules differ in one stated point, the read bits. Everything else is as strict: no link anywhere on the way from `/`, every
# folder and file owned by the user, no write bit for group or others, and a folder above that another account could replace
# is a problem (observer_sources' component rule, with its codes). What the plugin writes has mode 0600 (folders 0700).
# Every file of a record is opened and made here, and nothing of what a file holds is echoed in a message.

PLACES = ("own", "project", "chosen")
RECORD_BINDING_FORM = 1
RECORD_BINDING_CAP = 262144


def own_record_folder(record_id, create=False):
    """The folder of a record of the default place, <records top>/private/records/<id>, or None when it is missing and
    create is False. Private rules."""
    return folder(records_top(), names.RECORDS, record_id, create=create)


def record_path(root):
    """The folder `.recortex` of the project at `root`: its real parent and the name. The last component is looked at with
    lstat by check_record_folder, so a link there is refused."""
    return os.path.join(os.path.realpath(root), names.RECORD_DIR)


def _record_problem(path, info, record_folder):
    """The code of observer_sources' component rule for one folder of the walk to the record folder, or None. The record
    folder and what is under it count as inside (the user alone); the folders above may belong to root."""
    import observer_sources
    return observer_sources._component_problem(path, info, record_folder)


RECORD_KINDS = {"source-symlink": "symlink", "source-path": "not-folder", "source-owner": "ancestor-owner",
                "source-permissions": "ancestor-permissions"}


def check_record_folder(path, place="chosen", create=False):
    """Check the folder of a local record and return its real path. `place` is "own" (private rules), "project" or "chosen"
    (the record rules). With `create` a missing last folder is made with mode 0700 (the rest of the way must exist).

    Raises StorageError with a kind: symlink, not-folder, owner, permissions, ancestor-owner or ancestor-permissions (a folder
    above that another account owns or can write: the user may trust it exactly as it is, observer_sources.Trust), missing,
    name. Nothing of the path is put in the message."""
    if place not in PLACES or not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path \
            or "\x00" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise StorageError("the record folder's name is not usable", "name")
    if place == "own":
        info = check_path(path)
        if not info:
            if not create:
                raise StorageError("the record folder is missing", "missing")
            os.makedirs(path, mode=0o700, exist_ok=True)
            check_path(path)
        return os.path.realpath(path)
    try:
        last = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise StorageError("the record folder is missing", "missing")
        parent = os.path.dirname(path)
        if os.path.realpath(parent) != parent:
            raise StorageError("a folder on the way to the record folder is a link", "symlink")
        try:
            os.mkdir(path, 0o700)
            last = os.lstat(path)
        except OSError:
            raise StorageError("the record folder cannot be made", "missing")
    if stat.S_ISLNK(last.st_mode):
        raise StorageError("the record folder is a symlink", "symlink")
    if not stat.S_ISDIR(last.st_mode):
        raise StorageError("the record folder is not a folder", "not-folder")
    if _uid() is not None and last.st_uid != _uid():
        raise StorageError("the record folder belongs to another user", "owner")
    if last.st_mode & 0o022:
        raise StorageError("the record folder can be written by group or others", "permissions")
    # the walk from the folder to `/`: a link anywhere is refused, and a folder above must not be one another account can replace
    current = path
    while True:
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            raise StorageError("a folder on the way to the record folder is a link", "symlink")
        problem = _record_problem(current, info, path)
        if problem in ("source-owner", "source-permissions") and current != path:
            import observer_sources
            if not observer_sources.Trust().accepts(current, info, path, None):
                raise StorageError("a folder above the record folder can be replaced by another account",
                                   RECORD_KINDS[problem])
        elif problem:
            raise StorageError("a folder on the way to the record folder is not usable", RECORD_KINDS.get(problem, "unsafe"))
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return path


def _open_folder_nofollow(path):
    """A descriptor of the folder `path`, opened without following a link, or a StorageError."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except OSError:
        if os.path.islink(path):
            raise StorageError("a folder of the record is a symlink", "symlink")
        raise StorageError("a folder of the record cannot be opened", "not-folder")


def _entry_kind(info):
    if stat.S_ISLNK(info.st_mode):
        return "link"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "other"


def scan_record_folder(path, sub=""):
    """The entries of `path`/`sub` as (name, size, mtime_ns, kind) from lstat, kind "file", "dir", "link" or "other", in name
    order, without opening a file or following a link. [] when the folder is missing. Raises StorageError when the folder
    itself is a link or cannot be listed."""
    full = os.path.join(path, sub) if sub else path
    try:
        os.lstat(full)
    except FileNotFoundError:
        return []
    descriptor = _open_folder_nofollow(full)
    try:
        rows = []
        try:
            use_fd = os.scandir in getattr(os, "supports_fd", ())
            iterator = os.scandir(descriptor if use_fd else full)
        except OSError:
            raise StorageError("a folder of the record cannot be listed", "not-folder")
        with iterator:
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rows.append((entry.name, info.st_size, info.st_mtime_ns, _entry_kind(info)))
        return sorted(rows)
    finally:
        os.close(descriptor)


def read_record_file(path, limit):
    """The bytes of one file of a record, or None when it does not exist. Only a regular file that the user owns, that group
    and others cannot write, of at most `limit` bytes, read through one descriptor that is looked at (fstat) before and
    after and is still the file at `path` (a swap, a growing file and a link are refused). A pipe or a device at the name is
    opened without waiting and refused. Raises StorageError (kind symlink, not-folder, owner, permissions, changed, size);
    nothing of the file is echoed."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        if os.path.islink(path):
            raise StorageError("a file of the record is a symlink", "symlink")
        raise StorageError("a file of the record cannot be opened", "not-folder")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise StorageError("a name of the record is not a regular file", "not-folder")
        if _uid() is not None and before.st_uid != _uid():
            raise StorageError("a file of the record belongs to another user", "owner")
        if before.st_mode & 0o022:
            raise StorageError("a file of the record can be written by group or others", "permissions")
        if before.st_size > limit:
            raise StorageError("a file of the record is larger than the plugin reads", "size")
        chunks, total = [], 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise StorageError("a file of the record is larger than the plugin reads", "size")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev) != \
                (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) or total != before.st_size:
            raise StorageError("a file of the record changed while it was read", "changed")
        if not same_file(path, before):
            raise StorageError("a file of the record was replaced while it was read", "changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_record_temporary(target_folder, data):
    """A fresh file `.part-...` in `target_folder` with mode 0600 that holds `data` whole; its path."""
    descriptor, temporary = tempfile.mkstemp(dir=target_folder, prefix=".part-")
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while len(view):
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    os.close(descriptor)
    return temporary


def _check_record_target(path):
    """The folder of `path` is a real folder of the user's that group and others cannot write."""
    target = os.path.dirname(path)
    info = os.lstat(target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StorageError("a folder of the record is not a real folder", "symlink" if stat.S_ISLNK(info.st_mode) else "not-folder")
    if _uid() is not None and info.st_uid != _uid():
        raise StorageError("a folder of the record belongs to another user", "owner")
    if info.st_mode & 0o022:
        raise StorageError("a folder of the record can be written by group or others", "permissions")
    return target


def replace_record_file(path, data):
    """Put `data` (bytes) at `path` whole: through a fresh temporary file with mode 0600 in the same folder, then moved into
    place. What is at the name (a link included) is replaced, never followed."""
    target = _check_record_target(path)
    temporary = _write_record_temporary(target, data)
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def link_new_record_file(path, data):
    """Create the file `path` holding `data` (bytes) in one step, never replacing a file: a fresh temporary file hard-linked
    into place, so that of two creators exactly one wins and nobody sees it half written. Where a link is refused (some synced
    or network folders) the final name is made with an exclusive open instead, and written; a reader treats a file with no seal
    that is young as not yet there. True when this call made it, False when the name is taken."""
    target = _check_record_target(path)
    temporary = _write_record_temporary(target, data)
    try:
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            return False
        try:
            view = memoryview(data)
            while len(view):
                view = view[os.write(descriptor, view):]
        finally:
            os.close(descriptor)
        return True
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def make_record_folder(path):
    """A folder of a record (a topic's own folder) with mode 0700 where missing; the folder above must be the record's."""
    _check_record_target(path)
    if not os.path.lexists(path):
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise StorageError("a folder of the record is not a real folder", "symlink" if stat.S_ISLNK(info.st_mode) else "not-folder")
    return path


WRITE_WAIT = 30


def record_lock(record_id):
    """The write lock of one record, a context manager: an exclusive flock on `write.lock` in the record's folder of the state
    top (never in the record folder, which a sync tool would carry to another machine). Waits about WRITE_WAIT seconds, then
    raises StorageError("busy")."""
    import contextlib
    import time
    try:
        import fcntl
    except ImportError:   # pragma: no cover - platforms without fcntl have no lock to take
        fcntl = None

    @contextlib.contextmanager
    def held():
        place = folder(state_top(), names.LOCAL_STATE, record_id)
        path = os.path.join(place, names.WRITE_LOCK)
        if os.path.lexists(path):
            check_path(path, "file")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            if fcntl:
                deadline = time.monotonic() + WRITE_WAIT
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise StorageError("another process holds the record's write lock", "busy")
                        time.sleep(0.05)
            yield
        finally:
            os.close(descriptor)
    return held()


# The machine's record of which folder belongs to which record id: local-records.json in the private folder of the state top
# (beside the key). A row binds the id to the folder's real path, owner, group, mode and place, with an HMAC under the user key;
# a file restored or edited from elsewhere binds nothing. The hooks only read it.

def _binding_mac(row):
    message = json_text(["record", row["id"], row["path"], row["uid"], row["gid"], row["mode"], row["place"]]).encode("utf-8")
    return hmac.new(user_key(create=False, tighten=False), message, hashlib.sha256).hexdigest()[:32]


def json_text(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _binding_file(create=False):
    private = folder(state_top(for_key=True), create=create, tighten=create)
    return None if private is None else os.path.join(private, names.LOCAL_RECORDS)


def _binding_fits(row):
    return (isinstance(row, dict) and set(row) == {"id", "path", "uid", "gid", "mode", "place", "mac"}
            and isinstance(row["id"], str) and len(row["id"]) == 16 and isinstance(row["path"], str)
            and 0 < len(row["path"]) <= 4096 and os.path.isabs(row["path"])
            and all(type(row[key]) is int and 0 <= row[key] < 2 ** 32 for key in ("uid", "gid"))
            and type(row["mode"]) is int and 0 <= row["mode"] <= 0o7777 and row["place"] in PLACES
            and isinstance(row["mac"], str) and len(row["mac"]) == 32)


def record_bindings(record_id=None):
    """The rows that bind record ids to folders and pass their HMAC, as a list of dicts (those of `record_id` when given).
    [] when there is no file, it cannot be read or it does not fit; never raises for a damaged file, never echoes it."""
    import json
    try:
        path = _binding_file()
        text = read_file(path) if path else None
        if not text or len(text) > RECORD_BINDING_CAP:
            return []
        data = json.loads(text)
        rows = data.get("rows") if isinstance(data, dict) and data.get("form") == RECORD_BINDING_FORM else None
        if not isinstance(rows, list):
            return []
        good = []
        for row in rows:
            if _binding_fits(row) and (record_id is None or row["id"] == record_id) \
                    and hmac.compare_digest(row["mac"], _binding_mac(row)):
                good.append(row)
        return good
    except (OSError, ValueError, StorageError, RecursionError):
        return []


def bind_record(record_id, path, place):
    """The user's yes: record that this machine uses the folder `path` (a real path that passed check_record_folder) for
    `record_id`. Keeps the rows of other folders (an in-project record has one row per checkout). Returns the row."""
    import json
    from field_sheet import approvals_lock
    user_key()   # the key exists before a row is signed with it
    info = os.lstat(path)
    row = {"id": record_id, "path": path, "uid": info.st_uid, "gid": getattr(info, "st_gid", 0),
           "mode": stat.S_IMODE(info.st_mode), "place": place}
    with approvals_lock(state_top(for_key=True), names.LOCAL_RECORDS_LOCK):
        kept = [old for old in record_bindings() if (old["id"], old["path"]) != (record_id, path)]
        row["mac"] = _binding_mac(row)
        file_path = _binding_file(create=True)
        write_file(file_path, json.dumps({"form": RECORD_BINDING_FORM, "rows": kept[-63:] + [row]}, sort_keys=True,
                                         indent=1) + "\n")
    return row

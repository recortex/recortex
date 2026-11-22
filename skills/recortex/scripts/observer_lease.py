"""The host lease: at most one host's observer worker works on a project's queue at a time.

With one approval valid on every host of a shared mount (the key-file binding, observer_runtime), two workers on two hosts
could run on one queue at the same moment. The worker's own lock and SQLite rest on file locks, which reach across hosts on
NFS and on Lustre mounted with flock, and stay on the host on Lustre `localflock` and NFS `local_lock`. The lease is an
extra gate in front of that lock, and never replaces it: two workers on one host are still kept apart by the local lock.

One small file, `worker-host.lease`, beside the worker's lock in the observer state folder (storage.py: private folder, mode
0600, no link followed). It holds the host name (a strict pattern), the process id, a random token and the expiry as a whole
number of seconds since the epoch, UTC. It runs 10 minutes, and the holder renews it at each step of the pass.

  taken       this worker holds it: nobody else works
  other       another host holds it and 120 seconds of clock skew have not passed since its expiry: the worker leaves quietly
              and calls no model. A lease that cannot be read or does not fit the form counts as held until its file's
              modification time plus the lease time plus the skew, and then as expired. It is never a reason to crash or
              to post.
  expired     taken over through a claim that only one process can make (a hard link to the lease, the idea of
              index.take_lock, which this file does not touch): one of several contenders wins, the others see a live lease.

The host name is not printed anywhere, because it is not checked record text: status and --doctor say only until when.
Standard library only."""

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import time

import storage

LEASE = "worker-host.lease"
SECONDS = 600       # how long a lease runs; the holder renews it at each step
SKEW = 120          # seconds of clock skew allowed between hosts: another host's lease is expired only this long after
CLAIM_LEFT = 2.0    # seconds a claim may stand before it counts as left by a process that died
TRY_SECONDS = 3.0   # how long acquire tries in all
TRIES = 40          # and at most this many judgments of an expired lease
MAX_BYTES = 512
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
TOKEN = re.compile(r"[0-9a-f]{32}")
HELD = "another host's observer worker holds this folder until %s UTC"
# For --doctor, where the state folder's mount keeps file locks on the host (Lustre localflock, NFS local_lock): fixed words.
LOCAL_LOCKS = ("this folder is on a mount whose file locks do not reach other hosts (localflock or local_lock): SQLite and "
               "the file locks of this mount do not stop two hosts. The host lease keeps the observer's worker to one host, "
               "but hooks that write on two hosts at once can still conflict, so run sessions for one project on one host "
               "at a time")
LOCAL_FLAGS = re.compile(r"(?:^|,)(?:localflock|local_lock=(?:all|flock|posix))(?:,|$)")


def hostname():
    """This host's name in the strict form the lease holds; a name that does not fit is replaced by a fixed form of its
    digest, never written as it is. Tests replace this function to be two hosts."""
    name = socket.gethostname()
    return name if isinstance(name, str) and HOST.fullmatch(name) else \
        "host-" + hashlib.sha256(str(name).encode("utf-8", "replace")).hexdigest()[:16]


def clock():
    return time.time()


def clock_text(seconds):
    """HH:MM (UTC) of a number of seconds, as the fixed line shows it."""
    return time.strftime("%H:%M", time.gmtime(seconds))


def _valid(data):
    return (isinstance(data, dict) and set(data) == {"host", "pid", "token", "expires"}
            and isinstance(data["host"], str) and HOST.fullmatch(data["host"]) is not None
            and type(data["pid"]) is int and 1 <= data["pid"] < 2 ** 31
            and isinstance(data["token"], str) and TOKEN.fullmatch(data["token"]) is not None
            and type(data["expires"]) is int and 0 < data["expires"] < 4102444800)


def _record(host, now):
    return {"host": host, "pid": os.getpid(), "token": secrets.token_hex(16), "expires": int(now) + SECONDS}


def _text(record):
    return json.dumps(record, sort_keys=True) + "\n"


def read(folder):
    """(state, value, identity): ("none", None, None) when there is no lease; ("ok", the record, identity) for a lease of the
    form; ("bad", the file's modification time, identity) for one that cannot be read or does not fit it. The identity is
    (device, inode). Never raises."""
    path = os.path.join(folder, LEASE)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "none", None, None
    except OSError:
        return "bad", clock(), None
    identity = (info.st_dev, info.st_ino)
    try:
        storage.check_path(path, "file")
        if info.st_size > MAX_BYTES:
            raise ValueError("size")
        data = json.loads(storage.read_file(path) or "")
        if _valid(data):
            return "ok", data, identity
    except (storage.StorageError, OSError, ValueError, RecursionError):
        pass
    return "bad", info.st_mtime, identity


def held_until(state, value):
    """The second at which a lease of another host stops holding, with the skew (an unreadable one: its modification time plus
    the lease time plus the skew)."""
    return (value["expires"] if state == "ok" else value + SECONDS) + SKEW


def peek(folder, now=None):
    """None when no other host's lease holds this folder now, else the second until which it holds (without the skew), for the
    fixed line. Read only; the worker asks it before it takes any lock."""
    now = clock() if now is None else now
    state, value, _ = read(folder)
    if state == "none" or state == "ok" and value["host"] == hostname():
        return None
    until = held_until(state, value)
    return None if now > until else until - SKEW


def status(folder, now=None):
    """The fixed line for status and --doctor when another host's worker holds the folder, else "". Never raises."""
    try:
        until = peek(folder, now)
    except (OSError, ValueError, storage.StorageError):
        return ""
    return HELD % clock_text(until) if until is not None else ""


class Lease:
    """What acquire returns. `state` is "taken" (this worker holds it; `token` is its own), "other" (`until` is the second the
    other host's lease ran to) or "unavailable" (the file system cannot make the lease here: the worker goes on under its
    local lock alone, and says so)."""

    def __init__(self, folder, state, token=None, until=None):
        self.folder, self.state, self.token, self.until = folder, state, token, until

    @property
    def held(self):
        return self.state in ("taken", "unavailable")

    def renew(self):
        """Extend the lease by the lease time. True while this worker still holds it: the file is read, its token is checked,
        and a fresh temporary file is renamed over it. False when another host took it (the worker stops its work)."""
        if self.state == "unavailable":
            return True
        state, value, _ = read(self.folder)
        if state != "ok" or value["token"] != self.token or value["host"] != hostname():
            return False
        try:
            storage.write_file(os.path.join(self.folder, LEASE), _text(dict(value, expires=int(clock()) + SECONDS,
                                                                           pid=os.getpid())))
        except (OSError, storage.StorageError):
            return False
        return True

    def release(self):
        """Remove the lease when this worker still holds it (the token is checked). Never raises."""
        if self.state != "taken":
            return
        try:
            state, value, _ = read(self.folder)
            if state == "ok" and value["token"] == self.token:
                storage.remove_file(self.folder, LEASE)
        except (OSError, storage.StorageError):
            pass


def _identity(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_mtime_ns


def _drop_claim(claim, identity):
    """Remove the claim while it is still the one `identity` names; a claim made since by another process stays."""
    if identity is not None and _identity(claim) == identity:
        try:
            os.unlink(claim)
        except FileNotFoundError:
            pass


def _take_over(folder, host, seen):
    """Replace an expired or damaged lease with this host's own. `seen` is the (device, inode) the caller judged. The judge
    is the one process that can link the claim; it checks that the path still names what was judged, and replaces it in one
    step. Returns the new record, or None when the lease is not this worker's to take (another judge, or it changed)."""
    path = os.path.join(folder, LEASE)
    claim = path + ".claim"
    try:
        os.link(path, claim, follow_symlinks=False)   # one judge at a time
    except FileExistsError:
        return None
    except FileNotFoundError:
        return None
    mine = _identity(claim)
    try:
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) != seen:
            return None   # renewed or replaced since it was judged
        record = _record(host, clock())
        storage.replace_file(path, _text(record))
        return record
    except FileNotFoundError:
        return None
    finally:
        _drop_claim(claim, mine)


def acquire(folder):
    """Take the lease for this host, inside the worker's own lock: a Lease. A lease that this host's earlier worker left is
    replaced (the local lock says that no worker of this host is working). Another host's lease that holds is not touched. A
    file system that cannot make the file leaves the worker its local lock alone ("unavailable"); nothing here raises."""
    host = hostname()
    path = os.path.join(folder, LEASE)
    claim = path + ".claim"
    deadline, judged, waiting = time.monotonic() + TRY_SECONDS, 0, None
    try:
        while time.monotonic() < deadline and judged < TRIES:
            state, value, identity = read(folder)
            now = clock()
            if state == "none":
                record = _record(host, now)
                if storage.link_new_file(path, _text(record)):
                    return Lease(folder, "taken", record["token"])
                continue
            if state == "ok" and value["host"] == host:
                record = _record(host, now)   # this host's own: no other worker of it is working, the local lock says so
                if _replace_own(folder, identity, record):
                    return Lease(folder, "taken", record["token"])
                continue
            until = held_until(state, value)
            if now <= until:
                return Lease(folder, "other", until=until - SKEW)
            judged += 1
            try:
                os.lstat(claim)
                seen = _identity(claim)
                if waiting is None or waiting[0] != seen:
                    waiting = (seen, time.monotonic())
                elif time.monotonic() - waiting[1] >= CLAIM_LEFT:
                    _drop_claim(claim, seen)   # the judge died while it held the claim
                    waiting = None
                    continue
                time.sleep(0.02)
                judged -= 1
                continue
            except FileNotFoundError:
                waiting = None
            taken = _take_over(folder, host, identity)
            if taken is not None:
                return Lease(folder, "taken", taken["token"])
        return Lease(folder, "other", until=clock())   # no decision in time: never go on beside a lease that was not judged
    except (OSError, NotImplementedError, storage.StorageError):
        return Lease(folder, "unavailable")


def _replace_own(folder, seen, record):
    """Replace a lease of this host that a worker of it left, while this worker holds the local lock. False when it changed."""
    path = os.path.join(folder, LEASE)
    try:
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) != seen:
            return False
        storage.replace_file(path, _text(record))
        return True
    except FileNotFoundError:
        return False


def mount_note(path, mounts=None):
    """LOCAL_LOCKS when the mount that holds `path` is listed with localflock or local_lock=, as /proc/mounts shows it (the
    longest mount point that holds the path); "" when it is not, or when the file cannot be read. `mounts` is the text, for a
    test. Fixed words only; nothing from the file is printed."""
    try:
        if mounts is None:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
                mounts = handle.read(1024 * 1024)
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return ""
    best = None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        point = fields[1].replace("\\040", " ")
        if (real == point or real.startswith(point.rstrip("/") + "/")) and (best is None or len(point) > len(best[0])):
            best = (point, fields[3])
    return LOCAL_LOCKS if best is not None and LOCAL_FLAGS.search(best[1]) else ""

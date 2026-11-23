"""The live dashboard server of a machine: its state, its lock, where its link can work, the hook's ping and the link itself.

One server runs for every project of a user on one host. The first session starts it (`ensure`, called by the session-start hook)
and it stops 24 hours after the last session pinged it. It listens on 127.0.0.1 only, behind an address prefix and two keys: a view
key, which the session may be told and which opens no change, and a change key, which reaches the user only in the hook's
`systemMessage`, the state file, a launch file and their own terminal. This module imports neither `http.server` nor `mind`, so a
hook loads it cheaply; dashboard_action.py is the server itself.

Everything local goes through storage.py. The state is one file, `dashboard-server-<host16>.json`, with the lock `dashboard-server-
<host16>.lock` beside it, named by a digest of the host name so that a home folder shared by many nodes gives each its own server.
Nothing in it is text from GitHub or from a project."""

import contextlib
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time

import names
import recortex
import storage

HERE = os.path.dirname(os.path.abspath(__file__))
FORM = 1
LIFE_SECONDS = 24 * 3600     # the server stops this long after the last ping of a session
PING_WINDOW = 60             # a ping's time may differ from the server's by this much
PING_SECONDS = 0.3           # the hook's connect and answer together
PORT_BASE, PORT_TRIES = 47000, 20
HEX32 = re.compile(r"[0-9a-f]{32}")
ROOT_TEXT = re.compile(r"/[^\x00-\x1f\x7f]{0,1000}")
OFF_WORDS = ("0", "off", "no", "false")
REASONS = ("lock", "state", "port", "start", "disabled")
STATE_KEYS = {"form", "pid", "port", "mount", "view", "change", "version", "started"}
SSH_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def host16():
    return hashlib.sha256(socket.gethostname().encode("utf-8", "replace")).hexdigest()[:16]


def state_name():
    return "dashboard-server-%s.json" % host16()


def lock_name():
    return "dashboard-server-%s.lock" % host16()


def failed_name():
    return "dashboard-server-%s.failed" % host16()


def ping_name():
    return "dashboard-ping.json"


def private_folder(create=False):
    return storage.folder(storage.state_top(for_key=True), create=create, tighten=create)


def version():
    """The plugin's version, read from its manifest beside the scripts; "" when it cannot be read."""
    try:
        with open(os.path.join(HERE, "..", "..", "..", ".claude-plugin", "plugin.json"), encoding="utf-8") as handle:
            found = json.load(handle).get("version", "")
        return found if isinstance(found, str) and re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", found) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def project_id(root):
    """The id of a project in an address: the first 16 hex digits of the SHA-256 of its folder's real path."""
    return hashlib.sha256(os.path.realpath(root).encode("utf-8", "replace")).hexdigest()[:16]


def reach(environ=None):
    """Where the user's browser can reach this machine's loopback port: "local" (no SSH variable is set), "editor" (one is, and
    the session runs in VS Code's terminal, which forwards a port it sees) or "none" (any other remote machine, a batch job, a
    container reached over SSH). Only the variables' names are looked at, never a value but TERM_PROGRAM's."""
    environ = os.environ if environ is None else environ
    if not any(environ.get(name) for name in SSH_VARIABLES):
        return "local"
    if environ.get("TERM_PROGRAM") == "vscode" or environ.get("VSCODE_IPC_HOOK_CLI"):
        return "editor"
    return "none"


def enabled(environ=None):
    """False when the user switched the live dashboard off: `"live_dashboard": false` in the plugin's settings.json, or
    RECORTEX_DASHBOARD set to 0, off, no or false."""
    value = names.env("DASHBOARD") if environ is None else environ.get(names.ENV_PREFIX + "DASHBOARD")
    if isinstance(value, str) and value.strip().lower() in OFF_WORDS:
        return False
    try:
        with open(recortex.settings_file(), encoding="utf-8") as handle:
            return json.load(handle).get("live_dashboard") is not False
    except (OSError, ValueError, AttributeError, RecursionError):
        return True


# ---------------------------------------------------------------- the state and the lock

def fits(state):
    return (isinstance(state, dict) and set(state) == STATE_KEYS and state["form"] == FORM
            and all(type(state[key]) is int and 0 < state[key] < 2 ** 31 for key in ("pid", "port"))
            and type(state["started"]) is int and 0 < state["started"] < 4102444800
            and all(isinstance(state[key], str) and HEX32.fullmatch(state[key]) for key in ("mount", "view", "change"))
            and isinstance(state["version"], str) and len(state["version"]) <= 20)


def read_state():
    """The state of this host's server, or None when there is none or it does not fit its form. Never echoed."""
    try:
        folder = private_folder()
        text = storage.read_file(os.path.join(folder, state_name())) if folder else None
        state = json.loads(text) if text else None
    except (OSError, ValueError, storage.StorageError, RecursionError):
        return None
    return state if fits(state) else None


def write_state(state):
    storage.write_file(os.path.join(private_folder(create=True), state_name()), json.dumps(state, sort_keys=True) + "\n")


def remove_state():
    try:
        folder = private_folder()
        if folder:
            storage.remove_file(folder, state_name())
    except (OSError, storage.StorageError):
        pass


def note_failure(reason):
    """Keep why the server could not start, one word of REASONS, for --doctor and the next session start. Never raises."""
    try:
        if reason in REASONS:
            storage.write_file(os.path.join(private_folder(create=True), failed_name()), reason + "\n")
    except (OSError, storage.StorageError):
        pass


def read_failure():
    """The word kept by note_failure, or "" when there is none (or it is not one of REASONS)."""
    try:
        folder = private_folder()
        text = storage.read_file(os.path.join(folder, failed_name())) if folder else None
    except (OSError, storage.StorageError):
        return ""
    return text.strip() if isinstance(text, str) and text.strip() in REASONS else ""


def clear_failure():
    try:
        folder = private_folder()
        if folder:
            storage.remove_file(folder, failed_name())
    except (OSError, storage.StorageError):
        pass


def alive(state):
    """True when the process of `state` exists (a look only: nothing is sent, nothing is changed)."""
    try:
        os.kill(state["pid"], 0)
        return True
    except PermissionError:
        return True
    except (OSError, KeyError, TypeError, OverflowError):
        return False


def ping_due(root, now=None, every=1800):
    """For the prompt hook: True when this project was not pinged in the last `every` seconds; the time is then kept (at most 30
    projects, the newest). The keep is the user's private state file, written through storage.py."""
    now = int(time.time()) if now is None else now
    ident = project_id(root)
    try:
        folder = private_folder(create=True)
        path = os.path.join(folder, ping_name())
        try:
            text = storage.read_file(path)
            kept = json.loads(text) if text else {}
        except (ValueError, RecursionError):
            kept = {}
        kept = {key: at for key, at in kept.items() if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{16}", key)
                and type(at) is int and 0 < at <= now + 60} if isinstance(kept, dict) else {}
        if ident in kept and now - kept[ident] < every:
            return False
        kept[ident] = now
        newest = sorted(kept.items(), key=lambda item: item[1])[-30:]
        storage.write_file(path, json.dumps(dict(newest), sort_keys=True) + "\n")
        return True
    except (OSError, storage.StorageError):
        return False


def take_lock():
    """The descriptor of this host's lock, held with flock for as long as it is kept open (a crash or a reboot frees it), or None
    when another server holds it. Raises storage.StorageError for storage that is refused."""
    folder = private_folder(create=True)
    path = os.path.join(folder, lock_name())
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        storage.check_path(path, "file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, storage.StorageError) as error:
        os.close(descriptor)
        if isinstance(error, BlockingIOError):
            return None
        raise
    return descriptor


def ports(uid=None):
    """The ports to try: 47000 + uid % 1000 and the 20 after it, then any free port (0)."""
    first = PORT_BASE + (os.getuid() if uid is None else uid) % 1000
    return list(range(first, first + PORT_TRIES)) + [0]


# ---------------------------------------------------------------- the ping

def mac(key, *parts):
    """HMAC-SHA256 of the canonical form of `parts` under `key` (the user key, bytes)."""
    return hmac.new(key, canonical(list(parts)).encode("utf-8"), hashlib.sha256).hexdigest()


def user_key():
    return storage.user_key(create=True)


def hook_mac(root, at, nonce):
    return mac(user_key(), "dashboard-hook", root, at, nonce)


def pong_mac(nonce):
    return mac(user_key(), "dashboard-pong", nonce)


def ping(state, root, seconds=PING_SECONDS, stop=False, probe=False):
    """Ask the server of `state` to register the session's project (or to stop), and check that it answered with the pong only
    this user's server can make. ("running" or "other version" or "stale", the answer's version). Never raises; the connect and
    the answer together take at most `seconds`."""
    try:
        started = time.monotonic()
        at, nonce = int(time.time()), secrets.token_hex(16)
        body = {"root": os.path.realpath(root), "at": at, "nonce": nonce, "mac": hook_mac(os.path.realpath(root), at, nonce)}
        if stop:
            body["stop"] = True
        if probe:
            body["probe"] = True   # the server answers and registers nothing (what --doctor sends)
        connection = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=seconds)
        try:
            connection.request("POST", "/%s/api/hook" % state["mount"], body=json.dumps(body),
                               headers={"Host": "127.0.0.1:%d" % state["port"], "Content-Type": "application/json"})
            connection.sock.settimeout(max(0.05, seconds - (time.monotonic() - started)))
            answer = connection.getresponse()
            data = json.loads(answer.read(4096) or b"{}")
        finally:
            connection.close()
        got = data.get("pong") if isinstance(data, dict) else None
        if not (isinstance(got, str) and hmac.compare_digest(got, pong_mac(nonce))):
            return "stale", ""
        found = data.get("version")
        found = found if isinstance(found, str) and len(found) <= 20 else ""
        return ("running" if found == version() else "other version"), found
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException, storage.StorageError):
        return "stale", ""


def start():
    """Start `dashboard_action.py serve` detached, as the sync is started: its own session, no standard streams, the user's home
    as its folder, and neither the project's folder nor its configuration in its environment. Never waits for it."""
    environment = {key: value for key, value in os.environ.items() if key not in ("CLAUDE_PROJECT_DIR", names.ENV_PREFIX + "CONFIG")}
    subprocess.Popen([sys.executable, os.path.join(HERE, "dashboard_action.py"), "serve"], cwd=os.path.expanduser("~"),
                     env=environment, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)


def ensure(root, seconds=PING_SECONDS):
    """For the session-start hook: "off" (the user switched it off), "running" (a server of ours answered and knows the project now), "started" (none answered, one is starting) or "failed" (it
    could not be started). A server of another version is stopped and replaced. Never raises, never waits for the child."""
    try:
        if not enabled():
            return "off"   # reach() decides only whether the question of Part B follows the link, never whether the server starts
        state = read_state()
        if state is not None:
            answer, found = ping(state, root, seconds)
            if answer == "running":
                return "running"
            if answer == "other version":
                ping(state, root, seconds, stop=True)
        start()
        return "started"
    except (OSError, ValueError, storage.StorageError, subprocess.SubprocessError):
        return "failed"


def after_start(root, seconds=1.0):
    """After ensure said "started": wait up to `seconds` for the new server's state, then ping it so that it knows the project.
    The state, or None when it did not appear (a slow start is not a failure). The only wait of the hook, and a bounded one."""
    deadline = time.monotonic() + seconds
    while True:
        state = read_state()
        if state is not None:
            return state if ping(state, root)[0] == "running" else None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def link(state, root, key):
    """The link that opens the project at `root` with `key` (the view key or the change key) in its fragment, where the page's
    script reads it and removes it from the address."""
    return "http://127.0.0.1:%d/%s/p/%s/#%s" % (state["port"], state["mount"], project_id(root), key)


@contextlib.contextmanager
def project(root):
    """For one request: CLAUDE_PROJECT_DIR is the project's folder, and it is put back after. Safe because the server handles one
    request at a time; `recortex.project_root()` and `load_config()` read it afresh on every call."""
    before = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = root
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = before

"""The helper of the local-record tests (1.9.0): a sandbox in which a local record exists as sealed files.

LocalSandbox makes a temporary folder under .tmp/test-local-*, points HOME and the XDG folders into it (for every subprocess and,
through mock.patch.dict, for the test's own process), makes a record in the place of its `place` attribute, writes the project
file and the trusted list, binds the record to its folder, and seeds the invented record of tests/fixtures/local-record.json
through the store's own writes, so that every file is sealed with the key of the sandbox. `damage(name)` hurts a file the way
the fixture's damage list says. `gh` is a script that writes a line to a log and fails: a test of a local record ends by asserting
that the log is empty. No test imports a script at module level before the environment of its sandbox is in place: the scripts
read their folders from the environment when a function runs, not when the module loads."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "recortex", "scripts")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "local-record.json")
sys.path.insert(0, SCRIPTS)
sys.dont_write_bytecode = True

import names  # noqa: E402

RECORD_ID = "k3f5x2m7q4w6z5ab"
OTHER_ID = "q7c2m5t3v4x6b2da"
REPO = "local--record/" + RECORD_ID
START = 1795000000        # the clock of a seeded record: a minute between two writes
HARNESS = {"RECORTEX_SETUP_QUESTIONS": "0", "RECORTEX_LINK_PAUSE": "0", "PYTHONDONTWRITEBYTECODE": "1"}


def fixture():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


class LocalSandbox(unittest.TestCase):
    """The record, its folder and its project, made fresh for each test. `place` is "own", "project" or "chosen"."""

    place = "own"
    trusted = True
    seeded = True

    def setUp(self):
        base = os.path.join(ROOT, ".tmp")
        os.makedirs(base, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="test-local-", dir=base)
        self.outside = tempfile.mkdtemp(prefix="recortex-local-")   # the folders above a record in the system's temporary folder
        os.chmod(self.outside, 0o755)
        self.addCleanup(self.forget)
        self.addCleanup(self.no_gh_was_started)   # registered first, so that it runs last
        self.gh_log = os.path.join(self.dir, "gh-calls.log")
        self.no_gh = os.path.join(self.dir, "no-gh.sh")
        with open(self.no_gh, "w") as handle:
            handle.write("#!/bin/sh\necho \"$@\" >> %s\nexit 1\n" % self.gh_log)
        os.chmod(self.no_gh, 0o755)
        self.trusted_file = os.path.join(self.dir, "trusted-repos")
        self.project = os.path.join(self.outside if self.place == "project" else self.dir, "project")
        os.makedirs(self.project)
        os.chmod(self.project, 0o755)
        environment = dict(HARNESS, HOME=self.dir, XDG_STATE_HOME=os.path.join(self.dir, "state"),
                           XDG_CACHE_HOME=os.path.join(self.dir, "cache"), XDG_CONFIG_HOME=os.path.join(self.dir, "config"),
                           XDG_DATA_HOME=os.path.join(self.dir, "data"), CLAUDE_PROJECT_DIR=self.project,
                           RECORTEX_GH=self.no_gh, RECORTEX_TRUSTED_FILE=self.trusted_file,
                           CLAUDE_CONFIG_DIR=os.path.join(self.dir, "claude"))
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        for variable in sorted(names.ENV_PREFIX + name for name in names.VARIABLES):
            if variable not in environment:
                os.environ.pop(variable, None)
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        import local_store
        import record_store
        import storage
        self.local_store, self.record_store, self.storage = local_store, record_store, storage
        self.moment = START
        patcher = mock.patch.object(local_store, "now", side_effect=self.tick)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record_id, self.repo = RECORD_ID, REPO
        self.config = {"repo": REPO, "hub": 1, "store": "local", "place": "own" if self.place != "project" else "project",
                       "profiles": ["software", "research"]}
        self.folder = self.make_folder()
        self.write_config(self.config)
        if self.trusted:
            self.write_trusted()
        if self.seeded:
            self.seed()

    # ------------------------------------------------------------ the world
    def tick(self):
        self.moment += 60
        return self.moment

    def forget(self):
        if not os.environ.get("RECORTEX_TEST_KEEP"):
            shutil.rmtree(self.dir, True)
            shutil.rmtree(self.outside, True)

    def no_gh_was_started(self):
        log = ""
        if os.path.exists(self.gh_log):
            with open(self.gh_log) as handle:
                log = handle.read()
        self.assertEqual("", log, "a gh call was started for a local record")

    def make_folder(self):
        """The record folder of this sandbox's place, with record.json, bound to this machine."""
        storage, store = self.storage, self.local_store
        if self.place == "own":
            path = storage.own_record_folder(self.record_id, create=True)
            real = storage.check_record_folder(path, "own")
            place = "own"
        elif self.place == "project":
            path = storage.record_path(self.project)
            real = storage.check_record_folder(path, "project", create=True)
            place = "project"
        else:
            path = os.path.join(self.outside, "chosen-record")
            real = storage.check_record_folder(path, "chosen", create=True)
            place = "chosen"
        store.LocalStore.create(real, self.record_id, "sample project", self.config["profiles"])
        storage.bind_record(self.record_id, real, place)
        return real

    def write_config(self, config):
        with open(os.path.join(self.project, ".recortex.json"), "w") as handle:
            json.dump(config, handle)

    def write_trusted(self, extra=""):
        with open(self.trusted_file, "w") as handle:
            handle.write("%s%s\n" % (self.repo, extra))

    @property
    def store(self):
        return self.record_store.for_repo(self.repo, self.config)

    # ------------------------------------------------------------ the record
    def seed(self):
        """Write the fixture through the store: the hub, three topics, the entries, a later text and a closed task."""
        data = fixture()
        store = self.store
        self.numbers, self.entries = {}, {}
        for item in data["topics"]:
            address = store.create_issue(item["title"], item["labels"], body=item["body"])
            self.numbers[item["key"]] = store.created_number(address)
        for item in data["entries"]:
            address = store.post_entry(self.numbers[item["topic"]], body=item["body"])
            self.entries.setdefault(item["topic"], []).append(int(store.posted_id(address)))
        for item in data["topics"]:
            if item.get("later"):
                store.edit_body(self.numbers[item["key"]], body=item["later"])
            if item.get("close"):
                store.close(self.numbers[item["key"]], item["close"])

    def files(self, sub=""):
        """The names under the record folder (a folder's own files, or a topic's)."""
        return sorted(os.listdir(os.path.join(self.folder, sub) if sub else self.folder))

    def entry_path(self, topic, index=0):
        """The path of the index-th entry file of a topic (by name), not a body version."""
        folder = os.path.join(self.folder, "hub" if topic == "hub" else os.path.join("topics", self.slug(topic)))
        names_ = [n for n in sorted(os.listdir(folder)) if self.local_store.ENTRY_NAME.fullmatch(n)]
        found = []
        for name in names_:
            with open(os.path.join(folder, name), "rb") as handle:
                if b"\nkind: entry\n" in handle.read():
                    found.append(os.path.join(folder, name))
        return found[index]

    def slug(self, key):
        return {"reader": "reader-quoting-and-line-endings", "speed": "read-speed-against-the-standard-library",
                "task": "task-newlines-inside-quoted-fields"}[key]

    def topic_path(self, key):
        return os.path.join(self.folder, "hub.md" if key == "hub" else os.path.join("topics", self.slug(key) + ".md"))

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def write(self, path, data, mode=0o600):
        if os.path.lexists(path):
            os.chmod(path, 0o600)
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)

    def damage(self, name, key="reader"):
        """Hurt a file as the fixture's damage list says. Returns the path of the file concerned."""
        if name == "changed-entry":
            path = self.entry_path(key, 0)
            data = self.read(path)
            self.write(path, data.replace(b"**Decision:**", b"**Decision (edited):**", 1))
            return path
        if name == "foreign-entry":
            path = self.entry_path(key, 0)
            relpath = os.path.relpath(path, self.folder).replace(os.sep, "/")
            parsed = self.local_store.parse_file(self.read(path), relpath, self.record_id)
            other = self.local_store.Sealing(b"another machine's key")
            self.write(path, self.local_store.render(parsed.kind, parsed.front, parsed.body, relpath, self.record_id, other))
            return path
        if name == "changed-body":
            path = self.topic_path(key)
            self.write(path, self.read(path).replace(b"strict quoting", b"lenient quoting", 1))
            return path
        if name == "conflict-copy":
            path = self.topic_path(key)
            copy = path[:-3] + " (conflicted copy).md"
            self.write(copy, self.read(path))
            return copy
        if name == "half-written":
            path = os.path.join(self.folder, "topics", "half-written.md")
            self.write(path, b"---\nrecortex: 1\nkind: topic\nnumber: 9\ntitle: \"Half")
            return path
        if name == "symlink":
            path = self.entry_path(key, 0)
            other = self.entry_path(key, 1)
            os.remove(path)
            os.symlink(other, path)
            return path
        if name == "duplicate-number":
            path = self.topic_path(key)
            copy = os.path.join(os.path.dirname(path), "reader-again.md")
            relpath = "topics/reader-again.md"
            parsed = self.local_store.parse_file(self.read(path), os.path.relpath(path, self.folder).replace(os.sep, "/"),
                                                 self.record_id)
            self.write(copy, self.local_store.render("topic", parsed.front, parsed.body, relpath, self.record_id))
            return copy
        if name == "crlf":
            path = self.entry_path(key, 0)
            self.write(path, self.read(path).replace(b"\n", b"\r\n"))
            return path
        raise ValueError("no damage named %s" % name)

    # ------------------------------------------------------------ scripts
    def run_script(self, script, *args, stdin=None, env=None):
        environment = dict(os.environ)
        environment.update(env or {})
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, script)] + list(args), capture_output=True, text=True,
                              env=environment, cwd=self.project, input=stdin, timeout=240)

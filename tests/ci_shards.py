#!/usr/bin/env python3
"""The shards that CI runs, and the guard that keeps them whole.

.github/workflows/tests.yml runs one job for each Python version and each shard below. A shard is a name and a list of
commands, and each command is a line of the test block in CONTRIBUTING.md: a list of classes of tests/test_scripts.py
(`python3 -m unittest tests.test_scripts.A tests.test_scripts.B`) or the files that a pattern picks
(`python3 -m unittest discover -s tests -p 'test_x.py'`). The workflow names the shards and runs each through
`python tests/ci_shards.py run NAME`; the commands exist only here.

    python tests/ci_shards.py plan [NAME]   the shards and their commands, as JSON
    python tests/ci_shards.py check         what each shard holds; exit 1 if a test is in no shard or in two
    python tests/ci_shards.py run NAME      run the shard's commands one after the other, all of them even after a
                                            failure, and exit 1 if any failed

`run` gives its commands a fresh home: HOME and the four XDG folders (cache, state, config, data) point into a new folder
under .tmp/, so that no test writes under the real home (`isolated_home`). A test that needs another place sets its own.

`check` loads the tests without running them: it asks the loader for everything that `discover -s tests` finds, asks it
again for each command, and compares the two. A new test file or class that no shard holds fails the check, and so
does one that two shards hold. tests/test_jobs_ci.py runs it, so a new test cannot be skipped by CI without a red test.

To add a test file or a class of test_scripts.py: put it into a command of a shard here and into the matching line of
CONTRIBUTING.md. Balance by the measured times of the commands (the notes below, in seconds, from a run of each
command alone on an idle machine); keep a shard under about ten minutes there, since a runner is two to three times
slower and a job stops at 45 minutes.

Standard library only; it runs on Python 3.8.
"""

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = re.compile(r"[a-z][a-z0-9-]*")


def classes(*names):
    """A command that runs classes of tests/test_scripts.py."""
    return ["tests.test_scripts." + name for name in names]


def files(pattern):
    """A command that runs every test file that the pattern picks."""
    return ["discover", "-s", "tests", "-p", pattern]


# Each shard is (name, commands). The lines of CONTRIBUTING.md, in its order, are the commands; about 2,850 seconds
# in all, so seven shards of 340 to 470 seconds each. Notes: the command's measured seconds, idle.
SHARDS = [
    ("scripts-1", [
        classes("LintTests", "HelperTests", "WorkingFolderTests", "LabelTests", "CreateIssuesTests", "PostVerifiedTests",
                "ConvertBodyTests", "SyncTests", "RecoveryTests", "CompileTests", "TriageAndFormatTests",
                "CatchUpTests", "SnapshotTests", "PackagingTests", "CompatTests", "MemoryFormatTests",
                "DocsRecordTests", "DocsTests", "SetupTests"),                                        # 240
        classes("ResumeBlockTests", "IndexTests", "HookTests", "StatusLineTests", "StorageTests", "GuardTests",
                "BackgroundTests", "DiscoveryTests", "CurrentNameTests"),                             # 230
    ]),
    ("scripts-2", [
        classes("EditHistoryTests", "MindIndexTests", "MindModelTests", "MindPageTests", "MindTextTests",
                "MindFigureTests", "FieldFormTests", "FieldTests"),                                   # 220
        classes("GraphqlReaderTests", "FakeProjectsTests", "SubIssueTests", "BoardRulesTests", "BoardCompareTests",
                "BoardWriteTests", "BoardSyncTests", "BoardTrustTests"),                              # 185
        files("test_observer_*.py"),                                                                  # 40
        files("test_continuity.py"),                                                                  # 7
    ]),
    ("write", [
        files("test_manuscript_write.py"),                                                            # 265
        files("test_manuscript_approval.py"),                                                         # 120
        files("test_status_summary.py"),                                                              # 35
    ]),
    ("versions", [
        files("test_manuscript_versions.py"),                                                         # 240
        files("test_manuscript_plan.py"),                                                             # 165
    ]),
    ("page", [
        files("test_dashboard_*.py"),
        files("test_decision_*.py"),
        files("test_project_map*.py"),
        files("test_manuscript_history.py"),                                                          # 70
        files("test_manuscript_paper.py"),                                                            # 50
        files("test_manuscript_page.py"),                                                             # 150
        files("test_setup_questions.py"),                                                             # 90
    ]),
    ("jobs-1", [
        files("test_jobs_[a-j]*.py"),                                                                 # 195
        files("test_jobs_[l]*.py"),                                                                   # 205
    ]),
    ("jobs-2", [
        files("test_jobs_[km-r]*.py"),                                                                # 145
        files("test_jobs_[s-z]*.py"),                                                                 # 194
    ]),
]


def isolated_home():
    """A fresh folder under .tmp/ and the variables that make it the home of a test run: HOME and the four XDG folders of the
    plugin's storage. Returns (the folder, {name: path}). The folder is made with mode 0700."""
    os.makedirs(os.path.join(ROOT, ".tmp"), exist_ok=True)
    folder = tempfile.mkdtemp(prefix="home-", dir=os.path.join(ROOT, ".tmp"))
    return folder, {"HOME": folder,
                    "XDG_CACHE_HOME": os.path.join(folder, ".cache"),
                    "XDG_STATE_HOME": os.path.join(folder, ".local", "state"),
                    "XDG_CONFIG_HOME": os.path.join(folder, ".config"),
                    "XDG_DATA_HOME": os.path.join(folder, ".local", "share")}


def run_isolated(name, out=sys.stdout):
    """`run`, with the variables of isolated_home in this process's environment, which the commands inherit. The folder is
    removed afterwards unless RECORTEX_TEST_KEEP=1; the environment is put back."""
    folder, variables = isolated_home()
    saved = {key: os.environ.get(key) for key in variables}
    os.environ.update(variables)
    try:
        return run(name, out=out)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if os.environ.get("RECORTEX_TEST_KEEP") != "1":
            shutil.rmtree(folder, ignore_errors=True)


def command_line(args):
    """The command that runs one command of a shard: `python -m unittest ARGS -v`."""
    return [sys.executable, "-m", "unittest"] + list(args) + ["-v"]


def load(args):
    """The tests that `python -m unittest ARGS` would run, loaded and not run (a TestSuite)."""
    loader = unittest.TestLoader()
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)   # `python -m unittest` has the current folder, the checkout's top, on its path
    if args[0] != "discover":
        return loader.loadTestsFromNames(list(args))
    start, pattern, top = ".", "test*.py", None
    rest = list(args[1:])
    while rest:
        flag = rest.pop(0)
        if flag in ("-s", "--start-directory"):
            start = rest.pop(0)
        elif flag in ("-p", "--pattern"):
            pattern = rest.pop(0)
        elif flag in ("-t", "--top-level-directory"):
            top = rest.pop(0)
        else:
            raise ValueError("a discover argument this guard does not know: %s" % flag)
    return loader.discover(os.path.join(ROOT, start), pattern, os.path.join(ROOT, top) if top else None)


def walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for inner in walk(item):
                yield inner
        else:
            yield item


def tally(args):
    """(a Counter of test ids, the tests that did not load) for one command. The id is `module.Class.method` with
    no leading `tests.`, so that a class named through its package and found by discovery has the same id."""
    ids, broken = collections.Counter(), []
    for test in walk(load(args)):
        name = test.id()
        if type(test).__name__ == "_FailedTest":
            broken.append(name)
        else:
            ids[name[len("tests."):] if name.startswith("tests.") else name] += 1
    return ids, broken


def owner(test_id):
    """The class a test id belongs to."""
    return test_id.rsplit(".", 1)[0]


def per_shard(shards):
    """({shard name: Counter of ids}, the tests that did not load) for the commands of every shard."""
    out, broken = {}, []
    for name, commands in shards:
        out[name] = collections.Counter()
        for args in commands:
            ids, bad = tally(args)
            out[name].update(ids)
            broken += ["%s: %s" % (name, line) for line in bad]
    return out, broken


def problems(shards, found, held, broken=()):
    """What is wrong with the shards: lines of text, or [] when every test that discovery finds runs in exactly one
    shard. `found` is the Counter of what `discover -s tests` loads, `held` the Counter of each shard's tests."""
    out = ["a test that did not load: %s" % line for line in broken]
    names = collections.Counter(name for name, _ in shards)
    for name, commands in shards:
        if not NAME.fullmatch(name):
            out.append("a shard name that is not lowercase letters, digits and hyphens: %r" % name)
        if not commands or not all(commands):
            out.append("shard %s has no command, or an empty one" % name)
    out += ["a shard named %s twice" % name for name, count in sorted(names.items()) if count > 1]
    total = collections.Counter()
    for counter in held.values():
        total.update(counter)
    missing = collections.Counter(owner(i) for i in (found - total).elements())
    for name, count in sorted(missing.items()):
        out.append("in no shard: %s (%d tests)" % (name, count))
    more = collections.Counter(owner(i) for i in (total - found).elements())
    for name, count in sorted(more.items()):
        where = sorted(shard for shard, counter in held.items() if any(owner(i) == name for i in counter))
        out.append("in two shards, twice in one, or outside discovery: %s (%d tests too many; in %s)"
                   % (name, count, ", ".join(where)))
    return out


def check(shards=None, out=sys.stdout):
    """Compare the shards with discovery. Prints a table and returns the problems (a list of lines)."""
    shards = SHARDS if shards is None else shards
    found, broken = tally(files("test*.py"))
    held, more = per_shard(shards)
    lines = problems(shards, found, held, broken + more)
    width = max(len(name) for name, _ in shards)
    for name, _ in shards:
        out.write("%-*s %5d tests\n" % (width, name, sum(held[name].values())))
    out.write("%-*s %5d tests; discovery finds %d\n" % (width, "all", sum(sum(c.values()) for c in held.values()),
                                                      sum(found.values())))
    return lines


def run_commands(commands, call=subprocess.call, clock=time.time, out=sys.stdout):
    """Run each command in order, every one even after a failure. Returns a list of (args, exit code, seconds)."""
    results = []
    for number, args in enumerate(commands, 1):
        out.write("== %d of %d: python -m unittest %s\n" % (number, len(commands), " ".join(args)))
        out.flush()
        started = clock()
        code = call(command_line(args), cwd=ROOT)
        results.append((list(args), code, clock() - started))
    return results


def run(name, call=subprocess.call, clock=time.time, out=sys.stdout):
    """Run one shard; the exit code: 0 if every command passed, else 1."""
    commands = dict(SHARDS).get(name)
    if commands is None:
        out.write("no shard named %s; the shards are %s\n" % (name, ", ".join(n for n, _ in SHARDS)))
        return 2
    os.makedirs(os.path.join(ROOT, ".tmp"), exist_ok=True)   # a test needs this folder when its class runs first
    results = run_commands(commands, call, clock, out)
    out.write("== shard %s\n" % name)
    for args, code, seconds in results:
        out.write("%s %5.0f s  python -m unittest %s\n" % ("ok    " if code == 0 else "FAILED", seconds, " ".join(args)))
    return 0 if all(code == 0 for _, code, _ in results) else 1


def plan(name=None):
    """The shards as plain data (what `plan` prints)."""
    return [{"name": n, "commands": [list(c) for c in commands]} for n, commands in SHARDS if name in (None, n)]


def main(argv):
    if argv[:1] == ["plan"] and len(argv) in (1, 2):
        data = plan(argv[1] if len(argv) == 2 else None)
        if not data:
            sys.stderr.write("no shard named %s\n" % argv[1])
            return 2
        print(json.dumps({"shards": data}, indent=2))
        return 0
    if argv == ["check"]:
        lines = check()
        for line in lines:
            sys.stderr.write(line + "\n")
        return 1 if lines else 0
    if argv[:1] == ["run"] and len(argv) == 2:
        return run_isolated(argv[1])
    sys.stderr.write("usage: ci_shards.py plan [NAME] | check | run NAME\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

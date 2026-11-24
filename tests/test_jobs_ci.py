"""CI runs the tests in parallel shards (1.5.1): tests/ci_shards.py lists them, and these tests keep the list whole.

A new test file or class that no shard holds, or that two shards hold, makes the first test fail. The workflow is read
as text: it must name every shard, run each through ci_shards.py, and keep the pins and settings it has always had."""

import collections
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import ci_shards  # noqa: E402

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON = "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0"


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def contributing_commands():
    """The commands of CONTRIBUTING.md's test block, as lists of unittest arguments (the braces expanded)."""
    block = read("CONTRIBUTING.md").split("```bash", 1)[1].split("```", 1)[0]
    out = []
    for line in block.splitlines():
        if not line.startswith("python3 -m unittest "):
            continue
        args = shlex.split(line)[3:]
        braces = re.fullmatch(r"(.*)\{([^}]*)\}", args[0]) if len(args) == 1 else None
        out.append([braces.group(1) + name for name in braces.group(2).split(",")] if braces else args)
    return out


def identity(args):
    """A command as the same thing however its classes are ordered."""
    return tuple(sorted(args)) if args[0] != "discover" else tuple(args)


class ShardCoverageTests(unittest.TestCase):
    def test_every_test_runs_in_exactly_one_shard(self):
        """The guard itself, run as a process so that importing every test module changes nothing here."""
        done = subprocess.run([sys.executable, "-W", "ignore", os.path.join(ROOT, "tests", "ci_shards.py"), "check"], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        self.assertEqual("", done.stderr)
        self.assertEqual(0, done.returncode)
        total = re.search(r"^all +([0-9]+) tests; discovery finds ([0-9]+)$", done.stdout, re.M)
        self.assertIsNotNone(total, done.stdout)
        self.assertEqual(total.group(1), total.group(2))
        self.assertGreater(int(total.group(1)), 2000)
        self.assertEqual([name for name, _ in ci_shards.SHARDS],
                         [line.split()[0] for line in done.stdout.splitlines()[:-1]])

    def test_the_guard_names_a_test_that_no_shard_holds_or_that_two_hold(self):
        ids = collections.Counter
        found = ids({"test_a.A.test_1": 1, "test_a.A.test_2": 1, "test_b.B.test_1": 1})
        shards = [("one", [["x"]]), ("two", [["y"]])]
        whole = {"one": ids({"test_a.A.test_1": 1, "test_a.A.test_2": 1}), "two": ids({"test_b.B.test_1": 1})}
        self.assertEqual([], ci_shards.problems(shards, found, whole))
        self.assertEqual(["in no shard: test_b.B (1 tests)"],
                         ci_shards.problems(shards, found, {"one": whole["one"], "two": ids()}))
        both = {"one": whole["one"], "two": whole["two"] + ids({"test_a.A.test_1": 1, "test_a.A.test_2": 1})}
        self.assertEqual(["in two shards, twice in one, or outside discovery: test_a.A (2 tests too many; in one, two)"],
                         ci_shards.problems(shards, found, both))
        again = {"one": ids({"test_a.A.test_1": 2, "test_a.A.test_2": 1}), "two": whole["two"]}
        self.assertEqual(["in two shards, twice in one, or outside discovery: test_a.A (1 tests too many; in one)"],
                         ci_shards.problems(shards, found, again))
        outside = {"one": whole["one"], "two": whole["two"] + ids({"test_c.C.test_1": 1})}
        self.assertEqual(1, len(ci_shards.problems(shards, found, outside)))

    def test_the_guard_refuses_a_bad_shard_list(self):
        found = collections.Counter({"test_a.A.test_1": 1})
        held = {"one": collections.Counter({"test_a.A.test_1": 1})}
        self.assertEqual(["a shard named one twice"],
                         ci_shards.problems([("one", [["x"]]), ("one", [["y"]])], found, held))
        self.assertEqual(1, len(ci_shards.problems([("One", [["x"]])], found, held)))
        self.assertEqual(["shard one has no command, or an empty one"], ci_shards.problems([("one", [])], found, held))
        self.assertEqual(["shard one has no command, or an empty one"], ci_shards.problems([("one", [[]])], found, held))
        self.assertEqual(["a test that did not load: one: unittest.loader._FailedTest.test_x"],
                         ci_shards.problems([("one", [["x"]])], found, held, ["one: unittest.loader._FailedTest.test_x"]))

    def test_the_loader_counts_tests_without_running_them(self):
        """A small folder of tests: a pattern finds its files, a file that does not import is reported, and a command
        that names a class that is not there is reported too; none of it runs a test."""
        root = os.path.join(ROOT, ".tmp")
        os.makedirs(root, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="test-ci-", dir=root)
        self.addCleanup(shutil.rmtree, folder, True)
        self.addCleanup(lambda: [sys.modules.pop(name) for name in list(sys.modules) if name.startswith("test_ci_")])
        self.addCleanup(lambda: sys.path.count(folder) and sys.path.remove(folder))
        body = ("import unittest\n\nclass %s(unittest.TestCase):\n"
                "    def test_a(self):\n        raise SystemExit('a test that runs')\n"
                "    def test_b(self):\n        raise SystemExit('a test that runs')\n")
        for name, text in (("test_ci_one.py", body % "OneTests"), ("test_ci_two.py", body % "TwoTests"),
                           ("test_ci_bad.py", "def (:\n")):
            with open(os.path.join(folder, name), "w") as handle:
                handle.write(text)

        def files(pattern):
            return ["discover", "-s", folder, "-p", pattern]
        ids, broken = ci_shards.tally(files("test_ci_one.py"))
        self.assertEqual({"test_ci_one.OneTests.test_a": 1, "test_ci_one.OneTests.test_b": 1}, dict(ids))
        self.assertEqual([], broken)
        ids, broken = ci_shards.tally(files("test_ci_*.py"))
        self.assertEqual(4, sum(ids.values()))
        self.assertEqual(1, len(broken))
        self.assertIn("test_ci_bad", broken[0])
        with self.assertRaises(ValueError):
            ci_shards.tally(["discover", "-s", folder, "--unknown", "x"])
        # What the guard says about shards over that folder, through the same functions that `check` uses.
        shards = [("one", [files("test_ci_one.py")]), ("two", [files("test_ci_two.py")])]
        found, bad = ci_shards.tally(files("test_ci_[ot]*.py"))
        held, more = ci_shards.per_shard(shards)
        self.assertEqual([], ci_shards.problems(shards, found, held, bad + more))
        held, more = ci_shards.per_shard(shards[:1])
        self.assertEqual(["in no shard: test_ci_two.TwoTests (2 tests)"], ci_shards.problems(shards[:1], found, held))
        doubled = shards + [("three", [files("test_ci_one.py")])]
        held, more = ci_shards.per_shard(doubled)
        lines = ci_shards.problems(doubled, found, held)
        self.assertEqual(["in two shards, twice in one, or outside discovery: test_ci_one.OneTests "
                          "(2 tests too many; in one, three)"], lines)
        twice = [("one", [files("test_ci_one.py"), files("test_ci_o*.py")]), ("two", [files("test_ci_two.py")])]
        held, more = ci_shards.per_shard(twice)
        self.assertEqual(["in two shards, twice in one, or outside discovery: test_ci_one.OneTests "
                          "(2 tests too many; in one)"], ci_shards.problems(twice, found, held))
        # A class named through its package is the same test as the one that discovery finds.
        ids, _ = ci_shards.tally(files("test_ci_one.py"))
        self.assertTrue(all(not name.startswith("tests.") for name in ids))


class ShardRunTests(unittest.TestCase):
    def test_run_goes_on_after_a_failure_and_exits_with_one(self):
        calls = []

        def call(argv, cwd=None):
            calls.append((argv, cwd))
            return [0, 1, 0][len(calls) - 1]
        shards = [("t", [["tests.test_scripts.A"], ["discover", "-s", "tests", "-p", "test_x.py"], ["tests.test_scripts.B"]])]
        out = io.StringIO()
        with mock.patch.object(ci_shards, "SHARDS", shards):
            code = ci_shards.run("t", call=call, out=out)
        self.assertEqual(1, code)
        self.assertEqual(3, len(calls))   # the third ran although the second failed
        self.assertEqual([sys.executable, "-m", "unittest", "tests.test_scripts.A", "-v"], calls[0][0])
        self.assertEqual([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_x.py", "-v"], calls[1][0])
        self.assertEqual({ROOT}, {cwd for _, cwd in calls})
        text = out.getvalue()
        self.assertEqual(1, text.count("FAILED"))
        self.assertEqual(2, len(re.findall(r"^ok ", text, re.M)))

    def test_run_exits_with_zero_when_every_command_passes_and_two_for_an_unknown_shard(self):
        out = io.StringIO()
        shards = [("t", [["tests.test_scripts.A"], ["tests.test_scripts.B"]])]
        with mock.patch.object(ci_shards, "SHARDS", shards):
            self.assertEqual(0, ci_shards.run("t", call=lambda argv, cwd=None: 0, out=out))
            self.assertEqual(2, ci_shards.run("nothing", call=lambda argv, cwd=None: 0, out=out))
        self.assertIn("no shard named nothing; the shards are t", out.getvalue())

    def test_the_last_command_failing_fails_the_shard(self):
        shards = [("t", [["a"], ["b"]])]
        with mock.patch.object(ci_shards, "SHARDS", shards):
            self.assertEqual(1, ci_shards.run("t", call=lambda argv, cwd=None: 0 if "a" in argv else 3, out=io.StringIO()))

    def test_plan_prints_the_shards_as_json(self):
        done = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "ci_shards.py"), "plan"], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(0, done.returncode)
        plan = json.loads(done.stdout)["shards"]
        self.assertEqual([(name, commands) for name, commands in ci_shards.SHARDS],
                         [(shard["name"], shard["commands"]) for shard in plan])
        one = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "ci_shards.py"), "plan", "jobs-1"], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(["jobs-1"], [shard["name"] for shard in json.loads(one.stdout)["shards"]])
        nothing = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "ci_shards.py"), "plan", "nothing"],
                                 cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(2, nothing.returncode)


class ShardListTests(unittest.TestCase):
    def test_the_shards_are_the_lines_of_the_contributing_test_block(self):
        """Each line of the block is in exactly one shard and a shard holds nothing else: the local batches and CI's
        shards are the same commands."""
        held = collections.Counter(identity(args) for _, commands in ci_shards.SHARDS for args in commands)
        lines = collections.Counter(identity(args) for args in contributing_commands())
        self.assertIn(identity(ci_shards.files("test_dashboard_*.py")), lines)
        self.assertEqual(lines, held)
        self.assertEqual(set(), {key for key, count in held.items() if count != 1})

    def test_each_command_is_a_class_list_or_a_discover_pattern(self):
        for name, commands in ci_shards.SHARDS:
            for args in commands:
                if args[0] == "discover":
                    self.assertEqual(["discover", "-s", "tests", "-p"], args[:4], name)
                    self.assertEqual(5, len(args), name)
                    self.assertRegex(args[4], r"^test_[A-Za-z0-9_\[\]*.-]+\.py$", name)
                else:
                    for path in args:
                        self.assertRegex(path, r"^tests\.test_scripts\.[A-Za-z0-9]+$", name)

    def test_the_names_are_plain_and_each_shard_has_a_command(self):
        names = [name for name, _ in ci_shards.SHARDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 5)
        for name, commands in ci_shards.SHARDS:
            self.assertRegex(name, r"^[a-z][a-z0-9-]*$")
            self.assertGreaterEqual(len(commands), 1, name)

    def test_the_two_jobs_shards_hold_the_four_lines_of_the_sixteenth_command(self):
        shards = dict(ci_shards.SHARDS)
        self.assertEqual([ci_shards.files("test_jobs_%s*.py" % part) for part in ("[a-j]", "[l]")], shards["jobs-1"])
        self.assertEqual([ci_shards.files("test_jobs_%s*.py" % part) for part in ("[km-r]", "[s-z]")], shards["jobs-2"])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = read(".github", "workflows", "tests.yml")

    def test_the_workflow_names_every_shard_and_runs_each_through_the_script(self):
        listed = re.findall(r"^        shard: \[([^\]]*)\]$", self.text, re.M)
        self.assertEqual(1, len(listed))
        self.assertEqual([name for name, _ in ci_shards.SHARDS], [name.strip() for name in listed[0].split(",")])
        self.assertIn("          SHARD: ${{ matrix.shard }}\n", self.text)
        self.assertIn('          python tests/ci_shards.py run "$SHARD"\n', self.text)
        self.assertEqual(['python tests/ci_shards.py run "$SHARD"'],
                         re.findall(r"^\s+(python [^\n]*)$", self.text, re.M))   # nothing else runs Python by hand
        self.assertNotIn("discover -s tests", self.text)   # no job runs the whole suite in one command

    def test_the_matrix_is_two_pythons_each_with_its_system_times_the_shards(self):
        self.assertIn("        python: ['3.8', '3.14']\n", self.text)
        self.assertRegex(self.text, r"include:\n          - python: '3\.8'\n            os: ubuntu-22\.04\n"
                                    r"          - python: '3\.14'\n            os: ubuntu-24\.04\n")
        self.assertIn("    runs-on: ${{ matrix.os }}\n", self.text)
        name = re.search(r"^    name: (.*)$", self.text, re.M).group(1)
        for part in ("matrix.python", "matrix.os", "matrix.shard"):
            self.assertIn("${{ %s }}" % part, name)
        self.assertIn("      - name: Run shard ${{ matrix.shard }}\n", self.text)

    def test_the_limit_the_triggers_and_the_security_settings_are_kept(self):
        self.assertIn("    timeout-minutes: 45\n", self.text)
        self.assertEqual(1, self.text.count("timeout-minutes"))
        self.assertIn("      fail-fast: false\n", self.text)
        self.assertIn("on:\n  push:\n    branches: [main]\n  pull_request:\n  workflow_dispatch:\n\n", self.text)
        self.assertIn("permissions:\n  contents: read\n\n", self.text)
        self.assertEqual(1, self.text.count("permissions:"))
        self.assertIn("concurrency:\n  group: tests-${{ github.event.pull_request.number || github.ref }}\n"
                      "  cancel-in-progress: true\n", self.text)
        self.assertEqual(1, self.text.count("persist-credentials: false"))
        self.assertIn("          persist-credentials: false\n", self.text)
        self.assertIn("          GH_TOKEN: ''\n          GITHUB_TOKEN: ''\n", self.text)

    def test_the_actions_are_pinned_to_their_commits_and_nothing_else_is_used(self):
        used = re.findall(r"^\s*- uses: (\S+)", self.text, re.M)
        self.assertEqual([CHECKOUT, SETUP_PYTHON], used)
        self.assertEqual(2, self.text.count("uses:"))
        for action in used:
            self.assertRegex(action, r"^[a-z-]+/[a-z-]+@[0-9a-f]{40}$")

    def test_a_failing_gh_stands_ahead_of_the_real_cli_before_any_test_runs(self):
        text = self.text
        for needed in ('mkdir -p "$ci_bin" .tmp', "echo 'Tests must use tests/fake_gh.py, not the live GitHub CLI.' >&2",
                       "exit 1", 'chmod +x "$ci_bin/gh"', 'export PATH="$ci_bin:$PATH"'):
            self.assertIn(needed, text)
        self.assertLess(text.index('export PATH="$ci_bin:$PATH"'), text.index("python tests/ci_shards.py run"))
        self.assertLess(text.index('cat > "$ci_bin/gh"'), text.index('export PATH="$ci_bin:$PATH"'))


class ContributingCiTests(unittest.TestCase):
    def test_contributing_describes_the_shards_and_how_to_add_a_test(self):
        text = read("CONTRIBUTING.md")
        paragraph = text.split("[CI](.github/workflows/tests.yml)", 1)[1].split("\n\n", 1)[0]
        for needed in ("tests/ci_shards.py", "python tests/ci_shards.py run", "python tests/ci_shards.py check",
                       "tests/test_jobs_ci.py", "45 minutes", "eight shards", "the local batches"):
            self.assertIn(needed, paragraph, needed)
        for name, _ in ci_shards.SHARDS:
            self.assertIn("`%s`" % name, paragraph, name)
        self.assertIn("a new `test_*.py` file or a new class in `tests/test_scripts.py` goes into a command of a shard",
                      paragraph)
        self.assertEqual(8, len(ci_shards.SHARDS))   # "eight shards" above
        self.assertNotIn("which CI uses", text)


if __name__ == "__main__":
    unittest.main()

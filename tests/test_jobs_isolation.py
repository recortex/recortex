"""A test run writes nothing under the real home (1.9.0).

Every test command (CONTRIBUTING.md, tests/ci_shards.py) runs with HOME and the four XDG folders in a fresh folder, so that
the key, the grants and the index of a test never land in the user's own folders. The first class fails when a test is run
without that setting, which makes it the guard for the whole suite. Offline."""

import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import ci_shards  # noqa: E402
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import storage  # noqa: E402


class HomeIsolationTests(unittest.TestCase):
    """The storage folders of a test run are inside the fresh home that its command gave it. Run without that setting, this
    class fails; it is the guard for the whole suite. A test that needs another place sets its own."""

    def test_every_storage_folder_resolves_inside_the_home_of_the_run(self):
        saved = {key: os.environ.get(key) for key in ("RECORTEX_TESTING", "RECORTEX_DATA_HOME")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for key in saved:
            os.environ.pop(key, None)   # a test's own test_home() would hide where the real folders point
        home = os.path.realpath(os.environ.get("HOME", "/nonexistent"))
        allowed = [os.path.realpath(os.path.join(ROOT, ".tmp")), os.path.realpath(tempfile.gettempdir())]
        self.assertTrue(any(home.startswith(place + os.sep) for place in allowed),
                        "HOME is %s: run the tests with a fresh home (CONTRIBUTING.md, tests/ci_shards.py)" % home)
        for name, path in (("state_top", storage.state_top()), ("cache_top", storage.cache_top()),
                           ("config_top", storage.config_top()), ("key_path", storage.key_path()),
                           ("expanduser", os.path.expanduser("~/x"))):
            real = os.path.realpath(path)
            self.assertTrue(real.startswith(home + os.sep), "%s is %s, outside %s" % (name, real, home))

    def test_a_fresh_home_is_made_for_each_run_and_names_the_five_variables(self):
        folder, variables = ci_shards.isolated_home()
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual({"HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}, set(variables))
        self.assertEqual(0o700, os.stat(folder).st_mode & 0o777)
        for name, path in variables.items():
            self.assertTrue(os.path.isabs(path), name)
            self.assertTrue(path == folder or path.startswith(folder + os.sep), name)
        other, _ = ci_shards.isolated_home()
        self.addCleanup(shutil.rmtree, other, True)
        self.assertNotEqual(folder, other)
        self.assertTrue(os.path.realpath(folder).startswith(os.path.realpath(os.path.join(ROOT, ".tmp")) + os.sep))

    def test_run_gives_its_commands_the_home_and_puts_the_environment_back(self):
        seen = []

        def call(argv, cwd=None):
            seen.append({key: os.environ.get(key) for key in ("HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                                                              "XDG_DATA_HOME")})
            return 0
        before = dict(os.environ)
        shards = [("t", [["tests.test_scripts.A"], ["tests.test_scripts.B"]])]
        with mock.patch.object(ci_shards, "SHARDS", shards), \
                mock.patch.object(ci_shards, "run_commands", side_effect=lambda commands, *a, **k:
                                  [(args, call(args), 0.0) for args in commands]):
            code = ci_shards.run_isolated("t", out=io.StringIO())
        self.assertEqual(0, code)
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0], seen[1])   # one home for the shard's commands
        home = seen[0]["HOME"]
        self.assertTrue(home.startswith(os.path.join(ROOT, ".tmp") + os.sep))
        for key in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            self.assertTrue(seen[0][key].startswith(home + os.sep), key)
        self.assertEqual(before, dict(os.environ))
        self.assertFalse(os.path.exists(home))   # removed afterwards unless RECORTEX_TEST_KEEP=1

    def test_the_contributing_block_sets_the_same_variables_before_its_first_test_command(self):
        with open(os.path.join(ROOT, "CONTRIBUTING.md"), encoding="utf-8") as handle:
            block = handle.read().split("```bash", 1)[1].split("```", 1)[0]
        first = block.index("python3 -m unittest")
        head = block[:first]
        for variable in ("HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            self.assertRegex(head, r"(?m)^export (?:[A-Z_]+=\S+ )*%s=" % variable, variable)
        self.assertIn("mktemp -d", head)
        self.assertIn(".tmp/", head)
        self.assertEqual(1, len(re.findall(r"(?m)^export HOME=", head)))


if __name__ == "__main__":
    unittest.main()

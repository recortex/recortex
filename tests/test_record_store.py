"""The record's interface (1.9.0): what GithubStore emits for each operation, which store a name or a project file gets, and
that no script builds a `gh` argument list of its own. Offline: a recording stand-in takes the place of recortex.gh."""

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "recortex", "scripts")
sys.path.insert(0, SCRIPTS)
import index  # noqa: E402
import record_store  # noqa: E402
import recortex  # noqa: E402

REPO = "octo-org/octo-repo"
LOCAL = "local--record/k3f5x2m7q4w6z5ab"


class Recorder:
    """Stands in for recortex.gh: records each call and answers with the next canned text (or the same one)."""

    def __init__(self, answer=""):
        self.calls = []
        self.answers = answer if isinstance(answer, list) else [answer]

    def __call__(self, args, stdin=None, timeout=60, errors=None, binary=False):
        self.calls.append({"args": list(args), "stdin": stdin, "timeout": timeout, "errors": errors, "binary": binary})
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Box(unittest.TestCase):
    def setUp(self):
        self.store = record_store.GithubStore(REPO)

    def run_with(self, answer, call):
        recorder = Recorder(answer)
        with mock.patch.object(recortex, "gh", recorder):
            result = call(self.store)
        return result, recorder.calls


class ReadTests(Box):
    def test_whoami_names_the_host_only_when_asked(self):
        for host, args in ((False, ["api", "user", "--jq", ".login"]),
                           (True, ["api", "user", "--hostname", "github.com", "--jq", ".login"])):
            with self.subTest(host=host):
                result, calls = self.run_with(" octocat\n", lambda s: s.whoami(host=host))
                self.assertEqual("octocat", result)
                self.assertEqual([args], [c["args"] for c in calls])
                self.assertEqual(60, calls[0]["timeout"])

    def test_account_state_runs_the_two_calls_of_gh_account(self):
        result, calls = self.run_with(["", "octocat\n"], lambda s: s.account_state(7))
        self.assertEqual(("ready", "octocat"), result)
        self.assertEqual([["auth", "status", "--hostname", "github.com"],
                          ["api", "user", "--hostname", "github.com", "--jq", ".login"]], [c["args"] for c in calls])
        self.assertEqual([7, 7], [c["timeout"] for c in calls])
        self.assertEqual(["replace", "replace"], [c["errors"] for c in calls])
        for error, state in ((recortex.GhMissing("x"), "missing"), (recortex.GhTimeout("x"), "slow"),
                             (recortex.GhError("x"), "signed-out")):
            with self.subTest(first=state), mock.patch.object(recortex, "gh", side_effect=error):
                self.assertEqual((state, None), self.store.account_state(1))
        with mock.patch.object(recortex, "gh", side_effect=["", recortex.GhError("x")]):
            self.assertEqual(("unnamed", None), self.store.account_state(1))
        with mock.patch.object(recortex, "gh", side_effect=["", "not a login!\n"]):
            self.assertEqual(("unnamed", None), self.store.account_state(1))

    def test_read_issue_parses_the_object_and_with_jq_returns_the_text(self):
        result, calls = self.run_with(json.dumps({"number": 5, "body": "x"}), lambda s: s.read_issue(5))
        self.assertEqual({"number": 5, "body": "x"}, result)
        self.assertEqual([["api", "repos/octo-org/octo-repo/issues/5"]], [c["args"] for c in calls])
        result, calls = self.run_with("5\n", lambda s: s.read_issue(5, timeout=9, jq=".number"))
        self.assertEqual("5\n", result)
        self.assertEqual([["api", "repos/octo-org/octo-repo/issues/5", "--jq", ".number"]], [c["args"] for c in calls])
        self.assertEqual(9, calls[0]["timeout"])

    def test_read_entry_takes_a_number_or_its_text(self):
        for cid in (7, "7"):
            result, calls = self.run_with('{"id": 7}', lambda s: s.read_entry(cid))
            self.assertEqual({"id": 7}, result)
            self.assertEqual([["api", "repos/octo-org/octo-repo/issues/comments/7"]], [c["args"] for c in calls])

    def test_list_entries_pages_and_returns_the_raw_text(self):
        result, calls = self.run_with("[1][2]", lambda s: s.list_entries(3))
        self.assertEqual("[1][2]", result)
        self.assertEqual([["api", "--paginate", "repos/octo-org/octo-repo/issues/3/comments?per_page=100"]],
                         [c["args"] for c in calls])

    def test_hub_entries_is_one_graphql_call_and_the_list_read_hub_comments_returns(self):
        answer = json.dumps({"data": {"repository": {"issue": {"comments": {"nodes": [
            {"url": "u1", "createdAt": "2026-01-01T00:00:00Z", "body": "a", "author": {"login": "octocat"},
             "editor": None, "lastEditedAt": None, "userContentEdits": {"totalCount": 0, "nodes": []}},
            {"url": "u2", "createdAt": "2026-01-02T00:00:00Z", "body": "b", "author": {"login": "hubot"},
             "editor": {"login": "octocat"}, "lastEditedAt": "2026-01-03T00:00:00Z",
             "userContentEdits": {"totalCount": 1, "nodes": [{"editedAt": "x", "editor": {"login": "octocat"}}]}}]}}}}})
        result, calls = self.run_with(answer, lambda s: s.hub_entries(4, timeout=11))
        self.assertEqual([["api", "graphql", "-f", "query=" + recortex.RESUME_QUERY, "-f", "owner=octo-org", "-f",
                           "name=octo-repo", "-F", "number=4"]], [c["args"] for c in calls])
        self.assertEqual(11, calls[0]["timeout"])
        self.assertEqual(["u2", "u1"], [row["url"] for row in result])   # newest first
        self.assertEqual({"url": "u2", "created": "2026-01-02T00:00:00Z", "body": "b", "author": "hubot",
                          "last_edited": "2026-01-03T00:00:00Z", "editor": "octocat", "edit_total": 1,
                          "edit_editors": ["octocat"]}, result[0])
        with mock.patch.object(recortex, "gh", return_value='{"data": {"repository": {"issue": null}}}'):
            with self.assertRaises(recortex.GhError):
                self.store.hub_entries(4)

    def test_histories_is_one_graphql_call_for_the_nodes(self):
        nodes = {5: ("IC_abc", None), 6: ("IC_def", "cursor")}
        result, calls = self.run_with("{}", lambda s: s.histories(nodes, timeout=12))
        self.assertEqual("{}", result)
        self.assertEqual([["api", "graphql", "-f", "query=" + index.comment_edits_query(nodes)]], [c["args"] for c in calls])
        self.assertEqual(12, calls[0]["timeout"])

    def test_find_issues_has_four_shapes(self):
        shapes = {
            "mine": ["issue", "list", "--repo", REPO, "--author", "@me", "--state", "all", "--json",
                     "number,title,body,url", "--limit", "200"],
            "active": ["issue", "list", "--repo", REPO, "--label", "active", "--state", "open", "--json",
                       "number,labels", "--limit", "50"],
            "plan": ["issue", "list", "--repo", REPO, "--label", "plan", "--state", "open", "--json", "number",
                     "--limit", "100"],
            "dated": ["issue", "list", "--repo", REPO, "--state", "all", "--json", "number,title,createdAt,closedAt,url",
                      "--limit", "1000"],
        }
        for shape, args in shapes.items():
            with self.subTest(shape=shape):
                result, calls = self.run_with("[{\"number\": 1}]", lambda s: s.find_issues(shape, timeout=13))
                self.assertEqual([{"number": 1}], result)
                self.assertEqual([args], [c["args"] for c in calls])
                self.assertEqual(13, calls[0]["timeout"])
        result, _ = self.run_with("", lambda s: s.find_issues("dated"))
        self.assertEqual([], result)   # an empty answer is an empty list, as catchup read it
        with self.assertRaises(ValueError):
            self.store.find_issues("other")

    def test_list_labels_names_the_fields(self):
        result, calls = self.run_with('[{"name": "hub"}]', lambda s: s.list_labels())
        self.assertEqual([{"name": "hub"}], result)
        self.assertEqual([["label", "list", "--repo", REPO, "--json", "name", "--limit", "1000"]], [c["args"] for c in calls])
        _, calls = self.run_with("[]", lambda s: s.list_labels("name,color,description", timeout=30))
        self.assertEqual([["label", "list", "--repo", REPO, "--json", "name,color,description", "--limit", "1000"]],
                         [c["args"] for c in calls])
        self.assertEqual(30, calls[0]["timeout"])

    def test_is_pinned_and_visibility(self):
        result, calls = self.run_with('{"isPinned": true}', lambda s: s.is_pinned(5))
        self.assertIs(True, result)
        self.assertEqual([["issue", "view", "5", "--repo", REPO, "--json", "isPinned"]], [c["args"] for c in calls])
        self.assertIs(False, self.run_with('{"isPinned": false}', lambda s: s.is_pinned(5))[0])
        for text, wanted in (("true\n", "true"), ("false\n", "false"), ("", "")):
            result, calls = self.run_with(text, lambda s: s.visibility())
            self.assertEqual(wanted, result)
            self.assertEqual([["api", "repos/octo-org/octo-repo", "--jq", ".private"]], [c["args"] for c in calls])
            self.assertEqual(30, calls[0]["timeout"])
        with mock.patch.object(recortex, "gh", side_effect=recortex.GhError("no")):
            self.assertEqual("", self.store.visibility())

    def test_dump_reads_every_issue_and_every_comment(self):
        result, calls = self.run_with(["[1]", "[2]"], lambda s: s.dump(99))
        self.assertEqual(("[1]", "[2]"), result)
        self.assertEqual([["api", "--paginate", "repos/octo-org/octo-repo/issues?state=all&per_page=100"],
                          ["api", "--paginate", "repos/octo-org/octo-repo/issues/comments?per_page=100"]],
                         [c["args"] for c in calls])
        self.assertEqual([99, 99], [c["timeout"] for c in calls])
        self.assertEqual(list(self.store.dump_commands()), [c["args"] for c in calls])

    def test_versions_has_no_call_on_github(self):
        result, calls = self.run_with("", lambda s: s.versions(5))
        self.assertEqual(([], []), (result, calls))


class WriteTests(Box):
    def test_create_issue_puts_the_labels_after_the_body_in_both_spellings(self):
        for kwargs, file, stdin in (({"body": "text"}, "-", "text"), ({"body_file": "/x/body.md"}, "/x/body.md", None)):
            with self.subTest(file=file):
                result, calls = self.run_with("https://github.com/octo-org/octo-repo/issues/9\n",
                                              lambda s: s.create_issue("A title", ["hub", "active"], **kwargs))
                self.assertEqual("https://github.com/octo-org/octo-repo/issues/9\n", result)
                self.assertEqual([["issue", "create", "--repo", REPO, "--title", "A title", "--body-file", file,
                                   "--label", "hub", "--label", "active"]], [c["args"] for c in calls])
                self.assertEqual(stdin, calls[0]["stdin"])

    def test_edit_body_and_post_entry_have_both_spellings(self):
        for op, head in (("edit_body", ["issue", "edit", "5", "--repo", REPO]),
                         ("post_entry", ["issue", "comment", "5", "--repo", REPO])):
            for kwargs, file, stdin in (({"body": "t"}, "-", "t"), ({"body_file": "f.md"}, "f.md", None)):
                with self.subTest(op=op, file=file):
                    result, calls = self.run_with("out\n", lambda s: getattr(s, op)(5, **kwargs))
                    self.assertEqual([head + ["--body-file", file]], [c["args"] for c in calls])
                    self.assertEqual(stdin, calls[0]["stdin"])
                    self.assertEqual("out\n" if op == "post_entry" else None, result)

    def test_set_labels_adds_then_removes(self):
        _, calls = self.run_with("", lambda s: s.set_labels(5, ["a", "b"], ["c"]))
        self.assertEqual([["issue", "edit", "5", "--repo", REPO, "--add-label", "a", "--add-label", "b",
                           "--remove-label", "c"]], [c["args"] for c in calls])
        self.assertEqual(recortex.label_command(REPO, 5, ["a", "b"], ["c"]), calls[0]["args"])

    def test_labels_pins_and_state_changes(self):
        cases = [
            (lambda s: s.create_label("hub", "ffffff", "d"), ["label", "create", "hub", "--repo", REPO, "--color", "ffffff",
                                                               "--description", "d"]),
            (lambda s: s.edit_label("hub", "ffffff", "d"), ["label", "edit", "hub", "--repo", REPO, "--color", "ffffff",
                                                             "--description", "d"]),
            (lambda s: s.pin(4), ["issue", "pin", "4", "--repo", REPO]),
            (lambda s: s.close(4, "completed"), ["issue", "close", "4", "--repo", REPO, "--reason", "completed"]),
            (lambda s: s.close(4, "not planned"), ["issue", "close", "4", "--repo", REPO, "--reason", "not planned"]),
            (lambda s: s.reopen(4), ["issue", "reopen", "4", "--repo", REPO]),
        ]
        for call, args in cases:
            with self.subTest(args=args[:2]):
                _, calls = self.run_with("", call)
                self.assertEqual([args], [c["args"] for c in calls])
                self.assertIsNone(calls[0]["stdin"])

    def test_show_is_the_dry_run_text_of_the_same_list(self):
        self.assertEqual(recortex.show_command(["issue", "comment", "5", "--repo", REPO, "--body-file", "f.md"], "3 characters"),
                         self.store.show("post_entry", 5, body_file="f.md", note="3 characters"))
        self.assertEqual("gh issue edit 5 --repo octo-org/octo-repo --body-file -", self.store.show("edit_body", 5))
        self.assertEqual("gh issue close 4 --repo octo-org/octo-repo --reason 'not planned'",
                         self.store.show("close", 4, "not planned"))
        self.assertEqual(recortex.show_command(["label", "create", "x", "--repo", REPO, "--color", "ffffff",
                                                "--description", "d e"]), self.store.show("create_label", "x", "ffffff", "d e"))
        with self.assertRaises(ValueError):
            self.store.show("nothing", 1)


class ReadBackTests(Box):
    def test_the_id_of_a_post_is_the_number_after_issuecomment(self):
        url = "https://github.com/octo-org/octo-repo/issues/5#issuecomment-4242"
        self.assertEqual("4242", self.store.posted_id("noise\n" + url))
        self.assertIsNone(self.store.posted_id("https://github.com/octo-org/octo-repo/issues/5"))
        self.assertEqual("4242", self.store.posted_id(url, issue=5))
        self.assertIsNone(self.store.posted_id(url, issue=6))
        self.assertIsNone(self.store.posted_id(url.replace("octo-repo", "other-repo"), issue=5))

    def test_the_number_of_a_new_issue(self):
        self.assertEqual(9, self.store.created_number("https://github.com/octo-org/octo-repo/issues/9"))
        self.assertEqual(9, self.store.created_number("https://github.com/octo-org/octo-repo/pull/9"))
        with self.assertRaises(recortex.GhError):
            self.store.created_number("no number here")

    def test_addresses_and_the_safe_form(self):
        self.assertEqual("https://github.com/octo-org/octo-repo/issues/5", self.store.address(5))
        self.assertEqual("https://github.com/octo-org/octo-repo/issues/5#issuecomment-9", self.store.address(5, 9))
        self.assertEqual("https://api.github.com/repos/octo-org/octo-repo/issues/5", self.store.api_issue(5))
        good = "https://github.com/octo-org/octo-repo/issues/5#issuecomment-9"
        self.assertEqual(good, self.store.safe_address(good))
        for bad in (good + " x", "http://github.com/a/b", "https://evil.example/https://github.com/a", "", None, 5,
                    "https://github.com/a b"):
            self.assertIsNone(self.store.safe_address(bad), bad)

    def test_a_missing_issue_is_gh_saying_not_found(self):
        self.assertTrue(self.store.is_missing(recortex.GhError("gh api failed (exit 1): Not Found (HTTP 404)")))
        self.assertFalse(self.store.is_missing(recortex.GhError("gh api failed (exit 1): boom")))

    def test_the_raw_call_is_gh_itself(self):
        result, calls = self.run_with("ok", lambda s: s.github(["api", "x"], stdin="in", timeout=5, errors="replace"))
        self.assertEqual("ok", result)
        self.assertEqual([{"args": ["api", "x"], "stdin": "in", "timeout": 5, "errors": "replace", "binary": False}], calls)


class FeatureTests(Box):
    def test_a_github_store_has_every_feature_and_requires_nothing(self):
        for feature in record_store.FEATURES:
            self.assertTrue(self.store.supports(feature), feature)
            self.assertIsNone(self.store.require(feature))
        self.assertFalse(self.store.supports("something-else"))
        self.assertEqual("github", self.store.kind)
        self.assertEqual("GitHub", self.store.words["name"])


class WhichStoreTests(unittest.TestCase):
    def test_a_github_store_refuses_the_local_owner(self):
        for name in (LOCAL, "LOCAL--record/k3f5x2m7q4w6z5ab", "local--record/anything", None, 5):
            with self.subTest(name=name):
                with self.assertRaises(record_store.StoreError):
                    record_store.GithubStore(name)
        record_store.GithubStore("local--records/x")   # another owner is a GitHub name

    def test_for_repo_gives_one_store_per_name(self):
        first = record_store.for_repo(REPO)
        self.assertIs(first, record_store.for_repo(REPO))
        self.assertIsNot(first, record_store.for_repo("octo-org/other"))
        self.assertEqual(REPO, first.repo)

    def test_the_names(self):
        self.assertTrue(record_store.is_local(LOCAL))
        for name in (REPO, "local--record/short", "local--record/K3F5X2M7Q4W6Z5AB", "local--record/k3f5x2m7q4w6z5a1",
                     "local--record/k3f5x2m7q4w6z5ab/", "x" + LOCAL, None, 5):
            self.assertFalse(record_store.is_local(name), name)
        self.assertTrue(recortex.valid_repo(LOCAL))   # two hyphens in a row pass GitHub's own pattern, so the key decides

    def test_the_error_classes_are_gh_errors(self):
        for kind in (record_store.StoreError, record_store.Missing, record_store.Unsupported):
            self.assertTrue(issubclass(kind, recortex.GhError))
        self.assertTrue(issubclass(record_store.Missing, record_store.StoreError))


class KindOfTests(unittest.TestCase):
    def test_the_key_and_the_name_must_agree_both_ways(self):
        cases = [
            ({"repo": REPO}, "github"),
            ({"repo": REPO, "store": "github"}, "github"),
            ({"repo": LOCAL, "store": "local"}, "local"),
        ]
        for config, wanted in cases:
            with self.subTest(config=config):
                self.assertEqual(wanted, record_store.kind_of(config))
        refused = [
            ({"repo": LOCAL}, record_store.NOT_AGREE),                      # the name says local, the key says nothing
            ({"repo": LOCAL, "store": "github"}, record_store.NOT_AGREE),   # the key says GitHub
            ({"repo": REPO, "store": "local"}, record_store.NOT_AGREE),     # the key says local, the name is GitHub's
            ({"repo": "LOCAL--record/k3f5x2m7q4w6z5ab", "store": "local"}, record_store.NOT_AGREE),
            ({"repo": "local--record/short", "store": "local"}, record_store.NOT_AGREE),
            ({"repo": "local--record/short"}, record_store.NOT_AGREE),
            ({"repo": REPO, "store": "gitlab"}, record_store.UNKNOWN_KIND),
            ({"repo": REPO, "store": 5}, record_store.UNKNOWN_KIND),
            ({"repo": REPO, "store": ["local"]}, record_store.UNKNOWN_KIND),
        ]
        for config, line in refused:
            with self.subTest(config=config):
                with self.assertRaises(SystemExit) as caught:
                    record_store.kind_of(config)
                self.assertEqual(line, str(caught.exception))
                self.assertIsInstance(caught.exception, record_store.ConfigRefused)


if __name__ == "__main__":
    unittest.main()

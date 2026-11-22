"""A first Project Map in one operation and one hub comment (1.7.0): the `seed` change holds a whole tree of branches, is made by a
user on a map that has none, and later changes apply on its branches. Offline: the hub is the fake GitHub."""

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import index  # noqa: E402
import mind  # noqa: E402
import project_map as model  # noqa: E402
import project_map_io as api  # noqa: E402
import recortex  # noqa: E402


def tree(count=15):
    """A first tree of `count` branches: a few roots, children named by their parents, one link, one piece of evidence."""
    branches = [{"id": "goal", "title": "Goal", "description": "What the project is for."},
                {"id": "method", "title": "Method", "parent": "goal", "evidence": ["issue:12"]},
                {"id": "data", "title": "Data"}]
    for number in range(count - 3):
        parent = ("goal", "method", "data")[number % 3]
        branches.append({"id": "b%d" % number, "title": "Branch %d" % number, "parent": parent})
    branches[2]["links"] = ["goal", "b0"] if count > 3 else ["goal"]   # a link to a branch that comes later in the list
    return branches[:count]


class ReducerTests(unittest.TestCase):
    def seed(self, state=None, branches=None, **extra):
        state = state or model.empty()
        change = dict({"action": "seed", "branches": branches if branches is not None else tree()}, **extra)
        return state, model.prepare(state, change)

    def test_fifteen_branches_with_parents_and_links_are_one_operation_and_replay_gives_fifteen_nodes(self):
        state, op = self.seed()
        self.assertEqual("seed", op["change"]["action"])
        after = model.apply(state, op)
        self.assertEqual(15, len(after["nodes"]))
        self.assertEqual("goal", after["nodes"]["method"]["parent"])
        self.assertEqual(["goal", "b0"], after["nodes"]["data"]["links"])
        self.assertEqual(["issue:12"], after["nodes"]["method"]["evidence"])
        self.assertEqual(1, len(after["history"]))
        event = after["history"][0]
        self.assertEqual(("seed", "goal", "Goal", None, 15), (event["action"], event["node"], event["title"],
                                                              event["before_title"], event["count"]))
        self.assertEqual(sorted(after["nodes"]), event["affected"])
        replayed = model.replay("octo-org/octo-repo", 4, {"octocat"}, [
            {"id": 7, "author": "octocat", "created_at": "2026-11-22T10:00:00Z",
             "body": model.encode("octo-org/octo-repo", 4, op)}])
        self.assertEqual(15, len(replayed["nodes"]))
        self.assertEqual(after["revision"], replayed["revision"])

    def test_ids_are_given_to_a_change_and_to_each_branch_that_has_none(self):
        branches = [{"title": "One"}, {"title": "Two"}]
        op = model.prepare(model.empty(), {"action": "seed", "branches": branches})
        ids = [b["id"] for b in op["change"]["branches"]]
        self.assertEqual(3, len({op["change"]["id"]} | set(ids)))
        self.assertNotIn("id", branches[0])   # the caller's change is not changed
        self.assertEqual(2, len(model.apply(model.empty(), op)["nodes"]))

    def test_a_second_seed_a_seed_on_a_map_with_one_node_and_a_seed_by_the_observer_are_refused(self):
        state, op = self.seed()
        after = model.apply(state, op)
        with self.assertRaisesRegex(model.MapError, "already has branches"):
            model.prepare(after, {"action": "seed", "branches": [{"id": "x", "title": "X"}]})
        one = model.apply(model.empty(), model.prepare(model.empty(), {"action": "add", "id": "only", "title": "Only"}))
        with self.assertRaisesRegex(model.MapError, "already has branches"):
            model.prepare(one, {"action": "seed", "branches": [{"id": "x", "title": "X"}]})
        for source in ("observer",):
            with self.assertRaisesRegex(model.MapError, "A first map is made by a user"):
                model.prepare(model.empty(), {"action": "seed", "branches": [{"id": "x", "title": "X"}]}, source)

    def test_a_deleted_map_is_not_empty(self):
        state = model.empty()
        for change in ({"action": "add", "id": "a", "title": "A"}, {"action": "delete", "id": "a", "children": "keep"}):
            state = model.apply(state, model.prepare(state, change))
        with self.assertRaisesRegex(model.MapError, "already has branches"):
            model.prepare(state, {"action": "seed", "branches": [{"id": "x", "title": "X"}]})

    def test_a_parent_that_comes_later_a_duplicate_id_and_a_duplicate_title_under_one_parent_are_refused(self):
        cases = {
            "parent later": ([{"id": "child", "title": "Child", "parent": "root"}, {"id": "root", "title": "Root"}], "missing"),
            "duplicate id": ([{"id": "a", "title": "A"}, {"id": "a", "title": "B"}], "already in use"),
            "duplicate title": ([{"id": "a", "title": "Same"}, {"id": "b", "title": "same"}], "duplicate"),
            "own parent": ([{"id": "a", "title": "A", "parent": "a"}], "missing|ancestor"),
            "a link to nothing": ([{"id": "a", "title": "A", "links": ["ghost"]}], "missing"),
            "a link to itself": ([{"id": "a", "title": "A", "links": ["a"]}], "itself"),
            "a bad id": ([{"id": "a b", "title": "A"}], "identifier"),
            "no title": ([{"id": "a"}], "text"),
            "another key": ([{"id": "a", "title": "A", "deleted": True}], "branches"),
            "not a dict": (["a"], "branches"),
        }
        for name, (branches, words) in cases.items():
            with self.assertRaisesRegex(model.MapError, words, msg=name):
                model.prepare(model.empty(), {"action": "seed", "branches": branches})

    def test_the_same_title_under_different_parents_is_fine(self):
        branches = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}, {"id": "a1", "title": "Notes", "parent": "a"},
                    {"id": "b1", "title": "Notes", "parent": "b"}]
        self.assertEqual(4, len(model.apply(model.empty(), model.prepare(model.empty(), {"action": "seed", "branches": branches}))["nodes"]))

    def test_two_hundred_branches_pass_and_two_hundred_and_one_do_not(self):
        many = lambda count: [{"id": "n%d" % n, "title": "Branch %d" % n} for n in range(count)]
        self.assertEqual(200, len(model.apply(model.empty(), model.prepare(model.empty(), {"action": "seed", "branches": many(200)}))["nodes"]))
        with self.assertRaisesRegex(model.MapError, "one to 200"):
            model.prepare(model.empty(), {"action": "seed", "branches": many(201)})
        with self.assertRaisesRegex(model.MapError, "one to 200"):
            model.prepare(model.empty(), {"action": "seed", "branches": []})
        with self.assertRaisesRegex(model.MapError, "one to 200"):
            model.prepare(model.empty(), {"action": "seed", "branches": "x"})

    def test_a_seed_cannot_carry_another_field_and_the_change_needs_its_own_fields_only(self):
        with self.assertRaisesRegex(model.MapError, "Unknown map action or field"):
            model.prepare(model.empty(), {"action": "seed", "branches": [{"id": "a", "title": "A"}], "title": "X"})

    def test_later_add_rename_and_move_apply_on_seeded_nodes_and_history_reads_started(self):
        state, op = self.seed()
        state = model.apply(state, op)
        for change in ({"action": "add", "id": "extra", "title": "Extra", "parent": "method"},
                       {"action": "rename", "id": "b0", "title": "Renamed"},
                       {"action": "move", "id": "b1", "parent": "data"}):
            state = model.apply(state, model.prepare(state, change))
        self.assertEqual(16, len(state["nodes"]))
        self.assertEqual("Renamed", state["nodes"]["b0"]["title"])
        self.assertEqual(["seed", "add", "rename", "move"], [e["action"] for e in state["history"]])

    def test_history_says_started_the_map_with_the_count(self):
        state, op = self.seed()
        state = model.apply(state, op)
        text = mind.render({"repo": ts.REPO, "name": "Project", "project_map": state})
        self.assertEqual([], ts.page_problems(text))
        self.assertIn("Started the map with 15 branches", " ".join(ts.PageReader(text).texts).replace(" ,", ","))
        one = model.apply(model.empty(), model.prepare(model.empty(), {"action": "seed", "branches": [{"id": "x", "title": "X"}]}))
        self.assertIn("Started the map with 1 branch", " ".join(ts.PageReader(mind.render(
            {"repo": ts.REPO, "name": "Project", "project_map": one})).texts))

    def test_the_encoded_comment_is_one_json_block_and_a_hostile_title_stays_data(self):
        hostile = "<img src=x onerror=alert(1)> @octocat https://example.com [x](y)"
        op = model.prepare(model.empty(), {"action": "seed", "branches": [{"id": "x", "title": hostile}]})
        body = model.encode(ts.REPO, 4, op)
        self.assertTrue(body.startswith(model.MARKER + "\n```json\n"))
        lines = body.split("\n")
        self.assertEqual([model.MARKER, "```json", "```"], [lines[0], lines[1], lines[3]])
        self.assertEqual(4, len(lines))   # the whole operation is one line of JSON
        text = mind.render({"repo": ts.REPO, "name": "Project", "project_map": model.apply(model.empty(), op)})
        self.assertEqual([], ts.page_problems(text))
        self.assertNotIn("<img src=x", text)


class HubTests(ts.RecordSandbox):
    def setUp(self):
        super().setUp()
        self.in_process(extra={"CLAUDE_PROJECT_DIR": self.project})
        self.hub = self.config["hub"]

    def comments(self):
        return len(self.repo()["comments"])

    def test_one_preview_one_apply_one_hub_comment_and_the_replay_gives_the_tree(self):
        before = self.comments()
        preview = api.preview(ts.REPO, self.hub, {"action": "seed", "branches": tree()})
        self.assertEqual(before, self.comments())   # a preview posts nothing
        self.assertEqual(15, len(preview["affected"]))
        done = api.commit(ts.REPO, self.hub, preview["operation"])
        self.assertEqual(before + 1, self.comments())   # one comment for fifteen branches
        self.assertEqual(15, len(done["state"]["nodes"]))
        self.assertEqual(done, api.commit(ts.REPO, self.hub, preview["operation"]))
        self.assertEqual(before + 1, self.comments())
        self.assertEqual(15, len(api.current(ts.REPO, self.hub)["nodes"]))
        index.sync(ts.REPO, self.hub, rebuild=True, log=lambda *args: None)
        db = index.connect(ts.REPO, create=False)
        self.addCleanup(db.close)
        self.assertEqual(15, len(model.read_map(db, self.hub, recortex.allowed_users(ts.REPO))["nodes"]))

    def test_a_second_seed_is_refused_at_the_preview(self):
        api.commit(ts.REPO, self.hub, api.preview(ts.REPO, self.hub, {"action": "seed", "branches": tree(3)})["operation"])
        with self.assertRaisesRegex(api.MapError, "already has branches"):
            api.preview(ts.REPO, self.hub, {"action": "seed", "branches": [{"id": "x", "title": "X"}]})

    def test_the_observer_cannot_preview_a_seed(self):
        with self.assertRaisesRegex(api.MapError, "first map is made by a user"):
            api.preview(ts.REPO, self.hub, {"action": "seed", "branches": [{"id": "x", "title": "X"}]}, "observer")

    def test_a_body_over_the_limit_says_to_seed_fewer_branches(self):
        wide = [{"id": "n%d" % n, "title": "Branch %d" % n, "description": "x" * 1500} for n in range(60)]
        with self.assertRaisesRegex(api.MapError, "This first map is too large for one hub comment. Seed fewer branches, then add the rest."):
            api.preview(ts.REPO, self.hub, {"action": "seed", "branches": wide})
        with self.assertRaisesRegex(api.MapError, "This change is too large for one hub comment."):
            api.preview(ts.REPO, self.hub, {"action": "add", "id": "big", "title": "Big", "description": "x" * 1600,
                                            "evidence": [chr(0x1F600 + n) * 299 for n in range(64)]})

    def test_the_privacy_rules_look_at_every_branch_of_a_seed(self):
        self.write_config(dict(self.config, restricted_patterns=[{"name": "instrument", "pattern": "Thermal calibration"}]))
        branches = [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "description": "About the Thermal calibration rig"}]
        with self.assertRaisesRegex(api.MapError, "publication checks"):
            api.preview(ts.REPO, self.hub, {"action": "seed", "branches": branches})
        self.assertEqual({}, api.current(ts.REPO, self.hub)["nodes"])

    def test_a_1_6_0_reader_skips_what_it_does_not_know(self):
        """1.6.0's reducer knows no `seed`: the comment is skipped as an unknown action, never an error, and a later change that
        names a seeded branch is skipped too (references/upgrading.md says to update before seeding)."""
        op = api.preview(ts.REPO, self.hub, {"action": "seed", "branches": tree(3)})["operation"]
        old_actions = {key: value for key, value in model.ACTIONS.items() if key != "seed"}
        with mock.patch.object(model, "ACTIONS", old_actions):
            self.assertEqual({}, model.replay(ts.REPO, self.hub, {"octocat"}, [
                {"id": 1, "author": "octocat", "created_at": "t", "body": model.encode(ts.REPO, self.hub, op)}])["nodes"])

    def test_the_observers_map_proposals_never_name_a_seed(self):
        import observer_map
        self.assertNotIn("seed", observer_map.ACTIONS)


if __name__ == "__main__":
    unittest.main()

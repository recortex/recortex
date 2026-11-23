"""No line hands off (1.8.0): recortex never tells the user to run a command in a terminal of their own. A line either names the
question to ask (`acts.py ask KIND`) or is a read-only check that the session runs and reports. This scans the documents, every
string a script can print, the page's script and the hook table for the phrases that send the user somewhere else. Offline."""

import ast
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "recortex")
PHRASES = re.compile(r"own terminal|separate terminal|outside Claude Code|in a terminal|your terminal|their terminal", re.I)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def documents():
    found = [os.path.join(ROOT, "README.md")]
    for folder in (SKILL, os.path.join(ROOT, "commands"), os.path.join(ROOT, "docs"), os.path.join(ROOT, "agents")):
        for place, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
            found += [os.path.join(place, name) for name in files if name.endswith(".md")]
    return sorted(found)


def under(heading, text):
    """The numbers of the lines of the section that starts with `heading` and goes to the next heading of the same or a higher level."""
    lines = text.split("\n")
    start = next((n for n, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return set()
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for n in range(start + 1, len(lines)):
        if re.match(r"#{1,%d} " % level, lines[n]):
            end = n
            break
    return set(range(start, end))


# What may name a terminal, as a literal table with a reason for each. A path that is not here may not.
ALLOWED = {
    "skills/recortex/references/upgrading.md": (
        "the entries of versions before 1.8.0 say what those versions did",
        lambda text: set(range(next(n for n, line in enumerate(text.split("\n")) if line.startswith("## Version 1.7.1")),
                               len(text.split("\n"))))),
    "skills/recortex/references/observer.md": (
        "one section says that the commands also run from the user's own terminal, without a question, and the paragraph that "
        "explains how the plugin tells that terminal from a session's",
        lambda text: under("## From a terminal", text) | {n for n, line in enumerate(text.split("\n")) if "on_terminal()" in line}),
    "docs/security.md": (
        "the paragraph that explains how the plugin tells the user's own terminal from a session's",
        lambda text: {n for n, line in enumerate(text.split("\n")) if "on_terminal()" in line}),
}


def docstring_nodes(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def printed_strings(source):
    """The strings of a script that are not its docstrings: what it can print, in an argparse help or a note. Comments are not
    strings."""
    tree = ast.parse(source)
    skip = docstring_nodes(tree)
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip]


def offenders_in_source(source):
    return [(line, text) for line, text in printed_strings(source) if PHRASES.search(text)]


class HandOffTests(unittest.TestCase):
    def test_no_document_tells_the_user_to_use_a_terminal_except_where_it_is_said_why(self):
        found, used = [], set()
        for path in documents():
            relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
            text = read(path)
            keep = ALLOWED[relative][1](text) if relative in ALLOWED else set()
            for number, line in enumerate(text.split("\n")):
                if PHRASES.search(line):
                    if number in keep:
                        used.add(relative)
                    else:
                        found.append("%s:%d: %s" % (relative, number + 1, line.strip()[:100]))
        self.assertEqual([], found)
        self.assertEqual(sorted(ALLOWED), sorted(used))   # every entry of the table is still needed

    def test_no_script_prints_such_a_line(self):
        folder = os.path.join(SKILL, "scripts")
        found = []
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py"):
                for line, text in offenders_in_source(read(os.path.join(folder, name))):
                    found.append("%s:%d: %s" % (name, line, text.strip()[:100]))
        self.assertEqual([], found)

    def test_the_page_script_and_the_hook_table_name_no_terminal(self):
        found = []
        folder = os.path.join(SKILL, "page")
        for name in sorted(os.listdir(folder)):
            if name.endswith(".js"):
                found += ["%s:%d" % (name, n + 1) for n, line in enumerate(read(os.path.join(folder, name)).split("\n"))
                          if PHRASES.search(line)]
        hooks = read(os.path.join(ROOT, "hooks", "hooks.json"))
        found += ["hooks.json:%d" % (n + 1) for n, line in enumerate(hooks.split("\n")) if PHRASES.search(line)]
        json.loads(hooks)
        self.assertEqual([], found)

    def test_the_scan_fires_on_a_planted_string_and_stays_quiet_on_a_docstring_and_a_comment(self):
        planted = 'def run():\n    print("Run it in your own terminal.")\n'
        self.assertEqual([2], [line for line, _ in offenders_in_source(planted)])
        quiet = ('"""Describes on_terminal(): the user\'s own terminal."""\n\n\ndef run():\n    """Not in a terminal."""\n'
                 '    # in a separate terminal\n    return "fine"\n')
        self.assertEqual([], offenders_in_source(quiet))
        text = "line one\nline in your terminal\n## Version 1.7.1\nthen"
        self.assertEqual({2, 3}, ALLOWED["skills/recortex/references/upgrading.md"][1](text) - {0, 1})
        self.assertTrue(PHRASES.search("Run it OUTSIDE Claude Code"))
        self.assertTrue(PHRASES.search("their terminal"))
        self.assertIsNone(PHRASES.search("the terminal output shows"))

    def test_every_printed_line_that_names_an_act_names_its_question(self):
        """The acts a refusal can name are the kinds of acts.py, each by the one form `acts.py ask KIND`."""
        import sys
        sys.path.insert(0, os.path.join(SKILL, "scripts"))
        import acts
        for kind in acts.ACTS:
            note = acts.ASK % kind
            self.assertIn("acts.py ask %s" % kind, note)
            self.assertNotRegex(note, PHRASES)


if __name__ == "__main__":
    unittest.main()

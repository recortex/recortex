"""The ask agent can only read (S5 of the 1.5.0 review): the PreToolUse check that stops every Bash command of the
`recortex:ask` agent but one `index.py query` read, in every project, whether or not the observer is on. Offline."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_scripts as ts  # noqa: E402

sys.path.insert(0, ts.SCRIPTS)
import pretool_guard  # noqa: E402

INDEX = os.path.join(ts.SCRIPTS, "index.py")
GUARD = os.path.join(ts.SCRIPTS, "run_pretool_guard.sh")
BASH = shutil.which("bash")
ASK = "recortex:ask"
ALLOWED = (
    'python3 %s query find --words "why strict quoting"' % INDEX,
    "python3 %s query recall --issue 5" % INDEX,
    "python3 %s query timeline --issue 5 --json" % INDEX,
    "python3 %s query tasks" % INDEX,
    "python3 %s query plan --json" % INDEX,
    "python3 %s query find --words 'reader speed' --issue 3" % INDEX,
)
DENIED = (
    "cd %s && python3 %s query find --words x" % (ts.SCRIPTS, INDEX),   # the needless cd the agent adds
    "python3 %s query find --words x; echo done" % INDEX,
    "python3 %s query find --words x | cat" % INDEX,
    "python3 %s query find --words x > out.txt" % INDEX,
    "python3 %s query find --words x && gh issue comment 5 --body hi" % INDEX,
    "python3 %s query find --words $(whoami)" % INDEX,
    "python3 %s query find --words `whoami`" % INDEX,
    "python3 %s query find --words x\necho second" % INDEX,
    "python3 %s query find --words 'a;b'" % INDEX,           # an operator inside quotes is refused as well
    "python3 %s query sync" % INDEX,                          # not one of the five reads
    "python3 %s sync" % INDEX,
    "python3 %s query find" % os.path.join(ts.SCRIPTS, "post_verified.py"),
    "python3 %s comment --issue 5 --body-file x.md" % os.path.join(ts.SCRIPTS, "post_verified.py"),
    "python3 %s enable --level everything --model sonnet --yes" % os.path.join(ts.SCRIPTS, "observer.py"),
    "gh issue comment 5 --body hi",
    "echo agent-probe",
    "PYTHONSTARTUP=x python3 %s query find --words x" % INDEX,
    "python3 /tmp/elsewhere/index.py query find --words x",
    "python3 -c 'import os' %s query find" % INDEX,
    "",
)


def call(command, agent=ASK, **extra):
    data = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}
    if agent:
        data.update(agent_type=agent, agent_id="a1b2c3")
    data.update(extra)
    return data


def run_script(data, env=None, raw=None):
    """The hook as Claude Code starts it: the shell script, the call on its stdin. Returns (exit code, stdout)."""
    done = subprocess.run([BASH, GUARD], input=raw if raw is not None else json.dumps(data), capture_output=True,
                          text=True, env=env or dict(os.environ), timeout=20)
    return done.returncode, done.stdout


def reason(out):
    value = json.loads(out)["hookSpecificOutput"]
    assert value["hookEventName"] == "PreToolUse" and value["permissionDecision"] == "deny", value
    return value["permissionDecisionReason"]


class CommandTests(unittest.TestCase):
    def test_the_five_reads_pass(self):
        for command in ALLOWED:
            self.assertIsNone(pretool_guard.ask_decision(call(command)), command)

    def test_every_other_command_is_denied_with_the_same_fixed_words(self):
        for command in DENIED:
            out = pretool_guard.ask_decision(call(command))
            self.assertIsNotNone(out, command)
            self.assertEqual(pretool_guard.DENIAL % ts.SCRIPTS, out["hookSpecificOutput"]["permissionDecisionReason"], command)

    def test_a_shell_operator_is_refused_even_after_the_right_prefix(self):
        for operator in (";", "&&", "||", "|", ">", ">>", "<", "`x`", "$(x)", "${x}", "\n", "\\", "(x)", "&"):
            command = "python3 %s query find --words x %s y" % (INDEX, operator)
            self.assertFalse(pretool_guard.allowed_command(command), repr(operator))
        self.assertTrue(pretool_guard.allowed_command("python3 %s query find --words 'two words' --issue 4" % INDEX))

    def test_the_index_script_is_the_plugins_own_by_its_whole_absolute_path(self):
        with tempfile.TemporaryDirectory() as folder:
            link = os.path.join(folder, "index.py")
            os.symlink(INDEX, link)
            self.assertFalse(pretool_guard.allowed_command("python3 %s query find --words x" % link))   # a link elsewhere
            elsewhere = os.path.join(folder, "other", "index.py")
            os.makedirs(os.path.dirname(elsewhere))
            with open(elsewhere, "w") as handle:
                handle.write("print(1)\n")
            self.assertFalse(pretool_guard.allowed_command("python3 %s query find --words x" % elsewhere))
            # a path through a link to the plugin's folder is another string: refused as well
            folder_link = os.path.join(folder, "scripts")
            os.symlink(ts.SCRIPTS, folder_link)
            self.assertFalse(pretool_guard.allowed_command("python3 %s/index.py query find --words x" % folder_link))
            self.assertFalse(pretool_guard.allowed_command("python3 %s/../scripts/index.py query find --words x" % ts.SCRIPTS))
        relative = os.path.relpath(INDEX, os.getcwd())
        self.assertFalse(os.path.isabs(relative))
        self.assertFalse(pretool_guard.allowed_command("python3 %s query find --words x" % relative))
        self.assertFalse(pretool_guard.allowed_command("python3 index.py query find --words x"))
        self.assertTrue(pretool_guard.allowed_command("python3 %s query find --words x" % INDEX))

    def test_the_denial_holds_no_word_of_the_command(self):
        out = pretool_guard.ask_decision(call("echo ZZMARK-HOSTILE <script>"))
        self.assertNotIn("ZZMARK", json.dumps(out))
        self.assertNotIn("script>", json.dumps(out))

    def test_another_agent_and_the_main_session_are_not_looked_at(self):
        for agent in ("recortex:record-auditor", "recortex:record-drafter", "general-purpose", None):
            self.assertIsNone(pretool_guard.decide(json.dumps(call("gh issue comment 5 --body hi", agent=agent))), agent)
        self.assertIsNone(pretool_guard.decide(json.dumps(call("echo hi that says recortex:ask in it", agent=None))))

    def test_a_tool_that_is_not_bash_is_denied_for_the_ask_agent(self):
        data = call(ALLOWED[0])
        data["tool_name"] = "Write"
        self.assertIsNotNone(pretool_guard.ask_decision(data))
        data = call(ALLOWED[0])
        data["tool_input"] = "not a mapping"
        self.assertIsNotNone(pretool_guard.ask_decision(data))


OBSERVER = os.path.join(ts.SCRIPTS, "observer.py")
SETUP = os.path.join(ts.SCRIPTS, "setup.py")
THROUGH_A_TERMINAL = (
    'script -qc "python3 %s later ab12cd34" /dev/null' % OBSERVER,
    "unbuffer python3 %s enable --level everything --model sonnet --yes" % OBSERVER,
    "expect -c 'spawn python3 %s undo 3; send \"undo\\r\"'" % OBSERVER,
    'python3 -c "import pty; pty.spawn([\'python3\', \'%s\', \'discard\', \'x\'])"' % OBSERVER,
    "python3 -m pty python3 %s observer --level notes --yes" % SETUP,
    'socat - EXEC:"python3 %s disable",pty' % OBSERVER,
    "faketty python3 %s drafts drop ab12cd34" % OBSERVER,
    "echo undo | script -qec 'python3 %s undo 4' /dev/null" % OBSERVER,
    "cat x.script; python3 %s later ab12cd34" % OBSERVER,             # after a dot
    "(unbuffer python3 %s later ab12cd34)" % OBSERVER,                # in brackets
    "/usr/bin/script -qc 'python3 %s later ab12cd34' /dev/null" % OBSERVER,   # after a slash
    "python3 -c 'import pty;pty.spawn([\"python3\",\"%s\"])'" % OBSERVER,   # before a bracket's dot and after a semicolon
    "tmux new-session -d 'python3 %s later ab12cd34'" % OBSERVER,
    "screen -dm python3 %s later ab12cd34" % OBSERVER,
    "ssh localhost python3 %s later ab12cd34" % OBSERVER,
    "setsid python3 %s later ab12cd34" % OBSERVER,
    "nohup python3 %s later ab12cd34 &" % OBSERVER,
    "python3 -c 'import zpty' %s" % OBSERVER,
    "env -u CLAUDECODE python3 %s later ab12cd34" % OBSERVER,           # the variable named beside observer.py
    "CLAUDECODE= python3 %s discard x" % OBSERVER,
    "SCRIPT -qc 'python3 %s observer --level notes --yes' /dev/null" % SETUP,   # a helper name in upper case
    "Unbuffer python3 %s observer --strong opus --yes" % SETUP,
    "env -u CLAUDECODE python3 %s observer --level everything --yes" % SETUP,    # setup.py is the user's command as well
    "CLAUDECODE=0 python3 %s apply --answers a.json --yes" % SETUP,
)
NOT_THROUGH_A_TERMINAL = (
    "python3 %s status" % OBSERVER,
    "python3 %s pending --waiting --json" % OBSERVER,
    "cat scripts.txt; python3 %s status --source-details" % OBSERVER,   # `scripts` is not `script`
    "python3 %s status --no-script" % OBSERVER,                            # nor is a dashed option
    "python3 %s observer --level notes --dry-run" % SETUP,
    "ls %s" % ts.SCRIPTS,
    "script -qc 'ls' /dev/null",   # a terminal for another command is not this check's business
)


class TerminalTests(unittest.TestCase):
    """A terminal a session makes is not the user's: observer.py and setup.py are not run through a program that makes one."""

    def test_each_helper_of_the_list_is_caught_in_each_way_of_writing_it(self):
        for command in THROUGH_A_TERMINAL:
            out = pretool_guard.decide(json.dumps(call(command, agent=None)))
            self.assertEqual(pretool_guard.TERMINAL_DENIAL, out["hookSpecificOutput"]["permissionDecisionReason"], command)
        for helper in pretool_guard.PTY_HELPERS:
            command = "%s python3 %s status" % (helper, OBSERVER)
            self.assertIsNotNone(pretool_guard.terminal_decision(call(command, agent=None)), helper)

    def test_the_same_commands_without_a_terminal_helper_go_on(self):
        for command in NOT_THROUGH_A_TERMINAL:
            self.assertIsNone(pretool_guard.terminal_decision(call(command, agent=None)), command)
            self.assertIsNone(pretool_guard.decide(json.dumps(call(command, agent=None))), command)

    def test_a_subagent_is_stopped_too_and_the_denial_is_fixed_words(self):
        out = pretool_guard.decide(json.dumps(call(THROUGH_A_TERMINAL[0], agent="general-purpose")))
        self.assertEqual(pretool_guard.TERMINAL_DENIAL, out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotIn("ab12cd34", json.dumps(out))
        self.assertIn("Ask the user the question the command names.", pretool_guard.TERMINAL_DENIAL)
        self.assertNotIn("own terminal", pretool_guard.TERMINAL_DENIAL)

    def test_the_hook_script_denies_them_in_every_project_and_leaves_the_rest(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = {"PATH": os.environ["PATH"], "HOME": folder.name, "XDG_STATE_HOME": os.path.join(folder.name, "state")}
        for command in THROUGH_A_TERMINAL:
            code, out = run_script(call(command, agent=None), env)
            self.assertEqual((0, pretool_guard.TERMINAL_DENIAL), (code, reason(out)), command)
        for command in NOT_THROUGH_A_TERMINAL:
            self.assertEqual((0, ""), run_script(call(command, agent=None), env), command)


class ScriptTests(unittest.TestCase):
    """The hook as Claude Code runs it, in a shell, with a clean environment: no project, no approval, no observer."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.env = {"PATH": os.environ["PATH"], "HOME": folder.name, "XDG_STATE_HOME": os.path.join(folder.name, "state"),
                    "XDG_CACHE_HOME": os.path.join(folder.name, "cache"), "XDG_CONFIG_HOME": os.path.join(folder.name, "config")}
        self.folder = folder.name

    def test_the_allowed_reads_print_nothing_and_the_rest_are_denied_in_a_folder_with_no_observer(self):
        for command in ALLOWED:
            self.assertEqual((0, ""), run_script(call(command), self.env), command)
        for command in DENIED:
            code, out = run_script(call(command), self.env)
            self.assertEqual(0, code, command)
            self.assertEqual(pretool_guard.DENIAL % ts.SCRIPTS, reason(out), command)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "state")))   # nothing was written for any of them

    def test_the_denial_holds_with_the_observer_on_and_with_it_off(self):
        project = os.path.join(self.folder, "project")
        os.makedirs(project)
        for config in ({"repo": "octo-org/octo-repo", "hub": 1, "observer": False}, {"repo": "octo-org/octo-repo", "hub": 1}):
            with open(os.path.join(project, ".recortex.json"), "w") as handle:
                json.dump(config, handle)
            env = dict(self.env, CLAUDE_PROJECT_DIR=project)
            code, out = run_script(call("cd x && python3 %s query find --words x" % INDEX), env)
            self.assertEqual(pretool_guard.DENIAL % ts.SCRIPTS, reason(out))

    def test_other_calls_are_untouched_and_cost_no_python(self):
        for agent in (None, "recortex:record-reviewer", "general-purpose"):
            self.assertEqual((0, ""), run_script(call("gh issue comment 5 --body hi", agent=agent), self.env), agent)
        self.assertEqual((0, ""), run_script(None, self.env, raw="not json at all"))
        self.assertEqual((0, ""), run_script(None, self.env, raw=""))

    def test_an_input_that_cannot_be_read_is_a_denial_for_the_ask_agent_only(self):
        code, out = run_script(None, self.env, raw='{"agent_type": "%s", "tool_input": {"command": ' % ASK)
        self.assertEqual((0, pretool_guard.UNREADABLE), (code, reason(out)))
        self.assertEqual((0, ""), run_script(None, self.env, raw='{"agent_type": "general-purpose", "tool_input": {"command"'))

    def test_without_python_the_ask_agent_is_stopped_and_nothing_else_is(self):
        with tempfile.TemporaryDirectory() as bin_folder:
            for name in ("cat", "dirname"):
                os.symlink(subprocess.run(["which", name], capture_output=True, text=True).stdout.strip(),
                           os.path.join(bin_folder, name))
            env = dict(self.env, PATH=bin_folder)
            code, out = run_script(call(ALLOWED[0]), env)
            self.assertEqual((0, pretool_guard.UNREADABLE), (code, reason(out)))   # the same words as Python's own
            self.assertEqual((0, ""), run_script(call("echo hi", agent=None), env))
            self.assertEqual((0, ""), run_script(call("echo hi", agent="general-purpose"), env))


class RegistrationTests(unittest.TestCase):
    def test_the_guard_is_registered_before_every_bash_call_in_the_plugin_and_in_the_snippet(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json")) as handle:
            pre = json.load(handle)["hooks"]["PreToolUse"]
        self.assertEqual(["Bash"], [entry["matcher"] for entry in pre])
        commands = [h["command"] for h in pre[0]["hooks"]]
        self.assertIn('"${CLAUDE_PLUGIN_ROOT}"/skills/recortex/scripts/run_pretool_guard.sh', commands)
        self.assertTrue(all(h["timeout"] <= 5 for h in pre[0]["hooks"]))
        self.assertTrue(os.access(GUARD, os.X_OK))
        with open(os.path.join(ts.TEMPLATES, "settings-hook-snippet.json")) as handle:
            snippet = [h["command"] for h in json.load(handle)["hooks"]["PreToolUse"][0]["hooks"]]
        self.assertIn('"$HOME/.claude/skills/recortex/scripts/run_pretool_guard.sh"', snippet)

    def test_the_agent_says_the_hook_enforces_what_it_may_run(self):
        with open(os.path.join(ROOT, "agents", "ask.md")) as handle:
            text = " ".join(handle.read().split())
        self.assertIn("tools: Bash", text)
        self.assertIn("A hook stops any other command", text)


class HeredocTests(unittest.TestCase):
    """The guard reads the commands that run, not the text of a note that is being written (1.9.0, 18.3). A body is dropped only when
    the whole command is a note: every line outside a body is one of a few plain shapes (a list of bad receivers cannot be made
    tight). Anything else is matched as a whole."""

    HELPER = "set" + "sid"
    BODY = "note: %s python3 %s status" % (HELPER, OBSERVER)

    def denied(self, command):
        return pretool_guard.terminal_decision(call(command, agent=None)) is not None

    def note(self, opening, body=None, end="EOF"):
        return "%s\n%s\n%s" % (opening, self.BODY if body is None else body, end)

    # ---- the plain cases pass
    def test_a_note_written_with_cat_or_tee_or_a_commit_or_a_post_is_not_read(self):
        for opening in ("cat >> notes.md <<'EOF'", "cat > notes.md <<'EOF'", 'cat > notes.md <<"EOF"', "cat <<'EOF' > notes.md",
                        "cat <<'EOF' >> notes.md", "tee notes.md <<'EOF'", "tee -a a.md b.md <<'EOF'", "git commit -F - <<'EOF'",
                        "git commit -a -F - <<'EOF'", "gh issue comment 5 --repo octo-org/octo-repo --body-file - <<'EOF'",
                        "gh issue create --title Plain --body-file - <<'EOF'"):
            with self.subTest(opening=opening):
                self.assertFalse(self.denied(self.note(opening)))

    def test_an_unquoted_end_word_drops_a_body_that_the_shell_would_not_expand(self):
        self.assertFalse(self.denied(self.note("cat >> notes.md <<EOF")))
        self.assertFalse(self.denied(self.note("cat >> notes.md <<EOF", "a note\nwith two lines")))

    def test_one_heredoc_to_a_command_and_two_are_read_whole(self):
        command = "cat >> a.md <<'EOF'\n%s\nEOF\ncat >> b.md <<'EOF'\n%s\nEOF\n" % (self.BODY, self.BODY)
        self.assertTrue(self.denied(command))
        self.assertFalse(self.denied(self.note("cat >> a.md <<'EOF'") + "\n"))   # a final line break is no other line

    def test_a_hook_written_by_one_heredoc_and_run_by_the_commit_of_another_is_read(self):
        command = ("cat > .git/hooks/pre-commit <<'EOF'\n%s\nEOF\ngit commit -F - <<'EOF'\nmessage\nEOF" % self.BODY)
        self.assertTrue(self.denied(command))
        command = ("cat > hook.sh <<'EOF'\nplain\nEOF\ngit commit -F - <<'EOF'\n%s\nEOF" % self.BODY)
        self.assertTrue(self.denied(command))

    def test_only_a_note_file_is_written(self):
        for opening in ("cat > .notes.md <<'EOF'", "cat > ../x.md <<'EOF'", "cat > /tmp/x.md <<'EOF'", "cat > run.sh <<'EOF'",
                        "cat > .git/hooks/pre-commit <<'EOF'", "cat > /dev/tty <<'EOF'", "cat > notes <<'EOF'",
                        "cat > a/../b.md <<'EOF'", "cat > a/.b.md <<'EOF'", "cat > x.md.sh <<'EOF'", "tee run.sh <<'EOF'",
                        "tee .bashrc <<'EOF'", "cat <<'EOF' > ~/x.md", "cat <<'EOF' >> /etc/x.txt"):
            with self.subTest(opening=opening):
                self.assertTrue(self.denied(self.note(opening)))
        for opening in ("cat > notes.md <<'EOF'", "cat >> docs/notes.v2.md <<'EOF'", "cat > a_b/c-d.txt <<'EOF'"):
            with self.subTest(opening=opening):
                self.assertFalse(self.denied(self.note(opening)))

    def test_gh_is_the_three_built_in_commands_only_and_never_an_alias(self):
        for opening in ("gh myalias --body-file - <<'EOF'", "gh alias set x --body-file - <<'EOF'", "gh api --body-file - <<'EOF'",
                        "gh issue view 5 --body-file - <<'EOF'", "gh --body-file - <<'EOF'", "gh issue --body-file - <<'EOF'"):
            with self.subTest(opening=opening):
                self.assertTrue(self.denied(self.note(opening)))
        for opening in ("gh issue comment 5 --body-file - <<'EOF'", "gh pr create --title Plain --body-file - <<'EOF'",
                        "gh issue edit 5 --repo octo-org/octo-repo --body-file - <<'EOF'"):
            with self.subTest(opening=opening):
                self.assertFalse(self.denied(self.note(opening)))

    # ---- the seven ways past the first version, and the other shapes that keep the whole text
    def test_an_unquoted_body_is_expanded_by_the_shell_so_it_is_read(self):
        for body in ("$(%s python3 %s status)" % (self.HELPER, OBSERVER), "`%s python3 %s status`" % (self.HELPER, OBSERVER),
                     "%s python3 %s status \\" % (self.HELPER, OBSERVER)):
            with self.subTest(body=body):
                self.assertTrue(self.denied(self.note("cat > notes.md <<EOF", body)))

    def test_a_marker_in_a_comment_or_in_quotes_is_no_heredoc(self):
        for first in ('echo "cat > notes.md <<EOF"', "echo hi # cat > notes.md <<EOF", "echo 'x' ; cat > notes.md <<EOF",
                      "true && cat > notes.md <<EOF"):
            with self.subTest(first=first):
                self.assertTrue(self.denied("%s\n%s python3 %s status\nEOF" % (first, self.HELPER, OBSERVER)))

    def test_a_continuation_line_or_a_compound_command_is_not_judged_by_one_line(self):
        for command in ("cat > notes.md \\\n  <<'EOF'\n%s\nEOF" % self.BODY, "cat > notes.md <<'EOF' \\\n && true\n%s\nEOF" % self.BODY,
                        "{ cat > notes.md <<'EOF'\n%s\nEOF\n}" % self.BODY, "( cat > notes.md <<'EOF'\n%s\nEOF\n)" % self.BODY,
                        "if true; then cat > f <<'EOF'\n%s\nEOF\nfi" % self.BODY):
            with self.subTest(command=command[:40]):
                self.assertTrue(self.denied(command))

    def test_a_process_substitution_or_an_fd_redirection_keeps_the_text(self):
        for opening in ("cat <<'EOF' > >(bash)", "cat > f >&2 <<'EOF'", "cat > f 2>&1 <<'EOF'", "cat <<'EOF' >& 2",
                        "cat > f <<'EOF' | bash", "cat > f <<'EOF' && bash f", "cat > $HOME/f <<'EOF'", "cat > `x` <<'EOF'"):
            with self.subTest(opening=opening):
                self.assertTrue(self.denied(self.note(opening)))

    def test_a_body_file_post_is_plain_only_for_gh_itself(self):
        for opening in ("python3 --body-file - <<'EOF'", "bash --body-file - <<'EOF'", "env gh issue comment 5 --body-file - <<'EOF'",
                        "/usr/bin/gh issue comment 5 --body-file - <<'EOF'", "sudo gh issue comment 5 --body-file - <<'EOF'",
                        "foo=1 gh issue comment 5 --body-file - <<'EOF'", "command gh issue comment 5 --body-file - <<'EOF'",
                        "some-tool --body-file - <<'EOF'", "gh issue comment 5 --body-file - -x $y <<'EOF'"):
            with self.subTest(opening=opening):
                self.assertTrue(self.denied(self.note(opening)))

    def test_an_end_word_that_is_only_part_of_the_word_keeps_the_text(self):
        for opening in ("cat > f <<EOF$x", "cat > f <<EOF;bash", "cat > f <<'EOF'x", "cat > f <<EOF#"):
            with self.subTest(opening=opening):
                self.assertTrue(self.denied(self.note(opening)))
        self.assertTrue(self.denied("cat > f <<'EOF'\n%s\nEOFX" % self.BODY))   # no end line: the whole text stays
        self.assertTrue(self.denied("cat > f <<'EOF'\n%s\n EOF" % self.BODY))   # the end line is the end word alone
        self.assertTrue(self.denied("cat > f <<'EOF'\n%s\nEOF " % self.BODY))

    def test_a_file_written_by_a_heredoc_and_run_by_a_later_line_is_read(self):
        for later in ("bash notes.sh", "sh notes.sh", "python3 notes.py", "./notes.sh", "source notes.sh", ". notes.sh"):
            command = "cat > notes.sh <<'EOF'\n%s\nEOF\n%s" % (self.BODY, later)
            with self.subTest(later=later):
                self.assertTrue(self.denied(command))

    def test_the_hook_decision_names_the_denial_for_a_chain(self):
        command = ("cat > .git/hooks/pre-commit <<'EOF'\n%s\nEOF\ngit commit -F - <<'EOF'\nm\nEOF" % self.BODY)
        out = pretool_guard.decide(json.dumps(call(command, agent=None)))
        self.assertEqual(pretool_guard.TERMINAL_DENIAL, out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_the_text_for_a_shell_or_an_interpreter_is_still_denied(self):
        for first in ("bash", "sh", "zsh", "dash", "python3 -", "python -", "perl", "node", "env bash", "sudo bash", "xargs",
                      "ssh host", "tmux", "script -q", "cat | bash"):
            with self.subTest(first=first):
                self.assertTrue(self.denied("%s <<'EOF'\n%s\nEOF" % (first, self.BODY)))

    def test_a_heredoc_with_no_end_word_or_after_another_command_is_read(self):
        self.assertTrue(self.denied("cat >> notes.md <<'EOF'\n%s\n" % self.BODY))
        self.assertTrue(self.denied("cat >> notes.md <<'EOF'\nplain text\nEOF\n%s python3 %s status" % (self.HELPER, OBSERVER)))
        self.assertTrue(self.denied("echo before\ncat >> notes.md <<'EOF'\n%s\nEOF" % self.BODY))   # a line of another shape

    def test_two_heredocs_one_for_cat_and_one_for_a_shell_are_denied(self):
        command = "cat >> notes.md <<'EOF'\n%s\nEOF\nbash <<'EOF'\n%s\nEOF" % (self.BODY, self.BODY)
        self.assertTrue(self.denied(command))

    def test_the_decision_of_the_hook_follows_the_same_rule(self):
        self.assertIsNone(pretool_guard.decide(json.dumps(call(self.note("cat >> notes.md <<'EOF'"), agent=None))))
        out = pretool_guard.decide(json.dumps(call(self.note("bash <<'EOF'"), agent=None)))
        self.assertEqual(pretool_guard.TERMINAL_DENIAL, out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_dropping_a_body_changes_nothing_else_of_the_command(self):
        command = "cat >> notes.md <<'EOF'\nthe body\nEOF"
        self.assertEqual("cat >> notes.md <<'EOF'\nEOF", pretool_guard.without_text_heredocs(command))
        for same in ("echo no heredoc", "bash <<'EOF'\nthe code\nEOF", "cat <<< here string", "", "cat > f <<EOF\n$x\nEOF"):
            self.assertEqual(same, pretool_guard.without_text_heredocs(same))
        two = "cat >> a.md <<'A'\nx\nA\ntee b.md <<'B'\ny\nB\n"
        self.assertEqual(two, pretool_guard.without_text_heredocs(two))   # two heredocs: read whole

    def test_each_shape_is_a_whole_line_and_nothing_else(self):
        for line in ("cat > a.md <<'EOF'", "cat <<'EOF' >> a.md", "tee a.md b.md <<'EOF'", "git commit -F - <<'EOF'",
                     "gh issue comment 5 --body-file - <<'EOF'"):
            self.assertIsNotNone(pretool_guard.sink_opening(line), line)
        for line in ("cat -n a.md <<'EOF'", "cat > -a <<'EOF'", "cat > a.md  <<'EOF'", "cat > a.md <<'EOF' ", "tee <<'EOF'",
                     "git commit -m x -F - <<'EOF'", "git log -F - <<'EOF'", "gh issue comment 5 --body-file x <<'EOF'",
                     "cat > a.md <<-'EOF'", "cat > a.md <<< x", "cat > ../../a b <<'EOF'", "cat > run.sh <<'EOF'",
                     "gh myalias --body-file - <<'EOF'"):
            self.assertIsNone(pretool_guard.sink_opening(line), line)


if __name__ == "__main__":
    unittest.main()

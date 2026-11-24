"""The plugin's names: its own name, the configuration file, the workflow, the variable prefix and the file names of
its local state.

Every other module takes a name from here, and reads the plugin's environment variables only through env().

The comment markers `<!-- ghim:event ... -->` and `<!-- ghim:resume:start -->`
are not names in this sense: they are the record format, defined in
record_format.py and recortex.py, and they never change, so that comments
posted by every version are one format.

Standard library only, and nothing but `os`, so that storage.py can use it.
"""

import os

NAME = "recortex"
CONFIG_NAME = ".recortex.json"
ENV_PREFIX = "RECORTEX_"
WORKFLOW = "recortex-triage.yml"
# The user's writing mode per repository, the paper kept outside the project
# folder (both in the private folder of the configuration top, like the field approvals), and the branch that holds the
# versions of the plugin's own manuscript in the record's repository.
WRITING_MODES = "writing-modes.json"
OWN_PAPER = "own-paper.json"
# 1.4.0: the answers of this machine to the setup questions an update adds (private folder of the configuration top).
SETUP_ANSWERS = "setup-answers.json"
# 1.4.0: when `setup.py questions` last ran for a repository, in its folder of the hook state (the prompt hook's reminder reads it).
SETUP_RUN = "setup-questions-run.json"
MANUSCRIPT_BRANCH = "recortex-manuscript"
# 1.7.0: whether the task reminder (the line about work that rests on a replaced decision) is added to prompts, per repository on
# this machine (private folder of the configuration top, like the writing modes).
TASK_REMINDERS = "task-reminders.json"
# The plugin's environment variables, without the prefix. The three before the last are read only by the tests.
# LINK_PAUSE is the seconds create_issues.py waits after each sub-issue link it writes (default 1, at most 10);
# the tests set it to 0. SETUP_QUESTIONS set to 0, off, no or false switches the hook's line about the setup questions off.
# TEST_SERVE is read only under TESTING=1: the hook starts no live dashboard server in a test run unless a test that is about
# the server sets it to 1 (dashboard_server.may_start).
VARIABLES = ("GH", "CONFIG", "TRUSTED_FILE", "TRUSTED_REPOS", "HOOK_TIMEOUT", "DISCOVERY", "SYNC_PAGE",
             "DATA_HOME", "TESTING", "TEST_KEEP", "LINK_PAUSE", "NOTICE", "SETUP_QUESTIONS", "DASHBOARD", "TEST_SERVE")


def env(name, default=None):
    """RECORTEX_<name> when set to a non-empty value, else default.

    A value of spaces is not empty, as in the shell's ${VAR:-default}, which run_hook.sh uses for the same rule.
    """
    value = os.environ.get(ENV_PREFIX + name)
    return value if value else default

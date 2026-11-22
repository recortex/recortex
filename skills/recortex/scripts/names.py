"""The plugin's names: the current ones, and the ones it had before it was renamed to recortex in version 0.5.0.

Every other module takes a name that changed from here, and reads the plugin's
environment variables only through env(). A name from before the rename is
read where the current one is absent, and nothing is moved or rewritten.

The comment markers `<!-- ghim:event ... -->` and `<!-- ghim:resume:start -->`
are not names in this sense: they are the record format, defined in
record_format.py and recortex.py, and they are not renamed, so that comments
posted before and after the rename are one format.

Standard library only, and nothing but `os`, so that storage.py can use it.
"""

import os

NAME = "recortex"
OLD_NAME = "gh-issues-manager"
CONFIG_NAME = ".recortex.json"
OLD_CONFIG_NAME = ".gh-issues-manager.json"
ENV_PREFIX = "RECORTEX_"
OLD_ENV_PREFIX = "GHIM_"
WORKFLOW = "recortex-triage.yml"
OLD_WORKFLOW = "ghim-triage.yml"
# Names of 1.3.0 that have no earlier name: the user's writing mode per repository, the paper kept outside the project
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
VARIABLES = ("GH", "CONFIG", "TRUSTED_FILE", "TRUSTED_REPOS", "HOOK_TIMEOUT", "DISCOVERY", "SYNC_PAGE",
             "DATA_HOME", "TESTING", "TEST_KEEP", "LINK_PAUSE", "NOTICE", "SETUP_QUESTIONS")


def env(name, default=None):
    """RECORTEX_<name> when set to a non-empty value, else GHIM_<name> when non-empty, else default.

    A value of spaces is not empty, as in the shell's ${VAR:-default}, which run_hook.sh uses for the same rule.
    """
    for prefix in (ENV_PREFIX, OLD_ENV_PREFIX):
        value = os.environ.get(prefix + name)
        if value:
            return value
    return default


def env_source(name):
    """For --doctor: "new", "old", "both" or None, by which names of the variable are set to a non-empty value.
    It says which names are set, never a value."""
    new = bool(os.environ.get(ENV_PREFIX + name))
    old = bool(os.environ.get(OLD_ENV_PREFIX + name))
    if new and old:
        return "both"
    return "new" if new else "old" if old else None

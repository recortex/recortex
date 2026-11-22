# Install and upgrade

How to install the plugin, which repositories it may print from, how to use it as a plain skill and how to move from the plugin's earlier name.

## Requirements

- Claude Code
- The GitHub CLI `gh`, authenticated (`gh auth status`)
- Python 3.8 or newer and bash; no other packages
- Only for the optional board: the `project` scope on your `gh` login (`gh auth refresh -s project`), which the plugin never adds on its own. See [How the record works](how-it-works.md#sub-issues-and-the-board).

Setup checks `gh` and Python before it asks anything (`setup.py preflight`): that `gh` runs, that it is signed in to github.com and names its account (`gh auth status`, then `gh api user`), and that Python is 3.8 or newer. When a check fails it prints one line with what to do, such as installing `gh` from https://cli.github.com or running `gh auth login` (in Claude Code, `! gh auth login`), and stops before the first question. It installs nothing and signs in to nothing on its own, and prints nothing of what `gh` says except your login. In a project that is set up, `hooks.py --doctor` also says whether `gh` is found and which account it is signed in as.

## Install

### As a plugin (includes the session hook)

```bash
claude plugin marketplace add recortex/recortex
claude plugin install recortex@recortex
```

The skill is then available as `/recortex:recortex`, and Claude loads it on its own when a task matches. The plugin's hooks are registered for every project but print nothing until you trust a repository (next section). The same trust list governs `resume_block.py show`.

### Trust the repositories whose record the hooks may print

The hook puts text from a GitHub issue into every session, so it is off until you opt in, per repository, in a file that belongs to you and not to any project. Setup asks whether to add the repository for you; to add one by hand:

```bash
mkdir -p ~/.config/recortex
echo "your-name/your-project" >> ~/.config/recortex/trusted-repos
```

Each line is `owner/name`, optionally followed by GitHub logins whose resume comments you accept besides the repository owner's:

```text
# owner/name   extra allowed logins
your-name/your-project
your-lab/shared-project   your-name   a-colleague
```

A name is read only in GitHub's form, in ASCII: an owner of 1 to 39 letters, digits, `-` or `_` that starts with a letter or digit, and a name of 1 to 100 letters, digits, `.`, `-` or `_` other than `.` and `..`; a line in another form is ignored.

For a repository owned by an organization, list the people who post the resume comments, because the owner is then not a person. `RECORTEX_TRUSTED_REPOS` (comma-separated `owner/name`) does the same for one shell. See [Safety and security](security.md) for what this protects against.

### As a plain skill

```bash
git clone https://github.com/recortex/recortex
cp -r recortex/skills/recortex ~/.claude/skills/
```

Use `<project>/.claude/skills/` in place of `~/.claude/skills/` to install it for one project only. The skill is then available as `/recortex`. After setting up a trusted project, say "open my dashboard" to get its local address. The hook is optional in this mode; to add it, see [the hooks](reference.md#the-hooks-and-the-claudemd-snippet).

## A home folder on shared storage

If your home folder is on a network mount that many hosts see, and a session may start on any of them, the observer's approval follows the key file from host to host (see [the observer](../skills/recortex/references/observer.md)). Where the folders above Claude Code's transcripts belong to another account, run `observer.py trust-ancestor --failing` in your own terminal and then the same with `--yes`. Run sessions for one project on one host at a time where `hooks.py --doctor` says the mount keeps its file locks on the host. For the interactive dashboard on a machine with no browser, forward the port with `ssh -L`, with the same port number on both ends and `127.0.0.1` in the address ([the dashboard](../skills/recortex/references/mind.md)).

## Body updates from 1.1.0

Scripts that call `post_verified.py body` must now pass `--expected-body-file` with the unchanged body saved before drafting. Keep the draft in a separate file. A changed live body stops the replacement; merge the latest text before retrying. Comments, existing records and the index format are unchanged. See [the posting example](../skills/recortex/references/posting-and-verification.md#4-post).

## Upgrading from an earlier version

From 1.4.0 the repository is `recortex/recortex` and its marketplace is `recortex`. Until 1.3.0 the plugin came from `mr-eyes/recortex` as `recortex@mr-eyes-skills`, and before 0.5.0 it was named gh-issues-manager. Move an installed copy of any earlier version with these four commands, then start a new session:

```bash
claude plugin marketplace add recortex/recortex
claude plugin install recortex@recortex
claude plugin uninstall recortex@mr-eyes-skills
claude plugin marketplace remove mr-eyes-skills
```

If `claude plugin list` shows `gh-issues-manager@mr-eyes-skills`, uninstall that name in the third command. Add `--scope project` or `--scope local` if that is how you installed it. Until the old copy is removed, every session gets two briefs; the same holds for any other copy of the plugin, such as a folder install.

## Upgrading from gh-issues-manager

The plugin was named gh-issues-manager before version 0.5.0. After the move above, nothing else is needed. The configuration file `.gh-issues-manager.json`, the folders `~/.cache/gh-issues-manager` and `~/.local/state/gh-issues-manager`, the trusted list `~/.config/gh-issues-manager/trusted-repos`, the `GHIM_*` variables, an installed `ghim-triage.yml` workflow and everything on GitHub keep working as they are. Edit by hand what names the plugin's parts: the agent prefix (`recortex:record-auditor`) and the skill name in permission rules, your own agents and a project's CLAUDE.md snippet, and the folder and hook paths of a folder install. Rename the configuration file to `.recortex.json` only once everyone on the project has 0.5.0 or later. Optionally, with no session running and no copy of 0.4.x left on the machine, delete `~/.cache/gh-issues-manager` and move `~/.local/state/gh-issues-manager` to `~/.local/state/recortex`. The earlier names are read for at least twelve months, and `hooks.py --doctor` lists the ones in use. Details: [references/upgrading.md](../skills/recortex/references/upgrading.md).

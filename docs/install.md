# Install and upgrade

How to install the plugin, which repositories it may print from, how to use it as a plain skill and how to move to a newer version.

## Requirements

- Claude Code
- The GitHub CLI `gh`, authenticated (`gh auth status`)
- Python 3.8 or newer and bash; no other packages
- Only for the optional board: the `project` scope on your `gh` login (`gh auth refresh -s project`), which the plugin never adds on its own. See [How the record works](how-it-works.md#sub-issues-and-the-board).

Setup checks `gh` and Python before it asks anything (`setup.py preflight`): that `gh` runs, that it is signed in to github.com and names its account (`gh auth status`, then `gh api user`), and that Python is 3.8 or newer. When a check fails it prints one line with what to do, such as installing `gh` from https://cli.github.com or running `gh auth login`, and stops before the first question. It installs nothing and signs in to nothing on its own, and prints nothing of what `gh` says except your login. In a project that is set up, `hooks.py --doctor` also says whether `gh` is found and which account it is signed in as.

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

If your home folder is on a network mount that many hosts see, and a session may start on any of them, the observer's approval follows the key file from host to host (see [the observer](../skills/recortex/references/observer.md)). Where the folders above Claude Code's transcripts belong to another account, the session asks the question that `acts.py ask trust` prints and, on your yes, runs `observer.py trust-ancestor --failing --yes`. Run sessions for one project on one host at a time where `hooks.py --doctor` says the mount keeps its file locks on the host.

## Updating

To update, see [Upgrading](../skills/recortex/references/upgrading.md).

The GitHub CLI `gh` is needed only for a record in GitHub issues. A local record needs Python and nothing else.

# Upgrading from gh-issues-manager

The plugin was named gh-issues-manager (short `ghim`) before version 0.5.0. A project, a machine and a record set up under that name keep working with recortex as they are: nothing has to be edited, rebuilt or moved, and the brief shows as before. This page says what changed, what still reads the earlier names, and the optional steps to move to the new ones.

## The one step Claude Code needs

From 1.4.0 the repository is `recortex/recortex` and its marketplace is `recortex`; until 1.3.0 it was `mr-eyes/recortex`, installed as `recortex@mr-eyes-skills` (`gh-issues-manager@mr-eyes-skills` before 0.5.0). Move an installed copy of any earlier version with these four commands, one after the other:

```bash
claude plugin marketplace add recortex/recortex
claude plugin install recortex@recortex
claude plugin uninstall recortex@mr-eyes-skills
claude plugin marketplace remove mr-eyes-skills
```

Then start a new session. If `claude plugin list` shows `gh-issues-manager@mr-eyes-skills`, uninstall that name in the third command. Add `--scope project` or `--scope local` if that is how you installed it. Run `claude plugin list` afterwards: it shows `recortex@recortex`, version 1.4.0 or later. Do not run `claude plugin marketplace update mr-eyes-skills` instead: GitHub forwards the old address to the new repository, whose marketplace now has another name. The new marketplace still maps the old plugin name to the new one (`renames` in `marketplace.json`). The old plugin folders under `~/.claude/plugins/cache/mr-eyes-skills/` are left behind and can be deleted.

A copy of the plugin installed another way (a folder under `~/.claude/skills/` with hooks from `templates/settings-hook-snippet.json`, or a second plugin entry) stays as it is. Copy the new `skills/recortex` folder in, remove the old folder, and change the four hook commands to `$HOME/.claude/skills/recortex/scripts/`. Two copies enabled at once print two briefs, so disable or uninstall the other one.

## Version 1.7.0: shared home folders, changing hosts, and the questions asked again

For a home folder on a network mount and a session that starts on whatever host is free (a batch job, a container, a cloud workspace). An observer approval now follows the key file: an approval made on one host holds on every host that sees the same key file (the same inode at the same real path). An approval from an earlier version still holds on its own host; run `setup.py observer --level L --yes` once, in your own terminal or in a session on your answer to the level question (on a host where the approval does not hold, the command is a new approval there, so a session cannot run it unasked), and it holds on every host. Where inode numbers differ between clients (sshfs and other FUSE mounts, a container that mounts the home at another path), `observer.py enable ... --hosts any --yes` in your own terminal makes it hold on any host that can read the state folder, a copy included. `observer.py status` says why an approval does not hold (`approval_reason`) and what it is bound to (`binding`). One host's observer worker at a time works on a queue (a host lease). Where the mount keeps file locks on the host (Lustre `localflock`, NFS `local_lock`), run sessions for one project on one host at a time; `hooks.py --doctor` says so.

When a folder above Claude Code's transcripts belongs to another account or can be written by a shared group, `observer.py trust-ancestor --failing --yes` (your own terminal) trusts those folders exactly as they are, and a refusal says "your answer could not be read" with the code instead of asking again. The observer questions are not asked where notes cannot be taken; `setup.py questions` prints the command. The task reminder (the line about work that rests on a replaced decision) has its own question and setting and needs no observer. New setup questions: `task-reminder`, `map-files`, `map-upkeep` (a session may then run `observer.py map-mode` once, on your answer) and `status-line-combine`; each option has a description. The dashboard adds one line to a session's next prompt when you recorded something in it. A first Project Map is one comment (`seed`). `publish.py` puts the static page on a branch you name, after a warning and a typed confirmation, from your own terminal. `ssh -L` is documented for the interactive dashboard on a machine with no browser.

Back to 1.5.1's way of asking: what waits for you and the setup questions are asked again, each as one multiple-choice question, and `/recortex:status` tells In short, the open judgment, the tasks, the text and what waits, in that order; the dashboard opens "Resume work" with In short at the top again. Nothing is migrated. Going back to 1.6.0: it reads the same approvals and judges them by the host, as it always did, and ignores the new key; it does not know `seed`, so a first map made with it looks empty or partial there (update every collaborator before seeding), and the new questions, files and commands are simply unused.

## Version 1.6.0

In Claude Code, open the project dashboard with `/recortex:dashboard` or ask "open my dashboard". The existing `mind.py --open` command also works. Overview, Project Map, Work, and History are shared across projects. Start a hierarchy on an existing project with `/recortex:map`. The shared entries rebuild from the project hub. File selection and observer map maintenance each require a local choice. Projects with configured deliverables also have Outputs. Saved links to earlier views still open. To edit the map or record a completed task step from the browser, explicitly launch the local interactive dashboard with `dashboard_action.py --open`. Projects can opt into a task-time reminder of a relevant past decision. See [the dashboard](mind.md) and [the observer](observer.md).

Existing observer approvals keep their level and limits. Both new modes start off. If you enable task-time reminders or map maintenance and later return to 1.5.1, first turn off each mode you enabled in that project's terminal with `observer.py advisory off --yes` or `observer.py map-mode off --yes`. Version 1.5.1 can then read the same approval.

## Version 1.5.1: nothing you want recorded is lost silently

A moment that the observer kept off the record now waits for your answer. When the reviewer judges that a moment was kept off the record (you typed "don't post this", or a model misread you), the memory is no longer dropped: it is on the list of what waits, as a held memory with the options Post and Drop. When you release a hold in your own terminal (`observer.py release`), the moments it held are offered to the sort again, and the models are told which request you lifted; any other request not to post still holds. A message that holds a failure and the decision it led to now gives two memories, the decision linked to the failure.

The observer cleans up after itself. The folders in which it keeps a draft of the paper's text go once the draft is applied, dropped or stale, or after 14 days. A failed write of the log of its acts is counted and said once a day, in `hooks.py --doctor`, in `observer.py status` and in the first line of a session. The housekeeping step that links tasks under their topics says what it did, and goes on the next time when more than 60 links wait. The closed side panel of the mind page no longer shows a shadow at the edge of the window, and the pictures of the README describe the 1.5.0 observer.

Nothing changes in the record, the index or the approvals, so an older version reads everything as before. The only local files that change are the draft folders, which are removed.

## Version 1.5.0: an observer with several jobs

The observer is one always-on note-taker with several jobs. It keeps memories, as before, and finds more of them. It keeps a to-do list with checkmarks: a plan you approve (also in plan mode, which 1.4.0 could not see) becomes a task with numbered steps, a step the agent reports done is checked, and a loose "remind me to" becomes an item. It keeps the status summary and the copies (task lists, the hub's active list, the board) current, and it drafts a change of the paper's text in the background for you to accept; it never writes the text on its own. A cheap model (Haiku) sorts each stretch of the conversation, Sonnet drafts and reviews memories, and a stronger model (Opus) is used only for the paper's text and only when you allow it. What it may do on its own depends on the level you choose: Notes only, Assistant or Everything ([observer.md](observer.md)). Under a subscription the calls count against your usage limits: at most 80 sort, 70 middle and 8 strong calls a day for a project, about 36 on a typical day, which is about what 1.4.0 used.

The first session after the update asks the level question, in the batch of setup questions. A folder whose observer was on with 1.4.0 or earlier is paused until it is answered: no hook captures, no worker runs and no model call is made, and the brief's setup line counts the question; `hooks.py --doctor` and the status line say that the observer is paused until you choose a level. Your earlier choice of a model is not carried over as consent to the new jobs; the answer is a new approval for the models of that level. A folder that was in shadow mode stays in shadow mode. A project with a plan, a writing mode other than off and the level Assistant or Everything is also asked whether the stronger model may draft the paper's text; "No" keeps the keeper off. A folder with the observer off sees no change but the level question.

The record has a new kind of memory, `steps`, and two new marker keys, `done=` and `dropped=`, which check a step off or drop it; a task's body holds its steps as a task list under `## Plan` ([tasks.md](tasks.md)). An older version of the plugin reads a `steps` comment as an ordinary comment and ignores `done=`, so it shows the checkmarks as nothing and the task lists as plain text. Update every machine of a team before the first task is opened by the observer. The first sync after the update derives the index again from the stored comments (the index version is now 5), with no network and no change to the record or the project file.

New local files, all private (mode 0600 in folders of mode 0700) and never in the project: `usage.db` in the state folder (token counts of the plugin's own subagents, and an estimate of what the hooks add to a session; integers only), the folders in which the observer keeps a draft of the paper's text while it waits (under the cache folder), and the backups of task bodies before a render, a close or a reopen (beside the index). The observer's private store moves to a new format on first use. The plugin registers a fifth hook event, `SubagentStop`, which prints nothing; it only counts a plugin subagent's tokens. The mind page has two new views, "What is there to do?" and "What did recortex use?", and five commands, `/recortex:record`, `:ask`, `:todo`, `:status` and `:usage`, with a read-only agent, `@agent-recortex:ask`, give direct access. On a phone-width screen the views of the mind page other than the paper keep their side gutter.

## Version 1.4.0: the manuscript page, and setup questions after an update

A project with a plan of its paper gets a new view of the mind page, "Read the manuscript": the paper as a paper, with the changes since an earlier version, who wrote each paragraph and what the plan says that the text does not yet. Nothing changed in the record, the index or the project file, and an older version of the plugin ignores the view. Every other project gets the page it had.

The first session after an update asks only the setup questions the user has not answered yet, in one batch: the brief carries one line with a count, and the session runs `setup.py questions` before its first reply ends, once the user's request is done (at once when the first prompt is a greeting, an open question or about recortex), and asks them as one multiple-choice batch, not in prose; a session that has not asked by its second prompt is reminded, at most three times, by one fixed line of the prompt hook. A question is asked only when it applies to the project (the writing mode and the author's own paper for a project with a plan; the manuscript profile for a project with paper files and no such profile; the labels for a project with the manuscript profile) or to the machine (the status line, the observer). Which are open is read from the current state, so a project set up with any earlier version needs no migration, and a project with nothing open sees no change. The writing-mode question of 1.3.0 joins the batch: the brief no longer asks it on its own, unless the questions are switched off.

"No" and "yes" are remembered for good on that machine, "later" for seven days. The answers are the new local file `private/setup-answers.json` in the configuration folder (mode 0600, like the writing modes); nothing is added to the project file, and nothing in it is read as an answer. The reminder of the prompt hook keeps two numbers in the state folder, the start of each session in `prompt-state.json` and the time `setup.py questions` last ran in `setup-questions-run.json` beside it; an older version ignores both. A project-wide "yes" settles a question for everyone through the project file or GitHub; a project-wide "no" is remembered on each machine, so each collaborator is asked it once. Switch the line off with `RECORTEX_SETUP_QUESTIONS=0` or `"setup_questions": false` in `~/.config/recortex/settings.json`; `hooks.py --doctor` prints the open, answered and deferred ids. Details: [setup.md](setup.md), "Questions added after setup".

An older version ignores the new file, the new line and the new commands, and nothing in the record changes: no comment, body, label or hub row is written by the questions themselves, only by the commands the user agrees to. A user who chose "off" for the observer in an earlier wizard is asked once more, because earlier versions did not keep a "no".

The license changes with this version. Version 1.4.0 and later are under the Functional Source License, FSL-1.1-MIT: using, changing and sharing the plugin are allowed for any purpose except a competing use, which means making it available to others in a commercial product or service that substitutes for it, that substitutes for another product or service the licensor offers with it, or that offers the same or substantially similar functionality. Each release becomes MIT two years after it is published. Versions up to 1.3.0 stay under MIT. The full terms are in `LICENSE` at the root of the plugin.

## Version 1.3.0: the plan of the paper, its text and its versions

The resume block gains a first field, `**In short:**` (two short plain paragraphs, at most 1,200 characters, no issue numbers), and the linter asks for it in every new block. A block posted before has none and is still read; the mind page says so in one line until the next block. An older version reads a block with the field as a block with one more line, and posts blocks without it. The index keeps two more meta keys (`resume_posted`, `summary_state`); older versions ignore them.

The manuscript profile gains a plan: a `plan` label, a plan issue (a child of the hub) with one dated comment per planned part, and three kinds of comment, `plan`, `text` (a change of a topic's text) and `proposal` (a change put forward as a pull request). The record's format gains one link key, `against`, from a result to a planned part it speaks against. A project without a plan-capable profile is unchanged: its briefs are byte for byte those of 1.2.x.

Update every machine of the team before the first plan is adopted. An older version reads a `plan`, `text` or `proposal` comment as an ordinary comment, so it shows them in no thread and counts them as nothing, and it reads `against=` as no link; nothing breaks and the index stays readable, but the plan and the text changes are invisible to it. The index re-derives its tables on the first sync of 1.3.0 (one pass, no network call for it). Two index meta keys are new (`plan_counts`, and the derive version); older versions ignore them. An older version lists the plan issue as one that needs triage, since it does not know the label `plan`; leave it as it is.

Nothing is written to GitHub for the manuscript until the author chooses a writing mode on a machine (`setup.py writing set`): an upgraded project's first session asks once. The mode is per machine and per repository, kept in the user's private configuration folder; a project file cannot choose it. With review or autonomous, recortex also keeps the versions of the manuscript on the branch `recortex-manuscript` of the record's repository ([versions.md](versions.md)); nothing is built or run on GitHub; with off nothing is written to the repository.

New local files, all under the private folders: `private/writing-modes.json` and `private/own-paper.json` of the configuration folder, and `write/`, `texts/` and `versions/` beside the index. Settings: `"planning": false` in the user's `settings.json` stops the plan lines of the hooks.

## Version 1.2.0: the manuscript profile and the mind page

The `manuscript` profile gains two section labels, `ms:introduction` (`## Introduction`) and `ms:discussion` (`## Discussion`), and its order becomes Introduction, Background, Methods, Results, Discussion, Limitations, Supplement. An existing manuscript project gets the two labels when it runs `setup_labels.py` again; the descriptions of `ms:background` and `ms:limitations` change too, and `setup_labels.py` leaves a label whose description differs alone unless `--update` is given. Nothing moves text: a body that holds its introduction under `## Background` stays valid. The hub of a project set up with 1.1.1 needs no edit for planning or writing: `plan.py setup` makes the plan issue and links it under the hub as a sub-issue, `setup.py writing set` records a mode on the machine, and the page places each topic by its section labels, so a hub whose table of sections has no row for the two new sections, or a `none yet` where a topic stands, is read as it is (a test holds this with the hub the 1.1.1 setup wrote). Update every machine of the team before a body uses the two new headings: an older version's linter reports `## Introduction` or `## Discussion` as a heading the profile does not define, and its `compile.py` leaves the two sections out. The index and the record's format do not change, so versions 1.1.x and 1.2.0 can sync the same index in any order; an older version reads an idea that a decision took up as not tested yet.

## What keeps working with no action

| Thing | Current name | Earlier name, still read | When both are there |
|---|---|---|---|
| Configuration file at the project root | `.recortex.json` | `.gh-issues-manager.json` | The current file is read; the earlier one is ignored, and `hooks.py --doctor` says so |
| Index top folder | `$XDG_CACHE_HOME/recortex` | `$XDG_CACHE_HOME/gh-issues-manager`, used in place | The current folder; the earlier one is not touched |
| State and key top folder | `$XDG_STATE_HOME/recortex` | `$XDG_STATE_HOME/gh-issues-manager`, used in place | The current folder |
| Trusted list | `~/.config/recortex/trusted-repos` | `~/.config/gh-issues-manager/trusted-repos` | Both are read; a repository listed in either is trusted, with the logins of both |
| Settings (`"discovery": false`) | `~/.config/recortex/settings.json` | `~/.config/gh-issues-manager/settings.json` | The current file |
| Environment variables | `RECORTEX_GH`, `RECORTEX_CONFIG`, `RECORTEX_TRUSTED_FILE`, `RECORTEX_TRUSTED_REPOS`, `RECORTEX_HOOK_TIMEOUT`, `RECORTEX_DISCOVERY`, `RECORTEX_SYNC_PAGE` | the same with `GHIM_` | The `RECORTEX_` one when it is set and not empty |
| Triage workflow | `.github/workflows/recortex-triage.yml` | `.github/workflows/ghim-triage.yml`, left alone | `setup.py apply` writes no second labeler when the earlier one is there |
| Markers in comments | `<!-- ghim:event ... -->`, `<!-- ghim:resume:start -->`, `<!-- ghim:resume:end -->` | | Not renamed: they are the record format, and old and new comments are one format |

Everything on GitHub keeps working: labels, hub and topic bodies, event and resume comments. The plugin edits no posted text; a hub body that says "from the gh-issues-manager skill" can stay. `setup.py apply` run again in a project with `.gh-issues-manager.json` adds to that file and makes no second one.

Each storage folder is chosen by itself: the folder with the current name when anything is at that path, else the one with the earlier name when it exists, else a new folder with the current name. The earlier folders are used in place, never moved: an index carries a mark made over its path, so a moved index would be refused and built again, and an older copy of the plugin still running on the same machine shares the same index, lock and key. Every check on the storage applies to whichever folder is chosen; a symlink or a file at the current name is refused, and the earlier folder is then not used instead.

The session-start and subagent briefs carry one fixed sentence that the skill was named gh-issues-manager before 0.5.0, for projects whose CLAUDE.md still names the old skill. No brief says which earlier names this machine uses; `hooks.py --doctor` does.

## What to change by hand

- Names that point at the plugin's parts: the agents are now `recortex:record-drafter`, `recortex:record-reviewer` and `recortex:record-auditor`, and the skill is `/recortex:recortex` (plugin) or `/recortex` (folder install). Edit permission rules, your own agents and notes that use the old prefix; there is no alias for it. Permission rules that hold the plugin's path (under `~/.claude/plugins/cache/` or a folder install's folder) need the new path in the same way.
- A project's CLAUDE.md snippet that names the `gh-issues-manager` skill: replace the name. `setup.py apply` does not rewrite it and prints a note when it sees it.
- A folder install's folder and hook paths (see above).

## Optional renames

- The configuration file: rename `.gh-issues-manager.json` to `.recortex.json` once everyone on the project has recortex 0.5.0 or later. The file is committed, and a collaborator on 0.4.x cannot read the new name: their hooks would go silent and the setup line would appear. A record set up with 0.5.0 gets `.recortex.json` from the start, so collaborators on such a project need 0.5.0 or later too.
- The storage folders, with no session running and only once no copy of 0.4.x is used on this machine (a folder install or another Claude Code profile would make the earlier folders again with another key, and the two versions would then refuse each other's index): delete `~/.cache/gh-issues-manager` (the next session builds a new index under `~/.cache/recortex` in the background; moving it instead gains nothing, because an index is refused at a path other than the one it was made at), and move `~/.local/state/gh-issues-manager` to `~/.local/state/recortex` to keep the key and the hook state. A key in the earlier state folder also works with an index in the new cache folder.
- The trusted list and settings: move them to `~/.config/recortex/`. Until then both lists are read, so a repository is no longer trusted only once it is gone from both.
- Environment variables: rename `GHIM_*` to `RECORTEX_*`.
- An installed `.github/workflows/ghim-triage.yml` needs nothing; it labels new issues as before.

## How long the earlier names are read

Through every 0.x release, and at least twelve months from 0.5.0. Reading an earlier name is removed only in a major version, after one minor version in which `hooks.py --doctor` reports it as a problem with the fix. The storage folders used in place and the `ghim:` markers have no end date. Until then `hooks.py --doctor` lists, as notes, every earlier name in use: the configuration file, the storage folders, the trusted list that names the repository and each `GHIM_` variable that is set (by name, never its value).

The upgrade was checked on Claude Code 2.1.325. A client too old to know `renames` is likely to keep the installed 0.4.x under the old name and report that the marketplace no longer lists it; this is not documented by Claude Code.

## Teams on mixed versions from 0.6.0

From 0.6.0 a memory starts with a line in plain words, may link to earlier memories in its marker, may list Evidence and may end with folded explanations, and ideas and questions are kinds of memory ([issue-model.md](issue-model.md), "Events"). A teammate on 0.4.x or 0.5.0, on the same record or on the same machine, loses nothing:

| What 0.6.0 writes | What 0.4.x and 0.5.0 make of it |
|---|---|
| Links in the marker (`because=`, `replaces=`, `instead=`, `checks=`) | Kept in the marker and ignored; the comment keeps its type and time, and their linter is silent |
| The plain line and Evidence | Two more fields; their linter checks only the fields it requires, in order |
| Folds | Text after the last field; only a final-record body may not hold a `<details>` block |
| `#N` and comment addresses in fields | `#N` is checked as before; a comment address is a URL like any other |
| An idea or a question | A comment of type `other` at its marker time, never dropped; they cannot write or lint one. On an index shared with 0.6.0 an older version shows the type 0.6.0 wrote (`idea`, `question`) until its own full derive, and nothing breaks on it |
| Four more tables and five triggers in a shared index | Left alone by their sync; the triggers record that it wrote, and the next sync of 0.6.0 derives everything again |
| Hook payloads | The same: no payload reads the text of a comment other than the hub's checked resume block |

What an older version cannot do: write an idea or a question, have links, Evidence or folds checked, or run `index.py query recall`. A memory written by an older version has no plain line, and `query recall` shows its title in its place. When an older version synced last, `query recall` of 0.6.0 says "the index was last written by an older version of the plugin, or never by this one; run index.py sync", and one `index.py sync` of 0.6.0 brings it back.

## From 0.7.0

0.7.0 adds the mind page: `mind.py` builds one private local file that tells the project's memory in sentences, beside the index ([mind.md](mind.md)). Nothing in the index changes: no table, column, trigger or meta key is added, and 0.4.x, 0.5.0 and 0.6.0 sync, rebuild and print their briefs on the same index as before and ignore the page. The hook payloads are the same.

What changes for a memory edited on GitHub after it was posted (GitHub's `updated_at` differs from its `created_at`): when the repository is in your trusted list, `index.py query recall` of 0.7.0 no longer lists it, its links no longer count, its folds no longer explain a word or a thread, and the linter refuses a new link to it. `recall` closes with one line that says how many memories it left out for that reason. 0.6.0 on the same index keeps listing it.

Run `python3 scripts/index.py sync --rebuild` once after the update: it reads the edit history of every topic again, and an edit that another account made in the minute an issue was opened is now seen.

## From 0.8.0

0.8.0 adds field guidance: a project's `.recortex.json` can carry a `fields` entry, the field sheet the user approved, and every subagent gets its short part in its start brief on a machine where the user approved that exact text ([fields.md](fields.md)). Nothing changes in the index or in the record on GitHub. A project without a sheet gets the payloads of 0.7.0, and once the line that offers to set the field.

Older versions ignore the entry: their hooks, sync, linter and `--doctor` do not read it, and their `setup.py apply` keeps it as it is. Their `apply --dry-run` prints the whole configuration, the sheet included; 0.8.0 prints a sheet it does not write as one line. The identifier patterns the wizard adds go into `restricted_patterns` in the form every version reads. The approvals are kept per machine, in `private/field-approvals.json` of the plugin's folder in your configuration folder, which no older version reads: each machine, and each collaborator, approves a sheet once with `setup.py fields show` and `setup.py fields approve`.

## From 1.0.0

1.0.0 brings the parts below, and a shorter README with pages in `docs/`. The built-in example is now `octo-org/octo-repo`, with names that GitHub itself holds.

### Sub-issues and the board

1.0.0 adds two views of the record on github.com ([board.md](board.md)): each topic becomes a sub-issue of the hub and each `active` task a sub-issue of its destination topic, and a project can opt into a private GitHub Projects board of its work. Nothing in the record changes. `create_issues.py` links what it creates; for an existing record run `sub_issues.py plan` and then `sub_issues.py apply`. A project that does not answer yes to the board question never makes a board call, never sees a board line and never needs the `project` scope.

The index gains three tables (`parents`, `pull_merge`, `board_cards`) and three meta keys, as `CREATE TABLE IF NOT EXISTS` and plain rows. Versions 0.4.0 to 0.8.0 on the same index ignore them, and ignore the `board` entry of `.recortex.json`. The first sync of 1.0.0 on an older index lists the issues and reads their body edits again once.

### Edited memories and whole edit histories

A memory that your allowed accounts edited, and nobody else, is told again, with the quiet note "Changed on GitHub after it was posted." (see [mind.md](mind.md), "Edited memories"). The sync reads, from GitHub, the edit history of each memory whose comment looks edited, and of those only; it also reads every page of the edit history of a topic's text, no longer the newest 100 only. The first sync after the update reads the histories of the memories that already look edited (one more call for 40 of them, and one read for each that this index has no node id for). Nothing has to be rebuilt for that.

A topic with more than 100 edits was read up to its newest 100 before. Run `python3 scripts/index.py sync --rebuild` once to read the rest: it reads every topic's history again.

The index gains two tables, `comment_edit_reads` and `comment_edits`; no column, trigger or format changes. Versions 0.4.0 to 0.8.0 on the same index ignore them (0.7.0 and 0.8.0 keep leaving every edited memory out, and 0.6.0 lists it as it always did), and nothing they write makes the new version tell a memory it should not: a comment they rewrite has a new `updated_at`, which the stored history does not match.

## From 1.0.1

1.0.1 checks before setup asks its first question that `gh` runs, is signed in to github.com and names its account, and that Python is 3.8 or newer: `setup.py preflight`, which the session runs first, and which `setup.py apply --yes` runs again before it writes anything. When a check fails, one line says what to run (install `gh` from https://cli.github.com, or `gh auth login`) and setup stops; it installs nothing and signs in to nothing. A dry run makes no such check. On a Python older than 3.8 every `setup.py` command prints that one line and stops. The quick start in the README no longer adds the repository to the trusted list by hand, because setup asks whether to.

`hooks.py --doctor` gains one line on `gh`: found or not, and signed in to github.com as which account, the login printed only when it has GitHub's form. For that it asks GitHub who is signed in (`gh auth status`, `gh api user`), each with a limit of 10 seconds; it is a note, never a problem of the brief. No hook event makes a new call, the four payloads are the same, and nothing changes in the index or in the record on GitHub.

## From 1.1.0

1.1.0 adds the observer, experimental and off by default (see [observer.md](observer.md)). Nothing changes until you run `observer.py enable --model MODEL --yes` in a project folder: its settings and approval stay in this machine's private state for that folder, and the project's `.recortex.json` can only switch it off (`"observer": false`). The plugin registers five more hook events for it; without an approval for the folder each costs a shell and a grep, about 3 ms, and starts no Python. The four brief payloads are unchanged. When it is on, the main session keeps posting its own memories; each verified post is registered in the observer's queue. A memory comment it posts carries `eventkey=` in its marker, which earlier versions read and ignore.

`post_verified.py body` now requires `--expected-body-file <base>` for a real replacement. Save the live body before drafting and edit a separate copy; the script refuses a live body that differs from the saved base and checks again before writing. Update hand-written commands and wrappers that replace bodies. A dry run can still preview without a base, and a draft already stored is a no-op. A retry after a label failure can finish the labels when the body still matches the draft. See [posting and verification](posting-and-verification.md#4-post).

There is no record, index or configuration migration. Older clients still read the same record, but do not have the new update check. Coordinate one writer per issue: the check and write are separate GitHub calls, not an atomic lock. CI runs the full offline suite, including the [continuity demonstration](../../../examples/continuity/README.md); the separate live Claude protocol has no reported results until someone runs it.

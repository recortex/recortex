# The versions of the paper

For a project with a plan (the manuscript profile) whose author chose review or autonomous writing ([writing.md](writing.md)). The topic bodies stay the paper's text and the issues stay the record. A copy of the text is kept as files on one branch of the record's repository, `recortex-manuscript`, one commit per change, so that every version, the difference between any two versions and the exact paper back are one command away. Nothing is built or run on GitHub: the files are a copy, and the paper's own export is `mind.py --paper` (below). Nothing is cloned, the project's working tree is never touched, and the plugin never asks the author to run a git command: the mind page and the chat are the two places versions are seen and used.

The files are a copy, as the board is. Nothing is read from them into the record except through the two checked paths below: a merged proposal and a restore.

## What is kept, and when

Versions are kept exactly when this machine's writing mode is review or autonomous. `setup.py writing set` with either mode, and the first write on a machine that has none yet, call `versions.ensure()`: it checks that the sign-in may write files, makes the branch with a first commit of the current text, and says so in one line. With the mode off or not chosen nothing is written to the branch; the commands that need a version say "versions are kept once you choose review or autonomous writing."

The branch has no parent in the project's history, so it holds none of the project's code and none of its workflows. What it holds, in Manubot's layout:

| Path on the branch | Holds |
|---|---|
| `topics/<issue>.md` | the topic's body, byte for byte: the source of a restore and of every hash check |
| `content/metadata.yaml` | title (the hub's title when it may be told, else the repository's name), authors and keywords (`versions.py meta --authors "A; B" --keywords "x, y" --yes`), every value a quoted string, so no record text is read as YAML |
| `content/00.front-matter.md` | the plugin's own short front matter template (`templates/manubot-front-matter.md`) |
| `content/NN.<section>.md` | one file per section that has text, in the order of the deliverable, numbered from 10 in steps of 10: the section's heading, then each topic's text for it under a heading of the topic's name, every heading one level lower |
| `content/90.back-matter.md` | the fixed "References" heading and Manubot's `<div id="refs"></div>` |
| `content/manual-references.json` | `[]` |
| `content/images.tsv` | per figure: its id, the repository, the commit or tag and the path of its image, from the figure block; the images are fetched by the workflow |
| `VERSION.json` | repository, hub, and per topic the body's SHA-256 and the id of its newest text change at that commit |

A figure block becomes `![Title. Caption](images/<id>.<ext>){#fig:<id>}`, a table block its table followed by `Table: Title. Caption {#tbl:<id>}`; formulas, citations and `@fig:`, `@tbl:` and `@eq:` references pass through unchanged; "Sources" stays in `topics/`. `paper_layout.py` does this, one way only: nothing is ever read back from `content/`. Manubot reads `content/` as Jinja2 templates, so the layout writes each `{` of the text that would open one (`{{`, `{%` or `{##`) as `{{ '{' }}`: nothing in the record is read as a template, and a formula or a topic name with double braces reaches the build as it was written.

Every write goes through `recortex.gh()` to the git data endpoints: read the branch's head, post one tree on top of it, post one commit, move the branch forward without force. A head that moved meanwhile is read again and the commit is made once more. The commit message is fixed wording plus checked values ("Text: Results of topic 5"); no record text is in it. A body replacement through `post_verified.py` with `--text-memory FILE` makes the commit for a body edit of the session's ordinary work and posts the text change with the commit in its Version field (the file holds `**Version:** pending`); without it the edit is left for `versions.py sync`, which records it as an edit by the account that made it.

## The commands

| Command | Does | Writes to GitHub |
|---|---|---|
| `versions.py status` | per topic: in step, the text changed since its last version, or the file changed without the text; the names | no |
| `versions.py sync` | for each text that changed outside the plugin: one commit and one text change that says who edited it and when ("Edited by (login) on GitHub, (stamp).", Why "Not recorded: the text was edited by hand."); a file changed on the branch alone is never taken | yes |
| `versions.py log [--issue N] [--json]` | the versions, newest first: day, how it came about, the plain line, the names on it | no |
| `versions.py diff A B [--issue N]` | the difference between two versions (a commit, `HEAD` or a name), made from the two files, then the text changes between them | no |
| `versions.py name NAME --words-file F` | names the paper as it stands (below) | yes |
| `versions.py restore --issue N --to A [--section H] --out DIR` | prepares a restore | no |
| `versions.py put-back --issue N --yes` | puts a file that was changed on the branch alone back to the text, as a new commit | yes |
| `versions.py fetch [--issue N]` | brings the vouched versions' files into the private cache for the mind page (`mind.py --sync` and `--versions` call it) | no |
| `versions.py meta --authors A --keywords K --yes` | the authors and keywords of the paper's metadata | yes |
| `versions.py doctor` | the mode, the permission, the branch, the last refusal with its reason | no |
| `compile.py --version NAME [--layout manubot] --out DIR` | the deliverable of a named version; `--layout manubot` also writes `content/` | no |

## Names, and the exact paper back

Asked for in the chat ("call this version submitted"). The session confirms the name with one multiple-choice prompt and runs `versions.py name submitted --words-file F`: it syncs first and refuses when a text and its file differ or the name is taken (a name is 1 to 40 characters of `a-z`, `0-9`, `.` and `-`, starting with a letter or digit); posts a decision on the plan issue (the hub when there is none) with the author's exact words, whose Effect field starts with the fixed line `Version name: (name); commit (40 hex); manifest sha256 (64 hex)`; then makes the tag `paper-<name>` at that commit. A tag is never moved or deleted by the plugin.

`compile.py --version NAME --out DIR` builds the deliverable of that version and reads no body from the index. It stops with one fixed line, and writes nothing, unless a told decision of an allowed account carries the name, the tag points at the commit the decision names, `VERSION.json` at that commit has the SHA-256 the decision names, and every topic file at that commit has the hash `VERSION.json` holds. The plan files are left out and the timeline stops at the decision.

## Restoring a part

Asked for in the chat ("go back to last Tuesday's Results"). The session finds the version with `versions.py log --issue N --json`, shows its day and plain line in a multiple-choice prompt ("Restore this one", "Show me another", "Cancel"), and runs `versions.py restore --issue N --to <commit or name> [--section "Results"] --out DIR`. A restore is offered only for a version the record vouches for: the SHA-256 of `topics/N.md` at that commit must be the "after" value of a told text change of that issue. Otherwise: "that file is not a version the record holds, so it is not restored; copy it by hand if it is yours."

The folder is the one `write.py start` makes; `write.py check` (one more check, number 9, holds the draft to exactly the vouched text of the sections being restored), then `write.py apply --words-file F`. A restore is always asked in the chat, in every mode, and is written as a text change in the form "Restored by (name or role) in the chat, (stamp):" and a quote. History only grows: restoring is a new version that equals an old one.

## A file edited directly

`versions.py status` finds a topic file whose hash is not its body's while the body is as last committed. Nothing is taken from it. The session asks the author: "The file of (part) was changed on the branch and the text was not. Take the change into the text, or put the file back?" Taking it is the author's own edit of the topic (or a chat-approved write) that `versions.py sync` then records; putting it back is `versions.py put-back --issue N --yes`. Until the author answers, proposals for that topic wait.

## Approval on GitHub

In review mode the chat is the primary path and the one the session uses unless the author asks for the other. When they would rather read the change on GitHub, `write.py propose --dir DIR` puts the checked draft's topic file on a branch `recortex-proposal-<issue>-<8 hex>` cut from the head of the manuscript branch, opens a pull request into it, and posts a `proposal` comment on the topic with the pull request's number, the SHA-256 of the base body and the SHA-256 of the proposed body. The author reads the difference and merges, or closes.

`write.py approvals [--apply]` lists the proposals and, with `--apply`, writes one only when every row holds, each read live from GitHub at that moment and none from the index:

| Rule | Check |
|---|---|
| The proposal is ours | its comment's author is an allowed account, and it is the newest proposal on that topic |
| Nobody changed it | the comment was never edited (its edit history is empty); an edited proposal is void for good |
| An allowed account approved | the pull request is merged into the manuscript branch from a proposal branch, and `merged_by` is an allowed login |
| What was approved is what we proposed | the topic's file at the merge commit has the SHA-256 of the proposal's Text |
| The text did not move meanwhile | the live body has the SHA-256 of the proposal's Base |
| It is still fit to post | `write.py check` passes again on that text against the live body, and what it rests on still stands |
| This machine may | the mode here is review or autonomous, and the repository is trusted |

Otherwise: a proposal closed without merging is dropped and a checkpoint on the topic says so; one merged by an account that is not allowed, or made by one, writes nothing and is counted; one merged with other text than proposed is never written on its own (the session shows the difference and asks); a proposal whose base moved is not applied and its open pull request is closed with a fixed comment, and the session drafts again from the live body. A merged proposal that was applied already, which is when the live body is its text or a text change made after the proposal records the body as going from its Base to its Text, is done: `approvals` says nothing of it and closes nothing. A proposal can be applied only on the machine that made it. The write is the same as a chat approval, with the Approved form `By (login) on GitHub, by merging pull request (number).`; its version commit is made after the merge so that the section files match.

## When GitHub refuses

The text is written first and stays written; the copy can lag and never blocks the text. Each refusal is one fixed line, a reason kept for `versions.py doctor` and `hooks.py --doctor`, and "Not kept: (reason)" in the text change's Version field:

| Case | The fixed line |
|---|---|
| The sign-in may not write contents (a 403 or 404 on the write) | `versions: this account may not write files to (repository), so this version was not kept as a file. The text itself is saved. Run versions.py doctor.` |
| A rule protects the branch | `versions: a branch rule of (repository) refused the write to recortex-manuscript, so this version was not kept as a file. The text itself is saved. Run versions.py doctor.` |
| The branch moved twice while writing | `versions: the branch changed while writing; run versions.py sync.` |

`versions.py sync` later makes the missing commit. While GitHub refuses, the history of a part is its text changes without differences, and `name`, `restore`, `diff` and the GitHub approval path stop with the refusal's line. The hooks never contact the branch.

## The files for Manubot

The Manubot-format files stay on the branch (`content/`, `metadata.yaml`, citations in Manubot's forms), so that anyone who needs a journal's format can run Manubot on them themselves. The plugin runs none of it: there is no workflow on the branch, no build, and no Pages site. Manubot reads `content/` as Jinja2 templates; the layout writes each `{` of the text that would open one as `{{ '{' }}` (above).

## On the mind page

Each part of the text carries a History fold: every version, newest first, with its day, how it came about, the reason, the names on it, and "What changed", the difference from the version before drawn on the page (unchanged paragraphs collapse to a count, a removed or added paragraph is marked, inside a changed paragraph removed words are struck through and added words underlined). Above the parts, "Versions of the paper" lists the named versions with the author's words, and what changed since the name before. The page links to no commit, branch or tag and changes nothing: to go back to a version the author asks in the chat.

In a project with a plan, "Read the manuscript" ([mind.md](mind.md), "The manuscript") numbers the versions 1 to N, one for each told text change with a kept commit, and compares the current text with the version the reader picks (the previous one at first): the changed words of a paragraph, the paragraphs added or gone, and in the margin who wrote each paragraph. A name is only a label beside a number. It reads the same cache, and a part whose file is not kept says so and shows no marks until `mind.py --versions` brings it.

The page reads what `versions.py fetch` kept in the private cache (`mind.py --sync` and `mind.py --versions` call it) and contacts nothing at view time. Only the file `topics/<issue>.md` at a commit that a told text change of that issue names is asked for, and a file is used only when its SHA-256 is that change's "after" value; otherwise the entry stays and says "The text of this version could not be checked, so it is not shown." Budgets: 300 KB per file, 200 files and 20 MB per fetch inside 60 seconds, newest first; at most 30 differences per part and 3 MB of differences per page (each is drawn once, in the box of the manuscript view), older versions keep their entries; the marked variants of the manuscript view have a budget of 2 MB, and an earlier figure or table counts for the cells and words the page draws of it. With the mode off the fold holds only the days the text changed.

A local record has none of this: the script prints one fixed line, `recortex: the manuscript's versions branch is kept on GitHub. A local record keeps every version of a topic's text in the topic's folder.`, and does nothing.

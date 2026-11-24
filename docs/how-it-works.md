# How the record works

What lives in GitHub, what a session does with it, and what comes back after a compaction.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/how-it-works-dark.svg">
  <img src="../assets/how-it-works-light.svg" alt="An optional observer follows each session at the level you choose, keeps notes and a to-do list with checkmarks, and asks you before anything it should not do alone. GitHub issues are the record. The cortex works from it: a brief, the project dashboard, the finished text and a board.">
</picture>

## What it sets up

| Piece | What it is |
|---|---|
| Hub issue | One pinned issue that is the entry point. It states the deliverable-text rule and holds the project statement, a Mermaid map, the issues of each deliverable section in order, the active issues, the pull requests, the labels and the recording rules. It repeats no numbers from the bodies |
| Resume comments | The short "Resume block" (in short, current focus, active issues, last decisions, next actions, holds) is posted as a new comment on the hub at the end of every working stretch, and its first field, "In short", is the project's status in two short paragraphs, kept current by the session (or, with the observer on, by its worker) however long the session runs. The newest one is what a fresh session reads first; older ones stay as history |
| Topic issues | One issue per topic, labeled `final-record`. Its body is the deliverable's text for the sections it feeds (for a paper, for example `## Methods` and `## Results`), the current version only and ready to copy, followed by the sources of its numbers. Section labels such as `ms:methods` follow the body's headings |
| Event comments | Dated comments on the topic issue, written from templates: the memories of the project. Seven kinds for the work (result, decision, failure, checkpoint, correction, idea and question), and three more for the paper's plan, its text and an agreed list of steps. Each starts with one line in plain words and may name the earlier memories it follows from, replaces, was done instead of or tests. They are the history and never deliverable text |
| Active tasks | Issues labeled `active` for work in development and open questions; each names the topic its result goes to and carries no section label. A task can hold numbered steps, shown as a task list that is checked off as the work is reported done |
| Sub-issues | The hub's topics are sub-issues of the hub, and each active task is a sub-issue of its topic, so github.com shows the structure with a progress count. The links are computed from the record and added by `create_issues.py` and `sub_issues.py apply` |
| Board | Off unless chosen at setup: a private GitHub Projects board whose cards follow the record, brought up to date by `board.py apply` (see below) |
| Labels | Role labels, deliverable-section labels per project type, and topic labels |

When what the deliverable says changes, the topic's body is edited in place; the dated comment that caused the change and GitHub's edit history are the trail. A result that leaves the deliverable leaves the body, and its section label comes off. Posted comments are never rewritten.

Before editing a body, the session saves the current text and drafts a separate replacement. Posting compares that saved text with the live body and refuses an outdated draft. Coordinate one writer per issue: GitHub's body update is not atomic with the check, so two writers can still race. See [posting and verification](../skills/recortex/references/posting-and-verification.md#4-post).

## The daily loop

1. Start. The session reads the newest resume comment on the hub, then the open `active` issues, then the newest comments of the topics in play. With the hook this begins automatically.
2. Work. When an experiment finishes, a decision is made, something fails or a change is merged, the session drafts a comment from the matching template, checks every number against its source file, lints the draft and posts it. The script reads the comment back from GitHub and compares it with the draft.
3. The deliverable text changes. The topic's body is edited in place and its section labels follow its headings; the comment that caused the change is the record of why.
4. Stop. The session posts a checkpoint on each active issue and a new resume comment on the hub.

Before a new memory is written, `index.py query recall --issue N` shows the newest memories of its thread with their state, the open questions and untried ideas elsewhere, and the words the project has explained. The session writes the plain line first, links the memory to the earlier one it builds on when that link is true, and explains in a fold at its end any word that cannot be understood without the project. The linter checks the plain line, the links and their targets, the Evidence lines and the folds before anything is posted, and the post prints the new memory's id for the next link.

## Steps and checkmarks

A plan you agree to, in plan mode or by typing "go ahead", becomes a task with numbered steps: a `steps` memory on an `active` issue. When the agent reports a step done, a later memory carries a mark that checks it, and the task's list, `- [x] 2. step text`, is rewritten from the memories by code. A box ticked by hand on GitHub is not believed and not overwritten; the session asks you what to do. A loose "remind me to ..." becomes an item on one standing "To do" task. You see the list on the task, in the brief, on the mind page and with `/recortex:todo`. With the observer on (at the level you chose) this happens in the background, and anything it is unsure about waits for your answer; without it, the session does it when you ask. Details: [references/tasks.md](../skills/recortex/references/tasks.md).

## Sub-issues and the board

On github.com the hub shows its topics as sub-issues, and each topic its tasks, so the structure of the record is one click away. The links are computed from the record, and a link changed by hand on GitHub is reported, not believed. They need only the `repo` scope that `gh` already has.

A project can also keep a private GitHub Projects board of its work. It is off unless you answer yes to the board question at setup. It holds a card for each open `active` task, each open pull request and each issue waiting for triage, and for those finished in the last 30 days, in the columns To triage, Todo, In Progress, Waiting for a yes, Blocked, Done and Set aside. The plugin computes each card's place from the record: a failure that blocks a task puts its card in Blocked, a hold puts it in Waiting for a yes, and a not-planned close puts it in Set aside, each with a link to the memory behind it.

- Setup needs the `project` scope on your `gh` login. The plugin never adds it on its own: `gh auth refresh -s project` opens a browser, and it lets `gh` read and write all of your projects. Without it, setup does the rest and prints the command.
- The board is a copy. The record stays authoritative, and the plugin writes nothing from a card into the record. Text on the board is untrusted and never reaches a brief.
- The main session runs `board.py apply` at each pause; hooks never write to GitHub. A card you move by hand is reported in the next session's brief, and the session asks you before anything changes.

The rules are in [references/board.md](../skills/recortex/references/board.md).

## What survives compaction, and why

Compaction replaces a long conversation with a summary. Summaries drop detail and sometimes get numbers and decisions wrong, so this skill never treats one as a source.

Only posted work is durable in GitHub. An abrupt stop before capture or publication can lose a result. The ordinary PreCompact brief supplies instructions; the [observer](../skills/recortex/references/observer.md), experimental and off unless enabled for a folder, can queue source pointers and review events independently. That queue depends on native source availability and is local until publication. The [continuity demo](../examples/continuity/README.md) tests recorded recovery with a fresh local index and provides a separate protocol for measuring Claude's behavior across sessions.

| What | Where it lives | How it comes back |
|---|---|---|
| Current focus, active issues, last decisions, next actions, holds | The newest resume comment on the hub by an allowed account | Printed into context in the record brief after every compaction, or read with `resume_block.py show` |
| Where the record is, the topics by section, active issues, issues that need triage | The hub and the issue labels, copied into a local index | The record brief at session start; a short brief in every subagent; a one-line reminder on prompts when it changed |
| Results, decisions and failures not yet posted | The conversation | The PreCompact hook tells the compaction summary to keep them, with enough detail to post them |
| Work in flight | Checkpoint comments on `active` issues | Read in step 2 of the session protocol |
| Results, decisions, failures | Dated comments on topic issues | Read on demand; nothing depends on remembering them |
| What the deliverable says now, with its numbers | The bodies of the topic issues, each number with its source | Read before acting or writing |
| The rules themselves | The skill, and optionally the project's CLAUDE.md | Loaded by Claude Code |

The resume block is small on purpose (under 4,000 characters) and a new one is posted at the end of every working stretch, so the first thing a blank session reads is short, current and written for exactly that reader. Posting it as a comment, never as an edit, also keeps a history of where the project stood at each stop.

## Where the record lives

The record is one interface with two homes: GitHub issues, or a folder of Markdown files (a hub, a file for each topic and a file for each entry, each sealed with a key that stays on your machine). Every script, hook and view reads and writes through that interface, so a local record has the same brief, recall, dashboard and tasks. The sync reads a local record's files into the same index. The dashboard's Record pane reads any topic whole.

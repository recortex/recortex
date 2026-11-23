# Reference

The agents, the hooks and the scripts, one table each, and the commands for the mind page and the field sheet.

## The agents

The plugin adds three agents for bulk and audit work, one tier below the main session: `record-drafter` drafts topic bodies in batches to files only; `record-reviewer` reviews drafts, per batch and across all bodies; `record-auditor` checks the record for drift, untriaged issues and new deliverable signals. These agents never publish. A fourth agent, `ask`, answers one question about the project's history from the local index; it has Bash, and a hook stops every command of its that is not one `index.py query` read, so it posts nothing. The drafter has no shell and no GitHub access. The reviewer and the auditor are read-only by instruction: they need Bash for read commands such as `gh issue view` and `git log`, so their tool list does not enforce it, and the main session checks what they did. Each preloads the skill and uses your default model; to choose another, copy its file into the project's `.claude/agents/` and set `model:`. A separately approved observer publisher can append reviewed event comments; mutable record state remains the main session's responsibility.

## The hooks and the CLAUDE.md snippet

Four brief hooks keep every session and subagent aware of the record, built from a local index of the issues (`scripts/index.py`, a SQLite file under `~/.cache/recortex` that can be rebuilt from GitHub at any time; hooks and terminal scripts use the same file):

| Hook | Prints |
|---|---|
| SessionStart (startup, resume, clear, compact, fork) | The record brief: the rules for writing to the record, the hub, the deliverable topics by section, the open active issues, the issues that need triage, and the newest allowed resume block. It syncs the index first; if GitHub does not answer in time it prints the brief from the index and says how old it is |
| SubagentStart | A short brief for the subagent: where the record is, the issues in play, and that it reads the record for facts and posts only when its task says so; with a field sheet you approved, its short part in its own frame |
| UserPromptSubmit | One line with the hub and the active issues, only when it changed or every tenth prompt (`"prompt_hook": {"enabled": false}` in `.recortex.json` turns it off) |
| PreCompact | Instructions for the compaction summary: keep the issue numbers in play and every result, decision or failure that is not yet posted |

A second PreToolUse check, `run_pretool_guard.sh`, runs before every Bash call in every project, observer on or off, and costs a shell and one `case` unless the call is the `ask` agent's or names the observer: it stops any command of the `ask` agent that is not one `index.py query` read, and records the grant that a session's `enable` needs from your recorded answer. A fifth hook, SubagentStop, prints nothing: when one of the plugin's own subagents stops, it adds that run's token counts to a small local file, so that the usage view can show them. Observer handlers, experimental and off by default, also run on these four events and on PreToolUse (Bash calls only) and Stop. Without this machine's approval for the folder they start no Python and cost about 3 ms. When on, they collect bounded pointers to the conversation locally (your typed prompts and the agent's messages to you, never tool output), return framed task context, and report pending or degraded work once per session. PreToolUse may deny only a supported action matching an established explicit user hold, or, when the holds cannot be read, every Bash command but `pwd`, `ls` and `cat`. The handlers make no model or network calls themselves; at the edges of a turn they can start a detached finite worker, which performs independent checks and optional append-only publication. Its private pending state cannot be rebuilt from GitHub. See [observer controls and limits](../skills/recortex/references/observer.md).

They print nothing when the project has no configuration file or the repository is not in your trusted list, and never write to GitHub. Set-up builds the index once (`index.py sync`); otherwise the first session builds it in the background and the brief arrives with its next prompt. In a project that is set up and trusted, a brief that cannot be produced is replaced by one line naming the reason. `python3 scripts/hooks.py --print` shows each payload and its size; `python3 scripts/hooks.py --doctor` shows why a brief is or is not shown: configuration, trust, whether `gh` is found and signed in, storage folders with owner and mode, the index and the last sync failure. The plugin installs them. For a plain skill install, merge [`templates/settings-hook-snippet.json`](../skills/recortex/templates/settings-hook-snippet.json) into `.claude/settings.json` or `~/.claude/settings.json` and adjust the paths.

The CLAUDE.md snippet: [`templates/claude-md-snippet.md`](../skills/recortex/templates/claude-md-snippet.md) is a few lines for the project's CLAUDE.md that give every session the hub's location and the recording rules.

## Scripts

All are in `skills/recortex/scripts/`, use only the Python standard library and `gh`, and accept `--help`.

| Script | What it does | Writes to GitHub |
|---|---|---|
| `setup_labels.py` | Creates the labels of the chosen profiles. Existing labels are left alone unless `--update` | yes |
| `lint_drafts.py` | Checks drafts: required sections, leftover placeholders, bare `#N`, body length, Mermaid rules, secrets, project-defined restricted patterns; history in a topic body; section headings against labels; a memory's plain line, links, Evidence and folds, against the record with `--index` or `--online` | no |
| `create_issues.py` | Creates interlinked issues and comments from a manifest, replaces `NEW-<key>` placeholders with real numbers, reads every body and comment back, and can be rerun after a failure without duplicates or overwriting changed bodies | yes |
| `post_verified.py` | Posts one comment or replaces one issue body, with lint and read-back; a body replacement requires `--expected-body-file` saved before drafting and a backup, and refuses a changed live body; for a topic body, the section labels follow the headings; for a memory, its links and evidence are checked against GitHub and its id is printed | yes |
| `convert_body.py` | Converts a record body of an older form into deliverable text: stops if the live body changed, keeps the former body as a comment, replaces the body and section labels, reads all back | yes |
| `resume_block.py` | `show` prints the newest resume comment written by an allowed account, for trusted repositories only. `update` posts a new resume comment on the hub and reads it back; it never edits the hub or an earlier comment, and warns when the posting login is not allowed | `update` only |
| `snapshot.py` | Saves all issues, pull requests and comments as local Markdown with an index, for offline reading and for surveying an old tracker | no |
| `index.py` | Keeps the local index of the record: `sync` reads what changed on GitHub; `query` answers consistency, impact, recall, mind, timeline, triage and brief questions without network | no |
| `sub_issues.py` | Links the hub's topics and each task as sub-issues, from the record; `plan` shows the difference | `apply` |
| `board.py` | The private board of the work: `setup`, `plan`, `apply` (and `--restore N` for a card moved by hand) | `setup`, `apply` |
| `statusline.py` | An optional status line segment: whether recortex is on for the project folder, and the age of the record; `setup.py apply` offers it | no |
| `mind.py` | Builds the mind page: one private local file that tells the project's memory in sentences, from the index; `--sync` or `--figures` also embeds the pictures of its figures; `--paper [--version NAME]` writes the paper alone as a page to print or save as PDF; with a plan, the page has "Read the manuscript", the paper as a paper with its changes since an earlier version | no |
| `hooks.py`, `run_hook.sh`, `session_start_hook.sh` | The four hooks; `hooks.py --print` shows their payloads | no |
| `observer.py`, `observer_hooks.py`, `run_observer_hook.sh` | `trust-ancestor PATH|--failing|--list|--forget PATH` (on your answer to the trust question: trusts a folder above Claude Code's transcripts on shared storage); `enable --hosts shared|any`; `status` shows `approval_reason` and `binding`; Experimental, off by default: sorting, drafting and review of the conversation, tasks and checkmarks, drafts of the paper's text for you to accept, task context, explicit holds and pending receipts; `enable --level`, `pending --waiting`, `log`, `undo`, `limits` | approved worker event comments, tasks and rendered copies only |
| `setup.py`, `setup_questions.py` | `task-reminder show` and `set --mode on|off --yes` (the light reminder setting); `observer --hosts shared|any`; each question's options carry a `description`; `preflight` checks `gh` and Python before the first question; `detect` suggests deliverables and fields from file names; `apply` makes the setup from answers you confirmed; `fields` shows, sets and approves the field sheet; `questions` lists what an update added that you have not answered yet, and `answer` keeps your answer on this machine | `apply` |
| `acts.py` | `ask KIND` prints the question for an act that needs your yes, and the command of each option; `list` | no |
| `publish.py` | Puts the static dashboard page on one file of one branch, on your answer to the question of `acts.py ask publish`: it makes the branch (one commit that holds only the page) and switches GitHub Pages on only when that question said so, reads the file back and prints the page's link; `--dry-run` and `--list` | one file of the branch it names |
| `task_reminder.py`, `dashboard_notes.py`, `observer_lease.py` | The light setting and line of the task reminder (no observer needed); the notes the dashboard leaves for a session's next prompt; the host lease that keeps one host's observer worker on a queue | no |
| `dashboard_action.py` | Launch the private interactive dashboard for this project. Use `--open` to open it in your browser. Preview and confirm a task step or a Project Map correction | confirmed checkpoint and derived task list, a map entry or a decision review on the hub, or a verified edit of one topic paragraph |
| `tasks.py` | Tasks and steps: `status`, `render` (the task list under `## Plan`), `close`, `reopen`, `open`, `add` (a to-do item), `mark` and `drop` (steps checked or dropped by your words) | yes |
| `status.py` | Where the project stands from the local index (`--sync` reads GitHub into it first): In short, open tasks with their latest done steps, what is due in the text and what waits for a yes | no |
| `usage.py` | `show`: what recortex used, in tokens and calls, never money | no |
| `catchup.py` | Lists earlier work and work done since the newest recorded event; drafts reconstructed event comments | no |
| `compile.py` | Writes the deliverable section by section in the hub's order, the figure, table and source lists, the full dated timeline of the project and a manifest; `--version NAME` compiles a named version of the paper | no |
| `plan.py`, `write.py`, `own_paper.py`, `versions.py` | For a manuscript: the plan of the paper, the writing of its text in the mode you chose, your own paper (never changed without your words), and the versions of the text on a branch of the repository | yes, each under your mode |

Command-line writers provide a dry run to inspect a change. The interactive dashboard previews the checkpoint or the affected map branches before confirmation.

## The commands

Type these commands in Claude Code from your configured project.

| Command | What it does |
|---|---|
| `/recortex:record [what]` | Records what just happened in this conversation, or what you name. A plan you approved becomes a task |
| `/recortex:ask <question>` | Answers from the record only, starting "From the record:", and posts nothing |
| `/recortex:todo [add <text> \| done <n>]` | Shows the to-do list with its checkmarks, adds an item or checks one off |
| `/recortex:status` | Where the project stands, the open tasks and their next steps, what is due in the text and what waits for your yes |
| `/recortex:usage` | What recortex used, in tokens and calls |
| `/recortex:map` | Browse or correct the project hierarchy and choose which local outlines can inform it |
| `/recortex:guide` | Weigh a choice, challenge an assumption, or adjust handoffs |
| `/recortex:impact` | Review work affected by a recorded change of decision |
| `/recortex:dashboard` | Opens the project dashboard and gives you its address, with a direct manuscript link when available |

`@agent-recortex:ask` starts the `ask` agent for a longer question. There is no bare `/record`: plugin commands carry the plugin's name.

## The mind page and the field sheet

The dashboard opens on Overview. Project Map shows how ideas, components, and methods fit together. Expand a branch to read its evidence or switch to Outline. Work contains open tasks, History contains project events, and Outputs contains configured deliverables. Build the offline page from the local index with `mind.py`. To report a completed step or edit a map branch in the browser, launch `dashboard_action.py --open` from the configured project. Details are in [the dashboard reference](../skills/recortex/references/mind.md). To compare invented software, research, mixed manuscript, and sparse projects, use the [offline browser examples](../examples/dashboard/README.md).

```bash
python3 skills/recortex/scripts/index.py sync
python3 skills/recortex/scripts/mind.py --open
```

The field sheet tells every subagent what counts as proof in your work and what must never be written down. You read the whole sheet and approve it; one that changed since you approved it is not shown until you approve it again. Details are in [`references/fields.md`](../skills/recortex/references/fields.md).

```bash
python3 skills/recortex/scripts/setup.py fields show
python3 skills/recortex/scripts/setup.py fields set --answers answers.json --dry-run
python3 skills/recortex/scripts/setup.py fields approve --fingerprint <the fingerprint in the line fields show printed> --yes
```

# The board and the sub-issues

> Commands on this page write `scripts/<name>` for short. That is the `scripts` folder of this skill, next to `SKILL.md`; call it by its full path.

Two views of the record on github.com, both kept in step from the record and both optional to look at. The record stays authoritative: delete the board, run setup and `board.py apply` again, and the same board comes back. Nothing on the board is ever written into the record by the plugin on its own.

## Sub-issues

Each topic issue (label `final-record`) is a sub-issue of the hub, and each `active` task is a sub-issue of its destination topic. The hub then shows its topics with a progress count on github.com, and a topic shows its tasks. The links are computed from the record (the hub's tables, the task's "Destination topic" section), and a link changed by hand on GitHub is reported, not believed.

- `create_issues.py` adds the link when it creates a topic or a task, waits a second after each (`RECORTEX_LINK_PAUSE`), and prints `sub-issue: #7 is under #5`. A task whose "Destination topic" names no topic number is not linked by it; `sub_issues.py apply` links it under the only topic that shares its `topic:` label.
- `sub_issues.py plan` says which links the record wants that GitHub lacks or shows differently; `sub_issues.py apply` (`--dry-run` first; at most 60 links a run, `--limit`) adds the missing ones and reads each back through `Issue.parent`, also after a failure, since GitHub may have acted before it failed. A task that already has another parent is reported and left alone. Nothing is removed.
- When GitHub refuses a link (an older GitHub Enterprise Server, or more than 100 sub-issues under one parent) the script says so in one fixed line, and the hub's tables stay the structure.
- The sync stores each issue's parent (`parents`), and `index.py query consistency` reports a link the record wants that GitHub does not show, for a record that uses sub-issues at all.
- The links need the `repo` scope `gh` already has. Topic issues stay open for the life of the project, so the hub's own count reads "0 of N" for as long.

## The board

A private GitHub Projects board of the work, opt-in at setup. It holds a card for each open `active` task, each open pull request and each issue waiting for triage, and for those finished in the last 30 days. Topics and the hub have no card; ideas and open questions stay on the mind page.

### Where a card belongs

One table, in `board_rules.place()`; the first matching row wins.

| The record says | Status | Why (after the status name) |
|---|---|---|
| Pull request, merged | Done | none |
| Pull request, closed without a merge | Set aside | none |
| Pull request, open (draft or not) | In Progress | none |
| Open issue that needs triage (`index.py query triage`) | To triage | none |
| `active`, closed as completed | Done | the result or failure memory its "Result" section links to, else the newest result or failure on it |
| `active`, closed as not planned | Set aside | the newest decision on the task or naming it |
| `active`, open, labeled `needs-decision`, or named (`#N`) in the Holds line of the current resume block | Waiting for a yes | the resume comment |
| `active`, open, with a failure that nothing followed, on the task or naming it in a field | Blocked | that failure |
| `active`, open, with at least one memory on it | In Progress | its newest checkpoint, if any |
| `active`, open, no memory yet | Todo | none |

Only memories the mind page would tell count: an allowed account, never edited. A failure is followed when any later memory links to it, so a decision posted with `because=<the failure>` unblocks the card. Two rules for sessions follow: a failure that blocks a task names the task in one of its fields (for example `#7`), and a hold names the issue it holds.

The Why field holds the plugin's own mark of what it last wrote: the status name, after it the address of the memory behind the state, and for a task that has steps its count, for example `In Progress 3 of 5 steps` (steps done of steps not dropped, from the record's told memories; a task with no steps has no count, and its mark is as it was). That is how a hand move is told from a card the plugin has not caught up with. A new mark of the same status is written to Why alone.

### What setup does

`setup.py` asks one question, off by default: keep a board of the work on GitHub Projects (no, a new board, or an existing board's number). Answering no means no call, no brief line and no scope, ever. The `board` answer is `"no"`, `"new"` or `{"existing": 3}`.

`board.py setup --mode new --yes` (setup.py runs it) needs the `project` scope on the user's `gh` login, which the plugin never adds on its own: `gh auth refresh -s project` opens a browser, and widening a login is the user's act. The scope lets `gh` read and write all of the user's projects; say so before asking. Without it, setup does the rest and prints one fixed line with the command.

It creates the board under the owner of the record's repository (a user's repository gets a user project, an organization's an organization project), linked to the repository; adds the options To triage, Waiting for a yes, Blocked and Set aside to Status next to Todo, In Progress and Done (existing options keep their ids, none is removed or renamed); adds the text field Why; makes three views (Board, Waiting on you, By thread); sets the board private and reads that back; on a board it creates, writes a fixed README and description with no record text (an existing board keeps its own); records the user's approval of owner and number on this machine (`board-approvals.json` in the private folder of the configuration folder, through `storage.py`); and writes the `board` entry (`{"owner": ..., "number": ...}`) into `.recortex.json`. The "By thread" view groups by "Parent issue"; the API takes no grouping, so set that once in the browser.

Optional, in the browser, none required: keep GitHub's two default workflows (a closed or merged item gets Done); an auto-add workflow with the filter `is:issue label:active` and one with `is:pr`; auto-archive. Leave off any workflow that closes an issue when its Status changes: it would turn a dragged card into a change of the record. `hooks.py --doctor` warns when it is on. A board of a user account does not include the repository's collaborators; the user invites them under Manage access.

### Commands

- `board.py plan`: offline, from the index. Lists cards moved by hand, cards behind the record, cards not on the board yet and cards with no issue in the record.
- `board.py apply --dry-run`, then `board.py apply`: syncs the index, reads the board, adds the missing cards, writes Status and Why for cards that are behind, leaves hand-moved cards alone, then reads every changed card back and says `MISMATCH` for any that did not take. At most 20 cards are written per run (`--limit`; about 40 requests, a minute or two in all); run it with a timeout of five minutes, and run it again until it says nothing waits. The cards it read back are kept in the index, so `board.py plan` and the brief are right at once. A run that stops is run again; adding a card is idempotent on GitHub's side.
- `board.py apply --restore N`: puts one hand-moved card back, after the user's answer.

Only the main session runs `apply`, and, from 1.5.0, the observer's worker at the level Everything (`board.apply_for`: the same run, at most 20 cards at a time, a fixed result code and no text from the board); hooks never write to GitHub, and the auditor runs only `plan`. A board failure never fails a record post, because no posting script calls `board.py`. A failure of `board.py` is one fixed line: `board: not done: <reason>. Run hooks.py --doctor.`

### When the board is updated

At each pause: `resume_block.py update` prints a fixed reminder when a board is configured, and the session runs `board.py apply`; and when a task opens or closes. During a stretch of work the board can be one stretch behind.

### A card moved by hand

The next session-start brief says, inside its data frame, `Board: 2 cards were moved by hand: #7, #13. Run board.py plan.` Run `board.py plan`, which prints `#13: the record says Todo; the board says In Progress`, and ask the user. Nothing changes until they answer:

- "Record it": post the memory that makes it true (a checkpoint for work that began, a failure, a decision with the user's exact words, a closed task). Record and board then agree with no board write.
- "Put it back": `board.py apply --restore 13`.

A card whose Status differs from the status the plugin last wrote (the first words of its Why) stays reported as moved by hand, whatever the record does after, until the user answers; the one exception is Done on a closed issue or pull request, which is GitHub's own workflow. A card dragged to a column the user added is left where it is and not reported, until the record itself moves on. A closed or merged item gets Done from GitHub's default workflow at once; the next `apply` corrects a not-planned close to Set aside.

### What is read back, and what is never done

The sync's last step (`index.py`, `board_step`) reads the cards into the index table `board_cards` when a board is configured and approved: the item id, kind, archived flag, the issue's number, the plugin's own status name found by exact option name (any other option is "other") and the Why text, which is compared and never printed. It runs after everything else, makes no write, never makes the record's sync incomplete, and leaves its fixed reason code in `board_error` (the gh login lacks the project scope; the project was not found; a field of the board is missing; the board is public while the record is private; GitHub could not be read; the budget ran out; the board is not approved on this machine; the board entry of the project file does not fit; the board does not read back as it was set up). The brief then shows `recortex: board not read: <reason>. Run hooks.py --doctor.` outside its data frame.

Everything on the board is untrusted: option names, draft titles, text fields, the README, view names. No text from it reaches a payload or a terminal line. The `board` entry is used only in a trusted repository, only when its owner is the owner of the record's repository, and only while it equals what the user approved on this machine; a cloned project cannot point the plugin at someone else's board. `apply` refuses a public board for a private record.

Never done: closing, reopening, labeling or editing an issue because of a card; priority, estimate or iteration fields; cards for topics or draft cards; deleting, renaming or archiving anything on the board; changing the user's login or its scopes.

Several records of one owner may share one board: each record's `board.py` touches only cards of its own repository.

A local record has none of this: the script prints one fixed line, `recortex: the board is on GitHub Projects, and this project keeps a local record. The dashboard shows the same work.`, and does nothing.

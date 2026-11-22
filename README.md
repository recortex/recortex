<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/recortex-lockup-reversed.svg">
    <img src="assets/recortex-lockup.svg" alt="recortex" width="420">
  </picture>
</p>

<p align="center">See. Record. Recall. Act.</p>

recortex is record + cortex: a record of your project, and a brain that works from it. It is a Claude Code plugin that
keeps findings, decisions, failures and deliverable text in GitHub issues.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/how-it-works-dark.svg">
  <img src="assets/how-it-works-light.svg" alt="An optional observer follows each session at the level you choose, keeps notes and a to-do list with checkmarks, and asks you before anything it should not do alone. GitHub issues are the record. The cortex works from it: a brief, the project dashboard, the finished text and a board.">
</picture>

## What you get

- A session that starts from the last recorded state: a short brief opens each session and subagent.
- A record you can read on GitHub. One pinned hub, one issue per topic, and a dated memory for each result, decision, failure, idea and question.
- Weigh options with `/recortex:guide`. Trace changed decisions with `/recortex:impact`.
- Open `/recortex:dashboard` to resume work, trace decisions, and read deliverables offline. Use `/recortex:map` to browse and edit the project hierarchy.
- Compile the current paper or release notes from topic bodies.
- The paper's plan, text and versions. The session writes text in the mode you choose, never changes a paragraph you wrote, and keeps every version.
- Helpers that know your field, from a short field sheet of what counts as proof.
- Shared and changing hosts. An approval follows your home folder across nodes.
- A board that keeps itself current. Sub-issues and a private Projects board follow the record.
- Tasks with checkmarks. A plan you approve becomes a task with numbered steps, checked off as the agent reports them done.
- An optional observer. It follows the session in the background, keeps the record and the task list, and drafts the paper's text only for your yes. You choose what it may do on its own. [Controls and limits](skills/recortex/references/observer.md).

## Quick start

You need Claude Code, the GitHub CLI `gh` (signed in) and Python 3.8 or newer.

1. Install the plugin.

   ```bash
   claude plugin marketplace add recortex/recortex
   claude plugin install recortex@recortex
   ```

2. Open Claude Code in your project and say: "Set up the record for this project." Setup checks `gh` first and says what to run if it is missing or signed out. You answer a few multiple-choice questions, including whether to trust the repository (the plugin shows a session nothing from GitHub until you do), and confirm a summary. Nothing is written before that.

3. Say "open my dashboard" to browse the project. For a manuscript, choose Outputs → Read the manuscript. Say "pause here" to save a handoff.

Months into a project, or coming from another tracker? See [Starting from existing work](docs/existing-work.md).

Other commands include `/recortex:record`, `/recortex:ask`, `/recortex:todo`, `/recortex:status` and `/recortex:usage`. `@agent-recortex:ask` answers project history questions from the record.

## A short story

An invented project sorts scanned letters by the date written on each page. This is one of its threads, as the mind page tells it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/mind-story-dark.png">
  <img src="assets/mind-story-light.png" alt="The mind page showing one thread: a decision, an idea that was tried, a failure, the decision it led to, and an open question.">
</picture>

> On March 4 we decided to put every page with no readable date in an undated pile, and never give it a guessed date.
>
> *then*
>
> On March 5 we had the idea to give an undated page the date of the pages scanned just before and after it.
>
> *we tried it*
>
> On March 6, borrowing the date from the pages around it put 4 of 6 test pages in the wrong year.
>
> *so*
>
> On March 7 we decided to show each undated page to the person and ask for its date, and stop borrowing dates from other pages.
>
> *so*
>
> On March 10 we asked whether asking still works when a box has a hundred undated pages and not twelve. Still open.

Each sentence is one comment on a GitHub issue; the words between them are links the session recorded. Three weeks later a new session opens with, abridged:

```text
recortex record brief for octo-org/sample-project (from the local index synced 2026-03-31T16:02:11Z (UTC), 2 minutes old).
Open active issues: 1
- #7 Task: pages that carry two dates
**Current focus:** Release 1.4.0, which reads dates written by hand. Pages that carry two dates are what is left.
**Holds:** The project owner says when 1.4.0 is tagged; until then, no tag.
```

Before it writes about the undated pages, it reads that thread and does not try the borrowed dates again.

## The board

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/board-dark.svg">
  <img src="assets/board-light.svg" alt="A board with the columns Todo, In Progress, Waiting for a yes and Done. A task's card names its thread, and the memory that put it in its column when there is one.">
</picture>

Optional, private, off unless chosen at setup. The record determines each card's column; each pause updates the board. A manual move prompts the session to ask what changed before updating anything. Setup is in [How the record works](docs/how-it-works.md#sub-issues-and-the-board).

## Read more

| If you want to | Read |
|---|---|
| See how the issues are laid out | [How the record works](docs/how-it-works.md) |
| Try three dashboards | [Offline examples](examples/dashboard/README.md) |
| Test recovery and measure a fresh Claude session | [Continuity demo](examples/continuity/README.md) |
| Install as a plain skill, trust a shared repository, upgrade | [Install and upgrade](docs/install.md) |
| Start from months of work or an older tracker | [Starting from existing work](docs/existing-work.md) |
| Know what the plugin will and will not do with untrusted text | [Safety and security](docs/security.md) |
| Look up a script, a hook or an agent | [Reference](docs/reference.md) |
| Change the plugin | [CONTRIBUTING.md](CONTRIBUTING.md) |

## License

FSL-1.1-MIT. See [LICENSE](LICENSE).

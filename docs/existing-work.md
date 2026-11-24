# Starting from existing work

How a project gets its record: from nothing, from months of work, or from an older tracker.

## A new project

In a project without a record, the first session prints one line saying the plugin is installed and the project has no record yet. Say something like "set up the record for this project". Claude will:

1. Check that `gh` runs and is signed in to github.com, and that Python is 3.8 or newer (`setup.py preflight`). When a check fails, it shows one line that says what to run, such as `gh auth login`, and stops before the first question.
2. Look at the project's file names (`setup.py detect`) only to pre-select answers, then ask a few questions with multiple-choice answers: what the project will produce (paper, thesis, software, dataset, report, prototype, proposal, documentation, other), the sections of each, where the record lives, who decides, the time zone, whether there is earlier work to catch up on, and the optional parts.
3. Show a summary for your confirmation; nothing is written before it.
4. Apply it (`setup.py apply`, dry run first): the configuration, the labels, the hub with its first resume comment, your trusted-list entry if you agree, the optional triage workflow and CLAUDE.md snippet, and the first sync of the local index.

## A project with months of work behind it

Say "catch up the record". Claude takes a read-only inventory of what exists (`catchup.py inventory`: history, issues, pull requests, documents, result folders, and, only if you agree, this project's local Claude Code transcripts), proposes a topic map for your approval, drafts the bodies with the plugin's agents, and posts past events as reconstructed comments with their original dates and sources. The steps are in [`references/catch-up.md`](../skills/recortex/references/catch-up.md).

## A project with an older tracker

Say "migrate the issues of owner/old-tracker into the new structure". Claude snapshots the old tracker to local Markdown, maps every old issue and pull request to a topic, shows you the map, copies the content into new issues (deliverable text in the bodies, history in comments), and, if you want, leaves one short pointer comment on each old item. Nothing in the old tracker is edited or deleted.

Topic bodies that hold a full record (a dated current state, folded old text, decisions, open work) are converted in place with `convert_body.py`: it checks that the live body is the one the new text was drafted from, keeps the former body as a comment, replaces the body, sets the section labels from the new headings and reads everything back. The steps are in [`references/migration.md`](../skills/recortex/references/migration.md).

A project can be caught up into a local record in the same way: the inventory and the gap read git and the files, and the lists of pull requests and issues are skipped.

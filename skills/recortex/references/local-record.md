# A local record

A project's record can live in a folder of Markdown files on the user's machine instead of GitHub issues. The scripts, the hooks, the dashboard and the commands work the same way; only the home of the record changes. A local record needs Python and nothing else: no `gh`, no account, no network.

This page is for the session. What each part of the record is: [issue-model.md](issue-model.md). How the index reads it: [hooks-and-index.md](hooks-and-index.md).

## What the record is made of

One record is one folder:

```text
record.json                          the record's id, its name and its profiles, with a seal
hub.md                               the hub (topic number 1)
hub/                                 the hub's entries and the earlier texts of its body
  2026-11-23T1410Z-<id>.md           an entry: a resume block, a ledger entry
topics/
  reader.md                          a topic: its text, its state and its labels
  reader/
    2026-11-23T1405Z-<id>.md         an entry of the topic: a result, a decision, a failure
    2026-11-23T1420Z-<id>.md         an earlier text of the topic (its front matter says kind: body)
```

The invented project here is a "sample project" whose record is `local--record/k3f5x2m7q4w6z5ab`. A file starts with a front matter block (`recortex`, `kind`, the number or id, the times, the title and the labels), then the text, then one last line that seals the file. The text is the same text that a GitHub issue or comment would hold: a topic's file holds the deliverable text, an entry holds one dated memory with its event marker.

The project file names the record: `"repo": "local--record/<id>"`, `"store": "local"`, `"place": "own"` or `"project"`, and `"hub": 1`. It never holds a path.

## Where it lives

| Place | Folder | When |
|---|---|---|
| Its own folder | under the user's data folder, in a private folder named by the id | the default: private to the user's account |
| Inside the project | `.recortex/` at the project's root | only on the user's answer to the in-project question (`inproject`); it is committed with the code if the user commits it, and a public repository then shows it |
| A folder of the user's | an absolute path | for a synced folder or a repository of its own |

The machine keeps a row for each record it uses (the id, the folder's real path, its owner and mode), signed under the user's key. A record at another path, or a copied project, is not read until the user answers the `folder` question.

## Addresses

A topic is `#N`; the hub is `#1`. An entry is `#N/ID`, where `ID` is the number that the entry's id gives (up to sixteen digits). A local address is shown as text and as a link inside the dashboard; it never becomes a link out of the page.

## The seal, and what it vouches for

Every file ends with a seal that holds the record's id, the file's path and a MAC of the bytes above it, made with a key that stays on this machine. A file reads in one of four states:

| State | Meaning | How it is treated |
|---|---|---|
| sealed | the seal holds under this machine's key | the user's own words and text |
| changed | the seal does not hold: the file was edited outside recortex | shown and recalled, marked "Changed outside recortex", never counted as the user's words |
| foreign | sealed under another key (another machine, or a lost key) | marked "Written on another machine", never counted |
| unread | not a file recortex can read (a sync tool's copy, a half-written file, a link) | not read; counted, and named by `hooks.py --doctor` |

A changed or foreign entry is not a decision, not a resume block and not a ledger row. A text that was changed is not told as a deliverable's text. Nothing edits or removes an entry: a correction is a new entry.

When the brief says that files in the record folder were not read, say so in one line and offer `hooks.py --doctor`, which names them and why. A sync tool's conflict copies (`reader (conflicted copy).md`) are the usual cause; nothing in them is read.

## The two questions about what is not vouched for

- `folder`: the record is at a folder this machine has not approved for it. `acts.py ask folder` prints the question; on a yes, `setup.py record-folder --yes` writes the row.
- `adopt`: a topic's text was changed outside recortex (`acts.py ask adopt topic N`), or files carry a seal this machine cannot check, because they were written on another machine (`acts.py ask adopt foreign`). The yes label ends with a short token of the bytes the question is about, so an answer holds for those bytes and no others. On a yes, `setup.py adopt --topic N --yes` or `setup.py adopt --foreign --yes` keeps the text as it is as a version and seals the text, the title and the labels (or the files) as the user's, reading each file once and refusing when it changed meanwhile. A changed entry is never adopted, and neither is a file this machine sealed that now carries another key. Which files this machine sealed is remembered in the index. That memory does not yet cover files written since the last complete sync, it is lost when the index file is deleted or moved aside, and a rebuild of the same record keeps it.

Each question shows counts and fixed words, never the text. Ask them one by one, as every question that waits for the user (SKILL.md, "What needs the user's yes").

## A folder above the record that others can write

When the brief says the record is not read because a folder above it can be replaced by another account, ask the question of `trust` for that folder: `acts.py ask trust --scope record`. It is the act `trust` with a scope of its own: its question names the local record, and a yes for it never covers the folders above Claude Code's transcripts, nor the other way round. On a yes, `observer.py trust-ancestor --record --failing --yes` trusts each folder exactly as it is (its owner, group and mode); a change of any of them refuses the record again.

## What the operations are

Every script reads and writes the record through one interface, `record_store`, with two implementations. Its operations are `account_state`, `address`, `api_issue`, `close`, `command`, `create_issue`, `created_number`, `edit_body`, `find_issues`, `histories`, `hub_entries`, `is_missing`, `list_entries`, `list_labels`, `parse_address`, `post_entry`, `posted_id`, `read_back`, `read_entry`, `read_issue`, `reopen`, `require`, `safe_address`, `set_labels`, `show`, `supports`, `versions` and `whoami`; a local record also has `adopt_plan` and `adopt`, which the `adopt` question uses; GitHub's own extras (`create_label`, `edit_label`, `pin`, `is_pinned`, `visibility`, `dump`, `dump_commands`, `github`) are off for a local record. A dry run prints, for a local record, the same steps in plain words (`local record: post an entry on #3`) where a GitHub record prints the `gh` command.

## What is off

A feature that needs GitHub prints one fixed line and does nothing: the board, sub-issue links, the triage workflow, publishing to GitHub Pages (the page is saved as a file), the manuscript's versions branch (every version of a topic's text is kept in the topic's folder), approval by pull request (a change of the text is approved in the chat) and the pictures of figures. The lines are in `docs/reference.md` in the repository.

## The dashboard

The Record pane lists every topic. A topic's view holds its whole text, the earlier versions of the text (newest first, each shut) and every entry in time order. What is not told is shut under its mark and a line that says recortex will ask in the session whether to take it back as the user's. The view only displays: it has no button, and its text feeds nothing else.

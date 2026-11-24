# Posting and verification

> Commands on this page write `scripts/<name>` for short. That is the `scripts` folder of this skill, next to `SKILL.md`; call it by its full path.

Every post goes through the same five steps. The scripts do steps 3 to 5; steps 1 and 2 are yours.

## 1. Draft to a file

Write the draft from its template into the drafts folder. One file per issue body or comment. Do not compose a body inside a shell command: quoting damages backticks, dollar signs and tables, and the text cannot be reviewed or linted.

Keep the drafts folder clean. Notes, extracted tables and other scratch files go somewhere else. Posting commands take explicit file names (`--body-file`, or the manifest), never a folder or a wildcard, so a stray file cannot be posted by accident; a clean folder also keeps a reviewer from reading the wrong file.

## 2. Check against the sources, then get an independent review

For every number in the draft, open the file named as its source and read the value there. For every quote, find the original message. For every identifier (commit, job, path), check that it exists. Memory, including a compaction summary, is not a source.

For a new topic body, a change to the deliverable text, a hub edit, or anything the deliverable will cite, ask a reviewer that does not share your context (a fresh agent, or a person) to try to break the draft. Give it the draft and the source paths, not your reasoning. Ask it to look hardest at: numbers against sources; denominators; mixed runs; exclusions and failures; claims that go beyond the evidence; things stated as negative that were only not tested. Fix what it finds, and if the review did not return, the draft is not reviewed.

Routine result and checkpoint comments need the source check but not always the reviewer; decide by what a wrong post would cost.

In a folder where the observer is on, the main session posts its own memories as usual, with `post_verified.py comment` or a `create_issues.py` manifest. Each verified post is registered in the observer's queue, so that the observer does not post the same memory again, and the observer's ready events of that kind for that issue then wait for the main session. A decision the queue already holds is refused with its event key: post that event with `--eventkey KEY`, or discard it. See [the observer contract](observer.md).

## 3. Lint

```bash
python3 scripts/lint_drafts.py --kind result --repo owner/name --online draft.md
python3 scripts/lint_drafts.py --kind decision --index --issue 5 draft.md     # a memory, against the local index
```

| Check | Level |
|---|---|
| Required sections present, in order, not empty | error |
| Event header with date, time and zone | error |
| Decision carries a quote block, or says the words are not available | error |
| `{{template fields}}` or `NEW-<key>` left in the text | error |
| Bare `#N` that does not exist in the target repository | error (`--online` or `--known-numbers`), otherwise warning |
| Body over 65,536 characters | error; warning when close |
| Mermaid: `#`, parentheses, unquoted labels, nodes undefined or unused | error |
| Resume comment: heading with timestamp, markers, all six fields, block length; resume markers inside a hub body | error |
| Final record: title line, at least one section, every section filled, "Sources" last | error |
| Final record: an "As of" line with a date, month or year, a date and time in the text, a `<details>` block, a heading for decisions, open work, provenance, next steps or the other sections of the older record form (current state, not established, code and evidence, related topics, history, status), a dated heading outside "Sources" | error |
| Event comment: the marker line `<!-- ghim:event type=<type> at=<ISO time> -->` first, with the type of the comment and the same local time as the heading | error |
| Final record: a figure block without a caption, an embedded image or a Sources row; a table block without a caption, a table or a link, or a Sources row | error |
| Final record: a figure or table linked from a branch such as `main` instead of a commit or tag | warning |
| Final record: a line that looks like a section heading but is not one (`### Results`, `##Results`, indented, underlined, inside a code block) | error |
| Final record: a `## ` heading that is not a section heading of the project's profiles, or that two profiles share and the labels do not settle | error |
| Final record: section labels that disagree with the headings (`--labels`, or the labels in a manifest) | error |
| Memory: the marker holds only `type`, `at`, `recorded`, `source` and the link keys, each once; a link value is one to four comment ids; an id stands under one key; a link only on a kind that may carry it, never on a checkpoint, a triage comment or a reconstructed memory (`marker-key`, `link-value`, `link-kind`) | error |
| Memory: the plain line, first, one line of 20 to 200 characters, without a final period, backtick, `#N`, URL, path or commit, not repeating its label, a question starting with a question word, every number also in a field, as written or rounded (`plain`) | error |
| Memory: Evidence on a result, decision, failure or correction only, last, at most six lines in the four forms, a line range of at most 120 lines, a commit and not a tag (`evidence`) | error |
| Memory: folds after the last field, each well formed, within the limits, a word used outside its fold and explained once; no `<details>` or `<summary>` outside a fold; no fold on a triage comment (`fold-format`) | error |
| Memory, with `--index` or `--online --issue N`: each link target exists in this repository, is a memory, is not later, may be linked, was written by an allowed account and was either never edited after it was posted or edited only by allowed accounts, as the index's edit history shows (`link-target`); a cited comment exists on its issue (`citation`); a table or figure is a block of the topic body and a pull request is one (`evidence`); a thread fold stands on an issue with at most one `topic:` label (`thread-issue`); "We use" and "This thread is about" only for what is not explained yet, "We now use" and "is now about" only for what is (`word-known`, `word-new`, `thread-known`, `thread-new`) | error |
| Memory, without a lookup, or where the lookup could not tell: one warning that names what was not checked (`not-checked`) | warning |
| Secrets (common token formats) and the project's `restricted_patterns` | error |
| The project's `banned_words` | error |

The linter does not judge content. It cannot know whether a number is right.

## 4. Post

| Task | Command |
|---|---|
| Several new issues and comments | `create_issues.py --manifest <file>` |
| One comment | `post_verified.py comment --issue N --kind <kind> --body-file <draft>` |
| A reviewed observer event, in automatic mode | `post_verified.py comment --issue N --kind <kind> --body-file <reviewed-draft> --eventkey KEY`; the exact draft and target must match the shared queue |
| Replace an issue body | `post_verified.py body --issue N --kind <kind> --body-file <draft> --expected-body-file <base> --backup <file>`; for a final record it also moves the section labels to follow the headings, and removes one only with `--allow-label-removal` |
| Convert a record body of an older form, keeping it as a comment | `convert_body.py --issue N --body-file <draft> --expected <saved live body> --note "<when and why>"` |
| A new resume comment on the hub | `resume_block.py update --file <draft>` |
| The hub's topics and the tasks as sub-issues, for a record that exists | `sub_issues.py plan`, then `sub_issues.py apply` (`--dry-run` first); a new topic or task is linked by `create_issues.py` |
| The board of the work, after a task opens or closes and after a resume block | `board.py apply` (`--dry-run` first); `board.py plan` says what differs |

`post_verified.py comment` lints a memory against GitHub, so a dangling link, a missing cited comment or a wrong evidence line stops the post; a failure of GitHub other than "Not Found" stops it too. With `--dry-run --offline` it checks against the local index instead and reads no network; without a usable index it says so in one line and gives the single `not-checked` warning. A memory of a kind that may link but carries no link gets one note before it is posted: "note: this memory links to no earlier one; ...". It is a note, not an error: add the link first if it follows from, replaces or tests an earlier memory. After a verified post of a memory it prints `memory id: <id>`, the id to link to it. `create_issues.py` lints offline first and checks each comment against GitHub just before posting it; a comment of a manifest cannot link to another comment of the same manifest, so post such a memory afterwards with `post_verified.py`.

Run each with `--dry-run` first; it prints the exact `gh` commands. Before drafting a body replacement, save the live body unchanged and edit a separate copy:

```bash
gh issue view N --repo owner/name --json body --jq .body > topic.base.md
cp topic.base.md topic.draft.md
# Edit topic.draft.md, leaving topic.base.md unchanged.
python3 scripts/post_verified.py body --repo owner/name --issue N --kind final-record \
  --body-file topic.draft.md --expected-body-file topic.base.md --backup topic.before.md --dry-run
```

Run the same command without `--dry-run` after checking the diff. A real replacement requires `--expected-body-file`; its text must match the live body, ignoring the same line-ending and trailing-whitespace differences as read-back. The script checks again before saving the backup and writing. If either check fails, read the latest body, merge your changes into it, and prepare a new base and draft. Never just replace the base to force an older draft through.

A dry run without a base can still preview the diff, but says that a real replacement needs one. An offline dry run checks no live body. A completed retry whose draft already matches the live body is a no-op; if section labels remain to be updated after a partial failure, it checks the body again before changing them.

These checks do not make GitHub issue updates atomic. Coordinate one writer per issue; another writer can still edit between the final read and write. The backup helps recovery, and read-back proves that the draft was stored, not that nobody else's simultaneous change was lost.

## 5. Read back and compare

After each write the scripts fetch the stored text from the API and compare it with the draft, ignoring only line-ending style and trailing whitespace. `verified:` means equal. `MISMATCH:` shows the first differing line; the usual causes are a body changed by someone else in between, or text altered on the way in. Resolve it before going on, and do not report the post as done until it verifies.

If you ever post by hand, do the same check by hand:

```bash
gh issue comment N --body-file draft.md          # prints the comment URL, ending in the comment id
gh api repos/owner/name/issues/comments/<id> --jq .body > back.md
diff <(sed 's/[[:space:]]*$//' draft.md) <(sed 's/\r$//; s/[[:space:]]*$//' back.md)
```

## What the scripts never do

They never delete an issue, comment or label, change no label on an issue except section labels that follow its headings, never edit a posted comment, never close or reopen issues, and never post a file that was not named on the command line or in the manifest. Closing a finished task is a deliberate `gh issue close N` by the session or the owner.

## The same commands, a local record

`post_verified.py`, `create_issues.py`, `convert_body.py`, `resume_block.py` and `setup_labels.py` post to either record. A dry run for a local record prints the same steps in plain words (`local record: post an entry on #3`) where a GitHub record prints the `gh` command; the read back compares the stored file with the draft. A posted entry's address is printed as `#N/ID`.

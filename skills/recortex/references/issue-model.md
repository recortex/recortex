# The issue model

One model serves every project type. Profiles (see [profiles.md](profiles.md)) only change the label set, the section headings and what counts as an event.

The model rests on one split. The body of a topic issue that carries a deliverable-section label is the text for that section of the deliverable: the current version only, in the deliverable's voice, ready to copy. The comments are the history. Whoever assembles the deliverable reads bodies only; whoever wants to know how the text came to be reads comments.

## Contents

- The hub
- Final-record issues
- Events
- The plan of the deliverable
- Changing the deliverable text
- Active tasks
- Labels
- Pull requests
- What goes where

## The hub

One issue, pinned, label `hub`. It is the single entry point: a reader who knows nothing opens the hub and can reach everything else. Template: `templates/hub.md`. Its sections, in order:

| Section | Holds |
|---|---|
| Deliverable text | The rule that bodies with a section label are deliverable text and comments are history |
| Resume here | A fixed note saying that the current resume block is the newest "Resume block" comment on the hub |
| Project | A short statement of the project and the tracker's time convention |
| Map | A Mermaid flowchart: deliverable sections, the topic issues that feed them, the pull requests behind the topics |
| Deliverable sections | One table per deliverable listing, section by section in the deliverable's order, the issues whose bodies hold its text |
| Active issues | The open `active` issues, each with its destination topic |
| Pull requests | Each pull request, when it merged, what it changed, its topic |
| Labels | The label legend |
| Older tracker | One row per item of an older tracker and its new home, or "None" |
| Recording rules | The rules for recording new work |

The resume block itself is not in the body. It is a comment on the hub, posted anew at the end of every working stretch from `templates/resume-block.md`: in short (two short plain paragraphs that say where the project stands and what changed lately, at most 1,200 characters, no issue numbers), last updated, current focus, active issues, last decisions, next actions with owners, holds. Comments have one author each and are never edited here, which is what lets the hook check who wrote the block before showing it (see [session-protocol.md](session-protocol.md)).

The hub holds pointers, not content. It repeats no number from a body: a number stated in two places drifts, so each number lives in one body, next to its source. The hub is edited in place when the list of issues per section or the list of active issues changes.

Numbers from different runs must never be combined. Each body's "Sources" section names the file, table, commit or tag behind each number, and before a deliverable is assembled one consistency pass over all bodies checks that the same quantity has the same value, denominator and source everywhere.

## Final-record issues

One issue per topic, label `final-record` plus section and topic labels. A topic is a unit that the deliverable will describe: a method, an analysis, a component, a figure, a claim. Template: `templates/final-record.md`. The body holds, in this order:

| Part | Holds |
|---|---|
| Title line | `# ` and the topic as the deliverable names it |
| Role sentence | One sentence: this body is deliverable text, current version only; the comments are history |
| One `## ` section per deliverable section | The text for that section, under the heading the profile defines (for example `## Methods` for `ms:methods`). The issue carries exactly the section labels of these headings |
| Sources | The file, table, commit or tag behind each number and claim. Commits and tags appear only here; resource releases, such as a reference build or a database release, may be named here |

What the text may and may not hold:

| Allowed | Not allowed |
|---|---|
| The current statement, in the deliverable's voice | Dates, "As of" lines, version history or version labels of the work itself |
| Every number with its denominator | Folded "Replaced on" blocks or any other old text |
| A design choice with a one-sentence reason | Decision tables or the story of a decision |
| A limitation stated as a property of the final method or result | The story of a failed attempt |
| Sub-headings (`###`) inside a section, not named like a section heading | Open work, next steps, provenance, links to older trackers, job identifiers |
| A figure block: `### Figure: <id> - <title>`, a caption, and the image embedded from a committed path at a commit or tag (`![text](https://github.com/<owner>/<repo>/blob/<commit or tag>/<path>?raw=true)`), with a Sources row `Figure <id>` naming the data file and the exact command | An image linked from a branch, which can change under the text |
| A table block: `### Table: <id> - <title>`, a caption, and a Markdown table or a link to a committed file, with a Sources row `Table <id>` | A table whose numbers have no source |

The linter enforces what it can see: no "As of" line followed by a date, month or year, no date and time in the text, no `<details>` block, no heading for decisions, open work, provenance, next steps or the other sections of the older record form, no dated heading outside "Sources", "Sources" last, no line that looks like a section heading but is not one, and section headings that agree with the issue's labels. It cannot see a job identifier, a missing denominator or a commit outside "Sources"; review catches those.

The mind page draws the body as it is written: `###` sub-headings inside a section, nested and numbered lists, bold and italic, pipe tables, HTML tables in the subset the page reads (`table`, `thead`, `tbody`, `tfoot`, `tr`, `th`, `td`, `caption`, `rowspan`, `colspan`, `align`, and the inline tags `b`, `strong`, `i`, `em`, `code`, `sup`, `sub`), figure and table blocks where they stand, and formulas in common LaTeX between `$` or `$$` (`references/mind.md`, "How the text is drawn"). The linter does not check any of this; `compile.py` passes the Markdown through unchanged.

In a project whose profiles define no section labels, the body is still the current text of the topic and nothing else, under plain `## ` headings of your choice, followed by "Sources".

The test for a body: someone who has read nothing else can paste it into the deliverable, and it is right. If the text says "as discussed", "see chat" or "as of", it fails the test.

A body is at most 65,536 characters. A deliverable section that needs more is split into two topics.

## Events

An event is something that happened and will matter later. Each one becomes a dated comment on its topic issue, never a new top-level issue and never text in a body. Ten kinds of memory, and two typed comments that are not memories (the triage comment and the proposal), each with a template:

| Kind | When | Template |
|---|---|---|
| Result | An experiment, analysis, benchmark or test campaign finished | `templates/comment-result.md` |
| Decision | Someone with authority chose something | `templates/comment-decision.md` |
| Failure | A run, approach or change failed | `templates/comment-failure.md` |
| Checkpoint | A working stretch ends with work in flight | `templates/comment-checkpoint.md` |
| Correction | A posted statement or number was wrong | `templates/comment-correction.md` |
| Idea | A thought raised inside a thread's work that might help, not yet tried | `templates/comment-idea.md` |
| Question | A question raised inside a thread's work, not yet answered | `templates/comment-question.md` |
| Plan | A part of the plan of the deliverable: what the paper will claim, show or concede ([The plan of the deliverable](#the-plan-of-the-deliverable)) | `templates/comment-plan.md` |
| Text | The text of a topic body changed, by the plugin or by hand | `templates/comment-text.md` |
| Steps | The user agreed to a plan with numbered steps, on an `active` task ([Tasks, steps and marks](#tasks-steps-and-marks)) | `templates/comment-steps.md` |
| Triage | An issue opened by hand was given a type and a route | `templates/comment-triage.md` |
| Proposal | The plugin proposed a change of the text and waits for a merge on GitHub (not a memory; the anchor of an approval) | `templates/comment-proposal.md` |

Every event comment starts with a machine-readable marker line, then its heading:

```
<!-- ghim:event type=result at=2026-03-04T14:10-08:00 -->
### Result: 2026-03-04 14:10 PST - first speed benchmark
```

The heading keeps the comment list of an issue readable as a timeline; the marker gives the type and the time with its offset, so the index and `compile.py` can order every event of the project exactly. Both carry the same local date and time, and the linter checks that they agree. The comment that keeps a former body during a conversion carries `type=record`. Resume comments keep their own heading first and have no marker, because the trust check reads that heading. A merged change is recorded as a result comment whose "What ran" names the pull request and commit.

Comments are append-only. They are the history: results, decisions with the decider's exact words, failures, corrections. The decision record lives only in decision comments. Nobody rewrites them, and none of them is deliverable text. A memory edited on GitHub after it was posted is no longer told (in `query recall` and on the mind page) and no new memory may link to it, unless only your allowed accounts edited it, which the sync checks and the page notes; post a correction instead ([mind.md](mind.md), "Edited memories").

A memory is written in a fixed order: marker, heading, the plain line, the kind's fields, `**Evidence:**` when there is something to show (never on a plan or a text change), then the folds, then nothing. A memory recorded before 0.6.0 has no plain line and no links; it stays as it is.

### The plain line

Directly under the heading, one line in plain words that completes a fixed label. It is the first field of every kind:

| Kind | The line starts with | And is |
|---|---|---|
| Result | `**In plain words, we found that:**` | a statement |
| Decision | `**In plain words, we decided to:**` | a verb phrase |
| Failure | `**In plain words, what went wrong:**` | a statement |
| Idea | `**In plain words, we had the idea to:**` | a verb phrase |
| Question | `**In plain words, we asked:**` | a clause that starts with whether, why, how, what, which, who, when, where or if |
| Correction | `**In plain words, we corrected ourselves:**` | a statement |
| Checkpoint | `**In plain words, we paused here:**` | a statement of where things stand |
| Plan | `**In plain words, we plan to:**` | a verb phrase |
| Text | `**In plain words, we changed the text to say that:**` | a statement |
| Steps | `**In plain words, we agreed to:**` | a verb phrase |

The linter refuses a plain line that is missing, longer than one line, under 20 or over 200 characters, ends with a period, holds a backtick, an issue number (`#N`), a URL, a path or file name, or a commit hash, repeats its label ("we decided to we decided"), or holds a number that stands in no field of the same memory, neither as written nor rounded ("about 3 times" stands for a field's 3.05, "2 million" for 2,000,000). The heading, the marker and the folds do not count as fields: put the number in a field, with its source, or leave it out of the line. The line is a summary written for someone who was away for a month; the fields stay the record, and in a decision the quote stays the decision.

### Links between memories

A memory may say, in its marker, which earlier memories it follows from, replaces, was done instead of, or puts to the test:

```
<!-- ghim:event type=decision at=2026-03-07T09:30-08:00 because=9104 instead=9103 -->
```

The value is one to four comment ids separated by commas: the number after `#issuecomment-` in a memory's address, which `post_verified.py` prints as `memory id:` and `index.py query recall --issue N` lists. A link points back in time, to a memory of the same repository. For each candidate in the recall output, ask the question of each kind:

| Key | Ask | Read as | Effect on the earlier memory |
|---|---|---|---|
| `because` | Did this happen as a consequence of that? | "That, so this." | none; an idea that a decision or a result points at with `because` is taken up |
| `replaces` | Did that once stand, and does this now stand in its place? | "Later that changed: this." | replaced |
| `instead` | Was that an attempt or an idea that never held, and is this what we did in its place? | "That was set aside. Instead, this." | set aside |
| `checks` | Does this put that question, idea or decision to the test? | "The answer came later" (question), "We tried it" (idea), "We checked it" (decision) | a question answered, an idea tried |
| `against` | Does this finding speak against that planned claim? | "A later finding spoke against it." | the planned part is in doubt |

Who may link to what (`LINK_RULES` in `scripts/record_format.py`, used by the linter and by every reader):

| Key | The memory that carries it | May point at |
|---|---|---|
| `because` | result, decision, failure, correction, idea, question | any of those six kinds |
| `replaces` | any of those six kinds | a memory of the same kind; a correction may point at any of the six |
| `instead` | decision, idea, result | idea, decision |
| `checks` | result, failure | question, idea, decision |
| `checks` | decision | question |
| `because` | plan | result, decision, failure, correction, idea, question, plan |
| `because` | result, decision, failure, correction, idea, question | plan |
| `because` | text | result, decision, correction, plan |
| `replaces` | plan | plan |
| `replaces` | steps | steps |
| `instead` | decision, plan | plan |
| `checks` | result, failure | plan |
| `against` | result | plan |

An idea has one of four states from the links that point at it, the first that applies: set aside (`instead`), tried (`checks`, a result or a failure put it to a test), taken up (a decision or a result made `because` of it, and no result has tested the idea itself), else not tested yet. `index.py query recall` prints `taken up` in its state column and leaves such an idea out of its list of untried ideas in other threads. The state is worked out when the index is read: an older version of the plugin on the same index calls the same idea untried.

A result never replaces a decision: it `checks` it, and a new decision replaces it. A change of the text (`text`) carries `because` and nothing links to it. A correction never `replaces` a plan memory: a plan is revised, not corrected. A failure that `checks` a planned claim does not put it in doubt: not tested is not negative. A checkpoint carries no link and is never a target; triage and record comments carry none; a reconstructed memory carries none in this version. A key appears once, and an id stands under one key. The marker accepts only `type`, `at`, `recorded`, `source`, `eventkey` (32 lowercase hexadecimal characters, written by the observer's publisher), the five link keys and the two mark keys (`done`, `dropped`; see [Tasks, steps and marks](#tasks-steps-and-marks)), so a misspelled key is an error. Add a link only when its sentence read aloud is true; link to the nearest cause, not to its ancestors. Most memories have one link, and none is better than a guess: a posted link cannot be edited away (a correction can replace the memory that carried a wrong one).

When it can ask the record (`post_verified.py`, `lint_drafts.py --index` or `--online --issue N`), the linter checks each target: it exists in this repository, it is a memory, it is not later than the draft, the rules above allow it, and, when the repository is in your trusted list, it was written by an allowed account. A memory by any other account is not told on the mind page, so a link to it would lead nowhere; if that account belongs to the project, add its login to the repository's line in your trusted list.

### Tasks, steps and marks

A task is an `active` issue. Its steps are a `steps` memory on it: the plain line, **Agreed** (how the user agreed: `Approved in the plan dialog, <date time zone>.`, or `By <name or role> in the chat, <date time zone>:` and the user's words quoted), **Steps** (a numbered list of 1 to 20 one-line steps) and **Done when**. A later `steps` memory on the same task adds steps, and one with `replaces=` (a `steps` memory of the same task) restates the list it replaces and takes its place in the numbering. The steps of a task are numbered 1 to N across its standing `steps` memories, oldest first; a `steps` memory that is not told, or that a later one replaces, gives no steps. One standing task per project, titled "To do", holds the loose to-do items: each is a `steps` memory of one step.

A step is checked by a mark in the marker of a later memory: `done=<steps comment id>:<n>[,<n>...]` on a result, a failure, a correction or a checkpoint (the agent reported the step finished), and `dropped=<...>` on a decision (the user dropped it). One to four groups, joined by `;`, with step numbers from 1 to 20 (`done=9108:1,2;9112:3`). A mark is not a link: a checkpoint still carries none. A mark counts only when the memory that carries it is told and not replaced or set aside, and the steps memory it points at is told, not replaced, and has that step; otherwise it counts for nothing, and `index.py query consistency` reports it (`mark-target`). A step is then, by the first rule that applies: dropped, done, blocked (a failure names the task and nothing followed) or open.

A task's body shows its steps as a task list under `## Plan`: `- [ ] <n>. <step>` or `- [x] <n>. <step>`, written by `tasks.py render` from the memories and never believed the other way: a box ticked or unticked on GitHub is not a mark. The line `<!-- ghim:list sha256 ... -->` after the list is the plugin's own mark of what it last wrote, so that a list changed by hand is told from one that is only behind.

### Evidence

Optional, on a result, decision, failure or correction, as the last field: up to six lines in four forms, and nothing else.

| Line | Means |
|---|---|
| `- table <id>` | a table block of this thread's topic body |
| `- figure <id>` | a figure block of this thread's topic body |
| `- code <path>#L<a>-L<b> at <commit>` | lines of a file at a commit (`#L<a>` for one line; at most 120 lines) |
| `- pull <n>` | a pull request |

`code` and `pull` may end with ` in <owner>/<name>` when the code lives in another repository; that name is recorded and not checked. A commit is 7 to 40 hexadecimal digits, never a tag or a branch, which could change. With the record at hand the linter checks that a table or figure is a block of the topic body and that a pull request exists and is one; code lines are checked for their form only. Copy each line from `git` output, never from memory: no line is better than a guessed one.

### Folds: explanations kept inside the memory

A word that cannot be understood without this project is explained in the memory that first uses it, in a fold at its end; a thread's opening, what the thread is about, goes into the first memory recorded on it from 0.6.0 on. Nothing else is posted for them. On GitHub a fold shows as one closed line that opens on a click:

```
<details>
<summary>We use "line splitter" to mean the first version of octo-repo's reader, which cut the file into lines before looking at quotes</summary>

**Why it matters here:** Its failures are why the reader was rewritten.

**Also called:** the splitter

**The longer story:**

octo-repo began as a few lines of code: split the file at every line break, then split each line at every comma.

</details>
```

The summary is one of `We use "<word>" to mean <meaning>`, `We now use "<word>" to mean <meaning>`, `This thread is about <meaning>` and `This thread is now about <meaning>`. A blank line follows it. Then `**Why it matters here:**` for a word or `**Why we care:**` for a thread, one paragraph of at most 400 characters; for a word optionally `**Also called:**` with up to five other names; optionally `**The longer story:**`, a blank line and paragraphs of plain text, at most 2,500 characters; then `</details>`.

- The word: 1 to 60 characters, starting with a letter or digit, without `"`, `<`, `>`, `&` or a backtick. The meaning is the line shown on hover in the mind page: 20 to 200 characters, one sentence without its period, with the plain line's bans (no backtick, `#N`, URL, path or commit) and no `<`, `>` or `&`.
- Inside a fold: no heading, no line that starts with `<` other than its own lines, no code fence, no field of the memory. Citations may stand in "Why it matters here", "Why we care" and the longer story, never in the summary.
- At most three word folds and one thread fold in a memory; a word is explained once in it, and it is used in the memory outside its own fold, under its name or one of its other names.
- A word is known by its key: lowercase, inner spaces collapsed, a leading "the", "a" or "an" dropped. The newest explanation of a key by an allowed account stands; earlier ones are what we meant before. A later memory that uses the word with a changed meaning writes `We now use`; `We use` for a word already explained is an error that names the memory, and `We now use` for a word never explained is an error. The same holds for `This thread is about` and `This thread is now about`. An explanation stands whatever becomes of the memory that holds it.
- A thread fold needs an issue with at most one `topic:` label, or it could not say which thread it opens.

Which words: only the project's own names for its test files, settings, earlier versions and internal parts, its nicknames and abbreviations, and ordinary words it uses with its own meaning. A general word of the field (streaming, delimiter, state machine) is never explained, however technical. Explain the word, not the project.

### Citations

`#N` for an issue of this repository and `https://github.com/<owner>/<name>/issues/<n>#issuecomment-<id>` for one memory may stand in every field and in a longer story, and GitHub links them. They never stand in the plain line or in a summary. `#N` is checked as before (`bare-ref`); a comment address of this repository is checked like a link target: the comment must exist on that issue.

### A worked chain

`examples/octo-repo/chain/` holds five memories posted one at a time on the reader topic, in this order:

1. An idea: keep the current reader and remember across lines whether a quoted field is open (9103).
2. A failure that `checks=9103`: the patch broke files with mixed line endings (9104).
3. A decision, `because=9104 instead=9103`: rewrite the reader to read one character at a time. It explains "line splitter" in a fold and lists a table, code at a commit and a pull request as Evidence (9105).
4. A question, `because=9105`: whether the new reader still works on a pipe (9106).
5. A decision that `replaces=9102`, the earlier decision on strict quoting, and explains the word again with "We now use" (9107).

`index.py query recall --issue 5` then shows the idea as set aside, the question as open and the first decision as replaced.

## The plan of the deliverable

A project that writes a paper (a profile with a `plan` key; only `manuscript` has one in 1.3.0) keeps the plan of the paper in the record: what it will claim, which figures and tables are worth making, what the discussion will argue and concede, and what is still missing. The plan issue carries the label `plan`, is a sub-issue of the hub, and has no section label; its number is in the project file (`"plans": {"manuscript": 12}`). Its comments are the planned parts, one dated comment of the kind `plan` each; its body is a readable copy of them that `plan.py render` writes again when they change. How the session plans is in [planning.md](planning.md).

A planned part has a **Part** (message, claim, figure, table, interpretation, limitation, comparison or implication), the **Section** it belongs to (or All, for the message), the **Topic** issue that holds its text (or None yet), an **Id** for a figure or a table, the **Statement** as the paper would make it, what **It needs**, what **We have**, what the paper says **If it does not hold**, and, on a revision, a **Change** (narrows, rewords, widens or reverses). `templates/comment-plan.md` has the form; the profile's part list and headings decide what the linter accepts.

The state of a part is computed from the links, never typed (`plan_rules.py`). A part is *agreed* when a told decision is linked to it with `because`, in either direction, or when it narrows or rewords an agreed part; otherwise it was *added by the session*. Then, the first that applies:

| State | When | Shown as |
|---|---|---|
| dropped | an `instead` points at it | Dropped on (day) |
| revised | a plan `replaces` it | Revised on (day) |
| in doubt | a standing result links it with `against` | A finding speaks against it |
| moved | a memory it rests on was replaced, corrected, set aside, or may no longer be told | What it rested on has changed |
| waits | it links a planned part that is neither backed, in the text nor standing | Waits for (the part) |
| in the text | a display part whose id is a block in a topic body; or a part that a later `text` comment links | In the text since (day) |
| can be made | a figure or table whose claims are backed | Can be made now |
| backed | a claim with a standing result linked (`because` from the claim, `checks` to it) | Backed by N findings |
| attempt failed | a claim that only a failure `checks` | An attempt to show it failed |
| planned | a claim, figure or table with none of the above | Planned; nothing backs it yet |
| stands | any other part | no chip |

"Backed" means a standing finding is linked. It can still turn out wrong. The author approves the first plan once; after that the session keeps the plan current and marks what it adds "added by the session". It asks before changing the paper's message and before widening, reversing or dropping a claim the author agreed.

A change of the text is a `text` comment: it says which section changed, what and why, how it was approved (one of five fixed forms), what the body was and became (two SHA-256 values), and which version holds it. [writing.md](writing.md) has the forms.

## Changing the deliverable text

The body always says what the deliverable says now. When that changes:

1. Edit the body in place. The new text replaces the old; nothing old is folded or kept below it.
2. A result that leaves the deliverable is deleted from the body. When a section has no text left, delete its heading; its section label comes off. When a whole topic leaves the deliverable, replace its body with one line saying that the topic is not part of the deliverable and why, pointing to the dated comment that records the reason (`post_verified.py body --issue N --kind generic --body-file <line> --expected-body-file <base> --backup <file>`, since a final record needs a section), remove its section labels and `final-record` with `gh issue edit N --remove-label <label>`, remove its rows from the hub, and close it as not planned (`gh issue close N --reason "not planned"`), or label it `active` if work on it continues.
3. Post the result, decision or correction comment that caused the change. The comment explains why; the body shows what is true now. GitHub keeps every earlier version of the body in the issue's edit history.
4. Update the hub only if the list of issues per section changed.

`post_verified.py body --kind final-record --expected-body-file <base>` replaces the body, refusing a live body that differs from the base saved before drafting, and moves the section labels to follow the headings in one run. It refuses an issue labeled `hub` or `active` or without `final-record`, and an issue that carries section labels of profiles not in use (pass `--profile`, or `--allow-other-profiles` to leave those labels alone). It removes a section label only with `--allow-label-removal`, after printing `removes: <labels>`: a heading mistyped as `### Results` or `##Results` would otherwise drop a section silently, and the linter reports such near-headings as errors. When no profile defines section labels it changes no label and says so.

## Active tasks

Work in development (an investigation, a refactor, a rerun, a method not yet merged) gets its own issue with label `active`, and so do open questions: one issue per bounded task, or one decision list for the owner. Template: `templates/active-task.md`. It must name its destination topic and never carries a section label. Checkpoint comments go on the task while it runs. When it ends:

1. Post the outcome as a result (or failure) comment on the destination topic. If the outcome was adopted into the product (merged, integrated, released), edit the destination body to say what the deliverable now says; until then it stays out of the body.
2. Write the outcome and the link to that comment into the task's "Result" section.
3. Close the task. The `active` label stays, so closed tasks remain findable; "open and labeled `active`" is the list of work in flight.
4. Post a new resume block.

A task that ends with nothing to report still gets a result: "no change, because ...".

On github.com each topic is a sub-issue of the hub and each task a sub-issue of its destination topic (`create_issues.py` links them; `sub_issues.py` fills them in for an existing record), and a private board can show where each task stands, computed from the memories on it: a failure that blocks a task names the task (`#N`) in one of its fields, and a hold in the resume block names the issue it holds, so that the board can show Blocked and Waiting for a yes. See [board.md](board.md).

## Labels

| Group | Labels | Use |
|---|---|---|
| Role | `hub`, `final-record`, `active`, `plan`, `triage`, `needs-decision`, `migrated` | What kind of issue this is; `triage` marks an issue opened by hand that is not yet placed |
| Type | `type:task`, `type:bug`, `type:question`, `type:docs`, `type:idea`, `type:data`, `type:decision-needed`, `type:duplicate`, `type:out-of-scope`, plus the profile's own | What an issue opened by hand is (see [triage.md](triage.md)) |
| Deliverable section | From the profile, for example `ms:methods` or `rel:fixed` | Which part of the deliverable the issue's body is the text of; follows the body's `## ` headings |
| Topic | `topic:<name>` | Which topic the issue or pull request belongs to |

Every issue and pull request gets its labels when it is opened. An unlabeled item is invisible to the queries the session protocol runs.

## Pull requests

Every pull request names its topic issue in its description ("Topic: #12"), carries the topic label, and gets a row in the hub's pull-request table when it merges. The merge date is written in the tracker's time convention, converted from the UTC time GitHub reports.

## What goes where

| You have | It goes |
|---|---|
| A finished measurement | Result comment on the topic; then the body, with its source under "Sources", if the deliverable now says something new |
| The owner said yes, no or "do it this way" | Decision comment with their exact words; in the body only what the deliverable now says, with a one-sentence reason |
| A crash, a dead end, a rejected idea | Failure comment; in the body only a limitation of the final method or result that follows from it |
| Work in development or an open question | An `active` issue that names its destination topic |
| A thought or a question that comes up in the middle of a thread's work | An idea or question memory on that thread; a later result may `checks` it. Open work and the owner's decision list stay `active` issues |
| A word that cannot be understood without this project | A fold in the memory that first uses it; afterwards it is used as written there |
| A claim the paper will make, a figure it needs, what it will concede | A `plan` comment on the plan issue; the state follows from the links ([The plan of the deliverable](#the-plan-of-the-deliverable)) |
| The text of a topic body changed | A `text` comment on that topic, written by whoever changed the text ([writing.md](writing.md)) |
| A version worth a name | The author asks in the chat; a decision on the plan issue names it ([versions.md](versions.md)) |
| A plan for the next hours | A new resume comment on the hub and, if work is in flight, a checkpoint comment |
| A doubt about an earlier number | An `active` issue until checked; then a correction comment, and the body edited if the number was wrong |
| A file, table, commit or tag behind a number | "Sources" of the body |
| A job identifier, log path or older-tracker link | The comment that reports the event |

## In a local record

The hub is `hub.md`, a topic is `topics/<slug>.md`, and each topic has a folder of the same name for its entries and for the earlier texts of its body. A label is a line of the topic's front matter, a closed topic has its reason and time there, and an entry is one file whose name carries its time and id. The address of a topic is `#N` and of an entry `#N/ID`. [local-record.md](local-record.md) has the layout and the seal.

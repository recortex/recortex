# Templates and how to fill them

All templates are in `templates/`. Fields to fill are written `{{like this}}`; the linter rejects a draft that still contains one. Where a field does not apply, write "None" and the reason. The optional parts are the exception: the link keys of the marker, the `**Evidence:**` field and the folds are left out when there is nothing to put in them, and "None" under Evidence is an error. An empty section is an error, because a reader cannot tell "nothing to report" from "forgotten". The one exception is the body of a final record: a deliverable section the topic does not feed is left out, with its label.

| Template | Kind for the linter | Use |
|---|---|---|
| `hub.md` | `hub` | The hub issue |
| `final-record.md` | `final-record` | One per topic: the deliverable's text for the topic's sections, then Sources |
| `active-task.md` | `active` | One per bounded task |
| `comment-result.md` | `result` | Finished experiment, analysis, benchmark, merged change |
| `comment-decision.md` | `decision` | A decision, quoted word for word |
| `comment-failure.md` | `failure` | A failed run, approach or change |
| `comment-checkpoint.md` | `checkpoint` | State of work in flight at the end of a stretch |
| `comment-correction.md` | `correction` | A posted statement or number was wrong |
| `comment-idea.md` | `idea` | A thought raised inside a thread's work that might help, not yet tried |
| `comment-question.md` | `question` | A question raised inside a thread's work, not yet answered |
| `comment-plan.md` | `plan` | A part of the plan of the paper: a claim, a figure, a limitation and so on |
| `comment-text.md` | `text` | The text of a topic body changed: what, why, how it was approved, which version holds it |
| `comment-steps.md` | `steps` | The user agreed to a plan with numbered steps, on an `active` task |
| `comment-proposal.md` | `proposal` | The plugin proposed a change of the text and waits for a merge on GitHub |
| `plan-body.md` | `plan-body` | The readable copy of the plan, in the body of the plan issue (written by `plan.py`) |
| `comment-triage.md` | `triage` | An issue opened by hand: its type, route and what waits for confirmation |
| `recortex-triage.yml` | | The optional GitHub Actions workflow that labels issues opened without a role label |
| `resume-block.md` | `resume` | A resume comment on the hub: heading, markers and the block |
| `pointer-comment.md` | `pointer` | The short note left in an older tracker |
| `migration-map.md` | `generic` | The survey table of a migration |
| `manifest.example.json` | | The list of drafts for `create_issues.py` |
| `recortex.example.json` | | The project configuration |
| `claude-md-snippet.md` | | Lines for the project's CLAUDE.md |
| `settings-hook-snippet.json` | | The SessionStart hook for a settings file |

## Field notes

**Event marker.** The first line of every event comment: `<!-- ghim:event type=<type> at=<YYYY-MM-DDTHH:MM+HH:MM> -->`, with the type in lower case (`result`, `decision`, `failure`, `checkpoint`, `correction`, `idea`, `question`, `plan`, `text`, `proposal`, `steps`) and the same local date and time as the heading, followed by its UTC offset (`date '+%Y-%m-%dT%H:%M%:z'`). The linter rejects a comment whose marker is missing or disagrees with its heading. The prefix in the marker is part of the record format and is never changed, so that comments posted by every version are one format.

**The plain line.** The first field of every memory, directly under the heading: one line that completes the label of its kind, written for someone who was away for a month. Result: "In plain words, we found that: ..." (a statement); decision: "we decided to: ..." (a verb phrase); failure: "what went wrong: ..."; idea: "we had the idea to: ..." (a verb phrase); question: "we asked: ..." (a clause that starts with whether, why, how, what, which, who, when, where or if); correction: "we corrected ourselves: ..."; checkpoint: "we paused here: ..." (where things stand); plan: "we plan to: ..." (a verb phrase); text: "we changed the text to say that: ..." (a statement); steps: "we agreed to: ..." (a verb phrase). 20 to 200 characters, no period at the end, no backtick, issue number, URL, path, file name or commit, and the label not repeated. Every number in it must also stand in a field of the memory, as written or as the number it rounds; if the linter says one does not, put the number in a field with its source, or leave it out of the line. Do not count on the heading or a fold to carry it. A plain line is a summary, not the record: a decision's quote stays the decision.

**Links.** Optional keys in the marker line, after `at=`: `because=`, `replaces=`, `instead=`, `checks=` and `against=`, each with one to four comment ids separated by commas (`because=9104` or `because=9104,9105`). The table of who may link to what, and the question to ask for each kind, is in [issue-model.md](issue-model.md), "Links between memories". Take ids only from `index.py query recall --issue N` or from the `memory id:` line `post_verified.py` printed in this session. A checkpoint, a triage comment and a reconstructed memory carry none.

**Evidence.** Optional, last field of a result, decision, failure or correction: `**Evidence:**` and up to six lines, each `- table <id>`, `- figure <id>`, `- code <path>#L<a>-L<b> at <commit>` (or `#L<a>` for one line) or `- pull <n>`; `code` and `pull` may end with ` in <owner>/<name>`. Copy the lines from `git` output (`git log -1 --format=%h`, `git blame -L`), never from memory: no line is better than a guessed one. Delete the field when there is nothing to show. The field sheet's "What counts as proof" line ([fields.md](fields.md)) is about what makes a result believable, and is not this field.

**Folds.** After the last field, a word that cannot be understood without this project is explained where the memory first uses it, and a thread's opening goes into the first memory recorded on it from 0.6.0 on, when `query recall` says "About this thread: not written yet". The exact form and its limits are in [issue-model.md](issue-model.md), "Folds". "We use" for a new word, "We now use" for a word whose meaning changed; "This thread is about" and "This thread is now about" likewise. Use a word listed under "Words explained so far" as written there.

**A planned part.** One comment of the kind `plan` on the plan issue ([planning.md](planning.md)). Part, Section and Topic are in the form the profile names; the Id of a figure or a table is the id its block will carry (`### Figure: <id> - <title>`); a statement holds a number only when a finding it links holds it. A part that stands in the paper's text for another needs `because=` to the claim it shows. A revision carries `replaces=<the part>` and says in **Change** whether it narrows, rewords, widens or reverses it; a revision that widens or reverses a claim, and the paper's message, link the decision of the author with `because=`. A planned part lists no Evidence. Use `plan.py check` on a folder of drafts, with `NEW-<file name>` where one draft links another, and `plan.py post` to post them.

**Steps and marks.** One comment of the kind `steps` on an `active` task ([issue-model.md](issue-model.md), "Tasks, steps and marks"). **Agreed** is one of two fixed forms, **Steps** a numbered list of 1 to 20 one-line steps, **Done when** one line. A step is checked by `done=<steps comment id>:<n>` in the marker of a later result, failure, correction or checkpoint, and dropped by `dropped=` in a decision's; take the ids from `tasks.py status`. `tasks.py render` keeps the task list under `## Plan` of the task's body current; write it as `- [ ] <n>. <step>`.

**A change of the text.** One comment of the kind `text` on the topic whose body changed, written by `write.py apply` or `versions.py sync` ([writing.md](writing.md)). **Approved** starts with one of five fixed forms, **Body** holds two SHA-256 values and **Version** a commit id or the reason it was not kept.

**The note at the end of a template.** The HTML comment at the end of the memory templates says which parts are optional. Delete it; the linter rejects a draft that still holds its `{{delete this note}}` field.

**Figures and tables.** In a body, a figure is a `### Figure: <id> - <title>` block inside its section: a caption with every number's denominator, then the image embedded from the commit or tag it was committed in. Its row under "Sources" starts with `Figure <id>` and gives `data` and `command` in backticks: ``| Figure read-time | data `bench/out/summary.tsv`; command `python bench/plot.py bench/out/summary.tsv`; commit 4f2a9c1 |``. A table block is the same with `### Table:` and a Markdown table or a link to a committed file. `compile.py` lists both, with their sources.

**Timestamps.** Always date, time and zone in the tracker's convention: `2026-03-04 14:10 PST`. Get it with `date '+%Y-%m-%d %H:%M %Z'`. The header of every event comment, and the heading and "Last updated" line of a resume comment, must carry one.

**Numbers.** Value, denominator, source path, every time: "41 of 53 files (`tests/conformance/report-4f2a9c1.txt`)". Open the source file and read the number from it while drafting. A percentage alone is not a number.

**What ran.** Enough to rerun or look it up: commit, command or workflow, job identifiers, output folder.

**Configuration.** Data version, code version and the settings that could change the result. If two results differ in any of these they are different runs and stay in different tables.

**Exclusions and failures.** Counts with reasons: "3 of 120 samples failed with out-of-memory, excluded". Write "None" only after checking.

**Not established.** What the result does not show. Typical entries: not tested on other inputs; cause of a failure not shown; an effect seen once; a comparison not made on the same data.

**Deliverable text.** The body of a final record is written in the deliverable's voice, as the paper, release notes or report will print it, with no "we decided", "so far" or "as of". Every number with its denominator in the sentence; its file, table, commit or tag under "Sources", and commits and tags nowhere else. One `## ` heading per deliverable section, with the heading the profile defines. A section the topic does not feed is left out, not written as "None".

**Exact words.** Copy the decider's sentence from where it was said. Keep typos. If only part is relevant, quote that part whole and mark omissions with "[...]". Never tidy up a quote. If the decision came by voice and nobody wrote it down, write "Exact words not available" and what was understood, and ask the decider to confirm in writing.

**Owner.** A name or a role, for every next step and open item. "Claude session" is a valid owner; "someone" is not.

**People.** Name or role. No pronouns inferred from names; use "they" unless the person has said otherwise.

## Placeholders for issues that do not exist yet

While drafting several issues that link to each other, write `NEW-<key>` where a number will go, with the key from the manifest (`NEW-hub`, `NEW-reader`). `create_issues.py` replaces each with `#N` in prose and with `Issue N` inside a Mermaid block, so inside Mermaid write `NEW-reader Reader`, not `Issue NEW-reader`.

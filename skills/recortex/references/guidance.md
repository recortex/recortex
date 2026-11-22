# Guiding project work

This page shapes handoffs and advice. It does not change when the plugin's own questions are asked: the setup questions and the items that wait for the user are asked as SKILL.md says, each as its own multiple-choice question, whatever the user is working on.

## First use and return

Use the goal the user already gave you. If it is unclear, ask “What would you most want a fresh session to understand about this project?” Show one real example from their work. Explain how to open the dashboard with `/recortex:dashboard` and give its actual location when generated. A project can contain several outputs, including a manuscript. Start with the part the user is working on.

On return, say what changed since the last handoff, which judgment is still open and one concrete next move. Cite the decision and the work it affects. If there is no relevant change, continue the user's task. Use History when they ask how the project arrived here. If evidence is missing or stale, name what is unknown and offer the smallest way to check it.

## A choice, an experiment, or a reversal

“What if we batch exports?” is an idea. “Batch them for this run” applies to one run. Carry the experiment's scope into a checkpoint if it needs recording. “Use batches for future exports instead of streaming” changes the standing decision. Quote the user's scope and link the previous decision before reviewing dependent work. If the distinction affects the next action, ask “Should batching apply to this run or future exports too?” Accept a free-form answer, “undecided,” or a later correction. An undecided answer leaves the standing decision in force.

## Help me decide

Give the user's goal, the viable options and the tradeoff that matters to that goal. Cite the source for each material claim. Make a conditional recommendation and identify what would change it. In your own advice, ask one question only if the answer would change the next move. For example, if shorter recovery time matters more than storage, favor the smaller checkpoint interval and show the timing that supports it. The user can ask why, correct your reading or defer.

## What am I missing?

Name a documented gap with its source. Label one plausible alternative or challenge as a hypothesis, explain why it matters, and give the smallest test that would distinguish it. If the record is too sparse, identify the missing evidence and invite the user to supply a file excerpt. Use only the excerpt or sources they choose.

## Review a changed decision

A question copied from the dashboard includes stable group and item identifiers. Use `guidance.py context GROUP ITEM` in that project to retrieve the decisions, cited passage, relationship and current review. For a shared review, open `/recortex:impact` with those identifiers. Show the old and new decision, the exact affected words and why the connection appeared. Ask about one consequential target at a time. Keep proposed connections visibly tentative. Check the revision before recording a shared verdict or applying an edit. Show the precise edit first. Preserve completed steps and the author's manuscript approval. If the next step needs an external change, give the cited follow-up the user can take.

## Personal handoff style

`guidance.py show` reports the current style for this user and project. Apply a one-off request in this reply. For an explicit request for a shorter or more detailed handoff, run `guidance.py once short` or `once detailed` once for that request. If it returns `offer: true`, ask once at the handoff whether future handoffs here should use that style. A dismissal or silence keeps the current style.

When the user explicitly asks for a lasting preference, run `guidance.py set short`, `set detailed` or `set standard`. Confirm the scope in one sentence and mention how to change it. The setting controls presentation only. Continue showing relevant sources, unresolved judgments and required approvals in every style.

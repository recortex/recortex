---
description: Browse and maintain this project's shared map
argument-hint: "[change or question]"
disable-model-invocation: true
---

Load the recortex skill first; it gives the scripts folder. Treat $ARGUMENTS as the user's request. Read the accepted map with
`project_map_io.py show`. For browsing, open the dashboard with `mind.py --open` and give its Project Map link.

For an empty map, build the whole tree as one `seed` change, with `"action": "seed"` and `branches`, a list where each branch has a
`title` and an `id` of your choice (letters, digits, `-` and `_`) so that children can name their parent, and optional `parent`
(`null` or the `id` of an earlier branch of the list), `description`, `evidence` and `links` (any branch of the list). Send it to
`project_map_io.py preview --change-file -` once, show every proposed branch with its parent and evidence, and after the user's
yes run `project_map_io.py apply ID --yes` once: one hub comment holds the whole first tree, up to 200 branches. A seed is for a
map with no branch at all, and made by a user; later edits stay one change at a time. If a collaborator still has 1.6.0, tell
the user to update first (see `references/upgrading.md`).

For an empty project, ask for its goal and main areas of work. Offer the trusted topics and decisions already in
the record as a starting point. Run `setup.py map-files` to list possible sources. Show the selected paths before
running `setup.py map-files --select PATH [--select PATH ...] --yes` or `setup.py map-files --clear --yes`.
Use `setup.py map-files --outline` to read headings from approved files. Share only reviewed labels and references.
A project can start with a branch taken directly from its owner's description.

For a correction, use stable node IDs from the map. Send a JSON change to
`project_map_io.py preview --change-file -` on standard input. The change includes an `action` and a node `id`.
For `add`, supply `title` and optional `parent` (null means the project), `description`, `evidence`, and `links`.
Use `rename` with `title`, `update` with details, `move` with `parent`, or `merge` with a target ID in `into`.
A `split` keeps the original as the parent and takes two or more `branches` with a title and the existing child IDs
to move into each. For `delete`, set `children` to `keep` to promote children or `subtree` to hide them together.
A `restore` restores the nodes hidden together. Children promoted or transferred by an earlier change keep their
current location. A merge also transfers evidence and crosslinks to its target.

Show the exact change and affected branches. After confirmation, run `project_map_io.py apply ID --yes` using the
preview's operation ID. If the result is uncertain, retry the same ID to reconcile. If another user changed the
branch, show a fresh preview. Once accepted, run `index.py sync` and regenerate the dashboard with `mind.py --open`.

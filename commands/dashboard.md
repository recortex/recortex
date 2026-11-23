---
description: Open this project's live dashboard
argument-hint: "[suggestions]"
disable-model-invocation: true
---

Load the recortex skill first; it gives the scripts folder. Run `dashboard_action.py open` from the configured project (with `--suggestions` when the user asks for observer suggestions, including `$ARGUMENTS` equal to `suggestions`).

If it opened the browser, say so and give the view-only link it printed. If it printed a link and no browser opened, give that link and say that the line recortex showed above your reply holds their private link with changes on. Never read the server's files, never call the service yourself, and never try to find the other link: the user's own confirmation in the page is what makes a change.

If it printed a question after the link (this machine's browser may not reach the link), give the link first and then ask that question with the question tool, word for word, with its options exactly. Never tell the user to forward a port. If it says the live dashboard is switched off, run `mind.py --open`; if that reports a missing or outdated index, run `index.py sync` and retry once. If the output of `mind.py --open` says "this machine has no browser", your whole reply is that one line and the one question the script printed, asked the same way.

For any other failure, tell the user what the script reported.

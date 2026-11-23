---
description: Open this project's live dashboard
argument-hint: "[suggestions]"
disable-model-invocation: true
---

Load the recortex skill first; it gives the scripts folder. Run `dashboard_action.py open` from the configured project (with `--suggestions` when the user asks for observer suggestions, including `$ARGUMENTS` equal to `suggestions`).

If it opened the browser, say so and give the view-only link it printed. If it printed a link and no browser opened, give that link and say that the line recortex showed above your reply holds their private link with changes on. Never read the server's files, never call the service yourself, and never try to find the other link: the user's own confirmation in the page is what makes a change.

If it says the live dashboard cannot be reached from the user's browser, run `mind.py --open`. If that reports a missing or outdated index, run `index.py sync` and retry once. If its output says "this machine has no browser", your whole reply is that one line and the one question the script printed, asked with the question tool, word for word. Give no address, no file size and no explanation of the page. Never tell the user to forward a port.

For any other failure, tell the user what the script reported.

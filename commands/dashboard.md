---
description: Open this project's dashboard and show its local address
argument-hint: "[suggestions]"
disable-model-invocation: true
---

Load the recortex skill first; it gives the scripts folder. When the user requests observer suggestions (including `$ARGUMENTS` equal to `suggestions`), start `dashboard_action.py --suggestions --open` in the background from the configured project. Started from a session it prints no address: it opens the user's browser on a private launch file and prints only that file's path. Give the user that path, never an address or token, and do not read the file or call the service yourself; the user's own confirmation in the page is what authorizes a change. If it says this machine has no browser, say that, and that the same actions work in the chat. The service shows sources and saves reminder preferences locally. It stops after 30 idle minutes, or when the user chooses Stop on any of its pages.

For the ordinary project dashboard, run `mind.py --open` from the configured project. If it reports a missing or outdated index, run `index.py sync` and retry once after a successful sync. For any other failure, tell the user what the script reported.

If a browser opened, give the user the dashboard address it printed, and the manuscript address when one is printed.

If the output says "this machine has no browser", your whole reply is that one line and the one question the script printed, asked with the question tool, word for word. Give no address, no file size and no explanation of the page. On "Show the file's path", give the path. On the other answer, run the command printed under it and give the user the link it prints. Never copy or send the page yourself.

---
description: Open this project's dashboard and show its local address
argument-hint: "[suggestions]"
disable-model-invocation: true
---

Load the recortex skill first; it gives the scripts folder. When the user requests observer suggestions (including `$ARGUMENTS` equal to `suggestions`), start `dashboard_action.py --suggestions --open` in the background from the configured project. Started from a session it prints no address: it opens the user's browser on a private launch file and prints only that file's path. Give the user that path, never an address or token, and do not read the file or call the service yourself; the user's own confirmation in the page is what authorizes a change. If no browser opened, the user opens the file. The service shows sources and saves reminder preferences locally. It stops after 30 idle minutes, or when the user chooses Stop on any of its pages.

For the ordinary project dashboard, run `mind.py --open` from the configured project.
If it reports a missing or outdated index, run `index.py sync` and retry once after a successful sync.
For any other failure, tell the user what the script reported.

To put the page on the web, the user runs `publish.py` in their own terminal; never run it for them.

Give the user the dashboard address it printed, even when a browser started. Include its Project Map link for browsing the project hierarchy. If it printed a manuscript
address, include that direct link too. In a remote session, also give the printed file path so the user
can copy the standalone page to their own device.

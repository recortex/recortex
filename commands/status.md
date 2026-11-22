---
description: Tell where the project stands, what is open, what is due in the text and what waits for a yes
disable-model-invocation: true
---

Load the recortex skill first; it gives the scripts folder. Run `status.py --sync`, which first reads GitHub into the local copy, so that a step or a memory recorded elsewhere since the session began (for example a step reported in the dashboard) counts; a done step is listed with the memory that checked it, so cite that rather than doubt it. If it says GitHub could not be read, tell the user the status is from the local copy and how old that is. Tell the user, in this order: the "In short" paragraphs, the open judgment about a changed decision when one is printed, the open tasks and their next steps, what is due in the paper's text, and what waits for a yes. Then ask what waits: run `status.py --waiting` and ask each item it lists as one multiple-choice question from the template and options it prints. Do not offer them in prose. The handoff style from `guidance.py show` shapes the wording, never what is asked.

(function () {
  'use strict';
  const list = document.getElementById('suggestions');
  const status = document.getElementById('status');
  const fragment = window.location.hash.slice(1);
  let token = /^[0-9a-f]{32}$/.test(fragment) ? fragment : '';
  if (token) window.history.replaceState(null, '', window.location.pathname);
  const errors = {
    unauthorized: 'This session has expired. Open observer suggestions again from your project.',
    trust_required: 'This repository is no longer trusted on this device.',
    project_changed: 'The project configuration changed. Reopen suggestions from the project.',
    index_unavailable: 'The project record is unavailable. Refresh the local index and reopen suggestions.',
    observer_disabled: 'The observer is disabled for this folder.',
    suggestion_missing: 'This suggestion has expired. Reopen this page for the latest suggestions.',
    mute_limit: 'You have muted 100 suggestions. Restore one before muting another.',
    sources_unavailable: 'The sources have changed. Reload this page to see their current status.'
  };
  async function api(path, data) {
    const options = {method: data === undefined ? 'GET' : 'POST', cache: 'no-store', credentials: 'same-origin', headers: {}};
    if (token) options.headers.Authorization = 'Bearer ' + token;
    if (data !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(data);
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(errors[result.code] || 'Could not read or save the suggestion. Reload the page and try again.');
    return result;
  }
  function element(tag, words) {
    const node = document.createElement(tag);
    node.textContent = words;
    return node;
  }
  function draw(row, repo) {
    const card = element('article', '');
    card.className = 'suggestion';
    const primary = row.available && row.sources[0];
    card.appendChild(element('h2', primary ? primary.summary || 'Recorded ' + primary.kind : 'Source unavailable'));
    const when = new Date(row.at * 1000).toLocaleString();
    card.appendChild(element('p', when + (row.available ? ' · ' + (row.scope_kind === 'task' ? 'Task' : 'Topic') + ' #' + row.scope_issue : '')));
    if (row.available) {
      const why = element('details', '');
      why.appendChild(element('summary', 'Why this appeared'));
      const reasons = {
        map_question: 'The matched Project Map branch cited this open question.',
        decision_change: 'The work you named cited an earlier decision that has since changed.',
        possible_change: 'Wording in the work you named matched an earlier decision. The connection needs your judgment.',
        decision_failure: 'The task matched a standing decision linked to an earlier failure.'
      };
      why.appendChild(element('p', reasons[row.reason] || 'See the recorded sources below.'));
      if (row.branch) {
        const branch = element('a', 'Open the matching branch');
        branch.href = document.getElementById('back').getAttribute('href').split('#')[0] + '#' + row.branch;
        why.appendChild(branch);
      }
      const sources = element('ul', '');
      row.sources.forEach(ref => {
        const li = element('li', '');
        const link = element('a', ref.kind[0].toUpperCase() + ref.kind.slice(1) + ' · #' + ref.issue + ' · ' + (ref.summary || 'Read source'));
        link.href = ref.url;
        link.rel = 'noreferrer';
        li.append(link, document.createTextNode(' (' + ref.state + ')'));
        sources.appendChild(li);
      });
      why.appendChild(sources);
      card.appendChild(why);
      const correction = element('details', '');
      correction.appendChild(element('summary', 'Correct the shared record'));
      correction.appendChild(element('p', 'Paste this request into your project chat and describe what changed. Review the correction there.'));
      const request = element('textarea', '');
      request.readOnly = true;
      request.setAttribute('aria-label', 'Correction request');
      request.rows = 3;
      request.value = 'Review the recorded ' + primary.kind + ' at https://github.com/' + repo + '/issues/' +
        primary.issue + '#issuecomment-' + primary.comment + ' and help me record a correction. What changed: ';
      correction.appendChild(request);
      const copy = element('button', 'Copy correction request');
      copy.type = 'button';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(request.value);
          status.textContent = 'Correction request copied for your project chat.';
        } catch (_) {
          request.focus();
          request.select();
          status.textContent = 'The request is selected. Use your device’s Copy command.';
        }
      });
      correction.appendChild(copy);
      card.appendChild(correction);
    } else {
      card.appendChild(element('p', 'The cited records are unavailable in the current trusted index.'));
    }
    if (row.available || row.muted) {
      const toggle = element('button', '');
      toggle.type = 'button';
      function label() { toggle.textContent = row.muted ? 'Restore reminder' : 'Mute this reminder'; }
      label();
      toggle.addEventListener('click', async () => {
        toggle.disabled = true;
        try {
          const updated = await api('api/suggestions/feedback', {id: row.id, muted: !row.muted});
          row.muted = updated.muted;
          label();
          status.textContent = row.muted ? 'Muted for this context and evidence in future sessions.' :
            'This reminder can appear again when it is relevant in a future session.';
          if (!row.available && !row.muted) toggle.hidden = true;
        } catch (error) { status.textContent = error.message; }
        finally { toggle.disabled = false; }
      });
      card.appendChild(toggle);
    }
    return card;
  }
  async function start() {
    if (token) { await api('api/session', {}); token = ''; }
    const data = await api('api/suggestions');
    document.getElementById('context').textContent = data.repo + ' · Reminder preferences apply to this project folder on this device.';
    const mode = {disabled: 'The observer is disabled for this folder.', off: 'Task-time suggestions are turned off.',
      shadow: 'Shadow mode keeps candidate suggestions private and never issues them.', deliver: ''};
    status.textContent = [mode[data.mode], data.suggestions.length ? '' : 'No delivered suggestions to review.'].filter(Boolean).join(' ');
    data.suggestions.forEach(row => list.appendChild(draw(row, data.repo)));
  }
  start().catch(error => { status.textContent = error.message; });
}());

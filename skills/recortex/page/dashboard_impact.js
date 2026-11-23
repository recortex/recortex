(function () {
  'use strict';
  const byId = id => document.getElementById(id);
  const names = {still_correct: 'Still correct', needs_update: 'Needs an update', unrelated: 'Unrelated',
    defer: 'Deferred', applying: 'Applying · verification pending', applied: 'Applied and verified'};
  const issueUrl = /^https:\/\/github\.com\/[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+\/issues\/[1-9][0-9]*(?:#issuecomment-[1-9][0-9]*)?$/;
  const snapshot = byId('back').getAttribute('href').split('#')[0];
  let rows = [], repo = '', chosen, operation, busy = false, token = '';
  const fragment = location.hash.slice(1);
  const fromOverview = fragment.endsWith('-overview');
  if (/^[0-9a-f]{32}$/.test(fragment)) {
    token = fragment;
    history.replaceState(null, '', location.pathname + location.search);
  }
  const errors = {unauthorized: 'The dashboard session expired. Reopen it from the project.',
    trust_required: 'The project requires trust on this device.', project_changed: 'The project configuration changed. Reopen the dashboard.',
    index_unavailable: 'The project record could not be read. Refresh the local index and reopen the dashboard.',
    hold: 'The current project hold prevents this change.'};
  async function api(path, body) {
    const options = {method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store', headers: {}};
    if (token) options.headers.Authorization = 'Bearer ' + token;
    if (body) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const res = await fetch(path, options), data = await res.json();
    if (!res.ok) {
      const hint = res.status === 409 ? 'The evidence or review changed. Reload this page and compare the current record.' :
        res.status === 403 ? 'Your current project permissions or policy prevent this change.' :
        'The operation could not be verified. You can retry or reopen the project dashboard.';
      throw new Error(data.code === 'review_refused' && typeof data.message === 'string' ? data.message : errors[data.code] || hint);
    }
    return data;
  }
  function element(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  }
  function link(node, url) {
    if (typeof url === 'string' && issueUrl.test(url) && url.startsWith('https://github.com/' + repo + '/issues/')) {
      node.href = url; node.rel = 'noreferrer'; node.hidden = false;
    } else { node.removeAttribute('href'); node.hidden = true; }
  }
  async function run(job) {
    if (busy) return;
    busy = true;
    const main = document.querySelector('main');
    main.setAttribute('aria-busy', 'true');
    const controls = Array.from(main.querySelectorAll('button,select,input,textarea'));
    controls.forEach(control => { control.disabled = true; });
    try { await job(); } catch (error) { byId('status').textContent = error.message; }
    finally {
      busy = false; main.removeAttribute('aria-busy');
      controls.forEach(control => { control.disabled = false; });
      if (chosen && chosen.group.conflict) document.querySelector('[value="needs_update"]').disabled = true;
    }
  }
  function selection(key) {
    chosen = rows.find(row => row.key === key);
    byId('preview').hidden = true; operation = null;
    byId('evidence').hidden = !chosen; byId('judgment').hidden = !chosen;
    if (!chosen) {
      byId('item').value = '';
      byId('status').textContent = key ? 'This item has changed or is unavailable. Choose a current item or open History.' :
        rows.length ? 'Choose a connection to review.' : 'No current items to review. History keeps past reviews.';
      return;
    }
    const {item, group} = chosen, review = item.review || {};
    byId('item').value = key;
    byId('back').href = snapshot + (fromOverview ? '#' : '#history-') + group.id;
    byId('back').textContent = fromOverview ? 'Back to decision in Overview' : 'Back to History';
    byId('label').textContent = item.label;
    byId('quote').textContent = item.quote || ''; byId('quote').hidden = !item.quote;
    byId('reason').textContent = (chosen.pending ? '' : item.relation === 'direct' ? 'Explicit connection. ' : 'Suggested connection. ') + item.reason;
    link(byId('source'), item.url);
    const decisions = byId('decisions'); decisions.replaceChildren();
    [group.old].concat(group.successors).forEach((entry, i) => {
      const p = element('p', i ? 'Recorded replacement · ' : 'Earlier decision · ');
      const a = element('a', entry.text); link(a, entry.url);
      p.append(a.hidden ? document.createTextNode(entry.text) : a); decisions.append(p);
    });
    const questions = byId('questions');
    questions.replaceChildren();
    if (!chosen.pending) {
      [['Help me decide','Help me decide what to do'], ['What am I missing?','What am I missing']].forEach(([title, question]) => {
        const details = document.createElement('details');
        details.append(element('summary', title), element('p', 'Ask in the project chat.'),
          element('code', '/recortex:guide ' + question + ' about ' + group.id + ', item ' + item.id + ' in ' + repo + '?'));
        questions.append(details);
      });
    }
    byId('current').textContent = review.verdict ? 'Last shared review · ' + (names[review.verdict] || 'Reviewed') +
      (review.reviewer ? ' · ' + review.reviewer : '') + (review.note ? ' — ' + review.note : '') : 'Awaiting a first review.';
    byId('resume').hidden = !(['needs_update', 'applying'].includes(review.verdict) && ['map','body'].includes(review.action));
    byId('resume').textContent = review.verdict === 'applying' ? 'Check pending change' : 'Reopen accepted change';
    byId('judgment').reset();
    document.querySelector('[value="needs_update"]').disabled = !!group.conflict;
    byId('note').value = review.note || '';
    byId('draft').value = review.draft || item.edit_text || item.draft || '';
    byId('draft-field').hidden = true;
    byId('judgment').hidden = !!chosen.pending || review.verdict === 'applying';
    byId('status').textContent = group.conflict ? 'Multiple replacements are in force. Confirm which governs before drafting an update.' : '';
  }
  function showDetail(result) {
    const detail = result.detail || {}, body = byId('detail');
    body.replaceChildren();
    [['Before',detail.before], ['Proposed change',detail.after], ['Request for the project chat', result.action === 'followup' ? detail.request : '']].forEach(pair => {
      if (pair[1]) body.append(element('h3', pair[0]), element('pre', pair[1]));
    });
    if (result.action === 'followup' && !detail.request && result.draft) body.append(element('pre', result.draft));
  }
  function display(result, stage) {
    operation = result;
    const native = ['map','body'].includes(result.action);
    byId('preview').hidden = false;
    byId('judgment').hidden = true;
    byId('preview-title').textContent = stage === 'preview' ? 'Review before saving' : result.applied ? 'Change verified' : 'Shared review';
    byId('summary').textContent = result.summary || (result.applied ? 'The updated target was read back and verified.' :
      'The review is recorded in the project.');
    showDetail(result);
    byId('effect').textContent = stage === 'preview' ? (native ? 'Saving records your review and draft. A separate action applies the exact change above.' :
      result.action === 'followup' ? 'Saving records your review and follow-up. Take the request to the project chat for approval.' :
        'Saving adds this judgment to the shared project history.') :
      result.applied ? '' : native ? 'Apply when the proposed change is ready.' :
        result.action === 'followup' ? 'Paste the request into the project chat to continue.' : '';
    byId('confirm').hidden = stage !== 'preview';
    byId('edit').hidden = !!chosen.pending;
    byId('edit').textContent = stage === 'preview' ? 'Back to editing' : 'Revisit review';
    byId('apply').textContent = chosen.pending || (chosen.item.review || {}).verdict === 'applying' ? 'Verify pending change' : 'Apply reviewed change';
    byId('apply').hidden = stage !== 'saved' || !native || !!result.applied;
    const review = result.review || {};
    const receipt = result.receipt || {};
    const issue = receipt.issue || chosen.group.hub, comment = receipt.comment || (!receipt.issue && review.comment);
    const url = Number.isSafeInteger(issue) ? 'https://github.com/' + repo + '/issues/' + issue +
      (Number.isSafeInteger(comment) && comment > 0 ? '#issuecomment-' + comment : '') : '';
    byId('receipt').textContent = receipt.issue ? 'Read updated target on GitHub' : 'Read shared review on GitHub';
    link(byId('receipt'), stage === 'saved' ? url : '');
    byId('preview-title').focus();
  }
  function version() {
    const {group, item} = chosen;
    return {group: group.id, target: item.id, revision: item.revision, previous: (item.review || {}).comment || 0};
  }
  async function start() {
    if (token) { await api('api/session', {}); token = ''; }
    const data = await api('api/impact');
    repo = data.repo;
    byId('project').textContent = [repo].concat(data.partial ? ['Partial record.'].concat(data.coverage || []) : []).join(' · ');
    rows = [];
    data.groups.forEach(group => {
      group.items.forEach(item => rows.push({group, item, key: group.id + '-' + item.id}));
      (group.pending_verifications || []).forEach(review => {
        const context = review.context || {};
        const item = {id: review.target, revision: review.revision, review, label: context.label || 'Earlier item',
          reason: 'A shared edit began and its result still needs to be checked on the originating device.'};
        rows.push({group, item, pending: true, key: group.id + '-' + item.id + '-pending-' + review.comment});
      });
    });
    const select = byId('item');
    select.append(new Option('Choose an item…', ''));
    rows.forEach(row => select.append(new Option(row.item.label + ' · ' + row.group.old.text, row.key)));
    byId('choose').hidden = !rows.length;
    const initial = location.hash.slice(1) || new URLSearchParams(location.search).get('item') || '';
    selection(initial.replace(/-overview$/, ''));
  }
  byId('item').addEventListener('change', () => {
    const key = byId('item').value;
    location.hash = key + (fromOverview && key ? '-overview' : '');
  });
  window.addEventListener('hashchange', () => {
    if (busy) { history.replaceState(null, '', '#' + (chosen ? chosen.key : '')); return; }
    selection(location.hash.slice(1).replace(/-overview$/, ''));
  });
  byId('judgment').addEventListener('change', () => {
    byId('draft-field').hidden = !document.querySelector('[value="needs_update"]').checked;
  });
  byId('judgment').addEventListener('submit', event => {
    event.preventDefault(); run(async () => {
      const verdict = document.querySelector('[name="verdict"]:checked').value;
      const body = Object.assign(version(), {verdict, note: byId('note').value,
        draft: verdict === 'needs_update' ? byId('draft').value : ''});
      display(await api('api/impact/preview', body), 'preview');
    });
  });
  byId('edit').addEventListener('click', () => {
    operation = null; byId('preview').hidden = true; byId('judgment').hidden = false;
    (document.querySelector('[name="verdict"]:checked') || document.querySelector('[name="verdict"]')).focus();
  });
  byId('resume').addEventListener('click', () => run(async () => display(await api('api/impact/resume', version()), 'saved')));
  async function save(path) {
    const result = await api('api/impact/' + path, {id: operation.id});
    if (result.review) chosen.item.review = result.review;
    selection(chosen.key);
    byId('status').textContent = result.applied ? 'Applied and verified.' : 'Shared review saved.';
    display(result, 'saved');
  }
  byId('confirm').addEventListener('click', () => run(() => save('commit')));
  byId('apply').addEventListener('click', () => run(() => save('apply')));
  run(start);
}());

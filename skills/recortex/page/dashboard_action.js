/* The explicit local action page only. The exported snapshot never loads this script. */
(function () {
  'use strict';
  var fragment = window.location.hash.slice(1);
  var selected = /^step-([1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*)$/.exec(fragment);
  var token = /^[A-Za-z0-9_-]{43}$/.test(fragment) ? fragment : '';
  window.history.replaceState(null, '', window.location.pathname);
  if (selected) document.getElementById('back').href = 'snapshot#work-step-' + selected[1];
  var busy = false, preview = null;
  var status = document.getElementById('status');
  // Recording waits on GitHub for up to about half a minute: the page says so at once, with the buttons off.
  var RECORDING = 'Recording this completion on GitHub… This can take about 30 seconds. Please wait, and do not record it again.';
  var messages = {
    unauthorized: 'This connection has expired. Open the private address again from the terminal or the launch file that started the service. A stopped service needs a new start.',
    unavailable: 'The record could not be checked. No success is assumed. Check recent operations before trying again.',
    index_unavailable: 'There is no usable local record yet. Choose Refresh from GitHub to build it.',
    project_changed: 'The project configuration changed. Stop and restart the service.',
    trust_required: 'This repository is no longer trusted. No change was made.',
    allowed_account_required: 'Sign in with an account allowed by this project before recording.',
    step_changed: 'This step changed or is no longer open. Refresh and review it again.',
    held: 'A publication or command hold blocks this action. Ask the session about your holds.',
    holds_unavailable: 'Holds could not be checked. Recording is unavailable.',
    preview_expired_or_changed: 'The preview expired or changed. Review a new preview.',
    completion_words_required: 'Describe what you completed in 1 to 1,000 characters.',
    content_refused: 'This statement does not pass the record checks. Review it in the main session.',
    busy: 'Another local update is running. Try again after it finishes.',
    reconcile_first: 'An earlier attempt for this step is uncertain. Check its receipt below first.',
    sync_incomplete: 'The record refresh was incomplete. No completion was recorded.',
    snapshot_failed: 'The record was refreshed, but the offline snapshot could not be rebuilt.'
  };
  function node(tag, text) {
    var element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  }
  async function api(path, data) {
    var options = {method: data === undefined ? 'GET' : 'POST', cache: 'no-store', credentials: 'same-origin', headers: {}};
    if (token) options.headers.Authorization = 'Bearer ' + token;
    if (data !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(data);
    }
    var response = await fetch(path, options);
    var result = await response.json();
    if (!response.ok) throw new Error(messages[result.code] || 'The action was refused. Refresh and review the record.');
    return result;
  }
  function closePreview() {
    preview = null;
    document.getElementById('preview').hidden = true;
    document.getElementById('body').textContent = '';
    document.getElementById('review-step').textContent = '';
    document.getElementById('review-statement').textContent = '';
    document.getElementById('exact-record').open = false;
  }
  async function run(action) {
    if (busy) return;
    busy = true;
    document.querySelector('main').setAttribute('aria-busy', 'true');
    status.textContent = 'Checking the record…';
    try { await action(); }
    catch (error) { status.textContent = error.message || messages.unavailable; }
    finally {
      busy = false;
      document.querySelector('main').removeAttribute('aria-busy');
    }
  }
  function showResult(result) {
    if (result.state !== 'recorded') {
      status.textContent = 'The outcome is uncertain. Check the receipt below; do not record another copy.';
    } else if (result.refresh === 'updated' || result.refresh === 'current') {
      status.textContent = 'Completion recorded and the task list updated. Refresh to rebuild the offline snapshot.';
    } else if (result.refresh === 'no_task_list') {
      status.textContent = 'Completion recorded. This task has no list to update. Refresh to rebuild the offline snapshot.';
    } else {
      status.textContent = 'Completion recorded. The task list or local refresh is incomplete; the completion must not be sent again.';
    }
  }
  async function reloadState() {
    try { draw(await api('api/state')); }
    catch (error) { /* A failed refresh must not erase a verified operation result. */ }
  }
  function recoveryButton(ident) {
    var button = node('button', 'Check this operation’s receipt');
    button.type = 'button';
    button.addEventListener('click', function () { run(async function () {
      var result = await api('api/reconcile', {id: ident});
      await reloadState();
      showResult(result);
    }); });
    document.getElementById('operations').prepend(button);
  }
  function draw(data) {
    document.getElementById('project').textContent = data.repo + ' · local interactive session';
    var tasks = document.getElementById('tasks');
    tasks.replaceChildren();
    if (!data.tasks.length) tasks.appendChild(node('p', 'No open tasks in the local record. Refresh to check GitHub.'));
    data.tasks.forEach(function (task) {
      if (selected && task.issue !== Number(selected[1].split('-')[0])) return;
      var section = node('section');
      section.appendChild(node('h2', '#' + task.issue + ' ' + task.title));
      task.steps.forEach(function (step) {
        if (selected && step.id !== selected[1].replace(/-/g, ':')) return;
        var item = node('div');
        item.className = 'step';
        item.appendChild(node('p', step.text + ' · ' + step.state));
        if (step.state === 'open') {
          var label = node('label', 'What did you complete?');
          var input = node('textarea');
          input.maxLength = 1000;
          input.rows = 2;
          label.appendChild(input);
          item.appendChild(label);
          var button = node('button', 'Preview completion');
          button.type = 'button';
          button.addEventListener('click', function () { run(async function () {
            closePreview();
            var words = input.value;
            var checked = await api('api/preview', {step: step.id, words: words});
            if (input.value !== words) {
              status.textContent = 'Your statement changed. Preview it again to confirm the new wording.';
              return;
            }
            preview = checked;
            document.getElementById('review-step').textContent = 'Task #' + checked.target.issue + ' · ' + checked.step_text;
            document.getElementById('review-statement').textContent = checked.statement;
            document.getElementById('body').textContent = preview.body;
            document.getElementById('preview').hidden = false;
            document.getElementById('confirm').focus();
            status.textContent = 'Review your statement before recording it.';
          }); });
          input.addEventListener('input', function () {
            if (preview && !busy) {
              closePreview();
              status.textContent = 'Preview your updated statement to continue.';
            }
          });
          item.appendChild(button);
        }
        section.appendChild(item);
      });
      tasks.appendChild(section);
    });
    if (data.omitted_tasks) tasks.appendChild(node('p', data.omitted_tasks + ' additional tasks are not shown in this bounded action view.'));
    var operations = document.getElementById('operations');
    operations.replaceChildren();
    data.operations.forEach(function (operation) {
      var item = node('p', '#' + operation.target.issue + ': ' + 'step ' + operation.target.comment + ':' + operation.target.ordinal + ' · ' + operation.state + ' ');
      if (operation.receipt) {
        var link = node('a', 'View recorded completion');
        link.href = operation.receipt.url;
        link.rel = 'noreferrer noopener';
        item.appendChild(link);
      }
      if (operation.state === 'uncertain' || operation.state === 'sending') {
        var button = node('button', 'Check receipt');
        button.type = 'button';
        button.addEventListener('click', function () { run(async function () {
          var result = await api('api/reconcile', {id: operation.id});
          await reloadState();
          showResult(result);
        }); });
        item.appendChild(button);
      }
      operations.appendChild(item);
    });
  }
  document.getElementById('refresh').addEventListener('click', function () { run(async function () {
    closePreview();
    draw(await api('api/refresh', {}));
    status.textContent = 'Record and offline snapshot refreshed. Reopen the saved snapshot to view its new contents.';
  }); });
  document.getElementById('cancel').addEventListener('click', closePreview);
  function lockConfirm(locked) {
    document.getElementById('confirm').disabled = locked;
    document.getElementById('cancel').disabled = locked;
  }
  document.getElementById('confirm').addEventListener('click', function () { run(async function () {
    if (!preview) return;
    var chosen = preview;
    lockConfirm(true);
    status.textContent = RECORDING;
    try {
      var result = await api('api/commit', {id: chosen.id, body_hash: chosen.body_hash});
      closePreview();
      await reloadState();
      showResult(result);
    } catch (error) {
      closePreview();
      await reloadState();
      recoveryButton(chosen.id);
      status.textContent = error.message + ' Check this operation’s receipt before starting another completion.';
    } finally {
      lockConfirm(false);
    }
  }); });
  run(async function () {
    if (token) {
      await api('api/session', {});
      token = '';
      await api('api/state');
      window.location.replace('snapshot#todo');
      return;
    }
    draw(await api('api/state'));
    status.textContent = 'Showing cached tasks. Every completion is checked again against GitHub before posting.';
  });
}());

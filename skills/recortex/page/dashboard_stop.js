/* The Stop control of the explicit local pages. It holds no record text and is never part of the exported snapshot. */
(function () {
  'use strict';
  var main = document.querySelector('main');
  if (!main) return;
  var wrap = document.createElement('p');
  wrap.className = 'stop-service';
  var button = document.createElement('button');
  button.type = 'button';
  button.id = 'stop-service';
  button.textContent = 'Stop the local service';
  var note = document.createElement('span');
  note.id = 'stop-note';
  note.setAttribute('role', 'status');
  note.setAttribute('aria-live', 'polite');
  wrap.appendChild(button);
  wrap.appendChild(note);
  main.appendChild(wrap);
  button.addEventListener('click', async function () {
    button.disabled = true;
    try {
      var response = await fetch('api/stop', {method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
      if (response.ok) {
        note.textContent = 'The local service has stopped. You can close this page.';
        return;
      }
      note.textContent = 'The service did not stop. It stops by itself 24 hours after the last session.';
    } catch (error) {
      note.textContent = 'The service did not answer. It may already have stopped.';
    }
    button.disabled = false;
  });
})();

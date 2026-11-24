(function () {
  'use strict';
  // Fixed text. No record text lives here: everything shown is read from the page with textContent or copied with
  // cloneNode, and new nodes get their words through textContent. An id read from the page is checked before it
  // goes into a selector or a hash.
  var MEMORY = /^[0-9]+$/, WORD = /^w[0-9]+$/, ISSUE = /^issue-[0-9]+$/, THREAD = /^thread-[0-9]+$/,
    PLACE = /^#[A-Za-z][A-Za-z0-9-]*$/, ID = /^[A-Za-z][A-Za-z0-9-]{0,80}$/, KEY = /^[a-z][a-z0-9-]{0,40}$/;
  var root = document.documentElement;
  root.classList.add('js');
  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  var views = $$('.view'), threads = $$('.thread'), mems = $$('.mem'), panel = $('#panel');
  var opened = null, lastFocus = null, returnFocus = null;
  var outputViews = $('#output-views');
  var AREAS = { todo: 'work', open: 'work', text: 'outputs', paper: 'outputs', threads: 'history',
    settled: 'history', lessons: 'history', map: 'history' };

  function show(id) {
    views.forEach(function (v) { v.classList.toggle('on', v.id === id); });
    root.setAttribute('data-view', id);
    if (outputViews) $$('a', outputViews).forEach(function (link) {
      var active = id === 'text' && link.hash === '#outputs' || id === 'paper' && link.hash === '#paper';
      link.classList.toggle('here', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    $$('[data-nav]').forEach(function (a) {
      var on = a.getAttribute('data-nav') === (AREAS[id] || (/^read-[0-9]+$/.test(id) ? 'record' : id));
      a.classList.toggle('on', on);
      if (on) { a.setAttribute('aria-current', 'page'); } else { a.removeAttribute('aria-current'); }
    });
  }
  function showThread(id) {
    threads.forEach(function (t) { t.classList.toggle('on', t.id === id); });
    $('#threads').classList.toggle('reading', !!id);
  }
  // a fold: a branch of the tree, a task group of a story, project tools on a phone. The page is built open; only this
  // script shuts what starts shut, so without it everything is open.
  function setShut(host, shut) {
    host.classList.toggle('shut', shut);
    var button = $('[data-fold]', host);
    if (button) { button.setAttribute('aria-expanded', shut ? 'false' : 'true'); }
  }
  function unfold(el) {
    for (var n = el; n && n.classList; n = n.parentElement) {
      if (n.classList.contains('fold') && n.classList.contains('shut')) { setShut(n, false); }
    }
  }
  // the two pictures of the map: only one shows at a time
  function showPane(name) {
    if (!KEY.test(name || '')) { return; }
    $$('.mappane').forEach(function (pane) { pane.classList.toggle('on', pane.id === 'map-' + name); });
    $$('[data-fold^="pane-"]').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-fold') === 'pane-' + name); });
  }
  function narrow() { return !!(window.matchMedia && window.matchMedia('(max-width: 860px)').matches); }
  function mark(id) {
    $$('.cur').forEach(function (el) { el.classList.remove('cur'); });
    $$('.hot').forEach(function (el) { el.classList.remove('hot'); });
    if (!id || !MEMORY.test(id)) { return; }
    $$('[data-m="' + id + '"]').forEach(function (el) {
      var host = el.closest('.station, .answer') || el;
      host.classList.add('cur');
    });
    $$('.link[data-a="' + id + '"], .link[data-b="' + id + '"]').forEach(function (el) { el.classList.add('hot'); });
  }
  function openMem(id, keep) {
    if (!MEMORY.test(id || '')) { return; }
    var target = document.getElementById('m-' + id);
    if (!target) { return; }
    if (!keep) { lastFocus = document.activeElement; }
    mems.forEach(function (m) { m.classList.toggle('on', m === target); });
    panel.classList.add('on');
    panel.scrollTop = 0;
    opened = id;
    mark(id);
    selectedHistory();
    target.focus({ preventScroll: true });
  }
  function closeMem(routing) {
    if (!opened) { return; }
    panel.classList.remove('on');
    opened = null;
    mark(null);
    if (lastFocus && document.contains(lastFocus)) {
      returnFocus = lastFocus;
      lastFocus.focus({ preventScroll: true });
    }
    selectedHistory();
    if (!routing && /^#m-[0-9]+$/.test(location.hash)) {
      var depth = history.state && history.state.memDepth;
      if (Number.isInteger(depth) && depth > 0 && depth < 100) { history.go(-depth); }
      else { location.hash = '#' + (root.getAttribute('data-view') || 'now'); }
    }
  }
  var branchRows = $$('.branch-row'), branchDetails = $$('.branch-detail'), branchSearch = $('#branch-search');
  var branchById = new Map(), branchInfo = new Map(), selectedBranch = null, sampleRename = null;
  function branchOpen(row) {
    return row.classList.contains('branch-open') || row.classList.contains('branch-reveal');
  }
  function setBranchOpen(row, open) {
    row.classList.remove('branch-reveal');
    row.classList.toggle('branch-open', open);
    var button = row.firstElementChild.querySelector('button');
    if (button) button.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  branchRows.forEach(function (row) {
    var link = row.firstElementChild.querySelector('a'), id = link.hash.slice(1);
    if (!/^branch-[a-f0-9]{24}$/.test(id)) return;
    var detail = document.getElementById(id);
    if (!detail) return;
    var button = row.firstElementChild.querySelector('button');
    branchById.set(id, row);
    branchInfo.set(row, {link: link, detail: detail, parent: row.parentElement.closest('.branch-row')});
    setBranchOpen(row, false);
    if (button) button.addEventListener('click', function () { setBranchOpen(row, !branchOpen(row)); });
    link.addEventListener('keydown', function (event) {
      var key = event.key;
      if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].indexOf(key) < 0) return;
      event.preventDefault();
      var child = row.children[1] && row.children[1].querySelector(':scope > li:not([hidden])');
      if (key === 'ArrowRight' && child) {
        if (branchOpen(row)) branchInfo.get(child).link.focus();
        else setBranchOpen(row, true);
      } else if (key === 'ArrowLeft') {
        if (child && branchOpen(row)) setBranchOpen(row, false);
        else if (branchInfo.get(row).parent) branchInfo.get(branchInfo.get(row).parent).link.focus();
      } else if (key === 'ArrowUp' || key === 'ArrowDown') {
        var visible = branchRows.filter(function (r) { return r.offsetHeight > 0; });
        var next = visible[visible.indexOf(row) + (key === 'ArrowDown' ? 1 : -1)];
        if (next) branchInfo.get(next).link.focus();
      }
    });
  });
  function filterBranches() {
    if (!branchSearch) return;
    var query = branchSearch.value.trim().toLowerCase(), found = 0, visible = new Set();
    branchRows.slice().reverse().forEach(function (row) {
      var info = branchInfo.get(row);
      var description = $('.branch-description', info.detail);
      var match = !query || (info.link.textContent + ' ' + (description ? description.textContent : '')).toLowerCase().indexOf(query) >= 0;
      if (match) found++;
      if (match || visible.has(row)) {
        if (info.parent) visible.add(info.parent);
        row.hidden = false;
      } else row.hidden = true;
      row.classList.toggle('branch-reveal', !!query && visible.has(row));
      var button = row.firstElementChild.querySelector('button');
      if (button) button.setAttribute('aria-expanded', String(branchOpen(row)));
    });
    $('#branch-matches').textContent = query ? found + (found === 1 ? ' match' : ' matches') : '';
  }
  function revealBranchLabel(link) {
    var canvas = link.closest('.branch-outline'), label = link.getBoundingClientRect(), bounds = canvas.getBoundingClientRect();
    if (label.right > bounds.right) canvas.scrollLeft += label.right - bounds.right + 16;
    else if (label.left < bounds.left) canvas.scrollLeft += label.left - bounds.left - 16;
    if (label.bottom > bounds.bottom) canvas.scrollTop += label.bottom - bounds.bottom + 16;
    else if (label.top < bounds.top) canvas.scrollTop += label.top - bounds.top - 16;
  }
  function selectBranch(id) {
    if (!branchDetails.length) return;
    var row = branchById.get(id), info = row && branchInfo.get(row), detail = info ? info.detail : $('.branch-home');
    if (selectedBranch !== id && $('#branch-apply')) {
      sampleRename = null; $('#branch-apply').hidden = true; $('#branch-sample-status').textContent = '';
    }
    selectedBranch = row ? id : null;
    branchDetails.forEach(function (d) { d.classList.toggle('selected', d === detail); });
    branchById.forEach(function (r) {
      r.classList.toggle('selected', r === row);
      branchInfo.get(r).link.removeAttribute('aria-current');
    });
    if (info) {
      if (row.hidden && branchSearch) { branchSearch.value = ''; filterBranches(); }
      info.link.setAttribute('aria-current', 'location');
      for (var p = row; p; p = branchInfo.get(p).parent) setBranchOpen(p, true);
      revealBranchLabel(info.link);
    }
  }
  if (branchSearch) {
    branchSearch.addEventListener('input', filterBranches);
    var mapView = $('#project-map'), diagramButton = $('#branch-diagram'), outlineButton = $('#branch-list');
    function chooseLayout(diagram) {
      mapView.classList.toggle('branch-diagram', diagram);
      diagramButton.setAttribute('aria-pressed', String(diagram));
      outlineButton.setAttribute('aria-pressed', String(!diagram));
      var row = branchById.get(selectedBranch);
      if (row) revealBranchLabel(branchInfo.get(row).link);
    }
    chooseLayout(window.matchMedia('(min-width: 700px)').matches);
    diagramButton.addEventListener('click', function () { chooseLayout(true); });
    outlineButton.addEventListener('click', function () { chooseLayout(false); });
  }
  var tryRename = $('#branch-try');
  if (tryRename) {
    var newName = $('#branch-new-name'), applyRename = $('#branch-apply'), sampleStatus = $('#branch-sample-status');
    tryRename.addEventListener('click', function () {
      var row = branchById.get(selectedBranch), words = newName.value.trim();
      applyRename.hidden = true; sampleRename = null;
      if (!row) { sampleStatus.textContent = 'Choose a branch in the map first.'; return; }
      if (!words || words.length > 120) { sampleStatus.textContent = 'Enter a branch name of 1 to 120 characters.'; return; }
      sampleRename = {id: selectedBranch, words: words};
      sampleStatus.textContent = 'Rename “' + branchInfo.get(row).link.textContent + '” to “' + words + '”.';
      applyRename.hidden = false;
    });
    newName.addEventListener('input', function () { sampleRename = null; applyRename.hidden = true; sampleStatus.textContent = ''; });
    applyRename.addEventListener('click', function () {
      if (!sampleRename || selectedBranch !== sampleRename.id) return;
      var info = branchInfo.get(branchById.get(selectedBranch));
      $$('.branch-layout a').forEach(function (link) {
        if (link.hash === '#' + selectedBranch && !link.closest('.branch-history')) link.textContent = sampleRename.words;
      });
      $('.branch-title', info.detail).textContent = sampleRename.words;
      var buttonLabel = $('.branch-sr', branchById.get(selectedBranch).firstElementChild);
      if (buttonLabel) buttonLabel.textContent = 'Branches under ' + sampleRename.words;
      sampleStatus.textContent = 'Sample renamed. Reload to reset the example.';
      newName.value = ''; sampleRename = null; applyRename.hidden = true; filterBranches();
    });
  }
  var mapNav = $('[data-nav="project-map"]');
  if (mapNav) mapNav.addEventListener('click', function (event) {
    if (!selectedBranch || root.getAttribute('data-view') === 'project-map') return;
    event.preventDefault(); location.hash = '#' + selectedBranch;
  });
  var kept = {};
  function route(outputScroll) {
    var was = root.getAttribute('data-view');
    var previousStory = was === 'threads' && $('.thread.on');
    if (was && !previousStory) { kept[was] = window.scrollY; }
    var start = root.getAttribute('data-start') || '';   // the live server's route for one topic opens that topic's view
    var h = location.hash.slice(1) || (/^read-[0-9]+$/.test(start) ? start : 'now');
    if (/^m-[0-9]+$/.test(h)) {
      if (!views.some(function (v) { return v.classList.contains('on'); })) { show('now'); }
      openMem(h.slice(2), true);
      return;
    }
    closeMem(true);
    // a hash that names anything inside a view shows that view, and inside a thread that thread's article
    var el = ID.test(h) ? document.getElementById(h) : null;
    if (!el && /^impact-[0-9]+$/.test(h)) {
      // A completed review may have cleared the last Overview item. Its history keeps the return anchor.
      el = document.getElementById('history-' + h);
    }
    var view = el && el.closest ? el.closest('.view') : null;
    if (view) {
      show(view.id);
      if (view.id === 'project-map') selectBranch(h);
      var thread = el.closest('.thread');
      if (view.id === 'threads') { showThread(thread && thread.id); }
      var pane = el.closest('.mappane');
      if (pane) { showPane(pane.id.slice(4)); }
      unfold(el);
      if (el.classList.contains('impact-group')) { el.open = true; }
    } else { show('now'); }
    if (el && el.classList && el.classList.contains('histbox') && el.hidden) { toggleBox(el.id); }
    var focusAfterRoute = returnFocus && view && returnFocus.closest('.view') === view ? returnFocus : null;
    if (!focusAfterRoute && previousStory && h === 'threads' && THREAD.test(previousStory.id)) {
      focusAfterRoute = $('.story-topics a[href="#' + previousStory.id + '"]');
    }
    if (!focusAfterRoute && thread && thread === el) { focusAfterRoute = thread; }
    if (!focusAfterRoute && el && el.classList.contains('impact-group')) { focusAfterRoute = $('summary', el); }
    if (!focusAfterRoute && el && el.classList.contains('branch-detail')) { focusAfterRoute = el; }
    function settle() {
      if (typeof outputScroll === 'number') { window.scrollTo(0, outputScroll); return; }
      var areaRoot = el === view || h === 'work' && view && view.id === 'todo' || h === 'outputs' && view && view.id === 'text';
      if (view && view.id === 'project-map' && window.matchMedia('(min-width: 1001px)').matches && el !== view) { return; }
      if (el && view && !areaRoot) { el.scrollIntoView(); } else { window.scrollTo(0, (view && kept[view.id]) || 0); }
    }
    settle();
    setTimeout(function () {
      settle();
      if (focusAfterRoute && document.contains(focusAfterRoute)) { focusAfterRoute.focus({ preventScroll: true }); }
      returnFocus = null;
    }, 0);
    var side = $('.side');
    if (narrow() && side) { setShut(side, true); }
  }
  // a project word: point at it for one line, choose it for the whole explanation in a box. The text is a copy of
  // its hidden entry; there is no page of words.
  var tip = $('#wordtip'), box = $('#wordbox'), boxBody = $('#wordbody');
  function entryOf(term) {
    var id = term.getAttribute('data-term') || '';
    return WORD.test(id) ? document.getElementById(id) : null;
  }
  function hideTip() { tip.hidden = true; }
  function showTip(term) {
    var entry = entryOf(term), def = entry && $('.def', entry);
    if (!def || box.open) { return; }
    while (tip.firstChild) { tip.removeChild(tip.firstChild); }
    tip.appendChild(def.cloneNode(true));
    var more = document.createElement('p');
    more.className = 'quiet'; more.textContent = 'Choose the word for more.';
    tip.appendChild(more);
    tip.hidden = false;
    var r = term.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    var top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) { top = Math.max(8, r.top - h - 8); }
    tip.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
    tip.style.top = top + 'px';
  }
  function explain(term) { openBox(entryOf(term), 'A word from this project'); }
  // a state, a kind or a mark: one fixed explanation, kept hidden in the page
  function explainKey(key) {
    if (KEY.test(key || '')) {
      openBox(document.getElementById('x-' + key), key === 'how' ? 'About this page' : 'What this means');
    }
  }
  function openBox(entry, kind) {
    if (!entry) { return; }
    $('#wordkind').textContent = kind;
    hideTip();
    while (boxBody.firstChild) { boxBody.removeChild(boxBody.firstChild); }
    $('#wordhead').textContent = $('.wt', entry).textContent;
    Array.prototype.forEach.call(entry.children, function (child) {
      if (!child.classList.contains('wt')) { boxBody.appendChild(child.cloneNode(true)); }
    });
    if (!box.open) { box.showModal(); }
    boxBody.scrollTop = 0;
  }
  function termAt(event) { return event.target.closest ? event.target.closest('.term') : null; }
  document.addEventListener('mouseover', function (event) { var t = termAt(event); if (t) { showTip(t); } });
  document.addEventListener('mouseout', function (event) {
    var t = termAt(event);
    if (t && !(event.relatedTarget && t.contains(event.relatedTarget))) { hideTip(); }
  });
  document.addEventListener('focusin', function (event) { var t = termAt(event); if (t) { showTip(t); } });
  document.addEventListener('focusout', function (event) { if (termAt(event)) { hideTip(); } });
  window.addEventListener('scroll', hideTip, true);
  $('#wordclose').addEventListener('click', function () { box.close(); });
  box.addEventListener('click', function (event) {
    // a click on the dimmed page around the box, or on a link inside it, closes it
    var a = event.target.closest('a');
    if (event.target === box || (a && !event.target.closest('.term') && (a.getAttribute('href') || '').charAt(0) === '#')) {
      box.close();
    }
  });

  $$('.fold.closed').forEach(function (host) { setShut(host, true); });
  if (outputViews) outputViews.addEventListener('click', function (event) {
    var link = event.target.closest('a');
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (location.hash !== link.hash) {
      var top = window.scrollY;
      history.pushState(null, '', link.hash);
      route(top);
    }
    link.focus({preventScroll: true});
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('popstate', route);
  window.addEventListener('pageshow', function () {
    // Firefox restores document focus after initial hash routing on reload.
    window.requestAnimationFrame(function () {
      if (!selectedBranch || document.activeElement !== document.body || location.hash !== '#' + selectedBranch) return;
      var detail = document.getElementById(selectedBranch);
      if (detail) detail.focus({preventScroll: true});
    });
  });
  route();
  // the panel slides only after the first frame, so that it does not slide off the page while it loads
  window.requestAnimationFrame(function () { root.classList.add('ready'); });

  document.addEventListener('click', function (event) {
    var term = event.target.closest && event.target.closest('.term');
    if (term) { event.preventDefault(); explain(term); return; }
    var why = event.target.closest && event.target.closest('[data-explain]');
    if (why) { event.preventDefault(); explainKey(why.getAttribute('data-explain')); return; }
    var fold = event.target.closest && event.target.closest('[data-fold]');
    if (fold) {
      event.preventDefault();
      var key = fold.getAttribute('data-fold') || '';
      if (key.indexOf('pane-') === 0) { showPane(key.slice(5)); return; }
      if (key === 'open-all' || key === 'close-all') {
        $$('.tree .fold').forEach(function (branch) { setShut(branch, key === 'close-all'); });
      } else {
        var host = fold.closest('.fold');
        if (host) { setShut(host, !host.classList.contains('shut')); }
      }
      return;
    }
    var cite = event.target.closest && event.target.closest('[data-issue]');
    if (cite) {
      event.preventDefault();
      var issue = cite.getAttribute('data-issue') || '';
      var reading = ISSUE.test(issue) ? document.getElementById('read-' + issue.slice(6)) : null;
      if (reading && reading.classList.contains('view')) { location.hash = '#read-' + issue.slice(6); }   // the whole topic is in the record
      else if (ISSUE.test(issue)) { openBox(document.getElementById(issue), 'An issue on GitHub'); }
      return;
    }
    var el = event.target.closest && event.target.closest('[data-m]');
    if (el) {
      event.preventDefault();
      var mid = el.getAttribute('data-m');
      if (!MEMORY.test(mid || '') || !document.getElementById('m-' + mid)) { return; }
      if (!el.closest('#panel')) { lastFocus = el; }
      var depth = opened && history.state ? history.state.memDepth : 0;
      depth = Number.isInteger(depth) && depth > 0 && depth < 98 ? depth : 0;
      if (location.hash !== '#m-' + mid) { history.pushState({ memDepth: depth + 1 }, '', '#m-' + mid); }
      openMem(mid, true); return;
    }
    if (opened && !event.target.closest('#panel') && !event.target.closest('#tour') &&
        !event.target.closest('#wordbox')) {
      var destination = event.target.closest('a');
      closeMem(!!(destination && (destination.getAttribute('href') || '').charAt(0) === '#'));
    }
  });
  $('#close').addEventListener('click', function () { closeMem(false); });

  // The timeline uses only the bounded, trusted rows already on this page. Filter state stays in this document.
  var historyList = $('#history-list'), historyRows = $$('.history-row'), historyControls = [
    'history-clock', 'history-order', 'history-topic', 'history-kind', 'history-state', 'history-scope',
    'history-query', 'history-from', 'history-to'];
  var historyEntries = historyRows.map(function (row) {
    return { row: row, ident: row.id.slice(8), text: row.textContent.toLowerCase(),
      posted: row.getAttribute('data-posted') || '', event: row.getAttribute('data-event') || '',
      topic: row.getAttribute('data-thread'), kind: row.getAttribute('data-kind'), state: row.getAttribute('data-state'),
      catchup: row.getAttribute('data-catchup') === 'yes', group: null };
  });
  var historyOrders = {}, historyOrderKey = '', historyGroups = [];
  function orderHistory(clock, reverse) {
    var key = clock + (reverse < 0 ? '-newest' : '-oldest');
    if (key === historyOrderKey) { return; }
    historyOrderKey = key;
    if (!historyOrders[key]) {
      historyOrders[key] = historyEntries.slice().sort(function (a, b) {
        var at = a[clock], bt = b[clock];
        if (!at && bt) { return 1; } if (at && !bt) { return -1; }
        var byId = a.ident.length - b.ident.length || (a.ident < b.ident ? -1 : a.ident > b.ident ? 1 : 0);
        return reverse * (at < bt ? -1 : at > bt ? 1 : byId);
      });
    }
    historyGroups.forEach(function (group) { group.node.remove(); });
    historyGroups = [];
    var fragment = document.createDocumentFragment(), lastDay = null, group;
    historyOrders[key].forEach(function (entry) {
      var day = entry[clock].slice(0, 10);
      if (day !== lastDay) {
        var label = document.createElement('li'); label.className = 'history-day';
        label.textContent = day ? day + ' (UTC)' : 'Time unknown';
        group = { node: label, visible: 0 }; historyGroups.push(group); fragment.appendChild(label); lastDay = day;
      }
      entry.group = group;
      fragment.appendChild(entry.row);
    });
    historyList.appendChild(fragment);
  }
  function selectedHistory() {
    var note = $('#history-selected'), row = opened && document.getElementById('history-' + opened);
    if (!note) { return; }
    note.hidden = !(row && row.hidden);
    note.textContent = row && row.hidden ? 'The selected memory is outside these filters. Its full detail and recorded links remain available.' : '';
  }
  function historyValue(id) { var el = $('#' + id); return el ? el.value : ''; }
  function filterHistory() {
    if (!historyList) { return; }
    var clock = historyValue('history-clock') === 'event' ? 'event' : 'posted';
    var words = historyValue('history-query').trim().toLowerCase().split(/\s+/).filter(Boolean);
    var topic = historyValue('history-topic'), kind = historyValue('history-kind'), state = historyValue('history-state');
    var since = historyValue('history-scope') === 'since', from = historyValue('history-from'), to = historyValue('history-to');
    var reverse = historyValue('history-order') === 'newest' ? -1 : 1;
    root.setAttribute('data-history-clock', clock);
    var activeFilters = [topic !== 'all', kind !== 'all', state !== 'all', since, !!words.length, !!from, !!to].filter(Boolean).length;
    $('#history-filter-label').textContent = activeFilters ? 'Filters (' + activeFilters + ' active)' : 'Filters';
    orderHistory(clock, reverse);
    historyGroups.forEach(function (group) { group.visible = 0; });
    var count = 0;
    historyEntries.forEach(function (entry) {
      var row = entry.row, day = entry[clock].slice(0, 10);
      var visible = (topic === 'all' || entry.topic === topic) &&
        (kind === 'all' || entry.kind === kind) &&
        (state === 'all' || entry.state === state) &&
        (!since || entry.catchup) &&
        (!from || day && day >= from) && (!to || day && day <= to) &&
        words.every(function (word) { return entry.text.indexOf(word) >= 0; });
      if (row.hidden !== !visible) { row.hidden = !visible; }
      if (visible) {
        count += 1;
        entry.group.visible += 1;
      }
    });
    historyGroups.forEach(function (group) {
      var hidden = group.visible === 0;
      if (group.node.hidden !== hidden) { group.node.hidden = hidden; }
    });
    $('#history-count').textContent = count + ' of ' + historyRows.length + ' included memories · ' +
      (clock === 'event' ? 'event time' : 'posting time') + ' · UTC' +
      (since ? ' · after the latest status' : '');
    var empty = $('#history-empty'); empty.hidden = count > 0;
    empty.textContent = historyRows.length ? 'No included memories match these filters. Reset filters to see the included history; omitted history is not searched.' :
      'No memories are included yet. The record may be empty or its memories unavailable.';
    selectedHistory();
  }
  if (historyList) {
    historyControls.forEach(function (id) {
      var control = $('#' + id);
      if (control.tagName !== 'SELECT') { control.addEventListener('input', filterHistory); }
      control.addEventListener('change', function () {
        if (id === 'history-clock') { $('#history-order').value = control.value === 'event' ? 'oldest' : 'newest'; }
        filterHistory();
      });
    });
    $('#history-reset').addEventListener('click', function () {
      historyControls.forEach(function (id) { var el = $('#' + id); if (el.tagName === 'SELECT') { el.selectedIndex = 0; } else { el.value = ''; } });
      filterHistory();
    });
    filterHistory();
  }

  // the map: reading line and lit links
  var reading = $('#reading'), restText = reading ? reading.textContent : '';
  $$('.dot').forEach(function (dot) {
    function on() {
      var title = $('title', dot);
      reading.textContent = title ? title.textContent : '';
      var id = dot.getAttribute('data-m') || '';
      if (!MEMORY.test(id)) { return; }
      $$('.link[data-a="' + id + '"], .link[data-b="' + id + '"]').forEach(function (el) { el.classList.add('hot'); });
    }
    function off() {
      reading.textContent = restText;
      $$('.link.hot').forEach(function (el) { el.classList.remove('hot'); });
      if (opened) { mark(opened); }
    }
    dot.addEventListener('mouseenter', on); dot.addEventListener('focus', on);
    dot.addEventListener('mouseleave', off); dot.addEventListener('blur', off);
  });

  // ask: answers are sentences already in the page
  var q = $('#q'), out = $('#answers');
  var corpus = mems.filter(function (m) { return /^m-[0-9]+$/.test(m.id); }).map(function (m) {
    return { el: m, id: m.id.slice(2), text: m.textContent.toLowerCase(), kind: m.getAttribute('data-kind'),
             group: m.getAttribute('data-group') };
  }).reverse();
  var HEAD = [['stands', 'What stands'], ['open', 'Still open'], ['done', 'Answered, tried or taken up'],
              ['wrong', 'What went wrong'], ['changed', 'Replaced or set aside'], ['pause', 'Where we paused']];
  var SKIP = ' what did we the a an of in on to is are was were about do does have has had how why when where which who it that this and or for with our us any there ';
  function line(tag, cls, text) { var el = document.createElement(tag); el.className = cls; el.textContent = text; return el; }
  function answerNode(c) {
    var box = document.createElement('div');
    box.className = 'answer k-' + c.kind;
    box.appendChild($('.meta', c.el).cloneNode(true));
    var own = $('.big', c.el).textContent, passage = $('.ans', c.el).textContent;
    var a = line('a', 'said', own);
    a.setAttribute('href', '#m-' + c.id); a.setAttribute('data-m', c.id);
    box.appendChild(a);
    if (passage && passage !== own) { box.appendChild(line('p', 'quiet', passage)); }
    return box;
  }
  function render(list, lead, grouped) {
    while (out.firstChild) { out.removeChild(out.firstChild); }
    out.appendChild(line('p', 'found', lead));
    if (!grouped) { list.forEach(function (c) { out.appendChild(answerNode(c)); }); return; }
    HEAD.forEach(function (pair) {
      var part = list.filter(function (c) { return c.group === pair[0]; });
      if (!part.length) { return; }
      out.appendChild(line('h3', '', pair[1]));
      part.forEach(function (c) { out.appendChild(answerNode(c)); });
    });
  }
  function count(n) { return n === 1 ? 'One memory mentions ' : n + ' memories mention '; }
  function ask() {
    var raw = q.value.trim();
    var words = raw.toLowerCase().split(/[^a-z0-9]+/).filter(function (w) {
      return w.length > 1 && SKIP.indexOf(' ' + w + ' ') < 0;
    }).map(function (w) { return w.length > 4 ? w.replace(/(ing|ed|es|s|e)$/, '') : w; });
    if (!words.length) { while (out.firstChild) { out.removeChild(out.firstChild); } return; }
    var hits = corpus.filter(function (c) { return words.every(function (w) { return c.text.indexOf(w) >= 0; }); });
    if (!hits.length) {
      render([], 'Nothing in this mind mentions “' + raw + '”. Try one plain word, or one of the questions above.', false);
      return;
    }
    render(hits, count(hits.length) + '“' + raw + '”.', true);
  }
  var ASK = {
    decided: [function (c) { return c.kind === 'decision' && c.group === 'stands'; }, 'These decisions still stand, newest first.'],
    wrong: [function (c) { return c.kind === 'failure'; }, 'Everything that went wrong, newest first, with what it led to.'],
    open: [function (c) { return c.group === 'open'; }, 'These thoughts are still open.'],
    changed: [function (c) { return c.group === 'changed'; }, 'These were replaced or set aside. Each is told with what took its place.']
  };
  if (q) {
    q.addEventListener('input', ask);
    $$('[data-ask]').forEach(function (b) {
      b.addEventListener('click', function () {
        var rule = ASK[b.getAttribute('data-ask')];
        q.value = '';
        render(corpus.filter(rule[0]), rule[1], false);
      });
    });
  }

  // the manuscript read as a paper: which text the sheet shows (the changes since a version, or a clean copy), which version, the
  // next change, the boxes of history and the copy buttons. Everything it shows is already in the page; the script holds no record
  // text, and a version number is read as a whole number before it is used.
  var paper = $('#paper'), VERSION = /^[0-9]{1,6}$/, MODES = ['changes', 'clean'];
  function tokens(el, name) { return (el.getAttribute(name) || '').split(' ').filter(function (t) { return VERSION.test(t); }); }
  function atTop(fn) {
    // keep what the reader is looking at where it is while the text around it changes
    var host = null, top = 0;
    $$('.partrow, .rw, .sechead', paper).some(function (el) {
      var r = el.getBoundingClientRect();
      if (r.bottom > 90 && el.offsetParent !== null) { host = el; top = r.top; return true; }
      return false;
    });
    fn();
    if (host) { window.scrollBy(0, host.getBoundingClientRect().top - top); }
  }
  function pickVersion(k) {
    if (!paper || !VERSION.test(k || '')) { return; }
    atTop(function () {
      $$('[data-for]', paper).forEach(function (el) { el.classList.toggle('on', tokens(el, 'data-for').indexOf(k) >= 0); });
      $$('.kv', paper).forEach(function (el) { el.textContent = k; });
      $$('.rw', paper).forEach(function (row) {
        var found = null;
        ['added', 'reworded', 'changed'].forEach(function (kind) {
          var has = tokens(row, 'data-' + kind).indexOf(k) >= 0;
          row.classList.toggle('r-' + kind, false);
          if (has && !found) { found = kind; }
        });
        if (found) { row.classList.add('r-' + found); }
      });
      sideNotes();
    });
  }
  function setMode(mode) {
    if (!paper || MODES.indexOf(mode) < 0) { return; }
    atTop(function () { paper.setAttribute('data-mode', mode); sideNotes(); });
  }
  function changes() {
    return $$('.rw.r-added, .rw.r-reworded, .rw.r-changed, .r-removed.on', paper).filter(function (el) { return el.offsetParent !== null; });
  }
  function goChange(step) {
    var bar = $('.sbar', paper), offset = (bar ? bar.offsetHeight : 0) + 16, list = changes(), target = null;
    if (step > 0) {
      target = list.filter(function (el) { return el.getBoundingClientRect().top > offset + 1; })[0];
    } else {
      list = list.filter(function (el) { return el.getBoundingClientRect().top < offset - 1; });
      target = list[list.length - 1];
    }
    if (!target) { say(step > 0 ? 'No later change' : 'No earlier change'); return; }
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollBy({ top: target.getBoundingClientRect().top - offset, behavior: calm ? 'auto' : 'smooth' });
    say('Change ' + (changes().indexOf(target) + 1) + ' of ' + changes().length);
  }
  // The notes of the margin sit beside their rows. A note taller than its row would leave a hole in the text, so each note
  // takes no room of its own (its bottom margin is as negative as it is tall) and a note that would overlap the one before it
  // is moved down, with no change to the text. Without the script, or in a narrow window, the layout is the style's.
  var laid = '', soon = 0;
  function sideNotes() {
    var host = $('#paper-text', paper), wide = window.matchMedia && window.matchMedia('(min-width: 1181px)').matches;
    if (!host) { return; }
    var units = $$('.row > .mn, .row > .mnotes', host);
    units.forEach(function (u) { u.style.position = ''; u.style.top = ''; u.style.marginBottom = ''; });
    host.style.paddingBottom = '';
    if (!wide) { return; }
    var shown = units.filter(function (u) { return u.offsetHeight > 0; });
    var heights = shown.map(function (u) { return u.offsetHeight; });
    shown.forEach(function (u, i) { u.style.marginBottom = (-heights[i]) + 'px'; });
    var bottom = -1e9, base = window.pageYOffset;
    shown.forEach(function (u, i) {
      var top = u.getBoundingClientRect().top + base, push = Math.max(0, bottom + 10 - top);
      if (push > 0) { u.style.position = 'relative'; u.style.top = push + 'px'; }
      bottom = top + push + heights[i];
    });
    var end = host.getBoundingClientRect().bottom + base;
    if (bottom > end) { host.style.paddingBottom = Math.ceil(bottom - end + 10) + 'px'; }
    laid = host.offsetWidth + 'x' + host.offsetHeight;
  }
  function toggleBox(id) {
    if (!ID.test(id || '')) { return; }
    var box = document.getElementById(id);
    if (!box) { return; }
    box.hidden = !box.hidden;
    var opener = $('[aria-controls="' + id + '"]');
    if (opener) { opener.setAttribute('aria-expanded', box.hidden ? 'false' : 'true'); }
    if (!box.hidden) { box.focus(); } else if (opener) { opener.focus(); }
  }
  var told = $('#copystatus');
  function say(words) {
    // the text is cleared first, so that the same words said twice are announced twice
    if (!told) { return; }
    told.textContent = '';
    setTimeout(function () { told.textContent = words; }, 50);
  }
  function copyRequest(button) {
    var req = button.previousElementSibling;
    if (!req || !req.classList.contains('req')) { return; }
    var text = req.textContent, shown = req.nextElementSibling;
    function failed() {
      var box = document.createElement('textarea');
      box.className = 'copybox'; box.readOnly = true; box.value = text; box.rows = 2;
      box.setAttribute('aria-label', 'The request, to copy by hand');
      if (shown && shown.classList.contains('copybox')) { shown.parentNode.replaceChild(box, shown); }
      else { req.parentNode.insertBefore(box, req.nextSibling); }
      box.focus(); box.select();
      say('Copy did not work; press Ctrl+C.');
    }
    copyText(text, function () { say('Request copied. Paste it in the chat.'); }, failed);
  }
  function copyText(text, copied, failed) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(copied, failed);
      return;
    }
    var area = document.createElement('textarea'), previous = document.activeElement;
    area.value = text; area.setAttribute('readonly', 'readonly'); area.className = 'copyaside';
    document.body.appendChild(area); area.select();
    var done = false;
    try { done = document.execCommand('copy'); } catch (error) { done = false; }
    document.body.removeChild(area);
    if (previous && previous.focus) { previous.focus({preventScroll: true}); }
    if (done) { copied(); } else { failed(); }
  }
  $$('.impact-copy').forEach(function (button) {
    button.addEventListener('click', function () {
      var prompt = button.closest('details'), text = $('code', prompt), status = $('[aria-live]', prompt);
      copyText(text.textContent, function () { status.textContent = 'Copied. Paste into your project chat.'; }, function () {
        var selection = window.getSelection(), range = document.createRange();
        range.selectNodeContents(text);
        selection.removeAllRanges(); selection.addRange(range);
        status.textContent = 'Use your device’s Copy command to copy the selected request.';
      });
    });
  });
  var handoffCopy = $('#handoff-copy');
  if (handoffCopy) {
    var handoff = $('#handoff-text'), handoffPreview = $('#handoff-preview'),
        manualCopy = $('#handoff-manual-copy'), copyStatus = $('#handoff-copy-status');
    function restoreHandoff() {
      manualCopy.hidden = true;
      handoff.hidden = false;
    }
    handoffPreview.addEventListener('toggle', function () {
      if (!handoffPreview.open) restoreHandoff();
    });
    handoffCopy.addEventListener('click', function () {
      var portable = handoff.cloneNode(true);
      $$('a', portable).forEach(function (link) { link.textContent = link.getAttribute('href'); });
      var text = portable.textContent;
      copyText(text, function () {
        restoreHandoff();
        copyStatus.textContent = 'Handoff copied.';
      }, function () {
        manualCopy.textContent = text;
        handoff.hidden = true;
        manualCopy.hidden = false;
        handoffPreview.open = true;
        manualCopy.focus();
        var selection = window.getSelection(), range = document.createRange();
        range.selectNodeContents(manualCopy);
        selection.removeAllRanges(); selection.addRange(range);
        copyStatus.textContent = 'Use your device’s Copy command to copy the selected handoff.';
      });
    });
  }
  if (paper) {
    $$('.histbox', paper).forEach(function (box) { box.hidden = true; });
    if (narrow()) { $$('.legend', paper).forEach(function (d) { d.open = false; }); }
    // the longer lists of states in a part's note start folded, so that a note stays short at the margin; they are built open
    $$('.stmore', paper).forEach(function (d) { d.open = false; });
    var pick = $('#pick');
    if (pick) {
      pick.addEventListener('change', function () {
        var changes = $('input[name=pmode][value=changes]', paper);
        if (paper.getAttribute('data-mode') === 'clean' && changes) { changes.checked = true; setMode('changes'); }
        pickVersion(pick.value);
      });
    }
    $$('input[name=pmode]', paper).forEach(function (radio) {
      radio.addEventListener('change', function () { if (radio.checked) { setMode(radio.value); } });
    });
    var prev = $('#chgprev'), next = $('#chgnext');
    if (prev) { prev.addEventListener('click', function () { goChange(-1); }); }
    if (next) { next.addEventListener('click', function () { goChange(1); }); }
    document.addEventListener('click', function (event) {
      var t = event.target && event.target.closest ? event.target.closest('[data-toggle]') : null;
      if (t) { event.preventDefault(); toggleBox(t.getAttribute('data-toggle')); return; }
      var c = event.target && event.target.closest ? event.target.closest('[data-copy]') : null;
      if (c) { event.preventDefault(); copyRequest(c); }
    });
    // the sections in the side bar, marked as the paper is read
    if ('IntersectionObserver' in window) {
      var spy = new IntersectionObserver(function (found) {
        found.forEach(function (entry) {
          if (!entry.isIntersecting) { return; }
          var head = $('.sech', entry.target), id = head ? head.id : '';
          if (!ID.test(id)) { return; }
          $$('.subnav a').forEach(function (a) {
            var here = a.getAttribute('href') === '#' + id;
            a.classList.toggle('here', here);
            if (here) { a.setAttribute('aria-current', 'location'); } else { a.removeAttribute('aria-current'); }
          });
        });
      }, { rootMargin: '-120px 0px -70% 0px' });
      $$('.psec', paper).forEach(function (section) { spy.observe(section); });
    }
    // the notes of the margin, laid out beside the text (see sideNotes)
    var side = window.matchMedia ? window.matchMedia('(min-width: 1181px)') : null;
    if (side) { if (side.addEventListener) { side.addEventListener('change', sideNotes); } else if (side.addListener) { side.addListener(sideNotes); } }
    // what a jump leaves clear at the top is the height of the bar, which grows with a long line on a narrow window
    var bar = $('.sbar', paper);
    function barPad() { if (bar) { root.style.setProperty('--barpad', (bar.offsetHeight + 24) + 'px'); } }
    if ('ResizeObserver' in window && $('#paper-text', paper)) {
      var watch = new ResizeObserver(function () {
        barPad();
        var host = $('#paper-text', paper), now = host.offsetWidth + 'x' + host.offsetHeight;
        if (now === laid || soon) { return; }
        soon = window.requestAnimationFrame(function () { soon = 0; sideNotes(); });
      });
      watch.observe($('#paper-text', paper));
      if (bar) { watch.observe(bar); }
    }
    barPad();
    sideNotes();
    // a fold opened or shut changes the height of a note: lay the notes out again (the event does not bubble, so it is caught on the way down)
    paper.addEventListener('toggle', function () { sideNotes(); }, true);
    // a link to a history box opens it (the boxes were shut above, after the first route)
    if (/^#hist-[0-9]+-[0-9]+$/.test(location.hash)) { route(); }
  }
  // a window that was narrow and is wide again shows the project tools
  var wide = window.matchMedia ? window.matchMedia('(max-width: 860px)') : null;
  function widened(event) { var side = $('.side'); if (side && !event.matches) { setShut(side, false); } }
  if (wide) { if (wide.addEventListener) { wide.addEventListener('change', widened); } else if (wide.addListener) { wide.addListener(widened); } }


  // first visit
  var tour = $('#tour'), steps = $$('.step', tour), at = 0;
  function step(i) {
    at = Math.max(0, Math.min(steps.length - 1, i));
    steps.forEach(function (s, n) { s.hidden = n !== at; });
    $('#tourn').textContent = (at + 1) + ' of ' + steps.length;
    $('#tourback').disabled = at === 0;
    $('#tournext').textContent = at === steps.length - 1 ? 'Done' : 'Next';
    var go = steps[at].getAttribute('data-go') || '';
    if (PLACE.test(go) && location.hash !== go) { location.hash = go; }
  }
  function endTour() { tour.hidden = true; }
  $('#tourstart').addEventListener('click', function () { tour.hidden = false; step(0); });
  $('#tourback').addEventListener('click', function () { step(at - 1); });
  $('#tournext').addEventListener('click', function () { if (at === steps.length - 1) { endTour(); } else { step(at + 1); } });
  $('#tourend').addEventListener('click', endTour);

  $('#theme').addEventListener('click', function () {
    var dark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });
  document.addEventListener('click', function (event) {
    var button = event.target && event.target.closest ? event.target.closest('[data-print]') : null;
    if (button && window.print) { window.print(); }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      if (box.open) { return; }
      var shownBox = paper && document.activeElement && document.activeElement.closest ? document.activeElement.closest('.histbox') : null;
      if (shownBox && !shownBox.hidden) { toggleBox(shownBox.id); return; }
      if (opened) { closeMem(); } else if (!tour.hidden) { endTour(); }
    }
    if (event.key === '/' && q && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) { event.preventDefault(); location.hash = '#ask'; q.focus(); }
  });
}());

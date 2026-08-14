/*
 * Challenge arena controller: editor, sandboxed preview, countdown, checks.
 *
 * The round is CSS only. `#wf-html` is a readonly view of the fixed markup --
 * it is never bound to the editor handlers and never posted to the server.
 *
 * Nothing here is authoritative. The countdown is a display; `remaining`,
 * the score, Design Mode, the hint count and the final submission all come
 * from the server, which refuses saves, checks, resets and hints once the
 * deadline has passed. Editing numbers in devtools buys the player nothing.
 */
(function () {
  'use strict';

  var config = JSON.parse(document.getElementById('wf-arena-data').textContent);
  var state = config.state;
  var timerConfig = config.timer;
  var urls = config.urls;

  var PREVIEW_DEBOUNCE_MS = 400;
  var AUTOSAVE_DEBOUNCE_MS = 1200;

  // Virtual viewport widths for the previews (see fitFrame).
  var DESKTOP_WIDTH = 1120;
  var PHONE_WIDTH = 390;

  var HINT_LEVELS = 3;
  var LEVEL_NAMES = ['the idea', 'where to look', 'which property'];

  var el = {
    timer: document.getElementById('wf-timer'),
    timerValue: document.getElementById('wf-timer-value'),
    progressCount: document.getElementById('wf-progress-count'),
    progressFill: document.getElementById('wf-progress-fill'),
    hintCount: document.getElementById('wf-hint-count'),
    html: document.getElementById('wf-html'),
    css: document.getElementById('wf-css'),
    preview: document.getElementById('wf-preview'),
    previewWrap: document.getElementById('wf-preview-wrap'),
    run: document.getElementById('wf-run'),
    reset: document.getElementById('wf-reset'),
    saveState: document.getElementById('wf-save-state'),
    editorHint: document.getElementById('wf-editor-hint'),
    objectives: document.getElementById('wf-objectives'),
    mission: document.getElementById('wf-mission'),
    designBanner: document.getElementById('wf-design-banner'),

    finalOpen: document.getElementById('wf-final-open'),
    finalClose: document.getElementById('wf-final-close'),
    final: document.getElementById('wf-final'),
    finalWrap: document.getElementById('wf-final-wrap'),
    finalFrame: document.getElementById('wf-final-frame'),
    finalTitle: document.getElementById('wf-final-title'),
    finalTag: document.getElementById('wf-final-tag'),
    finalFoot: document.getElementById('wf-final-foot'),

    modal: document.getElementById('wf-modal'),
    modalBox: document.getElementById('wf-modal-box'),
    modalIcon: document.getElementById('wf-modal-icon'),
    modalTitle: document.getElementById('wf-modal-title'),
    modalHeadline: document.getElementById('wf-modal-headline'),
    modalText: document.getElementById('wf-modal-text'),
    modalObjectives: document.getElementById('wf-modal-objectives'),
    modalStatus: document.getElementById('wf-modal-status'),
    modalEligible: document.getElementById('wf-modal-eligible'),
    modalHints: document.getElementById('wf-modal-hints'),
    myDesign: document.getElementById('wf-my-design')
  };

  var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
  var endsAt = Date.now() + state.remaining * 1000;
  var lastRemaining = state.remaining;
  var locked = false;
  var previewTimer = null;
  var saveTimer = null;
  var cleared = {};

  // ------------------------------------------------------------- helpers --

  function post(url, payload) {
    var body = new URLSearchParams(payload || {});
    return fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRFToken': csrfToken,
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: body.toString(),
      credentials: 'same-origin'
    }).then(function (response) { return response.json(); });
  }

  function submission() {
    return { css: el.css.value };
  }

  function pad(value) { return value < 10 ? '0' + value : String(value); }

  function clock(seconds) {
    return pad(Math.floor(seconds / 60)) + ':' + pad(seconds % 60);
  }

  // ------------------------------------------------------------- preview --

  /*
   * NovaCloud is a complete HTML document, so it is rendered as written and
   * its <link rel="stylesheet"> is swapped for the player's live CSS. The
   * link is matched loosely so a player who moves it does not lose the view.
   */
  var STYLESHEET_LINK = /<link\b[^>]*href\s*=\s*["'][^"']*style\.css["'][^>]*>/i;

  function previewDocument() {
    var html = el.html.value;
    var style = '<style>' + el.css.value + '</style>';

    if (STYLESHEET_LINK.test(html)) {
      return html.replace(STYLESHEET_LINK, style);
    }
    if (/<\/head\s*>/i.test(html)) {
      return html.replace(/<\/head\s*>/i, style + '</head>');
    }
    return '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width,initial-scale=1">' +
      style + '</head><body>' + html + '</body></html>';
  }

  function renderPreview() {
    // The player's page lives in a sandboxed iframe: no scripts, no access to
    // this document, and its CSS cannot reach the game shell around it.
    el.preview.srcdoc = previewDocument();
  }

  function queuePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderPreview, PREVIEW_DEBOUNCE_MS);
  }

  /*
   * Render at a fixed virtual width and scale to fit. Sizing the iframe to
   * the panel instead would sit permanently inside NovaCloud's own 860px
   * breakpoint and hide the desktop-only mistakes.
   */
  function fitFrame(wrap, frame) {
    var phone = wrap.getAttribute('data-width') === 'phone';
    var virtual = phone ? PHONE_WIDTH : DESKTOP_WIDTH;
    var width = wrap.clientWidth;
    var height = wrap.clientHeight;
    if (!width || !height) { return; }

    var scale = Math.min(1, width / virtual);
    frame.style.width = virtual + 'px';
    frame.style.height = Math.round(height / scale) + 'px';
    frame.style.transform = 'scale(' + scale + ')';
    frame.style.left = Math.max(0, (width - virtual * scale) / 2) + 'px';
  }

  function fitPreview() { fitFrame(el.previewWrap, el.preview); }
  function fitFinal() { fitFrame(el.finalWrap, el.finalFrame); }

  // --------------------------------------------------------------- saving --

  function setSaveState(value, text) {
    el.saveState.setAttribute('data-state', value);
    el.saveState.textContent = text;
  }

  function save() {
    if (locked) { return Promise.resolve(); }
    setSaveState('saving', 'saving…');
    return post(urls.save, submission()).then(function (data) {
      applyState(data);
      setSaveState(data.error ? 'error' : 'saved', data.error || 'saved');
    }).catch(function () {
      setSaveState('error', 'offline — retrying');
    });
  }

  function queueSave() {
    clearTimeout(saveTimer);
    setSaveState('dirty', 'unsaved changes');
    saveTimer = setTimeout(save, AUTOSAVE_DEBOUNCE_MS);
  }

  // ---------------------------------------------------------------- timer --

  function paintTimer(seconds) {
    el.timerValue.textContent = clock(seconds);
    var mode = 'normal';
    if (seconds <= 0) { mode = 'over'; }
    else if (seconds <= timerConfig.danger) { mode = 'danger'; }
    else if (seconds <= timerConfig.warning) { mode = 'warning'; }
    el.timer.setAttribute('data-state', mode);
  }

  function tick() {
    var seconds = Math.max(0, Math.round((endsAt - Date.now()) / 1000));
    lastRemaining = seconds;
    paintTimer(seconds);
    // The browser never decides the round is over -- it asks the server.
    if (seconds <= 0 && !locked) { syncState(); }
  }

  function syncState() {
    return fetch(urls.state, { credentials: 'same-origin' })
      .then(function (response) { return response.json(); })
      .then(applyState)
      .catch(function () { /* offline: keep counting locally until it returns */ });
  }

  function applyState(data) {
    if (!data || typeof data.remaining !== 'number') { return; }
    endsAt = Date.now() + data.remaining * 1000;
    lastRemaining = data.remaining;
    paintTimer(data.remaining);

    if (typeof data.hintsUsed === 'number') {
      el.hintCount.textContent = data.hintsUsed;
    }
    if (data.designMode) { showDesignMode(); }

    // Clearing every objective opens Design Mode; only the deadline locks.
    if (data.expired) { lock(data); }
  }

  function showDesignMode() {
    if (el.designBanner.hidden) {
      el.designBanner.hidden = false;
      el.mission.hidden = true;
    }
  }

  // ----------------------------------------------------------- objectives --

  function paintChecks(checks, passed) {
    checks.forEach(function (check) {
      var node = el.objectives.querySelector('[data-check-id="' + check.id + '"]');
      if (!node) { return; }

      if (check.passed && !cleared[check.id]) {
        cleared[check.id] = true;
        node.classList.add('is-just-done');
        setTimeout(function () { node.classList.remove('is-just-done'); }, 700);
      }
      if (!check.passed) { cleared[check.id] = false; }

      node.classList.toggle('is-done', check.passed);
      node.querySelector('.wf-objective__mark').textContent = check.passed ? '✓' : '';
    });

    el.progressCount.textContent = passed + '/' + state.total;
    el.progressFill.style.width = (passed / state.total * 100) + '%';
  }

  function runChecks() {
    if (locked) { return; }
    el.run.disabled = true;
    el.run.textContent = 'Checking…';
    post(urls.check, submission()).then(function (data) {
      var passed = data.passed !== undefined ? data.passed : (data.score || 0);
      if (data.checks) { paintChecks(data.checks, passed); }
      setSaveState('saved', 'saved');
      applyState(data);
      if (!data.expired) {
        var left = state.total - passed;
        setSaveState('saved', left === 0
          ? 'all objectives clear — design freely'
          : left + ' objective' + (left === 1 ? '' : 's') + ' left');
      }
    }).catch(function () {
      setSaveState('error', 'check failed — try again');
    }).finally(function () {
      if (!locked) { el.run.disabled = false; }
      el.run.textContent = 'Run checks';
    });
  }

  // ---------------------------------------------------------------- hints --

  /*
   * Hints live on the server. Asking for one records that it was revealed --
   * the first time only -- and returns the text. Nothing here can change the
   * count; the browser renders whatever the server sends back.
   */
  function paintHint(objective, level, html) {
    var box = el.objectives.querySelector('[data-hints-for="' + objective + '"]');
    if (!box || box.querySelector('[data-hint-level="' + level + '"]')) { return; }

    var node = document.createElement('p');
    node.className = 'wf-objective__hint';
    node.setAttribute('data-hint-level', level);
    node.innerHTML = '<b>Hint ' + level + ' · ' + LEVEL_NAMES[level - 1] + '</b>' + html;
    box.appendChild(node);

    var button = el.objectives.querySelector('.wf-hint-btn[data-hint-for="' + objective + '"]');
    if (!button) { return; }
    var shown = box.querySelectorAll('.wf-objective__hint').length;
    if (shown >= HINT_LEVELS) {
      button.hidden = true;
    } else {
      button.textContent = 'Show hint ' + (shown + 1) + ' of ' + HINT_LEVELS;
    }
  }

  function revealHint(button) {
    if (locked) { return; }
    var objective = button.getAttribute('data-hint-for');
    var box = el.objectives.querySelector('[data-hints-for="' + objective + '"]');
    var level = box.querySelectorAll('.wf-objective__hint').length + 1;
    if (level > HINT_LEVELS) { return; }

    button.disabled = true;
    post(urls.hint, { objective: objective, level: level }).then(function (data) {
      if (data.hintHtml) { paintHint(objective, data.level, data.hintHtml); }
      applyState(data);
    }).finally(function () {
      if (!locked) { button.disabled = false; }
    });
  }

  // --------------------------------------------------------------- ending --

  /*
   * The deadline, and only the deadline, ends the round. By the time this
   * runs the server has already snapshotted the stylesheet; this just
   * reports what it decided.
   */
  function lock(data) {
    if (locked) { return; }
    locked = true;
    clearTimeout(saveTimer);
    el.css.disabled = true;
    el.run.disabled = true;
    el.reset.disabled = true;
    Array.prototype.forEach.call(el.objectives.querySelectorAll('.wf-hint-btn'), function (button) {
      button.disabled = true;
    });
    paintTimer(0);

    var score = data.score || 0;
    var total = data.total || state.total;
    var eligible = !!data.eligible;

    el.modalBox.setAttribute('data-outcome', eligible ? 'win' : 'timeout');
    el.modalIcon.textContent = eligible ? '🏆' : '🏁';
    el.modalTitle.textContent = "Time's up";
    el.modalHeadline.textContent = score + ' / ' + total + ' objectives complete';
    el.modalText.textContent = eligible
      ? 'Design submitted. Your final NovaCloud design has been sent for judging, '
        + 'and your CSS is now locked.'
      : 'Your challenge has ended and your CSS has been locked. You did not complete '
        + 'all ' + total + ' objectives, so this submission is not eligible for the '
        + 'final design competition.';
    el.modalObjectives.textContent = score + ' / ' + total;
    el.modalStatus.textContent = eligible ? 'SUBMITTED' : 'EXPIRED';
    el.modalEligible.textContent = eligible ? 'YES' : 'NO';
    el.modalHints.textContent = data.hintsUsed || 0;
    el.modal.hidden = false;
    el.myDesign.focus();
  }

  // ------------------------------------------------------- final previews --

  /*
   * Two different things, deliberately kept apart:
   *   reference -- the official finished NovaCloud (markup + solution.css)
   *   mine      -- the player's own submitted entry (markup + their final CSS)
   * Both are composed server-side and loaded by URL into a sandboxed iframe,
   * so neither stylesheet can reach the game shell.
   */
  var VIEWS = {
    reference: {
      url: urls.finalPreview,
      title: 'Final preview',
      tag: 'the official finished NovaCloud, for reference',
      foot: 'Reference only — nothing here is graded, and your clock keeps running. '
        + 'Compare it with your preview, then fix style.css.'
    },
    mine: {
      url: urls.finalDesign,
      title: 'My final design',
      tag: 'your submitted competition entry',
      foot: 'This is the design captured when your time ran out. It is what the '
        + 'judges will see.'
    }
  };

  var finalKind = null;

  function openFinal(kind) {
    var view = VIEWS[kind];
    if (finalKind !== kind) {
      el.finalFrame.src = view.url;
      finalKind = kind;
    }
    el.finalTitle.textContent = view.title;
    el.finalTag.textContent = view.tag;
    el.finalFoot.textContent = view.foot;
    el.final.hidden = false;
    fitFinal();
    el.finalClose.focus();
  }

  function closeFinal() {
    el.final.hidden = true;
    (locked ? el.myDesign : el.finalOpen).focus();
  }

  // ----------------------------------------------------------------- wire --

  function bindEditor(area) {
    area.addEventListener('input', function () { queuePreview(); queueSave(); });
    area.addEventListener('keydown', function (event) {
      if (event.key !== 'Tab') { return; }
      event.preventDefault();
      var start = area.selectionStart;
      var end = area.selectionEnd;
      area.value = area.value.slice(0, start) + '  ' + area.value.slice(end);
      area.selectionStart = area.selectionEnd = start + 2;
      queuePreview();
      queueSave();
    });
  }

  bindEditor(el.css);  // the HTML pane is readonly and stays unbound

  Array.prototype.forEach.call(document.querySelectorAll('[data-tab]'), function (tab) {
    tab.addEventListener('click', function () {
      var target = tab.getAttribute('data-tab');
      Array.prototype.forEach.call(document.querySelectorAll('[data-tab]'), function (other) {
        other.setAttribute('aria-selected', String(other === tab));
      });
      el.html.hidden = target !== 'html';
      el.css.hidden = target !== 'css';
      el.editorHint.textContent = target === 'html'
        ? 'index.html is fixed for this round — read it, do not edit it'
        : 'Tab = 2 spaces · Ctrl+Enter = run checks';
      (target === 'html' ? el.html : el.css).focus();
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll('[data-width]'), function (button) {
    button.addEventListener('click', function () {
      var width = button.getAttribute('data-width');
      Array.prototype.forEach.call(document.querySelectorAll('[data-width]'), function (other) {
        other.setAttribute('aria-pressed', String(other === button));
      });
      el.previewWrap.setAttribute('data-width', width);
      fitPreview();
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll('[data-final-width]'), function (button) {
    button.addEventListener('click', function () {
      Array.prototype.forEach.call(document.querySelectorAll('[data-final-width]'), function (other) {
        other.setAttribute('aria-pressed', String(other === button));
      });
      el.finalWrap.setAttribute('data-width', button.getAttribute('data-final-width'));
      fitFinal();
    });
  });

  el.objectives.addEventListener('click', function (event) {
    var button = event.target.closest('.wf-hint-btn');
    if (button) { revealHint(button); }
  });

  el.run.addEventListener('click', runChecks);

  el.reset.addEventListener('click', function () {
    if (locked || !window.confirm('Restore the original broken style.css? Your edits will be lost.')) { return; }
    post(urls.reset, {}).then(function (data) {
      if (data.css !== undefined) {
        el.css.value = data.css;
        renderPreview();
        setSaveState('saved', 'reset to the broken stylesheet');
      }
      applyState(data);
    });
  });

  el.finalOpen.addEventListener('click', function () { openFinal('reference'); });
  el.myDesign.addEventListener('click', function () { openFinal('mine'); });
  el.finalClose.addEventListener('click', closeFinal);

  el.final.addEventListener('click', function (event) {
    if (event.target === el.final) { closeFinal(); }  // click the backdrop
  });

  window.addEventListener('resize', function () { fitPreview(); fitFinal(); });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !el.final.hidden) { closeFinal(); return; }
    if (!(event.ctrlKey || event.metaKey)) { return; }
    if (event.key === 'Enter') { event.preventDefault(); runChecks(); }
    if (event.key.toLowerCase() === 's') { event.preventDefault(); clearTimeout(saveTimer); save(); }
  });

  window.addEventListener('focus', syncState);

  // ----------------------------------------------------------------- boot --

  Array.prototype.forEach.call(el.objectives.querySelectorAll('.wf-objective.is-done'), function (item) {
    cleared[item.getAttribute('data-check-id')] = true;
  });

  // Hints bought earlier come back with the page, already paid for.
  (config.revealed || []).forEach(function (entry) {
    paintHint(entry.objective, entry.level, entry.html);
  });

  renderPreview();
  fitPreview();
  paintTimer(state.remaining);
  setInterval(tick, 250);
  setInterval(syncState, timerConfig.sync * 1000);

  if (state.designMode) { showDesignMode(); }
  if (state.expired) { lock(state); }
}());

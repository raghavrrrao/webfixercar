/* The live scoreboard client.
 *
 * The server owns every number on this screen. This file opens a WebSocket,
 * renders the snapshot it is handed, patches the one row each event names,
 * and reconnects with a fresh snapshot when the connection drops. It never
 * computes a score, never decides a status, and never touches a race.
 *
 * The only thing it does on its own is tick the clock of a running race
 * between server messages, and even that is re-anchored to the server every
 * time an event for that participant arrives.
 */
(function () {
  'use strict';

  function json(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : null;
  }

  var boot = json('wf-scoreboard-boot');
  if (!boot) { return; }

  var MODE = boot.mode;
  var TERMINAL = { completed: true, expired: true };
  var STATUS = {
    not_started: { label: 'NOT STARTED', mark: '○' },
    active: { label: 'RACING', mark: '●' },
    completed: { label: 'COMPLETE', mark: '✓' },
    expired: { label: "TIME'S UP", mark: '×' }
  };
  var FEED_LABELS = {
    race_started: { icon: '▶', text: 'started the race' },
    repair_collected: { icon: '✓', text: 'collected a CSS repair' },
    collision: { icon: '⚠', text: 'traffic impact' },
    race_completed: { icon: '★', text: 'CSS FIX COMPLETE' },
    race_expired: { icon: '×', text: "time's up" }
  };

  var el = {
    body: document.getElementById('wf-sb-body'),
    empty: document.getElementById('wf-sb-empty'),
    link: document.getElementById('wf-sb-link'),
    feed: document.getElementById('wf-sb-feed'),
    spotlight: document.getElementById('wf-sb-spotlight'),
    counts: {
      racing: document.getElementById('wf-sb-count-racing'),
      complete: document.getElementById('wf-sb-count-complete'),
      expired: document.getElementById('wf-sb-count-expired'),
      total: document.getElementById('wf-sb-count-total')
    },
    conn: document.getElementById('wf-sb-conn')
  };

  // The last state the *server* sent for each participant, keyed by their id.
  var players = new Map();
  var rows = new Map();
  var seenEvents = [];          // recent event ids, to swallow replays
  var anchoredAt = new Map();   // local clock reading when each state arrived

  function fmtClock(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' +
           String(seconds % 60).padStart(2, '0');
  }
  function now() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  /* Elapsed for display only. A finished run is frozen at what it scored and
   * a timed-out one at the full duration; a running race ticks forward from
   * the last server reading until the next one replaces it. */
  function shownElapsed(player) {
    var state = player.state;
    if (TERMINAL[state.status]) { return state.elapsed; }
    if (state.status !== 'active') { return 0; }
    var since = (now() - (anchoredAt.get(player.participant_id) || now())) / 1000;
    return Math.min(state.duration, state.elapsed + since);
  }

  // ------------------------------------------------------------- rendering

  function statusCell(state) {
    var meta = STATUS[state.status] || STATUS.not_started;
    return '<span class="wf-sb-status is-' + state.status + '">' +
           '<i aria-hidden="true">' + meta.mark + '</i>' + meta.label + '</span>';
  }

  function buildRow(player) {
    var row = document.createElement(MODE === 'display' ? 'div' : 'tr');
    row.className = 'wf-sb-row';
    row.dataset.participant = player.participant_id;
    if (MODE !== 'display' && el.link) {
      row.tabIndex = 0;
      row.classList.add('is-clickable');
      var open = function () {
        window.location.href = el.link.dataset.href.replace('0', player.participant_id);
      };
      row.addEventListener('click', open);
      row.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
      });
    }
    return row;
  }

  function cells(player) {
    var s = player.state;
    var cell = MODE === 'display' ? 'div' : 'td';
    var open = '<' + cell, close = '</' + cell + '>';
    return [
      open + ' class="wf-sb-player">' + escape(player.player) + close,
      open + ' class="wf-sb-pc">' + escape(player.pc_no || '—') + close,
      open + '>' + statusCell(s) + close,
      open + ' class="wf-sb-num" data-clock>' + fmtClock(shownElapsed(player)) + close,
      open + ' class="wf-sb-num">' + s.repairs + '/' + s.repair_total + close,
      open + ' class="wf-sb-num">' + s.penalties + close,
      open + ' class="wf-sb-num wf-sb-score">' + s.score + close,
      open + ' class="wf-sb-section">' + escape(s.section_label) + close
    ].join('');
  }

  function escape(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function paintRow(player, flash) {
    var row = rows.get(player.participant_id);
    if (!row) {
      row = buildRow(player);
      rows.set(player.participant_id, row);
    }
    row.innerHTML = cells(player);
    row.dataset.status = player.state.status;
    if (flash) {
      row.classList.remove('is-changed');
      void row.offsetWidth;
      row.classList.add('is-changed');
    }
    return row;
  }

  /* The server sends the snapshot already ordered; events are merged into
   * that order so a row does not jump around mid-glance. */
  function order() {
    var rank = { completed: 0, active: 1, not_started: 2, expired: 3 };
    return Array.from(players.values()).sort(function (a, b) {
      var byStatus = rank[a.state.status] - rank[b.state.status];
      if (byStatus) { return byStatus; }
      if (a.state.status === 'completed') { return b.state.score - a.state.score; }
      if (a.state.status === 'active') {
        return (b.state.repairs - a.state.repairs) ||
               (b.state.distance - a.state.distance);
      }
      return a.player.toLowerCase().localeCompare(b.player.toLowerCase());
    });
  }

  function repaint() {
    var sorted = order();
    var fragment = document.createDocumentFragment();
    sorted.forEach(function (player) { fragment.appendChild(paintRow(player)); });
    el.body.innerHTML = '';
    el.body.appendChild(fragment);
    if (el.empty) { el.empty.hidden = sorted.length > 0; }
    paintCounts(sorted);
    paintSpotlight(sorted);
  }

  function paintCounts(sorted) {
    if (!el.counts.total) { return; }
    var tally = { active: 0, completed: 0, expired: 0 };
    sorted.forEach(function (p) {
      if (tally[p.state.status] !== undefined) { tally[p.state.status] += 1; }
    });
    el.counts.racing.textContent = tally.active;
    el.counts.complete.textContent = tally.completed;
    el.counts.expired.textContent = tally.expired;
    el.counts.total.textContent = sorted.length;
  }

  /* The projector highlights one live race at a time, rotating slowly. */
  var spotlightIndex = 0;
  function paintSpotlight(sorted) {
    if (!el.spotlight) { return; }
    var racing = sorted.filter(function (p) { return p.state.status === 'active'; });
    if (!racing.length) {
      el.spotlight.hidden = true;
      return;
    }
    spotlightIndex = spotlightIndex % racing.length;
    var player = racing[spotlightIndex];
    var s = player.state;
    el.spotlight.hidden = false;
    el.spotlight.innerHTML =
      '<p class="wf-sb-spot__kicker">ON THE COURSE</p>' +
      '<h2 class="wf-sb-spot__name">' + escape(player.player) + '</h2>' +
      '<p class="wf-sb-spot__pc">' + escape(player.pc_no || '') + '</p>' +
      '<div class="wf-sb-spot__grid">' +
        '<div><small>REPAIRS</small><b>' + s.repairs + ' / ' + s.repair_total + '</b></div>' +
        '<div><small>SCORE</small><b>' + s.score + '</b></div>' +
        '<div><small>TIME</small><b data-clock>' + fmtClock(shownElapsed(player)) + '</b></div>' +
        '<div><small>PENALTIES</small><b>' + s.penalties + '</b></div>' +
      '</div>' +
      '<p class="wf-sb-spot__section">' + escape(s.section_label) + '</p>';
  }
  setInterval(function () { spotlightIndex += 1; paintSpotlight(order()); }, 9000);

  /* Only the clock is animated locally, and only for running races. */
  setInterval(function () {
    players.forEach(function (player) {
      if (player.state.status !== 'active') { return; }
      var row = rows.get(player.participant_id);
      var cell = row && row.querySelector('[data-clock]');
      if (cell) { cell.textContent = fmtClock(shownElapsed(player)); }
    });
    var spot = el.spotlight && el.spotlight.querySelector('[data-clock]');
    if (spot) {
      var racing = order().filter(function (p) { return p.state.status === 'active'; });
      if (racing.length) {
        spot.textContent = fmtClock(shownElapsed(racing[spotlightIndex % racing.length]));
      }
    }
  }, 1000);

  // ------------------------------------------------------------- the feed

  function pushFeed(player, event) {
    if (!el.feed) { return; }
    var meta = FEED_LABELS[event];
    if (!meta) { return; }
    var item = document.createElement('li');
    item.className = 'wf-sb-feed__item is-' + event;
    item.innerHTML = '<i aria-hidden="true">' + meta.icon + '</i>' +
                     '<b>' + escape(player.player) + '</b>' +
                     '<span>' + meta.text + '</span>';
    el.feed.insertBefore(item, el.feed.firstChild);
    while (el.feed.children.length > 8) { el.feed.removeChild(el.feed.lastChild); }
  }

  // ------------------------------------------------------------ the socket

  function absorb(player) {
    players.set(player.participant_id, player);
    anchoredAt.set(player.participant_id, now());
  }

  function onSnapshot(message) {
    players.clear();
    rows.clear();
    message.players.forEach(absorb);
    repaint();
  }

  function onUpdate(message) {
    // A reconnect can replay an event the UI already applied. The id is built
    // from the resulting state, so an exact repeat is safe to drop — and the
    // state is applied either way, because it is the server's.
    if (message.event_id) {
      if (seenEvents.indexOf(message.event_id) >= 0) { return; }
      seenEvents.push(message.event_id);
      if (seenEvents.length > 200) { seenEvents.shift(); }
    }
    var known = players.has(message.participant_id);
    absorb({ participant_id: message.participant_id, player: message.player,
             pc_no: message.pc_no, state: message.state });
    if (!known) {
      repaint();            // a participant appearing for the first time
    } else {
      paintRow(players.get(message.participant_id), true);
      var sorted = order();
      paintCounts(sorted);
      // re-order only when the change could have moved the row
      if (message.event === 'race_started' || message.event === 'race_completed' ||
          message.event === 'race_expired' || message.event === 'repair_collected') {
        repaint();
      }
    }
    pushFeed(message, message.event);
  }

  var socket = null;
  var retry = 0;
  var heartbeat = null;

  function setConnection(state, text) {
    if (!el.conn) { return; }
    el.conn.dataset.state = state;
    el.conn.textContent = text;
  }

  function connect() {
    var scheme = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
    socket = new WebSocket(scheme + window.location.host + boot.socket);
    setConnection('connecting', 'CONNECTING…');

    socket.onopen = function () {
      retry = 0;
      setConnection('live', 'LIVE');
      clearInterval(heartbeat);
      heartbeat = setInterval(function () {
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping' }));
        }
      }, (boot.heartbeat || 20) * 1000);
    };

    socket.onmessage = function (frame) {
      var message;
      try { message = JSON.parse(frame.data); } catch (err) { return; }
      if (message.type === 'scoreboard_snapshot') { onSnapshot(message); }
      else if (message.type === 'race_update') { onUpdate(message); }
      else if (message.type === 'welcome' && message.heartbeat) {
        boot.heartbeat = message.heartbeat;
      }
    };

    socket.onclose = function (event) {
      clearInterval(heartbeat);
      if (event.code === 4403) {
        setConnection('denied', 'NOT AUTHORISED');
        return;                              // do not hammer a closed door
      }
      setConnection('lost', 'CONNECTION LOST — RECONNECTING');
      retry = Math.min(retry + 1, 6);
      // The database is the recovery mechanism: reconnecting asks for a fresh
      // snapshot rather than replaying whatever was missed.
      setTimeout(connect, Math.min(1000 * retry, 6000));
    };

    socket.onerror = function () { if (socket) { socket.close(); } };
  }

  // The page is server-rendered with a snapshot already, so the table is
  // correct before the socket has even opened.
  if (boot.snapshot) { onSnapshot(boot.snapshot); }
  connect();
}());

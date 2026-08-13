/*
 * Live "players online" counter.
 *
 * Talks to /ws/presence/ over WebSocket. The scheme is derived from the page
 * (https -> wss, http -> ws) so the same file works on localhost and Render.
 * Any page that renders a [data-presence] element gets the counter for free.
 */
(function () {
  'use strict';

  var containers = document.querySelectorAll('[data-presence]');
  if (!containers.length || !('WebSocket' in window)) { return; }

  var ENDPOINT = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/presence/';
  var MAX_BACKOFF_MS = 15000;

  var socket = null;
  var heartbeatTimer = null;
  var reconnectTimer = null;
  var heartbeatMs = 20000;
  var attempts = 0;
  var closing = false;

  function setState(state) {
    for (var i = 0; i < containers.length; i++) {
      containers[i].setAttribute('data-state', state);
    }
  }

  function setCount(count) {
    var numbers = document.querySelectorAll('[data-presence-count]');
    for (var i = 0; i < numbers.length; i++) {
      var el = numbers[i];
      if (el.textContent === String(count)) { continue; }
      el.textContent = count;
      el.classList.remove('is-bump');
      void el.offsetWidth; // restart the bump animation
      el.classList.add('is-bump');
    }
    var labels = document.querySelectorAll('[data-presence-label]');
    for (var j = 0; j < labels.length; j++) {
      labels[j].textContent = count === 1 ? 'player online' : 'players online';
    }
  }

  function stopHeartbeat() {
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
  }

  function startHeartbeat() {
    stopHeartbeat();
    heartbeatTimer = setInterval(function () {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'ping' }));
      }
    }, heartbeatMs);
  }

  function scheduleReconnect() {
    if (closing || reconnectTimer) { return; }
    var delay = Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, attempts++));
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    setState('connecting');
    try {
      socket = new WebSocket(ENDPOINT);
    } catch (err) {
      scheduleReconnect();
      return;
    }

    socket.onopen = function () {
      attempts = 0;
      setState('live');
      startHeartbeat();
    };

    socket.onmessage = function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (err) { return; }
      if (data.type === 'welcome' && data.heartbeat) {
        heartbeatMs = data.heartbeat * 1000;
        startHeartbeat();
      }
      if (typeof data.count === 'number') { setCount(data.count); }
    };

    socket.onclose = function () {
      stopHeartbeat();
      if (!closing) { setState('offline'); scheduleReconnect(); }
    };

    socket.onerror = function () {
      if (socket) { socket.close(); }
    };
  }

  // Close explicitly so the server drops us immediately instead of waiting
  // for the heartbeat to time out.
  window.addEventListener('pagehide', function () {
    closing = true;
    stopHeartbeat();
    if (socket && socket.readyState === WebSocket.OPEN) { socket.close(); }
  });

  connect();
}());

/* CSS Repair Race — the display and input half of the game.
 *
 * The server owns the race: when it starts, when it ends, which repairs count,
 * how far the car can possibly have got and what the score is. This file
 * drives the car, draws the course, reports what happened and renders whatever
 * the server says back. Where the two disagree, the server wins — every
 * response is fed through applyState() and overwrites what is on screen.
 *
 * Layout of this file:
 *   1  bootstrap      config, DOM, small helpers
 *   2  net            posting progress, and the single source of truth
 *   3  audio          a few Web Audio voices, no assets, muteable
 *   4  sprites        every vehicle and prop, drawn once into a canvas
 *   5  course         deterministic generation from the server's seed
 *   6  traffic        autonomous AI — it never reads the player
 *   7  player         input and physics — the only thing the keyboard touches
 *   8  render         projected pseudo-3D, one pass, far to near
 *   9  hud            DOM updated only when a value actually changes
 *  10  lifecycle      countdown, loop, finish, timeout
 *
 * See RENDERER-DECISION.md for why this is a 2D canvas and not Three.js.
 */
(function () {
  'use strict';

  // ============================================================ 1. bootstrap

  function json(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : null;
  }

  var urls = json('wf-race-urls');
  var C = json('wf-race-config');
  var initial = json('wf-race-state') || {};
  if (!urls || !C) { return; }

  var CAR = C.car;
  var el = {
    brief: document.getElementById('wf-brief'),
    race: document.getElementById('wf-race'),
    startBtn: document.getElementById('wf-start-race'),
    briefError: document.getElementById('wf-brief-error'),
    track: document.getElementById('wf-track'),
    canvas: document.getElementById('wf-canvas'),
    speed: document.getElementById('wf-speed'),
    section: document.getElementById('wf-hud-section'),
    progress: document.getElementById('wf-hud-progress'),
    repairs: document.getElementById('wf-hud-repairs'),
    penalties: document.getElementById('wf-hud-penalties'),
    score: document.getElementById('wf-hud-score'),
    timer: document.getElementById('wf-hud-timer'),
    mute: document.getElementById('wf-mute'),
    objective: document.getElementById('wf-objective'),
    sectionCallout: document.getElementById('wf-section-callout'),
    sectionTitle: document.getElementById('wf-section-title'),
    sectionCopy: document.getElementById('wf-section-copy'),
    impact: document.getElementById('wf-impact'),
    toast: document.getElementById('wf-toast'),
    toastTitle: document.getElementById('wf-toast-title'),
    toastText: document.getElementById('wf-toast-text'),
    countdown: document.getElementById('wf-countdown'),
    finale: document.getElementById('wf-finale'),
    finaleTitle: document.getElementById('wf-finale-title'),
    finaleText: document.getElementById('wf-finale-text'),
    preview: document.getElementById('wf-preview'),
    previewUrl: document.getElementById('wf-preview-url'),
    siteStatus: document.getElementById('wf-site-status'),
    siteFlash: document.getElementById('wf-site-flash'),
    siteBanner: document.getElementById('wf-site-banner'),
    siteBannerLabel: document.getElementById('wf-site-banner-label'),
    siteBannerText: document.getElementById('wf-site-banner-text'),
    repairList: document.getElementById('wf-repairs')
  };

  var ctx = el.canvas.getContext('2d', { alpha: false });

  // ---- the world, in metres -------------------------------------------
  var LANE_WIDTH = 3.5;                  // metres across one lane
  var LANES = [-1.5, -0.5, 0.5, 1.5];    // lane centres, in lane units
  var ROAD_HALF = 2.0;                   // lane units from centre to verge
  var CAR_HALF = 0.34;                   // lane units, half the car's width
  var CAR_LENGTH = 5.6;                  // metres
  var SCRAPE_DECELERATION = 15;          // m/s² lost grinding along a barrier

  // ---- the camera ------------------------------------------------------
  var CAM_HEIGHT = 2.9;                  // metres above the road
  var CAM_BEHIND = 12.5;                 // metres behind the player's car
  var HORIZON = 0.44;                    // fraction of the canvas height
  var DRAW_DISTANCE = 260;               // metres
  // Nothing is drawn between the camera and the player's own car. A vehicle a
  // metre in front of the lens projects to several screen-widths across and
  // simply covers the game; anything this close is level with the player and
  // has already been passed.
  var NEAR_PLANE = 7.5;                  // metres in front of the camera
  var SEGMENT = 3.2;                     // metres of road per drawn quad

  var SECTION_THEMES = [
    { road: '#2a3145', edge: '#7c8bb5', grass: '#141c2e', sky: ['#0d1a33', '#2b3f6b'], tint: '#22d3ee' },
    { road: '#2b2c48', edge: '#8b86c8', grass: '#161a30', sky: ['#141033', '#3b2f70'], tint: '#8b7cff' },
    { road: '#26333a', edge: '#74a79c', grass: '#122029', sky: ['#0a2028', '#1f4f52'], tint: '#37e0b0' },
    { road: '#332c37', edge: '#c09277', grass: '#20182a', sky: ['#2a1626', '#6b3a34'], tint: '#ff9f6b' },
    { road: '#243542', edge: '#6fa8c4', grass: '#12222c', sky: ['#08202e', '#1d4a68'], tint: '#4bd3ff' },
    { road: '#33283c', edge: '#c07fae', grass: '#1d1528', sky: ['#25102c', '#5f2a58'], tint: '#ff79c6' },
    { road: '#28322c', edge: '#8fb877', grass: '#14201a', sky: ['#0d2016', '#2d4f2a'], tint: '#a3e635' }
  ];
  var SECTION_COPY = [
    'Stabilise the breakpoint before the layout collapses.',
    'Put the interface back into a working display flow.',
    'Close the runaway outer spacing gap.',
    'Restore breathing room inside every component.',
    'Re-align the flexible layout system.',
    'Return drifting elements to their intended position.',
    'Rebuild the missing grid structure.'
  ];

  function clamp(value, low, high) {
    return value < low ? low : (value > high ? high : value);
  }
  function now() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }
  function fmtClock(seconds) {
    seconds = Math.max(0, Math.ceil(seconds));
    return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' +
           String(seconds % 60).padStart(2, '0');
  }

  // ================================================================== 2. net

  var state = {          // the last thing the server said, verbatim
    status: initial.status || 'not_started',
    repairs: (initial.repairs || []).slice(),
    collisions: initial.collisions || 0,
    distance: initial.distance || 0,
    score: initial.score || 0,
    remaining: typeof initial.remaining === 'number' ? initial.remaining : C.duration,
    elapsed: initial.elapsed || 0
  };

  var anchorElapsed = state.elapsed;   // server time, advanced monotonically
  var anchorAt = now();
  var over = false;
  var running = false;

  function elapsedNow() { return anchorElapsed + (now() - anchorAt) / 1000; }
  function remainingNow() { return Math.max(0, C.duration - elapsedNow()); }

  function csrf() {
    var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function post(url, data) {
    var body = new URLSearchParams();
    Object.keys(data || {}).forEach(function (key) { body.append(key, data[key]); });
    return fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': csrf(),
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: body,
      credentials: 'same-origin'
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        return { ok: response.ok, status: response.status, data: payload };
      });
    });
  }

  /* Reports go out one at a time and in order: the server only accepts the
   * next repair in course order, so overlapping requests would be refused. */
  var chain = Promise.resolve();
  function enqueue(task) {
    chain = chain.then(task, task);
    return chain;
  }

  /* The single place a server answer is allowed to change the game. */
  function applyState(payload) {
    if (!payload || typeof payload !== 'object') { return false; }
    if (typeof payload.elapsed === 'number') {
      anchorElapsed = payload.elapsed;
      anchorAt = now();
    }
    if (typeof payload.collisions === 'number') { state.collisions = payload.collisions; }
    if (typeof payload.score === 'number') { state.score = payload.score; }
    if (typeof payload.distance === 'number') { state.distance = payload.distance; }
    if (payload.repairs) { syncRepairs(payload.repairs); }
    if (payload.status) { state.status = payload.status; }

    if (payload.status === 'completed' || payload.redirect) {
      finishUp(payload.redirect || urls.result);
      return true;
    }
    if (payload.status === 'expired') {
      timeUp();
      return true;
    }
    return false;
  }

  // ================================================================ 3. audio

  var audio = { ctx: null, engine: null, gain: null, muted: false };
  try {
    audio.muted = window.localStorage.getItem('wf-muted') === '1';
  } catch (err) { audio.muted = false; }

  function audioReady() {
    if (audio.ctx || audio.muted) { return audio.ctx; }
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) { return null; }
    audio.ctx = new Ctor();
    audio.gain = audio.ctx.createGain();
    audio.gain.gain.value = 0.5;
    audio.gain.connect(audio.ctx.destination);
    return audio.ctx;
  }

  function engineOn() {
    var context = audioReady();
    if (!context || audio.engine) { return; }
    var osc = context.createOscillator();
    var sub = context.createOscillator();
    var gain = context.createGain();
    osc.type = 'sawtooth';
    sub.type = 'triangle';
    osc.frequency.value = 60;
    sub.frequency.value = 30;
    gain.gain.value = 0;
    osc.connect(gain); sub.connect(gain); gain.connect(audio.gain);
    osc.start(); sub.start();
    audio.engine = { osc: osc, sub: sub, gain: gain };
  }

  function engineUpdate(speed) {
    if (!audio.engine || audio.muted) { return; }
    var ratio = Math.abs(speed) / CAR.topSpeed;
    audio.engine.osc.frequency.value = 58 + ratio * 155;
    audio.engine.sub.frequency.value = 29 + ratio * 62;
    audio.engine.gain.gain.value = 0.012 + ratio * 0.032;
  }

  function blip(frequency, duration, type, volume) {
    var context = audioReady();
    if (!context || audio.muted) { return; }
    var osc = context.createOscillator();
    var gain = context.createGain();
    osc.type = type || 'square';
    osc.frequency.value = frequency;
    gain.gain.setValueAtTime(volume || 0.12, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + duration);
    osc.connect(gain); gain.connect(audio.gain);
    osc.start();
    osc.stop(context.currentTime + duration);
  }

  function noise(duration, volume, filtered) {
    var context = audioReady();
    if (!context || audio.muted) { return; }
    var frames = Math.floor(context.sampleRate * duration);
    var buffer = context.createBuffer(1, frames, context.sampleRate);
    var data = buffer.getChannelData(0);
    for (var i = 0; i < frames; i++) {
      data[i] = (Math.random() * 2 - 1) * (1 - i / frames);
    }
    var source = context.createBufferSource();
    var gain = context.createGain();
    gain.gain.value = volume || 0.25;
    source.buffer = buffer;
    if (filtered) {
      var band = context.createBiquadFilter();
      band.type = 'bandpass';
      band.frequency.value = filtered;
      source.connect(band); band.connect(gain);
    } else {
      source.connect(gain);
    }
    gain.connect(audio.gain);
    source.start();
  }

  function chime() {
    blip(660, 0.12, 'triangle', 0.16);
    setTimeout(function () { blip(990, 0.22, 'triangle', 0.14); }, 110);
    setTimeout(function () { blip(1320, 0.26, 'triangle', 0.10); }, 210);
  }
  function fanfare() {
    [523, 659, 784, 1046].forEach(function (note, i) {
      setTimeout(function () { blip(note, 0.28, 'triangle', 0.15); }, i * 130);
    });
  }

  function setMuted(muted) {
    audio.muted = muted;
    try { window.localStorage.setItem('wf-muted', muted ? '1' : '0'); } catch (err) {}
    el.mute.setAttribute('aria-pressed', muted ? 'true' : 'false');
    el.mute.textContent = muted ? '🔇' : '🔊';
    el.mute.title = muted ? 'Unmute sound' : 'Mute sound';
    if (audio.gain) { audio.gain.gain.value = muted ? 0 : 0.5; }
  }
  el.mute.addEventListener('click', function () { setMuted(!audio.muted); });
  setMuted(audio.muted);

  // ============================================================== 4. sprites
  //
  // Every prop is drawn once into an offscreen canvas here and blitted from
  // then on. It costs a few milliseconds at load, keeps the frame loop down
  // to drawImage calls, and means the game ships with no asset downloads —
  // see RENDERER-DECISION.md.

  function makeSprite(width, height, draw) {
    var canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    draw(canvas.getContext('2d'), width, height);
    return canvas;
  }

  function roundRect(c, x, y, w, h, r) {
    c.beginPath();
    if (c.roundRect) { c.roundRect(x, y, w, h, r); } else { c.rect(x, y, w, h); }
  }

  /* A car seen from behind: shadow, wheels, body, glass, lights.
   * `spec` picks the silhouette so a van does not read as a sports car.
   *
   * The canvas is deliberately wider than it is tall. A car is about 1.9m
   * across and 1.5m high, and a sprite sheet that does not respect that
   * draws something the length of a bus standing on its end. */
  function drawCar(c, W, H, spec) {
    var bodyW = W * spec.width;
    var bodyH = H * spec.length;
    var x = (W - bodyW) / 2;
    var y = H - bodyH - H * 0.06;

    // ground shadow
    c.globalAlpha = 0.38;
    c.fillStyle = '#000';
    c.beginPath();
    c.ellipse(W / 2, H - H * 0.045, bodyW * 0.56, H * 0.045, 0, 0, 6.283);
    c.fill();

    // a fine beltline and a small badge give the player car a deliberately
    // engineered feel at close range instead of a generic coloured block.
    c.fillStyle = 'rgba(255,255,255,.22)';
    c.fillRect(x + bodyW * 0.12, y + bodyH * 0.48, bodyW * 0.76, Math.max(2, bodyH * 0.018));
    c.globalAlpha = 1;

    // wheels, poking out either side
    c.fillStyle = '#12151c';
    var wheelW = bodyW * 0.16, wheelH = bodyH * 0.24;
    c.fillRect(x - wheelW * 0.42, y + bodyH * 0.12, wheelW, wheelH);
    c.fillRect(x + bodyW - wheelW * 0.58, y + bodyH * 0.12, wheelW, wheelH);
    c.fillRect(x - wheelW * 0.34, y + bodyH * 0.68, wheelW, wheelH);
    c.fillRect(x + bodyW - wheelW * 0.66, y + bodyH * 0.68, wheelW, wheelH);

    // body, with a vertical shade so it reads as rounded metal
    var shade = c.createLinearGradient(x, 0, x + bodyW, 0);
    shade.addColorStop(0, spec.dark);
    shade.addColorStop(0.35, spec.body);
    shade.addColorStop(0.62, spec.light);
    shade.addColorStop(1, spec.dark);
    c.fillStyle = shade;
    roundRect(c, x, y, bodyW, bodyH, bodyW * spec.radius);
    c.fill();

    // roof / cabin
    if (spec.cabin) {
      c.fillStyle = spec.dark;
      roundRect(c, x + bodyW * 0.09, y + bodyH * 0.05,
                bodyW * 0.82, bodyH * spec.cabin, bodyW * 0.09);
      c.fill();
    }

    // rear window
    var glass = c.createLinearGradient(0, y, 0, y + bodyH * 0.4);
    glass.addColorStop(0, '#20293d');
    glass.addColorStop(1, '#4d6f9a');
    c.fillStyle = glass;
    roundRect(c, x + bodyW * 0.15, y + bodyH * 0.13, bodyW * 0.7, bodyH * 0.26, bodyW * 0.06);
    c.fill();

    // boot line + bumper
    c.fillStyle = 'rgba(0,0,0,.28)';
    c.fillRect(x + bodyW * 0.08, y + bodyH * 0.62, bodyW * 0.84, bodyH * 0.02);
    c.fillStyle = spec.dark;
    roundRect(c, x + bodyW * 0.04, y + bodyH * 0.82, bodyW * 0.92, bodyH * 0.13, bodyW * 0.05);
    c.fill();

    // tail lights
    c.fillStyle = spec.lights || '#ff4557';
    c.shadowColor = spec.lights || '#ff4557';
    c.shadowBlur = W * 0.06;
    roundRect(c, x + bodyW * 0.08, y + bodyH * 0.66, bodyW * 0.2, bodyH * 0.09, 3);
    c.fill();
    roundRect(c, x + bodyW * 0.72, y + bodyH * 0.66, bodyW * 0.2, bodyH * 0.09, 3);
    c.fill();
    c.shadowBlur = 0;

    if (spec.roofSign) {            // taxi
      c.fillStyle = '#f7d64a';
      roundRect(c, x + bodyW * 0.34, y - bodyH * 0.06, bodyW * 0.32, bodyH * 0.07, 3);
      c.fill();
    }
    if (spec.spoiler) {
      c.fillStyle = spec.dark;
      c.fillRect(x - bodyW * 0.04, y + bodyH * 0.5, bodyW * 1.08, bodyH * 0.05);
      c.fillRect(x + bodyW * 0.12, y + bodyH * 0.5, bodyW * 0.06, bodyH * 0.1);
      c.fillRect(x + bodyW * 0.82, y + bodyH * 0.5, bodyW * 0.06, bodyH * 0.1);
    }
    if (spec.player) {
      c.fillStyle = '#9ff6ff';
      c.shadowColor = '#22d3ee'; c.shadowBlur = 12;
      roundRect(c, x + bodyW * 0.38, y + bodyH * 0.52, bodyW * 0.24, bodyH * 0.055, 3); c.fill();
      c.shadowBlur = 0;
      c.fillStyle = '#dffcff';
      c.font = '800 ' + Math.max(8, W * 0.055) + 'px "IBM Plex Mono", monospace';
      c.textAlign = 'center'; c.fillText('WF', W / 2, y + bodyH * 0.78);
    }
  }

  var VEHICLES = {
    // the player: unmistakable — Website Fixer blue, light bar, spoiler, glow
    player: { width: 0.80, length: 0.80, radius: 0.20, cabin: 0.34, spoiler: true, player: true,
              body: '#4f7dfb', light: '#8fb4ff', dark: '#1f3ba8', lights: '#ff5a6e' },
    sedan:  { width: 0.74, length: 0.80, radius: 0.17, cabin: 0.30,
              body: '#c94f63', light: '#e88596', dark: '#7d2434' },
    hatch:  { width: 0.72, length: 0.72, radius: 0.20, cabin: 0.34,
              body: '#48a86b', light: '#7fd39b', dark: '#1f5d38' },
    sports: { width: 0.78, length: 0.76, radius: 0.24, cabin: 0.22, spoiler: true,
              body: '#e0912f', light: '#ffc46b', dark: '#8a4f0d' },
    van:    { width: 0.82, length: 0.92, radius: 0.11, cabin: 0.52,
              body: '#8b8fa6', light: '#c2c6d8', dark: '#4a4e60' },
    taxi:   { width: 0.75, length: 0.80, radius: 0.17, cabin: 0.30, roofSign: true,
              body: '#f2c033', light: '#ffe081', dark: '#8f6c0c' },
    truck:  { width: 0.88, length: 1.00, radius: 0.08, cabin: 0.60,
              body: '#5566b8', light: '#8e9be0', dark: '#2b3570' }
  };

  var sprites = {};
  function buildVehicleSprites() {
    Object.keys(VEHICLES).forEach(function (name) {
      var spec = VEHICLES[name];
      sprites[name] = makeSprite(170, 140, function (c, W, H) {
        drawCar(c, W, H, spec);
      });
      // a braking variant, so a braker reads as braking
      sprites[name + ':brake'] = makeSprite(170, 140, function (c, W, H) {
        drawCar(c, W, H, spec);
        c.fillStyle = '#ff2233';
        c.shadowColor = '#ff2233';
        c.shadowBlur = 22;
        var bodyW = W * spec.width, bodyH = H * spec.length;
        var x = (W - bodyW) / 2, y = H - bodyH - H * 0.06;
        roundRect(c, x + bodyW * 0.08, y + bodyH * 0.66, bodyW * 0.2, bodyH * 0.09, 3);
        c.fill();
        roundRect(c, x + bodyW * 0.72, y + bodyH * 0.66, bodyW * 0.2, bodyH * 0.09, 3);
        c.fill();
      });
    });
  }

  /* A CSS repair pickup: a holographic chip with the property on it. */
  function buildPickupSprites() {
    C.repairs.forEach(function (card, index) {
      var tint = SECTION_THEMES[index % SECTION_THEMES.length].tint;
      sprites['pickup:' + card.id] = makeSprite(190, 190, function (c, W, H) {
        c.translate(W / 2, H / 2);

        // outer glow ring
        var glow = c.createRadialGradient(0, 0, 8, 0, 0, W / 2);
        glow.addColorStop(0, tint);
        glow.addColorStop(0.45, 'rgba(255,255,255,0)');
        c.globalAlpha = 0.5;
        c.fillStyle = glow;
        c.beginPath(); c.arc(0, 0, W / 2, 0, 6.283); c.fill();
        c.globalAlpha = 1;

        // chip body: a rotated square, like a component seen edge-on
        c.rotate(Math.PI / 4);
        c.fillStyle = 'rgba(9,17,30,.94)';
        roundRect(c, -46, -46, 92, 92, 12); c.fill();
        c.strokeStyle = tint; c.lineWidth = 4;
        roundRect(c, -46, -46, 92, 92, 12); c.stroke();

        // chip legs
        c.fillStyle = tint;
        for (var i = -1; i <= 1; i++) {
          c.fillRect(-58, i * 22 - 4, 12, 8);
          c.fillRect(46, i * 22 - 4, 12, 8);
        }
        c.rotate(-Math.PI / 4);

        // label
        c.fillStyle = '#eaf7ff';
        c.font = '700 20px "IBM Plex Mono", monospace';
        c.textAlign = 'center';
        c.textBaseline = 'middle';
        c.fillText(card.label.slice(0, 10), 0, 1);
        c.fillStyle = tint;
        c.font = '700 11px "IBM Plex Mono", monospace';
        c.fillText('CSS', 0, -22);
      });
    });
  }

  /* Roadside furniture. Each is a post plus a panel with CSS on it. */
  function signSprite(lines, tint, width, height) {
    return makeSprite(width, height, function (c, W, H) {
      var panelH = H * 0.62;
      c.fillStyle = '#39405a';
      c.fillRect(W / 2 - W * 0.035, panelH, W * 0.07, H - panelH);
      c.fillStyle = '#0d1526';
      roundRect(c, 4, 4, W - 8, panelH - 8, 10); c.fill();
      c.strokeStyle = tint; c.lineWidth = 4;
      roundRect(c, 4, 4, W - 8, panelH - 8, 10); c.stroke();
      c.fillStyle = tint;
      c.textAlign = 'center';
      c.textBaseline = 'middle';
      var size = Math.min(30, (W - 24) / Math.max(6, lines[0].length) * 1.7);
      lines.forEach(function (line, i) {
        c.font = '700 ' + (i ? size * 0.72 : size) + 'px "IBM Plex Mono", monospace';
        c.fillStyle = i ? '#9fb0cf' : tint;
        c.fillText(line, W / 2, panelH / 2 + (i - (lines.length - 1) / 2) * size * 1.15);
      });
    });
  }

  /* The CSS billboards that give each section its identity. */
  var SECTION_SIGNS = [
    [['@media', 'min-width: 860px'], ['BREAKPOINT', 'INVERTED'], ['ROAD', 'NARROWS']],
    [['display:', 'block'], ['SHOULD BE', 'flex'], ['STACKED', 'NOT ROWED']],
    [['margin', '<-- 150px -->'], ['GAP', 'TOO WIDE'], ['SPACING', 'BROKEN']],
    [['padding:', '4px'], ['CARDS', 'CRUSHED'], ['NO ROOM', 'INSIDE']],
    [['justify-', 'content'], ['flex-end', 'WRONG END'], ['gap: 2px', 'TOO TIGHT']],
    [['transform:', 'rotate(45deg)'], ['POSITION', 'DRIFTED'], ['top: 40%', 'OFFSET']],
    [['repeat(1,', '1fr)'], ['GRID', 'COLLAPSED'], ['COLUMNS', 'LOST']]
  ];

  function buildPropSprites() {
    SECTION_SIGNS.forEach(function (signs, section) {
      var tint = SECTION_THEMES[section].tint;
      signs.forEach(function (lines, i) {
        sprites['sign:' + section + ':' + i] = signSprite(lines, tint, 210, 150);
      });
    });

    sprites.lamp = makeSprite(60, 200, function (c, W, H) {
      c.fillStyle = '#2c3350';
      c.fillRect(W / 2 - 4, 26, 8, H - 26);
      c.fillRect(W / 2 - 4, 22, W * 0.42, 7);
      c.fillStyle = '#ffe9b0';
      c.shadowColor = '#ffd88a'; c.shadowBlur = 18;
      c.beginPath(); c.ellipse(W / 2 + W * 0.3, 30, 9, 5, 0, 0, 6.283); c.fill();
    });

    sprites.barrier = makeSprite(120, 60, function (c, W, H) {
      c.fillStyle = '#c8ced9';
      c.fillRect(0, H * 0.3, W, H * 0.34);
      c.fillStyle = '#e0434f';
      for (var i = 0; i < 3; i++) { c.fillRect(i * (W / 3) + 6, H * 0.3, W / 6, H * 0.34); }
      c.fillStyle = '#5b6478';
      c.fillRect(W * 0.12, H * 0.6, 8, H * 0.4);
      c.fillRect(W * 0.8, H * 0.6, 8, H * 0.4);
    });

    sprites.block = makeSprite(150, 130, function (c, W, H) {
      c.fillStyle = '#1b2540';
      roundRect(c, 4, 10, W - 8, H - 14, 6); c.fill();
      c.strokeStyle = '#2f3f66'; c.lineWidth = 3;
      roundRect(c, 4, 10, W - 8, H - 14, 6); c.stroke();
      c.fillStyle = 'rgba(120,190,255,.22)';
      for (var y = 0; y < 4; y++) {
        for (var x = 0; x < 3; x++) {
          if ((x + y) % 3) { c.fillRect(18 + x * 40, 24 + y * 26, 26, 14); }
        }
      }
    });
  }

  /* The hazard for each section, drawn as the CSS mistake it stands for. */
  function buildHazardSprites() {
    var W = 260, H = 130;
    var make = function (section, draw) {
      sprites['hazard:' + section] = makeSprite(W, H, function (c) {
        var tint = SECTION_THEMES[section].tint;
        c.save(); draw(c, W, H, tint); c.restore();
        // every hazard sits on a shadow so it reads as on the road
        c.globalAlpha = 0.35; c.fillStyle = '#000';
        c.beginPath(); c.ellipse(W / 2, H - 8, W * 0.42, 9, 0, 0, 6.283); c.fill();
      });
    };

    // 0 RESPONSIVE — a breakpoint gate closing in from both sides
    make(0, function (c, W, H, tint) {
      c.fillStyle = tint;
      c.fillRect(0, H * 0.2, W * 0.2, H * 0.62);
      c.fillRect(W * 0.8, H * 0.2, W * 0.2, H * 0.62);
      c.strokeStyle = '#fff'; c.lineWidth = 3; c.setLineDash([7, 7]);
      c.beginPath(); c.moveTo(W * 0.24, H * 0.5); c.lineTo(W * 0.76, H * 0.5); c.stroke();
      c.setLineDash([]);
      c.fillStyle = '#08111f'; c.font = '700 17px "IBM Plex Mono",monospace';
      c.textAlign = 'center'; c.fillText('@media', W / 2, H * 0.34);
    });
    // 1 DISPLAY — boxes stacked instead of laid out in a row
    make(1, function (c, W, H, tint) {
      c.fillStyle = tint;
      for (var i = 0; i < 3; i++) {
        c.globalAlpha = 1 - i * 0.22;
        c.fillRect(W * 0.28, H * 0.16 + i * H * 0.22, W * 0.44, H * 0.17);
      }
      c.globalAlpha = 1;
    });
    // 2 MARGIN — two blocks shoved apart, the gap dimensioned
    make(2, function (c, W, H, tint) {
      c.fillStyle = tint;
      c.fillRect(0, H * 0.24, W * 0.26, H * 0.54);
      c.fillRect(W * 0.74, H * 0.24, W * 0.26, H * 0.54);
      c.strokeStyle = '#fff'; c.lineWidth = 2;
      c.beginPath();
      c.moveTo(W * 0.28, H * 0.5); c.lineTo(W * 0.72, H * 0.5);
      c.moveTo(W * 0.28, H * 0.42); c.lineTo(W * 0.28, H * 0.58);
      c.moveTo(W * 0.72, H * 0.42); c.lineTo(W * 0.72, H * 0.58);
      c.stroke();
      c.fillStyle = '#fff'; c.font = '700 15px "IBM Plex Mono",monospace';
      c.textAlign = 'center'; c.fillText('150px', W / 2, H * 0.42);
    });
    // 3 PADDING — a box whose content is crushed against its edge
    make(3, function (c, W, H, tint) {
      c.strokeStyle = tint; c.lineWidth = 6;
      c.strokeRect(W * 0.16, H * 0.16, W * 0.68, H * 0.66);
      c.fillStyle = tint; c.globalAlpha = 0.28;
      c.fillRect(W * 0.16, H * 0.16, W * 0.68, H * 0.66);
      c.globalAlpha = 1;
      c.fillStyle = '#0b1220';
      c.fillRect(W * 0.2, H * 0.2, W * 0.6, H * 0.58);
      c.fillStyle = tint;
      c.fillRect(W * 0.21, H * 0.21, W * 0.58, H * 0.1);
    });
    // 4 FLEXBOX — items all shoved to one end, arrow showing it
    make(4, function (c, W, H, tint) {
      c.fillStyle = tint;
      for (var i = 0; i < 3; i++) { c.fillRect(W * 0.5 + i * W * 0.16, H * 0.24, W * 0.13, H * 0.54); }
      c.strokeStyle = '#fff'; c.lineWidth = 4;
      c.beginPath();
      c.moveTo(W * 0.08, H * 0.5); c.lineTo(W * 0.42, H * 0.5);
      c.moveTo(W * 0.34, H * 0.4); c.lineTo(W * 0.42, H * 0.5);
      c.moveTo(W * 0.34, H * 0.6); c.lineTo(W * 0.42, H * 0.5);
      c.stroke();
    });
    // 5 POSITION — the block, and the dashed outline it drifted from
    make(5, function (c, W, H, tint) {
      c.strokeStyle = 'rgba(255,255,255,.55)'; c.lineWidth = 3; c.setLineDash([6, 6]);
      c.strokeRect(W * 0.12, H * 0.14, W * 0.5, H * 0.5);
      c.setLineDash([]);
      c.fillStyle = tint;
      c.fillRect(W * 0.34, H * 0.34, W * 0.5, H * 0.5);
    });
    // 6 GRID — a lattice barrier
    make(6, function (c, W, H, tint) {
      c.fillStyle = tint;
      c.fillRect(W * 0.06, H * 0.18, W * 0.88, H * 0.62);
      c.strokeStyle = '#0b1220'; c.lineWidth = 5;
      for (var x = 1; x < 4; x++) {
        c.beginPath();
        c.moveTo(W * 0.06 + (W * 0.88 / 4) * x, H * 0.18);
        c.lineTo(W * 0.06 + (W * 0.88 / 4) * x, H * 0.8);
        c.stroke();
      }
      c.beginPath();
      c.moveTo(W * 0.06, H * 0.49); c.lineTo(W * 0.94, H * 0.49); c.stroke();
    });
  }

  var skyline = null;
  var treeline = null;
  function buildSkyline() {
    skyline = makeSprite(1400, 200, function (c, W, H) {
      var rng = mulberry32(C.seed ^ 0x5f5f);
      c.fillStyle = '#141d33';
      for (var x = 0; x < W; ) {
        var w = 26 + rng() * 60;
        var h = 40 + rng() * 140;
        c.globalAlpha = 0.55 + rng() * 0.35;
        c.fillRect(x, H - h, w - 5, h);
        c.globalAlpha = 1;
        c.fillStyle = 'rgba(120,190,255,.16)';
        for (var wy = H - h + 10; wy < H - 10; wy += 14) {
          for (var wx = x + 5; wx < x + w - 12; wx += 12) {
            if (rng() > 0.55) { c.fillRect(wx, wy, 5, 7); }
          }
        }
        c.fillStyle = '#141d33';
        x += w;
      }
    });
  }

  /* A second, nearer parallax layer makes the roadside feel populated even
   * on long clear stretches. It is a cached silhouette, not per-frame foliage. */
  function buildTreeline() {
    treeline = makeSprite(1200, 145, function (c, W, H) {
      var rng = mulberry32(C.seed ^ 0x1d4a11);
      for (var x = -20; x < W + 40; x += 22 + rng() * 24) {
        var h = 28 + rng() * 76;
        c.fillStyle = rng() > 0.35 ? '#152c32' : '#1e3940';
        c.beginPath(); c.arc(x + 17, H - h * 0.45, h * 0.32, 0, 6.283); c.fill();
        c.fillStyle = '#101b28'; c.fillRect(x + 14, H - h * 0.52, 6, h * 0.58);
      }
    });
  }

  // =============================================================== 5. course

  /* Deterministic, seeded, and identical on every PC in the room: the course
   * is part of the competition, so everybody has to drive the same one. */
  function mulberry32(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
      var t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  var TRAFFIC_KINDS = [
    { kind: 'slow',    speed: [15, 21], vehicles: ['truck', 'van'],           weight: 3 },
    { kind: 'cruise',  speed: [27, 34], vehicles: ['sedan', 'hatch', 'taxi'], weight: 4 },
    { kind: 'fast',    speed: [43, 51], vehicles: ['sports'],                 weight: 2 },
    { kind: 'weaver',  speed: [25, 32], vehicles: ['hatch', 'sedan'],         weight: 3 },
    { kind: 'blocker', speed: [22, 27], vehicles: ['van', 'truck'],           weight: 2 },
    { kind: 'braker',  speed: [31, 38], vehicles: ['taxi', 'sedan'],          weight: 2 }
  ];
  var TRAFFIC_BAG = [];
  TRAFFIC_KINDS.forEach(function (entry) {
    for (var i = 0; i < entry.weight; i++) { TRAFFIC_BAG.push(entry); }
  });

  var obstacles = [];
  var traffic = [];
  var pickups = [];
  var props = [];

  function buildCourse() {
    var rng = mulberry32(C.seed);
    var pick = function (list) { return list[Math.floor(rng() * list.length)]; };
    var between = function (low, high) { return low + rng() * (high - low); };

    for (var s = 0; s < C.sectionCount; s++) {
      var from = s * C.sectionMetres;
      var to = from + C.sectionMetres;
      var repairAt = C.repairMetres[s];

      // --- CSS hazards. Section 0 is RESPONSIVE: the road itself is the
      // hazard there, so it gets a lighter scattering.
      var spacing = s === 0 ? 260 : between(150, 195);
      for (var d = from + 190; d < to - 120; d += spacing) {
        if (Math.abs(d - repairAt) < 130) { continue; }
        // Never more than half the road: there are always two lanes through.
        var width = s === 0 ? 1 : 1 + Math.floor(rng() * 1.9);
        var start = Math.floor(rng() * (LANES.length - width));
        obstacles.push({
          d: Math.round(d + between(-30, 30)),
          from: start,
          to: start + width - 1,
          kind: s
        });
        spacing = s === 0 ? 260 : between(150, 195);
      }

      // --- traffic. Every field it will ever need is decided here, from the
      // shared seed, so its behaviour is its own and reproducible.
      var gap = between(95, 145);
      for (var t = from + 120; t < to; t += gap) {
        var entry = pick(TRAFFIC_BAG);
        var lane = Math.floor(rng() * LANES.length);
        traffic.push({
          anchor: Math.round(t),
          d: Math.round(t),
          lane: lane,
          homeLane: lane,
          targetLane: lane,
          x: LANES[lane],
          speed: between(entry.speed[0], entry.speed[1]),
          base: 0,
          kind: entry.kind,
          sprite: pick(entry.vehicles),
          laneTimer: between(1.5, 6),
          brakeTimer: between(4, 12),
          braking: 0,
          drift: between(-1, 1),
          live: false
        });
        gap = between(95, 145);
      }

      // --- roadside scenery: the CSS billboards and the street furniture
      for (var p = from + 60; p < to; p += between(58, 96)) {
        var side = rng() > 0.5 ? 1 : -1;
        var roll = rng();
        var prop = { d: Math.round(p), side: side, sprite: 'lamp', width: 2.6, height: 8 };
        if (roll > 0.72) {
          prop.sprite = 'sign:' + s + ':' + Math.floor(rng() * SECTION_SIGNS[s].length);
          prop.width = 9;
          prop.height = 6.4;
        } else if (roll > 0.5) {
          prop.sprite = 'block';
          prop.width = 11;
          prop.height = 9.5;
        } else if (roll > 0.34) {
          prop.sprite = 'barrier';
          prop.width = 5;
          prop.height = 2.5;
        }
        props.push(prop);
      }

      pickups.push({
        d: repairAt,
        lane: 1 + Math.floor(rng() * 2),
        index: s,
        card: C.repairs[s],
        taken: false,
        misses: 0
      });
    }

    obstacles.sort(function (a, b) { return a.d - b.d; });
    traffic.sort(function (a, b) { return a.anchor - b.anchor; });
    props.sort(function (a, b) { return a.d - b.d; });
    traffic.forEach(function (other) { other.base = other.speed; });
  }

  /* RESPONSIVE CHAOS: the drivable width itself changes at the breakpoints. */
  function roadHalf(d) {
    if (d >= C.sectionMetres || d < 0) { return ROAD_HALF; }
    var t = (d % 460) / 460;
    if (t < 0.34) { return ROAD_HALF; }
    if (t < 0.44) { return ROAD_HALF - 0.85 * (t - 0.34) / 0.10; }
    if (t < 0.78) { return ROAD_HALF - 0.85; }
    if (t < 0.88) { return ROAD_HALF - 0.85 * (0.88 - t) / 0.10; }
    return ROAD_HALF;
  }

  // ============================================================== 6. traffic
  //
  // Traffic drives itself. Nothing in this section reads the player's car,
  // its keys, its speed or its position: each vehicle runs off its own
  // timers, its own target lane and its own seeded personality. That
  // separation is the whole point — a `blocker` used to steer towards
  // `car.x`, which meant it mirrored the player's steering keys.
  //
  // Player-vs-traffic contact is physics, and lives in section 7 with the
  // rest of the collision handling.

  var liveTraffic = [];
  var trafficCursor = 0;

  function trafficWindow(around) {
    while (trafficCursor < traffic.length && traffic[trafficCursor].anchor < around + 780) {
      var entering = traffic[trafficCursor++];
      entering.live = true;
      liveTraffic.push(entering);
    }
    for (var i = liveTraffic.length - 1; i >= 0; i--) {
      if (liveTraffic[i].d < around - 200) {
        liveTraffic[i].live = false;
        liveTraffic.splice(i, 1);
      }
    }
  }

  function updateTrafficAI(dt) {
    for (var i = 0; i < liveTraffic.length; i++) {
      var other = liveTraffic[i];

      // --- braking. Brakers work to their own cycle; everybody else eases
      // back towards their cruising speed.
      if (other.braking > 0) {
        other.braking -= dt;
        other.speed += (other.base * 0.35 - other.speed) * Math.min(1, dt * 2.2);
      } else {
        other.speed += (other.base - other.speed) * Math.min(1, dt * 0.8);
        if (other.kind === 'braker') {
          other.brakeTimer -= dt;
          if (other.brakeTimer <= 0) {
            other.braking = 1.1;
            other.brakeTimer = 6 + (i % 5);
          }
        }
      }

      // --- lane discipline, on each vehicle's own clock
      other.laneTimer -= dt;
      if (other.laneTimer <= 0) {
        if (other.kind === 'weaver') {
          other.targetLane = clamp(other.targetLane + (other.drift > 0 ? 1 : -1),
                                   0, LANES.length - 1);
          if (other.targetLane === 0 || other.targetLane === LANES.length - 1) {
            other.drift = -other.drift;
          }
          other.laneTimer = 1.8;
        } else if (other.kind === 'blocker') {
          // wanders between its home lane and the one beside it, on its own
          other.targetLane = (other.targetLane === other.homeLane)
            ? clamp(other.homeLane + (other.drift > 0 ? 1 : -1), 0, LANES.length - 1)
            : other.homeLane;
          other.laneTimer = 3.4;
        } else if (other.kind === 'fast') {
          other.targetLane = clamp(other.targetLane + (other.drift > 0 ? 1 : -1),
                                   0, LANES.length - 1);
          other.drift = -other.drift;
          other.laneTimer = 4.5;
        } else {
          other.targetLane = other.homeLane;
          other.laneTimer = 5 + (i % 4);
        }
      }

      var target = LANES[clamp(other.targetLane, 0, LANES.length - 1)];
      other.x += clamp(target - other.x, -dt * 0.9, dt * 0.9);
      other.d += other.speed * dt;
    }
  }

  // =============================================================== 7. player
  //
  // The only place keyboard input is read, and the only thing it moves.

  var car = {
    d: state.distance,
    x: 0,
    speed: 0,
    crashUntil: 0,
    shake: 0,
    tilt: 0
  };
  var keys = Object.create(null);
  var pendingCollisions = 0;
  var reportedDistance = state.distance;
  var lastReport = 0;
  var finishing = false;

  function repairsDone() { return state.repairs.length; }
  function allRepaired() { return repairsDone() >= C.repairs.length; }

  /* Take every repair the server has recorded off the course.
   *
   * This has to run at boot as well as on every update. Resuming a race after
   * a refresh arrives with repairs already collected, and without this their
   * chips are still sitting on the road: `collect()` refuses them because
   * they are no longer the next repair, and the one that *is* next never gets
   * drawn — only the first uncollected pickup is ever on the course. The
   * participant drives at a chip that does nothing, forever. */
  function reconcilePickups() {
    for (var i = 0; i < pickups.length; i++) {
      if (state.repairs.indexOf(pickups[i].card.id) >= 0) { pickups[i].taken = true; }
    }
  }

  function syncRepairs(list) {
    var changed = list.length !== state.repairs.length;
    state.repairs = list.slice();
    reconcilePickups();
    if (changed) { paintRepairList(); refreshPreview(); }
  }

  function steerInput() {
    var left = keys.ArrowLeft || keys.a || keys.A;
    var right = keys.ArrowRight || keys.d || keys.D;
    return (right ? 1 : 0) - (left ? 1 : 0);
  }
  function throttleInput() {
    var up = keys.ArrowUp || keys.w || keys.W;
    var down = keys.ArrowDown || keys.s || keys.S;
    return (up ? 1 : 0) - (down ? 1 : 0);
  }

  function driveCar(dt) {
    var throttle = throttleInput();
    if (throttle > 0) {
      car.speed += CAR.acceleration * dt;
    } else if (throttle < 0) {
      car.speed -= CAR.braking * dt;
    } else {
      car.speed -= CAR.drag * dt * (car.speed > 0 ? 1 : -1);
      if (Math.abs(car.speed) < CAR.drag * dt) { car.speed = 0; }
    }
    car.speed = clamp(car.speed, -CAR.reverseSpeed, CAR.topSpeed);

    // Steering bites a little less at a crawl, so the car does not pivot on
    // the spot — but never so little that a stopped car cannot pull itself
    // out of trouble, which is how a driver gets wedged and gives up.
    var grip = clamp(Math.abs(car.speed) / 14, 0.6, 1);
    var steer = steerInput() * CAR.steerRate * grip * dt;
    car.x += steer;
    car.tilt += (steer * 9 - car.tilt) * Math.min(1, dt * 9);

    var half = roadHalf(car.d) - CAR_HALF;
    if (car.x > half || car.x < -half) {
      car.x = clamp(car.x, -half, half);
      // Scraping the barrier costs speed but is not a scored penalty. It has
      // to be a rate, not a per-frame fraction: a fraction compounds sixty
      // times a second and pins anyone who touches a wall to walking pace.
      car.speed = Math.max(0, car.speed - SCRAPE_DECELERATION * dt);
      if (car.speed > 8 && Math.random() < 0.4) {
        spark(car.x > 0 ? half : -half, car.d + 1, '#ffd48a', 2);
      }
    }

    car.d += car.speed * dt;
    if (car.d < 0) { car.d = 0; car.speed = 0; }
    car.shake = Math.max(0, car.shake - dt * 2.6);
  }

  /* One crash is one penalty *and* one speed loss.
   *
   * Both have to sit behind the grace window. A car overlapping an obstacle
   * touches it for a dozen frames, and taking the speed off once per frame
   * compounds to a standstill — which reads to the participant as the game
   * having broken, not as having hit something. */
  function crash(force, speedKept) {
    var stamp = elapsedNow();
    if (stamp < car.crashUntil) { return false; }
    car.crashUntil = stamp + CAR.crashGrace;
    car.speed *= (speedKept === undefined ? CAR.crashSpeedKept : speedKept);
    car.shake = Math.min(1, car.shake + (force || 1));
    pendingCollisions += 1;
    state.collisions += 1;
    paintHud();
    noise(0.24, 0.32, 320);
    showImpact(force > 1 ? 'CSS HAZARD — SPEED REDUCED' : 'TRAFFIC IMPACT — PENALTY RECORDED');
    return true;
  }

  function overlapsCar(x, halfWidth, d, halfLength) {
    return Math.abs(x - car.x) < (halfWidth + CAR_HALF) &&
           Math.abs(d - car.d) < (halfLength + CAR_LENGTH / 2);
  }

  function checkTrafficContact() {
    for (var i = 0; i < liveTraffic.length; i++) {
      var other = liveTraffic[i];
      if (!overlapsCar(other.x, 0.32, other.d, 2.9)) { continue; }
      var ahead = other.d > car.d;
      if (crash(ahead ? 1 : 0.6)) {
        for (var s = 0; s < 7; s++) { spark(other.x, other.d, '#ffd166', 3); }
      }
      // physical separation only — this does not steer or throttle the AI
      other.d += ahead ? 3 : -3;
    }
  }

  var OBSTACLE_HALF_LENGTH = 3.4;
  var obstacleCursor = 0;

  function updateObstacles() {
    while (obstacleCursor < obstacles.length && obstacles[obstacleCursor].d < car.d - 60) {
      obstacleCursor++;
    }
    for (var i = obstacleCursor; i < obstacles.length; i++) {
      var block = obstacles[i];
      if (block.d > car.d + 120) { break; }
      var centre = (LANES[block.from] + LANES[block.to]) / 2;
      var half = (block.to - block.from + 1) / 2;
      if (!overlapsCar(centre, half - 0.08, block.d, OBSTACLE_HALF_LENGTH)) { continue; }

      // A CSS hazard costs far more speed than clipping a car does, and
      // deflects the car towards the open road rather than stopping it dead.
      // Being *blocked* would be more punishing than it looks: a stalled car
      // in front of a barrier is a participant who has lost their attempt.
      if (crash(1.2, 0.3)) {
        for (var s = 0; s < 10; s++) { spark(car.x, block.d, '#8fd8ff', 4); }
      }
      var escape = (car.x < centre) ? -1 : 1;
      car.x = clamp(car.x + escape * 0.09, -roadHalf(car.d) + CAR_HALF,
                    roadHalf(car.d) - CAR_HALF);
    }
  }

  function updatePickups() {
    for (var i = 0; i < pickups.length; i++) {
      var pickup = pickups[i];
      if (pickup.taken) { continue; }
      if (overlapsCar(LANES[pickup.lane], 0.5, pickup.d, 4.5)) {
        collect(pickup);
      } else if (car.d - pickup.d > 45) {
        // Missed it. The course does not let a repair get away: it comes
        // round again further on, in a different lane.
        pickup.misses += 1;
        pickup.d = Math.round(car.d + 330);
        pickup.lane = (pickup.lane + 1 + (pickup.misses % 2)) % LANES.length;
      }
      break;      // only the next uncollected repair is ever on the course
    }
  }

  function collect(pickup) {
    if (pickup.taken || pickup.index !== repairsDone()) { return; }
    pickup.taken = true;
    chime();
    burst(LANES[pickup.lane], pickup.d, SECTION_THEMES[pickup.index % 7].tint);
    showToast(pickup.card);
    var payload = { distance: Math.floor(car.d), repair: pickup.card.id };
    if (pendingCollisions) { payload.collisions = pendingCollisions; pendingCollisions = 0; }
    enqueue(function () {
      return post(urls.progress, payload).then(function (result) {
        if (applyState(result.data)) { return; }
        if (!result.ok) {
          // The server did not accept it; put it back on the course.
          pickup.taken = false;
          pickup.d = Math.round(car.d + 330);
        }
      }).catch(function () {
        pickup.taken = false;
        pickup.d = Math.round(car.d + 330);
      });
    });
  }

  function reportProgress(force) {
    var stamp = now();
    if (!force && stamp - lastReport < C.progressSeconds * 1000) { return; }
    var travelled = Math.floor(car.d);
    if (!force && travelled <= reportedDistance && !pendingCollisions) { return; }
    lastReport = stamp;
    reportedDistance = travelled;
    var payload = { distance: travelled };
    if (pendingCollisions) { payload.collisions = pendingCollisions; pendingCollisions = 0; }
    enqueue(function () {
      return post(urls.progress, payload)
        .then(function (result) { applyState(result.data); })
        .catch(function () {});
    });
  }

  // ---- particles -------------------------------------------------------
  var particles = [];
  function spark(x, d, colour, power) {
    if (particles.length > 90) { return; }
    particles.push({
      x: x, d: d, h: 0.5 + Math.random() * 0.6,
      vx: (Math.random() - 0.5) * 0.9, vd: (Math.random() - 0.5) * 9,
      vh: 1.5 + Math.random() * 2.5,
      life: 0.45 + Math.random() * 0.3, age: 0,
      colour: colour, size: power || 3
    });
  }
  function burst(x, d, colour) {
    for (var i = 0; i < 22; i++) { spark(x, d, colour, 4); }
  }
  function updateParticles(dt) {
    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.age += dt;
      if (p.age >= p.life) { particles.splice(i, 1); continue; }
      p.x += p.vx * dt;
      p.d += p.vd * dt;
      p.h += p.vh * dt;
      p.vh -= 9 * dt;
      if (p.h < 0) { p.h = 0; p.vh *= -0.4; }
    }
  }

  // =============================================================== 8. render
  //
  // A pinhole camera behind the car. Everything is in metres, and `scale` is
  // pixels-per-metre at a given depth, so a projection is one multiply.

  var view = { w: 0, h: 0, dpr: 1, focal: 0, horizon: 0 };
  var booted = false;      // sprites and course are ready to draw

  function resize() {
    var rect = el.track.getBoundingClientRect();
    view.dpr = Math.min(2, window.devicePixelRatio || 1);
    view.w = Math.max(320, Math.floor(rect.width));
    view.h = Math.max(320, Math.floor(rect.height));
    el.canvas.width = Math.floor(view.w * view.dpr);
    el.canvas.height = Math.floor(view.h * view.dpr);
    el.canvas.style.width = view.w + 'px';
    el.canvas.style.height = view.h + 'px';
    view.focal = view.h * 1.15;
    view.horizon = view.h * HORIZON;
    ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
    fitPreview();

    // Setting canvas.width wipes the canvas. While the loop is running the
    // next frame paints over that immediately, but during the countdown —
    // or any moment the loop is paused — the track would just go black and
    // stay black, so repaint here instead of waiting for a frame.
    if (booted && !running) { render(); }
  }
  window.addEventListener('resize', resize);

  /* The preview renders NovaCloud at a fixed 1280px virtual width and is
   * scaled down to whatever the panel is. Sizing the iframe to the panel
   * instead would put the site inside its own 860px breakpoint and hide the
   * desktop layout the repairs are fixing. */
  var PREVIEW_WIDTH = 1280;
  function fitPreview() {
    if (!el.preview || !el.preview.parentElement) { return; }
    var width = el.preview.parentElement.clientWidth;
    if (!width) { return; }
    el.preview.style.setProperty('--wf-preview-scale', (width / PREVIEW_WIDTH).toFixed(4));
  }

  var camX = 0;                       // camera lateral position, in metres
  function cameraDistance() { return car.d - CAM_BEHIND; }

  function scaleAt(dz) { return view.focal / Math.max(0.6, dz); }
  function screenXAt(metres, scale) { return view.w / 2 + (metres - camX) * scale; }
  function roadYAt(scale) { return view.horizon + CAM_HEIGHT * scale; }

  function theme(d) {
    return SECTION_THEMES[clamp(Math.floor(d / C.sectionMetres), 0, SECTION_THEMES.length - 1)];
  }

  function drawSky(palette) {
    var sky = ctx.createLinearGradient(0, 0, 0, view.horizon + 30);
    sky.addColorStop(0, palette.sky[0]);
    sky.addColorStop(1, palette.sky[1]);
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, view.w, view.horizon + 30);

    // parallax skyline, drifting with distance travelled
    if (skyline) {
      var offset = (car.d * 0.35) % skyline.width;
      var y = view.horizon - skyline.height * 0.78;
      ctx.globalAlpha = 0.75;
      ctx.drawImage(skyline, -offset, y);
      ctx.drawImage(skyline, skyline.width - offset, y);
      ctx.globalAlpha = 1;
    }

    if (treeline) {
      var nearOffset = (car.d * 0.72) % treeline.width;
      var treeY = view.horizon - treeline.height * 0.24;
      ctx.globalAlpha = 0.82;
      ctx.drawImage(treeline, -nearOffset, treeY);
      ctx.drawImage(treeline, treeline.width - nearOffset, treeY);
      ctx.globalAlpha = 1;
    }

    // the ground the road is laid on
    ctx.fillStyle = palette.grass;
    ctx.fillRect(0, view.horizon, view.w, view.h - view.horizon);
  }

  /* Drawn over the road: the far end fades into the sky rather than stopping
   * dead against the skyline. */
  function drawHorizonHaze(palette) {
    var haze = ctx.createLinearGradient(0, view.horizon - 18, 0, view.horizon + 78);
    haze.addColorStop(0, palette.sky[1]);
    haze.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = haze;
    ctx.fillRect(0, view.horizon - 18, view.w, 96);
  }

  /* The road, far to near, one quad per segment so RESPONSIVE can narrow it.
   *
   * Drawn in layers rather than segment by segment. A rumble strip is wider
   * than its own road quad, so painting a whole segment at a time lets the
   * near segment's red bleed a hairline over the far one's tarmac; going
   * verge-then-rumble-then-road across the whole road removes that entirely. */
  var SEG_CACHE = [];
  for (var seg = 0; seg < 260; seg++) {
    SEG_CACHE.push({ yNear: 0, yFar: 0, lNear: 0, rNear: 0, lFar: 0, rFar: 0,
                     rumbleNear: 0, rumbleFar: 0, band: 0, road: '', grass: '',
                     sNear: 0, sFar: 0, zNear: 0 });
  }

  function drawRoad(camZ) {
    var first = Math.floor(camZ / SEGMENT);
    var segments = Math.ceil(DRAW_DISTANCE / SEGMENT);
    var count = 0;

    for (var i = segments; i >= 0 && count < SEG_CACHE.length; i--) {
      var zNear = (first + i) * SEGMENT;
      var dzNear = zNear - camZ;
      var dzFar = dzNear + SEGMENT;
      if (dzFar < 1.2) { continue; }

      var sNear = scaleAt(dzNear);
      var sFar = scaleAt(dzFar);
      // +1 so consecutive quads overlap: butt-jointed edges leave hairline
      // seams where the layer underneath shows through.
      var yNear = roadYAt(sNear) + 1;
      var yFar = roadYAt(sFar);
      if (yNear - yFar < 0.35 && dzNear > 40) { continue; }   // sub-pixel: skip

      var palette = theme(zNear);
      var halfNear = roadHalf(zNear) * LANE_WIDTH;
      var halfFar = roadHalf(zNear + SEGMENT) * LANE_WIDTH;
      var s = SEG_CACHE[count++];
      s.zNear = zNear; s.sNear = sNear; s.sFar = sFar;
      s.yNear = yNear; s.yFar = yFar;
      s.lNear = screenXAt(-halfNear, sNear); s.rNear = screenXAt(halfNear, sNear);
      s.lFar = screenXAt(-halfFar, sFar); s.rFar = screenXAt(halfFar, sFar);
      s.rumbleNear = Math.max(2, 1.15 * sNear);
      s.rumbleFar = Math.max(1, 1.15 * sFar);
      s.band = Math.floor(zNear / SEGMENT) % 2;
      s.road = palette.road;
      s.grass = s.band ? palette.grass : shade(palette.grass, 1.1);
    }

    var n;
    for (n = 0; n < count; n++) {                       // verges
      var v = SEG_CACHE[n];
      ctx.fillStyle = v.grass;
      ctx.beginPath();
      ctx.moveTo(0, v.yNear); ctx.lineTo(view.w, v.yNear);
      ctx.lineTo(view.w, v.yFar); ctx.lineTo(0, v.yFar);
      ctx.closePath(); ctx.fill();
    }
    for (n = 0; n < count; n++) {                       // rumble strips
      var r = SEG_CACHE[n];
      ctx.fillStyle = r.band ? '#e8ecf6' : '#d0455a';
      quad(r.lNear - r.rumbleNear, r.yNear, r.rNear + r.rumbleNear, r.yNear,
           r.rFar + r.rumbleFar, r.yFar, r.lFar - r.rumbleFar, r.yFar);
    }
    for (n = 0; n < count; n++) {                       // tarmac
      var a = SEG_CACHE[n];
      ctx.fillStyle = a.road;
      quad(a.lNear, a.yNear, a.rNear, a.yNear, a.rFar, a.yFar, a.lFar, a.yFar);
    }
    // restrained aggregate texture: a few deterministic-looking dark bands
    // break up the perfectly flat asphalt without obscuring lane information.
    ctx.fillStyle = 'rgba(5,10,18,.12)';
    for (n = 0; n < count; n += 4) {
      var texture = SEG_CACHE[n];
      if (texture.yNear - texture.yFar < 1) { continue; }
      quad(texture.lNear, texture.yNear - 1, texture.rNear, texture.yNear - 1,
           texture.rFar, texture.yFar, texture.lFar, texture.yFar);
    }
    ctx.fillStyle = 'rgba(236,242,255,.55)';            // lane markings
    for (n = 0; n < count; n++) {
      var m = SEG_CACHE[n];
      if (m.band) { continue; }
      for (var lane = -1; lane <= 1; lane++) {
        var mid = lane * LANE_WIDTH;
        var wNear = Math.max(1, 0.16 * m.sNear);
        var wFar = Math.max(0.5, 0.16 * m.sFar);
        quad(screenXAt(mid, m.sNear) - wNear, m.yNear,
             screenXAt(mid, m.sNear) + wNear, m.yNear,
             screenXAt(mid, m.sFar) + wFar, m.yFar,
             screenXAt(mid, m.sFar) - wFar, m.yFar);
      }
    }
  }

  function quad(x1, y1, x2, y2, x3, y3, x4, y4) {
    ctx.beginPath();
    ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.lineTo(x3, y3); ctx.lineTo(x4, y4);
    ctx.closePath(); ctx.fill();
  }

  function shade(hex, factor) {
    var n = parseInt(hex.slice(1), 16);
    var r = clamp(Math.round(((n >> 16) & 255) * factor), 0, 255);
    var g = clamp(Math.round(((n >> 8) & 255) * factor), 0, 255);
    var b = clamp(Math.round((n & 255) * factor), 0, 255);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  /* Everything that stands on the road is drawn back to front. */
  function drawSprite(image, worldX, dz, worldWidth, options) {
    if (dz < NEAR_PLANE || dz > DRAW_DISTANCE) { return; }
    var scale = scaleAt(dz);
    var w = worldWidth * scale;
    var h = w * (image.height / image.width);
    var x = screenXAt(worldX, scale) - w / 2;
    var y = roadYAt(scale) - h + (options && options.lift ? -options.lift * scale : 0);
    if (x + w < -60 || x > view.w + 60) { return; }

    if (options && options.alpha !== undefined) { ctx.globalAlpha = options.alpha; }
    // distance haze, so the far end of the course recedes
    ctx.drawImage(image, x, y, w, h);
    if (dz > 90) {
      ctx.globalAlpha = clamp((dz - 90) / 200, 0, 0.55);
      ctx.fillStyle = theme(car.d).sky[1];
      ctx.fillRect(x, y, w, h);
    }
    ctx.globalAlpha = 1;
  }

  function collectRenderables(camZ) {
    var list = [];
    var look = camZ + DRAW_DISTANCE;

    for (var p = 0; p < props.length; p++) {
      var prop = props[p];
      if (prop.d - camZ < NEAR_PLANE) { continue; }
      if (prop.d > look) { break; }
      list.push({ dz: prop.d - camZ, kind: 'prop', ref: prop });
    }
    for (var o = obstacleCursor; o < obstacles.length; o++) {
      var block = obstacles[o];
      if (block.d > look) { break; }
      if (block.d - camZ < NEAR_PLANE) { continue; }
      list.push({ dz: block.d - camZ, kind: 'hazard', ref: block });
    }
    for (var t = 0; t < liveTraffic.length; t++) {
      var other = liveTraffic[t];
      var dz = other.d - camZ;
      if (dz < NEAR_PLANE || dz > DRAW_DISTANCE) { continue; }
      list.push({ dz: dz, kind: 'traffic', ref: other });
    }
    for (var k = 0; k < pickups.length; k++) {
      if (!pickups[k].taken) {
        var pickup = pickups[k];
        if (pickup.d - camZ >= NEAR_PLANE && pickup.d <= look) {
          list.push({ dz: pickup.d - camZ, kind: 'pickup', ref: pickup });
        }
        break;
      }
    }
    if (allRepaired() && C.course - camZ >= NEAR_PLANE && C.course <= look) {
      list.push({ dz: C.course - camZ, kind: 'finish', ref: null });
    }
    for (var s = 0; s <= C.sectionCount; s++) {
      var gate = s * C.sectionMetres;
      if (gate - camZ >= NEAR_PLANE && gate <= look && s < C.sectionCount) {
        list.push({ dz: gate - camZ, kind: 'gate', ref: s });
      }
    }
    list.sort(function (a, b) { return b.dz - a.dz; });   // far first
    return list;
  }

  function drawGate(section, dz) {
    var scale = scaleAt(dz);
    var half = ROAD_HALF * LANE_WIDTH + 2.4;
    var left = screenXAt(-half, scale);
    var right = screenXAt(half, scale);
    var base = roadYAt(scale);
    var height = 7.2 * scale;
    var palette = SECTION_THEMES[section];

    ctx.fillStyle = '#151b2c';
    ctx.fillRect(left, base - height, Math.max(2, 0.7 * scale), height);
    ctx.fillRect(right - Math.max(2, 0.7 * scale), base - height,
                 Math.max(2, 0.7 * scale), height);
    var barH = Math.max(3, 1.8 * scale);
    ctx.fillStyle = '#0d1424';
    ctx.fillRect(left, base - height, right - left, barH);
    ctx.fillStyle = palette.tint;
    ctx.fillRect(left, base - height + barH - Math.max(1, 0.18 * scale),
                 right - left, Math.max(1, 0.18 * scale));

    if (barH > 11) {
      ctx.fillStyle = palette.tint;
      ctx.font = '700 ' + Math.floor(barH * 0.62) + 'px "IBM Plex Mono", monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(C.repairs[section].section, (left + right) / 2,
                   base - height + barH / 2);
    }
  }

  function drawFinish(dz) {
    var scale = scaleAt(dz);
    var half = ROAD_HALF * LANE_WIDTH;
    var left = screenXAt(-half, scale);
    var right = screenXAt(half, scale);
    var base = roadYAt(scale);
    var cell = Math.max(2, 1.1 * scale);
    for (var row = 0; row < 2; row++) {
      for (var x = left, i = 0; x < right; x += cell, i++) {
        ctx.fillStyle = ((i + row) % 2) ? '#0b1220' : '#ffffff';
        ctx.fillRect(x, base - cell * (2 - row), Math.min(cell, right - x), cell);
      }
    }
    var height = 8 * scale;
    ctx.fillStyle = '#121a2c';
    ctx.fillRect(left - cell, base - height, cell, height);
    ctx.fillRect(right, base - height, cell, height);
    var barH = Math.max(3, 2 * scale);
    ctx.fillStyle = '#0d1424';
    ctx.fillRect(left - cell, base - height, right - left + cell * 2, barH);
    if (barH > 12) {
      ctx.fillStyle = '#22d3ee';
      ctx.font = '700 ' + Math.floor(barH * 0.6) + 'px "IBM Plex Mono", monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('FINISH', (left + right) / 2, base - height + barH / 2);
    }
  }

  /* A column of light over the next repair, so it can be picked out from the
   * far end of a straight rather than appearing at the last moment. */
  function drawPickupBeacon(pickup, dz) {
    var scale = scaleAt(dz);
    var x = screenXAt(LANES[pickup.lane] * LANE_WIDTH, scale);
    var base = roadYAt(scale);
    var height = 26 * scale;
    var width = Math.max(3, 2.6 * scale);
    var tint = SECTION_THEMES[pickup.index % SECTION_THEMES.length].tint;
    var beam = ctx.createLinearGradient(0, base, 0, base - height);
    beam.addColorStop(0, tint);
    beam.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.globalAlpha = 0.3 + Math.sin(elapsedNow() * 3) * 0.08;
    ctx.fillStyle = beam;
    ctx.beginPath();
    ctx.moveTo(x - width / 2, base);
    ctx.lineTo(x + width / 2, base);
    ctx.lineTo(x + width * 0.9, base - height);
    ctx.lineTo(x - width * 0.9, base - height);
    ctx.closePath(); ctx.fill();
    ctx.globalAlpha = 1;
  }

  function drawParticles(camZ) {
    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      var dz = p.d - camZ;
      if (dz < 1.5 || dz > 120) { continue; }
      var scale = scaleAt(dz);
      var size = Math.max(1, p.size * scale * 0.03);
      ctx.globalAlpha = clamp(1 - p.age / p.life, 0, 1);
      ctx.fillStyle = p.colour;
      ctx.fillRect(screenXAt(p.x * LANE_WIDTH, scale) - size / 2,
                   roadYAt(scale) - p.h * scale - size / 2, size, size);
    }
    ctx.globalAlpha = 1;
  }

  function render() {
    var camZ = cameraDistance();
    camX += ((car.x * LANE_WIDTH) * 0.72 - camX) * 0.12;    // camera lags the car

    var shakeX = car.shake ? (Math.random() - 0.5) * car.shake * 16 : 0;
    var shakeY = car.shake ? (Math.random() - 0.5) * car.shake * 11 : 0;
    ctx.setTransform(view.dpr, 0, 0, view.dpr, shakeX * view.dpr, shakeY * view.dpr);

    var palette = theme(car.d);
    drawSky(palette);
    drawRoad(camZ);
    drawHorizonHaze(palette);

    var renderables = collectRenderables(camZ);
    for (var i = 0; i < renderables.length; i++) {
      var item = renderables[i];
      switch (item.kind) {
        case 'prop':
          drawSprite(sprites[item.ref.sprite] || sprites.lamp,
                     item.ref.side * (ROAD_HALF * LANE_WIDTH + item.ref.width * 0.75),
                     item.dz, item.ref.width);
          break;
        case 'hazard':
          drawSprite(sprites['hazard:' + item.ref.kind],
                     ((LANES[item.ref.from] + LANES[item.ref.to]) / 2) * LANE_WIDTH,
                     item.dz,
                     (item.ref.to - item.ref.from + 1) * LANE_WIDTH * 0.98);
          break;
        case 'traffic':
          drawSprite(sprites[item.ref.sprite + (item.ref.braking > 0 ? ':brake' : '')],
                     item.ref.x * LANE_WIDTH, item.dz, 2.0);
          break;
        case 'pickup':
          drawPickupBeacon(item.ref, item.dz);
          var bob = Math.sin(elapsedNow() * 3.4) * 0.35;
          drawSprite(sprites['pickup:' + item.ref.card.id],
                     LANES[item.ref.lane] * LANE_WIDTH, item.dz, 4.2,
                     { lift: 2.0 + bob });
          break;
        case 'gate': drawGate(item.ref, item.dz); break;
        case 'finish': drawFinish(item.dz); break;
      }
    }

    drawParticles(camZ);

    // the player, always nearest the camera
    var scale = scaleAt(CAM_BEHIND);
    var sprite = sprites.player;
    var w = 2.05 * scale;
    var h = w * (sprite.height / sprite.width);
    var x = screenXAt(car.x * LANE_WIDTH, scale);
    var y = roadYAt(scale);
    var flashing = elapsedNow() < car.crashUntil && Math.floor(elapsedNow() * 12) % 2;

    var pool = ctx.createRadialGradient(x, y, w * 0.1, x, y, w * 0.9);
    pool.addColorStop(0, 'rgba(160,205,255,.10)');
    pool.addColorStop(1, 'rgba(160,205,255,0)');
    ctx.fillStyle = pool;
    ctx.beginPath();
    ctx.ellipse(x, y, w * 0.9, h * 0.32, 0, 0, 6.283);
    ctx.fill();

    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(clamp(car.tilt, -11, 11) * Math.PI / 180 * 0.55);
    if (flashing) { ctx.globalAlpha = 0.55; }
    ctx.drawImage(sprite, -w / 2, -h, w, h);
    ctx.globalAlpha = 1;
    ctx.restore();

    // speed streaks at the screen edges when moving quickly
    var rush = clamp((car.speed / CAR.topSpeed - 0.55) / 0.45, 0, 1);
    if (rush > 0) {
      ctx.globalAlpha = rush * 0.28;
      var vign = ctx.createRadialGradient(view.w / 2, view.h * 0.6, view.h * 0.32,
                                          view.w / 2, view.h * 0.6, view.h * 0.95);
      vign.addColorStop(0, 'rgba(0,0,0,0)');
      vign.addColorStop(1, '#050912');
      ctx.fillStyle = vign;
      ctx.fillRect(0, 0, view.w, view.h);
      ctx.globalAlpha = 1;
    }
  }

  // ================================================================== 9. hud

  /* The HUD is DOM, so every write here can cost a layout. Each value is
   * compared against what is already on screen and only written when it has
   * actually changed — at 60fps that is the difference between a handful of
   * writes a second and several hundred. */
  var painted = { repairs: -1, penalties: -1, score: -1, section: -1,
                  clock: '', speed: -1, progress: -1 };

  function paintHud() {
    var done = repairsDone();
    if (done !== painted.repairs) {
      painted.repairs = done;
      el.repairs.textContent = done + '/' + C.repairs.length;
    }
    if (state.collisions !== painted.penalties) {
      painted.penalties = state.collisions;
      el.penalties.textContent = state.collisions;
    }
    if (state.score !== painted.score) {
      painted.score = state.score;
      el.score.textContent = state.score;
    }
    var section = clamp(Math.floor(car.d / C.sectionMetres), 0, C.sectionCount - 1);
    if (section !== painted.section) {
      painted.section = section;
      el.section.textContent = C.repairs[section].section;
      if (running && car.d > 80) { showSection(section); }
    }
    var progress = Math.round(clamp((car.d / C.course) * 100, 0, 100) * 2) / 2;
    if (progress !== painted.progress) {
      painted.progress = progress;
      el.progress.style.width = progress + '%';
    }

    var speed = Math.max(0, Math.round(car.speed * 3.6));
    if (speed !== painted.speed) {
      painted.speed = speed;
      el.speed.textContent = speed;
    }
    var clock = fmtClock(remainingNow());
    if (clock !== painted.clock) {
      painted.clock = clock;
      el.timer.textContent = clock;
      var remaining = remainingNow();
      el.timer.classList.toggle('warn', remaining <= C.warnSeconds && remaining > C.dangerSeconds);
      el.timer.classList.toggle('danger', remaining <= C.dangerSeconds);
    }

    if (allRepaired()) {
      showObjective('ALL REPAIRS COLLECTED — CROSS THE FINISH LINE');
    } else if (car.d > C.course - 400) {
      showObjective('NOVACLOUD IS NOT FIXED YET — COLLECT ' +
                    C.repairs[done].label + ' TO OPEN THE FINISH');
    } else {
      showObjective(null);
    }
  }

  var objectiveText = null;
  function showObjective(text) {
    if (text === objectiveText) { return; }
    objectiveText = text;
    el.objective.hidden = !text;
    if (text) { el.objective.textContent = text; }
  }

  function paintRepairList() {
    var items = el.repairList.children;
    for (var i = 0; i < items.length; i++) {
      var done = state.repairs.indexOf(items[i].dataset.repair) >= 0;
      items[i].classList.toggle('is-done', done);
    }
    var count = repairsDone();
    var status = count === C.repairs.length ? 'READY FOR DEPLOYMENT' :
      (count >= 4 ? 'RECOVERING' : 'CRITICAL');
    el.siteStatus.textContent = count + '/' + C.repairs.length + ' — ' + status;
    el.siteStatus.classList.toggle('is-fixed', count >= C.repairs.length);
    if (el.previewUrl) {
      var left = C.repairs.length - count;
      el.previewUrl.textContent = left === 0
        ? 'novacloud.local — all styles restored'
        : 'novacloud.local — ' + left + ' stylesheet error' + (left === 1 ? '' : 's');
    }
  }

  /* The preview is composed by the server from the repairs it has recorded,
   * so reloading it is what makes an earned repair show up on the website.
   * The old frame is kept visible under a flash until the new one has loaded,
   * which turns a jarring blank reload into a transition. */
  function refreshPreview() {
    var count = repairsDone();
    el.siteFlash.classList.remove('is-on');
    void el.siteFlash.offsetWidth;
    el.siteFlash.classList.add('is-on');
    el.preview.classList.add('is-swapping');
    el.preview.addEventListener('load', function once() {
      el.preview.removeEventListener('load', once);
      el.preview.classList.remove('is-swapping');
    });
    el.preview.src = urls.preview + '?r=' + count;
  }

  var toastTimer = null;
  var bannerTimer = null;
  var impactTimer = null;
  var sectionTimer = null;
  function showImpact(message) {
    if (!el.impact) { return; }
    el.impact.textContent = message;
    el.impact.hidden = false;
    el.impact.classList.remove('is-in');
    void el.impact.offsetWidth;
    el.impact.classList.add('is-in');
    clearTimeout(impactTimer);
    impactTimer = setTimeout(function () { el.impact.hidden = true; }, 850);
  }
  function showSection(section) {
    if (!el.sectionCallout) { return; }
    el.sectionTitle.textContent = C.repairs[section].section;
    el.sectionCopy.textContent = SECTION_COPY[section] || 'Repair the next CSS system.';
    el.sectionCallout.hidden = false;
    el.sectionCallout.classList.remove('is-in');
    void el.sectionCallout.offsetWidth;
    el.sectionCallout.classList.add('is-in');
    clearTimeout(sectionTimer);
    sectionTimer = setTimeout(function () { el.sectionCallout.hidden = true; }, 2100);
  }
  function showToast(card) {
    el.toastTitle.textContent = card.label + ' REPAIRED';
    el.toastText.textContent = card.message;
    el.toast.hidden = false;
    el.toast.classList.remove('is-in');
    void el.toast.offsetWidth;
    el.toast.classList.add('is-in');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.toast.classList.remove('is-in');
      el.toast.hidden = true;
    }, 2600);

    // ...and the same news over the website panel, where the change happens
    if (el.siteBanner) {
      el.siteBannerLabel.textContent = card.label;
      el.siteBannerText.textContent = card.message;
      el.siteBanner.hidden = false;
      el.siteBanner.classList.remove('is-in');
      void el.siteBanner.offsetWidth;
      el.siteBanner.classList.add('is-in');
      clearTimeout(bannerTimer);
      bannerTimer = setTimeout(function () {
        el.siteBanner.classList.remove('is-in');
        el.siteBanner.hidden = true;
      }, 2800);
    }
  }

  // ============================================================ 10. lifecycle

  var lastFrame = 0;

  function loop(stamp) {
    if (!running || over) { return; }
    var dt = lastFrame ? Math.min(0.05, (stamp - lastFrame) / 1000) : 0.016;
    lastFrame = stamp;

    driveCar(dt);                       // the only place input is read
    trafficWindow(car.d);               // spawn/cull around the camera
    updateTrafficAI(dt);                // autonomous: never reads the player
    checkTrafficContact();              // physics between the two
    updateObstacles();
    updatePickups();
    updateParticles(dt);
    engineUpdate(car.speed);

    render();
    paintHud();
    reportProgress(false);

    if (remainingNow() <= 0) {
      running = false;
      enqueue(function () {
        return post(urls.progress, { distance: Math.floor(car.d) })
          .then(function (result) {
            if (!applyState(result.data)) { timeUp(); }
          })
          .catch(function () { timeUp(); });
      });
      return;
    }

    if (!finishing && allRepaired() && car.d >= C.course) { crossFinish(); }
    requestAnimationFrame(loop);
  }

  function crossFinish() {
    finishing = true;
    running = false;
    fanfare();
    var payload = { distance: Math.floor(car.d) };
    if (pendingCollisions) { payload.collisions = pendingCollisions; pendingCollisions = 0; }
    enqueue(function () {
      return post(urls.progress, payload).catch(function () {});
    });
    enqueue(function () {
      return post(urls.complete, { finish: 1 }).then(function (result) {
        if (applyState(result.data)) { return; }
        if (result.ok) { finishUp((result.data && result.data.redirect) || urls.result); return; }
        // The server does not agree the race is over: keep driving.
        finishing = false;
        running = true;
        showObjective((result.data && result.data.error) || 'NOT FINISHED YET');
        objectiveText = null;
        lastFrame = 0;
        requestAnimationFrame(loop);
      }).catch(function () {
        finishing = false;
        setTimeout(crossFinish, 1500);
      });
    });
  }

  function finishUp(target) {
    over = true;
    running = false;
    el.finaleTitle.textContent = 'CSS FIX COMPLETE!';
    el.finaleText.textContent = 'NovaCloud is back together. Loading your result…';
    el.finale.classList.add('is-win');
    el.finale.hidden = false;
    setTimeout(function () { window.location.href = target || urls.result; }, 900);
  }

  function timeUp() {
    if (over) { return; }
    over = true;
    running = false;
    blip(150, 0.5, 'sawtooth', 0.18);
    el.finaleTitle.textContent = "TIME'S UP!";
    el.finaleText.textContent =
      'Your race attempt has ended, and NovaCloud is still broken. ' +
      'One official attempt per participant — your performance has been recorded.';
    el.finale.classList.remove('is-win');
    el.finale.hidden = false;
  }

  function beginDriving(payload) {
    applyState(payload);
    car.d = state.distance;
    car.speed = 0;
    car.x = 0;
    camX = 0;
    reportedDistance = state.distance;

    // Anything the course already went past on a previous life stays past.
    while (obstacleCursor < obstacles.length && obstacles[obstacleCursor].d < car.d - 60) {
      obstacleCursor++;
    }
    while (trafficCursor < traffic.length && traffic[trafficCursor].anchor < car.d - 200) {
      trafficCursor++;
    }
    pickups.forEach(function (pickup) {
      if (!pickup.taken && pickup.d < car.d) { pickup.d = Math.round(car.d + 330); }
    });

    el.brief.hidden = true;
    el.race.hidden = false;
    resize();
    el.track.focus();
    paintRepairList();
    paintHud();
    engineOn();

    running = true;
    lastFrame = 0;
    requestAnimationFrame(loop);
  }

  function countdown(done) {
    var steps = ['CSS SYSTEM FAILURE', 'REPAIR ROUTE INITIALIZED', '3', '2', '1', 'GO!'];
    var index = 0;
    el.brief.hidden = true;
    el.race.hidden = false;
    resize();
    render();
    el.countdown.hidden = false;
    (function tick() {
      if (index >= steps.length) {
        el.countdown.hidden = true;
        done();
        return;
      }
      var text = steps[index++];
      el.countdown.textContent = text;
      el.countdown.classList.toggle('is-word', index <= 2);
      el.countdown.classList.remove('is-beat');
      void el.countdown.offsetWidth;
      el.countdown.classList.add('is-beat');
      blip(index >= steps.length ? 880 : 440, 0.12, 'square', 0.1);
      setTimeout(tick, index <= 2 ? 850 : 700);
    }());
  }

  function start() {
    if (running || over || !el.startBtn) { return; }
    el.startBtn.disabled = true;
    var label = el.startBtn.textContent;
    el.startBtn.textContent = 'STARTING…';
    audioReady();

    // The countdown runs *before* the server is asked to start the clock, so
    // none of it is charged to the participant's twelve minutes.
    countdown(function () {
      post(urls.start, {}).then(function (result) {
        if (result.ok) { beginDriving(result.data); return; }
        el.race.hidden = true;
        el.brief.hidden = false;
        if (result.data && result.data.redirect) {
          window.location.href = result.data.redirect;
          return;
        }
        if (result.status === 403) { window.location.href = urls.exit; return; }
        el.startBtn.hidden = true;
        el.briefError.textContent = (result.data && result.data.error) ||
                                    'The race could not be started.';
        el.briefError.hidden = false;
      }).catch(function () {
        el.race.hidden = true;
        el.brief.hidden = false;
        el.startBtn.disabled = false;
        el.startBtn.textContent = label;
        el.briefError.textContent =
          'Could not reach the server. Check the connection and try again.';
        el.briefError.hidden = false;
      });
    });
  }

  if (el.startBtn) { el.startBtn.addEventListener('click', start); }

  /* Keyboard input. It writes to `keys`, `keys` is read by steerInput() and
   * throttleInput(), and those are read by driveCar() and nothing else. */
  var DRIVING_KEYS = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
                      'a', 'd', 'w', 's', 'A', 'D', 'W', 'S'];
  window.addEventListener('keydown', function (event) {
    if (DRIVING_KEYS.indexOf(event.key) >= 0) {
      keys[event.key] = true;
      if (running) { event.preventDefault(); }
    }
  });
  window.addEventListener('keyup', function (event) { keys[event.key] = false; });
  window.addEventListener('blur', function () { keys = Object.create(null); });

  buildVehicleSprites();
  buildPickupSprites();
  buildPropSprites();
  buildHazardSprites();
  buildSkyline();
  buildTreeline();
  buildCourse();
  reconcilePickups();     // a resumed race arrives with repairs already made
  booted = true;
  fitPreview();

  if (initial.status === 'expired') { over = true; }

  // Paint from the server's state, not from anything this page assumed. An
  // attempt that is over shows the time it has left, which is none of it.
  paintRepairList();
  paintHud();

  /* Exposed for the deterministic input/AI simulation test only. Reading it
   * changes nothing and the game never uses it. The player and the traffic
   * are steppable separately on purpose: that is what lets the test hold the
   * set of live vehicles fixed and prove the keyboard changes none of them. */
  window.__wfRaceProbe = {
    car: car,
    traffic: liveTraffic,
    keys: function () { return keys; },
    pickups: pickups,
    spawn: function (around) { trafficWindow(around); },
    stepPlayer: function (dt) { driveCar(dt); },
    stepTraffic: function (dt) { updateTrafficAI(dt); },
    paint: function () { resize(); render(); paintHud(); },
    snapshotTraffic: function () {
      return liveTraffic.map(function (o) {
        return {
          anchor: o.anchor, kind: o.kind, d: o.d, x: o.x, speed: o.speed,
          targetLane: o.targetLane, braking: o.braking, laneTimer: o.laneTimer
        };
      });
    }
  };
}());

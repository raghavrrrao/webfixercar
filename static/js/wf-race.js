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
 *   4  course         deterministic generation from the server's seed
 *   5  world          physics, collisions, pickups
 *   6  render         one canvas, one pass
 *   7  hud            DOM updated only when a value actually changes
 *   8  lifecycle      countdown, loop, finish, timeout
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
    toast: document.getElementById('wf-toast'),
    toastTitle: document.getElementById('wf-toast-title'),
    toastText: document.getElementById('wf-toast-text'),
    countdown: document.getElementById('wf-countdown'),
    finale: document.getElementById('wf-finale'),
    finaleTitle: document.getElementById('wf-finale-title'),
    finaleText: document.getElementById('wf-finale-text'),
    finaleActions: document.getElementById('wf-finale-actions'),
    preview: document.getElementById('wf-preview'),
    siteStatus: document.getElementById('wf-site-status'),
    siteFlash: document.getElementById('wf-site-flash'),
    repairList: document.getElementById('wf-repairs')
  };

  var ctx = el.canvas.getContext('2d', { alpha: false });

  // The road, in lane units. Four lanes either side of centre 0.
  var LANES = [-1.5, -0.5, 0.5, 1.5];
  var ROAD_HALF = 2.0;
  var CAR_HALF = 0.34;          // lane units
  var CAR_LENGTH = 5.6;         // metres
  var PX_PER_METRE = 2.4;
  var SCRAPE_DECELERATION = 15;   // m/s² lost while grinding along a barrier
  var SECTION_THEMES = [
    { road: '#151d33', edge: '#2c3b63', tint: '#22d3ee', prop: '#1d2a49' },
    { road: '#161a30', edge: '#343a72', tint: '#8b7cff', prop: '#1f2547' },
    { road: '#141e2c', edge: '#2a4a55', tint: '#37e0b0', prop: '#1b3040' },
    { road: '#1b1a2c', edge: '#4a3a63', tint: '#ff9f6b', prop: '#2a2140' },
    { road: '#131f28', edge: '#27505c', tint: '#4bd3ff', prop: '#183341' },
    { road: '#1d1826', edge: '#54325c', tint: '#ff79c6', prop: '#2d1f3c' },
    { road: '#141c22', edge: '#2f4f4a', tint: '#a3e635', prop: '#1c2f2c' }
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
    var gain = context.createGain();
    osc.type = 'sawtooth';
    osc.frequency.value = 60;
    gain.gain.value = 0.0;
    osc.connect(gain); gain.connect(audio.gain);
    osc.start();
    audio.engine = { osc: osc, gain: gain };
  }

  function engineUpdate(speed) {
    if (!audio.engine || audio.muted) { return; }
    var ratio = speed / CAR.topSpeed;
    audio.engine.osc.frequency.value = 55 + ratio * 150;
    audio.engine.gain.gain.value = 0.012 + ratio * 0.035;
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

  function noise(duration, volume) {
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
    source.connect(gain); gain.connect(audio.gain);
    source.start();
  }

  function chime() {
    blip(660, 0.12, 'triangle', 0.16);
    setTimeout(function () { blip(990, 0.22, 'triangle', 0.14); }, 110);
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
    el.mute.textContent = muted ? '⃠' : '♪';
    el.mute.title = muted ? 'Unmute sound' : 'Mute sound';
    if (audio.gain) { audio.gain.gain.value = muted ? 0 : 0.5; }
  }
  el.mute.addEventListener('click', function () { setMuted(!audio.muted); });
  setMuted(audio.muted);

  // =============================================================== 4. course

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
    { kind: 'slow', speed: [15, 21], weight: 3 },
    { kind: 'cruise', speed: [27, 34], weight: 4 },
    { kind: 'fast', speed: [43, 51], weight: 2 },
    { kind: 'weaver', speed: [25, 32], weight: 3 },
    { kind: 'blocker', speed: [22, 27], weight: 2 },
    { kind: 'braker', speed: [31, 38], weight: 2 }
  ];
  var TRAFFIC_BAG = [];
  TRAFFIC_KINDS.forEach(function (entry) {
    for (var i = 0; i < entry.weight; i++) { TRAFFIC_BAG.push(entry); }
  });

  var obstacles = [];
  var traffic = [];
  var pickups = [];

  function buildCourse() {
    var rng = mulberry32(C.seed);
    var pick = function (list) { return list[Math.floor(rng() * list.length)]; };
    var between = function (low, high) { return low + rng() * (high - low); };

    for (var s = 0; s < C.sectionCount; s++) {
      var from = s * C.sectionMetres;
      var to = from + C.sectionMetres;
      var repairAt = C.repairMetres[s];

      // --- CSS hazards. Section 0 is RESPONSIVE: the road itself is the
      // hazard there, so it gets only a light scattering of cones.
      var spacing = s === 0 ? 260 : between(150, 195);
      for (var d = from + 190; d < to - 120; d += spacing) {
        if (Math.abs(d - repairAt) < 130) { continue; }
        // Never more than half the road: there are always two lanes through.
        var width = s === 0 ? 1 : 1 + Math.floor(rng() * 1.9);   // 1-2 lanes
        var start = Math.floor(rng() * (LANES.length - width));
        obstacles.push({
          d: Math.round(d + between(-30, 30)),
          from: start,
          to: start + width - 1,
          kind: s,
          seed: rng()
        });
        spacing = s === 0 ? 260 : between(150, 195);
      }

      // --- traffic
      var gap = between(95, 145);
      for (var t = from + 120; t < to; t += gap) {
        var entry = pick(TRAFFIC_BAG);
        traffic.push({
          anchor: Math.round(t),
          d: Math.round(t),
          lane: Math.floor(rng() * LANES.length),
          x: 0,
          targetLane: 0,
          speed: between(entry.speed[0], entry.speed[1]),
          base: 0,
          kind: entry.kind,
          phase: rng() * 6.283,
          live: false
        });
        gap = between(95, 145);
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
    traffic.forEach(function (car) {
      car.x = LANES[car.lane];
      car.targetLane = car.lane;
      car.base = car.speed;
    });
  }
  buildCourse();

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

  // ================================================================ 5. world

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

  function syncRepairs(list) {
    var changed = list.length !== state.repairs.length;
    state.repairs = list.slice();
    pickups.forEach(function (pickup) {
      if (state.repairs.indexOf(pickup.card.id) >= 0) { pickup.taken = true; }
    });
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
    noise(0.22, 0.3);
    return true;
  }

  function overlapsCar(x, halfWidth, d, halfLength) {
    return Math.abs(x - car.x) < (halfWidth + CAR_HALF) &&
           Math.abs(d - car.d) < (halfLength + CAR_LENGTH / 2);
  }

  var trafficCursor = 0;
  var liveTraffic = [];

  function updateTraffic(dt) {
    while (trafficCursor < traffic.length && traffic[trafficCursor].anchor < car.d + 750) {
      var entering = traffic[trafficCursor++];
      entering.live = true;
      liveTraffic.push(entering);
    }
    for (var i = liveTraffic.length - 1; i >= 0; i--) {
      var other = liveTraffic[i];
      if (other.d < car.d - 190) { liveTraffic.splice(i, 1); continue; }

      var behind = car.d - other.d;
      if (other.kind === 'braker' && behind < 0 && behind > -55) {
        other.speed += (12 - other.speed) * Math.min(1, dt * 1.6);
      } else {
        other.speed += (other.base - other.speed) * Math.min(1, dt * 0.8);
      }
      if (other.kind === 'weaver') {
        other.phase += dt * 0.55;
        other.targetLane = Math.round(1.5 + Math.sin(other.phase) * 1.5);
      } else if (other.kind === 'blocker') {
        other.targetLane = other.d > car.d ? clamp(Math.round(car.x + 1.5), 0, 3)
                                           : other.lane;
      }
      var target = LANES[clamp(other.targetLane, 0, LANES.length - 1)];
      other.x += clamp(target - other.x, -dt * 1.1, dt * 1.1);
      other.d += other.speed * dt;

      if (overlapsCar(other.x, 0.32, other.d, 2.9)) {
        // Rear-ending costs more than being nudged from behind.
        crash(other.d > car.d ? 1 : 0.6);
        other.d += other.d > car.d ? 3 : -3;
      }
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
      crash(1.2, 0.3);
      var escape = (car.x < centre) ? -1 : 1;
      car.x = clamp(car.x + escape * 0.09, -roadHalf(car.d) + CAR_HALF,
                    roadHalf(car.d) - CAR_HALF);
    }
  }

  function updatePickups() {
    for (var i = 0; i < pickups.length; i++) {
      var pickup = pickups[i];
      if (pickup.taken) { continue; }
      if (overlapsCar(LANES[pickup.lane], 0.44, pickup.d, 4.5)) {
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

  // =============================================================== 6. render

  var view = { w: 0, h: 0, dpr: 1, roadPx: 0, lanePx: 0, carY: 0 };

  function resize() {
    var rect = el.track.getBoundingClientRect();
    view.dpr = Math.min(2, window.devicePixelRatio || 1);
    view.w = Math.max(320, Math.floor(rect.width));
    view.h = Math.max(320, Math.floor(rect.height));
    el.canvas.width = Math.floor(view.w * view.dpr);
    el.canvas.height = Math.floor(view.h * view.dpr);
    el.canvas.style.width = view.w + 'px';
    el.canvas.style.height = view.h + 'px';
    view.roadPx = Math.min(view.w * 0.72, 560);
    view.lanePx = view.roadPx / 4;
    view.carY = view.h * 0.76;
    ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
  }
  window.addEventListener('resize', resize);

  function screenY(d) { return view.carY - (d - car.d) * PX_PER_METRE; }
  function screenX(x) { return view.w / 2 + x * view.lanePx; }

  function theme(d) {
    return SECTION_THEMES[clamp(Math.floor(d / C.sectionMetres), 0, SECTION_THEMES.length - 1)];
  }

  function drawRoadside(look) {
    // Cheap parallax: a column of "code blocks" either side, themed per section.
    var step = 60;
    var first = Math.floor((car.d - 60) / step) * step;
    for (var d = first; d < car.d + look; d += step) {
      var y = screenY(d);
      if (y < -40 || y > view.h + 40) { continue; }
      var palette = theme(d);
      var height = 26;
      var edge = view.w / 2 - view.roadPx / 2;
      ctx.fillStyle = palette.prop;
      ctx.fillRect(edge - 74, y, 54, height);
      ctx.fillRect(view.w / 2 + view.roadPx / 2 + 20, y, 54, height);
      ctx.fillStyle = palette.tint;
      ctx.globalAlpha = 0.35;
      ctx.fillRect(edge - 74, y, 16, 4);
      ctx.fillRect(view.w / 2 + view.roadPx / 2 + 20, y, 16, 4);
      ctx.globalAlpha = 1;
    }
  }

  function drawRoad(look) {
    var palette = theme(car.d);
    ctx.fillStyle = palette.road;

    // The road is a quad because RESPONSIVE narrows it: walk it in slices.
    var slice = 24;
    for (var d = car.d - 90; d < car.d + look; d += slice) {
      var half = roadHalf(d);
      var yTop = screenY(d + slice);
      var yBottom = screenY(d);
      var left = screenX(-half);
      var right = screenX(half);
      ctx.fillStyle = palette.road;
      ctx.fillRect(left, yTop, right - left, yBottom - yTop + 1);
      ctx.fillStyle = palette.edge;
      ctx.fillRect(left - 7, yTop, 7, yBottom - yTop + 1);
      ctx.fillRect(right, yTop, 7, yBottom - yTop + 1);
    }

    // Lane dashes
    ctx.strokeStyle = 'rgba(255,255,255,.20)';
    ctx.lineWidth = 3;
    ctx.setLineDash([16, 22]);
    ctx.lineDashOffset = (car.d * PX_PER_METRE) % 38;
    for (var lane = -1; lane <= 1; lane++) {
      ctx.beginPath();
      ctx.moveTo(screenX(lane), 0);
      ctx.lineTo(screenX(lane), view.h);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  function drawSectionGates(look) {
    for (var s = 0; s <= C.sectionCount; s++) {
      var d = s * C.sectionMetres;
      if (d < car.d - 30 || d > car.d + look) { continue; }
      var y = screenY(d);
      var left = screenX(-ROAD_HALF) - 7;
      var right = screenX(ROAD_HALF) + 7;
      var palette = SECTION_THEMES[clamp(s, 0, SECTION_THEMES.length - 1)];
      ctx.fillStyle = palette.tint;
      ctx.globalAlpha = 0.85;
      ctx.fillRect(left, y - 6, right - left, 6);
      ctx.globalAlpha = 1;
      if (s < C.sectionCount) {
        ctx.fillStyle = '#0b1220';
        ctx.fillRect(left, y - 34, right - left, 26);
        ctx.fillStyle = palette.tint;
        ctx.font = '700 15px "IBM Plex Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillText(C.repairs[s].section, view.w / 2, y - 15);
      }
    }
  }

  /* Each hazard is drawn as the CSS mistake it stands for. */
  function drawObstacle(block) {
    var y = screenY(block.d);
    if (y < -90 || y > view.h + 90) { return; }
    var left = screenX(LANES[block.from] - 0.5);
    var right = screenX(LANES[block.to] + 0.5);
    var width = right - left;
    var palette = SECTION_THEMES[block.kind];
    var height = 26;

    ctx.save();
    switch (block.kind) {
      case 1:   // DISPLAY — boxes that stacked instead of lining up
        ctx.fillStyle = palette.tint;
        for (var i = 0; i < 3; i++) {
          ctx.globalAlpha = 0.9 - i * 0.2;
          ctx.fillRect(left + 4, y - i * 13, width - 8, 10);
        }
        break;
      case 2:   // MARGIN — two blocks shoved apart by a huge gap
        ctx.fillStyle = palette.tint;
        ctx.fillRect(left, y - height, width * 0.28, height);
        ctx.fillRect(right - width * 0.28, y - height, width * 0.28, height);
        ctx.strokeStyle = 'rgba(255,255,255,.45)';
        ctx.setLineDash([5, 5]);
        ctx.strokeRect(left + width * 0.3, y - height + 4, width * 0.4, height - 8);
        ctx.setLineDash([]);
        break;
      case 3:   // PADDING — a box with a fat hatched ring inside it
        ctx.strokeStyle = palette.tint;
        ctx.lineWidth = 3;
        ctx.strokeRect(left, y - height, width, height);
        ctx.fillStyle = palette.tint;
        ctx.globalAlpha = 0.35;
        ctx.fillRect(left, y - height, width, height);
        ctx.globalAlpha = 1;
        ctx.fillStyle = '#0b1220';
        ctx.fillRect(left + width * 0.3, y - height + 8, width * 0.4, height - 16);
        break;
      case 4:   // FLEXBOX — items in a row, all shoved to one end
        ctx.fillStyle = palette.tint;
        for (var f = 0; f < 3; f++) {
          ctx.fillRect(left + f * (width / 3.4) + 3, y - height, width / 4.4, height);
        }
        ctx.strokeStyle = '#fff';
        ctx.globalAlpha = 0.7;
        ctx.beginPath();
        ctx.moveTo(right - 16, y - height / 2);
        ctx.lineTo(right - 4, y - height / 2);
        ctx.lineTo(right - 10, y - height / 2 - 5);
        ctx.stroke();
        ctx.globalAlpha = 1;
        break;
      case 5:   // POSITION — the block, and the outline it drifted away from
        ctx.strokeStyle = 'rgba(255,255,255,.4)';
        ctx.setLineDash([4, 4]);
        ctx.strokeRect(left, y - height - 18, width, height);
        ctx.setLineDash([]);
        ctx.fillStyle = palette.tint;
        ctx.fillRect(left + 10, y - height, width, height);
        break;
      case 6:   // GRID — a lattice barrier
        ctx.fillStyle = palette.tint;
        ctx.fillRect(left, y - height, width, height);
        ctx.strokeStyle = '#0b1220';
        ctx.lineWidth = 3;
        for (var g = 1; g < 4; g++) {
          ctx.beginPath();
          ctx.moveTo(left + (width / 4) * g, y - height);
          ctx.lineTo(left + (width / 4) * g, y);
          ctx.stroke();
        }
        ctx.beginPath();
        ctx.moveTo(left, y - height / 2); ctx.lineTo(right, y - height / 2);
        ctx.stroke();
        break;
      default:  // RESPONSIVE — a breakpoint marker beside the narrowing road
        ctx.fillStyle = palette.tint;
        ctx.fillRect(left, y - 16, width, 16);
        ctx.fillStyle = '#0b1220';
        ctx.font = '700 10px "IBM Plex Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillText('@media', (left + right) / 2, y - 4);
    }
    ctx.restore();
  }

  function drawCarShape(x, d, colour, glass, length) {
    var y = screenY(d);
    if (y < -80 || y > view.h + 80) { return; }
    var w = view.lanePx * 0.62;
    var h = (length || CAR_LENGTH) * PX_PER_METRE * 1.5;
    var cx = screenX(x);
    ctx.fillStyle = colour;
    ctx.beginPath();
    if (ctx.roundRect) {
      ctx.roundRect(cx - w / 2, y - h / 2, w, h, 7);
    } else {
      ctx.rect(cx - w / 2, y - h / 2, w, h);
    }
    ctx.fill();
    ctx.fillStyle = glass;
    ctx.fillRect(cx - w * 0.32, y - h * 0.22, w * 0.64, h * 0.34);
  }

  function drawPickup(pickup) {
    var y = screenY(pickup.d);
    if (y < -80 || y > view.h + 80) { return; }
    var cx = screenX(LANES[pickup.lane]);
    var pulse = 1 + Math.sin(elapsedNow() * 6) * 0.06;
    var w = view.lanePx * 0.92 * pulse;
    var h = 34 * pulse;

    ctx.save();
    ctx.shadowColor = '#22d3ee';
    ctx.shadowBlur = 22;
    ctx.fillStyle = '#0e1b2c';
    ctx.fillRect(cx - w / 2, y - h / 2, w, h);
    ctx.shadowBlur = 0;
    ctx.strokeStyle = '#22d3ee';
    ctx.lineWidth = 2;
    ctx.strokeRect(cx - w / 2, y - h / 2, w, h);
    ctx.fillStyle = '#eaf7ff';
    ctx.font = '700 13px "IBM Plex Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(pickup.card.label, cx, y);
    ctx.restore();
  }

  function drawFinish() {
    if (!allRepaired()) { return; }
    var y = screenY(C.course);
    if (y < -60 || y > view.h + 60) { return; }
    var left = screenX(-ROAD_HALF) - 7;
    var right = screenX(ROAD_HALF) + 7;
    for (var i = 0; i * 26 < right - left; i++) {
      ctx.fillStyle = i % 2 ? '#0b1220' : '#ffffff';
      ctx.fillRect(left + i * 26, y - 26, 26, 13);
      ctx.fillStyle = i % 2 ? '#ffffff' : '#0b1220';
      ctx.fillRect(left + i * 26, y - 13, 26, 13);
    }
  }

  function render() {
    var look = view.h / PX_PER_METRE + 40;
    var shakeX = car.shake ? (Math.random() - 0.5) * car.shake * 14 : 0;
    var shakeY = car.shake ? (Math.random() - 0.5) * car.shake * 10 : 0;

    ctx.setTransform(view.dpr, 0, 0, view.dpr, shakeX * view.dpr, shakeY * view.dpr);
    ctx.fillStyle = '#080d17';
    ctx.fillRect(-20, -20, view.w + 40, view.h + 40);

    drawRoadside(look);
    drawRoad(look);
    drawSectionGates(look);

    for (var i = obstacleCursor; i < obstacles.length; i++) {
      if (obstacles[i].d > car.d + look) { break; }
      drawObstacle(obstacles[i]);
    }
    for (var p = 0; p < pickups.length; p++) {
      if (!pickups[p].taken) { drawPickup(pickups[p]); break; }
    }
    drawFinish();

    for (var t = 0; t < liveTraffic.length; t++) {
      drawCarShape(liveTraffic[t].x, liveTraffic[t].d, '#c2506b', '#2a0d18');
    }

    ctx.save();
    ctx.translate(screenX(car.x), view.carY);
    ctx.rotate(clamp(car.tilt, -12, 12) * Math.PI / 180);
    ctx.translate(-screenX(car.x), -view.carY);
    if (elapsedNow() < car.crashUntil && Math.floor(elapsedNow() * 12) % 2) {
      drawCarShape(car.x, car.d, '#ff7a8a', '#3a0a14');
    } else {
      drawCarShape(car.x, car.d, '#5b7cfa', '#9cf6ff');
    }
    ctx.restore();
  }

  // ================================================================== 7. hud

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
    var done = repairsDone();
    el.siteStatus.textContent = done === 0 ? 'BROKEN'
      : (done >= C.repairs.length ? 'FIXED' : 'REPAIRING ' + done + '/' + C.repairs.length);
    el.siteStatus.classList.toggle('is-fixed', done >= C.repairs.length);
  }

  function refreshPreview() {
    // The preview is composed by the server from the repairs it has recorded,
    // so reloading it is what makes an earned repair show up on the website.
    el.preview.src = urls.preview + '?r=' + repairsDone();
    el.siteFlash.classList.remove('is-on');
    void el.siteFlash.offsetWidth;
    el.siteFlash.classList.add('is-on');
  }

  var toastTimer = null;
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
  }

  // ============================================================ 8. lifecycle

  var lastFrame = 0;

  function loop(stamp) {
    if (!running || over) { return; }
    var dt = lastFrame ? Math.min(0.05, (stamp - lastFrame) / 1000) : 0.016;
    lastFrame = stamp;

    driveCar(dt);
    updateTraffic(dt);
    updateObstacles();
    updatePickups();
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
    reportedDistance = state.distance;

    // Anything the course already went past on a previous life stays past.
    while (obstacleCursor < obstacles.length && obstacles[obstacleCursor].d < car.d - 60) {
      obstacleCursor++;
    }
    while (trafficCursor < traffic.length && traffic[trafficCursor].anchor < car.d - 190) {
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
    var steps = ['REPAIR ROUTE INITIALIZED', '3', '2', '1', 'GO!'];
    var index = 0;
    el.brief.hidden = true;
    el.race.hidden = false;
    resize();
    el.countdown.hidden = false;
    (function tick() {
      if (index >= steps.length) {
        el.countdown.hidden = true;
        done();
        return;
      }
      var text = steps[index++];
      el.countdown.textContent = text;
      el.countdown.classList.remove('is-beat');
      void el.countdown.offsetWidth;
      el.countdown.classList.add('is-beat');
      blip(index === steps.length ? 880 : 440, 0.12, 'square', 0.1);
      setTimeout(tick, index === 1 ? 900 : 700);
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

  if (initial.status === 'expired') { over = true; }

  // Paint from the server's state, not from anything this page assumed. An
  // attempt that is over shows the time it has left, which is none of it.
  paintRepairList();
  paintHud();
}());

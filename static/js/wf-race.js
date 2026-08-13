/* CSS race — the display half of the game.
 *
 * Everything that decides anything lives on the server: when the race starts,
 * when it ends, which obstacles count and what the score is. This file draws
 * the course, reads the keys, reports what happened and renders whatever the
 * server says back. If the two ever disagree, the server wins.
 */
(function () {
  'use strict';

  function json(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : null;
  }

  var urls = json('wf-race-urls');
  var config = json('wf-race-config');
  var initial = json('wf-race-state') || {};
  if (!urls || !config) { return; }

  var brief = document.getElementById('wf-brief');
  var race = document.getElementById('wf-race');
  var startBtn = document.getElementById('wf-start-race');
  var briefError = document.getElementById('wf-brief-error');
  var shell = document.getElementById('wf-track-shell');
  var road = document.getElementById('wf-road');
  var car = document.getElementById('wf-car');
  var finish = document.getElementById('wf-finish');
  var timerEl = document.getElementById('wf-race-timer');
  var progressEl = document.getElementById('wf-race-progress');
  var obstacleEl = document.getElementById('wf-obstacles');
  var collisionEl = document.getElementById('wf-collisions');
  var message = document.getElementById('wf-race-message');
  var overPanel = document.getElementById('wf-race-over');
  var overText = document.getElementById('wf-race-over-text');

  var obstacles = Array.prototype.slice.call(document.querySelectorAll('.wf-obstacle'));
  var TOTAL = Math.min(config.obstacleCount, obstacles.length);
  var COURSE = config.course;
  var TIMES = config.obstacleTimes;

  // Seconds of road visible ahead of the car. The scroll speed follows from
  // it, so an obstacle due at t seconds appears at roughly t - LEAD.
  var LEAD_SECONDS = 4.5;
  var LANES = [20, 50, 80];
  var LANE_OF = [1, 0, 2, 1, 2, 0];

  var running = false;      // the loop is drawing
  var over = false;         // terminal: expired, completed or rejected
  var cleared = 0;          // obstacles the server has accepted
  var reported = 0;         // obstacles handed to the server (may be in flight)
  var collisions = 0;
  var x = 50;
  var keys = {};
  var hitAt = {};
  var lastSync = 0;

  // Elapsed time is anchored to the server's answer and advanced with a
  // monotonic clock, so moving the machine's clock changes nothing.
  var anchorElapsed = 0;
  var anchorAt = 0;
  var duration = config.duration;

  // How far the race clock is ahead of the course. Zero for a normal start;
  // on a mid-race refresh it is set so the course picks up where the player
  // left it instead of having scrolled away while the page reloaded. Only
  // ever positive, so every server-side timing floor is still cleared.
  var courseOffset = 0;

  function now() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }
  function elapsedNow() { return anchorElapsed + (now() - anchorAt) / 1000; }
  function remainingNow() { return Math.max(0, duration - elapsedNow()); }
  function courseNow() { return elapsedNow() - courseOffset; }

  function anchor(state) {
    if (typeof state.duration === 'number') { duration = state.duration; }
    if (typeof state.elapsed === 'number') {
      anchorElapsed = state.elapsed;
    } else if (typeof state.remaining === 'number') {
      anchorElapsed = duration - state.remaining;
    }
    anchorAt = now();
  }

  function fmt(seconds) {
    seconds = Math.max(0, Math.ceil(seconds));
    return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' +
           String(seconds % 60).padStart(2, '0');
  }

  function csrf() {
    var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  /* Every POST resolves to {ok, status, data} — a rejected request is a
   * normal outcome here, not an exception. */
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

  /* Reports are sent one at a time and in order: the server only accepts the
   * next obstacle in course order, so overlapping requests would be refused. */
  var chain = Promise.resolve();
  function enqueue(task) {
    chain = chain.then(task, task);
    return chain;
  }

  function showBriefError(text) {
    if (!briefError) { return; }
    briefError.textContent = text;
    briefError.hidden = false;
  }

  function endRace(text) {
    over = true;
    running = false;
    message.textContent = '';
    if (overPanel) {
      overText.textContent = text;
      overPanel.hidden = false;
    } else {
      message.textContent = text;
    }
  }

  /* One place decides what a server answer means for the run. */
  function applyState(state) {
    if (!state) { return false; }
    if (typeof state.obstacles === 'number' && state.obstacles > cleared) {
      cleared = state.obstacles;
      renderProgress();
    }
    if (typeof state.collisions === 'number' && state.collisions > collisions) {
      collisions = state.collisions;
      collisionEl.textContent = collisions;
    }
    if (state.status === 'completed' || state.redirect) {
      over = true;
      running = false;
      window.location.href = state.redirect || urls.result;
      return true;
    }
    if (state.status === 'expired' || state.expired) {
      endRace("Your race attempt has ended. One attempt per participant.");
      return true;
    }
    if (typeof state.remaining === 'number') { anchor(state); }
    return false;
  }

  function renderProgress() {
    obstacleEl.textContent = cleared + '/' + TOTAL;
    progressEl.style.width = (TOTAL ? (cleared / TOTAL) * 100 : 0) + '%';
  }

  // -------------------------------------------------------------- course --

  obstacles.forEach(function (node, i) {
    node.style.left = LANES[LANE_OF[i % LANE_OF.length]] + '%';
    node.style.width = '25%';
    node.style.top = '-400px';
    node.dataset.index = String(i + 1);
  });

  function speed() {
    // Pixels per second, derived from how much road is above the car.
    return (car.offsetTop + 160) / LEAD_SECONDS;
  }

  function positionCourse(elapsed) {
    var pxPerSecond = speed();
    var carTop = car.offsetTop;
    var roadHeight = road.clientHeight;

    obstacles.forEach(function (node, i) {
      if (i >= TOTAL) { node.style.opacity = '0'; return; }
      var top = carTop + (elapsed - TIMES[i]) * pxPerSecond;
      node.style.top = top + 'px';
      node.style.opacity = (top < -node.offsetHeight - 40 || top > roadHeight + 60) ? '0' : '1';
    });

    var finishTop = carTop + (elapsed - COURSE) * pxPerSecond;
    finish.style.top = finishTop + 'px';
    finish.style.opacity =
      (finishTop < -finish.offsetHeight - 40 || finishTop > roadHeight + 60) ? '0' : '1';
  }

  function overlaps(a, b) {
    return !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom);
  }

  function reportObstacle(index) {
    enqueue(function () {
      return post(urls.progress, { obstacle: index }).then(function (result) {
        if (result.ok) { applyState(result.data); return; }
        if (applyState(result.data)) { return; }
        // The server refused the clear (out of order, or too early). Fall back
        // to its count so the HUD never claims progress that was not recorded.
        cleared = (result.data && typeof result.data.obstacles === 'number')
          ? result.data.obstacles : cleared;
        reported = cleared;
        renderProgress();
      }).catch(function () {
        // Network hiccup: let the next pass try this obstacle again.
        if (reported >= index) { reported = index - 1; }
      });
    });
  }

  function reportCollision() {
    enqueue(function () {
      return post(urls.progress, { collision: 1 })
        .then(function (result) { applyState(result.data); })
        .catch(function () {});
    });
  }

  /* An obstacle is cleared once the course has carried it past the car. That
   * also covers a mid-race refresh: anything the course already went by is
   * reported on the next frame rather than being lost. */
  function clearPassedObstacles(elapsed) {
    var passSeconds = (car.offsetHeight + 70) / speed();
    while (reported < TOTAL && elapsed >= TIMES[reported] + passSeconds) {
      reported += 1;
      reportObstacle(reported);
    }
  }

  function checkCollisions() {
    var carBox = car.getBoundingClientRect();
    obstacles.forEach(function (node, i) {
      if (i >= TOTAL || node.style.opacity === '0') { return; }
      if (!overlaps(carBox, node.getBoundingClientRect())) { return; }
      var stamp = now();
      if (hitAt[i] && stamp - hitAt[i] < 900) { return; }
      hitAt[i] = stamp;
      collisions += 1;
      collisionEl.textContent = collisions;
      reportCollision();
      car.animate([
        { transform: 'translateX(-50%) scale(1)' },
        { transform: 'translateX(-50%) scale(.84) rotate(-3deg)' },
        { transform: 'translateX(-50%) scale(1)' }
      ], { duration: 240 });
    });
  }

  /* The finish line is a real object on the road: the race ends when the car
   * actually reaches it, with every obstacle behind it. */
  function crossedFinish() {
    if (cleared < TOTAL || finish.style.opacity === '0') { return false; }
    return overlaps(car.getBoundingClientRect(), finish.getBoundingClientRect());
  }

  var completing = false;
  function complete() {
    if (completing) { return; }
    completing = true;
    running = false;
    message.textContent = 'CSS FIXED!';
    // Any obstacle report still in flight has to land first, or the server
    // will not yet believe the course was cleared.
    enqueue(function () {
      return post(urls.complete, { finish: 1 }).then(function (result) {
        if (applyState(result.data)) { return; }
        if (result.ok) {
          window.location.href = (result.data && result.data.redirect) || urls.result;
          return;
        }
        // Rejected: the server does not agree the race is finished.
        completing = false;
        running = true;
        message.textContent = (result.data && result.data.error) || 'NOT FINISHED YET';
        setTimeout(function () {
          if (running) { message.textContent = ''; }
        }, 2500);
        requestAnimationFrame(loop);
      }).catch(function () {
        completing = false;
        message.textContent = 'NETWORK ERROR — RETRYING';
        setTimeout(complete, 1500);
      });
    });
  }

  function sync() {
    fetch(urls.state, { credentials: 'same-origin' })
      .then(function (response) { return response.json(); })
      .then(function (state) { if (!over) { applyState(state); } })
      .catch(function () {});
  }

  function steer() {
    if (keys.ArrowLeft || keys.a || keys.A) { x -= 0.75; }
    if (keys.ArrowRight || keys.d || keys.D) { x += 0.75; }
    x = Math.max(8, Math.min(92, x));
    car.style.left = x + '%';
  }

  function loop() {
    if (!running || over) { return; }

    var course = courseNow();
    var remaining = remainingNow();

    steer();
    positionCourse(course);
    checkCollisions();
    clearPassedObstacles(course);

    timerEl.textContent = fmt(remaining);
    timerEl.classList.toggle('warn', remaining <= config.warnSeconds && remaining > config.dangerSeconds);
    timerEl.classList.toggle('danger', remaining <= config.dangerSeconds);

    if (remaining <= 0) {
      // The display ran out; confirm with the server before saying so.
      running = false;
      sync();
      setTimeout(function () {
        if (!over) { endRace("Your race attempt has ended. One attempt per participant."); }
      }, 1200);
      return;
    }

    if (crossedFinish()) { complete(); return; }

    if (now() - lastSync > config.syncSeconds * 1000) {
      lastSync = now();
      sync();
    }
    requestAnimationFrame(loop);
  }

  function enterRace(state) {
    anchor(state);
    cleared = state.obstacles || 0;
    reported = cleared;

    // Line the course up with whatever the server says is still ahead. On a
    // fresh start this is 0 and the course simply begins at the start line.
    var nextMark = cleared < TOTAL ? TIMES[cleared] : COURSE;
    courseOffset = Math.max(0, elapsedNow() - nextMark + LEAD_SECONDS);

    collisions = state.collisions || 0;
    collisionEl.textContent = collisions;
    renderProgress();

    brief.hidden = true;
    race.hidden = false;
    shell.focus();

    running = true;
    lastSync = now();
    message.textContent = state.resumed ? 'BACK ON TRACK' : 'GO!';
    setTimeout(function () { if (running) { message.textContent = ''; } }, 900);
    requestAnimationFrame(loop);
  }

  function start() {
    if (running || over || !startBtn) { return; }
    startBtn.disabled = true;
    var label = startBtn.textContent;
    startBtn.textContent = 'STARTING…';

    post(urls.start, {}).then(function (result) {
      if (result.ok) { enterRace(result.data); return; }
      if (result.data && result.data.redirect) {
        window.location.href = result.data.redirect;
        return;
      }
      if (result.status === 403) {
        window.location.href = urls.exit;
        return;
      }
      startBtn.hidden = true;
      showBriefError((result.data && result.data.error) ||
                     'The race could not be started.');
    }).catch(function () {
      startBtn.disabled = false;
      startBtn.textContent = label;
      showBriefError('Could not reach the server. Check the connection and try again.');
    });
  }

  if (startBtn) { startBtn.addEventListener('click', start); }

  window.addEventListener('keydown', function (event) {
    if (['ArrowLeft', 'ArrowRight', 'a', 'd', 'A', 'D'].indexOf(event.key) >= 0) {
      keys[event.key] = true;
      event.preventDefault();
    }
  });
  window.addEventListener('keyup', function (event) { keys[event.key] = false; });
  shell.addEventListener('click', function (event) {
    var box = shell.getBoundingClientRect();
    x = ((event.clientX - box.left) / box.width) * 100;
  });

  if (initial.status === 'expired') { over = true; }
}());

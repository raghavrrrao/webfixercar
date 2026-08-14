/* Deterministic proof that the keyboard moves the player and nothing else.
 *
 * Runs the real `static/js/wf-race.js` under a stub DOM, steps the simulation
 * twice from the same seeded start — once with no input, once holding a key —
 * and compares what happened to the traffic. Traffic is seeded and autonomous,
 * so the two traffic timelines must be identical to the last decimal while
 * the player's own state must differ.
 *
 * This is the regression cover for the bug where `blocker` cars steered
 * towards `car.x` and therefore mirrored the player's steering keys.
 *
 * Invoked by first/test_race.py; prints one JSON object on stdout.
 */
import fs from 'node:fs';
import vm from 'node:vm';

const SCRIPT = process.argv[2];
const FRAMES = 900;                 // 15 seconds at 60fps
const DT = 1 / 60;

const CONFIG = {
  duration: 720, course: 18900, sectionMetres: 2700, sectionCount: 7,
  repairMetres: [1620, 4320, 7020, 9720, 12420, 15120, 17820],
  repairs: ['responsive', 'display', 'margin', 'padding', 'flexbox', 'position', 'grid']
    .map((id, i) => ({ id, label: id.toUpperCase(), section: 'SECTION ' + i,
                       message: 'restored', blurb: '' })),
  seed: 20260813,
  car: { topSpeed: 62, acceleration: 17, braking: 36, drag: 7, reverseSpeed: 9,
         steerRate: 2.6, crashSpeedKept: 0.5, crashGrace: 1.2 },
  warnSeconds: 300, dangerSeconds: 120, syncSeconds: 5, progressSeconds: 3
};
const URLS = { start: '/s/', state: '/t/', progress: '/p/', complete: '/c/',
               preview: '/v/', result: '/r/', home: '/h/', exit: '/e/' };
const STATE = { status: 'active', repairs: [], collisions: 0, distance: 0,
                score: 0, remaining: 720, elapsed: 0 };

const CTX_KEYS = ['fillRect', 'clearRect', 'beginPath', 'moveTo', 'lineTo', 'stroke',
  'fill', 'save', 'restore', 'translate', 'rotate', 'scale', 'setTransform',
  'setLineDash', 'strokeRect', 'fillText', 'roundRect', 'rect', 'arc', 'ellipse',
  'closePath', 'drawImage', 'createLinearGradient', 'createRadialGradient'];

function makeContext() {
  const gradient = { addColorStop() {} };
  return new Proxy({}, {
    get(target, key) {
      if (key === 'createLinearGradient' || key === 'createRadialGradient') {
        return () => gradient;
      }
      if (CTX_KEYS.includes(key)) { return () => {}; }
      return target[key] ?? 0;
    },
    set(target, key, value) { target[key] = value; return true; }
  });
}

function run(heldKey, bootRepairs) {
  const payloads = {
    'wf-race-urls': JSON.stringify(URLS),
    'wf-race-config': JSON.stringify(CONFIG),
    'wf-race-state': JSON.stringify(
      Object.assign({}, STATE, { repairs: bootRepairs || [] }))
  };
  const handlers = {};
  const windowHandlers = {};
  const elements = new Map();

  const element = (id) => {
    if (elements.has(id)) { return elements.get(id); }
    const node = {
      id, textContent: payloads[id] ?? '', hidden: false, disabled: false,
      title: '', src: '', width: 0, height: 0,
      style: { setProperty() {} },
      dataset: {}, children: [],
      parentElement: { clientWidth: 400 },
      offsetTop: 500, offsetHeight: 100, offsetWidth: 60,
      clientHeight: 700, clientWidth: 700,
      classList: { s: new Set(), add(c) { this.s.add(c); }, remove(c) { this.s.delete(c); },
                   toggle(c, on) { on ? this.s.add(c) : this.s.delete(c); },
                   contains(c) { return this.s.has(c); } },
      addEventListener(type, fn) { handlers[id + ':' + type] = fn; },
      removeEventListener() {}, setAttribute() {}, focus() {},
      getBoundingClientRect: () => ({ left: 0, top: 0, right: 900, bottom: 700,
                                      width: 900, height: 700 }),
      getContext: makeContext
    };
    if (id === 'wf-repairs') {
      node.children = CONFIG.repairs.map((r) => {
        const item = element('item-' + r.id);
        item.dataset.repair = r.id;
        return item;
      });
    }
    elements.set(id, node);
    return node;
  };

  let clock = 0;
  const sandbox = {
    console, Math, Date, JSON, Promise, Object, Array, String, Number, Error,
    RegExp, Set, Map, URLSearchParams, parseInt, parseFloat, isNaN,
    document: {
      cookie: '',
      getElementById: element,
      querySelectorAll: () => [],
      createElement: (tag) => (tag === 'canvas'
        ? { width: 0, height: 0, getContext: makeContext }
        : element('created-' + tag))
    },
    setTimeout: () => 0,
    clearTimeout: () => {},
    requestAnimationFrame: () => 0,
    performance: { now: () => clock },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
  };
  sandbox.window = {
    addEventListener: (type, fn) => { windowHandlers[type] = fn; },
    performance: sandbox.performance,
    localStorage: { getItem: () => '1', setItem() {} },
    location: { href: '' }
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SCRIPT, 'utf8'), sandbox, { filename: 'wf-race.js' });

  const probe = sandbox.window.__wfRaceProbe;
  if (!probe) { throw new Error('wf-race.js exposed no simulation probe'); }

  // Spawn one fixed set of vehicles and never re-run the spawn window, so
  // both runs simulate *the same cars* for the same number of frames. Without
  // this a throttled run simply travels further and meets different traffic,
  // which would look like a difference and prove nothing.
  probe.spawn(3000);
  const before = probe.snapshotTraffic();

  if (heldKey) { windowHandlers.keydown({ key: heldKey, preventDefault() {} }); }

  for (let frame = 0; frame < FRAMES; frame++) {
    clock += DT * 1000;
    probe.stepPlayer(DT);       // reads the keyboard
    probe.stepTraffic(DT);      // must not
  }

  return {
    player: { x: +probe.car.x.toFixed(6), d: +probe.car.d.toFixed(6),
              speed: +probe.car.speed.toFixed(6) },
    before: before,
    traffic: probe.snapshotTraffic(),
    // index of the first repair still on the course
    nextPickup: probe.pickups.findIndex((p) => !p.taken)
  };
}

const idle = run(null);
const steering = run('a');
const steeringRight = run('d');
const accelerating = run('w');
const braking = run('s');
// a resumed race: the repairs already recorded must be off the course
const resumed = run(null, ['responsive', 'display', 'margin']);

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const moved = (run_) => run_.traffic.some((car, i) => car.d !== run_.before[i].d);
const laneChanged = (run_) => run_.traffic.some((car, i) =>
  car.targetLane !== run_.before[i].targetLane || car.x !== run_.before[i].x);

console.log(JSON.stringify({
  trafficCount: idle.traffic.length,
  // the claim: identical vehicles, identical timeline, whatever is held down
  trafficUnchangedBySteerLeft: same(idle.traffic, steering.traffic),
  trafficUnchangedBySteerRight: same(idle.traffic, steeringRight.traffic),
  trafficUnchangedByThrottle: same(idle.traffic, accelerating.traffic),
  trafficUnchangedByBraking: same(idle.traffic, braking.traffic),
  // ...and the test is not vacuous: the player moved and so did the traffic
  playerSteeredLeft: steering.player.x < idle.player.x,
  playerSteeredRight: steeringRight.player.x > idle.player.x,
  playerAccelerated: accelerating.player.speed > idle.player.speed &&
                     accelerating.player.d > idle.player.d,
  trafficDroveItself: moved(idle),
  trafficChangedLanesItself: laneChanged(idle),
  freshNextPickup: idle.nextPickup,
  resumedNextPickup: resumed.nextPickup,
  idlePlayer: idle.player,
  steeringPlayer: steering.player,
  throttlePlayer: accelerating.player
}, null, 2));

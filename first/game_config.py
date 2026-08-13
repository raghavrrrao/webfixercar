"""
Single source of truth for the Website Fixer challenge.

Everything the game needs to know about "what the player is fixing" and
"how long they get" lives here, so views, checks and templates never
hardcode duplicate copies of it.

This is a **CSS-only** challenge. The challenge itself is not written in
this file; it ships as three plain files under ``first/challenge/novacloud/``:

    index.html      the finished page -- READ ONLY, never edited or submitted
    style.css       the broken stylesheet the player repairs
    solution.css    the organisers' gold standard, never sent to a browser

`style.css` carries 37 deliberate defects; 14 of them are graded objectives.
The rest are cosmetic noise the player may fix or ignore. `GRADED_FIXES`
below is the authoritative list of what is scored, and `checks.py` grades the
*outcome* of each objective rather than matching this text.
"""

from pathlib import Path

# Hard 12 minute racing session. This is the *maximum* the attempt may last,
# not how long the course takes; it is enforced by the server.
GAME_DURATION_SECONDS = 12 * 60

# Below this many seconds the race UI switches its timer into warning/danger states.
TIMER_DANGER_SECONDS = 2 * 60
TIMER_WARNING_SECONDS = 5 * 60

# How often the browser re-syncs its countdown with the server.
TIMER_SYNC_INTERVAL_SECONDS = 20

# How often the running race re-reads the authoritative clock. Short, because
# this is what makes a tampered-with browser clock stop mattering.
RACE_STATE_SYNC_SECONDS = 5


# ------------------------------------------------------------- the race ----
#
# The course is measured in metres, and the car drives it. How long a run
# takes is up to the driver, which is the whole point: the twelve minutes
# above is the *ceiling* on an attempt, not its length.
#
# It is also what lets the server check the browser's claims. Distance is
# reported, never invented: the car cannot have travelled further than
# `RACE_TOP_SPEED` allows in the time the server has been counting, so a
# repair cannot be collected before the car could have reached it and the
# finish cannot be crossed before the course could have been driven.

# The course: seven sections, one per CSS repair.
#
# The length is tuned against the car below. Flat out with a clean line the
# course takes about 5 minutes; lifting off for traffic the way an average
# driver has to puts it around 6, and somebody who keeps having to slow down
# and go back for a repair lands near 10 — inside the twelve-minute ceiling,
# but only just, which is the tension the round is supposed to have.
RACE_SECTION_METRES = 2700
RACE_SECTION_COUNT = 7
RACE_COURSE_METRES = RACE_SECTION_METRES * RACE_SECTION_COUNT   # 18.9 km

# Where the repair pickup sits inside its section.
RACE_REPAIR_MARK = 0.6
RACE_REPAIR_METRES = tuple(
    int((index + RACE_REPAIR_MARK) * RACE_SECTION_METRES)
    for index in range(RACE_SECTION_COUNT)
)

# The car, in metres and seconds. Tuned so a confident driver finishes in
# roughly four to six minutes, an average one in six to nine, and somebody
# who keeps hitting things is still inside the twelve-minute ceiling.
RACE_TOP_SPEED = 62.0          # m/s, about 223 km/h on the dial
RACE_ACCELERATION = 17.0       # m/s², standstill to top speed in ~4s
RACE_BRAKING = 36.0            # m/s² on the brake
RACE_DRAG = 7.0                # m/s² coasting
RACE_REVERSE_SPEED = 9.0       # m/s, enough to back out of a bad line
RACE_STEER_RATE = 2.6          # lanes per second at speed
RACE_CRASH_SPEED_KEPT = 0.5    # fraction of speed surviving a collision
RACE_CRASH_GRACE_SECONDS = 1.2 # one crash is one penalty, not five

# Distance is checked against what the car could physically have covered.
# The tolerance absorbs frame timing and a late progress report; it is far
# too small to drive the course in less than `RACE_MIN_SECONDS`.
RACE_SPEED_TOLERANCE = 1.08
RACE_DISTANCE_GRACE_METRES = 120

# The floor this puts under an honest run: ~228 seconds for 14.7 km.
RACE_MIN_SECONDS = int(RACE_COURSE_METRES / (RACE_TOP_SPEED * RACE_SPEED_TOLERANCE))

# How often the browser reports distance. Every event that matters (a repair)
# is sent immediately; routine progress rides on this tick.
RACE_PROGRESS_INTERVAL_SECONDS = 3

# A hostile client can post collisions all day; it only ever costs it points,
# but the counter still needs a ceiling it cannot overflow.
RACE_MAX_COLLISIONS = 500
RACE_MAX_COLLISIONS_PER_REPORT = 30

# The course layout is generated from this seed, so every participant at
# every PC drives exactly the same traffic and the same obstacles.
RACE_COURSE_SEED = 20260813

# Server-side scoring. Nothing here is ever taken from the browser.
RACE_MAX_SCORE = 1000
RACE_BASE_POINTS = 150
RACE_TIME_POINTS = 400
RACE_REPAIR_POINTS = 50        # x7 = 350
RACE_COLLISION_PENALTY = 10
RACE_CLEAN_RUN_BONUS = 100     # 150 + 400 + 350 + 100 = 1000


# ------------------------------------------------------------ challenge ----

# There is one challenge, so it is named rather than numbered.
CHALLENGE_LABEL = 'CSS Challenge'
SITE_NAME = 'NovaCloud'
SITE_TAGLINE = 'AI-powered cloud platform landing page'
DIFFICULTY = 'Basic'
MODE = 'CSS racing challenge'

CHALLENGE_DIR = Path(__file__).resolve().parent / 'challenge' / 'novacloud'


def _read(name):
    return (CHALLENGE_DIR / name).read_text(encoding='utf-8')


# The finished markup. It is shown read-only, is never accepted from the
# browser, and is what every submission is graded against.
CHALLENGE_HTML = _read('index.html')

# The broken stylesheet the player repairs, and the intended result.
STARTER_CSS = _read('style.css')
SOLUTION_CSS = _read('solution.css')


# ----------------------------------------------------------------- fixes ----
#
# objective id -> the (broken, fixed) edits that clear it. Grouped edits count
# as one objective: the stats band needs its column count and its alignment,
# the steps row needs its column count and its padding, the pricing row needs
# its gap and the size of the featured card.
#
# These are the *reference* answers. The checker accepts any equivalent
# result, so this table exists for the tests and for organisers -- it is not
# a string comparison the player has to match.

GRADED_FIXES = {
    'css-line-height': (
        ('  line-height: 1;\n  -webkit-font-smoothing',
         '  line-height: 1.6;\n  -webkit-font-smoothing'),
    ),
    'css-navbar-row': (
        ('.navbar {\n  display: block;', '.navbar {\n  display: flex;'),
    ),
    'css-nav-spacing': (
        ('  gap: 2px;\n  flex: 1;\n  justify-content: flex-end;\n}',
         '  gap: 32px;\n  flex: 1;\n  justify-content: center;\n}'),
    ),
    'css-hero-split': (
        ('  display: grid;\n  grid-template-columns: 1fr;\n  align-items: center;\n  gap: 64px;',
         '  display: grid;\n  grid-template-columns: 1fr 1fr;\n  align-items: center;\n  gap: 64px;'),
    ),
    'css-hero-title': (
        ('.hero__title {\n  font-size: 1rem;',
         '.hero__title {\n  font-size: clamp(2.4rem, 4.4vw, 3.6rem);'),
    ),
    'css-hero-gap': (
        ('.hero__actions {\n  display: flex;\n  align-items: center;\n  gap: 150px;',
         '.hero__actions {\n  display: flex;\n  align-items: center;\n  gap: 16px;'),
    ),
    'css-console': (
        ('  overflow: hidden;\n  transform: rotate(45deg);',
         '  overflow: hidden;\n  transform: rotate(1.2deg);'),
    ),
    'css-stats-band': (
        ('.stats__grid {\n  display: grid;\n  grid-template-columns: repeat(2, 1fr);',
         '.stats__grid {\n  display: grid;\n  grid-template-columns: repeat(4, 1fr);'),
        ('.stat-card {\n  text-align: left;', '.stat-card {\n  text-align: center;'),
    ),
    'css-features': (
        ('.features__grid {\n  display: grid;\n  grid-template-columns: repeat(1, 1fr);',
         '.features__grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);'),
    ),
    'css-feature-box': (
        ('  border-radius: 0;\n  padding: 4px;\n  transition: transform',
         '  border-radius: var(--radius-md);\n  padding: 32px;\n  transition: transform'),
    ),
    'css-feature-icon': (
        ('  width: 180px;\n  height: 48px;\n  border-radius: var(--radius-sm);',
         '  width: 48px;\n  height: 48px;\n  border-radius: var(--radius-sm);'),
    ),
    'css-steps': (
        ('.steps {\n  display: grid;\n  grid-template-columns: repeat(4, 1fr);',
         '.steps {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);'),
        ('.step {\n  position: relative;\n  padding: 0;',
         '.step {\n  position: relative;\n  padding: 32px 28px;'),
    ),
    'css-pricing': (
        ('  grid-template-columns: repeat(3, 1fr);\n  gap: 0px;\n  align-items: stretch;',
         '  grid-template-columns: repeat(3, 1fr);\n  gap: 28px;\n  align-items: stretch;'),
        ('  border-color: transparent;\n  transform: scale(0.85);',
         '  border-color: transparent;\n  transform: scale(1.04);'),
    ),
    'css-responsive': (
        ('@media (min-width: 860px) {', '@media (max-width: 860px) {'),
    ),
}


def apply_fixes(source, fixes):
    """Apply every (broken, fixed) pair to `source`, exactly once each.

    `fixes` may be a flat sequence of pairs or a mapping of objective id ->
    pairs. Raises if an anchor is missing or ambiguous, so a drifted
    challenge file fails loudly instead of silently grading the wrong thing.
    """
    if isinstance(fixes, dict):
        fixes = [pair for pairs in fixes.values() for pair in pairs]

    for broken, fixed in fixes:
        if source.count(broken) != 1:
            raise ValueError(f'fix anchor is not unique: {broken!r}')
        source = source.replace(broken, fixed, 1)
    return source

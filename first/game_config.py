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
# The course scrolls at a fixed pace, so "where the car is" is a pure function
# of how long the race has been running. That is what lets the *server* check
# the browser's claims: an obstacle cannot be cleared before the course has
# carried it to the car, and the finish line cannot be reached before the
# whole course has gone past.

# How long the course itself takes to scroll from the start to the finish line.
RACE_COURSE_SECONDS = 180

# Obstacles on the course, and where each one sits along it (0 = start line,
# 1 = finish line). The browser positions them from exactly these numbers.
RACE_OBSTACLE_COUNT = 6
RACE_OBSTACLE_MARKS = (0.10, 0.24, 0.38, 0.52, 0.66, 0.82)

# The earliest second at which each obstacle can possibly reach the car.
RACE_OBSTACLE_TIMES = tuple(
    int(mark * RACE_COURSE_SECONDS) for mark in RACE_OBSTACLE_MARKS
)

# Slack allowed on those times, for latency and frame timing. Small enough
# that it cannot be used to skip the course, large enough to be fair.
RACE_TIMING_GRACE_SECONDS = 4

# A hostile client can post collisions all day; it only ever costs it points,
# but the counter still needs a ceiling it cannot overflow.
RACE_MAX_COLLISIONS = 200

# Server-side scoring. Nothing here is ever taken from the browser.
RACE_MAX_SCORE = 1000
RACE_BASE_POINTS = 200
RACE_TIME_POINTS = 400
RACE_OBSTACLE_POINTS = 60
RACE_COLLISION_PENALTY = 15
RACE_CLEAN_RUN_BONUS = 40


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

"""
The seven CSS repairs the race hands out, and what each one actually fixes.

This is the bridge between the game and the website. Each repair owns a slice
of the *real* difference between the broken `style.css` the participant is
shown and the finished `solution.css` they are racing for -- the slice that
belongs to its CSS concept. Collecting a repair applies that slice, so the
preview beside the track changes because the stylesheet genuinely changed.

Nothing here is a mock-up. The seven slices are a partition of the whole diff:
one rule each, no rule in two slices, no rule in none. So a full set composes
into `solution.css` exactly, and `RepairLayerTests` fails the moment that
stops being true.

The order below is the order the repairs appear on the course, and it is
chosen so each one is *visible* when it lands. RESPONSIVE comes first because
the broken `@media (min-width: 860px)` applies the phone rules to a desktop
window: until it is fixed it hides the nav menu and flattens the hero, which
would mask several of the repairs that follow.
"""

from .game_config import SOLUTION_CSS, STARTER_CSS, apply_fixes


# ---------------------------------------------------------- RESPONSIVE ----
RESPONSIVE_FIXES = (
    (
        '@media (min-width: 860px) {',
        '@media (max-width: 860px) {',
    ),
)

# ------------------------------------------------------------- DISPLAY ----
DISPLAY_FIXES = (
    (
        'button {\n  font: inherit;\n  color: inherit;\n  background: none;\n  border: none;\n  cursor: default;',
        'button {\n  font: inherit;\n  color: inherit;\n  background: none;\n  border: none;\n  cursor: pointer;',
    ),
    (
        '.navbar {\n  display: block;',
        '.navbar {\n  display: flex;',
    ),
    (
        '.hero {\n  position: relative;\n  padding: 120px 0 100px;\n  overflow: visible;',
        '.hero {\n  position: relative;\n  padding: 120px 0 100px;\n  overflow: hidden;',
    ),
    (
        '.hero__title {\n  font-size: 1rem;',
        '.hero__title {\n  font-size: clamp(2.4rem, 4.4vw, 3.6rem);',
    ),
    (
        '.console__stats {\n  display: block;',
        '.console__stats {\n  display: flex;',
    ),
    (
        '.feature-card__icon {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  width: 180px;',
        '.feature-card__icon {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  width: 48px;',
    ),
    (
        '.why-us__visual {\n  position: relative;\n  height: 140px;',
        '.why-us__visual {\n  position: relative;\n  height: 420px;',
    ),
    (
        '.orbit-core {\n  position: relative;\n  width: 340px;',
        '.orbit-core {\n  position: relative;\n  width: 180px;',
    ),
    (
        '.testimonial-card__avatar {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  width: 40px;\n  height: 40px;\n  border-radius: 0%;',
        '.testimonial-card__avatar {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  width: 40px;\n  height: 40px;\n  border-radius: 50%;',
    ),
    (
        '.reveal--visible {\n  opacity: 0.3;',
        '.reveal--visible {\n  opacity: 1;',
    ),
)

# -------------------------------------------------------------- MARGIN ----
MARGIN_FIXES = (
    (
        'body {\n  font-family: var(--font-body);\n  background: var(--color-bg);\n  color: var(--color-text);\n  line-height: 1;',
        'body {\n  font-family: var(--font-body);\n  background: var(--color-bg);\n  color: var(--color-text);\n  line-height: 1.6;',
    ),
    (
        '.hero__actions {\n  display: flex;\n  align-items: center;\n  gap: 150px;',
        '.hero__actions {\n  display: flex;\n  align-items: center;\n  gap: 16px;',
    ),
    (
        '.why-us__layout {\n  display: grid;\n  grid-template-columns: 1fr 1fr;\n  align-items: center;\n  gap: 4px;',
        '.why-us__layout {\n  display: grid;\n  grid-template-columns: 1fr 1fr;\n  align-items: center;\n  gap: 72px;',
    ),
    (
        '.pricing__grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);\n  gap: 0px;',
        '.pricing__grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);\n  gap: 28px;',
    ),
    (
        '.faq-item__icon {\n  font-size: 1.2rem;\n  color: var(--color-primary);\n  transition: transform var(--transition-fast);\n  flex-shrink: 0;\n  margin-left: -20px;',
        '.faq-item__icon {\n  font-size: 1.2rem;\n  color: var(--color-primary);\n  transition: transform var(--transition-fast);\n  flex-shrink: 0;\n  margin-left: 16px;',
    ),
)

# ------------------------------------------------------------- PADDING ----
PADDING_FIXES = (
    (
        '.feature-card {\n  background: var(--color-bg);\n  border: 1px solid var(--color-border);\n  border-radius: 0;\n  padding: 4px;',
        '.feature-card {\n  background: var(--color-bg);\n  border: 1px solid var(--color-border);\n  border-radius: var(--radius-md);\n  padding: 32px;',
    ),
    (
        '.step {\n  position: relative;\n  padding: 0;',
        '.step {\n  position: relative;\n  padding: 32px 28px;',
    ),
)

# ------------------------------------------------------------- FLEXBOX ----
FLEXBOX_FIXES = (
    (
        '.navbar__menu {\n  display: flex;\n  align-items: center;\n  gap: 2px;\n  flex: 1;\n  justify-content: flex-end;',
        '.navbar__menu {\n  display: flex;\n  align-items: center;\n  gap: 32px;\n  flex: 1;\n  justify-content: center;',
    ),
    (
        '.trusted__logos {\n  display: flex;\n  align-items: center;\n  justify-content: space-between;',
        '.trusted__logos {\n  display: flex;\n  align-items: center;\n  justify-content: center;',
    ),
    (
        '.pricing-card__features {\n  display: flex;\n  flex-direction: column;\n  gap: 0px;',
        '.pricing-card__features {\n  display: flex;\n  flex-direction: column;\n  gap: 14px;',
    ),
    (
        '.faq-item__question {\n  width: 100%;\n  display: flex;\n  align-items: center;\n  justify-content: center;',
        '.faq-item__question {\n  width: 100%;\n  display: flex;\n  align-items: center;\n  justify-content: space-between;',
    ),
    (
        '.contact-cta__actions {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  gap: 0px;',
        '.contact-cta__actions {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  gap: 16px;',
    ),
    (
        '.footer__bottom {\n  display: flex;\n  align-items: center;\n  justify-content: center;',
        '.footer__bottom {\n  display: flex;\n  align-items: center;\n  justify-content: space-between;',
    ),
)

# ------------------------------------------------------------ POSITION ----
POSITION_FIXES = (
    (
        '.btn--primary:hover {\n  transform: translateY(20px);',
        '.btn--primary:hover {\n  transform: translateY(-2px);',
    ),
    (
        '.site-header {\n  position: sticky;\n  top: 0;\n  z-index: 1;',
        '.site-header {\n  position: sticky;\n  top: 0;\n  z-index: 100;',
    ),
    (
        '.console {\n  background: var(--color-dark);\n  border-radius: var(--radius-lg);\n  box-shadow: var(--shadow-lg);\n  overflow: hidden;\n  transform: rotate(45deg);',
        '.console {\n  background: var(--color-dark);\n  border-radius: var(--radius-lg);\n  box-shadow: var(--shadow-lg);\n  overflow: hidden;\n  transform: rotate(1.2deg);',
    ),
    (
        '.orbit-card--1 { top: 50%; left: 50%; animation-delay: 0s; }',
        '.orbit-card--1 { top: 6%; left: 4%; animation-delay: 0s; }',
    ),
    (
        '.pricing-card--featured {\n  background: linear-gradient(180deg, var(--color-dark), var(--color-dark-alt));\n  color: var(--color-text-inverse);\n  border-color: transparent;\n  transform: scale(0.85);',
        '.pricing-card--featured {\n  background: linear-gradient(180deg, var(--color-dark), var(--color-dark-alt));\n  color: var(--color-text-inverse);\n  border-color: transparent;\n  transform: scale(1.04);',
    ),
    (
        '.pricing-card__badge {\n  position: absolute;\n  top: 40%;',
        '.pricing-card__badge {\n  position: absolute;\n  top: -14px;',
    ),
)

# ---------------------------------------------------------------- GRID ----
GRID_FIXES = (
    (
        '.hero__container {\n  position: relative;\n  z-index: 1;\n  display: grid;\n  grid-template-columns: 1fr;',
        '.hero__container {\n  position: relative;\n  z-index: 1;\n  display: grid;\n  grid-template-columns: 1fr 1fr;',
    ),
    (
        '.stats__grid {\n  display: grid;\n  grid-template-columns: repeat(2, 1fr);',
        '.stats__grid {\n  display: grid;\n  grid-template-columns: repeat(4, 1fr);',
    ),
    (
        '.stat-card {\n  text-align: left;',
        '.stat-card {\n  text-align: center;',
    ),
    (
        '.features__grid {\n  display: grid;\n  grid-template-columns: repeat(1, 1fr);',
        '.features__grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);',
    ),
    (
        '.steps {\n  display: grid;\n  grid-template-columns: repeat(4, 1fr);',
        '.steps {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);',
    ),
    (
        '.testimonials__grid {\n  display: grid;\n  grid-template-columns: repeat(2, 1fr);',
        '.testimonials__grid {\n  display: grid;\n  grid-template-columns: repeat(3, 1fr);',
    ),
    (
        '.footer__grid {\n  display: grid;\n  grid-template-columns: 1fr 1fr 1fr 1fr;',
        '.footer__grid {\n  display: grid;\n  grid-template-columns: 2fr 1fr 1fr 1fr;',
    ),
)


# The course order: index 0 is the first repair on the track.
REPAIRS = (
    {
        'id': 'responsive',
        'label': 'RESPONSIVE',
        'section': 'RESPONSIVE CHAOS',
        'message': 'Responsive behaviour restored',
        'blurb': 'The breakpoint fired the wrong way round, so the desktop layout was wearing the phone stylesheet.',
        'fixes': RESPONSIVE_FIXES,
    },
    {
        'id': 'display',
        'label': 'DISPLAY',
        'section': 'DISPLAY DISASTER',
        'message': 'Layout display restored',
        'blurb': 'Boxes that should lay out in a row were stacking, and several were the wrong size entirely.',
        'fixes': DISPLAY_FIXES,
    },
    {
        'id': 'margin',
        'label': 'MARGIN',
        'section': 'MARGIN MADNESS',
        'message': 'Outer spacing restored',
        'blurb': 'Gaps between things were enormous where they should be small, and missing where they should not.',
        'fixes': MARGIN_FIXES,
    },
    {
        'id': 'padding',
        'label': 'PADDING',
        'section': 'PADDING PIT',
        'message': 'Inner spacing restored',
        'blurb': 'Cards had no room inside them, so their content sat hard against the edge.',
        'fixes': PADDING_FIXES,
    },
    {
        'id': 'flexbox',
        'label': 'FLEXBOX',
        'section': 'FLEXBOX FAILURE',
        'message': 'Flexible layout restored',
        'blurb': 'Flex rows were pushing their items to the wrong end, with no gap between them.',
        'fixes': FLEXBOX_FIXES,
    },
    {
        'id': 'position',
        'label': 'POSITION',
        'section': 'POSITION PROBLEM',
        'message': 'Element positioning restored',
        'blurb': 'Positioned and transformed elements had drifted away from where they belong.',
        'fixes': POSITION_FIXES,
    },
    {
        'id': 'grid',
        'label': 'GRID',
        'section': 'GRIDLOCK',
        'message': 'Grid structure restored',
        'blurb': 'Every grid on the page had collapsed to the wrong number of columns.',
        'fixes': GRID_FIXES,
    },
)


REPAIR_IDS = tuple(repair['id'] for repair in REPAIRS)
REPAIR_COUNT = len(REPAIRS)
REPAIRS_BY_ID = {repair['id']: repair for repair in REPAIRS}

# What the browser is told about the repairs. Deliberately carries no CSS:
# composing the stylesheet is the server's job, and the page never sees the
# answers before it has earned them.
REPAIR_CARDS = tuple(
    {
        'id': repair['id'],
        'label': repair['label'],
        'section': repair['section'],
        'message': repair['message'],
        'blurb': repair['blurb'],
    }
    for repair in REPAIRS
)


def repair_index(repair_id):
    """Position of `repair_id` on the course, or None if there is no such repair."""
    try:
        return REPAIR_IDS.index(repair_id)
    except ValueError:
        return None


def clean_repair_ids(raw):
    """Read a stored repair list back, dropping anything unrecognised.

    Stored as a comma-separated string on the participant, so this is the one
    place that turns it back into ids and the one place that decides what
    counts as a valid one.
    """
    if not raw:
        return []
    seen, ids = set(), []
    for token in str(raw).split(','):
        token = token.strip()
        if token in REPAIRS_BY_ID and token not in seen:
            seen.add(token)
            ids.append(token)
    return ids


def repair_css(collected):
    """The stylesheet as it stands after `collected` repairs have been applied.

    An empty list gives the broken stylesheet the participant starts with; the
    full set gives `solution.css`. `apply_fixes` raises if any anchor has
    drifted, so a stylesheet edited out from under this table fails loudly
    rather than silently composing something wrong.
    """
    ids = set(clean_repair_ids(collected) if isinstance(collected, str) else collected)
    css = STARTER_CSS
    for repair in REPAIRS:
        if repair['id'] in ids:
            css = apply_fixes(css, repair['fixes'])
    return css


def is_fully_repaired(css):
    """True when `css` is the finished stylesheet, banner comment aside.

    The two files differ by their own header comment as well as by the
    defects; that comment is documentation, not a repair, so it is not part of
    anybody's slice.
    """
    return _without_banner(css) == _without_banner(SOLUTION_CSS)


def _without_banner(css):
    _, _, rest = css.partition('*/')
    return rest

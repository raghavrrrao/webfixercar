"""
Server-side validation of the player's HTML/CSS.

Player code is *parsed*, never executed. The HTML is walked with the
stdlib HTMLParser and the CSS is read with a deliberately small
tolerant parser -- enough to answer "did they fix this declaration?"
without pulling in a full CSS engine.

Checks are outcome-based on purpose: they ask "is the navigation laid out
in a row?", not "does line 74 say `display: flex`". Whitespace, property
order, comments and equivalent values (flex vs inline-flex, 3rem vs 48px,
repeat(3, 1fr) vs auto-fit) are all accepted.
"""

import re
from html.parser import HTMLParser

VOID_TAGS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
}

# Enough named colours for the palette this challenge uses.
NAMED_COLORS = {
    'white': '#ffffff', 'black': '#000000', 'red': '#ff0000',
    'lime': '#00ff00', 'blue': '#0000ff', 'transparent': 'transparent',
}

AUTO_COLUMNS = -1  # grid-template-columns using auto-fit / auto-fill


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

class Node:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children = []
        self.text = ''

    @property
    def classes(self):
        return set(self.attrs.get('class', '').split())

    def has_class(self, name):
        return name in self.classes

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()

    def find_all(self, tag=None, cls=None):
        return [
            n for n in self.walk()
            if (tag is None or n.tag == tag) and (cls is None or n.has_class(cls))
        ]

    def find(self, tag=None, cls=None, node_id=None):
        for n in self.walk():
            if tag is not None and n.tag != tag:
                continue
            if cls is not None and not n.has_class(cls):
                continue
            if node_id is not None and n.attrs.get('id') != node_id:
                continue
            return n
        return None

    def all_text(self):
        return (self.text + ''.join(c.all_text() for c in self.children)).strip()


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or '') for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or '') for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        # Tolerant close: unwind to the nearest matching open tag, ignore strays.
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        self.stack[-1].text += data


def parse_html(source):
    builder = _TreeBuilder()
    builder.feed(source or '')
    builder.close()
    return builder.root


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------

class Rule:
    def __init__(self, selector, declarations, media=None):
        self.selector = selector
        self.declarations = declarations
        self.media = media


def _strip_comments(css):
    return re.sub(r'/\*.*?\*/', ' ', css or '', flags=re.S)


def _split_top_level(text, separator):
    """Split on `separator`, ignoring anything inside (), [] or quotes."""
    parts, buf, depth, quote = [], '', 0, None
    for ch in text:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in '"\'':
            quote = ch
        elif ch in '([':
            depth += 1
        elif ch in ')]':
            depth = max(0, depth - 1)
        elif ch == separator and depth == 0:
            parts.append(buf)
            buf = ''
            continue
        buf += ch
    parts.append(buf)
    return parts


def _parse_declarations(body):
    declarations = []
    for chunk in _split_top_level(body, ';'):
        if ':' not in chunk:
            continue
        prop, _, value = chunk.partition(':')
        prop = prop.strip().lower()
        value = re.sub(r'!\s*important', '', value, flags=re.I).strip()
        if prop and value:
            declarations.append((prop, value))
    return declarations


def _parse_rules(text, media, out):
    i, prelude = 0, ''
    while i < len(text):
        ch = text[i]
        if ch == '{':
            depth, j = 1, i + 1
            while j < len(text) and depth:
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                j += 1
            body = text[i + 1:j - 1]
            head = prelude.strip()
            if head.startswith('@'):
                if head.lower().startswith('@media'):
                    _parse_rules(body, head, out)
                # @keyframes / @font-face carry no declarations we validate
            elif head:
                declarations = _parse_declarations(body)
                for selector in _split_top_level(head, ','):
                    out.append(Rule(selector.strip(), declarations, media))
            prelude = ''
            i = j
            continue
        if ch == '}':
            prelude = ''
            i += 1
            continue
        prelude += ch
        i += 1


def parse_css(source):
    rules = []
    _parse_rules(_strip_comments(source), None, rules)
    return rules


def _targets(selector, target, exact=False):
    """True when `selector`'s subject is exactly `target` (e.g. `.feature-grid`).

    Ancestors are normally allowed (`.section .feature-grid`) but pseudo-classes
    never are (`.btn-primary:hover` must not answer questions asked about
    `.btn-primary`).

    `exact=True` additionally rejects descendant selectors, so a contextual
    override like `.cta .btn-primary` cannot stand in for the base rule.
    """
    selector = selector.strip()
    parts = re.split(r'[\s>+~]+', selector)
    if exact and len(parts) > 1:
        return False
    return parts[-1] == target


def declared(rules, target, prop, media='desktop', exact=False):
    """Last declared value of `prop` for `target`, or None.

    media='desktop' -> rules outside any @media
    media='narrow'  -> rules inside a max-width @media block
    media='any'     -> everywhere
    """
    found = None
    for rule in rules:
        if not _targets(rule.selector, target, exact=exact):
            continue
        if media == 'desktop' and rule.media:
            continue
        if media == 'narrow' and not (rule.media and 'max-width' in rule.media.lower()):
            continue
        for name, value in rule.declarations:
            if name == prop:
                found = value
    return found


def _normalize_color(value):
    if not value:
        return None
    token = re.sub(r'!\s*important', '', value, flags=re.I).strip().lower()
    token = NAMED_COLORS.get(token, token)
    if re.fullmatch(r'#[0-9a-f]{3}', token):
        return '#' + ''.join(ch * 2 for ch in token[1:])
    return token


def _color_from_background(value):
    if not value:
        return None
    if 'gradient' in value.lower():
        return None
    for token in value.split():
        if token.startswith('#') or token.startswith('rgb') or token in NAMED_COLORS:
            return _normalize_color(token)
    return None


def _length_px(token):
    """One CSS length in px, or None. Understands px/rem/em/pt and bare 0."""
    if token is None:
        return None
    token = token.strip().lower()
    if re.fullmatch(r'0+(\.0+)?', token):
        return 0.0
    match = re.fullmatch(r'(\d*\.?\d+)\s*(px|rem|em|pt)', token)
    if not match:
        return None
    size, unit = float(match.group(1)), match.group(2)
    if unit in ('rem', 'em'):
        return size * 16
    if unit == 'pt':
        return size * 4 / 3
    return size


def _to_px(value):
    """Largest length in `value`, in px. Handles px/rem/em/pt and clamp()."""
    best = None
    for number, unit in re.findall(r'(\d*\.?\d+)\s*(px|rem|em|pt)', value or '', re.I):
        size = float(number)
        unit = unit.lower()
        if unit in ('rem', 'em'):
            size *= 16
        elif unit == 'pt':
            size *= 4 / 3
        best = size if best is None else max(best, size)
    return best


def _lengths(value):
    return [t for t in _split_top_level(value or '', ' ') if t.strip()]


def _padding_top(rules, target):
    """Effective top padding in px from `padding` / `padding-block` / `padding-top`."""
    top = None
    shorthand = declared(rules, target, 'padding')
    if shorthand:
        parts = _lengths(shorthand)
        top = _length_px(parts[0]) if parts else None
    block = declared(rules, target, 'padding-block')
    if block:
        parts = _lengths(block)
        top = _length_px(parts[0]) if parts else top
    explicit = declared(rules, target, 'padding-top')
    if explicit is not None:
        top = _length_px(explicit)
    return top


def _smallest_gap(rules, target):
    """Smallest gap in px across gap / row-gap / column-gap, or None."""
    sizes = []
    for prop in ('gap', 'row-gap', 'column-gap'):
        value = declared(rules, target, prop)
        if not value:
            continue
        for token in _lengths(value):
            size = _length_px(token)
            if size is not None:
                sizes.append(size)
    return min(sizes) if sizes else None


def _max_rotation(value):
    """Largest absolute rotation/skew in degrees inside a transform value."""
    if not value or value.strip().lower() in ('none', 'initial', 'unset'):
        return 0.0
    angles = [
        abs(float(degrees))
        for args in re.findall(r'(?:rotate|rotatez|skew|skewx|skewy)[^(]*\(([^)]*)\)', value, re.I)
        for degrees in re.findall(r'(-?\d*\.?\d+)\s*deg', args, re.I)
    ]
    return max(angles) if angles else 0.0


def _count_columns(value):
    if not value:
        return None
    lowered = value.lower()
    if 'auto-fit' in lowered or 'auto-fill' in lowered:
        return AUTO_COLUMNS
    repeat = re.search(r'repeat\(\s*(\d+)\s*,', lowered)
    if repeat:
        return int(repeat.group(1))
    return len([t for t in _split_top_level(lowered, ' ') if t.strip()])


def _unitless(value):
    """A bare number such as a line-height, or a length in px."""
    if not value:
        return None
    match = re.fullmatch(r'(\d*\.?\d+)', value.strip())
    return float(match.group(1)) if match else _to_px(value)


def _scale_factor(value):
    """The largest scale() factor in a transform value; 1.0 when absent."""
    if not value:
        return 1.0
    factors = [
        float(n)
        for args in re.findall(r'scale[xy3d]*\s*\(([^)]*)\)', value, re.I)
        for n in re.findall(r'-?\d*\.?\d+', args)
    ]
    return max(factors) if factors else 1.0


# --------------------------------------------------------------------------
# The objectives
#
# The challenge is CSS only: the markup is read-only and never submitted, so
# every objective below asks a question about the stylesheet. `style.css`
# ships 37 deliberate defects and these 14 are the graded ones. Each check
# asks about an outcome, so any equivalent repair passes.
# --------------------------------------------------------------------------

def _check_line_height(dom, rules):
    return (_unitless(declared(rules, 'body', 'line-height')) or 0) >= 1.3


def _check_navbar_row(dom, rules):
    value = (declared(rules, '.navbar', 'display') or '').lower()
    return value in ('flex', 'inline-flex', 'grid', 'inline-grid')


def _check_nav_spacing(dom, rules):
    gap = _smallest_gap(rules, '.navbar__menu')
    aligned = (declared(rules, '.navbar__menu', 'justify-content') or '').lower()
    return gap is not None and gap >= 16 and aligned == 'center'


def _check_hero_split(dom, rules):
    columns = _count_columns(declared(rules, '.hero__container', 'grid-template-columns'))
    return columns in (2, AUTO_COLUMNS)


def _check_hero_title(dom, rules):
    size = _to_px(declared(rules, '.hero__title', 'font-size'))
    return size is not None and size >= 32


def _check_hero_gap(dom, rules):
    gap = _smallest_gap(rules, '.hero__actions')
    return gap is not None and 0 < gap <= 48


def _check_console_upright(dom, rules):
    return _max_rotation(declared(rules, '.console', 'transform', exact=True)) <= 3


def _check_stats_band(dom, rules):
    columns = _count_columns(declared(rules, '.stats__grid', 'grid-template-columns'))
    aligned = (declared(rules, '.stat-card', 'text-align') or '').lower()
    return columns in (4, AUTO_COLUMNS) and aligned == 'center'


def _check_features_grid(dom, rules):
    columns = _count_columns(declared(rules, '.features__grid', 'grid-template-columns'))
    return columns in (3, AUTO_COLUMNS)


def _check_feature_box(dom, rules):
    padding = _padding_top(rules, '.feature-card')
    radius = (declared(rules, '.feature-card', 'border-radius') or '0').strip().lower()
    rounded = radius not in ('0', '0px', '0%', 'none')
    return padding is not None and padding >= 16 and rounded


def _check_feature_icon(dom, rules):
    width = _to_px(declared(rules, '.feature-card__icon', 'width'))
    height = _to_px(declared(rules, '.feature-card__icon', 'height'))
    if width is None or height is None:
        return False
    # A square-ish tile, not a stretched bar.
    return width <= height * 1.5


def _check_steps(dom, rules):
    columns = _count_columns(declared(rules, '.steps', 'grid-template-columns'))
    padding = _padding_top(rules, '.step')
    return columns in (3, AUTO_COLUMNS) and padding is not None and padding >= 16


def _check_pricing(dom, rules):
    gap = _smallest_gap(rules, '.pricing__grid')
    scale = _scale_factor(declared(rules, '.pricing-card--featured', 'transform', exact=True))
    return gap is not None and gap >= 12 and scale >= 1


def _check_responsive(dom, rules):
    # The 860px block must be a max-width query or the phone layout never runs.
    return _count_columns(
        declared(rules, '.hero__container', 'grid-template-columns', media='narrow')
    ) == 1


# id, group, title, description, (hint 1, hint 2, hint 3), function
#
# Hints narrow the search in three steps, so a player can stop at any level:
#   1  the idea            -- what kind of CSS this is, no selector, no property
#   2  where to look       -- the rule(s) involved, so nobody reads 1278 lines
#   3  which property      -- the property and which way it is wrong, not the
#                             finished declaration
#
# Two objectives are overridden by the misfiring 860px breakpoint while it is
# still broken (measured: the hero keeps a single 1072px track, and the menu is
# opacity:0 / position:fixed with a 4px gap). Their hints say so, without
# giving away how to fix the breakpoint.
_DEFINITIONS = [
    ('css-line-height', 'css', 'Body text has readable line spacing',
     'Paragraph lines should not be touching each other.',
     ('Every paragraph is set solid — the lines of text sit right on top of one '
      'another. This is about the spacing *within* a block of text, not the '
      'spacing between elements.',
      'It affects text everywhere, so it comes from the `body` rule at the top '
      'of the stylesheet.',
      'Check `line-height` on `body`. A value of `1` gives each line exactly the '
      'height of the font and nothing more; comfortable body copy needs around '
      'one and a half times that.'),
     _check_line_height),

    ('css-navbar-row', 'css', 'The header lays out in a row',
     'The logo, the menu and the buttons should sit on one line across the top.',
     ('The logo, the navigation and the action buttons should share one '
      'horizontal line. Right now each one starts on a new line — which is '
      'simply what block-level elements do.',
      'Look at the `.navbar` rule, in the navbar section of the stylesheet.',
      'That rule already sets `justify-content`, `align-items` and `gap`, and '
      'none of those do anything unless the container uses a layout mode built '
      'for arranging children in a row. Check its `display` property.'),
     _check_navbar_row),

    ('css-nav-spacing', 'css', 'Nav links are spaced and centered',
     'The menu links belong in the middle of the header with room between them.',
     ('The menu links need comfortable, even spacing, and the group should sit '
      'in the middle of the header rather than being pushed to one side.',
      'Look at `.navbar__menu`.',
      'Check `gap` and `justify-content`. One sets the distance between the '
      'links, the other decides where the group sits along the row. '
      'Note: while the phone styles are still being applied to desktop, this '
      'menu is hidden, so your change may not show up on screen yet — the '
      'responsive objective covers that.'),
     _check_nav_spacing),

    ('css-hero-split', 'css', 'The hero is a two-column layout',
     'The hero copy and the deploy console should sit side by side on desktop.',
     ('The hero text and the deployment console are meant to sit next to each '
      'other on a desktop screen, instead of stacking one above the other.',
      'Look at `.hero__container` in the hero section.',
      'It is already a grid, so check `grid-template-columns`. The declaration '
      'defines a single track where the desktop design needs two. '
      'Note: the phone breakpoint sets this same property, and while that '
      'breakpoint is misfiring it will win over your change — if nothing moves, '
      'the responsive objective is why.'),
     _check_hero_split),

    ('css-hero-title', 'css', 'The hero headline is headline-sized',
     'The main headline should be the largest text on the page.',
     ('The main headline should obviously be the most prominent text in the '
      'hero. At the moment it is no bigger than the paragraph below it.',
      'Look at `.hero__title`.',
      'Check its `font-size`. `1rem` is the browser\'s ordinary body-text size — '
      'a hero headline wants to be several times that. The other large headings '
      'in this stylesheet use `clamp()` if you want to match the house style.'),
     _check_hero_title),

    ('css-hero-gap', 'css', 'The hero buttons sit together',
     'The two call-to-action buttons should be next to each other, not far apart.',
     ('The two hero buttons should read as one pair of actions. Right now they '
      'are marooned at opposite ends of the row.',
      'Look at `.hero__actions`.',
      'Check the `gap`. It is the property that sets the space between a flex '
      'container\'s children, and this value is far larger than the spacing used '
      'anywhere else in the design.'),
     _check_hero_gap),

    ('css-console', 'css', 'The deploy console sits straight',
     'The dark console panel in the hero should be almost level.',
     ('The dark deployment console should sit very nearly level. The design does '
      'tilt it, but only just enough to notice.',
      'Look at the `.console` rule — the panel itself. Careful if you search: '
      '`rotate(45deg)` appears three times in this stylesheet. The other two '
      'turn the FAQ\'s + into a x and the menu button into a cross, and both '
      'of those are correct. Only the one on `.console` is wrong.',
      'Check the `transform` on `.console`. The angle inside its `rotate()` is '
      'doing all the damage; the design only wants a degree or so of tilt.'),
     _check_console_upright),

    ('css-stats-band', 'css', 'The statistics band is a centered 4-column row',
     'The four headline figures should sit in one centered row.',
     ('There are four statistics. They should sit in a single balanced row, with '
      'each figure centred inside its own card — instead of wrapping into a '
      'block with everything shoved against the left edge.',
      'Two rules are involved: `.stats__grid` for the row itself, and '
      '`.stat-card` for the text inside each card.',
      'On `.stats__grid`, check `grid-template-columns` — the number of tracks '
      'should match the number of statistics. On `.stat-card`, check '
      '`text-align`.'),
     _check_stats_band),

    ('css-features', 'css', 'Feature cards form a 3-column grid',
     'The six feature cards should sit three across on a desktop screen.',
     ('The six feature cards should spread across the page in rows, rather than '
      'running down it in one long single-file column.',
      'Look at `.features__grid`.',
      'Check `grid-template-columns`. The declaration currently creates only one '
      'column; this design puts these cards three across.'),
     _check_features_grid),

    ('css-feature-box', 'css', 'Feature cards look like cards',
     'Each feature card needs inner spacing and rounded corners.',
     ('The feature cards need breathing room inside them and softened corners. '
      'At the moment the text is pressed right up against a hard square border.',
      'Look at `.feature-card`.',
      'Check its `padding` and its `border-radius`: one sets the space between '
      'the card\'s border and its contents, the other the corner shape. The '
      'stylesheet defines `--radius-*` tokens near the top if you want the '
      'design\'s own value.'),
     _check_feature_box),

    ('css-feature-icon', 'css', 'Feature icons are square tiles',
     'Each feature icon should sit in a small square, not a stretched bar.',
     ('The small coloured icon tiles should be compact squares. Right now they '
      'are stretched into wide bars across the top of every feature card.',
      'Look at `.feature-card__icon`.',
      'Compare its `width` with its `height`. The tile is meant to be as wide as '
      'it is tall, and one of the two is dramatically larger than the other.'),
     _check_feature_icon),

    ('css-steps', 'css', 'The three steps sit in a padded 3-column row',
     'The workflow section has three steps and they need room to breathe.',
     ('There are three workflow steps. They should fill the row evenly, and each '
      'card needs space inside it — right now they are squeezed into part of the '
      'row with their text jammed against the edges.',
      'Two rules are involved: `.steps` for the row, and `.step` for the '
      'individual cards.',
      'On `.steps`, check `grid-template-columns` — count the steps and compare '
      'that with the number of tracks. On `.step`, check `padding`; the other '
      'cards in this design use roughly 30px.'),
     _check_steps),

    ('css-pricing', 'css', 'Pricing cards are spaced and the featured one stands out',
     'The three plans need space between them, and "Growth" should be the biggest.',
     ('The three plans should be clearly separated from each other, and the '
      'highlighted "Most popular" plan is supposed to draw the eye by being '
      'slightly larger than its neighbours — not smaller.',
      'Two rules are involved: `.pricing__grid` for the row, and '
      '`.pricing-card--featured` for the highlighted card.',
      'On `.pricing__grid`, check the `gap`. On `.pricing-card--featured`, check '
      'the `transform`: a `scale()` below 1 shrinks an element, and above 1 '
      'enlarges it.'),
     _check_pricing),

    ('css-responsive', 'css', 'Mobile styles only apply to mobile',
     'The phone layout should take over on small screens, not on large ones.',
     ('The phone layout is being applied to desktop screens. Look at the preview: '
      'the nav links have vanished and a hamburger button has appeared in their '
      'place — that is the mobile menu, on a full-size screen.',
      'One `@media` block near the bottom of the stylesheet holds the entire '
      'phone layout. Look at the condition on the block itself, not at the rules '
      'inside it.',
      'Check whether that query is triggered by a `min-width` or a `max-width` '
      'condition, then work out which of the two switches styles on when the '
      'screen gets *narrower* than the breakpoint.'),
     _check_responsive),
]

TOTAL_CHECKS = len(_DEFINITIONS)
CSS_CHECKS = sum(1 for d in _DEFINITIONS if d[1] == 'css')
HTML_CHECKS = sum(1 for d in _DEFINITIONS if d[1] == 'html')


def objectives():
    """The objective list without any grading -- for the home page."""
    return [
        {'id': cid, 'group': group, 'title': title, 'description': description}
        for cid, group, title, description, _hints, _fn in _DEFINITIONS
    ]


def run_checks(html, css):
    """Return one result dict per objective. Never raises on bad input.

    `html` is the fixed challenge markup, supplied by the server -- it is not
    editable, but the checks receive it so a future round can grade markup
    without changing this signature.
    """
    dom = parse_html(html)
    rules = parse_css(css)

    results = []
    for check_id, group, title, description, hints, function in _DEFINITIONS:
        try:
            passed = bool(function(dom, rules))
        except Exception:  # a malformed submission must not 500 the game
            passed = False
        results.append({
            'id': check_id,
            'group': group,
            'title': title,
            'description': description,
            'hints': list(hints),
            'passed': passed,
        })
    return results


# --------------------------------------------------------------------------
# Lookups for the hint API
# --------------------------------------------------------------------------

HINT_LEVELS = 3

OBJECTIVE_TITLES = {cid: title for cid, _g, title, _d, _h, _f in _DEFINITIONS}
OBJECTIVE_IDS = tuple(OBJECTIVE_TITLES)

_HINTS = {cid: hints for cid, _g, _t, _d, hints, _f in _DEFINITIONS}


def hint_text(objective, level):
    """The text of one hint, or None if the objective/level is not real.

    Levels are 1-based. Anything the client sends is validated here, so a
    forged objective id or level cannot reach the database.
    """
    hints = _HINTS.get(objective)
    if not hints or not isinstance(level, int) or not 1 <= level <= len(hints):
        return None
    return hints[level - 1]

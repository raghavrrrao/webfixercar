"""
Regression tests for the challenge files, the live player count and the
deploy-time admin account.

The participant no longer edits CSS — the race in `test_race.py` is the game
now — but both NovaCloud stylesheets are still served: `style.css` is the
broken page shown in the briefing and `solution.css` is the fixed page
unlocked by crossing the finish line. So the checks that keep those two files
honest, and the hint text that documents them for the organisers, still earn
their place here.
"""

import itertools
import re
from io import StringIO

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from games.asgi import application as presence_application

from . import presence as first_presence
from .checks import CSS_CHECKS, HTML_CHECKS, TOTAL_CHECKS, run_checks
from .game_config import (
    CHALLENGE_HTML,
    GRADED_FIXES,
    SOLUTION_CSS,
    STARTER_CSS,
    apply_fixes,
)
from .models import User
from .presence import HEARTBEAT_INTERVAL_SECONDS, get_presence_store
from .repairs import (
    REPAIR_CARDS,
    REPAIR_IDS,
    REPAIRS,
    clean_repair_ids,
    is_fully_repaired,
    repair_css,
)


def graded_solution():
    """The minimum passing stylesheet: only the 14 graded objectives fixed."""
    return apply_fixes(STARTER_CSS, GRADED_FIXES)


def results_by_id(css, html=None):
    return {r['id']: r['passed'] for r in run_checks(html or CHALLENGE_HTML, css)}


class ChallengeSourceTests(TestCase):
    """The challenge files must stay consistent with the fix table."""

    def test_every_graded_fix_anchors_uniquely(self):
        # apply_fixes raises when an anchor is missing or ambiguous, so this
        # fails loudly the moment a challenge file drifts from the table.
        apply_fixes(STARTER_CSS, GRADED_FIXES)

    def test_the_round_is_css_only(self):
        self.assertEqual(HTML_CHECKS, 0)
        self.assertEqual(CSS_CHECKS, TOTAL_CHECKS)
        self.assertEqual(len(GRADED_FIXES), TOTAL_CHECKS)
        self.assertEqual({d['id'] for d in run_checks('', '')}, set(GRADED_FIXES))

    def test_the_round_stays_beginner_sized(self):
        # 12-16 objectives. A wider brief would stop being a 30 minute
        # beginner round, so lock the shape in.
        self.assertTrue(12 <= TOTAL_CHECKS <= 16, TOTAL_CHECKS)

    def test_every_objective_ships_three_hints(self):
        for check in run_checks(CHALLENGE_HTML, STARTER_CSS):
            self.assertEqual(len(check['hints']), 3, check['id'])
            self.assertTrue(check['description'].strip(), check['id'])

    def test_the_markup_is_the_finished_novacloud_page(self):
        # The player repairs a stylesheet; the page itself is already correct.
        self.assertIn('<!DOCTYPE html>', CHALLENGE_HTML)
        self.assertEqual(CHALLENGE_HTML.count('<h1'), 1)
        for section in ('id="features"', 'id="pricing"', 'id="faq"',
                        'id="testimonials"', 'id="how-it-works"', 'site-footer'):
            self.assertIn(section, CHALLENGE_HTML, section)
        # the five nav links all resolve to real sections
        self.assertEqual(CHALLENGE_HTML.count('class="navbar__link"'), 5)
        self.assertGreater(len(CHALLENGE_HTML), 20000)

    def test_the_page_needs_no_javascript(self):
        # The preview sandbox blocks scripts, so nothing may depend on them.
        self.assertNotIn('<script', CHALLENGE_HTML)
        self.assertNotIn('class="reveal"', CHALLENGE_HTML)
        # the statistics read their real values rather than a JS placeholder
        for number in ('>12,000<', '>99<', '>14<', '>6<'):
            self.assertIn(number, CHALLENGE_HTML, number)
        # the FAQ answers are readable without an accordion script
        self.assertNotIn('.faq-item__answer {\n  max-height: 0;', SOLUTION_CSS)
        self.assertNotIn('.faq-item__answer {\n  max-height: 0;', STARTER_CSS)
        # ...and the theme toggle shows one icon, not both
        self.assertIn('.theme-toggle__icon--moon {\n  display: none;\n}', STARTER_CSS)

    def test_the_stylesheet_carries_more_noise_than_objectives(self):
        # A deliberate design decision: 37 defects ship, 14 are scored.
        differing = sum(1 for a, b in zip(SOLUTION_CSS.splitlines(),
                                          STARTER_CSS.splitlines()) if a != b)
        self.assertGreater(differing, TOTAL_CHECKS)

    def test_the_hero_glow_stays_out_of_normal_flow(self):
        # `position: static` on the 900x900 glow opens a 900px void above the
        # hero and hides four objectives. It must never come back.
        self.assertIn('.hero__glow {\n  position: absolute;', STARTER_CSS)


class ChallengeCheckTests(TestCase):
    def test_shipped_stylesheet_fails_every_objective(self):
        results = run_checks(CHALLENGE_HTML, STARTER_CSS)
        self.assertEqual(len(results), TOTAL_CHECKS)
        self.assertEqual([r['id'] for r in results if r['passed']], [])

    def test_gold_standard_passes_every_objective(self):
        failed = [r['id'] for r in run_checks(CHALLENGE_HTML, SOLUTION_CSS) if not r['passed']]
        self.assertEqual(failed, [])

    def test_fixing_only_the_graded_objectives_is_enough_to_win(self):
        css = graded_solution()
        failed = [r['id'] for r in run_checks(CHALLENGE_HTML, css) if not r['passed']]
        self.assertEqual(failed, [])
        self.assertNotEqual(css, SOLUTION_CSS)  # deliberately not pristine

    def test_each_fix_clears_exactly_its_own_objective(self):
        """Fixing one thing must not accidentally tick a different box."""
        for objective, pairs in GRADED_FIXES.items():
            css = apply_fixes(STARTER_CSS, pairs)
            passed = [k for k, v in results_by_id(css).items() if v]
            self.assertEqual(passed, [objective])

    def test_grouped_objectives_need_all_their_parts(self):
        """A half-finished grouped fix must not score."""
        for objective in ('css-stats-band', 'css-steps', 'css-pricing'):
            pairs = GRADED_FIXES[objective]
            self.assertEqual(len(pairs), 2, objective)
            for half in pairs:
                css = apply_fixes(STARTER_CSS, (half,))
                self.assertFalse(results_by_id(css)[objective], f'{objective} half {half[0][:30]}')

    def test_alternative_but_valid_answers_are_accepted(self):
        css = graded_solution()

        auto_fit = css.replace('.features__grid {\n  display: grid;\n'
                               '  grid-template-columns: repeat(3, 1fr);',
                               '.features__grid {\n  display: grid;\n'
                               '  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));')
        self.assertTrue(results_by_id(auto_fit)['css-features'])

        three_tracks = css.replace('.features__grid {\n  display: grid;\n'
                                   '  grid-template-columns: repeat(3, 1fr);',
                                   '.features__grid {\n  display: grid;\n'
                                   '  grid-template-columns: 1fr 1fr 1fr;')
        self.assertTrue(results_by_id(three_tracks)['css-features'])

        # a plain size instead of the design system's clamp()
        plain = css.replace('font-size: clamp(2.4rem, 4.4vw, 3.6rem);', 'font-size: 56px;')
        self.assertTrue(results_by_id(plain)['css-hero-title'])

        # inline-flex is as good as flex for the header bar
        inline = css.replace('.navbar {\n  display: flex;', '.navbar {\n  display: inline-flex;')
        self.assertTrue(results_by_id(inline)['css-navbar-row'])

        # rem units, and a literal radius instead of the token
        rem_gap = css.replace('  gap: 32px;\n  flex: 1;', '  gap: 2rem;\n  flex: 1;')
        self.assertTrue(results_by_id(rem_gap)['css-nav-spacing'])

        literal = css.replace('  border-radius: var(--radius-md);\n  padding: 32px;',
                              '  border-radius: 16px;\n  padding: 2rem;')
        self.assertTrue(results_by_id(literal)['css-feature-box'])

        # deleting the tilt is as good as reducing it
        for value in ('rotate(0deg)', 'none'):
            upright = STARTER_CSS.replace('  overflow: hidden;\n  transform: rotate(45deg);',
                                          '  overflow: hidden;\n  transform: ' + value + ';')
            self.assertTrue(results_by_id(upright)['css-console'], value)

        # dropping the featured card's transform entirely is a valid answer
        no_scale = css.replace('  border-color: transparent;\n  transform: scale(1.04);',
                               '  border-color: transparent;')
        self.assertTrue(results_by_id(no_scale)['css-pricing'])

        # a square icon at any size, not just 48px
        bigger_icon = css.replace('  width: 48px;\n  height: 48px;\n'
                                  '  border-radius: var(--radius-sm);',
                                  '  width: 56px;\n  height: 56px;\n'
                                  '  border-radius: var(--radius-sm);')
        self.assertTrue(results_by_id(bigger_icon)['css-feature-icon'])

    def test_formatting_noise_does_not_affect_grading(self):
        css = graded_solution().replace(
            '.navbar {\n  display: flex;\n  align-items: center;',
            '.navbar{align-items:center;display:flex;  /* tidied */')
        self.assertTrue(results_by_id(css)['css-navbar-row'])

    def test_the_faq_icon_rotation_is_not_mistaken_for_the_console(self):
        # `.faq-item--open .faq-item__icon` legitimately uses rotate(45deg).
        self.assertIn('.faq-item--open .faq-item__icon', STARTER_CSS)
        self.assertTrue(results_by_id(graded_solution())['css-console'])
        self.assertFalse(results_by_id(STARTER_CSS)['css-console'])

    def test_hover_rules_do_not_satisfy_base_selectors(self):
        css = STARTER_CSS.replace('.navbar__link:hover {\n  color: var(--color-text);',
                                  '.navbar__link:hover {\n  color: var(--color-text);\n'
                                  '  display: flex;')
        self.assertFalse(results_by_id(css)['css-navbar-row'])

    def test_media_query_answers_are_scoped_to_the_media_query(self):
        css = graded_solution()
        stripped = css.replace('@media (max-width: 860px) {', '@media (min-width: 861px) {')
        self.assertFalse(results_by_id(stripped)['css-responsive'])
        self.assertTrue(results_by_id(stripped)['css-hero-split'])

    def test_malformed_submission_does_not_raise(self):
        results = run_checks(CHALLENGE_HTML, 'body { color: ; } @media { .x {')
        self.assertEqual(len(results), TOTAL_CHECKS)

    def test_hostile_css_is_graded_without_crashing(self):
        hostile = '* { all: unset !important; } body { background: red !important; }'
        results = results_by_id(hostile)
        self.assertEqual(len(results), TOTAL_CHECKS)
        # Deleting the stylesheet does not solve the round. (`css-console`
        # legitimately passes: with no transform declared, nothing is rotated.)
        for objective in ('css-line-height', 'css-navbar-row', 'css-nav-spacing',
                          'css-hero-split', 'css-hero-title', 'css-hero-gap',
                          'css-stats-band', 'css-features', 'css-feature-box',
                          'css-feature-icon', 'css-steps', 'css-pricing',
                          'css-responsive'):
            self.assertFalse(results[objective], objective)

class HintQualityTests(TestCase):
    """Hints must narrow the search in three steps, never paste the answer.

    Hint 1 = the idea, hint 2 = where to look, hint 3 = which property.
    """

    SELECTOR = re.compile(r'`[.#][\w_-]+`')
    PROPERTY = re.compile(r'`[a-z-]+`')

    @staticmethod
    def finished_declarations():
        """Every declaration the player has to end up with, as literal text."""
        answers = []
        for pairs in GRADED_FIXES.values():
            for broken, fixed in pairs:
                already = {l.strip().rstrip(';') for l in broken.splitlines() if ':' in l}
                for line in fixed.splitlines():
                    declaration = line.strip().rstrip(';')
                    if ':' in declaration and declaration not in already:
                        answers.append(declaration)
                if fixed.strip().startswith('@media'):
                    answers.append(fixed.strip().rstrip(' {'))
        return answers

    def objectives(self):
        return [(c['id'], c['description'], c['hints'])
                for c in run_checks(CHALLENGE_HTML, STARTER_CSS)]

    def test_every_objective_has_exactly_three_hints(self):
        for objective, _description, hints in self.objectives():
            self.assertEqual(len(hints), 3, objective)
            for level, hint in enumerate(hints, start=1):
                self.assertTrue(hint.strip(), f'{objective} hint {level} is empty')

    def test_hint_one_teaches_the_idea_without_naming_the_code(self):
        for objective, _description, hints in self.objectives():
            self.assertNotRegex(hints[0], r'`[.#@][\w-]+', f'{objective}: hint 1 names a selector')

    def test_hint_two_says_where_to_look(self):
        for objective, _description, hints in self.objectives():
            located = (self.SELECTOR.search(hints[1])
                       or '@media' in hints[1]
                       or '`body`' in hints[1])
            self.assertTrue(located, f'{objective}: hint 2 points at no rule')

    def test_hint_three_names_the_property(self):
        for objective, _description, hints in self.objectives():
            self.assertRegex(hints[2], self.PROPERTY.pattern,
                             f'{objective}: hint 3 names no property')

    def test_no_hint_ever_pastes_a_finished_declaration(self):
        answers = self.finished_declarations()
        self.assertGreaterEqual(len(answers), TOTAL_CHECKS)
        for objective, description, hints in self.objectives():
            text = ' '.join(hints) + ' ' + description
            for answer in answers:
                self.assertNotIn(answer, text, f'{objective} gives away {answer!r}')

    def test_hints_stay_short_enough_to_read_at_a_glance(self):
        for objective, _description, hints in self.objectives():
            for level, hint in enumerate(hints, start=1):
                self.assertLessEqual(len(hint), 420, f'{objective} hint {level} is a paragraph')

    def test_the_two_masked_objectives_warn_the_player(self):
        """Fixing these changes nothing on screen until the breakpoint is fixed.

        Measured at 1120px: `.hero__container` keeps a single 1072px track and
        `.navbar__menu` stays opacity:0 / position:fixed, both because the
        misfiring 860px block still applies. Without a note the player thinks
        their correct edit failed.
        """
        hints = {objective: h for objective, _d, h in self.objectives()}
        for objective in ('css-hero-split', 'css-nav-spacing'):
            self.assertIn('responsive objective', ' '.join(hints[objective]).lower(),
                          f'{objective} needs the interaction note')
        # ...and the note must not leak the breakpoint's own answer
        for objective in ('css-hero-split', 'css-nav-spacing'):
            self.assertNotIn('max-width', ' '.join(hints[objective]))

    def test_the_console_hint_disambiguates_every_other_rotation(self):
        """`rotate(45deg)` appears three times; only `.console` is wrong.

        The other two are correct: the FAQ's open icon and the mobile menu
        button's cross. A player who searches the file must be told, or they
        will "fix" a rule that was never broken.
        """
        decoys = STARTER_CSS.count('rotate(45deg)')
        self.assertEqual(decoys, 3)
        hints = ' '.join({o: h for o, _d, h in self.objectives()}['css-console'])
        self.assertIn('.console', hints)
        self.assertIn('three times', hints, 'the hint must state how many there are')
        self.assertIn('FAQ', hints)

    def test_no_hint_asks_for_html_or_javascript(self):
        for objective, description, hints in self.objectives():
            text = (' '.join(hints) + ' ' + description).lower()
            for forbidden in ('javascript', 'index.html', '<div', '<span', 'markup'):
                self.assertNotIn(forbidden, text, f'{objective} mentions {forbidden}')


class RepairLayerTests(TestCase):
    """The seven repairs must be a real partition of the real diff.

    This is the test that stops the repair mechanic from being a mock-up. If
    the layers ever stop composing into `solution.css` — because somebody
    edited a stylesheet, or dropped a rule from a slice, or put one rule in two
    slices — the game would be handing out repairs that do not repair
    anything, and these fail.
    """

    def test_the_broken_stylesheet_is_what_a_participant_starts_with(self):
        self.assertEqual(repair_css([]), STARTER_CSS)
        self.assertFalse(is_fully_repaired(STARTER_CSS))

    def test_the_full_set_composes_into_the_finished_stylesheet(self):
        self.assertTrue(is_fully_repaired(repair_css(REPAIR_IDS)))

    def test_the_full_set_passes_every_graded_objective(self):
        failed = [check['id'] for check in
                  run_checks(CHALLENGE_HTML, repair_css(REPAIR_IDS))
                  if not check['passed']]
        self.assertEqual(failed, [])

    def test_every_collection_state_composes_cleanly(self):
        """All 128 of them: repairs may be missed and picked up later."""
        for size in range(len(REPAIR_IDS) + 1):
            for combination in itertools.combinations(REPAIR_IDS, size):
                repair_css(combination)      # raises if an anchor has drifted

    def test_the_website_improves_with_every_repair_in_course_order(self):
        previous = -1
        for count in range(len(REPAIR_IDS) + 1):
            css = repair_css(REPAIR_IDS[:count])
            passed = sum(1 for check in run_checks(CHALLENGE_HTML, css)
                         if check['passed'])
            self.assertGreaterEqual(passed, previous, f'{count} repairs went backwards')
            previous = passed
        self.assertEqual(previous, TOTAL_CHECKS)

    def test_every_repair_changes_the_stylesheet(self):
        for index, repair_id in enumerate(REPAIR_IDS):
            before = repair_css(REPAIR_IDS[:index])
            after = repair_css(REPAIR_IDS[:index + 1])
            self.assertNotEqual(before, after, repair_id)

    def test_no_rule_belongs_to_two_repairs(self):
        anchors = [broken for repair in REPAIRS for broken, _fixed in repair['fixes']]
        self.assertEqual(len(anchors), len(set(anchors)))

    def test_the_repairs_cover_the_whole_difference(self):
        """Nothing in the diff is left for nobody to fix."""
        broken_lines = STARTER_CSS.splitlines()
        fixed_lines = SOLUTION_CSS.splitlines()
        composed = repair_css(REPAIR_IDS).splitlines()
        self.assertEqual(len(broken_lines), len(fixed_lines))

        differing = [n for n, (a, b) in enumerate(zip(broken_lines, fixed_lines)) if a != b]
        self.assertGreater(len(differing), 30, 'the challenge lost its defects')
        for line in differing:
            # the banner comment is documentation, not a repair
            if line < 6:
                continue
            self.assertEqual(composed[line], fixed_lines[line], f'line {line + 1}')

    def test_each_repair_is_described_for_the_participant(self):
        self.assertEqual(len(REPAIR_CARDS), len(REPAIRS))
        for card in REPAIR_CARDS:
            self.assertTrue(card['label'].isupper(), card['label'])
            self.assertTrue(card['section'].strip())
            self.assertTrue(card['message'].strip())
            self.assertLessEqual(len(card['message']), 60, card['id'])
            self.assertNotIn('{', card['blurb'], 'a card must not leak the answer')

    def test_the_cards_never_carry_the_css(self):
        """The page is told what it collected, never how the fix is written."""
        text = ' '.join(
            str(value) for card in REPAIR_CARDS for value in card.values())
        for answer in ('repeat(3, 1fr)', 'max-width: 860px', 'display: flex',
                       'clamp(2.4rem', 'scale(1.04)'):
            self.assertNotIn(answer, text, answer)

    def test_a_stored_repair_list_is_read_back_safely(self):
        self.assertEqual(clean_repair_ids(''), [])
        self.assertEqual(clean_repair_ids(None), [])
        self.assertEqual(clean_repair_ids('margin,padding'), ['margin', 'padding'])
        self.assertEqual(clean_repair_ids('margin,margin'), ['margin'])
        self.assertEqual(clean_repair_ids('margin,nope,<script>'), ['margin'])
        self.assertEqual(clean_repair_ids(' margin , grid '), ['margin', 'grid'])


class PresenceTests(TransactionTestCase):
    """The live player count is per browser session, not per socket.

    Sessions are created up front: they are database writes, and the async
    bodies below cannot touch the ORM directly. Frames are read by exact
    count rather than "until it goes quiet" -- letting a read time out makes
    asgiref cancel the consumer under us.
    """

    # Frames a socket receives about itself when it connects: welcome + count.
    OWN_CONNECT_FRAMES = 2

    def setUp(self):
        first_presence._store = None  # a fresh store per test

    def tearDown(self):
        first_presence._store = None

    @staticmethod
    def new_session():
        session = SessionStore()
        session.create()
        return session.session_key

    @staticmethod
    def socket(session_key):
        """A presence socket carrying `session_key` the way a browser would."""
        cookie = f'{settings.SESSION_COOKIE_NAME}={session_key}'.encode()
        return WebsocketCommunicator(
            presence_application, '/ws/presence/', headers=[(b'cookie', cookie)],
        )

    @classmethod
    async def read(cls, communicator, frames):
        """Read exactly `frames` messages; return the last count seen."""
        latest = None
        for _ in range(frames):
            message = await communicator.receive_json_from(timeout=3)
            if 'count' in message:
                latest = message['count']
        return latest

    @classmethod
    async def join(cls, communicator):
        connected, _ = await communicator.connect()
        assert connected
        return await cls.read(communicator, cls.OWN_CONNECT_FRAMES)

    def test_connect_receives_a_welcome_and_a_count(self):
        session = self.new_session()

        async def run():
            socket = self.socket(session)
            connected, _ = await socket.connect()
            self.assertTrue(connected)

            welcome = await socket.receive_json_from(timeout=3)
            self.assertEqual(welcome['type'], 'welcome')
            self.assertEqual(welcome['heartbeat'], HEARTBEAT_INTERVAL_SECONDS)

            broadcast = await socket.receive_json_from(timeout=3)
            self.assertEqual(broadcast['count'], 1)
            await socket.disconnect()

        async_to_sync(run)()

    def test_tabs_of_one_session_count_as_one_player(self):
        mine, stranger_key, returning_key = (
            self.new_session(), self.new_session(), self.new_session())

        async def run():
            store = get_presence_store()

            tab_one = self.socket(mine)
            self.assertEqual(await self.join(tab_one), 1)

            tab_two = self.socket(mine)
            self.assertEqual(await self.join(tab_two), 1)
            self.assertEqual(await store.count(), 1, 'a second tab is not a second player')

            stranger = self.socket(stranger_key)
            self.assertEqual(await self.join(stranger), 2)

            # closing one tab of a two-tab session leaves that player online
            await tab_two.disconnect()
            self.assertEqual(await store.count(), 2)

            await stranger.disconnect()
            self.assertEqual(await store.count(), 1)

            # ...and a visitor can come back
            returning = self.socket(returning_key)
            self.assertEqual(await self.join(returning), 2)

            await tab_one.disconnect()
            await returning.disconnect()
            self.assertEqual(await store.count(), 0)

        async_to_sync(run)()

    def test_everyone_is_told_when_the_count_changes(self):
        watcher_key, other_key = self.new_session(), self.new_session()

        async def run():
            watcher = self.socket(watcher_key)
            self.assertEqual(await self.join(watcher), 1)

            other = self.socket(other_key)
            await other.connect()
            # the watcher is told about the new player
            self.assertEqual(await self.read(watcher, 1), 2)

            await other.disconnect()
            self.assertEqual(await self.read(watcher, 1), 1)
            await watcher.disconnect()

        async_to_sync(run)()

    def test_a_ping_answers_with_the_current_count(self):
        session = self.new_session()

        async def run():
            socket = self.socket(session)
            await self.join(socket)

            await socket.send_json_to({'type': 'ping'})
            pong = await socket.receive_json_from(timeout=3)
            self.assertEqual(pong['type'], 'pong')
            self.assertEqual(pong['count'], 1)
            await socket.disconnect()

        async_to_sync(run)()


class HintMarkupTests(TestCase):
    """Backtick spans in a hint render as <code>, and only as <code>."""

    def test_backticks_become_code_elements(self):
        from .templatetags.wf_hints import code_spans
        self.assertEqual(code_spans('Check `display` on `.navbar`.'),
                         'Check <code>display</code> on <code>.navbar</code>.')

    def test_the_filter_escapes_before_it_marks_anything_safe(self):
        from .templatetags.wf_hints import code_spans
        rendered = code_spans('<script>alert(1)</script> and `<b>x</b>`')
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('<code>&lt;b&gt;x&lt;/b&gt;</code>', rendered)


class EnsureAdminCommandTests(TestCase):
    """The deploy-time admin account: created once, safe to re-run forever."""

    USERNAME = 'admin123'
    PC_NO = 'admin123'
    PASSWORD = 'piyush123456@'

    def run_command(self):
        out = StringIO()
        call_command('ensure_admin', stdout=out)
        return out.getvalue()

    def test_it_creates_the_admin_account(self):
        self.assertFalse(User.objects.filter(username=self.USERNAME).exists())
        self.run_command()

        admin = User.objects.get(username=self.USERNAME)
        self.assertTrue(admin.is_admin)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_active)
        self.assertTrue(admin.check_password(self.PASSWORD))

    def test_running_it_again_changes_nothing(self):
        self.run_command()
        original = User.objects.get(username=self.USERNAME)

        for _ in range(3):
            output = self.run_command()
            self.assertIn('already exists', output)

        self.assertEqual(User.objects.filter(username=self.USERNAME).count(), 1)
        again = User.objects.get(username=self.USERNAME)
        self.assertEqual(again.pk, original.pk)
        self.assertEqual(again.password, original.password)

    def test_it_does_not_reset_a_password_changed_later(self):
        self.run_command()
        admin = User.objects.get(username=self.USERNAME)
        admin.set_password('something-else-entirely')
        admin.save(update_fields=['password'])

        self.run_command()

        admin.refresh_from_db()
        self.assertTrue(admin.check_password('something-else-entirely'))
        self.assertFalse(admin.check_password(self.PASSWORD))

    def test_the_login_field_is_the_participant_name_not_an_email(self):
        """The account is a person, not the machine they sat at."""
        self.run_command()
        self.assertEqual(User.USERNAME_FIELD, 'username')
        self.assertNotIn('email', [f.name for f in User._meta.get_fields()])

        # this is exactly what the admin login form posts
        signed_in = self.client.login(username=self.USERNAME, password=self.PASSWORD)
        self.assertTrue(signed_in)

    def test_the_admin_account_can_reach_the_judging_screens(self):
        self.run_command()
        self.client.login(username=self.USERNAME, password=self.PASSWORD)

        for url in (reverse('admin:index'),
                    reverse('admin:first_user_changelist'),
                    reverse('admin:first_finalsubmission_changelist')):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_it_creates_no_other_admin_accounts(self):
        self.run_command()
        self.assertEqual(
            sorted(User.objects.filter(is_admin=True).values_list('username', flat=True)),
            [self.USERNAME],
        )

    def test_it_promotes_an_existing_non_admin_with_that_name(self):
        User.objects.create_user(
            username=self.USERNAME, pc_no='PC-1', password='pw-123456')
        output = self.run_command()

        self.assertIn('Granted admin rights', output)
        self.assertTrue(User.objects.get(username=self.USERNAME).is_admin)
        self.assertEqual(User.objects.filter(username=self.USERNAME).count(), 1)

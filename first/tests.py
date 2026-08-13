"""
Regression tests for the challenge itself.

The challenge is CSS only. The markup is fixed, read-only and never accepted
the browser, so the properties that matter are: the shipped stylesheet fails
every objective, the gold standard passes every one, each graded fix clears
exactly its own box, equivalent answers are accepted, a posted `html` field
is ignored, and the clock cannot be restarted.

`style.css` carries 37 deliberate defects while only 14 are graded, so
"apply the graded fixes" produces a *passing* submission, not a pristine one.
`solution.css` remains the organisers' reference.
"""

import re
from datetime import timedelta
from io import StringIO

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.core.management import call_command
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games.asgi import application as presence_application

from . import presence as first_presence
from .checks import CSS_CHECKS, HTML_CHECKS, TOTAL_CHECKS, run_checks
from .game_config import (
    CHALLENGE_HTML,
    GAME_DURATION_SECONDS,
    GRADED_FIXES,
    SOLUTION_CSS,
    STARTER_CSS,
    apply_fixes,
)
from .models import CssRule, FinalSubmission, HintReveal, User
from .presence import HEARTBEAT_INTERVAL_SECONDS, get_presence_store
from .views import finalize_all_due, finalize_if_due


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


class ReadOnlyMarkupTests(TestCase):
    """The markup is fixed: it is never taken from the client, ever."""

    def setUp(self):
        self.user = User.objects.create_user(username='Tester', pc_no='PC-RO', password='pw-123456')
        self.client.force_login(self.user, backend='first.backends.PCNoBackend')
        self.client.get(reverse('home'))

    def test_a_posted_html_field_is_ignored_on_save(self):
        self.client.post(reverse('save_css'), {'html': '<h1>hacked</h1>', 'css': STARTER_CSS})
        served = self.client.get(reverse('get_css')).json()
        self.assertEqual(served['html'], CHALLENGE_HTML)
        self.assertNotIn('hacked', served['html'])

    def test_a_posted_html_field_cannot_change_the_score(self):
        # Every objective is CSS, so even a "perfect" forged page scores zero.
        data = self.client.post(reverse('api_check'),
                                {'html': CHALLENGE_HTML, 'css': STARTER_CSS}).json()
        self.assertEqual(data['passed'], 0)

        data = self.client.post(reverse('api_check'),
                                {'html': '<h1>hacked</h1>', 'css': graded_solution()}).json()
        self.assertEqual(data['passed'], TOTAL_CHECKS)

    def test_reset_returns_the_stylesheet_only(self):
        self.client.post(reverse('save_css'), {'css': 'body{}'})
        data = self.client.post(reverse('api_reset'), {}).json()
        self.assertEqual(data['css'], STARTER_CSS)
        self.assertNotIn('html', data)

    def test_the_arena_marks_the_html_pane_read_only(self):
        page = self.client.get(reverse('home')).content.decode()
        self.assertIn('id="wf-html"', page)
        self.assertIn('readonly', page)
        self.assertIn('aria-readonly="true"', page)
        self.assertIn('read only', page)


class ChallengeClockTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='Tester', pc_no='PC-TEST', password='pw-123456')
        self.client.force_login(self.user, backend='first.backends.PCNoBackend')

    def test_clock_starts_on_first_arena_visit_and_never_restarts(self):
        self.assertIsNone(self.user.game_start_time)

        self.client.get(reverse('home'))
        self.user.refresh_from_db()
        started = self.user.game_start_time
        self.assertIsNotNone(started)

        self.client.get(reverse('home'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.game_start_time, started)

    def test_state_endpoint_reports_the_full_duration_at_the_start(self):
        self.client.get(reverse('home'))
        data = self.client.get(reverse('api_state')).json()
        self.assertEqual(data['duration'], GAME_DURATION_SECONDS)
        self.assertGreater(data['remaining'], GAME_DURATION_SECONDS - 10)
        self.assertFalse(data['expired'])

    def test_reset_restores_the_broken_css_without_touching_the_clock(self):
        self.client.get(reverse('home'))
        self.client.post(reverse('save_css'), {'css': graded_solution()})

        self.user.refresh_from_db()
        started = self.user.game_start_time

        data = self.client.post(reverse('api_reset'), {}).json()
        self.assertEqual(data['css'], STARTER_CSS)

        self.user.refresh_from_db()
        self.assertEqual(self.user.game_start_time, started)

    def test_expired_session_refuses_saves_and_checks(self):
        self.client.get(reverse('home'))
        self.user.game_start_time = timezone.now() - timedelta(seconds=GAME_DURATION_SECONDS + 1)
        self.user.save(update_fields=['game_start_time'])

        saved = self.client.post(reverse('save_css'), {'css': 'body{}'}).json()
        self.assertEqual(saved['error'], 'Time is up!')

        checked = self.client.post(reverse('api_check'), {'css': graded_solution()}).json()
        self.assertEqual(checked['error'], 'Time is up!')
        self.assertFalse(checked['completed'])

        self.user.refresh_from_db()
        self.assertIsNone(self.user.completed_at)
        self.assertNotEqual(self.client.get(reverse('get_css')).json()['css'], 'body{}')

    def test_expired_session_also_refuses_reset(self):
        self.client.get(reverse('home'))
        self.client.post(reverse('save_css'), {'css': 'body{ /* mine */ }'})
        self.user.game_start_time = timezone.now() - timedelta(seconds=GAME_DURATION_SECONDS + 1)
        self.user.save(update_fields=['game_start_time'])

        data = self.client.post(reverse('api_reset'), {}).json()
        self.assertNotIn('css', data)
        self.assertEqual(self.client.get(reverse('get_css')).json()['css'], 'body{ /* mine */ }')

    def test_solving_the_challenge_opens_design_mode_without_ending_it(self):
        """14/14 is the gateway to phase 2, not the end of the round."""
        self.client.get(reverse('home'))
        data = self.client.post(reverse('api_check'), {'css': graded_solution()}).json()

        self.assertEqual(data['passed'], TOTAL_CHECKS)
        self.assertTrue(data['designMode'])
        self.assertTrue(data['eligible'])
        self.assertFalse(data['locked'], 'the clock still has time on it')

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.completed_at)
        self.assertEqual(self.user.best_score, TOTAL_CHECKS)

        # ...and they can keep editing
        saved = self.client.post(reverse('save_css'), {'css': 'body { color: pink; }'}).json()
        self.assertNotIn('error', saved)

    def test_partial_progress_is_recorded_as_a_best_score(self):
        self.client.get(reverse('home'))
        some = dict(list(GRADED_FIXES.items())[:3])
        data = self.client.post(reverse('api_check'), {'css': apply_fixes(STARTER_CSS, some)}).json()

        self.assertEqual(data['passed'], 3)
        self.assertFalse(data['completed'])

        self.client.post(reverse('api_check'), {'css': STARTER_CSS})
        self.user.refresh_from_db()
        self.assertEqual(self.user.best_score, 3)

    def test_autosaved_work_counts_even_without_running_the_checks(self):
        self.client.get(reverse('home'))
        some = dict(list(GRADED_FIXES.items())[:4])
        self.client.post(reverse('save_css'), {'css': apply_fixes(STARTER_CSS, some)})

        self.client.get(reverse('home'))  # grading happens on render
        self.user.refresh_from_db()
        self.assertEqual(self.user.best_score, 4)

    def test_running_out_of_time_closes_the_round_even_on_a_perfect_page(self):
        self.client.get(reverse('home'))
        self.client.post(reverse('save_css'), {'css': graded_solution()})

        self.user.game_start_time = timezone.now() - timedelta(seconds=GAME_DURATION_SECONDS + 1)
        self.user.save(update_fields=['game_start_time'])

        self.client.get(reverse('home'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.best_score, TOTAL_CHECKS)
        self.assertIsNone(self.user.completed_at, 'time ran out before they submitted')
        self.assertTrue(self.user.is_expired)

    def test_anonymous_visitors_cannot_touch_the_api(self):
        self.client.logout()
        for name in ('save_css', 'api_check', 'api_reset'):
            self.assertEqual(self.client.post(reverse(name), {}).status_code, 403, name)
        self.assertEqual(self.client.get(reverse('api_state')).status_code, 403)


class ArenaPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='Tester', pc_no='PC-UI', password='pw-123456')
        self.client.force_login(self.user, backend='first.backends.PCNoBackend')

    def test_home_page_advertises_the_round(self):
        page = self.client.get(reverse('intro')).content.decode()
        self.assertIn('NovaCloud', page)
        self.assertIn('Repair the code. Beat the clock.', page)
        self.assertIn('CSS Challenge', page)
        self.assertNotIn('Round 01', page)
        self.assertIn(str(TOTAL_CHECKS), page)
        self.assertIn('CSS DEBUGGING', page)
        self.assertIn('read-only', page)

    def test_arena_renders_the_broken_css_and_every_objective(self):
        page = self.client.get(reverse('home')).content.decode()
        self.assertIn('wf-preview', page)
        self.assertIn('sandbox', page)
        for check in run_checks(CHALLENGE_HTML, STARTER_CSS):
            self.assertIn(f'data-check-id="{check["id"]}"', page)
        # each objective has a hint slot; the hints themselves arrive from /api/hint/
        self.assertEqual(page.count('data-hints-for='), TOTAL_CHECKS)

    def test_arena_never_ships_the_answers_to_the_browser(self):
        page = self.client.get(reverse('home')).content.decode()
        for answer in ('.features__grid {\n  display: grid;\n'
                       '  grid-template-columns: repeat(3, 1fr);',
                       '.hero__title {\n  font-size: clamp(2.4rem, 4.4vw, 3.6rem);',
                       '@media (max-width: 860px)'):
            self.assertNotIn(answer, page, answer)
        for defect in ('display: block', 'font-size: 1rem', 'gap: 150px',
                       'rotate(45deg)', '@media (min-width: 860px)'):
            self.assertIn(defect, page, defect)

    def test_arena_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('home')).status_code, 302)
        self.assertEqual(self.client.get(reverse('start')).status_code, 302)



class FinalPreviewTests(TestCase):
    """The final preview is a visual reference: it shows, it never grades."""

    def setUp(self):
        self.user = User.objects.create_user(username='Tester', pc_no='PC-FP', password='pw-123456')
        self.client.force_login(self.user, backend='first.backends.PCNoBackend')
        self.client.get(reverse('home'))  # start the clock
        self.url = reverse('api_final_preview')

    def fetch(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response

    # 1. renders CHALLENGE_HTML + solution.css
    def test_it_renders_the_challenge_markup_with_the_gold_standard(self):
        body = self.fetch().content.decode()

        # the finished page, in full
        for section in ('id="features"', 'id="pricing"', 'id="faq"',
                        'id="testimonials"', 'id="how-it-works"', 'site-footer'):
            self.assertIn(section, body, section)
        self.assertIn('<h1 class="hero__title">', body)

        # ...styled by the gold standard, inlined in place of the link tag
        self.assertIn('<style>', body)
        self.assertNotIn('href="style.css"', body)
        for correct in ('line-height: 1.6;',
                        'grid-template-columns: repeat(3, 1fr);',
                        '@media (max-width: 860px)'):
            self.assertIn(correct, body, correct)

    # 2. does not use the player's CSS
    def test_it_ignores_whatever_the_player_has_written(self):
        self.client.post(reverse('save_css'), {'css': 'body { background: fuchsia; }'})
        body = self.fetch().content.decode()
        self.assertNotIn('fuchsia', body)
        self.assertIn('line-height: 1.6;', body)

        # ...and the player's own stylesheet is untouched by the visit
        self.assertEqual(self.client.get(reverse('get_css')).json()['css'],
                         'body { background: fuchsia; }')

    # 3. does not modify objective progress
    def test_it_never_grades_or_advances_progress(self):
        before = self.client.post(reverse('api_check'), {'css': STARTER_CSS}).json()
        self.assertEqual(before['passed'], 0)

        self.fetch()
        self.fetch()

        self.user.refresh_from_db()
        self.assertEqual(self.user.best_score, 0)
        self.assertIsNone(self.user.completed_at)
        self.assertFalse(self.client.get(reverse('api_state')).json()['completed'])

    # 4. does not modify the player's CSS
    def test_it_leaves_the_saved_stylesheet_alone(self):
        mine = STARTER_CSS + '\n/* my working notes */\n'
        self.client.post(reverse('save_css'), {'css': mine})
        self.fetch()
        self.assertEqual(self.client.get(reverse('get_css')).json()['css'], mine)

    # 5. does not reveal source code
    def test_the_arena_page_never_carries_the_answers(self):
        arena = self.client.get(reverse('home')).content.decode()
        # anchored to the rule each answer belongs to: `line-height: 1.6` on its
        # own is legitimately still there, for `.testimonial-card__quote`.
        for answer in ('  color: var(--color-text);\n  line-height: 1.6;',
                       '.features__grid {\n  display: grid;\n'
                       '  grid-template-columns: repeat(3, 1fr);',
                       '@media (max-width: 860px)'):
            self.assertNotIn(answer, arena, answer)
        # the arena links to the preview rather than embedding it
        self.assertIn(self.url, arena)

    def test_there_is_no_endpoint_serving_the_raw_stylesheet(self):
        body = self.fetch().content.decode()
        # the gold standard only ever arrives inside a rendered page
        self.assertTrue(body.lstrip().startswith('<!DOCTYPE html>'))
        self.assertNotEqual(body.strip(), SOLUTION_CSS.strip())

    def test_it_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    # 6. does not touch the timer
    def test_it_does_not_start_extend_or_reset_the_clock(self):
        self.user.refresh_from_db()
        started = self.user.game_start_time
        remaining = self.client.get(reverse('api_state')).json()['remaining']

        self.fetch()

        self.user.refresh_from_db()
        self.assertEqual(self.user.game_start_time, started)
        self.assertLessEqual(self.client.get(reverse('api_state')).json()['remaining'], remaining)

    def test_it_does_not_start_the_clock_for_someone_who_never_entered(self):
        fresh = User.objects.create_user(username='Fresh', pc_no='PC-FP2', password='pw-123456')
        self.client.force_login(fresh, backend='first.backends.PCNoBackend')
        self.fetch()
        fresh.refresh_from_db()
        self.assertIsNone(fresh.game_start_time, 'the reference view must not start a round')

    def test_it_still_opens_after_the_session_expires(self):
        self.user.game_start_time = timezone.now() - timedelta(seconds=GAME_DURATION_SECONDS + 1)
        self.user.save(update_fields=['game_start_time'])
        self.assertEqual(self.client.get(self.url).status_code, 200)

    # 7. isolated from the game shell
    def test_it_is_served_as_its_own_document_for_a_sandboxed_frame(self):
        response = self.fetch()
        self.assertTrue(response['Content-Type'].startswith('text/html'))
        self.assertEqual(response['Cache-Control'], 'no-store')

        # The site denies framing everywhere else; this one view must allow the
        # arena to frame it, or the overlay silently renders an empty box.
        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(self.client.get(reverse('home'))['X-Frame-Options'], 'DENY')

        body = response.content.decode()
        # nothing from the game shell leaks in, and nothing of it leaks out
        self.assertNotIn('wf-arena', body)
        self.assertNotIn('wf-base.css', body)
        self.assertNotIn('<script', body)

        arena = self.client.get(reverse('home')).content.decode()
        self.assertIn('id="wf-final-frame"', arena)
        # the frame that shows it carries an empty sandbox, like the main preview
        self.assertIn('class="wf-preview" title="The finished NovaCloud page" sandbox', arena)

    # 8. it can be closed and the arena is still there
    def test_the_overlay_ships_with_a_way_out(self):
        arena = self.client.get(reverse('home')).content.decode()
        self.assertIn('id="wf-final-open"', arena)
        self.assertIn('id="wf-final-close"', arena)
        self.assertIn('id="wf-final"', arena)
        # it is an overlay on the live arena, not a separate page
        self.assertIn('hidden role="dialog"', arena)
        self.assertIn('id="wf-css"', arena)
        self.assertIn('id="wf-run"', arena)


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


# ==========================================================================
# The competition workflow: deadline -> immutable submission -> judging
# ==========================================================================

class CompetitionMixin:
    """Helpers shared by the workflow tests."""

    def make_player(self, pc_no, password='pw-123456'):
        user = User.objects.create_user(username=pc_no, pc_no=pc_no, password=password)
        client = Client()
        client.force_login(user, backend='first.backends.PCNoBackend')
        client.get(reverse('home'))  # starts the clock
        user.refresh_from_db()
        return user, client

    @staticmethod
    def expire(user):
        """Move the deadline into the past, the way the wall clock would."""
        user.game_start_time = timezone.now() - timedelta(seconds=GAME_DURATION_SECONDS + 5)
        user.save(update_fields=['game_start_time'])
        return user

    @staticmethod
    def solve(client):
        return client.post(reverse('api_check'), {'css': graded_solution()}).json()


class DesignModeTests(CompetitionMixin, TestCase):
    """Phase 2: 14/14 opens design mode, it does not end the round."""

    def test_design_mode_is_locked_until_every_objective_is_clear(self):
        user, client = self.make_player('PC-DM1')
        partial = apply_fixes(STARTER_CSS, dict(list(GRADED_FIXES.items())[:13]))

        data = client.post(reverse('api_check'), {'css': partial}).json()
        self.assertEqual(data['passed'], 13)
        self.assertFalse(data['designMode'])
        self.assertFalse(data['eligible'])

        user.refresh_from_db()
        self.assertFalse(user.design_mode)

    def test_clearing_every_objective_opens_design_mode_without_locking(self):
        user, client = self.make_player('PC-DM2')
        data = self.solve(client)

        self.assertEqual(data['passed'], TOTAL_CHECKS)
        self.assertTrue(data['designMode'])
        self.assertTrue(data['eligible'])
        self.assertFalse(data['locked'], 'reaching 14/14 must not end the round')

        user.refresh_from_db()
        self.assertTrue(user.design_mode)
        self.assertFalse(user.is_locked)

    def test_the_player_may_redesign_freely_after_14_of_14(self):
        user, client = self.make_player('PC-DM3')
        self.solve(client)

        mine = 'body { background: #ff00ff; font-family: Comic Sans MS; }'
        saved = client.post(reverse('save_css'), {'css': mine}).json()
        self.assertNotIn('error', saved)
        self.assertEqual(client.get(reverse('get_css')).json()['css'], mine)

        # a wild redesign does not take eligibility away
        user.refresh_from_db()
        self.assertTrue(user.is_eligible)

    def test_a_redesign_that_breaks_objectives_keeps_the_earned_eligibility(self):
        user, client = self.make_player('PC-DM4')
        self.solve(client)
        client.post(reverse('api_check'), {'css': 'body{}'})

        user.refresh_from_db()
        self.assertTrue(user.is_eligible, 'they cleared 14/14 before the deadline')
        self.assertEqual(user.best_score, TOTAL_CHECKS)


class DeadlineTests(CompetitionMixin, TestCase):
    """What the server does when the clock runs out."""

    def test_expiry_captures_the_stylesheet_as_it_stood(self):
        user, client = self.make_player('PC-EX1')
        self.solve(client)
        mine = graded_solution() + '\n/* MY ENTRY */\nbody { background: #102030; }\n'
        client.post(reverse('save_css'), {'css': mine})

        self.expire(user)
        client.get(reverse('api_state'))

        final = FinalSubmission.objects.get(user=user)
        self.assertEqual(final.final_css, mine)
        self.assertIn('MY ENTRY', final.final_css)
        self.assertEqual(final.score, TOTAL_CHECKS)
        self.assertTrue(final.eligible)
        self.assertTrue(final.reached_all)

    def test_expiry_records_the_deadline_not_the_moment_of_discovery(self):
        user, client = self.make_player('PC-EX2')
        self.expire(user)
        user.refresh_from_db()

        client.get(reverse('api_state'))
        final = FinalSubmission.objects.get(user=user)
        self.assertAlmostEqual(
            (final.submitted_at - user.deadline).total_seconds(), 0, delta=1,
        )
        self.assertEqual(final.started_at, user.game_start_time)

    def test_expiry_locks_the_editor(self):
        user, client = self.make_player('PC-EX3')
        self.expire(user)
        state = client.get(reverse('api_state')).json()
        self.assertTrue(state['locked'])
        self.assertTrue(state['expired'])
        self.assertTrue(state['submitted'])

    def test_expiry_deletes_nothing(self):
        user, client = self.make_player('PC-EX4')
        client.post(reverse('api_hint'), {'objective': 'css-console', 'level': 1})
        client.post(reverse('save_css'), {'css': 'body { color: red; }'})

        self.expire(user)
        client.get(reverse('api_state'))

        self.assertTrue(User.objects.filter(pc_no='PC-EX4').exists())
        self.assertTrue(CssRule.objects.filter(user=user).exists())
        self.assertTrue(HintReveal.objects.filter(user=user).exists())
        self.assertTrue(FinalSubmission.objects.filter(user=user).exists())

    def test_the_snapshot_is_taken_once_and_never_rewritten(self):
        user, client = self.make_player('PC-EX5')
        client.post(reverse('save_css'), {'css': 'body { color: teal; }'})
        self.expire(user)

        client.get(reverse('api_state'))
        first = FinalSubmission.objects.get(user=user)

        # every later request must find the same row, untouched
        for _ in range(3):
            client.get(reverse('api_state'))
            client.get(reverse('home'))
        self.assertEqual(FinalSubmission.objects.filter(user=user).count(), 1)

        again = FinalSubmission.objects.get(user=user)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.final_css, first.final_css)
        self.assertEqual(again.submitted_at, first.submitted_at)

    def test_final_css_is_immutable(self):
        user, client = self.make_player('PC-EX6')
        client.post(reverse('save_css'), {'css': 'body { color: teal; }'})
        self.expire(user)
        client.get(reverse('api_state'))

        final = FinalSubmission.objects.get(user=user)
        final.final_css = 'body { color: hacked; }'
        with self.assertRaises(ValueError):
            final.save()

        self.assertEqual(
            FinalSubmission.objects.get(user=user).final_css, 'body { color: teal; }',
        )

    def test_a_forgotten_player_is_submitted_without_anyone_watching(self):
        """Nobody touches the browser; the next server hit finalises them."""
        user, client = self.make_player('PC-FORGOT')
        self.solve(client)
        client.post(reverse('save_css'), {'css': graded_solution() + '\n/* LEFT */\n'})
        client.logout()

        self.expire(user)
        self.assertFalse(FinalSubmission.objects.filter(user=user).exists())

        # an organiser opening the admin list is enough to settle it
        staff = User.objects.create_superuser(
            username='ref', pc_no='PC-REF', password='pw-123456')
        admin_client = Client()
        admin_client.force_login(staff, backend='first.backends.PCNoBackend')
        admin_client.get(reverse('admin:first_user_changelist'))

        finalize_if_due(User.objects.get(pk=user.pk))
        final = FinalSubmission.objects.get(user=user)
        self.assertIn('LEFT', final.final_css)
        self.assertTrue(final.eligible)


class EligibilityTests(CompetitionMixin, TestCase):
    def test_fourteen_of_fourteen_before_the_deadline_is_eligible(self):
        user, client = self.make_player('PC-EL1')
        self.solve(client)
        self.expire(user)
        client.get(reverse('api_state'))

        final = FinalSubmission.objects.get(user=user)
        self.assertTrue(final.eligible)
        self.assertEqual(final.status, 'Submitted')

    def test_anything_less_is_not_eligible_but_is_still_kept(self):
        user, client = self.make_player('PC-EL2')
        partial = apply_fixes(STARTER_CSS, dict(list(GRADED_FIXES.items())[:11]))
        client.post(reverse('api_check'), {'css': partial})
        self.expire(user)
        client.get(reverse('api_state'))

        final = FinalSubmission.objects.get(user=user)
        self.assertFalse(final.eligible)
        self.assertEqual(final.score, 11)
        self.assertEqual(final.status, 'Expired')
        self.assertEqual(final.final_css, partial, 'their work is still preserved')

    def test_reaching_the_last_objective_after_the_deadline_does_not_count(self):
        user, client = self.make_player('PC-EL3')
        self.expire(user)
        client.post(reverse('api_check'), {'css': graded_solution()})

        user.refresh_from_db()
        self.assertFalse(user.is_eligible)
        self.assertIsNone(user.completed_at)


class PostExpirySecurityTests(CompetitionMixin, TestCase):
    """Buttons are disabled in the browser; the server is what actually stops it."""

    def setUp(self):
        self.user, self.client_ = self.make_player('PC-SEC')
        self.client_.post(reverse('api_hint'), {'objective': 'css-console', 'level': 1})
        self.client_.post(reverse('save_css'), {'css': 'body { color: teal; }'})
        self.expire(self.user)
        self.client_.get(reverse('api_state'))

    def test_saving_css_is_rejected(self):
        data = self.client_.post(reverse('save_css'), {'css': 'body { color: red; }'}).json()
        self.assertEqual(data['error'], 'Time is up!')
        self.assertEqual(self.client_.get(reverse('get_css')).json()['css'],
                         'body { color: teal; }')

    def test_running_checks_is_rejected(self):
        data = self.client_.post(reverse('api_check'), {'css': graded_solution()}).json()
        self.assertEqual(data['error'], 'Time is up!')
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_eligible)

    def test_resetting_is_rejected(self):
        data = self.client_.post(reverse('api_reset'), {}).json()
        self.assertNotIn('css', data)
        self.assertEqual(self.client_.get(reverse('get_css')).json()['css'],
                         'body { color: teal; }')

    def test_requesting_another_hint_is_rejected_and_the_count_is_frozen(self):
        before = self.user.hints_used
        data = self.client_.post(
            reverse('api_hint'), {'objective': 'css-features', 'level': 1}).json()
        self.assertEqual(data['error'], 'Time is up!')
        self.assertNotIn('hint', data)

        self.user.refresh_from_db()
        self.assertEqual(self.user.hints_used, before)
        self.assertEqual(FinalSubmission.objects.get(user=self.user).hints_used, before)

    def test_a_forged_timer_cannot_buy_more_time(self):
        """The client may claim anything; the server reads its own clock."""
        for payload in ({'remaining': 999, 'css': 'x'},
                        {'expired': 'false', 'css': 'x'},
                        {'duration': 99999, 'css': 'x'}):
            data = self.client_.post(reverse('save_css'), payload).json()
            self.assertEqual(data['error'], 'Time is up!')
            self.assertTrue(data['expired'])
            self.assertLessEqual(data['remaining'], 0)

    def test_a_forged_score_or_hint_count_is_ignored(self):
        data = self.client_.post(reverse('api_check'), {
            'css': STARTER_CSS, 'score': 14, 'passed': 14,
            'hintsUsed': 0, 'eligible': 'true',
        }).json()
        self.assertEqual(data['error'], 'Time is up!')

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_eligible)
        self.assertEqual(self.user.hints_used, 1)

    def test_a_refresh_after_expiry_stays_locked(self):
        page = self.client_.get(reverse('home'))
        self.assertEqual(page.status_code, 200)

        state = self.client_.get(reverse('api_state')).json()
        self.assertTrue(state['locked'])
        self.assertEqual(state['remaining'], 0)
        self.assertTrue(state['submitted'])
        self.assertEqual(FinalSubmission.objects.filter(user=self.user).count(), 1)


class HintTrackingTests(CompetitionMixin, TestCase):
    def setUp(self):
        self.user, self.client_ = self.make_player('PC-HINT')

    def reveal(self, objective, level):
        return self.client_.post(
            reverse('api_hint'), {'objective': objective, 'level': level}).json()

    def test_the_first_reveal_is_charged_and_returns_the_hint(self):
        data = self.reveal('css-navbar-row', 1)
        self.assertTrue(data['charged'])
        self.assertEqual(data['hintsUsed'], 1)
        self.assertIn('horizontal line', data['hint'])
        # hint 1 names no selector by design, so it carries no code span;
        # hint 2 points at a rule and does.
        second = self.reveal('css-navbar-row', 2)
        self.assertIn('<code>.navbar</code>', second['hintHtml'])

    def test_revealing_the_same_hint_again_is_free(self):
        self.reveal('css-navbar-row', 1)
        for _ in range(5):
            data = self.reveal('css-navbar-row', 1)
            self.assertFalse(data['charged'])
        self.assertEqual(data['hintsUsed'], 1)
        self.assertEqual(HintReveal.objects.filter(user=self.user).count(), 1)

    def test_each_level_is_tracked_independently(self):
        for level in (1, 2, 3):
            self.assertTrue(self.reveal('css-console', level)['charged'])
        data = self.reveal('css-console', 1)
        self.assertEqual(data['hintsUsed'], 3)
        self.assertEqual(data['objectivesHinted'], 1)

    def test_each_objective_is_tracked_independently(self):
        self.reveal('css-navbar-row', 1)
        self.reveal('css-navbar-row', 2)
        self.reveal('css-console', 1)
        data = self.reveal('css-features', 1)
        self.assertEqual(data['hintsUsed'], 4)
        self.assertEqual(data['objectivesHinted'], 3)

    def test_the_spec_example_counts_as_three(self):
        """Hint 1 + Hint 1 + Hint 2 + Hint 3 = 3."""
        self.reveal('css-pricing', 1)
        self.reveal('css-pricing', 1)
        self.reveal('css-pricing', 2)
        data = self.reveal('css-pricing', 3)
        self.assertEqual(data['hintsUsed'], 3)

    def test_a_forged_objective_or_level_is_refused(self):
        for payload in ({'objective': 'css-nope', 'level': 1},
                        {'objective': 'css-console', 'level': 0},
                        {'objective': 'css-console', 'level': 4},
                        {'objective': '', 'level': 1}):
            response = self.client_.post(reverse('api_hint'), payload)
            self.assertEqual(response.status_code, 400, payload)
        self.assertEqual(HintReveal.objects.filter(user=self.user).count(), 0)

    def test_a_forged_hint_total_is_ignored(self):
        self.reveal('css-console', 1)
        data = self.client_.post(reverse('api_hint'), {
            'objective': 'css-console', 'level': 2, 'hintsUsed': 0,
        }).json()
        self.assertEqual(data['hintsUsed'], 2)

    def test_hint_history_survives_a_refresh(self):
        self.reveal('css-steps', 1)
        self.reveal('css-steps', 2)
        page = self.client_.get(reverse('home')).content.decode()
        self.assertIn('css-steps', page)
        self.assertEqual(self.client_.get(reverse('api_state')).json()['hintsUsed'], 2)

    def test_hint_history_survives_logout(self):
        self.reveal('css-steps', 1)
        self.client_.get(reverse('logout'))
        self.assertEqual(HintReveal.objects.filter(user=self.user).count(), 1)

        self.client_.force_login(self.user, backend='first.backends.PCNoBackend')
        self.assertEqual(self.client_.get(reverse('api_state')).json()['hintsUsed'], 1)

    def test_hint_history_survives_expiry(self):
        self.reveal('css-steps', 1)
        self.reveal('css-hero-gap', 1)
        self.expire(self.user)
        self.client_.get(reverse('api_state'))

        final = FinalSubmission.objects.get(user=self.user)
        self.assertEqual(final.hints_used, 2)
        self.assertEqual(final.objectives_hinted, 2)
        self.assertEqual(HintReveal.objects.filter(user=self.user).count(), 2)

    def test_the_maximum_is_three_per_objective(self):
        state = self.client_.get(reverse('api_state')).json()
        self.assertEqual(state['maxHints'], TOTAL_CHECKS * 3)

    def test_hints_are_not_shipped_in_the_arena_page(self):
        """They are fetched, so reading the source does not give them away free."""
        page = self.client_.get(reverse('home')).content.decode()
        self.assertNotIn('Look at the <code>.navbar</code> rule', page)
        self.assertIn('data-hints-for="css-navbar-row"', page)

    def test_hints_require_authentication(self):
        self.client_.logout()
        response = self.client_.post(
            reverse('api_hint'), {'objective': 'css-console', 'level': 1})
        self.assertEqual(response.status_code, 403)


class PlayerFinalDesignTests(CompetitionMixin, TestCase):
    """The player's own entry, kept distinct from the official reference."""

    def setUp(self):
        self.user, self.client_ = self.make_player('PC-MINE')
        self.solve(self.client_)
        self.mine = graded_solution() + '\n/* SIGNATURE */\nbody { background: #abcdef; }\n'
        self.client_.post(reverse('save_css'), {'css': self.mine})

    def test_it_is_unavailable_until_the_round_closes(self):
        response = self.client_.get(reverse('api_final_design'))
        self.assertEqual(response.status_code, 302)

    def test_it_renders_the_players_own_css(self):
        self.expire(self.user)
        body = self.client_.get(reverse('api_final_design')).content.decode()
        self.assertIn('SIGNATURE', body)
        self.assertIn('#abcdef', body)
        self.assertIn('<h1 class="hero__title">', body)

    def test_it_does_not_render_the_official_solution(self):
        self.expire(self.user)
        body = self.client_.get(reverse('api_final_design')).content.decode()
        self.assertNotIn(SOLUTION_CSS, body)

    def test_it_is_a_different_document_from_the_final_preview(self):
        self.expire(self.user)
        mine = self.client_.get(reverse('api_final_design')).content.decode()
        official = self.client_.get(reverse('api_final_preview')).content.decode()

        self.assertNotEqual(mine, official)
        self.assertIn('SIGNATURE', mine)
        self.assertNotIn('SIGNATURE', official)
        self.assertIn(SOLUTION_CSS, official)

    def test_the_final_preview_still_shows_the_official_solution(self):
        body = self.client_.get(reverse('api_final_preview')).content.decode()
        self.assertIn('  color: var(--color-text);\n  line-height: 1.6;', body)

    def test_it_keeps_showing_the_snapshot_even_if_the_row_changes_later(self):
        self.expire(self.user)
        self.client_.get(reverse('api_state'))
        CssRule.objects.filter(user=self.user).update(css='body { background: black; }')

        body = self.client_.get(reverse('api_final_design')).content.decode()
        self.assertIn('SIGNATURE', body, 'the entry is the snapshot, not the live row')

    def test_it_is_framed_safely_and_requires_login(self):
        self.expire(self.user)
        response = self.client_.get(reverse('api_final_design'))
        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn('wf-arena', response.content.decode())

        self.client_.logout()
        self.assertEqual(self.client_.get(reverse('api_final_design')).status_code, 302)


class ExitAndNextParticipantTests(CompetitionMixin, TestCase):
    def test_exit_returns_to_the_login_screen_and_keeps_everything(self):
        user, client = self.make_player('PC-EXIT')
        self.solve(client)
        client.post(reverse('save_css'), {'css': graded_solution() + '/* KEEP */'})
        self.expire(user)
        client.get(reverse('api_state'))

        response = client.get(reverse('logout'))
        self.assertRedirects(response, reverse('login'))
        self.assertNotIn('_auth_user_id', client.session)

        self.assertTrue(User.objects.filter(pc_no='PC-EXIT').exists())
        final = FinalSubmission.objects.get(user__pc_no='PC-EXIT')
        self.assertIn('KEEP', final.final_css)
        self.assertTrue(final.eligible)

    def test_a_new_participant_inherits_nothing(self):
        first, first_client = self.make_player('PC-ONE')
        first_client.post(reverse('api_hint'), {'objective': 'css-console', 'level': 1})
        self.solve(first_client)
        first_client.post(reverse('save_css'), {'css': 'body { background: #111111; }'})
        self.expire(first)
        first_client.get(reverse('api_state'))
        first_client.get(reverse('logout'))

        second, second_client = self.make_player('PC-TWO')
        state = second_client.get(reverse('api_state')).json()

        self.assertEqual(second_client.get(reverse('get_css')).json()['css'], STARTER_CSS)
        self.assertEqual(state['score'], 0)
        self.assertFalse(state['designMode'])
        self.assertFalse(state['expired'])
        self.assertFalse(state['submitted'])
        self.assertEqual(state['hintsUsed'], 0)
        self.assertGreater(state['remaining'], GAME_DURATION_SECONDS - 30)
        self.assertFalse(FinalSubmission.objects.filter(user=second).exists())

        # ...and the first player's entry is untouched by any of it
        self.assertIn('#111111', FinalSubmission.objects.get(user=first).final_css)


class AdminJudgingTests(CompetitionMixin, TestCase):
    def setUp(self):
        self.player, self.player_client = self.make_player('PC-JUDGE')
        self.player_client.post(reverse('api_hint'), {'objective': 'css-navbar-row', 'level': 1})
        self.player_client.post(reverse('api_hint'), {'objective': 'css-navbar-row', 'level': 2})
        self.player_client.post(reverse('api_hint'), {'objective': 'css-console', 'level': 1})
        self.solve(self.player_client)
        self.entry = graded_solution() + '\n/* JUDGE ME */\nbody { background: #fedcba; }\n'
        self.player_client.post(reverse('save_css'), {'css': self.entry})
        self.expire(self.player)
        self.player_client.get(reverse('api_state'))
        self.submission = FinalSubmission.objects.get(user=self.player)

        self.staff = User.objects.create_superuser(
            username='organiser', pc_no='PC-ADMIN', password='pw-123456')
        self.admin = Client()
        self.admin.force_login(self.staff, backend='first.backends.PCNoBackend')

    def test_admin_lists_participants_with_the_judging_columns(self):
        page = self.admin.get(reverse('admin:first_finalsubmission_changelist'))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn('PC-JUDGE', body)
        self.assertIn('14/14', body)
        self.assertIn('View final design', body)

    def test_admin_can_inspect_score_eligibility_and_hints(self):
        page = self.admin.get(
            reverse('admin:first_finalsubmission_change', args=[self.submission.pk]))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn('14/14', body)
        self.assertIn('PC-JUDGE', body)

        self.assertEqual(self.submission.score, TOTAL_CHECKS)
        self.assertTrue(self.submission.eligible)
        self.assertEqual(self.submission.hints_used, 3)
        self.assertEqual(self.submission.objectives_hinted, 2)

    def test_admin_final_design_renders_the_participants_own_css(self):
        page = self.admin.get(
            reverse('admin:first_finalsubmission_design', args=[self.submission.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertIn('sandbox', page.content.decode())

        rendered = self.admin.get(
            reverse('admin:first_finalsubmission_design_render', args=[self.submission.pk]))
        body = rendered.content.decode()
        self.assertIn('JUDGE ME', body)
        self.assertIn('#fedcba', body)
        self.assertNotIn(SOLUTION_CSS, body)
        self.assertEqual(rendered['X-Frame-Options'], 'SAMEORIGIN')

    def test_admin_can_view_the_submitted_css(self):
        page = self.admin.get(
            reverse('admin:first_finalsubmission_css', args=[self.submission.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertIn('JUDGE ME', page.content.decode())

    def test_admin_can_view_hint_usage(self):
        page = self.admin.get(
            reverse('admin:first_finalsubmission_hints', args=[self.submission.pk]))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn('Total hints used: 3', body)
        self.assertIn('css-navbar-row', body)
        self.assertIn('css-console', body)
        self.assertIn('The header lays out in a row', body)

    def test_admin_previews_cannot_modify_the_submission(self):
        before = FinalSubmission.objects.get(pk=self.submission.pk)
        for name in ('design', 'design_render', 'css', 'hints'):
            self.admin.get(reverse(f'admin:first_finalsubmission_{name}',
                                   args=[self.submission.pk]))

        after = FinalSubmission.objects.get(pk=self.submission.pk)
        self.assertEqual(after.final_css, before.final_css)
        self.assertEqual(after.score, before.score)
        self.assertEqual(after.eligible, before.eligible)
        self.assertEqual(after.submitted_at, before.submitted_at)
        self.assertEqual(after.hints_used, before.hints_used)
        self.assertTrue(User.objects.filter(pk=self.player.pk).exists())

    def test_the_submission_admin_is_view_only(self):
        from django.contrib import admin as django_admin
        model_admin = django_admin.site._registry[FinalSubmission]
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_add_permission(None))

    def test_participant_passwords_are_never_rendered(self):
        for url in (reverse('admin:first_user_changelist'),
                    reverse('admin:first_user_change', args=[self.player.pk]),
                    reverse('admin:first_finalsubmission_changelist')):
            body = self.admin.get(url).content.decode()
            self.assertNotIn(self.player.password, body)
            self.assertNotIn('pbkdf2', body)

    def test_a_participant_cannot_reach_the_admin(self):
        response = self.player_client.get(
            reverse('admin:first_finalsubmission_changelist'))
        self.assertIn(response.status_code, (302, 403))

    def test_an_anonymous_visitor_cannot_reach_the_judging_views(self):
        anonymous = Client()
        for name in ('design', 'design_render', 'css', 'hints'):
            response = anonymous.get(
                reverse(f'admin:first_finalsubmission_{name}', args=[self.submission.pk]))
            self.assertIn(response.status_code, (302, 403), name)


class AdminDeletionTests(CompetitionMixin, TestCase):
    """Deletion is manual, deliberate, and takes everything with it."""

    def setUp(self):
        self.player, self.player_client = self.make_player('PC-DEL')
        self.player_client.post(reverse('api_hint'), {'objective': 'css-console', 'level': 1})
        self.solve(self.player_client)
        self.player_client.post(reverse('save_css'), {'css': graded_solution() + '/* BYE */'})
        self.expire(self.player)
        self.player_client.get(reverse('api_state'))

        self.staff = User.objects.create_superuser(
            username='organiser', pc_no='PC-ADMIN2', password='pw-123456')
        self.admin = Client()
        self.admin.force_login(self.staff, backend='first.backends.PCNoBackend')

    def assert_player_data_present(self):
        self.assertTrue(User.objects.filter(pc_no='PC-DEL').exists())
        self.assertTrue(FinalSubmission.objects.filter(pc_no='PC-DEL').exists())
        self.assertTrue(HintReveal.objects.filter(user=self.player).exists())
        self.assertTrue(CssRule.objects.filter(user=self.player).exists())

    def test_expiry_does_not_delete_anything(self):
        self.assert_player_data_present()

    def test_exit_and_logout_do_not_delete_anything(self):
        self.player_client.get(reverse('logout'))
        self.assert_player_data_present()

    def test_everything_is_inspectable_before_deletion(self):
        submission = FinalSubmission.objects.get(user=self.player)
        for name in ('design', 'design_render', 'css', 'hints'):
            response = self.admin.get(
                reverse(f'admin:first_finalsubmission_{name}', args=[submission.pk]))
            self.assertEqual(response.status_code, 200, name)
        self.assertIn('BYE', self.admin.get(
            reverse('admin:first_finalsubmission_css', args=[submission.pk])
        ).content.decode())

    def test_a_participant_cannot_delete_their_own_data(self):
        for method, url in ((self.player_client.post, reverse('api_reset')),
                            (self.player_client.post, reverse('save_css'))):
            method(url, {})
        self.assert_player_data_present()

        # ...and there is no game endpoint that deletes anything
        response = self.player_client.post(
            reverse('admin:first_user_changelist'),
            {'action': 'delete_selected', '_selected_action': [self.player.pk]})
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(User.objects.filter(pc_no='PC-DEL').exists())

    def test_an_unauthorised_user_cannot_delete_a_participant(self):
        outsider, outsider_client = self.make_player('PC-OUTSIDER')
        response = outsider_client.post(
            reverse('admin:first_user_delete', args=[self.player.pk]), {'post': 'yes'})
        self.assertIn(response.status_code, (302, 403))
        self.assert_player_data_present()

    def test_deletion_asks_for_confirmation_first(self):
        page = self.admin.get(reverse('admin:first_user_delete', args=[self.player.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertIn('Are you sure', page.content.decode())
        self.assert_player_data_present()

    def test_an_authorised_admin_can_delete_a_participant_and_all_their_data(self):
        player_pk = self.player.pk
        response = self.admin.post(
            reverse('admin:first_user_delete', args=[player_pk]), {'post': 'yes'})
        self.assertEqual(response.status_code, 302)

        self.assertFalse(User.objects.filter(pk=player_pk).exists())
        self.assertFalse(FinalSubmission.objects.filter(user_id=player_pk).exists())
        self.assertFalse(HintReveal.objects.filter(user_id=player_pk).exists())
        self.assertFalse(CssRule.objects.filter(user_id=player_pk).exists())

    def test_the_submission_action_deletes_the_participant_behind_it(self):
        submission = FinalSubmission.objects.get(user=self.player)
        player_pk = self.player.pk

        self.admin.post(
            reverse('admin:first_finalsubmission_changelist'),
            {'action': 'delete_participants',
             '_selected_action': [submission.pk],
             'index': 0},
            follow=True,
        )

        self.assertFalse(User.objects.filter(pk=player_pk).exists())
        self.assertFalse(FinalSubmission.objects.filter(pk=submission.pk).exists())
        self.assertFalse(HintReveal.objects.filter(user_id=player_pk).exists())
        self.assertFalse(CssRule.objects.filter(user_id=player_pk).exists())

    def test_no_orphans_are_left_behind(self):
        self.admin.post(reverse('admin:first_user_delete', args=[self.player.pk]),
                        {'post': 'yes'})
        live = set(User.objects.values_list('pk', flat=True))
        for model in (FinalSubmission, HintReveal, CssRule):
            orphans = model.objects.exclude(user_id__in=live)
            self.assertFalse(orphans.exists(), model.__name__)

    def test_deleting_one_participant_leaves_the_others_alone(self):
        other, other_client = self.make_player('PC-KEEP')
        other_client.post(reverse('save_css'), {'css': 'body { color: keep; }'})
        self.expire(other)
        other_client.get(reverse('api_state'))

        self.admin.post(reverse('admin:first_user_delete', args=[self.player.pk]),
                        {'post': 'yes'})

        self.assertFalse(User.objects.filter(pc_no='PC-DEL').exists())
        self.assertTrue(User.objects.filter(pc_no='PC-KEEP').exists())
        self.assertIn('keep', FinalSubmission.objects.get(pc_no='PC-KEEP').final_css)

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

    def setUp(self):
        self.user = User.objects.create_user(username='Tester', pc_no='PC-HM', password='pw-123456')
        self.client.force_login(self.user, backend='first.backends.PCNoBackend')

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

    def test_the_api_renders_hint_code_spans(self):
        """The arena fetches hints, so the code spans come back on the wire."""
        self.client.get(reverse('home'))
        data = self.client.post(
            reverse('api_hint'), {'objective': 'css-navbar-row', 'level': 2}).json()
        self.assertIn('<code>.navbar</code>', data['hintHtml'])
        self.assertNotIn('`.navbar`', data['hintHtml'])

    def test_the_arena_page_does_not_leak_hints(self):
        page = self.client.get(reverse('home')).content.decode()
        self.assertNotIn('<code>.navbar</code>', page)
        for objective in ('css-navbar-row', 'css-console', 'css-responsive'):
            self.assertIn(f'data-hints-for="{objective}"', page)


class ForgottenPlayerTests(CompetitionMixin, TestCase):
    """Nobody is watching the screen; the entry must still exist for judging."""

    def test_a_walked_away_player_is_settled_when_an_organiser_looks(self):
        user, client = self.make_player('PC-GHOST')
        client.post(reverse('api_hint'), {'objective': 'css-console', 'level': 1})
        self.solve(client)
        client.post(reverse('save_css'), {'css': graded_solution() + '/* GONE */'})
        client.get(reverse('logout'))          # they close the laptop and leave
        self.expire(user)

        # nothing has touched that account, so no snapshot exists yet
        self.assertFalse(FinalSubmission.objects.filter(user=user).exists())

        staff = User.objects.create_superuser(
            username='organiser', pc_no='PC-ORG-G', password='pw-123456')
        admin = Client()
        admin.force_login(staff, backend='first.backends.PCNoBackend')

        # opening either participant list is enough to settle them
        page = admin.get(reverse('admin:first_finalsubmission_changelist'))
        self.assertEqual(page.status_code, 200)

        final = FinalSubmission.objects.get(user=user)
        self.assertIn('GONE', final.final_css)
        self.assertTrue(final.eligible)
        self.assertEqual(final.hints_used, 1)
        self.assertIn('PC-GHOST', page.content.decode())

    def test_the_participant_list_settles_them_too(self):
        user, client = self.make_player('PC-GHOST2')
        client.post(reverse('save_css'), {'css': 'body { color: ghost; }'})
        self.expire(user)

        staff = User.objects.create_superuser(
            username='organiser2', pc_no='PC-ORG-G2', password='pw-123456')
        admin = Client()
        admin.force_login(staff, backend='first.backends.PCNoBackend')
        admin.get(reverse('admin:first_user_changelist'))

        self.assertIn('ghost', FinalSubmission.objects.get(user=user).final_css)

    def test_settling_is_idempotent_and_skips_live_rounds(self):
        live, _ = self.make_player('PC-LIVE')
        done, done_client = self.make_player('PC-DONE')
        self.expire(done)

        self.assertEqual(finalize_all_due(), 1)
        self.assertEqual(finalize_all_due(), 0, 'a second sweep creates nothing')

        self.assertTrue(FinalSubmission.objects.filter(user=done).exists())
        self.assertFalse(FinalSubmission.objects.filter(user=live).exists(),
                         'a running round must not be submitted early')


class EnsureAdminCommandTests(TestCase):
    """The deploy-time admin account: created once, safe to re-run forever."""

    PC_NO = 'admin123'
    PASSWORD = 'piyush123456@'

    def run_command(self):
        out = StringIO()
        call_command('ensure_admin', stdout=out)
        return out.getvalue()

    def test_it_creates_the_admin_account(self):
        self.assertFalse(User.objects.filter(pc_no=self.PC_NO).exists())
        self.run_command()

        admin = User.objects.get(pc_no=self.PC_NO)
        self.assertTrue(admin.is_admin)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_active)
        self.assertTrue(admin.check_password(self.PASSWORD))

    def test_running_it_again_changes_nothing(self):
        self.run_command()
        original = User.objects.get(pc_no=self.PC_NO)

        for _ in range(3):
            output = self.run_command()
            self.assertIn('already exists', output)

        self.assertEqual(User.objects.filter(pc_no=self.PC_NO).count(), 1)
        again = User.objects.get(pc_no=self.PC_NO)
        self.assertEqual(again.pk, original.pk)
        self.assertEqual(again.password, original.password)

    def test_it_does_not_reset_a_password_changed_later(self):
        self.run_command()
        admin = User.objects.get(pc_no=self.PC_NO)
        admin.set_password('something-else-entirely')
        admin.save(update_fields=['password'])

        self.run_command()

        admin.refresh_from_db()
        self.assertTrue(admin.check_password('something-else-entirely'))
        self.assertFalse(admin.check_password(self.PASSWORD))

    def test_the_login_field_is_the_pc_number_not_an_email(self):
        self.run_command()
        self.assertEqual(User.USERNAME_FIELD, 'pc_no')
        self.assertNotIn('email', [f.name for f in User._meta.get_fields()])

        # this is exactly what the admin login form posts
        signed_in = self.client.login(pc_no=self.PC_NO, password=self.PASSWORD)
        self.assertTrue(signed_in)

    def test_the_admin_account_can_reach_the_judging_screens(self):
        self.run_command()
        self.client.login(pc_no=self.PC_NO, password=self.PASSWORD)

        for url in (reverse('admin:index'),
                    reverse('admin:first_user_changelist'),
                    reverse('admin:first_finalsubmission_changelist')):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_it_creates_no_other_admin_accounts(self):
        self.run_command()
        self.assertEqual(
            sorted(User.objects.filter(is_admin=True).values_list('pc_no', flat=True)),
            [self.PC_NO],
        )

    def test_it_promotes_an_existing_non_admin_with_that_pc_number(self):
        User.objects.create_user(username='someone', pc_no=self.PC_NO, password='pw-123456')
        output = self.run_command()

        self.assertIn('Granted admin rights', output)
        self.assertTrue(User.objects.get(pc_no=self.PC_NO).is_admin)
        self.assertEqual(User.objects.filter(pc_no=self.PC_NO).count(), 1)

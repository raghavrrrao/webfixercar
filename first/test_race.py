"""
The race: one official attempt, a server-owned clock, and a website that only
gets rebuilt by driving to the parts.

Every one of these tests drives the same HTTP API the browser drives, because
that is the only thing an attacker has too. The rule the whole suite is built
around is that the browser is never believed: it says what it thinks happened,
and the server decides what did.

Distance is the spine of it. The car reports how far it has got; the server
accepts that only if the car could physically have covered it in the time the
server has been counting, and every repair and the finish line is then checked
against the accepted distance. So the tests simulate driving by moving the
recorded start time backwards — exactly what the wall clock would have done to
it — which exercises the real `timezone.now()` comparisons and means no test
has to sit through twelve minutes.
"""

import json
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .game_config import (
    GAME_DURATION_SECONDS,
    RACE_BASE_POINTS,
    RACE_CLEAN_RUN_BONUS,
    RACE_COLLISION_PENALTY,
    RACE_COURSE_METRES,
    RACE_MAX_COLLISIONS,
    RACE_MAX_COLLISIONS_PER_REPORT,
    RACE_MAX_SCORE,
    RACE_MIN_SECONDS,
    RACE_REPAIR_METRES,
    RACE_REPAIR_POINTS,
    RACE_SECTION_METRES,
    RACE_TIME_POINTS,
    RACE_TOP_SPEED,
    SOLUTION_CSS,
    STARTER_CSS,
)
from .models import (
    RACE_ACTIVE,
    RACE_COMPLETED,
    RACE_EXPIRED,
    RACE_NOT_STARTED,
    FinalSubmission,
    User,
)
from .repairs import REPAIR_COUNT, REPAIR_IDS, is_fully_repaired, repair_css
from .views import race_score


class RaceMixin:
    """Helpers for driving a race the way the browser does."""

    @staticmethod
    def urls():
        return {
            'start': reverse('api_race_start'),
            'state': reverse('api_race_state'),
            'progress': reverse('api_race_progress'),
            'complete': reverse('api_race_complete'),
            'preview': reverse('api_race_preview'),
            'home': reverse('home'),
            'result': reverse('race_result'),
            'fixed': reverse('api_final_preview'),
            'broken': reverse('api_broken_preview'),
        }

    def make_player(self, pc_no, password='pw-123456'):
        """A signed-in participant who has *not* started a race."""
        user = User.objects.create_user(username=pc_no, pc_no=pc_no, password=password)
        client = Client()
        client.force_login(user, backend='first.backends.PCNoBackend')
        return user, client

    def start_race(self, client, user):
        response = client.post(self.urls()['start'], {})
        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()
        return response.json()

    @staticmethod
    def at(user, seconds):
        """Put the race exactly `seconds` into its run, on the server clock."""
        started = timezone.now() - timedelta(seconds=seconds)
        User.objects.filter(pk=user.pk).update(
            race_started_at=started, game_start_time=started)
        user.refresh_from_db()
        return user

    @staticmethod
    def seconds_for(metres):
        """The least time an honest car needs to cover `metres`."""
        return int(metres / RACE_TOP_SPEED) + 1

    def time_out(self, user):
        """Push the attempt past the twelve-minute deadline."""
        return self.at(user, GAME_DURATION_SECONDS + 5)

    def drive_to(self, client, user, metres, **extra):
        """Spend the time it takes to reach `metres`, then report being there."""
        self.at(user, self.seconds_for(metres))
        response = client.post(self.urls()['progress'], {'distance': metres, **extra})
        user.refresh_from_db()
        return response

    def collect(self, client, user, index):
        """Drive to repair `index` and pick it up."""
        self.drive_to(client, user, RACE_REPAIR_METRES[index])
        response = client.post(self.urls()['progress'],
                               {'repair': REPAIR_IDS[index]})
        user.refresh_from_db()
        return response

    def drive_course(self, client, user, repairs=REPAIR_COUNT, collisions=0):
        """Collect `repairs` components in order. Leaves the race unfinished."""
        for index in range(repairs):
            response = self.collect(client, user, index)
            self.assertEqual(response.status_code, 200, response.content)
        for _ in range(collisions):
            self.assertEqual(
                client.post(self.urls()['progress'], {'collisions': 1}).status_code, 200)
        user.refresh_from_db()

    def cross_finish(self, client, user, seconds=None):
        """Reach the finish line and report crossing it."""
        self.at(user, seconds or self.seconds_for(RACE_COURSE_METRES))
        client.post(self.urls()['progress'], {'distance': RACE_COURSE_METRES})
        response = client.post(self.urls()['complete'], {'finish': 1})
        user.refresh_from_db()
        return response

    def full_race(self, pc_no, collisions=0, seconds=None):
        """Start, drive the whole course, finish. The happy path, end to end."""
        user, client = self.make_player(pc_no)
        self.start_race(client, user)
        self.drive_course(client, user, collisions=collisions)
        response = self.cross_finish(client, user, seconds)
        return user, client, response


# ==========================================================================
# The clock belongs to the server
# ==========================================================================

class RaceTimerTests(RaceMixin, TestCase):
    def setUp(self):
        self.user, self.client_ = self.make_player('PC-TIMER')

    def test_the_briefing_page_does_not_start_the_clock(self):
        """Reading about the broken site is free."""
        response = self.client_.get(self.urls()['home'])
        self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_started_at)
        self.assertIsNone(self.user.game_start_time)
        self.assertEqual(self.user.race_status, RACE_NOT_STARTED)

    def test_looking_at_the_broken_website_does_not_start_the_clock(self):
        self.assertEqual(self.client_.get(self.urls()['broken']).status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_started_at)

    def test_reading_the_race_state_does_not_start_the_clock(self):
        state = self.client_.get(self.urls()['state']).json()
        self.assertEqual(state['status'], RACE_NOT_STARTED)
        self.assertFalse(state['started'])
        self.assertEqual(state['remaining'], GAME_DURATION_SECONDS)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_started_at)

    def test_logging_in_and_walking_through_every_page_starts_nothing(self):
        """Only one request in the whole application starts a race."""
        for url in (reverse('intro'), reverse('start'), self.urls()['home'],
                    self.urls()['broken'], self.urls()['state'], self.urls()['preview'],
                    self.urls()['result'], self.urls()['fixed']):
            self.client_.get(url)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_started_at)

    def test_start_race_starts_the_server_side_timer(self):
        before = timezone.now()
        data = self.start_race(self.client_, self.user)
        after = timezone.now()

        self.assertEqual(data['status'], RACE_ACTIVE)
        self.assertTrue(data['started'])
        self.assertEqual(data['duration'], GAME_DURATION_SECONDS)
        self.assertGreater(data['remaining'], GAME_DURATION_SECONDS - 10)

        self.assertIsNotNone(self.user.race_started_at)
        self.assertLessEqual(before, self.user.race_started_at)
        self.assertLessEqual(self.user.race_started_at, after)

    def test_the_start_timestamp_is_the_servers_not_the_browsers(self):
        """A POST claiming its own start time is ignored entirely."""
        forged = timezone.now() - timedelta(minutes=30)
        self.client_.post(self.urls()['start'], {
            'race_started_at': forged.isoformat(),
            'started_at': forged.isoformat(),
            'remaining': 99999,
            'elapsed': 0,
        })

        self.user.refresh_from_db()
        self.assertGreater(self.user.race_started_at, forged)
        self.assertLessEqual(self.user.remaining_seconds, GAME_DURATION_SECONDS)

    def test_the_duration_is_exactly_twelve_minutes(self):
        self.assertEqual(GAME_DURATION_SECONDS, 720)
        data = self.start_race(self.client_, self.user)
        self.assertEqual(data['duration'], 720)

        self.at(self.user, 719)
        self.assertFalse(self.user.is_expired)
        self.at(self.user, 720)
        self.assertTrue(self.user.is_expired)

    def test_refreshing_does_not_restart_the_timer(self):
        self.start_race(self.client_, self.user)
        self.at(self.user, 400)
        started = self.user.race_started_at

        for _ in range(3):
            self.client_.get(self.urls()['home'])
            data = self.client_.post(self.urls()['start'], {}).json()
            self.assertTrue(data['resumed'])

        self.user.refresh_from_db()
        self.assertEqual(self.user.race_started_at, started)
        self.assertLessEqual(self.user.remaining_seconds, GAME_DURATION_SECONDS - 400)

    def test_a_refresh_mid_race_keeps_the_progress_already_earned(self):
        self.start_race(self.client_, self.user)
        self.drive_course(self.client_, self.user, repairs=3, collisions=2)

        resumed = self.client_.post(self.urls()['start'], {}).json()
        self.assertTrue(resumed['resumed'])
        self.assertEqual(resumed['repairsCollected'], 3)
        self.assertEqual(resumed['repairs'], list(REPAIR_IDS[:3]))
        self.assertEqual(resumed['collisions'], 2)
        self.assertGreaterEqual(resumed['distance'], RACE_REPAIR_METRES[2])

    def test_a_second_tab_cannot_buy_a_fresh_twelve_minutes(self):
        self.start_race(self.client_, self.user)
        self.at(self.user, 600)

        second_tab = Client()
        second_tab.force_login(self.user, backend='first.backends.PCNoBackend')
        data = second_tab.post(self.urls()['start'], {}).json()

        self.assertTrue(data['resumed'])
        self.assertLessEqual(data['remaining'], GAME_DURATION_SECONDS - 600)

    def test_the_race_expires_after_twelve_minutes(self):
        self.start_race(self.client_, self.user)
        self.time_out(self.user)

        state = self.client_.get(self.urls()['state']).json()
        self.assertEqual(state['status'], RACE_EXPIRED)
        self.assertTrue(state['expired'])
        self.assertEqual(state['remaining'], 0)

    def test_a_forged_clock_cannot_buy_more_time(self):
        """The browser may claim anything; the server reads its own clock."""
        self.start_race(self.client_, self.user)
        self.drive_course(self.client_, self.user)
        self.time_out(self.user)

        for payload in ({'remaining': 999}, {'elapsed': 1}, {'expired': 'false'},
                        {'duration': 99999}, {'finish': 1, 'remaining': 700}):
            response = self.client_.post(self.urls()['complete'], payload)
            self.assertEqual(response.status_code, 409, payload)
            self.assertTrue(response.json()['expired'])
            self.assertEqual(response.json()['remaining'], 0)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_completed_at)

    def test_the_timeout_records_an_ineligible_entry_for_the_organisers(self):
        self.start_race(self.client_, self.user)
        self.drive_course(self.client_, self.user, repairs=4)
        self.time_out(self.user)
        self.client_.get(self.urls()['state'])

        final = FinalSubmission.objects.get(user=self.user)
        self.assertFalse(final.eligible)
        self.assertFalse(final.reached_all)
        self.assertEqual(final.status, 'Expired')


# ==========================================================================
# One participant, one official attempt
# ==========================================================================

class OneAttemptTests(RaceMixin, TestCase):
    def test_a_timed_out_attempt_cannot_be_restarted(self):
        user, client = self.make_player('PC-ONCE')
        self.start_race(client, user)
        self.time_out(user)
        started = user.race_started_at

        for _ in range(3):
            response = client.post(self.urls()['start'], {})
            self.assertEqual(response.status_code, 409)
            self.assertIn('ended', response.json()['error'])

        user.refresh_from_db()
        self.assertEqual(user.race_started_at, started)
        self.assertEqual(user.race_status, RACE_EXPIRED)

    def test_the_briefing_offers_no_restart_after_a_timeout(self):
        user, client = self.make_player('PC-ONCE2')
        self.start_race(client, user)
        self.time_out(user)

        page = client.get(self.urls()['home']).content.decode()
        self.assertIn("TIME'S UP!", page)
        self.assertIn('Your race attempt has ended', page)
        self.assertNotIn('id="wf-start-race"', page)
        self.assertNotIn('RESTART', page.upper())

    def test_logging_out_and_back_in_does_not_grant_a_new_attempt(self):
        user, client = self.make_player('PC-ONCE3')
        self.start_race(client, user)
        self.at(user, 300)
        client.get(reverse('logout'))

        client.force_login(user, backend='first.backends.PCNoBackend')
        data = client.post(self.urls()['start'], {}).json()

        self.assertTrue(data['resumed'])
        self.assertLessEqual(data['remaining'], GAME_DURATION_SECONDS - 300)

    def test_a_finished_attempt_cannot_be_started_again(self):
        user, client, response = self.full_race('PC-ONCE4')
        self.assertEqual(response.status_code, 200)

        again = client.post(self.urls()['start'], {})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()['redirect'], self.urls()['result'])

        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_COMPLETED)

    def test_a_finished_attempt_cannot_be_completed_twice(self):
        user, client, _first = self.full_race('PC-ONCE5', collisions=2)
        recorded = (user.race_completed_at, user.best_score,
                    user.race_time_seconds, user.race_collisions)

        for _ in range(3):
            again = client.post(self.urls()['complete'], {'finish': 1, 'collisions': 0})
            self.assertEqual(again.status_code, 409)

        user.refresh_from_db()
        self.assertEqual(
            (user.race_completed_at, user.best_score,
             user.race_time_seconds, user.race_collisions), recorded)
        self.assertEqual(FinalSubmission.objects.filter(user=user).count(), 1)

    def test_a_finished_race_never_becomes_expired(self):
        """Terminal means terminal: twelve minutes later it is still finished."""
        user, client, _response = self.full_race('PC-ONCE6')
        self.at(user, GAME_DURATION_SECONDS + 600)

        self.assertFalse(user.is_expired)
        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertEqual(client.get(self.urls()['state']).json()['status'], RACE_COMPLETED)

    def test_an_expired_race_can_never_be_completed(self):
        user, client = self.make_player('PC-ONCE7')
        self.start_race(client, user)
        self.drive_course(client, user)
        self.time_out(user)

        response = client.post(self.urls()['complete'], {'finish': 1})
        self.assertEqual(response.status_code, 409)

        user.refresh_from_db()
        self.assertIsNone(user.race_completed_at)
        self.assertEqual(user.race_status, RACE_EXPIRED)

    def test_progress_is_refused_once_the_attempt_is_over(self):
        expired, expired_client = self.make_player('PC-ONCE8')
        self.start_race(expired_client, expired)
        self.time_out(expired)
        for payload in ({'distance': 500}, {'collisions': 1},
                        {'repair': REPAIR_IDS[0]}):
            self.assertEqual(
                expired_client.post(self.urls()['progress'], payload).status_code, 409)

        done, done_client, _response = self.full_race('PC-ONCE9')
        after = done_client.post(self.urls()['progress'], {'collisions': 5})
        self.assertEqual(after.status_code, 409)
        done.refresh_from_db()
        self.assertEqual(done.race_collisions, 0)


# ==========================================================================
# Driving: distance is the thing the server can actually check
# ==========================================================================

class DistanceTests(RaceMixin, TestCase):
    def setUp(self):
        self.user, self.client_ = self.make_player('PC-DIST')
        self.start_race(self.client_, self.user)

    def report(self, **payload):
        return self.client_.post(self.urls()['progress'], payload)

    def test_distance_is_recorded_as_the_car_reports_it(self):
        self.drive_to(self.client_, self.user, 900)
        self.assertEqual(self.user.race_distance, 900)
        self.assertEqual(self.report().json()['distance'], 900)

    def test_a_car_cannot_have_travelled_further_than_it_could_drive(self):
        """The classic teleport: claim the finish line one second in."""
        self.at(self.user, 2)
        self.report(distance=RACE_COURSE_METRES)

        self.user.refresh_from_db()
        possible = 2 * RACE_TOP_SPEED * 1.08 + 120
        self.assertLessEqual(self.user.race_distance, possible)
        self.assertLess(self.user.race_distance, RACE_COURSE_METRES)

    def test_the_whole_course_cannot_be_driven_faster_than_physics_allows(self):
        for second in range(1, RACE_MIN_SECONDS, 15):
            self.at(self.user, second)
            self.report(distance=RACE_COURSE_METRES)
        self.user.refresh_from_db()
        self.assertLess(self.user.race_distance, RACE_COURSE_METRES,
                        'the course cannot be finished before it can be driven')

    def test_distance_never_goes_backwards(self):
        self.drive_to(self.client_, self.user, 4000)
        self.report(distance=10)
        self.user.refresh_from_db()
        self.assertEqual(self.user.race_distance, 4000)

    def test_a_negative_or_nonsense_distance_is_refused(self):
        self.drive_to(self.client_, self.user, 2000)
        for value in (-1, -5000, 'far', '1e999'):
            self.assertEqual(self.report(distance=value).status_code, 400, value)
        self.user.refresh_from_db()
        self.assertEqual(self.user.race_distance, 2000)

    def test_the_section_follows_the_distance(self):
        for index in range(REPAIR_COUNT):
            self.drive_to(self.client_, self.user,
                          int((index + 0.5) * RACE_SECTION_METRES))
            self.assertEqual(self.report().json()['section'], index)


# ==========================================================================
# The CSS repairs: the core mechanic
# ==========================================================================

class RepairCollectionTests(RaceMixin, TestCase):
    def setUp(self):
        self.user, self.client_ = self.make_player('PC-REPAIR')
        self.start_race(self.client_, self.user)

    def repair(self, repair_id, **extra):
        return self.client_.post(self.urls()['progress'],
                                 {'repair': repair_id, **extra})

    def test_collecting_a_repair_records_it_against_the_participant(self):
        response = self.collect(self.client_, self.user, 0)
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['counted'])
        self.assertEqual(data['repairs'], [REPAIR_IDS[0]])
        self.assertEqual(data['repairsCollected'], 1)
        self.assertEqual(data['repair']['label'], 'RESPONSIVE')
        self.assertEqual(self.user.repair_ids, [REPAIR_IDS[0]])

    def test_every_repair_is_collected_in_course_order(self):
        for index in range(REPAIR_COUNT):
            self.collect(self.client_, self.user, index)
        self.assertEqual(self.user.repair_ids, list(REPAIR_IDS))
        self.assertEqual(self.user.repairs_collected, REPAIR_COUNT)
        self.assertEqual(self.user.race_obstacles, REPAIR_COUNT)

    def test_a_repair_cannot_be_collected_before_the_car_reaches_it(self):
        self.at(self.user, 30)
        response = self.repair(REPAIR_IDS[0])

        self.assertEqual(response.status_code, 400)
        self.assertIn('further up the course', response.json()['error'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.repair_ids, [])

    def test_repairs_cannot_be_collected_out_of_order(self):
        """Driving to the last one does not let you skip the six before it."""
        self.drive_to(self.client_, self.user, RACE_REPAIR_METRES[-1])
        response = self.repair(REPAIR_IDS[-1])

        self.assertEqual(response.status_code, 400)
        self.assertIn('in course order', response.json()['error'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.repair_ids, [])

    def test_the_same_repair_cannot_be_collected_twice(self):
        self.collect(self.client_, self.user, 0)
        for _ in range(5):
            response = self.repair(REPAIR_IDS[0])
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['counted'])

        self.user.refresh_from_db()
        self.assertEqual(self.user.repair_ids, [REPAIR_IDS[0]])
        self.assertEqual(self.user.race_repairs.count(REPAIR_IDS[0]), 1)

    def test_an_invalid_repair_id_is_refused(self):
        self.drive_to(self.client_, self.user, RACE_COURSE_METRES // 2)
        for value in ('flexbox-pro', 'MARGIN', '1', 'margin,padding', 'sql'):
            response = self.repair(value)
            self.assertEqual(response.status_code, 400, value)
            self.assertIn('error', response.json())

        self.user.refresh_from_db()
        self.assertEqual(self.user.repair_ids, [])

    def test_a_repair_cannot_be_collected_before_the_race_starts(self):
        stranger, client = self.make_player('PC-REPAIR2')
        response = client.post(self.urls()['progress'], {'repair': REPAIR_IDS[0]})

        self.assertEqual(response.status_code, 400)
        stranger.refresh_from_db()
        self.assertEqual(stranger.repair_ids, [])

    def test_the_full_set_takes_at_least_as_long_as_driving_to_them(self):
        for index in range(REPAIR_COUNT):
            self.collect(self.client_, self.user, index)
        self.user.refresh_from_db()
        self.assertGreaterEqual(self.user.elapsed_seconds,
                                self.seconds_for(RACE_REPAIR_METRES[-1]) - 1)


class LiveRepairPreviewTests(RaceMixin, TestCase):
    """The panel beside the track shows the site the *server* has rebuilt."""

    def setUp(self):
        self.user, self.client_ = self.make_player('PC-PREVIEW')
        self.start_race(self.client_, self.user)

    def preview(self):
        response = self.client_.get(self.urls()['preview'])
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_it_starts_as_the_broken_website(self):
        body = self.preview()
        self.assertIn('<h1 class="hero__title">', body)
        self.assertIn('font-size: 1rem;', body)              # the defect
        self.assertIn('@media (min-width: 860px)', body)     # the defect
        self.assertIn('.navbar {\n  display: block;', body)  # the defect
        self.assertNotIn('.features__grid {\n  display: grid;\n'
                         '  grid-template-columns: repeat(3, 1fr);', body)

    def test_each_repair_changes_the_stylesheet_it_serves(self):
        seen = [self.preview()]
        for index in range(REPAIR_COUNT):
            self.collect(self.client_, self.user, index)
            body = self.preview()
            self.assertNotIn(body, seen, f'{REPAIR_IDS[index]} changed nothing')
            seen.append(body)

    def test_a_collected_repair_shows_its_own_fix(self):
        self.collect(self.client_, self.user, 0)               # RESPONSIVE
        self.assertIn('@media (max-width: 860px)', self.preview())

        self.collect(self.client_, self.user, 1)               # DISPLAY
        self.assertIn('.navbar {\n  display: flex;', self.preview())

        self.collect(self.client_, self.user, 2)               # MARGIN
        self.assertIn('gap: 16px;', self.preview())

    def test_the_full_set_serves_the_finished_website(self):
        for index in range(REPAIR_COUNT):
            self.collect(self.client_, self.user, index)
        body = self.preview()

        # Byte-for-byte the finished stylesheet, its banner comment aside.
        self.assertTrue(is_fully_repaired(repair_css(self.user.repair_ids)))
        self.assertNotIn(STARTER_CSS, body)
        for correct in ('line-height: 1.6;', '@media (max-width: 860px)',
                        '.navbar {\n  display: flex;',
                        'font-size: clamp(2.4rem, 4.4vw, 3.6rem);'):
            self.assertIn(correct, body, correct)

    def test_it_only_shows_repairs_the_server_recorded(self):
        """A repair the server refused is not in the preview either."""
        self.drive_to(self.client_, self.user, RACE_REPAIR_METRES[-1])
        self.client_.post(self.urls()['progress'], {'repair': REPAIR_IDS[-1]})

        self.user.refresh_from_db()
        self.assertEqual(self.user.repair_ids, [])
        self.assertIn('@media (min-width: 860px)', self.preview())

    def test_a_timeout_puts_the_broken_website_back(self):
        self.drive_course(self.client_, self.user, repairs=5)
        self.assertIn('.navbar {\n  display: flex;', self.preview())

        self.time_out(self.user)
        body = self.preview()
        self.assertIn('.navbar {\n  display: block;', body)
        self.assertNotIn(SOLUTION_CSS, body)

    def test_one_participants_repairs_do_not_leak_into_anothers_preview(self):
        self.drive_course(self.client_, self.user, repairs=4)

        other, other_client = self.make_player('PC-PREVIEW2')
        self.start_race(other_client, other)
        body = other_client.get(self.urls()['preview']).content.decode()

        self.assertIn('@media (min-width: 860px)', body)
        self.assertNotIn('.navbar {\n  display: flex;', body)

    def test_it_requires_authentication(self):
        self.client_.logout()
        self.assertEqual(self.client_.get(self.urls()['preview']).status_code, 302)

    def test_it_is_framed_safely_and_never_cached(self):
        response = self.client_.get(self.urls()['preview'])
        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn('<script', response.content.decode())


# ==========================================================================
# The finish line
# ==========================================================================

class FinishLineTests(RaceMixin, TestCase):
    def setUp(self):
        self.user, self.client_ = self.make_player('PC-FINISH')
        self.start_race(self.client_, self.user)

    def complete(self, **extra):
        return self.client_.post(self.urls()['complete'], {'finish': 1, **extra})

    def test_completing_without_starting_is_rejected(self):
        stranger, client = self.make_player('PC-NOSTART')
        response = client.post(self.urls()['complete'], {'finish': 1})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Race has not started.')
        stranger.refresh_from_db()
        self.assertIsNone(stranger.race_completed_at)

    def test_every_repair_is_not_enough_without_the_finish_line(self):
        """The car has to actually get there."""
        self.drive_course(self.client_, self.user)
        response = self.complete()

        self.assertEqual(response.status_code, 400)
        self.assertIn('finish line', response.json()['error'])
        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_completed_at)

    def test_the_finish_line_is_not_enough_without_every_repair(self):
        self.drive_course(self.client_, self.user, repairs=REPAIR_COUNT - 1)
        self.at(self.user, self.seconds_for(RACE_COURSE_METRES))
        self.client_.post(self.urls()['progress'], {'distance': RACE_COURSE_METRES})

        response = self.complete()
        self.assertEqual(response.status_code, 400)
        self.assertIn('1 more CSS repair', response.json()['error'])

        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_completed_at)

    def test_time_alone_never_finishes_the_race(self):
        """Sitting still for eleven minutes completes nothing."""
        self.at(self.user, GAME_DURATION_SECONDS - 30)
        response = self.complete()

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_completed_at)

    def test_both_conditions_together_finish_the_race(self):
        self.drive_course(self.client_, self.user)
        response = self.cross_finish(self.client_, self.user)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.user.race_status, RACE_COMPLETED)
        self.assertEqual(self.user.repairs_collected, REPAIR_COUNT)

    def test_nobody_can_finish_faster_than_the_course_can_be_driven(self):
        """Collect everything, then try to be at the line before you could be."""
        self.drive_course(self.client_, self.user)

        self.at(self.user, RACE_MIN_SECONDS - 40)
        self.client_.post(self.urls()['progress'], {'distance': RACE_COURSE_METRES})
        self.user.refresh_from_db()
        self.assertLess(self.user.race_distance, RACE_COURSE_METRES)
        self.assertEqual(self.complete().status_code, 400)

        # ...and once the time really has passed, the same request is fine.
        response = self.cross_finish(self.client_, self.user)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(self.user.race_time_seconds, RACE_MIN_SECONDS)


# ==========================================================================
# The score is the server's, and only the server's
# ==========================================================================

class RaceScoringTests(RaceMixin, TestCase):
    def test_the_score_is_calculated_by_the_server(self):
        user, _client, response = self.full_race('PC-SCORE1')
        expected = race_score(user.race_time_seconds, REPAIR_COUNT, 0)

        self.assertEqual(user.best_score, expected)
        self.assertEqual(response.json()['score'], expected)
        self.assertEqual(expected, min(RACE_MAX_SCORE, (
            RACE_BASE_POINTS
            + int(RACE_TIME_POINTS * (GAME_DURATION_SECONDS - user.race_time_seconds)
                  / (GAME_DURATION_SECONDS - RACE_MIN_SECONDS))
            + REPAIR_COUNT * RACE_REPAIR_POINTS
            + RACE_CLEAN_RUN_BONUS)))

    def test_a_perfect_run_scores_the_maximum_and_never_more(self):
        user, _client, _response = self.full_race('PC-SCORE2', seconds=RACE_MIN_SECONDS)
        self.assertEqual(user.best_score, RACE_MAX_SCORE)

    def test_a_forged_score_in_the_completion_request_is_ignored(self):
        user, client = self.make_player('PC-SCORE3')
        self.start_race(client, user)
        self.drive_course(client, user, collisions=4)
        self.at(user, self.seconds_for(RACE_COURSE_METRES))
        client.post(self.urls()['progress'], {'distance': RACE_COURSE_METRES})

        client.post(self.urls()['complete'], {
            'finish': 1, 'score': 1000, 'best_score': 1000, 'repairs': 7,
            'collisions': 0, 'elapsed': 1, 'race_time_seconds': 1,
        })

        user.refresh_from_db()
        self.assertEqual(user.race_collisions, 4)
        self.assertEqual(user.best_score,
                         race_score(user.race_time_seconds, REPAIR_COUNT, 4))
        self.assertLess(user.best_score, RACE_MAX_SCORE)

    def test_a_forged_repair_count_cannot_finish_a_race(self):
        """The classic DevTools attack: claim a perfect run, immediately."""
        user, client = self.make_player('PC-SCORE4')
        self.start_race(client, user)

        response = client.post(self.urls()['complete'], {
            'repairs': 7, 'repairsCollected': 7, 'collisions': 0,
            'distance': RACE_COURSE_METRES, 'finish': 1,
        })

        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertIsNone(user.race_completed_at)
        self.assertEqual(user.repair_ids, [])
        self.assertEqual(user.best_score, 0)
        self.assertFalse(FinalSubmission.objects.filter(user=user).exists())

    def test_collisions_are_counted_by_the_server_and_cannot_be_undone(self):
        user, client = self.make_player('PC-SCORE5')
        self.start_race(client, user)

        for _ in range(3):
            client.post(self.urls()['progress'], {'collisions': 1})
        user.refresh_from_db()
        self.assertEqual(user.race_collisions, 3)

        for payload in ({'collisions': 0}, {'collisions': ''}, {'collisions': -5}):
            client.post(self.urls()['progress'], payload)
        user.refresh_from_db()
        self.assertEqual(user.race_collisions, 3)

    def test_a_single_report_cannot_dump_an_absurd_number_of_collisions(self):
        user, client = self.make_player('PC-SCORE6')
        self.start_race(client, user)
        client.post(self.urls()['progress'], {'collisions': 100000})

        user.refresh_from_db()
        self.assertEqual(user.race_collisions, RACE_MAX_COLLISIONS_PER_REPORT)

    def test_the_collision_counter_cannot_be_overflowed(self):
        user, client = self.make_player('PC-SCORE7')
        self.start_race(client, user)
        User.objects.filter(pk=user.pk).update(race_collisions=RACE_MAX_COLLISIONS)

        for _ in range(5):
            client.post(self.urls()['progress'], {'collisions': 30})

        user.refresh_from_db()
        self.assertEqual(user.race_collisions, RACE_MAX_COLLISIONS)

    def test_collisions_cost_points(self):
        clean, _c1, _r1 = self.full_race('PC-SCORE8', collisions=0)
        messy, _c2, _r2 = self.full_race('PC-SCORE9', collisions=3)

        self.assertGreater(clean.best_score, messy.best_score)
        self.assertEqual(clean.best_score - messy.best_score,
                         3 * RACE_COLLISION_PENALTY + RACE_CLEAN_RUN_BONUS)

    def test_driving_the_course_faster_scores_more(self):
        quick, _c1, _r1 = self.full_race('PC-SCORE10', seconds=RACE_MIN_SECONDS)
        slow, _c2, _r2 = self.full_race('PC-SCORE11', seconds=RACE_MIN_SECONDS + 300)

        self.assertGreater(quick.best_score, slow.best_score)
        self.assertEqual(slow.race_time_seconds, RACE_MIN_SECONDS + 300)

    def test_the_score_never_goes_negative_or_over_the_maximum(self):
        self.assertEqual(race_score(GAME_DURATION_SECONDS, 0, RACE_MAX_COLLISIONS), 0)
        self.assertEqual(race_score(1, REPAIR_COUNT, 0), RACE_MAX_SCORE)
        self.assertEqual(race_score(RACE_MIN_SECONDS, REPAIR_COUNT, 0), RACE_MAX_SCORE)

    def test_a_successful_completion_records_the_performance(self):
        user, _client, response = self.full_race('PC-SCORE12', collisions=2)
        data = response.json()

        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertIsNotNone(user.race_completed_at)
        self.assertEqual(user.repairs_collected, REPAIR_COUNT)
        self.assertEqual(user.race_collisions, 2)
        self.assertEqual(user.race_distance, RACE_COURSE_METRES)
        self.assertGreater(user.race_time_seconds, 0)
        self.assertGreater(user.best_score, 0)
        self.assertEqual(data['redirect'], self.urls()['result'])

        final = FinalSubmission.objects.get(user=user)
        self.assertTrue(final.eligible)
        self.assertTrue(final.reached_all)
        self.assertEqual(final.score, user.best_score)
        self.assertEqual(final.total, RACE_MAX_SCORE)

    def test_the_live_score_comes_from_the_server_while_racing(self):
        """The HUD's running score is the server's arithmetic, not the page's."""
        user, client = self.make_player('PC-SCORE13')
        self.start_race(client, user)

        state = client.get(self.urls()['state']).json()
        self.assertEqual(state['score'], race_score(state['elapsed'], 0, 0))

        self.drive_course(client, user, repairs=3, collisions=2)
        state = client.get(self.urls()['state']).json()
        self.assertEqual(state['score'], race_score(state['elapsed'], 3, 2))
        self.assertEqual(state['repairsCollected'], 3)

    def test_the_result_page_reports_the_performance_without_naming_a_winner(self):
        user, client, _response = self.full_race('PC-SCORE14', collisions=1)
        page = client.get(self.urls()['result']).content.decode()

        self.assertIn('CHALLENGE COMPLETE', page)
        self.assertIn('CSS FIX', page)
        self.assertIn(str(user.best_score), page)
        self.assertIn('one overall winner', page)
        for claim in ('YOU ARE THE WINNER', 'YOU WON', '1ST PLACE', 'RANK'):
            self.assertNotIn(claim, page.upper(), claim)

    def test_the_result_page_lists_every_repair_collected(self):
        _user, client, _response = self.full_race('PC-SCORE15')
        page = client.get(self.urls()['result']).content.decode()
        for label in ('RESPONSIVE', 'DISPLAY', 'MARGIN', 'PADDING',
                      'FLEXBOX', 'POSITION', 'GRID'):
            self.assertIn(label, page, label)

    def test_the_result_page_is_closed_until_the_race_is_finished(self):
        _user, client = self.make_player('PC-SCORE16')
        self.assertRedirects(client.get(self.urls()['result']), self.urls()['home'])


# ==========================================================================
# The reward
# ==========================================================================

class FixedWebsiteTests(RaceMixin, TestCase):
    def test_the_fixed_website_is_locked_before_the_race(self):
        _user, client = self.make_player('PC-REWARD1')
        self.assertRedirects(client.get(self.urls()['fixed']), self.urls()['home'])

    def test_the_fixed_website_is_locked_during_the_race(self):
        user, client = self.make_player('PC-REWARD2')
        self.start_race(client, user)
        self.drive_course(client, user)
        self.assertRedirects(client.get(self.urls()['fixed']), self.urls()['home'])

    def test_a_timeout_does_not_unlock_the_fixed_website(self):
        user, client = self.make_player('PC-REWARD3')
        self.start_race(client, user)
        self.drive_course(client, user)
        self.time_out(user)

        response = client.get(self.urls()['fixed'])
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('line-height: 1.6;', str(response.content))

    def test_crossing_the_finish_line_unlocks_the_fixed_website(self):
        _user, client, _response = self.full_race('PC-REWARD4')

        response = client.get(self.urls()['fixed'])
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        # the existing finished page: the challenge markup + solution.css
        self.assertIn('<h1 class="hero__title">', body)
        self.assertIn('line-height: 1.6;', body)
        self.assertIn('grid-template-columns: repeat(3, 1fr);', body)
        self.assertIn('@media (max-width: 860px)', body)
        self.assertNotIn('href="style.css"', body)

    def test_the_reward_is_what_the_repairs_composed(self):
        """The site handed over is the one the participant actually rebuilt."""
        user, client, _response = self.full_race('PC-REWARD5')
        body = client.get(self.urls()['fixed']).content.decode()

        self.assertTrue(is_fully_repaired(repair_css(user.repair_ids)),
                        'the collected repairs must compose the finished site')
        self.assertIn(SOLUTION_CSS, body)

    def test_the_reward_is_framed_safely(self):
        _user, client, _response = self.full_race('PC-REWARD6')
        response = client.get(self.urls()['fixed'])

        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn('<script', response.content.decode())
        self.assertEqual(client.get(self.urls()['home'])['X-Frame-Options'], 'DENY')

    def test_the_result_page_links_to_the_existing_fixed_website(self):
        _user, client, _response = self.full_race('PC-REWARD7')
        page = client.get(self.urls()['result']).content.decode()

        self.assertIn(self.urls()['fixed'], page)
        self.assertIn('sandbox', page)
        self.assertNotIn(SOLUTION_CSS, page)

    def test_the_fixed_website_requires_authentication(self):
        _user, client, _response = self.full_race('PC-REWARD8')
        client.get(reverse('logout'))
        self.assertEqual(client.get(self.urls()['fixed']).status_code, 302)

    def test_one_participants_completion_does_not_unlock_it_for_another(self):
        self.full_race('PC-REWARD9')
        _other, other_client = self.make_player('PC-REWARD10')
        self.assertRedirects(other_client.get(self.urls()['fixed']), self.urls()['home'])


# ==========================================================================
# Authentication, ownership and direct endpoint access
# ==========================================================================

class RaceSecurityTests(RaceMixin, TestCase):
    def test_every_race_endpoint_refuses_anonymous_requests(self):
        anonymous = Client()
        for name in ('start', 'progress', 'complete'):
            response = anonymous.post(self.urls()[name], {})
            self.assertEqual(response.status_code, 403, name)
        self.assertEqual(anonymous.get(self.urls()['state']).status_code, 403)

        for name in ('home', 'result', 'fixed', 'broken', 'preview'):
            self.assertEqual(anonymous.get(self.urls()[name]).status_code, 302, name)

    def test_the_write_endpoints_refuse_get(self):
        _user, client = self.make_player('PC-SEC1')
        for name in ('start', 'progress', 'complete'):
            self.assertEqual(client.get(self.urls()[name]).status_code, 405, name)

    def test_the_write_endpoints_require_a_csrf_token(self):
        user, _client = self.make_player('PC-SEC2')
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(user, backend='first.backends.PCNoBackend')

        for name in ('start', 'progress', 'complete'):
            self.assertEqual(strict.post(self.urls()[name], {}).status_code, 403, name)

        user.refresh_from_db()
        self.assertIsNone(user.race_started_at)

    def test_a_race_belongs_to_the_account_that_started_it(self):
        mine, my_client = self.make_player('PC-SEC3')
        self.start_race(my_client, mine)
        self.drive_course(my_client, mine)

        theirs, their_client = self.make_player('PC-SEC4')
        self.start_race(their_client, theirs)

        # their completion cannot ride on my repairs
        response = their_client.post(self.urls()['complete'], {'finish': 1})
        self.assertEqual(response.status_code, 400)

        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(theirs.repair_ids, [])
        self.assertEqual(mine.repairs_collected, REPAIR_COUNT)
        self.assertIsNone(theirs.race_completed_at)

    def test_progress_only_ever_touches_the_participant_who_sent_it(self):
        neighbour, _client = self.make_player('PC-SEC5')
        mine, my_client = self.make_player('PC-SEC6')
        self.start_race(my_client, mine)
        self.drive_course(my_client, mine, repairs=4, collisions=3)

        neighbour.refresh_from_db()
        self.assertIsNone(neighbour.race_started_at)
        self.assertEqual(neighbour.race_distance, 0)
        self.assertEqual(neighbour.race_collisions, 0)
        self.assertEqual(neighbour.repair_ids, [])

    def test_a_new_participant_inherits_nothing(self):
        self.full_race('PC-SEC7', collisions=5)
        fresh, fresh_client = self.make_player('PC-SEC8')

        state = fresh_client.get(self.urls()['state']).json()
        self.assertEqual(state['status'], RACE_NOT_STARTED)
        self.assertEqual(state['repairs'], [])
        self.assertEqual(state['distance'], 0)
        self.assertEqual(state['collisions'], 0)
        self.assertEqual(state['score'], 0)
        self.assertEqual(state['remaining'], GAME_DURATION_SECONDS)


class AuthenticationTests(RaceMixin, TestCase):
    """The PC-number sign-in the event runs on, unchanged by the race."""

    def test_a_participant_can_sign_up_and_lands_on_the_launch_screen(self):
        client = Client()
        response = client.post(reverse('signup'), {
            'username': 'Ada', 'pc_no': 'PC-AUTH1', 'password': 'pw-123456'})

        self.assertRedirects(response, reverse('start'))
        self.assertTrue(User.objects.filter(pc_no='PC-AUTH1').exists())
        self.assertIsNone(User.objects.get(pc_no='PC-AUTH1').race_started_at)

    def test_a_duplicate_pc_number_is_refused(self):
        User.objects.create_user(username='First', pc_no='PC-AUTH2', password='pw-123456')
        client = Client()
        response = client.post(reverse('signup'), {
            'username': 'Second', 'pc_no': 'PC-AUTH2', 'password': 'pw-123456'})

        self.assertEqual(response.status_code, 200)
        self.assertIn('already registered', response.content.decode())
        self.assertEqual(User.objects.filter(pc_no='PC-AUTH2').count(), 1)

    def test_log_in_log_out_and_log_back_in(self):
        User.objects.create_user(username='Ada', pc_no='PC-AUTH3', password='pw-123456')
        client = Client()

        self.assertEqual(
            client.post(reverse('login'), {'pc_no': 'PC-AUTH3', 'password': 'wrong'}
                        ).status_code, 200)
        self.assertNotIn('_auth_user_id', client.session)

        self.assertRedirects(
            client.post(reverse('login'), {'pc_no': 'PC-AUTH3', 'password': 'pw-123456'}),
            reverse('start'))
        self.assertIn('_auth_user_id', client.session)

        self.assertRedirects(client.get(reverse('logout')), reverse('login'))
        self.assertNotIn('_auth_user_id', client.session)

    def test_the_game_pages_require_a_signed_in_participant(self):
        anonymous = Client()
        for name in ('start', 'home', 'race_result'):
            self.assertEqual(anonymous.get(reverse(name)).status_code, 302, name)

    def test_logging_out_keeps_the_recorded_performance(self):
        user, client, _response = self.full_race('PC-AUTH4', collisions=2)
        client.get(reverse('logout'))

        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertEqual(user.race_collisions, 2)
        self.assertEqual(user.repairs_collected, REPAIR_COUNT)
        self.assertTrue(FinalSubmission.objects.get(user=user).eligible)


class RetiredEndpointTests(TestCase):
    """The CSS editor is gone: the participant races now, they do not type CSS.

    Its endpoints wrote to the very fields the race scores — `best_score`,
    `completed_at` — so leaving them reachable would have let a signed-in
    participant award themselves a completion without racing. The grading
    code, the hint text and the stored history they produced are all still
    here; only the player-facing write endpoints were retired.
    """

    RETIRED = ('/save-css/', '/get-css/', '/api/state/', '/api/check/',
               '/api/reset/', '/api/hint/')

    def test_the_editor_endpoints_are_no_longer_routed(self):
        user = User.objects.create_user(
            username='Tester', pc_no='PC-RETIRED', password='pw-123456')
        client = Client()
        client.force_login(user, backend='first.backends.PCNoBackend')

        for path in self.RETIRED:
            self.assertEqual(client.post(path, {}).status_code, 404, path)
            self.assertEqual(client.get(path).status_code, 404, path)

        user.refresh_from_db()
        self.assertEqual(user.best_score, 0)
        self.assertIsNone(user.completed_at)

    def test_the_grading_infrastructure_is_still_present(self):
        from .checks import TOTAL_CHECKS, run_checks
        from .game_config import CHALLENGE_HTML

        self.assertEqual(len(run_checks(CHALLENGE_HTML, SOLUTION_CSS)), TOTAL_CHECKS)


# ==========================================================================
# What the organiser sees
# ==========================================================================

class AdminRaceDataTests(RaceMixin, TestCase):
    def setUp(self):
        self.finisher, _client, _response = self.full_race('PC-ADM-DONE', collisions=2)

        self.timed_out, timed_out_client = self.make_player('PC-ADM-OUT')
        self.start_race(timed_out_client, self.timed_out)
        self.drive_course(timed_out_client, self.timed_out, repairs=3)
        self.time_out(self.timed_out)

        self.staff = User.objects.create_superuser(
            username='organiser', pc_no='PC-ORG', password='pw-123456')
        self.admin = Client()
        self.admin.force_login(self.staff, backend='first.backends.PCNoBackend')

    def test_the_participant_list_shows_race_performance(self):
        page = self.admin.get(reverse('admin:first_user_changelist'))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()

        self.assertIn('PC-ADM-DONE', body)
        self.assertIn('PC-ADM-OUT', body)
        self.assertIn('Finished', body)
        self.assertIn('Time up', body)
        self.assertIn(f'{self.finisher.best_score}/{RACE_MAX_SCORE}', body)
        self.assertIn(f'{RACE_COURSE_METRES / 1000:.1f} km', body)   # distance driven
        self.assertIn('GRIDLOCK', body)                              # section reached

    def test_a_participants_detail_page_shows_every_recorded_figure(self):
        page = self.admin.get(
            reverse('admin:first_user_change', args=[self.finisher.pk]))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()

        for label in ('Race started at', 'Race completed at', 'Repairs collected',
                      'CSS repairs', 'Race collisions', 'Race time', 'Status',
                      'Timed out', 'Distance', 'Section'):
            self.assertIn(label, body, label)
        for repair in ('RESPONSIVE', 'FLEXBOX', 'GRID'):
            self.assertIn(repair, body, repair)
        self.assertNotIn(self.finisher.password, body)

    def test_a_partial_run_shows_how_far_it_got(self):
        page = self.admin.get(
            reverse('admin:first_user_change', args=[self.timed_out.pk]))
        body = page.content.decode()

        self.assertIn('RESPONSIVE', body)
        self.assertIn('MARGIN', body)
        self.assertNotIn('<code>GRID</code>', body)
        self.assertEqual(self.timed_out.repairs_collected, 3)

    def test_the_timed_out_attempt_is_recorded_and_marked_ineligible(self):
        self.admin.get(reverse('admin:first_user_changelist'))

        final = FinalSubmission.objects.get(user=self.timed_out)
        self.assertFalse(final.eligible)
        self.assertEqual(final.status, 'Expired')

    def test_a_walked_away_participant_is_settled_when_an_organiser_looks(self):
        """Nobody touched the browser; the entry still has to exist."""
        ghost, ghost_client = self.make_player('PC-ADM-GHOST')
        self.start_race(ghost_client, ghost)
        ghost_client.get(reverse('logout'))
        self.time_out(ghost)
        self.assertFalse(FinalSubmission.objects.filter(user=ghost).exists())

        self.admin.get(reverse('admin:first_finalsubmission_changelist'))
        self.assertTrue(FinalSubmission.objects.filter(user=ghost).exists())

    def test_settling_a_forgotten_round_is_idempotent(self):
        from .views import finalize_all_due

        live, live_client = self.make_player('PC-ADM-LIVE')
        self.start_race(live_client, live)

        finalize_all_due()
        self.assertEqual(finalize_all_due(), 0, 'a second sweep creates nothing')
        self.assertFalse(FinalSubmission.objects.filter(user=live).exists(),
                         'a running race must not be settled early')

    def test_the_admin_lists_no_automatic_placings(self):
        body = self.admin.get(reverse('admin:first_user_changelist')).content.decode()
        for word in ('1st', '2nd', '3rd', 'Winner', 'Rank'):
            self.assertNotIn(word, body, word)

    def test_the_judging_table_is_view_only(self):
        from django.contrib import admin as django_admin

        model_admin = django_admin.site._registry[FinalSubmission]
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_add_permission(None))

    def test_a_participant_cannot_reach_the_admin(self):
        _user, client = self.make_player('PC-ADM-NOPE')
        response = client.get(reverse('admin:first_user_changelist'))
        self.assertIn(response.status_code, (302, 403))

    def test_an_organiser_can_delete_a_participant_and_all_their_data(self):
        pk = self.finisher.pk
        response = self.admin.post(
            reverse('admin:first_user_delete', args=[pk]), {'post': 'yes'})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(pk=pk).exists())
        self.assertFalse(FinalSubmission.objects.filter(user_id=pk).exists())
        self.assertTrue(User.objects.filter(pc_no='PC-ADM-OUT').exists())


# ==========================================================================
# The pages the participant actually walks through
# ==========================================================================

class FreshRaceStartTests(RaceMixin, TestCase):
    """A brand new participant must get a running race, not an ended one.

    Regression cover for the bug where entering the race showed the TIME'S UP
    overlay on top of a 12:00 clock. The account was fresh and the server was
    right about that the whole time — see `HiddenPanelTests` for the half of
    it that was actually broken. These tests pin down the half that was not,
    so a future change cannot make the server the culprit instead.
    """

    def setUp(self):
        self.user, self.client_ = self.make_player('PC-FRESH')

    def test_a_fresh_participant_is_not_started_before_they_press_start(self):
        state = self.client_.get(self.urls()['state']).json()

        self.assertEqual(state['status'], RACE_NOT_STARTED)
        self.assertFalse(state['expired'])
        self.assertFalse(state['completed'])
        self.assertEqual(state['elapsed'], 0)
        self.assertEqual(state['remaining'], GAME_DURATION_SECONDS)

    def test_starting_a_fresh_race_returns_an_active_state(self):
        data = self.start_race(self.client_, self.user)

        self.assertEqual(data['status'], RACE_ACTIVE)
        self.assertTrue(data['started'])
        self.assertFalse(data['expired'], 'a race cannot start already expired')
        self.assertFalse(data['completed'])
        self.assertFalse(data['resumed'], 'this is a first attempt, not a resume')

    def test_a_fresh_race_has_the_full_twelve_minutes_on_it(self):
        data = self.start_race(self.client_, self.user)

        self.assertEqual(data['duration'], GAME_DURATION_SECONDS)
        self.assertGreater(data['remaining'], GAME_DURATION_SECONDS - 5)
        self.assertLessEqual(data['remaining'], GAME_DURATION_SECONDS)
        self.assertLess(data['elapsed'], 5)

    def test_the_race_start_timestamp_is_created_there_and_then(self):
        before = timezone.now()
        self.start_race(self.client_, self.user)

        self.assertIsNotNone(self.user.race_started_at)
        self.assertGreaterEqual(self.user.race_started_at, before)
        self.assertEqual(self.user.race_status, RACE_ACTIVE)

    def test_a_fresh_race_does_not_report_a_timeout_on_the_next_breath(self):
        """Every early call has to keep saying 'active', not 'time is up'."""
        self.start_race(self.client_, self.user)

        for _ in range(3):
            state = self.client_.get(self.urls()['state']).json()
            self.assertEqual(state['status'], RACE_ACTIVE)
            self.assertFalse(state['expired'])

        progress = self.client_.post(self.urls()['progress'], {'distance': 40})
        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.json()['status'], RACE_ACTIVE)
        self.assertFalse(progress.json()['expired'])
        self.assertFalse(FinalSubmission.objects.filter(user=self.user).exists())

    def test_the_page_hands_the_browser_an_unstarted_state_to_paint_from(self):
        """The clock the HUD shows comes from here, not from the markup."""
        page = self.client_.get(self.urls()['home']).content.decode()
        state = json.loads(page.split('id="wf-race-state" type="application/json">')[1]
                           .split('</script>')[0])

        self.assertEqual(state['status'], RACE_NOT_STARTED)
        self.assertEqual(state['remaining'], GAME_DURATION_SECONDS)
        self.assertFalse(state['expired'])
        # ...and the markup itself claims no time at all
        self.assertIn('id="wf-hud-timer">--:--<', page)
        self.assertNotIn('id="wf-hud-timer">12:00<', page)

    def test_an_expired_participant_is_never_handed_a_running_race(self):
        self.start_race(self.client_, self.user)
        self.time_out(self.user)

        page = self.client_.get(self.urls()['home']).content.decode()
        state = json.loads(page.split('id="wf-race-state" type="application/json">')[1]
                           .split('</script>')[0])

        self.assertEqual(state['status'], RACE_EXPIRED)
        self.assertEqual(state['remaining'], 0)
        self.assertTrue(state['expired'])

        # the ended panel, and no way back onto the track
        self.assertIn('id="wf-ended"', page)
        self.assertIn("TIME'S UP!", page)
        self.assertNotIn('id="wf-start-race"', page)


class HiddenPanelTests(TestCase):
    """Panels the game starts hidden must actually be hidden.

    The TIME'S UP bug was here, and it was pure cascade. The browser hides
    `[hidden]` from its *user-agent* stylesheet, and any author `display`
    declaration beats a user-agent one whatever the specificity — so
    `.wf-race { display: flex }` and `.wf-finale { display: flex }` left the
    race screen and its TIME'S UP overlay painted over the briefing before a
    race had even been started. The clock read 12:00 because that was static
    markup; the overlay read TIME'S UP because that was static markup too.

    Nothing in the Python could have caught it, so the check lives here: the
    stylesheets must carry a `[hidden]` rule strong enough to win.
    """

    CSS_DIR = Path(settings.BASE_DIR) / 'static' / 'css'
    TEMPLATE = Path(settings.BASE_DIR) / 'template' / 'entry.html'

    @classmethod
    def stylesheets(cls):
        return {path.name: path.read_text(encoding='utf-8')
                for path in cls.CSS_DIR.glob('wf-*.css')}

    @classmethod
    def hidden_elements(cls):
        """(id, [classes]) for every element the race page starts hidden."""
        markup = cls.TEMPLATE.read_text(encoding='utf-8')
        found = []
        for tag in re.findall(r'<[a-z]+\s[^>]*>', markup):
            if not re.search(r'(?:^|\s)hidden(?:[\s>=]|$)', tag):
                continue
            classes = re.search(r'class="([^"]*)"', tag)
            node_id = re.search(r'id="([^"]*)"', tag)
            if classes:
                found.append((node_id.group(1) if node_id else '?',
                              classes.group(1).split()))
        return found

    def test_the_race_page_really_does_start_things_hidden(self):
        elements = self.hidden_elements()
        self.assertGreaterEqual(len(elements), 5, 'expected several hidden panels')
        ids = {node_id for node_id, _ in elements}
        for required in ('wf-race', 'wf-finale', 'wf-countdown', 'wf-toast'):
            self.assertIn(required, ids, required)

    def test_a_hidden_guard_exists_and_can_win_the_cascade(self):
        guard = re.compile(r'\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important')
        matched = [name for name, css in self.stylesheets().items()
                   if guard.search(css)]
        self.assertTrue(
            matched,
            'no stylesheet declares [hidden] { display: none !important }, so any '
            'author `display` rule silently defeats the hidden attribute')

    def test_no_hidden_panel_is_left_visible_by_its_own_display_rule(self):
        """The check that would have failed on the bug, element by element."""
        guarded = any(
            re.search(r'\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important', css)
            for css in self.stylesheets().values())

        for node_id, classes in self.hidden_elements():
            for name in classes:
                for css in self.stylesheets().values():
                    rule = re.search(r'(?:^|[},])\s*\.' + re.escape(name)
                                     + r'\s*\{([^}]*)\}', css)
                    if rule and re.search(r'display\s*:\s*(?!none)', rule.group(1)):
                        self.assertTrue(
                            guarded,
                            f'#{node_id} is display-ed by .{name} and nothing '
                            f'restores [hidden]: it stays on screen when the '
                            f'game hides it')


class RaceFlowTests(RaceMixin, TestCase):
    def test_the_briefing_shows_the_broken_site_and_the_start_button(self):
        _user, client = self.make_player('PC-FLOW1')
        page = client.get(self.urls()['home']).content.decode()

        self.assertIn('START RACE', page)
        self.assertIn(self.urls()['broken'], page)
        self.assertIn('sandbox', page)
        self.assertIn('one official attempt', page)
        self.assertNotIn('id="wf-ended"', page)

    def test_the_briefing_lists_every_repair_on_the_course(self):
        _user, client = self.make_player('PC-FLOW2')
        page = client.get(self.urls()['home']).content.decode()
        for label in ('RESPONSIVE', 'DISPLAY', 'MARGIN', 'PADDING',
                      'FLEXBOX', 'POSITION', 'GRID'):
            self.assertIn(label, page, label)

    def test_the_broken_site_is_the_broken_stylesheet(self):
        _user, client = self.make_player('PC-FLOW3')
        body = client.get(self.urls()['broken']).content.decode()

        self.assertIn('<h1 class="hero__title">', body)
        self.assertIn('font-size: 1rem;', body)
        self.assertNotIn(SOLUTION_CSS, body)

    def test_the_briefing_never_ships_the_answers(self):
        _user, client = self.make_player('PC-FLOW4')
        page = client.get(self.urls()['home']).content.decode()

        for answer in ('grid-template-columns: repeat(3, 1fr);',
                       '@media (max-width: 860px)',
                       'font-size: clamp(2.4rem, 4.4vw, 3.6rem);'):
            self.assertNotIn(answer, page, answer)

    def test_a_race_in_progress_offers_to_resume_rather_than_restart(self):
        user, client = self.make_player('PC-FLOW5')
        self.start_race(client, user)
        self.at(user, 200)

        page = client.get(self.urls()['home']).content.decode()
        self.assertIn('RESUME RACE', page)
        self.assertIn('does not restart', page)

    def test_the_briefing_redirects_to_the_result_once_the_race_is_finished(self):
        _user, client, _response = self.full_race('PC-FLOW6')
        self.assertRedirects(client.get(self.urls()['home']), self.urls()['result'])

    def test_the_race_page_carries_the_course_the_server_defined(self):
        _user, client = self.make_player('PC-FLOW7')
        page = client.get(self.urls()['home']).content.decode()

        self.assertIn('wf-race-config', page)
        self.assertIn('wf-race-urls', page)
        self.assertIn(self.urls()['progress'], page)
        self.assertIn(self.urls()['preview'], page)
        self.assertIn(str(RACE_COURSE_METRES), page)
        self.assertIn(str(RACE_REPAIR_METRES[0]), page)
        self.assertIn('id="wf-canvas"', page)

    def test_the_race_page_carries_the_hud_the_brief_promises(self):
        _user, client = self.make_player('PC-FLOW8')
        page = client.get(self.urls()['home']).content.decode()

        for node in ('wf-hud-timer', 'wf-hud-repairs', 'wf-hud-penalties',
                     'wf-hud-score', 'wf-hud-section', 'wf-speed', 'wf-mute'):
            self.assertIn(f'id="{node}"', page, node)
        self.assertIn('accelerate', page)
        self.assertIn('steer', page)
        self.assertIn('brake', page)

    def test_the_whole_flow_end_to_end(self):
        user, client = self.make_player('PC-FLOW9')

        # intro -> start -> briefing, and nothing has begun
        self.assertEqual(client.get(reverse('intro')).status_code, 200)
        self.assertEqual(client.get(reverse('start')).status_code, 200)
        self.assertEqual(client.get(self.urls()['home']).status_code, 200)
        user.refresh_from_db()
        self.assertIsNone(user.race_started_at)

        # START RACE
        self.assertEqual(self.start_race(client, user)['status'], RACE_ACTIVE)

        # the course, one repair at a time, with the website rebuilding as we go
        for index in range(REPAIR_COUNT):
            self.collect(client, user, index)
            preview = client.get(self.urls()['preview']).content.decode()
            self.assertIn(repair_css(REPAIR_IDS[:index + 1]), preview)

        client.post(self.urls()['progress'], {'collisions': 1})
        response = self.cross_finish(client, user)
        self.assertEqual(response.status_code, 200)

        # the result, and the reward behind it
        self.assertRedirects(client.get(self.urls()['home']), self.urls()['result'])
        self.assertEqual(client.get(self.urls()['result']).status_code, 200)
        self.assertEqual(client.get(self.urls()['fixed']).status_code, 200)

        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertEqual(user.repair_ids, list(REPAIR_IDS))
        self.assertEqual(user.race_collisions, 1)
        self.assertEqual(user.best_score,
                         race_score(user.race_time_seconds, REPAIR_COUNT, 1))

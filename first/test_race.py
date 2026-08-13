"""
The race: one official attempt, a server-owned clock, and a finish line.

Every one of these tests drives the same HTTP API the browser drives, because
that is the only thing an attacker has too. The rule the whole suite is built
around is that the browser is never believed: it says what it thinks happened,
and the server decides what did.

Time is simulated by moving the server-recorded start time backwards. That is
exactly what the wall clock would have done to it, it exercises the real
`timezone.now()` comparisons rather than mocking them away, and it means no
test has to sit through twelve minutes.
"""

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .game_config import (
    GAME_DURATION_SECONDS,
    RACE_BASE_POINTS,
    RACE_CLEAN_RUN_BONUS,
    RACE_COLLISION_PENALTY,
    RACE_COURSE_SECONDS,
    RACE_MAX_COLLISIONS,
    RACE_MAX_SCORE,
    RACE_OBSTACLE_COUNT,
    RACE_OBSTACLE_POINTS,
    RACE_OBSTACLE_TIMES,
    RACE_TIME_POINTS,
    RACE_TIMING_GRACE_SECONDS,
    SOLUTION_CSS,
)
from .models import (
    RACE_ACTIVE,
    RACE_COMPLETED,
    RACE_EXPIRED,
    RACE_NOT_STARTED,
    FinalSubmission,
    User,
)
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

    def time_out(self, user):
        """Push the attempt past the twelve-minute deadline."""
        return self.at(user, GAME_DURATION_SECONDS + 5)

    def clear_obstacle(self, client, user, index):
        """Reach obstacle `index` at its earliest honest moment and clear it."""
        self.at(user, RACE_OBSTACLE_TIMES[index - 1])
        return client.post(self.urls()['progress'], {'obstacle': index})

    def clear_course(self, client, user, obstacles=RACE_OBSTACLE_COUNT, collisions=0):
        """Drive the whole course. Returns nothing; the race is left unfinished."""
        for index in range(1, obstacles + 1):
            response = self.clear_obstacle(client, user, index)
            self.assertEqual(response.status_code, 200, response.content)
        for _ in range(collisions):
            self.assertEqual(
                client.post(self.urls()['progress'], {'collision': 1}).status_code, 200)
        user.refresh_from_db()

    def finish(self, client, user, at_second=RACE_COURSE_SECONDS):
        """Reach the finish line and report crossing it."""
        self.at(user, at_second)
        return client.post(self.urls()['complete'], {'finish': 1})

    def full_race(self, pc_no, collisions=0, finish_at=RACE_COURSE_SECONDS):
        """Start, drive, finish. The happy path, end to end."""
        user, client = self.make_player(pc_no)
        self.start_race(client, user)
        self.clear_course(client, user, collisions=collisions)
        response = self.finish(client, user, finish_at)
        user.refresh_from_db()
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
        """Only one button in the whole application starts a race."""
        for url in (reverse('intro'), reverse('start'), self.urls()['home'],
                    self.urls()['broken'], self.urls()['state'],
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
        user, client, first = self.full_race('PC-ONCE5', collisions=2)
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
        user, client, _ = self.full_race('PC-ONCE6')
        self.at(user, GAME_DURATION_SECONDS + 600)

        self.assertFalse(user.is_expired)
        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertEqual(client.get(self.urls()['state']).json()['status'], RACE_COMPLETED)

    def test_an_expired_race_can_never_be_completed(self):
        user, client = self.make_player('PC-ONCE7')
        self.start_race(client, user)
        self.clear_course(client, user)
        self.time_out(user)

        response = client.post(self.urls()['complete'], {'finish': 1})
        self.assertEqual(response.status_code, 409)

        user.refresh_from_db()
        self.assertIsNone(user.race_completed_at)
        self.assertEqual(user.race_status, RACE_EXPIRED)


# ==========================================================================
# Obstacles and the finish line
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

    def test_clearing_six_obstacles_is_not_enough_without_the_finish_line(self):
        """The car has to actually get there."""
        self.clear_course(self.client_, self.user)
        self.at(self.user, RACE_OBSTACLE_TIMES[-1])

        response = self.complete()
        self.assertEqual(response.status_code, 400)
        self.assertIn('finish line', response.json()['error'])

        self.user.refresh_from_db()
        self.assertIsNone(self.user.race_completed_at)

    def test_reaching_the_finish_line_is_not_enough_without_the_obstacles(self):
        self.clear_course(self.client_, self.user, obstacles=RACE_OBSTACLE_COUNT - 1)
        self.at(self.user, RACE_COURSE_SECONDS)

        response = self.complete()
        self.assertEqual(response.status_code, 400)
        self.assertIn('obstacle', response.json()['error'])

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
        self.clear_course(self.client_, self.user)
        response = self.finish(self.client_, self.user)

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.race_status, RACE_COMPLETED)
        self.assertEqual(self.user.race_obstacles, RACE_OBSTACLE_COUNT)

    def test_an_obstacle_cannot_be_cleared_before_the_course_reaches_it(self):
        response = self.client_.post(self.urls()['progress'], {'obstacle': 1})
        # obstacle 1 is a few seconds down the course, and the race just began
        if RACE_OBSTACLE_TIMES[0] > RACE_TIMING_GRACE_SECONDS:
            self.assertEqual(response.status_code, 400)
            self.assertIn('ahead of you', response.json()['error'])

        self.at(self.user, RACE_OBSTACLE_TIMES[-1] - RACE_TIMING_GRACE_SECONDS - 1)
        early = self.client_.post(self.urls()['progress'],
                                  {'obstacle': RACE_OBSTACLE_COUNT})
        self.assertEqual(early.status_code, 400)

        self.user.refresh_from_db()
        self.assertEqual(self.user.race_obstacles, 0)

    def test_obstacles_must_be_cleared_in_course_order(self):
        self.at(self.user, RACE_COURSE_SECONDS)
        response = self.client_.post(self.urls()['progress'], {'obstacle': 3})

        self.assertEqual(response.status_code, 400)
        self.assertIn('in order', response.json()['error'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.race_obstacles, 0)

    def test_the_same_obstacle_cannot_be_counted_twice(self):
        self.clear_obstacle(self.client_, self.user, 1)
        for _ in range(5):
            response = self.client_.post(self.urls()['progress'], {'obstacle': 1})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['counted'])

        self.user.refresh_from_db()
        self.assertEqual(self.user.race_obstacles, 1)

    def test_a_forged_obstacle_index_is_refused(self):
        self.at(self.user, RACE_COURSE_SECONDS)
        for value in (0, -1, 7, 99, 'six', '1.5'):
            response = self.client_.post(self.urls()['progress'], {'obstacle': value})
            self.assertEqual(response.status_code, 400, value)

        # an empty field is simply "nothing to report", and counts nothing
        empty = self.client_.post(self.urls()['progress'], {'obstacle': ''})
        self.assertEqual(empty.status_code, 200)
        self.assertFalse(empty.json()['counted'])

        self.user.refresh_from_db()
        self.assertEqual(self.user.race_obstacles, 0)

    def test_the_course_cannot_be_cleared_faster_than_it_can_be_driven(self):
        """Six valid clears take at least as long as the course does."""
        for index in range(1, RACE_OBSTACLE_COUNT + 1):
            self.clear_obstacle(self.client_, self.user, index)

        self.user.refresh_from_db()
        self.assertGreaterEqual(
            self.user.elapsed_seconds,
            RACE_OBSTACLE_TIMES[-1] - RACE_TIMING_GRACE_SECONDS)

    def test_progress_is_refused_once_the_race_is_over(self):
        self.clear_course(self.client_, self.user)
        self.time_out(self.user)

        for payload in ({'obstacle': 1}, {'collision': 1}):
            response = self.client_.post(self.urls()['progress'], payload)
            self.assertEqual(response.status_code, 409)

        completed, client = self.make_player('PC-FINISH2')
        self.start_race(client, completed)
        self.clear_course(client, completed)
        self.finish(client, completed)

        after = client.post(self.urls()['progress'], {'collision': 1})
        self.assertEqual(after.status_code, 409)
        completed.refresh_from_db()
        self.assertEqual(completed.race_collisions, 0)

    def test_progress_before_the_race_starts_is_refused(self):
        stranger, client = self.make_player('PC-FINISH3')
        response = client.post(self.urls()['progress'], {'obstacle': 1})
        self.assertEqual(response.status_code, 400)

        stranger.refresh_from_db()
        self.assertEqual(stranger.race_obstacles, 0)


# ==========================================================================
# The score is the server's, and only the server's
# ==========================================================================

class RaceScoringTests(RaceMixin, TestCase):
    def test_the_score_is_calculated_by_the_server(self):
        user, _client, response = self.full_race('PC-SCORE1')
        data = response.json()

        expected = race_score(user.race_time_seconds, RACE_OBSTACLE_COUNT, 0)
        self.assertEqual(user.best_score, expected)
        self.assertEqual(data['score'], expected)

        # ...and that is the published formula, not a number from the browser
        self.assertEqual(expected, min(RACE_MAX_SCORE, (
            RACE_BASE_POINTS
            + int(RACE_TIME_POINTS * (GAME_DURATION_SECONDS - user.race_time_seconds)
                  / (GAME_DURATION_SECONDS - RACE_COURSE_SECONDS))
            + RACE_OBSTACLE_COUNT * RACE_OBSTACLE_POINTS
            + RACE_CLEAN_RUN_BONUS)))

    def test_a_perfect_run_scores_the_maximum_and_never_more(self):
        user, _client, _response = self.full_race('PC-SCORE2')
        self.assertEqual(user.best_score, RACE_MAX_SCORE)

    def test_a_forged_score_in_the_completion_request_is_ignored(self):
        user, client = self.make_player('PC-SCORE3')
        self.start_race(client, user)
        self.clear_course(client, user, collisions=4)
        self.at(user, RACE_COURSE_SECONDS)

        client.post(self.urls()['complete'], {
            'finish': 1, 'score': 1000, 'best_score': 1000, 'obstacles': 6,
            'collisions': 0, 'elapsed': 1, 'race_time_seconds': 1,
        })

        user.refresh_from_db()
        self.assertEqual(user.race_collisions, 4)
        self.assertEqual(user.best_score,
                         race_score(user.race_time_seconds, RACE_OBSTACLE_COUNT, 4))
        self.assertLess(user.best_score, RACE_MAX_SCORE)

    def test_forged_obstacle_and_collision_counts_cannot_finish_a_race(self):
        """The classic DevTools attack: claim a perfect run, immediately."""
        user, client = self.make_player('PC-SCORE4')
        self.start_race(client, user)

        response = client.post(self.urls()['complete'], {
            'obstacles': 6, 'collisions': 0, 'finish': 1,
        })

        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertIsNone(user.race_completed_at)
        self.assertEqual(user.race_obstacles, 0)
        self.assertEqual(user.best_score, 0)
        self.assertFalse(FinalSubmission.objects.filter(user=user).exists())

    def test_collisions_are_counted_by_the_server_and_cannot_be_undone(self):
        user, client = self.make_player('PC-SCORE5')
        self.start_race(client, user)

        for _ in range(3):
            client.post(self.urls()['progress'], {'collision': 1})
        user.refresh_from_db()
        self.assertEqual(user.race_collisions, 3)

        # nothing the browser sends can talk the count back down
        for payload in ({'collision': 0}, {'collision': ''}, {'collisions': 0}):
            client.post(self.urls()['progress'], payload)
        user.refresh_from_db()
        self.assertEqual(user.race_collisions, 3)

    def test_the_collision_counter_cannot_be_overflowed(self):
        user, client = self.make_player('PC-SCORE6')
        self.start_race(client, user)
        User.objects.filter(pk=user.pk).update(race_collisions=RACE_MAX_COLLISIONS)

        for _ in range(5):
            client.post(self.urls()['progress'], {'collision': 1})

        user.refresh_from_db()
        self.assertEqual(user.race_collisions, RACE_MAX_COLLISIONS)

    def test_collisions_cost_points(self):
        clean, _c1, _r1 = self.full_race('PC-SCORE7', collisions=0)
        messy, _c2, _r2 = self.full_race('PC-SCORE8', collisions=3)

        self.assertGreater(clean.best_score, messy.best_score)
        self.assertEqual(
            clean.best_score - messy.best_score,
            3 * RACE_COLLISION_PENALTY + RACE_CLEAN_RUN_BONUS)

    def test_finishing_later_scores_less(self):
        quick, _c1, _r1 = self.full_race('PC-SCORE9')
        slow, _c2, _r2 = self.full_race('PC-SCORE10', finish_at=RACE_COURSE_SECONDS + 300)

        self.assertGreater(quick.best_score, slow.best_score)
        self.assertEqual(slow.race_time_seconds, RACE_COURSE_SECONDS + 300)

    def test_the_score_never_goes_negative(self):
        self.assertEqual(race_score(GAME_DURATION_SECONDS, 0, RACE_MAX_COLLISIONS), 0)
        self.assertEqual(race_score(1, RACE_OBSTACLE_COUNT, 0), RACE_MAX_SCORE)

    def test_a_successful_completion_records_the_performance(self):
        user, _client, response = self.full_race('PC-SCORE11', collisions=2)
        data = response.json()

        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertIsNotNone(user.race_completed_at)
        self.assertEqual(user.race_obstacles, RACE_OBSTACLE_COUNT)
        self.assertEqual(user.race_collisions, 2)
        self.assertEqual(user.race_time_seconds, RACE_COURSE_SECONDS)
        self.assertGreater(user.best_score, 0)
        self.assertEqual(data['redirect'], self.urls()['result'])

        final = FinalSubmission.objects.get(user=user)
        self.assertTrue(final.eligible)
        self.assertTrue(final.reached_all)
        self.assertEqual(final.score, user.best_score)
        self.assertEqual(final.total, RACE_MAX_SCORE)

    def test_the_result_page_reports_the_performance_without_naming_a_winner(self):
        user, client, _response = self.full_race('PC-SCORE12', collisions=1)
        page = client.get(self.urls()['result']).content.decode()

        self.assertIn('CHALLENGE COMPLETE', page)
        self.assertIn(str(user.best_score), page)
        self.assertIn('one overall winner', page)
        for claim in ('YOU ARE THE WINNER', 'YOU WON', '1ST PLACE', 'RANK'):
            self.assertNotIn(claim, page.upper(), claim)

    def test_the_result_page_is_closed_until_the_race_is_finished(self):
        _user, client = self.make_player('PC-SCORE13')
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
        self.clear_course(client, user)
        self.assertRedirects(client.get(self.urls()['fixed']), self.urls()['home'])

    def test_a_timeout_does_not_unlock_the_fixed_website(self):
        user, client = self.make_player('PC-REWARD3')
        self.start_race(client, user)
        self.clear_course(client, user)
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

    def test_the_reward_is_framed_safely(self):
        _user, client, _response = self.full_race('PC-REWARD5')
        response = client.get(self.urls()['fixed'])

        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn('<script', response.content.decode())
        self.assertEqual(client.get(self.urls()['home'])['X-Frame-Options'], 'DENY')

    def test_the_result_page_links_to_the_existing_fixed_website(self):
        _user, client, _response = self.full_race('PC-REWARD6')
        page = client.get(self.urls()['result']).content.decode()

        self.assertIn(self.urls()['fixed'], page)
        self.assertIn('sandbox', page)
        # ...and the answers are not inlined into the result page itself
        self.assertNotIn(SOLUTION_CSS, page)

    def test_the_fixed_website_requires_authentication(self):
        _user, client, _response = self.full_race('PC-REWARD7')
        client.get(reverse('logout'))
        self.assertEqual(client.get(self.urls()['fixed']).status_code, 302)

    def test_one_participants_completion_does_not_unlock_it_for_another(self):
        self.full_race('PC-REWARD8')
        _other, other_client = self.make_player('PC-REWARD9')
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

        for name in ('home', 'result', 'fixed', 'broken'):
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
        self.clear_course(my_client, mine)

        theirs, their_client = self.make_player('PC-SEC4')
        self.start_race(their_client, theirs)

        # their progress cannot ride on mine
        response = their_client.post(self.urls()['complete'], {'finish': 1})
        self.assertEqual(response.status_code, 400)

        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(theirs.race_obstacles, 0)
        self.assertEqual(mine.race_obstacles, RACE_OBSTACLE_COUNT)
        self.assertIsNone(theirs.race_completed_at)

    def test_finishing_does_not_touch_anybody_elses_record(self):
        neighbour, _client = self.make_player('PC-SEC5')
        self.full_race('PC-SEC6')

        neighbour.refresh_from_db()
        self.assertIsNone(neighbour.race_started_at)
        self.assertEqual(neighbour.best_score, 0)
        self.assertFalse(FinalSubmission.objects.filter(user=neighbour).exists())

    def test_a_new_participant_inherits_nothing(self):
        self.full_race('PC-SEC7', collisions=5)
        fresh, fresh_client = self.make_player('PC-SEC8')

        state = fresh_client.get(self.urls()['state']).json()
        self.assertEqual(state['status'], RACE_NOT_STARTED)
        self.assertEqual(state['obstacles'], 0)
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
        # signing up does not start a race
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
        self.clear_course(timed_out_client, self.timed_out, obstacles=3)
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
        self.assertIn('03:00', body)  # the recorded race time

    def test_a_participants_detail_page_shows_every_recorded_figure(self):
        page = self.admin.get(
            reverse('admin:first_user_change', args=[self.finisher.pk]))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()

        for label in ('Race started at', 'Race completed at', 'Race obstacles',
                      'Race collisions', 'Race time', 'Status', 'Timed out'):
            self.assertIn(label, body, label)
        self.assertNotIn(self.finisher.password, body)

    def test_the_timed_out_attempt_is_recorded_and_marked_ineligible(self):
        self.admin.get(reverse('admin:first_user_changelist'))

        final = FinalSubmission.objects.get(user=self.timed_out)
        self.assertFalse(final.eligible)
        self.assertEqual(final.status, 'Expired')

        self.timed_out.refresh_from_db()
        self.assertEqual(self.timed_out.race_obstacles, 3)

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

class RaceFlowTests(RaceMixin, TestCase):
    def test_the_briefing_shows_the_broken_site_and_the_start_button(self):
        _user, client = self.make_player('PC-FLOW1')
        page = client.get(self.urls()['home']).content.decode()

        self.assertIn('START RACE', page)
        self.assertIn(self.urls()['broken'], page)
        self.assertIn('sandbox', page)
        self.assertIn('one official attempt', page)
        # the "attempt over" panel belongs to a timed-out briefing, not this one
        self.assertNotIn('id="wf-ended"', page)

    def test_the_broken_site_is_the_broken_stylesheet(self):
        _user, client = self.make_player('PC-FLOW2')
        body = client.get(self.urls()['broken']).content.decode()

        self.assertIn('<h1 class="hero__title">', body)
        self.assertIn('font-size: 1rem;', body)          # the defect
        self.assertNotIn('line-height: 1.6;\n  -webkit', body)   # not the answer
        self.assertNotIn(SOLUTION_CSS, body)

    def test_the_briefing_never_ships_the_answers(self):
        _user, client = self.make_player('PC-FLOW3')
        page = client.get(self.urls()['home']).content.decode()

        for answer in ('grid-template-columns: repeat(3, 1fr);',
                       '@media (max-width: 860px)',
                       'font-size: clamp(2.4rem, 4.4vw, 3.6rem);'):
            self.assertNotIn(answer, page, answer)

    def test_a_race_in_progress_offers_to_resume_rather_than_restart(self):
        user, client = self.make_player('PC-FLOW4')
        self.start_race(client, user)
        self.at(user, 200)

        page = client.get(self.urls()['home']).content.decode()
        self.assertIn('RESUME RACE', page)
        self.assertIn('does not restart', page)

    def test_the_briefing_redirects_to_the_result_once_the_race_is_finished(self):
        _user, client, _response = self.full_race('PC-FLOW5')
        self.assertRedirects(client.get(self.urls()['home']), self.urls()['result'])

    def test_the_race_page_carries_the_course_the_server_defined(self):
        _user, client = self.make_player('PC-FLOW6')
        page = client.get(self.urls()['home']).content.decode()

        self.assertIn('wf-race-config', page)
        self.assertIn('wf-race-urls', page)
        self.assertIn(self.urls()['progress'], page)
        self.assertIn(str(RACE_COURSE_SECONDS), page)
        self.assertEqual(page.count('class="wf-obstacle'), RACE_OBSTACLE_COUNT)
        self.assertIn('id="wf-finish"', page)

    def test_the_whole_flow_end_to_end(self):
        user, client = self.make_player('PC-FLOW7')

        # intro -> start -> briefing, and nothing has begun
        self.assertEqual(client.get(reverse('intro')).status_code, 200)
        self.assertEqual(client.get(reverse('start')).status_code, 200)
        self.assertEqual(client.get(self.urls()['home']).status_code, 200)
        user.refresh_from_db()
        self.assertIsNone(user.race_started_at)

        # START RACE
        self.assertEqual(self.start_race(client, user)['status'], RACE_ACTIVE)

        # the course, then the finish line
        self.clear_course(client, user, collisions=1)
        response = self.finish(client, user)
        self.assertEqual(response.status_code, 200)

        # the result, and the reward behind it
        self.assertRedirects(client.get(self.urls()['home']), self.urls()['result'])
        self.assertEqual(client.get(self.urls()['result']).status_code, 200)
        self.assertEqual(client.get(self.urls()['fixed']).status_code, 200)

        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertEqual(user.race_obstacles, RACE_OBSTACLE_COUNT)
        self.assertEqual(user.race_collisions, 1)
        self.assertEqual(user.best_score,
                         race_score(user.race_time_seconds, RACE_OBSTACLE_COUNT, 1))

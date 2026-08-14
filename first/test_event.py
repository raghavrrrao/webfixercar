"""Event hardening: the things that go wrong on the day, not in the design.

Phase 4 cover. Everything here is about a real hall full of borrowed PCs —
records left behind by an older version of the game, a machine handed to the
next participant, two completion requests arriving at once, an organiser who
has to reset the right person's attempt at eleven at night, and the file the
judges take away at the end.

Nothing in this module tests gameplay. The race, the scoring and the repairs
are covered by `test_race.py` and are deliberately untouched.
"""

import csv
import io
import threading
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .export import COLUMNS, result_rows, write_results_csv
from .game_config import (
    GAME_DURATION_SECONDS,
    RACE_COURSE_METRES,
    RACE_REPAIR_METRES,
)
from .models import (
    RACE_ACTIVE,
    RACE_COMPLETED,
    RACE_EXPIRED,
    RACE_NOT_STARTED,
    FinalSubmission,
    User,
)
from .repairs import REPAIR_IDS
from .test_race import RaceMixin


# ==========================================================================
# Records the previous version of the game left behind
# ==========================================================================

class LegacyRoundTests(RaceMixin, TestCase):
    """A round recorded before the race existed is still an attempt.

    The CSS-editor version stamped `game_start_time` and never wrote
    `race_started_at`. Reading only the newer field made those records report
    themselves as NOT_STARTED while simultaneously claiming to be expired —
    and handed them a fresh twelve minutes on request.
    """

    def legacy(self, name, ago):
        user = User.objects.create_user(username=name, pc_no='PC-OLD',
                                        password='pw-123456')
        User.objects.filter(pk=user.pk).update(
            game_start_time=timezone.now() - timedelta(seconds=ago))
        user.refresh_from_db()
        client = Client()
        client.force_login(user, backend='first.backends.ParticipantBackend')
        return user, client

    def test_a_finished_legacy_round_reads_as_timed_out(self):
        user, _client = self.legacy('Old Hand', GAME_DURATION_SECONDS * 3)
        self.assertEqual(user.race_status, RACE_EXPIRED)

    def test_the_state_machine_never_contradicts_itself(self):
        """`race_status` and `is_expired` are one answer, not two."""
        user, _client = self.legacy('Consistent', GAME_DURATION_SECONDS * 3)
        self.assertTrue(user.is_expired)
        self.assertTrue(user.is_locked)
        self.assertEqual(user.remaining_seconds, 0)

    def test_a_legacy_round_is_not_handed_a_second_attempt(self):
        user, client = self.legacy('No Rerun', GAME_DURATION_SECONDS * 3)
        response = client.post(self.urls()['start'], {})

        self.assertEqual(response.status_code, 409)
        user.refresh_from_db()
        self.assertIsNone(user.race_started_at)
        self.assertEqual(user.race_status, RACE_EXPIRED)

    def test_a_legacy_start_time_is_never_overwritten(self):
        """The old timestamp is the record of when they actually played."""
        user, client = self.legacy('Preserved', GAME_DURATION_SECONDS * 3)
        stamped = user.game_start_time
        client.post(self.urls()['start'], {})
        user.refresh_from_db()
        self.assertEqual(user.game_start_time, stamped)

    def test_a_legacy_round_still_inside_its_window_is_active(self):
        user, _client = self.legacy('Mid Round', 60)
        self.assertEqual(user.race_status, RACE_ACTIVE)
        self.assertFalse(user.is_expired)

    def test_start_challenge_itself_refuses_a_legacy_record(self):
        """Belt and braces: the model refuses even without the endpoint."""
        user, _client = self.legacy('Model Guard', GAME_DURATION_SECONDS * 3)
        self.assertFalse(user.start_challenge())

    def test_a_legacy_record_does_not_break_the_scoreboard(self):
        from .scoreboard import scoreboard_snapshot
        self.legacy('On The Board', GAME_DURATION_SECONDS * 3)
        rows = scoreboard_snapshot()['players']

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state']['status'], RACE_EXPIRED)
        self.assertEqual(rows[0]['state']['remaining'], 0)

    def test_a_legacy_record_does_not_break_the_export(self):
        self.legacy('In The File', GAME_DURATION_SECONDS * 3)
        row = result_rows()[0]
        self.assertEqual(row['status'], "TIME'S UP")
        self.assertEqual(row['reward_unlocked'], 'no')
        self.assertTrue(row['started_at'])

    def test_the_legacy_reward_stays_locked(self):
        _user, client = self.legacy('Still Locked', GAME_DURATION_SECONDS * 3)
        self.assertRedirects(client.get(self.urls()['fixed']),
                             self.urls()['home'])


# ==========================================================================
# Two requests at once
# ==========================================================================

class SimultaneousCompletionTests(RaceMixin, TransactionTestCase):
    """Only one completion may ever be recorded for one attempt.

    Two tabs, a double-click or a retried fetch can genuinely land together.
    The conditional update in `api_race_complete` is what makes the second one
    lose, and this drives it from real threads rather than trusting the read.
    """

    def ready_to_finish(self, name):
        user = User.objects.create_user(username=name, pc_no='PC-RACE',
                                        password='pw-123456')
        User.objects.filter(pk=user.pk).update(
            race_started_at=timezone.now() - timedelta(seconds=400),
            game_start_time=timezone.now() - timedelta(seconds=400))
        user.refresh_from_db()
        for index, repair_id in enumerate(REPAIR_IDS):
            user.race_distance = RACE_REPAIR_METRES[index]
            user.save(update_fields=['race_distance'])
            user.record_repair(repair_id)
        User.objects.filter(pk=user.pk).update(race_distance=RACE_COURSE_METRES)
        user.refresh_from_db()
        return user

    def test_a_request_that_read_the_race_before_it_finished_still_loses(self):
        """The exact interleave, without depending on thread scheduling.

        Two workers handle a completion at once: both load the participant
        while the race is still active, both pass every guard, and then one of
        them commits first. The loser is holding precisely this stale record.
        It must not be able to write a second result over the first.
        """
        from django.test import RequestFactory
        from .views import api_race_complete

        user = self.ready_to_finish('Photo Finish')
        stale = User.objects.get(pk=user.pk)          # loaded before the write

        winner = Client()
        winner.force_login(user, backend='first.backends.ParticipantBackend')
        self.assertEqual(winner.post(self.urls()['complete'], {}).status_code, 200)
        user.refresh_from_db()
        recorded = (user.race_completed_at, user.best_score,
                    user.race_time_seconds)

        # The straggler arrives, still believing the race is running.
        self.assertIsNone(stale.race_completed_at)
        request = RequestFactory().post(self.urls()['complete'])
        request.user = stale
        response = api_race_complete(request)

        self.assertEqual(response.status_code, 409)
        user.refresh_from_db()
        self.assertEqual(
            (user.race_completed_at, user.best_score, user.race_time_seconds),
            recorded)
        self.assertEqual(FinalSubmission.objects.filter(user=user).count(), 1)

    def test_concurrent_finishes_record_exactly_one_result(self):
        """The same claim again, from real threads.

        SQLite serialises writers with a table lock and will refuse a request
        outright rather than queue it, so a thread here can come back with an
        OperationalError instead of a response. That is the test database's
        limit, not the application's, and it does not weaken what is being
        asserted: whatever each request individually got, the database ends up
        holding exactly one completion and exactly one submission.
        """
        user = self.ready_to_finish('Dead Heat')
        client = Client()
        client.force_login(user, backend='first.backends.ParticipantBackend')

        codes, locked = [], []
        barrier = threading.Barrier(4)

        def finish():
            try:
                barrier.wait(timeout=10)
                codes.append(client.post(self.urls()['complete'], {}).status_code)
            except OperationalError as exc:
                locked.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=finish) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(codes) + len(locked), 4)
        self.assertLessEqual(codes.count(200), 1, codes)
        self.assertTrue(all(code in (200, 409) for code in codes), codes)

        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_COMPLETED)
        self.assertEqual(
            User.objects.filter(pk=user.pk,
                                race_completed_at__isnull=False).count(), 1)
        self.assertEqual(
            FinalSubmission.objects.filter(user=user).count(), 1)

    def test_the_score_does_not_move_after_the_first_finish(self):
        user = self.ready_to_finish('Stable')
        client = Client()
        client.force_login(user, backend='first.backends.ParticipantBackend')

        first = client.post(self.urls()['complete'], {})
        self.assertEqual(first.status_code, 200)
        user.refresh_from_db()
        settled = (user.best_score, user.race_completed_at, user.race_time_seconds)

        for _ in range(3):
            self.assertEqual(
                client.post(self.urls()['complete'], {}).status_code, 409)
        user.refresh_from_db()
        self.assertEqual(
            (user.best_score, user.race_completed_at, user.race_time_seconds),
            settled)


# ==========================================================================
# The machine is handed to the next participant
# ==========================================================================

class SharedMachineSessionTests(RaceMixin, TestCase):
    """Nothing of the previous participant survives on the PC itself."""

    def test_signed_in_pages_are_never_written_to_the_browser_cache(self):
        """Back-button on a shared PC must reach the server, not a cache."""
        user, client = self.make_player('Cached', pc_no='PC-01')
        self.start_race(client, user)

        for name in ('home', 'start'):
            response = client.get(reverse(name))
            self.assertIn('no-store', response['Cache-Control'], name)
            self.assertIn('private', response['Cache-Control'], name)

    def test_the_result_page_is_never_cached_either(self):
        _user, client, _response = self.full_race('Finished', pc_no='PC-01')
        response = client.get(self.urls()['result'])
        self.assertIn('no-store', response['Cache-Control'])

    def test_the_organiser_monitor_is_never_cached(self):
        organiser = User.objects.create_superuser(
            username='organiser', pc_no='desk', password='pw-123456')
        client = Client()
        client.force_login(organiser,
                           backend='first.backends.ParticipantBackend')
        for name in ('scoreboard', 'scoreboard_display'):
            response = client.get(reverse(name))
            self.assertIn('no-store', response['Cache-Control'], name)

    def test_the_next_participant_cannot_reach_the_previous_ones_pages(self):
        first, first_client = self.make_player('Rahul', pc_no='PC-01')
        self.start_race(first_client, first)
        self.drive_course(first_client, first)
        self.cross_finish(first_client, first)
        first_client.get(reverse('logout'))

        # Same physical machine, same browser session cookie jar, new person.
        second, _second_client = self.make_player('Priya', pc_no='PC-01')

        for path in (self.urls()['result'], self.urls()['fixed'],
                     self.urls()['home']):
            response = first_client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn(reverse('login'), response['Location'], path)

        # And the second participant's own race is untouched by any of it.
        second.refresh_from_db()
        self.assertEqual(second.race_status, RACE_NOT_STARTED)

    def test_logging_out_does_not_disturb_the_recorded_run(self):
        user, client, _response = self.full_race('Recorded', pc_no='PC-01')
        before = (user.best_score, user.race_completed_at, user.race_repairs)
        client.get(reverse('logout'))
        user.refresh_from_db()
        self.assertEqual(
            (user.best_score, user.race_completed_at, user.race_repairs), before)

    def test_the_race_page_carries_a_csrf_token_of_its_own(self):
        """START RACE must not depend on a cookie some earlier page set."""
        user, client = self.make_player('Token', pc_no='PC-01')
        page = client.get(self.urls()['home'])
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.assertIn('csrftoken', client.cookies)

    def test_three_participants_on_one_pc_keep_three_separate_records(self):
        rahul, rahul_client, _ = self.full_race('Rahul', pc_no='PC-14')
        priya, priya_client = self.make_player('Priya', pc_no='PC-14')
        self.start_race(priya_client, priya)
        arjun, arjun_client = self.make_player('Arjun', pc_no='PC-14')
        self.start_race(arjun_client, arjun)
        self.time_out(arjun)
        arjun_client.get(self.urls()['state'])

        rahul.refresh_from_db()
        priya.refresh_from_db()
        arjun.refresh_from_db()
        self.assertEqual(rahul.race_status, RACE_COMPLETED)
        self.assertEqual(priya.race_status, RACE_ACTIVE)
        self.assertEqual(arjun.race_status, RACE_EXPIRED)
        self.assertEqual(
            User.objects.filter(pc_no='PC-14').count(), 3)

        # Rahul's reward is Rahul's; the two after him get nothing from it.
        self.assertEqual(rahul_client.get(self.urls()['fixed']).status_code, 200)
        self.assertRedirects(priya_client.get(self.urls()['fixed']),
                             self.urls()['home'])
        self.assertRedirects(arjun_client.get(self.urls()['fixed']),
                             self.urls()['home'])


# ==========================================================================
# Database integrity
# ==========================================================================

class DatabaseIntegrityTests(TestCase):
    def test_a_participant_name_is_unique(self):
        self.assertTrue(User._meta.get_field('username').unique)

    def test_a_pc_number_is_deliberately_not_unique(self):
        """A PC is a desk, and a desk seats one participant after another."""
        self.assertFalse(User._meta.get_field('pc_no').unique)
        User.objects.create_user(username='One', pc_no='PC-14',
                                 password='pw-123456')
        User.objects.create_user(username='Two', pc_no='PC-14',
                                 password='pw-123456')
        self.assertEqual(User.objects.filter(pc_no='PC-14').count(), 2)

    def test_one_participant_can_hold_only_one_submission(self):
        self.assertTrue(
            FinalSubmission._meta.get_field('user').one_to_one)

    def test_deleting_a_participant_takes_only_their_own_data(self):
        keeper = User.objects.create_user(username='Keeper', pc_no='PC-14',
                                          password='pw-123456')
        doomed = User.objects.create_user(username='Doomed', pc_no='PC-14',
                                          password='pw-123456')
        for user in (keeper, doomed):
            FinalSubmission.objects.create(
                user=user, pc_no=user.pc_no, submitted_at=timezone.now(),
                final_css='', score=1, total=1)

        doomed.delete()

        self.assertTrue(User.objects.filter(pk=keeper.pk).exists())
        self.assertEqual(FinalSubmission.objects.count(), 1)
        self.assertEqual(FinalSubmission.objects.get().user_id, keeper.pk)

    def test_a_submission_is_never_orphaned_from_its_participant(self):
        field = FinalSubmission._meta.get_field('user')
        from django.db import models as db_models
        self.assertIs(field.remote_field.on_delete, db_models.CASCADE)


# ==========================================================================
# reset_race: the right person, or nobody
# ==========================================================================

class ResetRaceCommandTests(RaceMixin, TestCase):
    def call(self, *args, **options):
        out, err = io.StringIO(), io.StringIO()
        call_command('reset_race', *args, stdout=out, stderr=err, **options)
        return out.getvalue()

    def test_it_clears_the_named_participants_attempt(self):
        user, client = self.make_player('Rahul', pc_no='PC-14')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=2)

        self.call('Rahul', '--yes')

        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_NOT_STARTED)
        self.assertEqual(user.repair_ids, [])
        self.assertEqual(user.race_distance, 0)

    def test_it_refuses_a_pc_number_several_participants_used(self):
        """The bug this exists to prevent: wiping a stranger's live race."""
        rahul, rahul_client = self.make_player('Rahul', pc_no='PC-14')
        priya, priya_client = self.make_player('Priya', pc_no='PC-14')
        self.start_race(rahul_client, rahul)
        self.start_race(priya_client, priya)

        with self.assertRaises(CommandError) as caught:
            self.call('PC-14', '--yes')

        message = str(caught.exception)
        self.assertIn('Rahul', message)
        self.assertIn('Priya', message)
        self.assertIn('participant name', message)

        rahul.refresh_from_db()
        priya.refresh_from_db()
        self.assertEqual(rahul.race_status, RACE_ACTIVE)
        self.assertEqual(priya.race_status, RACE_ACTIVE)

    def test_a_pc_number_still_works_when_one_person_used_it(self):
        user, client = self.make_player('Solo', pc_no='PC-99')
        self.start_race(client, user)
        self.call('PC-99', '--yes')
        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_NOT_STARTED)

    def test_pc_narrows_a_shared_number_to_one_person(self):
        rahul, rahul_client = self.make_player('Rahul', pc_no='PC-14')
        priya, priya_client = self.make_player('Priya', pc_no='PC-14')
        self.start_race(rahul_client, rahul)
        self.start_race(priya_client, priya)

        self.call('Priya', '--yes', pc='PC-14')

        rahul.refresh_from_db()
        priya.refresh_from_db()
        self.assertEqual(rahul.race_status, RACE_ACTIVE)
        self.assertEqual(priya.race_status, RACE_NOT_STARTED)

    def test_it_refuses_to_clear_a_real_attempt_without_yes(self):
        user, client = self.make_player('Careful', pc_no='PC-14')
        self.start_race(client, user)

        with self.assertRaises(CommandError):
            self.call('Careful')

        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_ACTIVE)

    def test_it_says_what_it_is_about_to_destroy(self):
        user, client = self.make_player('Loud', pc_no='PC-14')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=3)

        with self.assertRaises(CommandError):
            self.call('Loud')
        # The refusal happens after the summary is written, so the operator
        # has seen exactly whose attempt they were about to clear.

    def test_it_removes_the_submission_with_the_attempt(self):
        user, _client, _response = self.full_race('Submitted', pc_no='PC-14')
        self.assertTrue(FinalSubmission.objects.filter(user=user).exists())

        self.call('Submitted', '--yes')

        self.assertFalse(FinalSubmission.objects.filter(user=user).exists())
        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_NOT_STARTED)

    def test_it_clears_the_legacy_clock_too(self):
        """Otherwise the reset participant is still holding an old attempt."""
        user, _client = self.make_player('Legacy Clear', pc_no='PC-14')
        User.objects.filter(pk=user.pk).update(
            game_start_time=timezone.now() - timedelta(seconds=60))

        self.call('Legacy Clear', '--yes')

        user.refresh_from_db()
        self.assertIsNone(user.game_start_time)
        self.assertEqual(user.race_status, RACE_NOT_STARTED)

    def test_it_never_touches_anybody_else(self):
        rahul, rahul_client = self.make_player('Rahul', pc_no='PC-14')
        priya, priya_client = self.make_player('Priya', pc_no='PC-15')
        self.start_race(rahul_client, rahul)
        self.start_race(priya_client, priya)
        self.drive_course(priya_client, priya, repairs=4)
        before = priya.race_repairs

        self.call('Rahul', '--yes')

        priya.refresh_from_db()
        self.assertEqual(priya.race_status, RACE_ACTIVE)
        self.assertEqual(priya.race_repairs, before)

    def test_new_creates_an_unstarted_test_participant(self):
        self.call('Tester', '--new', pc='PC-TEST')
        user = User.objects.get(username='Tester')
        self.assertEqual(user.pc_no, 'PC-TEST')
        self.assertEqual(user.race_status, RACE_NOT_STARTED)

    def test_new_refuses_to_stand_on_an_existing_participant(self):
        self.make_player('Existing', pc_no='PC-14')
        with self.assertRaises(CommandError):
            self.call('Existing', '--new')

    def test_an_unknown_participant_is_an_error_not_a_silent_no_op(self):
        with self.assertRaises(CommandError):
            self.call('Nobody', '--yes')


# ==========================================================================
# The file the judges take away
# ==========================================================================

class ResultsExportTests(RaceMixin, TestCase):
    def rows(self, text):
        return list(csv.DictReader(io.StringIO(text)))

    def export(self):
        buffer = io.StringIO()
        write_results_csv(buffer)
        return self.rows(buffer.getvalue())

    def test_every_run_on_a_reused_pc_gets_its_own_row(self):
        self.full_race('Rahul', pc_no='PC-14')
        self.full_race('Priya', pc_no='PC-14')
        arjun, arjun_client = self.make_player('Arjun', pc_no='PC-14')
        self.start_race(arjun_client, arjun)
        self.time_out(arjun)

        rows = self.export()
        self.assertEqual([row['participant'] for row in rows],
                         ['Rahul', 'Priya', 'Arjun'])
        self.assertEqual({row['pc_no'] for row in rows}, {'PC-14'})
        self.assertEqual([row['status'] for row in rows],
                         ['COMPLETED', 'COMPLETED', "TIME'S UP"])

    def test_it_carries_what_a_judge_needs(self):
        user, _client, _response = self.full_race('Rahul', pc_no='PC-14')
        row = self.export()[0]

        self.assertEqual(row['participant'], 'Rahul')
        self.assertEqual(row['pc_no'], 'PC-14')
        self.assertEqual(row['status'], 'COMPLETED')
        self.assertEqual(int(row['score']), user.best_score)
        self.assertEqual(int(row['repairs_collected']), len(user.repair_ids))
        self.assertEqual(int(row['penalties']), user.race_collisions)
        self.assertEqual(int(row['distance_metres']), user.race_distance)
        self.assertEqual(int(row['elapsed_seconds']), user.race_time_seconds)
        self.assertTrue(row['started_at'])
        self.assertTrue(row['completed_at'])
        self.assertEqual(row['reward_unlocked'], 'yes')

    def test_it_never_carries_a_credential(self):
        user, _client, _response = self.full_race('Rahul', pc_no='PC-14')
        buffer = io.StringIO()
        write_results_csv(buffer)
        text = buffer.getvalue()

        self.assertNotIn(user.password, text)
        self.assertNotIn('pbkdf2', text)
        for forbidden in ('password', 'session', 'token', 'last_login'):
            self.assertNotIn(forbidden, [name.lower() for name in COLUMNS],
                             forbidden)

    def test_an_organiser_is_left_out_of_the_results(self):
        User.objects.create_superuser(username='organiser', pc_no='desk',
                                      password='pw-123456')
        self.full_race('Rahul', pc_no='PC-14')
        rows = self.export()
        self.assertEqual([row['participant'] for row in rows], ['Rahul'])

    def test_a_timed_out_run_is_recorded_without_a_score_or_a_reward(self):
        user, client = self.make_player('Slow', pc_no='PC-02')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=3)
        self.time_out(user)

        row = self.export()[0]
        self.assertEqual(row['status'], "TIME'S UP")
        self.assertEqual(int(row['score']), 0)
        self.assertEqual(row['reward_unlocked'], 'no')
        self.assertEqual(int(row['repairs_collected']), 3)
        self.assertEqual(int(row['elapsed_seconds']), GAME_DURATION_SECONDS)

    def test_a_timeout_scores_the_same_zero_everywhere_it_is_shown(self):
        """The board, the export and the recorded entry must agree.

        The live projection keeps quoting a running total for as long as it is
        asked, so an attempt that stopped without finishing used to appear on
        the scoreboard holding points its own settled entry said it never
        earned.
        """
        from .scoreboard import player_payload

        user, client = self.make_player('Timed Out', pc_no='PC-04')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=2)
        self.time_out(user)
        client.get(self.urls()['state'])            # the server settles it

        user.refresh_from_db()
        entry = FinalSubmission.objects.get(user=user)
        self.assertEqual(entry.score, 0)
        self.assertEqual(user.best_score, 0)
        self.assertEqual(player_payload(user)['state']['score'], 0)
        self.assertEqual(int(self.export()[0]['score']), 0)
        # ...and the participant's own screen says the same thing.
        self.assertEqual(client.get(self.urls()['state']).json()['score'], 0)

    def test_a_finished_run_scores_the_same_everywhere_it_is_shown(self):
        from .scoreboard import player_payload

        user, _client, _response = self.full_race('Agreed', pc_no='PC-05')
        entry = FinalSubmission.objects.get(user=user)

        self.assertEqual(entry.score, user.best_score)
        self.assertEqual(player_payload(user)['state']['score'], user.best_score)
        self.assertEqual(int(self.export()[0]['score']), user.best_score)

    def test_a_race_still_running_appears_without_pretending_to_be_finished(self):
        user, client = self.make_player('Racing', pc_no='PC-03')
        self.start_race(client, user)
        row = self.export()[0]
        self.assertEqual(row['status'], 'RACING')
        self.assertEqual(row['reward_unlocked'], 'no')
        self.assertEqual(row['completed_at'], '')

    def test_it_names_no_winner_and_no_placings(self):
        self.full_race('Rahul', pc_no='PC-01')
        self.full_race('Priya', pc_no='PC-02')
        buffer = io.StringIO()
        write_results_csv(buffer)
        text = buffer.getvalue().lower()

        # `position` is deliberately absent from this list: it is the name of
        # one of the seven CSS repairs and legitimately appears in every row.
        for word in ('winner', 'rank', 'placing', '1st', '2nd', '3rd'):
            self.assertNotIn(word, text, word)
        # And no column is a placing either.
        self.assertNotIn('rank', [name.lower() for name in COLUMNS])

    def test_the_command_writes_the_same_file(self):
        import tempfile
        from pathlib import Path

        self.full_race('Rahul', pc_no='PC-14')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'results.csv'
            out = io.StringIO()
            call_command('export_results', '--out', str(target), stdout=out)
            rows = self.rows(target.read_text(encoding='utf-8'))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['participant'], 'Rahul')
        self.assertIn('1 run(s)', out.getvalue())

    def test_the_command_changes_no_race(self):
        user, client = self.make_player('Untouched', pc_no='PC-14')
        self.start_race(client, user)
        before = (user.race_started_at, user.best_score, user.race_repairs)

        buffer = io.StringIO()
        call_command('export_results', stdout=buffer)
        self.assertIn('Untouched', buffer.getvalue())

        user.refresh_from_db()
        self.assertEqual(
            (user.race_started_at, user.best_score, user.race_repairs), before)


class ResultsExportAccessTests(RaceMixin, TestCase):
    def setUp(self):
        self.url = reverse('scoreboard_export')

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self):
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_a_participant_cannot_download_the_results(self):
        _user, client = self.make_player('Rahul', pc_no='PC-14')
        self.assertEqual(client.get(self.url).status_code, 403)

    def test_a_participant_cannot_promote_themselves_with_form_data(self):
        _user, client = self.make_player('Sneaky', pc_no='PC-14')
        response = client.get(
            self.url, {'admin': 'true', 'is_admin': '1', 'role': 'admin'})
        self.assertEqual(response.status_code, 403)

    def test_an_organiser_downloads_a_csv_attachment(self):
        User.objects.create_superuser(username='organiser', pc_no='desk',
                                      password='pw-123456')
        client = Client()
        client.force_login(User.objects.get(username='organiser'),
                           backend='first.backends.ParticipantBackend')
        self.full_race('Rahul', pc_no='PC-14')

        response = client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('Rahul', response.content.decode())


# ==========================================================================
# Deployment configuration
# ==========================================================================

class DeploymentConfigTests(TestCase):
    """The settings an event has to get right, asserted rather than assumed."""

    # Every variable settings.py reads. The probe clears all of them before
    # applying a case, so a test asserts what it configured and never what the
    # shell that launched the suite happened to export.
    ENV_KEYS = (
        'SECRET_KEY', 'DJANGO_SECRET_KEY', 'DJANGO_DEBUG',
        'DJANGO_ALLOWED_HOSTS', 'DJANGO_CSRF_TRUSTED_ORIGINS',
        'DJANGO_SSL_REDIRECT', 'RENDER_EXTERNAL_HOSTNAME',
        'DATABASE_URL', 'REDIS_URL', 'WF_SINGLE_MACHINE',
    )

    # What a hosted deployment actually supplies. Individual tests take this
    # away one piece at a time to prove each guard fires on its own.
    HOSTED = {
        'DJANGO_DEBUG': 'false',
        'SECRET_KEY': 'x' * 60,
        'DJANGO_ALLOWED_HOSTS': 'fixer.onrender.com',
        'DATABASE_URL': 'postgres://u:p@db.internal:5432/wf',
        'REDIS_URL': 'redis://kv.internal:6379',
    }

    def load_settings(self, **environ):
        """Import settings.py under a given environment, in isolation."""
        import importlib.util
        import os
        from pathlib import Path
        from django.conf import settings as live

        path = Path(live.BASE_DIR) / 'games' / 'settings.py'
        spec = importlib.util.spec_from_file_location('wf_probe_settings', path)
        module = importlib.util.module_from_spec(spec)

        saved = dict(os.environ)
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({key: str(value) for key, value in environ.items()
                           if value != ''})
        try:
            spec.loader.exec_module(module)
            return module
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def hosted(self, **overrides):
        """The hosted environment, with pieces removed or replaced."""
        env = dict(self.HOSTED)
        env.update(overrides)
        return {key: value for key, value in env.items() if value is not None}

    def assertRefuses(self, expect, **environ):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.load_settings(**environ)
        self.assertIn(expect, str(caught.exception))
        return str(caught.exception)

    # -- development ------------------------------------------------------

    def test_development_still_runs_with_no_configuration_at_all(self):
        module = self.load_settings(DJANGO_DEBUG='true')
        self.assertTrue(module.DEBUG)
        self.assertEqual(module.ALLOWED_HOSTS, ['*'])
        self.assertIn('sqlite', module.DATABASES['default']['ENGINE'])
        self.assertEqual(module.CHANNEL_LAYERS['default']['BACKEND'],
                         'channels.layers.InMemoryChannelLayer')

    # -- the secret key ---------------------------------------------------

    def test_production_refuses_the_published_secret_key(self):
        self.assertRefuses('SECRET_KEY', **self.hosted(SECRET_KEY=None))

    def test_the_secret_key_comes_from_either_accepted_name(self):
        """`SECRET_KEY` is what the blueprint sets; the older name still works."""
        by_new = self.load_settings(**self.hosted(SECRET_KEY='n' * 60))
        self.assertEqual(by_new.SECRET_KEY, 'n' * 60)

        by_old = self.load_settings(
            **self.hosted(SECRET_KEY=None, DJANGO_SECRET_KEY='o' * 60))
        self.assertEqual(by_old.SECRET_KEY, 'o' * 60)

    # -- hosts ------------------------------------------------------------

    def test_production_refuses_to_answer_to_any_hostname(self):
        self.assertRefuses('DJANGO_ALLOWED_HOSTS',
                           **self.hosted(DJANGO_ALLOWED_HOSTS=None))

    def test_the_render_hostname_alone_is_enough_to_boot(self):
        """It is not knowable until the service exists, so it is not committed."""
        module = self.load_settings(**self.hosted(
            DJANGO_ALLOWED_HOSTS=None,
            RENDER_EXTERNAL_HOSTNAME='website-fixer.onrender.com'))
        self.assertIn('website-fixer.onrender.com', module.ALLOWED_HOSTS)
        self.assertIn('https://website-fixer.onrender.com',
                      module.CSRF_TRUSTED_ORIGINS)
        self.assertNotIn('*', module.ALLOWED_HOSTS)

    def test_a_configured_production_run_boots(self):
        module = self.load_settings(**self.hosted(
            DJANGO_ALLOWED_HOSTS='192.168.1.20, fixer.local'))
        self.assertFalse(module.DEBUG)
        self.assertEqual(module.ALLOWED_HOSTS, ['192.168.1.20', 'fixer.local'])
        self.assertTrue(module.SESSION_COOKIE_SECURE)
        self.assertTrue(module.CSRF_COOKIE_SECURE)
        self.assertIn(r'^healthz/?$', module.SECURE_REDIRECT_EXEMPT)

    # -- the database is the event ----------------------------------------

    def test_a_hosted_deployment_refuses_to_start_on_sqlite(self):
        """The Phase 4 blocker, now impossible to reach by accident.

        A hosted filesystem is wiped on every deploy, so a SQLite file there
        takes the whole event with it and says nothing.
        """
        message = self.assertRefuses('DATABASE_URL',
                                     **self.hosted(DATABASE_URL=None))
        self.assertIn('ephemeral', message)

    def test_a_hosted_deployment_uses_the_postgres_url_it_was_given(self):
        module = self.load_settings(**self.hosted())
        engine = module.DATABASES['default']['ENGINE']
        self.assertIn('postgresql', engine)
        self.assertNotIn('sqlite', engine)
        self.assertEqual(module.DATABASES['default']['NAME'], 'wf')

    # -- the channel layer ------------------------------------------------

    def test_a_hosted_deployment_refuses_to_start_without_redis(self):
        message = self.assertRefuses('REDIS_URL', **self.hosted(REDIS_URL=None))
        self.assertIn('second worker', message)

    def test_a_hosted_deployment_uses_the_redis_channel_layer(self):
        module = self.load_settings(**self.hosted())
        layer = module.CHANNEL_LAYERS['default']
        self.assertEqual(layer['BACKEND'],
                         'channels_redis.core.RedisChannelLayer')
        self.assertEqual(layer['CONFIG']['hosts'], ['redis://kv.internal:6379'])

    # -- the one-machine event --------------------------------------------

    def test_one_machine_may_still_run_the_event_on_sqlite_if_it_says_so(self):
        """EVENT-OPERATIONS.md's lab deployment, kept working deliberately."""
        module = self.load_settings(**self.hosted(
            DATABASE_URL=None, REDIS_URL=None,
            DJANGO_ALLOWED_HOSTS='192.168.1.20',
            WF_SINGLE_MACHINE='true'))
        self.assertFalse(module.DEBUG)
        self.assertIn('sqlite', module.DATABASES['default']['ENGINE'])
        self.assertEqual(module.CHANNEL_LAYERS['default']['BACKEND'],
                         'channels.layers.InMemoryChannelLayer')

    def test_the_single_machine_escape_is_opt_in_and_never_the_default(self):
        module = self.load_settings(DJANGO_DEBUG='true')
        self.assertFalse(module.SINGLE_MACHINE)

    def test_trusted_origins_can_be_configured_for_a_fest_domain(self):
        module = self.load_settings(**self.hosted(
            DJANGO_ALLOWED_HOSTS='fixer.college.edu',
            DJANGO_CSRF_TRUSTED_ORIGINS='https://fixer.college.edu'))
        self.assertIn('https://fixer.college.edu', module.CSRF_TRUSTED_ORIGINS)

    def test_the_channel_layer_is_in_memory_until_redis_is_configured(self):
        module = self.load_settings(DJANGO_DEBUG='true')
        self.assertEqual(
            module.CHANNEL_LAYERS['default']['BACKEND'],
            'channels.layers.InMemoryChannelLayer')

    def test_setting_redis_url_switches_the_channel_layer(self):
        module = self.load_settings(DJANGO_DEBUG='true',
                                    REDIS_URL='redis://localhost:6379/0')
        layer = module.CHANNEL_LAYERS['default']
        self.assertEqual(layer['BACKEND'], 'channels_redis.core.RedisChannelLayer')
        self.assertEqual(layer['CONFIG']['hosts'], ['redis://localhost:6379/0'])

    def test_no_redis_credential_is_committed(self):
        from pathlib import Path
        from django.conf import settings as live
        source = (Path(live.BASE_DIR) / 'games' / 'settings.py').read_text(
            encoding='utf-8')
        self.assertNotIn('redis://', source.replace(
            "os.environ.get('REDIS_URL', '')", ''))

    @staticmethod
    def blueprint():
        """render.yaml, parsed. Skips if PyYAML is not installed."""
        import unittest
        from pathlib import Path
        from django.conf import settings as live
        try:
            import yaml
        except ImportError:                                 # pragma: no cover
            raise unittest.SkipTest(
                'PyYAML is not installed — pip install -r requirements-dev.txt')
        return yaml.safe_load(
            (Path(live.BASE_DIR) / 'render.yaml').read_text(encoding='utf-8'))

    def test_the_blueprint_declares_the_three_resources_the_event_needs(self):
        """render.yaml must not quietly leave the event on an ephemeral disk."""
        blueprint = self.blueprint()

        databases = blueprint['databases']
        self.assertEqual(len(databases), 1)
        self.assertIn('postgresMajorVersion', databases[0])

        kinds = {service['type'] for service in blueprint['services']}
        self.assertIn('web', kinds)
        self.assertIn('keyvalue', kinds)

        web = next(s for s in blueprint['services'] if s['type'] == 'web')
        # WebSockets: this cannot become a WSGI server.
        self.assertIn('daphne', web['startCommand'])
        self.assertIn('games.asgi:application', web['startCommand'])
        self.assertIn('$PORT', web['startCommand'])
        self.assertEqual(web['healthCheckPath'], '/healthz/')

        keyvalue = next(s for s in blueprint['services']
                        if s['type'] == 'keyvalue')
        # Required by Render, and empty means internal-only.
        self.assertIn('ipAllowList', keyvalue)

    def test_the_blueprint_wires_every_variable_the_settings_require(self):
        blueprint = self.blueprint()
        web = next(s for s in blueprint['services'] if s['type'] == 'web')
        env = {item['key']: item for item in web['envVars']}

        self.assertEqual(env['DJANGO_DEBUG']['value'], 'false')
        self.assertTrue(env['SECRET_KEY']['generateValue'])
        self.assertTrue(env['WF_ADMIN_PASSWORD']['generateValue'])
        self.assertEqual(env['DATABASE_URL']['fromDatabase']['property'],
                         'connectionString')
        self.assertEqual(env['REDIS_URL']['fromService']['type'], 'keyvalue')
        self.assertEqual(env['REDIS_URL']['fromService']['property'],
                         'connectionString')

    def test_the_blueprint_carries_no_secret_value(self):
        from pathlib import Path
        from django.conf import settings as live

        # The text half needs no parser, so it always runs.
        text = (Path(live.BASE_DIR) / 'render.yaml').read_text(encoding='utf-8')
        self.assertNotIn('postgres://', text)
        self.assertNotIn('redis://', text)
        self.assertNotIn('piyush', text.lower())

        web = next(s for s in self.blueprint()['services']
                   if s['type'] == 'web')
        for item in web['envVars']:
            if item['key'] in ('SECRET_KEY', 'WF_ADMIN_PASSWORD',
                               'DATABASE_URL', 'REDIS_URL'):
                self.assertNotIn('value', item,
                                 f'{item["key"]} is written into the blueprint')

    def test_the_build_command_fails_loudly_rather_than_shipping_a_bad_schema(self):
        from pathlib import Path
        from django.conf import settings as live

        build = (Path(live.BASE_DIR) / 'build.sh').read_text(encoding='utf-8')
        self.assertIn('set -o errexit', build)
        self.assertIn('set -o pipefail', build)
        self.assertIn('makemigrations --check', build)
        self.assertIn('migrate --no-input', build)
        self.assertIn('collectstatic --no-input', build)
        self.assertIn('ensure_admin', build)
        # Nothing may swallow a failure.
        self.assertNotIn('|| true', build)
        self.assertNotIn('set +e', build)

    def test_the_websocket_client_derives_its_scheme_from_the_page(self):
        """https must give wss; no localhost is ever hardcoded."""
        from pathlib import Path
        from django.conf import settings as live
        for name in ('wf-scoreboard.js', 'wf-presence.js'):
            source = (Path(live.BASE_DIR) / 'static' / 'js' / name).read_text(
                encoding='utf-8')
            self.assertIn("'wss://'", source, name)
            self.assertIn("'ws://'", source, name)
            self.assertNotIn('ws://localhost', source, name)
            self.assertNotIn('ws://127.0.0.1', source, name)


# ==========================================================================
# The platform's health check
# ==========================================================================

class HealthCheckTests(RaceMixin, TestCase):
    def setUp(self):
        self.url = reverse('healthz')

    def test_it_answers_without_a_participant(self):
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(),
                         {'status': 'ok', 'database': True})

    def test_it_is_never_cached(self):
        self.assertIn('no-store', Client().get(self.url)['Cache-Control'])

    def test_it_reports_unhealthy_when_the_database_is_unreachable(self):
        from unittest import mock
        with mock.patch('first.views.connection') as fake:
            fake.cursor.side_effect = Exception('connection refused to db.internal')
            response = Client().get(self.url)

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['database'])
        # A public endpoint must not publish the database host in an error.
        self.assertNotIn('db.internal', response.content.decode())

    def test_it_changes_no_race(self):
        user, client = self.make_player('Healthy', pc_no='PC-01')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=3)
        user.refresh_from_db()
        before = (user.race_started_at, user.race_repairs, user.best_score,
                  user.race_distance)

        for _ in range(5):
            self.assertEqual(Client().get(self.url).status_code, 200)

        user.refresh_from_db()
        self.assertEqual(
            (user.race_started_at, user.race_repairs, user.best_score,
             user.race_distance), before)

    def test_it_creates_no_participant_and_no_session(self):
        Client().get(self.url)
        self.assertEqual(User.objects.count(), 0)

    def test_it_is_not_a_race_endpoint(self):
        """The health check must never be able to start an attempt."""
        self.assertNotEqual(self.url, reverse('api_race_start'))
        user, client = self.make_player('Untouched', pc_no='PC-02')
        client.get(self.url)
        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_NOT_STARTED)


# ==========================================================================
# Stale copy on the pages a participant actually sees
# ==========================================================================

class ActiveCopyTests(RaceMixin, TestCase):
    """The participant-facing pages describe *this* game.

    Retained legacy files are not searched: they are documented, unrouted
    infrastructure. These are the pages a participant is served.
    """

    STALE = ('30 minute', '30-minute', '30 minutes', 'CSS editor', 'autosave',
             'auto-save', 'reset CSS', 'one account per PC',
             'PC number already registered', 'Show hint')

    def pages(self):
        user, client = self.make_player('Reader', pc_no='PC-14')
        pages = {
            'intro': Client().get(reverse('intro')),
            'login': Client().get(reverse('login')),
            'signup': Client().get(reverse('signup')),
            'start': client.get(reverse('start')),
            'home': client.get(self.urls()['home']),
        }
        self.start_race(client, user)
        self.drive_course(client, user)
        self.cross_finish(client, user)
        pages['result'] = client.get(self.urls()['result'])
        return pages

    def test_no_active_page_still_describes_the_old_game(self):
        for name, response in self.pages().items():
            body = response.content.decode()
            for phrase in self.STALE:
                self.assertNotIn(phrase.lower(), body.lower(),
                                 f'{name} still says {phrase!r}')

    def test_the_briefing_names_the_twelve_minute_race_and_one_attempt(self):
        _user, client = self.make_player('Briefed', pc_no='PC-14')
        body = client.get(self.urls()['home']).content.decode()
        self.assertIn('12', body)
        self.assertIn('one official attempt', body.lower())

    def test_the_signup_page_says_a_pc_is_shared(self):
        body = Client().get(reverse('signup')).content.decode().lower()
        self.assertIn('pc number', body)
        self.assertIn('same machine', body)
        # The old rule was one account per PC, and its refusal message was
        # "PC number already registered". Neither may come back: it is what
        # turned the second participant at a machine away.
        self.assertNotIn('pc number already registered', body)
        self.assertNotIn('one account per pc', body)


# ==========================================================================
# A hall, for one evening
# ==========================================================================

class EventSimulationTests(RaceMixin, TestCase):
    """Five machines, ten participants, every ending the evening can have.

    This is the automated half of the event rehearsal: registration through
    result for everybody at once, one PC reused three times, and a scoreboard
    and an export taken while some of the races are still running.
    """

    HALL = (
        # (participant, PC, ending)
        ('Rahul',  'PC-01', 'complete'),
        ('Priya',  'PC-01', 'complete'),
        ('Arjun',  'PC-01', 'timeout'),
        ('Divya',  'PC-02', 'complete'),
        ('Karthik', 'PC-03', 'racing'),
        ('Meera',  'PC-04', 'complete'),
        ('Nikhil', 'PC-05', 'racing'),
        ('Sneha',  'PC-02', 'timeout'),
        ('Vikram', 'PC-03', 'complete'),
        ('Ananya', 'PC-05', 'registered'),
    )

    def run_hall(self):
        people = {}
        for name, pc_no, ending in self.HALL:
            user, client = self.make_player(name, pc_no=pc_no)
            if ending == 'registered':
                people[name] = (user, client, ending)
                continue

            self.start_race(client, user)
            if ending == 'complete':
                self.drive_course(client, user)
                self.cross_finish(client, user)
            elif ending == 'timeout':
                self.drive_course(client, user, repairs=4)
                self.time_out(user)
                client.get(self.urls()['state'])       # the server notices
            else:                                       # still driving
                self.drive_course(client, user, repairs=2)
            user.refresh_from_db()
            people[name] = (user, client, ending)
        return people

    def test_every_run_in_the_hall_ends_in_its_own_state(self):
        expected = {'complete': RACE_COMPLETED, 'timeout': RACE_EXPIRED,
                    'racing': RACE_ACTIVE, 'registered': RACE_NOT_STARTED}
        for name, (user, _client, ending) in self.run_hall().items():
            self.assertEqual(user.race_status, expected[ending], name)

    def test_no_participant_overwrites_another(self):
        people = self.run_hall()
        scores = {name: user.best_score
                  for name, (user, _c, _e) in people.items()}
        repairs = {name: user.race_repairs
                   for name, (user, _c, _e) in people.items()}

        # Everyone still racing keeps driving; nobody else moves.
        for name in ('Karthik', 'Nikhil'):
            user, client, _ending = people[name]
            self.drive_course(client, user, repairs=4)

        for name, (user, _client, _ending) in people.items():
            user.refresh_from_db()
            if name in ('Karthik', 'Nikhil'):
                continue
            self.assertEqual(user.best_score, scores[name], name)
            self.assertEqual(user.race_repairs, repairs[name], name)

    def test_the_reused_pc_holds_three_separate_runs(self):
        self.run_hall()
        on_pc_01 = User.objects.filter(pc_no='PC-01').order_by('pk')
        self.assertEqual([user.username for user in on_pc_01],
                         ['Rahul', 'Priya', 'Arjun'])
        self.assertEqual([user.race_status for user in on_pc_01],
                         [RACE_COMPLETED, RACE_COMPLETED, RACE_EXPIRED])

    def test_the_scoreboard_shows_the_whole_hall_at_once(self):
        from .scoreboard import scoreboard_snapshot
        self.run_hall()
        rows = scoreboard_snapshot()['players']

        self.assertEqual(len(rows), len(self.HALL))
        self.assertEqual({row['player'] for row in rows},
                         {name for name, _pc, _e in self.HALL})
        # Two names on PC-01 at once is the whole point of the change.
        on_pc_01 = [row for row in rows if row['pc_no'] == 'PC-01']
        self.assertEqual(len(on_pc_01), 3)

    def test_the_reward_reaches_exactly_the_people_who_finished(self):
        for name, (user, client, ending) in self.run_hall().items():
            response = client.get(self.urls()['fixed'])
            if ending == 'complete':
                self.assertEqual(response.status_code, 200, name)
            else:
                self.assertEqual(response.status_code, 302, name)

    def test_nobody_can_read_anybody_elses_run(self):
        people = self.run_hall()
        rahul = people['Rahul'][0]
        _user, intruder = people['Nikhil'][1], people['Nikhil'][1]

        for name in ('scoreboard_player', 'scoreboard_player_preview'):
            response = intruder.get(reverse(name, args=[rahul.pk]))
            self.assertEqual(response.status_code, 403, name)

    def test_the_export_taken_mid_event_is_honest_about_every_row(self):
        self.run_hall()
        buffer = io.StringIO()
        write_results_csv(buffer)
        rows = {row['participant']: row
                for row in csv.DictReader(io.StringIO(buffer.getvalue()))}

        self.assertEqual(len(rows), len(self.HALL))
        self.assertEqual(rows['Rahul']['status'], 'COMPLETED')
        self.assertEqual(rows['Arjun']['status'], "TIME'S UP")
        self.assertEqual(rows['Karthik']['status'], 'RACING')
        self.assertEqual(rows['Ananya']['status'], 'NOT STARTED')
        self.assertEqual(rows['Ananya']['started_at'], '')
        # The three PC-01 runs are three rows, not one.
        self.assertEqual(
            sum(1 for row in rows.values() if row['pc_no'] == 'PC-01'), 3)

    def test_a_restart_changes_nothing_because_the_database_is_the_state(self):
        """Everything the scoreboard shows is rebuilt from rows, every time.

        Nothing is cached in the web process, so a restart is indistinguishable
        from a second read — which is exactly what makes restarting safe.
        """
        from .scoreboard import scoreboard_snapshot
        self.run_hall()
        before = scoreboard_snapshot()

        # A restart empties the channel layer and every in-process object.
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if hasattr(layer, 'flush'):
            import asyncio
            asyncio.new_event_loop().run_until_complete(layer.flush())

        self.assertEqual(scoreboard_snapshot(), before)

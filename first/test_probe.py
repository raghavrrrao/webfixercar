"""Temporary Phase 4 probe. Deleted before the phase closes."""
import threading
from datetime import timedelta

from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from .game_config import (GAME_DURATION_SECONDS, RACE_COURSE_METRES,
                          RACE_REPAIR_METRES)
from .models import User
from .repairs import REPAIR_IDS


class CsrfRealFlowProbe(TestCase):
    def test_signup_then_race_page(self):
        c = Client(enforce_csrf_checks=True)
        c.get('/signup/')
        print('\nafter GET /signup/ cookie:', 'csrftoken' in c.cookies)
        token = c.cookies['csrftoken'].value
        r = c.post('/signup/', {'username': 'Zed', 'pc_no': 'PC-9',
                                'password': 'pw-123456',
                                'csrfmiddlewaretoken': token})
        print('signup ->', r.status_code, getattr(r, 'url', ''))
        print('cookie after signup:', 'csrftoken' in c.cookies)
        home = c.get('/home/')
        print('/home/ ok:', home.status_code)
        # simulate the cookie being absent when the race page posts
        del c.cookies['csrftoken']
        r = c.post('/api/race/start/')
        print('START RACE with no csrf cookie ->', r.status_code)


class ConcurrentCompletionProbe(TransactionTestCase):
    reset_sequences = True

    def test_two_simultaneous_completions(self):
        u = User.objects.create_user(username='Racer', pc_no='PC-8',
                                     password='pw-123456')
        u.start_challenge()
        User.objects.filter(pk=u.pk).update(
            race_started_at=timezone.now() - timedelta(seconds=400))
        u.refresh_from_db()
        for i, rid in enumerate(REPAIR_IDS):
            u.race_distance = RACE_REPAIR_METRES[i]
            u.save(update_fields=['race_distance'])
            u.record_repair(rid)
        User.objects.filter(pk=u.pk).update(race_distance=RACE_COURSE_METRES)

        results = []

        def finish():
            c = Client()
            c.force_login(u)
            try:
                r = c.post('/api/race/complete/')
                results.append((r.status_code, r.json().get('score')))
            finally:
                connection.close()

        threads = [threading.Thread(target=finish) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        u.refresh_from_db()
        from .models import FinalSubmission
        print('\nCONCURRENT results:', sorted(results))
        print('completions recorded:', User.objects.filter(
            pk=u.pk, race_completed_at__isnull=False).count())
        print('FinalSubmission rows:', FinalSubmission.objects.filter(user=u).count())
        print('final score:', u.best_score)


class ExpiryBroadcastProbe(TransactionTestCase):
    def test_expiry_broadcast_count(self):
        from unittest import mock
        u = User.objects.create_user(username='Slow', pc_no='PC-7',
                                     password='pw-123456')
        u.start_challenge()
        User.objects.filter(pk=u.pk).update(
            race_started_at=timezone.now() - timedelta(
                seconds=GAME_DURATION_SECONDS + 10),
            game_start_time=timezone.now() - timedelta(
                seconds=GAME_DURATION_SECONDS + 10))
        c = Client()
        c.force_login(u)
        with mock.patch('first.scoreboard._broadcast_after_commit') as bc:
            for _ in range(4):
                c.get('/api/race/state/')
            print('\nEXPIRY broadcasts from 4 state reads:', bc.call_count,
                  [call.args for call in bc.call_args_list])


class ProgressAfterCompletionProbe(TestCase):
    def test_apis_after_completion(self):
        u = User.objects.create_user(username='Done', pc_no='PC-6',
                                     password='pw-123456')
        now = timezone.now()
        User.objects.filter(pk=u.pk).update(
            race_started_at=now - timedelta(seconds=300),
            game_start_time=now - timedelta(seconds=300),
            race_completed_at=now, completed_at=now, best_score=800,
            race_distance=RACE_COURSE_METRES, race_time_seconds=300)
        u.refresh_from_db()
        c = Client()
        c.force_login(u)
        for path, data in (('/api/race/start/', {}),
                           ('/api/race/progress/', {'distance': '999999'}),
                           ('/api/race/progress/', {'collisions': '20'}),
                           ('/api/race/complete/', {})):
            r = c.post(path, data)
            print(f'\nAFTER COMPLETE {path} {data} -> {r.status_code}')
        u.refresh_from_db()
        print('score still', u.best_score, 'collisions', u.race_collisions,
              'distance', u.race_distance)


class SnapshotQueryProbe(TestCase):
    def test_snapshot_query_count(self):
        for i in range(30):
            u = User.objects.create_user(username=f'P{i}', pc_no=f'PC-{i%5}',
                                         password='pw-123456')
            u.start_challenge()
        from .scoreboard import scoreboard_snapshot
        with self.assertNumQueries(1):
            snap = scoreboard_snapshot()
        print('\nSNAPSHOT rows:', len(snap['players']))

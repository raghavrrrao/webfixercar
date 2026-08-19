"""
Event-day hardening: the things that only matter once real people are using
real machines.

Three concerns live here. **Shared hardware** — the event runs on lab PCs that
pass from one participant to the next, so a page served to one person must not
still be sitting in the browser cache for the next. **The results** — one
export, containing every run and no credential, that the organisers judge
from. **The reset** — the most destructive operation in the project, which must
be impossible to fire by accident and must never be the thing that loses a
result.
"""

import csv
import io
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from .models import RACE_COMPLETED, RACE_EXPIRED, FinalSubmission, User
from .repairs import REPAIR_COUNT
from .scoreboard import EXPORT_COLUMNS, export_rows
from .test_race import RaceMixin


class EventMixin(RaceMixin):
    def make_organiser(self, name='organiser'):
        staff = User.objects.create_superuser(
            username=name, pc_no='PC-DESK', password='pw-123456')
        client = Client()
        client.force_login(staff, backend='first.backends.ParticipantBackend')
        return staff, client


# ==========================================================================
# Shared machines: nothing private may linger in a browser cache
# ==========================================================================

class SharedMachineCacheTests(EventMixin, TestCase):
    """The next participant must not be able to press Back into the last one.

    Every server-side check in the project is correct — but a response that
    was *legitimately* served before the logout can still be replayed from the
    browser's own cache, and logging out cannot reach in and clean that up.
    """

    def test_a_participants_pages_are_never_cached(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)

        for url in (self.urls()['home'], self.urls()['state']):
            response = client.get(url)
            self.assertIn('no-store', response['Cache-Control'], url)
            self.assertIn('Cookie', response.get('Vary', ''), url)

    def test_a_finished_result_page_is_never_cached(self):
        _user, client, _response = self.full_race('Rahul')
        response = client.get(self.urls()['result'])
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])

    def test_the_organiser_screens_are_never_cached(self):
        participant, _client = self.make_player('Rahul')
        _staff, organiser = self.make_organiser()

        for url in (reverse('scoreboard'), reverse('scoreboard_display'),
                    reverse('scoreboard_player', args=[participant.pk])):
            response = organiser.get(url)
            self.assertIn('no-store', response['Cache-Control'], url)

    def test_pages_that_choose_their_own_caching_are_left_alone(self):
        """The NovaCloud previews already say no-store; don't fight them."""
        _user, client, _response = self.full_race('Rahul')
        response = client.get(self.urls()['fixed'])
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_anonymous_pages_are_not_touched(self):
        anonymous = Client()
        response = anonymous.get(reverse('intro'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('no-store', response.get('Cache-Control', ''))

    def test_the_next_participant_on_that_pc_starts_clean(self):
        rahul, browser = self.full_race('Rahul', pc_no='PC-14')[0], None
        rahul_client = Client()
        rahul_client.force_login(rahul, backend='first.backends.ParticipantBackend')
        rahul_client.get(reverse('logout'))

        # the same browser, now the next person
        response = rahul_client.post(reverse('signup'), {
            'username': 'Priya', 'pc_no': 'PC-14', 'password': 'pw-123456'})
        self.assertRedirects(response, reverse('start'))

        state = rahul_client.get(self.urls()['state']).json()
        self.assertEqual(state['score'], 0)
        self.assertRedirects(rahul_client.get(self.urls()['fixed']),
                             self.urls()['home'])


# ==========================================================================
# The judging sheet
# ==========================================================================

class ResultsExportTests(EventMixin, TestCase):
    def read(self, body):
        return list(csv.DictReader(io.StringIO(body)))

    def test_the_export_is_organiser_only(self):
        _participant, participant_client = self.make_player('Rahul')
        self.assertEqual(
            participant_client.get(reverse('scoreboard_export')).status_code, 403)
        self.assertEqual(Client().get(reverse('scoreboard_export')).status_code, 302)

    def test_it_downloads_a_row_for_every_run(self):
        self.full_race('Rahul', collisions=2, pc_no='PC-14')
        racing, racing_client = self.make_player('Priya', pc_no='PC-14')
        self.start_race(racing_client, racing)
        self.drive_course(racing_client, racing, repairs=3)
        _staff, organiser = self.make_organiser()

        response = organiser.get(reverse('scoreboard_export'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        self.assertIn('attachment;', response['Content-Disposition'])

        rows = {r['participant']: r for r in self.read(response.content.decode())}
        self.assertEqual(rows['Rahul']['status'], RACE_COMPLETED)
        self.assertEqual(rows['Rahul']['repairs_collected'], str(REPAIR_COUNT))
        self.assertEqual(rows['Rahul']['penalties'], '2')
        self.assertEqual(rows['Rahul']['eligible'], 'yes')
        self.assertEqual(rows['Priya']['repairs_collected'], '3')
        self.assertEqual(rows['Priya']['eligible'], 'no')

    def test_a_reused_pc_produces_one_row_per_participant(self):
        """Three people at PC-14 is three results, not one."""
        self.full_race('Rahul', pc_no='PC-14')
        self.full_race('Priya', pc_no='PC-14')
        arjun, arjun_client = self.make_player('Arjun', pc_no='PC-14')
        self.start_race(arjun_client, arjun)
        self.drive_course(arjun_client, arjun, repairs=4)
        self.time_out(arjun)
        arjun_client.get(self.urls()['state'])

        rows = [r for r in export_rows() if r['pc_no'] == 'PC-14']
        self.assertEqual(len(rows), 3)
        self.assertEqual({r['participant'] for r in rows},
                         {'Rahul', 'Priya', 'Arjun'})
        by_name = {r['participant']: r for r in rows}
        self.assertEqual(by_name['Arjun']['status'], RACE_EXPIRED)
        self.assertNotEqual(by_name['Rahul']['score'], 0)

    def test_the_export_carries_no_credential(self):
        user, _client, _response = self.full_race('Rahul')
        _staff, organiser = self.make_organiser()
        body = organiser.get(reverse('scoreboard_export')).content.decode()

        self.assertNotIn(user.password, body)
        self.assertNotIn('pbkdf2', body)
        for column in ('password', 'session', 'token', 'last_login'):
            self.assertNotIn(column, body.lower(), column)

    def test_the_columns_are_an_explicit_list(self):
        """Adding a model field must not silently start publishing it."""
        self.make_player('Rahul')
        rows = list(export_rows())
        self.assertEqual(set(rows[0]), set(EXPORT_COLUMNS))

    def test_it_names_no_winner(self):
        self.full_race('Rahul')
        self.full_race('Priya')
        _staff, organiser = self.make_organiser()
        body = organiser.get(reverse('scoreboard_export')).content.decode().lower()

        # 'position' is deliberately absent: it is a CSS repair id, not a placing
        for word in ('rank', 'winner', 'placing', '1st', '2nd'):
            self.assertNotIn(word, body, word)

    def test_the_management_command_writes_the_same_sheet(self):
        self.full_race('Rahul', pc_no='PC-14')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'results.csv'
            call_command('export_results', out=str(target))
            rows = self.read(target.read_text(encoding='utf-8'))

        self.assertEqual([r['participant'] for r in rows], ['Rahul'])
        self.assertEqual(rows[0]['pc_no'], 'PC-14')


# ==========================================================================
# The reset, and how hard it is to fire by accident
# ==========================================================================

class EventResetTests(EventMixin, TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.archive = Path(self.folder.name) / 'archive.csv'

    def test_it_refuses_without_confirmation_and_changes_nothing(self):
        user, _client, _response = self.full_race('Rahul')

        with self.assertRaises(CommandError):
            call_command('reset_event', archive=str(self.archive))

        self.assertFalse(self.archive.exists(), 'nothing should be written either')
        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_COMPLETED)

    def test_it_archives_before_it_clears(self):
        user, _client, _response = self.full_race('Rahul', collisions=1, pc_no='PC-14')
        score = user.best_score

        call_command('reset_event', archive=str(self.archive), yes=True)

        rows = list(csv.DictReader(io.StringIO(
            self.archive.read_text(encoding='utf-8'))))
        self.assertEqual(rows[0]['participant'], 'Rahul')
        self.assertEqual(rows[0]['score'], str(score))
        self.assertEqual(rows[0]['pc_no'], 'PC-14')

        # ...and only then is the race cleared
        user.refresh_from_db()
        self.assertIsNone(user.race_started_at)
        self.assertIsNone(user.race_completed_at)
        self.assertEqual(user.repair_ids, [])
        self.assertEqual(user.best_score, 0)
        self.assertFalse(FinalSubmission.objects.filter(user=user).exists())

    def test_it_will_not_overwrite_an_existing_archive(self):
        self.archive.write_text('an earlier event', encoding='utf-8')
        user, _client, _response = self.full_race('Rahul')

        with self.assertRaises(CommandError):
            call_command('reset_event', archive=str(self.archive), yes=True)

        self.assertEqual(self.archive.read_text(encoding='utf-8'), 'an earlier event')
        user.refresh_from_db()
        self.assertEqual(user.race_status, RACE_COMPLETED)

    def test_clearing_leaves_the_accounts_able_to_race_again(self):
        user, client, _response = self.full_race('Rahul')
        call_command('reset_event', archive=str(self.archive), yes=True)

        response = client.post(self.urls()['start'], {})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertIsNotNone(user.race_started_at)

    def test_deleting_participants_never_touches_an_organiser(self):
        self.full_race('Rahul')
        staff, _organiser = self.make_organiser()

        call_command('reset_event', archive=str(self.archive), yes=True,
                     delete_participants=True)

        self.assertFalse(User.objects.filter(username='Rahul').exists())
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())

    def test_one_participant_can_be_reset_without_touching_the_others(self):
        spoiled, _c1, _r1 = self.full_race('Rahul', pc_no='PC-14')
        keeper, _c2, _r2 = self.full_race('Priya', pc_no='PC-14')
        recorded = (keeper.best_score, keeper.race_completed_at)

        call_command('reset_race', 'Rahul', yes=True)

        spoiled.refresh_from_db()
        keeper.refresh_from_db()
        self.assertIsNone(spoiled.race_started_at)
        self.assertEqual((keeper.best_score, keeper.race_completed_at), recorded)


# ==========================================================================
# The shape of the database the event depends on
# ==========================================================================

class DatabaseIntegrityTests(EventMixin, TestCase):
    def test_the_participant_name_is_unique_and_the_pc_number_is_not(self):
        name_field = User._meta.get_field('username')
        pc_field = User._meta.get_field('pc_no')

        self.assertTrue(name_field.unique, 'the participant is the identity')
        self.assertFalse(pc_field.unique, 'a PC is used by many participants')
        self.assertEqual(User.USERNAME_FIELD, 'username')

    def test_the_database_actually_enforces_it(self):
        """Not just the model: the constraint has to be in the schema."""
        with connection.cursor() as cursor:
            indexes = connection.introspection.get_constraints(
                cursor, User._meta.db_table)

        unique_columns = [tuple(c['columns']) for c in indexes.values()
                          if c.get('unique')]
        self.assertIn(('username',), unique_columns)
        self.assertNotIn(('pc_no',), unique_columns)

    def test_a_race_belongs_to_a_participant_row(self):
        user, client = self.make_player('Rahul', pc_no='PC-14')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=2)

        # everything the race records lives on the participant, not the PC
        for field in ('race_started_at', 'race_repairs', 'race_distance',
                      'race_collisions', 'best_score', 'race_completed_at'):
            self.assertTrue(hasattr(user, field), field)
        self.assertEqual(User.objects.filter(pc_no='PC-14').count(), 1)

    def test_deleting_a_participant_leaves_no_orphan(self):
        user, _client, _response = self.full_race('Rahul')
        pk = user.pk
        user.delete()

        self.assertFalse(FinalSubmission.objects.filter(user_id=pk).exists())
        for model in (FinalSubmission,):
            live = set(User.objects.values_list('pk', flat=True))
            self.assertFalse(model.objects.exclude(user_id__in=live).exists(),
                             model.__name__)

    def test_a_participant_has_at_most_one_recorded_run(self):
        user, _client, _response = self.full_race('Rahul')
        self.assertEqual(FinalSubmission.objects.filter(user=user).count(), 1)

        # the one-to-one is the schema's guarantee, not a convention
        field = FinalSubmission._meta.get_field('user')
        self.assertTrue(field.one_to_one)


# ==========================================================================
# What the participant is told
# ==========================================================================

class ParticipantCopyTests(EventMixin, TestCase):
    """The active pages must describe the game that actually exists."""

    STALE = ('30 minute', '30-minute', 'one account per pc',
             'pc number already registered', 'autosave',
             'reset css', 'css editor')

    def pages(self):
        user, client = self.make_player('Rahul')
        anonymous = Client()
        return {
            'intro': anonymous.get(reverse('intro')).content.decode(),
            'signup': anonymous.get(reverse('signup')).content.decode(),
            'login': anonymous.get(reverse('login')).content.decode(),
            'start': client.get(reverse('start')).content.decode(),
            'briefing': client.get(self.urls()['home']).content.decode(),
        }

    def test_no_active_page_describes_the_old_challenge(self):
        for name, body in self.pages().items():
            lowered = body.lower()
            for phrase in self.STALE:
                self.assertNotIn(phrase, lowered, f'{name} still says {phrase!r}')

    def test_the_pages_describe_the_current_rules(self):
        pages = self.pages()
        self.assertIn('one official attempt', pages['signup'].lower())
        self.assertIn('12 minute', pages['signup'].lower())
        self.assertIn('one official attempt', pages['briefing'].lower())
        # the PC number is described as a machine, not an identity
        self.assertIn('computer', pages['signup'].lower())

    def test_the_login_form_asks_for_the_participant(self):
        body = self.pages()['login']
        self.assertIn('name="username"', body)
        self.assertNotIn('name="pc_no"', body)


# ==========================================================================
# Deployment configuration
# ==========================================================================

class DeploymentConfigTests(TestCase):
    """The settings that decide whether the event survives contact with reality.

    These exercise the *branches* in `games/settings.py` by re-importing the
    module with a patched environment. They cannot prove a Redis server works
    — there is none on this machine, and the report says so — but they do prove
    the switch is wired the right way round, which is the part that gets
    silently wrong.
    """

    def reload_settings(self, **environment):
        import importlib
        from unittest import mock

        import games.settings as module
        with mock.patch.dict(os.environ, environment, clear=False):
            reloaded = importlib.reload(module)
        # leave the module as the running process expects to find it
        self.addCleanup(importlib.reload, module)
        return reloaded

    def test_without_redis_the_channel_layer_is_in_process(self):
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('REDIS_URL', None)
            reloaded = self.reload_settings()
        self.assertIn('InMemoryChannelLayer',
                      reloaded.CHANNEL_LAYERS['default']['BACKEND'])

    def test_setting_redis_url_switches_the_channel_layer(self):
        """One worker is fine in memory; several need a shared layer."""
        reloaded = self.reload_settings(REDIS_URL='redis://127.0.0.1:6379/0')
        layer = reloaded.CHANNEL_LAYERS['default']

        self.assertIn('RedisChannelLayer', layer['BACKEND'])
        self.assertEqual(layer['CONFIG']['hosts'], ['redis://127.0.0.1:6379/0'])

    def test_the_redis_backend_is_actually_installed(self):
        """Switching to it must not be the moment we discover it is missing."""
        import channels_redis.core

        self.assertTrue(hasattr(channels_redis.core, 'RedisChannelLayer'))

    def test_production_refuses_to_start_on_the_development_secret_key(self):
        from unittest import mock

        from django.core.exceptions import ImproperlyConfigured

        import importlib
        import games.settings as module
        self.addCleanup(importlib.reload, module)

        with mock.patch.dict(os.environ, {'DJANGO_DEBUG': 'false'}, clear=False):
            os.environ.pop('DJANGO_SECRET_KEY', None)
            with self.assertRaises(ImproperlyConfigured):
                importlib.reload(module)

    def test_a_real_secret_key_lets_production_start(self):
        reloaded = self.reload_settings(
            DJANGO_DEBUG='false', DJANGO_SECRET_KEY='a-real-and-secret-value')

        self.assertFalse(reloaded.DEBUG)
        self.assertTrue(reloaded.SESSION_COOKIE_SECURE)
        self.assertTrue(reloaded.CSRF_COOKIE_SECURE)

    def test_allowed_hosts_can_be_locked_down(self):
        reloaded = self.reload_settings(
            DJANGO_ALLOWED_HOSTS='fixer.example.edu, 10.0.0.5')
        self.assertEqual(reloaded.ALLOWED_HOSTS, ['fixer.example.edu', '10.0.0.5'])

    def test_the_websocket_route_is_registered_for_asgi(self):
        from games.asgi import application
        from first.routing import websocket_urlpatterns

        paths = [str(route.pattern) for route in websocket_urlpatterns]
        self.assertIn('ws/scoreboard/', paths)
        self.assertIn('ws/presence/', paths)
        self.assertIsNotNone(application)

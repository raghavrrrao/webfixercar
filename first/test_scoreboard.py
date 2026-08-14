"""
The live scoreboard: an observation layer, and nothing more.

Two properties matter here and everything below is in service of them.

**The database is the only source of truth.** The scoreboard holds no score,
no clock and no status of its own; every snapshot and every event is rebuilt
from participant rows, which is why a restarted process or an empty channel
layer costs a race nothing.

**Watching cannot change what is watched.** No amount of connecting,
disconnecting, reconnecting or opening a detail page may move a participant's
race one metre — and no organiser view may hand a participant the fixed
website their own run has not earned.
"""

import json
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from games.asgi import application as asgi_application

from .game_config import GAME_DURATION_SECONDS, RACE_COURSE_METRES, RACE_REPAIR_METRES
from .models import (
    RACE_ACTIVE,
    RACE_COMPLETED,
    RACE_EXPIRED,
    RACE_NOT_STARTED,
    FinalSubmission,
    User,
)
from .repairs import REPAIR_COUNT, REPAIR_IDS
from .scoreboard import SCOREBOARD_GROUP, player_payload, scoreboard_snapshot
from .test_race import RaceMixin


class ScoreboardMixin(RaceMixin):
    """Race helpers, plus an organiser and a socket to watch them with."""

    def make_organiser(self, name='organiser'):
        staff = User.objects.create_superuser(
            username=name, pc_no='PC-DESK', password='pw-123456')
        client = Client()
        client.force_login(staff, backend='first.backends.ParticipantBackend')
        return staff, client

    @staticmethod
    def socket(user=None):
        """A scoreboard socket carrying `user`'s session, as a browser would."""
        communicator = WebsocketCommunicator(asgi_application, '/ws/scoreboard/')
        communicator.scope['user'] = user
        return communicator


# ==========================================================================
# The payload: derived from participant rows, and safe to publish
# ==========================================================================

class ScoreboardPayloadTests(ScoreboardMixin, TestCase):
    def test_a_row_is_built_from_the_participants_own_record(self):
        user, client = self.make_player('Rahul', pc_no='PC-14')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=3, collisions=2)

        payload = player_payload(user)
        state = payload['state']

        self.assertEqual(payload['player'], 'Rahul')
        self.assertEqual(payload['pc_no'], 'PC-14')
        self.assertEqual(state['status'], RACE_ACTIVE)
        self.assertEqual(state['repairs'], 3)
        self.assertEqual(state['repair_total'], REPAIR_COUNT)
        self.assertEqual(state['penalties'], 2)
        self.assertEqual(state['distance'], user.race_distance)
        self.assertEqual(state['score'], user.best_score or state['score'])

    def test_the_row_identifies_the_participant_not_the_pc(self):
        """PC-14 is a desk. Three people race at it; each is their own row."""
        rahul, _c = self.make_player('Rahul', pc_no='PC-14')
        priya, _c2 = self.make_player('Priya', pc_no='PC-14')

        self.assertNotEqual(player_payload(rahul)['participant_id'],
                            player_payload(priya)['participant_id'])
        self.assertEqual(player_payload(rahul)['participant_id'], rahul.pk)
        self.assertEqual(player_payload(priya)['pc_no'], 'PC-14')

    def test_no_credential_or_database_internal_is_ever_published(self):
        user, _client = self.make_player('Rahul', pc_no='PC-14')
        blob = json.dumps(player_payload(user))

        self.assertNotIn(user.password, blob)
        self.assertNotIn('pbkdf2', blob)
        for leaked in ('password', 'session', 'token', 'email', 'last_login'):
            self.assertNotIn(leaked, blob.lower(), leaked)

    def test_a_finished_row_freezes_at_the_recorded_result(self):
        user, _client, _response = self.full_race('Rahul', collisions=1)
        first = player_payload(user)['state']

        # ...and stays there however much later anybody looks
        self.at(user, GAME_DURATION_SECONDS + 900)
        later = player_payload(user)['state']

        self.assertEqual(first['status'], RACE_COMPLETED)
        self.assertEqual(later['status'], RACE_COMPLETED)
        self.assertEqual(later['elapsed'], user.race_time_seconds)
        self.assertEqual(later['score'], user.best_score)
        self.assertEqual(later['section_label'], 'FINISHED')

    def test_a_timed_out_row_freezes_at_the_full_duration(self):
        user, client = self.make_player('Arjun')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=2)
        self.time_out(user)

        state = player_payload(user)['state']
        self.assertEqual(state['status'], RACE_EXPIRED)
        self.assertEqual(state['elapsed'], GAME_DURATION_SECONDS)
        self.assertEqual(state['remaining'], 0)
        self.assertEqual(state['repairs'], 2)

    def test_the_snapshot_rebuilds_every_row_from_the_database(self):
        self.full_race('Done')
        racing, racing_client = self.make_player('Racing')
        self.start_race(racing_client, racing)
        self.make_player('Waiting')
        self.make_organiser()

        snapshot = scoreboard_snapshot()
        names = [row['player'] for row in snapshot['players']]

        self.assertEqual(snapshot['type'], 'scoreboard_snapshot')
        self.assertIn('Done', names)
        self.assertIn('Racing', names)
        self.assertIn('Waiting', names)
        self.assertNotIn('organiser', names, 'organisers are not participants')

    def test_the_snapshot_orders_for_monitoring_without_ranking_anybody(self):
        self.full_race('Finished')
        racing, racing_client = self.make_player('Racing')
        self.start_race(racing_client, racing)
        self.make_player('Waiting')
        expired, expired_client = self.make_player('Expired')
        self.start_race(expired_client, expired)
        self.time_out(expired)

        statuses = [row['state']['status'] for row in scoreboard_snapshot()['players']]
        self.assertEqual(
            statuses, [RACE_COMPLETED, RACE_ACTIVE, RACE_NOT_STARTED, RACE_EXPIRED])

        # ...and nothing in the payload calls anybody first, second or third
        blob = json.dumps(scoreboard_snapshot()).lower()
        for word in ('rank', 'place', 'winner', '1st', 'position'):
            self.assertNotIn(word, blob, word)


# ==========================================================================
# Access control
# ==========================================================================

class ScoreboardAccessTests(ScoreboardMixin, TestCase):
    def urls_under_test(self, participant):
        return (
            reverse('scoreboard'),
            reverse('scoreboard_display'),
            reverse('scoreboard_player', args=[participant.pk]),
            reverse('scoreboard_player_preview', args=[participant.pk]),
        )

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self):
        participant, _client = self.make_player('Rahul')
        anonymous = Client()
        for url in self.urls_under_test(participant):
            self.assertEqual(anonymous.get(url).status_code, 302, url)

    def test_a_participant_cannot_open_any_organiser_view(self):
        participant, participant_client = self.make_player('Rahul')
        for url in self.urls_under_test(participant):
            self.assertEqual(participant_client.get(url).status_code, 403, url)

    def test_a_participant_cannot_reach_their_own_detail_page(self):
        """Not even their own row: this is the organisers' screen."""
        participant, participant_client = self.make_player('Rahul')
        response = participant_client.get(
            reverse('scoreboard_player', args=[participant.pk]))
        self.assertEqual(response.status_code, 403)

    def test_an_organiser_can_open_every_view(self):
        participant, _client = self.make_player('Rahul')
        _staff, organiser = self.make_organiser()
        for url in self.urls_under_test(participant):
            self.assertEqual(organiser.get(url).status_code, 200, url)

    def test_authorisation_is_the_flag_the_admin_already_uses(self):
        """No new role system, and nothing the browser sends is trusted."""
        participant, participant_client = self.make_player('Rahul')
        forged = participant_client.get(
            reverse('scoreboard'), {'organizer': 'true', 'role': 'admin'},
            HTTP_X_ORGANISER='true')
        self.assertEqual(forged.status_code, 403)

        participant.is_admin = True
        participant.save(update_fields=['is_admin'])
        self.assertEqual(participant_client.get(reverse('scoreboard')).status_code, 200)

    def test_the_page_carries_no_secret(self):
        self.make_player('Rahul')
        _staff, organiser = self.make_organiser()
        body = organiser.get(reverse('scoreboard')).content.decode()

        self.assertNotIn('password', body.lower())
        self.assertNotIn('pbkdf2', body)
        self.assertNotIn('SECRET', body)


# ==========================================================================
# The socket
# ==========================================================================

class ScoreboardSocketTests(ScoreboardMixin, TransactionTestCase):
    """Connection, authorisation and the snapshot handed to a new client."""

    def test_an_organiser_gets_a_welcome_and_a_full_snapshot(self):
        staff, _client = self.make_organiser()
        self.full_race('Done')
        racing, racing_client = self.make_player('Racing')
        self.start_race(racing_client, racing)

        async def run():
            communicator = self.socket(staff)
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            welcome = await communicator.receive_json_from(timeout=5)
            self.assertEqual(welcome['type'], 'welcome')
            self.assertGreater(welcome['heartbeat'], 0)

            snapshot = await communicator.receive_json_from(timeout=5)
            self.assertEqual(snapshot['type'], 'scoreboard_snapshot')
            names = [row['player'] for row in snapshot['players']]
            self.assertIn('Done', names)
            self.assertIn('Racing', names)
            await communicator.disconnect()

        async_to_sync(run)()

    def test_a_participant_is_refused_the_socket(self):
        participant, _client = self.make_player('Rahul')

        async def run():
            communicator = self.socket(participant)
            connected, code = await communicator.connect()
            self.assertFalse(connected)
            self.assertEqual(code, 4403)

        async_to_sync(run)()

    def test_an_anonymous_socket_is_refused(self):
        from django.contrib.auth.models import AnonymousUser

        async def run():
            communicator = self.socket(AnonymousUser())
            connected, code = await communicator.connect()
            self.assertFalse(connected)
            self.assertEqual(code, 4403)

        async_to_sync(run)()

    def test_a_ping_is_answered_without_touching_any_race(self):
        staff, _client = self.make_organiser()
        racing, racing_client = self.make_player('Racing')
        self.start_race(racing_client, racing)
        before = (racing.race_started_at, racing.race_distance)

        async def run():
            communicator = self.socket(staff)
            await communicator.connect()
            await communicator.receive_json_from(timeout=5)   # welcome
            await communicator.receive_json_from(timeout=5)   # snapshot
            await communicator.send_json_to({'type': 'ping'})
            pong = await communicator.receive_json_from(timeout=5)
            self.assertEqual(pong['type'], 'pong')
            await communicator.disconnect()

        async_to_sync(run)()

        racing.refresh_from_db()
        self.assertEqual((racing.race_started_at, racing.race_distance), before)

    def test_reconnecting_rebuilds_the_board_from_the_database(self):
        """The recovery mechanism is the database, not a replay buffer."""
        staff, _client = self.make_organiser()
        racing, racing_client = self.make_player('Racing')
        self.start_race(racing_client, racing)

        async def first_visit():
            communicator = self.socket(staff)
            await communicator.connect()
            await communicator.receive_json_from(timeout=5)
            snapshot = await communicator.receive_json_from(timeout=5)
            await communicator.disconnect()
            return snapshot

        before = async_to_sync(first_visit)()
        self.assertEqual(
            [r['state']['repairs'] for r in before['players']
             if r['player'] == 'Racing'], [0])

        # the race moves on while nobody is watching
        self.drive_course(racing_client, racing, repairs=4, collisions=3)

        async def second_visit():
            communicator = self.socket(staff)
            await communicator.connect()
            await communicator.receive_json_from(timeout=5)
            snapshot = await communicator.receive_json_from(timeout=5)
            await communicator.disconnect()
            return snapshot

        after = async_to_sync(second_visit)()
        row = next(r for r in after['players'] if r['player'] == 'Racing')
        self.assertEqual(row['state']['repairs'], 4)
        self.assertEqual(row['state']['penalties'], 3)

    def test_a_scoreboard_disconnect_leaves_the_race_untouched(self):
        staff, _client = self.make_organiser()
        racing, racing_client = self.make_player('Racing')
        self.start_race(racing_client, racing)
        self.drive_course(racing_client, racing, repairs=2)
        racing.refresh_from_db()
        before = (racing.race_started_at, racing.race_repairs,
                  racing.race_distance, racing.race_collisions)

        async def run():
            for _ in range(3):
                communicator = self.socket(staff)
                await communicator.connect()
                await communicator.receive_json_from(timeout=5)
                await communicator.receive_json_from(timeout=5)
                await communicator.disconnect()

        async_to_sync(run)()

        racing.refresh_from_db()
        self.assertEqual((racing.race_started_at, racing.race_repairs,
                          racing.race_distance, racing.race_collisions), before)
        # ...and the race carries on afterwards exactly as before
        self.assertEqual(self.collect(racing_client, racing, 2).status_code, 200)


# ==========================================================================
# Broadcasts: one per committed race change
# ==========================================================================

class ScoreboardBroadcastTests(ScoreboardMixin, TransactionTestCase):
    """Every event the race emits, caught off the channel layer."""

    def setUp(self):
        self.layer = get_channel_layer()
        self.received = []

    def listen(self, action):
        """Run `action` and collect whatever it fans out to the group.

        Subscribing to the group directly is what proves the broadcast really
        left the request — a consumer of our own could hide a mistake in how
        the event is addressed. Draining has a timeout because an empty
        channel otherwise blocks forever, and "nothing was sent" is a result
        several of these tests are specifically looking for.
        """
        import asyncio

        async def drain():
            channel = await self.layer.new_channel()
            await self.layer.group_add(SCOREBOARD_GROUP, channel)
            try:
                await database_sync(action)
                messages = []
                while True:
                    try:
                        message = await asyncio.wait_for(
                            self.layer.receive(channel), timeout=0.4)
                    except asyncio.TimeoutError:
                        break
                    messages.append(message['payload'])
                return messages
            finally:
                await self.layer.group_discard(SCOREBOARD_GROUP, channel)

        return async_to_sync(drain)()

    def test_starting_a_race_announces_it(self):
        user, client = self.make_player('Rahul')
        events = self.listen(lambda: self.start_race(client, user))

        self.assertTrue(events)
        started = [e for e in events if e['event'] == 'race_started']
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]['type'], 'race_update')
        self.assertEqual(started[0]['participant_id'], user.pk)
        self.assertEqual(started[0]['player'], 'Rahul')
        self.assertEqual(started[0]['state']['status'], RACE_ACTIVE)

    def test_a_resume_does_not_announce_a_second_start(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)
        events = self.listen(lambda: client.post(self.urls()['start'], {}))
        self.assertEqual([e for e in events if e['event'] == 'race_started'], [])

    def test_collecting_a_repair_announces_it(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)
        events = self.listen(lambda: self.collect(client, user, 0))

        repairs = [e for e in events if e['event'] == 'repair_collected']
        self.assertTrue(repairs)
        self.assertEqual(repairs[-1]['state']['repairs'], 1)

    def test_a_collision_announces_itself(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)
        events = self.listen(
            lambda: client.post(self.urls()['progress'], {'collisions': 1}))

        collisions = [e for e in events if e['event'] == 'collision']
        self.assertTrue(collisions)
        self.assertEqual(collisions[-1]['state']['penalties'], 1)

    def test_progress_announces_itself(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)
        events = self.listen(lambda: self.drive_to(client, user, 900))

        progress = [e for e in events if e['event'] == 'race_progress']
        self.assertTrue(progress)
        self.assertEqual(progress[-1]['state']['distance'], 900)

    def test_completing_announces_the_finished_result(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)
        self.drive_course(client, user, collisions=1)
        events = self.listen(lambda: self.cross_finish(client, user))

        completed = [e for e in events if e['event'] == 'race_completed']
        self.assertEqual(len(completed), 1)
        user.refresh_from_db()
        self.assertEqual(completed[0]['state']['status'], RACE_COMPLETED)
        self.assertEqual(completed[0]['state']['score'], user.best_score)
        self.assertEqual(completed[0]['state']['repairs'], REPAIR_COUNT)

    def test_a_timeout_announces_itself_once_and_only_once(self):
        user, client = self.make_player('Arjun')
        self.start_race(client, user)
        self.time_out(user)

        first = self.listen(lambda: client.get(self.urls()['state']))
        expired = [e for e in first if e['event'] == 'race_expired']
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]['state']['status'], RACE_EXPIRED)

        # every later read of the same expired race stays quiet
        again = self.listen(lambda: client.get(self.urls()['state']))
        self.assertEqual([e for e in again if e['event'] == 'race_expired'], [])

    def test_an_event_carries_an_id_a_client_can_deduplicate_on(self):
        user, client = self.make_player('Rahul')
        events = self.listen(lambda: self.start_race(client, user))
        started = [e for e in events if e['event'] == 'race_started'][0]

        self.assertIn('event_id', started)
        self.assertIn(str(user.pk), started['event_id'])
        self.assertIn('race_started', started['event_id'])

    def test_nothing_is_announced_for_a_refused_request(self):
        """A broadcast must never describe a state that was not committed."""
        user, client = self.make_player('Rahul')
        self.start_race(client, user)

        # a repair the participant has not driven to
        events = self.listen(
            lambda: client.post(self.urls()['progress'], {'repair': REPAIR_IDS[3]}))
        self.assertEqual([e for e in events if e['event'] == 'repair_collected'], [])

        user.refresh_from_db()
        self.assertEqual(user.repair_ids, [])


def database_sync(action):
    """Run a synchronous ORM/test-client action from inside async code."""
    from channels.db import database_sync_to_async
    return database_sync_to_async(action)()


# ==========================================================================
# The organiser's views
# ==========================================================================

class ScoreboardViewTests(ScoreboardMixin, TestCase):
    def setUp(self):
        self.staff, self.organiser = self.make_organiser()

    def test_the_monitor_renders_every_participant(self):
        self.full_race('Rahul', pc_no='PC-14')
        racing, racing_client = self.make_player('Priya', pc_no='PC-14')
        self.start_race(racing_client, racing)

        body = self.organiser.get(reverse('scoreboard')).content.decode()
        self.assertIn('REAL-TIME RACE MONITOR', body)
        self.assertIn('Rahul', body)
        self.assertIn('Priya', body)
        self.assertIn('PC-14', body)
        # ...and it says outright that the order is not a placing
        collapsed = ' '.join(body.split())
        self.assertIn('not</strong> a placing', collapsed)
        self.assertIn('one overall winner', collapsed)

    def test_the_monitor_ships_a_snapshot_so_the_table_is_never_blank(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)

        body = self.organiser.get(reverse('scoreboard')).content.decode()
        boot = json.loads(
            body.split('id="wf-scoreboard-boot" type="application/json">')[1]
                .split('</script>')[0])

        self.assertEqual(boot['socket'], '/ws/scoreboard/')
        self.assertEqual(boot['snapshot']['type'], 'scoreboard_snapshot')
        self.assertIn('Rahul', [r['player'] for r in boot['snapshot']['players']])

    def test_the_projector_view_is_its_own_screen(self):
        self.full_race('Rahul')
        body = self.organiser.get(reverse('scoreboard_display')).content.decode()

        self.assertIn('LIVE RACE', body)
        self.assertIn('wf-sb-spotlight', body)
        self.assertIn('wf-sb-feed', body)
        self.assertIn('"mode": "display"', body)

    def test_the_detail_view_shows_one_participants_run(self):
        user, _client, _response = self.full_race('Rahul', collisions=2, pc_no='PC-14')
        body = self.organiser.get(
            reverse('scoreboard_player', args=[user.pk])).content.decode()

        self.assertIn('Rahul', body)
        self.assertIn('PC-14', body)
        self.assertIn('COMPLETE', body)
        self.assertIn(str(user.best_score), body)
        self.assertIn('RESPONSIVE', body)
        self.assertIn('GRID', body)
        self.assertNotIn(user.password, body)

    def test_the_detail_view_never_becomes_an_editor(self):
        user, _client = self.make_player('Rahul')
        body = self.organiser.get(
            reverse('scoreboard_player', args=[user.pk])).content.decode()

        self.assertNotIn('<form', body)
        self.assertNotIn('<input', body)
        self.assertIn('/admin/', body, 'administrative work stays in the admin')

    def test_opening_a_detail_page_changes_no_race(self):
        user, client = self.make_player('Rahul')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=3, collisions=1)
        user.refresh_from_db()
        before = (user.race_started_at, user.race_repairs, user.race_distance,
                  user.race_collisions, user.best_score, user.race_completed_at)

        for _ in range(3):
            self.organiser.get(reverse('scoreboard_player', args=[user.pk]))
            self.organiser.get(reverse('scoreboard_player_preview', args=[user.pk]))
            self.organiser.get(reverse('scoreboard'))

        user.refresh_from_db()
        self.assertEqual((user.race_started_at, user.race_repairs, user.race_distance,
                          user.race_collisions, user.best_score,
                          user.race_completed_at), before)


# ==========================================================================
# The preview an organiser sees, and the reward a participant has not earned
# ==========================================================================

class ScoreboardPreviewTests(ScoreboardMixin, TestCase):
    def setUp(self):
        self.staff, self.organiser = self.make_organiser()

    def preview(self, participant):
        response = self.organiser.get(
            reverse('scoreboard_player_preview', args=[participant.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_completed_run_shows_the_website_they_rebuilt(self):
        user, _client, _response = self.full_race('Rahul')
        body = self.preview(user)

        self.assertIn('line-height: 1.6;', body)
        self.assertIn('@media (max-width: 860px)', body)
        self.assertIn('.navbar {\n  display: flex;', body)

    def test_a_race_in_progress_shows_exactly_what_was_earned(self):
        user, client = self.make_player('Priya')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=2)

        body = self.preview(user)
        self.assertIn('@media (max-width: 860px)', body)      # RESPONSIVE, earned
        self.assertIn('.navbar {\n  display: flex;', body)    # DISPLAY, earned
        self.assertNotIn('.features__grid {\n  display: grid;\n'
                         '  grid-template-columns: repeat(3, 1fr);', body)  # GRID, not

    def test_a_timed_out_run_shows_the_broken_website(self):
        user, client = self.make_player('Arjun')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=5)
        self.time_out(user)

        body = self.preview(user)
        self.assertIn('@media (min-width: 860px)', body)      # still inverted
        self.assertIn('.navbar {\n  display: block;', body)   # still stacked

    def test_an_unstarted_run_shows_the_broken_website(self):
        user, _client = self.make_player('Waiting')
        self.assertIn('.navbar {\n  display: block;', self.preview(user))

    def test_the_organiser_preview_does_not_unlock_anybodys_reward(self):
        """Watching a finished run must not open the prize for a live one."""
        finished, _c, _r = self.full_race('Rahul', pc_no='PC-14')
        racing, racing_client = self.make_player('Priya', pc_no='PC-14')
        self.start_race(racing_client, racing)

        self.preview(finished)
        self.preview(racing)

        self.assertRedirects(racing_client.get(self.urls()['fixed']),
                             self.urls()['home'])
        racing.refresh_from_db()
        self.assertEqual(racing.race_status, RACE_ACTIVE)

    def test_the_preview_is_framed_safely(self):
        user, _client, _response = self.full_race('Rahul')
        response = self.organiser.get(
            reverse('scoreboard_player_preview', args=[user.pk]))

        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        # The render sets `no-store` and the organiser guard adds the rest of
        # the never-cache directives on top, so assert the property rather
        # than the exact string: this participant's website is never written
        # to a disk cache on a machine the next participant will sit at.
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])
        self.assertNotIn('<script', response.content.decode())


# ==========================================================================
# The event itself: several PCs, several participants, one board
# ==========================================================================

class MultiPcEventTests(ScoreboardMixin, TestCase):
    """The scenario the scoreboard exists for, driven through the real API."""

    def test_four_participants_on_three_pcs_are_all_represented(self):
        # PLAYER A — racing, some repairs, took a hit
        a, a_client = self.make_player('Player A', pc_no='PC-01')
        self.start_race(a_client, a)
        self.drive_course(a_client, a, repairs=5, collisions=2)

        # PLAYER B — started later, fewer repairs, on the same PC as D
        b, b_client = self.make_player('Player B', pc_no='PC-02')
        self.start_race(b_client, b)
        self.drive_course(b_client, b, repairs=3, collisions=1)

        # PLAYER C — ran out of time
        c, c_client = self.make_player('Player C', pc_no='PC-01')
        self.start_race(c_client, c)
        self.drive_course(c_client, c, repairs=4)
        self.time_out(c)
        c_client.get(self.urls()['state'])

        # PLAYER D — finished, on the PC Player B is using
        d, d_client, _response = self.full_race('Player D', pc_no='PC-02')

        board = {row['player']: row for row in scoreboard_snapshot()['players']}

        self.assertEqual(board['Player A']['state']['status'], RACE_ACTIVE)
        self.assertEqual(board['Player A']['state']['repairs'], 5)
        self.assertEqual(board['Player A']['state']['penalties'], 2)

        self.assertEqual(board['Player B']['state']['status'], RACE_ACTIVE)
        self.assertEqual(board['Player B']['state']['repairs'], 3)
        self.assertEqual(board['Player B']['state']['penalties'], 1)

        self.assertEqual(board['Player C']['state']['status'], RACE_EXPIRED)
        self.assertEqual(board['Player C']['state']['repairs'], 4)

        self.assertEqual(board['Player D']['state']['status'], RACE_COMPLETED)
        self.assertEqual(board['Player D']['state']['repairs'], REPAIR_COUNT)
        d.refresh_from_db()
        self.assertEqual(board['Player D']['state']['score'], d.best_score)

        # every participant is their own row, and the PC is only a note
        self.assertEqual(board['Player A']['pc_no'], 'PC-01')
        self.assertEqual(board['Player C']['pc_no'], 'PC-01')
        self.assertNotEqual(board['Player A']['participant_id'],
                            board['Player C']['participant_id'])

    def test_a_reused_pc_adds_a_row_and_never_overwrites_one(self):
        rahul, _c, _r = self.full_race('Rahul', pc_no='PC-14')
        first = {row['player']: row for row in scoreboard_snapshot()['players']}

        priya, priya_client = self.make_player('Priya', pc_no='PC-14')
        self.start_race(priya_client, priya)
        second = {row['player']: row for row in scoreboard_snapshot()['players']}

        self.assertEqual(len(second), len(first) + 1)
        self.assertEqual(second['Rahul']['state'], first['Rahul']['state'])
        self.assertEqual(second['Priya']['state']['status'], RACE_ACTIVE)
        self.assertEqual(second['Rahul']['pc_no'], second['Priya']['pc_no'], 'PC-14')

    def test_the_board_survives_a_restart_because_it_is_rebuilt_each_time(self):
        """Nothing is held in memory, so there is nothing a restart can lose."""
        a, a_client = self.make_player('Player A')
        self.start_race(a_client, a)
        self.drive_course(a_client, a, repairs=4, collisions=2)
        b, _bc, _r = self.full_race('Player B')

        before = scoreboard_snapshot()

        # A restart empties the channel layer and every in-process cache. The
        # snapshot is a query, so it is unaffected by both.
        from channels.layers import get_channel_layer
        get_channel_layer().flush() if hasattr(get_channel_layer(), 'flush') else None

        after = scoreboard_snapshot()
        self.assertEqual(before['players'], after['players'])

        rebuilt = {row['player']: row for row in after['players']}
        self.assertEqual(rebuilt['Player A']['state']['repairs'], 4)
        self.assertEqual(rebuilt['Player A']['state']['penalties'], 2)
        b.refresh_from_db()
        self.assertEqual(rebuilt['Player B']['state']['score'], b.best_score)

    def test_the_scoreboard_never_becomes_a_second_source_of_truth(self):
        """Every number on the board is read back out of the participant row."""
        user, client = self.make_player('Rahul')
        self.start_race(client, user)
        self.drive_course(client, user, repairs=3, collisions=2)
        user.refresh_from_db()

        row = next(r for r in scoreboard_snapshot()['players']
                   if r['player'] == 'Rahul')
        state = row['state']

        self.assertEqual(state['repairs'], user.repairs_collected)
        self.assertEqual(state['penalties'], user.race_collisions)
        self.assertEqual(state['distance'], user.race_distance)
        self.assertEqual(state['status'], user.race_status)
        self.assertEqual(row['participant_id'], user.pk)

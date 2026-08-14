"""Database-derived data and committed broadcasts for the live scoreboard.

This module deliberately owns no race fields and keeps no authoritative cache.
Every snapshot and every event is reconstructed from ``User`` records, so an
empty channel layer or a restarted web process has no effect on a race.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from .game_config import GAME_DURATION_SECONDS, RACE_COURSE_METRES
from .models import RACE_ACTIVE, RACE_COMPLETED, RACE_EXPIRED, RACE_NOT_STARTED, User
from .repairs import REPAIR_CARDS, REPAIR_COUNT


SCOREBOARD_GROUP = 'wf_scoreboard'
SCOREBOARD_HEARTBEAT_SECONDS = 20

_STATUS_ORDER = {
    RACE_COMPLETED: 0,
    RACE_ACTIVE: 1,
    RACE_NOT_STARTED: 2,
    RACE_EXPIRED: 3,
}


def _authoritative_race_state(user):
    """Use the existing race-state projection without importing it at load.

    ``views`` calls the broadcast helpers below, so importing it at module
    import time would create a cycle. The lazy import is safe in both a normal
    request and the consumer's database worker thread.
    """
    from .views import _race_state
    return _race_state(user)


def player_payload(user):
    """The public, event-safe projection of one existing participant record."""
    race = _authoritative_race_state(user)
    status = race['status']

    # The regular race endpoint continues to describe a completed player's
    # wall-clock elapsed time, and to keep quoting a live running total for an
    # attempt that has stopped. The scoreboard deliberately freezes terminal
    # rows at their recorded result; this is a presentation projection only,
    # and it never writes anything.
    #
    # The score is not frozen here: `_race_state` already reports the settled
    # figure once an attempt has stopped, so the board, the player's own
    # screen, the export and the recorded entry all quote one number.
    if status == RACE_COMPLETED:
        elapsed = user.race_time_seconds
        remaining = max(0, GAME_DURATION_SECONDS - elapsed)
        section = RACE_COMPLETED
    elif status == RACE_EXPIRED:
        elapsed = GAME_DURATION_SECONDS
        remaining = 0
        section = race['section']
    else:
        elapsed = race['elapsed']
        remaining = race['remaining']
        section = race['section']

    if section == RACE_COMPLETED:
        section_label = 'FINISHED'
    else:
        section_label = REPAIR_CARDS[section]['section']

    return {
        # The participant is the identity, and it is the account's own primary
        # key -- server-derived, stable for the whole event, and unique even
        # when three people race on PC-14 one after another. The PC number
        # rides along as metadata so organisers can see which desk a run
        # happened at; it deliberately does not identify the row.
        #
        # No credential, session, email or other database internals are sent.
        'participant_id': user.pk,
        'player': user.username or f'Participant {user.pk}',
        'pc_no': user.pc_no,
        'state': {
            'status': status,
            'elapsed': elapsed,
            'remaining': remaining,
            'duration': GAME_DURATION_SECONDS,
            'distance': race['distance'],
            'course': RACE_COURSE_METRES,
            'section': section,
            'section_label': section_label,
            'repairs': race['repairsCollected'],
            'repair_total': REPAIR_COUNT,
            'penalties': race['collisions'],
            'score': race['score'],
            'completed_at': user.race_completed_at.isoformat() if user.race_completed_at else None,
        },
    }


def _sort_key(player):
    state = player['state']
    status = state['status']
    # This is a monitoring order, not an event ranking or winner declaration.
    if status == RACE_COMPLETED:
        detail = -state['score']
    elif status == RACE_ACTIVE:
        detail = (-state['repairs'], -state['distance'])
    else:
        detail = player['player'].lower()
    return (_STATUS_ORDER[status], detail, player['player'].lower())


def scoreboard_snapshot():
    """Rebuild every visible row from the database for a new/reconnected UI."""
    players = [player_payload(user) for user in User.objects.filter(is_admin=False)]
    players.sort(key=_sort_key)
    return {'type': 'scoreboard_snapshot', 'players': players}


def scoreboard_event(user, event):
    """Build a predictable, state-based event for a committed race change."""
    player = player_payload(user)
    state = player['state']
    # It is intentionally derived from the resulting state, not a client
    # counter. Clients can harmlessly discard a duplicate after reconnect.
    event_id = ':'.join(str(part) for part in (
        event, player['participant_id'], state['status'], state['elapsed'],
        state['distance'], state['repairs'], state['penalties'], state['score'],
    ))
    return {
        'type': 'race_update',
        'event': event,
        'event_id': event_id,
        **player,
    }


def _broadcast_after_commit(user_id, event):
    """Load the committed row, then fan out the observation to scoreboard tabs."""
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        SCOREBOARD_GROUP,
        {'type': 'scoreboard.race_update', 'payload': scoreboard_event(user, event)},
    )


def schedule_scoreboard_event(user, event):
    """Broadcast only after the current database transaction has committed."""
    transaction.on_commit(lambda: _broadcast_after_commit(user.pk, event))


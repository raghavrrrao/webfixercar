"""The organisers' results export.

One row per *run*, because a run belongs to a participant and a participant
belongs to themselves. PC-14 legitimately appears three times in a day's
results — once for each person who sat at it — and each of those rows carries
its own status, time and score. Nothing here collapses them, sorts them into
placings or names a winner: the organisers compare the completed runs and pick
the one overall winner themselves.

Every figure is read from the participant record the race itself wrote. No
credential, password hash, session key or other database internal is ever
written to the file: the columns below are the whole export.
"""

import csv

from .game_config import (
    GAME_DURATION_SECONDS,
    RACE_COURSE_METRES,
    RACE_MAX_SCORE,
)
from .models import (
    RACE_ACTIVE,
    RACE_COMPLETED,
    RACE_EXPIRED,
    RACE_NOT_STARTED,
    User,
)
from .repairs import REPAIRS, REPAIR_COUNT

# What a judge actually needs in front of them, in the order they read it.
COLUMNS = (
    'participant',
    'pc_no',
    'status',
    'registered_at',
    'started_at',
    'completed_at',
    'elapsed_seconds',
    'elapsed',
    'repairs_collected',
    'repairs_total',
    'repairs',
    'penalties',
    'score',
    'score_max',
    'distance_metres',
    'course_metres',
    'section',
    'reward_unlocked',
)

_STATUS_LABEL = {
    RACE_NOT_STARTED: 'NOT STARTED',
    RACE_ACTIVE: 'RACING',
    RACE_COMPLETED: 'COMPLETED',
    RACE_EXPIRED: "TIME'S UP",
}


def _stamp(value):
    return value.isoformat(timespec='seconds') if value else ''


def _clock(seconds):
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def result_row(user):
    """One participant's run, as the organisers' spreadsheet sees it."""
    from .views import race_section

    status = user.race_status
    if status == RACE_COMPLETED:
        elapsed = user.race_time_seconds
    elif status == RACE_EXPIRED:
        elapsed = GAME_DURATION_SECONDS
    else:
        elapsed = user.elapsed_seconds

    collected = user.repair_ids
    return {
        'participant': user.username,
        'pc_no': user.pc_no,
        'status': _STATUS_LABEL.get(status, status),
        'registered_at': _stamp(user.registered_at),
        'started_at': _stamp(user._round_started_at),
        'completed_at': _stamp(user.race_completed_at),
        'elapsed_seconds': elapsed,
        'elapsed': _clock(elapsed),
        'repairs_collected': len(collected),
        'repairs_total': REPAIR_COUNT,
        'repairs': ' '.join(repair.upper() for repair in collected),
        'penalties': user.race_collisions,
        'score': user.best_score if status == RACE_COMPLETED else 0,
        'score_max': RACE_MAX_SCORE,
        'distance_metres': user.race_distance,
        'course_metres': RACE_COURSE_METRES,
        'section': ('' if status == RACE_NOT_STARTED
                    else REPAIRS[race_section(user.race_distance)]['section']),
        # The fixed website follows the server-side race state and nothing
        # else, so this column is that state, not a separate flag.
        'reward_unlocked': 'yes' if status == RACE_COMPLETED else 'no',
    }


def result_rows():
    """Every participant's run, oldest registration first.

    Registration order, not score order: it keeps the three people who used
    PC-14 in the order they used it, and it deliberately declines to rank
    anybody. Organiser accounts are not participants and are left out.
    """
    return [
        result_row(user)
        for user in User.objects.filter(is_admin=False).order_by(
            'registered_at', 'pk')
    ]


def write_results_csv(stream, rows=None):
    """Write the export to any text stream. Returns the number of rows."""
    rows = result_rows() if rows is None else rows
    writer = csv.DictWriter(stream, fieldnames=list(COLUMNS),
                           extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return len(rows)

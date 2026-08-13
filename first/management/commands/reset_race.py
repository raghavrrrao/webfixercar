"""Clear or create a participant's race attempt, from the server shell only.

One participant gets one official attempt, and nothing a participant can reach
from a browser may ever hand them a second one. That rule is not relaxed here:
this command is a *shell* tool, so using it needs access to the machine the
event runs on, which is the organiser's desk and nobody else's.

It exists for two real jobs:

    testing      -- a developer needs a clean attempt to try the game again
    the event    -- a PC dies mid-race and an organiser has to decide, with
                    their own judgement, to give that participant a rerun

Because the second one is a judgement call, wiping a real attempt requires
`--yes` and prints exactly what it is about to destroy first.

    python manage.py reset_race PC-12 --yes      # clear that attempt
    python manage.py reset_race PC-TEST --new    # fresh test participant
"""

from django.core.management.base import BaseCommand, CommandError

from first.models import FinalSubmission, User

# Everything the race writes. Clearing an attempt means clearing all of it,
# or the participant restarts holding half of their last run.
RACE_FIELDS = {
    'race_started_at': None,
    'race_completed_at': None,
    'race_time_seconds': 0,
    'race_repairs': '',
    'race_distance': 0,
    'race_obstacles': 0,
    'race_collisions': 0,
    'best_score': 0,
    'completed_at': None,
    'game_start_time': None,
}

DEFAULT_TEST_PASSWORD = 'pw-123456'


class Command(BaseCommand):
    help = "Clear a participant's race attempt, or create a fresh test participant."

    def add_arguments(self, parser):
        parser.add_argument('pc_no', help='the participant to act on')
        parser.add_argument(
            '--new', action='store_true',
            help='create the participant if they do not exist yet')
        parser.add_argument(
            '--password', default=DEFAULT_TEST_PASSWORD,
            help=f'password for --new (default: {DEFAULT_TEST_PASSWORD})')
        parser.add_argument(
            '--yes', action='store_true',
            help='confirm clearing an attempt that has already been started')

    def handle(self, *args, **options):
        pc_no = options['pc_no'].strip()
        if not pc_no:
            raise CommandError('A PC number is required.')

        user = User.objects.filter(pc_no=pc_no).first()

        if user is None:
            if not options['new']:
                raise CommandError(
                    f'No participant {pc_no!r}. Pass --new to create one.')
            user = User.objects.create_user(
                username=pc_no, pc_no=pc_no, password=options['password'])
            self.stdout.write(self.style.SUCCESS(
                f'Created participant {pc_no} with a fresh, unstarted race.'))
            return

        if user.race_started_at is None and not user.race_repairs:
            self.stdout.write(
                f'{pc_no} has not started a race - nothing to clear.')
            return

        # Say out loud what is about to be destroyed, before destroying it.
        self.stdout.write(self.style.WARNING(
            f'{pc_no} has an official attempt on record:'))
        self.stdout.write(f'  status      : {user.race_status}')
        self.stdout.write(f'  started at  : {user.race_started_at}')
        self.stdout.write(f'  completed at: {user.race_completed_at}')
        self.stdout.write(f'  repairs     : {user.repairs_collected}'
                          f' ({user.race_repairs or "none"})')
        self.stdout.write(f'  distance    : {user.race_distance} m')
        self.stdout.write(f'  collisions  : {user.race_collisions}')
        self.stdout.write(f'  score       : {user.best_score}')

        if not options['yes']:
            raise CommandError(
                'Refusing to clear a real attempt without --yes. '
                'One participant gets one official attempt; re-running this '
                'with --yes is a deliberate decision to give them another.')

        submissions, _ = FinalSubmission.objects.filter(user=user).delete()
        for field, value in RACE_FIELDS.items():
            setattr(user, field, value)
        user.save(update_fields=list(RACE_FIELDS))

        self.stdout.write(self.style.SUCCESS(
            f'Cleared the race attempt for {pc_no}'
            f'{f" and removed {submissions} submission record(s)" if submissions else ""}. '
            f'They can start one official attempt again.'))

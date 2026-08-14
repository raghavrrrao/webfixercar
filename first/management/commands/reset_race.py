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

The argument is the *participant*, because that is what an attempt belongs to.
A PC number is shared by everyone who sits at that machine during the day, so
it cannot select one run: passing one that several people used lists them and
refuses rather than guessing. `--pc` narrows a name that is somehow still
ambiguous.

    python manage.py reset_race Rahul --yes            # clear that attempt
    python manage.py reset_race PC-12 --yes            # only if PC-12 is one person
    python manage.py reset_race Tester --new --pc PC-TEST
"""

from django.core.management.base import BaseCommand, CommandError

from first.models import RACE_NOT_STARTED, FinalSubmission, User

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
    help = "Clear one participant's race attempt, or create a fresh test participant."

    def add_arguments(self, parser):
        parser.add_argument(
            'participant',
            help='the participant name (a PC number is accepted only when '
                 'exactly one participant used it)')
        parser.add_argument(
            '--pc', default=None,
            help='narrow the match to this PC number')
        parser.add_argument(
            '--new', action='store_true',
            help='create the participant if they do not exist yet')
        parser.add_argument(
            '--password', default=DEFAULT_TEST_PASSWORD,
            help=f'password for --new (default: {DEFAULT_TEST_PASSWORD})')
        parser.add_argument(
            '--yes', action='store_true',
            help='confirm clearing an attempt that has already been started')

    # -- finding the one person this is about ------------------------------

    def _resolve(self, name, pc_no):
        """The single participant `name` means, or a CommandError explaining why not.

        Participant names are unique, so a name always selects at most one
        account. PC numbers are deliberately not unique, so one only selects an
        account when the day happens to have put a single person on it.
        """
        by_name = User.objects.filter(username__iexact=name)
        if pc_no:
            by_name = by_name.filter(pc_no=pc_no)
        match = by_name.first()
        if match:
            return match

        by_pc = User.objects.filter(pc_no=name)
        if pc_no:
            by_pc = by_pc.filter(pc_no=pc_no)
        found = list(by_pc.order_by('registered_at', 'pk'))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            # Never guess. Picking one here would wipe a stranger's live race.
            lines = '\n'.join(
                f'    {user.username}  ({user.race_status}, '
                f'registered {user.registered_at:%Y-%m-%d %H:%M})'
                for user in found
            )
            raise CommandError(
                f'{name!r} is a PC number used by {len(found)} participants, '
                f'and an attempt belongs to a person rather than a machine.\n'
                f'{lines}\n'
                f'Re-run with the participant name.')
        return None

    def handle(self, *args, **options):
        name = options['participant'].strip()
        pc_no = (options['pc'] or '').strip()
        if not name:
            raise CommandError('A participant is required.')

        user = self._resolve(name, pc_no)

        if user is None:
            if not options['new']:
                raise CommandError(
                    f'No participant {name!r}. Pass --new to create one.')
            user = User.objects.create_user(
                username=name, pc_no=pc_no or name,
                password=options['password'])
            self.stdout.write(self.style.SUCCESS(
                f'Created participant {user.username} on {user.pc_no} '
                f'with a fresh, unstarted race.'))
            return

        if options['new']:
            raise CommandError(
                f'{user.username} already exists on {user.pc_no}. '
                f'Drop --new to clear their attempt instead.')

        if user.race_status == RACE_NOT_STARTED and not user.race_repairs:
            self.stdout.write(
                f'{user.username} has not started a race - nothing to clear.')
            return

        # Say out loud who this is and what is about to be destroyed, before
        # destroying it. The name is first because that is what identifies them.
        self.stdout.write(self.style.WARNING(
            f'{user.username} (PC {user.pc_no}) has an official attempt on record:'))
        self.stdout.write(f'  status      : {user.race_status}')
        self.stdout.write(f'  started at  : {user._round_started_at}')
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
            f'Cleared the race attempt for {user.username} (PC {user.pc_no})'
            f'{f" and removed {submissions} submission record(s)" if submissions else ""}. '
            f'They can start one official attempt again.'))

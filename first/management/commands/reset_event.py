"""Clear the field between events — but only after the results are safe.

`reset_race` handles one participant. This is the whole-event version, and it
is the most destructive thing in the project, so it is built to be hard to
fire by accident:

  * it refuses to run without ``--yes``;
  * it refuses to run without ``--archive``, and writes that CSV *first*, so a
    reset can never be the thing that loses the results;
  * it refuses to overwrite an existing archive;
  * it prints exactly what it is about to clear before clearing it;
  * organiser accounts are never touched.

    python manage.py reset_event --archive results-2026-08-14.csv --yes
    python manage.py reset_event --archive out.csv --yes --delete-participants

Without ``--delete-participants`` the accounts survive with their races
cleared, which is what you want for a rehearsal. With it, the field is emptied
for a genuinely new event.
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from first.models import FinalSubmission, User
from first.scoreboard import write_export

from .reset_race import RACE_FIELDS


class Command(BaseCommand):
    help = 'Archive results to CSV, then clear every participant race.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--archive', required=True,
            help='CSV path to write the results to before anything is cleared')
        parser.add_argument(
            '--yes', action='store_true',
            help='confirm the reset (without this, nothing is written or cleared)')
        parser.add_argument(
            '--delete-participants', action='store_true',
            help='also remove the participant accounts, not just their races')

    def handle(self, *args, **options):
        archive = options['archive']
        participants = User.objects.filter(is_admin=False)
        started = participants.exclude(race_started_at=None).count()
        finished = participants.exclude(race_completed_at=None).count()

        self.stdout.write(self.style.WARNING('This will clear the whole event:'))
        self.stdout.write(f'  participants     : {participants.count()}')
        self.stdout.write(f'  races started    : {started}')
        self.stdout.write(f'  races completed  : {finished}')
        self.stdout.write(f'  submissions      : '
                          f'{FinalSubmission.objects.filter(user__is_admin=False).count()}')
        self.stdout.write(f'  accounts deleted : '
                          f'{"yes" if options["delete_participants"] else "no, races cleared only"}')
        self.stdout.write(f'  archive          : {archive}')

        if not options['yes']:
            raise CommandError(
                'Refusing to reset without --yes. Nothing has been written or '
                'cleared. Re-run with --yes once the archive path is right.')

        if os.path.exists(archive):
            raise CommandError(
                f'{archive} already exists. Pick a new filename rather than '
                f'overwriting an archive that may be the only copy of a result.')

        # Archive first. If this fails, the event is untouched.
        with open(archive, 'w', newline='', encoding='utf-8') as handle:
            rows = write_export(handle)
        self.stdout.write(self.style.SUCCESS(
            f'Archived {rows} participant run(s) to {archive}.'))
        if rows == 0:
            self.stdout.write('Nothing to clear.')
            return

        with transaction.atomic():
            submissions, _ = FinalSubmission.objects.filter(
                user__is_admin=False).delete()
            if options['delete_participants']:
                cleared, _ = participants.delete()
                self.stdout.write(self.style.SUCCESS(
                    f'Deleted {cleared} participant record(s) and {submissions} '
                    f'submission row(s). Organiser accounts were left alone.'))
            else:
                cleared = participants.update(**RACE_FIELDS)
                self.stdout.write(self.style.SUCCESS(
                    f'Cleared the race on {cleared} participant(s) and removed '
                    f'{submissions} submission row(s). The accounts remain.'))

        self.stdout.write(
            'Keep the archive somewhere other than this machine before the next event.')

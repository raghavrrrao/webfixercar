"""Write the event results to a CSV file, for judging and for the archive.

This is the step that happens *before* anybody resets anything. It reads and
writes nothing in the database, so it is safe to run at any point during the
event — during a race, between participants, or at the end.

    python manage.py export_results                       # to the terminal
    python manage.py export_results --out results.csv     # to a file

One row per run. Three participants who used PC-14 are three rows, and the
file names no winner: the organisers compare the completed runs themselves.
"""

from django.core.management.base import BaseCommand, CommandError

from first.export import result_rows, write_results_csv
from first.views import finalize_all_due


class Command(BaseCommand):
    help = 'Export every run to CSV for judging and archiving.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--out', default=None,
            help='file to write (default: standard output)')
        parser.add_argument(
            '--settle', action='store_true',
            help='first record an entry for anyone whose clock ran out while '
                 'nobody was watching')

    def handle(self, *args, **options):
        if options['settle']:
            settled = finalize_all_due()
            self.stderr.write(f'Settled {settled} unattended timeout(s).')

        rows = result_rows()
        target = options['out']

        if not target:
            # The command's own stream, not `sys.stdout`: it is what a caller
            # redirects, and writing past it makes the CSV uncapturable.
            write_results_csv(self.stdout, rows)
            return

        try:
            # newline='' is what keeps csv from doubling line endings on Windows.
            with open(target, 'w', newline='', encoding='utf-8') as handle:
                count = write_results_csv(handle, rows)
        except OSError as exc:
            raise CommandError(f'Could not write {target}: {exc}') from exc

        completed = sum(1 for row in rows if row['status'] == 'COMPLETED')
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {count} run(s) to {target} — {completed} completed. '
            f'Keep this file before resetting anything.'))

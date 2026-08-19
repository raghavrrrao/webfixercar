"""Write the judging sheet to a CSV file.

The same data the organiser's download button produces, available from the
server shell so results can be archived without a browser — and so the backup
step in the event runbook is one command.

    python manage.py export_results                     # to stdout
    python manage.py export_results --out results.csv   # to a file
"""

import sys

from django.core.management.base import BaseCommand

from first.scoreboard import write_export


class Command(BaseCommand):
    help = 'Export every participant run as CSV, for judging and archiving.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--out', help='file to write (default: stdout)')

    def handle(self, *args, **options):
        destination = options.get('out')
        if not destination:
            write_export(sys.stdout)
            return

        # newline='' is what csv wants on Windows, or every row gains a blank
        # line between it and the next.
        with open(destination, 'w', newline='', encoding='utf-8') as handle:
            count = write_export(handle)
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {count} participant run(s) to {destination}.'))

"""Create the organiser's admin account, non-interactively and idempotently.

A hosted deploy cannot answer `createsuperuser` prompts, so deployment runs
this instead. It is safe to run on every deploy: if the account already exists
the command leaves it alone and exits 0.

The login field is the participant name -- `USERNAME_FIELD = "username"` -- so
the value typed into Django's "Username" box is the username below. The PC
number is metadata and several accounts may share one. There is no email
anywhere in this model.

**There is no default password, and there never can be.** This account can
read every participant's run and delete the entire event from `/admin/`; a
password living in this repository would be a published credential on every
deployment that ever ran the command. So:

    password supplied      use it, and never print it
    none, development      invent a random one and print it once
    none, production       refuse, and create nothing

`--password` is for a developer at a terminal. `WF_ADMIN_PASSWORD` is for a
deploy, where Render's `generateValue: true` supplies a value nobody has seen.
"""

import os
import secrets

from django.core.management.base import BaseCommand, CommandError

from first.models import User

DEFAULT_PC_NO = 'admin123'
DEFAULT_USERNAME = 'admin123'


def _is_deployed():
    """Is this a production-shaped run?

    Reads the deployment's own `DJANGO_DEBUG` rather than `settings.DEBUG`,
    because the test runner forces the latter off and this question is about
    how the server was deployed, not how a test happens to execute.
    """
    return os.environ.get('DJANGO_DEBUG', 'true').strip().lower() in (
        '0', 'false', 'no', 'off')


class Command(BaseCommand):
    help = 'Create the organiser admin account if it does not already exist.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--password', default=None,
            help='the password to set (otherwise WF_ADMIN_PASSWORD, otherwise '
                 'a random one in development)')
        parser.add_argument(
            '--username', default=None,
            help=f'login name (default: {DEFAULT_USERNAME}, or WF_ADMIN_USERNAME)')

    def handle(self, *args, **options):
        pc_no = os.environ.get('WF_ADMIN_PC_NO', DEFAULT_PC_NO).strip()
        username = (options['username']
                    or os.environ.get('WF_ADMIN_USERNAME', '').strip()
                    or DEFAULT_USERNAME)

        # Looked up by the login field. PC numbers repeat across participants,
        # so they cannot select a single account.
        existing = User.objects.filter(username=username).first()
        if existing:
            # Never overwrite a password that may have been changed on purpose.
            if not existing.is_admin:
                existing.is_admin = True
                existing.save(update_fields=['is_admin'])
                self.stdout.write(f'Granted admin rights to existing {username!r}.')
            else:
                self.stdout.write(f'Admin {username!r} already exists; nothing to do.')
            return

        supplied = options['password'] or os.environ.get('WF_ADMIN_PASSWORD', '')
        supplied = supplied.strip() if supplied else ''

        if supplied:
            password, invented = supplied, False
        elif _is_deployed():
            # Creating a known account here is the one outcome that is worse
            # than failing the deploy.
            raise CommandError(
                'WF_ADMIN_PASSWORD is not set. Refusing to create the '
                'organiser account without one: this account can read every '
                "participant's run and delete the event from /admin/, so it "
                'must never be created with a password that is guessable or '
                'published.\n'
                'On Render, render.yaml declares WF_ADMIN_PASSWORD with '
                'generateValue: true — check it is present on the service, '
                'then redeploy.'
            )
        else:
            # Development convenience, without ever shipping a known secret.
            password, invented = secrets.token_urlsafe(18), True

        User.objects.create_superuser(
            username=username, pc_no=pc_no, password=password,
        )

        self.stdout.write(self.style.SUCCESS(
            f'Created admin {username!r}. Log in at /admin/ with that as the '
            f'username.'
        ))
        if invented:
            # Only ever a password this command just invented for a developer.
            # A supplied one is never echoed: on a deploy this output is the
            # build log.
            self.stdout.write(self.style.WARNING(
                f'Generated development password: {password}\n'
                f'This is shown once. Set WF_ADMIN_PASSWORD to choose your own.'
            ))

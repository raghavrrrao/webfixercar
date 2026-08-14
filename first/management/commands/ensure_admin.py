"""Create the organiser's admin account, non-interactively and idempotently.

Render cannot answer `createsuperuser` prompts, so deployment runs this
instead. It is safe to run on every deploy: if the account already exists the
command leaves it alone and exits 0.

The login field is the participant name -- `USERNAME_FIELD = "username"` --
so the value typed into Django's "Username" box is `admin123`. The PC number
is metadata and several accounts may share one. There is no email anywhere in
this model.
"""

import os

from django.core.management.base import BaseCommand

from first.models import User

DEFAULT_PC_NO = 'admin123'
DEFAULT_USERNAME = 'admin123'

# The development password. It is in this repository, so for a real event it
# is a published credential and `WF_ADMIN_PASSWORD` must replace it -- the
# organiser account is the account that can read every participant's run and
# delete the whole event from /admin/.
DEFAULT_PASSWORD = 'piyush123456@'


class Command(BaseCommand):
    help = 'Create the admin account if it does not already exist.'

    def handle(self, *args, **options):
        # Env vars let the event change the password on Render without a code
        # change; the defaults are the credentials the organisers were given.
        pc_no = os.environ.get('WF_ADMIN_PC_NO', DEFAULT_PC_NO).strip()
        username = os.environ.get('WF_ADMIN_USERNAME', DEFAULT_USERNAME).strip()
        chosen = os.environ.get('WF_ADMIN_PASSWORD', '').strip()
        password = chosen or DEFAULT_PASSWORD

        # Never fail the deploy over it -- an event that cannot boot is worse
        # than one with a weak password -- but never let it pass quietly on a
        # production-shaped run either.
        #
        # The signal is the deployment's own DJANGO_DEBUG, not `settings.DEBUG`:
        # the test runner forces the latter off, and this warning is about how
        # the server was deployed rather than how a test happens to run.
        deployed = os.environ.get('DJANGO_DEBUG', 'true').strip().lower() in (
            '0', 'false', 'no', 'off')
        if deployed and not chosen:
            self.stderr.write(self.style.ERROR(
                'WF_ADMIN_PASSWORD is not set, so the organiser account is '
                'using the password published in this repository. Anybody who '
                'has read the source can sign in at /admin/ and delete the '
                'event. Set WF_ADMIN_PASSWORD and re-run this command.'))

        # Looked up by the login field. PC numbers repeat across participants,
        # so they cannot select a single account any more.
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

        User.objects.create_superuser(
            username=username, pc_no=pc_no, password=password,
        )
        self.stdout.write(self.style.SUCCESS(
            f'Created admin {username!r}. Log in at /admin/ with that as the username.'
        ))

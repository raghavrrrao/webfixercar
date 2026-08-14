"""Authentication for the event.

A participant signs in as *themselves*. The PC number is which machine they
happen to be sitting at, and a machine is used by one participant after
another all day, so it identifies a desk and never a person.
"""

from django.contrib.auth.backends import ModelBackend

from .models import User


class ParticipantBackend(ModelBackend):
    """Sign in by participant name.

    `username` is the model's USERNAME_FIELD, so Django's own admin login and
    `client.login()` both arrive here with `username=...`. The `pc_no` keyword
    is deliberately not accepted: several participants share a PC number, so
    it cannot select a single account.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if not username or password is None:
            return None
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None

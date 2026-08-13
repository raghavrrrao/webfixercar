from django.contrib.auth.backends import ModelBackend
from .models import User

class PCNoBackend(ModelBackend):
    def authenticate(self, request, pc_no=None, password=None, **kwargs):
        try:
            user = User.objects.get(pc_no=pc_no)
            if user.check_password(password):
                return user
        except User.DoesNotExist:
            return None
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None

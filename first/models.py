from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.utils import timezone

from .game_config import GAME_DURATION_SECONDS

# The race state machine, in one place.
#
#   NOT_STARTED --START RACE--> ACTIVE --finish line--> COMPLETED
#                                     \--deadline----> EXPIRED
#
# COMPLETED and EXPIRED are terminal: a completed race never becomes active or
# expired again, and an expired race can never be completed.
RACE_NOT_STARTED = 'not_started'
RACE_ACTIVE = 'active'
RACE_COMPLETED = 'completed'
RACE_EXPIRED = 'expired'


# Custom User Manager
class UserManager(BaseUserManager):
    def create_user(self, username, pc_no, password=None):
        if not username or not pc_no:
            raise ValueError("Users must have a username and PC number")
        user = self.model(username=username, pc_no=pc_no)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, pc_no, password=None):
        user = self.create_user(username=username, pc_no=pc_no, password=password)
        user.is_admin = True
        user.save(using=self._db)
        return user


# Custom User Model
class User(AbstractBaseUser):
    username = models.CharField(max_length=100)
    pc_no = models.CharField(max_length=50, unique=True)
    password = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    is_admin = models.BooleanField(default=False)

    registered_at = models.DateTimeField(auto_now_add=True, null=True)

    # The challenge clock. Set when the player first opens the arena and
    # never reset by a refresh -- this is the authoritative timer source.
    game_start_time = models.DateTimeField(null=True, blank=True)

    # When the player first cleared all 14 objectives. This unlocks Design
    # Mode and is what makes their entry eligible for judging; it does NOT
    # end the round, which runs until the deadline either way.
    completed_at = models.DateTimeField(null=True, blank=True)
    best_score = models.PositiveSmallIntegerField(default=0)

    # New racing round data. The old CSS-editor fields are kept for backwards
    # compatibility with existing databases/admin records.
    race_started_at = models.DateTimeField(null=True, blank=True)
    race_completed_at = models.DateTimeField(null=True, blank=True)
    race_time_seconds = models.PositiveSmallIntegerField(default=0)
    race_collisions = models.PositiveSmallIntegerField(default=0)

    # The CSS repairs collected, in the order they were collected, as a
    # comma-separated list of repair ids. This is the authoritative record of
    # how much of NovaCloud the participant has put back together: the live
    # preview is composed from it, and so is the score.
    race_repairs = models.TextField(blank=True, default="")

    # Furthest point on the course the server has accepted, in metres. Every
    # repair and the finish line are checked against it.
    race_distance = models.PositiveIntegerField(default=0)

    # Kept in step with `race_repairs` by `record_repair()` so the admin can
    # sort and filter on it in the database. `race_repairs` is the source of
    # truth; this is a count derived from it, never written anywhere else.
    race_obstacles = models.PositiveSmallIntegerField(
        default=0, verbose_name="repairs collected")

    objects = UserManager()

    USERNAME_FIELD = "pc_no"
    REQUIRED_FIELDS = ["username"]

    def __str__(self):
        return f"{self.pc_no} ({self.username})"

    # -- django admin ------------------------------------------------------

    # This model is an AbstractBaseUser without PermissionsMixin, so there is
    # no `is_staff`/`is_superuser` column: `is_admin` is the single stored
    # flag and these read from it.
    @property
    def is_staff(self):
        return self.is_admin

    @property
    def is_superuser(self):
        return self.is_admin

    def has_perm(self, perm, obj=None):
        return self.is_admin

    def has_module_perms(self, app_label):
        return self.is_admin

    # -- challenge clock ---------------------------------------------------

    def start_challenge(self):
        """Start the one official race attempt.

        Called only from the START RACE endpoint. Refreshing, reopening the
        page or replaying the request never restarts the clock: the timestamp
        is written once, by the server, and never again.

        Returns True if this call actually started the race.
        """
        if self.race_started_at or self.race_completed_at:
            return False
        now = timezone.now()
        self.race_started_at = now
        # Keep the legacy field populated so old admin/reporting code and
        # existing migrations remain compatible.
        self.game_start_time = now
        self.save(update_fields=["race_started_at", "game_start_time"])
        return True

    @property
    def _round_started_at(self):
        # Fall back to the legacy field so old records/tests remain readable.
        return self.race_started_at or self.game_start_time

    @property
    def deadline(self):
        started = self._round_started_at
        if not started:
            return None
        return started + timezone.timedelta(seconds=GAME_DURATION_SECONDS)

    @property
    def elapsed_seconds(self):
        """Whole seconds since the server started the round. 0 if unstarted."""
        started = self._round_started_at
        if not started:
            return 0
        return max(0, int((timezone.now() - started).total_seconds()))

    @property
    def remaining_seconds(self):
        started = self._round_started_at
        if not started:
            return GAME_DURATION_SECONDS
        return max(0, GAME_DURATION_SECONDS - self.elapsed_seconds)

    @property
    def is_expired(self):
        """True once the deadline has passed on a round that never finished.

        A finished race is *completed*, not expired — the two are different
        terminal states and a finished attempt must never fall into the second
        one just because twelve minutes have since gone by.
        """
        if self.race_completed_at:
            return False
        return self._round_started_at is not None and self.remaining_seconds <= 0

    # -- the repairs -------------------------------------------------------

    @property
    def repair_ids(self):
        """The repairs collected so far, in course order."""
        from .repairs import clean_repair_ids
        return clean_repair_ids(self.race_repairs)

    @property
    def repairs_collected(self):
        return len(self.repair_ids)

    def record_repair(self, repair_id):
        """Append one repair to the participant's record.

        The only writer of `race_repairs`, so the stored list is always in
        course order and never holds a duplicate. Returns False if this repair
        was already collected, leaving the record untouched.
        """
        collected = self.repair_ids
        if repair_id in collected:
            return False
        collected.append(repair_id)
        self.race_repairs = ','.join(collected)
        self.race_obstacles = len(collected)
        self.save(update_fields=['race_repairs', 'race_obstacles'])
        return True

    @property
    def race_status(self):
        if self.race_completed_at:
            return RACE_COMPLETED
        if not self.race_started_at:
            return RACE_NOT_STARTED
        if self.remaining_seconds <= 0:
            return RACE_EXPIRED
        return RACE_ACTIVE

    @property
    def design_mode(self):
        # Kept for compatibility with the original competition model. In the
        # new game the race completion also sets completed_at.
        return self.completed_at is not None

    is_completed = design_mode

    @property
    def is_eligible(self):
        return self.completed_at is not None

    @property
    def is_locked(self):
        return self.is_expired or self.race_completed_at is not None

    # -- hints -------------------------------------------------------------

    @property
    def hints_used(self):
        return self.hint_reveals.count()

    @property
    def objectives_hinted(self):
        return self.hint_reveals.values('objective').distinct().count()


# The player's live, editable stylesheet (one row per player).
class CssRule(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="css_rules")
    html = models.TextField(blank=True, default="")
    css = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Working stylesheet for {self.user.pc_no}"


class HintReveal(models.Model):
    """One row the first time a player reveals a given hint level.

    The unique constraint is what makes the count honest: clicking the same
    hint again is a no-op, so a total cannot be inflated by re-reading.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hint_reveals")
    objective = models.CharField(max_length=64)
    level = models.PositiveSmallIntegerField()
    revealed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "objective", "level")
        ordering = ("revealed_at", "id")

    def __str__(self):
        return f"{self.user.pc_no} · {self.objective} · hint {self.level}"


class FinalSubmission(models.Model):
    """The immutable snapshot taken when a player's deadline passes.

    Written once, by the server, from whatever the player's stylesheet held at
    the deadline. `final_css` is the competition entry and is never rewritten;
    `save()` refuses to change it once the row exists.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="final_submission")

    pc_no = models.CharField(max_length=50)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField()

    final_css = models.TextField()

    score = models.PositiveSmallIntegerField(default=0)
    total = models.PositiveSmallIntegerField(default=0)
    reached_all = models.BooleanField(default=False)
    design_mode = models.BooleanField(default=False)
    eligible = models.BooleanField(default=False)

    hints_used = models.PositiveSmallIntegerField(default=0)
    objectives_hinted = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("-submitted_at",)
        verbose_name = "final submission"
        verbose_name_plural = "final submissions"

    def __str__(self):
        return f"{self.pc_no} — {self.score}/{self.total}"

    @property
    def status(self):
        return "Submitted" if self.eligible else "Expired"

    def save(self, *args, **kwargs):
        """Allow the first write; refuse any later change to the entry itself."""
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values(
                'final_css', 'score', 'eligible', 'submitted_at',
            ).first()
            if stored:
                frozen = {
                    'final_css': self.final_css,
                    'score': self.score,
                    'eligible': self.eligible,
                    'submitted_at': self.submitted_at,
                }
                changed = [k for k, v in frozen.items() if stored[k] != v]
                if changed:
                    raise ValueError(
                        'FinalSubmission is immutable; refusing to change '
                        + ', '.join(sorted(changed))
                    )
        return super().save(*args, **kwargs)

import re
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from .checks import TOTAL_CHECKS, run_checks
from .game_config import (
    CHALLENGE_HTML,
    DIFFICULTY,
    GAME_DURATION_SECONDS,
    MODE,
    CHALLENGE_LABEL,
    RACE_ACCELERATION,
    RACE_BASE_POINTS,
    RACE_BRAKING,
    RACE_CLEAN_RUN_BONUS,
    RACE_COLLISION_PENALTY,
    RACE_COURSE_METRES,
    RACE_COURSE_SEED,
    RACE_CRASH_GRACE_SECONDS,
    RACE_CRASH_SPEED_KEPT,
    RACE_DISTANCE_GRACE_METRES,
    RACE_DRAG,
    RACE_MAX_COLLISIONS,
    RACE_MAX_COLLISIONS_PER_REPORT,
    RACE_MAX_SCORE,
    RACE_MIN_SECONDS,
    RACE_PROGRESS_INTERVAL_SECONDS,
    RACE_REPAIR_METRES,
    RACE_REPAIR_POINTS,
    RACE_REVERSE_SPEED,
    RACE_SECTION_COUNT,
    RACE_SECTION_METRES,
    RACE_SPEED_TOLERANCE,
    RACE_STATE_SYNC_SECONDS,
    RACE_STEER_RATE,
    RACE_TIME_POINTS,
    RACE_TOP_SPEED,
    SITE_NAME,
    SITE_TAGLINE,
    SOLUTION_CSS,
    STARTER_CSS,
    TIMER_DANGER_SECONDS,
    TIMER_WARNING_SECONDS,
)
from .models import (
    RACE_ACTIVE,
    RACE_COMPLETED,
    RACE_EXPIRED,
    RACE_NOT_STARTED,
    CssRule,
    FinalSubmission,
    User,
)
from .repairs import REPAIR_CARDS, REPAIR_COUNT, repair_css, repair_index
from .scoreboard import (
    SCOREBOARD_HEARTBEAT_SECONDS,
    player_payload,
    schedule_scoreboard_event,
    scoreboard_snapshot,
)

# Everything the templates need to name the current round in one place.
CHALLENGE = {
    'label': CHALLENGE_LABEL,
    'site': SITE_NAME,
    'tagline': SITE_TAGLINE,
    'difficulty': DIFFICULTY,
    'mode': MODE,
    'minutes': GAME_DURATION_SECONDS // 60,
    'objectives': TOTAL_CHECKS,
    'repairs': REPAIR_COUNT,
    'kilometres': round(RACE_COURSE_METRES / 1000, 1),
}

# The course the browser draws, and the physics it drives with. Sent to the
# page rather than hardcoded in the script so the two can never disagree about
# where a repair sits or how fast the car can possibly be going — the server
# validates against these same numbers.
RACE_CONFIG = {
    'duration': GAME_DURATION_SECONDS,
    'course': RACE_COURSE_METRES,
    'sectionMetres': RACE_SECTION_METRES,
    'sectionCount': RACE_SECTION_COUNT,
    'repairMetres': list(RACE_REPAIR_METRES),
    'repairs': list(REPAIR_CARDS),
    'seed': RACE_COURSE_SEED,
    'car': {
        'topSpeed': RACE_TOP_SPEED,
        'acceleration': RACE_ACCELERATION,
        'braking': RACE_BRAKING,
        'drag': RACE_DRAG,
        'reverseSpeed': RACE_REVERSE_SPEED,
        'steerRate': RACE_STEER_RATE,
        'crashSpeedKept': RACE_CRASH_SPEED_KEPT,
        'crashGrace': RACE_CRASH_GRACE_SECONDS,
    },
    'warnSeconds': TIMER_WARNING_SECONDS,
    'dangerSeconds': TIMER_DANGER_SECONDS,
    'syncSeconds': RACE_STATE_SYNC_SECONDS,
    'progressSeconds': RACE_PROGRESS_INTERVAL_SECONDS,
}


# Matches the challenge page's own <link rel="stylesheet" href="style.css">.
_STYLESHEET_LINK = re.compile(
    r"""<link\s[^>]*href\s*=\s*['"][^'"]*style[.]css['"][^>]*>""", re.I,
)


def _ensure_session(request):
    """Presence dedupes by session key, so make sure the visitor has one."""
    if not request.session.session_key:
        request.session.create()


def _get_submission(user):
    """The player's stylesheet row.

    The race does not write CSS any more, but rounds recorded by the previous
    CSS-editor version still have one and `finalize_if_due` still reads it.
    """
    submission, _ = CssRule.objects.get_or_create(
        user=user,
        defaults={'html': '', 'css': STARTER_CSS},
    )
    return submission


def finalize_if_due(user):
    """Freeze an unfinished round at the authoritative server deadline.

    A race that ran out of time is recorded as an *ineligible* entry: the
    performance is kept for the organisers, but nothing about it counts as a
    completion, and it never unlocks the fixed website.

    Legacy records created by the previous CSS-editor version still finalize
    using their old submission data, so an existing database stays readable.
    """
    if not user.is_expired:
        return None
    existing = FinalSubmission.objects.filter(user=user).first()
    if existing:
        return existing

    if user.race_started_at:
        kwargs = {
            'started_at': user.race_started_at,
            'submitted_at': user.deadline or timezone.now(),
            'final_css': STARTER_CSS,
            'score': 0,
            'total': RACE_MAX_SCORE,
            'reached_all': False,
            'design_mode': False,
            'eligible': False,
            'hints_used': 0,
            'objectives_hinted': 0,
        }
    else:
        submission = _get_submission(user)
        checks = run_checks(CHALLENGE_HTML, submission.css)
        score = sum(1 for check in checks if check['passed'])
        if score > user.best_score:
            user.best_score = score
            user.save(update_fields=['best_score'])
        kwargs = {
            'started_at': user.game_start_time,
            'submitted_at': user.deadline or timezone.now(),
            'final_css': submission.css,
            'score': user.best_score,
            'total': TOTAL_CHECKS,
            'reached_all': user.design_mode,
            'design_mode': user.design_mode,
            'eligible': user.is_eligible,
            'hints_used': user.hints_used,
            'objectives_hinted': user.objectives_hinted,
        }

    try:
        with transaction.atomic():
            return FinalSubmission.objects.create(
                user=user, pc_no=user.pc_no, **kwargs,
            )
    except IntegrityError:
        return FinalSubmission.objects.get(user=user)


def finalize_all_due():
    """Settle every round whose deadline has passed but was never revisited.

    A player who closes the laptop and walks away generates no further
    requests, so `finalize_if_due` never fires for them. The admin calls this
    when an organiser opens a participant list, which is the point at which
    somebody actually needs the entry to exist.

    Returns the number of submissions created.
    """
    due = User.objects.filter(
        game_start_time__isnull=False,
        race_completed_at__isnull=True,
        final_submission__isnull=True,
        game_start_time__lte=timezone.now() - timezone.timedelta(
            seconds=GAME_DURATION_SECONDS),
    )
    return sum(1 for user in due if finalize_if_due(user) is not None)


# ------------------------------------------------------------ race state ----

def race_section(distance):
    """Which of the seven CSS sections `distance` metres falls in."""
    if distance <= 0:
        return 0
    return max(0, min(RACE_SECTION_COUNT - 1, int(distance // RACE_SECTION_METRES)))


def _race_state(user):
    """The one shape every race endpoint answers with.

    Every number in it is read from the database or the server clock, and it
    is the only race state there is: the browser renders this and reports
    against it, but never contributes to it. It is also deliberately the
    shape a live scoreboard would want to broadcast.
    """
    status = user.race_status
    started = status != RACE_NOT_STARTED
    elapsed = user.elapsed_seconds if started else 0
    repairs = user.repair_ids
    completed = bool(user.race_completed_at)
    return {
        'status': status,
        'started': started,
        'remaining': user.remaining_seconds,
        'elapsed': elapsed,
        'duration': GAME_DURATION_SECONDS,
        'expired': user.is_expired,
        'completed': completed,
        'repairs': repairs,
        'repairCount': REPAIR_COUNT,
        'repairsCollected': len(repairs),
        'distance': user.race_distance,
        'course': RACE_COURSE_METRES,
        'section': race_section(user.race_distance),
        'collisions': user.race_collisions,
        # While the race runs this is the score as it stands *right now*, so
        # the HUD never has to do arithmetic of its own. A race that has not
        # started has not scored anything.
        'score': (user.best_score if completed
                  else (race_score(elapsed, len(repairs), user.race_collisions)
                        if started else 0)),
    }


def _settle_expired(user):
    """Record the timeout entry the moment the server notices the deadline.

    Broadcast exactly once, on the transition. This runs on every state read
    of an expired race, so announcing unconditionally would republish the same
    timeout for as long as the tab stayed open; the entry existing already is
    what tells us the scoreboard has been told.
    """
    if not user.is_expired:
        return
    already_settled = FinalSubmission.objects.filter(user=user).exists()
    finalize_if_due(user)
    if not already_settled:
        schedule_scoreboard_event(user, 'race_expired')


def race_score(elapsed, repairs, collisions):
    """The official score. Server-side, deterministic, and the only one.

    Driving the course quickly scores most, every CSS repair scores, every
    collision costs, and a crash-free run with the whole website rebuilt earns
    the last hundred points. The theoretical perfect run — every repair, no
    collisions, the fastest the car can physically go — is exactly
    RACE_MAX_SCORE.
    """
    span = max(1, GAME_DURATION_SECONDS - RACE_MIN_SECONDS)
    time_points = int(RACE_TIME_POINTS * (GAME_DURATION_SECONDS - elapsed) / span)
    time_points = max(0, min(RACE_TIME_POINTS, time_points))

    score = (
        RACE_BASE_POINTS
        + time_points
        + min(repairs, REPAIR_COUNT) * RACE_REPAIR_POINTS
        - collisions * RACE_COLLISION_PENALTY
    )
    if collisions == 0 and repairs >= REPAIR_COUNT:
        score += RACE_CLEAN_RUN_BONUS
    return max(0, min(RACE_MAX_SCORE, score))


def _reachable_metres(elapsed):
    """The furthest the car could honestly have got in `elapsed` seconds."""
    return elapsed * RACE_TOP_SPEED * RACE_SPEED_TOLERANCE + RACE_DISTANCE_GRACE_METRES


# ---------------------------------------------------------------- pages ----

def intro(request):
    """Game home page."""
    _ensure_session(request)
    return render(request, 'intro.html', {'challenge': CHALLENGE})


def start(request):
    """Short 'booting the arena' transition between auth and the challenge."""
    if not request.user.is_authenticated:
        return redirect('login')
    return render(request, 'start.html', {'challenge': CHALLENGE})


def home(request):
    """Broken-site briefing.

    Rendering this page reads state and never writes any: opening it, or
    reopening it, does not start the race. Only START RACE does.
    """
    if not request.user.is_authenticated:
        return redirect('login')

    _ensure_session(request)
    user = request.user
    _settle_expired(user)

    if user.race_status == RACE_COMPLETED:
        return redirect('race_result')

    return render(request, 'entry.html', {
        'challenge': CHALLENGE,
        'state': _race_state(user),
        'race_config': RACE_CONFIG,
        'race_over': user.race_status == RACE_EXPIRED,
        'race_active': user.race_status == RACE_ACTIVE,
        'broken_url': reverse('api_broken_preview'),
        'repairs': REPAIR_CARDS,
        'race_urls': {
            'start': reverse('api_race_start'),
            'state': reverse('api_race_state'),
            'progress': reverse('api_race_progress'),
            'complete': reverse('api_race_complete'),
            'preview': reverse('api_race_preview'),
            'result': reverse('race_result'),
            'home': reverse('home'),
            'exit': reverse('logout'),
        },
    })


def race_result(request):
    """The performance record. It never declares anybody the winner."""
    if not request.user.is_authenticated:
        return redirect('login')
    user = request.user
    if not user.race_completed_at:
        return redirect('home')
    collected = user.repair_ids
    return render(request, 'race_result.html', {
        'challenge': CHALLENGE,
        'user': user,
        'score': user.best_score,
        'max_score': RACE_MAX_SCORE,
        'time_seconds': user.race_time_seconds,
        'repairs': [
            {**card, 'done': card['id'] in collected} for card in REPAIR_CARDS
        ],
        'repairs_collected': len(collected),
        'repair_count': REPAIR_COUNT,
        'collisions': user.race_collisions,
        'kilometres': round(user.race_distance / 1000, 1),
        'fixed_url': reverse('api_final_preview'),
    })


def _render_novacloud(css):
    """The challenge markup rendered with `css`, for a sandboxed iframe.

    Used by every preview endpoint — the broken page, the official solution
    and a player's own recorded entry — so they all render through exactly the
    same path and differ only in which stylesheet goes in.

    The site sends X-Frame-Options: DENY everywhere else; these views are
    relaxed to SAMEORIGIN because the game pages and the admin frame them.
    They stay un-embeddable by any other origin, and the frames carry an empty
    `sandbox` so the CSS inside cannot reach the page around it.
    """
    document = _STYLESHEET_LINK.sub(
        lambda _: '<style>' + css + '</style>', CHALLENGE_HTML, count=1,
    )
    response = HttpResponse(document, content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@xframe_options_sameorigin
def broken_preview(request):
    """Render the intentionally broken NovaCloud page before the race.

    Reading it is free and changes nothing — in particular it does not start
    the clock, which is why the briefing can show it before START RACE.
    """
    if not request.user.is_authenticated:
        return redirect('login')
    return _render_novacloud(STARTER_CSS)


# ------------------------------------------------------------- race api ----

@require_POST
def api_race_start(request):
    """Begin the one official attempt, and stamp the start time server-side.

    This is the *only* place a race clock ever begins. Replaying the request —
    a refresh, a second tab, a retried fetch — returns the running race
    unchanged rather than granting a fresh twelve minutes.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user = request.user
    status = user.race_status

    if status == RACE_COMPLETED:
        return JsonResponse(
            {**_race_state(user), 'error': 'This attempt is already finished.',
             'redirect': reverse('race_result')},
            status=409,
        )
    if status == RACE_EXPIRED:
        _settle_expired(user)
        return JsonResponse(
            {**_race_state(user), 'error': "Time is up. Your race attempt has ended."},
            status=409,
        )
    if status == RACE_ACTIVE:
        # A refresh mid-race: hand back the running attempt, untouched.
        return JsonResponse({**_race_state(user), 'resumed': True})

    user.start_challenge()
    schedule_scoreboard_event(user, 'race_started')
    return JsonResponse({**_race_state(user), 'resumed': False})


def api_race_state(request):
    """Authoritative race state. The browser only renders what this says."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)
    _settle_expired(request.user)
    return JsonResponse(_race_state(request.user))


def _live_race(request):
    """Return (user, None) for a running race, or (user, response) to refuse.

    Every write endpoint starts here, so "is this attempt allowed to change?"
    is answered in exactly one place.
    """
    user = request.user
    status = user.race_status
    if status == RACE_NOT_STARTED:
        return user, JsonResponse(
            {**_race_state(user), 'error': 'Race has not started.'}, status=400)
    if status == RACE_COMPLETED:
        return user, JsonResponse(
            {**_race_state(user), 'error': 'This attempt is already finished.',
             'redirect': reverse('race_result')}, status=409)
    if status == RACE_EXPIRED:
        _settle_expired(user)
        return user, JsonResponse(
            {**_race_state(user),
             'error': "Time is up. Your race attempt has ended."}, status=409)
    return user, None


@require_POST
def api_race_progress(request):
    """Report where the car has got to, what it hit, and what it picked up.

    This is what keeps the finish honest. Distance is the spine of it: the
    server accepts a new distance only if the car could physically have
    covered it in the time the server has been counting, and it never goes
    backwards. A repair is then accepted only when

      * the race is genuinely active,
      * it is the next repair in course order, and
      * the car has actually reached the point where that repair sits.

    So the seven repairs the completion endpoint insists on cannot be
    conjured up: collecting them takes as long as driving to them does.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user, refusal = _live_race(request)
    if refusal is not None:
        return refusal

    elapsed = user.elapsed_seconds
    reachable = _reachable_metres(elapsed)
    fields = []

    raw_distance = (request.POST.get('distance') or '').strip()
    if raw_distance:
        try:
            # float() first so "1200.5" is read, int() so infinities are not.
            distance = int(float(raw_distance))
        except (TypeError, ValueError, OverflowError):
            return JsonResponse(
                {**_race_state(user), 'error': 'Invalid distance.'}, status=400)
        if distance < 0:
            return JsonResponse(
                {**_race_state(user), 'error': 'Invalid distance.'}, status=400)
        # Clamp rather than refuse: a client that overstates its position gets
        # held at what was actually possible, and simply makes no progress.
        distance = int(min(distance, reachable))
        if distance > user.race_distance:
            user.race_distance = distance
            fields.append('race_distance')

    raw_collisions = (request.POST.get('collisions') or '').strip()
    if raw_collisions:
        try:
            hits = int(raw_collisions)
        except (TypeError, ValueError):
            return JsonResponse(
                {**_race_state(user), 'error': 'Invalid collisions.'}, status=400)
        hits = max(0, min(RACE_MAX_COLLISIONS_PER_REPORT, hits))
        if hits:
            user.race_collisions = min(RACE_MAX_COLLISIONS, user.race_collisions + hits)
            fields.append('race_collisions')

    if fields:
        user.save(update_fields=fields)
        # A collision is the more interesting half of the same report, so it
        # is what the feed hears about when both arrive together.
        schedule_scoreboard_event(
            user, 'collision' if 'race_collisions' in fields else 'race_progress')

    repair_id = (request.POST.get('repair') or '').strip()
    if repair_id:
        return _record_repair(user, repair_id)

    return JsonResponse({**_race_state(user), 'counted': bool(fields)})


def _record_repair(user, repair_id):
    """Accept one CSS repair, if the car has genuinely earned it."""
    index = repair_index(repair_id)
    if index is None:
        return JsonResponse(
            {**_race_state(user), 'error': 'No such repair.'}, status=400)

    collected = user.repair_ids
    if repair_id in collected:
        # A replayed report. Already recorded; say so without recording twice.
        return JsonResponse({**_race_state(user), 'counted': False})
    if index != len(collected):
        return JsonResponse(
            {**_race_state(user),
             'error': 'Repairs must be collected in course order.'}, status=400)
    if user.race_distance < RACE_REPAIR_METRES[index]:
        return JsonResponse(
            {**_race_state(user),
             'error': 'That repair is still further up the course.'}, status=400)

    user.record_repair(repair_id)
    schedule_scoreboard_event(user, 'repair_collected')
    return JsonResponse({
        **_race_state(user),
        'counted': True,
        'repair': REPAIR_CARDS[index],
    })


@require_POST
def api_race_complete(request):
    """Cross the finish line. The server decides whether that really happened.

    Nothing the browser sends is scored. The completion is accepted only when
    the server's own record says the attempt is active, the whole course has
    been driven, and NovaCloud is fully repaired — and it can only ever be
    accepted once.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user, refusal = _live_race(request)
    if refusal is not None:
        return refusal

    repairs = user.repair_ids
    if len(repairs) < REPAIR_COUNT:
        missing = REPAIR_COUNT - len(repairs)
        return JsonResponse(
            {**_race_state(user),
             'error': f'NovaCloud still needs {missing} more CSS '
                      f'repair{"s" if missing > 1 else ""}.'}, status=400)
    if user.race_distance < RACE_COURSE_METRES:
        return JsonResponse(
            {**_race_state(user),
             'error': 'You have not reached the finish line yet.'}, status=400)

    elapsed = max(1, min(GAME_DURATION_SECONDS, user.elapsed_seconds))
    collisions = min(RACE_MAX_COLLISIONS, user.race_collisions)
    score = race_score(elapsed, len(repairs), collisions)

    now = timezone.now()
    updated = User.objects.filter(
        pk=user.pk, race_completed_at__isnull=True,
    ).update(
        race_completed_at=now,
        completed_at=now,
        race_time_seconds=elapsed,
        best_score=score,
    )
    user.refresh_from_db()
    if not updated:
        # Two requests raced each other; the first one is the record.
        return JsonResponse(
            {**_race_state(user), 'redirect': reverse('race_result')}, status=409)

    # The immutable competition record. A completed race is eligible and is
    # rewarded with the fixed stylesheet; timeout entries never are.
    FinalSubmission.objects.get_or_create(
        user=user,
        defaults={
            'pc_no': user.pc_no,
            'started_at': user.race_started_at,
            'submitted_at': now,
            'final_css': SOLUTION_CSS,
            'score': score,
            'total': RACE_MAX_SCORE,
            'reached_all': True,
            'design_mode': True,
            'eligible': True,
            'hints_used': 0,
            'objectives_hinted': 0,
        },
    )
    schedule_scoreboard_event(user, 'race_completed')
    return JsonResponse({**_race_state(user), 'redirect': reverse('race_result')})


@xframe_options_sameorigin
def race_preview(request):
    """NovaCloud as it stands right now, for the panel beside the track.

    The stylesheet is composed here, from the repairs the *server* has
    recorded — so the preview cannot show a repair that was not earned, and a
    participant whose time ran out is looking at the broken site again.
    """
    if not request.user.is_authenticated:
        return redirect('login')

    user = request.user
    collected = user.repair_ids if user.race_status == RACE_ACTIVE else []
    return _render_novacloud(repair_css(collected))


# --------------------------------------------------------- the reward ----

@xframe_options_sameorigin
def final_design(request):
    """Render the player's own recorded entry.

    Distinct from `final_preview`, which renders the official solution. It
    only exists once the round has been recorded.
    """
    if not request.user.is_authenticated:
        return redirect('login')

    finalize_if_due(request.user)
    final = FinalSubmission.objects.filter(user=request.user).first()
    if not final:
        return redirect('home')

    return _render_novacloud(final.final_css)


@xframe_options_sameorigin
def final_preview(request):
    """The finished NovaCloud page: the reward for crossing the finish line.

    It is locked until the server has recorded a completed race. Running out
    of time does not open it, and neither does simply being logged in — the
    fixed site is the prize, not a reference.
    """
    if not request.user.is_authenticated:
        return redirect('login')
    if not request.user.race_completed_at:
        return redirect('home')

    return _render_novacloud(SOLUTION_CSS)


# ----------------------------------------------------------------- auth ----

# A PC number names a machine on the floor. Kept permissive on purpose — the
# organisers label their lab however they like — but tight enough that a
# fat-fingered paste does not become a participant's recorded desk.
_PC_NO = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _-]{0,19}$')

AUTH_BACKEND = 'first.backends.ParticipantBackend'


def user_signup(request):
    """Register one participant.

    The PC number is *not* checked for uniqueness. A machine is used by person
    after person all day, so PC-14 legitimately appears once for each of them,
    each with their own account and their own single official attempt.
    """
    if request.user.is_authenticated:
        return redirect('start')

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        pc_no = (request.POST.get('pc_no') or '').strip()
        password = request.POST.get('password') or ''

        if not username or not pc_no or not password:
            return render(request, 'signup.html', {'error': 'All fields are required'})

        if not _PC_NO.match(pc_no):
            return render(request, 'signup.html', {
                'error': 'PC number looks wrong — use the label on your machine, '
                         'like PC-14.'})

        # The participant is the identity, so *this* is what must be unique.
        if User.objects.filter(username__iexact=username).exists():
            return render(request, 'signup.html', {
                'error': f'“{username}” is already registered. Add something to '
                         f'make it yours — every participant needs their own name.'})

        user = User.objects.create_user(username=username, pc_no=pc_no, password=password)
        user.backend = AUTH_BACKEND
        login(request, user)
        return redirect('start')

    return render(request, 'signup.html')


def user_login(request):
    """Sign in as the participant, never as the machine."""
    if request.user.is_authenticated:
        return redirect('start')

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        user = authenticate(request, username=username, password=password)
        if user:
            user.backend = AUTH_BACKEND
            login(request, user)
            return redirect('start')
        return render(request, 'login.html', {'error': 'Invalid credentials'})

    return render(request, 'login.html')


def user_logout(request):
    logout(request)
    return redirect('login')


# ---------------------------------------------------- the live scoreboard ----
#
# A read-only window onto the same participant rows the race writes. Nothing
# below mutates a race, calculates a score, or ranks anybody: the organisers
# pick the one overall winner themselves by comparing the completed runs.


def organiser_only(view):
    """Server-side authorisation, reusing the flag the Django admin already uses.

    `is_admin` is this project's single stored permission flag — the same one
    behind `is_staff`/`is_superuser` and the admin login. Reusing it means the
    organiser account that already exists is the account that can watch the
    event, and a participant can never reach these pages by editing anything
    the browser sends.
    """
    @wraps(view)
    def guarded(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect('login')
        if not user.is_admin:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return guarded


def _scoreboard_boot(mode):
    """What the page hands the script: the socket, and a first snapshot.

    Rendering the snapshot into the page means the table is already correct
    before the WebSocket has opened — and the script replaces it with the
    server's own snapshot the moment it connects.
    """
    return {
        'mode': mode,
        'socket': '/ws/scoreboard/',
        'heartbeat': SCOREBOARD_HEARTBEAT_SECONDS,
        'snapshot': scoreboard_snapshot(),
    }


@organiser_only
def scoreboard(request):
    """The organiser's monitor: every participant, live."""
    finalize_all_due()
    return render(request, 'scoreboard.html', {
        'challenge': CHALLENGE,
        'boot': _scoreboard_boot('organiser'),
    })


@organiser_only
def scoreboard_display(request):
    """The projector view: bigger, quieter, readable across a hall."""
    finalize_all_due()
    return render(request, 'scoreboard_display.html', {
        'challenge': CHALLENGE,
        'boot': _scoreboard_boot('display'),
    })


@organiser_only
def scoreboard_player(request, participant_id):
    """One participant's run, including the website they left behind."""
    participant = get_object_or_404(User, pk=participant_id, is_admin=False)
    _settle_expired(participant)
    participant.refresh_from_db()
    player = player_payload(participant)
    elapsed = player['state']['elapsed']
    return render(request, 'scoreboard_player.html', {
        'challenge': CHALLENGE,
        'participant': participant,
        'player': player,
        'elapsed_display': (f'{elapsed // 60:02d}:{elapsed % 60:02d}'
                            if participant.race_started_at else '--:--'),
        'repairs': [
            {**card, 'done': card['id'] in participant.repair_ids}
            for card in REPAIR_CARDS
        ],
        'preview_url': reverse('scoreboard_player_preview', args=[participant.pk]),
        'socket_path': '/ws/scoreboard/',
    })


@organiser_only
@xframe_options_sameorigin
def scoreboard_player_preview(request, participant_id):
    """NovaCloud exactly as that participant's own race left it.

    Composed through the same `repair_css` the participant's own preview uses,
    from the same recorded repairs, so this shows what they actually earned:
    the finished site for a completed run, the broken one for a timeout, and
    work-in-progress for a race still going.

    Being organiser-only, this cannot become a way for a participant to see
    the fixed site early — their own reward stays behind `final_preview`.
    """
    participant = get_object_or_404(User, pk=participant_id, is_admin=False)
    if participant.race_status in (RACE_ACTIVE, RACE_COMPLETED):
        collected = participant.repair_ids
    else:
        # Not started, or the clock beat them: the site is still broken.
        collected = []
    return _render_novacloud(repair_css(collected))

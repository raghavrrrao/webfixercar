import re

from django.contrib.auth import authenticate, login, logout
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
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
    RACE_BASE_POINTS,
    RACE_CLEAN_RUN_BONUS,
    RACE_COLLISION_PENALTY,
    RACE_COURSE_SECONDS,
    RACE_MAX_COLLISIONS,
    RACE_MAX_SCORE,
    RACE_OBSTACLE_COUNT,
    RACE_OBSTACLE_MARKS,
    RACE_OBSTACLE_POINTS,
    RACE_OBSTACLE_TIMES,
    RACE_STATE_SYNC_SECONDS,
    RACE_TIME_POINTS,
    RACE_TIMING_GRACE_SECONDS,
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

# Everything the templates need to name the current round in one place.
CHALLENGE = {
    'label': CHALLENGE_LABEL,
    'site': SITE_NAME,
    'tagline': SITE_TAGLINE,
    'difficulty': DIFFICULTY,
    'mode': MODE,
    'minutes': GAME_DURATION_SECONDS // 60,
    'objectives': TOTAL_CHECKS,
    'obstacles': RACE_OBSTACLE_COUNT,
}

# The course description the browser draws from. It is sent to the page rather
# than hardcoded in the script so the client and the server can never disagree
# about where an obstacle is or how long the course takes.
RACE_CONFIG = {
    'duration': GAME_DURATION_SECONDS,
    'course': RACE_COURSE_SECONDS,
    'obstacleCount': RACE_OBSTACLE_COUNT,
    'obstacleMarks': list(RACE_OBSTACLE_MARKS),
    'obstacleTimes': list(RACE_OBSTACLE_TIMES),
    'warnSeconds': TIMER_WARNING_SECONDS,
    'dangerSeconds': TIMER_DANGER_SECONDS,
    'syncSeconds': RACE_STATE_SYNC_SECONDS,
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

def _race_state(user):
    """The one shape every race endpoint answers with.

    Every number in it is read from the database or the server clock. The
    browser renders this; it never contributes to it.
    """
    return {
        'status': user.race_status,
        'started': bool(user.race_started_at),
        'remaining': user.remaining_seconds,
        'elapsed': user.elapsed_seconds if user.race_started_at else 0,
        'duration': GAME_DURATION_SECONDS,
        'course': RACE_COURSE_SECONDS,
        'expired': user.is_expired,
        'completed': bool(user.race_completed_at),
        'obstacles': user.race_obstacles,
        'obstacleCount': RACE_OBSTACLE_COUNT,
        'collisions': user.race_collisions,
        'score': user.best_score if user.race_completed_at else 0,
    }


def _settle_expired(user):
    """Record the timeout entry the moment the server notices the deadline."""
    if user.is_expired:
        finalize_if_due(user)


def race_score(elapsed, obstacles, collisions):
    """The official score. Server-side, deterministic, and the only one.

    Finishing sooner scores more, every obstacle scores, every collision
    costs, and a clean run earns the last few points. Completing the course
    perfectly at the earliest possible moment is exactly RACE_MAX_SCORE.
    """
    span = max(1, GAME_DURATION_SECONDS - RACE_COURSE_SECONDS)
    time_points = int(RACE_TIME_POINTS * (GAME_DURATION_SECONDS - elapsed) / span)
    time_points = max(0, min(RACE_TIME_POINTS, time_points))

    score = (
        RACE_BASE_POINTS
        + time_points
        + obstacles * RACE_OBSTACLE_POINTS
        - collisions * RACE_COLLISION_PENALTY
    )
    if collisions == 0 and obstacles >= RACE_OBSTACLE_COUNT:
        score += RACE_CLEAN_RUN_BONUS
    return max(0, min(RACE_MAX_SCORE, score))


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
        'race_urls': {
            'start': reverse('api_race_start'),
            'state': reverse('api_race_state'),
            'progress': reverse('api_race_progress'),
            'complete': reverse('api_race_complete'),
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
    return render(request, 'race_result.html', {
        'challenge': CHALLENGE,
        'user': user,
        'score': user.best_score,
        'max_score': RACE_MAX_SCORE,
        'time_seconds': user.race_time_seconds,
        'obstacles': user.race_obstacles,
        'obstacle_count': RACE_OBSTACLE_COUNT,
        'collisions': user.race_collisions,
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
    return JsonResponse({**_race_state(user), 'resumed': False})


def api_race_state(request):
    """Authoritative race state. The browser only renders what this says."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)
    _settle_expired(request.user)
    return JsonResponse(_race_state(request.user))


@require_POST
def api_race_progress(request):
    """Record one obstacle cleared, or one collision, while the race runs.

    This is what keeps the finish honest. An obstacle is only accepted when

      * the race is genuinely active,
      * it is the next obstacle in course order, and
      * the course has actually had time to carry it to the car.

    So the six clears the completion endpoint insists on cannot be conjured
    up: they take at least as long as driving the course does.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user = request.user
    status = user.race_status
    if status == RACE_NOT_STARTED:
        return JsonResponse(
            {**_race_state(user), 'error': 'Race has not started.'}, status=400)
    if status == RACE_COMPLETED:
        return JsonResponse(
            {**_race_state(user), 'error': 'This attempt is already finished.'},
            status=409)
    if status == RACE_EXPIRED:
        _settle_expired(user)
        return JsonResponse(
            {**_race_state(user), 'error': "Time is up."}, status=409)

    elapsed = user.elapsed_seconds
    fields = []

    raw_obstacle = request.POST.get('obstacle')
    if raw_obstacle not in (None, ''):
        try:
            index = int(raw_obstacle)
        except (TypeError, ValueError):
            return JsonResponse(
                {**_race_state(user), 'error': 'Invalid obstacle.'}, status=400)

        if not 1 <= index <= RACE_OBSTACLE_COUNT:
            return JsonResponse(
                {**_race_state(user), 'error': 'Invalid obstacle.'}, status=400)
        if index <= user.race_obstacles:
            # A replayed report. Already counted; say so without counting twice.
            return JsonResponse({**_race_state(user), 'counted': False})
        if index != user.race_obstacles + 1:
            return JsonResponse(
                {**_race_state(user), 'error': 'Obstacles must be cleared in order.'},
                status=400)
        if elapsed + RACE_TIMING_GRACE_SECONDS < RACE_OBSTACLE_TIMES[index - 1]:
            return JsonResponse(
                {**_race_state(user), 'error': 'That obstacle is still ahead of you.'},
                status=400)

        user.race_obstacles = index
        fields.append('race_obstacles')

    collision = (request.POST.get('collision') or '').strip().lower()
    if collision not in ('', '0', 'false', 'no'):
        if user.race_collisions < RACE_MAX_COLLISIONS:
            user.race_collisions += 1
            fields.append('race_collisions')

    if fields:
        user.save(update_fields=fields)
    return JsonResponse({**_race_state(user), 'counted': bool(fields)})


@require_POST
def api_race_complete(request):
    """Cross the finish line. The server decides whether that really happened.

    Nothing the browser sends is scored. The completion is accepted only when
    the server's own record says the attempt is active, the whole course has
    gone past, and every obstacle was cleared along the way — and it can only
    ever be accepted once.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user = request.user
    status = user.race_status

    if status == RACE_COMPLETED:
        # Idempotent: a retried request lands on the existing result, and
        # cannot overwrite the recorded performance.
        return JsonResponse(
            {**_race_state(user), 'redirect': reverse('race_result')}, status=409)
    if status == RACE_NOT_STARTED:
        return JsonResponse(
            {**_race_state(user), 'error': 'Race has not started.'}, status=400)
    if status == RACE_EXPIRED:
        _settle_expired(user)
        return JsonResponse(
            {**_race_state(user), 'error': "Time is up. Your race attempt has ended."},
            status=409)

    elapsed = user.elapsed_seconds
    if user.race_obstacles < RACE_OBSTACLE_COUNT:
        return JsonResponse(
            {**_race_state(user),
             'error': 'You have not cleared every obstacle yet.'}, status=400)
    if elapsed + RACE_TIMING_GRACE_SECONDS < RACE_COURSE_SECONDS:
        return JsonResponse(
            {**_race_state(user),
             'error': 'You have not reached the finish line yet.'}, status=400)

    obstacles = min(RACE_OBSTACLE_COUNT, user.race_obstacles)
    collisions = min(RACE_MAX_COLLISIONS, user.race_collisions)
    elapsed = max(1, min(GAME_DURATION_SECONDS, elapsed))
    score = race_score(elapsed, obstacles, collisions)

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
            'reached_all': obstacles == RACE_OBSTACLE_COUNT,
            'design_mode': True,
            'eligible': True,
            'hints_used': 0,
            'objectives_hinted': 0,
        },
    )
    return JsonResponse({**_race_state(user), 'redirect': reverse('race_result')})


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

def user_signup(request):
    if request.user.is_authenticated:
        return redirect('start')

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        pc_no = (request.POST.get('pc_no') or '').strip()
        password = request.POST.get('password') or ''

        if not username or not pc_no or not password:
            return render(request, 'signup.html', {'error': 'All fields are required'})

        if User.objects.filter(pc_no=pc_no).exists():
            return render(request, 'signup.html', {'error': 'PC number already registered'})

        user = User.objects.create_user(username=username, pc_no=pc_no, password=password)
        user.backend = 'first.backends.PCNoBackend'
        login(request, user)
        return redirect('start')

    return render(request, 'signup.html')


def user_login(request):
    if request.user.is_authenticated:
        return redirect('start')

    if request.method == 'POST':
        pc_no = (request.POST.get('pc_no') or '').strip()
        password = request.POST.get('password') or ''
        user = authenticate(request, pc_no=pc_no, password=password)
        if user:
            user.backend = 'first.backends.PCNoBackend'
            login(request, user)
            return redirect('start')
        return render(request, 'login.html', {'error': 'Invalid credentials'})

    return render(request, 'login.html')


def user_logout(request):
    logout(request)
    return redirect('login')

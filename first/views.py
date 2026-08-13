import re

from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError, transaction
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from .checks import HINT_LEVELS, TOTAL_CHECKS, hint_text, run_checks
from .game_config import (
    CHALLENGE_HTML,
    DIFFICULTY,
    GAME_DURATION_SECONDS,
    MODE,
    CHALLENGE_LABEL,
    SITE_NAME,
    SITE_TAGLINE,
    SOLUTION_CSS,
    STARTER_CSS,
    TIMER_DANGER_SECONDS,
    TIMER_SYNC_INTERVAL_SECONDS,
    TIMER_WARNING_SECONDS,
)
from .models import CssRule, FinalSubmission, HintReveal, User
from .templatetags.wf_hints import code_spans

# Generous ceiling; the shipped stylesheet is ~25 KB.
MAX_SUBMISSION_CHARS = 200_000

# Everything the templates need to name the current round in one place.
CHALLENGE = {
    'label': CHALLENGE_LABEL,
    'site': SITE_NAME,
    'tagline': SITE_TAGLINE,
    'difficulty': DIFFICULTY,
    'mode': MODE,
    'minutes': GAME_DURATION_SECONDS // 60,
    'objectives': TOTAL_CHECKS,
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
    """The player's stylesheet. The markup is fixed and never stored per-user."""
    submission, _ = CssRule.objects.get_or_create(
        user=user,
        defaults={'html': '', 'css': STARTER_CSS},
    )
    return submission


def _record_progress(user, passed):
    """Persist a new personal best, and completion once every objective is met.

    Completion is only awarded while the session is still live: running out of
    time closes the round whatever the last submission scores.
    """
    fields = []
    if passed > user.best_score:
        user.best_score = passed
        fields.append('best_score')
    if passed == TOTAL_CHECKS and not user.completed_at and not user.is_expired:
        user.completed_at = timezone.now()
        fields.append('completed_at')
    if fields:
        user.save(update_fields=fields)


def finalize_if_due(user):
    """Freeze an unfinished round at the authoritative server deadline.

    New race attempts use race_started_at and a 1000-point result. Legacy
    records created by the previous CSS-editor version still finalize using
    their old submission data, so an existing database remains readable.
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
            'score': user.best_score,
            'total': 1000,
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
        final_submission__isnull=True,
        game_start_time__lte=timezone.now() - timezone.timedelta(
            seconds=GAME_DURATION_SECONDS),
    )
    return sum(1 for user in due if finalize_if_due(user) is not None)


def _state(user):
    """Backward-compatible state shape, now representing the race."""
    final = FinalSubmission.objects.filter(user=user).first()
    return {
        'remaining': user.remaining_seconds,
        'duration': GAME_DURATION_SECONDS,
        'expired': user.is_expired,
        'completed': user.design_mode,
        'designMode': user.design_mode,
        'locked': user.is_locked,
        'score': user.best_score,
        'total': TOTAL_CHECKS,
        'eligible': user.is_eligible,
        'hintsUsed': 0,
        'objectivesHinted': 0,
        'maxHints': 0,
        'submitted': final is not None,
        'submittedAt': final.submitted_at.isoformat() if final else None,
    }


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
    """Broken-site briefing. The 12-minute clock starts only when the race starts."""
    if not request.user.is_authenticated:
        return redirect('login')

    _ensure_session(request)
    state = _state(request.user)
    return render(request, 'entry.html', {
        'challenge': CHALLENGE,
        'state': state,
        'broken_url': reverse('api_broken_preview'),
        'race_urls': {
            'start': reverse('api_race_start'),
            'state': reverse('api_race_state'),
            'complete': reverse('api_race_complete'),
            'result': reverse('race_result'),
            'fixed': reverse('api_final_preview'),
            'exit': reverse('logout'),
        },
    })


def race_result(request):
    if not request.user.is_authenticated:
        return redirect('login')
    user = request.user
    if not user.race_completed_at:
        return redirect('home')
    return render(request, 'race_result.html', {
        'challenge': CHALLENGE,
        'user': user,
        'score': user.best_score,
        'time_seconds': user.race_time_seconds,
        'obstacles': user.race_obstacles,
        'collisions': user.race_collisions,
        'fixed_url': reverse('api_final_preview'),
    })


def _render_novacloud(css):
    """The challenge markup rendered with `css`, for a sandboxed iframe.

    Used by both preview endpoints — the official solution and a player's own
    submitted design — so the two render through exactly the same path and
    differ only in which stylesheet goes in.

    The site sends X-Frame-Options: DENY everywhere else; these two views are
    relaxed to SAMEORIGIN because the arena and the admin frame them. They
    stay un-embeddable by any other origin, and the frames carry an empty
    `sandbox` so the CSS inside cannot reach the page around it.
    """
    document = _STYLESHEET_LINK.sub(
        lambda _: '<style>' + css + '</style>', CHALLENGE_HTML, count=1,
    )
    response = HttpResponse(document, content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@require_POST
def api_hint(request):
    """Reveal one hint, and record that it was revealed.

    The count is kept here, not in the browser: a level is charged the first
    time it is opened and never again, so re-reading a hint is free and no
    amount of clicking (or a forged `hints_used`) can change the total.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user = request.user
    if user.is_locked:
        payload = _state(user)
        payload['error'] = 'Time is up!'
        return JsonResponse(payload, status=200)

    objective = (request.POST.get('objective') or '').strip()
    try:
        level = int(request.POST.get('level') or 0)
    except (TypeError, ValueError):
        level = 0

    text = hint_text(objective, level)
    if text is None:
        return JsonResponse({'error': 'No such hint'}, status=400)

    _, created = HintReveal.objects.get_or_create(
        user=user, objective=objective, level=level,
    )

    payload = _state(user)
    payload.update({
        'objective': objective,
        'level': level,
        'hint': text,
        'hintHtml': code_spans(text),
        'charged': created,
    })
    return JsonResponse(payload)


@xframe_options_sameorigin
def broken_preview(request):
    """Render the intentionally broken NovaCloud page before the race."""
    return _render_novacloud(STARTER_CSS)


@require_POST
def api_race_start(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)
    user = request.user
    if user.race_completed_at:
        return JsonResponse({'error': 'Challenge already completed.', 'completed': True}, status=409)
    if user.race_started_at and not user.is_expired:
        return JsonResponse(_race_state(user))
    if user.race_started_at and user.is_expired:
        return JsonResponse({'error': 'Time is up. This attempt has ended.', 'expired': True}, status=409)

    user.start_challenge()
    return JsonResponse(_race_state(user))


def _race_state(user):
    return {
        'started': bool(user.race_started_at),
        'remaining': user.remaining_seconds,
        'duration': GAME_DURATION_SECONDS,
        'expired': user.is_expired,
        'completed': bool(user.race_completed_at),
        'score': user.best_score,
        'obstacles': user.race_obstacles,
        'collisions': user.race_collisions,
    }


def api_race_state(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)
    return JsonResponse(_race_state(request.user))


@require_POST
def api_race_complete(request):
    """Server-authoritative race completion and score calculation."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user = request.user
    if user.race_completed_at:
        return JsonResponse({**_race_state(user), 'redirect': reverse('race_result')})
    if not user.race_started_at:
        return JsonResponse({'error': 'Race has not started.'}, status=400)
    if user.is_expired:
        return JsonResponse({**_race_state(user), 'error': 'Time is up.'}, status=409)

    try:
        obstacles = max(0, min(6, int(request.POST.get('obstacles') or 0)))
        collisions = max(0, min(50, int(request.POST.get('collisions') or 0)))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid race data.'}, status=400)

    elapsed = int((timezone.now() - user.race_started_at).total_seconds())
    elapsed = max(1, min(GAME_DURATION_SECONDS, elapsed))

    # Fair, deterministic score: finishing quickly matters, every obstacle
    # matters, and collisions cost points. Completing all six obstacles within
    # 12 minutes can reach 1000; no client-supplied score is trusted.
    time_points = max(0, int((GAME_DURATION_SECONDS - elapsed) / GAME_DURATION_SECONDS * 400))
    obstacle_points = obstacles * 80
    collision_penalty = collisions * 15
    score = max(0, min(1000, 120 + time_points + obstacle_points - collision_penalty))
    if obstacles == 6:
        score = min(1000, score + 80)

    now = timezone.now()
    user.race_completed_at = now
    user.completed_at = now
    user.race_time_seconds = elapsed
    user.race_obstacles = obstacles
    user.race_collisions = collisions
    user.best_score = score
    user.save(update_fields=[
        'race_completed_at', 'completed_at', 'race_time_seconds',
        'race_obstacles', 'race_collisions', 'best_score',
    ])

    # Store an immutable competition record. The completed game is rewarded
    # with the official fixed stylesheet, while timeout records remain ineligible.
    FinalSubmission.objects.get_or_create(
        user=user,
        defaults={
            'pc_no': user.pc_no,
            'started_at': user.race_started_at,
            'submitted_at': now,
            'final_css': SOLUTION_CSS,
            'score': score,
            'total': 1000,
            'reached_all': obstacles == 6,
            'design_mode': True,
            'eligible': True,
            'hints_used': 0,
            'objectives_hinted': 0,
        },
    )
    return JsonResponse({**_race_state(user), 'elapsed': elapsed, 'redirect': reverse('race_result')})


@xframe_options_sameorigin
def final_design(request):
    """Render the player's own submitted design: the markup + *their* CSS.

    Distinct from `final_preview`, which renders the official solution. This
    one is what the judges look at, and it only exists once the round has
    been submitted.
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
    """Render the finished NovaCloud page: the fixed markup + the gold standard.

    A *visual reference only*. It reads nothing from the player and writes
    nothing back: no grading, no progress, no autosave, and deliberately no
    `start_challenge()` call, so opening it never touches the clock.

    The composed document is served from here rather than embedded in the arena
    page so the arena's own source never carries the answers, and it is loaded
    into a sandboxed iframe so it cannot reach the game shell.
    """
    if not request.user.is_authenticated:
        return redirect('login')

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


# ------------------------------------------------------------------ api ----

def _read_submitted_css(request):
    """The challenge is CSS only: any `html` field in the POST is ignored.

    The markup the player sees is read-only and the markup the checker grades
    always comes from `CHALLENGE_HTML`, so a forged `html` parameter cannot
    change the page or the score.
    """
    return (request.POST.get('css') or '')[:MAX_SUBMISSION_CHARS]


@require_POST
def save_css(request):
    """Autosave. Refuses writes once the session is locked (time up / done)."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    state = _state(request.user)
    if state['locked']:
        state['error'] = 'Time is up!' if state['expired'] else 'Challenge already completed.'
        return JsonResponse(state, status=200)

    css = _read_submitted_css(request)
    CssRule.objects.update_or_create(
        user=request.user, defaults={'css': css},
    )
    return JsonResponse(state)


def get_css(request):
    if not request.user.is_authenticated:
        return JsonResponse({'html': '', 'css': ''})
    submission = _get_submission(request.user)
    # The markup is the same for everybody and is never edited.
    return JsonResponse({'html': CHALLENGE_HTML, 'css': submission.css})


def api_state(request):
    """Authoritative countdown source. The browser only renders it."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)
    return JsonResponse(_state(request.user))


@require_POST
def api_check(request):
    """Save, then grade the submission against the challenge objectives."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)

    user = request.user
    if user.is_locked:
        submission = _get_submission(user)
        payload = _state(user)
        payload['checks'] = run_checks(CHALLENGE_HTML, submission.css)
        payload['error'] = 'Time is up!' if user.is_expired else 'Challenge already completed.'
        return JsonResponse(payload)

    css = _read_submitted_css(request)
    CssRule.objects.update_or_create(user=user, defaults={'css': css})

    checks = run_checks(CHALLENGE_HTML, css)
    passed = sum(1 for check in checks if check['passed'])
    _record_progress(user, passed)

    payload = _state(user)
    payload['checks'] = checks
    payload['passed'] = passed
    return JsonResponse(payload)


@require_POST
def api_reset(request):
    """Put the broken page back. Does not touch the countdown."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Not logged in'}, status=403)
    if request.user.is_locked:
        return JsonResponse(_state(request.user))

    CssRule.objects.update_or_create(
        user=request.user, defaults={'css': STARTER_CSS},
    )
    payload = _state(request.user)
    payload.update({'css': STARTER_CSS})
    return JsonResponse(payload)

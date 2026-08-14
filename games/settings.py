"""
Django settings for games project (Website Fixer).

Runs under ASGI (Daphne + Django Channels) so the live player-count
WebSocket works both locally and on Render.
"""

from pathlib import Path
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_URL = BASE_DIR / "template"


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name):
    """Comma-separated environment value -> list, ignoring blanks."""
    return [item.strip() for item in os.environ.get(name, '').split(',') if item.strip()]


def _env_first(*names):
    """First non-empty value among `names`, or ''."""
    for name in names:
        value = os.environ.get(name, '').strip()
        if value:
            return value
    return ''


# The development key. Fine for `runserver` on a laptop; it is in the
# repository, so it signs nothing anybody should trust.
_DEV_SECRET_KEY = 'django-insecure-1)*yy1*vlg4_m@(3l7rl(y+yocy#@e^f)pze6=)onh^6)(-3dq'

# SECURITY WARNING: keep the secret key used in production secret!
#
# `SECRET_KEY` is the name Render's blueprint sets. `DJANGO_SECRET_KEY` is
# accepted too so an existing deployment configured before this does not stop
# booting when it is redeployed.
SECRET_KEY = _env_first('SECRET_KEY', 'DJANGO_SECRET_KEY') or _DEV_SECRET_KEY

# SECURITY WARNING: don't run with debug turned on in production!
#
# Production is declared, never guessed. Nothing here infers it from a
# hostname, from RENDER, or from anything else the platform happens to set:
# the deployment says what it is, and every guard below follows that.
DEBUG = _env_bool('DJANGO_DEBUG', True)

# Session cookies are signed with SECRET_KEY. Shipping the published one to a
# real event would let anybody who read the repository mint an organiser
# session, so a production-shaped run refuses to boot without its own.
if not DEBUG and SECRET_KEY == _DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        'SECRET_KEY must be set when DJANGO_DEBUG is off. The development key '
        'is published in this repository and would let anybody forge an '
        'organiser session. Generate one with:\n'
        "  python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )

# Which hostnames this server answers to. `DJANGO_ALLOWED_HOSTS` is a
# comma-separated list -- on event day that is the lab machine's LAN address
# and hostname, e.g. "192.168.1.20,fixer.local".
#
# The wildcard stays the default in DEBUG so `runserver` keeps working from a
# phone on the same wifi without configuration. A production-shaped run needs
# the list, because ALLOWED_HOSTS is what stops a poisoned Host header turning
# into a password-reset link pointing at somebody else's server.
ALLOWED_HOSTS = _env_list('DJANGO_ALLOWED_HOSTS')
_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()
if _render_host:
    ALLOWED_HOSTS.append(_render_host)
    ALLOWED_HOSTS.append('.onrender.com')
if not ALLOWED_HOSTS:
    if DEBUG:
        ALLOWED_HOSTS = ['*']
    else:
        raise ImproperlyConfigured(
            'DJANGO_ALLOWED_HOSTS must list the hostnames this server answers '
            'to when DJANGO_DEBUG is off, e.g. '
            'DJANGO_ALLOWED_HOSTS=192.168.1.20,fixer.local'
        )

# Origins allowed to POST. Only consulted for HTTPS requests, so a plain-HTTP
# LAN event needs nothing here; a fest served over TLS on its own domain does.
CSRF_TRUSTED_ORIGINS = _env_list('DJANGO_CSRF_TRUSTED_ORIGINS') or []
CSRF_TRUSTED_ORIGINS.append('https://*.onrender.com')
if _render_host:
    CSRF_TRUSTED_ORIGINS.append(f'https://{_render_host}')


# Application definition

INSTALLED_APPS = [
    # 'daphne' must come first so `manage.py runserver` serves ASGI (WebSockets).
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'first',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'games.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [TEMP_URL],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'games.wsgi.application'
ASGI_APPLICATION = 'games.asgi.application'


# ------------------------------------------------------- hosted vs local ----
#
# Two deployments are supported, and they are told apart by explicit
# environment variables rather than by sniffing the platform:
#
#   HOSTED     DJANGO_DEBUG=false, DATABASE_URL (Postgres), REDIS_URL.
#              Render, or anything shaped like it. This is the default meaning
#              of "not DEBUG", and the guards below enforce it.
#
#   ONE BOX    a laptop or lab machine running the whole event by itself, on
#              SQLite and a single Daphne process, as EVENT-OPERATIONS.md
#              describes. That is a legitimate production run, but it has to
#              say so out loud with WF_SINGLE_MACHINE=true, because the point
#              of the guards is that nobody reaches Render on SQLite by
#              accident and loses the event to an ephemeral disk.
SINGLE_MACHINE = _env_bool('WF_SINGLE_MACHINE', False)


# Channel layer -- how a race event reaches the scoreboard sockets.
#
# In-memory is a Python dictionary inside one process. It is correct for
# `runserver` and for a single Daphne worker, and wrong the moment there are
# two: a race handled by worker A never reaches a scoreboard socket held by
# worker B. Redis is what makes the fan-out cross process boundaries.
#
# Either way the channel layer only ever carries live *notifications*. Every
# scoreboard is rebuilt from the database on connect and on reconnect, so
# losing the layer degrades liveness and never correctness.
REDIS_URL = os.environ.get('REDIS_URL', '').strip()

if not DEBUG and not REDIS_URL and not SINGLE_MACHINE:
    raise ImproperlyConfigured(
        'REDIS_URL must be set for a hosted deployment. Without it the '
        'channel layer lives inside one process, so a scoreboard served by a '
        'second worker or instance never receives live race events. On Render '
        'this comes from the Key Value instance in render.yaml.\n'
        'If this really is one machine running the whole event by itself, '
        'set WF_SINGLE_MACHINE=true to say so.'
    )

if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {
        'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}
    }


# Database
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases
#
# SQLite by default, so a fresh checkout runs with no configuration.
# DATABASE_URL (Postgres) in production -- see the guard below.

DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# The database *is* the event: every race, score, repair and reward lives in
# it and nothing is held in the web process. A hosted platform gives each
# deploy a fresh, empty filesystem, so a SQLite file there is deleted on every
# deploy and on every restart after an idle spin-down -- taking the entire
# event with it, silently, with no error anywhere.
#
# So a production run refuses to start on SQLite unless it is the one-box
# deployment above, where the file lives on a real disk somebody can back up.
if (not DEBUG and not SINGLE_MACHINE
        and 'sqlite' in DATABASES['default'].get('ENGINE', '')):
    raise ImproperlyConfigured(
        'DATABASE_URL must point at PostgreSQL for a hosted deployment. '
        'This run fell back to SQLite, and a hosted filesystem is ephemeral: '
        'the file — and every participant, score and result in it — is '
        'destroyed on the next deploy or restart.\n'
        'On Render this comes from the Postgres instance in render.yaml.\n'
        'If this really is one machine running the whole event off a disk you '
        'back up yourself, set WF_SINGLE_MACHINE=true to say so.'
    )


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Compressed (gzip/brotli) but not hashed: WhiteNoise still serves these fine,
# and `manage.py test` / any DEBUG=False run works before collectstatic has run.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = "first.User"

AUTHENTICATION_BACKENDS = [
    "first.backends.ParticipantBackend",   # sign in as the participant
    "django.contrib.auth.backends.ModelBackend",  # keep default
]


LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'start'
LOGOUT_REDIRECT_URL = 'login'

if not DEBUG:
    # Render terminates TLS at its proxy, so trust the forwarded scheme.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Off switch for running a production-shaped server locally over plain HTTP.
    SECURE_SSL_REDIRECT = _env_bool('DJANGO_SSL_REDIRECT', True)
    # The platform's health check may reach the instance over plain HTTP from
    # inside the network. Redirecting it to HTTPS would make a healthy
    # instance look unhealthy, so this one path answers either way.
    SECURE_REDIRECT_EXEMPT = [r'^healthz/?$']
    # HSTS is intentionally not enabled: browsers cache it for its full
    # duration, which is painful to undo while the domain is still changing.

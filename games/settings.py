"""
Django settings for games project (Website Fixer).

Runs under ASGI (Daphne + Django Channels) so the live player-count
WebSocket works both locally and on Render.
"""

from pathlib import Path
import os

import dj_database_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_URL = BASE_DIR / "template"


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-1)*yy1*vlg4_m@(3l7rl(y+yocy#@e^f)pze6=)onh^6)(-3dq',
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool('DJANGO_DEBUG', True)

ALLOWED_HOSTS = ['*']

# Render exposes the public hostname here; needed for POST/CSRF over HTTPS.
CSRF_TRUSTED_ORIGINS = ['https://*.onrender.com']
_render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
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


# Channel layer.
# In-memory is correct for a single process (local dev, one Render instance).
# Set REDIS_URL to broadcast across several instances -- see README.
REDIS_URL = os.environ.get('REDIS_URL', '').strip()
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
# SQLite locally; DATABASE_URL (Postgres) in production.

DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}


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
    "first.backends.PCNoBackend",   # custom backend
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
    # HSTS is intentionally not enabled: browsers cache it for its full
    # duration, which is painful to undo while the domain is still changing.

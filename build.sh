#!/usr/bin/env bash
# Render build step.
#
# Every stage below is required. `set -e` stops on the first failure and
# `set -o pipefail` makes sure a failure inside a pipeline is not swallowed by
# a successful tail, so a broken migration fails the deploy loudly instead of
# shipping an application whose schema does not match its code.
set -o errexit
set -o nounset
set -o pipefail

echo "==> Installing dependencies"
pip install --no-cache-dir -r requirements.txt

echo "==> Checking the project"
# Catches a misconfigured deployment before any of it reaches the database:
# missing SECRET_KEY, SQLite in production, missing REDIS_URL.
python manage.py check --deploy

echo "==> Checking for unmade migrations"
# The schema in the database is built from the migration files. If a model
# has changed without one, the deploy would run against the wrong schema.
python manage.py makemigrations --check --dry-run

echo "==> Collecting static files"
python manage.py collectstatic --no-input

echo "==> Applying database migrations"
python manage.py migrate --no-input

echo "==> Ensuring the organiser account exists"
# Idempotent. Refuses to create the account if WF_ADMIN_PASSWORD is missing
# rather than inventing a guessable one, and never prints the password.
python manage.py ensure_admin

echo "==> Build complete"

#!/usr/bin/env bash
# Render build step.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate

# Non-interactive and idempotent: safe to run on every deploy.
python manage.py ensure_admin

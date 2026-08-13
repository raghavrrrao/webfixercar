"""Template helpers for the arena's hint panel."""

import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

_CODE_SPAN = re.compile(r'`([^`]+)`')


@register.filter
def code_spans(text):
    """Render `backtick spans` in a hint as <code>, e.g. `.navbar` -> code.

    The whole string is HTML-escaped *first*, so the only markup that can ever
    reach the page is the <code> wrapper this function adds. Hints are static
    strings written in `first/checks.py`, never player input, but escaping
    first keeps that true even if that ever changes.
    """
    return mark_safe(_CODE_SPAN.sub(r'<code>\1</code>', escape(text)))

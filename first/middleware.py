"""Keep signed-in pages out of the browser cache.

The event runs on shared machines. One participant finishes, logs out, and
hands the keyboard straight to the next person — who can press Back. Without a
cache directive a browser is free to re-display the previous participant's
briefing, their result page with their score on it, or an organiser's whole
scoreboard, all from its own cache and without asking the server. Logging out
does not reach into the cache to clean up after itself.

Every server-side authorisation check in the project is correct; this is about
the responses that were legitimately served *before* the logout still sitting
on the disk of a machine somebody else is now using.
"""


class PrivateResponsesNoStoreMiddleware:
    """Mark every response to a signed-in request as uncacheable.

    Deliberately the last middleware, for two reasons: `request.user` needs
    AuthenticationMiddleware to have run, and WhiteNoise short-circuits static
    files before reaching here, so the CSS and JS that *should* be cached still
    are.
    """

    HEADER = 'no-store, no-cache, must-revalidate, max-age=0'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        user = getattr(request, 'user', None)
        if not (user is not None and user.is_authenticated):
            return response

        # The NovaCloud previews already set their own no-store; leave any
        # view that has taken an explicit decision alone.
        if not response.has_header('Cache-Control'):
            response['Cache-Control'] = self.HEADER
        response.setdefault('Pragma', 'no-cache')
        # The same URL genuinely differs per participant, so a shared cache
        # must never treat one participant's copy as anybody else's.
        patch_vary(response, 'Cookie')
        return response


def patch_vary(response, header):
    existing = [v.strip() for v in response.get('Vary', '').split(',') if v.strip()]
    if header.lower() not in {v.lower() for v in existing}:
        existing.append(header)
    response['Vary'] = ', '.join(existing)

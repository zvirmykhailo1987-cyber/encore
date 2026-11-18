"""Rate limits per key, and per address for callers with no valid key. Limits live in settings."""

from rest_framework.throttling import SimpleRateThrottle

from .permissions import principal


class PerKeyThrottle(SimpleRateThrottle):
    scope = "key"

    def get_cache_key(self, request, view):
        who = principal(request)
        if who is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": f"{who.scope}:{who.key_id or 'bootstrap'}"}


class NoKeyThrottle(SimpleRateThrottle):
    """Slows down anyone calling without a valid key, including key guessers.

    DRF checks permissions before throttles, so a refused call would never be counted there. Authentication
    runs this instead, before anything else answers. It must not touch request.user, which is not set yet.
    """

    scope = "no-key"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}

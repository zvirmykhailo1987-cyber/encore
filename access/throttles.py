"""Rate limits per key, and per address for callers with no valid key. Limits live in settings."""

from rest_framework.throttling import SimpleRateThrottle

from .permissions import principal


class PerKeyThrottle(SimpleRateThrottle):
    scope = "key"

    def allow_request(self, request, view):
        # Gate keys get their own, higher rate. The rate is chosen per request, before the window is checked.
        who = principal(request)
        self.scope = "gate-key" if who is not None and who.scope == "gate" else "key"
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

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

"""Who is calling. A missing key is 401, a wrong or revoked key is 401, never a silent pass."""

import datetime as dt
import hmac
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, Throttled

from .models import ApiKey, digest
from .principal import PLATFORM, Principal
from .throttles import NoKeyThrottle

#: last_used_at is a hint for people cleaning up keys, so it is written at most once a minute.
TOUCH_EVERY = dt.timedelta(minutes=1)


def supplied_key(request) -> str:
    header = request.headers.get("Authorization", "")
    return header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""


def keys_configured() -> bool:
    return bool(settings.ENCORE_OPERATOR_TOKEN) or ApiKey.objects.filter(revoked_at__isnull=True).exists()


def debug_machine_open() -> bool:
    return settings.DEBUG and not keys_configured()


def count_call_without_key(request) -> None:
    """Raises 429 once an address has made too many calls without a valid key."""
    if debug_machine_open():
        return
    throttle = NoKeyThrottle()
    if not throttle.allow_request(request, None):
        raise Throttled(throttle.wait())


class ApiKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        supplied = supplied_key(request)
        if not supplied:
            count_call_without_key(request)
            return None
        bootstrap = settings.ENCORE_OPERATOR_TOKEN
        if bootstrap and hmac.compare_digest(supplied.encode(), bootstrap.encode()):
            return PLATFORM, None
        key = ApiKey.objects.filter(digest=digest(supplied), revoked_at__isnull=True).first()
        if key is None:
            count_call_without_key(request)
            raise AuthenticationFailed("Unknown or revoked API key")
        now = timezone.now()
        if key.last_used_at is None or now - key.last_used_at > TOUCH_EVERY:
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
        return Principal(key.scope, key.organizer, key.pk), key

    def authenticate_header(self, request) -> str:
        return 'Bearer realm="encore"'


class DebugMachineAuthentication(BaseAuthentication):
    """A developer's machine with no keys at all stays usable, but never from another website."""

    def authenticate(self, request):
        if supplied_key(request) or not debug_machine_open():
            return None
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.get_host():
            return None
        return PLATFORM, None

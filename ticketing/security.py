import hashlib
import hmac
import time
from urllib.parse import urlsplit

from django.conf import settings
from rest_framework.permissions import SAFE_METHODS, BasePermission

WEBHOOK_TOLERANCE_SECONDS = 300


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def webhook_is_authentic(body: bytes, timestamp: str, signature: str, now: float | None = None) -> bool:
    secret = settings.TICKETING_WEBHOOK_SECRET
    if not secret or not timestamp or not signature:
        return False
    try:
        age = abs((now if now is not None else time.time()) - float(timestamp))
    except ValueError:
        return False
    if age > WEBHOOK_TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(sign_webhook(secret, timestamp, body), signature)


class OperatorToken(BasePermission):
    """Writes need the operator token. With no token set they are open only on a debug machine."""

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return True
        token = settings.ENCORE_OPERATOR_TOKEN
        if not token:
            # Open on a debug machine, but not to another website the operator happens to have open.
            origin = request.headers.get("Origin")
            same_site = not origin or urlsplit(origin).netloc == request.get_host()
            return settings.DEBUG and same_site
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(supplied.encode(), token.encode())

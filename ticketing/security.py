import hashlib
import hmac
import re
import time

from django.conf import settings

WEBHOOK_TOLERANCE_SECONDS = 300
TIMESTAMP = re.compile(r"\d{1,12}(?:\.\d{1,9})?")


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def webhook_is_authentic(body: bytes, timestamp: str, signature: str, now: float | None = None) -> bool:
    secret = settings.TICKETING_WEBHOOK_SECRET
    if not secret or not timestamp or not signature:
        return False
    # Unix seconds, optionally with a fraction. float() alone would accept "nan" or "inf", and abs(nan) > tolerance
    # is False, so a message signed with it would never expire.
    if not TIMESTAMP.fullmatch(timestamp):
        return False
    age = abs((now if now is not None else time.time()) - float(timestamp))
    if age > WEBHOOK_TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(sign_webhook(secret, timestamp, body), signature)

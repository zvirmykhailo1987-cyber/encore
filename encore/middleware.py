from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

# Style attributes carry the board's layout variables, so inline styles stay allowed. Scripts do not.
POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class ContentSecurityPolicy:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not settings.DEBUG:
            response.headers.setdefault("Content-Security-Policy", POLICY)
        return response

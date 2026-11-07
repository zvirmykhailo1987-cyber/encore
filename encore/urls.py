from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.urls import include, path

from ticketing.views import event_page


def healthz(_request: HttpRequest) -> JsonResponse:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"ok": True})


urlpatterns = [
    path("healthz", healthz),
    path("api/v1/imports/", include("imports.urls")),
    path("api/v1/", include("ticketing.urls")),
    path("events/<str:event>", event_page),
    path("", include("console.urls")),
]

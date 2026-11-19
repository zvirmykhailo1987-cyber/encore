# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

from django.urls import include, path

from access.views import WhoAmIView
from ticketing.views import event_page

# /livez, /healthz and /metrics are answered by encore.middleware.OperationalEndpoints.
urlpatterns = [
    path("api/v1/whoami", WhoAmIView.as_view()),
    path("api/v1/imports/", include("imports.urls")),
    path("api/v1/", include("ticketing.urls")),
    path("events/<str:event>", event_page),
    path("", include("console.urls")),
]

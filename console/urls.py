from django.conf import settings
from django.urls import path, re_path
from django.views.static import serve

from . import views

urlpatterns = [
    path("", views.overview, name="console-overview"),
    path("runs/<int:pk>", views.run_detail, name="console-run"),
    path("runs/<int:pk>/quarantine", views.quarantine, name="console-quarantine"),
    path("integration", views.integration, name="console-integration"),
    path("load-results", views.benchmarks, name="console-benchmarks"),
    # gunicorn does not serve static files, and no static-file package is installed. A handful of small local
    # files for one operator, so Django's own path-safe view is enough here.
    re_path(r"^static/(?P<path>.+)$", serve, {"document_root": settings.BASE_DIR / "static"}),
]

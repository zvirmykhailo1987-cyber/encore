from django.urls import path

from . import views

urlpatterns = [
    path("scans", views.ScanView.as_view()),
    path("events/<str:event>/availability", views.AvailabilityView.as_view()),
    path("connector/health", views.ConnectorHealthView.as_view()),
    path("connector/faults", views.DemoFaultsView.as_view()),
    path("connector/dead-letters", views.DeadLetterListView.as_view()),
    path("connector/dead-letters/<int:pk>/resolve", views.DeadLetterResolveView.as_view()),
    path("webhooks/ticketing", views.ticketing_webhook),
]

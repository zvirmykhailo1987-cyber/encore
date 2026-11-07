from django.urls import path

from . import views

urlpatterns = [
    path("sources", views.SourceListView.as_view()),
    path("runs", views.RunListView.as_view()),
    path("runs/<int:pk>", views.RunDetailView.as_view()),
    path("runs/<int:pk>/quarantine", views.QuarantineListView.as_view()),
    path("runs/<int:pk>/<str:action>", views.RunActionView.as_view()),
    path("quarantine/<int:row_id>/<str:action>", views.QuarantineActionView.as_view()),
]

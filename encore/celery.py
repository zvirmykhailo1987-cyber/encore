# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "encore.settings")

app = Celery("encore")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

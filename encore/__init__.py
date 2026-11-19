# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

from .celery import app as celery_app

__all__ = ["celery_app"]

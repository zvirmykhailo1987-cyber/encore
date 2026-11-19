#!/usr/bin/env python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "encore.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

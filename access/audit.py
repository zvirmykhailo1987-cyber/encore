# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

from .models import AccessRecord, ApiKey
from .permissions import principal


class Audited:
    """Mixed into views that return or change personal data. Every answer to a known caller is recorded.

    `audited_methods` narrows it, for a view where only some methods touch personal data.
    """

    audited_methods: frozenset[str] | None = None

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)  # type: ignore[misc]
        who = principal(request)
        if who is not None and (self.audited_methods is None or request.method in self.audited_methods):
            key = request.auth if isinstance(request.auth, ApiKey) else None
            AccessRecord.objects.create(
                scope=who.scope,
                organizer=who.organizer,
                key_prefix=key.prefix if key else "bootstrap",
                method=request.method,
                path=request.get_full_path()[:200],
                status=response.status_code,
            )
        return response

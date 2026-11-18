from .models import AccessRecord, ApiKey
from .permissions import principal


class Audited:
    """Mixed into views that return or change personal data. Every answer to a known caller is recorded."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)  # type: ignore[misc]
        who = principal(request)
        if who is not None:
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

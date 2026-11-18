"""Every API view is closed unless it names who may call it. The default in settings is Authenticated."""

from rest_framework.permissions import BasePermission

from .principal import Principal


def principal(request) -> Principal | None:
    user = getattr(request, "user", None)
    return user if isinstance(user, Principal) else None


class Authenticated(BasePermission):
    def has_permission(self, request, view) -> bool:
        return principal(request) is not None


class _Scopes(BasePermission):
    scopes: frozenset[str] = frozenset()
    message = "This API key cannot do that"

    def has_permission(self, request, view) -> bool:
        who = principal(request)
        return who is not None and who.scope in self.scopes


class PlatformOnly(_Scopes):
    scopes = frozenset({"platform"})


class ImportsAccess(_Scopes):
    scopes = frozenset({"platform", "organizer"})


class GateAccess(_Scopes):
    scopes = frozenset({"platform", "gate"})


class Public(BasePermission):
    """Named on purpose, so a reader sees the view is meant to be open."""

    def has_permission(self, request, view) -> bool:
        return True

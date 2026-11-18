from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """Who is calling. DRF keeps it in request.user."""

    scope: str
    organizer: str | None = None
    key_id: int | None = None

    is_authenticated = True

    @property
    def is_platform(self) -> bool:
        return self.scope == "platform"


#: The bootstrap key from ENCORE_OPERATOR_TOKEN, and the open debug machine.
PLATFORM = Principal("platform")

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .permissions import Authenticated, principal


class WhoAmIView(APIView):
    """What the caller's key may do, so a client can show only what will work."""

    permission_classes = [Authenticated]

    def get(self, request: Request) -> Response:
        who = principal(request)
        assert who is not None
        return Response({"scope": who.scope, "organizer": who.organizer})

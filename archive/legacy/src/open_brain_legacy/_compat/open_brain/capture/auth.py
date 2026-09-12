# Private legacy compatibility snapshot; excluded from every shipping artifact.
from __future__ import annotations

import hmac


class BearerAuthenticator:
    """Validates one composition-provided bearer token without exposing it."""

    def __init__(self, expected_token: str) -> None:
        if (
            not isinstance(expected_token, str)
            or not expected_token
            or expected_token.isspace()
            or not expected_token.isascii()
            or any(character.isspace() for character in expected_token)
        ):
            raise ValueError("invalid bearer token")
        self._expected_token = expected_token.encode("ascii")

    def authenticate(self, authorization_values: tuple[str, ...]) -> bool:
        if len(authorization_values) != 1:
            return False
        authorization = authorization_values[0]
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return False
        candidate = authorization.removeprefix("Bearer ")
        if (
            not candidate
            or not candidate.isascii()
            or any(character.isspace() for character in candidate)
        ):
            return False
        return hmac.compare_digest(candidate.encode("ascii"), self._expected_token)

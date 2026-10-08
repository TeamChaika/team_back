"""Credential input validation and safe comparisons for untrusted auth headers."""

import hmac

from pydantic import SecretStr, field_validator

from .models import Model


def csrf_matches(candidate: str, expected: str) -> bool:
    """CSRF tokens are ASCII; malformed header text must fail closed, not raise."""
    try:
        return hmac.compare_digest(candidate.encode("ascii"), expected.encode("ascii"))
    except UnicodeEncodeError:
        return False


class CredentialsModel(Model):
    @field_validator("*", mode="after")
    @classmethod
    def valid_unicode(cls, value: str | SecretStr) -> str | SecretStr:
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(text, str):
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("Недопустимые символы в учётных данных") from None
        return value

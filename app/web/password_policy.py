"""Password onboarding is enforced from the current database profile, not a JWT claim."""

from fastapi import HTTPException


def require_personal_password(user: dict) -> None:
    if user.get("password_change_required"):
        raise HTTPException(
            403,
            {
                "code": "password_change_required",
                "message": "Чтобы продолжить, смените временный пароль.",
            },
        )

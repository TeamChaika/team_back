"""Безопасные ошибки интеграции без URL, паролей, токенов и тела ответа."""


class IikoError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        *,
        outcome_unknown: bool = False,
        upstream_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        self.upstream_status_code = upstream_status_code

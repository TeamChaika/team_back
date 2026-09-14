"""Маскирование параметров авторизации в штатном журнале HTTPX."""

import logging
import re


class IikoCredentialsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = re.sub(
            r"([?&](?:login|pass|key)=)[^&\s\"']+",
            r"\1[REDACTED]",
            record.getMessage(),
            flags=re.IGNORECASE,
        )
        record.args = ()
        return True


def configure_http_logging() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, IikoCredentialsFilter) for item in logger.filters):
        logger.addFilter(IikoCredentialsFilter())

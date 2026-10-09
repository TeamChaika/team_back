"""Stable tenant bot identity; token rotation does not change its namespace."""

import hashlib
import re

from app.documents.context import runtime_of


def bot_namespace(service):
    if runtime_of(getattr(service, "database", None)).mode != "tenant":
        return ""
    token = service.settings.bot_token.get_secret_value()
    prefix, separator, secret = token.partition(":")
    if not separator or not secret or not re.fullmatch(r"[1-9][0-9]{0,19}", prefix):
        raise ValueError("Invalid tenant Telegram bot identity")
    return prefix


def credential_digest(token, namespace=""):
    material = f"telegram:{namespace}:{token}" if namespace else token
    return hashlib.sha256(material.encode()).hexdigest()

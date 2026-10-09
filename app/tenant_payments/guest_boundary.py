"""Separate payment origins have no access to authenticated portal endpoints."""

import re

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_GUEST = re.compile(r"/api/(?:guest-deposits/" + _UUID + r"|guest-links/[A-Za-z0-9_-]{32})")


def guest_method_allowed(path, method):
    if method == "GET":
        return bool(_GUEST.fullmatch(path))
    if method == "POST":
        base, _, action = path.rpartition("/")
        return action in {"prepare", "reconcile"} and bool(_GUEST.fullmatch(base))
    return False

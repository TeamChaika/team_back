"""Immutable source selected only after checking tenant membership and capabilities."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DashboardSource:
    company_id: str
    company_name: str
    user_id: str
    user_name: str
    version: int
    fingerprint: str
    url: str = field(repr=False)
    login: str = field(repr=False)
    password: str = field(repr=False)

    @property
    def cache_key(self):
        return self.company_id, self.version, self.fingerprint

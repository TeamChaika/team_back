"""Health handle for a separately supervised own-company collector socket."""

import httpx


class ExternalCollector:
    def __init__(self, runtime):
        self.runtime = runtime

    def poll(self):
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(self.runtime.collector_socket)),
                base_url="http://runtime",
                timeout=3,
                trust_env=False,
            ) as client:
                response = client.get("/_runtime/health")
                body = response.json()
                if (
                    response.status_code == 200
                    and body.get("company_id") == str(self.runtime.company_id)
                    and body.get("configuration_version") == self.runtime.configuration_version
                ):
                    return None
        except (httpx.HTTPError, ValueError):
            pass
        return 1

    def terminate(self):
        pass  # This handle never owns or terminates another service's process.

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

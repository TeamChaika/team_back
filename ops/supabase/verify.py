"""Run on the Supabase host; report checks without printing credentials or bodies."""

import argparse
import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = urlsplit(req.full_url), urlsplit(newurl)
        if (before.scheme, before.netloc) != (after.scheme, after.netloc):
            raise ValueError("Refusing to forward credentials to another origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verify(directory: Path, base_url: str) -> dict:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    ):
        raise ValueError("Use HTTPS, or HTTP on localhost through an SSH tunnel")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Credentials must not be passed in the URL")
    env = dict(
        line.split("=", 1)
        for line in (directory / ".env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    opener = build_opener(ProxyHandler({}), SameOriginRedirect())
    checks = []

    def request(name: str, path: str, expected: int, headers: dict | None = None):
        try:
            with opener.open(
                Request(base_url.rstrip("/") + path, headers=headers or {}), timeout=20
            ) as response:
                code, body = response.status, response.read(4 * 1024 * 1024)
        except HTTPError as error:
            code, body = error.code, b""
        checks.append({"check": name, "status": code, "passed": code == expected})
        return body

    request("REST without key rejected", "/rest/v1/", 401)
    request("REST invalid key rejected", "/rest/v1/", 401, {"apikey": "invalid-key"})
    for key_name, expected in [
        ("SUPABASE_PUBLISHABLE_KEY", 403),
        ("SUPABASE_SECRET_KEY", 200),
        ("SERVICE_ROLE_KEY", 200),
    ]:
        request(key_name + " REST root", "/rest/v1/", expected, {"apikey": env[key_name]})
    public_headers = {"apikey": env["SUPABASE_PUBLISHABLE_KEY"]}
    request("Auth health", "/auth/v1/health", 200, public_headers)
    settings = request("Auth settings", "/auth/v1/settings", 200, public_headers)
    if settings:
        value = json.loads(settings)
        checks.append({"check": "Signup disabled", "passed": value.get("disable_signup") is True})
    jwks = request("Public JWKS", "/auth/v1/.well-known/jwks.json", 200)
    if jwks:
        keys = json.loads(jwks).get("keys", [])
        checks.append(
            {
                "check": "JWKS has public EC keys only",
                "passed": bool(keys)
                and all(key.get("kty") == "EC" and not {"d", "k"} & key.keys() for key in keys),
            }
        )
    request("Storage buckets", "/storage/v1/bucket", 200, {"apikey": env["SUPABASE_SECRET_KEY"]})
    request("Studio requires password", "/", 401)
    credentials = (env["DASHBOARD_USERNAME"] + ":" + env["DASHBOARD_PASSWORD"]).encode()
    request(
        "Studio authenticated",
        "/",
        200,
        {"Authorization": "Basic " + base64.b64encode(credentials).decode()},
    )
    sql = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            "SELECT current_database(), current_setting('server_version');",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    checks.append({"check": "PostgreSQL query", "passed": sql.returncode == 0})
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/opt/supabase"))
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    try:
        report = verify(args.directory, args.url)
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)

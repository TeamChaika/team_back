"""Read-only MCP handshake and SQL checks through the local SSH tunnel."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def verify(url: str, cycles: int) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("Expected a loopback SSH tunnel URL")
    if not 1 <= cycles <= 10:
        raise ValueError("Choose between 1 and 10 verification cycles")
    checks = []
    for cycle in range(1, cycles + 1):
        with httpx.Client(timeout=30, trust_env=False) as client:
            # Studio may close a reused connection after the initialized notification.
            # Match the Codex setting; no requests, including SQL, are retried.
            headers = {"Accept": "application/json, text/event-stream", "Connection": "close"}

            def rpc(
                method: str,
                params: dict | None,
                request_id: int | None,
                *,
                headers: dict = headers,
                cycle: int = cycle,
            ):
                payload = {"jsonrpc": "2.0", "method": method}
                if params is not None:
                    payload["params"] = params
                if request_id is not None:
                    payload["id"] = request_id
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                checks.append({"cycle": cycle, "method": method, "status": response.status_code})
                if request_id is None:
                    return None
                if "mcp-session-id" in response.headers:
                    headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
                if "text/event-stream" in response.headers.get("content-type", ""):
                    data = json.loads(
                        next(
                            line[6:]
                            for line in response.text.splitlines()
                            if line.startswith("data: ")
                        )
                    )
                else:
                    data = response.json()
                if "error" in data or data.get("result", {}).get("isError", False):
                    raise ValueError("MCP returned an error")
                return data["result"]

            initialization = rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "chaika-codex-check", "version": "1.0"},
                },
                1,
            )
            headers["MCP-Protocol-Version"] = initialization["protocolVersion"]
            rpc("notifications/initialized", None, None)
            tool_names = [tool["name"] for tool in rpc("tools/list", None, 2)["tools"]]
            if "execute_sql" not in tool_names:
                raise ValueError("Database tools are missing")
            result = rpc(
                "tools/call",
                {
                    "name": "execute_sql",
                    "arguments": {
                        "query": "SELECT current_database() AS database_name, "
                        "current_setting('server_version') AS server_version;"
                    },
                },
                3,
            )
            text = json.dumps(result)
            if "database_name" not in text or "server_version" not in text:
                raise ValueError("Expected SQL results are missing")
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "url": url,
        "server": initialization["serverInfo"],
        "tool_names": tool_names,
        "tool_count": len(tool_names),
        "verification": checks,
        "authentication": "SSH public key; MCP OAuth unsupported",
        "http_headers": {"Connection": "close"},
        "tunnel_user": "chaika-mcp",
        "tunnel_destination": "127.0.0.1:8300",
        "launch_agent": "team.chaika.supabase-mcp",
        "final_consecutive_cycles_passed": cycles,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.url, args.cycles)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"passed": True, "cycles": args.cycles, "tools": report["tool_count"]}))

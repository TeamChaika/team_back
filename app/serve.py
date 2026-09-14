"""Run the public portal on the container platform's assigned HTTP port."""

import os

import uvicorn


def main() -> None:
    raw_port = os.environ.get("PORT", "").strip() or "8000"
    try:
        port = int(raw_port)
    except ValueError:
        raise SystemExit("PORT must be an integer between 1 and 65535.") from None
    if not 1 <= port <= 65535:
        raise SystemExit("PORT must be an integer between 1 and 65535.")

    uvicorn.run("app.portal:app", host="0.0.0.0", port=port, workers=1, access_log=False)


if __name__ == "__main__":
    main()

"""Control-plane CLI; full tenant gateway is opt-in via --runtime-config."""

import argparse
import os
from pathlib import Path

from .postgres_backup import restore_postgres, snapshot_postgres


def main():
    parser = argparse.ArgumentParser(description="Standalone SaaS owner registry")
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--data-dir", required=True)
    bootstrap.add_argument("--username", required=True)
    bootstrap.add_argument("--display-name", default="Владелец")
    bootstrap.add_argument("--password-file", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--data-dir", required=True)
    serve.add_argument("--dist-dir", required=True)
    listen = serve.add_mutually_exclusive_group()
    listen.add_argument("--port", type=int, default=None)
    listen.add_argument("--uds")
    serve.add_argument("--mode", choices=["local", "production"], default="local")
    serve.add_argument("--origin")
    serve.add_argument(
        "--runtime-config",
        help="Private verifier/operator JSON; explicitly enable full gateway assembly",
    )
    backup = sub.add_parser("backup")
    backup.add_argument("--data-dir", required=True)
    backup.add_argument("--output", required=True)
    backup.add_argument("--clear-sessions", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument("--input", required=True)
    restore.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "bootstrap":
        parser.error("Use existing Supabase Auth identity and operator-created platform membership")
    elif args.command == "backup":
        dsn = os.environ.get("CHAIKA_SAAS_DATABASE_URL")
        if not dsn:
            parser.error("CHAIKA_SAAS_DATABASE_URL is required")
        snapshot_postgres(dsn, args.data_dir, args.output)
        print("Private PostgreSQL snapshot created; copied sessions omitted.")
    elif args.command == "restore":
        dsn = os.environ.get("CHAIKA_SAAS_RESTORE_DATABASE_URL")
        if not dsn:
            parser.error("CHAIKA_SAAS_RESTORE_DATABASE_URL operator connection is required")
        restore_postgres(args.input, dsn, args.data_dir)
        print("Private PostgreSQL snapshot restored; no Auth passwords or sessions restored.")
    else:
        import uvicorn

        from .deployment import create_deployment_app

        if args.mode == "production" and not args.origin:
            parser.error("Production requires an explicit --origin https://hostname")
        port = args.port if args.port is not None else 8210
        if args.uds:
            if args.mode != "production":
                parser.error("Unix socket is supported only in production")
            socket = Path(args.uds)
            if not socket.is_absolute():
                parser.error("Unix socket requires an absolute path")
            for private_root in (args.data_dir, args.dist_dir):
                if socket.resolve().is_relative_to(Path(private_root).resolve()):
                    parser.error("Unix socket must be outside data and static directories")
        origin = args.origin or f"http://127.0.0.1:{port}"
        app = create_deployment_app(
            args.data_dir,
            args.dist_dir,
            origin,
            args.mode,
            runtime_config=args.runtime_config,
        )
        if args.uds:
            uvicorn.run(app, uds=args.uds, proxy_headers=False, access_log=False)
        else:
            uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()

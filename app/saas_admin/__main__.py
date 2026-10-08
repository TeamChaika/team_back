"""Run only this module, never app.portal or the scheduler."""

import argparse
import os
from pathlib import Path

from .lifecycle import restore_registry, snapshot_registry
from .repository import Repository


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
        path = Path(args.password_file)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            parser.error("Password file must be a regular private file (0600)")
        password = path.read_text().rstrip("\r\n")
        Repository(args.data_dir).bootstrap(args.username, password, args.display_name)
        print("Owner initialized. No company records created.")
    elif args.command == "backup":
        snapshot_registry(args.data_dir, args.output, clear_sessions=args.clear_sessions)
        print("Validated private registry backup created.")
    elif args.command == "restore":
        restore_registry(args.input, args.data_dir)
        print("Validated private registry restored; all sessions revoked.")
    else:
        import uvicorn

        from .server import create_app

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
        app = create_app(args.data_dir, args.dist_dir, origin, args.mode)
        if args.uds:
            uvicorn.run(app, uds=args.uds, proxy_headers=False, access_log=False)
        else:
            uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()

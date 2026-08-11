from __future__ import annotations

import argparse

import uvicorn

from runner_manager.auth import AuthManager
from runner_manager.config import load_settings
from runner_manager.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="easyrunners")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="run the EasyRunners web service")
    admin = subparsers.add_parser("admin", help="administrator recovery commands")
    admin_subcommands = admin.add_subparsers(dest="admin_command", required=True)
    admin_subcommands.add_parser("reset-password", help="reset and print a one-time password")
    args = parser.parse_args()

    settings = load_settings()
    if args.command == "serve":
        settings.assert_production_safe()
        uvicorn.run(
            "runner_manager.main:create_app",
            factory=True,
            host=settings.manager_host,
            port=settings.manager_port,
            proxy_headers=True,
            forwarded_allow_ips=settings.trusted_proxy_cidrs,
        )
        return

    database = Database(settings.data_dir / "easyrunners.sqlite3", settings.history_limit)
    try:
        auth = AuthManager(settings, database)
        if args.admin_command == "reset-password":
            print(auth.reset_password())
    finally:
        database.close()

# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import getpass
import os
import re
import sys
from pathlib import Path

from sqlalchemy import URL, select
from sqlalchemy.exc import SQLAlchemyError

from .config import Config
from .db import engine_for, exclusive_writer, schema, users
from .feed import import_local
from .migration import migrate
from .security import ADMIN, ARCHIVE, HASHER, LOGIN_PATTERN, PASSWORD_MIN, SETTINGS


def legacy_url():
    password_file = os.getenv("LEGACY_PASSWORD_FILE")
    password = Path(password_file).read_text().rstrip("\r\n") if password_file else os.getenv("LEGACY_PASSWORD", "")
    return URL.create(
        "mysql+pymysql", host=os.environ["LEGACY_HOST"], port=int(os.getenv("LEGACY_PORT", "3306")),
        username=os.environ["LEGACY_USER"], password=password, database=os.environ["LEGACY_DATABASE"],
        query={"charset": "utf8mb4"},
    )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init-db")
    admin = sub.add_parser("create-admin")
    admin.add_argument("login")
    legacy = sub.add_parser("migrate-legacy")
    legacy.add_argument("--prefix", default="bo_")
    legacy.add_argument("--user-prefix")
    legacy.add_argument("--check", action="store_true")
    ingest = sub.add_parser("import-file")
    ingest.add_argument("kind", choices=["strikes", "stations"])
    ingest.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        config = Config.from_env()
        engine = engine_for(config.database)
        if args.action == "init-db":
            with exclusive_writer(engine) as conn:
                schema.create_all(conn)
                conn.commit()
        elif args.action == "create-admin":
            if not re.fullmatch(LOGIN_PATTERN, args.login):
                raise ValueError("Invalid login")
            password = getpass.getpass("New admin password: ")
            if len(password) < PASSWORD_MIN or len(password) > 256:
                raise ValueError("Password must have 12 to 256 characters")
            if getpass.getpass("Repeat password: ") != password:
                raise ValueError("Passwords do not match")
            with exclusive_writer(engine) as conn, conn.begin():
                if conn.scalar(select(users.c.id).where(users.c.login == args.login)):
                    raise ValueError("Login already exists")
                conn.execute(users.insert().values(
                    login=args.login, password=HASHER.hash(password), level=ADMIN | SETTINGS | ARCHIVE,
                ))
        elif args.action == "migrate-legacy":
            counts = migrate(engine_for(legacy_url()), engine, args.prefix, args.user_prefix, args.check)
            for name, count in counts.items():
                print(f"{name}: {count}")
        else:
            print(import_local(engine, args.kind, args.path, config.own_station))
    except SQLAlchemyError as exc:
        print(f"Database operation failed ({type(exc).__name__}). Check schema, database grants and source integrity.",
              file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

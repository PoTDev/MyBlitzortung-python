# SPDX-License-Identifier: GPL-3.0-or-later
import re
from datetime import datetime

from sqlalchemy import MetaData, Table, func, inspect, select

from .db import exclusive_writer, links, raw, samples, stations, strikes, users

SOURCES = (
    ("stations", stations, {"id", "bo_station_id", "lat", "lon"}),
    ("strikes", strikes, {"id", "time", "time_ns", "lat", "lon"}),
    ("raw", raw, {"id", "data"}),
    ("stations_strikes", links, {"station_id", "strike_id"}),
    ("stations_stat", samples, {"station_id", "time", "signalsh", "strikesh"}),
    ("user", users, {"id", "login", "password", "level", "mail"}),
)


def legacy_tables(source, prefix: str, user_prefix: str):
    for value in (prefix, user_prefix):
        if not re.fullmatch(r"[A-Za-z0-9_]{0,40}", value):
            raise ValueError("Invalid legacy prefix")
    available = set(inspect(source).get_table_names())
    metadata = MetaData()
    for suffix, target, required in SOURCES:
        name = (user_prefix if target is users else prefix) + suffix
        if name not in available:
            raise ValueError(f"Missing legacy table {name}")
        table = Table(name, metadata, autoload_with=source)
        missing = required - set(table.c.keys())
        if missing:
            raise ValueError(f"{name} missing columns: {', '.join(sorted(missing))}")
        yield table, target


def migrate(source_engine, target_engine, prefix="bo_", user_prefix=None, check_only=False):
    if source_engine.url == target_engine.url or (
        source_engine.dialect.name in {"mysql", "mariadb"}
        and (source_engine.url.host, source_engine.url.port or 3306, source_engine.url.database)
        == (target_engine.url.host, target_engine.url.port or 3306, target_engine.url.database)
    ):
        raise ValueError("Use a separate target database")
    with source_engine.connect() as source:
        plan = list(legacy_tables(source, prefix, prefix if user_prefix is None else user_prefix))
        queries = []
        for table, target in plan:
            columns = [table.c[name] for name in target.c.keys() if name in table.c and target is not samples]
            if target is samples:
                columns = [table.c[name] for name in ("station_id", "time", "signalsh", "strikesh")]
            query = select(*columns)
            if target is users:
                query = query.where(table.c.id > 1)
            queries.append((table, target, query))
        counts = {
            table.name: source.scalar(select(func.count()).select_from(query.subquery()))
            for table, _, query in queries
        }
        if check_only:
            return counts

        with exclusive_writer(target_engine) as conn, conn.begin():
            for _, target, _ in queries:
                if conn.scalar(select(target).limit(1)) is not None:
                    raise ValueError(f"Target table {target.name} must be empty")

            for table, target, query in queries:
                with source.execution_options(yield_per=500).execute(query) as result:
                    for rows in result.mappings().partitions(500):
                        batch = [dict(row) for row in rows]
                        for row in batch:
                            # MySQL can contain nullable zero dates in old deployments.
                            for key in ("time", "last_time", "first_seen", "changed"):
                                value = row.get(key)
                                if isinstance(value, str):
                                    if value.startswith("0000-00-00") and target.c[key].nullable:
                                        row[key] = None
                                    else:
                                        row[key] = datetime.fromisoformat(value)
                        conn.execute(target.insert(), batch)
                actual = conn.scalar(select(func.count()).select_from(target))
                if actual != counts[table.name]:
                    raise ValueError(f"Count mismatch for {table.name}: {actual} vs {counts[table.name]}")
        return counts

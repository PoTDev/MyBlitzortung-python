# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Column, DateTime, Float, Index, Integer, LargeBinary, MetaData,
    SmallInteger, String, Table, Text, UniqueConstraint, create_engine, event, text,
)
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

schema = MetaData()
identifier = BigInteger().with_variant(Integer, "sqlite")

strikes = Table(
    "strikes", schema,
    Column("id", identifier, primary_key=True, autoincrement=True),
    Column("time", DateTime, nullable=False),
    Column("time_ns", Integer, nullable=False),
    Column("lat", Float(precision=53), nullable=False),
    Column("lon", Float(precision=53), nullable=False),
    Column("alt", Float(precision=53)),
    Column("distance", Integer),
    Column("bearing", Float(precision=53)),
    Column("current", Float(precision=53)),
    Column("type", SmallInteger),
    Column("deviation", Integer),
    Column("stations", Integer),
    Column("stations_calc", Integer),
    Column("part", SmallInteger),
    Column("part_pos", Integer),
    Column("raw_id", identifier, index=True),
    Column("status", SmallInteger),
    Index("ix_strikes_time_ns_id", "time", "time_ns", "id"),
)
stations = Table(
    "stations", schema,
    Column("id", identifier, primary_key=True, autoincrement=True),
    Column("bo_station_id", Integer, nullable=False, index=True),
    Column("bo_user_id", Integer),
    Column("city", String(100)),
    Column("country", String(100)),
    Column("comment", String(256)),
    Column("lat", Float(precision=53)),
    Column("lon", Float(precision=53)),
    Column("alt", Float(precision=53)),
    Column("distance", Integer),
    Column("last_time", DateTime),
    Column("last_time_ns", Integer),
    Column("status", String(12)),
    Column("controller_pcb", String(50)),
    Column("amp_pcbs", String(100)),
    Column("amp_gains", String(100)),
    Column("amp_antennas", String(100)),
    Column("amp_firmwares", String(100)),
    Column("firmware", String(100)),
    Column("url", String(256)),
    Column("show_mybo", String(12)),
    Column("first_seen", DateTime),
    Column("changed", DateTime),
)
links = Table(
    "station_strikes", schema,
    Column("strike_id", identifier, primary_key=True),
    Column("station_id", identifier, primary_key=True),
    Index("ix_station_strikes_station_strike", "station_id", "strike_id"),
)
samples = Table(
    "station_samples", schema,
    Column("id", identifier, primary_key=True, autoincrement=True),
    Column("station_id", identifier, nullable=False),
    Column("time", DateTime, nullable=False),
    Column("signalsh", Integer, nullable=False),
    Column("strikesh", Integer, nullable=False),
    Index("ix_station_samples_station_time", "station_id", "time"),
)
raw = Table(
    "raw_signals", schema,
    Column("id", identifier, primary_key=True, autoincrement=True),
    Column("time", DateTime),
    Column("time_ns", Integer),
    Column("lat", Float(precision=53)),
    Column("lon", Float(precision=53)),
    Column("height", Integer),
    Column("strike_id", identifier, index=True),
    Column("channels", Integer),
    Column("ntime", Integer),
    *(Column(name, Integer) for name in (
        "amp1", "amp2", "amp1_max", "amp2_max", "freq1", "freq1_amp", "freq2", "freq2_amp",
    )),
    Column("data", LargeBinary().with_variant(MEDIUMBLOB(), "mysql", "mariadb")),
)
users = Table(
    "users", schema,
    Column("id", identifier, primary_key=True, autoincrement=True),
    Column("login", String(50), nullable=False),
    Column("password", String(255), nullable=False),
    Column("level", Integer, nullable=False),
    Column("mail", String(254)),
    UniqueConstraint("login"),
)
sessions = Table(
    "sessions", schema,
    Column("token", String(64), primary_key=True),
    Column("user_id", identifier, nullable=False, index=True),
    Column("expires", DateTime, nullable=False, index=True),
)
settings = Table(
    "settings", schema,
    Column("name", String(50), primary_key=True),
    Column("value", Text, nullable=False),
)
feed_state = Table(
    "feed_state", schema,
    Column("name", String(160), primary_key=True),
    Column("etag", String(250)),
    Column("modified", String(60)),
    Column("checked", DateTime, nullable=False, index=True),
)

for table in schema.tables.values():
    table.dialect_options["mysql"]["engine"] = "InnoDB"
    table.dialect_options["mysql"]["charset"] = "utf8mb4"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_label(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def engine_for(url) -> Engine:
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if str(url) == "sqlite://":
        options.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
    engine = create_engine(url, **options)
    if engine.dialect.name in {"mysql", "mariadb"}:
        @event.listens_for(engine, "connect")
        def set_utc(connection, _record):
            with connection.cursor() as cursor:
                cursor.execute("SET time_zone = '+00:00'")
    return engine


@contextmanager
def exclusive_writer(engine: Engine):
    # GET_LOCK belongs to a connection. All writes use that same connection so
    # a dropped connection cannot leave an unlocked writer running.
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            yield conn
            return
        name = "mybo_" + hashlib.sha256(engine.url.database.encode()).hexdigest()[:40]
        lock = text("SELECT GET_LOCK(:name, 0)")
        if conn.scalar(lock, {"name": name}) != 1:
            raise RuntimeError("Another worker or migration holds the database writer lock")
        conn.commit()
        try:
            yield conn
        finally:
            # An invalidated connection has already released its lock.
            if not conn.invalidated:
                conn.rollback()
                conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
                conn.commit()

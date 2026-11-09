# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, func, inspect, select, text

from mybo.db import engine_for, links, raw, samples, schema, stations, strikes, users
from mybo.migration import migrate

STAMP = datetime(2023, 1, 2, 3, 4, 5)


def legacy_fixture(engine, mysql=False):
    suffixes = {
        "stations": "bo_",
        "strikes": "bo_",
        "raw": "bo_",
        "stations_strikes": "bo_",
        "stations_stat": "bo_",
        "user": "auth_",
    }
    if mysql:
        script = (Path(__file__).parents[1] / "legacy/install/db.sql").read_text()
        with engine.begin() as conn:
            for statement in script.split(";"):
                if statement.strip():
                    prefix = "auth_" if "{BO_DB_PREF}user" in statement else "bo_"
                    conn.exec_driver_sql(statement.replace("{BO_DB_PREF}", prefix))
    else:
        metadata = MetaData()
        for suffix, target in zip(suffixes, (stations, strikes, raw, links, samples, users), strict=True):
            target.to_metadata(metadata, name=suffixes[suffix] + suffix)
        metadata.create_all(engine)

    reflected = MetaData()
    reflected.reflect(engine)
    def seed(conn, name, **data):
        table = reflected.tables[suffixes[name] + name]
        if mysql:
            for column in table.columns:
                if column.name not in data and not (column.default or column.server_default or column.nullable):
                    data[column.name] = "" if column.type.python_type is str else 0
        conn.execute(table.insert().values(**data))

    with engine.begin() as conn:
        seed(conn, "stations", id=9, bo_station_id=1234, lat=55.0, lon=36.0)
        seed(conn, "strikes", id=7, time=STAMP, time_ns=987, lat=50.5, lon=30.4, raw_id=2)
        seed(conn, "stations_strikes", strike_id=7, station_id=9)
        seed(conn, "stations_stat", station_id=9, time=STAMP, signalsh=20, strikesh=5)
        seed(conn, "raw", id=2, strike_id=7, time=STAMP, channels=2, data=b"abcd")
        seed(conn, "user", id=1, login="", password="", level=1, mail="")
        seed(conn, "user", id=14, login="member", password=hashlib.md5(b"test").hexdigest(), level=128, mail="x@y.invalid")


def validate_migration(source, target):
    expected = {"bo_stations": 1, "bo_strikes": 1, "bo_raw": 1, "bo_stations_strikes": 1,
                "bo_stations_stat": 1, "auth_user": 1}
    assert migrate(source, target, user_prefix="auth_", check_only=True) == expected
    assert migrate(source, target, user_prefix="auth_") == expected
    with target.connect() as conn:
        assert conn.scalar(select(stations.c.bo_station_id).where(stations.c.id == 9)) == 1234
        assert conn.scalar(select(strikes.c.time).where(strikes.c.id == 7)) == STAMP
        assert conn.execute(select(links)).one() == (7, 9)
        assert conn.scalar(select(samples.c.signalsh)) == 20
        assert conn.scalar(select(raw.c.data)) == b"abcd"
        assert conn.scalar(select(users.c.id)) == 14
    with source.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM auth_user")) == 2
    with pytest.raises(ValueError, match="must be empty"):
        migrate(source, target, user_prefix="auth_")


def test_migrate_legacy_preserves_history_and_refuses_merge(tmp_path, db):
    source = engine_for(f"sqlite:///{tmp_path / 'legacy.db'}")
    legacy_fixture(source)
    validate_migration(source, db)
    source.dispose()


def test_source_preflight_fails_before_target_write(tmp_path, db):
    source = engine_for(f"sqlite:///{tmp_path / 'legacy.db'}")
    with source.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE bo_stations (id INT)")
    with pytest.raises(ValueError, match="missing columns"):
        migrate(source, db)
    with db.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(stations)) == 0
    source.dispose()


def test_mariadb_migration_and_application():
    import os

    import httpx
    from fastapi.testclient import TestClient
    from sqlalchemy.engine import make_url

    from mybo.api import create_app
    from mybo.config import Config
    from mybo.db import exclusive_writer, utcnow
    from mybo.feed import Feed

    urls = [os.getenv("MYBO_TEST_LEGACY_URL"), os.getenv("MYBO_TEST_DB_URL")]
    if not all(urls):
        pytest.skip("Set MYBO_TEST_LEGACY_URL and MYBO_TEST_DB_URL for the MariaDB integration test")
    parsed = [make_url(url) for url in urls]
    assert all(url.get_backend_name() in {"mysql", "mariadb"} and url.database.endswith("_test") for url in parsed)
    assert parsed[0].database != parsed[1].database
    source, target = map(engine_for, parsed)
    assert not inspect(source).get_table_names()
    assert not inspect(target).get_table_names()
    try:
        legacy_fixture(source, mysql=True)
        schema.create_all(target)
        validate_migration(source, target)
        now = utcnow()
        data = now.strftime("%Y-%m-%d %H:%M:%S") + ".000000001 pos;50.5;30.4;0 str;3 typ;0 dev;40 sta;2;2;1234,1235\n"
        config = Config(parsed[1], origin="https://testserver")
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=data))) as http:
            with exclusive_writer(target) as conn, conn.begin():
                assert Feed(config, http).fetch(conn, "strike:test", "Strokes/test.log", now)
        client = TestClient(create_app(config, target), base_url=config.origin)
        headers = {"Origin": config.origin}
        assert client.post("/api/login", headers=headers, json={"login": "member", "password": "test"}).status_code == 200
        assert len(client.get("/api/strikes/live").json()["items"]) == 1
        assert sum(point["count"] for point in client.get("/api/statistics").json()["strikes"]) == 1
        assert client.get("/api/strikes/7/signal").status_code == 200
    finally:
        schema.drop_all(target)
        reflected = MetaData()
        reflected.reflect(source)
        reflected.drop_all(source)
        source.dispose()
        target.dispose()


def test_migration_failure_rolls_back_all_target_tables(tmp_path, db):
    source = engine_for(f"sqlite:///{tmp_path / 'legacy.db'}")
    legacy_fixture(source)
    with source.begin() as conn:
        conn.exec_driver_sql("UPDATE bo_strikes SET time = 'invalid'")
    with pytest.raises(ValueError):
        migrate(source, db, user_prefix="auth_")
    with db.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(stations)) == 0
        assert conn.scalar(select(func.count()).select_from(strikes)) == 0
    with source.begin() as conn:
        conn.exec_driver_sql("UPDATE bo_strikes SET time = '2023-01-02 03:04:05'")
    assert migrate(source, db, user_prefix="auth_")["bo_strikes"] == 1
    source.dispose()

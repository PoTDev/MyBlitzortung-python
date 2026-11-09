# SPDX-License-Identifier: GPL-3.0-or-later
import gzip
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from mybo.config import Config
from mybo.db import exclusive_writer, feed_state, links, samples, stations, strikes
from mybo.feed import Feed, import_local, parse_station, parse_strike, state, store_stations

TIME = datetime(2026, 7, 1, 12, 1, 30)
STATION = 'station;3342 user;41 city;"Test City" country;"GB" pos;51.5;-0.1;42 status;30 signals;5,60;7,65 last_signal;"2026-07-01 12:01:27"'
STROKE = "2026-07-01 12:01:21.123456789 pos;50.5123;-1.31;91 str;-21.4 typ;0 dev;200 sta;15;12;3342,1785,3342"


def test_parse_legacy_formats():
    row, receivers = parse_strike(STROKE, 3342)
    assert (row["current"], row["deviation"], row["time_ns"], row["part_pos"]) == (-21.4, 60, 123456789, 1)
    assert receivers == {3342, 1785}
    row, _ = parse_strike(STROKE.replace(" typ;0", ""), 3342)
    assert row["type"] is None
    row, rate = parse_station(STATION)
    assert (row["city"], row["last_time"], rate) == ("Test City", datetime(2026, 7, 1, 12, 1, 27), 65)
    _, rate = parse_station(STATION.replace("signals;5,60;7,65", 'signals;"5,60;7,65"'))
    assert rate == 65


def test_duplicate_station_snapshot_rows_are_merged(db):
    earlier = parse_station(STATION)
    later = parse_station(STATION.replace("12:01:27", "12:01:29").replace("7,65", "7,70"))

    with db.begin() as conn:
        assert store_stations(conn, [earlier, later], TIME) == 1

    with db.connect() as conn:
        station = conn.execute(select(stations.c.last_time)).scalar_one()
        assert station == datetime(2026, 7, 1, 12, 1, 29)
        assert conn.scalar(select(func.count()).select_from(samples).where(samples.c.station_id > 0)) == 1
        assert conn.scalar(select(samples.c.signalsh).where(samples.c.station_id > 0)) == 70


def test_station_and_strike_file_retry_is_idempotent(db, tmp_path):
    station_file = tmp_path / "stations.gz"
    stroke_file = tmp_path / "strike.log"
    station_file.write_bytes(gzip.compress((STATION + "\n").encode()))
    stroke_file.write_text(STROKE + "\n")
    assert import_local(db, "stations", station_file, 3342) == 1
    assert import_local(db, "strikes", stroke_file, 3342) == 1
    assert import_local(db, "stations", station_file, 3342) == 0
    stroke_file.write_text(STROKE + "\n" + STROKE.replace("123456789", "123456780") + "\n")
    assert import_local(db, "strikes", stroke_file, 3342) == 1
    with db.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(stations)) == 2
        assert conn.scalar(select(func.count()).select_from(links)) == 4
        assert conn.scalar(select(func.count()).select_from(samples)) == 2
        assert conn.scalar(select(strikes.c.distance).limit(1)) > 0


def test_malformed_file_rolls_back_earlier_records(db, tmp_path):
    path = tmp_path / "bad.log"
    path.write_text(STROKE + "\ninvalid\n")
    with pytest.raises(ValueError, match="line 2"):
        import_local(db, "strikes", path, 3342)
    with db.connect() as conn:
        for table in (strikes, links, stations, feed_state):
            assert conn.scalar(select(func.count()).select_from(table)) == 0


def test_authorized_conditional_fetch_and_failure(db):
    responses = iter([
        httpx.Response(200, content=gzip.compress((STATION + "\n").encode()), headers={"ETag": "snapshot"}),
        httpx.Response(304), httpx.Response(503),
    ])
    requests = []

    def mock(request):
        requests.append(request)
        return next(responses)

    config = Config("sqlite://", feed_base="https://feed.example.org/Protected", own_station=3342)
    with httpx.Client(transport=httpx.MockTransport(mock), auth=("operator", "token")) as client:
        feed = Feed(config, client)
        with exclusive_writer(db) as conn:
            for i in range(2):
                with conn.begin():
                    assert feed.fetch(conn, "stations", "stations.txt.gz", TIME + timedelta(minutes=i))
            with pytest.raises(httpx.HTTPStatusError), conn.begin():
                feed.fetch(conn, "stations", "stations.txt.gz", TIME + timedelta(minutes=3))
            assert state(conn, "stations")["checked"] == TIME
            assert conn.scalar(select(func.count()).select_from(samples)) == 2
    assert requests[0].url.path == "/Protected/stations.txt.gz"
    assert requests[0].headers["authorization"].startswith("Basic ")
    assert requests[1].headers["if-none-match"] == "snapshot"


def test_worker_revisits_open_windows_and_imports_late_record(db):
    current = "2026/07/01/12/00"
    requests = []

    def mock(request):
        requests.append(request)
        if not request.url.path.endswith(current + ".log"):
            return httpx.Response(404)
        if sum(r.url.path.endswith(current + ".log") for r in requests) == 1:
            return httpx.Response(200, text=STROKE + "\n2026-07-01 12:01", headers={"ETag": "partial"})
        assert "if-none-match" not in request.headers
        return httpx.Response(200, text=STROKE + "\n" + STROKE.replace("123456789", "123456788") + "\n")

    config = Config("sqlite://", lookback_hours=1)
    with httpx.Client(transport=httpx.MockTransport(mock)) as client, exclusive_writer(db) as conn:
        feed = Feed(config, client)
        feed.strike_cycle(conn, TIME)
        feed.strike_cycle(conn, TIME + timedelta(minutes=5))
        assert conn.scalar(select(func.count()).select_from(strikes)) == 2
        assert conn.scalar(select(func.count()).select_from(links)) == 4
        assert state(conn, "strike_cursor")["checked"] == datetime(2026, 7, 1, 11, 30)


def test_broken_history_does_not_block_live_feed(db):
    def mock(request):
        if request.url.path.endswith("/12/00.log"):
            return httpx.Response(200, text=STROKE + "\n")
        if request.url.path.endswith("/11/00.log"):
            return httpx.Response(200, text="malformed historic entry\n")
        return httpx.Response(404)

    config = Config("sqlite://", lookback_hours=1)
    with httpx.Client(transport=httpx.MockTransport(mock)) as client, exclusive_writer(db) as conn:
        with pytest.raises(ValueError):
            Feed(config, client).strike_cycle(conn, TIME)
        assert conn.scalar(select(func.count()).select_from(strikes)) == 1
        assert state(conn, "strike_cursor") is None

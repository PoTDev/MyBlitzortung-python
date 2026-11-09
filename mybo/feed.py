# SPDX-License-Identifier: GPL-3.0-or-later
import csv
import gzip
import hashlib
import io
import logging
import math
import re
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select, update

from .db import exclusive_writer, feed_state, links, samples, stations, strikes, utcnow

log = logging.getLogger(__name__)
MAX_PAYLOAD = 32 * 1024 * 1024
WINDOW = timedelta(minutes=10)
OVERLAP = 2 * WINDOW
FIELD = re.compile(r'(?:^|\s)([a-z_]+);((?:[^"\s]|"[^"]*")*)')
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\.(\d{9})(?=\s)")


def decoded(payload: bytes) -> str:
    if payload.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as data:
                payload = data.read(MAX_PAYLOAD + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError("Invalid gzip payload") from exc
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Import file exceeds the size limit")
    return payload.decode("utf-8-sig")


def fields(line: str):
    return {key: next(csv.reader([value], delimiter=";"), []) for key, value in FIELD.findall(line)}


def position(group: list[str]):
    lat, lon, alt = map(float, group)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and math.isfinite(alt) and abs(alt) < 100_000):
        raise ValueError("Invalid position")
    return {"lat": lat, "lon": lon, "alt": alt}


def bounded(value, high: int, low: int = 0) -> int:
    number = int(value)
    if not low <= number <= high:
        raise ValueError("Invalid integer")
    return number


def parse_strike(line: str, own_station: int):
    stamp = STAMP.match(line)
    if not stamp:
        raise ValueError("Invalid strike timestamp")
    groups = fields(line[stamp.end():])
    calc, used, receivers = groups["sta"]
    station_ids = [bounded(value, 65535, 1) for value in receivers.split(",") if value]
    current = float(groups["str"][0])
    deviation = float(groups["dev"][0]) * 0.299792458
    if not (abs(current) < 100_000_000 and 0 <= deviation < 2**31):
        raise ValueError("Invalid strike measurement")
    pos = position(groups["pos"])
    if pos["lat"] == pos["lon"] == 0:
        raise ValueError("Missing strike position")
    strike_type = bounded(groups["typ"][0], 32767, -32768) if groups.get("typ") else None
    own = station_ids.index(own_station) + 1 if own_station in station_ids else 0
    return {
        "time": datetime.fromisoformat(stamp[1]), "time_ns": int(stamp[2]), **pos,
        "current": current, "type": strike_type,
        "deviation": round(deviation), "stations": bounded(used, 65535),
        "stations_calc": bounded(calc, 65535), "status": 2, "part": int(bool(own)), "part_pos": own,
    }, set(station_ids)


STATION_FIELDS = {
    "city": ("city", 100), "country": ("country", 100), "board": ("controller_pcb", 50),
    "status": ("status", 12), "firmware": ("firmware", 100), "comments": ("comment", 256),
    "website": ("url", 256), "input_board": ("amp_pcbs", 100), "input_gain": ("amp_gains", 100),
    "input_antenna": ("amp_antennas", 100), "input_firmware": ("amp_firmwares", 100),
}


def parse_station(line: str):
    groups = fields(line)
    entry = {
        "bo_station_id": bounded(groups["station"][0], 65535, 1),
        "bo_user_id": bounded(groups["user"][0], 65535),
        **position(groups["pos"]),
    }
    for key, (column, limit) in STATION_FIELDS.items():
        if groups.get(key):
            entry[column] = groups[key][0][:limit]
    last = (groups.get("last_signal") or [""])[0]
    if last and not last.startswith("0000-00-00"):
        entry["last_time"] = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    signal = 0
    signal_groups = groups.get("signals", [])
    if len(signal_groups) == 1:
        signal_groups = signal_groups[0].split(";")
    if len(signal_groups) == 1:
        signal = bounded(signal_groups[0], 2**31 - 1)
    elif signal_groups:
        windows, values = signal_groups
        rates = dict(zip(windows.split(","), values.split(","), strict=True))
        signal = bounded(rates.get("60", "0"), 2**31 - 1)
    return entry, signal


def parsed(payload: bytes, kind: str, own_station: int, allow_partial=False):
    contents = decoded(payload)
    for number, line in enumerate(contents.splitlines(keepends=True), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if kind == "stations" and not re.search(r"(?:^|\s)station;", value):
            continue
        try:
            yield parse_strike(value, own_station) if kind == "strikes" else parse_station(value)
        except (KeyError, ValueError, IndexError, csv.Error) as exc:
            if kind == "strikes" and allow_partial and not line.endswith(("\r", "\n")):
                break
            raise ValueError(f"Malformed {kind} record on line {number}") from exc


def station_index(conn):
    rows = conn.execute(select(stations.c.bo_station_id, stations.c.id)
                        .where(stations.c.bo_station_id > 0).order_by(stations.c.id))
    return dict(rows.tuples().all())


def location(a_lat, a_lon, b_lat, b_lon):
    a, b, delta = map(math.radians, (a_lat, b_lat, b_lon - a_lon))
    h = math.sin((b - a) / 2)**2 + math.cos(a) * math.cos(b) * math.sin(delta / 2)**2
    distance = round(12_742_000 * math.asin(math.sqrt(min(1, h))))
    bearing = math.degrees(math.atan2(
        math.sin(delta) * math.cos(b), math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(delta),
    )) % 360
    return distance, bearing


def store_strikes(conn, entries, own_station: int, now: datetime):
    index = station_index(conn)
    home = conn.execute(select(stations.c.lat, stations.c.lon)
                        .where(stations.c.id == index.get(own_station))).first()
    count = 0
    for row, receivers in entries:
        strike_id = conn.scalar(select(strikes.c.id).where(
            strikes.c.time == row["time"], strikes.c.time_ns == row["time_ns"],
        ).order_by(strikes.c.id).limit(1))
        if strike_id is None:
            if home and home.lat is not None and home.lon is not None:
                row["distance"], row["bearing"] = location(home.lat, home.lon, row["lat"], row["lon"])
            strike_id = conn.execute(strikes.insert().values(**row)).inserted_primary_key[0]
            count += 1
        known = set(conn.scalars(select(links.c.station_id).where(links.c.strike_id == strike_id)))
        for number in sorted(receivers):
            if number not in index:
                index[number] = conn.execute(stations.insert().values(
                    bo_station_id=number, first_seen=now,
                )).inserted_primary_key[0]
        new = {index[number] for number in receivers} - known
        if new:
            conn.execute(links.insert(), [{"strike_id": strike_id, "station_id": sid} for sid in sorted(new)])
    return count


def store_stations(conn, entries, now: datetime):
    index = station_index(conn)
    seen = set()
    hour_start = now - timedelta(hours=1)
    counts = dict(conn.execute(
        select(links.c.station_id, func.count()).join(strikes, strikes.c.id == links.c.strike_id)
        .where(strikes.c.time > hour_start, strikes.c.time <= now)
        .group_by(links.c.station_id)
    ).tuples().all())
    total = 0
    merged = {}
    for row, rate in entries:
        number = row["bo_station_id"]
        if number in merged:
            prior, prior_rate = merged[number]
            if any(row[key] != prior[key] for key in row if key != "last_time"):
                raise ValueError("Station snapshot contains conflicting station records")
            last_time = row.get("last_time")
            prior_last_time = prior.get("last_time")
            if last_time and (prior_last_time is None or last_time > prior_last_time):
                prior["last_time"] = row["last_time"]
            merged[number] = (prior, max(rate, prior_rate))
        else:
            merged[number] = (row, rate)
    for number, (row, rate) in merged.items():
        seen.add(number)
        sid = index.get(number)
        if sid is None:
            sid = conn.execute(stations.insert().values(**row, first_seen=now, changed=now)).inserted_primary_key[0]
        else:
            conn.execute(update(stations).where(stations.c.id == sid).values(**row, changed=now))
        conn.execute(samples.insert().values(station_id=sid, time=now, signalsh=rate, strikesh=counts.get(sid, 0)))
        total += rate
    if not seen:
        raise ValueError("Station snapshot contains no stations")
    if total > 2**31 - 1:
        raise ValueError("Station snapshot exceeds network rate limit")
    network = conn.scalar(select(func.count()).select_from(strikes)
                          .where(strikes.c.time > hour_start, strikes.c.time <= now))
    conn.execute(samples.insert().values(station_id=0, time=now, signalsh=total, strikesh=network))
    return len(seen)


def state(conn, name):
    return conn.execute(select(feed_state).where(feed_state.c.name == name)).mappings().first()


def mark(conn, name, now, etag=None, modified=None):
    values = {"checked": now, "etag": etag, "modified": modified}
    if state(conn, name) is None:
        conn.execute(feed_state.insert().values(name=name, **values))
    else:
        conn.execute(update(feed_state).where(feed_state.c.name == name).values(**values))


def import_local(engine, kind: str, path: Path, own_station: int):
    with path.open("rb") as handle:
        payload = handle.read(MAX_PAYLOAD + 1)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Import file exceeds the size limit")
    now = utcnow().replace(microsecond=0)
    marker = f"file:{kind}:{hashlib.sha256(payload).hexdigest()}"
    with exclusive_writer(engine) as conn, conn.begin():
        if state(conn, marker):
            return 0
        entries = parsed(payload, kind, own_station)
        count = store_strikes(conn, entries, own_station, now) if kind == "strikes" else store_stations(conn, entries, now)
        mark(conn, marker, now)
        return count


class Feed:
    def __init__(self, config, client: httpx.Client):
        self.config = config
        self.client = client

    def fetch(self, conn, name, path, now, allow_missing=False, allow_partial=False):
        previous = state(conn, name)
        headers = {}
        if previous:
            if previous["etag"]:
                headers["If-None-Match"] = previous["etag"]
            if previous["modified"]:
                headers["If-Modified-Since"] = previous["modified"]
        with self.client.stream("GET", self.config.feed_base + "/" + path, headers=headers) as response:
            if response.status_code == 404 and allow_missing:
                return False
            if response.status_code == 304 and headers:
                return True
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Expected a complete feed response")
            payload = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                payload.extend(chunk)
                if len(payload) > MAX_PAYLOAD:
                    raise ValueError("Feed response exceeds the size limit")
            etag = response.headers.get("etag", "")[:250] or None
            modified = response.headers.get("last-modified", "")[:60] or None
        kind = "stations" if name == "stations" else "strikes"
        entries = parsed(bytes(payload), kind, self.config.own_station, allow_partial)
        if kind == "stations":
            store_stations(conn, entries, now)
        else:
            store_strikes(conn, entries, self.config.own_station, now)
        if allow_partial and not decoded(bytes(payload)).endswith(("\n", "\r")):
            etag = modified = None
        mark(conn, name, now, etag, modified)
        return True

    def strike_cycle(self, conn, now):
        current = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
        with conn.begin():
            cursor = state(conn, "strike_cursor")
        recent_start = current - OVERLAP
        start = cursor["checked"] + WINDOW if cursor else current - timedelta(hours=self.config.lookback_hours)
        closed_count = max(0, int((recent_start - start) / WINDOW))
        # Keep live updates moving even when a historic window is failing.
        recent = [current - offset * WINDOW for offset in range(3)]
        closed = [start + offset * WINDOW for offset in range(min(closed_count, 15))]
        if closed_count > len(closed):
            log.warning("Strike backlog: %d closed windows remain", closed_count)
        missing = 0
        for window in recent + closed:
            path = window.strftime("%Y/%m/%d/%H/%M")
            open_window = window >= recent_start
            try:
                with conn.begin():
                    found = self.fetch(conn, f"strike:{path}", f"Strokes/{path}.log", now,
                                       allow_missing=True, allow_partial=open_window)
                    if not open_window:
                        if not found:
                            missing += 1
                        mark(conn, "strike_cursor", window)
            except (httpx.HTTPError, ValueError, OSError):
                log.warning("Strike window %s UTC will be retried", path)
                raise
        if missing:
            log.warning("Feed returned 404 for %d closed windows", missing)
        with conn.begin():
            conn.execute(delete(feed_state).where(
                feed_state.c.name.like("strike:%"), feed_state.c.checked < now - timedelta(days=3),
            ))

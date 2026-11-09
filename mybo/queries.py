# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import binascii
import math
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Integer, and_, case, cast, func, or_, select

from .db import links, samples, stations, strikes, utc_label


class Area(BaseModel):
    south: Annotated[float, Field(ge=-90, le=90)] = -90
    north: Annotated[float, Field(ge=-90, le=90)] = 90
    west: Annotated[float, Field(ge=-180, le=180)] = -180
    east: Annotated[float, Field(ge=-180, le=180)] = 180

    @model_validator(mode="after")
    def validate_area(self):
        if self.south >= self.north or self.west == self.east:
            raise ValueError("Invalid area")
        return self

    def conditions(self):
        longitude = strikes.c.lon.between(self.west, self.east) if self.west < self.east else or_(
            strikes.c.lon >= self.west, strikes.c.lon <= self.east,
        )
        return strikes.c.lat.between(self.south, self.north), longitude


class ArchiveFilters(Area):
    start: datetime
    end: datetime
    station_id: Annotated[int | None, Field(ge=1, le=2**53 - 1)] = None

    @model_validator(mode="after")
    def validate_range(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Use timestamps with a UTC offset")
        if not timedelta(0) < self.end - self.start <= timedelta(days=31):
            raise ValueError("Select up to 31 days")
        return self

    def conditions(self):
        start, end = (value.astimezone(timezone.utc).replace(tzinfo=None) for value in (self.start, self.end))
        conditions = (*super().conditions(), strikes.c.time >= start, strikes.c.time < end)
        if self.station_id:
            conditions += (select(links.c.strike_id).where(
                links.c.strike_id == strikes.c.id, links.c.station_id == self.station_id,
            ).exists(),)
        return conditions


def pack_cursor(row) -> str:
    value = f'{row["time"].isoformat()}|{row["time_ns"]}|{row["id"]}'
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def unpack_cursor(cursor: str):
    try:
        value = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        time, ns, strike_id = value.split("|")
        stamp = datetime.fromisoformat(time)
        if stamp.tzinfo is not None or not 0 <= int(ns) < 10**9 or not 0 < int(strike_id) < 2**63:
            raise ValueError
        return stamp, int(ns), int(strike_id)
    except (ValueError, OverflowError, binascii.Error):
        raise HTTPException(422, "Invalid cursor") from None


def strike_page(conn, conditions, limit, cursor=None):
    ordering = (strikes.c.time, strikes.c.time_ns, strikes.c.id)
    if cursor:
        time, ns, strike_id = unpack_cursor(cursor)
        conditions += (or_(
            strikes.c.time < time,
            and_(strikes.c.time == time, strikes.c.time_ns < ns),
            and_(strikes.c.time == time, strikes.c.time_ns == ns, strikes.c.id < strike_id),
        ),)
    rows = conn.execute(select(
        strikes.c.id, strikes.c.time, strikes.c.time_ns, strikes.c.lat, strikes.c.lon,
        strikes.c.current, strikes.c.deviation, strikes.c.stations, strikes.c.part,
    ).where(*conditions).order_by(*(column.desc() for column in ordering)).limit(limit + 1)).mappings().all()
    next_page = pack_cursor(rows[limit - 1]) if len(rows) > limit else None
    return {"items": [{**row, "time": utc_label(row["time"])} for row in rows[:limit]], "next": next_page}


def density(conn, filters, conditions=()):
    lat = cast(func.floor((strikes.c.lat + 90) / 2), Integer).label("lat_cell")
    lon = cast(func.floor((strikes.c.lon + 180) / 2), Integer).label("lon_cell")
    rows = conn.execute(select(lat, lon, func.count().label("total"))
                        .where(*filters.conditions(), *conditions).group_by(lat, lon)
                        .order_by(func.count().desc(), lat, lon).limit(2000))
    return [{"lat": min(90, 2 * row.lat_cell - 89), "lon": min(180, 2 * row.lon_cell - 179),
             "total": row.total} for row in rows]


def station_page(conn, limit, after, private=False):
    # Keep physical locations approximate for members without station settings.
    lat = stations.c.lat if private else func.round(stations.c.lat, 1)
    lon = stations.c.lon if private else func.round(stations.c.lon, 1)
    rows = conn.execute(select(
        stations.c.id, stations.c.bo_station_id, stations.c.city, stations.c.country,
        lat.label("lat"), lon.label("lon"), stations.c.status, stations.c.last_time,
    ).where(stations.c.id > after, stations.c.bo_station_id > 0)
        .order_by(stations.c.id).limit(limit + 1)).mappings().all()
    return {
        "items": [{**row, "last_time": utc_label(row["last_time"])} for row in rows[:limit]],
        "next": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


def visible_radius(lat, lon, km):
    # Haversine without the inverse trig step. The preliminary latitude
    # predicate keeps the geographic condition cheap on narrow searches.
    radians = km / 6371
    latitude_band = math.degrees(radians)
    h = (
        func.power(func.sin(func.radians(strikes.c.lat - lat) / 2), 2)
        + math.cos(math.radians(lat)) * func.cos(func.radians(strikes.c.lat))
        * func.power(func.sin(func.radians(strikes.c.lon - lon) / 2), 2)
    )
    return (strikes.c.lat.between(lat - latitude_band, lat + latitude_band), h <= math.sin(radians / 2)**2)


def strike_series(conn, start, end, station_id=None, conditions=()):
    hourly = end - start <= timedelta(days=3)
    floor = start.replace(hour=start.hour if hourly else 0, minute=0, second=0, microsecond=0)
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    fmt = "%Y-%m-%d %H:00:00" if hourly else "%Y-%m-%d 00:00:00"
    bucket = (func.strftime(fmt, strikes.c.time) if conn.dialect.name == "sqlite"
              else func.date_format(strikes.c.time, fmt)).label("bucket")
    query = select(bucket, func.count()).where(strikes.c.time >= start, strikes.c.time < end, *conditions)
    if station_id is not None:
        query = query.join(links, links.c.strike_id == strikes.c.id).where(links.c.station_id == station_id)
    counts = dict(conn.execute(query.group_by(bucket)).tuples().all())
    series = []
    while floor < end:
        series.append({"time": utc_label(floor), "count": counts.get(floor.strftime(fmt), 0)})
        floor += step
    return series


def station_series(conn, station_id, start, end):
    rows = conn.execute(select(samples.c.time, samples.c.signalsh, samples.c.strikesh).where(
        samples.c.station_id == station_id, samples.c.time >= start, samples.c.time < end,
    ).order_by(samples.c.time.desc(), samples.c.id.desc()).limit(5000)).mappings().all()
    return [{**row, "time": utc_label(row["time"])}     for row in reversed(rows)]


def strike_directions(conn, station_id, start, end, conditions=()):
    station = conn.execute(select(stations.c.lat, stations.c.lon).where(stations.c.id == station_id)).first()
    if not station or station.lat is None or station.lon is None:
        return []

    delta_lon = func.radians(strikes.c.lon - station.lon)
    station_lat = math.radians(station.lat)
    strike_lat = func.radians(strikes.c.lat)
    east = func.sin(delta_lon) * func.cos(strike_lat)
    north = (
        math.cos(station_lat) * func.sin(strike_lat)
        - math.sin(station_lat) * func.cos(strike_lat) * func.cos(delta_lon)
    )
    bearing = (func.degrees(func.atan2(east, north)) + 360) % 360
    sector = cast(func.floor(((bearing + 11.25) % 360) / 22.5), Integer).label("sector")
    rows = conn.execute(select(sector, func.count().label("total"))
                .join(links, links.c.strike_id == strikes.c.id)
                .where(
                    links.c.station_id == station_id,
                    strikes.c.time >= start,
                    strikes.c.time < end,
                    strikes.c.lat.is_not(None),
                    strikes.c.lon.is_not(None),
                    *conditions,
                )
                .group_by(sector)).tuples().all()
    counts = dict(rows)
    return [{"sector": index, "count": counts.get(index, 0)} for index in range(16)]


def station_insights(conn, station_id, start, end, conditions=()):
    station = conn.execute(select(stations.c.lat, stations.c.lon).where(stations.c.id == station_id)).first()
    if not station:
        return None
    latest = conn.execute(select(samples.c.time, samples.c.signalsh, samples.c.strikesh).where(
        samples.c.station_id == station_id,
    ).order_by(samples.c.time.desc(), samples.c.id.desc()).limit(1)).mappings().first()
    detected = select(links.c.strike_id).where(
        links.c.strike_id == strikes.c.id, links.c.station_id == station_id,
    ).exists()
    period_conditions = (strikes.c.time >= start, strikes.c.time < end, *conditions)
    detected_period = conn.scalar(select(func.count()).select_from(strikes).where(
        *period_conditions, detected,
    )) or 0
    summary = {
        "last_sample": utc_label(latest["time"]) if latest else None,
        "signal_rate": latest["signalsh"] if latest else None,
        "strike_rate": latest["strikesh"] if latest else None,
        "efficiency": round(100 * latest["strikesh"] / latest["signalsh"], 1)
        if latest and latest["signalsh"] else None,
        "detected_period": detected_period,
    }
    if station.lat is None or station.lon is None:
        return {"summary": summary, "radii": [], "distances": [], "hours": []}

    delta_lon = func.radians(strikes.c.lon - station.lon)
    delta_lat = func.radians(strikes.c.lat - station.lat)
    h = (func.power(func.sin(delta_lat / 2), 2)
         + math.cos(math.radians(station.lat)) * func.cos(func.radians(strikes.c.lat))
         * func.power(func.sin(delta_lon / 2), 2))
    radius_values = (100, 250, 500, 1000)
    radius_thresholds = [math.sin(km / 6371 / 2) ** 2 for km in radius_values]
    radius_columns = []
    for threshold in radius_thresholds:
        radius_columns.extend((
            func.sum(case((h <= threshold, 1), else_=0)),
            func.sum(case((and_(h <= threshold, detected), 1), else_=0)),
        ))
    radius_row = conn.execute(select(*radius_columns).select_from(strikes).where(*period_conditions)).one()
    radii = []
    for index, km in enumerate(radius_values):
        total, own = radius_row[index * 2:index * 2 + 2]
        total, own = int(total or 0), int(own or 0)
        radii.append({"km": km, "total": total, "detected": own,
                      "percent": round(100 * own / total, 1) if total else None})

    bucket = case(*((h <= threshold, index) for index, threshold in enumerate(radius_thresholds)),
                  else_=len(radius_thresholds)).label("distance_bucket")
    distance_counts = dict(conn.execute(select(bucket, func.count()).select_from(strikes).where(
        *period_conditions, detected,
    ).group_by(bucket)).tuples().all())
    labels = ("0–100", "100–250", "250–500", "500–1000", "1000+")
    distances = [{"bucket": index, "label": label, "count": distance_counts.get(index, 0)}
                 for index, label in enumerate(labels)]

    strike_hour = (cast(func.strftime("%H", strikes.c.time), Integer) if conn.dialect.name == "sqlite"
                   else func.hour(strikes.c.time)).label("hour")
    strike_hours = dict(conn.execute(select(strike_hour, func.count()).select_from(strikes).where(
        *period_conditions, detected,
    ).group_by(strike_hour)).tuples().all())
    sample_hour = (cast(func.strftime("%H", samples.c.time), Integer) if conn.dialect.name == "sqlite"
                   else func.hour(samples.c.time)).label("hour")
    signal_hours = dict(conn.execute(select(sample_hour, func.avg(samples.c.signalsh)).where(
        samples.c.station_id == station_id, samples.c.time >= start, samples.c.time < end,
    ).group_by(sample_hour)).tuples().all())
    hours = [{"hour": hour, "count": strike_hours.get(hour, 0),
              "signals": round(float(signal_hours[hour]), 1) if hour in signal_hours else None}
             for hour in range(24)]
    return {"summary": summary, "radii": radii, "distances": distances, "hours": hours}

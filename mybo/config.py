# SPDX-License-Identifier: GPL-3.0-or-later
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import URL


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Config:
    database: URL | str
    origin: str = "http://127.0.0.1:8000"
    base_path: str = ""
    secure_cookie: bool = False
    superadmin_login: str = ""
    superadmin_password: str = ""
    import_enabled: bool = False
    feed_base: str = "https://data.blitzortung.org/Data_1/Protected"
    feed_user: str = ""
    feed_password: str = ""
    poll_seconds: int = 300
    station_seconds: int = 900
    lookback_hours: int = 2
    tile_url: str = ""
    tile_label_url: str = ""
    tile_attribution: str = ""
    own_station: int = 0
    view_radius_km: float = 0
    site_lat: float = 0
    site_lon: float = 0

    @classmethod
    def from_env(cls) -> "Config":
        origin = os.getenv("MYBO_ORIGIN", cls.origin).rstrip("/")
        path = os.getenv("MYBO_BASE_PATH", "").rstrip("/")
        feed = os.getenv("BO_FEED_BASE", cls.feed_base).rstrip("/")
        database_url = os.getenv("MYBO_DATABASE_URL")
        cfg = cls(
            database=database_url or URL.create(
                "mysql+pymysql", username=os.getenv("MARIADB_USER", "mybo"),
                password=os.getenv("MARIADB_PASSWORD", ""), host=os.getenv("MYBO_DB_HOST", "db"),
                port=int(os.getenv("MYBO_DB_PORT", "3306")),
                database=os.getenv("MARIADB_DATABASE", "mybo"), query={"charset": "utf8mb4"},
            ),
            origin=origin, base_path=path, secure_cookie=env_bool("MYBO_SECURE_COOKIE", False),
            superadmin_login=os.getenv("MYBO_SUPERADMIN_LOGIN", ""),
            superadmin_password=os.getenv("MYBO_SUPERADMIN_PASSWORD", ""),
            import_enabled=env_bool("BO_IMPORT_ENABLED"),
            feed_base=feed, feed_user=os.getenv("BO_FEED_USER", ""),
            feed_password=os.getenv("BO_FEED_PASSWORD", ""),
            poll_seconds=int(os.getenv("BO_POLL_SECONDS", "300")),
            station_seconds=int(os.getenv("BO_STATION_SECONDS", "900")),
            lookback_hours=int(os.getenv("BO_LOOKBACK_HOURS", "2")),
            tile_url=os.getenv("MYBO_TILE_URL", ""),
            tile_label_url=os.getenv("MYBO_TILE_LABEL_URL", ""),
            tile_attribution=os.getenv("MYBO_TILE_ATTRIBUTION", ""),
            own_station=int(os.getenv("BO_OWN_STATION", "0")),
            view_radius_km=float(os.getenv("MYBO_VIEW_RADIUS_KM", "0")),
            site_lat=float(os.getenv("MYBO_SITE_LAT", "0")),
            site_lon=float(os.getenv("MYBO_SITE_LON", "0")),
        )
        parsed = urlsplit(cfg.origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or (
            parsed.username or parsed.path or parsed.query or parsed.fragment
        ):
            raise ValueError("MYBO_ORIGIN must be the browser origin, without a path")
        if cfg.secure_cookie and parsed.scheme != "https":
            raise ValueError("Secure cookies require an HTTPS origin")
        if bool(cfg.superadmin_login) != bool(cfg.superadmin_password):
            raise ValueError("Both MYBO_SUPERADMIN_LOGIN and MYBO_SUPERADMIN_PASSWORD are required")
        if cfg.superadmin_login and (
            not cfg.superadmin_login.isascii()
            or not all(char.isalnum() or char in "_.@+-" for char in cfg.superadmin_login)
            or not 1 <= len(cfg.superadmin_login) <= 50
            or not 12 <= len(cfg.superadmin_password) <= 256
        ):
            raise ValueError("Invalid superadmin credentials")
        if path and (not path.startswith("/") or any(
            not part or part in {".", ".."} or not all(c.isascii() and (c.isalnum() or c in "-_") for c in part)
            for part in path[1:].split("/")
        )):
            raise ValueError("Invalid MYBO_BASE_PATH")
        if cfg.import_enabled:
            parsed = urlsplit(cfg.feed_base)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.query or parsed.fragment or not cfg.feed_user or not cfg.feed_password):
                raise ValueError("Enabled import requires an authorized HTTPS base URL and credentials")
        if cfg.poll_seconds < 60 or cfg.station_seconds < 300 or not 1 <= cfg.lookback_hours <= 24:
            raise ValueError("Invalid import intervals or lookback")
        if cfg.tile_url and (
            urlsplit(cfg.tile_url).scheme != "https" or not cfg.tile_attribution
            or not all(token in cfg.tile_url for token in ("{x}", "{y}", "{z}"))
        ):
            raise ValueError("Tile provider requires HTTPS, x/y/z and attribution")
        if cfg.tile_label_url and (
            not cfg.tile_url or urlsplit(cfg.tile_label_url).scheme != "https"
            or not all(token in cfg.tile_label_url for token in ("{x}", "{y}", "{z}"))
        ):
            raise ValueError("Label tile provider requires a base layer, HTTPS and x/y/z")
        if not 0 <= cfg.own_station <= 65535:
            raise ValueError("Invalid BO_OWN_STATION")
        if not (0 <= cfg.view_radius_km <= 20015 and -90 <= cfg.site_lat <= 90 and -180 <= cfg.site_lon <= 180):
            raise ValueError("Invalid geographic access limits")
        return cfg

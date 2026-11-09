# SPDX-License-Identifier: GPL-3.0-or-later
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .config import Config
from .db import engine_for, raw, settings, strikes, users, utc_label, utcnow
from .queries import (
    ArchiveFilters, Area, density, station_insights, station_page, station_series, strike_directions,
    strike_page, strike_series,
    visible_radius,
)
from .security import (
    ADMIN, ARCHIVE, COOKIE, HASHER, LIFETIME, LOGIN_PATTERN, PASSWORD_MIN, SETTINGS, UNLIMITED,
    authenticate, authenticate_superadmin, issue_session, revoke_session, revoke_sessions, session_user,
)

WEB = Path(__file__).resolve().parent.parent / "web"
SECURITY = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
RowId = Annotated[int, PathParam(ge=1, le=2**53 - 1)]
ShortPassword = Annotated[str, Field(min_length=1, max_length=256)]
NewPassword = Annotated[str, Field(min_length=PASSWORD_MIN, max_length=256)]


class Login(BaseModel):
    login: Annotated[str, Field(min_length=1, max_length=50)]
    password: ShortPassword


class PasswordChange(BaseModel):
    old_password: ShortPassword
    new_password: NewPassword


class Account(BaseModel):
    login: Annotated[str, Field(pattern=LOGIN_PATTERN)]
    password: NewPassword
    archive: bool = False
    settings: bool = False


class AccountUpdate(BaseModel):
    password: NewPassword | None = None
    archive: bool = False
    settings: bool = False


class LiveFilters(Area):
    minutes: Annotated[int, Field(ge=1, le=360)] = 60


class ArchivePageFilters(ArchiveFilters):
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    cursor: Annotated[str | None, Field(max_length=120)] = None


class Preferences(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=70)] = "MyBlitzortung"
    locale: Literal["ru", "en"] = "ru"
    lat: Annotated[float, Field(ge=-85, le=85)] = 50
    lon: Annotated[float, Field(ge=-180, le=180)] = 20
    zoom: Annotated[int, Field(ge=1, le=18)] = 4
    minutes: Literal[15, 30, 60, 180, 360] = 60


def create_app(config: Config | None = None, engine=None):
    config = config or Config.from_env()
    engine = engine or engine_for(config.database)
    app = FastAPI(
        title="MyBlitzortung", root_path=config.base_path, docs_url=None, redoc_url=None,
        openapi_url=None, redirect_slashes=False,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("origin") != config.origin:
            response = JSONResponse({"detail": "Invalid origin"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = SECURITY
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc):
        errors = [{key: entry[key] for key in ("loc", "type")} for entry in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _exc):
        return JSONResponse({"detail": "Database unavailable"}, status_code=503)

    def connection():
        with engine.begin() as conn:
            yield conn

    Db = Annotated[object, Depends(connection, scope="function")]

    def current_user(request: Request, conn: Db):
        user = session_user(conn, request.cookies.get(COOKIE), config)
        if not user:
            raise HTTPException(401, "Sign in required")
        return user

    Member = Annotated[dict, Depends(current_user)]

    def check_permissions(user, required):
        if not user["level"] & (ADMIN | required):
            raise HTTPException(403, "Permission required")

    def geographic_limit(user):
        if config.view_radius_km and not user["level"] & (ADMIN | UNLIMITED):
            return visible_radius(config.site_lat, config.site_lon, config.view_radius_km)
        return ()

    def login_cookie(response: Response, token: str):
        response.set_cookie(COOKIE, token, max_age=int(LIFETIME.total_seconds()),
                            path=config.base_path + "/", httponly=True, secure=config.secure_cookie, samesite="strict")

    def display_settings(conn, public=False):
        value = conn.scalar(select(settings.c.value).where(settings.c.name == "display"))
        prefs = Preferences.model_validate_json(value) if value else Preferences()
        return {**prefs.model_dump(), "tile_url": config.tile_url, "tile_label_url": config.tile_label_url,
                "tile_attribution": config.tile_attribution, "own_station": 0 if public else config.own_station}

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(conn: Db):
        conn.execute(text("SELECT 1"))
        conn.execute(select(settings.c.name).limit(1))
        return {"status": "ready"}

    @app.post("/api/login")
    def login(payload: Login, request: Request, response: Response, conn: Db):
        user = authenticate_superadmin(payload.login, payload.password, config)
        if not user:
            user = authenticate(conn, payload.login, payload.password)
        if not user:
            raise HTTPException(401, "Invalid credentials")
        prior = request.cookies.get(COOKIE)
        if prior:
            revoke_session(conn, prior, config)
        login_cookie(response, issue_session(conn, user["id"], config))
        return {"login": user["login"], "level": user["level"], "auth_source": user.get("auth_source", "local")}

    @app.post("/api/logout", status_code=204)
    def logout(request: Request, response: Response, conn: Db):
        token = request.cookies.get(COOKIE)
        if token:
            revoke_session(conn, token, config)
        response.delete_cookie(COOKIE, path=config.base_path + "/", secure=config.secure_cookie,
                               httponly=True, samesite="strict")

    @app.get("/api/me")
    def me(user: Member):
        return dict(user)

    @app.put("/api/password", status_code=204)
    def password(payload: PasswordChange, user: Member, response: Response, conn: Db):
        if user["auth_source"] == "superadmin":
            raise HTTPException(403, "Superadmin credentials are managed in the server environment")
        account = authenticate(conn, user["login"], payload.old_password)
        if not account:
            raise HTTPException(403, "Invalid credentials")
        conn.execute(update(users).where(users.c.id == user["id"]).values(password=HASHER.hash(payload.new_password)))
        revoke_sessions(conn, user["id"])
        login_cookie(response, issue_session(conn, user["id"]))

    @app.get("/api/settings")
    def get_settings(conn: Db, _user: Member):
        return display_settings(conn)

    @app.get("/api/public/settings")
    def public_settings(conn: Db):
        return display_settings(conn, public=True)

    @app.put("/api/settings")
    def set_settings(payload: Preferences, user: Member, conn: Db):
        check_permissions(user, SETTINGS)
        value = payload.model_dump_json()
        if conn.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            stmt = insert(settings).values(name="display", value=value)
            stmt = stmt.on_conflict_do_update(index_elements=["name"], set_={"value": value})
        else:
            from sqlalchemy.dialects.mysql import insert
            stmt = insert(settings).values(name="display", value=value).on_duplicate_key_update(value=value)
        conn.execute(stmt)
        return payload

    @app.get("/api/users")
    def accounts(user: Member, conn: Db, after: Annotated[int, Query(ge=0)] = 0):
        check_permissions(user, ADMIN)
        return [dict(row) for row in conn.execute(select(users.c.id, users.c.login, users.c.level)
                .where(users.c.id > after).order_by(users.c.id).limit(200)).mappings()]

    @app.post("/api/users", status_code=201)
    def create_account(payload: Account, user: Member, conn: Db):
        check_permissions(user, ADMIN)
        level = (ARCHIVE if payload.archive else 0) | (SETTINGS if payload.settings else 0)
        try:
            result = conn.execute(users.insert().values(login=payload.login, password=HASHER.hash(payload.password),
                                                       level=level))
        except IntegrityError:
            raise HTTPException(409, "Login already exists") from None
        return {"id": result.inserted_primary_key[0], "login": payload.login, "level": level}

    def mutable_account(conn, account_id):
        level = conn.scalar(select(users.c.level).where(users.c.id == account_id).with_for_update())
        if level is None:
            raise HTTPException(404, "Account not found")
        if level & ADMIN:
            raise HTTPException(403, "Administrator account")

    @app.put("/api/users/{account_id}", status_code=204)
    def edit_account(account_id: RowId, payload: AccountUpdate, user: Member, conn: Db):
        check_permissions(user, ADMIN)
        mutable_account(conn, account_id)
        # Leave migrated permission bits for features outside the web port intact.
        mask = ~(ARCHIVE | SETTINGS)
        values = {"level": users.c.level.bitwise_and(mask).bitwise_or(
            (ARCHIVE if payload.archive else 0) | (SETTINGS if payload.settings else 0)
        )}
        if payload.password:
            values["password"] = HASHER.hash(payload.password)
        conn.execute(update(users).where(users.c.id == account_id).values(**values))
        if payload.password:
            revoke_sessions(conn, account_id)

    @app.delete("/api/users/{account_id}", status_code=204)
    def remove_account(account_id: RowId, user: Member, conn: Db):
        check_permissions(user, ADMIN)
        mutable_account(conn, account_id)
        revoke_sessions(conn, account_id)
        conn.execute(delete(users).where(users.c.id == account_id))

    @app.get("/api/strikes/live")
    def recent(conn: Db, user: Member, area: Annotated[LiveFilters, Query()]):
        now = utcnow()
        return strike_page(conn, (*area.conditions(), *geographic_limit(user), strikes.c.time >= now - timedelta(minutes=area.minutes),
                                  strikes.c.time <= now), 5000)

    @app.get("/api/strikes/public")
    def public_recent(conn: Db, area: Annotated[LiveFilters, Query()]):
        now = utcnow()
        return strike_page(conn, (*area.conditions(), strikes.c.time >= now - timedelta(minutes=area.minutes),
                                  strikes.c.time <= now), 5000)

    @app.get("/api/strikes/archive")
    def archive(conn: Db, user: Member, filters: Annotated[ArchivePageFilters, Query()]):
        check_permissions(user, ARCHIVE)
        return strike_page(conn, (*filters.conditions(), *geographic_limit(user)), filters.limit, filters.cursor)

    @app.get("/api/strikes/density")
    def archive_density(conn: Db, user: Member, filters: Annotated[ArchiveFilters, Query()]):
        check_permissions(user, ARCHIVE)
        return {"items": density(conn, filters, geographic_limit(user)), "cell_degrees": 2}

    @app.get("/api/strikes/{strike_id}/signal")
    def signal(strike_id: RowId, conn: Db, user: Member):
        check_permissions(user, ARCHIVE)
        strike = conn.execute(select(strikes.c.raw_id).where(strikes.c.id == strike_id, *geographic_limit(user))).first()
        if not strike:
            raise HTTPException(404, "Strike not found")
        query = select(raw.c.time, raw.c.channels, raw.c.ntime, raw.c.data)
        row = conn.execute(query.where(raw.c.id == strike.raw_id)).mappings().first() if strike.raw_id else None
        if row is None:
            row = conn.execute(query.where(raw.c.strike_id == strike_id).order_by(raw.c.id).limit(1)).mappings().first()
        if not row or not row["data"]:
            raise HTTPException(404, "Signal not available")
        count = row["channels"] or 1
        if not 1 <= count <= 16:
            raise HTTPException(422, "Unsupported signal encoding")
        data = row["data"][:count * 1024]
        return {"time": utc_label(row["time"]), "sample_interval_ns": row["ntime"],
                "channel_numbers": [row["data"][-1] % 2 + 1] if count == 1 else list(range(1, count + 1)),
                "channels": [[value - 128 for value in data[offset::count]] for offset in range(count)]}

    @app.get("/api/stations")
    def stations_list(conn: Db, user: Member, after: Annotated[int, Query(ge=0)] = 0,
                      limit: Annotated[int, Query(ge=1, le=500)] = 250):
        return station_page(conn, limit, after, bool(user["level"] & (SETTINGS | ADMIN)))

    @app.get("/api/statistics")
    def statistics(conn: Db, user: Member, days: Annotated[int, Query(ge=1, le=31)] = 1,
                   station_id: Annotated[int, Query(ge=0, le=2**53 - 1)] = 0):
        if days > 1:
            check_permissions(user, ARCHIVE)
        end = utcnow()
        start = end - timedelta(days=days)
        return {"strikes": strike_series(conn, start, end, station_id or None, geographic_limit(user)),
                "samples": station_series(conn, station_id, start, end),
                "directions": strike_directions(conn, station_id, start, end, geographic_limit(user))
                if station_id else [],
                "insights": station_insights(conn, station_id, start, end, geographic_limit(user))
                if station_id else None}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    return app

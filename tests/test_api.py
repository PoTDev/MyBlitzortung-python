# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mybo.api import create_app
from mybo.config import Config
from mybo.db import links, raw, samples, sessions, stations, strikes, users, utcnow
from mybo.security import ADMIN, ARCHIVE, COOKIE, HASHER, SETTINGS, UNLIMITED, token_hash

ORIGIN = "https://testserver"
PASSWORD = "old-test-password"
SUPERADMIN = Config("sqlite://", origin=ORIGIN, secure_cookie=True,
                    superadmin_login="site-owner", superadmin_password="configured-admin-secret")


@pytest.fixture
def api(db):
    moment = utcnow().replace(microsecond=0) - timedelta(minutes=4)
    with db.begin() as conn:
        conn.execute(users.insert(), [
            {"id": 1, "login": "admin", "password": HASHER.hash(PASSWORD), "level": ADMIN},
            {"id": 2, "login": "archive", "password": hashlib.md5(PASSWORD.encode()).hexdigest(), "level": ARCHIVE},
            {"id": 3, "login": "viewer", "password": HASHER.hash(PASSWORD), "level": 0},
        ])
        conn.execute(stations.insert().values(id=5, bo_station_id=101, lat=49.3456, lon=179.165, city="Test"))
        for strike_id, ns, lon in [(1, 10, 179.3), (2, 11, -179.5), (3, 12, 15.0), (4, 11, 178.2)]:
            conn.execute(strikes.insert().values(id=strike_id, time=moment, time_ns=ns, lat=49.3, lon=lon))
        conn.execute(links.insert(), [{"strike_id": n, "station_id": 5} for n in (1, 2, 4)])
        conn.execute(samples.insert().values(station_id=5, time=moment, signalsh=8, strikesh=3))
        conn.execute(raw.insert().values(strike_id=4, channels=2, ntime=2000, data=bytes([130, 127, 135, 124])))
    client = TestClient(create_app(Config("sqlite://", origin=ORIGIN, secure_cookie=True), db), base_url=ORIGIN)
    return client, moment


def login(client, name, password=PASSWORD):
    return client.post("/api/login", headers={"Origin": ORIGIN}, json={"login": name, "password": password})


def archive_params(moment):
    return {"start": (moment - timedelta(hours=1)).isoformat() + "Z",
            "end": (moment + timedelta(hours=1)).isoformat() + "Z"}


def test_superadmin_login_permissions_and_logout(api, db):
    _, moment = api
    client = TestClient(create_app(SUPERADMIN, db), base_url=ORIGIN)
    assert client.post("/api/login", json={"login": SUPERADMIN.superadmin_login,
                                         "password": SUPERADMIN.superadmin_password}).status_code == 403
    assert login(client, SUPERADMIN.superadmin_login, "wrong").status_code == 401
    assert login(client, "other-owner", SUPERADMIN.superadmin_password).status_code == 401
    response = login(client, SUPERADMIN.superadmin_login, SUPERADMIN.superadmin_password)
    assert response.status_code == 200
    assert response.json() == {"login": SUPERADMIN.superadmin_login,
                               "level": ADMIN | SETTINGS | UNLIMITED | ARCHIVE,
                               "auth_source": "superadmin"}
    assert SUPERADMIN.superadmin_password not in response.text
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    assert client.get("/api/me").json()["id"] == 0
    assert client.get("/api/strikes/archive", params=archive_params(moment)).status_code == 200
    assert client.put("/api/settings", headers={"Origin": ORIGIN}, json={"title": "Owner"}).status_code == 200
    assert [user["login"] for user in client.get("/api/users").json()] == ["admin", "archive", "viewer"]
    assert client.post("/api/users", headers={"Origin": ORIGIN},
                       json={"login": "new-member", "password": PASSWORD}).status_code == 201
    assert client.put("/api/password", headers={"Origin": ORIGIN}, json={
        "old_password": SUPERADMIN.superadmin_password, "new_password": "replacement-admin-password",
    }).status_code == 403
    token = client.cookies.get(COOKIE)
    with db.connect() as conn:
        stored = conn.execute(sessions.select().where(sessions.c.user_id == 0)).mappings().one()
        assert stored["token"] != token and stored["token"] != token_hash(token)
        assert not conn.execute(users.select().where(users.c.login == SUPERADMIN.superadmin_login)).first()
    # Superadmin sessions persist across API restarts, without a local account or schema change.
    restarted = TestClient(create_app(SUPERADMIN, db), base_url=ORIGIN)
    restarted.cookies.set(COOKIE, token)
    assert restarted.get("/api/me").json()["auth_source"] == "superadmin"
    assert client.post("/api/logout", headers={"Origin": ORIGIN}).status_code == 204
    assert restarted.get("/api/me").status_code == 401


@pytest.mark.parametrize("changes", [
    {"superadmin_password": "rotated-admin-password"}, {"superadmin_login": "new-owner"},
    {"superadmin_login": "", "superadmin_password": ""},
])
def test_superadmin_session_invalidated_when_credentials_change(api, db, changes):
    client = TestClient(create_app(SUPERADMIN, db), base_url=ORIGIN)
    assert login(client, SUPERADMIN.superadmin_login, SUPERADMIN.superadmin_password).status_code == 200
    changed = replace(SUPERADMIN, **changes)
    restarted = TestClient(create_app(changed, db), base_url=ORIGIN)
    restarted.cookies.set(COOKIE, client.cookies.get(COOKIE))
    assert restarted.get("/api/me").status_code == 401
    assert login(restarted, SUPERADMIN.superadmin_login, SUPERADMIN.superadmin_password).status_code == 401
    if changed.superadmin_login and changed.superadmin_password:
        assert login(restarted, changed.superadmin_login, changed.superadmin_password).status_code == 200
    assert login(restarted, "admin").status_code == 200


def test_feed_credentials_do_not_enable_site_login(api, db):
    config = Config("sqlite://", origin=ORIGIN, secure_cookie=True,
                    feed_user="station-owner", feed_password="configured-feed-secret")
    client = TestClient(create_app(config, db), base_url=ORIGIN)
    assert login(client, config.feed_user, config.feed_password).status_code == 401
    assert login(client, "viewer").status_code == 200
    assert client.get("/api/me").json()["auth_source"] == "local"


def test_superadmin_and_local_accounts_with_same_login_remain_separate(api, db):
    with db.begin() as conn:
        conn.execute(users.insert().values(login=SUPERADMIN.superadmin_login,
                                          password=HASHER.hash(PASSWORD), level=0))
    client = TestClient(create_app(SUPERADMIN, db), base_url=ORIGIN)
    assert login(client, SUPERADMIN.superadmin_login, SUPERADMIN.superadmin_password).status_code == 200
    previous_token = client.cookies.get(COOKIE)
    assert login(client, SUPERADMIN.superadmin_login).json()["level"] == 0
    assert client.get("/api/me").json()["auth_source"] == "local"
    assert client.get("/api/users").status_code == 403
    assert client.put("/api/password", headers={"Origin": ORIGIN}, json={
        "old_password": PASSWORD, "new_password": "replacement-local-password",
    }).status_code == 204
    client.cookies.clear()
    client.cookies.set(COOKIE, previous_token)
    assert client.get("/api/me").status_code == 401
    assert login(client, SUPERADMIN.superadmin_login, SUPERADMIN.superadmin_password).status_code == 200
    with db.begin() as conn:
        conn.execute(sessions.update().where(sessions.c.user_id == 0).values(expires=utcnow() - timedelta(seconds=1)))
    assert client.get("/api/me").status_code == 401

def test_session_permissions_csrf_and_upgrade(api, db):
    client, moment = api
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/strikes/live").status_code == 401
    public_settings = client.get("/api/public/settings")
    assert public_settings.status_code == 200 and public_settings.json()["own_station"] == 0
    public = client.get("/api/strikes/public", params={"west": 178, "east": 180})
    assert public.status_code == 200 and public.json()["items"][0]["id"] == 4
    assert client.get("/api/stations").status_code == 401
    assert client.get("/api/statistics").status_code == 401
    assert client.get("/api/strikes/archive", params=archive_params(moment)).status_code == 401
    assert client.post("/api/login", json={"login": "viewer", "password": PASSWORD}).status_code == 403
    assert login(client, "viewer", "wrong").status_code == 401
    response = login(client, "viewer")
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/strikes/archive", params=archive_params(moment)).status_code == 403
    assert client.get("/api/statistics", params={"days": 3}).status_code == 403
    assert client.get("/api/stations").json()["items"][0]["lat"] == 49.3
    assert client.put("/api/settings", headers={"Origin": ORIGIN}, json={}).status_code == 403
    assert login(client, "archive").status_code == 200
    with db.connect() as conn:
        assert conn.execute(users.select().where(users.c.id == 2)).mappings().one()["password"].startswith("$argon2id$")
    assert client.post("/api/logout", headers={"Origin": ORIGIN}).status_code == 204
    assert client.get("/api/me").status_code == 401


def test_archive_map_cursor_density_stats_and_signals(api):
    client, moment = api
    login(client, "archive")
    path = "/api/strikes/archive"
    params = {**archive_params(moment), "west": 178, "east": -178, "station_id": 5}
    page = client.get(path, params={**params, "limit": 1}).json()
    assert page["items"][0]["id"] == 4
    second = client.get(path, params={**params, "limit": 2, "cursor": page["next"]}).json()
    assert [item["id"] for item in second["items"]] == [2, 1]
    assert second["next"] is None
    assert client.get(path, params={**params, "cursor": "bad!"}).status_code == 422
    assert client.get(path, params={**params, "end": "2030-01-01T00:00:00Z"}).status_code == 422
    assert client.get("/api/strikes/live", params={"west": 178, "east": 180}).json()["items"][0]["id"] == 4
    result = client.get("/api/strikes/density", params=params).json()
    assert sum(cell["total"] for cell in result["items"]) == 3
    series = client.get("/api/statistics", params={"station_id": 5, "days": 7}).json()
    assert sum(point["count"] for point in series["strikes"]) == 3
    assert series["samples"][0]["signalsh"] == 8
    assert len(series["directions"]) == 16
    assert sum(point["count"] for point in series["directions"]) == 3
    insights = series["insights"]
    assert insights["summary"] == {
        "last_sample": moment.isoformat() + "Z", "signal_rate": 8, "strike_rate": 3,
        "efficiency": 37.5, "detected_period": 3,
    }
    assert len(insights["radii"]) == 4
    assert sum(bucket["count"] for bucket in insights["distances"]) == 3
    assert sum(hour["count"] for hour in insights["hours"]) == 3
    network = client.get("/api/statistics", params={"station_id": 0}).json()
    assert network["directions"] == [] and network["insights"] is None
    assert client.get("/api/strikes/4/signal").json()["channels"] == [[2, 7], [-1, -4]]
    assert client.get("/api/strikes/1/signal").status_code == 404


def test_preferences_users_and_password(api):
    client, _ = api
    login(client, "admin")
    assert client.put("/api/settings", headers={"Origin": ORIGIN},
                      json={"title": "Станция", "locale": "ru", "zoom": 8}).status_code == 200
    assert client.get("/api/settings").json()["title"] == "Станция"
    response = client.post("/api/users", headers={"Origin": ORIGIN},
                           json={"login": "new", "password": "test-new-password", "settings": True})
    assert response.status_code == 201
    account = response.json()
    assert account["level"] == SETTINGS
    assert client.put(f'/api/users/{account["id"]}', headers={"Origin": ORIGIN},
                      json={"archive": True}).status_code == 204
    assert client.delete(f'/api/users/{account["id"]}', headers={"Origin": ORIGIN}).status_code == 204
    assert client.delete("/api/users/1", headers={"Origin": ORIGIN}).status_code == 403
    assert client.put("/api/password", headers={"Origin": ORIGIN},
                      json={"old_password": PASSWORD, "new_password": "a-new-test-password"}).status_code == 204
    assert client.get("/api/me").status_code == 200
    assert login(client, "admin", "a-new-test-password").status_code == 200
    assert login(client, "admin").status_code == 401
    secret = "very-private"
    response = client.post("/api/users", headers={"Origin": ORIGIN},
                           json={"login": "new", "password": secret[:7]})
    assert response.status_code == 422 and secret[:7] not in response.text


def test_reverse_proxy_path_and_static_assets(db):
    client = TestClient(create_app(Config("sqlite://", origin=ORIGIN, base_path="/mybo"), db),
                        base_url=ORIGIN, root_path="/mybo")
    with db.begin() as conn:
        conn.execute(users.insert().values(login="admin", password=HASHER.hash(PASSWORD), level=ADMIN))
    assert client.get("/").status_code == 200
    markup = client.get("/").text
    assert 'id="login-open"' in markup and 'id="register"' in markup
    assert '<form id="stats-form"' in markup and 'data-t="ok"' in markup
    assert client.get("/mybo/assets/app.js").status_code == 200
    assert client.get("/mybo/legacy/index.php").status_code == 404
    response = login(client, "admin")
    assert "Path=/mybo/" in response.headers["set-cookie"]
    assert client.get("/mybo/api/me").status_code == 200


def test_optional_radius_filters_strike_endpoints(api, db):
    _, moment = api
    config = Config("sqlite://", origin=ORIGIN, view_radius_km=35, site_lat=49.3, site_lon=179.3)
    client = TestClient(create_app(config, db), base_url=ORIGIN)
    assert len(client.get("/api/strikes/public").json()["items"]) == 4
    assert login(client, "archive").status_code == 200
    assert [item["id"] for item in client.get("/api/strikes/live").json()["items"]] == [1]
    assert [item["id"] for item in client.get("/api/strikes/archive", params=archive_params(moment)).json()["items"]] == [1]
    assert client.get("/api/strikes/4/signal").status_code == 404
    login(client, "admin")
    assert len(client.get("/api/strikes/live").json()["items"]) == 4

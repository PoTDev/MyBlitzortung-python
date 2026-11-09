# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from mybo.config import Config


def test_local_defaults_and_sqlite_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("MYBO_ORIGIN", raising=False)
    monkeypatch.delenv("MYBO_SECURE_COOKIE", raising=False)
    monkeypatch.setenv("MYBO_DATABASE_URL", f"sqlite:///{tmp_path / 'mybo.sqlite3'}")

    config = Config.from_env()

    assert config.origin == "http://127.0.0.1:8000"
    assert config.secure_cookie is False
    assert config.database == f"sqlite:///{tmp_path / 'mybo.sqlite3'}"


def test_label_tiles_require_an_https_base_layer(monkeypatch):
    monkeypatch.setenv(
        "MYBO_TILE_LABEL_URL",
        "https://server.example.org/tiles/{z}/{x}/{y}",
    )
    monkeypatch.delenv("MYBO_TILE_URL", raising=False)
    monkeypatch.delenv("MYBO_TILE_ATTRIBUTION", raising=False)
    with pytest.raises(ValueError, match="base layer"):
        Config.from_env()

    monkeypatch.setenv(
        "MYBO_TILE_URL",
        "https://server.example.org/imagery/{z}/{x}/{y}",
    )
    monkeypatch.setenv("MYBO_TILE_ATTRIBUTION", "Map provider")
    assert Config.from_env().tile_label_url.endswith("/{z}/{x}/{y}")


def test_remote_import_requires_authorized_https(monkeypatch):
    monkeypatch.setenv("BO_IMPORT_ENABLED", "true")
    monkeypatch.setenv("BO_FEED_USER", "station-operator")
    monkeypatch.setenv("BO_FEED_PASSWORD", "authorized-test-secret")
    monkeypatch.setenv("BO_FEED_BASE", "http://feed.example.org/Data_1/Protected")
    with pytest.raises(ValueError, match="HTTPS"):
        Config.from_env()
    monkeypatch.setenv("BO_FEED_BASE", "https://feed.example.org/Data_1/Protected")
    assert Config.from_env().import_enabled
    monkeypatch.delenv("BO_FEED_PASSWORD")
    with pytest.raises(ValueError, match="credentials"):
        Config.from_env()


def test_superadmin_requires_complete_valid_credentials(monkeypatch):
    monkeypatch.setenv("MYBO_SUPERADMIN_LOGIN", "owner")
    monkeypatch.delenv("MYBO_SUPERADMIN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="Both MYBO_SUPERADMIN"):
        Config.from_env()
    monkeypatch.setenv("MYBO_SUPERADMIN_PASSWORD", "short")
    with pytest.raises(ValueError, match="Invalid superadmin"):
        Config.from_env()
    monkeypatch.setenv("MYBO_SUPERADMIN_PASSWORD", "unique-admin-password")
    config = Config.from_env()
    assert config.superadmin_login == "owner"

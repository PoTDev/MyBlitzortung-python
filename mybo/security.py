# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select, update

from .config import Config
from .db import sessions, users, utcnow

ADMIN = 1
SETTINGS = 2
UNLIMITED = 4
ARCHIVE = 128
COOKIE = "mybo_session"
LIFETIME = timedelta(hours=12)
LOGIN_PATTERN = r"^[A-Za-z0-9_.@+-]{1,50}$"
PASSWORD_MIN = 12
HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_hex(32))
LEGACY_HASH = re.compile(r"[a-fA-F0-9]{32}")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate_superadmin(login: str, password: str, config: Config):
    if not config.superadmin_login or not config.superadmin_password:
        return None
    login_matches = hmac.compare_digest(login.encode(), config.superadmin_login.encode())
    password_matches = hmac.compare_digest(password.encode(), config.superadmin_password.encode())
    if login_matches and password_matches:
        # ID 0 is reserved for the configured superadmin, outside the local users table.
        return {"id": 0, "login": config.superadmin_login, "level": ADMIN | SETTINGS | UNLIMITED | ARCHIVE,
                "auth_source": "superadmin"}
    return None


def _superadmin_session_hash(token: str, config: Config) -> str:
    # Bind superadmin sessions to both credentials so rotation invalidates old sessions.
    message = json.dumps(["superadmin-session", config.superadmin_login, token]).encode()
    return hmac.new(config.superadmin_password.encode(), message, hashlib.sha256).hexdigest()


def authenticate(conn, login: str, password: str):
    user = conn.execute(select(users).where(users.c.login == login).with_for_update()).mappings().first()
    encoded = user["password"] if user else DUMMY_HASH
    if LEGACY_HASH.fullmatch(encoded):
        password = password.strip(" \n\r\t\v\0")
        valid = bool(password) and hmac.compare_digest(
            hashlib.md5(password.encode(), usedforsecurity=False).hexdigest(), encoded.lower(),
        )
        if not valid:
            try:
                HASHER.verify(DUMMY_HASH, password)
            except VerificationError:
                pass
            return None
        replacement = HASHER.hash(password)
    else:
        try:
            if not HASHER.verify(encoded, password) or not user:
                return None
        except (VerificationError, InvalidHashError):
            return None
        replacement = HASHER.hash(password) if HASHER.check_needs_rehash(encoded) else None
    if replacement:
        changed = conn.execute(update(users).where(
            users.c.id == user["id"], users.c.password == encoded,
        ).values(password=replacement))
        if changed.rowcount != 1:
            return None
    return user


def issue_session(conn, user_id: int, config: Config | None = None) -> str:
    token = secrets.token_urlsafe(32)
    if user_id == 0:
        if not config or not config.superadmin_login or not config.superadmin_password:
            raise ValueError("Superadmin credentials are required for a superadmin session")
        digest = _superadmin_session_hash(token, config)
    else:
        digest = token_hash(token)
    conn.execute(sessions.insert().values(
        token=digest, user_id=user_id, expires=utcnow() + LIFETIME,
    ))
    return token


def session_user(conn, token: str | None, config: Config | None = None):
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    user = conn.execute(
        select(users.c.id, users.c.login, users.c.level)
        .join(sessions, sessions.c.user_id == users.c.id)
        .where(sessions.c.token == token_hash(token), sessions.c.expires > utcnow(), users.c.id > 0)
    ).mappings().first()
    if user:
        return {**user, "auth_source": "local"}
    if config and config.superadmin_login and config.superadmin_password:
        owner_session = conn.scalar(select(sessions.c.token).where(
            sessions.c.token == _superadmin_session_hash(token, config),
            sessions.c.user_id == 0, sessions.c.expires > utcnow(),
        ))
        if owner_session:
            return authenticate_superadmin(config.superadmin_login, config.superadmin_password, config)
    return None


def revoke_session(conn, token: str, config: Config):
    digests = [token_hash(token)]
    if config.superadmin_login and config.superadmin_password:
        digests.append(_superadmin_session_hash(token, config))
    conn.execute(delete(sessions).where(sessions.c.token.in_(digests)))


def revoke_sessions(conn, user_id: int):
    conn.execute(delete(sessions).where(sessions.c.user_id == user_id))

# -*- coding: utf-8 -*-
"""Запись статистики действий в crm.statistics (без UI дашборда)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

from .crm_task_store import _pg_connection
from .db import DatabaseConnection
from .log_util import log_warning

STATISTICS_SCHEMA = "crm"
STATISTICS_TABLE = "statistics"

OFFICE_SESSION_ROLES = frozenset({"office", "manager", "admin"})


def map_session_role_to_statistics(role: str) -> Optional[str]:
    role = (role or "").strip()
    if role == "field":
        return "field"
    if role in OFFICE_SESSION_ROLES:
        return "office"
    return None


@contextmanager
def skip_field_complete_trigger(conn: DatabaseConnection) -> Iterator[None]:
    pg = _pg_connection(conn)
    if pg is not None:
        with pg.cursor() as cur:
            cur.execute("SET LOCAL crm.statistics_skip_field_complete = 'true'")
    try:
        yield
    finally:
        pass


@contextmanager
def skip_area_complete_trigger(conn: DatabaseConnection) -> Iterator[None]:
    pg = _pg_connection(conn)
    if pg is not None:
        with pg.cursor() as cur:
            cur.execute("SET LOCAL crm.statistics_skip_area_complete = 'true'")
    try:
        yield
    finally:
        pass


def _serialize_metadata(metadata: Optional[Dict[str, Any]]) -> str:
    payload = dict(metadata or {})
    payload.setdefault("source", "qgis")
    return json.dumps(payload)


def resolve_role_from_login(conn: DatabaseConnection, login: str) -> Optional[str]:
    login = (login or "").strip()
    if not login:
        return None
    pg = _pg_connection(conn)
    if pg is None:
        return None
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT role FROM crm.users WHERE login = %s LIMIT 1", (login,))
            row = cur.fetchone()
        return map_session_role_to_statistics(str(row[0])) if row else None
    except Exception as exc:
        log_warning(f"Не удалось определить роль для статистики ({login}): {exc}")
        return None


def log_statistic(
    conn: DatabaseConnection,
    *,
    login: str,
    object_type: str,
    action: str,
    object_key: str,
    session_role: Optional[str] = None,
    created_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
    skip_if_exists: bool = True,
) -> None:
    login = (login or "").strip()
    if not login:
        return

    pg = _pg_connection(conn)
    if pg is None:
        return

    user_role = (
        map_session_role_to_statistics(session_role) if session_role else None
    )
    if user_role is None:
        user_role = resolve_role_from_login(conn, login)
    if user_role is None:
        return

    stamp = created_at or datetime.now(timezone.utc)
    meta_json = _serialize_metadata(metadata)

    try:
        if skip_if_exists:
            exists_sql = f"""
                SELECT 1
                FROM "{STATISTICS_SCHEMA}"."{STATISTICS_TABLE}"
                WHERE object_type = %s AND object_key = %s::uuid AND action = %s
                LIMIT 1
            """
            with pg.cursor() as cur:
                cur.execute(exists_sql, (object_type, object_key, action))
                if cur.fetchone():
                    return

        query = f"""
            INSERT INTO "{STATISTICS_SCHEMA}"."{STATISTICS_TABLE}" (
                user_id, user_login, user_role, object_type, action,
                object_key, created_at, metadata
            )
            SELECT
                u.uuid,
                %s,
                %s,
                %s,
                %s,
                %s::uuid,
                %s,
                %s::jsonb
            FROM (SELECT 1) AS _dummy
            LEFT JOIN crm.users u ON u.login = %s
            LIMIT 1
        """
        with pg.cursor() as cur:
            cur.execute(
                query,
                (
                    login,
                    user_role,
                    object_type,
                    action,
                    object_key,
                    stamp,
                    meta_json,
                    login,
                ),
            )
        pg.commit()
    except Exception as exc:
        try:
            pg.rollback()
        except Exception:
            pass
        log_warning(
            f"Не удалось записать статистику ({action}, {object_key}): {exc}"
        )

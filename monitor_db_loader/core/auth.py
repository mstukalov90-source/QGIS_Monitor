# -*- coding: utf-8 -*-
"""Авторизация пользователей CRM и ролевой доступ."""

from dataclasses import dataclass
from typing import List, Optional, Set

from .crm_ui_constants import TASK_SOURCES, TaskSource
from .db import DatabaseConnection
from .log_util import log_warning

DB_PASSWORD = "monitor1"

HOOD_SCHEMA = "odh_export"
HOOD_TABLE = "hood"

ROLE_TASK_SOURCES: dict[str, List[TaskSource]] = {
    "admin": list(TASK_SOURCES),
    "field": ["field", "area_wip"],
    "office": ["active", "area_wip", "area_done"],
    "manager": list(TASK_SOURCES),
}

DEFAULT_TASK_SOURCE: dict[str, TaskSource] = {
    "admin": "active",
    "field": "field",
    "office": "active",
    "manager": "active",
}


@dataclass(frozen=True)
class UserSession:
    uuid: str
    login: str
    role: str
    work_zones: List[int]


def districts_unrestricted(session: UserSession) -> bool:
    return session.role == "admin"


def allowed_task_sources(role: str) -> List[TaskSource]:
    return list(ROLE_TASK_SOURCES.get(role, []))


def default_task_source(role: str) -> TaskSource:
    return DEFAULT_TASK_SOURCE.get(role, "active")


def hood_sql_filter(session: UserSession) -> str:
    if districts_unrestricted(session):
        return ""
    if not session.work_zones:
        return ""
    gids = ",".join(str(g) for g in session.work_zones)
    return f'"gid" IN ({gids})'


def apply_hood_filter_to_layer_def(
    layer_def: dict, session: Optional[UserSession]
) -> dict:
    if session is None:
        return layer_def
    schema = str(layer_def.get("schema", "public"))
    table = str(layer_def.get("table_name", ""))
    display_name = str(layer_def.get("display_name", ""))
    if not (
        (schema == HOOD_SCHEMA and table == HOOD_TABLE)
        or display_name == "Границы районов"
    ):
        return layer_def
    filt = hood_sql_filter(session)
    if not filt:
        return layer_def
    layer_def = dict(layer_def)
    existing = layer_def.get("sql_filter", "")
    if existing:
        layer_def["sql_filter"] = f"({existing}) AND ({filt})"
    else:
        layer_def["sql_filter"] = filt
    return layer_def


def authenticate(
    conn: DatabaseConnection, login: str, password: str
) -> Optional[UserSession]:
    pg = conn._get_pg_connection()
    if pg is None:
        log_warning("authenticate: psycopg2 недоступен")
        return None
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT uuid::text, login, role, work_zones
                FROM crm.users
                WHERE login = %s AND password = crypt(%s, password)
                """,
                (login.strip(), password),
            )
            row = cur.fetchone()
    except Exception as exc:
        log_warning(f"authenticate: ошибка запроса crm.users: {exc}")
        try:
            pg.rollback()
        except Exception:
            pass
        return None
    if not row:
        return None
    work_zones = [int(g) for g in (row[3] or [])]
    return UserSession(
        uuid=str(row[0]),
        login=str(row[1]),
        role=str(row[2]),
        work_zones=work_zones,
    )


def fetch_allowed_rayons(
    conn: DatabaseConnection, session: UserSession
) -> List[str]:
    if districts_unrestricted(session):
        return []
    if not session.work_zones:
        return []
    pg = conn._get_pg_connection()
    if pg is None:
        return []
    try:
        with pg.cursor() as cur:
            cur.execute(
                f"""
                SELECT rayon
                FROM {HOOD_SCHEMA}.{HOOD_TABLE}
                WHERE gid = ANY(%s)
                ORDER BY rayon
                """,
                (session.work_zones,),
            )
            return [str(row[0]).strip() for row in cur.fetchall() if row[0]]
    except Exception as exc:
        log_warning(f"fetch_allowed_rayons: {exc}")
        try:
            pg.rollback()
        except Exception:
            pass
        return []


def allowed_rayons_set(
    conn: Optional[DatabaseConnection], session: UserSession
) -> Optional[Set[str]]:
    if districts_unrestricted(session):
        return None
    if conn is not None:
        rayons = fetch_allowed_rayons(conn, session)
        if rayons:
            return set(rayons)
    if session.work_zones:
        return set()
    return None


def is_rayon_allowed(
    conn: Optional[DatabaseConnection],
    session: UserSession,
    rayon: str,
) -> bool:
    allowed = allowed_rayons_set(conn, session)
    if allowed is None:
        return True
    return rayon.strip() in allowed


def filter_rayons_on_layer(
    layer, field: str, session: UserSession
) -> List[str]:
    """Районы из слоя границ с учётом work_zones (без подключения к БД)."""
    from ..ui.district_dialog import collect_rayon_names

    names = collect_rayon_names(layer, field)
    if districts_unrestricted(session) or not session.work_zones:
        return names
    gid_idx = layer.fields().indexOf("gid")
    if gid_idx < 0:
        return names
    allowed_gids = set(session.work_zones)
    filtered: Set[str] = set()
    for feat in layer.getFeatures():
        gid = feat["gid"]
        if gid is None:
            continue
        try:
            gid_val = int(gid)
        except (TypeError, ValueError):
            continue
        if gid_val not in allowed_gids:
            continue
        val = feat[field]
        if val is None:
            continue
        text = str(val).strip()
        if text:
            filtered.add(text)
    return sorted(filtered)

# -*- coding: utf-8 -*-
"""CRM tasks_area — площадные заказы."""

from typing import Any, Dict, List, Literal, Optional, Tuple

from qgis.core import QgsGeometry

from .crm_tasks import TaskFeature, TaskGroup, TaskResult, TaskSubgroup, _date_filter_range
from .crm_ui_constants import (
    AREA_GROUP_NAME,
    AREA_LAYER_KEY,
    AREA_LAYER_NAME,
    AREA_STATUS_LABELS,
    AreaStatus,
    normalize_rayon_name,
)
from .crm_task_store import (
    _pg_connection,
    _pg_recover_transaction,
    _pg_rollback,
    ensure_user_audit_columns,
    make_user_audit,
)
from .db import DatabaseConnection
from .log_util import log_info, log_warning

AreaTransitionResult = Literal["updated", "skipped", "not_found"]
AnaliseTransitionResult = Literal["updated", "skipped", "not_found", "conflict"]

_AREA_STATUS_ACTIONS: Dict[Tuple[Optional[str], str], str] = {
    (None, "wip"): "order_sent_to_survey",
    ("wip", "free"): "order_released_from_survey",
    ("wip", "done"): "order_completed_survey",
}


def _log_area_status_change(
    conn: DatabaseConnection,
    *,
    key: str,
    login: str,
    from_status: Optional[str],
    to_status: str,
) -> None:
    from .crm_statistics import log_statistic, resolve_role_from_login

    default_action = _AREA_STATUS_ACTIONS.get((from_status, to_status))
    if not default_action:
        return

    role = resolve_role_from_login(conn, login)
    if to_status == "done" and from_status == "wip":
        action = "order_completed" if role == "field" else "order_completed_survey"
    else:
        action = default_action

    log_statistic(
        conn,
        login=login,
        object_type="order",
        action=action,
        object_key=key,
        metadata={"from_status": from_status, "to_status": to_status},
        skip_if_exists=action in ("order_completed", "order_completed_survey"),
    )

ANALISE_AUDIT_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("analise_started_by", "TEXT"),
    ("analise_started_at", "TIMESTAMPTZ"),
    ("analise_finished_by", "TEXT"),
    ("analise_finished_at", "TIMESTAMPTZ"),
    ("analise_paused_by", "TEXT"),
    ("analise_paused_at", "TIMESTAMPTZ"),
)

_TASKS_AREA_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_tasks_area_rayon
        ON crm.tasks_area (rayon) WHERE geom IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tasks_area_geom
        ON crm.tasks_area USING GIST (geom)
    """,
)

_tasks_area_indexes_ready: set[str] = set()
_analise_audit_ready: set[str] = set()

AreaGeometryRow = Tuple[Dict[str, Any], Optional[QgsGeometry]]

TASKS_AREA_SCHEMA = "crm"
TASKS_AREA_TABLE = "tasks_area"


def ensure_tasks_area_indexes(conn: DatabaseConnection) -> bool:
    pg = _pg_connection(conn)
    if pg is None:
        return False
    key = f"{TASKS_AREA_SCHEMA}.{TASKS_AREA_TABLE}.indexes"
    if key in _tasks_area_indexes_ready:
        return True
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            for stmt in _TASKS_AREA_INDEXES:
                cur.execute(stmt)
        pg.commit()
        _tasks_area_indexes_ready.add(key)
        return True
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось создать индексы crm.tasks_area: {exc}")
        return False


def ensure_analise_audit_columns(conn: DatabaseConnection) -> bool:
    pg = _pg_connection(conn)
    if pg is None:
        return False
    key = f"{TASKS_AREA_SCHEMA}.{TASKS_AREA_TABLE}"
    if key in _analise_audit_ready:
        return True
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            for col_name, col_type in ANALISE_AUDIT_COLUMNS:
                cur.execute(
                    f'ALTER TABLE "{TASKS_AREA_SCHEMA}"."{TASKS_AREA_TABLE}" '
                    f'ADD COLUMN IF NOT EXISTS "{col_name}" {col_type}'
                )
        pg.commit()
        _analise_audit_ready.add(key)
        return True
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось добавить analise-столбцы в crm.tasks_area: {exc}")
        return False


def ensure_tasks_area_audit_columns(conn: DatabaseConnection) -> bool:
    pg = _pg_connection(conn)
    if pg is None:
        return False
    _pg_recover_transaction(pg)
    try:
        ensure_user_audit_columns(pg, TASKS_AREA_SCHEMA, TASKS_AREA_TABLE)
        pg.commit()
        return True
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось добавить audit-столбцы в crm.tasks_area: {exc}")
        return False


def _geometry_from_row(
    geom_wkb,
    geom_wkt,
    *,
    row_hint: Optional[Dict[str, Any]] = None,
) -> Optional[QgsGeometry]:
    if geom_wkb is not None:
        try:
            raw = (
                geom_wkb
                if isinstance(geom_wkb, (bytes, bytearray))
                else bytes(geom_wkb)
            )
            geom = QgsGeometry.fromWkb(raw)
            if geom and not geom.isEmpty():
                return geom
        except Exception:
            pass
    if geom_wkt:
        try:
            geom = QgsGeometry.fromWkt(str(geom_wkt))
            if geom and not geom.isEmpty():
                return geom
        except Exception:
            pass
    if row_hint is not None and (geom_wkb is not None or geom_wkt):
        key = row_hint.get("key") or "?"
        fid = row_hint.get("fid") or "?"
        log_warning(
            f"crm.tasks_area: не удалось распарсить geom (key={key}, fid={fid})"
        )
    return None


def _count_valid_geometries(rows: List[AreaGeometryRow]) -> int:
    return sum(
        1
        for _attrs, geom in rows
        if geom is not None and not geom.isEmpty()
    )


def _log_area_geometry_stats(
    rows: List[AreaGeometryRow],
    rayon_norm: str,
    *,
    status: Optional[str] = None,
) -> None:
    geom_ok = _count_valid_geometries(rows)
    geom_fail = len(rows) - geom_ok
    log_info(
        f"crm.tasks_area: rows={len(rows)} geom_ok={geom_ok} geom_fail={geom_fail} "
        f"(район «{rayon_norm}», status={status or 'all'})"
    )
    if geom_fail > 0:
        log_warning(
            f"crm.tasks_area: {geom_fail} записей без геометрии (район «{rayon_norm}»)"
        )


def _normalize_area_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(attrs)
    key = result.get("key")
    if key is not None:
        result["key"] = str(key)
    for field in ("fid", "gid", "area", "status", "rayon", "okrug", "okrug_shor"):
        if field in result and result[field] is not None:
            if field in ("fid", "gid"):
                result[field] = int(result[field]) if result[field] != "" else result[field]
            elif field == "area":
                try:
                    result[field] = float(result[field])
                except (TypeError, ValueError):
                    pass
            else:
                result[field] = str(result[field]).strip()
    return result


def _filter_rows_by_status(
    rows: List[AreaGeometryRow], status: Optional[str]
) -> List[AreaGeometryRow]:
    if not status:
        return list(rows)
    return [
        (attrs, geom)
        for attrs, geom in rows
        if (attrs.get("status") or "") == status
    ]


def fetch_tasks_area_geometries(
    conn: DatabaseConnection,
    rayon: str,
    status: Optional[str] = None,
    limit: int = 5000,
) -> List[AreaGeometryRow]:
    pg = _pg_connection(conn)
    if pg is None:
        return []

    rayon_norm = normalize_rayon_name(rayon)
    ensure_tasks_area_indexes(conn)
    filters = ['"geom" IS NOT NULL', '"rayon" = %s']
    params: List[Any] = [rayon_norm]
    if status:
        filters.append('"status" = %s')
        params.append(status)
    params.append(limit)
    where = " AND ".join(filters)

    query = f"""
        SELECT key, fid, gid, rayon, okrug, okrug_shor, area, status,
               date_survey, loaded_at, task_number, analise,
               analise_started_by, analise_started_at,
               analise_finished_by, analise_finished_at,
               analise_paused_by, analise_paused_at,
               ST_AsBinary(ST_Transform(geom, 4326)) AS geom_wkb,
               ST_AsText(ST_Transform(geom, 4326)) AS geom_wkt
        FROM crm.tasks_area
        WHERE {where}
        ORDER BY loaded_at DESC NULLS LAST
        LIMIT %s
    """

    rows: List[AreaGeometryRow] = []
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, params)
            col_names = [d[0] for d in cur.description]
            for row in cur.fetchall():
                data = dict(zip(col_names, row))
                geom_wkb = data.pop("geom_wkb", None)
                geom_wkt = data.pop("geom_wkt", None)
                attrs = _normalize_area_attrs(data)
                geom = _geometry_from_row(
                    geom_wkb, geom_wkt, row_hint=attrs
                )
                rows.append((attrs, geom))
        pg.commit()
        _log_area_geometry_stats(rows, rayon_norm, status=status)
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить crm.tasks_area: {exc}")
    return rows


def preload_area_geometries(
    conn: DatabaseConnection, rayon: str, limit: int = 5000
) -> int:
    """Загрузить все полигоны района в кэш соединения (один SQL до открытия диалога)."""
    rayon_norm = normalize_rayon_name(rayon)
    rows = fetch_tasks_area_geometries(conn, rayon=rayon_norm, status=None, limit=limit)
    conn.set_area_rows_cache(rayon_norm, rows)
    geom_ok = _count_valid_geometries(rows)
    log_info(
        f"crm.tasks_area: кэш района «{rayon_norm}» — "
        f"{geom_ok} полигонов из {len(rows)} записей"
    )
    return geom_ok


def _refresh_area_cache_row(conn: DatabaseConnection, key: str) -> None:
    state = _fetch_analise_state(conn, key)
    if state is None:
        return
    conn.update_area_row_attrs(key, state, normalize=_normalize_area_attrs)


def invalidate_area_geometries_cache(
    conn: DatabaseConnection, rayon: Optional[str] = None
) -> None:
    if rayon is None:
        conn.clear_area_rows_cache()
    else:
        conn.clear_area_rows_cache(normalize_rayon_name(rayon))


def get_area_geometries(
    conn: DatabaseConnection,
    rayon: str,
    status: Optional[str] = None,
    limit: int = 5000,
) -> List[AreaGeometryRow]:
    """Полигоны из кэша (фильтр по status in-memory) или fallback SQL."""
    rayon_norm = normalize_rayon_name(rayon)
    cached = conn.get_area_rows_cache(rayon_norm)
    if cached is not None:
        if cached and _count_valid_geometries(cached) == 0:
            log_warning(
                f"crm.tasks_area: кэш района «{rayon_norm}» без геометрии — перезагрузка"
            )
            conn.clear_area_rows_cache(rayon_norm)
        else:
            return _filter_rows_by_status(cached, status)

    if status:
        return fetch_tasks_area_geometries(
            conn, rayon=rayon_norm, status=status, limit=limit
        )

    rows = fetch_tasks_area_geometries(
        conn, rayon=rayon_norm, status=None, limit=limit
    )
    conn.set_area_rows_cache(rayon_norm, rows)
    return rows


def _rows_to_features(rows: List[AreaGeometryRow]) -> List[TaskFeature]:
    features: List[TaskFeature] = []
    for attrs, area_geom in rows:
        task_key = str(attrs.get("key", "")).strip() or None
        features.append(
            TaskFeature(
                layer=None,
                layer_name=AREA_LAYER_NAME,
                layer_key=AREA_LAYER_KEY,
                feature_id=None,
                attributes=attrs,
                task_key=task_key,
                area_geom=area_geom,
            )
        )
    return features


def collect_area_orders_for_picker(
    conn: DatabaseConnection,
    rayon: str,
) -> List[TaskFeature]:
    """Все площадные заказы района для диалога выбора (office)."""
    rows = get_area_geometries(conn, rayon=rayon, status=None)
    return _rows_to_features(rows)


def collect_tasks_area(
    conn: DatabaseConnection,
    rayon: str,
    status: AreaStatus,
) -> TaskResult:
    if status not in AREA_STATUS_LABELS:
        raise ValueError(f"Unknown area status: {status}")

    rows = get_area_geometries(conn, rayon=rayon, status=status)
    features = _rows_to_features(rows)

    subgroup = TaskSubgroup(
        name=AREA_STATUS_LABELS.get(status, status),
        features=features,
    )
    group = TaskGroup(name=AREA_GROUP_NAME, subgroups=[subgroup])

    date_from, date_to = _date_filter_range(3)
    return TaskResult(
        district_name=normalize_rayon_name(rayon),
        filter_date_from=date_from,
        filter_date_to=date_to,
        apply_date_filter=False,
        groups=[group],
        task_source=f"area_{status}",
    )


def send_area_to_survey(
    conn: DatabaseConnection, key: str, login: str
) -> AreaTransitionResult:
    return _transition_area_status(
        conn, key, login=login, from_status=None, to_status="wip", skip_if="wip"
    )


def release_area_from_survey(
    conn: DatabaseConnection, key: str, login: str
) -> AreaTransitionResult:
    return _transition_area_status(
        conn, key, login=login, from_status="wip", to_status="free"
    )


def complete_area_survey(
    conn: DatabaseConnection, key: str, login: str
) -> AreaTransitionResult:
    return _transition_area_status(
        conn, key, login=login, from_status="wip", to_status="done"
    )


def _fetch_analise_state(
    conn: DatabaseConnection, key: str
) -> Optional[Dict[str, Any]]:
    pg = _pg_connection(conn)
    if pg is None:
        return None
    ensure_analise_audit_columns(conn)
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT
                    analise,
                    analise_started_by,
                    analise_started_at,
                    analise_paused_by,
                    analise_paused_at
                FROM crm.tasks_area
                WHERE key = %s::uuid
                """,
                (key,),
            )
            row = cur.fetchone()
            if not row:
                pg.commit()
                return None
            cols = [d[0] for d in cur.description]
            pg.commit()
            return dict(zip(cols, row))
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось прочитать analise для {key}: {exc}")
        return None


def start_area_analise(
    conn: DatabaseConnection, key: str, login: str
) -> AnaliseTransitionResult:
    ensure_tasks_area_audit_columns(conn)
    ensure_analise_audit_columns(conn)
    state = _fetch_analise_state(conn, key)
    if state is None:
        return "not_found"
    if state.get("analise") is True:
        return "skipped"

    started_at = state.get("analise_started_at")
    started_by = (state.get("analise_started_by") or "").strip()
    paused_at = state.get("analise_paused_at")
    login = login.strip()

    pg = _pg_connection(conn)
    if pg is None:
        return "not_found"

    if started_at is None:
        audit = make_user_audit(login)
        _pg_recover_transaction(pg)
        try:
            with pg.cursor() as cur:
                cur.execute(
                    """
                    UPDATE crm.tasks_area SET
                        analise_started_by = %s,
                        analise_started_at = NOW(),
                        analise_paused_by = NULL,
                        analise_paused_at = NULL,
                        user_last_edit = %s::text[]
                    WHERE key = %s::uuid
                      AND COALESCE(analise, FALSE) = FALSE
                      AND analise_started_at IS NULL
                    RETURNING key
                    """,
                    (login, audit, key),
                )
                row = cur.fetchone()
            pg.commit()
            if row:
                _refresh_area_cache_row(conn, key)
                from .crm_statistics import log_statistic

                log_statistic(
                    conn,
                    login=login,
                    object_type="order",
                    action="order_analise_started",
                    object_key=key,
                    skip_if_exists=False,
                )
            return "updated" if row else "not_found"
        except Exception as exc:
            _pg_rollback(pg)
            log_warning(f"start_area_analise {key}: {exc}")
            return "not_found"

    if paused_at is not None:
        if started_by != login:
            return "conflict"
        audit = make_user_audit(login)
        _pg_recover_transaction(pg)
        try:
            with pg.cursor() as cur:
                cur.execute(
                    """
                    UPDATE crm.tasks_area SET
                        analise_paused_by = NULL,
                        analise_paused_at = NULL,
                        user_last_edit = %s::text[]
                    WHERE key = %s::uuid
                      AND COALESCE(analise, FALSE) = FALSE
                      AND analise_paused_at IS NOT NULL
                      AND analise_started_by = %s
                    RETURNING key
                    """,
                    (audit, key, login),
                )
                row = cur.fetchone()
            pg.commit()
            if row:
                _refresh_area_cache_row(conn, key)
                from .crm_statistics import log_statistic

                log_statistic(
                    conn,
                    login=login,
                    object_type="order",
                    action="order_analise_started",
                    object_key=key,
                    metadata={"resumed": True},
                    skip_if_exists=False,
                )
            return "updated" if row else "not_found"
        except Exception as exc:
            _pg_rollback(pg)
            log_warning(f"resume_area_analise {key}: {exc}")
            return "not_found"

    if started_by == login:
        return "skipped"
    return "conflict"


def pause_area_analise(
    conn: DatabaseConnection, key: str, login: str
) -> AnaliseTransitionResult:
    ensure_tasks_area_audit_columns(conn)
    ensure_analise_audit_columns(conn)
    login = login.strip()
    audit = make_user_audit(login)
    pg = _pg_connection(conn)
    if pg is None:
        return "not_found"

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                UPDATE crm.tasks_area SET
                    analise_paused_by = %s,
                    analise_paused_at = NOW(),
                    user_last_edit = %s::text[]
                WHERE key = %s::uuid
                  AND COALESCE(analise, FALSE) = FALSE
                  AND analise_started_at IS NOT NULL
                  AND analise_paused_at IS NULL
                  AND analise_started_by = %s
                RETURNING key
                """,
                (login, audit, key, login),
            )
            row = cur.fetchone()
        pg.commit()
        if row:
            _refresh_area_cache_row(conn, key)
            from .crm_statistics import log_statistic

            log_statistic(
                conn,
                login=login,
                object_type="order",
                action="order_analise_paused",
                object_key=key,
                skip_if_exists=False,
            )
            return "updated"
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"pause_area_analise {key}: {exc}")

    state = _fetch_analise_state(conn, key)
    if state is None:
        return "not_found"
    if state.get("analise") is True:
        return "skipped"
    if state.get("analise_paused_at") is not None:
        return "skipped"
    return "not_found"


def analise_lock_holder(conn: DatabaseConnection, key: str) -> Optional[str]:
    state = _fetch_analise_state(conn, key)
    if state is None:
        return None
    if state.get("analise") is True:
        return None
    if state.get("analise_started_at") is None:
        return None
    if state.get("analise_paused_at") is not None:
        holder = (state.get("analise_started_by") or "").strip()
        return holder or None
    holder = (state.get("analise_started_by") or "").strip()
    return holder or None


def complete_area_analise(
    conn: DatabaseConnection, key: str, login: str
) -> AnaliseTransitionResult:
    ensure_tasks_area_audit_columns(conn)
    ensure_analise_audit_columns(conn)
    login = login.strip()
    audit = make_user_audit(login)
    pg = _pg_connection(conn)
    if pg is None:
        return "not_found"

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                UPDATE crm.tasks_area SET
                    analise = TRUE,
                    analise_finished_by = %s,
                    analise_finished_at = NOW(),
                    analise_paused_by = NULL,
                    analise_paused_at = NULL,
                    user_last_edit = %s::text[]
                WHERE key = %s::uuid
                  AND COALESCE(analise, FALSE) = FALSE
                  AND analise_started_by = %s
                  AND analise_started_at IS NOT NULL
                  AND analise_paused_at IS NULL
                RETURNING key
                """,
                (login, audit, key, login),
            )
            row = cur.fetchone()
        pg.commit()
        if row:
            _refresh_area_cache_row(conn, key)
            conn.update_area_row_attrs(
                key, {"analise": True}, normalize=_normalize_area_attrs
            )
            from .crm_statistics import log_statistic

            log_statistic(
                conn,
                login=login,
                object_type="order",
                action="order_analise_completed",
                object_key=key,
            )
            return "updated"
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"complete_area_analise {key}: {exc}")

    state = _fetch_analise_state(conn, key)
    if state is None:
        return "not_found"
    if state.get("analise") is True:
        return "skipped"
    return "not_found"


def _transition_area_status(
    conn: DatabaseConnection,
    key: str,
    *,
    login: str,
    from_status: Optional[str],
    to_status: str,
    skip_if: Optional[str] = None,
) -> AreaTransitionResult:
    pg = _pg_connection(conn)
    if pg is None:
        return "not_found"

    ensure_tasks_area_audit_columns(conn)
    audit = make_user_audit(login)

    if from_status is None:
        where = "key = %s::uuid AND COALESCE(status, '') <> %s"
        params: tuple = (
            to_status,
            audit,
            audit,
            key,
            skip_if or to_status,
        )
        sql = f"""
            UPDATE crm.tasks_area SET
                status = %s,
                user_last_edit = %s::text[],
                user_created = COALESCE(user_created, %s::text[])
            WHERE {where}
            RETURNING key
        """
    else:
        where = "key = %s::uuid AND status = %s"
        params = (to_status, audit, audit, key, from_status)
        sql = f"""
            UPDATE crm.tasks_area SET
                status = %s,
                user_last_edit = %s::text[],
                user_created = COALESCE(user_created, %s::text[])
            WHERE {where}
            RETURNING key
        """

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        pg.commit()
        if row:
            conn.update_area_row_attrs(
                key, {"status": to_status}, normalize=_normalize_area_attrs
            )
            _log_area_status_change(
                conn,
                key=key,
                login=login,
                from_status=from_status,
                to_status=to_status,
            )
            return "updated"

        with pg.cursor() as cur:
            cur.execute(
                "SELECT status FROM crm.tasks_area WHERE key = %s::uuid",
                (key,),
            )
            existing = cur.fetchone()
        pg.commit()
        if not existing:
            return "not_found"
        if skip_if and existing[0] == skip_if:
            return "skipped"
        if from_status and existing[0] == from_status:
            return "skipped"
        return "not_found"
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось обновить статус tasks_area {key}: {exc}")
        return "not_found"

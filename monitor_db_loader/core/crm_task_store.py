# -*- coding: utf-8 -*-
"""Сохранение задач CRM в PostgreSQL (crm.tasks)."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from .crm_ui_constants import FIELD_DATA_SUBGROUP, OFFICE_DATA_SUBGROUP
from .db import CrmSessionCache, DatabaseConnection
from .log_util import log_info, log_warning

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore

TASK_ID_COLUMNS = (
    "photo_uuid",
    "photo_lens",
    "ogh_id",
    "oati_id",
    "earthwork_id",
    "localwork_id",
    "avr_mos_id",
)

_SNAPSHOT_STAT_ACTIONS: Dict[str, str] = {
    "field_table": "task_sent_to_field",
    "done_legal_table": "task_closed_legal",
    "done_illegal_table": "task_closed_illegal",
    "clear_table": "task_marked_clear",
}

CRM_GROUP_DISRUPTIONS = "Разрытия"
CRM_GROUP_ORDERS = "Новые ордера ОАТИ, АВР и земляные работы"

LINK_COLUMNS_BY_GROUP = {
    CRM_GROUP_DISRUPTIONS: (
        "oati_id",
        "earthwork_id",
        "localwork_id",
        "avr_mos_id",
    ),
    CRM_GROUP_ORDERS: ("photo_uuid", "photo_lens", "ogh_id"),
}

STATION_COLUMNS = ("sps", "kgs", "station_avr")

TASK_COLUMN_LABELS = {
    "key": "Ключ задачи",
    "type": "Тип",
    "field_observed": "Обследовано в поле",
    "is_field_data": "Полевые данные",
    "is_office_task": "Камеральный анализ",
    "photo_uuid": "Фото ИИ (uuid)",
    "photo_lens": "Фото Объектив (external_report_id)",
    "ogh_id": "ОГХ (id)",
    "oati_id": "ОАТИ (scoped id)",
    "earthwork_id": "Земляные работы (scoped id)",
    "localwork_id": "Локальные ремонты (scoped id)",
    "avr_mos_id": "АВР (scoped id)",
    "sps": "СПС",
    "kgs": "КГС",
    "station_avr": "АВР",
}

TASK_FORM_FIELDS = ("type",) + TASK_ID_COLUMNS

USER_AUDIT_COLUMNS = ("user_created", "user_last_edit")

_audit_columns_ready: Set[str] = set()


def user_audit_migration_statements(schema: str, table: str) -> Tuple[str, ...]:
    return tuple(
        f'ALTER TABLE "{schema}"."{table}" '
        f'ADD COLUMN IF NOT EXISTS "{col}" TEXT[]'
        for col in USER_AUDIT_COLUMNS
    )


def make_user_audit(login: str) -> List[str]:
    login = (login or "").strip()
    stamp = datetime.now(timezone.utc).isoformat()
    return [login, stamp]


def make_user_last_edit(login: str) -> List[str]:
    """Alias for make_user_audit (backward compatibility)."""
    return make_user_audit(login)


def _upgrade_user_audit_columns(cur, schema: str, table: str) -> None:
    for col in USER_AUDIT_COLUMNS:
        cur.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
            """,
            (schema, table, col),
        )
        row = cur.fetchone()
        if row is None or row[0] != "text":
            continue
        cur.execute(
            f'ALTER TABLE "{schema}"."{table}" ALTER COLUMN "{col}" TYPE TEXT[] '
            f'USING CASE WHEN "{col}" IS NULL THEN NULL::text[] '
            f'ELSE ARRAY["{col}"::text, (now() AT TIME ZONE \'utc\')::text] END'
        )


def ensure_user_audit_columns(pg, schema: str, table: str) -> None:
    key = f"{schema}.{table}"
    if key in _audit_columns_ready:
        return
    with pg.cursor() as cur:
        _pg_set_admin_timeouts(cur)
        for stmt in user_audit_migration_statements(schema, table):
            cur.execute(stmt)
        _upgrade_user_audit_columns(cur, schema, table)
    _audit_columns_ready.add(key)


_DDL_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS crm",
    """
    CREATE TABLE IF NOT EXISTS crm.tasks (
        key UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        type TEXT NOT NULL,
        photo_uuid TEXT,
        photo_lens TEXT,
        ogh_id TEXT,
        oati_id TEXT,
        earthwork_id TEXT,
        localwork_id TEXT,
        avr_mos_id TEXT,
        sps TEXT,
        kgs TEXT,
        station_avr TEXT
    )
    """,
)

_CREATE_TASK_ID_UNIQUE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_photo_uuid "
    "ON crm.tasks (photo_uuid) WHERE photo_uuid IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_photo_lens "
    "ON crm.tasks (photo_lens) WHERE photo_lens IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_ogh_id "
    "ON crm.tasks (ogh_id) WHERE ogh_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_oati_id "
    "ON crm.tasks (oati_id) WHERE oati_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_earthwork_id "
    "ON crm.tasks (earthwork_id) WHERE earthwork_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_localwork_id "
    "ON crm.tasks (localwork_id) WHERE localwork_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_avr_mos_id "
    "ON crm.tasks (avr_mos_id) WHERE avr_mos_id IS NOT NULL",
)


_tasks_unique_indexes_ready: Set[str] = set()


def _pg_set_admin_timeouts(cur) -> None:
    cur.execute("SET LOCAL statement_timeout = '120000'")
    cur.execute("SET LOCAL lock_timeout = '15000'")


def _station_migration_statements(schema: str, table: str) -> Tuple[str, ...]:
    return tuple(
        f'ALTER TABLE "{schema}"."{table}" '
        f'ADD COLUMN IF NOT EXISTS "{col}" TEXT'
        for col in STATION_COLUMNS
    ) + (
        f'ALTER TABLE "{schema}"."{table}" '
        f"ADD COLUMN IF NOT EXISTS field_observed BOOLEAN",
    )


_TASK_SELECT_COLUMNS = ("key", "type") + TASK_ID_COLUMNS + STATION_COLUMNS + (
    "field_observed",
    "is_field_data",
    "is_office_task",
)


def _snapshot_ddl_statements(
    schema: str,
    tasks_table: str,
    snapshot_table: str,
    *,
    with_fk: bool = True,
) -> Tuple[str, ...]:
    index_name = f"{snapshot_table}_uq_task_key"
    if with_fk:
        task_key_def = (
            f'task_key UUID NOT NULL REFERENCES "{schema}"."{tasks_table}"(key)'
        )
    else:
        task_key_def = "task_key UUID NOT NULL"
    return (
        "CREATE SCHEMA IF NOT EXISTS crm",
        f"""
        CREATE TABLE IF NOT EXISTS "{schema}"."{snapshot_table}" (
            key UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            {task_key_def},
            type TEXT NOT NULL,
            photo_uuid TEXT,
            photo_lens TEXT,
            ogh_id TEXT,
            oati_id TEXT,
            earthwork_id TEXT,
            localwork_id TEXT,
            avr_mos_id TEXT,
            sps TEXT,
            kgs TEXT,
            station_avr TEXT,
            sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
            ON "{schema}"."{snapshot_table}" (task_key)
        """,
    )


def _snapshot_migration_statements(schema: str, table: str) -> Tuple[str, ...]:
    return _station_migration_statements(schema, table) + (
        f'ALTER TABLE "{schema}"."{table}" '
        f"ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    )


def _tasks_field_migration_statements(schema: str, table: str) -> Tuple[str, ...]:
    return (
        f'ALTER TABLE "{schema}"."{table}" '
        f"ADD COLUMN IF NOT EXISTS office_comment TEXT",
        f'ALTER TABLE "{schema}"."{table}" '
        f"ADD COLUMN IF NOT EXISTS rayon TEXT",
    )


def _normalize_office_comment(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


SendTaskSnapshotResult = Literal["inserted", "skipped"]
# Backward-compatible alias
SendToFieldResult = SendTaskSnapshotResult


    key: str
    type: str
    photo_uuid: Optional[str] = None
    photo_lens: Optional[str] = None
    ogh_id: Optional[str] = None
    oati_id: Optional[str] = None
    earthwork_id: Optional[str] = None
    localwork_id: Optional[str] = None
    avr_mos_id: Optional[str] = None
    sps: Optional[str] = None
    kgs: Optional[str] = None
    station_avr: Optional[str] = None
    field_observed: Optional[bool] = None
    is_field_data: Optional[bool] = None
    is_office_task: Optional[bool] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "type": self.type,
            "photo_uuid": self.photo_uuid,
            "photo_lens": self.photo_lens,
            "ogh_id": self.ogh_id,
            "oati_id": self.oati_id,
            "earthwork_id": self.earthwork_id,
            "localwork_id": self.localwork_id,
            "avr_mos_id": self.avr_mos_id,
            "sps": self.sps,
            "kgs": self.kgs,
            "station_avr": self.station_avr,
            "field_observed": self.field_observed,
            "is_field_data": self.is_field_data,
            "is_office_task": self.is_office_task,
        }

    @classmethod
    def from_row(cls, row: Tuple) -> "TaskRecord":
        field_observed = None
        if len(row) > 12 and row[12] is not None:
            field_observed = bool(row[12])
        is_field_data = None
        if len(row) > 13 and row[13] is not None:
            is_field_data = bool(row[13])
        is_office_task = None
        if len(row) > 14 and row[14] is not None:
            is_office_task = bool(row[14])
        return cls(
            key=str(row[0]),
            type=row[1] or "",
            photo_uuid=_normalize_id_value(row[2]),
            photo_lens=_normalize_id_value(row[3]),
            ogh_id=_normalize_id_value(row[4]),
            oati_id=_normalize_id_value(row[5]),
            earthwork_id=_normalize_id_value(row[6]),
            localwork_id=_normalize_id_value(row[7]),
            avr_mos_id=_normalize_id_value(row[8]),
            sps=_normalize_id_value(row[9]) if len(row) > 9 else None,
            kgs=_normalize_id_value(row[10]) if len(row) > 10 else None,
            station_avr=_normalize_id_value(row[11]) if len(row) > 11 else None,
            field_observed=field_observed,
            is_field_data=is_field_data,
            is_office_task=is_office_task,
        )


def _pg_connection(conn: DatabaseConnection):
    if psycopg2 is None:
        return None
    return conn._get_pg_connection()


def _pg_rollback(pg) -> None:
    if pg is None:
        return
    try:
        pg.rollback()
    except Exception:
        pass


def _pg_recover_transaction(pg) -> None:
    """Сбросить прерванную транзакцию (InFailedSqlTransaction) перед новым запросом."""
    if pg is None or psycopg2 is None:
        return
    try:
        from psycopg2.extensions import TRANSACTION_STATUS_INERROR

        if pg.get_transaction_status() == TRANSACTION_STATUS_INERROR:
            pg.rollback()
    except Exception:
        _pg_rollback(pg)


def _schema_cache(conn: DatabaseConnection) -> Set[str]:
    return conn._crm_schema_ready


_last_snapshot_ensure_error: Optional[str] = None
_last_tasks_ensure_error: Optional[str] = None


def _table_exists(pg, schema: str, table: str) -> bool:
    return _relation_kind(pg, schema, table) == "r"


def _relation_kind(pg, schema: str, table: str) -> Optional[str]:
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT c.relkind
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                """,
                (schema, table),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


_TASKS_LOOKUP_INDEXES = tuple(
    f"""
    CREATE INDEX IF NOT EXISTS idx_tasks_{col}
        ON crm.tasks ("{col}") WHERE "{col}" IS NOT NULL
    """
    for col in TASK_ID_COLUMNS
)

_tasks_lookup_indexes_ready: Set[str] = set()


def ensure_tasks_unique_indexes(conn: DatabaseConnection) -> bool:
    pg = _pg_connection(conn)
    if pg is None:
        return False
    key = "crm.tasks.unique_indexes"
    if key in _tasks_unique_indexes_ready:
        return True
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            _pg_set_admin_timeouts(cur)
            for stmt in _CREATE_TASK_ID_UNIQUE_INDEXES:
                cur.execute(stmt)
        pg.commit()
        _tasks_unique_indexes_ready.add(key)
        return True
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось создать unique-индексы crm.tasks: {exc}")
        return False


def ensure_tasks_lookup_indexes(conn: DatabaseConnection) -> bool:
    pg = _pg_connection(conn)
    if pg is None:
        return False
    key = "crm.tasks.lookup_indexes"
    if key in _tasks_lookup_indexes_ready:
        return True
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            for stmt in _TASKS_LOOKUP_INDEXES:
                cur.execute(stmt)
        pg.commit()
        _tasks_lookup_indexes_ready.add(key)
        return True
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось создать индексы lookup crm.tasks: {exc}")
        return False


def ensure_tasks_table(conn: DatabaseConnection) -> bool:
    """Создать схему crm и таблицу tasks при отсутствии."""
    global _last_tasks_ensure_error
    _last_tasks_ensure_error = None

    cache = _schema_cache(conn)
    pg = _pg_connection(conn)
    if pg is None:
        _last_tasks_ensure_error = "psycopg2 недоступен"
        log_warning("psycopg2 недоступен — запись задач в БД невозможна")
        return False

    schema, table = "crm", "tasks"
    cache_key = f"{schema}.{table}"
    if cache_key in cache:
        _pg_recover_transaction(pg)
        if _table_exists(pg, schema, table):
            try:
                ensure_user_audit_columns(pg, schema, table)
                pg.commit()
            except Exception:
                _pg_rollback(pg)
            ensure_tasks_unique_indexes(conn)
            return True
        cache.discard(cache_key)

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            _pg_set_admin_timeouts(cur)
            for stmt in _DDL_STATEMENTS:
                cur.execute(stmt)
            for stmt in _station_migration_statements(schema, table):
                cur.execute(stmt)
            ensure_user_audit_columns(pg, schema, table)
        pg.commit()
        cache.add(cache_key)
        ensure_tasks_unique_indexes(conn)
        ensure_tasks_lookup_indexes(conn)
        log_info("Таблица crm.tasks проверена/создана")
        return True
    except Exception as exc:
        _pg_rollback(pg)
        _last_tasks_ensure_error = str(exc)
        log_warning(f"Не удалось создать crm.tasks: {exc}")
        return False


def _apply_snapshot_migrations(
    pg,
    snapshot_schema: str,
    snapshot_table: str,
    *,
    default_table: str = "",
) -> None:
    with pg.cursor() as cur:
        _pg_set_admin_timeouts(cur)
        for stmt in _snapshot_migration_statements(snapshot_schema, snapshot_table):
            cur.execute(stmt)
        if default_table == "tasks_field":
            for stmt in _tasks_field_migration_statements(
                snapshot_schema, snapshot_table
            ):
                cur.execute(stmt)
    ensure_user_audit_columns(pg, snapshot_schema, snapshot_table)


def _create_snapshot_table(
    pg,
    schema: str,
    tasks_table: str,
    snapshot_schema: str,
    snapshot_table: str,
    *,
    default_table: str = "",
) -> bool:
    global _last_snapshot_ensure_error
    last_error: Optional[str] = None

    for with_fk in (True, False):
        _pg_recover_transaction(pg)
        try:
            with pg.cursor() as cur:
                _pg_set_admin_timeouts(cur)
                for stmt in _snapshot_ddl_statements(
                    schema, tasks_table, snapshot_table, with_fk=with_fk
                ):
                    cur.execute(stmt)
                for stmt in _snapshot_migration_statements(
                    snapshot_schema, snapshot_table
                ):
                    cur.execute(stmt)
                if default_table == "tasks_field":
                    for stmt in _tasks_field_migration_statements(
                        snapshot_schema, snapshot_table
                    ):
                        cur.execute(stmt)
            pg.commit()
            if not with_fk:
                log_warning(
                    f"Таблица {snapshot_schema}.{snapshot_table} создана без FK "
                    f"на {schema}.{tasks_table}(key)"
                )
            return True
        except Exception as exc:
            _pg_rollback(pg)
            last_error = str(exc)
            if with_fk:
                log_warning(
                    f"DDL {snapshot_schema}.{snapshot_table} с FK не удался: {exc}; "
                    f"повтор без FK"
                )
                continue
            _last_snapshot_ensure_error = last_error
            return False
    _last_snapshot_ensure_error = last_error or "неизвестная ошибка"
    return False


def _normalize_id_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_SCOPED_GEOMETRY_PREFIXES = frozenset({"point", "line", "polygon"})


def format_scoped_business_id(geometry_type: str, raw_id: Any) -> Optional[str]:
    normalized = _normalize_id_value(raw_id)
    if normalized is None:
        return None
    if ":" in normalized:
        prefix = normalized.split(":", 1)[0]
        if prefix in _SCOPED_GEOMETRY_PREFIXES:
            return normalized
    return f"{geometry_type}:{normalized}"


def parse_scoped_business_id(business_id: str) -> Tuple[Optional[str], str]:
    if ":" in business_id:
        prefix, raw = business_id.split(":", 1)
        if prefix in _SCOPED_GEOMETRY_PREFIXES:
            return prefix, raw
    return None, business_id


def _geom_hash_expr(geom_col: str = "geom") -> str:
    return f"md5(ST_AsEWKB(ST_SetSRID(ST_MakeValid({geom_col}), 4326)))"


_ITEMS_LINK_TABLE_RE = re.compile(
    r"^data_mos\.items_\d+_(points|lines|polygons)$"
)


def _is_data_mos_items_table(qualified_table: Optional[str]) -> bool:
    return bool(qualified_table and _ITEMS_LINK_TABLE_RE.match(qualified_table))


def _coerce_items_row_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or ":" in text:
        return None
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def _resolve_items_row_id(
    attributes: Dict[str, Any],
    mapping: Dict[str, Any],
    business_id: str,
) -> Optional[int]:
    """Numeric items_* row id for scoped geometry tasks only."""
    if not mapping.get("scoped_geometry_id"):
        return None
    source_field = mapping.get("source_field", "id")
    row_id = _coerce_items_row_id(attributes.get(source_field))
    if row_id is not None:
        return row_id
    _, raw_business_id = parse_scoped_business_id(str(business_id))
    return _coerce_items_row_id(raw_business_id)


def find_task_by_source_anchor(
    conn: DatabaseConnection,
    global_id: Any,
    geom_hash: str,
    task_column: str,
) -> Optional[str]:
    if global_id is None or not geom_hash or task_column not in TASK_ID_COLUMNS:
        return None
    pg = _pg_connection(conn)
    if pg is None:
        return None
    query = f"""
        SELECT key::text
        FROM crm.tasks
        WHERE source_global_id = %s
          AND source_geom_hash = %s
          AND "{task_column}" IS NOT NULL
        LIMIT 1
    """
    with pg.cursor() as cur:
        cur.execute(query, (global_id, geom_hash))
        row = cur.fetchone()
    return str(row[0]) if row else None


def _parent_table_from_split(items_table: str) -> Optional[str]:
    for suffix in ("_points", "_lines", "_polygons"):
        if items_table.endswith(suffix):
            return items_table[: -len(suffix)]
    return None


def link_items_task_key(
    conn: DatabaseConnection,
    task_key: str,
    qualified_table: str,
    row_id: Any,
    *,
    geom_col: str = "geom",
    global_id: Any = None,
    task_column: Optional[str] = None,
    geometry_type: Optional[str] = None,
) -> bool:
    """Write task_key on items row and source anchor on crm.tasks."""
    if not _is_data_mos_items_table(qualified_table):
        return False
    row_id_int = _coerce_items_row_id(row_id)
    if row_id_int is None:
        return False
    pg = _pg_connection(conn)
    if pg is None:
        return False
    with pg.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {qualified_table}
            SET task_key = %s::uuid
            WHERE id = %s
              AND (task_key IS NULL OR task_key = %s::uuid)
            """,
            (task_key, row_id_int, task_key),
        )
        if cur.rowcount == 0:
            return False
        anchor_sets = ["source_table = %s", "source_row_id = %s"]
        anchor_params: List[Any] = [qualified_table, row_id_int]
        if global_id is not None:
            anchor_sets.append("source_global_id = %s")
            anchor_params.append(global_id)
        anchor_sets.append(
            f"source_geom_hash = {_geom_hash_expr(f't.\"{geom_col}\"')}"
        )
        anchor_params.extend([task_key, row_id_int])
        cur.execute(
            f"""
            UPDATE crm.tasks ct
            SET {", ".join(anchor_sets)}
            FROM {qualified_table} t
            WHERE ct.key = %s::uuid AND t.id = %s
            """,
            anchor_params,
        )
        if task_column and geometry_type:
            business_id = format_scoped_business_id(geometry_type, row_id_int)
            if business_id:
                cur.execute(
                    f"""
                    UPDATE crm.tasks
                    SET "{task_column}" = %s
                    WHERE key = %s::uuid
                      AND ("{task_column}" IS NULL OR "{task_column}" <> %s)
                    """,
                    (business_id, task_key, business_id),
                )
        parent_table = _parent_table_from_split(qualified_table)
        if parent_table:
            cur.execute(
                f"""
                UPDATE {parent_table} p
                SET tasked = true
                FROM {qualified_table} t
                WHERE t.id = %s
                  AND p.id = t.source_id
                  AND t.source_id IS NOT NULL
                """,
                (row_id_int,),
            )
    pg.commit()
    return True


    """point | line | polygon из QgsVectorLayer."""
    if layer is None:
        return None
    from qgis.core import QgsWkbTypes

    gtype = layer.geometryType()
    if gtype == QgsWkbTypes.PointGeometry:
        return "point"
    if gtype == QgsWkbTypes.LineGeometry:
        return "line"
    if gtype == QgsWkbTypes.PolygonGeometry:
        return "polygon"
    return None


def task_row_from_feature(
    group_name: str,
    subgroup_name: str,
    attributes: Dict[str, Any],
    store_cfg: Dict[str, Any],
    *,
    geometry_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    subgroups_cfg = store_cfg.get("subgroups", {})
    mapping = subgroups_cfg.get(subgroup_name)
    if not mapping:
        log_warning(f"Нет маппинга task_store для подгруппы «{subgroup_name}»")
        return None

    task_column = mapping.get("task_column")
    source_field = mapping.get("source_field")
    if task_column not in TASK_ID_COLUMNS or not source_field:
        log_warning(
            f"Некорректный маппинг task_store для «{subgroup_name}»: "
            f"{mapping}"
        )
        return None

    business_id = _normalize_id_value(attributes.get(source_field))
    if business_id is None:
        log_warning(
            f"Пропуск объекта «{subgroup_name}»: пустое поле «{source_field}»"
        )
        return None
    if mapping.get("scoped_geometry_id"):
        if not geometry_type:
            log_warning(
                f"Пропуск объекта «{subgroup_name}»: scoped_geometry_id "
                f"без типа геометрии слоя"
            )
            return None
        business_id = format_scoped_business_id(geometry_type, business_id)
        if business_id is None:
            return None

    row = {
        "type": group_name,
        "photo_uuid": None,
        "photo_lens": None,
        "ogh_id": None,
        "oati_id": None,
        "earthwork_id": None,
        "localwork_id": None,
        "avr_mos_id": None,
    }
    row[task_column] = business_id
    return row


def resolve_task_lookup(
    subgroup_name: str,
    attributes: Dict[str, Any],
    store_cfg: Dict[str, Any],
    *,
    geometry_type: Optional[str] = None,
    layer: Any = None,
) -> Optional[Tuple[str, str]]:
    """Вернуть (task_column, business_id) для поиска строки в crm.tasks."""
    if geometry_type is None and layer is not None:
        geometry_type = layer_geometry_type(layer)
    row = task_row_from_feature(
        "",
        subgroup_name,
        attributes,
        store_cfg,
        geometry_type=geometry_type,
    )
    if row is None:
        return None
    task_column = next(col for col in TASK_ID_COLUMNS if row[col] is not None)
    return task_column, row[task_column]


def _table_ref(store_cfg: Dict[str, Any]) -> Tuple[str, str]:
    return store_cfg.get("schema", "crm"), store_cfg.get("table", "tasks")


def _snapshot_table_ref(
    store_cfg: Dict[str, Any], config_key: str, default_table: str
) -> Tuple[str, str]:
    schema = store_cfg.get("schema", "crm")
    table = store_cfg.get(config_key, default_table)
    return schema, table


def _field_table_ref(store_cfg: Dict[str, Any]) -> Tuple[str, str]:
    return _snapshot_table_ref(store_cfg, "field_table", "tasks_field")


def ensure_task_snapshot_table(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    config_key: str,
    default_table: str,
) -> bool:
    """Создать таблицу-снимок задач при отсутствии."""
    global _last_snapshot_ensure_error
    _last_snapshot_ensure_error = None

    if not ensure_tasks_table(conn):
        _last_snapshot_ensure_error = (
            _last_tasks_ensure_error or "crm.tasks недоступна"
        )
        return False

    schema, tasks_table = _table_ref(store_cfg)
    snapshot_schema, snapshot_table = _snapshot_table_ref(
        store_cfg, config_key, default_table
    )
    cache_key = f"{snapshot_schema}.{snapshot_table}"
    cache = _schema_cache(conn)
    pg = _pg_connection(conn)
    if pg is None:
        _last_snapshot_ensure_error = "psycopg2 недоступен"
        log_warning(f"psycopg2 недоступен — запись в {snapshot_table} невозможна")
        return False

    if cache_key in cache:
        _pg_recover_transaction(pg)
        if _table_exists(pg, snapshot_schema, snapshot_table):
            return True
        cache.discard(cache_key)

    _pg_recover_transaction(pg)
    rel_kind = _relation_kind(pg, snapshot_schema, snapshot_table)
    if rel_kind is not None and rel_kind != "r":
        _last_snapshot_ensure_error = (
            f"{snapshot_schema}.{snapshot_table} существует, но это не таблица"
        )
        log_warning(_last_snapshot_ensure_error)
        return False

    if _table_exists(pg, snapshot_schema, snapshot_table):
        try:
            _apply_snapshot_migrations(
                pg, snapshot_schema, snapshot_table, default_table=default_table
            )
            pg.commit()
            cache.add(cache_key)
            log_info(f"Таблица {snapshot_schema}.{snapshot_table} проверена/создана")
            return True
        except Exception as exc:
            _pg_rollback(pg)
            _last_snapshot_ensure_error = str(exc)
            log_warning(
                f"Миграция {snapshot_schema}.{snapshot_table} не удалась: {exc}"
            )
            return False

    if not _create_snapshot_table(
        pg,
        schema,
        tasks_table,
        snapshot_schema,
        snapshot_table,
        default_table=default_table,
    ):
        log_warning(
            f"Не удалось создать {snapshot_schema}.{snapshot_table}: "
            f"{_last_snapshot_ensure_error}"
        )
        return False

    cache.add(cache_key)
    log_info(f"Таблица {snapshot_schema}.{snapshot_table} проверена/создана")
    return True


def ensure_tasks_field_table(
    conn: DatabaseConnection, store_cfg: Dict[str, Any]
) -> bool:
    return ensure_task_snapshot_table(
        conn, store_cfg, "field_table", "tasks_field"
    )


_SNAPSHOT_TABLES = (
    ("field_table", "tasks_field"),
    ("done_legal_table", "tasks_done_legal"),
    ("done_illegal_table", "tasks_done_illegal"),
    ("clear_table", "tasks_clear"),
)


def task_key_exists_in_snapshot(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    config_key: str,
    default_table: str,
    task_key: str,
) -> bool:
    schema, table = _snapshot_table_ref(store_cfg, config_key, default_table)
    pg = _pg_connection(conn)
    if pg is None:
        return False

    query = f'SELECT 1 FROM "{schema}"."{table}" WHERE task_key = %s LIMIT 1'
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (task_key,))
            return cur.fetchone() is not None
    except Exception:
        _pg_rollback(pg)
        return False


def task_key_exists_in_field(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    task_key: str,
) -> bool:
    return task_key_exists_in_snapshot(
        conn, store_cfg, "field_table", "tasks_field", task_key
    )


def ensure_all_snapshot_tables(
    conn: DatabaseConnection, store_cfg: Dict[str, Any]
) -> bool:
    """Подготовить crm.tasks и все snapshot-таблицы (один раз за сессию)."""
    if not ensure_tasks_table(conn):
        return False
    ok = True
    for config_key, default_table in _SNAPSHOT_TABLES:
        if not ensure_task_snapshot_table(
            conn, store_cfg, config_key, default_table
        ):
            ok = False
    return ok


def fetch_snapshot_task_keys(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> Set[str]:
    """Все task_key из tasks_field, tasks_done_*, tasks_clear."""
    keys: Set[str] = set()
    pg = _pg_connection(conn)
    if pg is None:
        return keys

    _pg_recover_transaction(pg)

    for config_key, default_table in _SNAPSHOT_TABLES:
        schema, table = _snapshot_table_ref(store_cfg, config_key, default_table)
        query = f'SELECT task_key FROM "{schema}"."{table}"'
        try:
            with pg.cursor() as cur:
                cur.execute(query)
                for row in cur.fetchall():
                    if row[0]:
                        keys.add(str(row[0]))
        except Exception as exc:
            _pg_rollback(pg)
            log_warning(
                f"Не удалось загрузить task_key из {schema}.{table}: {exc}"
            )
            continue
    try:
        pg.commit()
    except Exception:
        _pg_rollback(pg)
    return keys


def fetch_task_keys_index(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> Dict[Tuple[str, str], str]:
    """Соответствие (task_column, business_id) → key в crm.tasks."""
    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        return {}

    col_list = ", ".join(f'"{col}"' for col in ("key",) + TASK_ID_COLUMNS)
    query = f'SELECT {col_list} FROM "{schema}"."{table}"'
    index: Dict[Tuple[str, str], str] = {}
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query)
            for row in cur.fetchall():
                key = str(row[0])
                for col_index, col in enumerate(TASK_ID_COLUMNS, start=1):
                    value = _normalize_id_value(row[col_index])
                    if value:
                        index[(col, value)] = key
        pg.commit()
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить индекс crm.tasks: {exc}")
    return index


def _tasks_select_sql(store_cfg: Dict[str, Any]) -> str:
    schema, table = _table_ref(store_cfg)
    col_list = ", ".join(f'"{col}"' for col in _TASK_SELECT_COLUMNS)
    return f'SELECT {col_list} FROM "{schema}"."{table}"'


def fetch_all_field_observed(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> Dict[str, Optional[bool]]:
    """Все field_observed из crm.tasks (key → bool|None)."""
    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        return {}

    query = f'SELECT key::text, field_observed FROM "{schema}"."{table}"'
    result: Dict[str, Optional[bool]] = {}
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query)
            for row in cur.fetchall():
                key = str(row[0])
                result[key] = bool(row[1]) if row[1] is not None else None
        pg.commit()
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить field_observed из crm.tasks: {exc}")
    return result


def fetch_field_observed_by_keys(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    keys: Set[str],
) -> Dict[str, Optional[bool]]:
    """field_observed из crm.tasks по списку key (для snapshot и активных задач)."""
    if not keys:
        return {}

    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        return {}

    key_list = [k for k in keys if k]
    if not key_list:
        return {}

    placeholders = ", ".join(["%s::uuid"] * len(key_list))
    query = (
        f'SELECT key::text, field_observed FROM "{schema}"."{table}" '
        f"WHERE key IN ({placeholders})"
    )
    result: Dict[str, Optional[bool]] = {}
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, tuple(key_list))
            for row in cur.fetchall():
                key = str(row[0])
                result[key] = bool(row[1]) if row[1] is not None else None
        pg.commit()
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить field_observed из crm.tasks: {exc}")
    return result


def load_crm_session_cache(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> CrmSessionCache:
    """Загрузить индекс crm.tasks и ключи snapshot-таблиц (2 SQL-запроса)."""
    import time

    from .log_util import log_timing

    t0 = time.perf_counter()
    cache = CrmSessionCache()
    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        conn.set_crm_session_cache(cache)
        return cache

    col_list = ", ".join(
        f'"{col}"' for col in ("key",) + TASK_ID_COLUMNS + ("field_observed",)
    )
    tasks_query = f'SELECT {col_list} FROM "{schema}"."{table}"'

    union_parts: List[str] = []
    for config_key, default_table in _SNAPSHOT_TABLES:
        snap_schema, snap_table = _snapshot_table_ref(
            store_cfg, config_key, default_table
        )
        union_parts.append(
            f'SELECT task_key::text FROM "{snap_schema}"."{snap_table}"'
        )
    snapshots_query = " UNION ALL ".join(union_parts)

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(tasks_query)
            for row in cur.fetchall():
                key = str(row[0])
                for col_index, col in enumerate(TASK_ID_COLUMNS, start=1):
                    value = _normalize_id_value(row[col_index])
                    if value:
                        cache.task_index[(col, value)] = key
                observed = row[len(TASK_ID_COLUMNS) + 1]
                cache.field_observed[key] = (
                    bool(observed) if observed is not None else None
                )
            cur.execute(snapshots_query)
            for row in cur.fetchall():
                if row[0]:
                    cache.snapshot_keys.add(str(row[0]))
        pg.commit()
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить CRM session cache: {exc}")

    conn.set_crm_session_cache(cache)
    log_timing("load_crm_session_cache", (time.perf_counter() - t0) * 1000)
    log_info(
        f"CRM cache: tasks={len(cache.field_observed)}, "
        f"snapshots={len(cache.snapshot_keys)}, "
        f"index={len(cache.task_index)}"
    )
    return cache


def ensure_crm_session_cache(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    *,
    force_reload: bool = False,
) -> CrmSessionCache:
    cache = conn.get_crm_session_cache()
    if cache is not None and not force_reload:
        return cache
    return load_crm_session_cache(conn, store_cfg)


def get_snapshot_task_keys(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> Set[str]:
    cache = conn.get_crm_session_cache()
    if cache is not None:
        return cache.snapshot_keys
    return fetch_snapshot_task_keys(conn, store_cfg)


def enrich_task_result_field_observed(
    task_result,
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> None:
    """Заполнить attributes['field_observed'] и task_key для списков заказов."""
    from .crm_ui_constants import AREA_LAYER_KEY

    cache = ensure_crm_session_cache(conn, store_cfg)
    task_index = cache.task_index
    observed_map = cache.field_observed

    for group in task_result.groups:
        for subgroup in group.subgroups:
            for feat in subgroup.features:
                if feat.layer_key == AREA_LAYER_KEY:
                    continue
                key = feat.task_key
                if not key:
                    lookup = resolve_task_lookup(
                        subgroup.name,
                        feat.attributes,
                        store_cfg,
                        layer=feat.layer,
                    )
                    if lookup:
                        key = task_index.get(lookup)
                        if key:
                            feat.task_key = key
                if not key:
                    continue
                if key in observed_map:
                    feat.attributes["field_observed"] = observed_map[key]


def filter_sent_tasks_from_result(
    task_result,
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> int:
    """Скрыть задачи, уже отправленные в field/done_* таблицы. Возвращает число скрытых."""
    cache = ensure_crm_session_cache(conn, store_cfg)
    snapshot_keys = cache.snapshot_keys
    if not snapshot_keys:
        return 0

    task_index = cache.task_index
    hidden = 0

    for group in task_result.groups:
        for subgroup in group.subgroups:
            kept = []
            for feat in subgroup.features:
                lookup = resolve_task_lookup(
                    subgroup.name,
                    feat.attributes,
                    store_cfg,
                    layer=feat.layer,
                )
                if lookup:
                    task_key = task_index.get(lookup)
                    if task_key and task_key in snapshot_keys:
                        hidden += 1
                        continue
                kept.append(feat)
            subgroup.features = kept

    if hidden:
        log_info(f"crm.tasks: скрыто из списка {hidden} отправленных задач")
    return hidden


def _fetch_geometry_json_by_task_key(
    conn: DatabaseConnection,
    task_key: str,
    store_cfg: Dict[str, Any],
) -> Optional[str]:
    import json

    from .config import load_layers_config

    pg = _pg_connection(conn)
    if pg is None:
        return None
    cfg = load_layers_config()
    crm_cfg = cfg.get("crm_tasks", {})
    for group_cfg in crm_cfg.get("groups", []):
        for sub_cfg in group_cfg.get("subgroups", []):
            subgroup_name = sub_cfg.get("name", "")
            mapping = store_cfg.get("subgroups", {}).get(subgroup_name)
            if not mapping or not mapping.get("scoped_geometry_id"):
                continue
            for layer_ref in sub_cfg.get("layers", []):
                layer_name = layer_ref if isinstance(layer_ref, str) else layer_ref.get("name")
                for lg in cfg.get("layer_groups", []):
                    for grp in lg.get("groups", []):
                        for layer_def in grp.get("layers", []):
                            if layer_def.get("display_name") != layer_name:
                                continue
                            schema_name = layer_def.get("schema", "data_mos")
                            table_name = layer_def.get("table_name")
                            geom_col = layer_def.get("geometry_column", "geom")
                            if not table_name:
                                continue
                            qualified = f"{schema_name}.{table_name}"
                            with pg.cursor() as cur:
                                cur.execute(
                                    f"""
                                    SELECT ST_AsGeoJSON(ST_Transform("{geom_col}", 4326))
                                    FROM {qualified}
                                    WHERE task_key = %s::uuid
                                    LIMIT 1
                                    """,
                                    (task_key,),
                                )
                                row = cur.fetchone()
                            if row and row[0]:
                                return row[0] if isinstance(row[0], str) else json.dumps(row[0])
    return None


def send_task_snapshot(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
    config_key: str,
    default_table: str,
    login: str,
    *,
    office_comment: Optional[str] = None,
    rayon: Optional[str] = None,
) -> SendTaskSnapshotResult:
    """Сохранить снимок задачи в таблицу-снимок (без повторов по task_key)."""
    schema, table = _snapshot_table_ref(store_cfg, config_key, default_table)
    if not ensure_task_snapshot_table(conn, store_cfg, config_key, default_table):
        detail = _last_snapshot_ensure_error or "неизвестная ошибка"
        raise RuntimeError(
            f"Не удалось подготовить таблицу {schema}.{table}: {detail}"
        )

    if task_key_exists_in_snapshot(
        conn, store_cfg, config_key, default_table, record.key
    ):
        return "skipped"

    pg = _pg_connection(conn)
    if pg is None:
        raise RuntimeError("psycopg2 недоступен")

    task_type = (record.type or "").strip()
    if not task_type:
        raise ValueError("Поле «type» не может быть пустым")

    columns = (
        ["task_key", "type"]
        + list(TASK_ID_COLUMNS)
        + list(STATION_COLUMNS)
        + list(USER_AUDIT_COLUMNS)
    )
    values = [record.key, task_type] + [
        _normalize_id_value(getattr(record, col)) for col in TASK_ID_COLUMNS
    ] + [
        _normalize_id_value(getattr(record, col)) for col in STATION_COLUMNS
    ]
    audit = make_user_audit(login)
    values += [audit, audit]
    if config_key == "field_table":
        from .crm_ui_constants import normalize_rayon_name

        columns = list(columns) + ["office_comment", "rayon"]
        values.append(_normalize_office_comment(office_comment))
        values.append(normalize_rayon_name(rayon or "") or None)
    placeholders = ", ".join(["%s"] * len(columns))
    col_list = ", ".join(f'"{col}"' for col in columns)
    query = (
        f'INSERT INTO "{schema}"."{table}" ({col_list}) '
        f"VALUES ({placeholders})"
    )

    try:
        with pg.cursor() as cur:
            cur.execute(query, values)
        pg.commit()
        log_info(f"crm.{table}: отправлена задача {record.key}")
        cache = conn.get_crm_session_cache()
        if cache is not None:
            cache.snapshot_keys.add(str(record.key))
        else:
            conn.invalidate_crm_session_cache()
        if config_key in _SNAPSHOT_STAT_ACTIONS:
            from .crm_statistics import log_statistic

            log_statistic(
                conn,
                login=login,
                object_type="task",
                action=_SNAPSHOT_STAT_ACTIONS[config_key],
                object_key=record.key,
                metadata={"task_type": task_type},
            )
        return "inserted"
    except Exception:
        _pg_rollback(pg)
        raise


def send_task_to_field(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
    login: str,
    *,
    office_comment: Optional[str] = None,
    rayon: Optional[str] = None,
) -> SendTaskSnapshotResult:
    if rayon:
        from .crm_ui_constants import normalize_rayon_name

        rayon_norm = normalize_rayon_name(rayon)
        if not rayon_norm:
            raise ValueError("Район не указан")
    return send_task_snapshot(
        conn,
        record,
        store_cfg,
        "field_table",
        "tasks_field",
        login,
        office_comment=office_comment,
        rayon=rayon,
    )


def fetch_office_comment(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    task_key: str,
) -> Optional[str]:
    schema, table = _field_table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        return None

    query = (
        f'SELECT office_comment FROM "{schema}"."{table}" '
        f"WHERE task_key = %s "
        f"AND office_comment IS NOT NULL AND TRIM(office_comment) <> '' "
        f"LIMIT 1"
    )
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (task_key,))
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return _normalize_office_comment(str(row[0]))
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(
            f"Не удалось загрузить office_comment (key={task_key}): {exc}"
        )
        return None


def send_task_to_done_legal(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
    login: str,
) -> SendTaskSnapshotResult:
    return send_task_snapshot(
        conn, record, store_cfg, "done_legal_table", "tasks_done_legal", login
    )


def send_task_to_done_illegal(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
    login: str,
) -> SendTaskSnapshotResult:
    return send_task_snapshot(
        conn, record, store_cfg, "done_illegal_table", "tasks_done_illegal", login
    )


def send_task_to_clear(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
    login: str,
) -> SendTaskSnapshotResult:
    return send_task_snapshot(
        conn, record, store_cfg, "clear_table", "tasks_clear", login
    )


def fetch_task(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    task_column: str,
    business_id: str,
) -> Optional[TaskRecord]:
    if task_column not in TASK_ID_COLUMNS:
        return None

    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        return None

    columns = ", ".join(f'"{col}"' for col in _TASK_SELECT_COLUMNS)
    query = (
        f'SELECT {columns} FROM "{schema}"."{table}" '
        f'WHERE "{task_column}" = %s LIMIT 1'
    )
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (business_id,))
            row = cur.fetchone()
        return TaskRecord.from_row(row) if row else None
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(
            f"Не удалось загрузить задачу crm.tasks ({task_column}={business_id}): {exc}"
        )
        return None


def fetch_task_by_key(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    key: str,
) -> Optional[TaskRecord]:
    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        return None

    columns = ", ".join(f'"{col}"' for col in _TASK_SELECT_COLUMNS)
    query = f'SELECT {columns} FROM "{schema}"."{table}" WHERE key = %s LIMIT 1'
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (key,))
            row = cur.fetchone()
        return TaskRecord.from_row(row) if row else None
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить задачу crm.tasks (key={key}): {exc}")
        return None


def fetch_task_for_feature(
    conn: DatabaseConnection,
    subgroup_name: str,
    attributes: Dict[str, Any],
    store_cfg: Dict[str, Any],
    *,
    layer: Any = None,
) -> Optional[TaskRecord]:
    lookup = resolve_task_lookup(
        subgroup_name, attributes, store_cfg, layer=layer
    )
    if lookup is None:
        return None
    task_column, business_id = lookup
    return fetch_task(conn, store_cfg, task_column, business_id)


def resolve_primary_task_column(
    subgroup_name: Optional[str],
    store_cfg: Dict[str, Any],
    record: Optional[TaskRecord] = None,
) -> Optional[str]:
    """Столбец crm.tasks для исходного объекта по имени подгруппы."""
    if subgroup_name == FIELD_DATA_SUBGROUP:
        return None
    if record is not None and record.is_field_data:
        return None
    if record is not None and record.is_office_task:
        return None
    if subgroup_name:
        mapping = store_cfg.get("subgroups", {}).get(subgroup_name)
        if mapping:
            if mapping.get("source") in ("field_data", "office_data"):
                return None
            task_column = mapping.get("task_column")
            if task_column in TASK_ID_COLUMNS:
                return task_column

    if record is not None:
        for col in TASK_ID_COLUMNS:
            if getattr(record, col):
                return col
    return None


def task_form_field_groups(
    group_name: Optional[str],
    subgroup_name: Optional[str],
    store_cfg: Dict[str, Any],
    record: TaskRecord,
) -> Tuple[List[str], List[str]]:
    """Поля формы «Исполнить задачу»: readonly (источник) и link (сопоставление)."""
    if record.is_field_data or subgroup_name == FIELD_DATA_SUBGROUP:
        readonly: List[str] = ["type", "is_field_data"]
        link = list(LINK_COLUMNS_BY_GROUP.get(group_name or "", ()))
        return readonly, link
    if record.is_office_task or subgroup_name == OFFICE_DATA_SUBGROUP:
        readonly = ["type", "is_office_task"]
        link = list(LINK_COLUMNS_BY_GROUP.get(group_name or "", ()))
        return readonly, link

    primary = resolve_primary_task_column(subgroup_name, store_cfg, record)
    readonly = ["type"]
    if primary:
        readonly.append(primary)
    link = list(LINK_COLUMNS_BY_GROUP.get(group_name or "", ()))
    return readonly, link


def update_task_record(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
    login: str,
) -> None:
    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        raise RuntimeError("psycopg2 недоступен")

    _pg_recover_transaction(pg)
    task_type = (record.type or "").strip()
    if not task_type:
        raise ValueError("Поле «type» не может быть пустым")

    id_values = {
        col: _normalize_id_value(getattr(record, col)) for col in TASK_ID_COLUMNS
    }
    station_values = {
        col: _normalize_id_value(getattr(record, col)) for col in STATION_COLUMNS
    }
    audit = make_user_audit(login)

    try:
        with pg.cursor() as cur:
            all_columns = list(TASK_ID_COLUMNS) + list(STATION_COLUMNS)
            set_parts = (
                ['"type" = %s']
                + [f'"{col}" = %s' for col in all_columns]
                + ['"user_last_edit" = %s::text[]']
            )
            params: List[Any] = [task_type] + [
                id_values[col] for col in TASK_ID_COLUMNS
            ] + [station_values[col] for col in STATION_COLUMNS]
            params.append(audit)
            params.append(record.key)
            query = (
                f'UPDATE "{schema}"."{table}" '
                f'SET {", ".join(set_parts)} '
                f'WHERE key = %s'
            )
            cur.execute(query, params)
            if cur.rowcount == 0:
                raise ValueError(f"Задача с ключом {record.key} не найдена")
        pg.commit()
        log_info(f"crm.tasks: обновлена задача {record.key}")
        from .crm_statistics import log_statistic

        log_statistic(
            conn,
            login=login,
            object_type="task",
            action="task_updated",
            object_key=record.key,
            metadata={"task_type": task_type},
            skip_if_exists=False,
        )
    except Exception:
        _pg_rollback(pg)
        raise


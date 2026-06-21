# -*- coding: utf-8 -*-
"""Сохранение задач CRM в PostgreSQL (crm.tasks)."""

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from .db import DatabaseConnection
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
    "photo_uuid": "Фото ИИ (uuid)",
    "photo_lens": "Фото Объектив (external_report_id)",
    "ogh_id": "ОГХ (id)",
    "oati_id": "ОАТИ (order_number)",
    "earthwork_id": "Земляные работы (registration_number_notifications)",
    "localwork_id": "Локальные ремонты (global_id)",
    "avr_mos_id": "АВР (em_call_reg_num)",
    "sps": "СПС",
    "kgs": "КГС",
    "station_avr": "АВР",
}

TASK_FORM_FIELDS = ("type",) + TASK_ID_COLUMNS

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

_DROP_TASK_ID_UNIQUE_INDEXES = (
    "DROP INDEX IF EXISTS crm.tasks_uq_photo_uuid",
    "DROP INDEX IF EXISTS crm.tasks_uq_photo_lens",
    "DROP INDEX IF EXISTS crm.tasks_uq_ogh_id",
    "DROP INDEX IF EXISTS crm.tasks_uq_oati_id",
    "DROP INDEX IF EXISTS crm.tasks_uq_earthwork_id",
    "DROP INDEX IF EXISTS crm.tasks_uq_localwork_id",
    "DROP INDEX IF EXISTS crm.tasks_uq_avr_mos_id",
)

_TASKS_INDEXES_DROPPED_KEY = "crm.tasks.indexes_dropped"


def _pg_set_admin_timeouts(cur) -> None:
    cur.execute("SET LOCAL statement_timeout = '120000'")
    cur.execute("SET LOCAL lock_timeout = '15000'")


def _station_migration_statements(schema: str, table: str) -> Tuple[str, ...]:
    return tuple(
        f'ALTER TABLE "{schema}"."{table}" '
        f'ADD COLUMN IF NOT EXISTS "{col}" TEXT'
        for col in STATION_COLUMNS
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


SendTaskSnapshotResult = Literal["inserted", "skipped"]
# Backward-compatible alias
SendToFieldResult = SendTaskSnapshotResult


@dataclass
class PersistStats:
    inserted: int = 0
    skipped: int = 0
    invalid: int = 0


@dataclass
class TaskRecord:
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
        }

    @classmethod
    def from_row(cls, row: Tuple) -> "TaskRecord":
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
            return True
        cache.discard(cache_key)

    _pg_recover_transaction(pg)
    indexes_dropped_key = _TASKS_INDEXES_DROPPED_KEY
    try:
        with pg.cursor() as cur:
            _pg_set_admin_timeouts(cur)
            for stmt in _DDL_STATEMENTS:
                cur.execute(stmt)
            for stmt in _station_migration_statements(schema, table):
                cur.execute(stmt)
            if indexes_dropped_key not in cache:
                for stmt in _DROP_TASK_ID_UNIQUE_INDEXES:
                    cur.execute(stmt)
                cache.add(indexes_dropped_key)
        pg.commit()
        cache.add(cache_key)
        log_info("Таблица crm.tasks проверена/создана")
        return True
    except Exception as exc:
        _pg_rollback(pg)
        _last_tasks_ensure_error = str(exc)
        log_warning(f"Не удалось создать crm.tasks: {exc}")
        return False


def _apply_snapshot_migrations(
    pg, snapshot_schema: str, snapshot_table: str
) -> None:
    with pg.cursor() as cur:
        _pg_set_admin_timeouts(cur)
        for stmt in _snapshot_migration_statements(snapshot_schema, snapshot_table):
            cur.execute(stmt)


def _create_snapshot_table(
    pg,
    schema: str,
    tasks_table: str,
    snapshot_schema: str,
    snapshot_table: str,
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


def task_row_from_feature(
    group_name: str,
    subgroup_name: str,
    attributes: Dict[str, Any],
    store_cfg: Dict[str, Any],
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
) -> Optional[Tuple[str, str]]:
    """Вернуть (task_column, business_id) для поиска строки в crm.tasks."""
    row = task_row_from_feature("", subgroup_name, attributes, store_cfg)
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
            _apply_snapshot_migrations(pg, snapshot_schema, snapshot_table)
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
        pg, schema, tasks_table, snapshot_schema, snapshot_table
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


def filter_sent_tasks_from_result(
    task_result,
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> int:
    """Скрыть задачи, уже отправленные в field/done_* таблицы. Возвращает число скрытых."""
    snapshot_keys = fetch_snapshot_task_keys(conn, store_cfg)
    if not snapshot_keys:
        return 0

    task_index = fetch_task_keys_index(conn, store_cfg)
    hidden = 0

    for group in task_result.groups:
        for subgroup in group.subgroups:
            kept = []
            for feat in subgroup.features:
                lookup = resolve_task_lookup(
                    subgroup.name, feat.attributes, store_cfg
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


def send_task_snapshot(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
    config_key: str,
    default_table: str,
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

    columns = ["task_key", "type"] + list(TASK_ID_COLUMNS) + list(STATION_COLUMNS)
    values = [record.key, task_type] + [
        _normalize_id_value(getattr(record, col)) for col in TASK_ID_COLUMNS
    ] + [
        _normalize_id_value(getattr(record, col)) for col in STATION_COLUMNS
    ]
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
        return "inserted"
    except Exception:
        _pg_rollback(pg)
        raise


def send_task_to_field(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
) -> SendTaskSnapshotResult:
    return send_task_snapshot(
        conn, record, store_cfg, "field_table", "tasks_field"
    )


def send_task_to_done_legal(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
) -> SendTaskSnapshotResult:
    return send_task_snapshot(
        conn, record, store_cfg, "done_legal_table", "tasks_done_legal"
    )


def send_task_to_done_illegal(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
) -> SendTaskSnapshotResult:
    return send_task_snapshot(
        conn, record, store_cfg, "done_illegal_table", "tasks_done_illegal"
    )


def send_task_to_clear(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
) -> SendTaskSnapshotResult:
    return send_task_snapshot(
        conn, record, store_cfg, "clear_table", "tasks_clear"
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

    columns = ", ".join(
        f'"{col}"' for col in ("key", "type") + TASK_ID_COLUMNS + STATION_COLUMNS
    )
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

    columns = ", ".join(
        f'"{col}"' for col in ("key", "type") + TASK_ID_COLUMNS + STATION_COLUMNS
    )
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
) -> Optional[TaskRecord]:
    lookup = resolve_task_lookup(subgroup_name, attributes, store_cfg)
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
    if subgroup_name:
        mapping = store_cfg.get("subgroups", {}).get(subgroup_name)
        if mapping:
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
    primary = resolve_primary_task_column(subgroup_name, store_cfg, record)
    readonly: List[str] = ["type"]
    if primary:
        readonly.append(primary)
    link = list(LINK_COLUMNS_BY_GROUP.get(group_name or "", ()))
    return readonly, link


def update_task_record(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
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

    try:
        with pg.cursor() as cur:
            all_columns = list(TASK_ID_COLUMNS) + list(STATION_COLUMNS)
            set_parts = ['"type" = %s'] + [f'"{col}" = %s' for col in all_columns]
            params: List[Any] = [task_type] + [
                id_values[col] for col in TASK_ID_COLUMNS
            ] + [station_values[col] for col in STATION_COLUMNS]
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
    except Exception:
        _pg_rollback(pg)
        raise


def _task_exists(cur, schema: str, table: str, column: str, value: str) -> bool:
    query = f'SELECT 1 FROM "{schema}"."{table}" WHERE "{column}" = %s LIMIT 1'
    cur.execute(query, (value,))
    return cur.fetchone() is not None


def _insert_task(cur, schema: str, table: str, row: Dict[str, Any]) -> None:
    columns = ["type"] + list(TASK_ID_COLUMNS)
    values = [row["type"]] + [row[col] for col in TASK_ID_COLUMNS]
    placeholders = ", ".join(["%s"] * len(columns))
    col_list = ", ".join(f'"{col}"' for col in columns)
    query = f'INSERT INTO "{schema}"."{table}" ({col_list}) VALUES ({placeholders})'
    cur.execute(query, values)


def persist_task_result(
    conn: DatabaseConnection,
    task_result,
    store_cfg: Dict[str, Any],
) -> PersistStats:
    """Записать уникальные задачи из TaskResult в crm.tasks."""
    stats = PersistStats()
    if not ensure_tasks_table(conn):
        detail = _last_tasks_ensure_error or "неизвестная ошибка"
        raise RuntimeError(f"Не удалось подготовить таблицу crm.tasks: {detail}")

    schema = store_cfg.get("schema", "crm")
    table = store_cfg.get("table", "tasks")
    pg = _pg_connection(conn)
    if pg is None:
        raise RuntimeError(
            "Для записи в crm.tasks нужен psycopg2 в окружении QGIS"
        )

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            for group in task_result.groups:
                for subgroup in group.subgroups:
                    for task_feat in subgroup.features:
                        row = task_row_from_feature(
                            group.name,
                            subgroup.name,
                            task_feat.attributes,
                            store_cfg,
                        )
                        if row is None:
                            stats.invalid += 1
                            continue

                        task_column = next(
                            col for col in TASK_ID_COLUMNS if row[col] is not None
                        )
                        business_id = row[task_column]

                        if _task_exists(cur, schema, table, task_column, business_id):
                            stats.skipped += 1
                            continue

                        _insert_task(cur, schema, table, row)
                        stats.inserted += 1

        pg.commit()
        log_info(
            f"crm.tasks: добавлено {stats.inserted}, "
            f"пропущено {stats.skipped}, без ID {stats.invalid}"
        )
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Ошибка записи в crm.tasks: {exc}")
        raise

    return stats

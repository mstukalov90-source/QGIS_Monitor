# -*- coding: utf-8 -*-
"""Сохранение задач CRM в PostgreSQL (crm.tasks)."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
        avr_mos_id TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_photo_uuid
        ON crm.tasks (photo_uuid) WHERE photo_uuid IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_photo_lens
        ON crm.tasks (photo_lens) WHERE photo_lens IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_ogh_id
        ON crm.tasks (ogh_id) WHERE ogh_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_oati_id
        ON crm.tasks (oati_id) WHERE oati_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_earthwork_id
        ON crm.tasks (earthwork_id) WHERE earthwork_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_localwork_id
        ON crm.tasks (localwork_id) WHERE localwork_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_uq_avr_mos_id
        ON crm.tasks (avr_mos_id) WHERE avr_mos_id IS NOT NULL
    """,
)


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
        )


def _pg_connection(conn: DatabaseConnection):
    if psycopg2 is None:
        return None
    return conn._get_pg_connection()


def ensure_tasks_table(conn: DatabaseConnection) -> bool:
    """Создать схему crm и таблицу tasks при отсутствии."""
    pg = _pg_connection(conn)
    if pg is None:
        log_warning("psycopg2 недоступен — запись задач в БД невозможна")
        return False

    try:
        with pg.cursor() as cur:
            for stmt in _DDL_STATEMENTS:
                cur.execute(stmt)
        pg.commit()
        log_info("Таблица crm.tasks проверена/создана")
        return True
    except Exception as exc:
        try:
            pg.rollback()
        except Exception:
            pass
        log_warning(f"Не удалось создать crm.tasks: {exc}")
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

    columns = ", ".join(f'"{col}"' for col in ("key", "type") + TASK_ID_COLUMNS)
    query = (
        f'SELECT {columns} FROM "{schema}"."{table}" '
        f'WHERE "{task_column}" = %s LIMIT 1'
    )
    with pg.cursor() as cur:
        cur.execute(query, (business_id,))
        row = cur.fetchone()
    return TaskRecord.from_row(row) if row else None


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


def _duplicate_exists(
    cur,
    schema: str,
    table: str,
    column: str,
    value: str,
    exclude_key: str,
) -> bool:
    query = (
        f'SELECT 1 FROM "{schema}"."{table}" '
        f'WHERE "{column}" = %s AND key <> %s LIMIT 1'
    )
    cur.execute(query, (value, exclude_key))
    return cur.fetchone() is not None


def update_task_record(
    conn: DatabaseConnection,
    record: TaskRecord,
    store_cfg: Dict[str, Any],
) -> None:
    schema, table = _table_ref(store_cfg)
    pg = _pg_connection(conn)
    if pg is None:
        raise RuntimeError("psycopg2 недоступен")

    task_type = (record.type or "").strip()
    if not task_type:
        raise ValueError("Поле «type» не может быть пустым")

    values = {
        col: _normalize_id_value(getattr(record, col)) for col in TASK_ID_COLUMNS
    }

    try:
        with pg.cursor() as cur:
            for column, value in values.items():
                if value is None:
                    continue
                if _duplicate_exists(cur, schema, table, column, value, record.key):
                    label = TASK_COLUMN_LABELS.get(column, column)
                    raise ValueError(
                        f"Значение «{value}» для «{label}» уже используется в другой задаче"
                    )

            set_parts = ['"type" = %s'] + [f'"{col}" = %s' for col in TASK_ID_COLUMNS]
            params: List[Any] = [task_type] + [values[col] for col in TASK_ID_COLUMNS]
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
        try:
            pg.rollback()
        except Exception:
            pass
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
        return stats

    schema = store_cfg.get("schema", "crm")
    table = store_cfg.get("table", "tasks")
    pg = _pg_connection(conn)
    if pg is None:
        return stats

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
        try:
            pg.rollback()
        except Exception:
            pass
        log_warning(f"Ошибка записи в crm.tasks: {exc}")
        raise

    return stats

# -*- coding: utf-8 -*-
"""Создание задач камерального анализа (точка на карте)."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .crm_task_store import (
    CRM_GROUP_DISRUPTIONS,
    TASK_ID_COLUMNS,
    TaskRecord,
    _normalize_id_value,
    _pg_connection,
    _pg_recover_transaction,
    _pg_rollback,
    fetch_task_by_key,
    make_user_audit,
)
from .crm_tasks_area import analise_lock_holder
from .db import DatabaseConnection

_LINK_PREFILL_COLUMNS = frozenset(TASK_ID_COLUMNS)


def create_office_task(
    conn: DatabaseConnection,
    login: str,
    lng: float,
    lat: float,
    area_task_key: str,
    link_prefill: Optional[Dict[str, Any]] = None,
    store_cfg: Optional[Dict[str, Any]] = None,
) -> TaskRecord:
    login = (login or "").strip()
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise ValueError("Некорректные координаты точки")

    holder = analise_lock_holder(conn, area_task_key)
    if holder is None:
        raise ValueError("Площадный заказ не в режиме камерального анализа")
    if holder.strip() != login:
        raise ValueError("Камеральный анализ выполняет другой пользователь")

    pg = _pg_connection(conn)
    if pg is None:
        raise RuntimeError("Нет подключения к БД")

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT ST_Contains(
                    geom,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                )
                FROM crm.tasks_area
                WHERE key = %s::uuid
                  AND geom IS NOT NULL
                """,
                (lng, lat, area_task_key),
            )
            row = cur.fetchone()
        pg.commit()
    except Exception as exc:
        _pg_rollback(pg)
        raise RuntimeError(f"Не удалось проверить точку: {exc}") from exc

    if not row or not row[0]:
        raise ValueError("Точка должна находиться внутри полигона площадного заказа")

    id_values: Dict[str, Optional[str]] = {col: None for col in TASK_ID_COLUMNS}
    if link_prefill:
        for col, value in link_prefill.items():
            if col not in _LINK_PREFILL_COLUMNS:
                continue
            normalized = _normalize_id_value(value)
            if normalized:
                id_values[col] = normalized

    audit = make_user_audit(login)
    geom_json = json.dumps({"type": "Point", "coordinates": [lng, lat]})

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crm.tasks (
                    type,
                    photo_uuid, photo_lens, ogh_id, oati_id, earthwork_id,
                    localwork_id, avr_mos_id,
                    is_office_task,
                    user_created, user_last_edit
                ) VALUES (
                    %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    TRUE,
                    %s::text[], %s::text[]
                )
                RETURNING key
                """,
                (
                    CRM_GROUP_DISRUPTIONS,
                    id_values["photo_uuid"],
                    id_values["photo_lens"],
                    id_values["ogh_id"],
                    id_values["oati_id"],
                    id_values["earthwork_id"],
                    id_values["localwork_id"],
                    id_values["avr_mos_id"],
                    audit,
                    audit,
                ),
            )
            inserted = cur.fetchone()
            if not inserted:
                raise RuntimeError("Не удалось создать office-задачу")
            task_key = str(inserted[0])

            cur.execute(
                """
                INSERT INTO crm.office_task_points (task_key, point)
                VALUES (%s::uuid, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
                """,
                (task_key, geom_json),
            )
        pg.commit()
    except Exception as exc:
        _pg_rollback(pg)
        raise

    if store_cfg is None:
        store_cfg = {"schema": "crm", "table": "tasks"}
    record = fetch_task_by_key(conn, store_cfg, task_key)
    if record is None:
        raise RuntimeError("Office-задача не найдена после создания")
    conn.invalidate_crm_session_cache()
    return record

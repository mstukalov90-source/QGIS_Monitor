# -*- coding: utf-8 -*-
"""Загрузка подгруппы «Полевые данные» из crm.tasks + mggt_field.reports."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from qgis.core import QgsGeometry

from .crm_task_store import (
    TASK_ID_COLUMNS,
    _pg_connection,
    fetch_snapshot_task_keys,
)
from .crm_tasks import TaskFeature, TaskGroup, TaskResult, TaskSubgroup
from .crm_ui_constants import (
    FIELD_DATA_LAYER_KEY,
    FIELD_DATA_SUBGROUP,
)
from .db import DatabaseConnection
from .district_utils import DistrictBoundary
from .log_util import log_info, log_warning

_REPORT_SKIP_COLUMNS = frozenset({"point", "tasks_key", "geom", "geometry"})


def fetch_district_wkt_db(
    conn: DatabaseConnection,
    rayon: str,
    metric_srid: int,
    *,
    schema: str = "odh_export",
    table: str = "hood",
    field: str = "rayon",
) -> Optional[str]:
    """WKT полигона района из PostGIS (как в WEBCRM fetch_district_wkt)."""
    pg = _pg_connection(conn)
    if pg is None:
        return None

    query = f"""
        SELECT ST_AsText(
            ST_Union(ST_Transform(geom, %s))
        ) AS wkt
        FROM "{schema}"."{table}"
        WHERE "{field}" = %s
    """
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (metric_srid, rayon))
            row = cur.fetchone()
        pg.commit()
        if row and row[0]:
            return str(row[0])
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить полигон района «{rayon}»: {exc}")
    return None


def _pg_recover_transaction(pg) -> None:
    if pg is None:
        return
    try:
        pg.rollback()
    except Exception:
        pass


def _pg_rollback(pg) -> None:
    if pg is None:
        return
    try:
        pg.rollback()
    except Exception:
        pass


def field_data_mapping(store_cfg: Dict[str, Any]) -> Dict[str, Any]:
    return store_cfg.get("subgroups", {}).get(FIELD_DATA_SUBGROUP, {})


def _reports_qualified_table(mapping: Dict[str, Any]) -> str:
    schema = mapping.get("reports_schema", "mggt_field")
    table = mapping.get("reports_table", "reports")
    return f'"{schema}"."{table}"'


def _serialize_report_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list)):
        return value
    return value


def report_row_to_attributes(row: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    for key, value in row.items():
        if key in _REPORT_SKIP_COLUMNS:
            continue
        if key.startswith("_"):
            continue
        serialized = _serialize_report_value(value)
        if serialized is not None:
            attrs[key] = serialized
    return attrs


def _geometry_from_wkb(geom_wkb) -> Optional[QgsGeometry]:
    if geom_wkb is None:
        return None
    try:
        raw = bytes(geom_wkb) if not isinstance(geom_wkb, (bytes, bytearray)) else geom_wkb
        geom = QgsGeometry.fromWkb(raw)
        if geom and not geom.isEmpty():
            return geom
    except Exception:
        pass
    return None


def _geometry_from_wkt(geom_wkt: Optional[str]) -> Optional[QgsGeometry]:
    if not geom_wkt:
        return None
    try:
        geom = QgsGeometry.fromWkt(geom_wkt)
        return geom if geom and not geom.isEmpty() else None
    except Exception:
        return None


def mark_discovered_field_data_tasks(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
) -> int:
    mapping = field_data_mapping(store_cfg)
    if mapping.get("source") != "field_data":
        return 0

    pg = _pg_connection(conn)
    if pg is None:
        return 0

    tasks_schema = store_cfg.get("schema", "crm")
    tasks_table = store_cfg.get("table", "tasks")
    reports_table = _reports_qualified_table(mapping)
    tasks_key_col = mapping.get("reports_tasks_key", "tasks_key")
    geom_col = mapping.get("reports_geometry", "point")
    null_checks = " AND ".join(f't."{col}" IS NULL' for col in TASK_ID_COLUMNS)

    query = f"""
        UPDATE "{tasks_schema}"."{tasks_table}" t
        SET is_field_data = true
        FROM {reports_table} r
        WHERE r."{tasks_key_col}" = t.key
          AND COALESCE(t.is_field_data, false) IS NOT TRUE
          AND t.field_observed IS TRUE
          AND {null_checks}
          AND r."{geom_col}" IS NOT NULL
    """
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query)
            updated = cur.rowcount
        pg.commit()
        if updated:
            log_info(f"crm.tasks: помечено is_field_data={updated}")
        return updated
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось обновить is_field_data: {exc}")
        raise


def fetch_field_report_row(
    conn: DatabaseConnection,
    task_key: str,
    store_cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    mapping = field_data_mapping(store_cfg)
    if mapping.get("source") != "field_data":
        return None

    pg = _pg_connection(conn)
    if pg is None:
        return None

    reports_table = _reports_qualified_table(mapping)
    tasks_key_col = mapping.get("reports_tasks_key", "tasks_key")
    geom_col = mapping.get("reports_geometry", "point")

    query = f"""
        SELECT r.*,
               ST_AsBinary(ST_Transform(r."{geom_col}", 4326)) AS _geom_wkb
        FROM {reports_table} r
        WHERE r."{tasks_key_col}" = %s::uuid
          AND r."{geom_col}" IS NOT NULL
        LIMIT 1
    """
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (task_key,))
            row = cur.fetchone()
            if not row:
                pg.commit()
                return None
            cols = [d[0] for d in cur.description]
            data = dict(zip(cols, row))
        pg.commit()
        return data
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить report для {task_key}: {exc}")
        return None


def _row_to_task_feature(
    row: Dict[str, Any],
    report_attrs: Dict[str, Any],
    task_geom: Optional[QgsGeometry],
) -> TaskFeature:
    attrs = dict(report_attrs)
    attrs["field_observed"] = bool(row.get("field_observed"))
    attrs["is_field_data"] = True
    for col in (
        "oati_id",
        "earthwork_id",
        "localwork_id",
        "avr_mos_id",
        "sps",
        "kgs",
        "station_avr",
    ):
        value = row.get(col)
        if value is not None and str(value).strip():
            attrs[col] = str(value).strip()

    return TaskFeature(
        layer=None,
        layer_name=FIELD_DATA_SUBGROUP,
        layer_key=FIELD_DATA_LAYER_KEY,
        feature_id=None,
        attributes=attrs,
        task_key=str(row["key"]),
        task_geom=task_geom,
    )


def collect_field_data_tasks(
    conn: DatabaseConnection,
    district: DistrictBoundary,
    store_cfg: Dict[str, Any],
    metric_srid: int,
) -> Tuple[List[TaskFeature], List[str]]:
    mapping = field_data_mapping(store_cfg)
    if mapping.get("source") != "field_data":
        return [], []

    errors: List[str] = []
    district_wkt = fetch_district_wkt_db(conn, district.name, metric_srid)
    if not district_wkt:
        district_wkt = district.geom_metric.asWkt()
    if not district_wkt:
        return [], [f"District polygon not found for «{district.name}»"]

    pg = _pg_connection(conn)
    if pg is None:
        return [], ["Нет подключения к БД"]

    tasks_schema = store_cfg.get("schema", "crm")
    tasks_table = store_cfg.get("table", "tasks")
    reports_table = _reports_qualified_table(mapping)
    tasks_key_col = mapping.get("reports_tasks_key", "tasks_key")
    geom_col = mapping.get("reports_geometry", "point")

    query = f"""
        SELECT t.key, t.type, t.field_observed, t.is_field_data,
               t.oati_id, t.earthwork_id, t.localwork_id, t.avr_mos_id,
               t.sps, t.kgs, t.station_avr,
               ST_AsBinary(ST_Transform(r."{geom_col}", 4326)) AS geom_wkb,
               ST_AsText(ST_Transform(r."{geom_col}", 4326)) AS geom_wkt,
               row_to_json(r)::text AS report_json
        FROM "{tasks_schema}"."{tasks_table}" t
        INNER JOIN {reports_table} r ON r."{tasks_key_col}" = t.key
        WHERE t.is_field_data IS TRUE
          AND t.field_observed IS TRUE
          AND r."{geom_col}" IS NOT NULL
          AND ST_Intersects(
              ST_Transform(r."{geom_col}", {metric_srid}),
              ST_GeomFromText(%s, {metric_srid})
          )
    """

    snapshot_keys = fetch_snapshot_task_keys(conn, store_cfg)
    features: List[TaskFeature] = []

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (district_wkt,))
            col_names = [d[0] for d in cur.description]
            for row in cur.fetchall():
                data = dict(zip(col_names, row))
                task_key = str(data["key"])
                if task_key in snapshot_keys:
                    continue
                task_geom = _geometry_from_wkb(data.pop("geom_wkb", None))
                if not task_geom:
                    task_geom = _geometry_from_wkt(data.pop("geom_wkt", None))
                else:
                    data.pop("geom_wkt", None)
                if not task_geom:
                    continue
                report_raw = data.pop("report_json", None) or {}
                if isinstance(report_raw, str):
                    report_raw = json.loads(report_raw)
                report_attrs = report_row_to_attributes(dict(report_raw))
                features.append(_row_to_task_feature(data, report_attrs, task_geom))
        pg.commit()
        log_info(f"CRM «{FIELD_DATA_SUBGROUP}»: {len(features)} объектов")
    except Exception as exc:
        _pg_rollback(pg)
        errors.append(f"{FIELD_DATA_SUBGROUP}: {exc}")

    return features, errors


def append_field_data_to_result(
    result: TaskResult,
    conn: DatabaseConnection,
    district: DistrictBoundary,
    store_cfg: Dict[str, Any],
    metric_srid: int,
) -> None:
    mapping = field_data_mapping(store_cfg)
    if mapping.get("source") != "field_data":
        return

    try:
        mark_discovered_field_data_tasks(conn, store_cfg)
    except Exception as exc:
        result.errors.append(f"{FIELD_DATA_SUBGROUP}: не удалось обновить is_field_data: {exc}")
        return

    features, errors = collect_field_data_tasks(
        conn, district, store_cfg, metric_srid
    )
    result.errors.extend(errors)

    subgroup = _find_or_create_subgroup(result, FIELD_DATA_SUBGROUP)
    subgroup.features = features


def _find_or_create_subgroup(
    result: TaskResult, subgroup_name: str
) -> TaskSubgroup:
    from .crm_task_store import CRM_GROUP_DISRUPTIONS

    for group in result.groups:
        if group.name != CRM_GROUP_DISRUPTIONS:
            continue
        for subgroup in group.subgroups:
            if subgroup.name == subgroup_name:
                return subgroup
        subgroup = TaskSubgroup(name=subgroup_name, features=[])
        group.subgroups.append(subgroup)
        return subgroup

    group = TaskGroup(name=CRM_GROUP_DISRUPTIONS, subgroups=[])
    subgroup = TaskSubgroup(name=subgroup_name, features=[])
    group.subgroups.append(subgroup)
    result.groups.append(group)
    return subgroup

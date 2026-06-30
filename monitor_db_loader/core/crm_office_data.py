# -*- coding: utf-8 -*-
"""Загрузка подгруппы «Задачи из камерального анализа» из crm.tasks + office_task_points."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import QgsGeometry

from .crm_field_data import (
    _geometry_from_wkb,
    _geometry_from_wkt,
    _pg_recover_transaction,
    _pg_rollback,
    fetch_district_wkt_db,
)
from .crm_task_store import CRM_GROUP_DISRUPTIONS, _pg_connection, get_snapshot_task_keys
from .crm_tasks import TaskFeature, TaskGroup, TaskResult, TaskSubgroup
from .crm_ui_constants import OFFICE_DATA_LAYER_KEY, OFFICE_DATA_SUBGROUP
from .db import DatabaseConnection
from .district_utils import DistrictBoundary
from .log_util import log_info, log_warning


def office_data_mapping(store_cfg: Dict[str, Any]) -> Dict[str, Any]:
    return store_cfg.get("subgroups", {}).get(OFFICE_DATA_SUBGROUP, {})


def _points_qualified_table(mapping: Dict[str, Any]) -> str:
    schema = mapping.get("points_schema", "crm")
    table = mapping.get("points_table", "office_task_points")
    return f'"{schema}"."{table}"'


def _serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row_to_task_feature(
    row: Dict[str, Any],
    task_geom: Optional[QgsGeometry],
) -> TaskFeature:
    attrs: Dict[str, Any] = {
        "is_office_task": True,
        "created_at": _serialize_value(row.get("created_at")),
    }
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
        layer_name=OFFICE_DATA_SUBGROUP,
        layer_key=OFFICE_DATA_LAYER_KEY,
        feature_id=None,
        attributes=attrs,
        task_key=str(row["key"]),
        task_geom=task_geom,
    )


def collect_office_data_tasks(
    conn: DatabaseConnection,
    district: DistrictBoundary,
    store_cfg: Dict[str, Any],
    metric_srid: int,
) -> Tuple[List[TaskFeature], List[str]]:
    mapping = office_data_mapping(store_cfg)
    if mapping.get("source") != "office_data":
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
    points_table = _points_qualified_table(mapping)
    geom_col = mapping.get("points_geometry", "point")

    query = f"""
        SELECT t.key, t.type, t.is_office_task,
               t.oati_id, t.earthwork_id, t.localwork_id, t.avr_mos_id,
               t.sps, t.kgs, t.station_avr,
               p.created_at,
               ST_AsBinary(ST_Transform(p."{geom_col}", 4326)) AS geom_wkb,
               ST_AsText(ST_Transform(p."{geom_col}", 4326)) AS geom_wkt
        FROM "{tasks_schema}"."{tasks_table}" t
        INNER JOIN {points_table} p ON p.task_key = t.key
        WHERE t.is_office_task IS TRUE
          AND p."{geom_col}" IS NOT NULL
          AND ST_Intersects(
              ST_Transform(p."{geom_col}", {metric_srid}),
              ST_GeomFromText(%s, {metric_srid})
          )
    """

    snapshot_keys = get_snapshot_task_keys(conn, store_cfg)
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
                features.append(_row_to_task_feature(data, task_geom))
        pg.commit()
        log_info(f"CRM «{OFFICE_DATA_SUBGROUP}»: {len(features)} объектов")
    except Exception as exc:
        _pg_rollback(pg)
        errors.append(f"{OFFICE_DATA_SUBGROUP}: {exc}")

    return features, errors


def append_office_data_to_result(
    result: TaskResult,
    conn: DatabaseConnection,
    district: DistrictBoundary,
    store_cfg: Dict[str, Any],
    metric_srid: int,
) -> None:
    mapping = office_data_mapping(store_cfg)
    if mapping.get("source") != "office_data":
        return

    features, errors = collect_office_data_tasks(
        conn, district, store_cfg, metric_srid
    )
    result.errors.extend(errors)

    subgroup = _find_or_create_subgroup(result, OFFICE_DATA_SUBGROUP)
    subgroup.features = features


def _find_or_create_subgroup(
    result: TaskResult, subgroup_name: str
) -> TaskSubgroup:
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

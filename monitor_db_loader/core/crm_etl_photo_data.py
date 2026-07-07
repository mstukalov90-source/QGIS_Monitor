# -*- coding: utf-8 -*-
"""Load ETL-synced photo tasks (genplan + lens) from crm.tasks JOIN."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import QgsGeometry

from .config import iter_all_layer_defs, resolve_layer_source
from .crm_field_data import (
    _geometry_from_wkb,
    _geometry_from_wkt,
    _pg_connection,
    _pg_recover_transaction,
    _pg_rollback,
    fetch_district_wkt_db,
)
from .crm_task_store import CRM_GROUP_DISRUPTIONS, get_snapshot_task_keys
from .crm_tasks import TaskFeature, TaskGroup, TaskResult, TaskSubgroup
from .crm_ui_constants import (
    AI_PHOTO_SUBGROUP,
    LENS_PHOTO_SUBGROUP,
)
from .db import DatabaseConnection
from .district_utils import DistrictBoundary
from .log_util import log_info, log_warning

ETL_SYNC_SOURCE = "etl_sync"

ETL_PHOTO_TASK_COLUMNS = frozenset({"photo_uuid", "photo_lens"})

PHOTO_SUBGROUP_LAYER_NAMES: Dict[str, List[str]] = {
    "photo_uuid": ["Фотографии после обработки ИИ"],
    "photo_lens": ["Фото разрывий и строек"],
}

ETL_SYNC_SUBGROUPS = frozenset({AI_PHOTO_SUBGROUP, LENS_PHOTO_SUBGROUP})


def is_etl_sync_subgroup(subgroup_name: str) -> bool:
    return subgroup_name in ETL_SYNC_SUBGROUPS


def is_etl_sync_cfg(sub_cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(sub_cfg and sub_cfg.get("source") == ETL_SYNC_SOURCE)


def is_etl_photo_subgroup(store_cfg: Dict[str, Any], subgroup_name: str) -> bool:
    mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
    return mapping.get("task_column") in ETL_PHOTO_TASK_COLUMNS


def iter_etl_photo_subgroups(store_cfg: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for name, mapping in store_cfg.get("subgroups", {}).items():
        if mapping.get("task_column") in ETL_PHOTO_TASK_COLUMNS:
            names.append(name)
    return names


def _slugify(name: str) -> str:
    value = name.lower().strip()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "layer"


def _qualified_table(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def _find_layer_defs(config: Dict[str, Any], display_names: List[str]) -> List[Dict[str, Any]]:
    wanted = set(display_names)
    found: List[Dict[str, Any]] = []
    for _, layer_def in iter_all_layer_defs(config):
        display_name = layer_def.get("display_name")
        if display_name in wanted:
            found.append(layer_def)
    return found


def _district_spatial_sql(
    geometry_type: str,
    geom_col: str,
    metric_srid: int,
) -> str:
    geom_ref = f't."{geom_col}"'
    district_in_layer = f"""
        ST_Transform(
            ST_GeomFromText(%s, {metric_srid}),
            ST_SRID({geom_ref})
        )
    """
    if geometry_type == "point":
        return f"ST_Contains({district_in_layer}, {geom_ref})"
    return f"ST_Intersects({geom_ref}, {district_in_layer})"


def _row_to_task_feature(
    *,
    layer_def: Dict[str, Any],
    attrs: Dict[str, Any],
    task_key: str,
    task_geom: Optional[QgsGeometry],
) -> TaskFeature:
    display_name = layer_def.get("display_name", "")
    return TaskFeature(
        layer=None,
        layer_name=display_name,
        layer_key=_slugify(display_name),
        feature_id=None,
        attributes=attrs,
        task_key=task_key,
        task_geom=task_geom,
    )


def collect_etl_sync_subgroup_tasks(
    conn: DatabaseConnection,
    district: DistrictBoundary,
    metric_srid: int,
    subgroup_name: str,
    store_cfg: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[List[TaskFeature], List[str]]:
    mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
    source_field = mapping.get("source_field")
    task_column = mapping.get("task_column")
    if not source_field or not task_column:
        return [], [f"No task store mapping for subgroup «{subgroup_name}»"]

    crm_cfg = config.get("crm_tasks", {})
    sub_cfg = None
    for group_cfg in crm_cfg.get("groups", []):
        for candidate in group_cfg.get("subgroups", []):
            if candidate.get("name") == subgroup_name:
                sub_cfg = candidate
                break
        if sub_cfg is not None:
            break

    layer_names: List[str] = []
    if sub_cfg is not None:
        layer_names = list(sub_cfg.get("layers", []))
    if not layer_names:
        layer_names = list(
            PHOTO_SUBGROUP_LAYER_NAMES.get(str(task_column), [])
        )
    if not is_etl_sync_cfg(sub_cfg) and not is_etl_photo_subgroup(store_cfg, subgroup_name):
        return [], [f"Subgroup is not etl_sync: {subgroup_name}"]
    layer_defs = _find_layer_defs(config, layer_names)
    if not layer_defs:
        return [], [f"No layer definitions for subgroup «{subgroup_name}»"]

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
    snapshot_keys = get_snapshot_task_keys(conn, store_cfg)
    features: List[TaskFeature] = []
    errors: List[str] = []

    business_id_expr = f'NULLIF(TRIM(t."{source_field}"::text), \'\')'

    for layer_def in layer_defs:
        schema, table, geom_col = resolve_layer_source(layer_def)
        if not geom_col:
            geom_col = "geom"
        geometry_type = str(layer_def.get("geometry_type", "point")).lower()
        qualified = _qualified_table(schema, table)
        spatial = _district_spatial_sql(geometry_type, geom_col, metric_srid)
        filters = [
            f't."{geom_col}" IS NOT NULL',
            spatial,
            f't."{source_field}" IS NOT NULL',
            f"{business_id_expr} IS NOT NULL",
            f'ct."{task_column}" IS NOT NULL',
        ]
        sql_filter = layer_def.get("sql_filter")
        if sql_filter:
            filters.append(f"({sql_filter})")
        where_clause = " AND ".join(filters)
        pk_col = layer_def.get("primary_key") or "id"

        query = f"""
            SELECT DISTINCT ON (ct.key)
                   ct.key::text AS task_key,
                   ct.field_observed,
                   to_jsonb(t) - '{geom_col}' AS attrs,
                   ST_AsBinary(ST_Transform(t."{geom_col}", 4326)) AS geom_wkb,
                   ST_AsText(ST_Transform(t."{geom_col}", 4326)) AS geom_wkt
            FROM {qualified} t
            INNER JOIN "{tasks_schema}"."{tasks_table}" ct
                ON ct."{task_column}" = {business_id_expr}
            WHERE {where_clause}
            ORDER BY ct.key, t."{pk_col}"
        """

        _pg_recover_transaction(pg)
        try:
            with pg.cursor() as cur:
                cur.execute(query, (district_wkt,))
                col_names = [d[0] for d in cur.description]
                layer_count = 0
                for row in cur.fetchall():
                    data = dict(zip(col_names, row))
                    task_key = str(data.get("task_key") or "")
                    if not task_key or task_key in snapshot_keys:
                        continue
                    attrs = data.get("attrs") or {}
                    if isinstance(attrs, str):
                        attrs = json.loads(attrs)
                    attrs = dict(attrs)
                    field_observed = data.get("field_observed")
                    if field_observed is not None:
                        attrs["field_observed"] = bool(field_observed)
                    task_geom = _geometry_from_wkb(data.get("geom_wkb"))
                    if not task_geom:
                        task_geom = _geometry_from_wkt(data.get("geom_wkt"))
                    if not task_geom:
                        continue
                    features.append(
                        _row_to_task_feature(
                            layer_def=layer_def,
                            attrs=attrs,
                            task_key=task_key,
                            task_geom=task_geom,
                        )
                    )
                    layer_count += 1
            pg.commit()
            log_info(
                f"CRM «{subgroup_name}» / «{layer_def.get('display_name', '')}»: "
                f"{layer_count} объектов (ETL)"
            )
        except Exception as exc:
            _pg_rollback(pg)
            errors.append(f"{layer_def.get('display_name', subgroup_name)}: {exc}")

    return features, errors


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


def append_etl_photo_tasks_to_result(
    result: TaskResult,
    conn: DatabaseConnection,
    district: DistrictBoundary,
    store_cfg: Dict[str, Any],
    metric_srid: int,
    config: Dict[str, Any],
) -> None:
    for subgroup_name in iter_etl_photo_subgroups(store_cfg):
        features, errors = collect_etl_sync_subgroup_tasks(
            conn,
            district,
            metric_srid,
            subgroup_name,
            store_cfg,
            config,
        )
        result.errors.extend(errors)
        subgroup = _find_or_create_subgroup(result, subgroup_name)
        subgroup.features = features

# -*- coding: utf-8 -*-
"""Load CRM tasks from crm.tasks JOIN source tables (DB-only, no QGIS layers)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import QgsGeometry
from qgis.PyQt.QtCore import QDate

from .config import iter_all_layer_defs, resolve_layer_source
from .crm_field_data import (
    _geometry_from_wkb,
    _geometry_from_wkt,
    _pg_connection,
    _pg_recover_transaction,
    _pg_rollback,
    fetch_district_wkt_db,
)
from .crm_task_store import get_snapshot_task_keys
from .crm_tasks import TaskFeature
from .db import DatabaseConnection
from .district_utils import DistrictBoundary
from .log_util import log_info, log_warning

DB_SYNC_SOURCES = frozenset({"etl_sync", "db_tasks"})
DEFERRED_SOURCES = frozenset({"field_data", "office_data"})

ETL_PHOTO_TASK_COLUMNS = frozenset({"photo_uuid", "photo_lens"})

PHOTO_SUBGROUP_LAYER_NAMES: Dict[str, List[str]] = {
    "photo_uuid": ["Фотографии после обработки ИИ"],
    "photo_lens": ["Фото разрывий и строек"],
}


def is_deferred_subgroup(sub_cfg: Dict[str, Any]) -> bool:
    return sub_cfg.get("source") in DEFERRED_SOURCES


def is_db_loaded_subgroup(
    sub_cfg: Dict[str, Any],
    store_cfg: Dict[str, Any],
    subgroup_name: str,
) -> bool:
    if is_deferred_subgroup(sub_cfg):
        return False
    if sub_cfg.get("source") in DB_SYNC_SOURCES:
        return True
    if sub_cfg.get("layers") or sub_cfg.get("groups"):
        mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
        return bool(mapping.get("task_column") and mapping.get("source_field"))
    return False


def business_id_sql_expr(
    source_field: str,
    geometry_type: str,
    *,
    scoped: bool,
) -> str:
    raw = f'NULLIF(TRIM(t."{source_field}"::text), \'\')'
    if scoped:
        return f"('{geometry_type}:' || {raw})"
    return raw


def district_spatial_sql(
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


def date_filter_sql(date_field: str) -> str:
    return (
        f't."{date_field}" IS NOT NULL '
        f'AND t."{date_field}"::date >= %s '
        f'AND t."{date_field}"::date <= %s'
    )


def _slugify(name: str) -> str:
    value = name.lower().strip()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[-\s]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "layer"


def _qualified_table(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def _find_layer_defs_by_display_names(
    config: Dict[str, Any],
    display_names: List[str],
) -> List[Dict[str, Any]]:
    wanted = set(display_names)
    found: List[Dict[str, Any]] = []
    for _, layer_def in iter_all_layer_defs(config):
        display_name = layer_def.get("display_name")
        if display_name in wanted:
            found.append(layer_def)
    return found


def _find_layer_defs_in_groups(
    config: Dict[str, Any],
    group_names: List[str],
) -> List[Dict[str, Any]]:
    wanted = set(group_names)
    found: List[Dict[str, Any]] = []
    for group_path, layer_def in iter_all_layer_defs(config):
        if not group_path:
            continue
        leaf = group_path.split("/")[-1]
        if leaf in wanted or group_path in wanted:
            found.append(layer_def)
    return found


def resolve_subgroup_layer_defs(
    config: Dict[str, Any],
    sub_cfg: Dict[str, Any],
    store_cfg: Dict[str, Any],
    subgroup_name: str,
) -> List[Dict[str, Any]]:
    layer_names = list(sub_cfg.get("layers", []))
    group_names = list(sub_cfg.get("groups", []))

    if not layer_names and not group_names:
        mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
        task_column = mapping.get("task_column")
        layer_names = list(PHOTO_SUBGROUP_LAYER_NAMES.get(str(task_column), []))

    layer_defs: List[Dict[str, Any]] = []
    if layer_names:
        layer_defs.extend(_find_layer_defs_by_display_names(config, layer_names))
    if group_names:
        layer_defs.extend(_find_layer_defs_in_groups(config, group_names))

    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for layer_def in layer_defs:
        if layer_def.get("placeholder"):
            continue
        display_name = layer_def.get("display_name")
        if not display_name or not layer_def.get("table_name"):
            continue
        if display_name in seen:
            continue
        seen.add(display_name)
        unique.append(layer_def)
    return unique


def _row_to_task_feature(
    *,
    layer_def: Dict[str, Any],
    attrs: Dict[str, Any],
    task_key: str,
    task_geom: Optional[QgsGeometry],
    subgroup_name: str,
) -> TaskFeature:
    display_name = layer_def.get("display_name", subgroup_name)
    return TaskFeature(
        layer=None,
        layer_name=display_name,
        layer_key=_slugify(display_name),
        feature_id=None,
        attributes=attrs,
        task_key=task_key,
        task_geom=task_geom,
    )


def collect_db_subgroup_tasks(
    conn: DatabaseConnection,
    district: DistrictBoundary,
    metric_srid: int,
    subgroup_name: str,
    store_cfg: Dict[str, Any],
    config: Dict[str, Any],
    sub_cfg: Dict[str, Any],
    *,
    date_from: Optional[QDate] = None,
    date_to: Optional[QDate] = None,
    apply_date_filter: bool = False,
) -> Tuple[List[TaskFeature], List[str]]:
    mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
    source_field = mapping.get("source_field")
    task_column = mapping.get("task_column")
    if not source_field or not task_column:
        return [], [f"No task store mapping for subgroup «{subgroup_name}»"]

    layer_defs = resolve_subgroup_layer_defs(
        config, sub_cfg, store_cfg, subgroup_name
    )
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
    scoped = bool(mapping.get("scoped_geometry_id"))
    date_field = sub_cfg.get("date_field") if apply_date_filter else None

    for layer_def in layer_defs:
        schema, table, geom_col = resolve_layer_source(layer_def)
        if not geom_col:
            geom_col = "geom"
        geometry_type = str(layer_def.get("geometry_type", "point")).lower()
        qualified = _qualified_table(schema, table)
        spatial = district_spatial_sql(geometry_type, geom_col, metric_srid)
        business_id_expr = business_id_sql_expr(
            source_field, geometry_type, scoped=scoped
        )
        filters = [
            f't."{geom_col}" IS NOT NULL',
            spatial,
            f't."{source_field}" IS NOT NULL',
            f"{business_id_expr} IS NOT NULL",
            f'ct."{task_column}" IS NOT NULL',
        ]
        if date_field:
            filters.append(date_filter_sql(date_field))
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

        params: List[Any] = [district_wkt]
        if date_field and date_from is not None and date_to is not None:
            params.extend(
                [
                    date_from.toString("yyyy-MM-dd"),
                    date_to.toString("yyyy-MM-dd"),
                ]
            )

        _pg_recover_transaction(pg)
        try:
            with pg.cursor() as cur:
                cur.execute(query, tuple(params))
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
                            subgroup_name=subgroup_name,
                        )
                    )
                    layer_count += 1
            pg.commit()
            log_info(
                f"CRM «{subgroup_name}» / «{layer_def.get('display_name', '')}»: "
                f"{layer_count} объектов (БД)"
            )
        except Exception as exc:
            _pg_rollback(pg)
            errors.append(
                f"{layer_def.get('display_name', subgroup_name)}: {exc}"
            )

    return features, errors

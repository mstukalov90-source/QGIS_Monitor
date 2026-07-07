# -*- coding: utf-8 -*-
"""Загрузка задач из snapshot-таблиц (tasks_field, tasks_done_*, tasks_clear)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import QgsFeature, QgsProject, QgsVectorLayer

from .config import crm_task_store, crm_tasks
from .crm_task_store import (
    TASK_ID_COLUMNS,
    TaskRecord,
    _normalize_id_value,
    _pg_connection,
    _pg_recover_transaction,
    _pg_rollback,
    _snapshot_table_ref,
    format_scoped_business_id,
    layer_geometry_type,
    parse_scoped_business_id,
)
from .crm_tasks import (
    TaskFeature,
    TaskGroup,
    TaskResult,
    TaskSubgroup,
    _date_filter_range,
    _feature_to_task,
)
from .crm_ui_constants import SNAPSHOT_SOURCES, normalize_rayon_name
from .db import DatabaseConnection
from .district_utils import (
    DistrictBoundary,
    features_in_district,
    resolve_layers,
)
from .log_util import log_warning


@dataclass
class SnapshotRow:
    snapshot_key: str
    task_key: str
    sent_at: Optional[str]
    record: TaskRecord
    subgroup_name: str
    group_name: str
    office_comment: Optional[str] = None
    rayon: Optional[str] = None
    geom_json: Optional[str] = None


def _lookup_feature_by_task_key_db(
    conn: DatabaseConnection,
    snap: SnapshotRow,
    store_cfg: Dict[str, Any],
    crm_cfg: Dict[str, Any],
) -> Optional[TaskFeature]:
    """Lookup geometry from items_* by task_key (priority over scoped id)."""
    import json

    from .config import load_layers_config

    pg = _pg_connection(conn)
    if pg is None:
        return None
    sub_cfg = _subgroup_cfg(snap.subgroup_name, crm_cfg)
    if sub_cfg is None:
        return None
    cfg = load_layers_config()
    root = QgsProject.instance().layerTreeRoot()
    layers, _ = resolve_layers(
        root,
        sub_cfg.get("layers", []),
        sub_cfg.get("groups", []),
    )
    layer_by_table: Dict[str, Any] = {}
    for layer in layers:
        uri = layer.dataProvider().uri()
        table = uri.table()
        if table:
            layer_by_table[table] = layer

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
                            SELECT to_jsonb(t) - '{geom_col}' AS attrs,
                                   ST_AsGeoJSON(ST_Transform(t."{geom_col}", 4326)) AS geometry
                            FROM {qualified} t
                            WHERE t.task_key = %s::uuid
                            LIMIT 1
                            """,
                            (snap.task_key,),
                        )
                        row = cur.fetchone()
                    if not row or not row[1]:
                        continue
                    geom = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                    attrs = dict(row[0]) if row[0] else {}
                    attrs["_task_key"] = snap.task_key
                    attrs["_snapshot_key"] = snap.snapshot_key
                    qgis_layer = layer_by_table.get(table_name)
                    return TaskFeature(
                        layer=qgis_layer,
                        layer_name=layer_name,
                        feature_id=None,
                        attributes=attrs,
                        task_key=snap.task_key,
                        sent_at=snap.sent_at,
                        task_geom=geom,
                    )
    return None


def _find_subgroup_for_record(
    record: TaskRecord,
    store_cfg: Dict[str, Any],
) -> Optional[Tuple[str, str, str]]:
    for subgroup_name, mapping in store_cfg.get("subgroups", {}).items():
        task_column = mapping.get("task_column")
        if task_column not in TASK_ID_COLUMNS:
            continue
        value = getattr(record, task_column, None)
        if value:
            return subgroup_name, task_column, value
    return None


def _find_group_name(subgroup_name: str, crm_cfg: Dict[str, Any]) -> str:
    for group_cfg in crm_cfg.get("groups", []):
        for sub_cfg in group_cfg.get("subgroups", []):
            if sub_cfg.get("name") == subgroup_name:
                return group_cfg.get("name", "")
    return ""


def fetch_snapshot_rows(
    conn: DatabaseConnection,
    store_cfg: Dict[str, Any],
    config_key: str,
    default_table: str,
    crm_cfg: Dict[str, Any],
    *,
    rayon: Optional[str] = None,
) -> List[SnapshotRow]:
    schema, table = _snapshot_table_ref(store_cfg, config_key, default_table)
    columns = ["key", "task_key", "sent_at", "type"] + list(TASK_ID_COLUMNS) + [
        "sps",
        "kgs",
        "station_avr",
    ]
    include_office_comment = config_key == "field_table"
    if include_office_comment:
        columns.append("office_comment")
        columns.append("rayon")
        columns.append("geom")
    col_list = ", ".join(
        'ST_AsGeoJSON(geom) AS geom' if c == "geom" else f'"{c}"' for c in columns
    )

    filters: List[str] = []
    params: List[Any] = []
    if rayon and config_key == "field_table":
        filters.append("(rayon = %s OR rayon IS NULL)")
        params.append(normalize_rayon_name(rayon))
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    query = f'SELECT {col_list} FROM "{schema}"."{table}" {where} ORDER BY sent_at DESC'

    pg = _pg_connection(conn)
    if pg is None:
        return []

    rows: List[SnapshotRow] = []
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, params)
            for row in cur.fetchall():
                record = TaskRecord(
                    key=str(row[1]),
                    type=row[3] or "",
                    photo_uuid=_normalize_id_value(row[4]),
                    photo_lens=_normalize_id_value(row[5]),
                    ogh_id=_normalize_id_value(row[6]),
                    oati_id=_normalize_id_value(row[7]),
                    earthwork_id=_normalize_id_value(row[8]),
                    localwork_id=_normalize_id_value(row[9]),
                    avr_mos_id=_normalize_id_value(row[10]),
                    sps=_normalize_id_value(row[11]) if len(row) > 11 else None,
                    kgs=_normalize_id_value(row[12]) if len(row) > 12 else None,
                    station_avr=_normalize_id_value(row[13])
                    if len(row) > 13
                    else None,
                )
                office_comment = None
                rayon_value = None
                geom_json = None
                if include_office_comment and len(row) > 14 and row[14] is not None:
                    text = str(row[14]).strip()
                    office_comment = text or None
                if include_office_comment and len(row) > 15 and row[15] is not None:
                    text = str(row[15]).strip()
                    rayon_value = text or None
                if include_office_comment and len(row) > 16 and row[16] is not None:
                    geom_json = row[16]
                resolved = _find_subgroup_for_record(record, store_cfg)
                if resolved is None:
                    continue
                subgroup_name, _, _ = resolved
                group_name = row[3] or _find_group_name(subgroup_name, crm_cfg)
                sent_at = row[2]
                if isinstance(sent_at, datetime):
                    sent_str = sent_at.isoformat()
                else:
                    sent_str = str(sent_at or "")
                rows.append(
                    SnapshotRow(
                        snapshot_key=str(row[0]),
                        task_key=str(row[1]),
                        sent_at=sent_str or None,
                        record=record,
                        subgroup_name=subgroup_name,
                        group_name=group_name,
                        office_comment=office_comment,
                        rayon=rayon_value,
                        geom_json=geom_json,
                    )
                )
    except Exception as exc:
        _pg_rollback(pg)
        log_warning(f"Не удалось загрузить snapshot из {schema}.{table}: {exc}")
    return rows


def _subgroup_cfg(
    subgroup_name: str, crm_cfg: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    for group_cfg in crm_cfg.get("groups", []):
        for sub_cfg in group_cfg.get("subgroups", []):
            if sub_cfg.get("name") == subgroup_name:
                return sub_cfg
    return None


def build_district_feature_index(
    store_cfg: Dict[str, Any],
    crm_cfg: Dict[str, Any],
    district: DistrictBoundary,
    metric_crs,
) -> Dict[Tuple[str, str], TaskFeature]:
    """Индекс (subgroup_name, business_id) → TaskFeature внутри района."""
    index: Dict[Tuple[str, str], TaskFeature] = {}
    root = QgsProject.instance().layerTreeRoot()

    for subgroup_name, mapping in store_cfg.get("subgroups", {}).items():
        source_field = mapping.get("source_field")
        if not source_field:
            continue
        sub_cfg = _subgroup_cfg(subgroup_name, crm_cfg)
        if sub_cfg is None:
            continue
        layers, _ = resolve_layers(
            root,
            sub_cfg.get("layers", []),
            sub_cfg.get("groups", []),
        )
        for layer in layers:
            if layer.fields().indexOf(source_field) < 0:
                continue
            geometry_type = layer_geometry_type(layer)
            scoped = bool(mapping.get("scoped_geometry_id"))
            for feat in features_in_district(layer, district, metric_crs):
                raw_id = _normalize_id_value(feat[source_field])
                if not raw_id:
                    continue
                if scoped and geometry_type:
                    business_id = format_scoped_business_id(geometry_type, raw_id)
                else:
                    business_id = raw_id
                if not business_id:
                    continue
                lookup_key = (subgroup_name, business_id)
                if lookup_key in index:
                    continue
                index[lookup_key] = _feature_to_task(layer, feat)
    return index


def lookup_feature_in_index(
    snap: SnapshotRow,
    store_cfg: Dict[str, Any],
    feature_index: Dict[Tuple[str, str], TaskFeature],
) -> Optional[TaskFeature]:
    mapping = store_cfg.get("subgroups", {}).get(snap.subgroup_name)
    if not mapping:
        return None

    task_column = mapping.get("task_column")
    business_id = getattr(snap.record, task_column, None)
    if not business_id:
        return None

    base = feature_index.get((snap.subgroup_name, str(business_id).strip()))
    if base is None:
        return None

    attrs = dict(base.attributes)
    attrs["_task_key"] = snap.task_key
    attrs["_snapshot_key"] = snap.snapshot_key
    if snap.sent_at:
        attrs["_sent_at"] = snap.sent_at
    if snap.office_comment:
        attrs["_office_comment"] = snap.office_comment
    return TaskFeature(
        layer=base.layer,
        layer_name=base.layer_name,
        feature_id=base.feature_id,
        attributes=attrs,
        task_key=snap.task_key,
        sent_at=snap.sent_at,
        task_geom=base.task_geom,
    )


def lookup_feature_in_layers(
    snap: SnapshotRow,
    store_cfg: Dict[str, Any],
    crm_cfg: Dict[str, Any],
    conn: Optional[DatabaseConnection] = None,
) -> Optional[TaskFeature]:
    """Найти геометрию задачи: task_key в БД, затем business_id в слоях."""
    if snap.geom_json:
        import json

        geom = json.loads(snap.geom_json) if isinstance(snap.geom_json, str) else snap.geom_json
        return TaskFeature(
            layer=None,
            layer_name=snap.subgroup_name,
            feature_id=None,
            attributes={"_task_key": snap.task_key, "_snapshot_key": snap.snapshot_key},
            task_key=snap.task_key,
            sent_at=snap.sent_at,
            task_geom=geom,
        )
    if conn is not None:
        feat = _lookup_feature_by_task_key_db(conn, snap, store_cfg, crm_cfg)
        if feat is not None:
            return feat

    mapping = store_cfg.get("subgroups", {}).get(snap.subgroup_name)
    if not mapping:
        return None

    source_field = mapping.get("source_field")
    task_column = mapping.get("task_column")
    business_id = getattr(snap.record, task_column, None)
    if not source_field or not business_id:
        return None

    sub_cfg = _subgroup_cfg(snap.subgroup_name, crm_cfg)
    if sub_cfg is None:
        return None

    root = QgsProject.instance().layerTreeRoot()
    layers, _ = resolve_layers(
        root,
        sub_cfg.get("layers", []),
        sub_cfg.get("groups", []),
    )

    business_text = str(business_id).strip()
    scoped = bool(mapping.get("scoped_geometry_id"))
    prefix, raw_business_id = parse_scoped_business_id(business_text)

    for layer in layers:
        if scoped and prefix and layer_geometry_type(layer) != prefix:
            continue
        field_idx = layer.fields().indexOf(source_field)
        if field_idx < 0:
            continue
        lookup_id = raw_business_id if scoped else business_text
        for feat in layer.getFeatures():
            val = _normalize_id_value(feat[source_field])
            if val != lookup_id:
                continue
            attrs = {f.name(): feat[f.name()] for f in feat.fields()}
            attrs["_task_key"] = snap.task_key
            attrs["_snapshot_key"] = snap.snapshot_key
            if snap.sent_at:
                attrs["_sent_at"] = snap.sent_at
            if snap.office_comment:
                attrs["_office_comment"] = snap.office_comment
            return TaskFeature(
                layer=layer,
                layer_name=layer.name(),
                feature_id=feat.id(),
                attributes=attrs,
                task_key=snap.task_key,
                sent_at=snap.sent_at,
            )
    if scoped:
        link_field = mapping.get("link_lookup_field")
        if link_field:
            for layer in layers:
                field_idx = layer.fields().indexOf(link_field)
                if field_idx < 0:
                    continue
                for feat in layer.getFeatures():
                    val = _normalize_id_value(feat[link_field])
                    if val != business_text:
                        continue
                    attrs = {f.name(): feat[f.name()] for f in feat.fields()}
                    attrs["_task_key"] = snap.task_key
                    attrs["_snapshot_key"] = snap.snapshot_key
                    if snap.sent_at:
                        attrs["_sent_at"] = snap.sent_at
                    if snap.office_comment:
                        attrs["_office_comment"] = snap.office_comment
                    return TaskFeature(
                        layer=layer,
                        layer_name=layer.name(),
                        feature_id=feat.id(),
                        attributes=attrs,
                        task_key=snap.task_key,
                        sent_at=snap.sent_at,
                    )
    return None


def lookup_feature_in_project(
    snap: SnapshotRow,
    store_cfg: Dict[str, Any],
    crm_cfg: Dict[str, Any],
    district: DistrictBoundary,
    metric_crs,
) -> Optional[TaskFeature]:
    mapping = store_cfg.get("subgroups", {}).get(snap.subgroup_name)
    if not mapping:
        return None

    source_field = mapping.get("source_field")
    task_column = mapping.get("task_column")
    business_id = getattr(snap.record, task_column, None)
    if not source_field or not business_id:
        return None

    sub_cfg = _subgroup_cfg(snap.subgroup_name, crm_cfg)
    if sub_cfg is None:
        return None

    root = QgsProject.instance().layerTreeRoot()
    layers, _ = resolve_layers(
        root,
        sub_cfg.get("layers", []),
        sub_cfg.get("groups", []),
    )

    business_text = str(business_id).strip()
    scoped = bool(mapping.get("scoped_geometry_id"))
    prefix, raw_business_id = parse_scoped_business_id(business_text)

    for layer in layers:
        if scoped and prefix and layer_geometry_type(layer) != prefix:
            continue
        field_idx = layer.fields().indexOf(source_field)
        if field_idx < 0:
            continue
        lookup_id = raw_business_id if scoped else business_text
        for feat in features_in_district(layer, district, metric_crs):
            val = _normalize_id_value(feat[source_field])
            if val != lookup_id:
                continue

            attrs = {f.name(): feat[f.name()] for f in feat.fields()}
            attrs["_task_key"] = snap.task_key
            attrs["_snapshot_key"] = snap.snapshot_key
            if snap.sent_at:
                attrs["_sent_at"] = snap.sent_at
            if snap.office_comment:
                attrs["_office_comment"] = snap.office_comment
            return TaskFeature(
                layer=layer,
                layer_name=layer.name(),
                feature_id=feat.id(),
                attributes=attrs,
                task_key=snap.task_key,
                sent_at=snap.sent_at,
            )
    return None


def collect_snapshot_tasks(
    conn: DatabaseConnection,
    district: DistrictBoundary,
    source: str,
    config: Dict[str, Any],
) -> TaskResult:
    if source not in SNAPSHOT_SOURCES:
        raise ValueError(f"Unknown snapshot source: {source}")

    store_cfg = crm_task_store(config)
    crm_cfg = crm_tasks(config)
    config_key, default_table = SNAPSHOT_SOURCES[source]

    lookback_days = int(crm_cfg.get("date_lookback_days", 3))
    date_from, date_to = _date_filter_range(lookback_days)
    metric_crs_name = crm_cfg.get("metric_crs", "EPSG:32637")
    from qgis.core import QgsCoordinateReferenceSystem

    metric_crs = QgsCoordinateReferenceSystem(metric_crs_name)

    snapshot_rows = fetch_snapshot_rows(
        conn, store_cfg, config_key, default_table, crm_cfg, rayon=district.name
    )

    feature_index = build_district_feature_index(
        store_cfg, crm_cfg, district, metric_crs
    )
    district_norm = normalize_rayon_name(district.name)

    groups_map: Dict[str, Dict[str, List[TaskFeature]]] = {}
    for snap in snapshot_rows:
        if snap.rayon and normalize_rayon_name(snap.rayon) == district_norm:
            feat = lookup_feature_in_layers(snap, store_cfg, crm_cfg, conn=conn)
        else:
            feat = lookup_feature_in_index(snap, store_cfg, feature_index)
        if feat is None:
            continue
        groups_map.setdefault(snap.group_name, {}).setdefault(
            snap.subgroup_name, []
        ).append(feat)

    result = TaskResult(
        district_name=district.name,
        filter_date_from=date_from,
        filter_date_to=date_to,
        apply_date_filter=False,
        task_source=source,
    )

    for group_cfg in crm_cfg.get("groups", []):
        group_name = group_cfg.get("name", "")
        sub_map = groups_map.get(group_name, {})
        if not sub_map:
            continue
        group = TaskGroup(name=group_name)
        for sub_cfg in group_cfg.get("subgroups", []):
            sub_name = sub_cfg.get("name", "")
            features = sub_map.get(sub_name, [])
            if features:
                group.subgroups.append(
                    TaskSubgroup(name=sub_name, features=features)
                )
        if group.subgroups:
            result.groups.append(group)

    return result

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
)
from .crm_tasks import (
    TaskFeature,
    TaskGroup,
    TaskResult,
    TaskSubgroup,
    _date_filter_range,
)
from .crm_ui_constants import SNAPSHOT_SOURCES
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
) -> List[SnapshotRow]:
    schema, table = _snapshot_table_ref(store_cfg, config_key, default_table)
    columns = ["key", "task_key", "sent_at", "type"] + list(TASK_ID_COLUMNS) + [
        "sps",
        "kgs",
        "station_avr",
    ]
    col_list = ", ".join(f'"{c}"' for c in columns)
    query = f'SELECT {col_list} FROM "{schema}"."{table}" ORDER BY sent_at DESC'

    pg = _pg_connection(conn)
    if pg is None:
        return []

    rows: List[SnapshotRow] = []
    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query)
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

    for layer in layers:
        field_idx = layer.fields().indexOf(source_field)
        if field_idx < 0:
            continue
        for feat in features_in_district(layer, district, metric_crs):
            val = _normalize_id_value(feat[source_field])
            if val != business_text:
                continue

            attrs = {f.name(): feat[f.name()] for f in feat.fields()}
            attrs["_task_key"] = snap.task_key
            attrs["_snapshot_key"] = snap.snapshot_key
            if snap.sent_at:
                attrs["_sent_at"] = snap.sent_at
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
        conn, store_cfg, config_key, default_table, crm_cfg
    )

    groups_map: Dict[str, Dict[str, List[TaskFeature]]] = {}
    for snap in snapshot_rows:
        feat = lookup_feature_in_project(
            snap, store_cfg, crm_cfg, district, metric_crs
        )
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

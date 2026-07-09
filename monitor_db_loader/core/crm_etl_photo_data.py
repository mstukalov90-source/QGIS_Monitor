# -*- coding: utf-8 -*-
"""Load ETL-synced photo tasks (genplan + lens) from crm.tasks JOIN."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .crm_db_tasks import (
    ETL_PHOTO_TASK_COLUMNS,
    PHOTO_SUBGROUP_LAYER_NAMES,
    collect_db_subgroup_tasks,
)
from .crm_task_store import CRM_GROUP_DISRUPTIONS
from .crm_tasks import TaskGroup, TaskResult, TaskSubgroup
from .crm_ui_constants import (
    AI_PHOTO_SUBGROUP,
    LENS_PHOTO_SUBGROUP,
)
from .db import DatabaseConnection
from .district_utils import DistrictBoundary

ETL_SYNC_SOURCE = "etl_sync"

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


def collect_etl_sync_subgroup_tasks(
    conn: DatabaseConnection,
    district: DistrictBoundary,
    metric_srid: int,
    subgroup_name: str,
    store_cfg: Dict[str, Any],
    config: Dict[str, Any],
    *,
    sub_cfg: Optional[Dict[str, Any]] = None,
    **kwargs,
):
    """Backward-compatible wrapper around collect_db_subgroup_tasks."""
    if sub_cfg is None:
        crm_cfg = config.get("crm_tasks", {})
        for group_cfg in crm_cfg.get("groups", []):
            for candidate in group_cfg.get("subgroups", []):
                if candidate.get("name") == subgroup_name:
                    sub_cfg = candidate
                    break
            if sub_cfg is not None:
                break
    if sub_cfg is None:
        return [], [f"Subgroup config not found: {subgroup_name}"]
    if not is_etl_sync_cfg(sub_cfg) and not is_etl_photo_subgroup(
        store_cfg, subgroup_name
    ):
        return [], [f"Subgroup is not etl_sync: {subgroup_name}"]
    return collect_db_subgroup_tasks(
        conn,
        district,
        metric_srid,
        subgroup_name,
        store_cfg,
        config,
        sub_cfg,
        **kwargs,
    )


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
    """Deprecated: ETL photo tasks are loaded in _build_task_result_from_db."""
    del conn, district, store_cfg, metric_srid, config, result

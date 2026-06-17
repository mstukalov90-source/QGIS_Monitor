# -*- coding: utf-8 -*-
"""Разрешение слоёв для выбора ID с карты в crm.tasks."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from qgis.core import QgsProject, QgsVectorLayer

from .config import crm_task_store, crm_tasks
from .district_utils import resolve_layers


@dataclass
class PickTarget:
    subgroup_name: str
    task_column: str
    source_field: str
    layers: List[QgsVectorLayer] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)


def _subgroup_name_for_column(
    store_cfg: Dict[str, Any], task_column: str
) -> Optional[str]:
    for subgroup_name, mapping in store_cfg.get("subgroups", {}).items():
        if mapping.get("task_column") == task_column:
            return subgroup_name
    return None


def _find_subgroup_cfg(
    crm_cfg: Dict[str, Any], subgroup_name: str
) -> Optional[Dict[str, Any]]:
    for group_cfg in crm_cfg.get("groups", []):
        for sub_cfg in group_cfg.get("subgroups", []):
            if sub_cfg.get("name") == subgroup_name:
                return sub_cfg
    return None


def resolve_pick_target(
    config: Dict[str, Any],
    task_column: str,
    root=None,
) -> Optional[PickTarget]:
    """Вернуть слои и source_field для выбора значения task_column с карты."""
    store_cfg = crm_task_store(config)
    subgroup_name = _subgroup_name_for_column(store_cfg, task_column)
    if subgroup_name is None:
        return None

    mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
    source_field = mapping.get("source_field")
    if not source_field:
        return None

    sub_cfg = _find_subgroup_cfg(crm_tasks(config), subgroup_name)
    if sub_cfg is None:
        return PickTarget(
            subgroup_name=subgroup_name,
            task_column=task_column,
            source_field=source_field,
        )

    if root is None:
        root = QgsProject.instance().layerTreeRoot()

    layer_names = sub_cfg.get("layers", [])
    group_names = sub_cfg.get("groups", [])
    layers, missing = resolve_layers(root, layer_names, group_names)

    return PickTarget(
        subgroup_name=subgroup_name,
        task_column=task_column,
        source_field=source_field,
        layers=layers,
        missing=missing,
    )

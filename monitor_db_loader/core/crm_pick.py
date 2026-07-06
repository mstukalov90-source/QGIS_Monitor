# -*- coding: utf-8 -*-
"""Разрешение слоёв для выбора ID с карты в crm.tasks."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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


@dataclass
class LayerPickInfo:
    task_column: str
    source_field: str
    subgroup_name: str


@dataclass
class LinkPickBundle:
    layers: List[QgsVectorLayer] = field(default_factory=list)
    layer_info: Dict[str, LayerPickInfo] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    subgroup_names: List[str] = field(default_factory=list)


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


def _lookup_field_for_links(mapping: Dict[str, Any]) -> Optional[str]:
    return mapping.get("link_lookup_field") or mapping.get("source_field")


def _layers_for_link_search(
    root,
    subgroup_name: str,
    sub_cfg: Dict[str, Any],
    mapping: Dict[str, Any],
) -> Tuple[List[QgsVectorLayer], List[str]]:
    if mapping.get("link_lookup_field"):
        prefix = f"{subgroup_name} —"
        layers = [
            layer
            for layer in QgsProject.instance().mapLayers().values()
            if isinstance(layer, QgsVectorLayer)
            and layer.name().startswith(prefix)
        ]
        return layers, []
    return resolve_layers(
        root,
        sub_cfg.get("layers", []),
        sub_cfg.get("groups", []),
    )


def resolve_pick_target(
    config: Dict[str, Any],
    task_column: str,
    root=None,
    *,
    for_link_pick: bool = False,
) -> Optional[PickTarget]:
    """Вернуть слои и source_field для выбора значения task_column с карты."""
    store_cfg = crm_task_store(config)
    subgroup_name = _subgroup_name_for_column(store_cfg, task_column)
    if subgroup_name is None:
        return None

    mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
    if for_link_pick:
        source_field = _lookup_field_for_links(mapping)
    else:
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

    if for_link_pick and mapping.get("link_lookup_field"):
        layers, missing = _layers_for_link_search(
            root, subgroup_name, sub_cfg, mapping
        )
    else:
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


def resolve_link_pick_bundle(
    config: Dict[str, Any],
    link_columns: List[str],
    root=None,
) -> Optional[LinkPickBundle]:
    """Слои и маппинг layer_id → столбец для выбора сопоставления с карты."""
    if not link_columns:
        return None

    all_layers: List[QgsVectorLayer] = []
    layer_info: Dict[str, LayerPickInfo] = {}
    missing: List[str] = []
    subgroup_names: List[str] = []
    seen_layer_ids: set = set()

    for task_column in link_columns:
        target = resolve_pick_target(
            config, task_column, root=root, for_link_pick=True
        )
        if target is None:
            continue
        if target.subgroup_name not in subgroup_names:
            subgroup_names.append(target.subgroup_name)
        missing.extend(target.missing)
        for layer in target.layers:
            layer_id = layer.id()
            if layer_id in seen_layer_ids:
                continue
            seen_layer_ids.add(layer_id)
            all_layers.append(layer)
            layer_info[layer_id] = LayerPickInfo(
                task_column=target.task_column,
                source_field=target.source_field,
                subgroup_name=target.subgroup_name,
            )

    if not all_layers:
        return LinkPickBundle(layers=[], layer_info={}, missing=missing)

    return LinkPickBundle(
        layers=all_layers,
        layer_info=layer_info,
        missing=missing,
        subgroup_names=subgroup_names,
    )

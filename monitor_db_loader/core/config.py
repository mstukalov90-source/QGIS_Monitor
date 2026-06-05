# -*- coding: utf-8 -*-
"""Load and expose plugin JSON configuration."""

import json
import os
from typing import Any, Dict, List, Optional

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "resources", "layers_config.json")
LOG_CHANNEL = "Monitor DB Loader"

_GEOM_CANDIDATES = ("geom", "geometry", "the_geom", "wkb_geometry")


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def database_connection(config: Dict[str, Any]) -> Dict[str, Any]:
    return config["database_connection"]


def layer_groups(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    return config.get("layer_groups", [])


def ungrouped_layers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    return config.get("ungrouped_layers", [])


def ungrouped_layer_groups(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    return config.get("ungrouped_groups", [])


def additional_functionality(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("additional_functionality", {})


def load_on_startup(config: Dict[str, Any]) -> bool:
    return bool(additional_functionality(config).get("load_on_startup", False))


def parse_table_name(table_name: str) -> tuple:
    """Return (schema, table) from 'table' or 'schema.table'."""
    if "." in table_name:
        schema, table = table_name.split(".", 1)
        return schema, table
    return "public", table_name


# Устаревшие имена из первой версии конфига (до обхода схем БД)
_LEGACY_LAYER_SOURCES = {
    "response.photo_geom": ("genplan", "photo_meta", "geom"),
    "response.geom": ("genplan", "order", "geom"),
    "boundaries_aip.geom_valid": ("stroymonitoring", "boundaries_aip", "geom_valid"),
}


def resolve_layer_source(layer_def: Dict[str, Any]) -> tuple:
    """
    Return (schema, table, geometry_column).

    geometry_column may be None — тогда столбец определяется автоматически.
    """
    table_name = layer_def.get("table_name", "")
    schema = layer_def.get("schema")
    geom = layer_def.get("geometry_column")

    if schema:
        return str(schema), str(table_name), geom

    if table_name in _LEGACY_LAYER_SOURCES:
        return _LEGACY_LAYER_SOURCES[table_name]

    if "." in table_name:
        s, t = table_name.split(".", 1)
        return s, t, geom

    return "public", table_name, geom


def normalize_geometry_type(geometry_type: Any) -> List[str]:
    if isinstance(geometry_type, list):
        return [str(g).lower() for g in geometry_type]
    return [str(geometry_type).lower()]


def is_mixed_geometry(geometry_type: Any) -> bool:
    return isinstance(geometry_type, list) and len(geometry_type) > 1


def geom_column_candidates() -> tuple:
    return _GEOM_CANDIDATES


def _iter_group_layers(
    group_def: Dict[str, Any],
    path: Optional[str] = None,
):
    """Yield (group_path, layer_def) for layers in group_def and nested groups."""
    name = group_def.get("group_name", "")
    current_path = f"{path}/{name}" if path else name

    for layer_def in group_def.get("layers", []):
        yield current_path or None, layer_def

    for child in group_def.get("groups", []):
        yield from _iter_group_layers(child, current_path)


def iter_all_layer_defs(config: Dict[str, Any]):
    """Yield (group_path or None, layer_def) for every configured layer."""
    for group in layer_groups(config):
        yield from _iter_group_layers(group)

    for layer_def in ungrouped_layers(config):
        yield None, layer_def

    for group in ungrouped_layer_groups(config):
        yield from _iter_group_layers(group)


def count_layers(config: Dict[str, Any]) -> int:
    return sum(1 for _ in iter_all_layer_defs(config))

# -*- coding: utf-8 -*-
"""Подложка OpenStreetMap (XYZ) как нижний слой проекта QGIS."""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

from qgis.core import QgsProject, QgsRasterLayer

from .config import additional_functionality
from .log_util import log_info, log_warning

_DEFAULT_BASEMAP = {
    "enabled": True,
    "display_name": "OpenStreetMap",
    "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "zmin": 0,
    "zmax": 19,
}


def _basemap_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = additional_functionality(config).get("basemap", {})
    if not isinstance(raw, dict):
        raw = {}
    merged = dict(_DEFAULT_BASEMAP)
    merged.update(raw)
    return merged


def _find_basemap_layer(display_name: str) -> Optional[QgsRasterLayer]:
    project = QgsProject.instance()
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsRasterLayer):
            continue
        if layer.name() == display_name:
            return layer
    return None


def _move_layer_to_bottom(layer: QgsRasterLayer) -> None:
    root = QgsProject.instance().layerTreeRoot()
    node = root.findLayer(layer.id())
    if node is None:
        return
    parent = node.parent()
    if parent is None:
        return
    clone = node.clone()
    parent.insertChildNode(parent.children().__len__(), clone)
    parent.removeChildNode(node)


def ensure_osm_basemap(config: Dict[str, Any]) -> Optional[str]:
    """Добавить или переиспользовать OSM-подложку; вернуть layer id."""
    settings = _basemap_settings(config)
    if not settings.get("enabled", True):
        return None

    display_name = str(settings.get("display_name") or "OpenStreetMap").strip()
    url = str(settings.get("url") or _DEFAULT_BASEMAP["url"]).strip()
    zmin = int(settings.get("zmin", 0))
    zmax = int(settings.get("zmax", 19))

    existing = _find_basemap_layer(display_name)
    if existing is not None and existing.isValid():
        _move_layer_to_bottom(existing)
        log_info(f"Подложка «{display_name}» уже в проекте")
        return existing.id()

    encoded_url = quote(url, safe=":/?=&{}")
    uri = f"type=xyz&url={encoded_url}&zmin={zmin}&zmax={zmax}"
    layer = QgsRasterLayer(uri, display_name, "wms")
    if not layer.isValid():
        log_warning(
            f"Не удалось загрузить подложку «{display_name}»: "
            f"{layer.error().message() if layer.error() else 'неизвестная ошибка'}"
        )
        return None

    project = QgsProject.instance()
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    root.addLayer(layer)
    log_info(f"Подложка «{display_name}» добавлена")
    return layer.id()

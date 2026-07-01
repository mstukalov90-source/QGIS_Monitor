# -*- coding: utf-8 -*-
"""CRS, extent and post-load fixes for vector layers."""

from typing import Any, Dict, Iterable, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayerTree,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)

from .config import additional_functionality
from .log_util import log_info, log_warning

# СК хранения в PostGIS и memory-слоёв (не менять при смене display_crs).
STORAGE_CRS = QgsCoordinateReferenceSystem("EPSG:4326")
MOSCOW_CRS = STORAGE_CRS

DEFAULT_DISPLAY_CRS = "EPSG:3857"
DISPLAY_CRS = QgsCoordinateReferenceSystem(DEFAULT_DISPLAY_CRS)

MOSCOW_EXTENT_WGS84 = QgsRectangle(36.8, 55.1, 38.0, 56.1)
MOSCOW_EXTENT = MOSCOW_EXTENT_WGS84


def display_crs_from_config(config: Optional[Dict[str, Any]] = None) -> QgsCoordinateReferenceSystem:
    authid = DEFAULT_DISPLAY_CRS
    if config is not None:
        authid = str(
            additional_functionality(config).get("display_crs") or DEFAULT_DISPLAY_CRS
        ).strip() or DEFAULT_DISPLAY_CRS
    crs = QgsCoordinateReferenceSystem(authid)
    if crs.isValid():
        return crs
    log_warning(f"Некорректная display_crs «{authid}», используется {DEFAULT_DISPLAY_CRS}")
    return QgsCoordinateReferenceSystem(DEFAULT_DISPLAY_CRS)


def _extent_for_crs(rect_wgs84: QgsRectangle, dest_crs: QgsCoordinateReferenceSystem) -> QgsRectangle:
    if not dest_crs.isValid() or dest_crs == STORAGE_CRS:
        return QgsRectangle(rect_wgs84)
    return QgsCoordinateTransform(STORAGE_CRS, dest_crs, QgsProject.instance()).transform(
        rect_wgs84
    )


def ensure_project_crs(config: Optional[Dict[str, Any]] = None) -> QgsCoordinateReferenceSystem:
    """Установить СК отображения проекта (display_crs); storage остаётся EPSG:4326."""
    project = QgsProject.instance()
    target = display_crs_from_config(config)
    if not target.isValid():
        target = DISPLAY_CRS
    current = project.crs()
    if not current.isValid() or current != target:
        project.setCrs(target)
        log_info(f"СК проекта установлена: {target.authid()}")
    return target


def finalize_vector_layer(layer: QgsVectorLayer) -> None:
    """CRS и охват после загрузки."""
    if not layer.crs().isValid():
        layer.setCrs(MOSCOW_CRS)
    layer.updateExtents()
    provider = layer.dataProvider()
    if provider and hasattr(provider, "forceReload"):
        try:
            provider.forceReload()
        except Exception:
            pass


def _collect_visible_layers(node) -> list:
    layers = []
    if QgsLayerTree.isLayer(node):
        if node.isVisible():
            lyr = node.layer()
            if lyr and lyr.isValid():
                layers.append(lyr)
    else:
        for child in node.children():
            layers.extend(_collect_visible_layers(child))
    return layers


def refresh_map_canvas(iface) -> None:
    """Синхронизировать холст с деревом слоёв (иначе карта может быть пустой)."""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    layers = []
    for child in root.children():
        layers.extend(_collect_visible_layers(child))
    layers.reverse()

    canvas = iface.mapCanvas()
    canvas.setLayers(layers)
    canvas.refreshAllLayers()
    log_info(f"Холст карты: {len(layers)} видимых слоёв")


def zoom_map_to_layers(
    iface,
    layer_ids: Iterable[str],
    config: Optional[Dict[str, Any]] = None,
) -> None:
    """Подогнать карту под охват загруженных слоёв."""
    project = QgsProject.instance()
    dest_crs = ensure_project_crs(config)

    combined = QgsRectangle()
    combined.setNull()
    count = 0

    for layer_id in layer_ids:
        layer = project.mapLayer(layer_id)
        if not layer or not layer.isValid():
            continue
        finalize_vector_layer(layer)
        ext = layer.extent()
        if ext.isNull() or ext.isEmpty():
            log_warning(f"Пустой охват: {layer.name()}")
            continue

        if layer.crs().isValid() and dest_crs.isValid() and layer.crs() != dest_crs:
            ext = QgsCoordinateTransform(
                layer.crs(), dest_crs, project
            ).transform(ext)

        if combined.isNull():
            combined = QgsRectangle(ext)
        else:
            combined.combineExtentWith(ext)
        count += 1

    if combined.isNull() or combined.isEmpty():
        log_warning("Охват по слоям пуст — используется охват Москвы")
        combined = _extent_for_crs(MOSCOW_EXTENT_WGS84, dest_crs)

    combined.scale(1.1)
    iface.mapCanvas().setExtent(combined)
    refresh_map_canvas(iface)
    log_info(
        f"Масштаб: {count} слоёв, центр "
        f"({combined.center().x():.4f}, {combined.center().y():.4f})"
    )

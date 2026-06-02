# -*- coding: utf-8 -*-
"""CRS, extent and post-load fixes for vector layers."""

from typing import Iterable

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayerTree,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)

from .log_util import log_info, log_warning

MOSCOW_CRS = QgsCoordinateReferenceSystem("EPSG:4326")
MOSCOW_EXTENT = QgsRectangle(36.8, 55.1, 38.0, 56.1)


def ensure_project_crs() -> QgsCoordinateReferenceSystem:
    project = QgsProject.instance()
    crs = project.crs()
    if not crs.isValid():
        project.setCrs(MOSCOW_CRS)
        log_info("СК проекта установлена: EPSG:4326")
        return MOSCOW_CRS
    return crs


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


def zoom_map_to_layers(iface, layer_ids: Iterable[str]) -> None:
    """Подогнать карту под охват загруженных слоёв."""
    project = QgsProject.instance()
    dest_crs = ensure_project_crs()

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
        combined = QgsRectangle(MOSCOW_EXTENT)

    combined.scale(1.1)
    iface.mapCanvas().setExtent(combined)
    refresh_map_canvas(iface)
    log_info(
        f"Масштаб: {count} слоёв, центр "
        f"({combined.center().x():.4f}, {combined.center().y():.4f})"
    )

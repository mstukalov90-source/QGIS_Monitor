# -*- coding: utf-8 -*-
"""Отображение площадных заказов crm.tasks_area на карте QGIS."""

from typing import List, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFields,
    QgsFillSymbol,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsRubberBand

from .crm_tasks import TaskFeature, TaskResult
from .crm_tasks_area import get_area_geometries
from .crm_ui_constants import normalize_rayon_name
from .db import DatabaseConnection
from .layer_utils import refresh_map_canvas
from .qt_compat import qgs_field

AREA_GROUP_NAME = "Monitor CRM — площадные"
OVERLAY_LAYER_NAME = "Площадные заказы (контур)"
ACTIVE_LAYER_NAME = "Площадные заказы"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

AREA_OUTLINE_COLOR = "#0066cc"
AREA_SELECTION_FILL = QColor(255, 243, 224, 100)
AREA_SELECTION_STROKE = QColor(255, 152, 0)


def _area_features_from_result(result: TaskResult) -> List[TaskFeature]:
    features: List[TaskFeature] = []
    for group in result.groups:
        for subgroup in group.subgroups:
            for feat in subgroup.features:
                if feat.area_geom and not feat.area_geom.isEmpty():
                    features.append(feat)
    return features


def _create_area_symbol() -> QgsFillSymbol:
    symbol = QgsFillSymbol.createSimple(
        {
            "color": "0,0,0,0",
            "outline_color": "0,102,204",
            "outline_width": "0.8",
        }
    )
    return symbol


def _build_memory_layer(name: str, features: List[TaskFeature]) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer("Multipolygon?crs=EPSG:4326", name, "memory")
    if not layer.isValid():
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", name, "memory")
    if not layer.isValid():
        return None

    layer.setRenderer(QgsSingleSymbolRenderer(_create_area_symbol()))
    provider = layer.dataProvider()

    fields = QgsFields()
    fields.append(qgs_field("task_key", QVariant.String))
    fields.append(qgs_field("status", QVariant.String))
    fields.append(qgs_field("fid", QVariant.String))
    provider.addAttributes(fields.toList())
    layer.updateFields()

    qgs_features: List[QgsFeature] = []
    for feat in features:
        if not feat.area_geom or feat.area_geom.isEmpty():
            continue
        qgs_feat = QgsFeature(fields)
        qgs_feat.setGeometry(QgsGeometry(feat.area_geom))
        attrs = feat.attributes or {}
        qgs_feat.setAttributes(
            [
                str(feat.task_key or attrs.get("key") or ""),
                str(attrs.get("status") or ""),
                str(attrs.get("fid") or ""),
            ]
        )
        qgs_features.append(qgs_feat)

    if qgs_features:
        provider.addFeatures(qgs_features)
    layer.updateExtents()
    return layer


class TasksAreaMapController:
    """Overlay / active layer / selection highlight для площадных заказов."""

    def __init__(self, iface, district_name: str):
        self._iface = iface
        self._district_name = normalize_rayon_name(district_name)
        self._overlay_layer: Optional[QgsVectorLayer] = None
        self._active_layer: Optional[QgsVectorLayer] = None
        self._selection_band: Optional[QgsRubberBand] = None
        self._overlay_count: int = 0

    @property
    def overlay_count(self) -> int:
        return self._overlay_count

    def _canvas(self):
        return self._iface.mapCanvas() if self._iface else None

    def _ensure_selection_band(self) -> Optional[QgsRubberBand]:
        canvas = self._canvas()
        if canvas is None:
            return None
        if self._selection_band is None:
            self._selection_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            self._selection_band.setColor(AREA_SELECTION_STROKE)
            self._selection_band.setWidth(3)
            self._selection_band.setFillColor(AREA_SELECTION_FILL)
        return self._selection_band

    def _remove_layer(self, layer: Optional[QgsVectorLayer]) -> None:
        if layer is None:
            return
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(AREA_GROUP_NAME)
        if group:
            node = group.findLayer(layer.id())
            if node:
                group.removeChildNode(node)
        QgsProject.instance().removeMapLayer(layer.id())

    def _clear_layers(self) -> None:
        self._remove_layer(self._overlay_layer)
        self._remove_layer(self._active_layer)
        self._overlay_layer = None
        self._active_layer = None
        self._overlay_count = 0

    def _add_layer(self, layer: QgsVectorLayer) -> None:
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(AREA_GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, AREA_GROUP_NAME)
        QgsProject.instance().addMapLayer(layer, False)
        group.addLayer(layer)
        layer.setCustomProperty("monitor_crm_temp_layer", True)

    def clear_selection(self) -> None:
        if self._selection_band is not None:
            self._selection_band.reset(QgsWkbTypes.PolygonGeometry)

    def highlight_feature(self, task_feat: Optional[TaskFeature]) -> None:
        band = self._ensure_selection_band()
        if band is None:
            return
        band.reset(QgsWkbTypes.PolygonGeometry)
        if (
            task_feat is None
            or not task_feat.area_geom
            or task_feat.area_geom.isEmpty()
        ):
            return

        geom = QgsGeometry(task_feat.area_geom)
        canvas = self._canvas()
        if canvas is None:
            return
        dest_crs = canvas.mapSettings().destinationCrs()
        if WGS84.isValid() and dest_crs.isValid() and WGS84 != dest_crs:
            geom.transform(
                QgsCoordinateTransform(WGS84, dest_crs, QgsProject.instance())
            )
        band.addGeometry(geom, None)
        band.show()

    def _fit_to_layer(self, layer: Optional[QgsVectorLayer]) -> None:
        if layer is None or not layer.isValid():
            return
        canvas = self._canvas()
        if canvas is None:
            return
        extent = layer.extent()
        if extent.isNull() or extent.isEmpty():
            return
        rect = QgsRectangle(extent)
        rect.scale(1.2)
        canvas.setExtent(rect)
        refresh_map_canvas(self._iface)

    def show_overlay(self, conn: DatabaseConnection) -> None:
        self._remove_layer(self._active_layer)
        self._active_layer = None
        self._remove_layer(self._overlay_layer)
        self._overlay_layer = None

        rows = get_area_geometries(conn, self._district_name, status=None)
        from .crm_tasks_area import _rows_to_features

        features = _rows_to_features(rows)
        self._overlay_count = len(features)
        layer = _build_memory_layer(OVERLAY_LAYER_NAME, features)
        if layer is None:
            return
        self._overlay_layer = layer
        self._add_layer(layer)

    def show_active(self, result: TaskResult, *, fit: bool = False) -> None:
        self._remove_layer(self._overlay_layer)
        self._overlay_layer = None
        self._remove_layer(self._active_layer)
        self._active_layer = None

        features = _area_features_from_result(result)
        self._overlay_count = 0
        layer = _build_memory_layer(ACTIVE_LAYER_NAME, features)
        if layer is None:
            return
        self._active_layer = layer
        self._add_layer(layer)
        if fit:
            self._fit_to_layer(layer)

    def refresh(
        self,
        task_source: str,
        result: TaskResult,
        conn: Optional[DatabaseConnection],
        *,
        is_area_source: bool,
    ) -> None:
        from .crm_ui_constants import is_area_source as _is_area_source

        if _is_area_source(task_source):
            self.show_active(result, fit=True)
        elif conn is not None and self._district_name:
            self.show_overlay(conn)
        else:
            self._clear_layers()
        refresh_map_canvas(self._iface)

    def clear(self) -> None:
        self.clear_selection()
        if self._selection_band is not None:
            self._selection_band.setCanvas(None)
            self._selection_band = None
        self._clear_layers()
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(AREA_GROUP_NAME)
        if group and group.children():
            return
        if group:
            root.removeChildNode(group)

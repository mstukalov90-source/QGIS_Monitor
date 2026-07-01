# -*- coding: utf-8 -*-
"""Слой точек «Задачи из камерального анализа» на карте QGIS (постоянный)."""

from __future__ import annotations

from typing import List, Optional

from qgis.core import (
    QgsFeature,
    QgsFields,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsProject,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

from .crm_office_data import collect_office_data_tasks
from .crm_tasks import TaskFeature, TaskResult
from .crm_ui_constants import OFFICE_DATA_LAYER_KEY, OFFICE_DATA_SUBGROUP
from .db import DatabaseConnection
from .district_utils import DistrictBoundary
from .layer_utils import refresh_map_canvas
from .log_util import log_info
from .qt_compat import qgs_field

OFFICE_POINTS_GROUP_NAME = "Monitor CRM — камеральный анализ"
OFFICE_POINTS_LAYER_NAME = "Точки камерального анализа"
OFFICE_POINTS_LAYER_PROP = "monitor_crm_office_points_layer"

POINT_FILL = QColor(0, 102, 204)
POINT_STROKE = QColor(255, 102, 0)


def get_office_points_map_controller(iface) -> "OfficePointsMapController":
    ctrl = getattr(iface, "_monitor_office_points_map", None)
    if ctrl is None:
        ctrl = OfficePointsMapController(iface)
        iface._monitor_office_points_map = ctrl
    return ctrl


def refresh_office_points_on_map(
    iface,
    conn: DatabaseConnection,
    district: DistrictBoundary,
    store_cfg: dict,
    metric_srid: int,
) -> None:
    get_office_points_map_controller(iface).refresh_from_db(
        conn, district, store_cfg, metric_srid
    )


def _office_point_features(result: TaskResult) -> List[TaskFeature]:
    features: List[TaskFeature] = []
    for group in result.groups:
        for subgroup in group.subgroups:
            if subgroup.name != OFFICE_DATA_SUBGROUP:
                continue
            for feat in subgroup.features:
                if feat.layer_key and feat.layer_key != OFFICE_DATA_LAYER_KEY:
                    continue
                if feat.task_geom and not feat.task_geom.isEmpty():
                    features.append(feat)
    return features


def _point_symbol() -> QgsMarkerSymbol:
    symbol = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": f"{POINT_FILL.red()},{POINT_FILL.green()},{POINT_FILL.blue()},220",
            "outline_color": POINT_STROKE.name(),
            "outline_width": "0.8",
            "size": "5",
        }
    )
    return symbol


def _build_points_layer(features: List[TaskFeature]) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", OFFICE_POINTS_LAYER_NAME, "memory")
    if not layer.isValid():
        return None

    layer.setRenderer(QgsSingleSymbolRenderer(_point_symbol()))
    provider = layer.dataProvider()
    fields = QgsFields()
    fields.append(qgs_field("task_key", QVariant.String))
    provider.addAttributes(fields)
    layer.updateFields()

    qgs_features: List[QgsFeature] = []
    for feat in features:
        geom = QgsGeometry(feat.task_geom)
        if not geom or geom.isEmpty():
            continue
        qgs_feat = QgsFeature(fields)
        qgs_feat.setGeometry(geom)
        qgs_feat.setAttribute("task_key", feat.task_key or "")
        qgs_features.append(qgs_feat)

    if not qgs_features:
        return None

    provider.addFeatures(qgs_features)
    layer.updateExtents()
    return layer


class OfficePointsMapController:
    """Memory-слой точек office_data; живёт в проекте QGIS между сессиями диалога."""

    def __init__(self, iface, district_name: str = ""):
        self._iface = iface
        self._district_name = district_name
        self._layer: Optional[QgsVectorLayer] = None
        self._point_count = 0

    @property
    def point_count(self) -> int:
        return self._point_count

    def _find_existing_layer(self) -> Optional[QgsVectorLayer]:
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(OFFICE_POINTS_GROUP_NAME)
        if group is None:
            return None
        for child in group.children():
            layer_id = child.layerId() if hasattr(child, "layerId") else None
            if not layer_id:
                continue
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer and layer.name() == OFFICE_POINTS_LAYER_NAME:
                return layer
        return None

    def _remove_layer(self) -> None:
        layer = self._layer or self._find_existing_layer()
        if layer is None:
            self._layer = None
            self._point_count = 0
            return
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(OFFICE_POINTS_GROUP_NAME)
        if group:
            node = group.findLayer(layer.id())
            if node:
                group.removeChildNode(node)
            if not group.children():
                root.removeChildNode(group)
        QgsProject.instance().removeMapLayer(layer.id())
        self._layer = None
        self._point_count = 0

    def _add_layer(self, layer: QgsVectorLayer) -> None:
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(OFFICE_POINTS_GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, OFFICE_POINTS_GROUP_NAME)
        QgsProject.instance().addMapLayer(layer, False)
        group.addLayer(layer)
        layer.setCustomProperty(OFFICE_POINTS_LAYER_PROP, True)

    def _show_features(self, features: List[TaskFeature], *, district_name: str = "") -> None:
        if district_name:
            self._district_name = district_name
        self._remove_layer()
        self._point_count = len(features)
        if not features:
            refresh_map_canvas(self._iface)
            return

        layer = _build_points_layer(features)
        if layer is None:
            self._point_count = 0
            refresh_map_canvas(self._iface)
            return

        self._layer = layer
        self._add_layer(layer)
        log_info(
            f"CRM «{OFFICE_DATA_SUBGROUP}»: на карте {self._point_count} точек"
        )
        refresh_map_canvas(self._iface)

    def refresh_from_db(
        self,
        conn: DatabaseConnection,
        district: DistrictBoundary,
        store_cfg: dict,
        metric_srid: int,
    ) -> None:
        features, errors = collect_office_data_tasks(
            conn, district, store_cfg, metric_srid
        )
        for err in errors:
            log_info(f"CRM точки камерального анализа: {err}")
        self._show_features(features, district_name=district.name)

    def refresh(self, result: TaskResult) -> None:
        self._show_features(
            _office_point_features(result),
            district_name=result.district_name or self._district_name,
        )

    def clear(self) -> None:
        self._remove_layer()
        refresh_map_canvas(self._iface)

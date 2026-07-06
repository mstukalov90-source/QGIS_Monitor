# -*- coding: utf-8 -*-
"""Создание memory-слоёв задач CRM в проекте QGIS."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QApplication, QProgressDialog

from .config import crm_task_store
from .crm_snapshot_loader import collect_snapshot_tasks
from .crm_task_store import (
    TASK_ID_COLUMNS,
    enrich_task_result_field_observed,
    ensure_crm_session_cache,
    filter_sent_tasks_from_result,
    layer_geometry_type,
    task_row_from_feature,
)
from .crm_tasks import TaskFeature, TaskResult, copy_task_result
from .crm_tasks_area import collect_tasks_area
from .crm_ui_constants import (
    SNAPSHOT_SOURCES,
    TASK_SOURCE_LABELS,
    TaskSource,
    area_status_from_source,
    format_field_observed,
)
from .db import DatabaseConnection
from .district_utils import DistrictBoundary, WGS84, transform_context
from .layer_utils import refresh_map_canvas
from .symbology import apply_symbology
from .qt_compat import qgs_field

TASK_LAYERS_GROUP_PREFIX = "Monitor CRM — задачи"
WGS84_CRS = QgsCoordinateReferenceSystem("EPSG:4326")

_GEOM_SUFFIX = {
    QgsWkbTypes.PointGeometry: "точки",
    QgsWkbTypes.LineGeometry: "линии",
    QgsWkbTypes.PolygonGeometry: "полигоны",
}

_LAYER_URI = {
    QgsWkbTypes.PointGeometry: "Point?crs=EPSG:4326",
    QgsWkbTypes.LineGeometry: "LineString?crs=EPSG:4326",
    QgsWkbTypes.PolygonGeometry: "Polygon?crs=EPSG:4326",
}

_SYMBOLOGY = {
    QgsWkbTypes.PointGeometry: {
        "geometry_type": "point",
        "symbology": {"color": "#cc0000", "size": 4, "marker_type": "circle"},
    },
    QgsWkbTypes.LineGeometry: {
        "geometry_type": "line",
        "symbology": {"color": "#cc0000", "width": 1.0},
    },
    QgsWkbTypes.PolygonGeometry: {
        "geometry_type": "polygon",
        "symbology": {
            "fill_color": "#cc0000",
            "outline_color": "#990000",
            "fill_opacity": 0.35,
            "outline_width": 0.8,
        },
    },
}


@dataclass
class TaskLayersBuildStats:
    layers_created: int = 0
    features_added: int = 0
    features_skipped: int = 0
    sources_processed: int = 0
    warnings: List[str] = field(default_factory=list)


def task_layers_group_name(district_name: str) -> str:
    return f"{TASK_LAYERS_GROUP_PREFIX} ({district_name})"


def collect_task_result_for_source(
    source: str,
    conn: DatabaseConnection,
    district: DistrictBoundary,
    config: Dict[str, Any],
    *,
    active_result: Optional[TaskResult] = None,
    store_cfg: Optional[Dict[str, Any]] = None,
) -> TaskResult:
    """Собрать TaskResult для одного источника задач."""
    store_cfg = store_cfg or crm_task_store(config)

    if source == "active":
        if active_result is None:
            raise ValueError("Для активных задач нужен снимок active_result")
        result = copy_task_result(active_result)
        if store_cfg:
            ensure_crm_session_cache(conn, store_cfg)
            filter_sent_tasks_from_result(result, conn, store_cfg)
        enrich_task_result_field_observed(result, conn, store_cfg)
        result.task_source = source
        return result

    if source in SNAPSHOT_SOURCES:
        result = collect_snapshot_tasks(conn, district, source, config)
        enrich_task_result_field_observed(result, conn, store_cfg)
        return result

    status = area_status_from_source(source)
    if not status:
        raise ValueError(f"Неизвестный источник: {source}")
    return collect_tasks_area(conn, district.name, status)


def _geometry_type(geom: QgsGeometry) -> Optional[int]:
    if not geom or geom.isEmpty():
        return None
    gtype = QgsWkbTypes.geometryType(geom.wkbType())
    if gtype == QgsWkbTypes.UnknownGeometry:
        return None
    return gtype


def _feature_geometry_wgs84(task_feat: TaskFeature) -> Optional[QgsGeometry]:
    if task_feat.area_geom and not task_feat.area_geom.isEmpty():
        geom = QgsGeometry(task_feat.area_geom)
        return geom

    layer = task_feat.layer
    if not layer or not layer.isValid() or task_feat.feature_id is None:
        return None

    feat = layer.getFeature(task_feat.feature_id)
    if not feat.isValid():
        return None

    geom = feat.geometry()
    if not geom or geom.isEmpty():
        return None

    out = QgsGeometry(geom)
    source_crs = layer.crs() if layer.crs().isValid() else WGS84
    if source_crs.isValid() and WGS84_CRS.isValid() and source_crs != WGS84_CRS:
        out.transform(
            QgsCoordinateTransform(source_crs, WGS84_CRS, transform_context())
        )
    return out if not out.isEmpty() else None


def _business_id(
    subgroup_name: str,
    attributes: Dict[str, Any],
    store_cfg: Dict[str, Any],
    layer: Any = None,
) -> str:
    row = task_row_from_feature(
        "",
        subgroup_name,
        attributes,
        store_cfg,
        geometry_type=layer_geometry_type(layer),
    )
    if row is None:
        return ""
    for col in TASK_ID_COLUMNS:
        value = row.get(col)
        if value:
            return str(value)
    return ""


def _build_fields() -> QgsFields:
    fields = QgsFields()
    for name, length in (
        ("task_key", 64),
        ("group", 80),
        ("subgroup", 80),
        ("field_observed", 16),
        ("business_id", 64),
        ("sent_at", 32),
    ):
        fld = qgs_field(name, QVariant.String)
        fld.setLength(length)
        fields.append(fld)
    return fields


def _make_feature(
    fields: QgsFields,
    geom: QgsGeometry,
    task_feat: TaskFeature,
    group_name: str,
    subgroup_name: str,
    store_cfg: Dict[str, Any],
) -> QgsFeature:
    attrs = task_feat.attributes or {}
    out = QgsFeature(fields)
    out.setGeometry(geom)
    out.setAttributes(
        [
            str(task_feat.task_key or attrs.get("key") or ""),
            group_name,
            subgroup_name,
            format_field_observed(attrs.get("field_observed")),
            _business_id(subgroup_name, attrs, store_cfg, task_feat.layer),
            str(task_feat.sent_at or attrs.get("_sent_at") or ""),
        ]
    )
    return out


def _layer_uri_for_features(
    gtype: int, qgs_features: List[QgsFeature]
) -> Optional[str]:
    if gtype == QgsWkbTypes.PointGeometry:
        return _LAYER_URI[gtype]
    if gtype == QgsWkbTypes.LineGeometry:
        return _LAYER_URI[gtype]
    if gtype == QgsWkbTypes.PolygonGeometry:
        if any(
            QgsWkbTypes.isMultiType(f.geometry().wkbType())
            for f in qgs_features
            if f.geometry() and not f.geometry().isEmpty()
        ):
            return "Multipolygon?crs=EPSG:4326"
        return _LAYER_URI[gtype]
    return None


def build_subgroup_layers(
    subgroup_name: str,
    group_name: str,
    features: List[TaskFeature],
    store_cfg: Dict[str, Any],
    stats: TaskLayersBuildStats,
) -> List[QgsVectorLayer]:
    """Memory-слои для одной подгруппы; при смешанной геометрии — до трёх слоёв."""
    by_type: Dict[int, List[QgsFeature]] = {}
    fields = _build_fields()

    for task_feat in features:
        geom = _feature_geometry_wgs84(task_feat)
        if geom is None:
            stats.features_skipped += 1
            continue
        gtype = _geometry_type(geom)
        if gtype is None:
            stats.features_skipped += 1
            continue
        qgs_feat = _make_feature(
            fields, geom, task_feat, group_name, subgroup_name, store_cfg
        )
        by_type.setdefault(gtype, []).append(qgs_feat)

    if not by_type:
        return []

    layers: List[QgsVectorLayer] = []
    multi_type = len(by_type) > 1

    for gtype, qgs_features in by_type.items():
        suffix = _GEOM_SUFFIX.get(gtype, "")
        if multi_type and suffix:
            layer_name = f"{subgroup_name} — {suffix}"
        else:
            layer_name = subgroup_name

        uri = _layer_uri_for_features(gtype, qgs_features)
        if not uri:
            stats.features_skipped += len(qgs_features)
            continue

        layer = QgsVectorLayer(uri, layer_name, "memory")
        if not layer.isValid():
            stats.features_skipped += len(qgs_features)
            stats.warnings.append(f"Не удалось создать слой «{layer_name}»")
            continue

        provider = layer.dataProvider()
        provider.addAttributes(fields.toList())
        layer.updateFields()
        provider.addFeatures(qgs_features)
        layer.updateExtents()

        symbology_def = _SYMBOLOGY.get(gtype)
        if symbology_def:
            apply_symbology(layer, symbology_def)

        layers.append(layer)
        stats.layers_created += 1
        stats.features_added += len(qgs_features)

    return layers


def _remove_task_layers_group(group_name: str) -> None:
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    node = root.findGroup(group_name)
    if not node:
        return
    layer_ids = [
        child.layer().id() for child in node.findLayers() if child.layer()
    ]
    root.removeChildNode(node)
    for lid in layer_ids:
        project.removeMapLayer(lid)


def _add_layers_to_group(
    parent_node,
    layers: List[QgsVectorLayer],
) -> None:
    project = QgsProject.instance()
    for layer in layers:
        project.addMapLayer(layer, False)
        parent_node.addLayer(layer)
        layer.setCustomProperty("monitor_crm_task_layers", True)


def create_task_layers_in_qgis(
    iface,
    conn: DatabaseConnection,
    district: DistrictBoundary,
    config: Dict[str, Any],
    allowed_sources: List[TaskSource],
    *,
    active_result: Optional[TaskResult] = None,
    parent=None,
) -> TaskLayersBuildStats:
    """Создать слои задач всех разрешённых типов для текущего района."""
    stats = TaskLayersBuildStats()
    store_cfg = crm_task_store(config)
    group_name = task_layers_group_name(district.name)

    _remove_task_layers_group(group_name)
    root = QgsProject.instance().layerTreeRoot()
    top_group = root.insertGroup(0, group_name)

    total = len(allowed_sources)
    progress = QProgressDialog(
        "Подготовка…",
        "Отмена",
        0,
        max(total, 1),
        parent,
    )
    progress.setWindowTitle("Monitor CRM")
    progress.setMinimumDuration(0)

    try:
        for step, source in enumerate(allowed_sources, start=1):
            progress.setValue(step - 1)
            source_label = TASK_SOURCE_LABELS.get(source, source)
            progress.setLabelText(
                f"Источник {step}/{total}: {source_label}…"
            )
            QApplication.processEvents()
            if progress.wasCanceled():
                break

            try:
                if source == "active" and active_result is None:
                    stats.warnings.append(
                        f"«{source_label}»: нет снимка активных задач"
                    )
                    continue

                result = collect_task_result_for_source(
                    source,
                    conn,
                    district,
                    config,
                    active_result=active_result,
                    store_cfg=store_cfg,
                )
            except Exception as exc:
                stats.warnings.append(f"«{source_label}»: {exc}")
                continue

            if result.total_count == 0:
                continue

            stats.sources_processed += 1
            source_group = top_group.addGroup(source_label)

            for group in result.groups:
                for subgroup in group.subgroups:
                    if not subgroup.features:
                        continue
                    layers = build_subgroup_layers(
                        subgroup.name,
                        group.name,
                        subgroup.features,
                        store_cfg,
                        stats,
                    )
                    if layers:
                        _add_layers_to_group(source_group, layers)

            if source_group.children():
                source_group.setExpanded(True)
            else:
                top_group.removeChildNode(source_group)

        progress.setValue(total)

        if not top_group.children():
            root.removeChildNode(top_group)
        else:
            top_group.setExpanded(True)

        refresh_map_canvas(iface)
    finally:
        progress.close()

    return stats

# -*- coding: utf-8 -*-
"""Общие утилиты фильтрации объектов по полигону района."""

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLayerTree,
    QgsLayerTreeGroup,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .log_util import log_warning

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def transform_context():
    return QgsProject.instance().transformContext()


def escape_expr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


@dataclass
class DistrictBoundary:
    name: str
    layer_crs: QgsCoordinateReferenceSystem
    geom_layer_crs: QgsGeometry
    geom_metric: QgsGeometry


def district_bbox_for_layer(
    district: DistrictBoundary, layer: QgsVectorLayer
) -> QgsRectangle:
    geom = QgsGeometry(district.geom_layer_crs)
    layer_crs = layer.crs() if layer.crs().isValid() else district.layer_crs
    if layer_crs != district.layer_crs:
        geom.transform(
            QgsCoordinateTransform(
                district.layer_crs, layer_crs, transform_context()
            )
        )
    return geom.boundingBox()


def load_district_boundary(
    layer: QgsVectorLayer,
    field: str,
    rayon_name: str,
    metric_crs: QgsCoordinateReferenceSystem,
) -> Optional[DistrictBoundary]:
    idx = layer.fields().indexOf(field)
    if idx < 0:
        return None

    escaped = escape_expr_value(rayon_name)
    request = QgsFeatureRequest()
    request.setFilterExpression(f'"{field}" = \'{escaped}\'')

    geoms: List[QgsGeometry] = []
    for feat in layer.getFeatures(request):
        geom = feat.geometry()
        if geom and not geom.isEmpty():
            geoms.append(QgsGeometry(geom))

    if not geoms:
        return None

    if len(geoms) == 1:
        union = geoms[0]
    else:
        union = QgsGeometry.unaryUnion(geoms)
    if not union or union.isEmpty():
        return None

    layer_crs = layer.crs() if layer.crs().isValid() else WGS84
    geom_metric = QgsGeometry(union)
    if layer_crs != metric_crs:
        geom_metric.transform(
            QgsCoordinateTransform(layer_crs, metric_crs, transform_context())
        )

    return DistrictBoundary(
        name=rayon_name,
        layer_crs=layer_crs,
        geom_layer_crs=union,
        geom_metric=geom_metric,
    )


def collect_vector_layers(node) -> List[QgsVectorLayer]:
    layers: List[QgsVectorLayer] = []
    if QgsLayerTree.isLayer(node):
        lyr = node.layer()
        if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
            layers.append(lyr)
    elif isinstance(node, QgsLayerTreeGroup):
        for child in node.children():
            layers.extend(collect_vector_layers(child))
    return layers


def find_layer_by_name(root, name: str) -> Optional[QgsVectorLayer]:
    for node in root.findLayers():
        lyr = node.layer()
        if isinstance(lyr, QgsVectorLayer) and lyr.name() == name:
            return lyr
    return None


def find_group_by_name(root, name: str) -> Optional[QgsLayerTreeGroup]:
    group = root.findGroup(name)
    if group:
        return group
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup):
            found = find_group_by_name(child, name)
            if found:
                return found
    return None


def resolve_layers(
    root,
    layer_names: List[str],
    group_names: List[str],
) -> Tuple[List[QgsVectorLayer], List[str]]:
    found: List[QgsVectorLayer] = []
    seen_ids: Set[str] = set()
    missing: List[str] = []

    for name in layer_names:
        lyr = find_layer_by_name(root, name)
        if lyr and lyr.id() not in seen_ids:
            found.append(lyr)
            seen_ids.add(lyr.id())
        elif lyr is None:
            missing.append(name)

    for name in group_names:
        group = find_group_by_name(root, name)
        if group is None:
            missing.append(name)
            continue
        group_layers = collect_vector_layers(group)
        if not group_layers:
            log_warning(f"Группа «{name}» не содержит векторных слоёв")
        for lyr in group_layers:
            if lyr.id() not in seen_ids:
                found.append(lyr)
                seen_ids.add(lyr.id())

    return found, missing


def _geometry_in_district(
    metric_geom: QgsGeometry, district: DistrictBoundary
) -> bool:
    geom_type = QgsWkbTypes.geometryType(metric_geom.wkbType())
    if geom_type == QgsWkbTypes.PointGeometry:
        return district.geom_metric.contains(metric_geom)
    return district.geom_metric.intersects(metric_geom)


def features_in_district(
    layer: QgsVectorLayer,
    district: DistrictBoundary,
    metric_crs: QgsCoordinateReferenceSystem,
) -> List[QgsFeature]:
    """Объекты слоя внутри/пересекающие полигон района."""
    if not layer or not layer.isValid():
        return []

    transform = QgsCoordinateTransform(
        layer.crs(), metric_crs, transform_context()
    )
    request = QgsFeatureRequest()
    request.setFilterRect(district_bbox_for_layer(district, layer))

    matched: List[QgsFeature] = []
    for feat in layer.getFeatures(request):
        geom = feat.geometry()
        if not geom or geom.isEmpty():
            continue
        try:
            metric_geom = QgsGeometry(geom)
            metric_geom.transform(transform)
        except Exception:
            continue
        if metric_geom.isEmpty():
            continue
        if not _geometry_in_district(metric_geom, district):
            continue
        matched.append(feat)

    return matched

# -*- coding: utf-8 -*-
"""Пространственный фильтр активных задач по полигону площадного заказа."""

from typing import List, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsWkbTypes,
)

from .crm_tasks import TaskFeature, TaskGroup, TaskResult, TaskSubgroup, copy_task_result

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _transform_context():
    return QgsProject.instance().transformContext()


def feature_geometry_wgs84(task_feat: TaskFeature) -> Optional[QgsGeometry]:
    if task_feat.area_geom and not task_feat.area_geom.isEmpty():
        return QgsGeometry(task_feat.area_geom)

    if task_feat.task_geom and not task_feat.task_geom.isEmpty():
        return QgsGeometry(task_feat.task_geom)

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
    if source_crs.isValid() and WGS84.isValid() and source_crs != WGS84:
        out.transform(
            QgsCoordinateTransform(source_crs, WGS84, _transform_context())
        )
    return out if not out.isEmpty() else None


def _representative_point(geom: QgsGeometry) -> Optional[QgsGeometry]:
    if not geom or geom.isEmpty():
        return None
    gtype = QgsWkbTypes.geometryType(geom.wkbType())
    if gtype == QgsWkbTypes.PointGeometry:
        return geom
    try:
        centroid = geom.centroid()
        return centroid if centroid and not centroid.isEmpty() else None
    except Exception:
        return None


def geometry_inside_area(
    feature_geom: Optional[QgsGeometry],
    area_geom: QgsGeometry,
) -> bool:
    if not feature_geom or feature_geom.isEmpty() or not area_geom or area_geom.isEmpty():
        return False

    feat = QgsGeometry(feature_geom)
    area = QgsGeometry(area_geom)

    if feat.isMultipart() or QgsWkbTypes.geometryType(feat.wkbType()) != QgsWkbTypes.PointGeometry:
        point = _representative_point(feat)
        if point is None:
            return False
        feat = point

    try:
        return area.contains(feat) or feat.within(area)
    except Exception:
        return False


def count_task_result_features(result: Optional[TaskResult]) -> int:
    if not result:
        return 0
    return result.total_count


def filter_task_result_by_area(
    result: TaskResult,
    area_feature: TaskFeature,
) -> TaskResult:
    area_geom = feature_geometry_wgs84(area_feature)
    if not area_geom or area_geom.isEmpty():
        filtered = copy_task_result(result)
        filtered.groups = []
        return filtered

    groups: List[TaskGroup] = []
    for group in result.groups:
        subgroups: List[TaskSubgroup] = []
        for subgroup in group.subgroups:
            kept: List[TaskFeature] = []
            for feat in subgroup.features:
                feat_geom = feature_geometry_wgs84(feat)
                if geometry_inside_area(feat_geom, area_geom):
                    kept.append(feat)
            subgroups.append(
                TaskSubgroup(
                    name=subgroup.name,
                    features=kept,
                    date_field=subgroup.date_field,
                )
            )
        groups.append(TaskGroup(name=group.name, subgroups=subgroups))

    filtered = copy_task_result(result)
    filtered.groups = groups
    return filtered


def empty_task_result_shell(result: TaskResult) -> TaskResult:
    shell = copy_task_result(result)
    shell.groups = []
    return shell

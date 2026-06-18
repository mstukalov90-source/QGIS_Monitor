# -*- coding: utf-8 -*-
"""Экспорт объектов задач CRM в DXF (МСК-77)."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .config import crm_tasks
from .crm_tasks import TaskFeature, TaskResult
from .district_utils import WGS84, transform_context
from .log_util import log_info, log_warning


DEFAULT_BUFFER_METERS = 3

MSK77_PROJ4 = (
    "+proj=tmerc +lat_0=55.66666666667 +lon_0=37.5 +k=1 +x_0=0 +y_0=0 "
    "+ellps=bessel +towgs84=458.475,0.244,603.087,-3.98169,-0.43293,"
    "4.43381,1.713 +units=m +no_defs"
)

MSK77_WKT = (
    'PROJCS["MSK_77",GEOGCS["unknown",DATUM["Unknown based on Bessel 1841 '
    'ellipsoid",SPHEROID["Bessel 1841",6377397.155,299.1528128],'
    "TOWGS84[458.475,0.244,603.087,-3.98169,-0.43293,4.43381,1.713]],"
    'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",'
    '0.0174532925199433,AUTHORITY["EPSG","9122"]]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",55.66666666667],PARAMETER["central_meridian",37.5],'
    'PARAMETER["scale_factor",1],PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
    'AXIS["Easting",EAST],AXIS["Northing",NORTH]]'
)


@dataclass
class ExportStats:
    exported: int = 0
    skipped_empty: int = 0
    skipped_invalid: int = 0
    layers_written: int = 0
    errors: List[str] = field(default_factory=list)


def _export_settings(config: Dict[str, Any]) -> tuple:
    cfg = crm_tasks(config)
    buffer_m = float(cfg.get("export_buffer_meters", DEFAULT_BUFFER_METERS))
    proj4 = str(cfg.get("export_crs_proj4", MSK77_PROJ4)).strip() or MSK77_PROJ4
    metric_crs = QgsCoordinateReferenceSystem(
        cfg.get("metric_crs", "EPSG:32637")
    )
    return buffer_m, proj4, metric_crs


def msk77_crs(proj4: str = MSK77_PROJ4) -> QgsCoordinateReferenceSystem:
    crs = QgsCoordinateReferenceSystem()
    if crs.createFromProj(proj4):
        return crs
    crs = QgsCoordinateReferenceSystem()
    crs.createFromUserInput(MSK77_WKT)
    return crs


def geometry_for_export(
    geom: QgsGeometry,
    source_crs: QgsCoordinateReferenceSystem,
    metric_crs: QgsCoordinateReferenceSystem,
    dest_crs: QgsCoordinateReferenceSystem,
    buffer_m: float = DEFAULT_BUFFER_METERS,
) -> Optional[QgsGeometry]:
    if not geom or geom.isEmpty():
        return None

    if not source_crs.isValid():
        source_crs = WGS84

    geom_metric = QgsGeometry(geom)
    if source_crs != metric_crs:
        geom_metric.transform(
            QgsCoordinateTransform(source_crs, metric_crs, transform_context())
        )
    if geom_metric.isEmpty():
        return None

    gtype = QgsWkbTypes.geometryType(geom_metric.wkbType())
    if gtype in (QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry):
        geom_metric = geom_metric.buffer(buffer_m, 8)
    elif gtype != QgsWkbTypes.PolygonGeometry:
        return None

    if not geom_metric or geom_metric.isEmpty():
        return None

    if metric_crs != dest_crs:
        geom_metric.transform(
            QgsCoordinateTransform(metric_crs, dest_crs, transform_context())
        )
    if geom_metric.isEmpty():
        return None
    return geom_metric


def _build_subgroup_layer(
    subgroup_name: str,
    features: List[TaskFeature],
    dest_crs: QgsCoordinateReferenceSystem,
    metric_crs: QgsCoordinateReferenceSystem,
    buffer_m: float,
    stats: ExportStats,
) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer("Polygon", subgroup_name, "memory")
    if not layer.isValid():
        stats.errors.append(f"Не удалось создать слой «{subgroup_name}»")
        return None

    layer.setCrs(dest_crs)
    provider = layer.dataProvider()

    export_features: List[QgsFeature] = []
    for task_feat in features:
        qgs_layer = task_feat.layer
        if not qgs_layer or not qgs_layer.isValid():
            stats.skipped_invalid += 1
            continue

        feat = qgs_layer.getFeature(task_feat.feature_id)
        if not feat.isValid():
            stats.skipped_invalid += 1
            continue

        geom = feat.geometry()
        if not geom or geom.isEmpty():
            stats.skipped_empty += 1
            continue

        source_crs = qgs_layer.crs() if qgs_layer.crs().isValid() else WGS84
        export_geom = geometry_for_export(
            geom,
            source_crs,
            metric_crs,
            dest_crs,
            buffer_m=buffer_m,
        )
        if export_geom is None or export_geom.isEmpty():
            stats.skipped_invalid += 1
            continue

        out_feat = QgsFeature()
        out_feat.setGeometry(export_geom)
        export_features.append(out_feat)
        stats.exported += 1

    if not export_features:
        return None

    provider.addFeatures(export_features)
    layer.updateExtents()
    return layer


def _vector_writer_no_error():
    if hasattr(QgsVectorFileWriter, "NoError"):
        return QgsVectorFileWriter.NoError
    writer_error = getattr(QgsVectorFileWriter, "WriterError", None)
    if writer_error is not None and hasattr(writer_error, "NoError"):
        return writer_error.NoError
    return 0


def _write_layer_to_file(
    layer: QgsVectorLayer,
    path: str,
    transform_context,
    options: QgsVectorFileWriter.SaveVectorOptions,
) -> tuple:
    """Совместимость QGIS 3.x: writeAsVectorFormatV3 → 2 или 4 значения."""
    write_result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer,
        path,
        transform_context,
        options,
    )
    if isinstance(write_result, tuple):
        error = write_result[0]
        error_message = write_result[1] or ""
        return error, error_message
    return write_result, ""


def export_tasks_to_dxf(
    path: str,
    task_result: TaskResult,
    config: Dict[str, Any],
) -> ExportStats:
    """Экспортировать объекты TaskResult в многослойный DXF."""
    stats = ExportStats()
    if task_result.total_count == 0:
        stats.errors.append("Нет объектов для экспорта")
        return stats

    buffer_m, proj4, metric_crs = _export_settings(config)
    dest_crs = msk77_crs(proj4)
    if not dest_crs.isValid():
        stats.errors.append("Не удалось инициализировать СК МСК-77")
        return stats
    if not metric_crs.isValid():
        stats.errors.append("Некорректная metric_crs в конфигурации")
        return stats

    export_layers: List[QgsVectorLayer] = []

    for group in task_result.groups:
        for subgroup in group.subgroups:
            if not subgroup.features:
                continue
            mem_layer = _build_subgroup_layer(
                subgroup.name,
                subgroup.features,
                dest_crs,
                metric_crs,
                buffer_m,
                stats,
            )
            if mem_layer is not None:
                export_layers.append(mem_layer)

    if not export_layers:
        stats.errors.append("Нет геометрий для записи в DXF")
        return stats

    transform_context_obj = QgsProject.instance().transformContext()
    for index, mem_layer in enumerate(export_layers):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "DXF"
        options.fileEncoding = "UTF-8"
        if hasattr(options, "skipAttributeCreation"):
            options.skipAttributeCreation = True
        if index == 0:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile
            )
        else:
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrAppendLayer
            )

        result, error_message = _write_layer_to_file(
            mem_layer,
            path,
            transform_context_obj,
            options,
        )
        if result != _vector_writer_no_error():
            stats.errors.append(
                f"Ошибка записи слоя «{mem_layer.name()}»: {error_message}"
            )
            log_warning(
                f"DXF export layer «{mem_layer.name()}»: {error_message}"
            )
            return stats
        stats.layers_written += 1

    log_info(
        f"DXF export: {stats.exported} объектов, "
        f"{stats.layers_written} слоёв → {path}"
    )
    return stats

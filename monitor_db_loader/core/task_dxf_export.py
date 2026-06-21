# -*- coding: utf-8 -*-
"""Экспорт объектов задач CRM в DXF / SHP (МСК-77)."""

import csv
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

from .config import crm_task_store, crm_tasks
from .crm_tasks import TaskResult
from .district_utils import WGS84, transform_context
from .log_util import log_info, log_warning
from .qt_compat import qgs_field

DEFAULT_BUFFER_METERS = 3
EXPORT_LAYER_NAME = "tasks"

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
class ExportItem:
    geometry: QgsGeometry
    group: str
    subgroup: str
    task_column: str
    id_value: str


@dataclass
class ExportStats:
    exported: int = 0
    skipped_empty: int = 0
    skipped_invalid: int = 0
    layers_written: int = 0
    csv_path: str = ""
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


def _task_id_info(
    subgroup_name: str,
    attributes: Dict[str, Any],
    store_cfg: Dict[str, Any],
) -> Tuple[str, str]:
    mapping = store_cfg.get("subgroups", {}).get(subgroup_name, {})
    task_column = str(mapping.get("task_column", "") or "")
    source_field = mapping.get("source_field")
    if not source_field:
        return task_column, ""
    value = attributes.get(source_field)
    if value is None:
        return task_column, ""
    return task_column, str(value).strip()


def _ids_csv_path(export_path: str) -> str:
    base, _ext = os.path.splitext(export_path)
    return f"{base}_ids.csv"


def _shp_fields() -> QgsFields:
    fields = QgsFields()
    for name, length in (
        ("group", 80),
        ("subgroup", 80),
        ("task_col", 32),
        ("id", 64),
    ):
        fld = qgs_field(name, QVariant.String)
        fld.setLength(length)
        fields.append(fld)
    return fields


def _collect_export_items(
    task_result: TaskResult,
    config: Dict[str, Any],
    stats: ExportStats,
) -> List[ExportItem]:
    buffer_m, proj4, metric_crs = _export_settings(config)
    dest_crs = msk77_crs(proj4)
    if not dest_crs.isValid():
        stats.errors.append("Не удалось инициализировать СК МСК-77")
        return []
    if not metric_crs.isValid():
        stats.errors.append("Некорректная metric_crs в конфигурации")
        return []

    store_cfg = crm_task_store(config)
    items: List[ExportItem] = []

    for group in task_result.groups:
        for subgroup in group.subgroups:
            for task_feat in subgroup.features:
                task_column, id_value = _task_id_info(
                    subgroup.name,
                    task_feat.attributes,
                    store_cfg,
                )
                if not id_value:
                    stats.skipped_invalid += 1
                    continue

                qgs_layer = task_feat.layer
                if (
                    not qgs_layer
                    or not qgs_layer.isValid()
                    or task_feat.feature_id is None
                ):
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

                source_crs = (
                    qgs_layer.crs() if qgs_layer.crs().isValid() else WGS84
                )
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

                items.append(
                    ExportItem(
                        geometry=export_geom,
                        group=group.name,
                        subgroup=subgroup.name,
                        task_column=task_column,
                        id_value=id_value,
                    )
                )
                stats.exported += 1

    return items


def _build_memory_layer(
    items: List[ExportItem],
    dest_crs: QgsCoordinateReferenceSystem,
    *,
    with_attributes: bool,
) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer("Polygon", EXPORT_LAYER_NAME, "memory")
    if not layer.isValid():
        return None

    layer.setCrs(dest_crs)
    provider = layer.dataProvider()
    fields = _shp_fields() if with_attributes else QgsFields()
    if with_attributes:
        provider.addAttributes(fields.toList())
        layer.updateFields()

    export_features: List[QgsFeature] = []
    for item in items:
        out_feat = QgsFeature(fields)
        out_feat.setGeometry(item.geometry)
        if with_attributes:
            out_feat.setAttributes(
                [item.group, item.subgroup, item.task_column, item.id_value]
            )
        export_features.append(out_feat)

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


def _action_overwrite_file():
    if hasattr(QgsVectorFileWriter, "CreateOrOverwriteFile"):
        return QgsVectorFileWriter.CreateOrOverwriteFile
    action_enum = getattr(QgsVectorFileWriter, "ActionOnExistingFile", None)
    if action_enum is not None and hasattr(action_enum, "CreateOrOverwriteFile"):
        return action_enum.CreateOrOverwriteFile
    return 0


def _write_layer_to_file(
    layer: QgsVectorLayer,
    path: str,
    transform_context,
    options: QgsVectorFileWriter.SaveVectorOptions,
) -> tuple:
    write_result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer,
        path,
        transform_context,
        options,
    )
    if isinstance(write_result, tuple):
        return write_result[0], write_result[1] or ""
    return write_result, ""


def _write_ids_csv(path: str, items: List[ExportItem]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["group", "subgroup", "task_column", "id"])
        for item in items:
            writer.writerow(
                [item.group, item.subgroup, item.task_column, item.id_value]
            )


def _export_prepared(
    path: str,
    task_result: TaskResult,
    config: Dict[str, Any],
    *,
    driver_name: str,
    with_attributes: bool,
    write_csv: bool,
    empty_error: str,
) -> ExportStats:
    stats = ExportStats()
    if task_result.total_count == 0:
        stats.errors.append("Нет объектов для экспорта")
        return stats

    items = _collect_export_items(task_result, config, stats)
    if stats.errors:
        return stats
    if not items:
        stats.errors.append(empty_error)
        return stats

    _, proj4, _ = _export_settings(config)
    dest_crs = msk77_crs(proj4)
    layer = _build_memory_layer(items, dest_crs, with_attributes=with_attributes)
    if layer is None:
        stats.errors.append("Не удалось создать слой для экспорта")
        return stats

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = driver_name
    options.fileEncoding = "UTF-8"
    options.actionOnExistingFile = _action_overwrite_file()
    if driver_name == "DXF" and hasattr(options, "skipAttributeCreation"):
        options.skipAttributeCreation = True

    result, error_message = _write_layer_to_file(
        layer,
        path,
        QgsProject.instance().transformContext(),
        options,
    )
    if result != _vector_writer_no_error():
        stats.errors.append(f"Ошибка записи: {error_message}")
        log_warning(f"Task export ({driver_name}): {error_message}")
        return stats

    stats.layers_written = 1
    if write_csv:
        stats.csv_path = _ids_csv_path(path)
        try:
            _write_ids_csv(stats.csv_path, items)
        except OSError as exc:
            stats.errors.append(f"Файл записан, но CSV не создан: {exc}")
            return stats

    return stats


def export_tasks_to_dxf(
    path: str,
    task_result: TaskResult,
    config: Dict[str, Any],
) -> ExportStats:
    """Экспорт геометрий в DXF и ID задач в CSV."""
    stats = _export_prepared(
        path,
        task_result,
        config,
        driver_name="DXF",
        with_attributes=False,
        write_csv=True,
        empty_error="Нет геометрий для записи в DXF",
    )
    if not stats.errors:
        log_info(
            f"DXF export: {stats.exported} объектов → {path}, "
            f"ID → {stats.csv_path}"
        )
    return stats


def export_tasks_to_shp(
    path: str,
    task_result: TaskResult,
    config: Dict[str, Any],
) -> ExportStats:
    """Экспорт геометрий и ID задач в Shapefile (МСК-77)."""
    stats = _export_prepared(
        path,
        task_result,
        config,
        driver_name="ESRI Shapefile",
        with_attributes=True,
        write_csv=False,
        empty_error="Нет геометрий для записи в SHP",
    )
    if not stats.errors:
        log_info(f"SHP export: {stats.exported} объектов → {path}")
    return stats

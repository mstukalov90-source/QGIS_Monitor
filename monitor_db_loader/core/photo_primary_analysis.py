# -*- coding: utf-8 -*-
"""Первичный пространственный анализ фото — классификация точек по близости."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLayerTree,
    QgsLayerTreeGroup,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QApplication, QMessageBox, QProgressDialog

from .config import photo_primary_analysis
from .layer_utils import refresh_map_canvas
from .log_util import log_info, log_warning
from .symbology import apply_symbology


@dataclass
class SourcePoint:
    layer: QgsVectorLayer
    feature: QgsFeature
    metric_geom: QgsGeometry
    key: Tuple[str, int]


@dataclass
class AnalysisResult:
    green: int = 0
    yellow: int = 0
    red: int = 0
    total: int = 0
    errors: List[str] = field(default_factory=list)


class ReferenceIndex:
    """Пространственный индекс опорных объектов в метрической СК."""

    def __init__(self, metric_crs: QgsCoordinateReferenceSystem):
        self._geometries: Dict[int, QgsGeometry] = {}
        self._index = QgsSpatialIndex()
        self._metric_crs = metric_crs
        self._next_id = 0

    def add_layer(self, layer: QgsVectorLayer) -> int:
        if not layer or not layer.isValid():
            return 0
        project = QgsProject.instance()
        transform = QgsCoordinateTransform(
            layer.crs(), self._metric_crs, project
        )
        count = 0
        for feat in layer.getFeatures():
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
            idx = self._next_id
            self._next_id += 1
            self._geometries[idx] = metric_geom
            index_feat = QgsFeature()
            index_feat.setId(idx)
            index_feat.setGeometry(metric_geom)
            self._index.addFeature(index_feat)
            count += 1
        return count

    def within(self, point: QgsGeometry, radius_m: float) -> bool:
        if not self._geometries:
            return False
        search_rect = point.buffer(radius_m, 8).boundingBox()
        for idx in self._index.intersects(search_rect):
            ref_geom = self._geometries.get(idx)
            if ref_geom and ref_geom.distance(point) <= radius_m:
                return True
        return False

    @property
    def count(self) -> int:
        return len(self._geometries)


def _collect_vector_layers(node) -> List[QgsVectorLayer]:
    layers: List[QgsVectorLayer] = []
    if QgsLayerTree.isLayer(node):
        lyr = node.layer()
        if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
            layers.append(lyr)
    elif isinstance(node, QgsLayerTreeGroup):
        for child in node.children():
            layers.extend(_collect_vector_layers(child))
    return layers


def _find_layer_by_name(root, name: str) -> Optional[QgsVectorLayer]:
    for node in root.findLayers():
        lyr = node.layer()
        if isinstance(lyr, QgsVectorLayer) and lyr.name() == name:
            return lyr
    return None


def _find_group_by_name(root, name: str) -> Optional[QgsLayerTreeGroup]:
    group = root.findGroup(name)
    if group:
        return group
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup):
            found = _find_group_by_name(child, name)
            if found:
                return found
    return None


def _resolve_layers(
    root,
    layer_names: List[str],
    group_names: List[str],
) -> Tuple[List[QgsVectorLayer], List[str]]:
    found: List[QgsVectorLayer] = []
    seen_ids: Set[str] = set()
    missing: List[str] = []

    for name in layer_names:
        lyr = _find_layer_by_name(root, name)
        if lyr and lyr.id() not in seen_ids:
            found.append(lyr)
            seen_ids.add(lyr.id())
        elif lyr is None:
            missing.append(name)

    for name in group_names:
        group = _find_group_by_name(root, name)
        if group is None:
            missing.append(name)
            continue
        group_layers = _collect_vector_layers(group)
        if not group_layers:
            log_warning(f"Группа «{name}» не содержит векторных слоёв")
        for lyr in group_layers:
            if lyr.id() not in seen_ids:
                found.append(lyr)
                seen_ids.add(lyr.id())

    return found, missing


def _collect_source_points(
    layers: List[QgsVectorLayer],
    metric_crs: QgsCoordinateReferenceSystem,
) -> List[SourcePoint]:
    project = QgsProject.instance()
    points: List[SourcePoint] = []

    for layer in layers:
        transform = QgsCoordinateTransform(layer.crs(), metric_crs, project)
        layer_count = 0
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if not geom or geom.isEmpty():
                continue
            if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PointGeometry:
                log_warning(
                    f"Пропуск не-точки в «{layer.name()}» (fid={feat.id()})"
                )
                continue
            try:
                metric_geom = QgsGeometry(geom)
                metric_geom.transform(transform)
            except Exception as exc:
                log_warning(
                    f"Ошибка трансформации fid={feat.id()} в «{layer.name()}»: {exc}"
                )
                continue
            if metric_geom.isEmpty():
                continue
            points.append(
                SourcePoint(
                    layer=layer,
                    feature=feat,
                    metric_geom=metric_geom,
                    key=(layer.id(), feat.id()),
                )
            )
            layer_count += 1
        log_info(f"  исходный слой «{layer.name()}»: {layer_count} точек")
        if layer_count == 0:
            log_warning(f"Исходный слой «{layer.name()}» пуст или без точек")

    return points


def _union_fields(sources: List[SourcePoint]) -> QgsFields:
    fields = QgsFields()
    fields.append(QgsField("source_layer", QVariant.String))
    fields.append(QgsField("source_fid", QVariant.Int))
    fields.append(QgsField("category", QVariant.String))
    seen: Set[str] = {"source_layer", "source_fid", "category"}
    for sp in sources:
        for f in sp.feature.fields():
            if f.name() not in seen:
                fields.append(f)
                seen.add(f.name())
    return fields


def _create_result_layer(
    name: str,
    color: str,
    fields: QgsFields,
    entries: List[Tuple[SourcePoint, str]],
) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")
    if not layer.isValid():
        return None

    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()

    features: List[QgsFeature] = []
    for sp, category in entries:
        feat = QgsFeature(fields)
        feat.setGeometry(sp.feature.geometry())
        attrs = {f.name(): sp.feature[f.name()] for f in sp.feature.fields()}
        attrs["source_layer"] = sp.layer.name()
        attrs["source_fid"] = sp.feature.id()
        attrs["category"] = category
        for i, fld in enumerate(fields):
            if fld.name() in attrs:
                feat.setAttribute(i, attrs[fld.name()])
        features.append(feat)

    provider.addFeatures(features)
    layer.updateExtents()

    apply_symbology(
        layer,
        {
            "geometry_type": "point",
            "symbology": {"color": color, "size": 5, "marker_type": "circle"},
        },
    )
    return layer


def _remove_result_group(group_name: str) -> None:
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


def run_primary_analysis(config: Dict[str, Any], iface, parent=None) -> AnalysisResult:
    """Запуск первичного анализа фото. Возвращает статистику."""
    cfg = photo_primary_analysis(config)
    if not cfg:
        QMessageBox.critical(
            parent or iface.mainWindow(),
            "Monitor DB Loader",
            "Секция photo_primary_analysis отсутствует в конфигурации.",
        )
        return AnalysisResult(errors=["Конфигурация не найдена"])

    result_group = cfg.get("result_group", "Первичный анализ фото")
    metric_crs = QgsCoordinateReferenceSystem(cfg.get("metric_crs", "EPSG:32637"))
    radius_green = float(cfg.get("radius_green_m", 100))
    radius_yellow = float(cfg.get("radius_yellow_m", 250))

    progress = QProgressDialog(
        "Первичный анализ фото…",
        "Отмена",
        0,
        5,
        parent or iface.mainWindow(),
    )
    progress.setWindowTitle("Monitor DB Loader")
    progress.setMinimumDuration(0)
    progress.setValue(0)

    root = QgsProject.instance().layerTreeRoot()

    progress.setLabelText("Поиск слоёв…")
    QApplication.processEvents()
    if progress.wasCanceled():
        return AnalysisResult()

    source_layers, missing_src = _resolve_layers(
        root, cfg.get("source_layers", []), []
    )
    group_a_cfg = cfg.get("group_a", {})
    ref_a_layers, missing_a = _resolve_layers(
        root,
        group_a_cfg.get("layers", []),
        group_a_cfg.get("groups", []),
    )
    group_b_extra = cfg.get("group_b_extra", {})
    ref_b_extra, missing_b = _resolve_layers(
        root,
        group_b_extra.get("layers", []),
        group_b_extra.get("groups", []),
    )

    missing = missing_src + missing_a + missing_b
    if missing:
        msg = "Не найдены слои или группы:\n" + "\n".join(f"• {m}" for m in missing)
        log_warning(msg.replace("\n", "; "))
        QMessageBox.warning(
            parent or iface.mainWindow(),
            "Monitor DB Loader — первичный анализ",
            msg + "\n\nСначала загрузите слои Monitor DB.",
        )
        progress.close()
        return AnalysisResult(errors=missing)

    if not source_layers:
        QMessageBox.warning(
            parent or iface.mainWindow(),
            "Monitor DB Loader — первичный анализ",
            "Исходные слои не найдены в проекте.",
        )
        progress.close()
        return AnalysisResult(errors=["Нет исходных слоёв"])

    progress.setValue(1)
    progress.setLabelText("Сбор исходных точек…")
    QApplication.processEvents()
    if progress.wasCanceled():
        return AnalysisResult()

    log_info("Первичный анализ фото: сбор исходных точек…")
    source_points = _collect_source_points(source_layers, metric_crs)
    if not source_points:
        QMessageBox.information(
            parent or iface.mainWindow(),
            "Monitor DB Loader — первичный анализ",
            "В исходных слоях нет точек для анализа.",
        )
        progress.close()
        return AnalysisResult()

    progress.setValue(2)
    progress.setLabelText("Построение индекса Группы А…")
    QApplication.processEvents()
    if progress.wasCanceled():
        return AnalysisResult()

    index_a = ReferenceIndex(metric_crs)
    for lyr in ref_a_layers:
        cnt = index_a.add_layer(lyr)
        log_info(f"  Группа А «{lyr.name()}»: {cnt} объектов")
    log_info(f"Индекс Группы А: {index_a.count} объектов")

    progress.setValue(3)
    progress.setLabelText("Классификация (зелёная / жёлтая)…")
    QApplication.processEvents()
    if progress.wasCanceled():
        return AnalysisResult()

    seen_b: Set[str] = set()
    ref_b_layers: List[QgsVectorLayer] = []
    for lyr in ref_a_layers + ref_b_extra:
        if lyr.id() not in seen_b:
            ref_b_layers.append(lyr)
            seen_b.add(lyr.id())

    index_b = ReferenceIndex(metric_crs)
    for lyr in ref_b_layers:
        cnt = index_b.add_layer(lyr)
        log_info(f"  Группа Б «{lyr.name()}»: {cnt} объектов")
    log_info(f"Индекс Группы Б: {index_b.count} объектов")

    green: List[SourcePoint] = []
    yellow: List[SourcePoint] = []
    red: List[SourcePoint] = []

    for sp in source_points:
        if index_a.within(sp.metric_geom, radius_green):
            green.append(sp)
        elif index_b.within(sp.metric_geom, radius_yellow):
            yellow.append(sp)
        else:
            red.append(sp)

    log_info(
        f"Классификация: зелёная={len(green)}, "
        f"жёлтая={len(yellow)}, красная={len(red)}"
    )

    progress.setValue(4)
    progress.setLabelText("Формирование слоёв…")
    QApplication.processEvents()
    if progress.wasCanceled():
        return AnalysisResult()

    _remove_result_group(result_group)
    group_node = root.addGroup(result_group)

    fields = _union_fields(source_points)
    results_cfg = cfg.get("results", [])
    category_map = {
        "green": green,
        "yellow": yellow,
        "red": red,
    }

    for res_def in results_cfg:
        cat = res_def.get("category", "")
        entries = [(sp, cat) for sp in category_map.get(cat, [])]
        res_layer = _create_result_layer(
            res_def.get("name", cat),
            res_def.get("color", "#000000"),
            fields,
            entries,
        )
        if res_layer:
            QgsProject.instance().addMapLayer(res_layer, False)
            group_node.addLayer(res_layer)
            log_info(f"  → «{res_layer.name()}»: {len(entries)} точек")

    progress.setValue(5)
    progress.close()

    refresh_map_canvas(iface)

    analysis = AnalysisResult(
        green=len(green),
        yellow=len(yellow),
        red=len(red),
        total=len(source_points),
    )

    QMessageBox.information(
        parent or iface.mainWindow(),
        "Monitor DB Loader — первичный анализ",
        f"Анализ завершён ({analysis.total} точек):\n"
        f"• Зелёная таблица: {analysis.green}\n"
        f"• Жёлтая таблица: {analysis.yellow}\n"
        f"• Красная таблица: {analysis.red}",
    )
    log_info(
        f"Первичный анализ завершён: {analysis.green}/{analysis.yellow}/"
        f"{analysis.red} из {analysis.total}"
    )
    return analysis

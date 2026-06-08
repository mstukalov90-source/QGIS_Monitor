# -*- coding: utf-8 -*-
"""Первичный пространственный анализ фото — классификация точек по близости."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
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
from .qt_compat import qgs_field
from .symbology import apply_symbology

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _transform_context():
    return QgsProject.instance().transformContext()


def _to_wgs84(geom: QgsGeometry, crs: QgsCoordinateReferenceSystem) -> QgsGeometry:
    """Перепроецировать геометрию в EPSG:4326 для отображения."""
    result = QgsGeometry(geom)
    if not crs.isValid() or crs == WGS84:
        return result
    result.transform(
        QgsCoordinateTransform(crs, WGS84, _transform_context())
    )
    return result


@dataclass
class SourcePoint:
    layer: QgsVectorLayer
    feature: QgsFeature
    metric_geom: QgsGeometry
    key: Tuple[str, int]


@dataclass
class ReferenceMatch:
    layer_id: str
    layer_name: str
    feature_id: int
    distance_m: float
    metric_geom: QgsGeometry
    source_geom: QgsGeometry


@dataclass
class ClassifiedPoint:
    source: SourcePoint
    category: str
    matches: List[ReferenceMatch] = field(default_factory=list)


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
        self._records: Dict[int, ReferenceMatch] = {}
        self._index = QgsSpatialIndex()
        self._metric_crs = metric_crs
        self._next_id = 0

    def add_layer(self, layer: QgsVectorLayer) -> int:
        if not layer or not layer.isValid():
            return 0
        project = QgsProject.instance()
        transform = QgsCoordinateTransform(
            layer.crs(), self._metric_crs, _transform_context()
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
            record = ReferenceMatch(
                layer_id=layer.id(),
                layer_name=layer.name(),
                feature_id=feat.id(),
                distance_m=0.0,
                metric_geom=metric_geom,
                source_geom=QgsGeometry(geom),
            )
            self._records[idx] = record
            index_feat = QgsFeature()
            index_feat.setId(idx)
            index_feat.setGeometry(metric_geom)
            self._index.addFeature(index_feat)
            count += 1
        return count

    def find_matches(self, point: QgsGeometry, radius_m: float) -> List[ReferenceMatch]:
        if not self._records:
            return []
        matches: List[ReferenceMatch] = []
        search_rect = point.buffer(radius_m, 8).boundingBox()
        for idx in self._index.intersects(search_rect):
            record = self._records.get(idx)
            if not record:
                continue
            dist = record.metric_geom.distance(point)
            if dist <= radius_m:
                matches.append(
                    ReferenceMatch(
                        layer_id=record.layer_id,
                        layer_name=record.layer_name,
                        feature_id=record.feature_id,
                        distance_m=dist,
                        metric_geom=record.metric_geom,
                        source_geom=record.source_geom,
                    )
                )
        matches.sort(key=lambda m: m.distance_m)
        return matches

    def within(self, point: QgsGeometry, radius_m: float) -> bool:
        return bool(self.find_matches(point, radius_m))

    @property
    def count(self) -> int:
        return len(self._records)


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
    points: List[SourcePoint] = []

    for layer in layers:
        transform = QgsCoordinateTransform(
            layer.crs(), metric_crs, _transform_context()
        )
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


def _format_ref_fields(
    matches: List[ReferenceMatch], separator: str
) -> Tuple[str, str, str]:
    if not matches:
        return "", "", ""
    return (
        separator.join(m.layer_name for m in matches),
        separator.join(str(m.feature_id) for m in matches),
        separator.join(f"{m.distance_m:.1f}" for m in matches),
    )


def _union_fields(sources: List[SourcePoint]) -> QgsFields:
    fields = QgsFields()
    fields.append(qgs_field("source_layer", QVariant.String))
    fields.append(qgs_field("source_fid", QVariant.Int))
    fields.append(qgs_field("category", QVariant.String))
    fields.append(qgs_field("ref_layers", QVariant.String))
    fields.append(qgs_field("ref_fids", QVariant.String))
    fields.append(qgs_field("ref_distances_m", QVariant.String))
    seen: Set[str] = {
        "source_layer",
        "source_fid",
        "category",
        "ref_layers",
        "ref_fids",
        "ref_distances_m",
    }
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
    entries: List[ClassifiedPoint],
    ref_separator: str,
) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")
    if not layer.isValid():
        return None

    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()

    features: List[QgsFeature] = []
    for entry in entries:
        sp = entry.source
        feat = QgsFeature(fields)
        feat.setGeometry(sp.feature.geometry())
        attrs = {f.name(): sp.feature[f.name()] for f in sp.feature.fields()}
        attrs["source_layer"] = sp.layer.name()
        attrs["source_fid"] = sp.feature.id()
        attrs["category"] = entry.category
        ref_layers, ref_fids, ref_distances = _format_ref_fields(
            entry.matches, ref_separator
        )
        attrs["ref_layers"] = ref_layers
        attrs["ref_fids"] = ref_fids
        attrs["ref_distances_m"] = ref_distances
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


def _link_line_geometry(
    sp: SourcePoint,
    match: ReferenceMatch,
) -> Optional[QgsGeometry]:
    """Кратчайшая линия между фото и опорным объектом в EPSG:4326."""
    project = QgsProject.instance()
    ref_layer = project.mapLayer(match.layer_id)

    photo_crs = sp.layer.crs() if sp.layer.crs().isValid() else WGS84
    ref_crs = (
        ref_layer.crs()
        if ref_layer and ref_layer.crs().isValid()
        else WGS84
    )

    photo_g = _to_wgs84(sp.feature.geometry(), photo_crs)

    ref_geom = match.source_geom
    if ref_layer:
        ref_feat = ref_layer.getFeature(match.feature_id)
        if ref_feat.isValid() and ref_feat.geometry() and not ref_feat.geometry().isEmpty():
            ref_geom = ref_feat.geometry()

    ref_g = _to_wgs84(ref_geom, ref_crs)
    if photo_g.isEmpty() or ref_g.isEmpty():
        return None

    line = photo_g.shortestLine(ref_g)
    if line.isEmpty():
        return None
    return line


def _create_link_layer(
    name: str,
    symbology: Dict[str, Any],
    classified: List[ClassifiedPoint],
) -> Optional[QgsVectorLayer]:
    layer = QgsVectorLayer("LineString?crs=EPSG:4326", name, "memory")
    if not layer.isValid():
        return None

    fields = QgsFields()
    fields.append(qgs_field("photo_layer", QVariant.String))
    fields.append(qgs_field("photo_fid", QVariant.Int))
    fields.append(qgs_field("ref_layer", QVariant.String))
    fields.append(qgs_field("ref_fid", QVariant.Int))
    fields.append(qgs_field("distance_m", QVariant.Double))

    provider = layer.dataProvider()
    provider.addAttributes(fields.toList())
    layer.updateFields()

    features: List[QgsFeature] = []
    for entry in classified:
        sp = entry.source
        for match in entry.matches:
            line_geom = _link_line_geometry(sp, match)
            if not line_geom or line_geom.isEmpty():
                continue
            feat = QgsFeature(fields)
            feat.setGeometry(line_geom)
            feat.setAttributes(
                [
                    sp.layer.name(),
                    sp.feature.id(),
                    match.layer_name,
                    match.feature_id,
                    round(match.distance_m, 1),
                ]
            )
            features.append(feat)

    if not features:
        return None

    provider.addFeatures(features)
    layer.updateExtents()

    apply_symbology(
        layer,
        {
            "geometry_type": "line",
            "symbology": symbology,
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
    ref_separator = cfg.get("ref_field_separator", "; ")
    link_layers_cfg = cfg.get("link_layers", {})

    progress = QProgressDialog(
        "Первичный анализ фото…",
        "Отмена",
        0,
        6,
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

    green: List[ClassifiedPoint] = []
    yellow: List[ClassifiedPoint] = []
    red: List[ClassifiedPoint] = []

    for sp in source_points:
        matches_a = index_a.find_matches(sp.metric_geom, radius_green)
        if matches_a:
            green.append(ClassifiedPoint(sp, "green", matches_a))
        else:
            matches_b = index_b.find_matches(sp.metric_geom, radius_yellow)
            if matches_b:
                yellow.append(ClassifiedPoint(sp, "yellow", matches_b))
            else:
                red.append(ClassifiedPoint(sp, "red", []))

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
    group_node = root.insertGroup(0, result_group)

    fields = _union_fields(source_points)
    results_cfg = cfg.get("results", [])
    category_map = {
        "green": green,
        "yellow": yellow,
        "red": red,
    }

    for res_def in results_cfg:
        cat = res_def.get("category", "")
        entries = category_map.get(cat, [])
        res_layer = _create_result_layer(
            res_def.get("name", cat),
            res_def.get("color", "#000000"),
            fields,
            entries,
            ref_separator,
        )
        if res_layer:
            QgsProject.instance().addMapLayer(res_layer, False)
            group_node.addLayer(res_layer)
            log_info(f"  → «{res_layer.name()}»: {len(entries)} точек")

    progress.setValue(5)
    progress.setLabelText("Построение линий связей…")
    QApplication.processEvents()
    if progress.wasCanceled():
        return AnalysisResult()

    for cat_key, classified in (("green", green), ("yellow", yellow)):
        link_def = link_layers_cfg.get(cat_key, {})
        if not link_def or not classified:
            continue
        symbology = {
            "color": link_def.get("color", "#000000"),
            "width": link_def.get("width", 0.8),
            "style": link_def.get("style", "dash"),
        }
        link_layer = _create_link_layer(
            link_def.get("name", f"Связи {cat_key}"),
            symbology,
            classified,
        )
        if link_layer:
            QgsProject.instance().addMapLayer(link_layer, False)
            group_node.addLayer(link_layer)
            log_info(f"  → «{link_layer.name()}»: {link_layer.featureCount()} линий")

    progress.setValue(6)
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

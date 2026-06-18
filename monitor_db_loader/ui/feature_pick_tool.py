# -*- coding: utf-8 -*-
"""Инструмент выбора объекта на карте для заполнения полей crm.tasks."""

from typing import Dict, List, Optional

from qgis.core import QgsFeature, QgsVectorLayer
from qgis.gui import QgsHighlight, QgsMapTool, QgsMapToolIdentify
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor

from ..core.crm_task_store import _normalize_id_value


class FeaturePickMapTool(QgsMapTool):
    """Клик по карте — идентификация объекта в разрешённых слоях."""

    featurePicked = pyqtSignal(str, str, QgsFeature, QgsVectorLayer)
    pickFailed = pyqtSignal(str)

    def __init__(self, canvas, parent=None):
        super().__init__(canvas)
        self._allowed_layers: List[QgsVectorLayer] = []
        self._source_field = ""
        self._subgroup_label = ""
        self._layer_field_map: Dict[str, str] = {}
        self._layer_labels: Dict[str, str] = {}
        self._identify = QgsMapToolIdentify(canvas)
        self._highlight: Optional[QgsHighlight] = None

    def set_target(
        self,
        layers: List[QgsVectorLayer],
        source_field: str,
        subgroup_label: str,
    ) -> None:
        self._allowed_layers = layers
        self._source_field = source_field
        self._subgroup_label = subgroup_label
        self._layer_field_map = {}
        self._layer_labels = {}

    def set_multi_target(
        self,
        layers: List[QgsVectorLayer],
        layer_field_map: Dict[str, str],
        layer_labels: Dict[str, str],
        subgroup_label: str,
    ) -> None:
        self._allowed_layers = layers
        self._layer_field_map = layer_field_map
        self._layer_labels = layer_labels
        self._subgroup_label = subgroup_label
        self._source_field = ""

    def activate(self) -> None:
        self.canvas().setCursor(QCursor(Qt.CrossCursor))

    def deactivate(self) -> None:
        self._clear_highlight()
        self.canvas().unsetCursor()

    def canvasReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return

        results = self._identify.identify(
            event.x(),
            event.y(),
            self._allowed_layers,
            QgsMapToolIdentify.TopDownAll,
        )
        if not results:
            self.pickFailed.emit(
                f"Объект не найден. Кликните объект из «{self._subgroup_label}»."
            )
            return

        for result in results:
            layer = getattr(result, "mLayer", None) or result.layer()
            if layer not in self._allowed_layers:
                continue

            feat = getattr(result, "mFeature", None) or result.feature()
            if not feat or not feat.isValid():
                continue

            source_field = self._layer_field_map.get(layer.id(), self._source_field)
            if not source_field:
                continue

            field_idx = layer.fields().indexOf(source_field)
            if field_idx < 0:
                self.pickFailed.emit(
                    f"В слое «{layer.name()}» нет поля «{source_field}»."
                )
                return

            value = _normalize_id_value(feat.attribute(field_idx))
            if value is None:
                self.pickFailed.emit(
                    f"Поле «{source_field}» пустое у выбранного объекта."
                )
                return

            self._show_highlight(feat, layer)
            self.featurePicked.emit(value, layer.name(), feat, layer)
            return

        self.pickFailed.emit(
            f"Кликните объект из «{self._subgroup_label}»."
        )

    def _show_highlight(self, feat: QgsFeature, layer: QgsVectorLayer) -> None:
        self._clear_highlight()
        highlight = QgsHighlight(self.canvas(), feat, layer)
        highlight.setWidth(3)
        highlight.show()
        self._highlight = highlight

    def _clear_highlight(self) -> None:
        if not self._highlight:
            return
        self._highlight.hide()
        self._highlight = None

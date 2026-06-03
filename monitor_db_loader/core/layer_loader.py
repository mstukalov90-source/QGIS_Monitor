# -*- coding: utf-8 -*-
"""Load vector layers into the QGIS project with grouping."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Union

from qgis.core import QgsMessageLog, QgsProject, Qgis
from qgis.PyQt.QtWidgets import QMessageBox

from .config import (
    LOG_CHANNEL,
    iter_all_layer_defs,
    layer_groups,
    ungrouped_layer_groups,
    ungrouped_layers,
)
from .db import DatabaseConnection
from .log_util import log_info, log_warning
from .symbology import apply_symbology


@dataclass
class LoadResult:
    loaded: int = 0
    failed: int = 0
    total: int = 0
    errors: List[str] = field(default_factory=list)
    layer_ids: List[str] = field(default_factory=list)
    group_names: List[str] = field(default_factory=list)


class LayerLoader:
    def __init__(self, config: Dict[str, Any], connection: DatabaseConnection):
        self.config = config
        self.connection = connection
        self.project = QgsProject.instance()
        self.root = self.project.layerTreeRoot()

    def remove_previous_layers(self, layer_ids: List[str], group_names: List[str]):
        for group_name in group_names:
            node = self.root.findGroup(group_name)
            if node:
                self.root.removeChildNode(node)

        for layer_id in layer_ids:
            layer = self.project.mapLayer(layer_id)
            if layer:
                self.project.removeMapLayer(layer)

    def load_all(self) -> LoadResult:
        result = LoadResult()
        result.total = sum(1 for _ in iter_all_layer_defs(self.config))
        log_info(f"Начало загрузки слоёв (всего в конфиге: {result.total})…")

        for group_def in layer_groups(self.config):
            group_name = group_def.get("group_name", "")
            group_node = self.root.addGroup(group_name)
            result.group_names.append(group_name)

            for layer_def in group_def.get("layers", []):
                self._load_single(layer_def, group_node, result)

        for layer_def in ungrouped_layers(self.config):
            self._load_single(layer_def, None, result)

        for group_def in ungrouped_layer_groups(self.config):
            group_name = group_def.get("group_name", "")
            group_node = self.root.addGroup(group_name)
            result.group_names.append(group_name)
            if group_def.get("default_visibility") is False:
                group_node.setItemVisibilityChecked(False)
            for layer_def in group_def.get("layers", []):
                self._load_single(layer_def, group_node, result)

        log_info(
            f"Загрузка завершена: успешно {result.loaded}, ошибок {result.failed} "
            f"из {result.total}."
        )
        for err in result.errors:
            log_warning(err)
        return result

    def _load_single(self, layer_def: Dict[str, Any], group_node, result: LoadResult):
        display_name = layer_def.get("display_name", layer_def.get("table_name", ""))
        layers_or_layer, err = self.connection.create_vector_layer(
            layer_def, display_name
        )

        if isinstance(layers_or_layer, list):
            if not layers_or_layer:
                result.failed += 1
                msg = f"Слой «{display_name}»: {err}"
                result.errors.append(msg)
                QgsMessageLog.logMessage(msg, LOG_CHANNEL, Qgis.Warning)
                return
            for sub_layer in layers_or_layer:
                self._add_layer(sub_layer, layer_def, group_node, result, sub_layer_gtype=True)
            result.loaded += 1
            return

        if layers_or_layer is None:
            result.failed += 1
            msg = f"Слой «{display_name}»: {err}"
            result.errors.append(msg)
            QgsMessageLog.logMessage(msg, LOG_CHANNEL, Qgis.Warning)
            return

        self._add_layer(layers_or_layer, layer_def, group_node, result)
        result.loaded += 1

    def _add_layer(
        self,
        layer,
        layer_def: Dict[str, Any],
        group_node,
        result: LoadResult,
        sub_layer_gtype: bool = False,
    ):
        sym_def = layer_def
        if sub_layer_gtype:
            gtype = None
            name = layer.name()
            for key, label in (("точки", "point"), ("линии", "line"), ("полигоны", "polygon")):
                if f" — {key}" in name or name.endswith(key):
                    gtype = label
                    break
            if gtype:
                sym_def = {
                    **layer_def,
                    "geometry_type": gtype,
                    "symbology": layer_def.get("symbology", {}).get(gtype, {}),
                }

        try:
            apply_symbology(layer, sym_def)
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Символика «{layer.name()}»: {exc}",
                LOG_CHANNEL,
                Qgis.Warning,
            )

        self.project.addMapLayer(layer, False)
        result.layer_ids.append(layer.id())

        if group_node is not None:
            group_node.addLayer(layer)
        else:
            self.root.insertLayer(0, layer)

        tree_layer = self.root.findLayer(layer.id())
        if tree_layer and layer_def.get("default_visibility") is False:
            tree_layer.setItemVisibilityChecked(False)

    @staticmethod
    def show_summary(result: LoadResult, parent=None):
        if result.failed == 0:
            QMessageBox.information(
                parent,
                "Monitor DB Loader",
                f"Загружено слоёв: {result.loaded} из {result.total}.",
            )
        else:
            QMessageBox.information(
                parent,
                "Monitor DB Loader",
                f"Загружено {result.loaded} из {result.total} слоёв.\n"
                f"Ошибок: {result.failed}. Подробности — в журнале сообщений QGIS.",
            )

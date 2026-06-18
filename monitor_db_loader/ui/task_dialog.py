# -*- coding: utf-8 -*-
"""Диалог списка задач CRM по району."""

from typing import Any, List, Optional, Set, Tuple

from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsHighlight
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.config import crm_task_store
from ..core.crm_task_store import fetch_task_for_feature
from ..core.crm_tasks import TaskFeature, TaskResult, TaskSubgroup, _connect_with_password
from ..core.task_dxf_export import export_tasks_to_dxf
from ..core.db import DatabaseConnection
from ..core.layer_utils import refresh_map_canvas
from ..core.qt_compat import BTN_OK, TEXT_FORMAT_RICH, register_modeless_dialog, show_modeless_dialog

TreeRole = Tuple[str, ...]


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _subgroup_field_names(subgroup: TaskSubgroup) -> List[str]:
    names: Set[str] = set()
    for feat in subgroup.features:
        names.update(feat.attributes.keys())
    return sorted(names)


class TaskDialog(QDialog):
    def __init__(
        self,
        result: TaskResult,
        iface,
        parent=None,
        *,
        config: Optional[dict] = None,
        db_conn: Optional[DatabaseConnection] = None,
    ):
        super().__init__(parent)
        self._result = result
        self._iface = iface
        self._config = config
        self._db_conn = db_conn
        self._store_cfg = crm_task_store(config) if config else {}
        self._highlight: Optional[QgsHighlight] = None
        self._current_group_name = ""
        self._current_subgroup: Optional[TaskSubgroup] = None
        self._selected_task_feat: Optional[TaskFeature] = None
        self._selected_row: Optional[int] = None

        self.setWindowTitle(f"Задачи — {result.district_name}")
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(960, 560)

        layout = QVBoxLayout(self)

        if result.apply_date_filter:
            subtitle = QLabel(
                f"Район: <b>{result.district_name}</b> · "
                f"Период отбора ордеров/уведомлений: "
                f"<b>{result.filter_date_from.toString('dd.MM.yyyy')}</b> — "
                f"<b>{result.filter_date_to.toString('dd.MM.yyyy')}</b>"
            )
        else:
            subtitle = QLabel(
                f"Район: <b>{result.district_name}</b> · "
                f"Ордера и уведомления: <b>без фильтра по дате</b>"
            )
        subtitle.setTextFormat(TEXT_FORMAT_RICH)
        layout.addWidget(subtitle)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Группы")
        self.tree.currentItemChanged.connect(self._on_tree_selection_changed)
        splitter.addWidget(self.tree)

        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.cellClicked.connect(self._on_row_clicked)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        self._populate_tree()

        self.status_label = QLabel(self._status_text())
        layout.addWidget(self.status_label)

        action_row = QHBoxLayout()
        self.execute_btn = QPushButton("Исполнить задачу")
        self.execute_btn.setEnabled(False)
        self.execute_btn.clicked.connect(self._on_execute_task)
        action_row.addWidget(self.execute_btn)

        self.export_dxf_btn = QPushButton("Экспорт задач в DXF")
        self.export_dxf_btn.setEnabled(self._result.total_count > 0)
        self.export_dxf_btn.clicked.connect(self._on_export_dxf)
        action_row.addWidget(self.export_dxf_btn)

        action_row.addStretch()
        layout.addLayout(action_row)

        buttons = QDialogButtonBox(BTN_OK)
        buttons.button(BTN_OK).setText("Закрыть")
        buttons.accepted.connect(self.close)
        layout.addWidget(buttons)

        first_subgroup = self._first_subgroup_item()
        if first_subgroup:
            self.tree.setCurrentItem(first_subgroup)

    def _status_text(self) -> str:
        return f"Всего объектов в районе: {self._result.total_count}"

    def _populate_tree(self) -> None:
        self.tree.clear()

        for group_index, group in enumerate(self._result.groups):
            group_count = sum(len(sub.features) for sub in group.subgroups)
            group_item = QTreeWidgetItem(
                [f"{group.name} ({group_count})"]
            )
            group_item.setData(0, Qt.UserRole, ("group", group_index))
            self.tree.addTopLevelItem(group_item)

            for sub_index, subgroup in enumerate(group.subgroups):
                sub_item = QTreeWidgetItem(
                    [f"{subgroup.name} ({len(subgroup.features)})"]
                )
                sub_item.setData(0, Qt.UserRole, ("sub", group_index, sub_index))
                group_item.addChild(sub_item)

            group_item.setExpanded(True)

    def _first_subgroup_item(self) -> Optional[QTreeWidgetItem]:
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            group_item = root.child(i)
            if group_item.childCount() > 0:
                return group_item.child(0)
        return None

    def _role_data(self, item: Optional[QTreeWidgetItem]) -> Optional[TreeRole]:
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if isinstance(data, tuple) and data and isinstance(data[0], str):
            return data
        return None

    def _subgroup_from_role(self, role: Optional[TreeRole]) -> Optional[TaskSubgroup]:
        if not role or role[0] != "sub" or len(role) != 3:
            return None
        group_index = int(role[1])
        sub_index = int(role[2])
        try:
            return self._result.groups[group_index].subgroups[sub_index]
        except (IndexError, ValueError):
            return None

    def _group_name_from_role(self, role: Optional[TreeRole]) -> str:
        if not role:
            return ""
        if role[0] == "group" and len(role) == 2:
            group_index = int(role[1])
            return self._result.groups[group_index].name
        if role[0] == "sub" and len(role) == 3:
            group_index = int(role[1])
            return self._result.groups[group_index].name
        return ""

    def _on_tree_selection_changed(self, current, previous) -> None:
        if current is None:
            self._current_subgroup = None
            self._current_group_name = ""
            self._clear_row_selection()
            self._fill_table(None)
            return

        role = self._role_data(current)
        if role and role[0] == "group" and current.childCount() > 0:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(current.child(0))
            self.tree.blockSignals(False)
            current = self.tree.currentItem()
            role = self._role_data(current)

        subgroup = self._subgroup_from_role(role)
        self._current_group_name = self._group_name_from_role(role)
        self._current_subgroup = subgroup
        self._clear_row_selection()
        self._fill_table(subgroup)

    def _clear_row_selection(self) -> None:
        self._selected_task_feat = None
        self._selected_row = None
        self.execute_btn.setEnabled(False)
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.blockSignals(False)

    def _fill_table(self, subgroup: Optional[TaskSubgroup]) -> None:
        self.table.blockSignals(True)
        self.table.clear()
        if subgroup is None:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.table.blockSignals(False)
            return

        field_names = _subgroup_field_names(subgroup)
        headers = ["Слой", "FID"] + field_names
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(subgroup.features))

        for row, task_feat in enumerate(subgroup.features):
            self._set_cell(row, 0, task_feat.layer_name)
            self._set_cell(row, 1, str(task_feat.feature_id))
            for col, field_name in enumerate(field_names, start=2):
                value = task_feat.attributes.get(field_name, "")
                self._set_cell(row, col, _format_value(value))

        self.table.blockSignals(False)

    def _set_cell(self, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setData(Qt.UserRole, row)
        self.table.setItem(row, col, item)

    def _task_feat_at_row(self, row: int) -> Optional[TaskFeature]:
        if self._current_subgroup is None or row < 0:
            return None
        if row >= len(self._current_subgroup.features):
            return None
        return self._current_subgroup.features[row]

    def _select_row(self, row: int) -> None:
        task_feat = self._task_feat_at_row(row)
        if task_feat is None:
            return
        self._selected_row = row
        self._selected_task_feat = task_feat
        self.execute_btn.setEnabled(True)
        try:
            self._zoom_to_feature(task_feat)
        except Exception:
            pass

    def _on_row_clicked(self, row: int, column: int) -> None:
        self._select_row(row)

    def _on_table_selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._selected_task_feat = None
            self._selected_row = None
            self.execute_btn.setEnabled(False)
            return
        self._select_row(rows[0].row())

    def _get_db_connection(self) -> Optional[DatabaseConnection]:
        if self._db_conn is not None:
            return self._db_conn
        if not self._config:
            return None
        return _connect_with_password(self._config, self)

    def _on_execute_task(self) -> None:
        if not self._selected_task_feat or not self._current_subgroup:
            return
        if not self._store_cfg:
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                "Конфигурация task_store не найдена.",
            )
            return

        conn = self._get_db_connection()
        if conn is None:
            return

        own_conn = conn is not self._db_conn
        record = fetch_task_for_feature(
            conn,
            self._current_subgroup.name,
            self._selected_task_feat.attributes,
            self._store_cfg,
        )
        if record is None:
            if own_conn:
                conn.close()
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                "Задача не найдена в crm.tasks.\n\n"
                "Сначала сохраните задачи при получении списка.",
            )
            return

        from .task_edit_dialog import TaskEditDialog

        def _on_edit_closed(_result: int) -> None:
            if own_conn:
                conn.close()

        TaskEditDialog.open_edit(
            record,
            conn,
            self._store_cfg,
            None,
            iface=self._iface,
            config=self._config,
            subgroup_name=self._current_subgroup.name,
            group_name=self._current_group_name,
            on_finished=_on_edit_closed,
        )

    def _on_export_dxf(self) -> None:
        if self._result.total_count == 0:
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                "Нет объектов для экспорта.",
            )
            return

        if not self._config:
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                "Конфигурация плагина не найдена.",
            )
            return

        default_name = f"tasks_{self._result.district_name or 'export'}.dxf"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт задач в DXF",
            default_name,
            "DXF files (*.dxf)",
        )
        if not path:
            return
        if not path.lower().endswith(".dxf"):
            path += ".dxf"

        progress = QProgressDialog(
            "Экспорт задач в DXF…",
            "Отмена",
            0,
            max(self._result.total_count, 1),
            self,
        )
        progress.setWindowTitle("Monitor DB Loader")
        progress.setMinimumDuration(0)
        progress.setValue(0)

        stats = export_tasks_to_dxf(path, self._result, self._config)
        progress.setValue(self._result.total_count)

        if stats.errors:
            QMessageBox.critical(
                self,
                "Monitor DB Loader — задачи",
                "Не удалось экспортировать задачи в DXF:\n"
                + "\n".join(stats.errors),
            )
            return

        QMessageBox.information(
            self,
            "Monitor DB Loader — задачи",
            f"Экспорт завершён.\n\n"
            f"Файл: {path}\n"
            f"Объектов: {stats.exported}\n"
            f"Слоёв DXF: {stats.layers_written}\n"
            f"Пропущено (пустая геометрия): {stats.skipped_empty}\n"
            f"Пропущено (ошибка): {stats.skipped_invalid}",
        )

    def _ensure_layer_visible(self, layer) -> None:
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        if node and not node.isVisible():
            node.setItemVisibilityChecked(True)
            refresh_map_canvas(self._iface)

    def _zoom_extent_for_geometry(
        self, geom: QgsGeometry, layer_crs, canvas
    ) -> Optional[QgsRectangle]:
        if QgsWkbTypes.geometryType(geom.wkbType()) == QgsWkbTypes.PointGeometry:
            center = geom.asPoint()
            span = 0.0015
            extent = QgsRectangle(
                center.x() - span,
                center.y() - span,
                center.x() + span,
                center.y() + span,
            )
        else:
            extent = geom.boundingBox()

        if extent.isNull() or extent.isEmpty():
            return None

        dest_crs = canvas.mapSettings().destinationCrs()
        if layer_crs.isValid() and dest_crs.isValid() and layer_crs != dest_crs:
            extent = QgsCoordinateTransform(
                layer_crs, dest_crs, QgsProject.instance()
            ).transform(extent)

        rect = QgsRectangle(extent)
        rect.scale(1.5)
        return rect

    def _zoom_to_feature(self, task_feat: TaskFeature) -> None:
        layer = task_feat.layer
        if not layer or not layer.isValid():
            return

        feat = layer.getFeature(task_feat.feature_id)
        if not feat.isValid():
            return

        geom = feat.geometry()
        if not geom or geom.isEmpty():
            return

        self._ensure_layer_visible(layer)

        canvas = self._iface.mapCanvas()
        rect = self._zoom_extent_for_geometry(geom, layer.crs(), canvas)
        if rect is None:
            return

        canvas.setExtent(rect)
        canvas.refresh()

        self._clear_highlight()

        highlight = QgsHighlight(canvas, feat, layer)
        highlight.setWidth(3)
        highlight.show()
        self._highlight = highlight

    def _clear_highlight(self) -> None:
        if not self._highlight:
            return
        self._highlight.hide()
        self._highlight = None

    def _close_db_conn(self) -> None:
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None

    @classmethod
    def open(
        cls,
        result: TaskResult,
        iface,
        parent=None,
        *,
        config: Optional[dict] = None,
        db_conn: Optional[DatabaseConnection] = None,
    ) -> "TaskDialog":
        dlg = cls(
            result,
            iface,
            None,
            config=config,
            db_conn=db_conn,
        )
        register_modeless_dialog(iface, dlg)
        show_modeless_dialog(dlg)
        return dlg

    def closeEvent(self, event) -> None:
        self._clear_highlight()
        self._close_db_conn()
        super().closeEvent(event)

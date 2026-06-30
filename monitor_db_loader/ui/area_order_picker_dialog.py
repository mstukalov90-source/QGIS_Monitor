# -*- coding: utf-8 -*-
"""Диалог выбора площадного заказа для office."""

from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.crm_tasks import TaskFeature
from ..core.crm_ui_constants import (
    analise_workflow_status,
    analise_workflow_status_object_name,
    can_start_analise,
    format_analise_workflow_status,
    format_area_hectares,
    format_task_table_cell,
)
from ..core.qt_compat import DIALOG_ACCEPTED, dialog_exec
from .crm_theme import apply_crm_theme, style_analise_badge, style_button


class AreaOrderPickerDialog(QDialog):
    def __init__(
        self,
        orders: List[TaskFeature],
        current_login: str,
        parent=None,
        *,
        loading: bool = False,
    ):
        super().__init__(parent)
        self._orders = list(orders)
        self._current_login = current_login
        self._selected: Optional[TaskFeature] = None

        self.setWindowTitle("Выбор площадного заказа")
        self.setModal(True)
        self.resize(920, 480)
        apply_crm_theme(self, object_name="crmAreaOrderPicker")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Выбор площадного заказа")
        title.setObjectName("crmTitle")
        layout.addWidget(title)

        hint = QLabel(
            "Выберите заказ для анализа активных задач внутри полигона"
        )
        hint.setObjectName("crmMuted")
        layout.addWidget(hint)

        self._status_label = QLabel("")
        self._status_label.setObjectName("crmMuted")
        layout.addWidget(self._status_label)

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(
            ["Номер задачи", "Площадь", "Дата обследования", "Статус", ""]
        )
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self._table, stretch=1)

        actions = QHBoxLayout()
        actions.addStretch()
        self._refresh_btn = QPushButton("Обновить список")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        actions.addWidget(self._refresh_btn)
        layout.addLayout(actions)

        self._on_refresh = None
        self.set_loading(loading)
        self._fill_table()

    def set_refresh_handler(self, handler) -> None:
        self._on_refresh = handler

    def set_orders(self, orders: List[TaskFeature]) -> None:
        self._orders = list(orders)
        self._fill_table()

    def set_loading(self, loading: bool) -> None:
        self._refresh_btn.setEnabled(not loading)
        self._table.setEnabled(not loading)
        if loading:
            self._status_label.setText("Загрузка заказов…")
        elif not self._orders:
            self._status_label.setText("В районе нет площадных заказов")
        else:
            self._status_label.clear()

    def selected_order(self) -> Optional[TaskFeature]:
        return self._selected

    def _on_refresh_clicked(self) -> None:
        if self._on_refresh:
            self._on_refresh()

    def _attr_string(self, attrs: dict, field: str) -> str:
        value = attrs.get(field)
        if value is None or value == "":
            return "—"
        return str(value)

    def _fill_table(self) -> None:
        self._table.setRowCount(0)
        for order in self._orders:
            row = self._table.rowCount()
            self._table.insertRow(row)
            attrs = order.attributes or {}
            workflow = analise_workflow_status(attrs)
            can_start = can_start_analise(attrs, self._current_login)
            action_label = "В работу" if workflow == "idle" else "Продолжить"

            self._table.setItem(
                row, 0, QTableWidgetItem(self._attr_string(attrs, "task_number"))
            )
            self._table.setItem(
                row,
                1,
                QTableWidgetItem(
                    format_area_hectares(attrs.get("area")) or "—"
                ),
            )
            self._table.setItem(
                row,
                2,
                QTableWidgetItem(
                    format_task_table_cell(attrs.get("date_survey"), "date") or "—"
                ),
            )

            status_item = QTableWidgetItem(format_analise_workflow_status(attrs))
            status_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 3, status_item)

            if can_start:
                btn = QPushButton(action_label)
                style_button(btn, "crmBtnPrimary")
                btn.clicked.connect(
                    lambda _checked=False, feat=order: self._select_order(feat)
                )
                self._table.setCellWidget(row, 4, btn)
            else:
                badge = QLabel(format_analise_workflow_status(attrs))
                style_analise_badge(badge, workflow)
                badge.setAlignment(Qt.AlignCenter)
                self._table.setCellWidget(row, 4, badge)

    def _select_order(self, order: TaskFeature) -> None:
        self._selected = order
        self.accept()

    @classmethod
    def pick_order(
        cls,
        orders: List[TaskFeature],
        current_login: str,
        parent=None,
        *,
        on_refresh=None,
    ) -> Optional[TaskFeature]:
        dlg = cls(orders, current_login, parent)
        if on_refresh:
            dlg.set_refresh_handler(on_refresh)
        if dialog_exec(dlg) == DIALOG_ACCEPTED:
            return dlg.selected_order()
        return None

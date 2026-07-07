# -*- coding: utf-8 -*-
"""Вкладки источников задач CRM."""

from typing import Callable, List, Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..core.crm_ui_constants import (
    TASK_SOURCES,
    TASK_SOURCE_LABELS,
    TaskSource,
    is_area_source,
)
from .crm_theme import style_button, style_source_tab


class TaskSourceTabs(QWidget):
    sourceChanged = pyqtSignal(str)
    pauseOrderClicked = pyqtSignal()
    completeOrderClicked = pyqtSignal()
    ordersToggleClicked = pyqtSignal()
    selectOrderClicked = pyqtSignal()
    placePointClicked = pyqtSignal()

    def __init__(self, parent=None, allowed_sources: Optional[List[TaskSource]] = None):
        super().__init__(parent)
        self._value: TaskSource = "active"
        self._buttons: List[QPushButton] = []
        self._source_by_button: dict = {}
        self._loading = False
        self._allowed_sources = (
            list(allowed_sources) if allowed_sources is not None else list(TASK_SOURCES)
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        row_main = QHBoxLayout()
        row_main.setContentsMargins(0, 0, 0, 0)
        row_main.setSpacing(6)

        row_area = QHBoxLayout()
        row_area.setContentsMargins(0, 0, 0, 0)
        row_area.setSpacing(6)

        for source in TASK_SOURCES:
            btn = QPushButton(TASK_SOURCE_LABELS[source])
            btn.setCheckable(True)
            btn.clicked.connect(self._make_handler(source))
            self._buttons.append(btn)
            self._source_by_button[btn] = source
            if is_area_source(source):
                row_area.addWidget(btn)
            else:
                row_main.addWidget(btn)

        row_main.addStretch()
        row_area.addStretch()
        outer.addLayout(row_main)
        outer.addLayout(row_area)

        row_office = QHBoxLayout()
        row_office.setContentsMargins(0, 0, 0, 0)
        row_office.setSpacing(6)
        self._select_order_btn = QPushButton("Выбрать заказ")
        self._select_order_btn.clicked.connect(self.selectOrderClicked.emit)
        row_office.addWidget(self._select_order_btn)

        self._orders_toggle_btn = QPushButton("Заказы на карте")
        self._orders_toggle_btn.setCheckable(True)
        self._orders_toggle_btn.clicked.connect(self.ordersToggleClicked.emit)
        row_office.addWidget(self._orders_toggle_btn)

        self._pause_order_btn = QPushButton("Пауза")
        self._pause_order_btn.clicked.connect(self.pauseOrderClicked.emit)
        row_office.addWidget(self._pause_order_btn)

        self._complete_order_btn = QPushButton("Завершить анализ")
        style_button(self._complete_order_btn, "crmBtnPrimary")
        self._complete_order_btn.clicked.connect(self.completeOrderClicked.emit)
        row_office.addWidget(self._complete_order_btn)

        self._place_point_btn = QPushButton("Добавить разрытие на карте")
        self._place_point_btn.setCheckable(True)
        self._place_point_btn.clicked.connect(self.placePointClicked.emit)
        row_office.addWidget(self._place_point_btn)

        row_office.addStretch()
        outer.addLayout(row_office)
        self._office_row_widgets = [
            self._select_order_btn,
            self._orders_toggle_btn,
            self._pause_order_btn,
            self._complete_order_btn,
            self._place_point_btn,
        ]
        for widget in self._office_row_widgets:
            widget.hide()

        self.set_allowed_sources(self._allowed_sources)
        self._sync_styles()

    def set_allowed_sources(self, sources: List[TaskSource]) -> None:
        self._allowed_sources = list(sources)
        allowed = set(self._allowed_sources)
        for btn, src in self._source_by_button.items():
            visible = src in allowed
            btn.setVisible(visible)
            if not visible and btn.isChecked():
                btn.setChecked(False)
        if self._value not in allowed and self._allowed_sources:
            self.set_value(self._allowed_sources[0])

    def set_office_actions(
        self,
        *,
        visible: bool,
        awaiting_order: bool = False,
        working: bool = False,
        can_complete: bool = False,
        complete_tooltip: str = "",
        orders_on_map: bool = False,
        place_point_mode: bool = False,
    ) -> None:
        for widget in self._office_row_widgets:
            widget.setVisible(visible)
        if not visible:
            return

        self._select_order_btn.setVisible(awaiting_order or working)
        self._orders_toggle_btn.setVisible(working)
        self._pause_order_btn.setVisible(working)
        self._complete_order_btn.setVisible(working)
        self._place_point_btn.setVisible(working)

        self._orders_toggle_btn.blockSignals(True)
        self._orders_toggle_btn.setChecked(orders_on_map)
        self._orders_toggle_btn.blockSignals(False)

        self._complete_order_btn.setEnabled(can_complete and not self._loading)
        self._complete_order_btn.setToolTip(complete_tooltip)

        self._place_point_btn.blockSignals(True)
        self._place_point_btn.setChecked(place_point_mode)
        self._place_point_btn.blockSignals(False)
        self._place_point_btn.setText(
            "Отменить добавление"
            if place_point_mode
            else "Добавить разрытие на карте"
        )
        self._place_point_btn.setEnabled(not self._loading)

    def _make_handler(self, source: TaskSource) -> Callable:
        def handler(checked: bool = False) -> None:
            if self._loading or source == self._value:
                return
            self.set_value(source)
            self.sourceChanged.emit(source)

        return handler

    def set_value(self, source: str) -> None:
        if source not in self._allowed_sources:
            return
        self._value = source  # type: ignore[assignment]
        for btn, src in self._source_by_button.items():
            btn.setChecked(src == source)
        self._sync_styles()

    def value(self) -> str:
        return self._value

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        for btn in self._buttons:
            if btn.isVisible():
                btn.setEnabled(not loading)
        self._pause_order_btn.setEnabled(not loading)
        self._place_point_btn.setEnabled(not loading and self._place_point_btn.isVisible())

    def _sync_styles(self) -> None:
        for btn, src in self._source_by_button.items():
            if btn.isVisible():
                style_source_tab(btn, src == self._value)

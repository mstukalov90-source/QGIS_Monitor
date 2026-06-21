# -*- coding: utf-8 -*-
"""Вкладки источников задач CRM."""

from typing import Callable, List

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QWidget,
)

from ..core.crm_ui_constants import TASK_SOURCES, TASK_SOURCE_LABELS, TaskSource
from .crm_theme import style_source_tab


class TaskSourceTabs(QWidget):
    sourceChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value: TaskSource = "active"
        self._buttons: List[QPushButton] = []
        self._loading = False

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        self._layout = QHBoxLayout(container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        for source in TASK_SOURCES:
            btn = QPushButton(TASK_SOURCE_LABELS[source])
            btn.setCheckable(True)
            btn.clicked.connect(self._make_handler(source))
            self._buttons.append(btn)
            self._layout.addWidget(btn)

        self._layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)
        self._sync_styles()

    def _make_handler(self, source: TaskSource) -> Callable:
        def handler(checked: bool = False) -> None:
            if self._loading or source == self._value:
                return
            self.set_value(source)
            self.sourceChanged.emit(source)

        return handler

    def set_value(self, source: str) -> None:
        self._value = source  # type: ignore[assignment]
        for btn, src in zip(self._buttons, TASK_SOURCES):
            btn.setChecked(src == source)
        self._sync_styles()

    def value(self) -> str:
        return self._value

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        for btn in self._buttons:
            btn.setEnabled(not loading)

    def _sync_styles(self) -> None:
        for btn, src in zip(self._buttons, TASK_SOURCES):
            style_source_tab(btn, src == self._value)

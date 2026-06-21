# -*- coding: utf-8 -*-
"""Диалог выбора района для первичного анализа фото и CRM."""

from dataclasses import dataclass
from typing import List, Optional

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ..core.qt_compat import (
    BTN_CANCEL,
    BTN_OK,
    DIALOG_ACCEPTED,
    dialog_exec,
)
from .crm_theme import apply_crm_theme, style_button


@dataclass
class DistrictChoice:
    rayon: str
    apply_date_filter: bool = True


def _collect_rayon_names(layer: QgsVectorLayer, field: str) -> List[str]:
    idx = layer.fields().indexOf(field)
    if idx < 0:
        return []
    names = set()
    for feat in layer.getFeatures():
        val = feat[field]
        if val is None:
            continue
        text = str(val).strip()
        if text:
            names.add(text)
    return sorted(names)


class DistrictDialog(QDialog):
    def __init__(
        self,
        layer: QgsVectorLayer,
        field: str,
        parent=None,
        *,
        crm_mode: bool = False,
        date_from: Optional[QDate] = None,
        date_to: Optional[QDate] = None,
    ):
        super().__init__(parent)
        title = (
            "Monitor CRM — получить задачу"
            if crm_mode
            else "Monitor DB Loader — выбор района"
        )
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(440, 540 if crm_mode else 480)

        if crm_mode:
            apply_crm_theme(self, object_name="crmDistrictCard")

        self._field = field
        self._all_rayons = _collect_rayon_names(layer, field)
        self._date_filter_checkbox: Optional[QCheckBox] = None

        layout = QVBoxLayout(self)

        if crm_mode:
            title_label = QLabel("Monitor CRM")
            title_label.setObjectName("crmTitle")
            layout.addWidget(title_label)
            hint = QLabel("Выберите район для загрузки задач из crm.tasks")
            hint.setObjectName("crmHint")
            layout.addWidget(hint)
            layer_hint = QLabel(f"Слой «{layer.name()}», поле «{field}»")
            layer_hint.setObjectName("crmMuted")
            layout.addWidget(layer_hint)
        else:
            hint = (
                f"Выберите район для анализа (поле «{field}»):\n"
                f"Слой «{layer.name()}»"
            )
            layout.addWidget(QLabel(hint))

        search_label = QLabel("Поиск района")
        if crm_mode:
            search_label.setObjectName("crmMuted")
        layout.addWidget(search_label)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск района…")
        self.search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.list_widget)

        if crm_mode:
            period = ""
            if date_from and date_to and date_from.isValid() and date_to.isValid():
                period = (
                    f" ({date_from.toString('dd.MM.yyyy')} — "
                    f"{date_to.toString('dd.MM.yyyy')})"
                )
            self._date_filter_checkbox = QCheckBox(
                f"Фильтровать ордера и уведомления по дате{period}"
            )
            self._date_filter_checkbox.setChecked(True)
            layout.addWidget(self._date_filter_checkbox)

        if crm_mode:
            self._submit_btn = QPushButton("Получить задачу")
            style_button(self._submit_btn, "crmBtnPrimary")
            self._submit_btn.clicked.connect(self._on_accept)
            layout.addWidget(self._submit_btn)

            buttons = QDialogButtonBox(BTN_CANCEL)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
        else:
            buttons = QDialogButtonBox(BTN_OK | BTN_CANCEL)
            buttons.accepted.connect(self._on_accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        self._populate_list(self._all_rayons)
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def _populate_list(self, rayons: List[str]) -> None:
        self.list_widget.clear()
        for name in rayons:
            self.list_widget.addItem(QListWidgetItem(name))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        if not needle:
            filtered = self._all_rayons
        else:
            filtered = [r for r in self._all_rayons if needle in r.casefold()]
        self._populate_list(filtered)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        if item:
            self.accept()

    def _on_accept(self) -> None:
        if not self.list_widget.currentItem():
            return
        self.accept()

    def selected_rayon(self) -> str:
        item = self.list_widget.currentItem()
        return item.text() if item else ""

    def apply_date_filter(self) -> bool:
        if self._date_filter_checkbox is None:
            return True
        return self._date_filter_checkbox.isChecked()

    @staticmethod
    def list_rayons(layer: QgsVectorLayer, field: str) -> List[str]:
        return _collect_rayon_names(layer, field)

    @staticmethod
    def choose(
        layer: QgsVectorLayer, field: str, parent=None
    ) -> Optional[str]:
        if layer.fields().indexOf(field) < 0:
            return None
        rayons = _collect_rayon_names(layer, field)
        if not rayons:
            return None
        dlg = DistrictDialog(layer, field, parent)
        if dialog_exec(dlg) != DIALOG_ACCEPTED:
            return None
        selected = dlg.selected_rayon().strip()
        return selected or None

    @staticmethod
    def choose_for_crm(
        layer: QgsVectorLayer,
        field: str,
        parent=None,
        date_from: Optional[QDate] = None,
        date_to: Optional[QDate] = None,
    ) -> Optional[DistrictChoice]:
        if layer.fields().indexOf(field) < 0:
            return None
        rayons = _collect_rayon_names(layer, field)
        if not rayons:
            return None
        dlg = DistrictDialog(
            layer,
            field,
            parent,
            crm_mode=True,
            date_from=date_from,
            date_to=date_to,
        )
        if dialog_exec(dlg) != DIALOG_ACCEPTED:
            return None
        selected = dlg.selected_rayon().strip()
        if not selected:
            return None
        return DistrictChoice(
            rayon=selected,
            apply_date_filter=dlg.apply_date_filter(),
        )

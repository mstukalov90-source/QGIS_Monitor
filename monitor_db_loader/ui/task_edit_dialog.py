# -*- coding: utf-8 -*-
"""Диалог редактирования строки crm.tasks."""

from typing import Callable, Dict, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..core.crm_pick import resolve_pick_target
from ..core.crm_task_store import (
    TASK_COLUMN_LABELS,
    TASK_FORM_FIELDS,
    TASK_ID_COLUMNS,
    TaskRecord,
    update_task_record,
)
from ..core.db import DatabaseConnection
from ..core.qt_compat import BTN_CANCEL, BTN_OK, TEXT_FORMAT_RICH, register_modeless_dialog, show_modeless_dialog
from .feature_pick_tool import FeaturePickMapTool


class TaskEditDialog(QDialog):
    def __init__(
        self,
        record: TaskRecord,
        conn: DatabaseConnection,
        store_cfg: Dict,
        parent=None,
        *,
        iface=None,
        config: Optional[dict] = None,
    ):
        super().__init__(parent)
        self._record = record
        self._conn = conn
        self._store_cfg = store_cfg
        self._iface = iface
        self._config = config
        self._fields: Dict[str, QLineEdit] = {}
        self._pick_buttons: Dict[str, QPushButton] = {}
        self._pick_tool: Optional[FeaturePickMapTool] = None
        self._active_pick_column: Optional[str] = None

        self.setWindowTitle("Исполнить задачу")
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(560, 460)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Редактирование записи в таблице <b>crm.tasks</b>")
        )

        self._pick_status = QLabel("")
        self._pick_status.setTextFormat(TEXT_FORMAT_RICH)
        self._pick_status.setWordWrap(True)
        self._pick_status.hide()
        layout.addWidget(self._pick_status)

        form = QFormLayout()
        key_edit = QLineEdit(record.key)
        key_edit.setReadOnly(True)
        form.addRow(TASK_COLUMN_LABELS["key"], key_edit)

        for field_name in TASK_FORM_FIELDS:
            edit = QLineEdit(getattr(record, field_name) or "")
            self._fields[field_name] = edit
            if field_name in TASK_ID_COLUMNS:
                row = QHBoxLayout()
                row.addWidget(edit, stretch=1)
                pick_btn = QPushButton("Карта")
                pick_btn.clicked.connect(
                    lambda checked=False, col=field_name: self._start_pick(col)
                )
                self._pick_buttons[field_name] = pick_btn
                row.addWidget(pick_btn)
                form.addRow(TASK_COLUMN_LABELS.get(field_name, field_name), row)
            else:
                form.addRow(TASK_COLUMN_LABELS.get(field_name, field_name), edit)

        layout.addLayout(form)

        self._cancel_pick_btn = QPushButton("Отмена выбора на карте")
        self._cancel_pick_btn.hide()
        self._cancel_pick_btn.clicked.connect(self._cancel_pick)
        layout.addWidget(self._cancel_pick_btn)

        self._buttons = QDialogButtonBox(BTN_OK | BTN_CANCEL)
        self._buttons.button(BTN_OK).setText("Сохранить")
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _ensure_pick_tool(self) -> Optional[FeaturePickMapTool]:
        if self._iface is None:
            return None
        if self._pick_tool is None:
            canvas = self._iface.mapCanvas()
            self._pick_tool = FeaturePickMapTool(canvas, self)
            self._pick_tool.featurePicked.connect(self._on_feature_picked)
            self._pick_tool.pickFailed.connect(self._on_pick_failed)
        return self._pick_tool

    def _start_pick(self, task_column: str) -> None:
        if self._active_pick_column == task_column:
            self._cancel_pick()
            return

        if self._iface is None or self._config is None:
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                "Выбор с карты недоступен: нет доступа к карте QGIS.",
            )
            return

        if self._active_pick_column:
            self._cancel_pick(silent=True)

        target = resolve_pick_target(self._config, task_column)
        if target is None:
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                f"Не найдена конфигурация для поля «{task_column}».",
            )
            return

        if not target.layers:
            missing = ", ".join(target.missing) if target.missing else "—"
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                f"Слои для «{target.subgroup_name}» не найдены в проекте.\n\n"
                f"Загрузите слои Monitor DB.\n"
                f"Не найдено: {missing}",
            )
            return

        tool = self._ensure_pick_tool()
        if tool is None:
            return

        tool.set_target(
            target.layers,
            target.source_field,
            target.subgroup_name,
        )

        self._active_pick_column = task_column
        self._pick_status.setText(
            f"<b>Выбор на карте:</b> {target.subgroup_name} "
            f"({target.source_field}) — кликните объект на карте"
        )
        self._pick_status.show()
        self._cancel_pick_btn.show()
        self._buttons.setEnabled(False)
        for col, btn in self._pick_buttons.items():
            btn.setEnabled(col == task_column)

        canvas = self._iface.mapCanvas()
        canvas.setFocus()
        canvas.setMapTool(tool)

    def _cancel_pick(self, silent: bool = False) -> None:
        if self._pick_tool and self._iface:
            canvas = self._iface.mapCanvas()
            if canvas.mapTool() is self._pick_tool:
                canvas.unsetMapTool(self._pick_tool)

        self._active_pick_column = None
        self._pick_status.hide()
        self._cancel_pick_btn.hide()
        self._buttons.setEnabled(True)
        for btn in self._pick_buttons.values():
            btn.setEnabled(True)

    def _on_feature_picked(
        self, value: str, layer_name: str, feat, layer
    ) -> None:
        column = self._active_pick_column
        if not column or column not in self._fields:
            return

        self._fields[column].setText(value)
        self._cancel_pick(silent=True)
        self.raise_()
        self.activateWindow()

    def _on_pick_failed(self, message: str) -> None:
        parent = self._iface.mainWindow() if self._iface else self
        QMessageBox.warning(parent, "Monitor DB Loader — задачи", message)

    def _on_save(self) -> None:
        self._cancel_pick(silent=True)

        updated = TaskRecord(
            key=self._record.key,
            type=self._fields["type"].text().strip(),
            photo_uuid=self._fields["photo_uuid"].text().strip() or None,
            photo_lens=self._fields["photo_lens"].text().strip() or None,
            ogh_id=self._fields["ogh_id"].text().strip() or None,
            oati_id=self._fields["oati_id"].text().strip() or None,
            earthwork_id=self._fields["earthwork_id"].text().strip() or None,
            localwork_id=self._fields["localwork_id"].text().strip() or None,
            avr_mos_id=self._fields["avr_mos_id"].text().strip() or None,
        )

        try:
            update_task_record(self._conn, updated, self._store_cfg)
        except ValueError as exc:
            QMessageBox.warning(self, "Monitor DB Loader — задачи", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Monitor DB Loader — задачи",
                f"Не удалось сохранить задачу:\n{exc}",
            )
            return

        self._record = updated
        self.accept()

    def reject(self) -> None:
        self._cancel_pick(silent=True)
        super().reject()

    def closeEvent(self, event) -> None:
        self._cancel_pick(silent=True)
        super().closeEvent(event)

    @property
    def record(self) -> TaskRecord:
        return self._record

    @staticmethod
    def open_edit(
        record: TaskRecord,
        conn: DatabaseConnection,
        store_cfg: Dict,
        parent=None,
        *,
        iface=None,
        config: Optional[dict] = None,
        on_finished: Optional[Callable[[int], None]] = None,
    ) -> "TaskEditDialog":
        dlg = TaskEditDialog(
            record,
            conn,
            store_cfg,
            None,
            iface=iface,
            config=config,
        )
        if on_finished is not None:
            dlg.finished.connect(on_finished)
        register_modeless_dialog(iface, dlg)
        show_modeless_dialog(dlg)
        return dlg

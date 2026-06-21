# -*- coding: utf-8 -*-
"""Диалог редактирования строки crm.tasks."""

from typing import Callable, Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
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

from ..core.crm_pick import LinkPickBundle, resolve_link_pick_bundle
from ..core.crm_task_store import (
    STATION_COLUMNS,
    TASK_COLUMN_LABELS,
    TaskRecord,
    send_task_to_done_illegal,
    send_task_to_done_legal,
    send_task_to_field,
    send_task_to_clear,
    ensure_all_snapshot_tables,
    task_form_field_groups,
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
        subgroup_name: Optional[str] = None,
        group_name: Optional[str] = None,
    ):
        super().__init__(parent)
        self._record = record
        self._conn = conn
        self._store_cfg = store_cfg
        self._iface = iface
        self._config = config
        self._subgroup_name = subgroup_name
        self._group_name = group_name or record.type
        self._readonly_fields, self._link_fields = task_form_field_groups(
            self._group_name, subgroup_name, store_cfg, record
        )
        self._form_fields = self._readonly_fields + self._link_fields + list(STATION_COLUMNS)
        self._fields: Dict[str, QLineEdit] = {}
        self._pick_tool: Optional[FeaturePickMapTool] = None
        self._pick_bundle: Optional[LinkPickBundle] = None
        self._picking = False
        self._action_buttons: List[QPushButton] = []

        if self._conn and self._store_cfg:
            try:
                ensure_all_snapshot_tables(self._conn, self._store_cfg)
            except Exception:
                pass

        self.setWindowTitle("Исполнить задачу")
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(520, 500)

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

        if self._readonly_fields:
            form.addRow(QLabel("<b>Источник</b>"))
            for field_name in self._readonly_fields:
                edit = QLineEdit(getattr(record, field_name) or "")
                edit.setReadOnly(True)
                self._fields[field_name] = edit
                form.addRow(TASK_COLUMN_LABELS.get(field_name, field_name), edit)

        if self._link_fields:
            form.addRow(QLabel("<b>Сопоставление</b>"))
            for field_name in self._link_fields:
                edit = QLineEdit(getattr(record, field_name) or "")
                self._fields[field_name] = edit
                form.addRow(TASK_COLUMN_LABELS.get(field_name, field_name), edit)

        form.addRow(QLabel("<b>Данные из Станции</b>"))
        for field_name in STATION_COLUMNS:
            edit = QLineEdit(getattr(record, field_name) or "")
            self._fields[field_name] = edit
            form.addRow(TASK_COLUMN_LABELS[field_name], edit)

        layout.addLayout(form)

        self._pick_map_btn = QPushButton("Указать на карте")
        self._pick_map_btn.setEnabled(bool(self._link_fields))
        self._pick_map_btn.clicked.connect(self._toggle_link_pick)
        layout.addWidget(self._pick_map_btn)

        self._cancel_pick_btn = QPushButton("Отмена выбора на карте")
        self._cancel_pick_btn.hide()
        self._cancel_pick_btn.clicked.connect(self._cancel_pick)
        layout.addWidget(self._cancel_pick_btn)

        footer = QHBoxLayout()
        for label, handler in (
            ("Отправить задачу в поле", self._on_send_to_field),
            ("Закрыть легальное", self._on_close_legal),
            ("Закрыть нелегальное", self._on_close_illegal),
            ("Разрытие отсутствует", self._on_disruption_absent),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            self._action_buttons.append(btn)
            footer.addWidget(btn)
        footer.addStretch()

        self._buttons = QDialogButtonBox(BTN_OK | BTN_CANCEL)
        self._buttons.button(BTN_OK).setText("Сохранить")
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        footer.addWidget(self._buttons)
        layout.addLayout(footer)

    def _ensure_pick_tool(self) -> Optional[FeaturePickMapTool]:
        if self._iface is None:
            return None
        if self._pick_tool is None:
            canvas = self._iface.mapCanvas()
            self._pick_tool = FeaturePickMapTool(canvas, self)
            self._pick_tool.featurePicked.connect(self._on_feature_picked)
            self._pick_tool.pickFailed.connect(self._on_pick_failed)
        return self._pick_tool

    def _toggle_link_pick(self) -> None:
        if self._picking:
            self._cancel_pick()
            return
        self._start_link_pick()

    def _start_link_pick(self) -> None:
        if self._iface is None or self._config is None:
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                "Выбор с карты недоступен: нет доступа к карте QGIS.",
            )
            return

        bundle = resolve_link_pick_bundle(self._config, self._link_fields)
        if bundle is None or not bundle.layers:
            missing = ", ".join(bundle.missing) if bundle and bundle.missing else "—"
            QMessageBox.warning(
                self,
                "Monitor DB Loader — задачи",
                "Слои для сопоставления не найдены в проекте.\n\n"
                "Загрузите слои Monitor DB.\n"
                f"Не найдено: {missing}",
            )
            return

        tool = self._ensure_pick_tool()
        if tool is None:
            return

        layer_field_map = {
            layer_id: info.source_field
            for layer_id, info in bundle.layer_info.items()
        }
        subgroup_label = ", ".join(bundle.subgroup_names)
        tool.set_multi_target(
            bundle.layers,
            layer_field_map,
            {},
            subgroup_label,
        )

        self._pick_bundle = bundle
        self._picking = True
        self._pick_status.setText(
            f"<b>Выбор на карте:</b> {subgroup_label} — кликните объект на карте"
        )
        self._pick_status.show()
        self._cancel_pick_btn.show()
        self._pick_map_btn.setText("Отмена выбора на карте")
        self._buttons.setEnabled(False)
        self._set_action_buttons_enabled(False)

        canvas = self._iface.mapCanvas()
        canvas.setFocus()
        canvas.setMapTool(tool)

    def _cancel_pick(self, silent: bool = False) -> None:
        if self._pick_tool and self._iface:
            canvas = self._iface.mapCanvas()
            if canvas.mapTool() is self._pick_tool:
                canvas.unsetMapTool(self._pick_tool)

        self._picking = False
        self._pick_bundle = None
        self._pick_status.hide()
        self._cancel_pick_btn.hide()
        self._pick_map_btn.setText("Указать на карте")
        self._buttons.setEnabled(True)
        self._set_action_buttons_enabled(True)

    def _on_feature_picked(
        self, value: str, layer_name: str, feat, layer
    ) -> None:
        if not self._pick_bundle:
            return

        info = self._pick_bundle.layer_info.get(layer.id())
        if info is None or info.task_column not in self._fields:
            return

        self._fields[info.task_column].setText(value)
        self._cancel_pick(silent=True)
        self.raise_()
        self.activateWindow()

    def _on_pick_failed(self, message: str) -> None:
        parent = self._iface.mainWindow() if self._iface else self
        QMessageBox.warning(parent, "Monitor DB Loader — задачи", message)

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._action_buttons:
            btn.setEnabled(enabled)

    def _set_busy(self, busy: bool) -> None:
        self._set_action_buttons_enabled(not busy)
        self._buttons.setEnabled(not busy)
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
        else:
            QApplication.restoreOverrideCursor()

    def _record_from_form(self) -> TaskRecord:
        data = self._record.as_dict()
        for field_name in self._form_fields:
            if field_name in self._fields:
                value = self._fields[field_name].text().strip() or None
                data[field_name] = value
        return TaskRecord.from_row(
            (
                data["key"],
                data["type"],
                data["photo_uuid"],
                data["photo_lens"],
                data["ogh_id"],
                data["oati_id"],
                data["earthwork_id"],
                data["localwork_id"],
                data["avr_mos_id"],
                data["sps"],
                data["kgs"],
                data["station_avr"],
            )
        )

    def _on_save(self) -> None:
        self._cancel_pick(silent=True)

        updated = self._record_from_form()

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

    def _send_task_snapshot(
        self,
        send_fn: Callable,
        success_message: str,
        skipped_message: str,
        error_prefix: str,
    ) -> None:
        self._cancel_pick(silent=True)
        updated = self._record_from_form()

        self._set_busy(True)
        try:
            update_task_record(self._conn, updated, self._store_cfg)
            result = send_fn(self._conn, updated, self._store_cfg)
        except ValueError as exc:
            QMessageBox.warning(self, "Monitor DB Loader — задачи", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Monitor DB Loader — задачи",
                f"{error_prefix}:\n{exc}",
            )
            return
        finally:
            self._set_busy(False)

        self._record = updated
        QMessageBox.information(
            self,
            "Monitor DB Loader — задачи",
            skipped_message if result == "skipped" else success_message,
        )

    def _on_send_to_field(self) -> None:
        self._send_task_snapshot(
            send_task_to_field,
            "Задача отправлена в поле.",
            "Задача уже была отправлена в поле.",
            "Не удалось отправить задачу в поле",
        )

    def _on_close_legal(self) -> None:
        self._send_task_snapshot(
            send_task_to_done_legal,
            "Задача закрыта как легальная.",
            "Задача уже была закрыта как легальная.",
            "Не удалось закрыть задачу как легальную",
        )

    def _on_close_illegal(self) -> None:
        self._send_task_snapshot(
            send_task_to_done_illegal,
            "Задача закрыта как нелегальная.",
            "Задача уже была закрыта как нелегальная.",
            "Не удалось закрыть задачу как нелегальную",
        )

    def _on_disruption_absent(self) -> None:
        self._send_task_snapshot(
            send_task_to_clear,
            "Задача отмечена: разрытие отсутствует.",
            "Задача уже была отмечена как «разрытие отсутствует».",
            "Не удалось сохранить задачу в tasks_clear",
        )

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
        subgroup_name: Optional[str] = None,
        group_name: Optional[str] = None,
        on_finished: Optional[Callable[[int], None]] = None,
    ) -> "TaskEditDialog":
        dlg = TaskEditDialog(
            record,
            conn,
            store_cfg,
            None,
            iface=iface,
            config=config,
            subgroup_name=subgroup_name,
            group_name=group_name,
        )
        if on_finished is not None:
            dlg.finished.connect(on_finished)
        register_modeless_dialog(iface, dlg)
        show_modeless_dialog(dlg)
        return dlg

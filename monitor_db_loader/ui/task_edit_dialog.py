# -*- coding: utf-8 -*-
"""Диалог редактирования строки crm.tasks."""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.crm_office_link_prefill import (
    can_add_office_point_from_task,
    office_task_link_prefill,
    office_task_link_prefill_from_record,
)
from ..core.crm_pick import LinkPickBundle, resolve_link_pick_bundle
from ..core.crm_task_store import (
    CRM_GROUP_ORDERS,
    STATION_COLUMNS,
    TASK_COLUMN_LABELS,
    TaskRecord,
    fetch_office_comment,
    fetch_task_audit,
    is_monitor_owned_task,
    send_task_to_done_illegal,
    send_task_to_done_legal,
    send_task_to_field,
    send_task_to_clear,
    task_form_field_groups,
    update_task_record,
)
from ..core.crm_ui_constants import (
    LEGAL_STATION_FIELDS,
    TASK_SOURCE_LABELS,
    ai_photo_uuid_from_attributes,
    format_field_observed,
    get_legal_link_fields,
    is_ai_photo_context,
)
from ..core.db import DatabaseConnection
from ..core.qt_compat import TEXT_FORMAT_RICH, register_modeless_dialog, show_modeless_dialog
from .crm_theme import apply_crm_theme, style_button, style_field_observed_label
from .feature_pick_tool import FeaturePickMapTool
from .photo_view_dialog import PhotoViewDialog
from .field_materials_dialog import FieldMaterialsDialog

StatusAction = str

STATUS_CONFIRM_MESSAGES = {
    "field": "Отправить задачу в поле?",
    "legal": "Закрыть задачу как легальную?",
    "illegal": "Закрыть задачу как нелегальную?",
    "clear": "Отметить задачу: разрытие отсутствует?",
}


@dataclass
class LegalValidation:
    is_valid: bool
    has_link: bool
    has_station: bool
    message: Optional[str] = None


def _is_filled(value: str) -> bool:
    return bool(value.strip())


def _field_value(
    fields: Dict[str, QLineEdit],
    record: TaskRecord,
    field_name: str,
) -> str:
    text = fields[field_name].text().strip() if field_name in fields else ""
    if text:
        return text
    return str(getattr(record, field_name, "") or "").strip()


def _can_close_as_illegal(record: TaskRecord) -> bool:
    return record.field_observed is not False


ILLEGAL_CLOSE_REQUIRES_FIELD_SURVEY = "Не проведено полевое обследование."


def _illegal_close_block_message(record: TaskRecord) -> Optional[str]:
    if record.field_observed is False:
        return ILLEGAL_CLOSE_REQUIRES_FIELD_SURVEY
    return None


def _get_legal_validation(
    fields: Dict[str, QLineEdit],
    legal_link_fields: List[str],
    record: TaskRecord,
) -> LegalValidation:
    has_link = (
        not legal_link_fields
        or any(_is_filled(_field_value(fields, record, f)) for f in legal_link_fields)
    )
    has_station = any(
        _is_filled(_field_value(fields, record, f)) for f in LEGAL_STATION_FIELDS
    )

    if legal_link_fields and not has_link:
        return LegalValidation(
            is_valid=False,
            has_link=False,
            has_station=has_station,
            message="Заполните хотя бы одно поле в группе «Сопоставление» (кроме третьего).",
        )
    if not has_station:
        return LegalValidation(
            is_valid=False,
            has_link=True,
            has_station=False,
            message="Заполните СПС или АВР в группе «Данные из Станции».",
        )
    return LegalValidation(is_valid=True, has_link=True, has_station=True)


TASK_NOT_IN_DISTRICT = "Задача не в выбранном районе"


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
        task_source: str = "active",
        user_login: str = "",
        feature_attributes: Optional[Dict[str, Any]] = None,
        office_working: bool = False,
        on_start_place_office_point: Optional[Callable[[Optional[Dict[str, str]]], None]] = None,
        district_name: str = "",
        active_task_keys: Optional[Set[str]] = None,
    ):
        super().__init__(parent)
        self._record = record
        self._conn = conn
        self._store_cfg = store_cfg
        self._user_login = user_login or ""
        self._district_name = (district_name or "").strip()
        self._active_task_keys = set(active_task_keys or [])
        self._iface = iface
        self._config = config
        self._subgroup_name = subgroup_name
        self._group_name = group_name or record.type
        self._task_source = task_source
        self._feature_attributes = dict(feature_attributes or {})
        self._office_working = office_working
        self._on_start_place_office_point = on_start_place_office_point
        user_created = fetch_task_audit(conn, store_cfg, record.key)
        self._is_monitor_owned = is_monitor_owned_task(user_created)
        self._readonly_fields, self._link_fields = task_form_field_groups(
            self._group_name,
            subgroup_name,
            store_cfg,
            record,
            monitor_owned=self._is_monitor_owned,
        )
        self._form_fields = self._readonly_fields + self._link_fields + list(STATION_COLUMNS)
        self._fields: Dict[str, QLineEdit] = {}
        self._pick_tool: Optional[FeaturePickMapTool] = None
        self._pick_bundle: Optional[LinkPickBundle] = None
        self._picking = False
        self._pending_status: Optional[StatusAction] = None
        self._show_legal_requirements = False

        self._is_readonly = task_source != "active"
        self._requires_legal_link = self._group_name != CRM_GROUP_ORDERS
        self._legal_link_fields = (
            get_legal_link_fields(self._link_fields)
            if self._requires_legal_link
            else []
        )

        title = "Просмотр задачи" if self._is_readonly else "Исполнить задачу"
        self.setWindowTitle(title)
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(540, 620)
        apply_crm_theme(self)

        outer = QVBoxLayout(self)

        title_label = QLabel(f"<b>{title}</b>")
        title_label.setObjectName("crmTitle")
        title_label.setTextFormat(TEXT_FORMAT_RICH)
        outer.addWidget(title_label)

        source_label = QLabel(
            f"Источник: {TASK_SOURCE_LABELS.get(task_source, task_source)}"
        )
        source_label.setObjectName("crmMuted")
        outer.addWidget(source_label)

        key_label = QLabel(f"Ключ: {record.key}")
        key_label.setObjectName("crmMuted")
        outer.addWidget(key_label)

        if self._is_monitor_owned and record.type == CRM_GROUP_ORDERS:
            etl_hint = QLabel(
                "Задача создана ETL (MONITOR): идентификаторы заказа нельзя менять."
            )
            etl_hint.setObjectName("crmMuted")
            etl_hint.setWordWrap(True)
            outer.addWidget(etl_hint)

        self._observed_label = QLabel(
            f"Обследовано в поле: {format_field_observed(record.field_observed)}"
        )
        style_field_observed_label(self._observed_label, record.field_observed)
        outer.addWidget(self._observed_label)

        self._office_comment_group = QGroupBox("Комментарий камерального анализа")
        self._office_comment_group.setObjectName("crmFormSection")
        office_comment_layout = QVBoxLayout(self._office_comment_group)
        self._office_comment_view = QPlainTextEdit()
        self._office_comment_view.setReadOnly(True)
        self._office_comment_view.setObjectName("crmOfficeCommentView")
        office_comment_layout.addWidget(self._office_comment_view)
        self._office_comment_group.hide()
        outer.addWidget(self._office_comment_group)
        self._setup_office_comment_view()

        self._message_label = QLabel("")
        self._message_label.setWordWrap(True)
        outer.addWidget(self._message_label)

        self._pick_status = QLabel("")
        self._pick_status.setObjectName("crmPickBanner")
        self._pick_status.setWordWrap(True)
        self._pick_status.hide()
        outer.addWidget(self._pick_status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        form_host = QWidget()
        form_layout = QVBoxLayout(form_host)

        self._source_section = QGroupBox("Источник")
        self._source_section.setObjectName("crmFormSection")
        source_form = QFormLayout(self._source_section)
        for field_name in self._readonly_fields:
            raw = getattr(record, field_name, "")
            if isinstance(raw, bool):
                text = "да" if raw else "нет"
            else:
                text = str(raw or "")
            edit = QLineEdit(text)
            edit.setReadOnly(True)
            self._fields[field_name] = edit
            source_form.addRow(TASK_COLUMN_LABELS.get(field_name, field_name), edit)
        form_layout.addWidget(self._source_section)

        self._link_section = QGroupBox("Сопоставление")
        self._link_section.setObjectName("crmFormSection")
        self._link_form = QFormLayout(self._link_section)
        self._link_hint = QLabel("")
        self._link_hint.setObjectName("crmMuted")
        self._link_hint.setWordWrap(True)
        if not self._is_readonly and self._requires_legal_link:
            labels = [TASK_COLUMN_LABELS.get(f, f) for f in self._legal_link_fields]
            self._link_hint.setText(
                f"Для «Закрыть легальное» — одно из: {', '.join(labels)}"
            )
        self._link_form.addRow(self._link_hint)
        for field_name in self._link_fields:
            edit = QLineEdit(getattr(record, field_name) or "")
            if self._is_readonly:
                edit.setReadOnly(True)
            edit.textChanged.connect(self._update_legal_highlights)
            self._fields[field_name] = edit
            label = TASK_COLUMN_LABELS.get(field_name, field_name)
            if (
                not self._is_readonly
                and self._requires_legal_link
                and field_name in self._legal_link_fields
            ):
                label += ' <span style="color:#c62828">*</span>'
            row_label = QLabel(label)
            row_label.setTextFormat(TEXT_FORMAT_RICH)
            self._link_form.addRow(row_label, edit)
        if self._link_fields:
            form_layout.addWidget(self._link_section)
        else:
            self._link_section.hide()

        self._station_section = QGroupBox("Данные из Станции")
        self._station_section.setObjectName("crmFormSection")
        station_form = QFormLayout(self._station_section)
        station_labels = [TASK_COLUMN_LABELS.get(f, f) for f in LEGAL_STATION_FIELDS]
        self._station_hint = QLabel(
            f"Для «Закрыть легальное» — одно из: {' или '.join(station_labels)}"
        )
        self._station_hint.setObjectName("crmMuted")
        self._station_hint.setWordWrap(True)
        if not self._is_readonly:
            station_form.addRow(self._station_hint)
        for field_name in STATION_COLUMNS:
            edit = QLineEdit(getattr(record, field_name) or "")
            if self._is_readonly:
                edit.setReadOnly(True)
            edit.textChanged.connect(self._update_legal_highlights)
            self._fields[field_name] = edit
            label = TASK_COLUMN_LABELS[field_name]
            if not self._is_readonly and field_name in LEGAL_STATION_FIELDS:
                label += ' <span style="color:#c62828">*</span>'
            row_label = QLabel(label)
            row_label.setTextFormat(TEXT_FORMAT_RICH)
            station_form.addRow(row_label, edit)
        form_layout.addWidget(self._station_section)

        scroll.setWidget(form_host)
        outer.addWidget(scroll, stretch=1)

        self._pick_map_btn = QPushButton("Указать на карте")
        self._pick_map_btn.setEnabled(bool(self._link_fields) and not self._is_readonly)
        self._pick_map_btn.clicked.connect(self._toggle_link_pick)
        outer.addWidget(self._pick_map_btn)

        manage_group = QGroupBox("Управление задачей")
        manage_group.setObjectName("crmFormSection")
        manage_row = QHBoxLayout(manage_group)
        self._view_photo_btn = QPushButton("Посмотреть фото")
        self._view_photo_btn.setVisible(
            is_ai_photo_context(self._subgroup_name or "")
        )
        self._view_photo_btn.clicked.connect(self._on_view_photo)
        manage_row.addWidget(self._view_photo_btn)
        self._view_field_materials_btn = QPushButton("Просмотр полевых материалов")
        self._view_field_materials_btn.setVisible(
            self._record.field_observed is True
        )
        self._view_field_materials_btn.clicked.connect(self._on_view_field_materials)
        manage_row.addWidget(self._view_field_materials_btn)
        self._add_office_point_btn = QPushButton("Добавить разрытие на карте")
        self._add_office_point_btn.setVisible(
            can_add_office_point_from_task(
                office_working=self._office_working,
                task_source=self._task_source,
                subgroup_name=self._subgroup_name or "",
                record=self._record,
            )
            and self._on_start_place_office_point is not None
        )
        self._add_office_point_btn.clicked.connect(self._on_add_office_point)
        manage_row.addWidget(self._add_office_point_btn)
        self._save_btn = QPushButton("Сохранить")
        self._save_btn.clicked.connect(self._on_save)
        self._save_btn.setVisible(not self._is_readonly)
        manage_row.addWidget(self._save_btn)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.reject)
        manage_row.addWidget(close_btn)
        manage_row.addStretch()
        outer.addWidget(manage_group)

        self._status_group = QGroupBox("Изменить статус задачи")
        self._status_group.setObjectName("crmFormSection")
        status_layout = QVBoxLayout(self._status_group)

        self._legal_requirements = QLabel(
            '<span style="color:#c62828">*</span> Для «Закрыть легальное»: '
            + (
                "одно поле «Сопоставление» (кроме третьего) и СПС или АВР в «Данные из Станции»."
                if self._requires_legal_link
                else "СПС или АВР в «Данные из Станции»."
            )
        )
        self._legal_requirements.setObjectName("crmLegalRequirements")
        self._legal_requirements.setTextFormat(TEXT_FORMAT_RICH)
        self._legal_requirements.setWordWrap(True)
        if not self._is_readonly:
            status_layout.addWidget(self._legal_requirements)

        self._status_buttons_widget = QWidget()
        status_btn_row = QHBoxLayout(self._status_buttons_widget)
        status_btn_row.setContentsMargins(0, 0, 0, 0)

        self._btn_field = QPushButton("Отправить в поле")
        style_button(self._btn_field, "crmBtnStatusField")
        self._btn_field.clicked.connect(lambda: self._request_status("field"))
        status_btn_row.addWidget(self._btn_field)

        self._btn_legal = QPushButton("Закрыть легальное")
        style_button(self._btn_legal, "crmBtnStatusLegal")
        self._btn_legal.clicked.connect(lambda: self._request_status("legal"))
        status_btn_row.addWidget(self._btn_legal)

        self._btn_illegal = QPushButton("Закрыть нелегальное")
        style_button(self._btn_illegal, "crmBtnStatusIllegal")
        self._btn_illegal.clicked.connect(lambda: self._request_status("illegal"))
        status_btn_row.addWidget(self._btn_illegal)

        self._illegal_hint = QLabel(ILLEGAL_CLOSE_REQUIRES_FIELD_SURVEY)
        self._illegal_hint.setObjectName("crmError")
        self._illegal_hint.setWordWrap(True)
        self._illegal_hint.hide()
        status_layout.addWidget(self._illegal_hint)

        self._btn_clear = QPushButton("Разрытие отсутствует")
        style_button(self._btn_clear, "crmBtnStatusClear")
        self._btn_clear.clicked.connect(lambda: self._request_status("clear"))
        status_btn_row.addWidget(self._btn_clear)
        status_btn_row.addStretch()
        status_layout.addWidget(self._status_buttons_widget)

        self._confirm_frame = QFrame()
        self._confirm_frame.setObjectName("crmStatusConfirm")
        confirm_layout = QVBoxLayout(self._confirm_frame)
        self._confirm_label = QLabel("")
        self._confirm_label.setWordWrap(True)
        confirm_layout.addWidget(self._confirm_label)
        self._confirm_comment_label = QLabel("Комментарий (необязательно)")
        self._confirm_comment_label.setObjectName("crmMuted")
        self._confirm_comment_label.hide()
        confirm_layout.addWidget(self._confirm_comment_label)
        self._confirm_comment_edit = QPlainTextEdit()
        self._confirm_comment_edit.setPlaceholderText("Комментарий для полевого исполнителя")
        self._confirm_comment_edit.setMaximumHeight(100)
        self._confirm_comment_edit.hide()
        confirm_layout.addWidget(self._confirm_comment_edit)
        confirm_btns = QHBoxLayout()
        confirm_btn = QPushButton("Подтвердить")
        style_button(confirm_btn, "crmBtnPrimary")
        confirm_btn.clicked.connect(self._on_confirm_status)
        confirm_btns.addWidget(confirm_btn)
        cancel_confirm = QPushButton("Отмена")
        cancel_confirm.clicked.connect(self._cancel_confirm)
        confirm_btns.addWidget(cancel_confirm)
        confirm_btns.addStretch()
        confirm_layout.addLayout(confirm_btns)
        self._confirm_frame.hide()
        status_layout.addWidget(self._confirm_frame)

        self._configure_status_visibility()
        outer.addWidget(self._status_group)

        self._update_legal_highlights()

    def _resolve_office_comment_text(self) -> Optional[str]:
        raw = self._feature_attributes.get("_office_comment")
        if raw is not None:
            text = str(raw).strip()
            if text:
                return text
        return fetch_office_comment(self._conn, self._store_cfg, self._record.key)

    def _setup_office_comment_view(self) -> None:
        if self._task_source != "field":
            return
        text = self._resolve_office_comment_text()
        if not text:
            return
        self._office_comment_view.setPlainText(text)
        self._office_comment_group.show()

    def _on_view_photo(self) -> None:
        uuid = (self._record.photo_uuid or "").strip()
        if not uuid:
            uuid = ai_photo_uuid_from_attributes(self._feature_attributes) or ""
        if not uuid:
            self._set_message("UUID фотографии не найден", error=True)
            return
        if self._config is None:
            self._set_message(
                "Просмотр фото недоступен: нет конфигурации плагина.",
                error=True,
            )
            return
        PhotoViewDialog.open(uuid, self._conn, self._config, self)

    def _on_view_field_materials(self) -> None:
        task_key = (self._record.key or "").strip()
        if not task_key:
            self._set_message("Ключ задачи не найден", error=True)
            return
        if self._config is None:
            self._set_message(
                "Просмотр материалов недоступен: нет конфигурации плагина.",
                error=True,
            )
            return
        FieldMaterialsDialog.open(task_key, self._conn, self._config, self)

    def _on_add_office_point(self) -> None:
        if not self._on_start_place_office_point:
            return
        prefill = office_task_link_prefill_from_record(self._record)
        if not prefill:
            prefill = office_task_link_prefill(
                self._subgroup_name or "",
                self._feature_attributes,
            )
        self._on_start_place_office_point(prefill)
        self.accept()

    def _configure_status_visibility(self) -> None:
        in_active_session = (
            not self._active_task_keys or self._record.key in self._active_task_keys
        )
        can_field = (
            self._task_source == "active"
            and bool(self._district_name)
            and in_active_session
        )
        can_legal = self._task_source in ("active", "field")
        can_illegal = (
            self._task_source in ("active", "field")
            and _can_close_as_illegal(self._record)
        )
        can_clear = self._task_source in ("active", "field")
        has_actions = can_field or can_legal or can_illegal or can_clear

        self._status_group.setVisible(has_actions and not self._is_readonly)
        self._btn_field.setVisible(can_field)
        self._btn_legal.setVisible(can_legal)
        self._btn_illegal.setVisible(self._task_source in ("active", "field"))
        self._btn_illegal.setEnabled(can_illegal)
        show_illegal_hint = (
            self._task_source in ("active", "field")
            and self._record.field_observed is False
        )
        self._illegal_hint.setVisible(show_illegal_hint and not self._is_readonly)
        if show_illegal_hint:
            self._btn_illegal.setToolTip(ILLEGAL_CLOSE_REQUIRES_FIELD_SURVEY)
        else:
            self._btn_illegal.setToolTip("")
        self._btn_clear.setVisible(can_clear)
        self._legal_requirements.setVisible(can_legal and not self._is_readonly)
        self._link_hint.setVisible(
            can_legal and not self._is_readonly and bool(self._legal_link_fields)
        )
        self._station_hint.setVisible(can_legal and not self._is_readonly)

    def _legal_validation(self) -> LegalValidation:
        return _get_legal_validation(
            self._fields, self._legal_link_fields, self._record
        )

    def _update_legal_highlights(self) -> None:
        validation = self._legal_validation()
        if validation.is_valid:
            self._show_legal_requirements = False

        link_missing = (
            self._show_legal_requirements
            and self._requires_legal_link
            and not validation.has_link
        )
        station_missing = (
            self._show_legal_requirements and not validation.has_station
        )

        self._link_section.setObjectName(
            "crmFormSectionMissing" if link_missing else "crmFormSection"
        )
        self._station_section.setObjectName(
            "crmFormSectionMissing" if station_missing else "crmFormSection"
        )

        for field_name in self._link_fields:
            edit = self._fields.get(field_name)
            if edit is None:
                continue
            missing = (
                field_name in self._legal_link_fields
                and link_missing
                and not _is_filled(_field_value(self._fields, self._record, field_name))
            )
            edit.setObjectName("crmFormRowMissing" if missing else "")

        for field_name in STATION_COLUMNS:
            edit = self._fields.get(field_name)
            if edit is None:
                continue
            missing = (
                field_name in LEGAL_STATION_FIELDS
                and station_missing
                and not _is_filled(_field_value(self._fields, self._record, field_name))
            )
            edit.setObjectName("crmFormRowMissing" if missing else "")

        self.style().unpolish(self)
        self.style().polish(self)

    def _set_message(self, text: str, *, error: bool = False) -> None:
        self._message_label.setText(text)
        self._message_label.setObjectName("crmError" if error else "crmSuccess")

    def _request_status(self, action: StatusAction) -> None:
        if action == "legal":
            validation = self._legal_validation()
            if not validation.is_valid:
                self._show_legal_requirements = True
                self._update_legal_highlights()
                self._set_message(validation.message or "", error=True)
                return
        if action == "illegal":
            block_msg = _illegal_close_block_message(self._record)
            if block_msg:
                self._set_message(block_msg, error=True)
                return
        self._show_legal_requirements = False
        self._update_legal_highlights()
        self._pending_status = action
        self._confirm_label.setText(STATUS_CONFIRM_MESSAGES[action])
        show_comment = action == "field"
        self._confirm_comment_label.setVisible(show_comment)
        self._confirm_comment_edit.setVisible(show_comment)
        if show_comment:
            self._confirm_comment_edit.clear()
        self._confirm_frame.show()
        self._status_buttons_widget.hide()

    def _cancel_confirm(self) -> None:
        self._pending_status = None
        self._confirm_comment_edit.clear()
        self._confirm_comment_label.hide()
        self._confirm_comment_edit.hide()
        self._confirm_frame.hide()
        self._status_buttons_widget.show()

    def _on_confirm_status(self) -> None:
        if not self._pending_status:
            return
        action = self._pending_status
        office_comment: Optional[str] = None
        if action == "field":
            office_comment = self._confirm_comment_edit.toPlainText().strip() or None
        self._cancel_confirm()
        self._handle_status_action(action, office_comment=office_comment)

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
                self, "Мониторинг разрытий — задачи",
                "Выбор с карты недоступен: нет доступа к карте QGIS.",
            )
            return
        bundle = resolve_link_pick_bundle(self._config, self._link_fields)
        if bundle is None or not bundle.layers:
            missing = ", ".join(bundle.missing) if bundle and bundle.missing else "—"
            QMessageBox.warning(
                self, "Мониторинг разрытий — задачи",
                "Слои для сопоставления не найдены в проекте.\n\n"
                f"Сначала выполните загрузку данных из БД.\nНе найдено: {missing}",
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
        tool.set_multi_target(bundle.layers, layer_field_map, {}, subgroup_label)
        self._pick_bundle = bundle
        self._picking = True
        self._pick_status.setText(
            f"<b>Режим выбора на карте</b> — кликните объект ({subgroup_label})"
        )
        self._pick_status.show()
        self._pick_map_btn.setText("Отмена выбора на карте")
        self._set_form_enabled(False)
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
        self._pick_map_btn.setText("Указать на карте")
        self._set_form_enabled(True)

    def _set_form_enabled(self, enabled: bool) -> None:
        self._save_btn.setEnabled(enabled)
        for btn in (self._btn_field, self._btn_legal, self._btn_illegal, self._btn_clear):
            btn.setEnabled(enabled)

    def _on_feature_picked(self, value: str, layer_name: str, feat, layer) -> None:
        if not self._pick_bundle:
            return
        info = self._pick_bundle.layer_info.get(layer.id())
        if info is None or info.task_column not in self._fields:
            return
        self._fields[info.task_column].setText(value)
        self._cancel_pick(silent=True)
        self._set_message(f"Выбрано: {info.task_column} = {value}")
        self._update_legal_highlights()
        self.raise_()
        self.activateWindow()

    def _on_pick_failed(self, message: str) -> None:
        parent = self._iface.mainWindow() if self._iface else self
        QMessageBox.warning(parent, "Мониторинг разрытий — задачи", message)

    def _set_busy(self, busy: bool) -> None:
        self._set_form_enabled(not busy)
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
                data.get("field_observed"),
            )
        )

    def _on_save(self) -> None:
        self._cancel_pick(silent=True)
        updated = self._record_from_form()
        try:
            update_task_record(
                self._conn, updated, self._store_cfg, self._user_login
            )
        except ValueError as exc:
            self._set_message(str(exc), error=True)
            return
        except Exception as exc:
            QMessageBox.critical(
                self, "Мониторинг разрытий — задачи",
                f"Не удалось сохранить задачу:\n{exc}",
            )
            return
        self._record = updated
        self._set_message("Сохранено")
        self._update_legal_highlights()

    def _handle_status_action(
        self,
        action: StatusAction,
        *,
        office_comment: Optional[str] = None,
    ) -> None:
        if self._is_readonly:
            return
        if action == "illegal":
            block_msg = _illegal_close_block_message(self._record)
            if block_msg:
                self._set_message(block_msg, error=True)
                return
        if action == "legal":
            validation = self._legal_validation()
            if not validation.is_valid:
                self._show_legal_requirements = True
                self._update_legal_highlights()
                self._set_message(validation.message or "", error=True)
                return

        self._cancel_pick(silent=True)
        updated = self._record_from_form()
        if action == "field":
            if not self._district_name:
                self._set_message("Не выбран район сессии", error=True)
                return
            if self._active_task_keys and updated.key not in self._active_task_keys:
                self._set_message(TASK_NOT_IN_DISTRICT, error=True)
                return
        district_name = self._district_name
        send_fn = {
            "field": lambda c, r, s, l: send_task_to_field(
                c,
                r,
                s,
                l,
                office_comment=office_comment,
                rayon=district_name,
            ),
            "legal": send_task_to_done_legal,
            "illegal": send_task_to_done_illegal,
            "clear": send_task_to_clear,
        }[action]

        self._set_busy(True)
        try:
            update_task_record(
                self._conn, updated, self._store_cfg, self._user_login
            )
            result = send_fn(self._conn, updated, self._store_cfg, self._user_login)
        except ValueError as exc:
            self._set_message(str(exc), error=True)
            return
        except Exception as exc:
            QMessageBox.critical(
                self, "Мониторинг разрытий — задачи",
                f"Не удалось изменить статус:\n{exc}",
            )
            return
        finally:
            self._set_busy(False)

        self._record = updated
        if action == "clear" and result == "skipped":
            self._set_message("Задача уже была отмечена как «разрытие отсутствует».")
        elif action == "clear":
            self._set_message("Задача отмечена: разрытие отсутствует.")
        else:
            self._set_message(f"Статус: {result}")
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
        subgroup_name: Optional[str] = None,
        group_name: Optional[str] = None,
        task_source: str = "active",
        on_finished: Optional[Callable[[int], None]] = None,
        user_login: str = "",
        feature_attributes: Optional[Dict[str, Any]] = None,
        office_working: bool = False,
        on_start_place_office_point: Optional[Callable[[Optional[Dict[str, str]]], None]] = None,
        district_name: str = "",
        active_task_keys: Optional[Set[str]] = None,
    ) -> "TaskEditDialog":
        win_parent = parent or (iface.mainWindow() if iface else None)
        dlg = TaskEditDialog(
            record,
            conn,
            store_cfg,
            win_parent,
            iface=iface,
            config=config,
            subgroup_name=subgroup_name,
            group_name=group_name,
            task_source=task_source,
            user_login=user_login,
            feature_attributes=feature_attributes,
            office_working=office_working,
            on_start_place_office_point=on_start_place_office_point,
            district_name=district_name,
            active_task_keys=active_task_keys,
        )
        if on_finished is not None:
            dlg.finished.connect(on_finished)
        register_modeless_dialog(iface, dlg)
        show_modeless_dialog(dlg, win_parent)
        return dlg

# -*- coding: utf-8 -*-
"""Диалог списка задач CRM по району."""

import time
from typing import List, Optional, Tuple

from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsHighlight
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.crm_area_map import TasksAreaMapController
from ..core.crm_office_points_map import get_office_points_map_controller
from ..core.config import crm_task_store, crm_tasks
from ..core.crm_filter_by_area import (
    count_task_result_features,
    empty_task_result_shell,
    filter_task_result_by_area,
    geometry_inside_area,
)
from ..core.crm_task_map_layers import (
    collect_task_result_for_source,
    create_task_layers_in_qgis,
)
from ..core.crm_task_store import (
    enrich_task_result_field_observed,
    ensure_all_snapshot_tables,
    ensure_crm_session_cache,
    fetch_task_by_key,
    fetch_task_for_feature,
    filter_sent_tasks_from_result,
    persist_task_result,
)
from ..core.crm_tasks import (
    TaskFeature,
    TaskResult,
    TaskSubgroup,
    connect_db,
    copy_task_result,
)
from ..core.auth import UserSession, allowed_task_sources
from ..core.crm_tasks_area import (
    collect_area_orders_for_picker,
    complete_area_analise,
    complete_area_survey,
    invalidate_area_geometries_cache,
    pause_area_analise,
    preload_area_geometries,
    release_area_from_survey,
    send_area_to_survey,
    start_area_analise,
)
from ..core.crm_ui_constants import (
    AREA_STATUS_LABELS,
    TASK_SOURCES,
    TASK_SOURCE_LABELS,
    area_status_from_source,
    format_analise_workflow_status,
    format_area_hectares,
    format_area_order_label,
    format_field_observed,
    format_task_table_cell,
    is_area_source,
    resolve_task_table_columns,
    task_execute_button_label,
)
from ..core.db import DatabaseConnection
from ..core.district_utils import DistrictBoundary
from ..core.layer_utils import refresh_map_canvas
from ..core.log_util import log_info, log_timing, log_warning
from ..core.task_dxf_export import export_tasks_to_dxf, export_tasks_to_shp
from ..core.qt_compat import TEXT_FORMAT_RICH, register_modeless_dialog, show_modeless_dialog
from .crm_source_tabs import TaskSourceTabs
from .crm_theme import apply_crm_theme, style_button
from .area_order_picker_dialog import AreaOrderPickerDialog

TreeRole = Tuple[str, ...]


def _format_sent_at(sent_at: Optional[str]) -> str:
    if not sent_at:
        return ""
    return format_task_table_cell(sent_at, "datetime")


class TaskDialog(QDialog):
    def __init__(
        self,
        result: TaskResult,
        iface,
        parent=None,
        *,
        config: Optional[dict] = None,
        db_conn: Optional[DatabaseConnection] = None,
        district: Optional[DistrictBoundary] = None,
        apply_date_filter: bool = True,
        on_change_district=None,
        user_session: Optional[UserSession] = None,
    ):
        super().__init__(parent)
        self._result = result
        self._active_result = copy_task_result(result)
        self._iface = iface
        self._config = config
        self._db_conn = db_conn
        self._own_db_conn = db_conn is not None
        self._user_session = user_session
        self._allowed_sources = (
            allowed_task_sources(user_session.role)
            if user_session
            else list(TASK_SOURCES)
        )
        self._store_cfg = crm_task_store(config) if config else {}
        self._district = district
        self._apply_date_filter = apply_date_filter
        self._on_change_district = on_change_district
        self._task_source = result.task_source or (
            self._allowed_sources[0] if self._allowed_sources else "active"
        )
        self._highlight: Optional[QgsHighlight] = None
        self._current_group_name = ""
        self._current_subgroup: Optional[TaskSubgroup] = None
        self._selected_task_feat: Optional[TaskFeature] = None
        self._selected_row: Optional[int] = None
        self._table_columns: List = []
        self._busy = False
        self._office_area_order: Optional[TaskFeature] = None
        self._office_full_result: Optional[TaskResult] = None
        self._office_filtered_result: Optional[TaskResult] = None
        self._office_filtered_key: Optional[str] = None
        self._office_orders_on_map = False
        self._office_picker: Optional[AreaOrderPickerDialog] = None
        self._place_point_mode = False
        self._place_point_busy = False
        self._pending_link_prefill = None
        self._place_point_tool = None
        district_name = (
            district.name if district else result.district_name
        )
        self._area_map = TasksAreaMapController(iface, district_name)
        self._office_points_map = get_office_points_map_controller(iface)

        self.setWindowTitle(f"Задачи — {result.district_name}")
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(1000, 640)
        self.setMinimumSize(760, 480)
        apply_crm_theme(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._district_label = QLabel("")
        self._district_label.setTextFormat(TEXT_FORMAT_RICH)
        header.addWidget(self._district_label, stretch=1)

        self._change_district_btn = QPushButton("Сменить район")
        self._change_district_btn.clicked.connect(self._on_change_district)
        header.addWidget(self._change_district_btn)

        self._refresh_btn = QPushButton("Обновить")
        style_button(self._refresh_btn, "crmBtnPrimary")
        self._refresh_btn.clicked.connect(self._on_refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._meta_label = QLabel("")
        self._meta_label.setObjectName("crmMuted")
        layout.addWidget(self._meta_label)

        self._office_context_label = QLabel("")
        self._office_context_label.setObjectName("crmOfficeContext")
        self._office_context_label.hide()
        layout.addWidget(self._office_context_label)

        self._source_tabs = TaskSourceTabs(allowed_sources=self._allowed_sources)
        self._source_tabs.set_value(self._task_source)
        self._source_tabs.sourceChanged.connect(self._on_source_changed)
        self._source_tabs.pauseOrderClicked.connect(self._on_pause_office_order)
        self._source_tabs.completeOrderClicked.connect(self._on_complete_office_order)
        self._source_tabs.ordersToggleClicked.connect(self._on_office_orders_toggle)
        self._source_tabs.selectOrderClicked.connect(self._open_office_order_picker)
        self._source_tabs.syncDistrictClicked.connect(self._on_sync_district_tasks)
        self._source_tabs.placePointClicked.connect(self._on_toggle_place_point)
        layout.addWidget(self._source_tabs)

        expand = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setSizePolicy(expand)
        splitter.setMinimumHeight(240)

        self.tree = QTreeWidget()
        self.tree.setSizePolicy(expand)
        self.tree.setMinimumWidth(200)
        self.tree.setHeaderLabel("Группы")
        self.tree.header().setStretchLastSection(True)
        self.tree.currentItemChanged.connect(self._on_tree_selection_changed)
        splitter.addWidget(self.tree)

        self.table = QTableWidget()
        self.table.setSizePolicy(expand)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.cellClicked.connect(self._on_row_clicked)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, stretch=1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("crmMuted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.execute_btn = QPushButton(task_execute_button_label(self._task_source))
        style_button(self.execute_btn, "crmBtnPrimary")
        self.execute_btn.setEnabled(False)
        self.execute_btn.clicked.connect(self._on_execute_task)
        footer.addWidget(self.execute_btn)

        self._area_send_btn = QPushButton("Отправить на полевое обследование")
        style_button(self._area_send_btn, "crmBtnPrimary")
        self._area_send_btn.hide()
        self._area_send_btn.clicked.connect(self._on_send_area_to_survey)
        footer.addWidget(self._area_send_btn)

        self._area_release_btn = QPushButton("Снять с обследования")
        self._area_release_btn.hide()
        self._area_release_btn.clicked.connect(self._on_release_area_from_survey)
        footer.addWidget(self._area_release_btn)

        self._area_complete_btn = QPushButton("Завершить обследование")
        style_button(self._area_complete_btn, "crmBtnPrimary")
        self._area_complete_btn.hide()
        self._area_complete_btn.clicked.connect(self._on_complete_area_survey)
        footer.addWidget(self._area_complete_btn)

        self._create_layers_btn = QPushButton("Создать слои задач в QGIS")
        self._create_layers_btn.clicked.connect(self._on_create_task_layers)
        footer.addWidget(self._create_layers_btn)

        self.export_dxf_btn = QPushButton("Экспорт задач в DXF")
        self.export_dxf_btn.clicked.connect(self._on_export_dxf)
        footer.addWidget(self.export_dxf_btn)

        self.export_shp_btn = QPushButton("Экспорт задач в SHP")
        self.export_shp_btn.clicked.connect(self._on_export_shp)
        footer.addWidget(self.export_shp_btn)

        footer.addStretch(1)

        self._close_btn = QPushButton("Закрыть")
        self._close_btn.clicked.connect(self.close)
        footer.addWidget(self._close_btn)
        layout.addLayout(footer)

        if self._is_office_user():
            self._create_layers_btn.hide()

        self._update_header()
        if (
            self._task_source != "active"
            and self._db_conn
            and self._district
        ):
            QTimer.singleShot(0, lambda: self._reload_for_source(self._task_source))
        elif self._db_conn and self._store_cfg and self._task_source == "active":
            self._apply_snapshot_filter()
            if self._is_office_user():
                QTimer.singleShot(0, self._open_office_order_picker)
        else:
            if self._db_conn and self._store_cfg and not self._is_area():
                self._enrich_field_observed()
            self._populate_tree()
            self._update_status()
            self._update_action_buttons()

        first_subgroup = self._first_subgroup_item()
        if first_subgroup:
            self.tree.setCurrentItem(first_subgroup)
        QTimer.singleShot(0, self._deferred_map_refresh)

    def _deferred_map_refresh(self) -> None:
        self._refresh_area_map()
        self._update_header()
        self._update_office_actions()

    def _is_office_user(self) -> bool:
        return bool(self._user_session and self._user_session.role == "office")

    def _office_working(self) -> bool:
        return self._is_office_user() and self._office_area_order is not None

    def _office_awaiting_order(self) -> bool:
        return (
            self._is_office_user()
            and self._task_source == "active"
            and self._office_area_order is None
        )

    def _office_order_key(self, order: Optional[TaskFeature]) -> Optional[str]:
        if order is None:
            return None
        key = order.task_key or order.attributes.get("key")
        return str(key).strip() if key else None

    def _invalidate_office_filter_cache(self) -> None:
        self._office_filtered_result = None
        self._office_filtered_key = None

    def _rebuild_office_filter_cache(self) -> None:
        if not self._office_working() or not self._office_full_result:
            self._invalidate_office_filter_cache()
            return
        key = self._office_order_key(self._office_area_order)
        if not key:
            self._invalidate_office_filter_cache()
            return
        if self._office_filtered_key == key and self._office_filtered_result:
            return
        t0 = time.perf_counter()
        self._office_filtered_result = filter_task_result_by_area(
            self._office_full_result, self._office_area_order
        )
        self._office_filtered_key = key
        log_timing("office_filter_cache_rebuild", (time.perf_counter() - t0) * 1000)

    def _office_remaining_count(self) -> int:
        if not self._office_working() or not self._office_full_result:
            return 0
        self._rebuild_office_filter_cache()
        if self._office_filtered_result:
            return count_task_result_features(self._office_filtered_result)
        return 0

    def _view_result(self) -> TaskResult:
        if self._office_awaiting_order() and self._office_full_result:
            return empty_task_result_shell(self._office_full_result)
        if self._office_working() and self._office_full_result and self._office_area_order:
            self._rebuild_office_filter_cache()
            if self._office_filtered_result:
                return self._office_filtered_result
            return filter_task_result_by_area(
                self._office_full_result, self._office_area_order
            )
        return self._result

    def _sync_office_display(self) -> None:
        if not self._is_office_user() or self._task_source != "active":
            self._result = self._office_full_result or self._result
            self._invalidate_office_filter_cache()
            return
        if self._office_area_order is None:
            base = self._office_full_result or self._result
            self._result = empty_task_result_shell(base)
            self._invalidate_office_filter_cache()
        else:
            self._rebuild_office_filter_cache()
            self._result = (
                self._office_filtered_result
                or filter_task_result_by_area(
                    self._office_full_result or self._result,
                    self._office_area_order,
                )
            )

    def _load_area_orders(self) -> List[TaskFeature]:
        conn = self._get_db_connection()
        if conn is None or not self._result.district_name:
            return []
        try:
            return collect_area_orders_for_picker(conn, self._result.district_name)
        except Exception as exc:
            log_warning(f"Не удалось загрузить площадные заказы: {exc}")
            return []

    def _open_office_order_picker(self) -> None:
        if not self._is_office_user():
            return
        orders = self._load_area_orders()
        login = self._current_user_login()

        def _refresh_orders() -> None:
            if self._office_picker is None:
                return
            self._office_picker.set_loading(True)
            refreshed = self._load_area_orders()
            self._office_picker.set_orders(refreshed)
            self._office_picker.set_loading(False)

        self._office_picker = AreaOrderPickerDialog(
            orders,
            login,
            self,
        )
        self._office_picker.set_refresh_handler(_refresh_orders)
        from ..core.qt_compat import DIALOG_ACCEPTED, dialog_exec

        if dialog_exec(self._office_picker) == DIALOG_ACCEPTED:
            selected = self._office_picker.selected_order()
            if selected:
                self._on_office_order_selected(selected)
        self._office_picker = None

    def _on_office_order_selected(self, order: TaskFeature) -> None:
        key = self._office_order_key(order)
        if not key:
            return
        conn = self._get_db_connection()
        if conn is None:
            return
        self._busy = True
        self._update_office_actions()
        try:
            result = start_area_analise(conn, key, self._current_user_login())
            if result == "conflict":
                QMessageBox.warning(
                    self,
                    "Monitor CRM — площадный заказ",
                    "Заказ уже в работе у другого сотрудника.",
                )
                self._open_office_order_picker()
                return
            if result == "not_found":
                QMessageBox.warning(
                    self,
                    "Monitor CRM — площадный заказ",
                    "Не удалось начать анализ заказа.",
                )
                return
            refreshed_orders = self._load_area_orders()
            for item in refreshed_orders:
                item_key = self._office_order_key(item)
                if item_key == key:
                    order = item
                    break
            self._office_area_order = order
            self._office_orders_on_map = False
            self._invalidate_office_filter_cache()
            self._apply_office_order_filter(order)
        finally:
            self._busy = False
            self._update_office_actions()

    def _order_has_area_geom(self, order: Optional[TaskFeature]) -> bool:
        if order is None:
            return False
        geom = order.area_geom
        return geom is not None and not geom.isEmpty()

    def _apply_office_order_filter(self, order: TaskFeature) -> None:
        """Пространственный фильтр в UI-потоке (QgsGeometry не потокобезопасен)."""
        self.status_label.setText("Фильтрация задач по полигону заказа…")
        QApplication.processEvents()

        full_count = (
            count_task_result_features(self._office_full_result)
            if self._office_full_result
            else 0
        )
        order_geom = self._order_has_area_geom(order)

        if not self._office_full_result:
            self._sync_office_display()
            self._apply_office_order_ui()
            self.status_label.setText(
                "В районе нет активных задач. "
                "Нажмите «Синхронизировать задачи района»."
            )
            log_info(
                f"office filter: full=0 filtered=0 order_geom={'yes' if order_geom else 'no'}"
            )
            return

        try:
            self._rebuild_office_filter_cache()
            self._sync_office_display()
        except Exception as exc:
            log_warning(f"Фильтрация office-заказа: {exc}")
            self._sync_office_display()

        filtered_count = count_task_result_features(self._result)
        log_info(
            f"office filter: full={full_count} filtered={filtered_count} "
            f"order_geom={'yes' if order_geom else 'no'}"
        )

        self._apply_office_order_ui()

        if full_count == 0:
            self.status_label.setText(
                "В районе нет активных задач. "
                "Нажмите «Синхронизировать задачи района»."
            )
        elif not order_geom:
            self.status_label.setText(
                "Не удалось загрузить полигон заказа. "
                "Проверьте журнал QGIS (crm.tasks_area)."
            )
        elif filtered_count == 0:
            self.status_label.setText(
                f"В полигоне заказа активных задач не найдено (в районе: {full_count})."
            )

    def _apply_office_order_ui(self) -> None:
        self._populate_tree()
        self._clear_row_selection()
        first = self._first_subgroup_item()
        if first:
            self.tree.setCurrentItem(first)
        else:
            self._fill_table(None)
        self._refresh_area_map()
        self._update_status()

    def _metric_srid(self) -> int:
        cfg = crm_tasks(self._config) if self._config else {}
        metric_crs_name = cfg.get("metric_crs", "EPSG:32637")
        from qgis.core import QgsCoordinateReferenceSystem

        metric_crs = QgsCoordinateReferenceSystem(metric_crs_name)
        metric_srid = metric_crs.postgisSrid()
        if metric_srid <= 0:
            auth = metric_crs.authid() or ""
            if auth.upper().startswith("EPSG:"):
                try:
                    metric_srid = int(auth.split(":", 1)[1])
                except ValueError:
                    metric_srid = 32637
            else:
                metric_srid = 32637
        return metric_srid

    def _on_sync_district_tasks(self) -> None:
        if not self._is_office_user() or not self._config or not self._district:
            QMessageBox.warning(
                self,
                "Monitor CRM",
                "Синхронизация недоступна: нужны конфигурация и район.",
            )
            return
        conn = self._get_db_connection()
        if conn is None:
            return

        source_result = (
            self._office_full_result or self._active_result or self._result
        )
        progress = QProgressDialog(
            "Синхронизация задач района…", None, 0, 0, self
        )
        progress.setWindowTitle("Monitor CRM")
        progress.setMinimumDuration(0)
        progress.show()

        try:
            from ..core.crm_field_data import append_field_data_to_result
            from ..core.crm_office_data import append_office_data_to_result

            ensure_all_snapshot_tables(conn, self._store_cfg)
            login = self._current_user_login()
            stats = persist_task_result(
                conn, source_result, self._store_cfg, login
            )
            ensure_crm_session_cache(conn, self._store_cfg, force_reload=True)
            metric_srid = self._metric_srid()
            append_field_data_to_result(
                source_result,
                conn,
                self._district,
                self._store_cfg,
                metric_srid,
            )
            append_office_data_to_result(
                source_result,
                conn,
                self._district,
                self._store_cfg,
                metric_srid,
            )

            self._office_full_result = copy_task_result(source_result)
            self._active_result = copy_task_result(source_result)
            self._invalidate_office_filter_cache()
            self._apply_snapshot_filter()

            layer_stats = create_task_layers_in_qgis(
                self._iface,
                conn,
                self._district,
                self._config,
                self._allowed_sources,
                active_result=self._active_result,
                parent=self,
            )

            lines = [
                f"Добавлено в crm.tasks: {stats.inserted}",
                f"Уже в БД: {stats.skipped}",
                f"Без ID: {stats.invalid}",
                f"Слоёв QGIS: {layer_stats.layers_created}",
                f"Объектов на слоях: {layer_stats.features_added}",
            ]
            if layer_stats.warnings:
                lines.append("")
                lines.extend(f"• {w}" for w in layer_stats.warnings)
            QMessageBox.information(
                self,
                "Monitor CRM — синхронизация",
                "Синхронизация задач района завершена.\n\n" + "\n".join(lines),
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Monitor CRM — синхронизация",
                f"Не удалось синхронизировать задачи района:\n{exc}",
            )
        finally:
            progress.close()

    def _on_pause_office_order(self) -> None:
        if not self._office_working():
            return
        key = self._office_order_key(self._office_area_order)
        if not key:
            return
        conn = self._get_db_connection()
        if conn is None:
            return
        self._busy = True
        try:
            result = pause_area_analise(conn, key, self._current_user_login())
            if result not in ("updated", "skipped"):
                QMessageBox.warning(
                    self,
                    "Monitor CRM — площадный заказ",
                    "Не удалось приостановить анализ.",
                )
                return
            self._office_area_order = None
            self._office_orders_on_map = False
            self._invalidate_office_filter_cache()
            self._sync_office_display()
            self._populate_tree()
            self._clear_row_selection()
            self._fill_table(None)
            self._refresh_area_map()
            self._update_status()
            self._open_office_order_picker()
        finally:
            self._busy = False
            self._update_office_actions()

    def _on_complete_office_order(self) -> None:
        if not self._office_working():
            return
        remaining = self._office_remaining_count()
        if remaining > 0:
            QMessageBox.information(
                self,
                "Monitor CRM — площадный заказ",
                f"В полигоне остались активные задачи: {remaining}",
            )
            return
        key = self._office_order_key(self._office_area_order)
        if not key:
            return
        conn = self._get_db_connection()
        if conn is None:
            return
        self._busy = True
        try:
            result = complete_area_analise(conn, key, self._current_user_login())
            if result not in ("updated", "skipped"):
                QMessageBox.warning(
                    self,
                    "Monitor CRM — площадный заказ",
                    "Не удалось завершить анализ.",
                )
                return
            self._on_refresh()
            self._office_area_order = None
            self._office_orders_on_map = False
            self._sync_office_display()
            self._populate_tree()
            self._clear_row_selection()
            self._fill_table(None)
            self._refresh_area_map()
            self._update_status()
            self._open_office_order_picker()
        finally:
            self._busy = False
            self._update_office_actions()

    def _on_office_orders_toggle(self) -> None:
        if not self._office_working():
            return
        self._office_orders_on_map = self._source_tabs._orders_toggle_btn.isChecked()
        self._refresh_area_map()
        self._update_office_actions()

    def _update_office_actions(self) -> None:
        if not self._is_office_user():
            self._office_context_label.hide()
            self._source_tabs.set_office_actions(visible=False)
            return

        self._office_context_label.show()
        self._source_tabs.set_office_actions(
            visible=self._task_source == "active",
            awaiting_order=self._office_awaiting_order(),
            working=self._office_working(),
            can_complete=self._office_working() and self._office_remaining_count() == 0,
            complete_tooltip=(
                f"В полигоне остались активные задачи: {self._office_remaining_count()}"
                if self._office_working() and self._office_remaining_count() > 0
                else "Завершить анализ заказа"
            ),
            orders_on_map=self._office_orders_on_map,
            place_point_mode=self._place_point_mode,
        )

        if self._office_working() and self._office_area_order:
            attrs = self._office_area_order.attributes or {}
            order_label = format_area_order_label(self._office_area_order)
            area_text = format_area_hectares(attrs.get("area"))
            status_text = format_analise_workflow_status(attrs)
            remaining = self._office_remaining_count()
            parts = [f"Заказ: <b>{order_label}</b>"]
            if area_text:
                parts.append(area_text)
            parts.append(f"Статус: {status_text}")
            parts.append(f"Осталось задач: <b>{remaining}</b>")
            self._office_context_label.setText(" · ".join(parts))
            self._office_context_label.setTextFormat(TEXT_FORMAT_RICH)
        elif self._office_awaiting_order():
            self._office_context_label.setText(
                "Выберите площадный заказ для начала сопоставления"
            )
            self._office_context_label.setTextFormat(Qt.PlainText)
        else:
            self._office_context_label.hide()

    def _on_toggle_place_point(self) -> None:
        if not self._office_working() or self._place_point_busy:
            return
        checked = self._source_tabs._place_point_btn.isChecked()
        self._enable_place_point_mode(checked)

    def start_place_office_point_from_edit(
        self, link_prefill: Optional[dict] = None
    ) -> None:
        self._enable_place_point_mode(True, link_prefill)

    def _enable_place_point_mode(
        self,
        active: bool,
        link_prefill: Optional[dict] = None,
    ) -> None:
        if link_prefill is not None:
            self._pending_link_prefill = link_prefill

        if not active:
            self._place_point_mode = False
            self._pending_link_prefill = None
            if self._place_point_tool and self._iface:
                self._iface.mapCanvas().unsetMapTool(self._place_point_tool)
            self.status_label.setObjectName("crmMuted")
            self.status_label.setText("")
            self._update_office_actions()
            return

        if not self._iface or not self._office_area_order:
            return

        self._place_point_mode = True
        if self._place_point_tool is None:
            from .office_place_point_tool import OfficePlacePointMapTool

            self._place_point_tool = OfficePlacePointMapTool(self._iface.mapCanvas())
            self._place_point_tool.pointPlaced.connect(self._on_office_point_placed)
        self._iface.mapCanvas().setMapTool(self._place_point_tool)
        self.status_label.setObjectName("crmPickBanner")
        self.status_label.setText(
            "Укажите точку разрывия на карте внутри полигона заказа"
        )
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._update_office_actions()

    def _metric_srid(self) -> int:
        from qgis.core import QgsCoordinateReferenceSystem

        cfg = crm_tasks(self._config or {})
        metric_crs = QgsCoordinateReferenceSystem(cfg.get("metric_crs", "EPSG:32637"))
        metric_srid = metric_crs.postgisSrid()
        if metric_srid > 0:
            return metric_srid
        auth = metric_crs.authid() or ""
        if auth.upper().startswith("EPSG:"):
            try:
                return int(auth.split(":", 1)[1])
            except ValueError:
                pass
        return 32637

    def _reload_office_data_subgroup(self) -> None:
        conn = self._db_conn
        if not conn or not self._district or not self._store_cfg:
            return
        from ..core.crm_office_data import append_office_data_to_result

        if self._is_office_user():
            if self._office_full_result is None:
                self._office_full_result = copy_task_result(self._result)
            base = self._office_full_result
        else:
            base = self._result
        append_office_data_to_result(
            base,
            conn,
            self._district,
            self._store_cfg,
            self._metric_srid(),
        )
        if self._is_office_user():
            self._office_full_result = base

    def _refresh_current_subgroup_view(self) -> None:
        current = self.tree.currentItem()
        role = self._role_data(current)
        subgroup = self._subgroup_from_role(role)
        self._current_subgroup = subgroup
        self._current_group_name = self._group_name_from_role(role)
        self._fill_table(subgroup)
        if subgroup and subgroup.features and self._selected_row is not None:
            row = min(self._selected_row, len(subgroup.features) - 1)
            self._select_row(row)

    def _on_office_point_placed(self, lng: float, lat: float) -> None:
        if not self._office_working() or self._place_point_busy:
            return

        area_order = self._office_area_order
        area_key = self._office_order_key(area_order) if area_order else None
        if not area_key:
            return

        from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY

        point_geom = QgsGeometry.fromPointXY(QgsPointXY(lng, lat))
        area_geom = area_order.area_geom if area_order else None
        if area_geom and not geometry_inside_area(point_geom, area_geom):
            QMessageBox.warning(
                self,
                "Monitor CRM",
                "Точка должна находиться внутри полигона площадного заказа.",
            )
            return

        conn = self._get_db_connection()
        if conn is None:
            return

        self._place_point_busy = True
        self._update_office_actions()
        try:
            from ..core.crm_office_tasks import create_office_task

            create_office_task(
                conn,
                self._current_user_login(),
                lng,
                lat,
                area_key,
                link_prefill=self._pending_link_prefill,
                store_cfg=self._store_cfg,
            )
            self._invalidate_office_filter_cache()
            self._reload_office_data_subgroup()
            self._sync_office_display()
            self._populate_tree()
            self._refresh_current_subgroup_view()
            self._update_status()
            self._update_office_actions()
            self._refresh_area_map()
            self.status_label.setObjectName("crmSuccess")
            self.status_label.setText("Точка камерального анализа добавлена")
            self._enable_place_point_mode(False)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Monitor CRM",
                str(exc),
            )
        finally:
            self._place_point_busy = False
            self._update_office_actions()

    def _refresh_office_points_map(self) -> None:
        if not self._office_points_map:
            return
        conn = self._db_conn
        if not conn or not self._district or not self._store_cfg:
            return
        self._office_points_map.refresh_from_db(
            conn,
            self._district,
            self._store_cfg,
            self._metric_srid(),
        )

    def _refresh_area_map(self) -> None:
        if not self._area_map:
            return
        self._area_map.refresh(
            self._task_source,
            self._view_result(),
            self._db_conn,
            is_area_source=self._is_area(),
            office_selected_order=self._office_area_order,
            office_orders_on_map=self._office_orders_on_map,
            is_office_user=self._is_office_user(),
        )
        self._refresh_office_points_map()

    def _area_status_message(self) -> Optional[str]:
        if not self._is_area():
            return None
        if self._result.total_count > 0:
            return None
        status = area_status_from_source(self._task_source)
        label = AREA_STATUS_LABELS.get(status or "", status or "")
        return f"Нет площадных заказов ({label}) в районе «{self._result.district_name}»"

    def _update_header(self) -> None:
        view = self._view_result()
        name = self._result.district_name
        source_label = TASK_SOURCE_LABELS.get(self._task_source, self._task_source)
        login = self._current_user_login()
        header_parts = [f"Район: <b>{name}</b>"]
        if login:
            header_parts.append(f"Пользователь: {login}")
        self._district_label.setText(" · ".join(header_parts))

        parts = [f"{source_label}: {view.total_count}"]
        if self._office_working():
            parts.append(f"На карте: {view.total_count}")
        if (
            self._task_source == "active"
            and view.total_count == 0
            and not self._office_working()
            and self._area_map
            and self._area_map.overlay_count > 0
        ):
            parts.append(
                f"Площадные заказы на карте: {self._area_map.overlay_count}"
            )
        if self._task_source == "active" and self._result.apply_date_filter:
            parts.append(
                f"Период: {self._result.filter_date_from.toString('dd.MM.yyyy')} — "
                f"{self._result.filter_date_to.toString('dd.MM.yyyy')}"
            )
        elif self._task_source == "active":
            parts.append("Без фильтра по дате")
        self._meta_label.setText(" · ".join(parts))

    def _update_status(self) -> None:
        self._update_header()
        self._update_action_buttons()
        self._update_office_actions()
        area_msg = self._area_status_message()
        if self._office_awaiting_order():
            self.status_label.setText(
                "Выберите площадный заказ — кнопка «Выбрать заказ» или диалог после сбора задач."
            )
        elif area_msg:
            self.status_label.setText(area_msg)
        else:
            self.status_label.clear()

    def _is_area(self) -> bool:
        return is_area_source(self._task_source)

    def _show_sent_at(self) -> bool:
        return not self._is_area() and self._task_source != "active"

    def _update_action_buttons(self) -> None:
        is_area = self._is_area()
        self.execute_btn.setVisible(not is_area)
        self.execute_btn.setText(task_execute_button_label(self._task_source))
        self._area_send_btn.setVisible(is_area)
        self._area_release_btn.setVisible(is_area)
        self._area_complete_btn.setVisible(is_area)

        enabled = self._result.total_count > 0
        self.export_dxf_btn.setEnabled(enabled and not is_area)
        self.export_shp_btn.setEnabled(enabled and not is_area)

        if is_area:
            self._update_area_buttons()

        self.execute_btn.setEnabled(
            not is_area and self._selected_task_feat is not None and not self._busy
        )

    def _update_area_buttons(self) -> None:
        if not self._is_area():
            return
        feat = self._selected_task_feat
        status = str(feat.attributes.get("status", "")) if feat else ""
        can_send = bool(feat) and status != "wip"
        can_manage = bool(feat) and status == "wip"
        self._area_send_btn.setVisible(can_send)
        self._area_release_btn.setVisible(can_manage)
        self._area_complete_btn.setVisible(can_manage)
        self._area_send_btn.setEnabled(can_send and not self._busy)
        self._area_release_btn.setEnabled(can_manage and not self._busy)
        self._area_complete_btn.setEnabled(can_manage and not self._busy)

    def _enrich_field_observed(self) -> None:
        if not self._db_conn or not self._store_cfg or self._is_area():
            return
        try:
            enrich_task_result_field_observed(
                self._result, self._db_conn, self._store_cfg
            )
        except Exception as exc:
            log_warning(f"Не удалось загрузить field_observed: {exc}")

    def _apply_snapshot_filter(self) -> None:
        if not self._db_conn or not self._store_cfg:
            return
        try:
            ensure_crm_session_cache(self._db_conn, self._store_cfg)
        except Exception as exc:
            log_warning(f"CRM cache: {exc}")
        filter_sent_tasks_from_result(self._result, self._db_conn, self._store_cfg)
        self._enrich_field_observed()
        if self._task_source == "active":
            self._active_result = copy_task_result(self._result)
            if self._is_office_user():
                self._office_full_result = copy_task_result(self._result)
                self._invalidate_office_filter_cache()
                if self._office_area_order is None:
                    self._sync_office_display()
        self._populate_tree()
        self._clear_row_selection()
        if self._current_subgroup and self._current_subgroup.features:
            self._fill_table(self._current_subgroup)
        else:
            first_subgroup = self._first_subgroup_item()
            if first_subgroup:
                self.tree.setCurrentItem(first_subgroup)
            else:
                self._fill_table(None)
        self._refresh_area_map()
        self._update_status()

    def _populate_tree(self) -> None:
        self.tree.clear()
        view = self._view_result()
        for group_index, group in enumerate(view.groups):
            group_count = sum(len(sub.features) for sub in group.subgroups)
            group_item = QTreeWidgetItem([f"{group.name} ({group_count})"])
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
            return self._view_result().groups[group_index].subgroups[sub_index]
        except (IndexError, ValueError):
            return None

    def _group_name_from_role(self, role: Optional[TreeRole]) -> str:
        if not role:
            return ""
        if role[0] == "group" and len(role) == 2:
            group_index = int(role[1])
            return self._view_result().groups[group_index].name
        if role[0] == "sub" and len(role) == 3:
            group_index = int(role[1])
            return self._view_result().groups[group_index].name
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
        self._update_area_buttons()
        if self._area_map:
            self._area_map.clear_selection()

    def _apply_table_header_resize(self) -> None:
        header = self.table.horizontalHeader()
        count = self.table.columnCount()
        if count <= 0:
            return
        for index in range(count - 1):
            header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(count - 1, QHeaderView.Stretch)

    def _fill_table(self, subgroup: Optional[TaskSubgroup]) -> None:
        self.table.blockSignals(True)
        self.table.clear()
        if subgroup is None:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.table.blockSignals(False)
            return

        is_area = self._is_area()
        show_sent_at = self._show_sent_at()
        attrs_list = [f.attributes for f in subgroup.features]
        self._table_columns = resolve_task_table_columns(
            subgroup.name, is_area, attrs_list, show_sent_at
        )

        headers = ["Заказ" if is_area else "Слой"]
        if show_sent_at:
            headers.append("Отправлено")
        headers.extend(col.label for col in self._table_columns)

        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self._apply_table_header_resize()
        self.table.setRowCount(len(subgroup.features))

        for row, task_feat in enumerate(subgroup.features):
            col_idx = 0
            order_label = (
                format_area_order_label(task_feat)
                if is_area
                else task_feat.layer_name
            )
            self._set_cell(row, col_idx, order_label)
            col_idx += 1
            if show_sent_at:
                self._set_cell(row, col_idx, _format_sent_at(task_feat.sent_at))
                col_idx += 1
            for col in self._table_columns:
                if col.format == "field_observed":
                    cell_text = format_field_observed(
                        task_feat.attributes.get("field_observed")
                    )
                else:
                    cell_text = format_task_table_cell(
                        task_feat.attributes.get(col.field, ""), col.format
                    )
                self._set_cell(row, col_idx, cell_text)
                col_idx += 1

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
        self.execute_btn.setEnabled(not self._is_area() and not self._busy)
        self._update_area_buttons()
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
            self._update_area_buttons()
            if self._area_map:
                self._area_map.clear_selection()
            return
        self._select_row(rows[0].row())

    def _get_db_connection(self) -> Optional[DatabaseConnection]:
        if self._db_conn is not None:
            return self._db_conn
        if not self._config:
            return None
        conn = connect_db(self._config)
        if conn is not None:
            self._db_conn = conn
            self._own_db_conn = True
        return conn

    def _on_execute_task(self) -> None:
        if not self._selected_task_feat or not self._current_subgroup:
            return
        if not self._store_cfg:
            QMessageBox.warning(
                self, "Мониторинг разрытий — задачи",
                "Конфигурация task_store не найдена.",
            )
            return

        conn = self._get_db_connection()
        if conn is None:
            return

        record = None
        if self._selected_task_feat.task_key:
            record = fetch_task_by_key(
                conn, self._store_cfg, self._selected_task_feat.task_key
            )
        if record is None:
            record = fetch_task_for_feature(
                conn,
                self._current_subgroup.name,
                self._selected_task_feat.attributes,
                self._store_cfg,
                layer=self._selected_task_feat.layer,
            )
        if record is None:
            QMessageBox.warning(
                self,
                "Мониторинг разрытий — задачи",
                "Задача не найдена в crm.tasks.\n\n"
                "Сначала сохраните задачи при получении списка.",
            )
            return

        from .task_edit_dialog import TaskEditDialog

        def _on_edit_closed(_result: int) -> None:
            self._on_refresh()

        TaskEditDialog.open_edit(
            record,
            conn,
            self._store_cfg,
            self._iface.mainWindow() if self._iface else None,
            iface=self._iface,
            config=self._config,
            subgroup_name=self._current_subgroup.name,
            group_name=self._current_group_name,
            task_source=self._task_source,
            on_finished=_on_edit_closed,
            user_login=self._current_user_login(),
            feature_attributes=self._selected_task_feat.attributes,
            office_working=self._office_working(),
            on_start_place_office_point=self.start_place_office_point_from_edit,
        )

    def _current_user_login(self) -> str:
        if self._user_session is not None:
            return self._user_session.login
        return ""

    def _area_task_key(self) -> Optional[str]:
        if not self._selected_task_feat:
            return None
        key = self._selected_task_feat.task_key or self._selected_task_feat.attributes.get("key")
        return str(key).strip() if key else None

    def _run_area_action(self, fn, success_msg: str, skip_msg: str) -> None:
        key = self._area_task_key()
        if not key:
            return
        conn = self._get_db_connection()
        if conn is None:
            return
        self._busy = True
        self._update_action_buttons()
        try:
            result = fn(conn, key, self._current_user_login())
            if result == "updated":
                self.status_label.setText(success_msg)
                invalidate_area_geometries_cache(conn, self._result.district_name)
                try:
                    preload_area_geometries(conn, self._result.district_name)
                except Exception:
                    pass
                self._on_refresh()
            elif result == "skipped":
                self.status_label.setText(skip_msg)
            else:
                self.status_label.setText("Заказ не найден")
        finally:
            self._busy = False
            self._update_action_buttons()

    def _on_send_area_to_survey(self) -> None:
        self._run_area_action(
            send_area_to_survey,
            "Отправлено на полевое обследование (статус: wip)",
            "Уже на обследовании (wip)",
        )

    def _on_release_area_from_survey(self) -> None:
        self._run_area_action(
            release_area_from_survey,
            "Снято с обследования (статус: free)",
            "Заказ не найден или не на обследовании",
        )

    def _on_complete_area_survey(self) -> None:
        self._run_area_action(
            complete_area_survey,
            "Обследование завершено (статус: done)",
            "Заказ не найден или не на обследовании",
        )

    def _on_change_district(self) -> None:
        if self._on_change_district:
            self.close()
            self._on_change_district()

    def _on_refresh(self) -> None:
        self._reload_for_source(self._task_source)

    def _on_source_changed(self, source: str) -> None:
        if source not in self._allowed_sources:
            self._source_tabs.set_value(self._task_source)
            return
        self._reload_for_source(source)

    def _reload_for_source(self, source: str) -> None:
        if source not in self._allowed_sources:
            self._source_tabs.set_value(self._task_source)
            return
        self._source_tabs.set_loading(True)
        if not self._config or not self._district:
            if source == "active":
                self._result = copy_task_result(self._active_result)
                self._task_source = source
                self._source_tabs.set_value(source)
                if self._db_conn and self._store_cfg:
                    self._apply_snapshot_filter()
                else:
                    self._populate_tree()
                    self._refresh_area_map()
                    self._update_status()
                return
            QMessageBox.warning(
                self,
                "Мониторинг разрытий — задачи",
                "Для этого источника нужно подключение к БД и район.",
            )
            self._source_tabs.set_value(self._task_source)
            return

        conn = self._get_db_connection()
        if conn is None:
            self._source_tabs.set_value(self._task_source)
            return

        progress = QProgressDialog("Загрузка задач…", "Отмена", 0, 0, self)
        progress.setWindowTitle("Monitor CRM")
        progress.setMinimumDuration(0)
        progress.show()

        try:
            self._result = collect_task_result_for_source(
                source,
                conn,
                self._district,
                self._config,
                active_result=self._active_result,
                store_cfg=self._store_cfg,
            )
            if source == "active" and self._is_office_user():
                self._office_full_result = copy_task_result(self._result)
                self._invalidate_office_filter_cache()
                if self._office_area_order:
                    self._sync_office_display()
                else:
                    self._result = empty_task_result_shell(self._result)
            self._task_source = source
            self._source_tabs.set_value(source)
            self._populate_tree()
            self._clear_row_selection()
            first = self._first_subgroup_item()
            if first:
                self.tree.setCurrentItem(first)
            else:
                self._fill_table(None)
            self._refresh_area_map()
            self._update_status()
        except Exception as exc:
            QMessageBox.warning(
                self, "Мониторинг разрытий — задачи", str(exc)
            )
            self._source_tabs.set_value(self._task_source)
        finally:
            progress.close()
            self._source_tabs.set_loading(False)

    def _on_create_task_layers(self) -> None:
        if not self._config or not self._district:
            QMessageBox.warning(
                self,
                "Мониторинг разрытий — задачи",
                "Для создания слоёв нужны конфигурация плагина и выбранный район.",
            )
            return

        conn = self._get_db_connection()
        if conn is None:
            return

        stats = create_task_layers_in_qgis(
            self._iface,
            conn,
            self._district,
            self._config,
            self._allowed_sources,
            active_result=self._active_result,
            parent=self,
        )

        if stats.layers_created == 0 and not stats.warnings:
            QMessageBox.information(
                self,
                "Мониторинг разрытий — задачи",
                f"В районе «{self._result.district_name}» нет задач "
                f"для создания слоёв.",
            )
            return

        lines = [
            f"Слоёв: {stats.layers_created}",
            f"Объектов: {stats.features_added}",
        ]
        if stats.features_skipped:
            lines.append(f"Пропущено без геометрии: {stats.features_skipped}")
        if stats.warnings:
            lines.append("")
            lines.append("Предупреждения:")
            lines.extend(f"• {w}" for w in stats.warnings)

        if stats.layers_created == 0:
            QMessageBox.warning(
                self,
                "Мониторинг разрытий — задачи",
                "Не удалось создать слои задач.\n\n" + "\n".join(lines),
            )
        else:
            QMessageBox.information(
                self,
                "Мониторинг разрытий — задачи",
                "Слои задач добавлены в проект QGIS.\n\n" + "\n".join(lines),
            )

    def _on_export_dxf(self) -> None:
        if self._result.total_count == 0:
            QMessageBox.warning(self, "Мониторинг разрытий — задачи", "Нет объектов для экспорта.")
            return
        if not self._config:
            QMessageBox.warning(self, "Мониторинг разрытий — задачи", "Конфигурация плагина не найдена.")
            return
        default_name = f"tasks_{self._result.district_name or 'export'}.dxf"
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт задач в DXF", default_name, "DXF files (*.dxf)")
        if not path:
            return
        if not path.lower().endswith(".dxf"):
            path += ".dxf"
        progress = QProgressDialog("Экспорт задач в DXF…", "Отмена", 0, max(self._result.total_count, 1), self)
        progress.setWindowTitle("Мониторинг разрытий")
        progress.setMinimumDuration(0)
        stats = export_tasks_to_dxf(path, self._result, self._config)
        progress.setValue(self._result.total_count)
        if stats.errors:
            QMessageBox.critical(self, "Мониторинг разрытий — задачи", "Не удалось экспортировать задачи в DXF:\n" + "\n".join(stats.errors))
            return
        QMessageBox.information(
            self, "Мониторинг разрытий — задачи",
            f"Экспорт завершён.\n\nDXF: {path}\nID (CSV): {stats.csv_path}\nОбъектов: {stats.exported}",
        )

    def _on_export_shp(self) -> None:
        if self._result.total_count == 0:
            QMessageBox.warning(self, "Мониторинг разрытий — задачи", "Нет объектов для экспорта.")
            return
        if not self._config:
            QMessageBox.warning(self, "Мониторинг разрытий — задачи", "Конфигурация плагина не найдена.")
            return
        default_name = f"tasks_{self._result.district_name or 'export'}.shp"
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт задач в SHP", default_name, "Shapefile (*.shp)")
        if not path:
            return
        if not path.lower().endswith(".shp"):
            path += ".shp"
        progress = QProgressDialog("Экспорт задач в SHP…", "Отмена", 0, max(self._result.total_count, 1), self)
        progress.setWindowTitle("Мониторинг разрытий")
        progress.setMinimumDuration(0)
        stats = export_tasks_to_shp(path, self._result, self._config)
        progress.setValue(self._result.total_count)
        if stats.errors:
            QMessageBox.critical(self, "Мониторинг разрытий — задачи", "Не удалось экспортировать задачи в SHP:\n" + "\n".join(stats.errors))
            return
        QMessageBox.information(self, "Мониторинг разрытий — задачи", f"Экспорт завершён.\n\nSHP: {path}\nОбъектов: {stats.exported}")

    def _ensure_layer_visible(self, layer) -> None:
        if layer is None:
            return
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        if node and not node.isVisible():
            node.setItemVisibilityChecked(True)
            refresh_map_canvas(self._iface)

    def _zoom_extent_for_geometry(self, geom: QgsGeometry, layer_crs, canvas) -> Optional[QgsRectangle]:
        if QgsWkbTypes.geometryType(geom.wkbType()) == QgsWkbTypes.PointGeometry:
            center = geom.asPoint()
            span = 0.0015
            extent = QgsRectangle(center.x() - span, center.y() - span, center.x() + span, center.y() + span)
        else:
            extent = geom.boundingBox()
        if extent.isNull() or extent.isEmpty():
            return None
        dest_crs = canvas.mapSettings().destinationCrs()
        if layer_crs.isValid() and dest_crs.isValid() and layer_crs != dest_crs:
            extent = QgsCoordinateTransform(layer_crs, dest_crs, QgsProject.instance()).transform(extent)
        rect = QgsRectangle(extent)
        rect.scale(1.5)
        return rect

    def _zoom_to_feature(self, task_feat: TaskFeature) -> None:
        if task_feat.area_geom and not task_feat.area_geom.isEmpty():
            from qgis.core import QgsCoordinateReferenceSystem

            canvas = self._iface.mapCanvas()
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            rect = self._zoom_extent_for_geometry(
                task_feat.area_geom, wgs84, canvas
            )
            if rect is None:
                return
            canvas.setExtent(rect)
            canvas.refresh()
            self._clear_highlight()
            if self._area_map:
                self._area_map.highlight_feature(task_feat)
            return

        if task_feat.task_geom and not task_feat.task_geom.isEmpty():
            from qgis.core import QgsCoordinateReferenceSystem

            canvas = self._iface.mapCanvas()
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            rect = self._zoom_extent_for_geometry(
                task_feat.task_geom, wgs84, canvas
            )
            if rect is None:
                return
            canvas.setExtent(rect)
            canvas.refresh()
            self._clear_highlight()
            return

        layer = task_feat.layer
        if not layer or not layer.isValid() or task_feat.feature_id is None:
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
        if self._own_db_conn and self._db_conn is not None:
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
        district: Optional[DistrictBoundary] = None,
        apply_date_filter: bool = True,
        on_change_district=None,
        user_session: Optional[UserSession] = None,
    ) -> "TaskDialog":
        dlg = cls(
            result,
            iface,
            parent or (iface.mainWindow() if iface else None),
            config=config,
            db_conn=db_conn,
            district=district,
            apply_date_filter=apply_date_filter,
            on_change_district=on_change_district,
            user_session=user_session,
        )
        register_modeless_dialog(iface, dlg)
        show_modeless_dialog(dlg, dlg.parent())
        return dlg

    def closeEvent(self, event) -> None:
        self._enable_place_point_mode(False)
        self._clear_highlight()
        if self._area_map:
            try:
                self._area_map.clear()
            except Exception:
                pass
        self._close_db_conn()
        super().closeEvent(event)

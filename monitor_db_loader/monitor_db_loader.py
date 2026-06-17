# -*- coding: utf-8 -*-
"""Main plugin class for Monitor DB Loader."""

import os

from qgis.core import QgsMessageLog, Qgis
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from .core.config import (
    LOG_CHANNEL,
    database_connection,
    load_config,
    load_on_startup,
)
from .core.db import DatabaseConnection
from .core.layer_loader import LayerLoader
from .core.layer_utils import refresh_map_canvas, zoom_map_to_layers
from .core.log_util import log_info
from .core.crm_tasks import run_get_task
from .core.photo_primary_analysis import run_primary_analysis
from .core.qt_compat import MSGBOX_CANCEL, MSGBOX_RETRY
from .ui.password_dialog import PasswordDialog


class MonitorDbLoader:
    """QGIS plugin: load PostgreSQL layers from JSON configuration."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.action = None
        self.analysis_action = None
        self.task_action = None
        self._config = None
        self._loaded_layer_ids = []
        self._loaded_group_names = []

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(icon, "Загрузить слои Monitor DB", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&Monitor DB Loader", self.action)
        self.iface.addToolBarIcon(self.action)

        self.analysis_action = QAction(
            icon, "Первичный анализ фото", self.iface.mainWindow()
        )
        self.analysis_action.triggered.connect(self.run_primary_analysis)
        self.iface.addPluginToMenu("&Monitor DB Loader", self.analysis_action)
        self.iface.addToolBarIcon(self.analysis_action)

        self.task_action = QAction(
            icon, "Получить задачу", self.iface.mainWindow()
        )
        self.task_action.triggered.connect(self.run_get_task)
        self.iface.addPluginToMenu("&Monitor DB Loader", self.task_action)
        self.iface.addToolBarIcon(self.task_action)

        try:
            self._config = load_config()
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Ошибка чтения конфигурации: {exc}",
                LOG_CHANNEL,
                Qgis.Critical,
            )
            return

        if load_on_startup(self._config):
            QTimer.singleShot(500, self.run)

    def unload(self):
        if self.task_action:
            self.iface.removePluginMenu("&Monitor DB Loader", self.task_action)
            self.iface.removeToolBarIcon(self.task_action)
            del self.task_action
        if self.analysis_action:
            self.iface.removePluginMenu("&Monitor DB Loader", self.analysis_action)
            self.iface.removeToolBarIcon(self.analysis_action)
            del self.analysis_action
        if self.action:
            self.iface.removePluginMenu("&Monitor DB Loader", self.action)
            self.iface.removeToolBarIcon(self.action)
            del self.action

    def run_primary_analysis(self):
        if self._config is None:
            try:
                self._config = load_config()
            except Exception as exc:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Monitor DB Loader",
                    f"Не удалось загрузить конфигурацию:\n{exc}",
                )
                return

        log_info("Запуск первичного анализа фото…")
        run_primary_analysis(self._config, self.iface, self.iface.mainWindow())

    def run_get_task(self):
        if self._config is None:
            try:
                self._config = load_config()
            except Exception as exc:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Monitor DB Loader",
                    f"Не удалось загрузить конфигурацию:\n{exc}",
                )
                return

        log_info("Запуск «Получить задачу»…")
        run_get_task(self._config, self.iface, self.iface.mainWindow())

    def run(self):
        if self._config is None:
            try:
                self._config = load_config()
            except Exception as exc:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Monitor DB Loader",
                    f"Не удалось загрузить конфигурацию:\n{exc}",
                )
                return

        db_cfg = database_connection(self._config)
        connection_name = db_cfg.get("connection_name", "Monitor DB Connection")

        while True:
            password = PasswordDialog.ask(
                connection_name, self.iface.mainWindow()
            )
            if password is None:
                return

            conn = DatabaseConnection(db_cfg, password)
            ok, err = conn.test_connection()
            if ok:
                break

            conn.close()
            reply = QMessageBox.critical(
                self.iface.mainWindow(),
                "Monitor DB Loader — ошибка подключения",
                f"Не удалось подключиться к базе данных:\n{err}",
                MSGBOX_RETRY | MSGBOX_CANCEL,
                MSGBOX_RETRY,
            )
            if reply != MSGBOX_RETRY:
                return

        try:
            log_info("Запуск загрузки Monitor DB Loader…")
            if self._loaded_layer_ids or self._loaded_group_names:
                loader_cleanup = LayerLoader(self._config, conn)
                loader_cleanup.remove_previous_layers(
                    self._loaded_layer_ids, self._loaded_group_names
                )
                self._loaded_layer_ids = []
                self._loaded_group_names = []

            loader = LayerLoader(self._config, conn)
            result = loader.load_all()
            self._loaded_layer_ids = result.layer_ids
            self._loaded_group_names = result.group_names

            if result.loaded > 0:
                zoom_map_to_layers(self.iface, result.layer_ids)
            else:
                refresh_map_canvas(self.iface)

            LayerLoader.show_summary(result, self.iface.mainWindow())
        finally:
            conn.close()

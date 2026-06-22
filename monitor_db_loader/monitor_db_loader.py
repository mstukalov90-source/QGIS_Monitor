# -*- coding: utf-8 -*-
"""Main plugin class for Monitor DB Loader."""

import os
from typing import Optional

from qgis.core import QgsMessageLog, Qgis
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from .core.auth import DB_PASSWORD, UserSession, authenticate
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
from .ui.login_dialog import LoginDialog


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
        self._user_session: Optional[UserSession] = None

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

    def _ensure_config(self) -> bool:
        if self._config is not None:
            return True
        try:
            self._config = load_config()
            return True
        except Exception as exc:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Monitor DB Loader",
                f"Не удалось загрузить конфигурацию:\n{exc}",
            )
            return False

    def _db_connection(self) -> Optional[DatabaseConnection]:
        if not self._ensure_config():
            return None
        db_cfg = database_connection(self._config)
        conn = DatabaseConnection(db_cfg, DB_PASSWORD)
        ok, err = conn.test_connection()
        if ok:
            return conn
        conn.close()
        QMessageBox.critical(
            self.iface.mainWindow(),
            "Monitor DB Loader — ошибка подключения",
            f"Не удалось подключиться к базе данных:\n{err}",
        )
        return None

    def _ensure_session(self) -> Optional[UserSession]:
        if self._user_session is not None:
            return self._user_session
        if not self._ensure_config():
            return None

        parent = self.iface.mainWindow()
        while True:
            creds = LoginDialog.ask(parent)
            if creds is None:
                return None
            login, password = creds

            conn = self._db_connection()
            if conn is None:
                return None

            session = authenticate(conn, login, password)
            conn.close()
            if session is not None:
                self._user_session = session
                log_info(f"Вход выполнен: {session.login} (роль {session.role})")
                return session

            QMessageBox.warning(
                parent,
                "Monitor DB Loader — вход",
                "Неверный логин или пароль.",
            )

    def run_primary_analysis(self):
        if not self._ensure_config():
            return
        session = self._ensure_session()
        if session is None:
            return

        log_info("Запуск первичного анализа фото…")
        db_conn = self._db_connection()
        try:
            run_primary_analysis(
                self._config,
                self.iface,
                self.iface.mainWindow(),
                user_session=session,
                db_conn=db_conn,
            )
        finally:
            if db_conn is not None:
                db_conn.close()

    def run_get_task(self):
        if not self._ensure_config():
            return
        session = self._ensure_session()
        if session is None:
            return

        log_info("Запуск «Получить задачу»…")
        db_conn = self._db_connection()
        run_get_task(
            self._config,
            self.iface,
            self.iface.mainWindow(),
            user_session=session,
            db_conn=db_conn,
        )

    def run(self):
        if not self._ensure_config():
            return
        session = self._ensure_session()
        if session is None:
            return

        conn = self._db_connection()
        if conn is None:
            return

        try:
            log_info("Запуск загрузки Monitor DB Loader…")
            if self._loaded_layer_ids or self._loaded_group_names:
                loader_cleanup = LayerLoader(self._config, conn, session)
                loader_cleanup.remove_previous_layers(
                    self._loaded_layer_ids, self._loaded_group_names
                )
                self._loaded_layer_ids = []
                self._loaded_group_names = []

            loader = LayerLoader(self._config, conn, session)
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

# -*- coding: utf-8 -*-
"""Plugin message log helpers."""

from qgis.core import QgsMessageLog, Qgis

from .config import LOG_CHANNEL


def log_info(message: str) -> None:
    QgsMessageLog.logMessage(message, LOG_CHANNEL, Qgis.Info)


def log_warning(message: str) -> None:
    QgsMessageLog.logMessage(message, LOG_CHANNEL, Qgis.Warning)


def log_critical(message: str) -> None:
    QgsMessageLog.logMessage(message, LOG_CHANNEL, Qgis.Critical)

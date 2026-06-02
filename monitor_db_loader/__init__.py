# -*- coding: utf-8 -*-
"""Monitor DB Loader QGIS plugin."""

from .monitor_db_loader import MonitorDbLoader


def classFactory(iface):  # pylint: disable=invalid-name
    return MonitorDbLoader(iface)

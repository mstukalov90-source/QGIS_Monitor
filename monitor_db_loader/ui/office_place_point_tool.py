# -*- coding: utf-8 -*-
"""Инструмент размещения точки камерального анализа на карте."""

from qgis.core import QgsCoordinateTransform, QgsProject
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor

from ..core.layer_utils import STORAGE_CRS


class OfficePlacePointMapTool(QgsMapTool):
    pointPlaced = pyqtSignal(float, float)

    def activate(self) -> None:
        self.canvas().setCursor(QCursor(Qt.CrossCursor))

    def deactivate(self) -> None:
        self.canvas().unsetCursor()

    def canvasReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return

        map_point = self.toMapCoordinates(event.pos())
        canvas = self.canvas()
        dest_crs = canvas.mapSettings().destinationCrs()
        point = map_point
        if dest_crs.isValid() and STORAGE_CRS.isValid() and dest_crs != STORAGE_CRS:
            transform = QgsCoordinateTransform(
                dest_crs, STORAGE_CRS, QgsProject.instance().transformContext()
            )
            point = transform.transform(map_point)

        self.pointPlaced.emit(point.x(), point.y())

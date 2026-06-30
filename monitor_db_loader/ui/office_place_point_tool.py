# -*- coding: utf-8 -*-
"""Инструмент размещения точки камерального анализа на карте."""

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QCursor

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


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
        if dest_crs.isValid() and WGS84.isValid() and dest_crs != WGS84:
            transform = QgsCoordinateTransform(
                dest_crs, WGS84, QgsProject.instance().transformContext()
            )
            point = transform.transform(map_point)

        self.pointPlaced.emit(point.x(), point.y())

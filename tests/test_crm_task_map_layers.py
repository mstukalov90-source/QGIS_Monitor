"""Tests for CRM task map layer helpers (geometry + field-data symbology)."""

from __future__ import annotations

import unittest

pytest_skip = False
try:
    from qgis.core import QgsGeometry, QgsWkbTypes

    from monitor_db_loader.core.crm_task_map_layers import (
        _FIELD_DATA_POINT_SYMBOLOGY,
        _SYMBOLOGY,
        _feature_geometry_wgs84,
        _symbology_for_subgroup,
    )
    from monitor_db_loader.core.crm_tasks import TaskFeature
    from monitor_db_loader.core.crm_ui_constants import FIELD_DATA_SUBGROUP
except Exception:
    pytest_skip = True


@unittest.skipIf(pytest_skip, "QGIS monitor_db_loader not importable")
class CrmTaskMapLayersTests(unittest.TestCase):
    def test_feature_geometry_uses_task_geom(self) -> None:
        point = QgsGeometry.fromWkt("POINT(37.6 55.7)")
        feat = TaskFeature(
            layer=None,
            layer_name=FIELD_DATA_SUBGROUP,
            feature_id=None,
            attributes={},
            task_key="k1",
            task_geom=point,
        )
        geom = _feature_geometry_wgs84(feat)
        self.assertIsNotNone(geom)
        self.assertFalse(geom.isEmpty())
        self.assertEqual(geom.asWkt(), point.asWkt())

    def test_feature_geometry_prefers_area_geom_over_task_geom(self) -> None:
        area = QgsGeometry.fromWkt("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))")
        point = QgsGeometry.fromWkt("POINT(37.6 55.7)")
        feat = TaskFeature(
            layer=None,
            layer_name="area",
            feature_id=None,
            attributes={},
            task_key="k2",
            area_geom=area,
            task_geom=point,
        )
        geom = _feature_geometry_wgs84(feat)
        self.assertIsNotNone(geom)
        self.assertEqual(
            QgsWkbTypes.geometryType(geom.wkbType()),
            QgsWkbTypes.PolygonGeometry,
        )

    def test_field_data_point_symbology_is_purple(self) -> None:
        sym = _symbology_for_subgroup(
            FIELD_DATA_SUBGROUP, QgsWkbTypes.PointGeometry
        )
        self.assertIs(sym, _FIELD_DATA_POINT_SYMBOLOGY)
        self.assertEqual(sym["symbology"]["color"], "#9900cc")
        self.assertEqual(sym["symbology"]["marker_type"], "circle")

    def test_other_subgroup_keeps_default_red_point(self) -> None:
        sym = _symbology_for_subgroup("Ордера ОАТИ", QgsWkbTypes.PointGeometry)
        self.assertIs(sym, _SYMBOLOGY[QgsWkbTypes.PointGeometry])
        self.assertEqual(sym["symbology"]["color"], "#cc0000")


if __name__ == "__main__":
    unittest.main()

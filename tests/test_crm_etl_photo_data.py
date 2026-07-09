"""Tests for QGIS ETL photo task loader SQL."""

from __future__ import annotations

import unittest

pytest_skip = False
try:
    from monitor_db_loader.core.crm_db_tasks import district_spatial_sql
    from monitor_db_loader.core.crm_etl_photo_data import (
        ETL_SYNC_SOURCE,
        is_etl_sync_cfg,
        is_etl_sync_subgroup,
    )
    from monitor_db_loader.core.crm_ui_constants import (
        AI_PHOTO_SUBGROUP,
        LENS_PHOTO_SUBGROUP,
    )
except Exception:
    pytest_skip = True


@unittest.skipIf(pytest_skip, "QGIS monitor_db_loader not importable")
class CrmEtlPhotoDataTests(unittest.TestCase):
    def test_is_etl_sync_subgroup(self) -> None:
        self.assertTrue(is_etl_sync_subgroup(AI_PHOTO_SUBGROUP))
        self.assertTrue(is_etl_sync_subgroup(LENS_PHOTO_SUBGROUP))

    def test_is_etl_sync_cfg(self) -> None:
        self.assertTrue(
            is_etl_sync_cfg({"name": AI_PHOTO_SUBGROUP, "source": ETL_SYNC_SOURCE})
        )
        self.assertFalse(is_etl_sync_cfg({"name": AI_PHOTO_SUBGROUP, "layers": []}))

    def test_point_spatial_uses_contains(self) -> None:
        sql = district_spatial_sql("point", "geom", 32637)
        self.assertIn("ST_Contains", sql)

    def test_line_spatial_uses_intersects(self) -> None:
        sql = district_spatial_sql("line", "geom", 32637)
        self.assertIn("ST_Intersects", sql)


if __name__ == "__main__":
    unittest.main()

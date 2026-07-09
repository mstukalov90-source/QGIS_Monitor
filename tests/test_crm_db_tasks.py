"""Tests for DB-only CRM task loader helpers."""

from __future__ import annotations

import unittest

pytest_skip = False
try:
    from monitor_db_loader.core.crm_db_tasks import (
        DB_SYNC_SOURCES,
        business_id_sql_expr,
        date_filter_sql,
        district_spatial_sql,
        is_db_loaded_subgroup,
        is_deferred_subgroup,
    )
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
class CrmDbTasksTests(unittest.TestCase):
    def test_is_deferred_subgroup(self) -> None:
        self.assertTrue(is_deferred_subgroup({"source": "field_data"}))
        self.assertTrue(is_deferred_subgroup({"source": "office_data"}))
        self.assertFalse(is_deferred_subgroup({"source": "db_tasks"}))

    def test_is_db_loaded_subgroup(self) -> None:
        store_cfg = {
            "subgroups": {
                "Ордера ОАТИ": {
                    "task_column": "oati_id",
                    "source_field": "id",
                    "scoped_geometry_id": True,
                }
            }
        }
        sub_cfg = {
            "name": "Ордера ОАТИ",
            "source": "db_tasks",
            "groups": ["Ордера ОАТИ"],
        }
        self.assertTrue(is_db_loaded_subgroup(sub_cfg, store_cfg, "Ордера ОАТИ"))
        self.assertFalse(
            is_db_loaded_subgroup(
                {"source": "field_data"}, store_cfg, "Полевые данные"
            )
        )

    def test_business_id_sql_expr_scoped(self) -> None:
        expr = business_id_sql_expr("id", "point", scoped=True)
        self.assertIn("'point:'", expr)
        self.assertIn('t."id"', expr)

    def test_business_id_sql_expr_plain(self) -> None:
        expr = business_id_sql_expr("uuid", "point", scoped=False)
        self.assertNotIn("'point:'", expr)
        self.assertIn('t."uuid"', expr)

    def test_point_spatial_uses_contains(self) -> None:
        sql = district_spatial_sql("point", "geom", 32637)
        self.assertIn("ST_Contains", sql)

    def test_line_spatial_uses_intersects(self) -> None:
        sql = district_spatial_sql("line", "geom", 32637)
        self.assertIn("ST_Intersects", sql)

    def test_date_filter_sql(self) -> None:
        sql = date_filter_sql("order_date")
        self.assertIn('t."order_date"', sql)
        self.assertIn("::date", sql)

    def test_db_sync_sources(self) -> None:
        self.assertIn("etl_sync", DB_SYNC_SOURCES)
        self.assertIn("db_tasks", DB_SYNC_SOURCES)

    def test_is_etl_sync_subgroup(self) -> None:
        self.assertTrue(is_etl_sync_subgroup(AI_PHOTO_SUBGROUP))
        self.assertTrue(is_etl_sync_subgroup(LENS_PHOTO_SUBGROUP))

    def test_is_etl_sync_cfg(self) -> None:
        self.assertTrue(
            is_etl_sync_cfg({"name": AI_PHOTO_SUBGROUP, "source": ETL_SYNC_SOURCE})
        )
        self.assertFalse(is_etl_sync_cfg({"name": AI_PHOTO_SUBGROUP, "layers": []}))


if __name__ == "__main__":
    unittest.main()

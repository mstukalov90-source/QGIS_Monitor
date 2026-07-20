"""Tests for crm.tasks update guards (no QGIS runtime required)."""

from __future__ import annotations

import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STORE_PATH = _REPO_ROOT / "monitor_db_loader" / "core" / "crm_task_store.py"
_PLUGIN_ROOT = _REPO_ROOT / "monitor_db_loader"

pytest_skip = False
try:
    for pkg_name in ("monitor_db_loader", "monitor_db_loader.core"):
        if pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg_name] = pkg

    log_util = types.ModuleType("monitor_db_loader.core.log_util")
    log_util.log_info = lambda *args, **kwargs: None
    log_util.log_warning = lambda *args, **kwargs: None
    sys.modules["monitor_db_loader.core.log_util"] = log_util

    crm_ui_constants = types.ModuleType("monitor_db_loader.core.crm_ui_constants")
    crm_ui_constants.FIELD_DATA_SUBGROUP = "Полевые данные"
    crm_ui_constants.OFFICE_DATA_SUBGROUP = "Задачи из камерального анализа"
    sys.modules["monitor_db_loader.core.crm_ui_constants"] = crm_ui_constants

    db_mod = types.ModuleType("monitor_db_loader.core.db")

    class _DatabaseConnection:  # noqa: D101
        pass

    class _CrmSessionCache:  # noqa: D101
        pass

    db_mod.DatabaseConnection = _DatabaseConnection
    db_mod.CrmSessionCache = _CrmSessionCache
    sys.modules["monitor_db_loader.core.db"] = db_mod

    spec = importlib.util.spec_from_file_location(
        "monitor_db_loader.core.crm_task_store",
        _STORE_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError("crm_task_store spec")
    store = importlib.util.module_from_spec(spec)
    sys.modules["monitor_db_loader.core.crm_task_store"] = store
    spec.loader.exec_module(store)

    CRM_GROUP_DISRUPTIONS = store.CRM_GROUP_DISRUPTIONS
    CRM_GROUP_ORDERS = store.CRM_GROUP_ORDERS
    TASK_ID_COLUMNS = store.TASK_ID_COLUMNS
    TaskRecord = store.TaskRecord
    is_monitor_owned_task = store.is_monitor_owned_task
    merge_task_id_values = store.merge_task_id_values
    task_form_field_groups = store.task_form_field_groups
    validate_monitor_owned_task_update = store.validate_monitor_owned_task_update
except Exception:
    pytest_skip = True


@unittest.skipIf(pytest_skip, "monitor_db_loader not importable")
class CrmTaskStoreGuardsTests(unittest.TestCase):
    def test_is_monitor_owned_task(self) -> None:
        self.assertTrue(is_monitor_owned_task(["etl", "2026-01-01T00:00:00+00:00"]))
        self.assertTrue(is_monitor_owned_task(["ETL", "2026-01-01T00:00:00+00:00"]))
        self.assertTrue(
            is_monitor_owned_task(["manager", "etl", "2026-01-01T00:00:00+00:00"])
        )
        self.assertFalse(is_monitor_owned_task(["manager", "2026-01-01T00:00:00+00:00"]))
        self.assertFalse(is_monitor_owned_task(None))
        self.assertFalse(is_monitor_owned_task([]))

    def test_merge_task_id_values_keeps_existing(self) -> None:
        existing = {
            "photo_uuid": None,
            "photo_lens": None,
            "ogh_id": None,
            "oati_id": None,
            "earthwork_id": "point:123",
            "localwork_id": None,
            "avr_mos_id": None,
        }
        proposed = {
            "photo_uuid": None,
            "photo_lens": None,
            "ogh_id": None,
            "oati_id": None,
            "earthwork_id": None,
            "localwork_id": None,
            "avr_mos_id": None,
        }
        merged = merge_task_id_values(existing, proposed)
        self.assertEqual(merged["earthwork_id"], "point:123")

    def test_merge_task_id_values_accepts_new_value(self) -> None:
        existing = {"earthwork_id": "point:1"}
        proposed = {"earthwork_id": "point:2"}
        merged = merge_task_id_values(existing, proposed)
        self.assertEqual(merged["earthwork_id"], "point:2")

    def test_validate_monitor_owned_rejects_clearing_id(self) -> None:
        existing = TaskRecord(
            key="00000000-0000-0000-0000-000000000001",
            type=CRM_GROUP_ORDERS,
            earthwork_id="point:99",
        )
        merged = {col: None for col in TASK_ID_COLUMNS}
        with self.assertRaises(ValueError) as ctx:
            validate_monitor_owned_task_update(
                existing, merged, CRM_GROUP_ORDERS
            )
        self.assertIn("очистить", str(ctx.exception).lower())

    def test_validate_monitor_owned_rejects_changing_id(self) -> None:
        existing = TaskRecord(
            key="00000000-0000-0000-0000-000000000001",
            type=CRM_GROUP_ORDERS,
            oati_id="point:10",
        )
        merged = {col: None for col in TASK_ID_COLUMNS}
        merged["oati_id"] = "point:99"
        with self.assertRaises(ValueError) as ctx:
            validate_monitor_owned_task_update(
                existing, merged, CRM_GROUP_ORDERS
            )
        self.assertIn("изменить", str(ctx.exception).lower())

    def test_validate_monitor_owned_allows_filling_empty_id(self) -> None:
        existing = TaskRecord(
            key="00000000-0000-0000-0000-000000000001",
            type=CRM_GROUP_DISRUPTIONS,
            earthwork_id="point:99",
        )
        merged = {col: None for col in TASK_ID_COLUMNS}
        merged["earthwork_id"] = "point:99"
        merged["photo_uuid"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        validate_monitor_owned_task_update(
            existing, merged, CRM_GROUP_DISRUPTIONS
        )

    def test_validate_monitor_owned_rejects_type_change(self) -> None:
        existing = TaskRecord(
            key="00000000-0000-0000-0000-000000000001",
            type=CRM_GROUP_ORDERS,
            earthwork_id="point:99",
        )
        merged = merge_task_id_values(
            {"earthwork_id": "point:99"},
            {"earthwork_id": "point:99"},
        )
        with self.assertRaises(ValueError) as ctx:
            validate_monitor_owned_task_update(existing, merged, "Разрытия")
        self.assertIn("тип", str(ctx.exception).lower())

    def test_validate_monitor_owned_allows_station_only_context(self) -> None:
        existing = TaskRecord(
            key="00000000-0000-0000-0000-000000000001",
            type=CRM_GROUP_ORDERS,
            earthwork_id="point:99",
        )
        merged = merge_task_id_values(
            {"earthwork_id": "point:99"},
            {"earthwork_id": "point:99"},
        )
        validate_monitor_owned_task_update(existing, merged, CRM_GROUP_ORDERS)

    def test_task_form_field_groups_orders_etl_locks_ids(self) -> None:
        record = TaskRecord(
            key="00000000-0000-0000-0000-000000000001",
            type=CRM_GROUP_ORDERS,
            earthwork_id="point:1",
        )
        readonly, link = task_form_field_groups(
            CRM_GROUP_ORDERS,
            "Уведомления на земляные работы",
            {"subgroups": {}},
            record,
            monitor_owned=True,
        )
        self.assertEqual(link, [])
        for col in TASK_ID_COLUMNS:
            self.assertIn(col, readonly)
        self.assertIn("type", readonly)

    def test_task_form_field_groups_disruptions_keeps_link_editable(self) -> None:
        record = TaskRecord(
            key="00000000-0000-0000-0000-000000000001",
            type=CRM_GROUP_DISRUPTIONS,
        )
        readonly, link = task_form_field_groups(
            CRM_GROUP_DISRUPTIONS,
            "Разрытия",
            {"subgroups": {}},
            record,
            monitor_owned=True,
        )
        self.assertIn("earthwork_id", link)
        self.assertNotIn("earthwork_id", readonly)
        self.assertIn("oati_id", link)


class CrmTasksNoDeleteTests(unittest.TestCase):
    def test_plugin_sources_have_no_delete_from_crm_tasks(self) -> None:
        pattern = re.compile(
            r"DELETE\s+FROM\s+crm\.tasks\b",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for path in _PLUGIN_ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
        self.assertEqual(
            offenders,
            [],
            f"Found DELETE FROM crm.tasks in: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Prefill полей сопоставления при создании office-задачи из ордера."""

from typing import Any, Dict, Optional

from .crm_task_store import TASK_ID_COLUMNS, TaskRecord, _normalize_id_value
from .crm_ui_constants import (
    AVR_SUBGROUP,
    EARTHWORK_SUBGROUP,
    FIELD_DATA_SUBGROUP,
    LOCAL_REPAIR_SUBGROUP,
    OATI_ORDERS_SUBGROUP,
    OFFICE_DATA_SUBGROUP,
)

_SUBGROUP_LINK_PREFILL = {
    OATI_ORDERS_SUBGROUP: ("oati_id", "order_number"),
    EARTHWORK_SUBGROUP: ("earthwork_id", "registration_number_notifications"),
    LOCAL_REPAIR_SUBGROUP: ("localwork_id", "global_id"),
    AVR_SUBGROUP: ("avr_mos_id", "em_call_reg_num"),
}


def office_task_link_prefill_from_record(
    record: TaskRecord,
) -> Optional[Dict[str, str]]:
    """Все непустые link-поля исходной задачи для новой office-точки."""
    prefill: Dict[str, str] = {}
    for col in TASK_ID_COLUMNS:
        value = _normalize_id_value(getattr(record, col, None))
        if value:
            prefill[col] = value
    return prefill or None


def office_task_link_prefill(
    subgroup_name: str,
    attributes: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    mapping = _SUBGROUP_LINK_PREFILL.get(subgroup_name)
    if not mapping:
        return None
    column, source_field = mapping
    value = attributes.get(source_field)
    if value is None or str(value).strip() == "":
        return None
    return {column: str(value).strip()}


def can_add_office_point_from_task(
    *,
    office_working: bool,
    task_source: str,
    subgroup_name: str,
    record: TaskRecord,
) -> bool:
    if not office_working or task_source != "active":
        return False
    if record.is_office_task or subgroup_name == OFFICE_DATA_SUBGROUP:
        return False
    if record.is_field_data or subgroup_name == FIELD_DATA_SUBGROUP:
        return False
    return True

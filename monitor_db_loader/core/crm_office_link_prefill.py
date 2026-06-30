# -*- coding: utf-8 -*-
"""Prefill полей сопоставления при создании office-задачи из ордера."""

from typing import Any, Dict, Optional

from .crm_ui_constants import (
    AVR_SUBGROUP,
    EARTHWORK_SUBGROUP,
    LOCAL_REPAIR_SUBGROUP,
    OATI_ORDERS_SUBGROUP,
)

_SUBGROUP_LINK_PREFILL = {
    OATI_ORDERS_SUBGROUP: ("oati_id", "order_number"),
    EARTHWORK_SUBGROUP: ("earthwork_id", "registration_number_notifications"),
    LOCAL_REPAIR_SUBGROUP: ("localwork_id", "global_id"),
    AVR_SUBGROUP: ("avr_mos_id", "em_call_reg_num"),
}


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

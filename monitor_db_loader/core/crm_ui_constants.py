# -*- coding: utf-8 -*-
"""Константы UI CRM — порт MONITOR_WEBCRM/frontend/src/types.ts."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from qgis.PyQt.QtCore import QDate, QDateTime

TaskSource = Literal[
    "active",
    "field",
    "done_legal",
    "done_illegal",
    "clear",
    "area_free",
    "area_wip",
    "area_done",
]

AreaStatus = Literal["free", "wip", "done"]

TASK_SOURCES: Tuple[TaskSource, ...] = (
    "active",
    "field",
    "done_legal",
    "done_illegal",
    "clear",
    "area_free",
    "area_wip",
    "area_done",
)

TASK_SOURCE_LABELS: Dict[TaskSource, str] = {
    "active": "Активные",
    "field": "В поле",
    "done_legal": "Закрыты легальные",
    "done_illegal": "Закрыты нелегальные",
    "clear": "Разрытие отсутствует",
    "area_free": "Площадные — свободные",
    "area_wip": "Площадные — на обследовании",
    "area_done": "Площадные — завершённые",
}

SNAPSHOT_SOURCES: Dict[str, Tuple[str, str]] = {
    "field": ("field_table", "tasks_field"),
    "done_legal": ("done_legal_table", "tasks_done_legal"),
    "done_illegal": ("done_illegal_table", "tasks_done_illegal"),
    "clear": ("clear_table", "tasks_clear"),
}

AI_PHOTO_SUBGROUP = "Фото после обработки ИИ"
AI_PHOTO_LAYER_KEY = "фотографии_после_обработки_ии"
LENS_PHOTO_SUBGROUP = "Фото разрытий и строек"
OGH_DISRUPTION_SUBGROUP = "Разрытия из полигонов ОГХ"
OATI_ORDERS_SUBGROUP = "Ордера ОАТИ"
EARTHWORK_SUBGROUP = "Уведомления на земляные работы"
AVR_SUBGROUP = "Аварийно-восстановительные работы"
LOCAL_REPAIR_SUBGROUP = "Текущие локальные ремонты"

AREA_LAYER_KEY = "tasks_area"
AREA_LAYER_NAME = "Площадные заказы"
AREA_GROUP_NAME = "Площадные заказы"

AREA_STATUS_LABELS = {
    "free": "Свободные",
    "wip": "На обследовании",
    "done": "Завершённые",
}


@dataclass(frozen=True)
class TaskTableColumn:
    field: str
    label: str
    format: Optional[str] = None


TASK_TABLE_COLUMNS: Dict[str, List[TaskTableColumn]] = {
    AI_PHOTO_SUBGROUP: [
        TaskTableColumn("azimuth_deg", "Угол камеры"),
        TaskTableColumn("date", "Дата съёмки", "date"),
    ],
    LENS_PHOTO_SUBGROUP: [
        TaskTableColumn("comment", "Комментарий"),
        TaskTableColumn("created_at", "Дата съёмки", "date"),
    ],
    OGH_DISRUPTION_SUBGROUP: [
        TaskTableColumn("loaded_at", "Дата загрузки", "date"),
    ],
    OATI_ORDERS_SUBGROUP: [
        TaskTableColumn("customer_construction", "Заказчик"),
        TaskTableColumn("order_number", "Номер ордера"),
    ],
    EARTHWORK_SUBGROUP: [
        TaskTableColumn("executor", "Заказчик"),
        TaskTableColumn(
            "registration_number_notifications", "Номер уведомления"
        ),
    ],
    AVR_SUBGROUP: [
        TaskTableColumn("balanceholder", "Заказчик"),
        TaskTableColumn("lead_of_work", "Исполнитель"),
        TaskTableColumn("em_call_reg_num", "Номер аварийного вызова"),
    ],
    LOCAL_REPAIR_SUBGROUP: [
        TaskTableColumn("customer", "Заказчик"),
        TaskTableColumn("global_id", "Номер data.mos"),
    ],
}

AREA_TASK_TABLE_COLUMNS: List[TaskTableColumn] = [
    TaskTableColumn("fid", "FID заказа"),
    TaskTableColumn("area", "Площадь"),
    TaskTableColumn("date_survey", "Дата обследования", "date"),
]

FIELD_OBSERVED_COLUMN = TaskTableColumn(
    "field_observed", "Обследовано в поле", "field_observed"
)


def normalize_rayon_name(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def format_area_order_label(feat) -> str:
    attrs = getattr(feat, "attributes", {}) or {}
    fid = attrs.get("fid")
    if fid is not None and str(fid).strip():
        return str(fid)
    task_key = getattr(feat, "task_key", None) or attrs.get("key")
    if task_key:
        text = str(task_key)
        return text[:8] + "…" if len(text) > 8 else text
    return getattr(feat, "layer_name", "") or AREA_LAYER_NAME

LEGAL_STATION_FIELDS = ("sps", "station_avr")
LEGAL_LINK_EXCLUDED_INDEX = 2


def is_area_source(source: str) -> bool:
    return source.startswith("area_")


def area_status_from_source(source: str) -> Optional[AreaStatus]:
    if source == "area_free":
        return "free"
    if source == "area_wip":
        return "wip"
    if source == "area_done":
        return "done"
    return None


def task_execute_button_label(task_source: str) -> str:
    return "Исполнить задачу" if task_source == "active" else "Просмотр задачи"


def is_ai_photo_context(subgroup_name: str, layer_key: Optional[str] = None) -> bool:
    return (
        subgroup_name == AI_PHOTO_SUBGROUP
        or layer_key == AI_PHOTO_LAYER_KEY
    )


def ai_photo_uuid_from_attributes(attributes: Dict[str, Any]) -> Optional[str]:
    value = attributes.get("uuid")
    if value is None:
        return None
    uuid = str(value).strip()
    return uuid or None


def _parse_date_value(value: Any) -> Optional[QDate]:
    if value is None or value == "":
        return None
    if isinstance(value, QDate):
        return value if value.isValid() else None
    if isinstance(value, QDateTime):
        return value.date() if value.isValid() else None
    if isinstance(value, datetime):
        return QDate(value.year, value.month, value.day)
    if isinstance(value, date):
        return QDate(value.year, value.month, value.day)
    text = str(value).strip()
    for fmt in ("dd.MM.yyyy", "yyyy-MM-dd", "dd.MM.yyyy HH:mm:ss"):
        parsed = QDate.fromString(text[: len(fmt.replace(" ", "0"))], fmt)
        if parsed.isValid():
            return parsed
    parsed = QDate.fromString(text[:10], "yyyy-MM-dd")
    if parsed.isValid():
        return parsed
    return None


def format_field_observed(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    text = str(value).strip().lower()
    if text in ("true", "t", "1", "yes", "да"):
        return "Да"
    if text in ("false", "f", "0", "no", "нет"):
        return "Нет"
    return str(value)


def format_task_table_cell(value: Any, fmt: Optional[str] = None) -> str:
    if fmt == "field_observed":
        return format_field_observed(value)
    if value is None or value == "":
        return ""
    if fmt == "date":
        parsed = _parse_date_value(value)
        if parsed and parsed.isValid():
            return parsed.toString("dd.MM.yyyy")
    if fmt == "datetime":
        text = str(value).strip()
        parsed = _parse_date_value(value)
        if parsed and parsed.isValid():
            return parsed.toString("dd.MM.yyyy HH:mm")
        return text
    return str(value)


def task_table_columns_for_subgroup(
    subgroup_name: Optional[str],
    is_area: bool = False,
) -> Optional[List[TaskTableColumn]]:
    if is_area:
        return AREA_TASK_TABLE_COLUMNS
    if not subgroup_name:
        return None
    return TASK_TABLE_COLUMNS.get(subgroup_name)


def resolve_task_table_columns(
    subgroup_name: Optional[str],
    is_area: bool,
    feature_attributes_list: List[Dict[str, Any]],
    show_sent_at: bool,
) -> List[TaskTableColumn]:
    configured = task_table_columns_for_subgroup(subgroup_name, is_area)
    if configured:
        cols = list(configured)
    else:
        names: set = set()
        for attrs in feature_attributes_list:
            for key in attrs:
                if not str(key).startswith("_"):
                    names.add(key)
        limit = 5 if show_sent_at else 6
        cols = [
            TaskTableColumn(field=f, label=f)
            for f in sorted(names)[:limit]
        ]

    if not is_area and not any(c.field == "field_observed" for c in cols):
        cols = [FIELD_OBSERVED_COLUMN] + cols
    return cols


def get_legal_link_fields(link_fields: List[str]) -> List[str]:
    return [
        f for i, f in enumerate(link_fields) if i != LEGAL_LINK_EXCLUDED_INDEX
    ]

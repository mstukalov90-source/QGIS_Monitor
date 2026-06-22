# -*- coding: utf-8 -*-
"""QSS-тема CRM в стиле MONITOR_WEBCRM."""

from typing import Optional

# Design tokens from MONITOR_WEBCRM/frontend/src/App.css
COLOR_TEXT = "#1a1a1a"
COLOR_MUTED = "#666666"
COLOR_BORDER = "#dddddd"
COLOR_BORDER_LIGHT = "#eeeeee"
COLOR_HEADER_BG = "#f8f9fa"
COLOR_PRIMARY = "#1976d2"
COLOR_PRIMARY_HOVER = "#1565c0"
COLOR_ROW_SELECTED = "#fff3e0"
COLOR_TREE_ACTIVE = "#e3f2fd"
COLOR_ERROR = "#c62828"
COLOR_SUCCESS = "#2e7d32"
COLOR_STATUS_FIELD = "#b8860b"
COLOR_STATUS_FIELD_HOVER = "#9a7209"
COLOR_STATUS_LEGAL = "#1b5e20"
COLOR_STATUS_LEGAL_HOVER = "#144a18"
COLOR_STATUS_ILLEGAL = "#b71c1c"
COLOR_STATUS_ILLEGAL_HOVER = "#9a1616"
COLOR_STATUS_CLEAR = "#2e7d32"
COLOR_STATUS_CLEAR_HOVER = "#1b5e20"
COLOR_PICK_BANNER = "#0066cc"
COLOR_FORM_MISSING_BG = "#fff8f8"
COLOR_FORM_MISSING_BORDER = "#ffcdd2"
COLOR_FORM_MISSING_INPUT = "#fff5f5"
COLOR_LEGAL_HINT_BG = "#f8fafc"
COLOR_LEGAL_HINT_BORDER = "#e2e8f0"
COLOR_CARD_SHADOW = "rgba(0, 0, 0, 0.08)"


def crm_stylesheet() -> str:
    return f"""
QDialog#crmDialog {{
    background: #ffffff;
    color: {COLOR_TEXT};
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
}}

QDialog#crmDialog QLabel#crmTitle {{
    font-size: 16px;
    font-weight: 600;
}}

QDialog#crmDialog QLabel#crmHint,
QDialog#crmDialog QLabel#crmMuted {{
    color: {COLOR_MUTED};
    font-size: 12px;
}}

QDialog#crmDialog QLabel#crmError {{
    color: {COLOR_ERROR};
    font-size: 12px;
}}

QDialog#crmDialog QLabel#crmSuccess {{
    color: {COLOR_SUCCESS};
    font-size: 12px;
}}

QDialog#crmDialog QLabel#crmPickBanner {{
    background: {COLOR_PICK_BANNER};
    color: #ffffff;
    padding: 6px 14px;
    border-radius: 4px;
    font-size: 12px;
}}

QDialog#crmDialog QLabel#crmFieldObservedYes {{
    color: {COLOR_SUCCESS};
    background: #e8f5e9;
    border: 2px solid {COLOR_SUCCESS};
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 12px;
}}

QDialog#crmDialog QLabel#crmFieldObservedNo {{
    color: {COLOR_ERROR};
    background: #ffebee;
    border: 2px solid {COLOR_ERROR};
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 12px;
}}

QDialog#crmDialog QGroupBox#crmFormSection {{
    font-weight: 600;
    font-size: 13px;
    border: 1px solid {COLOR_BORDER_LIGHT};
    border-radius: 8px;
    margin-top: 10px;
    padding: 12px 10px 10px 10px;
}}

QDialog#crmDialog QGroupBox#crmFormSection::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}

QDialog#crmDialog QGroupBox#crmFormSectionMissing {{
    background: {COLOR_FORM_MISSING_BG};
    border: 1px solid {COLOR_FORM_MISSING_BORDER};
    border-radius: 8px;
    margin-top: 10px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
}}

QDialog#crmDialog QGroupBox#crmFormSectionMissing::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}

QDialog#crmDialog QFrame#crmStatusConfirm {{
    background: {COLOR_LEGAL_HINT_BG};
    border: 1px solid #dde3ea;
    border-radius: 8px;
    padding: 8px;
}}

QDialog#crmDialog QFrame#crmLegalRequirements {{
    background: {COLOR_LEGAL_HINT_BG};
    border: 1px solid {COLOR_LEGAL_HINT_BORDER};
    border-radius: 6px;
    padding: 8px;
}}

QDialog#crmDialog QLineEdit#crmFormRowMissing,
QDialog#crmDialog QLineEdit[crmMissing="true"] {{
    border: 1px solid #e53935;
    background: {COLOR_FORM_MISSING_INPUT};
}}

QDialog#crmDialog QPushButton {{
    padding: 6px 12px;
    border: 1px solid #cccccc;
    border-radius: 4px;
    background: #ffffff;
    min-height: 24px;
}}

QDialog#crmDialog QPushButton:hover:enabled {{
    background: #f5f5f5;
}}

QDialog#crmDialog QPushButton:disabled {{
    opacity: 0.5;
}}

QDialog#crmDialog QPushButton#crmBtnPrimary {{
    background: {COLOR_PRIMARY};
    color: #ffffff;
    border-color: {COLOR_PRIMARY};
}}

QDialog#crmDialog QPushButton#crmBtnPrimary:hover:enabled {{
    background: {COLOR_PRIMARY_HOVER};
}}

QDialog#crmDialog QPushButton#crmBtnGhost {{
    border-color: transparent;
    background: transparent;
}}

QDialog#crmDialog QPushButton#crmBtnGhost:hover:enabled {{
    background: #f5f5f5;
}}

QDialog#crmDialog QPushButton#crmBtnStatusField {{
    background: {COLOR_STATUS_FIELD};
    border-color: #8b6914;
    color: #ffffff;
}}

QDialog#crmDialog QPushButton#crmBtnStatusField:hover:enabled {{
    background: {COLOR_STATUS_FIELD_HOVER};
}}

QDialog#crmDialog QPushButton#crmBtnStatusLegal {{
    background: {COLOR_STATUS_LEGAL};
    border-color: {COLOR_STATUS_LEGAL};
    color: #ffffff;
}}

QDialog#crmDialog QPushButton#crmBtnStatusLegal:hover:enabled {{
    background: {COLOR_STATUS_LEGAL_HOVER};
}}

QDialog#crmDialog QPushButton#crmBtnStatusIllegal {{
    background: {COLOR_STATUS_ILLEGAL};
    border-color: {COLOR_STATUS_ILLEGAL};
    color: #ffffff;
}}

QDialog#crmDialog QPushButton#crmBtnStatusIllegal:hover:enabled {{
    background: {COLOR_STATUS_ILLEGAL_HOVER};
}}

QDialog#crmDialog QPushButton#crmBtnStatusClear {{
    background: {COLOR_STATUS_CLEAR};
    border-color: {COLOR_STATUS_CLEAR};
    color: #ffffff;
}}

QDialog#crmDialog QPushButton#crmBtnStatusClear:hover:enabled {{
    background: {COLOR_STATUS_CLEAR_HOVER};
}}

QDialog#crmDialog QPushButton#crmSourceTab {{
    padding: 6px 12px;
    border: 1px solid #cccccc;
    border-radius: 6px;
    background: #ffffff;
    font-size: 11px;
}}

QDialog#crmDialog QPushButton#crmSourceTab:hover:enabled {{
    background: #f5f5f5;
}}

QDialog#crmDialog QPushButton#crmSourceTabActive {{
    padding: 6px 12px;
    border: 1px solid {COLOR_PRIMARY};
    border-radius: 6px;
    background: {COLOR_PRIMARY};
    color: #ffffff;
    font-size: 11px;
}}

QDialog#crmDialog QTreeWidget {{
    border: 1px solid {COLOR_BORDER_LIGHT};
    border-radius: 4px;
    font-size: 11px;
}}

QDialog#crmDialog QTreeWidget::item:selected {{
    background: {COLOR_TREE_ACTIVE};
    color: {COLOR_TEXT};
}}

QDialog#crmDialog QTableWidget {{
    border: 1px solid {COLOR_BORDER_LIGHT};
    font-size: 11px;
    gridline-color: {COLOR_BORDER_LIGHT};
}}

QDialog#crmDialog QTableWidget::item:selected {{
    background: {COLOR_ROW_SELECTED};
    color: {COLOR_TEXT};
}}

QDialog#crmDialog QHeaderView::section {{
    background: {COLOR_HEADER_BG};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER_LIGHT};
    padding: 4px 6px;
    font-size: 11px;
}}

QDialog#crmDialog QLineEdit,
QDialog#crmDialog QComboBox,
QDialog#crmDialog QListWidget {{
    padding: 6px 8px;
    border: 1px solid #cccccc;
    border-radius: 4px;
    background: #ffffff;
}}

QDialog#crmDialog QProgressBar {{
    border: none;
    border-radius: 3px;
    background: #e9ecef;
    max-height: 6px;
    text-align: center;
}}

QDialog#crmDialog QProgressBar::chunk {{
    background: #0d6efd;
    border-radius: 3px;
}}

QDialog#crmDistrictCard {{
    background: #ffffff;
    border-radius: 12px;
}}
"""


def apply_crm_theme(widget, *, object_name: str = "crmDialog") -> None:
    """Apply CRM stylesheet to dialog root."""
    widget.setObjectName(object_name)
    widget.setStyleSheet(crm_stylesheet())


def style_button(btn, style_id: str) -> None:
    btn.setObjectName(style_id)


def style_source_tab(btn, active: bool) -> None:
    btn.setObjectName("crmSourceTabActive" if active else "crmSourceTab")


def style_field_observed_label(label, value: Optional[bool]) -> None:
    if value is True:
        label.setObjectName("crmFieldObservedYes")
    elif value is False:
        label.setObjectName("crmFieldObservedNo")
    else:
        label.setObjectName("crmMuted")
    label.style().unpolish(label)
    label.style().polish(label)

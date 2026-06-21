# -*- coding: utf-8 -*-
"""Password input dialog for database connection."""

from typing import Optional

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..core.qt_compat import (
    BTN_CANCEL,
    BTN_OK,
    DIALOG_ACCEPTED,
    ECHO_PASSWORD,
    TEXT_FORMAT_RICH,
    dialog_exec,
)


class PasswordDialog(QDialog):
    def __init__(self, connection_name: str, parent=None, *, crm_theme: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Monitor DB Loader — пароль")
        self.setModal(True)
        if crm_theme:
            from .crm_theme import apply_crm_theme
            apply_crm_theme(self)

        layout = QVBoxLayout(self)
        hint = QLabel(
            f"Введите пароль для подключения:<br><b>{connection_name}</b>"
        )
        hint.setTextFormat(TEXT_FORMAT_RICH)
        layout.addWidget(hint)

        form = QFormLayout()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(ECHO_PASSWORD)
        self.password_edit.setPlaceholderText("Пароль")
        form.addRow("Пароль:", self.password_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(BTN_OK | BTN_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.password_edit.returnPressed.connect(self.accept)

    def get_password(self) -> str:
        return self.password_edit.text()

    @staticmethod
    def ask(connection_name: str, parent=None, *, crm_theme: bool = False) -> Optional[str]:
        dlg = PasswordDialog(connection_name, parent, crm_theme=crm_theme)
        if dialog_exec(dlg) != DIALOG_ACCEPTED:
            return None
        return dlg.get_password()

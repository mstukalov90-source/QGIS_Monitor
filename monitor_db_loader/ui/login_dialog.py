# -*- coding: utf-8 -*-
"""Диалог входа пользователя CRM."""

from typing import Optional, Tuple

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
    dialog_exec,
)
from .crm_theme import apply_crm_theme


class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Monitor DB Loader — вход")
        self.setModal(True)
        apply_crm_theme(self, object_name="crmDistrictCard")

        layout = QVBoxLayout(self)

        title_label = QLabel("Monitor CRM")
        title_label.setObjectName("crmTitle")
        layout.addWidget(title_label)

        hint = QLabel("Введите логин и пароль для доступа к данным")
        hint.setObjectName("crmHint")
        layout.addWidget(hint)

        form = QFormLayout()
        self.login_edit = QLineEdit()
        self.login_edit.setPlaceholderText("Логин")
        form.addRow("Логин:", self.login_edit)

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
        self.login_edit.returnPressed.connect(self.password_edit.setFocus)

    def credentials(self) -> Tuple[str, str]:
        return self.login_edit.text().strip(), self.password_edit.text()

    @staticmethod
    def ask(parent=None) -> Optional[Tuple[str, str]]:
        dlg = LoginDialog(parent)
        if dialog_exec(dlg) != DIALOG_ACCEPTED:
            return None
        login, password = dlg.credentials()
        if not login:
            return None
        return login, password

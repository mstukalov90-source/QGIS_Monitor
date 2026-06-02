# -*- coding: utf-8 -*-
"""Qt5 (QGIS 3.x) / Qt6 (QGIS 4.x) compatibility shims."""

from qgis.PyQt.QtCore import QT_VERSION, Qt
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLineEdit, QMessageBox

IS_QT6 = QT_VERSION >= 0x060000

# QLabel text format
if IS_QT6:
    TEXT_FORMAT_RICH = Qt.TextFormat.RichText
else:
    TEXT_FORMAT_RICH = Qt.RichText  # type: ignore[attr-defined]

# Pen styles (symbology)
if IS_QT6:
    PEN_NONE = Qt.PenStyle.NoPen
    PEN_DASH = Qt.PenStyle.DashLine
    PEN_SOLID = Qt.PenStyle.SolidLine
else:
    PEN_NONE = Qt.NoPen  # type: ignore[attr-defined]
    PEN_DASH = Qt.DashLine  # type: ignore[attr-defined]
    PEN_SOLID = Qt.SolidLine  # type: ignore[attr-defined]

# QLineEdit echo mode
if IS_QT6:
    ECHO_PASSWORD = QLineEdit.EchoMode.Password
else:
    ECHO_PASSWORD = QLineEdit.Password  # type: ignore[attr-defined]

# QDialog result codes
if IS_QT6:
    DIALOG_ACCEPTED = QDialog.DialogCode.Accepted
else:
    DIALOG_ACCEPTED = QDialog.Accepted  # type: ignore[attr-defined]

# QDialogButtonBox standard buttons
if IS_QT6:
    BTN_OK = QDialogButtonBox.StandardButton.Ok
    BTN_CANCEL = QDialogButtonBox.StandardButton.Cancel
else:
    BTN_OK = QDialogButtonBox.Ok  # type: ignore[attr-defined]
    BTN_CANCEL = QDialogButtonBox.Cancel  # type: ignore[attr-defined]

# QMessageBox standard buttons
if IS_QT6:
    MSGBOX_RETRY = QMessageBox.StandardButton.Retry
    MSGBOX_CANCEL = QMessageBox.StandardButton.Cancel
else:
    MSGBOX_RETRY = QMessageBox.Retry  # type: ignore[attr-defined]
    MSGBOX_CANCEL = QMessageBox.Cancel  # type: ignore[attr-defined]


def dialog_exec(dialog) -> int:
    """Run modal dialog (QGIS 3.44 / Qt5: exec_; QGIS 4 / Qt6: exec)."""
    if IS_QT6:
        return dialog.exec()
    return dialog.exec_()

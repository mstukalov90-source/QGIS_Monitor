# -*- coding: utf-8 -*-
"""Qt5 (QGIS 3.x) / Qt6 (QGIS 4.x) compatibility shims."""

from qgis.PyQt.QtCore import QT_VERSION, Qt, QVariant
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLineEdit, QMessageBox

try:
    from qgis.PyQt.QtCore import QMetaType

    _QVARIANT_TO_METATYPE = {
        QVariant.String: QMetaType.Type.QString,
        QVariant.Int: QMetaType.Type.Int,
        QVariant.LongLong: QMetaType.Type.LongLong,
        QVariant.Double: QMetaType.Type.Double,
        QVariant.Bool: QMetaType.Type.Bool,
    }
except (ImportError, AttributeError):
    QMetaType = None  # type: ignore[misc, assignment]
    _QVARIANT_TO_METATYPE = {}

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


def show_modeless_dialog(dialog) -> None:
    """Показать диалог без блокировки остального интерфейса QGIS."""
    dialog.setModal(False)
    if IS_QT6:
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        window_flag = Qt.WindowType.Window
    else:
        dialog.setWindowModality(Qt.NonModal)  # type: ignore[attr-defined]
        window_flag = Qt.Window  # type: ignore[attr-defined]
    flags = dialog.windowFlags()
    dialog.setWindowFlags(flags | window_flag)
    QDialog.show(dialog)
    dialog.raise_()
    dialog.activateWindow()


def register_modeless_dialog(iface, dialog) -> None:
    """Держать ссылку на немодальный диалог, чтобы его не собрал GC."""
    if iface is None:
        return
    dialogs = getattr(iface, "_monitor_db_loader_task_dialogs", None)
    if dialogs is None:
        dialogs = []
        iface._monitor_db_loader_task_dialogs = dialogs
    dialogs.append(dialog)

    def _unregister(_obj=None) -> None:
        if dialog in dialogs:
            dialogs.remove(dialog)

    dialog.destroyed.connect(_unregister)


def qgs_field(name: str, field_type: int):
    """QgsField без DeprecationWarning (QGIS 3.38+: setMetaType вместо setType/конструктора)."""
    from qgis.core import QgsField

    field = QgsField()
    field.setName(name)
    meta_type = _QVARIANT_TO_METATYPE.get(field_type, field_type)
    if hasattr(field, "setMetaType"):
        field.setMetaType(int(meta_type))
    elif hasattr(field, "setType"):
        field.setType(field_type)
    return field

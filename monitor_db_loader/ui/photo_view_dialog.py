# -*- coding: utf-8 -*-
"""Диалог просмотра фотографии ИИ."""

from typing import Any, Dict, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.ai_photo import (
    AiPhotoMeta,
    PhotoFetchError,
    fetch_photo_bytes,
    resolve_ai_photo,
)
from ..core.config import photo_view
from ..core.db import DatabaseConnection
from .crm_theme import apply_crm_theme


class PhotoViewDialog(QDialog):
    def __init__(
        self,
        uuid: str,
        conn: DatabaseConnection,
        config: Dict[str, Any],
        parent=None,
    ):
        super().__init__(parent)
        self._uuid = uuid
        self._conn = conn
        self._config = config
        self._meta: Optional[AiPhotoMeta] = None

        self.setWindowTitle("Просмотр фотографии")
        self.setModal(True)
        self.resize(720, 560)
        apply_crm_theme(self)

        outer = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("<b>Просмотр фотографии</b>")
        title.setObjectName("crmTitle")
        header.addWidget(title)
        header.addStretch()
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.reject)
        header.addWidget(close_btn)
        outer.addLayout(header)

        self._status_label = QLabel("Загрузка…")
        self._status_label.setObjectName("crmMuted")
        outer.addWidget(self._status_label)

        self._meta_label = QLabel("")
        self._meta_label.setObjectName("crmMuted")
        self._meta_label.setWordWrap(True)
        self._meta_label.hide()
        outer.addWidget(self._meta_label)

        self._uuid_label = QLabel("")
        self._uuid_label.setObjectName("crmMuted")
        self._uuid_label.hide()
        outer.addWidget(self._uuid_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignCenter)
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignCenter)
        self._scroll.setWidget(self._image_label)
        self._scroll.hide()
        outer.addWidget(self._scroll, stretch=1)

        self._load_photo()

    def _set_error(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setObjectName("crmError")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _format_meta_line(self, meta: AiPhotoMeta) -> str:
        parts = []
        if meta.date:
            parts.append(f"Дата: {meta.date}")
        if meta.order_id:
            parts.append(f"Ордер: {meta.order_id}")
        if meta.image_name:
            parts.append(meta.image_name)
        return " · ".join(parts)

    def _load_photo(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            meta = resolve_ai_photo(self._conn, self._uuid)
            if meta is None:
                self._set_error("Фото не найдено в genplan.photo_meta")
                return

            self._meta = meta
            photo_cfg = photo_view(self._config)
            try:
                content, _media_type = fetch_photo_bytes(meta, photo_cfg)
            except PhotoFetchError as exc:
                self._set_error(str(exc))
                return

            pixmap = QPixmap()
            if not pixmap.loadFromData(content):
                self._set_error("Не удалось загрузить изображение")
                return

            self._status_label.hide()
            meta_line = self._format_meta_line(meta)
            if meta_line:
                self._meta_label.setText(meta_line)
                self._meta_label.show()
            self._uuid_label.setText(f"UUID: {meta.uuid}")
            self._uuid_label.show()

            max_w = max(self.width() - 48, 400)
            if pixmap.width() > max_w:
                pixmap = pixmap.scaledToWidth(
                    max_w, Qt.SmoothTransformation
                )
            self._image_label.setPixmap(pixmap)
            self._scroll.show()
        finally:
            QApplication.restoreOverrideCursor()

    @staticmethod
    def open(
        uuid: str,
        conn: DatabaseConnection,
        config: Dict[str, Any],
        parent=None,
    ) -> "PhotoViewDialog":
        dlg = PhotoViewDialog(uuid, conn, config, parent)
        dlg.exec_()
        return dlg

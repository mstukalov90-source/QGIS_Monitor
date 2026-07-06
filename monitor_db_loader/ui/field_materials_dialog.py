# -*- coding: utf-8 -*-
"""Диалог просмотра полевых материалов (баннер + галерея)."""

from typing import Any, Dict, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.crm_ui_constants import format_task_table_cell
from ..core.db import DatabaseConnection
from ..core.field_photo import (
    FieldPhotoFetchError,
    FieldPhotoItem,
    FieldPhotosResult,
    fetch_field_photo_bytes,
    fetch_field_photos,
)
from .crm_theme import apply_crm_theme


class FieldMaterialsDialog(QDialog):
    def __init__(
        self,
        task_key: str,
        conn: DatabaseConnection,
        config: Dict[str, Any],
        parent=None,
    ):
        super().__init__(parent)
        self._task_key = task_key
        self._conn = conn
        self._config = config
        self._result: Optional[FieldPhotosResult] = None
        self._gallery_index = 0
        self._gallery_photos: list[FieldPhotoItem] = []

        self.setWindowTitle("Просмотр полевых материалов")
        self.setModal(True)
        self.resize(760, 640)
        self.setObjectName("crmDialog")
        apply_crm_theme(self)

        outer = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("<b>Просмотр полевых материалов</b>")
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

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content.hide()
        outer.addWidget(self._content, stretch=1)

        self._load_photos()

    def _set_error(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setObjectName("crmError")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _clear_content(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _photo_meta_line(self, photo: FieldPhotoItem) -> str:
        parts = []
        if photo.label:
            parts.append(photo.label)
        if photo.created_at:
            parts.append(
                f"Дата: {format_task_table_cell(photo.created_at, 'date')}"
            )
        return " · ".join(parts)

    def _make_image_widget(self, photo: FieldPhotoItem) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)

        meta = self._photo_meta_line(photo)
        if meta:
            meta_label = QLabel(meta)
            meta_label.setObjectName("crmMuted")
            meta_label.setWordWrap(True)
            layout.addWidget(meta_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)
        scroll.setObjectName("crmPhotoScroll")
        image_label = QLabel("Загрузка изображения…")
        image_label.setAlignment(Qt.AlignCenter)
        image_label.setObjectName("crmPhotoImage")
        scroll.setWidget(image_label)
        layout.addWidget(scroll, stretch=1)

        try:
            content, _media_type = fetch_field_photo_bytes(
                photo.file_path, self._config
            )
            pixmap = QPixmap()
            if not pixmap.loadFromData(content):
                image_label.setText("Не удалось загрузить изображение")
                image_label.setObjectName("crmError")
            else:
                max_w = max(self.width() - 64, 400)
                if pixmap.width() > max_w:
                    pixmap = pixmap.scaledToWidth(
                        max_w, Qt.SmoothTransformation
                    )
                image_label.setPixmap(pixmap)
                image_label.setText("")
        except FieldPhotoFetchError as exc:
            image_label.setText(str(exc))
            image_label.setObjectName("crmError")

        return host

    def _build_comment_section(self, comment: str) -> None:
        section = QWidget()
        section.setObjectName("crmFieldMaterialsSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Комментарий полевого сотрудника")
        title.setObjectName("crmFieldMaterialsSectionTitle")
        layout.addWidget(title)

        text_edit = QPlainTextEdit()
        text_edit.setObjectName("crmFieldMaterialsComment")
        text_edit.setReadOnly(True)
        text_edit.setPlainText(comment)
        text_edit.setMaximumHeight(160)
        layout.addWidget(text_edit)

        self._content_layout.addWidget(section)

    def _build_banner_section(self, result: FieldPhotosResult) -> None:
        section = QWidget()
        section.setObjectName("crmFieldMaterialsSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Фото баннера")
        title.setObjectName("crmFieldMaterialsSectionTitle")
        layout.addWidget(title)

        banner = result.banner_photo
        if banner is None:
            missing = QLabel("Фото баннера отсутствует")
            missing.setObjectName("crmFieldMaterialsBannerMissing")
            layout.addWidget(missing)
        else:
            layout.addWidget(self._make_image_widget(banner))

        self._content_layout.addWidget(section)

    def _build_gallery_section(self, photos: list[FieldPhotoItem]) -> None:
        if not photos:
            return

        section = QWidget()
        section.setObjectName("crmFieldMaterialsSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self._gallery_title = QLabel("")
        self._gallery_title.setObjectName("crmFieldMaterialsSectionTitle")
        header.addWidget(self._gallery_title)
        header.addStretch()

        self._prev_btn = QPushButton("←")
        self._prev_btn.clicked.connect(self._show_prev_gallery_photo)
        header.addWidget(self._prev_btn)
        self._next_btn = QPushButton("→")
        self._next_btn.clicked.connect(self._show_next_gallery_photo)
        header.addWidget(self._next_btn)
        layout.addLayout(header)

        self._gallery_host = QWidget()
        self._gallery_host_layout = QVBoxLayout(self._gallery_host)
        self._gallery_host_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._gallery_host, stretch=1)

        self._content_layout.addWidget(section)
        self._update_gallery_view()

    def _update_gallery_view(self) -> None:
        photos = self._gallery_photos
        show_nav = len(photos) > 1
        self._prev_btn.setVisible(show_nav)
        self._next_btn.setVisible(show_nav)

        if not photos:
            self._gallery_title.setText("Фото")
            return

        if len(photos) == 1:
            self._gallery_title.setText("Фото")
        else:
            self._gallery_title.setText(
                f"Фото ({self._gallery_index + 1} из {len(photos)})"
            )

        while self._gallery_host_layout.count():
            item = self._gallery_host_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        current = photos[self._gallery_index]
        self._gallery_host_layout.addWidget(self._make_image_widget(current))

    def _show_prev_gallery_photo(self) -> None:
        if len(self._gallery_photos) <= 1:
            return
        self._gallery_index = (
            self._gallery_index - 1 + len(self._gallery_photos)
        ) % len(self._gallery_photos)
        self._update_gallery_view()

    def _show_next_gallery_photo(self) -> None:
        if len(self._gallery_photos) <= 1:
            return
        self._gallery_index = (self._gallery_index + 1) % len(
            self._gallery_photos
        )
        self._update_gallery_view()

    def _load_photos(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            result = fetch_field_photos(self._conn, self._task_key)
            self._result = result
            if not result.photos and not result.comment:
                self._set_error("Материалы не найдены")
                return

            self._status_label.hide()
            self._clear_content()
            if result.comment:
                self._build_comment_section(result.comment)
            self._build_banner_section(result)
            self._gallery_photos = result.gallery_photos
            self._gallery_index = 0
            self._build_gallery_section(self._gallery_photos)
            self._content.show()
        except Exception as exc:
            self._set_error(str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    @staticmethod
    def open(
        task_key: str,
        conn: DatabaseConnection,
        config: Dict[str, Any],
        parent=None,
    ) -> "FieldMaterialsDialog":
        dlg = FieldMaterialsDialog(task_key, conn, config, parent)
        dlg.exec_()
        return dlg

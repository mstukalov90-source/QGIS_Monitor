# -*- coding: utf-8 -*-
"""Полевые фото из mggt_field.reports + mggt_field.photos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import field_photo_view
from .crm_task_store import _pg_connection
from .db import DatabaseConnection
from .photo_sftp import SftpPhotoError, fetch_photo_bytes_sftp

BANNER_LABEL = "Фото баннера"


class FieldPhotoFetchError(Exception):
    pass


def _pg_recover_transaction(pg) -> None:
    if pg is None:
        return
    try:
        pg.rollback()
    except Exception:
        pass


def _pg_rollback(pg) -> None:
    if pg is None:
        return
    try:
        pg.rollback()
    except Exception:
        pass


def _format_created_at(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


@dataclass
class FieldPhotoItem:
    id: int
    file_path: str
    banner: bool
    created_at: Optional[str] = None
    photo_key: Optional[str] = None
    username: Optional[str] = None

    @property
    def label(self) -> Optional[str]:
        return BANNER_LABEL if self.banner else None


@dataclass
class FieldPhotosResult:
    photos: List[FieldPhotoItem]
    banner_missing: bool
    comment: Optional[str] = None

    @property
    def banner_photo(self) -> Optional[FieldPhotoItem]:
        for photo in self.photos:
            if photo.banner:
                return photo
        return None

    @property
    def gallery_photos(self) -> List[FieldPhotoItem]:
        return [photo for photo in self.photos if not photo.banner]


def _fetch_field_survey_comment(
    pg,
    task_key: str,
) -> Optional[str]:
    query = """
        SELECT comment
        FROM mggt_field.reports
        WHERE tasks_key = %s::uuid
          AND comment IS NOT NULL
          AND TRIM(comment) <> ''
        LIMIT 1
    """
    try:
        with pg.cursor() as cur:
            cur.execute(query, (task_key,))
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        text = str(row[0]).strip()
        return text or None
    except Exception:
        return None


def fetch_field_photos(
    conn: DatabaseConnection,
    task_key: str,
) -> FieldPhotosResult:
    pg = _pg_connection(conn)
    if pg is None:
        return FieldPhotosResult(photos=[], banner_missing=True, comment=None)

    comment = _fetch_field_survey_comment(pg, task_key)

    query = """
        SELECT p.id, p.file_path, p.banner, p.created_at, p.photo_key, p.username
        FROM mggt_field.reports r
        JOIN mggt_field.photos p ON p.task = r.task
        WHERE r.tasks_key = %s::uuid
          AND p.file_path IS NOT NULL
          AND TRIM(p.file_path) <> ''
        ORDER BY p.banner DESC, p.created_at ASC
    """
    photos: List[FieldPhotoItem] = []

    _pg_recover_transaction(pg)
    try:
        with pg.cursor() as cur:
            cur.execute(query, (task_key,))
            for row in cur.fetchall():
                file_path = str(row[1]).strip()
                if not file_path:
                    continue
                photos.append(
                    FieldPhotoItem(
                        id=int(row[0]),
                        file_path=Path(file_path).name,
                        banner=bool(row[2]),
                        created_at=_format_created_at(row[3]),
                        photo_key=row[4],
                        username=row[5],
                    )
                )
        pg.commit()
    except Exception:
        _pg_rollback(pg)
        raise

    has_banner = any(photo.banner for photo in photos)
    return FieldPhotosResult(
        photos=photos,
        banner_missing=not has_banner,
        comment=comment,
    )


def fetch_field_photo_bytes(
    file_name: str,
    config: Dict[str, Any],
) -> Tuple[bytes, str]:
    photo_cfg = field_photo_view(config)
    try:
        return fetch_photo_bytes_sftp(file_name, photo_cfg)
    except SftpPhotoError as exc:
        raise FieldPhotoFetchError(str(exc)) from exc

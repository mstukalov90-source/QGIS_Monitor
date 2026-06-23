# -*- coding: utf-8 -*-
"""Просмотр фото ИИ: метаданные из genplan.photo_meta, файл по HTTP или SFTP."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .db import DatabaseConnection
from .photo_sftp import SftpPhotoError, fetch_photo_bytes_sftp, sftp_configured

try:
    from psycopg2.extras import RealDictCursor
except ImportError:
    RealDictCursor = None  # type: ignore

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

DEFAULT_TIMEOUT_SEC = 60


@dataclass
class AiPhotoMeta:
    uuid: str
    image_name: str
    date: Optional[str] = None
    azimuth_deg: Optional[float] = None
    order_id: Optional[str] = None


class PhotoFetchError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_valid_uuid(value: str) -> bool:
    return bool(UUID_RE.match(value.strip()))


def resolve_ai_photo(conn: DatabaseConnection, uuid: str) -> Optional[AiPhotoMeta]:
    if not is_valid_uuid(uuid):
        return None
    pg = conn._get_pg_connection()
    if pg is None:
        return None

    query = """
        SELECT uuid, image_name, date, azimuth_deg, order_id
        FROM genplan.photo_meta
        WHERE uuid = %s
        LIMIT 1
    """
    if RealDictCursor is not None:
        with pg.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (uuid.strip(),))
            row = cur.fetchone()
    else:
        with pg.cursor() as cur:
            cur.execute(query, (uuid.strip(),))
            raw = cur.fetchone()
            if not raw:
                row = None
            else:
                row = {
                    "uuid": raw[0],
                    "image_name": raw[1],
                    "date": raw[2],
                    "azimuth_deg": raw[3],
                    "order_id": raw[4],
                }

    if not row or not row.get("image_name"):
        return None
    return AiPhotoMeta(
        uuid=str(row["uuid"]),
        image_name=str(row["image_name"]).strip(),
        date=row.get("date"),
        azimuth_deg=row.get("azimuth_deg"),
        order_id=row.get("order_id"),
    )


def _photo_view_settings(photo_cfg: Dict[str, Any]) -> Tuple[str, str, str, int]:
    base_url = str(photo_cfg.get("base_url", "")).strip().rstrip("/")
    url_mode = str(photo_cfg.get("url_mode", "auto")).strip().lower()
    api_key = str(photo_cfg.get("api_key", "")).strip()
    timeout = int(photo_cfg.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
    return base_url, url_mode, api_key, timeout


def build_photo_url(meta: AiPhotoMeta, photo_cfg: Dict[str, Any]) -> str:
    base_url, url_mode, api_key, _timeout = _photo_view_settings(photo_cfg)
    if not base_url:
        raise PhotoFetchError(404, "Не настроен photo_view.base_url в конфигурации")

    if url_mode == "api":
        path = f"/api/photos/ai/{urllib.parse.quote(meta.uuid)}/image"
        url = f"{base_url}{path}"
        if api_key:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}key={urllib.parse.quote(api_key)}"
        return url

    return f"{base_url}/{urllib.parse.quote(meta.image_name)}"


def _http_get_bytes(
    url: str,
    *,
    api_key: str = "",
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> Tuple[bytes, str]:
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            content = response.read()
            media_type = response.headers.get("Content-Type", "image/jpeg")
            if ";" in media_type:
                media_type = media_type.split(";", 1)[0].strip()
            return content, media_type or "image/jpeg"
    except urllib.error.HTTPError as exc:
        raise PhotoFetchError(
            exc.code,
            f"Ошибка загрузки фото ({url}): HTTP {exc.code}",
        ) from exc
    except urllib.error.URLError as exc:
        raise PhotoFetchError(
            502,
            f"Сервер фото недоступен ({url}): {exc.reason}",
        ) from exc


def _fetch_photo_http(meta: AiPhotoMeta, photo_cfg: Dict[str, Any]) -> Tuple[bytes, str]:
    base_url, _url_mode, api_key, timeout_sec = _photo_view_settings(photo_cfg)
    if not base_url:
        raise PhotoFetchError(404, "Не настроен photo_view.base_url в конфигурации")
    url = build_photo_url(meta, photo_cfg)
    return _http_get_bytes(url, api_key=api_key, timeout_sec=timeout_sec)


def fetch_photo_bytes(meta: AiPhotoMeta, photo_cfg: Dict[str, Any]) -> Tuple[bytes, str]:
    _base_url, url_mode, _api_key, _timeout = _photo_view_settings(photo_cfg)

    if url_mode == "sftp":
        try:
            return fetch_photo_bytes_sftp(meta.image_name, photo_cfg)
        except SftpPhotoError as exc:
            raise PhotoFetchError(502, str(exc)) from exc

    if url_mode in ("static", "api"):
        return _fetch_photo_http(meta, photo_cfg)

    # auto: HTTP, при ошибке — SFTP (если настроен)
    http_error: Optional[PhotoFetchError] = None
    if str(photo_cfg.get("base_url", "")).strip():
        try:
            return _fetch_photo_http(meta, photo_cfg)
        except PhotoFetchError as exc:
            http_error = exc

    if sftp_configured(photo_cfg):
        try:
            return fetch_photo_bytes_sftp(meta.image_name, photo_cfg)
        except SftpPhotoError as exc:
            if http_error is not None:
                raise PhotoFetchError(
                    502,
                    f"{http_error}\n\nРезервный SFTP: {exc}",
                ) from exc
            raise PhotoFetchError(502, str(exc)) from exc

    if http_error is not None:
        raise http_error

    raise PhotoFetchError(
        404,
        "Не настроена загрузка фото: укажите photo_view.base_url или photo_view.sftp_host",
    )

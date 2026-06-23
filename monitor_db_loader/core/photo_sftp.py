# -*- coding: utf-8 -*-
"""Загрузка фото с VPS по SCP/SFTP (как в MONITOR_WEBCRM)."""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class SftpPhotoError(Exception):
    pass


def _safe_image_name(image_name: str) -> Optional[str]:
    name = image_name.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if Path(name).suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        return None
    return name


def _photo_file_path(image_name: str, base_dir: Path) -> Optional[Path]:
    name = _safe_image_name(image_name)
    if not name:
        return None
    base = base_dir.resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _media_type_for_name(image_name: str) -> str:
    suffix = Path(image_name).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "image/jpeg"


def resolved_cache_dir(photo_cfg: Dict[str, Any]) -> Path:
    raw = str(photo_cfg.get("local_cache_dir", "")).strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        try:
            from qgis.core import QgsApplication

            base = Path(QgsApplication.qgisSettingsDirPath())
        except Exception:
            base = Path.home() / ".monitor_db_loader"
        path = base / "photo_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def sftp_configured(photo_cfg: Dict[str, Any]) -> bool:
    return bool(str(photo_cfg.get("sftp_host", "")).strip())


def _resolve_identity_file(photo_cfg: Dict[str, Any]) -> str:
    raw = str(photo_cfg.get("sftp_key_path", "")).strip()
    if not raw:
        return ""
    return str(Path(raw).expanduser().resolve())


def _download_via_scp(
    host: str,
    user: str,
    remote_path: str,
    target: Path,
    *,
    port: int,
    identity_file: str,
) -> None:
    remote = f"{user}@{host}:{remote_path}"
    command = [
        "scp",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=30",
        "-P",
        str(port),
    ]
    if identity_file:
        command.extend(["-i", identity_file])
    command.extend([remote, str(target)])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        if target.exists():
            target.unlink(missing_ok=True)
        stderr = (result.stderr or result.stdout or "").strip()
        raise SftpPhotoError(stderr or f"scp завершился с кодом {result.returncode}")


def fetch_photo_bytes_sftp(
    image_name: str,
    photo_cfg: Dict[str, Any],
) -> Tuple[bytes, str]:
    safe_name = _safe_image_name(image_name)
    if not safe_name:
        raise SftpPhotoError("Некорректное имя файла фото")

    cache_dir = resolved_cache_dir(photo_cfg)
    cached = _photo_file_path(safe_name, cache_dir)
    if cached is not None:
        return cached.read_bytes(), _media_type_for_name(safe_name)

    host = str(photo_cfg.get("sftp_host", "")).strip()
    if not host:
        raise SftpPhotoError("Не настроен photo_view.sftp_host")

    user = str(photo_cfg.get("sftp_user", "")).strip() or getpass.getuser()
    port = int(photo_cfg.get("sftp_port", 22))
    remote_dir = str(
        photo_cfg.get("sftp_remote_dir", "/opt/monitor/downloaded_photo")
    ).rstrip("/")
    remote_path = f"{remote_dir}/{safe_name}"
    identity_file = _resolve_identity_file(photo_cfg)
    target = cache_dir / safe_name

    _download_via_scp(
        host,
        user,
        remote_path,
        target,
        port=port,
        identity_file=identity_file,
    )

    cached = _photo_file_path(safe_name, cache_dir)
    if cached is None:
        raise SftpPhotoError("Скачанный файл не прошёл проверку")
    return cached.read_bytes(), _media_type_for_name(safe_name)

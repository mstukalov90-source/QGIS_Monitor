#!/usr/bin/env bash
# Сборка ZIP для QGIS 3.44+ и 4.x:
# Модули → Управление модулями → «Установить модуль из ZIP».
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_NAME="monitor_db_loader"
PLUGIN_DIR="${ROOT}/${PLUGIN_NAME}"
DIST_DIR="${ROOT}/dist"
ZIP_PATH="${DIST_DIR}/${PLUGIN_NAME}.zip"

if [[ ! -f "${PLUGIN_DIR}/metadata.txt" ]]; then
  echo "Ошибка: не найден ${PLUGIN_DIR}/metadata.txt" >&2
  exit 1
fi

mkdir -p "${DIST_DIR}"
rm -f "${ZIP_PATH}"

(
  cd "${ROOT}"
  zip -r "${ZIP_PATH}" "${PLUGIN_NAME}" \
    -x "${PLUGIN_NAME}/**/__pycache__/*" \
    -x "${PLUGIN_NAME}/**/*.pyc" \
    -x "${PLUGIN_NAME}/**/.DS_Store" \
    -x "${PLUGIN_NAME}/.DS_Store"
)

echo "Готово: ${ZIP_PATH}"
echo "Целевая версия: QGIS 3.44 LTR (также QGIS 4.x)"
grep -E '^(version|qgisMinimum|qgisMaximum)' "${PLUGIN_DIR}/metadata.txt" || true
unzip -l "${ZIP_PATH}" | head -22

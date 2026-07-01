#!/usr/bin/env bash
# Сборка ZIP и публикация в приватный репозиторий QGIS (plugins.xml + ZIP по HTTP).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_NAME="monitor_db_loader"
METADATA="${ROOT}/${PLUGIN_NAME}/metadata.txt"
PUBLISH_DIR="${QGIS_PLUGIN_PUBLISH_DIR:-/var/www/qgis-plugins}"
BASE_URL="${QGIS_PLUGIN_BASE_URL:-http://77.222.63.161/qgis-plugins}"

if [[ ! -f "${METADATA}" ]]; then
  echo "Ошибка: не найден ${METADATA}" >&2
  exit 1
fi

meta_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "${METADATA}" | head -1 || true)"
  echo "${line#*=}"
}

NAME="$(meta_value name)"
VERSION="$(meta_value version)"
DESCRIPTION="$(meta_value description)"
AUTHOR="$(meta_value author)"
QGIS_MIN="$(meta_value qgisMinimumVersion)"
QGIS_MAX="$(meta_value qgisMaximumVersion)"

ABOUT=""
if grep -q '^\[about\]' "${METADATA}"; then
  ABOUT="$(awk '/^\[about\]/{found=1; next} /^\[/{found=0} found && /^description=/{sub(/^description=/,""); print; exit}' "${METADATA}")"
fi
[[ -z "${ABOUT}" ]] && ABOUT="${DESCRIPTION}"

"${ROOT}/scripts/build_plugin_zip.sh"

mkdir -p "${PUBLISH_DIR}"
install -m 644 "${ROOT}/dist/${PLUGIN_NAME}.zip" "${PUBLISH_DIR}/${PLUGIN_NAME}.zip"

XML_PATH="${PUBLISH_DIR}/plugins.xml"
cat > "${XML_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<plugins>
  <pyqgis_plugin name="${NAME}" version="${VERSION}">
    <description>${DESCRIPTION}</description>
    <about>${ABOUT}</about>
    <author_name>${AUTHOR}</author_name>
    <qgis_minimum_version>${QGIS_MIN}</qgis_minimum_version>
    <qgis_maximum_version>${QGIS_MAX}</qgis_maximum_version>
    <file_name>${PLUGIN_NAME}</file_name>
    <download_url>${BASE_URL}/${PLUGIN_NAME}.zip</download_url>
    <experimental>false</experimental>
    <deprecated>false</deprecated>
  </pyqgis_plugin>
</plugins>
EOF

chmod 644 "${XML_PATH}"

echo "Опубликовано:"
echo "  ${XML_PATH}"
echo "  ${PUBLISH_DIR}/${PLUGIN_NAME}.zip"
echo "  URL: ${BASE_URL}/plugins.xml"

# -*- coding: utf-8 -*-
"""PostgreSQL connection helpers and layer URI building."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from qgis.core import QgsDataSourceUri, QgsVectorLayer

from .config import (
    geom_column_candidates,
    is_mixed_geometry,
    resolve_layer_source,
)
from .layer_utils import finalize_vector_layer
from .log_util import log_info, log_warning
from .mixed_geometry import MixedGeometryLoader, layer_load_error, sanitize_uri

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore


def _plugin_version() -> str:
    """Read plugin version from metadata.txt (fallback for audit application_name)."""
    meta_path = Path(__file__).resolve().parents[1] / "metadata.txt"
    try:
        for line in meta_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("version="):
                return line.split("=", 1)[1].strip() or "unknown"
    except OSError:
        pass
    return "unknown"


@dataclass
class CrmSessionCache:
    """In-memory CRM index loaded once per DB session."""

    snapshot_keys: Set[str] = field(default_factory=set)
    task_index: Dict[Tuple[str, str], str] = field(default_factory=dict)
    field_observed: Dict[str, Optional[bool]] = field(default_factory=dict)


class DatabaseConnection:
    """Holds connection parameters and caches geometry column names."""

    def __init__(self, db_config: Dict, password: str):
        self.host = str(db_config["host"])
        self.port = str(db_config.get("port", 5432))
        self.database = str(db_config["database"])
        self.username = str(db_config["username"])
        self.password = password
        self._geom_cache: Dict[Tuple[str, str, str], Optional[str]] = {}
        self._pg_conn = None
        self._mixed_loader = MixedGeometryLoader(self)
        self._crm_schema_ready: Set[str] = set()
        self._area_rows_by_rayon: Dict[str, List[Any]] = {}
        self._crm_session_cache: Optional[CrmSessionCache] = None
        self._district_wkt_by_rayon: Dict[str, str] = {}

    def get_crm_session_cache(self) -> Optional[CrmSessionCache]:
        return self._crm_session_cache

    def set_crm_session_cache(self, cache: CrmSessionCache) -> None:
        self._crm_session_cache = cache

    def invalidate_crm_session_cache(self) -> None:
        self._crm_session_cache = None

    def get_district_wkt_cache(self, rayon_norm: str) -> Optional[str]:
        return self._district_wkt_by_rayon.get(rayon_norm)

    def set_district_wkt_cache(self, rayon_norm: str, wkt: str) -> None:
        self._district_wkt_by_rayon[rayon_norm] = wkt

    def clear_district_wkt_cache(self, rayon_norm: Optional[str] = None) -> None:
        if rayon_norm is None:
            self._district_wkt_by_rayon.clear()
        else:
            self._district_wkt_by_rayon.pop(rayon_norm, None)

    def get_area_rows_cache(self, rayon_norm: str) -> Optional[List[Any]]:
        return self._area_rows_by_rayon.get(rayon_norm)

    def set_area_rows_cache(self, rayon_norm: str, rows: List[Any]) -> None:
        self._area_rows_by_rayon[rayon_norm] = rows

    def clear_area_rows_cache(self, rayon_norm: Optional[str] = None) -> None:
        if rayon_norm is None:
            self._area_rows_by_rayon.clear()
        else:
            self._area_rows_by_rayon.pop(rayon_norm, None)

    def update_area_row_attrs(
        self, key: str, attrs_patch: Dict[str, Any], normalize=None
    ) -> bool:
        """Обновить атрибуты одной записи tasks_area во всех кэшах района."""
        key_str = str(key).strip()
        for rows in self._area_rows_by_rayon.values():
            for index, (attrs, geom) in enumerate(rows):
                if str(attrs.get("key", "")).strip() != key_str:
                    continue
                merged = dict(attrs)
                merged.update(attrs_patch)
                if normalize is not None:
                    merged = normalize(merged)
                rows[index] = (merged, geom)
                return True
        return False

    def close(self):
        if self._pg_conn is not None:
            try:
                self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None
        self._crm_schema_ready.clear()
        self._area_rows_by_rayon.clear()
        self._crm_session_cache = None
        self._district_wkt_by_rayon.clear()

    def _pg_connect_kwargs(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": int(self.port),
            "dbname": self.database,
            "user": self.username,
            "password": self.password,
            "connect_timeout": 15,
            "application_name": f"monitor_db_loader/{_plugin_version()}",
            "options": "-c statement_timeout=30000",
        }

    def _get_pg_connection(self):
        if psycopg2 is None:
            return None
        if self._pg_conn is None or self._pg_conn.closed:
            self._pg_conn = psycopg2.connect(**self._pg_connect_kwargs())
        return self._pg_conn

    def test_connection(self) -> Tuple[bool, str]:
        log_info(
            f"Проверка подключения: {self.username}@{self.host}:{self.port}/{self.database} "
            f"(psycopg2={'да' if psycopg2 else 'нет'})"
        )
        if psycopg2 is not None:
            try:
                conn = psycopg2.connect(**self._pg_connect_kwargs())
                conn.close()
                log_info("Подключение к БД успешно (psycopg2).")
                return True, ""
            except Exception as exc:
                log_warning(f"Подключение к БД не удалось (psycopg2): {exc}")
                return False, str(exc)

        uri = QgsDataSourceUri()
        uri.setConnection(
            self.host, self.port, self.database, self.username, self.password
        )
        uri.setDataSource("public", "spatial_ref_sys", "", "", "")
        layer = QgsVectorLayer(uri.uri(), "connection_test", "postgres")
        if layer.isValid():
            log_info("Подключение к БД успешно (провайдер postgres).")
            return True, ""
        err = layer.error().message() or "Не удалось подключиться к базе данных"
        log_warning(f"Подключение к БД не удалось (провайдер postgres): {err}")
        return False, err

    def detect_geometry_column(
        self, schema: str, table: str, preferred: Optional[str] = None
    ) -> Optional[str]:
        if preferred:
            return preferred

        key = (schema, table, "")
        if key in self._geom_cache:
            return self._geom_cache[key]

        geom_col = self._detect_via_postgres(schema, table)
        if not geom_col:
            geom_col = self._detect_via_postgres_any_schema(table)
        if not geom_col:
            geom_col = self._detect_via_qgis(schema, table)

        self._geom_cache[key] = geom_col
        return geom_col

    def _detect_via_postgres_any_schema(self, table: str) -> Optional[str]:
        conn = self._get_pg_connection()
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f_table_schema, f_geometry_column
                    FROM geometry_columns
                    WHERE f_table_name = %s
                    ORDER BY f_table_schema, f_geometry_column
                    LIMIT 1
                    """,
                    (table,),
                )
                row = cur.fetchone()
                if row and row[1]:
                    log_info(
                        f"Таблица {table} найдена в схеме «{row[0]}», geom={row[1]}"
                    )
                    return row[1]
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        return None

    def _detect_via_postgres(self, schema: str, table: str) -> Optional[str]:
        conn = self._get_pg_connection()
        if conn is None:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT f_geometry_column
                    FROM geometry_columns
                    WHERE f_table_schema = %s AND f_table_name = %s
                    LIMIT 1
                    """,
                    (schema, table),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]

                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                      AND udt_name IN ('geometry', 'geography')
                    ORDER BY ordinal_position
                    LIMIT 1
                    """,
                    (schema, table),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        return None

    def _detect_via_qgis(self, schema: str, table: str) -> Optional[str]:
        log_info(f"Поиск geom для {schema}.{table} перебором: {geom_column_candidates()}")
        last_err = ""
        for candidate in geom_column_candidates():
            uri = self._build_uri_simple(schema, table, candidate, "id")
            layer = QgsVectorLayer(uri, "geom_probe", "postgres")
            if layer.isValid():
                log_info(f"  → найден столбец «{candidate}»")
                return candidate
            last_err = layer.error().message() or ""
        if last_err:
            log_warning(f"  → geom не найден, последняя ошибка: {last_err}")
        return None

    def _connection_uri(self) -> QgsDataSourceUri:
        uri = QgsDataSourceUri()
        uri.setConnection(
            self.host, self.port, self.database, self.username, self.password
        )
        return uri

    def _build_uri_simple(
        self,
        schema: str,
        table: str,
        geom_column: str,
        primary_key: str,
        sql_filter: str = "",
    ) -> str:
        uri = self._connection_uri()
        uri.setDataSource(schema, table, geom_column, sql_filter, primary_key)
        uri.setParam("srid", "4326")
        return uri.uri()

    def create_vector_layer(
        self, layer_def: Dict[str, Any], display_name: str
    ) -> Tuple[Union[Optional[QgsVectorLayer], List[QgsVectorLayer]], str]:
        schema, table, geom_hint = resolve_layer_source(layer_def)
        mixed = is_mixed_geometry(layer_def.get("geometry_type"))
        pk = str(layer_def.get("primary_key", "id"))

        log_info(
            f"Загрузка слоя «{display_name}» ({schema}.{table}"
            f"{', ' + geom_hint if geom_hint else ''}"
            f"{', смешанная геометрия' if mixed else ''})…"
        )
        geom_col = self.detect_geometry_column(schema, table, geom_hint)
        if not geom_col:
            return None, (
                f"Не найден геометрический столбец для {schema}.{table}"
            )

        if mixed:
            return self._mixed_loader.load_sublayers(
                layer_def, display_name, schema, table, geom_col, pk
            )

        sql_filter = layer_def.get("sql_filter", "")
        uri_str = self._build_uri_simple(schema, table, geom_col, pk, sql_filter)
        log_info(f"  uri: {sanitize_uri(uri_str)}")
        if sql_filter:
            log_info(f"  filter: {sql_filter}")
        layer = QgsVectorLayer(uri_str, display_name, "postgres")
        if layer.isValid():
            finalize_vector_layer(layer)
            count = layer.featureCount()
            log_info(f"  → OK, объектов: {count if count >= 0 else '?'}")
            return layer, ""

        err = layer_load_error(layer)
        log_warning(f"  → ошибка postgres: {err}")
        return None, f"{schema}.{table} ({geom_col}): {err}"

# -*- coding: utf-8 -*-
"""PostgreSQL connection helpers and layer URI building."""

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

    def close(self):
        if self._pg_conn is not None:
            try:
                self._pg_conn.close()
            except Exception:
                pass
            self._pg_conn = None
        self._crm_schema_ready.clear()

    def _pg_connect_kwargs(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": int(self.port),
            "dbname": self.database,
            "user": self.username,
            "password": self.password,
            "connect_timeout": 15,
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

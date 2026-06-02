# -*- coding: utf-8 -*-
"""Загрузка таблиц PostGIS с типом GEOMETRY (смешанные фигуры)."""

import re
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import QgsDataSourceUri, QgsVectorLayer

from .config import normalize_geometry_type
from .layer_utils import finalize_vector_layer
from .log_util import log_info, log_warning

_GTYPE_LABELS = {
    "point": "точки",
    "line": "линии",
    "polygon": "полигоны",
}

# Поднаборы через postgres (type=Point/LineString/Polygon + SQL)
_SUBSET_SPECS = {
    "point": {
        "types": ("MultiPoint", "Point"),
        "sql": "{g} IS NOT NULL AND ST_GeometryType({g}) IN ('ST_Point','ST_MultiPoint')",
    },
    "line": {
        "types": ("MultiLineString", "LineString"),
        "sql": "{g} IS NOT NULL AND ST_GeometryType({g}) IN ('ST_LineString','ST_MultiLineString')",
    },
    "polygon": {
        "types": ("MultiPolygon", "Polygon"),
        "sql": "{g} IS NOT NULL AND ST_GeometryType({g}) IN ('ST_Polygon','ST_MultiPolygon')",
    },
}


def sanitize_uri(uri_str: str) -> str:
    return re.sub(r"password='[^']*'", "password='***'", uri_str or "")


def layer_load_error(layer: QgsVectorLayer) -> str:
    parts = []
    msg = layer.error().message()
    if msg:
        parts.append(msg)
    provider = layer.dataProvider()
    if provider:
        pmsg = provider.error().message()
        if pmsg and pmsg not in parts:
            parts.append(pmsg)
    return " | ".join(parts) or "Invalid PostgreSQL layer"


class MixedGeometryLoader:
    """
    Смешанная геометрия загружается отдельными слоями (точки/линии/полигоны)
    с простой символикой — без rule-based (надёжно в QGIS 3.44).
    """

    def __init__(self, connection):
        self._conn = connection

    def load_sublayers(
        self,
        layer_def: Dict[str, Any],
        display_name: str,
        schema: str,
        table: str,
        geom_column: str,
        primary_key: str,
    ) -> Tuple[List[QgsVectorLayer], str]:
        gtypes = normalize_geometry_type(layer_def.get("geometry_type"))
        log_info(
            f"  смешанная геометрия → {len(gtypes)} подслоя из {schema}.{table}"
        )

        loaded: List[QgsVectorLayer] = []
        errors: List[str] = []

        for gtype in gtypes:
            spec = _SUBSET_SPECS.get(gtype)
            if not spec:
                continue
            layer, err = self._load_one_subset(
                schema, table, geom_column, primary_key, gtype, spec
            )
            if layer:
                label = _GTYPE_LABELS.get(gtype, gtype)
                layer.setName(f"{display_name} — {label}")
                loaded.append(layer)
            elif err:
                errors.append(f"{gtype}: {err}")

        if loaded:
            log_info(f"  → подслоёв загружено: {len(loaded)}")
            return loaded, ""

        return [], "; ".join(errors) if errors else "нет подходящих объектов"

    def _load_one_subset(
        self,
        schema: str,
        table: str,
        geom_column: str,
        primary_key: str,
        gtype: str,
        spec: Dict[str, Any],
    ) -> Tuple[Optional[QgsVectorLayer], str]:
        sql = spec["sql"].format(g=geom_column)
        last_err = ""

        for qgis_type in spec["types"]:
            layer, err = self._try_postgres_subset(
                schema, table, geom_column, primary_key, sql, qgis_type, gtype
            )
            if layer:
                return layer, ""
            last_err = err

        layer, err = self._try_postgres_subset(
            schema, table, geom_column, primary_key, sql, None, gtype
        )
        if layer:
            return layer, ""
        return None, last_err or err

    def _try_postgres_subset(
        self,
        schema: str,
        table: str,
        geom_column: str,
        primary_key: str,
        sql_filter: str,
        qgis_type: Optional[str],
        gtype: str,
    ) -> Tuple[Optional[QgsVectorLayer], str]:
        uri = self._conn._connection_uri()
        uri.setDataSource(schema, table, geom_column, sql_filter, primary_key)
        if qgis_type:
            uri.setParam("type", qgis_type)
        uri.setParam("srid", "4326")
        uri_str = uri.uri()

        log_info(f"  поднабор «{gtype}» type={qgis_type or 'auto'}")
        log_info(f"    uri: {sanitize_uri(uri_str)}")

        layer = QgsVectorLayer(uri_str, f"{table}_{gtype}", "postgres")
        if layer.isValid():
            finalize_vector_layer(layer)
            cnt = layer.featureCount()
            log_info(f"    → OK, объектов: {cnt}")
            if cnt == 0:
                return None, "0 объектов"
            return layer, ""

        err = layer_load_error(layer)
        log_warning(f"    → {err}")
        return None, err

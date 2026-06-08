# -*- coding: utf-8 -*-
"""Apply layer symbology from JSON configuration."""

from typing import Any, Dict, List, Optional

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsLinePatternFillSymbolLayer,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsRendererCategory,
    QgsRuleBasedRenderer,
    QgsSimpleFillSymbolLayer,
    QgsSimpleLineSymbolLayer,
    QgsSimpleMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsUnitTypes,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from .config import is_mixed_geometry, normalize_geometry_type
from .log_util import log_info, log_warning
from .qt_compat import PEN_DASH, PEN_NONE, PEN_SOLID


def _color(hex_color: str, alpha: Optional[float] = None) -> QColor:
    c = QColor(hex_color)
    if alpha is not None:
        c.setAlphaF(float(alpha))
    return c


def apply_symbology(layer: QgsVectorLayer, layer_def: Dict[str, Any]) -> None:
    if layer_def.get("categorized_symbology") or layer_def.get("categorized_field"):
        _apply_categorized(layer, layer_def)
        return

    if layer_def.get("rule_based_symbology"):
        _apply_rule_based(layer, layer_def)
        return

    symbology = layer_def.get("symbology", {})
    geometry_type = layer_def.get("geometry_type")

    if symbology.get("complex_marker"):
        symbol = _create_complex_point_symbol(symbology)
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()
        return

    if is_mixed_geometry(geometry_type):
        _apply_mixed_geometry(layer, geometry_type, symbology)
        return

    gtypes = normalize_geometry_type(geometry_type)
    gtype = gtypes[0] if gtypes else "point"

    if gtype == "point":
        symbol = _create_point_symbol(symbology)
    elif gtype == "line":
        symbol = _create_line_symbol(symbology)
    else:
        symbol = _create_polygon_symbol(symbology)

    if symbol:
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.triggerRepaint()


def _symbology_from_rules(rules: List[Dict], role: str) -> Dict[str, Any]:
    """role: 'red' (first non-ELSE) or 'default' (ELSE)."""
    for rule in rules:
        if rule.get("condition", "").upper() == "ELSE":
            if role == "default":
                return rule.get("symbology", {})
        else:
            if role == "red":
                return rule.get("symbology", {})
    return {}


def _apply_categorized(layer: QgsVectorLayer, layer_def: Dict[str, Any]) -> None:
    field = layer_def.get("categorized_field", "short_sobstv_rr")
    idx = layer.fields().indexOf(field)
    if idx < 0:
        log_warning(
            f"Символика «{layer.name()}»: поле «{field}» не найдено, "
            f"доступны: {[f.name() for f in layer.fields()]}"
        )
        return

    rules = layer_def.get("rules", [])
    red_values = layer_def.get("red_category_values", ())
    red_label = "Город Москва и неизвестные"
    default_label = "Прочие"
    for rule in rules:
        if rule.get("condition", "").upper() == "ELSE":
            default_label = rule.get("name", default_label)
        else:
            red_label = rule.get("name", red_label)

    red_sym = _create_polygon_symbol(_symbology_from_rules(rules, "red"))
    default_sym = _create_polygon_symbol(_symbology_from_rules(rules, "default"))

    categories = []
    for val in red_values:
        if red_sym is None:
            continue
        categories.append(
            QgsRendererCategory(str(val), red_sym.clone(), red_label)
        )

    renderer = QgsCategorizedSymbolRenderer(field, categories)
    if default_sym:
        renderer.setSourceSymbol(default_sym.clone())
        if hasattr(renderer, "setSourceSymbolAnnotation"):
            renderer.setSourceSymbolAnnotation(default_label)

    layer.setRenderer(renderer)
    layer.triggerRepaint()
    log_info(
        f"Символика «{layer.name()}»: categorized по «{field}», "
        f"{len(categories)} значений → «{red_label}», остальное → «{default_label}»"
    )


def _apply_mixed_geometry(
    layer: QgsVectorLayer,
    geometry_type: Any,
    symbology: Dict[str, Any],
) -> None:
    root = QgsRuleBasedRenderer.Rule(None)
    mapping = {
        "point": (
            "Точки",
            "geometry_type($geometry) IN ('Point','MultiPoint')",
        ),
        "line": (
            "Линии",
            "geometry_type($geometry) IN ('LineString','MultiLineString')",
        ),
        "polygon": (
            "Полигоны",
            "geometry_type($geometry) IN ('Polygon','MultiPolygon')",
        ),
    }
    for gtype in normalize_geometry_type(geometry_type):
        label, filt = mapping.get(gtype, (gtype, ""))
        part = symbology.get(gtype, {})
        if gtype == "point":
            symbol = _create_point_symbol(part)
        elif gtype == "line":
            symbol = _create_line_symbol(part)
        else:
            symbol = _create_polygon_symbol(part)
        if symbol:
            rule = QgsRuleBasedRenderer.Rule(symbol, 0, 0, label, filt)
            if hasattr(rule, "setDescription"):
                rule.setDescription(label)
            root.appendChild(rule)

    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def _apply_rule_based(layer: QgsVectorLayer, layer_def: Dict[str, Any]) -> None:
    root = QgsRuleBasedRenderer.Rule(None)
    for rule_def in layer_def.get("rules", []):
        condition = rule_def.get("condition", "")
        symbol = _create_polygon_symbol(rule_def.get("symbology", {}))
        if condition.upper() == "ELSE":
            rule = QgsRuleBasedRenderer.Rule(symbol)
            rule.setIsElse(True)
        else:
            rule = QgsRuleBasedRenderer.Rule(
                symbol,
                0,
                0,
                rule_def.get("name", ""),
                condition,
            )
        root.appendChild(rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def _create_complex_point_symbol(symbology: Dict[str, Any]) -> QgsMarkerSymbol:
    """Круг с чёрным центром и красной обводкой (один слой маркера)."""
    size = float(symbology.get("size", 6))
    outer_w = float(symbology.get("outer_width", 2))
    symbol = QgsMarkerSymbol()
    layer = QgsSimpleMarkerSymbolLayer()
    layer.setShape(QgsSimpleMarkerSymbolLayer.Circle)
    layer.setSize(size)
    layer.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    layer.setColor(_color(symbology.get("center_color", "#000000")))
    layer.setStrokeColor(_color(symbology.get("outer_color", "#FF0000")))
    layer.setStrokeWidth(outer_w)
    layer.setStrokeStyle(PEN_SOLID)
    symbol.appendSymbolLayer(layer)
    return symbol


def _create_point_symbol(symbology: Dict[str, Any]) -> QgsMarkerSymbol:
    symbol = QgsMarkerSymbol()
    layer = QgsSimpleMarkerSymbolLayer()
    marker_type = symbology.get("marker_type", "circle")
    if marker_type == "square":
        layer.setShape(QgsSimpleMarkerSymbolLayer.Square)
    elif marker_type in ("pyramid", "triangle"):
        layer.setShape(QgsSimpleMarkerSymbolLayer.Triangle)
    else:
        layer.setShape(QgsSimpleMarkerSymbolLayer.Circle)
    layer.setSize(float(symbology.get("size", 3)))
    layer.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    color = _color(symbology.get("color", "#000000"))
    opacity = symbology.get("opacity")
    if opacity is not None:
        color.setAlphaF(float(opacity))
    layer.setColor(color)
    layer.setStrokeStyle(PEN_NONE)
    symbol.appendSymbolLayer(layer)
    return symbol


def _create_line_symbol(symbology: Dict[str, Any]) -> QgsLineSymbol:
    symbol = QgsLineSymbol()
    layer = QgsSimpleLineSymbolLayer()
    layer.setColor(_color(symbology.get("color", "#000000")))
    layer.setWidth(float(symbology.get("width", 1.0)))
    style = symbology.get("style", symbology.get("outline_style", "solid"))
    if style == "dash":
        layer.setPenStyle(PEN_DASH)
    else:
        layer.setPenStyle(PEN_SOLID)
    symbol.appendSymbolLayer(layer)
    return symbol


def _create_polygon_symbol(symbology: Dict[str, Any]) -> QgsFillSymbol:
    symbol = QgsFillSymbol()
    fill_opacity = symbology.get("fill_opacity", 0.5)

    if float(fill_opacity) <= 0:
        fill_layer = QgsSimpleFillSymbolLayer()
        fill_layer.setFillColor(QColor(0, 0, 0, 0))
        fill_layer.setStrokeStyle(PEN_NONE)
        symbol.appendSymbolLayer(fill_layer)
    else:
        fill_layer = QgsSimpleFillSymbolLayer()
        fill_color = _color(
            symbology.get("fill_color", symbology.get("color", "#808080")),
            fill_opacity,
        )
        fill_layer.setFillColor(fill_color)
        fill_layer.setStrokeStyle(PEN_NONE)
        symbol.appendSymbolLayer(fill_layer)

        hatch = symbology.get("hatch", {})
        if hatch.get("enabled"):
            _add_hatch_layer(symbol, hatch)

    outline_color = symbology.get("outline_color", symbology.get("fill_color", "#000000"))
    outline_width = float(symbology.get("outline_width", 1.0))
    if outline_color or outline_width > 0:
        outline = QgsSimpleLineSymbolLayer()
        outline.setColor(_color(outline_color))
        outline.setWidth(outline_width)
        style = symbology.get("outline_style", "solid")
        if style == "dash":
            outline.setPenStyle(PEN_DASH)
        else:
            outline.setPenStyle(PEN_SOLID)
        symbol.appendSymbolLayer(outline)

    return symbol


def _add_hatch_layer(symbol: QgsFillSymbol, hatch: Dict[str, Any]) -> None:
    line_symbol = QgsLineSymbol()
    line_layer = QgsSimpleLineSymbolLayer()
    line_layer.setColor(_color(hatch.get("color", "#FFFFFF")))
    line_layer.setWidth(0.5)
    line_symbol.appendSymbolLayer(line_layer)

    pattern = QgsLinePatternFillSymbolLayer()
    pattern.setLineAngle(float(hatch.get("angle", 45)))
    pattern.setDistance(float(hatch.get("spacing", 5)))
    if hasattr(pattern, "setLineSymbol"):
        pattern.setLineSymbol(line_symbol)
    elif hasattr(pattern, "setStrokeSymbol"):
        pattern.setStrokeSymbol(line_symbol)
    symbol.appendSymbolLayer(pattern)

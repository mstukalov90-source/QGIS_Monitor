# -*- coding: utf-8 -*-
"""Сбор задач CRM по району."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate, QDateTime
from qgis.PyQt.QtWidgets import QApplication, QMessageBox, QProgressDialog

from ..ui.district_dialog import DistrictDialog
from .auth import (
    DB_PASSWORD,
    UserSession,
    allowed_rayons_set,
    default_task_source,
    filter_rayons_on_layer,
)
from .config import crm_task_store, crm_tasks, database_connection
from .crm_task_store import ensure_all_snapshot_tables, persist_task_result
from .db import DatabaseConnection
from .district_utils import (
    DistrictBoundary,
    features_in_district,
    find_layer_by_name,
    load_district_boundary,
    resolve_layers,
)
from .district_utils import WGS84
from .log_util import log_info, log_warning


@dataclass
class TaskFeature:
    layer: Optional[QgsVectorLayer]
    layer_name: str
    feature_id: Optional[int]
    attributes: Dict[str, Any]
    task_key: Optional[str] = None
    sent_at: Optional[str] = None
    area_geom: Optional[QgsGeometry] = None
    layer_key: Optional[str] = None
    task_geom: Optional[QgsGeometry] = None


@dataclass
class TaskSubgroup:
    name: str
    features: List[TaskFeature] = field(default_factory=list)
    date_field: Optional[str] = None


@dataclass
class TaskGroup:
    name: str
    subgroups: List[TaskSubgroup] = field(default_factory=list)


@dataclass
class TaskResult:
    district_name: str
    filter_date_from: QDate
    filter_date_to: QDate
    apply_date_filter: bool = True
    groups: List[TaskGroup] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    task_source: str = "active"

    @property
    def total_count(self) -> int:
        return sum(
            len(sub.features)
            for group in self.groups
            for sub in group.subgroups
        )


def copy_task_feature(feat: TaskFeature) -> TaskFeature:
    """Копия TaskFeature без deepcopy (QgsVectorLayer/QgsGeometry не копируются)."""
    return TaskFeature(
        layer=feat.layer,
        layer_name=feat.layer_name,
        feature_id=feat.feature_id,
        attributes=dict(feat.attributes),
        task_key=feat.task_key,
        sent_at=feat.sent_at,
        area_geom=feat.area_geom,
        layer_key=feat.layer_key,
        task_geom=feat.task_geom,
    )


def copy_task_result(result: TaskResult) -> TaskResult:
    """Копия TaskResult для снимка «активных» задач до фильтрации."""
    groups: List[TaskGroup] = []
    for group in result.groups:
        subgroups: List[TaskSubgroup] = []
        for sub in group.subgroups:
            subgroups.append(
                TaskSubgroup(
                    name=sub.name,
                    features=[copy_task_feature(f) for f in sub.features],
                    date_field=sub.date_field,
                )
            )
        groups.append(TaskGroup(name=group.name, subgroups=subgroups))
    return TaskResult(
        district_name=result.district_name,
        filter_date_from=result.filter_date_from,
        filter_date_to=result.filter_date_to,
        apply_date_filter=result.apply_date_filter,
        groups=groups,
        errors=list(result.errors),
        task_source=result.task_source,
    )


_DATE_TEXT_FORMATS = (
    "dd.MM.yyyy",
    "yyyy-MM-dd",
    "dd.MM.yyyy HH:mm:ss",
    "yyyy-MM-dd HH:mm:ss",
    "yyyy-MM-ddTHH:mm:ss",
)


def _parse_text_date(text: str) -> Optional[QDate]:
    text = text.strip()
    if not text:
        return None
    candidates = [text]
    if len(text) >= 10:
        candidates.append(text[:10])
    if len(text) >= 19:
        candidates.append(text[:19])
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        for fmt in _DATE_TEXT_FORMATS:
            parsed = QDate.fromString(candidate, fmt)
            if parsed.isValid():
                return parsed
    return None


def _feature_date_value(feat: QgsFeature, field_name: str) -> Optional[QDate]:
    idx = feat.fields().indexOf(field_name)
    if idx < 0:
        return None
    val = feat[field_name]
    if val is None:
        return None
    if isinstance(val, QDate):
        return val if val.isValid() else None
    if isinstance(val, QDateTime):
        return val.date() if val.isValid() else None
    if isinstance(val, datetime):
        return QDate(val.year, val.month, val.day)
    if isinstance(val, date):
        return QDate(val.year, val.month, val.day)
    return _parse_text_date(str(val))


def _date_filter_range(lookback_days: int) -> Tuple[QDate, QDate]:
    """Интервал от (сегодня − lookback_days) до сегодня включительно."""
    today = QDate.currentDate()
    return today.addDays(-lookback_days), today


def _feature_matches_date_range(
    feat: QgsFeature,
    field_name: str,
    date_from: QDate,
    date_to: QDate,
) -> bool:
    feat_date = _feature_date_value(feat, field_name)
    if feat_date is None:
        return False
    return date_from <= feat_date <= date_to


def _feature_geom_wgs84(
    layer: QgsVectorLayer, feat: QgsFeature
) -> Optional[QgsGeometry]:
    geom = feat.geometry()
    if not geom or geom.isEmpty():
        return None
    out = QgsGeometry(geom)
    source_crs = layer.crs() if layer.crs().isValid() else WGS84
    if source_crs.isValid() and WGS84.isValid() and source_crs != WGS84:
        out.transform(
            QgsCoordinateTransform(
                source_crs, WGS84, QgsProject.instance().transformContext()
            )
        )
    return out if not out.isEmpty() else None


def _feature_to_task(layer: QgsVectorLayer, feat: QgsFeature) -> TaskFeature:
    attrs = {f.name(): feat[f.name()] for f in feat.fields()}
    return TaskFeature(
        layer=layer,
        layer_name=layer.name(),
        feature_id=feat.id(),
        attributes=attrs,
        task_geom=_feature_geom_wgs84(layer, feat),
    )


def _collect_subgroup_features(
    layers: List[QgsVectorLayer],
    district: DistrictBoundary,
    metric_crs: QgsCoordinateReferenceSystem,
    date_field: Optional[str],
    date_from: QDate,
    date_to: QDate,
    subgroup_name: str,
) -> List[TaskFeature]:
    collected: List[TaskFeature] = []
    warned_missing_field = False
    warned_date_sample = False

    for layer in layers:
        layer_count = 0
        in_district = 0
        for feat in features_in_district(layer, district, metric_crs):
            in_district += 1
            if date_field:
                if layer.fields().indexOf(date_field) < 0:
                    if not warned_missing_field:
                        log_warning(
                            f"Подгруппа «{subgroup_name}»: в слое «{layer.name()}» "
                            f"нет поля «{date_field}»"
                        )
                        warned_missing_field = True
                    continue
                if not _feature_matches_date_range(
                    feat, date_field, date_from, date_to
                ):
                    if not warned_date_sample and feat[date_field]:
                        parsed = _feature_date_value(feat, date_field)
                        log_info(
                            f"  CRM «{subgroup_name}»: пример даты "
                            f"{feat[date_field]!r} → "
                            f"{parsed.toString('yyyy-MM-dd') if parsed else 'не распознана'}, "
                            f"период {date_from.toString('dd.MM.yyyy')}–"
                            f"{date_to.toString('dd.MM.yyyy')}"
                        )
                        warned_date_sample = True
                    continue
            collected.append(_feature_to_task(layer, feat))
            layer_count += 1

        log_info(
            f"  CRM «{subgroup_name}» / «{layer.name()}»: "
            f"{layer_count} объектов (в районе: {in_district})"
        )

    return collected


def _build_task_result(
    cfg: Dict[str, Any],
    district: DistrictBoundary,
    metric_crs: QgsCoordinateReferenceSystem,
    root,
    date_from: QDate,
    date_to: QDate,
    apply_date_filter: bool,
    progress: QProgressDialog,
) -> tuple:
    result = TaskResult(
        district_name=district.name,
        filter_date_from=date_from,
        filter_date_to=date_to,
        apply_date_filter=apply_date_filter,
    )
    groups_cfg = cfg.get("groups", [])
    total_steps = sum(
        len(group.get("subgroups", [])) for group in groups_cfg
    )
    step = 0

    for group_cfg in groups_cfg:
        group = TaskGroup(name=group_cfg.get("name", ""))
        for sub_cfg in group_cfg.get("subgroups", []):
            step += 1
            progress.setValue(step)
            sub_name = sub_cfg.get("name", "")
            progress.setLabelText(
                f"Слой {step}/{total_steps}: {sub_name}…"
            )
            QApplication.processEvents()
            if progress.wasCanceled():
                return result, True

            if sub_cfg.get("source") == "field_data":
                group.subgroups.append(
                    TaskSubgroup(name=sub_name, features=[])
                )
                log_info(
                    f"CRM подгруппа «{sub_name}»: загрузка из БД после сохранения"
                )
                continue

            if sub_cfg.get("source") == "office_data":
                group.subgroups.append(
                    TaskSubgroup(name=sub_name, features=[])
                )
                log_info(
                    f"CRM подгруппа «{sub_name}»: загрузка из БД после сохранения"
                )
                continue

            layer_names = sub_cfg.get("layers", [])
            group_names = sub_cfg.get("groups", [])
            layers, missing = resolve_layers(root, layer_names, group_names)
            for name in missing:
                msg = f"Не найден слой или группа: {name}"
                log_warning(msg)
                result.errors.append(msg)

            date_field = sub_cfg.get("date_field") if apply_date_filter else None
            features = _collect_subgroup_features(
                layers,
                district,
                metric_crs,
                date_field,
                date_from,
                date_to,
                sub_cfg.get("name", ""),
            )
            group.subgroups.append(
                TaskSubgroup(
                    name=sub_cfg.get("name", ""),
                    features=features,
                    date_field=date_field,
                )
            )
            log_info(
                f"CRM подгруппа «{sub_cfg.get('name', '')}»: {len(features)} объектов"
            )

        result.groups.append(group)

    return result, False


def connect_db(config: Dict[str, Any]) -> Optional[DatabaseConnection]:
    """Подключение к PostgreSQL с фиксированным паролем после авторизации."""
    db_cfg = database_connection(config)
    conn = DatabaseConnection(db_cfg, DB_PASSWORD)
    ok, err = conn.test_connection()
    if ok:
        return conn
    conn.close()
    log_warning(f"Подключение к БД не удалось: {err}")
    return None


def _persist_tasks_to_db(
    config: Dict[str, Any],
    task_result: TaskResult,
    parent,
    db_conn: Optional[DatabaseConnection] = None,
    user_session: Optional[UserSession] = None,
) -> Optional[DatabaseConnection]:
    store_cfg = crm_task_store(config)
    if not store_cfg:
        log_warning("Секция crm_tasks.task_store отсутствует — запись в БД пропущена")
        return db_conn

    conn = db_conn
    own_conn = False
    if conn is None:
        conn = connect_db(config)
        own_conn = True
    if conn is None:
        log_info("Подключение к БД недоступно")
        return None

    if not ensure_all_snapshot_tables(conn, store_cfg):
        log_warning(
            "Не все snapshot-таблицы CRM подготовлены — "
            "отправка задач может завершиться ошибкой"
        )

    if task_result.total_count == 0:
        log_info(
            "Нет объектов слоёв для записи в crm.tasks; "
            "подключение сохранено для площадных заказов"
        )
        return conn

    login = user_session.login if user_session else ""

    try:
        stats = persist_task_result(conn, task_result, store_cfg, login)
    except Exception as exc:
        if own_conn:
            conn.close()
        QMessageBox.warning(
            parent,
            "Мониторинг разрытий — задачи",
            f"Не удалось записать задачи в БД:\n{exc}\n\n"
            f"Список объектов будет показан без сохранения.",
        )
        return None

    processed = stats.inserted + stats.skipped + stats.invalid
    if processed == 0:
        log_info("crm.tasks: нечего записывать (0 объектов с ID на слоях)")
        return conn

    if stats.inserted > 0 or stats.skipped > 0:
        QMessageBox.information(
            parent,
            "Мониторинг разрытий — задачи",
            f"Запись в crm.tasks:\n"
            f"• Добавлено: {stats.inserted}\n"
            f"• Уже в БД: {stats.skipped}\n"
            f"• Без ID: {stats.invalid}",
        )
    elif stats.invalid > 0:
        QMessageBox.warning(
            parent,
            "Мониторинг разрытий — задачи",
            f"Не удалось сохранить задачи в crm.tasks:\n"
            f"• Без ID (нет поля-идентификатора): {stats.invalid}",
        )
    if own_conn:
        return conn
    return conn


def run_get_task(
    config: Dict[str, Any],
    iface,
    parent=None,
    user_session: Optional[UserSession] = None,
    db_conn: Optional[DatabaseConnection] = None,
) -> TaskResult:
    """Запуск сбора задач CRM по выбранному району."""
    today = QDate.currentDate()
    empty_result = TaskResult(
        district_name="",
        filter_date_from=today,
        filter_date_to=today,
    )

    cfg = crm_tasks(config)
    if not cfg:
        QMessageBox.critical(
            parent or iface.mainWindow(),
            "Мониторинг разрытий",
            "Секция crm_tasks отсутствует в конфигурации.",
        )
        empty_result.errors = ["Конфигурация не найдена"]
        return empty_result

    metric_crs = QgsCoordinateReferenceSystem(cfg.get("metric_crs", "EPSG:32637"))
    district_cfg = cfg.get("district_filter", {})
    lookback_days = int(cfg.get("date_lookback_days", 3))
    date_from, date_to = _date_filter_range(lookback_days)

    root = QgsProject.instance().layerTreeRoot()
    boundaries_name = district_cfg.get("boundaries_layer", "Границы районов")
    boundaries_field = district_cfg.get("field", "rayon")
    boundaries_layer = find_layer_by_name(root, boundaries_name)

    if boundaries_layer is None:
        QMessageBox.warning(
            parent or iface.mainWindow(),
            "Мониторинг разрытий — получить задачу",
            f"Слой «{boundaries_name}» не найден.\n\nСначала выполните загрузку данных из БД.",
        )
        return TaskResult(
            district_name="",
            filter_date_from=date_from,
            filter_date_to=date_to,
            errors=[boundaries_name],
        )

    if boundaries_layer.fields().indexOf(boundaries_field) < 0:
        QMessageBox.warning(
            parent or iface.mainWindow(),
            "Мониторинг разрытий — получить задачу",
            f"В слое «{boundaries_name}» нет поля «{boundaries_field}».",
        )
        return TaskResult(
            district_name="",
            filter_date_from=date_from,
            filter_date_to=date_to,
            errors=[boundaries_field],
        )

    allowed_rayons = None
    if user_session is not None:
        allowed_rayons = allowed_rayons_set(db_conn, user_session)
        if allowed_rayons is None:
            allowed_rayons = None
        elif not allowed_rayons and db_conn is None:
            rayons = filter_rayons_on_layer(
                boundaries_layer, boundaries_field, user_session
            )
            allowed_rayons = set(rayons) if rayons else set()

    if not DistrictDialog.list_rayons(
        boundaries_layer, boundaries_field, allowed_rayons
    ):
        QMessageBox.warning(
            parent or iface.mainWindow(),
            "Мониторинг разрытий — получить задачу",
            (
                f"Нет доступных районов в слое «{boundaries_name}»."
                if allowed_rayons is not None
                else f"В слое «{boundaries_name}» нет значений в поле «{boundaries_field}»."
            ),
        )
        return TaskResult(
            district_name="",
            filter_date_from=date_from,
            filter_date_to=date_to,
            errors=["Нет районов"],
        )

    choice = DistrictDialog.choose_for_crm(
        boundaries_layer,
        boundaries_field,
        parent or iface.mainWindow(),
        date_from=date_from,
        date_to=date_to,
        allowed_rayons=allowed_rayons,
    )
    if choice is None:
        log_info("Получить задачу: отменено пользователем")
        return TaskResult(
            district_name="",
            filter_date_from=date_from,
            filter_date_to=date_to,
        )

    rayon = choice.rayon
    apply_date_filter = choice.apply_date_filter

    district = load_district_boundary(
        boundaries_layer, boundaries_field, rayon, metric_crs
    )
    if district is None:
        QMessageBox.warning(
            parent or iface.mainWindow(),
            "Мониторинг разрытий — получить задачу",
            f"Не удалось загрузить полигон района «{rayon}».",
        )
        return TaskResult(
            district_name=rayon,
            filter_date_from=date_from,
            filter_date_to=date_to,
            errors=[rayon],
        )

    log_info(
        f"Получить задачу: район «{district.name}», "
        f"фильтр по дате: {'да' if apply_date_filter else 'нет'}"
        + (
            f", период {date_from.toString('yyyy-MM-dd')} — "
            f"{date_to.toString('yyyy-MM-dd')}"
            if apply_date_filter
            else ""
        )
    )

    role = user_session.role if user_session else "manager"
    initial_source = default_task_source(role)
    field_only = role == "field"

    if field_only:
        task_result = TaskResult(
            district_name=district.name,
            filter_date_from=date_from,
            filter_date_to=date_to,
            apply_date_filter=apply_date_filter,
            task_source=initial_source,
        )
        log_info(
            f"Получить задачу (полевой режим): район «{district.name}»"
        )
        if db_conn is None:
            db_conn = connect_db(config)
    else:
        groups_cfg = cfg.get("groups", [])
        total_steps = sum(len(g.get("subgroups", [])) for g in groups_cfg)
        progress = QProgressDialog(
            "Подготовка…",
            "Отмена",
            0,
            max(total_steps, 1),
            parent or iface.mainWindow(),
        )
        progress.setWindowTitle("Monitor CRM")
        progress.setMinimumDuration(0)
        progress.setValue(0)

        task_result, canceled = _build_task_result(
            cfg,
            district,
            metric_crs,
            root,
            date_from,
            date_to,
            apply_date_filter,
            progress,
        )
        progress.close()

        if canceled:
            log_info("Получить задачу: отменено при сборе")
            return task_result

        log_info(
            f"Получить задачу завершено: район «{district.name}», "
            f"объектов на слоях: {task_result.total_count}"
        )

        task_result.task_source = initial_source
        office_role = role == "office"

        if office_role:
            if db_conn is None:
                db_conn = connect_db(config)
            log_info(
                "Получить задачу (office): запись в crm.tasks отложена — "
                "кнопка «Синхронизировать задачи района»"
            )
        else:
            db_conn = _persist_tasks_to_db(
                config,
                task_result,
                parent or iface.mainWindow(),
                db_conn=db_conn,
                user_session=user_session,
            )

            if db_conn is not None:
                from .crm_field_data import append_field_data_to_result
                from .crm_office_data import append_office_data_to_result

                store_cfg = crm_task_store(config)
                metric_srid = metric_crs.postgisSrid()
                if metric_srid <= 0:
                    auth = metric_crs.authid() or ""
                    if auth.upper().startswith("EPSG:"):
                        try:
                            metric_srid = int(auth.split(":", 1)[1])
                        except ValueError:
                            metric_srid = 32637
                    else:
                        metric_srid = 32637
                append_field_data_to_result(
                    task_result,
                    db_conn,
                    district,
                    store_cfg,
                    metric_srid,
                )
                append_office_data_to_result(
                    task_result,
                    db_conn,
                    district,
                    store_cfg,
                    metric_srid,
                )

    if db_conn is not None:
        from .crm_tasks_area import preload_area_geometries

        area_progress = QProgressDialog(
            "Загрузка площадных заказов…",
            None,
            0,
            0,
            parent or iface.mainWindow(),
        )
        area_progress.setWindowTitle("Monitor CRM")
        area_progress.setMinimumDuration(0)
        area_progress.show()
        QApplication.processEvents()
        try:
            preload_area_geometries(db_conn, district.name)
        except Exception as exc:
            log_warning(f"Предзагрузка площадных заказов не удалась: {exc}")
        finally:
            area_progress.close()

    from ..ui.task_dialog import TaskDialog

    def _restart():
        run_get_task(
            config,
            iface,
            parent,
            user_session=user_session,
            db_conn=db_conn,
        )

    TaskDialog.open(
        task_result,
        iface,
        config=config,
        db_conn=db_conn,
        district=district,
        apply_date_filter=apply_date_filter,
        on_change_district=_restart,
        user_session=user_session,
    )

    return task_result

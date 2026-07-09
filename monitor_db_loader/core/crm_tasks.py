# -*- coding: utf-8 -*-
"""Сбор задач CRM по району."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDate
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
from .db import DatabaseConnection
from .district_utils import (
    DistrictBoundary,
    find_layer_by_name,
    load_district_boundary,
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



def _date_filter_range(lookback_days: int) -> Tuple[QDate, QDate]:
    """Интервал от (сегодня − lookback_days) до сегодня включительно."""
    today = QDate.currentDate()
    return today.addDays(-lookback_days), today


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


def _feature_to_task(layer: QgsVectorLayer, feat: QgsFeature) -> TaskFeature:
    attrs = {f.name(): feat[f.name()] for f in feat.fields()}
    return TaskFeature(
        layer=layer,
        layer_name=layer.name(),
        feature_id=feat.id(),
        attributes=attrs,
        task_geom=_feature_geom_wgs84(layer, feat),
    )


def _metric_srid(metric_crs: QgsCoordinateReferenceSystem) -> int:
    metric_srid = metric_crs.postgisSrid()
    if metric_srid > 0:
        return metric_srid
    auth = metric_crs.authid() or ""
    if auth.upper().startswith("EPSG:"):
        try:
            return int(auth.split(":", 1)[1])
        except ValueError:
            pass
    return 32637


def _build_task_result_from_db(
    cfg: Dict[str, Any],
    district: DistrictBoundary,
    metric_crs: QgsCoordinateReferenceSystem,
    date_from: QDate,
    date_to: QDate,
    apply_date_filter: bool,
    progress: QProgressDialog,
    store_cfg: Dict[str, Any],
    config: Dict[str, Any],
    db_conn: DatabaseConnection,
) -> tuple:
    from .crm_db_tasks import (
        collect_db_subgroup_tasks,
        is_db_loaded_subgroup,
        is_deferred_subgroup,
    )

    result = TaskResult(
        district_name=district.name,
        filter_date_from=date_from,
        filter_date_to=date_to,
        apply_date_filter=apply_date_filter,
    )
    groups_cfg = cfg.get("groups", [])
    total_steps = sum(len(group.get("subgroups", [])) for group in groups_cfg)
    step = 0
    metric_srid = _metric_srid(metric_crs)

    for group_cfg in groups_cfg:
        group = TaskGroup(name=group_cfg.get("name", ""))
        for sub_cfg in group_cfg.get("subgroups", []):
            step += 1
            progress.setValue(step)
            sub_name = sub_cfg.get("name", "")
            progress.setLabelText(
                f"Загрузка {step}/{total_steps}: {sub_name}…"
            )
            QApplication.processEvents()
            if progress.wasCanceled():
                return result, True

            date_field = sub_cfg.get("date_field") if apply_date_filter else None

            if is_deferred_subgroup(sub_cfg):
                group.subgroups.append(
                    TaskSubgroup(
                        name=sub_name,
                        features=[],
                        date_field=date_field,
                    )
                )
                log_info(
                    f"CRM подгруппа «{sub_name}»: загрузка из БД после сохранения"
                )
                continue

            if not is_db_loaded_subgroup(sub_cfg, store_cfg, sub_name):
                group.subgroups.append(
                    TaskSubgroup(
                        name=sub_name,
                        features=[],
                        date_field=date_field,
                    )
                )
                log_warning(
                    f"CRM подгруппа «{sub_name}»: нет маппинга для загрузки из БД"
                )
                continue

            features, errors = collect_db_subgroup_tasks(
                db_conn,
                district,
                metric_srid,
                sub_name,
                store_cfg,
                config,
                sub_cfg,
                date_from=date_from,
                date_to=date_to,
                apply_date_filter=apply_date_filter,
            )
            result.errors.extend(errors)
            group.subgroups.append(
                TaskSubgroup(
                    name=sub_name,
                    features=features,
                    date_field=date_field,
                )
            )
            log_info(f"CRM подгруппа «{sub_name}»: {len(features)} объектов")

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
        if db_conn is None:
            db_conn = connect_db(config)
        if db_conn is None:
            QMessageBox.warning(
                parent or iface.mainWindow(),
                "Мониторинг разрытий — получить задачу",
                "Нет подключения к БД.\n\nЗадачи загружаются только из crm.tasks.",
            )
            return TaskResult(
                district_name=district.name,
                filter_date_from=date_from,
                filter_date_to=date_to,
                errors=["Нет подключения к БД"],
            )

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

        store_cfg = crm_task_store(config)
        task_result, canceled = _build_task_result_from_db(
            cfg,
            district,
            metric_crs,
            date_from,
            date_to,
            apply_date_filter,
            progress,
            store_cfg,
            config,
            db_conn,
        )
        progress.close()

        if canceled:
            log_info("Получить задачу: отменено при сборе")
            return task_result

        log_info(
            f"Получить задачу завершено: район «{district.name}», "
            f"объектов из БД: {task_result.total_count}"
        )

        task_result.task_source = initial_source

        from .crm_field_data import append_field_data_to_result
        from .crm_office_data import append_office_data_to_result
        from .crm_task_store import (
            ensure_crm_session_cache,
            enrich_task_result_field_observed,
            filter_sent_tasks_from_result,
        )

        ensure_crm_session_cache(db_conn, store_cfg)
        filter_sent_tasks_from_result(task_result, db_conn, store_cfg)
        enrich_task_result_field_observed(task_result, db_conn, store_cfg)
        metric_srid = _metric_srid(metric_crs)
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

        store_cfg = crm_task_store(config)
        metric_srid = _metric_srid(metric_crs)
        from .crm_office_points_map import refresh_office_points_on_map

        refresh_office_points_on_map(
            iface, db_conn, district, store_cfg, metric_srid
        )

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

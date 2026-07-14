-- Remove legacy localwork / AVR order tasks (without scoped prefix).
-- Pattern: MONITOR_WEBCRM/sql/20_oati_scoped_geometry_tasks.sql
--
-- Targets order tasks only (type «Новые ордера ОАТИ, АВР и земляные работы»).
-- Disruption tasks (type «Разрытия») are NOT touched.
-- Does NOT touch earthwork_id / oati_id (already scoped).
--
-- Before running: db_crm_legacy_localwork_avr_inventory.sql
-- Before running: SELECT * FROM crm.tasks_deletion_log ORDER BY deleted_at DESC LIMIT 50;
-- After this script: re-collect districts from section E of the inventory
--   via WebCRM or QGIS → «Получить задачу».
-- After this script: python -m collector.scheduler --run backfill_data_mos_crm_tasks
--
-- Usage:
--   psql -h HOST -U monitor -d monitor -f scripts/db_crm_legacy_localwork_avr_cleanup.sql

BEGIN;

CREATE TEMP TABLE legacy_localwork_avr_order_tasks ON COMMIT DROP AS
SELECT key
FROM crm.tasks
WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
  AND (
      (localwork_id IS NOT NULL AND localwork_id !~ '^(point|line|polygon):')
      OR (avr_mos_id IS NOT NULL AND avr_mos_id !~ '^(point|line|polygon):')
  )
  AND NOT (
      user_created IS NOT NULL
      AND user_created[1] ILIKE '%etl%'
  );

-- Dry-run: SELECT COUNT(*) FROM legacy_localwork_avr_order_tasks;

DELETE FROM crm.tasks_field
WHERE task_key IN (SELECT key FROM legacy_localwork_avr_order_tasks);

DELETE FROM crm.tasks_done_legal
WHERE task_key IN (SELECT key FROM legacy_localwork_avr_order_tasks);

DELETE FROM crm.tasks_done_illegal
WHERE task_key IN (SELECT key FROM legacy_localwork_avr_order_tasks);

DELETE FROM crm.tasks_clear
WHERE task_key IN (SELECT key FROM legacy_localwork_avr_order_tasks);

DELETE FROM crm.tasks
WHERE key IN (SELECT key FROM legacy_localwork_avr_order_tasks);

COMMIT;

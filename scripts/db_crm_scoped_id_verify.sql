-- Verification queries for scoped business id on crm.tasks.
-- Run after MONITOR_WEBCRM/sql/19_scoped_geometry_task_ids.sql,
-- MONITOR_WEBCRM/sql/20_oati_scoped_geometry_tasks.sql, and
-- MONITOR_WEBCRM/sql/21_localwork_avr_scoped_geometry_tasks.sql.
--
-- Usage: psql -h HOST -U monitor -d monitor -f scripts/db_crm_scoped_id_verify.sql

\echo '=== 1. Scoped ids present (earthwork sample) ==='
SELECT earthwork_id
FROM crm.tasks
WHERE earthwork_id LIKE 'point:%'
LIMIT 1;

\echo '=== 2. Duplicate business ids (expect 0 rows) ==='
SELECT task_column, business_id, COUNT(*) AS cnt
FROM (
    SELECT 'oati_id' AS task_column, oati_id AS business_id
    FROM crm.tasks WHERE oati_id IS NOT NULL
    UNION ALL
    SELECT 'earthwork_id', earthwork_id
    FROM crm.tasks WHERE earthwork_id IS NOT NULL
    UNION ALL
    SELECT 'localwork_id', localwork_id
    FROM crm.tasks WHERE localwork_id IS NOT NULL
    UNION ALL
    SELECT 'avr_mos_id', avr_mos_id
    FROM crm.tasks WHERE avr_mos_id IS NOT NULL
) s
GROUP BY 1, 2
HAVING COUNT(*) > 1;

\echo '=== 3. Legacy OATI order tasks without scoped prefix (expect 0 after migration 20) ==='
SELECT key, oati_id, type
FROM crm.tasks
WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
  AND oati_id IS NOT NULL
  AND oati_id !~ '^(point|line|polygon):'
LIMIT 20;

\echo '=== 4. Legacy numeric earthwork ids (expect 0 after migration 19) ==='
SELECT key, earthwork_id
FROM crm.tasks
WHERE earthwork_id ~ '^[0-9]+$'
LIMIT 20;

\echo '=== 5. Partial unique indexes on crm.tasks ==='
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'crm'
  AND tablename = 'tasks'
  AND indexname LIKE 'tasks_uq_%'
ORDER BY indexname;

\echo '=== 6. Legacy localwork/avr order tasks without scoped prefix (expect 0 after migration 21) ==='
SELECT key, localwork_id, avr_mos_id, type
FROM crm.tasks
WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
  AND (
      (localwork_id IS NOT NULL AND localwork_id !~ '^(point|line|polygon):')
      OR (avr_mos_id IS NOT NULL AND avr_mos_id !~ '^(point|line|polygon):')
  )
LIMIT 20;

\echo '=== 7. Legacy numeric localwork/avr ids (expect 0 after migration 21 + re-collect) ==='
SELECT key, localwork_id, avr_mos_id
FROM crm.tasks
WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
  AND (
      localwork_id ~ '^[0-9]+$'
      OR avr_mos_id ~ '^[0-9]+$'
  )
LIMIT 20;

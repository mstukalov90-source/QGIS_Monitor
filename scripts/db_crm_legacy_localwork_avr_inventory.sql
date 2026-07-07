-- Read-only inventory of legacy localwork / AVR order tasks (without scoped prefix).
-- Resolves rayon via data_mos geometry + odh_export.hood (EPSG:32637).
--
-- Does NOT modify data. Run before db_crm_legacy_localwork_avr_cleanup.sql.
--
-- Usage:
--   psql -h HOST -U monitor -d monitor -f scripts/db_crm_legacy_localwork_avr_inventory.sql

\set ON_ERROR_STOP on

\echo '=== A. Summary ==='
WITH legacy AS (
    SELECT
        key,
        localwork_id,
        avr_mos_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND (
          (localwork_id IS NOT NULL AND localwork_id !~ '^(point|line|polygon):')
          OR (avr_mos_id IS NOT NULL AND avr_mos_id !~ '^(point|line|polygon):')
      )
)
SELECT
    COUNT(*) FILTER (WHERE localwork_id IS NOT NULL) AS legacy_localwork_tasks,
    COUNT(*) FILTER (WHERE avr_mos_id IS NOT NULL) AS legacy_avr_tasks,
    COUNT(*) AS legacy_total,
    (
        SELECT COUNT(*)
        FROM crm.tasks_field f
        JOIN legacy l ON l.key = f.task_key
    ) AS in_tasks_field,
    (
        SELECT COUNT(*)
        FROM crm.tasks_done_legal d
        JOIN legacy l ON l.key = d.task_key
    ) AS in_tasks_done_legal,
    (
        SELECT COUNT(*)
        FROM crm.tasks_done_illegal d
        JOIN legacy l ON l.key = d.task_key
    ) AS in_tasks_done_illegal,
    (
        SELECT COUNT(*)
        FROM crm.tasks_clear c
        JOIN legacy l ON l.key = c.task_key
    ) AS in_tasks_clear
FROM legacy;

\echo '=== B. By rayon ==='
WITH legacy_tasks AS (
    SELECT
        key AS task_key,
        'localwork_id' AS task_column,
        localwork_id AS business_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND localwork_id IS NOT NULL
      AND localwork_id !~ '^(point|line|polygon):'

    UNION ALL

    SELECT
        key,
        'avr_mos_id',
        avr_mos_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND avr_mos_id IS NOT NULL
      AND avr_mos_id !~ '^(point|line|polygon):'
),
resolved_geom AS (
    SELECT lt.task_key, lt.task_column, lt.business_id, src.geom, src.match_method
    FROM legacy_tasks lt
    JOIN LATERAL (
        SELECT p.geom, 'localwork:id:point' AS match_method
        FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT l.geom, 'localwork:id:line'
        FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT g.geom, 'localwork:id:polygon'
        FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT p.geom, 'localwork:global_id:point'
        FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.global_id::text = lt.business_id
        UNION ALL
        SELECT l.geom, 'localwork:global_id:line'
        FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.global_id::text = lt.business_id
        UNION ALL
        SELECT g.geom, 'localwork:global_id:polygon'
        FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.global_id::text = lt.business_id
        UNION ALL
        SELECT p.geom, 'avr:id:point'
        FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT l.geom, 'avr:id:line'
        FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT g.geom, 'avr:id:polygon'
        FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT p.geom, 'avr:em_call_reg_num:point'
        FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(p.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT l.geom, 'avr:em_call_reg_num:line'
        FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(l.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT g.geom, 'avr:em_call_reg_num:polygon'
        FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(g.em_call_reg_num::text) = trim(lt.business_id)
    ) src ON true
),
with_rayon AS (
    SELECT DISTINCT ON (rg.task_key, rg.task_column)
        rg.task_key,
        rg.task_column,
        rg.business_id,
        rg.match_method,
        h.rayon
    FROM resolved_geom rg
    JOIN odh_export.hood h
      ON ST_Within(
          ST_Transform(rg.geom, 32637),
          ST_Transform(h.geom, 32637)
      )
    ORDER BY rg.task_key, rg.task_column, h.rayon
),
enriched AS (
    SELECT
        lt.task_key,
        lt.task_column,
        lt.business_id,
        COALESCE(wr.rayon, '(не определён)') AS rayon,
        COALESCE(wr.match_method, 'orphan') AS match_method
    FROM legacy_tasks lt
    LEFT JOIN with_rayon wr
      ON wr.task_key = lt.task_key AND wr.task_column = lt.task_column
)
SELECT rayon, task_column, COUNT(*) AS cnt
FROM enriched
GROUP BY rayon, task_column
ORDER BY rayon, task_column;

\echo '=== C. Detail ==='
WITH legacy_tasks AS (
    SELECT
        key AS task_key,
        'localwork_id' AS task_column,
        localwork_id AS business_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND localwork_id IS NOT NULL
      AND localwork_id !~ '^(point|line|polygon):'

    UNION ALL

    SELECT
        key,
        'avr_mos_id',
        avr_mos_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND avr_mos_id IS NOT NULL
      AND avr_mos_id !~ '^(point|line|polygon):'
),
resolved_geom AS (
    SELECT lt.task_key, lt.task_column, lt.business_id, src.geom, src.match_method
    FROM legacy_tasks lt
    JOIN LATERAL (
        SELECT p.geom, 'localwork:id:point' AS match_method
        FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT l.geom, 'localwork:id:line'
        FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT g.geom, 'localwork:id:polygon'
        FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT p.geom, 'localwork:global_id:point'
        FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.global_id::text = lt.business_id
        UNION ALL
        SELECT l.geom, 'localwork:global_id:line'
        FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.global_id::text = lt.business_id
        UNION ALL
        SELECT g.geom, 'localwork:global_id:polygon'
        FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.global_id::text = lt.business_id
        UNION ALL
        SELECT p.geom, 'avr:id:point'
        FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT l.geom, 'avr:id:line'
        FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT g.geom, 'avr:id:polygon'
        FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT p.geom, 'avr:em_call_reg_num:point'
        FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(p.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT l.geom, 'avr:em_call_reg_num:line'
        FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(l.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT g.geom, 'avr:em_call_reg_num:polygon'
        FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(g.em_call_reg_num::text) = trim(lt.business_id)
    ) src ON true
),
with_rayon AS (
    SELECT DISTINCT ON (rg.task_key, rg.task_column)
        rg.task_key,
        rg.task_column,
        rg.business_id,
        rg.match_method,
        h.rayon
    FROM resolved_geom rg
    JOIN odh_export.hood h
      ON ST_Within(
          ST_Transform(rg.geom, 32637),
          ST_Transform(h.geom, 32637)
      )
    ORDER BY rg.task_key, rg.task_column, h.rayon
),
enriched AS (
    SELECT
        lt.task_key,
        lt.task_column,
        lt.business_id,
        COALESCE(wr.rayon, '(не определён)') AS rayon,
        COALESCE(wr.match_method, 'orphan') AS match_method
    FROM legacy_tasks lt
    LEFT JOIN with_rayon wr
      ON wr.task_key = lt.task_key AND wr.task_column = lt.task_column
)
SELECT
    e.task_key,
    e.task_column,
    e.business_id,
    e.rayon,
    e.match_method,
    (f.task_key IS NOT NULL) AS in_field,
    (dl.task_key IS NOT NULL) AS in_done_legal,
    (di.task_key IS NOT NULL) AS in_done_illegal,
    (c.task_key IS NOT NULL) AS in_clear
FROM enriched e
LEFT JOIN crm.tasks_field f ON f.task_key = e.task_key
LEFT JOIN crm.tasks_done_legal dl ON dl.task_key = e.task_key
LEFT JOIN crm.tasks_done_illegal di ON di.task_key = e.task_key
LEFT JOIN crm.tasks_clear c ON c.task_key = e.task_key
ORDER BY e.rayon, e.task_column, e.business_id;

\echo '=== D. Orphans (no geometry in data_mos) ==='
WITH legacy_tasks AS (
    SELECT
        key AS task_key,
        'localwork_id' AS task_column,
        localwork_id AS business_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND localwork_id IS NOT NULL
      AND localwork_id !~ '^(point|line|polygon):'

    UNION ALL

    SELECT
        key,
        'avr_mos_id',
        avr_mos_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND avr_mos_id IS NOT NULL
      AND avr_mos_id !~ '^(point|line|polygon):'
),
resolved AS (
    SELECT DISTINCT lt.task_key, lt.task_column
    FROM legacy_tasks lt
    WHERE EXISTS (
        SELECT 1
        FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.global_id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.global_id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.global_id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT 1 FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(p.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT 1 FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(l.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT 1 FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(g.em_call_reg_num::text) = trim(lt.business_id)
    )
)
SELECT lt.task_key, lt.task_column, lt.business_id
FROM legacy_tasks lt
LEFT JOIN resolved r
  ON r.task_key = lt.task_key AND r.task_column = lt.task_column
WHERE r.task_key IS NULL
ORDER BY lt.task_column, lt.business_id;

\echo '=== E. Re-collect list (rayons with legacy, excluding orphans) ==='
WITH legacy_tasks AS (
    SELECT
        key AS task_key,
        'localwork_id' AS task_column,
        localwork_id AS business_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND localwork_id IS NOT NULL
      AND localwork_id !~ '^(point|line|polygon):'

    UNION ALL

    SELECT
        key,
        'avr_mos_id',
        avr_mos_id
    FROM crm.tasks
    WHERE type = 'Новые ордера ОАТИ, АВР и земляные работы'
      AND avr_mos_id IS NOT NULL
      AND avr_mos_id !~ '^(point|line|polygon):'
),
resolved_geom AS (
    SELECT lt.task_key, lt.task_column, src.geom
    FROM legacy_tasks lt
    JOIN LATERAL (
        SELECT p.geom
        FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT l.geom FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT g.geom FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT p.geom FROM data_mos.items_62441_points p
        WHERE lt.task_column = 'localwork_id' AND p.global_id::text = lt.business_id
        UNION ALL
        SELECT l.geom FROM data_mos.items_62441_lines l
        WHERE lt.task_column = 'localwork_id' AND l.global_id::text = lt.business_id
        UNION ALL
        SELECT g.geom FROM data_mos.items_62441_polygons g
        WHERE lt.task_column = 'localwork_id' AND g.global_id::text = lt.business_id
        UNION ALL
        SELECT p.geom FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id' AND p.id::text = lt.business_id
        UNION ALL
        SELECT l.geom FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id' AND l.id::text = lt.business_id
        UNION ALL
        SELECT g.geom FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id' AND g.id::text = lt.business_id
        UNION ALL
        SELECT p.geom FROM data_mos.items_62461_points p
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(p.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT l.geom FROM data_mos.items_62461_lines l
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(l.em_call_reg_num::text) = trim(lt.business_id)
        UNION ALL
        SELECT g.geom FROM data_mos.items_62461_polygons g
        WHERE lt.task_column = 'avr_mos_id'
          AND trim(g.em_call_reg_num::text) = trim(lt.business_id)
    ) src ON true
),
with_rayon AS (
    SELECT DISTINCT ON (rg.task_key, rg.task_column)
        rg.task_key,
        h.rayon
    FROM resolved_geom rg
    JOIN odh_export.hood h
      ON ST_Within(
          ST_Transform(rg.geom, 32637),
          ST_Transform(h.geom, 32637)
      )
    ORDER BY rg.task_key, rg.task_column, h.rayon
)
SELECT DISTINCT rayon
FROM with_rayon
WHERE rayon IS NOT NULL
ORDER BY rayon;

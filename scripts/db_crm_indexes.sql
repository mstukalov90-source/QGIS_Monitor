-- CRM performance indexes (run on production with CONCURRENTLY where noted).
-- Dev/sandbox: psql -f scripts/db_crm_indexes.sql

-- crm.tasks_area
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_area_rayon
    ON crm.tasks_area (rayon) WHERE geom IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_area_geom
    ON crm.tasks_area USING GIST (geom);

-- crm.tasks — lookup by business identifiers
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_photo_uuid
    ON crm.tasks (photo_uuid) WHERE photo_uuid IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_photo_lens
    ON crm.tasks (photo_lens) WHERE photo_lens IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_ogh_id
    ON crm.tasks (ogh_id) WHERE ogh_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_oati_id
    ON crm.tasks (oati_id) WHERE oati_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_earthwork_id
    ON crm.tasks (earthwork_id) WHERE earthwork_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_localwork_id
    ON crm.tasks (localwork_id) WHERE localwork_id IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tasks_avr_mos_id
    ON crm.tasks (avr_mos_id) WHERE avr_mos_id IS NOT NULL;

-- Office / field geometries
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_office_task_points_geom
    ON crm.office_task_points USING GIST (point);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_reports_point
    ON mggt_field.reports USING GIST (point);

-- District boundaries
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_hood_rayon
    ON odh_export.hood (rayon);

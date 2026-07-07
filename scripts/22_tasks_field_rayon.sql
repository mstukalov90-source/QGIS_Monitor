-- Rayon сессии при отправке задачи «В поле» (синхронизировано с MONITOR_WEBCRM/sql/22_tasks_field_rayon.sql).

ALTER TABLE crm.tasks_field
    ADD COLUMN IF NOT EXISTS rayon TEXT;

CREATE INDEX IF NOT EXISTS idx_tasks_field_rayon
    ON crm.tasks_field (rayon)
    WHERE rayon IS NOT NULL;
